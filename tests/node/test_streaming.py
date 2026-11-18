"""Streaming tests (TD-slice-1 §8.4).

S1-6: passthrough without buffering, captured stream replays, and client disconnects during a
stream and while waiting for the headers (S1-S5). Every test runs the sidecar and a fake
Ollama on real loopback sockets (`harness.py`).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from harness import (
    WAIT_S,
    Hold,
    all_free,
    exchange,
    exchange_body,
    raw_connect,
    ready_gate,
    replay,
    sequence,
    status,
    wait_for,
)

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
