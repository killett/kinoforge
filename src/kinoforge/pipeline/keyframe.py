"""KeyframeStage: fills missing image-kind conditioning roles via an ImageEngine.

Reads MODE_ROLE_REQUIREMENTS[request.mode] to discover required roles; for each
role with kind == "image" not already present in request.assets, generates an
image via the configured ImageEngine and appends a ConditioningAsset.
User-supplied assets are preserved (per-role gap fill).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from kinoforge.core.config import KeyframeConfig
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    ConditioningAsset,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    PipelineState,
)
from kinoforge.pipeline.artifact_bytes import artifact_bytes
from kinoforge.stores.base import ArtifactStore


@dataclass
class KeyframeStage:
    """Fills missing image-kind conditioning roles via an ImageEngine."""

    keyframe_cfg: KeyframeConfig
    image_engine: ImageEngine
    image_backend: ImageBackend
    image_profile: ImageProfile  # reserved for future spec validation
    store: ArtifactStore
    run_id: str
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Fill each missing image-kind role and return an updated PipelineState.

        Args:
            state: Incoming pipeline state; ``state.request.assets`` may already
                contain some conditioning assets (partial user supply).

        Returns:
            A new ``PipelineState`` with any missing image-kind roles filled via
            the configured ``ImageEngine``.  The original ``state`` is not mutated.
        """
        request = state.request
        required = MODE_ROLE_REQUIREMENTS.get(request.mode, {})
        have = {a.role for a in request.assets}

        new_assets = list(request.assets)
        new_artifacts = dict(state.artifacts)

        for role, kind in required.items():
            if kind != "image":
                continue
            if role in have:
                continue
            prompt = self._resolve_prompt(role)
            spec = self._resolve_spec(role)
            params = self._resolve_params(role)
            job = ImageJob(spec=spec, prompt=prompt, params=params)
            self.image_engine.validate_spec(job)
            job_id = self.image_backend.submit(job)
            artifact = self.image_backend.result(job_id)
            png_bytes = artifact_bytes(artifact, self.http_get_bytes)
            filename = f"keyframe-{role}.png"
            stored = self.store.put_bytes(self.run_id, filename, png_bytes)
            stored = replace(stored, filename=filename, meta=dict(artifact.meta))
            new_assets.append(ConditioningAsset(kind="image", role=role, ref=stored))
            new_artifacts[f"keyframe-{role}"] = stored

        new_request = replace(request, assets=new_assets)
        return replace(state, request=new_request, artifacts=new_artifacts)

    def _resolve_prompt(self, role: str) -> str:
        """Return the prompt for ``role``: per-role override > top-level default.

        Args:
            role: The conditioning role name (e.g. ``"init_image"``).

        Returns:
            The resolved prompt string.

        Raises:
            ValidationError: Neither a per-role prompt nor the top-level prompt
                is configured for this role.
        """
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.prompt:
            return role_block.prompt
        if self.keyframe_cfg.prompt:
            return self.keyframe_cfg.prompt
        raise ValidationError(
            f"keyframe role {role!r} has no prompt configured: set "
            f"keyframe.prompt or keyframe.roles.{role}.prompt"
        )

    def _resolve_spec(self, role: str) -> dict:  # type: ignore[type-arg]
        """Return the merged spec for ``role``: top-level base + per-role overrides.

        Args:
            role: The conditioning role name.

        Returns:
            A shallow-merged dict (per-role keys override top-level keys).
        """
        base = dict(self.keyframe_cfg.spec or {})
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.spec:
            base.update(role_block.spec)
        return base

    def _resolve_params(self, role: str) -> dict:  # type: ignore[type-arg]
        """Return the merged params for ``role``: top-level base + per-role overrides.

        Args:
            role: The conditioning role name.

        Returns:
            A shallow-merged dict (per-role keys override top-level keys).
        """
        base = dict(self.keyframe_cfg.params or {})
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.params:
            base.update(role_block.params)
        return base
