#!/usr/bin/env python3
"""Ingest scraped forum posts from threads/<thread-id>/posts.json into OpenSearch.

Uses only the Python standard library, so it runs without installing anything
extra. Bulk-indexes every post found under the destination directory,
upserting by post id so re-runs never duplicate. Assumes the index template
has already been created (see ../opensearch-bootstrap), which docker-compose
guarantees by ordering this service after opensearch-bootstrap completes.

Pass --watch to keep running afterwards: it uses the `watchdog` package (only
required for that mode) to listen for created/modified threads/<id>/posts.json
files and reindex just that thread as soon as the scraper writes new posts.
"""

import argparse
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
DEFAULT_INDEX = os.environ.get("INDEX_NAME", "forum-posts")
DEFAULT_DEST_DIR = os.environ.get(
    "DEST_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "threads")
)
DEFAULT_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")
EMBED_TEXT_CHAR_LIMIT = 8000


def request(method, url, body=None, content_type="application/json"):
    data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {e.code} {detail}") from e


def wait_for_cluster(es_url, timeout):
    """Poll until the cluster responds. timeout <= 0 means wait forever."""
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    last_error = None
    while deadline is None or time.monotonic() < deadline:
        try:
            status, body = request("GET", f"{es_url}/_cluster/health")
            if body.get("status") in ("green", "yellow"):
                print(f"Cluster is up (status={body.get('status')})")
                return
        except Exception as e:
            last_error = e
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for cluster at {es_url}: {last_error}")


def iter_posts_files(dest_dir):
    pattern = os.path.join(dest_dir, "*", "posts.json")
    return sorted(glob.glob(pattern))


def load_posts(path, retries=3, retry_delay=0.5):
    """Read a posts.json file, retrying briefly in case it's mid-write."""
    last_error = None
    for attempt in range(retries):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(retry_delay)
    print(f"Skipping {path}: {last_error}", file=sys.stderr)
    return []


def embed_text(ollama_url, model, text):
    """Get an embedding vector for text from Ollama. Returns None for empty text."""
    text = (text or "").strip()
    if not text:
        return None
    body = {"model": model, "prompt": text[:EMBED_TEXT_CHAR_LIMIT]}
    _, result = request("POST", f"{ollama_url}/api/embeddings", body)
    return result["embedding"]


def bulk_index(es_url, index_name, batch, ollama_url=None, embed_model=None):
    if not batch:
        return 0, 0
    lines = []
    for post in batch:
        if ollama_url:
            vector = embed_text(ollama_url, embed_model, post.get("body_text") or post.get("message_text"))
            if vector is not None:
                post = {**post, "vector_field": vector}
        action = {"index": {"_index": index_name, "_id": post["id"]}}
        lines.append(json.dumps(action))
        lines.append(json.dumps(post))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    status, body = request("POST", f"{es_url}/_bulk", payload, content_type="application/x-ndjson")

    indexed, errors = 0, 0
    if body.get("errors"):
        for item in body.get("items", []):
            result = item.get("index", {})
            if result.get("status", 500) >= 300:
                errors += 1
                print(f"Failed to index post {result.get('_id')}: {result.get('error')}", file=sys.stderr)
            else:
                indexed += 1
    else:
        indexed = len(batch)
    return indexed, errors


def ingest_file(es_url, index_name, path, batch_size, ollama_url=None, embed_model=None):
    posts = load_posts(path)
    total_indexed = total_errors = 0
    for start in range(0, len(posts), batch_size):
        indexed, errors = bulk_index(
            es_url, index_name, posts[start : start + batch_size], ollama_url, embed_model
        )
        total_indexed += indexed
        total_errors += errors
    return total_indexed, total_errors


def ingest_all(es_url, index_name, dest_dir, batch_size, ollama_url=None, embed_model=None):
    total_indexed = total_errors = 0
    files = iter_posts_files(dest_dir)
    for path in files:
        indexed, errors = ingest_file(es_url, index_name, path, batch_size, ollama_url, embed_model)
        total_indexed += indexed
        total_errors += errors
    print(f"Processed {len(files)} thread files: indexed {total_indexed} posts, {total_errors} errors.")
    return total_indexed, total_errors


def watch(es_url, index_name, dest_dir, batch_size, ollama_url=None, embed_model=None):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class PostsFileHandler(FileSystemEventHandler):
        def on_created(self, event):
            self._handle(event)

        def on_modified(self, event):
            self._handle(event)

        def _handle(self, event):
            if event.is_directory or os.path.basename(event.src_path) != "posts.json":
                return
            # The scraper may still be flushing the write; give it a moment.
            time.sleep(0.5)
            indexed, errors = ingest_file(
                es_url, index_name, event.src_path, batch_size, ollama_url, embed_model
            )
            print(f"Reindexed {event.src_path}: {indexed} posts, {errors} errors.")

    observer = Observer()
    observer.schedule(PostsFileHandler(), dest_dir, recursive=True)
    observer.start()
    print(f"Watching {dest_dir} for new/updated posts.json files...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--es-url", default=DEFAULT_ES_URL, help="OpenSearch/Elasticsearch base URL")
    parser.add_argument("--index", default=DEFAULT_INDEX, help="Index name to ingest into")
    parser.add_argument("--dest-dir", default=DEFAULT_DEST_DIR, help="Directory containing <thread-id>/posts.json files")
    parser.add_argument("--batch-size", type=int, default=500, help="Number of posts per bulk request")
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=60,
        help="Seconds to wait for the cluster to be reachable. 0 or less waits forever.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="After the initial ingest, keep running and reindex posts.json files as they change.",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help="Ollama base URL used to embed post text for vector search. Empty disables embedding.",
    )
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help="Ollama embedding model name")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="Skip embedding posts (index text/metadata only, no vector search support).",
    )
    args = parser.parse_args()

    ollama_url = None if args.no_embed else args.ollama_url

    wait_for_cluster(args.es_url, args.wait_timeout)

    _, total_errors = ingest_all(
        args.es_url, args.index, args.dest_dir, args.batch_size, ollama_url, args.embed_model
    )

    if args.watch:
        watch(args.es_url, args.index, args.dest_dir, args.batch_size, ollama_url, args.embed_model)
    elif total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
