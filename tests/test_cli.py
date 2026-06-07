"""Tests for the kinoforge CLI (Task 22).

Each test builds a throwaway ``local-fake.yaml`` in a ``tmp_path`` fixture and
uses an isolated state dir to avoid touching the caller's real ``.kinoforge/``.

Naming convention
-----------------
* ``cfg_path`` — the tmp YAML file
* ``state_dir`` — the tmp state directory (``--state-dir`` equivalent)
"""

from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOCAL_FAKE_YAML = textwrap.dedent("""\
    engine:
      kind: fake
      precision: fp16
    models:
      - ref: "http://example.com/model.safetensors"
        kind: base
        target: checkpoints
    compute:
      provider: local
      image: "kinoforge/local:latest"
      lifecycle:
        idle_timeout: 1h
        job_timeout: 30m
        time_buffer: 30m
        max_lifetime: 3h
        budget: 10.0
    """)


def _write_cfg(tmp_path: Path, content: str = _LOCAL_FAKE_YAML) -> Path:
    """Write config YAML to ``tmp_path/cfg.yaml`` and return the path."""
    p = tmp_path / "cfg.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _call(argv: list[str], state_dir: Path) -> int:
    """Call ``main(argv)`` with the given argv and an isolated state dir.

    Returns:
        The integer exit code (0 on success; non-zero on error).
    """
    from kinoforge.cli import main

    full_argv = ["--state-dir", str(state_dir), *argv]
    try:
        return main(full_argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 1


# ---------------------------------------------------------------------------
# AC1 — Dry-run plan
# ---------------------------------------------------------------------------


def test_dry_run_prints_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC1: dry-run prints engine, provider, key hash, offer count, lifecycle, model count.

    Zero calls to provider.create_instance.
    """
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    create_calls: list[object] = []

    import unittest.mock as mock

    from kinoforge.providers.local import LocalProvider

    original_create = LocalProvider.create_instance

    def spy_create(self: LocalProvider, spec: object) -> object:
        create_calls.append(spec)
        return original_create(self, spec)  # type: ignore[arg-type]

    with mock.patch.object(LocalProvider, "create_instance", spy_create):
        code = _call(
            ["deploy", "--config", str(cfg_path), "--dry-run"],
            state_dir,
        )

    assert code == 0
    out = capsys.readouterr().out
    # plan content
    assert "fake" in out
    assert "local" in out
    assert "offer" in out.lower()
    # zero real create_instance calls
    assert len(create_calls) == 0


# ---------------------------------------------------------------------------
# AC2 — End-to-end generate
# ---------------------------------------------------------------------------


def test_generate_produces_artifact(tmp_path: Path) -> None:
    """AC2: generate command produces a stored artifact via LocalProvider + FakeEngine."""
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    code = _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hi",
            "--mode",
            "t2v",
            "--run-id",
            "r1",
        ],
        state_dir,
    )

    assert code == 0
    # artifact should be stored
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(state_dir)
    items = store.list("r1")
    assert len(items) > 0, "expected at least one artifact in run r1"


# ---------------------------------------------------------------------------
# AC3 — GC subcommand
# ---------------------------------------------------------------------------


def test_gc_removes_run_artifacts(tmp_path: Path) -> None:
    """AC3: ``gc --run r1`` removes all artifacts for run r1."""
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    # first generate to populate the store
    code = _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
            "--run-id",
            "r1",
        ],
        state_dir,
    )
    assert code == 0

    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(state_dir)
    before = store.list("r1")
    assert len(before) > 0, "expected artifacts before gc"

    # run gc
    code = _call(["gc", "--config", str(cfg_path), "--run", "r1"], state_dir)
    assert code == 0

    after = store.list("r1")
    assert len(after) == 0, f"expected empty after gc; got {after}"


# ---------------------------------------------------------------------------
# AC4 — reap subcommand
# ---------------------------------------------------------------------------


def test_reap_destroys_stale_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: reap sweeps over-age instances from the ledger and prints destroyed ids."""
    state_dir = tmp_path / "state"

    # Manually inject a stale ledger entry
    from kinoforge.core.interfaces import Instance
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(state_dir)
    ledger = Ledger(store=store, run_id="_lifecycle")
    old_instance = Instance(
        id="local-stale123",
        provider="local",
        status="ready",
        created_at=time.time() - 100_000,  # very old
        cost_rate_usd_per_hr=0.0,
    )
    ledger.record(old_instance)

    # The reap subcommand needs a provider that reports this instance as live.
    # We mock LocalProvider.list_instances to include it.
    import unittest.mock as mock

    from kinoforge.providers.local import LocalProvider

    old_inst_copy = old_instance
    # Track calls so list_instances returns empty after first destroy call
    destroy_calls: list[str] = []

    def fake_list(self: LocalProvider) -> list[Instance]:
        # Return the instance only until destroy has been called for it
        if old_inst_copy.id in destroy_calls:
            return []
        return [old_inst_copy]

    def fake_destroy(self: LocalProvider, iid: str) -> None:
        destroy_calls.append(iid)

    capsys.readouterr()  # clear

    with (
        mock.patch.object(LocalProvider, "list_instances", fake_list),
        mock.patch.object(LocalProvider, "destroy_instance", fake_destroy),
    ):
        code = _call(["reap"], state_dir)

    assert code == 0
    out = capsys.readouterr().out
    assert (
        "local-stale123" in out or "destroyed" in out.lower() or "reaped" in out.lower()
    )


# ---------------------------------------------------------------------------
# AC5 — instance overview on every invocation
# ---------------------------------------------------------------------------


def test_instance_overview_on_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5: any subcommand prints the instance overview header with id and spend."""
    state_dir = tmp_path / "state"

    # Seed a ledger entry
    from kinoforge.core.interfaces import Instance
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(state_dir)
    ledger = Ledger(store=store, run_id="_lifecycle")
    inst = Instance(
        id="local-overview99",
        provider="local",
        status="ready",
        created_at=time.time() - 3600,
        cost_rate_usd_per_hr=1.5,
    )
    ledger.record(inst)

    capsys.readouterr()
    code = _call(["list"], state_dir)
    assert code == 0

    out = capsys.readouterr().out
    assert "local-overview99" in out
    # should contain a dollar sign (spend figure)
    assert "$" in out


# ---------------------------------------------------------------------------
# AC6 — unknown adapter exits non-zero
# ---------------------------------------------------------------------------


def test_unknown_engine_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC6: cfg with engine.kind: bogus causes CLI to exit non-zero with a clear message."""
    # Config with known engine kinds passes config validation,
    # so we need to bypass it by using a cfg that passes pydantic but
    # triggers UnknownAdapter at deploy time.
    # The config validator checks KNOWN_ENGINES, so we need to sneak past that.
    # We'll inject a fake engine via monkeypatching KNOWN_ENGINES.
    bogus_yaml = textwrap.dedent("""\
        engine:
          kind: bogus
          precision: fp16
        models:
          - ref: "http://example.com/model.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: local
          image: "kinoforge/local:latest"
          lifecycle:
            idle_timeout: 1h
            job_timeout: 30m
            time_buffer: 30m
            max_lifetime: 3h
            budget: 10.0
        """)
    cfg_path = _write_cfg(tmp_path, bogus_yaml)
    state_dir = tmp_path / "state"

    # Patch KNOWN_ENGINES in config to allow 'bogus' past validation
    import kinoforge.core.config as cfg_mod

    original_known = cfg_mod.KNOWN_ENGINES
    cfg_mod.KNOWN_ENGINES = original_known | {"bogus"}
    try:
        code = _call(["deploy", "--config", str(cfg_path)], state_dir)
    finally:
        cfg_mod.KNOWN_ENGINES = original_known

    assert code != 0
    combined = capsys.readouterr()
    err_out = combined.err + combined.out
    assert "bogus" in err_out.lower() or "unknown" in err_out.lower()


# ---------------------------------------------------------------------------
# AC7 — duplicate pod refused
# ---------------------------------------------------------------------------


def test_duplicate_pod_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC7: deploying when ledger has existing entry for same capability_key exits non-zero."""
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    # First deploy succeeds
    code = _call(["deploy", "--config", str(cfg_path)], state_dir)
    assert code == 0

    # Second deploy should be refused
    capsys.readouterr()
    code = _call(["deploy", "--config", str(cfg_path)], state_dir)
    assert code != 0

    combined = capsys.readouterr()
    err_out = combined.err + combined.out
    assert "duplicate" in err_out.lower()


# ---------------------------------------------------------------------------
# AC8 — --help exits 0
# ---------------------------------------------------------------------------


def test_help_exits_zero() -> None:
    """AC8: ``main(['--help'])`` raises SystemExit(0)."""
    from kinoforge.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Phase 13 / Layer C — store selection via config
# ---------------------------------------------------------------------------


def test_cli_generate_uses_local_when_store_block_absent(
    tmp_path: Path,
) -> None:
    """Absent store: block -> CLI uses LocalArtifactStore(state_dir).

    Bug this catches: _build_store regression breaks backwards compat for
    every config file written before Phase 13.
    """
    from kinoforge.cli import _build_store
    from kinoforge.core.config import Config, StoreConfig
    from kinoforge.stores.local import LocalArtifactStore

    cfg = Config.model_construct(store=StoreConfig())  # defaults: local, root=None
    store = _build_store(cfg, tmp_path)

    assert isinstance(store, LocalArtifactStore)
    assert store.root == tmp_path.resolve()


def test_cli_generate_uses_s3_when_store_kind_s3(tmp_path: Path) -> None:
    """store.kind='s3' -> _build_store returns an S3ArtifactStore.

    Bug this catches: _build_store branch missing or constructs with the
    wrong bucket/prefix arguments.
    """
    import sys
    import types

    from kinoforge.cli import _build_store
    from kinoforge.core.config import Config, StoreConfig
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    cfg = Config.model_construct(
        store=StoreConfig(kind="s3", bucket="my-bkt", prefix="some/prefix")
    )
    # _build_store doesn't inject client= — the lazy gate inside
    # S3ArtifactStore.__init__ would fire and import boto3. We satisfy
    # the import by putting a fake module in sys.modules under "boto3".
    fake_boto3 = types.SimpleNamespace(client=lambda *args, **kwargs: FakeS3Client())
    sys.modules["boto3"] = fake_boto3  # type: ignore[assignment]
    try:
        store = _build_store(cfg, tmp_path)
    finally:
        sys.modules.pop("boto3", None)

    assert isinstance(store, S3ArtifactStore)
    assert store.bucket == "my-bkt"
    assert store.prefix == "some/prefix"


def test_cli_gc_uses_store_uri_for_not_path_peek() -> None:
    """cli._cmd_gc calls store.uri_for(...) — never store._path(...) anymore.

    Bug this catches: Layer A's cleanup pattern was applied to JsonProfileCache
    but missed cli.py:441; this test pins the fix so a future refactor can't
    silently reintroduce the private-attr peek.
    """
    import re

    from kinoforge.cli import _commands as _cli_cmds

    cli_src = Path(_cli_cmds.__file__).read_text()

    # The private-attr peek must be gone.
    assert "store._path" not in cli_src, (
        "cli.py still calls store._path; "
        "Layer C should have replaced it with store.uri_for"
    )

    # And uri_for must be called somewhere in the file.
    assert re.search(r"\.uri_for\s*\(", cli_src), "cli.py never calls .uri_for(...)"


# ---------------------------------------------------------------------------
# .env loader integration (Task 3 of dotenv-secrets plan)
# ---------------------------------------------------------------------------


def test_cli_loads_env_from_cwd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() loads ./.env from cwd before subcommand dispatch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KINOFORGE_TEST_ENV_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "KINOFORGE_TEST_ENV_KEY=cwd-value\n", encoding="utf-8"
    )

    # `list` is a no-arg subcommand that exits 0 cleanly under empty state.
    code = _call(["list"], tmp_path / "state")
    assert code == 0

    assert os.environ.get("KINOFORGE_TEST_ENV_KEY") == "cwd-value"


def test_cli_env_file_flag_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--env-file PATH loads that file instead of the cwd default."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KINOFORGE_TEST_ENV_KEY", raising=False)

    # Default cwd .env that we should NOT load.
    (tmp_path / ".env").write_text(
        "KINOFORGE_TEST_ENV_KEY=cwd-value\n", encoding="utf-8"
    )

    # Explicit file we SHOULD load.
    custom = tmp_path / "custom.env"
    custom.write_text("KINOFORGE_TEST_ENV_KEY=custom-value\n", encoding="utf-8")

    code = _call(["--env-file", str(custom), "list"], tmp_path / "state")
    assert code == 0

    assert os.environ.get("KINOFORGE_TEST_ENV_KEY") == "custom-value"


def test_cli_env_file_missing_propagates_FileNotFoundError(
    tmp_path: Path,
) -> None:
    """--env-file PATH with a missing file raises FileNotFoundError through main()."""
    missing = tmp_path / "nope.env"

    with pytest.raises(FileNotFoundError, match=str(missing)):
        _call(["--env-file", str(missing), "list"], tmp_path / "state")


# ---------------------------------------------------------------------------
# Layer O Task 7 — --output-dir / --no-output-dir / --run-id uniquification
# ---------------------------------------------------------------------------


def test_cli_output_dir_flag_overrides_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: --output-dir /tmp/foo builds LocalOutputSink(dir=resolved /tmp/foo).

    Verifies the CLI passes sink=LocalOutputSink(dir=...) into generate().
    """
    from kinoforge.outputs.local import LocalOutputSink

    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"
    out_dir = tmp_path / "my-output"

    captured: dict[str, object] = {}

    def fake_generate(cfg: object, request: object, **kwargs: object) -> object:
        captured.update(kwargs)
        import types

        return types.SimpleNamespace(uri="fake://result"), None

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)

    code = _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
            "--output-dir",
            str(out_dir),
        ],
        state_dir,
    )

    assert code == 0
    sink = captured.get("sink")
    assert isinstance(sink, LocalOutputSink), f"expected LocalOutputSink; got {sink!r}"
    assert sink.dir == out_dir.resolve()


