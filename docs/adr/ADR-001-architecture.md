# ADR-001: Seriatim architecture

**Status:** revision 6, proposed. **Date:** 2026-11-16.
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
| D3 | Busy means an **immediate admission 429**, marked `X-Seriatim-Gate: busy`. There is no queue. The gateway fails over by **one rule**: its own connect failure, or a replay-safe sidecar envelope whose code and gate value are in SPEC REQ-37's table. |
| D4 | **Byte-for-byte passthrough.** `num_ctx` is never changed; an over-limit value is rejected. |
| D5 | The gateway is a **thin router**: it routes by the operator's eligibility rules, and merges the read-only views **in Ollama's own shapes**; Seriatim's data lives on `/seriatim/*`. |
| D6 | **Bearer keys** with `infer` and `admin` scopes. Nodes store hashes. Each node has its own keys. |
| D7 | **Python 3.12, Starlette, uvicorn (h11), httpx, anyio and pydantic-settings.** A 30-line OS lock (`fcntl.flock` / `msvcrt.locking`) for the singleton. WinSW and launchd run the services. |
| D8 | **The contract comes first** (Appendix A), plus a black-box behaviour suite, so another implementation can drop in. |
| D9 | **Config-driven deploy scripts.** The local-only check runs after every Ollama upgrade. Maintenance goes through the sidecar. |
| D10 | **Abnormal ends and restarts drain.** A slot is freed only after the upstream connection is closed and a bounded drain has passed. |
| D11 | **One sidecar per Ollama**, enforced by a host-wide file lock. |
| D12 | **Exact classification.** Only verified method and path pairs are ungated. Everything unknown is gated and needs `admin`. Unsafe raw paths are rejected before matching. |
| D14 | **Measured fit is deferred.** Sidecars measure and report capacity now; admission from it is a later slice, calibrated on real nodes. Until then, routing uses the operator's rules and fails closed without them. |
| D16 | **Releases deploy.** CI builds verified, attested releases from `main`; a release follower installs them. Until it has passed PPE, hosts follow `ppe`/`prod` branches, changed only by maintainer-approved pull requests (`prod` from `main` only), through self-hosted runners behind approval. |
| D15 | **Metrics through the host's exporter.** Sidecar and gateway write Prometheus text files for the standard `node_exporter` / `windows_exporter` textfile collector; Seriatim opens no metrics port. |

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

### D3. Busy means an immediate admission 429; failover by one rule

**Decision.**
- A gated request with no free slot gets 429 at once, decided from the method and raw path
  alone, before the body is read.
- The admission 429 carries `X-Seriatim-Gate: busy` and an envelope with `error_code:
  GPU_BUSY`, `source: sidecar` and `replay_safe: true`.
- The sidecar **strips every `X-Seriatim-*` header from Ollama's responses**, so Ollama cannot
  forge the marker.
- **Other refusals.** Seriatim's other answers given before Ollama is contacted (offline,
  exposure, starting, a `num_ctx` limit) carry `X-Seriatim-Gate: refused`. They too are
  answered before the body is read, except the `num_ctx` check, which needs it.
- **One failover rule** (SPEC REQ-37). The gateway tries the next eligible node exactly
  when its own connect failed (recorded as 502 `UPSTREAM_UNAVAILABLE`, `source: gateway`), or
  the node answered with an envelope whose `source` is `sidecar`, whose `replay_safe` is
  true, and whose code and `X-Seriatim-Gate` value are one of the listed pairs: `GPU_BUSY`
  with `busy`; the refusals with `refused`; `NODE_SATURATED` with `ungated`; the sidecar's
  connect-stage `UPSTREAM_UNAVAILABLE` with `acquired` or `ungated`. The gate header alone,
  or the status code alone, never decides.
- It tries each eligible node once, in priority order, and never waits (ruling Q1). An
  Ollama 429 is passed through untouched. `X-Reroute-Reason` is the first tried node's
  reason.

**Alternatives rejected.**
- **A short wait-and-retry queue.** This is the hidden wait the evaluation measured against
  LiteLLM (30 s) and Olla (28 s).
- **Spilling over on any 429.** An Ollama 429 or 503 may mean Ollama *did* receive the request,
  so retrying it elsewhere could run it twice.
- **Marking every failover answer `refused`.** `NODE_SATURATED` and the sidecar's
  connect-stage `UPSTREAM_UNAVAILABLE` would then lie about the gate (`ungated`, `acquired`).
  The code-and-gate table keeps the header's meaning and still names every case.
- **503 without `Retry-After`** (ollama_load_balancer). It doesn't say when to retry, and it
  means "down", not "busy".
- **An estimated `Retry-After`.** Generation lengths can't be predicted. The value is static
  and configurable (default 15 s), and is extended to cover any drain still in progress.

### D4. Byte-for-byte passthrough; `num_ctx` rejected, never changed

**Decision.**
- Bodies are forwarded as the exact bytes received, and responses as raw bytes, without
  decoding.
