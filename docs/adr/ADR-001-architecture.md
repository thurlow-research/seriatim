# ADR-001: Seriatim architecture

**Status:** revision 2, proposed. **Date:** 2026-11-16.
**Requirements:** [SPEC](../SPEC.md). **Evidence:** [EVALUATION](../EVALUATION.md).
**First slice:** [TD-slice-1](../design/TD-slice-1.md).

## 1. Context

Several clients share a few GPU machines that run Ollama. When two inference requests land on
one GPU, they compete for VRAM. Ollama then queues the second one out of sight, so client
timeouts fire, or it offloads layers to the CPU, which makes both requests slow. The
operators want three things:
- each machine runs one request at a time;
- a busy machine says so at once;
- clients see a single address that sends each request to a suitable machine.

The [evaluation](../EVALUATION.md) found that no existing gateway meets *busy → immediate
429* (E2) together with *Bearer auth* (E4), and that the in-house predecessor had four
defects (SPEC §1, F1 to F4). This ADR records how Seriatim is built instead.

## 2. Decision summary

```
 client ──Bearer──▶ gateway ──node key──▶ sidecar :11434 ──loopback──▶ Ollama 127.0.0.1:11435
                    (router,                (lock authority,              (never reachable
                     no lock)                one per GPU machine)          from the LAN)
```

| # | decision |
|---|---|
| D1 | The lock lives in a **sidecar on each GPU machine**. The gateway has no lock. |
| D2 | The sidecar **takes Ollama's port 11434**. Ollama moves to **`127.0.0.1:11435`**. |
| D3 | Busy means an **immediate admission 429**, marked `X-Seriatim-Gate: busy`. There is no queue. The gateway spills over **only on that marker**. |
| D4 | **Byte-for-byte passthrough.** `num_ctx` is never changed; an over-limit value is rejected. |
| D5 | The gateway is a **thin router**: it routes by size from Ollama's metadata, and combines the read-only views. |
| D6 | **Bearer keys** with `infer` and `admin` scopes. Nodes store hashes. Each node has its own keys. |
| D7 | **Python 3.12, Starlette, uvicorn (h11), httpx, anyio and pydantic-settings.** A 30-line OS lock (`fcntl.flock` / `msvcrt.locking`) for the singleton. WinSW and launchd run the services. |
| D8 | **The contract comes first** (Appendix A), plus a black-box behaviour suite, so another implementation can drop in. |
| D9 | **Config-driven deploy scripts.** The local-only check runs after every Ollama upgrade. Maintenance goes through the sidecar. |
| D10 | **Abnormal ends and restarts drain.** A slot is freed only after the upstream connection is closed and a bounded drain has passed. |
| D11 | **One sidecar per Ollama**, enforced by a host-wide file lock. |
| D12 | **Exact classification.** Only verified method and path pairs are ungated. Everything unknown is gated and needs `admin`. Unsafe raw paths are rejected before matching. |
| D13 | **Deploy from branches.** A push to `ppe` updates the test host; a push to `prod` updates production. Both move only by fast-forward to a verified `main` commit; `prod` also needs the maintainer's approval. Hosts pull; nothing pushes into them. |

## 3. Decisions and the alternatives rejected

### D1. The lock lives at the GPU machine

**Decision.** Each GPU machine runs a sidecar that holds that machine's slots. Only it admits
requests to its Ollama.

**Why at the machine.**
- **Correct by construction.** Only a process in front of Ollama on that machine sees every
  network request to it. A central lock misses any client that reaches the machine some other
  way, such as a script, a developer or a second gateway.
- **One machine per lock (F1).** The predecessor's single global lock returned 429 for idle
  machine B while A was busy (E2). Locks on separate machines can't couple them.
- **A stateless gateway.** The gateway can restart, or run as several copies, without losing
  or splitting the lock state.
- **Fails locally.** If the gateway is down, clients can use a sidecar directly, with the same
  guarantees.

| alternative | why rejected (evaluation evidence) |
|---|---|
| A lock in the central gateway, fixed to one lock per backend | Every path to Ollama that skips the gateway is ungated. The state lives in the router process, so a restart or a second copy breaks the guarantee |
| LiteLLM plus a `CustomLogger` gate hook | Worked in the PPE (a 429 in 30 ms). But a dead backend leaks its slot until a reaper frees it, because failure events in v1.108.9 carry no deployment id. It has no native `/api/chat` (E1a ❌), so it needs a ~100-line translation layer. It is a 1.8 GB, fast-moving dependency, about 250 lines against about 150 |
| Olla plus a layer in front | Olla routes best (E3 ✅) and passes options through (E1a ✅), but has no concurrency cap and no auth (E2 ❌, E4 ❌). The layer we would put in front *is* this design. Olla stays the runner-up for discovery later |
| A per-endpoint `max_concurrency` patch to Olla | A 150–250-line Go PR with an uncertain merge, and it still needs auth |
| lollms_hub or ollama_load_balancer | Stale projects that failed E2 and E3. lollms_hub ships a default secret that works as an admin token. ollama_load_balancer returns 503 without `Retry-After`, and spills over instead of rejecting |
| Ollama's `OLLAMA_NUM_PARALLEL=1` alone | It queues instead of rejecting. Kept only as a second line of defence (REQ-4) |
| A distributed lock (Redis or etcd) | Extra infrastructure, and direct access to Ollama would still be ungated |

### D2. The sidecar takes port 11434; Ollama moves to 127.0.0.1:11435

Clients and tools that default to `:11434` reach the sidecar without changes.

**Alternatives rejected.**
- **The sidecar on another port, Ollama left on 11434.** Any client left on the default port
  skips the gate.
- **A Unix socket for Ollama.** Not available on Windows.
- **The predecessor's layout** (the gateway on 11435, Ollama on 11434). It has the same flaw
  as the first alternative.

**Consequence.** Ollama upgrades can reset its bind address or turn its own auto-start back
on. The local-only check (REQ-53) and the fail-closed exposure check (REQ-19) cover this.

### D3. Busy means an immediate admission 429; spill-over only on that marker

**Decision.**
- A gated request with no free slot gets 429 at once, decided from the method and raw path
  alone, before the body is read.
- The admission 429 carries `X-Seriatim-Gate: busy` and an envelope with `error_code:
  GPU_BUSY`, `source: sidecar` and `replay_safe: true`.
- The sidecar **strips every `X-Seriatim-*` header from Ollama's responses**, so Ollama cannot
  forge the marker.
- The gateway spills over only on that marker. It tries each eligible node once, in priority
  order, and never waits (ruling Q1). An Ollama 429 is passed through untouched.

**Alternatives rejected.**
- **A short wait-and-retry queue.** This is the hidden wait the evaluation measured against
  LiteLLM (30 s) and Olla (28 s).
- **Spilling over on any 429.** An Ollama 429 or 503 may mean Ollama *did* receive the request,
  so retrying it elsewhere could run it twice.
- **503 without `Retry-After`** (ollama_load_balancer). It doesn't say when to retry, and it
  means "down", not "busy".
- **An estimated `Retry-After`.** Generation lengths can't be predicted. The value is static
  and configurable (default 15 s), and is extended to cover any drain still in progress.

### D4. Byte-for-byte passthrough; `num_ctx` rejected, never changed

**Decision.**
- Bodies are forwarded as the exact bytes received, and responses as raw bytes, without
  decoding.
- A body is parsed only to check `num_ctx` (S3), and the parse is strict: duplicate keys, wrong
  types and conflicting placements are rejected as ambiguous.
