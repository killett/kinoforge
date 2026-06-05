"""Shared artifact-bytes resolver (Layer R extraction).

Resolves an :class:`~kinoforge.core.interfaces.Artifact` to its raw bytes via
three fallback paths: ``artifact.uri`` (file://) → ``artifact.url`` (http(s)
via an injected seam) → deterministic synthetic bytes (FakeEngine tests).

Originally lived as ``GenerateClipStage._artifact_bytes``; extracted in
Layer R so :class:`~kinoforge.pipeline.keyframe.KeyframeStage` and any future
stage reuse it without re-implementing the resolution rules.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from kinoforge.core.interfaces import Artifact

_DEFAULT_USER_AGENT = "kinoforge/0.1"


def _default_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
    """GET ``url`` with optional ``headers`` and return raw bytes.

    Injects a default ``User-Agent: kinoforge/0.1`` because edge proxies on
    RunPod / fal reject the stdlib default ``Python-urllib/<ver>`` with HTTP 403.
    """
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    req = urllib.request.Request(url, headers=merged)  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return bytes(resp.read())


def artifact_bytes(
    artifact: Artifact,
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None,
) -> bytes:
    """Resolve an Artifact's bytes via uri→file / url→http / synthetic fallback.

    Args:
        artifact: The artifact to resolve.
        http_get_bytes: Optional injectable HTTP GET seam.  When ``None``,
            :func:`_default_http_get_bytes` is used.

    Returns:
        The raw bytes addressed by the artifact.
    """
    uri = (artifact.uri or "").strip()
    if uri:
        parsed = urllib.parse.urlparse(uri)
        local_path: str | None = None
        if parsed.scheme == "file":
            local_path = urllib.request.url2pathname(parsed.path)
        elif parsed.scheme == "" and uri:
            local_path = uri
        if local_path is not None:
            candidate = Path(local_path)
            if candidate.exists():
                return candidate.read_bytes()

    url = (artifact.url or "").strip()
    if url.startswith(("http://", "https://")):
        fetch = http_get_bytes or _default_http_get_bytes
        return fetch(url, dict(artifact.headers))

    return (
        artifact.filename.encode("utf-8")
        + b"|"
        + repr(sorted(artifact.meta.items())).encode("utf-8")
    )
