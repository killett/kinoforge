"""Multi-node coordination integration: Layer T's headline win.

Proves that two concurrent CLI invocations against the same S3-backed
ledger serialise via Layer H's acquire_lock and BOTH entries land — no
lost update.

Bug-catch: a future regression that drops the acquire_lock wrapper
around Ledger.record would let one writer overwrite the other's entry
when both read the empty ledger before either writes. This test would
then return only one ID in the final ledger and fail.
"""

from __future__ import annotations

import threading
from pathlib import Path

from kinoforge.cli.context import SessionContext
from kinoforge.core.interfaces import Instance
from tests.stores.conftest import FakeS3Client


def _make_ctx(state_dir: Path, shared_client: FakeS3Client) -> SessionContext:
    """Construct a SessionContext pre-seeded with an S3 store backed by the shared client."""
    from kinoforge.stores.s3 import S3ArtifactStore

    store = S3ArtifactStore(bucket="kf-prod", prefix="", client=shared_client)
    ctx = SessionContext(state_dir=state_dir, cfg=None, sidecar=None)
    # Pre-seed the store cache so .ledger() picks up our injected store
    # rather than the LocalArtifactStore fallback.
    ctx._store = store
    return ctx


def test_two_machines_record_to_shared_s3_ledger_no_lost_update(
    tmp_path: Path,
) -> None:
    """Two threads recording concurrently both land their entries."""
    shared = FakeS3Client()
    state_a = tmp_path / "host-a"
    state_b = tmp_path / "host-b"
    state_a.mkdir()
    state_b.mkdir()

    inst_a = Instance(
        id="i-host-a",
        provider="local",
        status="ready",
        tags={},
        created_at=1.0,
        cost_rate_usd_per_hr=0.0,
    )
    inst_b = Instance(
        id="i-host-b",
        provider="local",
        status="ready",
        tags={},
        created_at=2.0,
        cost_rate_usd_per_hr=0.0,
    )

    ctx_a = _make_ctx(state_a, shared)
    ctx_b = _make_ctx(state_b, shared)

    # Barrier ensures both threads race for the lock at the same instant.
    barrier = threading.Barrier(2)

    def _record(ctx: SessionContext, inst: Instance) -> None:
        barrier.wait()
        ctx.ledger().record(inst)

    t_a = threading.Thread(target=_record, args=(ctx_a, inst_a))
    t_b = threading.Thread(target=_record, args=(ctx_b, inst_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)
    assert not t_a.is_alive() and not t_b.is_alive(), (
        "thread timed out — likely deadlock"
    )

    # Either ctx can read — both are backed by the same shared FakeS3Client.
    final = ctx_a.ledger().entries()
    final_ids = sorted(e["id"] for e in final)
    assert final_ids == ["i-host-a", "i-host-b"], (
        f"expected both ids in final ledger, got {final_ids!r} — "
        f"likely Ledger.record dropped the acquire_lock wrapper"
    )
