"""Behavior: the ``kinoforge deploy`` row lands where the CLI actually reads.

Option (b) of the brief — building a store inside ``deploy()`` from a hardcoded
default path — was rejected because it produces protection that looks wired but
writes the row somewhere ``kinoforge list`` and the sweeper never read. These
tests read the row back through ``SessionContext.ledger()``, which is the exact
accessor ``_cmd_list`` uses, rather than through the store the test built.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any

import pytest

# Import providers/engines/sources so they self-register for the CLI path.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.cli._commands import _cmd_deploy
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.providers.local import LocalProvider

if TYPE_CHECKING:
    from pathlib import Path

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
  warm_reuse_auto_attach: false
  lifecycle:
    budget: 1.0
"""


def _deploy_args() -> argparse.Namespace:
    """Return the namespace ``kinoforge deploy`` produces with no flags.

    Returns:
        A namespace carrying every attribute ``_cmd_deploy`` reads.
    """
    return argparse.Namespace(
        dry_run=False,
        diagnostic_mode=False,
        stall_window_override=None,
        restart_loop_window_override=None,
    )


def _ctx(state_dir: Path) -> SessionContext:
    """Build the session context ``kinoforge deploy`` runs with.

    Args:
        state_dir: Operator state root (the CLI's ``--state-dir``).

    Returns:
        A fresh SessionContext over *state_dir*.
    """
    return SessionContext(state_dir=state_dir, cfg=load_config(_CFG_YAML), sidecar=None)


def test_cmd_deploy_row_is_visible_through_the_ledger_kinoforge_list_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-create, a SECOND SessionContext over the same state dir sees the row.

    A fresh context is used deliberately: it resolves the store the same way
    ``kinoforge list`` does in a separate process, so nothing about this
    assertion can be satisfied by an in-memory object the deploy invocation
    happens to be holding.

    Bug caught: option (b) — writing the provisional row into a store built from
    a default path rather than ``ctx.store()``. Every orchestrator-level test
    stays green while the operator's ``kinoforge list``, the sweeper and the
    reconciler all see nothing.
    """
    seen: list[list[dict[str, Any]]] = []
    real_create = LocalProvider.create_instance

    def _peeking_create(self: LocalProvider, spec: InstanceSpec) -> Instance:
        seen.append(_ctx(tmp_path).ledger().entries())
        return real_create(self, spec)

    monkeypatch.setattr(LocalProvider, "create_instance", _peeking_create)

    rc = _cmd_deploy(_deploy_args(), _ctx(tmp_path))

    assert rc == 0
    assert seen and seen[0], (
        "no row was readable through SessionContext.ledger() while "
        "create_instance was running — the row went to a different store"
    )
    row = seen[0][0]
    assert row["tags"]["kf_launch_phase"] == "launching"
    assert row["provider"] == "local"
    assert row["id"] == row["tags"]["kf_run_id"]


def test_cmd_deploy_leaves_exactly_one_real_row(tmp_path: Path) -> None:
    """After a successful deploy the operator sees one row: the real instance.

    Bug caught: recording the real row in BOTH ``deploy()`` and ``_cmd_deploy``
    (``Ledger.record`` appends, so ``kinoforge list`` shows the pod twice and
    ``est_spend`` is double-counted), or in neither (the pod is invisible, which
    is the pre-S5 state this work exists to end).
    """
    ctx = _ctx(tmp_path)

    rc = _cmd_deploy(_deploy_args(), ctx)

    assert rc == 0
    entries = _ctx(tmp_path).ledger().entries()
    assert len(entries) == 1, f"expected one row, got {[e['id'] for e in entries]}"
    entry = entries[0]
    assert entry["id"].startswith("local-")
    assert "kf_launch_phase" not in entry["tags"]
    assert entry["tags"]["kinoforge_engine"] == "fake"


def test_cmd_deploy_still_applies_the_stall_window_override(tmp_path: Path) -> None:
    """The C26 override still lands on the row it is meant to annotate.

    ``_cmd_deploy`` touches the row after ``deploy()`` returns, so moving the
    record into the orchestrator must leave a row for the touch to find.

    Bug caught: moving the real-row write without keeping it ahead of the
    override — ``Ledger.touch`` is a strict update, so the override would
    silently no-op and a known-slow-boot workload would be stall-reaped on the
    default window.
    """
    args = _deploy_args()
    args.stall_window_override = 1800.0

    rc = _cmd_deploy(args, _ctx(tmp_path))

    assert rc == 0
    entries = _ctx(tmp_path).ledger().entries()
    assert len(entries) == 1
    assert entries[0]["stall_window_s"] == 1800.0
