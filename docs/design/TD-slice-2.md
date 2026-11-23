# TD — Slice 2: sidecar authentication

**Status:** revision 1, for review. **Date:** 2026-11-23. The design for ADR-001 §10's S2 row.
Sixteen questions that the SPEC and the ADR leave open are listed in §10. Each has options and
a recommendation, and the design below follows the recommendation, so that it can be reviewed
whole. A ruling that picks another option changes the sections it names.

Enforced: no

(The `td-test-ids` check, SPEC REQ-63, reads this line. S2's code ships as the series of small
PRs in §9. The **last** one, S2-5, sets it to `yes`. Each earlier PR marks the tests of the IDs
its §9 row gives it with `@pytest.mark.td("<ID>")`.)

**Implements:** SPEC REQ-13, REQ-14, REQ-15 (S2 form: the refusal without keys, and the hourly
warning; its listen-address rule keeps its pre-auth form until S4a), REQ-5 (the scope half:
"from slice S2, the scope a key needs"), REQ-22 (the `client` field; a credential is never
logged), REQ-23 (the `keys` setting), and SPEC §6.2's "Generation" and "On nodes".
**Architecture:** ADR-001 D6 (the sidecar half), D7 (standard library only: `hashlib`, `hmac`,
`secrets`, `argparse`), D8, D12 (unknown endpoints need `admin`); threat-model rows T3, T4, T6,
T11 and T15.
**Not in this slice:**
- gateway authentication, the gateway's per-node keys and client-key hashes (REQ-31, S7);
- `/seriatim/status` and its redaction for `infer` keys (REQ-5, REQ-17; S4a). S2 leaves the
  authenticated key in the ASGI scope (§2.4) for S4a to read;
- the admin switch routes and their `changed_by` key id (REQ-19a; S4c);
- a non-loopback listen address with authentication on. REQ-15 also needs the exposure check
  (REQ-19), so that waits for S4a. In S2 the sidecar still listens on loopback only, except
  in test mode;
- reloading keys without a restart. SPEC §6.2 rotates a node's keys by a restart;
- rate limits, lockouts or quotas on failed keys (SPEC §2 non-goals; T5);
- TLS (S11) and mTLS (ADR-001 D6, rejected for now);
- the `num_ctx` policy (S3).

