#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def make_handler(static_dir: Path, upstream: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._dispatch()

        def do_POST(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_DELETE(self):
            self._dispatch()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.end_headers()

        def _dispatch(self):
            if self.path.startswith(("/v1/", "/healthz", "/docs", "/openapi.json")):
                self._proxy()
            else:
                self._serve_static()

        def _proxy(self):
            body = None
            if "Content-Length" in self.headers:
                body = self.rfile.read(int(self.headers["Content-Length"]))

            req = Request(urljoin(upstream, self.path), data=body, method=self.command)
            for key, value in self.headers.items():
                if key.lower() not in {"host", "content-length", "connection"}:
                    req.add_header(key, value)

            try:
                with urlopen(req, timeout=180) as resp:
                    payload = resp.read()
                    self.send_response(resp.status)
                    for key, value in resp.headers.items():
                        if key.lower() not in {"transfer-encoding", "connection", "content-encoding"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    if payload:
                        self.wfile.write(payload)
            except HTTPError as exc:
                payload = exc.read()
                self.send_response(exc.code)
                self.send_header("Content-Type", exc.headers.get("Content-Type", "text/plain; charset=utf-8"))
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)
            except URLError as exc:
                payload = f"upstream unavailable: {exc}".encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def _serve_static(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                path = "/index.html"

            file_path = (static_dir / path.lstrip("/")).resolve()
            if not str(file_path).startswith(str(static_dir.resolve())) or not file_path.exists() or file_path.is_dir():
                file_path = static_dir / "index.html"

            payload = file_path.read_bytes()
            content_type, _ = mimetypes.guess_type(str(file_path))
            self.send_response(200)
            self.send_header("Content-Type", content_type or "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "15175")))
    parser.add_argument("--static-dir", default="frontend/dist")
    parser.add_argument("--upstream", default=os.getenv("UPSTREAM_API", "http://127.0.0.1:18000"))
    args = parser.parse_args()

    static_dir = Path(args.static_dir).resolve()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(static_dir, args.upstream.rstrip("/")))
    print(f"serving {static_dir} on http://127.0.0.1:{args.port} proxy->{args.upstream}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
