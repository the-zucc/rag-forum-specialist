#!/usr/bin/env python3
"""Register the local Ollama models as OpenSearch ml-commons remote models.

Automates what's documented in models.md: sets the cluster settings needed to
trust a local Ollama endpoint and run ml-commons on a data node, then
creates-or-updates the generative (chat) and embedding connectors/models by
name (so editing their body builders and re-running actually applies the
change, not just skips), undeploying each model first since ml-commons
refuses to update a connector while a model still uses it. Safe to re-run.
"""

import os
import re
import time

from opensearch_client import request, wait_for_cluster

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
OLLAMA_ENDPOINT = os.environ.get("OLLAMA_ENDPOINT", "host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_DIMENSION = int(os.environ.get("EMBED_DIMENSION", "768"))
WAIT_TIMEOUT = int(os.environ.get("WAIT_TIMEOUT", "120"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "180"))

# ml-commons' own default trusted endpoints, kept so other connectors still work.
DEFAULT_TRUSTED_ENDPOINTS = [
    r"^https://runtime\.sagemaker\..*[a-z0-9-]\.amazonaws\.com/.*$",
    r"^https://api\.openai\.com/.*$",
    r"^https://api\.cohere\.ai/.*$",
    r"^https://bedrock-runtime\..*[a-z0-9-]\.amazonaws\.com/.*$",
    r"^https://api\.deepseek\.com/.*$",
]


def configure_cluster_settings(es_url=ES_URL):
    endpoint_pattern = rf"^http://{re.escape(OLLAMA_ENDPOINT)}/.*$"
    request(es_url, "PUT", "/_cluster/settings", {
        "persistent": {
            "plugins.ml_commons.only_run_on_ml_node": False,
            # Ollama is a local/private-IP endpoint; ml-commons blocks those
            # by default (SSRF guard) unless explicitly enabled.
            "plugins.ml_commons.connector.private_ip_enabled": True,
            "plugins.ml_commons.trusted_connector_endpoints_regex": (
                DEFAULT_TRUSTED_ENDPOINTS + [endpoint_pattern]
            ),
        }
    })
    print("Configured ml-commons cluster settings")


def find_by_name(es_url, search_path, name):
    _, body = request(es_url, "POST", search_path, {"query": {"match_all": {}}, "size": 1000})
    for hit in body.get("hits", {}).get("hits", []):
        if hit.get("_source", {}).get("name") == name:
            return hit["_id"], hit["_source"]
    return None, None


def poll_task(es_url, task_id, timeout=TASK_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, body = request(es_url, "GET", f"/_plugins/_ml/tasks/{task_id}")
        state = body.get("state")
        if state == "COMPLETED":
            return body.get("model_id")
        if state in ("FAILED", "COMPLETED_WITH_ERROR"):
            raise RuntimeError(f"Task {task_id} failed: {body}")
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for task {task_id}")


def connector_body(name, description, model, api_path, request_body, read_timeout_ms):
    return {
        "name": name,
        "description": description,
        "version": "1",
        "protocol": "http",
        "parameters": {
            "endpoint": OLLAMA_ENDPOINT,
            "model": model,
        },
        # ml-commons rejects connectors with a null/empty credential map even
        # when the connector doesn't need auth (Ollama is unauthenticated).
        "credential": {"placeholder_key": "not-needed-for-ollama"},
        "client_config": {
            "connect_timeout_ms": 30000,
            "read_timeout_ms": read_timeout_ms,
        },
        "actions": [
            {
                "action_type": "predict",
                "method": "POST",
                "url": f"http://${{parameters.endpoint}}{api_path}",
                "headers": {"Content-Type": "application/json"},
                "request_body": request_body,
            }
        ],
    }


def model_body(name, description, connector_id, input_param, output_field, output_field_type, model_config=None):
    body = {
        "name": name,
        "function_name": "remote",
        "description": description,
        "connector_id": connector_id,
        "interface": {
            "input": {
                "type": "object",
                "properties": {
                    "parameters": {
                        "type": "object",
                        "properties": {input_param: {"type": "string"}},
                        "additionalProperties": True,
                        "required": [input_param],
                    }
                },
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
                                            "name": {"type": "string"},
                                            "dataAsMap": {
                                                "type": "object",
                                                "properties": {output_field: {"type": output_field_type}},
                                                "required": [output_field],
                                            },
                                        },
                                        "required": ["name", "dataAsMap"],
                                    },
                                },
                                "status_code": {"type": "integer"},
                            },
                            "required": ["output", "status_code"],
                        },
                    }
                },
                "required": ["inference_results"],
            },
        },
    }
    if model_config:
        body["model_config"] = model_config
    return body


