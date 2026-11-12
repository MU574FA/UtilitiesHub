"""A tiny fake Ollama server for tests (no model, no network): answers are scripted by a handler function.

    with FakeOllama(handler) as srv:
        cfg = LlmConfig(enabled=True, host=srv.url)

`handler(path, payload) -> dict | str | int`: a dict is sent as JSON, a str becomes the chat answer's text,
an int is an HTTP error code.  `srv.calls` lists every request seen.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeOllama:
    def __init__(self, handler=None, models=("llama3.2:latest", "qwen2.5vl:latest")) -> None:
        self.handler = handler or (lambda path, payload: "")
        self.models = list(models)
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):                       # keep the test output quiet
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send(200, {"models": [{"name": m} for m in outer.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.calls.append((self.path, payload))
                model = payload.get("model", "")
                if outer.models and not any(m.split(":")[0] == model.split(":")[0] for m in outer.models):
                    return self._send(404, {"error": f"model '{model}' not found"})
                res = outer.handler(self.path, payload)
                if isinstance(res, int):
                    return self._send(res, {"error": "scripted error"})
                if isinstance(res, str):
                    res = {"message": {"role": "assistant", "content": res}}
                elif isinstance(res, (list, tuple)) or (isinstance(res, dict) and "message" not in res):
                    res = {"message": {"role": "assistant", "content": json.dumps(res)}}
                self._send(200, res)

        self._srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
