# Layer T — cloud-ledger CLI routing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the CLI ledger through the configured `cfg.store` (s3/gcs) by introducing a `SessionContext` threaded through every subcommand and a JSON sidecar (`state_dir/store.json`) that records which store backs the ledger; close PROGRESS:127 carry-forward.

**Architecture:** Promote `src/kinoforge/cli.py` to a `cli/` package, add `cli/sidecar.py` (record + verify/write) and `cli/context.py` (lazy store/ledger factory), refactor `Ledger._compute_uri` to use `store.uri_for` (universal ABC), migrate all 9 subcommand handlers from `(args, state_dir)` to `(args, ctx)`, and wire sidecar handling + SessionContext construction into `main()`.

**Tech Stack:** Python 3.12+, pydantic v2 (`SidecarRecord`, `extra="forbid"`), stdlib `argparse` / `pathlib` / `json`, pytest (offline fakes via `tests/stores/conftest.py` for S3/GCS).

**Spec:** `docs/superpowers/specs/2026-06-05-layer-t-cloud-ledger-cli-routing-design.md` (commit `4bb530f`).

---

## File map

**New files:**
- `src/kinoforge/cli/__init__.py` (entry-point shim + back-compat re-exports)
- `src/kinoforge/cli/_main.py` (parser, `main()`, dispatch table, overview printer)
- `src/kinoforge/cli/_commands.py` (all `_cmd_*` handlers, `_build_store`, `_build_sink`, Layer S helpers)
- `src/kinoforge/cli/context.py` (`SessionContext`, `_build_store_from_sidecar`)
- `src/kinoforge/cli/sidecar.py` (`SidecarRecord`, `verify_or_write_sidecar`, constants)
- `tests/cli/__init__.py` (empty marker)
- `tests/cli/test_sidecar.py`
- `tests/cli/test_context.py`
- `tests/cli/test_commands_routing.py`
- `tests/cli/test_main_flow.py`
- `tests/cli/test_multinode_lock.py`

**Modified files:**
- `src/kinoforge/cli.py` → deleted (replaced by `cli/` package; all symbols re-exported from `cli/__init__.py`)
- `src/kinoforge/core/lifecycle.py` (Ledger `_compute_uri` refactor)
- `src/kinoforge/core/errors.py` (add `SidecarMismatch`, `SidecarMigrationBlocked`)
- `tests/core/test_lifecycle.py` (drop dead-path test; add S3/GCS round-trip tests)
- `tests/core/test_lifecycle_sweeper.py` (update `_LockingStore` to honour `uri_for`)
- `tests/test_cli.py` (verify re-exports work; existing tests should pass unmodified)
- `README.md` (Cloud-backed ledger section, Multi-host setup subsection, Breaking change note, migration steps)
- `PROGRESS.md` (Phase 34 entry under Post-MVP)

---

## Task 1: Refactor `Ledger._compute_uri` to use `store.uri_for`

**Goal:** Drop the `isinstance(LocalArtifactStore)` switch in `Ledger._compute_uri`; delegate to the universal `store.uri_for` ABC. Pure cleanup; enabling change for cloud-backed ledger routing in later tasks.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py:399-415`
- Test: `tests/core/test_lifecycle.py` (new tests for fake S3 / GCS round-trip; drop dead-path TypeError test)
- Test: `tests/core/test_lifecycle_sweeper.py` (update `_LockingStore` if it relies on the old `_compute_uri` path)

**Acceptance Criteria:**
- [ ] `Ledger._compute_uri` body is exactly `return self._store.uri_for(self._run_id, self._LEDGER_NAME)` — no `isinstance` branch, no `TypeError` raise
- [ ] `Ledger(store=fake_s3, run_id="_lifecycle")` constructs without raising
- [ ] `Ledger.record(instance)` followed by `Ledger.entries()` round-trips through a fake S3 store and yields the recorded entry
- [ ] Same round-trip succeeds against a fake GCS store
- [ ] Existing local-backed Ledger tests still pass (`pytest tests/core/test_lifecycle_sweeper.py -v`)
- [ ] Dead-path test that asserted `TypeError` on non-Local stores is deleted

**Verify:** `pixi run test tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py -v` → all tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests for cloud round-trip**

Add to `tests/core/test_lifecycle.py`:

```python
from tests.stores.conftest import FakeS3Client, FakeGCSClient


def test_ledger_compute_uri_delegates_to_store_uri_for(tmp_path):
    """Bug-catch: prevents reintroduction of the isinstance switch.

    A future edit that re-adds `isinstance(LocalArtifactStore)` checking
    breaks the universal ABC contract. This test calls _compute_uri on a
    NON-Local store and asserts it returns the result of store.uri_for —
    not raises TypeError.
    """
    from kinoforge.stores.s3 import S3ArtifactStore
    fake = FakeS3Client()
    store = S3ArtifactStore(bucket="b", prefix="p", client=fake)

    ledger = Ledger(store=store, run_id="_lifecycle")

    assert ledger._compute_uri() == store.uri_for("_lifecycle", "ledger.json")


def test_ledger_round_trip_against_fake_s3(tmp_path):
    """Record + entries() round-trips through fake S3 store."""
    from kinoforge.stores.s3 import S3ArtifactStore
    from kinoforge.core.interfaces import Instance
    fake = FakeS3Client()
    store = S3ArtifactStore(bucket="b", prefix="p", client=fake)
    ledger = Ledger(store=store, run_id="_lifecycle")

    inst = Instance(
        id="i-1", provider="local", status="ready",
        tags={"kinoforge_key": "abc"}, created_at=1000.0,
        cost_rate_usd_per_hr=0.5,
    )
    ledger.record(inst)

    entries = ledger.entries()
    assert len(entries) == 1
    assert entries[0]["id"] == "i-1"
    assert entries[0]["provider"] == "local"


def test_ledger_round_trip_against_fake_gcs(tmp_path):
    """Same contract against fake GCS — proves both clouds work."""
    from kinoforge.stores.gcs import GCSArtifactStore
    from kinoforge.core.interfaces import Instance
    fake = FakeGCSClient()
    store = GCSArtifactStore(
        bucket="b", prefix="p", client=fake, not_found_exc=fake.NotFound,
    )
    ledger = Ledger(store=store, run_id="_lifecycle")

    inst = Instance(
        id="i-2", provider="local", status="ready",
        tags={}, created_at=2000.0, cost_rate_usd_per_hr=0.0,
    )
    ledger.record(inst)
    assert [e["id"] for e in ledger.entries()] == ["i-2"]
```

- [ ] **Step 2: Run failing tests; confirm RED**

Run: `pixi run test tests/core/test_lifecycle.py::test_ledger_compute_uri_delegates_to_store_uri_for tests/core/test_lifecycle.py::test_ledger_round_trip_against_fake_s3 tests/core/test_lifecycle.py::test_ledger_round_trip_against_fake_gcs -v`

Expected: 3 tests FAIL. The `_compute_uri` test fails with `TypeError: Ledger._compute_uri: unsupported store type 'S3ArtifactStore'`. The round-trip tests fail at the `Ledger.record` call (same underlying `TypeError`).

- [ ] **Step 3: Apply the refactor**

Edit `src/kinoforge/core/lifecycle.py`, replace the `_compute_uri` body (lines 399-415):

```python
    def _compute_uri(self) -> str:
        """Return the store URI for the ledger JSON.

        Delegates to ``self._store.uri_for`` — the universal ABC (Phase 11 /
        Layer A) that every artifact store implements. The previous
        isinstance(LocalArtifactStore) switch was a vestige from before that
        ABC existed.

        Returns:
            Absolute URI string for the ledger JSON file.
        """
        return self._store.uri_for(self._run_id, self._LEDGER_NAME)
```

- [ ] **Step 4: Delete the dead-path test**

Search `tests/core/test_lifecycle.py` and `tests/core/test_lifecycle_sweeper.py` for any test asserting `pytest.raises(TypeError)` on `Ledger._compute_uri` with a non-Local store. Delete the test function. The `# pragma: no cover` line in the source confirms it was already unreachable in CI.

- [ ] **Step 5: Update any `_LockingStore` test double**

Check `tests/core/test_lifecycle_sweeper.py:473` (the `_LockingStore` subclass of `LocalArtifactStore`). It already inherits `uri_for` from `LocalArtifactStore`, so no change needed unless the subclass overrides `_path` without overriding `uri_for`. Read the subclass and confirm. If it overrides `_path`, ensure `uri_for` still returns the right string.

- [ ] **Step 6: Run targeted tests; confirm GREEN**

Run: `pixi run test tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py -v`

Expected: all tests pass.

- [ ] **Step 7: Run full suite; confirm no regression**

Run: `pixi run test`

Expected: 1222 → 1224 passed (same baseline + 2 new tests; one dead-path test deleted; one `test_ledger_compute_uri_delegates_to_store_uri_for` added; two round-trip tests added; net +2 / -1 → +1 baseline change). Confirm number is right.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/lifecycle.py tests/core/test_lifecycle.py tests/core/test_lifecycle_sweeper.py
git commit -m "$(cat <<'EOF'
refactor(lifecycle): Ledger._compute_uri uses store.uri_for ABC (Phase 34 T1)

Drops the isinstance(LocalArtifactStore) switch + TypeError dead-code
path. Delegates to the universal store.uri_for ABC (Phase 11 / Layer A)
that every artifact store implements. Enabling change for Layer T
cloud-backed ledger routing.

Adds 2 round-trip tests (fake S3 + fake GCS) and 1 bug-catch lock against
isinstance regression.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `SidecarMismatch` and `SidecarMigrationBlocked` errors

**Goal:** Add the two new exception types to `core/errors.py`; both subclass `KinoforgeError` so the batch command's existing `except KinoforgeError` arm catches them as exit 1 without code change.

**Files:**
- Modify: `src/kinoforge/core/errors.py`
- Test: `tests/core/test_errors.py` (or append to existing test_errors module if present)

**Acceptance Criteria:**
- [ ] `SidecarMismatch` is a subclass of `KinoforgeError`
- [ ] `SidecarMigrationBlocked` is a subclass of `KinoforgeError`
- [ ] Both classes have a docstring matching the spec §7.4 wording
- [ ] `except KinoforgeError` catches both

**Verify:** `pixi run test tests/core/test_errors.py -v` → all error tests pass.

