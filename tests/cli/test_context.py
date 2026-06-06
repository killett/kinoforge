"""Tests for kinoforge.cli.context — SessionContext factory + lazy build."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kinoforge.cli.context import SessionContext, _build_store_from_sidecar
from kinoforge.cli.sidecar import (
    SIDECAR_NAME,
    SidecarRecord,
    write_sidecar,
)
from kinoforge.core.config import Config, StoreConfig
from kinoforge.core.errors import (
    SidecarMismatch,
    UnknownAdapter,
)
from kinoforge.stores.local import LocalArtifactStore


def _local_cfg() -> Config:
    return Config.model_construct(store=StoreConfig(kind="local"))


def _write_local_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "kf.yaml"
    p.write_text(
        "engine:\n  kind: fake\n  precision: fp16\n"
        "models:\n  - kind: base\n    ref: fake://m\n    target: checkpoints\n"
    )
    return p


# ---------------------------------------------------------------------------
# from_args
# ---------------------------------------------------------------------------


def test_from_args_no_cfg_no_sidecar(tmp_path: Path) -> None:
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=None)
    assert ctx.cfg is None
    assert ctx.sidecar is None


def test_from_args_no_cfg_with_existing_sidecar(tmp_path: Path) -> None:
    write_sidecar(tmp_path, _local_cfg())
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=None)
    assert ctx.cfg is None
    assert ctx.sidecar is not None
    assert ctx.sidecar.kind == "local"


def test_from_args_with_cfg_writes_sidecar(tmp_path: Path) -> None:
    cfg_path = _write_local_cfg(tmp_path)
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=cfg_path)
    assert ctx.cfg is not None
    assert ctx.sidecar is not None
    assert (tmp_path / SIDECAR_NAME).exists()


def test_from_args_propagates_mismatch(tmp_path: Path) -> None:
    write_sidecar(
        tmp_path,
        Config.model_construct(store=StoreConfig(kind="s3", bucket="other")),
    )
    cfg_path = _write_local_cfg(tmp_path)
    with pytest.raises(SidecarMismatch):
        SessionContext.from_args(state_dir=tmp_path, cfg_path=cfg_path)


# ---------------------------------------------------------------------------
# Lazy store / ledger
# ---------------------------------------------------------------------------


def test_store_is_lazy(tmp_path: Path) -> None:
    """No store construction until ctx.store() is called."""
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    assert ctx._store is None


def test_store_identity_cached(tmp_path: Path) -> None:
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    a = ctx.store()
    b = ctx.store()
    assert a is b


def test_store_falls_back_to_local_when_no_cfg_no_sidecar(tmp_path: Path) -> None:
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    assert isinstance(ctx.store(), LocalArtifactStore)


def test_store_uses_sidecar_when_no_cfg(tmp_path: Path) -> None:
    sidecar = SidecarRecord(kind="local", root=str(tmp_path / "alt"))
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=sidecar)
    s = ctx.store()
    assert isinstance(s, LocalArtifactStore)
    # uses sidecar.root, not state_dir
    assert str(s.root) == str(tmp_path / "alt")


def test_ledger_identity_cached(tmp_path: Path) -> None:
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    a = ctx.ledger()
    b = ctx.ledger()
    assert a is b


def test_ledger_uses_lifecycle_run_id(tmp_path: Path) -> None:
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    ledger = ctx.ledger()
    assert ledger._run_id == "_lifecycle"


# ---------------------------------------------------------------------------
# ledger_safe
# ---------------------------------------------------------------------------


def test_ledger_safe_returns_ledger_on_success(tmp_path: Path) -> None:
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    ledger, warn = ctx.ledger_safe()
    assert ledger is not None
    assert warn is None


def test_ledger_safe_returns_warning_on_store_failure(tmp_path: Path) -> None:
    """ledger_safe MUST catch store construction errors.

    Bug-catch: a future regression that drops the try/except in
    ledger_safe would let store-construction errors (expired creds,
    bucket missing) crash main()'s always-on overview before any
    subcommand runs — including `kinoforge --help`.
    """
    bad_sidecar = SidecarRecord(kind="s3", bucket="nope", prefix="")
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=bad_sidecar)

    with patch(
        "kinoforge.cli.context._build_store_from_sidecar",
        side_effect=RuntimeError("auth expired"),
    ):
        ledger, warn = ctx.ledger_safe()

    assert ledger is None
    assert warn is not None
    assert "RuntimeError" in warn
    assert "auth expired" in warn


# ---------------------------------------------------------------------------
# _build_store_from_sidecar
# ---------------------------------------------------------------------------


def test_build_from_sidecar_local_no_root_uses_state_dir(tmp_path: Path) -> None:
    rec = SidecarRecord(kind="local", root=None)
    store = _build_store_from_sidecar(rec, tmp_path)
    assert isinstance(store, LocalArtifactStore)
    assert str(store.root) == str(tmp_path)


def test_build_from_sidecar_unknown_kind_raises(tmp_path: Path) -> None:
    """Forward-compat: a sidecar from a newer kinoforge with kind='azure'
    fails cleanly on an older binary."""
    rec = SidecarRecord.model_construct(kind="azure", bucket="x")
    with pytest.raises(UnknownAdapter):
        _build_store_from_sidecar(rec, tmp_path)


def test_build_from_sidecar_s3_missing_bucket_raises(tmp_path: Path) -> None:
    """ValueError fires at runtime when a corrupt s3 sidecar lacks bucket.

    Bug-catch: the prior `assert sc.bucket is not None` would be stripped
    under `python -O` and proceed to S3ArtifactStore(bucket=None) — a
    silent NoneType crash one frame deeper. The runtime `raise ValueError`
    must fire on every interpreter mode.
    """
    rec = SidecarRecord.model_construct(kind="s3", bucket=None, prefix="")
    with pytest.raises(ValueError, match="bucket"):
        _build_store_from_sidecar(rec, tmp_path)


def test_build_from_sidecar_gcs_missing_bucket_raises(tmp_path: Path) -> None:
    """Same guard for gcs sidecars."""
    rec = SidecarRecord.model_construct(kind="gcs", bucket=None, prefix="")
    with pytest.raises(ValueError, match="bucket"):
        _build_store_from_sidecar(rec, tmp_path)
