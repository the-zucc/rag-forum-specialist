# RAG

A retrieval-augmented generation CLI for asking questions over the indexed
forum posts, using LangChain, Ollama, and OpenSearch as the vector store.

## Files

- `rag.py` — CLI and `initialize_rag`/`ask_rag` helpers.
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
  --embed-model nomic-embed-text
```

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
