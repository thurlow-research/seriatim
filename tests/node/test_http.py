"""HTTP-level tests (TD-slice-1 §8.3).

S1-1: the error table against the contract (K-OAS3). S1-3: the contract against `classify()`
(K-OAS1). S1-5: the dispatcher and health over real sockets (H12, H22, H23, E2, HL1-HL7,
HL9), with the harness in `harness.py`. S1-6: the proxy core over real sockets (H1, H2,
H4-H8, H25, B1-B5, E1). S1-7: the header rules (H3, H9, H11, H15-H18, H20, H21, H24).
S1-8: the request limits (E3, E4, E7-E11, B6, B7, H28, H29). S1-9: the framing rule, the
in-stream record and internal errors (H13, E5, E15). S1-10: client construction after
admission, the full ungated pool and the ungated stalls and closes (E16, H14, H26, H27).
S1-11: streamed blob uploads (H10, E6, E12-E14).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import random
import re
import socket
import threading
import time
import tracemalloc
from pathlib import Path

import anyio
import httpx
import jsonschema
import pytest
import yaml
from harness import (
    TIMING_BOUND_S,
    WAIT_S,
    FakeOllama,
    Hold,
    RawResponse,
    ThreadServer,
    abort_body,
    all_free,
    closed_port,
    drip_body,
    exchange,
    exchange_body,
    make_settings,
    raw_connect,
    raw_exchange,
    read_response,
    ready_gate,
    replay,
    reserve_socket,
    sequence,
    stall_body,
    stall_headers,
    status,
    wait_for,
)
from openapi_spec_validator import OpenAPIV31SpecValidator, validate

import seriatim_node.proxy as proxy_module
from seriatim_node.app import (
    ERROR_TABLE,
    ErrorCode,
    ErrorFactory,
    ErrorSpec,
    NoCookies,
    RequestLog,
    create_app,
    new_health_client,
    request_id_from,
)
from seriatim_node.gate import BLOB_RE, Kind, Route, Scope, classify
from seriatim_node.proxy import Proxy

# Resolved from this file, so the same path works when mutmut copies tests/ and contract/
# into node/ (tools/ci/mutation.py).
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "openapi.yaml"


@pytest.fixture(scope="module")
def contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def envelope_validator(contract):
    """A validator for ErrorEnvelope whose local $refs resolve within the contract."""
    schema = {
        **contract["components"]["schemas"]["ErrorEnvelope"],
        "components": contract["components"],
    }
    return jsonschema.Draft202012Validator(schema)


@pytest.mark.td("K-OAS3")
def test_error_code_enum_equals_the_contract(contract):
    assert {c.value for c in ErrorCode} == set(
        contract["components"]["schemas"]["ErrorCode"]["enum"]
    )


@pytest.mark.td("K-OAS3")
def test_error_table_equals_x_seriatim_codes(contract):
    codes = contract["components"]["x-seriatim-codes"]
    table = {
        code.value: {"status": s.status, "retryable": s.retryable, "replay_safe": s.replay_safe}
        for code, s in ERROR_TABLE.items()
    }
    assert table == codes
    assert set(ERROR_TABLE) == set(ErrorCode)


def test_error_table_is_read_only():
    with pytest.raises(TypeError):
        ERROR_TABLE[ErrorCode.GPU_BUSY] = ErrorSpec(200, False, False)  # type: ignore[index]


class _Factory:
    """Builds the factory per call, so mutation testing sees its constructor run."""

    def response(self, *args, **kwargs):
        return ErrorFactory("node-a").response(*args, **kwargs)


FACTORY = _Factory()


def _headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    names = [k for k, _ in raw]
    assert len(names) == len(set(names)), "no header is sent twice"
    assert all(k == k.lower() for k in names)
    return {k.decode(): v.decode() for k, v in raw}


def test_gpu_busy_matches_appendix_a(envelope_validator):
    status, raw, body = FACTORY.response(
        ErrorCode.GPU_BUSY,
        "GPU busy: another request is running on this node",
        retry_after=15,
        gate_value="ungated",
        request_id="r-1",
        busy_for_s=12.4,
    )
    assert status == 429
    assert body == (
        b'{"error":"GPU busy: another request is running on this node","error_code":"GPU_BUSY",'
        b'"retryable":true,"replay_safe":true,"retry_after":15,"source":"sidecar","node":"node-a",'
        b'"busy_for_s":12.4}'
    )
    assert _headers(raw) == {
        "content-type": "application/json",
        "content-length": str(len(body)),
        "retry-after": "15",
        "x-seriatim-gate": "busy",
        "x-seriatim-node": "node-a",
        "x-request-id": "r-1",
    }
    envelope_validator.validate(json.loads(body))


# Written out from TD §5 and SPEC REQ-37, not imported from app.py, so the test is independent.
TD5_FIXED_GATE = {
    ErrorCode.GPU_BUSY: "busy",
    ErrorCode.NODE_STARTING: "refused",
    ErrorCode.NODE_OFFLINE: "refused",
    ErrorCode.EXPOSURE_UNVERIFIED: "refused",
    ErrorCode.OLLAMA_EXPOSED: "refused",
    ErrorCode.NUM_CTX_EXCEEDS_LIMIT: "refused",
}


@pytest.mark.parametrize("code", [c for c in ErrorCode if ERROR_TABLE[c].status is not None])
@pytest.mark.parametrize("gate_value", ["acquired", "ungated"])
def test_every_response_code_uses_the_table(code, gate_value, envelope_validator):
    status, raw, body = FACTORY.response(code, "msg", gate_value=gate_value, request_id="rid")
    spec = ERROR_TABLE[code]
    doc = json.loads(body)
    assert status == spec.status
    assert doc == {
        "error": "msg",
        "error_code": code.value,
        "retryable": spec.retryable,
        "replay_safe": spec.replay_safe,
        "source": "sidecar",
        "node": "node-a",
    }
    headers = _headers(raw)
    # TD §5 fixes X-Seriatim-Gate for these codes; every other code sends the caller's value
    # (#71 review). Adding a code to the fixed-gate table by mistake fails here.
    assert headers["x-seriatim-gate"] == TD5_FIXED_GATE.get(code, gate_value)
    assert "retry-after" not in headers
    assert headers["content-length"] == str(len(body))
    assert headers["x-request-id"] == "rid"
    assert headers["x-seriatim-node"] == "node-a"
    envelope_validator.validate(doc)


@pytest.mark.parametrize(
    ("code", "gate"),
    [
        (ErrorCode.GPU_BUSY, "busy"),
        (ErrorCode.NODE_STARTING, "refused"),
        (ErrorCode.NODE_OFFLINE, "refused"),
        (ErrorCode.EXPOSURE_UNVERIFIED, "refused"),
        (ErrorCode.OLLAMA_EXPOSED, "refused"),
        (ErrorCode.NUM_CTX_EXCEEDS_LIMIT, "refused"),
        (ErrorCode.BAD_PATH, "ungated"),
        (ErrorCode.LOCK_HOLD_EXCEEDED, "acquired"),
    ],
)
def test_gate_header(code, gate):
    caller = "acquired" if gate == "acquired" else "ungated"
    _, raw, _ = FACTORY.response(code, "m", gate_value=caller, request_id="r")
    assert _headers(raw)["x-seriatim-gate"] == gate


def test_fixed_gate_ignores_the_call_site():
    _, raw, _ = FACTORY.response(
        ErrorCode.NODE_STARTING, "m", gate_value="acquired", request_id="r"
    )
    assert _headers(raw)["x-seriatim-gate"] == "refused"


def test_retry_after_zero_is_sent():
    _, raw, body = FACTORY.response(
        ErrorCode.NODE_SATURATED, "m", retry_after=0, gate_value="ungated", request_id="r"
    )
    assert _headers(raw)["retry-after"] == "0"
    assert json.loads(body)["retry_after"] == 0


def test_headers_are_in_order():
    _, raw, _ = FACTORY.response(
        ErrorCode.NODE_STARTING, "m", retry_after=3, gate_value="ungated", request_id="r"
    )
    assert [k for k, _ in raw] == [
        b"content-type",
        b"content-length",
        b"retry-after",
        b"x-seriatim-gate",
        b"x-seriatim-node",
        b"x-request-id",
    ]


def test_extra_fields_are_appended(envelope_validator):
    _, _, body = FACTORY.response(
        ErrorCode.BODY_TOO_LARGE,
        "too big",
        gate_value="ungated",
        request_id="r",
        limit=1024,
        hint="h",
    )
    doc = json.loads(body)
    assert list(doc)[-2:] == ["limit", "hint"]
    assert doc["limit"] == 1024
    envelope_validator.validate(doc)


def test_in_stream_code_is_refused():
    with pytest.raises(ValueError, match="in-stream"):
        FACTORY.response(
            ErrorCode.UPSTREAM_STREAM_BROKEN, "m", gate_value="acquired", request_id="r"
        )


@pytest.mark.parametrize("bad", ["busy", "refused", "", "ACQUIRED"])
def test_bad_gate_value_is_refused(bad):
    with pytest.raises(ValueError, match="gate_value"):
        FACTORY.response(ErrorCode.INTERNAL, "m", gate_value=bad, request_id="r")


@pytest.mark.parametrize("key", ["retryable", "replay_safe", "source", "node", "error", "other"])
def test_unknown_or_core_extra_is_refused(key):
    with pytest.raises(ValueError, match="not ErrorEnvelope fields"):
        FACTORY.response(ErrorCode.INTERNAL, "m", gate_value="ungated", request_id="r", **{key: 1})


@pytest.mark.parametrize("bad", [-1, 1.5, True, "5"])
def test_bad_retry_after_is_refused(bad):
    with pytest.raises(ValueError, match="retry_after"):
        FACTORY.response(
            ErrorCode.GPU_BUSY, "m", retry_after=bad, gate_value="ungated", request_id="r"
        )


def test_non_finite_extra_is_refused():
    with pytest.raises(ValueError):
        FACTORY.response(
            ErrorCode.GPU_BUSY, "m", gate_value="ungated", request_id="r", busy_for_s=float("nan")
        )


def test_node_id_comes_from_the_factory():
    _, raw, body = ErrorFactory("node-b").response(
        ErrorCode.INTERNAL, "m", gate_value="ungated", request_id="r"
    )
    assert json.loads(body)["node"] == "node-b"
    assert _headers(raw)["x-seriatim-node"] == "node-b"


# --- K-OAS1: the contract is valid and agrees with classify() ------------------------------

HTTP_METHODS = ("get", "head", "post", "put", "delete", "options", "patch", "trace")
SAMPLE_DIGEST = "sha256:" + "0123456789abcdef" * 4
# Paths S1 serves itself (TD §2.2 SERIATIM_ROUTES); the rest of /seriatim/* comes later.
S1_SERIATIM_PATHS = {"/seriatim/health"}


def _refs(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref":
                yield v
            else:
                yield from _refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from _refs(v)


def _resolve(doc, ref: str):
    assert ref.startswith("#/"), f"only local $refs are used: {ref}"
    node = doc
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        assert isinstance(node, dict) and part in node, f"unresolved $ref {ref}"
        node = node[part]
    return node


def _operations(contract):
    for path, item in contract["paths"].items():
        for method in HTTP_METHODS:
            if method in item:
                yield path, method.upper(), item[method]


def _raw(path: str) -> bytes:
    return path.replace("{digest}", SAMPLE_DIGEST).encode()


def _is_seriatims(path: str) -> bool:
    return path == "/seriatim" or path.startswith("/seriatim/") or path == "/models/select"


@pytest.mark.td("K-OAS1")
def test_k_oas1_contract_is_valid_openapi_3_1(contract):
    assert contract["openapi"].startswith("3.1")
    validate(contract, cls=OpenAPIV31SpecValidator)


@pytest.mark.td("K-OAS1")
def test_k_oas1_every_ref_resolves(contract):
    refs = set(_refs(contract))
    assert refs, "the contract uses $refs"
    for ref in refs:
        _resolve(contract, ref)


@pytest.mark.td("K-OAS1")
def test_k_oas1_ollama_paths_agree_with_classify(contract):
    checked = 0
    for path, method, op in _operations(contract):
        if _is_seriatims(path):
            continue
        route = classify(method, _raw(path))
        assert route.known, (method, path)
        assert route.kind.value == op["x-seriatim-gate"], (method, path)
        assert route.scope.value == op["x-seriatim-scope"], (method, path)
        checked += 1
    assert checked == 21  # every pair of TD §2.2 rules 2 to 4, the blob routes once each


@pytest.mark.td("K-OAS1")
def test_k_oas1_blob_parameter_matches_blob_re(contract):
    params = contract["paths"]["/api/blobs/{digest}"]["parameters"]
    (digest,) = [p for p in params if p["name"] == "digest"]
    assert BLOB_RE.pattern.decode() == "^/api/blobs/" + digest["schema"]["pattern"][1:]


@pytest.mark.td("K-OAS1")
def test_k_oas1_seriatim_paths_declare_a_scope(contract):
    for path, method, op in _operations(contract):
        if not _is_seriatims(path):
            continue
        assert "x-seriatim-gate" not in op, (method, path)
        assert op["x-seriatim-scope"] in {"none", "infer", "admin"}, (method, path)
        if path in S1_SERIATIM_PATHS:
            route = classify(method, _raw(path))
            assert route == Route(Kind.SERIATIM, Scope(op["x-seriatim-scope"]), True, 200)


@pytest.mark.td("K-OAS1")
def test_k_oas1_unknown_endpoint_agrees_with_rule_5(contract):
    rule = contract["x-seriatim-unknown-endpoint"]
    for method, path in [("GET", b"/api/unknown"), ("OPTIONS", b"/api/tags"), ("post", b"/")]:
        route = classify(method, path)
        assert (route.kind.value, route.scope.value, route.known) == (
            rule["x-seriatim-gate"],
            rule["x-seriatim-scope"],
            False,
        )


# --- S1-5: the dispatcher (TD §2.4) ---------------------------------------------------------

VERSION_OK = status(200, b'{"version":"0.40.16"}', [(b"content-type", b"application/json")])
HEX32 = re.compile(r"[0-9a-f]{32}")


@pytest.fixture(scope="module")
def health_validator(contract):
    return jsonschema.Draft202012Validator(contract["components"]["schemas"]["Health"])


def _error_doc(body: bytes, code: ErrorCode, envelope_validator) -> dict:
    doc = json.loads(body)
    envelope_validator.validate(doc)
    assert doc["error_code"] == code.value
    assert doc["node"] == "node-a" and doc["source"] == "sidecar"
    return doc


@pytest.mark.td("H12")
@pytest.mark.parametrize(
    "request_bytes",
    [
        b"POST /api/chat HTTP/1.0\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}",
        b"GET /api/tags HTTP/1.0\r\nHost: x\r\n\r\n",
    ],
)
def test_h12_http_1_0_gets_505(request_bytes, fake_ollama, sidecar, envelope_validator):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = raw_exchange(car.port, request_bytes)
    assert resp.status == 505
    _error_doc(resp.body, ErrorCode.HTTP_VERSION_UNSUPPORTED, envelope_validator)
    assert resp.header("connection") == ["close"]
    assert resp.header("x-seriatim-gate") == ["ungated"]
    assert fake_ollama.requests == []
    assert all_free(gate)


@pytest.mark.td("E2")
def test_e2_unsafe_raw_path_gets_400(fake_ollama, sidecar, envelope_validator):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = raw_exchange(
        car.port,
        b"POST /api/chat/../show HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
        b"Connection: close\r\n\r\n{}",
    )
    assert resp.status == 400
    doc = _error_doc(resp.body, ErrorCode.BAD_PATH, envelope_validator)
    assert doc["error"] == "the path contains a . or .. segment"
    assert resp.header("x-seriatim-gate") == ["ungated"]
    assert fake_ollama.requests == []
    assert all_free(gate)


@pytest.mark.td("H22")
@pytest.mark.parametrize(
    ("method", "path", "code", "allow"),
    [
        ("GET", "/seriatim/nope", ErrorCode.NOT_FOUND, []),
        ("GET", "/seriatim", ErrorCode.NOT_FOUND, []),
        ("POST", "/seriatim/health", ErrorCode.METHOD_NOT_ALLOWED, ["GET"]),
    ],
)
def test_h22_seriatim_paths(method, path, code, allow, fake_ollama, sidecar, envelope_validator):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = httpx.request(method, car.url + path)
    assert resp.status_code == ERROR_TABLE[code].status
    _error_doc(resp.content, code, envelope_validator)
    assert resp.headers.get_list("allow") == allow
    assert resp.headers.get_list("x-seriatim-gate") == ["ungated"]
    assert resp.headers.get_list("x-seriatim-node") == ["node-a"]
    assert fake_ollama.requests == []
    assert all_free(gate)


def _call(app, method: str, raw_path: bytes | None, http_version: str = "1.1") -> list[dict]:
    """Call the ASGI app directly, as uvicorn would, and return the messages it sends."""
    scope = {
        "type": "http",
        "http_version": http_version,
        "method": method,
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-length", b"2"), (b"x-request-id", b"rid")],
    }
    if raw_path is not None:
        scope["raw_path"] = raw_path
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    anyio.run(app, scope, receive, send)
    return sent


@pytest.mark.td("H23")
def test_h23_missing_raw_path_is_a_500(fake_ollama, envelope_validator):
    gate, _ = ready_gate()
    app = create_app(make_settings(fake_ollama.url), gate=gate)
    sent = _call(app, "POST", None)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 500
    _error_doc(sent[1]["body"], ErrorCode.INTERNAL, envelope_validator)
    assert fake_ollama.requests == []
    assert all_free(gate)


# Each dispatcher refusal, exactly: the response and its log line (the request id is "rid").
_UNCHECKED = {"request_id": "rid", "method": "GET"}
REFUSALS = [
    # (method, raw_path, http_version, code, message, extra headers, log level, outcome, where)
    ("GET", b"/api/tags", "1.0", ErrorCode.HTTP_VERSION_UNSUPPORTED, "only HTTP/1.1 is supported",
     [(b"connection", b"close")], "INFO", "rejected", {}),
    ("POST", None, "1.1", ErrorCode.INTERNAL, "internal error", [], "ERROR", "internal_error",
     {}),
    ("GET", b"/a//b", "1.1", ErrorCode.BAD_PATH, "the path contains //", [], "INFO", "rejected",
     {}),
    ("GET", b"/seriatim/nope", "1.1", ErrorCode.NOT_FOUND, "no such path", [], "INFO",
     "rejected", {"path": "/seriatim/nope", "kind": "seriatim", "scope": "none",
                  "known": False}),
    ("POST", b"/seriatim/health", "1.1", ErrorCode.METHOD_NOT_ALLOWED, "method not allowed",
     [(b"allow", b"GET")], "INFO", "rejected", {"path": "/seriatim/health",
                                                "kind": "seriatim", "scope": "none",
                                                "known": False}),
]  # fmt: skip


@pytest.mark.parametrize(
    ("method", "raw_path", "version", "code", "message", "extra", "level", "outcome", "where"),
    REFUSALS,
    ids=[f"{r[3]}-{r[1]}" for r in REFUSALS],
)
def test_dispatcher_refusals_exactly(
    method, raw_path, version, code, message, extra, level, outcome, where, capsys
):
    gate, _ = ready_gate()
    app = create_app(make_settings("http://127.0.0.1:11435"), gate=gate)
    capsys.readouterr()
    sent = _call(app, method, raw_path, version)
    status_, headers, body = ErrorFactory("node-a").response(
        code, message, gate_value="ungated", request_id="rid"
    )
    assert sent == [
        {"type": "http.response.start", "status": status_, "headers": headers + extra},
        {"type": "http.response.body", "body": body, "more_body": False},
    ]
    (line,) = _log_lines(capsys.readouterr().out)
    del line["ts"]
    assert line == {
        "level": level,
        "event": "request",
        "request_id": "rid",
        "method": method,
        **where,
        "outcome": outcome,
        "status": status_,
        "slot_end": "none",
        "node": "node-a",
    }
    assert list(line)[-4:] == ["outcome", "status", "slot_end", "node"]
    assert all_free(gate)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"x-request-id", b"")],
        [(b"x-request-id", b"a" * 129)],
        [(b"x-request-id", b"a b")],
        [(b"x-request-id", b"a/b")],
        [(b"x-request-id", b"a\r\nb")],
        [(b"x-request-id", b"abc\n")],
        [(b"x-request-id", b"ok"), (b"X-Request-Id", b"ok")],
    ],
)
@pytest.mark.td("H18")
def test_request_id_is_replaced(headers):
    assert HEX32.fullmatch(request_id_from(headers))


@pytest.mark.parametrize("value", [b"abc-1.2_3", b"a" * 128, b"Z"])
def test_valid_request_id_is_kept(value):
    assert request_id_from([(b"host", b"x"), (b"X-Request-Id", value)]) == value.decode()


def test_request_ids_are_fresh():
    assert request_id_from([]) != request_id_from([])


def test_the_sidecar_echoes_the_request_id(fake_ollama, sidecar):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = httpx.get(car.url + "/seriatim/health", headers={"x-request-id": "abc-1.2_3"})
    assert resp.headers.get_list("x-request-id") == ["abc-1.2_3"]
    resp = httpx.get(car.url + "/seriatim/nope", headers={"x-request-id": "a b"})
    (rid,) = resp.headers.get_list("x-request-id")
    assert HEX32.fullmatch(rid)


# --- S1-5: the request log (REQ-22) ---------------------------------------------------------


def _log_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith('{"ts"')]


def test_request_log_line_exactly(capsys):
    RequestLog("INFO", "node-a", wall=lambda: 1763287200.1239).emit(request_id="r", status=200)
    assert capsys.readouterr().out == (
        '{"ts":"2025-11-16T10:00:00.123Z","level":"INFO","event":"request",'
        '"request_id":"r","status":200,"node":"node-a"}\n'
    )


def test_request_log_uses_the_wall_clock(capsys):
    RequestLog("INFO", "n").emit()
    (doc,) = _log_lines(capsys.readouterr().out)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", doc["ts"])
    assert (
        abs(
            time.mktime(time.strptime(doc["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
            - time.mktime(time.gmtime())
        )
        < 5
    )


def test_health_client_settings():
    client = new_health_client(make_settings("http://127.0.0.1:11435", health_probe_timeout_s=3))
    pool = client._transport._pool
    assert (pool._max_connections, pool._max_keepalive_connections) == (1, 1)
    assert (pool._http1, pool._http2) == (True, False)
    assert client.base_url == httpx.URL("http://127.0.0.1:11435/")
    assert client.timeout == httpx.Timeout(3.0)
    assert (client.trust_env, client.follow_redirects) == (False, False)
    assert isinstance(client.cookies.jar, NoCookies)
    anyio.run(client.aclose)


def test_no_cookies_stores_nothing():
    jar = httpx.Cookies(NoCookies())
    request = httpx.Request("GET", "http://127.0.0.1:11435/api/tags")
    jar.extract_cookies(httpx.Response(200, headers={"set-cookie": "s=1"}, request=request))
    assert list(jar.jar) == []
    plain = httpx.Cookies()
    plain.extract_cookies(httpx.Response(200, headers={"set-cookie": "s=1"}, request=request))
    assert len(list(plain.jar)) == 1  # the same response is stored by an ordinary jar


@pytest.mark.parametrize(
    ("threshold", "written"),
    [
        ("DEBUG", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
        ("INFO", ["INFO", "WARNING", "ERROR", "CRITICAL"]),
        ("WARNING", ["WARNING", "ERROR", "CRITICAL"]),
        ("ERROR", ["ERROR", "CRITICAL"]),
    ],
)
def test_request_log_threshold(threshold, written, capsys):
    log = RequestLog(threshold, "n")
    for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        log.emit(level=level)
    assert [d["level"] for d in _log_lines(capsys.readouterr().out)] == written


def test_rejections_are_logged_at_info(fake_ollama, sidecar, capsys):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    httpx.get(car.url + "/seriatim/nope", headers={"x-request-id": "r1"})
    raw_exchange(car.port, b"GET /api/tags HTTP/1.0\r\n\r\n")
    raw_exchange(car.port, b"GET /a//b HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    lines = _log_lines(capsys.readouterr().out)
    assert [(d["level"], d["status"], d["outcome"], d["slot_end"]) for d in lines] == [
        ("INFO", 404, "rejected", "none"),
        ("INFO", 505, "rejected", "none"),
        ("INFO", 400, "rejected", "none"),
    ]
    assert lines[0]["request_id"] == "r1"
    assert (lines[0]["path"], lines[0]["kind"], lines[0]["known"]) == (
        "/seriatim/nope",
        "seriatim",
        False,
    )
    assert "path" not in lines[2]  # an unsafe path is never logged


# --- S1-5: health (TD §3.4) ----------------------------------------------------------------


def _health(car, health_validator) -> httpx.Response:
    resp = httpx.get(car.url + "/seriatim/health", timeout=30)
    health_validator.validate(resp.json())
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers.get_list("x-seriatim-gate") == ["ungated"]
    assert resp.headers.get_list("x-seriatim-node") == ["node-a"]
    assert "0.40.16" not in resp.text and "127.0.0.1" not in resp.text
    return resp


def _reasons(resp: httpx.Response) -> list[str] | None:
    return resp.json().get("reasons")


@pytest.mark.td("HL1")
def test_hl1_healthy(fake_ollama, sidecar, health_validator):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _health(car, health_validator)
    assert resp.status_code == 200
    assert resp.content == b'{"status":"ok"}'
    assert [(r.method, r.raw_path) for r in fake_ollama.requests] == [("GET", b"/api/version")]


@pytest.mark.td("HL2")
def test_hl2_upstream_down(sidecar, health_validator):
    closed = reserve_socket()  # bound, never listening: nothing answers
    try:
        gate, _ = ready_gate()
        car = sidecar(f"http://127.0.0.1:{closed.getsockname()[1]}", gate=gate)
        resp = _health(car, health_validator)
        assert resp.status_code == 503
        assert resp.json() == {"status": "degraded", "reasons": ["ollama_unreachable"]}
    finally:
        closed.close()


@pytest.mark.td("HL3")
@pytest.mark.parametrize(
    "behaviour",
    [
        status(500, b'{"version":"0.40.16"}'),
        status(200, b"not json"),
        status(200, b"\xff\xfe"),
        status(200, b"[]"),
        status(200, b"{}"),
        status(200, b'{"version":""}'),
        status(200, b'{"version":7}'),
        status(200, b'{"version":null}'),
    ],
)
def test_hl3_bad_response(behaviour, fake_ollama, sidecar, health_validator):
    fake_ollama.script("GET", "/api/version", behaviour)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _health(car, health_validator)
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "reasons": ["ollama_bad_response"]}


@pytest.mark.td("HL4")
def test_hl4_overdue_then_stuck(fake_ollama, sidecar, health_validator):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, clock = ready_gate(max_hold_s=10)
    car = sidecar(fake_ollama.url, gate=gate)
    lease = gate.try_acquire("/api/chat", "r1")
    clock.advance(40)  # exactly max_hold_s + 30 is not yet overdue
    assert _health(car, health_validator).status_code == 200
    clock.advance(0.5)
    resp = _health(car, health_validator)
    assert resp.status_code == 503 and _reasons(resp) == ["slot_overdue"]
    assert "/api/chat" not in resp.text and "r1" not in resp.text
    assert lease.stuck()
    resp = _health(car, health_validator)
    assert resp.status_code == 503 and _reasons(resp) == ["slot_stuck"]


@pytest.mark.td("HL5")
@pytest.mark.parametrize(
    "behaviour",
    [stall_headers(), stall_body(b'{"version":"0.4', 21), drip_body(100, 0.1)],
    ids=["headers", "body", "drip"],  # a drip never trips a per-read timeout: the bound is total
)
def test_hl5_stalled_probe(behaviour, fake_ollama, sidecar, health_validator):
    fake_ollama.script("GET", "/api/version", behaviour)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, health_probe_timeout_s=0.5)
    started = time.perf_counter()
    resp = _health(car, health_validator)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 503 and _reasons(resp) == ["ollama_unreachable"]
    assert 0.4 < elapsed < 0.5 + TIMING_BOUND_S
    fake_ollama.release.set()


class StubGate:
    """Only what health reads: each fault is a flag the test clears."""

    def __init__(self) -> None:
        self.is_overdue = self.is_stuck = self.is_starting = True

    def overdue(self):
        return ["holder"] if self.is_overdue else []

    def any_stuck(self):
        return self.is_stuck

    def starting(self):
        return self.is_starting


@pytest.mark.td("HL6")
def test_hl6_every_fault_in_order(sidecar, health_validator):
    down = reserve_socket()
    gate = StubGate()
    car = sidecar(f"http://127.0.0.1:{down.getsockname()[1]}", gate=gate)
    order = ["ollama_unreachable", "slot_overdue", "slot_stuck", "starting"]
    assert _reasons(_health(car, health_validator)) == order
    fake = FakeOllama()
    fake.script("GET", "/api/version", VERSION_OK)
    fake.start(down)  # Ollama comes up on the same port
    try:
        assert _reasons(_health(car, health_validator)) == order[1:]
        gate.is_overdue = False
        assert _reasons(_health(car, health_validator)) == order[2:]
        gate.is_stuck = False
        assert _reasons(_health(car, health_validator)) == order[3:]
        gate.is_starting = False
        resp = _health(car, health_validator)
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    finally:
        fake.stop()


@pytest.mark.td("HL6")
def test_hl6_real_gate_faults_in_order(sidecar, health_validator):
    """The faults a real Gate can hold at once: it can't be overdue while starting."""
    down = reserve_socket()
    gate, clock = ready_gate(slots=2, max_hold_s=10)
    car = sidecar(f"http://127.0.0.1:{down.getsockname()[1]}", gate=gate)
    overdue = gate.try_acquire("/api/chat", "overdue")
    stuck = gate.try_acquire("/api/chat", "stuck")
    clock.advance(10 + 30 + 1)  # past max_hold_s plus overdue()'s 30 s grace
    assert stuck.stuck()
    order = ["ollama_unreachable", "slot_overdue", "slot_stuck"]
    assert _reasons(_health(car, health_validator)) == order
    fake = FakeOllama()
    fake.script("GET", "/api/version", VERSION_OK)
    fake.start(down)  # Ollama comes up on the same port
    try:
        assert _reasons(_health(car, health_validator)) == order[1:]
        assert overdue.finish(clean=True)
        resp = _health(car, health_validator)  # STUCK is never freed before a restart
        assert resp.status_code == 503 and _reasons(resp) == ["slot_stuck"]
    finally:
        fake.stop()


