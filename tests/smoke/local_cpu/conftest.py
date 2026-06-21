"""uvicorn subprocess fixture for Tier-1 local CPU smoke."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _await_health(base_url: str, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310
                f"{base_url}/health", timeout=1
            ) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.1)
    raise RuntimeError(
        f"uvicorn /health did not become ready within {timeout_s}s — last: {last_exc!r}"
    )


@pytest.fixture
def uvicorn_server() -> Iterator[str]:
    """Spawn wan_t2v_server on localhost with the stub pipe; yield base URL."""
    port = _pick_free_port()
    env = dict(os.environ)
    env["KINOFORGE_DIFFUSERS_LOAD_STUB"] = (
        "tests.smoke.local_cpu.stub_pipe._stub_diffusers_load"
    )
    # Subprocess needs the workspace on sys.path to import the dotted
    # stub callable.
    repo_root = str(Path(__file__).resolve().parents[3])
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(
        os.pathsep
    )
    # Local Tier-1 server doesn't gate on ?api_key=; ensure the harness
    # http helpers don't append the suffix.
    env.pop("RUNPOD_API_KEY", None)
    proc = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "kinoforge.engines.diffusers.servers.wan_t2v_server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _await_health(base)
        yield base
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
