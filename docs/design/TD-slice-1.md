# TD — Slice 1: contract, sidecar gate, 429 and passthrough

**Status:** revision 2, for review. **Date:** 2026-11-16.
**Implements:** SPEC REQ-1 to REQ-7, REQ-10 to REQ-12b, REQ-15 (pre-auth form), REQ-18
(S1 form), REQ-20 to REQ-26. **Architecture:** ADR-001 D1 to D4, D7, D8, D10 to D12.
**Not in this slice:**
- auth (S2);
- the `num_ctx` policy (S3);
- `/seriatim/status`, the capacity report and the exposure check (S4);
- deploy (S5, S6); automated deployment is deferred (SPEC §7.6);
- the gateway (S7 and later);
- TLS (S11).

**Exposure before S4.** Until S2 and S4 land, the sidecar has no authentication and no
exposure check. It therefore refuses a non-loopback listen address unless
`SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true` is set (SPEC REQ-15), and only the PPE sets that.
Slices S1 to S3 are not deployed to GPU nodes. The first node deploy needs S4, because REQ-19
must pass before anything is admitted.

## 1. Files (13)

S0a has already created `node/pyproject.toml` (with pinned dependencies),
`node/seriatim_node/__init__.py`, `node/seriatim_node/__main__.py` and `.env.example`. S0b
has already committed `tests/fixtures/route-pairs.json` and the fixtures. This slice does not touch them, and its
TD is merged before its code (ADR-001 §10).

| # | path | purpose |
|---|---|---|
| 1 | `contract/openapi.yaml` | ADR-001 Appendix A, committed and linted |
| 2 | `node/seriatim_node/config.py` | `Settings`, loading, validation and default lock directories |
| 3 | `node/seriatim_node/gate.py` | `check_raw_path()`, `classify()`, `Gate`, `Lease` |
| 4 | `node/seriatim_node/proxy.py` | `Proxy`: passthrough with streaming, disconnects, the hold limit, framing-safe errors and the shielded release |
| 5 | `node/seriatim_node/app.py` | the dispatcher, `ErrorCode` and the error table, `/seriatim/health`, `RequestLog`, the singleton lock, `main()` |
| 6 | `tests/node/conftest.py` | the real-socket harness: the sidecar and a fake Ollama on ephemeral ports, a fixture replayer and a raw-socket fake |
| 7 | `tests/node/test_config.py` | C1 to C8 |
| 8 | `tests/node/test_gate.py` | K1 to K3, G1 to G10 |
| 9 | `tests/node/test_http.py` | H1 to H14, B1 to B5, E1 to E6, HL1 to HL4, L1, K-OAS1 to K-OAS3 |
| 10 | `tests/node/test_streaming.py` | S1 to S10, F1 to F5 |
| 11 | `tests/node/test_process.py` | R1 to R5 (subprocess: startup quarantine, kill and restart, duplicate start) |
| 12 | `tests/behaviour/test_sidecar.py` | P1 to P7 (black-box; the required `ppe` CI job) |
| 13 | `ppe/compose.yaml` (modified) | adds the `node-a` sidecar container in `ollama-a`'s network namespace |

**Dependencies.** S0a pins these in `node/pyproject.toml` and `uv.lock`:
- **Runtime:** `starlette`, `uvicorn` (h11 only; no `[standard]` extra, so `httptools` is never
  picked), `httpx`, `anyio`, `pydantic-settings`. The singleton lock uses the standard
  library only (`fcntl`, `msvcrt`).
- **Test:** `pytest`, `pytest-cov`, `hypothesis`, `openapi-spec-validator`, `jsonschema`,
  `pyyaml`, and `mutmut>=3.3,<4`.

## 2. Modules and functions

### 2.1 `config.py`

```python
class Settings(BaseSettings):            # pydantic-settings; env prefix SERIATIM_NODE_, TOML source
    listen_host: str = "127.0.0.1"
    listen_port: int = 11434             # 1..65535
    ollama_url: AnyHttpUrl = "http://127.0.0.1:11435"
    id: str = "node"                     # ^[a-z0-9][a-z0-9-]{0,31}$ ; opaque label, never a hostname
    slots: int = 1                       # 1..64
    retry_after_s: int = 15              # 1..3600
    drain_s: float = 10                  # 1..600
    max_hold_s: float = 1800             # 1..86400
    connect_timeout_s: float = 5         # (0, 60]
    close_timeout_s: float = 5           # (0, 60]
    ungated_read_timeout_s: float = 300  # (0, 3600]
    max_body_bytes: int = 64 * 2**20     # >= 1024
    body_timeout_s: float = 30           # (0, 3600]
    health_probe_timeout_s: float = 2    # (0, 30]
    allow_unauthenticated: bool = False
    lock_dir: Path = default_lock_dir()  # overridable only in PPE/test mode (step 6)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    model_config = SettingsConfigDict(env_prefix="SERIATIM_NODE_", extra="forbid")

class ConfigError(Exception): ...        # the message names the offending key

def default_lock_dir() -> Path           # %ProgramData%\seriatim\run | /usr/local/var/run/seriatim | /run/seriatim
def load_settings(environ: Mapping[str, str] = os.environ) -> Settings
```

There is no `workers` setting (REQ-21), and no configurable allowlist. The allowlist is fixed
in `gate.py` and checked against the route probe (K3).

`load_settings` runs these steps in order:
1. **Config file.** If `SERIATIM_NODE_CONFIG` is set, read that file as TOML, with the field
   names in lower case and no prefix. A missing, unreadable or malformed file raises
   `ConfigError("SERIATIM_NODE_CONFIG: …")`. An unknown key in the file raises a
   `ConfigError` that names it.
2. **Unknown variables.** Reject every environment variable that starts with
   `SERIATIM_NODE_` and is not a field or `SERIATIM_NODE_CONFIG`. This is needed because
   pydantic's `extra="forbid"` does not cover the environment. Other `SERIATIM_*` variables,
   such as developer tooling settings, are ignored (REQ-23).
3. **Build `Settings`**, with environment values taking precedence over the file. `Settings`
   overrides `settings_customise_sources` to return **only** the init source. That way
   pydantic never reads `os.environ` or a `.env` file behind `load_settings`'s back.
   `load_settings` builds the input dictionary itself: the file's values first, then the
   values from `environ`, with the prefix stripped. Every pydantic `ValidationError` becomes
   a `ConfigError` that names `SERIATIM_NODE_<FIELD>`, mapped from the error's `loc`.
4. **Upstream URL.** `ollama_url` must meet all of these:
   - the scheme is `http`;
   - the port is explicit;
   - the path is empty or `/`;
   - there is no query, fragment or user info;
   - the host is an IP literal in `127.0.0.0/8`, or `::1`.

   `localhost` is a name, not a literal, so it is refused. No name resolution ever happens.
5. **Listen address.** If `listen_host` is not a loopback literal and `allow_unauthenticated`
   is false, raise `ConfigError`. When it is true, log a `WARNING` at startup.
6. **Lock directory.** A `lock_dir` other than `default_lock_dir()` is accepted only when
   `allow_unauthenticated` is true, which is the PPE and test mode. Otherwise it raises
   `ConfigError`. Two production sidecars therefore always meet at the same lock file, and
   cannot be configured apart (REQ-12b).

### 2.2 `gate.py`

```python
class PathError(ValueError): ...

def check_raw_path(raw_path: bytes) -> None          # raises PathError (REQ-20a)

class Kind(StrEnum): SERIATIM = "seriatim"; UNGATED = "ungated"; GATED = "gated"
class Scope(StrEnum): NONE = "none"; INFER = "infer"; ADMIN = "admin"

@dataclass(frozen=True)
class Route:
    kind: Kind
    scope: Scope                 # used from S2; S1 records it in the log only
    known: bool                  # False for unlisted pairs (gated + admin)
    seriatim_status: int | None  # 200 known, 404 unknown path, 405 wrong method (SERIATIM only)

def classify(method: str, raw_path: bytes) -> Route

# Every table holds (method: str, raw_path: bytes); paths are compared as bytes, never decoded.
SERIATIM_ROUTES: Mapping[bytes, frozenset[str]]      # {b"/seriatim/health": {"GET"}} in S1
UNGATED_INFER: frozenset[tuple[str, bytes]]
UNGATED_ADMIN: frozenset[tuple[str, bytes]]          # plus HEAD/POST on BLOB_RE
GATED_INFER: frozenset[tuple[str, bytes]]
GATED_ADMIN: frozenset[tuple[str, bytes]]            # {("POST", b"/api/create")}
BLOB_RE = re.compile(rb"^/api/blobs/sha256:[0-9a-f]{64}$")
```

