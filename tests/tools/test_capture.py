"""Tests for tools/capture_fixtures.py: cleaning, format, the route probe, the shape check (#26).

The capture runs end to end against a fake Ollama: a real HTTP/1.1 server on loopback that
answers with chunked NDJSON, SSE, JSON errors and the router's plain-text 404.
"""

from __future__ import annotations

import base64
import copy
import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "capture_fixtures", ROOT / "tools/capture_fixtures.py"
)
cf = importlib.util.module_from_spec(_spec)
sys.modules["capture_fixtures"] = cf
_spec.loader.exec_module(cf)

HOST = "fakehost7"
CHAT_LINES = [
    b'{"message":{"content":"Hi"},"done":false}\n',
    b'{"done":true,"done_reason":"stop"}\n',
]
DIGESTS = {m: f"{i}" * 64 for i, m in enumerate(cf.MODELS, 1)}
LEAK_IP = ".".join(["10", "1", "2", "3"])  # built at run time: ip-literals scans this file
KNOWN = {
    ("GET", "/"), ("HEAD", "/"), ("GET", "/api/tags"), ("GET", "/api/ps"),
    ("GET", "/api/version"), ("POST", "/api/show"), ("GET", "/v1/models"),
    ("POST", "/api/pull"), ("POST", "/api/push"), ("POST", "/api/copy"),
    ("DELETE", "/api/delete"), ("POST", "/api/generate"), ("POST", "/api/chat"),
    ("POST", "/api/embed"), ("POST", "/api/embeddings"), ("POST", "/v1/chat/completions"),
    ("POST", "/v1/completions"), ("POST", "/v1/embeddings"), ("POST", "/api/create"),
}  # fmt: skip


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[tuple[str, str, bytes]] = []
    blobs: set[str] = set()

    def log_message(self, *args):  # quiet
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _chunked(self, pieces: list[bytes], ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for p in pieces:
            self.wfile.write(f"{len(p):x}\r\n".encode() + p + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")

    def _handle(self) -> None:
        body = self._body()
        type(self).seen.append((self.command, self.path, body))
        pair = (self.command, self.path)
        if self.path.startswith("/api/blobs/sha256:") and self.command in ("POST", "HEAD"):
            digest = self.path.split(":", 1)[1]
            if self.command == "POST":
                type(self).blobs.add(digest)
                return self._send(201, b"")
            return self._send(200 if digest in type(self).blobs else 404, b"")
        if pair not in KNOWN:
            return self._send(404, b"404 page not found", "text/plain")
        if pair == ("GET", "/api/version"):
            return self._send(200, b'{"version":"0.40.16"}')
        if pair == ("GET", "/api/tags"):
            models = [{"name": m, "digest": d, "details": {"parameter_size": "1B"}}
                      for m, d in DIGESTS.items()]  # fmt: skip
            return self._send(200, json.dumps({"models": models}).encode())
        try:
            req = json.loads(body) if body else {}
        except ValueError:
            return self._send(400, b'{"error":"unexpected EOF"}')
        if req.get("model") == "seriatim-no-such-model":
            return self._send(404, b'{"error":"model \'seriatim-no-such-model\' not found"}')
        if pair in (("POST", "/api/chat"), ("POST", "/api/generate")) and req.get("stream", True):
            return self._chunked(CHAT_LINES, "application/x-ndjson")
        if pair == ("POST", "/v1/chat/completions") and req.get("stream"):
            return self._chunked([b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
                                  b"data: [DONE]\n\n"], "text/event-stream")  # fmt: skip
        if pair == ("POST", "/api/show"):
            return self._send(200, json.dumps({"modelfile": f"# on {HOST} {LEAK_IP}"}).encode())
        if pair == ("GET", "/"):
            return self._send(200, b"Ollama is running", "text/plain; charset=utf-8")
        return self._send(200, b'{"ok":true}')

    do_GET = do_POST = do_HEAD = do_DELETE = do_PUT = _handle


@pytest.fixture
def ollama():
    FakeOllama.seen, FakeOllama.blobs = [], set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
    t = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def captured(ollama) -> dict:
    return cf.capture(ollama, hostname=HOST)


def by_id(data: dict) -> dict:
    return {e["id"]: e for e in data["exchanges"]}


def body(ex: dict) -> bytes:
    return base64.b64decode(ex["response"]["body_b64"])


# --- format -------------------------------------------------------------------------------


def test_the_capture_records_every_exchange_in_the_td_format(captured):
    assert captured["format"] == 1
    assert captured["ollama_version"] == "0.40.16"
    assert captured["models"] == DIGESTS
    ids = [e["id"] for e in captured["exchanges"]]
    assert ids == [x[0] for x in cf.EXCHANGES]
    required = (
        "chat.stream chat.nostream generate.stream embed v1.chat.stream v1.chat.nostream tags "
        "ps version show pull.progress chat.model_not_found chat.bad_json v1.models"
    )
    assert set(required.split()) <= set(ids)
    for ex in captured["exchanges"]:
        assert set(ex) == {"id", "model_digest", "request", "response"}
        assert set(ex["request"]) == {"method", "path", "body"}
        assert set(ex["response"]) == {"status", "headers", "body_b64", "chunks"}
        assert sum(ex["response"]["chunks"]) == len(body(ex))
        assert all(k.lower() != "date" for k, _ in ex["response"]["headers"])
    ex = by_id(captured)
    assert ex["chat.stream"]["model_digest"] == DIGESTS["qwen2.5:0.5b"]
    assert ex["embed"]["model_digest"] == DIGESTS["all-minilm:22m"]
    assert ex["version"]["model_digest"] is None


def test_chunk_boundaries_are_the_observed_transfer_chunks(captured):
    ex = by_id(captured)
    assert ex["chat.stream"]["response"]["chunks"] == [len(line) for line in CHAT_LINES]
    assert len(ex["v1.chat.stream"]["response"]["chunks"]) == 2
    assert ex["chat.nostream"]["response"]["chunks"] == [len(body(ex["chat.nostream"]))]


def test_errors_are_recorded_as_ollama_sent_them(captured):
    ex = by_id(captured)
    assert ex["chat.model_not_found"]["response"]["status"] == 404
    assert b"not found" in body(ex["chat.model_not_found"])
    assert ex["chat.bad_json"]["response"]["status"] == 400
    assert ex["chat.bad_json"]["request"]["body"].endswith("[")


# --- cleaning ------------------------------------------------------------------------------


def test_machine_specific_text_is_masked_at_the_same_length(captured):
    show = by_id(captured)["show"]
    data = body(show)
    assert HOST.encode() not in data and LEAK_IP.encode() not in data
    assert b"h" * len(HOST) in data and b"xx.x.x.x" in data
    assert show["response"]["chunks"] == [len(data)]
    assert captured["masked"] == 2


@pytest.mark.parametrize(
    ("raw", "expected", "count"),
    [
        (b"no host here", b"no host here", 0),
        (("at " + LEAK_IP).encode(), b"at xx.x.x.x", 1),
        (b"version 0.40.16 and 1.2.3.4.5", b"version 0.40.16 and 1.2.3.4.5", 0),
        (b"fakehost7:11434 fakehost7", b"hhhhhhhhh:11434 hhhhhhhhh", 2),
    ],
)
def test_mask(raw, expected, count):
    assert cf.mask(raw, HOST) == (expected, count)


def test_a_name_or_ip_split_across_chunks_is_still_masked():
    # #29 review, fix 1: masking runs on the joined body, then the boundaries are restored.
    text = f"on {HOST} at {LEAK_IP} done".encode()
    cut_host, cut_ip = text.index(HOST.encode()) + 4, text.index(LEAK_IP.encode()) + 3
    pieces = [text[:cut_host], text[cut_host:cut_ip], text[cut_ip:]]
    raw = {"status": 200, "headers": [], "chunks": pieces}
    response, masked = cf.record(raw, HOST)
    body = base64.b64decode(response["body_b64"])
    assert masked == 2
    assert HOST.encode() not in body and LEAK_IP.encode() not in body
    assert body == f"on {'h' * len(HOST)} at xx.x.x.x done".encode()
    assert response["chunks"] == [len(p) for p in pieces]


def test_a_short_hostname_is_not_masked():
    assert cf.mask(b"ab abc", "ab") == (b"ab abc", 0)


# --- the route probe ------------------------------------------------------------------------


def test_the_route_probe_covers_every_pair_and_cleans_up(captured, ollama):
    pairs = json.loads(cf.ROUTE_PAIRS.read_text())
    listed = [(m, p) for cls in ("ungated_infer", "ungated_admin", "gated_infer", "gated_admin")
              for m, p in pairs[cls]]  # fmt: skip
    probed = [(p["method"], p["path"]) for p in captured["route_probe"]]
    assert sorted(probed) == sorted(listed)
    assert all(p["exists"] for p in captured["route_probe"]), captured["route_probe"]
    head = next(
        p for p in captured["route_probe"] if p["method"] == "HEAD" and "blobs" in p["path"]
    )
    assert head["status"] == 200  # the blob was POSTed first
    deletes = [json.loads(b) for m, p, b in FakeOllama.seen if (m, p) == ("DELETE", "/api/delete")]
    assert {d["model"] for d in deletes} == {cf.PROBE_COPY, cf.PROBE_CREATE}


def test_only_the_routers_own_404_means_absent(ollama, tmp_path):
    pairs = json.loads(cf.ROUTE_PAIRS.read_text())
    pairs["gated_admin"].append(["POST", "/api/not-a-route"])
    f = tmp_path / "pairs.json"
    f.write_text(json.dumps(pairs))
    data = cf.capture(ollama, hostname=HOST, pairs_file=f)
    probe = {(p["method"], p["path"]): p for p in data["route_probe"]}
    assert probe[("POST", "/api/not-a-route")] == {
        "class": "gated_admin", "method": "POST", "path": "/api/not-a-route",
        "status": 404, "exists": False,
    }  # fmt: skip
    assert cf.router_404({"status": 404, "chunks": [b"404 page not found\n"]})
    assert not cf.router_404({"status": 404, "chunks": [b'{"error":"model not found"}']})
    assert not cf.router_404({"status": 200, "chunks": [b"404 page not found"]})


def test_a_missing_head_route_is_absent(ollama, tmp_path):
    # #29 review, fix 3: a HEAD answer has no body, so any 404 means the route is absent.
    pairs = json.loads(cf.ROUTE_PAIRS.read_text())
    pairs["gated_admin"].append(["HEAD", "/api/not-a-route"])
    f = tmp_path / "pairs.json"
    f.write_text(json.dumps(pairs))
    probe = {(p["method"], p["path"]): p for p in cf.capture(ollama, HOST, f)["route_probe"]}
    assert probe[("HEAD", "/api/not-a-route")]["exists"] is False
    assert probe[("HEAD", "/")]["exists"] is True
    assert probe[("HEAD", "/api/blobs/sha256:<64 hex>")]["exists"] is True


@pytest.mark.parametrize(
    ("method", "status", "body", "exists"),
    [
        ("HEAD", 404, b"", False),
        ("HEAD", 200, b"", True),
        ("HEAD", 405, b"", True),
        ("GET", 404, b"404 page not found", False),
        ("GET", 404, b'{"error":"not found"}', True),
        ("POST", 500, b'{"error":"unauthorized"}', True),
    ],
)
def test_route_exists(method, status, body, exists):
    assert cf.route_exists(method, {"status": status, "chunks": [body]}) is exists


def test_route_pairs_are_the_req5_pairs():
    """route-pairs.json is copied from SPEC REQ-5: every pair is named there, and vice versa."""
    spec = (ROOT / "docs/SPEC.md").read_text(encoding="utf-8")
    req5 = spec[spec.index("- **REQ-5. Classification.**") : spec.index("- **REQ-6.")]
    named = set(re.findall(r"`(GET|HEAD|POST|DELETE) (/[^`]*)`", req5))
    pairs = json.loads(cf.ROUTE_PAIRS.read_text())
    listed = {(m, p) for cls in ("ungated_infer", "ungated_admin", "gated_infer", "gated_admin")
              for m, p in pairs[cls]}  # fmt: skip
    assert listed == named


# --- compare -------------------------------------------------------------------------------


def set_ctype(ex: dict, ctype: str) -> None:
    headers = ex["response"]["headers"]
    i = next(i for i, (k, _) in enumerate(headers) if k.lower() == "content-type")
    headers[i] = [headers[i][0], ctype]


def probe_entry(data: dict, method: str, path: str) -> dict:
    return next(p for p in data["route_probe"] if (p["method"], p["path"]) == (method, path))


def test_a_capture_matches_itself_and_values_may_differ(captured):
    assert cf.compare(captured, copy.deepcopy(captured)) == []
    fresh = copy.deepcopy(captured)
    ex = by_id(fresh)["chat.nostream"]
    ex["response"]["body_b64"] = base64.b64encode(b'{"ok":false}').decode()
    ex["response"]["chunks"] = [len(b'{"ok":false}')]
    assert cf.compare(captured, fresh) == []


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d.update(ollama_version="0.41.0"), "ollama_version: committed '0.40.16'"),
        (lambda d: d["exchanges"].pop(), "exchange chat.bad_json: only in the committed file"),
        (lambda d: by_id(d)["tags"]["response"].update(status=500), "exchange tags: status"),
        (
            lambda d: by_id(d)["chat.nostream"]["response"].update(
                body_b64=base64.b64encode(b'{"ok":true,"new":1}').decode()
            ),
            "exchange chat.nostream: body differs",
        ),
        (
            lambda d: set_ctype(by_id(d)["chat.stream"], "application/json"),
            "exchange chat.stream: content-type differs",
        ),
        (
            lambda d: probe_entry(d, "GET", "/").update(exists=False),
            "route probe GET /: committed (status, exists) (200, True), fresh (200, False)",
        ),
        (lambda d: d.update(models={}), "models: the PPE's model list differs"),
    ],
)
def test_compare_reports_each_shape_change(captured, edit, message):
    fresh = copy.deepcopy(captured)
    edit(fresh)
    diffs = cf.compare(captured, fresh)
    assert any(message in d for d in diffs), diffs


