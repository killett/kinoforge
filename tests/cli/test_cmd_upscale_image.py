"""`kinoforge upscale --image`: surface, preflight refusals, media stamp.

Every refusal here exits 2 BEFORE any pod work. The non-dry-run tests patch
``kinoforge.core.orchestrator.generate`` so nothing is provisioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.core.interfaces import Artifact

_HEAD = (
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


def _spandrel_cfg(tmp_path: Path, *, scale: str = "2x", extra: str = "") -> Path:
    cfg = tmp_path / "spandrel.yaml"
    cfg.write_text(
        _HEAD
        + "upscale:\n"
        + "  engine: spandrel\n"
        + f"  scale: {scale}\n"
        + extra
        + "  spandrel:\n"
        + "    model_url: hf:foo/bar.pth\n"
        + "    arch: realesrgan\n"
        + "    precision: fp16\n"
        + "    tile_size: 512\n"
        + "    batch_size: 4\n"
    )
    return cfg


def _seedvr2_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "seedvr2.yaml"
    cfg.write_text(
        _HEAD
        + "upscale:\n"
        + "  engine: seedvr2\n"
        + "  scale: 2x\n"
        + "  seedvr2:\n"
        + "    variant: 3B\n"
        + "    precision: fp8\n"
    )
    return cfg


def _png(tmp_path: Path, name: str = "in.png") -> Path:
    import imageio.v3 as iio

    p = tmp_path / name
    iio.imwrite(p, np.zeros((8, 8, 3), dtype=np.uint8))
    return p


@pytest.fixture
def no_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any reach into the orchestrator is a test failure."""

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("orchestrator.generate must not be reached")

    monkeypatch.setattr("kinoforge.core.orchestrator.generate", boom)


class TestArgparse:
    def test_video_and_image_are_mutually_exclusive(self, tmp_path: Path) -> None:
        # Bug caught: both accepted → the handler has two sources and picks
        # one silently.
        cfg = _spandrel_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "--video", "a.mp4", "--image", "b.png", "-c", str(cfg)])
        assert exc.value.code == 2

    def test_one_source_is_required(self, tmp_path: Path) -> None:
        cfg = _spandrel_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "-c", str(cfg)])
        assert exc.value.code == 2


class TestConfigRefusals:
    def test_engine_without_image_support_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a FlashVSR/SeedVR2 cfg boots a 10-minute pod and the
        # server then 400s the image request.
        cfg = _seedvr2_cfg(tmp_path)
        rc = main(
            ["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"]
        )
        err = capsys.readouterr().err
        assert rc == 2
        assert "seedvr2" in err and "--image" in err

    def test_chunk_frames_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _spandrel_cfg(tmp_path, extra="  chunk_frames: 16\n")
        rc = main(
            ["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"]
        )
        assert rc == 2
        assert "chunk_frames" in capsys.readouterr().err

    def test_tile_grid_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _spandrel_cfg(tmp_path, extra="  tile_grid: [2, 1]\n")
        rc = main(
            ["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"]
        )
        assert rc == 2
        assert "tile_grid" in capsys.readouterr().err

    def test_height_scale_in_cfg_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: only the --scale flag is height-checked; a cfg
        # `scale: 1080p` reaches spandrel's validate_spec after the boot.
        cfg = _spandrel_cfg(tmp_path, scale="1080p")
        rc = main(
            ["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"]
        )
        assert rc == 2
        assert "1080p" in capsys.readouterr().err

    def test_video_with_height_scale_is_still_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the new height refusal leaks onto --video, breaking
        # FlashVSR 1080p dry runs.
        cfg = _spandrel_cfg(tmp_path, scale="1080p")
        rc = main(["upscale", "--video", "x.mp4", "-c", str(cfg), "--dry-run"])
        assert rc == 0
        assert "media: video" in capsys.readouterr().out