def test_cli_no_output_dir_disables_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: --no-output-dir produces sink=None."""
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    captured: dict[str, object] = {}

    def fake_generate(cfg: object, request: object, **kwargs: object) -> object:
        captured.update(kwargs)
        import types

        return types.SimpleNamespace(uri="fake://result"), None

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)

    code = _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
            "--no-output-dir",
        ],
        state_dir,
    )

    assert code == 0
    assert captured.get("sink") is None, (
        f"expected sink=None; got {captured.get('sink')!r}"
    )


def test_cli_default_output_dir_is_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: no flag, fresh tmp cwd -> sink rooted at cwd/output."""
    from kinoforge.outputs.local import LocalOutputSink

    monkeypatch.chdir(tmp_path)
    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    captured: dict[str, object] = {}

    def fake_generate(cfg: object, request: object, **kwargs: object) -> object:
        captured.update(kwargs)
        import types

        return types.SimpleNamespace(uri="fake://result"), None

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)

    code = _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
        ],
        state_dir,
    )

    assert code == 0
    sink = captured.get("sink")
    assert isinstance(sink, LocalOutputSink), f"expected LocalOutputSink; got {sink!r}"
    assert sink.dir == (tmp_path / "output").resolve()


def test_cli_output_dir_and_no_output_dir_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4: passing both --output-dir and --no-output-dir causes argparse error."""
    from kinoforge.cli import main

    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--state-dir",
                str(state_dir),
                "generate",
                "--config",
                str(cfg_path),
                "--prompt",
                "hello",
                "--mode",
                "t2v",
                "--output-dir",
                str(tmp_path / "foo"),
                "--no-output-dir",
            ]
        )

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    # argparse should mention the conflicting flags
    assert "--output-dir" in err and "--no-output-dir" in err


def test_cli_default_run_id_uniquifies_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: successive invocations without --run-id produce distinct run-YYYYMMDD-HHMMSS ids."""
    import re

    from kinoforge.core.clock import FakeClock

    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    fake_clock = FakeClock(start=1_000_000.0)  # 1970-01-12 13:46:40 UTC
    monkeypatch.setattr("kinoforge.cli._cli_clock", fake_clock)

    captured_run_ids: list[str] = []

    def fake_generate(cfg: object, request: object, **kwargs: object) -> object:
        captured_run_ids.append(str(kwargs.get("run_id", "")))
        import types

        return types.SimpleNamespace(uri="fake://result"), None

    monkeypatch.setattr("kinoforge.cli.generate", fake_generate)

    # First invocation at t=1_000_000
    _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
        ],
        state_dir,
    )

    # Advance clock so timestamps differ
    fake_clock.advance(2.0)

    # Second invocation at t=1_000_002
    _call(
        [
            "generate",
            "--config",
            str(cfg_path),
            "--prompt",
            "hello",
            "--mode",
            "t2v",
        ],
        state_dir,
    )

    assert len(captured_run_ids) == 2, f"expected 2 run_ids; got {captured_run_ids}"
    rid1, rid2 = captured_run_ids
    assert rid1 != rid2, f"run_ids should differ; both were {rid1!r}"

    # Each must match run-YYYYMMDD-HHMMSS
    pattern = re.compile(r"^run-\d{8}-\d{6}$")
    assert pattern.match(rid1), f"run_id {rid1!r} does not match run-YYYYMMDD-HHMMSS"
    assert pattern.match(rid2), f"run_id {rid2!r} does not match run-YYYYMMDD-HHMMSS"


