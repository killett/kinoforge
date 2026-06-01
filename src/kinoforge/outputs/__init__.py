"""kinoforge.outputs — user-facing publish seam (Layer O).

Sibling axis to ``kinoforge.stores`` with the same shape:

* :mod:`kinoforge.outputs.base` holds the engine-agnostic Protocol +
  pure helpers (``slugify``, ``format_filename``, ``OutputPublishError``).
* :mod:`kinoforge.outputs.local` holds the default ``LocalOutputSink``
  and self-registers under ``"local"`` on import.
* :func:`register_sink` / :func:`get_sink` mirror the patterns proven
  by ``kinoforge.core.registry`` for stores, providers, sources,
  engines, and splitters.

Concrete sinks are imported only by ``kinoforge._adapters`` (the
concrete-import hub used by the CLI); ``kinoforge.core`` never imports
this module's submodules directly.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.errors import UnknownAdapter
from kinoforge.outputs.base import (
    OutputPublishError,
    OutputSink,
    format_filename,
    slugify,
)

__all__ = [
    "OutputPublishError",
    "OutputSink",
    "format_filename",
    "get_sink",
    "register_sink",
    "slugify",
]

_SINKS: dict[str, Callable[[], OutputSink]] = {}


def register_sink(name: str, factory: Callable[[], OutputSink]) -> None:
    """Register a zero-arg sink factory under *name*.

    Args:
        name: The registry key (lowercase, matches ``output.kind`` in YAML).
        factory: Zero-arg callable returning a fresh ``OutputSink`` instance.
    """
    _SINKS[name] = factory


def get_sink(name: str) -> Callable[[], OutputSink]:
    """Return the registered factory for *name*.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory; call it to produce an ``OutputSink``.

    Raises:
        UnknownAdapter: No sink is registered under *name*.
    """
    try:
        return _SINKS[name]
    except KeyError:
        raise UnknownAdapter(f"unknown output sink kind: {name!r}") from None
