"""Pure helpers for conditioning-asset discovery and URI resolution.

Used by per-engine ``submit()`` implementations to find the asset on
``segments[0].assets`` matching a declared role and resolve its URI to
bytes (when the engine needs to upload them) or pass the URI through
(when the engine's server fetches it).

This module is part of ``core`` and must never import any concrete
engine, provider, source, or store — verified by ``test_core_invariant``.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kinoforge.core.errors import AssetFetchError, ValidationError
from kinoforge.core.interfaces import ConditioningAsset, GenerationJob


def find_asset(job: GenerationJob, role: str) -> ConditioningAsset | None:
    """Return ``segments[0]``'s asset whose role matches, or ``None``.

    Looks at ``segments[0]`` only because the chain mechanism injects
    tail-frames there and the happy-path single-segment build attaches
    user-supplied request assets there too (see ``GenerateClipStage``).

    Args:
        job: The generation job to inspect.
        role: Exact role string to match.

    Returns:
        The matching :class:`ConditioningAsset`, or ``None`` if none
        carry that role.

    Raises:
        ValidationError: ``segments[0].assets`` contains more than one
            asset with the requested role.
    """
    if not job.segments:
        return None
    matches = [a for a in job.segments[0].assets if a.role == role]
    if len(matches) > 1:
        raise ValidationError(
            f"duplicate asset role {role!r} in segments[0]: "
            f"{len(matches)} entries found, expected at most 1"
        )
    return matches[0] if matches else None


def asset_bytes(
    uri: str,
    *,
    http_get_bytes: Callable[[str], bytes],
) -> bytes:
    """Resolve ``uri`` to raw bytes by scheme.

    ``http``/``https`` dispatch to the injected ``http_get_bytes``;
    ``file://`` reads via :class:`pathlib.Path`.  Any other scheme
    raises :class:`AssetFetchError`.

    Args:
        uri: URI to resolve (``http``, ``https``, or ``file``).
        http_get_bytes: Injected HTTP byte fetcher; tests pass spies.

    Returns:
        Raw bytes at the URI.

    Raises:
        AssetFetchError: Unsupported scheme, HTTP transport error, or
            missing local file.
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        try:
            return http_get_bytes(uri)
        except (urllib.error.URLError, OSError) as e:
            raise AssetFetchError(f"failed to fetch {uri}: {e}") from e
    if scheme == "file":
        path = Path(parsed.path)
        try:
            return path.read_bytes()
        except (FileNotFoundError, OSError) as e:
            raise AssetFetchError(f"failed to read {uri}: {e}") from e
    raise AssetFetchError(f"unsupported scheme {scheme!r} for asset URI {uri!r}")


def set_by_dot_path(
    body: dict[str, Any],
    dot_path: str,
    value: Any,  # noqa: ANN401 — leaf value is engine-defined (URL str, dict, etc.)
) -> None:
    """Write ``value`` at ``dot_path`` in ``body``, creating intermediate dicts.

    Mutation is in-place.  Caller is responsible for passing a copy if
    the original must remain unchanged.  A single-segment ``dot_path``
    (no ``"."``) writes at the top level.

    Args:
        body: Target dict (mutated in place).
        dot_path: Dot-separated key path (e.g. ``"input.image_url"``).
        value: Value to write at the leaf.
    """
    parts = dot_path.split(".")
    cursor: dict[str, Any] = body
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
