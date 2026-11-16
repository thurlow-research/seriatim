# Seriatim — Specification

**Status:** revision 2, for review (Phase 1). **Date:** 2026-11-16.

**Companion documents:**
- [ADR-001](adr/ADR-001-architecture.md): architecture, contract, threat model, slice plan.
- [TD-slice-1](design/TD-slice-1.md): the first code slice only.
- [EVALUATION](EVALUATION.md): the comparison that led here.

Seriatim is a one-at-a-time GPU gate for [Ollama](https://ollama.com). It has two parts:
- **A sidecar on each GPU machine.** It sits in front of Ollama and admits a limited number of
  inference requests at a time, one by default. A request that arrives while the GPU is busy
  gets an immediate HTTP 429 with `Retry-After`; it is never queued.
- **A thin gateway.** It gives clients one address, routes each request to a machine by model
  size, and combines the read-only Ollama views across machines.

The words MUST, MUST NOT, SHOULD and MAY follow RFC 2119. Each requirement has an ID (`REQ-n`)
and the tests that prove it. The test IDs for slice 1 are defined in TD-slice-1 §8; the tests
for later slices are named in ADR-001 §10.

---

## 1. Background

The [evaluation](EVALUATION.md) (2026-11-15) compared a predecessor gateway with LiteLLM, Olla,
lollms_hub and ollama_load_balancer in a CPU-only test environment. Its test numbers are used
here as tags: `E1a`, `E1b`, and `E2` to `E6`. No candidate passed both *busy → immediate 429*
(E2) and *Bearer auth* (E4) without custom code. The evaluation also found four defects:

| tag | defect found in the predecessor | addressed by |
|---|---|---|
| F1 | One global lock: while machine A was busy, a request for idle machine B also got 429 | One lock per machine, in the sidecar (REQ-2); busy state never couples machines (REQ-36) |
| F2 | No routing by size: a 32B request went to the small machine | Routing by size (REQ-32, REQ-33) |
| F3 | `num_ctx` above an internal table was silently lowered (30000 → 24576), and on the OpenAI route it had no effect | `num_ctx` is never changed; an over-limit value is rejected (REQ-7 to REQ-9) |
| F4 | `/api/tags`, `/api/ps` and `/api/version` showed only one machine | Combined across machines (REQ-38) |

| evaluation requirement | requirements here |
|---|---|
| E1a native options unchanged, including `num_ctx` | REQ-6, REQ-7 |
| E1b `num_ctx` on the OpenAI route | REQ-9 |
| E2 busy → immediate 429 with `Retry-After` | REQ-2 to REQ-4, REQ-36 |
| E3 routing by size, including a machine down | REQ-32 to REQ-35, REQ-37 |
| E4 Bearer-key auth | REQ-13 to REQ-15, REQ-31 |
| E5 GPU-memory awareness | REQ-16, REQ-39 |
| E6 native `/api/tags`, `/api/ps` and `/api/version` | REQ-6 (sidecar), REQ-38 (gateway) |

## 2. Goals and non-goals

### Goals

1. **G1. Exclusive GPU.** At most *N* inference requests run on a GPU machine at a time; *N*
   is configurable and defaults to 1.
   - **What is guaranteed.** The sidecar never *admits* more than *N*. It covers every request
     that reaches Ollama through the sidecar, and its exact scope is given in §6.1.
   - **The one window.** After an abnormal end, a slot waits a fixed drain time before it is
     reused (REQ-10). Ollama gives no signal that a cancelled generation has stopped. If it
     takes longer than the drain, Ollama's own one-at-a-time setting (REQ-4) makes the next
     request wait inside Ollama, so the two still do not run together. That wait is the one
     place a request can queue, and it is bounded by the cancellation time.
2. **G2. Busy is a fast answer.** A request that cannot run now is told so at once, with a hint
   for when to retry. It never waits in a hidden queue.
3. **G3. Transparent.** Request and response bodies pass through byte for byte, including
   streams and every Ollama option. Existing Ollama clients work with nothing changed except
   the base URL and a Bearer key.
4. **G4. One address.** Clients use one gateway. It sends each request to a machine suited to
   the model's size and shows a combined view of every machine.
5. **G5. Visible failure.** Every failure is an explicit, machine-readable error. A timeout,
   an unavailable dependency or an answer that cannot be parsed is never treated as success,
   and is never quietly downgraded.
6. **G6. Safe to publish.**
   - Ollama is reachable only through the sidecar.
   - GPU machines never store keys in plain text.
   - The repository holds no host names, addresses or keys.
7. **G7. Replaceable.** The HTTP contract (ADR-001 Appendix A) is the interface. A future
   implementation in another language must pass the same black-box behaviour tests.

### Non-goals

- **Queueing or scheduling.** Seriatim rejects work; it never holds it.
- **Translating between APIs.** OpenAI-format requests go to Ollama's own `/v1` routes,
  unconverted (REQ-9).
- **A model catalogue.** Model facts come from Ollama and from operator configuration, never
  from tables in the code.
- **A CPU lane that bypasses the lock.** It was dropped for v1 (ruling Q4); see §9.
- **Load balancing** across identical replicas, beyond a configured priority order.
- **Per-client quotas, rate limits or billing.**
- **A web UI, cookie sessions or CORS support.**
- **HTTP/2.** Seriatim speaks HTTP/1.1 only, at both ends.
- **Managing TLS certificates.** TLS is optional in v1 (REQ-27).
- **Deploy scripts for Linux GPU machines.** The sidecar code is portable, but the scripts
  cover Windows and macOS only.
- **The Rust version.** It is only a design constraint (G7).

## 3. Terms

| term | meaning |
|---|---|
| **node** | One GPU machine, running one Ollama and one sidecar |
| **sidecar** | `node/`: the process on port 11434 of a node; the lock authority |
| **gateway** | `gateway/`: the router clients talk to; it has no lock |
| **slot** | One unit of a node's concurrency limit. A slot is *free*, *held* (by one admitted request), *draining* (§4.1, REQ-10) or *stuck* (closing its upstream connection could not be proven; freed only by a restart) |
| **gated request** | A request that needs a slot (REQ-5) |
| **admission 429** | The sidecar's own busy answer (REQ-3), marked by `X-Seriatim-Gate: busy`. An Ollama 429 passed through is *not* one |
| **replay-safe** | Ollama provably never received the request, so it may be sent again anywhere (REQ-25) |
| **Ollama** | The real Ollama server on the node, bound to `127.0.0.1:11435` |
| **error envelope** | The JSON error body Seriatim produces (ADR-001 §5.4) |
| **PPE** | The Docker Compose test environment with CPU-only micro-models (§8.3) |

## 4. Requirements

### 4.1 Sidecar: classification, gate and passthrough

- **REQ-1. Placement.**
  - The sidecar MUST listen on a configurable address and port (default port 11434).
  - It MUST forward to Ollama at a configured URL (default `http://127.0.0.1:11435`).
  - It MUST refuse to start if that URL is not a loopback literal, or if it is HTTPS. Plain
    HTTP over loopback is the only upstream.

  *Tests:* C4, P1.
- **REQ-2. One at a time.**
  - The sidecar MUST admit at most `SERIATIM_NODE_SLOTS` gated requests at once (an integer
    ≥ 1, default 1).
  - A *draining* slot counts as in use.
  - The limit MUST hold under every interleaving of arrivals, completions, disconnects,
    errors, keep-alive reuse and cancellation.

  *Tests:* G1–G10, B4, H8.
- **REQ-3. Immediate admission 429.** When no slot is free, a gated request MUST get HTTP 429
  with all of these:
  - `Retry-After` in whole seconds: the configured value (default 15), or the time left in a
    drain, whichever is larger;
  - `X-Seriatim-Gate: busy`;
  - an error envelope with `error_code: GPU_BUSY`, `retryable: true`, `replay_safe: true`,
    `source: sidecar` and `retry_after`.

  The sidecar MUST answer **before reading the request body** and **without contacting
  Ollama**. CI proves both, with packet capture in the PPE. CI also checks a latency bound of
  p99 < 250 ms in the PPE; the in-process component test bound is 100 ms.

  *Tests:* B1, B3, P2.
- **REQ-4. No queue.**
  - The sidecar MUST NOT wait for a slot.
  - The deployed Ollama MUST run with `OLLAMA_NUM_PARALLEL=1`, and the deploy check REQ-53
    verifies it. Admission never relies on it. It is the backstop for the drain window
    described in G1.

  *Test:* B1 (the rejected request's latency).
- **REQ-5. Classification.** Every request is classified once, from its method and its **raw**
  path, into one of three classes (`classify()` in TD-slice-1 §2.2). That one result decides
  both admission (gated or not) and, from slice S2, the scope a key needs.
  - **Seriatim's own paths.** Anything under `/seriatim/` is reserved. A known path with the
    right method is served by Seriatim. A known path with the wrong method gets 405
    `METHOD_NOT_ALLOWED`. Any other path under `/seriatim/` gets 404 `NOT_FOUND`. These are
    never forwarded to Ollama.
  - **Explicit allowlist (ungated).** Exact method and path pairs, each verified to exist in
    the supported Ollama version. S0b probes every listed pair against that version and
    records the result, and a test checks the allowlist against the record:
    - `GET /`, `HEAD /`, `GET /api/tags`, `GET /api/ps`, `GET /api/version`, `POST /api/show`
      and `GET /v1/models` need the `infer` scope;
    - `POST /api/pull`, `POST /api/push`, `POST /api/copy`, `DELETE /api/delete`, `HEAD /api/blobs/sha256:<64 hex>` and
      `POST /api/blobs/sha256:<64 hex>` need the `admin` scope.
  - **Known inference (gated, `infer` scope):** `POST /api/generate`, `POST /api/chat`,
    `POST /api/embed`, `POST /api/embeddings`, `POST /v1/chat/completions`,
    `POST /v1/completions` and `POST /v1/embeddings`.
  - **Gated, `admin` scope:** `POST /api/create`. Converting and quantising a model loads the
    machine, so it waits for the GPU like inference does.
  - **Everything else** is gated **and** needs the `admin` scope (ruling Q5). This covers
    unknown paths, other methods on known paths (including `OPTIONS`) and new Ollama
    endpoints. A new endpoint therefore fails safe.
  - **Paths are matched exactly.** No trailing-slash folding, no case folding and no segment
    collapsing.

  *Tests:* K1, K2, H6, H7.
- **REQ-6. Passthrough.**
  - The sidecar MUST forward the method, the raw path, the query string and the request body
    bytes unchanged.
  - It MUST return Ollama's status and body bytes unchanged, including `application/x-ndjson`
    and `text/event-stream` streams and `/api/pull` progress.
  - It MUST forward streams incrementally: a chunk reaches the client before Ollama sends the
    next one.
  - Only the headers listed in ADR-001 §5.3 are changed. Those are hop-by-hop headers, `Host`,
    credentials, `Accept-Encoding`, CORS headers and Seriatim's reserved headers.

  *Tests:* H1–H5, S1–S3, P1, P3, P5.
- **REQ-7. Options unchanged.** Seriatim MUST NOT re-serialise a body, add or remove a field, or
  change any option. It MUST never lower `num_ctx`. A body is read only to apply REQ-8 and
  REQ-9.

  *Tests:* H2, P1.
- **REQ-8. `num_ctx` limit (ruling Q2: reject).**
  - An operator MAY set a maximum `num_ctx` per model pattern (exact names or `*` globs). Two
    patterns that can match the same name are a configuration error at startup; S3 defines a
    deterministic overlap test.
  - A request over its limit gets HTTP 422 `NUM_CTX_EXCEEDS_LIMIT`, with `limit` and a `hint`.
    Ollama is not contacted.
  - There is no clamp mode and no flag mode. With no limits configured, nothing is checked.
  - **Strict inspection.** Each of these gets 400 `NUM_CTX_AMBIGUOUS` and is never guessed at:
    duplicate JSON keys at any level the check reads; a `num_ctx` that is not a positive
    integer (booleans, floats, strings and negatives are all rejected); and `num_ctx` set both
    at the top level and in `options`, even to equal values.
  - A gated JSON body that cannot be parsed while a check needs it gets 400
    `BAD_REQUEST_BODY`.

  *Tests:* slice S3 (N1–N10).
- **REQ-9. `num_ctx` on `/v1` (ruling Q3: reject with a hint).** Ollama's `/v1` routes ignore
  `num_ctx` (E1b). A `/v1/*` request that carries `num_ctx`, at the top level or in `options`,
  MUST get 400 `NUM_CTX_UNSUPPORTED_ROUTE`. Its `hint` names the native `/api/chat`, where
  `options.num_ctx` works. The strict inspection rules of REQ-8 apply.

  *Tests:* slice S3.
- **REQ-10. Release and drain.** A held slot ends in one of two ways:
  - **Clean end.** Ollama's response finished normally, and the sidecar read all of it from
    Ollama. The slot becomes *free*. The GPU work is over at that point. If the final write
    to the client then fails, that is logged, but it does not change the slot, because
    nothing is running on Ollama any more.
  - **Abnormal end.** The client disconnected, Ollama failed, the hold limit passed, or an
    internal error occurred after the request was sent to Ollama. The upstream connection MUST
    be closed first. The slot then becomes *draining* for `SERIATIM_NODE_DRAIN_S` (default 10,
    at least 1), and only then free.

  Closing a connection makes Ollama cancel the generation, but it does not prove that the GPU
  has stopped. The drain covers that window, and it is time-based, not proof (G1).
  - **Choosing the default.** It is checked against the cancellation latency measured in the
    PPE (P6). S5 and S6 measure it again on each GPU node, and record it.
  - **Replacing it.** S4 may replace it with a positive signal, but only if one is shown to be
    reliable.
  - **Unproven closure.** If closing the upstream connection fails or times out, closure is
    unproven. The slot then stays *stuck* until the sidecar restarts, health reports it, and
    it is never freed by a timer (REQ-11).
  - A request rejected before anything was sent (413, 408, `BAD_PATH`) frees its slot at once.
  - While a slot drains, `/seriatim/status` reports it, and new gated requests get the
    admission 429.

  *Tests:* B2, S4–S9, E1, E3–E5, F1–F5.
- **REQ-11. No forced release.** The sidecar MUST NOT free a slot while the request holding it
  may still be talking to Ollama. A leaked slot shows up as a fault (REQ-18), never as two
  requests running at once.

  *Tests:* G5, S7, F4.
- **REQ-12. Maximum hold.** A request MUST be cut off once it has held its slot for
  `SERIATIM_NODE_MAX_HOLD_S` seconds (default 1800).
  - Before the response headers were sent: HTTP 504 `LOCK_HOLD_EXCEEDED`.
  - After the headers were sent: an error record or an abort, as in REQ-24.
  - In both cases: upstream closed, then drain, and a `CRITICAL` log line.

  *Tests:* S7, S8.
- **REQ-12a. Restart.** On startup, the sidecar MUST treat every slot as draining for
  `SERIATIM_NODE_DRAIN_S`, and answer gated requests with 503 `NODE_STARTING` plus
  `Retry-After`. A previous sidecar process that was killed with work in flight had its
  upstream sockets closed by the operating system, which is the same cancellation that the
  drain covers. Ollama is not restarted with the sidecar; ADR-001 D10 says why.

  *Tests:* R1, R2.
- **REQ-12b. One sidecar per Ollama.**
  - At startup the sidecar MUST take an exclusive, host-wide lock (an OS file lock) named
    after its upstream's port (`upstream-<port>.lock`). The upstream is always loopback, so
    the port identifies it on the host.
  - **Where the lock lives.** The lock directory is fixed and system-wide:
    `%ProgramData%\seriatim\run` on Windows, `/usr/local/var/run/seriatim` on macOS,
    `/run/seriatim` on Linux. Every account therefore meets the same lock.
  - **No override in production.** `SERIATIM_NODE_LOCK_DIR` can change it only in PPE and
    test mode (`ALLOW_UNAUTHENTICATED=true`). That way two production sidecars can never be
    configured apart.
  - **Second line of defence.** The local-only check (REQ-53) also verifies that exactly one
    sidecar process runs on the host.
  - If the lock is held by another process, or cannot be created, the sidecar MUST exit with
    code 3 and a message.

  *Tests:* R3, R4.

### 4.2 Sidecar: authentication (slice S2)

- **REQ-13. Bearer keys.**
  - Every endpoint except `GET /seriatim/health` MUST require `Authorization: Bearer <key>`.
  - Keys MUST NOT be accepted in query strings, cookies or any other header. `X-API-Key` is
    not accepted; no existing client uses it (ruling Q7).
  - **Ambiguous credentials** get 400 `AUTH_AMBIGUOUS`: more than one `Authorization` header,
    a scheme other than Bearer, or a Bearer value with embedded whitespace.
  - The sidecar MUST compare the request key's SHA-256 against **every** configured hash with
    a constant-time comparison, without stopping early.
  - A missing key gets 401 `AUTH_MISSING`. A wrong key gets 401 `AUTH_INVALID`. Both carry
    `WWW-Authenticate: Bearer` and `retryable: false`.
  - Credentials MUST NOT be forwarded to Ollama or logged.

  *Tests:* slice S2 (A1–A12).
- **REQ-14. Scopes (ruling Q5).**
  - A key has the scope `infer` or `admin`. The admin key is a separate key; an `admin` key
    may also use `infer` routes.
  - The scope each route needs comes from REQ-5, so unknown endpoints need `admin`.
  - A key without the scope a route needs gets 403 `FORBIDDEN_SCOPE`.

  *Tests:* slice S2.
- **REQ-15. Fail closed on missing keys.**
  - With no keys configured, the sidecar MUST refuse to start, unless
    `SERIATIM_NODE_ALLOW_UNAUTHENTICATED=true` is set. That setting exists for the PPE; it logs
    a `WARNING` at startup and, from S2, every hour.
  - The sidecar MUST refuse a non-loopback listen address unless authentication is configured
    and the exposure check (REQ-19) is active. In slices S1 to S3 that means loopback only,
    except in the PPE.

  *Tests:* C5, slice S2.

### 4.3 Sidecar: status, health, memory and exposure (slice S4, unless noted)

- **REQ-16. GPU memory, measured only.** `GET /seriatim/status` reports `memory`:
  - **NVIDIA:** from `nvidia-smi`, the total, used and free memory of each device.
  - **Apple Silicon:** the total from `hw.memsize`. `used_mb` and `free_mb` are `null`, because
    unified memory has no measured "free GPU memory". The sum of `size_vram` from `/api/ps` is
    reported separately, as `models_vram_mb`.
  - Every figure names its source. A probe that fails or times out (2 s) gives
    `source: unavailable` and an `error`. Figures are never invented.

  *Tests:* M1–M8.
- **REQ-17. Loaded models and slots.** Status includes:
  - Ollama's `/api/ps` list, unchanged. If `/api/ps` fails or cannot be parsed, the field is
    `loaded_models: null` with `loaded_models_error`, never an empty list.
  - Each slot's state (free, held, draining or stuck), and for each holder its path, its model (when
    known), its start time, its elapsed time and any time left to drain.

  *Tests:* G6, M5, M6.
- **REQ-18. Health (slice S1, extended in S4).** `GET /seriatim/health` requires no
  authentication.
  - It returns 200 `{"status":"ok"}` only when all of these hold:
    - Ollama answers `GET /api/version` within 2 s with HTTP 200 and a JSON object whose
      `version` is a non-empty string;
    - no slot has been held longer than its maximum hold plus 30 s, and no slot is stuck
      because closing its upstream connection timed out (TD-slice-1 §3.3);
    - the node is not starting;
    - from S4, the exposure check has passed.
  - Otherwise it returns 503, with `status: degraded` and a list of reasons.
  - The body never contains versions, models or addresses.

  *Tests:* HL1–HL4.
- **REQ-19. Exposure check (ruling: fail closed).**
  - At startup and every 5 minutes, the sidecar tries to connect to Ollama's port on each of
    the host's non-loopback interface addresses, IPv4 and IPv6. IPv6 link-local addresses are
    probed with their interface scope id.
  - **No gated request is admitted until the first check has passed.**
  - If any connection succeeds, gated requests get 503 `OLLAMA_EXPOSED` (`retryable: false`).
  - If the addresses cannot be listed, a probe times out, or the check is incomplete, gated
    requests get 503 `EXPOSURE_UNVERIFIED` (`retryable: true`). An uncertain check never
    counts as a pass.
  - There is no warn-only mode.
  - **Limits of the check.** It is a detector, not the enforcement boundary. The boundary is
    the bind address and the firewall (§6.1).
    - **The window.** Between two checks, a rebind can go unnoticed for up to 5 minutes.
    - **Blind spots.** A probe from the host itself cannot see port forwarding or NAT done
      elsewhere.
    - **Deploy order.** The deploy scripts therefore stop the sidecar before they reconfigure
      Ollama, and the check runs again before admission resumes.

  *Tests:* X1–X6.

### 4.4 Sidecar: limits, process, errors and logging

- **REQ-20. Request limits.**
  - **Buffered bodies.** Every request body is read completely before anything is forwarded,
    with one exception (below).
    - A body larger than `SERIATIM_NODE_MAX_BODY_BYTES` (default 64 MiB) gets 413
      `BODY_TOO_LARGE`.
    - A body that has not fully arrived within `SERIATIM_NODE_BODY_TIMEOUT_S` (default 30)
      gets 408 `REQUEST_BODY_TIMEOUT`.
    - In both cases nothing is forwarded, so the error is replay-safe, and the slot is freed
      at once.
  - **Blob uploads are streamed.** `POST /api/blobs/sha256:<hex>` (admin, ungated) can carry
    gigabytes, so it is streamed with no size limit. `BODY_TIMEOUT_S` then applies between
    chunks.
    - A stall or a disconnect before the first byte is forwarded gets 408
      `REQUEST_BODY_TIMEOUT`.
    - After the first byte, it gets 408 `UPLOAD_INTERRUPTED`, which is **not** replay-safe.

  *Tests:* E3, E4, E6, H10.
- **REQ-20a. Path safety.** Before classifying, the sidecar MUST reject with 400 `BAD_PATH`
  any raw path that contains any of the following. The check is case-insensitive and runs
  before any decoding or normalisation:
  - a path that does not start with `/`;
  - a `.` or `..` segment, `%2e` (`.`), `%2f` (`/`), `%5c` (`\`), `%00`, or a literal `\`;
  - an empty segment (`//`);
  - a NUL byte or a control character (below `0x20`, or `0x7f`).

  *Tests:* K2.
- **REQ-21. Single process, HTTP/1.1.** The lock is in-process state, so the sidecar runs as one
  process with one event loop. There is no setting for more workers. It serves HTTP/1.1 only
  (h11), and talks HTTP/1.1 only to Ollama. Any other HTTP version gets 505
  `HTTP_VERSION_UNSUPPORTED`. This covers HTTP/1.0 in particular, because an HTTP/1.0
  response ends when the connection closes, so an abort would look like a complete
  response.

  *Tests:* C8.
- **REQ-22. Logs.**
  - One JSON line per request, with the fields given in TD-slice-1 Appendix A.
  - Never a request or response body, and never a credential.

  *Test:* L1.
- **REQ-23. Configuration.**
  - The sidecar reads only `SERIATIM_NODE_*` environment variables, plus an optional TOML file
    named by `SERIATIM_NODE_CONFIG`; environment variables take precedence. The gateway reads
    only `SERIATIM_GW_*`. Other `SERIATIM_*` variables are ignored, so developer tooling
    variables cannot collide.
  - An unknown key under the process's own prefix, a value out of range, or a missing config
    file MUST stop startup with exit code 2 and a message that names the key.
  - Only `.env.example`, with placeholder values, is committed.

  *Tests:* C1–C7.
- **REQ-24. Errors after the response headers.** If Ollama fails, or the hold limit passes,
  after the headers were sent:
  - **Error record.** The sidecar MAY send one final error record, but only when it can be
    framed safely. That means the response is chunked (no `Content-Length`), its
    `Content-Encoding` is identity (the sidecar strips `Accept-Encoding` upstream, so Ollama
    does not compress), its content type is NDJSON or SSE, and the bytes sent so far end at a
    record boundary (`\n` for NDJSON, `\n\n` for SSE). The record is one NDJSON line, or one
    SSE `data:` event. A partial line or event is never completed, because the client would
    parse a truncated record.
  - **Abort.** It then MUST abort the connection without the terminating chunk. When a record
    cannot be framed safely, it only aborts.

  Either way, the client cannot mistake the response for a complete one.

  *Tests:* S6, S7, S10.
- **REQ-25. The error envelope.** Every error Seriatim produces uses the envelope in ADR-001
  §5.4:
  - `error` (a string, so Ollama clients still work), `error_code`, `retryable`,
    `replay_safe` and `source`, plus `retry_after` where it applies;
  - `replay_safe` is true only when Ollama provably never received the request;
  - `retryable` and `replay_safe` come from one table, never from the call site.

  Errors from Ollama itself are passed through unchanged, as described in the contract's
  `UpstreamError`.

  *Tests:* K-OAS2, K-OAS3.
- **REQ-26. Upstream failures.**
  - Connection refused or a connect timeout: 502 `UPSTREAM_UNAVAILABLE`, with
    `replay_safe: true`.
  - A protocol error or reset before the response headers: 502 `UPSTREAM_PROTOCOL_ERROR`,
    with `replay_safe: false`.
  - An unexpected internal error: 500 `INTERNAL`, with `replay_safe: false`.
  - The slot follows REQ-10 in every case.

  *Tests:* E1, E5, F1–F3.
- **REQ-27. TLS (ruling Q6: optional in v1; slice S11).** Both the sidecar and the gateway
  accept a certificate and key file, and then serve HTTPS. The default is plain HTTP.

### 4.5 Gateway (slices S7 to S10)

- **REQ-30. One address.** The gateway serves:
  - the native Ollama API and the `/v1` routes;
  - `/models/select`;
  - `GET /seriatim/health` and `GET /seriatim/nodes`;
  - `GET /` and `HEAD /`, which answer 200 `Ollama is running` when at least one node is
    healthy, and 503 `GPU_OFFLINE` otherwise.

  Its classification uses the same table as REQ-5.
- **REQ-31. Gateway auth.**
  - Clients are authenticated as REQ-13 to REQ-15 require.
  - The gateway authenticates to each node with that node's own keys (one `infer` key and one
    `admin` key), kept in its configuration.
  - A client's key is never forwarded.
- **REQ-32. Routing by size.**
  - **Candidates.** For a request that names a model, the candidates are the nodes that meet
    all three of these, in configured priority order:
    - healthy;
    - the node's last discovery listed the model;
    - their `max_params_b` is at least the model's size.
  - **Model size.** The size comes from `size_overrides` in the gateway configuration, or
    otherwise from `details.parameter_size` in `/api/tags`.
  - **Size unknown.** A model can then go only to a node with no `max_params_b`. Otherwise it
    gets 422 `MODEL_SIZE_UNKNOWN`.
  - **Discovery** refreshes every 60 s, and immediately when a model is missing.
- **REQ-33. Never too small.** The gateway MUST NOT send a model to a node whose `max_params_b`
  is smaller than the model, even when every larger node is down (E3).
- **REQ-34. Unknown model vs unknown state.**
  - 404 `MODEL_NOT_FOUND` only when **every** configured node answered its last discovery
    with a valid `/api/tags`, and none lists the model.
  - If a node is unreachable, or answered with something that cannot be parsed, its state is
    unknown. A model not found on the others then gets 503 `DISCOVERY_INCOMPLETE` with
    `Retry-After`.
- **REQ-35. No healthy node.** If the model is known but no candidate is healthy, the request
  gets 503 `GPU_OFFLINE` with `retryable: false` and `Retry-After`. The code and the
  `retryable` value match what existing clients already handle (ruling Q7).
- **REQ-36. Busy and spill-over (ruling Q1).** The gateway has no lock and no queue.
  - **Admission 429.** When a candidate answers with an admission 429 (`X-Seriatim-Gate: busy`
    and `error_code: GPU_BUSY` with `source: sidecar`), the gateway MUST try the next eligible
    candidate at once. It tries each eligible candidate **once**, in priority order, and never
    waits.
  - **All busy.** If every candidate gives an admission 429, the client gets that 429 with the
    **smallest** `Retry-After`.
  - **Ollama's 429.** An Ollama 429 (no `X-Seriatim-Gate: busy`) is passed through and never
    triggers spill-over.
  - **No coupling.** One node's busy state never affects another node (F1).
  - **Reroute headers.** A response served after spill-over carries `X-Rerouted-From: <first
    node id>` and `X-Reroute-Reason: busy`.
- **REQ-37. Node failure.**
  - Only `replay_safe: true` failures may be retried on another candidate: a failed connect to
    the node, an admission 429, `NODE_STARTING`, `NODE_SATURATED`, `EXPOSURE_UNVERIFIED` or
    `OLLAMA_EXPOSED`. Only `UPSTREAM_UNAVAILABLE` and a failed connect mark a node down;
    `NODE_SATURATED` does not.
  - **Only routed requests fail over.** A request the gateway routed itself may be retried on
    another candidate. A request that names its node in `X-Seriatim-Node`, which every admin
    request must (REQ-41), is never retried elsewhere. Its failure goes back to the
    client.
    Those nodes are marked down or busy as appropriate.
  - A failure after the request reached Ollama is never retried; the client gets it as in
    REQ-24.
  - A node that is down is checked again through `/seriatim/health` every 15 s.
- **REQ-38. Combined views.**
  - `GET /api/tags` returns the union of all nodes' model lists, and each entry carries
    `seriatim_nodes`.
  - `GET /api/ps` returns every node's list, and each entry carries `seriatim_node`.
  - `GET /api/version` returns the lowest version, plus `seriatim_nodes: {id: version}`.
  - If some nodes did not answer, the response carries `seriatim_partial` and the
    `X-Seriatim-Partial` header. If none answered, it is 503 `GPU_OFFLINE`.
  - The schemas are in the contract (F4).
- **REQ-39. `/models/select`.**
  - It picks a model and a node for a `task`, from operator-configured preference lists, model
    presence, routing (REQ-32) and measured free memory.
  - `fits_in_free_memory` is `null` whenever memory is not measured (REQ-16). There is no
    estimation in v1.
  - An unknown task gets 400 `UNKNOWN_TASK`.
- **REQ-40. Gateway passthrough.** The gateway meets REQ-6, REQ-7 and REQ-24 end to end.
- **REQ-41. Admin requests through the gateway** (pull, push, create, copy, delete, blobs, and
  unknown endpoints) MUST name a node in `X-Seriatim-Node`. Otherwise they get 400
  `NODE_REQUIRED`.
- **REQ-42. Gateway health.**
  - `/seriatim/health` returns 200 when at least one node is healthy, otherwise 503.
  - `/seriatim/nodes` (authenticated) returns each node's id, health, priority,
    `max_params_b`, last check time and status.
- **REQ-43. Compatibility (ruling Q7).** The headers and fields existing clients read keep
  their names. §7.1 documents the full compatibility surface.

### 4.6 Repository and process

- **REQ-50. Licence and contributing.** MIT licence. `CONTRIBUTING.md` says that bug-fix PRs
  are welcome and that features start as an issue.
- **REQ-51. No identifying data.**
  - The repository holds no host names, no IP addresses other than the allowed ones, and no
    keys or tokens. The allowed addresses are loopback, `0.0.0.0` and the RFC 5737
    documentation ranges, such as `192.0.2.0/24`.
  - It holds no personal names other than the licence's copyright line, and no
    developer-workflow tooling.
  - CI enforces this (REQ-63).
- **REQ-52. Project infrastructure only.** The repository holds only:
  - the code and its tests;
  - CI and the secret scan;
  - the docs;
  - `.gitignore`, `.env.example`, the licence, CODEOWNERS and CONTRIBUTING.

## 5. Failure behaviour

| condition | sidecar | gateway |
|---|---|---|
| No free slot, or a slot is draining | 429 `GPU_BUSY` + `Retry-After` + `X-Seriatim-Gate: busy`; body not read; Ollama not contacted | Tries each other eligible node once; if all are busy, the smallest `Retry-After` |
| The sidecar is starting | 503 `NODE_STARTING` + `Retry-After` | Tries the next node (replay-safe) |
| Ollama 429 or 503 | Passed through unchanged | Passed through; no spill-over |
| Connection to Ollama refused, or connect timeout | 502 `UPSTREAM_UNAVAILABLE`, replay-safe; slot freed | Marks the node down; tries the next node |
| Ollama fails before the response headers | 502 `UPSTREAM_PROTOCOL_ERROR`, not replay-safe; upstream closed, then drain | Passed through; no retry |
| Ollama fails mid-stream | A framed error record if safe, then abort; then drain | The same, end to end |
| Hold limit passes | 504, or a record and abort; upstream closed, then drain; `CRITICAL` log | Passed through |
| Client disconnects | Upstream closed, then drain | Closes its node connection |
| Sidecar killed or restarted | OS closes upstream sockets; the new process starts with all slots draining | Node unhealthy, then recovers |
| A second sidecar for the same Ollama | Exit code 3 at startup | — |
| Body too large or too slow | 413 / 408; slot freed at once | The same limits apply |
| Unsafe raw path | 400 `BAD_PATH` | 400 `BAD_PATH` |
| An HTTP version other than 1.1 | 505 `HTTP_VERSION_UNSUPPORTED` | The same |
| A streamed blob upload interrupted after bytes were forwarded | 408 `UPLOAD_INTERRUPTED`, not replay-safe | Passed through; no retry |
| The ungated connection pool is full | 503 `NODE_SATURATED`; inference is unaffected (it has its own connections) | A routed request tries the next node; one aimed at a named node gets the 503. The node is not marked down |
| Closing the upstream connection fails or times out | Slot *stuck*; health degraded; freed only by a restart (REQ-10) | Node unhealthy |
| Unknown `/seriatim/*` path, or the wrong method on one | 404 `NOT_FOUND` / 405 `METHOD_NOT_ALLOWED` | The same |
| `num_ctx` over its limit, ambiguous, or sent on `/v1` | 422 / 400; Ollama not contacted | Passed through |
| GPU probe or `/api/ps` fails | `source: unavailable` / `loaded_models: null`, each with an error | `fits_in_free_memory: null` |
| Ollama reachable from the LAN | 503 `OLLAMA_EXPOSED` on gated routes; health degraded | Node unhealthy |
| Exposure check uncertain | 503 `EXPOSURE_UNVERIFIED`; never treated as safe | Tries the next node |
| Invalid or unknown config key | Exit code 2, naming the key | The same |
| No keys configured | Refuses to start (REQ-15) | The same |
| Model missing / node state unknown / no healthy node / size unknown | — | 404 `MODEL_NOT_FOUND` / 503 `DISCOVERY_INCOMPLETE` / 503 `GPU_OFFLINE` / 422 `MODEL_SIZE_UNKNOWN` |
| Some nodes unreachable in a combined view | — | Partial result, flagged |
| A slot leaked through a bug | Health degraded, and status shows the holder's age; never force-freed (REQ-11) | Node unhealthy |

Scripts follow the same rule. Every native command's exit code is checked, and any failure
makes the script exit non-zero. In PowerShell, a `try`/`catch` around a native command never
fires, so it does not count as a check.

## 6. Security

### 6.1 Ollama is reachable only through the sidecar

**Scope of the guarantee.** The one-at-a-time guarantee covers every request that reaches
Ollama through the sidecar: from the network, from the gateway, and from Seriatim's own
maintenance scripts, which use the sidecar's admin routes (REQ-54). A process running
locally on the node can still connect to `127.0.0.1:11435` directly. Local users are trusted
by assumption, and the guarantee says so explicitly (ADR-001 §7, T1).

Four independent layers protect the bind:
1. **Bind.** Ollama runs with `OLLAMA_HOST=127.0.0.1:11435`. The sidecar refuses a
   non-loopback upstream (REQ-1).
2. **Firewall.** Inbound traffic is allowed to port 11434 (the sidecar) only. There is no rule
   for 11435.
3. **Deploy check (REQ-53).** It runs after install, after every model update, after every
   Ollama upgrade, and weekly.
4. **Runtime exposure check (REQ-19).** It fails closed: nothing is admitted until the check
   passes.

### 6.2 Key handling

- **Generation (S2).** `seriatim-node keygen --id <client> --scope infer|admin` prints a new
  256-bit random key once, with the config line (`id`, `scope`, `sha256`). The key itself is
  never written to disk.
- **On nodes.** Only SHA-256 hashes are stored. Plain SHA-256 is enough for 256-bit random
  keys, which are not passwords.
- **On the gateway.**
  - It must send each node's keys, so it holds them in plain text in a config file or the
    environment. The file must be readable only by the service account: an ACL on Windows,
    mode 0600 elsewhere.
  - Client keys are stored as hashes.
- **Rotation and revocation.** Several keys can be valid at once. To rotate one, add the new
  hash, move the client over, then remove the old hash and restart. A key's id appears in
  logs; the key itself never does.
- **Transport.** Plain HTTP on a trusted LAN by default, which is a residual risk (ADR-001 §8).
  TLS is optional (REQ-27).
  - **Active attackers are out of scope while TLS is off.** Someone who can read or change
    LAN traffic can steal keys. They can also forge a node's admission 429, so that the
    gateway runs a request twice.
  - **Where the LAN is not trusted,** TLS between the gateway and the nodes is required.
- **In the repository.**
  - gitleaks runs in the pre-commit hook and in CI (`secret-scan / gitleaks`).
  - A private list of terms that must never appear (host names and personal names) is kept as
    a CI secret and checked in CI (REQ-63). The check never runs code from a pull request,
    and never prints a matched term.

## 7. Deployment, compatibility and upgrade

### 7.1 Compatibility with existing clients (ruling Q7)

**Kept wire-compatible:**
- `Authorization: Bearer <key>`;
- on a 429, the `Retry-After` header and the body field `retry_after`;
- the envelope fields `error`, `error_code` and `retryable`, and the code `GPU_BUSY`;
- 401 and 403 as fatal, non-retryable errors;
- 503 with `error_code: GPU_OFFLINE` and `retryable: false` as fail-fast;
- other 502 and 503 responses as retryable with backoff.

**Optional response headers** (clients already treat their absence as a default):

| header | sent by Seriatim |
|---|---|
| `X-Resolved-Model` | the gateway: the model the request named |
| `X-Model-Digest` | the gateway: from its discovery cache, when known |
| `X-Execution-Device` | always `gpu` (there is no CPU lane) |
| `X-Rerouted-From`, `X-Reroute-Reason` | the gateway, after spill-over (REQ-36) |
| `X-Context-Clamped` | **never**, because nothing is clamped |

**Changed, and documented for clients:**
- **No `X-API-Key`.** Send the Bearer header only.
- **`device: "cpu"` no longer selects a CPU lane.** The field is passed to Ollama unchanged,
  which ignores it, and the request is gated like any other.
- **`auto:*` model aliases are gone.** Use `/models/select`.
- **Clamping is gone.** An over-limit `num_ctx` is rejected (422) instead.
- **`/models` and `/status` are replaced** by `/api/tags` (combined) and `/seriatim/nodes`.
- **`X-GPU-Status` is not sent.** No existing client reads it.

### 7.2 Scripts

Every script reads its configuration from a config file beside it, holds no host names in the
repository, is idempotent, logs with timestamps, and exits non-zero on any failure.

- **REQ-53. Local-only check** (`deploy/<os>/check-local-only`). It checks all of these:
  - port 11435 listens on loopback only;
  - port 11434 belongs to the sidecar process, and exactly one sidecar process is running;
  - Ollama's own auto-start (the tray app or login item) is disabled;
  - the Ollama service environment sets `OLLAMA_HOST` and `OLLAMA_NUM_PARALLEL=1`;
  - the firewall has no rule for 11435.

  It runs after install, after every model update, after every Ollama upgrade, and weekly.
  It is tested with Pester (Windows) and bats (macOS), with system commands stubbed.
- **REQ-54. Model update** (`deploy/<os>/update-models`).
  - It pulls models **through the sidecar's admin route** (`POST /api/pull` with the node's
    admin key, from the local config), never through 11435 directly.
  - **It reads the whole progress stream** for each pull. A pull counts as successful only if
    the HTTP status is 200, no record contains `error`, and the last record is
    `{"status":"success"}`. Ollama can report an error inside an HTTP 200 stream, so the
    status alone proves nothing.
  - It prints one line per model, and exits non-zero if any pull failed.
  - It then runs the local-only check.