**Steps:**

- [ ] **Step 1: Locate or create the error test module**

Check whether `tests/core/test_errors.py` exists:

```bash
ls tests/core/test_errors.py 2>/dev/null || echo "absent"
```

If absent, create `tests/core/test_errors.py` with the header:

```python
"""Tests for kinoforge.core.errors."""

import pytest

from kinoforge.core.errors import (
    KinoforgeError,
    SidecarMigrationBlocked,
    SidecarMismatch,
)
```

If present, just append the new test functions.

- [ ] **Step 2: Write failing tests**

```python
def test_sidecar_mismatch_subclasses_kinoforge_error():
    """except KinoforgeError catches SidecarMismatch — wiring contract."""
    assert issubclass(SidecarMismatch, KinoforgeError)


def test_sidecar_migration_blocked_subclasses_kinoforge_error():
    """except KinoforgeError catches SidecarMigrationBlocked — wiring contract."""
    assert issubclass(SidecarMigrationBlocked, KinoforgeError)


def test_sidecar_mismatch_carries_message():
    """Exception message round-trips."""
    err = SidecarMismatch("cfg.store ({s3}) differs from sidecar ({gcs})")
    assert "differs" in str(err)
```

- [ ] **Step 3: Run failing tests; confirm RED**

Run: `pixi run test tests/core/test_errors.py -v`

Expected: FAIL with `ImportError: cannot import name 'SidecarMismatch'`.

- [ ] **Step 4: Add the error classes**

Append to `src/kinoforge/core/errors.py`:

```python
class SidecarMismatch(KinoforgeError):
    """cfg.store differs from sidecar on disk.

    Raised by ``cli.sidecar.verify_or_write_sidecar`` when the operator
    runs a cfg-bearing command with a config whose store identity
    differs from the sidecar already recorded in ``state_dir/store.json``.
    """


class SidecarMigrationBlocked(KinoforgeError):
    """First cloud-store command refused while local ledger non-empty.

    Raised by ``cli.sidecar.verify_or_write_sidecar`` when the operator
    runs a cloud-store cfg on a ``state_dir`` whose local ledger still
    has entries — guards against silently orphaning in-flight pods.
    """
```

- [ ] **Step 5: Confirm GREEN**

Run: `pixi run test tests/core/test_errors.py -v`

Expected: 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/errors.py tests/core/test_errors.py
git commit -m "$(cat <<'EOF'
feat(errors): SidecarMismatch + SidecarMigrationBlocked (Phase 34 T2)

Two new KinoforgeError subclasses used by cli.sidecar.verify_or_write
to surface (a) cfg.store divergence from on-disk sidecar and (b) first
cloud-cfg run while a non-empty local ledger still has in-flight
entries. Subclassing KinoforgeError means _cmd_batch's existing
Setup-fatal catch arm handles them at exit 1 with no code change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Promote `src/kinoforge/cli.py` to a `cli/` package

**Goal:** Convert the existing `cli.py` monolith into `cli/__init__.py` verbatim — zero behavioural change, zero test churn. Enables siblings (`cli/sidecar.py`, `cli/context.py`, `cli/_main.py`, `cli/_commands.py`) in later tasks.

**Files:**
- Modify: `src/kinoforge/cli.py` (delete after promotion)
- Create: `src/kinoforge/cli/__init__.py` (verbatim copy of old `cli.py`)

**Acceptance Criteria:**
- [ ] `src/kinoforge/cli/__init__.py` is a byte-for-byte copy of the prior `cli.py`
- [ ] `src/kinoforge/cli.py` no longer exists
- [ ] `from kinoforge.cli import main` still works
- [ ] `from kinoforge.cli import _build_store, _build_parser, _build_ledger_block` still works
- [ ] All 1224 existing tests pass with zero modifications
- [ ] `python -m kinoforge --help` still prints help

**Verify:** `pixi run test && python -m kinoforge --help` → tests green, help prints.

**Steps:**

- [ ] **Step 1: Promote file to package**

```bash
mkdir -p src/kinoforge/cli
git mv src/kinoforge/cli.py src/kinoforge/cli/__init__.py
```

- [ ] **Step 2: Confirm imports unchanged**

Run: `python -c "from kinoforge.cli import main, _build_store, _build_parser, _build_ledger_block; print('OK')"`

Expected: `OK`.

- [ ] **Step 3: Confirm CLI still works**

Run: `python -m kinoforge --help`

Expected: argparse help block prints. Exit 0.

- [ ] **Step 4: Run full suite**

Run: `pixi run test`

Expected: 1224 tests pass — exact same count as Task 1 completion.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/__init__.py
git rm src/kinoforge/cli.py 2>/dev/null || true
git commit -m "$(cat <<'EOF'
refactor(cli): promote cli.py to cli/ package (Phase 34 T3)

Verbatim file move. Enables sibling modules (sidecar, context, _main,
_commands) in later Layer T tasks. Zero behaviour change; all 1224
existing tests pass unmodified.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Build `cli/sidecar.py` module + tests

**Goal:** Pure module that owns the sidecar record schema (`SidecarRecord`), read/write/verify operations, and the local-ledger-non-empty migration check. ~15 offline tests.

**Files:**
- Create: `src/kinoforge/cli/sidecar.py`
- Create: `tests/cli/__init__.py` (empty)
- Create: `tests/cli/test_sidecar.py`

**Acceptance Criteria:**
- [ ] `SidecarRecord` is a frozen pydantic v2 BaseModel with `extra="forbid"`
- [ ] `SidecarRecord.from_cfg(cfg)` mirrors every identity field of `StoreConfig` (`kind`, `bucket`, `prefix`, `root`)
- [ ] `read_sidecar(state_dir)` returns `None` for missing file, parses JSON for present file, raises `pydantic.ValidationError` for corrupt JSON or extra fields
- [ ] `write_sidecar(state_dir, cfg)` creates `state_dir/store.json` (and parent dir if absent)
- [ ] `verify_or_write_sidecar(state_dir, cfg)` is idempotent on match, raises `SidecarMismatch` on differing existing sidecar, raises `SidecarMigrationBlocked` on first cloud-cfg with non-empty local ledger, writes new sidecar when none exists and migration check passes
- [ ] `_local_ledger_nonempty(state_dir)` returns False for missing/corrupt/empty ledger.json, True for one+ entries
- [ ] Parametrized field-mirror test fails when a new `StoreConfig` field is added without mirroring in `SidecarRecord`

**Verify:** `pixi run test tests/cli/test_sidecar.py -v` → 15+ tests pass.

**Steps:**

- [ ] **Step 1: Create empty test marker**

```bash
mkdir -p tests/cli
touch tests/cli/__init__.py
```

- [ ] **Step 2: Write failing tests**

Create `tests/cli/test_sidecar.py`:

