"""Sidecar authentication over real sockets (TD-slice-2 §8, the http tier): A6 to A11, A13 and A14.

The real sidecar on a loopback port, in front of `fake_ollama` (TD-slice-1 §7), with keys
configured. Keys come from `new_key()`, never from a literal, so nothing that looks like a key
is committed; the http tests' key files are written under `tmp_path` (TD-slice-2 §8). Each
error body is also checked against the contract by `conftest.py`'s K-OAS2 hook.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from harness import (
    WAIT_S,
    FakeClock,
    RawResponse,
    ThreadServer,
    all_free,
    closed_port,
    key_tables,
    log_lines,
    raw_connect,
    raw_exchange,
    read_response,
    ready_gate,
    replay,
    reserve_socket,
    sequence,
    status,
    wait_for,
)

from seriatim_node.app import create_app
from seriatim_node.auth import OTHER_SCHEME, TWO_HEADERS, WHITESPACE, new_key
from seriatim_node.config import load_settings
from seriatim_node.gate import Gate, SlotState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BLOB = "/api/blobs/sha256:" + hashlib.sha256(b"a blob").hexdigest()
CHAT = b'{"model":"m","messages":[]}'
LIMIT = 1024  # max_body_bytes where a test needs a 413: the setting's minimum
VERSION_OK = status(200, b'{"version":"0.40.16"}', [(b"content-type", b"application/json")])
OK = b'{"ok":true}'
HINT = "X-API-Key is not accepted; send Authorization: Bearer <key>"


def _route_pairs() -> dict[str, list[tuple[str, str]]]:
    """SPEC REQ-5's lists (`route-pairs.json`), with a real digest for the blob routes."""
    data = json.loads((FIXTURES / "route-pairs.json").read_text(encoding="utf-8"))
    return {
        cls: [(m, p.replace("/api/blobs/sha256:<64 hex>", BLOB)) for m, p in data[cls]]
        for cls in ("ungated_infer", "ungated_admin", "gated_infer", "gated_admin")
    }


PAIRS = _route_pairs()
UNGATED = {*PAIRS["ungated_infer"], *PAIRS["ungated_admin"]}
INFER_PAIRS = PAIRS["ungated_infer"] + PAIRS["gated_infer"]
UNKNOWN_PAIRS = [("OPTIONS", "/api/tags"), ("PUT", "/api/chat"), ("POST", "/api/new-feature")]
# POST /api/create is the gated_admin class
ADMIN_PAIRS = PAIRS["ungated_admin"] + PAIRS["gated_admin"] + UNKNOWN_PAIRS
# Unknown `/seriatim/*` pairs: `admin` (S2-R3); the admin switch is not served yet
SERIATIM_404 = [
    ("GET", "/seriatim/nope"),
    ("POST", "/seriatim/admin/offline"),
    ("POST", "/seriatim/admin/online"),
    ("GET", "/seriatim/admin/offline"),
    ("GET", "/seriatim/health/"),
]
SERIATIM_405 = [("POST", "/seriatim/health"), ("HEAD", "/seriatim/health")]
# §4's exact-matching cases that are unknown pairs (rule 5), so gated `admin`
UNLISTED = [("GET", "/SERIATIM/HEALTH"), ("GET", "/api/tags/"), ("POST", "/API/chat")]
NEEDS_ADMIN = ADMIN_PAIRS + SERIATIM_404 + SERIATIM_405 + UNLISTED
EVERY_PAIR = INFER_PAIRS + NEEDS_ADMIN


@dataclass(frozen=True)
class Keys:
    infer: str
    admin: str

    @property
    def tables(self) -> list[tuple[str, str, str]]:
        return [("client-a", "infer", self.infer), ("ops", "admin", self.admin)]

    def secrets(self) -> list[str]:
        """Each key, and its SHA-256 (the configured hash) in either case."""
        hexes = [hashlib.sha256(k.encode("ascii")).hexdigest() for k in (self.infer, self.admin)]
        return [self.infer, self.admin, *hexes, *(h.upper() for h in hexes)]


@pytest.fixture
def keys() -> Keys:
    return Keys(new_key(), new_key())


def bearer(key: str) -> tuple[str, str]:
    return ("authorization", f"Bearer {key}")