@pytest.mark.td("HL6")
def test_hl6_a_new_gate_is_starting(fake_ollama, sidecar, health_validator):
    """Without an injected gate, the lifespan's Gate starts in its quarantine."""
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    car = sidecar(fake_ollama.url, drain_s=600)
    resp = _health(car, health_validator)
    assert resp.status_code == 503 and _reasons(resp) == ["starting"]


@pytest.mark.td("HL7")
def test_hl7_normal_use_is_not_a_fault(fake_ollama, sidecar, health_validator):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, clock = ready_gate(slots=2, max_hold_s=10)
    car = sidecar(fake_ollama.url, gate=gate)
    gate.try_acquire("/api/chat", "held")
    assert gate.try_acquire("/api/chat", "drained").finish(clean=False)
    clock.advance(5)
    assert sorted(s.state.value for s in gate.slots()) == ["draining", "held"]
    resp = _health(car, health_validator)
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


@pytest.mark.td("HL9")
@pytest.mark.parametrize(("level", "lines"), [("INFO", 0), ("DEBUG", 10)])
def test_hl9_health_is_logged_at_debug(level, lines, fake_ollama, sidecar, capsys):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, log_level=level)
    capsys.readouterr()
    for i in range(10):
        httpx.get(car.url + "/seriatim/health", headers={"x-request-id": f"h{i}"})
    logged = _log_lines(capsys.readouterr().out)
    assert len(logged) == lines
    for i, d in enumerate(logged):
        del d["ts"]
        assert d == _health_line(f"h{i}", "ok", 200)


def _health_line(rid: str, outcome: str, status_: int) -> dict:
    return {
        "level": "DEBUG",
        "event": "request",
        "request_id": rid,
        "method": "GET",
        "path": "/seriatim/health",
        "kind": "seriatim",
        "scope": "none",
        "known": True,
        "outcome": outcome,
        "status": status_,
        "slot_end": "none",
        "node": "node-a",
    }


@pytest.mark.td("HL9")
def test_hl9_a_degraded_health_line(sidecar, capsys):
    closed = reserve_socket()
    try:
        gate, _ = ready_gate()
        car = sidecar(f"http://127.0.0.1:{closed.getsockname()[1]}", gate=gate, log_level="DEBUG")
        capsys.readouterr()
        httpx.get(car.url + "/seriatim/health", headers={"x-request-id": "d1"}, timeout=30)
        (line,) = _log_lines(capsys.readouterr().out)
        del line["ts"]
        assert line == _health_line("d1", "degraded", 503)
    finally:
        closed.close()


# --- S1-6: the proxy core (TD §3.3, §4, §5) --------------------------------------------------

CHAT = b'{"model":"m","messages":[]}'


def _slot_states(gate) -> list[str]:
    return sorted(s.state.value for s in gate.slots())


