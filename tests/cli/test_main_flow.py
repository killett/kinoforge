"""End-to-end main() through cli.main([...]) — sidecar lifecycle + error paths.

Covers spec §8 scenarios: first-run sidecar write, mismatch error, migration
blocked, corrupt sidecar, config error, and no-subcommand with degraded cloud
state.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


_LOCAL_FAKE_YAML = (
    "engine:\n  kind: fake\n  precision: fp16\n"
    "models:\n  - kind: base\n    name: m\n    ref: fake://m\n    target: checkpoints\n"
    "compute:\n  provider: local\n  image: kinoforge/local:latest\n"
    "  lifecycle:\n    idle_timeout: 1h\n    job_timeout: 30m\n"
    "    time_buffer: 30m\n    max_lifetime: 3h\n    budget: 10.0\n"
)

_S3_FAKE_YAML_TEMPLATE = (
    "engine:\n  kind: fake\n  precision: fp16\n"
    "models:\n  - kind: base\n    name: m\n    ref: fake://m\n    target: checkpoints\n"
    "compute:\n  provider: local\n  image: kinoforge/local:latest\n"
    "  lifecycle:\n    idle_timeout: 1h\n    job_timeout: 30m\n"
    "    time_buffer: 30m\n    max_lifetime: 3h\n    budget: 10.0\n"
    "store:\n  kind: s3\n  bucket: {bucket}\n"
)


def _write_local_cfg(p: Path) -> Path:
    """Write a minimal local-store config and return its path."""
    cfg = p / "kf.yaml"
    cfg.write_text(_LOCAL_FAKE_YAML)
    return cfg


def _write_s3_cfg(p: Path, bucket: str = "kf-prod") -> Path:
    """Write a minimal s3-store config and return its path."""
    cfg = p / "kf-s3.yaml"
    cfg.write_text(_S3_FAKE_YAML_TEMPLATE.format(bucket=bucket))
    return cfg


# ---------------------------------------------------------------------------
# § spec §8.1 — first local deploy writes sidecar
# ---------------------------------------------------------------------------


def test_first_local_deploy_writes_sidecar(tmp_path: Path) -> None:
    """First cfg-bearing run with a local store creates store.json."""
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    rc = main(["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"])
    assert rc == 0
    assert (state / "store.json").exists()


def test_sidecar_contains_correct_kind(tmp_path: Path) -> None:
    """store.json written by a local-store cfg has kind='local'."""
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    main(["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"])
    data = json.loads((state / "store.json").read_text())
    assert data["kind"] == "local"


# ---------------------------------------------------------------------------
# §8.2 — matching sidecar is a no-op (idempotent)
# ---------------------------------------------------------------------------


def test_matching_sidecar_noop(tmp_path: Path) -> None:
    """Second cfg-bearing run with same cfg does not rewrite the sidecar."""
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"

    rc_1 = main(
        ["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"]
    )
    assert rc_1 == 0
    sidecar_path = state / "store.json"
    mtime_before = sidecar_path.stat().st_mtime_ns

    rc_2 = main(
        ["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"]
    )
    assert rc_2 == 0
    assert sidecar_path.stat().st_mtime_ns == mtime_before


# ---------------------------------------------------------------------------
# §8.3 — SidecarMismatch → exit 1 + "differs from sidecar" on stderr
# ---------------------------------------------------------------------------


def test_mismatch_errors_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Running a second cfg whose store differs from the sidecar errors cleanly."""
    from kinoforge.cli import main

    cfg_a = _write_local_cfg(tmp_path)
    cfg_b = _write_s3_cfg(tmp_path)
    state = tmp_path / "state"

    rc_a = main(
        ["--state-dir", str(state), "deploy", "--config", str(cfg_a), "--dry-run"]
    )
    assert rc_a == 0

    rc_b = main(
        ["--state-dir", str(state), "deploy", "--config", str(cfg_b), "--dry-run"]
    )
    captured = capsys.readouterr()
    assert rc_b == 1
    assert "differs from sidecar" in captured.err


# ---------------------------------------------------------------------------
# §8.4 — SidecarMigrationBlocked → exit 1 + "refusing to switch" on stderr
# ---------------------------------------------------------------------------