class Log:
    """Everything written to stdout and stderr since it was made (tracebacks included), and
    the request lines in it."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.text = ""
        capsys.readouterr()

    def lines(self) -> list[dict]:
        out, err = self.capsys.readouterr()
        self.text += out + err
        return log_lines(self.text)

    def line(self, rid: str) -> dict:
        """The one request line for `rid`, waited for: a reply's line follows its write."""
        wait_for(lambda: any(d["request_id"] == rid for d in self.lines()))
        (found,) = [d for d in self.lines() if d["request_id"] == rid]
        return found


def call(
    car,
    method: str,
    path: str,
    rid: str,
    headers: Sequence[tuple[str, str]] = (),
    content: bytes | None = None,
) -> httpx.Response:
    return httpx.request(
        method,
        car.url + path,
        headers=[("x-request-id", rid), *headers],
        content=content,
        timeout=WAIT_S,
    )


def raw(path: bytes, rid: str, head: bytes = b"", *, method: bytes = b"GET", body=b"") -> bytes:
    """A whole request, as bytes, that asks the sidecar to close the connection after it."""
    framing = b"Content-Length: %d\r\n" % len(body) if body or method == b"POST" else b""
    return b"%s %s HTTP/1.1\r\nHost: x\r\nX-Request-Id: %s\r\nConnection: close\r\n%s%s\r\n%s" % (
        method,
        path,
        rid.encode(),
        head,
        framing,
        body,
    )


def headers_of(resp: httpx.Response | RawResponse) -> list[tuple[str, str]]:
    if isinstance(resp, httpx.Response):
        return [(k.lower(), v) for k, v in resp.headers.multi_items()]
    return resp.headers


def values(resp: httpx.Response | RawResponse, name: str) -> list[str]:
    return [v for k, v in headers_of(resp) if k == name]


def body_of(resp: httpx.Response | RawResponse) -> bytes:
    return resp.content if isinstance(resp, httpx.Response) else resp.body


def status_of(resp: httpx.Response | RawResponse) -> int:
    return resp.status_code if isinstance(resp, httpx.Response) else resp.status


def assert_refused(
    resp: httpx.Response | RawResponse,
    line: dict,
    method: str,
    code: str,
    *,
    validator,
    client: str | None = None,
) -> dict | None:
    """An auth refusal (TD-slice-2 §2.4, §5): its status, headers and log line, and, unless it
    answers a HEAD (no body, RFC 9110 §9.3.2), its `ErrorEnvelope`. The line has no
    `error_code`, so `AUTH_MISSING` against `AUTH_INVALID` is told apart by the body only."""
    want = {
        "AUTH_AMBIGUOUS": (400, "rejected"),
        "AUTH_MISSING": (401, "unauthenticated"),
        "AUTH_INVALID": (401, "unauthenticated"),
        "FORBIDDEN_SCOPE": (403, "forbidden"),
    }[code]
    assert status_of(resp) == want[0], (method, line.get("path"), body_of(resp))
    assert values(resp, "www-authenticate") == (["Bearer"] if want[0] == 401 else [])
    assert values(resp, "x-seriatim-gate") == ["ungated"]
    assert values(resp, "x-seriatim-node") == ["node-a"]
    assert (line["status"], line["outcome"], line["level"]) == (*want, "INFO")
    assert line["client"] == client and line["slot_end"] == "none"
    assert "error_code" not in line
    if method == "HEAD":
        assert body_of(resp) == b""
        return None
    doc = json.loads(body_of(resp))
    validator.validate(doc)
    assert doc["error_code"] == code
    assert (doc["retryable"], doc["replay_safe"]) == (False, True)
    assert (doc["source"], doc["node"]) == ("sidecar", "node-a")
    return doc


@pytest.fixture
def envelope(contract_schemas):
    return contract_schemas["ErrorEnvelope"]


def script_every_pair(fake) -> None:
    """Ollama answers every pair a key may pass: 200 with a small body (none to a HEAD)."""
    for method, path in [*EVERY_PAIR]:
        fake.script(method, path, status(200) if method == "HEAD" else status(200, OK))


# --- A6: 401 across every route class ---------------------------------------------------------


