"""`prepare_upload`: passthrough vs controller-side re-encode to 8-bit RGB PNG."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from kinoforge.core.image_dir import ImageDirItem, prepare_upload, register_openers


@pytest.fixture(autouse=True)
def _openers() -> None:
    # The .avif / .heic fixtures are SAVED before prepare_upload registers.
    register_openers()


def _item(src: Path) -> ImageDirItem:
    return ImageDirItem(src, src.parent / "out.png", 1, 1, "pending")


def _rgb(size: tuple[int, int] = (6, 4)) -> Image.Image:
    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


class TestPassthrough:
    @pytest.mark.parametrize(
        ("name", "mode"),
        [("a.png", "RGB"), ("b.png", "RGBA"), ("c.png", "L"), ("d.jpg", "RGB")],
    )
    def test_identity_oriented_8bit_png_jpeg_is_untouched(
        self, tmp_path: Path, scratch: Path, name: str, mode: str
    ) -> None:
        # Bug caught: re-encoding everything changes the sha the pod
        # cross-checks and costs a decode per file for nothing.
        src = tmp_path / name
        _rgb().convert(mode).save(src)
        before = src.read_bytes()
        assert prepare_upload(_item(src), scratch) == src
        assert src.read_bytes() == before
        assert list(scratch.iterdir()) == []


class TestReencode:
    def test_orientation_is_applied(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: a phone portrait is upscaled lying on its side, because
        # the pod's imageio read ignores EXIF orientation.
        src = tmp_path / "o.jpg"
        im = _rgb((40, 20))
        exif = im.getexif()
        exif[0x0112] = 6
        im.save(src, exif=exif, quality=100, subsampling=0)
        out = prepare_upload(_item(src), scratch)
        assert out != src and out.parent == scratch and out.suffix == ".png"
        with Image.open(out) as got, Image.open(src) as orig:
            expected = ImageOps.exif_transpose(orig).convert("RGB")
            assert got.size == (20, 40)
            assert got.mode == "RGB"
            assert np.array_equal(np.asarray(got), np.asarray(expected))

    @pytest.mark.parametrize(
        ("name", "build"),
        [
            ("sixteen.png", lambda: Image.new("I;16", (6, 4), 1000)),
            ("pal.png", lambda: _rgb().convert("P", palette=Image.Palette.ADAPTIVE)),
            ("alpha.webp", lambda: _rgb().convert("RGBA")),
            ("x.avif", _rgb),
            ("x.bmp", _rgb),
        ],
    )
    def test_other_formats_become_8bit_rgb_png(
        self, tmp_path: Path, scratch: Path, name: str, build: object
    ) -> None:
        # Bug caught: 16-bit bytes reach the pod (its _to_rgb raises), or
        # a WebP is uploaded under a content type /upload rejects with 415.
        src = tmp_path / name
        build().save(src)  # type: ignore[operator]
        out = prepare_upload(_item(src), scratch)
        assert out.parent == scratch and out.suffix == ".png"
        with Image.open(out) as got:
            assert got.format == "PNG" and got.mode == "RGB" and got.size == (6, 4)

    def test_avif_is_readable_in_a_fresh_interpreter(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        # Bug caught: read_image_header or prepare_upload drops its
        # register_openers() call — hidden in-process by the autouse fixture
        # above, fatal in the real CLI where nothing else registers AVIF.
        src = tmp_path / "x.avif"
        _rgb().save(src)
        code = (
            "import sys; from pathlib import Path\n"
            "from kinoforge.core.image_dir import ImageDirItem, prepare_upload\n"
            "from kinoforge.core.image_dir import read_image_header\n"
            "src = Path(sys.argv[1]); scratch = Path(sys.argv[2])\n"
            "h = read_image_header(src)\n"
            "item = ImageDirItem(src, src.parent / 'out.png', 1, 1, 'pending')\n"
            "print(h.width, h.height, prepare_upload(item, scratch))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, str(src), str(scratch)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        width, height, out = proc.stdout.split()
        assert (width, height) == ("6", "4")
        with Image.open(out) as got:
            assert got.format == "PNG" and got.mode == "RGB" and got.size == (6, 4)

    def test_animated_gif_uses_frame_zero(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: the last frame, or a Pillow error on seek.
        src = tmp_path / "anim.gif"
        f0 = Image.new("RGB", (4, 4), (200, 0, 0))
        f1 = Image.new("RGB", (4, 4), (0, 200, 0))
        f0.save(src, save_all=True, append_images=[f1])
        out = prepare_upload(_item(src), scratch)
        with Image.open(out) as got:
            r, g, b = got.getpixel((0, 0))  # type: ignore[misc]
            assert r > 150 and g < 50

    def test_scratch_name_is_sha8(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: two converted inputs with the same stem overwrite each
        # other inside one run.
        src = tmp_path / "x.webp"
        _rgb().save(src)
        out = prepare_upload(_item(src), scratch)
        assert out.name == hashlib.sha256(src.read_bytes()).hexdigest()[:8] + ".png"
