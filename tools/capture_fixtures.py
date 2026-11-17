#!/usr/bin/env python3
"""Record real Ollama exchanges and a route probe from the PPE (SPEC §8.1, REQ-5; #26).

Standard library only. It drives `ollama-a` directly, never through Seriatim.

    capture_fixtures.py capture --url <base> --out <file>
    capture_fixtures.py compare <committed> <fresh>

**capture** records each exchange in EXCHANGES (TD-slice-1 App. B, S0b, Fixtures):
- `id`, and the model's digest when the exchange names a model;
- the request: method, path and body (as text: every body here is JSON or deliberately bad
  JSON);
- the response: status, headers in order minus `Date`, the body as base64, and the chunk
  boundaries as observed (the length of each piece `http.client` delivered: one transfer
  chunk for a chunked response).

It adds `ollama_version`, the digest of every PPE model, and a **route probe** of every pair
in `tests/fixtures/route-pairs.json`: each pair is sent a valid request and recorded as
`exists: true` unless the answer is the router's own not-found (HTTP 404 with the plain
body `404 page not found`). A JSON 404 such as "model not found" comes from a handler, so it
proves the route exists. The probe's side effects are undone: the copy and the created
model are deleted.

**Cleaning.** Anything machine-specific (this host's name, IPv4 literals) is masked with
characters of the same length, so lengths and chunk boundaries stay true. The number of
masked spans is recorded as `masked`.

**compare** checks the shape of a committed capture against a fresh one, as the `ppe` job
does: the same exchange ids, statuses, content types, chunked or not, and JSON structure
(object keys and value types, recursively; for NDJSON, the set of line shapes), and the same
route probe. Values may differ; shapes may not. It prints every difference.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import re
import socket
import sys
import urllib.parse
from pathlib import Path

FORMAT = 1
ROUTE_PAIRS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "route-pairs.json"
CHAT = "qwen2.5:0.5b"
EMBED = "all-minilm:22m"
MODELS = ("qwen2.5:0.5b", "qwen2.5:1.5b", "all-minilm:22m")
OPTIONS = {"temperature": 0, "seed": 42, "num_predict": 8}
MESSAGES = [{"role": "user", "content": "Say hello."}]
ROUTER_404 = b"404 page not found"
PROBE_COPY = "seriatim-probe-copy"
PROBE_CREATE = "seriatim-probe-create"
IPV4_RE = re.compile(rb"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
TIMEOUT = 300


def _j(obj: object) -> str:
    return json.dumps(obj, separators=(",", ":"))


# (id, method, path, body or None, model). The order matters: chat runs before ps, so a model
# is loaded when /api/ps is recorded.
EXCHANGES: list[tuple[str, str, str, str | None, str | None]] = [
    ("version", "GET", "/api/version", None, None),
    ("tags", "GET", "/api/tags", None, None),
    (
        "chat.stream",
        "POST",
        "/api/chat",
        _j({"model": CHAT, "messages": MESSAGES, "options": OPTIONS}),
        CHAT,
    ),
    (
        "chat.nostream",
        "POST",
        "/api/chat",
        _j({"model": CHAT, "messages": MESSAGES, "options": OPTIONS, "stream": False}),
        CHAT,
    ),
    (
        "generate.stream",
        "POST",
        "/api/generate",
        _j({"model": CHAT, "prompt": "Say hello.", "options": OPTIONS}),
        CHAT,
    ),
    ("embed", "POST", "/api/embed", _j({"model": EMBED, "input": "hello"}), EMBED),
    (
        "v1.chat.stream",
        "POST",
        "/v1/chat/completions",
        _j(
            {
                "model": CHAT,
                "messages": MESSAGES,
                "temperature": 0,
                "seed": 42,
                "max_tokens": 8,
                "stream": True,
            }
        ),
        CHAT,
    ),
    (
        "v1.chat.nostream",
        "POST",
        "/v1/chat/completions",
        _j({"model": CHAT, "messages": MESSAGES, "temperature": 0, "seed": 42, "max_tokens": 8}),
        CHAT,
    ),
    ("v1.models", "GET", "/v1/models", None, None),
    ("ps", "GET", "/api/ps", None, None),
    ("show", "POST", "/api/show", _j({"model": CHAT}), CHAT),
    ("pull.progress", "POST", "/api/pull", _j({"model": CHAT}), CHAT),
    (
        "chat.model_not_found",
        "POST",
        "/api/chat",
        _j({"model": "seriatim-no-such-model", "messages": MESSAGES, "stream": False}),
        None,
    ),
    ("chat.bad_json", "POST", "/api/chat", '{"model": "qwen2.5:0.5b", "messages": [', None),
]


class CaptureError(Exception):
    """Ollama answered in a way the capture can't record; the message says why."""


def mask(data: bytes, hostname: str) -> tuple[bytes, int]:
    """Mask machine-specific spans with same-length placeholders; return (data, count)."""
    count = 0

    def ip(m: re.Match[bytes]) -> bytes:
        nonlocal count
        count += 1
        return re.sub(rb"[0-9]", b"x", m.group(0))

    data = IPV4_RE.sub(ip, data)
    host = hostname.encode()
    if len(host) >= 3:
        n = data.count(host)
        data = data.replace(host, b"h" * len(host))
        count += n
    return data, count


