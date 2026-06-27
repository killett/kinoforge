"""Cleanup paths — ensure stale index rows do not accumulate.

destroy_confirmed (chokepoint for sweeper / explicit destroy / reaper actor)
+ try_warm_attach_with_swap exception arm (Path 3 — pod 404 during attach).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import LoraSwapPodUnreachableError, TeardownError
from kinoforge.core.lifecycle import destroy_confirmed
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.core.warm_reuse.integration import try_warm_attach_with_swap
from kinoforge.core.warm_reuse.matcher import (
    SwapPlan,
    WarmAttachMatch,
    find_warm_attach_candidate,
)
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path)


def _seed(store: LocalArtifactStore, pod_id: str = "pod-A") -> EphemeralIndex:
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id=pod_id,
            warm_attach_key="wak-X",
            kinoforge_key="cap-X",
            endpoint_url=f"https://{pod_id}.example.invalid",
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )
    return idx


def test_destroy_confirmed_removes_row_on_success(store: LocalArtifactStore) -> None:
    """Bug: sweeper/destroy success leaves stale row → matcher attaches to ghost."""
    idx = _seed(store)

    provider = MagicMock()
    provider.list_instances.return_value = []  # confirms destroyed

    destroy_confirmed(provider, "pod-A", ephemeral_index=idx, sleep=lambda _: None)

    assert idx.rows() == [], "row must be removed after confirmed destroy"


def test_destroy_confirmed_does_not_remove_row_on_failure(
    store: LocalArtifactStore,
) -> None:
    """Bug: row vanishes even though pod still alive → matcher misses live pod."""
    idx = _seed(store)

    provider = MagicMock()
    provider.list_instances.return_value = [
        MagicMock(id="pod-A")
    ]  # pod still alive after retries

    with pytest.raises(TeardownError):
        destroy_confirmed(
            provider,
            "pod-A",
            ephemeral_index=idx,
            retries=2,
            sleep=lambda _: None,
        )

    assert len(idx.rows()) == 1, "row must survive when destroy did NOT confirm"


def test_destroy_confirmed_default_none_does_not_touch_index(
    store: LocalArtifactStore,
) -> None:
    """Bug: default kwarg accidentally removes rows in non-ephemeral context."""
    idx = _seed(store)

    provider = MagicMock()
    provider.list_instances.return_value = []

    destroy_confirmed(provider, "pod-A", sleep=lambda _: None)

    assert len(idx.rows()) == 1


def test_matcher_probe_404_silently_skips_and_removes_row(
    store: LocalArtifactStore,
) -> None:
    """Bug: matcher propagates probe-404 + leaves stale row → next attach repeats 404."""
    idx = _seed(store, pod_id="pod-404")

    cfg = MagicMock()
    cfg.capability_key.return_value.derive.return_value = "cap-X"
    cfg.capability_key.return_value.warm_attach_key.return_value.derive.return_value = (
        "wak-X"
    )
    cfg.capability_key.return_value.lora_stack.return_value.refs = []

    class _FakeLedger:
        def find_pods_by_warm_attach_key(self, wak: str) -> list[dict[str, Any]]:
            return []

    class _LockReg:
        def __contains__(self, _id: str) -> bool:
            return False

        def acquire(self, _id: str, *, blocking: bool = False) -> bool:
            return True

        def release(self, _id: str) -> None:
            return None

    def probe(pod_id: str) -> Any:
        raise LoraSwapPodUnreachableError(pod_id=pod_id, underlying="probe 404")

    match = find_warm_attach_candidate(
        cfg=cfg,
        ledger=_FakeLedger(),
        pod_lock_registry=_LockReg(),
        re_probe=probe,
        ephemeral_index=idx,
    )

    assert match is None, "matcher must silently skip the 404'd pod, not raise"
    assert idx.rows() == [], "matcher must remove stale row before continuing"


def test_path3_swap_time_404_removes_row(store: LocalArtifactStore) -> None:
    """Bug: 404 during /lora/set_stack leaves stale row → next attach repeats 404."""
    from unittest.mock import patch

    idx = _seed(store, pod_id="pod-A")

    match = WarmAttachMatch(
        pod_id="pod-A",
        pod_entry={"id": "pod-A", "warm_attach_key": "wak-X"},
        swap_plan=SwapPlan(evict=[], download=["new-ref"], estimated_cost_seconds=0.0),
    )

    backend = MagicMock()
    backend.set_lora_stack.side_effect = LoraSwapPodUnreachableError(
        pod_id="pod-A", underlying="set_stack 404"
    )

    cfg = MagicMock()
    ledger = MagicMock()
    ledger.touch = MagicMock()
    pod_lock_registry = MagicMock()
    pod_lock_registry.release = MagicMock()

    with (
        patch(
            "kinoforge.core.warm_reuse.integration.find_warm_attach_candidate",
            return_value=match,
        ),
        pytest.raises(LoraSwapPodUnreachableError),
    ):
        try_warm_attach_with_swap(
            cfg,
            ledger,
            build_backend=lambda _id: backend,
            pod_lock_registry=pod_lock_registry,
            ephemeral_index=idx,
            download_specs={"new-ref": {"size_hint": 0, "url": "u", "headers": {}}},
        )

    assert idx.rows() == [], "Path 3 must remove the row before re-raising"
