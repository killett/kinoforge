"""RIFE v4 interpolator package.

Full client engine in :mod:`._engine`; self-registers with
``kinoforge.core.registry`` at import. On-pod embeds (which only include
``kinoforge.core.errors`` + ``.fps_resolver``) raise ImportError for the engine
deps; swallow so the pod can still import ``._runtime`` without the full
registry / interfaces / proxy-retry modules (mirrors the flashvsr package).
"""

from __future__ import annotations

try:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    from ._engine import RifeEngine

    try:
        registry.register_interpolator("rife", RifeEngine)
    except UnknownAdapter:
        pass

    __all__ = ["RifeEngine"]
except ImportError:
    __all__ = []