def _in_background(fn, *args, **kwargs) -> concurrent.futures.Future:
    """Run a blocking client call in a thread, so the test can act while it is held."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn, *args, **kwargs)
    pool.shutdown(wait=False)
    return future


def _chat(car, body: bytes = CHAT) -> httpx.Response:
    return httpx.post(car.url + "/api/chat", content=body, timeout=WAIT_S)


@pytest.mark.td("H1")
def test_h1_non_streaming_chat_replayed(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", replay("chat.nostream"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    captured = exchange("chat.nostream")
    resp = _chat(car, captured["request"]["body"].encode())
    assert resp.status_code == captured["response"]["status"] == 200
    assert resp.headers["content-type"] == "application/json; charset=utf-8"
    assert resp.content == exchange_body("chat.nostream")
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    assert resp.headers.get_list("x-seriatim-node") == ["node-a"]
    (rid,) = resp.headers.get_list("x-request-id")
    assert HEX32.fullmatch(rid)
    (req,) = fake_ollama.requests
    assert dict(req.headers)[b"x-request-id"] == rid.encode()
    wait_for(lambda: all_free(gate))


@pytest.mark.td("H2")
def test_h2_unusual_json_reaches_upstream_byte_identical(fake_ollama, sidecar):
    body = (
        b'{"options":{"num_ctx": 30000 ,"top_k":40,"seed":7},  "model":"q\\u00e9",'
        b'"n":1.0e0,"x_future":[1],"messages":[]}\n'
    )
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    assert _chat(car, body).status_code == 200
    (req,) = fake_ollama.requests
    assert req.body == body
    assert [v for k, v in req.headers if k == b"content-length"] == [str(len(body)).encode()]


@pytest.mark.td("H4")
@pytest.mark.parametrize(
    ("line", "route", "raw_path", "query"),
    [
        (b"GET /api/tags?x=1&y=%20", ("GET", "/api/tags"), b"/api/tags", b"x=1&y=%20"),
        (b"POST /api/show%20x", ("POST", "/api/show x"), b"/api/show%20x", b""),
    ],
)
def test_h4_raw_path_and_query_are_byte_identical(
    line, route, raw_path, query, fake_ollama, sidecar
):
    fake_ollama.script(*route, status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    request = line + b" HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
    assert raw_exchange(car.port, request).status == 200
    (req,) = fake_ollama.requests
    assert (req.raw_path, req.query) == (raw_path, query)


@pytest.mark.td("H5")
def test_h5_upstream_404_passes_through_and_frees_the_slot(fake_ollama, sidecar):
    body = b'{"error":"model \\"nope\\" not found"}'
    fake_ollama.script(
        "POST", "/api/chat", status(404, body, [(b"content-type", b"application/json")])
    )
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert (resp.status_code, resp.content) == (404, body)
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    wait_for(lambda: all_free(gate))  # a clean end: freed at once, and the clock never moved


@pytest.mark.td("H25")
@pytest.mark.parametrize(
    ("code", "ctype", "body"),
    [
        (500, b"application/json", b'{"error":"x"}'),
        (503, b"text/plain", b"oops"),
        (502, b"text/html", b"<html>bad gateway</html>"),
    ],
)
def test_h25_clean_upstream_errors_pass_through(code, ctype, body, fake_ollama, sidecar):
    first = status(code, body, [(b"content-type", ctype)])
    fake_ollama.script("POST", "/api/chat", sequence(first, status(200, b"{}")))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert (resp.status_code, resp.headers["content-type"], resp.content) == (
        code,
        ctype.decode(),
        body,
    )
    wait_for(lambda: all_free(gate))
    assert _chat(car).status_code == 200  # freed with no drain: the fake clock never moved


@pytest.mark.td("H6")
def test_h6_ungated_routes_pass_while_gated_is_held(fake_ollama, sidecar):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    tags = httpx.get(car.url + "/api/tags", timeout=WAIT_S)
    assert (tags.status_code, tags.content) == (200, b'{"models":[]}')
    assert tags.headers.get_list("x-seriatim-gate") == ["ungated"]
    for method, path in [("OPTIONS", "/api/tags"), ("GET", "/api/chat")]:  # gated, unknown
        resp = httpx.request(method, car.url + path, timeout=WAIT_S)
        assert resp.status_code == 429
        assert resp.headers["x-seriatim-gate"] == "busy"
    hold.release()
    assert held.result(WAIT_S).status_code == 200
    assert [r.method for r in fake_ollama.requests] == ["POST", "GET"]


@pytest.mark.td("H7")
def test_h7_pull_progress_streams_while_gated_is_held(fake_ollama, sidecar):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    fake_ollama.script("POST", "/api/pull", replay("pull.progress"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    pull = httpx.post(car.url + "/api/pull", content=b'{"model":"m"}', timeout=WAIT_S)
    assert pull.status_code == 200
    assert pull.headers["content-type"] == "application/x-ndjson"
    assert pull.content == exchange_body("pull.progress")
    assert pull.headers.get_list("x-seriatim-gate") == ["ungated"]
    hold.release()
    assert held.result(WAIT_S).status_code == 200


def _raw_chat(body: bytes = CHAT) -> bytes:
    length = str(len(body)).encode()
    return b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: " + length + b"\r\n\r\n" + body


@pytest.mark.td("H8")
def test_h8_keep_alive_next_request_is_admitted(fake_ollama, sidecar, monkeypatch):
    """The lease ends in step (b), before step (c) sends the final body message, so the next
    request on the same connection is never refused because of its own previous one."""
    entered, go = threading.Event(), threading.Event()
    original = Proxy._finish_response
    free_at_step_c = []

    async def held_at_step_c(self, *args, **kwargs):
        free_at_step_c.append(all_free(self.gate))
        if len(free_at_step_c) == 1:
            entered.set()
            while not go.is_set():
                await anyio.sleep(0.002)
        await original(self, *args, **kwargs)

    monkeypatch.setattr(Proxy, "_finish_response", held_at_step_c)
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_raw_chat())
        first = read_response(sock)  # every body byte arrives before step (c)
        assert (first.status, first.body) == (200, b'{"done":true}')
        assert entered.wait(WAIT_S)
        sock.sendall(_raw_chat())  # at once, while step (c) is held
        assert all_free(gate)
        go.set()
        second = read_response(sock)
    assert (second.status, second.body) == (200, b'{"done":true}')
    wait_for(lambda: len(free_at_step_c) == 2)  # step (c) runs after the body bytes are sent
    assert free_at_step_c == [True, True]
    assert len(fake_ollama.requests) == 2


@pytest.mark.td("H8")
def test_h8b_an_abnormal_end_drains(fake_ollama, sidecar):
    """The abort closes the keep-alive connection (TD §2.3), so the next requests use new ones."""
    cut_short = abort_body(b'{"do', 100)
    fake_ollama.script("POST", "/api/chat", sequence(cut_short, status(200, b'{"done":true}')))
    gate, clock = ready_gate(drain_s=10)
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_raw_chat())
        received = b""
        while chunk := sock.recv(65536):  # ends only when the sidecar closes the connection
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 ") and b"content-length: 100" in head.lower()
    assert body == b'{"do'  # cut short: 4 of the 100 bytes, then the connection closes
    wait_for(lambda: _slot_states(gate) == ["draining"])
    assert _chat(car).status_code == 429
    clock.advance(10)
    assert _chat(car).status_code == 200


@pytest.mark.td("B1")
def test_b1_second_request_gets_429_at_once(fake_ollama, sidecar, envelope_validator):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    started = time.perf_counter()
    resp = _chat(car)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 429
    assert elapsed < TIMING_BOUND_S
    assert resp.headers["retry-after"] == "15"
    assert resp.headers.get_list("x-seriatim-gate") == ["busy"]
    doc = _error_doc(resp.content, ErrorCode.GPU_BUSY, envelope_validator)
    assert (doc["retryable"], doc["replay_safe"], doc["retry_after"]) == (True, True, 15)
    assert doc["busy_for_s"] >= 0
    assert len(fake_ollama.requests) == 1
    hold.release()
    assert held.result(WAIT_S).status_code == 200


@pytest.mark.td("B2")
def test_b2_a_clean_end_admits_the_next_at_once(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    assert _chat(car).status_code == 200
    wait_for(lambda: all_free(gate))
    assert _chat(car).status_code == 200
    assert len(fake_ollama.requests) == 2


@pytest.mark.td("B3")
def test_b3_429_without_reading_the_body(fake_ollama, sidecar):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    with raw_connect(car.port) as sock:
        sock.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 10485760\r\n\r\n")
        resp = read_response(sock)  # no body byte was ever sent
    assert resp.status == 429
    assert resp.header("x-seriatim-gate") == ["busy"]
    assert len(fake_ollama.requests) == 1
    hold.release()
    assert held.result(WAIT_S).status_code == 200


@pytest.mark.td("B4")
def test_b4_two_slots(fake_ollama, sidecar):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate(slots=2)
    car = sidecar(fake_ollama.url, gate=gate)
    first, second = _in_background(_chat, car), _in_background(_chat, car)
    hold.wait_entered(2)
    assert _chat(car).status_code == 429
    hold.release(0)
    wait_for(lambda: _slot_states(gate) == ["free", "held"])
    fourth = _in_background(_chat, car)
    hold.wait_entered(3)
    hold.release_all()
    assert [f.result(WAIT_S).status_code for f in (first, second, fourth)] == [200, 200, 200]


@pytest.mark.td("B5")
def test_b5_startup_quarantine(fake_ollama, sidecar, envelope_validator, health_validator):
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    car = sidecar(fake_ollama.url, drain_s=600)  # the lifespan's Gate starts in quarantine
    with raw_connect(car.port) as sock:
        sock.sendall(b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 10485760\r\n\r\n")
        resp = read_response(sock)  # the body is never read
    assert resp.status == 503
    doc = _error_doc(resp.body, ErrorCode.NODE_STARTING, envelope_validator)
    (retry_after,) = resp.header("retry-after")
    assert 0 < int(retry_after) <= 600 and doc["retry_after"] == int(retry_after)
    assert resp.header("x-seriatim-gate") == ["refused"]
    assert httpx.get(car.url + "/api/tags", timeout=WAIT_S).status_code == 200
    health = _health(car, health_validator)
    assert health.status_code == 503 and _reasons(health) == ["starting"]
    assert [r.raw_path for r in fake_ollama.requests] == [b"/api/tags", b"/api/version"]


@pytest.mark.td("E1")
def test_e1_closed_port_is_502_and_frees_the_slot(sidecar, envelope_validator):
    gate, _ = ready_gate()
    car = sidecar(f"http://127.0.0.1:{closed_port()}", gate=gate)  # refused, on every OS
    for _ in range(2):  # the second is 502 too, never 429: nothing was sent
        resp = _chat(car)
        assert resp.status_code == 502
        doc = _error_doc(resp.content, ErrorCode.UPSTREAM_UNAVAILABLE, envelope_validator)
        assert doc["replay_safe"] is True
        assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
        wait_for(lambda: all_free(gate))


# --- S1-6: the lifecycle's branches, in process ----------------------------------------------
# The proxy is called directly, with httpx's MockTransport upstream, so each branch of §3.3 and
# §5 is checked exactly. The over-the-wire tests for these paths come with their own IDs in
# S1-8 to S1-10; these pin the S1-6 structure they build on.


class _Stream(httpx.AsyncByteStream):
    """A response body: `chunks`, then optionally a stall or an error; `aclose` may fail."""

    def __init__(self, chunks=(), *, then=None, close=None):
        self.chunks, self.then, self.close = list(chunks), then, close

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.then == "stall":
            await anyio.sleep(30)
        elif self.then is not None:
            raise self.then

    async def aclose(self):
        if self.close == "raise":
            raise OSError("close failed")
        if self.close == "hang":
            await anyio.sleep(30)


class _Client(httpx.AsyncClient):
    """A client whose own `aclose` can fail or hang (the gated close proof)."""

    def __init__(self, handler, close=None):
        super().__init__(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1")
        self.close = close

    async def aclose(self):
        await super().aclose()
        if self.close == "raise":
            raise OSError("close failed")
        if self.close == "hang":
            await anyio.sleep(30)


def _run_proxy(
    handler,
    *,
    method="POST",
    path=b"/api/chat",
    gate=None,
    gated_close=None,
    shared=None,
    monkeypatch,
    cancel_after=None,
    **settings,
):
    """Call the proxy directly; return (messages sent, gate, the gated client used)."""
    settings = make_settings("http://127.0.0.1:1", close_timeout_s=0.2, **settings)
    gate = gate or ready_gate()[0]
    gated: list[_Client] = []

    def new_gated(_settings):
        gated.append(_Client(handler, gated_close))
        return gated[-1]

    monkeypatch.setattr(proxy_module, "new_gated_client", new_gated)
    proxy = Proxy(
        settings,
        gate,
        shared or _Client(handler),
        RequestLog(settings.log_level, settings.id),
        ErrorFactory(settings.id),
    )
    scope = {
        "type": "http",
        "method": method,
        "raw_path": path,
        "query_string": b"",
        "headers": [(b"host", b"x"), (b"content-length", b"2")],
    }
    sent: list[dict] = []
    body = [{"type": "http.request", "body": b"{}", "more_body": False}]

    async def receive():
        if body:
            return body.pop()
        await anyio.sleep(30)  # the watcher waits; no disconnect comes

    async def send(message):
        sent.append(message)

    async def main():
        with anyio.move_on_after(cancel_after if cancel_after is not None else 30):
            await proxy(scope, receive, send, classify(method, path), "rid")

    anyio.run(main)
    return sent, gate, gated


def _ok(request):
    # A stream body: MockTransport treats `content=` as already read, so aiter_raw would fail.
    # The `content-length` frames it: a close-delimited response is broken (S1-9).
    return httpx.Response(200, headers={"content-length": "13"}, stream=_Stream([b'{"done":true}']))


def _one_line(capsys) -> dict:
    (line,) = _log_lines(capsys.readouterr().out)
    return line


def test_proxy_clean_gated_exchange(monkeypatch, capsys):
    sent, gate, _ = _run_proxy(_ok, monkeypatch=monkeypatch)
    assert [(m["type"], m.get("more_body")) for m in sent] == [
        ("http.response.start", None),
        ("http.response.body", True),
        ("http.response.body", False),
    ]
    assert sent[1]["body"] == b'{"done":true}' and sent[2]["body"] == b""
    assert all_free(gate)
    line = _one_line(capsys)
    assert (line["level"], line["outcome"], line["status"], line["slot_end"]) == (
        "INFO",
        "ok",
        200,
        "free",
    )
    assert (line["path"], line["kind"], line["scope"], line["known"]) == (
        "/api/chat",
        "gated",
        "infer",
        True,
    )
    assert "upstream_close_failed" not in line


@pytest.mark.parametrize("close", ["raise", "hang"])
def test_proxy_unproven_close_marks_the_slot_stuck(close, monkeypatch, capsys):
    sent, gate, _ = _run_proxy(_ok, gated_close=close, monkeypatch=monkeypatch)
    assert _slot_states(gate) == ["stuck"]
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    line = _one_line(capsys)
    assert (line["level"], line["slot_end"], line["upstream_close_failed"]) == (
        "CRITICAL",
        "stuck",
        True,
    )


def test_proxy_gated_client_creation_fails(monkeypatch, capsys, envelope_validator):
    def boom(_settings):
        raise RuntimeError("no client")

    settings = make_settings("http://127.0.0.1:1")
    gate, _ = ready_gate()
    proxy = Proxy(
        settings, gate, _Client(_ok), RequestLog("INFO", "node-a"), ErrorFactory("node-a")
    )
    monkeypatch.setattr(proxy_module, "new_gated_client", boom)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"method": "POST", "raw_path": b"/api/chat", "query_string": b"", "headers": []}
    anyio.run(proxy, scope, receive, send, classify("POST", b"/api/chat"), "rid")
    assert sent[0]["status"] == 500
    assert [v for k, v in sent[0]["headers"] if k == b"x-seriatim-gate"] == [b"acquired"]
    _error_doc(sent[1]["body"], ErrorCode.INTERNAL, envelope_validator)
    assert all_free(gate)  # nothing was sent: freed with no drain
    line = _one_line(capsys)
    assert (line["level"], line["outcome"], line["slot_end"]) == ("ERROR", "internal_error", "free")


async def _never_answers(request):
    await anyio.sleep(30)


def test_proxy_hold_limit_before_the_headers(monkeypatch, capsys, envelope_validator):
    gate, _ = ready_gate(max_hold_s=0.2)  # the fake clock stands still: 0.2 s of real time
    sent, _, _ = _run_proxy(_never_answers, gate=gate, monkeypatch=monkeypatch)
    assert sent[0]["status"] == 504
    _error_doc(sent[1]["body"], ErrorCode.LOCK_HOLD_EXCEEDED, envelope_validator)
    assert _slot_states(gate) == ["draining"]  # it was sent upstream
    line = _one_line(capsys)
    assert (line["level"], line["outcome"], line["slot_end"]) == (
        "CRITICAL",
        "hold_exceeded",
        "draining",
    )


def test_proxy_hold_limit_mid_stream_aborts(monkeypatch, capsys):
    gate, _ = ready_gate(max_hold_s=0.2)
    sent, _, _ = _run_proxy(
        lambda r: httpx.Response(200, stream=_Stream([b"a\n"], then="stall")),
        gate=gate,
        monkeypatch=monkeypatch,
    )
    assert [m.get("more_body") for m in sent] == [None, True]  # no final message: an abort
    assert _slot_states(gate) == ["draining"]
    assert _one_line(capsys)["outcome"] == "hold_exceeded"


@pytest.mark.parametrize(
    ("error", "code", "outcome", "slot"),
    [
        (httpx.ConnectError("refused"), ErrorCode.UPSTREAM_UNAVAILABLE, "upstream_unavailable",
         "free"),
        (httpx.ConnectTimeout("slow"), ErrorCode.UPSTREAM_UNAVAILABLE, "upstream_unavailable",
         "free"),
        (httpx.ReadError("reset"), ErrorCode.UPSTREAM_PROTOCOL_ERROR, "upstream_error",
         "draining"),
        (ValueError("bug"), ErrorCode.INTERNAL, "internal_error", "draining"),
    ],
)  # fmt: skip
def test_proxy_errors_before_the_headers(
    error, code, outcome, slot, monkeypatch, capsys, envelope_validator
):
    def fail(request):
        raise error

    sent, gate, _ = _run_proxy(fail, monkeypatch=monkeypatch)
    assert sent[0]["status"] == ERROR_TABLE[code].status
    assert b"connection" not in dict(sent[0]["headers"])  # the body was read: keep-alive is safe
    _error_doc(sent[1]["body"], code, envelope_validator)
    assert _slot_states(gate) == [slot]
    line = _one_line(capsys)
    assert (line["outcome"], line["slot_end"], line["status"]) == (
        outcome,
        slot,
        ERROR_TABLE[code].status,
    )
    assert line["level"] == ("ERROR" if code is ErrorCode.INTERNAL else "INFO")


def test_proxy_ungated_pool_timeout_is_saturated(monkeypatch, capsys, envelope_validator):
    def full(request):
        raise httpx.PoolTimeout("full")

    sent, gate, gated = _run_proxy(full, method="GET", path=b"/api/tags", monkeypatch=monkeypatch)
    assert sent[0]["status"] == 503 and gated == []
    headers = dict(sent[0]["headers"])
    assert (headers[b"retry-after"], headers[b"x-seriatim-gate"]) == (b"15", b"ungated")
    doc = _error_doc(sent[1]["body"], ErrorCode.NODE_SATURATED, envelope_validator)
    assert doc["retry_after"] == 15
    line = _one_line(capsys)
    assert (line["outcome"], line["slot_end"], line["kind"]) == ("saturated", "none", "ungated")
    assert all_free(gate)


def test_proxy_error_mid_stream_aborts(monkeypatch, capsys):
    sent, gate, _ = _run_proxy(
        lambda r: httpx.Response(200, stream=_Stream([b"a\n"], then=httpx.ReadError("reset"))),
        monkeypatch=monkeypatch,
    )
    assert [m.get("more_body") for m in sent] == [None, True]
    assert _slot_states(gate) == ["draining"]
    line = _one_line(capsys)
    assert (line["outcome"], line["status"]) == ("upstream_error", 200)


def _broken_then(close):
    """An ungated response that breaks mid-stream, so step (a)'s close is the only one: when
    the stream ends normally, httpx's aiter_raw closes it itself (S1-10 covers that, H26)."""
    return lambda r: httpx.Response(
        200, stream=_Stream([b"{"], then=httpx.ReadError("reset"), close=close)
    )


