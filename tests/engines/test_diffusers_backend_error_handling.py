"""Tests for DiffusersBackend error-handling audit gaps.

Two gaps surfaced by the Wan 2.2 spec (2026-06-19):

E.1 result() looped to TimeoutError when the server reported
    status="error". The operator never saw the underlying error
    message; instead they got a generic poll-exhausted message
    referring to an opaque job_id.

E.2 wait_for_ready already swallows transient exceptions inside its
    poll loop, so the proxy 404 race is implicitly handled. A
    regression test pins that behaviour so a future "let's surface
    poll exceptions" refactor doesn't reopen the race.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import GenerationError
from kinoforge.core.interfaces import Instance, ModelProfile
from kinoforge.engines.diffusers import DiffusersBackend, DiffusersEngine

_PROBE = ModelProfile(
    name="diffusers-test",
    max_frames=24,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(1024, 576),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _make_backend(
    http_post: Any = None,
    http_get: Any = None,
    sleep: Any = None,
    base_url: str = "http://localhost:8000",
) -> DiffusersBackend:
    return DiffusersBackend(
        http_post=http_post or (lambda url, body: {}),
        http_get=http_get or (lambda url: {}),
        sleep=sleep or (lambda s: None),
        base_url=base_url,
        probe_profile=_PROBE,
    )


def test_result_raises_generation_error_on_server_status_error() -> None:
    # Bug caught: result() loops to TimeoutError when the server already
    # reported status="error". The operator never sees the actual error
    # message — they get a confusing "did not complete within N polls"
    # referring to an opaque job_id, even though /status returned a
    # clear error 4 seconds in.
    calls = {"count": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["count"] += 1
        return {"status": "error", "error": "CUDA out of memory: 23.5 GiB"}

    backend = _make_backend(http_get=http_get)
    with pytest.raises(GenerationError, match="CUDA out of memory"):
        backend.result("job-xyz")
    assert calls["count"] == 1, (
        f"expected immediate raise on first error poll, got {calls['count']} polls"
    )


def test_result_still_returns_artifact_on_status_done() -> None:
    # Regression: error short-circuit must NOT break the happy path.
    def http_get(url: str) -> dict[str, Any]:
        return {
            "status": "done",
            "filename": "abc123.mp4",
            "url": "http://localhost:8000/artifacts/abc123.mp4",
        }

    backend = _make_backend(http_get=http_get)
    art = backend.result("job-xyz")
    assert art.filename == "abc123.mp4"
    assert art.url.endswith("/artifacts/abc123.mp4")
    assert art.meta == {"job_id": "job-xyz"}


def test_result_keeps_polling_on_status_running() -> None:
    # Regression: error short-circuit must not eat the running state.
    polls = iter(
        [
            {"status": "running"},
            {"status": "running"},
            {
                "status": "done",
                "filename": "abc.mp4",
                "url": "http://h/artifacts/abc.mp4",
            },
        ]
    )

    def http_get(url: str) -> dict[str, Any]:
        return next(polls)

    sleep_calls: list[float] = []
    backend = _make_backend(http_get=http_get, sleep=sleep_calls.append)
    art = backend.result("job-xyz")
    assert art.filename == "abc.mp4"
    assert len(sleep_calls) == 2


def test_wait_for_ready_retries_through_transient_404() -> None:
    # Regression for E.2: wait_for_ready already wraps each /health
    # poll in a broad except, so a 404 from the RunPod proxy startup
    # window does NOT abort the wait. Pin this so a future refactor
    # doesn't reopen the race.
    poll_results: list[Any] = [
        FileNotFoundError("simulated 404"),
        FileNotFoundError("simulated 404"),
        {"ready": True, "model": "Wan-AI/Wan2.2-T2V-A14B"},
    ]
    poll_iter = iter(poll_results)

    def http_get(url: str) -> dict[str, Any]:
        nxt = next(poll_iter)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    engine = DiffusersEngine()
    instance = Instance(
        id="pod-1",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://localhost:8000"},
        tags={},
    )

    sleeps: list[float] = []

    def get_inst(_id: str) -> Instance:
        return instance

    engine.wait_for_ready(
        instance,
        http_get=http_get,
        sleep=sleeps.append,
        get_instance=get_inst,
        timeout_s=60.0,
    )
    assert len(sleeps) >= 2
