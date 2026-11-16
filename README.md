# Seriatim

**One request at a time per GPU, routed across a home lab's machines.**

Seriatim sits in front of [Ollama](https://ollama.com) on a small fleet of GPU machines. It
makes sure each machine works on one inference request at a time, and it sends each request to
a machine that can actually hold the model. A machine that is busy says so at once, and the
request moves on to another machine that can take it.

> **Status:** design phase. There is no runnable code yet. The requirements, architecture and
> first-slice design are under review in [`docs/`](docs/).

## Who it is for

Seriatim is built for **home labs and small teams**. These usually have two to four GPU
machines that were never bought as a matched set. Each machine has a different amount of
video memory. For example:
- a desktop with one consumer card;
- an older card in a second box;
- a workstation with a large pool of unified memory.

Several people, scripts or agents share these machines. Each machine runs Ollama.

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
- **Routing by model size.** The **gateway** knows how large a model each machine can hold.
  You set that per machine, for example "up to 14B" for the smaller card and no limit for the
  big one. The model's size comes from Ollama's own metadata. A model is only ever sent to a
  machine big enough for it, even when that machine is busy and a smaller one is idle.
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

- **GPU machines:** Windows with NVIDIA, and macOS on Apple Silicon. Each runs Ollama and the
  sidecar as services.
- **The gateway:** any Linux host, or a container.
- **Requirements:** Python 3.12 or later.

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
