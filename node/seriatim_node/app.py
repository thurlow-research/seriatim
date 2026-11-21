"""The sidecar's application module (TD-slice-1 §2.4).

S1-1 adds the error table: `ErrorCode`, `ERROR_TABLE` and `ErrorFactory`. `contract/openapi.yaml`
is the source of truth, and K-OAS3 checks that this table equals its `x-seriatim-codes`.
S1-5 adds `RequestLog`, `create_app` (the dispatcher and `/seriatim/health`) and the health
client. S1-6 hands every other route to the proxy (`proxy.py`). S1-12 adds the singleton
lock (`acquire_singleton`) and `main()`. S1-13 completes the log line (TD Appendix A): each
line has `wait_ms`, `hold_ms`, `bytes_in` and `bytes_out`.
"""

from __future__ import annotations

import contextlib
import errno
import http.cookiejar
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import anyio
import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route as StarletteRoute

from seriatim_node.config import ConfigError, Settings, load_settings
from seriatim_node.gate import SERIATIM_ROUTES, Gate, Kind, PathError, check_raw_path, classify


class ErrorCode(StrEnum):
    """Exactly the contract's `ErrorCode` enum (ADR-001 §5.4; K-OAS3)."""

    GPU_BUSY = "GPU_BUSY"
    BAD_PATH = "BAD_PATH"
    AUTH_AMBIGUOUS = "AUTH_AMBIGUOUS"
    BAD_REQUEST_BODY = "BAD_REQUEST_BODY"
    NUM_CTX_AMBIGUOUS = "NUM_CTX_AMBIGUOUS"
    NUM_CTX_UNSUPPORTED_ROUTE = "NUM_CTX_UNSUPPORTED_ROUTE"
    NODE_REQUIRED = "NODE_REQUIRED"
    UNKNOWN_TASK = "UNKNOWN_TASK"
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_INVALID = "AUTH_INVALID"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    REQUEST_BODY_TIMEOUT = "REQUEST_BODY_TIMEOUT"
    UPLOAD_INTERRUPTED = "UPLOAD_INTERRUPTED"
    BODY_TOO_LARGE = "BODY_TOO_LARGE"
    NUM_CTX_EXCEEDS_LIMIT = "NUM_CTX_EXCEEDS_LIMIT"
    MODEL_SIZE_UNKNOWN = "MODEL_SIZE_UNKNOWN"
    MODEL_NOT_ELIGIBLE = "MODEL_NOT_ELIGIBLE"
    INTERNAL = "INTERNAL"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    UPSTREAM_PROTOCOL_ERROR = "UPSTREAM_PROTOCOL_ERROR"
    NODE_STARTING = "NODE_STARTING"
    NODE_OFFLINE = "NODE_OFFLINE"
    EXPOSURE_UNVERIFIED = "EXPOSURE_UNVERIFIED"
    OLLAMA_EXPOSED = "OLLAMA_EXPOSED"
    NODE_SATURATED = "NODE_SATURATED"
    DISCOVERY_INCOMPLETE = "DISCOVERY_INCOMPLETE"
    SIZE_POLICY_MISSING = "SIZE_POLICY_MISSING"
    GPU_OFFLINE = "GPU_OFFLINE"
    FLEET_UNAVAILABLE = "FLEET_UNAVAILABLE"
    LOCK_HOLD_EXCEEDED = "LOCK_HOLD_EXCEEDED"
    HTTP_VERSION_UNSUPPORTED = "HTTP_VERSION_UNSUPPORTED"
    UPSTREAM_STREAM_BROKEN = "UPSTREAM_STREAM_BROKEN"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """One row of the error table. `status` is None for an in-stream-only code."""

    status: int | None
    retryable: bool
    replay_safe: bool


