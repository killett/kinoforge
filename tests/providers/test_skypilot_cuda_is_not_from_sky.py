"""Behavior: SkyPilot must not claim a CUDA version sky never told it (U48).

``_offers_from_records`` builds every offer with
``cuda=_record_field(info, "cuda", default="12.0")``. That reads like a lookup
into sky's catalog. It is not one: :class:`sky.catalog.common.InstanceTypeInfo`
has no ``cuda`` field at all (verified 2026-09-16 against the pinned sky in the
``live-skypilot`` env — its ``_fields`` are cloud, instance_type,
accelerator_name, accelerator_count, cpu_count, device_memory, memory, price,
spot_price, region). So the default is the ONLY value that can ever be
produced, and every SkyPilot offer reports ``"12.0"`` regardless of hardware.

The harm is not cosmetic. ``Placement.min_cuda`` defaults to ``"12.8"``, so a
config that simply does not mention CUDA has every offer filtered out — an
H100 rejected for a floor that describes nothing about it. All five shipped
SkyPilot configs set ``min_cuda`` to ``"0"``, ``"11.0"`` or ``"12.0"``, which
is the config surface bent around a constant rather than an operator choice.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.interfaces import FieldSupport


def test_skypilot_declares_min_cuda_unsupported() -> None:
    """The declaration must say sky does not really read this.

    Bug caught: ``consumes()`` claimed ``min_cuda: CONSUMED`` ("filter_offers
    excludes below the floor"), which is true only in an all-or-nothing sense —
    the value never varies, so the filter can only pass everything or nothing
    and never discriminates between two GPUs. Because it claimed CONSUMED,
    ``UnsupportedFieldCheck`` stayed silent and an operator writing
    ``min_cuda: "12.8"`` got an empty catalog with no hint why.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    assert SkyPilotProvider.consumes()["min_cuda"] is FieldSupport.UNSUPPORTED


def test_the_min_cuda_finding_warns_rather_than_refuses() -> None:
    """It must carry a substitute, so the row is WARN and not ERROR.

    Bug caught: a row absent from ``_SUBSTITUTE`` is an ERROR by construction,
    and an ERROR here would REFUSE the two shipped SkyPilot configs that set
    ``min_cuda`` perfectly reasonably (``skypilot-gpu.yaml`` at 11.0,
    ``skypilot-lambda-comfyui.yaml`` at 12.0). Turning a silent wrong answer
    into a hard refusal of working configs is not an improvement.
    """
    from kinoforge.validation.checks.field_support import evaluate_field_gaps

    cfg = Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp16"},
            "spec": {"model": "m", "precision": "bf16"},
            "models": [
                {"kind": "base", "ref": "hf:org/repo", "target": "diffusion_models"}
            ],
            "compute": {
                "provider": "skypilot",
                "image": "img:1",
                "placement": {"min_cuda": "12.4", "min_vram_gb": 24},
            },
        }
    )

    # ``Gap.field`` is the DOTTED path the operator would have written, not the
    # bare declaration key — that is the whole point of ``_dotted_path``.
    gaps = [
        g for g in evaluate_field_gaps(cfg) if g.field == "compute.placement.min_cuda"
    ]

    assert len(gaps) == 1, f"expected exactly one min_cuda gap, got {gaps}"
    assert gaps[0].substitute is not None, (
        "no substitute makes this an ERROR, which would refuse shipped configs"
    )
    # The operator must learn what really constrains the GPU choice here.
    assert "accelerators" in gaps[0].substitute


def test_sky_really_has_no_cuda_field_to_read() -> None:
    """Pins the upstream fact the fix rests on.

    Bug caught: someone "restores" a ``_record_field(info, "cuda", ...)`` lookup
    on the grounds that reading a real field is better than a constant. It is —
    but there is no such field, so the lookup silently yields the default
    forever and the comment above it becomes a lie again. If a future sky ever
    DOES publish CUDA, this test fails and the constant should be replaced.
    """
    sky_catalog = pytest.importorskip(
        "sky.catalog.common", reason="sky lives only in the live-skypilot env"
    )

    assert "cuda" not in sky_catalog.InstanceTypeInfo._fields
