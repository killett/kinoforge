"""FlashVSR upscaler package.

Full client engine in :mod:`._engine`; self-registers with
``kinoforge.core.registry`` at import. On-pod embeds (which only include
``kinoforge.core.errors`` + ``.scale_target``) raise ImportError for the
engine deps; swallow so the pod can still import ``._runtime`` /
``._fetch_weights`` without the full registry / interfaces / proxy-retry
modules.
"""

from __future__ import annotations

try:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    from ._engine import FlashVSREngine

    try:
        registry.register_upscaler("flashvsr", FlashVSREngine)
    except UnknownAdapter:
        pass

    __all__ = ["FlashVSREngine"]
except ImportError:
    __all__ = []