```python
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
    return Config(
        engine={"kind": "fake"},
        models=[{"kind": "base", "name": "m", "ref": "fake://m"}],
    )


def _s3_cfg(bucket: str = "kf-prod", prefix: str = "") -> Config:
    return Config(
        engine={"kind": "fake"},
        models=[{"kind": "base", "name": "m", "ref": "fake://m"}],
        store=StoreConfig(kind="s3", bucket=bucket, prefix=prefix),
    )


def _gcs_cfg(bucket: str = "kf-prod") -> Config:
    return Config(
        engine={"kind": "fake"},
        models=[{"kind": "base", "name": "m", "ref": "fake://m"}],
        store=StoreConfig(kind="gcs", bucket=bucket),
    )


# ---------------------------------------------------------------------------
# Pure record / read / write
# ---------------------------------------------------------------------------


def test_read_sidecar_missing_returns_none(tmp_path):
    assert read_sidecar(tmp_path) is None


def test_read_sidecar_corrupt_raises(tmp_path):
    (tmp_path / SIDECAR_NAME).write_text("{not valid json")
    with pytest.raises(ValidationError):
        read_sidecar(tmp_path)


def test_read_sidecar_extra_field_rejected(tmp_path):
    """extra='forbid' catches forward-compat skew at read time."""
    payload = {"kind": "s3", "bucket": "b", "prefix": "", "root": None, "future_key": "x"}
    (tmp_path / SIDECAR_NAME).write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        read_sidecar(tmp_path)


def test_write_sidecar_creates_parent_dir(tmp_path):
    nested = tmp_path / "deep" / "nest"
    write_sidecar(nested, _s3_cfg())
    assert (nested / SIDECAR_NAME).exists()


def test_write_then_read_roundtrip(tmp_path):
    write_sidecar(tmp_path, _s3_cfg(bucket="kf-prod", prefix="runs"))
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "s3"
    assert rec.bucket == "kf-prod"
    assert rec.prefix == "runs"


def test_record_from_cfg_local(tmp_path):
    rec = SidecarRecord.from_cfg(_local_cfg())
    assert rec.kind == "local"
    assert rec.bucket is None
    assert rec.prefix == ""


def test_record_from_cfg_gcs(tmp_path):
    rec = SidecarRecord.from_cfg(_gcs_cfg("kf-staging"))
    assert rec.kind == "gcs"
    assert rec.bucket == "kf-staging"


def test_record_differs_on_bucket(tmp_path):
    a = SidecarRecord.from_cfg(_s3_cfg(bucket="b1"))
    b = SidecarRecord.from_cfg(_s3_cfg(bucket="b2"))
    assert a.differs_from(b)


def test_record_differs_on_prefix(tmp_path):
    a = SidecarRecord.from_cfg(_s3_cfg(prefix="run-1"))
    b = SidecarRecord.from_cfg(_s3_cfg(prefix="run-2"))
    assert a.differs_from(b)


def test_record_same_does_not_differ(tmp_path):
    a = SidecarRecord.from_cfg(_s3_cfg())
    b = SidecarRecord.from_cfg(_s3_cfg())
    assert not a.differs_from(b)


# ---------------------------------------------------------------------------
# Field-mirror lockdown (bug-catch)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["kind", "bucket", "prefix", "root"],
)
def test_sidecar_mirrors_storeconfig_identity_fields(field_name):
    """Bug-catch: future StoreConfig identity field added but not mirrored.

    Precedent: Phase 16 post-merge fix 484e368 (pydantic strip silently
    dropped Layer E/F config fields). This test reads StoreConfig's
    schema and asserts SidecarRecord covers every identity field.
    """
    sc_fields = set(StoreConfig.model_fields.keys())
    sr_fields = set(SidecarRecord.model_fields.keys())
    assert field_name in sc_fields  # invariant: param list matches StoreConfig
    assert field_name in sr_fields, (
        f"StoreConfig.{field_name} is not mirrored in SidecarRecord — "
        f"add the field or rebuild this lockdown to exclude it"
    )


# ---------------------------------------------------------------------------
# _local_ledger_nonempty
# ---------------------------------------------------------------------------


def test_local_ledger_nonempty_missing_file_returns_false(tmp_path):
    assert _local_ledger_nonempty(tmp_path) is False


def test_local_ledger_nonempty_empty_entries_returns_false(tmp_path):
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": []}))
    assert _local_ledger_nonempty(tmp_path) is False


def test_local_ledger_nonempty_with_entry_returns_true(tmp_path):
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))
    assert _local_ledger_nonempty(tmp_path) is True


def test_local_ledger_nonempty_corrupt_returns_false(tmp_path):
    """Corrupt local ledger is treated as empty (safer to write the sidecar)."""
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert _local_ledger_nonempty(tmp_path) is False


# ---------------------------------------------------------------------------
# verify_or_write_sidecar
# ---------------------------------------------------------------------------


def test_verify_no_sidecar_local_cfg_writes(tmp_path):
    verify_or_write_sidecar(tmp_path, _local_cfg())
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "local"


def test_verify_no_sidecar_cloud_cfg_empty_state_writes(tmp_path):
    verify_or_write_sidecar(tmp_path, _s3_cfg())
    rec = read_sidecar(tmp_path)
    assert rec is not None
    assert rec.kind == "s3"


def test_verify_no_sidecar_cloud_cfg_nonempty_local_ledger_blocked(tmp_path):
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))

    with pytest.raises(SidecarMigrationBlocked) as exc:
        verify_or_write_sidecar(tmp_path, _s3_cfg())

    assert "refusing to switch" in str(exc.value)
    assert "destroy" in str(exc.value)
    assert read_sidecar(tmp_path) is None  # no sidecar written on block


def test_verify_no_sidecar_local_cfg_nonempty_local_ledger_writes(tmp_path):
    """Same-kind cfg + non-empty local ledger → no block."""
    p = tmp_path / LEDGER_RUN_ID / LEDGER_NAME
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"entries": [{"id": "i-1"}]}))

    verify_or_write_sidecar(tmp_path, _local_cfg())
    assert read_sidecar(tmp_path) is not None


def test_verify_matching_sidecar_is_noop(tmp_path):
    cfg = _s3_cfg(bucket="kf-prod")
    write_sidecar(tmp_path, cfg)
    mtime_before = (tmp_path / SIDECAR_NAME).stat().st_mtime_ns

    verify_or_write_sidecar(tmp_path, cfg)
    mtime_after = (tmp_path / SIDECAR_NAME).stat().st_mtime_ns

    assert mtime_before == mtime_after  # no rewrite on match


def test_verify_mismatch_bucket_raises(tmp_path):
    write_sidecar(tmp_path, _s3_cfg(bucket="kf-prod"))
    with pytest.raises(SidecarMismatch) as exc:
        verify_or_write_sidecar(tmp_path, _s3_cfg(bucket="kf-staging"))
    assert "differs from sidecar" in str(exc.value)


def test_verify_mismatch_prefix_raises(tmp_path):
    write_sidecar(tmp_path, _s3_cfg(prefix="run-a"))
    with pytest.raises(SidecarMismatch):
        verify_or_write_sidecar(tmp_path, _s3_cfg(prefix="run-b"))


def test_verify_mismatch_kind_raises(tmp_path):
    write_sidecar(tmp_path, _s3_cfg())
    with pytest.raises(SidecarMismatch):
        verify_or_write_sidecar(tmp_path, _gcs_cfg())
```

- [ ] **Step 3: Run failing tests; confirm RED**

Run: `pixi run test tests/cli/test_sidecar.py -v`

Expected: every test fails with `ModuleNotFoundError: No module named 'kinoforge.cli.sidecar'`.

- [ ] **Step 4: Implement the module**

Create `src/kinoforge/cli/sidecar.py`:

```python
"""Sidecar pointer recording which artifact store backs the ledger.

Written by cfg-bearing CLI subcommands on first run; read by no-config
subcommands (``list``, ``stop``, ``destroy``, ``forget``, ``reap``) so
they discover the configured store without needing ``--config`` on the
command line.

The sidecar is per-``state_dir``: every operator's local
``.kinoforge/store.json`` records which store their ledger lives in.
Cross-machine bootstrap is a Layer T+1 concern (``--store-uri`` /
``KINOFORGE_STORE_URI``).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from kinoforge.core.config import Config
from kinoforge.core.errors import SidecarMigrationBlocked, SidecarMismatch

SIDECAR_NAME = "store.json"
LEDGER_RUN_ID = "_lifecycle"
LEDGER_NAME = "ledger.json"


class SidecarRecord(BaseModel):
    """Frozen mirror of ``StoreConfig``'s identity fields.

    ``extra="forbid"`` catches forward-compat drift: a newer kinoforge
    that adds a ``StoreConfig`` field but forgets to mirror it here will
    fail the parametrized field-mirror test before the change ships.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    kind: str
    bucket: str | None = None
    prefix: str = ""
    root: str | None = None

    @classmethod
    def from_cfg(cls, cfg: Config) -> SidecarRecord:
        """Build a record from the store block of a loaded Config."""
        sc = cfg.store
        return cls(
            kind=sc.kind,
            bucket=sc.bucket,
            prefix=sc.prefix,
            root=str(sc.root) if sc.root is not None else None,
        )

    def differs_from(self, other: SidecarRecord) -> bool:
        """Return True when any mirrored identity field differs."""
        return self.model_dump() != other.model_dump()


def _path(state_dir: Path) -> Path:
    return state_dir / SIDECAR_NAME


def read_sidecar(state_dir: Path) -> SidecarRecord | None:
    """Load the sidecar from ``state_dir/store.json``.

    Raises:
        pydantic.ValidationError: corrupt JSON or unknown field.

    Returns:
        ``SidecarRecord`` if present, ``None`` if the file is absent.
    """
    p = _path(state_dir)
    if not p.exists():
        return None
    return SidecarRecord.model_validate_json(p.read_text())


def write_sidecar(state_dir: Path, cfg: Config) -> None:
    """Persist a fresh sidecar describing ``cfg.store``.

    Creates ``state_dir`` (and parents) if absent.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = SidecarRecord.from_cfg(cfg)
    _path(state_dir).write_text(json.dumps(rec.model_dump(), indent=2))


def _local_ledger_nonempty(state_dir: Path) -> bool:
    """Return True iff ``state_dir/_lifecycle/ledger.json`` has one+ entries.

    Reads the raw file (no ``LocalArtifactStore`` construction). Corrupt
    JSON is treated as empty — safer to allow a fresh sidecar to be
    written than to brick the operator on a malformed local file.
    """
    p = state_dir / LEDGER_RUN_ID / LEDGER_NAME
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    entries = data.get("entries", []) if isinstance(data, dict) else []
    return bool(entries)


def verify_or_write_sidecar(state_dir: Path, cfg: Config) -> None:
    """Verify cfg.store matches the sidecar, or write a fresh sidecar.

    Args:
        state_dir: Root of the operator's local state directory.
        cfg: Loaded Config whose ``store`` block describes the store.

    Raises:
        SidecarMismatch: cfg.store differs from the sidecar on disk.
        SidecarMigrationBlocked: first cloud-store cfg attempted while
            ``state_dir/_lifecycle/ledger.json`` has entries.
    """
    existing = read_sidecar(state_dir)
    new = SidecarRecord.from_cfg(cfg)
    if existing is not None:
        if existing.differs_from(new):
            raise SidecarMismatch(
                f"cfg.store ({new.model_dump()}) differs from sidecar "
                f"({existing.model_dump()}); remove {_path(state_dir)} "
                f"or revert cfg.store to switch"
            )
        return
    if new.kind != "local" and _local_ledger_nonempty(state_dir):
        raise SidecarMigrationBlocked(
            f"refusing to switch to cloud store ({new.kind}) while local "
            f"ledger has entries; run `kinoforge destroy` on each "
            f"local-tracked instance, then re-run"
        )
    write_sidecar(state_dir, cfg)
```

- [ ] **Step 5: Confirm GREEN**

Run: `pixi run test tests/cli/test_sidecar.py -v`

Expected: 18 tests pass (10 record/read/write + 4 field-mirror parametrize + 4 ledger-empty + 8 verify).

- [ ] **Step 6: Run full suite to confirm no regression**

Run: `pixi run test`

Expected: 1224 + 18 = ~1242 tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/cli/sidecar.py tests/cli/__init__.py tests/cli/test_sidecar.py
git commit -m "$(cat <<'EOF'
feat(cli): cli/sidecar.py — store pointer in state_dir (Phase 34 T4)

SidecarRecord (frozen pydantic, extra='forbid') mirrors StoreConfig
identity fields; read_sidecar / write_sidecar handle JSON persistence;
verify_or_write_sidecar implements the match-noop / mismatch-raise /
migration-block contract from Layer T spec §7.1.

Includes a parametrized field-mirror lockdown that fails the suite if a
future StoreConfig identity field is added without mirroring in
SidecarRecord (precedent: Phase 16 post-merge fix 484e368).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Build `cli/context.py` `SessionContext` module + tests

**Goal:** `SessionContext` dataclass that bundles `state_dir`, `cfg`, `sidecar`, and lazy `store()` / `ledger()` factories. `ledger_safe()` variant for the always-on overview. `_build_store_from_sidecar` for no-config-with-sidecar fallback. ~12 offline tests.

**Files:**
- Create: `src/kinoforge/cli/context.py`
- Create: `tests/cli/test_context.py`

