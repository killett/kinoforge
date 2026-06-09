"""Pre-flight ``EPHEMERAL_CAPABILITIES`` gate at CLI entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.cli import main


def _write_runway_yaml(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "runway.yaml"
    cfg_path.write_text(
        "engine:\n"
        "  kind: runway\n"
        "  precision: ''\n"
        "spec:\n  model: gen4.5\n  mode: t2v\n"
        "models:\n  - ref: 'synthetic:runway-hosted'\n    kind: base\n    target: checkpoints\n"
        "lifecycle:\n  budget: 1.5\n",
    )
    return cfg_path


def _write_replicate_yaml(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "replicate.yaml"
    cfg_path.write_text(
        "engine:\n"
        "  kind: replicate\n"
        "  precision: ''\n"
        "spec:\n  model: bytedance/seedance-1-lite\n  mode: t2v\n"
        "models:\n  - ref: 'synthetic:replicate-hosted'\n    kind: base\n    target: checkpoints\n"
        "lifecycle:\n  budget: 1.5\n",
    )
    return cfg_path


def _write_fal_yaml(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "fal.yaml"
    cfg_path.write_text(
        "engine:\n"
        "  kind: fal\n"
        "  precision: ''\n"
        "  fal:\n"
        "    endpoint: 'fal-ai/wan-t2v'\n"
        "    queue_base: 'https://queue.fal.run'\n"
        "    api_key_env: 'FAL_KEY'\n"
        "    url_path: video.url\n"
        "models:\n  - ref: 'synthetic:fal-hosted'\n    kind: base\n    target: checkpoints\n"
        "lifecycle:\n  budget: 5.0\n",
    )
    return cfg_path


def test_ephemeral_fal_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--ephemeral + engine=fal → exit 2 with spec §11.4 block.

    Would-fail-bug: a permissive pre-flight would let an ephemeral run
    against fal proceed and leave the prompt-laden request_id on fal's
    dashboard after the artifact downloaded.
    """
    cfg = _write_fal_yaml(tmp_path)
    state = tmp_path / "state"
    rc = main(
        [
            "--state-dir",
            str(state),
            "--ephemeral",
            "generate",
            "-c",
            str(cfg),
            "--prompt",
            "x",
            "--mode",
            "t2v",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "fal" in err
    assert "ephemeral" in err.lower()
    assert "DELETE" in err  # spec block names the cleanup verb


def _preflight_for_config(tmp_path: Path, cfg_path: Path) -> str | None:
    """Build a ``SessionContext`` from ``cfg_path`` and call the pre-flight."""
    from kinoforge.cli._main import _preflight_ephemeral
    from kinoforge.cli.context import SessionContext

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    return _preflight_ephemeral(ctx)


def test_ephemeral_replicate_passes_preflight(tmp_path: Path) -> None:
    """``--ephemeral`` + engine=replicate → pre-flight returns ``None``.

    Drives the pre-flight directly rather than the whole CLI so we don't
    require the ``replicate`` SDK to be installed for this test to pass.

    Would-fail-bug: a default-False capability table would refuse the
    very providers the spec lists as confidentiality-supported.
    """
    cfg = _write_replicate_yaml(tmp_path)
    assert _preflight_for_config(tmp_path, cfg) is None


def test_ephemeral_runway_passes_preflight(tmp_path: Path) -> None:
    """``--ephemeral`` + engine=runway → pre-flight returns ``None``.

    Would-fail-bug: a flipped capability bit would refuse runway despite
    its concrete ``_delete`` impl from Task 17.
    """
    cfg = _write_runway_yaml(tmp_path)
    assert _preflight_for_config(tmp_path, cfg) is None


def test_preflight_fal_returns_error_block(tmp_path: Path) -> None:
    """``--ephemeral`` + engine=fal → pre-flight returns a non-empty block.

    Belt-and-suspenders for the CLI-integration test
    (``test_ephemeral_fal_refused``); locks the unit-level contract so
    future refactors of ``_preflight_ephemeral``'s return shape are
    caught explicitly.

    Would-fail-bug: returning the empty string from the refused branch
    would let ``main()``'s ``if err_block is not None`` always-truthy
    gate fire on the empty string too (Python truthiness pitfall).
    """
    cfg = _write_fal_yaml(tmp_path)
    block = _preflight_for_config(tmp_path, cfg)
    assert block is not None
    assert "fal" in block
    assert "DELETE" in block


def test_ephemeral_readonly_subcommand_emits_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--ephemeral + ``list`` → stderr note, no error, no pre-flight check.

    Would-fail-bug: running the cfg-aware pre-flight on a read-only
    subcommand would refuse list/status/stop on hosted-fal configs even
    though those commands never touch the engine.
    """
    state = tmp_path / "state"
    main(["--state-dir", str(state), "--ephemeral", "list"])
    err = capsys.readouterr().err
    assert "--ephemeral has no effect on read-only subcommands" in err


def test_no_ephemeral_no_preflight_block(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--ephemeral``, fal+list does NOT print a pre-flight block.

    Drives ``list`` (read-only, no SDK import path) under a fal cfg —
    the pre-flight branch must remain dormant since the flag is unset.

    Would-fail-bug: a pre-flight that fired regardless of the flag would
    refuse every fal run, even non-ephemeral ones.
    """
    _write_fal_yaml(tmp_path)  # built to mirror the refused-config layout
    state = tmp_path / "state"
    # ``list`` doesn't accept --config, so use a non-cfg subcommand here.
    # The point is: no --ephemeral flag → no error block on stderr.
    main(["--state-dir", str(state), "list"])
    err = capsys.readouterr().err
    assert "--ephemeral is not supported" not in err
