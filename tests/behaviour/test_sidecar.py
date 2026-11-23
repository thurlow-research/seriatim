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
BODY_TIMEOUT_S = float(NODE_ENV["SERIATIM_NODE_BODY_TIMEOUT_S"])  # bounds P4's held request
MODEL = "qwen2.5:0.5b"
EMBED_MODEL = "all-minilm:22m"
# Generation on the PPE's CPUs takes seconds; nothing here waits longer than this for one answer.
GENERATION_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
WAIT_S = 60.0  # the upper bound of every poll for a state; a hang fails the test instead
POLL_S = 0.2
LATENCY_P99_S = 0.25  # TD §7: "The PPE bound is p99 < 250 ms"
# P3: 48 tokens take over a second on the PPE's CPUs (2.2 s in CI), so a streamed answer's
# first and final records are far more than this apart; a buffered one's arrive together.
MIN_STREAM_GAP_S = 0.25
# P7: from `docker compose start` to the sidecar's gate existing (the container, the
# interpreter and the imports); its quarantine ends drain_s after that.
STARTUP_ALLOWANCE_S = 15.0
# P7: the most time between the new gate being made and its first NODE_STARTING answer
# reaching the test: uvicorn binding after the lifespan, one POLL_S poll interval, and the
# request itself through the host's port forwarder.
QUARANTINE_MARGIN_S = 1.0
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
    """Wait until the gate admits a request, and that request's slot has been freed: a drain
    (10 s after a disconnect) or the startup quarantine left by an earlier test is a time
    window, so this polls it.

    The probe's answer has a `Content-Length`, so the client has all of it before the
    sidecar's `finally` closes upstream and frees the slot (§3.3 steps (a) to (c)). A second,
    ungated request on the same connection is handled only after the probe's final message,
    which comes after the slot was freed, so once it answers the gate is free."""
    deadline = time.monotonic() + DRAIN_S + WAIT_S
    with httpx.Client(timeout=10) as client:
        while True:
            r = client.get(f"{BASE_URL}{GATE_PROBE}")
            if r.status_code not in (429, 503):
                assert r.status_code == 404, r.text  # Ollama's answer: the probe was admitted
                assert r.headers["x-seriatim-gate"] == "acquired"
                assert client.get(f"{BASE_URL}/api/version").status_code == 200
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
    body: bytes | None  # None while the body is still arriving: the headers alone match an id

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
                # Loopback loses nothing, and tcpdump writes each packet in order, so a gap
                # means the capture lost bytes: fail, rather than drop requests silently.
                assert rel <= len(stream), (
                    f"{PCAP}: a gap of {rel - len(stream)} bytes in a connection to Ollama"
                )
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
        ts = starts[bisect.bisect_right(offsets, i) - 1][1]
        if "content-length" in headers:
            n = int(headers["content-length"][0])
            body, after = (stream[j : j + n], j + n) if j + n <= len(stream) else (None, None)
        elif b"chunked" in b",".join(headers.get("transfer-encoding", [])).lower():
            body, after = _dechunk(stream, j)
        else:
            body, after = b"", j
        yield Captured(ts, method, target, headers, body)
        if after is None:
            return  # the rest of this connection is still arriving
        i = after


def _dechunk(stream: bytes, j: int) -> tuple[bytes | None, int | None]:
    """A chunked body and where it ends, or (None, None) while it is still arriving. Every
    chunk's CRLF and the trailer section's closing empty line are checked (RFC 9112 §7.1)."""
    body = bytearray()
    while True:
        eol = stream.find(b"\r\n", j)
        if eol < 0:
            return None, None
        size_field = stream[j:eol].split(b";")[0].strip()
        assert size_field and all(c in b"0123456789abcdefABCDEF" for c in size_field), (
            f"a malformed chunk size {stream[j:eol]!r}"
        )
        size = int(size_field, 16)
        j = eol + 2
        if size == 0:
            while True:  # trailer fields, then the empty line
                eol = stream.find(b"\r\n", j)
                if eol < 0:
                    return None, None
                if eol == j:
                    return bytes(body), j + 2
                assert b":" in stream[j:eol], f"a malformed trailer {stream[j:eol]!r}"
                j = eol + 2
        if j + size + 2 > len(stream):
            return None, None
        assert stream[j + size : j + size + 2] == b"\r\n", "a chunk without its CRLF"
        body += stream[j : j + size]
        j += size + 2


def captured() -> list[Captured]:
    """Every request Ollama has received since the PPE started, oldest first."""
    found = [r for s, starts in _streams(PCAP.read_bytes()) for r in _requests(s, starts)]
    return sorted(found, key=lambda r: r.ts)


