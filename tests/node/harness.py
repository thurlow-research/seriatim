"""The real-socket test harness (TD-slice-1 §7). `conftest.py` exposes it as fixtures.

- `FakeOllama`: a real uvicorn server on 127.0.0.1 running a small ASGI app that each test
  scripts per route. Every request is recorded with its raw method, path, query, headers and
  body.
- `RawUpstream`: a plain socket server for the faults an ASGI app can't produce.
- `start_sidecar`: runs `create_app(settings)` under a real uvicorn server (h11) on
  127.0.0.1, with `ollama_url` pointing at the fake (or at a port nothing listens on).
- `start_process`: runs `python -m seriatim_node` as a subprocess (`test_process.py`).
- `ContractBodies` and `check_contract_bodies`: every error body, in-stream record and health
  body the sidecar makes in a test, checked against the contract (K-OAS2; `conftest.py`).

Servers run in their own thread and event loop, so tests stay synchronous and work the same
on Linux, macOS and Windows. Every socket is bound by the harness to an ephemeral port before
the server starts, so a test always knows the port, and a port can be held closed ("Ollama is
down") and started later on the same socket.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx
import jsonschema
import uvicorn
import yaml

from seriatim_node.app import create_app
from seriatim_node.config import Settings, load_settings
from seriatim_node.gate import Gate, SlotState

LOOPBACK = "127.0.0.1"
# TD §7 "Timing": an elapsed-time assertion may overrun its target by this much.
TIMING_BOUND_S = 0.5 if os.environ.get("CI") == "true" else 0.1
STALL_LIMIT_S = 30.0  # a stalled fake gives up after this, so teardown can never hang
WAIT_S = 5.0  # TD §7: tests wait on events for at most this long, so a hang fails the test
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ollama-0.40.16.json"
# Resolved from this file, so the same path works when mutmut copies tests/ and contract/ into
# node/ (tools/ci/mutation.py).
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "contract" / "openapi.yaml"


def refused_post(url: str, **kw: Any) -> httpx.Response:
    """A `POST` the sidecar will refuse before admission (429 or 503), sent with no body.

    A refusal sent before a declared body is read carries `Connection: close` (S1-8), and the
    body is never read (B3). httpx writes the body after the headers, so its bytes can reach
    the socket after uvicorn's last read; uvicorn then closes with them unread, and the OS
    answers that close with a reset. Windows drops the received but unread response when the
    reset arrives (`WinError 10053` on the next read), where Linux still delivers it first.
    A test that needs only the refusal sends no body; the close itself is tested with raw
    sockets, which write the whole request at once."""
    return httpx.post(url, content=b"", timeout=WAIT_S, **kw)


def wait_for(predicate: Callable[[], object], timeout: float = WAIT_S) -> None:
    """Poll until `predicate()` is true; fail (never pass) on a timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for a condition")
        time.sleep(0.002)


def reserve_socket() -> socket.socket:
    """A TCP socket bound to an ephemeral loopback port, not yet listening, so a server can be
    started on it later. Until then nothing answers: Linux and Windows refuse a connection,
    but macOS drops the SYN, so a connection hangs until its connect timeout. A test that
    needs the refusal itself uses `closed_port`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, 0))
    return sock


def closed_port() -> int:
    """An ephemeral loopback port with no socket bound to it, so every OS refuses a connection
    at once. The port was free a moment ago; nothing in the suite binds a fixed port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


class ThreadServer:
    """Runs an ASGI app under uvicorn (h11) on a given socket, in a background thread."""

    def __init__(self, app: Any, sock: socket.socket, *, lifespan: str) -> None:
        self.sock = sock
        self.port: int = sock.getsockname()[1]

        # uvicorn inspects the app's signature to tell an app from a factory. Under mutmut the
        # app's methods take (*args, **kwargs), so it is always handed a plain 3-argument one.
        async def asgi(scope, receive, send):
            await app(scope, receive, send)

        config = uvicorn.Config(
            asgi,
            http="h11",
            ws="none",
            lifespan=lifespan,
            log_config=None,
            access_log=False,
            server_header=False,
            date_header=True,
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, daemon=True
        )

    def start(self) -> ThreadServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the test server did not start")
            time.sleep(0.005)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.sock.close()


# --- the contract's body schemas (K-OAS2; TD §7 "Error bodies") ----------------------------


@dataclass
class ContractBodies:
    """The bodies the sidecar made during one test, as `conftest.py`'s hook collects them:
    `ErrorFactory.response` bodies, `stream_error_record` records and health bodies."""

    errors: list[bytes] = field(default_factory=list)
    records: list[bytes] = field(default_factory=list)
    health: list[bytes] = field(default_factory=list)


