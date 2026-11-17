"""The real-socket test harness (TD-slice-1 §7). `conftest.py` exposes it as fixtures.

- `FakeOllama`: a real uvicorn server on 127.0.0.1 running a small ASGI app that each test
  scripts per route. Every request is recorded with its raw method, path, query, headers and
  body.
- `start_sidecar`: runs `create_app(settings)` under a real uvicorn server (h11) on
  127.0.0.1, with `ollama_url` pointing at the fake (or at a port nothing listens on).

Servers run in their own thread and event loop, so tests stay synchronous and work the same
on Linux, macOS and Windows. Every socket is bound by the harness to an ephemeral port before
the server starts, so a test always knows the port, and a port can be held closed ("Ollama is
down") and started later on the same socket.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import uvicorn

from seriatim_node.app import create_app
from seriatim_node.config import Settings, load_settings
from seriatim_node.gate import Gate, SlotState

LOOPBACK = "127.0.0.1"
STALL_LIMIT_S = 30.0  # a stalled fake gives up after this, so teardown can never hang


def reserve_socket() -> socket.socket:
    """A TCP socket bound to an ephemeral loopback port, not yet listening: connections to it
    are refused until a server is started on it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((LOOPBACK, 0))
    return sock


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


# --- fake_ollama ---------------------------------------------------------------------------

Behaviour = Callable[[Callable[[dict], Awaitable[None]], "FakeOllama"], Awaitable[None]]


@dataclass
class Recorded:
    method: str
    raw_path: bytes
    query: bytes
    headers: list[tuple[bytes, bytes]]
    body: bytes


async def _stall(fake: FakeOllama) -> None:
    deadline = time.monotonic() + STALL_LIMIT_S
    while not fake.release.is_set() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def status(code: int, body: bytes = b"", headers: list[tuple[bytes, bytes]] = ()) -> Behaviour:
    """Answer with this status, body and headers."""

    async def run(send, fake):
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

    async def run(send, fake):
        await _stall(fake)

    return run


def stall_body(first: bytes, length: int) -> Behaviour:
    """Send a 200 with `Content-Length: length` and the first bytes, then stall."""

    async def run(send, fake):
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

    async def run(send, fake):
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


@dataclass
class FakeOllama:
    """A scripted upstream. Unscripted routes answer 404."""

    routes: dict[tuple[str, str], Behaviour] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)
    server: ThreadServer | None = None

    def script(self, method: str, path: str, behaviour: Behaviour) -> None:
        self.routes[(method, path)] = behaviour

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://{LOOPBACK}:{self.server.port}"

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
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
        await behaviour(send, self)

    def start(self, sock: socket.socket | None = None) -> FakeOllama:
        self.server = ThreadServer(self, sock or reserve_socket(), lifespan="off").start()
        return self

    def stop(self) -> None:
        self.release.set()
        if self.server is not None:
            self.server.stop()


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


def start_sidecar(ollama_url: str, *, gate: Any = None, **overrides: object) -> Sidecar:
    settings = make_settings(ollama_url, **overrides)
    app = create_app(settings, gate=gate)
    server = ThreadServer(app, reserve_socket(), lifespan="on").start()
    return Sidecar(settings, app, server)


# --- raw sockets ----------------------------------------------------------------------------


@dataclass
class RawResponse:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]


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
