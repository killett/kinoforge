"""ComfyUIBackend.result raises TimeoutError after poll_timeout_s."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import ModelProfile
from kinoforge.engines.comfyui import ComfyUIBackend

_PROBE = ModelProfile(
    name="comfyui",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _unused_post(_url: str, _body: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("submit should not POST in result()-only tests")


def test_timeout_raises_with_status_in_message() -> None:
    """poll_timeout_s=0.2 raises TimeoutError; message contains last_status + exec_node.

    Bug: today ComfyUIBackend.result has no upper-bound timeout — the
    reason the 2026-06-10 stall took 30s of operator patience to surface.
    """

    def _http_get(url: str) -> dict[str, Any]:
        # Simulate ComfyUI returning "running, currently on WanVideoSampler" forever.
        return {
            "status": {
                "status_str": "running",
                "exec_info": {"current_node": "WanVideoSampler"},
            }
        }

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.05,
        poll_timeout_s=0.2,
    )

    with pytest.raises(TimeoutError) as exc_info:
        backend.result("job-789")

    msg = str(exc_info.value)
    assert "last_status=" in msg, f"missing last_status in message: {msg}"
    assert "exec_node=" in msg, f"missing exec_node in message: {msg}"
    assert "WanVideoSampler" in msg, f"missing actual node name in message: {msg}"