def generate_connector_body():
    return connector_body(
        name=f"Ollama - {OLLAMA_MODEL}",
        description=f"Connector for a local Ollama {OLLAMA_MODEL} model",
        model=OLLAMA_MODEL,
        api_path="/api/generate",
        # ml-commons' default read timeout is too short for a 20B reasoning
        # model's first response (verified: ~18s direct against Ollama, and
        # that's without the model already warm in memory).
        request_body=(
            '{ "model": "${parameters.model}", '
            '"prompt": "${parameters.prompt}", "stream": false }'
        ),
        read_timeout_ms=120000,
    )


def generate_model_body(connector_id):
    return model_body(
        name=f"Ollama {OLLAMA_MODEL}",
        description=f"Local Ollama-hosted {OLLAMA_MODEL} generative model",
        connector_id=connector_id,
        input_param="prompt",
        output_field="response",
        output_field_type="string",
    )


def embed_connector_body():
    return connector_body(
        name=f"Ollama - {EMBED_MODEL} (embedding)",
        description=f"Connector for a local Ollama {EMBED_MODEL} embedding model",
        model=EMBED_MODEL,
        api_path="/api/embeddings",
        request_body='{ "model": "${parameters.model}", "prompt": "${parameters.text}" }',
        read_timeout_ms=30000,
    )


def embed_model_body(connector_id):
    return model_body(
        name=f"Ollama {EMBED_MODEL} (embedding)",
        description=f"Local Ollama-hosted {EMBED_MODEL} embedding model",
        connector_id=connector_id,
        input_param="text",
        output_field="embedding",
        output_field_type="array",
        model_config={
            "model_type": EMBED_MODEL,
            "embedding_dimension": EMBED_DIMENSION,
            # ml-commons requires framework_type from a fixed enum
            # (SENTENCE_TRANSFORMERS or HUGGINGFACE_TRANSFORMERS) even for
            # remote connector models where it doesn't really apply.
            "framework_type": "SENTENCE_TRANSFORMERS",
        },
    )


def ensure_connector(es_url, existing_id, body):
    if existing_id:
        request(es_url, "PUT", f"/_plugins/_ml/connectors/{existing_id}", body)
        print(f"Updated connector {existing_id}")
        return existing_id
    _, resp = request(es_url, "POST", "/_plugins/_ml/connectors/_create", body)
    connector_id = resp["connector_id"]
    print(f"Created connector {connector_id}")
    return connector_id


def ensure_model(es_url, existing_id, body):
    if existing_id:
        request(es_url, "PUT", f"/_plugins/_ml/models/{existing_id}", body)
        print(f"Updated model {existing_id}")
        return existing_id
    _, resp = request(es_url, "POST", "/_plugins/_ml/models/_register", body)
    model_id = resp.get("model_id") or poll_task(es_url, resp["task_id"])
    print(f"Registered model {model_id}")
    return model_id


def register_model(es_url, connector_name, connector_body_value, model_name, model_body_fn):
    """Create-or-update a connector + model pair by name, then (re)deploy it."""
    connector_id, _ = find_by_name(es_url, "/_plugins/_ml/connectors/_search", connector_name)
    model_id, _ = find_by_name(es_url, "/_plugins/_ml/models/_search", model_name)

    if model_id:
        # ml-commons refuses to update a connector while a model still uses
        # it; undeploying an already-undeployed model is a harmless no-op.
        request(es_url, "POST", f"/_plugins/_ml/models/{model_id}/_undeploy")

    connector_id = ensure_connector(es_url, connector_id, connector_body_value)
    model_id = ensure_model(es_url, model_id, model_body_fn(connector_id))

    _, body = request(es_url, "POST", f"/_plugins/_ml/models/{model_id}/_deploy")
    task_id = body.get("task_id")
    if task_id:
        poll_task(es_url, task_id)
    print(f"Deployed model {model_id}")
    return model_id


def register_ollama_models(es_url=ES_URL):
    configure_cluster_settings(es_url)

    llm_model_id = register_model(
        es_url,
        f"Ollama - {OLLAMA_MODEL}",
        generate_connector_body(),
        f"Ollama {OLLAMA_MODEL}",
        generate_model_body,
    )
    embed_model_id = register_model(
        es_url,
        f"Ollama - {EMBED_MODEL} (embedding)",
        embed_connector_body(),
        f"Ollama {EMBED_MODEL} (embedding)",
        embed_model_body,
    )
    return llm_model_id, embed_model_id


if __name__ == "__main__":
    wait_for_cluster(ES_URL, WAIT_TIMEOUT)
    register_ollama_models()
