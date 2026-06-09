"""fal has no DELETE endpoint — ``_delete`` raises; manual URL is empty.

Pre-flight (Task 18) refuses ephemeral on the fal engine before any
submit fires; the runtime branch here is belt-and-suspenders.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import EphemeralDeleteUnsupportedError
from kinoforge.core.interfaces import ModelProfile

_PROBE = ModelProfile(
    name="probe",
    max_frames=120,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _backend() -> object:
    """Construct a ``FalBackend`` with stub seams."""
    from kinoforge.engines.fal import FalBackend

    return FalBackend(
        endpoint="fal-ai/wan-t2v",
        queue_base="https://queue.example/fal",
        api_key="k-fake",
        url_path="video.url",
        asset_paths={},
        profile=_PROBE,
        http_post=lambda *a, **k: {"request_id": "req-1"},
        http_get=lambda *a, **k: {"status": "IN_PROGRESS"},
    )


def test_fal_delete_raises_unsupported() -> None:
    """fal._delete raises so a bypassed pre-flight cannot silently leak.

    Would-fail-bug: a no-op ``_delete`` would let any caller that
    bypassed pre-flight (e.g. a direct SDK consumer) believe the
    ephemeral scrub succeeded while the prompt-laden request_id stayed
    on fal's dashboard.
    """
    backend = _backend()
    with pytest.raises(EphemeralDeleteUnsupportedError, match="DELETE endpoint"):
        backend._delete("req-abc")  # type: ignore[attr-defined]


def test_fal_manual_url_empty() -> None:
    """fal has no browser-facing per-job dashboard URL.

    Would-fail-bug: returning a synthetic URL would 404 in the
    operator's browser; pre-flight refuses fal anyway, so the empty
    sentinel is the honest answer.
    """
    from kinoforge.engines.fal import FalBackend

    assert FalBackend.manual_cleanup_url("req-abc") == ""
