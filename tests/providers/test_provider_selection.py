"""Behavior: each provider chooses its own SKU from the portable Placement.

compute-seam S4 takes ``find_offers`` off ``ComputeProvider`` and deletes
``InstanceSpec.offer``. Before it, a caller pre-decided a SKU and handed it
down — which a declarative placer cannot honour (SkyPilot's optimizer picks
cloud, region and instance type for itself) and a scheduler does not need
(Modal takes a GPU class, not a host).

What replaces it is not "no selection" but "selection where the knowledge is":
the enumerating providers read their own catalog, and every provider reads the
same portable ``Placement``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import ComputeProvider, InstanceSpec
from kinoforge.providers.skypilot import SkyPilotProvider
from tools.snapshot_launch_payloads import build_spec


class _RecordingSky:
    """Captures the task_config SkyPilotProvider builds, then aborts."""

    def __init__(self, accelerators: Any = None) -> None:
        self.captured: dict[str, Any] = {}
        self._accelerators = accelerators if accelerators is not None else {}

    def list_accelerators(self, **_kw: object) -> Any:  # noqa: ANN401
        return self._accelerators

    def Task(self, **_kw: object) -> object:  # noqa: N802 — mirrors sky.Task
        raise RuntimeError("stop after the resources block")

    def launch(self, *_a: object, **_kw: object) -> object:
        raise AssertionError("launch must not be reached")


def _captured_task_config(cfg_path: str, accelerators: Any = None) -> dict[str, Any]:
    """Return the task_config SkyPilot would build for *cfg_path*.

    Args:
        cfg_path: Path to a kinoforge config.
        accelerators: Optional ``sky.list_accelerators`` stand-in.

    Returns:
        The ``task_config`` dict handed to ``sky.Task.from_yaml_config``.
    """
    cfg = load_config(cfg_path)
    spec = build_spec(cfg)
    captured: dict[str, Any] = {}

    class _Sky(_RecordingSky):
        def Task(self, **_kw: object) -> object:  # noqa: N802
            raise RuntimeError("stop")

    sky = _Sky(accelerators)

    def _from_yaml_config(config: dict[str, Any]) -> object:
        captured.update(config)
        raise RuntimeError("stop after the resources block")

    task_ns = type("_TaskNS", (), {"from_yaml_config": staticmethod(_from_yaml_config)})
    sky.Task = task_ns  # type: ignore[method-assign]

    provider = SkyPilotProvider(sky_client=sky)
    with pytest.raises(RuntimeError, match="stop"):
        provider.create_instance(spec)
    return captured


def test_skypilot_cpu_config_requests_cpus_not_an_accelerator() -> None:
    """Bug caught, and it would be invisible offline before S4: the CPU branch
    keyed off `spec.offer.gpu_type` being empty, which came from find_offers'
    own short-circuit. Delete spec.offer without moving that signal onto
    placement and the CPU smoke config starts asking for a GPU — the config
    both the S2 and S3 live smokes ran on, at ~20x the price."""
    task_config = _captured_task_config("examples/configs/skypilot-cpu.yaml")
    assert "accelerators" not in task_config["resources"]
    assert task_config["resources"]["cpus"]


def test_a_gpu_config_still_requests_its_named_accelerator() -> None:
    """The other side of the same branch. Bug caught: collapsing both arms into
    the CPU shape books a CPU box for a Wan render, which fails late and
    expensively at model load."""
    task_config = _captured_task_config(
        "examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml"
    )
    assert task_config["resources"]["accelerators"]
    assert "cpus" not in task_config["resources"]


def test_skypilot_picks_an_accelerator_by_vram_when_the_config_names_none() -> None:
    """Two shipped configs (skypilot-gpu, skypilot-lambda-comfyui) set only a
    VRAM floor.

    Bug caught: deleting the enumeration outright leaves those configs with no
    accelerator on the wire at all, so sky books a CPU box for a GPU render —
    the failure lands minutes later at model load, on a cluster that is billing.
    """
    accelerators = {
        "T4": [{"accelerator_name": "T4", "vram_gb": 16, "price": 0.35}],
        "A100": [{"accelerator_name": "A100", "vram_gb": 80, "price": 2.10}],
    }
    task_config = _captured_task_config(
        "examples/configs/skypilot-gpu.yaml", accelerators=accelerators
    )
    assert task_config["resources"]["accelerators"] == "T4:1"


def test_find_offers_is_off_the_compute_provider_abc() -> None:
    """Bug caught: leaving it on the ABC keeps every new provider owing an
    implementation of a method only marketplaces can honour — and keeps callers
    free to enumerate on a provider whose optimizer never consults a catalog."""
    assert not hasattr(ComputeProvider, "find_offers")


def test_skypilot_has_no_public_find_offers() -> None:
    """SkyPilot's selection is now a private step of create_instance.

    Bug caught: a public find_offers on the one provider without a catalog
    invites `kinoforge offers` and any future caller to treat its synthetic
    single-entry list as bookable inventory. It is not — the optimizer picks.
    """
    assert not hasattr(SkyPilotProvider, "find_offers")


@pytest.mark.parametrize("provider_name", ["runpod", "modal", "local"])
def test_enumerating_providers_keep_find_offers_public(provider_name: str) -> None:
    """The three with a real catalog keep it — that is what CATALOG_ENUMERATION
    declares, and what `kinoforge offers` prints."""
    import kinoforge._adapters  # noqa: F401,PLC0415
    from kinoforge.core import registry  # noqa: PLC0415

    cls = registry.provider_class(provider_name)
    assert cls is not None
    # getattr, not attribute access: mypy is right that ComputeProvider no
    # longer promises this — which is the whole point. The three that DO have
    # a catalog must still expose it.
    assert callable(getattr(cls, "find_offers", None))


def test_instance_spec_no_longer_carries_an_offer() -> None:
    """Bug caught: leaving the field lets a caller keep pre-deciding a SKU that
    a declarative placer will silently ignore — the shape that made every
    kinoforge surface report the asked-for rate instead of the billed one."""
    assert not hasattr(InstanceSpec(image="x"), "offer")


def test_no_source_file_still_reads_spec_offer() -> None:
    """The grep the plan asks for, as a test so it cannot rot.

    Bug caught: a surviving `spec.offer` read is an AttributeError at launch
    time on a path no offline test exercises. Comment lines are excluded: the
    ones that survive explain where the field went, and a comment cannot raise.
    """
    hits = [
        f"{path}:{i}"
        for path in Path("src/kinoforge").rglob("*.py")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "spec.offer" in line and not line.strip().startswith("#")
    ]
    assert hits == []
