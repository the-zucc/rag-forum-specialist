#!/usr/bin/env python3
"""Distill popular forum threads into durable knowledge pieces.

Reads whole threads from the `forum-posts` OpenSearch index — most popular
first, popularity being a thread's post (reply) count — reconstructs each one
in chronological order, asks a local Ollama model to extract the small number
of standalone, durable facts the thread establishes, embeds each fact, and
writes it to a new `knowledge` index.

Each run processes one *window* of the popularity ranking: threads ranked
(--offset + 1) .. (--offset + --top-n), not always the top N. With --loop, the
job never exits: it processes a window, advances the offset by --top-n for
the next one, wraps back to the top once the ranked list is exhausted, and
sleeps --poll-interval between cycles — waiting each time for the source
index to stop growing (the same stabilization wait used on startup), so newly
scraped posts are picked up as the corpus grows.

Threads are processed at most once, ever: before distilling a window, each
thread's id is looked up in a `knowledge-processor-status` index, and any
thread already marked processed there is skipped. A thread is marked
processed immediately after its distillation attempt reaches a terminal
outcome (pieces written, or the model found nothing durable) — not on a
transient failure (Ollama/OpenSearch error), so those retry on the next
sweep. Run with --mark-all-processed to bulk-mark every currently-ranked
thread as processed without distilling anything — useful for seeding the
status index around an existing `knowledge` index built before this
tracking existed.

Standard library only (urllib), like ../ingestion/ingest.py, so it runs without
installing anything extra. See knowledge-processor.md for the design.

  forum-posts ──rank──> window [offset+1, offset+top_n] ──skip processed──> reconstruct──> Ollama ──> knowledge
                                                               |
                                                               v
                                                knowledge-processor-status
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger("knowledge")

DEFAULT_ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
DEFAULT_SOURCE_INDEX = os.environ.get("SOURCE_INDEX", "forum-posts")
DEFAULT_KNOWLEDGE_INDEX = os.environ.get("KNOWLEDGE_INDEX", "knowledge")
DEFAULT_STATUS_INDEX = os.environ.get("STATUS_INDEX", "knowledge-processor-status")
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-oss:20b")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")

# How much of a reconstructed thread to render into one post before trimming.
THREAD_POST_CHARS = 1200
# Most posts we'll ever pull for one thread (the corpus tops out well under this).
THREAD_FETCH_MAX_POSTS = 500
# Ollama silently defaults to a 4096-token context and truncates past it, which
# would cut off a reconstructed thread. Give plenty of headroom over the
# thread char budget (~3-4k tokens) plus the prompt scaffolding and output.
DEFAULT_NUM_CTX = 32768
# Ignore obviously-empty "facts" the model might emit.
MIN_STATEMENT_CHARS = 15
# Terms-agg size used by --mark-all-processed to rank every eligible thread at
# once (no windowing) — the corpus tops out well under this.
ALL_THREADS_SIZE = 100_000


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only, matching the rest of the repo)
# --------------------------------------------------------------------------- #
def request(method, url, body=None, content_type="application/json"):
    data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {e.code} {detail}") from e


def get_json(url):
    return request("GET", url)[1]


def post_json(url, body):
    return request("POST", url, body)[1]


def ollama_generate(ollama_url, model, prompt, num_ctx=DEFAULT_NUM_CTX):
    """Non-streaming completion — this is a batch job, no terminal to stream to."""
    body = {"model": model, "prompt": prompt, "stream": False, "options": {"num_ctx": num_ctx}}
    result = post_json(f"{ollama_url}/api/generate", body)
    return result.get("response", "")


def ollama_embed(ollama_url, model, text):
    result = post_json(f"{ollama_url}/api/embeddings", {"model": model, "prompt": text})
    return result["embedding"]


# --------------------------------------------------------------------------- #
# Waiting for the cluster and for ingestion to have caught up
# --------------------------------------------------------------------------- #
def wait_for_cluster(es_url, timeout):
    """Poll until the cluster responds. timeout <= 0 means wait forever."""
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    last_error = None
    while deadline is None or time.monotonic() < deadline:
        try:
            body = get_json(f"{es_url}/_cluster/health")
            if body.get("status") in ("green", "yellow"):
                logger.info("Cluster is up (status=%s)", body.get("status"))
                return
        except Exception as e:
            last_error = e
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for cluster at {es_url}: {last_error}")


def count_docs(es_url, index):
    try:
        return int(get_json(f"{es_url}/{index}/_count").get("count", 0))
    except RuntimeError as e:
        if "404" in str(e):  # index not created yet
            return 0
        raise


def wait_for_posts(es_url, index, timeout, poll_interval=5, stable_polls=2):
    """Wait until the source index has posts and its count stops growing.

    The ingestion service runs as a watcher and never "completes", so this is
    how we run *after ingestion has caught up*: proceed once the post count is
    non-zero and unchanged across a couple of polls. If the timeout is hit
    first, proceed with whatever is there (logged), rather than hanging.
    """
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    last, stable = -1, 0
    while deadline is None or time.monotonic() < deadline:
        count = count_docs(es_url, index)
        if count > 0 and count == last:
            stable += 1
            if stable >= stable_polls:
                logger.info("Source index '%s' settled at %d posts", index, count)
                return count
        else:
            if count != last:
                logger.info("Waiting for ingestion: '%s' has %d posts…", index, count)
            stable = 0
        last = count
        time.sleep(poll_interval)
    logger.warning(
        "Timed out waiting for '%s' to settle; proceeding with %d posts", index, max(last, 0)
    )
    return max(last, 0)


# --------------------------------------------------------------------------- #
# The knowledge index
# --------------------------------------------------------------------------- #
KNOWLEDGE_INDEX_TEMPLATE = {
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "index": {"knn": True},
        },
        "mappings": {
            "properties": {
                # The self-contained knowledge piece.
                "statement": {"type": "text"},
                # Embedded statement, so the index supports semantic search.
                "vector_field": {
                    "type": "knn_vector",
                    "dimension": 768,
                    "method": {"name": "hnsw", "space_type": "l2", "engine": "faiss"},
                },
                "subject": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
                "thread_id": {"type": "keyword"},
                "thread_title": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
                },
                "thread_url": {"type": "keyword"},
                "source_posts": {
                    "type": "nested",
                    "properties": {
                        "post_id": {"type": "keyword"},
                        "post_url": {"type": "keyword"},
                        "author": {"type": "text"},
                    },
                },
                "thread_post_count": {"type": "integer"},
                "thread_like_count": {"type": "integer"},
                "popularity_rank": {"type": "integer"},
                "model": {"type": "keyword"},
                "created_at": {"type": "date"},
            }
        },
    },
}


def ensure_knowledge_index(es_url, index, replace=False):
    """Create/update the index template, and the index if missing.

    PUT _index_template is an upsert (same idempotent pattern as
    ../opensearch-bootstrap), so mapping changes apply on the next run. With
    --replace the index is deleted first for a clean rebuild.
    """
    if replace:
        try:
            request("DELETE", f"{es_url}/{index}")
            logger.info("[knowledge] deleted existing index '%s' (--replace)", index)
        except RuntimeError as e:
            if "404" not in str(e):
                raise
    template = {"index_patterns": [index], **KNOWLEDGE_INDEX_TEMPLATE}
    request("PUT", f"{es_url}/_index_template/{index}", template)
    try:
        urllib.request.urlopen(urllib.request.Request(f"{es_url}/{index}", method="HEAD"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        request("PUT", f"{es_url}/{index}", {})
    logger.info("[knowledge] index template + index '%s' ready", index)


# --------------------------------------------------------------------------- #
# The knowledge-processor-status index — what makes processing idempotent
# --------------------------------------------------------------------------- #
# One document per thread, id = thread_id. Presence with processed=True means
# "skip this thread"; a thread with no document (or found=False) is untouched.
STATUS_INDEX_TEMPLATE = {
    "template": {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "properties": {
                "processed": {"type": "boolean"},
                "processed_at": {"type": "date"},
                "thread_title": {"type": "keyword"},
                "thread_url": {"type": "keyword"},
                "post_count": {"type": "integer"},
                "like_count": {"type": "integer"},
                "popularity_rank": {"type": "integer"},
                "pieces_written": {"type": "integer"},
            }
        },
    },
}


def ensure_status_index(es_url, index):
    """Create/update the status index template and the index if missing.

    Same idempotent upsert pattern as ensure_knowledge_index — no --replace
    here, since deleting this index would forget which threads were already
    processed, defeating the point of it.
    """
    template = {"index_patterns": [index], **STATUS_INDEX_TEMPLATE}
    request("PUT", f"{es_url}/_index_template/{index}", template)
    try:
        urllib.request.urlopen(urllib.request.Request(f"{es_url}/{index}", method="HEAD"))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        request("PUT", f"{es_url}/{index}", {})
    logger.info("[status] index template + index '%s' ready", index)


def fetch_processed_thread_ids(es_url, status_index, thread_ids):
    """Which of these thread ids are already marked processed, via one _mget."""
    if not thread_ids:
        return set()
    result = post_json(f"{es_url}/{status_index}/_mget", {"ids": thread_ids})
    return {
        doc["_id"]
        for doc in result.get("docs", [])
        if doc.get("found") and doc.get("_source", {}).get("processed")
    }


def mark_processed(es_url, status_index, thread, rank, pieces_written=None):
    """Record that a thread has reached a terminal outcome — never retried."""
    doc = {
        "processed": True,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "thread_title": thread["title"],
        "thread_url": thread["url"],
        "post_count": thread["post_count"],
        "like_count": thread["like_count"],
        "popularity_rank": rank,
    }
    if pieces_written is not None:
        doc["pieces_written"] = pieces_written
    request("PUT", f"{es_url}/{status_index}/_doc/{thread['thread_id']}", doc)


# --------------------------------------------------------------------------- #
# Stage 1: rank threads by popularity
# --------------------------------------------------------------------------- #
def rank_threads(es_url, index, top_n, min_posts, offset=0):
    """Threads ranked (offset+1)..(offset+top_n) by post count, most popular
    of the window first (single-shard terms agg → exact counts).

    OpenSearch's terms aggregation has no native offset, so the aggregation
    is sized to offset+top_n (the full ranking down to the end of the window)
    and the first `offset` buckets are sliced off in Python. Returns dicts
    with thread_id, title, url, post_count and like_count — fewer than top_n
    when the window runs past the end of the ranked list.
    """
    size = offset + top_n

    def agg_body(field):
        return {
            "size": 0,
            "aggs": {
                "threads": {
                    "terms": {
                        "field": field,
                        "size": size,
                        "min_doc_count": min_posts,
                        "order": {"_count": "desc"},
                    },
                    "aggs": {
                        "likes_total": {"sum": {"field": "likes.count"}},
                        "example": {
                            "top_hits": {"size": 1, "_source": ["thread_title", "thread_url"]}
                        },
                    },
                }
            },
        }

    # The bootstrap template maps thread_id as a keyword, so aggregating on it
    # directly works. Fall back to the .keyword subfield if this index predates
    # the template and thread_id came out as a text field (no fielddata).
    try:
        result = post_json(f"{es_url}/{index}/_search", agg_body("thread_id"))
    except RuntimeError as e:
        if "fielddata" not in str(e) and "not optimised" not in str(e):
            raise
        result = post_json(f"{es_url}/{index}/_search", agg_body("thread_id.keyword"))
    buckets = result.get("aggregations", {}).get("threads", {}).get("buckets", [])
    buckets = buckets[offset:offset + top_n]
    threads = []
    for bucket in buckets:
        hits = bucket.get("example", {}).get("hits", {}).get("hits", [])
        source = hits[0].get("_source", {}) if hits else {}
        threads.append(
            {
                "thread_id": str(bucket["key"]),
                "title": source.get("thread_title") or f"thread {bucket['key']}",
                "url": source.get("thread_url"),
                "post_count": int(bucket["doc_count"]),
                "like_count": int(bucket.get("likes_total", {}).get("value") or 0),
            }
        )
    return threads


# --------------------------------------------------------------------------- #
# Stage 2: reconstruct a thread
# --------------------------------------------------------------------------- #
def reconstruct_thread(es_url, index, thread_id):
    """Every post of one thread, in chronological order."""
    body = {
        "size": THREAD_FETCH_MAX_POSTS,
        "query": {"term": {"thread_id": thread_id}},
        "sort": [{"created_at_timestamp": {"order": "asc", "missing": "_last"}}],
        "_source": [
            "id",
            "post_url",
            "author",
            "body_text",
            "message_text",
            "likes",
            "created_at",
        ],
    }
    hits = post_json(f"{es_url}/{index}/_search", body).get("hits", {}).get("hits", [])
    posts = []
    for hit in hits:
        source = hit.get("_source", {})
        text = source.get("body_text") or source.get("message_text")
        if not text:
            continue
        posts.append(
            {
                "id": str(source.get("id") or hit.get("_id")),
                "post_url": source.get("post_url"),
                "author": (source.get("author") or {}).get("name") or "unknown",
                "text": text,
                "likes": int((source.get("likes") or {}).get("count") or 0),
            }
        )
    return posts


def render_thread(posts, char_budget):
    """Render posts as text for the model, trimmed to a char budget.

    Under budget, every post is included in chronological order. Over budget,
    the opener and the most-liked posts are kept preferentially, then the kept
    posts are printed back in chronological order with omission markers so the
    conversation still reads in sequence.
    """
    rendered = []
    for i, post in enumerate(posts):
        text = " ".join(post["text"].split())
        if len(text) > THREAD_POST_CHARS:
            text = text[:THREAD_POST_CHARS] + "…"
        rendered.append(f"[post {post['id']} by {post['author']}]: {text}")

    if sum(len(r) for r in rendered) <= char_budget:
        return "\n\n".join(rendered)

    # Priority: opener first, then by likes desc, then earliest — fill the budget.
    order = sorted(range(len(posts)), key=lambda i: (i != 0, -posts[i]["likes"], i))
    kept, used = set(), 0
    for i in order:
        if kept and used + len(rendered[i]) > char_budget:
            continue
        kept.add(i)
        used += len(rendered[i])

    out, prev = [], 0
    for i in sorted(kept):
        if i > prev:
            out.append(f"… {i - prev} post(s) omitted …")
        out.append(rendered[i])
        prev = i + 1
    if prev < len(posts):
        out.append(f"… {len(posts) - prev} post(s) omitted …")
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# Stage 3: distill knowledge pieces
# --------------------------------------------------------------------------- #
# Each line: "- <statement> [posts: 629, 631]". The bracket is optional.
_PIECE_RE = re.compile(r"^(.*?)\s*\[posts?:\s*([^\]]*)\]\s*$", re.I)


def build_distill_prompt(thread, thread_text):
    return (
        "You are building a knowledge base from an internet forum. "
        "Below is one complete discussion thread, its posts in chronological "
        "order. Extract the durable KNOWLEDGE it establishes about the subject "
        "under discussion — NOT a summary of the conversation.\n\n"
        "Each knowledge piece must:\n"
        "- Stand on its own: understandable without reading the thread. Name the "
        "part/system explicitly; do not write 'as mentioned above' or 'he said'.\n"
        "- Be durable and factual: a specification, how something works, a known "
        "failure mode, or a fix a poster CONFIRMED worked. Later posts may "
        "correct earlier ones and the original poster often reports what actually "
        "worked — trust those.\n"
        "- Not include for-sale notes, greetings, arguments, or speculation.\n\n"
        f'Thread title: "{thread["title"]}"\n'
        f"Posts ({thread['post_count']} total):\n\n{thread_text}\n\n"
        "Respond with ONE knowledge piece per line, in exactly this format:\n"
        "- <the fact, one sentence or two> [posts: <post_id>, <post_id>]\n"
        "listing the ids of the posts each fact came from. Write only the lines, "
        "no preamble or numbering. If the thread establishes nothing durable, "
        "respond with exactly: NONE"
    )


def parse_pieces(raw, valid_post_ids):
    """Parse the model's lines into (statement, [post_id, …]) tuples."""
    pieces = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line or line.upper() == "NONE":
            continue
        post_ids = []
        match = _PIECE_RE.match(line)
        if match:
            statement = match.group(1).strip()
            post_ids = [
                pid for pid in re.split(r"[,\s]+", match.group(2)) if pid in valid_post_ids
            ]
        else:
            statement = line
        statement = statement.strip().strip("*_`").strip()
        if len(statement) < MIN_STATEMENT_CHARS:
            continue
        pieces.append((statement, post_ids))
    return pieces


