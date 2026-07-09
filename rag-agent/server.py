"""Minimal OpenAI-compatible HTTP API in front of the RAG graph (stdlib only).

Exposes `POST /v1/chat/completions` (and `GET /v1/models`, `GET /healthz`) so
the agent can be pointed at by any OpenAI-client-compatible tool (Open WebUI,
LangChain's ChatOpenAI, curl) instead of asked one question at a time from
the CLI. The last `user` message in the request is treated as the question;
everything else about a request (model, prior turns) is ignored — this is a
research agent that answers one question per call, not a multi-turn chat.

Requests are served one at a time (a lock around `ask_rag`): they all share
one local Ollama instance already saturated by the multi-step research loop,
so concurrent requests would just thrash the same model rather than run any
faster.

CORS is wide open (`Access-Control-Allow-Origin: *`) since the intended
browser client (../webui/) is served from a different origin/port than this
API and has no credentials to leak; a `do_OPTIONS` handles the preflight
browsers send before a JSON POST.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from graph import ask_rag

logger = logging.getLogger("rag")

_lock = threading.Lock()


def _extract_question(messages):
    for message in reversed(messages or []):
        if message.get("role") == "user" and message.get("content"):
            return message["content"]
    return None


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rag-agent/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _cors_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path == "/healthz":
                self._send_json(200, {"status": "ok"})
                return
            if self.path == "/v1/models":
                self._send_json(200, {
                    "object": "list",
                    "data": [{
                        "id": cfg.llm_model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "rag-agent",
                    }],
                })
                return
            self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})
                return

            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"error": {"message": "invalid JSON body"}})
                return

            question = _extract_question(body.get("messages"))
            if not question:
                self._send_json(
                    400, {"error": {"message": "no 'user' message found in 'messages'"}}
                )
                return

            model = body.get("model") or cfg.llm_model
            stream = bool(body.get("stream"))
            request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
            created = int(time.time())

            logger.info("Chat completion request: %r", question[:200])
            try:
                with _lock:
                    answer = ask_rag(question, cfg)
            except Exception as e:
                logger.error("ask_rag failed: %s", e)
                self._send_json(500, {"error": {"message": str(e)}})
                return

            if not stream:
                self._send_json(200, {
                    "id": request_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": answer},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                })
                return

            # Streaming clients get the whole answer as a single delta, then
            # [DONE] — the graph itself runs to completion before anything is
            # known, so there is nothing to stream incrementally at this level.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self._cors_headers()
            self.end_headers()
            base = {"id": request_id, "object": "chat.completion.chunk", "created": created,
                    "model": model}
            chunk = {**base, "choices": [{"index": 0,
                                          "delta": {"role": "assistant", "content": answer},
                                          "finish_reason": None}]}
            done = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            self.wfile.write(f"data: {json.dumps(done)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")

    return Handler


def serve(cfg):
    handler = make_handler(cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), handler)
    logger.info(
        "Serving OpenAI-compatible API on http://%s:%d (POST /v1/chat/completions)",
        cfg.host, cfg.port,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