@pytest.mark.td("A6")
@pytest.mark.parametrize("credential", ["none", "wrong"])
def test_a6_no_key_or_a_wrong_key_gets_401_on_every_route_class(
    credential, fake_ollama, sidecar, keys, capsys, envelope
):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    sent = [] if credential == "none" else [bearer(new_key())]
    code = "AUTH_MISSING" if credential == "none" else "AUTH_INVALID"
    for i, (method, path) in enumerate(EVERY_PAIR):
        rid = f"a6-{i}"
        resp = call(car, method, path, rid, sent)
        doc = assert_refused(resp, log.line(rid), method, code, validator=envelope)
        if doc is not None:
            assert "hint" not in doc
            assert doc["error"] == (
                "a Bearer key is required" if credential == "none" else "the key is not valid"
            )
    assert fake_ollama.requests == []
    assert all_free(gate)


# --- A7: the scope matrix ---------------------------------------------------------------------


@pytest.mark.td("A7")
def test_a7_an_infer_key_passes_infer_routes_and_gets_403_on_admin_ones(
    fake_ollama, sidecar, keys, capsys, envelope
):
    script_every_pair(fake_ollama)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    for i, (method, path) in enumerate(INFER_PAIRS):
        rid = f"a7-pass-{i}"
        resp = call(car, method, path, rid, [bearer(keys.infer)])
        assert resp.status_code == 200, (method, path)
        gate_value = "ungated" if (method, path) in UNGATED else "acquired"
        assert resp.headers.get_list("x-seriatim-gate") == [gate_value]
        assert resp.content == (b"" if method == "HEAD" else OK)
        line = log.line(rid)
        assert (line["client"], line["outcome"], line["scope"]) == ("client-a", "ok", "infer")
    assert [(r.method, r.raw_path.decode()) for r in fake_ollama.requests] == INFER_PAIRS
    for i, (method, path) in enumerate(NEEDS_ADMIN):
        rid = f"a7-403-{i}"
        resp = call(car, method, path, rid, [bearer(keys.infer)])
        line = log.line(rid)
        doc = assert_refused(
            resp, line, method, "FORBIDDEN_SCOPE", validator=envelope, client="client-a"
        )
        assert line["scope"] == "admin"
        if doc is not None:
            assert doc["error"] == "this route needs the admin scope"
    assert len(fake_ollama.requests) == len(INFER_PAIRS)  # nothing more reached Ollama
    assert all_free(gate)


@pytest.mark.td("A7")
def test_a7_an_admin_key_passes_every_route_class(fake_ollama, sidecar, keys, capsys):
    script_every_pair(fake_ollama)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    forwarded = INFER_PAIRS + ADMIN_PAIRS + UNLISTED  # GET /SERIATIM/HEALTH included
    for i, (method, path) in enumerate(forwarded):
        rid = f"a7-admin-{i}"
        resp = call(car, method, path, rid, [bearer(keys.admin)])
        assert resp.status_code == 200, (method, path)
        gate_value = "ungated" if (method, path) in UNGATED else "acquired"
        assert resp.headers.get_list("x-seriatim-gate") == [gate_value]
        assert log.line(rid)["client"] == "ops"
    assert [(r.method, r.raw_path.decode()) for r in fake_ollama.requests] == forwarded
    for i, (method, path) in enumerate(SERIATIM_404 + SERIATIM_405):
        rid = f"a7-seriatim-{i}"
        resp = call(car, method, path, rid, [bearer(keys.admin)])
        want = 404 if (method, path) in SERIATIM_404 else 405
        assert resp.status_code == want, (method, path)
        assert resp.headers.get_list("allow") == ([] if want == 404 else ["GET"])
        line = log.line(rid)
        assert (line["status"], line["outcome"], line["client"]) == (want, "rejected", "ops")
    assert len(fake_ollama.requests) == len(forwarded)
    assert all_free(gate)


# --- A8: health -------------------------------------------------------------------------------


@pytest.mark.td("A8")
def test_a8_health_reads_no_credentials(fake_ollama, sidecar, keys, capsys):
    """S2-R5: no key, a wrong key, two `Authorization` headers and `Basic x` all get 200, and
    the line stays at DEBUG with `client: null` (TD-slice-1 HL9)."""
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables, log_level="DEBUG")
    log = Log(capsys)
    answers = [
        call(car, "GET", "/seriatim/health", "a8-0"),
        call(car, "GET", "/seriatim/health", "a8-1", [bearer(new_key())]),
        call(car, "GET", "/seriatim/health", "a8-2", [("authorization", "Basic x")]),
        raw_exchange(
            car.port,
            raw(
                b"/seriatim/health",
                "a8-3",
                b"Authorization: Bearer %s\r\nAuthorization: Bearer %s\r\n"
                % (keys.admin.encode(), new_key().encode()),
            ),
        ),
    ]
    for i, resp in enumerate(answers):
        assert (status_of(resp), body_of(resp)) == (200, b'{"status":"ok"}')
        line = log.line(f"a8-{i}")
        assert (line["level"], line["outcome"], line["client"]) == ("DEBUG", "ok", None)


