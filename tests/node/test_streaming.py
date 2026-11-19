"""Streaming tests (TD-slice-1 §8.4).

S1-6: passthrough without buffering, captured stream replays, and client disconnects during a
stream and while waiting for the headers (S1-S5). S1-9: errors after the headers, the
in-stream record and its framing, the bodyless rule, and upstream faults (S6-S10, S17-S21,
F1, F3, F5). The tests run the sidecar and a fake Ollama on real loopback sockets
(`harness.py`), except where a chunk boundary or a race must be exact: those call the proxy
directly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import anyio
import httpx
import jsonschema
import pytest
import yaml
from harness import (
    TIMING_BOUND_S,
    WAIT_S,
    Hold,
    all_free,
    exchange,
    exchange_body,
    make_settings,
    raw_connect,
    ready_gate,
    replay,
    reset_after,
    sequence,
    status,
    wait_closed,
    wait_for,
)

import seriatim_node.proxy as proxy_module
from seriatim_node.app import ErrorFactory, RequestLog
from seriatim_node.gate import Lease, classify
from seriatim_node.proxy import Proxy

NDJSON = [(b"content-type", b"application/x-ndjson")]
LINE_1 = b'{"message":{"content":"Hel"},"done":false}\n'
LINE_2 = b'{"message":{"content":"lo"},"done":true}\n'
DISCONNECT_BOUND_S = 1.0  # TD §8.4 S4 and S5: upstream sees the disconnect within 1 s


def _slot_states(gate) -> list[str]:
    return sorted(s.state.value for s in gate.slots())


def _request(path: bytes, body: bytes) -> bytes:
    head = b"POST " + path + b" HTTP/1.1\r\nHost: x\r\nContent-Length: "
    return head + str(len(body)).encode() + b"\r\n\r\n" + body


def _chat_lines(capsys, n: int) -> list[dict]:
    """Wait for `n` log lines for /api/chat. A line is written after the client is answered,
    so it can follow the response by a moment."""
    out: list[str] = []

    def lines() -> list[dict]:
        text = "".join(out)
        docs = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
        return [d for d in docs if d.get("path") == "/api/chat"]

    wait_for(lambda: out.append(capsys.readouterr().out) or len(lines()) >= n)
    return lines()


@pytest.mark.td("S1")
def test_s1_the_first_chunk_arrives_before_upstream_continues(fake_ollama, sidecar):
    hold = Hold(headers=NDJSON, first=LINE_1, rest=LINE_2)
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    with httpx.stream("POST", car.url + "/api/chat", content=b"{}", timeout=WAIT_S) as resp:
        chunks = resp.iter_raw()
        assert next(chunks) == LINE_1  # no buffering: upstream is still holding
        assert hold.finished == set()
        hold.release()
        rest = b"".join(chunks)
    assert rest == LINE_2
    assert resp.headers["content-type"] == "application/x-ndjson"
    wait_for(lambda: all_free(gate))


@pytest.mark.td("S2")
def test_s2_ndjson_chat_stream_replayed(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", replay("chat.stream"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    body = exchange("chat.stream")["request"]["body"].encode()
    resp = httpx.post(car.url + "/api/chat", content=body, timeout=WAIT_S)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-ndjson"
    assert resp.content == exchange_body("chat.stream")
    assert fake_ollama.requests[0].body == body


@pytest.mark.td("S3")
def test_s3_sse_chat_completions_replayed(fake_ollama, sidecar):
    fake_ollama.script("POST", "/v1/chat/completions", replay("v1.chat.stream"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    body = exchange("v1.chat.stream")["request"]["body"].encode()
    resp = httpx.post(car.url + "/v1/chat/completions", content=body, timeout=WAIT_S)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/event-stream"
    assert resp.content == exchange_body("v1.chat.stream")
    assert resp.content.endswith(b"data: [DONE]\n\n")


@pytest.mark.td("S4")
def test_s4_client_closes_mid_stream(fake_ollama, sidecar, capsys):
    hold = Hold(headers=NDJSON, first=LINE_1)
    fake_ollama.script("POST", "/api/chat", sequence(hold, status(200, b"{}")))
    gate, clock = ready_gate(retry_after_s=1, drain_s=10)
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_request(b"/api/chat", b"{}"))
        received = b""
        while LINE_1 not in received:
            received += sock.recv(65536)
        closed_at = time.monotonic()
    assert hold.wait_disconnected() - closed_at < DISCONNECT_BOUND_S
    wait_for(lambda: _slot_states(gate) == ["draining"])
    (view,) = gate.slots()
    busy = httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S)
    assert busy.status_code == 429
    assert int(busy.headers["retry-after"]) >= view.drain_remaining_s
    clock.advance(10)
    assert httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S).status_code == 200
    lines = _chat_lines(capsys, 3)
    assert [(line["outcome"], line["slot_end"]) for line in lines] == [
        ("client_disconnect", "draining"),
        ("busy", "none"),
        ("ok", "free"),
    ]


@pytest.mark.td("S5")
def test_s5_client_closes_while_waiting_for_headers(fake_ollama, sidecar):
    hold = Hold()  # a non-streaming request: upstream holds before the headers
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_request(b"/api/chat", b'{"stream":false}'))
        hold.wait_entered()
        closed_at = time.monotonic()
    assert hold.wait_disconnected() - closed_at < DISCONNECT_BOUND_S
    wait_for(lambda: _slot_states(gate) == ["draining"])


# --- S1-9: errors after the headers (TD §3.3 step 5, §4, §5) ---------------------------------

SSE = [(b"content-type", b"text/event-stream; charset=utf-8")]
# Resolved from this file, so the same path works under mutmut (tools/ci/mutation.py).
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "openapi.yaml"


@pytest.fixture(scope="module")
def contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def record_validator(contract):
    return jsonschema.Draft202012Validator(contract["components"]["schemas"]["StreamErrorRecord"])


@pytest.fixture(scope="module")
def envelope_validator(contract):
    schema = {
        **contract["components"]["schemas"]["ErrorEnvelope"],
        "components": contract["components"],
    }
    return jsonschema.Draft202012Validator(schema)


def _aborted(car, path: str = "/api/chat") -> tuple[httpx.Response, bytes]:
    """The response's raw bytes. It must end in an abort, never a clean EOF."""
    got = b""
    with httpx.stream("POST", car.url + path, content=b"{}", timeout=WAIT_S) as resp:
        with pytest.raises(httpx.RemoteProtocolError):
            for chunk in resp.iter_raw():
                got += chunk
    return resp, got


