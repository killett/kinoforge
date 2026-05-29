"""Runtime registry: name (providers/engines) + scheme (sources) -> impl.

Adapters self-register via ``register_*`` at import time. Core resolves by
name/scheme only and MUST NEVER import a concrete adapter module.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import ComputeProvider, GenerationEngine, ModelSource

_providers: dict[str, Callable[[], ComputeProvider]] = {}
_engines: dict[str, Callable[[], GenerationEngine]] = {}
_sources: list[ModelSource] = []


def register_provider(name: str, factory: Callable[[], ComputeProvider]) -> None:
    """Register a compute provider factory under ``name`` (overwrites).

    Args:
        name: The registry key for this provider.
        factory: Zero-arg callable that returns a ``ComputeProvider`` instance.
    """
    _providers[name] = factory


def get_provider(name: str) -> Callable[[], ComputeProvider]:
    """Return the provider factory for ``name`` or raise ``UnknownAdapter``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: No provider is registered under ``name``.
    """
    try:
        return _providers[name]
    except KeyError:
        raise UnknownAdapter(f"no compute provider registered: {name!r}") from None


def register_engine(name: str, factory: Callable[[], GenerationEngine]) -> None:
    """Register a generation engine factory under ``name`` (overwrites).

    Args:
        name: The registry key for this engine.
        factory: Zero-arg callable that returns a ``GenerationEngine`` instance.
    """
    _engines[name] = factory


def get_engine(name: str) -> Callable[[], GenerationEngine]:
    """Return the engine factory for ``name`` or raise ``UnknownAdapter``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: No engine is registered under ``name``.
    """
    try:
        return _engines[name]
    except KeyError:
        raise UnknownAdapter(f"no generation engine registered: {name!r}") from None


def register_source(source: ModelSource) -> None:
    """Register a model source; an existing entry with the same scheme is replaced.

    Args:
        source: The ``ModelSource`` instance to register. Its ``.scheme`` attribute
            is used to deduplicate on re-registration.
    """
    global _sources
    _sources = [s for s in _sources if s.scheme != source.scheme] + [source]


def source_for_ref(ref: str) -> ModelSource:
    """Return the source whose ``handles(ref)`` is True or raise ``UnknownAdapter``.

    Args:
        ref: The model reference string to route.

    Returns:
        The first registered ``ModelSource`` whose ``handles(ref)`` returns ``True``.

    Raises:
        UnknownAdapter: No registered source handles ``ref``.
    """
    for s in _sources:
        if s.handles(ref):
            return s
    raise UnknownAdapter(f"no model source handles ref: {ref!r}")
