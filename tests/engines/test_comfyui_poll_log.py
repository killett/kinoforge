"""Every ComfyUIBackend.result poll tick emits the structured INFO log line."""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from kinoforge.core.interfaces import ModelProfile
from kinoforge.engines.comfyui import ComfyUIBackend

POLL_LOG_RE = re.compile(
    r"comfyui poll job=\S+ elapsed=\d+\.\d+s status=\S+ queue_pos=\S+ exec_node=\S+"
)

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


def test_each_tick_emits_structured_log(caplog: pytest.LogCaptureFixture) -> None:
    """Three poll ticks → three matching INFO lines.

    Bug: today the poll loop emits no per-tick log; operator cannot tell
    where the stall is.
    """
    tick = [0]

    def _http_get(url: str) -> dict[str, Any]:
        tick[0] += 1
        if tick[0] >= 3:
            return {
                "outputs": {
                    "node-1": {"images": [{"filename": "out.mp4"}]},
                }
            }
        return {
            "status": {
                "status_str": "running",
                "exec_info": {"current_node": "KSampler"},
            }
        }

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.comfyui")
    try:
        backend.result("job-xyz")
    except Exception:
        pass  # _build_artifact may need extra fakes; we only care about the log lines.

    poll_lines = [r.message for r in caplog.records if POLL_LOG_RE.search(r.message)]
    assert len(poll_lines) >= 3, (
        f"expected >=3 structured log lines, got {len(poll_lines)}: {poll_lines}"
    )


def test_queue_pos_none_when_not_queued(caplog: pytest.LogCaptureFixture) -> None:
    """queue_pos field is rendered as `None` when status != queued."""

    def _http_get(url: str) -> dict[str, Any]:
        return {
            "outputs": {"node": {"images": [{"filename": "x.mp4"}]}},
            "status": {"status_str": "complete"},
        }

    backend = ComfyUIBackend(
        http_post=_unused_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.comfyui")
    try:
        backend.result("job-q")
    except Exception:
        pass

    poll_lines = [r.message for r in caplog.records if "comfyui poll" in r.message]
    assert any("queue_pos=None" in line for line in poll_lines), (
        f"expected queue_pos=None: {poll_lines}"
    )