def test_proxy_ungated_close_failure_is_logged(monkeypatch, capsys):
    sent, gate, _ = _run_proxy(
        _broken_then("raise"), method="GET", path=b"/api/tags", monkeypatch=monkeypatch
    )
    assert [m.get("more_body") for m in sent] == [None, True]  # an abort
    line = _one_line(capsys)
    assert (line["level"], line["upstream_close_failed"], line["slot_end"]) == (
        "ERROR",
        True,
        "none",
    )
    assert line["outcome"] == "upstream_error"
    assert all_free(gate)


def test_proxy_ungated_close_that_hangs_is_bounded(monkeypatch, capsys):
    started = time.perf_counter()
    _run_proxy(_broken_then("hang"), method="GET", path=b"/api/tags", monkeypatch=monkeypatch)
    assert time.perf_counter() - started < 0.2 + TIMING_BOUND_S  # close_timeout_s, then on
    line = _one_line(capsys)
    assert (line["level"], line["upstream_close_failed"]) == ("ERROR", True)


def test_proxy_external_cancellation_sends_nothing_and_drains(monkeypatch, capsys):
    sent, gate, gated = _run_proxy(_never_answers, monkeypatch=monkeypatch, cancel_after=0.2)
    assert sent == []
    assert _slot_states(gate) == ["draining"]
    assert gated[0].is_closed  # the shielded finally still proved the close
    line = _one_line(capsys)
    assert (line["outcome"], line["status"], line["slot_end"]) == ("cancelled", None, "draining")


def _direct(proxy, receive, send):
    scope = {"method": "POST", "raw_path": b"/api/chat", "query_string": b"", "headers": []}
    anyio.run(proxy, scope, receive, send, classify("POST", b"/api/chat"), "rid")


def _plain_proxy(gate, handler=_ok):
    settings = make_settings("http://127.0.0.1:1")
    return Proxy(
        settings, gate, _Client(handler), RequestLog("INFO", "node-a"), ErrorFactory("node-a")
    )


def test_proxy_a_failed_final_write_never_changes_the_slot(monkeypatch, capsys):
    monkeypatch.setattr(proxy_module, "new_gated_client", lambda s: _Client(_ok))
    gate, _ = ready_gate()
    sent: list[dict] = []

    async def receive():
        if not sent:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await anyio.sleep(30)

    async def send(message):
        if message.get("more_body") is False:
            raise OSError("the client is gone")
        sent.append(message)

    _direct(_plain_proxy(gate), receive, send)
    assert all_free(gate)
    line = _one_line(capsys)
    assert (line["outcome"], line["slot_end"]) == ("ok", "free")


def test_proxy_client_gone_during_the_body(monkeypatch, capsys):
    monkeypatch.setattr(proxy_module, "new_gated_client", lambda s: _Client(_ok))
    gate, _ = ready_gate()
    sent: list[dict] = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    _direct(_plain_proxy(gate), receive, send)
    assert sent == []  # nobody to answer
    assert all_free(gate)  # nothing was sent upstream: freed with no drain
    line = _one_line(capsys)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "client_disconnect",
        None,
        "free",
    )


def test_watch_disconnect_ignores_other_messages():
    messages = [{"type": "http.request", "body": b""}, {"type": "http.disconnect"}]

    async def main():
        outcome = proxy_module.Outcome()

        async def receive():
            return messages.pop(0)

        with anyio.CancelScope() as scope:
            await proxy_module.watch_disconnect(receive, outcome, scope)
        return outcome, scope

    outcome, scope = anyio.run(main)
    assert outcome.disconnected and scope.cancel_called and messages == []


def test_read_body_joins_chunks_and_sees_a_disconnect():
    async def read(messages):
        async def receive():
            return messages.pop(0)

        return await proxy_module.read_body(receive, limit=3, timeout_s=WAIT_S)

    parts = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "more_body": True},
        {"type": "http.request", "body": b"c"},
    ]
    assert anyio.run(read, parts) == b"abc"
    gone = [{"type": "http.request", "body": b"a", "more_body": True}, {"type": "http.disconnect"}]
    with pytest.raises(proxy_module.ClientGone):
        anyio.run(read, gone)


# Every name on the §4 strip lists, in mixed case. The hop-by-hop headers are on both.
HOP_BY_HOP = [
    b"Connection", b"Keep-Alive", b"Proxy-Authenticate", b"Proxy-Authorization", b"TE",
    b"Trailer", b"Transfer-Encoding", b"Upgrade",
]  # fmt: skip
REQUEST_STRIPPED = [
    *HOP_BY_HOP, b"Host", b"AUTHORIZATION", b"Cookie", b"x-API-key", b"Content-Length",
    b"Accept-Encoding", b"Forwarded", b"X-Forwarded-For", b"X-Forwarded-Host",
    b"X-Forwarded-Proto", b"X-Real-IP", b"X-Request-Id", b"X-SERIATIM-GATE", b"X-Seriatim-Node",
    b"Proxy-Connection", b"PROXY-FOO",
]  # fmt: skip
RESPONSE_STRIPPED = [
    *HOP_BY_HOP, b"Access-Control-Allow-Origin", b"ACCESS-CONTROL-EXPOSE-HEADERS",
    b"X-Seriatim-Gate", b"X-Seriatim-Node", b"X-Resolved-Model", b"X-Model-Digest",
    b"X-Execution-Device", b"X-Rerouted-From", b"X-Reroute-Reason", b"X-Context-Clamped",
    b"Set-Cookie", b"Server", b"X-Request-Id", b"Date",
]  # fmt: skip


@pytest.mark.td("H16")
def test_upstream_headers_exactly():
    client = [
        *[(name, b"v") for name in REQUEST_STRIPPED],
        (b"Authorization", b"Bearer second"),
        (b"X-Custom", b"a"),
        (b"x-custom", b"b"),
        (b"Accept", b"*/*"),
        (b"User-Agent", b"ua"),
        (b"X-Forwarded", b"not a forwarding header"),
        (b"Proxyish", b"not a proxy-* header"),
    ]
    assert proxy_module.upstream_headers(client, body_len=3, request_id="rid") == [
        (b"X-Custom", b"a"),
        (b"x-custom", b"b"),
        (b"Accept", b"*/*"),
        (b"User-Agent", b"ua"),
        (b"X-Forwarded", b"not a forwarding header"),
        (b"Proxyish", b"not a proxy-* header"),
        (b"content-length", b"3"),
        (b"x-request-id", b"rid"),
    ]


@pytest.mark.td("H16")
def test_downstream_headers_exactly():
    upstream = httpx.Headers(
        [
            (b"Content-Type", b"application/json"),
            (b"Content-Length", b"2"),
            (b"Content-Encoding", b"identity"),
            *[(name, b"v") for name in RESPONSE_STRIPPED],
            (b"Link", b"<a>"),
            (b"Link", b"<b>"),
            (b"Cookie", b"kept: a response header nobody sends"),
        ]
    )
    headers = proxy_module.downstream_headers(
        upstream, gate_value="acquired", node_id="node-a", request_id="rid"
    )
    assert headers == [
        (b"Content-Type", b"application/json"),
        (b"Content-Length", b"2"),
        (b"Content-Encoding", b"identity"),
        (b"Link", b"<a>"),
        (b"Link", b"<b>"),
        (b"Cookie", b"kept: a response header nobody sends"),
        (b"x-seriatim-gate", b"acquired"),
        (b"x-seriatim-node", b"node-a"),
        (b"x-request-id", b"rid"),
    ]


@pytest.mark.td("H15")
def test_connection_tokens_are_trimmed_and_lower_case():
    headers = [
        (b"Connection", b"Keep-Alive, X-A ,x-b"),
        (b"connection", b"X-C"),
        (b"X-Connection", b"X-Z"),
    ]
    assert proxy_module.connection_tokens(headers) == {b"keep-alive", b"x-a", b"x-b", b"x-c"}
    named = [*headers, (b"X-A", b"1"), (b"x-B", b"2"), (b"X-C", b"3"), (b"X-Z", b"4")]
    assert proxy_module.upstream_headers(named, body_len=0, request_id="r")[:2] == [
        (b"X-Connection", b"X-Z"),
        (b"X-Z", b"4"),
    ]
    response = httpx.Headers([(b"Connection", b"X-D, x-e"), (b"X-D", b"1"), (b"X-E", b"2")])
    assert proxy_module.downstream_headers(
        response, gate_value="ungated", node_id="n", request_id="r"
    ) == [(b"x-seriatim-gate", b"ungated"), (b"x-seriatim-node", b"n"), (b"x-request-id", b"r")]


def test_httpx_defaults_are_the_ones_pruned():
    """An httpx upgrade that adds a default header fails here, not in production."""
    with httpx.Client() as client:
        assert [k.lower() for k, _ in client.headers.raw] == list(proxy_module.HTTPX_DEFAULTS)


@pytest.mark.td("H3")
@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        ([], []),
        (
            [(b"Accept", b"text/x"), (b"user-agent", b"mine")],
            [(b"accept", b"text/x"), (b"user-agent", b"mine")],
        ),
    ],
)
def test_prune_httpx_defaults(forwarded, expected):
    forwarded = [*forwarded, (b"content-length", b"2"), (b"x-request-id", b"r")]
    # A jar that does store a cookie, so httpx adds a `cookie` header for prune to remove.
    with httpx.Client(base_url="http://127.0.0.1:1", cookies={"s": "1"}) as client:
        req = client.build_request("POST", "/api/chat", headers=forwarded, content=b"{}")
    proxy_module.prune_httpx_defaults(req, forwarded)
    assert sorted((k.lower(), v) for k, v in req.headers.raw) == sorted(
        [
            (b"host", b"127.0.0.1:1"),
            *expected,
            (b"content-length", b"2"),
            (b"x-request-id", b"r"),
        ]
    )


def _pool(client: httpx.AsyncClient):
    return client._transport._pool


@pytest.mark.parametrize(
    ("factory", "connections", "keepalive", "read"),
    [("new_gated_client", 1, 0, None), ("new_ungated_client", 16, 8, 300.0)],
)
def test_proxy_client_settings(factory, connections, keepalive, read):
    settings = make_settings("http://127.0.0.1:11435")
    client = getattr(proxy_module, factory)(settings)
    try:
        pool = _pool(client)
        assert (pool._max_connections, pool._max_keepalive_connections) == (connections, keepalive)
        timeout = client.timeout
        assert (timeout.connect, timeout.pool) == (5.0, 5.0)
        assert (timeout.read, timeout.write) == (read, read)
        assert str(client.base_url) == "http://127.0.0.1:11435/"
        assert client.follow_redirects is False and client._trust_env is False
        assert isinstance(client.cookies.jar, NoCookies)
    finally:
        anyio.run(client.aclose)


def test_create_app_uses_an_injected_shared_client():
    """Ungated routes go through an injected shared client, which the lifespan leaves open."""
    seen = []

    def handler(request):
        seen.append(request.url.raw_path)
        return httpx.Response(200, headers={"content-length": "2"}, stream=_Stream([b"{}"]))

    shared = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:1"
    )
    gate, _ = ready_gate()
    app = create_app(make_settings("http://127.0.0.1:1"), client=shared, gate=gate)
    server = ThreadServer(app, reserve_socket(), lifespan="on").start()
    try:
        resp = httpx.get(f"http://127.0.0.1:{server.port}/api/tags", timeout=WAIT_S)
    finally:
        server.stop()
    assert (resp.status_code, seen) == (200, [b"/api/tags"])
    assert not shared.is_closed
    anyio.run(shared.aclose)


# --- S1-7: the header rules over real sockets (TD §4) ---------------------------------------


def _raw_post(car, header_lines: list[bytes], body: bytes = CHAT, path: bytes = b"/api/chat"):
    """POST with exactly these header lines (no client defaults), on a keep-alive socket."""
    head = b"".join(line + b"\r\n" for line in header_lines)
    request = b"POST %s HTTP/1.1\r\n%sContent-Length: %d\r\n\r\n%s" % (path, head, len(body), body)
    with raw_connect(car.port) as sock:
        sock.sendall(request)
        return read_response(sock)


def _names(headers) -> list[bytes]:
    return [k.lower() for k, _ in headers]


def _wait_log_lines(capsys, n: int) -> list[dict]:
    """The request log is written after the response, from the server thread: wait for it."""
    lines: list[dict] = []

    def enough() -> bool:
        lines.extend(_log_lines(capsys.readouterr().out))
        return len(lines) >= n

    wait_for(enough)
    return lines


