# Seriatim — Specification

**Status:** revision 6, for review (Phase 1). **Date:** 2026-11-16.

**Companion documents:**
- [ADR-001](adr/ADR-001-architecture.md): architecture, contract, threat model, slice plan.
- [TD-slice-1](design/TD-slice-1.md): the first code slice only.
- [EVALUATION](EVALUATION.md): the comparison that led here.

Seriatim is a one-at-a-time GPU gate for [Ollama](https://ollama.com). It has two parts:
- **A sidecar on each GPU machine.** It sits in front of Ollama and admits a limited number of
  inference requests at a time, one by default. A request that arrives while the GPU is busy
  gets an immediate HTTP 429 with `Retry-After`; it is never queued.
- **A thin gateway.** It gives clients one address, routes each request to a machine the
  operator has made eligible for that model, and combines the read-only Ollama views across machines.

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
| F2 | No routing by size: a 32B request went to a machine too small for it | Routing by size (REQ-32, REQ-33) |
| F3 | `num_ctx` above an internal table was silently lowered (30000 → 24576), and on the OpenAI route it had no effect | `num_ctx` is never changed; an over-limit value is rejected (REQ-7 to REQ-9) |
| F4 | `/api/tags`, `/api/ps` and `/api/version` showed only one machine | Merged across machines in Ollama's own shape, with per-node detail on `/seriatim/models` and `/seriatim/nodes` (REQ-38) |

| evaluation requirement | requirements here |
|---|---|
| E1a native options unchanged, including `num_ctx` | REQ-6, REQ-7 |
| E1b `num_ctx` on the OpenAI route | REQ-9 |
| E2 busy → immediate 429 with `Retry-After` | REQ-2 to REQ-4, REQ-36 |
| E3 routing by size, including a machine down | REQ-32 to REQ-35, REQ-37 |
| E4 Bearer-key auth | REQ-13 to REQ-15, REQ-31 |
| E5 GPU-memory awareness | REQ-16, REQ-42 (and ADR-001 D14) |
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
4. **G4. One address.** Clients use one gateway. It sends each request to a machine the
   operator has made eligible for the model, and merges the model lists of every machine.
5. **G4a. Any fleet, defined by configuration.** The set of machines is whatever the
   gateway's configuration lists. Its size and makeup are not fixed: any number of machines,
   each with any amount of memory, on any platform that runs Python and Ollama.
   - **Changing it is a configuration edit.** Growing, shrinking or resizing the fleet takes
     effect without a code change or a gateway restart (REQ-44 to REQ-46).
   - **No assumptions in the code.** Nothing assumes a machine count, a memory size, an
     operating system or a GPU vendor. Install scripts exist first for Windows and macOS,
     but that is a packaging order, not a supported topology.
6. **G5. Visible failure.** Every failure is an explicit, machine-readable error. A timeout,
   an unavailable dependency or an answer that cannot be parsed is never treated as success,
   and is never quietly downgraded.
7. **G6. Safe to publish.**
   - Ollama is reachable only through the sidecar.
   - GPU machines never store keys in plain text.
   - The repository holds no host names, addresses or keys.
8. **G7. Replaceable.** The HTTP contract (ADR-001 Appendix A) is the interface. A future
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
- **Automatic admission from measured capacity.** Phase 1 measures and reports each node's
  capacity (REQ-16), but routes only by the operator's rules (REQ-32). Deriving fit from
  model size, context and measured memory is the later "Measured fit" slice (ADR-001 D14),
  calibrated against recordings from real nodes.
- **Install scripts for every platform in v1.** The sidecar runs on any platform with Python
  3.12 and Ollama (REQ-46). The v1 install scripts cover Windows and macOS. That is a
  packaging order, not a supported topology. Elsewhere the sidecar is installed as an ordinary service by hand.
  Scripts for other platforms are welcome as contributions.
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
    - **Their scopes** are fixed by the contract (`x-seriatim-scope` on each path):
      `/seriatim/health` needs no key; `/seriatim/status`, `/seriatim/nodes`,
      `/seriatim/models` and `/models/select` need `infer`; every `/seriatim/admin/*` route
      needs `admin`.
    - **Redaction.** With an `infer` key, the operator's offline `reason`, `changed_by`, and
      every slot's holder details (`path`, `model` and `request_id`) are returned as
      `null`. A key can't be matched to a slot across the gateway, so no slot is shown in
      full to an `infer` key. On `/seriatim/nodes` the gateway redacts its cached copies,
      fetched with the node's admin key: each `admin_state`, and the embedded status's
      `admin_state` and slots. An `admin` key sees everything. The fields stay present, so the
      schema is the same for both.
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
  REQ-9 on the sidecar, and on the gateway to read `model` for routing (REQ-32).
  Reading never changes the bytes forwarded.

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
  - **Replacing it.** A later slice may replace it with a positive signal, but only if one is shown to be
    reliable.
  - **Unproven closure.** If closing the upstream connection fails or times out, closure is
    unproven. The slot then stays *stuck* until the sidecar restarts, health reports it, and
    it is never freed by a timer (REQ-11).
  - **Rejected before Ollama.** A request rejected after taking its slot but before
    anything was sent to Ollama frees the slot at once, with **no drain**, because nothing
    ran: 400 (`BAD_REQUEST_BODY`, `NUM_CTX_AMBIGUOUS`, `NUM_CTX_UNSUPPORTED_ROUTE`), 408
    `REQUEST_BODY_TIMEOUT`, 413 `BODY_TOO_LARGE` (a body that grows past the limit), 422
    `NUM_CTX_EXCEEDS_LIMIT`, and a client that disconnects while its body is being read.
    (`BAD_PATH`, and a 413 on `Content-Length`, are answered before any slot is taken.) A failed connect to Ollama (502 `UPSTREAM_UNAVAILABLE`) sent
    nothing either, and also frees the slot with no drain.
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
    `%ProgramData%\seriatim\run` on Windows, `/Library/Application Support/seriatim/run`
    on macOS, `/run/seriatim` on Linux. On macOS that is outside Homebrew's tree, whose
    directories may belong to an unprivileged user, or may not exist (Apple Silicon uses
    `/opt/homebrew`). Every account therefore meets the same lock.
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

### 4.3 Sidecar: status, health, capacity, admin switch and exposure (slices S4a to S4c, as noted)

- **REQ-16. Capacity, measured and reported (slice S4b).** `GET /seriatim/status` reports a
  `capacity` object (contract schema `Capacity`). Every figure names its source, and none is
  ever estimated or invented.
  - **What it is for.** In Phase 1 the report is **information**. The gateway shows it in
    `/seriatim/nodes` next to each node's configured eligibility (REQ-42), so the operator can
    set those numbers from real data. Routing does not use it (REQ-32). Automatic admission
    from measured capacity is a later slice, "Measured fit" (ADR-001 D14).
  - **NVIDIA** (`source: nvidia-smi`). One sample is these steps, in this order:
    1. **Inventory A:** `nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free
       --format=csv,noheader,nounits`.
    2. **Processes:** `nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory
       --format=csv,noheader,nounits`.
    3. **Ollama's view:** `GET /api/ps` on the local Ollama.
    4. **Inventory B:** the same query as step 1.

    Figures are MiB, converted to bytes as × 1 048 576.
    - **The inventory is authoritative.** The process rows' `gpu_uuid` values must be a
      **subset** of the inventory's UUIDs. A device with no process rows has
      `ollama_used = 0`; an idle GPU is normal, not an error.
    - **Three outcomes of the process query are kept apart:**
      - **Succeeded and empty** (exit 0, no rows): no compute processes.
      - **Failed** (non-zero exit or timeout): attribution is unavailable, and the WDDM rule
        below applies.
      - **Malformed** (a row with the wrong field count, or a UUID not in the inventory): the
        sample is discarded, and the probe reports `unavailable` with an error.
    - **Whose memory it is.** Memory is credited to Ollama only through the **process tree
      of the configured Ollama service**. The sidecar finds the service's main PID from the
      service manager, by `SERIATIM_NODE_OLLAMA_SERVICE`. Its default is the service the
      install script creates: `seriatim-ollama` (Windows Service Control Manager),
      `org.seriatim.ollama` (launchd) or `ollama.service` (systemd).
      - **The tree** is that PID, plus `psutil.Process(pid).children(recursive=True)`. On
        systemd it also includes every PID in the unit's cgroup (`cgroup.procs`), so a
        runner that was re-parented is still found. A process that exits while the tree
        is read (`NoSuchProcess`) is skipped, and `AccessDenied` makes attribution
        unavailable.
      - **The rule.** A process row counts as Ollama's only if its PID is in that tree.
      - **The cross-check.** On a node with one visible device, if `/api/ps` reports
        `size_vram > 0` but no row in the tree has memory, attribution is treated as
        unavailable (below), and the sample counts as a transition (retried). Matching on executable names is
      not enough, because another Ollama could run on the same machine.
      - **Names are compared by basename, case-insensitively.** Windows reports
        `process_name` as a full path in varying case. Names are only a cross-check: a PID in
        the tree whose basename is not one of Ollama's executables is logged.
      - **If the service PID can't be found,** attribution is unavailable.
    - **Usable memory, per device.**
      `usable = min(total, free + ollama_used)`, with `basis: free_plus_ollama`. Each device
      reports `ollama_used_bytes`: the attributed figure, the credited `/api/ps` figure under
      `free_plus_ps`, or `null` under `free_only`. Ollama's own
      models can be evicted, so they don't reduce capacity; memory used by any other process
      does.
    - **When per-process attribution is unavailable for a device**, it falls back as below.
      That happens when any row of Ollama's tree on that device has `used_memory` `[N/A]`
      (as under the Windows WDDM driver), when the process query failed, or when the
      service's process tree could not be read, including descendants it was denied
      access to. The process figures and the `/api/ps` credit are never added together
      for the same device. The fallback:
      - **One visible device** (after `CUDA_VISIBLE_DEVICES` filtering, so a multi-GPU host
        restricted to one device counts as one):
        `ollama_used = min(Σ size_vram from /api/ps, total − free)`, with
        `basis: free_plus_ps`. So the memory Ollama already holds is still counted as its
        own, and a second request for a loaded model does not see a smaller GPU.
      - **More than one visible device:** `/api/ps` doesn't say which device holds what, so
        each device reports `usable = free`, with `basis: free_only`, and a warning.
      - **If `/api/ps` also fails,** `basis: free_only`.
    - **The sample is not atomic.** The figures are reported from inventory B. Inventory A
      is a consistency check:
      - **What must agree.** A and B must list the same UUIDs with the same totals.
        `memory.used` and `memory.free` on every device must each differ by at most 1 % of
        that device's total.
      - **If the device lists or totals differ** on every attempt, there is no pair to
        compare. The report is `source: unavailable`, with the error `inventory changed`.
      - **If only the figures differ,** the probe retries, up to 3 times. If none agree,
        every device reports, from the last attempt, `free_bytes = min(free_A, free_B)`,
        `used_bytes = max(used_A, used_B)`, `usable = free_bytes` and `basis: free_only`,
        and the report has `transition: true`.
      - **Also checked:** `ollama_used ≤ min(used_A, used_B)` on every device. A sample that
        breaks it counts as a transition.
      - **What the check can't catch.** A model that loads and unloads between A and B, or
        memory that moves between Ollama and another process at a constant total. The
        report is therefore a measurement with an uncertainty of up to that 1 % plus one
        model's change, not an exact figure. That is acceptable in Phase 1, where it is
        information for the operator (D14).
    - **Which devices.** If `SERIATIM_NODE_CUDA_VISIBLE_DEVICES` is set, only the devices it
      names are reported. It must equal the Ollama service's `CUDA_VISIBLE_DEVICES`, and the
      local-only check (REQ-53) verifies that it does.
      - **Full UUIDs** (`GPU-` followed by the whole UUID) are matched against the
        inventory's `uuid`. CUDA also accepts unique prefixes, but Seriatim deliberately
        rejects them as a configuration error, so that both sides name exactly one device.
      - **Indices** are accepted only if the Ollama service also sets
        `CUDA_DEVICE_ORDER=PCI_BUS_ID`, verified by REQ-53. The probe never assumes that a
        CUDA index equals `nvidia-smi`'s `index`. It queries `pci.bus_id` too, sorts the
        devices by bus id, and maps CUDA index *n* to the *n*-th device's UUID.
      - **An empty value, or `-1`,** hides every GPU, as it does for CUDA. The probe then
        reports `source: none`, with a warning.
      - **Other forms**, such as negative numbers or text, are configuration errors.
        So is an entry that matches nothing.
      - **MIG entries** (`MIG-…`) are not supported in v1. The probe reports `unavailable`
        with the error `MIG devices are not supported`.
    - **More than one device.** Every device's figures are listed. The node's `usable_bytes`
      is the **largest single device's** `usable`, because Ollama's split of a model across
      GPUs is not exposed. The node's `basis` is that device's basis; a tie goes to the
      lowest index.
  - **Apple Silicon** (`source: apple-metal`):
    - **Unified memory is required.** The probe reports `apple-metal` only on a machine
      with unified memory: Metal's `hasUnifiedMemory` is true or, when Metal can't be read,
      `sysctl hw.optional.arm64` is 1. Any other Mac is `source: none`.
      - **A missing OID** (`unknown oid`) for `hw.optional.arm64` means "not Apple
        Silicon", so `source: none`, not a probe failure.
      - **"Metal can't be read"** covers: the binding is not installed, the import fails,
        or `MTLCreateSystemDefaultDevice()` returns `None`, as it can in a daemon or
        headless context. PyObjC exposes `hasUnifiedMemory()` and
        `recommendedMaxWorkingSetSize()` as methods.
      - **A failed read of `iogpu.wired_limit_mb`**, including an unknown OID, gives
        `wired_limit_mb: null`, and the next budget source is used. It is not a probe
        failure.
    - **The total** unified memory comes from `sysctl hw.memsize`, as `total_bytes`. If it
      can't be read, the probe reports `unavailable`.
    - **The GPU budget** (`usable_bytes`), the first that applies:
      1. **A raised wired limit.** `sysctl iogpu.wired_limit_mb` above 0 means the operator
         raised the GPU limit on purpose. Then `usable_bytes = min(wired_limit_mb × 1 048 576,
         hw.memsize)`, with `basis: wired_limit`. This applies even when Metal can't be read,
         as can happen under a LaunchDaemon.
      2. **Metal's recommendation.** `MTLCreateSystemDefaultDevice().recommendedMaxWorkingSetSize`,
         read through the optional `pyobjc-framework-Metal` dependency, which the macOS install
         script adds (`basis: recommended_working_set`). It is Apple's *recommended* budget
         for keeping performance, not a measurement of free memory, and not a hard cap.
      3. **Neither** (no raised limit, and Metal unavailable): the probe reports `unavailable`
         with an error. There is no fallback to a fraction of RAM.

      `wired_limit_mb` itself is reported: 0 when unset, `null` when unreadable. S6 records
      both values on real hardware, before and after raising the limit.
    - **Memory pressure.** `sysctl kern.memorystatus_vm_pressure_level` is mapped
      explicitly: 1 → `normal`, 2 → `warn`, 4 → `critical`.
      - **Any other value, or a failed read,** gives `memory_pressure: null` and a
        `pressure_error`. The rest of the report still stands.
      - **Not used for routing in Phase 1.** It is reported for the operator.
    - **The limitation.** Memory that other programs use on a Mac is not subtracted, because
      it cannot be attributed to the GPU.
  - **No GPU found** (`source: none`): no probe applies, for example a CPU-only machine,
    AMD or Intel graphics, or a Mac without unified memory. `usable_bytes: null`, and no
    devices.
  - **A probe that fails, times out (2 s) or returns malformed output** gives
    `source: unavailable`, `usable_bytes: null`, no devices and an `error`. The contract
    has one shape for every failure, whatever the platform.
  - **Context and cache settings** are reported for the operator and for the later "Measured
    fit" slice: `default_num_ctx`, `kv_cache_type` and `flash_attention`. They come from
    `SERIATIM_NODE_OLLAMA_CONTEXT_LENGTH`, `SERIATIM_NODE_OLLAMA_KV_CACHE_TYPE` and
    `SERIATIM_NODE_OLLAMA_FLASH_ATTENTION`, which must equal the Ollama service's own
    settings. The local-only check (REQ-53) verifies that they do. Each is `null` when unset.
  - **Freshness.** The sidecar measures again on each status request, caching for at most
    5 s. Every report carries `measured_at`.
  - **Pluggable probes.** Each probe is a small platform adapter behind one interface, so
    support for another platform can be added without touching the gate or the router.
  - **Fixtures, not hardware.** In tests, and only in test mode, probes can replay recorded
    output (`SERIATIM_NODE_PROBE_FIXTURE`, accepted only with `ALLOW_UNAUTHENTICATED`). No
    test needs a real GPU.

  *Tests:* M1–M16, with the recorded probe fixtures (§8.1).
- **REQ-17. Loaded models and slots (slice S4a).** Status includes:
  - Ollama's `/api/ps` list, unchanged. If `/api/ps` fails or cannot be parsed, the field is
    `loaded_models: null` with `loaded_models_error`, never an empty list.
  - Each slot's `state` (`free`, `held`, `draining` or `stuck`), and for each holder its
    `path`, its `model` (when known), its `request_id`, `started_at`, `elapsed_s` and
    `drain_remaining_s` (contract `Slot`). `path`, `model` and `request_id` are `null` for
    `infer` keys (REQ-5).

  *Tests:* G6 (S1); ST1–ST3 (S4a: the `/api/ps` error state, slot details, and redaction for `infer` keys).
- **REQ-18. Health (slice S1, extended in S4a and S4c).** `GET /seriatim/health` requires no
  authentication.
  - It returns 200 `{"status":"ok"}` only when all of these hold:
    - Ollama answers `GET /api/version` within 2 s with HTTP 200 and a JSON object whose
      `version` is a non-empty string;
    - no slot has been held longer than its maximum hold plus 30 s, and no slot is stuck
      because closing its upstream connection timed out (TD-slice-1 §3.3);
    - the node is not starting;
    - from S4a, the exposure check has passed;
    - from S4c, the node is not offline by the admin switch (REQ-19a).
  - Otherwise it returns 503. The body has `status: offline` whenever the switch is on, and
    `status: degraded` otherwise. `reasons` lists every cause, including `offline_admin`,
    so an offline node with another fault shows both.
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
- **REQ-19a. Admin offline/online switch (slice S4c).** The operator can take a node out of
  service on purpose, and the sidecar owns that state.
  - **Routes**, both in the `admin` scope:
    - `POST /seriatim/admin/offline`, with an optional JSON body `{"reason": "…"}`. The
      reason is at most 200 printable characters; anything else gets 400
      `BAD_REQUEST_BODY`.
    - `POST /seriatim/admin/online`, with no body. A non-empty body gets 400
      `BAD_REQUEST_BODY`.
    - **The reason.** No body, `{}` and `{"reason": null}` all mean no reason. A reason is
      a string of 1 to 200 characters, each of which is in a Unicode general category L,
      M, N, P or S, or is the space U+0020. That rule is language-independent, and it is
      what Python's `str.isprintable()` checks. So no control or format characters, line
      breaks, or other spaces such as U+00A0. Contract cases include each of those, and
      an accepted letter, accent and symbol.

    Both return 200 with the resulting state (contract `AdminState`).
    - **Going offline** sets `offline: true`, the reason, `since` (now) and `changed_by`.
    - **Going online** sets `offline: false` and `reason: null`, with `since` and
      `changed_by` recording that change.
    - **After `init-state`** (below), every field but `offline: false`, `epoch` and
      `revision: 0` is `null`.
    - **Idempotent.** A call that would change no field is a **no-op**: nothing is
      written, `revision` doesn't change, and the 200 carries the current state. That holds
      only while the state is persisted: after a fail-closed start (below), whose state is
      in memory only, every `offline` or `online` call writes a fresh file. So a
      repeated `online` on an online node does nothing. A repeated `offline` with a
      different reason or key replaces `reason` and `changed_by`, keeps `since`, and
      increments `revision`.
  - **While offline:**
    - **A new gated request** gets 503 `NODE_OFFLINE` (`retryable: false`,
      `replay_safe: true`). Ollama is not contacted.
    - **A request already admitted** runs to completion. Going offline never aborts work in
      progress, and its slot ends and drains as usual (REQ-10).
    - **Gated admin requests** are refused too, with the same `NODE_OFFLINE`: `POST
      /api/create` and every unknown endpoint (REQ-5), because they wait for the GPU.
    - **Ungated requests are still served,** so the operator can inspect and maintain the
      node: the `infer` ones (`/api/tags`, `/api/ps`, `/api/version`, `/api/show`,
      `/v1/models`) and the ungated `admin` ones (pull, push, copy, delete, blobs), as well
      as `/seriatim/*`.
  - **One commit point.** Changes are serialised by an admin lock of their own, never by
    the gate's lock:
    - **Persist first.** The new state is written and made durable (below) in a worker
      thread (`anyio.to_thread`), holding only the admin lock. Neither the event loop nor
      the gate's lock is held during that I/O, so admission never waits for a disk.
    - **Then publish.** The new state replaces the in-memory one in one synchronous step on
      the event loop. Admission is itself a synchronous check on the event loop (TD-slice-1
      §3.2), so the two never interleave. That step is the **commit point**, and the 200 is
      sent after it.
    - **Not cancellable.** From the start of persistence to publication, the work is
      shielded from the request's cancellation and keeps the admin lock. A client that
      disconnects doesn't leave the file and memory apart.
    - **A crash between persisting and publishing** leaves the new state on disk, so the
      next process starts with it. The call's outcome is then unknown to its caller, and
      re-sending it is safe.
    - **What it guarantees.** After the commit point, there are **no new admissions**: a
      gated request that asks for a slot gets `NODE_OFFLINE`. A request admitted before it
      keeps running, and may still reach Ollama afterwards, because a body is buffered after
      admission (REQ-20). The switch never claims otherwise.
    - **Concurrent calls** are serialised by the admin lock. Once each call has finished,
      memory and disk hold the state of the last call to commit.
  - **Epoch and revision.** The state carries two persisted values, so an observer can
    always order what it sees:
    - **`epoch`:** 32 random hex characters. A new epoch is started by `init-state`, and by
      every recovery from a lost or unreadable state (below). It is never reused.
    - **`revision`:** an integer that starts at 0 in each epoch and increments on every
      committed change within it.

    A recovered node therefore never depends on an old revision number that it can no
    longer read. REQ-42a defines how the gateway orders observations.
  - **Precedence of refusals** for a new gated request: `NODE_OFFLINE`, then the exposure
    codes (`OLLAMA_EXPOSED`, `EXPOSURE_UNVERIFIED`), then `NODE_STARTING`, then busy (a
    maintainer decision).
    - **Before the body.** All of them are answered before the request body is read, as
      busy is, so a refused request costs no upload.
    - **Marked.** Each carries `X-Seriatim-Gate: refused` (busy keeps `busy`), so the
      gateway knows that Seriatim refused it and Ollama was not contacted (REQ-37). So an
    offline node that restarts says it is offline, which is not retryable, rather than
    starting, which is.
    - **Independent of slots.** The admin state is separate from slot state. `online` never
      shortens the startup quarantine, a drain or a held slot. `offline` never frees or
      aborts one.
  - **Visible.** `/seriatim/status` reports `admin_state` (contract `AdminState`:
    `offline`, `reason`, `since`, `changed_by`, the key id that changed it, `epoch` and
    `revision`), and health reports `offline` (REQ-18).
    Health carries no reason text: it is unauthenticated, and the reason is operator
    text (T12). Every change is logged
    at `WARNING`, with the key id, the reason and the previous state.
  - **Gateway or not.** The switch also applies to clients that call the sidecar directly,
    because the sidecar holds the lock.
  - **It survives a restart.** The state is kept in `node-state.json` in a **persistent
    state directory**, separate from the runtime lock directory (REQ-12b), which may be a
    tmpfs:
    - `%ProgramData%\seriatim\state` on Windows, `/Library/Application Support/seriatim/state`
      on macOS, and `/var/lib/seriatim` on Linux;
    - **owned by the sidecar's service account,** with every parent directory owned by an
      administrator (`root:wheel` on macOS, `root` on Linux), so no other account can
      rename or replace it;
    - **fixed in production,** like the lock directory. `SERIATIM_NODE_STATE_DIR` can
      change it only in PPE and test mode;
    - on POSIX, mode 0700 for the directory and 0600 for the file. On Windows the
      directory's ACL (REQ-57) protects it, because a file mode means nothing there.
    - **`seriatim-node init-state`** is a first-install step. It creates the directory and an
      online state with a new epoch.
      - **Already initialised:** a valid file is left unchanged, and it exits 0 saying so.
      - **Unreadable file:** it is never overwritten; `init-state` exits non-zero.
      - **Who runs it:** the install scripts (REQ-57, REQ-58), and `ppe/ppe up` when it
        creates a node's state volume. An ordinary service start never runs it, so lost
        state is never re-initialised automatically.
    - **Writes are atomic and durable.** A temporary file with a unique name
      (`node-state.json.<pid>.<random>.tmp`) in the same directory is written and fsynced, then renamed over the old one, then made durable:
      - **POSIX:** `os.replace`, then an fsync of the directory.
      - **Windows:** the temporary file is closed after its fsync, then moved with
        `MoveFileExW` and `MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH`, which returns
        only when the move is on disk. A sharing or access-denied error (a scanner holding
        the file) is retried up to 5 times, 100 ms apart. If it still fails, the rename and
        the flush were one call, so the sidecar decides which side of the rename it is on by
        checking whether the temporary file still exists.
    - **A failure before the rename** gets 500 `INTERNAL`. Neither the file nor memory
      changes, and the temporary file is removed.
    - **A failure after the rename** (the directory fsync) gets 500 `INTERNAL` too. The
      file may already hold the new state, so memory takes the new state as well, and an
      `ERROR` says the change may not survive a power loss. Re-sending the same call is
      safe, because it is idempotent.
    - **At startup** the sidecar reads the file before it admits anything.
    - **Lost or unreadable state fails closed.** A missing directory, a missing file, or a
      file that cannot be read or parsed means **offline**:
      - the reason is `state file missing` or `state file unreadable`;
      - a **new epoch** begins, with `revision: 0`, held in memory, with `since` the
        startup time and `changed_by: null`;
      - an `ERROR` is logged.

      A missing file is never taken as a first start: only `init-state` makes one.
    - **Recovery.** An explicit `online` call writes a fresh file in the new epoch
      (`revision: 1`), and the node returns to service. The gateway accepts it, because the
      epoch is new (REQ-42a), whatever revision it held before. An explicit `offline` call
      also writes a fresh file (`revision: 1`), keeping the node offline with the
      operator's reason.
    - **A missing state directory** is recreated by every write, `offline` and `online`
      alike, with the ownership and permissions or ACL above, before the temporary file is
      opened.
    - **A reboot during maintenance** therefore never brings a GPU back into service, even
      where the lock directory is on a tmpfs.

  *Tests:* AS1–AS20:
  - offline refuses new gated work with `NODE_OFFLINE`, while a request already admitted
    finishes;
  - **barrier tests:** an `offline` that commits while a request is between "read state"
    and "take slot" (that request gets `NODE_OFFLINE`, or was admitted before the commit
    and finishes); concurrent `offline` and `online` end with memory, file and `revision`
    agreeing;
  - **precedence:** an offline node gets `NODE_OFFLINE`, never `GPU_BUSY` or
    `NODE_STARTING`, including during the startup quarantine; `online` during the
    quarantine or a drain does not shorten either;
  - ungated and admin requests still work;
  - the state persists across a restart, and across a kill during the write; a first
    write and a replacement are tested separately, with failures injected before and after
    the rename;
  - an unreadable file, a missing file and a missing state directory each start the node
    offline, in a new epoch; on Linux, the state survives with the lock directory on a
    tmpfs that is emptied between runs;
  - `init-state` leaves a valid file unchanged (exit 0) and never overwrites an unreadable
    one (non-zero);
  - **cancellation and crashes:** a client disconnect during an `offline` call, and a
    process kill between persisting and publishing, each leave memory and disk agreeing
    after restart;
  - **sidecar-side recovery:** after corruption, `online` recreates the directory if needed
    and reports `revision: 1` in a new epoch, and so does `offline`, staying offline (the
    gateway side is GA, in S8);
  - a repeated `online` on an online node writes nothing and keeps `revision`;
  - gated admin requests (`/api/create`, an unknown endpoint) get `NODE_OFFLINE`, while
    ungated admin requests are served;
  - state I/O never runs under the gate's lock: with an injected slow fsync during an
    `offline` call, ungated requests and the admission check keep answering;
  - a non-admin key gets 403; a reason that is too long, empty or has a control character
    gets 400.

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
  - `GET /seriatim/health`, `GET /seriatim/nodes` and `GET /seriatim/models`;
  - `POST /seriatim/admin/nodes/{id}/offline` and `.../online` (REQ-42a);
  - `GET /` and `HEAD /`, which answer 200 `Ollama is running` when at least one node is
    healthy, and 503 `FLEET_UNAVAILABLE` otherwise (REQ-35).

  Its classification uses the same table as REQ-5.
- **REQ-31. Gateway auth.**
  - Clients are authenticated as REQ-13 to REQ-15 require.
  - The gateway authenticates to each node with that node's own keys (one `infer` key and one
    `admin` key), kept in its configuration.
  - A client's key is never forwarded.
