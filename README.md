# Seriatim

**One request at a time per GPU, routed across a home lab's machines.**

Seriatim sits in front of [Ollama](https://ollama.com) on a small fleet of GPU machines. It
makes sure each machine works on one inference request at a time, and it sends each request to
a machine that can actually hold the model. A machine that is busy says so at once, and the
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

The gateway's configuration file lists the machines in the fleet. You don't enter how much
memory each one has: the sidecar on each machine measures that and reports it. Each machine
is one entry:

```toml
[[nodes]]
id = "node-a"                       # any label you like
url = "http://node-a.example:11434"
priority = 1                        # lower is tried first
# capacity is measured and reported by the machine's sidecar

[[nodes]]
id = "node-b"
url = "http://node-b.example:11434"
priority = 2
capacity = "unlimited"              # optional override: no size check (e.g. a CPU-only box)

[[nodes]]
id = "node-c"
url = "http://node-c.example:11434"
priority = 3
max_params_b = 14                   # optional override: cap by parameter count instead
```

The overrides are for machines that can't report their memory, or where you know better.
When one is set, it wins, and the gateway's node list shows which rule applies to each
machine and why.

Nothing in the code counts machines, or knows their sizes or platforms. To change the fleet,
you edit that file:
- **Grow.** Install Ollama and the sidecar on the new machine, then add an entry. The
  gateway checks the new machine's health and models before it routes anything to it.
- **Shrink.** Remove the entry. No new work goes to that machine, and requests already
  running there finish normally.
- **Change.** After a card upgrade there is nothing to edit: the sidecar reports the new
  capacity. Change `priority` to prefer a different machine, or adjust an override if you
  use one. The next routing decision uses the new values.
- **Models.** Pull or delete models on any machine as usual. The gateway rediscovers each
  machine's models every minute, and at once when a model it was asked for is missing.

The gateway picks up an edited file without a restart. A file that fails validation is
rejected with a logged error, and the gateway keeps the configuration it already has.

## How Seriatim handles it

```
 clients ──▶ gateway ──▶ sidecar ──▶ Ollama      (machine A, smaller card)
                   │
                   ├──▶ sidecar ──▶ Ollama      (machine B, larger card)
                   └──▶ sidecar ──▶ Ollama      (machine C, …)
```

- **One at a time, enforced at the machine.** A small **sidecar** runs on each GPU machine,
  in front of Ollama. It admits one inference request at a time (configurable). Ollama is
  reachable only through the sidecar, so nothing slips past it.
- **Busy means an immediate answer.** A request that arrives while the machine is working
  gets HTTP 429 with `Retry-After` straight away, without Ollama ever seeing it. It never waits in a hidden queue, so a
  client is never stuck behind someone else's long generation without knowing why.
- **Routing by fit.** Each **sidecar** measures how much GPU memory its machine can give
  Ollama (on NVIDIA from `nvidia-smi`, on Apple Silicon from Metal) and reports it. The
  **gateway** estimates what a model needs from its size on disk plus the memory for the
  context length the request asks for, adds a safety margin, and sends it only to a machine
  where it fits. A model never goes to a machine too small for it, even when that machine is
  idle and the big one is busy.
- **Unknown means no.** If a machine can't report its memory and has no override, the
  gateway doesn't guess. It sends that machine no size-checked work, and says why.
- **Spill-over to keep every machine working.** When a machine answers busy, the gateway
  tries the next machine that can serve the same model, in your priority order. Each one is
  tried once, and nothing waits. Only if every suitable machine is busy does the client get
  the 429, with the shortest `Retry-After` of them.
- **Throughput.** Each GPU runs one job at full speed, with nothing competing for its
  memory, and the next job goes to a machine that is free and big enough. Work spreads across
  every machine that can serve it, instead of stacking up on one of them.
- **One address.** Clients point their usual Ollama client at the gateway and add a key.
  `/api/tags`, `/api/ps` and `/api/version` show every machine at once. `/models/select`
  suggests a model and a machine for a task.
- **Your requests, unchanged.** Request and response bodies pass through byte for byte,
  including streaming and every Ollama option. A `num_ctx` that is too large for a model is
  rejected with a clear error. It is never quietly lowered.
- **Failures you can see.** An unreachable machine, a broken stream or an exceeded limit
  comes back as an explicit, machine-readable error. It is never a silent success.

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
  never guessed. Give such a machine an override (`capacity = "unlimited"` or
  `max_params_b`) to route to it. Probes for other platforms are welcome contributions.
- **Install scripts** come first for Windows and macOS. That is a packaging order, not a
  supported topology. Other platforms can run the sidecar as an ordinary service, and
  scripts for them are welcome contributions.
- **The gateway** runs on any host with Python 3.12, or in a container.

## Documents

- [Specification](docs/SPEC.md): what Seriatim must do, and how each requirement is tested.
- [Architecture (ADR-001)](docs/adr/ADR-001-architecture.md): the decisions, the
  alternatives rejected, the HTTP contract and the threat model.
- [Slice 1 design](docs/design/TD-slice-1.md): the first piece of code, in detail.
- [Evaluation](docs/EVALUATION.md): how existing Ollama gateways compared, and why Seriatim
  exists.

These links work once the Phase 1 documents are merged.

## Contributing

Bug-fix pull requests are welcome. For a new feature, please open an issue first. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT. See [LICENSE](LICENSE).