_E = ErrorCode
ERROR_TABLE: Mapping[ErrorCode, ErrorSpec] = MappingProxyType(
    {
        _E.GPU_BUSY: ErrorSpec(429, True, True),
        _E.BAD_PATH: ErrorSpec(400, False, True),
        _E.AUTH_AMBIGUOUS: ErrorSpec(400, False, True),
        _E.BAD_REQUEST_BODY: ErrorSpec(400, False, True),
        _E.NUM_CTX_AMBIGUOUS: ErrorSpec(400, False, True),
        _E.NUM_CTX_UNSUPPORTED_ROUTE: ErrorSpec(400, False, True),
        _E.NODE_REQUIRED: ErrorSpec(400, False, True),
        _E.UNKNOWN_TASK: ErrorSpec(400, False, True),
        _E.AUTH_MISSING: ErrorSpec(401, False, True),
        _E.AUTH_INVALID: ErrorSpec(401, False, True),
        _E.FORBIDDEN_SCOPE: ErrorSpec(403, False, True),
        _E.NOT_FOUND: ErrorSpec(404, False, True),
        _E.MODEL_NOT_FOUND: ErrorSpec(404, False, True),
        _E.METHOD_NOT_ALLOWED: ErrorSpec(405, False, True),
        _E.REQUEST_BODY_TIMEOUT: ErrorSpec(408, True, True),
        _E.UPLOAD_INTERRUPTED: ErrorSpec(408, True, False),
        _E.BODY_TOO_LARGE: ErrorSpec(413, False, True),
        _E.NUM_CTX_EXCEEDS_LIMIT: ErrorSpec(422, False, True),
        _E.MODEL_SIZE_UNKNOWN: ErrorSpec(422, False, True),
        _E.MODEL_NOT_ELIGIBLE: ErrorSpec(422, False, True),
        _E.INTERNAL: ErrorSpec(500, True, False),
        _E.UPSTREAM_UNAVAILABLE: ErrorSpec(502, True, True),
        _E.UPSTREAM_PROTOCOL_ERROR: ErrorSpec(502, True, False),
        _E.NODE_STARTING: ErrorSpec(503, True, True),
        _E.NODE_OFFLINE: ErrorSpec(503, False, True),
        _E.EXPOSURE_UNVERIFIED: ErrorSpec(503, True, True),
        _E.OLLAMA_EXPOSED: ErrorSpec(503, False, True),
        _E.NODE_SATURATED: ErrorSpec(503, True, True),
        _E.DISCOVERY_INCOMPLETE: ErrorSpec(503, True, True),
        _E.SIZE_POLICY_MISSING: ErrorSpec(503, False, True),
        _E.GPU_OFFLINE: ErrorSpec(503, False, True),
        _E.FLEET_UNAVAILABLE: ErrorSpec(503, True, True),
        _E.LOCK_HOLD_EXCEEDED: ErrorSpec(504, True, False),
        _E.HTTP_VERSION_UNSUPPORTED: ErrorSpec(505, False, True),
        _E.UPSTREAM_STREAM_BROKEN: ErrorSpec(None, True, False),
    }
)
del _E

# X-Seriatim-Gate for codes whose value never depends on the call site (TD §5): `busy` only
# for GPU_BUSY, and `refused` for the refusals given before Ollama is contacted (SPEC REQ-37).
_FIXED_GATE: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.GPU_BUSY: "busy",
        ErrorCode.NODE_STARTING: "refused",
        ErrorCode.NODE_OFFLINE: "refused",
        ErrorCode.EXPOSURE_UNVERIFIED: "refused",
        ErrorCode.OLLAMA_EXPOSED: "refused",
        ErrorCode.NUM_CTX_EXCEEDS_LIMIT: "refused",
    }
)
# Every other code is `acquired` when a slot was taken, otherwise `ungated`.
_CALLER_GATES = frozenset({"acquired", "ungated"})
# The optional ErrorEnvelope properties a call site may add (contract: additionalProperties
# is false, so any other key would make the body invalid).
_EXTRA_FIELDS = frozenset({"busy_for_s", "limit", "hint", "nodes"})

Headers = list[tuple[bytes, bytes]]


