"""CivArchive model source — resolves ``civarchive:<modelId>@<versionId>`` refs.

CivArchive is the archival successor to CivitAI for content that may have been
removed from the original host. It shares CivitAI's numeric ``modelId`` /
``versionId`` scheme but publishes **no JSON metadata API**: this module
HTML-scrapes the canonical model-version page for the SHA256 hash and the
filename, then builds one :class:`~kinoforge.core.interfaces.Artifact` whose
``url`` points at ``civarchive.com/api/download/models/<vid>``. That endpoint
307-redirects to whichever host CivArchive currently mirrors (civitai.com,
HuggingFace, or its own ``/sha256/`` store) — keeping ``Artifact.url`` at the
abstract CivArchive endpoint preserves cache validity across CivArchive
re-mirror events.

Parser anchors (do NOT loosen without re-pinning the fixture):

- SHA256: ``<a href="/sha256/<hex64>">`` — the ``/sha256/`` URL path is part
  of CivArchive's own routing.
- Filename: ``<h2 class="font-semibold text-xl ...">NAME.EXT<a`` for EXT in
  ``{safetensors, ckpt, pt, bin, gguf}``. The trailing ``<a`` is the
  "Search for this file across all platforms" link civarchive renders next
  to the filename — mirror filenames (rendered inside ``<p>`` further down
  the page) lack this link and are therefore excluded.
"""

from __future__ import annotations

import re

from kinoforge.core.errors import KinoforgeError

# ---------------------------------------------------------------------------
# Ref pattern (mirrors civitai source)
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^civarchive:(\d+)(?:@(\d+))?$")

# ---------------------------------------------------------------------------
# HTML parser helpers
# ---------------------------------------------------------------------------

_SHA256_HREF_RE = re.compile(r'href="/sha256/([0-9a-f]{64})"')
_FILENAME_RE = re.compile(
    r'<h2 class="font-semibold text-xl[^"]*">'
    r"([^<>\s]+\.(?:safetensors|ckpt|pt|bin|gguf))<a"
)


def _extract_sha256(html: str) -> str:
    """Extract the sha256 hex from the CivArchive HTML body.

    Args:
        html: HTML text from a CivArchive model-version page.

    Returns:
        The 64-char lowercase hex hash.

    Raises:
        KinoforgeError: No ``/sha256/<hex64>`` anchor is present.
    """
    m = _SHA256_HREF_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing /sha256/ anchor — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)


def _extract_filename(html: str) -> str:
    """Extract the model filename from the CivArchive HTML body.

    Args:
        html: HTML text from a CivArchive model-version page.

    Returns:
        The filename (e.g. ``"model.safetensors"``).

    Raises:
        KinoforgeError: No ``<h2 class="font-semibold text-xl">NAME.EXT<a``
            anchor is present for a recognised model file extension.
    """
    m = _FILENAME_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing model filename — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)
