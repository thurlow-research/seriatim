# TD — Slice 1: contract, sidecar gate, 429 and passthrough

**Status:** revision 8, for review. **Date:** 2026-11-17. Revision 5 updates Appendix B
to the mutation layout S0a built (PR #13); revision 6 adds the enforcement line below and
the `td-test-ids` job to Appendix B (#16); revision 7 says what "exists" means for `HEAD` in
the route probe (Appendix B, S0b; #29); revision 8 adds the 57 scenarios of #15 as tests
(57 new IDs; Appendix C maps each one), fixes the design gaps they found (Appendix C,
"Design changes"), and splits the code into the PR series of §9.

Enforced: yes

(The `td-test-ids` check, SPEC REQ-63, reads this line. S1's code ships as the series of
small PRs in §9. The **last** one, S1-14, set it to `yes`, so every ID in §8 needs a passing
test marked `@pytest.mark.td("<ID>")`. Each earlier PR marked the tests of the IDs it
delivered, as §9 lists them.)
**Implements:** SPEC REQ-1 to REQ-7, REQ-10 to REQ-12b, REQ-15 (pre-auth form), REQ-18
(S1 form), REQ-20 to REQ-26. **Architecture:** ADR-001 D1 to D4, D7, D8, D10 to D12.
**Not in this slice:**
- auth (S2);
- the `num_ctx` policy (S3);
- `/seriatim/status` and the exposure check (S4a), the capacity report (S4b), the durable admin switch (S4c) and node metrics (S4d);
- deploy (S5, S6), releases (SPEC §7.6), the release follower (§7.7) and the interim deployment (§7.8);
- the gateway (S7 and later);
- TLS (S11).

**Exposure before S4a.** Until S2 and S4a land, the sidecar has no authentication and no
exposure check. It therefore refuses a non-loopback listen address unless
`SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true` is set (SPEC REQ-15), and only the PPE sets that.
Slices S1 to S3 are not deployed to GPU nodes. The first node deploy needs S4a, because REQ-19
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
| 6 | `tests/node/conftest.py` and `tests/node/harness.py` | the real-socket harness: the sidecar and a fake Ollama on ephemeral ports, a fixture replayer and a raw-socket fake (helpers in `harness.py`, fixtures in `conftest.py`) |
| 7 | `tests/node/test_config.py` | C1 to C11 |
| 8 | `tests/node/test_gate.py` | K1 to K6, G1 to G12 |
| 9 | `tests/node/test_http.py` | H1 to H29, B1 to B7, E1 to E16, HL1 to HL9, L1 to L3, K-OAS1 to K-OAS3 |
| 10 | `tests/node/test_streaming.py` | S1 to S21, F1 to F5 |
| 11 | `tests/node/test_process.py` | R1 to R9 (subprocess: startup quarantine, kill and restart, duplicate start, the lock file) |
| 12 | `tests/behaviour/test_sidecar.py` | P1 to P7 (black-box; the required `ppe` CI job) |
| 13 | `ppe/compose.yaml` (modified) | adds the `node-a` sidecar container in `ollama-a`'s network namespace |

**Dependencies.** S0a pins these in `uv.lock`, the runtime ones also exactly in `node/pyproject.toml`:
- **Runtime:** `starlette`, `uvicorn` (h11 only; no `[standard]` extra, so `httptools` is never
  picked), `httpx`, `anyio`, `pydantic-settings`. The singleton lock uses the standard
  library only (`fcntl`, `msvcrt`).
- **Test:** `pytest`, `pytest-cov`, `hypothesis`, `openapi-spec-validator`, `jsonschema`,
  `pyyaml`, and `mutmut>=3.3,<4`, in the root `pyproject.toml`'s `dev` dependency group.
  The `[tool.mutmut]` settings are in `node/pyproject.toml`, because mutmut runs from
  `node/` (Appendix B).

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

def default_lock_dir() -> Path           # %ProgramData%\seriatim\run | /Library/Application Support/seriatim/run | /run/seriatim
def load_settings(environ: Mapping[str, str] = os.environ) -> Settings
```

Every float setting must be finite: `nan`, `inf` and `-inf` are refused (`allow_inf_nan=False`;
C9). Enum values are case-sensitive, so `LOG_LEVEL=debug` is refused.

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
   - the port is explicit, and 1–65535 (`:0` is refused);
   - the path is empty or `/`;
   - there is no query, fragment or user info;
   - the host is an IP literal in `127.0.0.0/8`, or `::1`.

   `localhost` is a name, not a literal, so it is refused. No name resolution ever happens.
5. **Listen address.** If `listen_host` is not a loopback literal and `allow_unauthenticated`
   is false, raise `ConfigError`. When it is true, `load_settings` logs a `WARNING`, so it
   appears at startup. As for `ollama_url`, a name is not a literal: `localhost` is refused
   too, and so is any non-loopback IPv6 address, including `::` (C11).
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

**Details fixed by the code (S1-3).**
- **`SERIATIM` routes** have scope `none`, and `known` is true only with status 200. A 404 or
  405 is `known=False`.
- **`BLOB_RE` is used with `fullmatch`.** With `match` or `search`, its `$` would also match
  before a trailing `\n`. `check_raw_path` refuses that byte anyway, but `classify` doesn't
  rely on that, so a digest followed by `\n` is never classified as a blob.
- **`classify` doesn't call `check_raw_path`.** The dispatcher calls `check_raw_path` first
  (S1-5). `classify` is total and never raises.
- **`PathError` messages** name the rule that refused the path, for example "the path
  contains %2e". K2 asserts each one.

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
  clock()))` and `busy_for_s` = the age of the oldest `HELD` holder (0 if no slot is `HELD`).

**`Lease.finish(clean=True)`** sets the slot to `FREE`. **`finish(clean=False)`** sets it to
`DRAINING`, with `drain_until = clock() + drain_s`.

**Details fixed by the code (S1-4).**
- **Construction.** `Gate` also refuses `retry_after_s < 1` and `max_hold_s ≤ 0`, as well as the
  `slots < 1` and `drain_s ≤ 0` of G10. NaN counts as invalid for all four, and G10 tests it.
- **Arguments.** `Gate` takes plain arguments, not `Settings`, so `gate.py` does not import
  `config.py`.
- **`busy_for_s` and `overdue()`** count `HELD` slots only. A `STUCK` slot is reported by
  `any_stuck()` (`slot_stuck`), never as busy time or as overdue.
- **What `slots()` shows.** A `STUCK` slot keeps its holder there. A `DRAINING` or `FREE` slot
  has none.
- **Old leases.** A lease ends at most once: after `finish` or `stuck`, every later call returns
  `False` and changes nothing. So an old lease can never free a slot that a newer lease now holds.

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

**Details fixed by the code (S1-6).** S1-6 implements §3.3 (the three clients, the lifecycle,
the error mapping before the headers and Seriatim's own response headers). It leaves these
seams for the later slices:
- **Header signatures.** `upstream_headers` also takes `request_id`, and `downstream_headers`
  takes `node_id`. They set `X-Request-Id` (both ways) and `X-Seriatim-Node` (downstream).
- **Strip lists.** S1-6 shipped interim lists. S1-7 replaces them with the full lists of §4
  and adds `prune_httpx_defaults` (see "Details fixed by the code (S1-7)" below).
  `upstream_headers` still appends `content-length` and `x-request-id` after the strip, and
  `downstream_headers` still appends `x-seriatim-gate`, `x-seriatim-node` and `x-request-id`.
- **`read_body(receive)`** had no size limit and no timeout until S1-8 (see "Details fixed by
  the code (S1-8)" below). Ungated bodies are read whole too (`body_stream` arrives with blob
  streaming in S1-11).
- **The end of the stream.** `upstream_done` is set when `aiter_raw()` ends normally. S1-9
  adds the framing rule of §3.3 step 5 and the in-stream error record. Until then an abort
  after the headers returns without the final body message: the client sees a cut-short
  response, and the slot drains. `Outcome.tail` keeps the last two bytes for S1-9.
- **An abort closes the client connection.** uvicorn closes a connection whose response ended
  without its final body message, so no second request can follow on it. H8b therefore checks
  the cut-short body and the close on one raw socket, and sends its 429 and 200 requests on
  new connections.
- **The hold deadline** is read from the gate's own clock through a new
  `Lease.remaining_s()` in `gate.py`. Ungated requests have no hold limit.
- **Where the clients are built.** `new_gated_client` and `new_ungated_client` are in
  `proxy.py`, on top of `app.new_client`, which applies the settings every client shares. The
  lifespan imports `Proxy` lazily, because `proxy.py` imports `app.py`.
- **`PoolTimeout`** maps to 503 `NODE_SATURATED` with `Retry-After: retry_after_s` now, as
  §5 says. S1-10 adds its over-the-wire test; S1-6 unit-tests the mapping.
- **The request log line** has `request_id`, `method`, `path`, `kind`, `scope`, `known`,
  `outcome`, `status` and `slot_end` (`free`, `draining`, `stuck` or `none`). S1-13 adds the
  rest. `outcome` is one of `ok`, `busy`, `starting`, `client_disconnect`, `hold_exceeded`
  (CRITICAL), `upstream_unavailable`, `saturated`, `upstream_error`, `internal_error` (ERROR)
  or `cancelled`. When a close isn't proven, the line also has `upstream_close_failed: true`,
  at CRITICAL for a gated request (the slot is `STUCK`) and at ERROR for an ungated one.
- **An external cancellation** (server shutdown) sends nothing more to the client, still runs
  the shielded close proof, and drains the slot if the request was sent.
- **`main()`** is still the S0a placeholder, so the sidecar can't be started from the command
  line until S1-12.

**Details fixed by the code (S1-7).** S1-7 implements the header rules of §4.
- **What `prune_httpx_defaults` compares against.** Its `client_headers` argument is the list
  `upstream_headers` returned, not the client's raw headers. So `accept-encoding` and
  `connection`, which are on the strip list, never come back as httpx defaults, even when the
  client sent them. A client's own `accept` and `user-agent` are forwarded with the client's
  values.
- **Which defaults are pruned.** `HTTPX_DEFAULTS` holds `accept`, `accept-encoding`,
  `connection` and `user-agent`. ADR-001 §5.3 names three of these; `connection` is the
  fourth, because httpx adds it too. A unit test checks that the list equals httpx's own
  defaults, so an httpx upgrade that adds one fails the suite. `host` and the body framing
  are httpx's and stay. A `cookie` header is always removed (§2.4, §3.3 step 3); its unit test
  uses a client whose jar does hold a cookie.
- **`Connection` tokens** are read from every `Connection` header, split on commas, trimmed
  and lower-cased (`connection_tokens`). The same rule applies to requests and responses.
- **`proxy-*` and `date`** (#82, from the maintainer's answers on #57). Requests lose every
  `proxy-*` header by prefix, as ADR-001 §5.3 writes; responses lose Ollama's `date`, so
  uvicorn's is the only one. H16's tests check both.
- **Case.** h11 delivers header names in lower case, so the mixed-case names of H16 can't
  reach the proxy over the wire. The unit tests of `upstream_headers` and `downstream_headers`
  check case-insensitive matching with every name on both lists.
- **Already true in S1-6.** H17, H18, H20, H21 and H24 pass against the S1-6 code: it kept
  repeated headers, replaced `x-request-id` both ways, and built its clients with
  `trust_env=False` and `follow_redirects=False`. S1-7 adds their tests. H18's `a\r\nb` case
  is the unit test of `request_id_from`, because a value with CR or LF can't be sent.
- **H20** sets the variables in upper and lower case. It first checks that a client that does
  trust the environment goes through the fake proxy, so the test can't pass vacuously.
- **H21's "nothing was sent to the `Location`".** The `Location` is a documentation address
  that nothing answers, so following it would end in a 502 or a timeout, not the 302.

**Details fixed by the code (S1-8).** S1-8 implements the request limits of REQ-20 (§3.3
lines 3 and 4 and step 1, §5).
- **The size check before admission.** It runs in `Proxy.__call__` for every non-Seriatim
  route, before `try_acquire`, so it comes before 429 and 503 (B7).
  - `declared_length(headers)` returns the first `content-length` value, or None for a chunked
    body. h11 has already refused a malformed or conflicting value and folded repeats into one.
  - The 413 carries `X-Seriatim-Gate: ungated`. Its log line has `outcome: rejected` and
    `slot_end: none`.
  - It never calls `receive()`, so uvicorn never sends `100 Continue` for it. The 429 and 503
    paths don't call it either (B6), and that needed no new code. B6 also checks the contrast:
    an admitted request with `Expect: 100-continue` does get `100 Continue`.
- **Blob uploads** are still buffered until S1-11, so until then the limit and the timeout
  apply to them too. S1-11 exempts them when it adds `body_stream`.
- **`read_body(receive, *, limit, timeout_s)`.**
  - It raises `BodyTooLarge` as soon as more than `limit` bytes have arrived, and never reads
    the rest.
  - It raises `BodyTimeout` when one `move_on_after(timeout_s)` around the whole loop expires.
    That deadline starts at the first `receive()` and is never reset (E8).
  - The hold deadline encloses the body read, so whichever passes first decides: the hold
    limit gives 504 `LOCK_HOLD_EXCEEDED` with the slot freed (E9), and the body timeout gives
    408.
- **The replies.** `BodyTooLarge` → 413 `BODY_TOO_LARGE`, "the request body is too large";
  `BodyTimeout` → 408 `REQUEST_BODY_TIMEOUT`, "the request body did not arrive in time".
  Both are logged as `outcome: rejected` at `INFO`. After admission they carry
  `X-Seriatim-Gate: acquired` and the slot is freed with no drain, because nothing was sent.
- **A reply sent before the body is fully read closes the connection** (#85 review).
  uvicorn 0.54 never closes a connection for an unread body: once the response is complete
  it keeps reading, and while h11 is still in `SEND_BODY` it takes the next bytes as the rest
  of the old body. A keep-alive retry after a 408 (which is `retryable`) would therefore be
  parsed as body bytes, giving a spurious 400 or a desync; and after a refused
  `Expect: 100-continue` request, whose client never sends the body, the next request would
  wait unanswered. So every reply sent while a declared body is still unread carries
  `Connection: close`, and uvicorn closes the connection after it:
  - the 413 before admission;
  - the 429 and 503 refusals, when the request declares a body;
  - the 413 (`BodyTooLarge`), the 408 (`BodyTimeout`) and the hold limit's 504 (E9) during
    the read, and any other error reply before the read completes.
  `has_body(headers)` says whether a body is declared: `Transfer-Encoding`, or a
  `Content-Length` above 0. `Outcome.body_pending` starts as that and turns false once
  `read_body` returns. A reply to a request with no body (a `GET` refused with 429) keeps the
  connection, and so does an error after the body was read (502, 504 before the headers, 500).
  RFC 9110 §15.5.9 says a server SHOULD close the connection on a 408.
  H29 tests this: a refusal mid-body (408, 413 while the body is still streaming, 504) and a
  refused `100-continue` request (413, 429, 503) are each followed by a request on the same
  socket, which must be answered correctly or find the socket closed. A bodyless 429 keeps
  the socket, and the next request on it is answered.
- **`Transfer-Encoding` together with `Content-Length`** (asked on #58). h11 0.16 does not
  refuse this framing. It uses `Transfer-Encoding` alone (RFC 9112 §6.3) and keeps the
  connection open, so a request hidden after the chunked body would be served. The dispatcher
  therefore refuses any request that has both headers: 400 `BAD_REQUEST_BODY`, "Transfer-Encoding
  and Content-Length together", with `Connection: close` (RFC 9112 §6.1 says the server MUST
  close), `X-Seriatim-Gate: ungated` and `outcome: rejected`. The check runs right after the
  HTTP-version check and before the path check, so `/seriatim/*` is covered too. This adds
  `app.py` to S1-8's files. The other invalid framings of H28 are refused by h11, and uvicorn
  answers those with its own `text/plain` 400 and a close.

**Details fixed by the code (S1-9).** S1-9 implements the framing rule of §3.3 step 5 and
the errors after the headers of §4 and §5.
- **The framing rule** is `upstream_framed(method, status, headers, received)`, with the
  bodyless rule (`bodyless(method, status)`: `HEAD`, 1xx, 204, 304) checked first.
  - A response with `Transfer-Encoding` counts as framed. h11 accepts only `chunked` there,
    and raises `RemoteProtocolError` itself when the terminating chunk never comes or a
    chunk-size line is malformed (S20), so an iterator that ended normally saw the end.
  - A `content-length` must equal the bytes `aiter_raw()` yielded. h11 already raises on a
    short body (H13), so this comparison is defence in depth.
  - A close-delimited response raises `Unframed`, a subclass of `httpx.RemoteProtocolError`.
    It therefore takes the same path as any transport error after the headers: a record if
    the framing allows one, an abort, a drain, and log `outcome: upstream_error`.
- **Which failures get a record.** The hold limit gives `LOCK_HOLD_EXCEEDED`, and any
  `httpx.TransportError` (including `Unframed` and an ungated read timeout) gives
  `UPSTREAM_STREAM_BROKEN`. The messages are "the request exceeded the hold limit" and
  "Ollama's response stream broke". An internal error or an external cancellation after the
  headers aborts with no record, because the contract's `StreamErrorRecord` allows only
  those two codes. A disconnect sends nothing.
- **`record_framing`.** The media type is compared after trimming and in lower case, and so
  is `content-encoding` against `identity`. Any `content-length` at all refuses a record. The
  NDJSON boundary is a final `\n`, and the SSE boundary is a final `\n\n`, so an SSE stream
  that uses `\r\n` gets no record. Ollama sends `\n`.
- **When the record is sent.** It goes in step (c), after the close proof and the end of the
  lease, as one `http.response.body` with `more_body=True`. The proxy then returns without
  the final message.
- **The traceback of an internal error** (E5) goes in the request's own log line, as a
  `traceback` field (`traceback.format_exception`, with no local values), so there is still
  exactly one line per request (L2). The client gets only the generic `internal error`.
- **The `raw_upstream` fixture** (§7) is `RawUpstream` in `harness.py`: a blocking socket
  server that runs `handler(fake, conn)` for each connection on its own thread, with
  `read_request` and `wait_closed` helpers. It is not an `asyncio.start_server`: a thread is
  simpler, and works the same on every OS. A fake Ollama reset is `reset_after(headers,
  *chunks)`, which returns without finishing the response.
- **F1's error.** When the fake closes at once, the sidecar sees `RemoteProtocolError`,
  `ReadError` or `WriteError`, depending on the OS and on timing. Each one is a
  `TransportError` that is not in `_NOT_SENT`, so each gives 502 `UPSTREAM_PROTOCOL_ERROR`
  and a drain.
- **S7's ordering.** The test wraps `Lease.finish` and blocks the sidecar's event loop in it
  until the fake has seen the disconnect. The fake can see it then only if the sidecar had
  already closed the socket, so "the disconnect came before `DRAINING`" is proven, not
  timed.
- **S9 and S19 run in process.** They call the proxy directly, with httpx's `MockTransport`,
  so the chunk boundaries are exact (a socket may merge one-byte chunks), and so the
  disconnect and the deadline share one event loop's clock. S9 pins each order rather than
  sampling the race: with `max_hold_s=0.4`, a disconnect 150 ms before the deadline must
  log `client_disconnect` with nothing sent after it; one 150 ms after must log
  `hold_exceeded` with exactly one write (the record) and nothing after it; and in a third
  case the deadline wins and that one write fails, which must be caught, still log
  `hold_exceeded` and write nothing more. Every case drains the slot exactly once. The
  margin keeps each order fixed on a slow runner; no case sits on the race itself.
- **S21's `HEAD /`** is an ungated route, so its case checks the log line (`ok`). The
  gated cases are the 204 and the 304, which the raw fake sends with no length and no
  chunking, and with the connection left open. Without the bodyless rule, each would look
  close-delimited and drain.

**Details fixed by the code (S1-10).** S1-10 implements the cancellation and close edge
cases of §3.3 and §5 and tests the ungated pool and its timeouts over the wire.
- **A second cancellation** (S12). anyio's shield holds off anyio's own cancellations only.
  uvicorn cancels a request's task on shutdown with asyncio's `Task.cancel()`, and a second
  such call lands inside the shielded `finally`: it would cut `aclose()` short and skip
  step (b), so the lease would never end. The `finally` therefore runs steps (a) to (c) and
  the log line (`Proxy._end`) in a task of its own (`_uninterrupted`), which the request's
  task awaits through `asyncio.shield`. A cancellation that arrives meanwhile is noted, the
  wait goes on, and the cancellation is raised again once the cleanup has ended, so the task
  still ends cancelled. Each step keeps its own `close_timeout_s` bound, so the wait is at
  most `2 × close_timeout_s`. This ties the proxy to asyncio, which is what uvicorn runs.
- **Every upstream close is in step (a)** (H26). `aiter_raw()` closes the response itself
  when the body ends, inside the exchange, where nothing bounds an ungated request. So the
  relay iterates `resp.stream` instead: the same raw bytes, with httpx's exceptions, and no
  close at the end. For an ungated request, step (a)'s `resp.aclose()` is then the close on
  every path, a clean end included, and `close_timeout_s` bounds it. For a gated one,
  `req_client.aclose()` closes the connection, as before.
- **`client_write_failed`** (§3.3 line 40, S15). L2 wins over §3.3's "log DEBUG" wording,
  per the maintainer's ruling on #60
  (https://github.com/thurlow-research/seriatim/issues/60#issuecomment-7048425226). So a
  failed write writes no line of its own. When step (c) raises or passes
  `close_timeout_s`, the request's own line gets `client_write_failed: true`, at that
  line's own level, and `client_write_failed_reason` (`timeout`, or the exception's class
  name). Its other fields are unchanged (`outcome: ok`, `slot_end: free` after a clean
  end), so a request has exactly one line at every log level.
- **S11 to S13 run in process**, with the proxy as an asyncio task cancelled by
  `Task.cancel()`, as uvicorn does. S11 uses real fakes upstream, so the fake sees each
  close: the body read (nothing sent; freed), the upstream write (a raw fake that stops
  reading a 32 MiB body; it then reads to EOF and got less than the body), the wait for the
  headers and the stream (a `Hold`; drained). S12 and S13 use a gated client whose
  `aclose()` takes 0.5 s, with `close_timeout_s=2`, and record the slots at the moment it
  returns: still `HELD`. S13 checks both ends of (b): a clean end frees and a broken one
  drains.
- **S16 pins each order** rather than sampling the race, in process, 50 runs each. A
  disconnect recorded while the last chunk is still being written gives `client_disconnect`
  and a drain, with nothing written after it. A disconnect delivered in the same step as
  upstream's end is never recorded: the exchange runs on to its end with no `await`, and the
  task group cancels the watcher before it wakes, so the response ends cleanly (`ok`, freed,
  the final message). Every run has one effective end and one log line.
- **S14 and F2 use 32 MiB**, far more than every socket buffer on the path, so the fake can
  never finish while the other side stalls: in S14 the client sets a 4 KiB receive buffer
  and reads nothing, and the fake's `sendall` fails when the sidecar closes at the deadline;
  in F2 the fake reads only the headers, and the client half-closes its socket, which
  uvicorn takes as a disconnect. **"The write was cut short" is checked on POSIX only.**
  On POSIX, anyio's close aborts the transport, so the bytes it had not yet written are
  dropped and the fake reads less than the body. On Windows, asyncio's proactor hands the
  whole 32 MiB to the OS as one overlapped send. The OS completes it once the fake reads,
  even after the cancel and the close, so the fake reads the whole body and then EOF (seen
  on the Windows runner, with or without small socket buffers). Every other clause of F2
  and S11's write phase runs on every OS: the cancel during the pending write, the close
  the fake sees, the drain, and nothing written to the client. The sidecar is deployed on
  Linux.
- **F4 injects the fault at the transport**: the per-request client's `httpcore` pool gets
  an `aclose` that sleeps for ever or raises, beneath httpx's own `aclose`. A clean response
  with a `content-length` reaches the client before step (a), so the test times the slot
  turning `STUCK` (at `close_timeout_s`, or at once when the close raises) and checks the
  client's whole exchange against `2 × close_timeout_s`.
- **H14 and H27** fill the shared pool with 16 `/api/show` requests held before the headers,
  started one at a time, each once the previous one has reached the fake. The fake parks
  them with a 50 ms poll rather than `Hold`'s 2 ms, so 16 of them add little load.
  `connect_timeout_s=1` is also the pool timeout, so the 17th gets its 503 after 1 s. **H26** injects the blocking `aclose()` into the shared client's transport
  (each response's stream closes the real one, then sleeps for ever), for a clean chunked
  end (answered) and a read timeout mid-stream (aborted). Its disconnect case uses a read
  timeout far beyond the disconnect, so the order is fixed.

**Details fixed by the code (S1-11).** S1-11 streams blob uploads (§3.3 steps 1 and 2, §5).
- **What is streamed.** `streamed(method, raw_path)` is true for `POST` on `BLOB_RE` only. A
  `HEAD` on a blob path has no body and takes the buffered path. The size check before
  admission and `read_body`'s limit skip a streamed body, so H10's 100 MiB upload passes
  with `max_body_bytes` at 1 MiB, while the same `Content-Length` on `/api/show` gets 413.
- **The signatures.** The §2.3 sketch's `body_stream(receive)` is two functions:
  - `first_chunk(receive, *, timeout_s) -> (bytes, more_body)` reads the first chunk
    **before** Ollama is contacted. It skips empty messages that are not the last, and one
    `move_on_after(body_timeout_s)` bounds the whole wait, as in `read_body`. A stall gives
    `BodyTimeout` (408 `REQUEST_BODY_TIMEOUT`, `replay_safe: true`, `outcome: rejected`); a
    disconnect gives `ClientGone` (nothing sent, `client_disconnect`). Either way Ollama
    never sees a connection (E6, E12).
  - `body_stream(receive, first, more, *, timeout_s, on_end)` is the async iterator httpx
    sends. Each `receive()` has its own `move_on_after(body_timeout_s)`, so the timeout is an
    idle timeout between chunks and the total time is unbounded; an ungated request has no
    hold limit. Empty chunks are not yielded.
- **`UploadInterrupted(gone=…)`** is raised by `body_stream` for a stall (`gone=False`) or a
  disconnect (`gone=True`). It leaves `client.send()` unmapped (httpx maps only httpcore's
  own errors), and httpcore closes the half-written connection itself rather than return it
  to the pool, so no response exists and step (a) has nothing more to close. E6 and E12
  check that the fake sees the close.
  - A stall gets 408 `UPLOAD_INTERRUPTED`, "the upload was interrupted", with
    `replay_safe: false` and `Connection: close`, because the rest of the body is unread.
  - A client that left gets nothing, as when a client leaves during a buffered body: there
    is nobody to answer. Its line has `status: null`. §5's row says 408 for both cases, so
    this departs from it, and the row now says so (asked on #62).
  - Both log `outcome: upload_interrupted` at `INFO`, with `slot_end: none`.
- **The framing.** `upstream_headers(body_len=None)` keeps the client's `Content-Length`,
  which h11 has validated (H10), or sends none, and httpx then chunks the body (E13). An
  empty chunked upload goes as a chunked body with only the terminating chunk, and Ollama's
  status comes back unchanged.
- **The watcher.** `on_end()` starts it as soon as `body_stream` sees the last message,
  before that chunk is yielded; `body_stream` calls `receive()` no more after that. If Ollama
  answers before it has read the whole upload, `send()` returns while the body is still
  unread. The watcher then takes `receive()` over, so a disconnect is still seen, and it
  discards the rest of the body, because it ignores `http.request` messages. The relayed
  response carries `Connection: close`, by S1-8's rule for a body still unread.
- **The harness.** `FakeOllama.sink = True` records each request at once and hashes its body
  as it arrives: `length` grows while the body streams in, `sha256` is set at its end, and
  `complete` turns false if the sidecar closes first. `start_sidecar(wrap=…)` runs a wrapped
  app, which E14 uses to guard `receive()`. A unit test shows that the guard does count two
  pending calls as an overlap, so E14 can't pass vacuously.
- **H10's memory bound.** `tracemalloc` traces the whole test process: the client, the
  sidecar's thread and the fake's. The client's 1 MiB block is allocated before tracing
  starts and is sent 100 times from a `memoryview`, so the peak (about 3 MiB locally) is the
  servers' buffers. A buffered upload would put the peak above 100 MiB.

**Details fixed by the code (S1-13).** S1-13 completes the request log line (Appendix A). It
also tests the log, Seriatim's own headers and the contract's bodies on every path built so
far.
- **Ruled on #64** by the maintainer (https://github.com/thurlow-research/seriatim/issues/64#issuecomment-7053085830): question 1 is option A and question 2 is
  option B, as built below.
- **The four new fields** (asked on #64, question 1, option A). Every line has
  `wait_ms`, `hold_ms`, `bytes_in` and `bytes_out`, after `slot_end` and before `node`, in
  Appendix A's order. That includes the dispatcher's refusals and health.
  - `wait_ms` is the time from the proxy's start to the gate's answer. It is set on every
    gated request the gate answered: admitted, 429 or 503 `NODE_STARTING`. It is `null` when
    no gate was asked: an ungated route, a 413 before admission, a dispatcher refusal, health.
  - `hold_ms` is the time from admission to the lease's end in step (b), or to `STUCK` for an
    unproven close. It is `null` when no slot was taken.
  - Both come from `time.perf_counter()`, not the gate's clock (the tests run the gate on a
    fake clock), in milliseconds rounded to 2 decimal places.
  - `bytes_in` is the request body bytes the sidecar received, counted by a wrapped
    `receive`:
    - 0 when the body was never read (the dispatcher, a 413 before admission, 429, 503);
    - what had arrived, for a client that left mid-body;
    - for a blob upload, every byte received, including any the watcher discards after an
      early answer.
  - `bytes_out` is the response body bytes whose `send()` returned, counted by a wrapped
    `send`: Ollama's relayed bytes, Seriatim's error body, or Ollama's bytes plus an in-stream
    record. Headers don't count, and a write that failed adds nothing. The same holds on every
    path (#91 review): the proxy wraps its `send` (`_counted`), and the dispatcher's replies
    and health use `SentBytes`. Health's line is written by `_health_route` once Starlette
    has sent the response, not by the handler; `_health` leaves its status in the scope.
- **L2 when the write fails** (#91 review). The refusals sent before any slot is held (the
  413 before admission, 429, 503 `NODE_STARTING`; `Proxy._refuse`), the dispatcher's
  replies and health write their line in a `finally`. A write that raises is noted on that
  line (`client_write_failed`, `client_write_failed_reason`), as at the end of an exchange
  (S1-10), and isn't re-raised: the client is gone, and nothing is left to tell it. In
  health, only a failed write is swallowed. Any other error in the handler still reaches
  uvicorn after the line, which says `internal_error`, at `ERROR`, with `status: null`.
  `test_l2_a_refusal_whose_write_fails_…` drives each of these paths three ways: `send`
  raises at the start, raises at the body, or succeeds.
- **A refusal test sends no body** (#91 review). On the first Windows run of #91,
  `test_f4_an_unproven_close_marks_the_slot_stuck[hang]` failed on its 429 with
  `httpx.ReadError: [WinError 10053]`. The cause:
  1. httpx writes a request's body after its headers, so the 2-byte body can reach the
     socket after uvicorn's last read.
  2. The 429 carries `Connection: close` and the body is never read (S1-8, B3). uvicorn
     closes with those bytes unread, and the OS answers that close with a reset.
  3. Windows drops the received but unread response when the reset arrives. Linux still
     delivers it first, so the race shows only on Windows.

  A test that needs only the refusal now sends it with no body (`refused_post` in
  `harness.py`), so no close follows. That covers F4, B1, B4, L1, and the drain tests in
  `test_http.py` and `test_streaming.py`. The close itself is tested with raw sockets, which
  write the whole request at once. A real client on Windows that sends a body to a busy node
  can hit the same race. Avoiding it needs a lingering close (RFC 9112 §9.6), which uvicorn
  doesn't do, so it is left for a later slice.
- **Files.** The §9 row names `app.py` and `test_http.py`. The proxy writes its own lines, so
  `proxy.py` changes too. The K-OAS2 hook of §7 lives in `conftest.py`, with its check in
  `harness.py`. `test_config.py`'s C8 follows `main()`'s new argument. `test_streaming.py`
  sends its refusals with `refused_post`.
- **L3 and the libraries' own loggers** (asked on #64, question 2, option B):
  - httpx logs every request's URL, query included, at `INFO`;
  - httpcore's `DEBUG` traces carry Ollama's response headers, `Set-Cookie` among them;
  - uvicorn's access line has the query.

  So `create_app` sets the `httpx` and `httpcore` loggers to `WARNING`, and `main()` passes
  `access_log=False` (§2.4). Nothing in the sidecar configures `logging`, so none of these
  lines was written as shipped; this keeps it that way if a handler is ever added. L3 checks
  stdout, stderr and every `logging` record at `DEBUG`. A contrast test turns httpx's logger
  back to `INFO` and finds the marker, so that check can't pass vacuously.
- **The traceback keeps exception messages** (E5 checks one). L3 passes because only an
  internal error's line has a traceback, and the proxy never builds an exception message from
  the body, the query or a header. The errors that may carry the URL (httpx's own) are mapped
  outcomes, and their lines have no traceback.
- **K-OAS2's hook.** An autouse fixture in `conftest.py`, for every test in `tests/node`,
  wraps `ErrorFactory.response`, `proxy.stream_error_record` and the health handler.
  - It collects each body made in process (the servers run in threads), and at teardown
    validates each against `ErrorEnvelope`, `StreamErrorRecord` or `Health`.
  - An NDJSON record must be one line, and an SSE record one `data:` event.
  - A body that fails fails its test.
  - A unit test shows that each schema refuses a broken body, so the hook isn't vacuous.
  - Bodies from the subprocess sidecar (`test_process.py`) aren't collected.
- **One table for L2, H19 and K-OAS2.** `test_every_path_…` drives 25 paths, each with one
  request carrying `X-Request-Id: s1-13`, on a sidecar logging at `DEBUG`. For each it checks:
  - exactly one line (a sentinel request's line is awaited after it) and its values;
  - Seriatim's three headers, once each;
  - that the hook collected exactly the body the client got.

  S11's shutdown runs in process, with `Task.cancel()`. F4 uses the pool close that raises.
- **HL8.**
  - **Connections.** The 200 connections are opened one at a time before the burst (macOS's
    listen backlog is 128), then each sends its request at once. The test raises
    `RLIMIT_NOFILE` on POSIX.
  - **Stalled.** `health_probe_timeout_s` is 1 s. "Within the timeout plus the bound" is timed
    in the sidecar, from the health handler's start to its response. From the client, the
    event loop's queue for 200 requests at once comes on top: about 0.1 s locally, and more on
    a slow runner. That isn't the probe's time, so the client-side check is looser: every
    answer within twice the timeout plus the bound. No probe waits for another's budget.
  - **Answering.** The 200 probes queue for the health client's one connection, and each
    probe's budget includes that wait (§3.4). So the case sets `health_probe_timeout_s=20`
    to show that load alone never causes an error. With a slow Ollama and the default 2 s, a
    queue longer than the budget gives `ollama_unreachable`, by design.
  - **The gated chat** is sent once the fake has seen a probe. In the stalled case it is
    answered before any probe gives up; in the answering case it is sent before the last
    health answer.

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
2. It sanitises `X-Request-Id`: the value is kept if the request has **exactly one**
   `X-Request-Id` header and its value matches `^[A-Za-z0-9._-]{1,128}$`. Otherwise (none,
   several, or an invalid value) it is replaced with a fresh `uuid4().hex` (H18).
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
  implicit close in `send()` on failure may already have closed the stream. It never stands
  in for the pool close: `aclose()` is still called, and only its return counts. (Since
  S1-10 the body is read from `resp.stream`, so the end of the body closes nothing; see
  "Details fixed by the code (S1-10)".) A loopback connect costs well under a millisecond.
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

**Details fixed by the code (S1-5).**
- **Order.** The HTTP-version check (§4, 505) runs right after step 2, before step 3, so a
  1.0 request is refused whatever its path.
- **What `create_app` takes.** Since S1-6 it takes both `client` and `gate`, as the signature
  above shows. An injected `client` becomes the shared ungated client, and the lifespan leaves
  closing it to the caller.
- **The health probe.** `health_probe_timeout_s` bounds the whole probe, as well as each
  phase of the health client, so a slowly dripping body can't stretch it. Any
  `httpx.TransportError` counts as `ollama_unreachable`: for example, a connection closed
  before a response, because Ollama has not answered. A body that isn't UTF-8 is
  `ollama_bad_response`.
- **`RequestLog.emit(level=…, **fields)`.** `level` defaults to `INFO`, and a line is written
  only at or above `log_level`. The line is `ts` (UTC, milliseconds, from an injectable wall
  clock), `level`, `event`, the fields as given, then `node`. Health logs `outcome: ok` or `degraded`. The 505, `BAD_PATH` and missing
  `raw_path` lines have no `path`, `kind`, `scope` or `known`, because the path wasn't
  checked or classified. So an unsafe path is never logged.
- **The harness.** The helpers are in `tests/node/harness.py`, and `conftest.py` exposes them
  as the `fake_ollama` and `sidecar` fixtures. Servers run in a thread, on a socket the
  harness binds first. That lets a test hold a port closed ("Ollama is down") and later
  start the fake on it (HL6).
- **HL6 uses a stub gate for the four-fault order.** A real `Gate` can't hold an overdue
  lease while it is `starting`: `try_acquire` returns `Starting` until the quarantine ends,
  and time only moves forward. So the full four-reason order is tested against a stub that
  reports each fault as a flag. A second HL6 case uses the real `Gate` and `Lease` for the
  three faults that can coexist, and a third checks `starting` on a new real gate.

**`main()`** runs these steps:
1. `load_settings()`. On `ConfigError`, print the message to stderr and exit 2.
2. `acquire_singleton(settings)`. On `SingletonError`, print the message and exit 3.
3. `uvicorn.run(create_app(settings), host=…, port=…, http="h11", workers=1,
   lifespan="on", log_config=None, access_log=False, server_header=False,
   date_header=True)`. `access_log=False` came with S1-13: uvicorn's access line has the
   query string (L3).

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

**Details fixed by the code (S1-12).**
- **`main() -> int`** returns the exit code (2, 3, or 0 once uvicorn has stopped) instead of
  calling `sys.exit`. `python -m seriatim_node` and the `seriatim-node` script both exit with
  it. Each message goes to stderr as one line, prefixed `seriatim-node: `.
- **The app uvicorn gets** is a plain 3-argument ASGI function that calls the dispatcher.
  uvicorn inspects the app's signature to tell an app from a factory, and under mutmut the
  dispatcher's methods take `(*args, **kwargs)`; the harness's servers do the same.
- **Helpers.** `lock_path(settings)` gives `lock_dir / f"upstream-{port}.lock"`. `_lock(fd)`
  picks `fcntl` or `msvcrt` by `sys.platform` when it is called, so both branches are tested
  on every OS with a stand-in module.
- **Held or cannot lock.** Only an error from the lock call itself can mean "held": on POSIX
  `BlockingIOError`, on Windows `EACCES`. Any other lock error, and any error from `mkdir` or
  `os.open` (a `PermissionError` included), is `cannot lock <path>: <strerror>`, where `<path>`
  is always the lock file's path, even when the directory is what failed.
- **A refused attempt closes its own descriptor**, which holds no lock. The file is left in
  place; only the winning descriptor is kept, in `_SINGLETON_FD`.

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
 8      Starting → 503 NODE_STARTING, Retry-After, X-Seriatim-Gate: refused; return  # body never read
 9      lease ← r
10  try:
10a     if lease: req_client ← new_gated_client()        # inside the try (E16); req_client is None from line 2
                                                     #   until this returns. An exception here is outside
                                                     #   _exchange's guard: line 16 catches it as Internal,
                                                     #   (b) frees the lease (nothing was sent) and (c)
                                                     #   sends 500 INTERNAL
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
40              except Exception: note client_write_failed        #   record or abort (§5); a field
                                                              #   on the line of 41 (S1-10, #60)
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
   - Gated route: `body = await read_body(receive, limit, body_timeout_s)`. The timeout
     bounds the **whole** read, from the first `receive()` to the last, and is not reset by
     each chunk (REQ-20: "has not fully arrived within"; E8).
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
     more_body=True)`, and `outcome.tail` keeps the last two bytes sent **of the whole
     stream**, `(tail + chunk)[-2:]`, so a record boundary split across chunks (even
     one-byte chunks) is still seen (S19).
   - When the iterator ends normally, it sets `outcome.upstream_done = True` and returns,
     but only if the upstream response was **framed**: it had a `content-length` that the
     received bytes matched; or it was chunked and ended with the terminating chunk; or it
     has no body by HTTP's rules, because the request was `HEAD` or the status is 1xx, 204 or
     304 (S21). The bodyless rule is checked **first**: a `HEAD` response's `Content-Length`
     describes the body a `GET` would have had, so it is never compared with the bytes
     received, and a 204 or 304 needs no length or chunking.
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
- `ollama_unreachable`: a connect error or any timeout, including the health client's pool
  timeout when many probes wait for its one connection (HL8). No probe error escapes as a
  500;
- `ollama_bad_response`: a status other than 200, a body that is not JSON or not an object,
  or a missing or empty `version`;
- `slot_overdue`;
- `slot_stuck`;
- `starting`.

The body never includes the Ollama version or any address. Probes are logged at `DEBUG` only.

## 4. Streaming passthrough and headers

**Bytes.**
- The proxy reads the raw bytes (`resp.stream`, the bytes `resp.aiter_raw()` would give,
  without its close at the end; S1-10), so nothing is decoded.
- Chunks are forwarded as they arrive, never collected.
- A gated request body is the exact bytes received. An ungated body is streamed as received.

**Request headers** (`upstream_headers`). Removed:
- the hop-by-hop headers: `connection`, every header named in `Connection`, `keep-alive`,
  `proxy-authenticate`, `proxy-authorization`, `te`, `trailer`, `transfer-encoding` and
  `upgrade`;
- every `proxy-*` header, by prefix (ADR-001 §5.3), so `proxy-connection` and any other
  `Proxy-` header never reach Ollama;
- `host`, `authorization`, `cookie`, `x-api-key`, every `x-seriatim-*`, `content-length` and
  `accept-encoding`;
- the forwarding headers `forwarded`, `x-forwarded-*` and `x-real-ip`.

Then:
- `content-length` is set from `body_len` when the body was buffered. For an ungated stream,
  the client's validated `content-length` is kept if it sent one; otherwise httpx sends the
  body chunked.
- `x-request-id` is set to the sanitised id, after every `x-request-id` the client sent has
  been removed.

**Why `accept-encoding` is removed.** Ollama then never compresses. The sidecar does not
decode, so this keeps in-stream error records safe to frame. It costs some bandwidth on
loopback only.

**Response headers** (`downstream_headers`). Removed:
- the hop-by-hop headers, the same list as for requests, including every header named in
  the **response's** own `Connection` (H15);
- every `access-control-*` header (Seriatim serves no CORS; T11);
- every `x-seriatim-*` header (T9);
- the compatibility headers `x-resolved-model`, `x-model-digest`, `x-execution-device`,
  `x-rerouted-from`, `x-reroute-reason` and `x-context-clamped`;
- `set-cookie`, `server` and `x-request-id`, so a conflicting id from Ollama never reaches the
  client (H18);
- `date`, so the response carries exactly one `Date`, uvicorn's (`date_header=True`, §2.4;
  RFC 9110 §6.6.1).

The prefix rule for `proxy-*` is for requests only: a response's `proxy-authenticate` goes
as a hop-by-hop header, and no other `Proxy-` response header is removed.

Every other header is kept, and a repeated header keeps all its values, in order (H17).
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
| `Transfer-Encoding` together with `Content-Length` | 400 `BAD_REQUEST_BODY` (`Connection: close`) | — | none taken | `rejected` |
| Unsafe raw path | 400 `BAD_PATH` | — | none taken | `rejected` |
| Unknown `/seriatim/*` path, or the wrong method on one | 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED` | — | none taken | `rejected` |
| Starting | 503 `NODE_STARTING` + `Retry-After` + `X-Seriatim-Gate: refused` | — | none taken | `starting` |
| No free slot | 429 `GPU_BUSY` + `Retry-After` + `X-Seriatim-Gate: busy` | — | none taken | `busy` |
| `Content-Length` > limit (buffered routes) | 413 `BODY_TOO_LARGE` (`Connection: close`) | — | none taken | `rejected` |
| Body grows past the limit while being read | 413 `BODY_TOO_LARGE` (`Connection: close`) | — | freed | `rejected` |
| Body stalls (`BodyTimeout`) | 408 `REQUEST_BODY_TIMEOUT` (`Connection: close`) | — | freed | `rejected` |
| Client gone while sending the body | nothing sent | — | freed | `client_disconnect` |
| Blob upload stalls or the client leaves after bytes were forwarded | 408 `UPLOAD_INTERRUPTED` (not replay-safe; `Connection: close`) for a stall; nothing sent to a client that left (S1-11) | — | no slot (ungated) | `upload_interrupted` |
| `ConnectError`, `ConnectTimeout` | 502 `UPSTREAM_UNAVAILABLE` (replay-safe) | — | freed | `upstream_unavailable` |
| `PoolTimeout` (ungated pool full; gated requests have their own connection) | 503 `NODE_SATURATED` + `Retry-After` (replay-safe) | — | no slot | `saturated` |
| `WriteError`, `ReadError`, `RemoteProtocolError`, other `httpx.TransportError` | 502 `UPSTREAM_PROTOCOL_ERROR` | record `UPSTREAM_STREAM_BROKEN` if framing allows, then abort | drains | `upstream_error` |
| Hold limit passed | 504 `LOCK_HOLD_EXCEEDED` | record `LOCK_HOLD_EXCEEDED` if framing allows, then abort | drains if sent upstream; freed if it passed while the body was still being read (E9) | `hold_exceeded` (`CRITICAL`) |
| Client disconnect | nothing sent | nothing sent | drains | `client_disconnect` |
| External cancellation (shutdown) | nothing sent | abort | drains | `cancelled` |
| Ollama non-2xx (including 429) | passed through unchanged | — | freed (clean end) | `ok` (with `status`) |
| Closing the upstream connection timed out or raised | as above | as above | **stuck** | adds `slot_stuck` (`CRITICAL`) |
| Any other `Exception` | 500 `INTERNAL` (generic message; the traceback is in the log only) | abort | drains if sent upstream, else freed | `internal_error` (`ERROR`) |

**"Freed" means released at once, with no drain** (SPEC REQ-10). That covers two cases: a
rejection before anything was sent to Ollama, and a clean end, where Ollama's response,
2xx or not, was read in full. Only "drains" starts a drain. For the node metrics (S4d), a request that took a
slot and then ends in a "freed" 400, 408 or 413, or with the client gone while sending the
body, counts once, as `rejected`, never also as `acquired`.

If the client has already gone, writing an error reply fails. That failure is caught and
noted on the request's own log line (`client_write_failed: true`; S1-10, ruled on #60),
and it never changes the slot result.

**Building error responses.** Every 4xx and 5xx produced here comes from
`ErrorFactory.response`, which fills in these fields:
- `source: "sidecar"` and `node: settings.id`;
- `retryable` and `replay_safe` from `ERROR_TABLE`, never from the call site;
- `retry_after`, with the matching `Retry-After` header, whenever it is given;
- `X-Seriatim-Gate`: `busy` only for `GPU_BUSY`; `refused` for `NODE_STARTING` (and, from
  later slices, the other refusals given before Ollama is contacted: `NODE_OFFLINE`, the
  exposure codes and `NUM_CTX_EXCEEDS_LIMIT`, SPEC REQ-37); `acquired` when a slot was taken;
  otherwise `ungated`.

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
- `LOCK_DIR=/run/seriatim`, on a tmpfs;
- `LISTEN_PORT=11436`, `ID=node-a` and `DRAIN_S=10` (S1-14, §8.6).

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
| C7 | `SERIATIM_NODE_CONFIG=/nonexistent.toml` → `ConfigError`; a malformed TOML file, including one that is not valid UTF-8 (TOML must be), → `ConfigError` naming `SERIATIM_NODE_CONFIG` | 23 |
| C8 | `main()` with invalid config exits 2 and prints the key. With valid config it calls `uvicorn.run` with `workers=1` and `http="h11"` (monkeypatched) | 21, 23 |
| C9 | Ranges and types C3 doesn't cover → `ConfigError` naming the variable. `LISTEN_PORT` = `0`, `65536`, `x`; `ID` = `Node`, `-a`, 33 characters, empty; `CONNECT_TIMEOUT_S` and `CLOSE_TIMEOUT_S` = `0`, `61`; `UNGATED_READ_TIMEOUT_S` = `0`, `3601`; `MAX_BODY_BYTES=1023`; `BODY_TIMEOUT_S` = `0`, `3601`; `HEALTH_PROBE_TIMEOUT_S` = `0`, `31`; `LOG_LEVEL` = `debug`, `TRACE`. Every float field also refuses `nan`, `inf` and `-inf`. The boundary values (`1`, `65535`, `60`, `3600`, `1024`, `30`) are accepted | 23 |
| C10 | `SERIATIM_NODE_CONFIG` names a file that exists but can't be read (POSIX, mode `000`; skipped on Windows and as root) → `ConfigError` naming `SERIATIM_NODE_CONFIG`. A `.env` file in the working directory with `SERIATIM_NODE_SLOTS=5` → ignored: `slots == 1` | 23 |
| C11 | `LISTEN_HOST` = `192.0.2.10`, `::`, `example.com` or `localhost`, without `ALLOW_UNAUTHENTICATED` → `ConfigError` naming it; `::1` and `127.0.0.2` are accepted | 15 |

### 8.2 `test_gate.py`

| id | test | REQ |
|---|---|---|
| K1 | Table-driven `classify`, with the expected (kind, scope, known) for every pair in §2.2 rules 2 to 4, including `POST /api/create` → gated, admin, known. Also gated + admin + `known=False`: `OPTIONS /api/tags`, `GET /api/chat`, `POST /api/chat/`, `POST /API/CHAT`, `post /api/chat`, `PUT /api/chat`, `POST /api/blobs`, `POST /api/blobs/sha256:XYZ`, `GET /api/unknown`, `POST /api/new-feature`. `SERIATIM` gives 200 for `GET /seriatim/health`, 405 for `POST /seriatim/health`, 404 for `GET /seriatim/nope` and `GET /seriatim` | 5 |
| K2 | `check_raw_path` raises for: `/api/chat/../show`, `/api/./chat`, `/api//chat`, `/api/%2e%2e/show`, `/api/%2E%2e/show`, `/api%2fchat`, `/api%2Fchat`, `/api%5cchat`, `/api\chat`, `/api/chat%00`, `/api/chat\x01`, `api/chat` and `//api/chat`. It accepts `/api/blobs/sha256:<hex>` and `/api/show%20x`. Hypothesis: any path containing a `.`/`..` segment or a listed escape raises | 20a |
| K3 | The pairs in `UNGATED_INFER`, `UNGATED_ADMIN` and `GATED_INFER` equal `tests/fixtures/route-pairs.json`, and equal the contract's paths. Every pair is marked `exists: true` in the route probe in `tests/fixtures/ollama-0.40.16.json` (see Appendix B for what "exists" means) | 5 |
| K4 | Every forbidden byte, not one sample: `/api/chat` followed by each of `0x00`–`0x1f` and `0x7f` (33 cases) → `PathError`. Allowed escapes pass unchanged and are classified by their raw bytes: `/api/chat%41`, `/api/chat%252e`, `/api/%252e%252e/show` and `/api/chat%20` pass `check_raw_path` and are gated, admin, `known=False` (never `/api/chat`) | 20a, 5 |
| K5 | Blob digests. `HEAD` and `POST` on `/api/blobs/sha256:` followed by 63 hex digits, 65 hex digits, 64 upper-case hex digits, or 64 hex digits and `/` → gated, admin, `known=False`; 64 hex digits and `\n` → `PathError`. Exactly 64 lower-case hex digits → ungated, admin | 5, 20a |
| K6 | Look-alikes of Seriatim's paths are not Seriatim's: `/seriatimx`, `/seriatim-health` and `/Seriatim/health` → gated, admin, `known=False` (REQ-5 rule 5: an unknown path is forwarded, gated), never `SERIATIM` | 5 |
| G1 | After the quarantine (fake clock advanced by `drain_s`), `try_acquire` returns a `Lease`; the slot is `HELD` | 2 |
| G2 | slots 1, one lease held → `Busy(retry_after_s=15, busy_for_s≥0)` | 2, 3 |
| G3 | slots 2: two leases, the third is `Busy` | 2 |
| G4 | `finish(clean=True)` → `FREE` at once. `finish(clean=False)` → `DRAINING`: `try_acquire` returns `Busy` with `retry_after_s ≥ ceil(remaining drain)` until the clock passes `drain_s`, then a `Lease` | 10 |
| G5 | `finish` twice: first `True`, second `False`, and the slot is not freed twice. With slots 1, an old lease finished again after a new lease is taken does not free the new one. `stuck()` then `finish()` → still `STUCK` | 10, 11 |
| G6 | `slots()` shows the state, holder and remaining drain. `overdue()` lists a holder only once `max_hold_s + grace` has passed (injected clock). `any_stuck()` is true after `stuck()` | 17, 18 |
| G7 | 1000 tasks call `try_acquire` concurrently with `asyncio.gather` (slots 1, after quarantine) → exactly one `Lease` | 2 |
| G8 | A new `Gate` returns `Starting(retry_after_s=ceil(drain_s))` until `drain_s` passes; `starting()` is true until then | 12a |
| G9 | Hypothesis: random sequences of acquire, finish(clean), finish(drain), stuck and clock steps (slots 1–4) keep `held + draining + stuck ≤ slots`, and never return a `Lease` while no slot is `FREE` | 2, 10, 11 |
| G10 | `Gate(slots=0)` and `Gate(drain_s=0)` raise `ValueError`, as do `retry_after_s < 1`, `max_hold_s ≤ 0`, and NaN for any of the four | 2, 10 |
| G11 | Monotonic time only. With an injected monotonic clock and a separate wall clock, moving the wall clock by ±1 day changes nothing in `try_acquire`, the drain or `overdue()`. The overdue boundary is strict: a holder at exactly `max_hold_s + grace_s` is not overdue; one at that plus 1 ms is | 10, 18 |
| G12 | slots 3: one `HELD`, two `DRAINING` with 4 s and 9 s left, `retry_after_s=1` → `Busy(retry_after_s=4)`, the **earliest** drain. Every slot `DRAINING` → `busy_for_s == 0` | 2, 3 |

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
| H8 | One raw keep-alive connection: two `/api/chat` requests, the second written to the socket immediately after the first response's last byte arrives, while a test hook holds the first request's `finally` at step (c) → the second is admitted (200), never 429. **H8b:** the same, but the first ends abnormally (upstream reset) → the next request within `drain_s` gets 429, and after `drain_s` (fake clock) it gets 200. The abort closes the connection (§2.3), so H8b asserts the cut-short response and the close on the raw socket, then sends the 429 and 200 requests on new connections | 2, 10 |
| H9 | Upstream answers 429 with `Retry-After: 3` and its own `X-Seriatim-Gate: busy`, `Access-Control-Allow-Origin: *` and `X-Rerouted-From: x` → the client gets 429 with Ollama's body, `Retry-After: 3`, `x-seriatim-gate: acquired`, and no CORS or `X-Rerouted-From` header | 6, 36 |
| H10 | A 100 MiB streamed `POST /api/blobs/sha256:<hex>` with `max_body_bytes=1 MiB` → forwarded intact (the SHA-256 of the received bytes matches). Not buffered: the in-process sidecar's `tracemalloc` peak during the upload stays under 16 MiB | 6, 20 |
| H11 | Upstream answers request A with `Set-Cookie: s=1` → the client does not get the header, and a later request B (another client) reaches upstream with no `Cookie` header | 6, T3 |
| H13 | A close-delimited upstream response (`raw_upstream`: no `content-length`, not chunked, then close) → treated as broken: the client's response is aborted, and the slot drains. A `Content-Length: 100` response cut at 50 bytes → aborted, and the slot drains. The client sends `X-Forwarded-For` and `Forwarded` → upstream receives neither | 6, 24 |
| H14 | With the ungated pool full (16 held `/api/show` requests on a stalled fake), the 17th `/api/show` → 503 `NODE_SATURATED`; a gated `/api/chat` and the health probe still succeed | 20, 18 |
| H12 | A raw-socket `HTTP/1.0` request to `/api/chat`, and one to `/api/tags` → 505 `HTTP_VERSION_UNSUPPORTED`; upstream received nothing; no slot taken | 21, 24 |
| H15 | `Connection` with several mixed-case tokens, and in both directions: the client sends `Connection: Keep-Alive, X-A ,x-b` (and a second `Connection: X-C`) with `X-A`, `X-B` and `X-C` → upstream gets none of them. Upstream answers with `Connection: X-D, x-e`, `X-D` and `X-E` → the client gets none of them | 6 |
| H16 | The full strip lists (§4, ADR-001 §5.3), table-driven. Request side: `Proxy-Authorization`, `TE`, `Trailer`, `Upgrade`, `X-Real-IP`, `X-Forwarded-Host`, `X-Forwarded-Proto`, two `Authorization` headers, `AUTHORIZATION`, `x-API-key` and `X-SERIATIM-GATE` → none reaches upstream. Response side: `Proxy-Authenticate`, `Server`, `X-Resolved-Model`, `X-Model-Digest`, `X-Execution-Device`, `X-Rerouted-From`, `X-Reroute-Reason`, `X-Context-Clamped`, `X-Seriatim-Node` and `X-Request-Id` → none reaches the client from Ollama; Seriatim's own `X-Seriatim-*` and `X-Request-Id` appear exactly once | 6, 13 |
| H17 | Repeated end-to-end headers survive: the client sends `X-Custom: a` and `X-Custom: b`; upstream answers with two `Link` headers → each side receives both values, in order | 6 |
| H18 | `X-Request-Id`. A valid `abc-1.2_3` is echoed, forwarded and logged. Empty, 129 characters, `a b` and `a/b` are each replaced with a fresh 32-hex id, which is echoed, forwarded and logged instead. Two `X-Request-Id` headers → replaced. Values with CR or LF can't be sent over HTTP, so the sanitiser is also unit-tested with `a\r\nb` → replaced. Upstream answers with `X-Request-Id: upstream` → the client sees only the sidecar's id | 22, 6 |
| H19 | Every response class carries exactly one `X-Seriatim-Node` (equal to `settings.id`), one `X-Request-Id` and the right `X-Seriatim-Gate` (§5): 200 gated (`acquired`); 200 ungated, health 200 and 503, 400 `BAD_PATH`, 404, 405, 413 before admission and 505 (`ungated`); 429 (`busy`); 503 `NODE_STARTING` (`refused`); 413 or 408 after a slot was taken, 502, 504 and 500 after a slot was taken (`acquired`); 503 `NODE_SATURATED` and 500 on an ungated route (`ungated`) | 25 |
| H20 | `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY=` are set in `os.environ`, pointing at a recording fake proxy → gated, ungated and health requests all reach `fake_ollama`, and the proxy records nothing | 1 |
| H21 | Upstream answers `302` with `Location: http://192.0.2.1/x` → the client gets the 302 with `Location` unchanged; `fake_ollama` recorded one request, and nothing was sent to the `Location` | 6 |
| H22 | Seriatim's own paths over real HTTP: `GET /seriatim/nope` and `GET /seriatim` → 404 `NOT_FOUND`; `POST /seriatim/health` → 405 `METHOD_NOT_ALLOWED` with `Allow: GET`. Each body validates against `ErrorEnvelope` and carries `X-Seriatim-Gate: ungated`; `fake_ollama` recorded nothing, and no slot was taken | 5 |
| H23 | The ASGI app is called directly with a scope that has no `raw_path` → 500 `INTERNAL`; `fake_ollama` recorded nothing; every slot is still `FREE` | 26 |
| H24 | In S1 the body is opaque bytes (S3's inspection does not exist yet): `{"a":`, `{"model":"a","model":"b"}`, the invalid UTF-8 `\xff\xfe`, and 1 KiB of random bytes sent to `/api/chat` → `fake_ollama` records identical bytes each time, and the sidecar adds no error of its own | 6, 7 |
| H25 | Clean upstream errors pass through: 500 `{"error":"x"}`, 503 `text/plain` `oops` and 502 `text/html` → status, `content-type` and bytes unchanged; the slot is freed at once (the next request gets 200 with the fake clock not advanced) | 6, 25 |
| H26 | Ungated stalls. `ungated_read_timeout_s=0.5`: `/api/show` with the fake stalling before the headers → 502 `UPSTREAM_PROTOCOL_ERROR` within the bound; stalling after the headers → aborted. A client disconnect during an ungated stream → the fake sees the upstream close. In every case the shared pool has no connection in use afterwards, and every gate slot stays `FREE` throughout. When the shared response's `aclose()` blocks → `upstream_close_failed` at `ERROR` within `close_timeout_s`, and the client is answered or aborted within `2 × close_timeout_s` | 20, 26 |
| H27 | The `NODE_SATURATED` response from H14 in full: `Retry-After` equals `retry_after`, `X-Seriatim-Gate: ungated`, and `retryable` and `replay_safe` are the `ERROR_TABLE` values (`replay_safe: true`); the body validates against `ErrorEnvelope` | 20, 25 |
| H28 | Request smuggling, over a raw socket: two different `Content-Length` headers; `Content-Length: 5, 6`; `Content-Length: -1`; `Transfer-Encoding: chunked` together with `Content-Length`; `Transfer-Encoding: gzip, chunked`; and a chunked body with a trailer and a second request hidden in it → the invalid framings (two different `Content-Length` values, a list value, `-1`, and `Transfer-Encoding` together with `Content-Length`; RFC 9112 §6.1 and §6.3) are each **refused with 400 and a closed connection**. h11 refuses the first three; it accepts `Transfer-Encoding` with `Content-Length`, so the dispatcher refuses that one with 400 `BAD_REQUEST_BODY` and `Connection: close` (§2.3, S1-8), on Ollama paths and `/seriatim/*` alike. `Transfer-Encoding: gzip, chunked` is validly framed, but the sidecar supports no transfer coding except `chunked`. h11 refuses it, and uvicorn 0.54 (h11) answers every h11 protocol error with **400 and a closed connection**, so that is its exact result too. RFC 9112 §6.1 says a server SHOULD send 501 here, and 400 is still a refusal; the test asserts 400, so a uvicorn upgrade that changes it is caught. The valid chunked body with a trailer is parsed as exactly one request, with the body its framing gives, and the hidden request is never seen as a request. `fake_ollama` never records a request the client didn't frame, and no slot is taken by one | 21, 2 |
| H29 | A refused request, then another on the same keep-alive socket: request 1 gets 429 (busy) after sending its 4 KiB body; request 2 follows on the same socket → request 2 gets **either** its own real response (200 once the holder ends) **or** a closed connection with no second response. A 400 (from request 1's body bytes being parsed as a request) fails the test. The same after a 413. And with the body still owed: part of a body, then a 408, a 413 (still streaming) or the hold limit's 504; and an `Expect: 100-continue` request refused with 413, 429 or 503 whose body is never sent. Each of those replies carries `Connection: close`, and the next request on the socket is answered correctly or finds it closed, never taken as body bytes. A 429 to a request with no body keeps the connection (§2.3, S1-8) | 3, 21 |
| B1 | Request 1 is held at upstream; request 2 (`/api/chat`) → 429 within the bound, with `Retry-After: 15` and `X-Seriatim-Gate: busy`. The body validates against `ErrorEnvelope`: `error_code: GPU_BUSY`, `retryable: true`, `replay_safe: true`, `retry_after: 15`, `source: sidecar`, `busy_for_s ≥ 0`. `fake_ollama` recorded **one** request | 3, 4, 25 |
| B2 | Request 1 ends cleanly → a new request 2 gets 200 at once | 10 |
| B3 | Raw socket: request 1 is held; request 2 sends only the headers, with `Content-Length: 10485760` → the 429 arrives without any body being sent | 3 |
| B4 | slots 2: two held requests proceed and the third gets 429; after one finishes cleanly, a fourth gets 200 | 2 |
| B5 | During startup quarantine: gated → 503 `NODE_STARTING` with `Retry-After ≤ drain_s` and `X-Seriatim-Gate: refused`, and the request body is never read; `GET /api/tags` → 200; health → 503 with `starting` | 12a, 18 |
| B6 | Raw socket, `Expect: 100-continue` with `Content-Length: 10485760`: while busy → 429, and while starting → 503 `NODE_STARTING`. In both, no `100 Continue` is sent first, so the client never sends its body | 3, 12a |
| B7 | The size check comes before admission: an honest `Content-Length` over `max_body_bytes` while every slot is `HELD` → 413, not 429; during the startup quarantine → 413, not 503 | 20, 3 |
| E1 | `ollama_url` points at a closed port → 502 `UPSTREAM_UNAVAILABLE` with `replay_safe: true`; the next request also gets 502, not 429, because the slot was freed: nothing was sent | 26, 10 |
| E2 | An unsafe raw path sent over a raw socket (`POST /api/chat/../show`) → 400 `BAD_PATH`; upstream received nothing | 20a |
| E3 | `max_body_bytes=2048`: a 4096-byte body with an honest `Content-Length` → 413, and no slot was taken. The same body chunked, without a length → 413, and the slot was freed. Upstream received nothing | 20 |
| E4 | `body_timeout_s=0.5`: a raw socket sends half the body and stalls → 408 `REQUEST_BODY_TIMEOUT`; the slot was freed | 20 |
| E6 | A blob upload whose client stalls after 1 MiB has been forwarded → 408 `UPLOAD_INTERRUPTED` with `replay_safe: false`; the upstream connection is closed. A stall before the first chunk → 408 `REQUEST_BODY_TIMEOUT` with `replay_safe: true` | 20, 25 |
| E5 | An unexpected exception inside the proxy (monkeypatch `downstream_headers` to raise) → 500 `INTERNAL` with a generic message; the traceback is in the log and not in the body; the slot drains (the request reached upstream) | 26 |
| E7 | The client sends half of a gated body and closes → `fake_ollama` recorded nothing; the slot is `FREE` with no drain; log `outcome: client_disconnect`, `slot_end: free` | 10, 20 |
| E8 | `body_timeout_s=1`: a raw socket sends one byte every 0.3 s → 408 `REQUEST_BODY_TIMEOUT` after about 1 s in total (the timeout is not reset by each chunk); the slot is freed | 20 |
| E9 | `max_hold_s=1`, `body_timeout_s=30`: a gated body trickles in past 1 s → 504 `LOCK_HOLD_EXCEEDED`; `fake_ollama` recorded nothing; the slot is freed with no drain | 12, 10 |
| E10 | `max_body_bytes=2048`: an empty body and a 2048-byte body are forwarded (200); 2049 bytes → 413, with an honest `Content-Length` and chunked | 20 |
| E11 | Ungated buffered routes enforce the limits too: `POST /api/show`, `POST /api/pull` and `DELETE /api/delete` with a body over `max_body_bytes` → 413, and with a stalled body → 408 `REQUEST_BODY_TIMEOUT`; `fake_ollama` recorded nothing | 20 |
| E12 | Blob upload disconnects. The client leaves before the first chunk → `fake_ollama` recorded no request; log `client_disconnect`. The client leaves after the first chunk was forwarded → the fake sees the upstream connection close; log `upload_interrupted` | 20, 25 |
| E13 | A chunked blob upload with no `Content-Length`: 3 MiB in uneven chunks → the SHA-256 of the received bytes matches. An empty chunked upload → forwarded as an empty body, and the upstream status is passed through | 6, 20 |
| E14 | One owner of `receive`: a wrapped `receive` fails the test if it is entered while another call is pending. A blob upload with a disconnect before, during and after the body, and a gated request with a disconnect while waiting for headers → no concurrent `receive()` calls | 10, 20 |
| E15 | Internal errors before and after the response starts. `upstream_headers` is monkeypatched to raise on a gated request → 500 `INTERNAL`; `fake_ollama` recorded nothing; the slot is freed with no drain. The relay is made to raise after `http.response.start` → aborted; a wrapped `send` sees no second `http.response.start`; the slot drains | 26, 10 |
| E16 | `new_gated_client` is monkeypatched to raise right after the slot is taken → 500 `INTERNAL`; the slot is `FREE` (the next request is admitted); the lease had exactly one effective end | 2, 11 |
| HL1 | Upstream `/api/version` 200 `{"version":"0.40.16"}` → health 200 `{"status":"ok"}`, no other keys | 18 |
| HL2 | Upstream down → 503 with `["ollama_unreachable"]` | 18 |
| HL3 | Upstream `/api/version` returns each of: 500; 200 `not json`; 200 `[]`; 200 `{}`; 200 `{"version":""}`; 200 `{"version":7}` → each gives 503 with `["ollama_bad_response"]` | 18 |
| HL4 | An injected overdue lease → `slot_overdue`; an injected stuck slot → `slot_stuck`. The body never contains the version string | 18 |
| HL5 | The probe connects, then stalls: once before the headers and once in the middle of the body → 503 `["ollama_unreachable"]` within `health_probe_timeout_s` plus the bound | 18 |
| HL6 | Several faults at once: upstream down, an overdue lease, a stuck slot and the startup quarantine → `reasons` is exactly `["ollama_unreachable", "slot_overdue", "slot_stuck", "starting"]`. Clearing each fault in turn removes only its reason, and the rest stay in order. A real gate can't be overdue while starting, so this order uses a stub gate. With the real `Gate` (2 slots: one lease overdue, one `stuck()`) and upstream down → `["ollama_unreachable", "slot_overdue", "slot_stuck"]`. Starting Ollama removes only `ollama_unreachable`, and finishing the overdue lease removes only `slot_overdue`. `slot_stuck` stays, because a `STUCK` slot is never freed before a restart. A new real gate reports `["starting"]` | 18 |
| HL7 | Normal use is not a fault: slots 2, one `HELD` within its limit and one `DRAINING` → 200 `{"status":"ok"}` | 18 |
| HL8 | Two cases. **Stalled:** 200 concurrent health requests while the fake stalls `/api/version` past `health_probe_timeout_s` → **every** response is 503 `["ollama_unreachable"]` within `health_probe_timeout_s` plus the bound; none is 200 and none is 500. **Not stalled:** 200 concurrent health requests on a fake that answers → every response is 200. In both cases, a gated `/api/chat` sent meanwhile gets 200 | 18 |
| HL9 | Health requests are logged at `DEBUG` only: at `LOG_LEVEL=INFO`, 10 health requests add no log line; at `DEBUG`, each adds exactly one | 22 |
| L1 | After H1, B1, E1 and S4: one JSON log line each, with the fields in Appendix A. No line contains the body marker string, the `Authorization` value or the `X-API-Key` value | 22, 13 |
| L2 | Exactly one log line per request, with the `status`, `outcome`, `bytes_in`, `bytes_out` and `slot_end` of §5, on every path: 400, 404, 405, 413 before and after admission, 408, 429, 503 `NODE_STARTING`, a disconnect while sending the body (E7), `upload_interrupted` (E12), `hold_exceeded` (S7), a stuck close (F4) and a shutdown (S11) | 22 |
| L3 | Nothing secret reaches the log: the request has `Cookie: c=<marker>` and the query `?key=<marker>&token=<marker>`; the response has `Set-Cookie: s=<marker>`; and an internal error (E15) is raised while the body holds the marker; and an upstream error on a request with `?key=<marker>` (E1's closed port), since an exception message can carry the URL → no log line contains the marker, **traceback included**. The query string is never logged (`path` is the path only) | 22, 13 |
| K-OAS1 | `contract/openapi.yaml` passes `openapi-spec-validator` (OpenAPI 3.1), and every `$ref` resolves (checked separately, because the validator accepts an unresolved one). Every Ollama path's `x-seriatim-gate` and `x-seriatim-scope` agree with `classify()` (Seriatim's own `/seriatim/*` and `/models/select` paths are not Ollama routes and have no `x-seriatim-gate`, but each must declare `x-seriatim-scope`: `none`, `infer` or `admin`, and the ones S1 serves must agree with `classify()`); `x-seriatim-unknown-endpoint` agrees with rule 5 | 5, G7 |
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
| S11 | External cancellation (the server shutting down, simulated by cancelling the request's task) in each phase: the body read, the upstream write, the wait for headers, and streaming → the shielded `finally` runs: the per-request client is closed (the fake sees it), the slot is `DRAINING` if anything was sent and `FREE` otherwise, the log says `cancelled`, and the cancellation propagates (the task ends cancelled) | 10, 11 |
| S12 | A second cancellation arrives while step (a) waits for a slow `aclose()` (it returns after 0.5 s, below `close_timeout_s`) → `aclose()` still completes, and the slot is not freed before it returns; the lease has exactly one end | 11 |
| S13 | While step (a) waits for a slow `aclose()` (0.5 s), another gated request → 429; once `aclose()` returns, the slot follows §3.3 (b) | 11, 2 |
| S14 | A slow reader: the client reads nothing while upstream streams 32 MiB, `max_hold_s=1` → the fake's writes block (upstream reads pause); after about 1 s the upstream connection is closed and the slot drains; log `hold_exceeded` | 12, 10 |
| S15 | Upstream ends cleanly; the final `send` (`more_body=False`) is wrapped to hang → the slot is already `FREE` when it is called, and stays `FREE`. After `close_timeout_s` the write is abandoned, and the request's one log line has `client_write_failed: true` (ruled on #60; see S1-10's details in §2.3), `outcome: ok` and `slot_end: free`. The same when the final `send` raises | 10 |
| S16 | A disconnect scheduled at the moment upstream completes (the fake's last chunk and the client's close together), 50 runs → in each run the lease has exactly one effective end (`finish` or `stuck` returns `True` once), and there is one log line | 10 |
| S17 | A positive SSE record: `content-type: text/event-stream; charset=utf-8`; upstream sends two complete events, then resets → the client gets the two events, then exactly one `data: {"error":…,"error_code":"UPSTREAM_STREAM_BROKEN"}` event, then an abort | 24 |
| S18 | Upstream sends the headers (NDJSON, chunked), then resets before any body → the client gets the headers, one record line, then an abort (an empty tail is a record boundary) | 24 |
| S19 | Boundaries split across chunks, then a reset. NDJSON `{"a"` + `:1}` + `\n` → a record. SSE `data: x\n` + `\n` → a record. SSE `data: x\n\n` one byte per chunk → a record. NDJSON ending `{"a":1}` + `\n{"b"` → no record, only an abort | 24 |
| S20 | Broken chunked framing from `raw_upstream`: a malformed chunk-size line; and a response that closes without the terminating `0\r\n\r\n` → never a clean end: aborted, the slot drains, log `upstream_error` | 24, 10 |
| S21 | Bodyless responses are complete: a clean `HEAD /` whose response has a **non-zero** `Content-Length` (`1234`) and no body; and a gated request that gets 204, or 304, each sent with **no** `Content-Length` and not chunked → the client sees a complete response, and the gated slot is freed with no drain. (Revision 7's rule fails each case: the `HEAD` length doesn't match the bytes received, and the 204 and 304 look close-delimited) | 6, 10 |
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
| R6 | Four sidecars started at once with the same lock directory and upstream port, two with `OLLAMA_URL` on `127.0.0.1` and two on `127.0.0.2` → exactly one serves, and three exit 3 | 12b |
| R7 | Permission failures (POSIX, not root; Windows is covered by R4): a lock directory with mode `0500`, and a lock file with mode `000` → exit 3 with `cannot lock` | 12b |
| R8 | The lock file is never removed: three graceful stops (`Popen.terminate()`) and restarts → after each exit the file exists with the same inode (`os.stat().st_ino`) | 12b |
| R9 | Two sidecars with the same lock directory and different upstream ports → both serve | 12b |

**Details fixed by the code (S1-12).**
- **The `gc.collect()` trigger (R3)** is a line on the sidecar's stdin, not a signal, so R3 runs
  the same on Windows. The harness starts that sidecar through a small wrapper (`GC_HOOK` in
  `harness.py`) that watches stdin in a thread, collects, writes an acknowledgement to stderr
  and then calls the package's normal entry point. The sidecar's own code has no hook.
- **`sidecar_process`** picks a free listen port (`closed_port()`), removes every
  `SERIATIM_NODE_*` from the inherited environment, sets test mode
  (`ALLOW_UNAUTHENTICATED=true`, so the temporary `LOCK_DIR` is accepted) and writes stdout and
  stderr to files, so a full pipe can never block the child. Waiting for the port or for an
  exit allows 30 s (`PROCESS_S`), because a cold interpreter start on a CI runner is not an
  event from a fake. R3's "within 5 s" is asserted with `WAIT_S`.
- **"Answers `NODE_STARTING` until `drain_s` has passed" (R1, R2)** is checked with
  `drain_s=2`: the first gated request is 503 `NODE_STARTING`, every one after it is too until
  one is admitted, and that one arrives at least `drain_s` after the spawn and at most
  `drain_s` plus the poll interval and `TIMING_BOUND_S` after the port first accepted.
- **R2's "same port"** is the same listen port, upstream and lock directory.
- **R8** does not assert an exit code: after a graceful stop uvicorn re-raises the `SIGTERM`
  it caught. On Windows `Popen.terminate()` is `TerminateProcess`, so R8 there checks the file
  after a hard stop.
- **The S0a placeholder's tests** go: `test_package.py` now checks that
  `python -m seriatim_node` reaches `main()` (an invalid `SLOTS` exits 2), and simulates the
  missing module for `__main__`'s own fallback.

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

**Details fixed by the code (S1-14).** S1-14 adds the sidecar container to the PPE, the P
tests and `Enforced: yes`.
- **Asked on #69** (https://github.com/thurlow-research/seriatim/issues/69#issuecomment-7071983385):
  question 1, how node-a gets its code, and question 2, P4's model. Option A is built for
  both, as below.
- **node-a** (`ppe/compose.yaml`) runs a digest-pinned `python:3.12.16-slim-trixie`
  (Dependabot's docker-compose ecosystem bumps it with the others) in `ollama-a`'s network
  namespace, with `cap_drop: [ALL]`, the §6 settings and its code mounted read-only from
  `ppe/data/node` (gitignored). No image is built, so the guard needs no `build` key and its
  allowlist is unchanged (#28).
  - **Staging** (#69, question 1, option A). Before `ppe/ppe up --ci`, the `ppe` job installs
    the runtime dependencies there from `uv.lock`'s hashes (`uv export --frozen`, then `uv pip
    install --target … --require-hashes --only-binary :all:` as cp312 manylinux wheels), and
    copies in `node/seriatim_node`. A local pre-check run on the designated host stages the
    same way first.
  - **Ports.** The sidecar listens on 11436, published as `127.0.0.1:11436` on `ollama-a`,
    because `dbg-a` already has 11434 in the namespace. 11434 stays plain Ollama, so S0b's
    checks and `capture_fixtures.py` are unchanged. The tests get `SERIATIM_BASE_URL` (the
    sidecar) and `SERIATIM_PPE_URL` (plain Ollama) from the job.
  - **Limits.** cap-a drops to 0.25 CPU / 256 MiB, and node-a gets 0.5 CPU / 384 MiB. The
    sum is 4 CPUs and 5.75 GiB, inside the guard's bound.
  - `tests/tools/test_ppe_guard.py` checks the rendered node-a (namespace, no capability,
    test mode, the tmpfs, the read-only mount and the two loopback ports).
- **The capture.** `test_sidecar.py` reads cap-a's pcap (`ppe/data/capture/ollama-a.pcap`)
  with the standard library. It reassembles each client-to-Ollama connection by sequence
  number, splitting on each SYN (in case a port is reused) and dropping retransmissions, then
  splits it into HTTP requests by `Content-Length` or chunked framing. Each request is
  matched by the `X-Request-Id` the test sent, which the sidecar keeps and forwards (§4), and
  timed by its first packet. Host and container share the kernel clock, so a trial's window
  can be compared with the capture's timestamps.
- **P1** sends a body spaced, ordered and escaped as no JSON encoder would write it
  (`"temperature": 0.30`, `\u00e9`), so a re-serialised body can't match. The captured body
  must equal it byte for byte, with the same method, path and `Content-Length`. `/api/ps` is
  read directly from Ollama.
- **P2.** Request 1 asks for `num_predict` 128, and is read to its end, so each trial ends
  cleanly and frees the slot without a drain. Request 2's latency is measured from its send to
  its complete 429, and the p99 over 20 trials is the nearest rank, here the slowest one. "Exactly one
  inference request per trial" means that, in each trial's window, the capture has exactly
  one `POST` to a gated `infer` route (§2.2 rule 4), and that it is request 1. Request 2's id
  is nowhere in the capture.
- **P4 holds the slot without a model** (the slot is `HELD`, §2.2). A raw socket sends the
  headers of a gated `/api/chat` and half its body. The gate admits on the headers (§3.3
  line 6), then waits for the body, within `body_timeout_s` (30 s). A gated probe getting 429
  shows the slot is held, before the pull and again after it. The rest of the body then
  completes that request with 200.
  - **The model** (#69, question 2, option A). `all-minilm:22m` is deleted through the
    sidecar and pulled again through it, so `/api/tags` listing it proves the pull installed
    it. The progress stream must end with `{"status":"success"}` and contain no `error`.
- **P5** compares the status, the body bytes and the headers, without `Date` and Seriatim's
  three headers (case-insensitive, in any order). It also checks that those three headers
  are there.
- **P6.** The baseline is the median of three idle probes taken directly, after one probe that
  loads the model. The probe and the cancelled generation use the same model and context, so
  Ollama doesn't reload between them. The client disconnects after three records. The result
  goes into the job summary (`GITHUB_STEP_SUMMARY`) and the log.
- **P7.** While node-a is down, the host's port forwarder still accepts on 11436 and then
  drops the connection, so "the moment it accepts" means the first gated request that gets
  an HTTP answer. That answer must be 503 `NODE_STARTING` with `X-Seriatim-Gate: refused`.
  Every answer after it is also `NODE_STARTING`, until a 200 arrives. The 200 comes no
  sooner than the first `Retry-After` minus 1 s (it is the quarantine left, rounded up), and
  at most 5 s plus two poll intervals after that.
- **Between tests**, a gated probe to an unlisted path (`GET /api/seriatim-ppe-gate-probe`,
  gated by §2.2 rule 5, and answered 404 by Ollama without a model) is polled until it's
  admitted. That waits out any drain or quarantine an earlier test left. `drain_s` is read
  from `ppe/compose.yaml`, so the bounds follow the PPE's own setting.

### 8.7 Quality gates for this slice (CI output quoted in the PR)

- **Coverage.** `pytest --cov=seriatim_node --cov-branch --cov-fail-under=80`, plus a
  separate check of the branch percentage alone (from `coverage json`, `totals.percent_branches_covered`
  ≥ 80), with the output.
- **Mutation.** `mutmut run` on the changed files (`config.py`, `gate.py`, `proxy.py` and
  `app.py`), with a score of at least 75 % (SPEC REQ-61). The PR lists each survivor, with one
  line on why it is equivalent or which test was added.
- **PPE.** The `ppe` job's P1 to P7 output, including the P6 latency.
- **Size.** Each PR in the §9 series passes the PR-size check on its own. Together they
  touch the 13 files of §1.
- **Per PR.** Each PR in §9 runs these gates on the files it changes: coverage, mutation on
  the modules it adds or changes, and the PPE once S1-14 adds the sidecar container.
- **Review.** An independent review of the PR diff. Every real finding is fixed and listed,
  and every rejected one is given with its reason.

## 9. Delivery: the S1 PR series

The code is too large for one PR under the size limit, so it ships as 14 small PRs, in this
order. Each has its own issue, a sub-issue of #34. Each leaves `main` releasable and green,
and ships the tests for exactly the IDs listed. **Every behaviour an ID asserts is delivered
by its PR or by one in that PR's "needs"** (checked row by row). A PR may add a stub that a
later PR completes, but never one that a test depends on. Modules grow across PRs: for
example, `app.py` starts in S1-1 with the error table. Once `app.py` exists,
`python -m seriatim_node` and the `seriatim-node` script call its `main()`. So until S1-12,
`main()` is a placeholder that keeps the S0a behaviour: it prints S0a's message and exits 2.
No TD ID depends on that placeholder.

The proxy's request lifecycle (§3.3: admission, the body read, the watcher, the relay, the
hold limit, and the shielded `finally` with its close proof, clean end, drain and `STUCK`)
is one PR, S1-6, because every gated test depends on all of it. The later proxy PRs add
behaviour on top of it: header rules, request limits, in-stream records, the edge cases of
cancellation and closing, and blob streaming.

| PR | scope | files (new or changed) | TD IDs | needs |
|---|---|---|---|---|
| S1-1 | Contract and the error table | `contract/openapi.yaml`; `app.py` (`ErrorCode`, `ERROR_TABLE`, `ErrorFactory`) | K-OAS3 | — |
| S1-2 | Configuration | `config.py`, `test_config.py` | C1–C7, C9–C11 | — |
| S1-3 | Path safety and classification | `gate.py` (`check_raw_path`, `classify`, the tables), `test_gate.py`, `test_http.py` (K-OAS1) | K1–K6, K-OAS1 | 1 |
| S1-4 | The gate | `gate.py` (`Gate`, `Lease`), `test_gate.py` | G1–G12 | — |
| S1-5 | Harness, dispatcher and health | `conftest.py`, `app.py` (`create_app`, the dispatcher, `X-Request-Id`, 505, `/seriatim/*`, health, `RequestLog`), `test_http.py` | H12, H22, H23, E2, HL1–HL7, HL9 | 1, 2, 3, 4 |
| S1-6 | The proxy core and its lifecycle | `proxy.py` (§3.3 complete; the three clients; upstream error mapping before the headers; Seriatim's own response headers), `test_http.py`, `test_streaming.py` | H1, H2, H4–H8, H25, B1–B5, E1, S1–S5 | 5 |
| S1-7 | Header rules | `proxy.py` (`upstream_headers`, `prune_httpx_defaults`, `downstream_headers` strip lists), `test_http.py` | H3, H9, H11, H15–H18, H20, H21, H24 | 6 |
| S1-8 | Request limits | `proxy.py` (`read_body` limits and the total timeout, the size check before admission), `app.py` (the `Transfer-Encoding` with `Content-Length` refusal), `test_http.py` | E3, E4, E7–E11, B6, B7, H28, H29 | 6 |
| S1-9 | Errors after the headers and upstream faults | `proxy.py` (`record_framing`, `stream_error_record`, the bodyless rule), `test_streaming.py`, `test_http.py` | S6–S10, S17–S21, H13, F1, F3, F5, E5, E15 | 6 |
| S1-10 | Cancellation, the close proof and the ungated pool | `proxy.py` (cancellation and close edge cases, `PoolTimeout`, ungated timeouts), `test_streaming.py`, `test_http.py` | S11–S16, F2, F4, E16, H14, H26, H27 | 6 |
| S1-11 | Blob uploads | `proxy.py` (`body_stream`), `test_http.py` | H10, E6, E12–E14 | 6 |
| S1-12 | Process and singleton | `app.py` (`main`, `acquire_singleton`), `test_config.py`, `test_process.py` | C8, R1–R9 | 6 |
| S1-13 | Request log, Seriatim's headers on every response, health under load | `app.py` (the log fields on every path), `test_http.py`; also `proxy.py`, `conftest.py` and `harness.py` (the K-OAS2 hook), `test_config.py` (C8) and `test_streaming.py` (`refused_post`) | L1–L3, K-OAS2, H19, HL8 | 8, 9, 10, 11 |
| S1-14 | The PPE sidecar and enforcement | `ppe/compose.yaml` (`node-a`), `tests/behaviour/test_sidecar.py`; `Enforced: yes` | P1–P7 | 1–13 (all) |

"needs" lists the PRs that must merge first; dependencies through them count too (S1-6
needs S1-5, which needs S1-1 to S1-4). PRs with no dependency between them may be open at
the same time when they touch different files. S1-1, S1-2 and S1-4 are independent. S1-7 to
S1-11 all build on S1-6 and all change `proxy.py`, so they go one at a time. S1-14 needs
**every** earlier PR, not only 12 and 13: it sets `Enforced: yes`, so all 138 IDs must pass,
and S1-7's IDs (H3, H9, H11, H15–H18, H20, H21, H24) are in no other PR's chain. S1-14
closes #34 and #15.

**Why each ID sits where it does**, for the ones the review asked about:
- **K-OAS1 and K3** compare the contract with `gate.py`'s tables, so both are in S1-3,
  which needs S1-1.
- **H23, HL4, HL6 and HL7** use `Gate` and `Lease`, so S1-5 needs S1-4. (HL6's four-fault
  order uses a stub gate, because a real gate can't be overdue while starting; its other
  cases use the real one.)
- **HL8** sends a gated request during the health load, so it waits for S1-6 (in S1-13).
- **H25 and the other "freed at once" assertions** need admission and the `finally`, which
  are both in S1-6.
- **H19** covers every response class: 413 and 408 after admission (S1-8), the in-stream
  paths (S1-9), `NODE_SATURATED` (S1-10) and 505 (S1-5). So it is in S1-13.
- **R1 and R2** send gated requests through a real subprocess, so S1-12 needs S1-6.
- **E14**'s gated case (a disconnect while waiting for headers) uses S1-6's watcher, so S1-11
  needs S1-6 only.
- **The drain assertions** in S1-8 to S1-11 rely on the shielded `finally`, which is in S1-6.
- **L2** includes paths from S1-8 (E7), S1-9 (S7), S1-10 (F4, S11) and S1-11 (E12), so S1-13
  needs all four.

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
- `wait_ms`, `hold_ms`, `bytes_in` and `bytes_out` are on every line; `wait_ms` and
  `hold_ms` are `null` when no gate was asked or no slot was taken (see "Details fixed by the
  code (S1-13)").
- The fields `client` and `model` are added in S2 and S3.

## Appendix B — What slice 1 needs from S0 (S0a and S0b)

- **S0a**:
  - **The uv workspace.** A root `pyproject.toml` with `[tool.uv.workspace] members =
    ["node"]` (later also `gateway`), plus the `ruff`, `pytest` and coverage settings. The
    `mutmut` settings live in each package's own `pyproject.toml` (the mutation job, below).
  - **The node package.** `node/pyproject.toml`, with the pinned dependencies in §1 and the
    console script `seriatim-node = seriatim_node.app:main`.
    `node/seriatim_node/__init__.py` holds `__version__`, and `__main__.py` calls `main()`.
  - **`.env.example`**, with the `SERIATIM_NODE_*` names from §6 as commented placeholders.
    It lands in S0a part 2 (#8). S0a ships as several PRs: part 1 (#13: workspace, node
    skeleton, `ci.yml`), part 1b (#16: `private-terms`, `td-test-ids`), part 2 (#8:
    `security.yml`, Dependabot, the PR template, `.env.example`) and the release workflow
    (#10).
  - **The CI workflow** `ci.yml`, with these jobs:
    - `lint`;
    - `test` (matrix: ubuntu, windows, macos);
    - `coverage` (ubuntu, `--cov-fail-under=80`);
    - `mutation`;
    - `contract-lint`, `ip-literals` and `pr-size`;
    - `td-test-ids` (S0a part 1b): it reads the ubuntu `test` leg's JUnit (artifact
      `junit-test-ubuntu`) and this TD's `Enforced:` line (SPEC REQ-63). While this TD says
      `Enforced: no` it prints a notice. S1 is a PR series (§9): each PR marks the tests for
      its §9 IDs with `@pytest.mark.td("<ID>")`, and the last one, S1-14, sets
      `Enforced: yes`. P1 to P7 run in the `ppe` job,
      not in the `test` leg; from S0b, `td-test-ids` also reads that job's JUnit
      (`junit-ppe`, SPEC REQ-63);
    - `private-terms` (S0a part 1b), in its own workflow, as SPEC REQ-63 specifies for pull
      requests from forks.
  - **The mutation job** (`tools/ci/mutation.py`).
    - It finds the changed files with `git diff --name-only --diff-filter=d <base>...HEAD --
      ':(glob)node/**/*.py' ':(glob)gateway/**/*.py'`. With none, it passes with a notice.
    - **It runs mutmut from the package directory** (`node/`), with `[tool.mutmut]` in
      `node/pyproject.toml`:
      - `source_paths = ["seriatim_node"]`. mutmut names a mutant from its file path,
        stripping only a leading `src.`, so from the repository root `node/seriatim_node/x.py`
        would be named `node.seriatim_node.x`, while the tests import `seriatim_node.x`;
      - `only_mutate`, which the job sets to the changed files. mutmut 3.8 deprecated
        `paths_to_mutate`;
      - `pytest_add_cli_args_test_selection = ["tests/node"]`, the package's own tests.
    - **It stages `tests/` and `contract/` inside the package directory** for the run, and
      removes them afterwards, even on failure. mutmut copies them into `mutants/`, beside the
      mutated code, so collection and each mutant's re-run share one root. Without this,
      the recorded test IDs don't resolve, pytest exits 4, and mutmut counts every mutant as
      killed.
    - It runs the in-process tests only (`-m "not process and not behaviour"`). Subprocess
      tests would run the unmutated installed code.
    - It runs `mutmut run`, then `mutmut export-cicd-stats`.
    - It computes the score from the JSON export: (killed + timeout) ÷ (total − skipped).
    - **No mutants.** When the changed files have no mutable code (a version-only change),
      mutmut generates no mutants, exits 1 and writes no export. The job reads mutmut's
      per-file records (`mutants/<file>.meta`, an empty `exit_code_by_key` for every
      changed file) and passes with a notice. A failed run that did generate mutants still
      fails, so does new code no test covers.
    - It fails if the export is missing or malformed, has missing or unexpected keys or a
      non-integer field, reports an interrupted run, or has categories that don't add up to
      its total (unchecked mutants), or if the score is below 75 %.
- **S0b**:
  - **`ppe/compose.yaml`**, with these services:
    - `ollama-a`: `ollama/ollama:0.40.16`, with env `OLLAMA_HOST=127.0.0.1:11435`,
      `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_CONTEXT_LENGTH=2048`, and a debug port published
      on loopback only, through a small `socat` helper, for direct comparison;
    - `cap-a`: tcpdump in `ollama-a`'s network namespace, with the filter
      `tcp dst port 11435` and the capabilities `NET_RAW` and `NET_ADMIN` only;
    - from S1-14, `node-a`: the sidecar, in `ollama-a`'s network namespace (§8.6, "Details
      fixed by the code (S1-14)").

    The limits sum to at most 4 CPUs and 6 GiB.
  - **`ppe/ppe up`.** Before anything starts, it checks the effective configuration
    (`docker compose config`) as SPEC §8.3 requires. In a local run it also checks the
    hostname against the gitignored `ppe/.allowed-host`. `--ci` is accepted only when
    `GITHUB_ACTIONS=true`. It then pulls `qwen2.5:0.5b`, `qwen2.5:1.5b` and `all-minilm:22m`, with no
    `ollama cp` aliases. Each sidecar container's entrypoint runs `seriatim-node init-state`
    against its test state directory before it starts, from S4c on.
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
      proves the route exists. A `HEAD` answer has no body to tell the two apart, so for
      `HEAD` any 404 means the route is absent (the blob is created first, so a 404 there
      means the route has gone). The script removes anything machine-specific (no IPs, no host names) and writes
    `tests/fixtures/ollama-0.40.16.json`.

## Appendix C — The #15 scenarios: where each one is tested

Issue #15 lists 57 scenarios that a codex and agy scenario pass found missing from revision 7.
Each was checked against SPEC and ADR-001 first. Every one is now a test, under a new ID or
in an existing one. One part of one scenario was dropped, with its reason below.

| # | scenario (short) | test |
|---|---|---|
| 1 | External cancellation in each phase | S11 |
| 2 | A second cancellation during cleanup | S12 |
| 3 | A slow reader until the hold deadline | S14 |
| 4 | The final write hangs or raises after a clean end | S15 |
| 5 | A disconnect races a clean completion | S16 |
| 6 | The close is pending while another request arrives | S13 |
| 7 | Client construction fails after admission | E16 |
| 8 | An internal error before and after the response starts | E15 |
| 9 | Monotonic time; the strict overdue boundary | G11 |
| 10 | `Retry-After` from the earliest drain; `busy_for_s == 0` | G12 |
| 11 | Disconnect before a gated body is complete | E7 |
| 12 | The body timeout is total, not per chunk | E8 |
| 13 | The hold deadline during a gated upload | E9 |
| 14 | Empty, at the limit, and one over | E10 |
| 15 | 413 before 429 or 503 | B7 |
| 16 | Ungated buffered routes enforce the limits | E11 |
| 17 | Blob disconnect before and after the first chunk | E12 |
| 18 | Chunked and empty blob uploads | E13 |
| 19 | One owner of `receive` | E14 |
| 20 | `Content-Length` and `Transfer-Encoding` smuggling | H28 |
| 21 | A refused request, then another on the same socket | H29 |
| 22 | `Expect: 100-continue` while busy or starting | B6 |
| 23 | `Connection` tokens in both directions | H15 |
| 24 | The full strip lists | H16 |
| 25 | Repeated end-to-end headers survive | H17 |
| 26 | `X-Request-Id` rules | H18 |
| 27 | Seriatim's three headers on every response | H19 |
| 28 | `X-Seriatim-Gate` for every error class | H19 |
| 29 | Proxy environment variables ignored | H20 |
| 30 | Redirects pass through, never followed | H21 |
| 31 | `/seriatim/*` 404 and 405 over HTTP; look-alikes | H22, K6 (corrected, below) |
| 32 | A missing `raw_path` | H23 |
| 33 | Every forbidden control byte; allowed escapes | K4 |
| 34 | Blob digest edge cases | K5 |
| 35 | Bodies are opaque in S1 | H24 |
| 36 | The health probe stalls | HL5 |
| 37 | Several health faults at once | HL6 |
| 38 | Held or draining slots are not a fault | HL7 |
| 39 | Many concurrent health requests | HL8 |
| 40 | Health logged at `DEBUG` only | HL9 |
| 41 | Ungated upstream stalls and close failures | H26 |
| 42 | The full `NODE_SATURATED` response | H27 |
| 43 | A positive SSE record with a parameterised type | S17 |
| 44 | A failure right after the headers | S18 |
| 45 | Record boundaries split across chunks | S19 |
| 46 | Broken chunked framing | S20 |
| 47 | `HEAD` and bodyless responses are clean | S21 |
| 48 | Clean upstream 5xx and non-JSON errors | H25 |
| 49 | Concurrent startup; loopback aliases | R6 |
| 50 | Permission failures exit 3 | R7 |
| 51 | The lock file keeps its inode | R8 |
| 52 | Different upstream ports are independent | R9 |
| 53 | The remaining ranges and types; NaN and infinity | C9 |
| 54 | An unreadable file; no `.env` | C10 |
| 55 | Non-loopback IPv6 and names as `LISTEN_HOST` | C11 |
| 56 | One accurate log line on every path | L2 |
| 57 | No secrets in the log | L3 (narrowed, below) |

**Corrected or narrowed, with the reason.**
- **#31, the look-alike paths.** The scenario expects `/seriatimx` to get a Seriatim 404
  without contacting Ollama. That contradicts REQ-5: only `/seriatim` and `/seriatim/…` are
  Seriatim's (§2.2 rule 1), and every other unlisted path is forwarded as gated, admin and
  unknown (rule 5). K6 therefore tests that the look-alikes are classified as **not**
  Seriatim's. The rest of #31 is H22.
- **#57, "no exception text".** §5 and E5 put the traceback of an unexpected error in the log
  on purpose, so the error can be diagnosed, and REQ-22 forbids bodies and credentials, not
  exception text. L3 therefore checks that no body, cookie or query credential reaches the
  log, including through an internal error raised while handling a body. It doesn't forbid
  the traceback itself.

**Design changes.** Writing these tests found places where revision 7 would have failed them.
Each is fixed in the section named:
1. **A lease could leak** (#7). `new_gated_client()` ran before the `try`, so an exception
   there skipped the `finally`. It now runs inside the `try` (§3.3, line 10a).
2. **`X-Request-Id`** (#26). Several client headers are now replaced, not guessed between (§2.4
   step 2). The client's own headers are removed before Seriatim's is set, and Ollama's is
   removed from the response (§4).
3. **`Connection` on the response** (#23). The removal of the headers it names is now stated
   for responses too, and repeated headers are kept with every value (§4).
4. **Bodyless responses** (#47). A response to `HEAD`, or a 1xx, 204 or 304, has no body by
   HTTP's rules, so it counts as framed. Before, it would have been treated as broken and
   drained (§3.3 step 5).
5. **The record tail** (#45) is the last two bytes of the whole stream, not of the last
   chunk (§3.3 step 5).
6. **The buffered body timeout is total** (#12), as REQ-20 says (§3.3 step 1).
7. **The health client's pool timeout** counts as `ollama_unreachable`, so many concurrent
   probes can't produce a 500 (#39; §3.4).
8. **The hold limit during the body read** (#13) frees the slot, because nothing was sent.
   The §5 row said "drains" (§5).
9. **Float settings must be finite**, and enum values are case-sensitive (#53; §2.1).
10. **Names as `LISTEN_HOST`** are refused like non-loopback addresses (#55; §2.1 step 5).
