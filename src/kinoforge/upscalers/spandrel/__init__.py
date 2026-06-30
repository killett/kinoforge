"""Spandrel upscaler package.

The full client engine lives in :mod:`._engine` and registers itself with
``kinoforge.core.registry`` at import. On-pod embeds (which only embed
``kinoforge.core.errors`` + ``.scale_target``) raise ``ImportError`` for the
engine deps; we swallow that so the pod can still import ``._runtime``
without the registry / interfaces / proxy-retry modules.
"""

from __future__ import annotations

# Re-export the client engine when its full dep tree is available. On-pod
# imports of ``._runtime`` first trigger this package init; the ImportError
# branch keeps the pod path working with the minimal embed set.
try:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    from ._engine import SpandrelEngine

    try:
        registry.register_upscaler("spandrel", SpandrelEngine)
    except UnknownAdapter:
        pass

    __all__ = ["SpandrelEngine"]
except ImportError:
    __all__ = []
