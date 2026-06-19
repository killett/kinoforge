"""ModelRefReachableCheck.applies_to filters by engine kind.

Hosted / fal / replicate / runway / bedrock_video / fake engines do
not HEAD-fetch ``cfg.models[].ref`` themselves — the wire identifier
lives on ``spec.model`` or ``engine.<kind>.model_id``. Skipping the
check for those kinds lets their cfgs ship informational refs (e.g.
``bedrock://amazon.nova-reel-v1:1``) without doctor failing.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.config import (
    Config,
    EngineConfig,
    FalEngineConfig,
    LifecycleConfig,
    ModelEntry,
)
from kinoforge.validation.checks.models import ModelRefReachableCheck


def _cfg(kind: str) -> Config:
    """Build a minimal valid Config for the given engine kind.

    Each engine kind that requires a sub-block gets one; the model list
    always carries a single base entry with a network-scheme ref so that
    the ref-scheme guard inside applies_to does not short-circuit before
    we reach the engine-kind gate.
    """
    engine_kwargs: dict[str, Any] = {"kind": kind, "precision": "fp16"}
    if kind == "fal":
        engine_kwargs["fal"] = FalEngineConfig(
            endpoint="fal-ai/wan/v2.2/t2v",
            url_path="video.url",
        )
    return Config(
        engine=EngineConfig(**engine_kwargs),
        models=[
            ModelEntry(
                ref="hf:owner/repo:file.safetensors", kind="base", target="checkpoints"
            )
        ],
        lifecycle=LifecycleConfig(budget=1.0),
    )


@pytest.mark.parametrize(
    "kind", ["hosted", "fal", "replicate", "runway", "bedrock_video", "fake"]
)
def test_skips_for_non_fetching_engines(kind: str) -> None:
    """Non-fetching engines must not trigger the HEAD probe."""
    assert ModelRefReachableCheck().applies_to(_cfg(kind)) is False


@pytest.mark.parametrize("kind", ["comfyui", "diffusers"])
def test_applies_for_fetching_engines(kind: str) -> None:
    """Engines that pull weights from the ref must still be checked."""
    assert ModelRefReachableCheck().applies_to(_cfg(kind)) is True
