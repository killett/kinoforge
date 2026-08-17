"""Runtime registry: name (providers/engines) + ref-dispatch (sources) -> impl.

Adapters self-register via ``register_*`` at import time. Core resolves by
name (providers/engines) or by asking each source ``handles(ref)`` and
returning the first match (sources). It MUST NEVER import a concrete adapter
module.

Sources use behavioural dispatch rather than a name-keyed lookup because the
same ref (e.g. ``https://...``) may be claimed by more than one source, and a
source may legitimately handle multiple schemes; the choice belongs in the
source, not the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import (
    ComputeProvider,
    GenerationEngine,
    InterpolatorEngine,
    ModelSource,
    Splitter,
    UpscalerEngine,
)
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ImageEngine

_providers: dict[str, Callable[[], ComputeProvider]] = {}
_provider_classes: dict[str, type[ComputeProvider]] = {}
_engines: dict[str, Callable[[], GenerationEngine]] = {}
_sources: list[ModelSource] = []
_artifact_stores: dict[str, Callable[[], ArtifactStore]] = {}
_splitters: dict[str, Callable[[], Splitter]] = {}
_upscalers: dict[str, Callable[[], UpscalerEngine]] = {}
_interpolators: dict[str, Callable[[], InterpolatorEngine]] = {}


def register_provider(
    name: str,
    factory: Callable[[], ComputeProvider],
    provider_cls: type[ComputeProvider],
) -> None:
    """Register a compute provider factory + class under ``name`` (overwrites).

    The class is stored alongside the factory so capability lookups can be
    answered by name without constructing a provider — the reaper must be
    able to ask in a process that cannot reach the provider at all.

    Args:
        name: The registry key for this provider.
        factory: Zero-arg callable that returns a ``ComputeProvider`` instance.
        provider_cls: The class itself, for class-level capability lookup.
    """
    _providers[name] = factory
    _provider_classes[name] = provider_cls


def provider_class(name: str) -> type[ComputeProvider] | None:
    """Return the registered class for ``name``, or None if unregistered.

    Unlike :func:`get_provider` this never raises — callers (capability
    lookup) treat an unknown provider as "declares nothing".

    What comes back is whatever :func:`register_provider` was handed; nothing
    enforces that it is a ``ComputeProvider`` subclass at runtime (tests
    register bare ``object``). Callers must therefore read ``capabilities`` /
    ``billed`` off it defensively — ``core/capabilities.py`` does, via
    ``getattr`` with the documented defaults.
    """
    return _provider_classes.get(name)


def provider_names() -> list[str]:
    """Return the names of every currently-registered compute provider.

    Used by surfaces that need to probe every backend (e.g.
    ``kinoforge destroy --id`` on an orphan pod whose source provider
    is not recorded in the local ledger).
    """
    return list(_providers.keys())


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
    """Register a model source instance.

    The source is stored as an instance (not a factory) because routing goes
    through ``source.handles(ref)`` — the registry needs a live object to ask.
    An existing entry sharing ``source.scheme`` is replaced so module re-imports
    are idempotent.

    Args:
        source: The ``ModelSource`` instance to register. Its ``.scheme``
            attribute is used to deduplicate on re-registration.
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


def register_store(name: str, factory: Callable[[], ArtifactStore]) -> None:
    """Register an artifact-store factory under ``name`` (overwrites).

    Args:
        name: The registry key for this store (e.g. ``"local"``).
        factory: Zero-arg callable that returns an :class:`~kinoforge.stores.base.ArtifactStore`
            instance.  Construction is deferred — the factory is called only when
            the caller invokes ``get_store(name)()``.
    """
    _artifact_stores[name] = factory


def get_store(name: str) -> Callable[[], ArtifactStore]:
    """Return the artifact-store factory for ``name`` or raise ``UnknownAdapter``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: No artifact store is registered under ``name``.
    """
    try:
        return _artifact_stores[name]
    except KeyError:
        raise UnknownAdapter(f"no artifact store registered: {name!r}") from None


