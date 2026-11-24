# TD — Slice 2: sidecar authentication

**Status:** revision 3, for review. **Date:** 2026-11-23. The design for ADR-001 §10's S2 row.
Revision 1 listed sixteen questions in §10. Revisions 2 and 3 apply two rounds of #95 review
and the maintainer's rulings on #35 (issuecomment-7109174985 and issuecomment-7109496940),
recorded as S2-R1 to S2-R8 in SPEC §9. Every question in §10 is now ruled. Revision 2 also
mapped the 15 scenarios that #96 found missing (§8.2), and added A13 and A14.

Enforced: no

(The `td-test-ids` check, SPEC REQ-63, reads this line. S2's code ships as the series of small
PRs in §9. The **last** one, S2-5, sets it to `yes`. Each earlier PR marks the tests of the IDs
its §9 row gives it with `@pytest.mark.td("<ID>")`.)

**Implements:** SPEC REQ-13, REQ-14, REQ-15 (S2 form: the refusal without keys, the hourly
warning, and the loopback listen address that `ALLOW_UNAUTHENTICATED` no longer relaxes),
REQ-5 (the scope half: "from slice S2, the scope a key needs"), REQ-6 (no `Access-Control-*`
on any response), REQ-22 (the `client` field; a credential is never logged), REQ-23 (the
`keys` setting), and SPEC §6.2's "Generation", "On nodes" and the gitleaks rule.
**Architecture:** ADR-001 D6 (the sidecar half), D7 (standard library only: `hashlib`, `hmac`,
`secrets`, `argparse`), D8, D12 (unknown endpoints need `admin`); threat-model rows T3, T4, T6,
T11 and T15.
**Not in this slice:**
- gateway authentication, the gateway's per-node keys and client-key hashes (REQ-31, S7);
- `/seriatim/status` and its redaction for `infer` keys (REQ-5, REQ-17; S4a). S2 leaves the
  authenticated key in the ASGI scope (§2.4) for S4a to read;
- the admin switch routes and their `changed_by` key id (REQ-19a; S4c);
- a non-loopback listen address with authentication on. REQ-15 also needs the exposure check
  (REQ-19), so that waits for S4a. In S2 the sidecar listens on loopback only, in test mode
  too (S2-R2);
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
| 1 | `node/seriatim_node/auth.py` (new) | `Principal`, `KeyEntry`, `Keyring`, `read_credentials()`, `allows()`, `authorise()` and `new_key()` (S2-1, so every later PR's tests can make keys); `keygen()` (S2-4) |
| 2 | `node/seriatim_node/config.py` | the `keys` setting, its validation, and the REQ-15 refusal (load step 7) |
| 3 | `node/seriatim_node/gate.py` | `classify()` gives `/seriatim/*` 404 and 405 the `admin` scope, the contract's `x-seriatim-unknown-endpoint` rule (§2.3, Q6) |
| 4 | `node/seriatim_node/app.py` | the dispatcher's auth step, the 401/403/400 replies, the principal in the scope, the `client` log field, `main(argv)` with `keygen`, the hourly warning task |
| 5 | `node/seriatim_node/proxy.py` | the `client` field on the proxy's log line; the strip lists are unchanged |
| 6 | `tests/node/test_auth.py` (new) | A1 to A5 |
| 7 | `tests/node/test_http_auth.py` (new) | A6 to A12 |
| 8 | `tests/node/harness.py` | `make_settings(keys=…)`, test mode by default, and `log_lines()`, the shared log reader, which keeps `event: request` lines only (Appendix B) |
| 9 | `tests/node/test_config.py` | the S1 tests' baseline gains a key (Appendix B) |
| 10 | `tests/node/test_http.py` | L1's field list gains `client`; `_log_lines` is replaced by `harness.log_lines` (Appendix B) |
| 11 | `tests/node/test_gate.py` | K1's `/seriatim/*` 404 and 405 rows carry `admin` (S2-R3) |
| 12 | `.env.example` | says that keys are set in the TOML file, never in the environment (S2-R4) |
| 13 | `.gitleaks.toml` (new) | the key-prefix rule (§7, S2-R7), extending gitleaks' default rules |
| 14 | `ppe/compose.yaml` | `node-a` listens on loopback, behind a new forwarder, `fwd-a`, in its namespace; `dbg-a`'s CPU limit drops to 0.15 to make room (§6, S2-R2, S2-R8) |
| 15 | `docs/design/TD-slice-1.md` | the S1 rows whose tests or text S2 changes (C1, C5, C11, K1, L1, §2.1 step 5, §2.2's `/seriatim/*` scope note, §6's PPE lines; Appendix B) |
| 16 | `docs/design/TD-slice-2.md` | the details fixed by the code, and `Enforced: yes` in S2-5 |

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
    def __init__(self, entries: Sequence[KeyEntry]) -> None   # 1..MAX_KEYS entries, unique ids and digests
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

def new_key() -> str                   # Q2; S2-1, used by every test that needs a key
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
4. **The separator** after the scheme must be one or more spaces (Q9; RFC 6750 §2.1's
   `1*SP`). A separator that contains a tab, such as `Bearer<TAB><key>` or
   `Bearer <TAB><key>`, → `Ambiguous("whitespace in the key")`, so steps 3 and 4 agree on
   where the scheme ends. **The token** is what follows the separator. No token (`Bearer`
   alone) → `Missing` (Q10). A token that contains a space, a tab or any other byte below
   `0x21` → `Ambiguous("whitespace in the key")`. So `Bearer a, Bearer b`, the form a
   proxy gives two headers folded into one, is ambiguous.
5. Otherwise → `Presented(token)`, with the token's bytes exactly as received.

Keys are never read from the query string, `Cookie`, `X-API-Key`, `Proxy-Authorization` or
any other header (REQ-13; S2-R6). `X-API-Key` changes nothing but the hint on a 401 (Q11).
A query string is forwarded to Ollama unchanged, as REQ-6 requires, `?key=…` included; the
sidecar logs only the path, never the query (TD-slice-1 L3). REQ-13 records the residual
risk of a key put in a URL.

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
   fails a load balancer's probe, and a degraded node still answers 503 to a caller with no
   key (REQ-13, ADR-001 D6; S2-R5).
3. `read_credentials`: `Ambiguous` → `AMBIGUOUS`; `Missing` → `MISSING`.
4. `keyring.match(token)` is `None` → `INVALID`.
5. `allows(principal.scope, route.scope)` is false → `FORBIDDEN`, with the principal.
6. Otherwise `ALLOW`, with the principal.

So an ambiguous header is refused even when one of its values is a valid key, and a missing or
wrong key is refused before the route's scope is looked at.

**Details fixed by the code (S2-1).** S2-1 adds `auth.py` without `keygen` (S2-4); nothing
calls `authorise` until S2-3.
- **`MAX_KEYS` lives in `auth.py`**, because `Keyring` refuses more than 256 entries and
  `auth.py` cannot import `config.py`. S2-2's `config.py` uses `auth.MAX_KEYS` (§2.2) rather
  than a second constant.
- **`Keyring` also refuses** an entry whose scope is not `INFER` or `ADMIN`, and one whose
  digest is not 32 bytes, with `ValueError`, as it does 0 or 257 entries, a repeated id and a
  repeated digest. Each message names the key id, never a digest. It keeps the entries as a
  tuple, in the order given.
- **`sha256` and `compare_digest` are imported by name** (`from hashlib import sha256`,
  `from hmac import compare_digest`), so A2 patches `auth.sha256` and `auth.compare_digest`
  to count calls without touching `hashlib` or `hmac` for the rest of the process. The loop
  is §2.1's, unchanged.
- **Health, in step 2, is `route == Route(Kind.SERIATIM, Scope.NONE, True, 200)`**, the value
  `classify("GET", b"/seriatim/health")` returns, all four fields compared. A `/seriatim/*`
  404 or 405, and a known `/seriatim/*` route with another scope (as `/seriatim/status` will
  be in S4a), have their credentials read. Until S2-3 gives the 404 and 405 the `admin` scope
  (§2.3), any valid key passes them, since `allows(_, NONE)` is true.
- **Header names are compared exactly** with `b"authorization"` and `b"x-api-key"`: ASGI
  gives them in lower case. Values are not stripped, since h11 already strips them. So a
  hand-built value with leading whitespace has an empty scheme (`a scheme other than
  Bearer`), and one with trailing whitespace has whitespace in the key. A separator that
  contains a tab is ambiguous even with no token after it.
- **The token rule refuses only bytes below `0x21`**, as step 4 says. `0x7F` and bytes from
  `0x80` are part of the token and hashed. The `b64token` form is never checked (§7).
- **The three types.** `Missing` is a frozen dataclass with no fields, so `Missing() ==
  Missing()`. `Missing`, `Ambiguous` and `Presented` all use `slots=True`. The three reasons
  are the constants `TWO_HEADERS`, `OTHER_SCHEME` and `WHITESPACE`.
- **`api_key_seen`** is true when any header is named `x-api-key`, for every verdict of steps
  3 to 6. It is false in test mode and on health, where the headers are not read.
- **`authorise`'s unit tests carry no TD ID.** Its IDs are the http tests of S2-3 (A6 to
  A10). `test_auth.py` tests its six steps directly, so `auth.py`'s mutation score does not
  wait for S2-3.

