"""Terminal image generation: one prompt in, one artifact out.

Deliberately carries none of the video pipeline's cross-stage plumbing — no
chained-stage state object, no compute-session attach call, no synthesized
placeholder request, no ledger row and no heartbeat — every image engine
declares ``requires_compute = False``, so all of that machinery is dead
weight here. The video side's chained-stage state exists to thread one
stage's output into the next stage's input; a terminal image has nothing
to chain, so it needs no state object to thread through. Image-then-upscale
would be file hand-off between two commands, the way the existing video
chains work.

A structural test (``tests/core/test_image_run.py::
test_module_has_no_pipeline_machinery``) greps this file's raw source for
the literal names of that plumbing, so this docstring must avoid spelling
them out even in prose explaining their absence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from kinoforge.core.errors import ValidationError
from kinoforge.core.image_stack import resolve_image_stack
from kinoforge.core.interfaces import ImageJob
from kinoforge.pipeline.artifact_bytes import artifact_bytes

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.config import Config, ImageConfig
    from kinoforge.core.interfaces import (
        Artifact,
        ImageEngine,
        ImageProfileProvider,
    )
    from kinoforge.outputs.base import OutputSink
    from kinoforge.stores.base import ArtifactStore

logger = logging.getLogger(__name__)

_MODE = "t2i"
_STORE_FILENAME = "image.png"


def generate_image(
    cfg: Config,
    *,
    store: ArtifactStore,
    run_id: str,
    sink: OutputSink | None,
    namespace: str | None = None,
    prompt_override: str | None = None,
    image_engine: ImageEngine | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    cancel_token: CancelToken | None = None,
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None,
) -> Artifact:
    """Generate one image from ``cfg.image`` and publish it.

    Args:
        cfg: Loaded config carrying an ``image:`` block.
        store: Artifact store for the internal copy.
        run_id: Run identifier namespacing the stored artifact.
        sink: User-facing output sink, or ``None`` when publishing is disabled.
        namespace: Optional sink subdirectory.
        prompt_override: The CLI ``--prompt`` value; wins over config prompts.
        image_engine: Pre-constructed engine (test injection).
        image_profile_provider: Profile cache (test injection).
        cancel_token: Honoured across the engine's poll loop.
        http_get_bytes: Injectable HTTP GET seam for fetching the image bytes.

    Returns:
        The stored :class:`~kinoforge.core.interfaces.Artifact`.

    Raises:
        ValidationError: No ``image:`` block, no resolvable prompt, or the
            resolved profile does not support ``t2i``.
        UnknownAdapter: ``cfg.image.engine`` is not a registered image engine.
    """
    block = cfg.image
    if block is None:
        raise ValidationError(
            "generate_image requires an `image:` block in the config; "
            "this config has none"
        )

    prompt = _resolve_prompt(cfg, block, prompt_override)

    engine, backend, profile = resolve_image_stack(
        block,
        store=store,
        image_engine=image_engine,
        image_profile_provider=image_profile_provider,
    )

    # Gate BEFORE submit. This is the one consumer ImageProfile has ever had:
    # pipeline/keyframe.py holds the field and never reads it, and every
    # engine's validate_spec checks only spec.model plus a non-empty prompt.
    # Mirrors core/validation.py's mode check on the video side.
    if _MODE not in profile.supported_modes:
        raise ValidationError(
            f"image engine {block.engine!r} model "
            f"{block.spec.get('model', '?')!r} does not support {_MODE!r}; "
            f"profile {profile.name!r} supports: "
            f"{sorted(profile.supported_modes)}"
        )

    cfg_dict = block.model_dump()
    job = ImageJob(spec=block.spec, prompt=prompt, params=block.params)
    engine.validate_spec(job)

    job_id = backend.submit(job)
    artifact = backend.result(job_id, cancel_token=cancel_token)
    png_bytes = artifact_bytes(artifact, http_get_bytes)

    # kinoforge:public-name — a fixed identifier, not prompt-derived.
    stored = store.put_bytes(run_id, _STORE_FILENAME, png_bytes)

    if sink is not None:
        model = engine.model_identity(cfg_dict)
        if not model:
            logger.warning(
                "image engine %r returned an empty model_identity; the "
                "published filename will render its model slug as 'unknown' "
                "(see successful-generations.md entry 17)",
                block.engine,
            )
        sink.publish(
            png_bytes,
            prompt=prompt,
            extension=".png",
            namespace=namespace,
            provider=engine.name,
            model=model,
            kind="image",
        )
    return stored


def _resolve_prompt(
    cfg: Config, block: ImageConfig, prompt_override: str | None
) -> str:
    """Resolve the effective prompt: CLI > ``image.prompt`` > top-level.

    CLI-over-config matches ``upscale --scale`` overriding ``cfg.upscale.scale``
    and ``interpolate --fps`` overriding ``cfg.interpolate.fps``.

    Args:
        cfg: The loaded config (for its top-level ``prompt`` fallback).
        block: The already-validated ``image:`` block.
        prompt_override: The CLI-supplied prompt, if any.

    Returns:
        The non-empty prompt to submit.

    Raises:
        ValidationError: None of the three sources supplied a prompt.
    """
    for candidate in (prompt_override, block.prompt, cfg.prompt):
        if candidate and candidate.strip():
            return candidate
    raise ValidationError(
        "no prompt to generate from: pass --prompt, or set `image.prompt` "
        "(or top-level `prompt:`) in the config"
    )
