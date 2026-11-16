# Seriatim

**One request at a time per GPU, routed across a home lab's machines.**

Seriatim sits in front of [Ollama](https://ollama.com) on a small fleet of GPU machines. It
makes sure each machine works on one inference request at a time, and it sends each request
only to a machine you have said can hold the model. A machine that is busy says so at once, and the
request moves on to another machine that can take it.

> **Status:** design phase. There is no runnable code yet. The requirements, architecture and
> first-slice design are under review in [`docs/`](docs/).

## Who it is for

Seriatim is built for **home labs and small teams**. These usually have a handful of GPU
machines that were never bought as a matched set. Each machine has a different amount of
video memory, and the set changes over time: a card gets upgraded, a machine is added, an
old one is retired. A lab might have, for example:
- a desktop with one consumer card;
- an older card in a second box;
- a workstation with a large pool of unified memory.

Several people, scripts or agents share these machines. Each machine runs Ollama. Seriatim
makes no assumption about how many machines there are, which operating system they run, or
whose GPUs they have.

It is not a cluster scheduler, and it does not need Kubernetes, a message queue or a database.
Each GPU machine runs one small Python process, and one more process acts as the front door.

## The problem

Ollama on a single machine is easy. A shared set of uneven machines goes wrong in three ways:

- **Two requests on one GPU slow each other down.** When a second request arrives while a
  large model is generating, the two compete for video memory. Ollama either queues the second
  one out of sight, so clients time out with no explanation, or pushes layers onto the CPU.
  Either way both requests get much slower. On a card that a model nearly fills, two at once
  can take longer than two in a row.
- **Models land on machines too small for them.** A client asks for a 32B model, and nothing
  stops that request going to the machine with the smallest card. The model then runs mostly
  on the CPU, or fails.
- **Idle machines sit next to busy ones.** Without a view across the fleet, clients pile onto
  the machine they know about while another machine that could serve the same model does
  nothing.

## Your fleet is configuration

The gateway's configuration file lists the machines in the fleet, and says which models each
one may run. Each machine is one entry (the keys each entry also needs are left out here,
for brevity):

```toml
[[nodes]]
id = "node-a"                       # any label you like
url = "http://node-a.example:11434"
priority = 1                        # lower is tried first
max_params_b = 14                   # may run models up to 14B parameters

[[nodes]]
id = "node-b"
url = "http://node-b.example:11434"
priority = 2
capacity = "unlimited"              # no size check (for example a CPU-only box)

[[nodes]]
id = "node-c"
url = "http://node-c.example:11434"
priority = 3
capacity = "listed_only"            # only the models whose eligible_nodes name it

[models."llama3.1:70b"]
eligible_nodes = ["node-c"]         # this model runs only here
```

**Each machine needs a rule** for the models it should serve: `max_params_b`,
`capacity = "unlimited"`, `capacity = "listed_only"`, or a place in a model's
`eligible_nodes` list. A machine with no rule for a model gets none of it, and the gateway
says why, naming the rule to add. It never guesses.

**You set the numbers from real data.** Each machine's sidecar reports how much GPU memory
Ollama can use: on NVIDIA, the free memory plus what Ollama already holds, from
`nvidia-smi`; on Apple Silicon, the GPU memory budget macOS sets, which does not subtract
what other apps are using. The gateway's node list shows that next to your rules. The list also shows how much of each
loaded model is really in GPU memory, so a model that spilled onto the CPU is easy to spot.
Choosing a machine automatically from those measurements is planned as a later slice, once
it has been calibrated on real machines.

Nothing in the code counts machines, or knows their sizes or platforms. To change the fleet,
you edit that file:
- **Grow.** Install Ollama and the sidecar on the new machine, then add an entry with its
  rule. The gateway checks the new machine's health and models before it routes anything to
  it.
- **Shrink.** Remove the entry. No new work goes to that machine, and requests already
  running there finish normally.
- **Change.** After a card upgrade, check the new measured memory in the node list and
  raise `max_params_b`, or edit an `eligible_nodes` list. Change `priority` to prefer a
  different machine. The next routing decision uses the new values.
- **Models.** Pull or delete models on any machine as usual. The gateway rediscovers each
  machine's models every minute, and at once when a model it was asked for is missing.

The gateway picks up an edited file without a restart. A file that fails validation is
rejected with a logged error, and the gateway keeps the configuration it already has.

**Taking a machine out for maintenance** doesn't need a config edit. An admin call takes it
offline:

```
POST /seriatim/admin/nodes/node-a/offline   {"reason": "driver update"}
POST /seriatim/admin/nodes/node-a/online
```

Work already running there finishes, and no new work is admitted; it goes to the other
machines. The setting is held by the machine's own sidecar, in a state directory on disk, so
it survives a reboot. If that state is ever lost or damaged, the machine starts offline
rather than back in service, until someone brings it online. Clients that talk to that
machine directly are refused too.

## How Seriatim handles it

```
 clients ──▶ gateway ──▶ sidecar ──▶ Ollama      (machine A)
                   │
                   ├──▶ sidecar ──▶ Ollama      (machine B)
                   └──▶ sidecar ──▶ Ollama      (machine C, …)
```

- **One at a time, enforced at the machine.** A small **sidecar** runs on each GPU machine,
  in front of Ollama. It admits one inference request at a time (configurable). Ollama is
  reachable only through the sidecar, so nothing slips past it.
- **Busy means an immediate answer.** A request that arrives while the machine is working
  gets HTTP 429 with `Retry-After` straight away, without Ollama ever seeing it. It never waits in a hidden queue, so a
  client is never stuck behind someone else's long generation without knowing why.
- **Routing by your rules.** The **gateway** sends a model only to a machine whose rule
  allows it, using the model's size from Ollama's own metadata. A model never goes to a
  machine you haven't allowed it on, even when that machine is idle and the others are busy.
- **No rule means no.** A machine with no rule for a model gets none of it, and the error
  lists every machine with the reason, so you know what to add.
- **Spill-over to keep every machine working.** When a machine answers busy, or refuses
  before starting the work (offline, starting, or a `num_ctx` above its limit), the gateway
  tries the next machine that can serve the same model, in your priority order. Each one is
  tried once, and nothing waits. If none of them takes it, the client gets one answer: a 429
  with the shortest `Retry-After` if any machine was busy; otherwise a refusal that may
  clear soon, such as a machine still starting; otherwise, if every one refused for
  `num_ctx`, that error with the largest limit; otherwise the first machine's refusal.
- **Throughput.** Seriatim admits one job per GPU at a time, so its requests never compete
  with each other for that GPU's memory, and the next job goes to a free machine that may
  run the model. (Other programs on the same machine can still use the GPU.) Work spreads
  across every machine that can serve it, instead of stacking up on one of them.
- **One address.** Clients point their usual Ollama client at the gateway and add a key.
  `/api/tags`, `/api/ps`, `/api/version` and `/v1/models` show every machine at once, in
  Ollama's own format. `/models/select` suggests a model and a machine for a task.
- **Your requests, unchanged.** The bodies Seriatim forwards pass through byte for byte,
  including streaming and every Ollama option. The only responses it builds itself are the
  merged fleet-wide lists, still in Ollama's format, and its own errors. A `num_ctx` that is too large for a model is
  rejected with a clear error. It is never quietly lowered.
- **Failures you can see.** An unreachable machine, a broken stream or an exceeded limit
  comes back as an explicit, machine-readable error. It is never a silent success.
- **Monitoring.** The sidecar and the gateway write Prometheus metrics for the textfile
  collector of the exporter you already run (`node_exporter` or `windows_exporter`): slots,
  busy answers, errors, drains, spill-over, offline machines, and measured memory. Seriatim
  opens no extra port for them. If you run more than one gateway, give each its own
  instance name, so each writes its own file.

## Is it the standard API?

Yes, by design. **Every `/api/*` and `/v1/*` response has exactly Ollama's (or OpenAI's)
format, with no extra fields.** Bodies Seriatim forwards pass through unchanged. The one
exception is Seriatim's own refusals, such as "busy", which use its error format and are
marked by `X-Seriatim-*` headers. For the
lists that cover the whole fleet, the gateway merges the machines' answers within that same
format: one `/api/tags` entry per model name, every machine's loaded models in `/api/ps`,
and the lowest machine's version in `/api/version`. A list missing some machines is flagged
only by the `X-Seriatim-Partial` header. Which machine holds what is on Seriatim's own
paths, `/seriatim/models` and `/seriatim/nodes`. That will be checked by a test suite that runs the official `ollama`
Python client, and the `openai` Python SDK on Ollama's `/v1` routes, against plain Ollama,
a sidecar and the gateway, requires the same results, and checks every response against
the plain format with no extra keys. (The suite builds the `openai`
client with `max_retries=0`. Its default of 2 retries turns a fast "busy" into a delay, so
set it the same way if you want Seriatim's immediate answer.) When that suite lands, this
section will name the exact versions verified: "a drop-in Ollama endpoint, verified against
Ollama x.y with ollama-python a.b and openai c.d". The first Ollama version targeted is
0.40.16.

## What it does not do

- **No queueing or scheduling.** Seriatim answers "busy" and lets the client decide when to
  retry.
- **No load balancing beyond your priority order**, and no per-user quotas or billing.
- **No translation between API formats.** The OpenAI-compatible `/v1` routes go to Ollama's
  own `/v1` routes.
- **No web UI.**

## Platforms

- **The sidecar** is plain Python 3.12. It runs wherever Python and Ollama run: Linux,
  Windows or macOS, with NVIDIA, AMD, Apple or Intel graphics, or on the CPU alone. The gate
  and the routing work the same on every platform.
- **Memory reporting** is the one per-platform part. NVIDIA (through `nvidia-smi`) and Apple
  Silicon (through Metal) come first. Elsewhere the sidecar reports its memory as unknown,
  never guessed. Routing doesn't depend on it, because it uses your rules. Probes for other
  platforms are welcome contributions.
- **Install scripts** come first for Windows and macOS. That is a packaging order, not a
  supported topology. Other platforms can run the sidecar as an ordinary service, and
  scripts for them are welcome contributions.
- **The gateway** runs on any host with Python 3.12, or in a container.
- **Releases** are built by CI from `main`, with checksums and build-provenance
  attestations, and the install scripts verify both before installing.

## Documents

- [Specification](docs/SPEC.md): what Seriatim must do, and how each requirement is tested.
- [Architecture (ADR-001)](docs/adr/ADR-001-architecture.md): the decisions, the
  alternatives rejected, the HTTP contract and the threat model.
- [Slice 1 design](docs/design/TD-slice-1.md): the first piece of code, in detail.
- [Evaluation](docs/EVALUATION.md): how existing Ollama gateways compared, and why Seriatim
  exists.

## Contributing

Bug-fix pull requests are welcome. For a new feature, please open an issue first. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. See [LICENSE](LICENSE).