### 2.2 `config.py`

```python
class KeyConfig(BaseModel):            # one [[keys]] table in the TOML file
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")   # Q3
    scope: Literal["infer", "admin"]
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    model_config = ConfigDict(extra="forbid")

class Settings(BaseSettings):
    ...                                 # every S1 field, unchanged
    keys: tuple[KeyConfig, ...] = ()    # from the TOML file only (S2-R4)

KEYS_VAR = "SERIATIM_NODE_KEYS"
MAX_KEYS = auth.MAX_KEYS                # 256 (Q3); defined in auth.py, which Keyring needs (S2-1)

def keyring(settings: Settings) -> Keyring | None   # None in unauthenticated test mode
```

`load_settings` keeps S1's six steps, adds to two of them, changes step 5 (below) and adds
steps 7 and 8:
- **Step 2 (unknown variables).** `SERIATIM_NODE_KEYS` in the environment →
  `ConfigError("SERIATIM_NODE_KEYS: keys are set in the SERIATIM_NODE_CONFIG file only")`
  (REQ-23; S2-R4). It is a field, so S1's rule alone would accept it.
- **Step 3 (build).** A `ValidationError` inside `keys` names the entry:
  `SERIATIM_NODE_KEYS[<index>].<field>: <err["msg"]>`, built from the first error's `loc` and
  `msg` only, as S1's step 3 does. `str(ValidationError)` is never used, because it includes
  each error's `input_value`. **Ids may be named in errors; hashes never are**: step 7 names
  the ids involved, and no message contains a configured `sha256` or a presented key.
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
- **Step 8 (the startup warning), moved from step 5.** S1's step 5 logs `ALLOW_WARNING` on
  stderr whenever `allow_unauthenticated` is true. The message now depends on whether keys
  are configured, and step 5 runs before step 7 has validated the keys, so the warning moves
  to a new last step, after step 7: when `allow_unauthenticated` is true, `load_settings`
  logs `ALLOW_WARNING` with no keys and `ALLOW_WITH_KEYS_WARNING` with keys (§2.4), then
  returns. Settings that a later step refuses therefore log no warning, only their
  `ConfigError`.

`keyring(settings)` builds the `Keyring` from `settings.keys`, with each `sha256` as
`bytes.fromhex(…)`. With no keys it returns `None`, which only happens in test mode, because
step 7 refused every other case. With keys it returns a `Keyring` whatever
`allow_unauthenticated` says: configured keys are always enforced (S2-R1).

**The listen address (S2-R2).** Step 5 changes: a non-loopback `listen_host` is refused
whatever `ALLOW_UNAUTHENTICATED` says, with or without keys, because REQ-15 needs both keys
and the exposure check (S4a) and the flag never relaxes that. The message becomes
`SERIATIM_NODE_LISTEN_HOST: '<host>' is not a loopback IP literal; a non-loopback address
needs keys and the exposure check (S4a)`. Step 5 no longer logs the startup `WARNING`, which
moves to step 8 (above). The flag's other test-mode relaxations are unchanged (another lock directory, REQ-12b; from S4b the probe fixture, REQ-16). The PPE
therefore reaches its sidecar through container networking (§6).

**Details fixed by the code (S2-2).** S2-2 adds the keys to `config.py`, the startup refusals,
and the PPE layout of §6. Nothing enforces the keys until S2-3.
- **Where an error is.** Step 3 names `loc[0]` as S1 does, then each further part of `loc`:
  an index as `[<i>]`, a name as `.<name>`. So a bad table gives
  `SERIATIM_NODE_KEYS[1].sha256: String should match pattern …`, an unknown field gives
  `SERIATIM_NODE_KEYS[1].<field>: Extra inputs are not permitted`, and `keys = "x"` gives
  `SERIATIM_NODE_KEYS: Input should be a valid tuple`. pydantic's `msg` never holds the
  input, so no message holds a configured `sha256`.
- **Step 2** tests for `SERIATIM_NODE_KEYS` before the unknown-variable rule, so any value,
  valid JSON or not, gets the one message: `SERIATIM_NODE_KEYS: keys are set in the
  SERIATIM_NODE_CONFIG file only`.
- **Step 5's message** drops S1's "needs `ALLOW_UNAUTHENTICATED=true`", as §2.2 says.
- **Step 7's messages** are `SERIATIM_NODE_KEYS: at most 256 keys, not <n>`,
  `SERIATIM_NODE_KEYS: the id '<id>' is repeated`, and `SERIATIM_NODE_KEYS: '<first>' and
  '<repeat>' have the same sha256`, followed by §2.2's no-keys message. The count is checked
  first. Then one pass, in configuration order, compares each key's id and then its digest
  with the keys before it, and reports the first repeat it finds.
- **Constants.** `KEYS_VAR`, `ALLOW_WITH_KEYS_WARNING` and `NO_KEYS` (the no-keys message)
  are in `config.py`, beside `ALLOW_WARNING`. S2-5's hourly task can import both warnings
  from there.
- **`keyring()`** builds each entry with `Scope(key.scope)` and `bytes.fromhex(key.sha256)`.
  Step 7 has already refused repeated ids and digests, so `Keyring`'s own `ValueError` can
  come only from `Settings` built directly, which S2-3's `create_app` check handles.
- **The S2-2 guard** (§9) is in `main()`, in the same `try` as `load_settings`: it raises
  `ConfigError(app.KEYS_NOT_ENFORCED)`, so it prints `seriatim-node: <message>` on stderr and
  exits 2 before the lock is taken. With the flag set, step 8's warning is logged first. Its
  test also checks the load-level half of "keys with the flag": `keyring` gives a `Keyring`,
  and the warning is the "keys are enforced" one. S2-3's A4 case for keys starting takes
  that over when it removes the guard.
- **`harness.make_settings(…, keys=…)`** takes `(id, scope, key)` triples. `key_tables()`
  writes them as `[[keys]]` with each key's SHA-256, as `keygen` will print them. The file is
  in a temporary directory that is removed once the settings are loaded. Overrides are
  applied after the default flag, so a test can still pass `allow_unauthenticated=False`.
- **Where the tests are.** A3 and A4 are in `test_auth.py`, as §1 says, with their own copy
  of `test_config.py`'s `node_env` fixture. `test_config.py`'s one-key baseline is a
  module-scoped file. `load()` and `refused()` add it unless a test names its own
  `SERIATIM_NODE_CONFIG`. C5's warning case uses a loopback host with the flag now, because a
  refused host logs no warning.
- **`tests/tools/test_ppe_guard.py`** is changed as well, though §9's row doesn't list it.
  Its committed-compose test asserts the service set and `node-a`'s listen address, so it now
  checks `fwd-a` (its namespace, `cap_drop` and `socat` command) and `node-a` on
  `127.0.0.1:11437`. The guard itself is unchanged, and it accepts the new file (4.0 CPUs,
  5,952 MiB).

### 2.3 `gate.py`