**`check_raw_path`** works on the raw bytes, before any decoding, and is case-insensitive
for escapes. It raises if any of these hold:
- the path does not start with `/`;
- it contains `//`, `\`, a NUL or any byte below `0x20` or equal to `0x7f`;
- a segment is `.` or `..`;
- it contains `%2e`, `%2f`, `%5c` or `%00`, in any case.

Other percent-escapes are allowed and passed through untouched.

**`classify`** decodes nothing. It compares the exact raw bytes, after `check_raw_path` has
passed, with these rules applied in order:
1. **Seriatim's own paths.** A path equal to `/seriatim` or starting with `/seriatim/` is
   `SERIATIM`. A listed path with a listed method gives status 200. A listed path with any
   other method gives 405. Any other path gives 404.
2. **Ungated, `infer` scope:** `GET /`, `HEAD /`, `GET /api/tags`, `GET /api/ps`,
   `GET /api/version`, `POST /api/show`, `GET /v1/models`.
3. **Ungated, `admin` scope:** `POST /api/pull`, `POST /api/push`, `POST /api/copy`,
   `DELETE /api/delete`, and `HEAD` or `POST` on a path matching `BLOB_RE`.
4. **Gated, `infer` scope:** `POST` on `/api/generate`, `/api/chat`, `/api/embed`,
   `/api/embeddings`, `/v1/chat/completions`, `/v1/completions` and `/v1/embeddings`.
   **Gated, `admin` scope, known:** `POST /api/create`.
5. **Anything else** is gated, with `admin` scope and `known=False`. That includes
   `OPTIONS` anywhere, `GET /api/chat`, `POST /api/chat/` and `POST /API/CHAT`.

Methods are compared in upper case as h11 delivers them. Because h11 keeps the method's case,
`post` is not `POST`, so it falls to rule 5 and is gated.

```python
class SlotState(StrEnum): FREE = "free"; HELD = "held"; DRAINING = "draining"; STUCK = "stuck"

@dataclass(frozen=True)
class HolderInfo:
    path: str; request_id: str; started_mono: float; started_wall: float

@dataclass(frozen=True)
class SlotView:
    state: SlotState; holder: HolderInfo | None; drain_remaining_s: float | None

@dataclass(frozen=True)
class Busy:     retry_after_s: int; busy_for_s: float
@dataclass(frozen=True)
class Starting: retry_after_s: int

class Lease:
    info: HolderInfo
    deadline: float                         # monotonic: acquired + max_hold_s
    def finish(self, *, clean: bool) -> bool    # idempotent; True only on the first call
    def stuck(self) -> bool                     # idempotent; marks the slot STUCK, never freed

class Gate:
    def __init__(self, slots: int, retry_after_s: int, drain_s: float, max_hold_s: float,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time)
    def try_acquire(self, path: str, request_id: str) -> Lease | Busy | Starting   # never awaits
    def starting(self) -> bool
    def slots(self) -> list[SlotView]
    def overdue(self, grace_s: float = 30) -> list[HolderInfo]
    def any_stuck(self) -> bool
```

**Slot states.**
- `FREE`: available.
- `HELD`: has a `Lease`.
- `DRAINING`: has a `drain_until` time.
- `STUCK`: closing its upstream connection timed out (§3.3). It is never freed until the
  process restarts.

**Expiry is lazy.** `try_acquire`, `slots()` and `starting()` first turn every `DRAINING`
slot whose `drain_until ≤ clock()` back into `FREE`. That is a synchronous computation inside
the caller, not a background task (invariant 4).

**Startup quarantine (REQ-12a).** `__init__` puts every slot in `DRAINING`, with
`drain_until = clock() + drain_s`, and sets `starting_until` to the same time. While
`clock() < starting_until`, `try_acquire` returns
`Starting(retry_after_s=ceil(starting_until - clock()))`.

**`try_acquire`** returns one of:
- `Starting` while starting;
- a `Lease` on the first `FREE` slot, which becomes `HELD`;
- otherwise `Busy`, with `retry_after_s = max(retry_after_s, ceil(earliest drain_until -
  clock()))` and `busy_for_s` = the age of the oldest holder (0 if every slot is draining).

**`Lease.finish(clean=True)`** sets the slot to `FREE`. **`finish(clean=False)`** sets it to
`DRAINING`, with `drain_until = clock() + drain_s`.

### 2.3 `proxy.py`

```python
class Proxy:                                  # an ASGI app for non-SERIATIM routes
    def __init__(self, settings: Settings, gate: Gate, client: httpx.AsyncClient,
                 log: RequestLog, errors: ErrorFactory)
    async def __call__(self, scope, receive, send, route: Route, request_id: str) -> None

# module-level helpers, pure and unit-tested
def upstream_headers(client_headers: list[tuple[bytes, bytes]], *, body_len: int | None) -> list[tuple[bytes, bytes]]
def prune_httpx_defaults(req: httpx.Request, client_headers: list[tuple[bytes, bytes]]) -> None
def downstream_headers(resp_headers: httpx.Headers, *, gate_value: str, request_id: str) -> list[tuple[bytes, bytes]]
def record_framing(resp_headers: httpx.Headers, tail: bytes) -> Literal["ndjson", "sse"] | None
def stream_error_record(framing: Literal["ndjson", "sse"], code: str, message: str) -> bytes
async def read_body(receive, *, limit: int, timeout_s: float) -> bytes     # raises BodyTooLarge / BodyTimeout / ClientGone
def body_stream(receive) -> AsyncIterator[bytes]                           # ungated: forwards without buffering
async def watch_disconnect(receive, outcome: Outcome, scope: anyio.CancelScope) -> None
```

### 2.4 `app.py`

```python
class ErrorCode(StrEnum): ...            # exactly the contract's ErrorCode enum (K-OAS3)
ERROR_TABLE: Mapping[ErrorCode, ErrorSpec]   # status, retryable, replay_safe; equals x-seriatim-codes
class ErrorFactory:
    def response(self, code: ErrorCode, message: str, *, retry_after: int | None = None,
                 gate_value: str, request_id: str, **extra) -> tuple[int, list[tuple[bytes, bytes]], bytes]
class RequestLog:
    def emit(self, **fields) -> None     # one JSON line per request to stdout (REQ-22)

def create_app(settings: Settings, *, client: httpx.AsyncClient | None = None,
               gate: Gate | None = None) -> ASGIApp
