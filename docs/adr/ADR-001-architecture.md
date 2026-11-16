# ADR-001: Seriatim architecture

**Status:** proposed (Phase 1, for review). **Date:** 2026-11-16.
**Requirements:** [SPEC](../SPEC.md). **First slice design:** [TD-slice-1](../design/TD-slice-1.md).

## 1. Context

Several clients share a few GPU machines that run Ollama. When two inference requests land
on one GPU at the same time, they compete for VRAM. Ollama then either queues the second one
out of sight (and client timeouts fire) or offloads layers to the CPU, which makes both slow.
The operators want each machine to run one request at a time. A busy machine should say so at
once, and clients should see a single address that sends each request to the right machine.

The **evaluation** (2026-11-15; summarised in §9 and cited by test number `E1a` to `E6`)
compared a predecessor gateway with LiteLLM, Olla, lollms_hub and ollama_load_balancer. No
candidate met *busy → immediate 429* (E2) and *Bearer auth* (E4) together without custom code.
The predecessor met the most requirements, but it had four defects (SPEC §1, F1 to F4). The
recommendation was to keep a small in-house gate and fix those defects. This ADR records how.

## 2. Decision summary

Seriatim has two layers.

```
 client ──Bearer──▶ gateway ──node key──▶ sidecar :11434 ──loopback──▶ Ollama 127.0.0.1:11435
                    (router,                (lock authority,              (never reachable
                     no lock)                one per GPU machine)          from the LAN)
```

| # | decision |
|---|---|
| D1 | The **lock lives in a sidecar on each GPU machine**. There is no lock in the gateway. |
| D2 | The sidecar **takes Ollama's port 11434**. Ollama moves to **`127.0.0.1:11435`**. |
| D3 | When busy, the answer is an **immediate 429 with `Retry-After`**. There is no queue. A 429 means Ollama never saw the request. |
| D4 | **Byte-for-byte passthrough** with no body rewriting. `num_ctx` is never lowered; it is rejected or flagged. |
| D5 | The gateway is a **thin router**: it routes by size using Ollama's own metadata, combines the read-only views, and serves `/models/select` from config. |
| D6 | **Bearer keys at both layers.** Hashes are stored on nodes. Each node has its own key, and client keys are never forwarded. |
| D7 | **Python 3.12, Starlette, uvicorn and httpx.** Config through pydantic-settings. Services through WinSW and launchd. |
| D8 | **The contract comes first.** `contract/openapi.yaml` (Appendix A) and a black-box behaviour suite define the interface, so another implementation can drop in. |
| D9 | **Config-driven deploy scripts**, with a local-only check after every Ollama upgrade. |

## 3. Decisions and the alternatives rejected

### D1. The lock lives at the GPU machine

**Decision.** Each GPU machine runs a sidecar that holds that machine's slots. It is the only
process that can admit a request to its Ollama.

**Why at the machine.**
- **Correct by construction.** Only something sitting in front of Ollama on that machine can
  see every request to it. A central lock misses any client that talks to the machine some
  other way: a maintenance script, a developer, or a second gateway. Each of those reaches
  Ollama without being counted.
- **One machine per lock (F1).** The predecessor's single global lock in the gateway returned
  429 for idle machine B while A was busy (E2). A lock on each machine cannot couple machines.
- **The gateway becomes stateless.** It can restart, or be duplicated, without losing or
  splitting the lock state. With the lock in the gateway, two gateway replicas mean two locks.
- **Fails locally.** If the gateway is down, clients can still use a sidecar directly, with the
  same guarantees.

**Alternatives rejected.**

