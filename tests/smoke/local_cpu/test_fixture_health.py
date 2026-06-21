"""Smoke: the uvicorn fixture spawns + /health responds 200."""

from __future__ import annotations

import urllib.request


def test_uvicorn_fixture_health(uvicorn_server: str) -> None:
    """Bug: fixture yields before /health is actually 200 → flake."""
    with urllib.request.urlopen(  # noqa: S310
        f"{uvicorn_server}/health", timeout=2
    ) as r:
        assert r.status == 200