def _record(doc_bytes: bytes, code: str, validator) -> None:
    doc = json.loads(doc_bytes)
    validator.validate(doc)
    assert list(doc) == ["error", "error_code"]
    assert doc["error_code"] == code


@pytest.mark.td("S6")
def test_s6_a_reset_after_two_lines_ends_with_a_record(
    fake_ollama, sidecar, record_validator, capsys
):
    fake_ollama.script("POST", "/api/chat", reset_after(NDJSON, LINE_1, LINE_2))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp, got = _aborted(car)
    assert resp.status_code == 200
    assert got.startswith(LINE_1 + LINE_2)
    rest = got[len(LINE_1 + LINE_2) :]
    assert rest.endswith(b"\n") and rest.count(b"\n") == 1  # exactly one more line
    _record(rest, "UPSTREAM_STREAM_BROKEN", record_validator)
    assert json.loads(rest)["error"] == "Ollama's response stream broke"
    wait_for(lambda: _slot_states(gate) == ["draining"])
    (line,) = _chat_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "upstream_error",
        200,
        "draining",
    )


@pytest.mark.td("S7")
def test_s7_the_hold_limit_mid_stream(fake_ollama, sidecar, record_validator, capsys, monkeypatch):
    hold = Hold(headers=NDJSON, first=LINE_1)
    fake_ollama.script("POST", "/api/chat", hold)
    drained_at: list[float] = []
    original = Lease.finish

    def finish(self, *, clean):
        # This blocks the sidecar's event loop, so the fake can see the disconnect now only
        # if the sidecar closed the upstream socket before it ended the lease.
        deadline = time.monotonic() + WAIT_S
        while 0 not in hold.disconnected_at and time.monotonic() < deadline:
            time.sleep(0.002)
        drained_at.append(time.monotonic())
        return original(self, clean=clean)

    monkeypatch.setattr(Lease, "finish", finish)
    gate, _ = ready_gate(max_hold_s=1)
    car = sidecar(fake_ollama.url, gate=gate)
    started = time.perf_counter()
    _, got = _aborted(car)
    assert 1 <= time.perf_counter() - started < 1 + TIMING_BOUND_S
    assert got.startswith(LINE_1)
    _record(got[len(LINE_1) :], "LOCK_HOLD_EXCEEDED", record_validator)
    wait_for(lambda: _slot_states(gate) == ["draining"])
    assert hold.disconnected_at[0] <= drained_at[0]  # closed first, then the drain
    (line,) = _chat_lines(capsys, 1)
    assert (line["level"], line["outcome"], line["slot_end"]) == (
        "CRITICAL",
        "hold_exceeded",
        "draining",
    )


