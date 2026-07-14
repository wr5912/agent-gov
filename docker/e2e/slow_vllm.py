from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SlowVllmHandler(BaseHTTPRequestHandler):
    server_version = "AgentGovSlowVllm/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health/live":
            self._json_response(200, {"status": "ok"})
            return
        if self.path == "/version":
            self._delayed_response({"version": "0.14.0"})
            return
        if self.path == "/v1/models":
            self._delayed_response({"data": [{"id": "agent-gov-model"}]})
            return
        self._json_response(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._delayed_response({"choices": [{"message": {"content": "delayed"}}]})

    def _delayed_response(self, payload: dict[str, object]) -> None:
        time.sleep(float(os.getenv("SLOW_VLLM_DELAY_SECONDS", "8")))
        self._json_response(200, payload)

    def _json_response(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), SlowVllmHandler)
    server.daemon_threads = True
    server.serve_forever()