def acquire_singleton(settings: Settings) -> None   # REQ-12b; raises SingletonError
_SINGLETON_FD: int | None                            # module-level; holds the open, locked file
def main() -> None
```

**The dispatcher.** `create_app` returns a plain ASGI callable that takes every request
before Starlette sees it. That is what lets it check the raw path first. For each request:
1. `lifespan` messages go to the Starlette app. Its lifespan creates the `Gate` (which starts
   in quarantine) and the `httpx.AsyncClient`, and closes the client on shutdown.
2. It sanitises `X-Request-Id`: the value is kept if it matches `^[A-Za-z0-9._-]{1,128}$`,
   and otherwise replaced with a fresh `uuid4().hex`.
3. It reads `scope["raw_path"]`. If the key is missing, that is a server fault, so it returns
   500 `INTERNAL` (fail closed). Uvicorn with h11 always sets it.
4. `check_raw_path(raw_path)`. A `PathError` gives 400 `BAD_PATH`.
5. `route = classify(method, raw_path)`.
6. For `SERIATIM` routes, a status of 404 or 405 gives `NOT_FOUND` or `METHOD_NOT_ALLOWED`
   (405 also carries `Allow`). Otherwise the request goes to the Starlette router, which
   holds only `/seriatim/health` in S1.
7. Everything else goes to `proxy(scope, receive, send, route, request_id)`.

**HTTP clients.** There are three kinds, so that ungated traffic can never starve inference
or health, and so that closing a gated request's upstream connection can be proven:
- **One client per gated request.** It is created after admission and used for exactly one
  exchange: `Limits(max_connections=1, max_keepalive_connections=0)`. Its cleanup is
  `await req_client.aclose()`, which closes the connection pool and with it the only
  connection. That one awaited call is the proof of closure that invariant 3 needs. httpx's
  implicit closes (in `send()` on failure, or when `aiter_raw()` is exhausted) may already
  have closed the stream. They never stand in for the pool close: `aclose()` is still called,
  and only its return counts. A loopback connect costs well under a millisecond.
- **One shared client for ungated routes**, with `Limits(max_connections=16,
  max_keepalive_connections=8)` and `Timeout(connect=connect_timeout_s,
  read=ungated_read_timeout_s, write=ungated_read_timeout_s, pool=connect_timeout_s)`. If the
  pool is exhausted, the request gets `PoolTimeout`, then 503 `NODE_SATURATED` with
  `Retry-After`. That affects ungated traffic only, and the gateway does not mark the node
  down for it.
- **One health client**, with one connection and `timeout=health_probe_timeout_s`.

Every client is built with these settings:
- `base_url=ollama_url`, `http1=True`, `http2=False`;
- `trust_env=False`, `follow_redirects=False`, `headers={}`;
- `cookies=NoCookies()`: an `http.cookiejar.CookieJar` whose policy refuses every cookie
  (`DefaultCookiePolicy(allowed_domains=[])`).

Two of those settings deserve a note:
- **`trust_env=False`.** An `HTTP(S)_PROXY` in the service's environment must not send
  loopback traffic through a proxy.
- **`NoCookies`.** A `Set-Cookie` from Ollama could otherwise be stored by httpx and sent on
  another client's request. `prune_httpx_defaults` also removes any `cookie` header from the
  built request.

Gated requests have `read=None` and `write=None`. Their only bound is the hold limit.

**`main()`** runs these steps:
1. `load_settings()`. On `ConfigError`, print the message to stderr and exit 2.
2. `acquire_singleton(settings)`. On `SingletonError`, print the message and exit 3.
3. `uvicorn.run(create_app(settings), host=…, port=…, http="h11", workers=1,
   lifespan="on", log_config=None, server_header=False, date_header=True)`.

**Holding the lock.** The locked file descriptor is kept in the module-level
`_SINGLETON_FD`, so garbage collection can never release it. It is never unlocked, closed or
deleted by the code: the OS releases the lock when the process exits for any reason,
including a crash. Never deleting the file matters. If the file were deleted while another
process had it open, a third process could lock a new file at the same path, and two sidecars
would then each hold a lock.

**`acquire_singleton`** works as follows:
- It creates `lock_dir` with `mkdir(parents=True, exist_ok=True)`.
- It opens `lock_dir / f"upstream-{port}.lock"` with `os.open(path, O_RDWR | O_CREAT, 0o644)`.
- It takes a non-blocking exclusive lock: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on POSIX, or
  `msvcrt.locking(fd, LK_NBLCK, 1)` at offset 0 on Windows.
- `BlockingIOError` (POSIX) or `PermissionError` with `EACCES` (Windows) gives
  `SingletonError("another sidecar holds <path>")`.
- An `OSError` on the directory or the file gives `SingletonError("cannot lock <path>: …")`.
- Being unable to take the lock never lets the sidecar start.

## 3. Lock and concurrency design

### 3.1 Invariants

1. **The limit.** `0 ≤ (held + draining + stuck) ≤ slots`. Only `FREE` slots are admitted, so
   at every point between two `await`s, at most `slots` requests are past admission.
2. **One end per lease.** Each `Lease` returned by `try_acquire` gets exactly one effective
   `finish` or `stuck`.
3. **Close first.** A lease whose request reached Ollama (`outcome.sent_upstream`) is
   finished only **after** `req_client.aclose()` has returned normally. If that call raises,
   or does not return within `close_timeout_s`, closure is unproven, so the lease is marked
   `stuck` and the slot is never freed (REQ-11). A restart then closes the socket for
   certain.
4. **No reaper.** Only the request's own `Proxy.__call__` frame ends its lease. The lazy drain
   expiry in `Gate` never touches a `HELD` slot.

### 3.2 Why admission is atomic

The event loop runs one coroutine at a time, and switches only at an `await`. `try_acquire`
is a plain function: it expires drains, finds a `FREE` slot and marks it `HELD`, with no
`await` in between. Two requests therefore cannot both take the last slot. Uvicorn runs one
event loop on one thread (REQ-21).

Three tests check this:
- G7: 1000 concurrent callers, for which exactly one gets the slot;
- G9: property-based sequences of operations;
- C8: that `main()` pins `workers=1`.

### 3.3 Request lifecycle in `Proxy.__call__`

The cancel-scope structure is the core of the slice. The three cases (clean end, client
disconnect and hold deadline) are told apart by explicit flags, never by guessing from the
exception type.

```text
 1  outcome ← Outcome(sent_upstream=False, started=False, upstream_done=False,
                      disconnected=False, error=None, tail=b"")
 2  lease ← None; req_client ← None; slot_end ← "none"
 3  if Content-Length present and > max_body_bytes and route is buffered:
 4      → 413 BODY_TOO_LARGE; return                                   # no slot taken
 5  if route.kind == GATED:
 6      r ← gate.try_acquire(path, request_id)
 7      Busy     → 429 GPU_BUSY, Retry-After, X-Seriatim-Gate: busy; return   # body never read
 8      Starting → 503 NODE_STARTING, Retry-After;               return
 9      lease ← r; req_client ← new_gated_client()
