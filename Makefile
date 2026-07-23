ES_URL ?= http://localhost:9200
INDEX_NAME ?= forum-posts
DEST_DIR ?= threads
COMPOSE ?= docker compose
OLLAMA_URL ?= http://localhost:11434
LLM_MODEL ?= gemma4:26b
EMBED_MODEL ?= nomic-embed-text
EMBED_DIMENSION ?= 768
KNOWLEDGE_INDEX ?= knowledge
STATUS_INDEX ?= knowledge-processor-status
LOOP ?=
MARK_ALL_PROCESSED ?=
POLL_INTERVAL ?= 1800

RAG_IMAGE ?= rag
RAG_BUILD_STAMP ?= rag/.rag-build.sha256
RAG_BUILD_INPUTS := rag/Dockerfile Pipfile $(wildcard rag/*.py)
RAG_PORT ?= 8000
WEBUI_PORT ?= 3000
WEBUI_URL := http://localhost:$(WEBUI_PORT)

.PHONY: up down logs bootstrap ingest knowledge clean ask rag-build rag-serve

# Self-contained: brings up OpenSearch, OpenSearch Dashboards, opensearch-bootstrap
# (index template + Ollama model registration, before ingestion starts), the
# ingestion pipeline (watches threads/ and keeps the index in sync), the
# knowledge processor (sweeps the popularity ranking window by window,
# forever, into the knowledge index), the RAG agent's OpenAI-compatible API
# (rag-serve, on $(RAG_PORT)), the chat web UI in front of it (rag-webui, on
# $(WEBUI_PORT)), and the scraper for board 9 (tuning & troubleshooting) in
# serve mode, all in the background. Builds the images from scratch (no layer
# cache) so code changes always take, then waits for the web UI to answer and
# opens it in a browser.
up:
	$(COMPOSE) build --no-cache
	export LLM_MODEL=$(LLM_MODEL); export EMBED_MODEL=$(EMBED_MODEL); $(COMPOSE) up -d
	@if command -v curl >/dev/null 2>&1; then \
		echo "Waiting for the web UI at $(WEBUI_URL) ..."; \
		for i in $$(seq 1 60); do \
			curl -sf $(WEBUI_URL) >/dev/null 2>&1 && break; \
			sleep 1; \
		done; \
	fi; \
	if command -v xdg-open >/dev/null 2>&1; then xdg-open $(WEBUI_URL); \
	elif command -v open >/dev/null 2>&1; then open $(WEBUI_URL); \
	else echo "Web UI ready: open $(WEBUI_URL) in your browser."; fi

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
	python3 etl/ingestion/ingest.py \
		--es-url $(ES_URL) \
		--index $(INDEX_NAME) \
		--dest-dir $(DEST_DIR)

# One-off local run of the knowledge processor, without Docker: processes one
# ranked window of not-yet-processed threads in $(INDEX_NAME) into the
# $(KNOWLEDGE_INDEX) index and exits — a thread already marked processed in
# $(STATUS_INDEX) is skipped, so re-running is idempotent. Requires OpenSearch
# (make up) and a local Ollama with $(LLM_MODEL)/$(EMBED_MODEL) pulled. Pass
# LOOP=1 to sweep window by window forever instead (same as the
# `knowledge-processor` compose service), e.g. `make knowledge LOOP=1
# POLL_INTERVAL=300`. Pass MARK_ALL_PROCESSED=1 to bulk-mark every currently-
# ranked thread as processed without distilling anything, e.g. to seed
# $(STATUS_INDEX) around a `knowledge` index built before this tracking
# existed: `make knowledge MARK_ALL_PROCESSED=1`.
knowledge:
	python3 etl/knowledge-processing/knowledge_processor.py \
		--opensearch-url $(ES_URL) \
		--source-index $(INDEX_NAME) \
		--knowledge-index $(KNOWLEDGE_INDEX) \
		--status-index $(STATUS_INDEX) \
		--ollama-url $(OLLAMA_URL) \
		--llm-model $(LLM_MODEL) \
		--embed-model $(EMBED_MODEL) \
		--wait-timeout 60 \
		$(if $(MARK_ALL_PROCESSED),--mark-all-processed) \
		$(if $(LOOP),--loop --poll-interval $(POLL_INTERVAL))

clean:
	$(COMPOSE) down -v

# Build the RAG CLI image (rag/*.py + its dependencies), separate from
# the scraper app image, from scratch (no layer cache) so code changes always
# take — but only when there's a code change to take: the SHA-256 of the
# Dockerfile, Pipfile, and rag/*.py is stashed in $(RAG_BUILD_STAMP)
# after each build, and a rebuild is skipped when that hash and the image
# both still match. Delete $(RAG_BUILD_STAMP) (or `docker rmi $(RAG_IMAGE)`) 
# to force one.
rag-build:
	@current_sha=$$(cat $(RAG_BUILD_INPUTS) | sha256sum | cut -d' ' -f1); \
	if [ -f $(RAG_BUILD_STAMP) ] && [ "$$(cat $(RAG_BUILD_STAMP))" = "$${current_sha}" ]; then \
		echo "rag-build: '$(RAG_IMAGE)' is up to date (Dockerfile/Pipfile/rag/*.py unchanged); skipping build."; \
	else \
		docker build --no-cache -f rag/Dockerfile -t $(RAG_IMAGE) . \
		    && echo "$${current_sha}" > $(RAG_BUILD_STAMP); \
	fi

# Demonstration query against the RAG CLI, run in its own container.
# Requires OpenSearch (make up), a populated $(KNOWLEDGE_INDEX) index
# (make knowledge), and a local Ollama with $(LLM_MODEL)/$(EMBED_MODEL)
# pulled. Uses host networking so the container can reach OpenSearch and
# Ollama on localhost.
ask: rag-build
	docker run --rm -t --network host $(RAG_IMAGE) \
	    "how do I get rid of the hesitation around 3-4000 rpm on my Suzuki RE5?" \
	    --opensearch-url $(ES_URL) \
	    --knowledge-index $(KNOWLEDGE_INDEX) \
	    --source-index $(INDEX_NAME) \
	    --ollama-url $(OLLAMA_URL) \
	    --llm-model $(LLM_MODEL) \
	    --embed-model $(EMBED_MODEL)

# Serves the RAG agent as an OpenAI-compatible HTTP API (POST
# /v1/chat/completions) in its own container, on $(RAG_PORT), instead of
# answering one CLI question. Same prerequisites as `make ask`: OpenSearch
# (make up), a populated $(KNOWLEDGE_INDEX) index (make knowledge), and a
# local Ollama with $(LLM_MODEL)/$(EMBED_MODEL) pulled. Uses host networking
# so the container can reach OpenSearch/Ollama on localhost and bind
# $(RAG_PORT) directly (same as the `rag-serve` compose service). Runs in
# the foreground; Ctrl-C to stop.
rag-serve: rag-build
	docker run --rm -t --network host $(RAG_IMAGE) \
	    --serve --host 0.0.0.0 --port $(RAG_PORT) \
	    --opensearch-url $(ES_URL) \
	    --knowledge-index $(KNOWLEDGE_INDEX) \
	    --source-index $(INDEX_NAME) \
	    --ollama-url $(OLLAMA_URL) \
	    --llm-model $(LLM_MODEL) \
	    --embed-model $(EMBED_MODEL)
