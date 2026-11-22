"""Streaming tests (TD-slice-1 §8.4).

S1-6: passthrough without buffering, captured stream replays, and client disconnects during a
stream and while waiting for the headers (S1-S5). S1-9: errors after the headers, the
in-stream record and its framing, the bodyless rule, and upstream faults (S6-S10, S17-S21,
F1, F3, F5). S1-10: external cancellation in each phase, a second cancellation during the
close, a request while the close is pending, a slow reader, a final write that fails, a
disconnect as upstream completes, a blocked upstream write and an unproven close (S11-S16,
F2, F4). The tests run the sidecar and a fake Ollama on real loopback sockets
(`harness.py`), except where a chunk boundary or a race must be exact: those call the proxy
directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import socket
import sys
import threading
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
    refused_post,
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
    busy = refused_post(car.url + "/api/chat")
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


# --- S1-10: cancellation and the close proof (TD §3.3, §5) -----------------------------------
# A server shutting down cancels the request's task with asyncio's `Task.cancel()`, so these
# tests do exactly that, on a task that runs the proxy directly. Where the upstream must see
# the close (S11), it is a real fake on a loopback socket; where a step's timing must be exact
# (S12, S13, S15, S16), it is httpx's MockTransport.

CHAT_ROUTE = classify("POST", b"/api/chat")
BIG = 32 * 1024 * 1024  # S11, S14, F2: far more than every socket buffer on the path holds
# Whether a cancelled upstream write leaves bytes unsent. On POSIX, anyio's close aborts the
# transport and drops what it had not yet written, so the fake gets less than the body. On
# Windows, asyncio's proactor hands the whole write to the OS as one overlapped send, which
# the OS completes on its own once the fake reads, even after the cancel and the close. There
# the fake reads the whole body and then EOF; every other clause is checked the same way.
WRITE_IS_CUT_SHORT = sys.platform != "win32"


def _chat_scope(body_len: int = 2) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "headers": [(b"content-length", str(body_len).encode())],
    }


def _body_once(body: bytes = b"{}"):
    """A `receive` that gives the whole body, then waits (no disconnect comes)."""
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop()
        await anyio.sleep_forever()

    return receive


async def _until(predicate) -> None:
    """Poll inside the event loop; fail (never pass) after WAIT_S."""
    with anyio.fail_after(WAIT_S):
        while not predicate():
            await anyio.sleep(0.002)


class _Body(httpx.AsyncByteStream):
    """An upstream body: these chunks, then `then` raised, or the end (`at_end` runs first)."""

    def __init__(self, chunks: list[bytes], *, at_end=None, then=None) -> None:
        self.chunks, self.at_end, self.then = chunks, at_end, then

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.then is not None:
            raise self.then
        if self.at_end is not None:
            self.at_end()


def _framed(at_end=None) -> httpx.Response:
    body = LINE_1 + LINE_2
    headers = [*NDJSON, (b"content-length", str(len(body)).encode())]
    return httpx.Response(200, headers=headers, stream=_Body([LINE_1, LINE_2], at_end=at_end))


def _clean(request) -> httpx.Response:
    return _framed()


def _broken(request) -> httpx.Response:
    reset = httpx.ReadError("reset")
    return httpx.Response(200, headers=NDJSON, stream=_Body([LINE_1], then=reset))


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1")


class _SlowClose(httpx.AsyncClient):
    """A gated client whose `aclose()` takes `delay_s`, and notes the slots as it returns."""

    def __init__(self, handler, delay_s: float, gate) -> None:
        super().__init__(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1")
        self.delay_s, self.gate = delay_s, gate
        self.close_started = self.close_done = False
        self.slots_at_return: list[str] = []

    async def aclose(self) -> None:
        self.close_started = True
        await anyio.sleep(self.delay_s)
        await super().aclose()
        self.slots_at_return = _slot_states(self.gate)
        self.close_done = True


def _record_ends(monkeypatch) -> list[tuple[str, bool]]:
    """Every `finish` and `stuck` call, with what it returned (True: the effective end)."""
    ends: list[tuple[str, bool]] = []
    finish, stuck = Lease.finish, Lease.stuck

    def finish_(self, *, clean):
        ends.append(("finish", finish(self, clean=clean)))
        return ends[-1][1]

    def stuck_(self):
        ends.append(("stuck", stuck(self)))
        return ends[-1][1]

    monkeypatch.setattr(Lease, "finish", finish_)
    monkeypatch.setattr(Lease, "stuck", stuck_)
    return ends


def _proxy(gate, monkeypatch, gated, ollama_url: str = "http://127.0.0.1:1", **overrides) -> Proxy:
    monkeypatch.setattr(proxy_module, "new_gated_client", gated)
    settings = make_settings(ollama_url, **overrides)
    shared = httpx.AsyncClient(base_url=ollama_url)
    log = RequestLog(settings.log_level, "node-a")
    return Proxy(settings, gate, shared, log, ErrorFactory("node-a"))


def _log(capsys) -> list[dict]:
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]


class _StopsReading:
    """A raw upstream that reads the request's headers, then stops reading until `go` is
    set; it then reads to EOF and counts the bytes it got (S11's write phase, F2)."""

    def __init__(self) -> None:
        self.head = threading.Event()
        self.go = threading.Event()
        self.eof = threading.Event()
        self.read = 0

    def __call__(self, fake, conn) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:  # the headers, and on purpose not the body
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        self.head.set()
        self.go.wait(WAIT_S)
        try:
            while chunk := conn.recv(65536):
                self.read += len(chunk)
        except OSError:
            pass  # a reset is the sidecar closing its socket too
        self.eof.set()

    def seen_close(self) -> bool:
        self.go.set()
        return self.eof.wait(WAIT_S)


def _cancel_when(proxy, scope, receive, ready) -> tuple[asyncio.Task, list[dict], list[dict]]:
    """Run the proxy as a task, cancel it with `Task.cancel()` once `ready(sent)` holds, and
    wait for it. Returns the task and the messages sent before and after the cancel."""
    before: list[dict] = []
    after: list[dict] = []
    cancelled = threading.Event()

    async def send(message):
        (after if cancelled.is_set() else before).append(message)

    async def main() -> asyncio.Task:
        task = asyncio.ensure_future(proxy(scope, receive, send, CHAT_ROUTE, "rid"))
        await _until(lambda: ready(before))
        cancelled.set()
        task.cancel()
        with anyio.fail_after(WAIT_S), pytest.raises(asyncio.CancelledError):
            await task
        return task

    return asyncio.run(main()), before, after


# Each phase gives (upstream URL, scope, receive, ready(sent), the upstream's close check).


def _s11_body_read(fake_ollama, raw_upstream):
    calls: list[int] = []

    async def receive():
        calls.append(1)
        if len(calls) == 1:
            return {"type": "http.request", "body": b"{", "more_body": True}
        await anyio.sleep_forever()

    return fake_ollama.url, _chat_scope(), receive, lambda sent: len(calls) >= 2, None


def _s11_upstream_write(fake_ollama, raw_upstream):
    upstream = _StopsReading()
    url = raw_upstream(upstream).url
    ready = lambda sent: upstream.head.is_set()  # noqa: E731
    return url, _chat_scope(BIG), _body_once(b"x" * BIG), ready, upstream


def _s11_wait_for_headers(fake_ollama, raw_upstream):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    ready = lambda sent: hold.entered >= 1  # noqa: E731
    return fake_ollama.url, _chat_scope(), _body_once(), ready, hold


def _s11_streaming(fake_ollama, raw_upstream):
    hold = Hold(headers=NDJSON, first=LINE_1)
    fake_ollama.script("POST", "/api/chat", hold)
    ready = lambda sent: any(m.get("body") == LINE_1 for m in sent)  # noqa: E731
    return fake_ollama.url, _chat_scope(), _body_once(), ready, hold


@pytest.mark.td("S11")
@pytest.mark.parametrize(
    ("phase", "slot"),
    [
        (_s11_body_read, "free"),
        (_s11_upstream_write, "draining"),
        (_s11_wait_for_headers, "draining"),
        (_s11_streaming, "draining"),
    ],
    ids=["body-read", "upstream-write", "wait-for-headers", "streaming"],
)
def test_s11_an_external_cancellation_in_each_phase(
    phase, slot, fake_ollama, raw_upstream, monkeypatch, capsys
):
    url, scope, receive, ready, upstream = phase(fake_ollama, raw_upstream)
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    made: list[httpx.AsyncClient] = []
    real = proxy_module.new_gated_client

    def gated(settings):
        made.append(real(settings))
        return made[-1]

    proxy = _proxy(gate, monkeypatch, gated, url)
    task, before, after = _cancel_when(proxy, scope, receive, ready)
    assert task.cancelled()  # the cancellation propagated
    (client,) = made
    assert client.is_closed  # the shielded finally closed the per-request client
    if isinstance(upstream, Hold):
        upstream.wait_disconnected()  # the fake saw the upstream connection close
    elif isinstance(upstream, _StopsReading):
        assert upstream.seen_close()
        assert upstream.read < BIG or not WRITE_IS_CUT_SHORT  # the write never finished
    else:
        assert fake_ollama.requests == []  # nothing was sent upstream
    assert _slot_states(gate) == [slot]  # drained if anything was sent, else freed
    assert ends == [("finish", True)]
    assert after == []  # nothing more for the client: no reply, no record, no final message
    streaming = phase is _s11_streaming
    types = ["http.response.start", "http.response.body"] if streaming else []
    assert [m["type"] for m in before] == types
    (line,) = _log(capsys)
    assert (line["outcome"], line["slot_end"], line["status"]) == (
        "cancelled",
        slot,
        200 if streaming else None,
    )


SLOW_CLOSE_S = 0.5  # S12, S13: the slow aclose(); close_timeout_s is 2, so it is never cut


@pytest.mark.td("S12")
def test_s12_a_second_cancellation_while_the_close_is_pending(monkeypatch, capsys):
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    clients: list[_SlowClose] = []
    entered: list[int] = []

    async def handler(request):
        entered.append(1)
        await anyio.sleep(30)  # waiting for Ollama's headers

    def gated(_settings):
        clients.append(_SlowClose(handler, SLOW_CLOSE_S, gate))
        return clients[-1]

    proxy = _proxy(gate, monkeypatch, gated, close_timeout_s=2)
    seen: dict[str, object] = {}

    async def send(message):
        raise AssertionError("nothing is sent to a cancelled request's client")

    async def main() -> asyncio.Task:
        task = asyncio.ensure_future(proxy(_chat_scope(), _body_once(), send, CHAT_ROUTE, "rid"))
        await _until(lambda: entered)
        task.cancel()  # the first: the shielded finally starts step (a)
        await _until(lambda: clients[0].close_started)
        seen["at_second"] = (clients[0].close_done, _slot_states(gate))
        task.cancel()  # the second, while aclose() is still running
        with anyio.fail_after(WAIT_S), pytest.raises(asyncio.CancelledError):
            await task
        return task

    started = time.perf_counter()
    task = asyncio.run(main())
    assert time.perf_counter() - started >= SLOW_CLOSE_S
    assert task.cancelled()
    assert seen == {"at_second": (False, ["held"])}
    (client,) = clients
    assert client.close_done  # aclose() ran to its end despite the second cancellation
    assert client.slots_at_return == ["held"]  # the slot was not freed before it returned
    assert _slot_states(gate) == ["draining"]  # then (b): the request had reached upstream
    assert ends == [("finish", True)]  # exactly one end
    (line,) = _log(capsys)
    assert (line["outcome"], line["slot_end"]) == ("cancelled", "draining")
    assert "upstream_close_failed" not in line


@pytest.mark.td("S12")
def test_s12_a_cancellation_that_first_arrives_during_the_close(monkeypatch, capsys):
    """The `finally` was entered by a clean end, so no cancellation is in flight: the one
    absorbed while aclose() runs must still be raised once the cleanup has ended."""
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    clients: list[_SlowClose] = []

    def gated(_settings):
        clients.append(_SlowClose(_clean, SLOW_CLOSE_S, gate))
        return clients[-1]

    proxy = _proxy(gate, monkeypatch, gated, close_timeout_s=2)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def main() -> asyncio.Task:
        task = asyncio.ensure_future(proxy(_chat_scope(), _body_once(), send, CHAT_ROUTE, "rid"))
        await _until(lambda: clients and clients[0].close_started)
        task.cancel()  # the only cancellation, inside step (a)
        with anyio.fail_after(WAIT_S), pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(main())
    assert task.cancelled()  # raised again after the cleanup
    (client,) = clients
    assert client.close_done and client.slots_at_return == ["held"]
    assert _slot_states(gate) == ["free"] and ends == [("finish", True)]  # a clean end
    assert [m.get("more_body") for m in sent] == [None, True, True, False]  # (c) still ran
    (line,) = _log(capsys)
    assert (line["outcome"], line["slot_end"]) == ("ok", "free")


@pytest.mark.td("S13")
@pytest.mark.parametrize(
    ("handler", "slot"), [(_clean, "free"), (_broken, "draining")], ids=["clean", "broken"]
)
def test_s13_another_request_while_the_close_is_pending(handler, slot, monkeypatch, capsys):
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    clients: list[_SlowClose] = []

    def gated(_settings):
        clients.append(_SlowClose(handler, SLOW_CLOSE_S, gate))
        return clients[-1]

    proxy = _proxy(gate, monkeypatch, gated, close_timeout_s=2)
    second: list[dict] = []
    seen: dict[str, object] = {}

    async def ignore(message):
        pass

    async def send_second(message):
        second.append(message)

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(proxy, _chat_scope(), _body_once(), ignore, CHAT_ROUTE, "first")
            await _until(lambda: clients and clients[0].close_started)
            await proxy(_chat_scope(), _body_once(), send_second, CHAT_ROUTE, "second")
            seen["after_second"] = (clients[0].close_done, _slot_states(gate))

    anyio.run(main)
    assert seen == {"after_second": (False, ["held"])}  # answered while the close was pending
    assert second[0]["status"] == 429
    assert json.loads(second[1]["body"])["error_code"] == "GPU_BUSY"
    (client,) = clients  # the 429 took no slot and built no client
    assert client.slots_at_return == ["held"]
    assert _slot_states(gate) == [slot]  # then §3.3 (b): a clean end frees, a broken one drains
    assert ends == [("finish", True)]
    lines = {line["request_id"]: line for line in _log(capsys)}
    assert (lines["second"]["outcome"], lines["second"]["slot_end"]) == ("busy", "none")
    assert lines["first"]["slot_end"] == slot


@pytest.mark.td("S14")
def test_s14_a_slow_reader_until_the_hold_deadline(raw_upstream, sidecar, capsys):
    written: dict[str, float | None] = {"bytes": 0, "failed_at": None}

    def streams_32_mib(fake, conn):
        fake.read(conn)
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
        conn.sendall(head + b"Content-Length: " + str(BIG).encode() + b"\r\n\r\n")
        chunk = b"x" * 65536
        try:
            while written["bytes"] < BIG:
                conn.sendall(chunk)  # blocks once every buffer on the path is full
                written["bytes"] += len(chunk)
        except OSError:
            written["failed_at"] = time.perf_counter()  # the sidecar closed the connection

    upstream = raw_upstream(streams_32_mib)
    gate, _ = ready_gate(max_hold_s=1)
    car = sidecar(upstream.url, gate=gate)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)  # and it reads nothing
        sock.settimeout(WAIT_S)
        started = time.perf_counter()
        sock.connect(("127.0.0.1", car.port))
        sock.sendall(_request(b"/api/chat", b"{}"))
        wait_for(lambda: written["failed_at"] is not None)
        wait_for(lambda: _slot_states(gate) == ["draining"])
    assert written["bytes"] < BIG  # the fake's writes blocked: the upstream reads paused
    assert 1 <= written["failed_at"] - started < 1 + TIMING_BOUND_S
    (line,) = _chat_lines(capsys, 1)
    assert (line["level"], line["outcome"], line["slot_end"]) == (
        "CRITICAL",
        "hold_exceeded",
        "draining",
    )