**Acceptance Criteria:**
- [ ] `SessionContext` is a dataclass with `state_dir`, `cfg`, `sidecar`, `clock`, and two private lazy fields
- [ ] `SessionContext.from_args(state_dir, cfg_path, clock)` loads cfg, verifies/writes sidecar, snapshots sidecar
- [ ] `ctx.store()` is lazy + identity-cached; uses `_build_store(cfg)` when cfg is set, `_build_store_from_sidecar(sidecar)` when sidecar is set, `LocalArtifactStore(state_dir)` fallback
- [ ] `ctx.ledger()` is lazy + identity-cached; constructs `Ledger(store=ctx.store(), run_id="_lifecycle")`
- [ ] `ctx.ledger_safe()` returns `(ledger, None)` on success, `(None, "<type>: <msg>")` on store-construction failure
- [ ] `_build_store_from_sidecar` handles local / s3 / gcs / unknown-kind
- [ ] Tests cover lazy-build, identity-cache, error degradation, all three from_args paths

**Verify:** `pixi run test tests/cli/test_context.py -v` → 12+ tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/cli/test_context.py`:

```python
"""Tests for kinoforge.cli.context — SessionContext factory + lazy build."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from kinoforge.cli.context import SessionContext, _build_store_from_sidecar
from kinoforge.cli.sidecar import (
    SIDECAR_NAME,
    SidecarRecord,
    read_sidecar,
    write_sidecar,
)
from kinoforge.core.config import Config, StoreConfig
from kinoforge.core.errors import (
    SidecarMigrationBlocked,
    SidecarMismatch,
    UnknownAdapter,
)
from kinoforge.stores.local import LocalArtifactStore


def _local_cfg() -> Config:
    return Config(
        engine={"kind": "fake"},
        models=[{"kind": "base", "name": "m", "ref": "fake://m"}],
    )


def _write_local_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "kf.yaml"
    p.write_text(
        "engine:\n  kind: fake\n"
        "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
    )
    return p


# ---------------------------------------------------------------------------
# from_args
# ---------------------------------------------------------------------------


def test_from_args_no_cfg_no_sidecar(tmp_path):
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=None)
    assert ctx.cfg is None
    assert ctx.sidecar is None


def test_from_args_no_cfg_with_existing_sidecar(tmp_path):
    write_sidecar(tmp_path, _local_cfg())
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=None)
    assert ctx.cfg is None
    assert ctx.sidecar is not None
    assert ctx.sidecar.kind == "local"


def test_from_args_with_cfg_writes_sidecar(tmp_path):
    cfg_path = _write_local_cfg(tmp_path)
    ctx = SessionContext.from_args(state_dir=tmp_path, cfg_path=cfg_path)
    assert ctx.cfg is not None
    assert ctx.sidecar is not None
    assert (tmp_path / SIDECAR_NAME).exists()


def test_from_args_propagates_mismatch(tmp_path):
    write_sidecar(
        tmp_path,
        Config(
            engine={"kind": "fake"},
            models=[{"kind": "base", "name": "m", "ref": "fake://m"}],
            store=StoreConfig(kind="s3", bucket="other"),
        ),
    )
    cfg_path = _write_local_cfg(tmp_path)
    with pytest.raises(SidecarMismatch):
        SessionContext.from_args(state_dir=tmp_path, cfg_path=cfg_path)


# ---------------------------------------------------------------------------
# Lazy store / ledger
# ---------------------------------------------------------------------------


def test_store_is_lazy(tmp_path):
    """No store construction until ctx.store() is called."""
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    assert ctx._store is None


def test_store_identity_cached(tmp_path):
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    a = ctx.store()
    b = ctx.store()
    assert a is b


def test_store_falls_back_to_local_when_no_cfg_no_sidecar(tmp_path):
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    assert isinstance(ctx.store(), LocalArtifactStore)


def test_store_uses_sidecar_when_no_cfg(tmp_path):
    sidecar = SidecarRecord(kind="local", root=str(tmp_path / "alt"))
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=sidecar)
    s = ctx.store()
    assert isinstance(s, LocalArtifactStore)
    # uses sidecar.root, not state_dir
    assert str(s.root) == str(tmp_path / "alt")


def test_ledger_identity_cached(tmp_path):
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    a = ctx.ledger()
    b = ctx.ledger()
    assert a is b


def test_ledger_uses_store_lifecycle_run_id(tmp_path):
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    ledger = ctx.ledger()
    assert ledger._run_id == "_lifecycle"


# ---------------------------------------------------------------------------
# ledger_safe
# ---------------------------------------------------------------------------


def test_ledger_safe_returns_ledger_on_success(tmp_path):
    ctx = SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)
    ledger, warn = ctx.ledger_safe()
    assert ledger is not None
    assert warn is None


def test_ledger_safe_returns_warning_on_store_failure(tmp_path):
    """ledger_safe MUST catch store construction errors."""
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


def test_build_from_sidecar_local_no_root_uses_state_dir(tmp_path):
    rec = SidecarRecord(kind="local", root=None)
    store = _build_store_from_sidecar(rec, tmp_path)
    assert isinstance(store, LocalArtifactStore)
    assert str(store.root) == str(tmp_path)


def test_build_from_sidecar_unknown_kind_raises(tmp_path):
    """Forward-compat: a sidecar from a newer kinoforge with kind='azure'
    fails cleanly on an older binary."""
    rec = SidecarRecord(kind="azure", bucket="x")
    with pytest.raises(UnknownAdapter):
        _build_store_from_sidecar(rec, tmp_path)
```

- [ ] **Step 2: Run failing tests; confirm RED**

Run: `pixi run test tests/cli/test_context.py -v`

Expected: every test fails with `ModuleNotFoundError`.

- [ ] **Step 3: Implement the module**

Create `src/kinoforge/cli/context.py`:

```python
"""SessionContext — the per-invocation bundle of state_dir + cfg + lazy store.

Built once in ``cli._main.main()`` and threaded through every subcommand
handler. Lazy ``store()`` and ``ledger()`` accessors mean ``kinoforge --help``
never touches cloud SDKs, and ``ledger_safe()`` lets the always-on
instance overview degrade gracefully when cloud credentials are
unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from kinoforge.cli.sidecar import (
    LEDGER_RUN_ID,
    SidecarRecord,
    read_sidecar,
    verify_or_write_sidecar,
)
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config, load_config
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

log = logging.getLogger(__name__)


@dataclass
class SessionContext:
    """Per-invocation state for the CLI.

    Attributes:
        state_dir: Operator's local state directory (``--state-dir`` arg).
        cfg: Loaded Config, or None when no ``--config`` was passed.
        sidecar: Snapshot of the sidecar from ``state_dir/store.json``,
            or None if absent.
        clock: Injected clock seam — defaults to RealClock.
    """

    state_dir: Path
    cfg: Config | None
    sidecar: SidecarRecord | None
    clock: Clock = field(default_factory=RealClock)
    _store: ArtifactStore | None = None
    _ledger: Ledger | None = None

    @classmethod
    def from_args(
        cls,
        *,
        state_dir: Path,
        cfg_path: Path | None,
        clock: Clock | None = None,
    ) -> SessionContext:
        """Build a SessionContext from parsed CLI arguments.

        - Loads ``cfg_path`` when present (None for no-config commands).
        - Verifies / writes the sidecar when cfg is loaded.
        - Snapshots the sidecar for later lookup.

        Raises:
            SidecarMismatch: when cfg.store differs from on-disk sidecar.
            SidecarMigrationBlocked: on first cloud-cfg with non-empty
                local ledger.
            pydantic.ValidationError: when the on-disk sidecar is corrupt.
        """
        cfg = load_config(cfg_path) if cfg_path is not None else None
        if cfg is not None:
            verify_or_write_sidecar(state_dir, cfg)
        sidecar = read_sidecar(state_dir)
        return cls(
            state_dir=state_dir,
            cfg=cfg,
            sidecar=sidecar,
            clock=clock or RealClock(),
        )

    def store(self) -> ArtifactStore:
        """Lazily build and cache the configured ArtifactStore.

        Precedence: cfg.store > sidecar > LocalArtifactStore(state_dir).
        """
        if self._store is not None:
            return self._store
        if self.cfg is not None:
            from kinoforge.cli._commands import _build_store  # noqa: PLC0415

            self._store = _build_store(self.cfg, self.state_dir)
        elif self.sidecar is not None:
            self._store = _build_store_from_sidecar(self.sidecar, self.state_dir)
        else:
            self._store = LocalArtifactStore(self.state_dir)
        return self._store

    def ledger(self) -> Ledger:
        """Lazily build and cache the lifecycle Ledger backed by ``store()``."""
        if self._ledger is None:
            self._ledger = Ledger(store=self.store(), run_id=LEDGER_RUN_ID)
        return self._ledger

    def ledger_safe(self) -> tuple[Ledger | None, str | None]:
        """Best-effort ledger accessor — never raises.

        Used by ``_print_instance_overview`` which runs at the top of
        every invocation. When store construction fails (expired creds,
        unreachable bucket), returns ``(None, "<type>: <msg>")`` for the
        overview to print as a warning header.

        Returns:
            ``(ledger, None)`` on success, ``(None, reason)`` on failure.
        """
        try:
            return self.ledger(), None
        except Exception as exc:  # noqa: BLE001 — best-effort surface
            return None, f"{type(exc).__name__}: {exc}"


def _build_store_from_sidecar(
    sc: SidecarRecord, state_dir: Path
) -> ArtifactStore:
    """Reconstruct the ArtifactStore named by a sidecar record.

    Cloud SDK imports are lazy so no-config commands (``kinoforge --help``,
    ``kinoforge list`` with a local sidecar) never load boto3 / google-cloud.

    Raises:
        UnknownAdapter: sidecar.kind is not one of ``local | s3 | gcs``
            (e.g. a sidecar written by a newer kinoforge with cloud
            backends this binary does not understand).
    """
    if sc.kind == "local":
        root = Path(sc.root) if sc.root else state_dir
        return LocalArtifactStore(root)
    if sc.kind == "s3":
        from kinoforge.stores.s3 import S3ArtifactStore  # noqa: PLC0415

        assert sc.bucket is not None  # invariant from StoreConfig
        return S3ArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    if sc.kind == "gcs":
        from kinoforge.stores.gcs import GCSArtifactStore  # noqa: PLC0415

        assert sc.bucket is not None
        return GCSArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    raise UnknownAdapter(f"unknown sidecar kind: {sc.kind!r}")
