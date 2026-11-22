"""S1's PPE tests, P1-P7 (TD-slice-1 §8.6; SPEC §8.3), run only by the `ppe` job against a
PPE started with `ppe/ppe up --ci`.

Black-box (REQ-62): nothing here imports `seriatim_node`. The tests use:
- the sidecar, node-a, at SERIATIM_BASE_URL;
- plain Ollama, through the PPE's debug port, at SERIATIM_PPE_URL, for comparison;
- the capture of the bytes Ollama receives (cap-a's pcap, SERIATIM_PPE_PCAP);
- for P7 only, `docker compose kill` and `start` on node-a.

node-a's drain_s is read from `ppe/compose.yaml`, so the bounds follow the PPE's own setting.
They run in this file's order, after S0b's checks: P7 restarts the sidecar, so it is last.
"""

from __future__ import annotations

import bisect
import http.client
import json
import math
import os
import socket
import statistics
import struct
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

pytestmark = pytest.mark.behaviour

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "ppe" / "compose.yaml"
PROJECT = "seriatim-ppe"
BASE_URL = os.environ.get("SERIATIM_BASE_URL", "http://127.0.0.1:11436").rstrip("/")
DIRECT_URL = os.environ.get("SERIATIM_PPE_URL", "http://127.0.0.1:11434").rstrip("/")
PCAP = Path(
    os.environ.get("SERIATIM_PPE_PCAP", str(ROOT / "ppe" / "data" / "capture" / "ollama-a.pcap"))
)
NODE_ENV = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["node-a"]["environment"]
DRAIN_S = float(NODE_ENV["SERIATIM_NODE_DRAIN_S"])
NODE_ID = NODE_ENV["SERIATIM_NODE_ID"]
MODEL = "qwen2.5:0.5b"
EMBED_MODEL = "all-minilm:22m"
# Generation on the PPE's CPUs takes seconds; nothing here waits longer than this for one answer.
GENERATION_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
WAIT_S = 60.0  # the upper bound of every poll for a state; a hang fails the test instead
POLL_S = 0.2
LATENCY_P99_S = 0.25  # TD §7: "The PPE bound is p99 < 250 ms"
# The gated routes of the infer scope (TD §2.2 rule 4): what P2 counts as an inference request.
INFER_PATHS = frozenset(
    {
        b"/api/generate",
        b"/api/chat",
        b"/api/embed",
        b"/api/embeddings",
        b"/v1/chat/completions",
        b"/v1/completions",
        b"/v1/embeddings",
    }
)
# Not on any list, so the sidecar gates it (TD §2.2 rule 5) and Ollama answers 404 at once,
# without touching a model: a cheap way to ask the gate whether its slot is free.
GATE_PROBE = "/api/seriatim-ppe-gate-probe"
# A prompt that keeps a micro-model generating, so a stream is still running when it's needed.
LONG_PROMPT = "Count from 1 to 5000, one number per line, with no other text."
SERIATIM_HEADERS = frozenset({"x-seriatim-gate", "x-seriatim-node", "x-request-id"})


def rid(label: str) -> str:
    """A valid `X-Request-Id` (TD §2.4 step 2), which the sidecar keeps and sends upstream."""
    return f"{label}-{uuid.uuid4().hex}"


def chat(prompt: str, num_predict: int, *, stream: bool = True, **options: object) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "options": {"temperature": 0, "seed": 7, "num_predict": num_predict, **options},
    }


def gated_probe() -> httpx.Response:
    return httpx.get(f"{BASE_URL}{GATE_PROBE}", timeout=10)


def wait_free() -> None:
    """Wait until the gate admits a request: a drain (10 s after a disconnect) or the startup
    quarantine left by an earlier test is a time window, so this polls it."""
    deadline = time.monotonic() + DRAIN_S + WAIT_S
    while True:
        r = gated_probe()
        if r.status_code not in (429, 503):
            assert r.status_code == 404, r.text  # Ollama's own answer: the probe was admitted
            assert r.headers["x-seriatim-gate"] == "acquired"
            return
        assert r.json()["error_code"] in ("GPU_BUSY", "NODE_STARTING"), r.text
        assert time.monotonic() < deadline, f"the gate stayed closed: {r.text}"
        time.sleep(POLL_S)