# ---------------------------------------------------------------------------
# Layer S — _cmd_deploy threads lifecycle policy into Ledger.record
# ---------------------------------------------------------------------------


def test_cmd_deploy_persists_lifecycle_policy_into_ledger(
    tmp_path: Path,
) -> None:
    """`kinoforge deploy` records idle_timeout_s + max_age_s into the ledger.

    Bug-catch: a future refactor of _cmd_deploy that drops the kwargs would
    leave a silent gap — `kinoforge status` would fall back to
    `<not in ledger>` even when --config wasn't supplied.
    """
    import json as _json

    cfg_path = _write_cfg(tmp_path)
    state_dir = tmp_path / "state"

    rc = _call(["deploy", "--config", str(cfg_path)], state_dir)

    assert rc == 0
    ledger_path = state_dir / "_lifecycle" / "ledger.json"
    data = _json.loads(ledger_path.read_text())
    entries = data["entries"]
    assert len(entries) == 1
    entry = entries[0]
    # _LOCAL_FAKE_YAML carries lifecycle: idle_timeout: 1h, max_lifetime: 3h.
    # The persisted ledger key `max_age_s` mirrors the effective
    # `Lifecycle.max_lifetime_s` value at deploy time (Layer S spec naming).
    assert "idle_timeout_s" in entry
    assert "max_age_s" in entry
    assert isinstance(entry["idle_timeout_s"], int)
    assert isinstance(entry["max_age_s"], int)
    assert entry["idle_timeout_s"] == 3600  # 1h
    assert entry["max_age_s"] == 3 * 3600  # 3h


