#!/usr/bin/env python3
"""Create/update the OpenSearch index template for forum posts.

Idempotent: PUT _index_template is itself an upsert, so re-running just
overwrites the template with the same content.
"""

import json
import os

from opensearch_client import request, wait_for_cluster

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
INDEX_NAME = os.environ.get("INDEX_NAME", "forum-posts")
TEMPLATE_FILE = os.environ.get(
    "TEMPLATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_template.json")
)
WAIT_TIMEOUT = int(os.environ.get("WAIT_TIMEOUT", "60"))


def create_index_template(es_url=ES_URL, index_name=INDEX_NAME, template_file=TEMPLATE_FILE):
    with open(template_file) as f:
        template = json.load(f)
    template.setdefault("index_patterns", [index_name])
    _, body = request(es_url, "PUT", f"/_index_template/{index_name}", template)
    print(f"Index template '{index_name}' created/updated: {body}")


if __name__ == "__main__":
    wait_for_cluster(ES_URL, WAIT_TIMEOUT)
    create_index_template()