FINAL_CLOSE_S = 0.5  # S15's close_timeout_s


@pytest.mark.td("S15")
@pytest.mark.parametrize("final", ["hang", "raise"])
def test_s15_the_final_write_hangs_or_raises(final, monkeypatch, capsys):
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    gated = lambda _s: _mock_client(_clean)  # noqa: E731
    proxy = _proxy(gate, monkeypatch, gated, close_timeout_s=FINAL_CLOSE_S, log_level="DEBUG")
    at_final: list[list[str]] = []

    async def send(message):
        if message.get("more_body") is False:  # the final, empty body message
            at_final.append(_slot_states(gate))
            if final == "hang":
                await anyio.sleep(30)
            raise OSError("the client is gone")

    started = time.perf_counter()
    anyio.run(proxy, _chat_scope(), _body_once(), send, CHAT_ROUTE, "rid")
    elapsed = time.perf_counter() - started
    assert at_final == [["free"]]  # the slot was already free when the write began
    assert _slot_states(gate) == ["free"] and ends == [("finish", True)]  # and it stays free
    if final == "hang":
        assert FINAL_CLOSE_S <= elapsed < FINAL_CLOSE_S + TIMING_BOUND_S  # abandoned on time
    else:
        assert elapsed < TIMING_BOUND_S
    (line,) = _log(capsys)  # one line per request, even at DEBUG (L2; ruled on #60)
    assert (line["level"], line["event"], line["request_id"]) == ("INFO", "request", "rid")
    assert (line["outcome"], line["slot_end"]) == ("ok", "free")
    assert (line["client_write_failed"], line["client_write_failed_reason"]) == (
        True,
        "timeout" if final == "hang" else "OSError",
    )