def wait_captured(ids: set[str], *, bodies: bool = False) -> dict[str, list[Captured]]:
    """The captured requests carrying each id, once every id has reached the capture file
    (tcpdump writes each packet as it arrives, `-U`, but the file is read from outside). Ids
    match on the headers alone; with `bodies`, each one's body must have arrived too."""

    def check() -> dict[str, list[Captured]] | None:
        by_id: dict[str, list[Captured]] = {}
        for r in captured():
            if r.request_id in ids:
                by_id.setdefault(r.request_id, []).append(r)
        if set(by_id) != ids:
            return None
        if bodies and any(r.body is None for rs in by_id.values() for r in rs):
            return None
        return by_id

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


P5_ROUNDS = 20  # with two models loaded, each round matches with probability 1/2 or more


def canonical(body: bytes) -> object:
    """A JSON body with every list of models sorted by name: what is equal whatever the
    order Ollama lists them in."""
    doc = json.loads(body)
    if isinstance(doc, dict) and isinstance(doc.get("models"), list):
        doc = {**doc, "models": sorted(doc["models"], key=lambda m: m.get("name", ""))}
    return doc


@pytest.mark.td("P5")
def test_p5_listings_through_the_sidecar_equal_plain_ollama():
    def comparable(raw: Raw) -> list[tuple[str, str]]:
        dropped = SERIATIM_HEADERS | {"date"}
        return sorted((k.lower(), v) for k, v in raw.headers if k.lower() not in dropped)

    for path in ("/api/tags", "/api/ps", "/api/version"):
        # Ollama lists the loaded models of /api/ps in no fixed order (a Go map), so two
        # direct answers can differ in order alone. Each round asks Ollama, then the
        # sidecar; the sidecar's bytes must equal Ollama's in some round, and every round
        # must hold the same content.
        for rounds in range(1, P5_ROUNDS + 1):  # noqa: B007 (reported below)
            direct = raw_get(DIRECT_URL, path)
            side = raw_get(BASE_URL, path)
            assert direct.status == side.status == 200, path
            assert comparable(side) == comparable(direct), path
            assert canonical(side.body) == canonical(direct.body), path
            if side.body == direct.body:
                break
        else:
            pytest.fail(f"{path}: the sidecar's bytes never equalled Ollama's in {P5_ROUNDS}")
        mine = {k.lower(): v for k, v in side.headers if k.lower() in SERIATIM_HEADERS}
        assert mine["x-seriatim-gate"] == "ungated" and mine["x-seriatim-node"] == NODE_ID
        assert mine["x-request-id"]
        print(f"P5 {path}: {len(side.body)} bytes, equal (round {rounds})")


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
        [wire] = wait_captured({request_id}, bodies=True)[request_id]
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
    arrived: list[float] = []  # when each complete record reached the client
    first_byte = None
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
            now = time.perf_counter() - t0
            if first_byte is None and chunk:
                first_byte = now
            arrived += [now] * chunk.count(b"\n")
            data += chunk
    total = time.perf_counter() - t0
    records = ndjson(data)
    assert data.endswith(b"\n")
    assert len(records) == len(arrived) > 2
    assert all(rec["done"] is False for rec in records[:-1])
    assert records[-1]["done"] is True
    assert first_byte is not None and first_byte < total, (first_byte, total)
    # Streamed, not buffered: the first record reached the client well before the final one.
    # A sidecar that collected the response would deliver them together.
    gap = arrived[-1] - arrived[0]
    assert gap >= MIN_STREAM_GAP_S, (arrived[0], arrived[-1])
    print(
        f"P3: {len(records)} records, first byte {first_byte * 1000:.0f} ms, first record "
        f"{arrived[0] * 1000:.0f} ms, final record {arrived[-1] * 1000:.0f} ms "
        f"of {total * 1000:.0f} ms"
    )


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
        t_hold = time.monotonic()
        # The hold lasts body_timeout_s (raised for the PPE, ppe/compose.yaml). Everything below
        # must finish well inside it, or the held request ends in 408 and the check means
        # nothing, so a slow registry fails here, saying so.
        budget = BODY_TIMEOUT_S - 30

        def within_the_hold(step: str) -> None:
            spent = time.monotonic() - t_hold
            if spent > budget:
                pytest.fail(
                    f"P4: {step} took the hold to {spent:.0f} s, past its {budget:.0f} s budget "
                    f"(body_timeout_s {BODY_TIMEOUT_S:g} s): the registry was too slow to show "
                    "anything about the sidecar"
                )

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
            "POST",
            f"{BASE_URL}/api/pull",
            json={"model": EMBED_MODEL},
            timeout=httpx.Timeout(budget, connect=5.0),
        ) as r:
            assert r.status_code == 200 and r.headers["x-seriatim-gate"] == "ungated"
            assert r.headers["content-type"].startswith("application/x-ndjson")
            for chunk in r.iter_raw():
                progress += chunk
                within_the_hold("the pull")
        records = ndjson(progress)
        assert not [rec for rec in records if "error" in rec], records
        assert records[-1] == {"status": "success"}, records[-3:]
        assert EMBED_MODEL in tags()
        within_the_hold("the pull and its checks")
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
    """Seconds from sending a streamed generation to its first complete NDJSON record, so a
    flush of the headers or of a partial line can't count as the first token."""
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
        for line in r.iter_lines():
            if line:
                took = time.perf_counter() - t
                assert "response" in json.loads(line), line
                return took
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
        f"P6 cancellation latency (CPU evidence only): idle first record {baseline * 1000:.0f} "
        f"ms, after a disconnect {after * 1000:.0f} ms, extra {extra * 1000:.0f} ms "
        f"(limit drain_s / 2 = {DRAIN_S / 2:g} s)"
    )
    assert extra <= DRAIN_S / 2


