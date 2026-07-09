"""HTTP plumbing for OpenSearch and Ollama (stdlib only, matching the repo)."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("rag")

# Ollama silently defaults to a 4096-token context and truncates past it,
# which would cut off the reconstructed threads.
DEFAULT_NUM_CTX = 65536


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


def ollama_embed(ollama_url, model, text):
    result = post_json(f"{ollama_url}/api/embeddings", {"model": model, "prompt": text})
    return result["embedding"]


def ollama_stream(ollama_url, model, prompt, num_ctx=DEFAULT_NUM_CTX):
    """Stream a completion to the terminal token by token; return the full text.

    A spinner runs while the model loads and evaluates the prompt, before its
    first token.
    """
    body = {"model": model, "prompt": prompt, "stream": True, "options": {"num_ctx": num_ctx}}
    req = urllib.request.Request(
        f"{ollama_url}/api/generate", data=json.dumps(body).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")

    stop = threading.Event()

    def spin():
        frames = "|/-\\"
        i = 0
        while not stop.is_set():
            sys.stdout.write(frames[i % len(frames)] + "\r")
            sys.stdout.flush()
            i += 1
            stop.wait(0.15)

    spinner = threading.Thread(target=spin, daemon=True)
    spinner.start()
    chunks = []
    try:
        with urllib.request.urlopen(req) as resp:
            for line in resp:
                data = json.loads(line)
                token = data.get("response", "")
                if token:
                    if not chunks:
                        stop.set()
                        spinner.join()
                        sys.stdout.write("  \r")
                    chunks.append(token)
                    sys.stdout.write(token)
                    sys.stdout.flush()
                if data.get("done"):
                    break
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {ollama_url}/api/generate failed: {e.code} {detail}") from e
    finally:
        stop.set()
        if spinner.is_alive():
            spinner.join()
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chunks)


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
