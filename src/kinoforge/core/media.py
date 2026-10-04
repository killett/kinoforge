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

from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from kinoforge.core.interfaces import Artifact

Media = Literal["video", "image"]
"""The two media kinds the upscale path understands."""

MEDIA_KEY = "media"
"""The ``Artifact.meta`` key the kind travels under."""

IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})
"""Suffixes ``kinoforge upscale --image`` accepts (lower-cased)."""

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
