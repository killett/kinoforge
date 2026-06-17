"""Shared pytest fixtures for kinoforge tests.

Provides ``http_server``: a Range-aware loopback HTTP server for downloader tests.
"""

from __future__ import annotations

import re
import tempfile
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.redaction import RedactionRegistry


@dataclass
class FakeRunPodHeartbeatEndpoint:
    """Dict-backed test double for the RunPod GraphQL-tag satisfier.

    Mirrors the wire-level shape exactly: sub-second-precision round-trip;
    explicit transport-failure injection point per call type.
    """

    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"FakeRunPodHeartbeatEndpoint: injected write failure for {instance_id}"
            )
        self._slots[instance_id] = ts_local

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"FakeRunPodHeartbeatEndpoint: injected read failure for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_transport_failure(self, method: Literal["read", "write"]) -> None:
        """Arm the next call of ``method`` to raise :class:`TransportError`."""
        if method == "read":
            self._fail_on_read = True
        elif method == "write":
            self._fail_on_write = True
        else:
            raise ValueError(f"method must be 'read' or 'write'; got {method!r}")

    def destroy_instance(self, instance_id: str) -> None:
        """Test helper: simulate the pod being destroyed.

        After this call, ``read(instance_id)`` returns ``None`` per the
        Protocol invariant 'returns None if the instance is gone'.
        """
        self._slots.pop(instance_id, None)


@dataclass
class FakeSkyPilotHeartbeatEndpoint:
    """Dict-backed test double for the future B5b SSH-touch satisfier.

    Mirrors the SkyPilot wire shape: round-trip truncates to seconds
    (``stat -c %Y`` returns POSIX-seconds); cold-vs-warm SSH latency is
    injectable but not actually measured here.
    """

    cold_latency_s: float = 0.0
    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"FakeSkyPilotHeartbeatEndpoint: SSH connection refused for {instance_id}"
            )
        # SkyPilot stores via filesystem mtime — second-precision only.
        truncated = ts_local.replace(microsecond=0)
        self._slots[instance_id] = truncated

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"FakeSkyPilotHeartbeatEndpoint: SSH connection refused for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_ssh_refused(self) -> None:
        """Arm BOTH next read and next write to raise :class:`TransportError`.

        Mirrors the SkyPilot SSH-multiplexer failure mode where one bad
        ControlMaster takes down both directions.
        """
        self._fail_on_read = True
        self._fail_on_write = True

    def destroy_instance(self, instance_id: str) -> None:
        """Test helper: simulate the cluster being torn down."""
        self._slots.pop(instance_id, None)


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


@dataclass
class LocalHeartbeatEndpoint:
    """Thin Protocol-shaped adapter around a dict, for parity tests only.

    LocalProvider already manages heartbeats in an in-memory dict and
    has no production reason to grow a HeartbeatEndpoint satisfier
    (offline tests use LocalProvider directly). This adapter exists so
    the cross-provider parity test (Task e) can parametrize across all
    three satisfiers symmetrically.

    NOT registered as a production satisfier — test fixture only.
    """

    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"LocalHeartbeatEndpoint: injected write failure for {instance_id}"
            )
        self._slots[instance_id] = ts_local

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"LocalHeartbeatEndpoint: injected read failure for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_transport_failure(self, method: Literal["read", "write"]) -> None:
        if method == "read":
            self._fail_on_read = True
        elif method == "write":
            self._fail_on_write = True
        else:
            raise ValueError(f"method must be 'read' or 'write'; got {method!r}")

    def destroy_instance(self, instance_id: str) -> None:
        self._slots.pop(instance_id, None)


@pytest.fixture()
def fake_runpod_heartbeat_endpoint() -> FakeRunPodHeartbeatEndpoint:
    """Fresh fake RunPod heartbeat endpoint per test."""
    return FakeRunPodHeartbeatEndpoint()


@pytest.fixture()
def fake_skypilot_heartbeat_endpoint() -> FakeSkyPilotHeartbeatEndpoint:
    """Fresh fake SkyPilot heartbeat endpoint per test."""
    return FakeSkyPilotHeartbeatEndpoint()


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


# ---------------------------------------------------------------------------
# Post-session thread-dump diagnostic (spec
# docs/superpowers/specs/2026-06-17-pytest-post-session-hang-diagnostic-design.md).
#
# Pytest finishes its test summary then returns from main(); if any
# non-daemon thread is still alive at that point, threading._shutdown()
# blocks the interpreter indefinitely (observed on ubuntu CI run
# 27693732183 — 6 h job cancel; macOS exits cleanly so the leak is
# linux-platform-primitive-bound). This hook surfaces the live-thread
# inventory + stack frames so the next CI run names the leaker instead
# of timing out at 6 h.
# ---------------------------------------------------------------------------


def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001
    """Dump live threads + open FDs to stderr after the test summary line.

    Args:
        session: The pytest ``Session`` object. Unused — present to match the
            documented hook signature.
        exitstatus: The integer exit status pytest will return. Echoed in the
            banner so a green vs. red session is distinguishable in CI logs.
    """
    import io  # noqa: PLC0415
    import os  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import threading  # noqa: PLC0415
    import traceback  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    threads = threading.enumerate()
    main_thread = threading.main_thread()
    non_daemon_extras = [t for t in threads if t is not main_thread and not t.daemon]
    if not non_daemon_extras:
        # Fast path: no non-daemon extras → no leak that can block shutdown.
        sys.stderr.write(
            f"=== POST-SESSION THREAD DUMP === clean ({len(threads)} threads, "
            "no non-daemon extras)\n"
        )
        return

    main_ident = threading.main_thread().ident
    frames = sys._current_frames()

    buf = io.StringIO()
    buf.write(
        f"=== POST-SESSION THREAD DUMP === pid={os.getpid()} "
        f"exitstatus={exitstatus} n_threads={len(threads)}\n"
    )
    for t in threads:
        marker = " (main)" if t.ident == main_ident else ""
        buf.write(
            f"  thread name={t.name!r} ident={t.ident} "
            f"daemon={t.daemon} alive={t.is_alive()}{marker}\n"
        )
        frame = frames.get(t.ident) if t.ident is not None else None
        if frame is None:
            buf.write("    <no Python frame — thread exited or in C extension>\n")
            continue
        for line in traceback.format_stack(frame):
            buf.write("    " + line.rstrip() + "\n")

    try:
        fds = sorted(int(e) for e in os.listdir("/proc/self/fd/"))
        buf.write(f"  open fds: {len(fds)} → {fds}\n")
    except OSError:
        # macOS / non-linux — no /proc. Skip the FD inventory.
        pass

    payload = buf.getvalue()
    sys.stderr.write(payload)
    try:
        Path("tests/_post_session_dump.txt").write_text(payload, encoding="utf-8")
    except OSError:
        # cwd not writable (sandboxed CI step) — stderr is authoritative.
        pass
