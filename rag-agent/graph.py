"""The LangGraph graph: state, nodes, edges (see RAG-agent.md).

  loop A (knowledge fetching):
    planner --(gap: keywords)--> search --> planner --> ...
  loop B (thread mining):
    reconstruct --> distill --(leads: more threads)--> reconstruct --> ...
  then: answer
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from clients import ollama_stream
from knowledge import index_piece, search_knowledge
from parsing import MAX_KEYWORDS, parse_field, parse_keywords, parse_pieces
from prompts import (
    build_answer_prompt,
    build_crosscheck_prompt,
    build_distill_prompt,
    build_planner_prompt,
)
from threads import fetch_thread_posts, render_thread

logger = logging.getLogger("rag")

# Bound how much of the corpus one answer can pull in.
MAX_THREADS_PER_ROUND = 5


class RagState(TypedDict, total=False):
    query: str
    status: str                    # planner decision: search | reconstruct | answer
    keywords: list                 # current round's search keywords
    tried: list                    # keyword sets already searched (lists of str)
    fetch_rounds: int              # loop A rounds used
    thread_rounds: int             # loop B rounds used
    pieces: dict                   # retained knowledge pieces, by doc id
    pending_threads: list          # thread ids queued for reconstruction
    threads: dict                  # reconstructed threads, by thread id
    mined: list                    # thread ids already distilled
    answer: str


def build_graph(cfg):
    def llm(label, prompt):
        logger.info("[%s]", label)
        return ollama_stream(cfg.ollama_url, cfg.llm_model, prompt, num_ctx=cfg.num_ctx)

    def threads_of(pieces, piece_ids=None):
        """Thread ids referenced by the given pieces, in retrieval order."""
        ordered = []
        for pid, piece in pieces.items():
            if piece_ids is not None and pid not in piece_ids:
                continue
            tid = piece.get("thread_id")
            if tid and tid not in ordered:
                ordered.append(tid)
        return ordered

    def cited_post_ids(pieces):
        """thread_id -> the post ids the retained pieces cite in it."""
        cited = {}
        for piece in pieces.values():
            for sp in piece.get("source_posts", []):
                if sp.get("post_id"):
                    cited.setdefault(piece.get("thread_id"), set()).add(str(sp["post_id"]))
        return cited

    # ---- planner: stages 1 + 3 — emit keywords, or judge sufficiency ------- #
    def planner(state):
        pieces = state.get("pieces", {})
        tried = state.get("tried", [])
        fetch_rounds = state.get("fetch_rounds", 0)
        budget_left = fetch_rounds < cfg.max_fetch_rounds

        raw = llm("planner", build_planner_prompt(state["query"], pieces.values(),
                                                  tried, budget_left))
        status = parse_field(raw, "STATUS").upper()

        if "DIRECT" in status and pieces:
            ordered = list(pieces)
            numbers = [int(n) for n in re.findall(r"\d+", parse_field(raw, "HIT_PIECES"))]
            hit_ids = {ordered[n - 1] for n in numbers if 1 <= n <= len(ordered)}
            pending = threads_of(pieces, hit_ids or None)
            logger.info("Direct hit — reconstructing only its thread(s): %s", pending)
            return {"status": "reconstruct", "pending_threads": pending}

        if "SEARCH" in status and budget_left:
            keywords = parse_keywords(parse_field(raw, "KEYWORDS"))
            if not keywords and not pieces:
                # Unparseable first round: fall back to the query's own terms.
                keywords = re.findall(r"[A-Za-z0-9-]{3,}", state["query"])[:MAX_KEYWORDS]
            kwset = frozenset(k.lower() for k in keywords)
            if keywords and kwset not in (frozenset(k.lower() for k in t) for t in tried):
                return {
                    "status": "search",
                    "keywords": keywords,
                    "tried": tried + [keywords],
                    "fetch_rounds": fetch_rounds + 1,
                }
            logger.info("Keywords repeat an already-tried search — nothing left to try.")

        if not pieces:
            logger.info("No knowledge pieces retrieved — nothing to reconstruct.")
            return {"status": "answer", "pending_threads": []}
        return {"status": "reconstruct", "pending_threads": threads_of(pieces)}

    def planner_route(state):
        return state["status"]

    # ---- search: stage 2 ---------------------------------------------------- #
    def search(state):
        keywords = state["keywords"]
        logger.info("Searching knowledge for: %s", ", ".join(keywords))
        pieces = dict(state.get("pieces", {}))
        before = len(pieces)
        pieces.update(
            search_knowledge(cfg.opensearch_url, cfg.knowledge_index, cfg.ollama_url,
                             cfg.embed_model, state["query"], keywords)
        )
        logger.info("Retained %d piece(s) (+%d new)", len(pieces), len(pieces) - before)
        return {"pieces": pieces}

    # ---- reconstruct: stage 4 ----------------------------------------------- #
    def reconstruct(state):
        threads = dict(state.get("threads", {}))
        pieces = state.get("pieces", {})
        pending = [t for t in state.get("pending_threads", []) if t not in threads]
        for tid in pending[:MAX_THREADS_PER_ROUND]:
            piece = next((p for p in pieces.values() if p.get("thread_id") == tid), {})
            posts = fetch_thread_posts(cfg.opensearch_url, cfg.source_index, tid)
            threads[tid] = {
                "thread_id": tid,
                "title": piece.get("thread_title") or f"thread {tid}",
                "url": piece.get("thread_url"),
                "posts": posts,
            }
            logger.info('Reconstructed "%s" (%d posts)', threads[tid]["title"], len(posts))
        return {"threads": threads, "pending_threads": []}

    # ---- distill: stages 5 + 6 ---------------------------------------------- #
    def distill(state):
        pieces = dict(state.get("pieces", {}))
        threads = state["threads"]
        mined = list(state.get("mined", []))
        cited = cited_post_ids(pieces)

        for tid, thread in threads.items():
            if tid in mined:
                continue
            mined.append(tid)
            if not thread["posts"]:
                logger.warning('No readable posts in "%s"; skipping', thread["title"])
                continue
            posts_by_id = {p["id"]: p for p in thread["posts"]}
            text = render_thread(thread["posts"], cfg.thread_char_budget,
                                 cited.get(tid, frozenset()))
            raw = llm(f'distill "{thread["title"]}"',
                      build_distill_prompt(state["query"], thread, text))
            stored = 0
            for statement, post_ids in parse_pieces(raw, set(posts_by_id)):
                try:
                    doc_id, piece = index_piece(
                        cfg.opensearch_url, cfg.knowledge_index, cfg.ollama_url,
                        cfg.embed_model, statement=statement, thread=thread,
                        post_ids=post_ids, posts_by_id=posts_by_id,
                        llm_model=cfg.llm_model,
                    )
                    if doc_id not in pieces:
                        stored += 1
                    pieces[doc_id] = piece
                except Exception as e:
                    logger.error("Failed to index piece %r: %s", statement[:80], e)
            logger.info("Distilled %d new piece(s) into '%s'", stored, cfg.knowledge_index)

        # Stage 6: does the cross-thread picture close the question, or do
        # leads warrant reconstructing more threads?
        thread_rounds = state.get("thread_rounds", 0) + 1
        updates = {"pieces": pieces, "mined": mined,
                   "thread_rounds": thread_rounds, "pending_threads": []}
        if thread_rounds >= cfg.max_thread_rounds:
            logger.info("Thread-mining budget spent — answering.")
            return updates
        raw = llm("cross-thread check",
                  build_crosscheck_prompt(state["query"], pieces.values(), threads.values()))
        if "MORE" not in parse_field(raw, "STATUS").upper():
            return updates
        leads = parse_keywords(parse_field(raw, "LEAD_KEYWORDS"))
        if leads:
            logger.info("Following leads: %s", ", ".join(leads))
            pieces.update(
                search_knowledge(cfg.opensearch_url, cfg.knowledge_index, cfg.ollama_url,
                                 cfg.embed_model, state["query"], leads)
            )
            updates["pieces"] = pieces
        pending = [t for t in threads_of(pieces) if t not in mined]
        if not pending:
            logger.info("Leads produced no unread threads — answering.")
        updates["pending_threads"] = pending
        return updates

    def distill_route(state):
        return "reconstruct" if state.get("pending_threads") else "answer"

    # ---- answer: stage 7 ---------------------------------------------------- #
    def answer(state):
        threads = list(state.get("threads", {}).values())
        pieces = state.get("pieces", {})
        cited = cited_post_ids(pieces)
        thread_texts = {
            t["thread_id"]: render_thread(t["posts"], cfg.thread_char_budget,
                                          cited.get(t["thread_id"], frozenset()))
            for t in threads
        }
        raw = llm("answer", build_answer_prompt(state["query"], pieces.values(),
                                                threads, thread_texts))
        return {"answer": raw.strip()}

    graph = StateGraph(RagState)
    graph.add_node("planner", planner)
    graph.add_node("search", search)
    graph.add_node("reconstruct", reconstruct)
    graph.add_node("distill", distill)
    graph.add_node("answer", answer)
    graph.add_edge(START, "planner")
    graph.add_conditional_edges("planner", planner_route,
                                {"search": "search", "reconstruct": "reconstruct",
                                 "answer": "answer"})
    graph.add_edge("search", "planner")
    graph.add_edge("reconstruct", "distill")
    graph.add_conditional_edges("distill", distill_route,
                                {"reconstruct": "reconstruct", "answer": "answer"})
    graph.add_edge("answer", END)
    return graph.compile()


def ask_rag(question, cfg):
    graph = build_graph(cfg)
    state = graph.invoke({"query": question}, {"recursion_limit": 50})
    return state.get("answer", "")