10  try:
11      with anyio.move_on_after(deadline_remaining(lease)) as hold:      # inf when ungated
12          async with anyio.create_task_group() as tg:
13              await self._exchange(..., outcome, tg)       # never raises Exception; stops before
14              tg.cancel_scope.cancel()                     #   the final empty body message
15      outcome.hold_exceeded ← hold.cancelled_caught and not outcome.disconnected
16  except Exception as exc:                                 # includes ExceptionGroup
17      outcome.error ← outcome.error or Internal(exc)       # defence in depth
18  finally:
19      with anyio.CancelScope(shield=True):
20          # (a) prove upstream closure; bounded
21          closed ← True
22          if req_client is not None:
23              closed ← False
24              with anyio.move_on_after(close_timeout_s):
25                  try: await req_client.aclose(); closed ← True
26                  except Exception: log CRITICAL upstream_close_failed
27          elif outcome.resp is not None:                    # ungated: shared client
28              with anyio.move_on_after(close_timeout_s):
29                  try: await outcome.resp.aclose()
30                  except Exception: log ERROR upstream_close_failed   # no slot at stake
31          # (b) end the lease; synchronous, cannot block or be skipped
32          if lease:
33              if not closed:                 lease.stuck();  slot_end ← "stuck"
34              elif outcome.upstream_done or not outcome.sent_upstream:
35                                             lease.finish(clean=True);  slot_end ← "free"
36              else:                          lease.finish(clean=False); slot_end ← "draining"
37          # (c) only now talk to the client; bounded; failure never matters to the slot
38          with anyio.move_on_after(close_timeout_s):
39              try: await self._finish_response(send, outcome)   # final message, error reply,
40              except Exception: log DEBUG client_write_failed   #   record or abort (§5)
41          log.emit(..., slot_end=slot_end)
```

**The order in the `finally` is the design.**
- **(a) Closure.** Upstream closure is proven first, within a time bound.
- **(b) The lease** ends next, in plain synchronous code, so nothing can block it or skip it.
- **(c) The client.** Only then does the proxy talk to the client. That covers the final empty
  body message of a clean response, an error reply, or an in-stream record and abort. A slow
  or vanished client can delay nothing that matters to the slot.

Step (c) also fixes keep-alive ordering. Uvicorn moves a connection on to its next request
as soon as the final body message is sent. By then the lease has already ended in (b), so a
client's next request on the same connection is never refused because of its own previous
one (H8).

**`_exchange`** wraps everything that can fail. It catches every `Exception` and records it
in `outcome.error`, so the task group sees only cancellations, never an `ExceptionGroup`.
Cancellation is a `BaseException` in anyio, so `except Exception` never swallows it. Its
steps:

1. **Read the body.**
   - Gated route: `body = await read_body(receive, limit, body_timeout_s)`.
   - Every other route is buffered the same way, except `POST /api/blobs/sha256:<hex>`.
   - **Blob uploads** can be gigabytes, so they are streamed with `content =
     body_stream(receive)`. Streaming skips the size limit, but the stream still has an idle
     timeout of `body_timeout_s` between chunks.
   - **The first chunk.** `body_stream` reads the first chunk from the client **before**
     Ollama is contacted. A stall or a disconnect before it arrives gives 408
     `REQUEST_BODY_TIMEOUT` (or no response), which is replay-safe, because nothing was sent.
   - **After `client.send()` starts,** request headers may already have reached Ollama. Any
     stall or disconnect from then on gives 408 `UPLOAD_INTERRUPTED`, which is **not**
     replay-safe.
2. **Start the watcher** with `tg.start_soon(watch_disconnect, receive, outcome,
   tg.cancel_scope)`.
   - Buffered routes start it right after the body is read.
   - A blob upload starts it after the body stream ends. Until then `receive` belongs to
     `body_stream`, which sees the disconnect itself. A blob upload holds no slot.
   - The watcher starts only after the request body has been fully consumed (`more_body`
     false). From then on it is the only caller of `receive()`, so two tasks never read the
     ASGI channel at once.
   - It loops on `receive()`. On `http.disconnect` it sets `outcome.disconnected = True` and
     cancels the task group's scope. Any other message type is ignored.
3. **Build the request.**
   - `req = client.build_request(method, url, headers=upstream_headers(...),
     content=body or content)`.
   - The URL is absolute, so `build_request` never merges it with `base_url` or normalises
     it. H4 checks, with the pinned httpx, that the raw path and query reach Ollama
     byte-identical: `%20` stays `%20`.
   - `url = client.base_url.copy_with(raw_path=raw_path + (b"?" + query_string if
     query_string else b""))`, so the path and query are the raw bytes.
   - `prune_httpx_defaults(req, client_headers)` then removes `accept` and `user-agent` unless
     the client sent them, because httpx adds them in `build_request`. It always removes
     `accept-encoding` (§4) and `cookie`.
4. **Send it.** `outcome.sent_upstream = True` is set immediately **before**
   `resp = await client.send(req, stream=True)`.
   - If `send` raises `ConnectError`, `ConnectTimeout` or `PoolTimeout`, the handler sets
     `sent_upstream` back to `False`. Those errors mean no byte reached Ollama, so the slot is
     freed and the error is replay-safe.
   - Every other failure leaves `sent_upstream = True`.
5. **Relay the response.**
   - `outcome.resp = resp`.
   - `await send(http.response.start)` with `downstream_headers(...)`, then
     `outcome.started = True`.
   - For each chunk of `resp.aiter_raw()`: `await send(http.response.body, chunk,
     more_body=True)`, and `outcome.tail` keeps the last two bytes sent.
   - When the iterator ends normally, it sets `outcome.upstream_done = True` and returns,
     but only if the upstream response was **framed**: either it had a `content-length` that
     the received bytes matched, or it was chunked and ended with the terminating chunk.
   - A response with neither is close-delimited, and ends when Ollama closes the connection.
     A crash would look the same as a normal end. So such a response is treated as broken at
     its end: an in-stream record if framing allows, an abort, and a drain. Ollama, a Go
     `net/http` server, frames every HTTP/1.1 response, so this path exists only as a
     defence.
     The final empty body message is **not** sent here; the `finally` sends it in step (c),
     after the lease has ended.

**How the three end conditions are told apart:**

| what happened | flag | effect of the cancellation |
|---|---|---|
| The client disconnected | `outcome.disconnected` (set by the watcher **before** it cancels) | The task group's own scope absorbs it, so `hold.cancelled_caught` is False |
| The hold limit passed | `hold.cancelled_caught` and not `disconnected` | The outer `move_on_after` absorbs it |
| An external cancellation (server shutdown) | neither flag | It propagates out of `__call__` after the shielded `finally`, which closes upstream and drains the slot |

**Why the `finally` is shielded.** Without the shield, an external cancellation or a
disconnect would cancel `req_client.aclose()` itself, leaving the socket open while the slot
is freed. That is exactly what invariant 3 forbids. Each awaited step inside the shield has
its own `move_on_after`, so the whole `finally` takes at most `2 × close_timeout_s`. A shield
with no time bound could hang shutdown for ever.

**Other notes:**
- **Line 7 never reads the body.** A busy client that has sent only the request headers gets
  its 429 at once (B3). Uvicorn discards any unread body, and closes the connection if the
  body was too large to discard.
- **Read the body under the deadline.** Line 12 encloses the body read, so a slow uploader
  can't hold a slot past `max_hold_s`. `body_timeout_s` bounds it too.
- **Waiting for headers.** For `stream:false` the wait lasts the whole generation. The
  watcher covers it, which fixes the predecessor's failure mode, where an abandoned
  non-streaming request kept the GPU busy until Ollama finished.
- **`stream=True` on every request.** The proxy never buffers a response.
- **Keep-alive.** The lease ends before the final body message is sent (step (c)), so the next
  request on the same connection can't be refused because of its own previous one (H8). An
  abnormal end still drains (H8b).
- **The drain is a time window, not a proof.** A drained slot is free again after `drain_s`,
  whether or not Ollama has actually stopped. If Ollama is slower than that, its own
  `OLLAMA_NUM_PARALLEL=1` queue makes the next request wait rather than run alongside
  (SPEC REQ-10, ADR-001 §8 risk 2).

### 3.4 Health

`GET /seriatim/health` (no auth; `X-Seriatim-Gate: ungated`) returns 200 `{"status":"ok"}`
when all of these hold:
- **Ollama answers.** `GET /api/version`, through the health client, returns 200, a body that parses as a JSON **object**, and
  `version` is a non-empty string.
- **No slot is overdue.** `gate.overdue()` is empty.
- **No slot is stuck.** `gate.any_stuck()` is false.
- **The node is not starting.** `gate.starting()` is false.

Otherwise it returns 503 `{"status":"degraded","reasons":[…]}`. The reasons, in this order:
- `ollama_unreachable`: connect error or timeout;
- `ollama_bad_response`: a status other than 200, a body that is not JSON or not an object,
  or a missing or empty `version`;
- `slot_overdue`;
- `slot_stuck`;
- `starting`.

The body never includes the Ollama version or any address. Probes are logged at `DEBUG` only.

## 4. Streaming passthrough and headers

**Bytes.**
- The proxy uses `resp.aiter_raw()`, so nothing is decoded.
- Chunks are forwarded as they arrive, never collected.
- A gated request body is the exact bytes received. An ungated body is streamed as received.

**Request headers** (`upstream_headers`). Removed:
- the hop-by-hop headers: `connection`, every header named in `Connection`, `keep-alive`,
  `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `transfer-encoding` and
  `upgrade`;
- `host`, `authorization`, `cookie`, `x-api-key`, every `x-seriatim-*`, `content-length` and
  `accept-encoding`;
- the forwarding headers `forwarded`, `x-forwarded-*` and `x-real-ip`.

Then:
- `content-length` is set from `body_len` when the body was buffered. For an ungated stream,
  the client's validated `content-length` is kept if it sent one; otherwise httpx sends the
  body chunked.
- `x-request-id` is set to the sanitised id.

**Why `accept-encoding` is removed.** Ollama then never compresses. The sidecar does not
decode, so this keeps in-stream error records safe to frame. It costs some bandwidth on
loopback only.

**Response headers** (`downstream_headers`). Removed:
- the hop-by-hop headers;
- every `access-control-*` header (Seriatim serves no CORS; T11);
- every `x-seriatim-*` header (T9);
- the compatibility headers `x-resolved-model`, `x-model-digest`, `x-execution-device`,
  `x-rerouted-from`, `x-reroute-reason` and `x-context-clamped`;
- `set-cookie` and `server`.

Kept unchanged: `content-type`, `content-length` and `content-encoding`. Then added:
`x-seriatim-gate` (`acquired` if a slot was taken, otherwise `ungated`), `x-seriatim-node`
and `x-request-id`. Without a `content-length`, uvicorn sends chunked encoding.

**Status.** Ollama's status and body pass through unchanged, including Ollama's own 4xx, 5xx
and 429 responses. Those carry no `X-Seriatim-Gate: busy`, so the gateway never spills over
on them (H5, H9).

**Errors after `started`** (REQ-24). `record_framing(headers, outcome.tail)` returns
`"ndjson"` or `"sse"` only when all of these hold:
- the response has no `content-length`;
- `content-encoding` is absent or `identity`;
- the media type (before `;`) is `application/x-ndjson` or `text/event-stream`;
- the tail is at a record boundary: nothing sent yet, or ending in `\n` for NDJSON or `\n\n`
  for SSE.

Otherwise it returns `None`.

When there is a framing, `stream_error_record` gives one of:
- NDJSON: `json.dumps({"error": msg, "error_code": code}, separators=(",", ":")) + "\n"`;
- SSE: `"data: " + <the same JSON> + "\n\n"`.

The proxy sends the record with `more_body=True`, if there is one. It then **returns without
sending the final `more_body=False`**. Uvicorn then closes the transport without the chunked
terminator, and logs "ASGI callable returned without completing response". The client sees an
incomplete response either way. Tests S6, S7 and S10 check this through the client's
`httpx.RemoteProtocolError`.

**Backpressure.** `await send(...)` blocks while the client reads slowly, and the upstream
read pauses with it (TCP flow control). A slow reader therefore holds the slot until it
finishes or `max_hold_s` passes (T5).

**HTTP version.** Uvicorn runs h11, so HTTP/2 is never negotiated, and the client side is
`http2=False`. h11 also accepts **HTTP/1.0**, whose responses end when the connection
closes. A client could not tell an aborted 1.0 response from a complete one. The dispatcher
therefore rejects every request whose `scope["http_version"]` is not `"1.1"`, with 505
`HTTP_VERSION_UNSUPPORTED`, before it classifies the request.

