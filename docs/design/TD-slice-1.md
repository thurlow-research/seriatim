# TD — Slice 1: contract, sidecar gate, 429 and passthrough

**Status:** draft for review. **Date:** 2026-11-16.
**Implements:** SPEC REQ-1 to REQ-7, REQ-10 to REQ-12, REQ-15 (pre-auth form), REQ-18 (partly),
REQ-20 to REQ-26. **Architecture:** ADR-001 D1 to D4, D7, D8.
**Not in this slice:** auth and TLS (S2), `num_ctx` policy (S3), `/seriatim/status`, memory
and the exposure self-check (S4), deploy (S5 and S6), the gateway (S7 and later).

Until S2 lands, the sidecar has no authentication. It therefore refuses to listen on a
non-loopback address unless `SERIATIM_ALLOW_UNAUTHENTICATED=true` is set (SPEC REQ-15). Only
the PPE sets that flag.

## 1. Files (15)

| # | path | purpose |
|---|---|---|
| 1 | `contract/openapi.yaml` | ADR-001 Appendix A, committed and linted |
| 2 | `node/pyproject.toml` | the `seriatim-node` package; dependencies; console script `seriatim-node = seriatim_node.app:main` |
| 3 | `node/seriatim_node/__init__.py` | `__version__` |
| 4 | `node/seriatim_node/config.py` | `Settings`, loading and validation |
| 5 | `node/seriatim_node/gate.py` | `Gate`, `Lease`, `classify()` |
| 6 | `node/seriatim_node/proxy.py` | `Proxy`, the ASGI passthrough with streaming, disconnects, deadline and error mapping |
| 7 | `node/seriatim_node/app.py` | the Starlette app, error envelopes, `/seriatim/health`, logging, `main()` |
| 8 | `tests/node/conftest.py` | the real-socket harness: the sidecar and a fake Ollama on ephemeral ports, plus a fixture replayer |
| 9 | `tests/node/test_config.py` | C1 to C8 |
| 10 | `tests/node/test_gate.py` | K1, G1 to G9 |
| 11 | `tests/node/test_http.py` | H1 to H7, B1 to B4, E1 to E5, HL1 and HL2, L1 |
| 12 | `tests/node/test_streaming.py` | S1 to S8 |
| 13 | `tests/node/test_contract.py` | K-OAS1 to K-OAS3 |
| 14 | `tests/behaviour/test_sidecar.py` | P1 to P6 (black-box; run in the PPE) |
| 15 | `ppe/compose.yaml` (modified) | adds the `node-a` sidecar container in `ollama-a`'s network namespace |

