"""Search of and write-back to the `knowledge` index (stages 2 and 5).

The index is built by ../knowledge-processing/; this module shares its
document shape and content-hash id scheme so pieces distilled while answering
coexist with (and refresh) the processor's.
"""

from __future__ import annotations

import hashlib
from typing import Any
import logging
import re
from datetime import datetime, timezone

from clients import ollama_embed, post_json, request

logger = logging.getLogger("rag")

# Search sizes: per-keyword queries cast a wide net, the combined query
# surfaces the pieces where the concepts intersect.
PER_KEYWORD_HITS = 4
COMBINED_HITS = 8

PIECE_SOURCE_FIELDS = [
    "statement", "subject", "thread_id", "thread_title", "thread_url", "source_posts",
]


def _keyword_clause(keyword: str) -> dict[str, Any] | None:
    """`*token*` wildcards over statement/subject, all of a keyword's tokens."""
    tokens = re.findall(r"[a-z0-9]+", keyword.lower())
    if not tokens:
        return None
    return {
        "query_string": {
            "query": " AND ".join(f"*{t}*" for t in tokens),
            "fields": ["statement", "subject", "thread_title"],
            "analyze_wildcard": True,
        }
    }


def _collect_pieces(result: dict[str, Any]) -> dict[string, Any]:
    pieces = {}
    for hit in result.get("hits", {}).get("hits", []):
        source = hit.get("_source", {})
        if not source.get("statement"):
            continue
        pieces[hit["_id"]] = {
            "id": hit["_id"],
            "statement": source["statement"],
            "subject": source.get("subject"),
            "thread_id": str(source.get("thread_id") or ""),
            "thread_title": source.get("thread_title") or "unknown thread",
            "thread_url": source.get("thread_url"),
            "source_posts": source.get("source_posts") or [],
        }
    return pieces


def search_knowledge(
    es_url: str,
    index: str,
    ollama_url: str,
    embed_model: str,
    query: str,
    keywords: list[str],
) -> dict[str, Any]:
    """One search round: each keyword one by one, then all in combination.

    The combined query pairs the wildcard clauses with a knn clause on the
    embedded query+keywords (same hybrid bool.should shape as ingestion
    search), so a piece worded differently from the query can still be found.
    """
    found = {}
    clauses = [(k, c) for k in keywords if (c := _keyword_clause(k))]
    for keyword, clause in clauses:
        body = {"size": PER_KEYWORD_HITS, "query": clause, "_source": PIECE_SOURCE_FIELDS}
        found.update(_collect_pieces(post_json(f"{es_url}/{index}/_search", body)))
        logger.debug("  keyword %r -> %d retained so far", keyword, len(found))

    combined_should = []
    if len(clauses) > 1:
        combined_should.append(
            {
                "query_string": {
                    "query": " AND ".join(
                        "(" + c["query_string"]["query"] + ")" for _, c in clauses
                    ),
                    "fields": ["statement", "subject", "thread_title"],
                    "analyze_wildcard": True,
                }
            }
        )
    try:
        vector = ollama_embed(ollama_url, embed_model, query + "\n" + ", ".join(keywords))
        combined_should.append({"knn": {"vector_field": {"vector": vector, "k": COMBINED_HITS}}})
    except Exception as e:
        logger.warning("  embedding failed (%s); combined search is wildcard-only", e)
    if combined_should:
        body = {
            "size": COMBINED_HITS,
            "query": {"bool": {"should": combined_should, "minimum_should_match": 1}},
            "_source": PIECE_SOURCE_FIELDS,
        }
        found.update(_collect_pieces(post_json(f"{es_url}/{index}/_search", body)))
    return found


def index_piece(
    es_url: str,
    index: str,
    ollama_url: str,
    embed_model: str,
    *,
    statement: str,
    thread: dict[str, Any],
    post_ids: list[str],
    posts_by_id: dict[str, Any],
    llm_model: str,
) -> tuple[str, dict[str, Any]]:
    """Embed one knowledge piece and upsert it by a hash of its normalized text.

    Same id scheme as the knowledge processor, so a fact re-derived from an
    already-mined thread refreshes its document instead of duplicating it.
    Returns (doc_id, piece) in the retained-pieces shape.
    """
    normalized = " ".join(statement.lower().split())
    doc_id = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]
    source_posts = [
        {
            "post_id": pid,
            "post_url": posts_by_id.get(pid, {}).get("post_url"),
            "author": posts_by_id.get(pid, {}).get("author"),
        }
        for pid in post_ids
    ]
    doc = {
        "statement": statement,
        "vector_field": ollama_embed(ollama_url, embed_model, statement),
        "subject": thread["title"],
        "thread_id": thread["thread_id"],
        "thread_title": thread["title"],
        "thread_url": thread["url"],
        "source_posts": source_posts,
        "thread_post_count": len(thread["posts"]),
        "thread_like_count": sum(p["likes"] for p in thread["posts"]),
        "model": llm_model,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request("PUT", f"{es_url}/{index}/_doc/{doc_id}", doc)
    return doc_id, {
        "id": doc_id,
        "statement": statement,
        "subject": thread["title"],
        "thread_id": thread["thread_id"],
        "thread_title": thread["title"],
        "thread_url": thread["url"],
        "source_posts": source_posts,
    }