def exchange(conn_factory, method: str, path: str, body: str | None) -> dict:
    """One request, recorded with the chunk boundaries http.client delivers."""
    conn = conn_factory()
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(
            method, path, body=body.encode() if body is not None else None, headers=headers
        )
        resp = conn.getresponse()
        chunks: list[bytes] = []
        if method != "HEAD":
            while piece := resp.read1(65536):
                chunks.append(piece)
        return {
            "status": resp.status,
            "headers": [[k, v] for k, v in resp.getheaders() if k.lower() != "date"],
            "chunks": chunks,
        }
    finally:
        conn.close()


def record(raw: dict, hostname: str) -> tuple[dict, int]:
    """The response as stored: body base64, chunk lengths, after masking."""
    masked = 0
    pieces = []
    for piece in raw["chunks"]:
        cleaned, n = mask(piece, hostname)
        pieces.append(cleaned)
        masked += n
    headers = []
    for k, v in raw["headers"]:
        cleaned, n = mask(v.encode("latin-1"), hostname)
        headers.append([k, cleaned.decode("latin-1")])
        masked += n
    body = b"".join(pieces)
    return {
        "status": raw["status"],
        "headers": headers,
        "body_b64": base64.b64encode(body).decode(),
        "chunks": [len(p) for p in pieces],
    }, masked


def router_404(raw: dict) -> bool:
    return raw["status"] == 404 and b"".join(raw["chunks"]).strip() == ROUTER_404


def _json(raw: dict) -> object:
    try:
        return json.loads(b"".join(raw["chunks"]))
    except ValueError as e:
        raise CaptureError(f"expected JSON, got status {raw['status']}: {e}") from None


def probe_requests(pairs: dict, blob_digest: str) -> list[tuple[str, str, str, str | None]]:
    """(class, method, path, body) for every pair, in the order that keeps them valid."""
    bodies = {
        ("POST", "/api/show"): _j({"model": CHAT}),
        ("POST", "/api/pull"): _j({"model": CHAT, "stream": False}),
        ("POST", "/api/push"): _j({"model": CHAT, "stream": False}),
        ("POST", "/api/copy"): _j({"source": CHAT, "destination": PROBE_COPY}),
        ("DELETE", "/api/delete"): _j({"model": PROBE_COPY}),
        ("POST", "/api/generate"): _j(
            {"model": CHAT, "prompt": "hi", "stream": False, "options": OPTIONS}
        ),
        ("POST", "/api/chat"): _j(
            {"model": CHAT, "messages": MESSAGES, "stream": False, "options": OPTIONS}
        ),
        ("POST", "/api/embed"): _j({"model": EMBED, "input": "hi"}),
        ("POST", "/api/embeddings"): _j({"model": EMBED, "prompt": "hi"}),
        ("POST", "/v1/chat/completions"): _j(
            {"model": CHAT, "messages": MESSAGES, "max_tokens": 8}
        ),
        ("POST", "/v1/completions"): _j({"model": CHAT, "prompt": "hi", "max_tokens": 8}),
        ("POST", "/v1/embeddings"): _j({"model": EMBED, "input": "hi", "encoding_format": "float"}),
        ("POST", "/api/create"): _j({"model": PROBE_CREATE, "from": CHAT, "stream": False}),
    }
    out = []
    for cls in ("ungated_infer", "ungated_admin", "gated_infer", "gated_admin"):
        for method, path in pairs[cls]:
            body = bodies.get((method, path))
            if "<64 hex>" in path:
                path = path.replace("<64 hex>", blob_digest)
            out.append((cls, method, path, body))
    # The blob must exist before HEAD can find it: POST it first.
    out.sort(key=lambda r: 0 if (r[1], r[2].startswith("/api/blobs/")) == ("POST", True) else 1)
    return out


def capture(url: str, hostname: str | None = None, pairs_file: Path = ROUTE_PAIRS) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise CaptureError(f"--url must be http://host:port, not {url!r}")
    host, port = parsed.hostname, parsed.port or 80
    hostname = socket.gethostname() if hostname is None else hostname

    def conn():
        return http.client.HTTPConnection(host, port, timeout=TIMEOUT)

    version = _json(exchange(conn, "GET", "/api/version", None))["version"]
    tags = _json(exchange(conn, "GET", "/api/tags", None))
    digests = {m["name"]: m["digest"] for m in tags["models"]}
    missing = [m for m in MODELS if m not in digests]
    if missing:
        raise CaptureError(f"models not pulled: {missing}")
    masked = 0
    exchanges = []
    for xid, method, path, body, model in EXCHANGES:
        raw = exchange(conn, method, path, body)
        response, n = record(raw, hostname)
        masked += n
        exchanges.append(
            {
                "id": xid,
                "model_digest": digests[model] if model else None,
                "request": {"method": method, "path": path, "body": body},
                "response": response,
            }
        )
    blob = b"seriatim route probe\n"
    digest = hashlib.sha256(blob).hexdigest()
    probe = []
    try:
        for cls, method, path, body in probe_requests(json.loads(pairs_file.read_text()), digest):
            if path.startswith("/api/blobs/") and method == "POST":
                c = conn()
                try:
                    c.request(method, path, body=blob)
                    r = c.getresponse()
                    raw = {"status": r.status, "headers": r.getheaders(), "chunks": [r.read()]}
                finally:
                    c.close()
            else:
                raw = exchange(conn, method, path, body)
            template = path.replace(digest, "<64 hex>")
            probe.append(
                {
                    "class": cls,
                    "method": method,
                    "path": template,
                    "status": raw["status"],
                    "exists": not router_404(raw),
                }
            )
    finally:
        for name in (PROBE_COPY, PROBE_CREATE):
            exchange(conn, "DELETE", "/api/delete", _j({"model": name}))
    return {
        "format": FORMAT,
        "ollama_version": version,
        "models": {m: digests[m] for m in MODELS},
        "masked": masked,
        "exchanges": exchanges,
        "route_probe": probe,
    }