class TestPathRefusals:
    def test_bad_suffix_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "in.gif"
        bad.write_bytes(b"GIF89a")
        rc = main(["upscale", "--image", str(bad), "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert ".gif" in capsys.readouterr().err

    def test_missing_file_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            [
                "upscale",
                "--image",
                str(tmp_path / "nope.png"),
                "-c",
                str(_spandrel_cfg(tmp_path)),
            ]
        )
        assert rc == 2
        assert "does not exist" in capsys.readouterr().err

    def test_directory_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(
            ["upscale", "--image", str(tmp_path), "-c", str(_spandrel_cfg(tmp_path))]
        )
        assert rc == 2
        assert "not a file" in capsys.readouterr().err

    def test_url_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: --video's http(s) passthrough is copied to --image and
        # the pod infers the kind from a URL it has not fetched.
        rc = main(
            [
                "upscale",
                "--image",
                "https://x/y.png",
                "-c",
                str(_spandrel_cfg(tmp_path)),
            ]
        )
        assert rc == 2
        assert "local file" in capsys.readouterr().err

    def test_empty_image_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: `--image ""` is read as "flag absent" and the run
        # silently becomes a VIDEO run that then complains about --video.
        rc = main(["upscale", "--image", "", "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert "--image is empty" in capsys.readouterr().err

    def test_empty_image_dry_run_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the empty-path check sits after the --dry-run block, so
        # `--image "" --dry-run` prints a plan (`media: video`) and exits 0.
        rc = main(
            ["upscale", "--image", "", "-c", str(_spandrel_cfg(tmp_path)), "--dry-run"]
        )
        assert rc == 2
        assert "--image is empty" in capsys.readouterr().err


class TestDryRun:
    def test_prints_media_and_source(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        png = _png(tmp_path)
        rc = main(
            [
                "upscale",
                "--image",
                str(png),
                "-c",
                str(_spandrel_cfg(tmp_path)),
                "--dry-run",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "media: image" in out
        assert f"source: {png}" in out


class TestMediaStamp:
    def _run(self, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_generate(cfg: Any, request: Any, **kw: Any) -> Any:
            captured.update(kw)
            return (Artifact(uri="file:///out", sha256="x", size=1), None)

        monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
        assert main(argv) == 0
        return captured

    def test_image_seeds_image_media(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: the flag parses, the preflight passes, and the seeded
        # artifact still says video — the pod gets a "video" job.
        png = _png(tmp_path)
        captured = self._run(
            [
                "upscale",
                "--image",
                str(png),
                "-c",
                str(_spandrel_cfg(tmp_path)),
                "--no-reuse",
            ],
            monkeypatch,
        )
        assert captured["initial_clip"].meta["media"] == "image"
        assert captured["initial_clip"].uri == f"file://{png.resolve()}"

    def test_video_seeds_video_media(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mp4 = tmp_path / "in.mp4"
        mp4.write_bytes(b"x")
        captured = self._run(
            [
                "upscale",
                "--video",
                str(mp4),
                "-c",
                str(_spandrel_cfg(tmp_path)),
                "--no-reuse",
            ],
            monkeypatch,
        )
        assert captured["initial_clip"].meta["media"] == "video"


class TestMegapixelGuard:
    def test_oversize_image_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: --image and --image-dir disagree on the cap; a 50 MP
        # still at 4x reaches the pod and kills it on host RAM.
        import imageio.v3 as iio

        big = tmp_path / "big.png"
        iio.imwrite(big, np.zeros((600, 600, 3), dtype=np.uint8))
        cfg = _spandrel_cfg(tmp_path, extra="  max_output_megapixels: 1\n")
        rc = main(["upscale", "--image", str(big), "-c", str(cfg), "--no-reuse"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "max_output_megapixels=1" in err and "600x600" in err

    def test_unreadable_image_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not a png")
        rc = main(
            [
                "upscale",
                "--image",
                str(bad),
                "-c",
                str(_spandrel_cfg(tmp_path)),
                "--no-reuse",
            ]
        )
        assert rc == 2 and "bad.png" in capsys.readouterr().err
