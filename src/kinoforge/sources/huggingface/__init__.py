"""HuggingFace model source — resolves ``hf:<repo>[:<path>][@<rev>]`` refs.

Single-file refs (``hf:<repo>:<path>`` or ``hf:<repo>@<rev>:<path>``)
construct the canonical HuggingFace resolve URL directly with no HTTP
calls.  Bare-repo refs (``hf:<repo>`` or ``hf:<repo>@<rev>``) enumerate
the repo tree via the HuggingFace tree API and emit one Artifact per
file, with content SHA256 auto-populated from LFS metadata when present.

Example ref formats::

    hf:Wan-AI/Wan2.2:diffusion/model.safetensors
    hf:Wan-AI/Wan2.2@v1.0:diffusion/model.safetensors
    hf:Wan-AI/Wan2.2                                  # bare, revision = main
    hf:Wan-AI/Wan2.2@<sha>                            # bare, pinned

The HTTP transport for tree listing is injected via the ``fetch``
constructor parameter so tests can pass a stub without touching the
network.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core import registry
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource

# ---------------------------------------------------------------------------
# Ref pattern
# ---------------------------------------------------------------------------

# Matches anything starting with "hf:" followed by at least one non-colon
# character (the repo path, optionally with @rev), with an optional ":path"
# suffix.  Bare-repo refs (no ":path") are recognised here; resolve() decides
# whether to dispatch single-file or tree-listing.
_REF_RE = re.compile(r"^hf:[^:]+(:.*)?$")

_HF_BASE = "https://huggingface.co"


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------

FetchCallable = Callable[
    [str, dict[str, str]],
    tuple[list[dict[str, Any]], str | None],
]


def _next_cursor_from_link(link_header: str) -> str | None:
    """Extract the ``cursor`` query-param from a ``Link: <...>; rel="next"`` header.

    Args:
        link_header: The raw ``Link`` response-header string, possibly empty.

    Returns:
        The URL-decoded cursor token from the ``rel="next"`` entry's URL,
        or ``None`` when no such entry is present.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if not m:
            continue
        parsed = urllib.parse.urlparse(m.group(1))
        qs = urllib.parse.parse_qs(parsed.query)
        cursor = qs.get("cursor", [None])[0]
        return cursor
    return None


def _urllib_fetch_json(
    url: str, headers: dict[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch *url* with GET, return ``(parsed_json_list, next_cursor_or_None)``.

    Args:
        url: The endpoint URL.
        headers: HTTP request headers to include.

    Returns:
        ``(entries, next_cursor)`` where *entries* is the parsed JSON array
        body and *next_cursor* is extracted from the ``Link`` response header.

    Raises:
        AuthError: The server returned HTTP 401.
        KinoforgeError: Any other non-2xx HTTP error or network failure.
    """
    req = Request(url, headers=headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310 — only huggingface.co HTTPS URLs used
            body: bytes = resp.read()
            link_header: str = resp.headers.get("Link", "") or ""
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(f"HuggingFace 401 Unauthorized for {url}") from exc
        raise KinoforgeError(f"HuggingFace HTTP {exc.code} for {url}") from exc
    parsed: list[dict[str, Any]] = json.loads(body.decode("utf-8"))
    return parsed, _next_cursor_from_link(link_header)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_hf_ref(ref: str) -> tuple[str, str, str | None]:
    """Parse a HuggingFace ref into ``(repo, revision, path_or_None)``.

    The grammar is::

        hf:<repo>                          → (repo, "main", None)
        hf:<repo>@<revision>               → (repo, revision, None)
        hf:<repo>:<path>                   → (repo, "main", path)
        hf:<repo>@<revision>:<path>        → (repo, revision, path)

    Split order: ``:`` first (path separator), then ``@`` on the head
    (revision separator).  ``@`` is legal inside HuggingFace paths and must
    not be claimed as a revision marker.

    Args:
        ref: The HuggingFace reference string, e.g. ``"hf:org/repo@v1.0:path/file.bin"``.

    Returns:
        ``(repo, revision, path_or_None)`` triple.
    """
    remainder = ref[len("hf:") :]
    repo_rev, _, path = remainder.partition(":")
    path_or_none: str | None = path or None
    if "@" in repo_rev:
        repo, _, revision = repo_rev.partition("@")
    else:
        repo, revision = repo_rev, "main"
    return repo, revision, path_or_none


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class HuggingFaceSource(ModelSource):
    """Resolves ``hf:<repo>[@<rev>][:<path>]`` refs to one or more Artifacts.

    Single-file refs return exactly one Artifact whose URL is the canonical
    HuggingFace resolve URL for that file at the parsed revision.  Bare
    refs are routed through the tree-listing branch added in Task 4.

    Attributes:
        scheme: Registry scheme key — ``"hf"``.
    """

    scheme = "hf"

    def __init__(self, *, fetch: FetchCallable = _urllib_fetch_json) -> None:
        """Initialise the source with an optional transport override.

        Args:
            fetch: Callable used to perform tree-listing HTTP requests.
                Defaults to :func:`_urllib_fetch_json`.  Unused on the
                single-file branch.
        """
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        """Return ``True`` when *ref* matches ``^hf:[^:]+(:.*)?$``."""
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve *ref* to a list of Artifacts.

        Single-file refs return a single-element list; bare-repo refs raise
        ``ValidationError`` until Task 4 lands the tree branch.

        Args:
            ref: The HuggingFace reference string.
            creds: Credential provider; reads ``HF_TOKEN`` from it.

        Returns:
            List of :class:`~kinoforge.core.interfaces.Artifact` objects.

        Raises:
            ValidationError: *ref* is a bare repo ref (no file path).
        """
        repo, revision, path = _parse_hf_ref(ref)
        token: str | None = creds.get("HF_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

        if path is None:
            # DEFERRED to Task 4: directory listing via HF tree API.
            raise ValidationError(
                f"No file path in HuggingFace ref {ref!r} — "
                "specify a file path (hf:repo:path/to/file). "
                "Directory listing is not yet supported."
            )

        return [self._single_file_artifact(repo, revision, path, headers)]

    def _single_file_artifact(
        self,
        repo: str,
        revision: str,
        path: str,
        headers: dict[str, str],
    ) -> Artifact:
        """Build the canonical resolve-URL Artifact for one file.

        Args:
            repo: ``<org>/<name>`` HuggingFace repo identifier.
            revision: Branch, tag, or commit SHA.
            path: Relative file path within the repo.
            headers: HTTP headers (``Authorization`` when ``HF_TOKEN`` set).

        Returns:
            A single :class:`~kinoforge.core.interfaces.Artifact` whose
            ``filename`` is the leaf of *path* (existing flatten contract).
        """
        filename = path.rsplit("/", 1)[-1]
        url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
        return Artifact(url=url, filename=filename, headers=dict(headers))


# Self-register on import so a single ``import kinoforge.sources.huggingface``
# is enough for ``source_for_ref()`` to route HuggingFace refs without an
# explicit register call.
registry.register_source(HuggingFaceSource())
