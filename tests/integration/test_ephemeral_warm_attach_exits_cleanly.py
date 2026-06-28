"""Offline repro for the --ephemeral warm-attach teardown hang.

The 2026-06-27 live smoke
(``tests/live/test_runpod_ephemeral_warm_reuse_smoke.py``) showed
run #2 subprocess hangs after emitting ``generate completed`` on the
warm-attach path. Run #1 (cold-boot, ``instance is None``) exits
cleanly; run #2 (warm-attach, ``instance is not None``) does not.

This test isolates the warm-attach branch with the fake engine + a
stub local provider, seeds an ``EphemeralIndex`` row for the cfg's
capability_key, and drives ``_cmd_generate`` end-to-end inside
``EphemeralSession(enabled=True)``. The orchestrator's
``deploy_session`` runs verbatim; the bug surfaces in the post-yield
teardown of ``hold_until_first_tick``.

Pre-fix: hangs ≥30s, faulthandler dumps the stack inside
``hold_until_first_tick``'s post-yield poll loop, marked XFAIL.
Post-fix (Task 2): exits in <5s; remove ``@pytest.mark.xfail`` in the
same commit.
"""

from __future__ import annotations

import faulthandler
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from kinoforge.cli._commands import _cmd_generate
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.warm_reuse.ephemeral_index import (
    EphemeralIndex,
    EphemeralIndexRow,
)

# Cfg copied from tests/cli/test_cmd_generate_scan.py with
# heartbeat_interval_s + boot_timeout_s pinned so the C29 closure builds
# and ``hold_until_first_tick``'s claim_ttl is bounded.
_CFG_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
    heartbeat_interval_s: 1
    boot_timeout_s: 60
"""


class _FakeLocalProvider(ComputeProvider):
    """Local-shape provider that surfaces a pre-seeded warm Instance.

    Pre-creates the warm pod at construction so ``_resolve_warm_instance``'s
    ``provider.get_instance`` + ``list_instances`` calls both return it
    without going through ``create_instance``. ``destroy_instance`` is
    a no-op — the warm-attach path under ``no_reuse=False`` should not
    invoke it.
    """

    name: str = "local"

    def __init__(self, instance: Instance) -> None:
        self._instance = instance

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        del reqs
        return []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        raise AssertionError("warm-attach must not create a fresh instance")

    def get_instance(self, instance_id: str) -> Instance:
        if instance_id != self._instance.id:
            raise KeyError(instance_id)
        return self._instance

    def list_instances(self) -> list[Instance]:
        return [self._instance]

    def stop_instance(self, instance_id: str) -> None:
        return None

    def destroy_instance(self, instance_id: str) -> None:
        return None

    def heartbeat(self, instance_id: str) -> None:
        return None

    def endpoints(self, instance: Instance) -> dict[str, str]:
        return dict(instance.endpoints)

    def last_heartbeat(self, instance_id: str) -> float | None:
        del instance_id
        return None


def _seeded_warm_instance(pod_id: str = "warm-pod") -> Instance:
    return Instance(
        id=pod_id,
        provider="local",
        status="ready",
        created_at=0.0,
        tags={"mode": "pod"},
        cost_rate_usd_per_hr=0.0,
        endpoints={"8188": f"http://{pod_id}.invalid"},
    )


def _make_ctx(tmp_path: Path) -> tuple[SessionContext, Any]:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG_YAML)
    cfg = load_config(cfg_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None)
    return ctx, cfg


def _make_args(*, run_id: str) -> Any:
    import argparse

    return argparse.Namespace(
        config="<unused>",
        prompt="x",
        mode="t2v",
        run_id=run_id,
        output_dir=None,
        no_output_dir=True,
        instance_id=None,
        force_attach=False,
        no_reuse=False,
        skip_preflight=True,
        dry_run_swap=False,
        env_file=None,
        loras=None,
        attach_pod=None,
        emit_provision_record=None,
        diagnostic_mode=False,
    )


@pytest.mark.xfail(
    reason=(
        "Reproduces the --ephemeral warm-attach teardown hang. "
        "Flips XPASS after the Task 2 fix in orchestrator.deploy_session; "
        "remove this decorator in Task 2's final commit."
    ),
    run=True,
    strict=False,
)
def test_warm_attach_exits_cleanly_under_ephemeral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: --ephemeral warm-attach subprocess hangs after generate completed.

    Concrete failure surface: ``hold_until_first_tick``'s post-yield poll
    waits for ``heartbeat_thread_tick`` to land on a ledger entry that
    was never recorded — under STRICT_POLICY the in-memory ledger has
    no entry for the warm-attached pod (record fires only on cold-boot
    via ``_record_then_install``), so every ``HeartbeatLoop`` tick's
    ``ledger.touch`` is a silent no-op, and the post-yield poller spins
    until ``claim_ttl`` (≈ boot_timeout_s + 2*heartbeat_interval_s)
    elapses.
    """
    # Watchdog: dump traceback to stderr at 25s then raise TimeoutError at
    # 30s so a hang fails the test with a captured stack instead of
    # blocking pytest forever (pytest-timeout is not on this project's
    # dep list; SIGALRM is a one-file alternative).
    faulthandler.dump_traceback_later(25, file=sys.stderr, exit=False)

    def _watchdog(_sig: int, _frame: object) -> None:
        raise TimeoutError(
            "test hung > 30s — see faulthandler dump above for stuck frame"
        )

    signal.signal(signal.SIGALRM, _watchdog)
    signal.alarm(30)
    try:
        _drive_warm_attach(tmp_path, monkeypatch)
    finally:
        signal.alarm(0)
        faulthandler.cancel_dump_traceback_later()


def _drive_warm_attach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, cfg = _make_ctx(tmp_path)

    instance = _seeded_warm_instance("warm-pod")
    provider = _FakeLocalProvider(instance)

    # Patch every registry surface the orchestrator + scan path consults so
    # the cfg's "local" provider kind resolves to our pre-seeded fake.
    def _fake_get_provider(kind: str) -> Any:
        del kind
        return lambda: provider

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _fake_get_provider)

    # Seed the EphemeralIndex row INSIDE the EphemeralSession so the row is
    # tagged with the kinoforge_key derived from the active session+cfg.
    cap_key12 = cfg.capability_key().derive()[:12]
    with EphemeralSession(enabled=True):
        EphemeralIndex(store=ctx.store()).add(
            EphemeralIndexRow(
                id=instance.id,
                warm_attach_key="wak-irrelevant",
                kinoforge_key=cap_key12,
                endpoints=dict(instance.endpoints),
                provider="local",
                created_at_local=datetime.now()
                .astimezone()
                .isoformat(timespec="seconds"),
            )
        )
        rc = _cmd_generate(_make_args(run_id="run-warm"), ctx)

    assert rc == 0, f"expected clean exit, got rc={rc}"
