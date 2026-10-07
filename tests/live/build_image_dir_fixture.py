"""Build the `--image-dir` live-smoke fixture directory (design §6.1).

Usage: ``pixi run python tests/live/build_image_dir_fixture.py [DEST]``
(default ``output/dir-smoke``). Idempotent: DEST is recreated from scratch.
Every branch of the feature is represented once; see the design for why each
file is there.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).parent.parent.parent
_LUMA = (
    _ROOT
    / "output"
    / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"
)


def build(dest: Path) -> None:
    """Create the fixture tree under *dest* and the pre-placed existing output."""
    if not _LUMA.exists():
        raise SystemExit(
            f"missing §35 input {_LUMA.name}; regenerate with kinoforge image"
        )
    shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(dest.parent / f"{dest.name}_upscaled", ignore_errors=True)
    (dest / "sub").mkdir(parents=True)
    with Image.open(_LUMA) as luma_file:
        luma = luma_file.convert("RGB")
        luma.save(dest / "luma.png")  # passthrough
        luma.save(dest / "luma-webp.webp", quality=95)  # conversion
        luma.save(dest / "luma-avif.avif", quality=80)  # conversion
        rotated = luma.transpose(Image.Transpose.ROTATE_90)  # stored on its side …
        exif = rotated.getexif()
        exif[0x0112] = (
            6  # … orientation 6 (exif_transpose applies ROTATE_270) restores it
        )
        rotated.save(dest / "rotated.jpg", exif=exif, quality=95)
    f0 = Image.new("RGB", (320, 200), (200, 30, 30))
    f1 = Image.new("RGB", (320, 200), (30, 200, 30))
    f0.save(dest / "sub" / "anim.gif", save_all=True, append_images=[f1], duration=200)
    alpha = Image.new("RGBA", (300, 180), (40, 120, 220, 255))
    alpha.putalpha(Image.linear_gradient("L").resize((300, 180)))
    alpha.save(dest / "sub" / "alpha.png")
    Image.new("RGB", (12000, 12000), (90, 90, 90)).save(dest / "huge.webp", quality=50)
    (dest / "notes.txt").write_text("not an image\n")
    Image.new("RGB", (64, 64), (0, 0, 0)).save(dest / "existing.png")
    out = dest.parent / f"{dest.name}_upscaled"
    out.mkdir()
    Image.new("RGB", (128, 128), (255, 0, 255)).save(
        out / "existing.png"
    )  # pre-placed → skipped
    print(f"fixture at {dest}; pre-placed {out / 'existing.png'}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else _ROOT / "output" / "dir-smoke")
