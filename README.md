# Seriatim

A one-at-a-time GPU gate for [Ollama](https://ollama.com).

A sidecar on each GPU machine admits one inference request at a time. A request that arrives
while the GPU is busy gets an immediate HTTP 429 with `Retry-After` instead of waiting in a
hidden queue. A thin gateway gives clients one address and routes each request to a machine
by model size.

**Status:** design phase. There is no runnable code yet. The requirements, architecture and
first-slice design are under review in `docs/`.

## Licence

MIT. See [LICENSE](LICENSE).
