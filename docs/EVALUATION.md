# Ollama gateway evaluation (2026-11-15)

This is a cleaned copy of the evaluation that led to Seriatim. Private names, paths and
commit ids have been removed. "The predecessor" is the unpublished in-house gateway that
Seriatim replaces. Nothing in the evaluation ran on, or connected to, production. The raw
captures stayed in the private test environment and are not published.

**Environment.** A local Docker Compose test environment (PPE) ran two CPU-only Ollama
0.40.16 containers, both serving `qwen2.5:0.5b` under size-named aliases:
- backend A: `14b`;
- backend B: a `14b` mirror and `32b`.

Every published port was bound to `127.0.0.1`, and the sum of the containers' limits was
capped at 4 CPUs and 6 GiB.

**Evidence.**
- "Ollama received" means the raw HTTP request bytes captured by a tcpdump sidecar in each
  backend's network namespace, plus the `context_length` read from `/api/ps` on each backend.
  It never relies on what a proxy reports.
- Each request carried a unique marker in its prompt, so it could be found in the capture.
- Each backend's default context was 2048, so a forwarded `num_ctx` shows up as a change.

## Candidates

| project | licence | activity (2026-11-15) | version tested |
|---|---|---|---|
| the predecessor gateway | unpublished | last change 2026-09-13 | its main branch |
| BerriAI/litellm (proxy) | MIT, except `litellm/enterprise/` (commercial); nothing used here is gated | very active: ≥ 100 commits in 90 days, release 2026-11-14 | image `v1.108.9` |
| thushan/olla | Apache-2.0 | active: ≥ 100 commits in 90 days, v0.1.0 on 2026-11-01 | image `v0.1.0` |
| ParisNeo/lollms_hub (the same repo as `ParisNeo/ollama_proxy_server`, renamed) | Apache-2.0 | stale: no commits in 90 days, last push 2026-04-23 | `eb5a106` (built) |
| BigBIueWhale/ollama_load_balancer | Unlicense | stale: no commits in 90 days, last 2026-02-02 | `54ac01e` (built) |
| jchristn/OllamaFlow → ollamaflow/ollamaflow | MIT | **archived** | excluded |

## Results (baseline configuration)

✅ pass, ❌ fail, ◐ partial.

| | predecessor | LiteLLM | Olla | lollms_hub | ollama_load_balancer |
|---|---|---|---|---|---|
| **1a. Options unchanged, native `/api/chat`** | ◐ `num_ctx` 6144, `top_k`, `seed`, `temperature` and `num_predict` reached A byte for byte (ctx 6144 loaded). But it **rewrites `num_ctx` above its "safe" table**: 30000 → 24576 on a `32b` name (`X-Context-Clamped`) | ❌ no native route (404). Clients must use the OpenAI format | ✅ unchanged, including `num_ctx` 30000 (B loaded ctx 30000) | ✅ unchanged in this test (30000 loaded). The code has a hard-coded "crop" to model limits (32768 if unknown) and drops `num_ctx` on an OOM retry | ✅ raw bytes, unchanged |
| **1b. `num_ctx` on the OpenAI route** | ❌ forwarded raw to Ollama's `/v1`, which ignores it: ctx 2048 loaded for both `options:{num_ctx}` and a top-level `num_ctx` | ◐ a top-level `num_ctx: 5120` became `options.num_ctx` on native `/api/chat` (5120 loaded) ✅. A nested `options:{num_ctx}` arrived as `options.options.num_ctx` and was ignored (2048) ❌ | ❌ raw to `/v1` (2048) | ❌ route off by default; when enabled it returns 500 | ❌ raw to `/v1` (2048) |
| **2. Busy → immediate 429 with `Retry-After`** | ◐ second request to A: **429 in < 10 ms, `Retry-After: 15`**. But the lock is global: a 32B request for idle B also got 429 | ❌ with `max_parallel_requests: 1` per deployment, the second request **queued 30 s** on A (no 429), even though B's 14B mirror was idle. ✅ with a custom hook (see below) | ❌ second request queued 28 s on A. No per-backend limit exists. With the default `server.read_timeout` (30 s), a 32 s non-streaming request was cut off with 502 | ❌ all three requests queued on **B** (24 s); it retries on 429/503 up to 10 times over 600 s | ❌ hard one-per-server, but spills the second request over to the other server; when both are busy it returns **503 without `Retry-After`**. Not configurable |
| **3. Routing ≤ 14B → A, ≥ 32B → B, including a backend down** | ❌ no size routing: a 32B request went to **A** ("model not found", inside an HTTP 200 stream). Failover only on a connection error; with B down, 32B was sent to A | ✅ by config (`order` plus per-model `api_base`). A down → 14B to B (the first request took 14 s while it hit the cooldown). B down → 32B gets an error (500) and is never sent to A | ✅ by discovery (`/api/tags`) plus `priority`. A down → B; B down → 32B gets 503 `model_unavailable`, never A | ❌ with both up, 14B went to **B** (no priority). B down → 32B gets 503 after 11 s ✅. A down → B ✅ | ❌ not model-aware: 32B went to A (404). A down backend → 502 to the client, no retry |
| **4. Bearer-key auth** | ✅ 401 without a key or with a wrong one; 200 with the key, on inference and on `/api/tags` | ✅ master key: 401 without, 400 with a wrong key | ❌ none (documented; needs a proxy in front) | ✅ 401/401/200. ⚠ its `SECRET_KEY` is also accepted as an admin bearer token, and a default value ships in the repo | ❌ none; the client's `Authorization` header is forwarded to Ollama |
| **5. GPU-memory awareness (docs and code only)** | ✅ `nvidia-smi` free and used VRAM (5 s cache), falling back to `/api/ps` `size_vram`. Not used to gate requests | ❌ none | ❌ none | ◐ `/api/ps` only, in the `least_loaded` strategy; the dashboard hard-codes 24 GB | ❌ none |
| **6. Native `/api/tags`, `/api/ps` and `/api/version`** | ◐ all three identical to backend A (primary only; B not shown) | ❌ 404. Only reachable through `pass_through_endpoints` to one fixed target | ◐ under `/olla/ollama/`: tags built from its own registry (unified by digest, so it showed 1 of 3 aliases here, an artefact of the shared blob); ps merged from both with an `olla_endpoint` field; version forwarded ✅ | ❌ tags aggregated (plus synthetic `auto` and `lollms` entries); `/api/ps` and `/api/version` return 200 with a 503 error inside the body | ◐ raw, from whichever server is free |
| **Setup effort** | low | low–medium: one YAML file, 1.8 GB image | low: one YAML file, 42 MB image | **high**: the upstream Dockerfile produced an image that could not boot (3 fixes needed); servers and keys only through its admin UI or database | low (Rust build); CLI flags only |