# --------------------------------------------------------------------------- #
# Stage 4: embed + index
# --------------------------------------------------------------------------- #
def index_piece(es_url, index, ollama_url, embed_model, *, statement, thread, post_ids,
                posts_by_id, rank, llm_model):
    """Embed one knowledge piece and upsert it by a hash of its normalized text."""
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
        "thread_post_count": thread["post_count"],
        "thread_like_count": thread["like_count"],
        "popularity_rank": rank,
        "model": llm_model,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request("PUT", f"{es_url}/{index}/_doc/{doc_id}", doc)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def process_window(args, threads, offset):
    """Reconstruct, distill, and index every not-yet-processed thread in one
    ranked window, skipping any thread already marked processed in the
    status index — that's what makes re-running the processor idempotent.

    `offset` is only used to report each thread's true popularity rank
    (offset + its position in the window); the window itself is already the
    slice `rank_threads` returned.
    """
    processed_ids = fetch_processed_thread_ids(
        args.opensearch_url, args.status_index, [t["thread_id"] for t in threads]
    )

    total_pieces = 0
    for i, thread in enumerate(threads):
        rank = offset + i + 1
        if thread["thread_id"] in processed_ids:
            logger.info("[rank %d] \"%s\" already processed; skipping", rank, thread["title"])
            continue
        logger.info(
            "[rank %d] \"%s\" (%d posts, %d likes)",
            rank, thread["title"], thread["post_count"], thread["like_count"],
        )
        posts = reconstruct_thread(args.opensearch_url, args.source_index, thread["thread_id"])
        if not posts:
            logger.warning("  no readable posts; skipping")
            mark_processed(args.opensearch_url, args.status_index, thread, rank, pieces_written=0)
            continue
        posts_by_id = {p["id"]: p for p in posts}
        thread_text = render_thread(posts, args.thread_char_budget)

        prompt = build_distill_prompt(thread, thread_text)
        try:
            raw = ollama_generate(args.ollama_url, args.llm_model, prompt)
        except Exception as e:
            # Transient/systemic failure (Ollama down, etc.) — leave unmarked
            # so this thread is retried on the next sweep.
            logger.error("  distillation failed: %s", e)
            continue
        pieces = parse_pieces(raw, set(posts_by_id))
        if not pieces:
            logger.info("  no durable knowledge in this thread")
            mark_processed(args.opensearch_url, args.status_index, thread, rank, pieces_written=0)
            continue

        stored = 0
        for statement, post_ids in pieces:
            try:
                index_piece(
                    args.opensearch_url, args.knowledge_index, args.ollama_url,
                    args.embed_model, statement=statement, thread=thread,
                    post_ids=post_ids, posts_by_id=posts_by_id, rank=rank,
                    llm_model=args.llm_model,
                )
                stored += 1
                logger.info("  + %s", statement if len(statement) <= 200 else statement[:200] + "…")
            except Exception as e:
                logger.error("  failed to index piece %r: %s", statement[:80], e)
        total_pieces += stored
        mark_processed(args.opensearch_url, args.status_index, thread, rank, pieces_written=stored)

    return total_pieces