`classify()` is unchanged for every Ollama route. For `/seriatim/*`, the scope it returns
becomes the scope the request needs (REQ-5, "Their scopes are fixed by the contract"):
- `GET /seriatim/health` → scope `NONE` (the contract's `none`), as in S1;
- an unknown `/seriatim/*` path (404), and a known one with the wrong method (405), → scope
  `ADMIN`. That is the contract's rule for every method and path pair it doesn't list,
  `x-seriatim-unknown-endpoint: { x-seriatim-gate: gated, x-seriatim-scope: admin }`
  (`contract/openapi.yaml`, lines 525–526), which REQ-5's "everything else … needs the
  `admin` scope" states too. So an `admin` key gets the 404 or 405, and an `infer` key gets
  403 `FORBIDDEN_SCOPE`: the answer it gets from `/seriatim/admin/*` once S4c serves those
  routes, and before. That includes `POST` and `HEAD` on `/seriatim/health`. S4a and S4c add
  their routes to `SERIATIM_ROUTES` with their contract scopes (`/seriatim/status`: `infer`;
  `/seriatim/admin/*`: `admin`). The maintainer confirmed that the rule covers `/seriatim/*`
  (S2-R3).

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

**The hourly warning (REQ-15; S2-R1).** Whenever `ALLOW_UNAUTHENTICATED=true`, with or
without keys, the lifespan starts one task, in a task group that the lifespan cancels on
shutdown:

```python
async def _warn_allow_unauthenticated(self) -> None:
    message = ALLOW_WARNING if self.keyring is None else ALLOW_WITH_KEYS_WARNING
    while True:
        self.log.event("allow_unauthenticated", level="WARNING", message=message)
        await sleep(self.warn_every_s)  # 3600; create_app(…, warn_every_s=…) in tests
```

`sleep` is `app`'s own binding (`from anyio import sleep`), so A12 can patch
`seriatim_node.app.sleep` without touching `anyio.sleep`, which the rest of the sidecar and
the test harness use.

- **The message says which mode it is.** With no keys it is S1's `ALLOW_WARNING` ("…: the
  sidecar has no authentication (PPE and test mode only)"). With keys it is
  `ALLOW_WITH_KEYS_WARNING`: `SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true: test mode; keys are
  enforced (PPE and test mode only)`, because saying "no authentication" would be false.
  `load_settings`'s startup line on stderr, now written by its step 8 (§2.2), picks the same
  message by the same rule.
- The first line is written when the lifespan starts, then one every hour (Q14), beside the
  stderr line of step 8.
- `RequestLog.event(name, *, level, **fields)` writes the same JSON line format as `emit`,
  with `event` set to `name` and no request fields (Appendix A). `emit` becomes
  `event("request", …)`.
- `warn_every_s` is a `create_app` argument, not a setting, so a deployment can't change it
  (REQ-23 has no key for it).
- With `allow_unauthenticated` false there is no task; with it true there is always one, keys
  or not (S2-R1).
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
- **No CORS (REQ-6; S2-R7).** `downstream_headers` already drops every `access-control-*`
  header from Ollama's responses (TD-slice-1 §4), and `ErrorFactory` and health never add
  one. S2 adds A14, which checks every response class for it, the auth refusals included.

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
| `GET /seriatim/health` | none | passes | passes (not read, S2-R5) | passes | passes |
| other `/seriatim/*` (404, 405) | `admin` (the contract's unknown-endpoint rule; S2-R3) | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | 404 / 405 |
| ungated, `infer`: `GET /`, `HEAD /`, `GET /api/tags`, `GET /api/ps`, `GET /api/version`, `POST /api/show`, `GET /v1/models` | `infer` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | passes | passes |
| ungated, `admin`: `POST /api/pull`, `POST /api/push`, `POST /api/copy`, `DELETE /api/delete`, `HEAD`/`POST /api/blobs/sha256:<64 hex>` | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| gated, `infer`: `POST /api/generate`, `/api/chat`, `/api/embed`, `/api/embeddings`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | `infer` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | passes | passes |
| gated, `admin`, known: `POST /api/create` | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| gated, `admin`, unknown: every other method and path, `OPTIONS` included | `admin` | 401 `AUTH_MISSING` | 401 `AUTH_INVALID` | 403 `FORBIDDEN_SCOPE` | passes |
| *S4a:* `GET /seriatim/status` | `infer` | 401 | 401 | passes (redacted) | passes |
| *S4c:* `POST /seriatim/admin/offline`, `.../online` | `admin` | 401 | 401 | 403 | passes |

An ambiguous `Authorization` gets 400 `AUTH_AMBIGUOUS` on every row but health. In
unauthenticated test mode (no keys), every row passes, the admin rows included (Q12).

**Exact matching still decides the row** (REQ-5; TD-slice-1 §2.2). `GET /seriatim/health/`
starts with `/seriatim/` but is not a listed path, so it is the second row (needs `admin`):
401 with no key, 403 with an `infer` key, 404 with an `admin` key. `GET /SERIATIM/HEALTH` is
not a `/seriatim/` path at all (K6): it is gated, `admin`, unknown, so 401 with no key, 403
with an `infer` key, and forwarded to Ollama with an `admin` key. `GET /api/tags/` and
`POST /API/chat` are unknown pairs too, so 403 with an `infer` key.

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

**The keys** are a TOML array of tables in the file named by `SERIATIM_NODE_CONFIG`, and only
there (REQ-23; S2-R4), one table per key, as `keygen` prints them (§7):

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
| `keys` | 0 to 256 tables (Q3), only in the TOML file; `SERIATIM_NODE_KEYS` in the environment → exit 2 (S2-R4) |
| `keys[].id` | `^[a-z0-9][a-z0-9-]{0,31}$`, unique (Q3) |
| `keys[].scope` | `infer` or `admin` |
| `keys[].sha256` | 64 hex digits, either case; unique as bytes |
| any other key in a table | exit 2, naming it |

**The three modes.** Whether the sidecar authenticates follows from the keys alone:

| keys | `ALLOW_UNAUTHENTICATED` | result |
|---|---|---|
| none | `false` | refuses to start, exit 2 (REQ-15) |
| none | `true` | **unauthenticated test mode**: no request is authenticated; loopback listen only (S2-R2); the startup `WARNING` on stderr, and a `WARNING` line every hour (§2.4) |
| some | `false` | authenticated; loopback listen only (REQ-15, until S4a) |
| some | `true` | authenticated (S2-R1); loopback listen only (S2-R2); the other test-mode relaxations still apply (another lock directory, from S4b the probe fixture); the startup `WARNING` and the hourly line too, with the "keys are enforced" message (§2.4, S2-R1) |

The flag's name says "unauthenticated", but it is also S1's test-mode switch (REQ-12b,
REQ-16). The rulings keep it as it is; renaming it is not part of S2.

**Rotation** (SPEC §6.2): add the new key's table, restart the sidecar, move the client over,
remove the old table, restart again. Several keys can be valid at once.

**The PPE (S2-R2).** It stays in unauthenticated test mode (`ALLOW_UNAUTHENTICATED=true`, no
keys; Q16), but S2-R2 breaks its current layout. S1-14's `node-a` sets
`SERIATIM_NODE_LISTEN_HOST: 0.0.0.0` and `LISTEN_PORT: "11436"` in `ollama-a`'s network
namespace, and `ollama-a` publishes `127.0.0.1:11436:11436` on the host (`ppe/compose.yaml`).
Docker forwards a published port to the container's own interface, not to its loopback, so
the sidecar has to listen beyond loopback to be reached, and S1's step 5 allowed that only
because the flag was set. Once S2-R2's step 5 lands, that `node-a` would exit 2 at startup and
P1 to P7 would fail. So the same PR, S2-2, changes the PPE to use container networking, the
way `dbg-a` already reaches Ollama (ruled in S2-R8):
- `node-a` listens on `127.0.0.1` inside the namespace, on another port (`11437`);
- a new forwarder service, `fwd-a`, in the same namespace and built like `dbg-a` (the same
  pinned image, `network_mode: "service:ollama-a"`, `cap_drop: [ALL]`), runs
  `socat TCP-LISTEN:11436,fork,reuseaddr TCP:127.0.0.1:11437`;
- the published port, `127.0.0.1:11436` on the host, and every P test are unchanged.

**The resource split.** `tools/ppe_guard.py` refuses limits that sum above `MAX_CPUS = 4.0`
or `MAX_MEMORY = 6 GiB`. Today's sums are exactly 4.0 CPUs and 5.75 GiB:

| service | CPUs now | CPUs in S2-2 | memory now | memory in S2-2 |
|---|---|---|---|---|
| `ollama-a` | 3 | 3 | 5G | 5G |
| `cap-a` | 0.25 | 0.25 | 256M | 256M |
| `dbg-a` | 0.25 | **0.15** | 128M | 128M |
| `fwd-a` (new) | — | **0.1** | — | **64M** |
| `node-a` | 0.5 | 0.5 | 384M | 384M |
| **sum** | **4.0** | **4.0** | **5.75 GiB** (5,888 MiB) | **5.8125 GiB** (5,952 MiB) |

A forwarder at `dbg-a`'s 0.25 would make 4.25, which the guard refuses. The 0.1 for `fwd-a`
comes from `dbg-a`, not from `node-a`: both forwarders are `socat` copying bytes for one
small model, while `node-a` is the code under test, and P1 to P7's timing margins were set
with its 0.5. Folding the forwarder into `dbg-a` as a second listener would add no container,
but one `socat` serves one listener, so `dbg-a` would need a shell running two `socat`s, and
if one died the container would stay up half-broken; as its own service, each forwarder is
its container's only process and fails visibly. S2-2's `ppe` job quotes the guard's
acceptance of the new file.

The PPE guard already accepts `dbg-a`'s shape, so `fwd-a` needs no guard change. P7
already allows for a forwarder that accepts while `node-a` is down ("the host's port forwarder
accepts and resets"; it waits for an answer, not for a listening port), so it still holds. The
sidecar then sees every PPE client as `127.0.0.1`, which nothing in S1 to S3 reads.
TD-slice-1 §6's PPE lines and C5, which say the PPE listens on `0.0.0.0` with the flag, are
reworded in S2-2 (Appendix B).

## 7. `keygen`

```text
seriatim-node keygen --id <id> --scope infer|admin
```

- **Arguments.** Both are required. `--id` follows the key-id rule of §6; `--scope` is `infer`
  or `admin`. A missing or invalid argument, or any other argument, prints argparse's usage on
  stderr and exits 2. argparse's default `store` action keeps the last of a repeated option,
  so both options use a small `store` action, `Once`, that calls `parser.error(…)` when its
  option has already been given: `--id a --id b` and `--scope infer --scope admin` also exit 2
  with usage, never with the last value.
- **The key.** `new_key()` (in `auth.py` from S2-1): `seriatim_` followed by
  `secrets.token_urlsafe(32)`, the 43-character unpadded base64url form of 32 random bytes
  (Q2, ruled B in S2-R8). The prefix lets a secret scanner recognise a leaked key.
- **The gitleaks rule (S2-R7, S2-R8).** The repository has no gitleaks configuration today: the
  pre-commit hook (`.pre-commit-config.yaml`, gitleaks v8.21.2) and the `secret-scan`
  workflow (`gitleaks git --redact --verbose --exit-code 1 .`) both run gitleaks' default
  rules. gitleaks v8 reads a `.gitleaks.toml` at the root of the scanned repository when no
  `--config` is given, so S2-4, the PR that adds `keygen`, adds that file with no workflow
  change:

  ```toml
  [extend]
  useDefault = true

  [[rules]]
  id = "seriatim-node-key"
  description = "A Seriatim sidecar key (TD-slice-2 §7)"
  regex = '''(?:^|[^A-Za-z0-9_-])(seriatim_[A-Za-z0-9_-]{43})(?:[^A-Za-z0-9_-]|$)'''
  secretGroup = 1
  entropy = 3.5
  keywords = ["seriatim_"]
  ```

  The boundaries are explicit character classes, not `\b`, because a key may end in `-`,
  where `\b` would not match before a newline. The entropy floor keeps identifiers such as `seriatim_node` (and longer `snake_case` names
  that happen to be 43 characters after the prefix) from matching. The repository has no
  test of its gitleaks configuration, so S2-4 adds none; its PR shows the rule working by
  running gitleaks on a scratch directory outside the repository that holds one `new_key()`
  output (the check fails) and one 43-character identifier (it passes). The regex is exactly
  `new_key()`'s form.
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
| A1 | `read_credentials`, table-driven: no header → `Missing`; `Authorization:` with an empty value → `Missing`; `Bearer` alone → `Missing`; `Bearer <key>` → `Presented` with the exact bytes; `bearer <key>` and `BEARER <key>` → `Presented` (Q8); `Bearer  <key>` (two spaces) → `Presented` (Q9); two `Authorization` headers, equal and different, one of them valid → `Ambiguous("two Authorization headers")`; `Basic <b64>`, `Digest x`, `Token <key>` → `Ambiguous("a scheme other than Bearer")`; `Bearer a b`, `Bearer a\tb`, `Bearer a, Bearer b`, `Bearer\t<key>` and `Bearer \t<key>` (a tab in the separator) → `Ambiguous("whitespace in the key")`. `X-API-Key: <key>` alone, `Cookie: key=<key>`, `Proxy-Authorization: Bearer <key>` alone, and a key only in the query string → `Missing` (S2-R6). Hypothesis: any token of `b64token` characters round-trips to `Presented` with the same bytes, and any token with an embedded space or tab is `Ambiguous` | unit | 13 |
| A2 | `Keyring.match` and `allows`. With 5 keys, `hmac.compare_digest` (patched in `auth`, counting) is called exactly 5 times when the key matches the first entry, the third, the last, and none; SHA-256 is computed once per call. The matching entry's `Principal` (id and scope) is returned; a key differing in one character, the key's hex hash sent as the key, and an empty token → `None`. `allows` for every (have, need) pair of `INFER`/`ADMIN` × `NONE`/`INFER`/`ADMIN`: only `INFER` needing `ADMIN` is false. `Keyring` refuses 0 entries, 257 entries, a repeated id and a repeated digest (`ValueError`). `new_key()` (S2-1): `seriatim_` followed by 43 base64url characters that decode to 32 bytes, and 100 calls give 100 different keys | unit | 13, 14, §6.2 |
| A3 | Loading keys. A config file with an `infer` and an `admin` key → `settings.keys` in order, and `keyring(settings)` matches each key with its id and scope; `sha256` in upper case is accepted and equal to the lower-case one. Each of these → `ConfigError` naming `SERIATIM_NODE_KEYS[<i>].<field>` or `SERIATIM_NODE_KEYS`: a table with no `id`, `scope` or `sha256`; an unknown field; `scope = "read"`; an id `Client`, `-a`, 33 characters or empty; a `sha256` of 63 or 65 digits, or with a non-hex digit; `keys = "x"`; 257 tables; two tables with one id (the message names the id); two tables with one hash in different cases (the message names both ids). `SERIATIM_NODE_KEYS` set in the environment → `ConfigError` naming it, even when it is valid JSON (REQ-23; S2-R4). No error message contains a configured `sha256` in either case, including the messages for a malformed `sha256` and a duplicate one | unit | 23, 13 |
| A4 | Startup refusals (REQ-15). No keys and no `ALLOW_UNAUTHENTICATED` → `ConfigError` naming `SERIATIM_NODE_KEYS`, and `main()` exits 2 with that message on stderr and never calls `uvicorn.run`. No keys with `ALLOW_UNAUTHENTICATED=true` → starts, `keyring` is `None`, the startup `WARNING` is captured. Keys with `ALLOW_UNAUTHENTICATED=true` → starts with a `Keyring`, and the startup `WARNING` is the "keys are enforced" one (S2-R1). `LISTEN_HOST` = `0.0.0.0`, `::` or `192.0.2.10` → `ConfigError` naming `SERIATIM_NODE_LISTEN_HOST` in all four combinations of keys or none, and the flag set or not (S2-R2: the flag never relaxes it); `main()` then exits 2 without calling `uvicorn.run`. `create_app` given `Settings(keys=(), allow_unauthenticated=False)` built directly → `ConfigError` | process | 15, 23 |
| A5 | `keygen`. `main(["keygen", "--id", "client-a", "--scope", "infer"])` exits 0 and prints the key, a blank line and the table; the key has the Q2 form and decodes to 32 bytes; the table's `sha256` is the SHA-256 of the printed key; written to a config file, the table loads (A3) and the sidecar built from it accepts the printed key with id `client-a` and scope `infer` (one http request). Two runs print different keys. It succeeds with an invalid `SERIATIM_NODE_SLOTS` and an unreadable `SERIATIM_NODE_CONFIG` in the environment (it reads neither), creates no file in the working directory or the lock directory, and calls neither `acquire_singleton` nor `uvicorn.run` (patched). `--scope read`, `--id Bad`, a missing `--id`, a repeated `--id` (`--id a --id b`), a repeated `--scope` (even with the same value), an extra argument, `main(["keygen"])` and `main(["nope"])` each exit 2 with usage on stderr and print no key. `main([])` serves as in S1 (TD-slice-1 C8) | process | §6.2, 23 |
| A6 | 401 across every route class. With keys configured, for one pair of each class in §4 (every pair of the REQ-5 lists, from `tests/fixtures/route-pairs.json`, plus `POST /api/create`, `OPTIONS /api/tags`, `PUT /api/chat`, `POST /api/new-feature`, `GET /seriatim/nope` and `POST /seriatim/health`): no key → 401 `AUTH_MISSING`; a key that is not configured → 401 `AUTH_INVALID`. Each: `WWW-Authenticate: Bearer` exactly once, `retryable: false`, `replay_safe: true`, `X-Seriatim-Gate: ungated`, the body validates against `ErrorEnvelope` (K-OAS2); `fake_ollama` recorded nothing; every slot is `FREE`. **HEAD requests** (`HEAD /`, `HEAD /api/blobs/sha256:<hex>`, and any other HEAD pair) get no body (RFC 9110 §9.3.2), so for them the test asserts the response status, `WWW-Authenticate` (on a 401) and `X-Seriatim-Gate: ungated`, that the body is empty, and the request log line's `status` and `outcome` (`unauthenticated` for a 401, `forbidden` for a 403). The log line has no `error_code` field, and both 401s log the same status and outcome, so `error_code`, and with it `AUTH_MISSING` against `AUTH_INVALID`, is asserted only on the non-HEAD cases. A7, A8 and A14 treat HEAD the same way | http | 13, 25 |
| A7 | The scope matrix of §4, for the same pairs as A6: an `infer` key → the `infer` classes pass (the gated ones get Ollama's answer with `X-Seriatim-Gate: acquired`, the ungated ones `ungated`), and every `admin` class gets 403 `FORBIDDEN_SCOPE`, including unknown endpoints, `/seriatim/nope`, `POST /seriatim/health`, `POST /seriatim/admin/offline` and `POST /seriatim/admin/online` (not yet served, so unknown `/seriatim/*` paths, S2-R3), `GET /seriatim/admin/offline` (a wrong method there), and §4's exact-matching cases `GET /seriatim/health/`, `GET /SERIATIM/HEALTH`, `GET /api/tags/` and `POST /API/chat`. Each 403 has no `WWW-Authenticate`, `error_code: FORBIDDEN_SCOPE`, `retryable: false`, `replay_safe: true`, `source: sidecar`, `node` equal to `settings.id`, `X-Seriatim-Gate: ungated`, and the body validates against `ErrorEnvelope` (K-OAS2); nothing recorded upstream and no slot taken. An `admin` key → every class passes; `/seriatim/nope`, `/seriatim/admin/offline` and `GET /seriatim/health/` get 404, `POST /seriatim/health` gets 405, and `GET /SERIATIM/HEALTH` is forwarded to Ollama. A 403's log line has `client` equal to the `infer` key's id. (Once S4c serves the admin switch, its own tests show an `infer` key's 403 there; these cases keep passing, because the route's scope stays `admin`.) | http | 14, 5, 25 |
| A8 | Health. With keys configured, `GET /seriatim/health` with no key, a wrong key, two `Authorization` headers and `Basic x` → 200 `{"status":"ok"}` each time (S2-R5); its line is still at `DEBUG` with `client: null` (TD-slice-1 HL9). Degraded health with no key: Ollama down, and a new gate in its startup quarantine → 503 with `["ollama_unreachable"]` and `["starting"]`, never 401. `HEAD /seriatim/health` and `POST /seriatim/health` with no key → 401 `AUTH_MISSING` (for `HEAD`, as A6's HEAD rule: the response status, headers and empty body, and the log line's `status` 401 and `outcome` `unauthenticated`; `error_code` only on the `POST`); with an `infer` key → 403 `FORBIDDEN_SCOPE`; with an `admin` key → 405 with `Allow: GET` (S2-R3) | http | 13, 18 |
| A9 | Ambiguous credentials over HTTP (raw socket where httpx would merge headers): two `Authorization` headers, one of them a valid `admin` key; `Basic <b64>`; `Bearer <key> x`; `Bearer a, Bearer b` → 400 `AUTH_AMBIGUOUS` on a gated, an ungated and a `/seriatim/*` route: `error_code: AUTH_AMBIGUOUS`, `retryable: false`, `replay_safe: true`, `source: sidecar`, `node` equal to `settings.id`, `X-Seriatim-Gate: ungated`, and the body validates against `ErrorEnvelope` (K-OAS2); no `WWW-Authenticate`; nothing recorded upstream; no slot taken; the message names the reason and contains no header value. A valid key in `X-API-Key` with no `Authorization` → 401 `AUTH_MISSING` with the `X-API-Key` hint (Q11); with a valid Bearer key as well → passes, and Ollama receives neither header. `?key=<valid key>`, `Cookie: key=<valid key>` and `Proxy-Authorization: Bearer <valid key>`, each with no `Authorization` → 401 `AUTH_MISSING`, and `fake_ollama` recorded nothing (S2-R6) | http | 13, 25 |
| A10 | Auth comes first (§3). With no key: a gated request while the only slot is held → 401, never 429; during the startup quarantine → 401, never 503 `NODE_STARTING`; with `Content-Length` over `max_body_bytes` → 401, never 413; in each case the slot holder is undisturbed and the gate state is unchanged. Over a raw socket, the headers of a `POST /api/chat` with `Content-Length: 4096` and no body bytes → the 401 arrives before any body is sent, with `Connection: close`, and the connection is closed; an `Expect: 100-continue` request gets its 401 with no `100 Continue`. A 401 to a `GET` with no body keeps the connection, and the next request on it with a valid key gets 200. S1's earlier checks still win: HTTP/1.0 → 505, `/api/../chat` → 400 `BAD_PATH`, `Transfer-Encoding` with `Content-Length` → 400 `BAD_REQUEST_BODY`, each with no key (Q5) | http | 13, 3, 20, 20a |
| A11 | The key never leaves. With a valid `infer` key on a gated `/api/chat` (non-streaming and streaming) and on `/api/tags`, and a valid `admin` key on a streamed `POST /api/blobs/sha256:<hex>`, each also sending `X-API-Key`, `Cookie` and `Proxy-Authorization` → `fake_ollama` recorded none of `Authorization`, `X-API-Key`, `Cookie` or `Proxy-Authorization`. Across one request of every outcome in §5 and TD-slice-1 §5 that S2 can reach (200, 400 `AUTH_AMBIGUOUS`, 401 both ways, 403, 404, 413, 429, 502), no log line, response body or response header contains the key, its SHA-256 in hex, or the configured hash of any other key. The same holds for credentials that are refused or ignored, each its own marker secret: a wrong key (`Bearer <secret>`, 401 `AUTH_INVALID`); `Basic <secret>` (400); `Bearer <secret> x` (400); and a secret sent only in `X-API-Key`, only in `Cookie`, and only in the query string (`?key=<secret>`), each with no `Authorization` → no log line (traceback included) and no response body or header contains the secret. This asserts only that nothing is logged or echoed; how a query-string key is answered is A9's. An authenticated `GET /api/tags?key=<secret>` (valid Bearer `infer` key) → 200: `fake_ollama` receives the query string `key=<secret>` byte for byte (REQ-6 forwards it unchanged; the residual risk REQ-13 records), and no log line contains `<secret>` (S2-R6). `client` is the key's id on the authenticated lines (the 403 included) and `null` on the 400 and 401 lines. Each auth refusal's line has §2.4's `outcome` and `level`: `unauthenticated` for both 401s, `forbidden` for the 403 and `rejected` for the 400, each at `INFO`. `uvicorn`'s access log stays off (TD-slice-1 L3) | http | 13, 22 |
| A12 | Unauthenticated test mode and the hourly warning. With no keys and `ALLOW_UNAUTHENTICATED=true`: `POST /api/create` and `OPTIONS /api/tags` with no key reach Ollama (every route is open, Q12); a request with two `Authorization` headers is not refused, and Ollama receives neither; `client` is `null`. With `create_app(…, warn_every_s=0.2)`: one `WARNING` line with `event: "allow_unauthenticated"` and S1's "no authentication" message at startup, and further ones at that interval (at least two more within a bounded wait, each at least the interval apart); none after shutdown. With keys and `ALLOW_UNAUTHENTICATED=true` → the same lines, with the "keys are enforced" message (S2-R1). With keys and no flag → none at all. The line has the fields in Appendix A, `level: WARNING`, and no request fields. Without `warn_every_s`, `create_app` uses 3600 s: with `seriatim_node.app.sleep` (`app`'s own binding, §2.4; `anyio.sleep` itself is untouched) patched to a fake that records its argument and then `await anyio.sleep_forever()`, so the loop suspends until the lifespan cancels it instead of spinning, the recorded first argument is exactly 3600 and exactly one warning line was written | http | 15 |
| A13 | Rotation (SPEC §6.2). A config file with two `infer` keys, `old` and `new` → each gets 200 on `/api/tags`, each logged with its own `client`. The same sidecar stopped, the `old` table removed, and a sidecar started from the edited file → `old` gets 401 `AUTH_INVALID` and `new` still gets 200 | http | 13, §6.2 |
| A14 | No CORS on any response (REQ-6; S2-R7). With keys configured, and `fake_ollama` answering `/api/tags` and `/api/chat` with `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Credentials: true` and `Access-Control-Expose-Headers: x` → the client receives no `access-control-*` header (an `infer` key; Ollama's other headers kept). Every Seriatim-made response, each sent with an `Origin: http://example.com` header, has none either: 400 `AUTH_AMBIGUOUS`, 401 both ways, 403, a preflight `OPTIONS /api/chat` with `Access-Control-Request-Method: POST` and no key (401), health 200 and 503, 404, 405, 413, 429 and 503 `NODE_STARTING` | http | 6, 13 |

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

### 8.2 The #96 scenarios

#96 checked revision 1 against the S2 scenarios and found 15 missing (M1 to M15). Each is now
a case of the ID named here; none is dropped.

| M# | TD ID | reason |
|---|---|---|
| M1 | A7 | An `infer` key on `POST /seriatim/admin/offline` and `.../online`, and on `GET /seriatim/admin/offline` (a wrong method), → 403 (S2-R3: unknown `/seriatim/*` paths need `admin`). After S4c the same cases still give 403, because those routes are `admin` in the contract; S4c's own tests cover them as served routes |
| M2 | A7, A9 | The 403 (A7) and the 400 `AUTH_AMBIGUOUS` (A9) each assert `ErrorEnvelope`, `retryable: false`, `replay_safe: true`, `source: sidecar` and `X-Seriatim-Gate: ungated` (REQ-25) |
| M3 | A12 | Keys with the flag set → the hourly `WARNING` still fires, with the "keys are enforced" message (S2-R1) |
| M4 | A4 | A non-loopback `LISTEN_HOST` is refused with the flag and no keys, and in every other combination (S2-R2) |
| M5 | A11 | A wrong key, `Basic <secret>`, `Bearer <secret> x`, and a secret only in `X-API-Key`, `Cookie` or the query string: never in a log line, body or header |
| M6 | A11 | An authenticated request with `?key=<secret>`: Ollama receives the query string unchanged, because REQ-6 forwards it byte for byte (REQ-13 records the risk); the log never contains it (S2-R6) |
| M7 | A11 | Each auth refusal's line: `outcome` `unauthenticated`, `forbidden` or `rejected`, at `INFO` |
| M8 | A12 | `create_app` without `warn_every_s` → the task sleeps 3600 s |
| M9 | A5 | A repeated `--id` or `--scope` → exit 2, no key printed |
| M10 | A1 | `Bearer<TAB><key>` and `Bearer <TAB><key>` → `Ambiguous("whitespace in the key")` |
| M11 | A1, A9 | `Proxy-Authorization: Bearer <key>` alone → `Missing` (A1), so 401 `AUTH_MISSING` with nothing sent to Ollama (A9) |
| M12 | A6, A7 | With no key, `GET /seriatim/health/` and `GET /SERIATIM/HEALTH` → 401 (A6's every-class rule); with an `infer` key, those two, `GET /api/tags/` and `POST /API/chat` → 403 (A7). Each expected result matches REQ-5 and `classify()`: the first is an unknown `/seriatim/*` path, the other three are unknown pairs under rule 5 (§4) |
| M13 | A8 | Degraded health with no key (Ollama down; the startup quarantine) → 503, never 401 |
| M14 | A13 (new) | Rotation: two keys both work; one removed and the sidecar restarted → it gets 401 `AUTH_INVALID`, the other 200 |
| M15 | A14 (new) | An upstream `Access-Control-*` header, and every Seriatim response (the 401 and 403 included) → none reaches the client (REQ-6, S2-R7) |

## 9. Delivery: the S2 PR series

Five small PRs, in this order. Each has its own issue, a sub-issue of #35. Each leaves `main`
releasable and green, and ships the tests for exactly the IDs listed.

| PR | scope | files (new or changed) | TD IDs | needs |
|---|---|---|---|---|
| S2-1 | Credentials and the keyring | `auth.py` (`Principal`, `KeyEntry`, `Keyring`, `read_credentials`, `allows`, `authorise`, `new_key`), `tests/node/test_auth.py` | A1, A2 | — |
| S2-2 | Keys in the configuration, and the startup refusals | `config.py` (`KeyConfig`, `keys`, steps 2, 3, 5, 7 and 8, `keyring()`), `app.py` (`main()` refuses configured keys until S2-3; below), `test_auth.py`, `test_config.py` and `harness.py` (Appendix B), `.env.example`, `ppe/compose.yaml` (`node-a` on loopback behind `fwd-a`, and the resource split; S2-R2, S2-R8; §6), `TD-slice-1.md` (C1, C5, C11, §2.1 step 5, §6's PPE lines) | A3, A4 (all but its `create_app` case and its case of keys starting) | 1 |
| S2-3 | Enforcement | `app.py` (the auth step, `refuse_auth`, `PRINCIPAL_KEY`, the `client` field, the `create_app` refusal; S2-2's guard removed), `proxy.py` (`client`), `gate.py` (S2-R3), `tests/node/test_http_auth.py` (new), `test_http.py`, `test_gate.py` and `harness.py` (Appendix B), `TD-slice-1.md` (K1, L1, §2.2) | A4 (its `create_app` case and keys starting), A6–A11, A13, A14 | 2 |
| S2-4 | `keygen` and the gitleaks rule | `auth.py` (`keygen`; `new_key` is S2-1's), `app.py` (`main(argv)`), `test_auth.py`, `test_config.py` (C8 calls `main([])`), `.gitleaks.toml` (new; §7, S2-R7, S2-R8) | A5 | 2, 3 |
| S2-5 | Test mode's hourly warning, and enforcement | `app.py` (the warning task, `RequestLog.event`, `warn_every_s`), `test_http_auth.py`, `docs/design/TD-slice-2.md` (`Enforced: yes`, the details fixed by the code) | A12 | 1–4 (all) |

- **S2-2 never serves with keys it doesn't check.** After S2-2, `load_settings` loads and
  validates `[[keys]]`, but nothing enforces them until S2-3. So S2-2's `main()` refuses to
  start, with exit 2 and the message `SERIATIM_NODE_KEYS: keys are configured, but this build
  does not enforce them yet (TD-slice-2 S2-3)`, whenever `settings.keys` is not empty. A
  sidecar built from S2-2 therefore either runs in unauthenticated test mode, with its
  startup `WARNING`, or doesn't start: it never serves unauthenticated while keys are
  configured. S2-2 tests the guard with a test that carries no TD ID; S2-3 removes the guard
  and that test together, in the PR that adds enforcement, and A4's "keys … starts" case moves
  to S2-3 with it. No TD ID depends on the guard. This keeps each PR small: merging S2-2 and
  S2-3 instead would make one PR of about 13 files and the largest review of the slice.
- **S2-3 and S2-4 both change `app.py`,** so they go one at a time. S2-4 needs S2-2, because
  A5 loads the printed table through `load_settings`.
- **S2-5 needs every earlier PR,** because it sets `Enforced: yes`, so all of A1 to A14 must
  pass. It closes #35.
- **S2-2 changes the PPE.** S2-R2 makes step 5 refuse `node-a`'s current `0.0.0.0` listen
  address, so the compose change of §6 lands in the same PR as the step-5 change, and S2-2's
  `ppe` job shows P1 to P7 passing through the forwarder.
- **Docs with code.** Each PR updates this TD's "details fixed by the code" for what it built,
  and the SPEC or ADR where the code refines them.

## 10. Questions, all ruled

Each was put to the maintainer on #35. Q1, Q4, Q6 and Q7 are **ruled**
(issuecomment-7109174985), and so are three points raised in the #95 review: REQ-15's PPE
exception, credentials in query strings, and T11 and T3. SPEC §9 records all seven as S2-R1
to S2-R7, with where each is applied, and the design above follows them. A second ruling
(issuecomment-7109496940), S2-R8, gives Q2, Q3, Q5 and Q8 to Q16 their recommended answers,
confirms the PPE layout of §6 (`node-a` on loopback behind a forwarder, within the guard's
4-CPU cap, shipped in S2), and confirms the `.gitleaks.toml` of §7. So every question is
ruled, and each is marked below.

| ruling | outcome | TD sections |
|---|---|---|
| S2-R1 (Q4) | Configured keys are always enforced; the hourly `WARNING` fires whenever `ALLOW_UNAUTHENTICATED` is set, keys or not | §2.2, §2.4, §6, A4, A12 |
| S2-R2 (REQ-15) | The flag still forces a loopback listen address; the PPE uses container networking | §2.2, §6, §9 (S2-2), A4 |
| S2-R3 (Q6) | Unknown `/seriatim/*` paths and wrong methods need `admin`, as the contract says | §2.3, §4, A7, A8 |
| S2-R4 (Q1) | Keys in the file only, no environment form; REQ-23 amended | §2.2, §6, A3 |
| S2-R5 (Q7) | Health reads no credentials; REQ-13 and ADR-001 D6 amended | §2.1, A8 |
| S2-R6 | A credential comes from headers only; the URL residual risk is recorded (REQ-13, T3); query values never reach the log | §2.1, A1, A9, A11 |
| S2-R7 | No `Access-Control-*` on any sidecar response (REQ-6); T11's exceptions corrected; a gitleaks rule for the key prefix in S2 | §2.5, §7, §9 (S2-4), A14 |
| S2-R8 (Q2, Q3, Q5, Q8–Q16; the PPE; gitleaks) | Each of those questions takes its recommended answer. The PPE: `node-a` binds `127.0.0.1` and a `socat` forwarder publishes 11436, within the guard's 4-CPU cap; the `ppe/` change ships in S2. Gitleaks: a `.gitleaks.toml` that extends the defaults with the `seriatim_` key-prefix rule | §2.1, §2.2, §3, §6, §7, §9 (S2-2, S2-4), §10 |

1. **Ruled (issuecomment-7109174985): A, and REQ-23 amended (S2-R4).** **Where keys are configured.** REQ-23 says the sidecar reads `SERIATIM_NODE_*` variables and
   one TOML file. Options: **(A)** `[[keys]]` tables in the `SERIATIM_NODE_CONFIG` file only;
   `SERIATIM_NODE_KEYS` in the environment is refused (exit 2); **(B)** also accept
   `SERIATIM_NODE_KEYS` as a JSON array; **(C)** a separate keys file, which needs a REQ-23
   change. *Recommendation: A.* A list of tables is natural in TOML and awkward in an
   environment variable, and one place means rotation edits one file. (§2.2, §6.) A related
   detail: SPEC §6.2 says `keygen` prints "the config line"; a `[[keys]]` table is three lines.
   The TD prints the table (§7, Appendix A).
2. **Ruled (issuecomment-7109496940): B (S2-R8).** **The key's printed form.** SPEC §6.2 and D6 fix 256 random bits, not the encoding.
   Options: **(A)** `secrets.token_urlsafe(32)`, 43 base64url characters; **(B)** the same with
   a fixed prefix, `seriatim_`; **(C)** 64 hex digits. *Recommendation: B.* The prefix lets
   gitleaks (and other scanners) be given a rule for leaked keys (T3), and base64url is a valid
   RFC 6750 `b64token`. The sidecar never checks the form: it hashes whatever is presented.
   (§7.) S2-R7's gitleaks rule matches this form.
3. **Ruled (issuecomment-7109496940): A (S2-R8).** **Key ids and the number of keys.** The SPEC gives no rule. Options: **(A)** the node-id
   pattern `^[a-z0-9][a-z0-9-]{0,31}$`, at most 256 keys; **(B)** any printable string up to 64
   characters, no limit. *Recommendation: A.* Ids appear in logs, and from S4c in `changed_by`,
   so a narrow alphabet keeps them safe to log; a cap bounds the per-request comparison cost.
   (§2.2, §6.)
4. **Ruled (issuecomment-7109174985): A, except that the hourly `WARNING` fires whenever
   `ALLOW_UNAUTHENTICATED` is set (S2-R1). REQ-15's listen-address exception for the PPE is
   removed too: the flag still forces loopback (S2-R2).** The question as asked:
   **Keys and `ALLOW_UNAUTHENTICATED=true` together.** REQ-15 says what happens with no keys,
   not with both. The flag is also S1's test-mode switch (REQ-12b's lock directory, REQ-16's
   probe fixture, REQ-15's listen address before S4a). Options: **(A)** configured keys are
   always enforced; the flag keeps its test-mode relaxations and the startup `WARNING`, and
   there is no hourly line; **(B)** refuse to start, as contradictory; **(C)** the flag wins,
   and the keys are ignored. *Recommendation: A.* It is the only option that lets a PPE or a
   test run with keys on, and C would silently turn configured keys off. A later rename of the
   flag to say "test mode" is a separate question. (§2.2, §6.)
5. **Ruled (issuecomment-7109496940): A (S2-R8).** **The checks before auth.** Auth needs `classify()` for the scope, and REQ-20a puts the
   path check before `classify()`. Options: **(A)** keep S1's 505, framing 400, missing
   `raw_path` 500 and `BAD_PATH` 400 before auth; **(B)** read the credentials (400 and 401)
   before everything, and check the scope (403) after `classify()`. *Recommendation: A.* Those
   checks reveal nothing about the node, and A keeps the dispatcher's order and S1's tests as
   they are. (§3.)
6. **Ruled (issuecomment-7109174985): unknown `/seriatim/*` paths are `admin`, as the
   contract says (S2-R3).** The ruling names option C of revision 1, which was `admin`; it is
   option A below. The question as asked:
   **Unknown `/seriatim/*` paths and wrong methods** (404, 405, including `POST` and `HEAD` on
   `/seriatim/health`). REQ-13 says every endpoint but `GET /seriatim/health` needs a key. The
   contract gives every method and path pair it doesn't list the `admin` scope:
   `x-seriatim-unknown-endpoint: { x-seriatim-gate: gated, x-seriatim-scope: admin }`
   (`contract/openapi.yaml`, lines 525–526), as REQ-5's "everything else" rule does. It does
   not single out `/seriatim/*`, so it covers those paths too. Options: **(A)** `admin`, as the
   contract says: an `admin` key gets the 404 or 405 and an `infer` key gets 403, so
   `/seriatim/admin/*` is a 403 to an `infer` key both before and after S4c; **(B)** any valid
   key (`infer`), then 404 or 405, which needs a contract change and makes `/seriatim/admin/*`
   a 404 to an `infer` key until S4c, against REQ-5; **(C)** answer 404 or 405 before auth,
   against REQ-13. *Recommendation: A*, the contract's rule as written; the design (§2.3, §4,
   A7, A8) follows it. A or B changes K1's three `/seriatim/*` rows.
7. **Ruled (issuecomment-7109174985): A; REQ-13 and ADR-001 D6 amended to match (S2-R5).**
   **Health with bad credentials.** `GET /seriatim/health` with a wrong key or an ambiguous
   `Authorization`. Options: **(A)** credentials are not read: 200; **(B)** they are checked
   whenever present: 401 or 400. *Recommendation: A.* Health needs no key (REQ-18), and a load
   balancer configured with a stale key should not take a healthy node out. (§2.1, A8.)
8. **Ruled (issuecomment-7109496940): A (S2-R8).** **The scheme's case.** RFC 9110 §11.1 makes auth schemes case-insensitive; REQ-13 says "a
   scheme other than Bearer". Options: **(A)** `bearer` and `BEARER` are Bearer; **(B)** only
   the exact `Bearer`; anything else is `AUTH_AMBIGUOUS`. *Recommendation: A*, as the RFC
   says. (§2.1.)
9. **Ruled (issuecomment-7109496940): A (S2-R8).** **Whitespace after `Bearer`.** RFC 6750 §2.1 allows one or more spaces before the token;
   REQ-13 rejects "a Bearer value with embedded whitespace". Options: **(A)** one or more
   spaces after the scheme; whitespace inside the token is ambiguous; **(B)** exactly one
   space; any other whitespace is ambiguous. *Recommendation: A*, as the RFC says; "embedded"
   then means inside the token. (§2.1.)
10. **Ruled (issuecomment-7109496940): A (S2-R8).** **An empty credential.** `Authorization:` with an empty value, or `Bearer` with no token.
    Options: **(A)** 401 `AUTH_MISSING`; **(B)** 401 `AUTH_INVALID`; **(C)** 400
    `AUTH_AMBIGUOUS`. *Recommendation: A.* No key was presented, and the client's fix is the
    same as for no header. (§2.1.)
11. **Ruled (issuecomment-7109496940): A (S2-R8).** **`X-API-Key`.** REQ-13 says it is not accepted. Options: **(A)** ignored: the request is
    judged on `Authorization` alone, so a client that sends only `X-API-Key` gets 401
    `AUTH_MISSING`, with a `hint` saying to send `Authorization: Bearer`; **(B)** its presence
    is `AUTH_AMBIGUOUS`. *Recommendation: A.* The predecessor's clients that still send
    `X-API-Key` get a fatal 401 that tells them what to change (SPEC §7.1), and one that
    sends both keeps working. Either way it is never forwarded. (§2.1, §2.4.)
12. **Ruled (issuecomment-7109496940): A (S2-R8).** **Unauthenticated test mode.** With no keys and the flag set. Options: **(A)** auth is
    skipped entirely: every route is open, `admin` ones included, and `Authorization` is not
    parsed; **(B)** ambiguous headers are still refused. *Recommendation: A.* It is S1's
    behaviour, so S1's suite and the PPE run unchanged (TD-slice-1 H16 sends two
    `Authorization` headers and expects them stripped, not refused). (§2.1.)
13. **Ruled (issuecomment-7109496940): A (S2-R8).** **Where the no-keys refusal lives.** Options: **(A)** in `load_settings` (step 7), with every
    other startup refusal, so it exits 2 (REQ-23); several S1 tests that call
    `load_settings({})` then get a key or the flag (Appendix B); **(B)** in `main()` and
    `create_app` only, so `load_settings({})` stays valid and fewer S1 tests change.
    *Recommendation: A.* One place for every refusal, and settings that would be refused can
    never be loaded. REQ-15 says "refuse to start" without an exit code; A gives it REQ-23's
    2. (§2.2, Appendix B.)
14. **Ruled (issuecomment-7109496940): A (S2-R8).** **The hourly warning's form.** Today's startup warning goes through Python `logging` to
    stderr, as plain text. Options: **(A)** the hourly one is a JSON line on stdout, in the
    request log's format (`event: "allow_unauthenticated"`, `level: WARNING`), first at startup and
    then every hour, beside today's stderr line; **(B)** the same plain stderr line every
    hour. *Recommendation: A.* Operators and S4d read the JSON stream, and a warning that is
    meant to be noticed belongs where the request lines are. (§2.4, Appendix A.)
15. **Ruled (issuecomment-7109496940): A (S2-R8).** **How auth refusals are logged.** S1 has `outcome: rejected` for its 400, 404 and 405.
    Options: **(A)** `unauthenticated` for both 401s, `forbidden` for the 403, `rejected` for
    the 400, all at `INFO`; **(B)** `rejected` for all of them; **(C)** as A, with
    `AUTH_INVALID` at `WARNING`. *Recommendation: A.* Distinct outcomes answer T15's "who
    tried" and give S4d something to count, without making every stale client a warning.
    (§2.4, §5.)
16. **Ruled (issuecomment-7109496940): A (S2-R8); the PPE's layout change of §6 ships in
    S2-2.** **A PPE case in S2.** The ADR's S2 row names no PPE test. Options: **(A)** none; the A
    tests run in process, and the PPE stays in unauthenticated test mode until S4a (its
    layout still changes for S2-R2, §6); **(B)** a PPE case with keys, which S2-R1 now allows,
    with a key generated by `ppe/ppe up` and
    never committed, and every P test sending it. *Recommendation: A.* The component tests
    see the exact headers Ollama receives (A11), and S4a, the first deployable slice, is
    where the PPE has to run with keys anyway. (§6, §8.)

**Consistency notes (no contradiction found).**
- ADR-001 D6 listed two kinds of ambiguous credentials; SPEC REQ-13 lists three (whitespace
  in the value too). This PR brings D6 in line with REQ-13, along with S2-R5's health rule.
- REQ-15's "except in the PPE" is removed by S2-R2: the flag no longer allows a non-loopback
  listen address anywhere (§2.2, §6).

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

The hourly warning with `ALLOW_UNAUTHENTICATED=true` and no keys (Q14; with keys, the
message is §2.4's "keys are enforced" one):

```json
{"ts":"2026-11-23T11:00:00.004Z","level":"WARNING","event":"allow_unauthenticated",
 "message":"SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true: the sidecar has no authentication (PPE and test mode only)",
 "node":"node-a"}
```

`keygen`'s output (Q2's form, S2-R8; the key is shown as a placeholder):

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
  one they test. C5's "no warning by default" uses the one-key baseline. C5's "with it,
  accepted" for `LISTEN_HOST=0.0.0.0` becomes "with it, still `ConfigError`" (S2-R2); its
  `LOCK_DIR` half is unchanged. C11's hosts are refused with the flag set too.
- **`harness.py` and `test_http.py` (S2-3).** The log reader moves to `harness.py` as
  `log_lines(text)`, which keeps only `event: "request"` lines, so the hourly line (S2-5) is
  never counted as a request. `test_http.py`'s `_log_lines` is replaced by it, and
  `test_http_auth.py` uses it too. L1's exact field list gains `client` after `known`.
- **`test_gate.py` (S2-3).** K1's `/seriatim/*` rows: 404 and 405 carry scope `ADMIN`, health
  keeps `NONE` (S2-R3). K-OAS1's check that the `/seriatim/*` scopes S1 serves agree with
  `classify()` is unchanged: health is still `none`.
- **`test_config.py` (S2-4).** C8 calls `main([])`.
- **TD-slice-1 itself.** Its §2.2 note that `SERIATIM` routes have scope `none`, and the K1,
  C1 and L1 rows, are reworded in S2-3 and S2-2 to match, in the same PRs as the tests (docs go
  with the code).
- **The PPE (S2-2).** `ppe/compose.yaml` puts `node-a` on loopback behind a forwarder (§6,
  S2-R2). `tests/behaviour/` and the P IDs do not change (Q16).
