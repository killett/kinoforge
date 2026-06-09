"""Shared pytest fixtures for kinoforge tests.

Provides ``http_server``: a Range-aware loopback HTTP server for downloader tests.
"""

from __future__ import annotations

import re
import tempfile
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clear_redaction_registry_between_tests() -> Generator[None, None, None]:
    """Reset the process-wide RedactionRegistry around every test.

    The registry is a singleton — tokens written by tests in
    test_redaction.py / test_ledger_redaction.py / test_downloader_opaque_name.py
    / OutputSink.publish would otherwise leak into unrelated tests and
    redact substrings (e.g. cluster names) inside captured fixture JSON.
    """
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


@dataclass
class HttpServerInfo:
    """Info and helpers yielded to tests by the ``http_server`` fixture.

    Attributes:
        base_url: The ``http://host:port`` root URL of the loopback server.
        temp_dir: Temporary directory from which files are served.
        request_log: Accumulated ``(path, method, range_header)`` tuples,
            one entry per request received.  ``range_header`` is ``""`` when
            no ``Range`` header was sent.
    """

    base_url: str
    temp_dir: Path
    request_log: list[tuple[str, str, str]] = field(default_factory=list)

    def serve_bytes(self, name: str, data: bytes) -> None:
        """Write *data* into the temp directory under *name*.

        Args:
            name: Filename (no directory components).
            data: Raw bytes to serve at ``/<name>``.
        """
        (self.temp_dir / name).write_bytes(data)


def _make_handler(
    serve_dir: Path,
    log: list[tuple[str, str, str]],
) -> type[BaseHTTPRequestHandler]:
    """Build a ``BaseHTTPRequestHandler`` subclass closed over *serve_dir* and *log*.

    Args:
        serve_dir: Directory containing files to serve.
        log: Mutable list that receives ``(path, method, range_header)`` tuples.

    Returns:
        A ``BaseHTTPRequestHandler`` subclass.
    """

    class _RangeHandler(BaseHTTPRequestHandler):
        """HTTP/1.1 handler that honours ``Range: bytes=N-`` requests."""

        def log_message(self, fmt: str, *args: object) -> None:  # noqa: D102
            # Suppress default stderr logging during tests.
            pass

        def do_GET(self) -> None:  # noqa: N802
            """Serve the requested file, honouring a ``Range`` header if present."""
            range_header = self.headers.get("Range", "")
            log.append((self.path, "GET", range_header))

            # Strip query string, decode the path component.
            pure_path = self.path.split("?")[0].lstrip("/")
            target = serve_dir / pure_path

            if not target.exists() or not target.is_file():
                self.send_error(404, "Not Found")
                return

            data = target.read_bytes()
            total = len(data)

            if range_header:
                # Only handle open-ended "bytes=N-" ranges.
                match = re.fullmatch(r"bytes=(\d+)-", range_header.strip())
                if match is None:
                    self.send_error(416, "Range Not Satisfiable")
                    return
                start = int(match.group(1))
                if start > total:
                    self.send_error(416, "Range Not Satisfiable")
                    return
                body = data[start:]
                end = total - 1
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(data)

    return _RangeHandler


@pytest.fixture()
def http_server() -> Generator[HttpServerInfo, None, None]:
    """Spin up a Range-aware loopback HTTP server.

    Creates its own private temporary directory (separate from the test's
    ``tmp_path``) so that the served file tree never collides with the
    download destination directory used by the test.

    Yields:
        An :class:`HttpServerInfo` instance with ``.base_url``,
        ``.serve_bytes()``, ``.request_log``, and ``.temp_dir``.

    The server runs in a daemon thread and is shut down on teardown.
    """
    with tempfile.TemporaryDirectory() as td:
        serve_dir = Path(td)
        info = HttpServerInfo(base_url="", temp_dir=serve_dir)
        handler_cls = _make_handler(serve_dir, info.request_log)

        # Port 0 → OS assigns a free port.
        server = HTTPServer(("127.0.0.1", 0), handler_cls)
        addr = server.server_address
        # server_address is (host: str, port: int) for TCP; cast via indexing.
        host = str(addr[0])
        port = int(addr[1])
        info.base_url = f"http://{host}:{port}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            yield info
        finally:
            server.shutdown()
            thread.join(timeout=5)
