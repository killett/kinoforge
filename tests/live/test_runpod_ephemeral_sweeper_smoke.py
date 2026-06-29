"""Live smoke: RunPod ephemeral-pod reap via real GraphQL probe.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.10

Real evidence captured 2026-06-28 in
``tests/live/evidence/2026-06-28-sweeper-ephemeral-reap/evidence.json``.

Scope: validates the GC_404 path against real RunPod GraphQL. A fake
pod_id is injected into the EphemeralIndex; the real provider returns
``data.pod = null`` (404); sweep classifies GC_404; ``--apply``
removes the row. No actual pod is provisioned — $0 spend.

The STALL_REAP path against a real wedged pod is left to the manual
operator script under tests/live/evidence/...; it requires
ssh-based selfterm-kill which the CLI does not currently expose. The
classify + sweep + act_on_verdict logic for STALL_REAP is fully
covered by tests/integration/test_sweeper_reaps_ephemeral_stall.py.

Auto-skips when ``RUNPOD_API_KEY`` is unset so default test runs
remain offline.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.clock import RealClock
from kinoforge.core.interfaces import CredentialProvider
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.stores.local import LocalArtifactStore

pytestmark = pytest.mark.skipif(
    not os.environ.get("RUNPOD_API_KEY"),
    reason="live smoke — requires RUNPOD_API_KEY in env",
)


class _EnvCreds(CredentialProvider):
    def get(self, key: str) -> str | None:
        return os.environ.get(key)


_THRESHOLDS = {
    "max_lifetime_s": 5 * 3600.0,
    "stall_window_s": 0.0,
    "heartbeat_interval_s": 30.0,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 10.0,
    "idle_timeout_s": 600.0,
    "grace_after_session_s": 60.0,
    "restart_loop_window_s": 600.0,
    "restart_loop_uptime_threshold_s": 60.0,
}


def test_runpod_gc_404_against_live_graphql() -> None:
    """Real RunPod GraphQL returns 404 for fake pod_id → sweeper GC_404 path."""
    from kinoforge.providers.runpod import RunPodProvider

    fake_pod_id = "fake-nonexistent-sweeper-smoke-zz"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = LocalArtifactStore(root=root)
        ledger = Ledger(store=store, run_id="live-gc404-smoke")
        index = EphemeralIndex(store=store)
        index.add(
            EphemeralIndexRow(
                id=fake_pod_id,
                warm_attach_key="wak-live-smoke",
                kinoforge_key="k-livesmoke01",
                endpoints={"8188": f"https://{fake_pod_id}-8188.proxy.runpod.net"},
                provider="runpod",
                created_at_local=datetime.now().isoformat(),
            )
        )

        provider = RunPodProvider(creds=_EnvCreds())

        def get_provider(name: str) -> object:
            return (lambda: provider) if name == "runpod" else None

        # Dry-run — verdict only, no removal
        dry = sweep(
            store=store,
            ledger=ledger,
            registry_get_provider=get_provider,  # type: ignore[arg-type]
            thresholds=_THRESHOLDS,
            clock=RealClock(),
            policy=None,
            stall_history=None,
        )
        dry_entry, dry_verdict = dry.snapshot[fake_pod_id]
        assert dry_verdict == Verdict.GC_404
        assert dry_entry["probe_state"] == "not_found"
        assert any(r.id == fake_pod_id for r in index.rows())

        # Apply — GC_404 → row removed via EphemeralIndex.remove
        applied = sweep(
            store=store,
            ledger=ledger,
            registry_get_provider=get_provider,  # type: ignore[arg-type]
            thresholds=_THRESHOLDS,
            clock=RealClock(),
            policy=DEFAULT_APPLY_POLICY,
            stall_history=None,
        )
        _entry, apply_verdict = applied.snapshot[fake_pod_id]
        assert apply_verdict == Verdict.GC_404
        assert any(
            a.action == "gc_404_removed" and a.instance_id == fake_pod_id
            for a in applied.actions
        )
        assert not any(r.id == fake_pod_id for r in index.rows())