## 5. Error mapping

"Before" means before `http.response.start` was sent; "after" means after it. The "slot"
column shows the lease result for a gated request.

| condition | before | after | slot | log `outcome` |
|---|---|---|---|---|
| HTTP version other than 1.1 | 505 `HTTP_VERSION_UNSUPPORTED` (`Connection: close`) | — | none taken | `rejected` |
| Unsafe raw path | 400 `BAD_PATH` | — | none taken | `rejected` |
| Unknown `/seriatim/*` path, or the wrong method on one | 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED` | — | none taken | `rejected` |
| Starting | 503 `NODE_STARTING` + `Retry-After` | — | none taken | `starting` |
| No free slot | 429 `GPU_BUSY` + `Retry-After` + `X-Seriatim-Gate: busy` | — | none taken | `busy` |
| `Content-Length` > limit (buffered routes) | 413 `BODY_TOO_LARGE` | — | none taken | `rejected` |
| Body grows past the limit while being read | 413 `BODY_TOO_LARGE` | — | freed | `rejected` |
| Body stalls (`BodyTimeout`) | 408 `REQUEST_BODY_TIMEOUT` | — | freed | `rejected` |
| Client gone while sending the body | nothing sent | — | freed | `client_disconnect` |
| Blob upload stalls or the client leaves after bytes were forwarded | 408 `UPLOAD_INTERRUPTED` (not replay-safe) | — | no slot (ungated) | `upload_interrupted` |
| `ConnectError`, `ConnectTimeout` | 502 `UPSTREAM_UNAVAILABLE` (replay-safe) | — | freed | `upstream_unavailable` |
| `PoolTimeout` (ungated pool full; gated requests have their own connection) | 503 `NODE_SATURATED` + `Retry-After` (replay-safe) | — | no slot | `saturated` |
| `WriteError`, `ReadError`, `RemoteProtocolError`, other `httpx.TransportError` | 502 `UPSTREAM_PROTOCOL_ERROR` | record `UPSTREAM_STREAM_BROKEN` if framing allows, then abort | drains | `upstream_error` |
| Hold limit passed | 504 `LOCK_HOLD_EXCEEDED` | record `LOCK_HOLD_EXCEEDED` if framing allows, then abort | drains | `hold_exceeded` (`CRITICAL`) |
| Client disconnect | nothing sent | nothing sent | drains | `client_disconnect` |
| External cancellation (shutdown) | nothing sent | abort | drains | `cancelled` |
| Ollama non-2xx (including 429) | passed through unchanged | — | freed (clean end) | `ok` (with `status`) |
| Closing the upstream connection timed out or raised | as above | as above | **stuck** | adds `slot_stuck` (`CRITICAL`) |
| Any other `Exception` | 500 `INTERNAL` (generic message; the traceback is in the log only) | abort | drains if sent upstream, else freed | `internal_error` (`ERROR`) |

If the client has already gone, writing an error reply fails. That failure is caught and
logged at `DEBUG`, and it never changes the slot result.

**Building error responses.** Every 4xx and 5xx produced here comes from
`ErrorFactory.response`, which fills in these fields:
- `source: "sidecar"` and `node: settings.id`;
- `retryable` and `replay_safe` from `ERROR_TABLE`, never from the call site;
- `retry_after`, with the matching `Retry-After` header, whenever it is given;
- `X-Seriatim-Gate`: `busy` only for `GPU_BUSY`, `acquired` when a slot was taken, otherwise
  `ungated`.

## 6. Configuration

Every key is `SERIATIM_NODE_<FIELD>`. The same names, in lower case and without the prefix,
can be set in the TOML file named by `SERIATIM_NODE_CONFIG`.

| variable | default | rule |
|---|---|---|
| `SERIATIM_NODE_LISTEN_HOST` | `127.0.0.1` | if not loopback, needs `ALLOW_UNAUTHENTICATED` (S1–S3) |
| `SERIATIM_NODE_LISTEN_PORT` | `11434` | 1–65535 |
| `SERIATIM_NODE_OLLAMA_URL` | `http://127.0.0.1:11435` | `http`, loopback literal, no path, query or user info |
| `SERIATIM_NODE_ID` | `node` | `^[a-z0-9][a-z0-9-]{0,31}$` |
| `SERIATIM_NODE_SLOTS` | `1` | 1–64 |
| `SERIATIM_NODE_RETRY_AFTER_S` | `15` | 1–3600 |
| `SERIATIM_NODE_DRAIN_S` | `10` | 1–600 |
| `SERIATIM_NODE_MAX_HOLD_S` | `1800` | 1–86400 |
| `SERIATIM_NODE_CONNECT_TIMEOUT_S` | `5` | > 0, ≤ 60 |
| `SERIATIM_NODE_CLOSE_TIMEOUT_S` | `5` | > 0, ≤ 60 |
| `SERIATIM_NODE_UNGATED_READ_TIMEOUT_S` | `300` | > 0, ≤ 3600; the idle limit for ungated upstream reads and writes |
| `SERIATIM_NODE_MAX_BODY_BYTES` | `67108864` | ≥ 1024 |
| `SERIATIM_NODE_BODY_TIMEOUT_S` | `30` | > 0, ≤ 3600 |
| `SERIATIM_NODE_HEALTH_PROBE_TIMEOUT_S` | `2` | > 0, ≤ 30 |
| `SERIATIM_NODE_ALLOW_UNAUTHENTICATED` | `false` | logs a `WARNING` at startup (hourly from S2) |
| `SERIATIM_NODE_LOCK_DIR` | per OS (§2.1) | another value needs `ALLOW_UNAUTHENTICATED`; created if missing; failure → exit 3 |
| `SERIATIM_NODE_LOG_LEVEL` | `INFO` | enum |
| `SERIATIM_NODE_CONFIG` | unset | path to TOML; must exist if set |

In the PPE, the compose file sets these, inside the container's network namespace:
- `LISTEN_HOST=0.0.0.0` and `ALLOW_UNAUTHENTICATED=true`;
- `LOCK_DIR=/run/seriatim`, on a tmpfs.

The published port is bound to `127.0.0.1` on the host.

## 7. Test harness (`tests/node/conftest.py`)

- **`fake_ollama` fixture.** A real uvicorn server on `127.0.0.1:0` running a small ASGI app
  that each test scripts per route.
  - Each request is recorded as raw method, raw path, query, headers and body bytes. A
    `sink` mode hashes the body as it arrives (SHA-256 and length only) instead of storing
    it, for H10.
  - Scripted behaviours:
    - `replay(fixture_id)`: from the captured file, with the recorded chunk boundaries;
    - `hold(event)`: send the headers and/or the first chunk, then wait on an `asyncio.Event`;
    - `stall_headers()`;
    - `status(code, body, headers)`;
    - `observe_disconnect()`: sets an event, with a monotonic timestamp, when the sidecar
      closes the connection.
- **`raw_upstream` fixture.** An `asyncio.start_server` socket fake, for faults that an ASGI
  app can't produce:
  - close at once after accepting;
  - stop reading the request body, so the sidecar's write blocks;
  - reset after N bytes of response;
  - send a malformed status line.
- **`sidecar` fixture.** A real uvicorn server (h11) for `create_app(settings)` on
  `127.0.0.1:0`, with `ollama_url` pointing at the fake.
  - A settings factory overrides slots, timeouts and so on.
  - `drain_s` is at least 1 here too. Tests that need a free gate at once inject a `Gate` with
    a fake clock, and advance it.
- **`sidecar_process` fixture** (for `test_process.py`). It runs `python -m seriatim_node` as
  a subprocess, with its environment and a temporary `LOCK_DIR`. It waits for the port, and
  kills the process on teardown. It works on Windows, macOS and Linux: it uses
  `Popen.kill()`, not POSIX signals.
- **Clients.** Tests use `httpx.AsyncClient`. B3, E4 and H8 use raw `asyncio` sockets, where
  the headers and the body must be sent at different times, or on one connection.
- **Timing.**
  - Assertions use `time.perf_counter()`.
  - The bounds are 100 ms locally and 500 ms when `CI=true`. The PPE bound is p99 < 250 ms.
  - Tests never sleep to synchronise. They wait on events from the fakes, with a timeout of at
    most 5 s, so a hang fails the test instead of passing it.
- **Error bodies.** A fixture hook collects every error body produced in `test_http.py` and
  `test_streaming.py`, for K-OAS2.

## 8. Tests to write (exact)