- **REQ-32. Routing by the operator's eligibility rules.** For each request that names a
  model, the gateway decides which nodes may run it. In Phase 1 that decision comes **only
  from the operator's configuration**. Measured capacity (REQ-16) is shown next to it, but
  does not decide anything (ADR-001 D14).
  - **`POST /api/show`** names a model but runs nothing, so the rules don't apply to it.
    It goes to the highest-priority healthy node that lists the model, and otherwise gets
    the first two errors below (`DISCOVERY_INCOMPLETE`, `MODEL_NOT_FOUND`) or
    `GPU_OFFLINE`. An operator can therefore inspect a model before writing a rule for it.
    - **Named in `X-Seriatim-Node`,** it goes to that node even when the node is `offline`,
      because an offline sidecar still serves ungated requests (REQ-19a), so a model can be
      inspected during maintenance. It is not forwarded to an `unhealthy` node; that gets
      `GPU_OFFLINE`.
  - **Reading the request.** The gateway reads only `model` from the body. It gets 400
    `BAD_REQUEST_BODY` from the gateway, before any node is contacted, if the body is not
    valid JSON, its top level is not an object, the top level has duplicate keys, or
    `model` is missing, not a string or empty. (`NUM_CTX_AMBIGUOUS` stays a sidecar code.) The gateway does not read `num_ctx`; the sidecar applies REQ-8 and REQ-9.
  - **The rules** (REQ-44). For a node and a model, the first rule that applies decides:
    1. **The model's eligible-nodes list.** If the configuration has
       `[models."<name>"] eligible_nodes = [...]` for this model, the node is eligible
       exactly when it is in the list. Names match exactly, after Ollama's usual `:latest`
       default is applied.
    2. **`capacity = "unlimited"` on the node.** Eligible, with no size check. This is for
       CPU-only or unusual machines, by the operator's explicit choice.
    2a. **`capacity = "listed_only"` on the node.** Not eligible (reason `listed_only`).
       Such a node runs only the models whose eligible-nodes lists name it (rule 1), which
       is how a node is kept for a few chosen models.
    3. **`max_params_b` on the node.** Eligible when the model's parameter count is at most
       `max_params_b`.
    4. **None of these.** The node has **no size policy** for this model. It is **excluded**,
       and Seriatim never guesses.

    **Configuration errors**, rejected at startup and on reload (REQ-45):
    - setting `max_params_b` together with any `capacity` value on one node;
    - an eligible-nodes list that names an unknown node;
    - two `[models."…"]` tables whose names normalise to the same model, such as
      `[models."m"]` and `[models."m:latest"]`.
  - **Parameter counts are per node.** Each node is judged against the model as that node's
    own `/api/tags` describes it, because two nodes can hold different models under one name.
    - **The node's own `details.parameter_size` first,** parsed as
      `^([0-9]+(\.[0-9]+)?)([MB])$` (M = 10⁶, B = 10⁹).
    - **`size_overrides` only where that fails.** A table from a model to a positive,
      finite number of billions of parameters. It is consulted only for a node whose own
      `parameter_size` can't be parsed, such as `8x7B`, so an override never changes a
      node whose size is already known. Its keys are:
      - `"<name>@<digest>"`, such as `"mixtral:8x7b@sha256:…"`, for exactly that model;
      - or `"<name>"`, for any digest under that name.

      A digest key wins over a name key. Names are normalised with Ollama's `:latest`
      default.
    - **Override errors.** Two keys that normalise to the same name and digest, or a value
      that is not positive and finite, are configuration errors.
    - **Otherwise** the count is unknown, and the verdict is `size_unknown`.
  - **A node named in the request.** A request that names its node in `X-Seriatim-Node` and
    names a model is checked against **that node only**.
    - **An unknown node id** gets 404 `NOT_FOUND`.
    - **One node in scope.** The candidates and the error order below are evaluated as
      if it were the only node, so another node's eligibility never lets a request through
      that the named node refuses.
    - **Every verdict listed.** The error body still lists every configured node's
      verdict.
    - **Admin routes** (REQ-41) don't name a model in the sense of this rule, and are not
      checked.
  - **A per-node verdict for every configured node**, exactly one of:
    - `eligible`;
    - `not_eligible` (not in the model's list, `listed_only`, or `max_params_b` too small);
    - `no_size_policy` (rule 4);
    - `size_unknown` (rule 3 applies, but the parameter count is unknown);
    - `model_absent` (its last discovery answered and did not list the model);
    - `discovery_unknown` (its last discovery failed or could not be parsed, REQ-34);
    - `offline` (the node's admin switch is off, REQ-19a);
    - `unhealthy`.

    **The order of checking** (the list above is not the order):
    1. `offline`;
    2. `unhealthy`;
    3. `discovery_unknown`;
    4. `model_absent`;
    5. only then the rules, giving `eligible`, `not_eligible`, `no_size_policy` or
       `size_unknown`.

    The first that applies is the node's verdict. So a node that is both unhealthy and
    unknown is `unhealthy`, and an `unlimited` node that does not list the model is
    `model_absent`, never `eligible`.

    **Presence is kept separately.** Each node also has a `lists_model` value, whatever its
    verdict:
    - `unknown` if its last discovery attempt failed, or none has succeeded yet;
    - otherwise `true` or `false`, from that discovery;
    - **for an `offline` or `unhealthy` node** that has been discovered at least once, the
      value from its last successful discovery, even if later attempts failed. A node taken
      offline and rebooted, or one that crashed while it was the only holder of a model,
      therefore gives `GPU_OFFLINE`, not `DISCOVERY_INCOMPLETE`. That retained value only
      chooses the error; it never makes a node a candidate.
    - **Back in service.** When an `offline` or `unhealthy` node becomes routable again, its
      `lists_model` becomes `unknown` until its `/api/tags` is refreshed (REQ-42a), because
      its models may have changed meanwhile.

    **The rules are also applied to `offline` and `unhealthy` nodes**, on their last known
    metadata, to give a `rule_verdict` (`eligible`, `not_eligible`, `no_size_policy` or
    `size_unknown`). It is never used to route, only to choose the error.
    - **`rule_verdict` is `null`** unless `lists_model` is `true`: without the node's own
      entry for the model there is no metadata to judge.
    - **A node that could still be eligible.** A node with `lists_model: unknown` *could be
      eligible* unless its rules exclude the model by name alone: it is not in the model's
      eligible-nodes list, or it is `listed_only` and not named there. Otherwise (it is in the
      list, `unlimited`, or `max_params_b` with the size not yet known) it could be eligible.

    The error order below is defined on `lists_model`, not on the verdict. So an
    unreachable node, whose verdict is `unhealthy` and whose `lists_model` is `unknown`,
    still makes a model that no other node lists `DISCOVERY_INCOMPLETE`, as REQ-34
    requires.
  - **Verdict reasons** never contain the operator's offline reason or a key id. An offline
    node's reason is `offline_admin`. The full `AdminState` is in `/seriatim/nodes`, for
    admin keys (REQ-42).
  - **Candidates** are the nodes whose verdict is `eligible`, in configured priority order.
  - **When there are no candidates,** the first of these that applies is returned, so each
    case has exactly one answer:
    1. **`DISCOVERY_INCOMPLETE` (503, `Retry-After`).** No node has `lists_model: true`,
       and some node has `lists_model: unknown`.
    2. **`MODEL_NOT_FOUND` (404).** Every node has `lists_model: false`.
    3. **`GPU_OFFLINE` (503).** Some node with `lists_model: true` is `unhealthy` or
       `offline` and has `rule_verdict: eligible`, and no node is a candidate. The model
       could run there once the node is back. Existing clients already treat this code as
       fail-fast (ruling Q7), so it has `retryable: false` and **no** `Retry-After`. The verdicts in
       `nodes` tell an admin switch apart from a failure.
    4. **`DISCOVERY_INCOMPLETE` (503, `Retry-After`)** again, when some node with
       `lists_model: unknown` could be eligible (above). That node may hold the model and
       take it soon, so a retryable answer comes before the operator errors below, which
       another node's rules would otherwise give.
    5. **`SIZE_POLICY_MISSING` (503, `retryable: false`).** Some node that lists the model
       has `no_size_policy` as its verdict or its `rule_verdict`. The operator has to add a rule; retrying won't help.
       The `hint` names the node and the four ways to fix it (an eligible-nodes list,
       `unlimited`, `listed_only`, `max_params_b`).
    6. **`MODEL_SIZE_UNKNOWN` (422).** Some node that lists the model has `size_unknown` as
       its verdict or its `rule_verdict`. `size_overrides` fixes it.
    7. **`MODEL_NOT_ELIGIBLE` (422).** Every node that lists the model has `not_eligible` as
       its verdict or its `rule_verdict`, and no node with `lists_model: unknown` could be
       eligible. No node could run it, even with every node back.

    **Every one of these lists every configured node's verdict** in `nodes`, each with its
    id, verdict and reason (contract `ErrorEnvelope.nodes`).
  - **Discovery** refreshes every 60 s, and immediately when a model is missing.
- **REQ-33. Never outside the operator's rules.** The gateway MUST NOT send a model to a node
  where it is not `eligible`, even when every eligible node is down or busy. That prevents
  the failure this rule exists for: a model sent to a machine too small for it, where Ollama
  spills layers onto the CPU (E3).
  - **What this guarantees.** The guarantee is relative to the operator's configuration.
    Seriatim checks the rules exactly. Whether a rule is right for the hardware is the
    operator's judgement, informed by the measured capacity and by the loaded models'
    `size_vram` that `/seriatim/nodes` shows next to it (REQ-42).
- **REQ-34. Unknown model vs unknown state.**
  - 404 `MODEL_NOT_FOUND` only when **every** configured node answered its last discovery
    with a valid `/api/tags`, and none lists the model.
  - If a node has **never** been discovered, or its last discovery failed or could not be
    parsed while it is healthy and online, its state is unknown. A model not found on the
    others then gets 503 `DISCOVERY_INCOMPLETE` with `Retry-After`.
  - An `offline` or `unhealthy` node that was discovered before keeps its last successful
    answer (REQ-32), so a model it listed gets `GPU_OFFLINE`, not `DISCOVERY_INCOMPLETE`.
- **REQ-35. No healthy node.** If a node that lists the model and whose rules admit it
  is `unhealthy` or `offline`, and no node is a candidate (REQ-32, case 3), the request gets 503
  `GPU_OFFLINE` with `retryable: false`, its per-node verdicts, and no `Retry-After`. The
  code and the `retryable` value match what existing clients already treat as fail-fast
  (ruling Q7).
  - **Fleet-wide answers are different.** A route that names no model (`/`, `HEAD /` and
    the merged views of REQ-38) gets 503 `FLEET_UNAVAILABLE` when no node answers. That is
    `retryable: true`, with `Retry-After: 15`, the health-check interval, and no per-node
    verdicts.
- **REQ-36. Busy and spill-over (ruling Q1).** The gateway has no lock and no queue. REQ-37
  extends the same rule to Seriatim's other refusals.
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
    node id tried>` and `X-Reroute-Reason`, with the same values as the metric's `reason`.
    Nodes skipped without being tried are not reported there.
- **REQ-37. Node failure.**
  - **One failover rule.** The gateway MUST try the next eligible candidate at once, in
    priority order and once each, exactly when one of these holds. In none of them was
    Ollama contacted:
    1. **The gateway's connect to the node failed.** The gateway records it as 502
       `UPSTREAM_UNAVAILABLE` with `source: gateway`, `replay_safe: true` and the node's
       id, and that is the answer the client gets if it is the one chosen below.
    2. **The node answered with an `ErrorEnvelope`** whose `source` is `sidecar` and whose
       `replay_safe` is `true`, and whose code and `X-Seriatim-Gate` value are one of these
       pairs:

       | `error_code` | `X-Seriatim-Gate` | reroute reason |
       |---|---|---|
       | `GPU_BUSY` (REQ-36) | `busy` | `busy` |
       | `NODE_OFFLINE` | `refused` | `offline` |
       | `OLLAMA_EXPOSED`, `EXPOSURE_UNVERIFIED` | `refused` | `exposure` |
       | `NODE_STARTING` | `refused` | `starting` |
       | `NUM_CTX_EXCEEDS_LIMIT` (REQ-8: the nodes may have different limits) | `refused` | `num_ctx_limit` |
       | `NODE_SATURATED` | `ungated` | `saturated` |
       | `UPSTREAM_UNAVAILABLE` (the sidecar's connect to Ollama failed) | `acquired` or `ungated` | `unreachable` |

       A gateway connect failure (case 1) also has the reason `unreachable`.

    Nothing else fails over: not the status code alone, not an envelope with another
    `source`, and not another replay-safe sidecar code (such as `BAD_PATH`), which every
    node would answer the same way.
  - **Which reason is reported.** `X-Rerouted-From` names the first node tried, and
    `X-Reroute-Reason` is that node's reason from the table. Each spill-over is counted
    separately in `spillovers_total` with its own node and reason (REQ-67).
  - **When every candidate refuses,** the client gets one answer, the first that applies:
    1. if any candidate was busy, that 429, with the smallest `Retry-After`;
    2. if any refusal was retryable (`NODE_STARTING`, `EXPOSURE_UNVERIFIED`,
       `NODE_SATURATED`, `UPSTREAM_UNAVAILABLE`, or a failed connect), the first of those,
       because that node may take the request soon;
    3. if every candidate refused for `num_ctx`, that 422, with the **largest** `limit`
       among them, and a `hint` naming that node;
    4. otherwise, the first refusal that is not for `num_ctx` (`NODE_OFFLINE`,
       `OLLAMA_EXPOSED`).
  - **Marking.** A `NODE_OFFLINE` answer marks that node offline until its next status
    read, and never marks it down. Only `UPSTREAM_UNAVAILABLE` and a failed connect mark a
    node down. `NODE_SATURATED` and the other refusals do not.
  - **Only routed requests fail over.** A request the gateway routed itself may be retried on
    another candidate. A request that names its node in `X-Seriatim-Node`, which every admin
    request must (REQ-41), is never retried elsewhere. Its failure goes back to the
    client.
    Those nodes are marked down or busy as appropriate.
  - A failure after the request reached Ollama is never retried; the client gets it as in
    REQ-24.
  - A node that is down is checked again through `/seriatim/health` every 15 s. When it
    answers healthy again, its `lists_model` becomes `unknown`, its `/api/tags` is refreshed
    at once, and it is not a candidate until that succeeds (REQ-42a).
- **REQ-38. Merged views, in the standard shapes (maintainer decision).** A response on an
  Ollama-native (`/api/*`) or OpenAI-compatible (`/v1/*`) route has **exactly Ollama's
  schema**. Seriatim adds no body field there, not even in a merged view. Seriatim's own
  data lives on `/seriatim/*` paths and in `X-Seriatim-*` headers. Seriatim's own refusals
  are its `ErrorEnvelope`, marked by `X-Seriatim-*` headers, as before.
  - **`GET /api/tags`:** one entry per model name, so a client sees one model once.
    - **The source node** for a name is chosen among the nodes whose answer lists it, by
      priority, in this order: first a node whose verdict is `eligible`; then one whose
      `rule_verdict` is `eligible` (it is offline or unhealthy now); then any node that
      answered. The entry is that node's, unchanged, in Ollama's fields only. So the entry
      describes the model that requests are routed to first.
    - **Different models under one name** (the nodes' digests differ) still give one entry,
      the source node's. That entry is **representative**: if the source node is busy, a
      request may fail over to a node with a different digest under the same name. Which
      node holds which digest is in `/seriatim/models`, with `digests_differ`.
    - **Order:** by `modified_at`, newest first, as Ollama orders its own list, then by
      name. `modified_at` is compared as a parsed instant, because each node writes it in
      its own time zone and precision.
    - **Priority ties** between nodes are broken by their order in the configuration file,
      here and everywhere else.
  - **`GET /api/ps`:** every node's entries, unchanged and concatenated in node priority
    order. A model loaded on two nodes appears twice, which Ollama's list allows.
  - **`GET /api/version`:** `{"version": "<v>"}`, Ollama's shape, where `<v>` is the
    lowest of the nodes' versions, compared as dotted integers after removing any `-…` or
    `+…` suffix. A node whose version still can't be parsed is left out and named in
    `X-Seriatim-Partial`. If no version can be parsed, the answer is 503
    `FLEET_UNAVAILABLE`.
  - **`GET /v1/models`:** OpenAI's list shape exactly: one entry per model id, from the same
    source node as `/api/tags`, so `created` and `owned_by` agree with it. Ordered by
    `created`, newest first, then by id.
    - **If the source node's `/v1/models` fails,** or doesn't list the id, the entry comes
      from the next node in the same order whose `/v1/models` lists it, and the source node
      is named in `X-Seriatim-Partial`. If no node's list has it, the id is left out.
  - **Partial results** are signalled only by the `X-Seriatim-Partial` header, which names
    the nodes that didn't answer, or whose answer didn't validate against the plain shape
    and was therefore left out. If no valid answer remains, the view gets 503
    `FLEET_UNAVAILABLE`.
  - **Per-node detail** is on Seriatim's own paths:
    - `GET /seriatim/models` (scope `infer`): for each model name, every node that lists
      it, with that node's `digest`, `size`, `parameter_size` and `modified_at`, plus
      `digests_differ` when they don't agree. Models are ordered by name, and each model's
      nodes by priority. A node that didn't answer is listed in `partial`.
  - **Live, not cached.** The merged views and `/seriatim/models` ask the nodes at the time
    of the request, each bounded by the discovery timeout; if no node answers, they get 503
    `FLEET_UNAVAILABLE`.
    - `/seriatim/nodes` (REQ-42): each node's loaded models and Ollama version.
  - **Checked against the plain shapes.** Every merged response validates against the
    response shape recorded from the pinned Ollama version by S0b, with no extra keys (F4,
    and REQ-62a).
- **REQ-39. `/models/select`.**
  - It picks a model and a node for a `task`, from operator-configured preference lists, model
    presence and the eligibility rules (REQ-32).
  - **The chosen node** is always `eligible`. `eligibility_basis` names the rule that made
    it so: `eligible_nodes`, `unlimited` or `max_params_b`.
  - **`alternatives`** lists the other configured nodes for the chosen model, each with its
    verdict and reason.
  - **When no preferred model has a candidate,** it returns the error that REQ-32 would
    return for the first preferred model.
  - `context_tokens` is accepted and echoed, for compatibility. It doesn't affect the choice
    in Phase 1.
  - An unknown task gets 400 `UNKNOWN_TASK`.
- **REQ-40. Gateway passthrough.** The gateway meets REQ-6, REQ-7 and REQ-24 end to end.
- **REQ-41. Admin requests through the gateway** (pull, push, create, copy, delete, blobs, and
  unknown endpoints) MUST name a node in `X-Seriatim-Node`. Otherwise they get 400
  `NODE_REQUIRED`.
- **REQ-42. Gateway health.**
  - `/seriatim/health` returns 200 when at least one node is healthy, otherwise 503.
  - `/seriatim/nodes` (scope `infer`, redacted for `infer` keys as REQ-5 says) returns, for
    each node:
    - its id, health, priority, last check time, Ollama version and status;
    - its `admin_state`, and `reconciling` while the gateway reconciles a new epoch
      (REQ-42a);
    - **its configured eligibility:** `size_policy` (`unlimited`, `listed_only`,
      `max_params_b` or `none`),
      `max_params_b` when set, and `eligible_for_models`: the models whose eligible-nodes
      lists name it;
    - **its measured capacity**, next to that eligibility: the node's last `Capacity` report
      (REQ-16), or `null` with the reason it could not be read;
    - **the loaded models** from the node's `/api/ps`, each with `size` and `size_vram`. A
      model with `size_vram < size` is partly on the CPU, which tells the operator that
      the node's rule allows a model that is too large;
    - **warnings**, for example a node with no size policy at all.
- **REQ-42a. The admin switch through the gateway (slice S8).**
  - **Routes**, with the gateway's admin key: `POST /seriatim/admin/nodes/{id}/offline`
    (same optional body as REQ-19a) and `POST /seriatim/admin/nodes/{id}/online`.
  - **They forward** to that node's sidecar route, with the node's admin key from the
    gateway configuration.
  - **Success only after confirmation.** The gateway returns 200, with the sidecar's
    `AdminState` and the node id, only after the sidecar has answered 200. It returns:
    - 404 `NOT_FOUND` for an unknown node id;
    - 502 `UPSTREAM_UNAVAILABLE` when the connection fails, before anything is sent;
    - 502 `UPSTREAM_PROTOCOL_ERROR` when the call was sent but no HTTP answer arrives, as
      after a 10 s timeout. The outcome is then **unknown**, because the sidecar may have
      committed. The error says so, and the gateway answers at once: it does **not** wait
      for the status refresh it then starts in the background, itself bounded at 10 s.
      Re-sending is still safe, because the call is idempotent;
    - the sidecar's HTTP error, unchanged, when the sidecar answers with one;
    - 502 `UPSTREAM_PROTOCOL_ERROR` when a 200 body is not a valid `AdminState`.

    It never retries on another node.
  - **No state of its own.** The sidecar is the source of truth, and the gateway persists
    nothing.
    - **What it keeps** is an in-memory observation of each node's `AdminState`, with the
      time it was read.
    - **Where it comes from:**
      - **authoritative:** the sidecar's `/seriatim/status`, read with the node's admin key
        every 15 s, including while health is degraded, and the `AdminState` returned by a
        confirmed admin call. A confirmed call **in the held epoch** is applied at once, so
        routing changes before the next read; one in a new epoch goes through
        reconciliation (below);
      - **hints only:** health saying `offline`, and a `NODE_OFFLINE` answer. Neither
        carries an epoch or revision, so neither overwrites the observation. Each marks the
        node offline for routing and triggers an immediate status read. A hint whose
        request was sent before the held observation was received is ignored, so a slow
        health check can't undo a confirmed `online`.
    - **Unreachable while offline.** A node that stops answering keeps its last observed
      `AdminState`, so a node rebooted during maintenance stays `offline`, not `unhealthy`,
      until a status read shows otherwise.
    - **Ordering: one authoritative operation per node at a time.** Status reads and admin
      calls to a node are serialised by a per-node lock on the gateway, so their answers
      can't cross.
      - **Fencing.** Each operation takes a sequence number. An operation that timed out
        is fenced: its late answer, if one ever arrives, is discarded.
      - **Same epoch:** an observation with a lower `revision` than the one held is
        ignored.
      - **A different epoch: reconciliation.** Any authoritative answer, a status read or
        a confirmed admin call, that carries an epoch other than the held one puts the node
        in the **reconciling** state:
        1. **Out of routing at once.** Its verdict is `unhealthy` with the reason
           `admin_state_reconciling`, and health hints don't change that. The held
           observation is kept for display only.
        2. **One immediate fenced status read,** under the per-node lock, started after the
           new epoch was seen. The answer that triggered reconciliation never decides on
           its own.
        3. **It decides.** If that read shows the same new epoch, its `AdminState` becomes
           the held observation (any revision is accepted, because the epoch is new) and the
           node leaves reconciling. If it shows yet another epoch, or fails, the read is
           retried up to 3 times, 1 s apart.
        4. **Not yet routable.** A new epoch means the sidecar restarted or recovered, so its
           models may have changed. On leaving reconciling, its `lists_model` becomes
           `unknown` and it is routed to only after its `/api/tags` is refreshed (below),
           whatever its state was before.
        5. **Bounded.** After those retries the node stays reconciling, out of routing, and
           the regular 15 s status read tries again. No answer from before the new epoch can
           end reconciliation, because every read in it was started after the epoch was
           seen.
  - **Coming back into service.** When a node's observation turns from offline to online,
    or it leaves reconciling, or it turns from unhealthy to healthy, its `lists_model`
    becomes `unknown` and the gateway refreshes its `/api/tags` before routing to it,
    because models may have changed meanwhile. Until then the node is not a candidate. Two
    gates must both be passed to route: a confirmed online state in the current epoch, and
    a successful discovery after it.
    - **After a confirmed `online` call,** the gateway does, in order and still holding
      the per-node lock: reconciliation's status read, if the answer was in a new epoch;
      then that refresh, bounded by the discovery timeout; then the 200. A request sent
      right after the 200 can therefore be routed to the node.
      - **When a new-epoch `online` takes effect:** when the reconciliation read shows that
        epoch with `offline: false`. The node becomes routable when the discovery refresh
        after it succeeds.
      - **If the read or the refresh fails,** the 200 still stands, because the sidecar
        committed. The node stays out of routing until reconciliation, then discovery,
        succeeds in the background.
    - **After a status read** that shows the change, the refresh runs in the background.
  - **Routing.** An offline node is out of routing (verdict `offline`, REQ-32), so requests
    spill over to the other eligible nodes.
  - **Visible.** `/seriatim/nodes` shows each node's full `admin_state`: `offline`,
    `reason`, `since`, `changed_by`, `epoch` and `revision` (redacted for `infer` keys, REQ-5),
    and whether the gateway is reconciling it (below).

  *Tests:* GA1–GA14:
  - the gateway forwards the call and reports success only after the sidecar confirms;
  - an unreachable sidecar gives 502, and the node's state is unchanged in the view;
  - a timeout after the sidecar committed gives 502 with the outcome unknown, and the next
    status read shows the committed state;
  - an older observation never replaces a newer `revision` in the same epoch; health and
    `NODE_OFFLINE` never overwrite the observation;
  - **barrier tests for epochs:** a status read that snapshots the old epoch while a
    confirmed admin call reports the new one; a timed-out call whose answer arrives after
    recovery (fenced); in each, the node is out of routing while reconciling, the
    reconciliation read decides, and the node is routed again after recovery although the
    gateway held a higher revision;
  - **reconciliation bounds:** a reconciliation read that fails 3 times leaves the node out
    of routing until a later read succeeds; a new-epoch `online` returns 200 only after its
    reconciliation read and refresh, or with the node still reconciling if they failed;
  - after `online`, the node is not routed to until its discovery has been refreshed;
  - an unknown id gives 404;
  - requests fail over around an offline node;
  - online restores routing;
  - a non-admin key gets 403.
- **REQ-43. Compatibility (ruling Q7).** The headers and fields existing clients read keep
  their names. §7.1 documents the full compatibility surface.
- **REQ-44. The fleet is configuration.**
  - **One place.** The gateway's configuration file is the only description of the fleet.
    - **Per node:** `id`, `url`, keys and `priority`, plus an optional size policy:
      `max_params_b`, `capacity = "unlimited"` or `capacity = "listed_only"`.
    - **Per model** (optional): `[models."<name>"] eligible_nodes = [...]`.
    - **Fleet-wide:** `size_overrides` and the task preferences.
  - **Measured, but not yet trusted.** Each node measures and reports its capacity (REQ-16).
    `/seriatim/nodes` shows it next to the configured rules, so the operator can set them
    from real data. Admission from measured capacity is the later "Measured fit" slice.
  - **Any size.** The gateway MUST work with any number of nodes from one upwards.
  - **No hardware assumptions in production code or defaults.** They MUST NOT contain a node
    count, a node name, a GPU model, a memory size, an operating-system pairing or a role such
    as "small machine" or "large machine". There is no default size policy: a node without
    one is excluded (REQ-32).
    - **Allowed:** platform adapters (the probes of REQ-16, the per-OS install scripts),
      which by nature know their platform.
    - **Allowed:** sanitised recorded fixtures and generated test fleets, which carry
      concrete figures as data.
  - **Tests** run the same routing suite against fleets of 1, 2 and 5 nodes, mixing the three
    size policies and nodes with none, so no test depends on one particular fleet. Property
    tests generate fleets, rules and models, and check REQ-32's error order and REQ-33 for
    every combination.
- **REQ-45. Changing the fleet without a restart.**
  - **When it reloads.** The gateway reloads its configuration when the file's content
    changes, checked every 10 s, and on `SIGHUP` where the platform has it.
  - **Validation first.** The new file is validated exactly as at startup.
    - If it is invalid, the gateway keeps the configuration it has and logs an `ERROR` that
      names the key.
    - `/seriatim/nodes` then shows the error and when it happened.
    - A bad edit never takes the gateway down, and never leaves it half-applied.
  - **Atomic swap.** A valid file replaces the routing table in one step. A request that is
    already running keeps the node it was sent to. Each request sees either the old table or
    the new one, never a mix.
  - **Effects:**
    - **A node added:** its state is *unknown* until its first health check and discovery
      have succeeded. Until then it is not a candidate, and a model not found elsewhere gets
      `DISCOVERY_INCOMPLETE`, not `MODEL_NOT_FOUND` (REQ-34).
    - **A node removed:** it stops being a candidate at once. Requests already running on it
      finish normally, and its cached health and models are dropped.
    - **`priority`, `max_params_b`, `capacity`, an eligible-nodes list or `size_overrides`
      changed:** applies from the next routing decision.
    - **A node's `url` or keys changed:** treated as removing the old node and adding a new
      one.
    - **Client keys** added or removed: apply from the next request. A gateway key can
      therefore be rotated without a restart.
  - **The sidecar is different.** Its configuration changes need a restart, because a
    restart is what safely resets its lock state (REQ-12a).
- **REQ-46. Platform independence.**
  - **The sidecar** MUST run on Linux, Windows and macOS, and its unit and component tests run
    on all three in CI (REQ-60).
  - **Nothing depends on a GPU vendor except the memory probes** (REQ-16). The gate, the
    passthrough and the routing never do.
  - **Deploying** a node needs only Python 3.12, Ollama bound to loopback, the sidecar as a
    service, and the firewall rule (§6.1). The install scripts (§7.3, §7.4) automate this for
    their platforms. The local-only check (REQ-53) states what must hold on any platform.

### 4.5a Monitoring (sidecar in S4d, gateway in S8)

- **REQ-67. Prometheus metrics through the textfile collector.**
  - **The mechanism.** Each sidecar, and the gateway, writes its metrics in the Prometheus
    text exposition format to a file that the host's standard exporter already serves:
    - `node_exporter` on Linux and macOS, with `--collector.textfile.directory`;
    - `windows_exporter` on Windows, with its `textfile` collector.

    Seriatim opens no metrics port of its own, so the exposure and authentication rules
    (§6) are unchanged. ADR-001 D15 explains why.
  - **Configuration.**
    - `SERIATIM_NODE_METRICS_TEXTFILE_DIR` or `SERIATIM_GW_METRICS_TEXTFILE_DIR` names the
      collector's directory. When it is unset, no file is written.
    - **One writer per file.** The file is `seriatim_node.prom` on a node, and
      `seriatim_gateway_<instance>.prom` for a gateway, where `<instance>` is
      `SERIATIM_GW_INSTANCE` (default `default`), which must match
      `^[a-z0-9][a-z0-9-]{0,31}$`, or the gateway exits with code 2. Several gateway copies
      may run, so each needs its own instance name. Every gateway series carries the label
      `gateway="<instance>"`.
    - **Enforced.** A writer holds an exclusive lock on `.<file>.lock` in the directory. A
      second writer that can't take it writes no metrics and logs an `ERROR` naming the
      file.
  - **Writing.** Every 15 s, and at shutdown, the file is written atomically: a temporary
    file in the same directory, then a rename. The collector therefore never reads half a
    file.
    - **The temporary name** is `.<file>.<pid>.tmp`. It doesn't end in `.prom`, so the
      collector never reads it, and it is unique to the writer.
    - **No sample timestamps.** `node_exporter` rejects a whole file that has them.
      Freshness is the value of `metrics_timestamp_seconds`.
    - **On Windows** a reader can hold the file open. The rename is retried up to 5 times,
      100 ms apart. If it still fails, the old, complete file stays, and the failure is
      logged.
    - **Mode** 0644, because the exporter must read it. On Windows, the file takes its ACL by
      inheritance from the directory, where the install script grants the exporter's
      account read and the sidecar's account modify, both inherited by new files
      (`(OI)(CI)`). The sidecar sets no ACL itself.
    - **In a container,** the directory must be a bind mount of the host's collector
      directory, writable by the container's user. A named volume the exporter can't see is
      a configuration error, and the startup check reports it.
    - **A failed write** is logged at `WARNING` at most once a minute, and never affects
      serving.
  - **What the file must not contain:** keys, key hashes, prompts, request bodies or
    addresses. Labels may carry only configured node ids, the gateway instance name, device
    indices, error codes, the Seriatim version, and the fixed values listed below for each
    label.
    - **A retired gateway instance's file** stays in the directory until removed. The
      install notes say to remove it, and alerts on `metrics_timestamp_seconds` catch one
      that was forgotten.
    - **No model names in labels.** They are unbounded and partly user-controlled.
    - **No client key ids in labels.** The request log (REQ-22) answers "who".
  - **Sidecar metrics** (prefix `seriatim_node_`):
    - **Every family** has `# HELP` and `# TYPE` lines. Info and state metrics, such as
      `build_info`, `capacity_source` and `exposure`, are gauges with the value 1 for the
      current label value.
    - `up`: 1 while serving, and 0 in the file written at a clean shutdown. After a crash the
      exporter keeps serving the last file, so alerts must use
      `time() - metrics_timestamp_seconds`, not `up` alone;
    - `build_info{version}` and
      `metrics_timestamp_seconds`, which lets a stale file be alerted on;
    - `slots{state}`: the number of slots in each of `free`, `held`, `draining`, `stuck`;
    - `admissions_total{result}`: each gated request is counted **once**, by its final
      admission outcome:
      - `acquired`: it took a slot and went on to Ollama, even if the connect to Ollama then
        failed;
      - `busy`: the admission 429;
      - `refused`: every answer with `X-Seriatim-Gate: refused` (`NODE_STARTING`,
        `NODE_OFFLINE`, `OLLAMA_EXPOSED`, `EXPOSURE_UNVERIFIED`, and
        `NUM_CTX_EXCEEDS_LIMIT`, which is decided after the slot was taken);
      - `rejected`: it took a slot and then ended before Ollama, with a 400, 408 or 413, or
        because the client disconnected while its body was being read (REQ-10).

      A request that took a slot and was then refused or rejected is never also counted as
      `acquired`. A gated request turned away before the admission check (a bad path, or a
      413 on its `Content-Length`) is not an admission, and is counted only in
      `requests_total` and `errors_total`;
    - `requests_total{kind, status}`: `kind` is `gated`, `ungated` or `seriatim`;
      `status` is the HTTP status class (`2xx`, `4xx`, `5xx`);
    - `errors_total{error_code}`, for every code the **sidecar** can produce (the codes
      marked "sidecar" or "both" in ADR-001 §5.4), including in-stream
      `UPSTREAM_STREAM_BROKEN` records. Gateway-only codes never appear in the sidecar's
      file;
    - `hold_seconds`: a histogram of gated slot hold times, with buckets 1, 5, 15, 60,
      300, 900 and 3600 s;
    - `drains_total{cause}`, one per abnormal end (REQ-10):
      - `disconnect`: the client went away;
      - `deadline`: the hold limit passed (`LOCK_HOLD_EXCEEDED`);
      - `upstream_error`: Ollama failed after the request was sent
        (`UPSTREAM_PROTOCOL_ERROR`, `UPSTREAM_STREAM_BROKEN`);
      - `cancel`: an `INTERNAL` error after sending, or a shutdown that cut a request off.

      A rejection before Ollama (REQ-10) has no drain, so it has no cause;
    - `admin_offline` (0 or 1), `starting` (0 or 1);
    - `exposure{state}`: 1 for the current state of `local_only`, `exposed` or
      `unverified`;
    - `ollama_reachable` (0 or 1);
    - **capacity, from REQ-16:** `capacity_usable_bytes`, `capacity_source{source}` and
      `device_memory_bytes{index, kind}`, where `kind` is `total`, `free` or `usable`. A
      figure that is `null` in the report is left out of the file, never written as 0.
  - **Gateway metrics** (prefix `seriatim_gw_`):
    - `up`, `build_info{version}`, `metrics_timestamp_seconds`;
    - **per node:** `node_healthy{node}` and `node_offline{node}`. Per-node series exist only
      for the nodes currently configured: a node removed by a reload disappears from the
      next file;
    - `routed_total{node, outcome}`, where `outcome` is `served`, `spilled` or `failed`;
    - `spillovers_total{from_node, reason}`: a tried node answered with a replay-safe
      refusal (REQ-37) and the next was tried. `reason` is `busy`, `offline`, `starting`,
      `saturated`, `exposure`, `num_ctx_limit` or `unreachable`. Skipping a node already known to be offline
      or down is not a spill-over, and is counted in `skipped_total{node, verdict}`;
    - `routing_errors_total{error_code}`, for the REQ-32 errors;
    - `config_reloads_total{result}` (`applied` or `rejected`) and
      `config_last_applied_timestamp_seconds`.
  - **Counters reset** when the process restarts, as Prometheus expects.
  - **The install scripts** (§7.3, §7.4) set the directory when an exporter with a textfile
    collector is present, and never install or reconfigure the exporter. They read the
    directory from the exporter's own configuration (`--collector.textfile.directory`, or
    `windows_exporter`'s `--collector.textfile.directories`), never assume a path.

  *Tests:* MX1–MX12:
  - a second writer for the same file writes nothing and logs the error; two gateway
    instances write two files with distinct `gateway` labels;
  - a node removed by a reload has no series in the next file;
  - the file parses with a pinned `promtool check metrics`, and a pinned `node_exporter`
    serves every series from it;
  - no sample has a timestamp (no sample line has a third field), every family has
    `# HELP` and `# TYPE`, and the temporary file is never read;
  - on Windows, a rename while the exporter has the file open keeps the old file intact;
  - every documented metric and label is present, and nothing else;
  - the write is atomic under concurrent reads;
  - a failed write doesn't affect serving;
  - a `null` capacity figure is left out;
  - no key, address or model name appears in the file;
  - an unset directory writes nothing;
  - the gateway's per-node series follow config reloads.

### 4.6 Repository and process

- **REQ-50. Licence and contributing.** MIT licence. `CONTRIBUTING.md` says that bug-fix PRs
  are welcome and that features start as an issue.
- **REQ-51. No identifying data.**
  - The repository holds no host names, no IP addresses other than the allowed ones, and no
    keys or tokens. The allowed addresses are loopback (`127.0.0.0/8` and `::1`), `0.0.0.0`
    and the RFC 5737 documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`,
    `203.0.113.0/24`).
    - An IPv4-mapped IPv6 address (in `::ffff:0:0/96`, the IPv4 part written dotted or in
      hex) is judged by the IPv4 address it maps to, so a hex form can't hide a private
      address.
    - Every other IPv6 address is refused, the IPv6 documentation prefix (RFC 3849)
      included. A bare `::` is punctuation, not an address.
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
| The sidecar is starting | 503 `NODE_STARTING` + `Retry-After` + `X-Seriatim-Gate: refused`; body not read | Tries the next node (replay-safe) |
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
| `num_ctx` over its limit, ambiguous, or sent on `/v1` | 422 / 400; Ollama not contacted; the over-limit 422 carries `X-Seriatim-Gate: refused` | Over the limit: the next eligible candidate is tried, and if all refuse, the largest limit is returned (REQ-37). Ambiguous or on `/v1`: passed through, because every node would refuse it |
| Body cannot be parsed, or `model` missing or not a string | 400 `BAD_REQUEST_BODY`, when a check needs the body | 400 `BAD_REQUEST_BODY` before routing (REQ-32); no node contacted |
| GPU probe fails / `/api/ps` fails | `source: unavailable`, with an error / `loaded_models: null`, with an error | Shown in `/seriatim/nodes`. Routing is unaffected, because it uses the configured rules (REQ-32) |
| Ollama reachable from the LAN | 503 `OLLAMA_EXPOSED` + `X-Seriatim-Gate: refused` on gated routes; health degraded | Tries the next eligible node (REQ-37); node unhealthy |
| Exposure check uncertain | 503 `EXPOSURE_UNVERIFIED`; never treated as safe | Tries the next node |
| Invalid or unknown config key | Exit code 2, naming the key | The same |
| No keys configured | Refuses to start (REQ-15) | The same |
| Model missing / node state unknown / no healthy node / size unknown | — | 404 `MODEL_NOT_FOUND` / 503 `DISCOVERY_INCOMPLETE` / 503 `GPU_OFFLINE` / 422 `MODEL_SIZE_UNKNOWN` |
| Node switched offline by the operator | 503 `NODE_OFFLINE` for new gated requests; in-flight requests finish; state persists across restarts | Node out of routing (verdict `offline`); spill-over to other eligible nodes; `GPU_OFFLINE` if none is left |
| No eligible node / a node with no size policy | — | 422 `MODEL_NOT_ELIGIBLE` / 503 `SIZE_POLICY_MISSING`, in the order of REQ-32, with every node's verdict; never sent to a node outside the rules |
| Some nodes unreachable in a merged view | — | Partial result, in Ollama's shape, flagged only by `X-Seriatim-Partial`; 503 `FLEET_UNAVAILABLE` if none answered |
| Admin state lost or unreadable at startup | Node starts offline in a new epoch, `ERROR` logged; an explicit `online` recovers it (REQ-19a) | Node out of routing until a status read shows it online |
| A pre-admission refusal (offline, exposure, starting, `num_ctx` limit) | 503 or 422 with `X-Seriatim-Gate: refused`; body not read (except for the `num_ctx` check); Ollama not contacted | The next eligible candidate is tried (REQ-37) |
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

### 6.1a The admin switch

The offline switch (REQ-19a, REQ-42a) can take a node out of service, so it is an `admin`
route on both the sidecar and the gateway, and every change is logged with the key id. A
holder of an admin key can already delete models, so the switch adds no new class of power.
An offline node still serves the read-only routes, so taking it offline hides nothing from
the operator.

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
  hash, move the client over, then remove the old hash. That takes a configuration reload on
  the gateway (REQ-45) and a restart on a node. A key's id appears in
  logs; the key itself never does.
- **Transport.** Plain HTTP on a trusted LAN by default, which is a residual risk (ADR-001 §8).
  TLS is optional (REQ-27).
  - **Active attackers are out of scope while TLS is off.** Someone who can read or change
    LAN traffic can steal keys. They can also forge a node's admission 429, so that the
    gateway runs a request twice.
  - **Where the LAN is not trusted,** TLS between the gateway and the nodes is required.
- **In the repository.**
  - gitleaks runs in the pre-commit hook and in CI (workflow `secret-scan`, check `gitleaks`).
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
- 503 with `error_code: GPU_OFFLINE` and `retryable: false` as fail-fast. Revision 5 keeps
  that: it has no `Retry-After`. The fleet-wide case is the new `FLEET_UNAVAILABLE`, which
  falls under the next line;
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
- **`/models` and `/status` are replaced** by `/api/tags` (merged, in Ollama's shape),
  `/seriatim/models` and `/seriatim/nodes`.
- **No Seriatim fields in standard bodies.** The `/api/*` and `/v1/*` responses are exactly
  Ollama's (REQ-38). Per-node detail is on `/seriatim/*`.
- **`X-GPU-Status` is not sent.** No existing client reads it.

### 7.2 Scripts

Every script reads its configuration from a config file beside it, holds no host names in the
repository, is idempotent, logs with timestamps, and exits non-zero on any failure.

- **REQ-53. Local-only check** (`deploy/<os>/check-local-only`). It checks all of these:
  - port 11435 listens on loopback only;
  - port 11434 belongs to the sidecar process, and exactly one sidecar process is running;
  - Ollama's own auto-start (the tray app or login item) is disabled;
  - the Ollama service environment sets `OLLAMA_HOST` and `OLLAMA_NUM_PARALLEL=1`;
  - the sidecar's copies of the Ollama settings it reports (REQ-16) equal the Ollama
    service's own: `OLLAMA_CONTEXT_LENGTH`, `OLLAMA_KV_CACHE_TYPE`, `OLLAMA_FLASH_ATTENTION`
    and `CUDA_VISIBLE_DEVICES`. Each must be either unset on both sides or equal;
  - when `CUDA_VISIBLE_DEVICES` uses indices, the Ollama service sets
    `CUDA_DEVICE_ORDER=PCI_BUS_ID`;
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
  - It does **not** update Seriatim (ruling Q10). Seriatim changes only through a verified
    release (§7.6, §7.7), or, until the follower has passed PPE on that host, the interim
    deployment (§7.8).
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
  - creates the state directory `%ProgramData%\seriatim\state`, with an ACL for the
    sidecar's service account and Administrators only, and runs `seriatim-node init-state`
    (REQ-19a);
  - if `windows_exporter` is installed with its textfile collector, reads its directory from
    the service's `--collector.textfile.directories`, sets
    `SERIATIM_NODE_METRICS_TEXTFILE_DIR`, and grants the sidecar's service account modify
    rights on that directory (REQ-67). It never changes the exporter;
  - installs Python 3.12+ and a release, verified as REQ-68a requires, into a venv under `%ProgramFiles%\seriatim`;
  - puts the config in `%ProgramData%\seriatim\`, with an ACL for the service account and
    Administrators;
  - registers two services with WinSW: `seriatim-ollama` (`ollama serve`, with `OLLAMA_HOST`,
    `OLLAMA_NUM_PARALLEL=1` and `OLLAMA_MODELS`) and `seriatim-node`, which depends on it;
  - disables the Ollama tray app's auto-start;
  - adds a firewall rule for inbound TCP 11434 on the Private profile;
  - runs the local-only check.

  The weekly task runs from Task Scheduler. To upgrade Seriatim: stop `seriatim-node`,
  install the new tagged release, start the service, then run the local-only check.

### 7.4 macOS (Apple Silicon node)

- **REQ-58.** `deploy/macos/install.sh` does the following:
  - if `node_exporter` runs with `--collector.textfile.directory` (it has no default on
    macOS), sets `SERIATIM_NODE_METRICS_TEXTFILE_DIR` to that directory (REQ-67). It never
    changes the exporter;
  - installs a release, verified as REQ-68a requires, into a venv under `/usr/local/seriatim`;
  - puts the config in `/usr/local/etc/seriatim/` (mode 0600);
  - runs the sidecar's LaunchDaemon as a dedicated service account (`UserName` in the
    plist), not as root;
  - creates `/Library/Application Support/seriatim/run` and `.../state` owned by that
    account (mode 0700), with every parent owned by `root:wheel`, then runs
    `seriatim-node init-state` as that account (REQ-12b, REQ-19a);
  - installs the `macos` extra, which adds `pyobjc-framework-Metal` for the capacity probe
    (REQ-16);
  - adds launchd plists `org.seriatim.ollama` and `org.seriatim.node`, with `KeepAlive`;
  - removes the Ollama.app login item;
  - adds an application-firewall rule;
  - runs the local-only check.

  Slice S6 tests whether Metal works under a LaunchDaemon, and records the result. The weekly
  job uses `StartCalendarInterval`.

### 7.5 Gateway host

- **REQ-59.** The gateway ships an OCI image and an example systemd unit. Its config lists
  each node's id, URL, keys and priority, its optional size policy (`max_params_b` or
  `capacity`), the per-model eligible-nodes lists, and `size_overrides` (REQ-44), and
  is reloaded live (REQ-45). Examples use placeholders only, such as `node-a.example`, and
  show fleets of more than one shape.

### 7.6 Releases (slices S0a, S10 and S12)

- **REQ-68. Release workflow.** `.github/workflows/release.yml` is the only way a release is
  made. It runs on CI only; nothing is published from a laptop.
  - **Tags from `main` only.** It runs on a pushed tag, and refuses, failing closed, any tag
    whose commit is not reachable from `main`.
  - **What it builds:** the sdist and wheels, and from S10 the gateway container image.
  - **What it publishes:** a GitHub Release with a SHA-256 checksum for every asset, and a
    build-provenance attestation (`actions/attest-build-provenance`) for each. The image
    goes to GHCR, and deployers refer to it **by digest**, never by tag.
  - **Two channels, one workflow:**
    - **pre-release:** `vX.Y.Z-rc.N`, published as a GitHub pre-release, for PPE and testing;
    - **release:** `vX.Y.Z`, a full release, for production.

    The same commit can be tagged rc first, then released.
  - **Who may publish.**
    - **Tags:** only the maintainer may create a `v*` tag, and **no one** may move or delete
      one, so a tag names one commit forever. The repository's immutable-releases setting
      also stops a published release's assets from being replaced (§7.9).
    - **Credentials:** only `release.yml`'s publish job holds write permissions
      (`contents: write`, `packages: write`, `id-token: write`, `attestations: write`), and
      it runs in the `release` environment, which the maintainer must approve and which
      accepts only `v*` tags. That approval applies to every `v*` tag, rc included. Every
      other workflow has no write permission to contents or packages, and no personal token
      publishes anything.
    - **Publishing order,** because immutable releases can't take assets once published:
      `release.yml` creates the release as a draft, uploads every asset and its checksum
      list, attests them, and only then publishes it (as a pre-release for an rc).
- **REQ-68a. Installers verify before installing.** The install scripts (REQ-57, REQ-58),
  the gateway deployment and the follower (REQ-69) install a release only after all of
  these hold, for the **exact tag they expect**. Any failure is a hard stop.
  - **The tag's commit.** The expected tag `vX.Y.Z[-rc.N]` is resolved to its commit in
    this repository. Tags are immutable (REQ-68), so that commit is fixed.
  - **The checksum:** each asset's SHA-256 matches the release's checksum list.
  - **The attestation, bound to tag, commit and digest:** `gh attestation verify` on each
    asset, and on the image as `oci://ghcr.io/…@sha256:<digest>`, with:
    - `--repo` this repository;
    - `--signer-workflow` this repository's `.github/workflows/release.yml`;
    - `--source-ref refs/tags/<expected tag>`;
    - `--source-digest <that commit>`;
    - `--deny-self-hosted-runners`.

    An older release, or an rc, therefore can't pass as a newer or full release, even
    though the same workflow built it.
  - **The version inside matches the tag.** For tag `vX.Y.Z` the package's own version is
    `X.Y.Z`; for `vX.Y.Z-rc.N` it is the PEP 440 form `X.Y.ZrcN`. The image's
    `org.opencontainers.image.version` label is the tag without its leading `v` (`X.Y.Z` or
    `X.Y.Z-rc.N`).
  - **The channel:** the release channel accepts only `vX.Y.Z` tags of full releases,
    never an rc or a release marked as a pre-release.
### 7.7 The release follower (a slice right after S12)

Releases are the only thing that will deploy. This replaces the deploy-branch design of
revision 2, and with it both revision-2 blocking findings: the follower needs no promotion App
and no ruleset bypass. (The interim deployment, §7.8, uses an App only to open pull requests,
with no bypass and no push to `ppe` or `prod`.)

- **REQ-69. The follower.** A generic, config-driven host agent in `deploy/`. Its config
  sets the channel, the install path or image, the service name and the health URL. No host
  appears in the repository. It:
  - polls GitHub Releases for its channel;
  - verifies each candidate exactly as REQ-68a requires;
  - installs side by side: a new venv, or the image by digest;
  - runs the health and local-only checks, then switches;
  - **rolls back automatically** to the last-good version if a check fails;
  - logs and reports the version, tag, commit and digest it runs, in status and in
    metrics.

  It covers both the sidecar (Windows and macOS services) and the gateway container.
  - **Channels and their order.**
    - **Pre-release** accepts `vX.Y.Z-rc.N` and `vX.Y.Z`; **release** accepts only `vX.Y.Z`
      (REQ-68a).
    - Versions are ordered as semantic versions, an rc before its release. The follower
      installs only a version **newer** than its last-good one.
  - **Durable record.** In its own state directory (owned like REQ-19a's, written
    atomically and durably), the follower keeps:
    - the current and last-good versions, each with tag, commit and artifact digests;
    - the held versions;
    - the pin, if any.

    A missing or unreadable record fails safe: the follower keeps running whatever is
    installed, installs nothing, and reports the error until an operator repairs it.
  - **Anti-downgrade.** A version older than the last-good one is refused, even if it
    verifies, unless the operator **pins** it in the host's local follower config, by tag
    and digest. A pin is never read from GitHub or from any release.
  - **Cached, verified rollback.** The last-good version's artifacts stay on disk, with
    their digests in the record. Rollback re-checks those digests and needs no network.
  - **Held versions.** A version that failed its checks is held in the record and never
    retried, until the operator clears the hold. A newer version is still considered.
  - **Rollback that fails** leaves an explicit `rollback_failed` state, reported in status
    and metrics, and the follower then does nothing more until an operator acts.
  - **GitHub unavailable** (an outage, a rate limit, or an attestation lookup that fails):
    the follower keeps running the current version, retries with backoff up to once an
    hour, reports the condition, and never installs anything it could not fully verify.
  - **PPE follows the pre-release channel,** with the same follower, so every rc exercises
    the code path production uses before any full release exists. Production follows the
    release channel.
  - **Promotion is a human action.** The maintainer publishes `vX.Y.Z` only after the
    matching rc has passed PPE.
  - **Carried over from the old deferred list:** one captured version used throughout,
    with immutable artifacts and frozen dependencies; and honest wording for a multi-node
    rollout, which is best effort, one node at a time, not coordinated.
  - **Still an open problem:** an automated check, before a full release is allowed, that
    the rc passed PPE. It needs a check whose source can be authenticated (the revision-2
    provenance finding), so it is not designed here.
  - **Order:** S12 publishes the first rc (`v0.1.0-rc.1`) → the follower slice lands and the
    PPE host's follower installs that rc → the maintainer publishes `v0.1.0` → production
    follows it. So `v0.1.0` itself closes the follower slice, not S12.

  Threats: ADR-001 T18 to T20.

### 7.8 Interim deployment, until the follower has passed PPE

Until the release follower has passed PPE on a host, that host deploys the way the
maintainer's existing services do: by following a `ppe` or a `prod` branch, through a
GitHub Actions self-hosted runner. This lets the gateway and sidecars run on real hosts long
before S12. It is temporary.

- **REQ-70. Interim deployment.**
  - **Promotion is a pull request the maintainer approves.** Nobody pushes `ppe` or `prod`
    directly, and no App may bypass their ruleset. GitHub merges pull requests with a merge
    commit (it can't fast-forward), so each promotion adds one merge commit.
  - **The flow, with the maintainer as the gate:**
    1. a branch is deployed to `ppe` for testing;
    2. it is merged to `main`, through the usual review;
    3. `main` is deployed to `ppe`;
    4. a smoke test runs on PPE;
    5. `main` is deployed to `prod`.

    Before approving the `prod` promotion, the maintainer checks that the same `main` commit
    passed step 4; if `main` has moved since, steps 3 and 4 run again. Automating that
    check stays deferred, because it needs a PPE result whose source can be authenticated
    (§7.7).
  - **Sources:**
    - **`ppe`:** only a `promote/ppe-<sha12>` branch of this repository, made by the
      promotion builder (below) from any branch of this repository. **Never a fork.**
    - **`prod`:** **only `main`**, as a plain pull request. `prod` only ever merges `main`
      commits, so its merged tree always equals the `main` commit's.
  - **The promotion builder** (`deploy/promote-ppe.py`, a stdlib Python script using the
    REST API). It exists because a merge into `ppe` would otherwise keep an abandoned
    branch's changes, after which the tree check below fails forever, and the ruleset
    forbids any reset.
    - **What it does,** given a source branch:
      1. reads the source head `S` and the `ppe` tip `T`, and refuses if `S`'s `.github/`
         differs from `main`'s;
      2. creates a commit `P` whose tree is **exactly `S`'s tree**, with `T` as its first
         parent and `S` as its second (`git commit-tree S^{tree} -p T -p S`, done through
         the git-data API);
      3. pushes `promote/ppe-<first 12 hex of S>` at `P`;
      4. opens the pull request into `ppe`, naming `S` and the source branch.
      - **Repeated promotions.** If `promote/ppe-<sha12>` already exists, with an open pull
        request whose commit still has the current `ppe` tip as its first parent, the
        builder reports that pull request and stops. Otherwise it rebuilds the branch on the
        current `ppe` tip, force-updates it (only the App may), and reuses or opens the pull
        request. So promoting A, then B, then A again works.

      While `T` is still the `ppe` tip, `T` is the merge base, so the merge's tree equals
      `S`'s tree. If `ppe` has moved, the promotion is out of date: the ruleset blocks its
      merge, `promotion-source` refuses it, and the builder rebuilds it on the new tip.
    - **Who runs it, as whom.** The dev session runs it locally at the maintainer's request,
      with a short-lived `seriatim-worker` App token from the environment (`GH_TOKEN`). No
      App key sits in Actions secrets.
      - It can't use a workflow's `GITHUB_TOKEN`: branches and pull requests made with it
        trigger no workflows, so `promotion-source` would never run and its required check
        would block the merge.
      - The bot opens the pull request, so the maintainer, who must approve it, is never
        its author.
      - ADR-001 D16 records the choice, and T21 its threat.
  - **The `promotion-source` check** (`.github/workflows/promotion-source.yml`) is required
    on both branches.
    - **How it runs:** on `pull_request_target`, with `permissions: contents: read` and no
      checkout, so it never runs code from the pull request. It reads only the event and
      the REST API.
      - It must be `pull_request_target`: under `pull_request` the workflow file would come
        from the pull request, which could edit it to pass.
      - GitHub runs a `pull_request_target` workflow from the default branch. **`main` stays
        the default branch,** a protected invariant (§7.9). The check fails if the event
        reports another default branch.
    - **What it refuses:**
      - **any base:** a head repository other than this repository;
      - **`prod`:** a head branch other than `main`;
      - **`ppe`:**
        - a head branch not named `promote/ppe-<sha12>`;
        - a head commit that is not a two-parent commit whose tree equals its second
          parent's, with that second parent's first 12 hex characters in the branch name,
          and whose first parent is the **current `ppe` tip**. Only then is the merge's tree
          exactly the source's; a promotion built on an older tip is refused and rebuilt;
        - **a tree that differs from `main`'s under `.github/`.** Workflow changes reach
          `ppe` only through `main`. It compares the `.github` subtree of the head commit with
          `main`'s, through the git-trees API.
    - **When it runs:** on `opened`, `synchronize`, `reopened` and `edited`, so a pull
      request retargeted to another base is checked again.
    - **Event values** reach it only through `if:` or `env:`, never interpolated into `run:`.
    - **Its limits, and why a counterfeit check can't help.** Binding the check to the
      GitHub Actions app keeps other integrations out, but any Actions job named
      `promotion-source` would satisfy it. A pull request's `pull_request` workflows come from
      its own tree, so a counterfeit needs a promotion tree whose `.github/` differs from
      `main`'s. That can't be built:
      - only the `seriatim-worker` App can create or update a `promote/**` branch (ruleset
        `promote-branches`, §7.9), and `ppe` accepts only those branches;
      - the builder refuses a source whose `.github/` differs from `main`'s, before it
        creates anything.

      So every workflow that can run on a `ppe` promotion pull request is one of `main`'s,
      which passed code-owner review. A `prod` promotion's tree is a `main` commit's. Where
      the organisation can require a workflow by rule (an organisation-required workflow),
      that is stronger still, and §7.9 prefers it.
  - **The deploy workflow** (`.github/workflows/deploy.yml`) runs only on `push` to `ppe` or
    `prod`, which only a merged promotion can cause. It never runs on `pull_request`,
    `pull_request_target` or `workflow_run`.
    - **Scope:** `permissions: contents: read`, no repository secrets. It declares the
      environment `ppe` or `prod`, and runs on `[self-hosted, seriatim-ppe]` or
      `[self-hosted, seriatim-prod]`.
    - **It checks out the pushed SHA,** and fails closed unless all of these hold:
      - the pushed commit `M` is a merge commit whose first parent is the branch's previous
        tip;
      - `M`'s tree equals its second parent's tree, so the merge adds nothing of its own;
      - **the merged pull request**, looked up by its merge commit
        (`GET /repos/{repo}/commits/{sha}/pulls`), came from this repository. Its head is
        `M`'s second parent, and its **base equals the pushed branch**, so a pull request
        retargeted after its check is refused. GitHub fills that lookup asynchronously, so
        an empty answer is retried up to 6 times, 10 s apart. It needs no `pull-requests`
        permission only while the repository is public; if it ever becomes private, the
        lookup fails and the job fails closed;
      - **for `ppe`:**
        - the head branch is `promote/ppe-<sha12>`;
        - the head commit `P` has two parents; its first parent is the previous `ppe` tip
          (`M`'s first parent); its tree equals its second parent `S`'s; and `S` matches the
          branch name;
        - `M`'s `.github/` tree equals `origin/main`'s, so a promotion whose check went stale
          while `main` changed its workflows is refused;
      - **for `prod`:** the head branch is `main`, and the head commit is on `origin/main`.
    - **These checks run from the pushed tree,** so they can't defend against a
      `deploy.yml` that was itself changed. The `.github/` rule in `promotion-source`, and
      review on `main`, are what keep that file trustworthy; the runner groups pin only its
      path and ref, not its contents.
    - **It logs the deployed source commit:** `S` for `ppe`, the `main` commit for `prod`.
    - **Every `ppe` deployment is a clean wipe.** Before deploying, it tears down all of the
      PPE project's own resources: its containers, volumes, networks and state, then
      deploys from scratch, so nothing carries over between tests.
      - **Scoped strictly to the PPE project.** The runner's environment names the project
        (`SERIATIM_PPE_COMPOSE_PROJECT`, which must match `^seriatim-ppe(-[a-z0-9]+)*$`, or
        the job fails). The job removes exactly the containers, then the networks, then the
        volumes whose `com.docker.compose.project` label equals that name, and fails if any
        remain. It never loads a Compose file for the wipe: a promoted file could name an
        unrelated volume explicitly (`name:`), and `-p` does not namespace explicit names.
      - **All PPE state lives in that project's volumes,** never in a host path, so the
        wipe needs no file deletion on the host.
      - **Never** anything else on the host, and never `docker system prune` or any other
        prune.
      - **`prod` is never wiped.**
    - **The deploy step** is a no-op until there is a service to deploy. From then on it
      installs with frozen dependencies (`uv sync --frozen`, or the image by digest), runs
      the health and local-only checks, rolls back to the previous deployment if they fail
      (`prod`), and logs what it runs.
    - **No hosts in the repository.** The file is generic. Host-specific values come from
      the runner's own environment.
  - **CI is unchanged.** A `prod` deployment is always a `main` commit. A `ppe` deployment of
    another branch runs only after the maintainer has approved both the pull request and
    the `ppe` environment, and only with `main`'s workflows.
  - **Retirement.** Each host leaves this mechanism as soon as the release follower has
    passed PPE on it. The follower is pull-based and needs no runner, so the runner is then
    removed.

  Threat: ADR-001 T17. The controls are in the next section.

### 7.9 Repository settings

The maintainer applies these settings; each slice's pull request says which rows it adds.

| setting | value | from |
|---|---|---|
| Ruleset `main` | pull request with 1 approval and code-owner review; no force push, no deletion; required checks, each bound to **GitHub Actions** and named by job: `gitleaks` (workflow `secret-scan`; now), then `ci` (S0a part 1), `private-terms` (S0a part 1b), `security` (S0a part 2) and `ppe` (S0b), each added by the PR that introduces it | now, extended per slice |
| Default branch | `main`, and it stays `main`: `pull_request_target` workflows run from it, so changing it would change which `promotion-source` runs. `promotion-source` fails if the event reports another default branch | now |
| Ruleset `deploy-branches` (targets `ppe`, `prod`; already active) | changes by pull request only, with the code owner's approval after the last push (CODEOWNERS has a catch-all `*` entry for the maintainer, so every promotion needs that approval); stale approvals dismissed; threads resolved; merge commits only; no force push, no deletion; an empty bypass list, so no App, `seriatim-worker` included, can bypass it | interim deploy |
| Rulesets `deploy-checks-ppe` and `deploy-checks-prod` | require the check `promotion-source`, bound to the **GitHub Actions** app, so no other integration can report it. **"Require branches to be up to date before merging" is on for `ppe` only** (`deploy-checks-ppe`, strict), so a promotion built on an older `ppe` tip can't merge and is rebuilt instead. `deploy-checks-prod` is not strict: every merge adds a merge commit to `prod` that `main` never has, so a strict rule would leave every later `main` → `prod` pull request behind. Where the organisation supports it, also an organisation-required workflow pinned to `promotion-source.yml` on `main`, which no branch workflow can stand in for | interim deploy |
| Ruleset `promote-branches` (targets `promote/**`) | creation, update and deletion restricted, with only the `seriatim-worker` App as bypass actor, so only the promotion builder makes promotion branches; force-push allowed for the App only, so it can rebuild one | interim deploy |
| Environments `ppe`, `prod` | the maintainer as required reviewer; deployment branches limited to `ppe` and `prod` respectively; no secrets | interim deploy |
| `seriatim-worker` App | `contents: write` and `pull-requests: write`. GitHub can't limit an App to a branch prefix, so its reach is bounded by the rulesets: it can't push `main`, `ppe`, `prod` or a `v*` tag. It is a bypass actor only on `promote-branches`, so that it alone makes promotion branches. It pushes working branches and `promote/ppe-*`, and opens pull requests. Its key never sits in Actions secrets; tokens are minted per use, outside the repository | interim deploy |
| Fork pull request workflows | "Require approval for all external contributors". It does not cover members of the repository or organisation, so it adds to the runner-group limits and does not replace them | now |
| Self-hosted runners | one per host, labelled `seriatim-ppe` or `seriatim-prod`. **Two organisation runner groups**, each limited to this repository: the PPE group only to `thurlow-research/seriatim/.github/workflows/deploy.yml@refs/heads/ppe`, the production group only to `…/deploy.yml@refs/heads/prod`. Labels route jobs; groups authorise them. One group for both refs would let a `ppe` promotion run a job on the production runner. The groups are created once `deploy.yml` exists on those refs. Each runner: a non-administrator service account with no access to unrelated credentials or clones; ephemeral or just-in-time where practical; registered only to its group | interim deploy |
| Tag ruleset `tags-create` (`refs/tags/v*`) | restrict creations; the maintainer is the only bypass actor. A ruleset's bypass list covers all its rules, so this ruleset holds only this rule | S0a; applied |
| Tag ruleset `tags-immutable` (`refs/tags/v*`) | restrict updates and deletions, block force pushes; an **empty** bypass list, so nobody, the maintainer included, can move or delete a published tag | S0a; applied |
| Immutable releases | on, so a published release's assets and tag can't be changed | S0a; applied |
| Environment `release` | the maintainer as required reviewer; deployment limited to `v*` tags; the only environment whose job holds publish permissions (REQ-68) | S0a; applied |
| GHCR package | linked to this repository; write access only from this repository's Actions (the `release` job); no personal access token | S10 |
| Dependabot | `uv` and `github-actions` ecosystems, grouped weekly; never auto-merged | S0a |
| Auto-merge, delete branch on merge | on | now |

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
- **Probe fixtures, not hardware (REQ-16).** Capacity tests replay recorded probe output
  under `tests/fixtures/probes/`, one directory per shape:
  - `nvidia/`:
    - inventories for one and for two devices;
    - an idle GPU (no process rows);
    - Ollama's service tree using memory, and another process using memory;
    - `used_memory` `[N/A]` with `/api/ps` succeeding and failing;
    - a failed and a malformed process query;
    - a transition between the inventories, with and without recovery;
    - Windows full-path process names in mixed case;
    - `CUDA_VISIBLE_DEVICES` given as indices, as UUIDs, as an unmatched entry and as MIG;
  - `apple/`:
    - Metal readable, with the wired limit unset and raised;
    - Metal unreadable, with the wired limit raised and unset;
    - each pressure level, and an unmapped value;
    - a Mac without unified memory;
  - `none/`: no probe applies;
  - `failure/`: a non-zero exit, a timeout and malformed output.

  **What the fixtures contain.** They are recorded from real tools, then sanitised:
  - device names become placeholders (`gpu-0`);
  - serials, UUIDs and PIDs are replaced;
  - each shape is recorded at more than one memory size, so no size is special.

  **Independent expectations.** The expected reports are written by hand from each
  fixture's raw figures, not computed by the probe under test. Every test runs in CI on all
  three operating systems, and none needs a GPU.

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
  - **The score must be ≥ 75 %.** The parser reads mutmut's CI statistics export. A missing,
    malformed or unknown-format export fails the job, and so does one with a non-integer
    field, an interrupted run, or categories that don't add up to its total (unchecked
    mutants).
  - **Nothing to score.** A PR that changes no Python file under `node/` or `gateway/`, or
    whose changed files have no mutable code (mutmut generates no mutants, such as for a
    version-only change), passes with a visible notice. It never computes a score from zero
    mutants. mutmut then exits non-zero and writes no export, so the gate decides "no
    mutants" from mutmut's own per-file records; a failed run that did generate mutants,
    or a missing export after a successful run, still fails.
  - **How it runs** (TD-slice-1 Appendix B): from each package directory, with `only_mutate`
    set to the changed files, and with `tests/` and `contract/` staged beside the mutated
    code, so every test result really comes from a mutant.

### 8.3 HTTP behaviour tests (PPE)

- **REQ-62. A black-box suite.** `tests/behaviour/` is parametrised by base URL, so it can run
  unchanged against a future implementation in another language.
- **REQ-62a. Client compatibility ("is it the standard API?") (slice S10).** A PPE suite
  runs real clients against plain Ollama, a sidecar and the gateway, and compares the
  results.
  - **Clients:** the official `ollama` Python client, and the `openai` Python SDK pointed at
    Ollama's `/v1`. Both are pinned to exact versions in `uv.lock`.
  - **Operations:**
    - with `ollama`: chat and generate, each streamed and not, `embed` (`/api/embed`) and
      the older `embeddings` (`/api/embeddings`), plus `list` (`/api/tags`) and `ps`
      (`/api/ps`);
    - with `openai`: `chat.completions` and `completions`, each streamed and not,
      `embeddings` with `encoding_format="float"` (the SDK asks for base64 by default when
      `numpy` is installed), and `models.list` (`/v1/models`).
  - **Errors, through both clients:** a busy node (429 `GPU_BUSY`), `GPU_OFFLINE`,
    `NODE_OFFLINE`, `MODEL_NOT_ELIGIBLE` and a stream broken by fault injection.
    - **How each is triggered in the two-node PPE:** busy, by holding both nodes' slots;
      `GPU_OFFLINE`, by switching both nodes offline through the gateway; `NODE_OFFLINE`,
      by switching one node offline and calling its sidecar directly; `MODEL_NOT_ELIGIBLE`,
      by a live config reload (REQ-45) that sets `max_params_b = 1` on both nodes and then
      requesting `qwen2.5:1.5b`; a broken stream, through the capture container's fault
      injection. Each client must raise its
    documented exception, and never treat an error record as a finished answer.
    - **No hidden retries.** The `openai` client is built with `max_retries=0`, so a fast
      429 stays fast. The README says so, and says what the default of 2 retries does.
  - **What is pinned:** the Ollama image digest, each model's digest, the execution
    settings (CPU only, `OLLAMA_NUM_PARALLEL=1`, one runner), and plain Ollama as the
    baseline.
  - **Equivalent results.** Generation requests use `temperature: 0` and a fixed `seed`.
    - **Baseline first.** Each generation case runs twice against plain Ollama. Exact
      equality is required only if those two agree. If they don't, the case is reported as
      unstable and fails, so a non-repeatable baseline is never mistaken for a Seriatim
      difference.
    - **Text:** the response text, `done_reason` and finish reason are equal.
    - **Embeddings** (no sampling): equal length, and every component within 1e-6 of the
      baseline's.
    - **Streams:** the concatenated content and the final record's `done_reason` are equal.
      Records may be split differently.
    - **Never compared:** timing fields, `created_at`, `created`, `id`,
      `system_fingerprint`, and token counts that depend on the prompt cache
      (`prompt_eval_count`, `usage.prompt_tokens`).
    - **Bytes.** That Seriatim passes bodies through unchanged is checked by the capture
      tests (P-series), not here.
  - **Listing.** `ollama` parses `/api/tags` and `/api/ps`, and `openai` parses
    `/v1/models`. Each is compared with the baseline:
    - **through a sidecar,** the same entries;
    - **through the gateway,** the merge of REQ-38: one entry per name (`/api/tags`,
      `/v1/models`), or every node's entries (`/api/ps`).
  - **Plain shapes, no extra keys.** Every successful standard response, and every error
    that Ollama itself produced, through a sidecar and through the gateway, validates
    against the plain Ollama or OpenAI shape recorded by S0b from the pinned versions, with
    no keys beyond it. Seriatim's own refusals validate against `ErrorEnvelope` instead,
    and in-stream failures against `StreamErrorRecord`. That is asserted on the raw JSON, because the
    clients ignore unknown keys and would hide them.
  - **Recorded versions.** CI records the Ollama version and both client versions. The
    README states, from that record: "a drop-in Ollama endpoint, verified against Ollama
    x.y with ollama-python a.b and openai c.d".

  *Tests:* CC1–CC18.
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
  - `all-minilm:22m`, a small embedding model, on every node, for the embedding cases of
    REQ-62a;
  - `qwen2.5:1.5b` (1.5B) on both nodes of the two-node PPE. Two is the smallest fleet that
    exercises spill-over; fleets of other sizes are covered by the component tests (REQ-44).

  The PPE nodes have no GPU. Size routing there goes through `max_params_b`, set between
  those sizes and checked against Ollama's real `details.parameter_size`, and through an
  eligible-nodes list. Each sidecar replays a probe fixture (REQ-16), so `/seriatim/nodes`
  shows a capacity report next to the rules. The PPE does not use `ollama cp` aliases, which keep
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
  - **security scanners**, each a required check, under one failure policy:
    - **Fail on findings and on errors.** Every scanner job exits non-zero on a finding at
      or above its threshold. It also fails when the scanner could not run properly: a
      crash, a timeout, a failed download of its vulnerability database, or missing or
      unreadable output. A scanner that did not finish never counts as a pass.
    - **Exceptions are explicit.** A finding may be ignored only through a committed ignore
      file, where each entry gives a reason and an expiry date. CI fails on an entry without
      both, and on an expired one. Inline suppressions such as `# nosec` need a reason on
      the same line, and a lint checks for it.
    - **Pinned tools.** Each scanner is pinned to an exact version and verified by checksum.
    - **The scanners and their thresholds:**
      - **gitleaks** (`secret-scan / gitleaks`): `--exit-code 1` on any finding.
      - **Bandit** (Python SAST): fails on medium or high severity (`-ll`).
      - **pip-audit**, on the locked third-party dependencies (`uv export --frozen
        --no-emit-workspace --format requirements-txt`, then `pip-audit --strict --no-deps
        --disable-pip -r` on that file): `--strict`, so a
        package that cannot be audited fails too. Fails on any known vulnerability.
      - **Trivy**: `trivy fs --scanners vuln,misconfig,secret` on the repository and, from
        S10, `trivy image` on the gateway image. Each runs twice on the same cached
        database:
        - a full report at every severity, with `--exit-code 0`, written to the job summary;
        - the gate, with `--severity HIGH,CRITICAL --exit-code 1`. A failed database download fails the job; the scan is
        never run silently against a stale or empty database.
  - **the private-terms check.**
    - **What it does.** It scans the pull request's changes against a list of private terms
      (host names and personal names) held in a CI secret.
    - **Fork pull requests** get the same check, because the workflow and the secret come
      from `main`. If the secret is unavailable, the check fails; it never passes in that
      case.
    - **It also runs on every push to `main`,** scanning the whole tree.
    - **Injection rules.** It runs under `pull_request_target`, so it must never let the
      pull request control what runs:
      - **Code from `main` only.** The workflow, the scanner script and its config come
        from the tip of the base branch that the event runs on (`GITHUB_SHA`), never from
        the pull request. `github.event.pull_request.base.sha` is used only to bound
        the commits scanned.
      - **Minimal token.** `permissions: contents: read` and nothing else; checkout with
        `persist-credentials: false`. The term list is its only secret, and it reaches the
        scanner through an environment variable, never on a command line or in the log.
      - **A validated head SHA.** `github.event.pull_request.head.sha` must match
        `^[0-9a-f]{40}$`. Exactly that object is fetched, never a branch name, and
        `git cat-file -t` must say `commit`.
      - **What is scanned.** Everything the pull request introduces, across its whole
        commit range, so a term added in one commit and deleted in a later one is still
        found:
        - **A fresh, complete repository.** The job runs `git init --bare` in an empty
          directory and adds the remote `origin` as
          `https://github.com/${{ github.repository }}`: the base repository, from the
          workflow context, never from the pull request's metadata. The repository is
          public, so the fetch needs no credentials, and none are configured. It then runs
          `git fetch --no-tags --no-write-fetch-head origin <base.sha> <head.sha>`. That is a full fetch, with no `--depth` and no
          `--filter`, so every commit and blob is local. It is never run inside the
          checkout of `main`, which may be shallow. `git rev-parse --is-shallow-repository`
          must print `false`.
        - **The commits:** `git rev-list --topo-order <head.sha> --not <base.sha>`, the one
          authoritative list. If it fails, or lists no commit, the check fails.
        - **Every blob introduced** by those commits, including in merges:
          - **The listing.** For each commit,
            `git diff-tree -r -z -m --no-renames --no-textconv --no-ext-diff --root <commit>`
            lists the added and modified blobs. With `-m`, a merge commit is compared with
            each of its parents, so content introduced only while resolving a merge is
            listed too.
          - **Reading.** Blob ids are deduplicated, then read with `git cat-file --batch`,
            as bytes. That includes blobs of files deleted later.
          - **Completeness.** A `missing` answer, a short read or a non-zero exit fails the
            check.
        - **Every path** in those lists.
        - **Every commit's metadata**: its message, and its author and committer names and
          emails.
        - **Git's configuration is neutral.** `GIT_CONFIG_NOSYSTEM=1`,
          `GIT_CONFIG_GLOBAL=/dev/null`, `-c core.hooksPath=/dev/null`, and no textconv,
          external diff or filter drivers, so nothing in the repository's attributes can
          run code or change what is read.
      - **The pull request is data.** It is never checked out into the working tree. It is
        read only as objects, through `cat-file`, so hooks, LFS smudging and submodules never
        come into play.
      - **No shell.** Subprocesses take argument arrays (`shell=False`). Nothing uses
        `shell=True`, `os.system` or string-built commands.
      - **No interpolation.** Values the pull request controls are never interpolated into
        a `run:` script or a `${{ }}` expression inside one: the title, the body, the branch
        name, labels, filenames and commit messages. Filenames are handled as NUL-separated
        bytes.
      - **No cache** is used, so it cannot be poisoned, and the job has a timeout.
    - **What it reports.**
      - **Publicly** (the check result, the job log and the summary): only "Private-terms
        check failed. The maintainer has been notified." No file, line, count or term, and
        the output is the same for one match as for many. Workflow logs and artifacts of a
        public repository are readable by anyone, so they get nothing more.
      - **For the maintainer:** the match locations are encrypted to the maintainer's
        public key (an `age` recipient kept as a repository variable) and uploaded as an
        artifact that only the maintainer can decrypt.
        - **No size signal.** The artifact is uploaded on every run, pass or fail, under a
          fixed name. Its plaintext is padded to a fixed 64 KiB before encryption; a
          longer report is cut short, with a note that says so.
        - **No plaintext leak.** The plaintext exists only in memory and is never written
          to disk or to the log. If encryption or upload fails, the job fails with the same
          generic message.
    - **The accepted trade-off.** Pass or fail is still a one-bit signal. A contributor could
      in principle learn whether one candidate name is on the list by opening a pull request
      that contains it. That is accepted for three reasons:
      - each probe costs a public pull request, which the maintainer sees;
      - each probe tests one guess and reveals nothing else;
      - hiding the result altogether would mean the check cannot block a leak.
  - an IP-literal check (`ip-literals`), over every tracked file, enforcing REQ-51;
  - the PPE suite;
  - a PR-size check (`pr-size`): ≤ 15 changed files (`uv.lock` excluded), and a diff of
    ≤ 150 KB (150,000 bytes: the text diff, plus the full size of every changed binary
    file, because git's binary patches are compressed), so
    that one reviewer can read the whole change.
  - **The contract lint until slice 1.** With no `contract/` directory, `contract-lint`
    passes with a visible notice. Once anything exists under `contract/`, `openapi.yaml`
    must exist and pass.

  **Check names.** Rulesets match a check by its job name. `ci.yml`'s jobs are `lint`,
  `test` (one per operating system), `coverage`, `mutation`, `contract-lint`, `ip-literals`
  and `pr-size`, and its roll-up job **`ci`** fails unless every one of them passed
  (`pr-size` may be skipped on a push to `main`, where there's no pull request). The ruleset
  requires `ci`, not the individual jobs.

  CI runs on every pull request and on every push to `main`. The security
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
| Q10 | Should maintenance update Seriatim? | No. Seriatim is updated by a verified release (§7.6, §7.7), or by the interim deployment (§7.8) until the follower has passed PPE | REQ-55, §7.6 |
| Q11 | Publish the evaluation? | Yes, cleaned, as `docs/EVALUATION.md` | `docs/EVALUATION.md` |

Two points were raised in review and decided without change. Ollama **0.40.16** is the real
version the evaluation ran. Per-mutant timeouts count as killed, and the whole-run timeout
fails closed (REQ-61).

## 10. Open questions

None remain from revision 1. New questions found during implementation are raised in that
slice's TD, with a recommendation, the options and the risks.
