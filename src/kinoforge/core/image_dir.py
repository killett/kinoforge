"""Plan a directory of stills for ``kinoforge upscale --image-dir``.

Pure planning: walk the tree, name every output, read image HEADERS (size,
mode, EXIF orientation — never pixels), apply the output-megapixel guard, and
hand the CLI and the session runner one finished :class:`ImageDirPlan` so
neither re-derives a rule. An animated input (``frames > 1``) keeps its own
container on output and is refused above ``max_animation_frames``.
:func:`prepare_upload` (same module) converts one planned input into the
PNG/JPEG bytes the pod accepts.

Controller-side only. Pillow is imported lazily inside functions.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from kinoforge.core.media import DIR_IMAGE_SUFFIXES, IMAGE_SUFFIXES

if TYPE_CHECKING:
    from PIL import Image as _PILImage

_log = logging.getLogger("kinoforge.core.image_dir")

OUTPUT_DIR_SUFFIX = "_upscaled"
"""Appended to the source directory's last component to name the output dir."""

_ORIENTATION_TAG = 0x0112
_ROTATED_ORIENTATIONS = frozenset({5, 6, 7, 8})
"""EXIF orientations that swap width and height as viewed."""

_ANIMATED_SUFFIXES = frozenset({".webp", ".gif", ".png", ".tif", ".tiff"})
"""Containers whose Pillow ``n_frames`` means frames (or pages) of ONE image.

Deliberately not every container that reports ``n_frames``: a PSD reports
its layer count and an ICO its size count. Anything else is a still.
"""

Disposition = Literal["pending", "exists", "oversize", "unreadable"]


@dataclass(frozen=True)
class ImageHeader:
    """What planning reads from a file without decoding pixels.

    Attributes:
        width: Width as VIEWED (EXIF orientation applied).
        height: Height as viewed.
        mode: Pillow mode string (``"RGB"``, ``"I;16"``, ``"P"``, ...).
        format: Pillow format name (``"PNG"``, ``"JPEG"``, ``"WEBP"``, ...).
        orientation: EXIF orientation 1-8; 1 is the identity.
        frames: Frame count; ``1`` for a still. Read only for
            :data:`_ANIMATED_SUFFIXES`; other containers always report ``1``.
    """

    width: int
    height: int
    mode: str
    format: str | None
    orientation: int
    frames: int = 1


@dataclass(frozen=True)
class ImageDirItem:
    """One discovered image and what will happen to it.

    Attributes:
        source: Absolute input path.
        output: Absolute output path — ``.png`` for a still, the input's own
            suffix for an animation.
        width: Viewed width (0 when ``unreadable``).
        height: Viewed height (0 when ``unreadable``).
        disposition: ``pending`` (will run), ``exists`` (output present,
            skipped), ``oversize`` / ``unreadable`` (plan-time failures).
        reason: Set for the two failure dispositions.
        renamed: The same-folder stem-collision rule applied (§3.2).
        frames: Frame count from the header; ``> 1`` takes the animated path
            and keeps the input container's suffix on ``output``.
    """

    source: Path
    output: Path
    width: int
    height: int
    disposition: Disposition
    reason: str | None = None
    renamed: bool = False
    frames: int = 1


@dataclass(frozen=True)
class ImageDirPlan:
    """The finished plan for one directory.

    Attributes:
        source_dir: Resolved input directory.
        output_dir: Resolved sibling ``<name>_upscaled`` directory.
        items: Every recognised image in sorted walk order.
        skipped_non_image: Files whose suffix is outside
            :data:`~kinoforge.core.media.DIR_IMAGE_SUFFIXES`.
    """

    source_dir: Path
    output_dir: Path
    items: tuple[ImageDirItem, ...]
    skipped_non_image: int

    @property
    def pending(self) -> tuple[ImageDirItem, ...]:
        """Items the runner will upscale."""
        return tuple(i for i in self.items if i.disposition == "pending")

    @property
    def exists(self) -> tuple[ImageDirItem, ...]:
        """Items whose output already exists."""
        return tuple(i for i in self.items if i.disposition == "exists")

    @property
    def failed_at_plan(self) -> tuple[ImageDirItem, ...]:
        """Items refused before any pod work (``oversize`` / ``unreadable``)."""
        return tuple(
            i for i in self.items if i.disposition in ("oversize", "unreadable")
        )


