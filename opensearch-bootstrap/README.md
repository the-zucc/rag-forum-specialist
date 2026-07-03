# OpenSearch Bootstrap

One-shot setup for the OpenSearch cluster, run to completion before the
[ingestion pipeline](../ingestion/README.md) starts: creates the index
template and registers/deploys the local Ollama generative (`gpt-oss:20b`)
and embedding (`nomic-embed-text`) models as ml-commons remote models.
Re-runnable on every `docker compose up` — an existing connector/model
(matched by name) is undeployed, updated in place, and redeployed rather than
skipped, so config changes here actually take effect on the next `up`.

**Prerequisite:** Ollama must listen on all interfaces, not just `127.0.0.1`
(the default) — otherwise the connector registers and deploys fine but every
actual prediction fails with "Connection refused", since `host.docker.internal`
resolves to the docker bridge gateway, not loopback. See
[models.md](models.md#ollama-must-listen-on-all-interfaces) for the fix
(verified end-to-end on this machine).

## Files

- `bootstrap.py` — entry point; runs the two steps below in order.
- `create_index_template.py` — creates/updates the OpenSearch index template
  from `index_template.json` (mappings/settings for the posts index).
- `index_template.json` — the template itself.
- `register_ollama_model.py` — registers both local Ollama models (connector +
  model + deploy each) with ml-commons, so they're usable from Dashboards
  (Flow Framework, search pipelines) directly, not just from
  [`rag-agent/rag.py`](../rag-agent/rag.py) / [`ingestion/ingest.py`](../ingestion/ingest.py).
- `opensearch_client.py` — shared HTTP request / wait-for-cluster helpers.
- `models.md` — manual/step-by-step walkthrough of what `register_ollama_model.py`
  automates, useful for debugging directly in OpenSearch Dev Tools.

## How It Runs

The `opensearch-bootstrap` service in the repo-root `docker-compose.yml`
builds this directory and runs `bootstrap.py` once, after `opensearch` is
healthy. The `ingest` service declares `depends_on: opensearch-bootstrap:
condition: service_completed_successfully`, so ingestion never starts against
a cluster that's missing the index template or the model.

```bash
make up
```

## Standalone / Without Docker

```bash
python3 opensearch-bootstrap/bootstrap.py
```

Or run either step independently:

```bash
python3 opensearch-bootstrap/create_index_template.py
python3 opensearch-bootstrap/register_ollama_model.py
```

## Environment Variables

- `ES_URL` — OpenSearch base URL (default `http://localhost:9200`).
- `INDEX_NAME` — index name for the template (default `forum-posts`).
- `OLLAMA_ENDPOINT` — `host:port` where OpenSearch can reach Ollama (default
  `host.docker.internal:11434`; requires the `opensearch` service's
  `extra_hosts: host.docker.internal:host-gateway`, already set).
- `OLLAMA_MODEL` — generative model name pulled in Ollama (default `gpt-oss:20b`).
- `EMBED_MODEL` — embedding model name pulled in Ollama (default `nomic-embed-text`).
- `EMBED_DIMENSION` — the embedding model's output vector length (default
  `768`, matching `nomic-embed-text`). Must match `index_template.json`'s
  `vector_field.dimension` if you change `EMBED_MODEL` to something else.
- `WAIT_TIMEOUT` — seconds to wait for the cluster before giving up (default
  varies per script; see each file).