# ---------------------------------------------------------------------------
# Layer S — _build_ledger_block helper (pure)
# ---------------------------------------------------------------------------


def _legacy_entry() -> dict[str, object]:
    """Return a legacy-shape ledger entry (no idle_timeout_s / max_age_s)."""
    return {
        "id": "i-legacy",
        "provider": "runpod",
        "tags": {},
        "created_at": 1717635791.0,
        "cost_rate_usd_per_hr": 0.35,
    }


def _new_entry() -> dict[str, object]:
    """Return a Layer-S-shape ledger entry with lifecycle keys present."""
    e = _legacy_entry()
    e["id"] = "i-new"
    e["idle_timeout_s"] = 900
    e["max_age_s"] = 14400
    return e


def test_build_ledger_block_legacy_entry_no_cfg_shows_sentinel() -> None:
    """Legacy entry + no cfg → idle_timeout_s / max_age_s fall back to sentinel.

    Bug-catch: if the fallback chain skipped the sentinel and emitted "None",
    operators would see a confusing 'None' string with no actionable signal.
    """
    from kinoforge.cli import _build_ledger_block

    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    assert block["idle_timeout_s"] == "<not in ledger>"
    assert block["max_age_s"] == "<not in ledger>"


def test_build_ledger_block_legacy_entry_with_cfg_fills_from_lifecycle(
    tmp_path: Path,
) -> None:
    """Legacy entry + cfg → cfg.lifecycle() values fill the gap.

    Bug-catch: spec requires entry > cfg > sentinel precedence. If cfg won
    over entry, new-shape entries would silently lose their snapshot.
    """
    from kinoforge.cli import _build_ledger_block
    from kinoforge.core.config import load_config

    cfg_yaml = tmp_path / "cfg.yaml"
    cfg_yaml.write_text(Path("examples/configs/local-fake.yaml").read_text())
    cfg = load_config(cfg_yaml)

    block = _build_ledger_block(_legacy_entry(), cfg=cfg, now=1717635791.0)
    lc = cfg.lifecycle()

    # Note: ledger key `max_age_s` maps to Lifecycle attribute `max_lifetime_s`
    # at the cfg-side; the dict key here intentionally stays the spec-named
    # `max_age_s` (matches the ledger schema).
    assert block["idle_timeout_s"] == str(lc.idle_timeout_s)
    assert block["max_age_s"] == str(lc.max_lifetime_s)