def test_migration_blocked_when_local_ledger_nonempty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cloud-store cfg is blocked while local ledger has entries."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    (state / "_lifecycle").mkdir(parents=True)
    (state / "_lifecycle" / "ledger.json").write_text(
        json.dumps({"entries": [{"id": "i-1"}]})
    )
    cfg = _write_s3_cfg(tmp_path)

    rc = main(["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "refusing to switch" in captured.err


# ---------------------------------------------------------------------------
# §8.5 — corrupt sidecar → exit 1 + "sidecar at ... rm to reset"
# ---------------------------------------------------------------------------


def test_corrupt_sidecar_returns_1_with_rm_advisory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt sidecar produces a clean error with removal advisory."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (state / "store.json").write_text("{not valid json")

    rc = main(["--state-dir", str(state), "list"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "sidecar at" in captured.err
    assert "rm to reset" in captured.err


# ---------------------------------------------------------------------------
# §8.6 — no-subcommand with cloud sidecar degrades gracefully
# ---------------------------------------------------------------------------


def test_no_subcommand_with_local_sidecar_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """kinoforge (no subcommand) with a local sidecar returns 0."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (state / "store.json").write_text(
        json.dumps({"kind": "local", "bucket": None, "prefix": "", "root": None})
    )

    rc = main(["--state-dir", str(state)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "instance overview" in captured.out.lower()


def test_no_subcommand_with_cloud_sidecar_still_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """kinoforge (no subcommand) with a cloud sidecar degrades gracefully."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    state.mkdir()
    (state / "store.json").write_text(
        json.dumps({"kind": "s3", "bucket": "nope", "prefix": "", "root": None})
    )

    rc = main(["--state-dir", str(state)])
    captured = capsys.readouterr()
    assert rc == 0
    # Overview may say "unavailable" if S3 SDK is missing, or "No running instances"
    # if SDK exists but the bucket is empty. Either is fine — the point is exit 0.
    assert (
        "unavailable" in captured.out
        or "No running instances" in captured.out
        or "instance overview" in captured.out.lower()
    )


# ---------------------------------------------------------------------------
# §8.7 — ConfigError / FileNotFoundError from load_config → exit 1
# ---------------------------------------------------------------------------


def test_config_file_not_found_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """deploy with a missing config path prints 'error: config:' and exits 1."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    missing = tmp_path / "does_not_exist.yaml"
    rc = main(
        ["--state-dir", str(state), "deploy", "--config", str(missing), "--dry-run"]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "error: config:" in captured.err


# ---------------------------------------------------------------------------
# §8.8 — list with local sidecar reads local store (no crash)
# ---------------------------------------------------------------------------


def test_list_with_local_sidecar_reads_local_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """list after a local deploy exits 0 and produces output."""
    from kinoforge.cli import main

    cfg = _write_local_cfg(tmp_path)
    state = tmp_path / "state"
    rc_setup = main(
        ["--state-dir", str(state), "deploy", "--config", str(cfg), "--dry-run"]
    )
    assert rc_setup == 0
    rc_list = main(["--state-dir", str(state), "list"])
    capsys.readouterr()  # consume output
    assert rc_list == 0


# ---------------------------------------------------------------------------
# §8.9 — forget removes an entry without --config
# ---------------------------------------------------------------------------


def test_forget_removes_entry_no_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """forget works without --config when sidecar points at local store."""
    from kinoforge.cli import main
    from kinoforge.cli.context import SessionContext
    from kinoforge.core.interfaces import Instance

    state = tmp_path / "state"
    # Seed a local-store sidecar + a ledger entry manually
    state.mkdir()
    (state / "store.json").write_text(
        json.dumps({"kind": "local", "bucket": None, "prefix": "", "root": None})
    )
    ctx = SessionContext(state_dir=state, cfg=None, sidecar=None)
    ctx.ledger().record(
        Instance(
            id="i-forget-flow",
            provider="local",
            status="ready",
            tags={},
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
        )
    )

    rc = main(["--state-dir", str(state), "forget", "--id", "i-forget-flow"])
    capsys.readouterr()
    assert rc == 0


# ---------------------------------------------------------------------------
# §8.10 — reap on empty state dir does not crash
# ---------------------------------------------------------------------------


def test_reap_on_empty_state_dir(tmp_path: Path) -> None:
    """reap on a fresh state dir exits 0."""
    from kinoforge.cli import main

    state = tmp_path / "state"
    rc = main(["--state-dir", str(state), "reap"])
    assert rc == 0


def test_dispatch_table_covers_every_argparse_subcommand() -> None:
    """Bug-catch: every parser subcommand must have a _DISPATCH entry.

    A future commit that wires a new subcommand into _build_parser but
    forgets to add the corresponding _cmd_ handler to _DISPATCH would
    raise KeyError at runtime when the user invokes the cmd. This test
    locks the two sets in sync.
    """
    from kinoforge.cli._main import _DISPATCH, _build_parser

    parser = _build_parser()
    # argparse exposes the subparsers via the dest's choices on the parent.
    sub_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    parser_cmds = set(sub_action.choices.keys())
    dispatch_cmds = set(_DISPATCH.keys())

    assert parser_cmds == dispatch_cmds, (
        f"parser/dispatch drift — parser has {parser_cmds - dispatch_cmds!r} "
        f"not in dispatch; dispatch has {dispatch_cmds - parser_cmds!r} not "
        f"in parser"
    )
