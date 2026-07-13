"""_ephemeral_index_add: session-gated, provider-agnostic index row writer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import Instance
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
from kinoforge.stores.local import LocalArtifactStore

if TYPE_CHECKING:
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.config import Config


def _mk_instance() -> Instance:
    return Instance(
        id="eph-deadbeef",
        provider="modal",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://x--kinoforge-eph-deadbeef-y.modal.run"},
    )


def _mk_ctx_cfg(tmp_path: Path) -> tuple[SessionContext, Config]:
    """Real local store + the cheap modal cfg."""
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml"))
    store = LocalArtifactStore(tmp_path)
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None, _store=store)
    return ctx, cfg


def test_helper_noops_without_session(tmp_path: Path) -> None:
    """Bug caught: an ungated helper would leak index rows into normal runs,
    violating the ephemeral-only discovery contract (AST invariant)."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    _ephemeral_index_add(ctx, cfg, _mk_instance())
    assert EphemeralIndex(store=ctx.store()).rows() == []


def test_helper_indexes_modal_instance_under_session(tmp_path: Path) -> None:
    """Bug caught: upscale/interpolate ephemeral pods invisible to the next
    CLI process (the pre-lift state: add() was inlined in _cmd_generate only)."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    with EphemeralSession(enabled=True):
        _ephemeral_index_add(ctx, cfg, _mk_instance())
    rows = EphemeralIndex(store=ctx.store()).rows()
    assert len(rows) == 1
    assert rows[0].provider == "modal"
    assert rows[0].endpoints["8000"].endswith(".modal.run")
    assert rows[0].id == "eph-deadbeef"


def test_helper_noops_on_none_instance(tmp_path: Path) -> None:
    """Bug caught: hosted-path (no compute instance) upscale would crash on
    instance.endpoints."""
    from kinoforge.cli._commands import _ephemeral_index_add

    ctx, cfg = _mk_ctx_cfg(tmp_path)
    with EphemeralSession(enabled=True):
        _ephemeral_index_add(ctx, cfg, None)
    assert EphemeralIndex(store=ctx.store()).rows() == []
