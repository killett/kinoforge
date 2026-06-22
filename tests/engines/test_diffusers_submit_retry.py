"""Tests for DiffusersBackend.submit() retry-wrapping (Task 22).

Each test names the concrete bug it catches: a regression in the retry
wrap (or its absence) that would cause the assertion to fail.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from kinoforge.core.interfaces import (
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.engines.diffusers import DiffusersBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(
    http_post: Any,
) -> DiffusersBackend:
    profile = ModelProfile(
        name="test",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda _u: {},
        base_url="http://pod.example",
        probe_profile=profile,
        sleep=lambda _s: None,
    )


def _job() -> GenerationJob:
    return GenerationJob(
        spec={"prompt": "a wide ocean wave"},
        segments=[Segment(prompt="a wide ocean wave")],
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_submit_recovers_from_transient_502() -> None:
    """submit() retries and returns job_id after one transient 502.

    Bug caught: if submit() calls _http_post bare (no retry wrap), the
    first 502 propagates immediately and the job is never submitted.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(502)
        return {"job_id": "jid-42"}

    backend = _make_backend(http_post)
    result = backend.submit(_job())
    assert result == "jid-42"
    assert attempts["n"] == 2


def test_submit_exhaustion_reraises_last_http_error() -> None:
    """submit() re-raises the final transient HTTPError after backoff exhaustion.

    Bug caught: if the helper swallows the last exception (returns None or
    raises a different type), callers cannot distinguish submit failure from
    a successful empty job_id.
    """

    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _http_error(503)

    backend = _make_backend(http_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.submit(_job())
    assert exc_info.value.code == 503
    # 7 attempts = 1 initial + 6 retries (RUNPOD_PROXY_POLICY.backoffs).
    # Locked to the policy structure so a calibration change breaks loudly.
    assert attempts["n"] == 7


def test_submit_non_transient_400_raises_immediately() -> None:
    """submit() re-raises a non-transient 400 on the first attempt without retry.

    Bug caught: if the helper retried 400, every malformed request would
    be replayed N times, amplifying backend load for hard-failure cases.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _http_error(400)

    backend = _make_backend(http_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.submit(_job())
    assert exc_info.value.code == 400
    assert attempts["n"] == 1


def test_submit_recovers_from_tls_reset() -> None:
    """submit() retries and returns job_id after one TLS connection reset.

    Bug caught: if submit() does not wrap URLError / OSError, a single
    TLS reset on the first attempt permanently loses the generation job.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset"))
        return {"job_id": "jid-tls"}

    backend = _make_backend(http_post)
    result = backend.submit(_job())
    assert result == "jid-tls"
    assert attempts["n"] == 2