def contract_validators() -> dict[str, jsonschema.Draft202012Validator]:
    """Validators for `ErrorEnvelope`, `StreamErrorRecord` and `Health`, whose local `$ref`s
    resolve within the contract."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    components = contract["components"]
    return {
        name: jsonschema.Draft202012Validator(
            {**components["schemas"][name], "components": components}
        )
        for name in ("ErrorEnvelope", "StreamErrorRecord", "Health")
    }


def record_doc(record: bytes) -> object:
    """The JSON one in-stream record carries: an NDJSON line, or one SSE `data:` event."""
    if record.startswith(b"data: "):
        assert record.endswith(b"\n\n") and record.count(b"\n") == 2, record
        return json.loads(record[len(b"data: ") : -2])
    assert record.endswith(b"\n") and record.count(b"\n") == 1, record
    return json.loads(record)


def check_contract_bodies(
    bodies: ContractBodies, validators: dict[str, jsonschema.Draft202012Validator]
) -> None:
    """Raise `jsonschema.ValidationError` (or `AssertionError` for a malformed record) on the
    first body that does not match its schema."""
    for body in bodies.errors:
        validators["ErrorEnvelope"].validate(json.loads(body))
    for record in bodies.records:
        validators["StreamErrorRecord"].validate(record_doc(record))
    for body in bodies.health:
        validators["Health"].validate(json.loads(body))


# --- fake_ollama ---------------------------------------------------------------------------

# A behaviour answers one request: (send, fake, receive). `receive` returns `http.disconnect`
# once the sidecar closes the connection, which is how a behaviour observes a disconnect.
Behaviour = Callable[..., Awaitable[None]]


@dataclass
class Recorded:
    method: str
    raw_path: bytes
    query: bytes
    headers: list[tuple[bytes, bytes]]
    body: bytes
    # `sink` mode (TD §7, for H10): the body is hashed as it arrives, never stored. `length`
    # grows while the body streams in; `sha256` is set once it has ended, and `complete` turns
    # false if the sidecar closed the connection first.
    length: int = 0
    sha256: str | None = None
    complete: bool = True


async def _stall(fake: FakeOllama) -> None:
    deadline = time.monotonic() + STALL_LIMIT_S
    while not fake.release.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def status(code: int, body: bytes = b"", headers: list[tuple[bytes, bytes]] = ()) -> Behaviour:
    """Answer with this status, body and headers."""

    async def run(send, fake, receive=None):
        await send(
            {
                "type": "http.response.start",
                "status": code,
                "headers": [(b"content-length", str(len(body)).encode()), *headers],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return run


def stall_headers() -> Behaviour:
    """Accept the request, then send nothing until the test releases the fake."""

    async def run(send, fake, receive=None):
        await _stall(fake)

    return run


def stall_body(first: bytes, length: int) -> Behaviour:
    """Send a 200 with `Content-Length: length` and the first bytes, then stall."""

    async def run(send, fake, receive=None):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(length).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": first, "more_body": True})
        await _stall(fake)

    return run


def drip_body(length: int, every_s: float) -> Behaviour:
    """Send a 200 with `Content-Length: length`, then one byte every `every_s` seconds."""

    async def run(send, fake, receive=None):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(length).encode())],
            }
        )
        for _ in range(length - 1):
            if fake.release.is_set():
                return
            await send({"type": "http.response.body", "body": b" ", "more_body": True})
            await asyncio.sleep(every_s)
        await send({"type": "http.response.body", "body": b" "})

    return run


def exchange(exchange_id: str) -> dict:
    """One captured exchange from the S0b fixture file."""
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    (found,) = [e for e in doc["exchanges"] if e["id"] == exchange_id]
    return found


def exchange_body(exchange_id: str) -> bytes:
    return base64.b64decode(exchange(exchange_id)["response"]["body_b64"])


def replay(exchange_id: str) -> Behaviour:
    """Answer as Ollama did in the capture: its status, headers and body, sent with the
    recorded chunk boundaries. Transfer-Encoding is left to uvicorn, which chunks a response
    without a Content-Length."""
    resp = exchange(exchange_id)["response"]
    body = exchange_body(exchange_id)
    headers = [
        (k.lower().encode(), v.encode())
        for k, v in resp["headers"]
        if k.lower() != "transfer-encoding"
    ]

    async def run(send, fake, receive=None):
        await send({"type": "http.response.start", "status": resp["status"], "headers": headers})
        at = 0
        for n in resp["chunks"]:
            await send({"type": "http.response.body", "body": body[at : at + n], "more_body": True})
            at += n
        await send({"type": "http.response.body", "body": b""})

    return run


def sequence(*behaviours: Behaviour) -> Behaviour:
    """The first request gets the first behaviour, the second the next, and so on; the last
    one repeats."""
    calls = [0]

    async def run(send, fake, receive=None):
        i = min(calls[0], len(behaviours) - 1)
        calls[0] += 1
        await behaviours[i](send, fake, receive)

    return run


def abort_body(first: bytes, length: int) -> Behaviour:
    """Send a 200 with `Content-Length: length` and the first bytes, then return without
    finishing: uvicorn closes the connection, so the response is cut short (an upstream reset
    as the sidecar sees it)."""

    async def run(send, fake, receive=None):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(length).encode())],
            }
        )
        await send({"type": "http.response.body", "body": first, "more_body": True})

    return run


class Hold:
    """A behaviour that holds each request until the test releases it, and records when the
    sidecar closes the connection (TD §7 `hold` and `observe_disconnect`).

    The response's headers and first chunk, if given, are sent before the hold; after a
    release, `rest` follows and the response ends. Requests are numbered in arrival order.
    """

    def __init__(
        self,
        *,
        headers: list[tuple[bytes, bytes]] | None = None,
        first: bytes | None = None,
        rest: bytes = b"",
        status_code: int = 200,
    ) -> None:
        self.headers = headers
        self.first = first
        self.rest = rest
        self.status_code = status_code
        self._go: list[threading.Event] = []
        self.disconnected_at: dict[int, float] = {}
        self.finished: set[int] = set()

    @property
    def entered(self) -> int:
        return len(self._go)

    def release(self, i: int = 0) -> None:
        self._go[i].set()

    def release_all(self) -> None:
        for go in self._go:
            go.set()

    def wait_entered(self, n: int = 1) -> None:
        wait_for(lambda: self.entered >= n)

    def wait_disconnected(self, i: int = 0) -> float:
        wait_for(lambda: i in self.disconnected_at)
        return self.disconnected_at[i]

    async def __call__(self, send, fake, receive=None) -> None:
        i = len(self._go)
        go = threading.Event()
        self._go.append(go)
        started = self.headers is not None
        if started:
            start = {"type": "http.response.start", "status": self.status_code}
            await send({**start, "headers": self.headers})
            if self.first is not None:
                await send({"type": "http.response.body", "body": self.first, "more_body": True})
        assert receive is not None
        gone = asyncio.ensure_future(receive())  # resolves when the sidecar disconnects
        deadline = time.monotonic() + STALL_LIMIT_S
        try:
            while not go.is_set() and not fake.release.is_set():
                if gone.done():
                    self.disconnected_at[i] = time.monotonic()
                    return
                if time.monotonic() > deadline:
                    return
                await asyncio.sleep(0.002)
        finally:
            if not gone.done():
                gone.cancel()
        if not started:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": [(b"content-length", str(len(self.rest)).encode())],
                }
            )
        await send({"type": "http.response.body", "body": self.rest})
        self.finished.add(i)


@dataclass
class FakeOllama:
    """A scripted upstream. Unscripted routes answer 404."""

    routes: dict[tuple[str, str], Behaviour] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)
    server: ThreadServer | None = None
    sink: bool = False  # hash request bodies as they arrive instead of storing them

    def script(self, method: str, path: str, behaviour: Behaviour) -> None:
        self.routes[(method, path)] = behaviour

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://{LOOPBACK}:{self.server.port}"

    async def _sink(self, scope, receive, send) -> None:
        """Record the request at once, then hash its body chunk by chunk."""
        rec = Recorded(
            scope["method"], scope["raw_path"], scope["query_string"], list(scope["headers"]), b""
        )
        self.requests.append(rec)
        digest = hashlib.sha256()
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                rec.complete = False
                return
            chunk = msg.get("body", b"")
            digest.update(chunk)
            rec.length += len(chunk)
            if not msg.get("more_body"):
                break
        rec.sha256 = digest.hexdigest()
        behaviour = self.routes.get((scope["method"], scope["path"]), status(404))
        await behaviour(send, self, receive)

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
        if self.sink:
            await self._sink(scope, receive, send)
            return
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.requests.append(
            Recorded(
                scope["method"],
                scope["raw_path"],
                scope["query_string"],
                list(scope["headers"]),
                body,
            )
        )
        behaviour = self.routes.get((scope["method"], scope["path"]), status(404))
        await behaviour(send, self, receive)

    def start(self, sock: socket.socket | None = None) -> FakeOllama:
        self.server = ThreadServer(self, sock or reserve_socket(), lifespan="off").start()
        return self

    def stop(self) -> None:
        self.release.set()
        if self.server is not None:
            self.server.stop()


def reset_after(headers: list[tuple[bytes, bytes]], *chunks: bytes) -> Behaviour:
    """Send a 200 with these headers and chunks, then return without finishing: uvicorn
    closes the connection, so the sidecar sees the stream break (an upstream reset). With no
    `content-length`, uvicorn chunks the body and never sends the terminating chunk."""

    async def run(send, fake, receive=None):
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for chunk in chunks:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})

    return run


# --- raw_upstream ---------------------------------------------------------------------------


def read_request(conn: socket.socket) -> bytes:
    """Read one request with a `Content-Length` body (the sidecar always sends one for a
    buffered body), and return it as received."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            return buf
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(body) < length:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return head + b"\r\n\r\n" + body


