#!/usr/bin/env python3
r"""Agentic RAG pipeline using LangGraph, Ollama, and OpenSearch.

Instead of a single search-then-answer pass, the model researches the forum
iteratively — the way a person would: search, read, notice what's still
unclear, search again, and only answer once it understands the subject.

  START -> plan --(need more info)--> search --> plan --> ...
             \--(confident / out of budget)--> answerer --> END

- plan: the model reflects on the question, its accumulated understanding, and
  the documents from the latest search. It updates its understanding, then
  decides either DONE (it can answer confidently) or CONTINUE with the next
  search keywords aimed at whatever is still missing.
- search: embeds the keywords and runs one OpenSearch query combining a knn
  clause (vector_field) and a multi_match clause (post text fields) in a single
  bool/should. Relevant hits are *accumulated and deduplicated* across
  iterations, so nothing potentially useful is thrown away between rounds.
- answerer: answers the original question from everything kept, or says the
  posts don't contain the answer rather than guessing.

Every step is logged (see --log-level) so the model's research is observable.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

logger = logging.getLogger("rag")

TOP_K = 5
DEFAULT_MAX_ITERATIONS = 3
DOC_SNIPPET_CHARS = 1200


class RagState(TypedDict, total=False):
    question: str
    max_iterations: int
    iteration: int
    # Accumulated free-text understanding of the subject, grown each round.
    notes: str
    # Deduplicated kept documents, keyed by post_url/id.
    documents: dict[str, dict]
    # Documents returned by the most recent search (for the model to reflect on).
    recent: list[dict]
    # Keywords already searched, to discourage repeats.
    queries: list[str]
    # Next search to run, and the model's continue/stop decision.
    pending_query: str
    decision: str
    answer: str


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only, matching the rest of the repo)
# --------------------------------------------------------------------------- #
def _post_json(url: str, body: dict) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed: {e.code} {detail}") from e


def ollama_generate(ollama_url: str, model: str, prompt: str) -> str:
    result = _post_json(
        f"{ollama_url}/api/generate",
        {"model": model, "prompt": prompt, "stream": False},
    )
    return result.get("response", "")


def ollama_embed(ollama_url: str, model: str, text: str) -> list[float]:
    result = _post_json(f"{ollama_url}/api/embeddings", {"model": model, "prompt": text})
    return result["embedding"]


def opensearch_hybrid_search(
    opensearch_url: str, index_name: str, query_text: str, vector: list[float], k: int = TOP_K
) -> list[dict]:
    """Single query combining a knn clause and a multi_match clause in one bool/should."""
    body = {
        "size": k,
        "query": {
            "bool": {
                "should": [
                    {"knn": {"vector_field": {"vector": vector, "k": k}}},
                    {
                        "multi_match": {
                            "query": query_text,
                            "fields": ["body_text", "message_text", "thread_title^2"],
                        }
                    },
                ]
            }
        },
        "_source": [
            "body_text",
            "message_text",
            "thread_title",
            "post_url",
            "thread_url",
            "author",
        ],
    }
    result = _post_json(f"{opensearch_url}/{index_name}/_search", body)
    return result.get("hits", {}).get("hits", [])


# --------------------------------------------------------------------------- #
# Formatting / parsing helpers
# --------------------------------------------------------------------------- #
def hit_to_document(hit: dict) -> dict | None:
    source = hit.get("_source", {})
    text = source.get("body_text") or source.get("message_text")
    if not text:
        return None
    return {
        "key": source.get("post_url") or hit.get("_id"),
        "text": text,
        "thread_title": source.get("thread_title"),
        "post_url": source.get("post_url"),
        "author": (source.get("author") or {}).get("name"),
        "score": hit.get("_score"),
    }


def format_documents(documents: list[dict], max_chars: int = DOC_SNIPPET_CHARS) -> str:
    blocks = []
    for i, doc in enumerate(documents, 1):
        text = doc["text"]
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        blocks.append(f'[{i}] From "{doc["thread_title"]}" by {doc["author"]}:\n{text}')
    return "\n\n".join(blocks)


# [ \t]* (not \s*) so an empty "LEARNED:" line doesn't let (.*?) swallow the
# following STATUS:/NEXT_QUERY: lines as the learned text.
_PLAN_LEARNED_RE = re.compile(r"LEARNED:[ \t]*(.*?)(?=\n\s*STATUS:|\Z)", re.S | re.I)
_PLAN_STATUS_RE = re.compile(r"STATUS:\s*([A-Za-z]+)", re.I)
_PLAN_QUERY_RE = re.compile(r"NEXT_QUERY:\s*(.+)", re.I)


def parse_plan(text: str) -> tuple[str, str, str]:
    """Parse the LEARNED / STATUS / NEXT_QUERY block the plan node asks for."""
    learned = m.group(1).strip() if (m := _PLAN_LEARNED_RE.search(text)) else ""
    status = m.group(1).strip().upper() if (m := _PLAN_STATUS_RE.search(text)) else "CONTINUE"
    next_query = m.group(1).strip() if (m := _PLAN_QUERY_RE.search(text)) else ""
    status = "DONE" if "DONE" in status else "CONTINUE"
    return learned, status, next_query


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_graph(
    opensearch_url: str = "http://localhost:9200",
    index_name: str = "forum-posts",
    ollama_url: str = "http://localhost:11434",
    embed_model: str = "nomic-embed-text",
    llm_model: str = "llama2",
):
    """Build the compiled LangGraph app for the research loop above."""

    def plan(state: RagState) -> dict:
        question = state["question"]
        notes = state.get("notes", "")
        documents = state.get("documents", {})
        recent = state.get("recent", [])
        queries = state.get("queries", [])
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", DEFAULT_MAX_ITERATIONS)

        # Out of search budget with something to work with: don't spend an LLM
        # call reflecting, just go answer (route sends DONE+docs to the answerer).
        if iteration >= max_iterations and documents:
            logger.info("[plan] iteration=%d at budget; answering with what's kept", iteration)
            return {"decision": "DONE"}

        if not documents:
            # First pass: no research yet, just pick the opening search.
            prompt = (
                "You are researching a forum to answer a user's question. Think about "
                "what a forum post containing the answer would actually say, and choose "
                "the best initial search keywords or short phrases to find it — not a "
                "rephrasing of the question.\n\n"
                f"Question: {question}\n\n"
                "Respond in exactly this format:\n"
                "LEARNED:\n"
                "STATUS: CONTINUE\n"
                "NEXT_QUERY: <search keywords/phrases>"
            )
        else:
            recent_text = format_documents(recent) or "(no new documents found)"
            tried = "; ".join(queries) or "(none)"
            prompt = (
                "You are researching a forum to answer a user's question, one search at "
                "a time. Below is your current understanding and the documents from your "
                "latest search. Integrate the new information into your understanding, "
                "then decide whether you now understand the subject well enough to answer "
                "confidently, or whether you should search again for something specific "
                "that is still missing.\n\n"
                f"Question: {question}\n\n"
                f"Your understanding so far:\n{notes or '(nothing yet)'}\n\n"
                f"Documents from your latest search:\n{recent_text}\n\n"
                f"Searches already tried: {tried}\n\n"
                "Respond in exactly this format:\n"
                "LEARNED: <your updated understanding, integrating the new documents>\n"
                "STATUS: DONE if you can now answer confidently, otherwise CONTINUE\n"
                "NEXT_QUERY: <if CONTINUE, keywords for a DIFFERENT search that fills the "
                "gap; avoid repeating earlier searches>"
            )

        raw = ollama_generate(ollama_url, llm_model, prompt)
        learned, status, next_query = parse_plan(raw)

        # Guard: the very first pass must run at least one search.
        if not documents:
            status = "CONTINUE"
            if not next_query:
                next_query = question

        new_notes = learned or notes
        logger.info("[plan] iteration=%d decision=%s", iteration, status)
        if next_query and status == "CONTINUE":
            logger.info("[plan] next search: %r", next_query)
        if new_notes:
            logger.info("[plan] understanding: %s", _truncate(new_notes, 240))
            logger.debug("[plan] full understanding:\n%s", new_notes)

        return {"notes": new_notes, "decision": status, "pending_query": next_query}

    def search(state: RagState) -> dict:
        query = state["pending_query"]
        iteration = state.get("iteration", 0) + 1
        logger.info("[search] iteration=%d querying OpenSearch for: %r", iteration, query)

        vector = ollama_embed(ollama_url, embed_model, query)
        hits = opensearch_hybrid_search(opensearch_url, index_name, query, vector)

        documents = dict(state.get("documents", {}))
        recent: list[dict] = []
        new_count = 0
        for hit in hits:
            doc = hit_to_document(hit)
            if not doc:
                continue
            recent.append(doc)
            if doc["key"] not in documents:
                documents[doc["key"]] = doc
                new_count += 1

        logger.info(
            "[search] %d hits, %d relevant, %d new (kept %d total)",
            len(hits), len(recent), new_count, len(documents),
        )
        return {
            "documents": documents,
            "recent": recent,
            "queries": state.get("queries", []) + [query],
            "iteration": iteration,
        }

    def answerer(state: RagState) -> dict:
        documents = list(state.get("documents", {}).values())
        logger.info("[answer] generating answer from %d kept document(s)", len(documents))
        if documents:
            context = format_documents(documents)
        else:
            context = "No relevant forum posts were found."
        prompt = (
            "Use the following forum posts to answer the question. If the posts don't "
            "contain the answer, say so plainly instead of guessing.\n\n"
            f"Context:\n{context}\n\n"
            f"Question: {state['question']}\n\n"
            "Answer:"
        )
        answer = ollama_generate(ollama_url, llm_model, prompt).strip()
        return {"answer": answer}

    def route(state: RagState) -> str:
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", DEFAULT_MAX_ITERATIONS)
        if iteration >= max_iterations:
            logger.info("[route] hit max_iterations=%d -> answer", max_iterations)
            return "answerer"
        if state.get("decision") == "DONE" and state.get("documents"):
            logger.info("[route] model is confident -> answer")
            return "answerer"
        if not state.get("pending_query"):
            logger.info("[route] no further query proposed -> answer")
            return "answerer"
        logger.info("[route] continuing research -> search")
        return "search"

    graph = StateGraph(RagState)
    graph.add_node("plan", plan)
    graph.add_node("search", search)
    graph.add_node("answerer", answerer)
    graph.add_edge(START, "plan")
    graph.add_conditional_edges("plan", route, {"search": "search", "answerer": "answerer"})
    graph.add_edge("search", "plan")
    graph.add_edge("answerer", END)
    return graph.compile()


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def ask_rag(app, question: str, max_iterations: int = DEFAULT_MAX_ITERATIONS) -> dict:
    """Run the research graph for a question.

    Returns a dict with 'answer', 'understanding' (the model's final notes),
    'queries' (every search it ran), and 'sources' (the documents it kept).
    """
    logger.info("Question: %s", question)
    final_state = app.invoke(
        {"question": question, "max_iterations": max_iterations},
        # plan->search->plan repeats up to max_iterations times, plus answerer.
        {"recursion_limit": max_iterations * 2 + 5},
    )
    documents = list(final_state.get("documents", {}).values())
    logger.info(
        "Done: %d search(es), %d document(s) kept",
        len(final_state.get("queries", [])), len(documents),
    )
    return {
        "answer": final_state.get("answer", ""),
        "understanding": final_state.get("notes", ""),
        "queries": final_state.get("queries", []),
        "sources": [
            {"content": doc["text"], "metadata": {k: v for k, v in doc.items() if k not in ("text", "key")}}
            for doc in documents
        ],
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agentic RAG pipeline for forum posts")
    parser.add_argument("question", help="Question to ask the RAG system")
    parser.add_argument(
        "--opensearch-url",
        default="http://localhost:9200",
        help="OpenSearch URL",
    )
    parser.add_argument(
        "--index-name",
        default="forum-posts",
        help="OpenSearch index name",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama base URL",
    )
    parser.add_argument(
        "--embed-model",
        default="nomic-embed-text",
        help="Embedding model in Ollama",
    )
    parser.add_argument(
        "--llm-model",
        default="llama2",
        help="LLM model in Ollama",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help="Maximum number of search rounds before the model must answer.",
    )
    parser.add_argument("--log-level", default="INFO", help="Python logging level (e.g. DEBUG, INFO).")

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("Building RAG graph...")
    app = build_graph(
        opensearch_url=args.opensearch_url,
        index_name=args.index_name,
        ollama_url=args.ollama_url,
        embed_model=args.embed_model,
        llm_model=args.llm_model,
    )

    result = ask_rag(app, args.question, max_iterations=args.max_iterations)

    print(f"\nSearches run: {result['queries']}\n")
    print(f"Answer:\n{result['answer']}\n")

    if result["sources"]:
        print("Sources:")
        for i, source in enumerate(result["sources"], 1):
            print(f"\n{i}. {source['metadata']}")
            print(f"   {source['content'][:200]}...")
