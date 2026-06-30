"""Tests for the ``kinoforge upscale`` subcommand (T15).

CLI surface only — argparse plumbing, flag conflicts, and the
height-target ``--scale`` refusal that fires at startup before any
orchestrator work. The full warm-reuse / attach / cold-create path
is exercised live in T18/T19 against a real RunPod pod; unit-testing
the orchestration here would just duplicate ``test_cmd_generate.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.cli._main import main


def _stub_cfg(tmp_path: Path) -> Path:
    """Write a minimum cfg that survives ``Config.from_yaml`` validation."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "engine:\n"
        "  kind: diffusers\n"
        "  precision: fp8\n"
        "models:\n"
        "  - kind: base\n"
        "    ref: hf:Wan-AI/Wan2.2-T2V\n"
        "    target: diffusion_models\n"
        "compute:\n"
        "  provider: fake\n"
        "  image: fake:latest\n"
        "upscale:\n"
        "  engine: seedvr2\n"
        "  scale: 2x\n"
        "  seedvr2:\n"
        "    variant: 3B\n"
        "    precision: fp8\n"
    )
    return cfg


class TestArgparse:
    def test_missing_video_exits_2(self, tmp_path: Path) -> None:
        # Bug caught: ``--video`` not marked ``required=True`` on the
        # argparse spec → ``kinoforge upscale --config foo.yaml`` would
        # silently proceed with ``args.video = None`` and crash later
        # with an opaque AttributeError. argparse usage exit (2) lets
        # operators see the missing flag immediately.
        cfg = _stub_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "--config", str(cfg)])
        assert exc.value.code == 2

    def test_no_reuse_and_attach_pod_mutual_exclusion(self, tmp_path: Path) -> None:
        # Bug caught: the two flags express opposite intents (--no-reuse
        # forces cold-create-then-destroy; --attach-pod implies pod
        # survival) — accepting both lets the operator combine them into
        # a state the handler can't honor consistently. Mirror
        # _cmd_generate's same check so users get identical UX.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "upscale",
                "--video",
                "x.mp4",
                "--config",
                str(cfg),
                "--no-reuse",
                "--attach-pod",
                "abc",
            ]
        )
        assert rc == 2


class TestScaleRefusal:
    def test_height_target_refused_at_startup(self, tmp_path: Path) -> None:
        # Bug caught: ``--scale 1080p`` is a known v1 deferral
        # (NotYetImplementedError in SeedVR2Runtime). Refusing it at CLI
        # startup — BEFORE any pod is provisioned — saves the user a
        # live-spend cycle. Asserts the refusal fires before cfg / pod
        # work, otherwise the operator pays cold-boot cost for a job
        # that was always going to crash at inference time.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "upscale",
                "--video",
                "x.mp4",
                "--config",
                str(cfg),
                "--scale",
                "1080p",
                "--dry-run",
            ]
        )
        assert rc == 2

    def test_invalid_scale_token_refused(self, tmp_path: Path) -> None:
        # Bug caught: typo'd ``--scale 2`` (missing 'x') or any malformed
        # token slips through to runtime, surfacing as a confusing
        # ValueError in the upscale pipeline. CLI startup MUST catch
        # malformed scale tokens with a usage exit.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "upscale",
                "--video",
                "x.mp4",
                "--config",
                str(cfg),
                "--scale",
                "garbage",
                "--dry-run",
            ]
        )
        assert rc == 2


class TestDryRun:
    def test_dry_run_exits_zero_and_emits_plan(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: dry-run silently exits 0 with no output, defeating
        # its purpose ("show me the plan you would execute"). The plan
        # MUST surface the resolved scale + the source mp4 path so the
        # operator can sanity-check before paying for compute.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "upscale",
                "--video",
                "input.mp4",
                "--config",
                str(cfg),
                "--scale",
                "2x",
                "--dry-run",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "input.mp4" in captured.out
        assert "2x" in captured.out

    def test_dry_run_scale_override_takes_precedence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: CLI ``--scale`` flag silently ignored — the
        # handler reads cfg.upscale.scale unconditionally. Operator
        # then can't override the cfg from the command line for one-off
        # experiments without editing yaml. Asserts the CLI flag wins.
        cfg = _stub_cfg(tmp_path)
        # cfg ships with scale=2x; override to 4x and assert the
        # printed plan reflects 4x, not 2x.
        rc = main(
            [
                "upscale",
                "--video",
                "input.mp4",
                "--config",
                str(cfg),
                "--scale",
                "4x",
                "--dry-run",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "4x" in out
        # 2x must NOT appear as the chosen scale — cfg default got overridden.
        # (Allowing "2x" anywhere else in the plan output would leak the
        # ignored cfg value into operator-facing summary.)
        assert "scale=2x" not in out
