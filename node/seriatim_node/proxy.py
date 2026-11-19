"""The proxy: passthrough for every non-Seriatim route (TD-slice-1 §2.3, §3.3, §4, §5).

S1-6 adds the request lifecycle of §3.3 complete: admission, the body read, the disconnect
watcher, the relay, the hold limit, and the shielded `finally` that proves upstream closure
before it ends the lease, then talks to the client. Later PRs add to it without changing that
structure. S1-7 adds the full header strip lists of §4 and `prune_httpx_defaults`. S1-8
adds the request limits: the size limit and the total timeout in `read_body`, and the
`Content-Length` check before admission. S1-9 adds the framing rule for a clean end
(`upstream_framed`, with the bodyless rule first) and the in-stream error record sent before
an abort after the headers (`record_framing`, `stream_error_record`). S1-10 makes the
shielded `finally` survive a second cancellation (`_uninterrupted`), bounds every upstream
close by `close_timeout_s` (the body is read from `resp.stream`, so no close happens inside
the exchange) and logs a failed final write. Still to come:
- S1-11: streamed blob uploads (`body_stream`).
"""

from __future__ import annotations

import asyncio
import json
import traceback
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from dataclasses import dataclass
from typing import Any, Literal

import anyio
import httpx

from seriatim_node.app import ErrorCode, ErrorFactory, RequestLog, new_client
from seriatim_node.config import Settings
from seriatim_node.gate import Busy, Gate, Kind, Lease, Route, Starting

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
Headers = list[tuple[bytes, bytes]]

# Errors that mean no byte reached Ollama (§3.3 step 4): the slot is freed, and the error is
# replay-safe.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

# The strip lists of TD §4 (ADR-001 §5.3). Names are compared in lower case, and every header
# named in a message's own `Connection` header goes too.
_HOP_BY_HOP = frozenset(
    {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te",
     b"trailer", b"transfer-encoding", b"upgrade"}
)  # fmt: skip
# Request side: also `host`, the credentials, the forwarding headers, every `proxy-*` (ADR-001
# §5.3), the framing the sidecar sets itself, `accept-encoding` (so Ollama never compresses)
# and the client's request id.
_UPSTREAM_DROP = _HOP_BY_HOP | {
    b"host", b"authorization", b"cookie", b"x-api-key", b"content-length", b"accept-encoding",
    b"forwarded", b"x-real-ip", b"x-request-id",
}  # fmt: skip
_UPSTREAM_DROP_PREFIXES = (b"x-seriatim-", b"x-forwarded-", b"proxy-")
# Response side: also the compatibility headers Ollama must not forge, `set-cookie`, `server`,
# Ollama's own request id (H18) and Ollama's `date`, so uvicorn's is the only one.
_DOWNSTREAM_DROP = _HOP_BY_HOP | {
    b"x-resolved-model", b"x-model-digest", b"x-execution-device", b"x-rerouted-from",
    b"x-reroute-reason", b"x-context-clamped", b"set-cookie", b"server", b"x-request-id",
    b"date",
}  # fmt: skip
_DOWNSTREAM_DROP_PREFIXES = (b"access-control-", b"x-seriatim-")
# The headers httpx fills in on every request it builds. They reach Ollama only if the client
# sent them (`accept-encoding` and `connection` never do: both are on the strip list).
HTTPX_DEFAULTS = (b"accept", b"accept-encoding", b"connection", b"user-agent")


# --- clients (TD §2.4, "HTTP clients") ------------------------------------------------------


def new_gated_client(settings: Settings) -> httpx.AsyncClient:
    """One client per gated request, used for one exchange. `aclose()` closes its only
    connection, which is the proof of closure invariant 3 needs. No read or write timeout:
    the hold limit is the only bound."""
    return new_client(
        settings,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_s,
            read=None,
            write=None,
            pool=settings.connect_timeout_s,
        ),
    )


def new_ungated_client(settings: Settings) -> httpx.AsyncClient:
    """The one client shared by ungated routes, so they can never starve inference."""
    return new_client(
        settings,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        timeout=httpx.Timeout(
            connect=settings.connect_timeout_s,
            read=settings.ungated_read_timeout_s,
            write=settings.ungated_read_timeout_s,
            pool=settings.connect_timeout_s,
        ),
    )