@pytest.mark.td("A8")
def test_a8_degraded_health_with_no_key_is_503_never_401(fake_ollama, sidecar, keys):
    down = sidecar(f"http://127.0.0.1:{closed_port()}", gate=ready_gate()[0], keys=keys.tables)
    resp = httpx.get(down.url + "/seriatim/health", timeout=30)
    assert (resp.status_code, resp.json()) == (
        503,
        {"status": "degraded", "reasons": ["ollama_unreachable"]},
    )
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    quarantined = Gate(1, 15, 10, 1800, clock=FakeClock(), wall=lambda: 1.7e9)
    starting = sidecar(fake_ollama.url, gate=quarantined, keys=keys.tables)
    resp = httpx.get(starting.url + "/seriatim/health", timeout=30)
    assert (resp.status_code, resp.json()) == (503, {"status": "degraded", "reasons": ["starting"]})


@pytest.mark.td("A8")
@pytest.mark.parametrize("method", ["HEAD", "POST"])
def test_a8_other_methods_on_health_need_admin(
    method, fake_ollama, sidecar, keys, capsys, envelope
):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    resp = call(car, method, "/seriatim/health", "a8-none")
    assert_refused(resp, log.line("a8-none"), method, "AUTH_MISSING", validator=envelope)
    resp = call(car, method, "/seriatim/health", "a8-infer", [bearer(keys.infer)])
    line = log.line("a8-infer")
    assert_refused(resp, line, method, "FORBIDDEN_SCOPE", validator=envelope, client="client-a")
    resp = call(car, method, "/seriatim/health", "a8-admin", [bearer(keys.admin)])
    assert (resp.status_code, resp.headers.get_list("allow")) == (405, ["GET"])
    assert fake_ollama.requests == [] and all_free(gate)


# --- A9: ambiguous credentials, and the places a key is never read --------------------------


def _ambiguous(keys: Keys) -> list[tuple[bytes, str, list[str]]]:
    """(the `Authorization` lines, the reason, the values that must not be echoed)."""
    basic = base64.b64encode(f"user:{keys.admin}".encode()).decode()
    wrong = new_key()
    return [
        (
            f"Authorization: Bearer {keys.admin}\r\nAuthorization: Bearer {wrong}\r\n".encode(),
            TWO_HEADERS,
            [keys.admin, wrong],
        ),
        (f"Authorization: Basic {basic}\r\n".encode(), OTHER_SCHEME, [basic, keys.admin]),
        (f"Authorization: Bearer {keys.admin} x\r\n".encode(), WHITESPACE, [keys.admin]),
        (b"Authorization: Bearer a, Bearer b\r\n", WHITESPACE, ["Bearer a", "Bearer b"]),
    ]


@pytest.mark.td("A9")
def test_a9_ambiguous_credentials_get_400(fake_ollama, sidecar, keys, capsys, envelope):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    routes = [
        (b"POST", b"/api/chat", CHAT),
        (b"GET", b"/api/tags", b""),
        (b"GET", b"/seriatim/nope", b""),
    ]
    n = 0
    for head, reason, hidden in _ambiguous(keys):
        for method, path, body in routes:
            rid = f"a9-{n}"
            n += 1
            resp = raw_exchange(car.port, raw(path, rid, head, method=method, body=body))
            doc = assert_refused(
                resp, log.line(rid), method.decode(), "AUTH_AMBIGUOUS", validator=envelope
            )
            assert doc is not None and doc["error"] == f"ambiguous credentials: {reason}"
            for value in hidden:
                assert value.encode() not in resp.body
    assert fake_ollama.requests == []
    assert all_free(gate)