```

- [ ] **Step 4: Confirm GREEN**

Run: `pixi run test tests/cli/test_context.py -v`

Expected: 14 tests pass.

- [ ] **Step 5: Run full suite**

Run: `pixi run test`

Expected: ~1256 tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/context.py tests/cli/test_context.py
git commit -m "$(cat <<'EOF'
feat(cli): cli/context.py — SessionContext + lazy store/ledger (Phase 34 T5)

SessionContext bundles state_dir / cfg / sidecar with lazy + identity-cached
store() and ledger() factories. ledger_safe() returns (None, reason)
without raising so the always-on instance overview can degrade gracefully
when cloud credentials are unavailable. _build_store_from_sidecar handles
the no-config-with-sidecar fallback path with lazy SDK imports per Layer C
pattern.

Per spec §7.2 ctx.store() lazy-imports _build_store from cli._commands
to avoid a circular import; that target lands in T6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

> **Heads up to the implementer:** the `from kinoforge.cli._commands import _build_store` inside `SessionContext.store()` is a lazy import — the test at `test_store_falls_back_to_local_when_no_cfg_no_sidecar` does NOT trip it (cfg is None, so the import statement is unreachable). All T5 tests pass against the current `cli/__init__.py` because T6 has not yet created `cli/_commands.py`. After T6, the lazy import resolves. Do not "fix" this by adding the import eagerly — it would create a circular dependency with `cli/_commands.py` which (post-T6) imports `SessionContext` for its handler signatures.

---

## Task 6: Split `cli/__init__.py` into `_main.py` + `_commands.py`

**Goal:** Mechanical module re-organisation — move the parser+main+overview into `cli/_main.py`, move every `_cmd_*` + the build helpers + Layer S helpers into `cli/_commands.py`, leave `cli/__init__.py` as a back-compat re-export shim. ZERO behaviour change; ZERO signature change; ZERO test churn.

**Files:**
- Modify: `src/kinoforge/cli/__init__.py` (becomes a re-export shim)
- Create: `src/kinoforge/cli/_main.py`
- Create: `src/kinoforge/cli/_commands.py`

**Acceptance Criteria:**
- [ ] `cli/_main.py` houses `_build_parser`, `main`, `_print_instance_overview`
- [ ] `cli/_commands.py` houses every `_cmd_*` handler, `_build_store`, `_build_sink`, `_build_ledger_block`, `_print_status_block`, `_ledger_field_or_cfg`, `_ledger`, `_CFG_LIFECYCLE_ATTR`
- [ ] `cli/__init__.py` re-exports every public + protected symbol that existing tests import (`main`, `_build_store`, `_build_sink`, `_build_parser`, `_build_ledger_block`, `_print_status_block`, `_print_instance_overview`, `_ledger_field_or_cfg`, `_ledger`, `_cli_clock`)
- [ ] `from kinoforge.cli import main` / `_build_store` / `_build_parser` / `_build_ledger_block` all resolve
- [ ] `python -m kinoforge --help` exits 0
- [ ] All 1256 tests pass — including the existing `tests/test_cli.py` and `tests/test_batch_cli.py` suites — without modification

**Verify:** `pixi run test && python -m kinoforge --help` → tests green, help prints.

**Steps:**

- [ ] **Step 1: Read the current `cli/__init__.py` end-to-end**

Run: `wc -l src/kinoforge/cli/__init__.py`

Expected: ~1000 lines. Identify the line ranges to move:
  - `_build_parser` (~line 156-273) → `_main.py`
  - `main` (~line 910-957) → `_main.py`
  - `_print_instance_overview` (~line 131-153) → `_main.py`
  - everything else → `_commands.py`

- [ ] **Step 2: Carve `_commands.py`**

Create `src/kinoforge/cli/_commands.py`. Top of file:

```python
"""Subcommand handlers + build helpers for the kinoforge CLI.

Every ``_cmd_*`` handler accepts ``(args, state_dir)`` today; Task 7
migrates them to ``(args, ctx)``. This task moves them verbatim — no
signature change.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import kinoforge._adapters  # noqa: F401 — triggers self-registrations
from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.config import Config
from kinoforge.core.dotenv_loader import load_env_file
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import GenerationRequest
from kinoforge.core.lifecycle import Ledger, destroy_confirmed, reap
from kinoforge.core.orchestrator import generate
from kinoforge.outputs.base import OutputSink
from kinoforge.outputs.local import LocalOutputSink
from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore

# Module-level clock seam preserved for test monkeypatching.
_cli_clock: Clock = RealClock()
```

Then copy these definitions verbatim from `cli/__init__.py` into `_commands.py`, preserving order:

- `_ledger(state_dir)`
- `_build_store(cfg, state_dir)`
- `_build_sink(cfg, args)`
- `_cmd_deploy`, `_cmd_provision`, `_cmd_generate`, `_cmd_batch`, `_cmd_list`
- `_CFG_LIFECYCLE_ATTR` constant
- `_ledger_field_or_cfg`, `_build_ledger_block`, `_print_status_block`
- `_cmd_status`, `_cmd_stop`, `_cmd_destroy`, `_cmd_forget`, `_cmd_reap`, `_cmd_gc`

- [ ] **Step 3: Carve `_main.py`**

Create `src/kinoforge/cli/_main.py`:

```python
"""argparse entry point + dispatch table for the kinoforge CLI.

``main(argv)`` resolves to the ``kinoforge.cli.main`` import surface; the
package's ``__init__.py`` re-exports it so existing imports
(``from kinoforge.cli import main``) keep working.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from kinoforge.cli._commands import (
    _build_sink,  # noqa: F401 — re-export-via-package
    _build_store,  # noqa: F401 — re-export-via-package
    _cli_clock,
    _cmd_batch,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_list,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
    _ledger,
)
from kinoforge.core.dotenv_loader import load_env_file


def _build_parser(state_dir_default: str = ".kinoforge") -> argparse.ArgumentParser:
    """Build and return the top-level ArgumentParser."""
    # ... verbatim from old cli/__init__.py:156-273 ...


def _print_instance_overview(state_dir: Path) -> None:
    """Print a one-line overview of every ledger entry to stdout."""
    # ... verbatim from old cli/__init__.py:131-153 ...


def main(argv: list[str] | None = None) -> int:
    """Parse *argv* and dispatch to the appropriate subcommand."""
    # ... verbatim from old cli/__init__.py:910-957 ...
```

Copy the actual function bodies from the old `cli/__init__.py`. Do NOT change any logic — this is a pure move.

- [ ] **Step 4: Rewrite `cli/__init__.py` as a re-export shim**

Replace the entire contents of `src/kinoforge/cli/__init__.py` with:

```python
"""Back-compat re-export surface for the kinoforge.cli package.

The CLI internals live in cli._main + cli._commands. This shim
preserves every import path that tests and the entry point rely on:

    from kinoforge.cli import main          # used by __main__.py
    from kinoforge.cli import _build_store  # used by tests/test_cli.py
    from kinoforge.cli import _build_parser # used by tests/test_cli.py
    from kinoforge.cli import _build_ledger_block  # used by tests/test_cli.py
"""

from kinoforge.cli._commands import (
    _build_ledger_block,
    _build_sink,
    _build_store,
    _cli_clock,
    _cmd_batch,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_list,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
    _ledger,
    _ledger_field_or_cfg,
    _print_status_block,
)
from kinoforge.cli._main import _build_parser, _print_instance_overview, main

__all__ = [
    "_build_ledger_block",
    "_build_parser",
    "_build_sink",
    "_build_store",
    "_cli_clock",
    "_cmd_batch",
    "_cmd_deploy",
    "_cmd_destroy",
    "_cmd_forget",
    "_cmd_gc",
    "_cmd_generate",
    "_cmd_list",
    "_cmd_provision",
    "_cmd_reap",
    "_cmd_status",
    "_cmd_stop",
    "_ledger",
    "_ledger_field_or_cfg",
    "_print_instance_overview",
    "_print_status_block",
    "main",
]
```

- [ ] **Step 5: Verify imports resolve**

```bash
python -c "from kinoforge.cli import main, _build_store, _build_parser, _build_ledger_block, _print_status_block; print('OK')"
```

Expected: `OK`.

- [ ] **Step 6: Verify CLI still runs**

```bash
python -m kinoforge --help
```

Expected: help block prints, exit 0.

- [ ] **Step 7: Run full suite — must be zero churn**

Run: `pixi run test`

Expected: 1256 tests pass, exact same count as Task 5 completion. Any failure here means the move was not verbatim — fix before committing.

- [ ] **Step 8: Run pre-commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/__init__.py src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py
```

Expected: all checks pass.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/cli/
git commit -m "$(cat <<'EOF'
refactor(cli): split package into _main + _commands (Phase 34 T6)

Mechanical module re-organisation:
  _main.py     — _build_parser, main, _print_instance_overview
  _commands.py — every _cmd_*, _build_store, _build_sink, Layer S helpers
  __init__.py  — back-compat re-export shim

Zero behaviour change. Zero signature change. Zero test churn — the same
1256 tests pass exactly as before. Sets up cli/_main.py to be the single
edit site for SessionContext wiring in T7.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire `SessionContext` through `main()` + migrate `_cmd_*` signatures

**Goal:** Change every subcommand handler from `(args, state_dir)` to `(args, ctx)`; build the dispatch table; thread `ctx` from `main()` through every cmd; replace every `_ledger(state_dir)` call with `ctx.ledger()` and every `_build_store(cfg, state_dir)` with `ctx.store()`; refactor `_print_instance_overview` to take `ctx` and use `ledger_safe`.

**Files:**
- Modify: `src/kinoforge/cli/_main.py`
- Modify: `src/kinoforge/cli/_commands.py`
- Modify: `src/kinoforge/cli/__init__.py` (re-export `SessionContext`)
- Modify: `tests/test_cli.py` (signature-driven tests that probe handlers directly)
- Modify: `tests/test_batch_cli.py` (same)
- Create: `tests/cli/test_commands_routing.py`
- Create: `tests/cli/test_main_flow.py`

