"""`core/image_dir.py` planning: walk, naming, header read, megapixel guard."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from kinoforge.core.image_dir import (
    ImageDirPlan,
    output_dir_for,
    output_megapixels,
    plan_image_dir,
    read_image_header,
)


def _png(path: Path, size: tuple[int, int] = (8, 6), mode: str = "RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, 0).save(path)
    return path


def _jpeg_oriented(path: Path, size: tuple[int, int], orientation: int) -> Path:
    im = Image.new("RGB", size, (1, 2, 3))
    exif = im.getexif()
    exif[0x0112] = orientation
    im.save(path, exif=exif)
    return path


def _plan(src: Path, *, scale: int = 2, cap: int = 256) -> ImageDirPlan:
    return plan_image_dir(src, scale=scale, max_output_megapixels=cap)


class TestOutputDirFor:
    def test_sibling_with_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: a trailing slash yields "photos/_upscaled" or an empty
        # name; a relative path resolves somewhere other than the cwd.
        (tmp_path / "photos").mkdir()
        monkeypatch.chdir(tmp_path)
        expected = tmp_path.resolve() / "photos_upscaled"
        assert output_dir_for(Path("photos")) == expected
        assert output_dir_for(Path("photos/")) == expected
        assert output_dir_for(tmp_path / "photos") == expected

    def test_root_is_refused(self) -> None:
        with pytest.raises(ValueError, match="root"):
            output_dir_for(Path("/"))


class TestWalk:
    def test_recursive_sorted_mirrored(self, tmp_path: Path) -> None:
        # Bug caught: subdirectories dropped, or non-deterministic order.
        src = tmp_path / "photos"
        _png(src / "b.png")
        _png(src / "a.webp")
        _png(src / "sub" / "deep" / "c.bmp")
        plan = _plan(src)
        rel_in = [i.source.relative_to(src).as_posix() for i in plan.items]
        rel_out = [i.output.relative_to(plan.output_dir).as_posix() for i in plan.items]
        assert rel_in == ["a.webp", "b.png", "sub/deep/c.bmp"]
        assert rel_out == ["a.png", "b.png", "sub/deep/c.png"]
        assert plan.output_dir == tmp_path.resolve() / "photos_upscaled"
        assert _plan(src).items == plan.items  # stable across calls

    def test_dotfiles_and_symlinks_are_skipped(self, tmp_path: Path) -> None:
        # Bug caught: following a symlinked directory loops or escapes the
        # tree; .DS_Store-style files become "unreadable" failures.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / ".hidden.png")
        (src / ".cache").mkdir()
        _png(src / ".cache" / "x.png")
        outside = _png(tmp_path / "outside" / "o.png")
        os.symlink(outside.parent, src / "linked_dir")
        os.symlink(outside, src / "linked.png")
        plan = _plan(src)
        assert [i.source.name for i in plan.items] == ["a.png"]
        assert plan.skipped_non_image == 0

    def test_non_image_files_are_counted_not_items(self, tmp_path: Path) -> None:
        # Bug caught: a refusal or an unreadable item for notes.txt.
        src = tmp_path / "photos"
        _png(src / "a.png")
        (src / "notes.txt").write_text("hi")
        (src / "clip.mp4").write_bytes(b"\x00")
        plan = _plan(src)
        assert [i.source.name for i in plan.items] == ["a.png"]
        assert plan.skipped_non_image == 2

    def test_suffix_match_is_case_insensitive(self, tmp_path: Path) -> None:
        src = tmp_path / "photos"
        _png(src / "A.PNG")
        assert [i.source.name for i in _plan(src).items] == ["A.PNG"]


class TestNaming:
    def test_same_folder_collision_keeps_full_name(self, tmp_path: Path) -> None:
        # Bug caught: a.webp and a.png both map to a.png and one output
        # silently overwrites the other.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "a.webp")
        _png(src / "b.webp")
        plan = _plan(src)
        by_name = {i.source.name: i for i in plan.items}
        assert by_name["a.png"].output.name == "a.png.png" and by_name["a.png"].renamed
        assert (
            by_name["a.webp"].output.name == "a.webp.png" and by_name["a.webp"].renamed
        )
        assert (
            by_name["b.webp"].output.name == "b.png" and not by_name["b.webp"].renamed
        )

    def test_cross_folder_same_stem_is_not_a_collision(self, tmp_path: Path) -> None:
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "sub" / "a.webp")
        plan = _plan(src)
        assert sorted(i.output.name for i in plan.items) == ["a.png", "a.png"]
        assert not any(i.renamed for i in plan.items)


class TestDispositions:
    def test_existing_output_is_skipped(self, tmp_path: Path) -> None:
        # Bug caught: every re-run re-upscales and overwrites.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "b.png")
        out = output_dir_for(src)
        _png(out / "a.png")
        plan = _plan(src)
        assert [i.source.name for i in plan.exists] == ["a.png"]
        assert [i.source.name for i in plan.pending] == ["b.png"]

    def test_unreadable_is_a_plan_failure(self, tmp_path: Path) -> None:
        # Bug caught: a corrupt .jpg crashes planning (so nothing runs) or
        # is uploaded and fails on the pod after the boot.
        src = tmp_path / "photos"
        _png(src / "ok.png")
        (src / "bad.jpg").write_bytes(b"not a jpeg")
        plan = _plan(src)
        bad = next(i for i in plan.items if i.source.name == "bad.jpg")
        assert bad.disposition == "unreadable"
        assert bad.reason is not None and "UnidentifiedImageError" in bad.reason
        assert [i.source.name for i in plan.failed_at_plan] == ["bad.jpg"]
        assert [i.source.name for i in plan.pending] == ["ok.png"]

    def test_oversize_boundary(self, tmp_path: Path) -> None:
        # Bug caught: an off-by-one at the cap, or a guard on INPUT pixels.
        src = tmp_path / "photos"
        # cap = 1 MP, scale 2 → input limit is 250,000 px.
        _png(src / "under.png", (500, 500))  # 250,000 × 4 = 1,000,000 → not over
        _png(src / "over.png", (501, 500))  # 250,500 × 4 = 1,002,000 → over
        plan = _plan(src, scale=2, cap=1)
        by = {i.source.name: i for i in plan.items}
        assert by["under.png"].disposition == "pending"
        assert by["over.png"].disposition == "oversize"
        assert "max_output_megapixels=1" in (by["over.png"].reason or "")
        assert "501x500" in (by["over.png"].reason or "")

    def test_output_megapixels_formula(self) -> None:
        assert output_megapixels(1000, 1000, 2) == 4.0
        assert output_megapixels(2672, 1504, 2) == pytest.approx(16.07, abs=0.01)


class TestHeader:
    def test_orientation_swaps_dimensions(self, tmp_path: Path) -> None:
        # Bug caught: the guard and the dry run report a portrait phone
        # photo as landscape; the output dims line is wrong.
        p = _jpeg_oriented(tmp_path / "o.jpg", (40, 20), orientation=6)
        hdr = read_image_header(p)
        assert (hdr.width, hdr.height) == (20, 40)
        assert hdr.orientation == 6
        plain = _jpeg_oriented(tmp_path / "p.jpg", (40, 20), orientation=1)
        assert (read_image_header(plain).width, read_image_header(plain).height) == (
            40,
            20,
        )

    def test_header_reports_mode_and_format(self, tmp_path: Path) -> None:
        p = _png(tmp_path / "x.png", mode="RGBA")
        hdr = read_image_header(p)
        assert hdr.mode == "RGBA" and hdr.format == "PNG" and hdr.orientation == 1

    def test_planning_does_not_decode_pixels(self, tmp_path: Path) -> None:
        # Bug caught: a planner that calls im.load() decodes every file;
        # a 1×60000 PNG header must plan in well under a second.
        import time

        src = tmp_path / "photos"
        _png(src / "tall.png", (1, 60000))
        t0 = time.perf_counter()
        plan = _plan(src, cap=1000)
        assert plan.items[0].height == 60000
        assert time.perf_counter() - t0 < 1.0


def _anim(
    path: Path, n: int = 4, size: tuple[int, int] = (8, 6), mode: str = "RGBA"
) -> Path:
    """Write an n-frame animation (or multi-page TIFF) in the container of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [Image.new(mode, size, (10 * i, 0, 0, 255)[: len(mode)]) for i in range(n)]
    kw: dict[str, Any] = {}
    if path.suffix.lower() in (".webp", ".gif", ".png"):
        kw = {"duration": [100] * n, "loop": 0}
    frames[0].save(path, save_all=True, append_images=frames[1:], **kw)
    return path