@pytest.mark.td("S8")
def test_s8_the_hold_limit_before_the_headers(fake_ollama, sidecar, envelope_validator):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate(max_hold_s=1)
    car = sidecar(fake_ollama.url, gate=gate)
    started = time.perf_counter()
    resp = httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S)
    assert 1 <= time.perf_counter() - started < 1 + TIMING_BOUND_S
    assert resp.status_code == 504
    doc = json.loads(resp.content)
    envelope_validator.validate(doc)
    assert doc["error_code"] == "LOCK_HOLD_EXCEEDED"
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    hold.wait_disconnected()
    wait_for(lambda: _slot_states(gate) == ["draining"])


# In process: the proxy called directly, with httpx's MockTransport upstream, so the chunk
# boundaries (S19) and the timing of a race (S9) are exact.


class _Chunks(httpx.AsyncByteStream):
    """These chunks, then an optional stall, then a reset."""

    def __init__(self, chunks: list[bytes], stall: bool) -> None:
        self.chunks, self.stall = chunks, stall

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await anyio.sleep(30)
        raise httpx.ReadError("reset")


def _direct(headers, chunks, *, stall, gate, receive, send, monkeypatch) -> None:
    def handler(request):
        return httpx.Response(200, headers=headers, stream=_Chunks(chunks, stall))

    def client(_settings=None):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1"
        )

    monkeypatch.setattr(proxy_module, "new_gated_client", client)
    settings = make_settings("http://127.0.0.1:1", close_timeout_s=0.2)
    proxy = Proxy(settings, gate, client(), RequestLog("INFO", "node-a"), ErrorFactory("node-a"))
    scope = {
        "method": "POST",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "headers": [(b"content-length", b"2")],
    }

    async def main():
        with anyio.move_on_after(WAIT_S):
            await proxy(scope, receive, send, classify("POST", b"/api/chat"), "rid")

    anyio.run(main)


def _body_then_disconnect(delay_s: float, gone: dict):
    """A `receive` that gives the body, then a disconnect after `delay_s`."""
    body = [{"type": "http.request", "body": b"{}", "more_body": False}]

    async def receive():
        if body:
            return body.pop()
        await anyio.sleep(delay_s)
        gone["recorded"] = True  # the watcher records it before its next await
        return {"type": "http.disconnect"}

    return receive


S9_HOLD_S = 0.4
S9_MARGIN_S = 0.15  # far from the race, so each order is fixed even on a slow runner


@pytest.mark.td("S9")
@pytest.mark.parametrize(
    ("offset_s", "write_fails", "expected"),
    [
        (-S9_MARGIN_S, False, "client_disconnect"),  # the disconnect is clearly first
        (S9_MARGIN_S, False, "hold_exceeded"),  # the deadline is clearly first
        (S9_MARGIN_S, True, "hold_exceeded"),  # ... and the client is gone at its write
    ],
    ids=["disconnect-first", "deadline-first", "deadline-first-write-fails"],
)
def test_s9_a_disconnect_and_the_hold_deadline(
    offset_s, write_fails, expected, monkeypatch, capsys
):
    gate, _ = ready_gate(max_hold_s=S9_HOLD_S)
    ends: list[bool] = []
    original = Lease.finish

    def finish(self, *, clean):
        ends.append(original(self, clean=clean))
        return ends[-1]

    monkeypatch.setattr(Lease, "finish", finish)
    gone = {"recorded": False}
    after_disconnect: list[dict] = []
    after_line: list[dict] = []  # every send attempted after Ollama's line

    async def send(message):
        if gone["recorded"]:
            after_disconnect.append(message)
        if message.get("body") == LINE_1:
            return
        if message["type"] == "http.response.body":
            after_line.append(message)
            if write_fails:
                raise OSError("the client is gone")

    receive = _body_then_disconnect(S9_HOLD_S + offset_s, gone)
    _direct(
        NDJSON, [LINE_1], stall=True, gate=gate, receive=receive, send=send, monkeypatch=monkeypatch
    )
    (line,) = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert (line["outcome"], line["slot_end"]) == (expected, "draining")  # one line, one outcome
    assert ends == [True]  # the slot drains exactly once
    assert _slot_states(gate) == ["draining"]
    if expected == "client_disconnect":
        assert gone["recorded"]
        assert after_disconnect == [] and after_line == []  # nothing once it is recorded
    else:
        assert not gone["recorded"]  # the deadline cancelled the watcher first
        (record,) = after_line  # exactly one write attempted, and nothing after it
        assert record["more_body"] is True
        assert json.loads(record["body"])["error_code"] == "LOCK_HOLD_EXCEEDED"