@dataclass
class Answer:
    status: int
    headers: httpx.Headers
    at: float  # time.monotonic() when the response headers arrived
    body: bytes


def gated_answer() -> Answer | None:
    """A gated request, timed at its response headers (not after a generation), or None when
    nothing answered: while node-a is down, the host's port forwarder accepts and resets."""
    try:
        # Streamed, so Ollama sends the headers with the first token, not after the answer.
        with httpx.stream(
            "POST", f"{BASE_URL}/api/chat", json=chat("Say hi.", 4, stream=True), timeout=60
        ) as r:
            at = time.monotonic()
            return Answer(r.status_code, r.headers, at, r.read())
    except httpx.TransportError:
        return None


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
        assert records[0]["done"] is False  # so the kill lands mid-generation
        compose("kill", "node-a")
        with pytest.raises(httpx.TransportError):  # the stream ends without its terminator
            for line in lines:
                if line:
                    records.append(json.loads(line))
    assert not any(rec.get("done") for rec in records)
    # The new process, and its gate, start after this, so its quarantine ends no sooner
    # than DRAIN_S from here.
    t_start = time.monotonic()
    compose("start", "node-a")

    first = wait_for("node-a to accept again", gated_answer, timeout=WAIT_S)
    assert first.status == 503, first.body
    assert json.loads(first.body)["error_code"] == "NODE_STARTING"
    assert first.headers["x-seriatim-gate"] == "refused"
    assert 1 <= int(first.headers["retry-after"]) <= math.ceil(DRAIN_S)
    while True:
        answer = gated_answer()
        assert answer is not None
        if answer.status != 503:
            break
        assert json.loads(answer.body)["error_code"] == "NODE_STARTING"
        assert answer.at - t_start < DRAIN_S + STARTUP_ALLOWANCE_S + WAIT_S
        time.sleep(POLL_S)
    assert answer.status == 200, answer.body
    assert answer.headers["x-seriatim-gate"] == "acquired"
    admitted = answer.at - t_start
    # A full drain_s of quarantine, measured from the restart, not from the server's own
    # Retry-After. The upper bound allows for the container and the interpreter to start.
    assert admitted >= DRAIN_S, (admitted, DRAIN_S)
    assert admitted <= DRAIN_S + STARTUP_ALLOWANCE_S + 2 * POLL_S, (admitted, DRAIN_S)
    # The bound above counts the startup toward drain_s, so a quarantine short by the startup
    # time would pass it. The first NODE_STARTING answer came at most QUARANTINE_MARGIN_S after
    # the gate was made (the gate exists before the port is bound, and the port is polled
    # every POLL_S), so from that answer at least drain_s minus the margin must remain.
    quarantined = answer.at - first.at
    assert quarantined >= DRAIN_S - QUARANTINE_MARGIN_S, (quarantined, DRAIN_S)
    summary(
        f"P7: after the restart, first answer NODE_STARTING at {first.at - t_start:.1f} s, "
        f"first 200 (headers) at {admitted:.1f} s, {quarantined:.1f} s after the first "
        f"NODE_STARTING (drain_s {DRAIN_S:g} s, margin {QUARANTINE_MARGIN_S:g} s, startup "
        f"allowance {STARTUP_ALLOWANCE_S:g} s)"
    )