@pytest.mark.parametrize(
    ("chunks", "ok"),
    [(None, True), ("none", False), ("short", False), ("zero", False), ("long", False)],
)
def test_compare_checks_chunks_describe_the_body(captured, chunks, ok):
    # #29 review, fix 2: the replay follows these boundaries, so they must add up.
    fresh = copy.deepcopy(captured)
    r = by_id(fresh)["chat.stream"]["response"]
    size = sum(r["chunks"])
    r["chunks"] = {
        None: r["chunks"], "none": [], "short": [size - 1], "zero": [0, size], "long": [size, 1],
    }[chunks]  # fmt: skip
    diffs = cf.compare(captured, fresh)
    expected = (
        f"exchange chat.stream (fresh): chunks {r['chunks']} don't add up to its {size}-byte body"
    )
    assert (expected in diffs) is not ok, diffs
    assert cf.compare(fresh, captured) == ([] if ok else [expected.replace("fresh", "committed")])


def test_an_empty_body_has_no_chunks(captured):
    fresh = copy.deepcopy(captured)
    r = by_id(fresh)["version"]["response"]
    r["body_b64"], r["chunks"] = "", []
    assert not any("chunks" in d for d in cf.compare(fresh, fresh))
    r["chunks"] = [0]
    assert any("chunks [0] don't add up to its 0-byte body" in d for d in cf.compare(fresh, fresh))