@pytest.mark.td("A9")
def test_a9_x_api_key_is_never_a_credential(fake_ollama, sidecar, keys, capsys, envelope):
    """Q11: `X-API-Key` alone gets 401 with the hint; beside a valid Bearer key it passes, and
    Ollama receives neither header."""
    fake_ollama.script("GET", "/api/tags", status(200, OK))
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    log = Log(capsys)
    api_key = ("x-api-key", keys.infer)
    resp = call(car, "GET", "/api/tags", "a9-hint", [api_key])
    doc = assert_refused(resp, log.line("a9-hint"), "GET", "AUTH_MISSING", validator=envelope)
    assert doc is not None and doc["hint"] == HINT
    assert fake_ollama.requests == []
    resp = call(car, "GET", "/api/tags", "a9-both", [api_key, bearer(keys.infer)])
    assert resp.status_code == 200 and log.line("a9-both")["client"] == "client-a"
    (received,) = fake_ollama.requests
    names = {k.lower() for k, _ in received.headers}
    assert b"authorization" not in names and b"x-api-key" not in names


@pytest.mark.td("A9")
def test_a9_a_key_anywhere_but_authorization_is_missing(
    fake_ollama, sidecar, keys, capsys, envelope
):
    """S2-R6: the query string, `Cookie` and `Proxy-Authorization` are never read."""
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    log = Log(capsys)
    cases = [
        (f"/api/tags?key={keys.infer}", []),
        ("/api/tags", [("cookie", f"key={keys.infer}")]),
        ("/api/tags", [("proxy-authorization", f"Bearer {keys.infer}")]),
    ]
    for i, (path, sent) in enumerate(cases):
        resp = call(car, "GET", path, f"a9-{i}", sent)
        doc = assert_refused(resp, log.line(f"a9-{i}"), "GET", "AUTH_MISSING", validator=envelope)
        assert doc is not None and "hint" not in doc
    assert fake_ollama.requests == []
    assert all_free(gate)


# --- A10: auth comes first --------------------------------------------------------------------


@pytest.mark.td("A10")
def test_a10_auth_comes_before_the_gate_and_the_body_limit(fake_ollama, sidecar, keys, capsys):
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables, max_body_bytes=LIMIT)
    log = Log(capsys)
    holder = gate.try_acquire("/api/chat", "holder")  # the only slot is held: busy
    before = gate.slots()
    resp = call(car, "POST", "/api/chat", "a10-busy", content=b"")
    assert resp.status_code == 401 and log.line("a10-busy")["outcome"] == "unauthenticated"
    assert gate.slots() == before and before[0].holder.request_id == "holder"
    assert holder.finish(clean=True)
    # over the body limit: the 401 comes before the 413, and before any body byte is sent
    with raw_connect(car.port) as sock:
        sock.sendall(
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nX-Request-Id: a10-big\r\n"
            b"Content-Length: 4096\r\n\r\n"
        )
        resp = read_response(sock)
        assert sock.recv(65536) == b""  # the sidecar closed the connection
    assert resp.status == 401 and resp.header("connection") == ["close"]
    with raw_connect(car.port) as sock:  # no 100 Continue: the body is never asked for
        sock.sendall(
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nX-Request-Id: a10-expect\r\n"
            b"Expect: 100-continue\r\nContent-Length: 2\r\n\r\n"
        )
        resp = read_response(sock)
        assert resp.status == 401 and resp.header("connection") == ["close"]
    assert fake_ollama.requests == [] and all_free(gate)


@pytest.mark.td("A10")
def test_a10_auth_comes_before_the_startup_quarantine(fake_ollama, sidecar, keys):
    gate = Gate(1, 15, 10, 1800, clock=FakeClock(), wall=lambda: 1.7e9)
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables)
    resp = call(car, "POST", "/api/chat", "a10-starting", content=b"")
    assert resp.status_code == 401 and resp.json()["error_code"] == "AUTH_MISSING"
    assert gate.starting()  # unchanged: no slot was asked for
    assert [s.state for s in gate.slots()] == [SlotState.DRAINING]
    assert fake_ollama.requests == []


@pytest.mark.td("A10")
def test_a10_a_401_without_a_body_keeps_the_connection(fake_ollama, sidecar, keys):
    fake_ollama.script("GET", "/api/tags", status(200, OK))
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    with raw_connect(car.port) as sock:
        sock.sendall(b"GET /api/tags HTTP/1.1\r\nHost: x\r\n\r\n")
        refused = read_response(sock)
        sock.sendall(
            b"GET /api/tags HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n\r\n"
            % keys.infer.encode()
        )
        passed = read_response(sock)
    assert (refused.status, refused.header("connection")) == (401, [])
    assert (passed.status, passed.body) == (200, OK)


