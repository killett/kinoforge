"""Resolve an image block into a live (engine, backend, profile) triple.

Shared by three call sites: ``orchestrator.generate`` and ``batch`` (which pass
``cfg.keyframe``) and ``image_run.generate_image`` (which passes ``cfg.image``).
Both config types are :class:`~kinoforge.core.config.ImageConfig` — a keyframe
spec IS an image spec plus per-role overrides — so the parameter type is nominal
rather than a structural Protocol.

Lives in its own neutral module rather than in ``core/image_run.py`` so the
video path (orchestrator, batch) never imports the standalone-image command's
module. The dependency arrows point at a shared seam, not at a sibling feature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.config import ImageConfig
    from kinoforge.core.interfaces import (
        ImageBackend,
        ImageEngine,
        ImageProfile,
        ImageProfileProvider,
    )
    from kinoforge.stores.base import ArtifactStore


def resolve_image_stack(
    block: ImageConfig,
    *,
    store: ArtifactStore,
    image_engine: ImageEngine | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
) -> tuple[ImageEngine, ImageBackend, ImageProfile]:
    """Resolve *block* into a provisioned engine, a live backend and a profile.

    The registry lookup happens FIRST so an unregistered engine name fails
    before any provisioning, profile-cache write or live probe — an image run
    costs money the moment the backend is reached.

    Args:
        block: The image block to resolve (``cfg.image`` or ``cfg.keyframe``).
        store: Artifact store backing the default profile cache.
        image_engine: Pre-constructed engine (test injection). When ``None``,
            resolved from the image-engine registry via ``block.engine``.
        image_profile_provider: Profile cache (test injection). When ``None``,
            a :class:`~kinoforge.core.profiles.JsonImageProfileCache` over
            *store*.

    Returns:
        ``(engine, backend, profile)``.

    Raises:
        UnknownAdapter: ``block.engine`` is not a registered image engine.
    """
    from kinoforge.core import registry
    from kinoforge.core.errors import ProfileNotCached
    from kinoforge.core.profiles import JsonImageProfileCache

    engine = (
        image_engine
        if image_engine is not None
        else registry.get_image_engine(block.engine)()
    )
    cfg_dict = block.model_dump()
    engine.provision(None, cfg_dict)
    backend = engine.backend(None, cfg_dict)

    key = block.capability_key()
    provider: ImageProfileProvider = (
        image_profile_provider
        if image_profile_provider is not None
        else JsonImageProfileCache(store)  # type: ignore[assignment]
    )
    try:
        profile = provider.resolve(key)
    except ProfileNotCached:
        profile = provider.discover(key, engine, backend)
    return engine, backend, profile
