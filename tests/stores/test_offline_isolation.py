"""AC4: offline ``pixi run test`` makes no real network calls.

Runs ``pytest tests/stores/`` in a subprocess with a conftest plugin that
monkey-patches ``socket.socket.connect`` to track any non-loopback connection
attempts and asserts the tracked list is empty at session end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_offline_run_makes_no_network_calls(tmp_path: Path) -> None:
    """Run the stores test suite in a subprocess and verify no non-loopback sockets.

    A conftest plugin is written to a temp directory and injected via
    ``-p`` so it is opt-in for this guard test only.  The plugin patches
    ``socket.socket.connect`` at the OS level; any connection whose host
    is not loopback (``127.*``, ``localhost``, ``::1``) is appended to a
    module-level ``_seen`` list.  ``pytest_sessionfinish`` asserts that list
    is empty.

    Bug this catches: a test that accidentally imports an SDK which dials
    out on import, or a fixture that triggers a live connection without the
    ``KINOFORGE_LIVE_TESTS`` gate.
    """
    spy_path = tmp_path / "spy_offline_plugin.py"
    spy_path.write_text(
        '''\
"""Inline pytest plugin: socket spy for offline isolation guard."""

import socket as _socket_module

_seen: list[tuple[str, int]] = []
_real_connect = _socket_module.socket.connect


def _spy_connect(self: _socket_module.socket, addr: object) -> None:  # type: ignore[override]
    try:
        if isinstance(addr, (list, tuple)) and addr:
            host = str(addr[0])
            loopback = (
                host == "localhost"
                or host == "::1"
                or host.startswith("127.")
            )
            if not loopback:
                _seen.append(addr)  # type: ignore[arg-type]
    except Exception:
        pass
    return _real_connect(self, addr)


_socket_module.socket.connect = _spy_connect  # type: ignore[method-assign]


def pytest_sessionfinish(session: object, exitstatus: object) -> None:  # noqa: ARG001
    assert _seen == [], f"unexpected non-loopback socket connections: {_seen}"
'''
    )

    # Run the stores test suite (excluding the live/ subdirectory which
    # requires KINOFORGE_LIVE_TESTS=1 and is already gated by conftest).
    # We pass the absolute path to tests/stores/ so the working-directory
    # of the subprocess does not matter.
    stores_dir = Path(__file__).parent

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(stores_dir),
            "-q",
            # Exclude the live subdirectory (requires KINOFORGE_LIVE_TESTS).
            "--ignore",
            str(stores_dir / "live"),
            # Exclude this file itself to prevent recursive subprocess spawning.
            "--ignore",
            str(stores_dir / "test_offline_isolation.py"),
            "-p",
            "spy_offline_plugin",
        ],
        capture_output=True,
        text=True,
        # PYTHONPATH must include tmp_path so -p can import the plugin,
        # AND the workspace root so kinoforge + tests packages resolve.
        env=_build_env(tmp_path),
    )

    assert result.returncode == 0, (
        "Offline stores test suite failed or detected non-loopback connections.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _build_env(plugin_dir: Path) -> dict[str, str]:
    """Build an environment dict that adds *plugin_dir* to PYTHONPATH.

    Inherits the current process environment (so pixi-installed packages are
    on ``sys.path``) and prepends *plugin_dir* so the spy plugin is importable.

    Args:
        plugin_dir: Directory containing ``spy_offline_plugin.py``.

    Returns:
        A dict suitable for passing to ``subprocess.run(env=...)``.
    """
    import os

    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(plugin_dir)]
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = ":".join(parts)
    # Ensure KINOFORGE_LIVE_TESTS is NOT set so the live gate stays closed.
    env.pop("KINOFORGE_LIVE_TESTS", None)
    return env