def test_build_ledger_block_new_entry_ignores_cfg() -> None:
    """Entry-supplied idle / max win over cfg.

    Bug-catch: a future operator who edits the YAML lifecycle limits AFTER
    spinning a pod must still see the pod's snapshot, not the new YAML.
    """
    from kinoforge.cli import _build_ledger_block
    from kinoforge.core.config import load_config

    cfg_yaml = Path("examples/configs/local-fake.yaml")
    cfg = load_config(cfg_yaml)

    block = _build_ledger_block(_new_entry(), cfg=cfg, now=1717635791.0)

    assert block["idle_timeout_s"] == "900"
    assert block["max_age_s"] == "14400"


def test_build_ledger_block_clamps_negative_age_to_zero() -> None:
    """`created_at > now` (clock skew on resume) clamps age_h to 0.0.

    Bug-catch: negative age would propagate to a negative accrued_spend_usd
    and confuse the operator.
    """
    from kinoforge.cli import _build_ledger_block

    block = _build_ledger_block(
        _legacy_entry(),
        cfg=None,
        now=1717635791.0 - 3600.0,  # 1h before created_at
    )

    assert block["age_h"] == "0.0"
    assert block["accrued_spend_usd"] == "0.0000"


def test_build_ledger_block_created_at_is_local_iso8601() -> None:
    """`created_at` formatted via .astimezone().isoformat(timespec='seconds').

    Bug-catch: if the formatter used .utcfromtimestamp / .isoformat() without
    offset, operators in non-UTC zones would misread the timestamp.  CLAUDE.md
    + memory both insist on local timezone.
    """
    import re

    from kinoforge.cli import _build_ledger_block

    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
        block["created_at"],
    ), block["created_at"]


