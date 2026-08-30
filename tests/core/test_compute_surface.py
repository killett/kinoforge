"""Behavior: the compute block's non-placement keys are real or refused.

S1's whole-branch review found compute.tags written by four shipped configs
and read by nothing — pydantic's default extra="ignore" dropped it silently.
That is the exact bug the compute-seam work exists to end, sitting in the
repo's own examples.
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.config import Config, load_config
from kinoforge.core.interfaces import (
    InstanceSpec,
    Lifecycle,
    Offer,
    RenderedProvision,
)
from kinoforge.core.spec_builder import build_instance_spec

_BASE = {
    "engine": {"kind": "diffusers", "precision": "bf16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
}

#: The four shipped configs that write ``compute.tags``. Named rather than
#: globbed: the point of the test below is that THESE files, which have been
#: dropping their tag on the floor, now deliver it.
_TAGGED_CONFIGS = (
    "examples/configs/runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml",
    "examples/configs/"
    "runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml",
    "examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml",
    "examples/configs/"
    "runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml",
)


def _spec(
    compute: dict[str, Any], *, caller_tags: dict[str, str] | None = None
) -> InstanceSpec:
    """Return the InstanceSpec a cfg carrying *compute* builds.

    Args:
        compute: The ``compute`` block to validate into a Config.
        caller_tags: Tags passed as ``build_instance_spec(tags=...)``, the
            route the CLI and the grid executor use.

    Returns:
        The built spec.
    """
    cfg = Config.model_validate({**_BASE, "compute": compute})
    rendered = RenderedProvision(
        script="#!/bin/bash\necho hi",
        run_cmd=["python", "-m", "server"],
        image="img:tag",
        ports=["8000/http"],
        env_required=[],
    )
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
        offer=Offer(
            id="g", gpu_type="g", vram_gb=80, cuda="12.4", cost_rate_usd_per_hr=1.0
        ),
        engine_name="diffusers",
        key_hash="abc",
        image="fallback:img",
        lifecycle=Lifecycle(),
        env={},
        run_id="run-1",
        tags=caller_tags,
    )


def test_compute_tags_reach_the_spec() -> None:
    """A tag written in the cfg arrives on the spec the provider receives."""
    spec = _spec({"provider": "runpod", "image": "i", "tags": {"smoke_tier": "tier-3"}})
    assert spec.tags["smoke_tier"] == "tier-3"


def test_caller_tags_win_over_compute_tags() -> None:
    """Per-invocation tags beat the config's static ones.

    Bug caught: a per-invocation tag (CLI, grid cell) silently loses to the
    config's static one, so two grid cells become indistinguishable in the
    ledger and the reaper cannot tell them apart.
    """
    spec = _spec(
        {"provider": "runpod", "image": "i", "tags": {"cell": "from-cfg"}},
        caller_tags={"cell": "from-caller"},
    )
    assert spec.tags["cell"] == "from-caller"


def test_kinoforge_owned_tags_survive_both() -> None:
    """Neither cfg nor caller can overwrite the two warm-reuse keys.

    Bug caught: a cfg that sets kinoforge_key breaks warm-reuse matching,
    which keys off exactly that tag — every subsequent run would cold-boot,
    or worse, attach to a pod built for another capability key.
    """
    spec = _spec(
        {"provider": "runpod", "image": "i", "tags": {"kinoforge_key": "hijack"}},
        caller_tags={"kinoforge_engine": "hijack"},
    )
    assert spec.tags["kinoforge_key"] == "abc"
    assert spec.tags["kinoforge_engine"] == "diffusers"


def test_the_four_shipped_configs_deliver_their_smoke_tier() -> None:
    """The configs that were silently dropping their tag now carry it."""
    for path in _TAGGED_CONFIGS:
        cfg = load_config(path)
        assert cfg.compute is not None
        assert cfg.compute.tags.get("smoke_tier", "").startswith(
            "kinoforge-smoke-tier"
        ), path


def test_a_shipped_configs_tag_reaches_the_instance_the_ledger_sees() -> None:
    """The cfg tag survives all the way onto the Instance, not just the spec.

    Bug caught: ``spec.tags`` is populated and the provider drops it while
    fabricating its Instance, so the ledger and the reaper — which read
    ``Instance.tags`` — still cannot tell one smoke tier from another. That
    is where the value has to arrive, because RunPod's create mutation
    carries no ``tags`` key at all: this is precisely why NO golden moved
    when ``compute.tags`` became real, even though all four tagged configs
    have one.
    """
    from pathlib import Path  # noqa: PLC0415

    from tools.snapshot_launch_payloads import capture_launch  # noqa: PLC0415

    launch = capture_launch(
        Path("examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml")
    )
    assert launch.instance is not None
    assert launch.instance.tags["smoke_tier"] == "kinoforge-smoke-tier-3-strength-grid"
    assert "tags" not in launch.payload["input"]
