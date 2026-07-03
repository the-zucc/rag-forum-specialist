# Forum Scraper

Scrapes a forum, indexes the posts for search, and answers questions about
them with a local RAG pipeline. The project is split into four independent
components:

| Directory | What it does | README |
|---|---|---|
| [`crawler/`](crawler/README.md) | Crawls a forum with Selenium and writes `<thread-id>/posts.json` files. | [crawler/README.md](crawler/README.md) |
| [`opensearch-bootstrap/`](opensearch-bootstrap/README.md) | One-shot: creates the OpenSearch index template and registers the local Ollama generative + embedding models, before ingestion starts. | [opensearch-bootstrap/README.md](opensearch-bootstrap/README.md) |
| [`ingestion/`](ingestion/README.md) | Watches those JSON files and indexes them into OpenSearch, embedding text with Ollama for vector search. | [ingestion/README.md](ingestion/README.md) |
| [`rag-agent/`](rag-agent/README.md) | CLI that answers questions over the indexed posts using LangChain + Ollama + OpenSearch. | [rag-agent/README.md](rag-agent/README.md) |

## How It Fits Together

```text
crawler/  --writes-->  threads/<thread-id>/posts.json
                              |
                              v
opensearch-bootstrap/  --sets up-->  index template + Ollama models in OpenSearch
                              |
                              v
ingestion/  --indexes-->  OpenSearch
                              |
                              v
rag-agent/  --queries-->  answers with cited source posts
```

## Quickstart

Copy `.env.example` to `.env` and fill in your forum's actual board URL.
`.env` is gitignored, so the real forum host never lands in source control;
`docker compose` (and thus `make`) loads it automatically:

```bash
cp .env.example .env
```

Bring up OpenSearch, OpenSearch Dashboards, opensearch-bootstrap (index
template + generative/embedding Ollama model registration — see
[opensearch-bootstrap/models.md](opensearch-bootstrap/models.md)), the
ingestion watcher, and a scraper crawling one board in `--serve` mode, all in
the background:

```bash
make up
```

Ask a question against the indexed posts (requires a local Ollama with the
configured models pulled):

```bash
make ask
```

See `Makefile` for all targets (`up`, `down`, `logs`, `bootstrap`, `ingest`,
`clean`, `rag-build`, `ask`), and each component's README for direct
(non-Docker) usage.

## Setup

Crawler and RAG dependencies are managed with a shared root Pipenv
environment:

```bash
pipenv install
```

The ingestion pipeline has its own minimal `requirements.txt` (see
[ingestion/README.md](ingestion/README.md)) so it can run standalone without
Selenium/LangChain installed.

## Development Notes

`crawler/page-samples/` holds saved example forum pages used to inspect the
forum structure while developing the crawler (gitignored, local only); the
live crawler fetches pages through Selenium instead.
