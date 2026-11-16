# Seriatim — Specification

**Status:** draft for review (Phase 1). **Date:** 2026-11-16.
**Companion documents:** [ADR-001](adr/ADR-001-architecture.md) (architecture, contract, threat
model, slice plan) and [TD-slice-1](design/TD-slice-1.md) (first slice only).

Seriatim is a one-at-a-time GPU gate for [Ollama](https://ollama.com). One sidecar sits in
front of Ollama on each GPU machine and admits only one inference request at a time. A request
that arrives while the GPU is busy gets an immediate HTTP 429 with `Retry-After`; it is never
queued. A thin gateway gives clients one address, routes each request to a machine by model
size, and combines the read-only Ollama endpoints across machines.

The words MUST, MUST NOT, SHOULD and MAY follow RFC 2119. Each requirement has an ID
(`REQ-n`) and an acceptance test. The test IDs are defined in §8.

---

## 1. Background

A predecessor gateway was compared on 2026-11-15 with LiteLLM, Olla, lollms_hub and
ollama_load_balancer, in a CPU-only test environment. That comparison is cited here as **the
evaluation**. Its test numbers are used as tags throughout: `E1a`, `E1b`, `E2` to `E6`. It
concluded that no candidate passes both *busy → immediate 429* (E2) and *Bearer auth* (E4)
without custom code. It also found four defects that this project must not repeat:

| tag | defect found in the predecessor | where this spec addresses it |
|---|---|---|
| F1 | One global lock: while machine A was busy, a request for idle machine B also got 429 | One lock per machine, in the sidecar: REQ-2 and REQ-36 |
| F2 | No routing by size: a 32B request went to the small machine | Size routing in the gateway: REQ-32 and REQ-33 |
| F3 | `num_ctx` above an internal table was silently lowered (30000 → 24576), and on the OpenAI route it had no effect | `num_ctx` is never lowered. It is rejected or flagged explicitly: REQ-7 to REQ-9 |
| F4 | `/api/tags`, `/api/ps` and `/api/version` showed only the primary machine | Combined across all machines: REQ-38 |

The evaluation's six requirements map to this spec as follows:

| evaluation | requirement here |
|---|---|
| E1a options unchanged on the native route, including `num_ctx` | REQ-6, REQ-7 |
| E1b `num_ctx` on the OpenAI route | REQ-9 |
| E2 busy → immediate 429 with `Retry-After` | REQ-2 to REQ-4, REQ-36 |
| E3 routing by size, including when a machine is down | REQ-32 to REQ-35, REQ-37 |
| E4 Bearer-key auth | REQ-13 to REQ-15, REQ-31 |
| E5 GPU-memory awareness | REQ-16, REQ-39 |
| E6 native `/api/tags`, `/api/ps` and `/api/version` | REQ-6 (sidecar), REQ-38 (gateway) |

## 2. Goals and non-goals

### Goals

1. **G1. Exclusive GPU.** At most *N* inference requests run on a GPU machine at a time, where
   *N* is configurable and defaults to 1.
2. **G2. Busy is a fast answer.** A request that cannot run now is told so at once, with a hint
   for when to retry. It never waits in a hidden queue.
3. **G3. Transparent.** Requests and responses pass through byte for byte, including streaming
   and every Ollama option. Existing Ollama clients work with nothing changed except the base
   URL and a Bearer key.
4. **G4. One address.** Clients use one gateway. It sends each request to the right machine
   for the model's size and shows a combined view of all machines.
5. **G5. Visible failure.** Every failure is an explicit, machine-readable error. Nothing is
   quietly downgraded.
6. **G6. Safe to publish.** Ollama is reachable only through the sidecar. Keys are never stored
   in plain text on GPU machines. The repository holds no host names, addresses or keys.
7. **G7. Replaceable.** The HTTP contract (ADR-001 Appendix A) is the interface. A future
   implementation in another language must pass the same black-box behaviour tests.

### Non-goals

- **Queueing or scheduling.** Seriatim rejects work; it never holds it. Retrying is the
  client's job.
- **Translating between APIs.** OpenAI-format requests are passed to Ollama's own `/v1`
  routes and are not converted to the native API. See REQ-9.
- **A model catalogue or knowledge base.** Model facts come from Ollama (`/api/tags`,
  `/api/show`, `/api/ps`) and from operator configuration, never from tables in the code.
- **Load balancing across identical replicas,** beyond a configured priority order.
- **Per-client quotas or rate limits,** or billing.
- **A web UI or cookie sessions.** Access is by Bearer key only.
- **Managing TLS certificates.** TLS is optional; see REQ-27.
- **Deploying Linux GPU machines.** The sidecar code is portable, but the deploy scripts
  cover only Windows and macOS.
- **Implementing the Rust version.** It is only a design constraint (G7).

## 3. Terms

| term | meaning |
|---|---|
| **node** | One GPU machine, running one Ollama and one sidecar |
| **sidecar** | `node/`: the process on port 11434 of a node. It is the lock authority |
| **gateway** | `gateway/`: the router clients talk to. It has no lock |
| **gated request** | A request that needs the GPU and so needs a slot (see REQ-5) |
| **slot / lease** | One unit of the node's concurrency limit, and the claim a gated request holds on it from admission to release |
| **Ollama** | The real Ollama server on the node, bound to `127.0.0.1:11435` |
| **error envelope** | The JSON error body defined in ADR-001 Appendix A (`ErrorEnvelope`) |
| **PPE** | The pre-production environment: Docker Compose with CPU-only micro-models (§8.3) |

## 4. Requirements

### 4.1 Sidecar: gate and passthrough

- **REQ-1. Placement.** The sidecar MUST listen on a configurable address and port (default
  port 11434) and forward to Ollama at a configured URL (default `http://127.0.0.1:11435`). It
  MUST refuse to start if the Ollama URL is not a loopback address.
  *Test:* C4, P1.
- **REQ-2. One at a time.** The sidecar MUST admit at most `SERIATIM_SLOTS` gated requests at
  once (an integer ≥ 1, default 1). The count MUST NOT be exceeded under any interleaving of
  arrivals, completions, disconnects and errors.
  *Test:* G1 to G9, B4.
- **REQ-3. Immediate 429.** When no slot is free, a gated request MUST get HTTP 429 with a
  `Retry-After` header (whole seconds, configurable, default 15) and an error envelope with
  `error_code: GPU_BUSY` and `retryable: true`. The sidecar MUST answer before reading the
  request body and without contacting Ollama. The p99 latency of this answer MUST be under
  100 ms in the PPE. A 429 from the sidecar guarantees that Ollama never received the request,
  so the client may retry it on any node.
  *Test:* B1, B3, P2.
- **REQ-4. No queue.** The sidecar MUST NOT wait for a slot to become free. It MUST set
  `OLLAMA_NUM_PARALLEL=1` in the deployed Ollama configuration as a second line of defence (the
  deploy check REQ-53 verifies this), but MUST NOT rely on Ollama's own queue.
  *Test:* B1 (the elapsed time of the rejected request).
- **REQ-5. Which requests are gated.** By default these are gated: `POST /api/generate`,
  `/api/chat`, `/api/embed`, `/api/embeddings`, `/v1/chat/completions`, `/v1/completions`,
  `/v1/embeddings` and `/v1/responses`. These are not gated: every `GET`, `HEAD` and
  `OPTIONS`, plus `POST /api/show`, `/api/pull`, `/api/push`, `/api/create`, `/api/copy`,
  `/api/blobs/*` and `DELETE /api/delete`. Any other method and path MUST be gated, so an
  Ollama endpoint added in future fails safe. The table MUST be configurable.
  *Test:* K1, H6, H7.
- **REQ-6. Passthrough.** The sidecar MUST forward the method, path, query string and request
  body bytes unchanged. It MUST return Ollama's status code and response body bytes unchanged,
  including `application/x-ndjson` and `text/event-stream` streams, and `ollama pull` progress.
  Streams MUST be forwarded incrementally: a chunk reaches the client before Ollama sends the
  next one. Only hop-by-hop headers, `Host`, `Authorization` and `X-API-Key` are changed or
  removed (ADR-001 §5.3).
  *Test:* H1 to H5, S1 to S3, P1, P3, P5.
- **REQ-7. Options unchanged.** The sidecar and the gateway MUST NOT parse the body and
  re-serialise it, add or remove fields, or change any option, including `num_ctx`. They MUST
  NEVER lower `num_ctx`.
  *Test:* H2, P1.
- **REQ-8. `num_ctx` limit (optional).** An operator MAY configure a maximum `num_ctx` for
  each model name pattern. When a request asks for more, the configured mode applies:
  `reject` returns HTTP 422 `NUM_CTX_EXCEEDS_LIMIT`, naming the limit, and Ollama is not
  contacted. `flag` forwards the request unchanged and adds the response header
  `X-Seriatim-Ctx: exceeds-limit; requested=<n>; limit=<m>`. There is no clamp mode. With no
  limits configured, nothing is checked. (The default mode is open question Q2.)
  *Test:* slice S3.
- **REQ-9. `num_ctx` on the OpenAI route.** Ollama's `/v1` routes ignore `num_ctx` (E1b). A
  `/v1/*` request that carries `num_ctx`, either at the top level or in `options`, MUST NOT be
  forwarded silently. By default it gets HTTP 400 `NUM_CTX_UNSUPPORTED_ROUTE`, with a hint to
  use the native `/api/chat` (open question Q3).
  *Test:* slice S3.
- **REQ-10. Release.** A slot MUST be released exactly once for each admission: when the
  response completes, when the client disconnects (before or after the response headers), when
  Ollama fails, when the request fails validation, and when the maximum hold time passes. Before
  the slot is released, the upstream connection MUST be closed, which makes Ollama cancel the
  generation.
  *Test:* B2, S4 to S8, E1, E3 to E5.
- **REQ-11. No forced release.** The sidecar MUST NOT release a slot while the request that
  holds it may still be talking to Ollama. A "stale lock reaper" that frees a live request's
  slot breaks REQ-2. A leaked slot must show up as a visible fault (REQ-18), never as two
  requests running at once.
  *Test:* G5, S7 (the slot is freed only after the upstream connection is closed).
- **REQ-12. Maximum hold.** A gated request MUST be cut off once it has held its slot for
  `SERIATIM_MAX_HOLD_S` seconds (default 1800). Before the response headers are sent, the client
  gets HTTP 504 `LOCK_HOLD_EXCEEDED`. After the headers are sent, the client gets an error record
  and then an aborted connection, as in REQ-24. The event is logged at `CRITICAL`.
  *Test:* S7, S8.

### 4.2 Sidecar: authentication

- **REQ-13. Bearer keys.** Every sidecar endpoint except `GET /seriatim/health` MUST require
  `Authorization: Bearer <key>`. `X-API-Key: <key>` MUST also be accepted, for compatibility
  with existing clients (open question Q7). Keys MUST NOT be accepted in query strings or
  cookies. The sidecar MUST compare SHA-256 hashes of keys in constant time. A missing key gets
  HTTP 401 `AUTH_MISSING` and a wrong key gets HTTP 401 `AUTH_INVALID`, both with
  `WWW-Authenticate: Bearer` and `retryable: false`. Credentials MUST NOT be forwarded to Ollama
  or written to any log.
  *Test:* slice S2.
- **REQ-14. Scopes.** Each key MUST carry the scope `infer`, `admin`, or both. `admin` is
  required for `/api/pull`, `/api/push`, `/api/create`, `/api/copy`, `/api/delete` and
  `/api/blobs/*`. A key without the needed scope gets HTTP 403 `FORBIDDEN_SCOPE` (open question
  Q5).
  *Test:* slice S2.
- **REQ-15. Fail closed on missing keys.** If no keys are configured, the sidecar MUST refuse to
  start. The only exception is when `SERIATIM_ALLOW_UNAUTHENTICATED=true` is set. That setting
  is meant for the PPE, and the sidecar logs a `WARNING` at startup and every hour while it is
  in effect. Until the authentication slice (S2) exists, the sidecar MUST refuse to listen on a
  non-loopback address unless that flag is set.
  *Test:* C5, slice S2.

### 4.3 Sidecar: status, health and memory

- **REQ-16. GPU memory.** `GET /seriatim/status` MUST report GPU memory read locally. On NVIDIA
  this comes from `nvidia-smi` (total, used and free for each device). On Apple Silicon it is
  unified memory (`hw.memsize`) plus the `size_vram` of loaded models from `/api/ps`. The report
  MUST say where each figure came from (`nvidia-smi`, `apple-unified` or `unavailable`). If a
  probe fails or times out (2 s), the source MUST be `unavailable` with an `error` string. The
  sidecar MUST NOT invent default figures.
  *Test:* slice S4.
- **REQ-17. Loaded models and lock state.** `GET /seriatim/status` MUST include Ollama's
  `/api/ps` list, unchanged, and the state of every slot: total, in use, and for each holder
  its path, model (when the body was parseable), start time and elapsed seconds.
  *Test:* G6, slice S4.
- **REQ-18. Health.** `GET /seriatim/health` (no authentication) MUST return 200
  `{"status":"ok"}` only when Ollama answers `/api/version` within 2 s, no slot has been held
  beyond its maximum hold time plus 30 s, and the local-only self-check (REQ-19) has not failed.
  Otherwise it MUST return 503 `{"status":"degraded","reasons":[...]}`. The body MUST NOT
  include versions, models or addresses.
  *Test:* HL1, HL2, slice S4.
- **REQ-19. Ollama exposure self-check.** At startup and every 5 minutes, the sidecar MUST try
  to connect to Ollama's port on each of the machine's non-loopback interface addresses. If any
  connection succeeds, Ollama is reachable without the sidecar. The sidecar MUST then log
  `CRITICAL`, report health as degraded, and answer gated requests with HTTP 503
  `OLLAMA_EXPOSED` until a later check passes. This setting MAY be changed to warn-only, which
  is logged at every check.
  *Test:* slice S4.

### 4.4 Sidecar: limits, process and logging

- **REQ-20. Request limits.** A request body larger than `SERIATIM_MAX_BODY_BYTES` (default
  64 MiB) MUST get HTTP 413 `BODY_TOO_LARGE` before anything is forwarded. A body that has not
  fully arrived within `SERIATIM_BODY_TIMEOUT_S` (default 30) MUST get HTTP 408
  `REQUEST_BODY_TIMEOUT`. In both cases the slot is released.
  *Test:* E3, E4.
- **REQ-21. Single process.** The lock is in-process state, so the sidecar MUST run as exactly
  one process with one event loop. It MUST NOT offer a setting for more workers.
  *Test:* C8.
- **REQ-22. Logs.** The sidecar MUST write one JSON log line per request, with: time, request
  id, method, path, gated, outcome, status, model (if known), slot wait and hold times, and the
  byte counts in each direction. Logs MUST NOT contain request or response bodies, or
  credentials.
  *Test:* L1.
- **REQ-23. Configuration.** All configuration MUST come from `SERIATIM_*` environment
  variables, or from an optional TOML file named by `SERIATIM_CONFIG`. Environment variables
  take precedence. An unknown `SERIATIM_*` key, a value that is out of range, or a missing
  config file MUST stop startup with a non-zero exit and a message naming the key. The
  repository MUST hold only `.env.example`, with placeholder values.
  *Test:* C1 to C7.
- **REQ-24. Errors inside a stream.** If Ollama fails, or the hold limit passes, after the
  response headers were sent, the sidecar MUST send one final error record with a valid
  `error_code`. For NDJSON this is a JSON line, `{"error":...,"error_code":...}`; for SSE it is
  a `data:` event. The sidecar MUST then abort the connection without the terminating chunk, so
  that clients cannot mistake the response for a complete one.
  *Test:* S6, S7.
- **REQ-25. Error envelope.** Every error that Seriatim itself produces MUST use the error
  envelope: `error` (a string, so Ollama clients that read `error` still work), `error_code`,
  `retryable`, `source` (`sidecar` or `gateway`), and `retry_after` where it applies. Errors
  from Ollama are passed through unchanged.
  *Test:* K-OAS2, K-OAS3.
- **REQ-26. Upstream failures.** If the connection to Ollama is refused or times out, the client
  MUST get HTTP 502 `UPSTREAM_UNAVAILABLE`. A protocol error before the response headers MUST
  give 502 `UPSTREAM_PROTOCOL_ERROR`. Any unexpected internal error MUST give 500 `INTERNAL`.
  In every case the slot is released.
  *Test:* E1, E5.
- **REQ-27. TLS (optional).** The sidecar and the gateway MUST accept a certificate and key
  file through configuration and then serve HTTPS. The default is plain HTTP (open question Q6).
  *Test:* slice S2.

### 4.5 Gateway

- **REQ-30. One address.** The gateway MUST serve the native Ollama API, the `/v1` routes,
  `/models/select`, `GET /seriatim/health` and `GET /seriatim/nodes`. Ollama clients MUST work
  against it with nothing changed except the base URL and key.
  *Test:* slice S7.
- **REQ-31. Gateway auth.** The gateway MUST authenticate clients exactly as REQ-13 to REQ-15
  require. It MUST authenticate to each sidecar with that node's own key, kept in gateway
  configuration. A client's key MUST NOT be forwarded to a node.
  *Test:* slice S7.
- **REQ-32. Routing by size.** For a request that names a model, the candidate nodes MUST be
  those that are healthy, list the model in their `/api/tags`, and have a configured
  `max_params_b` at least the model's size. Candidates are ordered by configured priority. The
  model's size comes from `details.parameter_size` in `/api/tags`. A model whose size is unknown
  can go only to a node with no `max_params_b`, or to a node named for that model in
  `route_overrides`. Otherwise it gets HTTP 422 `MODEL_SIZE_UNKNOWN`. Model lists are refreshed
  every 60 s, and right away when a model is not found.
  *Test:* slice S7, P-GW1.
- **REQ-33. Never too small.** The gateway MUST NOT send a model to a node whose `max_params_b`
  is smaller than the model, even when every larger node is down (E3: "B down → 32B gets an
  error, never sent to A").
  *Test:* slice S7, P-GW2.
- **REQ-34. Unknown model.** A model that no node lists MUST get HTTP 404 `MODEL_NOT_FOUND`.
  *Test:* slice S7.
- **REQ-35. No healthy node.** If a model is known but no node that can take it is healthy, the
  request MUST get HTTP 503 `NO_HEALTHY_NODE` with `Retry-After`.
  *Test:* slice S7.
- **REQ-36. Busy is passed through.** The gateway MUST NOT have a lock or a queue. When a node
  answers 429, the gateway MUST either pass that 429 through, with its status, `Retry-After` and
  body unchanged plus `X-Seriatim-Node`, or try the next candidate once, without waiting. Which
  of these it does is open question Q1. A 429 MUST NEVER reach the client when an eligible
  candidate is idle, if Q1 chooses spill-over. A node's busy state MUST NOT affect any other
  node (F1).
  *Test:* slice S7, P-GW3.
- **REQ-37. Node failure.** If the connection to a node fails before the request was sent, the
  gateway MUST mark the node down and try the next candidate. If a node fails after the request
  was sent, the gateway MUST NOT retry (the request may not be idempotent). The client gets 502
  or an in-stream error, as in REQ-24. A node marked down is checked again through
  `/seriatim/health` every 15 s.
  *Test:* slice S7, P-GW2.
- **REQ-38. Combined views.** `GET /api/tags` MUST return the union of every node's models,
  with each entry carrying `seriatim_nodes: [node ids]`. `GET /api/ps` MUST return every node's
  list, with each entry carrying `seriatim_node`. `GET /api/version` MUST return the lowest
  version, plus `seriatim_nodes: {id: version}`. If any node did not answer, the response MUST
  include `seriatim_partial: [ids]` and the header `X-Seriatim-Partial`. If no node answered, it
  MUST be HTTP 503 `NO_HEALTHY_NODE` (F4).
  *Test:* slice S8, P-GW4.
- **REQ-39. `/models/select`.** `GET` and `POST /models/select` MUST pick a model and node for a
  `task`, using operator-configured preference lists (task → ordered models), model presence,
  routing (REQ-32) and free memory from each node's `/seriatim/status`. The response MUST give
  the model, the node, `fits_in_free_memory` (true, false, or null when memory is unavailable)
  and its reasons. An unknown task gets HTTP 400 `UNKNOWN_TASK`. There MUST NOT be a built-in
  model table.
  *Test:* slice S9.
- **REQ-40. Gateway passthrough.** The gateway MUST meet REQ-6, REQ-7 and REQ-24 end to end.
  *Test:* slice S7, P-GW1.
- **REQ-41. Requests without a model.** Admin requests that act on one node, such as
  `/api/pull`, `/api/push`, `/api/create`, `/api/copy`, `/api/delete` and `/api/blobs/*`, MUST
  name the node in `X-Seriatim-Node`. Otherwise they get HTTP 400 `NODE_REQUIRED`.
  *Test:* slice S7.
- **REQ-42. Gateway health.** `GET /seriatim/health` returns 200 when at least one node is
  healthy, and 503 otherwise. `GET /seriatim/nodes` (authenticated) returns each node's id,
  health, priority, `max_params_b`, last check time and, if fetched, its `/seriatim/status`.
  *Test:* slice S8.

### 4.6 Repository and process

- **REQ-50. Licence and contributing.** Apache-2.0 `LICENSE`. `CONTRIBUTING.md` says that
  bug-fix PRs are welcome and that features start as an issue.
- **REQ-51. No identifying data.** The repository MUST NOT contain host names, IP addresses
  (other than loopback, `0.0.0.0` and the RFC 5737 documentation ranges such as `192.0.2.0/24`), keys or tokens. CI enforces this (REQ-63).
- **REQ-52. Short agent instructions.** `AGENTS.md` (with `CLAUDE.md` pointing to it) is under
  100 lines and holds only the working rules and the repo commands.

## 5. Failure behaviour

Seriatim fails **closed and loudly**. A timeout, an unavailable dependency, or an answer it
cannot parse is never treated as success, and nothing is silently downgraded.

| condition | sidecar | gateway |
|---|---|---|
| No free slot | 429 `GPU_BUSY` + `Retry-After`; body not read; Ollama not contacted | Passes the node's 429 through, or spills over once (Q1) |
| Ollama refuses the connection, or the connect times out | 502 `UPSTREAM_UNAVAILABLE`; slot released | Marks the node down; tries the next candidate (safe, nothing was sent) |
| Ollama fails before the response headers | 502 `UPSTREAM_PROTOCOL_ERROR`; slot released | 502 passed through; no retry |
| Ollama fails mid-stream | Final error record, then the connection is aborted; slot released | Same, end to end |
| Hold limit passes | 504 `LOCK_HOLD_EXCEEDED`, or an in-stream error record; upstream closed, then slot released; `CRITICAL` log | Passed through |
| Client disconnects | Upstream closed, then slot released; logged as `client_disconnect` | Closes its connection to the node |
| Ollama returns 4xx/5xx | Passed through unchanged | Passed through unchanged |
| Body too large, or arrives too slowly | 413 / 408; slot released | Same limits applied at the gateway |
| `num_ctx` over the configured limit | 422, or forwarded with the `X-Seriatim-Ctx` flag (Q2); never lowered | Passed through |
| `num_ctx` on `/v1` | 400 `NUM_CTX_UNSUPPORTED_ROUTE` (Q3) | Passed through |
| GPU probe fails | `memory.source = unavailable` + `error`; never invented figures | `fits_in_free_memory: null` |
| Ollama reachable from the LAN | 503 `OLLAMA_EXPOSED` on gated routes; health degraded | Node unhealthy; traffic goes elsewhere |
| Invalid or unknown config key | Exits non-zero, naming the key | Same |
| No keys configured | Refuses to start (REQ-15) | Same |
| Model not on any node / size unknown | — | 404 `MODEL_NOT_FOUND` / 422 `MODEL_SIZE_UNKNOWN` |
| Some nodes unreachable for a combined view | — | Partial result, flagged (`seriatim_partial`, `X-Seriatim-Partial`) |
| All nodes unreachable | — | 503 `NO_HEALTHY_NODE` |
| A leaked slot (a bug) | Health degraded, status shows the holder's age; never force-released (REQ-11) | Node unhealthy |

Scripts follow the same rule. Every native command's exit code is checked, and any failure
makes the script exit non-zero. A `try/catch` around a native command, which never fires in
PowerShell, does not count as a check.

## 6. Security

### 6.1 Ollama is reachable only through the sidecar

There are four independent layers. Any one of them failing is detected by another.

1. **Bind.** Ollama runs with `OLLAMA_HOST=127.0.0.1:11435`. The sidecar refuses a
   non-loopback upstream (REQ-1).
2. **Firewall.** The Windows Firewall rule (or the macOS application firewall rule) allows
   inbound traffic to port 11434 (the sidecar) only. No rule exists for 11435.
3. **Deploy check (REQ-53).** It runs after install, after every Ollama upgrade and weekly. It
   checks that port 11435 listens only on loopback, that port 11434 belongs to the sidecar
   process, that Ollama's own auto-start (the tray app or login item) is disabled, and that the
   Ollama service environment has `OLLAMA_HOST` and `OLLAMA_NUM_PARALLEL=1`. A failure exits
   non-zero and is logged.
4. **Runtime self-check (REQ-19).** The sidecar probes Ollama's port from every non-loopback
   address. If Ollama is exposed, gated requests are refused.

### 6.2 Key handling

- **Generation.** `seriatim-node keygen --id <client> --scope infer[,admin]` prints a new
  256-bit random key once, plus the line to add to the config (`id`, `scope`, `sha256`). The
  key itself is never written to disk by Seriatim.
- **Storage on nodes.** Only SHA-256 hashes are stored. Plain SHA-256 is enough because keys
  are 256-bit random values, not passwords; no slow hash is needed.
- **Storage on the gateway.** The gateway must send each node's key, so it keeps that key in
  plain text in its config file or environment. The file must be readable only by the service
  account; the install script sets an ACL on Windows and mode 0600 on macOS and Linux. Client
  keys at the gateway are stored as hashes.
- **Rotation and revocation.** Several keys can be valid at once. To rotate a key, add the new
  hash, move the client over, then remove the old hash and restart. Each key has an id, which
  appears in logs as `client`; the key never does.
- **Transport.** The default is plain HTTP on a trusted LAN, which leaves keys open to anyone
  who can sniff that LAN. This is a residual risk (ADR-001 §8), and TLS is available
  (REQ-27, Q6).
- **In the repository.** `.env.example` holds placeholders only. gitleaks runs in the
  pre-commit hook and in CI (REQ-63). A gitignored `.private-terms` file, and the same list in a
  CI secret, block named hosts and other private strings.

The full threat model is in ADR-001 §7.

## 7. Deployment and upgrade

Every script is driven by configuration (a `seriatim.env` or TOML file beside it, with no host
names in the repository), is idempotent, logs with timestamps, and exits non-zero on any
failure.

- **REQ-53. Local-only check** (`deploy/<os>/check-local-only`). It checks everything in §6.1
  layer 3. It runs at the end of install and model update, inside weekly maintenance, and on
  its own. *Test:* Pester (Windows) and bats (macOS) tests with stubbed system commands; a
  manual run on each node, with its output pasted into the deploy PR.
- **REQ-54. Model update** (`deploy/<os>/update-models`). It pulls each installed model (or the
  models on a configured list) from the local Ollama, checks each pull's exit code, prints one
  line per model, exits non-zero if any pull failed, and then runs the local-only check.
- **REQ-55. Weekly maintenance** (`deploy/<os>/weekly-maintenance` plus a scheduler entry). It
  runs the model update, the local-only check and log rotation, and writes a summary line. It
  does not update Seriatim itself (open question Q10).
- **REQ-56. Ollama upgrades.** These steps run in order, and a failure stops the sequence:
  1. In the PPE, capture fixtures with the new Ollama version (`tools/capture_fixtures`).
  2. Run the contract and behaviour tests against those fixtures and that container.
  3. Upgrade one node.
  4. Run the local-only check.
  5. Run a smoke request through the gateway.
  6. Upgrade the rest of the nodes.

  The supported Ollama versions are listed in `README.md`.

### Windows (GPU node)

- **REQ-57.** `deploy/windows/install.ps1`:
  - installs Python 3.12+ and the sidecar into `%ProgramFiles%\seriatim` (a venv from a tagged
    release);
  - sets up config in `%ProgramData%\seriatim\`, readable only by the service account and
    Administrators;
  - registers two services with WinSW: `seriatim-ollama` (`ollama serve`, with `OLLAMA_HOST`,
    `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MODELS`) and `seriatim-node`, which depends on it;
  - disables the Ollama tray app's auto-start;
  - adds a firewall rule for inbound TCP 11434 on the Private profile only;
  - runs the local-only check.
- The weekly task is registered with Task Scheduler and runs as the service account.
- **Upgrading Seriatim:** stop `seriatim-node`, install the new tagged release into the venv,
  start the service, then run the local-only check.

### macOS (Apple Silicon node)

- **REQ-58.** `deploy/macos/install.sh`:
  - installs the sidecar into `/usr/local/seriatim` (a venv from a tagged release);
  - sets up config in `/usr/local/etc/seriatim/` (mode 0600);
  - adds launchd plists `org.seriatim.ollama` (`ollama serve`, with environment as on Windows)
    and `org.seriatim.node`, both with `KeepAlive`;
  - removes the Ollama.app login item;
  - adds an application-firewall allow rule for the sidecar's Python;
  - runs the local-only check.
- Whether the plists are LaunchAgents or LaunchDaemons depends on whether Metal is available
  to a daemon. Slice S6 verifies this and records the result.
- The weekly job runs from a launchd `StartCalendarInterval`.

### Gateway host

- **REQ-59.** The gateway ships an OCI image (Dockerfile) and an example systemd unit. Its
  configuration lists the nodes with id, URL, key, priority and `max_params_b`. Examples use
  only placeholders such as `node-a.example`.

## 8. Test plan

**Checks are executed, not attested.** CI on GitHub Actions is the authority for tests,
coverage, mutation and secret scanning; local runs are only pre-checks. A PR description
quotes the CI result or pasted command output. It never just says "tests pass".

### 8.1 Unit and component tests

- **REQ-60.** pytest, with branch coverage **≥ 80%** for each package (`node/`, `gateway/`),
  enforced by `--cov-fail-under=80` in CI on `ubuntu-latest`. The unit suite also runs on
  `windows-latest` and `macos-latest`, because the sidecar runs on both.
- Component tests start the real sidecar (uvicorn on an ephemeral loopback port) in front of a
  scriptable fake Ollama (also a real HTTP server). This lets them test streaming, disconnects
  and timing over real sockets.
- **Real data over synthetic data.** Fake-Ollama responses are replayed from **captured real
  Ollama responses** (`tests/fixtures/ollama-<version>.json`, recorded in the PPE by
  `tools/capture_fixtures`). Hand-written fixtures are added only for faults that cannot be
  captured (connection resets, stalls, malformed chunks).

### 8.2 Mutation testing

- **REQ-61.** mutmut on the Python files changed in the PR, under `node/` and `gateway/`. Score
  = killed ÷ (all mutants − mutants mutmut marks as skipped). Survivors, mutants with no tests
  and suspicious mutants all count against the score, and timeouts count as killed. The score
  must be **≥ 75%**. CI enforces it and prints the surviving mutants; the PR quotes the score.

### 8.3 HTTP behaviour tests (PPE)

- **REQ-62.** These are black-box HTTP tests (`tests/behaviour/`), parametrised by base URL,
  so the same suite can run against a future implementation in another language.
- They run in a **Docker Compose PPE on the designated PPE host**: a CPU-only development
  machine. The PPE never runs on, or connects to, a production or GPU machine. The host name
  is not in the repository. `ppe/ppe up` refuses to start unless the local hostname matches the
  gitignored `ppe/.allowed-host`.
- **Limits.** The sum of the per-service limits must be ≤ **4 CPUs and 6 GiB**. Every
  published port must be on `127.0.0.1`. `ppe/ppe up` checks both before starting, and refuses
  to start if either is broken.
- **Models.** CPU micro-models, for example `qwen2.5:0.5b`, with size-named aliases made by
  `ollama cp` (such as `…:14b` and `…:32b`). Aliases share blobs, so routing can be checked
  without large downloads.
- **Topology.** Each node is an Ollama container with a sidecar container in its network
  namespace. Ollama binds `127.0.0.1:11435` inside that namespace, as in production.
  A packet-capture container in the same namespace records the raw bytes Ollama receives, so
  "Ollama received X" comes from the wire, not from a proxy. `/api/ps` `context_length`, read
  directly, shows the `num_ctx` that was loaded.
- **Suites.** P1 to P6 test the sidecar alone. P-GW1 to P-GW6 test the gateway with two nodes,
  from slice S10. Each slice says which of these it adds.
- **Who runs it.** The PPE runs on the PPE host before each push, and the output is pasted into
  the PR. Whether it should also run in CI is open question Q9.

### 8.4 CI checks (rules as checks, not prose)

- **REQ-63.** CI on `ubuntu-latest` runs:
  - ruff (lint and format);
  - pytest with coverage (REQ-60);
  - mutmut (REQ-61);
  - the contract lint (`openapi-spec-validator` on `contract/openapi.yaml`);
  - gitleaks (a pinned binary, checksum-verified; the GitHub Action needs a licence for
    organisations);
  - the private-terms check (from a CI secret);
  - an IP-literal check;
  - a PR-size check: ≤ 15 changed files (lock files excluded) and a reviewable diff
    ≤ 150 KB, below agy's ~180 KB prompt limit.

  Actions are pinned by commit SHA. Dependencies are locked (`uv.lock`).

### 8.5 Test-ID index (sidecar, slice S1)

Defined in full in TD-slice-1 §8: C1 to C8 (config), K1 (classification), G1 to G9 (gate),
H1 to H7 (passthrough), B1 to B4 (busy), S1 to S8 (streaming and disconnects), E1 to E5
(errors), HL1 and HL2 (health), L1 (logs), K-OAS1 to K-OAS3 (contract), P1 to P6 (PPE).

## 9. Open questions for Scott

Each question is a decision. It gives a recommendation, the options, and the risks.

**Q1. When a node is busy, should the gateway spill over to a mirror?**
*Recommendation: (b), spill over once.*
- (a) Always pass the 429 straight through.
  - Pros: simplest; matches the stated direction.
  - Cons: a client can get 429 while an idle machine has the same model, which the evaluation
    counted against LiteLLM in E2.
- (b) On a node's 429, try the next eligible candidate once, without waiting. Pass the 429
  through only if every candidate is busy, using the smallest `Retry-After`.
  - Pros: a sidecar 429 guarantees Ollama never saw the request, so the retry is safe; it uses
    idle mirrors.
  - Cons: small models run on the large machine more often.
- Risk of (b): the large machine is taken by small-model overflow just as a large request
  arrives. A per-node setting `accept_spillover: false` limits this.

**Q2. What should the default `num_ctx` limit mode be?** *Recommendation: `reject`.*
- `reject`
  - Pros: fail-closed; an over-limit context can spill layers to the CPU and hold the GPU for
    minutes.
  - Cons: breaks callers that relied on the old silent clamp.
- `flag`
  - Pros: never breaks a caller.
  - Cons: the flag is easy to ignore, which is close to a silent failure.
- Risk: either way, a limit applies only to models the operator configures. Without limits,
  Seriatim checks nothing and passes `num_ctx` unchanged.

**Q3. `num_ctx` on `/v1`: reject, flag, or translate?** *Recommendation: reject (400) with a
hint.*
- Translating `/v1` to `/api/chat`:
  - Pros: it works.
  - Cons: it is a translation layer (a non-goal), changes bytes, and duplicates LiteLLM.
- Flagging:
  - Cons: the request runs with 2048 context; the flag only tells the client afterwards that
    it was ignored.
- Risk of rejecting: OpenAI-SDK clients that send `num_ctx` in `extra_body` start failing. That
  is intended, because they are getting a 2048 context today.

**Q4. Keep the old "CPU lane" (`device: cpu` / `num_gpu: 0` bypasses the lock)?**
*Recommendation: not in v1. Later, perhaps an opt-in `exempt_models` list (for example an
embedding model), off by default.*
- Pros of keeping it: embeddings don't wait behind long generations.
- Cons: `device` is not an Ollama field, so honouring it means editing the body, which breaks
  REQ-7. A CPU model can also make Ollama evict the GPU model when `OLLAMA_MAX_LOADED_MODELS`
  is low.
- Risk of dropping it: embedding callers see 429 during long jobs.

**Q5. Should admin endpoints (`pull`, `push`, `create`, `copy`, `delete`) need an `admin`
scope?** *Recommendation: yes (REQ-14).*
- Pros: an inference key can no longer delete models, fill the disk, or push to a registry
  (`/api/pull` can also make the node fetch from any URL).
- Cons: one more key to manage, and maintenance scripts need an admin key (or a direct local
  call to 11435).
- Risk if no: any leaked key can delete every model.

**Q6. TLS by default?** *Recommendation: optional now (REQ-27), plain HTTP on a trusted LAN by
default, revisit before v1.0.*
- Pros of making it the default: keys and prompts cannot be sniffed on the LAN.
- Cons: someone must issue and renew certificates on three or more machines; it is easy to get
  wrong, and an expired certificate is an outage.
- Risk of the default: anyone on the LAN can read keys and prompts.

**Q7. How far should the new design stay compatible with existing clients?**
*Recommendation:*
- keep the field names `error`, `error_code`, `retryable` and `retry_after`, and the code
  `GPU_BUSY`;
- accept `X-API-Key`;
- drop the `auto:*` model aliases, the `device` field, `X-Execution-Device` and
  `X-Model-Digest` (clients can read digests from `/api/tags`).

Risk: clients that parse the dropped headers need a small change. Scott should confirm which
existing clients read them.

**Q8. Should repository scaffolding be its own slice (S0) without a TD?**
*Recommendation: yes.*
- What S0 holds: licence, CI, pre-commit, PPE harness and fixture capture. There is no runtime
  code to design, so it needs no TD; TD-slice-1 Appendix B specifies what slice 1 needs from
  it.
- Pros: keeps slice 1 within 15 files.
- Cons: two PRs before the first feature.
- Risk: S0's CI rules (mutation on changed files, PR size) are only exercised for real by S1.

**Q9. Should the PPE behaviour tests also run in CI?** *Recommendation: yes. Use a nightly and
label-triggered job on `ubuntu-latest` with a cached micro-model, in addition to the PPE-host
run.*
- Pros: the CPU micro-models fit easily in a runner, and it makes the behaviour tests "executed,
  not attested".
- Cons: about 5 minutes per run, plus downloading a ~400 MB model when the cache misses.
- Risk: runner timing is noisy for the 100 ms latency check. CI asserts a looser bound, and the
  PPE host asserts the strict one.

**Q10. Should weekly maintenance update Seriatim itself?** *Recommendation: no. Seriatim
updates only by a manual, tagged-release install; maintenance updates models and runs checks.*
- Pros: no unreviewed code reaches the GPU machines automatically.
- Cons: fixes arrive only when someone installs them.
- Risk: nodes drift onto different versions. `/seriatim/nodes` shows each node's version, so
  the drift is visible.

**Q11. Should a cleaned copy of the evaluation be published in this repo?**
*Recommendation: yes, as `docs/EVALUATION.md` in S0, without private names or paths. ADR-001
already summarises the parts it cites.*
- Pros: the public "why this exists" is checkable.
- Cons: it needs a careful pass to remove private names.
- Risk: a leaked internal name. The private-terms check covers this.
