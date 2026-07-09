# Web UI

A single static page (`index.html` — no build step, no framework, no
dependencies) that chats with the [RAG agent](../rag-agent/README.md) through
the OpenAI-compatible API [`rag-serve`](../rag-agent/README.md#serving-an-openai-compatible-api)
exposes.

It talks directly to `http://<host>:8000` from the browser (not through this
container), so `webui`'s only job is to serve the one HTML file — it's
`python -m http.server` in its own image, nothing more.

## How It Works

- Every send POSTs `{"model": "rag-agent", "messages": [{"role": "user",
  "content": "<question>"}]}` to `/v1/chat/completions` and renders
  `choices[0].message.content`. Only the new question is sent — the agent
  answers one question at a time and doesn't use prior turns, so there's no
  point pretending otherwise by re-sending history it will ignore.
- A pending bubble ("Researching the forum…") stays up for the duration of
  the request, since a real answer involves multiple LLM calls and can take a
  few minutes (see the design in [RAG-agent.md](../rag-agent/RAG-agent.md)).
- A banner at the top warns if `/healthz` isn't reachable when the page
  loads (e.g. `rag-serve` is still starting).

## Running It

Part of `make up` (service `rag-webui` in the repo's `docker-compose.yml`),
which also opens a browser at `http://localhost:$(WEBUI_PORT)` (default
`3000`) once the page responds. Requires `rag-serve` to be up (same compose
file) since that's what actually answers questions.

To run it standalone:

```bash
docker build -t rag-webui webui
docker run --rm -p 3000:3000 rag-webui
```

Then open `http://localhost:3000` with `rag-serve` reachable at
`http://localhost:8000` (adjust `API_BASE` in `index.html` if it's elsewhere).
