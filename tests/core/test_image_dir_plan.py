"""`core/image_dir.py` planning: walk, naming, header read, megapixel guard."""

from __future__ import annotations

import os
from pathlib import Path

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
