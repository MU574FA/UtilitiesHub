"""Tiny fake OpenAI-compatible + Anthropic server for tests (no network, no key).

    with FakeApi(handler, api_key="sk-test") as srv:
        cfg = LlmConfig(enabled=True, provider="openai", host=srv.url + "/v1", api_key="sk-test", text_model="m")

`handler(kind, payload, headers) -> str | dict | int`: kind is "openai" or "anthropic"; a str is the model's answer, a dict is sent as
the raw JSON body, an int is an HTTP error code (400 = bad request, 401 ...).  `srv.calls` lists (kind, payload, headers).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeApi:
    def __init__(self, handler=None, api_key: str = "", models=("gpt-test", "claude-test-20250101")) -> None:
        self.handler = handler or (lambda kind, payload, headers: "OK")
        self.api_key = api_key
        self.models = list(models)
        self.list_status = 200                          # set to 404 to imitate a server without /models
        self.calls: list[tuple[str, dict, dict]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: dict, extra: dict | None = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self) -> bool:
                if not outer.api_key:
                    return True
                got = self.headers.get("x-api-key") or (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                return got == outer.api_key

            def do_GET(self):
                if not self._authorised():
                    return self._send(401, {"error": {"message": "invalid api key"}})
                if self.path.split("?")[0].endswith("/models"):
                    if outer.list_status != 200:
                        return self._send(outer.list_status, {"error": {"message": "no list"}})
                    return self._send(200, {"data": [{"id": m} for m in outer.models]})
                self._send(404, {"error": {"message": "not found"}})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not self._authorised():
                    return self._send(401, {"error": {"message": "invalid api key"}})
                path = self.path.split("?")[0]
                if path.endswith("/chat/completions"):
                    kind = "openai"
                elif path.endswith("/messages"):
                    kind = "anthropic"
                else:
                    return self._send(404, {"error": {"message": "not found"}})
                outer.calls.append((kind, payload, headers))
                res = outer.handler(kind, payload, headers)
                if isinstance(res, int):
                    return self._send(res, {"error": {"message": f"scripted {res}"}}, {"Retry-After": "0.01"})
                if isinstance(res, (dict, list)) and not (isinstance(res, dict) and ("choices" in res or "content" in res)):
                    res = json.dumps(res)
                if isinstance(res, str):
                    res = ({"choices": [{"message": {"role": "assistant", "content": res}}]} if kind == "openai"
                           else {"content": [{"type": "text", "text": res}], "stop_reason": "end_turn"})
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