def output_dir_for(source_dir: Path) -> Path:
    """Return the sibling output directory for *source_dir*.

    ``photos`` and ``photos/`` both give ``<parent>/photos_upscaled``.

    Args:
        source_dir: The input directory (relative paths resolve from cwd).

    Returns:
        The resolved output directory; it may not exist yet.

    Raises:
        ValueError: *source_dir* is a filesystem root.
    """
    src = Path(source_dir).resolve()
    if src.parent == src:
        raise ValueError(f"cannot upscale a filesystem root: {src}")
    return src.parent / f"{src.name}{OUTPUT_DIR_SUFFIX}"


def register_heif() -> bool:
    """Register Pillow's HEIF/HEIC opener if ``pillow-heif`` is importable.

    Returns:
        ``True`` when registered, ``False`` when the package is absent (the
        ``.heic`` / ``.heif`` suffixes stay recognised and such files become
        ``unreadable`` items).
    """
    try:
        import pillow_heif
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


def register_avif() -> bool:
    """Register the AVIF opener if ``pillow-avif-plugin`` is importable.

    conda-forge's Pillow is built without libavif, so Pillow's bundled
    ``AvifImagePlugin`` registers nothing there; the plugin package carries
    its own codec. Importing it is the registration.

    Returns:
        ``True`` when registered, ``False`` when the package is absent (the
        ``.avif`` suffix stays recognised and such files become
        ``unreadable`` items).
    """
    try:
        import pillow_avif  # noqa: F401 — import side effect registers
    except ImportError:
        return False
    return True


def register_openers() -> None:
    """Register every optional Pillow opener a ``DIR_IMAGE_SUFFIXES`` entry needs."""
    register_heif()
    register_avif()


def read_image_header(path: Path) -> ImageHeader:
    """Read size, mode, format, EXIF orientation and frame count without decoding pixels.

    Args:
        path: An image file.

    Returns:
        The header, with width/height swapped for the 90° orientations.

    Raises:
        OSError: Pillow cannot open the file (``UnidentifiedImageError`` is
            an ``OSError``); other Pillow failures propagate as raised.
    """
    from PIL import Image

    register_openers()
    with Image.open(path) as im:
        orientation = int(im.getexif().get(_ORIENTATION_TAG, 1) or 1)
        width, height = im.size
        if orientation in _ROTATED_ORIENTATIONS:
            width, height = height, width
        frames = 1
        if path.suffix.lower() in _ANIMATED_SUFFIXES:
            frames = max(1, int(getattr(im, "n_frames", 1)))
        return ImageHeader(
            width=width,
            height=height,
            mode=im.mode,
            format=im.format,
            orientation=orientation,
            frames=frames,
        )


def output_megapixels(width: int, height: int, scale: int) -> float:
    """Return ``width × height × scale²`` in megapixels.

    Args:
        width: Viewed width in pixels.
        height: Viewed height in pixels.
        scale: Integer upscale factor.

    Returns:
        The resulting output size in megapixels.
    """
    return width * height * scale * scale / 1e6


def walk_images(source_dir: Path) -> tuple[list[Path], int]:
    """Recursively list image files under *source_dir* in sorted order.

    Dot-prefixed names are skipped at every level, symlinks are never
    followed, and files outside :data:`DIR_IMAGE_SUFFIXES` are counted.

    Args:
        source_dir: Directory to walk.

    Returns:
        ``(image paths in walk order, non-image file count)``.
    """
    images: list[Path] = []
    skipped = 0

    def _walk(directory: Path) -> None:
        nonlocal skipped
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir():
                _walk(entry)
            elif entry.is_file():
                if entry.suffix.lower() in DIR_IMAGE_SUFFIXES:
                    images.append(entry)
                else:
                    skipped += 1

    _walk(source_dir)
    return images, skipped


def _output_suffix(path: Path, frames: int) -> str:
    """``.png`` for a still; the input's own (lower-cased) suffix for an animation."""
    return ".png" if frames <= 1 else path.suffix.lower()


