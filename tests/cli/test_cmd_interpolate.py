"""Tests for the ``kinoforge interpolate`` subcommand (plan Task 8).

CLI surface: argparse plumbing, flag conflicts, the missing-``interpolate:``
refusal, dry-run output, and the non-dry-run ``generate(skip_clip_stage=True)``
wiring with the ``--fps`` override. Mirrors tests/cli/test_cmd_upscale*.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.core.interfaces import Artifact


def _stub_cfg(tmp_path: Path) -> Path:
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
        "interpolate:\n"
        "  engine: rife\n"
        "  fps: 48.0\n"
        "  rife:\n"
        "    weights_ref: hf:kinoforge/rife\n"
        "    model: rife49\n"
        "    precision: fp16\n"
    )
    return cfg


def _cfg_no_interp(tmp_path: Path) -> Path:
    cfg = tmp_path / "noi.yaml"
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
    )
    return cfg


class TestArgparse:
    def test_missing_video_exits_2(self, tmp_path: Path) -> None:
        # Bug caught: --video not required -> args.video None -> opaque crash.
        cfg = _stub_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["interpolate", "--config", str(cfg)])
        assert exc.value.code == 2

    def test_no_reuse_and_attach_pod_mutual_exclusion(self, tmp_path: Path) -> None:
        # Bug caught: opposite-intent flags accepted together -> handler can't
        # honor both consistently.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "interpolate",
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


class TestInterpBlockRequired:
    def test_cfg_without_interpolate_block_exits_2(self, tmp_path: Path) -> None:
        # Bug caught: a cfg with no interpolate: block would reach the
        # orchestrator, append no stage, and silently return the input clip.
        cfg = _cfg_no_interp(tmp_path)
        rc = main(
            ["interpolate", "--video", "x.mp4", "--config", str(cfg), "--dry-run"]
        )
        assert rc == 2


class TestVideoArgValidation:
    def test_directory_video_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught (2026-07-06): a chained `--video "$(ls ...)"` that expands
        # empty resolves to cwd, and _resolve_input_video_as_artifact opened the
        # DIRECTORY -> opaque `IsADirectoryError` deep in provisioning. A local
        # --video that is not a regular file must fail fast with exit 2.
        cfg = _stub_cfg(tmp_path)
        rc = main(["interpolate", "--video", str(tmp_path), "--config", str(cfg)])
        assert rc == 2
        assert "not a file" in capsys.readouterr().err

    def test_missing_video_file_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "interpolate",
                "--video",
                str(tmp_path / "nope.mp4"),
                "--config",
                str(cfg),
            ]
        )
        assert rc == 2
        assert "does not exist" in capsys.readouterr().err

    def test_empty_video_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the exact 2026-07-06 trigger — a chained `--video "$(ls
        # ...)"` expanded to "" and crashed deep in provisioning.
        cfg = _stub_cfg(tmp_path)
        rc = main(["interpolate", "--video", "", "--config", str(cfg)])
        assert rc == 2
        assert "empty" in capsys.readouterr().err

    def test_http_video_not_file_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: file-checking an http(s):// source would reject valid
        # remote inputs (the pod fetches them). Remote URLs bypass the check.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "interpolate",
                "--video",
                "https://example.com/clip.mp4",
                "--config",
                str(cfg),
                "--dry-run",
            ]
        )
        assert rc == 0


class TestDryRun:
    def test_dry_run_emits_plan(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: dry-run exits 0 with no output, defeating its purpose.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            ["interpolate", "--video", "input.mp4", "--config", str(cfg), "--dry-run"]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "input.mp4" in out
        assert "48" in out  # cfg fps
        assert "rife" in out

    def test_dry_run_fps_override_takes_precedence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: CLI --fps silently ignored -> can't override cfg per-run.
        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "interpolate",
                "--video",
                "input.mp4",
                "--config",
                str(cfg),
                "--fps",
                "60",
                "--dry-run",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "60" in out


class TestNonDryRun:
    def test_non_dry_run_invokes_generate_with_skip_and_fps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: the non-dry-run path never reaches generate() with
        # skip_clip_stage=True, or drops the --fps override before the stage.
        video = tmp_path / "in.mp4"
        payload = b"dummy video bytes"
        video.write_bytes(payload)
        expected_sha = hashlib.sha256(payload).hexdigest()

        captured: dict[str, Any] = {}
        fake_artifact = Artifact(uri="file:///out.mp4", sha256="xx", size=1)

        def fake_generate(cfg: Any, request: Any, **kw: Any) -> Any:
            captured["cfg"] = cfg
            captured["request"] = request
            captured.update(kw)
            return (fake_artifact, None)

        monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)

        cfg = _stub_cfg(tmp_path)
        rc = main(
            [
                "interpolate",
                "--video",
                str(video),
                "--config",
                str(cfg),
                "--fps",
                "60",
                "--no-reuse",
            ]
        )
        assert rc == 0
        assert captured["request"] is None
        assert captured["skip_clip_stage"] is True
        # --fps override reaches the stage via cfg.interpolate.fps.
        assert captured["cfg"].interpolate.fps == 60.0
        initial = captured["initial_clip"]
        assert initial is not None
        assert initial.sha256 == expected_sha
        assert initial.uri.startswith("file://")
