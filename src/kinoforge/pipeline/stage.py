"""Pipeline stage protocol — re-export from kinoforge.core.interfaces.

The :class:`Stage` protocol is defined once in ``interfaces.py`` as the single
source of truth. This module re-exports it so pipeline-layer code can import
from ``kinoforge.pipeline.stage`` without creating an import dependency on the
core layer in the other direction.
"""

from kinoforge.core.interfaces import Stage  # noqa: F401  re-export

__all__ = ["Stage"]
