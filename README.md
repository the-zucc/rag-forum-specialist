# RAG-Enabled Forum Specialist

Scrapes a forum, indexes the posts for search, and answers questions about
them with a local RAG pipeline using Opensearch for finding relevant info.

The project is split into independent components:

| Directory | What it does | README |
|---|---|---|
| [`etl/crawler/`](etl/crawler/README.md) | Crawls a forum with Selenium and writes `<thread-id>/posts.json` files. | [etl/crawler/README.md](etl/crawler/README.md) |
| [`opensearch-bootstrap/`](opensearch-bootstrap/README.md) | One-shot: creates the OpenSearch index template and registers the local Ollama generative + embedding models, before ingestion starts. | [opensearch-bootstrap/README.md](opensearch-bootstrap/README.md) |
| [`etl/ingestion/`](etl/ingestion/README.md) | Watches those JSON files and indexes them into Opensoarch, embedding text with Ollama for vector search. | [etl/ingestion/README.md](etl/ingestion/README.md) |
| [`etl/knowledge-processing/`](etl/knowledge-processing/knowledge-processor.md) | Distills popular/recent threads into durable, standalone knowledge pieces in a `knowledge` index — endlessly, sweeping the popularity ranking window by window. | [etl/knowledge-processing/knowledge-processor.md](etl/knowledge-processing/knowledge-processor.md) |
| [`rag/`](rag/README.md) | Agentic LangGraph loop that answers questions from the `knowledge` index (re-reading source threads and growing the index as it goes), as a CLI or as an OpenAI-compatible HTTP API (`--serve`). | [rag/README.md](rag/README.md) |
| [`webui/`](webui/README.md) | A single static page that chats with the RAG agent's API — no build step, no framework. | [webui/README.md](webui/README.md) |

## Near-Real-Time ETL Flow, with LLM Transform Step

Extract, Transform, Load. This is what is done, at the high level.
1. The raw data (forum posts) is extracted from the forum and stored on-disk as JSON files via a user-supplied crawler (an example crawler is included here but this is site-specific). In the example crawler, the implementation includes a "serve" mode, so that the crawler can endlessly wait for new posts, making it a near-real-time data collector.
2. The data is indexed (with vector embedding) into Opensearch as-is using a simple ingestion pipeline. This also features a "serve" mode which monitors the filesystem for new posts.
3. A knowledge processor reads the posts from Opensearch, reconstructs the threads, and distills the knowledge into standalone pieces of information. These pieces are indexed (with vector embedding) in Opensearch in an index separate from the one containing the posts. This also includes a "serve" mode, so any new threads and posts are analyzed and distilled into knowledge pieces.
4. An OpenAI-Compatible API, accessible through a web app, exposes a flow that relates user query to knowledge pieces, and fetches Opensearch for context based on user query and existing knowledge. Threads are reconstructed knowledge pieces kept in context, and the response is served to the user.

## How It Fits Together

```text
crawler/  --writes-->  threads/<thread-id>/posts.json
                              |
                              v
opensearch-bootstrap/  --sets up-->  index template + Ollama models in OpenSearch
                              |
                              v
ingestion/  --indexes-->  OpenSearch: forum-posts
                              |
                              v
knowledge-processing/  --distills-->  OpenSearch: knowledge
                              |
                              v
rag/  --answers-->  CLI answer, or an an OpenAI-compatible API (--serve)
                              |
                              v
webui/  --chats with-->  rag's API, in a browser
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
ingestion watcher, the knowledge processor (endlessly distilling threads into
the `knowledge` index), the RAG agent's OpenAI-compatible API, the chat web
UI in front of it, and a scraper crawling one board in `--serve` mode, all in
the background — then waits for the web UI to come up and opens it in a
browser:

```bash
make up
```

Or ask a one-off question from the CLI instead (requires a local Ollama with
the configured models pulled):

```bash
make ask
```

See `Makefile` for all targets (`up`, `down`, `logs`, `bootstrap`, `ingest`,
`knowledge`, `clean`, `rag-build`, `ask`, `rag-serve`), and each component's
README for direct (non-Docker) usage.

## Setup

Crawler and RAG dependencies are managed with a shared root Pipenv
environment:

```bash
pipenv install
```

The ingestion pipeline has its own minimal `requirements.txt` (see
[ingestion/README.md](ingestion/README.md)) so it can run standalone without
Selenium/LangGraph installed.

## Development Notes

`crawler/page-samples/` holds saved example forum pages used to inspect the
forum structure while developing the crawler (gitignored, local only); the
live crawler fetches pages through Selenium instead.
