ES_URL ?= http://localhost:9200
INDEX_NAME ?= forum-posts
DEST_DIR ?= threads
COMPOSE ?= docker compose
OLLAMA_URL ?= http://localhost:11434
LLM_MODEL ?= gpt-oss:20b
EMBED_MODEL ?= nomic-embed-text
EMBED_DIMENSION ?= 768

RAG_IMAGE ?= rag-agent

.PHONY: up down logs bootstrap ingest clean ask rag-build

# Self-contained: brings up OpenSearch, OpenSearch Dashboards, opensearch-bootstrap
# (index template + Ollama model registration, before ingestion starts), the
# ingestion pipeline (watches threads/ and keeps the index in sync), and the
# scraper for board 9 (tuning & troubleshooting) in serve mode, all in the background.
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

# One-off local run of the index template + Ollama model setup, without Docker.
bootstrap:
	ES_URL=$(ES_URL) INDEX_NAME=$(INDEX_NAME) OLLAMA_MODEL=$(LLM_MODEL) \
		EMBED_MODEL=$(EMBED_MODEL) EMBED_DIMENSION=$(EMBED_DIMENSION) \
		python3 opensearch-bootstrap/bootstrap.py

# One-off local reindex of whatever is currently in threads/, without Docker.
ingest:
	python3 ingestion/ingest.py \
		--es-url $(ES_URL) \
		--index $(INDEX_NAME) \
		--dest-dir $(DEST_DIR)

clean:
	$(COMPOSE) down -v

# Build the RAG CLI image (rag-agent/rag.py + its dependencies), separate from
# the scraper app image.
rag-build:
	docker build -f rag-agent/Dockerfile -t $(RAG_IMAGE) .

# Demonstration query against the RAG CLI, run in its own container.
# Requires OpenSearch (make up) and a local Ollama with
# $(LLM_MODEL)/$(EMBED_MODEL) pulled. Uses host networking so the container
# can reach OpenSearch and Ollama on localhost.
ask: up rag-build
	docker run --rm --network host $(RAG_IMAGE) \
		"how do I get rid of the hesitation around 3-4000 rpm on my Suzuki RE5?" \
		--opensearch-url $(ES_URL) \
		--index-name $(INDEX_NAME) \
		--ollama-url $(OLLAMA_URL) \
		--llm-model $(LLM_MODEL) \
		--embed-model $(EMBED_MODEL)