@pytest.mark.td("S10")
@pytest.mark.parametrize(
    ("headers", "chunks"),
    [
        ([*NDJSON, (b"content-length", b"100")], [LINE_1]),
        ([*NDJSON, (b"content-encoding", b"gzip")], [LINE_1]),
        ([(b"content-type", b"application/json")], [LINE_1]),
        (NDJSON, [LINE_1, b'{"mess']),
        (SSE, [b"data: a\n\n", b"data: b\n"]),
    ],
    ids=["content-length", "gzip", "json", "ndjson-mid-line", "sse-mid-event"],
)
def test_s10_no_record_when_the_framing_forbids_one(headers, chunks, fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", reset_after(headers, *chunks))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    _, got = _aborted(car)
    assert got == b"".join(chunks)  # what Ollama sent, and nothing else
    wait_for(lambda: _slot_states(gate) == ["draining"])


@pytest.mark.td("S17")
def test_s17_an_sse_record(fake_ollama, sidecar, record_validator):
    events = [b'data: {"a":1}\n\n', b'data: {"b":2}\n\n']
    fake_ollama.script("POST", "/v1/chat/completions", reset_after(SSE, *events))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp, got = _aborted(car, path="/v1/chat/completions")
    assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert got.startswith(b"".join(events))
    rest = got[len(b"".join(events)) :]
    assert rest.startswith(b"data: ") and rest.endswith(b"\n\n") and rest.count(b"\n") == 2
    _record(rest[len(b"data: ") :], "UPSTREAM_STREAM_BROKEN", record_validator)


@pytest.mark.td("S18")
def test_s18_a_reset_before_any_body(fake_ollama, sidecar, record_validator):
    fake_ollama.script("POST", "/api/chat", reset_after(NDJSON))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp, got = _aborted(car)
    assert (resp.status_code, resp.headers["content-type"]) == (200, "application/x-ndjson")
    assert got.endswith(b"\n") and got.count(b"\n") == 1
    _record(got, "UPSTREAM_STREAM_BROKEN", record_validator)
    wait_for(lambda: _slot_states(gate) == ["draining"])


@pytest.mark.td("S19")
@pytest.mark.parametrize(
    ("headers", "chunks", "framing"),
    [
        (NDJSON, [b'{"a"', b":1}", b"\n"], "ndjson"),
        (SSE, [b"data: x\n", b"\n"], "sse"),
        (SSE, [bytes([b]) for b in b"data: x\n\n"], "sse"),
        (SSE, [bytes([b]) for b in b"data: x\n"], None),  # the tail is the stream's, not ""
        (NDJSON, [b'{"a":1}', b'\n{"b"'], None),
    ],
    ids=[
        "ndjson-split",
        "sse-split",
        "sse-one-byte-chunks",
        "sse-one-byte-mid-event",
        "ndjson-mid-line",
    ],
)
def test_s19_record_boundaries_split_across_chunks(
    headers, chunks, framing, record_validator, monkeypatch
):
    gate, _ = ready_gate()
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    receive = _body_then_disconnect(30, {})
    _direct(
        headers, chunks, stall=False, gate=gate, receive=receive, send=send, monkeypatch=monkeypatch
    )
    assert sent[0]["type"] == "http.response.start"
    bodies = sent[1:]
    assert all(m["more_body"] is True for m in bodies)  # never a clean end
    assert [m["body"] for m in bodies[: len(chunks)]] == chunks
    records = [m["body"] for m in bodies[len(chunks) :]]
    if framing is None:
        assert records == []
    else:
        (record,) = records
        if framing == "sse":
            assert record.startswith(b"data: ") and record.endswith(b"\n\n")
            record = record[len(b"data: ") :]
        _record(record, "UPSTREAM_STREAM_BROKEN", record_validator)
    assert _slot_states(gate) == ["draining"]


def _chunked_head() -> bytes:
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
    )


def _malformed_chunk_size(fake, conn):
    fake.read(conn)
    conn.sendall(_chunked_head() + b"5\r\nhello\r\nzz\r\n")
    wait_closed(conn)


def _no_terminating_chunk(fake, conn):
    fake.read(conn)
    conn.sendall(_chunked_head() + b"5\r\nhello\r\n")  # then the harness closes