def register_splitter(name: str, factory: Callable[[], Splitter]) -> None:
    """Register a splitter factory under ``name`` (overwrites).

    Args:
        name: The registry key for this splitter.
        factory: Zero-arg callable that returns a ``Splitter`` instance.
    """
    _splitters[name] = factory


def get_splitter(name: str) -> Callable[[], Splitter]:
    """Return the splitter factory for ``name`` or raise ``UnknownAdapter``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: No splitter is registered under ``name``.
    """
    try:
        return _splitters[name]
    except KeyError:
        raise UnknownAdapter(f"no splitter registered: {name!r}") from None


# --- image engines (Layer R) --------------------------------------------------

_image_engines: dict[str, Callable[[], ImageEngine]] = {}


def register_image_engine(name: str, factory: Callable[[], ImageEngine]) -> None:
    """Register an image engine under ``name``.

    Mirrors :func:`register_engine` shape. Separate registry namespace from
    video engines — names may collide across the two (e.g. ``"fake"`` engine
    coexists with ``"fake"`` image engine without conflict).

    Args:
        name: The registry key for this image engine.
        factory: Zero-arg callable that returns an ``ImageEngine`` instance.
    """
    _image_engines[name] = factory


def get_image_engine(name: str) -> Callable[[], ImageEngine]:
    """Return the registered factory for image engine ``name``.

    Args:
        name: The registry key to look up.

    Returns:
        The zero-arg factory registered under ``name``.

    Raises:
        UnknownAdapter: ``name`` is not registered.
    """
    try:
        return _image_engines[name]
    except KeyError:
        raise UnknownAdapter(f"no image engine registered: {name!r}") from None


def register_upscaler(name: str, factory: Callable[[], UpscalerEngine]) -> None:
    """Register an upscaler factory under ``name``.

    Unlike :func:`register_engine`, duplicate registration is rejected so
    that adapter import-order accidents surface loudly rather than silently
    overwriting the production binding.

    Args:
        name: Registry key (e.g. ``"seedvr2"``).
        factory: Zero-arg callable returning an :class:`UpscalerEngine`.

    Raises:
        UnknownAdapter: ``name`` is already registered.
    """
    if name in _upscalers:
        raise UnknownAdapter(f"upscaler {name!r} already registered")
    _upscalers[name] = factory


def get_upscaler(name: str) -> Callable[[], UpscalerEngine]:
    """Return the factory for ``name``.

    Raises:
        UnknownAdapter: No upscaler registered under ``name``.
    """
    try:
        return _upscalers[name]
    except KeyError:
        raise UnknownAdapter(
            f"no upscaler registered as {name!r}; known: {sorted(_upscalers)}"
        ) from None


def upscaler_names() -> list[str]:
    """Return all registered upscaler names, sorted."""
    return sorted(_upscalers)


def register_interpolator(name: str, factory: Callable[[], InterpolatorEngine]) -> None:
    """Register an interpolator factory under ``name``.

    Duplicate registration is rejected so adapter import-order accidents surface
    loudly rather than silently overwriting the production binding.

    Args:
        name: Registry key (e.g. ``"rife"``).
        factory: Zero-arg callable returning an :class:`InterpolatorEngine`.

    Raises:
        UnknownAdapter: ``name`` is already registered.
    """
    if name in _interpolators:
        raise UnknownAdapter(f"interpolator {name!r} already registered")
    _interpolators[name] = factory


def get_interpolator(name: str) -> Callable[[], InterpolatorEngine]:
    """Return the factory for ``name``.

    Raises:
        UnknownAdapter: No interpolator registered under ``name``.
    """
    try:
        return _interpolators[name]
    except KeyError:
        raise UnknownAdapter(
            f"no interpolator registered as {name!r}; known: {sorted(_interpolators)}"
        ) from None


def interpolator_names() -> list[str]:
    """Return all registered interpolator names, sorted."""
    return sorted(_interpolators)