# --- compare ------------------------------------------------------------------------------


def shape(value: object) -> object:
    """The structure of a JSON value: keys and types, recursively; lists by their items."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = []
        for item in value:
            s = shape(item)
            if s not in items:
                items.append(s)
        return ["list", sorted(items, key=json.dumps)]
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def body_shape(response: dict) -> object:
    """The body's shape by its content type; a body that doesn't parse as it says is a shape."""
    body = base64.b64decode(response["body_b64"])
    ctype = next((v for k, v in response["headers"] if k.lower() == "content-type"), "")
    if not body:
        return "empty"
    try:
        return _parsed_shape(body, ctype)
    except ValueError:
        return ["unparseable", ctype]


def _parsed_shape(body: bytes, ctype: str) -> object:
    if "ndjson" in ctype:
        lines = [shape(json.loads(line)) for line in body.splitlines() if line.strip()]
        return ["ndjson", sorted({json.dumps(s, sort_keys=True) for s in lines})]
    if "event-stream" in ctype:
        events = set()
        for line in body.splitlines():
            if line.startswith(b"data: ") and line != b"data: [DONE]":
                events.add(json.dumps(shape(json.loads(line[6:])), sort_keys=True))
        return ["sse", sorted(events)]
    if "json" in ctype:
        return shape(json.loads(body))
    return ["text", ctype]


def exchange_shape(ex: dict) -> dict:
    r = ex["response"]
    headers = {k.lower(): v for k, v in r["headers"]}
    return {
        "status": r["status"],
        "content-type": headers.get("content-type"),
        "chunked": headers.get("transfer-encoding") == "chunked",
        "body": body_shape(r),
    }


def compare(committed: dict, fresh: dict) -> list[str]:
    diffs = []
    for key in ("format", "ollama_version"):
        if committed.get(key) != fresh.get(key):
            diffs.append(f"{key}: committed {committed.get(key)!r}, fresh {fresh.get(key)!r}")
    if set(committed.get("models", {})) != set(fresh.get("models", {})):
        diffs.append("models: the PPE's model list differs")
    old = {e["id"]: e for e in committed.get("exchanges", [])}
    new = {e["id"]: e for e in fresh.get("exchanges", [])}
    for xid in sorted(set(old) ^ set(new)):
        diffs.append(f"exchange {xid}: only in the {'committed' if xid in old else 'fresh'} file")
    for xid in sorted(set(old) & set(new)):
        a, b = exchange_shape(old[xid]), exchange_shape(new[xid])
        for field in a:
            if a[field] != b[field]:
                diffs.append(
                    f"exchange {xid}: {field} differs: committed {a[field]!r}, fresh {b[field]!r}"
                )

    def probe(d: dict) -> dict:
        return {
            (p["method"], p["path"]): (p["status"], p["exists"]) for p in d.get("route_probe", [])
        }

    po, pn = probe(committed), probe(fresh)
    for pair in sorted(set(po) | set(pn)):
        if po.get(pair) != pn.get(pair):
            diffs.append(
                f"route probe {pair[0]} {pair[1]}: committed (status, exists) "
                f"{po.get(pair)}, fresh {pn.get(pair)}"
            )
    return diffs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--url", required=True)
    c.add_argument("--out", type=Path, required=True)
    m = sub.add_parser("compare")
    m.add_argument("committed", type=Path)
    m.add_argument("fresh", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "capture":
            data = capture(a.url)
            a.out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
            print(
                f"captured {len(data['exchanges'])} exchanges and {len(data['route_probe'])} "
                f"route pairs from Ollama {data['ollama_version']} ({data['masked']} masked)"
            )
            return 0
        diffs = compare(json.loads(a.committed.read_text()), json.loads(a.fresh.read_text()))
    except (CaptureError, OSError, ValueError, KeyError) as e:
        print(f"::error::capture_fixtures: {type(e).__name__}: {e}")
        return 1
    for d in diffs:
        print(f"::error::fixture shape: {d}")
    if not diffs:
        print("the committed fixtures match a fresh capture in shape")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
