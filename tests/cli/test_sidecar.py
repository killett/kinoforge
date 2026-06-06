"""Tests for kinoforge.cli.sidecar — pure module, no I/O outside tmp_path."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from kinoforge.cli.sidecar import (
    LEDGER_NAME,
    LEDGER_RUN_ID,
    SIDECAR_NAME,
    SidecarRecord,
    _local_ledger_nonempty,
    read_sidecar,
    verify_or_write_sidecar,
    write_sidecar,
)
from kinoforge.core.config import Config, StoreConfig
from kinoforge.core.errors import SidecarMigrationBlocked, SidecarMismatch


def _local_cfg() -> Config:
    return Config.model_construct(store=StoreConfig())


def _s3_cfg(bucket: str = "kf-prod", prefix: str = "") -> Config:
    return Config.model_construct(
        store=StoreConfig(kind="s3", bucket=bucket, prefix=prefix)
    )


def _gcs_cfg(bucket: str = "kf-prod") -> Config:
    return Config.model_construct(store=StoreConfig(kind="gcs", bucket=bucket))


# ---------------------------------------------------------------------------
# Pure record / read / write
# ---------------------------------------------------------------------------


def test_read_sidecar_missing_returns_none(tmp_path: Path) -> None:
    assert read_sidecar(tmp_path) is None


def test_read_sidecar_corrupt_raises(tmp_path: Path) -> None:
    (tmp_path / SIDECAR_NAME).write_text("{not valid json")
    with pytest.raises(ValidationError):
        read_sidecar(tmp_path)


def test_read_sidecar_extra_field_rejected(tmp_path: Path) -> None:
    """extra='forbid' catches forward-compat skew at read time.

    Bug-catch: a sidecar produced by a newer kinoforge that grew a new
    StoreConfig identity field would be silently parsed (dropping the
    new field) under extra='ignore' — this test fails that scenario.
    """
    payload = {
        "kind": "s3",
        "bucket": "b",
        "prefix": "",
        "root": None,
        "future_key": "x",
    }
    (tmp_path / SIDECAR_NAME).write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        read_sidecar(tmp_path)


def test_write_sidecar_creates_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nest"
    write_sidecar(nested, _s3_cfg())
    assert (nested / SIDECAR_NAME).exists()


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    write_sidecar(tmp_path, _s3_cfg(bucket="kf-prod", prefix="runs"))
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "s3"
    assert rec.bucket == "kf-prod"
    assert rec.prefix == "runs"


def test_record_from_cfg_local() -> None:
    rec = SidecarRecord.from_cfg(_local_cfg())
    assert rec.kind == "local"
    assert rec.bucket is None
    assert rec.prefix == ""


def test_record_from_cfg_gcs() -> None:
    rec = SidecarRecord.from_cfg(_gcs_cfg("kf-staging"))
    assert rec.kind == "gcs"
    assert rec.bucket == "kf-staging"


def test_record_differs_on_bucket() -> None:
    a = SidecarRecord.from_cfg(_s3_cfg(bucket="b1"))
    b = SidecarRecord.from_cfg(_s3_cfg(bucket="b2"))
    assert a.differs_from(b)


def test_record_differs_on_prefix() -> None:
    a = SidecarRecord.from_cfg(_s3_cfg(prefix="run-1"))
    b = SidecarRecord.from_cfg(_s3_cfg(prefix="run-2"))
    assert a.differs_from(b)


def test_record_same_does_not_differ() -> None:
    a = SidecarRecord.from_cfg(_s3_cfg())
    b = SidecarRecord.from_cfg(_s3_cfg())
    assert not a.differs_from(b)


# ---------------------------------------------------------------------------
# Field-mirror lockdown (bug-catch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["kind", "bucket", "prefix", "root"])
def test_sidecar_mirrors_storeconfig_identity_fields(field_name: str) -> None:
    """Bug-catch: future StoreConfig identity field added but not mirrored.

    Precedent: Phase 16 post-merge fix 484e368 (pydantic strip silently
    dropped Layer E/F config fields). This test asserts SidecarRecord
    covers every identity field this layer's parametrize list names.
    """
    sc_fields = set(StoreConfig.model_fields.keys())
    sr_fields = set(SidecarRecord.model_fields.keys())
    assert field_name in sc_fields, f"StoreConfig is missing {field_name!r}"
    assert field_name in sr_fields, (
        f"StoreConfig.{field_name} is not mirrored in SidecarRecord — "
        f"add the field or rebuild this lockdown to exclude it"
    )


# ---------------------------------------------------------------------------
# _local_ledger_nonempty
# ---------------------------------------------------------------------------


def test_local_ledger_nonempty_missing_file_returns_false(tmp_path: Path) -> None:
    assert _local_ledger_nonempty(tmp_path) is False


def test_local_ledger_nonempty_empty_entries_returns_false(tmp_path: Path) -> None:
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": []}))
    assert _local_ledger_nonempty(tmp_path) is False


def test_local_ledger_nonempty_with_entry_returns_true(tmp_path: Path) -> None:
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))
    assert _local_ledger_nonempty(tmp_path) is True


def test_local_ledger_nonempty_corrupt_returns_false(tmp_path: Path) -> None:
    """Corrupt local ledger is treated as empty (safer to write sidecar)."""
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert _local_ledger_nonempty(tmp_path) is False


def test_local_ledger_nonempty_non_list_entries_returns_false() -> None:
    """Corrupt ledger with non-list `entries` (string, dict, int) → False.

    Bug-catch: a `bool(entries)` check without an isinstance guard
    treats truthy non-list values as 'has entries' and falsely blocks
    migration to cloud stores. The ledger writer only ever produces
    list[dict] under entries; any other shape is corrupt and should be
    treated as empty.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path as _P

        state_dir = _P(tmp)
        p = state_dir / LEDGER_RUN_ID / LEDGER_NAME
        p.parent.mkdir(parents=True)
        for shape in (
            '{"entries": "garbage"}',
            '{"entries": {"a": 1}}',
            '{"entries": 42}',
        ):
            p.write_text(shape)
            assert _local_ledger_nonempty(state_dir) is False, (
                f"non-list entries shape {shape!r} should be treated as empty"
            )