**Still not deployed.** S1 to S3 are not deployed to GPU nodes (TD-slice-1, "Exposure before
S4a"). S2 makes the sidecar refuse to start without keys, but the first node deploy still
needs S4a.

## 1. Files

Across the PR series of §9. No new dependency: everything is in the standard library, so
`node/pyproject.toml` and `uv.lock` do not change.

| # | path | purpose |
|---|---|---|
| 1 | `node/seriatim_node/auth.py` (new) | `Principal`, `KeyEntry`, `Keyring`, `read_credentials()`, `allows()`, `authorise()`, `new_key()`, `keygen()` |
| 2 | `node/seriatim_node/config.py` | the `keys` setting, its validation, and the REQ-15 refusal (load step 7) |
| 3 | `node/seriatim_node/gate.py` | `classify()` gives `/seriatim/*` 404 and 405 the `infer` scope (§4, Q6) |
| 4 | `node/seriatim_node/app.py` | the dispatcher's auth step, the 401/403/400 replies, the principal in the scope, the `client` log field, `main(argv)` with `keygen`, the hourly warning task |
| 5 | `node/seriatim_node/proxy.py` | the `client` field on the proxy's log line; the strip lists are unchanged |
| 6 | `tests/node/test_auth.py` (new) | A1 to A5 |
| 7 | `tests/node/test_http_auth.py` (new) | A6 to A12 |
| 8 | `tests/node/harness.py` | `make_settings(keys=…)`, test mode by default, the log reader keeps `event: request` lines only (Appendix B) |
| 9 | `tests/node/test_config.py` | the S1 tests' baseline gains a key (Appendix B) |
| 10 | `tests/node/test_http.py` | L1's field list gains `client`; the shared log reader (Appendix B) |
| 11 | `tests/node/test_gate.py` | K1's `/seriatim/*` 404 and 405 rows carry `infer` (Q6) |
| 12 | `.env.example` | says that keys are set in the TOML file, never in the environment (Q1) |
| 13 | `docs/design/TD-slice-1.md` | the S1 rows whose tests S2 changes (C1, K1, L1, and §2.2's `/seriatim/*` scope note; Appendix B) |
| 14 | `docs/design/TD-slice-2.md` | the details fixed by the code, and `Enforced: yes` in S2-5 |

`contract/openapi.yaml` needs no change: `AUTH_AMBIGUOUS`, `AUTH_MISSING`, `AUTH_INVALID` and
`FORBIDDEN_SCOPE` are already in `x-seriatim-codes`, the `Unauthorized` response already
requires `WWW-Authenticate: Bearer`, and every path already declares its `x-seriatim-scope`.

## 2. Modules and functions

### 2.1 `auth.py`

`auth.py` imports `gate.py` (for `Route`, `Kind` and `Scope`) and nothing else from the
package, so `config.py` and `app.py` can both import it without a cycle. It never imports
`app.py`: it returns a `Verdict`, and the dispatcher maps that to an `ErrorCode`.

```python
@dataclass(frozen=True, slots=True)
class Principal:                       # the key a request was authenticated with
    id: str                            # the key id: logged, never the key
    scope: Scope                       # Scope.INFER or Scope.ADMIN

@dataclass(frozen=True, slots=True)
class KeyEntry:
    id: str
    scope: Scope
    digest: bytes                      # 32 bytes: bytes.fromhex(the configured sha256)

class Keyring:
    def __init__(self, entries: Sequence[KeyEntry]) -> None   # 1..256 entries, unique ids and digests
    def __len__(self) -> int
    def match(self, token: bytes) -> Principal | None         # every digest compared; no early exit

class Missing:   ...                   # no Authorization header, or one with no credential (Q10)
@dataclass(frozen=True) class Ambiguous: reason: str      # "two Authorization headers", …
@dataclass(frozen=True) class Presented: token: bytes     # the token's bytes, exactly as sent

def read_credentials(headers: list[tuple[bytes, bytes]]) -> Missing | Ambiguous | Presented

class Verdict(StrEnum):
    ALLOW = "allow"; MISSING = "missing"; INVALID = "invalid"
    AMBIGUOUS = "ambiguous"; FORBIDDEN = "forbidden"

@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    principal: Principal | None        # set for ALLOW (with keys) and FORBIDDEN
    reason: str | None                 # Ambiguous.reason, for the 400's message
    api_key_seen: bool                 # an X-API-Key header was sent (the 401's hint, Q11)

def allows(have: Scope, need: Scope) -> bool
def authorise(route: Route, headers: list[tuple[bytes, bytes]], keyring: Keyring | None) -> Decision

def new_key() -> str                   # Q2
def keygen(argv: Sequence[str], out: TextIO) -> int
```

**`read_credentials`** reads only `Authorization`. h11 gives header names in lower case and
strips the value's leading and trailing whitespace. In order:
1. **More than one `authorization` header** → `Ambiguous("two Authorization headers")`, even
   when the values are equal.
2. **None, or one with an empty value** → `Missing` (Q10).
3. **The scheme** is the value up to its first space or tab. It must be `Bearer`, compared
   without regard to case (Q8). Any other scheme, `Basic` and `Digest` included, →
   `Ambiguous("a scheme other than Bearer")`.
4. **The token** is what follows the scheme and one or more spaces (Q9). No token
   (`Bearer` alone) → `Missing` (Q10). A token that contains a space, a tab or any other byte
   below `0x21` → `Ambiguous("whitespace in the key")`. So `Bearer a, Bearer b`, the form a
   proxy gives two headers folded into one, is ambiguous.
5. Otherwise → `Presented(token)`, with the token's bytes exactly as received.

Keys are never read from the query string, `Cookie`, `X-API-Key` or any other header (REQ-13).
`X-API-Key` changes nothing but the hint on a 401 (Q11).

**`Keyring.match`** hashes the token once, `hashlib.sha256(token).digest()`, then compares that
digest with **every** entry's `digest` by `hmac.compare_digest`, in configuration order,
without stopping at a match (REQ-13; T4):

```python
digest = hashlib.sha256(token).digest()
found = None
for entry in self._entries:  # all of them, always
    if hmac.compare_digest(digest, entry.digest):
        found = entry  # digests are unique: at most one match
return None if found is None else Principal(found.id, found.scope)
```

The work done per entry does not depend on where, or whether, the key matches. The only
data-dependent step after a comparison is one assignment. The comparison is digest against
digest, so its time does not depend on the length of the presented key either. Plain SHA-256
is what SPEC §6.2 and ADR-001 D6 require: the keys are 256-bit random values, not passwords,
and a slow hash would only add latency.

**`allows(have, need)`**: `need` is `NONE` → true; `need` is `INFER` → `have` is `INFER` or
`ADMIN`; `need` is `ADMIN` → `have` is `ADMIN`. An `admin` key may use `infer` routes, and an
`infer` key never uses `admin` ones (REQ-14).

**`authorise(route, headers, keyring)`** is the one place a request's credentials are judged.
`keyring` is `None` only in unauthenticated test mode (no keys, and
`ALLOW_UNAUTHENTICATED=true`; §6).
1. `keyring is None` → `ALLOW`, with no principal. The headers are not read at all, so S1's
   behaviour, and S1's suite, are unchanged in test mode (Q12).
2. `route` is `GET /seriatim/health` (`Kind.SERIATIM`, status 200, scope `NONE`) → `ALLOW`,
   with no principal. Its credentials are not read, so a wrong or ambiguous header never
   fails a load balancer's probe (Q7).
3. `read_credentials`: `Ambiguous` → `AMBIGUOUS`; `Missing` → `MISSING`.
4. `keyring.match(token)` is `None` → `INVALID`.
5. `allows(principal.scope, route.scope)` is false → `FORBIDDEN`, with the principal.
6. Otherwise `ALLOW`, with the principal.

So an ambiguous header is refused even when one of its values is a valid key, and a missing or
wrong key is refused before the route's scope is looked at.

### 2.2 `config.py`

```python
class KeyConfig(BaseModel):            # one [[keys]] table in the TOML file
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")   # Q3
    scope: Literal["infer", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    model_config = ConfigDict(extra="forbid")

class Settings(BaseSettings):
    ...                                 # every S1 field, unchanged
    keys: tuple[KeyConfig, ...] = ()    # from the TOML file only (Q1)

KEYS_VAR = "SERIATIM_NODE_KEYS"
MAX_KEYS = 256                          # Q3

def keyring(settings: Settings) -> Keyring | None   # None in unauthenticated test mode
```

`load_settings` keeps S1's six steps and adds to two of them:
- **Step 2 (unknown variables).** `SERIATIM_NODE_KEYS` in the environment →
  `ConfigError("SERIATIM_NODE_KEYS: keys are set in the SERIATIM_NODE_CONFIG file only")`
  (Q1). It is a field, so S1's rule alone would accept it.
- **Step 3 (build).** A `ValidationError` inside `keys` names the entry:
  `SERIATIM_NODE_KEYS[<index>].<field>: <pydantic's message>`. Pydantic's message never
  contains the input value, and the code never adds it, so neither a hash nor anything else
  from the file is printed.
- **Step 7 (keys), new, after step 6:**
  - more than `MAX_KEYS` entries → `ConfigError` naming `SERIATIM_NODE_KEYS`;
  - two entries with the same `id` → `ConfigError` naming the id;
  - two entries with the same `sha256` (compared as bytes, so case does not matter) →
    `ConfigError` naming both ids;
  - **no keys and `allow_unauthenticated` false** → `ConfigError("SERIATIM_NODE_KEYS: no keys
    are configured; add [[keys]] to the SERIATIM_NODE_CONFIG file (seriatim-node keygen), or
    set SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true in the PPE and tests only")` (REQ-15; Q13).

  `main()` already turns a `ConfigError` into exit code 2 with the message on stderr
  (REQ-23), so each of these refuses to start.

`keyring(settings)` builds the `Keyring` from `settings.keys`, with each `sha256` as
`bytes.fromhex(…)`. With no keys it returns `None`, which only happens in test mode, because
step 7 refused every other case. With keys it returns a `Keyring` whatever
`allow_unauthenticated` says: configured keys are always enforced (Q4).

**The listen address.** Step 5 is unchanged in S2: a non-loopback `listen_host` still needs
`ALLOW_UNAUTHENTICATED=true`, even with keys, because REQ-15 also needs the exposure check
(S4a). The startup `WARNING` that step 5 logs when `allow_unauthenticated` is true is also
unchanged.

### 2.3 `gate.py`

`classify()` is unchanged for every Ollama route. For `/seriatim/*`, the scope it returns
becomes the scope the request needs (REQ-5, "Their scopes are fixed by the contract"):
- `GET /seriatim/health` → scope `NONE` (the contract's `none`), as in S1;
- an unknown `/seriatim/*` path (404), and a known one with the wrong method (405), → scope
  `INFER` (Q6), so they need a valid key of either scope before they get their 404 or 405.
  That includes `POST` and `HEAD` on `/seriatim/health`. S4a and S4c add their routes to
  `SERIATIM_ROUTES` with their contract scopes (`/seriatim/status`: `infer`;
  `/seriatim/admin/*`: `admin`).

That keeps ADR-001 D6's rule that the scope a route needs comes from the same `classify()`
that decides admission. K1's three `/seriatim/*` rows change with it (Appendix B).

### 2.4 `app.py`

**The dispatcher's auth step.** It goes right after `classify` (TD-slice-1 §2.4 step 5), before
anything route-specific: the `/seriatim/*` 404 and 405, health, and the proxy, so before the
proxy's 413 and before admission. The checks that run before `classify` keep their S1 order,
before auth (Q5). §3 gives the whole order.

```python
decision = authorise(route, scope["headers"], self.keyring)
if decision.verdict is not Verdict.ALLOW:
    await self.refuse_auth(send, decision, request_id=rid, method=method, where=where)
    return
scope[PRINCIPAL_KEY] = decision.principal  # "seriatim.principal": Principal | None
```

- `self.keyring` is `keyring(settings)`, built once in `_Dispatcher.__init__`.
- `PRINCIPAL_KEY` is how later handlers learn who is calling: S4a redacts `/seriatim/status`
  for `infer` keys from it, and S4c records `changed_by` from it.
- **Fail closed.** `create_app` raises `ConfigError` for settings with no keys and
  `allow_unauthenticated` false. `load_settings` never builds such settings, but `Settings`
  can be built directly, and the app must never serve them.

**`refuse_auth`** uses S1's `reply` path, so it sends exactly one response and writes exactly
one log line:

| verdict | status, `error_code` | message | extra headers |
|---|---|---|---|
| `AMBIGUOUS` | 400 `AUTH_AMBIGUOUS` | `ambiguous credentials: <reason>` | — |
| `MISSING` | 401 `AUTH_MISSING` | `a Bearer key is required`; with `hint: "X-API-Key is not accepted; send Authorization: Bearer <key>"` when `api_key_seen` (Q11) | `WWW-Authenticate: Bearer` |
| `INVALID` | 401 `AUTH_INVALID` | `the key is not valid` | `WWW-Authenticate: Bearer` |
| `FORBIDDEN` | 403 `FORBIDDEN_SCOPE` | `this route needs the admin scope` | — |

- `retryable: false` and `replay_safe: true` come from `ERROR_TABLE`, as for every code.
- **`X-Seriatim-Gate: ungated`.** No slot was asked for. None of these codes is `refused`,
  so a gateway never fails over on one (SPEC REQ-37's table doesn't list them).
- **`Connection: close`** whenever the request has a body (`has_body(headers)`: a non-zero
  `Content-Length`, or `Transfer-Encoding`). The reply is sent before the body is read, the
  rule S1-8 set for the 429 and 503 (TD-slice-1 H29). A request with no body keeps its
  connection.
- **The message never echoes** the header, the key, its hash or the scheme's value. The
  `reason` is one of the three fixed strings of §2.1.
- **The `WWW-Authenticate` value** is exactly `Bearer`, which is what the contract's
  `Unauthorized` response requires. It carries no `error=` parameter.

**The log line** (REQ-22, TD-slice-1 Appendix A) gains `client`, after `known`: the key id
of the request's principal, or `null` when there is none (test mode, health, and every 400 or
401 from auth). A 403 logs the id of the key that was refused, which is what T15 needs.
`outcome` is `unauthenticated` for both 401s, `forbidden` for the 403, and `rejected` for the
400, all at `INFO` (Q15). Lines that S1 writes before `classify` (505, `BAD_PATH`, the
missing `raw_path`) have no `client`, just as they have no `path`.

**`main(argv=None)`.** S1's `main()` takes no arguments. S2's reads `argv` (default
`sys.argv[1:]`):
- `[]` → serve, exactly as in S1;
- `["keygen", …]` → `keygen(argv[1:], sys.stdout)` (§7). It never calls `load_settings`,
  `acquire_singleton` or `uvicorn.run`;
- anything else → a one-line usage message on stderr, exit 2.

The `seriatim-node` console script and `python -m seriatim_node` both call `main()` with no
argument, so both get the subcommand.

**The hourly warning (REQ-15).** In unauthenticated test mode, the lifespan starts one task,
in a task group that the lifespan cancels on shutdown:

```python
async def _warn_unauthenticated(self) -> None:
    while True:
        self.log.event("unauthenticated", level="WARNING", message=ALLOW_WARNING)
        await anyio.sleep(self.warn_every_s)  # 3600; create_app(…, warn_every_s=…) in tests
```

- The first line is written when the lifespan starts, then one every hour (Q14). The stderr
  line that `load_settings` already writes at startup stays as it is (TD-slice-1 C5).
- `RequestLog.event(name, *, level, **fields)` writes the same JSON line format as `emit`,
  with `event` set to `name` and no request fields (Appendix A). `emit` becomes
  `event("request", …)`.
- `warn_every_s` is a `create_app` argument, not a setting, so a deployment can't change it
  (REQ-23 has no key for it).
- With keys configured, there is no task, whatever `allow_unauthenticated` says (Q4).
- The task never touches the gate, so ADR-001 §6's "no background task ever frees a slot"
  still holds.

### 2.5 `proxy.py`

`Proxy.__call__` adds `client` to its `where` fields, from `scope[PRINCIPAL_KEY]`, so its one
log line carries it (§2.4). Nothing else in the proxy changes:
- **The strip lists already remove every credential** before Ollama sees the request:
  `authorization`, `cookie` and `x-api-key` by name, `proxy-authorization` as a hop-by-hop
  header, and `x-seriatim-*` by prefix (TD-slice-1 §4; ADR-001 §5.3). S2 adds the tests that
  show it with authentication on (A11).
- **Admission is unchanged.** An authenticated request then meets S1's 413, `NODE_STARTING`
  and the admission 429 exactly as before.

## 3. Request flow and precedence

For each request, in this order. The first rule that applies answers it:

| step | check | answer | since |
|---|---|---|---|
| 1 | HTTP version other than 1.1 | 505 `HTTP_VERSION_UNSUPPORTED` | S1 |
| 2 | `Transfer-Encoding` with `Content-Length` | 400 `BAD_REQUEST_BODY` | S1 |
| 3 | no `raw_path` in the ASGI scope | 500 `INTERNAL` | S1 |
| 4 | unsafe raw path | 400 `BAD_PATH` | S1 |
| 5 | `classify()` | — | S1 |
| 6 | **auth** (§2.1 `authorise`): ambiguous, then missing, then invalid, then scope | 400 `AUTH_AMBIGUOUS`, 401 `AUTH_MISSING`, 401 `AUTH_INVALID`, 403 `FORBIDDEN_SCOPE` | **S2** |
| 7 | `/seriatim/*`: unknown path, wrong method, health | 404, 405, or health | S1 |
| 8 | `Content-Length` over the limit (buffered routes) | 413 `BODY_TOO_LARGE` | S1 |
| 9 | gated: `NODE_OFFLINE` (S4c), the exposure codes (S4a), `NODE_STARTING`, busy | 503 or 429 | S1, S4a, S4c |
| 10 | the body, the exchange with Ollama | as TD-slice-1 §5 | S1 |

Consequences:
- **Auth is answered before the body is read**, before any slot is asked for, and without
  contacting Ollama. An unauthenticated client never learns whether the node is busy or
  starting, or what its body limit is (T11: blind requests are rejected with 401).
- **The REQ-19a precedence of refusals** (offline, exposure, starting, busy) applies to a
  request that has passed auth. S2 does not change it.
- **Steps 1 to 4 stay before auth** (Q5). They say nothing about the node: the HTTP version,
  framing and path rules are public, and a path has to be safe before `classify()` can say
  which scope it needs.

## 4. Route classes and scopes

The classes are `classify()`'s (REQ-5). "Passes" means the request goes on to step 7 of §3,
with the principal in the scope, and is served as S1 serves it.

| route class (REQ-5) | needs | no key | wrong key | `infer` key | `admin` key |
|---|---|---|---|---|---|
| `GET /seriatim/health` | none | passes | passes (not read, Q7) | passes | passes |
| other `/seriatim/*` (404, 405) | `infer` (Q6) | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 404 / 405 | 404 / 405 |
| ungated, `infer`: `GET /`, `HEAD /`, `GET /api/tags`, `GET /api/ps`, `GET /api/version`, `POST /api/show`, `GET /v1/models` | `infer` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | passes | passes |
| ungated, `admin`: `POST /api/pull`, `POST /api/push`, `POST /api/copy`, `DELETE /api/delete`, `HEAD`/`POST /api/blobs/sha256:<64 hex>` | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| gated, `infer`: `POST /api/generate`, `/api/chat`, `/api/embed`, `/api/embeddings`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | `infer` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | passes | passes |
| gated, `admin`, known: `POST /api/create` | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| gated, `admin`, unknown: every other method and path, `OPTIONS` included | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| *S4a:* `GET /seriatim/status` | `infer` | 401 | 401 | passes (redacted) | passes |
| *S4c:* `POST /seriatim/admin/offline`, `.../online` | `admin` | 401 | 401 | 403 | passes |

An ambiguous `Authorization` gets 400 `AUTH_AMBIGUOUS` on every row but health. In
unauthenticated test mode (no keys), every row passes, the admin rows included (Q12).

## 5. Error mapping

These rows join TD-slice-1 §5's table, between `BAD_PATH` and the `/seriatim/*` 404 and 405:

| condition | before | after | slot | log `outcome` |
|---|---|---|---|---|
| Two `Authorization` headers, a scheme other than Bearer, or whitespace in the key | 400 `AUTH_AMBIGUOUS` (`Connection: close` with a body) | — | none taken | `rejected` |
| No key | 401 `AUTH_MISSING` + `WWW-Authenticate: Bearer` (`Connection: close` with a body) | — | none taken | `unauthenticated` |
| A key that matches no configured hash | 401 `AUTH_INVALID` + `WWW-Authenticate: Bearer` (`Connection: close` with a body) | — | none taken | `unauthenticated` |
| A valid key without the route's scope | 403 `FORBIDDEN_SCOPE` (`Connection: close` with a body) | — | none taken | `forbidden` |

Each carries `X-Seriatim-Gate: ungated`, `X-Seriatim-Node` and `X-Request-Id`, as every
response does (TD-slice-1 H19). None ever happens after the response headers.

## 6. Configuration

**The keys** are a TOML array of tables in the file named by `SERIATIM_NODE_CONFIG` (Q1),
one table per key, as `keygen` prints them (§7):

```toml
[[keys]]
id = "client-a"
scope = "infer"
sha256 = "<64 hex digits>"

[[keys]]
id = "ops"
scope = "admin"
sha256 = "<64 hex digits>"
```

| key | rule |
|---|---|
| `keys` | 0 to 256 tables (Q3), only in the TOML file; `SERIATIM_NODE_KEYS` in the environment → exit 2 (Q1) |
| `keys[].id` | `^[a-z0-9][a-z0-9-]{0,31}$`, unique (Q3) |
| `keys[].scope` | `infer` or `admin` |
| `keys[].sha256` | 64 hex digits, either case; unique as bytes |
| any other key in a table | exit 2, naming it |

**The three modes.** Whether the sidecar authenticates follows from the keys alone:

| keys | `ALLOW_UNAUTHENTICATED` | result |
|---|---|---|
| none | `false` | refuses to start, exit 2 (REQ-15) |
| none | `true` | **unauthenticated test mode**: no request is authenticated; the startup `WARNING` on stderr, and a `WARNING` line every hour (§2.4) |
| some | `false` | authenticated; loopback listen only (REQ-15, until S4a) |
| some | `true` | authenticated; the test-mode relaxations still apply (another lock directory, a non-loopback listen address, from S4b the probe fixture) and the startup `WARNING` too, but no hourly line (Q4) |

The flag's name says "unauthenticated", but it has been S1's test-mode switch too (REQ-12b,
REQ-16). Q4 asks whether to keep that.

**Rotation** (SPEC §6.2): add the new key's table, restart the sidecar, move the client over,
remove the old table, restart again. Several keys can be valid at once.

**The PPE** is unchanged in S2. Its compose file sets `ALLOW_UNAUTHENTICATED=true` and no
keys, so it runs in unauthenticated test mode and P1 to P7 run as before (Q16).

## 7. `keygen`

```text
seriatim-node keygen --id <id> --scope infer|admin
```

- **Arguments.** Both are required. `--id` follows the key-id rule of §6; `--scope` is `infer`
  or `admin`. A missing, repeated or invalid argument, or any other argument, prints argparse's
  usage on stderr and exits 2.
- **The key.** `new_key()`: 32 bytes from `secrets.token_bytes`, encoded as Q2 says
  (recommended: `seriatim_` followed by the 43-character unpadded base64url form,
  `secrets.token_urlsafe(32)`). The prefix lets a secret scanner recognise a leaked key.
- **The output**, on stdout only, once: the key on its own line, a blank line, then the
  `[[keys]]` table with `id`, `scope` and `sha256 = hashlib.sha256(key.encode("ascii")).hexdigest()`
  (Appendix A). That is the hash the sidecar computes for `Authorization: Bearer <key>`.
- **Nothing is written to disk.** `keygen` opens no file, reads no `SERIATIM_NODE_*` variable
  and no config file (so it works on a node whose config is not yet valid), takes no lock, and
  starts no server. Its exit code is 0.
- **Where it runs.** On the operator's machine or on the node. The key goes to the client
  (or, from S7, into the gateway's config), and the table into the node's config file.

## 8. Tests to write (exact)

Each test names the requirement it proves. All run in CI on `ubuntu-latest`, and also on
`windows-latest` and `macos-latest`. None needs the PPE (Q16). The tiers are:
- **unit**: functions called directly, in `tests/node/test_auth.py`;
- **http**: the real sidecar on a loopback port in front of `fake_ollama` (TD-slice-1 §7), in
  `tests/node/test_http_auth.py`;
- **process**: `main()` in process with `uvicorn.run` monkeypatched, as TD-slice-1 C8 does.

Keys in tests come from `new_key()`, never from a literal, so nothing that looks like a key is
committed. The http tests write their `[[keys]]` file under `tmp_path`.

| id | test | tier | REQ |
|---|---|---|---|
| A1 | `read_credentials`, table-driven: no header → `Missing`; `Authorization:` with an empty value → `Missing`; `Bearer` alone → `Missing`; `Bearer <key>` → `Presented` with the exact bytes; `bearer <key>` and `BEARER <key>` → `Presented` (Q8); `Bearer  <key>` (two spaces) → `Presented` (Q9); two `Authorization` headers, equal and different, one of them valid → `Ambiguous("two Authorization headers")`; `Basic <b64>`, `Digest x`, `Token <key>` → `Ambiguous("a scheme other than Bearer")`; `Bearer a b`, `Bearer a\tb`, `Bearer a, Bearer b` → `Ambiguous("whitespace in the key")`. `X-API-Key: <key>` alone, `Cookie: key=<key>`, and a key only in the query string → `Missing`. Hypothesis: any token of `b64token` characters round-trips to `Presented` with the same bytes, and any token with an embedded space or tab is `Ambiguous` | unit | 13 |
| A2 | `Keyring.match` and `allows`. With 5 keys, `hmac.compare_digest` (patched in `auth`, counting) is called exactly 5 times when the key matches the first entry, the third, the last, and none; SHA-256 is computed once per call. The matching entry's `Principal` (id and scope) is returned; a key differing in one character, the key's hex hash sent as the key, and an empty token → `None`. `allows` for every (have, need) pair of `INFER`/`ADMIN` × `NONE`/`INFER`/`ADMIN`: only `INFER` needing `ADMIN` is false. `Keyring` refuses 0 entries, 257 entries, a repeated id and a repeated digest (`ValueError`) | unit | 13, 14 |
| A3 | Loading keys. A config file with an `infer` and an `admin` key → `settings.keys` in order, and `keyring(settings)` matches each key with its id and scope; `sha256` in upper case is accepted and equal to the lower-case one. Each of these → `ConfigError` naming `SERIATIM_NODE_KEYS[<i>].<field>` or `SERIATIM_NODE_KEYS`: a table with no `id`, `scope` or `sha256`; an unknown field; `scope = "read"`; an id `Client`, `-a`, 33 characters or empty; a `sha256` of 63 or 65 digits, or with a non-hex digit; `keys = "x"`; 257 tables; two tables with one id; two tables with one hash in different cases. `SERIATIM_NODE_KEYS` set in the environment → `ConfigError` naming it, even when it is valid JSON (Q1). No error message contains the configured hash or any other value from the file | unit | 23, 13 |
| A4 | Startup refusals (REQ-15). No keys and no `ALLOW_UNAUTHENTICATED` → `ConfigError` naming `SERIATIM_NODE_KEYS`, and `main()` exits 2 with that message on stderr and never calls `uvicorn.run`. No keys with `ALLOW_UNAUTHENTICATED=true` → starts, `keyring` is `None`, the startup `WARNING` is captured. Keys with `ALLOW_UNAUTHENTICATED=true` → starts with a `Keyring` (Q4). Keys and `LISTEN_HOST=0.0.0.0` without `ALLOW_UNAUTHENTICATED` → still `ConfigError` naming `SERIATIM_NODE_LISTEN_HOST` (S2 does not relax the listen rule). `create_app` given `Settings(keys=(), allow_unauthenticated=False)` built directly → `ConfigError` | process | 15, 23 |
| A5 | `keygen`. `main(["keygen", "--id", "client-a", "--scope", "infer"])` exits 0 and prints the key, a blank line and the table; the key has the Q2 form and decodes to 32 bytes; the table's `sha256` is the SHA-256 of the printed key; written to a config file, the table loads (A3) and the sidecar built from it accepts the printed key with id `client-a` and scope `infer` (one http request). Two runs print different keys. It succeeds with an invalid `SERIATIM_NODE_SLOTS` and an unreadable `SERIATIM_NODE_CONFIG` in the environment (it reads neither), creates no file in the working directory or the lock directory, and calls neither `acquire_singleton` nor `uvicorn.run` (patched). `--scope read`, `--id Bad`, a missing `--id`, an extra argument, `main(["keygen"])` and `main(["nope"])` each exit 2 with usage on stderr. `main([])` serves as in S1 (TD-slice-1 C8) | process | §6.2, 23 |
| A6 | 401 across every route class. With keys configured, for one pair of each class in §4 (every pair of the REQ-5 lists, from `tests/fixtures/route-pairs.json`, plus `POST /api/create`, `OPTIONS /api/tags`, `PUT /api/chat`, `POST /api/new-feature`, `GET /seriatim/nope` and `POST /seriatim/health`): no key → 401 `AUTH_MISSING`; a key that is not configured → 401 `AUTH_INVALID`. Each: `WWW-Authenticate: Bearer` exactly once, `retryable: false`, `replay_safe: true`, `X-Seriatim-Gate: ungated`, the body validates against `ErrorEnvelope` (K-OAS2); `fake_ollama` recorded nothing; every slot is `FREE` | http | 13, 25 |
| A7 | The scope matrix of §4, for the same pairs as A6: an `infer` key → the `infer` classes pass (the gated ones get Ollama's answer with `X-Seriatim-Gate: acquired`, the ungated ones `ungated`, `/seriatim/nope` 404 and `POST /seriatim/health` 405), and every `admin` class, unknown endpoints included, gets 403 `FORBIDDEN_SCOPE` with no `WWW-Authenticate`, nothing recorded upstream and no slot taken; an `admin` key → every class passes. A 403's log line has `client` equal to the `infer` key's id | http | 14, 5 |
| A8 | Health. With keys configured, `GET /seriatim/health` with no key, a wrong key, two `Authorization` headers and `Basic x` → 200 `{"status":"ok"}` each time (Q7); its line is still at `DEBUG` with `client: null` (TD-slice-1 HL9). `HEAD /seriatim/health` and `POST /seriatim/health` with no key → 401 `AUTH_MISSING`; with an `infer` key → 405 with `Allow: GET` (Q6) | http | 13, 18 |
| A9 | Ambiguous credentials over HTTP (raw socket where httpx would merge headers): two `Authorization` headers, one of them a valid `admin` key; `Basic <b64>`; `Bearer <key> x`; `Bearer a, Bearer b` → 400 `AUTH_AMBIGUOUS` on a gated, an ungated and a `/seriatim/*` route; no `WWW-Authenticate`; nothing recorded upstream; no slot taken; the message names the reason and contains no header value. A valid key in `X-API-Key` with no `Authorization` → 401 `AUTH_MISSING` with the `X-API-Key` hint (Q11); with a valid Bearer key as well → passes, and Ollama receives neither header. `?key=<valid key>` and `Cookie: key=<valid key>` with no `Authorization` → 401 `AUTH_MISSING` | http | 13 |
| A10 | Auth comes first (§3). With no key: a gated request while the only slot is held → 401, never 429; during the startup quarantine → 401, never 503 `NODE_STARTING`; with `Content-Length` over `max_body_bytes` → 401, never 413; in each case the slot holder is undisturbed and the gate state is unchanged. Over a raw socket, the headers of a `POST /api/chat` with `Content-Length: 4096` and no body bytes → the 401 arrives before any body is sent, with `Connection: close`, and the connection is closed; an `Expect: 100-continue` request gets its 401 with no `100 Continue`. A 401 to a `GET` with no body keeps the connection, and the next request on it with a valid key gets 200. S1's earlier checks still win: HTTP/1.0 → 505, `/api/../chat` → 400 `BAD_PATH`, `Transfer-Encoding` with `Content-Length` → 400 `BAD_REQUEST_BODY`, each with no key (Q5) | http | 13, 3, 20, 20a |
| A11 | The key never leaves. With a valid `infer` key on a gated `/api/chat` (non-streaming and streaming) and on `/api/tags`, and a valid `admin` key on a streamed `POST /api/blobs/sha256:<hex>`, each also sending `X-API-Key`, `Cookie` and `Proxy-Authorization` → `fake_ollama` recorded none of `Authorization`, `X-API-Key`, `Cookie` or `Proxy-Authorization`. Across one request of every outcome in §5 and TD-slice-1 §5 that S2 can reach (200, 400 `AUTH_AMBIGUOUS`, 401 both ways, 403, 404, 413, 429, 502), no log line, response body or response header contains the key, its SHA-256 in hex, or the configured hash of any other key; `client` is the key's id on the authenticated lines (the 403 included) and `null` on the 400 and 401 lines. `uvicorn`'s access log stays off (TD-slice-1 L3) | http | 13, 22 |
| A12 | Unauthenticated test mode and the hourly warning. With no keys and `ALLOW_UNAUTHENTICATED=true`: `POST /api/create` and `OPTIONS /api/tags` with no key reach Ollama (every route is open, Q12); a request with two `Authorization` headers is not refused, and Ollama receives neither; `client` is `null`. With `create_app(…, warn_every_s=0.2)`: one `WARNING` line with `event: "unauthenticated"` at startup, and further ones at that interval (at least two more within a bounded wait, each at least the interval apart); none after shutdown; none at all when keys are configured, with or without `ALLOW_UNAUTHENTICATED` (Q4). The line has the fields in Appendix A and no request fields | http | 15 |

### 8.1 Quality gates for this slice (CI output quoted in each PR)

As TD-slice-1 §8.7:
- **Coverage.** At least 80 % combined and 80 % branch, from the `coverage` job.
- **Mutation.** `mutmut` on the changed files under `node/` (`auth.py`, and `config.py`,
  `gate.py`, `app.py` and `proxy.py` where a PR changes them), at least 75 %, with each
  survivor explained. `Keyring.match`'s "no early exit" is what A2's call count kills: a
  `break` mutant changes it.
- **S1 still passes.** Every TD-slice-1 ID still has a passing test (TD-slice-1 is
  `Enforced: yes`), including those whose tests Appendix B changes.
- **Size.** Each PR in §9 passes the PR-size check on its own.
- **Review.** An independent review of each PR's diff; every real finding fixed and listed.

## 9. Delivery: the S2 PR series

Five small PRs, in this order. Each has its own issue, a sub-issue of #35. Each leaves `main`
releasable and green, and ships the tests for exactly the IDs listed.

| PR | scope | files (new or changed) | TD IDs | needs |
|---|---|---|---|---|
| S2-1 | Credentials and the keyring | `auth.py` (`Principal`, `KeyEntry`, `Keyring`, `read_credentials`, `allows`, `authorise`), `tests/node/test_auth.py` | A1, A2 | — |
| S2-2 | Keys in the configuration, and the startup refusals | `config.py` (`KeyConfig`, `keys`, steps 2, 3 and 7, `keyring()`), `test_auth.py`, `test_config.py` and `harness.py` (Appendix B), `.env.example`, `TD-slice-1.md` (C1) | A3, A4 (all but its `create_app` case) | 1 |
| S2-3 | Enforcement | `app.py` (the auth step, `refuse_auth`, `PRINCIPAL_KEY`, the `client` field, the `create_app` refusal), `proxy.py` (`client`), `gate.py` (Q6), `tests/node/test_http_auth.py` (new), `test_http.py`, `test_gate.py` (Appendix B), `TD-slice-1.md` (K1, L1, §2.2) | A4 (its `create_app` case), A6–A11 | 2 |
| S2-4 | `keygen` | `auth.py` (`new_key`, `keygen`), `app.py` (`main(argv)`), `test_auth.py`, `test_config.py` (C8 calls `main([])`) | A5 | 2, 3 |
| S2-5 | Test mode's hourly warning, and enforcement | `app.py` (the warning task, `RequestLog.event`, `warn_every_s`), `test_http_auth.py`, `docs/design/TD-slice-2.md` (`Enforced: yes`, the details fixed by the code) | A12 | 1–4 (all) |

- **S2-2 before S2-3.** Between them, `main` refuses to start without keys, but does not yet
  check them. Nothing is deployed from S1 to S3 (the "Still not deployed" note above), so no
  node runs that state. Putting both in one PR would make it about 12 files and the largest
  review of the slice.
- **S2-3 and S2-4 both change `app.py`,** so they go one at a time. S2-4 needs S2-2, because
  A5 loads the printed table through `load_settings`.
- **S2-5 needs every earlier PR,** because it sets `Enforced: yes`, so all of A1 to A12 must
  pass. It closes #35.
- **Docs with code.** Each PR updates this TD's "details fixed by the code" for what it built,
  and the SPEC or ADR where the code refines them.

## 10. Open questions

Each is for the maintainer, on #35. The design above follows each recommendation.

1. **Where keys are configured.** REQ-23 says the sidecar reads `SERIATIM_NODE_*` variables and
   one TOML file. Options: **(A)** `[[keys]]` tables in the `SERIATIM_NODE_CONFIG` file only;
   `SERIATIM_NODE_KEYS` in the environment is refused (exit 2); **(B)** also accept
   `SERIATIM_NODE_KEYS` as a JSON array; **(C)** a separate keys file, which needs a REQ-23
   change. *Recommendation: A.* A list of tables is natural in TOML and awkward in an
   environment variable, and one place means rotation edits one file. (§2.2, §6.) A related
   detail: SPEC §6.2 says `keygen` prints "the config line"; a `[[keys]]` table is three lines.
   The TD prints the table (§7, Appendix A).
2. **The key's printed form.** SPEC §6.2 and D6 fix 256 random bits, not the encoding.
   Options: **(A)** `secrets.token_urlsafe(32)`, 43 base64url characters; **(B)** the same with
   a fixed prefix, `seriatim_`; **(C)** 64 hex digits. *Recommendation: B.* The prefix lets
   gitleaks (and other scanners) be given a rule for leaked keys (T3), and base64url is a valid
   RFC 6750 `b64token`. The sidecar never checks the form: it hashes whatever is presented.
   (§7.)
3. **Key ids and the number of keys.** The SPEC gives no rule. Options: **(A)** the node-id
   pattern `^[a-z0-9][a-z0-9-]{0,31}$`, at most 256 keys; **(B)** any printable string up to 64
   characters, no limit. *Recommendation: A.* Ids appear in logs, and from S4c in `changed_by`,
   so a narrow alphabet keeps them safe to log; a cap bounds the per-request comparison cost.
   (§2.2, §6.)
4. **Keys and `ALLOW_UNAUTHENTICATED=true` together.** REQ-15 says what happens with no keys,
   not with both. The flag is also S1's test-mode switch (REQ-12b's lock directory, REQ-16's
   probe fixture, REQ-15's listen address before S4a). Options: **(A)** configured keys are
   always enforced; the flag keeps its test-mode relaxations and the startup `WARNING`, and
   there is no hourly line; **(B)** refuse to start, as contradictory; **(C)** the flag wins,
   and the keys are ignored. *Recommendation: A.* It is the only option that lets a PPE or a
   test run with keys on, and C would silently turn configured keys off. A later rename of the
   flag to say "test mode" is a separate question. (§2.2, §6.)
5. **The checks before auth.** Auth needs `classify()` for the scope, and REQ-20a puts the
   path check before `classify()`. Options: **(A)** keep S1's 505, framing 400, missing
   `raw_path` 500 and `BAD_PATH` 400 before auth; **(B)** read the credentials (400 and 401)
   before everything, and check the scope (403) after `classify()`. *Recommendation: A.* Those
   checks reveal nothing about the node, and A keeps the dispatcher's order and S1's tests as
   they are. (§3.)
6. **Unknown `/seriatim/*` paths and wrong methods** (404, 405, including `POST` and `HEAD` on
   `/seriatim/health`). REQ-13 says every endpoint but `GET /seriatim/health` needs a key, and
   the contract gives no scope to paths it doesn't list. Options: **(A)** any valid key
   (`infer`), then 404 or 405; **(B)** answer 404 or 405 before auth; **(C)** `admin`.
   *Recommendation: A.* It follows REQ-13 to the letter, and an unauthenticated caller learns
   nothing about which `/seriatim/*` routes a node has. Until S4c adds them, the
   `/seriatim/admin/*` paths are a 404 to any valid key; from S4c an `infer` key gets 403. It
   changes K1's three `/seriatim/*` rows. (§2.3, §4.)
7. **Health with bad credentials.** `GET /seriatim/health` with a wrong key or an ambiguous
   `Authorization`. Options: **(A)** credentials are not read: 200; **(B)** they are checked
   whenever present: 401 or 400. *Recommendation: A.* Health needs no key (REQ-18), and a load
   balancer configured with a stale key should not take a healthy node out. (§2.1, A8.)
8. **The scheme's case.** RFC 9110 §11.1 makes auth schemes case-insensitive; REQ-13 says "a
   scheme other than Bearer". Options: **(A)** `bearer` and `BEARER` are Bearer; **(B)** only
   the exact `Bearer`; anything else is `AUTH_AMBIGUOUS`. *Recommendation: A*, as the RFC
   says. (§2.1.)
9. **Whitespace after `Bearer`.** RFC 6750 §2.1 allows one or more spaces before the token;
   REQ-13 rejects "a Bearer value with embedded whitespace". Options: **(A)** one or more
   spaces after the scheme; whitespace inside the token is ambiguous; **(B)** exactly one
   space; any other whitespace is ambiguous. *Recommendation: A*, as the RFC says; "embedded"
   then means inside the token. (§2.1.)
10. **An empty credential.** `Authorization:` with an empty value, or `Bearer` with no token.
    Options: **(A)** 401 `AUTH_MISSING`; **(B)** 401 `AUTH_INVALID`; **(C)** 400
    `AUTH_AMBIGUOUS`. *Recommendation: A.* No key was presented, and the client's fix is the
    same as for no header. (§2.1.)
11. **`X-API-Key`.** REQ-13 says it is not accepted. Options: **(A)** ignored: the request is
    judged on `Authorization` alone, so a client that sends only `X-API-Key` gets 401
    `AUTH_MISSING`, with a `hint` saying to send `Authorization: Bearer`; **(B)** its presence
    is `AUTH_AMBIGUOUS`. *Recommendation: A.* The predecessor's clients that still send
    `X-API-Key` get a fatal 401 that tells them what to change (SPEC §7.1), and one that
    sends both keeps working. Either way it is never forwarded. (§2.1, §2.4.)
12. **Unauthenticated test mode.** With no keys and the flag set. Options: **(A)** auth is
    skipped entirely: every route is open, `admin` ones included, and `Authorization` is not
    parsed; **(B)** ambiguous headers are still refused. *Recommendation: A.* It is S1's
    behaviour, so S1's suite and the PPE run unchanged (TD-slice-1 H16 sends two
    `Authorization` headers and expects them stripped, not refused). (§2.1.)
13. **Where the no-keys refusal lives.** Options: **(A)** in `load_settings` (step 7), with every
    other startup refusal, so it exits 2 (REQ-23); several S1 tests that call
    `load_settings({})` then get a key or the flag (Appendix B); **(B)** in `main()` and
    `create_app` only, so `load_settings({})` stays valid and fewer S1 tests change.
    *Recommendation: A.* One place for every refusal, and settings that would be refused can
    never be loaded. REQ-15 says "refuse to start" without an exit code; A gives it REQ-23's
    2. (§2.2, Appendix B.)
14. **The hourly warning's form.** Today's startup warning goes through Python `logging` to
    stderr, as plain text. Options: **(A)** the hourly one is a JSON line on stdout, in the
    request log's format (`event: "unauthenticated"`, `level: WARNING`), first at startup and
    then every hour, beside today's stderr line; **(B)** the same plain stderr line every
    hour. *Recommendation: A.* Operators and S4d read the JSON stream, and a warning that is
    meant to be noticed belongs where the request lines are. (§2.4, Appendix A.)
15. **How auth refusals are logged.** S1 has `outcome: rejected` for its 400, 404 and 405.
    Options: **(A)** `unauthenticated` for both 401s, `forbidden` for the 403, `rejected` for
    the 400, all at `INFO`; **(B)** `rejected` for all of them; **(C)** as A, with
    `AUTH_INVALID` at `WARNING`. *Recommendation: A.* Distinct outcomes answer T15's "who
    tried" and give S4d something to count, without making every stale client a warning.
    (§2.4, §5.)
16. **A PPE case in S2.** The ADR's S2 row names no PPE test. Options: **(A)** none; the A
    tests run in process, and the PPE stays in unauthenticated test mode until S4a;
    **(B)** a PPE case with keys, which needs Q4's A and a key generated by `ppe/ppe up` and
    never committed, and every P test sending it. *Recommendation: A.* The component tests
    see the exact headers Ollama receives (A11), and S4a, the first deployable slice, is
    where the PPE has to run with keys anyway. (§6, §8.)

**Consistency notes (no contradiction found).**
- ADR-001 D6 lists two kinds of ambiguous credentials (more than one `Authorization` header,
  a scheme other than Bearer); SPEC REQ-13 adds a third (whitespace in the value). They don't
  conflict: the TD follows REQ-13's fuller list.
- REQ-15's second bullet needs both keys and the exposure check for a non-loopback listen
  address, and its last sentence says S1 to S3 listen on loopback only except in the PPE. S2
  keeps that (§2.2).

## Appendix A — Data shapes

A 401, as `ErrorFactory.response(AUTH_MISSING, …)` builds it, with its headers:

```text
HTTP/1.1 401 Unauthorized
content-type: application/json
www-authenticate: Bearer
x-seriatim-gate: ungated
x-seriatim-node: node-a
x-request-id: 9f…
```
```json
{"error":"a Bearer key is required","error_code":"AUTH_MISSING","retryable":false,
 "replay_safe":true,"source":"sidecar","node":"node-a"}
```

A 403:

```json
{"error":"this route needs the admin scope","error_code":"FORBIDDEN_SCOPE","retryable":false,
 "replay_safe":true,"source":"sidecar","node":"node-a"}
```

A request line with `client` (TD-slice-1 Appendix A, plus `client` after `known`):

```json
{"ts":"2026-11-23T10:00:00.123Z","level":"INFO","event":"request","request_id":"9f…",
 "method":"POST","path":"/api/pull","kind":"ungated","scope":"admin","known":true,
 "client":"client-a","outcome":"forbidden","status":403,"slot_end":"none","wait_ms":null,
 "hold_ms":null,"bytes_in":0,"bytes_out":131,"node":"node-a"}
```

The hourly warning in unauthenticated test mode (Q14):

```json
{"ts":"2026-11-23T11:00:00.004Z","level":"WARNING","event":"unauthenticated",
 "message":"SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true: the sidecar has no authentication (PPE and test mode only)",
 "node":"node-a"}
```

`keygen`'s output (Q2's recommended form; the key is shown as a placeholder):

```text
seriatim_<43 base64url characters>

[[keys]]
id = "client-a"
scope = "infer"
sha256 = "<64 hex digits>"
```

## Appendix B — What S2 changes in S1's tests and harness

TD-slice-1 is `Enforced: yes`, so every one of its IDs must keep a passing test. S2 changes
some of those tests, never their IDs. Each change is in the PR that causes it (§9):

- **`harness.py` (S2-2).** `make_settings` sets `allow_unauthenticated=true` unless the test
  passes `keys=[…]`, in which case it writes them as `[[keys]]` to a file under a temporary
  directory and sets `SERIATIM_NODE_CONFIG`. So every S1 http test runs in unauthenticated
  test mode, which is S1's behaviour (Q12), and the A tests run with keys. `process_env`
  already sets the flag.
- **`test_config.py` (S2-2).** C1 lists `keys: ()` in the defaults, and its `load_settings`
  call gets the flag. C2 to C11 build on a baseline file with one key wherever they call
  `load_settings` with neither keys nor the flag (Q13), so a refusal they assert is still the
  one they test. C5's "no warning by default" uses the one-key baseline.
- **`test_http.py` (S2-3).** `_log_lines` keeps only `event: "request"` lines, so the hourly
  line (S2-5) is never counted as a request. L1's exact field list gains `client` after
  `known`.
- **`test_gate.py` (S2-3).** K1's `/seriatim/*` rows: 404 and 405 carry scope `INFER`, health
  keeps `NONE` (Q6). K-OAS1's check that the `/seriatim/*` scopes S1 serves agree with
  `classify()` is unchanged: health is still `none`.
- **`test_config.py` (S2-4).** C8 calls `main([])`.
- **TD-slice-1 itself.** Its §2.2 note that `SERIATIM` routes have scope `none`, and the K1,
  C1 and L1 rows, are reworded in S2-3 and S2-2 to match, in the same PRs as the tests (docs go
  with the code).
- **The PPE** (`ppe/compose.yaml`, `tests/behaviour/`) does not change (Q16).
