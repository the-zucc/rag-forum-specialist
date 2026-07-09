# RAG

An agentic retrieval-augmented generation CLI for asking questions over the
`knowledge` index distilled by the
[knowledge processor](../knowledge-processing/knowledge-processor.md), using
LangGraph, Ollama, and OpenSearch. See [RAG-agent.md](RAG-agent.md) for the
full design.

## How It Answers a Question

`rag.py` builds a LangGraph graph that runs two loops in sequence — and
**grows the knowledge base as a side effect of answering**:

```text
user query
    |
    v
planner ──(gap: keywords)──> search ──> planner ──> …          (loop A)
    └──(sufficient / direct hit)──> reconstruct ──> distill ──> …   (loop B)
                                        └──(no more leads)──> answer
```

- **Loop A — knowledge fetching.** The planner distills the question into
  keywords aimed at what is *still missing*; the search node runs them over
  the `knowledge` index — each keyword individually as a wildcard query, then
  all in combination alongside a `knn` clause on the embedded query. Hits
  accumulate and deduplicate across rounds. The loop ends when the retained
  pieces cover the question, when a piece **answers it directly** (the
  "direct hit" fast path — only that piece's thread is mined), when keywords
  repeat, or when `--max-fetch-rounds` is spent.
- **Loop B — thread mining.** The retained pieces' `thread_id`s are followed
  back to `forum-posts`; each source thread is reconstructed in chronological
  order and re-read with the knowledge processor's extraction prompt, the
  user's question included as the *area of interest*. The resulting
  standalone pieces are embedded and **upserted back into `knowledge`** (same
  content-hash id scheme, so re-derived facts refresh instead of duplicating).
  A cross-thread check then decides whether the picture closes the question
  or leads warrant reconstructing more threads (`--max-thread-rounds` caps
  this).
- **Answer.** Composed from the knowledge pieces plus the reconstructed
  threads, citing thread titles and URLs — and instructed to say plainly when
  the sources don't contain the answer rather than guess.

Every LLM call **streams**: tokens are printed to the terminal as they are
generated (with a spinner while the model evaluates the prompt). Every step is
also logged (`--log-level`, default `INFO`): the planner's assessment, each
search, each reconstructed thread, each piece written back, and each
continue/answer decision.

## Files

- `rag.py` — the CLI entrypoint.
- `graph.py` — the LangGraph graph (`build_graph`, `ask_rag`): state, nodes,
  and edges.
- `server.py` — the `--serve` HTTP API (OpenAI-compatible), stdlib `http.server`.
- `knowledge.py` — search of and write-back to the `knowledge` index.
- `threads.py` — thread reconstruction from `forum-posts` and rendering.
- `prompts.py` — the planner / distiller / cross-check / answerer prompts.
- `parsing.py` — parsing of the model's structured output.
- `clients.py` — stdlib HTTP plumbing for OpenSearch and Ollama (streaming
  included).
- `RAG-agent.md` — the design document.
- `Dockerfile` — standalone image for the RAG CLI, built from the repo root
  (needs the shared `Pipfile`).

## Prerequisites

- A populated `knowledge` index — run the
  [knowledge processor](../knowledge-processing/) first (`make knowledge`).
  The agent answers from distilled knowledge, not raw posts, and exits with a
  pointer to `make knowledge` if the index is empty.
- The `forum-posts` index populated by the
  [ingestion pipeline](../ingestion/README.md) (`make up`), for thread
  reconstruction.
- A local Ollama instance with the embedding model (default `nomic-embed-text`)
  and LLM model (default `gpt-oss:20b`) pulled.

## Usage

Directly with Pipenv (from the repo root):

```bash
pipenv run python rag-agent/rag.py "how do I get rid of the hesitation around 3-4000 rpm?" \
  --opensearch-url http://localhost:9200 \
  --knowledge-index knowledge \
  --source-index forum-posts \
  --ollama-url http://localhost:11434 \
  --llm-model gpt-oss:20b \
  --embed-model nomic-embed-text
```

`--max-fetch-rounds` (default 3) caps the keyword-search rounds and
`--max-thread-rounds` (default 2) the reconstruction/distillation rounds;
`--thread-char-budget` (default 12000) trims long reconstructed threads;
`--num-ctx` sets the Ollama context window (default 65536 — Ollama's own
default of 4096 would silently truncate the reconstructed threads).

Show all options:

```bash
pipenv run python rag-agent/rag.py --help
```

## Serving an OpenAI-Compatible API

`--serve` starts an HTTP server (`server.py`, stdlib `http.server`) instead of
answering one question and exiting:

```bash
pipenv run python rag-agent/rag.py --serve --host 0.0.0.0 --port 8000 \
  --opensearch-url http://localhost:9200 \
  --knowledge-index knowledge \
  --ollama-url http://localhost:11434
```

- `POST /v1/chat/completions` — the last `user` message in `messages` is
  taken as the question; the response is a standard chat-completion object
  (or, with `"stream": true`, a single SSE delta followed by `[DONE]` — the
  graph runs to completion before anything is known, so there's nothing to
  stream token-by-token at this level). Point any OpenAI-client-compatible
  tool (Open WebUI, LangChain's `ChatOpenAI`, `curl`) at
  `http://<host>:<port>/v1` as its base URL.
- `GET /v1/models` — lists `--llm-model` as the one available model, for
  tools that populate a model picker from this endpoint.
- `GET /healthz` — plain liveness check.

This is a research agent, not a multi-turn chatbot: each request answers one
question from `messages`, ignoring prior turns and taking a request's `model`
field as informational only (it always uses `--llm-model` to plan and
answer). Requests are served **one at a time** — a lock serializes them, since
they all share one local Ollama instance that a second concurrent research
loop would only slow down, not speed up. A request can take several minutes
(the same two-loop research process as one-shot mode), so set client
timeouts accordingly.

An empty `knowledge` index is not a startup failure in `--serve` mode (only in
one-shot mode) — the server logs a warning and keeps running, since the
[knowledge processor](../knowledge-processing/)'s endless sweep may simply not
have reached anything relevant yet.

## Docker

Build the image (from the repo root):

```bash
docker build -f rag-agent/Dockerfile -t rag-agent .
```

Or via the Makefile, which also brings up OpenSearch first and runs the
container with host networking so it can reach OpenSearch/Ollama on
`localhost`:

```bash
make ask          # one-shot demonstration query
make rag-serve    # --serve, foreground, on $(RAG_PORT) (default 8000)
```

The `rag-serve` service in the repo's `docker-compose.yml` runs the same
`--serve` setup continuously (`restart: unless-stopped`) as part of `make up`,
listening on `:8000`.
