"""Phase 51 — parser handles real ComfyUI envelope shape, queue probe fires during execution.

Phase 50 shipped `_extract_poll_fields` against the flat fixture shape only
(``{"status": {"status_str": ...}}`` at the top level). Real ComfyUI
``/history/{prompt_id}`` nests the per-job dict under the ``prompt_id`` key
(``{prompt_id: {"status": {"status_str": ...}, "outputs": {...}}}``), and
returns ``{}`` for jobs that are still executing or queued. With the parser
reading the wrong shape, ``status`` was reported as ``"unknown"`` forever in
production — which then gated the ``/queue`` probe out, leaving the operator
with no observability into a running job. A healthy Wan-14B sampler at
100% GPU then died to the default 600s ``poll_timeout_s``.

These tests pin:

1. ``_extract_poll_fields`` descends into ``envelope[prompt_id]`` when the
   top-level keys are absent (real shape), and keeps the flat shape working
   (test-fixture shape).
2. ``ComfyUIBackend.result`` probes ``/queue`` when status is ``"unknown"``
   (real-empty ``/history``) or ``"queued"`` — populating ``queue_pos`` so
   the operator can distinguish "still running" from "stuck".
3. The default ``poll_timeout_s`` is large enough that a real Wan-14B
   sampler does not get killed mid-run.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from kinoforge.core.interfaces import ModelProfile
from kinoforge.engines.comfyui import ComfyUIBackend, _extract_poll_fields

_PROBE = ModelProfile(
    name="comfyui",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)

_PROMPT_ID = "929aecfb-22c9-4cbf-85e5-4dc92042f2d7"


def _no_post(_url: str, _body: dict[str, Any]) -> dict[str, Any]:
    raise AssertionError("submit should not POST in result()-only tests")


def test_extract_poll_fields_nested_shape() -> None:
    """Real ComfyUI envelope nests status under prompt_id — parser must descend.

    Bug today: parser reads ``envelope["status"]`` only. Real ``/history``
    response is ``envelope[prompt_id]["status"]``. Returned ``"unknown"``
    even for completed jobs — masked both healthy mid-run state and final
    success status.
    """
    envelope = {
        _PROMPT_ID: {
            "status": {
                "status_str": "success",
                "completed": True,
                "exec_info": {"current_node": None},
            },
            "outputs": {"node-1": {"images": [{"filename": "out.mp4"}]}},
        }
    }
    status, queue_pos, exec_node = _extract_poll_fields(envelope, _PROMPT_ID)
    assert status == "success", f"expected nested status_str; got {status!r}"
    assert queue_pos is None
    assert exec_node is None


def test_extract_poll_fields_nested_shape_with_exec_node() -> None:
    """Nested-shape envelope with non-null exec_info.current_node populates exec_node."""
    envelope = {
        _PROMPT_ID: {
            "status": {
                "status_str": "executing",
                "exec_info": {"current_node": "WanVideoSampler"},
            },
        }
    }
    status, _, exec_node = _extract_poll_fields(envelope, _PROMPT_ID)
    assert status == "executing"
    assert exec_node == "WanVideoSampler"


def test_extract_poll_fields_flat_shape_unchanged() -> None:
    """Flat shape (test fixture) keeps working — back-compat."""
    envelope: dict[str, Any] = {
        "status": {
            "status_str": "running",
            "exec_info": {"current_node": "KSampler"},
        }
    }
    status, _, exec_node = _extract_poll_fields(envelope, "any-id")
    assert status == "running"
    assert exec_node == "KSampler"


def test_extract_poll_fields_empty_envelope_returns_unknown() -> None:
    """ComfyUI returns ``{}`` for jobs not yet in history → ``"unknown"`` sentinel.

    This is the steady-state during execution: ``/history/{id}`` stays empty
    until the job completes. Parser MUST return the unknown sentinel here
    (not raise) so the poll loop continues + the ``/queue`` probe fires.
    """
    status, queue_pos, exec_node = _extract_poll_fields({}, _PROMPT_ID)
    assert status == "unknown"
    assert queue_pos is None
    assert exec_node is None


def test_extract_poll_fields_real_shape_without_job_id_falls_back() -> None:
    """job_id=None preserves legacy callers; nested shape returns 'unknown' then."""
    envelope = {
        _PROMPT_ID: {
            "status": {"status_str": "success"},
        }
    }
    status, _, _ = _extract_poll_fields(envelope, None)
    assert status == "unknown"


def test_queue_pos_populates_when_history_empty_but_job_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mid-run ``/history`` is ``{}`` but job is in ``/queue.queue_running``.

    Phase 51: ``/queue`` probe now fires when status is ``"unknown"`` too,
    not just ``"queued"``. Operator sees ``queue_pos=0`` in the log during
    execution — distinguishes "healthy and running" from "stuck on a
    stale prompt_id ComfyUI has lost".
    """
    job_id = "exec-job-abc"
    tick = [0]

    def _http_get(url: str) -> dict[str, Any]:
        if url.endswith("/queue"):
            return {
                "queue_running": [[0, job_id, {}, {}, []]],
                "queue_pending": [],
            }
        # /history/{id}: empty during execution, completes on tick 3.
        tick[0] += 1
        if tick[0] >= 3:
            return {
                job_id: {
                    "outputs": {"n": {"images": [{"filename": "x.mp4"}]}},
                    "status": {"status_str": "success"},
                }
            }
        return {}

    backend = ComfyUIBackend(
        http_post=_no_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.comfyui")
    backend.result(job_id)

    poll_lines = [r.message for r in caplog.records if "comfyui poll" in r.message]
    assert any("queue_pos=0" in line for line in poll_lines), (
        f"expected queue_pos=0 to populate when /history is empty but job runs: "
        f"{poll_lines}"
    )


def test_queue_pos_remains_none_when_job_not_in_running_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Queue probe fires but reports None when the job_id is not present.

    Discriminating bug: a parser regression that returns the position of
    the wrong entry, or a queue envelope shape change, would surface here
    as a non-None queue_pos.
    """
    job_id = "missing-job"
    tick = [0]

    def _http_get(url: str) -> dict[str, Any]:
        if url.endswith("/queue"):
            return {
                "queue_running": [[0, "some-other-job", {}, {}, []]],
                "queue_pending": [],
            }
        tick[0] += 1
        if tick[0] >= 2:
            return {
                job_id: {
                    "outputs": {"n": {"images": [{"filename": "x.mp4"}]}},
                    "status": {"status_str": "success"},
                }
            }
        return {}

    backend = ComfyUIBackend(
        http_post=_no_post,
        http_get=_http_get,
        base_url="http://x",
        probe=_PROBE,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.comfyui")
    backend.result(job_id)

    pre_completion = [
        r.message
        for r in caplog.records
        if "comfyui poll" in r.message and "elapsed=0.0s" in r.message
    ]
    assert pre_completion, "expected at least one pre-completion log line"
    assert all("queue_pos=None" in line for line in pre_completion), (
        f"unmatched job_id must keep queue_pos=None: {pre_completion}"
    )


def test_default_poll_timeout_s_covers_wan_14b() -> None:
    """Wan 14B sampler can run 25-40 min on A5000-class GPUs.

    Phase 51 bump: 600s default killed a healthy run on pod
    ``2fhv2v3cccs98d`` at elapsed=602.8s while the GPU was at 100%.
    1800s (30 min) is the new floor; operators override per-config
    for slower setups.
    """
    backend = ComfyUIBackend(
        http_post=lambda *_a, **_k: {},
        http_get=lambda *_a, **_k: {},
        base_url="http://x",
        probe=_PROBE,
    )
    assert backend._poll_timeout_s == 1800.0, (
        f"default poll_timeout_s must cover Wan 14B; got {backend._poll_timeout_s}"
    )


def test_default_poll_timeout_s_in_pydantic_config() -> None:
    """``ComfyUIEngineConfig.poll_timeout_s`` default matches backend default.

    Drift between the two would silently re-introduce the 600s cap via
    the engine.backend() resolution path.
    """
    from kinoforge.core.config import ComfyUIEngineConfig

    cfg = ComfyUIEngineConfig(version="0.3.10")
    assert cfg.poll_timeout_s == 1800.0
