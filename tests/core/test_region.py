"""Behavior: compute.placement.region is portable and provider-declared.

F6 in the cloud-layer verification doc: SkyPilotProvider has accepted a
`region` constructor argument all along, and no config path ever reached it,
so `sky` was free to pick a hemisphere. The standing project rule is that
region is pinned on every cloud (default Oregon) — a rule no YAML could
express until this field existed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.config import Config
from kinoforge.core.interfaces import FieldSupport, Placement

_BASE = {
    "engine": {"kind": "diffusers", "precision": "bf16"},
    "spec": {"model": "m", "precision": "bf16"},
    "models": [{"kind": "base", "ref": "hf:org/repo", "target": "checkpoints"}],
}


def _load(compute: dict[str, Any]) -> Config:
    """Return a Config carrying *compute* over the shared minimal base."""
    return Config.model_validate({**_BASE, "compute": compute})


def test_region_reaches_the_placement_object() -> None:
    """A cfg-written region survives into the portable placement block."""
    cfg = _load(
        {"provider": "skypilot", "image": "i", "placement": {"region": "us-west-2"}}
    )
    assert cfg.placement().region == "us-west-2"


def test_region_defaults_to_none_meaning_provider_decides() -> None:
    """No region in the cfg leaves the field None on both sides.

    Bug caught: a non-None default would pin every existing config to one
    region, which is a behaviour change disguised as a new field.
    """
    assert Placement().region is None
    assert _load({"provider": "skypilot", "image": "i"}).placement().region is None


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("skypilot", FieldSupport.CONSUMED),
        ("runpod", FieldSupport.UNSUPPORTED),
        ("modal", FieldSupport.UNSUPPORTED),
        ("local", FieldSupport.UNSUPPORTED),
    ],
)
def test_every_provider_declares_region(provider: str, expected: FieldSupport) -> None:
    """Each shipped provider states what it does with a pinned region.

    Bug caught: a provider that neither reads region nor declares it lets an
    operator pin a region that silently does nothing — the F5 failure mode.
    """
    import kinoforge._adapters  # noqa: F401, PLC0415 — composition root registers providers
    from kinoforge.core import registry  # noqa: PLC0415

    cls = registry.provider_class(provider)
    assert cls is not None
    assert cls.consumes()["region"] is expected


# ---------------------------------------------------------------------------
# The wire: a region written in YAML has to reach the launch payload
# ---------------------------------------------------------------------------


def _capture(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> dict[str, Any]:
    """Return the launch payload for skypilot-cpu.yaml with *mutate* applied.

    Args:
        tmp_path: pytest tmp dir the rewritten config is written into.
        mutate: Applied to the parsed YAML before it is written back.

    Returns:
        The captured wire payload (``provider`` / ``seam`` / ``task_config`` /
        ``launch_kwargs`` for skypilot).
    """
    import yaml  # noqa: PLC0415 — test-local, keeps the module import cheap

    from tools.snapshot_launch_payloads import capture_payload  # noqa: PLC0415

    src = yaml.safe_load(Path("examples/configs/skypilot-cpu.yaml").read_text())
    mutate(src)
    cfg_path = tmp_path / "region-probe.yaml"
    cfg_path.write_text(yaml.safe_dump(src))
    return capture_payload(cfg_path)


def test_cfg_region_lands_in_the_skypilot_launch_payload(tmp_path: Path) -> None:
    """A YAML-written region reaches ``resources.region`` on the wire.

    Bug caught: the field exists, validates, and never reaches sky — which is
    exactly the state F6 documented (constructor knob, no config path). This
    observes the payload rather than ``provider._region``, so wiring that
    stops at the attribute fails here.
    """

    def mutate(src: dict[str, Any]) -> None:
        src["compute"].setdefault("placement", {})["region"] = "us-west-2"
        src["compute"].setdefault("backend_options", {})["skypilot"] = {
            "clouds": ["aws"]
        }

    payload = _capture(tmp_path, mutate)
    assert payload["task_config"]["resources"]["region"] == "us-west-2"


def test_no_region_in_cfg_leaves_the_payload_unpinned(tmp_path: Path) -> None:
    """Omitting the key leaves sky's optimizer free, as it was before S2.

    Bug caught: a default region pinned into the payload would relocate every
    existing skypilot config without anyone writing a region anywhere.
    """

    def mutate(src: dict[str, Any]) -> None:
        src["compute"].get("placement", {}).pop("region", None)

    payload = _capture(tmp_path, mutate)
    assert "region" not in payload["task_config"]["resources"]


# ---------------------------------------------------------------------------
# The providers that cannot honour it must say so at load time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["runpod", "modal"])
def test_region_on_a_provider_that_drops_it_is_a_load_time_error(provider: str) -> None:
    """Pinning a region where nothing reads it refuses the load.

    Bug caught: the operator writes ``region: us-west-2`` against runpod,
    kinoforge sends no ``dataCenterId``, and the run lands wherever RunPod has
    capacity — a data-residency decision made silently by a provider. Nothing
    else in the cfg bounds that risk, so per the S1 risk-coverage rule the row
    carries no substitute and the severity is ERROR.
    """
    from kinoforge.validation.checks.field_support import (  # noqa: PLC0415
        UnsupportedFieldCheck,
    )
    from kinoforge.validation.protocol import Severity  # noqa: PLC0415

    cfg = _load(
        {"provider": provider, "image": "i", "placement": {"region": "us-west-2"}}
    )
    result = UnsupportedFieldCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert "compute.placement.region" in result.message
    assert "nothing else in this cfg bounds that risk" in result.message


def test_region_on_skypilot_is_not_a_finding() -> None:
    """The provider that honours it must not be nagged about it.

    Bug caught (the mirror image): a blanket rule that reports every region
    would train operators to ignore the check on the one provider where the
    pin is real.
    """
    from kinoforge.validation.checks.field_support import (  # noqa: PLC0415
        evaluate_field_gaps,
    )

    cfg = _load(
        {"provider": "skypilot", "image": "i", "placement": {"region": "us-west-2"}}
    )
    assert [g.field for g in evaluate_field_gaps(cfg)] == []
