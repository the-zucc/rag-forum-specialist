#!/usr/bin/env python3
r"""RAG agent over the `knowledge` index, using LangGraph, Ollama, and OpenSearch.

Answers a question from the knowledge pieces distilled by the knowledge
processor (../knowledge-processing/) — and grows that index as a side effect
of answering. Two loops, run in sequence (see RAG-agent.md for the design):

  loop A (knowledge fetching):
    planner --(gap: keywords)--> search --> planner --> ...
  loop B (thread mining):
    reconstruct --> distill --(leads: more threads)--> reconstruct --> ...
  then: answer

- planner: distills the query into keywords aimed at what is still missing;
  searching stops when the retained pieces cover the question (or a piece
  answers it directly — the "direct hit" fast path).
- search: hybrid query over `knowledge` — each keyword individually as a
  wildcard query, then all keywords in combination alongside a knn clause on
  the embedded query. Hits accumulate and deduplicate across rounds.
- reconstruct: follows the retained pieces' thread_ids back to `forum-posts`
  and rebuilds each source thread in chronological order.
- distill: re-reads each thread with the knowledge processor's extraction
  prompt, attention directed at the user's question, and upserts the new
  standalone pieces back into `knowledge` (same content-hash id scheme).
  Then judges whether the cross-thread picture closes the question or leads
  warrant reconstructing more threads.
- answer: composes the final answer from the pieces and the reconstructed
  threads, citing thread URLs — or says plainly that the sources don't
  contain the answer.

Every LLM call streams to the terminal (one-shot mode). Standard library
HTTP (urllib) plus langgraph, matching the repo's conventions.

With --serve, instead of answering one question from argv and exiting, it
starts an OpenAI-compatible HTTP API (POST /v1/chat/completions) that runs
this same graph per request — see server.py.

This is the CLI entrypoint; the implementation lives in the sibling modules:
clients.py (HTTP), parsing.py (model output), knowledge.py (index search +
write-back), threads.py (reconstruction), prompts.py, graph.py (the graph),
server.py (the --serve HTTP API).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from clients import DEFAULT_NUM_CTX, count_docs, wait_for_cluster
from graph import ask_rag
from server import serve

logger = logging.getLogger("rag")

DEFAULT_ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
DEFAULT_KNOWLEDGE_INDEX = os.environ.get("KNOWLEDGE_INDEX", "knowledge")
DEFAULT_SOURCE_INDEX = os.environ.get("SOURCE_INDEX", "forum-posts")
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="?",
                        help="The question to answer (omit this when --serve is set)")
    parser.add_argument(
        "--serve", action="store_true",
        help="Serve an OpenAI-compatible HTTP API (POST /v1/chat/completions) that runs "
        "this graph per request, instead of answering one question and exiting",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind when --serve is set")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind when --serve is set")
    parser.add_argument("--opensearch-url", default=DEFAULT_ES_URL, help="OpenSearch base URL")
    parser.add_argument("--knowledge-index", default=DEFAULT_KNOWLEDGE_INDEX,
                        help="Knowledge index searched and written back to")
    parser.add_argument("--source-index", default=DEFAULT_SOURCE_INDEX,
                        help="Posts index threads are reconstructed from")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama base URL")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL,
                        help="Planner / distiller / answerer model")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL,
                        help="Ollama embedding model (768-dim, same as the index)")
    parser.add_argument("--max-fetch-rounds", type=int, default=3,
                        help="Loop A budget: keyword -> search rounds")
    parser.add_argument("--max-thread-rounds", type=int, default=2,
                        help="Loop B budget: reconstruction -> distillation rounds")
    parser.add_argument("--thread-char-budget", type=int, default=12000,
                        help="Trim reconstructed threads longer than this")
    parser.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX,
                        help="Ollama context window")
    parser.add_argument("--wait-timeout", type=int, default=60,
                        help="Seconds to wait for the cluster. 0 or less waits forever.")
    parser.add_argument("--log-level", default="INFO",
                        help="Python logging level (e.g. DEBUG, INFO)")
    args = parser.parse_args()
    if not args.serve and not args.question:
        parser.error("the 'question' argument is required unless --serve is set")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    wait_for_cluster(args.opensearch_url, args.wait_timeout)
    if count_docs(args.opensearch_url, args.knowledge_index) == 0:
        message = (
            "The '%s' index is empty — this agent answers from distilled knowledge, "
            "not raw posts. Run the knowledge processor first (make knowledge)."
        )
        if args.serve:
            # A long-running server shouldn't refuse to start over a corpus
            # that may still be empty only because the sweep hasn't reached
            # it yet; the knowledge processor keeps filling it in the
            # background (see knowledge-processing/knowledge-processor.md).
            logger.warning(message, args.knowledge_index)
        else:
            logger.error(message, args.knowledge_index)
            sys.exit(2)

    if args.serve:
        serve(args)
        return

    answer = ask_rag(args.question, args)
    sys.exit(0 if answer else 1)


if __name__ == "__main__":
    main()