@pytest.mark.td("S15")
def test_s15_a_final_write_that_succeeds_logs_no_failure(monkeypatch, capsys):
    """The contrast: at DEBUG, a clean end whose final write succeeds logs its one line,
    with no `client_write_failed` field."""
    gate, _ = ready_gate()
    gated = lambda _s: _mock_client(_clean)  # noqa: E731
    proxy = _proxy(gate, monkeypatch, gated, log_level="DEBUG")
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    anyio.run(proxy, _chat_scope(), _body_once(), send, CHAT_ROUTE, "rid")
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    (line,) = _log(capsys)
    assert (line["event"], line["outcome"], line["slot_end"]) == ("request", "ok", "free")
    assert "client_write_failed" not in line and "client_write_failed_reason" not in line


S16_RUNS = 50


def _s16_run(order: str, monkeypatch) -> tuple[list, list[dict], list[dict], list[str]]:
    """One run of S16: the client's disconnect is tied to a pinned point of the end."""
    gate, _ = ready_gate()
    ends = _record_ends(monkeypatch)
    moment = anyio.Event()

    def upstream_ends():
        if order == "with-the-end":
            moment.set()  # the stream's last step: upstream has just completed

    gated = lambda _s: _mock_client(lambda request: _framed(upstream_ends))  # noqa: E731
    proxy = _proxy(gate, monkeypatch, gated)
    sent: list[dict] = []
    body = [{"type": "http.request", "body": b"{}", "more_body": False}]

    async def receive():
        if body:
            return body.pop()
        await moment.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if order == "with-the-last-chunk" and message.get("body") == LINE_2:
            moment.set()  # the client goes as the last chunk is written to it
            await anyio.sleep(30)  # so this write never completes; the watcher cancels it

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        anyio.run(proxy, _chat_scope(), receive, send, CHAT_ROUTE, "rid")
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    return ends, sent, lines, _slot_states(gate)