Each test names the requirement it proves. All run in CI on `ubuntu-latest`. The non-PPE tests
also run on `windows-latest` and `macos-latest`.

### 8.1 `test_config.py`

| id | test | REQ |
|---|---|---|
| C1 | `load_settings({})` gives the defaults in §6 exactly, including the per-OS lock directory | 23 |
| C2 | A TOML file sets `slots=2` and the environment sets `SERIATIM_NODE_SLOTS=3` → 3; keys set only in the file are applied; an unknown key in the file → `ConfigError` naming it | 23 |
| C3 | Out of range or wrong type → `ConfigError` whose message contains the variable name. Cases: `SLOTS` = `abc`, `0`, `-1`, `65`; `RETRY_AFTER_S=0`; `DRAIN_S` = `0` or `0.5`; `MAX_HOLD_S=0`; `CLOSE_TIMEOUT_S=0` | 23, 2, 10 |
| C4 | `OLLAMA_URL` → `ConfigError` for: `http://192.0.2.5:11435`, `http://example.com:11435`, `http://localhost:11435`, `http://0.0.0.0:11435`, `https://127.0.0.1:11435`, `http://127.0.0.1:11435/x`, `http://127.0.0.1:11435/?a`, `http://127.0.0.1:11435/#f`, `http://u:p@127.0.0.1:11435`, `http://127.0.0.1` (no port). Accepted: `http://[::1]:11435` and `http://127.0.0.2:11435`. `load_settings({})` with `SERIATIM_NODE_SLOTS=5` set in the real `os.environ` still gives `slots == 1` | 1, 23 |
| C5 | `LISTEN_HOST=0.0.0.0` without `ALLOW_UNAUTHENTICATED` → `ConfigError`; with it, accepted, and a `WARNING` is captured. `LOCK_DIR=/tmp/x` without `ALLOW_UNAUTHENTICATED` → `ConfigError`; with it, accepted | 15, 12b |
| C6 | `SERIATIM_NODE_SLOTZ=1` → `ConfigError` naming it. `SERIATIM_APP_ID=1`, `SERIATIM_CONFIG_DIR=/x` and `SERIATIM_GW_FOO=1` are ignored | 23 |
| C7 | `SERIATIM_NODE_CONFIG=/nonexistent.toml` → `ConfigError`; a malformed TOML file → `ConfigError` | 23 |
| C8 | `main()` with invalid config exits 2 and prints the key. With valid config it calls `uvicorn.run` with `workers=1` and `http="h11"` (monkeypatched) | 21, 23 |

### 8.2 `test_gate.py`

| id | test | REQ |
|---|---|---|
| K1 | Table-driven `classify`, with the expected (kind, scope, known) for every pair in §2.2 rules 2 to 4, including `POST /api/create` → gated, admin, known. Also gated + admin + `known=False`: `OPTIONS /api/tags`, `GET /api/chat`, `POST /api/chat/`, `POST /API/CHAT`, `post /api/chat`, `PUT /api/chat`, `POST /api/blobs`, `POST /api/blobs/sha256:XYZ`, `GET /api/unknown`, `POST /api/new-feature`. `SERIATIM` gives 200 for `GET /seriatim/health`, 405 for `POST /seriatim/health`, 404 for `GET /seriatim/nope` and `GET /seriatim` | 5 |
| K2 | `check_raw_path` raises for: `/api/chat/../show`, `/api/./chat`, `/api//chat`, `/api/%2e%2e/show`, `/api/%2E%2e/show`, `/api%2fchat`, `/api%2Fchat`, `/api%5cchat`, `/api\chat`, `/api/chat%00`, `/api/chat\x01`, `api/chat` and `//api/chat`. It accepts `/api/blobs/sha256:<hex>` and `/api/show%20x`. Hypothesis: any path containing a `.`/`..` segment or a listed escape raises | 20a |
| K3 | The pairs in `UNGATED_INFER`, `UNGATED_ADMIN` and `GATED_INFER` equal `tests/fixtures/route-pairs.json`, and equal the contract's paths. Every pair is marked `exists: true` in the route probe in `tests/fixtures/ollama-0.40.16.json` (see Appendix B for what "exists" means) | 5 |
| G1 | After the quarantine (fake clock advanced by `drain_s`), `try_acquire` returns a `Lease`; the slot is `HELD` | 2 |
| G2 | slots 1, one lease held → `Busy(retry_after_s=15, busy_for_s≥0)` | 2, 3 |
| G3 | slots 2: two leases, the third is `Busy` | 2 |
| G4 | `finish(clean=True)` → `FREE` at once. `finish(clean=False)` → `DRAINING`: `try_acquire` returns `Busy` with `retry_after_s ≥ ceil(remaining drain)` until the clock passes `drain_s`, then a `Lease` | 10 |
| G5 | `finish` twice: first `True`, second `False`, and the slot is not freed twice. With slots 1, an old lease finished again after a new lease is taken does not free the new one. `stuck()` then `finish()` → still `STUCK` | 10, 11 |
| G6 | `slots()` shows the state, holder and remaining drain. `overdue()` lists a holder only once `max_hold_s + grace` has passed (injected clock). `any_stuck()` is true after `stuck()` | 17, 18 |
| G7 | 1000 tasks call `try_acquire` concurrently with `asyncio.gather` (slots 1, after quarantine) → exactly one `Lease` | 2 |
| G8 | A new `Gate` returns `Starting(retry_after_s=ceil(drain_s))` until `drain_s` passes; `starting()` is true until then | 12a |
| G9 | Hypothesis: random sequences of acquire, finish(clean), finish(drain), stuck and clock steps (slots 1–4) keep `held + draining + stuck ≤ slots`, and never return a `Lease` while no slot is `FREE` | 2, 10, 11 |
| G10 | `Gate(slots=0)` and `Gate(drain_s=0)` raise `ValueError` | 2, 10 |

### 8.3 `test_http.py`