def wait_for(what: str, check: Callable[[], object], timeout: float = WAIT_S) -> object:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(POLL_S)


def ndjson(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.split(b"\n") if line.strip()]


@dataclass
class Raw:
    status: int
    headers: list[tuple[str, str]]
    body: bytes


def raw_get(base: str, path: str) -> Raw:
    """One GET with http.client, so the headers and body bytes are exactly what was sent."""
    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return Raw(resp.status, resp.getheaders(), resp.read())
    finally:
        conn.close()


def compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args],
        check=True,
        capture_output=True,
        timeout=120,
    )


def summary(text: str) -> None:
    """Print a result, and add it to the job summary when the job provides one."""
    print(text)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(text + "\n")


# --- The capture (cap-a: tcpdump -w on lo, `tcp dst port 11435`) ----------------------------


@dataclass
class Captured:
    """One HTTP request Ollama received, reassembled from the capture."""

    ts: float  # the capture time of the request's first byte (the host's clock)
    method: bytes
    target: bytes
    headers: dict[str, list[bytes]]
    body: bytes

    @property
    def request_id(self) -> str | None:
        values = self.headers.get("x-request-id")
        return values[0].decode("latin-1") if values else None


@dataclass
class _Conn:
    isn: int | None
    segments: list[tuple[int, float, bytes]] = field(default_factory=list)


def _pcap_packets(data: bytes) -> Iterator[tuple[float, int, bytes]]:
    """(timestamp, link type, frame) for each whole packet; a packet still being written at
    the end of the file is left out."""
    magic = data[:4]
    order = {
        b"\xd4\xc3\xb2\xa1": ("<", 1e6),
        b"\x4d\x3c\xb2\xa1": ("<", 1e9),
        b"\xa1\xb2\xc3\xd4": (">", 1e6),
        b"\xa1\xb2\x3c\x4d": (">", 1e9),
    }.get(magic)
    assert order, f"{PCAP} is not a pcap file (magic {magic.hex()})"
    endian, units = order
    linktype = struct.unpack(endian + "I", data[20:24])[0] & 0x0FFFFFFF
    off = 24
    while off + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack(endian + "IIII", data[off : off + 16])
        off += 16
        if off + incl > len(data):
            return
        yield sec + frac / units, linktype, data[off : off + incl]
        off += incl


def _ip_payload(linktype: int, frame: bytes) -> bytes | None:
    if linktype == 1:  # Ethernet, what Linux uses for lo
        proto, ip = frame[12:14], frame[14:]
    elif linktype == 113:  # Linux cooked capture
        proto, ip = frame[14:16], frame[16:]
    elif linktype == 276:  # Linux cooked capture v2
        proto, ip = frame[0:2], frame[20:]
    elif linktype in (101, 12):  # raw IP
        proto, ip = (b"\x08\x00" if frame[:1] and frame[0] >> 4 == 4 else b"\x86\xdd"), frame
    else:
        raise AssertionError(f"{PCAP}: link type {linktype} is not handled")
    return ip if proto in (b"\x08\x00", b"\x86\xdd") else None


def _tcp(ip: bytes) -> tuple[bytes, bytes, bytes] | None:
    """(flow key, TCP header, payload) of a TCP segment, or None."""
    if ip[0] >> 4 == 4:
        ihl, total = (ip[0] & 0x0F) * 4, struct.unpack("!H", ip[2:4])[0]
        if ip[9] != 6:
            return None
        addrs, seg = ip[12:20], ip[ihl:total]
    else:
        if ip[6] != 6:  # no extension headers on loopback
            return None
        addrs, seg = ip[8:40], ip[40 : 40 + struct.unpack("!H", ip[4:6])[0]]
    offset = (seg[12] >> 4) * 4
    return addrs + seg[0:4], seg[:offset], seg[offset:]