# ---------------------------------------------------------------------------
# verify_or_write_sidecar
# ---------------------------------------------------------------------------


def test_verify_no_sidecar_local_cfg_writes(tmp_path: Path) -> None:
    verify_or_write_sidecar(tmp_path, _local_cfg())
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "local"


def test_verify_no_sidecar_cloud_cfg_empty_state_writes(tmp_path: Path) -> None:
    verify_or_write_sidecar(tmp_path, _s3_cfg())
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "s3"


def test_verify_no_sidecar_cloud_cfg_nonempty_local_ledger_blocked(
    tmp_path: Path,
) -> None:
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))

    with pytest.raises(SidecarMigrationBlocked) as exc:
        verify_or_write_sidecar(tmp_path, _s3_cfg())

    assert "refusing to switch" in str(exc.value)
    assert "destroy" in str(exc.value)
    assert read_sidecar(tmp_path) is None  # no sidecar written on block


def test_verify_no_sidecar_local_cfg_nonempty_local_ledger_writes(
    tmp_path: Path,
) -> None:
    """Same-kind cfg + non-empty local ledger → no block."""
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))

    verify_or_write_sidecar(tmp_path, _local_cfg())
    assert read_sidecar(tmp_path) is not None


def test_verify_matching_sidecar_is_noop(tmp_path: Path) -> None:
    cfg = _s3_cfg(bucket="kf-prod")
    write_sidecar(tmp_path, cfg)
    mtime_before = (tmp_path / SIDECAR_NAME).stat().st_mtime_ns

    verify_or_write_sidecar(tmp_path, cfg)
    mtime_after = (tmp_path / SIDECAR_NAME).stat().st_mtime_ns

    assert mtime_before == mtime_after  # no rewrite on match


def test_verify_mismatch_bucket_raises(tmp_path: Path) -> None:
    write_sidecar(tmp_path, _s3_cfg(bucket="kf-prod"))
    with pytest.raises(SidecarMismatch) as exc:
        verify_or_write_sidecar(tmp_path, _s3_cfg(bucket="kf-staging"))
    assert "differs from sidecar" in str(exc.value)


def test_verify_mismatch_prefix_raises(tmp_path: Path) -> None:
    write_sidecar(tmp_path, _s3_cfg(prefix="run-a"))
    with pytest.raises(SidecarMismatch):
        verify_or_write_sidecar(tmp_path, _s3_cfg(prefix="run-b"))


def test_verify_mismatch_kind_raises(tmp_path: Path) -> None:
    write_sidecar(tmp_path, _s3_cfg())
    with pytest.raises(SidecarMismatch):
        verify_or_write_sidecar(tmp_path, _gcs_cfg())