# --------------------------------------------------------------------------- #
# Bootstrap: mark every currently-ranked thread as processed without distilling
# --------------------------------------------------------------------------- #
def mark_all_processed(args):
    """Rank the whole corpus (no windowing) and mark every thread processed.

    For seeding the status index around a `knowledge` index that was already
    built before this tracking existed, so a first idempotent run doesn't
    re-distill everything from scratch.
    """
    threads = rank_threads(
        args.opensearch_url, args.source_index, ALL_THREADS_SIZE, args.min_posts, offset=0,
    )
    logger.info("Marking %d thread(s) as processed in '%s'...", len(threads), args.status_index)
    for i, thread in enumerate(threads):
        mark_processed(args.opensearch_url, args.status_index, thread, rank=i + 1)
    logger.info("Done: %d thread(s) marked processed.", len(threads))
    return 0


def process_once(args):
    """Single pass over one window: threads ranked (offset+1)..(offset+top_n)."""
    threads = rank_threads(
        args.opensearch_url, args.source_index, args.top_n, args.min_posts,
        offset=args.offset,
    )
    if not threads:
        logger.warning(
            "No threads ranked %d.. with at least %d posts in '%s'; nothing to do.",
            args.offset + 1, args.min_posts, args.source_index,
        )
        return 0
    logger.info(
        "Processing threads ranked %d-%d by post count:",
        args.offset + 1, args.offset + len(threads),
    )
    total_pieces = process_window(args, threads, args.offset)
    logger.info(
        "Done: %d knowledge piece(s) written to '%s'.", total_pieces, args.knowledge_index
    )
    return 0