def _streams(data: bytes) -> Iterator[tuple[bytes, list[tuple[int, float]]]]:
    """Each captured connection's client-to-Ollama bytes, in order, with the capture time of
    the segment that starts at each offset."""
    conns: dict[bytes, list[_Conn]] = {}
    for ts, linktype, frame in _pcap_packets(data):
        ip = _ip_payload(linktype, frame)
        seg = _tcp(ip) if ip else None
        if seg is None:
            continue
        key, header, payload = seg
        seq, flags = struct.unpack("!I", header[4:8])[0], header[13]
        if flags & 0x02:  # SYN: a new connection, even on a reused port
            conns.setdefault(key, []).append(_Conn(isn=seq))
        elif payload:
            if key not in conns:
                conns[key] = [_Conn(isn=None)]
            conns[key][-1].segments.append((seq, ts, payload))
    for history in conns.values():
        for conn in history:
            if not conn.segments:
                continue
            base = conn.isn + 1 if conn.isn is not None else conn.segments[0][0]
            stream, starts = bytearray(), []
            for rel, ts, payload in sorted(
                ((seq - base) % 2**32, ts, p) for seq, ts, p in conn.segments
            ):
                if rel > len(stream):
                    break  # a gap: nothing after it can be placed
                new = payload[len(stream) - rel :]
                if new:  # else a retransmission of bytes already placed
                    starts.append((len(stream), ts))
                    stream += new
            yield bytes(stream), starts


def _requests(stream: bytes, starts: list[tuple[int, float]]) -> Iterator[Captured]:
    offsets = [o for o, _ in starts]
    i = 0
    while True:
        end = stream.find(b"\r\n\r\n", i)
        if end < 0:
            return
        lines = stream[i:end].split(b"\r\n")
        method, target, _version = lines[0].split(b" ", 2)
        headers: dict[str, list[bytes]] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers.setdefault(name.strip().lower().decode("latin-1"), []).append(value.strip())
        j = end + 4
        if "content-length" in headers:
            n = int(headers["content-length"][0])
            if j + n > len(stream):
                return  # still arriving
            body, after = stream[j : j + n], j + n
        elif b"chunked" in b",".join(headers.get("transfer-encoding", [])).lower():
            body, after = _dechunk(stream, j)
            if after is None:
                return
        else:
            body, after = b"", j
        ts = starts[bisect.bisect_right(offsets, i) - 1][1]
        yield Captured(ts, method, target, headers, body)
        i = after


def _dechunk(stream: bytes, j: int) -> tuple[bytes, int | None]:
    body = bytearray()
    while True:
        eol = stream.find(b"\r\n", j)
        if eol < 0:
            return b"", None
        size = int(stream[j:eol].split(b";")[0], 16)
        j = eol + 2
        if size == 0:
            trailer_end = stream.find(b"\r\n", j)
            return (bytes(body), trailer_end + 2) if trailer_end >= 0 else (b"", None)
        if j + size + 2 > len(stream):
            return b"", None
        body += stream[j : j + size]
        j += size + 2


def captured() -> list[Captured]:
    """Every request Ollama has received since the PPE started, oldest first."""
    found = [r for s, starts in _streams(PCAP.read_bytes()) for r in _requests(s, starts)]
    return sorted(found, key=lambda r: r.ts)


def wait_captured(ids: set[str]) -> dict[str, list[Captured]]:
    """The captured requests carrying each id, once every id has reached the capture file
    (tcpdump writes each packet as it arrives, `-U`, but the file is read from outside)."""

    def check() -> dict[str, list[Captured]] | None:
        by_id: dict[str, list[Captured]] = {}
        for r in captured():
            if r.request_id in ids:
                by_id.setdefault(r.request_id, []).append(r)
        return by_id if set(by_id) == ids else None

    return wait_for(f"requests {sorted(ids)} in the capture", check)


# --- The tests --------------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def sidecar_up() -> None:
    """node-a answers its health check: Ollama answers it, and the quarantine has passed."""

    def ok() -> bool:
        try:
            r = httpx.get(f"{BASE_URL}/seriatim/health", timeout=10)
        except httpx.TransportError:
            return False
        return r.status_code == 200 and r.json() == {"status": "ok"}

    wait_for("node-a's health check", ok, timeout=DRAIN_S + WAIT_S)
    assert httpx.get(f"{BASE_URL}/seriatim/health").headers["x-seriatim-node"] == NODE_ID