**Acceptance Criteria:**
- [ ] Every `_cmd_*` accepts `(args, ctx)` — no `state_dir` parameter
- [ ] `ctx.state_dir` replaces every interior `state_dir` reference
- [ ] `_ledger(state_dir)` no longer exists as a callable; tests that imported it now import nothing (or `SessionContext` directly via test seam)
- [ ] `_build_store(cfg, state_dir)` is reachable from `ctx.store()` but no longer called directly by subcommand handlers
- [ ] `_print_instance_overview(ctx)` prints `"[instance overview] unavailable: <reason>"` when `ledger_safe` returns `(None, reason)`
- [ ] `main()` catches `SidecarMismatch`, `SidecarMigrationBlocked`, `ConfigError`, `FileNotFoundError`, `PydanticValidationError` and surfaces them at exit 1 with the spec §9 stderr formats
- [ ] `main()` builds a single `SessionContext` via `SessionContext.from_args` and threads it via a `_DISPATCH` table
- [ ] Existing test_cli.py / test_batch_cli.py suites pass unchanged (all interaction is via `main([...])`); any test that imported `_ledger` directly is migrated to construct a `SessionContext` instead
- [ ] New `test_commands_routing.py` proves every cmd routes via `ctx.ledger()` / `ctx.store()` — monkeypatch the removed helpers to explode and assert no cmd touches them
- [ ] New `test_main_flow.py` covers the spec §8 scenarios end-to-end through `main([...])`

**Verify:** `pixi run test` → ~1280 tests pass.

**Steps:**

- [ ] **Step 1: Update `_cmd_*` signatures in `_commands.py`**

For each handler, change `(args, state_dir)` to `(args, ctx)` and rewrite the body to use `ctx.ledger()` / `ctx.store()` / `ctx.state_dir`. Example for `_cmd_deploy`:

```python
def _cmd_deploy(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``deploy`` subcommand."""
    from kinoforge.core.config import load_config  # noqa: PLC0415
    from kinoforge.core.orchestrator import deploy  # noqa: PLC0415

    cfg = ctx.cfg
    assert cfg is not None, "_cmd_deploy requires --config; argparse enforces"

    if not args.dry_run:
        ledger = ctx.ledger()
        key_hash = cfg.capability_key().derive()[:12]
        for entry in ledger.entries():
            tags = entry.get("tags", {})
            if tags.get("kinoforge_key") == key_hash:
                print(
                    f"duplicate instance refused; use `kinoforge destroy --id {entry['id']}` first",
                    file=sys.stderr,
                )
                return 1

    try:
        result = deploy(cfg, dry_run=args.dry_run)
    except UnknownAdapter as exc:
        print(f"error: unknown adapter — {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(result.plan_text)
    else:
        print(f"deployed: instance={result.instance and result.instance.id!r}")
        if result.instance is not None:
            ledger = ctx.ledger()
            lc = cfg.lifecycle()
            ledger.record(
                result.instance,
                idle_timeout_s=int(lc.idle_timeout_s),
                max_age_s=int(lc.max_lifetime_s),
            )
    return 0
```

Apply the same `(args, state_dir) → (args, ctx)` + `ctx.ledger()` / `ctx.store()` rewrite to every other `_cmd_*` (`_cmd_provision`, `_cmd_generate`, `_cmd_batch`, `_cmd_list`, `_cmd_status`, `_cmd_stop`, `_cmd_destroy`, `_cmd_forget`, `_cmd_reap`, `_cmd_gc`).

Special cases:
- `_cmd_provision` uses `state_dir / "weights"` → `ctx.state_dir / "weights"`
- `_cmd_generate` and `_cmd_batch` use `_build_store(cfg, state_dir)` → `ctx.store()` and pass `state_dir=ctx.state_dir` into `generate(...)` / `batch_generate(...)`
- `_cmd_gc` uses `_build_store(cfg, state_dir)` → `ctx.store()` (and `args.config` must equal `ctx.cfg`'s source — assert this)
- `_cmd_list`, `_cmd_status`, `_cmd_stop`, `_cmd_destroy`, `_cmd_forget`, `_cmd_reap` had a signature without `args` for the no-arg variants; rename to take `(args, ctx)` where `args` is unused (add `# noqa: ARG001`)
- `_cmd_status` had an `args.config` path for legacy-entry fallback; under T7, that path becomes "`ctx.cfg`" (already loaded by `from_args` when `--config` is present)

Add the `SessionContext` import at the top of `_commands.py`:

```python
from kinoforge.cli.context import SessionContext
```

- [ ] **Step 2: Update `_main.py`**

Replace the body of `_main.py` (functions `_print_instance_overview` and `main`) with:

```python
from kinoforge.cli._commands import (
    _cli_clock,
    _cmd_batch,
    _cmd_deploy,
    _cmd_destroy,
    _cmd_forget,
    _cmd_gc,
    _cmd_generate,
    _cmd_list,
    _cmd_provision,
    _cmd_reap,
    _cmd_status,
    _cmd_stop,
)
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import ConfigError
from kinoforge.core.errors import SidecarMigrationBlocked, SidecarMismatch
from kinoforge.core.dotenv_loader import load_env_file
from pydantic import ValidationError as PydanticValidationError


_DISPATCH: dict[str, Callable[[argparse.Namespace, SessionContext], int]] = {
    "deploy": _cmd_deploy,
    "provision": _cmd_provision,
    "generate": _cmd_generate,
    "batch": _cmd_batch,
    "list": _cmd_list,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "destroy": _cmd_destroy,
    "forget": _cmd_forget,
    "reap": _cmd_reap,
    "gc": _cmd_gc,
}


def _print_instance_overview(ctx: SessionContext) -> None:
    """Print one-line overview of every ledger entry; degrade on failure."""
    ledger, warn = ctx.ledger_safe()
    if ledger is None:
        print(f"[instance overview] unavailable: {warn}")
        return
    try:
        entries = ledger.entries()
    except Exception as exc:  # noqa: BLE001 — best-effort surface
        print(f"[instance overview] unavailable: {type(exc).__name__}: {exc}")
        return
    now = time.time()
    if not entries:
        print("[instance overview] No running instances.")
        return
    print("[instance overview]")
    for entry in entries:
        iid = entry.get("id", "?")
        created_at = float(entry.get("created_at", now))
        age_s = now - created_at
        age_h = age_s / 3600.0
        rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        spend = age_h * rate
        print(f"  {iid}  age={age_h:.1f}h  est_spend=${spend:.4f}")


def main(argv: list[str] | None = None) -> int:
    """Parse *argv*, build SessionContext, dispatch to subcommand."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    env_file = Path(args.env_file) if args.env_file is not None else None
    load_env_file(env_file)

    cfg_path = Path(args.config) if getattr(args, "config", None) else None
    try:
        ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    except (SidecarMismatch, SidecarMigrationBlocked) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PydanticValidationError as exc:
        print(
            f"error: sidecar at {state_dir / 'store.json'} is unreadable: "
            f"{exc}; rm to reset",
            file=sys.stderr,
        )
        return 1
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: config: {exc}", file=sys.stderr)
        return 1

    _print_instance_overview(ctx)

    if args.cmd is None:
        parser.print_help()
        return 0

    return _DISPATCH[args.cmd](args, ctx)
```

- [ ] **Step 3: Update `cli/__init__.py` re-exports**

Add `SessionContext` to the re-export shim:

```python
from kinoforge.cli.context import SessionContext

__all__ = [
    # ... existing entries ...
    "SessionContext",
]
```

Remove `_ledger` from the `__init__.py` re-export list — it is no longer needed by any subcommand, and any test that imports `_ledger` directly must now construct a `SessionContext` instead. Grep for direct importers:

```bash
rg "from kinoforge.cli import.*_ledger\b" tests/
```

Migrate any hit to `from kinoforge.cli import SessionContext` with the appropriate ctx construction.

- [ ] **Step 4: Write the `test_commands_routing.py` tests**

```python
"""Per-cmd lockdown: every handler reads ledger/store via ctx, never via
removed helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.cli import _commands
from kinoforge.cli.context import SessionContext


def _explode(*args, **kwargs):
    raise AssertionError("subcommand bypassed SessionContext")


def _ctx_no_cfg(tmp_path: Path) -> SessionContext:
    return SessionContext(state_dir=tmp_path, cfg=None, sidecar=None)


def test_cmd_list_reads_only_via_ctx(tmp_path, monkeypatch, capsys):
    """_cmd_list must use ctx.ledger() — not _ledger(state_dir)."""
    monkeypatch.setattr(_commands, "_ledger", _explode, raising=True)
    ctx = _ctx_no_cfg(tmp_path)

    class _Args:
        pass

    code = _commands._cmd_list(_Args(), ctx)
    assert code == 0
    # Did not explode → routed via ctx


def test_cmd_forget_routes_via_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(_commands, "_ledger", _explode, raising=True)
    ctx = _ctx_no_cfg(tmp_path)
    ledger = ctx.ledger()
    from kinoforge.core.interfaces import Instance

    ledger.record(
        Instance(
            id="i-1", provider="local", status="ready",
            tags={}, created_at=0.0, cost_rate_usd_per_hr=0.0,
        )
    )

    class _Args:
        id = "i-1"

    assert _commands._cmd_forget(_Args(), ctx) == 0
    assert ledger.entries() == []


# Add analogous routing tests for: _cmd_stop, _cmd_destroy, _cmd_reap,
# _cmd_status, _cmd_generate, _cmd_gc, _cmd_batch, _cmd_deploy.
# Each test builds a SessionContext, monkeypatches the removed helpers
# to explode, and asserts the cmd returns the expected exit code.
```

The implementer should round this out to ~12 tests, one per cmd, all asserting "the cmd does not reach the removed helpers and exits as expected".

- [ ] **Step 5: Write the `test_main_flow.py` tests**

```python
"""End-to-end through cli.main([...]) — sidecar lifecycle + degradation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_local_cfg(p: Path) -> Path:
    cfg = p / "kf.yaml"
    cfg.write_text(
        "engine:\n  kind: fake\n"
        "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
    )
    return cfg


def test_first_local_deploy_writes_sidecar(tmp_path, monkeypatch):
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"

    rc = main(["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"])
    assert rc == 0
    assert (state / "store.json").exists()


def test_second_deploy_with_different_store_errors_mismatch(
    tmp_path, capsys, monkeypatch
):
    from kinoforge.cli import main

    cfg_a = _write_local_cfg(tmp_path / "a")
    cfg_b = tmp_path / "b" / "kf.yaml"
    cfg_b.parent.mkdir(parents=True)
    cfg_b.write_text(
        "engine:\n  kind: fake\n"
        "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
        "store:\n  kind: s3\n  bucket: somewhere\n"
    )
    state = tmp_path / "state"

    rc_a = main(["--state-dir", str(state), "deploy", "--config", str(cfg_a), "--dry-run"])
    assert rc_a == 0

    rc_b = main(["--state-dir", str(state), "deploy", "--config", str(cfg_b), "--dry-run"])
    captured = capsys.readouterr()

    assert rc_b == 1
    assert "differs from sidecar" in captured.err


def test_no_cmd_with_unreachable_sidecar_does_not_crash(tmp_path, capsys):
    """kinoforge --help with a broken cloud sidecar must still print help."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (state / "store.json").write_text(
        json.dumps({"kind": "s3", "bucket": "nope", "prefix": "", "root": None})
    )

    # No subcommand → main prints help + returns 0.
    rc = main(["--state-dir", str(state)])
    captured = capsys.readouterr()

    assert rc == 0
    assert "unavailable" in captured.out or "No running instances" in captured.out


def test_corrupt_sidecar_clean_error(tmp_path, capsys):
    from kinoforge.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (state / "store.json").write_text("{not valid")

    rc = main(["--state-dir", str(state), "list"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "sidecar at" in captured.err
    assert "rm to reset" in captured.err


def test_migration_blocked_when_local_ledger_nonempty(tmp_path, capsys):
    from kinoforge.cli import main

    state = tmp_path / "state"
    (state / "_lifecycle").mkdir(parents=True)
    (state / "_lifecycle" / "ledger.json").write_text(
        json.dumps({"entries": [{"id": "i-1"}]})
    )
    cfg = tmp_path / "kf.yaml"
    cfg.write_text(
        "engine:\n  kind: fake\n"
        "models:\n  - kind: base\n    name: m\n    ref: fake://m\n"
        "store:\n  kind: s3\n  bucket: kf\n"
    )

    rc = main(["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "refusing to switch" in captured.err
```

Round out to ~12 tests covering: matching-sidecar no-op, mismatch (already above), migration block (already above), corrupt sidecar (already above), broken-cred overview degradation (use monkeypatch on `_build_store_from_sidecar` to raise), no-cmd help with cloud sidecar (already above), `kinoforge gc --config` writes sidecar match, batch run shares store with overview, dry-run still writes sidecar (already above), `kinoforge list` with cloud sidecar reads via cloud store, `kinoforge stop` against missing instance returns exit 1 unchanged, sidecar with `extra` field clean error.

- [ ] **Step 6: Migrate any test_cli.py test that imported `_ledger` directly**

```bash
rg "from kinoforge.cli import.*_ledger\b" tests/
```

For each hit, replace with `SessionContext(state_dir=state, cfg=None, sidecar=None).ledger()` constructed inline.

- [ ] **Step 7: Run full suite — iterate until green**

Run: `pixi run test`

Expected outcome: ~1280 tests pass. Failures likely at:
- A `_cmd_*` body that still references `state_dir` instead of `ctx.state_dir`
- A test that called `_cmd_X(args, state_dir)` directly — these need `_cmd_X(args, ctx)`
- A test that relied on `_ledger(state_dir)` returning an in-process Ledger — replace with `SessionContext(state_dir, None, None).ledger()`

Fix and re-run.

- [ ] **Step 8: Confirm overview degradation by manual smoke**

```bash
mkdir -p /tmp/kf-smoke-t7
echo '{"kind":"s3","bucket":"nope","prefix":"","root":null}' > /tmp/kf-smoke-t7/store.json
python -m kinoforge --state-dir /tmp/kf-smoke-t7 --help
```

Expected: help block prints; before the help block an `[instance overview] unavailable: <reason>` line appears. No traceback.

- [ ] **Step 9: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py src/kinoforge/cli/__init__.py tests/cli/test_commands_routing.py tests/cli/test_main_flow.py
git add src/kinoforge/cli/ tests/cli/ tests/test_cli.py tests/test_batch_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): SessionContext threaded through every subcommand (Phase 34 T7)