**Can queueing be turned off?**

| gateway | answer |
|---|---|
| predecessor | It never queues |
| LiteLLM | Its docs say there is no wait queue, and to set `num_retries: 0`. Both were configured, yet requests still waited |
| Olla | No setting for it |
| lollms_hub | `max_retries` controls its own retries; Ollama's internal queue still applies |
| ollama_load_balancer | It never queues |

## Extension paths

If a candidate were adopted, three gaps would remain:
- **(a)** routing by size to each backend;
- **(b)** one request per backend, with an immediate 429 and `Retry-After`;
- **(c)** a `/models/select` helper.

| | (a) routing | (b) one per backend + 429 | (c) `/models/select` | code we would own |
|---|---|---|---|---|
| **LiteLLM** | config: `model_list` with `api_base` and `order` ✅ tested | hook: `CustomLogger.async_filter_deployments` plus success and failure events, raising `RouterRateLimitError(cooldown_time=N)` so the proxy sets `retry-after`. ✅ tested: 429 in 30 ms with `Retry-After: 15`, and B's 32B served at the same time. ◐ **Known gap:** in v1.108.9, failure events from a dead backend carry no deployment id, call id or `api_base`, so that backend's slot stays held until a reaper frees it | a layer plus config: a selector add-on exposed through `pass_through_endpoints` with `auth: true` ✅ tested | about 60 lines (hook) + 20 (gap fix) + 45 (add-on) + 25 (YAML). Native `/api/chat` is still missing, and existing clients call it, so it would need a ~100-line translation layer or client changes |
| **Olla** | config: discovery plus `priority` ✅ (the best of all candidates) | an upstream PR (Go: a per-endpoint `max_concurrency` plus 429 when saturated; 150–250 lines), or a ~120-line Python layer in front, which is essentially the predecessor's lock | a ~45-line layer | about 165 lines, plus an auth layer |
| **lollms_hub** | a PR: no priority or pinning order | a PR (~100 lines): replace retry-on-busy with a hard per-server cap and 429 | a ~45-line layer | about 150 lines in a fork, plus carrying the Dockerfile fixes, the OpenAI-route crash and the default-secret issue. Stale |
| **ollama_load_balancer** | a PR (~100 lines of Rust) or a layer | a PR of about 3 lines (`SERVICE_UNAVAILABLE` → `TOO_MANY_REQUESTS` + `Retry-After`); it still spills over instead of rejecting | a ~45-line layer | about 150 lines, plus an auth layer; stale, single maintainer |
| *predecessor (for comparison)* | about 80 lines | about 30 lines (one lock per backend) | already built | about 150 lines in total, plus ~40 for `/v1` `num_ctx` and making the clamp optional |

## Recommendation (as made on 2026-11-15)

Keep an in-house gateway and publish it, with this table as the reason it exists.

**Why not adopt a candidate:**
- **No candidate meets requirements 2 and 4 together without custom code.** Busy-means-429 is
  the reason the gateway exists. Only LiteLLM gets there, and only with a custom hook that
  still leaks slots when a backend dies.
- **LiteLLM** (the strongest on routing and auth) has no native Ollama API. Existing clients
  call `/api/chat`, so adopting it would mean a ~100-line translation layer plus the hook plus
  the add-on. That is about 250 lines on a 1.8 GB, fast-moving dependency.
- **Olla** passes options through and routes best, but has no auth and no concurrency cap. The
  layer we would put in front of it *is* the gate. It is the runner-up if we later want to hand
  off health checks and model discovery.
- **lollms_hub** and **ollama_load_balancer** are stale. They failed routing and the busy
  semantics, and lollms_hub has a default-key bypass.

**What the predecessor must fix before publishing:**
- **(3)** It has no size routing: a 32B request goes to A.
- **(2)** Its single lock blocks B while A is busy.
- **(1b)** On the OpenAI route, `num_ctx` has no effect.
- **(1a)** The `num_ctx` clamp silently changes the caller's options.

GPU-memory awareness (5) is unique to the predecessor.

Seriatim's own design ([ADR-001](adr/ADR-001-architecture.md)) builds on this result. The
lock moves into a sidecar on each GPU machine. The router becomes a thin gateway. The clamp
is replaced by explicit rejection.