class TestFrames:
    @pytest.mark.parametrize(
        ("name", "n"),
        [("a.webp", 4), ("a.gif", 4), ("a.png", 4), ("a.tif", 3), ("a.tiff", 3)],
    )
    def test_header_reports_frames(self, tmp_path: Path, name: str, n: int) -> None:
        # Bug caught: a two-frame APNG reports one frame and passes through
        # byte for byte; a multi-page TIFF is treated as its first page.
        mode = "RGB" if name.endswith(("tif", "tiff")) else "RGBA"
        hdr = read_image_header(_anim(tmp_path / name, n, mode=mode))
        assert hdr.frames == n

    @pytest.mark.parametrize("name", ["s.png", "s.jpg", "s.webp"])
    def test_still_reports_one_frame(self, tmp_path: Path, name: str) -> None:
        # Bug caught: a still is routed through the animated branch.
        assert read_image_header(_png(tmp_path / name)).frames == 1

    def test_frames_only_consulted_for_animated_containers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: a PSD (n_frames == layer count) or ICO (n_frames ==
        # size count) is classified as an animation and fed frame by frame.
        from PIL import Image as _Image

        # Bug caught (fixture note): Pillow can READ many containers it cannot
        # WRITE — plain `_png(path)` infers the save format from `.psd` and
        # KeyErrors since Pillow has no PSD encoder. Saving with an explicit
        # `format="PNG"` still produces real PNG bytes under the `.psd` name,
        # which is exactly "a PNG under a .psd name" the brief calls for.
        src = tmp_path / "layers.psd"
        src.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 6), 0).save(src, format="PNG")
        real_open = _Image.open

        class _Multi:
            def __init__(self, im: object) -> None:
                self._im = im
                self.n_frames = 7

            def __getattr__(self, name: str) -> object:
                return getattr(self._im, name)

            def __enter__(self) -> _Multi:
                return self

            def __exit__(self, *a: object) -> None:
                self._im.close()  # type: ignore[attr-defined]

        monkeypatch.setattr(_Image, "open", lambda p, *a, **k: _Multi(real_open(p)))
        assert read_image_header(src).frames == 1

    def test_above_cap_is_oversize_naming_the_cap(self, tmp_path: Path) -> None:
        # Bug caught: a 2,000-frame file enters the pod loop; or the reason
        # does not say which knob to raise.
        src = tmp_path / "photos"
        _anim(src / "big.webp", 4)
        _anim(src / "ok.webp", 3)
        plan = plan_image_dir(
            src, scale=2, max_output_megapixels=256, max_animation_frames=3
        )
        by = {i.source.name: i for i in plan.items}
        assert by["big.webp"].disposition == "oversize"
        assert (
            by["big.webp"].reason == "4 frames exceeds upscale.max_animation_frames=3"
        )
        assert by["ok.webp"].disposition == "pending"
        assert by["ok.webp"].frames == 3

    def test_default_cap_is_300(self, tmp_path: Path) -> None:
        # Bug caught: the keyword default drifts from the config default.
        src = tmp_path / "photos"
        _anim(src / "a.gif", 4, mode="RGB")
        plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
        assert plan.items[0].disposition == "pending"

    def test_megapixel_guard_applies_per_frame(self, tmp_path: Path) -> None:
        # Bug caught: the guard multiplies by the frame count and refuses
        # a 4-frame 8x6 animation at 2x as "oversize" on pixels.
        src = tmp_path / "photos"
        _anim(src / "a.webp", 4, size=(100, 100))
        # 100x100 at 2x = 0.04 MP per frame; a cap of 1 MP must pass
        plan = plan_image_dir(src, scale=2, max_output_megapixels=1)
        assert plan.items[0].disposition == "pending"