@pytest.mark.td("S16")
@pytest.mark.parametrize(
    ("order", "outcome", "slot", "bodies"),
    [
        # The watcher records the disconnect while the last chunk is still being written, so
        # the stream never ended cleanly: a drain, and nothing more is written.
        ("with-the-last-chunk", "client_disconnect", "draining", [LINE_1, LINE_2]),
        # The disconnect is delivered in the same step as upstream's end. The exchange runs
        # on to its end with no await, and the task group cancels the watcher before it
        # wakes, so the disconnect is never recorded: a clean end, and the final message.
        ("with-the-end", "ok", "free", [LINE_1, LINE_2, b""]),
    ],
    ids=["with-the-last-chunk", "with-the-end"],
)
def test_s16_a_disconnect_at_the_moment_upstream_completes(
    order, outcome, slot, bodies, monkeypatch
):
    for _ in range(S16_RUNS):
        with monkeypatch.context() as patch:
            ends, sent, lines, slots = _s16_run(order, patch)
        assert ends == [("finish", True)]  # one effective end in every run
        (line,) = lines  # and one log line
        assert (line["outcome"], line["slot_end"], slots) == (outcome, slot, [slot])
        assert [m.get("body") for m in sent[1:]] == bodies


@pytest.mark.td("F2")
def test_f2_a_disconnect_during_a_blocked_upstream_write(raw_upstream, sidecar, capsys):
    upstream = _StopsReading()
    gate, _ = ready_gate()
    car = sidecar(raw_upstream(upstream).url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_request(b"/api/chat", b"x" * BIG))
        assert upstream.head.wait(WAIT_S)  # the sidecar read the body and is writing it
        sock.shutdown(socket.SHUT_WR)  # the client goes: uvicorn takes EOF as a disconnect
        wait_for(lambda: _slot_states(gate) == ["draining"])
        assert upstream.seen_close()  # the fake sees EOF: the sidecar closed its socket
        try:
            rest = sock.recv(65536)
        except OSError:
            rest = b""
    assert upstream.read < BIG or not WRITE_IS_CUT_SHORT  # the write never finished
    assert rest == b""  # no 5xx was written to the gone client
    (line,) = _chat_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "client_disconnect",
        None,
        "draining",
    )