@pytest.mark.td("H3")
def test_h3_request_headers_are_stripped(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _raw_post(
        car,
        [
            b"Host: evil",
            b"Authorization: Bearer secret",
            b"X-API-Key: secret",
            b"Cookie: c=1",
            b"Connection: x-foo",
            b"X-Foo: named",
            b"Keep-Alive: timeout=5",
            b"X-Seriatim-Node: forged",
            b"Accept-Encoding: gzip",
            b"X-Custom: kept",
        ],
    )
    assert resp.status == 200
    (req,) = fake_ollama.requests
    # Nothing else: no removed header, and no httpx default (`user-agent`, `accept`,
    # `accept-encoding`, `connection`), since the client sent none.
    assert sorted(_names(req.headers)) == [b"content-length", b"host", b"x-custom", b"x-request-id"]
    headers = dict(req.headers)
    assert headers[b"host"] == f"127.0.0.1:{fake_ollama.server.port}".encode()
    assert headers[b"x-custom"] == b"kept"


@pytest.mark.td("H3")
def test_h3_the_clients_own_accept_and_user_agent_are_forwarded(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    lines = [b"Host: x", b"Accept: application/x-ndjson", b"User-Agent: client/1"]
    assert _raw_post(car, lines).status == 200
    (req,) = fake_ollama.requests
    headers = dict(req.headers)
    assert (headers[b"accept"], headers[b"user-agent"]) == (b"application/x-ndjson", b"client/1")
    assert b"accept-encoding" not in headers and b"connection" not in headers


@pytest.mark.td("H9")
def test_h9_an_ollama_429_passes_through_without_forged_headers(fake_ollama, sidecar):
    body = b'{"error":"server busy"}'
    fake_ollama.script(
        "POST",
        "/api/chat",
        status(
            429,
            body,
            [
                (b"content-type", b"application/json"),
                (b"retry-after", b"3"),
                (b"x-seriatim-gate", b"busy"),
                (b"access-control-allow-origin", b"*"),
                (b"x-rerouted-from", b"x"),
            ],
        ),
    )
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert (resp.status_code, resp.content) == (429, body)
    assert resp.headers.get_list("retry-after") == ["3"]
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]  # never `busy`: no spill-over
    assert "access-control-allow-origin" not in resp.headers
    assert "x-rerouted-from" not in resp.headers
    wait_for(lambda: all_free(gate))  # Ollama's 429 is a clean end


@pytest.mark.td("H11")
@pytest.mark.parametrize(("method", "path"), [("POST", "/api/chat"), ("GET", "/api/tags")])
def test_h11_set_cookie_is_never_relayed_or_replayed(method, path, fake_ollama, sidecar):
    fake_ollama.script(
        method,
        path,
        sequence(status(200, b"{}", [(b"set-cookie", b"s=1")]), status(200, b"{}")),
    )
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    content = CHAT if method == "POST" else None
    with httpx.Client(timeout=WAIT_S) as a:
        resp = a.request(method, car.url + path, content=content)
        assert resp.status_code == 200
        assert "set-cookie" not in resp.headers
        assert not a.cookies
    wait_for(lambda: all_free(gate))
    with httpx.Client(timeout=WAIT_S) as b:  # another client
        assert b.request(method, car.url + path, content=content).status_code == 200
    first, second = fake_ollama.requests
    assert b"cookie" not in _names(first.headers)
    assert b"cookie" not in _names(second.headers)


@pytest.mark.td("H15")
def test_h15_headers_named_in_connection_are_removed_both_ways(fake_ollama, sidecar):
    fake_ollama.script(
        "POST",
        "/api/chat",
        status(
            200,
            b"{}",
            [(b"connection", b"X-D, x-e"), (b"x-d", b"1"), (b"x-e", b"2"), (b"x-keep", b"3")],
        ),
    )
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _raw_post(
        car,
        [
            b"Host: x",
            b"Connection: Keep-Alive, X-A ,x-b",
            b"Connection: X-C",
            b"X-A: 1",
            b"X-B: 2",
            b"X-C: 3",
            b"X-Keep: 4",
        ],
    )
    assert resp.status == 200
    (req,) = fake_ollama.requests
    assert sorted(_names(req.headers)) == [b"content-length", b"host", b"x-keep", b"x-request-id"]
    names = {k for k, _ in resp.headers}
    assert not {"x-d", "x-e"} & names
    assert resp.header("x-keep") == ["3"]


@pytest.mark.td("H16")
def test_h16_the_request_strip_list(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    stripped = [
        b"Proxy-Authorization: Basic x",
        b"Proxy-Connection: keep-alive",
        b"Proxy-Foo: x",
        b"TE: trailers",
        b"Trailer: X-T",
        b"Upgrade: example/1",
        b"X-Real-IP: 192.0.2.7",
        b"X-Forwarded-Host: example.com",
        b"X-Forwarded-Proto: https",
        b"X-Forwarded-For: 192.0.2.7",
        b"Forwarded: for=192.0.2.7",
        b"Authorization: Bearer one",
        b"Authorization: Bearer two",
        b"AUTHORIZATION: Bearer three",
        b"x-API-key: k",
        b"X-SERIATIM-GATE: busy",
    ]
    assert _raw_post(car, [b"Host: x", *stripped, b"X-Keep: 1"]).status == 200
    (req,) = fake_ollama.requests
    assert sorted(_names(req.headers)) == [b"content-length", b"host", b"x-keep", b"x-request-id"]


@pytest.mark.td("H16")
def test_h16_the_response_strip_list(fake_ollama, sidecar):
    forged = [
        (b"proxy-authenticate", b"Basic"),
        (b"server", b"ollama"),
        (b"x-resolved-model", b"m"),
        (b"x-model-digest", b"sha256:00"),
        (b"x-execution-device", b"cpu"),
        (b"x-rerouted-from", b"node-b"),
        (b"x-reroute-reason", b"busy"),
        (b"x-context-clamped", b"true"),
    ]
    stale_date = b"Thu, 01 Jan 1970 00:00:00 GMT"  # Ollama's; uvicorn sends its own
    seriatims = [(b"x-seriatim-node", b"forged"), (b"x-seriatim-gate", b"busy")]
    upstream = [*forged, (b"date", stale_date), *seriatims, (b"x-request-id", b"upstream")]
    upstream.append((b"x-keep", b"1"))
    fake_ollama.script("POST", "/api/chat", status(200, b"{}", upstream))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert resp.status_code == 200
    for name, _ in forged:
        assert name.decode() not in resp.headers
    assert resp.headers.get_list("x-seriatim-node") == ["node-a"]
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    (rid,) = resp.headers.get_list("x-request-id")
    assert HEX32.fullmatch(rid)
    assert resp.headers.get_list("x-keep") == ["1"]
    (date,) = resp.headers.get_list("date")  # exactly one, RFC 9110 §6.6.1
    assert date != stale_date.decode()


@pytest.mark.td("H17")
def test_h17_repeated_headers_keep_every_value_in_order(fake_ollama, sidecar):
    links = [(b"link", b"<a>; rel=x"), (b"link", b"<b>; rel=y")]
    fake_ollama.script("POST", "/api/chat", status(200, b"{}", links))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _raw_post(car, [b"Host: x", b"X-Custom: a", b"X-Custom: b"])
    assert resp.status == 200
    assert resp.header("link") == ["<a>; rel=x", "<b>; rel=y"]
    (req,) = fake_ollama.requests
    assert [v for k, v in req.headers if k == b"x-custom"] == [b"a", b"b"]


@pytest.mark.td("H18")
@pytest.mark.parametrize(
    ("lines", "kept"),
    [
        ([b"X-Request-Id: abc-1.2_3"], "abc-1.2_3"),
        ([b"X-Request-Id: "], None),
        ([b"X-Request-Id: " + b"a" * 129], None),
        ([b"X-Request-Id: a b"], None),
        ([b"X-Request-Id: a/b"], None),
        ([b"X-Request-Id: abc", b"X-Request-Id: abc"], None),
    ],
    ids=["valid", "empty", "129-chars", "space", "slash", "two-headers"],
)
def test_h18_request_id_is_echoed_forwarded_and_logged(lines, kept, fake_ollama, sidecar, capsys):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}", [(b"x-request-id", b"upstream")]))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _raw_post(car, [b"Host: x", *lines])
    assert resp.status == 200
    (rid,) = resp.header("x-request-id")  # only the sidecar's: never Ollama's `upstream`
    if kept is not None:
        assert rid == kept
    else:
        assert HEX32.fullmatch(rid)
    (req,) = fake_ollama.requests
    assert [v for k, v in req.headers if k == b"x-request-id"] == [rid.encode()]
    (line,) = _wait_log_lines(capsys, 1)
    assert line["request_id"] == rid


@pytest.fixture
def recording_proxy():
    """A fake that records every request, standing in for an HTTP proxy."""
    proxy = FakeOllama().start()
    yield proxy
    proxy.stop()


@pytest.mark.td("H20")
def test_h20_proxy_environment_variables_are_ignored(
    fake_ollama, sidecar, recording_proxy, monkeypatch
):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, recording_proxy.url)
        monkeypatch.setenv(name.lower(), recording_proxy.url)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    # The fake proxy works: a client that trusts the environment goes through it.
    httpx.get("http://192.0.2.1/x", timeout=WAIT_S)
    assert len(recording_proxy.requests) == 1
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    fake_ollama.script("GET", "/api/tags", status(200, b"{}"))
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)  # its clients are built with the variables set
    with httpx.Client(timeout=WAIT_S, trust_env=False) as client:
        assert client.post(car.url + "/api/chat", content=CHAT).status_code == 200
        assert client.get(car.url + "/api/tags").status_code == 200
        assert client.get(car.url + "/seriatim/health").status_code == 200
    paths = sorted(r.raw_path for r in fake_ollama.requests)
    assert paths == [b"/api/chat", b"/api/tags", b"/api/version"]
    assert len(recording_proxy.requests) == 1


@pytest.mark.td("H21")
@pytest.mark.parametrize(("method", "path"), [("POST", "/api/chat"), ("GET", "/api/tags")])
def test_h21_a_redirect_is_passed_through_not_followed(method, path, fake_ollama, sidecar):
    location = b"http://192.0.2.1/x"
    fake_ollama.script(method, path, status(302, b"", [(b"location", location)]))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    content = CHAT if method == "POST" else None
    resp = httpx.request(
        method, car.url + path, content=content, timeout=WAIT_S, follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers.get_list("location") == [location.decode()]
    assert len(fake_ollama.requests) == 1


@pytest.mark.td("H24")
@pytest.mark.parametrize(
    "body",
    [
        b'{"a":',
        b'{"model":"a","model":"b"}',
        b"\xff\xfe",
        random.Random(57).randbytes(1024),
    ],
    ids=["truncated", "duplicate-keys", "invalid-utf8", "random-1k"],
)
def test_h24_the_body_is_opaque_bytes(body, fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car, body)
    assert (resp.status_code, resp.content) == (200, b'{"done":true}')  # no error of its own
    (req,) = fake_ollama.requests
    assert req.body == body
    assert [v for k, v in req.headers if k == b"content-length"] == [str(len(body)).encode()]


# --- S1-8: the request limits (TD §3.3 steps 3-4 and step 1, §5; SPEC REQ-20) ---------------

LIMIT = 2048  # max_body_bytes in these tests; the setting's minimum is 1024
TOO_LARGE = "the request body is too large"
TOO_SLOW = "the request body did not arrive in time"


def _framed(
    body: bytes,
    *,
    chunked: bool = False,
    method: bytes = b"POST",
    path: bytes = b"/api/chat",
) -> bytes:
    """A request with `body` framed by an honest `Content-Length`, or chunked in 1000-byte
    chunks with no length."""
    head = b"%s %s HTTP/1.1\r\nHost: x\r\n" % (method, path)
    if not chunked:
        return head + b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
    parts = [body[i : i + 1000] for i in range(0, len(body), 1000)]
    chunks = b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in parts)
    return head + b"Transfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"


def _count_acquires(gate) -> list:
    """Record every `try_acquire` result, so a test can tell 'no slot taken' from 'freed'."""
    results: list = []
    original = gate.try_acquire

    def counted(*args, **kwargs):
        results.append(original(*args, **kwargs))
        return results[-1]

    gate.try_acquire = counted
    return results


def _send_and_read(car, data: bytes) -> RawResponse:
    with raw_connect(car.port) as sock:
        sock.sendall(data)
        return read_response(sock)


def _drip(sock: socket.socket, every_s: float, stop: threading.Event) -> concurrent.futures.Future:
    """Send one body byte every `every_s` seconds until `stop` is set or the socket fails."""

    def run() -> None:
        while not stop.wait(every_s):
            try:
                sock.sendall(b" ")
            except OSError:
                return

    return _in_background(run)


def _refused(resp: RawResponse, status_: int, code: ErrorCode, message: str, gate: str, v) -> None:
    """Every refusal checked here is sent before the request body was fully read, so it closes
    the connection (TD §2.3, S1-8)."""
    assert resp.status == status_
    assert resp.header("connection") == ["close"]
    doc = _error_doc(resp.body, code, v)
    assert doc["error"] == message
    assert (doc["retryable"], doc["replay_safe"]) == (
        ERROR_TABLE[code].retryable,
        ERROR_TABLE[code].replay_safe,
    )
    assert resp.header("x-seriatim-gate") == [gate]


def test_has_body():
    assert not proxy_module.has_body([(b"host", b"x")])
    assert not proxy_module.has_body([(b"content-length", b"0")])
    assert proxy_module.has_body([(b"content-length", b"1")])
    assert proxy_module.has_body([(b"transfer-encoding", b"chunked")])


def test_declared_length():
    assert proxy_module.declared_length([(b"host", b"x")]) is None
    assert proxy_module.declared_length([(b"host", b"x"), (b"content-length", b"0")]) == 0
    assert proxy_module.declared_length([(b"content-length", b"2049")]) == 2049


def test_read_body_limit_and_total_timeout():
    async def read(messages, *, limit=4, timeout_s=WAIT_S):
        async def receive():
            if messages:
                return messages.pop(0)
            await anyio.sleep(30)

        return await proxy_module.read_body(receive, limit=limit, timeout_s=timeout_s)

    def part(body: bytes, more: bool = True) -> dict:
        return {"type": "http.request", "body": body, "more_body": more}

    assert anyio.run(read, [part(b"ab"), part(b"cd", False)]) == b"abcd"  # exactly the limit
    assert anyio.run(read, [part(b"", False)]) == b""
    with pytest.raises(proxy_module.BodyTooLarge):
        anyio.run(read, [part(b"ab"), part(b"cde")])  # never waits for the rest
    started = time.perf_counter()
    with pytest.raises(proxy_module.BodyTimeout):
        anyio.run(lambda: read([part(b"a")], timeout_s=0.2))
    assert 0.2 * 0.9 <= time.perf_counter() - started < 0.2 + TIMING_BOUND_S


@pytest.mark.td("E3")
def test_e3_an_honest_length_over_the_limit_takes_no_slot(fake_ollama, sidecar, envelope_validator):
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    resp = _send_and_read(car, _framed(b"x" * 4096))
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, "ungated", envelope_validator)
    assert acquires == [] and fake_ollama.requests == []


@pytest.mark.td("E3")
def test_e3_a_chunked_body_over_the_limit_frees_the_slot(
    fake_ollama, sidecar, envelope_validator, capsys
):
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    resp = _send_and_read(car, _framed(b"x" * 4096, chunked=True))
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, "acquired", envelope_validator)
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == ("rejected", 413, "free")
    assert len(acquires) == 1 and _slot_states(gate) == ["free"]  # freed with no drain
    assert fake_ollama.requests == []


@pytest.mark.td("E4")
def test_e4_a_stalled_body_gets_408(fake_ollama, sidecar, envelope_validator, capsys):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, body_timeout_s=0.5)
    with raw_connect(car.port) as sock:
        started = time.perf_counter()
        sock.sendall(_framed(b"x" * 100)[:-50])  # the headers and half the body, then a stall
        resp = read_response(sock)
        elapsed = time.perf_counter() - started
    _refused(resp, 408, ErrorCode.REQUEST_BODY_TIMEOUT, TOO_SLOW, "acquired", envelope_validator)
    assert 0.5 * 0.9 <= elapsed < 0.5 + TIMING_BOUND_S
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == ("rejected", 408, "free")
    assert _slot_states(gate) == ["free"]
    assert fake_ollama.requests == []


@pytest.mark.td("E7")
def test_e7_the_client_leaves_during_a_gated_body(fake_ollama, sidecar, capsys):
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(_framed(b"x" * 100)[:-50])
        wait_for(lambda: len(acquires) == 1)  # admitted, and reading the body
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "client_disconnect",
        None,
        "free",
    )
    assert _slot_states(gate) == ["free"]  # no drain: nothing was sent
    assert fake_ollama.requests == []


@pytest.mark.td("E8")
def test_e8_the_body_timeout_is_total(fake_ollama, sidecar, envelope_validator, capsys):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, body_timeout_s=1)
    stop = threading.Event()
    with raw_connect(car.port) as sock:
        started = time.perf_counter()
        sock.sendall(_framed(b"x" * 100)[:-100])  # the headers only
        dripping = _drip(sock, 0.3, stop)  # a byte every 0.3 s: never idle for 1 s
        try:
            resp = read_response(sock)
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            dripping.result(WAIT_S)
    _refused(resp, 408, ErrorCode.REQUEST_BODY_TIMEOUT, TOO_SLOW, "acquired", envelope_validator)
    assert 0.95 <= elapsed < 1 + TIMING_BOUND_S
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("rejected", "free")
    assert _slot_states(gate) == ["free"]
    assert fake_ollama.requests == []


@pytest.mark.td("E9")
def test_e9_the_hold_limit_during_a_gated_body(fake_ollama, sidecar, envelope_validator, capsys):
    gate, _ = ready_gate(max_hold_s=1)  # the fake clock stands still: 1 s of real time
    car = sidecar(fake_ollama.url, gate=gate, body_timeout_s=30)
    stop = threading.Event()
    with raw_connect(car.port) as sock:
        started = time.perf_counter()
        sock.sendall(_framed(b"x" * 100)[:-100])
        dripping = _drip(sock, 0.2, stop)
        try:
            resp = read_response(sock)
            elapsed = time.perf_counter() - started
        finally:
            stop.set()
            dripping.result(WAIT_S)
    _refused(
        resp,
        504,
        ErrorCode.LOCK_HOLD_EXCEEDED,
        "the request exceeded the hold limit",
        "acquired",
        envelope_validator,
    )
    assert elapsed < 1 + TIMING_BOUND_S
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["level"], line["outcome"], line["slot_end"]) == (
        "CRITICAL",
        "hold_exceeded",
        "free",
    )
    assert _slot_states(gate) == ["free"]  # freed with no drain: nothing was sent
    assert fake_ollama.requests == []


@pytest.mark.td("E10")
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
@pytest.mark.parametrize("size", [0, LIMIT])
def test_e10_empty_and_at_the_limit_are_forwarded(size, chunked, fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    body = random.Random(size).randbytes(size)
    resp = _send_and_read(car, _framed(body, chunked=chunked))
    assert (resp.status, resp.body) == (200, b'{"done":true}')
    (req,) = fake_ollama.requests
    assert req.body == body
    assert [v for k, v in req.headers if k == b"content-length"] == [str(size).encode()]


@pytest.mark.td("E10")
@pytest.mark.parametrize(
    ("chunked", "gate_value", "acquired"),
    [(False, "ungated", 0), (True, "acquired", 1)],
    ids=["content-length", "chunked"],
)
def test_e10_one_byte_over_the_limit_is_413(
    chunked, gate_value, acquired, fake_ollama, sidecar, envelope_validator, capsys
):
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    resp = _send_and_read(car, _framed(b"x" * (LIMIT + 1), chunked=chunked))
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, gate_value, envelope_validator)
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"]) == ("rejected", 413)
    assert line["slot_end"] == ("free" if acquired else "none")
    assert len(acquires) == acquired and all_free(gate)
    assert fake_ollama.requests == []


UNGATED_BUFFERED = [(b"POST", b"/api/show"), (b"POST", b"/api/pull"), (b"DELETE", b"/api/delete")]


@pytest.mark.td("E11")
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
@pytest.mark.parametrize(("method", "path"), UNGATED_BUFFERED)
def test_e11_ungated_routes_enforce_the_size_limit(
    method, path, chunked, fake_ollama, sidecar, envelope_validator, capsys
):
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    resp = _send_and_read(car, _framed(b"x" * 4096, chunked=chunked, method=method, path=path))
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, "ungated", envelope_validator)
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["kind"], line["outcome"], line["slot_end"]) == ("ungated", "rejected", "none")
    assert acquires == [] and fake_ollama.requests == []


@pytest.mark.td("E11")
@pytest.mark.parametrize(("method", "path"), UNGATED_BUFFERED)
def test_e11_ungated_routes_enforce_the_body_timeout(
    method, path, fake_ollama, sidecar, envelope_validator
):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, body_timeout_s=0.5)
    resp = _send_and_read(car, _framed(b"x" * 100, method=method, path=path)[:-50])
    _refused(resp, 408, ErrorCode.REQUEST_BODY_TIMEOUT, TOO_SLOW, "ungated", envelope_validator)
    assert fake_ollama.requests == []


