# RAG

An agentic retrieval-augmented generation CLI for asking questions over the
indexed forum posts, using LangGraph, Ollama, and OpenSearch.

## How It Answers a Question

`rag.py` builds a LangGraph graph that *researches* the forum iteratively —
the way a person would: search, read, notice what's still unclear, search
again, and only answer once it understands the subject. It doesn't answer from
a single retrieval pass.

```text
START ─> plan ──(need more info)──> search ──> plan ──> …
           └────(confident / out of budget)──> answerer ─> END
```

1. **plan** — the model reflects on the question, its **accumulated
   understanding** so far, and the documents from the latest search. It
   rewrites its understanding to fold in the new information, then decides:
   `DONE` (it can answer confidently) or `CONTINUE` with the next search
   keywords aimed at whatever is *still missing*. On the first pass (nothing
   searched yet) it just picks the opening keywords.
2. **search** — embeds the keywords and runs *one* OpenSearch query combining
   a `knn` clause (`vector_field`) and a `multi_match` clause
   (`body_text`/`message_text`/`thread_title`) in the same `bool.should`, so a
   post can match on either vector similarity or keyword overlap. Relevant
   hits are **accumulated and deduplicated across iterations** (keyed by post
   URL/id), so nothing potentially useful is discarded between rounds.
3. The graph loops `plan → search → plan …` until the model is confident or it
   hits `--max-iterations` (default 3).
4. **answerer** — answers the original question from *everything kept*, and is
   told to say so plainly if the posts don't contain the answer rather than
   guess.

Every step is logged (`--log-level`, default `INFO`) so the model's
research — each search query, how many documents it kept, its evolving
understanding, and each continue/answer decision — is observable.

## Files

- `rag.py` — the graph (`build_graph`), the `ask_rag` helper, and the CLI.
- `Dockerfile` — standalone image for the RAG CLI, built from the repo root
  (needs the shared `Pipfile`).

## Prerequisites

- An OpenSearch index populated by the [ingestion pipeline](../ingestion/README.md)
  (`make up` brings up OpenSearch + the ingest watcher).
- A local Ollama instance with the embedding model (default `nomic-embed-text`)
  and LLM model (default `llama2`, `gpt-oss:20b` via `make ask`) pulled.

## Usage

Directly with Pipenv (from the repo root):

```bash
pipenv run python rag-agent/rag.py "how do I get rid of the hesitation around 3-4000 rpm?" \
  --opensearch-url http://localhost:9200 \
  --index-name forum-posts \
  --ollama-url http://localhost:11434 \
  --llm-model gpt-oss:20b \
  --embed-model nomic-embed-text \
  --max-iterations 3 \
  --log-level INFO
```

`--max-iterations` caps how many search rounds the model may run before it
must answer; `--log-level DEBUG` additionally prints the model's full evolving
understanding each round.

Show all options:

```bash
pipenv run python rag-agent/rag.py --help
```

## Docker

Build the image (from the repo root):

```bash
docker build -f rag-agent/Dockerfile -t rag-agent .
```

Or via the Makefile, which also brings up OpenSearch first and runs the
container with host networking so it can reach OpenSearch/Ollama on
`localhost`:

```bash
make ask
```
