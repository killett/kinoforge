"""Tests for DiffusersBackend.result() restructure (Task 20).

Each test states the behavior under test and the concrete bug it
catches. Mocks live at the HTTP boundary (http_get callable) plus
the clock + sleep seams; the retry_proxy_call helper itself is NOT
mocked.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled, GenerationError
from kinoforge.core.interfaces import ModelProfile
from kinoforge.engines.diffusers import DiffusersBackend


def _make_backend(
    *,
    http_get: Callable[[str], dict[str, Any]],
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_timeout_s: float = 60.0,
    poll_interval_s: float = 0.0,
) -> DiffusersBackend:
    return DiffusersBackend(
        http_post=http_post or (lambda _u, _b: {"job_id": "jid"}),
        http_get=http_get,
        base_url="http://pod.example",
        probe_profile=ModelProfile(
            name="test",
            max_frames=81,
            fps=24,
            supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False,
            supports_joint_audio=False,
        ),
        sleep=sleep or (lambda _s: None),
        poll_timeout_s=poll_timeout_s,
        poll_interval_s=poll_interval_s,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="x",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --- cancel_token honoring -------------------------------------------


def test_cancel_token_raises_before_first_io() -> None:
    """Catches: del cancel_token regression or pre-I/O check missing.

    A token set before result() is called must abort BEFORE any
    http_get call lands.
    """
    calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"status": "done", "filename": "x.mp4"}

    backend = _make_backend(http_get=http_get)
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.result("jid", cancel_token=token)
    assert calls == []


def test_cancel_token_raises_during_interpoll_wait() -> None:
    """Catches: inter-poll sleep not token-aware.

    A token set between polls must surface as Cancelled within the
    next poll_interval_s window, not at the next http_get call.
    """
    state = {"polls": 0}
    token = CancelToken()

    def http_get(url: str) -> dict[str, Any]:
        state["polls"] += 1
        if state["polls"] == 1:
            token.set()
        return {"status": "pending"}

    backend = _make_backend(http_get=http_get, poll_interval_s=10.0)

    with pytest.raises(Cancelled):
        backend.result("jid", cancel_token=token)
    assert state["polls"] == 1


# --- timeout bounds ---------------------------------------------------


def test_wall_clock_timeout_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches: poll_idx-only bound (sleep-stubbed tests would hang).

    Stub time.monotonic so the clock advances past poll_timeout_s
    within a few iterations. Expect TimeoutError.
    """
    times = iter([0.0, 0.5, 1.5, 100.0, 200.0, 300.0, 400.0, 500.0])

    def fake_monotonic() -> float:
        return next(times)

    import kinoforge.engines.diffusers as diffusers_mod

    monkeypatch.setattr(diffusers_mod.time, "monotonic", fake_monotonic)  # type: ignore[attr-defined]

    def http_get(url: str) -> dict[str, Any]:
        return {"status": "pending"}

    backend = _make_backend(http_get=http_get, poll_timeout_s=10.0)
    with pytest.raises(TimeoutError):
        backend.result("jid")


def test_max_poll_belt_and_braces() -> None:
    """Catches: removing _MAX_POLL fallback (sleep-stubbed tests would loop forever)."""
    import kinoforge.engines.diffusers as diffusers_mod

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(diffusers_mod, "_MAX_POLL", 5)

        def http_get(url: str) -> dict[str, Any]:
            return {"status": "pending"}

        backend = _make_backend(
            http_get=http_get,
            poll_timeout_s=100000.0,
            poll_interval_s=0.0,
        )
        with pytest.raises(TimeoutError):
            backend.result("jid")


def test_last_transient_preferred_over_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: bare TimeoutError masking the actual proxy failure.

    If sustained 502s cause the timeout, operators must see the
    HTTPError(502) not a generic TimeoutError.
    """
    import kinoforge.engines.diffusers as diffusers_mod

    # start=0.0, elapsed-check-1=0.5 (< timeout → proceed to HTTP call),
    # elapsed-check-2=100.0 (> timeout → raise last_transient).
    # retry_proxy_call does NOT call time.monotonic, so only 3 ticks needed.
    times = iter([0.0, 0.5, 100.0] + [200.0 + i * 50.0 for i in range(20)])
    monkeypatch.setattr(diffusers_mod.time, "monotonic", lambda: next(times))  # type: ignore[attr-defined]

    def http_get(url: str) -> dict[str, Any]:
        raise _http_error(502)

    backend = _make_backend(http_get=http_get, poll_timeout_s=10.0, poll_interval_s=0.0)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.result("jid")
    assert exc_info.value.code == 502


# --- the actual original crash ---------------------------------------


def test_tls_reset_absorbed_then_done() -> None:
    """Catches: the exact ConnectionResetError crash from 2026-06-21.

    Two URLError(ConnectionResetError)s in a row, then status=done.
    Expect the helper to absorb both and return an Artifact.
    """
    calls = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return {"status": "done", "filename": "out.mp4"}

    backend = _make_backend(http_get=http_get)
    artifact = backend.result("jid")
    assert artifact.filename == "out.mp4"
    assert artifact.url == "http://pod.example/artifacts/out.mp4"
    assert artifact.meta == {"job_id": "jid"}
    assert calls["n"] == 3


# --- status branches --------------------------------------------------


def test_status_done_builds_url_from_base() -> None:
    """Catches: regression where server-supplied (localhost) URL is honored.

    Workspace cannot reach localhost on the pod; URL must be built
    from backend base_url. Server's url field is intentionally ignored.
    """

    def http_get(url: str) -> dict[str, Any]:
        return {
            "status": "done",
            "filename": "v.mp4",
            "url": "http://localhost:8000/artifacts/v.mp4",
        }

    backend = _make_backend(http_get=http_get)
    artifact = backend.result("jid")
    assert artifact.url == "http://pod.example/artifacts/v.mp4"


def test_status_error_raises_generation_error() -> None:
    """Catches: error-path swallowed during refactor."""

    def http_get(url: str) -> dict[str, Any]:
        return {"status": "error", "error": "out of memory"}

    backend = _make_backend(http_get=http_get)
    with pytest.raises(GenerationError, match="out of memory"):
        backend.result("jid")
