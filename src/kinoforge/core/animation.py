"""Reassemble upscaled frames into an animation in the input's own container.

Controller-side only; Pillow is imported lazily inside functions. The pod
returns one RGB PNG per frame; this module puts the frames back together
with the durations, loop count and alpha the planner's converter read from
the source. Alpha never visits the pod (it returns RGB), so each mask is
resized on the controller with Lanczos and reattached — cheap, keeps
stickers transparent, and the mask's edges are interpolated rather than
model-upscaled.

Per-container writers, all Pillow ``save_all``:

* WebP — lossy at :data:`WEBP_QUALITY`, durations + loop, alpha kept.
* GIF — Pillow quantises each frame to a palette; alpha becomes 1-bit
  transparency via a reserved palette index (saving RGBA straight to GIF
  drops transparency entirely on Pillow 12).
* APNG — lossless, durations + loop, alpha kept. The PNG writer does NOT
  accept a generator for ``append_images`` (it yields a one-frame file), so
  this is the one container whose frames are materialised in memory.
* TIFF — multi-page, lossless, no timing.

A GIF with ``loop=None`` is written WITHOUT the Netscape extension (plays
once, as the input did); the WebP and APNG containers always carry a loop
field, so for them "absent" falls back to the writer default (0 = forever)
— the input's own loop value is always preserved when present.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image as _PILImage

WEBP_QUALITY = 90
"""Lossy animated-WebP quality; a knob waits for someone to need it."""

_GIF_TRANSPARENT_INDEX = 255
"""Palette index reserved for transparent pixels when any frame has alpha."""

_WRITERS: dict[str, str] = {
    ".webp": "WEBP",
    ".gif": "GIF",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def _open_frame(path: Path) -> _PILImage.Image:
    """Open one upscaled frame (a seam the laziness test replaces)."""
    from PIL import Image

    return Image.open(path)


def _composed(
    paths: Sequence[Path], alphas: Sequence[_PILImage.Image | None]
) -> Iterator[_PILImage.Image]:
    """Yield each upscaled frame with its (resized) alpha reattached.

    Frames are opened one at a time and copied out of the file so the file
    handle closes before the next frame opens.
    """
    from PIL import Image

    for path, alpha in zip(paths, alphas, strict=True):
        with _open_frame(path) as im:
            frame = im.convert("RGB")
        if alpha is not None:
            mask = alpha
            if mask.size != frame.size:
                mask = mask.resize(frame.size, Image.Resampling.LANCZOS)
            frame.putalpha(mask)
        yield frame


def _gif_frame(frame: _PILImage.Image) -> _PILImage.Image:
    """Quantise one frame for GIF, reserving index 255 for alpha < 128."""
    from PIL import Image

    rgba = frame.convert("RGBA")
    pal = rgba.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    mask = rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    pal.paste(_GIF_TRANSPARENT_INDEX, mask=mask)
    return pal


def assemble_animation(
    frame_paths: Sequence[Path],
    *,
    durations_ms: Sequence[int],
    loop: int | None,
    alphas: Sequence[_PILImage.Image | None],
    output_suffix: str,
) -> bytes:
    """Encode upscaled frames as an animation in the container of *output_suffix*.

    Args:
        frame_paths: Upscaled RGB PNGs in frame order.
        durations_ms: One duration per frame (ignored for TIFF).
        loop: Loop count, ``0`` = forever, ``None`` = the source container
            carried no loop count (a play-once GIF). The loop keyword is
            passed to the writer only when this is not ``None``; WebP and
            APNG always carry a loop field, so for them an absent value
            falls back to the writer default (0 = forever).
        alphas: One ``L`` mask per frame (any size; resized to the frame), or
            ``None`` for an opaque frame.
        output_suffix: ``.webp`` / ``.gif`` / ``.png`` / ``.tif`` / ``.tiff``.

    Returns:
        The encoded file bytes; the caller writes them atomically.

    Raises:
        ValueError: No frames, mismatched sequence lengths, or a suffix with
            no animated writer.
    """
    if not frame_paths:
        raise ValueError("no frames to assemble")
    if not (len(frame_paths) == len(durations_ms) == len(alphas)):
        raise ValueError(
            f"frames/durations/alphas length mismatch: "
            f"{len(frame_paths)}/{len(durations_ms)}/{len(alphas)}"
        )
    fmt = _WRITERS.get(output_suffix.lower())
    if fmt is None:
        raise ValueError(f"no animated writer for suffix {output_suffix!r}")

    frames = _composed(frame_paths, alphas)
    durations = [int(d) for d in durations_ms]
    loop_kwargs: dict[str, Any] = {} if loop is None else {"loop": loop}
    buf = io.BytesIO()
    if fmt == "WEBP":
        first = next(frames)
        first.save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=frames,
            duration=durations,
            lossless=False,
            quality=WEBP_QUALITY,
            method=4,
            **loop_kwargs,
        )
    elif fmt == "GIF":
        has_alpha = any(a is not None for a in alphas)
        if has_alpha:
            frames = (_gif_frame(f) for f in frames)
        first = next(frames)
        extra: dict[str, Any] = (
            {"transparency": _GIF_TRANSPARENT_INDEX} if has_alpha else {}
        )
        first.save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames,
            duration=durations,
            disposal=2,
            **extra,
            **loop_kwargs,
        )
    elif fmt == "PNG":
        materialised = list(frames)  # the PNG writer cannot stream a generator
        materialised[0].save(
            buf,
            format="PNG",
            save_all=True,
            append_images=materialised[1:],
            duration=durations,
            **loop_kwargs,
        )
    else:  # TIFF
        first = next(frames)
        first.save(buf, format="TIFF", save_all=True, append_images=frames)
    return buf.getvalue()
