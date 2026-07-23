"""Thread reconstruction from `forum-posts` and rendering for the model (stage 4)."""

from __future__ import annotations

from clients import post_json

# How much of a reconstructed thread to render into one post before trimming.
THREAD_POST_CHARS = 1200
THREAD_FETCH_MAX_POSTS = 500


def fetch_thread_posts(
    es_url: str,
    index: str,
    thread_id: str,
) -> list[dict[str, Any]]:
    """Every post of one thread, in chronological order."""
    body = {
        "size": THREAD_FETCH_MAX_POSTS,
        "query": {"term": {"thread_id": thread_id}},
        "sort": [{"created_at_timestamp": {"order": "asc", "missing": "_last"}}],
        "_source": ["id", "post_url", "author", "body_text", "message_text", "likes"],
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


def render_thread(
    posts: list[dict[str, Any]],
    char_budget: int,
    cited_ids: frozenset[str] = frozenset(),
) -> str:
    """Render posts as text for the model, trimmed to a char budget.

    Under budget, every post is included in chronological order. Over budget,
    the opener and the posts the retained knowledge pieces cite are kept
    preferentially (then the most-liked), printed back in chronological order
    with omission markers so the conversation still reads in sequence.
    """
    rendered = []
    for post in posts:
        text = " ".join(post["text"].split())
        if len(text) > THREAD_POST_CHARS:
            text = text[:THREAD_POST_CHARS] + "…"
        rendered.append(f"[post {post['id']} by {post['author']}]: {text}")

    if sum(len(r) for r in rendered) <= char_budget:
        return "\n\n".join(rendered)

    order = sorted(
        range(len(posts)),
        key=lambda i: (i != 0, posts[i]["id"] not in cited_ids, -posts[i]["likes"], i),
    )
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