def test_build_ledger_block_surfaces_last_heartbeat_when_present() -> None:
    """`last_heartbeat` (when present in entry) is formatted like created_at.

    Bug-catch: spec is forward-compat — production doesn't persist this yet,
    but the surface must be wired so the operator-visible side lights up the
    moment a future layer starts persisting it.
    """
    import re

    from kinoforge.cli import _build_ledger_block

    entry = _legacy_entry()
    entry["last_heartbeat"] = 1717636791.0

    block = _build_ledger_block(entry, cfg=None, now=1717636791.0)

    assert "last_heartbeat" in block
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}",
        block["last_heartbeat"],
    ), block["last_heartbeat"]


def test_build_ledger_block_omits_last_heartbeat_when_absent() -> None:
    """No `last_heartbeat` key → block omits the field entirely.

    Bug-catch: emitting `last_heartbeat=None` would be a usability regression.
    """
    from kinoforge.cli import _build_ledger_block

    block = _build_ledger_block(_legacy_entry(), cfg=None, now=1717635791.0)

    assert "last_heartbeat" not in block


# ---------------------------------------------------------------------------
# Layer S — _cmd_status ledger-first dispatch
# ---------------------------------------------------------------------------


class _StatusFakeProvider:
    """Test double for ``kinoforge status`` provider dispatch.

    Per-test overrides of ``get_instance_impl`` and ``endpoints_impl`` let each
    test target a single branch in ``_cmd_status``. The fake intentionally does
    NOT inherit :class:`kinoforge.core.interfaces.ComputeProvider`; the registry
    only stores the factory (``Callable[[], ComputeProvider]``) and the runtime
    type annotation is not enforced — matching the
    ``tests/core/test_registry.py`` lambda pattern.
    """

    name = "fake-status"

    def __init__(self) -> None:
        from kinoforge.core.interfaces import Instance

        self.get_instance_impl = lambda iid: Instance(
            id=iid,
            provider=self.name,
            status="ready",
            endpoints={"http": "https://example/"},
            tags={},
            created_at=0.0,
            cost_rate_usd_per_hr=0.0,
        )
        self.endpoints_impl = lambda iid: {"http": "https://example/"}

    # ComputeProvider methods used by _cmd_status:
    def get_instance(self, instance_id):
        return self.get_instance_impl(instance_id)

    def endpoints(self, instance_id):
        return self.endpoints_impl(instance_id)


@pytest.fixture
def status_fake_provider():
    """Register a fake provider under ``fake-status``; yield it; tear down."""
    from kinoforge.core import registry

    inst = _StatusFakeProvider()
    # Lambda returns a duck-typed fake, not a ComputeProvider subclass;
    # registry only stores the factory and the runtime type is not enforced
    # (matches `tests/core/test_registry.py` `lambda: "P"` pattern).
    registry.register_provider("fake-status", lambda: inst)  # type: ignore[arg-type, return-value]
    try:
        yield inst
    finally:
        # The registry has no public unregister API; pop the private dict
        # entry so the registration does not leak across tests. Test-only
        # escape hatch (private module attr).
        registry._providers.pop("fake-status", None)


def _seed_ledger_with(tmp_path: Path, entry: dict[str, object]) -> Path:
    """Write a ledger.json under ``tmp_path/state/_lifecycle/`` and return state_dir."""
    import json as _json

    state_dir = tmp_path / "state"
    target = state_dir / "_lifecycle" / "ledger.json"
    target.parent.mkdir(parents=True)
    target.write_text(_json.dumps({"entries": [entry]}))
    return state_dir


def _runpod_entry(iid: str = "i-runpod") -> dict[str, object]:
    """Return a Layer-S-shape ledger entry pointing at the fake provider."""
    return {
        "id": iid,
        "provider": "fake-status",
        "tags": {},
        "created_at": 1717635791.0,
        "cost_rate_usd_per_hr": 0.35,
        "idle_timeout_s": 900,
        "max_age_s": 14400,
    }


def test_cmd_status_id_absent_from_ledger_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """status --id <unknown> → exit 1 + stderr 'not found in ledger'.

    Bug-catch: this is the ONE precondition for the entire status workflow.
    A regression here would let stale references silently succeed.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-existing"))

    rc = _call(["status", "--id", "i-missing"], state_dir)

    captured = capsys.readouterr()
    assert rc == 1
    assert "not found in ledger" in captured.err


def test_cmd_status_provider_success_prints_full_block_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Happy path: ledger entry + provider returns Instance → exit 0 + full block.

    Bug-catch: prior to Layer S the LocalProvider-only dispatch would always
    return 'not found' for any non-local entry. This lock proves the new
    dispatch path actually runs.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "provider_status=ready" in captured.out
    assert "endpoints=" in captured.out
    assert "provider=fake-status" in captured.out


def test_cmd_status_keyerror_prints_advisory_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Provider KeyError → stale ledger advisory + exit 0.

    Bug-catch: returning exit 1 here would block scripts that expect "the
    instance is gone, ledger is stale, move on" as a successful outcome.
    """

    def raise_keyerror(iid):
        raise KeyError(iid)

    status_fake_provider.get_instance_impl = raise_keyerror

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "stale ledger" in captured.out
    assert "advisory:" in captured.out
    assert "kinoforge forget --id i-runpod" in captured.out
    # Advisory printed exactly once
    assert captured.out.count("advisory:") == 1


