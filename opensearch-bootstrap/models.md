# Registering the Ollama Models in OpenSearch

Registers two locally-hosted Ollama models as ml-commons remote models, so
they can be used directly from OpenSearch Dashboards (Flow Framework, search
pipelines, Dev Tools) instead of only through
[`rag-agent/rag.py`](../rag-agent/rag.py) / [`ingestion/ingest.py`](../ingestion/ingest.py):

- **`gpt-oss:20b`** (generative/chat), via Ollama's `/api/generate`.
- **`nomic-embed-text`** (embedding), via Ollama's `/api/embeddings` — the
  same model `ingestion/ingest.py` already calls directly to embed posts for
  kNN search; this registers it as a *second*, independent path to it inside
  OpenSearch itself.

Same idea as the connector examples in
[dashboards-flow-framework's models.md](https://github.com/opensearch-project/dashboards-flow-framework/blob/main/documentation/models.md),
adapted for local Ollama endpoints instead of cloud providers.

## Automated

`make up` (or plain `docker compose up -d`) runs this automatically: the
`opensearch-bootstrap` service in `docker-compose.yml` waits for OpenSearch
to be healthy, then runs [`register_ollama_model.py`](register_ollama_model.py),
which does everything below (cluster settings, connector, model, deploy — for
both models) via the same REST calls. It's re-runnable, not just idempotent:
if a connector/model by that name already exists it's undeployed, updated in
place (same connector/model IDs, current config from the script), and
redeployed, rather than skipped — so changing the body builders (endpoint,
model name, timeouts, etc.) and re-running `make up` actually applies the
change. Override the models via the `OLLAMA_MODEL` / `EMBED_MODEL` env vars
(`.env`/`.env.example`) if you're not using `gpt-oss:20b` / `nomic-embed-text`.

Prerequisite either way: Ollama running on the host with both models pulled
(`ollama pull gpt-oss:20b && ollama pull nomic-embed-text`).

### Ollama must listen on all interfaces

Verified by testing end-to-end: Ollama defaults to binding `127.0.0.1:11434`
only. `host.docker.internal` (used by the connector to reach the host from
inside the `opensearch` container) resolves to the docker bridge gateway, not
loopback — so a loopback-only Ollama gets "Connection refused" on every
`_predict` call even though the connector/model themselves register and
deploy fine. If Ollama runs as the systemd service installed by the official
installer, fix it with:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf <<EOF
[Service]
Environment="OLLAMA_HOST=0.0.0.0"
EOF
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Confirm with `ss -tlnp | grep 11434` — it should show `0.0.0.0:11434`, not
`127.0.0.1:11434`.

What follows is the manual/step-by-step equivalent, useful for understanding
what the script does or for debugging it directly in Dev Tools
(`$ES_URL` = `http://localhost:9200` by default).

## Cluster settings

This is a single-node dev cluster with no dedicated ML node, so ml-commons
must be allowed to run on the data node, and the local Ollama endpoint must
be added to the connector trust list:

```
PUT _cluster/settings
{
  "persistent": {
    "plugins.ml_commons.only_run_on_ml_node": false,
    "plugins.ml_commons.connector.private_ip_enabled": true,
    "plugins.ml_commons.trusted_connector_endpoints_regex": [
      "^https://runtime\\.sagemaker\\..*[a-z0-9-]\\.amazonaws\\.com/.*$",
      "^https://api\\.openai\\.com/.*$",
      "^https://api\\.cohere\\.ai/.*$",
      "^https://bedrock-runtime\\..*[a-z0-9-]\\.amazonaws\\.com/.*$",
      "^https://api\\.deepseek\\.com/.*$",
      "^http://host\\.docker\\.internal:11434/.*$"
    ]
  }
}
```

(The first five trusted-endpoint patterns are ml-commons' own defaults; keep
them if you use those providers elsewhere. Only the last one, plus
`private_ip_enabled`, are Ollama-specific — Ollama is a local/private-IP
target, which ml-commons blocks by default as an SSRF guard.)

**Troubleshooting:** if this `PUT` 400s with `unknown setting
[archived.plugins...]`, a prior OpenSearch version left a stale archived
setting in cluster state (hit this migrating a dev cluster from 2.19.1 to
3.7.0). Clear it first, then retry:

```
PUT _cluster/settings
{ "persistent": { "archived.<setting-name-from-the-error>": null } }
```

Two things not obvious from ml-commons' own docs, both found by testing this
end-to-end, apply to *both* connectors below:

- `credential` is required and rejected if null/empty, even though Ollama
  needs no auth — a placeholder object satisfies it.
- ml-commons' default connector read timeout is too short for `gpt-oss:20b`'s
  first response (~15-18s observed for a trivial prompt, including "thinking"
  tokens); `client_config.read_timeout_ms` gives it headroom. The embedding
  connector is much faster, so it keeps a shorter timeout.

Also note the `connector_id` returned by each `_create` call; the matching
`_register` call below needs it.

## Generative Connector + Model

```
POST /_plugins/_ml/connectors/_create
{
  "name": "Ollama - gpt-oss:20b",
  "description": "Connector for a local Ollama gpt-oss:20b model",
  "version": "1",
  "protocol": "http",
  "parameters": {
    "endpoint": "host.docker.internal:11434",
    "model": "gpt-oss:20b"
  },
  "credential": {
    "placeholder_key": "not-needed-for-ollama"
  },
  "client_config": {
    "connect_timeout_ms": 30000,
    "read_timeout_ms": 120000
  },
  "actions": [
    {
      "action_type": "predict",
      "method": "POST",
      "url": "http://${parameters.endpoint}/api/generate",
      "headers": {
        "Content-Type": "application/json"
      },
      "request_body": "{ \"model\": \"${parameters.model}\", \"prompt\": \"${parameters.prompt}\", \"stream\": false }"
    }
  ]
}
```

```
POST /_plugins/_ml/models/_register
{
  "name": "Ollama gpt-oss:20b",
  "function_name": "remote",
  "description": "Local Ollama-hosted gpt-oss:20b generative model",
  "connector_id": "<connector_id>",
  "interface": {
    "input": {
      "type": "object",
      "properties": {
        "parameters": {
          "type": "object",
          "properties": {
            "prompt": { "type": "string" }
          },
          "additionalProperties": true,
          "required": ["prompt"]
        }
      }
    },
    "output": {
      "type": "object",
      "properties": {
        "inference_results": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "output": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "name": { "type": "string" },
                    "dataAsMap": {
                      "type": "object",
                      "properties": {
                        "response": { "type": "string" }
                      },
                      "required": ["response"]
                    }
                  },
                  "required": ["name", "dataAsMap"]
                }
              },
              "status_code": { "type": "integer" }
            },
            "required": ["output", "status_code"]
          }
        }
      },
      "required": ["inference_results"]
    }
  }
}
```

The response includes a `model_id`; deploy it, then test it:

```
POST /_plugins/_ml/models/<model_id>/_deploy
```

```
POST /_plugins/_ml/models/<model_id>/_predict
{
  "parameters": {
    "prompt": "how do I get rid of the hesitation around 3-4000 rpm?"
  }
}
```

The generated text comes back at
`inference_results[0].output[0].dataAsMap.response` — verified working
end-to-end (`status_code: 200`, e.g. prompting "Say hello in exactly three
words." returned `"response": "Hello there, friend!"`, ~15s).

## Embedding Connector + Model

Same shape, pointed at Ollama's `/api/embeddings` instead, with `text` as the
input parameter and `embedding` as the output field:

```
POST /_plugins/_ml/connectors/_create
{
  "name": "Ollama - nomic-embed-text (embedding)",
  "description": "Connector for a local Ollama nomic-embed-text embedding model",
  "version": "1",
  "protocol": "http",
  "parameters": {
    "endpoint": "host.docker.internal:11434",
    "model": "nomic-embed-text"
  },
  "credential": {
    "placeholder_key": "not-needed-for-ollama"
  },
  "client_config": {
    "connect_timeout_ms": 30000,
    "read_timeout_ms": 30000
  },
  "actions": [
    {
      "action_type": "predict",
      "method": "POST",
      "url": "http://${parameters.endpoint}/api/embeddings",
      "headers": {
        "Content-Type": "application/json"
      },
      "request_body": "{ \"model\": \"${parameters.model}\", \"prompt\": \"${parameters.text}\" }"
    }
  ]
}
```

```
POST /_plugins/_ml/models/_register
{
  "name": "Ollama nomic-embed-text (embedding)",
  "function_name": "remote",
  "description": "Local Ollama-hosted nomic-embed-text embedding model",
  "connector_id": "<connector_id>",
  "model_config": {
    "model_type": "nomic-embed-text",
    "embedding_dimension": 768,
    "framework_type": "SENTENCE_TRANSFORMERS"
  },
  "interface": {
    "input": {
      "type": "object",
      "properties": {
        "parameters": {
          "type": "object",
          "properties": {
            "text": { "type": "string" }
          },
          "additionalProperties": true,
          "required": ["text"]
        }
      }
    },
    "output": {
      "type": "object",
      "properties": {
        "inference_results": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "output": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "name": { "type": "string" },
                    "dataAsMap": {
                      "type": "object",
                      "properties": {
                        "embedding": { "type": "array" }
                      },
                      "required": ["embedding"]
                    }
                  },
                  "required": ["name", "dataAsMap"]
                }
              },
              "status_code": { "type": "integer" }
            },
            "required": ["output", "status_code"]
          }
        }
      },
      "required": ["inference_results"]
    }
  }
}
```

`model_config` isn't required to make `_predict` work, but declares the
embedding length on the model itself (surfaced in Dashboards' model picker,
and needed by some neural-search features that read it). Two more things
found by testing: `embedding_dimension` 400s with "model type is null" unless
`model_type` is also set, and `framework_type` only accepts
`SENTENCE_TRANSFORMERS` or `HUGGINGFACE_TRANSFORMERS` — not something
Ollama-accurate like `"OLLAMA"` — even though this is a remote connector
model, not an actual sentence-transformers deployment.

Deploy and test the same way:

```
POST /_plugins/_ml/models/<model_id>/_deploy
```

```
POST /_plugins/_ml/models/<model_id>/_predict
{
  "parameters": {
    "text": "how do I get rid of the hesitation around 3-4000 rpm?"
  }
}
```

The vector comes back at `inference_results[0].output[0].dataAsMap.embedding`
— verified working end-to-end (`status_code: 200`, 768 dimensions, matching
`index_template.json`'s `vector_field` mapping).

## From Here

Both models can be wired into a Flow Framework use case or search pipeline
like any other remote model.