- An over-limit value gets 422. `num_ctx` on `/v1` gets 400 with a hint (rulings Q2 and Q3).

**Alternatives rejected.**
- **Clamping to a "safe" table** (the predecessor, and lollms_hub's crop). It silently changes
  what the caller asked for (E1a: 30000 → 24576), and the table goes stale.
- **A flag mode that forwards unchanged and sets a header.** The flag is easy to ignore, and an
  over-limit context can spill layers to the CPU and hold the GPU for minutes (ruled out, Q2).
- **Translating `/v1` to `/api/chat`** (LiteLLM). It lost a nested `options.num_ctx` (E1b),
  it is a translation layer, which is a non-goal, and it changes the bytes.
- **Re-serialising JSON.** It changes key order, whitespace and number formatting, and the
  result can't be audited on the wire.

### D5. The gateway is a thin router

**Decision.**
- **Routing.** Candidates are the nodes that are healthy, list the model, and have
  `max_params_b` ≥ the model's size. The size comes from `size_overrides` in the
  configuration, or else from Ollama's `details.parameter_size`. Candidates are tried in
  priority order.
- **Discovery.** A node whose discovery failed is in an *unknown* state, never treated as "has
  no models". So 404 is given only when every node answered (REQ-34).
- **The fleet is configuration.** The gateway knows only the nodes its config lists, with
  their URLs, keys, priorities and size limits. Nothing in the code names a node, a count, a
  platform or a memory size. A changed file is validated and swapped in atomically without a
  restart (SPEC REQ-44, REQ-45). A home lab's machines change, so a fixed topology would go
  stale.
- **Combined views and `/models/select`.** It combines the read-only views and serves
  `/models/select` from configuration and measured memory.

**Alternatives rejected.**
- **Parsing the model name** (`:32b`). Names are free text, and Ollama's metadata is the
  authority.
- **An estimated token count**, as an earlier design used (`len(prompt)/4`). Too inaccurate to
  choose a machine by.
- **Discovery alone.** It can't keep a 32B model off the smaller card when both machines have
  it.
- **A fixed topology**, such as a built-in "small node" and "large node". A third machine,
  or one that replaces another, would then need a code change.
- **Config changes only on restart.** A restart drops every open stream through the gateway,
  so a routine edit would interrupt clients.
- **The predecessor's built-in model knowledge base.** It goes stale, it is tied to specific
  hardware, and it is a non-goal.
- **LiteLLM as the router.** It has no native API (E1a, E6 ❌).

### D6. Bearer keys with scopes, at both layers

**Decision.**
- **Keys.** 256-bit random keys. Nodes store SHA-256 hashes, compared in constant time against
  every configured hash.
- **Scopes.** `infer` or `admin` (ruling Q5). The scope a route needs comes from the same
  `classify()` that decides admission, so authorisation and the gate can't disagree. Unknown
  endpoints need `admin`.
- **Ambiguous credentials are rejected.** That covers more than one `Authorization` header, or
  a scheme other than Bearer.
- **Gateway keys.** The gateway holds an `infer` key and an `admin` key for each node. Client
  credentials are stripped before forwarding.

**Alternatives rejected.**
- **No auth on the sidecar.** A LAN host could use the GPU, and a direct client would lose the
  gateway's guarantees.
- **Forwarding the client's `Authorization` header.** ollama_load_balancer does this, and it
  leaks keys to Ollama.
- **Keys in query strings.** They leak into logs and `Referer` headers.
- **Accepting `X-API-Key` as well.** No existing client uses it (ruling Q7), and a second
  header creates ambiguity.
- **Slow password hashes.** Unneeded for random 256-bit keys, and they would add latency.
- **mTLS between the gateway and the nodes.** It brings certificate management without
  removing the need for client keys. Deferred.
- **A default secret.** This was lollms_hub's bypass (E4 ⚠). Seriatim refuses to start without
  keys.

### D7. Libraries

**Decision.**
- **Starlette** for the `/seriatim/*` routes and the lifespan.
- **A plain ASGI callable** for the proxy, for exact control over disconnects and over when a
  slot is freed.
- **httpx** (async, HTTP/1.1, raw streaming) for upstream.
- **uvicorn with h11**, one worker.
- **anyio** for cancellation scopes.
- **pydantic-settings** for the env and TOML config.
- **A small OS lock** for the singleton (D11): `fcntl.flock` on POSIX and `msvcrt.locking` on
  Windows. It is about 30 lines and never deletes the lock file.
- **WinSW and launchd** for the services; **uv** for the workspace and lock file.

**Alternatives rejected.**
- **FastAPI.** It adds a layer mainly for a generated OpenAPI, and this project is
  contract-first.
- **aiohttp.** httpx with Starlette is what the predecessor proved, with a cleaner streaming
  and timeout model.
- **A hand-rolled HTTP server.** That would rebuild what already exists.
- **The `filelock` library.** Its Windows release deletes the lock file. A sidecar that is
  exiting can delete the file while a new one has it open, and a third can then create and
  lock a fresh file at the same path. Two live sidecars would then hold locks on different
  files. The singleton needs a lock file that is never deleted, and the OS calls give that
  directly.
- **Go or Rust now.** The contract (D8) keeps a later port possible.
- **NSSM.** Unmaintained since 2017. **AlwaysUp** is commercial. **pywin32 services** would be
  code needing its own tests.
- **Ollama.app on macOS.** Its configuration doesn't survive a reboot, and it auto-starts with
  its own bind address.

### D8. The contract comes first

**Decision.** `contract/openapi.yaml` (Appendix A) is the source of truth for every
Seriatim-owned endpoint, header, status and error code.
- CI lints it.
- Tests check every Seriatim error body against `ErrorEnvelope`.
- The code's error table must equal `x-seriatim-codes`.
- The allowlist must match the route probe recorded against the supported Ollama version.
- `tests/behaviour/` is a black-box suite parametrised by base URL. Another implementation is
  a drop-in when it passes that suite in the PPE.

**Alternatives rejected.**
- **Generating the spec from the code.** The spec would then follow the code instead of
  constraining it.
- **JSON Schema alone.** It has no paths, headers or status codes.

### D9. Deploy scripts; maintenance goes through the sidecar

**Decision.**
- **Scripts.** `deploy/windows/*.ps1` and `deploy/macos/*.sh` are driven by configuration,
  check every native exit code, and are tested with Pester and bats.
- **Model updates** use the sidecar's admin route, `POST /api/pull`, never 11435 directly. No
  Seriatim component therefore bypasses the gate.
- **The local-only check** runs after install, after every model update, after every Ollama
  upgrade, and weekly.

**Alternatives rejected.**
- **Copying the predecessor's scripts.** They are tied to one machine. They also wrap native
  commands in `try/catch`, which never fires in PowerShell, so a failed `ollama pull` was
  counted as updated.
- **Pulling through 11435 directly.** That is the bypass review item 7 found.
- **Ansible or a similar tool** for two or three machines. Too heavy for the fleet.

### D10. Abnormal ends and restarts drain; Ollama is not restarted with the sidecar

**Decision.** Closing the upstream connection makes Ollama cancel the generation, but it does
not prove the GPU is idle. So after any abnormal end the slot *drains* for
`SERIATIM_NODE_DRAIN_S` (default 10 s) before it is free. Abnormal ends are a client
disconnect, an upstream failure, the hold limit, or an internal error after the request was
sent. New gated requests meanwhile get the admission 429.

A restarted sidecar starts with **every slot draining**, and answers 503 `NODE_STARTING`.
- **Why this covers a crash.** When a sidecar process dies, by crash or kill, the operating
  system closes its sockets. Ollama sees the same disconnect as a cancellation, so the drain
  window covers it.
- **The default.** It is checked against the cancellation latency measured in the PPE (P6),
  and again on each GPU node in S5 and S6.
- **What it does not prove.** The drain is a time window. Ollama exposes no signal that a
  cancelled generation has stopped. A generation slower to stop than `DRAIN_S` is caught by
  Ollama's own `OLLAMA_NUM_PARALLEL=1` queue. The next request then waits; the two never run
  together. SPEC G1 states this scope.
- **Closure must be proven before the drain starts.** Each gated request uses its own
  one-connection httpx client. The drain starts only after that client's `aclose()` has
  returned. If that call fails or times out, the slot is *stuck* until a restart, which
  closes the socket for certain (TD-slice-1 §3.3).

**Alternatives rejected.**
- **Freeing the slot as soon as the connection is closed** (revision 1). It assumes the GPU
  stops instantly.
- **Waiting for `/api/ps` to change.** `/api/ps` lists *loaded* models, not *active*
  requests, so it can't show that a cancelled request has stopped. S4 may adopt a positive
  signal only if the PPE shows one is reliable.
- **Restarting Ollama together with the sidecar.** It unloads every model (minutes to reload
  a 32B model), it needs the sidecar to control the Ollama service, and it gives nothing the
  drain does not.
- **Persisting lock state to disk.** The state that matters is in Ollama, not in the sidecar.
  A stale on-disk lock would be a new failure mode.

### D11. One sidecar per Ollama

**Decision.** At startup the sidecar takes an exclusive OS lock on
`<lock dir>/upstream-<port>.lock`.
- **The name.** The upstream is always a loopback address, so its port identifies it on the
  host.
- **The directory** is fixed and system-wide, not per user. It can be changed only in PPE and
  test mode (REQ-12b).
- **Holding it.** The lock is held by a module-level reference for the life of the process.
  It is never released or deleted explicitly; the OS releases it when the process exits.
- **Failure.** If the lock is held, or cannot be taken, the sidecar exits with code 3.

**Alternatives rejected.**
- **Relying on the listen port.** Two sidecars on different ports can still front one Ollama.
- **A PID file.** Stale PID files and PID reuse make it unreliable. An OS lock is released when
  the process dies.

**Residual risk.** The lock covers one filesystem. Two containers with separate filesystems
that share one Ollama are not detected. That is a deploy error, which the local-only check
also catches, because port 11434 must belong to *the* sidecar, and the check verifies that
exactly one sidecar process runs on the host.

### D12. Exact classification; unknown endpoints are gated and need admin

**Decision.**
- **Unsafe paths are rejected first.** The raw path (ASGI `raw_path`, never the decoded path)
  is checked for unsafe content (REQ-20a). Anything unsafe gets 400 `BAD_PATH`.
- **Exact matching.** The path is then matched exactly against `/seriatim/*`, the ungated
  allowlist, and the known inference routes. No trailing-slash folding, no case folding and no
  segment collapsing.
- **Everything else** is gated and needs `admin` (SPEC REQ-5).
- **The allowlist** is checked against a route probe recorded against the supported Ollama
  version.

**Alternatives rejected.**
- **Ungating every `GET`, `HEAD` and `OPTIONS`** (revision 1). Ungated methods on unknown paths
  are a gate bypass (review item 1).
- **Normalising before matching** (revision 1). A path such as `/api/chat/../show` would
  collapse to an ungated route (review item 2).
- **A deny-by-default 404 for unknown endpoints.** It would break new Ollama features until
  Seriatim is updated. Gating them and requiring `admin` is just as safe and keeps them usable.

### D13. Deploy from the `ppe` and `prod` branches; hosts pull

**Decision.**
- **Branches** (SPEC §7.6):
  - a push to `ppe` updates the designated PPE host, which then runs the behaviour suite;
  - a push to `prod` updates the GPU nodes and the gateway host.
- **Promotion.** `ppe` and `prod` move only through the `promote` workflow, which
  fast-forwards the target to a `main` commit whose required checks passed.
  - **`prod` needs two more things:** the maintainer's approval of the `prod` environment, and
    a `seriatim/ppe` success status on that exact SHA (SPEC REQ-65).
  - **The identity.** The workflow pushes as a dedicated App, `seriatim-promote`. Its key is
    an environment secret, released only to runs on `main` after the environment's rules
    pass. It is the only bypass actor on the rulesets, which allow no force-push and no
    deletion.
- **The deployer.** Each host runs a small deployer (`seriatim-deploy`, Python, on every OS),
  on a timer with random jitter. It polls its branch, and verifies the commit: CI green, and
  an ancestor of `main`. It then installs the commit into a new venv, restarts, checks, and
  rolls back on any failure.
- **Where the pattern comes from.** The maintainer's existing services use the same
  branches, with a polling deploy timer on each host. Seriatim adds three things: the
  verification before installing, the rollback, and enforcement through the workflow instead
  of by convention.

**Alternatives rejected.**
- **A self-hosted GitHub runner on each host, triggered by the push.** On a public
  repository, a self-hosted runner can be made to run code from untrusted pull requests. It
  also puts a credentialed agent with outbound control on every GPU machine. Polling a public
  branch needs no credentials and no inbound access. It updates within one interval (default
  60 s), which is close enough to "on push".
- **Pushing to the hosts from a GitHub-hosted workflow** (SSH or WinRM). The nodes would need
  inbound access from the internet, and GitHub would hold credentials for them.
- **Deploying tagged releases by hand** (revision 1). It is slower, and nothing enforces that
  production runs code from `main`.
- **Pushing `ppe` and `prod` by hand** (`git push` of a fast-forward). That is the existing
  convention elsewhere, but nothing on GitHub enforces it. A mistaken push of an unmerged
  commit would deploy it. The workflow and the deployer's own ancestry check are both kept.

**Consequences.**
- Weekly maintenance still never updates Seriatim (ruling Q10). Only the deployer does.
- **Two extra Apps for the maintainer to create:** `seriatim-promote` (`contents: write`)
  and `seriatim-ppe-reporter` (`statuses: write`). Neither is the agents' App.
- **Check filtering.** CI also runs on pushes to `ppe` and `prod`. The deployer accepts only
  check runs from the `push` event on `main`, so the fresh runs that a promotion triggers
  are ignored. They do not hold up the deploy.
- A restart drains the node (D10). The health-gated, slotted rollout makes it unlikely that
  two nodes restart at once, but does not guarantee it (SPEC REQ-66).

## 4. Components

| component | package | runs on | state |
|---|---|---|---|
| sidecar | `node/seriatim_node` | each GPU node (Windows, macOS) | slots (in memory), the singleton lock file, config |
| gateway | `gateway/seriatim_gateway` | one central host (container or systemd) | node health and discovery cache (in memory, rebuilt from the nodes) |
| contract | `contract/openapi.yaml` | — | — |
| deploy | `deploy/windows`, `deploy/macos` | GPU nodes | config, logs |
| PPE | `ppe/` | CI, and the designated local host | containers, model cache |

## 5. Interface contract

### 5.1 Who talks to whom

| from → to | transport | auth | what |
|---|---|---|---|
| client → gateway | HTTP/1.1 (TLS optional) | `Authorization: Bearer` (client key) | the Ollama native API, `/v1/*`, `/models/select`, `/seriatim/*` |
| client → sidecar | HTTP/1.1 :11434 | Bearer (a key known to that node) | the Ollama native API, `/v1/*`, `/seriatim/*` |
| gateway → sidecar | HTTP/1.1 :11434 | Bearer (the node's `infer` or `admin` key) | as a direct client, plus `X-Request-Id` |
| sidecar → Ollama | HTTP/1.1 over loopback :11435 | none | raw passthrough |

### 5.2 Headers that Seriatim adds or reads

| header | where | meaning |
|---|---|---|
| `Retry-After` | 429, and 503 except `OLLAMA_EXPOSED` | whole seconds |
| `X-Seriatim-Gate` | every sidecar response | `acquired` (the request held a slot), `ungated`, or `busy` (the admission 429) |
| `X-Seriatim-Node` | gateway responses; also a request header for admin calls | the node that served the request, or the node to target |
| `X-Seriatim-Partial` | gateway combined views | comma-separated ids of nodes that didn't answer |
| `X-Request-Id` | both directions | echoed if valid (≤ 128 characters, `[A-Za-z0-9._-]`), otherwise generated |
| `X-Resolved-Model`, `X-Model-Digest`, `X-Execution-Device`, `X-Rerouted-From`, `X-Reroute-Reason` | gateway responses | kept for compatibility (SPEC §7.1) |

### 5.3 Header handling in the passthrough

**Request to Ollama.**
- **Removed:**
  - the hop-by-hop headers: `Connection` and every header it names, `Keep-Alive`, `Proxy-*`,
    `TE`, `Trailer`, `Transfer-Encoding` and `Upgrade`;
  - `Authorization`, `Cookie`, `X-API-Key` and `X-Seriatim-*`;
  - the forwarding headers `Forwarded`, `X-Forwarded-*` and `X-Real-IP`;
  - `Accept-Encoding`, so Ollama never compresses and in-stream error records stay safe to
    frame.
- **Set:** `Host` to the upstream; `Content-Length` from the exact bytes of the body.
- **httpx's own defaults** (`accept`, `accept-encoding`, `user-agent`) are deleted *after*
  `build_request`, unless the client sent them. This is because httpx fills them in when the
  request is built.

**Response to the client.**
- **Removed:**
  - the hop-by-hop headers;
  - every `Access-Control-*` header, because Seriatim serves no CORS;
  - every `X-Seriatim-*` header, and the compatibility headers in §5.2, so Ollama can't forge
    Seriatim's own;
  - `Set-Cookie`.
- **Kept, unchanged:** `Content-Type`, `Content-Length` and `Content-Encoding`. The bytes are
  raw, so the values stay valid.
- **Then** Seriatim adds its own headers.

### 5.4 Error envelope, codes and replay safety

Every error Seriatim produces has this form (Ollama's own errors pass through as
`UpstreamError`):

```json
{"error": "GPU busy: another request is running on this node",
 "error_code": "GPU_BUSY", "retryable": true, "replay_safe": true, "retry_after": 15,
 "source": "sidecar", "node": "node-a", "busy_for_s": 12.4}
```

- `retryable` means that retrying later can succeed.
- `replay_safe` means that Ollama provably never received this request, so it may be resent
  anywhere. The gateway retries on another node only when `replay_safe` is true.

| code | HTTP | retryable | replay_safe | by |
|---|---|---|---|---|
| `GPU_BUSY` | 429 | yes | yes | sidecar |
| `BAD_PATH` | 400 | no | yes | both |
| `AUTH_AMBIGUOUS` | 400 | no | yes | both |
| `BAD_REQUEST_BODY`, `NUM_CTX_AMBIGUOUS`, `NUM_CTX_UNSUPPORTED_ROUTE` | 400 | no | yes | sidecar |
| `NODE_REQUIRED`, `UNKNOWN_TASK` | 400 | no | yes | gateway |
| `AUTH_MISSING`, `AUTH_INVALID` | 401 | no | yes | both |
| `FORBIDDEN_SCOPE` | 403 | no | yes | both |
| `NOT_FOUND` (a reserved `/seriatim/*` path) | 404 | no | yes | both |
| `MODEL_NOT_FOUND` | 404 | no | yes | gateway |
| `METHOD_NOT_ALLOWED` | 405 | no | yes | both |
| `REQUEST_BODY_TIMEOUT` | 408 | yes | yes | both |
| `UPLOAD_INTERRUPTED` (a streamed blob upload, after bytes were forwarded) | 408 | yes | **no** | both |
| `BODY_TOO_LARGE` | 413 | no | yes | both |
| `NUM_CTX_EXCEEDS_LIMIT` | 422 | no | yes | sidecar |
| `MODEL_SIZE_UNKNOWN` | 422 | no | yes | gateway |
| `INTERNAL` | 500 | yes | **no** | both |
| `UPSTREAM_UNAVAILABLE` | 502 | yes | yes (the connect failed) | both |
| `UPSTREAM_PROTOCOL_ERROR` | 502 | yes | **no** | both |
| `NODE_STARTING` | 503 | yes | yes | sidecar |
| `EXPOSURE_UNVERIFIED` | 503 | yes | yes | sidecar |
| `OLLAMA_EXPOSED` | 503 | **no** | yes | sidecar |
| `NODE_SATURATED` (the ungated connection pool is full) | 503 | yes | yes | sidecar |
| `DISCOVERY_INCOMPLETE` | 503 | yes | yes | gateway |
| `GPU_OFFLINE` (no healthy node can serve the model) | 503 | **no** | yes | gateway |
| `LOCK_HOLD_EXCEEDED` | 504 | yes | **no** | sidecar |
| `HTTP_VERSION_UNSUPPORTED` | 505 | no | yes | both |
| `UPSTREAM_STREAM_BROKEN` (an in-stream record only) | — | yes | **no** | both |

## 6. Concurrency model (summary)

The details are in TD-slice-1 §3.
- **One process, one event loop.** Admission is a synchronous counter check, with no `await`
  inside, so it is atomic.
- **Admission happens before the body is read.**
- **The lease belongs to the proxy's ASGI call.** The call runs the upstream exchange inside
  anyio cancel scopes: an outer deadline scope for the hold limit, and a task group with a
  disconnect watcher.
- **One way out.** Every exit passes through a single `finally`. Inside a **shielded** cancel
  scope it closes the upstream response, then frees the slot or starts its drain.
- **No background task ever frees a slot.**

## 7. Threat model

**Assets:**
- GPU time;
- models on disk;
- prompts and outputs;
- keys;
- Ollama's admin API (pull, push, create, copy and delete);
- the one-at-a-time guarantee itself.

**Actors:**
- **Trusted:** the maintainer and operator, who hold the admin keys; local users of a node.
- **Semi-trusted:** holders of `infer` keys.
- **Untrusted:** any host on the LAN; web pages in a LAN browser.

| # | threat (STRIDE) | vector | mitigation | residual |
|---|---|---|---|---|
| T1 | Elevation / DoS: bypassing the gate | A LAN host reaches Ollama directly; a node-local process uses 11435 | Loopback bind (D2); firewall; local-only check; the fail-closed exposure check (REQ-19); Seriatim's own scripts use the sidecar (D9) | Node-local processes can reach 11435. Local users are trusted, and the guarantee says so (SPEC §6.1) |
| T2 | Information disclosure and tampering: sniffing keys or prompts; forging a node's admission 429 so that the gateway runs a request twice | Plain HTTP on the LAN | Optional TLS (REQ-27); required where the LAN is not trusted (SPEC §6.2) | **Open** while TLS is off. Active network attackers are outside the threat model in that mode |
| T3 | Information disclosure: keys at rest or in logs | The repository, logs, config files | Hashes on nodes; credentials never logged or accepted in query strings; gitleaks; config ACLs | The gateway holds node keys in plain text (it must) |
| T4 | Spoofing: forging a client | Guessing, or a timing attack | 256-bit keys; constant-time comparison against every hash | — |
| T5 | DoS: holding the GPU | A long generation, a slow reader, a slow uploader | The maximum hold (REQ-12); body limits (REQ-20); disconnects drain | An `infer` key can keep a node busy up to the maximum hold, repeatedly (quotas are a non-goal) |
| T6 | Tampering / elevation: abuse of the admin API | Deleting models, filling the disk, pushing to a registry, SSRF through `/api/pull` | The `admin` scope (REQ-14); unknown endpoints need `admin` | Admin key holders are trusted |
| T7 | Tampering: path confusion | `..`, `%2e`, `%2f`, `//` or backslashes that turn a gated path into an ungated one | Raw-path rejection before any normalisation; exact matching (D12) | — |
| T8 | Tampering: request smuggling or header injection | Crafted `Transfer-Encoding`, `Connection` or `Host` | h11 and httpx parsing; HTTP/1.1 only; hop-by-hop headers stripped; body length from bytes | Depends on h11 and httpx (pinned and updated) |
| T9 | Spoofing: forging Seriatim headers through Ollama | A compromised or odd upstream sets `X-Seriatim-Gate: busy` to trigger replays | The sidecar strips every reserved header from Ollama's responses (§5.3) | — |
| T10 | Spoofing: a rogue node lies to the gateway | A fake 429, `/api/tags` or status | Nodes are configured explicitly; the gateway authenticates to them | Responses from nodes are not authenticated without TLS (see T2) |
| T11 | Cross-site requests from a LAN browser | A web page calling a node | Bearer only, no cookies, no CORS headers (and Ollama's are stripped) | Blind requests are possible, and are rejected with 401 |
| T12 | Information disclosure through status and errors | Health, the 429 body | Health gives only a status and reasons; the 429 shows only `busy_for_s`; status needs a key | Key holders see which models are loaded |
| T13 | Elevation: two sidecars on one Ollama | A second instance on another port, or with another lock directory | The host-wide OS lock in a fixed directory (D11); the local-only check's one-process check | Containers with separate filesystems (D11) |
| T16 | Elevation: an unreviewed change reaches a host | A push to `ppe`/`prod` of a commit not on `main`; a workflow on another branch using the promotion key | Rulesets with `seriatim-promote` as the only bypass; the key held as an environment secret, released only to runs on `main`; the deployer's own ancestry and required-check verification (D13) | The maintainer's own account, and the promotion App's key |
| T14 | Supply chain | A malicious dependency or Action | `uv.lock`; Actions pinned by SHA; gitleaks verified by checksum; hosts install only commits from `prod`, which is fast-forwarded from verified `main` commits and checked again by the deployer (D13) | A compromise between pinned updates |
| T15 | Repudiation | "Who used the GPU?" | A request log with key id, request id, path, model and timings (REQ-22) | Logs are local and not tamper-proof |

## 8. Residual risks

1. **Plain HTTP on the LAN** while TLS is off (T2).
2. **The drain is time-based** (D10).
   - If Ollama takes longer than `DRAIN_S` to stop after a cancellation, the next request is
     admitted, but Ollama's own `NUM_PARALLEL=1` queue holds it until the first one stops. The
     result is a delay, not two requests at once.
   - P6 and the S5/S6 node measurements record the real latency, and the default carries a
     margin over it.
   - If closing the upstream connection cannot be proven, the slot stays stuck instead
     (D10).
3. **Node-local bypass** of 11435 (T1), outside the guarantee by definition.
4. **A static `Retry-After`** can be too short or too long.
5. **Ollama API drift.** New endpoints are gated and need `admin` (D12). The fixtures and route
   list captured for each version catch format changes before an upgrade (REQ-56).
6. **The gateway as a single point of failure.** Clients can fall back to a sidecar directly.
7. **Model metadata quality.** A wrong `parameter_size` misroutes a model, unless
   `size_overrides` corrects it. A missing one is rejected (`MODEL_SIZE_UNKNOWN`).
8. **Memory figures are advisory and never invented.** On Apple Silicon `free_mb` is null, so
   `/models/select` cannot answer "fits" there.

## 9. Evaluation summary

The evidence is in [EVALUATION.md](../EVALUATION.md). In short:

| | predecessor | LiteLLM | Olla | lollms_hub | ollama_load_balancer |
|---|---|---|---|---|---|
| E1a native options | ◐ clamps `num_ctx` | ❌ no native route | ✅ | ✅ (crop in code) | ✅ |
| E1b `num_ctx` on `/v1` | ❌ | ◐ | ❌ | ❌ | ❌ |
| E2 busy → 429 | ◐ global lock | ❌ queued (✅ with hook) | ❌ queued | ❌ queued | ❌ 503 without `Retry-After` |
| E3 size routing | ❌ | ✅ | ✅ | ❌ | ❌ |
| E4 Bearer auth | ✅ | ✅ | ❌ | ✅ ⚠ default secret | ❌ |
| E5 GPU memory | ✅ | ❌ | ❌ | ◐ | ❌ |
| E6 native views | ◐ one machine | ❌ | ◐ | ❌ | ◐ |

## 10. Slice plan

**Rules for every slice:**
- **Size.** At most 15 changed files (lock files excluded) and a diff of at most 150 KB.
  Slice 1 is planned at 13 files, to leave a margin.
- **TD first.** The slice's TD is reviewed and merged *before* its code, in its own PR, so TD
  changes never sit in the implementation PR.
- **Gates.** CI is green, which includes coverage, mutation and the PPE. The PR quotes the
  coverage and mutation numbers.
- **Review and approval.** An independent reviewer reviews the slice, every real finding is
  fixed, and the maintainer approves.
- **Order.** Each slice leaves `main` releasable. A sidecar is useful on its own (S1 to S6)
  before the gateway exists (S7 to S10).

**Already done:** `main` holds the licence, README, CONTRIBUTING, `.gitignore`,
`.env.example`, CODEOWNERS, the gitleaks pre-commit hook and the `secret-scan` workflow.

| slice | content | tests | TD |
|---|---|---|---|
| **S0a** Tooling and CI | Root `pyproject.toml` (uv workspace, ruff, pytest, `mutmut>=3.3,<4`); `node/pyproject.toml` with pinned runtime dependencies; `node/seriatim_node/__init__.py` and `__main__.py`; `uv.lock`; `.env.example` with the `SERIATIM_NODE_*` names; `.github/workflows/ci.yml` (lint, test matrix, coverage, mutation with the score parser, contract lint, IP literals, private terms, PR size); `.github/workflows/security.yml` (Bandit, pip-audit, Trivy config scan; weekly schedule); both on PRs and on pushes to `main`, `ppe` and `prod`; PR template | Each gate shown failing on a deliberate bad commit, recorded in the PR | none (ruling Q8) |
| **S0b** PPE, fixtures and the PPE job | `ppe/compose.yaml` (one Ollama + capture, real micro-models, limits, localhost ports); `ppe/ppe` (up/down/purge, effective-config guard, `--ci` only on GitHub Actions, allowed-host for local runs); `tools/capture_fixtures.py` (fixtures plus a probe of every classified route); `tests/fixtures/route-pairs.json` (the REQ-5 pairs); `tests/fixtures/ollama-0.40.16.json`; `.github/workflows/ppe.yml` (required, model cache) | `tests/tools/test_ppe_guard.py` (every refusal in SPEC §8.3); `tests/tools/test_capture.py` (cleaning, format) | none (Q8); formats in TD-slice-1 App. B |
| **S1** Contract + sidecar gate, 429, passthrough | `contract/openapi.yaml`; `node/`: config, classification, gate with drain, proxy (streaming, disconnects, hold limit, framing-safe errors), startup quarantine, singleton, health, logs | TD-slice-1 §8: C, K, G, H, B, S, F, E, R, HL, L, K-OAS; PPE P1–P7 | **TD-slice-1** |
| **S2** Sidecar auth | keys (hashes, ids, scopes), `keygen`, 401/403, `AUTH_AMBIGUOUS`, constant-time comparison against every hash, the hourly unauthenticated warning | A1–A12: every route class × {missing, wrong, infer, admin} key; ambiguous credentials; key never in logs or upstream; startup refusals | TD-slice-2 |
| **S3** `num_ctx` policy | strict body inspection (duplicate keys, types, conflicts); limits by pattern with an overlap check; `/v1` rule; the holder's model in status | N1–N10, with wire-capture proof in the PPE that the bytes are unchanged | TD-slice-3 |
| **S4** Status, memory, exposure | `/seriatim/status`; `nvidia-smi` and macOS probes (measured-only); `/api/ps` error state; the fail-closed exposure check; health extensions; non-loopback listen enabled | M1–M8 (recorded `nvidia-smi` and `sysctl` outputs, failures, timeouts); X1–X6 (exposed, unverifiable, timeout, incomplete, first-check gating, recovery) | TD-slice-4 |
| **S0c** Release branches | `.github/workflows/promote.yml` (manual, target `ppe` or `prod`; the `prod` environment for `prod`; ancestry and check verification; fast-forward only); `deploy/common/seriatim_deploy.py` (poll, verify, versioned venv, switch, check, roll back, stagger); a systemd timer example for the PPE host; the `ppe` and `prod` ruleset settings, documented for the maintainer to apply | `tests/deploy/test_deployer.py` (refuses non-ancestor or red commits on `prod`; rolls back on a failed restart or a failed check; does not retry a refused head; stagger); a promote dry run on a fork | TD-slice-0c |
| **S5** Deploy: Windows | install (including the deployer and its Task Scheduler timer), WinSW templates, `check-local-only`, `update-models` (through the sidecar), weekly maintenance | Pester with stubbed system commands; a manual node run, output pasted | TD-slice-5 |
| **S6** Deploy: macOS | install (including the deployer and its launchd timer), launchd plists, the same scripts; the Metal-under-LaunchDaemon result recorded | bats with stubbed `lsof`, `launchctl` and `curl`; a manual node run | TD-slice-6 |
| **S7** Gateway core | node config (any number of nodes) with live reload (REQ-44, REQ-45), discovery with unknown states, size routing with `size_overrides`, passthrough, spill-over on the admission marker only, replay-safe failover, client auth, `NODE_REQUIRED`, compatibility headers | R-GW1–R-GW20, run against fleets of 1, 2 and 5 nodes; reload cases (a valid swap, an invalid file kept out, a node added or removed while a request is running, a changed `max_params_b`), using `/api/tags` captured in the PPE from real micro-models; upstream-429 vs admission-429 cases; every-candidate-once with 3 nodes | TD-slice-7 |
| **S8** Gateway combined views | `/api/tags`, `/api/ps` and `/api/version` merges with schemas; partial flags; `/seriatim/nodes`; health | V1–V8 | TD-slice-8 |
| **S9** `/models/select` | task preferences, routing, measured-memory fit (null when not measured) | SEL1–SEL8 | TD-slice-9 |
| **S10** Gateway deploy + two-node PPE | Dockerfile, systemd example, a two-node PPE with real size metadata | P-GW1–P-GW7 (routing, never-too-small, busy isolation between nodes (F1), spill-over, combined views, streaming end to end, failover) | TD-slice-10 |
| **S11** TLS (optional) | certificate and key settings for the sidecar and the gateway; PPE TLS smoke test | T1–T4 | TD-slice-11 |
| **S12** Release 0.1.0 | operator guide, supported Ollama versions, changelog, tag | a full PPE run in CI, plus each node's local-only output | none |

---

## Appendix A — Contract draft (`contract/openapi.yaml`)

S1 commits this file and lints it in CI. The gateway paths are specified now, so that both
components share one envelope and one set of headers. The Ollama API is passed through, not
redefined. Its paths are listed only to state the class each one gets (`x-seriatim-gate`,
`x-seriatim-scope`) and the Seriatim responses it can add.

```yaml
openapi: 3.1.0
info:
  title: Seriatim
  version: 0.2.0
  summary: One-at-a-time GPU gate for Ollama (sidecar + gateway contract)
  license: { name: MIT, identifier: MIT }
servers:
  - url: http://127.0.0.1:11434
    description: sidecar
  - url: http://127.0.0.1:8434
    description: gateway (example port)
security:
  - bearer: []

components:
  securitySchemes:
    bearer: { type: http, scheme: bearer }

  # Machine-readable form of ADR-001 §5.4 (checked by K-OAS3). status null = in-stream only.
  x-seriatim-codes:
    GPU_BUSY:                  { status: 429, retryable: true,  replay_safe: true }
    BAD_PATH:                  { status: 400, retryable: false, replay_safe: true }
    AUTH_AMBIGUOUS:            { status: 400, retryable: false, replay_safe: true }
    BAD_REQUEST_BODY:          { status: 400, retryable: false, replay_safe: true }
    NUM_CTX_AMBIGUOUS:         { status: 400, retryable: false, replay_safe: true }
    NUM_CTX_UNSUPPORTED_ROUTE: { status: 400, retryable: false, replay_safe: true }
    NODE_REQUIRED:             { status: 400, retryable: false, replay_safe: true }
    UNKNOWN_TASK:              { status: 400, retryable: false, replay_safe: true }
    AUTH_MISSING:              { status: 401, retryable: false, replay_safe: true }
    AUTH_INVALID:              { status: 401, retryable: false, replay_safe: true }
    FORBIDDEN_SCOPE:           { status: 403, retryable: false, replay_safe: true }
    NOT_FOUND:                 { status: 404, retryable: false, replay_safe: true }
    MODEL_NOT_FOUND:           { status: 404, retryable: false, replay_safe: true }
    METHOD_NOT_ALLOWED:        { status: 405, retryable: false, replay_safe: true }
    REQUEST_BODY_TIMEOUT:      { status: 408, retryable: true,  replay_safe: true }
    UPLOAD_INTERRUPTED:        { status: 408, retryable: true,  replay_safe: false }
    BODY_TOO_LARGE:            { status: 413, retryable: false, replay_safe: true }
    NUM_CTX_EXCEEDS_LIMIT:     { status: 422, retryable: false, replay_safe: true }
    MODEL_SIZE_UNKNOWN:        { status: 422, retryable: false, replay_safe: true }
    INTERNAL:                  { status: 500, retryable: true,  replay_safe: false }
    UPSTREAM_UNAVAILABLE:      { status: 502, retryable: true,  replay_safe: true }
    UPSTREAM_PROTOCOL_ERROR:   { status: 502, retryable: true,  replay_safe: false }
    NODE_STARTING:             { status: 503, retryable: true,  replay_safe: true }
    EXPOSURE_UNVERIFIED:       { status: 503, retryable: true,  replay_safe: true }
    OLLAMA_EXPOSED:            { status: 503, retryable: false, replay_safe: true }
    NODE_SATURATED:            { status: 503, retryable: true,  replay_safe: true }
    DISCOVERY_INCOMPLETE:      { status: 503, retryable: true,  replay_safe: true }
    GPU_OFFLINE:               { status: 503, retryable: false, replay_safe: true }
    LOCK_HOLD_EXCEEDED:        { status: 504, retryable: true,  replay_safe: false }
    HTTP_VERSION_UNSUPPORTED:  { status: 505, retryable: false, replay_safe: true }
    UPSTREAM_STREAM_BROKEN:    { status: null, retryable: true, replay_safe: false }

  headers:
    Retry-After:
      description: Whole seconds before retrying.
      required: true
      schema: { type: string, pattern: '^[0-9]+$' }
    X-Seriatim-Gate:
      schema: { type: string, enum: [acquired, ungated, busy] }
    X-Seriatim-Node:
      schema: { type: string, pattern: '^[a-z0-9][a-z0-9-]{0,31}$' }
    X-Seriatim-Partial:
      schema: { type: string }
    X-Request-Id:
      schema: { type: string, pattern: '^[A-Za-z0-9._-]{1,128}$' }
    X-Rerouted-From:
      schema: { type: string }
    X-Reroute-Reason:
      schema: { type: string, enum: [busy] }

  schemas:
    ErrorCode:
      type: string
      enum: [GPU_BUSY, BAD_PATH, AUTH_AMBIGUOUS, BAD_REQUEST_BODY, NUM_CTX_AMBIGUOUS,
             NUM_CTX_UNSUPPORTED_ROUTE, NODE_REQUIRED, UNKNOWN_TASK, AUTH_MISSING, AUTH_INVALID,
             FORBIDDEN_SCOPE, NOT_FOUND, MODEL_NOT_FOUND, METHOD_NOT_ALLOWED,
             REQUEST_BODY_TIMEOUT, UPLOAD_INTERRUPTED, BODY_TOO_LARGE, NUM_CTX_EXCEEDS_LIMIT, MODEL_SIZE_UNKNOWN,
             INTERNAL, UPSTREAM_UNAVAILABLE, UPSTREAM_PROTOCOL_ERROR, NODE_STARTING,
             EXPOSURE_UNVERIFIED, OLLAMA_EXPOSED, NODE_SATURATED, DISCOVERY_INCOMPLETE,
             GPU_OFFLINE, LOCK_HOLD_EXCEEDED, HTTP_VERSION_UNSUPPORTED, UPSTREAM_STREAM_BROKEN]
    ErrorEnvelope:
      description: Every error Seriatim itself produces.
      type: object
      required: [error, error_code, retryable, replay_safe, source]
      properties:
        error: { type: string, description: Human-readable; same key Ollama uses }
        error_code: { $ref: '#/components/schemas/ErrorCode' }
        retryable: { type: boolean }
        replay_safe: { type: boolean, description: True only if Ollama never received the request }
        retry_after: { type: integer, minimum: 0 }
        source: { type: string, enum: [sidecar, gateway] }
        node: { type: string }
        busy_for_s: { type: number, minimum: 0 }
        limit: { type: integer, minimum: 1 }
        hint: { type: string }
      additionalProperties: false
    UpstreamError:
      description: >
        An error produced by Ollama, passed through unchanged (status and bytes). Seriatim does
        not constrain it; Ollama's usual shape is shown for client authors.
      type: object
      properties:
        error: { type: string }
    StreamErrorRecord:
      description: >
        Final NDJSON line, or SSE data payload, sent only when framing is safe (SPEC REQ-24);
        always followed by an aborted connection.
      type: object
      required: [error, error_code]
      properties:
        error: { type: string }
        error_code: { type: string, enum: [UPSTREAM_STREAM_BROKEN, LOCK_HOLD_EXCEEDED] }
      additionalProperties: false
    Health:
      type: object
      required: [status]
      properties:
        status: { type: string, enum: [ok, degraded] }
        reasons:
          type: array
          items:
            type: string
            enum: [ollama_unreachable, ollama_bad_response, slot_overdue, slot_stuck,
                   starting, ollama_exposed, exposure_unverified, no_healthy_node]
      additionalProperties: false
    Slot:
      type: object
      required: [state]
      properties:
        state: { type: string, enum: [free, held, draining, stuck] }
        path: { type: [string, 'null'] }
        model: { type: [string, 'null'] }
        request_id: { type: [string, 'null'] }
        started_at: { type: [string, 'null'], format: date-time }
        elapsed_s: { type: [number, 'null'], minimum: 0 }
        drain_remaining_s: { type: [number, 'null'], minimum: 0 }
    NodeStatus:
      type: object
      required: [node, version, starting, slots, memory, loaded_models, ollama]
      properties:
        node: { type: string }
        version: { type: string }
        starting: { type: boolean }
        slots: { type: array, items: { $ref: '#/components/schemas/Slot' } }
        memory:
          type: object
          required: [source, devices]
          properties:
            source: { type: string, enum: [nvidia-smi, apple-unified, unavailable] }
            error: { type: string }
            models_vram_mb: { type: [integer, 'null'], minimum: 0 }
            devices:
              type: array
              items:
                type: object
                required: [name, total_mb, used_mb, free_mb]
                properties:
                  name: { type: string }
                  total_mb: { type: [integer, 'null'], minimum: 0 }
                  used_mb: { type: [integer, 'null'], minimum: 0, description: null unless measured }
                  free_mb: { type: [integer, 'null'], minimum: 0, description: null unless measured }
        loaded_models:
          description: Ollama /api/ps "models", unchanged; null (with loaded_models_error) if unavailable.
          type: [array, 'null']
          items: { type: object }
        loaded_models_error: { type: string }
        ollama:
          type: object
          required: [reachable, exposure]
          properties:
            reachable: { type: boolean }
            version: { type: [string, 'null'] }
            exposure: { type: string, enum: [local_only, exposed, unverified] }
    CombinedTags:
      type: object
      required: [models]
      properties:
        models:
          type: array
          items:
            description: Ollama /api/tags model entry, unchanged, plus seriatim_nodes.
            type: object
            required: [name, seriatim_nodes]
            properties:
              name: { type: string }
              digest: { type: string }
              details: { type: object }
              seriatim_nodes: { type: array, items: { type: string }, minItems: 1 }
        seriatim_partial: { type: array, items: { type: string } }
    CombinedPs:
      type: object
      required: [models]
      properties:
        models:
          type: array
          items:
            description: Ollama /api/ps model entry, unchanged, plus seriatim_node.
            type: object
            required: [name, seriatim_node]
            properties:
              name: { type: string }
              seriatim_node: { type: string }
        seriatim_partial: { type: array, items: { type: string } }
    CombinedVersion:
      type: object
      required: [version, seriatim_nodes]
      properties:
        version: { type: string, description: Lowest version among the nodes that answered }
        seriatim_nodes: { type: object, additionalProperties: { type: string } }
        seriatim_partial: { type: array, items: { type: string } }
    SelectRequest:
      type: object
      required: [task]
      properties:
        task: { type: string }
        context_tokens: { type: integer, minimum: 1 }
      additionalProperties: false
    SelectResponse:
      type: object
      required: [model, node, fits_in_free_memory, reasons]
      properties:
        model: { type: string }
        node: { type: string }
        fits_in_free_memory: { type: [boolean, 'null'] }
        reasons: { type: array, items: { type: string } }
        alternatives:
          type: array
          items:
            type: object
            properties: { model: { type: string }, node: { type: string } }

  responses:
    Busy:
      description: >
        Either Seriatim's admission 429 (X-Seriatim-Gate busy, ErrorEnvelope with GPU_BUSY;
        Ollama did not receive the request) or Ollama's own 429 passed through
        (X-Seriatim-Gate acquired, UpstreamError). Only the first triggers spill-over.
      headers:
        Retry-After: { description: Always present on the admission 429, schema: { type: string, pattern: '^[0-9]+$' } }
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json:
          schema:
            anyOf:
              - $ref: '#/components/schemas/ErrorEnvelope'
              - $ref: '#/components/schemas/UpstreamError'
    Unavailable:
      description: >
        Seriatim 503 (NODE_STARTING, EXPOSURE_UNVERIFIED, NODE_SATURATED, DISCOVERY_INCOMPLETE
        and GPU_OFFLINE carry Retry-After; OLLAMA_EXPOSED does not), or Ollama's own 503 passed
        through.
      headers:
        Retry-After: { schema: { type: string, pattern: '^[0-9]+$' } }
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json:
          schema:
            anyOf:
              - $ref: '#/components/schemas/ErrorEnvelope'
              - $ref: '#/components/schemas/UpstreamError'
    Error:
      description: >
        A Seriatim-produced error (ADR-001 §5.4), or, on proxied routes, Ollama's own error
        with the same status passed through. A body with error_code is Seriatim's.
      headers:
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json:
          schema:
            anyOf:
              - $ref: '#/components/schemas/ErrorEnvelope'
              - $ref: '#/components/schemas/UpstreamError'
    Unauthorized:
      description: Missing or invalid key.
      headers:
        WWW-Authenticate: { schema: { type: string, const: Bearer } }
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content: { application/json: { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } }
    Passthrough:
      description: >
        Ollama's response: status and body bytes unchanged, including Ollama's own errors
        (UpstreamError). Streams are forwarded incrementally; a mid-stream failure ends with a
        StreamErrorRecord when framing is safe, then an aborted connection.
      headers:
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json: { schema: { $ref: '#/components/schemas/UpstreamError' } }
        application/x-ndjson: {}
        text/event-stream: {}
        text/plain: {}
        application/octet-stream: {}

  x-seriatim-gated-operation: &gated
    x-seriatim-gate: gated
    x-seriatim-scope: infer
    requestBody:
      description: Opaque Ollama request; forwarded byte-for-byte. Seriatim may read
        `num_ctx` and `options.num_ctx` (slice S3) but never rewrites the body.
      content: { application/json: {} }
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '408': { $ref: '#/components/responses/Error' }
      '413': { $ref: '#/components/responses/Error' }
      '422': { $ref: '#/components/responses/Error' }
      '429': { $ref: '#/components/responses/Busy' }
      '500': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      '504': { $ref: '#/components/responses/Error' }
      '505': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }
  x-seriatim-ungated-operation: &ungated
    x-seriatim-gate: ungated
    x-seriatim-scope: infer
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      default: { $ref: '#/components/responses/Passthrough' }
  x-seriatim-admin-operation: &admin
    x-seriatim-gate: ungated
    x-seriatim-scope: admin
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      default: { $ref: '#/components/responses/Passthrough' }

# Unlisted method+path pairs are gated and need the admin scope (SPEC REQ-5).
x-seriatim-unknown-endpoint: { x-seriatim-gate: gated, x-seriatim-scope: admin }

paths:
  /seriatim/health:
    get:
      x-seriatim-roles: [sidecar, gateway]
      security: []
      responses:
        '200': { description: Healthy, content: { application/json: { schema: { $ref: '#/components/schemas/Health' } } } }
        '503': { description: Degraded, content: { application/json: { schema: { $ref: '#/components/schemas/Health' } } } }
  /seriatim/status:
    get:
      x-seriatim-roles: [sidecar]
      responses:
        '200': { description: Node status, content: { application/json: { schema: { $ref: '#/components/schemas/NodeStatus' } } } }
        '401': { $ref: '#/components/responses/Unauthorized' }
  /seriatim/nodes:
    get:
      x-seriatim-roles: [gateway]
      responses:
        '200':
          description: Per-node health and status
          content:
            application/json:
              schema:
                type: object
                required: [nodes, config]
                properties:
                  config:
                    description: Live-reload state (SPEC REQ-45)
                    type: object
                    required: [loaded_at]
                    properties:
                      loaded_at: { type: string, format: date-time }
                      last_error: { type: [string, 'null'] }
                      last_error_at: { type: [string, 'null'], format: date-time }
                  nodes:
                    type: array
                    items:
                      type: object
                      required: [id, healthy, priority, last_check]
                      properties:
                        id: { type: string }
                        healthy: { type: boolean }
                        priority: { type: integer }
                        max_params_b: { type: [number, 'null'] }
                        last_check: { type: string, format: date-time }
                        status: { $ref: '#/components/schemas/NodeStatus' }
        '401': { $ref: '#/components/responses/Unauthorized' }
  /models/select:
    get:
      x-seriatim-roles: [gateway]
      parameters:
        - { name: task, in: query, required: true, schema: { type: string } }
        - { name: context_tokens, in: query, schema: { type: integer, minimum: 1 } }
      responses:
        '200': { description: Selection, content: { application/json: { schema: { $ref: '#/components/schemas/SelectResponse' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
    post:
      x-seriatim-roles: [gateway]
      requestBody: { required: true, content: { application/json: { schema: { $ref: '#/components/schemas/SelectRequest' } } } }
      responses:
        '200': { description: Selection, content: { application/json: { schema: { $ref: '#/components/schemas/SelectResponse' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }

  # Ungated, infer scope. At the sidecar these are raw passthrough; at the gateway,
  # /api/tags, /api/ps and /api/version are combined (REQ-38, schemas Combined*).
  # At the gateway, / answers 200 "Ollama is running" if at least one node is healthy, else 503.
  /:            { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] }, head: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /api/tags:    { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-gateway-schema: CombinedTags } }
  /api/ps:      { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-gateway-schema: CombinedPs } }
  /api/version: { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-gateway-schema: CombinedVersion } }
  /api/show:    { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/models:   { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }

  # Gated inference, infer scope (REQ-5).
  /api/generate:        { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/chat:            { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/embed:           { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/embeddings:      { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/chat/completions: { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/completions:      { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/embeddings:       { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }

  # Ungated, admin scope (REQ-14). Through the gateway they need X-Seriatim-Node (REQ-41).
  /api/pull:   { post: { <<: *admin, x-seriatim-roles: [sidecar, gateway] } }
  /api/push:   { post: { <<: *admin, x-seriatim-roles: [sidecar, gateway] } }
  # create converts and quantises models, which loads the machine, so it is gated (admin scope).
  /api/create: { post: { <<: *gated, x-seriatim-scope: admin, x-seriatim-roles: [sidecar, gateway] } }
  /api/copy:   { post: { <<: *admin, x-seriatim-roles: [sidecar, gateway] } }
  /api/delete: { delete: { <<: *admin, x-seriatim-roles: [sidecar, gateway] } }
  /api/blobs/{digest}:
    parameters:
      - { name: digest, in: path, required: true, schema: { type: string, pattern: '^sha256:[0-9a-f]{64}$' } }
    head: { <<: *admin, x-seriatim-roles: [sidecar, gateway] }
    post: { <<: *admin, x-seriatim-roles: [sidecar, gateway] }
```

The YAML anchors under the `x-` keys are only a convenience in this draft;
`openapi-spec-validator` accepts the merged result. S1 keeps them, or expands them if a future
validator rejects merge keys. Either way, K-OAS1 checks that the file's gate and scope classes
agree with `classify()`.