def wait_closed(conn: socket.socket) -> None:
    """Block until the peer closes the connection (or STALL_LIMIT_S passes)."""
    try:
        while conn.recv(65536):
            pass
    except OSError:
        pass


class RawUpstream:
    """TD §7 `raw_upstream`: a plain socket server, for faults an ASGI app can't produce (a
    close at once, a malformed status line, broken chunking, a close-delimited body). Each
    connection runs `handler(conn)` in its own thread; the harness closes the socket after
    it. `requests` keeps what `read_request` returned for each connection, and `closed` is
    set once a handler has seen the peer close (through `wait_closed`)."""

    def __init__(self, handler: Callable[[RawUpstream, socket.socket], None]) -> None:
        self.handler = handler
        self.requests: list[bytes] = []
        self.accepted = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((LOOPBACK, 0))
        self._sock.listen(16)
        self._sock.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self._sock.getsockname()[1]}"

    def read(self, conn: socket.socket) -> bytes:
        request = read_request(conn)
        self.requests.append(request)
        return request

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            threading.Thread(target=self._run, args=(conn,), daemon=True).start()

    def _run(self, conn: socket.socket) -> None:
        conn.settimeout(STALL_LIMIT_S)
        with conn:
            try:
                self.handler(self, conn)
            except OSError:
                pass

    def start(self) -> RawUpstream:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(10)
        self._sock.close()


