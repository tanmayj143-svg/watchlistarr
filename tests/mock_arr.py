"""Minimal fake Seerr + Radarr for end-to-end tests.

Runs on 127.0.0.1:5999. Not part of the application.

    GET  /api/v1/status          -> Seerr version (requires X-Api-Key: test-key)
    POST /api/v1/request         -> 201 with an id
    GET  /api/v3/system/status   -> Radarr version
    GET  /api/v3/diskspace       -> one 1000 GB disk with 60 GB free
    GET  /api/v3/movie           -> 400 library movies

TMDB mock (v1.7.0: metadata is TMDB-only, so e2e needs a metadata source):
served on 127.0.0.1:5998 by serve_tmdb(); point providers.TMDB_BASE at it.
    GET  /3/search/movie         -> one deterministic hit per title
    GET  /3/movie/<id>           -> full details incl. genres/countries/languages
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



# ---------------------------------------------------------------------------
# mock TMDB -- deterministic metadata for e2e (no network)
# ---------------------------------------------------------------------------
TMDB_PORT = 5998
TMDB_STATE: dict = {"next_id": 1000, "by_title": {}}


class TMDBHandler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        import urllib.parse

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/3/search/movie":
            title = (qs.get("query") or [""])[0]
            year = (qs.get("year") or [None])[0]
            tid = TMDB_STATE["by_title"].get(title.lower())
            if tid is None:
                tid = TMDB_STATE["next_id"]
                TMDB_STATE["next_id"] += 1
                TMDB_STATE["by_title"][title.lower()] = tid
            self._send(200, {"results": [{
                "id": tid, "title": title,
                "release_date": f"{year or 2000}-06-01",
                "genre_ids": [18, 10749], "overview": "mock overview",
                "poster_path": "/mock.jpg", "popularity": 11.5,
            }]})
            return
        if parsed.path.startswith("/3/movie/"):
            tid = int(parsed.path.rsplit("/", 1)[-1])
            # Title is looked back up from the search map so details carry the
            # real requested title (distinct per film; the duplicate-title
            # e2e check depends on it).
            rev = {v: k for k, v in TMDB_STATE["by_title"].items()}
            real_title = rev.get(tid, "Mock Film")
            self._send(200, {
                "id": tid, "title": real_title, "release_date": "2001-03-09",
                "imdb_id": "tt0253474", "overview": "mock overview",
                "runtime": 97, "vote_average": round(6.4 + (tid % 20) / 10.0, 1), "vote_count": 500,
                "poster_path": "/mock.jpg", "backdrop_path": "/mockb.jpg",
                "genres": [{"id": 18, "name": "Drama"},
                           {"id": 10749, "name": "Romance"}],
                "production_countries": [{"iso_3166_1": "US",
                                          "name": "United States of America"}],
                "original_language": "en",
                "spoken_languages": [{"iso_639_1": "en", "name": "English"}],
            })
            return
        self._send(404, {"message": "not found"})

    def log_message(self, *args) -> None:
        pass


def serve_tmdb() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", TMDB_PORT), TMDBHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve() -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    print(f"mock ARR up on 127.0.0.1:{PORT}", flush=True)
    serve()
    threading.Event().wait()