# Ingestion Pipeline

Indexes scraped forum posts from `threads/<thread-id>/posts.json` (written by the
[crawler](../crawler/README.md)) into OpenSearch, optionally embedding each
post's text with Ollama for vector (kNN) search.

Assumes the index template already exists — that's created by
[`opensearch-bootstrap`](../opensearch-bootstrap/README.md), which
docker-compose always runs to completion before this service starts.

## Files

- `ingest.py` — the ingestion script. Uses only the Python standard library for
  the one-shot ingest path, so it runs without installing anything extra.
  `--watch` mode additionally requires the `watchdog` package.
- `Dockerfile` — standalone image for running `ingest.py --watch` continuously,
  built with its own `requirements.txt` (independent of the crawler/RAG Pipfile).
- `requirements.txt` — dependencies for the Docker image (currently just `watchdog`).

## What It Does

Bulk-indexes every post found under the destination directory, upserting by
post id so re-runs never duplicate. If an Ollama URL is supplied (the
default), each post's `body_text`/`message_text` is embedded and stored in
`vector_field` for kNN search.

Pass `--watch` to keep running afterwards: it listens for created/modified
`threads/<id>/posts.json` files and reindexes just that thread as soon as the
crawler writes new posts.

## Usage

One-off local reindex, without Docker (also available as `make ingest`):

```bash
python3 ingestion/ingest.py \
  --es-url http://localhost:9200 \
  --index forum-posts \
  --dest-dir threads
```

Skip embeddings (index text/metadata only, no vector search support):

```bash
python3 ingestion/ingest.py --no-embed
```

Show all options:

```bash
python3 ingestion/ingest.py --help
```

## Docker / docker-compose

The `ingest` service in the repo-root `docker-compose.yml` builds this
directory and runs `ingest.py --watch` continuously against `./threads`, using
host networking so it can reach a local Ollama instance:

```bash
make up    # starts opensearch, dashboards, ingest, and the tuning/troubleshooting scraper
```
