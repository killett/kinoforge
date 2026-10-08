"""`kinoforge upscale --image-dir`: surface, refusals, dry run, summary, exit codes.

Everything below the preflight patches ``kinoforge.core.upscale_dir.upscale_image_dir``
so no pod is ever provisioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.cli.pod_health import probe_pod_health
from kinoforge.core.image_dir import ImageDirItem, ImageDirPlan
from kinoforge.core.interfaces import Instance
from kinoforge.core.upscale_dir import ImageDirResult, ItemOutcome, PodDead

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


def _cfg(
    tmp_path: Path, *, scale: str = "2x", extra: str = "", engine: str = "spandrel"
) -> Path:
    cfg = tmp_path / "cfg.yaml"
    block = (
        "  spandrel:\n    model_url: hf:foo/bar.pth\n    arch: realesrgan\n"
        "    precision: fp16\n    tile_size: 512\n    batch_size: 4\n"
        if engine == "spandrel"
        else "  seedvr2:\n    variant: 3B\n    precision: fp8\n"
    )
    cfg.write_text(
        _HEAD + f"upscale:\n  engine: {engine}\n  scale: {scale}\n" + extra + block
    )
    return cfg


def _photos(
    tmp_path: Path, names: tuple[str, ...] = ("a.png", "sub/b.webp", "c.jpg")
) -> Path:
    src = tmp_path / "photos"
    for n in names:
        p = src / n
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 6), 0).save(p)
    return src


def _argv(tmp_path: Path, src: Path, *more: str) -> list[str]:
    return [
        "upscale",
        "--image-dir",
        str(src),
        "-c",
        str(_cfg(tmp_path)),
        "--state-dir",
        str(tmp_path / ".kf"),
        *more,
    ]


@pytest.fixture
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("upscale_image_dir must not be reached")

    monkeypatch.setattr("kinoforge.core.upscale_dir.upscale_image_dir", boom)


def _fake_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_names: set[str] | None = None,
    raise_exc: BaseException | None = None,
    instance: Instance | None = None,
) -> dict[str, Any]:
    fail_names = fail_names if fail_names is not None else set()
    captured: dict[str, Any] = {}

    def fake(
        cfg: Any, plan: ImageDirPlan, **kw: Any
    ) -> tuple[ImageDirResult, Instance | None]:
        captured["plan"] = plan
        captured.update(kw)
        outcomes: list[tuple[ImageDirItem, ItemOutcome, str | None]] = []
        for item in plan.pending:
            outcome: ItemOutcome
            reason: str | None
            if item.source.name in fail_names:
                outcome, reason = "failed", "RuntimeError: pod said no"
            else:
                outcome, reason = "written", None
            kw["on_item"](item, outcome, reason)
            outcomes.append((item, outcome, reason))
        if raise_exc is not None:
            raise raise_exc
        return ImageDirResult(plan, tuple(outcomes)), instance

    monkeypatch.setattr("kinoforge.core.upscale_dir.upscale_image_dir", fake)
    return captured


class TestArgparse:
    def test_mutually_exclusive_with_video_and_image(self, tmp_path: Path) -> None:
        # Bug caught: two sources accepted; the handler picks one silently.
        with pytest.raises(SystemExit) as exc:
            main(
                [
                    "upscale",
                    "--video",
                    "a.mp4",
                    "--image-dir",
                    "d",
                    "-c",
                    str(_cfg(tmp_path)),
                ]
            )
        assert exc.value.code == 2


class TestRefusals:
    @pytest.mark.parametrize(
        ("value", "needle"),
        [("", "empty"), ("/nonexistent/dir", "does not exist")],
    )
    def test_bad_path_exits_2(
        self,
        tmp_path: Path,
        no_runner: None,
        capsys: pytest.CaptureFixture[str],
        value: str,
        needle: str,
    ) -> None:
        # Bug caught: an empty or nonexistent --image-dir reaches
        # upscale_image_dir and boots a pod before anyone notices the typo.
        rc = main(
            [
                "upscale",
                "--image-dir",
                value,
                "-c",
                str(_cfg(tmp_path)),
                "--state-dir",
                str(tmp_path / ".kf"),
            ]
        )
        assert rc == 2
        assert needle in capsys.readouterr().err

    def test_file_instead_of_dir_exits_2(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a file passed as --image-dir is treated as a directory
        # and crashes deep inside the walk instead of refusing up front.
        f = tmp_path / "x.png"
        Image.new("RGB", (4, 4)).save(f)
        rc = main(_argv(tmp_path, f))
        assert rc == 2 and "not a directory" in capsys.readouterr().err

    def test_engine_without_image_support_exits_2(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a SeedVR2 cfg boots a pod and the server 400s image jobs.
        src = _photos(tmp_path)
        rc = main(
            [
                "upscale",
                "--image-dir",
                str(src),
                "-c",
                str(_cfg(tmp_path, engine="seedvr2")),
                "--state-dir",
                str(tmp_path / ".kf"),
            ]
        )
        assert rc == 2 and "seedvr2" in capsys.readouterr().err

    @pytest.mark.parametrize("extra", ["  chunk_frames: 16\n", "  tile_grid: [2, 1]\n"])
    def test_video_splits_exit_2(
        self,
        tmp_path: Path,
        no_runner: None,
        capsys: pytest.CaptureFixture[str],
        extra: str,
    ) -> None:
        # Bug caught: chunk_frames / tile_grid (video-only knobs) silently
        # apply to a directory of stills instead of being refused.
        src = _photos(tmp_path)
        rc = main(
            [
                "upscale",
                "--image-dir",
                str(src),
                "-c",
                str(_cfg(tmp_path, extra=extra)),
                "--state-dir",
                str(tmp_path / ".kf"),
            ]
        )
        assert rc == 2 and extra.split(":")[0].strip() in capsys.readouterr().err

    def test_height_scale_exits_2(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: only --image's height-scale guard is checked; a
        # directory run falls through and crashes deep on the pod.
        src = _photos(tmp_path)
        rc = main(_argv(tmp_path, src, "--scale", "1080p"))
        assert rc == 2 and "1080p" in capsys.readouterr().err

    def test_fractional_scale_exits_2(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a fractional factor like 1.5x truncates to 1 via
        # int(scale.value) in both the plan's arithmetic and the megapixel
        # guard, silently upscaling at 1x while claiming 1.5x.
        src = _photos(tmp_path)
        rc = main(_argv(tmp_path, src, "--scale", "1.5x"))
        assert rc == 2 and "1.5" in capsys.readouterr().err

    def test_no_images_exits_2(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: an empty (wrong) directory boots a pod for nothing.
        src = tmp_path / "photos"
        src.mkdir()
        (src / "notes.txt").write_text("x")
        rc = main(_argv(tmp_path, src))
        err = capsys.readouterr().err
        assert rc == 2 and "no images" in err and str(src.resolve()) in err


class TestDryRun:
    def test_prints_plan_and_never_runs(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a dry run that boots a pod.
        src = _photos(tmp_path)
        out = src.parent / "photos_upscaled"
        out.mkdir()
        Image.new("RGB", (4, 4)).save(out / "a.png")
        rc = main(_argv(tmp_path, src, "--dry-run"))
        o = capsys.readouterr().out
        assert rc == 0
        assert (
            f"source_dir: {src.resolve()}" in o and f"output_dir: {out.resolve()}" in o
        )
        assert "media: image" in o
        assert "a.png -> a.png [exists]" in o
        assert "sub/b.webp -> sub/b.png [pending]" in o
        assert (
            "found 3: pending 2, exists 1, oversize 0, unreadable 0; non-image skipped 0"
            in o
        )
        assert not (out / "sub").exists()


class TestNothingToDo:
    def test_all_outputs_present_exits_0(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a fully-cached directory still boots a pod it never
        # needed, and a no-op run leaves a stray ledger behind.
        src = _photos(tmp_path, ("a.png",))
        out = src.parent / "photos_upscaled"
        out.mkdir()
        Image.new("RGB", (4, 4)).save(out / "a.png")
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 0 and "nothing to do" in capsys.readouterr().out
        assert not (tmp_path / ".kf" / "ledger.json").exists()

    def test_plan_failures_only_exit_1(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a directory of nothing but oversize files reports success.
        src = tmp_path / "photos"
        src.mkdir()
        Image.new("RGB", (600, 600), 0).save(
            src / "a.png"
        )  # 600·600·4 = 1.44 MP > cap 1
        cfg = _cfg(tmp_path, extra="  max_output_megapixels: 1\n")
        rc = main(
            [
                "upscale",
                "--image-dir",
                str(src),
                "-c",
                str(cfg),
                "--state-dir",
                str(tmp_path / ".kf"),
            ]
        )
        o = capsys.readouterr().out
        assert rc == 1
        assert "nothing to do" in o
        assert (
            "failed 1" in o and "failed: a.png" in o and "max_output_megapixels=1" in o
        )


class TestRun:
    def test_clean_run_prints_progress_and_exits_0(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: progress lines report the wrong per-item
        # numerator/denominator or lose walk order against the plan.
        src = _photos(tmp_path)
        cap = _fake_runner(monkeypatch)
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        o = capsys.readouterr().out
        assert rc == 0
        # Walk order: plan sorts entries by name at each level, subdirectories
        # interleaved among files — a.png, c.jpg, then sub/b.webp.
        assert "[1/3] a.png -> a.png (8x6 -> 16x12)" in o
        assert "[2/3] c.jpg -> c.png (8x6 -> 16x12)" in o
        assert "[3/3] sub/b.webp -> sub/b.png (8x6 -> 16x12)" in o
        assert (
            f"upscaled 3, skipped 0 existing, failed 0, aborted 0 -> {src.resolve()}_upscaled"
            in o
        )
        assert cap["single"] is True and cap["instance"] is None

    def test_scale_override_reaches_the_runner(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: --scale 4x plans/prints at 4x but the runner re-derives
        # cfg.upscale.scale (2x in the fixture cfg) for the actual stage, so
        # the pod renders at a different factor than the plan promised.
        src = _photos(tmp_path)
        cap = _fake_runner(monkeypatch)
        rc = main(_argv(tmp_path, src, "--no-reuse", "--scale", "4x"))
        o = capsys.readouterr().out
        assert rc == 0
        assert cap["scale"].value == 4
        assert "(8x6 -> 32x24)" in o

    def test_runner_receives_the_cli_health_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: core.upscale_dir must never import an adapter
        # namespace (kinoforge.engines), so its health_probe default is
        # `None` — dead-pod detection only works when the CLI explicitly
        # injects the concrete probe. If this call site drops the kwarg,
        # every directory run silently loses dead-pod detection.
        src = _photos(tmp_path)
        cap = _fake_runner(monkeypatch)
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        capsys.readouterr()
        assert rc == 0
        assert cap["health_probe"] is probe_pod_health

    def test_one_failure_exits_1_and_names_it(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: a failure reported as success.
        src = _photos(tmp_path)
        _fake_runner(monkeypatch, fail_names={"b.webp"})
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        o = capsys.readouterr().out
        assert rc == 1
        assert "[3/3] sub/b.webp FAILED: RuntimeError: pod said no" in o
        assert "upscaled 2, skipped 0 existing, failed 1, aborted 0" in o
        assert "failed: sub/b.webp" in o

    def test_pod_dead_prints_summary_then_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: a dead pod mid-run is reported as a clean exit instead
        # of surfacing PodDead and keeping the summary of what did complete.
        src = _photos(tmp_path)
        _fake_runner(
            monkeypatch, raise_exc=PodDead("pod pod-1 stopped answering /health")
        )
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        cap = capsys.readouterr()
        assert rc == 1
        assert "upscaled 3," in cap.out  # the fake emitted all three before raising
        assert "error: PodDead: pod pod-1" in cap.err

    def test_cancelled_prints_and_exits_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: a cancelled run exits 0, or the cancellation is
        # swallowed instead of being surfaced on stderr.
        from kinoforge.core.errors import Cancelled

        src = _photos(tmp_path)
        _fake_runner(monkeypatch, raise_exc=Cancelled("stop"))
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 1 and "upscale: cancelled" in capsys.readouterr().err

    def test_output_dir_flag_is_ignored_with_note(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Bug caught: files silently landing in output/.
        src = _photos(tmp_path)
        _fake_runner(monkeypatch)
        other = tmp_path / "elsewhere"
        rc = main(_argv(tmp_path, src, "--no-reuse", "--output-dir", str(other)))
        cap = capsys.readouterr()
        assert rc == 0
        assert "--output-dir" in cap.err and "ignored" in cap.err
        assert not other.exists()


class TestLaunchRow:
    def test_cold_create_stamps_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: the directory path forgets to record the pod, so the
        # next invocation cannot warm-attach to it.
        src = _photos(tmp_path)
        pod = Instance(id="pod-9", provider="fake", status="ready", created_at=0.0)
        _fake_runner(monkeypatch, instance=pod)
        stamped: list[Any] = []
        settled: list[Any] = []

        def _fake_stamp(ctx: Any, cfg: Any, inst: Any, **kw: Any) -> str:
            stamped.append(inst)
            return "pod-9"

        monkeypatch.setattr(
            "kinoforge.cli._commands._stamp_cold_created_instance", _fake_stamp
        )
        monkeypatch.setattr(
            "kinoforge.cli._commands._settle_unused_launch_row",
            lambda *a: settled.append(a),
        )
        monkeypatch.setattr(
            "kinoforge.cli._commands._scan_warm_candidates",
            lambda ctx, cfg: (
                None,
                type("R", (), {"summarize": lambda self: "none"})(),
            ),
        )
        rc = main(_argv(tmp_path, src))  # warm-reuse default: no --no-reuse
        assert rc == 0 and stamped == [pod] and settled == []

    def test_no_reuse_settles_not_stamps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: --no-reuse still stamps the ledger, stranding a row
        # for a pod that was actually destroyed at exit.
        src = _photos(tmp_path)
        _fake_runner(monkeypatch, instance=None)
        stamped: list[Any] = []
        settled: list[Any] = []
        monkeypatch.setattr(
            "kinoforge.cli._commands._stamp_cold_created_instance",
            lambda *a, **kw: stamped.append(a),
        )
        monkeypatch.setattr(
            "kinoforge.cli._commands._settle_unused_launch_row",
            lambda *a: settled.append(a),
        )
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 0 and stamped == [] and len(settled) == 1

    def test_single_image_path_uses_the_shared_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: a fourth copy of the stamp/settle block drifting.
        from kinoforge.core.interfaces import Artifact

        png = tmp_path / "in.png"
        Image.new("RGB", (8, 6)).save(png)
        calls: list[Any] = []
        monkeypatch.setattr(
            "kinoforge.core.orchestrator.generate",
            lambda *a, **kw: (Artifact(uri="file:///out"), None),
        )
        monkeypatch.setattr(
            "kinoforge.cli._commands._finish_launch_row",
            lambda *a, **kw: calls.append((a, kw)),
        )
        rc = main(
            [
                "upscale",
                "--image",
                str(png),
                "-c",
                str(_cfg(tmp_path)),
                "--state-dir",
                str(tmp_path / ".kf"),
                "--no-reuse",
            ]
        )
        assert rc == 0 and len(calls) == 1 and calls[0][1]["no_reuse"] is True
