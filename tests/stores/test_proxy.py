"""Fail503Proxy unit tests against a loopback target."""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.error
import urllib.request

import pytest

from tests.stores.proxy import Fail503Proxy


@pytest.fixture
def loopback_target():
    """Tiny upstream that 200s with the path echoed in the body."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            body = f"got {self.path}".encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = socketserver.ThreadingTCPServer(("localhost", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_proxy_fails_first_n_then_forwards(loopback_target):
    with Fail503Proxy(loopback_target, fail_count=2) as proxy:
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{proxy.endpoint}/ping").read()
            assert excinfo.value.code == 503
        with urllib.request.urlopen(f"{proxy.endpoint}/ping") as resp:
            body = resp.read()
        assert body == b"got /ping"
        assert proxy.request_count == 3


def test_proxy_put_round_trip(loopback_target):
    with Fail503Proxy(loopback_target, fail_count=0) as proxy:
        req = urllib.request.Request(
            f"{proxy.endpoint}/upload",
            data=b"payload",
            method="PUT",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.read() == b"payload"


def test_two_proxies_coexist(loopback_target):
    with (
        Fail503Proxy(loopback_target, fail_count=0) as p1,
        Fail503Proxy(loopback_target, fail_count=0) as p2,
    ):
        assert p1.port != p2.port
        urllib.request.urlopen(f"{p1.endpoint}/a").read()
        urllib.request.urlopen(f"{p2.endpoint}/b").read()
        assert p1.request_count == 1
        assert p2.request_count == 1