def _faulty_pool(how: str):
    """A gated-client factory whose connection pool's `aclose` hangs for ever or raises (F4):
    the fault is injected at the transport, beneath httpx's own `aclose`."""
    real = proxy_module.new_gated_client

    def gated(settings):
        client = real(settings)

        async def aclose():
            if how == "hang":
                await anyio.sleep_forever()
            raise OSError("the pool would not close")

        client._transport._pool.aclose = aclose
        return client

    return gated


F4_CLOSE_S = 1.0
VERSION = status(200, b'{"version":"0.40.16"}', [(b"content-type", b"application/json")])


@pytest.mark.td("F4")
@pytest.mark.parametrize("how", ["hang", "raise"])
def test_f4_an_unproven_close_marks_the_slot_stuck(how, fake_ollama, sidecar, monkeypatch, capsys):
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    fake_ollama.script("GET", "/api/version", VERSION)
    monkeypatch.setattr(proxy_module, "new_gated_client", _faulty_pool(how))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, close_timeout_s=F4_CLOSE_S)
    started = time.perf_counter()
    resp = httpx.post(car.url + "/api/chat", content=b"{}", timeout=WAIT_S)
    elapsed = time.perf_counter() - started
    assert (resp.status_code, resp.content) == (200, b'{"done":true}')  # still answered
    assert elapsed < 2 * F4_CLOSE_S
    wait_for(lambda: _slot_states(gate) == ["stuck"])
    stuck_after = time.perf_counter() - started
    if how == "hang":  # step (a) waited for its bound, then gave up
        assert F4_CLOSE_S <= stuck_after < F4_CLOSE_S + TIMING_BOUND_S
    else:
        assert stuck_after < TIMING_BOUND_S
    busy = refused_post(car.url + "/api/chat")
    assert busy.status_code == 429  # never two at once: a stuck slot is never admitted again
    assert len(fake_ollama.requests) == 1
    health = httpx.get(car.url + "/seriatim/health", timeout=WAIT_S)
    assert (health.status_code, health.json()["reasons"]) == (503, ["slot_stuck"])
    first, second = _chat_lines(capsys, 2)
    assert (first["level"], first["outcome"], first["slot_end"]) == ("CRITICAL", "ok", "stuck")
    assert first["upstream_close_failed"] is True
    assert (second["outcome"], second["slot_end"]) == ("busy", "none")
