# Seriatim — Specification

**Status:** revision 4, for review (Phase 1). **Date:** 2026-11-16.

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
| F4 | `/api/tags`, `/api/ps` and `/api/version` showed only one machine | Combined across machines (REQ-38) |

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
   operator has made eligible for the model, and shows a combined view of every machine.
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

### 4.3 Sidecar: status, health, capacity, admin switch and exposure (slice S4, unless noted)

- **REQ-16. Capacity, measured and reported (slice S4).** `GET /seriatim/status` reports a
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
    - from S4, the exposure check has passed;
    - from S4, the node is not offline by the admin switch (REQ-19a).
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
- **REQ-19a. Admin offline/online switch (slice S4).** The operator can take a node out of
  service on purpose, and the sidecar owns that state.
  - **Routes**, both in the `admin` scope:
    - `POST /seriatim/admin/offline`, with an optional JSON body `{"reason": "…"}`. The
      reason is at most 200 printable characters; anything else gets 400
      `BAD_REQUEST_BODY`.
    - `POST /seriatim/admin/online`, with no body. A non-empty body gets 400
      `BAD_REQUEST_BODY`.
    - **The reason.** No body, `{}` and `{"reason": null}` all mean no reason. A reason is
      a string of 1 to 200 characters for which Python's `str.isprintable()` is true, so no
      control characters or line breaks.

    Both return 200 with the resulting state (contract `AdminState`).
    - **Going offline** sets `offline: true`, the reason, `since` (now) and `changed_by`.
    - **Going online** sets `offline: false` and `reason: null`, with `since` and
      `changed_by` recording that change.
    - **On a first start,** with no file, every field but `offline: false` and
      `revision: 0` is `null`.
    - **Idempotent.** Repeating the current state keeps `since`. A repeated `offline`
      replaces the reason and `changed_by`. A call that changes nothing still increments
      `revision` only if it changed a field.
  - **While offline:**
    - **A new gated request** gets 503 `NODE_OFFLINE` (`retryable: false`,
      `replay_safe: true`). Ollama is not contacted.
    - **A request already admitted** runs to completion. Going offline never aborts work in
      progress, and its slot ends and drains as usual (REQ-10).
    - **Ungated requests** (`/api/tags`, `/api/ps`, `/api/version`, `/api/show`) and admin
      requests are still served, so the operator can inspect and maintain the node.
  - **One commit point.** A change and admission are serialised by the gate's own lock:
    - **The order.** The change is persisted (below), then published to memory, all while
      that lock is held, and the 200 is sent only after it is released.
    - **What it guarantees.** A gated request that took a slot before that point may
      finish. One that asks for a slot after it gets `NODE_OFFLINE`. So no request reaches
      Ollama after an `offline` call has returned 200.
    - **Concurrent calls** are serialised the same way. Memory and disk always end in the
      state of the last call to commit.
  - **A revision number.** Every committed change increments `revision`, a persisted
    integer, so an observer can tell a newer state from an older one.
  - **Precedence of refusals** for a new gated request: `NODE_OFFLINE`, then the exposure
    codes (`OLLAMA_EXPOSED`, `EXPOSURE_UNVERIFIED`), then `NODE_STARTING`, then busy. So an
    offline node that restarts says it is offline, which is not retryable, rather than
    starting, which is.
    - **Independent of slots.** The admin state is separate from slot state. `online` never
      shortens the startup quarantine, a drain or a held slot. `offline` never frees or
      aborts one.
  - **Visible.** `/seriatim/status` reports `admin_state` (offline, reason, since,
    revision, and the key id that changed it), and health reports `offline` (REQ-18).
    Health carries no reason text: it is unauthenticated, and the reason is operator
    text (T12). Every change is logged
    at `WARNING`, with the key id, the reason and the previous state.
  - **Gateway or not.** The switch also applies to clients that call the sidecar directly,
    because the sidecar holds the lock.
  - **It survives a restart.** The state is kept in `node-state.json`, in the same fixed,
    system-wide directory as the singleton lock (REQ-12b). On POSIX it has mode 0600. On
    Windows the directory's ACL (REQ-57) protects it, because a file mode means nothing
    there.
    - **Writes are atomic and durable.** A temporary file with a unique name
      (`node-state.json.<pid>.<random>.tmp`) in the same directory is written and fsynced, then renamed over the old one, then made durable:
      - **POSIX:** `os.replace`, then an fsync of the directory.
      - **Windows:** `MoveFileExW` with `MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH`,
        which returns only when the move is on disk.
    - **A failure before the rename** gets 500 `INTERNAL`. Neither the file nor memory
      changes, and the temporary file is removed.
    - **A failure after the rename** (the directory fsync) gets 500 `INTERNAL` too. The
      file may already hold the new state, so memory takes the new state as well, and an
      `ERROR` says the change may not survive a power loss. Re-sending the same call is
      safe, because it is idempotent.
    - **At startup** the sidecar reads the file before it admits anything. A missing file
      means online (a first start).
    - **A file that cannot be read or parsed** means **offline**, with the reason
      `state file unreadable`, and an `ERROR` is logged. Only an explicit `online` call
      brings the node back, and that call rewrites the file.
    - **A reboot during maintenance** therefore never brings a GPU back into service.

  *Tests:* AS1–AS12:
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
  - an unreadable file starts the node offline;
  - online restores it;
  - a non-admin key gets 403, and a reason that is too long gets 400.

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
  - `POST /seriatim/admin/nodes/{id}/offline` and `.../online` (REQ-42a);
  - `GET /` and `HEAD /`, which answer 200 `Ollama is running` when at least one node is
    healthy, and 503 `GPU_OFFLINE` otherwise.

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
    3. **`max_params_b` on the node.** Eligible when the model's parameter count is at most
       `max_params_b`.
    4. **None of these.** The node has **no size policy** for this model. It is **excluded**,
       and Seriatim never guesses.

    Setting both `capacity = "unlimited"` and `max_params_b` on one node is a configuration
    error. An eligible-nodes list that names an unknown node is also a configuration error.
  - **Parameter counts are per node.** Each node is judged against the model as that node's
    own `/api/tags` describes it, because two nodes can hold different models under one name.
    - **`size_overrides` first.** A table of the model name, normalised with Ollama's
      `:latest` default, to a positive, finite number of billions of parameters, such as
      `"mixtral:8x7b" = 46.7`. It applies to that name on every node.
    - **Override errors.** Two keys that normalise to the same name, or a value that is not
      positive and finite, are configuration errors.
    - **Otherwise `details.parameter_size`,** parsed as `^([0-9]+(\.[0-9]+)?)([MB])$` (M = 10⁶,
      B = 10⁹). Any other form, such as `8x7B`, is unknown unless `size_overrides` gives
      it.
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
    - `not_eligible` (not in the model's list, or `max_params_b` too small);
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
    - **for an `offline` node,** the value from its last successful discovery, even if
      later attempts failed. A node taken offline and rebooted for maintenance therefore
      doesn't turn every unknown model into `DISCOVERY_INCOMPLETE`.

    **The rules are also applied to `offline` and `unhealthy` nodes**, on their last known
    metadata, to give a `rule_verdict` (`eligible`, `not_eligible`, `no_size_policy` or
    `size_unknown`). It is never used to route, only to choose the error.

    The error order below is defined on `lists_model`, not on the verdict. So an
    unreachable node, whose verdict is `unhealthy` and whose `lists_model` is `unknown`,
    still makes a model that no other node lists `DISCOVERY_INCOMPLETE`, as REQ-34
    requires.
  - **Candidates** are the nodes whose verdict is `eligible`, in configured priority order.
  - **When there are no candidates,** the first of these that applies is returned, so each
    case has exactly one answer:
    1. **`DISCOVERY_INCOMPLETE` (503, `Retry-After`).** No node has `lists_model: true`,
       and some node has `lists_model: unknown`.
    2. **`MODEL_NOT_FOUND` (404).** Every node has `lists_model: false`.
    3. **`GPU_OFFLINE` (503).** Some node with `lists_model: true` is `unhealthy` or
       `offline` and has `rule_verdict: eligible`, and no node is a candidate. The model
       could run there once the node is back. Existing clients already handle this code (ruling Q7). The verdicts in
       `nodes` tell an admin switch apart from a failure.
    4. **`SIZE_POLICY_MISSING` (503, `retryable: false`).** Some node that lists the model
       has `no_size_policy` as its verdict or its `rule_verdict`. The operator has to add a rule; retrying won't help.
       The `hint` names the node and the three ways to fix it.
    5. **`MODEL_SIZE_UNKNOWN` (422).** Some node that lists the model has `size_unknown` as
       its verdict or its `rule_verdict`. `size_overrides` fixes it.
    6. **`MODEL_NOT_ELIGIBLE` (422).** Every node that lists the model has `not_eligible` as
       its verdict or its `rule_verdict`. No node could run it, even with every node back.

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
  - If a node is unreachable, or answered with something that cannot be parsed, its state is
    unknown. A model not found on the others then gets 503 `DISCOVERY_INCOMPLETE` with
    `Retry-After`.
- **REQ-35. No healthy node.** If a node that lists the model and whose rules admit it
  is `unhealthy` or `offline`, and no node is a candidate (REQ-32, case 3), the request gets 503
  `GPU_OFFLINE` with `retryable: false` and `Retry-After`. The code and the
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
    node id tried>` and `X-Reroute-Reason`, with the same values as the metric's `reason`.
    Nodes skipped without being tried are not reported there.
- **REQ-37. Node failure.**
  - Only `replay_safe: true` failures may be retried on another candidate: a failed connect to
    the node, an admission 429, `NODE_STARTING`, `NODE_SATURATED`, `EXPOSURE_UNVERIFIED`,
    `OLLAMA_EXPOSED` or `NODE_OFFLINE`. A `NODE_OFFLINE` answer marks that node `offline`
    until its next health check, and never marks it down. Only `UPSTREAM_UNAVAILABLE` and a failed connect mark a node down;
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
  - `GET /api/tags` returns the union of all nodes' model lists, one entry per model name.
    - **The entry** is the highest-priority node's, unchanged, including both `name` and
      `model`, plus `seriatim_nodes`: every node that lists the name, in priority order.
    - **Different models under one name.** If the nodes' digests differ, the entry also
      carries `seriatim_digests: {node id: digest}`, so the difference is visible.
  - `GET /api/ps` returns every node's list, and each entry carries `seriatim_node`.
  - `GET /api/version` returns the lowest version, plus `seriatim_nodes: {id: version}`.
  - `GET /v1/models` returns the union of the nodes' `/v1/models` lists, in OpenAI's list
    shape, one entry per model id, with no added fields. `created` is the latest of the
    nodes' values.
  - If some nodes did not answer, the response carries `seriatim_partial` and the
    `X-Seriatim-Partial` header. If none answered, it is 503 `GPU_OFFLINE`.
  - The schemas are in the contract (F4).
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
  - `/seriatim/nodes` (authenticated) returns, for each node:
    - its id, health, priority, last check time and status;
    - **its configured eligibility:** `size_policy` (`unlimited`, `max_params_b` or `none`),
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
      committed. The gateway refreshes that node's state at once, and the error says so.
      Re-sending is still safe, because the call is idempotent;
    - the sidecar's HTTP error, unchanged, when the sidecar answers with one;
    - 502 `UPSTREAM_PROTOCOL_ERROR` when a 200 body is not a valid `AdminState`.

    It never retries on another node.
  - **No state of its own.** The sidecar is the source of truth, and the gateway persists
    nothing.
    - **What it keeps** is an in-memory observation of each node's `AdminState`, with the
      time it was read.
    - **Where it comes from:**
      - the sidecar's health and its authenticated `/seriatim/status`, both read every
        15 s, including while health is degraded;
      - a confirmed admin call, applied at once, so routing changes before the next read;
      - a `NODE_OFFLINE` answer, which marks the node offline until the next status read.
    - **Unreachable while offline.** A node that stops answering keeps its last observed
      `AdminState`, so a node rebooted during maintenance stays `offline`, not `unhealthy`,
      until a status read shows otherwise.
    - **Ordering.** An observation with a lower `revision` than the one held is ignored, so
      a slow health check never overwrites a newer confirmation. A sidecar restart keeps
      `revision`, because it is persisted.
  - **Routing.** An offline node is out of routing (verdict `offline`, REQ-32), so requests
    spill over to the other eligible nodes.
  - **Visible.** `/seriatim/nodes` shows each node's `admin_state`: offline, reason, since.

  *Tests:* GA1–GA8:
  - the gateway forwards the call and reports success only after the sidecar confirms;
  - an unreachable sidecar gives 502, and the node's state is unchanged in the view;
  - a timeout after the sidecar committed gives 502 with the outcome unknown, and the next
    status read shows the committed state;
  - an older observation never replaces a newer `revision`;
  - an unknown id gives 404;
  - requests fail over around an offline node;
  - online restores routing;
  - a non-admin key gets 403.
- **REQ-43. Compatibility (ruling Q7).** The headers and fields existing clients read keep
  their names. §7.1 documents the full compatibility surface.
- **REQ-44. The fleet is configuration.**
  - **One place.** The gateway's configuration file is the only description of the fleet.
    - **Per node:** `id`, `url`, keys and `priority`, plus an optional size policy:
      `max_params_b`, or `capacity = "unlimited"`.
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

### 4.5a Monitoring (sidecar in S4, gateway in S8)

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
    - The file is `seriatim_node.prom` on a node and `seriatim_gateway.prom` on the gateway
      host. In the gateway's container image, the directory is a mounted volume.
  - **Writing.** Every 15 s, and at shutdown, the file is written atomically: a temporary
    file in the same directory, then a rename. The collector therefore never reads half a
    file.
    - **The temporary name** is `.seriatim_node.prom.tmp` (or `.seriatim_gateway.prom.tmp`).
      It doesn't end in `.prom`, so the collector never reads it.
    - **No sample timestamps.** `node_exporter` rejects a whole file that has them.
      Freshness is the value of `metrics_timestamp_seconds`.
    - **On Windows** a reader can hold the file open. The rename is retried up to 5 times,
      100 ms apart. If it still fails, the old, complete file stays, and the failure is
      logged.
    - **Mode** 0644, because the exporter must read it. On Windows, the file grants read to
      the exporter's service account.
    - **In a container,** the directory must be a bind mount of the host's collector
      directory, writable by the container's user. A named volume the exporter can't see is
      a configuration error, and the startup check reports it.
    - **A failed write** is logged at `WARNING` at most once a minute, and never affects
      serving.
  - **What the file must not contain:** keys, key hashes, prompts, request bodies or
    addresses. Labels may carry only configured node ids, device indices, error codes, the
    Seriatim version, and the fixed values listed below for each label.
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
    - `admissions_total{result}`: `acquired`, `busy`, `refused`. `refused` covers a
      request turned away by `NODE_STARTING`, `NODE_OFFLINE`, `OLLAMA_EXPOSED` or
      `EXPOSURE_UNVERIFIED`;
    - `requests_total{kind, status}`: `kind` is `gated`, `ungated` or `seriatim`;
      `status` is the HTTP status class (`2xx`, `4xx`, `5xx`);
    - `errors_total{error_code}`, for every code in the contract;
    - `hold_seconds`: a histogram of gated slot hold times, with buckets 1, 5, 15, 60,
      300, 900 and 3600 s;
    - `drains_total{cause}`: `disconnect`, `deadline`, `upstream_error`, `cancel`;
    - `admin_offline` (0 or 1), `starting` (0 or 1);
    - `exposure{state}`: 1 for the current state of `local_only`, `exposed` or
      `unverified`;
    - `ollama_reachable` (0 or 1);
    - **capacity, from REQ-16:** `capacity_usable_bytes`, `capacity_source{source}` and
      `device_memory_bytes{index, kind}`, where `kind` is `total`, `free` or `usable`. A
      figure that is `null` in the report is left out of the file, never written as 0.
  - **Gateway metrics** (prefix `seriatim_gw_`):
    - `up`, `build_info{version}`, `metrics_timestamp_seconds`;
    - **per node:** `node_healthy{node}` and `node_offline{node}`;
    - `routed_total{node, outcome}`, where `outcome` is `served`, `spilled` or `failed`;
    - `spillovers_total{from_node, reason}`: a tried node answered with a replay-safe
      refusal (REQ-37) and the next was tried. `reason` is `busy`, `offline`, `starting`,
      `saturated`, `exposure` or `unreachable`. Skipping a node already known to be offline
      or down is not a spill-over, and is counted in `skipped_total{node, verdict}`;
    - `routing_errors_total{error_code}`, for the REQ-32 errors;
    - `config_reloads_total{result}` (`applied` or `rejected`) and
      `config_last_applied_timestamp_seconds`.
  - **Counters reset** when the process restarts, as Prometheus expects.
  - **The install scripts** (§7.3, §7.4) set the directory when an exporter with a textfile
    collector is present, and never install or reconfigure the exporter. They read the
    directory from the exporter's own configuration (`--collector.textfile.directory`, or
    `windows_exporter`'s `--collector.textfile.directories`), never assume a path.

  *Tests:* MX1–MX10:
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
| Body cannot be parsed, or `model` missing or not a string | 400 `BAD_REQUEST_BODY`, when a check needs the body | 400 `BAD_REQUEST_BODY` before routing (REQ-32); no node contacted |
| GPU probe fails / `/api/ps` fails | `source: unavailable`, with an error / `loaded_models: null`, with an error | Shown in `/seriatim/nodes`. Routing is unaffected, because it uses the configured rules (REQ-32) |
| Ollama reachable from the LAN | 503 `OLLAMA_EXPOSED` on gated routes; health degraded | Node unhealthy |
| Exposure check uncertain | 503 `EXPOSURE_UNVERIFIED`; never treated as safe | Tries the next node |
| Invalid or unknown config key | Exit code 2, naming the key | The same |
| No keys configured | Refuses to start (REQ-15) | The same |
| Model missing / node state unknown / no healthy node / size unknown | — | 404 `MODEL_NOT_FOUND` / 503 `DISCOVERY_INCOMPLETE` / 503 `GPU_OFFLINE` / 422 `MODEL_SIZE_UNKNOWN` |
| Node switched offline by the operator | 503 `NODE_OFFLINE` for new gated requests; in-flight requests finish; state persists across restarts | Node out of routing (verdict `offline`); spill-over to other eligible nodes; `GPU_OFFLINE` if none is left |
| No eligible node / a node with no size policy | — | 422 `MODEL_NOT_ELIGIBLE` / 503 `SIZE_POLICY_MISSING`, in the order of REQ-32, with every node's verdict; never sent to a node outside the rules |
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
  - It does **not** update Seriatim (ruling Q10). Seriatim changes only through a manual
    install of a tagged release (§7.6).
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
  - if `windows_exporter` is installed with its textfile collector, reads its directory from
    the service's `--collector.textfile.directories`, sets
    `SERIATIM_NODE_METRICS_TEXTFILE_DIR`, and grants the sidecar's service account modify
    rights on that directory (REQ-67). It never changes the exporter;
  - installs Python 3.12+ and a tagged release into a venv under `%ProgramFiles%\seriatim`;
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
  - installs a tagged release into a venv under `/usr/local/seriatim`;
  - puts the config in `/usr/local/etc/seriatim/` (mode 0600);
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

### 7.6 Deferred: automated promotion and deployment

Phase 1 covers the gate and CI. **Deploys are manual** until a later slice designs automated
promotion and deployment. Until then, a node or the gateway is upgraded by installing a tagged
release by hand (§7.3 to §7.5).

That slice starts from these open problems, recorded as the revision-2 review stated them.
They are not designed here.

- **Blocking, PPE→prod bypass:** a single promotion App key in both `ppe` and `prod`, with
  bypass on both rulesets, gives a compromised PPE job prod push authority. Use separate
  identities; the prod credential is reachable only after prod approval.
- **Blocking, unauthenticated PPE status:** `seriatim/ppe` is checked by name only. Bind it
  to the reporter App (an expected source), and verify that identity during promotion.
- **Should-fix:**
  - bind the verified SHA to the installed bytes: an immutable checkout, frozen or
    hash-pinned deps, and the same check on first install;
  - verify rollback health, persist the last-good deployment and its config, and define a
    rollback-failed state;
  - either coordinate the one-node-at-a-time rollout or call it best effort.

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
  - **The score must be ≥ 75 %.** The parser reads mutmut's CI statistics export; a missing,
    malformed or unknown-format export fails the job.

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
  - **Errors, through both clients:** a busy node (429 `GPU_BUSY`), `GPU_OFFLINE`, a
    `MODEL_NOT_ELIGIBLE` and a stream broken by fault injection. Each client must raise its
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
    - **through the gateway,** the union over the nodes (`/api/tags`, `/v1/models`), or the
      multiset of every node's entries (`/api/ps`).

    The extra fields `seriatim_nodes` and `seriatim_node` (REQ-38) are checked through raw
    HTTP. "Accepted" means `ollama` parses the response without error; its models ignore
    unknown fields. `openai` never sees them.
  - **Recorded versions.** CI records the Ollama version and both client versions. The
    README states, from that record: "a drop-in Ollama endpoint, verified against Ollama
    x.y with ollama-python a.b and openai c.d".

  *Tests:* CC1–CC16.
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
  - an IP-literal check;
  - the PPE suite;
  - a PR-size check: ≤ 15 changed files (lock files excluded), and a diff of ≤ 150 KB, so
    that one reviewer can read the whole change.

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
| Q10 | Should maintenance update Seriatim? | No. Seriatim is updated by a manual install of a tagged release until automated deployment is designed (§7.6) | REQ-55, §7.6 |
| Q11 | Publish the evaluation? | Yes, cleaned, as `docs/EVALUATION.md` | `docs/EVALUATION.md` |

Two points were raised in review and decided without change. Ollama **0.40.16** is the real
version the evaluation ran. Per-mutant timeouts count as killed, and the whole-run timeout
fails closed (REQ-61).

## 10. Open questions

None remain from revision 1. New questions found during implementation are raised in that
slice's TD, with a recommendation, the options and the risks.