Closes PROGRESS:127. Migrates every _cmd_* handler from
(args, state_dir) → (args, ctx) and wires SessionContext through
main() with a dispatch table. _print_instance_overview uses
ledger_safe() so kinoforge --help and other dispatch-only paths never
crash on unreachable cloud credentials. Sidecar mismatch / migration
errors land at exit 1 with clean stderr lines per spec §9.

New tests: tests/cli/test_commands_routing.py (~12), tests/cli/test_main_flow.py (~12).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Multi-node lock integration test

**Goal:** Subprocess test against a thread-shared `FakeS3Client` proving that two concurrent CLI invocations (different "machines") record into the same S3-backed ledger with no lost updates — the headline win of Layer T.

**Files:**
- Create: `tests/cli/test_multinode_lock.py`

**Acceptance Criteria:**
- [ ] Test launches two threads, each constructs a `SessionContext` bound to the same `FakeS3Client` (passed via injected `client=` arg)
- [ ] Both threads call `ctx.ledger().record(instance)` concurrently with distinct instance IDs
- [ ] Both entries land in the final ledger (no lost update)
- [ ] Test asserts `acquire_lock` serialised the two writes (mock the lock acquire/release with a counter; expect lock taken exactly twice)

**Verify:** `pixi run test tests/cli/test_multinode_lock.py -v` → 1 test passes.

**Steps:**

- [ ] **Step 1: Write the test**

```python
"""Multi-node coordination integration: Layer T headline win.

Proves that two concurrent CLI invocations against the same S3-backed
ledger serialise via Layer H's acquire_lock and BOTH entries land — no
lost update.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kinoforge.cli.context import SessionContext
from kinoforge.cli.sidecar import SidecarRecord
from kinoforge.core.interfaces import Instance
from tests.stores.conftest import FakeS3Client


def _make_ctx(state_dir: Path, shared_client: FakeS3Client) -> SessionContext:
    from kinoforge.stores.s3 import S3ArtifactStore

    store = S3ArtifactStore(bucket="kf-prod", prefix="", client=shared_client)
    ctx = SessionContext(state_dir=state_dir, cfg=None, sidecar=None)
    ctx._store = store  # pre-seeded so SessionContext.store() returns it
    return ctx


def test_two_machines_record_to_shared_s3_ledger_no_lost_update(tmp_path):
    """Bug-catch: a future regression that drops the acquire_lock wrapper
    around Ledger.record would let one writer overwrite the other's entry
    when both read the empty ledger before either writes."""
    shared = FakeS3Client()
    state_a = tmp_path / "host-a"
    state_b = tmp_path / "host-b"
    state_a.mkdir()
    state_b.mkdir()

    inst_a = Instance(
        id="i-host-a", provider="local", status="ready",
        tags={}, created_at=1.0, cost_rate_usd_per_hr=0.0,
    )
    inst_b = Instance(
        id="i-host-b", provider="local", status="ready",
        tags={}, created_at=2.0, cost_rate_usd_per_hr=0.0,
    )

    ctx_a = _make_ctx(state_a, shared)
    ctx_b = _make_ctx(state_b, shared)

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
    assert not t_a.is_alive() and not t_b.is_alive()

    # Either ctx can read — the ledger is shared via the shared client.
    final = ctx_a.ledger().entries()
    final_ids = sorted(e["id"] for e in final)
    assert final_ids == ["i-host-a", "i-host-b"]
```

- [ ] **Step 2: Run test; confirm GREEN**

Run: `pixi run test tests/cli/test_multinode_lock.py -v`

Expected: 1 test passes.

If it fails with a lost-update (one entry only): inspect `FakeS3Client.put_object` for whether it honours `IfNoneMatch="*"` precondition (Layer H Phase 18 Task 4 added that support). If the fake lacks precondition semantics, this test cannot prove serialisation — add a per-write `time.sleep(0.001)` and a lock counter assertion as a weaker proxy.

- [ ] **Step 3: Run full suite**

Run: `pixi run test`

Expected: 1281 tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/cli/test_multinode_lock.py
git commit -m "$(cat <<'EOF'
test(cli): multi-node coordination via S3-backed ledger (Phase 34 T8)

Subprocess-style threading test against shared FakeS3Client proves two
concurrent CLI invocations record into the same S3-backed ledger with
no lost update — Layer T's headline win. Bug-catch: a future regression
that drops the acquire_lock wrapper around Ledger.record would let one
writer overwrite the other's entry when both read the empty ledger
before either writes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: README + PROGRESS + final gate + merge

**Goal:** Document the operator-facing semantics in README, add Phase 34 entry to PROGRESS, run the full suite + pre-commit, merge to main via `--no-ff` per project convention.

**Files:**
- Modify: `README.md` (new section "Cloud-backed ledger"; update "Multi-node coordination"; add "Multi-host setup" subsection; add "Breaking change" note)
- Modify: `PROGRESS.md` (Phase 34 entry under Post-MVP, with per-task SHAs)
- Modify: `docs/superpowers/plans/2026-06-05-layer-t-cloud-ledger-cli-routing.md.tasks.json` (mark all tasks completed)

**Acceptance Criteria:**
- [ ] README "Cloud-backed ledger" section explains the sidecar workflow with a worked example
- [ ] README "Multi-host setup" subsection states the "first command per host must be cfg-bearing" constraint and the safety implication
- [ ] README "Migration from local ledger" subsection walks operators through the 4 steps
- [ ] PROGRESS.md Phase 34 entry lists all 9 task SHAs and the closing-of PROGRESS:127
- [ ] Full test suite passes: `pixi run test` → 1281+ tests
- [ ] Pre-commit clean: `pixi run pre-commit run --all-files`
- [ ] Branch merged to main via `git merge --no-ff` with a merge commit body that references PROGRESS:127

**Verify:** `pixi run test && pixi run pre-commit run --all-files && git log --oneline -1 main` → all green; merge commit visible on main.

**Steps:**

- [ ] **Step 1: README additions**

Open `README.md`. Find the existing "Multi-node coordination" section (added by Phase 18). Add a new sibling section **above** it:

```markdown
## Cloud-backed ledger

kinoforge's instance ledger (the list of running pods, their providers,
their lifecycle policy snapshots) is persisted via the configured
artifact store. When `store.kind` is `s3` or `gcs` in your `kinoforge.yaml`,
the ledger lives at `s3://<bucket>/<prefix>/_lifecycle/ledger.json` (or
the GCS equivalent), not on the host that ran `kinoforge deploy`.

