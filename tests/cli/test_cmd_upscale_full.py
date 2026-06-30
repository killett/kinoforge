"""Tests for the _cmd_upscale full-run path (T11 wiring).

Replaces the prior NotYetImplementedError stub with the orchestrator
``generate(skip_clip_stage=True)`` invocation + symmetric ledger stamp.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.core.interfaces import Artifact


@pytest.fixture
def stub_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg.yaml"
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
        "  engine: spandrel\n"
        "  scale: 2x\n"
        "  spandrel:\n"
        "    model_url: hf:foo/bar.pth\n"
        "    arch: realesrgan\n"
        "    precision: fp16\n"
        "    tile_size: 512\n"
        "    batch_size: 4\n"
    )
    return cfg


def test_non_dry_run_invokes_generate_with_skip_flag(
    stub_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: _cmd_upscale still raises NotYetImplementedError on
    # the non-dry-run path. Asserts the wiring exists and calls
    # generate() with the right flags.
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

    rc = main(
        [
            "upscale",
            "--video",
            str(video),
            "--config",
            str(stub_cfg),
            "--no-reuse",
        ]
    )
    assert rc == 0
    assert captured["request"] is None
    assert captured["skip_clip_stage"] is True
    initial = captured["initial_clip"]
    assert initial is not None
    assert initial.sha256 == expected_sha
    assert initial.uri.startswith("file://")


def test_dry_run_path_unchanged(
    stub_cfg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Bug caught: the T11 wiring breaks the dry-run path so the existing
    # `--dry-run` operators see an exception instead of the plan summary.
    video = tmp_path / "in.mp4"
    video.write_bytes(b"x")
    rc = main(
        [
            "upscale",
            "--video",
            str(video),
            "--config",
            str(stub_cfg),
            "--dry-run",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "upscale plan:" in out


def test_upscaler_precision_tag_helper_spandrel() -> None:
    # Bug caught: helper hardcodes "fp16" or only inspects seedvr2 block,
    # so spandrel cfgs land an empty precision tag and the matcher cannot
    # diff-check warm pods by precision.
    from kinoforge.cli._commands import _upscaler_precision_tag
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "upscale": {
                "engine": "spandrel",
                "scale": "2x",
                "spandrel": {"model_url": "hf:x/y.pth", "precision": "fp32"},
            },
        }
    )
    assert _upscaler_precision_tag(cfg) == "fp32"


def test_upscaler_precision_tag_helper_seedvr2() -> None:
    # Bug caught: helper returns bare precision for seedvr2 too, losing
    # the variant qualifier the matcher needs to distinguish 3b-fp8 from
    # 7b-fp8 cached pods.
    from kinoforge.cli._commands import _upscaler_precision_tag
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "7B", "precision": "fp8"},
            },
        }
    )
    assert _upscaler_precision_tag(cfg) == "7b-fp8"


def test_upscaler_precision_tag_helper_no_upscale_block() -> None:
    # Bug caught: helper raises AttributeError on cfg without upscale block,
    # crashing _cmd_generate's ledger-stamp path for pure-t2v cfgs.
    from kinoforge.cli._commands import _upscaler_precision_tag
    from kinoforge.core.config import Config

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
        }
    )
    assert _upscaler_precision_tag(cfg) == ""
