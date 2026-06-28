"""Unit tests for _classify_ephemeral decision tree.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §3.5
"""

from __future__ import annotations

from collections import deque
from typing import Any

from kinoforge.core.reaper import Verdict, _classify_ephemeral, classify


def _entry(
    probe_state: str, created_at_s_ago: float = 60.0, **extra: Any
) -> dict[str, Any]:
    """Build a synthetic ephemeral entry dict matching `_synthesize_ephemeral_entry`."""
    base: dict[str, Any] = {
        "id": "pod-1",
        "provider": "runpod",
        "provider_kind": "runpod",
        "kinoforge_ephemeral": True,
        "probe_state": probe_state,
        "created_at": 1000.0,
    }
    if probe_state == "ok":
        base["container_uptime_s"] = 300.0
        base["gpu_util_pct"] = extra.pop("gpu", 50.0)
        base["cpu_pct"] = extra.pop("cpu", 20.0)
    base.update(extra)
    return base


_NOW = 1060.0  # 60s after created_at=1000
_THRESHOLDS: dict[str, Any] = {
    "max_lifetime_s": 5 * 3600,
    "stall_window_s": 120.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "heartbeat_interval_s": 30.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_classify_dispatches_to_ephemeral_on_sentinel() -> None:
    """`classify()` routes to ephemeral branch when sentinel is True."""
    entry = _entry("not_found")
    verdict = classify(entry, live_pod_ids=set(), now=_NOW, **_THRESHOLDS)
    assert verdict == Verdict.GC_404


def test_classify_uses_heartbeat_branch_when_sentinel_absent() -> None:
    """Regression guard: no `kinoforge_ephemeral` key → heartbeat branch."""
    entry: dict[str, Any] = {
        "id": "ledger-pod",
        "provider": "runpod",
        "provider_kind": "runpod",
    }
    verdict = classify(entry, live_pod_ids={"ledger-pod"}, now=_NOW, **_THRESHOLDS)
    assert verdict != Verdict.GC_404
    assert verdict != Verdict.SKIP_NO_PROBE


def test_ephemeral_probe_not_found_gc_404() -> None:
    verdict = _classify_ephemeral(
        _entry("not_found"), _THRESHOLDS, _NOW, stall_history=None
    )
    assert verdict == Verdict.GC_404


def test_ephemeral_probe_no_substrate_skip() -> None:
    verdict = _classify_ephemeral(
        _entry("no_substrate"), _THRESHOLDS, _NOW, stall_history=None
    )
    assert verdict == Verdict.SKIP_NO_PROBE


def test_ephemeral_probe_failed_returns_probe_failed() -> None:
    verdict = _classify_ephemeral(
        _entry("failed"), _THRESHOLDS, _NOW, stall_history=None
    )
    assert verdict == Verdict.PROBE_FAILED


def test_ephemeral_overage_fires_when_lifetime_exceeded() -> None:
    """created_at = 1000, now = 1000 + max_lifetime + 1 → OVERAGE_REAP."""
    now = 1000.0 + float(_THRESHOLDS["max_lifetime_s"]) + 1.0
    verdict = _classify_ephemeral(_entry("ok"), _THRESHOLDS, now, stall_history=None)
    assert verdict == Verdict.OVERAGE_REAP


def test_ephemeral_overage_takes_precedence_over_stall() -> None:
    """OVERAGE fires even when stall history would otherwise say STALL."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0)] * 10),
    }
    now = 1000.0 + float(_THRESHOLDS["max_lifetime_s"]) + 1.0
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, now, stall_history=history
    )
    assert verdict == Verdict.OVERAGE_REAP


def test_ephemeral_stall_skipped_when_history_none_one_shot_mode() -> None:
    """`kinoforge reap` one-shot passes stall_history=None → STALL never fires."""
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=None
    )
    assert verdict == Verdict.LIVE


def test_ephemeral_stall_window_unsatisfied_returns_live() -> None:
    """N-1 zero-util samples → not yet stall; LIVE."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)]),  # 3, need 4
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history
    )
    assert verdict == Verdict.LIVE


def test_ephemeral_stall_window_satisfied_returns_stall_reap() -> None:
    """N consecutive zero-util samples → STALL_REAP."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0)] * 4),
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history
    )
    assert verdict == Verdict.STALL_REAP


def test_ephemeral_stall_window_resets_on_recovery_sample() -> None:
    """One sample above threshold breaks the streak → LIVE."""
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(0.0, 0.0), (60.0, 30.0), (0.0, 0.0), (0.0, 0.0)]),
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), _THRESHOLDS, _NOW, stall_history=history
    )
    assert verdict == Verdict.LIVE


def test_ephemeral_no_idle_reap_even_with_zero_util() -> None:
    """IDLE_REAP must never fire from the ephemeral branch.

    Model-load periods (Wan 14B weight fetch, 4-8 minutes at 0% GPU) would
    trip false positives otherwise.
    """
    history: dict[str, deque[tuple[float, float]]] = {"pod-1": deque([(0.0, 0.0)] * 4)}
    thresholds = {**_THRESHOLDS, "idle_timeout_s": 1.0}
    verdict = _classify_ephemeral(
        _entry("ok", gpu=0.0, cpu=0.0), thresholds, _NOW, stall_history=history
    )
    assert verdict != Verdict.IDLE_REAP


def test_ephemeral_live_when_util_high() -> None:
    history: dict[str, deque[tuple[float, float]]] = {
        "pod-1": deque([(80.0, 40.0)] * 4)
    }
    verdict = _classify_ephemeral(
        _entry("ok", gpu=80.0, cpu=40.0), _THRESHOLDS, _NOW, stall_history=history
    )
    assert verdict == Verdict.LIVE


def test_default_apply_policy_includes_gc_404() -> None:
    """DEFAULT_APPLY_POLICY must include GC_404 so --apply removes stale rows."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY

    assert Verdict.GC_404 in DEFAULT_APPLY_POLICY.act_verdicts


def test_default_apply_policy_excludes_skip_no_probe_and_probe_failed() -> None:
    """SKIP_NO_PROBE and PROBE_FAILED are log-only (no state mutation)."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY

    assert Verdict.SKIP_NO_PROBE not in DEFAULT_APPLY_POLICY.act_verdicts
    assert Verdict.PROBE_FAILED not in DEFAULT_APPLY_POLICY.act_verdicts
