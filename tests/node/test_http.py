"""HTTP-level tests (TD-slice-1 §8.3).

S1-1: the error table against the contract (K-OAS3). S1-3: the contract against `classify()`
(K-OAS1). S1-5: the dispatcher and health over real sockets (H12, H22, H23, E2, HL1-HL7,
HL9), with the harness in `harness.py`.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import anyio
import httpx
import jsonschema
import pytest
import yaml
from harness import (
    TIMING_BOUND_S,
    FakeOllama,
    all_free,
    drip_body,
    make_settings,
    raw_exchange,
    ready_gate,
    reserve_socket,
    stall_body,
    stall_headers,
    status,
)
from openapi_spec_validator import OpenAPIV31SpecValidator, validate

from seriatim_node.app import (
    ERROR_TABLE,
    ErrorCode,
    ErrorFactory,
    ErrorSpec,
    NoCookies,
    RequestLog,
    create_app,
    main,
    new_health_client,
    request_id_from,
)
from seriatim_node.gate import BLOB_RE, Kind, Route, Scope, classify

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


def test_main_is_a_placeholder_until_s1_12(capsys):
    assert main() == 2
    assert "not implemented until slice 1" in capsys.readouterr().err


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
    ("GET", b"/api/tags", "1.1", ErrorCode.INTERNAL, "passthrough is not available in this build",
     [], "ERROR", "internal_error", {"path": "/api/tags", "kind": "ungated", "scope": "infer",
                                     "known": True}),
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


def test_passthrough_is_refused_until_s1_6(fake_ollama, sidecar, envelope_validator):
    """Until the proxy lands (S1-6), a non-Seriatim route gets 500 and reaches nothing."""
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate)
    for method, path in [("GET", "/api/tags"), ("POST", "/api/chat")]:
        resp = httpx.request(method, car.url + path, content=b"{}")
        assert resp.status_code == 500
        _error_doc(resp.content, ErrorCode.INTERNAL, envelope_validator)
        assert resp.headers["x-seriatim-gate"] == "ungated"
    assert fake_ollama.requests == []
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
    closed = reserve_socket()  # bound, never listening: connections are refused
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
