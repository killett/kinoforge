"""`upscale_image_dir`: one deploy_session, one UpscaleStage run per image."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

import kinoforge._adapters  # noqa: F401 — self-register upscalers
from kinoforge.core.config import Config
from kinoforge.core.errors import BudgetExceeded, Cancelled, KinoforgeError
from kinoforge.core.image_dir import plan_image_dir
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    UpscaleJob,
    UpscaleResult,
)
from kinoforge.core.orchestrator import DeploySession
from kinoforge.core.upscale_dir import PodDead, upscale_image_dir


def _cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {
                    "kind": "base",
                    "ref": "hf:Wan-AI/Wan2.2-T2V",
                    "target": "diffusion_models",
                }
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {
                "engine": "spandrel",
                "scale": "2x",
                "spandrel": {
                    "model_url": "hf:foo/bar.pth",
                    "arch": "realesrgan",
                    "precision": "fp16",
                    "tile_size": 512,
                    "batch_size": 4,
                },
            },
        }
    )


def _instance() -> Instance:
    return Instance(
        id="pod-1",
        provider="fake",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-1-8000.proxy.runpod.net"},
    )


class _Upscaler:
    """Fake UpscalerEngine: returns a file:// artifact of fixed bytes per call."""

    supports_image_input = True

    def __init__(
        self,
        tmp: Path,
        fail_on: AbstractSet[int] = frozenset(),
        raise_cls: type[BaseException] = RuntimeError,
    ) -> None:
        self.tmp = tmp
        self.calls: list[UpscaleJob] = []
        self.fail_on = fail_on
        self.raise_cls = raise_cls

    def upscale(
        self, instance: Any, job: UpscaleJob, cfg: Any, *, cancel_token: Any = None
    ) -> UpscaleResult:
        self.calls.append(job)
        n = len(self.calls)
        if n in self.fail_on:
            raise self.raise_cls(f"boom on call {n}")
        out = self.tmp / f"result{n}.png"
        out.write_bytes(f"upscaled-{n}".encode())
        return UpscaleResult(
            artifact=Artifact(uri=f"file://{out}", meta={"media": "image"}),
            input_resolution=(4, 4),
            output_resolution=(8, 8),
            elapsed_s=0.0,
        )


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub deploy_session; record entries and the kwargs it received."""
    rec: dict[str, Any] = {"entries": 0, "kwargs": None}
    sess = DeploySession(
        backend=MagicMock(),
        profile=MagicMock(),
        pool=MagicMock(),
        instance=_instance(),
        engine=MagicMock(name="GenerationEngine"),
        provider=MagicMock(),
    )

    @contextmanager
    def fake_deploy(cfg: Any, **kwargs: Any) -> Any:
        rec["entries"] += 1
        rec["kwargs"] = kwargs
        yield sess

    from kinoforge.core import orchestrator

    monkeypatch.setattr(orchestrator, "deploy_session", fake_deploy)
    return rec


def _src(tmp_path: Path, names: list[str]) -> Path:
    src = tmp_path / "photos"
    for n in names:
        p = src / n
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), 0).save(p)
    return src


def _run(
    tmp_path: Path,
    src: Path,
    upscaler: _Upscaler,
    *,
    probe: Any = None,
    single: bool = False,
) -> tuple[Any, list[tuple[str, str, str | None]]]:
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
    seen: list[tuple[str, str, str | None]] = []
    result = upscale_image_dir(
        _cfg(),
        plan,
        store=MagicMock(),
        run_id="r",
        state_dir=tmp_path / ".kf",
        upscaler=upscaler,  # type: ignore[arg-type]
        single=single,
        on_item=lambda item, outcome, reason: seen.append(
            (item.source.name, outcome, reason)
        ),
        health_probe=probe if probe is not None else (lambda url: {"ok": True}),
    )
    return result, seen


def test_one_session_three_items_written(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: one deploy_session per image (the loop-over-generate
    # shape), outputs not mirrored, scratch dir leaked.
    src = _src(tmp_path, ["a.png", "sub/b.webp", "c.png"])
    up = _Upscaler(tmp_path)
    (result, _), seen = _run(tmp_path, src, up)
    assert session["entries"] == 1
    assert [s[1] for s in seen] == ["written", "written", "written"]
    out = tmp_path / "photos_upscaled"
    # walk_images (Task 3) sorts each directory level by name and recurses
    # immediately, so top-level "a.png" < "c.png" < "sub" gives pending
    # order [a.png, c.png, sub/b.webp] — not source-creation order.
    assert (out / "a.png").read_bytes() == b"upscaled-1"
    assert (out / "c.png").read_bytes() == b"upscaled-2"
    assert (out / "sub" / "b.png").read_bytes() == b"upscaled-3"
    assert result.written == 3 and result.failed == 0 and result.aborted == 0
    assert all(j.media == "image" for j in up.calls)
    import tempfile

    assert not any(
        p.name.startswith("kf-image-dir-")
        for p in Path(tempfile.gettempdir()).iterdir()
    )


def test_per_item_failure_continues_and_probes_once(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: stop on first failure; or no health probe after one.
    src = _src(tmp_path, ["a.png", "b.png", "c.png"])
    probes: list[str] = []

    def probe(url: str) -> dict[str, bool]:
        probes.append(url)
        return {"ok": True}

    up = _Upscaler(tmp_path, fail_on={2})
    (result, _), seen = _run(tmp_path, src, up, probe=probe)
    assert [s[1] for s in seen] == ["written", "failed", "written"]
    assert "boom on call 2" in (seen[1][2] or "")
    assert probes == ["https://pod-1-8000.proxy.runpod.net/health"]
    assert result.written == 2 and result.failed == 1
    assert not (tmp_path / "photos_upscaled" / "b.png").exists()


def test_dead_pod_aborts_the_rest(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: paying an upload timeout per remaining file against a
    # pod that is gone.
    src = _src(tmp_path, ["a.png", "b.png", "c.png", "d.png"])

    def probe(url: str) -> dict[str, bool]:
        raise OSError("connection refused")

    up = _Upscaler(tmp_path, fail_on={2})
    with pytest.raises(PodDead, match="pod-1"):
        _run(tmp_path, src, up, probe=probe)
    assert len(up.calls) == 2


def test_dead_pod_reports_aborted_items_via_on_item(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    src = _src(tmp_path, ["a.png", "b.png", "c.png", "d.png"])
    seen: list[tuple[str, str]] = []
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)

    def probe(url: str) -> dict[str, bool]:
        raise OSError("down")

    with pytest.raises(PodDead):
        upscale_image_dir(
            _cfg(),
            plan,
            store=MagicMock(),
            run_id="r",
            state_dir=tmp_path / ".kf",
            upscaler=_Upscaler(tmp_path, fail_on={2}),  # type: ignore[arg-type]
            on_item=lambda item, outcome, reason: seen.append(
                (item.source.name, outcome)
            ),
            health_probe=probe,
        )
    assert seen == [
        ("a.png", "written"),
        ("b.png", "failed"),
        ("c.png", "aborted"),
        ("d.png", "aborted"),
    ]


@pytest.mark.parametrize("exc", [Cancelled("stop"), BudgetExceeded("over")])
def test_cancel_and_fatal_abort_the_rest_and_reraise(
    tmp_path: Path, session: dict[str, Any], exc: KinoforgeError
) -> None:
    # Bug caught: a cancel swallowed as a per-item failure, so the run keeps
    # uploading after Ctrl-C.
    src = _src(tmp_path, ["a.png", "b.png", "c.png"])
    seen: list[tuple[str, str]] = []
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
    up = _Upscaler(tmp_path, fail_on={2}, raise_cls=type(exc))
    with pytest.raises(type(exc)):
        upscale_image_dir(
            _cfg(),
            plan,
            store=MagicMock(),
            run_id="r",
            state_dir=tmp_path / ".kf",
            upscaler=up,  # type: ignore[arg-type]
            on_item=lambda item, outcome, reason: seen.append(
                (item.source.name, outcome)
            ),
            health_probe=lambda url: {"ok": True},
        )
    assert seen == [("a.png", "written"), ("b.png", "aborted"), ("c.png", "aborted")]
    assert len(up.calls) == 2


def test_single_is_forwarded_and_session_entered_once_despite_failure(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: --no-reuse destroying per item, or not at all after a
    # failure. The destroy itself is deploy_session's (tested there).
    src = _src(tmp_path, ["a.png", "b.png"])
    _run(tmp_path, src, _Upscaler(tmp_path, fail_on={1}), single=True)
    assert session["entries"] == 1
    assert session["kwargs"]["single"] is True


def test_no_pending_items_never_opens_a_session(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    src = _src(tmp_path, ["a.png"])
    (tmp_path / "photos_upscaled").mkdir()
    Image.new("RGB", (4, 4), 0).save(tmp_path / "photos_upscaled" / "a.png")
    (result, inst), seen = _run(tmp_path, src, _Upscaler(tmp_path))
    assert session["entries"] == 0 and seen == [] and result.written == 0


def test_stage_receives_image_media_and_no_tiling(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: the stage built with tile_grid/chunk_frames from cfg
    # would try to ffprobe a PNG.
    src = _src(tmp_path, ["a.png"])
    up = _Upscaler(tmp_path)
    _run(tmp_path, src, up)
    assert up.calls[0].media == "image" and up.calls[0].scale.value == 2
