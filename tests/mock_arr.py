"""Minimal fake Seerr + Radarr for end-to-end tests.

Runs on 127.0.0.1:5999. Not part of the application.

    GET  /api/v1/status          -> Seerr version (requires X-Api-Key: test-key)
    POST /api/v1/request         -> 201 with an id
    GET  /api/v3/system/status   -> Radarr version
    GET  /api/v3/diskspace       -> one 1000 GB disk with 60 GB free
    GET  /api/v3/movie           -> 400 library movies
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 5999
API_KEY = "test-key"
STATE: dict = {"requests": [], "next_id": 1}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/v1/status":
            if self.headers.get("X-Api-Key") != API_KEY:
                self._send(401, {"message": "bad key"})
                return
            self._send(200, {"version": "2.0.0-mock", "appName": "Jellyseerr (mock)"})
        elif self.path == "/api/v3/system/status":
            self._send(200, {"version": "5.0.0-mock"})
        elif self.path == "/api/v3/diskspace":
            self._send(200, [{"path": "/data", "freeSpace": 60 * 1024**3, "totalSpace": 1000 * 1024**3}])
        elif self.path == "/api/v3/movie":
            self._send(200, [
                {"id": i, "title": f"Lib Movie {i}", "size": 4 * 1024**3,
                 "hasFile": True, "year": 2010 + i % 10}
                for i in range(1, 401)
            ])
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._send(400, {"message": "bad json"})
            return
        if self.path == "/api/v1/request":
            if self.headers.get("X-Api-Key") != API_KEY:
                self._send(401, {"message": "bad key"})
                return
            rid = STATE["next_id"]
            STATE["next_id"] += 1
            STATE["requests"].append(body)
            self._send(201, {"id": rid, "status": "pending", "media": {"title": body.get("title")}})
            return
        self._send(404, {"message": "not found"})

    def log_message(self, *args) -> None:
        pass


def serve() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    print(f"mock ARR up on 127.0.0.1:{PORT}", flush=True)
    serve()
    threading.Event().wait()