#!/usr/bin/env python3
"""One-shot OpenSearch setup: creates the index template and registers/deploys
the local Ollama generative and embedding models. Re-runnable: an existing
connector/model is updated in place and redeployed, not skipped, so config
changes apply on the next run.

Meant to run to completion before the ingestion service starts (see
docker-compose.yml's `depends_on: opensearch-bootstrap: condition:
service_completed_successfully`).
"""

import os
import sys

from create_index_template import create_index_template
from opensearch_client import wait_for_cluster
from register_ollama_model import register_ollama_models

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
WAIT_TIMEOUT = int(os.environ.get("WAIT_TIMEOUT", "120"))


def main():
    wait_for_cluster(ES_URL, WAIT_TIMEOUT)
    create_index_template(ES_URL)
    register_ollama_models(ES_URL)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"OpenSearch bootstrap failed: {e}", file=sys.stderr)
        sys.exit(1)
