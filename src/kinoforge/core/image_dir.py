"""Plan a directory of stills for ``kinoforge upscale --image-dir``.

Pure planning: walk the tree, name every output, read image HEADERS (size,
mode, EXIF orientation — never pixels), apply the output-megapixel guard, and
hand the CLI and the session runner one finished :class:`ImageDirPlan` so
neither re-derives a rule. :func:`prepare_upload` (same module) converts one
planned input into the PNG/JPEG bytes the pod accepts.

Controller-side only. Pillow is imported lazily inside functions.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kinoforge.core.media import DIR_IMAGE_SUFFIXES

_log = logging.getLogger("kinoforge.core.image_dir")

OUTPUT_DIR_SUFFIX = "_upscaled"
"""Appended to the source directory's last component to name the output dir."""

_ORIENTATION_TAG = 0x0112
_ROTATED_ORIENTATIONS = frozenset({5, 6, 7, 8})
"""EXIF orientations that swap width and height as viewed."""

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
    """

    width: int
    height: int
    mode: str
    format: str | None
    orientation: int


@dataclass(frozen=True)
class ImageDirItem:
    """One discovered image and what will happen to it.

    Attributes:
        source: Absolute input path.
        output: Absolute output path, always ``.png``.
        width: Viewed width (0 when ``unreadable``).
        height: Viewed height (0 when ``unreadable``).
        disposition: ``pending`` (will run), ``exists`` (output present,
            skipped), ``oversize`` / ``unreadable`` (plan-time failures).
        reason: Set for the two failure dispositions.
        renamed: The same-folder stem-collision rule applied (§3.2).
    """

    source: Path
    output: Path
    width: int
    height: int
    disposition: Disposition
    reason: str | None = None
    renamed: bool = False


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


def read_image_header(path: Path) -> ImageHeader:
    """Read size, mode, format and EXIF orientation without decoding pixels.

    Args:
        path: An image file.

    Returns:
        The header, with width/height swapped for the 90° orientations.

    Raises:
        OSError: Pillow cannot open the file (``UnidentifiedImageError`` is
            an ``OSError``); other Pillow failures propagate as raised.
    """
    from PIL import Image

    register_heif()
    with Image.open(path) as im:
        orientation = int(im.getexif().get(_ORIENTATION_TAG, 1) or 1)
        width, height = im.size
        if orientation in _ROTATED_ORIENTATIONS:
            width, height = height, width
        return ImageHeader(
            width=width,
            height=height,
            mode=im.mode,
            format=im.format,
            orientation=orientation,
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


def _output_names(paths: list[Path]) -> dict[Path, tuple[str, bool]]:
    """Map each source to ``(output filename, renamed)`` per the §3.2 rule."""
    by_folder: dict[Path, Counter[str]] = {}
    for p in paths:
        by_folder.setdefault(p.parent, Counter())[p.stem] += 1
    names: dict[Path, tuple[str, bool]] = {}
    for p in paths:
        if by_folder[p.parent][p.stem] > 1:
            names[p] = (f"{p.name}.png", True)
        else:
            names[p] = (f"{p.stem}.png", False)
    return names


def plan_image_dir(
    source_dir: Path, *, scale: int, max_output_megapixels: int
) -> ImageDirPlan:
    """Build the plan for *source_dir*.

    Per file, in order: header read (``unreadable`` on failure), existing
    output (``exists``), the megapixel guard (``oversize``), else ``pending``.

    Args:
        source_dir: The input directory.
        scale: Integer upscale factor (the guard bounds OUTPUT pixels).
        max_output_megapixels: ``cfg.upscale.max_output_megapixels``.

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
    names = _output_names(paths)
    renamed_folders: set[Path] = set()
    items: list[ImageDirItem] = []
    for p in paths:
        name, renamed = names[p]
        if renamed and p.parent not in renamed_folders:
            renamed_folders.add(p.parent)
            _log.warning(
                "%s: stem collision; colliding files keep their full name + .png",
                p.parent,
            )
        output = out_dir / p.parent.relative_to(src) / name
        try:
            hdr = read_image_header(p)
        except Exception as exc:  # noqa: BLE001 — any Pillow failure is "unreadable"
            items.append(
                ImageDirItem(
                    p,
                    output,
                    0,
                    0,
                    "unreadable",
                    f"{type(exc).__name__}: {exc}",
                    renamed,
                )
            )
            continue
        if output.exists():
            disposition: Disposition = "exists"
            reason: str | None = None
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
            ImageDirItem(p, output, hdr.width, hdr.height, disposition, reason, renamed)
        )
    return ImageDirPlan(src, out_dir, tuple(items), skipped)


_PASSTHROUGH_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
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

    register_heif()
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
