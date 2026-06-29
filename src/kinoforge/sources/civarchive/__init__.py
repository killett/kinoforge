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
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core.errors import AuthError, KinoforgeError

# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

FetchHTMLCallable = Callable[[str, dict[str, str]], str]


def _urllib_fetch_html(url: str, headers: dict[str, str]) -> str:
    """Fetch *url* with GET, return the response body as a UTF-8 string.

    Args:
        url: The endpoint URL.
        headers: HTTP request headers to include. The default
            ``User-Agent: kinoforge-smoke/0.1`` is prepended but a
            caller-supplied ``User-Agent`` overrides it.

    Returns:
        Decoded UTF-8 body.

    Raises:
        AuthError: The server returned HTTP 401.
        KinoforgeError: Any other non-2xx HTTP error or network failure.
    """
    # CivArchive is Cloudflare-fronted (same gating as civitai); the default
    # Python-urllib UA gets a 403. Mirror the civitai source pattern.
    request_headers = {"User-Agent": "kinoforge-smoke/0.1", **headers}
    req = Request(url, headers=request_headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310 — only civarchive.com HTTPS URLs used
            body: bytes = resp.read()
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(f"CivArchive 401 Unauthorized for {url}") from exc
        raise KinoforgeError(f"CivArchive HTTP {exc.code} for {url}") from exc
    return body.decode("utf-8")


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


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

from kinoforge.core.interfaces import (  # noqa: E402
    Artifact,
    CredentialProvider,
    ModelSource,
)


class CivArchiveSource(ModelSource):
    """Resolves ``civarchive:<modelId>@<versionId>`` refs via HTML scrape.

    CivArchive publishes no JSON metadata API; this Source fetches the
    canonical model-version HTML page and parses the SHA256 + filename out
    of structural anchors. ``Artifact.url`` is kept at
    ``civarchive.com/api/download/models/<vid>`` so CivArchive's own 307
    redirect chain owns host indirection (preserving cache validity across
    re-mirror events).

    The optional ``fetch`` parameter injects the HTTP transport. Tests pass
    a spy that returns canned HTML; the default ``_urllib_fetch_html`` uses
    ``urllib.request`` from the stdlib.

    Bare refs (``civarchive:N`` with no ``@<versionId>``) are
    parse-accepted (``handles`` returns True) but rejected at resolve with
    a clear error — symmetric with the URL-normalisation layer from
    sub-project A.

    Attributes:
        scheme: Registry scheme key — ``"civarchive"``.
    """

    scheme = "civarchive"

    def __init__(
        self,
        *,
        fetch: FetchHTMLCallable = _urllib_fetch_html,
    ) -> None:
        """Initialise the source with an optional transport override.

        Args:
            fetch: Callable ``(url, headers) -> str`` used to perform HTTP
                requests. Defaults to :func:`_urllib_fetch_html`.
        """
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        """Return True when *ref* matches ``civarchive:<digits>[@<digits>]``.

        Args:
            ref: The model reference string to test.

        Returns:
            True if *ref* is a well-formed civarchive ref.
        """
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve a CivArchive ref to one :class:`Artifact`.

        Args:
            ref: The model reference string (e.g.
                ``"civarchive:2197303@2474081"``).
            creds: Credential provider; ``CIVITAI_TOKEN`` (if present) is
                copied into ``Artifact.headers`` for the eventual download.

        Returns:
            A single-element list containing the resolved :class:`Artifact`.

        Raises:
            ValueError: *ref* does not match the civarchive regex.
            KinoforgeError: *ref* is a bare ``civarchive:N`` (no
                ``@<versionId>``), or the upstream HTTP fetch failed, or
                the HTML did not contain a recognised SHA256 / filename
                anchor.
            AuthError: The HTTP fetch returned 401 (unexpected for the
                anonymous HTML endpoint, retained for symmetry with the
                civitai source).
        """
        m = _REF_RE.match(ref)
        if m is None:
            raise ValueError(f"Not a valid civarchive ref: {ref!r}")

        model_id_str, version_id_str = m.group(1), m.group(2)
        if version_id_str is None:
            raise KinoforgeError(
                f"civarchive ref {ref!r} requires @<versionId>; "
                "civarchive does not expose a stable default-version "
                "selector"
            )

        # HTML fetch is anonymous — civarchive does not gate metadata pages.
        page_url = (
            f"https://civarchive.com/models/{model_id_str}"
            f"?modelVersionId={version_id_str}"
        )
        html = self._fetch(page_url, {})

        sha256 = _extract_sha256(html)
        filename = _extract_filename(html)

        # CIVITAI_TOKEN is attached to Artifact.headers — mirrors the
        # CivitAI source pattern so the downloader can authenticate when
        # CivArchive's 307 lands on civitai.com.
        token: str | None = creds.get("CIVITAI_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

        return [
            Artifact(
                filename=filename,
                url=(f"https://civarchive.com/api/download/models/{version_id_str}"),
                size=None,
                sha256=sha256,
                headers=headers,
            )
        ]


# Self-register on import so a single ``import kinoforge.sources.civarchive``
# is enough for ``source_for_ref()`` to route CivArchive refs without an
# explicit register call. Mirrors the civitai pattern.
from kinoforge.core import registry  # noqa: E402

registry.register_source(CivArchiveSource())