@pytest.mark.td("P5")
def test_p5_listings_through_the_sidecar_equal_plain_ollama():
    def comparable(raw: Raw) -> list[tuple[str, str]]:
        dropped = SERIATIM_HEADERS | {"date"}
        return sorted((k.lower(), v) for k, v in raw.headers if k.lower() not in dropped)

    for path in ("/api/tags", "/api/ps", "/api/version"):
        direct = raw_get(DIRECT_URL, path)
        side = raw_get(BASE_URL, path)
        assert direct.status == side.status == 200, path
        assert side.body == direct.body, path
        assert comparable(side) == comparable(direct), path
        mine = {k.lower(): v for k, v in side.headers if k.lower() in SERIATIM_HEADERS}
        assert mine["x-seriatim-gate"] == "ungated" and mine["x-seriatim-node"] == NODE_ID
        assert mine["x-request-id"]
        print(f"P5 {path}: {len(side.body)} bytes, equal")


@pytest.mark.td("P1")
def test_p1_the_bytes_ollama_receives_are_the_bytes_sent_and_num_ctx_is_loaded():
    for num_ctx in (6144, 30000):
        wait_free()
        request_id = rid(f"p1-{num_ctx}")
        # Spaced, ordered and escaped as no JSON encoder would, so a re-serialised body differs.
        body = (
            '{"model":  "qwen2.5:0.5b", "prompt": "Name one colour, caf\\u00e9 owner.",'
            ' "stream": false, "options": {"temperature": 0.30, "top_k": 17,'
            f' "num_ctx": {num_ctx},  "seed": 4242, "num_predict": 4}}}}'
        ).encode()
        assert json.loads(body)["options"]["num_ctx"] == num_ctx
        r = httpx.post(
            f"{BASE_URL}/api/generate",
            content=body,
            headers={"content-type": "application/json", "x-request-id": request_id},
            timeout=GENERATION_TIMEOUT,
        )
        assert r.status_code == 200, r.text
        assert r.headers["x-seriatim-gate"] == "acquired"
        assert r.json()["done"] is True
        [wire] = wait_captured({request_id})[request_id]
        assert (wire.method, wire.target) == (b"POST", b"/api/generate")
        assert wire.body == body
        assert wire.headers["content-length"] == [str(len(body)).encode()]
        ps = {m["name"]: m for m in httpx.get(f"{DIRECT_URL}/api/ps", timeout=30).json()["models"]}
        assert ps[MODEL]["context_length"] == num_ctx, ps[MODEL]
        print(f"P1 num_ctx {num_ctx}: {len(body)} bytes on the wire, equal; context_length ok")


