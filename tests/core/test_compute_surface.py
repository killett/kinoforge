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
    Launch,
    Lifecycle,
    RenderedProvision,
)
from kinoforge.core.spec_builder import build_instance_spec

#: compute-seam S4: providers select their own SKU inside create_instance, so a
#: transport a create test drives must answer the catalog query first.
_S4_GPU_TYPES: dict[str, object] = {
    "data": {
        "gpuTypes": [
            {
                "id": name,
                "displayName": name,
                "memoryInGb": vram,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": price,
                    "uninterruptablePrice": price,
                },
            }
            for name, vram, price in (
                ("NVIDIA RTX A4000", 16, 0.32),
                ("NVIDIA RTX A5000", 24, 0.44),
                ("NVIDIA GeForce RTX 4090", 24, 0.69),
                ("NVIDIA A100 80GB PCIe", 80, 1.64),
            )
        ]
    }
}


def _s4_post(_url: str, body: dict[str, object]) -> dict[str, object]:
    """Answer the S4 catalog read; everything else gets an empty response.

    These tests are about WHICH BRANCH create_instance takes, not about the
    create's own reply — but the provider now enumerates first, and an empty
    catalog would make every branch end in CapacityError.
    """
    if "gpuTypes" in str(body.get("query", "")):
        return _S4_GPU_TYPES
    return {}


_S4_ACCELERATORS: list[dict[str, object]] = [
    {"accelerator_name": "T4", "vram_gb": 16, "cuda": "12.8", "price": 0.35},
    {"accelerator_name": "A100", "vram_gb": 80, "cuda": "12.8", "price": 2.10},
]


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
        launch=Launch(("python", "-m", "server")),
        image="img:tag",
        ports=["8000/http"],
        env_required=[],
    )
    return build_instance_spec(
        cfg=cfg,
        rendered=rendered,
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


def test_pod_mode_is_the_default_and_reaches_the_spec_tag() -> None:
    """The default mode arrives as a tag, not as an absent key.

    Bug caught: leaving the tag unwritten keeps RunPod on its own
    ``.get("mode", "pod")`` fallback, so the cfg and the wire agree only by
    coincidence — and stop agreeing the moment the fallback changes.
    """
    spec = _spec({"provider": "runpod", "image": "i"})
    assert spec.tags["mode"] == "pod"


def test_serverless_mode_reaches_the_serverless_branch() -> None:
    """`mode: serverless` takes the branch it has always claimed to.

    Bug caught (found by the S1 whole-branch review): compute.mode was
    written by 46 configs and read by nothing, so `mode: serverless` took the
    pod branch and produced a byte-identical payload. This captures WHICH
    branch ran rather than reading the code.
    """
    from unittest import mock  # noqa: PLC0415

    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    spec = _spec({"provider": "runpod", "image": "i", "mode": "serverless"})
    assert spec.tags["mode"] == "serverless"

    provider = RunPodProvider(
        http_post=_s4_post,
        http_get=lambda _url: {},
    )
    with (
        mock.patch.object(RunPodProvider, "_create_serverless") as serverless,
        mock.patch.object(RunPodProvider, "_create_pod") as pod,
    ):
        serverless.return_value = mock.MagicMock(id="sl-1")
        provider.create_instance(spec)
    assert serverless.called
    assert not pod.called


def test_pod_mode_takes_the_pod_branch() -> None:
    """The mirror of the above: the default must not start routing elsewhere.

    Bug caught: a `setdefault`/`update` mix-up that writes "serverless" for
    every cfg would pass the serverless test alone and silently move all 45
    pod configs onto a branch that bills differently.
    """
    from unittest import mock  # noqa: PLC0415

    from kinoforge.providers.runpod import RunPodProvider  # noqa: PLC0415

    spec = _spec({"provider": "runpod", "image": "i"})
    provider = RunPodProvider(
        http_post=_s4_post,
        http_get=lambda _url: {},
    )
    with (
        mock.patch.object(RunPodProvider, "_create_serverless") as serverless,
        mock.patch.object(RunPodProvider, "_create_pod") as pod,
    ):
        pod.return_value = mock.MagicMock(id="pod-1")
        provider.create_instance(spec)
    assert pod.called
    assert not serverless.called


def test_a_caller_supplied_mode_tag_still_wins_over_the_cfg() -> None:
    """RunPod's own internal re-create call passes an explicit mode tag.

    Bug caught: folding the cfg value in with `update` rather than
    `setdefault` would overwrite the mode a caller deliberately chose for one
    invocation — including the provider's own internal call site.
    """
    spec = _spec(
        {"provider": "runpod", "image": "i", "mode": "pod"},
        caller_tags={"mode": "serverless"},
    )
    assert spec.tags["mode"] == "serverless"


def test_misspelled_compute_key_is_refused() -> None:
    """An unknown key under ``compute`` raises instead of vanishing.

    Bug caught: `placemnt:` silently applies every placement default,
    including a disk and a CUDA floor the operator never chose, and the
    typo'd block they DID write is discarded without a word.
    """
    import pytest  # noqa: PLC0415

    with pytest.raises(Exception) as exc:  # noqa: PT011 — pydantic vs ConfigError
        Config.model_validate(
            {
                **_BASE,
                "compute": {
                    "provider": "runpod",
                    "image": "i",
                    "placemnt": {"disk_gb": 200},
                },
            }
        )
    assert "placemnt" in str(exc.value)


def test_removed_key_error_still_names_its_replacement() -> None:
    """S1's migration messages survive the new extra-key refusal.

    Bug caught: `extra="forbid"` fires first and the operator gets
    "Extra inputs are not permitted" instead of the path their value moved
    to. The removed-key validator runs in `mode="before"`, which is what
    keeps it ahead of pydantic's extra handling — this proves that ordering
    rather than assuming it.
    """
    import pytest  # noqa: PLC0415

    from kinoforge.core.errors import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError) as exc:
        Config.model_validate(
            {
                **_BASE,
                "compute": {
                    "provider": "skypilot",
                    "image": "i",
                    "cloud": ["lambda"],
                },
            }
        )
    assert "compute.backend_options.skypilot.clouds" in str(exc.value)


def test_every_shipped_config_still_loads() -> None:
    """The refusal must not take a config that ships with it.

    Recursive over ``examples/configs/**``, not just the top level: the grid
    and extras trees are where the unusual compute blocks live. Fragments
    that fail for a MISSING engine/models block are pre-existing and
    unrelated; a failure naming an extra compute key would be this change's.
    """
    from pathlib import Path  # noqa: PLC0415

    offenders: list[str] = []
    for path in sorted(Path("examples/configs").rglob("*.y*ml")):
        try:
            load_config(str(path))
        except Exception as exc:  # noqa: BLE001 — the message is the assertion
            text = str(exc)
            if "Extra inputs are not permitted" in text or "extra_forbidden" in text:
                offenders.append(f"{path}: {text[:160]}")
    assert not offenders, offenders


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