# --- headers (TD §4) ------------------------------------------------------------------------


def connection_tokens(headers: Headers) -> frozenset[bytes]:
    """The header names listed in every `Connection` header, in lower case (H15)."""
    return frozenset(
        token.strip().lower()
        for k, v in headers
        if k.lower() == b"connection"
        for token in v.split(b",")
    )


def _strip(headers: Headers, drop: frozenset[bytes], prefixes: tuple[bytes, ...]) -> Headers:
    """`headers` without the dropped names and prefixes and every header named in
    `Connection`. Order is kept, and a repeated header keeps all its values (H17)."""
    named = connection_tokens(headers)
    return [
        (k, v)
        for k, v in headers
        if (name := k.lower()) not in drop and name not in named and not name.startswith(prefixes)
    ]


def upstream_headers(client_headers: Headers, *, body_len: int, request_id: str) -> Headers:
    """The client's headers for Ollama (TD §4): the strip list removed, then the framing set
    from the buffered body, and the sidecar's sanitised request id in place of the client's."""
    return [
        *_strip(client_headers, _UPSTREAM_DROP, _UPSTREAM_DROP_PREFIXES),
        (b"content-length", str(body_len).encode()),
        (b"x-request-id", request_id.encode()),
    ]


def prune_httpx_defaults(req: httpx.Request, client_headers: Headers) -> None:
    """Delete the defaults httpx added in `build_request`, unless `client_headers` (the headers
    forwarded to Ollama) has them. `host` and the body framing come from httpx too, and stay.
    A `cookie` header is always removed: the cookie jar stores nothing (`NoCookies`), and this
    is the second guard (§2.4)."""
    sent = {k.lower() for k, _ in client_headers}
    for name in HTTPX_DEFAULTS:
        if name not in sent:
            req.headers.pop(name.decode(), None)
    req.headers.pop("cookie", None)


def downstream_headers(
    resp_headers: httpx.Headers, *, gate_value: str, node_id: str, request_id: str
) -> Headers:
    """Ollama's headers for the client (TD §4), then Seriatim's own: each exactly once."""
    return [
        *_strip(resp_headers.raw, _DOWNSTREAM_DROP, _DOWNSTREAM_DROP_PREFIXES),
        (b"x-seriatim-gate", gate_value.encode()),
        (b"x-seriatim-node", node_id.encode()),
        (b"x-request-id", request_id.encode()),
    ]


# --- the end of the response and the in-stream record (TD §3.3 step 5, §4) ----------------

Framing = Literal["ndjson", "sse"]
_RECORD_TYPES: dict[str, Framing] = {"application/x-ndjson": "ndjson", "text/event-stream": "sse"}
_BOUNDARY: dict[Framing, bytes] = {"ndjson": b"\n", "sse": b"\n\n"}


class Unframed(httpx.RemoteProtocolError):
    """The upstream response ended without its framing proving it complete: it had no
    `content-length` or chunking (close-delimited), so a crash would look the same."""


def bodyless(method: str, status: int) -> bool:
    """A response with no body by HTTP's rules: to `HEAD`, or a 1xx, 204 or 304 (S21)."""
    return method == "HEAD" or 100 <= status < 200 or status in (204, 304)


def upstream_framed(method: str, status: int, headers: httpx.Headers, received: int) -> bool:
    """Whether a response whose body iterator ended normally is complete (§3.3 step 5).

    The bodyless rule comes first: a `HEAD` response's `content-length` describes the body a
    `GET` would have had. Otherwise a chunked response is complete (h11 raises if the
    terminating chunk never came, S20), and so is one whose `content-length` the received
    bytes match. A close-delimited response is not."""
    if bodyless(method, status):
        return True
    if "transfer-encoding" in headers:  # h11 accepts only chunked, and checks its end
        return True
    length = headers.get("content-length")
    return length is not None and int(length) == received