| id | test | REQ |
|---|---|---|
| H1 | A captured non-streaming `/api/chat` replayed: the client's status, `content-type` and body bytes equal the fixture; `x-seriatim-gate: acquired`; `x-request-id` is present | 6 |
| H2 | A body with unusual JSON (key order `{"options":{…},"model":…}`, extra whitespace, `é` escapes, `1.0e0` numbers, an unknown field `"x_future":[1]`, `num_ctx: 30000`, `top_k`, `seed`) → `fake_ollama` records **identical bytes** and the same `Content-Length` | 6, 7 |
| H3 | The client sends `Authorization`, `X-API-Key`, `Cookie`, `Connection: x-foo`, `X-Foo`, `Keep-Alive`, `X-Seriatim-Node`, `Accept-Encoding: gzip`, `Host: evil` and `X-Custom` → upstream gets none of the removed headers, `Host` is the upstream, `X-Foo` is gone (named in `Connection`), and `X-Custom` is kept. With no `User-Agent` or `Accept` from the client, upstream receives none (httpx defaults pruned) | 6, 13 |
| H4 | `GET /api/tags?x=1&y=%20` and `POST /api/show%20x` → the upstream raw path and query are byte-identical | 6 |
| H5 | Upstream 404 `{"error":"model \"nope\" not found"}` → 404 with an identical body; the slot is free at once (clean end) | 6, 25 |
| H6 | While a gated request is held: `GET /api/tags` → 200, `x-seriatim-gate: ungated`; `OPTIONS /api/tags` and `GET /api/chat` → 429 (gated, unknown) | 5 |
| H7 | While a gated request is held: `POST /api/pull` replaying captured pull progress → 200, an identical stream | 5, 6 |
| H8 | One raw keep-alive connection: two `/api/chat` requests, the second written to the socket immediately after the first response's last byte arrives, while a test hook holds the first request's `finally` at step (c) → the second is admitted (200), never 429. **H8b:** the same, but the first ends abnormally (upstream reset) → the next request within `drain_s` gets 429, and after `drain_s` (fake clock) it gets 200 | 2, 10 |
| H9 | Upstream answers 429 with `Retry-After: 3` and its own `X-Seriatim-Gate: busy`, `Access-Control-Allow-Origin: *` and `X-Rerouted-From: x` → the client gets 429 with Ollama's body, `Retry-After: 3`, `x-seriatim-gate: acquired`, and no CORS or `X-Rerouted-From` header | 6, 36 |
| H10 | A 100 MiB streamed `POST /api/blobs/sha256:<hex>` with `max_body_bytes=1 MiB` → forwarded intact (the SHA-256 of the received bytes matches). Not buffered: the in-process sidecar's `tracemalloc` peak during the upload stays under 16 MiB | 6, 20 |
| H11 | Upstream answers request A with `Set-Cookie: s=1` → the client does not get the header, and a later request B (another client) reaches upstream with no `Cookie` header | 6, T3 |
| H13 | A close-delimited upstream response (`raw_upstream`: no `content-length`, not chunked, then close) → treated as broken: the client's response is aborted, and the slot drains. A `Content-Length: 100` response cut at 50 bytes → aborted, and the slot drains. The client sends `X-Forwarded-For` and `Forwarded` → upstream receives neither | 6, 24 |
| H14 | With the ungated pool full (16 held `/api/show` requests on a stalled fake), the 17th `/api/show` → 503 `NODE_SATURATED`; a gated `/api/chat` and the health probe still succeed | 20, 18 |
| H12 | A raw-socket `HTTP/1.0` request to `/api/chat`, and one to `/api/tags` → 505 `HTTP_VERSION_UNSUPPORTED`; upstream received nothing; no slot taken | 21, 24 |
| B1 | Request 1 is held at upstream; request 2 (`/api/chat`) → 429 within the bound, with `Retry-After: 15` and `X-Seriatim-Gate: busy`. The body validates against `ErrorEnvelope`: `error_code: GPU_BUSY`, `retryable: true`, `replay_safe: true`, `retry_after: 15`, `source: sidecar`, `busy_for_s ≥ 0`. `fake_ollama` recorded **one** request | 3, 4, 25 |
| B2 | Request 1 ends cleanly → a new request 2 gets 200 at once | 10 |
| B3 | Raw socket: request 1 is held; request 2 sends only the headers, with `Content-Length: 10485760` → the 429 arrives without any body being sent | 3 |
| B4 | slots 2: two held requests proceed and the third gets 429; after one finishes cleanly, a fourth gets 200 | 2 |
| B5 | During startup quarantine: gated → 503 `NODE_STARTING` with `Retry-After ≤ drain_s`; `GET /api/tags` → 200; health → 503 with `starting` | 12a, 18 |
| E1 | `ollama_url` points at a closed port → 502 `UPSTREAM_UNAVAILABLE` with `replay_safe: true`; the next request also gets 502, not 429, because the slot was freed: nothing was sent | 26, 10 |
| E2 | An unsafe raw path sent over a raw socket (`POST /api/chat/../show`) → 400 `BAD_PATH`; upstream received nothing | 20a |
| E3 | `max_body_bytes=2048`: a 4096-byte body with an honest `Content-Length` → 413, and no slot was taken. The same body chunked, without a length → 413, and the slot was freed. Upstream received nothing | 20 |
| E4 | `body_timeout_s=0.5`: a raw socket sends half the body and stalls → 408 `REQUEST_BODY_TIMEOUT`; the slot was freed | 20 |
| E6 | A blob upload whose client stalls after 1 MiB has been forwarded → 408 `UPLOAD_INTERRUPTED` with `replay_safe: false`; the upstream connection is closed. A stall before the first chunk → 408 `REQUEST_BODY_TIMEOUT` with `replay_safe: true` | 20, 25 |
| E5 | An unexpected exception inside the proxy (monkeypatch `downstream_headers` to raise) → 500 `INTERNAL` with a generic message; the traceback is in the log and not in the body; the slot drains (the request reached upstream) | 26 |
| HL1 | Upstream `/api/version` 200 `{"version":"0.40.16"}` → health 200 `{"status":"ok"}`, no other keys | 18 |
| HL2 | Upstream down → 503 with `["ollama_unreachable"]` | 18 |
| HL3 | Upstream `/api/version` returns each of: 500; 200 `not json`; 200 `[]`; 200 `{}`; 200 `{"version":""}`; 200 `{"version":7}` → each gives 503 with `["ollama_bad_response"]` | 18 |
| HL4 | An injected overdue lease → `slot_overdue`; an injected stuck slot → `slot_stuck`. The body never contains the version string | 18 |
| L1 | After H1, B1, E1 and S4: one JSON log line each, with the fields in Appendix A. No line contains the body marker string, the `Authorization` value or the `X-API-Key` value | 22, 13 |
| K-OAS1 | `contract/openapi.yaml` passes `openapi-spec-validator` (OpenAPI 3.1). Every Ollama path's `x-seriatim-gate` and `x-seriatim-scope` agree with `classify()` (Seriatim's own `/seriatim/*` and `/models/select` paths are not Ollama routes and are not classified); `x-seriatim-unknown-endpoint` agrees with rule 5 | 5, G7 |
| K-OAS2 | Every collected error body validates against `ErrorEnvelope`, in-stream records against `StreamErrorRecord`, and health bodies against `Health` | 25 |
| K-OAS3 | `set(ErrorCode)` equals the contract's `ErrorCode.enum`, and `ERROR_TABLE` equals `x-seriatim-codes` (status, `retryable` and `replay_safe` for each code) | 25 |

### 8.4 `test_streaming.py`

| id | test | REQ |
|---|---|---|
| S1 | `fake_ollama` sends NDJSON chunk 1, then waits on an event → the client **receives chunk 1 before** the test sets the event (no buffering); then the rest arrives | 6 |
| S2 | A captured `stream:true` `/api/chat` NDJSON replay → the client's bytes equal the fixture; `content-type: application/x-ndjson` | 6 |
| S3 | A captured `/v1/chat/completions` SSE replay → identical bytes, including `data: [DONE]` | 6 |
| S4 | The client closes mid-stream → `fake_ollama` sees the disconnect within 1 s. The next gated request within `drain_s` gets 429 with `Retry-After ≥` the remaining drain; after the drain, 200. Log `client_disconnect` | 10 |
| S5 | Non-streaming request; the client closes while upstream holds before the headers → upstream sees the disconnect within 1 s; the slot drains | 10 |
| S6 | Upstream resets after 2 complete NDJSON lines → the client receives the 2 lines, then one valid JSON line `{"error":…,"error_code":"UPSTREAM_STREAM_BROKEN"}`, then a `RemoteProtocolError` (not a clean EOF); the slot drains | 24, 10 |
| S7 | `max_hold_s=1`; upstream streams one complete line and then holds → after about 1 s the client gets the `LOCK_HOLD_EXCEEDED` record and an aborted connection. Event timestamps show `fake_ollama` saw the disconnect **before** the slot entered `DRAINING`. A `CRITICAL` log line | 12, 11 |
| S8 | `max_hold_s=1`; upstream never sends headers → 504 `LOCK_HOLD_EXCEEDED`; upstream sees the disconnect; the slot drains | 12 |
| S9 | `max_hold_s=1` and the client disconnects at the same moment the deadline fires (both scheduled on the fake) → the log says `client_disconnect` or `hold_exceeded`, never both. Once the watcher has recorded the disconnect, the proxy makes no further `send` call (checked with a wrapped `send`). If the deadline won the race, at most one error write is attempted, its failure is caught, and nothing else is written. The slot drains exactly once | 10, 12 |
| S10 | Framing refusals, each with an upstream reset: the response has `Content-Length`; `Content-Encoding: gzip` (passed through raw); `content-type: application/json`; NDJSON cut mid-line; SSE cut mid-event → in each case **no** record is sent, and the connection is aborted | 24 |
| F1 | **Fault at connect.** The `raw_upstream` fake closes at once after accepting → 502 `UPSTREAM_PROTOCOL_ERROR` with `replay_safe: false`; the slot drains (a connection was made, so bytes may have arrived) | 26, 10 |
| F2 | **Fault during the write.** The `raw_upstream` fake stops reading a 32 MiB gated body, and the client disconnects during the write → the sidecar cancels the write, closes the socket (the fake sees EOF), and the slot drains; no 5xx is written to the gone client | 10 |
| F3 | **Fault while waiting for headers.** The `raw_upstream` fake sends a malformed status line → 502 `UPSTREAM_PROTOCOL_ERROR`; the slot drains | 26 |
| F4 | **Fault on close**, injected at the transport: the per-request client's connection pool `aclose` is made to block for ever → after `close_timeout_s`, the slot is `STUCK`, a `CRITICAL` line is logged, the next gated request gets 429 (never two at once), and health reports `slot_stuck`. A second case where the pool's `aclose` raises → also `STUCK`. In both cases the client still gets its response or abort within `2 × close_timeout_s` | 11, 18 |
| F5 | **Ollama crash before headers.** The upstream closes the connection after reading the body → 502 `UPSTREAM_PROTOCOL_ERROR`; the slot drains | 26 |

### 8.5 `test_process.py`