def test_cmd_status_transient_error_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Provider raises non-KeyError → transient outcome → exit 2.

    Bug-catch: exit 0 here would mask real outages from monitoring scripts.
    """

    def raise_runtime(iid):
        raise RuntimeError("simulated network failure")

    status_fake_provider.get_instance_impl = raise_runtime

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 2
    assert "provider lookup failed: RuntimeError" in captured.out


def test_cmd_status_unknown_adapter_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ledger entry references a provider name not in the registry → exit 2.

    Bug-catch: silently returning 0 would hide broken installs.
    """
    entry = _runpod_entry()
    entry["provider"] = "this-provider-does-not-exist"
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown provider" in captured.out


def test_cmd_status_endpoints_raises_still_exit_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """``endpoints()`` raises while ``get_instance`` succeeds → endpoints=unknown(...) + exit 0.

    Bug-catch: ancillary endpoint discovery failure must not turn a healthy
    ``ready`` instance into an apparent outage.
    """

    def raise_endpoints(iid):
        raise RuntimeError("endpoint api down")

    status_fake_provider.endpoints_impl = raise_endpoints

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "provider_status=ready" in captured.out
    assert "endpoints=unknown (RuntimeError)" in captured.out


def test_cmd_status_output_is_alphabetised_key_value(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Stdout key=value lines are alphabetised; advisory (if any) is the last line.

    Bug-catch: unsorted output would break operator scripts that grep/awk by
    line number. The advisory line lacks ``=`` so it must NOT participate in
    the sort check.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    assert rc == 0
    out_lines = [
        ln
        for ln in capsys.readouterr().out.splitlines()
        if "=" in ln and not ln.startswith("[instance overview]")
    ]
    keys = [ln.split("=", 1)[0] for ln in out_lines]
    assert keys == sorted(keys), keys


def test_cmd_status_short_alias_dash_c_parses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """``status --id ID -c PATH`` parses (mirrors the documented quickstart).

    Bug-catch: argparse mis-wiring of the short alias would error before
    ``_cmd_status`` ever runs.
    """
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(Path("examples/configs/local-fake.yaml").read_text())
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry())

    rc = _call(
        ["status", "--id", "i-runpod", "-c", str(cfg_path)],
        state_dir,
    )

    assert rc == 0


def test_cmd_status_legacy_entry_with_cfg_fills_lifecycle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Legacy entry (no idle/max keys) + --config → cfg.lifecycle() fills the block.

    Bug-catch: legacy ledger entries from before Layer S would otherwise show
    '<not in ledger>' for every operator with a YAML on hand.
    """
    legacy = _runpod_entry()
    del legacy["idle_timeout_s"]
    del legacy["max_age_s"]
    state_dir = _seed_ledger_with(tmp_path, legacy)
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(Path("examples/configs/local-fake.yaml").read_text())

    rc = _call(
        ["status", "--id", "i-runpod", "--config", str(cfg_path)],
        state_dir,
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "idle_timeout_s=" in captured.out
    assert "<not in ledger>" not in captured.out


# ---------------------------------------------------------------------------
# Layer U T5 — last_heartbeat surface + sentinel-staleness advisory
# ---------------------------------------------------------------------------


def test_cmd_status_surfaces_last_heartbeat_when_present_in_entry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Layer U: ledger entry with last_heartbeat → status prints `last_heartbeat=<ISO>`.

    Half of the regression-guard pair (the omit-when-absent test below
    is the negative half). Together they pin down the Layer S
    forward-compat read path — if the write side (T1/T3) silently
    breaks, this test fails noisily.
    """
    import time as _time

    entry = _runpod_entry()
    entry["last_heartbeat"] = _time.time() - 5.0
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "last_heartbeat=" in captured.out
    # Sanity: ISO timestamp shape, not raw float.
    lines = [ln for ln in captured.out.splitlines() if ln.startswith("last_heartbeat=")]
    assert len(lines) == 1
    _, _, value = lines[0].partition("=")
    assert "T" in value, f"expected ISO timestamp with 'T', got {value!r}"


def test_cmd_status_omits_last_heartbeat_when_absent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """No last_heartbeat field → no last_heartbeat= line in the output.

    Negative half of the regression-guard pair. Catches a future
    refactor that emits the field unconditionally with an empty value.
    """
    entry = _runpod_entry()
    # Explicitly no last_heartbeat key.
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "last_heartbeat=" not in captured.out