def _head(sock: socket.socket) -> bytes:
    """Read up to the end of the first response head, and no further."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(1)
        assert chunk, f"the connection closed before a response: {buf!r}"
        buf += chunk
    return buf


EXPECT_10_MIB = (
    b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 10485760\r\n"
    b"Expect: 100-continue\r\n\r\n"
)


@pytest.mark.td("B6")
def test_b6_an_admitted_request_does_get_100_continue(fake_ollama, sidecar):
    """The contrast that makes the refusals below meaningful: reading the body is what sends
    `100 Continue`."""
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    with raw_connect(car.port) as sock:
        sock.sendall(
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n"
            b"Expect: 100-continue\r\n\r\n"
        )
        assert _head(sock).startswith(b"HTTP/1.1 100 ")
        sock.sendall(b"{}")
        assert read_response(sock).status == 200


@pytest.mark.td("B6")
def test_b6_expect_100_continue_while_busy(fake_ollama, sidecar, envelope_validator):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    with raw_connect(car.port) as sock:
        sock.sendall(EXPECT_10_MIB)
        resp = read_response(sock)  # its first line: no `100 Continue` came before it
    assert resp.status == 429
    _error_doc(resp.body, ErrorCode.GPU_BUSY, envelope_validator)
    assert resp.header("x-seriatim-gate") == ["busy"]
    assert len(fake_ollama.requests) == 1
    hold.release()
    assert held.result(WAIT_S).status_code == 200


@pytest.mark.td("B6")
def test_b6_expect_100_continue_while_starting(fake_ollama, sidecar, envelope_validator):
    car = sidecar(fake_ollama.url, drain_s=600)  # the lifespan's Gate starts in quarantine
    with raw_connect(car.port) as sock:
        sock.sendall(EXPECT_10_MIB)
        resp = read_response(sock)
    assert resp.status == 503
    _error_doc(resp.body, ErrorCode.NODE_STARTING, envelope_validator)
    assert resp.header("x-seriatim-gate") == ["refused"]
    assert fake_ollama.requests == []


def _over_the_limit_headers() -> bytes:
    return b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (LIMIT + 1)


@pytest.mark.td("B7")
def test_b7_413_comes_before_429(fake_ollama, sidecar, envelope_validator):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    held = _in_background(_chat, car)
    hold.wait_entered()
    assert _slot_states(gate) == ["held"]
    resp = _send_and_read(car, _over_the_limit_headers())
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, "ungated", envelope_validator)
    assert len(fake_ollama.requests) == 1
    hold.release()
    assert held.result(WAIT_S).status_code == 200


@pytest.mark.td("B7")
def test_b7_413_comes_before_503_starting(fake_ollama, sidecar, envelope_validator):
    car = sidecar(fake_ollama.url, drain_s=600, max_body_bytes=LIMIT)
    resp = _send_and_read(car, _over_the_limit_headers())
    _refused(resp, 413, ErrorCode.BODY_TOO_LARGE, TOO_LARGE, "ungated", envelope_validator)
    assert car.gate.starting()
    assert fake_ollama.requests == []


HIDDEN = b"GET /api/tags HTTP/1.1\r\nHost: x\r\n\r\n"  # a second request, smuggled after the first


@pytest.mark.td("H28")
@pytest.mark.parametrize(
    "framing",
    [
        b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello!",
        b"Content-Length: 5, 6\r\n\r\nhello!",
        b"Content-Length: -1\r\n\r\n",
        b"Transfer-Encoding: gzip, chunked\r\n\r\n0\r\n\r\n",
    ],
    ids=["two-lengths", "length-list", "negative-length", "gzip-chunked"],
)
def test_h28_h11_refuses_invalid_framing_and_closes(framing, fake_ollama, sidecar):
    """h11 refuses these itself, and uvicorn 0.54 answers every h11 protocol error with its
    own 400 and a close. RFC 9112 §6.1 would allow 501 for `gzip, chunked`; asserting 400
    catches a uvicorn upgrade that changes it."""
    fake_ollama.script("GET", "/api/tags", status(200, b"{}"))
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate)
    request = b"POST /api/chat HTTP/1.1\r\nHost: x\r\n" + framing + HIDDEN
    resp = raw_exchange(car.port, request, timeout=WAIT_S)  # returns once the server closes
    assert resp.status == 400
    assert resp.header("connection") == ["close"]
    assert b"HTTP/1.1" not in resp.body  # one response only: the hidden request got none
    assert acquires == [] and fake_ollama.requests == []


@pytest.mark.td("H28")
@pytest.mark.parametrize(
    ("line", "framing"),
    [
        (b"POST /api/chat", b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n"),
        (b"POST /api/chat", b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n"),
        (b"GET /seriatim/health", b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n"),
        (b"GET /a//b", b"Transfer-Encoding: chunked\r\nContent-Length: 5\r\n"),
    ],
    ids=["te-then-length", "length-then-te", "seriatim-path", "before-the-path-check"],
)
def test_h28_the_sidecar_refuses_te_with_length_and_closes(
    line, framing, fake_ollama, sidecar, envelope_validator, capsys
):
    """h11 accepts this framing (it uses Transfer-Encoding alone, RFC 9112 §6.3) and would
    keep the connection open, serving the hidden request. The dispatcher refuses it and closes
    the connection, as §6.1 requires."""
    fake_ollama.script("GET", "/api/tags", status(200, b"{}"))
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate)
    request = line + b" HTTP/1.1\r\nHost: x\r\n" + framing + b"\r\n0\r\n\r\n" + HIDDEN
    resp = raw_exchange(car.port, request, timeout=WAIT_S)  # returns once the server closes
    _refused(
        resp,
        400,
        ErrorCode.BAD_REQUEST_BODY,
        "Transfer-Encoding and Content-Length together",
        "ungated",
        envelope_validator,
    )
    assert resp.header("connection") == ["close"]
    assert b"HTTP/1.1" not in resp.body  # one response only: the hidden request got none
    (log,) = _wait_log_lines(capsys, 1)
    assert (log["outcome"], log["status"], log["slot_end"]) == ("rejected", 400, "none")
    assert acquires == [] and fake_ollama.requests == []


@pytest.mark.td("H28")
def test_h28_a_chunked_body_with_a_trailer_is_one_request(fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    fake_ollama.script("GET", "/api/tags", status(200, b"{}"))
    gate, _ = ready_gate()
    acquires = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate)
    request = (
        b"POST /api/chat HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"%x\r\n%s\r\n0\r\nX-Trailer: 1\r\n\r\n" % (len(HIDDEN), HIDDEN)
    )
    with raw_connect(car.port) as sock:
        sock.sendall(request)
        received = b""
        while chunk := sock.recv(65536):
            received += chunk
    assert received.count(b"HTTP/1.1 ") == 1 and received.startswith(b"HTTP/1.1 200 ")
    assert received.endswith(b'{"done":true}')
    (req,) = fake_ollama.requests  # the hidden request is only body bytes
    assert (req.method, req.raw_path, req.body) == ("POST", b"/api/chat", HIDDEN)
    assert len(acquires) == 1


def _next_or_closed(sock: socket.socket, request: bytes) -> RawResponse | None:
    """Send `request` on a used socket: its response, or None if the server closed it."""
    try:
        sock.sendall(request)
        if not sock.recv(1, socket.MSG_PEEK):
            return None
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
        return None
    return read_response(sock)


@pytest.mark.td("H29")
def test_h29_after_a_429_the_socket_is_clean(fake_ollama, sidecar):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", sequence(hold, status(200, b'{"done":true}')))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    with raw_connect(car.port) as sock:
        sock.sendall(_framed(b"x" * 4096))  # request 1 and its whole 4 KiB body
        assert read_response(sock).status == 429
        hold.release()
        assert held.result(WAIT_S).status_code == 200
        wait_for(lambda: all_free(gate))
        second = _next_or_closed(sock, _framed(CHAT))
    if second is not None:  # never a 400 from request 1's body parsed as a request
        assert (second.status, second.body) == (200, b'{"done":true}')
    assert [r.body for r in fake_ollama.requests] == [CHAT] * (1 if second is None else 2)


@pytest.mark.td("H29")
@pytest.mark.parametrize("chunked", [False, True], ids=["before-admission", "after-admission"])
def test_h29_after_a_413_the_socket_is_clean(chunked, fake_ollama, sidecar):
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    with raw_connect(car.port) as sock:
        sock.sendall(_framed(b"x" * 4096, chunked=chunked))
        assert read_response(sock).status == 413
        wait_for(lambda: all_free(gate))
        second = _next_or_closed(sock, _framed(CHAT))
    if second is not None:
        assert (second.status, second.body) == (200, b'{"done":true}')
    assert [r.body for r in fake_ollama.requests] == ([] if second is None else [CHAT])


TAGS = b"GET /api/tags HTTP/1.1\r\nHost: x\r\n\r\n"  # the follow-up: ungated, no body


def _follow_up_is_clean(sock: socket.socket, fake_ollama) -> bool:
    """Send `TAGS` on a socket whose first request was refused before its body was read. It
    must be answered as itself, or find the socket closed; it must never be taken as the rest
    of the old body (which would hang until the socket's timeout) or parsed into a 400. Returns
    whether the socket was closed."""
    second = _next_or_closed(sock, TAGS)
    if second is None:
        return True
    assert (second.status, second.body) == (200, b'{"models":[]}')
    return False


@pytest.mark.td("H29")
@pytest.mark.parametrize(
    ("settings", "gate_kwargs", "request_", "status_"),
    [
        ({"body_timeout_s": 0.5}, {}, _framed(b"x" * 100)[:-50], 408),
        (
            {"max_body_bytes": LIMIT},
            {},
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"%x\r\n%s\r\n" % (LIMIT + 1, b"x" * (LIMIT + 1)),
            413,
        ),
        ({"body_timeout_s": 30}, {"max_hold_s": 0.5}, _framed(b"x" * 100)[:-50], 504),
    ],
    ids=["408-stalled", "413-still-streaming", "504-hold-during-the-read"],
)
def test_h29_after_a_refusal_mid_body_the_next_request_is_clean(
    settings, gate_kwargs, request_, status_, fake_ollama, sidecar
):
    """Part of the body is sent, then the refusal comes while the rest is still owed."""
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    gate, _ = ready_gate(**gate_kwargs)
    car = sidecar(fake_ollama.url, gate=gate, **settings)
    with raw_connect(car.port) as sock:
        sock.sendall(request_)
        first = read_response(sock)
        assert first.status == status_ and first.header("connection") == ["close"]
        assert _follow_up_is_clean(sock, fake_ollama)  # closed, as the reply said
    assert fake_ollama.requests == []


@pytest.mark.td("H29")
@pytest.mark.parametrize("refusal", ["413", "429", "503"])
def test_h29_after_a_100_continue_refusal_the_next_request_is_clean(refusal, fake_ollama, sidecar):
    """The client waits for `100 Continue` and never sends the body it declared."""
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    if refusal == "503":
        car = sidecar(fake_ollama.url, drain_s=600)
    else:
        gate, _ = ready_gate()
        car = sidecar(fake_ollama.url, gate=gate, max_body_bytes=LIMIT)
    held = None
    if refusal == "429":
        held = _in_background(_chat, car)
        hold.wait_entered()
    length = LIMIT + 1 if refusal == "413" else LIMIT
    with raw_connect(car.port) as sock:
        sock.sendall(
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n"
            b"Expect: 100-continue\r\n\r\n" % length
        )
        first = read_response(sock)
        assert str(first.status) == refusal and first.header("connection") == ["close"]
        assert _follow_up_is_clean(sock, fake_ollama)
    if held is not None:
        hold.release()
        assert held.result(WAIT_S).status_code == 200
    assert [r.raw_path for r in fake_ollama.requests] == ([b"/api/chat"] if held else [])


@pytest.mark.td("H29")
def test_h29_a_bodyless_refusal_keeps_the_connection(fake_ollama, sidecar):
    """Only a reply that leaves a body unread closes: a 429 to a request with no body keeps the
    connection, and the next request on it is answered."""
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    fake_ollama.script("GET", "/api/tags", status(200, b'{"models":[]}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    held = _in_background(_chat, car)
    hold.wait_entered()
    with raw_connect(car.port) as sock:
        sock.sendall(b"GET /api/chat HTTP/1.1\r\nHost: x\r\n\r\n")
        first = read_response(sock)
        assert first.status == 429 and first.header("connection") == []
        assert not _follow_up_is_clean(sock, fake_ollama)  # answered on the same socket
    hold.release()
    assert held.result(WAIT_S).status_code == 200


# --- S1-9: the end of the response, the in-stream record and internal errors -----------------


@pytest.mark.parametrize(
    ("method", "status_", "headers", "received", "framed"),
    [
        ("HEAD", 200, {"content-length": "1234"}, 0, True),  # the bodyless rule comes first
        ("GET", 100, {}, 0, True),
        ("GET", 199, {}, 0, True),
        ("GET", 204, {}, 0, True),
        ("GET", 304, {}, 0, True),
        ("GET", 200, {}, 0, False),  # close-delimited
        ("GET", 99, {}, 0, False),
        ("GET", 200, {}, 5, False),
        ("GET", 205, {}, 0, False),
        ("GET", 303, {}, 0, False),
        ("GET", 200, {"transfer-encoding": "chunked"}, 7, True),
        ("GET", 200, {"content-length": "5"}, 5, True),
        ("GET", 200, {"content-length": "5"}, 4, False),
        ("GET", 200, {"content-length": "5"}, 6, False),
        ("POST", 200, {"content-length": "0"}, 0, True),
    ],
)  # fmt: skip
def test_upstream_framed(method, status_, headers, received, framed):
    assert proxy_module.upstream_framed(method, status_, httpx.Headers(headers), received) is framed


@pytest.mark.parametrize(
    ("headers", "tail", "framing"),
    [
        ({"content-type": "application/x-ndjson"}, b"", "ndjson"),
        ({"content-type": "application/x-ndjson"}, b"}\n", "ndjson"),
        ({"content-type": "application/x-ndjson"}, b"\n\n", "ndjson"),
        ({"content-type": "application/x-ndjson"}, b"\n}", None),
        ({"content-type": " Application/X-NDJSON ; charset=utf-8"}, b"\n", "ndjson"),
        ({"content-type": "text/event-stream"}, b"\n\n", "sse"),
        ({"content-type": "text/event-stream; charset=utf-8"}, b"", "sse"),
        ({"content-type": "text/event-stream"}, b"x\n", None),
        ({"content-type": "text/event-stream"}, b"\n", None),
        ({"content-type": "application/json"}, b"\n", None),
        ({}, b"", None),
        ({"content-type": "application/x-ndjson", "content-length": "9"}, b"\n", None),
        ({"content-type": "application/x-ndjson", "content-encoding": "gzip"}, b"\n", None),
        ({"content-type": "application/x-ndjson", "content-encoding": " Identity "}, b"\n",
         "ndjson"),
    ],
)  # fmt: skip
def test_record_framing(headers, tail, framing):
    assert proxy_module.record_framing(httpx.Headers(headers), tail) == framing


def test_stream_error_record_exactly():
    record = proxy_module.stream_error_record
    assert record("ndjson", "UPSTREAM_STREAM_BROKEN", 'a "b"') == (
        b'{"error":"a \\"b\\"","error_code":"UPSTREAM_STREAM_BROKEN"}\n'
    )
    assert record("sse", "LOCK_HOLD_EXCEEDED", "x") == (
        b'data: {"error":"x","error_code":"LOCK_HOLD_EXCEEDED"}\n\n'
    )


def _close_delimited(fake, conn):
    fake.read(conn)
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"done":true}')


def _cut_at_50(fake, conn):
    fake.read(conn)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 50)


@pytest.mark.td("H13")
@pytest.mark.parametrize(
    ("handler", "sent"), [(_close_delimited, b'{"done":true}'), (_cut_at_50, b"x" * 50)],
    ids=["close-delimited", "content-length-cut-short"],
)  # fmt: skip
def test_h13_an_unframed_or_cut_short_response_is_broken(
    handler, sent, raw_upstream, sidecar, capsys
):
    upstream = raw_upstream(handler)
    gate, _ = ready_gate()
    car = sidecar(upstream.url, gate=gate)
    got = b""
    headers = {"X-Forwarded-For": "192.0.2.1", "Forwarded": "for=192.0.2.1"}
    with httpx.stream(
        "POST", car.url + "/api/chat", content=CHAT, headers=headers, timeout=WAIT_S
    ) as resp:
        with pytest.raises(httpx.RemoteProtocolError):  # aborted, never a clean end
            for chunk in resp.iter_raw():
                got += chunk
    assert (resp.status_code, got) == (200, sent)  # no record: the framing forbids one
    wait_for(lambda: _slot_states(gate) == ["draining"])
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("upstream_error", "draining")
    (request,) = upstream.requests
    names = [h.split(b":")[0].lower() for h in request.split(b"\r\n\r\n")[0].split(b"\r\n")[1:]]
    assert b"x-forwarded-for" not in names and b"forwarded" not in names


@pytest.mark.td("E5")
def test_e5_an_unexpected_exception_is_a_generic_500(
    fake_ollama, sidecar, envelope_validator, capsys, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("e5-detail-only-in-the-log")

    monkeypatch.setattr(proxy_module, "downstream_headers", broken)
    fake_ollama.script("POST", "/api/chat", status(200, b'{"done":true}'))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert resp.status_code == 500
    doc = _error_doc(resp.content, ErrorCode.INTERNAL, envelope_validator)
    assert doc["error"] == "internal error"
    assert b"e5-detail" not in resp.content and b"Traceback" not in resp.content
    assert len(fake_ollama.requests) == 1
    wait_for(lambda: _slot_states(gate) == ["draining"])  # the request reached upstream
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["level"], line["outcome"], line["slot_end"]) == (
        "ERROR",
        "internal_error",
        "draining",
    )
    assert line["traceback"].startswith("Traceback (most recent call last):\n  File ")
    assert '\n  File "' in line["traceback"].split("\n", 1)[1]  # the frames, line by line
    assert line["traceback"].endswith("RuntimeError: e5-detail-only-in-the-log\n")


@pytest.mark.td("E15")
def test_e15_an_internal_error_before_anything_is_sent(
    fake_ollama, sidecar, envelope_validator, capsys, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(proxy_module, "upstream_headers", broken)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert resp.status_code == 500
    _error_doc(resp.content, ErrorCode.INTERNAL, envelope_validator)
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    assert fake_ollama.requests == []
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("internal_error", "free")
    assert all_free(gate)  # freed with no drain (the fake clock never moved)


@pytest.mark.td("E15")
def test_e15_an_internal_error_after_the_response_started(monkeypatch, capsys):
    def handler(request):
        headers = {"content-type": "application/x-ndjson"}  # a record would be allowed
        return httpx.Response(200, headers=headers, stream=_Stream([b"a\n"], then=KeyError("x")))

    sent, gate, _ = _run_proxy(handler, monkeypatch=monkeypatch)
    assert [m["type"] for m in sent].count("http.response.start") == 1
    assert [(m["type"], m.get("more_body")) for m in sent] == [
        ("http.response.start", None),
        ("http.response.body", True),  # Ollama's line, then an abort: no record, no end
    ]
    assert _slot_states(gate) == ["draining"]
    line = _one_line(capsys)
    assert (line["level"], line["outcome"], line["status"]) == ("ERROR", "internal_error", 200)
    assert "KeyError: 'x'" in line["traceback"]


def test_a_close_delimited_ndjson_stream_ends_with_a_record(monkeypatch, capsys):
    """A close-delimited response is broken even when it ends cleanly, so a record follows."""

    def handler(request):
        headers = {"content-type": "application/x-ndjson"}
        return httpx.Response(200, headers=headers, stream=_Stream([b"a\n"]))

    sent, gate, _ = _run_proxy(handler, monkeypatch=monkeypatch)
    assert [m.get("more_body") for m in sent] == [None, True, True]
    assert json.loads(sent[2]["body"])["error_code"] == "UPSTREAM_STREAM_BROKEN"
    assert _slot_states(gate) == ["draining"]
    line = _one_line(capsys)
    assert (line["outcome"], line["slot_end"]) == ("upstream_error", "draining")
    assert "traceback" not in line


def test_a_length_counts_every_chunk(monkeypatch, capsys):
    """`content-length` is compared with the bytes of all the chunks, not of the last one."""

    def handler(request):
        return httpx.Response(200, headers={"content-length": "4"}, stream=_Stream([b"ab", b"cd"]))

    sent, gate, _ = _run_proxy(handler, monkeypatch=monkeypatch)
    assert [m.get("more_body") for m in sent] == [None, True, True, False]  # a clean end
    assert all_free(gate)
    assert _one_line(capsys)["outcome"] == "ok"


def test_a_cancellation_after_the_headers_never_ends_the_response(monkeypatch, capsys):
    def handler(request):
        headers = {"content-type": "application/x-ndjson"}
        return httpx.Response(200, headers=headers, stream=_Stream([b"a\n"], then="stall"))

    sent, gate, _ = _run_proxy(handler, monkeypatch=monkeypatch, cancel_after=0.2)
    assert [m.get("more_body") for m in sent] == [None, True]  # no record and no final message
    assert _slot_states(gate) == ["draining"]
    assert _one_line(capsys)["outcome"] == "cancelled"


def test_the_hold_limit_record_mid_stream(monkeypatch, capsys):
    gate, _ = ready_gate(max_hold_s=0.2)
    sse = {"content-type": "text/event-stream"}

    def handler(request):
        return httpx.Response(200, headers=sse, stream=_Stream([b"data: a\n\n"], then="stall"))

    sent, _, _ = _run_proxy(handler, gate=gate, monkeypatch=monkeypatch)
    assert [m.get("more_body") for m in sent] == [None, True, True]
    assert sent[2]["body"] == (
        b'data: {"error":"the request exceeded the hold limit",'
        b'"error_code":"LOCK_HOLD_EXCEEDED"}\n\n'
    )
    assert _one_line(capsys)["outcome"] == "hold_exceeded"


# --- S1-10: client construction, the ungated pool and ungated stalls (TD §2.4, §3.3, §5) -----


@pytest.mark.td("E16")
def test_e16_client_construction_fails_after_admission(
    fake_ollama, sidecar, monkeypatch, envelope_validator, capsys
):
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    real = proxy_module.new_gated_client
    calls: list[int] = []

    def fails_once(settings):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("no client")  # right after the slot is taken
        return real(settings)

    ends: list[tuple[object, str, bool]] = []
    finish, stuck = proxy_module.Lease.finish, proxy_module.Lease.stuck

    def finish_(self, *, clean):
        ends.append((self, "finish", finish(self, clean=clean)))
        return ends[-1][2]

    def stuck_(self):
        ends.append((self, "stuck", stuck(self)))
        return ends[-1][2]

    monkeypatch.setattr(proxy_module, "new_gated_client", fails_once)
    monkeypatch.setattr(proxy_module.Lease, "finish", finish_)
    monkeypatch.setattr(proxy_module.Lease, "stuck", stuck_)
    gate, _ = ready_gate()
    acquired = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate)
    resp = _chat(car)
    assert resp.status_code == 500
    _error_doc(resp.content, ErrorCode.INTERNAL, envelope_validator)
    assert resp.json()["error"] == "internal error"
    assert resp.headers.get_list("x-seriatim-gate") == ["acquired"]
    assert fake_ollama.requests == []
    assert all_free(gate)  # nothing was sent: freed with no drain
    assert [(kind, effective) for _, kind, effective in ends] == [("finish", True)]
    assert _chat(car).status_code == 200  # the next request is admitted
    wait_for(lambda: len(ends) == 2)
    assert [lease for lease, _, _ in ends] == acquired  # each lease had exactly one end
    first, _ = _wait_log_lines(capsys, 2)
    assert (first["level"], first["outcome"], first["slot_end"]) == (
        "ERROR",
        "internal_error",
        "free",
    )
    assert "RuntimeError: no client" in first["traceback"]


UNGATED_SLOTS = 16  # the shared client's max_connections (TD §2.4)
# connect_timeout_s, which is also the shared pool's timeout. A loopback connect on a slow
# Windows runner can take a good part of a second, so this leaves room for each connect.
POOL_WAIT_S = 1.0


class _Parked:
    """Holds each request before its headers until `release_all()`. Unlike `Hold`, it polls
    every 50 ms, not every 2 ms, so 16 parked requests put little load on the fake's loop
    (and, through the GIL, on the sidecar's) on a slow runner."""

    def __init__(self) -> None:
        self.entered = 0
        self._go = threading.Event()

    def wait_entered(self, n: int) -> None:
        wait_for(lambda: self.entered >= n)

    def release_all(self) -> None:
        self._go.set()

    async def __call__(self, send, fake, receive=None) -> None:
        self.entered += 1
        while not self._go.is_set() and not fake.release.is_set():
            await anyio.sleep(0.05)
        start = {"type": "http.response.start", "status": 200}
        await send({**start, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"{}"})


def _saturated(fake_ollama, sidecar):
    """Fill the shared pool with 16 parked `/api/show` requests and send a 17th. Returns the
    sidecar, the 17th response, the time it took, the hold and the held requests."""
    hold = _Parked()
    fake_ollama.script("POST", "/api/show", hold)
    fake_ollama.script("POST", "/api/chat", status(200, b"{}"))
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, connect_timeout_s=POOL_WAIT_S)
    show = lambda: httpx.post(car.url + "/api/show", content=b"{}", timeout=WAIT_S)  # noqa: E731
    held: list[concurrent.futures.Future] = []
    for i in range(UNGATED_SLOTS):  # one at a time, so no burst of connects is ever needed
        held.append(_in_background(show))
        try:
            hold.wait_entered(i + 1)
        except AssertionError as exc:  # say what the requests that already ended got
            early = [f.exception() or f.result().status_code for f in held if f.done()]
            raise AssertionError(f"request {i + 1} never reached the fake; ended: {early}") from exc
    started = time.perf_counter()
    try:
        resp = show()
    except httpx.TimeoutException as exc:  # say where the 17th went
        pool = _pool(car.app.client)
        raise AssertionError(
            f"the 17th got no answer in {WAIT_S} s: {hold.entered} reached the fake; the pool "
            f"has {len(pool.connections)} connections and {len(pool._requests)} requests"
        ) from exc
    return car, resp, time.perf_counter() - started, hold, held


@pytest.mark.td("H14")
def test_h14_a_full_ungated_pool_saturates_only_ungated_traffic(
    fake_ollama, sidecar, health_validator, capsys
):
    car, resp, elapsed, hold, held = _saturated(fake_ollama, sidecar)
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "NODE_SATURATED"
    assert POOL_WAIT_S <= elapsed < POOL_WAIT_S + TIMING_BOUND_S  # the pool's own timeout
    assert hold.entered == UNGATED_SLOTS  # the 17th never reached Ollama
    chat = _chat(car)  # gated: its own client, never the shared pool
    assert (chat.status_code, chat.headers["x-seriatim-gate"]) == (200, "acquired")
    assert _health(car, health_validator).status_code == 200  # the health client is its own
    hold.release_all()
    assert [f.result(WAIT_S).status_code for f in held] == [200] * UNGATED_SLOTS
    lines = [line for line in _wait_log_lines(capsys, 18) if line.get("path") == "/api/show"]
    assert sorted(line["outcome"] for line in lines) == ["ok"] * UNGATED_SLOTS + ["saturated"]
    (saturated,) = [line for line in lines if line["outcome"] == "saturated"]
    assert (saturated["status"], saturated["slot_end"], saturated["kind"]) == (
        503,
        "none",
        "ungated",
    )


@pytest.mark.td("H27")
def test_h27_the_node_saturated_response_in_full(fake_ollama, sidecar, envelope_validator):
    car, resp, _, hold, held = _saturated(fake_ollama, sidecar)
    spec = ERROR_TABLE[ErrorCode.NODE_SATURATED]
    assert resp.status_code == spec.status == 503
    doc = _error_doc(resp.content, ErrorCode.NODE_SATURATED, envelope_validator)
    assert (doc["retryable"], doc["replay_safe"]) == (spec.retryable, spec.replay_safe)
    assert doc["replay_safe"] is True
    assert doc["error"] == "the node has no free upstream connection"
    assert resp.headers.get_list("retry-after") == [str(doc["retry_after"])]
    assert doc["retry_after"] == car.settings.retry_after_s
    assert resp.headers.get_list("x-seriatim-gate") == ["ungated"]
    assert resp.headers.get_list("x-seriatim-node") == ["node-a"]
    assert resp.headers["content-type"] == "application/json"
    hold.release_all()
    assert all(f.result(WAIT_S).status_code == 200 for f in held)


UNGATED_READ_S = 0.5  # H26's ungated_read_timeout_s
CLOSE_S = 0.5  # H26's close_timeout_s, where the shared response's aclose() blocks


def _connections_in_use(pool) -> list:
    """The shared pool's connections still in use, and any request still waiting for one."""
    return [c for c in pool.connections if not (c.is_idle() or c.is_closed())] + pool._requests


def _ungated_case(car, capsys, outcome: str) -> dict:
    """The checks every H26 case shares: one log line with `outcome`, and no connection of the
    shared pool still in use."""
    (line,) = [x for x in _wait_log_lines(capsys, 1) if x.get("kind") == "ungated"]
    assert line["outcome"] == outcome and line["slot_end"] == "none"
    assert _connections_in_use(_pool(car.app.client)) == []
    return line


@pytest.mark.td("H26")
def test_h26_a_stall_before_the_headers_is_a_502(fake_ollama, sidecar, envelope_validator, capsys):
    fake_ollama.script("POST", "/api/show", stall_headers())
    gate, _ = ready_gate()
    acquired = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, ungated_read_timeout_s=UNGATED_READ_S)
    started = time.perf_counter()
    resp = httpx.post(car.url + "/api/show", content=b"{}", timeout=WAIT_S)
    elapsed = time.perf_counter() - started
    assert resp.status_code == 502
    _error_doc(resp.content, ErrorCode.UPSTREAM_PROTOCOL_ERROR, envelope_validator)
    assert resp.headers.get_list("x-seriatim-gate") == ["ungated"]
    assert UNGATED_READ_S <= elapsed < UNGATED_READ_S + TIMING_BOUND_S
    _ungated_case(car, capsys, "upstream_error")
    assert acquired == [] and all_free(gate)