| id | test | REQ |
|---|---|---|
| R1 | Start the subprocess. The first gated request → 503 `NODE_STARTING`; after `drain_s`, 200 | 12a |
| R2 | **Kill and restart.** A request is in flight to `fake_ollama`; `Popen.kill()` the sidecar → `fake_ollama` sees the disconnect within 1 s. A new sidecar on the same port and lock directory starts and answers `NODE_STARTING` until `drain_s` has passed | 12a, 10 |
| R3 | **Duplicate start.** A second sidecar with the same upstream port and lock directory, on another listen port → exits with code 3 within 5 s, and stderr names the lock file. The first sidecar keeps serving, and after `gc.collect()` is triggered in it (through a test-only signal handler) a third attempt still exits 3 | 12b |
| R4 | The lock directory's parent is a regular file → exit 3 with `cannot lock` | 12b |
| R5 | Invalid config (`SERIATIM_NODE_SLOTS=0`) → exit 2, and stderr names `SERIATIM_NODE_SLOTS` | 23 |

### 8.6 `tests/behaviour/test_sidecar.py` (black-box, PPE)

These tests use only `SERIATIM_BASE_URL`, the PPE's direct Ollama debug port (for comparison)
and the capture log. They import nothing from `seriatim_node`. They run in the required
`ppe` CI job.

| id | test | REQ |
|---|---|---|
| P1 | `num_ctx` 6144, then 30000, with `top_k`, `seed` and `temperature` → the request bytes captured on the wire equal the bytes sent; `/api/ps` `context_length` equals the request | 6, 7 |
| P2 | 20 trials. In each, request 1 is a streaming `/api/chat` with a large `num_predict`; request 2 is sent only after request 1's first chunk has arrived, so they overlap by construction → request 2 gets 429 with `Retry-After` and `X-Seriatim-Gate: busy`. The 429 p99 latency is under 250 ms. The capture shows exactly one inference request per trial | 3, 4 |
| P3 | `stream:true` → the NDJSON lines parse, the final `done: true` arrives, and the time to the first byte is less than the total time | 6 |
| P4 | `/api/pull` of a small model while a gated request is held → the progress stream completes, and `/api/tags` lists the model | 5, 6 |
| P5 | `/api/tags`, `/api/ps` and `/api/version` through the sidecar equal the direct answers (bytes, after removing `Date`) | 6 |
| P6 | **Cancellation latency (a measurement, not a proof).** A client disconnects mid-generation. The test then sends a probe directly to Ollama (debug port), with the model already loaded, and measures the delay to its first token against an idle baseline taken the same way. The extra delay is recorded in the job summary. The test fails if it exceeds `drain_s / 2`. This is CPU evidence only; S5 and S6 repeat the measurement on each GPU node and record it (ADR-001 §8 risk 2) | 10 |
| P7 | **Restart mid-generation.** `docker compose kill node-a`, then `start`, while a generation is streaming → the client's stream is aborted. The test polls the port and sends a gated request the moment it accepts: that request gets `NODE_STARTING` (the PPE uses `drain_s=10`, so the window is wide). After `drain_s` it gets 200 | 12a |

### 8.7 Quality gates for this slice (CI output quoted in the PR)

- **Coverage.** `pytest --cov=seriatim_node --cov-branch --cov-fail-under=80`, plus a
  separate check of the branch percentage alone (from `coverage json`, `totals.percent_branches_covered`
  ≥ 80), with the output.
- **Mutation.** `mutmut run` on the changed files (`config.py`, `gate.py`, `proxy.py` and
  `app.py`), with a score of at least 75 % (SPEC REQ-61). The PR lists each survivor, with one
  line on why it is equivalent or which test was added.
- **PPE.** The `ppe` job's P1 to P7 output, including the P6 latency.
- **Size.** The PR-size check passes: 13 files.
- **Review.** An independent review of the PR diff. Every real finding is fixed and listed,
  and every rejected one is given with its reason.

## Appendix A — Data shapes

The 429 body, as sent by `ErrorFactory.response(GPU_BUSY, …)`:

```json
{"error":"GPU busy: another request is running on this node","error_code":"GPU_BUSY",
 "retryable":true,"replay_safe":true,"retry_after":15,"source":"sidecar","node":"node-a",
 "busy_for_s":12.4}
```

A log line, one per request:

```json
{"ts":"2026-11-16T10:00:00.123Z","level":"INFO","event":"request","request_id":"9f…",
 "method":"POST","path":"/api/chat","kind":"gated","scope":"infer","known":true,
 "outcome":"ok","status":200,"slot_end":"free","wait_ms":0.04,"hold_ms":5321.7,
 "bytes_in":812,"bytes_out":20411,"node":"node-a"}
```

- `slot_end` is one of `free`, `draining`, `stuck` or `none`.
- `path` is the raw path, which `check_raw_path` has already checked.
- The fields `client` and `model` are added in S2 and S3.

## Appendix B — What slice 1 needs from S0 (S0a and S0b)

- **S0a**:
  - **The uv workspace.** A root `pyproject.toml` with `[tool.uv.workspace] members =
    ["node"]` (later also `gateway`), plus the `ruff`, `pytest` and `mutmut` settings.
  - **The node package.** `node/pyproject.toml`, with the pinned dependencies in §1 and the
    console script `seriatim-node = seriatim_node.app:main`.
    `node/seriatim_node/__init__.py` holds `__version__`, and `__main__.py` calls `main()`.
  - **`.env.example`**, with the `SERIATIM_NODE_*` names from §6 as commented placeholders.
  - **The CI workflow** `ci.yml`, with these jobs:
    - `lint`;
    - `test` (matrix: ubuntu, windows, macos);
    - `coverage` (ubuntu, `--cov-fail-under=80`);
    - `mutation`;
    - `contract-lint`, `ip-literals` and `pr-size`;
    - `private-terms`, in its own workflow, as SPEC REQ-63 specifies for pull requests from
      forks.
  - **The mutation job.**
    - It finds the changed files with `git diff --name-only origin/main...HEAD --
      'node/**.py' 'gateway/**.py'`, and writes them to `[tool.mutmut] paths_to_mutate`.
    - It sets `also_copy` to the rest of the package, `contract/` and `tests/`, so the copy
      in `mutants/` can import and find its fixtures.
    - It runs the in-process tests only (`-m "not process and not behaviour"`). Subprocess
      tests would run the unmutated installed code.
    - It runs `mutmut run`, then `mutmut export-cicd-stats`.
    - It computes the score from the JSON export: (killed + timeout) ÷ (total − skipped).
    - It fails if the export is missing, malformed or has unexpected keys, or if the score is
      below 75 %.
- **S0b**:
  - **`ppe/compose.yaml`**, with these services:
    - `ollama-a`: `ollama/ollama:0.40.16`, with env `OLLAMA_HOST=127.0.0.1:11435`,
      `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_CONTEXT_LENGTH=2048`, and a debug port published
      on loopback only, through a small `socat` helper, for direct comparison;
    - `cap-a`: tcpdump in `ollama-a`'s network namespace, with the filter
      `tcp dst port 11435` and the capabilities `NET_RAW` and `NET_ADMIN` only.

    The limits sum to at most 4 CPUs and 6 GiB.
  - **`ppe/ppe up`.** Before anything starts, it checks the effective configuration
    (`docker compose config`) as SPEC §8.3 requires. In a local run it also checks the
    hostname against the gitignored `ppe/.allowed-host`. `--ci` is accepted only when
    `GITHUB_ACTIONS=true`. It then pulls `qwen2.5:0.5b`, `qwen2.5:1.5b` and `all-minilm:22m`, with no
    `ollama cp` aliases.
  - **Fixtures.** `tools/capture_fixtures.py` drives `ollama-a` directly. For each exchange
    it records:
    - `id`: for example `chat.stream`, `chat.nostream`, `generate.stream`, `embed`,
      `v1.chat.stream`, `v1.chat.nostream`, `tags`, `ps`, `version`, `show`,
      `pull.progress`, `chat.model_not_found` or `chat.bad_json`;
    - the request: method, path and body;
    - the response: status, headers minus `Date`, the body as base64, and the chunk
      boundaries as observed;
    - `ollama_version` and the model digest.

    It also writes a **route probe**, for every pair in `tests/fixtures/route-pairs.json`.
    S0b commits that file, copied from SPEC REQ-5, and S1's K3 checks `gate.py` against it.
    - **Valid requests.** Each pair is sent a valid request: an installed model for
      inference and `show`, and a blob created first for the blob routes.
    - **What "exists" means.** The pair is recorded as `exists: true` unless the response
      is the router's own not-found answer: HTTP 404 with the plain-text body
      `404 page not found`. A JSON 404 such as "model not found" comes from a handler, so it
      proves the route exists. The script removes anything machine-specific (no IPs, no host names) and writes
    `tests/fixtures/ollama-0.40.16.json`.