| alternative | why rejected |
|---|---|
| A lock in the central gateway (the predecessor's design, fixed to one lock per backend) | Leaves every non-gateway path to Ollama ungated. The state lives in one process that is also the router, so restarts and replicas break the guarantee. The evaluation's "~30 lines" estimate was for one lock per backend inside that process, and it does not fix the bypass. |
| LiteLLM with a `CustomLogger` gate hook (the evaluation's extension path) | Worked in the evaluation's PPE (429 in 30 ms). But a dead backend leaks the slot until a reaper releases it, because failure events in v1.108.9 carry no deployment id. It has no native `/api/chat` (E1a ❌), so it needs a ~100-line translation layer. It is a 1.8 GB, fast-moving dependency. Total ~250 lines against ~150 here. |
| Olla with a layer in front of it | Olla routes best (E3 ✅) and passes options through (E1a ✅). But it has no concurrency cap and no auth (E2 ❌, E4 ❌). The layer we would put in front *is* this design. It stays the runner-up for discovery and health later. |
| A per-endpoint `max_concurrency` patch to Olla | A Go PR of 150 to 250 lines, with an uncertain merge. It would still need an auth layer. |
| lollms_hub or ollama_load_balancer | Stale projects. They failed routing (E3) and busy semantics (E2). lollms_hub ships a default secret that also works as an admin token. ollama_load_balancer returns 503 without `Retry-After` and spills over instead of rejecting. |
| Ollama's own `OLLAMA_NUM_PARALLEL=1` alone | Ollama queues the excess request instead of rejecting it, which is the behaviour this project exists to remove. It is kept as a second line of defence (SPEC REQ-4). |
| A distributed lock (Redis or etcd) shared by gateways | Adds infrastructure, and still leaves direct access to Ollama ungated. |

### D2. The sidecar takes port 11434; Ollama moves to 127.0.0.1:11435

**Decision.** Clients and tools that default to `:11434` reach the sidecar without changes.
Ollama is configured with `OLLAMA_HOST=127.0.0.1:11435`.

**Alternatives rejected.**
- **Sidecar on another port, Ollama left on 11434.** Ollama stays on its well-known port, and
  any client left on the default skips the gate.
- **Ollama on a Unix socket.** Ollama does not support it on Windows, and that is not portable.
- **The predecessor's own layout** (gateway on 11435, Ollama on 11434). Same flaw as the first.

**Consequences.** Ollama upgrades can reset its bind or turn its own auto-start back on, so a
local-only check after upgrades is mandatory (D9, SPEC §6.1). The sidecar also probes for
exposure at runtime (SPEC REQ-19).

### D3. Busy means an immediate 429; no queue anywhere

**Decision.** A gated request with no free slot gets 429 right away, with `Retry-After`. The
sidecar decides from the method and path alone, before it reads the body (SPEC REQ-3). A 429
means Ollama never saw the request, so a client or the gateway can retry it safely anywhere.

**Alternatives rejected.**
- **A short wait and retry queue,** as an earlier routing design proposed. A hidden wait is the
  failure mode the evaluation measured against LiteLLM (30 s queued) and Olla (28 s queued).
  It also makes latency unpredictable for the client.
- **503 without `Retry-After`** (ollama_load_balancer). This does not tell the client when to
  retry. Also, 503 means "service down", which is not what busy is.
- **An estimated `Retry-After`** based on the work in flight. Generation lengths can't be
  predicted. A static, configurable value (default 15 s) is honest. The 429 body also includes
  `busy_for_s`.

### D4. Byte-for-byte passthrough; `num_ctx` is never lowered

**Decision.** Request and response bodies are forwarded as raw bytes (`aiter_raw`, no
re-serialisation). The sidecar may *read* a body to enforce limits, but never rewrites it. An
over-limit `num_ctx` is rejected (422) or flagged (`X-Seriatim-Ctx`), depending on the open
question Q2. `num_ctx` on `/v1` is rejected by default (Q3).

**Alternatives rejected.**
- **Clamping `num_ctx` to a "safe" table** (the predecessor; also lollms_hub's hard-coded
  crop). This silently changes what the caller asked for (E1a: 30000 → 24576), and the table
  goes stale with every new model (F3).
- **Translating `/v1` to `/api/chat`** (LiteLLM). LiteLLM's own translation lost nested
  `options.num_ctx` (E1b). It is a translation layer, which is a non-goal, and it changes bytes.
- **Parsing and re-serialising JSON for every request.** This changes key order, whitespace and
  number formatting, so it is not byte-for-byte and can't be audited on the wire.

### D5. The gateway is a thin router

**Decision.** The gateway does four things:
- authenticates clients;
- routes each model-bearing request to a node by priority, filtered by whether the node has
  the model (from that node's `/api/tags`) and by its `max_params_b`, compared with the model's
  `details.parameter_size`;
- passes a node's 429 through (or spills over once, per Q1);
- combines `/api/tags`, `/api/ps` and `/api/version`, and serves `/models/select` from
  operator-configured task preferences and each node's live memory.

**Alternatives rejected.**
- **Routing by parsing the model name** (`:32b`). Names are free text. `parameter_size` from
  Ollama is authoritative. The name is not used at all; a model with no known size needs an
  explicit override (SPEC REQ-32).
- **Routing by an estimated token count** (an earlier design's `len(prompt)/4`). Too inaccurate
  to decide a machine on. Context size is handled by REQ-8 limits at the node instead.
- **Discovery only,** without size limits. When a 32B model is present on both machines,
  discovery alone can't keep it off the 24 GB card. Olla's priority-plus-discovery approach
  (E3 ✅) is what the design follows, plus the size cap.
- **The predecessor's built-in model knowledge base** (hard-coded profiles, speeds and "safe
  context" tables). It goes stale, is specific to particular hardware, and is a non-goal.
  `/models/select` uses configuration plus live data instead.
- **LiteLLM as the router.** It has no native API (E1a, E6 ❌).

### D6. Bearer keys at both layers

**Decision.** Keys are 256-bit random values. Nodes store SHA-256 hashes and compare them in
constant time. Keys carry scopes (`infer`, `admin`; Q5). The gateway holds a separate key for
each node. Client credentials are stripped before forwarding.

**Alternatives rejected.**
- **No auth on the sidecar,** relying on the gateway. That breaks the property that a direct
  client gets the same guarantees as the gateway, and a LAN host could use the GPU.
- **Forwarding client `Authorization` to Ollama.** ollama_load_balancer does this. It leaks
  keys to a process that does not need them.
- **Accepting keys in query strings.** They leak into logs and `Referer` headers.
- **A slow password hash (bcrypt or argon2) for keys.** Unnecessary for 256-bit random keys,
  and it would add latency to every request.
- **mTLS between gateway and nodes.** Stronger, but it brings the certificate-management cost
  of Q6 without removing the need for client keys. It is deferred.
- **Shipping a default secret.** lollms_hub's default `SECRET_KEY` was accepted as an admin
  token (E4 ⚠). Seriatim refuses to start without configured keys (REQ-15).

### D7. Python 3.12 with Starlette, uvicorn and httpx; WinSW and launchd

**Decision.**
- Starlette routes the `/seriatim/*` endpoints and runs the lifespan.
- The proxy itself is a plain ASGI callable. That gives it full control over when disconnects
  are noticed and when the slot is released (TD-slice-1 §3).
- httpx (async) is the upstream client, with streaming raw bytes. uvicorn serves, with one
  worker.
- pydantic-settings reads the env and TOML config.
- WinSW wraps the Windows services and launchd runs the macOS ones.
- uv manages the workspace and lock file.

**Alternatives rejected.**
- **FastAPI.** It adds a framework layer over Starlette, mainly for its generated OpenAPI. This
  project is contract-first: the OpenAPI file is written by hand and is what gets tested.
- **aiohttp.** A good library, but httpx with Starlette is what the predecessor proved in
  production, and httpx has a cleaner streaming and timeout model.
- **Writing a raw asyncio HTTP server.** Rebuilds what exists.
- **Go or Rust now.** Python is what the operators run and review. The contract (D8) keeps a
  later port possible.
- **NSSM.** Widely used, but not maintained since 2017. **AlwaysUp** is commercial. **pywin32
  services** mean code that needs its own tests.
- **Ollama's macOS app** for running Ollama. It can't be configured reliably as a service
  (`launchctl setenv` doesn't survive a reboot), and it starts on login with its own bind.

### D8. The contract comes first, so another implementation can drop in

**Decision.** `contract/openapi.yaml` (OpenAPI 3.1; the draft is Appendix A) is the source of
truth for every Seriatim-owned endpoint, header, status and error code.
- CI lints it.
- The unit tests check every error body Seriatim produces against `ErrorEnvelope`.
- The enum of error codes in the code is checked for equality with the contract's.
- `tests/behaviour/` is a black-box suite parametrised by base URL.

A Rust (or other) sidecar is a drop-in when it passes that suite in the PPE.

**Alternatives rejected.**
- **Generating the spec from the code.** The spec then follows the implementation instead of
  constraining it.
- **JSON Schema only.** It can't express paths, headers or status codes.

### D9. Config-driven deploy scripts with a local-only check

**Decision.**
- `deploy/windows/*.ps1` and `deploy/macos/*.sh` cover install, the services, model update, the
  weekly maintenance and `check-local-only`.
- They read everything from a config file. They check every native exit code and exit non-zero
  on any failure.
- The local-only check runs after install, after every model update, after every Ollama upgrade
  and weekly.
- Scripts are tested with Pester and bats, with system commands stubbed.

**Alternatives rejected.**
- **Copying the predecessor's scripts.** They are machine-specific: fixed paths, a fixed
  repository, and a Docker rebuild. They also wrap native commands in `try/catch`, which never
  fires in PowerShell, so a failed `ollama pull` is counted as "updated". This is a
  fail-silent defect.
- **A configuration-management tool** (Ansible, Salt, or similar) for two or three machines.
  Too heavy for this fleet. The scripts stay simple enough to translate into one later.

## 4. Component overview

| component | package | runs on | state |
|---|---|---|---|
| sidecar | `node/seriatim_node` | every GPU node (Windows, macOS) | slots (in memory), config |
| gateway | `gateway/seriatim_gateway` | one central host (container or systemd) | node health and model cache (in memory, rebuilt from the nodes) |
| contract | `contract/openapi.yaml` | — | — |
| deploy | `deploy/windows`, `deploy/macos` | GPU nodes | config files, logs |
| PPE | `ppe/` | the PPE host only | containers, volumes |

## 5. Interface contract

### 5.1 Who talks to whom

| from → to | transport | auth | what |
|---|---|---|---|
| client → gateway | HTTP(S), port configurable | `Authorization: Bearer <client key>` (or `X-API-Key`) | Ollama native API, `/v1/*`, `/models/select`, `/seriatim/*` |
| client → sidecar (directly) | HTTP(S) :11434 | Bearer (client key known to that node) | Ollama native API, `/v1/*`, `/seriatim/*` |
| gateway → sidecar | HTTP(S) :11434 | Bearer (the node's key) | the same as a direct client, plus `X-Request-Id` |
| sidecar → Ollama | HTTP over loopback :11435 | none (loopback only) | raw passthrough |

### 5.2 Headers that Seriatim adds or reads

| header | direction | meaning |
|---|---|---|
| `Retry-After` | response (429, 503) | whole seconds |
| `X-Seriatim-Node` | response (gateway); request (admin calls through the gateway, REQ-41) | the node id that served the request, or the node to target |
| `X-Seriatim-Gate` | response (sidecar) | `acquired` (the request held a slot) or `ungated` |
| `X-Seriatim-Ctx` | response | `exceeds-limit; requested=<n>; limit=<m>` (REQ-8, `flag` mode only) |
| `X-Seriatim-Partial` | response (gateway combined views) | comma-separated ids of nodes that did not answer |
| `X-Request-Id` | both | echoed if the client sends it (≤ 128 characters, `[A-Za-z0-9._-]`), otherwise generated; it appears in logs |

### 5.3 Header handling in the passthrough

- **Request to Ollama.** The sidecar removes the hop-by-hop headers (`Connection`, and every
  header it names, `Keep-Alive`, `Proxy-*`, `TE`, `Trailer`, `Transfer-Encoding`, `Upgrade`),
  plus `Authorization`, `X-API-Key` and `X-Seriatim-*`.
  - `Host` is set to the upstream. `Content-Length` is set from the exact body bytes.
  - httpx's own default headers are suppressed, so Ollama sees `Accept-Encoding` only if the
    client sent it.
- **Response to the client.** The sidecar removes the hop-by-hop headers and keeps everything
  else, including `Content-Encoding` and `Content-Length`. The bytes are raw, so the length is
  still valid. Then it adds the headers in §5.2.

### 5.4 Error envelope and codes

All errors that Seriatim produces use this body (Ollama's own errors pass through unchanged):

```json
{"error": "GPU busy: another request is running on this node",
 "error_code": "GPU_BUSY", "retryable": true, "retry_after": 15,
 "source": "sidecar", "busy_for_s": 12.4}
```

| code | HTTP | retryable | produced by |
|---|---|---|---|
| `GPU_BUSY` | 429 | yes | sidecar |
| `AUTH_MISSING`, `AUTH_INVALID` | 401 | no | both |
| `FORBIDDEN_SCOPE` | 403 | no | both |
| `BAD_REQUEST_BODY` | 400 | no | both (only when a check needs the body and it can't be parsed) |
| `NUM_CTX_UNSUPPORTED_ROUTE` | 400 | no | sidecar |
| `NODE_REQUIRED`, `UNKNOWN_TASK` | 400 | no | gateway |
| `MODEL_NOT_FOUND` | 404 | no | gateway |
| `REQUEST_BODY_TIMEOUT` | 408 | yes | both |
| `BODY_TOO_LARGE` | 413 | no | both |
| `NUM_CTX_EXCEEDS_LIMIT`, `MODEL_SIZE_UNKNOWN` | 422 | no | sidecar / gateway |
| `INTERNAL` | 500 | yes | both |
| `UPSTREAM_UNAVAILABLE`, `UPSTREAM_PROTOCOL_ERROR` | 502 | yes | both |
| `OLLAMA_EXPOSED`, `NO_HEALTHY_NODE` | 503 | yes | sidecar / gateway |
| `LOCK_HOLD_EXCEEDED` | 504 | yes | sidecar |
| `UPSTREAM_STREAM_BROKEN` | — (in-stream record) | yes | both |

## 6. Concurrency model (summary)

The details are in TD-slice-1 §3.
- **One process, one event loop.** The gate is a counter with no `await` between the check and
  the increment, so admission is atomic under asyncio.
- **Admission happens before the body is read,** so a 429 costs microseconds.
- **The lease is owned by the proxy's ASGI call.** The call runs the upstream exchange and a
  disconnect watcher in one task group, under a deadline. Every way out (finishing, the client
  leaving, upstream failing, the deadline, a bug) goes through one `finally`. That block closes
  the upstream response, then releases the lease.
- **No background task ever releases a lease** (SPEC REQ-11).

## 7. Threat model

**Assets.**
- GPU time;
- the models on disk;
- prompts and outputs, which can be confidential;
- keys;
- Ollama's admin API (pull, push, create, copy, delete);
- the integrity of the "one at a time" guarantee.

**Actors.**
- **Trusted:** the operator (holds admin keys) and local users of the node.
- **Semi-trusted:** clients with `infer` keys.
- **Untrusted:** any host on the LAN, and web pages in a LAN browser.

| # | threat (STRIDE) | vector | mitigation | residual |
|---|---|---|---|---|
| T1 | Elevation / DoS: bypassing the gate | A LAN host connects to Ollama directly | Ollama binds to loopback (D2); firewall; deploy check; runtime self-check refuses gated traffic (REQ-19) | Local users on the node can still reach 11435 (trusted by assumption) |
| T2 | Information disclosure: key or prompt sniffing | Plain HTTP on the LAN | Optional TLS (REQ-27, Q6) | **Open** while TLS is off |
| T3 | Information disclosure: key leakage at rest or in logs | Repository, logs, config files | Hashes on nodes; credentials never logged; keys never accepted in queries; gitleaks plus the private-terms check; config ACL / 0600 | The gateway holds node keys in plain text (it must) |
| T4 | Spoofing: forged client | Guessing a key | 256-bit keys; constant-time compare; 401 with no hint about which part was wrong | — |
| T5 | DoS: holding the GPU | A long generation, a slow reader, a slow uploader | Maximum hold (REQ-12); body timeout and size limit (REQ-20); disconnect closes the upstream | An `infer` key holder can keep a node busy up to the maximum hold, repeatedly (there are no quotas, a non-goal) |
| T6 | Tampering / elevation: abuse of the admin API | Delete models, pull huge models, push to a registry, SSRF through `/api/pull` to any registry URL | The `admin` scope (REQ-14, Q5) | An admin key holder is trusted |
| T7 | Tampering: request smuggling or header injection through the passthrough | Crafted `Transfer-Encoding`, `Connection` or `Host` | h11 parsing (uvicorn) and httpx; hop-by-hop headers removed; `Host` rewritten; body length from bytes (§5.3) | Depends on h11 and httpx being correct (they are pinned and updated) |
| T8 | Spoofing: a rogue or compromised node lies to the gateway | Fake 429, `/api/tags` or status | Nodes are configured explicitly (no discovery); the gateway authenticates to them | Responses from nodes are not authenticated without TLS |
| T9 | Cross-site attack from a LAN browser | A web page calling `http://node:11434` | Bearer only (no cookies), so CSRF has no ambient credential; no CORS headers are sent, so pages can't read responses | A page can still send blind requests, which are rejected with 401 |
| T10 | Information disclosure through status and errors | `/seriatim/health`, the 429 body | Health returns only a status and reasons; the 429 shows only `busy_for_s` (not the other tenant's model or client); status needs a key | Key holders see which models are loaded |
| T11 | Supply chain | A malicious dependency or Action | Locked dependencies (`uv.lock`); Actions pinned by SHA; Dependabot; gitleaks binary verified by checksum; nodes install tagged releases only (Q10) | Upstream compromise between pinned updates |
| T12 | Repudiation | "Who used the GPU?" | Request log with client key id, request id, path, model and timings (REQ-22) | Logs are local to each node and not tamper-proof |

## 8. Residual risks

1. **Plain HTTP on the LAN** (T2), until Q6 changes the default.
2. **Cancellation lag.** After an upstream close, Ollama can take a moment to stop generating.
   A request admitted right then can briefly overlap the cancelled one on the GPU. Ollama's own
   `NUM_PARALLEL=1` queue serialises them, so the result is a short delay, not a VRAM clash.
   PPE test P6 measures this.
3. **Local bypass.** A process on the node can still reach 127.0.0.1:11435. Maintenance scripts
   use it on purpose, for pulls that are not gated anyway.
4. **Restart loses in-flight work.** Restarting the sidecar drops its in-flight request (the
   upstream closes and Ollama cancels it). There is no lock state to recover.
5. **A static `Retry-After`** can be too short (wasted retries) or too long (idle GPU).
6. **Ollama API drift.** A new Ollama endpoint is gated by default (REQ-5), but changed
   streaming formats or new options could still surprise clients. This is managed by
   capturing fixtures for each version (REQ-56).
7. **The gateway is a single point of failure** for clients that only know it. They can fall
   back to a sidecar directly.
8. **`parameter_size` quality.** Routing trusts Ollama's metadata. A wrong or missing value
   gives `MODEL_SIZE_UNKNOWN` (fail-closed) or a route chosen by the operator's override.
9. **GPU memory figures are advisory.** `/models/select` and status read memory at a point in
   time. They are not used to gate requests.

## 9. Evaluation summary (the evidence cited above)

All of this comes from the CPU-only PPE: two Ollama 0.40.16 backends serving `qwen2.5:0.5b`
under size-named aliases, with packet capture used to see what each backend received. A
cleaned full copy is proposed for `docs/EVALUATION.md` (Q11).

| | predecessor | LiteLLM v1.108.9 | Olla v0.1.0 | lollms_hub | ollama_load_balancer |
|---|---|---|---|---|---|
| E1a native options incl. `num_ctx` | ◐ silently clamps `num_ctx` above a table | ❌ no native route | ✅ | ✅ (but hard-coded crop in code) | ✅ |
| E1b `num_ctx` on `/v1` | ❌ ignored by Ollama | ◐ top-level only | ❌ | ❌ (500) | ❌ |
| E2 busy → immediate 429 | ◐ global lock blocks idle B | ❌ queued 30 s (✅ with hook) | ❌ queued 28 s | ❌ queued | ❌ spills over, then 503 without `Retry-After` |
| E3 size routing, including a machine down | ❌ | ✅ (config) | ✅ (discovery + priority) | ❌ | ❌ |
| E4 Bearer auth | ✅ | ✅ | ❌ | ✅ ⚠ default secret | ❌ forwards client auth |
| E5 GPU memory | ✅ | ❌ | ❌ | ◐ | ❌ |
| E6 native tags / ps / version | ◐ primary only | ❌ | ◐ | ❌ | ◐ |

## 10. Slice plan

Rules for every slice:
- **Size.** At most about 15 changed files (lock files excluded). The review bundle stays under
  ~150 KB, below agy's ~180 KB limit; CI checks both.
- **Process.** The slice's TD is written and reviewed before its code. CI is green, and the PR
  pastes the coverage (≥ 80%) and mutation (≥ 75%) results. The cross-vendor review (codex and
  agy through the model-validator) runs, and every real finding is fixed. The PPE behaviour
  tests are run with their output pasted. Then stop for Scott's approval before any push.
- **Order.** Each slice leaves `main` releasable. Slices are ordered so that a sidecar is
  useful alone (S1 to S6) before the gateway exists (S7 to S10).

| slice | content | tests it adds | TD |
|---|---|---|---|
| **S0a** Scaffolding and CI | `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, `README.md`, `AGENTS.md` + `CLAUDE.md`, `.gitignore`, `.env.example`, uv workspace `pyproject.toml`, `.pre-commit-config.yaml` (gitleaks, ruff, private terms), `.gitleaks.toml` (IP-literal rule), CI workflow (§8.4 checks), PR template ("paste output") | CI proves each gate on a deliberately failing commit (shown in the PR) | none (Q8); reviewed as a PR |
| **S0b** PPE harness and real fixtures | `ppe/compose.yaml` (one Ollama + capture container; caps; localhost ports), `ppe/ppe` (up/down/purge, cap check, allowed-host check), `tools/capture_fixtures.py`, `tests/fixtures/ollama-0.40.16.json`, `docs/EVALUATION.md` (Q11) | `tests/tools/test_capture_fixtures.py`; `ppe/ppe check` refuses a compose file that is over the cap or not on localhost | specified in TD-slice-1 App. B |
| **S1** Contract + sidecar gate, 429, passthrough | `contract/openapi.yaml`, `node/` package: config, gate, proxy (streaming, disconnects, maximum hold, error mapping), health, logs | C1–C8, K1, G1–G9, H1–H7, B1–B4, S1–S8, E1–E5, HL1–HL2, L1, K-OAS1–3; PPE P1–P6 | **TD-slice-1** |
| **S2** Sidecar auth and TLS | keys (hashes, ids, scopes), `keygen` CLI, 401/403, `X-API-Key`, non-loopback listen allowed only with keys, optional TLS | A1–A10: missing, wrong and right key on every route class; scope matrix; constant-time compare path; key never in logs or upstream; startup refusals; TLS smoke | TD-slice-2 |
| **S3** `num_ctx` policy | body inspection without rewriting; limits by model pattern; `reject`/`flag` modes; `/v1` rule; the holder's model in status | N1–N8: over and under limit in each mode; no limit; `/v1` top-level and `options`; unparseable body; bytes still identical (wire capture in PPE) | TD-slice-3 |
| **S4** Status, memory, exposure self-check | `/seriatim/status`; `nvidia-smi` and macOS probes with a 2 s timeout; `/api/ps`; REQ-19 probe; degraded health | M1–M8: recorded `nvidia-smi` outputs (captured on a real node, with ids removed) and failures; macOS `sysctl` outputs; probe timeout → `unavailable`; exposure detected → 503 | TD-slice-4 |
| **S5** Deploy: Windows | `install.ps1`, WinSW XML templates, `check-local-only.ps1`, `update-models.ps1`, `weekly-maintenance.ps1`, scheduled-task registration | Pester tests with stubbed `Get-NetTCPConnection`, `Get-Service` and the `ollama` exit code; a manual run on one node, with output pasted | TD-slice-5 |
| **S6** Deploy: macOS | `install.sh`, launchd plists, `check-local-only.sh`, `update-models.sh`, `weekly-maintenance.sh`; records the LaunchAgent vs LaunchDaemon (Metal) result | bats tests with stubbed `lsof`, `launchctl` and `ollama`; a manual node run | TD-slice-6 |
| **S7** Gateway core | node config, discovery cache, size routing, passthrough, 429 passthrough or spill-over (Q1), failover before the request is sent, client auth, `NODE_REQUIRED` | R1–R14: routing table cases (including E3's "B down → 32B error"); 429 handling; connect failure vs failure after sending; client key not forwarded | TD-slice-7 |
| **S8** Gateway combined views | `/api/tags`, `/api/ps`, `/api/version` merge; partial flags; `/seriatim/nodes`; health | V1–V8, using captured multi-node fixtures | TD-slice-8 |
| **S9** `/models/select` | task preferences from config, routing, `fits_in_free_memory` | SEL1–SEL8: memory unknown → null; nothing fits; unknown task | TD-slice-9 |
| **S10** Gateway deploy + two-node PPE | Dockerfile, systemd example, a PPE with two nodes | P-GW1–P-GW6 (routing, never-too-small, busy isolation between nodes (F1), combined views, streaming end to end, failover) | TD-slice-10 |
| **S11** Release 0.1.0 | operator guide, supported Ollama versions, changelog, tag | full PPE run plus each node's local-only output, pasted | none |

---

## Appendix A — Contract draft (`contract/openapi.yaml`)

Slice S1 commits this file to `contract/openapi.yaml` and lints it in CI. The gateway paths
are specified now so that the sidecar and the gateway share one envelope and one set of
headers; their behaviour is built in S7 to S9. The Ollama API is passed through, not
redefined. Its paths are listed only to state how the gate treats them (`x-seriatim-gate`)
and which Seriatim responses they can add.

```yaml
openapi: 3.1.0
info:
  title: Seriatim
  version: 0.1.0
  summary: One-at-a-time GPU gate for Ollama (sidecar + gateway contract)
  license: { name: Apache-2.0, identifier: Apache-2.0 }
servers:
  - url: http://127.0.0.1:11434
    description: sidecar (x-seriatim-role sidecar)
  - url: http://127.0.0.1:8434
    description: gateway (x-seriatim-role gateway; port is an example)
security:
  - bearer: []
  - apiKey: []

components:
  securitySchemes:
    bearer: { type: http, scheme: bearer }
    apiKey: { type: apiKey, in: header, name: X-API-Key }

  # Machine-readable form of the ADR-001 §5.4 table (checked by K-OAS3).
  # status null = only ever sent as an in-stream StreamErrorRecord.
  x-seriatim-codes:
    GPU_BUSY:                  { status: 429, retryable: true }
    AUTH_MISSING:              { status: 401, retryable: false }
    AUTH_INVALID:              { status: 401, retryable: false }
    FORBIDDEN_SCOPE:           { status: 403, retryable: false }
    BAD_REQUEST_BODY:          { status: 400, retryable: false }
    NUM_CTX_UNSUPPORTED_ROUTE: { status: 400, retryable: false }
    NODE_REQUIRED:             { status: 400, retryable: false }
    UNKNOWN_TASK:              { status: 400, retryable: false }
    MODEL_NOT_FOUND:           { status: 404, retryable: false }
    REQUEST_BODY_TIMEOUT:      { status: 408, retryable: true }
    BODY_TOO_LARGE:            { status: 413, retryable: false }
    NUM_CTX_EXCEEDS_LIMIT:     { status: 422, retryable: false }
    MODEL_SIZE_UNKNOWN:        { status: 422, retryable: false }
    INTERNAL:                  { status: 500, retryable: true }
    UPSTREAM_UNAVAILABLE:      { status: 502, retryable: true }
    UPSTREAM_PROTOCOL_ERROR:   { status: 502, retryable: true }
    OLLAMA_EXPOSED:            { status: 503, retryable: true }
    NO_HEALTHY_NODE:           { status: 503, retryable: true }
    LOCK_HOLD_EXCEEDED:        { status: 504, retryable: true }
    UPSTREAM_STREAM_BROKEN:    { status: null, retryable: true }
  headers:
    Retry-After:
      description: Whole seconds before retrying.
      schema: { type: string, pattern: '^[0-9]+$' }
    X-Seriatim-Gate:
      schema: { type: string, enum: [acquired, ungated] }
    X-Seriatim-Node:
      schema: { type: string, pattern: '^[a-z0-9][a-z0-9-]{0,31}$' }
    X-Seriatim-Ctx:
      schema: { type: string, pattern: '^exceeds-limit; requested=[0-9]+; limit=[0-9]+$' }
    X-Seriatim-Partial:
      schema: { type: string }
    X-Request-Id:
      schema: { type: string, pattern: '^[A-Za-z0-9._-]{1,128}$' }

  schemas:
    ErrorCode:
      type: string
      enum: [GPU_BUSY, AUTH_MISSING, AUTH_INVALID, FORBIDDEN_SCOPE, BAD_REQUEST_BODY,
             NUM_CTX_UNSUPPORTED_ROUTE, NODE_REQUIRED, UNKNOWN_TASK, MODEL_NOT_FOUND,
             REQUEST_BODY_TIMEOUT, BODY_TOO_LARGE, NUM_CTX_EXCEEDS_LIMIT, MODEL_SIZE_UNKNOWN,
             INTERNAL, UPSTREAM_UNAVAILABLE, UPSTREAM_PROTOCOL_ERROR, OLLAMA_EXPOSED,
             NO_HEALTHY_NODE, LOCK_HOLD_EXCEEDED, UPSTREAM_STREAM_BROKEN]
    ErrorEnvelope:
      type: object
      required: [error, error_code, retryable, source]
      properties:
        error: { type: string, description: Human-readable; same key Ollama uses }
        error_code: { $ref: '#/components/schemas/ErrorCode' }
        retryable: { type: boolean }
        retry_after: { type: integer, minimum: 0 }
        source: { type: string, enum: [sidecar, gateway] }
        node: { type: string }
        busy_for_s: { type: number, minimum: 0 }
        limit: { type: integer, minimum: 1 }
        hint: { type: string }
      additionalProperties: false
    StreamErrorRecord:
      description: Last NDJSON line (or SSE data payload) before an aborted stream.
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
            enum: [ollama_unreachable, slot_overdue, ollama_exposed, no_healthy_node]
      additionalProperties: false
    Holder:
      type: object
      required: [path, started_at, elapsed_s]
      properties:
        path: { type: string }
        model: { type: [string, 'null'] }
        request_id: { type: string }
        started_at: { type: string, format: date-time }
        elapsed_s: { type: number, minimum: 0 }
    NodeStatus:
      type: object
      required: [node, version, lock, memory, loaded_models, ollama]
      properties:
        node: { type: string }
        version: { type: string }
        lock:
          type: object
          required: [slots, in_use, holders]
          properties:
            slots: { type: integer, minimum: 1 }
            in_use: { type: integer, minimum: 0 }
            holders: { type: array, items: { $ref: '#/components/schemas/Holder' } }
        memory:
          type: object
          required: [source, devices]
          properties:
            source: { type: string, enum: [nvidia-smi, apple-unified, unavailable] }
            error: { type: string }
            devices:
              type: array
              items:
                type: object
                required: [name, total_mb, used_mb, free_mb]
                properties:
                  name: { type: string }
                  total_mb: { type: integer, minimum: 0 }
                  used_mb: { type: integer, minimum: 0 }
                  free_mb: { type: integer, minimum: 0 }
        loaded_models:
          description: Ollama /api/ps "models" array, unchanged.
          type: array
          items: { type: object }
        ollama:
          type: object
          required: [reachable, local_only]
          properties:
            reachable: { type: boolean }
            version: { type: [string, 'null'] }
            local_only: { type: string, enum: ['true', 'false', unknown] }
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
      description: No free slot. Ollama did not receive the request.
      headers:
        Retry-After: { $ref: '#/components/headers/Retry-After' }
      content: { application/json: { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } }
    Error:
      description: Seriatim-produced error (see ErrorCode table in ADR-001 §5.4).
      content: { application/json: { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } }
    Unauthorized:
      description: Missing or invalid key.
      headers:
        WWW-Authenticate: { schema: { type: string, const: Bearer } }
      content: { application/json: { schema: { $ref: '#/components/schemas/ErrorEnvelope' } } }
    Passthrough:
      description: >
        Ollama's response, status and body bytes unchanged. Streams
        (application/x-ndjson, text/event-stream) are forwarded incrementally and, on a
        mid-stream failure, end with a StreamErrorRecord followed by an aborted connection.
      headers:
        X-Seriatim-Gate: { $ref: '#/components/headers/X-Seriatim-Gate' }
        X-Seriatim-Node: { $ref: '#/components/headers/X-Seriatim-Node' }
        X-Seriatim-Ctx: { $ref: '#/components/headers/X-Seriatim-Ctx' }
      content:
        application/json: {}
        application/x-ndjson: {}
        text/event-stream: {}

  x-seriatim-gated-operation: &gated
    x-seriatim-gate: gated
    requestBody:
      description: Opaque Ollama request; forwarded byte-for-byte. Seriatim may read
        `model`, `num_ctx` and `options.num_ctx` but never rewrites the body.
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
      '503': { $ref: '#/components/responses/Error' }
      '504': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }
  x-seriatim-ungated-operation: &ungated
    x-seriatim-gate: ungated
    responses:
      '200': { $ref: '#/components/responses/Passthrough' }
      '401': { $ref: '#/components/responses/Unauthorized' }
      '403': { $ref: '#/components/responses/Error' }
      '502': { $ref: '#/components/responses/Error' }
      default: { $ref: '#/components/responses/Passthrough' }

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
                required: [nodes]
                properties:
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

  # Ollama read-only views: raw passthrough at the sidecar; combined at the gateway (REQ-38),
  # where every entry gains seriatim_node(s) and partial results carry seriatim_partial.
  /api/tags:    { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /api/ps:      { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /api/version: { get: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }

  # Gated inference (REQ-5). Unlisted method+path pairs are also gated.
  /api/generate:        { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/chat:            { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/embed:           { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /api/embeddings:      { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/chat/completions: { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/completions:      { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/embeddings:       { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }
  /v1/responses:        { post: { <<: *gated, x-seriatim-roles: [sidecar, gateway] } }

  # Ungated (REQ-5). Admin scope (REQ-14) applies to pull/push/create/copy/delete/blobs
  # from slice S2; through the gateway they need X-Seriatim-Node (REQ-41).
  /api/show:   { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway] } }
  /api/pull:   { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin } }
  /api/push:   { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin } }
  /api/create: { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin } }
  /api/copy:   { post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin } }
  /api/delete: { delete: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin } }
  /api/blobs/{digest}:
    parameters: [{ name: digest, in: path, required: true, schema: { type: string } }]
    head: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin }
    post: { <<: *ungated, x-seriatim-roles: [sidecar, gateway], x-seriatim-scope: admin }
```

*Note for S1:* the YAML anchors under `x-` keys are a convenience of the draft. If
`openapi-spec-validator` rejects merge keys, S1 expands them and adds a test (K-OAS1) that
checks the expanded file matches the gated table in code.