@pytest.mark.td("S20")
@pytest.mark.parametrize(
    "handler", [_malformed_chunk_size, _no_terminating_chunk], ids=["chunk-size", "no-end"]
)
def test_s20_broken_chunked_framing(handler, raw_upstream, sidecar, capsys):
    upstream = raw_upstream(handler)
    gate, _ = ready_gate()
    car = sidecar(upstream.url, gate=gate)
    resp, _ = _aborted(car)
    assert resp.status_code == 200
    wait_for(lambda: _slot_states(gate) == ["draining"])
    (line,) = _chat_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("upstream_error", "draining")


def _bodyless(status_line: bytes):
    def handler(fake, conn):
        fake.read(conn)
        conn.sendall(status_line + b"\r\n\r\n")  # no length, not chunked, and kept open
        wait_closed(conn)

    return handler


@pytest.mark.td("S21")
@pytest.mark.parametrize("status_line", [b"HTTP/1.1 204 No Content", b"HTTP/1.1 304 Not Modified"])
def test_s21_a_bodyless_status_is_complete(status_line, raw_upstream, sidecar, capsys):
    upstream = raw_upstream(_bodyless(status_line))
    gate, _ = ready_gate()
    car = sidecar(upstream.url, gate=gate)
    resp = httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S)
    assert (resp.status_code, resp.content) == (int(status_line.split()[1]), b"")
    (line,) = _chat_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("ok", "free")
    assert _slot_states(gate) == ["free"]  # no drain: the fake clock never moved


@pytest.mark.td("S21")
def test_s21_a_head_response_with_a_length_is_complete(fake_ollama, sidecar, capsys):
    async def head(send, fake, receive=None):
        start = {"type": "http.response.start", "status": 200}
        await send({**start, "headers": [(b"content-length", b"1234")]})
        await send({"type": "http.response.body", "body": b""})

    fake_ollama.script("HEAD", "/", head)
    car = sidecar(fake_ollama.url, gate=ready_gate()[0])
    resp = httpx.head(car.url + "/", timeout=WAIT_S)
    assert (resp.status_code, resp.headers["content-length"], resp.content) == (200, "1234", b"")
    out: list[str] = []

    def lines() -> list[dict]:
        docs = [json.loads(x) for x in "".join(out).splitlines() if x.startswith("{")]
        return [d for d in docs if d.get("method") == "HEAD"]

    wait_for(lambda: out.append(capsys.readouterr().out) or lines())
    (line,) = lines()
    assert (line["outcome"], line["kind"], line["status"]) == ("ok", "ungated", 200)


# --- S1-9: upstream faults before the headers (TD §8.4 F1, F3, F5) ---------------------------


def _close_at_once(fake, conn):
    pass  # the harness closes the accepted socket


def _malformed_status_line(fake, conn):
    fake.read(conn)
    conn.sendall(b"NOT-HTTP\r\n\r\n")
    wait_closed(conn)


def _close_after_the_body(fake, conn):
    fake.read(conn)  # then the harness closes: Ollama crashed before its headers


def _upstream_fault(handler, raw_upstream, sidecar, envelope_validator, capsys) -> None:
    upstream = raw_upstream(handler)
    gate, _ = ready_gate()
    car = sidecar(upstream.url, gate=gate)
    resp = httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S)
    assert resp.status_code == 502
    doc = json.loads(resp.content)
    envelope_validator.validate(doc)
    assert (doc["error_code"], doc["replay_safe"]) == ("UPSTREAM_PROTOCOL_ERROR", False)
    assert upstream.accepted == 1
    wait_for(lambda: _slot_states(gate) == ["draining"])  # bytes may have reached Ollama
    (line,) = _chat_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "upstream_error",
        502,
        "draining",
    )


@pytest.mark.td("F1")
def test_f1_a_fault_at_connect(raw_upstream, sidecar, envelope_validator, capsys):
    _upstream_fault(_close_at_once, raw_upstream, sidecar, envelope_validator, capsys)


@pytest.mark.td("F3")
def test_f3_a_malformed_status_line(raw_upstream, sidecar, envelope_validator, capsys):
    _upstream_fault(_malformed_status_line, raw_upstream, sidecar, envelope_validator, capsys)


@pytest.mark.td("F5")
def test_f5_ollama_crashes_before_the_headers(raw_upstream, sidecar, envelope_validator, capsys):
    upstream_requests: list[bytes] = []

    def handler(fake, conn):
        _close_after_the_body(fake, conn)
        upstream_requests.extend(fake.requests)

    _upstream_fault(handler, raw_upstream, sidecar, envelope_validator, capsys)
    assert upstream_requests[0].endswith(b"\r\n\r\n{}")  # the body was read in full first
