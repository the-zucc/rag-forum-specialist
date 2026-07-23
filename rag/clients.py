"""HTTP plumbing for OpenSearch and Ollama (stdlib only, matching the repo)."""

from __future__ import annotations

import json
import logging
from typing import Any
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("rag")

def request(method: str, url: str, body: Any = None, content_type: str = "application/json") -> tuple[int, dict]:
    """Performs an HTTP request using the standard library and returns a status code and JSON response.

    Args:
        method: The HTTP method (GET, POST, etc.).
        url: The target URL.
        body: The request body as a dict/list or bytes.
        content_type: The Content-Type header value.

    Returns:
        A tuple containing the HTTP status code and the parsed JSON response dictionary.

    Raises:
        RuntimeError: If the request fails with an HTTP error.
    """
    data = json.dumps(body).encode("                utf-8") if isinstance(body, (dict, list)) else body
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


def get_json(url: str) -> dict[str, Any]:
    """Performs a GET request and returns the JSON response."""
    return request("GET", url)[1]


def post_json(url: str, body: Any) -> dict[str, Any]:
    """Performs a POST request and returns the JSON response."""
    return request("POST", url, body)[1]


def ollama_embed(ollama_url: str, model: str, text: str) -> list[float]:
    """Generates an embedding for the given text using Ollama."""
    result = post_json(f"{ollama_url}/api/embeddings", {"model": model, "prompt": text})
    return result["embedding"]


def ollama_stream(
    ollama_url: str,
    model: str,
    prompt: str,
) -> str:
    """Stream a completion to the terminal token by token; return the full text.

    A spinner runs while the model loads and evaluates the prompt, before its
    first token.
    """
    body = {"model": model, "prompt": prompt, "stream": True, "options": {}}
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


def wait_for_cluster(es_url: str, timeout: float) -> None:
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


def count_docs(es_url: str, index: str) -> int:
    try:
        return int(get_json(f"{es_url}/{index}/_count").get("count", 0))
    except RuntimeError as e:
        if "404" in str(e):  # index not created yet
            return 0
        raise