def record_framing(resp_headers: httpx.Headers, tail: bytes) -> Framing | None:
    """The record format a broken stream can still end with, or None when a record would
    corrupt the body (§4): only for an NDJSON or SSE body with no `content-length` and no
    content coding, whose last bytes sent end a record (or nothing was sent)."""
    if "content-length" in resp_headers:
        return None
    if resp_headers.get("content-encoding", "identity").strip().lower() != "identity":
        return None
    media = resp_headers.get("content-type", "").split(";")[0].strip().lower()
    framing = _RECORD_TYPES.get(media)
    if framing is None:
        return None
    if tail and not tail.endswith(_BOUNDARY[framing]):
        return None
    return framing


def stream_error_record(framing: Framing, code: str, message: str) -> bytes:
    """One NDJSON line, or one SSE event, carrying a `StreamErrorRecord` (§4)."""
    doc = json.dumps({"error": message, "error_code": code}, separators=(",", ":"))
    if framing == "sse":
        return b"data: " + doc.encode() + b"\n\n"
    return doc.encode() + b"\n"


def _stream_error(outcome: Outcome) -> tuple[ErrorCode, str] | None:
    """The in-stream record's code for an abort after the headers (§5), or None: an internal
    error or a cancellation aborts with no record."""
    if outcome.hold_exceeded:
        return ErrorCode.LOCK_HOLD_EXCEEDED, "the request exceeded the hold limit"
    if isinstance(outcome.error, httpx.TransportError):
        return ErrorCode.UPSTREAM_STREAM_BROKEN, "Ollama's response stream broke"
    return None


# --- the request body and the watcher (TD §3.3 steps 1 and 2) -------------------------------


class ClientGone(Exception):
    """The client disconnected while sending the request body."""


class BodyTooLarge(Exception):
    """The request body grew past `max_body_bytes` while it was being read (413)."""


class BodyTimeout(Exception):
    """The request body had not fully arrived within `body_timeout_s` (408)."""


def declared_length(headers: Headers) -> int | None:
    """The request's `Content-Length`, or None when it has none (a chunked body). h11 has
    already refused a malformed or conflicting value (H28), so the first one is the value."""
    for k, v in headers:
        if k == b"content-length":
            return int(v)
    return None


def has_body(headers: Headers) -> bool:
    """Whether the request declares a body: chunked, or a `Content-Length` above 0. A reply
    sent before such a body is fully read must close the connection, because uvicorn would
    otherwise keep reading the old body, and take a keep-alive retry's bytes as part of it."""
    if any(k == b"transfer-encoding" for k, _ in headers):
        return True
    length = declared_length(headers)
    return length is not None and length > 0


async def read_body(receive: Receive, *, limit: int, timeout_s: float) -> bytes:
    """The whole request body, as received (REQ-20). Raises `BodyTooLarge` as soon as more
    than `limit` bytes have arrived, `BodyTimeout` if the body is not complete `timeout_s`
    after the first `receive()` (a total bound, not reset by each chunk: E8), and `ClientGone`
    on a disconnect."""
    chunks: list[bytes] = []
    size = 0
    with anyio.move_on_after(timeout_s):
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                raise ClientGone
            chunk = msg.get("body", b"")
            size += len(chunk)
            if size > limit:
                raise BodyTooLarge
            chunks.append(chunk)
            if not msg.get("more_body", False):
                return b"".join(chunks)
    raise BodyTimeout  # the loop leaves only by return, raise or the deadline


@dataclass
class Outcome:
    """What happened to one request. The `finally` decides on these flags alone (§3.3)."""

    sent_upstream: bool = False
    started: bool = False
    upstream_done: bool = False
    disconnected: bool = False
    hold_exceeded: bool = False
    error: Exception | None = None
    resp: httpx.Response | None = None
    status: int | None = None
    tail: bytes = b""
    body_pending: bool = False  # a declared body not yet fully read: a reply must close


async def watch_disconnect(receive: Receive, outcome: Outcome, scope: anyio.CancelScope) -> None:
    """The only caller of `receive()` once the body is read. A disconnect is recorded first,
    then the exchange is cancelled, so the cancellation is never mistaken for the hold limit."""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            outcome.disconnected = True
            scope.cancel()
            return