Dependencies (pinned in `uv.lock` by S0a's workspace):
- **Runtime:** `starlette`, `uvicorn[standard]` (h11; `httptools` is allowed), `httpx`,
  `anyio`, `pydantic-settings`.
- **Test:** `pytest`, `pytest-cov`, `hypothesis`, `openapi-spec-validator`, `jsonschema`,
  `pyyaml`, `mutmut`.

## 2. Modules and functions

### 2.1 `config.py`

```python
class Settings(BaseSettings):          # pydantic-settings; env prefix SERIATIM_, TOML file source
    listen_host: str = "127.0.0.1"
    listen_port: int = 11434           # 1..65535
    ollama_url: AnyHttpUrl = "http://127.0.0.1:11435"
    node_id: str = "node"              # ^[a-z0-9][a-z0-9-]{0,31}$ ; opaque label, never a hostname
    slots: int = 1                     # >= 1
    retry_after_s: int = 15            # 1..3600
    max_hold_s: float = 1800           # > 0
    connect_timeout_s: float = 5       # > 0
    max_body_bytes: int = 64 * 2**20   # >= 1024
    body_timeout_s: float = 30         # > 0
    health_probe_timeout_s: float = 2  # > 0
    allow_unauthenticated: bool = False
    ungated: list[str] = DEFAULT_UNGATED   # "METHOD /path" or "METHOD /prefix/*"
    log_level: Literal["DEBUG","INFO","WARNING","ERROR"] = "INFO"
    model_config = SettingsConfigDict(env_prefix="SERIATIM_", extra="forbid")

class ConfigError(Exception): ...      # message names the offending key

def load_settings(environ: Mapping[str, str] = os.environ) -> Settings
```

`load_settings`:
1. If `SERIATIM_CONFIG` is set, read that file as TOML. A missing or unreadable file raises
   `ConfigError("SERIATIM_CONFIG: …")`.
2. Build `Settings`, with environment values taking precedence over the file.
3. Reject any `SERIATIM_*` variable that is not a field name. pydantic's `extra="forbid"` does
   not cover the environment, so `load_settings` checks `environ` itself and raises for each
   unknown variable.
4. Check that `ollama_url` resolves to a loopback literal (`127.0.0.0/8`, `::1` or
   `localhost`), without doing any DNS lookup.
5. If `listen_host` is not loopback and `allow_unauthenticated` is false, raise `ConfigError`.
6. Turn every pydantic `ValidationError` into a `ConfigError` that names the
   `SERIATIM_<FIELD>`.

There is no `workers` setting (REQ-21). `main()` always runs one uvicorn server in-process.

### 2.2 `gate.py`

```python
@dataclass(frozen=True)
class HolderInfo: path: str; request_id: str; started_at: float  # monotonic + wall clock pair

class Lease:
    def release(self) -> bool           # idempotent; True only on the first call
    @property
    def held(self) -> bool
    info: HolderInfo
    deadline: float                     # monotonic time = acquired + max_hold_s

class Gate:
    def __init__(self, slots: int, max_hold_s: float, clock: Callable[[], float] = time.monotonic)
    def try_acquire(self, path: str, request_id: str) -> Lease | None   # synchronous, never awaits
    @property
    def in_use(self) -> int
    def holders(self) -> list[HolderInfo]
    def overdue(self, grace_s: float = 30) -> list[HolderInfo]          # for health (REQ-18)

def classify(method: str, path: str, ungated: Sequence[str]) -> Literal["gated", "ungated"]
```

`classify` treats `GET`, `HEAD` and `OPTIONS` as ungated. Any other method is ungated only if
`(METHOD, path)` matches an entry in `ungated`: an exact match, or for a pattern ending in
`/*`, a prefix match on a path-segment boundary. Before matching, the path is normalised: a
trailing `/` is removed, and empty segments are collapsed. Any path that still contains `..`
or `%2e` segments after that is **gated**, so a crafted path fails safe. Everything else is
gated.

`DEFAULT_UNGATED` = `POST /api/show`, `POST /api/pull`, `POST /api/push`, `POST /api/create`,
`POST /api/copy`, `POST /api/blobs/*`, `HEAD /api/blobs/*`, `DELETE /api/delete`.

### 2.3 `proxy.py`

```python
class Proxy:                                         # an ASGI app: await proxy(scope, receive, send)
    def __init__(self, settings: Settings, gate: Gate, client: httpx.AsyncClient, log: RequestLog)
    async def __call__(self, scope, receive, send) -> None

# internal helpers (module-level, unit-testable, pure where possible)
def upstream_headers(scope_headers: list[tuple[bytes, bytes]], body_len: int) -> list[tuple[bytes, bytes]]
def downstream_headers(resp_headers: httpx.Headers, gate_state: str) -> list[tuple[bytes, bytes]]
def stream_error_record(content_type: str | None, code: str, message: str) -> bytes | None
async def read_body(receive, limit: int, timeout_s: float) -> bytes   # raises BodyTooLarge / BodyTimeout / ClientGone
```

### 2.4 `app.py`

```python
def create_app(settings: Settings, *, client: httpx.AsyncClient | None = None) -> Starlette
def error_response(code: ErrorCode, message: str, *, retry_after: int | None = None, **extra) -> JSONResponse
class ErrorCode(StrEnum): ...          # exactly the contract enum (K-OAS3)
class RequestLog: def emit(self, **fields) -> None   # one JSON line per request to stdout (REQ-22)
def main() -> None                     # load_settings → on ConfigError print + exit 2 → uvicorn.run(app, workers=1)
```

Routes:
- `Route("/seriatim/health", health)`, then `Mount("/", app=proxy)`.
- Any other `/seriatim/*` path returns 404 from Seriatim. It is never forwarded to Ollama.
- The lifespan creates one `httpx.AsyncClient` and closes it on shutdown. The client has
  `base_url=ollama_url`, `timeout=Timeout(connect=connect_timeout_s, read=None, write=None,
  pool=connect_timeout_s)`, `limits=Limits(max_keepalive_connections=8)`, `trust_env=False`
  and `follow_redirects=False`.
- `trust_env=False` matters: a `HTTP(S)_PROXY` in the service's environment must not send
  loopback traffic through a proxy.

## 3. Lock and concurrency design

### 3.1 Invariants

1. `0 ≤ gate.in_use ≤ slots`, at every point between two `await`s.
2. Each successful `try_acquire` is matched by exactly one effective `release`.
3. A lease is released only **after** the upstream response, if any, has been closed
   (REQ-10, REQ-11).
4. Nothing except the request's own `Proxy.__call__` frame releases its lease. There is no
   reaper.

### 3.2 Why admission is atomic

asyncio runs one coroutine at a time, and a coroutine is only switched out at an `await`.
`try_acquire` is a plain function. It compares `in_use < slots`, increments `in_use` and builds
the `Lease`, with no `await` in between. Two requests therefore cannot both see the last free
slot. uvicorn runs a single event loop on one thread (REQ-21). Test G7 checks this with 1000
concurrent callers, and G9 checks the invariant with property-based sequences.

`Lease.release` sets `_released` and decrements `in_use` once. Calling it again returns
`False` and changes nothing (G5).

### 3.3 Request lifecycle in `Proxy.__call__`

```
1  request_id ← sanitised X-Request-Id or uuid4 hex
2  kind ← classify(method, path, settings.ungated)
3  if kind == gated:
4      lease ← gate.try_acquire(path, request_id)
5      if lease is None → send 429 GPU_BUSY (Retry-After, busy_for_s = oldest holder age)  # body never read
6                         log outcome=busy; return
7  try:
8      with anyio.fail_after(lease.deadline - now) if gated else nullcontext():     # REQ-12
9          body ← await read_body(receive, max_body_bytes, body_timeout_s)          # 413 / 408 / client gone
10         async with anyio.create_task_group() as tg:
11             tg.start_soon(watch_disconnect, receive, tg.cancel_scope)             # cancels on http.disconnect
12             req ← client.build_request(method, path+query, headers=upstream_headers(...), content=body)
13             resp ← await client.send(req, stream=True)                            # 502 on connect/protocol error
14             await send(http.response.start: resp.status, downstream_headers(resp, kind))
15             started ← True
16             async for chunk in resp.aiter_raw():                                  # raw bytes: no decode
17                 await send(http.response.body: chunk, more_body=True)
18             await send(http.response.body: b"", more_body=False)
19             tg.cancel_scope.cancel()                                              # stop the watcher
20  except <mapped errors> → see §5 (before or after `started`)
21  finally:
22     if resp: await resp.aclose()      # closes the upstream socket → Ollama cancels generation
23     if lease: lease.release()         # strictly after line 22 (invariant 3)
24     log.emit(...)
```

Notes:
- **Line 5 never reads the body.** A busy client that has sent only the request headers gets
  its 429 at once (B3). uvicorn discards any unread body.
- **Line 9 runs under the deadline.** A slow uploader therefore can't hold a slot past
  `max_hold_s`. It is also limited separately by `body_timeout_s`.
- **Line 11.** After the body is fully read, the only ASGI message left to arrive on `receive`
  is `http.disconnect`. `watch_disconnect` loops on `receive()` and, when it sees
  `http.disconnect`, cancels the task group. That covers both phases:
  - waiting for headers, which for `stream:false` lasts the whole generation;
  - streaming the body.

  This fixes the predecessor's failure mode, where an abandoned non-streaming request kept the
  GPU busy until Ollama finished.
- **Line 13 sends `stream=True` for every request.** The proxy never buffers a response, so a
  large non-streamed answer is also forwarded as it arrives.
- **Ungated requests** (line 3 false) go through the same path with no lease and no deadline.
  `/api/pull` progress streams the same way (H7, P4).
- **Exceptions inside the task group** come out as an `ExceptionGroup`. The handler unwraps it
  with `except*` and maps the first error by the priority in §5. Cancellation caused by our own
  scope (the disconnect) is recognised through `tg.cancel_scope.cancel_called` and logged as
  `client_disconnect`.

### 3.4 Health

`GET /seriatim/health` returns:
- 200 `{"status":"ok"}`, when Ollama answers `GET /api/version` with 200 within
  `health_probe_timeout_s` and `gate.overdue()` is empty;
- otherwise 503 `{"status":"degraded","reasons":[…]}`, with the reasons `ollama_unreachable`
  and `slot_overdue`.

The probe uses the shared client, is ungated and is not logged at `INFO`. `ollama_exposed`
arrives in S4.

## 4. Streaming passthrough

- **Bytes.** The proxy uses `resp.aiter_raw()`, so there is no content decoding. Chunks are
  forwarded as they arrive, never collected. The request body is the exact bytes received from
  the client.
- **Headers.** As in ADR-001 §5.3.
  - `upstream_headers` drops the hop-by-hop headers (including any named in `Connection`),
    `host`, `authorization`, `x-api-key`, `x-seriatim-*` and `content-length`, then sets
    `content-length` from `body_len`.
  - The `httpx.AsyncClient` is built with `headers={}`. Per request, the proxy removes the
    defaults httpx would add (`accept-encoding`, `user-agent`, `accept`), unless the client
    sent them. Ollama therefore sees the client's header set, minus the removed headers.
  - `downstream_headers` drops the hop-by-hop headers and adds `x-seriatim-gate`.
    `content-length` and `content-encoding` pass unchanged. Without a length, uvicorn sends
    chunked encoding.
- **Status.** Ollama's status is forwarded unchanged, including 4xx and 5xx bodies (H5).
- **Errors after `started` (REQ-24).** `stream_error_record(content_type, code, msg)` returns:
  - for `application/x-ndjson`: `json.dumps({"error": msg, "error_code": code}) + "\n"`;
  - for `text/event-stream`: `"data: " + json.dumps(...) + "\n\n"`;
  - for anything else, `None`, and nothing is injected.

  The proxy sends the record if there is one (`more_body=True`), then **raises** out of the
  ASGI call without sending the final `more_body=False`. uvicorn then closes the connection
  without the chunked terminator, so the client sees an incomplete response. This is tested in
  S6 and S7 by the client's `httpx.RemoteProtocolError` or `ReadError`.
- **Backpressure.** `await send(...)` blocks while the client reads slowly. The upstream read
  pauses with it (TCP flow control), so a slow reader holds the slot until it finishes or
  `max_hold_s` passes (T5).

## 5. Error mapping

"Before" means before `http.response.start` was sent; "after" means after it.

| condition (exception) | before | after | lease | log `outcome` |
|---|---|---|---|---|
| no free slot | 429 `GPU_BUSY`, `Retry-After` | — | none taken | `busy` |
| body > `max_body_bytes` (`BodyTooLarge`, from `Content-Length` early, or while counting) | 413 `BODY_TOO_LARGE` | — | released | `rejected` |
| body stalls (`BodyTimeout`) | 408 `REQUEST_BODY_TIMEOUT` | — | released | `rejected` |
| client gone while sending the body (`ClientGone`) | nothing sent | — | released | `client_disconnect` |
| `httpx.ConnectError`, `ConnectTimeout`, `PoolTimeout` | 502 `UPSTREAM_UNAVAILABLE` | — | released | `upstream_unavailable` |
| `httpx.RemoteProtocolError`, `ReadError`, `WriteError` | 502 `UPSTREAM_PROTOCOL_ERROR` | stream record `UPSTREAM_STREAM_BROKEN` + abort | released after `aclose` | `upstream_error` |
| deadline (`TimeoutError` from `fail_after`) | 504 `LOCK_HOLD_EXCEEDED` | stream record `LOCK_HOLD_EXCEEDED` + abort | released after `aclose` | `hold_exceeded` (`CRITICAL`) |
| client disconnect (our cancel scope) | nothing sent | nothing sent | released after `aclose` | `client_disconnect` |
| Ollama non-2xx | passed through unchanged | — | released at the end | `ok` (with `status`) |
| any other `Exception` | 500 `INTERNAL` (message is generic; the traceback goes only to the log) | abort | released after `aclose` | `internal_error` (`ERROR`) |

Every 4xx and 5xx produced here is built by `error_response()`, which fills `source:
"sidecar"`, `retryable` from the table in ADR-001 §5.4 and `node: settings.node_id`.
`retryable` is never decided at the call site.

## 6. Configuration

All keys are `SERIATIM_<FIELD>`. They can also be set in the TOML file named by
`SERIATIM_CONFIG`, using the same names in lower case without the prefix.

| variable | default | rule |
|---|---|---|
| `SERIATIM_LISTEN_HOST` | `127.0.0.1` | if not loopback, needs `ALLOW_UNAUTHENTICATED` in S1 |
| `SERIATIM_LISTEN_PORT` | `11434` | 1–65535 |
| `SERIATIM_OLLAMA_URL` | `http://127.0.0.1:11435` | loopback literal only |
| `SERIATIM_NODE_ID` | `node` | `^[a-z0-9][a-z0-9-]{0,31}$` |
| `SERIATIM_SLOTS` | `1` | ≥ 1 |
| `SERIATIM_RETRY_AFTER_S` | `15` | 1–3600 |
| `SERIATIM_MAX_HOLD_S` | `1800` | > 0 |
| `SERIATIM_CONNECT_TIMEOUT_S` | `5` | > 0 |
| `SERIATIM_MAX_BODY_BYTES` | `67108864` | ≥ 1024 |
| `SERIATIM_BODY_TIMEOUT_S` | `30` | > 0 |
| `SERIATIM_HEALTH_PROBE_TIMEOUT_S` | `2` | > 0 |
| `SERIATIM_ALLOW_UNAUTHENTICATED` | `false` | logs `WARNING` at startup and hourly when true |
| `SERIATIM_UNGATED` | see §2.2 | JSON list of `"METHOD /path"` strings |
| `SERIATIM_LOG_LEVEL` | `INFO` | enum |
| `SERIATIM_CONFIG` | unset | path to TOML; must exist if set |

S0a writes `.env.example` with these keys and their defaults already in it, so this slice does
not touch that file. The PPE
compose file sets `LISTEN_HOST=0.0.0.0` and `ALLOW_UNAUTHENTICATED=true` inside the container
network namespace, with the published port bound to `127.0.0.1` on the host.

## 7. Test harness (`tests/node/conftest.py`)

- **`fake_ollama` fixture.** A real uvicorn server on `127.0.0.1:0`, running a small ASGI app
  that tests script per route.
  - Each request is recorded as raw method, path, query, headers and body bytes.
  - Scripted behaviours: `replay(fixture_id)` (from the captured file); `hold(event)` (send
    headers and/or the first chunk, then wait on an `asyncio.Event`); `stall_headers()`;
    `reset_mid_stream(after_n_chunks)`; `status(code, body)`; and `observe_disconnect()`,
    which sets an event when the sidecar closes the connection.
- **`sidecar` fixture.** A real uvicorn server for `create_app(settings)` on `127.0.0.1:0`,
  with `ollama_url` pointing at `fake_ollama`. A settings factory lets each test override
  slots, timeouts and so on.
- **Clients.** Tests use `httpx.AsyncClient`, and raw `asyncio` sockets for B3 and E4, where
  headers and body must be sent at different times.
- **Timing assertions** use `time.perf_counter()`. Bounds are deliberately loose in unit tests
  (100 ms on a laptop, 500 ms when `CI=true`), and strict in the PPE.
- **No sleeps for synchronisation.** Tests wait on events from `fake_ollama`, with a timeout
  of at most 5 s each, so a hang fails the test instead of passing it.

## 8. Tests to write (exact)

Each test names the requirement it proves. All run in CI on `ubuntu-latest`. The non-PPE tests
also run on `windows-latest` and `macos-latest`.

### 8.1 `test_config.py`

| id | test | REQ |
|---|---|---|
| C1 | `load_settings({})` gives the defaults in §6 exactly | 23 |
| C2 | A TOML file sets `slots=2`, the env sets `SERIATIM_SLOTS=3` → 3; file-only keys are applied | 23 |
| C3 | `SERIATIM_SLOTS=abc`, `=0`, `=-1`; `RETRY_AFTER_S=0`; `MAX_HOLD_S=0` → each raises `ConfigError` whose message contains the variable name | 23, 2 |
| C4 | `OLLAMA_URL` set to `http://192.0.2.5:11435`, `http://example.com:11435` or `http://0.0.0.0:11435` → `ConfigError`; `http://[::1]:11435` and `http://localhost:11435` are accepted | 1 |
| C5 | `LISTEN_HOST=0.0.0.0` without `ALLOW_UNAUTHENTICATED` → `ConfigError`; with it → accepted, and a `WARNING` is captured | 15 |
| C6 | An unknown `SERIATIM_SLOTZ=1` → `ConfigError` naming `SERIATIM_SLOTZ` | 23 |
| C7 | `SERIATIM_CONFIG=/nonexistent.toml` → `ConfigError`; a malformed TOML file → `ConfigError` | 23 |
| C8 | `main()` with invalid config exits with code 2 and prints the key; `uvicorn.run` is called with `workers=1` (monkeypatched) | 21, 23 |

### 8.2 `test_gate.py`

| id | test | REQ |
|---|---|---|
| K1 | Table-driven `classify`: every `DEFAULT_UNGATED` entry is ungated; `GET`/`HEAD`/`OPTIONS` of any path are ungated; each gated path in REQ-5 is gated; `POST /api/unknown`, `PUT /api/chat`, `POST /api/blobs` (no digest), `POST /api/show/../chat`, `POST /api//chat`, `POST /api/chat/` are gated as the normalisation rules say; a custom `ungated` list is honoured | 5 |
| G1 | An idle gate (slots 1): `try_acquire` returns a held lease; `in_use == 1` | 2 |
| G2 | slots 1, one lease held: `try_acquire` returns `None`; `in_use == 1` | 2, 3 |
| G3 | slots 2: two leases, the third is `None` | 2 |
| G4 | After `release()`, `in_use == 0` and `try_acquire` succeeds | 10 |
| G5 | `release()` twice: first `True`, second `False`; `in_use` never goes below 0; with slots 1, a second lease cannot be obtained by double-releasing an old one | 10, 11 |
| G6 | `holders()` gives the path, request id and start time; `overdue()` lists a lease whose deadline plus grace has passed (injected clock), and only then | 17, 18 |
| G7 | 1000 tasks call `try_acquire` concurrently through `asyncio.gather` (slots 1) → exactly 1 lease | 2 |
| G8 | `Gate(slots=0)` raises `ValueError` | 2 |
| G9 | Hypothesis: a random sequence of acquire and release on random leases (slots 1–4) keeps `0 ≤ in_use ≤ slots`, and `in_use` equals the number of held leases | 2, 10 |

### 8.3 `test_http.py`

| id | test | REQ |
|---|---|---|
| H1 | A captured non-streaming `/api/chat` replayed: the client's status, `content-type` and body bytes equal the fixture; `x-seriatim-gate: acquired` | 6 |
| H2 | A body with unusual JSON (key order `{"options":{…},"model":…}`, extra whitespace, `é` escapes, `1.0e0` numbers, an unknown field `"x_future": [1]`, `num_ctx: 30000`, `top_k`, `seed`) → `fake_ollama` records **identical bytes**, and the same `Content-Length` | 6, 7 |
| H3 | The client sends `Authorization`, `X-API-Key`, `Connection: x-foo`, `X-Foo`, `Keep-Alive`, `X-Seriatim-Node`, `Host: evil` → upstream gets none of the removed headers, `Host` is the upstream, `X-Foo` is removed (named in `Connection`), and `X-Custom` is kept | 6, 13 |
| H4 | `GET /api/tags?x=1&y=%20` → upstream path and query are identical | 6 |
| H5 | Upstream 404 `{"error":"model \"nope\" not found"}` → the client gets a 404 with an identical body; the lease is released | 6, 25 |
| H6 | While a gated request is held, `GET /api/tags` → 200 (ungated), `x-seriatim-gate: ungated` | 5 |
| H7 | While a gated request is held, `POST /api/pull` replaying captured pull progress → 200, the stream is identical | 5, 6 |
| B1 | Request 1 is held at upstream; request 2 (`/api/chat`) → 429 within the bound; `Retry-After: 15`; the body validates against `ErrorEnvelope`, with `error_code: GPU_BUSY`, `retryable: true`, `retry_after: 15`, `source: sidecar` and `busy_for_s ≥ 0`; `fake_ollama` recorded **one** request | 3, 4, 25 |
| B2 | Release request 1 → a new request 2 gets 200 | 10 |
| B3 | Raw socket: request 1 held; request 2 sends only the headers, with `Content-Length: 10485760` → the 429 arrives without any body being sent | 3 |
| B4 | slots 2: two held requests proceed, the third gets 429; after one finishes, a fourth gets 200 | 2 |
| E1 | `ollama_url` points at a closed port → 502 `UPSTREAM_UNAVAILABLE`; the next request also gets 502 (not 429: the lease was released) | 26, 10 |
| E3 | `max_body_bytes=2048`: a 4096-byte body (with an honest `Content-Length`, and again chunked without one) → 413; upstream received nothing; the lease was released | 20 |
| E4 | `body_timeout_s=0.5`: a raw socket sends half the body and stalls → 408 `REQUEST_BODY_TIMEOUT`; the lease was released | 20 |
| E5 | An unexpected exception inside the proxy (monkeypatch `downstream_headers` to raise) → 500 `INTERNAL`, with a generic message; the traceback is in the log and not in the body; the lease was released | 26 |
| HL1 | Upstream `/api/version` is 200 → health 200 `{"status":"ok"}`, with no other keys | 18 |
| HL2 | Upstream down → 503 `{"status":"degraded","reasons":["ollama_unreachable"]}`; with an injected overdue lease → `slot_overdue` | 18 |
| L1 | After H1, B1 and E1: one JSON log line each, with the required fields (REQ-22); no line contains the body marker string, the `Authorization` value or the `X-API-Key` value | 22, 13 |

E2 (no upstream response headers) is covered by S8.

### 8.4 `test_streaming.py`

| id | test | REQ |
|---|---|---|
| S1 | `fake_ollama` sends NDJSON chunk 1, then waits on an event → the client **receives chunk 1 before** the test sets the event (proves there is no buffering); then the rest arrives | 6 |
| S2 | A captured `stream:true` `/api/chat` NDJSON replay → the client's bytes are identical to the fixture; `content-type: application/x-ndjson` | 6 |
| S3 | A captured `/v1/chat/completions` SSE stream replay → identical bytes, including `data: [DONE]` | 6 |
| S4 | The client closes mid-stream → `fake_ollama` sees the disconnect within 1 s; the gate is free (the next request gets 200); log `client_disconnect` | 10 |
| S5 | Non-streaming request; the client closes while upstream holds before the headers → upstream disconnect is seen within 1 s; the gate is free | 10 |
| S6 | Upstream resets after 2 chunks → the client receives the 2 chunks, then one valid JSON line `{"error":…, "error_code":"UPSTREAM_STREAM_BROKEN"}`, then the read fails with an incomplete-response error (not a clean EOF); the gate is free | 24, 10 |
| S7 | `max_hold_s=1`, upstream streams one chunk and then holds → after about 1 s the client gets the `LOCK_HOLD_EXCEEDED` record and an aborted connection; `fake_ollama` saw the disconnect **before** the gate reports free (order checked by event timestamps); a `CRITICAL` log line | 12, 11 |
| S8 | `max_hold_s=1`, upstream never sends headers → 504 `LOCK_HOLD_EXCEEDED`; upstream disconnect is seen; the gate is free | 12 |

### 8.5 `test_contract.py`

| id | test | REQ |
|---|---|---|
| K-OAS1 | `contract/openapi.yaml` passes `openapi-spec-validator` (OpenAPI 3.1); every path that has `x-seriatim-gate` agrees with `classify()` | 5 (and SPEC goal G7) |
| K-OAS2 | Every error body produced in `test_http.py` and `test_streaming.py` (gathered by a fixture hook) validates against `ErrorEnvelope`; in-stream records validate against `StreamErrorRecord`; health bodies validate against `Health` | 25 |
| K-OAS3 | `set(ErrorCode)` equals the contract's `ErrorCode.enum`, and each code's HTTP status and `retryable` in `app.py` match the table in ADR-001 §5.4 (which is kept machine-readable as `x-seriatim-codes` in the contract) | 25 |

### 8.6 `tests/behaviour/test_sidecar.py` (black-box, PPE)

These tests use only `SERIATIM_BASE_URL`, the PPE's direct Ollama port (for comparison) and the
capture log. They import nothing from `seriatim_node`.

| id | test | REQ |
|---|---|---|
| P1 | `num_ctx` 6144, then 30000, with `top_k`, `seed` and `temperature` → the captured request bytes on the wire equal the sent bytes; `/api/ps` `context_length` equals the request | 6, 7 |
| P2 | Two concurrent `/api/chat` → one 200 and one 429 with `Retry-After`; p99 latency of the 429 under 100 ms over 20 trials; the capture shows exactly one request per trial | 3 |
| P3 | `stream:true` → NDJSON lines parse, the final `done: true` arrives, and the first line arrives before the last is generated (time to first byte < total time) | 6 |
| P4 | `/api/pull` of a small model while a gated request is held → progress stream completes; `/api/tags` lists the model | 5, 6 |
| P5 | `/api/tags`, `/api/ps` and `/api/version` through the sidecar equal the direct answers (bytes, after removing `Date`) | 6 |
| P6 | A client disconnects mid-generation → the next request is admitted (200) within 2 s, and the time until Ollama starts the next generation is measured and recorded (ADR-001 §8 risk 2) | 10 |

### 8.7 Quality gates for this slice (CI, pasted into the PR)

- `pytest --cov=seriatim_node --cov-branch --cov-fail-under=80`, with the output pasted.
- `mutmut run` on `node/seriatim_node/{config,gate,proxy,app}.py`; score ≥ 75% (SPEC REQ-61).
  The result is pasted, with the list of survivors and, for each, one line on why it is
  equivalent or which test was added.
- The PPE behaviour run (P1 to P6) on the PPE host, with the output pasted.
- The cross-vendor review (codex and agy through the model-validator) on the PR diff, with
  every real finding fixed and listed.

## Appendix A — Data shapes

The 429 body, as sent by `error_response(GPU_BUSY, …)`:

```json
{"error":"GPU busy: another request is running on this node","error_code":"GPU_BUSY",
 "retryable":true,"retry_after":15,"source":"sidecar","node":"node-a","busy_for_s":12.4}
```

A log line (one per request):

```json
{"ts":"2026-11-16T10:00:00.123Z","level":"INFO","event":"request","request_id":"9f…",
 "method":"POST","path":"/api/chat","gated":true,"outcome":"ok","status":200,
 "wait_ms":0.04,"hold_ms":5321.7,"bytes_in":812,"bytes_out":20411,"node":"node-a"}
```

The fields `client` and `model` are added in S2 and S3.

## Appendix B — What slice 1 needs from S0 (S0a and S0b)

- **S0a.**
  - The uv workspace: root `pyproject.toml` with `[tool.uv.workspace] members = ["node"]`
    (later also `gateway`), plus `ruff`, `pytest` and `mutmut` settings.
  - The CI workflow, with jobs: `lint`, `test` (matrix ubuntu/windows/macos), `coverage`
    (ubuntu, `--cov-fail-under=80`), `mutation` (ubuntu, changed files only, threshold 75),
    `contract-lint`, `gitleaks`, `private-terms`, `ip-literals` and `pr-size`.
  - The mutation job works out the changed files with
    `git diff --name-only origin/main...HEAD -- 'node/**.py' 'gateway/**.py'`. It writes them
    to `[tool.mutmut] paths_to_mutate`, then fails if the score is below 75.
- **S0b.** `ppe/compose.yaml` with:
  - `ollama-a` (`ollama/ollama:0.40.16`, env `OLLAMA_HOST=127.0.0.1:11435`,
    `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_CONTEXT_LENGTH=2048`), with a loopback-only debug port
    published through a tiny `socat` helper for direct comparison;
  - `cap-a` (tcpdump in `ollama-a`'s network namespace, filter `tcp dst port 11435`);
  - limits that sum to ≤ 4 CPUs and 6 GiB.

  `ppe/ppe up` checks the limit sum, that every port is published on `127.0.0.1`, and the
  hostname against the gitignored `ppe/.allowed-host`. It then pulls `qwen2.5:0.5b` and makes
  the `:14b` and `:32b` aliases with `ollama cp`.
- **Fixtures.** `tools/capture_fixtures.py` drives `ollama-a` directly. For each exchange it
  records:
  - `id` (for example `chat.stream`, `chat.nostream`, `generate.stream`, `embed`,
    `v1.chat.stream`, `v1.chat.nostream`, `tags`, `ps`, `version`, `show`, `pull.progress`,
    `chat.model_not_found`, `chat.bad_json`);
  - the request (method, path, body);
  - the response (status, headers minus `Date`, body as base64, and the chunk boundaries as
    observed);
  - `ollama_version` and the model digest.

  It removes anything machine-specific (no IPs, no host names) and writes
  `tests/fixtures/ollama-0.40.16.json`.