def test_cmd_status_advisory_when_heartbeat_thread_tick_is_stale(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Stale sentinel (> 3 * interval) → 'heartbeat thread stale' advisory.

    Operator-visible signal of the Layer 2 crash-safety defense (Layer U
    spec §3.4). Without this advisory a silently-crashed loop looks
    identical to a quiet but healthy session, and a future
    heartbeat-aware reaper would have no way to gate on freshness.
    """
    import time as _time

    entry = _runpod_entry()
    entry["last_heartbeat"] = 0.0  # epoch zero — clearly old
    entry["heartbeat_thread_tick"] = 0.0  # > 3 * default 30s = 90s stale
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "advisory:" in captured.out
    assert "heartbeat thread stale" in captured.out
    # Sanity: advisory message includes the staleness amount.
    advisory_lines = [
        ln for ln in captured.out.splitlines() if "heartbeat thread stale" in ln
    ]
    assert len(advisory_lines) == 1
    # The age value scales with time since epoch — at least seconds-large.
    assert "since last tick" in advisory_lines[0]
    # The forget-style advisory line from KeyError must NOT appear here.
    assert "kinoforge forget" not in captured.out
    # And the test must actually have been "stale" relative to now.
    assert _time.time() - 0.0 > 90


def test_cmd_status_no_advisory_when_heartbeat_thread_tick_is_fresh(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status_fake_provider: _StatusFakeProvider,
) -> None:
    """Fresh sentinel → no advisory, regardless of last_heartbeat presence.

    Discriminating pair against the stale test: pins down the threshold
    semantics so a future refactor cannot silently widen or invert the
    comparison (e.g. always-advisory or never-advisory bug).
    """
    import time as _time

    now = _time.time()
    entry = _runpod_entry()
    entry["last_heartbeat"] = now - 1.0
    entry["heartbeat_thread_tick"] = now - 1.0  # well under 90s threshold
    state_dir = _seed_ledger_with(tmp_path, entry)

    rc = _call(["status", "--id", "i-runpod"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "heartbeat thread stale" not in captured.out
    assert "advisory:" not in captured.out


# ---------------------------------------------------------------------------
# Layer S — kinoforge forget --id <id>
# ---------------------------------------------------------------------------


def test_cmd_forget_removes_existing_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``forget --id ID`` removes the entry; stdout 'forgot: ID'; exit 0.

    Bug-catch: a no-op forget that returned 0 without mutating the ledger
    would leave operators chasing the same stale id forever.
    """
    import json as _json

    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-target"))

    rc = _call(["forget", "--id", "i-target"], state_dir)

    captured = capsys.readouterr()
    assert rc == 0
    assert "forgot: i-target" in captured.out
    on_disk = _json.loads((state_dir / "_lifecycle" / "ledger.json").read_text())
    # Ledger.forget rewrites as a dict-with-"entries" key.
    entries = on_disk["entries"] if isinstance(on_disk, dict) else on_disk
    assert all(e.get("id") != "i-target" for e in entries)


def test_cmd_forget_absent_id_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``forget --id <missing>`` → stderr 'not found in ledger'; exit 1.

    Bug-catch: silent success on absent ids would mask script bugs that
    pass a wrong instance id.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-other"))

    rc = _call(["forget", "--id", "i-missing"], state_dir)

    captured = capsys.readouterr()
    assert rc == 1
    assert "not found in ledger" in captured.err


def test_cmd_forget_is_non_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Second forget on the same id (post-success) returns exit 1.

    Bug-catch: idempotent ``forget`` would diverge from the design decision
    in spec §6 and from sibling commands ``stop`` / ``destroy``.
    """
    state_dir = _seed_ledger_with(tmp_path, _runpod_entry("i-twice"))

    rc1 = _call(["forget", "--id", "i-twice"], state_dir)
    rc2 = _call(["forget", "--id", "i-twice"], state_dir)

    assert rc1 == 0
    assert rc2 == 1
    captured = capsys.readouterr()
    # Second call's stderr surfaces the absent-entry message.
    assert "not found in ledger" in captured.err


def test_build_parser_registers_forget_with_id_flag() -> None:
    """``_build_parser()`` accepts ``forget --id ID`` → ``args.cmd == 'forget'``.

    Bug-catch: parser wiring is the only thing the dispatch in ``main()``
    relies on. A typo in the subparser name would route forget to argparse
    error before ``_cmd_forget`` ever runs.
    """
    from kinoforge.cli import _build_parser

    parser = _build_parser()
    ns = parser.parse_args(["forget", "--id", "i-foo"])

    assert ns.cmd == "forget"
    assert ns.id == "i-foo"
