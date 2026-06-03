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
    fake_boto3 = types.SimpleNamespace(client=lambda _: FakeS3Client())
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

    from kinoforge import cli as _cli

    cli_src = Path(_cli.__file__).read_text()

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
