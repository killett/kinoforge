"""Behavior: compute.placement.region is portable and provider-declared.

F6 in the cloud-layer verification doc: SkyPilotProvider has accepted a
`region` constructor argument all along, and no config path ever reached it,
so `sky` was free to pick a hemisphere. The standing project rule is that
region is pinned on every cloud (default Oregon) — a rule no YAML could
express until this field existed.
"""

from __future__ import annotations

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