@pytest.mark.td("A10")
@pytest.mark.parametrize(
    ("request_bytes", "want"),
    [
        (b"GET /api/tags HTTP/1.0\r\nHost: x\r\n\r\n", 505),
        (b"POST /api/../chat HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}", 400),
        (
            b"POST /api/chat HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Length: 2\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
            400,
        ),
    ],
    ids=["505", "BAD_PATH", "BAD_REQUEST_BODY"],
)
def test_a10_s1s_earlier_checks_still_win(request_bytes, want, fake_ollama, sidecar, keys):
    """Q5: the HTTP version, the framing and the path are checked before auth."""
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    resp = raw_exchange(car.port, request_bytes)
    code = {505: "HTTP_VERSION_UNSUPPORTED"}.get(want)
    doc = json.loads(resp.body)
    assert resp.status == want and resp.header("www-authenticate") == []
    assert doc["error_code"] in ({code} if code else {"BAD_PATH", "BAD_REQUEST_BODY"})
    assert fake_ollama.requests == []


# --- A11: the key never leaves ----------------------------------------------------------------

EXTRA_SECRETS = ("x-api-key", "cookie", "proxy-authorization")
CREDENTIAL_HEADERS = {b"authorization", b"x-api-key", b"cookie", b"proxy-authorization"}


@pytest.mark.td("A11")
def test_a11_ollama_receives_no_credential(fake_ollama, sidecar, keys):
    fake_ollama.script(
        "POST", "/api/chat", sequence(replay("chat.nostream"), replay("chat.stream"))
    )
    fake_ollama.script("GET", "/api/tags", replay("tags"))
    fake_ollama.script("POST", BLOB, status(201))
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    extra = [(name, new_key()) for name in EXTRA_SECRETS]

    def blob() -> Iterator[bytes]:
        yield b"x" * 100
        yield b"y" * 100

    resp = call(car, "POST", "/api/chat", "a11-1", [bearer(keys.infer), *extra], CHAT)
    assert resp.status_code == 200
    with httpx.stream(
        "POST",
        car.url + "/api/chat",
        headers=[bearer(keys.infer), *extra],
        content=CHAT,
        timeout=WAIT_S,
    ) as streamed:
        assert streamed.status_code == 200 and b"".join(streamed.iter_raw())
    assert call(car, "GET", "/api/tags", "a11-3", [bearer(keys.infer), *extra]).status_code == 200
    resp = httpx.post(
        car.url + BLOB, headers=[bearer(keys.admin), *extra], content=blob(), timeout=WAIT_S
    )
    assert resp.status_code == 201
    assert len(fake_ollama.requests) == 4
    for received in fake_ollama.requests:
        assert not {k.lower() for k, _ in received.headers} & CREDENTIAL_HEADERS
    assert fake_ollama.requests[-1].body == b"x" * 100 + b"y" * 100


@pytest.mark.td("A11")
def test_a11_no_key_or_hash_in_any_log_line_or_response(fake_ollama, sidecar, keys, capsys):
    """One request of each outcome S2 can reach: neither key, nor either configured hash, is in
    a log line, a response body or a response header. `client` is the key's id on the
    authenticated lines (the 403 included) and null on the 400 and 401 lines."""
    fake_ollama.script("GET", "/api/tags", status(200, OK))
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables, max_body_bytes=LIMIT)
    down = sidecar(f"http://127.0.0.1:{closed_port()}", gate=ready_gate()[0], keys=keys.tables)
    log = Log(capsys)
    two = b"Authorization: Bearer %s\r\nAuthorization: Bearer %s\r\n" % (
        keys.infer.encode(),
        keys.admin.encode(),
    )
    big = b"Authorization: Bearer %s\r\n" % keys.admin.encode()
    answers = {
        "a11-200": call(car, "GET", "/api/tags", "a11-200", [bearer(keys.infer)]),
        "a11-400": raw_exchange(car.port, raw(b"/api/tags", "a11-400", two)),
        "a11-401m": call(car, "GET", "/api/tags", "a11-401m"),
        "a11-401i": call(car, "GET", "/api/tags", "a11-401i", [bearer(keys.infer + "x")]),
        "a11-403": call(car, "POST", "/api/pull", "a11-403", [bearer(keys.infer)], b""),
        "a11-404": call(car, "GET", "/seriatim/nope", "a11-404", [bearer(keys.admin)]),
        "a11-413": raw_exchange(
            car.port, raw(b"/api/chat", "a11-413", big, method=b"POST", body=b"x" * 4096)
        ),
    }
    lease = gate.try_acquire("/api/chat", "holder")
    answers["a11-429"] = call(car, "POST", "/api/chat", "a11-429", [bearer(keys.infer)], b"")
    assert lease.finish(clean=True)
    answers["a11-502"] = call(down, "POST", "/api/chat", "a11-502", [bearer(keys.infer)], CHAT)
    clients = {"a11-400": None, "a11-401m": None, "a11-401i": None, "a11-403": "client-a"}
    clients |= {"a11-200": "client-a", "a11-404": "ops", "a11-413": "ops"}
    clients |= {"a11-429": "client-a", "a11-502": "client-a"}
    outcomes = {"a11-400": "rejected", "a11-401m": "unauthenticated"}
    outcomes |= {"a11-401i": "unauthenticated", "a11-403": "forbidden"}
    for rid, resp in answers.items():
        assert status_of(resp) == int(rid[4:7]), rid
        line = log.line(rid)
        assert line["client"] == clients[rid], rid
        if rid in outcomes:
            assert (line["outcome"], line["level"]) == (outcomes[rid], "INFO")
        exposed = body_of(resp).decode("latin-1") + repr(headers_of(resp))
        for secret in keys.secrets():
            assert secret not in exposed, rid
    log.lines()
    for secret in keys.secrets():
        assert secret not in log.text


