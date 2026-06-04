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
    """Resolves ``hf:<repo>:<path>`` refs to a single HuggingFace resolve URL.

    The ref format is::

        hf:<org>/<model>:<path/to/file>

    A bare ``hf:<org>/<model>`` ref (no file path) raises
    :class:`~kinoforge.core.errors.ValidationError` — directory listing is
    deferred to a later task.

    Attributes:
        scheme: Registry scheme key — ``"hf"``.
    """

    scheme = "hf"

    def handles(self, ref: str) -> bool:
        """Return ``True`` when *ref* starts with ``hf:`` followed by a repo path.

        Args:
            ref: The model reference string to test.

        Returns:
            ``True`` if *ref* matches ``^hf:[^:]+(:.*)?$``.
        """
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve a HuggingFace ref to a single :class:`~kinoforge.core.interfaces.Artifact`.

        Parses ``hf:<repo>:<path>`` and constructs the canonical HuggingFace
        resolve URL.  No network requests are made.

        Args:
            ref: The model reference string (e.g.
                ``"hf:Wan-AI/Wan2.2:diffusion/model.safetensors"``).
            creds: Credential provider; reads ``HF_TOKEN`` from it.

        Returns:
            A list containing exactly one :class:`~kinoforge.core.interfaces.Artifact`
            whose ``url`` is the HuggingFace resolve URL for the file.

        Raises:
            ValidationError: *ref* is a bare repo ref with no file path.
                Directory listing is DEFERRED; callers must specify a file path
                (e.g. ``hf:org/model:path/to/file``).
        """
        # Strip the leading "hf:" scheme prefix.
        remainder = ref[len("hf:") :]

        # Split into at most two parts: <repo> and <path>.
        parts = remainder.split(":", 1)
        repo = parts[0]

        if len(parts) < 2 or not parts[1]:
            # DEFERRED: directory listing via HF API
            raise ValidationError(
                f"No file path in HuggingFace ref {ref!r} — "
                "specify a file path (hf:repo:path/to/file). "
                "Directory listing is not yet supported."
            )

        path = parts[1]
        filename = path.rsplit("/", 1)[-1]
        url = f"{_HF_BASE}/{repo}/resolve/main/{path}"

        token: str | None = creds.get("HF_TOKEN")
        headers: dict[str, str] = {"Authorization": f"Bearer {token}"} if token else {}

        return [Artifact(url=url, filename=filename, headers=headers)]


# Self-register on import so a single ``import kinoforge.sources.huggingface``
# is enough for ``source_for_ref()`` to route HuggingFace refs without an
# explicit register call.
registry.register_source(HuggingFaceSource())