def _output_names(
    paths: list[Path], frames: dict[Path, int]
) -> dict[Path, tuple[str, bool]]:
    """Map each source to ``(output filename, renamed)`` per the §3.2 rule.

    Stems are compared within one folder regardless of suffix: an animated
    ``a.webp`` and a still ``a.png`` collide even though their outputs would
    not share a name — predictability beats a special case.
    """
    by_folder: dict[Path, Counter[str]] = {}
    for p in paths:
        by_folder.setdefault(p.parent, Counter())[p.stem] += 1
    names: dict[Path, tuple[str, bool]] = {}
    for p in paths:
        suffix = _output_suffix(p, frames.get(p, 1))
        if by_folder[p.parent][p.stem] > 1:
            names[p] = (f"{p.name}{suffix}", True)
        else:
            names[p] = (f"{p.stem}{suffix}", False)
    return names


def plan_image_dir(
    source_dir: Path,
    *,
    scale: int,
    max_output_megapixels: int,
    max_animation_frames: int = 300,
) -> ImageDirPlan:
    """Build the plan for *source_dir*.

    Per file, in order: header read (``unreadable`` on failure), existing
    output (``exists``), the frame cap (``oversize``), the megapixel guard
    (``oversize``), else ``pending``.

    Args:
        source_dir: The input directory.
        scale: Integer upscale factor (the guard bounds OUTPUT pixels).
        max_output_megapixels: ``cfg.upscale.max_output_megapixels``.
        max_animation_frames: ``cfg.upscale.max_animation_frames``; an
            animation with more frames is refused before any pod work.

    Returns:
        The plan.

    Raises:
        NotADirectoryError: *source_dir* is not a directory.
        ValueError: *source_dir* is a filesystem root.
    """
    src = Path(source_dir).resolve()
    if not src.is_dir():
        raise NotADirectoryError(str(src))
    out_dir = output_dir_for(src)
    paths, skipped = walk_images(src)
    headers: dict[Path, ImageHeader] = {}
    errors: dict[Path, str] = {}
    for p in paths:
        try:
            headers[p] = read_image_header(p)
        except Exception as exc:  # noqa: BLE001 — any Pillow failure is "unreadable"
            errors[p] = f"{type(exc).__name__}: {exc}"
    names = _output_names(paths, {p: h.frames for p, h in headers.items()})
    renamed_folders: set[Path] = set()
    items: list[ImageDirItem] = []
    for p in paths:
        name, renamed = names[p]
        if renamed and p.parent not in renamed_folders:
            renamed_folders.add(p.parent)
            _log.warning(
                "%s: stem collision; colliding files keep their full name + suffix",
                p.parent,
            )
        output = out_dir / p.parent.relative_to(src) / name
        if p in errors:
            items.append(
                ImageDirItem(p, output, 0, 0, "unreadable", errors[p], renamed)
            )
            continue
        hdr = headers[p]
        if output.exists():
            disposition: Disposition = "exists"
            reason: str | None = None
        elif hdr.frames > max_animation_frames:
            disposition = "oversize"
            reason = (
                f"{hdr.frames} frames exceeds "
                f"upscale.max_animation_frames={max_animation_frames}"
            )
        elif output_megapixels(hdr.width, hdr.height, scale) > max_output_megapixels:
            disposition = "oversize"
            reason = (
                f"{hdr.width}x{hdr.height} at {scale}x -> "
                f"{output_megapixels(hdr.width, hdr.height, scale):.1f} MP exceeds "
                f"upscale.max_output_megapixels={max_output_megapixels}"
            )
        else:
            disposition, reason = "pending", None
        items.append(
            ImageDirItem(
                p,
                output,
                hdr.width,
                hdr.height,
                disposition,
                reason,
                renamed,
                hdr.frames,
            )
        )
    return ImageDirPlan(src, out_dir, tuple(items), skipped)


_PASSTHROUGH_SUFFIXES = IMAGE_SUFFIXES
"""The pod accepts PNG/JPEG byte-for-byte — the same rule as ``--image``."""
_PASSTHROUGH_MODES = frozenset({"RGB", "RGBA", "L"})