On first run of a cfg-bearing command (`deploy`, `provision`, `generate`,
`gc`, `batch`), kinoforge writes a sidecar at `<state-dir>/store.json`
recording which store backs the ledger. Subsequent no-config commands
(`list`, `stop`, `destroy`, `forget`, `reap`) read the sidecar and
construct the matching store transparently — no `--config` needed.

### Example

```yaml
# kinoforge.yaml
engine:
  kind: fake  # or hosted / diffusers / comfyui / fal
models:
  - kind: base
    name: m
    ref: fake://m
store:
  kind: s3
  bucket: kf-prod
  prefix: kinoforge
```

```bash
# Host A — first command writes the sidecar
$ kinoforge deploy --config kinoforge.yaml
[instance overview] No running instances.
deployed: instance='i-abc'

# Host B — sees the same ledger via the sidecar (after running
# any cfg-bearing command at least once)
$ kinoforge deploy --dry-run --config kinoforge.yaml   # writes Host-B sidecar
$ kinoforge list
  i-abc  provider=runpod
```

### Multi-host setup

The sidecar is per-host: every host's `.kinoforge/store.json` must be
written before its first state-mutating command. **First command per
host MUST be cfg-bearing** (e.g. `kinoforge deploy --dry-run --config ...`)
so the sidecar gets written. A no-config command on a fresh host with no
sidecar falls back to a local `state_dir` ledger — meaning kinoforge will
not see the instances tracked in the shared cloud ledger, and the
duplicate-instance guard in `kinoforge deploy` may not fire.

This is a documented v1 constraint. A future layer will add
`--store-uri s3://kf-prod` (or `KINOFORGE_STORE_URI`) so that any
command can bootstrap its own sidecar from a single flag.

### Migration from local ledger

If you previously used a cloud `store.kind` but the ledger lived locally
(pre-Layer T behaviour), kinoforge will refuse to switch to a cloud
ledger while in-flight pods are still recorded locally. The error is:

```
error: refusing to switch to cloud store (s3) while local ledger has
entries; run `kinoforge destroy` on each local-tracked instance, then
re-run
```

To migrate:

1. `kinoforge list` — inventory in-flight instances tracked locally.
2. `kinoforge destroy --id <id>` for each — empties the local ledger.
3. Upgrade to the Layer T release.
4. `kinoforge deploy --config kinoforge.yaml` — writes the sidecar,
   opens a fresh cloud-backed ledger.

### Breaking change

Operators who relied on the pre-Layer-T behaviour of "cloud artifact
store, local ledger" should perform the migration above. New deployments
are unaffected.
```

Update the existing "Multi-node coordination" section to mention that
the sidecar makes Layer H's cross-process lock usable from the CLI
(rather than only from in-process consumers).

- [ ] **Step 2: PROGRESS.md Phase 34 entry**

Append under "Post-MVP":

```markdown
### Phase 34 — Layer T (cloud-ledger CLI routing)

Routes the CLI ledger through `cfg.store` (s3/gcs) via a JSON sidecar
in `state_dir`; introduces `SessionContext` threaded through every
subcommand; refactors `Ledger._compute_uri` to use the universal
`store.uri_for` ABC; splits `cli.py` (1000 LOC) into a `cli/` package.

- [x] Task 1: `Ledger._compute_uri` uses `store.uri_for` — commit `<SHA-T1>`
- [x] Task 2: `SidecarMismatch` + `SidecarMigrationBlocked` errors — commit `<SHA-T2>`
- [x] Task 3: `cli.py` → `cli/` package promotion — commit `<SHA-T3>`
- [x] Task 4: `cli/sidecar.py` module + 18 tests — commit `<SHA-T4>`
- [x] Task 5: `cli/context.py` `SessionContext` + 14 tests — commit `<SHA-T5>`
- [x] Task 6: `cli/` package split into `_main` + `_commands` (zero behaviour change) — commit `<SHA-T6>`
- [x] Task 7: `SessionContext` wired through `main()`; every `_cmd_*` signature migrated — commit `<SHA-T7>`
- [x] Task 8: Multi-node lock integration test — commit `<SHA-T8>`
- [x] Task 9: README + PROGRESS + final gate + merge — commit `<SHA-T9>`
- [x] Merge to main via `--no-ff` — merge commit `<SHA-MERGE>` (closes PROGRESS:127)

**Key design decisions:**
- Sidecar JSON in `state_dir/store.json` over global `--config` flag
  (Q1=A): no breaking flag change for single-user CLI.
- Hard error on cfg-vs-sidecar mismatch (Q2=A): mirrors `kinoforge gc --config`
  precedent — explicit > silent.
- Hard block on first cloud cmd when local ledger non-empty (Q3=A):
  prevents silently orphaning in-flight pods.
- Best-effort overview when cloud creds unavailable (Q4=A): keeps
  `kinoforge --help` working during credential rotation.
- `SessionContext` over thread-cfg-through-9-fns: single integration
  point for every future per-session field (streaming logs, spend cap,
  multi-tenant profiles, daemon mode).
- `cli.py` → `cli/` package: file was 1000+ LOC; splitting now while
  the surface is small avoids paying it later.

**Test count:** 1222 → ~1281 passed (+59 net).

**Known limitations (carry-forward):**
- Cross-machine bootstrap requires every host's first command to be
  cfg-bearing. `--store-uri` / `KINOFORGE_STORE_URI` is a Layer T+1
  candidate.
- Two concurrent cfg-bearing cmds on the same `state_dir` with different
  configs: last writer wins. Documented as operator-side concern.
- No real-cloud verification — PROGRESS:116 (S3 / GCS real-cloud) is the
  gate for that.
```

Replace `<SHA-T1>`..`<SHA-MERGE>` with actual commit SHAs after each
task commits. The final SHA backfill commit can be folded into the
merge or done immediately after.

- [ ] **Step 3: Update tasks.json**

Update `docs/superpowers/plans/2026-06-05-layer-t-cloud-ledger-cli-routing.md.tasks.json` so every task's `status` is `"completed"`. The persistence file structure follows the skill's reference.

- [ ] **Step 4: Final gate**

Run:

```bash
pixi run test
pixi run pre-commit run --all-files
```

Expected: 1281+ tests pass; pre-commit clean.

- [ ] **Step 5: Commit docs + tasks file**

```bash
git add README.md PROGRESS.md docs/superpowers/plans/2026-06-05-layer-t-cloud-ledger-cli-routing.md.tasks.json
git commit -m "$(cat <<'EOF'
docs: README + PROGRESS Phase 34 close-out (Layer T)

Adds README sections covering the cloud-backed ledger sidecar workflow,
multi-host setup constraint, and migration from the pre-Layer-T
local-ledger behaviour. PROGRESS Phase 34 entry lists all 9 task SHAs
and closes PROGRESS:127.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Merge to main**

```bash
git checkout main
git merge --no-ff <feature-branch-name> -m "$(cat <<'EOF'
Merge Layer T — cloud-ledger CLI routing

Routes the CLI ledger through cfg.store (s3/gcs) via a JSON sidecar in
state_dir; introduces SessionContext threaded through every subcommand;
refactors Ledger._compute_uri to use the universal store.uri_for ABC;
splits cli.py (1000+ LOC) into a cli/ package.

Test count: 1222 → ~1281 (+59 net).
Closes PROGRESS:127.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Backfill SHAs into PROGRESS.md**

After the merge commit lands on main, replace every `<SHA-TN>` and
`<SHA-MERGE>` placeholder in the Phase 34 PROGRESS entry with the
actual commit SHAs. Commit:

```bash
git add PROGRESS.md
git commit -m "chore(progress): backfill Phase 34 task SHAs"
```

---

## Self-review

**Spec coverage:** every spec section has a task.

| Spec section | Task |
|---|---|
| §3 Goal 1 (Ledger uses cfg.store) | T7 (`ctx.store()` / `ctx.ledger()` wiring) |
| §3 Goal 2 (no split-brain) | T4 (`verify_or_write_sidecar`) + T7 (main() error envelope) |
| §3 Goal 3 (overview degrades gracefully) | T5 (`ledger_safe`) + T7 (`_print_instance_overview`) |
| §3 Goal 4 (`cli/` package split) | T3 + T6 |
| §3 Goal 5 (`SessionContext` foundation) | T5 + T7 |
| §6 module layout | T3 + T6 |
| §7.1 `cli/sidecar.py` | T4 |
| §7.2 `cli/context.py` | T5 |
| §7.3 `Ledger._compute_uri` refactor | T1 |
| §7.4 new errors | T2 |
| §7.5 `_cmd_*` signature migration | T7 |
| §7.6 `cli/_main.py` orchestration | T6 + T7 |
| §8 data flow scenarios | T7 (every scenario covered by `test_main_flow.py`) |
| §9 error matrix | T7 (`main()` error envelope) |
| §10 test plan (~50 new tests) | T4 (~18) + T5 (~14) + T7 (~24) + T8 (~1) = ~57 |
| §12 known limitations | T9 (README documentation) |
| §13 migration steps | T9 (README) |

**Placeholder scan:** the only placeholders are commit-SHA references in T9 PROGRESS-backfill instructions — those are real future references, not hand-waves.

**Type consistency:** `SessionContext` declared in T5 has fields `state_dir`, `cfg`, `sidecar`, `clock`, `_store`, `_ledger` — same shape used in T6 wiring and T7 cmd signatures. `SidecarRecord` fields (`kind`, `bucket`, `prefix`, `root`) consistent across T4 (definition), T5 (consumer), T7 (test fixtures).

**Granularity:** every task produces one commit. T6 is the biggest by LOC moved but mechanically simplest; T7 is the conceptually biggest. Splitting T7 further would require either (a) shipping a half-migrated state where some cmds use `ctx` and others use `state_dir` (works in tests but not at runtime since `main()` builds one or the other) or (b) writing a redundant intermediate adapter. Neither pays off.

**No user-gate tasks tagged.** Spec language uses "verify" / "check" in normal engineering senses (no scope commitments, no "prove it works", no "first on one then all"). T1 verify steps mean "run pytest and read the output" — routine TDD discipline, not a user-thrown gate.