@pytest.mark.td("A11")
def test_a11_refused_or_ignored_secrets_are_never_logged_or_echoed(
    fake_ollama, sidecar, keys, capsys, caplog
):
    fake_ollama.script("GET", "/api/tags", status(200, OK))
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    log = Log(capsys)
    marks = [new_key() for _ in range(7)]
    with caplog.at_level(logging.DEBUG):
        answers = [
            call(car, "GET", "/api/tags", "a11-0", [bearer(marks[0])]),
            raw_exchange(
                car.port,
                raw(b"/api/tags", "a11-1", b"Authorization: Basic %s\r\n" % marks[1].encode()),
            ),
            raw_exchange(
                car.port,
                raw(b"/api/tags", "a11-2", b"Authorization: Bearer %s x\r\n" % marks[2].encode()),
            ),
            call(car, "GET", "/api/tags", "a11-3", [("x-api-key", marks[3])]),
            call(car, "GET", "/api/tags", "a11-4", [("cookie", f"key={marks[4]}")]),
            call(car, "GET", f"/api/tags?key={marks[5]}", "a11-5"),
            call(car, "GET", f"/api/tags?key={marks[6]}", "a11-6", [bearer(keys.infer)]),
        ]
        statuses = [status_of(resp) for resp in answers]
        assert statuses == [401, 400, 400, 401, 401, 401, 200]
        for i in range(7):
            log.line(f"a11-{i}")
    (received,) = fake_ollama.requests  # REQ-6: the query string is forwarded unchanged
    assert received.query == f"key={marks[6]}".encode()
    for resp in answers:
        exposed = body_of(resp).decode("latin-1") + repr(headers_of(resp))
        assert not [m for m in marks if m in exposed]
    log.lines()
    assert not [m for m in marks if m in log.text or m in caplog.text]
    assert [r for r in caplog.records if r.name == "uvicorn.access"] == []  # L3: it stays off


# --- A13: rotation ----------------------------------------------------------------------------


@pytest.mark.td("A13")
def test_a13_rotation(fake_ollama, tmp_path, capsys):
    """SPEC §6.2: two keys valid at once; one removed and the sidecar restarted from the edited
    file → that one gets 401 `AUTH_INVALID`, the other still 200."""
    fake_ollama.script("GET", "/api/tags", status(200, OK))
    old, new = new_key(), new_key()
    path = tmp_path / "node.toml"
    env = {
        "SERIATIM_NODE_OLLAMA_URL": fake_ollama.url,
        "SERIATIM_NODE_ID": "node-a",
        "SERIATIM_NODE_CONFIG": str(path),
    }

    def serve() -> ThreadServer:
        app = create_app(load_settings(env), gate=ready_gate()[0])
        return ThreadServer(app, reserve_socket(), lifespan="on").start()

    log = Log(capsys)
    path.write_text(key_tables([("old", "infer", old), ("new", "infer", new)]), encoding="utf-8")
    server = serve()
    try:
        url = f"http://127.0.0.1:{server.port}/api/tags"
        for key, rid in [(old, "a13-old"), (new, "a13-new")]:
            resp = httpx.get(url, headers=[bearer(key), ("x-request-id", rid)], timeout=WAIT_S)
            assert resp.status_code == 200
        assert (log.line("a13-old")["client"], log.line("a13-new")["client"]) == ("old", "new")
    finally:
        server.stop()
    path.write_text(key_tables([("new", "infer", new)]), encoding="utf-8")
    server = serve()
    try:
        url = f"http://127.0.0.1:{server.port}/api/tags"
        resp = httpx.get(url, headers=[bearer(old)], timeout=WAIT_S)
        assert (resp.status_code, resp.json()["error_code"]) == (401, "AUTH_INVALID")
        assert httpx.get(url, headers=[bearer(new)], timeout=WAIT_S).status_code == 200
    finally:
        server.stop()