def _sha8(path: Path) -> str:
    """First 8 hex digits of the file's sha256."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def prepare_upload(item: ImageDirItem, scratch: Path) -> Path:
    """Return the path to upload for *item*: the source itself, or a converted PNG.

    The pod accepts only PNG/JPEG, 8-bit, and reads them without applying
    EXIF orientation. A PNG or JPEG whose mode is ``RGB`` / ``RGBA`` / ``L``
    and whose orientation is the identity is uploaded byte for byte.
    Everything else is opened, EXIF-transposed, converted to 8-bit ``RGB``
    and written as ``<sha8>.png`` under *scratch*. Multi-frame inputs
    contribute frame 0.

    Args:
        item: A planned item (``pending``).
        scratch: Per-run scratch directory; the caller removes it.

    Returns:
        The path whose bytes go to the pod.
    """
    from PIL import Image, ImageOps

    register_openers()
    with Image.open(item.source) as im:
        orientation = int(im.getexif().get(_ORIENTATION_TAG, 1) or 1)
        if (
            item.source.suffix.lower() in _PASSTHROUGH_SUFFIXES
            and im.mode in _PASSTHROUGH_MODES
            and orientation == 1
        ):
            return item.source
        src_mode = im.mode
        n_frames = int(getattr(im, "n_frames", 1))
        if n_frames > 1:
            _log.info(
                "%s: %d frames; upscaling frame 0 only", item.source.name, n_frames
            )
            im.seek(0)
        transposed = ImageOps.exif_transpose(im)
        rgb = (transposed if transposed is not None else im).convert("RGB")
    out = scratch / f"{_sha8(item.source)}.png"
    rgb.save(out, format="PNG")
    _log.info(
        "%s: re-encoded to %s (%s -> RGB PNG)", item.source.name, out.name, src_mode
    )
    return out


@dataclass(frozen=True)
class Frame:
    """One decoded frame of an animated input, ready to upload.

    Attributes:
        index: 0-based frame index.
        path: 8-bit RGB PNG under the run's scratch directory
            (``<sha8>-f0001.png`` …); the caller unlinks it after upload.
        duration_ms: The frame's own duration; ``0`` when the container has
            no timing (multi-page TIFF).
        alpha: The frame's alpha channel as an 8-bit ``L`` image, or
            ``None`` when the frame has none.
    """

    index: int
    path: Path
    duration_ms: int
    alpha: _PILImage.Image | None


def _has_alpha(im: _PILImage.Image) -> bool:
    """True when the current frame carries alpha or palette transparency."""
    return "A" in im.getbands() or "transparency" in im.info


def read_loop(path: Path) -> int | None:
    """Return the animation's loop count, or ``None`` when the container carries none.

    ``None`` when the container carries no loop count (a GIF without the
    Netscape extension plays once; a multi-page TIFF has none).

    Args:
        path: An animated input.

    Returns:
        Pillow's ``info["loop"]`` as an int, or ``None`` when absent.
    """
    from PIL import Image

    register_openers()
    with Image.open(path) as im:
        v = im.info.get("loop")
        return None if v is None else int(v)


def prepare_frames(item: ImageDirItem, scratch: Path) -> Generator[Frame, None, None]:
    """Yield every frame of an animated *item* as an RGB PNG under *scratch*.

    One frame is decoded at a time: the generator writes frame *i* only when
    the consumer asks for it, so scratch holds at most one decoded frame plus
    whatever the consumer has not yet unlinked. Each frame is EXIF-transposed
    like :func:`prepare_upload`. WebP fills a frame's ``duration`` only on
    ``load()``, so every frame is loaded after ``seek``.

    Args:
        item: A planned animated item (``frames > 1``).
        scratch: Per-run scratch directory; the consumer unlinks each path.

    Yields:
        :class:`Frame` in frame order.
    """
    from PIL import Image, ImageOps

    register_openers()
    tag = _sha8(item.source)
    with Image.open(item.source) as im:
        n = max(1, int(getattr(im, "n_frames", 1)))
        for i in range(n):
            im.seek(i)
            im.load()
            duration = int(round(float(im.info.get("duration") or 0)))
            alpha: _PILImage.Image | None = None
            rgba = im.convert("RGBA")
            transposed = ImageOps.exif_transpose(rgba)
            frame = transposed if transposed is not None else rgba
            if _has_alpha(im):
                alpha = frame.getchannel("A")
            path = scratch / f"{tag}-f{i + 1:04d}.png"
            frame.convert("RGB").save(path, format="PNG")
            yield Frame(i, path, duration, alpha)