# --- the gate and the sidecar ---------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def ready_gate(
    slots: int = 1, *, retry_after_s: int = 15, drain_s: float = 10, max_hold_s: float = 1800
) -> tuple[Gate, FakeClock]:
    """A Gate on a fake clock, already past its startup quarantine."""
    clock = FakeClock()
    gate = Gate(slots, retry_after_s, drain_s, max_hold_s, clock=clock, wall=lambda: 1.7e9)
    clock.advance(drain_s)
    return gate, clock


def all_free(gate: Gate) -> bool:
    return all(s.state is SlotState.FREE for s in gate.slots())


def make_settings(ollama_url: str, **overrides: object) -> Settings:
    env = {"SERIATIM_NODE_OLLAMA_URL": ollama_url, "SERIATIM_NODE_ID": "node-a"}
    env |= {f"SERIATIM_NODE_{k.upper()}": str(v) for k, v in overrides.items()}
    return load_settings(env)


@dataclass
class Sidecar:
    settings: Settings
    app: Any
    server: ThreadServer

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.server.port}"

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def gate(self) -> Any:
        return self.app.gate


def start_sidecar(
    ollama_url: str,
    *,
    gate: Any = None,
    wrap: Callable[[Any], Any] | None = None,
    **overrides: object,
) -> Sidecar:
    """`wrap(app)`, if given, is the ASGI app the server runs (E14 wraps `receive`)."""
    settings = make_settings(ollama_url, **overrides)
    app = create_app(settings, gate=gate)
    server = ThreadServer(wrap(app) if wrap else app, reserve_socket(), lifespan="on").start()
    return Sidecar(settings, app, server)


# --- raw sockets ----------------------------------------------------------------------------


@dataclass
class RawResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]


