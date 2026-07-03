"""Shared HTTP helpers for the bootstrap scripts in this directory."""

import json
import time
import urllib.error
import urllib.request


def request(es_url, method, path, body=None):
    url = f"{es_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {e.code} {detail}") from e


def wait_for_cluster(es_url, timeout):
    """Poll until the cluster responds. timeout <= 0 means wait forever."""
    deadline = None if timeout <= 0 else time.monotonic() + timeout
    last_error = None
    while deadline is None or time.monotonic() < deadline:
        try:
            _, body = request(es_url, "GET", "/_cluster/health")
            if body.get("status") in ("green", "yellow"):
                print(f"Cluster is up (status={body.get('status')})")
                return
        except Exception as e:
            last_error = e
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for cluster at {es_url}: {last_error}")