async def _uninterrupted(work: Coroutine[Any, Any, None]) -> None:
    """Run `work` to its end in a task of its own, whatever cancels this task meanwhile (S12).

    anyio's shield holds off anyio's own cancellations only. A server shutting down cancels
    the request's task with asyncio's `Task.cancel()`, and a second such call would land
    inside the shielded `finally`, cutting the close proof short and skipping the end of the
    lease. Here it lands on `asyncio.shield`, the work goes on, and the cancellation is
    raised again once the work has ended. The sidecar runs on uvicorn's asyncio loop."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


# --- the proxy (TD §3.3) --------------------------------------------------------------------


class Proxy:
    """An ASGI app for every non-Seriatim route."""

    def __init__(
        self,
        settings: Settings,
        gate: Gate,
        client: httpx.AsyncClient,
        log: RequestLog,
        errors: ErrorFactory,
    ) -> None:
        self.settings = settings
        self.gate = gate
        self.client = client  # shared by ungated routes
        self.log = log
        self.errors = errors

    async def _reply(
        self,
        send: Send,
        code: ErrorCode,
        message: str,
        *,
        gate_value: str,
        rid: str,
        close: bool,
        **kw: Any,
    ) -> int:
        """Send an error reply. `close` adds `Connection: close`, for a reply sent before the
        request body was fully read (S1-8)."""
        status, headers, body = self.errors.response(
            code, message, gate_value=gate_value, request_id=rid, **kw
        )
        if close:
            headers = [*headers, (b"connection", b"close")]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return status

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send, route: Route, request_id: str
    ) -> None:
        rid = request_id
        where = {
            "request_id": rid,
            "method": scope["method"],
            "path": scope["raw_path"].decode("latin-1"),
            "kind": route.kind.value,
            "scope": route.scope.value,
            "known": route.known,
        }
        outcome = Outcome(body_pending=has_body(scope["headers"]))
        lease: Lease | None = None
        req_client: httpx.AsyncClient | None = None
        length = declared_length(scope["headers"])
        if length is not None and length > self.settings.max_body_bytes:  # before admission (B7)
            status = await self._reply(
                send,
                ErrorCode.BODY_TOO_LARGE,
                "the request body is too large",
                gate_value="ungated",
                rid=rid,
                close=True,
            )
            self.log.emit(**where, outcome="rejected", status=status, slot_end="none")
            return
        if route.kind is Kind.GATED:
            r = self.gate.try_acquire(where["path"], rid)
            if isinstance(r, Busy):  # the body is never read (B3)
                status = await self._reply(
                    send,
                    ErrorCode.GPU_BUSY,
                    "GPU busy: another request is running on this node",
                    gate_value="ungated",
                    rid=rid,
                    close=outcome.body_pending,
                    retry_after=r.retry_after_s,
                    busy_for_s=round(r.busy_for_s, 1),
                )
                self.log.emit(**where, outcome="busy", status=status, slot_end="none")
                return
            if isinstance(r, Starting):
                status = await self._reply(
                    send,
                    ErrorCode.NODE_STARTING,
                    "the node is starting",
                    gate_value="ungated",
                    rid=rid,
                    close=outcome.body_pending,
                    retry_after=r.retry_after_s,
                )
                self.log.emit(**where, outcome="starting", status=status, slot_end="none")
                return
            lease = r
        try:
            if lease is not None:
                req_client = new_gated_client(self.settings)  # inside the try (E16)
            with anyio.move_on_after(self._hold_remaining(lease)) as hold:
                async with anyio.create_task_group() as tg:
                    await self._exchange(scope, receive, send, rid, lease, req_client, outcome, tg)
                    tg.cancel_scope.cancel()
            outcome.hold_exceeded = hold.cancelled_caught and not outcome.disconnected
        except Exception as exc:  # defence in depth: _exchange never raises one
            outcome.error = outcome.error or exc
        finally:
            with anyio.CancelScope(shield=True):
                await _uninterrupted(self._end(send, where, outcome, lease, req_client))

    async def _end(
        self,
        send: Send,
        where: dict[str, Any],
        outcome: Outcome,
        lease: Lease | None,
        req_client: httpx.AsyncClient | None,
    ) -> None:
        """The `finally` of §3.3: (a) prove upstream closure, (b) end the lease, (c) talk to
        the client, then log. Each awaited step has its own `close_timeout_s` bound."""
        rid = where["request_id"]
        close_failed = False
        # (a) prove upstream closure, within a time bound
        closed = True
        if req_client is not None:
            closed = False
            with anyio.move_on_after(self.settings.close_timeout_s):
                try:
                    await req_client.aclose()
                    closed = True
                except Exception:
                    pass
            close_failed = not closed
        elif outcome.resp is not None:  # ungated: the shared client; no slot at stake
            with anyio.move_on_after(self.settings.close_timeout_s) as ungated_close:
                try:
                    await outcome.resp.aclose()
                except Exception:
                    close_failed = True
            close_failed = close_failed or ungated_close.cancelled_caught
        # (b) end the lease: synchronous, so nothing can block or skip it
        slot_end = "none"
        if lease is not None:
            if not closed:
                lease.stuck()
                slot_end = "stuck"
            elif outcome.upstream_done or not outcome.sent_upstream:
                lease.finish(clean=True)
                slot_end = "free"
            else:
                lease.finish(clean=False)
                slot_end = "draining"
        # (c) only now talk to the client; a failure here never matters to the slot
        write_failed: str | None = None
        with anyio.move_on_after(self.settings.close_timeout_s) as finishing:
            try:
                await self._finish_response(send, outcome, lease, rid)
            except Exception as exc:
                write_failed = type(exc).__name__
        if finishing.cancelled_caught:
            write_failed = "timeout"
        if write_failed is not None:  # §3.3 line 40: the client is gone or stalled
            self.log.emit(
                level="DEBUG", event="client_write_failed", request_id=rid, reason=write_failed
            )
        result, level = _log_outcome(outcome)
        if result == "internal_error" and outcome.error is not None:
            # §5: the traceback goes to the log only, never to the client (E5)
            where["traceback"] = "".join(traceback.format_exception(outcome.error))
        if close_failed:  # REQ-11: CRITICAL when a slot is at stake, else ERROR
            # Only a gated request can be CRITICAL already (the hold limit), so the slot
            # alone decides.
            where["upstream_close_failed"] = True
            level = "CRITICAL" if lease is not None else "ERROR"
        self.log.emit(
            level=level, **where, outcome=result, status=outcome.status, slot_end=slot_end
        )

    def _hold_remaining(self, lease: Lease | None) -> float | None:
        """Seconds left before the hold limit; None (no limit) for an ungated request."""
        return None if lease is None else lease.remaining_s()

    async def _exchange(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        rid: str,
        lease: Lease | None,
        req_client: httpx.AsyncClient | None,
        outcome: Outcome,
        tg: anyio.abc.TaskGroup,
    ) -> None:
        """Read the body, send it upstream and relay the response. Records every failure in
        `outcome` instead of raising, so the task group only ever sees cancellations."""
        try:
            body = await read_body(
                receive,
                limit=self.settings.max_body_bytes,
                timeout_s=self.settings.body_timeout_s,
            )
            outcome.body_pending = False
            tg.start_soon(watch_disconnect, receive, outcome, tg.cancel_scope)
            client = req_client if req_client is not None else self.client
            query = scope["query_string"]
            url = client.base_url.copy_with(
                raw_path=scope["raw_path"] + (b"?" + query if query else b"")
            )
            headers = upstream_headers(scope["headers"], body_len=len(body), request_id=rid)
            req = client.build_request(scope["method"], url, headers=headers, content=body)
            prune_httpx_defaults(req, headers)
            outcome.sent_upstream = True
            try:
                resp = await client.send(req, stream=True)
            except _NOT_SENT:
                outcome.sent_upstream = False
                raise
            outcome.resp = resp
            await send(
                {
                    "type": "http.response.start",
                    "status": resp.status_code,
                    "headers": downstream_headers(
                        resp.headers,
                        gate_value="acquired" if lease is not None else "ungated",
                        node_id=self.settings.id,
                        request_id=rid,
                    ),
                }
            )
            outcome.started = True
            outcome.status = resp.status_code
            received = 0
            # The raw bytes, as `aiter_raw()` gives them, but without its close at the end:
            # every upstream close is in step (a), where `close_timeout_s` bounds it (H26).
            async for chunk in resp.stream:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                outcome.tail = (outcome.tail + chunk)[-2:]
                received += len(chunk)
            if not upstream_framed(scope["method"], resp.status_code, resp.headers, received):
                raise Unframed("the upstream response was not framed")
            outcome.upstream_done = True
        except ClientGone:
            outcome.disconnected = True
        except Exception as exc:
            outcome.error = exc

    async def _finish_response(
        self, send: Send, outcome: Outcome, lease: Lease | None, rid: str
    ) -> None:
        """Step (c): the final body message of a clean response, an error reply, or nothing
        (an abort). The lease has already ended."""
        if outcome.disconnected:
            return  # nobody to answer
        if outcome.started:
            if outcome.upstream_done and outcome.error is None and not outcome.hold_exceeded:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            # An abort: a record if the framing allows one, then return without the final
            # message, so uvicorn closes the connection and the client sees it cut short.
            failure = _stream_error(outcome)
            if failure is None:
                return
            assert outcome.resp is not None  # set before the headers were sent
            framing = record_framing(outcome.resp.headers, outcome.tail)
            if framing is not None:
                record = stream_error_record(framing, failure[0].value, failure[1])
                await send({"type": "http.response.body", "body": record, "more_body": True})
            return
        gate_value = "acquired" if lease is not None else "ungated"
        reply = _error_reply(outcome)
        if reply is None:
            return  # an external cancellation: nothing to say
        code, message, extra = reply
        if code is ErrorCode.NODE_SATURATED:
            extra = {"retry_after": self.settings.retry_after_s}
        outcome.status = await self._reply(
            send, code, message, gate_value=gate_value, rid=rid, close=outcome.body_pending, **extra
        )


def _error_reply(outcome: Outcome) -> tuple[ErrorCode, str, dict[str, Any]] | None:
    """The error response for a request that ended before `http.response.start` (§5)."""
    err = outcome.error
    if outcome.hold_exceeded:
        return ErrorCode.LOCK_HOLD_EXCEEDED, "the request exceeded the hold limit", {}
    if isinstance(err, BodyTooLarge):
        return ErrorCode.BODY_TOO_LARGE, "the request body is too large", {}
    if isinstance(err, BodyTimeout):
        return ErrorCode.REQUEST_BODY_TIMEOUT, "the request body did not arrive in time", {}
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ErrorCode.UPSTREAM_UNAVAILABLE, "Ollama is not reachable", {}
    if isinstance(err, httpx.PoolTimeout):
        return ErrorCode.NODE_SATURATED, "the node has no free upstream connection", {}
    if isinstance(err, httpx.TransportError):
        return ErrorCode.UPSTREAM_PROTOCOL_ERROR, "Ollama's response was broken", {}
    if err is not None:
        return ErrorCode.INTERNAL, "internal error", {}
    return None


def _log_outcome(outcome: Outcome) -> tuple[str, str]:
    """The log line's `outcome` and level (§5)."""
    err = outcome.error
    if outcome.disconnected:
        return "client_disconnect", "INFO"
    if outcome.hold_exceeded:
        return "hold_exceeded", "CRITICAL"
    if isinstance(err, (BodyTooLarge, BodyTimeout)):
        return "rejected", "INFO"
    if isinstance(err, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "upstream_unavailable", "INFO"
    if isinstance(err, httpx.PoolTimeout):
        return "saturated", "INFO"
    if isinstance(err, httpx.TransportError):
        return "upstream_error", "INFO"
    if err is not None:
        return "internal_error", "ERROR"
    if outcome.upstream_done:
        return "ok", "INFO"
    return "cancelled", "INFO"