@pytest.mark.td("H26")
def test_h26_a_stall_after_the_headers_aborts(fake_ollama, sidecar, capsys):
    fake_ollama.script("POST", "/api/show", stall_body(b'{"a":', 100))
    gate, _ = ready_gate()
    acquired = _count_acquires(gate)
    car = sidecar(fake_ollama.url, gate=gate, ungated_read_timeout_s=UNGATED_READ_S)
    got = b""
    started = time.perf_counter()
    with httpx.stream("POST", car.url + "/api/show", content=b"{}", timeout=WAIT_S) as resp:
        with pytest.raises(httpx.RemoteProtocolError):  # cut short: an abort
            for chunk in resp.iter_raw():
                got += chunk
    elapsed = time.perf_counter() - started
    assert (resp.status_code, got) == (200, b'{"a":')  # a content-length: no record
    assert UNGATED_READ_S <= elapsed < UNGATED_READ_S + TIMING_BOUND_S
    _ungated_case(car, capsys, "upstream_error")
    assert acquired == [] and all_free(gate)


@pytest.mark.td("H26")
def test_h26_a_disconnect_during_an_ungated_stream_closes_upstream(fake_ollama, sidecar, capsys):
    hold = Hold(headers=[(b"content-type", b"application/x-ndjson")], first=b'{"status":"a"}\n')
    fake_ollama.script("POST", "/api/pull", hold)
    gate, _ = ready_gate()
    acquired = _count_acquires(gate)
    # A read timeout far beyond the disconnect, so the disconnect always comes first.
    car = sidecar(fake_ollama.url, gate=gate, ungated_read_timeout_s=WAIT_S)
    with raw_connect(car.port) as sock:
        sock.sendall(b"POST /api/pull HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
        received = b""
        while b'{"status":"a"}' not in received:
            received += sock.recv(65536)
    hold.wait_disconnected()  # the fake sees the upstream close
    _ungated_case(car, capsys, "client_disconnect")
    assert acquired == [] and all_free(gate)


class _HangOnClose(httpx.AsyncByteStream):
    """A response stream whose `aclose()` closes the real stream, then blocks for ever."""

    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self.inner = inner

    async def __aiter__(self):
        async for chunk in self.inner:
            yield chunk

    async def aclose(self) -> None:
        await self.inner.aclose()
        await anyio.sleep_forever()


class _HangingCloses(httpx.AsyncBaseTransport):
    """The shared client's real transport, with every response's `aclose()` blocking."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        resp = await self.inner.handle_async_request(request)
        return httpx.Response(
            resp.status_code,
            headers=resp.headers,
            stream=_HangOnClose(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


async def _chunked_tags(send, fake, receive=None) -> None:
    """A clean, chunked response: the client sees its end only with the final message."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"models":[]}', "more_body": True})
    await send({"type": "http.response.body", "body": b""})


@pytest.mark.td("H26")
@pytest.mark.parametrize("end", ["clean", "stalled"])
def test_h26_a_blocked_close_of_the_shared_response_is_bounded(end, fake_ollama, capsys):
    if end == "clean":
        fake_ollama.script("GET", "/api/tags", _chunked_tags)
    else:
        fake_ollama.script("GET", "/api/tags", stall_body(b'{"a":', 100))
    settings = make_settings(
        fake_ollama.url, ungated_read_timeout_s=UNGATED_READ_S, close_timeout_s=CLOSE_S
    )
    shared = proxy_module.new_ungated_client(settings)
    shared._transport = _HangingCloses(shared._transport)
    gate, _ = ready_gate()
    acquired = _count_acquires(gate)
    server = ThreadServer(
        create_app(settings, client=shared, gate=gate), reserve_socket(), lifespan="on"
    ).start()
    url = f"http://127.0.0.1:{server.port}/api/tags"
    got = b""
    try:
        started = time.perf_counter()
        with httpx.stream("GET", url, timeout=WAIT_S) as resp:
            if end == "clean":
                got = resp.read()  # answered
            else:
                with pytest.raises(httpx.RemoteProtocolError):  # aborted
                    for chunk in resp.iter_raw():
                        got += chunk
        elapsed = time.perf_counter() - started
        (line,) = _wait_log_lines(capsys, 1)
        assert _connections_in_use(shared._transport.inner._pool) == []  # closed under the hang
    finally:
        server.stop()
    fault_s = 0 if end == "clean" else UNGATED_READ_S  # when the exchange ended
    assert got == (b'{"models":[]}' if end == "clean" else b'{"a":')
    # Step (a) gave up on the close after close_timeout_s; the client was then answered or
    # aborted well within 2 x close_timeout_s of the exchange's end.
    assert fault_s + CLOSE_S <= elapsed < fault_s + 2 * CLOSE_S
    assert (line["level"], line["upstream_close_failed"], line["slot_end"]) == (
        "ERROR",
        True,
        "none",
    )
    assert line["outcome"] == ("ok" if end == "clean" else "upstream_error")
    assert acquired == [] and all_free(gate)


# --- S1-11: blob uploads (TD §3.3 step 1, §5) ------------------------------------------------

MIB = 1 << 20
BLOB = b"/api/blobs/sha256:" + b"ab" * 32
INTERRUPTED = "the upload was interrupted"


def _blob_head(length: int | None) -> bytes:
    """A blob upload's request head: an honest `Content-Length`, or chunked when None."""
    framing = b"Transfer-Encoding: chunked" if length is None else b"Content-Length: %d" % length
    return b"POST %s HTTP/1.1\r\nHost: x\r\n%s\r\n\r\n" % (BLOB, framing)


def _chunked(body: bytes, sizes: list[int]) -> bytes:
    """`body` in chunks of these sizes, then the terminating chunk."""
    parts, at = [], 0
    for n in sizes:
        parts.append(b"%x\r\n%s\r\n" % (n, body[at : at + n]))
        at += n
    assert at == len(body)
    return b"".join(parts) + b"0\r\n\r\n"


def _blob_fake(fake_ollama, behaviour=None):
    """The fake in sink mode, answering a stored blob as Ollama does (201)."""
    fake_ollama.sink = True
    fake_ollama.script("POST", BLOB.decode(), behaviour or status(201))
    return fake_ollama


def _upload(fake_ollama):
    """The one upload the fake has seen, once it has arrived."""
    wait_for(lambda: len(fake_ollama.requests) == 1)
    return fake_ollama.requests[0]


class _OneReceiver:
    """Wraps the app's `receive` for each HTTP request (E14). `overlaps` counts every call
    entered while another call for the same request was still pending; `messages` keeps the
    type of each message the proxy got; `entered` is set at the first call."""

    def __init__(self) -> None:
        self.overlaps = 0
        self.messages: list[str] = []
        self.entered = threading.Event()

    def guard(self, receive):
        pending = 0

        async def guarded():
            nonlocal pending
            if pending:
                self.overlaps += 1
            pending += 1
            self.entered.set()
            try:
                msg = await receive()
            finally:
                pending -= 1
            self.messages.append(msg["type"])
            return msg

        return guarded

    def wrap(self, app):
        async def guarded_app(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            return await app(scope, self.guard(receive), send)

        return guarded_app


def test_the_receive_guard_sees_an_overlap():
    """The guard E14 relies on is not vacuous: two pending calls are one overlap."""
    one = _OneReceiver()

    async def slow_receive():
        await anyio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def main():
        guarded = one.guard(slow_receive)
        async with anyio.create_task_group() as tg:
            tg.start_soon(guarded)
            tg.start_soon(guarded)
        await guarded()  # after both: no overlap

    anyio.run(main)
    assert (one.overlaps, one.messages) == (1, ["http.disconnect"] * 3)


def test_streamed():
    assert proxy_module.streamed("POST", BLOB)
    assert not proxy_module.streamed("HEAD", BLOB)  # bodyless, and buffered like the rest
    assert not proxy_module.streamed("POST", BLOB + b"/")
    assert not proxy_module.streamed("POST", b"/api/blobs/sha256:" + b"A" * 64)
    assert not proxy_module.streamed("POST", b"/api/chat")


def test_upstream_headers_for_a_streamed_body():
    """A streamed body keeps the client's validated `Content-Length`, or has none (§4)."""
    rid = (b"x-request-id", b"rid")
    sized = [(b"content-length", b"7"), (b"x-a", b"1")]
    assert proxy_module.upstream_headers(sized, body_len=None, request_id="rid") == [
        (b"x-a", b"1"),
        (b"content-length", b"7"),
        rid,
    ]
    chunked = [(b"transfer-encoding", b"chunked"), (b"x-a", b"1")]
    assert proxy_module.upstream_headers(chunked, body_len=None, request_id="rid") == [
        (b"x-a", b"1"),
        rid,
    ]
    assert proxy_module.upstream_headers(sized, body_len=3, request_id="rid") == [
        (b"x-a", b"1"),
        (b"content-length", b"3"),
        rid,
    ]


def _part(body: bytes, more: bool = True) -> dict:
    return {"type": "http.request", "body": body, "more_body": more}


def _feed(messages: list, *, every_s: float = 0):
    """A `receive` that returns these messages (each after `every_s`), then waits."""

    async def receive():
        if messages:
            await anyio.sleep(every_s)
            return messages.pop(0)
        await anyio.sleep(30)

    return receive


def test_first_chunk():
    async def first(messages, timeout_s=WAIT_S):
        return await proxy_module.first_chunk(_feed(messages), timeout_s=timeout_s)

    assert anyio.run(first, [_part(b""), _part(b"ab"), _part(b"c")]) == (b"ab", True)
    assert anyio.run(first, [_part(b"ab", False)]) == (b"ab", False)
    assert anyio.run(first, [_part(b""), _part(b"", False)]) == (b"", False)  # an empty body
    # ASGI's defaults: a message with no `body` has none, and one with no `more_body` is last.
    assert anyio.run(first, [{"type": "http.request"}]) == (b"", False)
    assert anyio.run(first, [{"type": "http.request", "body": b"a"}]) == (b"a", False)
    with pytest.raises(proxy_module.ClientGone):
        anyio.run(first, [_part(b""), {"type": "http.disconnect"}])
    started = time.perf_counter()
    with pytest.raises(proxy_module.BodyTimeout):
        anyio.run(first, [_part(b"")], 0.2)  # empty chunks are not the first chunk
    assert 0.2 * 0.9 <= time.perf_counter() - started < 0.2 + TIMING_BOUND_S


def _stream(messages, first=b"f", more=True, *, timeout_s=WAIT_S, every_s=0.0):
    """Run `body_stream` to its end; return the events seen, in order, and the error."""
    events: list = []

    async def main():
        stream = proxy_module.body_stream(
            _feed(messages, every_s=every_s),
            first,
            more,
            timeout_s=timeout_s,
            on_end=lambda: events.append("end"),
        )
        try:
            async for chunk in stream:
                events.append(chunk)
        except proxy_module.UploadInterrupted as exc:
            return exc
        return None

    return events, anyio.run(main)


def test_body_stream_forwards_each_chunk_and_ends_once():
    events, error = _stream([_part(b"a"), _part(b""), _part(b"b", False), _part(b"never")])
    assert (events, error) == ([b"f", b"a", "end", b"b"], None)  # `end` before the last chunk
    assert _stream([], b"f", False) == (["end", b"f"], None)  # all of it in the first message
    assert _stream([], b"", False) == (["end"], None)  # an empty body yields nothing
    assert _stream([_part(b"", False)]) == ([b"f", "end"], None)
    # ASGI's defaults, as in `first_chunk`
    assert _stream([{"type": "http.request"}]) == ([b"f", "end"], None)
    assert _stream([{"type": "http.request", "body": b"a"}]) == ([b"f", "end", b"a"], None)


def test_body_stream_interruptions():
    events, error = _stream([_part(b"a"), {"type": "http.disconnect"}])
    assert events == [b"f", b"a"] and error.gone and str(error) == "the client left"
    started = time.perf_counter()
    events, error = _stream([_part(b"a")], timeout_s=0.2)
    assert 0.2 * 0.9 <= time.perf_counter() - started < 0.2 + TIMING_BOUND_S
    assert events == [b"f", b"a"] and not error.gone and str(error) == "the upload stalled"


def test_body_stream_timeout_is_idle_not_total():
    """Four chunks 0.15 s apart take 0.6 s, past the 0.3 s timeout, and still arrive."""
    parts = [_part(b"a"), _part(b"b"), _part(b"c"), _part(b"d", False)]
    events, error = _stream(parts, timeout_s=0.3, every_s=0.15)
    assert (events, error) == ([b"f", b"a", b"b", b"c", "end", b"d"], None)


class _AnswersEarly(httpx.AsyncBaseTransport):
    """Answers before reading any of the request body, as a server may."""

    async def handle_async_request(self, request):
        return httpx.Response(400, stream=_Stream([b"{"], then="stall"))


def test_an_early_answer_closes_and_the_watcher_takes_receive(capsys):
    """Ollama answers before the upload has been read: the reply closes the connection (the
    client's body is still unread, S1-8), and the watcher takes `receive()` over, so a
    disconnect is still seen (§3.3 step 2)."""
    settings = make_settings("http://127.0.0.1:1")
    gate, _ = ready_gate()
    shared = httpx.AsyncClient(transport=_AnswersEarly(), base_url="http://127.0.0.1:1")
    proxy = Proxy(settings, gate, shared, RequestLog("INFO", "node-a"), ErrorFactory("node-a"))
    one = _OneReceiver()
    receive = one.guard(_feed([_part(b"ab"), {"type": "http.disconnect"}]))
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    headers = [(b"content-length", b"4")]
    scope = {"method": "POST", "raw_path": BLOB, "query_string": b"", "headers": headers}
    anyio.run(proxy, scope, receive, send, classify("POST", BLOB), "rid")
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert (sent[0]["status"], dict(sent[0]["headers"])[b"connection"]) == (400, b"close")
    assert (one.messages, one.overlaps) == (["http.request", "http.disconnect"], 0)
    line = _one_line(capsys)
    assert (line["outcome"], line["status"], line["slot_end"]) == ("client_disconnect", 400, "none")


@pytest.mark.td("H10")
def test_h10_a_100_mib_upload_streams_past_the_size_limit(fake_ollama, sidecar, capsys):
    _blob_fake(fake_ollama)
    car = sidecar(fake_ollama.url, max_body_bytes=MIB)
    total = 100 * MIB
    # The limit is in force: a buffered route refuses the same length before admission.
    show = b"POST /api/show HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % total
    assert _send_and_read(car, show).status == 413
    block = random.Random(10).randbytes(MIB)  # allocated before tracing starts
    expected = hashlib.sha256()
    for _ in range(100):
        expected.update(block)
    view = memoryview(block)
    tracemalloc.start()
    try:
        with raw_connect(car.port, timeout=60) as sock:
            sock.sendall(_blob_head(total))
            for _ in range(100):
                sock.sendall(view)
            resp = read_response(sock)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert resp.status == 201
    rec = _upload(fake_ollama)
    assert (rec.length, rec.sha256, rec.complete) == (total, expected.hexdigest(), True)
    assert [v for k, v in rec.headers if k == b"content-length"] == [str(total).encode()]
    assert peak < 16 * MIB, f"traced peak {peak / MIB:.1f} MiB: the upload was buffered"
    lines = _wait_log_lines(capsys, 2)
    assert (lines[0]["outcome"], lines[0]["status"]) == ("rejected", 413)
    assert (lines[1]["kind"], lines[1]["outcome"], lines[1]["status"], lines[1]["slot_end"]) == (
        "ungated",
        "ok",
        201,
        "none",
    )


def _replay_safe(resp: RawResponse) -> bool:
    return json.loads(resp.body)["replay_safe"]


@pytest.mark.td("E6")
def test_e6_a_stall_after_bytes_were_forwarded_is_upload_interrupted(
    fake_ollama, sidecar, envelope_validator, capsys
):
    _blob_fake(fake_ollama)
    car = sidecar(fake_ollama.url, body_timeout_s=0.5)
    with raw_connect(car.port) as sock:
        started = time.perf_counter()
        sock.sendall(_blob_head(2 * MIB) + bytes(MIB))  # half the body, then a stall
        rec = _upload(fake_ollama)
        wait_for(lambda: rec.length == MIB)  # 1 MiB has been forwarded
        forwarded = time.perf_counter()
        resp = read_response(sock)
        done = time.perf_counter()
    _refused(resp, 408, ErrorCode.UPLOAD_INTERRUPTED, INTERRUPTED, "ungated", envelope_validator)
    assert _replay_safe(resp) is False
    assert done - started >= 0.5 * 0.9  # the idle timeout, after the last byte
    assert done - forwarded < 0.5 + TIMING_BOUND_S
    wait_for(lambda: not rec.complete)  # the upstream connection is closed
    assert rec.sha256 is None
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "upload_interrupted",
        408,
        "none",
    )


@pytest.mark.td("E6")
def test_e6_a_stall_before_the_first_chunk_is_replay_safe(
    fake_ollama, sidecar, envelope_validator, capsys
):
    _blob_fake(fake_ollama)
    car = sidecar(fake_ollama.url, body_timeout_s=0.5)
    started = time.perf_counter()
    resp = _send_and_read(car, _blob_head(MIB))  # the headers only
    elapsed = time.perf_counter() - started
    _refused(resp, 408, ErrorCode.REQUEST_BODY_TIMEOUT, TOO_SLOW, "ungated", envelope_validator)
    assert _replay_safe(resp) is True
    assert 0.5 * 0.9 <= elapsed < 0.5 + TIMING_BOUND_S
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == ("rejected", 408, "none")
    assert fake_ollama.requests == []  # Ollama was never contacted


@pytest.mark.td("E12")
def test_e12_the_uploader_leaves_before_the_first_chunk(fake_ollama, sidecar, capsys):
    _blob_fake(fake_ollama)
    one = _OneReceiver()
    car = sidecar(fake_ollama.url, wrap=one.wrap)
    with raw_connect(car.port) as sock:
        sock.sendall(_blob_head(MIB))
        assert one.entered.wait(WAIT_S)  # the proxy is waiting for the first chunk
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "client_disconnect",
        None,
        "none",
    )
    assert one.messages == ["http.disconnect"]
    assert fake_ollama.requests == []  # nothing reached Ollama


