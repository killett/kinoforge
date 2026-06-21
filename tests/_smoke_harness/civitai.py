"""civitai.resolve — thin wrapper around CivitAISource."""

from __future__ import annotations

from typing import Any


def _civitai_source_factory() -> Any:
    """Test-seam — overridden in unit tests."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.sources.civitai import CivitAISource

    src = CivitAISource()
    creds = EnvCredentialProvider()

    class _Bound:
        def resolve(self, ref: str) -> Any:
            return src.resolve(ref, creds)

    return _Bound()


def resolve(ref: str) -> dict[str, Any]:
    """Resolve a civitai ref to a download_specs-shaped dict.

    Picks the first ``.safetensors`` artifact when present; falls back
    to the first artifact otherwise (some packs ship `.ckpt` only).
    """
    arts = _civitai_source_factory().resolve(ref)
    pick = next((a for a in arts if a.filename.endswith(".safetensors")), arts[0])
    return {
        "url": pick.url,
        "headers": dict(pick.headers or {}),
        "filename": pick.filename,
        "size_hint": pick.size,
    }
