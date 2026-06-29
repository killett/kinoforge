"""Live GC_404 smoke: real RunPod GraphQL probe returns 404 → sweeper reaps row.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.10

Cost: $0 (no pod provisioned — fake pod_id triggers 404).

Captures evidence into the sibling `*.json` files in this directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from kinoforge.core.clock import RealClock
from kinoforge.core.interfaces import CredentialProvider
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
from kinoforge.core.reaper_actor import sweep
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.stores.local import LocalArtifactStore


class _EnvCreds(CredentialProvider):
    def get(self, key: str) -> str | None:
        return os.environ.get(key)


def main() -> None:
    api_key_set = bool(os.environ.get("RUNPOD_API_KEY"))
    fake_pod_id = "fake-nonexistent-sweeper-smoke-zz"
    print(
        f"[live-gc404] start  ts={datetime.now().isoformat()}  RUNPOD_API_KEY set={api_key_set}"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        store = LocalArtifactStore(root=root)
        ledger = Ledger(store=store, run_id="live-gc404-smoke")
        index = EphemeralIndex(store=store)
        # Inject a fake row pointing at a non-existent pod.
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
        print(f"[live-gc404] injected fake row for pod_id={fake_pod_id}")

        provider = RunPodProvider(creds=_EnvCreds())

        def get_provider(name: str) -> object:
            return (lambda: provider) if name == "runpod" else None

        thresholds = {
            "max_lifetime_s": 5 * 3600.0,
            "stall_window_s": 0.0,  # disabled — only GC_404 path under test
            "heartbeat_interval_s": 30.0,
            "stall_gpu_threshold": 5.0,
            "stall_cpu_threshold": 10.0,
            "idle_timeout_s": 600.0,
            "grace_after_session_s": 60.0,
            "restart_loop_window_s": 600.0,
            "restart_loop_uptime_threshold_s": 60.0,
        }

        t0 = time.time()
        # Pre-sweep state
        pre_rows = [r.id for r in index.rows()]
        print(f"[live-gc404] pre-sweep index rows: {pre_rows}")

        # Dry-run sweep first (policy=None) — should still classify GC_404
        # but NOT remove the row.
        dry = sweep(
            store=store,
            ledger=ledger,
            registry_get_provider=get_provider,  # type: ignore[arg-type]
            thresholds=thresholds,
            clock=RealClock(),
            policy=None,
            stall_history=None,
        )
        dry_entry, dry_verdict = dry.snapshot[fake_pod_id]
        print(f"[live-gc404] dry-run verdict for {fake_pod_id}: {dry_verdict.value}")
        print(f"[live-gc404] dry-run probe_state: {dry_entry.get('probe_state')}")
        rows_after_dry = [r.id for r in index.rows()]
        print(
            f"[live-gc404] post-dry rows (should still contain fake): {rows_after_dry}"
        )

        # Apply-mode sweep — should call EphemeralIndex.remove(fake_pod_id)
        apply = sweep(
            store=store,
            ledger=ledger,
            registry_get_provider=get_provider,  # type: ignore[arg-type]
            thresholds=thresholds,
            clock=RealClock(),
            policy=DEFAULT_APPLY_POLICY,
            stall_history=None,
        )
        apply_entry, apply_verdict = apply.snapshot[fake_pod_id]
        print(f"[live-gc404] apply verdict for {fake_pod_id}: {apply_verdict.value}")
        rows_after_apply = [r.id for r in index.rows()]
        print(f"[live-gc404] post-apply rows (fake should be gone): {rows_after_apply}")
        actions = [
            {
                "id": a.instance_id,
                "snapshot_verdict": a.snapshot_verdict.value,
                "applied_verdict": a.applied_verdict.value,
                "action": a.action,
                "reason": a.reason,
            }
            for a in apply.actions
        ]
        print(f"[live-gc404] apply.actions: {actions}")

        elapsed_s = time.time() - t0
        evidence = {
            "ts_local": datetime.now().isoformat(),
            "fake_pod_id": fake_pod_id,
            "dry_verdict": dry_verdict.value,
            "dry_probe_state": dry_entry.get("probe_state"),
            "apply_verdict": apply_verdict.value,
            "apply_actions": actions,
            "rows_pre_sweep": pre_rows,
            "rows_post_dry": rows_after_dry,
            "rows_post_apply": rows_after_apply,
            "elapsed_s": elapsed_s,
            "pass": (
                dry_verdict == Verdict.GC_404
                and apply_verdict == Verdict.GC_404
                and fake_pod_id in rows_after_dry
                and fake_pod_id not in rows_after_apply
                and any(a["action"] == "gc_404_removed" for a in actions)
            ),
        }

        evidence_path = Path(__file__).resolve().parent / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, indent=2))
        print(f"[live-gc404] evidence: {evidence_path}")
        print(f"[live-gc404] PASS={evidence['pass']}  elapsed={elapsed_s:.2f}s")

        if not evidence["pass"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
