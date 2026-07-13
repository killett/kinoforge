"""CLI command tests for `kinoforge generate --loras` (spec §11.3)."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_generate
from kinoforge.cli._main import _build_parser


def _make_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "config": "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml",
        "prompt": "test",
        "mode": "t2v",
        "run_id": None,
        "output_dir": None,
        "no_output_dir": False,
        "instance_id": None,
        "force_attach": False,
        "no_reuse": False,
        "skip_preflight": True,  # bypass validate_for_generate so resolver runs
        "dry_run_swap": False,
        "loras": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _ctx_with_cfg() -> MagicMock:
    ctx = MagicMock()
    ctx.cfg = MagicMock()
    return ctx


def test_no_loras_arg_resolver_not_called_from_cmd_generate() -> None:
    """When --loras absent, the CLI eager-resolver call is skipped entirely."""
    args = _make_args(loras=None)
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        with patch("kinoforge.cli._commands.parse_loras_heredoc") as mock_parse:
            try:
                _cmd_generate(args, _ctx_with_cfg())
            except Exception:
                pass
    assert mock_parse.call_count == 0
    assert mock_resolve.call_count == 0


def test_loras_arg_parsed_and_threaded_to_resolver() -> None:
    args = _make_args(loras="civitai:1234@5678 0.7 h")
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        try:
            _cmd_generate(args, _ctx_with_cfg())
        except Exception:
            pass
    assert mock_resolve.called
    kwargs = mock_resolve.call_args_list[0].kwargs
    cli_loras = kwargs.get("cli_loras")
    assert cli_loras is not None
    assert len(cli_loras) == 1
    assert cli_loras[0].ref == "civitai:1234@5678"
    assert cli_loras[0].strength == 0.7
    assert cli_loras[0].branch == "high_noise"


def test_loras_parse_error_renders_report_to_stderr_exit_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _make_args(loras="cvtai:1@1\nbroken")
    rc = _cmd_generate(args, _ctx_with_cfg())
    captured = capsys.readouterr()
    assert rc == 1
    assert "--loras:" in captured.err
    assert "unknown scheme" in captured.err or "missing scheme" in captured.err


def test_loras_double_use_argparse_errors_exit_2() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "generate",
                "-c",
                "x.yaml",
                "--prompt",
                "p",
                "--mode",
                "t2v",
                "--loras",
                "civitai:1@1",
                "--loras",
                "civitai:2@2",
            ]
        )
    assert exc.value.code == 2


def test_loras_empty_heredoc_threads_empty_list() -> None:
    """D9 — --loras "" overrides cfg with empty stack."""
    args = _make_args(loras="")
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        try:
            _cmd_generate(args, _ctx_with_cfg())
        except Exception:
            pass
    assert mock_resolve.called
    assert mock_resolve.call_args_list[0].kwargs.get("cli_loras") == []


def test_dry_run_swap_with_cli_loras_emits_cli_label(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--loras + --dry-run-swap path prints `loras_source: cli`."""
    from kinoforge.core.ephemeral import EphemeralSession

    args = _make_args(loras="civitai:1234@5678", dry_run_swap=True)
    with EphemeralSession(enabled=False):
        try:
            _cmd_generate(args, _ctx_with_cfg())
        except Exception:
            pass
    out = capsys.readouterr().out
    assert "loras_source: cli" in out


def test_dry_run_swap_with_no_cli_loras_no_vault_empty_cfg_emits_empty_label(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --loras, no vault.loras, no cfg.loras → loras_source: empty."""
    from kinoforge.core.ephemeral import EphemeralSession

    args = _make_args(loras=None, dry_run_swap=True)
    ctx = _ctx_with_cfg()
    ctx.cfg.loras = []  # explicit empty
    with EphemeralSession(enabled=False):
        try:
            _cmd_generate(args, ctx)
        except Exception:
            pass
    out = capsys.readouterr().out
    assert "loras_source: empty" in out


def test_dry_run_swap_with_cfg_loras_only_emits_cfg_label(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --loras, no vault, cfg.loras non-empty → loras_source: cfg."""
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.lora import LoraEntry as _LE

    args = _make_args(loras=None, dry_run_swap=True)
    ctx = _ctx_with_cfg()
    ctx.cfg.loras = [_LE(ref="civitai:cfg@1")]
    with EphemeralSession(enabled=False):
        try:
            _cmd_generate(args, ctx)
        except Exception:
            pass
    out = capsys.readouterr().out
    assert "loras_source: cfg" in out


def test_loras_help_includes_loras_arg() -> None:
    """--help must document --loras."""
    parser = _build_parser()
    # Locate the generate subparser by walking sub-actions.
    sub_help = ""
    for action in parser._actions:
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            gen = action.choices.get("generate")
            if gen is not None:
                sub_help = gen.format_help()
                break
    assert "--loras" in sub_help
    assert "HEREDOC" in sub_help
    # Sub-project A — operators should see that URL paste is supported.
    # argparse word-wraps the help string, so collapse whitespace before
    # asserting the canonical phrase is present.
    sub_help_collapsed = " ".join(sub_help.split())
    assert "URLs from civitai.com, civarchive.com, huggingface.co" in sub_help_collapsed
    assert "modelVersionId" in sub_help_collapsed
    assert "civarchive" in sub_help_collapsed
