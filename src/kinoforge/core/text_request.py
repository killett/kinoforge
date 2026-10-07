"""Pure helpers behind ``kinoforge text`` — no I/O, no registry, no pod.

Design: ``docs/superpowers/specs/2026-10-04-text-command-design.md`` §2.1 (mode
derivation), §2.2 (prompt precedence), §4.1 (the pre-spend gate). Lives in
``core`` so the CLI handler, the engine and the stage share ONE copy of each
rule; the engine and the stage see the cfg as a dict (``_cfg_dict``), the CLI
sees a :class:`Config`, so the lookups accept both.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kinoforge.core.config import TEXT_MODES, Config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Artifact, ConditioningAsset, GenerationRequest
from kinoforge.core.media import IMAGE_SUFFIXES

__all__ = [
    "IMAGE_SUFFIXES",
    "TEXT_MODES",
    "base_model_ref",
    "build_request",
    "declared_modes",
    "derive_mode",
    "image_arg_error",
    "preflight_mode_error",
    "resolve_prompt",
]

_VISION_EXAMPLE = "examples/configs/runpod-diffusers-smolvlm-256m-it2t.yaml"


def derive_mode(image_count: int) -> str:
    """Return ``"it2t"`` when any image is supplied, else ``"t2t"``.

    Args:
        image_count: Number of ``--image`` flags.

    Returns:
        The request mode (design §2.1: derived, never typed).
    """
    return "it2t" if image_count > 0 else "t2t"


def resolve_prompt(cfg: Config, prompt_override: str | None) -> str:
    """Resolve the prompt: CLI > ``text.prompt`` > top-level ``prompt``.

    Whitespace-only candidates count as absent, matching ``image_run``.

    Args:
        cfg: The loaded config; ``cfg.text`` must be set.
        prompt_override: The ``--prompt`` value, if any.

    Returns:
        The non-empty prompt to submit.

    Raises:
        ValidationError: None of the three sources supplied a prompt.
    """
    block_prompt = cfg.text.prompt if cfg.text is not None else None
    for candidate in (prompt_override, block_prompt, cfg.prompt):
        if candidate and candidate.strip():
            return candidate
    raise ValidationError(
        "no prompt to generate from: pass --prompt, or set `text.prompt` "
        "(or top-level `prompt:`) in the config"
    )


def _as_mapping(cfg: Config | Mapping[str, Any]) -> Mapping[str, Any]:
    return cfg.model_dump(mode="json") if isinstance(cfg, Config) else cfg


def base_model_ref(cfg: Config | Mapping[str, Any]) -> str:
    """Return the ``models[kind: base]`` ref (``hf:...``).

    Raises:
        ValidationError: No base entry (the config validator forbids this for
            text configs, so hitting it means a hand-built dict).
    """
    for entry in _as_mapping(cfg).get("models") or []:
        if entry.get("kind") == "base":
            return str(entry["ref"])
    raise ValidationError("text: models has no `kind: base` entry")


def declared_modes(cfg: Config | Mapping[str, Any]) -> frozenset[str]:
    """Return ``engine.diffusers.capability.supported_modes`` as a set.

    Empty when undeclared — the config validator refuses that for text
    configs, so an empty return here means a hand-built dict.
    """
    engine = _as_mapping(cfg).get("engine") or {}
    diffusers = engine.get("diffusers") or {}
    capability = diffusers.get("capability") or {}
    return frozenset(str(m) for m in capability.get("supported_modes") or [])


def preflight_mode_error(cfg: Config | Mapping[str, Any], mode: str) -> str | None:
    """The pre-spend gate (design §4.1): refuse a mode the config did not declare.

    Args:
        cfg: The loaded config.
        mode: The derived request mode.

    Returns:
        ``None`` when *mode* is declared; else a complete ``error: ...`` line
        naming the model, the declared modes and both fixes.
    """
    declared = declared_modes(cfg)
    if mode in declared:
        return None
    model = base_model_ref(cfg)
    if mode == "it2t":
        return (
            f"error: model {model} declares modes {sorted(declared)}; --image needs "
            f"it2t. Either drop --image, or use a vision-language config such as "
            f"{_VISION_EXAMPLE}"
        )
    return (
        f"error: model {model} declares modes {sorted(declared)}, which does not "
        f"include {mode!r}; declare it under engine.diffusers.capability."
        f"supported_modes, or pass --image for an it2t request"
    )


def image_arg_error(path: str) -> str | None:
    """Validate one ``--image`` argument without touching a pod.

    Returns:
        ``None`` when *path* names an existing ``.png``/``.jpg``/``.jpeg``
        file; else an ``error: ...`` line naming the fault.
    """
    if not path:
        return "error: --image must name a file (got an empty path)"
    if path.startswith(("http://", "https://")):
        return (
            "error: --image must be a local file; http(s):// sources are not "
            "supported for still images"
        )
    p = Path(path)
    if p.suffix.lower() not in IMAGE_SUFFIXES:
        return f"error: --image {path!r}: only .png/.jpg/.jpeg are accepted"
    if not p.is_file():
        return f"error: --image {path!r}: file not found"
    return None


def build_request(prompt: str, images: list[Artifact]) -> GenerationRequest:
    """Wrap the prompt and image artifacts as a ``GenerationRequest``.

    Roles are ``image_1 … image_N`` in flag order (design §2.1); the mode is
    derived from the count.

    Args:
        prompt: The resolved prompt.
        images: Local-file artifacts from ``_resolve_input_as_artifact``.

    Returns:
        The request ``TextStage`` reads from ``PipelineState``.
    """
    assets = [
        ConditioningAsset(kind="image", role=f"image_{i}", ref=art)
        for i, art in enumerate(images, start=1)
    ]
    return GenerationRequest(
        prompt=prompt, mode=derive_mode(len(images)), assets=assets
    )