- **REQ-55. Weekly maintenance.**
  - It runs the model update, the local-only check and log rotation, and writes a summary.
  - It does **not** update Seriatim (ruling Q10). Seriatim changes only through the `prod`
    branch and the deployer (§7.6).
- **REQ-56. Ollama upgrades.** These steps run in order, and a failure stops the sequence:
  1. In the PPE, capture fixtures and the route probe with the new Ollama version.
  2. Run the contract, allowlist and behaviour tests against them, in CI.
  3. Upgrade one node.
  4. Run the local-only check.
  5. Send a smoke request through the gateway.
  6. Upgrade the other nodes.

  Supported Ollama versions are listed in `README.md`; the first is 0.40.16.

### 7.3 Windows (GPU node)

- **REQ-57.** `deploy/windows/install.ps1` does the following:
  - installs Python 3.12+, the deployer (REQ-66) and the commit at the head of `prod` into a
    versioned venv under `%ProgramFiles%\seriatim`;
  - puts the config in `%ProgramData%\seriatim\`, with an ACL for the service account and
    Administrators;
  - registers two services with WinSW: `seriatim-ollama` (`ollama serve`, with `OLLAMA_HOST`,
    `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MODELS`) and `seriatim-node`, which depends on it;
  - disables the Ollama tray app's auto-start;
  - adds a firewall rule for inbound TCP 11434 on the Private profile;
  - runs the local-only check.

  The weekly task and the deployer timer run from Task Scheduler. Seriatim is upgraded only
  by the deployer, from `prod` (REQ-66).

### 7.4 macOS (Apple Silicon node)

- **REQ-58.** `deploy/macos/install.sh` does the following:
  - installs the deployer (REQ-66) and the head of `prod` into a versioned venv under
    `/usr/local/seriatim`;
  - puts the config in `/usr/local/etc/seriatim/` (mode 0600);
  - adds launchd plists `org.seriatim.ollama` and `org.seriatim.node`, with `KeepAlive`;
  - removes the Ollama.app login item;
  - adds an application-firewall rule;
  - runs the local-only check.

  Slice S6 tests whether Metal works under a LaunchDaemon, and records the result. The weekly
  job uses `StartCalendarInterval`.

### 7.5 Gateway host

- **REQ-59.** The gateway ships an OCI image and an example systemd unit. Its config lists
  each node's id, URL, keys, priority and `max_params_b`, plus `size_overrides`. Examples use
  placeholders only, such as `node-a.example`.

### 7.6 Release branches: `ppe` and `prod`

Seriatim is deployed from two long-lived branches besides `main`. A push to a branch updates
the hosts that track it.

- **REQ-64. Branches.**
  - **`main`** is the integration branch. Changes arrive only by pull request, with CI green
    and the maintainer's approval. Nobody pushes to it directly.
  - **`ppe`** is the test deployment. A push to `ppe` updates the designated PPE host to that
    commit. The host then runs the behaviour suite (§8.3) against its own PPE and records the
    result.
  - **`prod`** is production. A push to `prod` updates the GPU nodes and the gateway host.
  - **Nobody commits to `ppe` or `prod`.** Both only ever fast-forward to a commit on
    `main`. They may fall behind `main`, and `prod` normally sits at or behind `ppe`.
- **REQ-65. Guardrails, enforced by GitHub, not by convention.**
  - **Only from `main`.** `ppe` and `prod` move only by fast-forward to a commit that is
    already on `main` and whose CI on `main` passed.
  - **Who can move them.** Only the `promote` workflow does this. It is started by hand with
    a target (`ppe` or `prod`) and a `main` commit SHA. It checks:
    - `git merge-base --is-ancestor <sha> origin/main`;
    - the commit's required checks (REQ-66 defines them);
    - that the move is a fast-forward.

    Only then does it update the branch.
  - **The promotion identity.** The workflow pushes with a dedicated GitHub App,
    `seriatim-promote`, which has only `contents: write` on this repository. This is not the
    agents' App.
    - **Where its key is kept.** Its key is an *environment secret* of the `ppe` and `prod`
      environments. A job receives it only after that environment's protection rules pass.
    - **Branch policy.** Both environments accept deployments only from runs on `main`, so a
      workflow on another branch, or one changed in a pull request, cannot get the key.
    - **Rulesets.** `seriatim-promote` is the only bypass actor on the `ppe` and `prod`
      rulesets.
  - **PPE first.** A promotion to `prod` also requires the commit status `seriatim/ppe` to
    be `success` on that exact SHA.
    - **Who posts it.** The PPE host's deployer posts it after deploying that SHA and passing
      the behaviour suite.
    - **Its credential.** It posts with a second narrow App, `seriatim-ppe-reporter`, which
      has only `statuses: write` and whose key lives only on the PPE host.
  - **Approval.**
    - A promotion to `prod` runs in the GitHub environment `prod`, which requires the
      maintainer's approval.
    - A promotion to `ppe` needs no approval. An agent may start one when asked, through the
      project's GitHub App.
    - No agent pushes to `prod` directly, and no agent approves the `prod` environment.
    - The agents' App has no bypass on either ruleset, and cannot post `seriatim/ppe`.
  - **The rulesets on `ppe` and `prod`.** They allow no force-push and no deletion. Only the
    `promote` workflow's identity may push.
- **REQ-66. Deployer (`seriatim-deploy`, pull-based).** Each deployed host runs it on a timer
  (default every 60 s). It tracks one branch: `ppe` on the PPE host, `prod` everywhere else.
  It needs no GitHub credentials, because the repository is public. The one exception is the
  PPE host, which posts `seriatim/ppe` (REQ-65).
  - **One deploy at a time.** Each run takes a host-wide deploy lock first. If another run
    holds it, the new run exits without doing anything.
  - **Interrupted runs.** The deployer records the commit it is moving to before it starts.
    After an interruption, the next run finishes or rolls back that same commit. It never
    takes a new one in the middle.
  - **Fetch.** It fetches the tracked branch. If the head has not changed, it does nothing.
  - **Verify before installing.**
    - **Required checks.** The deployer config lists the required checks by name. Each one
      must have a completed check run on that exact SHA, from the GitHub Actions app, in a
      check suite for a `push` event whose `head_branch` is `main`, with the conclusion
      `success`. The deployer reads every page of
      results. Check runs it doesn't recognise are ignored, and can never stand in for a
      missing required one.
    - The commit must also be an ancestor of `origin/main`. The deployer checks this itself,
      as a second line of defence behind REQ-65.
    - **Definite failures.** A required check that concluded with anything other than
      `success`, or a commit that is not an ancestor of `main`, is refused, logged and never
      retried for that head.
    - **Uncertain results.** A pending, missing or unreadable check is treated as uncertain,
      not as a failure. So is a rate limit, an API error or a fetch error. The deployer
      retries with backoff (up to 30 minutes between tries) and reports the host as
      `deploy_pending`. Uncertain is never treated as a pass.
  - **Jitter.** It waits a random 0–30 s before each poll, so hosts do not poll in step.
  - **Install and switch.** It installs the commit into a new versioned venv, switches the
    service to it and restarts the service. A restarted sidecar starts with its slots
    draining (REQ-12a).
  - **Check.** It then runs the local-only check (REQ-53) on GPU nodes, plus the health check,
    within a time limit.
  - **Roll back on failure.** If any step fails, it switches back to the previous venv,
    restarts, and records the failure. It does not try that commit again until the branch
    moves.
  - **Rolling, health-gated.** Before it restarts, a prod node checks the public
    `/seriatim/health` of every other node in its `deploy_peers` list. It restarts only if all
    of them are healthy. Otherwise it waits and checks again on the next poll.
    - **Order.** Each node has a distinct `deploy_slot` index (0, 1, 2…). It waits
      `deploy_slot × deploy_spacing_s` (default 600 s) after first seeing a new head before
      checking its peers. Two nodes therefore check at moments far apart.
    - **Bounded wait.** After 30 minutes it deploys anyway, with a `WARNING`, so that a node
      that has been down for a long time cannot block every update.
    - **Best effort, not a guarantee.** If a deploy overruns its spacing, two nodes can still
      be restarting together. Clients then briefly get 503 `GPU_OFFLINE`. Meanwhile the
      gateway sends work to the nodes that are up (REQ-36, REQ-37).
  - **Log.** One timestamped line per attempt: the old commit, the new commit and the
    outcome.

## 8. Test plan

**Checks are executed, not attested.** CI on GitHub Actions is the authority for tests,
coverage, mutation, the PPE behaviour tests and secret scanning. Every one of them is a
required check on `main`. Local runs are pre-checks only. A PR quotes CI results or pasted
command output, and never just says "tests pass".

### 8.1 Unit and component tests

- **REQ-60. Coverage and platforms.**
  - pytest on `ubuntu-latest`, with two coverage gates for each package:
    - **≥ 80 %** combined coverage (`--cov-branch --cov-fail-under=80`);
    - **≥ 80 %** branch coverage on its own, checked separately from `coverage json`.
  - The unit and component suites also run on `windows-latest` and `macos-latest`.
- **Component tests** start the real sidecar (uvicorn on an ephemeral loopback port) in front
  of a scriptable fake Ollama that is also a real HTTP server. Process-level tests (restart and
  duplicate start) run the sidecar as a subprocess.
- **Real data over synthetic data.** The fake Ollama replays **captured real Ollama 0.40.16
  responses** (`tests/fixtures/ollama-0.40.16.json`, recorded in the PPE). Hand-written
  fixtures are used only for faults that cannot be captured: resets, stalls and malformed
  chunks.

### 8.2 Mutation testing

- **REQ-61. Mutation score.**
  - mutmut, pinned to one major version (`mutmut>=3.3,<4`), runs on the Python files a PR
    changes under `node/` and `gateway/`.
  - **Score** = (killed + per-mutant timeouts) ÷ (all mutants − skipped). Survivors, mutants
    with no covering test and suspicious mutants count against the score.
  - **Per-mutant timeouts count as killed** (ruled): a mutant that hangs has changed the
    program's behaviour, and the test run noticed.
  - **A whole-run timeout fails the job** (the job's `timeout-minutes`). It never counts as a
    pass.
  - **The score must be ≥ 75 %.** The parser reads mutmut's CI statistics export; a missing,
    malformed or unknown-format export fails the job.

### 8.3 HTTP behaviour tests (PPE)

- **REQ-62. A black-box suite.** `tests/behaviour/` is parametrised by base URL, so it can run
  unchanged against a future implementation in another language.
- **Where it runs.**
  - The PPE is Docker Compose with CPU-only micro-models. **The `ppe` CI job on
    `ubuntu-latest` is a required check** (ruling Q9).
  - Local runs are pre-checks on a designated CPU-only development host. A local run refuses to
    start unless the hostname matches the gitignored `ppe/.allowed-host`.
  - CI declares itself explicitly with `ppe/ppe up --ci`, which is accepted only when
    `GITHUB_ACTIONS=true`.
  - The PPE never runs on, or connects to, a production or GPU machine.
- **The guard** checks the *effective* Compose configuration (`docker compose config`) before
  it starts, and refuses any of these:
  - a sum of per-service limits above **4 CPUs or 6 GiB**;
  - a published port that is not on `127.0.0.1`;
  - `network_mode: host`, `privileged`, capabilities beyond `NET_RAW`/`NET_ADMIN` on the
    capture containers, a Docker socket mount, or a bind mount outside `ppe/`;
  - a Docker context or `DOCKER_HOST` that is not the local socket.
- **Models.** These are real micro-models with real, different `parameter_size` metadata:
  - `qwen2.5:0.5b` (0.5B) on every node;
  - `qwen2.5:1.5b` (1.5B) on both nodes of the two-node PPE.

  The gateway's thresholds are set between those sizes, so size routing is tested through
  Ollama's real `details.parameter_size`. The PPE does not use `ollama cp` aliases, which keep
  the source model's metadata. `size_overrides` is tested separately. The models are cached
  between CI runs.
- **Topology.**
  - Each node is an Ollama container with the sidecar in its network namespace. Ollama binds
    `127.0.0.1:11435` inside that namespace, as in production.
  - A packet-capture container records the raw bytes Ollama receives.
  - `/api/ps` `context_length`, read directly, shows the `num_ctx` that was loaded.
- **Suites.** P1–P7 cover the sidecar (from S1). P-GW1–P-GW7 cover the gateway (from S10).

### 8.4 CI checks (rules as checks, not prose)

- **REQ-63. CI on `ubuntu-latest` runs these checks:**
  - ruff (lint and format);
  - pytest with coverage;
  - mutmut;
  - the contract lint (`openapi-spec-validator`) and the allowlist-vs-route-list test;
  - **security scanners**, each a required check:
    - gitleaks (`secret-scan / gitleaks`, a pinned and checksum-verified binary);
    - Bandit (Python SAST), failing on medium or high severity;
    - pip-audit, on the locked dependencies (`uv export --frozen`), failing on any known
      vulnerability;
    - Trivy, scanning the repository's configuration and, from S10, the gateway image;
  - **the private-terms check.**
    - **How it runs.** It runs under `pull_request_target` with the workflow from `main`. It
      fetches the pull request's head as data only, and never checks it out for execution or
      runs any of its code. It scans `git diff` and the changed files' text against the term
      list held in a CI secret.
    - **What it reports.** Only `file:line: private term matched`, never the term itself.
    - **Fork pull requests** get the same check, because the workflow and the secret come
      from `main`.
    - **If the secret is unavailable,** the check fails. It never passes in that case.
    - **It also runs on every push to `main`,** scanning the whole tree, so that the deployer's
      required checks (REQ-66) include a `push` run of it on `main`;
  - an IP-literal check;
  - the PPE suite;
  - a PR-size check: ≤ 15 changed files (lock files excluded), and a diff of ≤ 150 KB, so
    that one reviewer can read the whole change.

  CI runs on every pull request and on every push to `main`, `ppe` and `prod`. The security
  scanners also run weekly on a schedule, to catch newly disclosed vulnerabilities in pinned
  dependencies. Actions are pinned by commit SHA, and dependencies are locked (`uv.lock`).

### 8.5 Review

Each slice is reviewed independently before it merges, by a reviewer who is not its author.
The maintainer approves and merges.

## 9. Rulings (2026-11-16)

The maintainer ruled on the open questions of revision 1 as follows.

| # | question | ruling | where applied |
|---|---|---|---|
| Q1 | Spill over to a mirror? | Yes, once per eligible machine, and only on Seriatim's own admission 429, never on an upstream 429 | REQ-36, REQ-37 |
| Q2 | Default for an over-limit `num_ctx`? | Reject | REQ-8 |
| Q3 | `num_ctx` on `/v1`? | Reject, with a hint | REQ-9 |
| Q4 | A CPU lane that bypasses the lock? | Dropped in v1 | Non-goals, §7.1 |
| Q5 | An admin scope? | A separate admin key for model management; unknown endpoints need it too | REQ-5, REQ-14 |
| Q6 | TLS by default? | Optional in v1 | REQ-27 (slice S11) |
| Q7 | Compatibility? | Keep the header names and fields existing clients use; document everything else | REQ-43, §7.1 |
| Q8 | S0 without a TD? | Yes | ADR-001 §10 |
| Q9 | PPE in CI? | Yes, as a required check. The host guard applies to local runs only; CI declares itself | §8.3 |
| Q10 | Should maintenance update Seriatim? | No. Seriatim is updated only by the deployer, from `prod` (added 2026-11-16: the `ppe` and `prod` branch pattern) | REQ-55, §7.6 |
| Q11 | Publish the evaluation? | Yes, cleaned, as `docs/EVALUATION.md` | `docs/EVALUATION.md` |

Two points were raised in review and decided without change. Ollama **0.40.16** is the real
version the evaluation ran. Per-mutant timeouts count as killed, and the whole-run timeout
fails closed (REQ-61).

## 10. Open questions

None remain from revision 1. New questions found during implementation are raised in that
slice's TD, with a recommendation, the options and the risks.