def test_shapes():
    assert cf.shape({"b": [1, 2.5], "a": {"x": None, "y": True}}) == {
        "a": {"x": "null", "y": "bool"},
        "b": ["list", ["number"]],
    }
    assert cf.shape([]) == ["list", []]
    assert cf.shape("s") == "str"


def test_body_shapes_by_content_type():
    def r(b: bytes, ctype: str) -> dict:
        return {"body_b64": base64.b64encode(b).decode(), "headers": [["Content-Type", ctype]]}

    assert cf.body_shape(r(b"", "application/json")) == "empty"
    assert cf.body_shape(r(b"Ollama is running", "text/plain")) == ["text", "text/plain"]
    nd = cf.body_shape(r(b'{"a":1}\n{"a":2}\n{"b":"x"}\n', "application/x-ndjson"))
    assert nd == ["ndjson", ['{"a": "number"}', '{"b": "str"}']]
    sse = cf.body_shape(r(b'data: {"a":1}\n\ndata: [DONE]\n\n', "text/event-stream"))
    assert sse == ["sse", ['{"a": "number"}']]
    assert cf.body_shape(r(b"not json", "application/json")) == ["unparseable", "application/json"]


# --- the command line -------------------------------------------------------------------------


def test_main_capture_and_compare(ollama, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cf.socket, "gethostname", lambda: HOST)
    out = tmp_path / "fresh.json"
    assert cf.main(["capture", "--url", ollama, "--out", str(out)]) == 0
    assert "route pairs from Ollama 0.40.16 (2 masked)" in capsys.readouterr().out
    assert cf.main(["compare", str(out), str(out)]) == 0
    assert "match a fresh capture in shape" in capsys.readouterr().out
    other = json.loads(out.read_text())
    other["ollama_version"] = "9.9.9"
    (tmp_path / "other.json").write_text(json.dumps(other))
    assert cf.main(["compare", str(out), str(tmp_path / "other.json")]) == 1
    assert "::error::fixture shape: ollama_version" in capsys.readouterr().out
    assert cf.main(["compare", str(tmp_path / "missing.json"), str(out)]) == 1
    assert "FileNotFoundError" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://127.0.0.1:1", "127.0.0.1:11434", "http://"])
def test_capture_needs_an_http_url(url):
    with pytest.raises(cf.CaptureError, match="--url must be"):
        cf.capture(url)


def test_capture_fails_when_a_model_is_missing(ollama, monkeypatch):
    monkeypatch.setattr(cf, "MODELS", (*cf.MODELS, "qwen2.5:99b"))
    with pytest.raises(cf.CaptureError, match="models not pulled"):
        cf.capture(ollama, hostname=HOST)