def read_response(sock: socket.socket) -> RawResponse:
    """Read exactly one response with a `Content-Length` from a keep-alive socket."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(f"the connection closed before a response: {buf!r}")
        buf += chunk
    head, _, body = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = [
        (k.strip().lower(), v.strip()) for k, v in (line.split(":", 1) for line in lines[1:])
    ]
    length = int(next(v for k, v in headers if k == "content-length"))
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError("the connection closed inside a response body")
        body += chunk
    assert len(body) == length, "nothing follows the response on the socket yet"
    return RawResponse(int(lines[0].split()[1]), headers, body)


def raw_connect(port: int, timeout: float = WAIT_S) -> socket.socket:
    return socket.create_connection((LOOPBACK, port), timeout=timeout)


def raw_exchange(port: int, data: bytes, timeout: float = 10) -> RawResponse:
    """Send `data` as is and read until the server closes the connection."""
    with socket.create_connection((LOOPBACK, port), timeout=timeout) as s:
        s.sendall(data)
        chunks = []
        while chunk := s.recv(65536):
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = [
        (k.strip().lower(), v.strip()) for k, v in (line.split(":", 1) for line in lines[1:])
    ]
    return RawResponse(int(lines[0].split()[1]), headers, body)


# --- the sidecar as a process ---------------------------------------------------------------

NODE_PREFIX = "SERIATIM_NODE_"
# A sidecar process has this long to listen or to exit. A cold Python start that imports the
# package takes a few seconds on a slow CI runner; a failure still never waits for ever.
PROCESS_S = 30.0

# Runs `python -m seriatim_node` with a test-only trigger (R3): each line on stdin runs
# `gc.collect()` in the sidecar, which then writes GC_DONE to stderr.
GC_DONE = "test-hook: gc collected"
GC_HOOK = f"""
import gc, sys, threading

def watch():
    for _ in sys.stdin:
        gc.collect()
        print({GC_DONE!r}, file=sys.stderr, flush=True)

threading.Thread(target=watch, daemon=True).start()
from seriatim_node.__main__ import run
sys.exit(run())
"""


def process_env(ollama_url: str, lock_dir: Path, port: int, **overrides: object) -> dict[str, str]:
    """The child's environment: this one without any `SERIATIM_NODE_*`, plus test mode (which
    allows the temporary lock directory), the listen port, the upstream and `overrides`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(NODE_PREFIX)}
    settings = {
        "listen_port": port,
        "ollama_url": ollama_url,
        "lock_dir": lock_dir,
        "allow_unauthenticated": "true",
        "id": "node-a",
    } | overrides
    env |= {NODE_PREFIX + k.upper(): str(v) for k, v in settings.items()}
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class SidecarProcess:
    proc: subprocess.Popen
    port: int
    lock_dir: Path
    stderr_path: Path
    spawned_at: float  # time.perf_counter() just before the spawn
    _files: list[IO[bytes]]

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")

    def wait_listening(self, timeout: float = PROCESS_S) -> None:
        """Wait until the sidecar accepts connections; fail if it exits first."""
        deadline = time.monotonic() + timeout
        while True:
            if self.proc.poll() is not None:
                raise AssertionError(f"the sidecar exited {self.proc.returncode}: {self.stderr()}")
            try:
                socket.create_connection((LOOPBACK, self.port), timeout=1).close()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError("the sidecar did not start listening") from None
                time.sleep(0.01)

    def wait_exit(self, timeout: float = PROCESS_S) -> int:
        return self.proc.wait(timeout)

    def collect_garbage(self) -> None:
        """Run `gc.collect()` in a sidecar started with `GC_HOOK`, and wait until it has."""
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"gc\n")
        self.proc.stdin.flush()
        wait_for(lambda: GC_DONE in self.stderr())

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(PROCESS_S)
        self.close_files()

    def close_files(self) -> None:
        for f in self._files:
            f.close()
        self._files.clear()
        if self.proc.stdin is not None:
            self.proc.stdin.close()


def start_process(
    ollama_url: str,
    lock_dir: Path,
    log_dir: Path,
    *,
    port: int | None = None,
    gc_hook: bool = False,
    **overrides: object,
) -> SidecarProcess:
    """Spawn a sidecar on `port` (a fresh one by default). Its stdout and stderr go to files
    in `log_dir`, so a full pipe can never block it."""
    port = port or closed_port()
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"sidecar-{port}-{time.monotonic_ns()}"
    out = open(log_dir / f"{stem}.out", "wb")  # closed by close_files
    err_path = log_dir / f"{stem}.err"
    err = open(err_path, "wb")  # closed by close_files
    argv = [sys.executable, "-c", GC_HOOK] if gc_hook else [sys.executable, "-m", "seriatim_node"]
    spawned_at = time.perf_counter()
    proc = subprocess.Popen(  # nosec B603 # a fixed argv: this Python and the package
        argv,
        env=process_env(ollama_url, lock_dir, port, **overrides),
        stdin=subprocess.PIPE if gc_hook else subprocess.DEVNULL,
        stdout=out,
        stderr=err,
    )
    return SidecarProcess(proc, port, lock_dir, err_path, spawned_at, [out, err])