def process_loop(args):
    """Run forever, sweeping the popularity ranking window by window.

    Each cycle: wait for the source index to stop growing (posts scraped
    since the last cycle need time to settle), process the window at the
    current offset, then advance the offset by --top-n for next time. Once a
    window comes back short (fewer threads than --top-n — the tail of the
    ranked list), the next cycle wraps back to offset 0, so the sweep covers
    the whole corpus and then starts over, picking up newly-popular threads
    as ingestion adds posts.
    """
    offset = args.offset
    while True:
        wait_for_posts(args.opensearch_url, args.source_index, args.wait_timeout)

        threads = rank_threads(
            args.opensearch_url, args.source_index, args.top_n, args.min_posts,
            offset=offset,
        )
        if not threads:
            if offset == 0:
                logger.warning(
                    "No threads with at least %d posts in '%s' yet; nothing to do.",
                    args.min_posts, args.source_index,
                )
            else:
                logger.info(
                    "Offset %d is past the end of the ranked list; wrapping to the top.",
                    offset,
                )
                offset = 0
        else:
            logger.info(
                "Processing threads ranked %d-%d by post count:",
                offset + 1, offset + len(threads),
            )
            total_pieces = process_window(args, threads, offset)
            logger.info(
                "Cycle done: %d knowledge piece(s) written to '%s'.",
                total_pieces, args.knowledge_index,
            )
            # A short window means we've reached the tail of the ranked list —
            # start the next sweep over from the top instead of stepping past it.
            offset = 0 if len(threads) < args.top_n else offset + args.top_n

        logger.info("Sleeping %ds before the next cycle…", args.poll_interval)
        time.sleep(args.poll_interval)