- A body is parsed only to check `num_ctx` on the sidecar (S3), and on the gateway to read
  `model` for routing (D5). The parse is strict, and never changes the bytes sent.
  - **The sidecar** rejects duplicate keys, wrong types and conflicting placements of
    `num_ctx` as `NUM_CTX_AMBIGUOUS`.
  - **The gateway** rejects invalid JSON, duplicate top-level keys, and a missing, empty
    or non-string `model` as `BAD_REQUEST_BODY` (SPEC REQ-32).
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
- **Routing.** Candidates are the nodes that are healthy, list the model, and are eligible
  for it under the operator's rules: a per-model eligible-nodes list, `capacity =
  "unlimited"`, or `max_params_b`; `capacity = "listed_only"` keeps a node for its listed
  models only (SPEC REQ-32). A node with no rule is excluded. Candidates
  are tried in priority order.
- **Discovery.** A node whose discovery failed is in an *unknown* state, never treated as "has
  no models". So 404 is given only when every node answered (REQ-34).
- **The fleet is configuration.** The gateway knows only the nodes its config lists, with
  their URLs, keys, priorities and size rules. Each sidecar's measured capacity is shown next
  to those rules (D14). Nothing in the code names a node, a count, a GPU model, a platform or a
  memory size. A changed file is validated and swapped in atomically without a
  restart (SPEC REQ-44, REQ-45). A home lab's machines change, so a fixed topology would go
  stale.
- **Merged views, in the standard shapes.** `/api/tags`, `/api/ps`, `/api/version` and
  `/v1/models` are merged across nodes, but every body has exactly Ollama's or OpenAI's
  schema, with no Seriatim fields (a maintainer decision, SPEC REQ-38). Per-node detail is on
  `/seriatim/models` and `/seriatim/nodes`, and a partial result is flagged only by
  `X-Seriatim-Partial`. Clients that validate strictly are therefore never surprised.
  - *Rejected:* adding `seriatim_*` fields to the standard bodies (revision 4). Lenient
    clients ignore them, but strict ones may not, and a standard route should not carry a
    private extension.
- **`/models/select`** is served from configuration and the same eligibility rules.

**Alternatives rejected.**
- **Parsing the model name** (`:32b`). Names are free text, and Ollama's metadata is the
  authority.
- **An estimated token count**, as an earlier design used (`len(prompt)/4`). Too inaccurate to
  choose a machine by.
- **Discovery alone.** It can't keep a model off a machine too small for it when several
  machines have it.
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
- **Ansible or a similar tool.** Too heavy for a home lab's handful of machines, and a
  configuration-management dependency the operator may not want.

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
  requests, so it can't show that a cancelled request has stopped. A later slice may adopt a positive
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

### D14. Measured fit: deferred, with open problems

**Status.** Deferred to its own slice, "Measured fit", after S4b ships. It gets its own TD and
review.

**Context.** Revision 3 had the gateway derive fit itself: model bytes plus an estimated KV
cache, times a 25 % margin, compared with each node's measured capacity, with
`MODEL_TOO_LARGE` and `CAPACITY_UNKNOWN` as routing verdicts. The revision-3 review showed
that the rule was not yet sound (problems 1 to 3 below). A rule that guesses and fails
closed can refuse work on a healthy fleet, while one that guesses and lets work through
breaks SPEC REQ-33.

**Decision for Phase 1.**
- **Measure now.** S4b ships the capacity probes (SPEC REQ-16). Each sidecar reports its
  capacity, with source and basis, and `/seriatim/nodes` shows it next to the configured
  rules, together with each loaded model's `size` and `size_vram`.
- **Route by the operator's rules.** S7 routes by a per-model eligible-nodes list,
  `capacity = "unlimited"`, `capacity = "listed_only"` or `max_params_b` (SPEC REQ-32). A node with none of them is
  excluded, with the distinct verdict `no_size_policy` and the error
  `SIZE_POLICY_MISSING`. Nothing hardware-specific is built in (SPEC REQ-44).
- **Then calibrate.** Once S4b runs on real NVIDIA and Apple nodes, their capacity
  recordings, together with the `size_vram` that Ollama actually used for each model and
  context, become the calibration data for the "Measured fit" slice.

**Open problems for the "Measured fit" slice** (from the revision-3 review, items 5 to 7;
not solved here):
1. **A single 25% margin is wrong in both directions.**
   - Applied to `weights_bytes`, it refuses a 32B Q4 model on a 24 GiB card even at 4k
     context.
   - It is still not a bound: graph and compute buffers, batch size, multimodal projectors,
     `options.num_gpu`/`main_gpu`, and `OLLAMA_NUM_PARALLEL` (which multiplies KV) all fall
     outside it.

   So REQ-33's "never too small" isn't established, and a guessed rule that fails closed
   can refuse work on a healthy fleet.
2. **Long-context models get refused by default.** With no node context default, the
   effective `n_ctx` falls back to the model's maximum (128k+), which inflates KV for any
   `/v1/*` request or any native request without `num_ctx`.
3. **Unsupported layouts and a failed `/api/show` have no complete error path.** MLA,
   SWA/hybrid, embedding and vision models are refused or misestimated. A node whose tags
   list the model but whose `/api/show` fails gets no defined verdict.

**What the later slice starts from.** These are recorded as starting points, not
decisions:
- **The candidate formula:** revision 3's `(weights + KV) × (1 + margin)`.
- **The review findings already folded in:**
  - separate key and value lengths;
  - an `f16` cache unless the effective type can be verified;
  - context padding;
  - no "already loaded fits" shortcut;
  - the largest single device on multi-GPU nodes.

**Alternatives considered for Phase 1.**
- **Ship revision 3's rule anyway.** Rejected for the reasons above.
- **A hard-coded default policy**, such as a fraction of measured memory. Rejected because
  it is a hardware assumption and still unvalidated.
- **No size check at all** until the slice lands. Rejected because it reopens E3, a model
  sent to a machine too small for it.
- **Operator rules, with measurements shown beside them** (chosen). Correct by construction
  relative to the operator's choice, visible, and it produces the data the later slice
  needs.

**Consequences.**
- **Every node needs a rule.** Each node needs a rule for the models it should serve,
  either an eligible-nodes entry, `unlimited` or `max_params_b`. The operator guide and
  `/seriatim/nodes` warnings say so.
- **The guarantee is relative.** REQ-33 holds exactly, but only relative to those rules.

### D15. Metrics through the host exporter's textfile collector

**Context.** Operators already run Prometheus with the standard host exporters
(`node_exporter`, `windows_exporter`), and add service metrics through their textfile
collectors. Seriatim's own surface is deliberately small: one port on each node, every
route but health authenticated, and an exposure check (D2, D6, D12).

**Decision.**
- **A file, not a port.** The sidecar and the gateway each write a `.prom` file atomically,
  every 15 s, into a configured textfile-collector directory (SPEC REQ-67).
- **No `/metrics` route.** Seriatim adds no metrics route and no metrics port.
- **Bounded labels.** Only bounded, non-secret labels are used: node ids, states, error
  codes, path kinds. Model names and key ids are never labels.

**Alternatives rejected.**
- **A `/metrics` HTTP route on each sidecar.** It needs either a new unauthenticated route,
  which breaks "everything but health needs a key", or bearer configuration in every
  scraper. It also gives a LAN attacker a new target, and its exposure would need checking
  too.
- **A separate exporter process.** It is one more service to deploy and supervise on every
  OS, and it would still need to read the sidecar's state from somewhere.
- **Push to a Pushgateway.** It needs network egress and a central component, and stale
  series must be cleaned up by hand.

**Consequences.**
- **No exporter, no metrics.** A host without an exporter gets none, and `/seriatim/status`
  still works.
- **A short lag.** Metrics lag by up to 15 s plus the scrape interval. That is fine for
  dashboards and alerts, and `/seriatim/status` is the real-time view.

### D16. Releases deploy; an interim branch deploy until the follower is proven

**Decision.**
- **Releases are the deployable unit** (SPEC §7.6): built by CI from commits on `main`, with
  checksums and attestations, verified by every installer, images by digest.
- **A release follower deploys them** (SPEC §7.7). It is pull-based, so it needs no credential
  that can push code or bypass a ruleset.
- **Until the follower has passed PPE**, each host follows a `ppe` or `prod` branch through a
  self-hosted runner (SPEC §7.8). Those branches change only by a pull request the
  maintainer approves, never from a fork. The flow is branch → `ppe` → `main` → `ppe` →
  smoke test → `prod`, with the maintainer as the gate.
  - **`ppe` from a promotion builder.** Any branch of this repository reaches `ppe` through a
    `promote/ppe-*` branch whose commit has exactly the source's tree, so `ppe` never keeps an
    abandoned branch's changes. Every `ppe` deployment wipes the PPE project's own resources.
  - **`prod` from `main` only,** by plain pull request.
  - **Workflows only from `main`.** A `ppe` promotion whose `.github/` differs from `main`'s is
    refused.
  - **A required `promotion-source` check,** bound to GitHub Actions, enforces all of it.
- **The builder runs locally, as the `seriatim-worker` App.** The dev session runs
  `deploy/promote-ppe.py` at the maintainer's request, with a short-lived App token. The bot
  authors each promotion pull request, so the maintainer can approve it.

**Alternatives rejected.**
- **A promotion App with ruleset bypass** (revision 2). Its key, reachable from PPE, would
  have prod push authority, and PPE status could not be authenticated. Both were blocking.
- **Waiting for the follower before any real-host deployment.** It would leave the gateway
  and sidecars untested on real hosts until after S12.
- **Merging branches straight into `ppe`.** After a tested branch is abandoned, the next merge
  keeps its changes, the tree check fails forever, and the ruleset forbids a reset.
- **A `workflow_dispatch` builder.** It would need the App's private key in Actions secrets,
  where any workflow change reviewed onto `main` could read it. `GITHUB_TOKEN` is no
  substitute, because what it creates triggers no workflows, so `promotion-source` would never
  run. The local script keeps the key off GitHub entirely.

**Consequences.**
- **A public repository with self-hosted runners** is the main interim risk (T17). Its
  controls are all required, and the mechanism is retired host by host.
- **An automated "rc passed PPE" gate**, and an automated "this `main` commit passed PPE"
  gate for `prod`, stay open problems (SPEC §7.7, §7.8). The maintainer checks both by hand.
- **Promotion needs the dev session,** or anyone holding an App token, to run the builder.
  That is deliberate: the maintainer asks for each one.

## 4. Components

| component | package | runs on | state |
|---|---|---|---|
| sidecar | `node/seriatim_node` | each node (any OS with Python and Ollama) | slots (in memory), the singleton lock file (runtime directory), `node-state.json` in a separate persistent state directory (the admin switch), config |
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
| `Retry-After` | 429, and 503 except `OLLAMA_EXPOSED`, `SIZE_POLICY_MISSING`, `NODE_OFFLINE` and `GPU_OFFLINE` | whole seconds |
| `X-Seriatim-Gate` | every sidecar response | `acquired` (the request held a slot), `ungated`, `busy` (the admission 429), or `refused` (another refusal before Ollama was contacted: offline, exposure, starting, `num_ctx` limit) |
| `X-Seriatim-Node` | gateway responses; also a request header for admin calls | the node that served the request, or the node to target |
| `X-Seriatim-Partial` | gateway merged views (`/api/tags`, `/api/ps`, `/api/version`, `/v1/models`) | comma-separated ids of nodes that didn't answer, or whose answer didn't validate and was left out; the only partial signal, because those bodies keep Ollama's shape |
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
| `BAD_REQUEST_BODY` | 400 | no | yes | both (gateway: before routing, SPEC REQ-32) |
| `NUM_CTX_AMBIGUOUS`, `NUM_CTX_UNSUPPORTED_ROUTE` | 400 | no | yes | sidecar |
| `NODE_REQUIRED`, `UNKNOWN_TASK` | 400 | no | yes | gateway |
| `AUTH_MISSING`, `AUTH_INVALID` | 401 | no | yes | both |
| `FORBIDDEN_SCOPE` | 403 | no | yes | both |
| `NOT_FOUND` (a reserved `/seriatim/*` path, or an unknown node id in `X-Seriatim-Node` or `/seriatim/admin/nodes/{id}/*`) | 404 | no | yes | both |
| `MODEL_NOT_FOUND` | 404 | no | yes | gateway |
| `METHOD_NOT_ALLOWED` | 405 | no | yes | both |
| `REQUEST_BODY_TIMEOUT` | 408 | yes | yes | both |
| `UPLOAD_INTERRUPTED` (a streamed blob upload, after bytes were forwarded) | 408 | yes | **no** | both |
| `BODY_TOO_LARGE` | 413 | no | yes | both |
| `NUM_CTX_EXCEEDS_LIMIT` | 422 | no | yes | sidecar |
| `MODEL_SIZE_UNKNOWN` | 422 | no | yes | gateway |
| `MODEL_NOT_ELIGIBLE` | 422 | no | yes | gateway |
| `INTERNAL` | 500 | yes | **no** | both |
| `UPSTREAM_UNAVAILABLE` | 502 | yes | yes (the connect failed) | both |
| `UPSTREAM_PROTOCOL_ERROR` | 502 | yes | **no** | both |
| `NODE_STARTING` | 503 | yes | yes | sidecar |
| `NODE_OFFLINE` (the admin switch, SPEC REQ-19a) | 503 | no | yes | sidecar |
| `EXPOSURE_UNVERIFIED` | 503 | yes | yes | sidecar |
| `OLLAMA_EXPOSED` | 503 | **no** | yes | sidecar |
| `NODE_SATURATED` (the ungated connection pool is full) | 503 | yes | yes | sidecar |
| `DISCOVERY_INCOMPLETE` | 503 | yes | yes | gateway |
| `SIZE_POLICY_MISSING` | 503 | no | yes | gateway |
| `GPU_OFFLINE` (a node that could serve the model is down or offline; no `Retry-After`) | 503 | **no** | yes | gateway |
| `FLEET_UNAVAILABLE` (a route with no model, such as `/` or a merged view: no node answered; `Retry-After: 15`) | 503 | yes | yes | gateway |
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
| T12 | Information disclosure through status, errors and metrics | Health, the 429 body; the metrics file (REQ-67) carries no keys, addresses or model names, but is served to anyone who can reach the host exporter | Health gives only a status and reasons; the 429 shows only `busy_for_s`; status, `/seriatim/nodes` and `/seriatim/models` need a key, and an `infer` key sees the operator's offline reason, key ids and other clients' slot details as `null` (SPEC REQ-5); the exporter's own listener, authentication and firewall are the boundary for metrics, and the docs say so | Key holders see which models are loaded |
| T13 | Elevation: two sidecars on one Ollama | A second instance on another port, or with another lock directory | The host-wide OS lock in a fixed directory (D11); the local-only check's one-process check | Containers with separate filesystems (D11) |
| T14 | Supply chain | A malicious dependency or Action | `uv.lock`; Actions pinned by SHA; gitleaks verified by checksum; releases are built only by CI from commits on `main`, with SHA-256 checksums and build-provenance attestations, and installers and the follower verify both before installing; images are deployed by digest, never by tag; Dependabot updates are never auto-merged (SPEC §7.6, REQ-68) | A compromise between pinned updates |
| T16 | DoS: the admin switch takes nodes out of service | A stolen admin key calls `offline` on every node | The switch is `admin` scope on the sidecar and the gateway; each change is logged with the key id; read-only routes stay available, so the state is visible; `online` reverses it, including after lost state, which starts a new epoch (SPEC REQ-19a, §6.1a); routing errors never show the operator's reason | Admin key holders are trusted; they can already delete models |
| T17 | Elevation: untrusted code reaches a self-hosted runner (interim deployment) | A pull request adds a workflow with `runs-on: self-hosted` and no `environment:` key, which deployment approval never sees; a fork, an unreviewed branch, or a branch that rewrites `deploy.yml`, is promoted | Two runner groups, PPE only `deploy.yml@refs/heads/ppe` and production only `deploy.yml@refs/heads/prod`, so a `ppe` promotion can't reach the production runner; fork workflows need approval for all external contributors; `ppe`/`prod` change only by a pull request the code owner approves, with no bypass; `promotion-source` (on `pull_request_target`, `contents: read`, no checkout, bound to GitHub Actions, run from `main`, the protected default branch) refuses any fork, any `ppe` source but a `promote/ppe-*` commit with exactly its source's tree, **any `ppe` tree whose `.github/` differs from `main`'s**, and any `prod` source but `main`; `deploy.yml` runs only on `push` to `ppe`/`prod`, with `contents: read`, no secrets, an approved environment and the pushed SHA, and fails closed unless the merge adds nothing, the pull request came from this repository with its base equal to the pushed branch, and, for `prod`, the head is a `main` commit; a non-administrator, preferably ephemeral runner (SPEC §7.8, §7.9) | A branch deployed to `ppe` runs on the PPE host, but only after the maintainer approves both the pull request and the environment, and only with `main`'s workflows; forks can never be promoted; `prod` always runs a `main` commit, which passed every required check; retired with the follower |
| T18 | Tampering: a compromised, substituted or relabelled release asset (installers, follower) | A replaced wheel or image; an older release or an rc offered under a newer or full-release name | Installers and the follower verify the checksum and an attestation bound to this repository, `release.yml`, `refs/tags/<expected tag>`, that tag's commit and the artifact digest, with self-hosted runners denied; the package and image versions must equal the tag; the release channel takes only full `vX.Y.Z` releases; tags are immutable and releases immutable; only the `release` job holds publish permissions (SPEC REQ-68, REQ-68a) | A compromise of the release workflow itself, or of the maintainer's approval |
| T19 | Tampering: a downgrade or rollback attack (follower) | An attacker offers an older, vulnerable release | SPEC REQ-69: the follower installs only versions newer than its durable last-good record, in semantic-version order per channel; an older one only by an operator pin, by tag and digest, in the host's local config, never read from GitHub; held versions are never retried | A pinned old version is the operator's choice |
| T20 | DoS: the GitHub API is unavailable (follower) | An outage, a rate limit, or a failed attestation lookup | SPEC REQ-69: the follower keeps running the current version, retries with backoff, reports the condition, and never installs anything it could not fully verify; rollback uses the cached, digest-checked last-good artifacts, with no network | No updates during the outage |
| T21 | Elevation: misuse of the promotion builder's App identity (interim deployment) | A leaked `seriatim-worker` token pushes a `promote/ppe-*` branch or opens a promotion pull request | The token is short-lived, minted per use outside the repository, and never stored in Actions secrets; the App bypasses no ruleset but `promote-branches`, so it can't push `main`, `ppe`, `prod` or a `v*` tag; only it can create `promote/**` branches, and the builder refuses any whose `.github/` differs from `main`'s, so no counterfeit `promotion-source` job can ride on a promotion; a promotion still needs the maintainer's approval of the pull request and of the `ppe` environment, `promotion-source`, and `deploy.yml`'s checks (SPEC REQ-70, D16) | A leaked token can open pull requests and push working branches until it expires; nothing merges without the maintainer |
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
7. **An operator rule can be wrong.** A too-generous `max_params_b` or eligible-nodes entry
   lets a model reach a machine too small for it. `/seriatim/nodes` shows the measured
   capacity and each loaded model's `size_vram` next to the rule, so a model partly on
   the CPU is visible (D14).
8. **Capacity is never invented.** A probe that can't measure reports `unavailable` or
   `none`. In Phase 1 that only affects what `/seriatim/nodes` shows, not routing.

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
| **S0a** Tooling and CI | Root `pyproject.toml` (uv workspace; ruff, pytest and coverage settings; a dev group with the test tools and `mutmut>=3.3,<4`); `node/pyproject.toml` with pinned runtime dependencies and the `[tool.mutmut]` settings (TD-slice-1 Appendix B); `node/seriatim_node/__init__.py` and `__main__.py`; `uv.lock`; `.env.example` with the `SERIATIM_NODE_*` names; `.github/workflows/ci.yml` (lint, test matrix, the TD test-ID check `td-test-ids` reading the ubuntu leg's JUnit, coverage, mutation with the score parser, contract lint (OpenAPI 3.1), IP literals, PR size, and the `ci` roll-up) with its scripts under `tools/ci/`; `tests/conftest.py` (the `td` marker); an `Enforced: yes|no` line in each TD (SPEC REQ-63); `.github/workflows/private-terms.yml` (private terms, `pull_request_target`); `.github/workflows/security.yml` (Bandit, pip-audit, `trivy fs --scanners vuln,misconfig,secret`, the ignore-file lint and the `security` roll-up, under the failure policy of SPEC REQ-63; weekly schedule, also added to `secret-scan.yml`), with `.security-ignore.toml` and `tools/ci/security_ignore.py`; both on PRs and on pushes to `main`; `.github/workflows/release.yml` (REQ-68: tags from `main` only, sdist and wheels, checksums, attestation, pre-release and release channels); `.github/dependabot.yml` (SPEC §7.9, Dependabot row); `.github/pull_request_template.md` | Each gate shown failing on a deliberate bad commit, recorded in the PR. S0a ships as several PRs, each within the size limit: part 1 (workspace, node skeleton, `ci.yml`), part 1b (`private-terms`, `td-test-ids`), part 2 (`security.yml`, Dependabot, the PR template, `.env.example`) and the release workflow (`release.yml`) | none (ruling Q8) |
| **S0b** PPE, fixtures and the PPE job | `ppe/compose.yaml` (one Ollama + capture, real micro-models, limits, localhost ports); `ppe/ppe` (up/down/purge, effective-config guard, `--ci` only on GitHub Actions, allowed-host for local runs); `tools/capture_fixtures.py` (fixtures plus a probe of every classified route); `tests/fixtures/route-pairs.json` (the REQ-5 pairs); `tests/fixtures/ollama-0.40.16.json`, including the plain response shapes of `/api/tags`, `/api/ps`, `/api/version` and `/v1/models` that REQ-38 and REQ-62a check against; the `ppe` job in `ci.yml` (required, model cache; it writes JUnit as `junit-ppe`, which `td-test-ids` also reads). S0b ships as two PRs: the PPE, the guard and the job (#9), then the capture tool and the fixtures it records (#26) | `tests/tools/test_ppe_guard.py` (every refusal in SPEC §8.3); `tests/tools/test_capture.py` (cleaning, format) | none (Q8); formats in TD-slice-1 App. B |
| **S1** Contract + sidecar gate, 429, passthrough | `contract/openapi.yaml`; `node/`: config, classification, gate with drain, proxy (streaming, disconnects, hold limit, framing-safe errors), startup quarantine, singleton, health, logs | TD-slice-1 §8: C, K, G, H, B, S, F, E, R, HL, L, K-OAS; PPE P1–P7 | **TD-slice-1** |
| **S2** Sidecar auth | keys (hashes, ids, scopes), `keygen`, 401/403, `AUTH_AMBIGUOUS`, constant-time comparison against every hash, the hourly unauthenticated warning | A1–A12: every route class × {missing, wrong, infer, admin} key; ambiguous credentials; key never in logs or upstream; startup refusals | TD-slice-2 |
| **S3** `num_ctx` policy | strict body inspection (duplicate keys, types, conflicts); limits by pattern with an overlap check; `/v1` rule; the holder's model in status | N1–N10, with wire-capture proof in the PPE that the bytes are unchanged | TD-slice-3 |
| **S4a** Exposure and status | `/seriatim/status` (REQ-17) with the redaction for `infer` keys (REQ-5). Until S4b and S4c land, status reports `capacity` as `source: unavailable` with the error `capacity probe not built`, and `admin_state` as online in epoch `000…0` (32 zeros), revision 0, both valid against the contract; the exposure refusals with `X-Seriatim-Gate: refused`; `/api/ps` error state; the fail-closed exposure check (REQ-19); health extensions for exposure (REQ-18); non-loopback listen enabled | X1–X6 (exposed, unverifiable, timeout, incomplete, first-check gating, recovery); ST1–ST3 (`/api/ps` error state, slot details, redaction for `infer` keys); the staged `capacity` and `admin_state` placeholders validate | TD-slice-4a |
| **S4b** Capacity reporting | `capacity` in `/seriatim/status` (REQ-16), for information (depends on S4a); pluggable probes: NVIDIA (inventory A, compute apps, `/api/ps`, inventory B; the service's process tree via `psutil`), Apple (`hw.memsize`, the wired limit, Metal `recommendedMaxWorkingSetSize`, pressure), `none` and `unavailable`; the fixture-replay probe for tests; `default_num_ctx`, `kv_cache_type` and `flash_attention` | M1–M16 from recorded probe fixtures, with no hardware: an idle GPU (no process rows), one and two devices, Ollama's tree vs another process, a PID outside the tree with an Ollama name, WDDM `[N/A]` with `/api/ps` crediting (`free_plus_ps`) and without, multi-device `[N/A]`, a succeeded-empty vs failed vs malformed process query, a transition with retry and without recovery, Windows full-path mixed-case names, `CUDA_VISIBLE_DEVICES` as indices, UUIDs, unmatched and MIG; Apple with Metal (limit unset and raised), without Metal (limit raised: `wired_limit`; unset: `unavailable`), pressure 1/2/4 and unmapped, a Mac without unified memory; no GPU (`none`); failure, timeout and malformed output (`unavailable`); every report validated against `Capacity`, plus K-CAP's arithmetic (free and usable at most total; the top-level figure equals the largest device's) and the tie rule for `basis`; the service-tree lookup failing and denied descendants; mixed numeric and `[N/A]` rows; CUDA order (indices with and without `PCI_BUS_ID`, full UUIDs, a rejected prefix, an empty value); expectations written by hand; a lint that no probe default contains a memory size | TD-slice-4b |
| **S4c** Durable admin state | the admin offline/online switch (REQ-19a): the persistent state directory per OS, separate from the runtime lock directory; `init-state`; epoch and revision; persistence outside the gate's lock and publication synchronous with admission, shielded from cancellation; extends the refusal handling of S1 and S4a with `NODE_OFFLINE` (`X-Seriatim-Gate: refused`, the refusal precedence, answered before the body); health `offline`. Depends on S4a | AS1–AS20 (offline refuses new gated work while in-flight work finishes; ungated and admin still served; admission/offline and offline/online barrier tests; refusal precedence with startup and drain; persists across restart, a kill mid-write, and failures before and after the rename; unreadable, missing file and missing directory each start offline in a new epoch; Linux state survives an emptied tmpfs lock directory; sidecar-side recovery by `online` into a new epoch (the gateway side is GA, S8); cancellation and a crash between persisting and publishing; `init-state` won't overwrite; gated admin routes refused and ungated admin served; slow fsync never blocks admission; reason validation; online restores; non-admin 403) | TD-slice-4c |
| **S4d** Node metrics | the node metrics file (REQ-67): one writer, atomic writes, inherited ACLs on Windows. Depends on S4a–S4c, whose state it exports | MX1–MX9 against the node writer ( `promtool`, names and labels, atomic write, write failure isolated, null figures omitted, no secrets, unset directory, a second writer refused) | TD-slice-4d |
| **S5** Deploy: Windows | install, WinSW templates, `check-local-only`, `update-models` (through the sidecar), weekly maintenance | Pester with stubbed system commands; a manual node run, output pasted | TD-slice-5 |
| **S6** Deploy: macOS | install, launchd plists, the same scripts; the Metal-under-LaunchDaemon result recorded | bats with stubbed `lsof`, `launchctl` and `curl`; a manual node run | TD-slice-6 |
| **S7** Gateway core | node config (any number of nodes) with live reload (REQ-44, REQ-45), discovery with unknown states, eligibility routing (REQ-32: per-model eligible-nodes lists, `capacity = "unlimited"`, `capacity = "listed_only"`, `max_params_b`, and `size_overrides` only where a node's own size can't be parsed, by name or name and digest; no rule → `no_size_policy`; duplicate normalised model keys rejected), the verdict order with `lists_model` and `rule_verdict` (an offline or unhealthy node keeps its last discovery, and an unknown node that could be eligible gives `DISCOVERY_INCOMPLETE` before any operator error), a pinned node evaluated alone, `POST /api/show` routed without rules, every node's verdict in routing errors, `SIZE_POLICY_MISSING` and `MODEL_NOT_ELIGIBLE`, `BAD_REQUEST_BODY` on an unreadable `model`, passthrough, spill-over and failover by REQ-37's one rule (a gateway connect failure, or a replay-safe sidecar envelope whose code and gate value are in its table: `busy`, the `refused` codes, `NODE_SATURATED` with `ungated`, connect-stage `UPSTREAM_UNAVAILABLE`; the all-refused answer, including the largest `num_ctx` limit), client auth, `NODE_REQUIRED`, compatibility headers | R-GW1–R-GW20, run against fleets of 1, 2 and 5 nodes that mix the four rules and nodes with none; the rule order (a list wins over `unlimited` and `max_params_b`), `:latest` matching, `max_params_b` with any `capacity` value (`unlimited` or `listed_only`) on one node (a config error), a list naming an unknown node (a config error), an unparseable `parameter_size` (`size_unknown`), an override that never touches a node with a parseable size, a digest key winning over a name key, `listed_only` (the README's example), `[models."m"]` with `[models."m:latest"]` (a config error, on startup and reload), a pinned `X-Seriatim-Node` request checked against its node's rules, the REQ-32 error order with every node's verdict listed, `GPU_OFFLINE` included; failover on each code-and-gate pair in REQ-37's table, on a gateway connect failure, and on differing `num_ctx` limits; no failover on any other replay-safe sidecar code; property tests of REQ-32 and REQ-33 over generated fleets, rules and models; reload cases (a valid swap, an invalid file kept out, a node added or removed while a request is running, a changed rule), using `/api/tags` captured in the PPE from real micro-models; upstream-429 vs admission-429 cases; every-candidate-once with 3 nodes | TD-slice-7 |
| **S8** Gateway merged views and admin switch | `/api/tags` (one entry per name, from the source node chosen by verdict, then `rule_verdict`, then any answering node; representative when digests differ), `/api/ps`, `/api/version` and `/v1/models` merged in Ollama's and OpenAI's own shapes, with no added fields; partial results only through `X-Seriatim-Partial`; `FLEET_UNAVAILABLE`; `/seriatim/models` (per-node detail); scopes and redaction on `/seriatim/nodes` and `/seriatim/models`; `/seriatim/nodes` with each node's rules, measured capacity, loaded models' `size_vram`, `admin_state` and warnings (REQ-42); the admin switch routes (REQ-42a) and the `offline` verdict; status as the authoritative source, health and `NODE_OFFLINE` as hints; epoch-and-revision ordering, with the reconciling state for a new epoch; discovery refreshed before an online node is routed to; the gateway metrics file (REQ-67); health | V1–V8, each merged body validated against the plain shape with no extra keys; MX1–MX9 again against the gateway writer, and MX10–MX12 (per-instance files and labels, series only for configured nodes); GA1–GA14 (forward and confirm, reconciliation bounds, per-node serialised status reads and admin calls, fenced timeouts, epoch barrier tests, unreachable sidecar 502, unknown id 404, timeout with unknown outcome answered at once and reconciled, older revision ignored, a new epoch accepted, hints never overwrite, discovery refreshed after online, fail over around an offline node, online restores, non-admin 403) | TD-slice-8 |
| **S9** `/models/select` | task preferences, eligibility routing (REQ-32), `eligibility_basis`, every other node in `alternatives` with its verdict, routing errors as in REQ-32 | SEL1–SEL8 | TD-slice-9 |
| **S10** Gateway deploy + two-node PPE | Dockerfile, systemd example, a two-node PPE with real size metadata; the client-compatibility suite (REQ-62a) with pinned `ollama` and `openai` clients, and the README's verified-versions line; the gateway image added to `release.yml`, pushed to GHCR and deployed by digest | P-GW1–P-GW7 (routing, never-too-small, busy isolation between nodes (F1), spill-over, merged views, streaming end to end, failover); CC1–CC18 (both clients against plain Ollama, a sidecar and the gateway: chat, generate, completions and embeddings, streamed and not; errors raise the client's exception with retries off; listing against a sidecar and the gateway's merge; baseline repeatability; every standard response validated against the plain shape with no extra keys; `NODE_OFFLINE` and the PPE trigger for `MODEL_NOT_ELIGIBLE`) | TD-slice-10 |
| **S11** TLS (optional) | certificate and key settings for the sidecar and the gateway; PPE TLS smoke test | T1–T4 | TD-slice-11 |
| *Deferred* Measured fit | Admission from measured capacity (D14). Starts after S4b has run on real NVIDIA and Apple nodes, calibrated against their capacity recordings and the actual `size_vram`. It starts from D14's open problems | — | its own TD |
| **Interim deploy** (now, a small PR after this one) | `.github/workflows/deploy.yml`, `.github/workflows/promotion-source.yml` and `deploy/promote-ppe.py` (SPEC §7.8): the promotion flow; the source rules (`ppe` only from `promote/ppe-*` built from a branch of this repository, `prod` from `main` only, never a fork); the `.github/`-equals-`main` rule; the merge-commit, tree, base and pull-request lookup checks (with retry); the clean wipe of the PPE project; a no-op deploy step until there is a service; the settings in §7.9 | each fail-closed check shown on a deliberate bad pull request or push, recorded in the PR; a wipe that leaves another project's containers and volumes untouched | none |
| **S12** Release candidate 0.1.0-rc.1 | operator guide, supported Ollama versions, changelog; the first rc, `v0.1.0-rc.1`, through `release.yml` (`v0.1.0` follows in the release-follower slice, once the rc has passed PPE) | a full PPE run in CI, plus each node's local-only output; checksum and attestation verified by the installers | none |
| **Release follower** (right after S12) | `deploy/` follower for the sidecar (Windows, macOS) and the gateway container (SPEC §7.7, REQ-69): channel polling, verification, side-by-side install, health and local-only checks, automatic rollback with the failed version held; PPE on the pre-release channel; ends with the maintainer publishing `v0.1.0` once the PPE follower has run the rc; retires the interim deployment host by host | verification failures, a failed health check rolling back, a held failed version, downgrade refused, GitHub unavailable (T18–T20) | its own TD |

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
    MODEL_NOT_ELIGIBLE:        { status: 422, retryable: false, replay_safe: true }
    INTERNAL:                  { status: 500, retryable: true,  replay_safe: false }
    UPSTREAM_UNAVAILABLE:      { status: 502, retryable: true,  replay_safe: true }
    UPSTREAM_PROTOCOL_ERROR:   { status: 502, retryable: true,  replay_safe: false }
    NODE_STARTING:             { status: 503, retryable: true,  replay_safe: true }
    NODE_OFFLINE:              { status: 503, retryable: false, replay_safe: true }
    EXPOSURE_UNVERIFIED:       { status: 503, retryable: true,  replay_safe: true }
    OLLAMA_EXPOSED:            { status: 503, retryable: false, replay_safe: true }
    NODE_SATURATED:            { status: 503, retryable: true,  replay_safe: true }
    DISCOVERY_INCOMPLETE:      { status: 503, retryable: true,  replay_safe: true }
    SIZE_POLICY_MISSING:       { status: 503, retryable: false, replay_safe: true }
    GPU_OFFLINE:               { status: 503, retryable: false, replay_safe: true }
    FLEET_UNAVAILABLE:         { status: 503, retryable: true,  replay_safe: true }
    LOCK_HOLD_EXCEEDED:        { status: 504, retryable: true,  replay_safe: false }
    HTTP_VERSION_UNSUPPORTED:  { status: 505, retryable: false, replay_safe: true }
    UPSTREAM_STREAM_BROKEN:    { status: null, retryable: true, replay_safe: false }

  headers:
    Retry-After:
      description: Whole seconds before retrying.
      required: true
      schema: { type: string, pattern: '^[0-9]+$' }
    X-Seriatim-Gate:
      schema: { type: string, enum: [acquired, ungated, busy, refused] }
    X-Seriatim-Node:
      schema: { type: string, pattern: '^[a-z0-9][a-z0-9-]{0,31}$' }
    X-Seriatim-Partial:
      schema: { type: string }
    X-Request-Id:
      schema: { type: string, pattern: '^[A-Za-z0-9._-]{1,128}$' }
    X-Rerouted-From:
      schema: { type: string }
    X-Reroute-Reason:
      schema: { type: string, enum: [busy, offline, starting, saturated, exposure, num_ctx_limit, unreachable] }

  schemas:
    ErrorCode:
      type: string
      enum: [GPU_BUSY, BAD_PATH, AUTH_AMBIGUOUS, BAD_REQUEST_BODY, NUM_CTX_AMBIGUOUS,
             NUM_CTX_UNSUPPORTED_ROUTE, NODE_REQUIRED, UNKNOWN_TASK, AUTH_MISSING, AUTH_INVALID,
             FORBIDDEN_SCOPE, NOT_FOUND, MODEL_NOT_FOUND, METHOD_NOT_ALLOWED,
             REQUEST_BODY_TIMEOUT, UPLOAD_INTERRUPTED, BODY_TOO_LARGE, NUM_CTX_EXCEEDS_LIMIT, MODEL_SIZE_UNKNOWN,
             MODEL_NOT_ELIGIBLE, INTERNAL, UPSTREAM_UNAVAILABLE, UPSTREAM_PROTOCOL_ERROR, NODE_STARTING,
             EXPOSURE_UNVERIFIED, OLLAMA_EXPOSED, NODE_SATURATED, DISCOVERY_INCOMPLETE,
             GPU_OFFLINE, FLEET_UNAVAILABLE, LOCK_HOLD_EXCEEDED, HTTP_VERSION_UNSUPPORTED, UPSTREAM_STREAM_BROKEN,
             SIZE_POLICY_MISSING, NODE_OFFLINE]
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
        nodes:
          description: >
            Routing errors (SPEC REQ-32) only, including GPU_OFFLINE, which is now only ever a
            routing error (the fleet-wide case is FLEET_UNAVAILABLE). One verdict for every
            configured node. Reasons never carry the operator's offline reason or a key id.
          type: array
          items:
            type: object
            required: [id, verdict, reason]
            properties:
              id: { type: string }
              verdict: { type: string, enum: [eligible, not_eligible, no_size_policy, size_unknown, model_absent, discovery_unknown, offline, unhealthy] }
              lists_model: { type: [boolean, 'null'], description: null means unknown (SPEC REQ-32) }
              rule_verdict: { type: [string, 'null'], enum: [eligible, not_eligible, no_size_policy, size_unknown, null], description: the rules applied to last known metadata; set for offline and unhealthy nodes; null unless lists_model is true (SPEC REQ-32) }
              reason: { type: string, description: a fixed reason such as listed_only, offline_admin or admin_state_reconciling; never operator text or a key id }
            additionalProperties: false
            if:
              not: { required: [lists_model], properties: { lists_model: { const: true } } }
            then:
              properties: { rule_verdict: { const: null } }
      additionalProperties: false
      allOf:
        - if:
            properties:
              error_code: { enum: [DISCOVERY_INCOMPLETE, MODEL_NOT_FOUND, GPU_OFFLINE, SIZE_POLICY_MISSING, MODEL_SIZE_UNKNOWN, MODEL_NOT_ELIGIBLE] }
              source: { const: gateway }
            required: [error_code, source]
          then:
            required: [nodes]
            properties: { nodes: { minItems: 1 } }
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
        status: { type: string, enum: [ok, degraded, offline] }
        reasons:
          type: array
          items:
            type: string
            enum: [ollama_unreachable, ollama_bad_response, slot_overdue, slot_stuck,
                   starting, ollama_exposed, exposure_unverified, no_healthy_node,
                   offline_admin]
      additionalProperties: false
    Slot:
      type: object
      required: [state]
      properties:
        state: { type: string, enum: [free, held, draining, stuck] }
        path: { type: [string, 'null'], description: null for infer keys (redacted) }
        model: { type: [string, 'null'], description: null for infer keys (redacted) }
        request_id: { type: [string, 'null'], description: null for infer keys (redacted) }
        started_at: { type: [string, 'null'], format: date-time }
        elapsed_s: { type: [number, 'null'], minimum: 0 }
        drain_remaining_s: { type: [number, 'null'], minimum: 0 }
    NodeStatus:
      type: object
      required: [node, version, starting, admin_state, slots, capacity, loaded_models, ollama]
      properties:
        node: { type: string }
        admin_state: { $ref: '#/components/schemas/AdminState' }
        version: { type: string }
        starting: { type: boolean }
        slots: { type: array, items: { $ref: '#/components/schemas/Slot' } }
        capacity: { $ref: '#/components/schemas/Capacity' }
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
    SeriatimModels:
      description: Per-node model detail (SPEC REQ-38), kept off the standard /api/tags.
      type: object
      required: [models, partial]
      properties:
        models:
          type: array
          items:
            type: object
            required: [name, nodes, digests_differ]
            properties:
              name: { type: string }
              digests_differ: { type: boolean }
              nodes:
                type: array
                minItems: 1
                items:
                  type: object
                  required: [id, digest, size, parameter_size, modified_at]
                  properties:
                    id: { type: string }
                    digest: { type: string }
                    size: { type: integer, minimum: 0 }
                    parameter_size: { type: [string, 'null'] }
                    modified_at: { type: string }
                  additionalProperties: false
            additionalProperties: false
        partial: { type: array, items: { type: string }, description: nodes that did not answer }
      additionalProperties: false
    SelectRequest:
      type: object
      required: [task]
      properties:
        task: { type: string }
        context_tokens: { type: integer, minimum: 1 }
      additionalProperties: false
    AdminState:
      description: The node's admin offline/online switch (SPEC REQ-19a). The sidecar is the source of truth.
      type: object
      required: [offline, reason, since, changed_by, epoch, revision]
      properties:
        offline: { type: boolean }
        epoch: { type: string, pattern: '^[0-9a-f]{32}$', description: new on init-state and on every recovery from lost or unreadable state (SPEC REQ-19a) }
        revision: { type: integer, minimum: 0, description: persisted; starts at 0 in each epoch and increments on every committed change; ordered only within an epoch }
        reason: { type: [string, 'null'], maxLength: 200, description: null for infer keys (redacted) }
        since: { type: [string, 'null'], format: date-time }
        changed_by: { type: [string, 'null'], description: key id, never the key; null for infer keys (redacted) }
      additionalProperties: false
    AdminOfflineRequest:
      description: >
        No body, {} and {"reason": null} all mean no reason (SPEC REQ-19a). The pattern is a
        portable subset check with ECMA-262 semantics, where $ matches only at the end.
        Python's re lets $ match before a final newline, so "x\n" passes it there. The
        authoritative rule is x-seriatim-reason-chars, which the contract tests (K-OAS)
        enforce at the endpoint, with cases for a trailing newline, control, format and
        non-ASCII space characters.
      x-seriatim-reason-chars: every character in Unicode general category L, M, N, P or S, or U+0020
      type: object
      properties:
        reason:
          type: [string, 'null']
          minLength: 1
          maxLength: 200
          pattern: '^[^\u0000-\u001F\u007F-\u009F\u2028\u2029]+$'
      additionalProperties: false
    Capacity:
      description: >
        Measured capacity (SPEC REQ-16). Information only in Phase 1; routing uses the
        operator's rules. Figures are null unless measured; never estimated. Every failure,
        on any platform, is source unavailable. Arithmetic that JSON Schema can't express is
        checked by the contract tests (K-CAP): free_bytes <= total_bytes and usable_bytes <=
        total_bytes on each device, and on NVIDIA the top-level usable_bytes equals the
        largest device's.
      type: object
      required: [source, usable_bytes, devices, default_num_ctx, kv_cache_type, flash_attention, measured_at]
      properties:
        source: { type: string, enum: [nvidia-smi, apple-metal, none, unavailable] }
        basis:
          type: [string, 'null']
          enum: [free_plus_ollama, free_plus_ps, free_only, recommended_working_set, wired_limit, null]
        transition: { type: boolean, description: nvidia-smi only; inventories disagreed on every retry }
        usable_bytes: { type: [integer, 'null'], minimum: 0, description: largest device on NVIDIA }
        error: { type: string }
        warnings: { type: array, items: { type: string } }
        total_bytes: { type: [integer, 'null'], minimum: 0 }
        wired_limit_mb: { type: [integer, 'null'], minimum: 0, description: apple-metal only; 0 when unset }
        memory_pressure: { type: [string, 'null'], enum: [normal, warn, critical, null], description: apple-metal only; null when unreadable or unmapped }
        pressure_error: { type: string }
        default_num_ctx: { type: [integer, 'null'], minimum: 1 }
        kv_cache_type: { type: [string, 'null'], description: as configured; reported only }
        flash_attention: { type: [boolean, 'null'] }
        measured_at: { type: string, format: date-time }
        devices:
          type: array
          items:
            type: object
            required: [index, uuid, name, total_bytes, used_bytes, free_bytes, ollama_used_bytes, usable_bytes, basis]
            properties:
              index: { type: integer, minimum: 0 }
              uuid: { type: string, description: as reported by nvidia-smi (sanitised in fixtures) }
              name: { type: string }
              total_bytes: { type: integer, minimum: 0 }
              used_bytes: { type: integer, minimum: 0 }
              free_bytes: { type: integer, minimum: 0 }
              ollama_used_bytes: { type: [integer, 'null'], minimum: 0, description: attributed (free_plus_ollama), credited from /api/ps (free_plus_ps), or null (free_only) }
              usable_bytes: { type: integer, minimum: 0 }
              basis: { type: string, enum: [free_plus_ollama, free_plus_ps, free_only] }
            additionalProperties: false
      allOf:
        - if: { properties: { source: { const: nvidia-smi } } }
          then:
            required: [basis]
            properties:
              usable_bytes: { type: integer }
              basis: { enum: [free_plus_ollama, free_plus_ps, free_only] }
              devices: { minItems: 1 }
        - if: { properties: { source: { const: apple-metal } } }
          then:
            required: [basis, total_bytes, wired_limit_mb, memory_pressure]
            properties:
              usable_bytes: { type: integer }
              total_bytes: { type: integer }
              basis: { enum: [recommended_working_set, wired_limit] }
              devices: { maxItems: 0 }
        - if: { properties: { source: { enum: [none, unavailable] } } }
          then:
            properties:
              usable_bytes: { type: 'null' }
              basis: { type: 'null' }
              devices: { maxItems: 0 }
        - if: { properties: { source: { const: unavailable } } }
          then:
            required: [error]
        - if:
            required: [source, memory_pressure]
            properties: { source: { const: apple-metal }, memory_pressure: { type: 'null' } }
          then:
            required: [pressure_error]
    SelectResponse:
      type: object
      required: [model, node, eligibility_basis, reasons, alternatives]
      properties:
        model: { type: string }
        node: { type: string, description: always eligible (SPEC REQ-39) }
        eligibility_basis: { type: string, enum: [eligible_nodes, unlimited, max_params_b] }
        context_tokens: { type: integer, minimum: 1, description: echoed; does not affect the choice in Phase 1 }
        reasons: { type: array, items: { type: string } }
        alternatives:
          description: Every other configured node, for the chosen model.
          type: array
          items:
            type: object
            required: [node, verdict, reason]
            properties:
              node: { type: string }
              verdict: { type: string, enum: [eligible, not_eligible, no_size_policy, size_unknown, model_absent, discovery_unknown, offline, unhealthy] }
              reason: { type: string }
            additionalProperties: false
      additionalProperties: false

  responses:
    NodeAdminResult:
      description: The sidecar confirmed the change (SPEC REQ-42a).
      content:
        application/json:
          schema:
            type: object
            required: [node, admin_state]
            properties:
              node: { type: string }
              admin_state: { $ref: '#/components/schemas/AdminState' }
            additionalProperties: false
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
        and FLEET_UNAVAILABLE carry Retry-After; OLLAMA_EXPOSED, SIZE_POLICY_MISSING,
        NODE_OFFLINE and GPU_OFFLINE do not), or Ollama's own 503 passed through.
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
        Ollama's response: status and body bytes unchanged. Its JSON is arbitrary and is not
        constrained here; Ollama's own errors usually have the shape of UpstreamError, shown
        for client authors only. Streams are forwarded incrementally; a mid-stream failure
        ends with a StreamErrorRecord when framing is safe, then an aborted connection.
      headers:
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json: {}
        application/x-ndjson: {}
        text/event-stream: {}
        text/plain: {}
        application/octet-stream: {}
    MergedView:
      description: >
        A gateway merged view (SPEC REQ-38): exactly Ollama's or OpenAI's response shape, with
        no added fields, validated against the shape S0b recorded from the pinned Ollama
        version. A partial result is flagged only by X-Seriatim-Partial. At the sidecar the
        same route is a plain Passthrough, with X-Seriatim-Node naming the node; the gateway
        sends no X-Seriatim-Node on a merged view, which has no single node.
      headers:
        X-Seriatim-Partial: { $ref: '#/components/headers/X-Seriatim-Partial' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Request-Id: { $ref: '#/components/headers/X-Request-Id' }
      content:
        application/json: {}

  x-seriatim-gated-operation: &gated
    x-seriatim-gate: gated
    x-seriatim-scope: infer
    requestBody:
      description: Opaque Ollama request; forwarded byte-for-byte. The sidecar may read
        `num_ctx` and `options.num_ctx` (SPEC REQ-8, REQ-9); the gateway reads `model`
        (SPEC REQ-32). Neither ever rewrites the body.
      content: { application/json: {} }
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '404': { $ref: '#/components/responses/Error' }
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
      '404': { $ref: '#/components/responses/Error' }
      '408': { $ref: '#/components/responses/Error' }
      '413': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      '505': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }
  x-seriatim-merged-operation: &merged
    x-seriatim-gate: ungated
    x-seriatim-scope: infer
    x-seriatim-gateway-merge: REQ-38
    responses:
      '200': { $ref: '#/components/responses/MergedView' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '404': { $ref: '#/components/responses/Error' }
      '408': { $ref: '#/components/responses/Error' }
      '413': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      '505': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }
  x-seriatim-admin-operation: &admin
    x-seriatim-gate: ungated
    x-seriatim-scope: admin
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '400': { $ref: '#/components/responses/Error' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '404': { $ref: '#/components/responses/Error' }
      '408': { $ref: '#/components/responses/Error' }
      '413': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      '503': { $ref: '#/components/responses/Unavailable' }
      '505': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }

# Unlisted method+path pairs are gated and need the admin scope (SPEC REQ-5).
x-seriatim-unknown-endpoint: { x-seriatim-gate: gated, x-seriatim-scope: admin }

paths:
  /seriatim/health:
    get:
      x-seriatim-roles: [sidecar, gateway]
      x-seriatim-scope: none
      security: []
      responses:
        '200': { description: Healthy, content: { application/json: { schema: { $ref: '#/components/schemas/Health' } } } }
        '503': { description: Degraded, content: { application/json: { schema: { $ref: '#/components/schemas/Health' } } } }
  /seriatim/status:
    get:
      x-seriatim-roles: [sidecar]
      x-seriatim-scope: infer
      x-seriatim-redaction: For infer keys, admin_state.reason, admin_state.changed_by and every slot's path, model and request_id are null (SPEC REQ-5).
      responses:
        '200': { description: Node status, content: { application/json: { schema: { $ref: '#/components/schemas/NodeStatus' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/admin/offline:
    post:
      x-seriatim-roles: [sidecar]
      x-seriatim-scope: admin
      requestBody:
        required: false
        content: { application/json: { schema: { $ref: '#/components/schemas/AdminOfflineRequest' } } }
      responses:
        '200': { description: The resulting state, content: { application/json: { schema: { $ref: '#/components/schemas/AdminState' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Error' }
        '408': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '500': { $ref: '#/components/responses/Error' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/admin/online:
    post:
      x-seriatim-roles: [sidecar]
      x-seriatim-scope: admin
      responses:
        '200': { description: The resulting state, content: { application/json: { schema: { $ref: '#/components/schemas/AdminState' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Error' }
        '408': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '500': { $ref: '#/components/responses/Error' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/admin/nodes/{id}/offline:
    post:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: admin
      parameters: [ { name: id, in: path, required: true, schema: { type: string } } ]
      requestBody:
        required: false
        content: { application/json: { schema: { $ref: '#/components/schemas/AdminOfflineRequest' } } }
      responses:
        '200': { $ref: '#/components/responses/NodeAdminResult' }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Error' }
        '404': { $ref: '#/components/responses/Error' }
        '408': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '500': { $ref: '#/components/responses/Error' }
        '502': { $ref: '#/components/responses/Error' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/admin/nodes/{id}/online:
    post:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: admin
      parameters: [ { name: id, in: path, required: true, schema: { type: string } } ]
      responses:
        '200': { $ref: '#/components/responses/NodeAdminResult' }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '403': { $ref: '#/components/responses/Error' }
        '404': { $ref: '#/components/responses/Error' }
        '408': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '500': { $ref: '#/components/responses/Error' }
        '502': { $ref: '#/components/responses/Error' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/nodes:
    get:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: infer
      x-seriatim-redaction: For infer keys, each admin_state.reason and changed_by, and the embedded status's slot details, are null (SPEC REQ-5).
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
                      required: [id, healthy, admin_state, reconciling, priority, size_policy, eligible_for_models, capacity, loaded, ollama_version, warnings, last_check, status]
                      properties:
                        id: { type: string }
                        healthy: { type: boolean }
                        admin_state:
                          description: As last reported by the node's sidecar; null if never read.
                          oneOf:
                            - $ref: '#/components/schemas/AdminState'
                            - type: 'null'
                        priority: { type: integer }
                        size_policy: { type: string, enum: [unlimited, listed_only, max_params_b, none], description: the node's own rule; eligible-nodes lists are in eligible_for_models }
                        ollama_version: { type: [string, 'null'] }
                        max_params_b: { type: [number, 'null'] }
                        eligible_for_models: { type: array, items: { type: string }, description: models whose eligible-nodes list names this node }
                        capacity:
                          description: The node's last Capacity report, shown next to the configured rules; null if it could not be read.
                          oneOf:
                            - $ref: '#/components/schemas/Capacity'
                            - type: 'null'
                        capacity_error: { type: string, description: required when capacity is null }
                        loaded:
                          description: From the node's /api/ps. size_vram < size means part of the model is on the CPU.
                          type: [array, 'null']
                          items:
                            type: object
                            required: [model, size, size_vram]
                            properties:
                              model: { type: string }
                              size: { type: integer, minimum: 0 }
                              size_vram: { type: integer, minimum: 0 }
                        warnings: { type: array, items: { type: string } }
                        reconciling: { type: boolean, description: true while the gateway reconciles a new epoch; the node is out of routing (SPEC REQ-42a) }
                        last_check: { type: [string, 'null'], format: date-time, description: null until the first check of a new node }
                        status:
                          description: The node's last status, fetched with its admin key and redacted for infer keys; null if never read.
                          oneOf:
                            - $ref: '#/components/schemas/NodeStatus'
                            - type: 'null'
                      allOf:
                        - if: { properties: { capacity: { type: 'null' } }, required: [capacity] }
                          then: { required: [capacity_error] }
                        - if: { properties: { size_policy: { const: max_params_b } } }
                          then:
                            required: [max_params_b]
                            properties: { max_params_b: { type: number, exclusiveMinimum: 0 } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '505': { $ref: '#/components/responses/Error' }
  /seriatim/models:
    get:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: infer
      responses:
        '200': { description: Per-node model detail, content: { application/json: { schema: { $ref: '#/components/schemas/SeriatimModels' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '505': { $ref: '#/components/responses/Error' }
  /models/select:
    get:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: infer
      parameters:
        - { name: task, in: query, required: true, schema: { type: string } }
        - { name: context_tokens, in: query, schema: { type: integer, minimum: 1 } }
      responses:
        '200': { description: Selection, content: { application/json: { schema: { $ref: '#/components/schemas/SelectResponse' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/Error' }
        '422': { $ref: '#/components/responses/Error' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '505': { $ref: '#/components/responses/Error' }
    post:
      x-seriatim-roles: [gateway]
      x-seriatim-scope: infer
      requestBody: { required: true, content: { application/json: { schema: { $ref: '#/components/schemas/SelectRequest' } } } }
      responses:
        '200': { description: Selection, content: { application/json: { schema: { $ref: '#/components/schemas/SelectResponse' } } } }
        '400': { $ref: '#/components/responses/Error' }
        '401': { $ref: '#/components/responses/Unauthorized' }
        '404': { $ref: '#/components/responses/Error' }
        '408': { $ref: '#/components/responses/Error' }
        '413': { $ref: '#/components/responses/Error' }
        '422': { $ref: '#/components/responses/Error' }
        '503': { $ref: '#/components/responses/Unavailable' }
        '505': { $ref: '#/components/responses/Error' }

  # Ungated, infer scope. At the sidecar these are raw passthrough. At the gateway,
  # /api/tags, /api/ps, /api/version and /v1/models are merged (SPEC REQ-38), but each body
  # has exactly Ollama's or OpenAI's schema, with no added fields; a partial result is
  # flagged only by X-Seriatim-Partial. Per-node detail is on /seriatim/models and
  # /seriatim/nodes.
  # At the gateway, / answers 200 "Ollama is running" if at least one node is healthy, else 503.
  /:            { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] }, head: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /api/tags:    { get: { <<: *merged, x-seriatim-roles: [sidecar, gateway] } }
  /api/ps:      { get: { <<: *merged, x-seriatim-roles: [sidecar, gateway] } }
  /api/version: { get: { <<: *merged, x-seriatim-roles: [sidecar, gateway] } }
  /api/show:    { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/models:   { get: { <<: *merged, x-seriatim-roles: [sidecar, gateway] } }

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
