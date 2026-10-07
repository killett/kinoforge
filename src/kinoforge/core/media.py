"""Media kind of an artifact: ``"video"`` or ``"image"``.

The upscale path carries the input kind as DATA rather than inferring it from
a filename at every hop. The convention lives here so the CLI (which stamps
it onto the input ``Artifact``), ``UpscaleStage`` (which reads it into the
``UpscaleJob``), the engine (which forwards it to the pod) and the
orchestrator (which picks the published extension from it) share one
definition and one default. Pod-side code never imports this module — the pod
receives the string in the ``/upscale`` request body.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from kinoforge.core.interfaces import Artifact

Media = Literal["video", "image"]
"""The two media kinds the upscale path understands."""

MEDIA_KEY = "media"
"""The ``Artifact.meta`` key the kind travels under."""

IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})
"""Suffixes ``kinoforge upscale --image`` accepts (lower-cased)."""

DIR_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jfif",
        ".webp",
        ".avif",
        ".gif",
        ".bmp",
        ".dib",
        ".tif",
        ".tiff",
        ".tga",
        ".heic",
        ".heif",
        ".jp2",
        ".j2k",
        ".psd",
        ".ico",
        ".pcx",
        ".pbm",
        ".pgm",
        ".ppm",
        ".pnm",
        ".qoi",
        ".dds",
    }
)
"""Suffixes ``kinoforge upscale --image-dir`` treats as images (lower-cased).

A curated set, deliberately NOT everything Pillow registers — that list
includes ``.mpg``, ``.pdf``, ``.h5`` and ``.ps``. Files with other suffixes are
counted and skipped, never refused. Anything outside :data:`IMAGE_SUFFIXES`
is re-encoded to PNG on the controller before upload.
"""

_EXTENSIONS: dict[str, str] = {"video": ".mp4", "image": ".png"}


def media_of(artifact: Artifact) -> Media:
    """Return the artifact's media kind; an absent key means ``"video"``.

    Args:
        artifact: Any artifact; only ``.meta`` is consulted.

    Returns:
        ``"video"`` or ``"image"``.

    Raises:
        ValueError: ``meta["media"]`` is present but not one of the two kinds.
    """
    raw = artifact.meta.get(MEDIA_KEY, "video")
    if raw not in get_args(Media):
        raise ValueError(
            f"artifact meta[{MEDIA_KEY!r}] must be one of {get_args(Media)}, "
            f"got {raw!r}"
        )
    return raw  # type: ignore[no-any-return]


def extension_for(media: Media) -> str:
    """Return the published file extension for *media* (``.png`` / ``.mp4``).

    Args:
        media: ``"video"`` or ``"image"``.

    Returns:
        The extension including the leading dot.
    """
    return _EXTENSIONS[media]


def local_artifact(path: Path, media: Media) -> Artifact:
    """Materialise a local file as an input ``Artifact`` stamped with *media*.

    ``file://`` absolute uri, sha256 from disk, size from ``stat``. Shared by
    the CLI's ``--video`` / ``--image`` resolver and the directory runner so
    neither re-implements the stamp.

    Args:
        path: Local file; resolved to an absolute path.
        media: ``"video"`` or ``"image"``.

    Returns:
        The artifact to seed into ``state.artifacts["clip"]``.
    """
    import hashlib

    from kinoforge.core.interfaces import Artifact

    p = Path(path).resolve()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Artifact(
        uri=f"file://{p}",
        sha256=h.hexdigest(),
        size=p.stat().st_size,
        meta={MEDIA_KEY: media},
    )