def process(args):
    wait_for_cluster(args.opensearch_url, args.wait_timeout)
    wait_for_posts(args.opensearch_url, args.source_index, args.wait_timeout)
    ensure_status_index(args.opensearch_url, args.status_index)

    if args.mark_all_processed:
        return mark_all_processed(args)

    ensure_knowledge_index(args.opensearch_url, args.knowledge_index, replace=args.replace)

    if args.loop:
        return process_loop(args)  # never returns
    return process_once(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opensearch-url", default=DEFAULT_ES_URL, help="OpenSearch base URL")
    parser.add_argument("--source-index", default=DEFAULT_SOURCE_INDEX, help="Index to read posts from")
    parser.add_argument("--knowledge-index", default=DEFAULT_KNOWLEDGE_INDEX, help="Index to write pieces to")
    parser.add_argument(
        "--status-index", default=DEFAULT_STATUS_INDEX,
        help="Index tracking which threads have already been processed, for idempotent re-runs",
    )
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="Ollama base URL")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="Ollama model used to distill knowledge")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="Ollama embedding model (768-dim)")
    parser.add_argument(
        "--top-n", type=int, default=10,
        help="Window size: how many ranked threads to process per pass",
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="Skip this many top-ranked threads before the window, so the pass covers "
        "ranks (offset+1)..(offset+top-n) instead of always the top N",
    )
    parser.add_argument("--min-posts", type=int, default=5, help="Skip threads smaller than this")
    parser.add_argument(
        "--thread-char-budget", type=int, default=12000,
        help="Trim reconstructed threads longer than this before distillation",
    )
    parser.add_argument("--replace", action="store_true", help="Delete and recreate the knowledge index first")
    parser.add_argument(
        "--mark-all-processed", action="store_true",
        help="Mark every currently-ranked thread as processed in --status-index and exit, "
        "without distilling anything. For seeding the status index around an existing "
        "knowledge index built before this tracking existed.",
    )
    parser.add_argument(
        "--wait-timeout", type=int, default=0,
        help="Seconds to wait for the cluster/ingestion. 0 or less waits forever.",
    )
    parser.add_argument(
        "--loop", action="store_true",
        help="Run forever: process the window at --offset, advance by --top-n "
        "each cycle, wrap back to offset 0 once the ranked list runs out, "
        "waiting for the source index to settle before each cycle",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=1800,
        help="Seconds to sleep between cycles when --loop is set (default 1800 = 30 min)",
    )
    parser.add_argument("--log-level", default="INFO", help="Python logging level (e.g. DEBUG, INFO)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(process(args))


if __name__ == "__main__":
    main()
