"""Tests for DiffusersEngine.extract_last_frame() artifact-download retry wrap (Task 22).

The artifact wrap is on DiffusersEngine (not DiffusersBackend) and calls
retry_proxy_call around self._http_get_bytes(artifact.url).  All failures
are wrapped in FrameExtractionError after exhaustion.
"""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.interfaces import Artifact, ModelProfile
from kinoforge.engines.diffusers import DiffusersEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24  # minimal fake PNG bytes


def _make_engine(
    http_get_bytes: Any,
) -> DiffusersEngine:
    """Build a DiffusersEngine with all I/O seams stubbed out."""
    profile = ModelProfile(
        name="test",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1280, 720),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    return DiffusersEngine(
        http_get_bytes=http_get_bytes,
        # ffmpeg_run: just return whatever the "video bytes" are as-is so
        # we never need a real ffmpeg binary in tests.
        ffmpeg_run=lambda _argv, stdin: stdin,
        sleep=lambda _s: None,
        probe_profile=profile,
    )


def _artifact(url: str = "http://pod.example/output.mp4") -> Artifact:
    return Artifact(url=url, filename="output.mp4")


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


def test_artifact_recovers_from_tls_reset() -> None:
    """extract_last_frame() retries and returns bytes after one TLS reset.

    Bug caught: if the http_get_bytes call is not wrapped, a single TLS
    connection reset on the first download attempt permanently loses the
    artifact — forcing the caller to re-run the entire generation job.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_get_bytes(url: str) -> bytes:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "Connection reset"))
        return _FAKE_PNG

    engine = _make_engine(http_get_bytes)
    result = engine.extract_last_frame(_artifact())
    # ffmpeg_run is stubbed to return its stdin verbatim.
    assert result == _FAKE_PNG
    assert attempts["n"] == 2


def test_artifact_exhaustion_raises_frame_extraction_error() -> None:
    """Backoff exhaustion wraps the final URLError in FrameExtractionError.

    Bug caught: if the except block is absent, the raw URLError leaks out
    of extract_last_frame — callers expecting FrameExtractionError miss it.
    """

    attempts = {"n": 0}

    def http_get_bytes(url: str) -> bytes:
        attempts["n"] += 1
        raise urllib.error.URLError("dns resolution failed")

    engine = _make_engine(http_get_bytes)
    with pytest.raises(FrameExtractionError) as exc_info:
        engine.extract_last_frame(_artifact())
    # The error message must mention the artifact URL so operators know
    # which pod/file the download was attempted from.
    assert "http://pod.example/output.mp4" in str(exc_info.value)
    # 7 attempts = 1 initial + 6 retries (RUNPOD_PROXY_POLICY.backoffs).
    assert attempts["n"] == 7


def test_artifact_non_transient_410_raises_immediately_as_frame_extraction_error() -> (
    None
):
    """410 Gone re-raises immediately (no retry) and wraps in FrameExtractionError.

    Bug caught: if the helper retried non-transient codes like 410, every
    deleted-artifact fetch would be replayed N times, wasting time and
    amplifying load on the inference server.  The attempt counter must
    be exactly 1.
    """
    attempts: dict[str, int] = {"n": 0}

    def http_get_bytes(url: str) -> bytes:
        attempts["n"] += 1
        raise _http_error(410)

    engine = _make_engine(http_get_bytes)
    with pytest.raises(FrameExtractionError):
        engine.extract_last_frame(_artifact())
    assert attempts["n"] == 1