class TestAnimatedNaming:
    @pytest.mark.parametrize(
        ("name", "out"),
        [
            ("anim.webp", "anim.webp"),
            ("anim.gif", "anim.gif"),
            ("anim.png", "anim.png"),
            ("pages.tif", "pages.tif"),
            ("pages.tiff", "pages.tiff"),
        ],
    )
    def test_animated_keeps_its_container(
        self, tmp_path: Path, name: str, out: str
    ) -> None:
        # Bug caught: an animated WebP is written as a still <stem>.png.
        src = tmp_path / "photos"
        mode = "RGB" if name.endswith(("tif", "tiff")) else "RGBA"
        _anim(src / name, 3, mode=mode)
        plan = _plan(src)
        assert plan.items[0].output == plan.output_dir / out

    def test_still_webp_still_becomes_png(self, tmp_path: Path) -> None:
        # Bug caught: the suffix rule keys on the container, not on frames.
        src = tmp_path / "photos"
        _png(src / "b.webp")
        assert _plan(src).items[0].output.name == "b.png"

    def test_collision_between_animated_and_still_keeps_full_names(
        self, tmp_path: Path
    ) -> None:
        # Bug caught: different output suffixes exempt the pair from the rule,
        # so a later-added `a.webp` still silently shadows `a.png`'s output.
        src = tmp_path / "photos"
        _anim(src / "a.webp", 3)
        _png(src / "a.png")
        plan = _plan(src)
        names = {i.source.name: (i.output.name, i.renamed) for i in plan.items}
        assert names == {"a.webp": ("a.webp.webp", True), "a.png": ("a.png.png", True)}

    def test_existing_animated_output_is_exists(self, tmp_path: Path) -> None:
        # Bug caught: a finished animation is paid for again on rerun.
        src = tmp_path / "photos"
        _anim(src / "anim.webp", 3)
        out = tmp_path / "photos_upscaled"
        _anim(out / "anim.webp", 3)
        assert _plan(src).items[0].disposition == "exists"
