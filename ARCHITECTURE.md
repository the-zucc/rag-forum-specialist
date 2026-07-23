# Architecture Overview: Forum Scraper & RAG Pipeline

This repository contains a complete pipeline for scraping forum content, processing it into structured knowledge, and interacting with it using an agentic Retrieval-Augmented Generation (RAG) system.

## System Components

The system is composed of several decoupled services that work together in a data-driven pipeline.

### 1. Data Acquisition: Crawler
- **Input**: Forum URLs.
- **Process**: Uses Selenium/SeleniumBase to navigate forum pages, handle dynamic content, and scrape thread data.
- **Output**: Writes raw post data into the `threads/` directory in a structured format: `threads/<thread_id>/posts.arg`.
- **Storage**: Local filesystem (shared volume).

### 2. Data Ingestion: Ingest Service
- **Input**: JSON files in `threads/`.
- **Process**: Watches the `threads/` directory for new content. For each new post, it uses a local Ollama instance to generate embeddings (e.g., using `nomic-embed-text`) and indexes the text into OpenSearch.
- **Output**: Populates the `forum-posts` index in OpenSearch.
- **Dependencies**: Requires OpenSearch and Ollama.

### 3. Knowledge Distillation: Knowledge Processor
- **Input**: Documents from the `forum-posts` index.
- **Process**: A background worker that periodically (or via loop) selects high-quality/popular threads. It uses an LLM (via Ollama) to "distill" a thread of posts into a single, coherent "knowledge piece." This knowledge piece is then embedded and indexed.
- **Output**: Populates the `knowledge` index in Opensearch. Tracks progress in `knowledge-processor-status`.
- **Feature**: Uses importance/popularity ranking to decide which threads to process next.

### 4. Retrieval & Reasoning: RAG Agent (rag-serve)
- **Input**: Natural language queries via an OpenAI-compatible API or CLI.
- **Process**: Utils LangGraph to implement an agentic loop. It can search the `knowledge` index, reason about retrieved information, and even look for "leads" in the text that suggest further searching might be needed.
- **Output**: Structured, answer or conversational responses.
- **Interface**: Accessible via HTTP (POST `/v1/chat/completions`) or CLI.

### 5. User Interface: Web UI
- **Input**: User queries entered into a web browser.
- **Process**: A lightweight static HTML page that communicates directly with the `rag-serve` container's API.
- **Interface**: Accessible via HTTP (Port 3000).

---

## Data Flow Diagram

```mermaid
graph TD
    subgraph "Data Acquisition"
        A[Crawler] -->|Writes JSON| B(threads/ directory)
    end

    subgraph "Ingestion Pipeline"
        B --> C[Ingest Service]
        C -->|Embeds & Indexes| D[(OpenSearch: forum-posts)]
    end

    subgraph "Knowledge Processing"
        D --> E[Knowledge Processor]
        E -->|Distills & Summarizes| F[(OpenSearch: knowledge)]
        E -.->|Tracks Progress| G[(OpenSearch: status)]
    end

    subgraph "RAG Agentic Loop"
        F --> H[RAG Agent (rag-serve)]
        I[User Query] --> H
        H -->|Queries| F
    end

    subgraph "Frontend"
        J[Web UI] -->|API Calls| H
    end

    subgraph "External Services"
        K[Ollama LLM/Embed] <--> C
        K <--> E
        K <--> H
    end
```

## Key Infrastructure Components

### OpenSearch
The backbone of the system, serving as both a vector database for embeddings and a full-text search engine for all indexed forum content.

### Ollama
Provides local execution of Large Language Models (LLMs) for text generation and Embedding models for vector generation, ensuring the entire pipeline can run locally without external API costs or privacy concerns.

## Configuration & Orchestration
- **Docker Compose**: Manies all services, networking, volumes, and dependencies.
- **Makefile**: Provides a high-level interface for common development tasks (`make up`, `make ingest`, `make knowledge`, `make ask`).