class ErrorFactory:
    """Builds every 4xx and 5xx the sidecar produces (TD §5, "Building error responses").

    `retryable` and `replay_safe` always come from `ERROR_TABLE`, never from the call site.
    """

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id

    def response(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry_after: int | None = None,
        gate_value: str,
        request_id: str,
        **extra: object,
    ) -> tuple[int, Headers, bytes]:
        """Return `(status, headers, body)` for an `ErrorEnvelope` response.

        `gate_value` (`acquired` or `ungated`) is used unless the code fixes the gate header
        itself (`busy` for GPU_BUSY, `refused` for the pre-Ollama refusals). `Retry-After` is
        sent, and `retry_after` set in the body, whenever `retry_after` is given.
        """
        spec = ERROR_TABLE[code]
        if spec.status is None:
            raise ValueError(f"{code} is an in-stream record, not a response")
        gate = _FIXED_GATE.get(code)
        if gate is None:
            if gate_value not in _CALLER_GATES:
                raise ValueError(f"gate_value must be acquired or ungated, not {gate_value!r}")
            gate = gate_value
        unknown = set(extra) - _EXTRA_FIELDS
        if unknown:
            raise ValueError(f"not ErrorEnvelope fields: {sorted(unknown)}")
        if retry_after is not None and (
            isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 0
        ):
            raise ValueError(f"retry_after must be a whole number of seconds, not {retry_after!r}")

        envelope: dict[str, object] = {
            "error": message,
            "error_code": code.value,
            "retryable": spec.retryable,
            "replay_safe": spec.replay_safe,
        }
        if retry_after is not None:
            envelope["retry_after"] = retry_after
        envelope["source"] = "sidecar"
        envelope["node"] = self.node_id
        envelope.update(extra)
        body = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode()

        headers: Headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        headers += [
            (b"x-seriatim-gate", gate.encode()),
            (b"x-seriatim-node", self.node_id.encode()),
            (b"x-request-id", request_id.encode()),
        ]
        return spec.status, headers, body


# --- The request log (SPEC REQ-22) -----------------------------------------------------------

_LEVELS: Mapping[str, int] = MappingProxyType(
    {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
)


class RequestLog:
    """One JSON line per request to stdout (REQ-22; TD Appendix A).

    A line is written only when its `level` is at or above the configured one, so health
    requests, logged at `DEBUG`, add nothing at the default `INFO` (HL9).
    """

    def __init__(self, level: str, node_id: str, wall: Callable[[], float] = time.time) -> None:
        self._threshold = _LEVELS[level]
        self._node = node_id
        self._wall = wall

    def emit(self, *, level: str = "INFO", **fields: object) -> None:
        if _LEVELS[level] < self._threshold:
            return
        ms = int(self._wall() * 1000)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms // 1000))
        line = {
            "ts": f"{stamp}.{ms % 1000:03d}Z",
            "level": level,
            "event": "request",
            **fields,
            "node": self._node,
        }
        # Resolved at each call, so a redirected or captured stdout is honoured.
        sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
        sys.stdout.flush()


# --- HTTP clients (TD §2.4) ------------------------------------------------------------------


class NoCookies(http.cookiejar.CookieJar):
    """A cookie jar that stores nothing, so a `Set-Cookie` from Ollama is never replayed."""

    def __init__(self) -> None:
        super().__init__(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))


def new_client(
    settings: Settings, *, limits: httpx.Limits, timeout: httpx.Timeout
) -> httpx.AsyncClient:
    """An upstream client with the settings every client shares (TD §2.4)."""
    return httpx.AsyncClient(
        base_url=str(settings.ollama_url),
        http1=True,
        http2=False,
        trust_env=False,
        follow_redirects=False,
        headers={},
        cookies=NoCookies(),
        limits=limits,
        timeout=timeout,
    )


def new_health_client(settings: Settings) -> httpx.AsyncClient:
    """The health client: one connection, every phase bounded by the probe timeout."""
    return new_client(
        settings,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        timeout=httpx.Timeout(settings.health_probe_timeout_s),
    )


# --- The dispatcher and health (TD §2.4, §3.4) -----------------------------------------------

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_REQUEST_ID_RE = re.compile(rb"[A-Za-z0-9._-]{1,128}")  # used with fullmatch
_RID_KEY = "seriatim.request_id"  # where the dispatcher leaves the request id in the scope
_HEALTH_KEY = "seriatim.health_status"  # where `_health` leaves its status for the log line
_HEALTH_OUTCOMES: Mapping[int | None, str] = MappingProxyType({200: "ok", 503: "degraded"})
# The log line's gate timing for a request no gate was asked about (TD Appendix A).
_NO_GATE: Mapping[str, None] = MappingProxyType({"wait_ms": None, "hold_ms": None})


def request_id_from(headers: list[tuple[bytes, bytes]]) -> str:
    """Keep the client's `X-Request-Id` only if there is exactly one and it is valid; else a
    fresh `uuid4().hex` (TD §2.4 step 2)."""
    values = [v for k, v in headers if k.lower() == b"x-request-id"]
    if len(values) == 1 and _REQUEST_ID_RE.fullmatch(values[0]):
        return values[0].decode()
    return uuid.uuid4().hex