@pytest.mark.td("E12")
def test_e12_the_uploader_leaves_after_the_first_chunk(fake_ollama, sidecar, capsys):
    _blob_fake(fake_ollama)
    car = sidecar(fake_ollama.url)
    with raw_connect(car.port) as sock:
        sock.sendall(_blob_head(MIB) + bytes(64 * 1024))
        rec = _upload(fake_ollama)
        wait_for(lambda: rec.length > 0)  # the first chunk was forwarded
    wait_for(lambda: not rec.complete)  # the fake sees the upstream connection close
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"], line["slot_end"]) == (
        "upload_interrupted",
        None,  # nobody to answer
        "none",
    )


@pytest.mark.td("E13")
def test_e13_a_chunked_upload_in_uneven_chunks(fake_ollama, sidecar, capsys):
    _blob_fake(fake_ollama)
    car = sidecar(fake_ollama.url, max_body_bytes=LIMIT)  # no limit applies to a blob
    rng = random.Random(13)
    body = rng.randbytes(3 * MIB)
    sizes: list[int] = []
    while (left := len(body) - sum(sizes)) > 0:
        sizes.append(min(left, rng.choice([1, 7, 4096, 65537, 300_001])))
    resp = _send_and_read(car, _blob_head(None) + _chunked(body, sizes))
    assert resp.status == 201
    rec = _upload(fake_ollama)
    assert (rec.length, rec.sha256) == (len(body), hashlib.sha256(body).hexdigest())
    names = _names(rec.headers)
    assert b"content-length" not in names and b"transfer-encoding" in names  # sent chunked
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"]) == ("ok", 201)


@pytest.mark.td("E13")
def test_e13_an_empty_chunked_upload(fake_ollama, sidecar):
    mismatch = b'{"error":"digest mismatch"}'
    _blob_fake(fake_ollama, status(400, mismatch, [(b"content-type", b"application/json")]))
    car = sidecar(fake_ollama.url)
    resp = _send_and_read(car, _blob_head(None) + b"0\r\n\r\n")
    assert (resp.status, resp.body) == (400, mismatch)  # Ollama's status, passed through
    assert resp.header("connection") == []  # the body was read: keep-alive is safe
    rec = _upload(fake_ollama)
    assert (rec.length, rec.sha256, rec.complete) == (0, hashlib.sha256().hexdigest(), True)


@pytest.mark.td("E14")
@pytest.mark.parametrize("when", ["before", "during", "after"])
def test_e14_one_owner_of_receive_in_a_blob_upload(when, fake_ollama, sidecar, capsys):
    hold = Hold()
    _blob_fake(fake_ollama, hold)
    one = _OneReceiver()
    car = sidecar(fake_ollama.url, wrap=one.wrap)
    body = bytes(64 * 1024)
    with raw_connect(car.port) as sock:
        if when == "before":
            sock.sendall(_blob_head(len(body)))
            assert one.entered.wait(WAIT_S)
        elif when == "during":
            sock.sendall(_blob_head(2 * len(body)) + body)
            wait_for(lambda: fake_ollama.requests and fake_ollama.requests[0].length > 0)
        else:
            sock.sendall(_blob_head(len(body)) + body)
            hold.wait_entered()  # the body has ended; the watcher owns receive
    (line,) = _wait_log_lines(capsys, 1)
    expected = {"before": "client_disconnect", "during": "upload_interrupted"}
    assert line["outcome"] == expected.get(when, "client_disconnect")
    if when == "after":
        hold.wait_disconnected()
    assert one.overlaps == 0
    assert one.messages[-1] == "http.disconnect"  # the proxy saw the disconnect itself


@pytest.mark.td("E14")
def test_e14_one_owner_of_receive_while_waiting_for_headers(fake_ollama, sidecar, capsys):
    hold = Hold()
    fake_ollama.script("POST", "/api/chat", hold)
    one = _OneReceiver()
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, wrap=one.wrap)
    with raw_connect(car.port) as sock:
        sock.sendall(_raw_chat())
        hold.wait_entered()
    hold.wait_disconnected()
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["slot_end"]) == ("client_disconnect", "draining")
    assert one.overlaps == 0
    *body, last = one.messages  # the body read, then the watcher's disconnect
    assert body and set(body) == {"http.request"} and last == "http.disconnect"


@pytest.mark.td("E14")
@pytest.mark.parametrize("path", [BLOB, b"/api/chat"], ids=["blob", "gated"])
def test_e14_one_watcher_on_a_clean_end(path, fake_ollama, sidecar, capsys):
    """With no disconnect at all, the watcher is started once: at the end of the body, and
    not again when Ollama's answer arrives."""
    fake_ollama.script("POST", path.decode(), status(201))
    one = _OneReceiver()
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, wrap=one.wrap)
    head = b"POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (path, len(CHAT))
    assert _send_and_read(car, head + CHAT).status == 201
    (line,) = _wait_log_lines(capsys, 1)
    assert (line["outcome"], line["status"]) == ("ok", 201)
    assert one.overlaps == 0
    assert "http.request" in one.messages