@pytest.mark.td("P3")
def test_p3_a_stream_arrives_as_ndjson_before_it_ends():
    wait_free()
    data = b""
    first = None
    t0 = time.perf_counter()
    with httpx.stream(
        "POST",
        f"{BASE_URL}/api/chat",
        json=chat("Write three short sentences about the sea.", 48),
        timeout=GENERATION_TIMEOUT,
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers["x-seriatim-gate"] == "acquired"
        for chunk in r.iter_raw():
            if first is None and chunk:
                first = time.perf_counter() - t0
            data += chunk
    total = time.perf_counter() - t0
    records = ndjson(data)
    assert data.endswith(b"\n")
    assert len(records) > 2
    assert all(rec["done"] is False for rec in records[:-1])
    assert records[-1]["done"] is True
    assert first is not None and first < total, (first, total)
    print(f"P3: {len(records)} records, first byte {first * 1000:.0f} ms of {total * 1000:.0f} ms")


@pytest.mark.td("P2")
def test_p2_an_overlapping_request_gets_429_fast_and_never_reaches_ollama():
    trials = 20
    latencies, windows, ids = [], [], []
    with httpx.Client(timeout=GENERATION_TIMEOUT) as first, httpx.Client(timeout=10) as second:
        for trial in range(trials):
            wait_free()
            one, two = rid(f"p2-{trial}-one"), rid(f"p2-{trial}-two")
            t_start = time.time()
            with first.stream(
                "POST",
                f"{BASE_URL}/api/chat",
                json=chat(LONG_PROMPT, 128),
                headers={"x-request-id": one},
            ) as r1:
                assert r1.status_code == 200 and r1.headers["x-seriatim-gate"] == "acquired"
                lines = r1.iter_lines()
                head = json.loads(next(lines))  # request 1's first chunk has arrived
                assert head["done"] is False
                t = time.perf_counter()
                r2 = second.post(
                    f"{BASE_URL}/api/chat", json=chat("Say hi.", 4), headers={"x-request-id": two}
                )
                latencies.append(time.perf_counter() - t)
                assert r2.status_code == 429, r2.text
                assert r2.headers["x-seriatim-gate"] == "busy"
                assert int(r2.headers["retry-after"]) >= 1
                assert r2.json()["error_code"] == "GPU_BUSY"
                rest = [json.loads(line) for line in lines if line]
                assert rest and rest[-1]["done"] is True  # a clean end, so the slot is freed
            windows.append((t_start, time.time()))
            ids.append((one, two))
    by_id = wait_captured({one for one, _ in ids})
    seen = {r.request_id for r in captured()}
    for (one, two), (t_start, t_end) in zip(ids, windows, strict=True):
        assert len(by_id[one]) == 1
        assert two not in seen  # request 2 never reached Ollama
        inference = [
            r
            for r in captured()
            if t_start <= r.ts <= t_end and r.method == b"POST" and r.target in INFER_PATHS
        ]
        assert [r.request_id for r in inference] == [one], (one, inference)
    p99 = sorted(latencies)[math.ceil(0.99 * trials) - 1]
    summary(
        f"P2: {trials} trials; 429 latency p50 {statistics.median(latencies) * 1000:.1f} ms, "
        f"p99 {p99 * 1000:.1f} ms (bound {LATENCY_P99_S * 1000:.0f} ms)"
    )
    assert p99 < LATENCY_P99_S


@pytest.mark.td("P4")
def test_p4_a_pull_completes_while_a_gated_request_is_held():
    wait_free()
    u = urlsplit(BASE_URL)
    body = json.dumps(chat("Say hi.", 4, stream=False)).encode()
    held_id = rid("p4-held")
    head = (
        f"POST /api/chat HTTP/1.1\r\nHost: {u.netloc}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nX-Request-Id: {held_id}\r\n\r\n"
    ).encode()

    def busy() -> bool:
        r = gated_probe()
        return r.status_code == 429 and r.headers["x-seriatim-gate"] == "busy"

    with socket.create_connection((u.hostname, u.port), timeout=60) as held:
        # The gate admits on the headers, then waits for the body (TD §3.3 lines 6 and 12):
        # half a body holds the slot for as long as the test needs it, with no model running.
        held.sendall(head + body[:8])
        wait_for("the held request to take the slot", busy, timeout=10)

        def tags() -> set[str]:
            r = httpx.get(f"{BASE_URL}/api/tags", timeout=30)
            assert r.status_code == 200 and r.headers["x-seriatim-gate"] == "ungated"
            return {m["name"] for m in r.json()["models"]}

        # So that /api/tags listing the model proves the pull installed it.
        r = httpx.request("DELETE", f"{BASE_URL}/api/delete", json={"model": EMBED_MODEL})
        assert r.status_code == 200, r.text
        assert EMBED_MODEL not in tags()
        progress = b""
        t0 = time.perf_counter()
        with httpx.stream(
            "POST", f"{BASE_URL}/api/pull", json={"model": EMBED_MODEL}, timeout=GENERATION_TIMEOUT
        ) as r:
            assert r.status_code == 200 and r.headers["x-seriatim-gate"] == "ungated"
            assert r.headers["content-type"].startswith("application/x-ndjson")
            for chunk in r.iter_raw():
                progress += chunk
        records = ndjson(progress)
        assert not [rec for rec in records if "error" in rec], records
        assert records[-1] == {"status": "success"}, records[-3:]
        assert EMBED_MODEL in tags()
        assert busy(), "the gated request was no longer held when the pull ended"
        took = time.perf_counter() - t0
        print(f"P4: pull of {EMBED_MODEL}, {len(records)} records in {took:.1f} s")

        held.sendall(body[8:])  # the held request then runs as normal
        resp = http.client.HTTPResponse(held)
        resp.begin()
        answer = resp.read()
        assert resp.status == 200, answer
        assert resp.getheader("x-seriatim-gate") == "acquired"
        assert json.loads(answer)["done"] is True


def ttft(base: str, prompt: str) -> float:
    """Seconds from sending a streamed generation to its first record."""
    t = time.perf_counter()
    with httpx.stream(
        "POST",
        f"{base}/api/generate",
        json={
            "model": MODEL,
            "prompt": prompt,
            "options": {"temperature": 0, "seed": 7, "num_predict": 1},
        },
        timeout=GENERATION_TIMEOUT,
    ) as r:
        assert r.status_code == 200
        for chunk in r.iter_raw():
            if chunk:
                return time.perf_counter() - t
    raise AssertionError("the probe streamed no record")


@pytest.mark.td("P6")
def test_p6_a_disconnect_lets_the_next_generation_start_soon():
    wait_free()
    ttft(DIRECT_URL, "Hi.")  # loads the model with the options every probe here uses
    baseline = statistics.median(ttft(DIRECT_URL, "Hi.") for _ in range(3))
    with httpx.Client(timeout=GENERATION_TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{BASE_URL}/api/generate",
            json={
                "model": MODEL,
                "prompt": LONG_PROMPT,
                "options": {"temperature": 0, "seed": 7, "num_predict": 4096},
            },
        ) as r:
            assert r.status_code == 200 and r.headers["x-seriatim-gate"] == "acquired"
            lines = r.iter_lines()
            for _ in range(3):  # mid-generation
                assert json.loads(next(lines))["done"] is False
        # Leaving both blocks unread closes the connection: the client has gone.
    after = ttft(DIRECT_URL, "Hi.")
    extra = after - baseline
    summary(
        f"P6 cancellation latency (CPU evidence only): idle first token {baseline * 1000:.0f} ms, "
        f"after a disconnect {after * 1000:.0f} ms, extra {extra * 1000:.0f} ms "
        f"(limit drain_s / 2 = {DRAIN_S / 2:g} s)"
    )
    assert extra <= DRAIN_S / 2


@pytest.mark.td("P7")
def test_p7_a_restart_mid_generation_aborts_the_stream_and_starts_in_quarantine():
    wait_free()
    records = []
    with httpx.stream(
        "POST", f"{BASE_URL}/api/chat", json=chat(LONG_PROMPT, 4096), timeout=GENERATION_TIMEOUT
    ) as r:
        assert r.status_code == 200
        lines = r.iter_lines()
        records.append(json.loads(next(lines)))
        compose("kill", "node-a")
        with pytest.raises(httpx.TransportError):  # the stream ends without its terminator
            for line in lines:
                if line:
                    records.append(json.loads(line))
    assert not any(rec.get("done") for rec in records)
    compose("start", "node-a")

    def gated() -> httpx.Response | None:
        try:
            return httpx.post(
                f"{BASE_URL}/api/chat", json=chat("Say hi.", 4, stream=False), timeout=60
            )
        except httpx.TransportError:  # not listening yet (the host's port forwarder resets)
            return None

    first = wait_for("node-a to accept again", gated, timeout=WAIT_S)
    t_first = time.monotonic()
    assert first.status_code == 503, first.text
    assert first.json()["error_code"] == "NODE_STARTING"
    assert first.headers["x-seriatim-gate"] == "refused"
    retry_after = int(first.headers["retry-after"])
    assert 1 <= retry_after <= math.ceil(DRAIN_S)
    while True:
        r = gated()
        assert r is not None
        if r.status_code != 503:
            break
        assert r.json()["error_code"] == "NODE_STARTING"
        assert time.monotonic() - t_first < DRAIN_S + WAIT_S
        time.sleep(POLL_S)
    waited = time.monotonic() - t_first
    assert r.status_code == 200, r.text
    assert r.headers["x-seriatim-gate"] == "acquired"
    # Retry-After is the quarantine left, rounded up, so the 200 can't come a second earlier.
    assert waited >= retry_after - 1, (waited, retry_after)
    assert waited <= retry_after + 2 * POLL_S + 5, (waited, retry_after)
    summary(
        f"P7: first answer after the restart NODE_STARTING (Retry-After {retry_after} s), "
        f"200 after {waited:.1f} s more (drain_s {DRAIN_S:g} s)"
    )