# --- A14: no CORS on any response -------------------------------------------------------------

ORIGIN = ("origin", "http://example.com")
CORS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-allow-credentials", b"true"),
    (b"access-control-expose-headers", b"x"),
    (b"x-upstream-mark", b"kept"),
]


def no_cors(resp: httpx.Response | RawResponse) -> bool:
    return not [k for k, _ in headers_of(resp) if k.startswith("access-control-")]


@pytest.mark.td("A14")
def test_a14_ollamas_cors_headers_never_reach_the_client(fake_ollama, sidecar, keys):
    fake_ollama.script("GET", "/api/tags", status(200, OK, CORS))
    fake_ollama.script("POST", "/api/chat", status(200, OK, CORS))
    car = sidecar(fake_ollama.url, gate=ready_gate()[0], keys=keys.tables)
    for method, path in [("GET", "/api/tags"), ("POST", "/api/chat")]:
        resp = call(
            car,
            method,
            path,
            "a14",
            [ORIGIN, bearer(keys.infer)],
            CHAT if method == "POST" else None,
        )
        assert resp.status_code == 200 and no_cors(resp)
        assert resp.headers.get_list("x-upstream-mark") == ["kept"]  # Ollama's others are kept


@pytest.mark.td("A14")
def test_a14_no_seriatim_response_has_a_cors_header(fake_ollama, sidecar, keys):
    fake_ollama.script("GET", "/api/version", VERSION_OK)
    gate, _ = ready_gate()
    car = sidecar(fake_ollama.url, gate=gate, keys=keys.tables, max_body_bytes=LIMIT)
    quarantined = Gate(1, 15, 10, 1800, clock=FakeClock(), wall=lambda: 1.7e9)
    starting = sidecar(fake_ollama.url, gate=quarantined, keys=keys.tables)
    origin = b"Origin: http://example.com\r\n"
    infer, admin = [ORIGIN, bearer(keys.infer)], [ORIGIN, bearer(keys.admin)]
    preflight = [ORIGIN, ("access-control-request-method", "POST")]
    answers = [
        (
            400,
            raw_exchange(
                car.port, raw(b"/api/tags", "a14", origin + b"Authorization: Basic x\r\n")
            ),
        ),
        (401, call(car, "GET", "/api/tags", "a14", [ORIGIN])),
        (401, call(car, "GET", "/api/tags", "a14", [ORIGIN, bearer(new_key())])),
        (403, call(car, "POST", "/api/pull", "a14", infer, b"")),
        (401, call(car, "OPTIONS", "/api/chat", "a14", preflight)),
        (200, call(car, "GET", "/seriatim/health", "a14", [ORIGIN])),
        (503, call(starting, "GET", "/seriatim/health", "a14", [ORIGIN])),
        (404, call(car, "GET", "/seriatim/nope", "a14", admin)),
        (405, call(car, "POST", "/seriatim/health", "a14", admin)),
        (
            413,
            raw_exchange(
                car.port,
                raw(
                    b"/api/chat",
                    "a14",
                    origin + b"Authorization: Bearer %s\r\n" % keys.admin.encode(),
                    method=b"POST",
                    body=b"x" * 4096,
                ),
            ),
        ),
        (503, call(starting, "POST", "/api/chat", "a14", infer, b"")),
    ]
    lease = gate.try_acquire("/api/chat", "holder")
    answers.append((429, call(car, "POST", "/api/chat", "a14", infer, b"")))
    assert lease.finish(clean=True)
    for want, resp in answers:
        assert status_of(resp) == want and no_cors(resp), (want, headers_of(resp))