class SentBytes:
    """A `send` that counts the body bytes of each message once `send()` has returned, so a
    write that failed adds nothing (the log's `bytes_out`, TD Appendix A). `failed` names the
    exception a write raised, so the caller can tell the client going from its own error."""

    def __init__(self, send: Send) -> None:
        self._send = send
        self.count = 0
        self.failed: str | None = None

    async def __call__(self, message: MutableMapping[str, Any]) -> None:
        try:
            await self._send(message)
        except Exception as exc:
            self.failed = type(exc).__name__
            raise
        self.count += len(message.get("body", b""))


def note_write_failed(fields: dict[str, object], reason: str) -> None:
    """A failed write to the client goes on the request's own line, never a line of its own
    (L2 wins over §3.3's "log DEBUG" wording; ruled on #60)."""
    fields["client_write_failed"] = True
    fields["client_write_failed_reason"] = reason


async def _send_response(
    send: Send, status: int, headers: list[tuple[bytes, bytes]], body: bytes
) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


class _Dispatcher:
    """The ASGI callable `create_app` returns. It sees every request before Starlette."""

    def __init__(
        self, settings: Settings, gate: Gate | None, client: httpx.AsyncClient | None
    ) -> None:
        self.settings = settings
        self.gate = gate
        self.client = client  # the shared ungated client; the lifespan creates it if None
        self.health_client: httpx.AsyncClient | None = None
        self.errors = ErrorFactory(settings.id)
        self.log = RequestLog(settings.log_level, settings.id)
        self.proxy: Any = None  # set by the lifespan, which runs before any request
        # The dispatcher has already refused every other method (405), so the route lists none.
        self._router = Starlette(
            routes=[StarletteRoute("/seriatim/health", self._health)],
            lifespan=self._lifespan,
        )

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: Starlette) -> AsyncIterator[None]:
        s = self.settings
        # proxy.py imports this module, so it is imported here rather than at the top.
        from seriatim_node.proxy import Proxy, new_ungated_client

        if self.gate is None:  # a new Gate starts in its startup quarantine (REQ-12a)
            self.gate = Gate(s.slots, s.retry_after_s, s.drain_s, s.max_hold_s)
        owned = self.client is None
        client = self.client = new_ungated_client(s) if owned else self.client
        self.health_client = new_health_client(s)
        self.proxy = Proxy(s, self.gate, client, self.log, self.errors)
        try:
            yield
        finally:
            await self.health_client.aclose()
            if owned:
                await client.aclose()

    async def reply(
        self,
        send: Send,
        code: ErrorCode,
        message: str,
        *,
        request_id: str,
        method: str,
        log_level: str = "INFO",
        outcome: str = "rejected",
        extra_headers: list[tuple[bytes, bytes]] | None = None,
        **log_fields: object,
    ) -> None:
        """Send an ungated error response and log it (`slot_end` is `none`: no slot). The
        dispatcher never reads the body, and no gate was asked. The line is written whatever
        the write does, with the body bytes actually sent (L2)."""
        status, headers, body = self.errors.response(
            code, message, gate_value="ungated", request_id=request_id
        )
        sent = SentBytes(send)
        try:
            await _send_response(sent, status, headers + (extra_headers or []), body)
        except Exception as exc:  # only a write can raise: the client is gone (S1-10)
            note_write_failed(log_fields, type(exc).__name__)
        finally:
            self.log.emit(
                level=log_level,
                request_id=request_id,
                method=method,
                **log_fields,
                outcome=outcome,
                status=status,
                slot_end="none",
                **_NO_GATE,
                bytes_in=0,
                bytes_out=sent.count,
            )

    async def _health_route(self, scope: Scope, receive: Receive, send: Send) -> None:
        """`GET /seriatim/health` through Starlette, then its one line, at `DEBUG`, once the
        response has been sent (or its write has failed): `_health` leaves the status in
        the scope, and `bytes_out` counts what `send()` took. Only a failed write is
        swallowed; any other error still propagates, after the line."""
        sent = SentBytes(send)
        fields: dict[str, object] = {}
        try:
            await self._router(scope, receive, sent)
        except Exception:
            if sent.failed is None:
                raise
            note_write_failed(fields, sent.failed)  # the client is gone (S1-10)
        finally:
            status = scope.get(_HEALTH_KEY)  # None if `_health` itself failed
            result = _HEALTH_OUTCOMES.get(status, "internal_error")
            self.log.emit(
                # a load balancer polls this, so DEBUG (REQ-22; HL9), unless it broke
                level="ERROR" if result == "internal_error" else "DEBUG",
                request_id=scope[_RID_KEY],
                method="GET",
                path="/seriatim/health",
                kind="seriatim",
                scope="none",
                known=True,
                **fields,
                outcome=result,
                status=status,
                slot_end="none",
                **_NO_GATE,
                bytes_in=0,
                bytes_out=sent.count,
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan (and anything else) goes to Starlette
            await self._router(scope, receive, send)
            return
        rid = request_id_from(scope["headers"])
        method = scope["method"]
        if scope["http_version"] != "1.1":
            await self.reply(
                send,
                ErrorCode.HTTP_VERSION_UNSUPPORTED,
                "only HTTP/1.1 is supported",
                request_id=rid,
                method=method,
                extra_headers=[(b"connection", b"close")],
            )
            return
        names = {k for k, _ in scope["headers"]}
        if b"transfer-encoding" in names and b"content-length" in names:
            # h11 accepts this framing and uses Transfer-Encoding alone (RFC 9112 §6.3), so the
            # sidecar refuses it and closes the connection itself, as §6.1 requires (H28).
            await self.reply(
                send,
                ErrorCode.BAD_REQUEST_BODY,
                "Transfer-Encoding and Content-Length together",
                request_id=rid,
                method=method,
                extra_headers=[(b"connection", b"close")],
            )
            return
        raw_path = scope.get("raw_path")
        if raw_path is None:  # uvicorn with h11 always sets it: fail closed
            await self.reply(
                send,
                ErrorCode.INTERNAL,
                "internal error",
                request_id=rid,
                method=method,
                log_level="ERROR",
                outcome="internal_error",
            )
            return
        try:
            check_raw_path(raw_path)
        except PathError as e:
            await self.reply(send, ErrorCode.BAD_PATH, str(e), request_id=rid, method=method)
            return
        route = classify(method, raw_path)
        where = {
            "path": raw_path.decode("latin-1"),
            "kind": route.kind.value,
            "scope": route.scope.value,
            "known": route.known,
        }
        if route.kind is not Kind.SERIATIM:
            assert self.proxy is not None  # set by the lifespan
            await self.proxy(scope, receive, send, route, rid)
        elif route.seriatim_status == 404:
            await self.reply(
                send, ErrorCode.NOT_FOUND, "no such path", request_id=rid, method=method, **where
            )
        elif route.seriatim_status == 405:
            allow = ", ".join(sorted(SERIATIM_ROUTES[raw_path])).encode()
            await self.reply(
                send,
                ErrorCode.METHOD_NOT_ALLOWED,
                "method not allowed",
                request_id=rid,
                method=method,
                extra_headers=[(b"allow", allow)],
                **where,
            )
        else:
            scope[_RID_KEY] = rid
            await self._health_route(scope, receive, send)

    async def _probe(self) -> str | None:
        """None if Ollama answers well; otherwise the health reason (TD §3.4)."""
        client = self.health_client
        assert client is not None  # set by the lifespan, which runs before any request
        try:
            with anyio.fail_after(self.settings.health_probe_timeout_s):  # total, not per read
                resp = await client.get("/api/version")
        except (httpx.TransportError, TimeoutError):  # every httpx timeout is a TransportError
            return "ollama_unreachable"
        if resp.status_code != 200:
            return "ollama_bad_response"
        try:
            doc = json.loads(resp.content)
        except ValueError:  # includes a body that isn't UTF-8
            return "ollama_bad_response"
        if not isinstance(doc, dict):
            return "ollama_bad_response"
        version = doc.get("version")
        if not isinstance(version, str) or not version:
            return "ollama_bad_response"
        return None

    async def _health(self, request: Request) -> Response:
        gate = self.gate
        assert gate is not None  # set at construction or by the lifespan
        rid = request.scope[_RID_KEY]
        reasons = []
        probe = await self._probe()
        if probe is not None:
            reasons.append(probe)
        if gate.overdue():
            reasons.append("slot_overdue")
        if gate.any_stuck():
            reasons.append("slot_stuck")
        if gate.starting():
            reasons.append("starting")
        doc: dict[str, object] = {"status": "ok"}
        status = 200
        if reasons:
            doc = {"status": "degraded", "reasons": reasons}
            status = 503
        request.scope[_HEALTH_KEY] = status  # logged by `_health_route`, after the send
        return Response(
            json.dumps(doc, separators=(",", ":")).encode(),
            status_code=status,
            media_type="application/json",
            headers={
                "x-seriatim-gate": "ungated",
                "x-seriatim-node": self.settings.id,
                "x-request-id": rid,
            },
        )


def create_app(
    settings: Settings, *, client: httpx.AsyncClient | None = None, gate: Gate | None = None
) -> ASGIApp:
    """The sidecar's ASGI app (TD §2.4). Tests may inject the shared ungated `client` and the
    `gate`; otherwise the lifespan creates them (a new gate starts in its startup quarantine).

    It also quiets the HTTP client's own loggers (L3): httpx logs each request's URL, query
    included, at INFO, and httpcore's DEBUG traces carry Ollama's response headers. Nothing
    configures `logging` in the sidecar, so neither is written as shipped; this keeps it so
    if a handler is ever added."""
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return _Dispatcher(settings, gate, client)


# Libraries whose own log records can carry a secret (a query string, a `Set-Cookie`).
_QUIET_LOGGERS = ("httpx", "httpcore")


class SingletonError(Exception):
    """The singleton lock is held by another sidecar, or cannot be taken (REQ-12b; exit 3)."""


# The open, locked lock file. It is never unlocked, closed or deleted: the OS releases the lock
# when the process exits, for any reason. An int, not a file object, so that nothing (garbage
# collection included) can ever close it behind the code's back (TD §2.4 "Holding the lock").
_SINGLETON_FD: int | None = None


def lock_path(settings: Settings) -> Path:
    """`<lock_dir>/upstream-<port>.lock`: one lock per upstream, since it is always loopback."""
    return settings.lock_dir / f"upstream-{settings.ollama_url.port}.lock"


def _lock(fd: int) -> None:
    """A non-blocking exclusive lock on `fd`: `flock` on POSIX, one byte at offset 0 on
    Windows. Raises `OSError` if it can't be taken."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _held(e: OSError) -> bool:
    """Whether a failed lock means another process holds it (not that it can't be taken)."""
    if sys.platform == "win32":
        return e.errno == errno.EACCES
    return isinstance(e, BlockingIOError)


def acquire_singleton(settings: Settings) -> None:
    """Take the host-wide lock for this sidecar's upstream (REQ-12b), or raise
    `SingletonError`. Being unable to take it never lets the sidecar start."""
    global _SINGLETON_FD
    path = lock_path(settings)
    try:
        settings.lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        raise SingletonError(f"cannot lock {path}: {e.strerror or e}") from None
    try:
        _lock(fd)
    except OSError as e:
        os.close(fd)  # this descriptor holds nothing; the file itself is never removed
        if _held(e):
            raise SingletonError(f"another sidecar holds {path}") from None
        raise SingletonError(f"cannot lock {path}: {e.strerror or e}") from None
    _SINGLETON_FD = fd


def main() -> int:
    """`seriatim-node` and `python -m seriatim_node` (TD §2.4). Returns the exit code: 2 for
    invalid configuration, 3 if the singleton lock can't be taken, else 0 once uvicorn has
    stopped."""
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"seriatim-node: {e}", file=sys.stderr)
        return 2
    try:
        acquire_singleton(settings)
    except SingletonError as e:
        print(f"seriatim-node: {e}", file=sys.stderr)
        return 3
    app = create_app(settings)

    # uvicorn inspects the app's signature to tell an app from a factory, so it is handed a
    # plain 3-argument function, whatever the dispatcher's own signature looks like.
    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        await app(scope, receive, send)

    uvicorn.run(
        asgi,
        host=settings.listen_host,
        port=settings.listen_port,
        http="h11",
        workers=1,  # REQ-21: the gate is in-process state
        lifespan="on",
        log_config=None,
        access_log=False,  # its line has the query string; the request log never does (L3)
        server_header=False,
        date_header=True,
    )
    return 0
