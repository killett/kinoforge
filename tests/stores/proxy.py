"""In-process 503-injection proxy for Layer W retry-axis tests."""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.request
from contextlib import AbstractContextManager
from typing import Any


class Fail503Proxy(AbstractContextManager["Fail503Proxy"]):
    def __init__(self, target_endpoint: str, *, fail_count: int):
        self.target_endpoint = target_endpoint.rstrip("/")
        self.fail_count = fail_count
        self.request_count = 0
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def endpoint(self) -> str:
        return f"http://localhost:{self.port}"

    def __enter__(self) -> Fail503Proxy:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any, **kwargs: Any) -> None:
                pass  # mute access log

            def do_GET(self):
                self._dispatch("GET")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_POST(self):
                self._dispatch("POST")

            def do_HEAD(self):
                self._dispatch("HEAD")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def _dispatch(self, method: str) -> None:
                with outer._lock:
                    outer.request_count += 1
                    n = outer.request_count
                if n <= outer.fail_count:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                upstream = urllib.request.Request(
                    url=outer.target_endpoint + self.path,
                    data=body,
                    method=method,
                    headers={
                        k: v for k, v in self.headers.items() if k.lower() != "host"
                    },
                )
                with urllib.request.urlopen(upstream) as resp:
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        if k.lower() in ("transfer-encoding", "connection"):
                            continue
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp.read())

        server = socketserver.ThreadingTCPServer(("localhost", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def __exit__(self, *_exc: Any) -> bool | None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
        return None
