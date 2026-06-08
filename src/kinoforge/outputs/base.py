"""OutputSink Protocol + pure helpers (slugify, format_filename) + errors.

This module is the engine-agnostic side of the publish seam: the Protocol
that GenerateClipStage depends on, plus pure functions any sink
implementation can compose.  No I/O, no concrete sink — see ``local.py``
for the default implementation.

Naming + sanitization conventions:

- ``slugify`` is ASCII-conservative on purpose: emoji, CJK, and accented
  characters are dropped (via ``encode("ascii", "ignore")``) rather than
  transliterated, because shell-quoting and grep/tab-complete ergonomics
  matter more for operator UX than filename expressiveness.  Cross-platform
  safety (Linux NFC vs macOS HFS+ NFD divergence) falls out of the same
  decision.
- ``format_filename`` separates the ``ts`` / ``slug`` / ``ext`` rendering
  from the collision-resolution logic in ``LocalOutputSink.publish`` so a
  future ``S3OutputSink`` can reuse the same naming and collide on its own
  terms.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol


class OutputPublishError(RuntimeError):
    """Raised when a sink cannot persist the bytes to its destination.

    Wraps the underlying OSError (or equivalent) so callers can catch a
    single, semantic exception type and decide whether to fail the run or
    fall back to a different sink.
    """


class OutputSink(Protocol):
    """Publish a finished clip with a user-facing filename.

    The contract: take in-memory bytes and a prompt-derived filename hint,
    place the file at the sink's destination, and return the absolute
    path or URI of the published file.  The sink owns its own clock,
    sanitization rules, and collision policy.

    Implementations MUST be idempotent under retry only in the sense that
    a second call with the same arguments produces a NEW path (via
    collision suffix) rather than overwriting — clip output must never be
    silently destroyed.
    """

    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Publish *data* under a name derived from *prompt*, *provider*, *model*.

        Args:
            data: The raw clip bytes to write.
            prompt: The user-facing prompt; first 20 ASCII-safe chars
                become the slug portion of the filename.
            extension: File suffix including the dot (e.g. ".mp4"); use
                ".bin" when the engine returns no extension.
            namespace: Optional sub-directory under the sink's root; used
                by ``batch_generate`` to group entries by ``batch_id``.
            provider: Engine registry key (``replicate`` / ``runway`` /
                ``luma`` / ``fal``). ``None`` or empty falls back to the
                literal ``"unknown"``.
            model: ``cfg["spec"]["model"]`` slugified to max 24 chars.
                ``None`` or empty falls back to the literal ``"unknown"``.

        Returns:
            The absolute path of the published file as a string.

        Raises:
            OutputPublishError: The sink could not write (read-only dir,
                disk full, permission denied, etc.).
        """
        ...


# slugify -------------------------------------------------------------------

# Characters allowed verbatim in the slug.  Everything else gets replaced
# with a dash before the collapse + trim passes.
_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_DASH_RUN = re.compile(r"-+")


def slugify(prompt: str, max_chars: int = 20) -> str:
    """Return an ASCII-conservative slug of *prompt* up to *max_chars*.

    Pipeline:

    1. NFC-normalize, then ``encode("ascii", "ignore").decode()`` to drop
       emoji, CJK, and accented characters.
    2. Replace each character not in ``[A-Za-z0-9._-]`` with ``-``.
    3. Collapse runs of ``-`` to a single ``-``.
    4. Strip leading/trailing ``-`` and ``.``.
    5. Truncate to ``max_chars``.
    6. Strip trailing ``-`` and ``.`` again (truncation may have landed
       inside a dash run).
    7. Return ``"clip"`` if the result is empty.

    Args:
        prompt: The free-text prompt to slugify.
        max_chars: Maximum length of the returned slug (default 20).

    Returns:
        A filesystem-safe slug, guaranteed non-empty and ASCII-only.
    """
    ascii_only = unicodedata.normalize("NFC", prompt).encode("ascii", "ignore").decode()
    replaced = "".join(c if c in _ALLOWED_CHARS else "-" for c in ascii_only)
    collapsed = _DASH_RUN.sub("-", replaced)
    trimmed = collapsed.strip("-.")
    truncated = trimmed[:max_chars]
    final = truncated.rstrip("-.")
    return final or "clip"


def format_filename(
    *,
    ts: str,
    provider: str,
    model: str,
    slug: str,
    extension: str,
) -> str:
    """Compose ``{ts}_{provider}_{model}_{slug}{extension}``.

    Caller MUST pre-slugify ``provider``, ``model``, and ``slug``; this
    helper performs no sanitisation.

    Args:
        ts: The local-TZ timestamp string, e.g. ``"20260531-210015"``.
        provider: Pre-slugified provider name (or literal ``"unknown"``).
        model: Pre-slugified model identifier (or literal ``"unknown"``).
        slug: The ASCII slug from :func:`slugify`.
        extension: File suffix including the dot (e.g. ``".mp4"``).

    Returns:
        The composed filename.
    """
    return f"{ts}_{provider}_{model}_{slug}{extension}"
