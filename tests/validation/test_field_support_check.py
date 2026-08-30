"""Behavior: a portable field the selected provider cannot honour is surfaced.

F5's sharpest edge was ``provider: runpod`` + ``cloud: [lambda]`` validating
clean. The namespace split makes that specific pair impossible; these checks
cover the general case — a portable ``compute.placement`` field the selected
provider declares UNSUPPORTED, and a ``backend_options`` namespace belonging to
a provider that is not the one running.

Severity is by risk coverage (plan amendment 2026-08-27): ERROR only when
nothing bounds the same risk, WARN naming the substitute and the bound it
actually enforces otherwise. Every expectation below is derived from provider
source, not from the check: RunPod's ``containerDiskInGb: 250`` literal,
SkyPilot's ``setdefault("disk_size", 60 if gpu else 30)``, Modal's
``timeout=req.startup_timeout_s``, and
``skypilot.watchdog.compute_deadline``'s ``if budget_usd > 0`` arm.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kinoforge.core.config import Config, PlacementConfig, _parse_cfg_raw, load_config
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import Placement
from kinoforge.validation.checks.field_support import (
    ForeignNamespaceCheck,
    UnsupportedFieldCheck,
    evaluate_field_gaps,
)
from kinoforge.validation.protocol import CheckCategory, Severity
from kinoforge.validation.registry import default_registry

CONFIG_DIR = Path("examples/configs")


def _raw(
    provider: str,
    *,
    placement: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
    backend_options: dict[str, dict[str, Any]] | None = None,
    compute_level: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the raw cfg mapping for ``provider``.

    Args:
        provider: ``compute.provider`` value.
        placement: Optional ``compute.placement`` block. Omitted entirely
            when None, which is the "operator wrote nothing" case.
        lifecycle: Optional ``compute.lifecycle`` block; defaults to the
            minimum Pydantic accepts (``budget`` has no default).
        backend_options: Optional ``compute.backend_options`` mapping.
        compute_level: Optional keys written directly under ``compute``
            (``mode``, ``tags``, ``heartbeat_mode``, ...), merged last so a
            test can override the defaults this helper writes.

    Returns:
        A mapping ready for ``yaml.safe_dump``.
    """
    compute: dict[str, Any] = {
        "provider": provider,
        "image": "example/image:latest",
        "mode": "pod",
        "lifecycle": lifecycle if lifecycle is not None else {"budget": 1.0},
    }
    if placement is not None:
        compute["placement"] = placement
    if backend_options is not None:
        compute["backend_options"] = backend_options
    if compute_level is not None:
        compute.update(compute_level)
    return {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {
                "ref": "https://example.com/fake.safetensors",
                "kind": "base",
                "target": "diffusion_models",
            }
        ],
        "compute": compute,
    }


def _cfg(provider: str, **kwargs: Any) -> Config:
    """Return a Config for ``provider`` built through the real YAML parser.

    ``_parse_cfg_raw`` rather than ``load_config`` deliberately: it is the
    doctor path, running Pydantic only, so a cfg whose whole point is to
    produce an ERROR-severity finding can still be constructed and inspected.

    Args:
        provider: ``compute.provider`` value.
        **kwargs: Forwarded to :func:`_raw`.

    Returns:
        The parsed Config.
    """
    return _parse_cfg_raw(yaml.safe_dump(_raw(provider, **kwargs)))


# ---------------------------------------------------------------------------
# The default comparison — "only a value the operator WROTE is a finding"
# ---------------------------------------------------------------------------


def test_placement_yaml_defaults_match_the_portable_dataclass() -> None:
    """Catches a YAML default drifting from ``Placement()``'s.

    The check calls a field "written by the operator" by comparing it to the
    default. If ``PlacementConfig.disk_gb`` were bumped to 200 while
    ``Placement.disk_gb`` stayed 100, every cfg that wrote 100 would silently
    become a finding and every cfg that wrote nothing would inherit 200 — the
    inference this whole check rests on would be wrong in both directions.
    """
    portable = Placement()
    yaml_defaults = PlacementConfig()
    assert list(yaml_defaults.accelerators) == list(portable.accelerators)
    for name in ("accelerator_count", "min_vram_gb", "min_cuda", "disk_gb", "spot"):
        assert getattr(yaml_defaults, name) == getattr(portable, name), name
    assert yaml_defaults.max_usd_per_hr == portable.max_usd_per_hr


def test_unsupported_field_left_at_its_default_passes() -> None:
    """Catches refusing every config because a default exists for a field the
    provider ignores — which would refuse every shipped skypilot config."""
    assert UnsupportedFieldCheck().run(_cfg("skypilot")).passed


def test_unsupported_field_written_at_exactly_its_default_still_passes() -> None:
    """Catches the check keying off ``model_fields_set`` instead of the value.

    The grid executor's ``model_dump()`` round-trip repopulates
    ``model_fields_set`` for every field, so a ``model_fields_set`` check
    would report findings on grid children that the operator never wrote.
    ``disk_gb: 100`` IS the default, so it must not be a finding even though
    the YAML mentions it.
    """
    cfg = _cfg("skypilot", placement={"disk_gb": Placement.disk_gb})
    assert UnsupportedFieldCheck().run(cfg).passed


def test_consumed_field_set_to_a_non_default_passes() -> None:
    """Catches a check that flags every non-default placement field instead of
    consulting ``consumes()``: skypilot DOES honour ``min_vram_gb`` (the
    accelerator name that survives the VRAM floor is the name pinned on the
    wire), so 80 here must be silent."""
    cfg = _cfg("skypilot", placement={"min_vram_gb": 80})
    assert UnsupportedFieldCheck().run(cfg).passed


# ---------------------------------------------------------------------------
# ERROR — nothing bounds the same risk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["runpod", "skypilot", "modal"])
def test_accelerator_count_is_an_error_on_every_provider(provider: str) -> None:
    """Catches ``accelerator_count`` being downgraded to a WARN.

    All four providers hardcode one accelerator (RunPod ``"gpuCount": 1``,
    SkyPilot ``accelerators=f"{gpu_type}:1"``, Modal one GPU per function) and
    NOTHING else in the cfg can deliver a second one, so a 4-GPU ask that
    launches 1 GPU has no substitute to fall back on. A WARN here means the
    operator pays for a run that cannot fit the model it was sized for.
    """
    cfg = _cfg(provider, placement={"accelerator_count": 4})
    result = UnsupportedFieldCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert provider in result.message
    assert "compute.placement.accelerator_count" in result.message
    assert "4" in result.message


def test_error_line_says_nothing_bounds_the_risk() -> None:
    """Catches an ERROR that reads like a WARN — refusing the load without
    telling the operator that no other field can cover for the one refused."""
    result = UnsupportedFieldCheck().run(
        _cfg("runpod", placement={"accelerator_count": 2})
    )
    assert "nothing else in this cfg bounds" in result.message


def test_error_refuses_the_load_end_to_end(tmp_path: Path) -> None:
    """Catches the check existing but never firing in production.

    Registration in ``checks/__init__`` is what puts it on the default
    registry that ``load_config`` runs; without the import this passes its
    unit tests and validates nothing.
    """
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(_raw("runpod", placement={"accelerator_count": 8})))
    with pytest.raises(ValidationError) as exc:
        load_config(path)
    assert "compute.placement.accelerator_count" in str(exc.value)
    assert "8" in str(exc.value)


def test_provider_with_an_empty_declaration_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an undeclared provider being read as "consumes everything".

    ``capabilities()`` set the precedent: a provider that has not declared
    claims nothing. The mirror image — treating the empty mapping as "no
    UNSUPPORTED rows, therefore clean" — would let a brand-new provider ship
    with every portable field silently dropped.
    """
    from kinoforge.core import registry

    class _Undeclared:
        name = "undeclared"

    monkeypatch.setitem(registry._provider_classes, "undeclared", _Undeclared)
    result = UnsupportedFieldCheck().run(_cfg("undeclared"))
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert "declares no field support" in result.message


def test_unregistered_provider_is_skipped() -> None:
    """Catches this check preempting ``registry.get_provider``'s accurate
    unknown-provider refusal with a wrong "declares no field support"."""
    cfg = _cfg("not-a-real-provider", placement={"accelerator_count": 4})
    assert UnsupportedFieldCheck().applies_to(cfg) is False
    assert evaluate_field_gaps(cfg) == []


# ---------------------------------------------------------------------------
# WARN — the substitute and the bound it actually enforces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "hardcoded"),
    [("runpod", "250"), ("skypilot", "60")],
)
def test_disk_gb_warn_quotes_the_value_the_provider_really_uses(
    provider: str, hardcoded: str
) -> None:
    """Catches a disk WARN that omits the number, or reuses one provider's.

    RunPod's create-pod body pins ``containerDiskInGb: 250``; SkyPilot's
    ``resources.setdefault("disk_size", 60)`` wins for a GPU offer. Printing
    "disk_gb is unsupported" alone leaves the operator unable to see that a
    150 GB ask becomes 60 GB on sky — the direction that kills a 70 GB model
    download.
    """
    cfg = _cfg(provider, placement={"disk_gb": 150})
    result = UnsupportedFieldCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "compute.placement.disk_gb" in result.message
    assert "150" in result.message
    assert hardcoded in result.message


def test_skypilot_disk_warn_flags_the_shortfall_direction() -> None:
    """Catches a WARN that reads as reassuring when the substitute is SMALLER
    than the ask. 150 asked, 60 provisioned is the case that bites."""
    result = UnsupportedFieldCheck().run(_cfg("skypilot", placement={"disk_gb": 150}))
    assert "below" in result.message.lower()


def test_runpod_disk_warn_does_not_claim_a_shortfall() -> None:
    """Catches a shortfall phrase hardcoded onto every disk WARN: RunPod's 250
    is ABOVE a 150 GB ask, and calling that a shortfall is a false alarm."""
    result = UnsupportedFieldCheck().run(_cfg("runpod", placement={"disk_gb": 150}))
    assert "below" not in result.message.lower()


def test_skypilot_cpu_config_is_told_about_30_not_60() -> None:
    """Catches a SkyPilot disk bound that ignores the CPU short-circuit.

    ``find_offers`` returns the synthetic ``sky-cpu-auto`` offer when
    ``min_vram_gb == 0``, and an offer with no ``gpu_type`` takes the 30 GB
    arm of ``setdefault("disk_size", 60 if is_gpu else 30)``. Quoting 60 on
    ``skypilot-cpu.yaml`` would be a number sky never uses for that cfg.
    """
    cfg = _cfg("skypilot", placement={"min_vram_gb": 0, "disk_gb": 50})
    message = UnsupportedFieldCheck().run(cfg).message
    assert "30" in message
    assert "60" not in message


def test_skypilot_rate_cap_warn_names_the_watchdog_and_the_budget_arm() -> None:
    """Catches F4's rate cap being reported as enforced, or as an ERROR.

    ``max_usd_per_hr`` reaches ``filter_offers`` while enumerating and is then
    overridden by sky's optimizer, which has booked a $1.99 A100 under a $1.00
    ceiling. What DOES bound spend is the instance-side watchdog:
    ``compute_deadline`` takes the earlier of ``max_lifetime`` and
    ``budget_usd / rate``. The WARN must name both so the operator sees what
    is really holding the line.
    """
    cfg = _cfg(
        "skypilot",
        placement={"max_usd_per_hr": 1.0},
        lifecycle={"budget": 2.0, "max_lifetime": 1800, "idle_timeout": 900},
    )
    result = UnsupportedFieldCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "compute.placement.max_usd_per_hr" in result.message
    assert "watchdog" in result.message
    assert "budget=2.0" in result.message
    assert "max_lifetime=1800.0s" in result.message


def test_skypilot_rate_cap_warn_says_the_budget_arm_is_inactive_at_zero() -> None:
    """Catches quoting a bound the watchdog does not apply.

    ``compute_deadline`` guards the budget arm with ``if budget_usd > 0``, so
    at ``budget: 0`` the ONLY bound is ``max_lifetime`` and the run's dollar
    cost is unbounded. A WARN that still says "bounded by budget/rate" there
    names a cap that never fires.
    """
    cfg = _cfg(
        "skypilot",
        placement={"max_usd_per_hr": 1.0},
        lifecycle={"budget": 0.0, "max_lifetime": 1800, "idle_timeout": 900},
    )
    message = UnsupportedFieldCheck().run(cfg).message
    assert "inactive" in message
    assert "budget=0.0" not in message


def test_modal_rate_cap_warn_names_the_function_timeout() -> None:
    """Catches only skypilot's rate cap being handled.

    Modal declares ``max_usd_per_hr`` UNSUPPORTED too ("the catalog is
    serverless; the cap is skipped") and five shipped modal configs set it —
    an unhandled row would ERROR every one of them. Modal's real bound is
    ``@app.function(timeout=...)``, which the provider sets from
    ``int(spec.lifecycle.boot_timeout_s) or 1800``.
    """
    cfg = _cfg(
        "modal",
        placement={"max_usd_per_hr": 1.0},
        lifecycle={"budget": 1.0, "boot_timeout": 600},
    )
    result = UnsupportedFieldCheck().run(cfg)
    assert result.severity is Severity.WARN
    assert "timeout" in result.message
    assert "600" in result.message


def test_modal_rate_cap_warn_uses_the_1800_fallback_at_boot_timeout_zero() -> None:
    """Catches the WARN printing 0 s where Modal really applies 1800 s.

    ``startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800`` turns a
    zero into 1800; a message echoing the cfg value would tell the operator
    the container dies immediately.
    """
    cfg = _cfg(
        "modal",
        placement={"max_usd_per_hr": 1.0},
        lifecycle={"budget": 1.0, "boot_timeout": 0},
    )
    assert "1800" in UnsupportedFieldCheck().run(cfg).message


def test_runpod_spot_warn_names_the_rate_cap_that_does_reach_the_catalog() -> None:
    """Catches ``spot`` being refused on RunPod.

    RunPod's create mutation is on-demand only, so ``spot: true`` never
    arrives — but ``max_usd_per_hr`` IS consumed there (``filter_offers``
    excludes pod offers above it), and a rate ceiling bounds the same
    overspend risk. That makes it a WARN naming the ceiling, not an ERROR.
    """
    cfg = _cfg("runpod", placement={"spot": True, "max_usd_per_hr": 0.5})
    result = UnsupportedFieldCheck().run(cfg)
    assert result.severity is Severity.WARN
    assert "compute.placement.spot" in result.message
    assert "0.5" in result.message


def test_modal_spot_warns_under_the_same_timeout_bound_as_its_rate_cap() -> None:
    """Catches an ERROR/WARN split that the stated rule cannot re-derive.

    ``@app.function(timeout=...)`` bounds duration x the booked rate, and it
    does so for ``spot`` exactly as it does for ``max_usd_per_hr`` — the two
    rows cannot disagree on severity while sharing a substitute. Modal's
    missing spot pool costs a discount, which is a strictly smaller harm than
    ``accelerator_count``'s wrong hardware; sharing ERROR with it was the
    contradiction.
    """
    cfg = _cfg(
        "modal",
        placement={"spot": True},
        lifecycle={"budget": 1.0, "boot_timeout": 600},
    )
    result = UnsupportedFieldCheck().run(cfg)
    assert result.severity is Severity.WARN
    assert "compute.placement.spot" in result.message
    assert "600" in result.message
    assert "discount" in result.message


def test_only_substitute_free_rows_are_errors() -> None:
    """Pins the exact set of rows that refuse a load.

    Every UNSUPPORTED row on a registered provider must carry a substitute
    except two, and both are deliberate:

    * ``accelerator_count`` — every provider pins one accelerator and no
      other field can deliver a second.
    * ``region`` (added in S2) — RunPod sends no ``dataCenterId`` and Modal
      is passed no ``region=``, so the pin reaches nothing, and no timeout or
      rate cap substitutes for landing in the wrong jurisdiction.

    A THIRD name appearing here means a row was shipped without a substitute
    and now refuses loads; a name disappearing means a substitute was added
    (or a declaration flipped to CONSUMED) without anyone saying so. Both are
    decisions, not accidents, which is why this asserts the set rather than a
    count.
    """
    from kinoforge.core.capabilities import consumes_for
    from kinoforge.core.interfaces import FieldSupport

    errored: set[str] = set()
    for provider in ("runpod", "skypilot", "modal", "local"):
        declared = consumes_for(provider)
        unsupported = {
            name
            for name in PlacementConfig.model_fields
            if declared.get(name) is FieldSupport.UNSUPPORTED
        }
        # A non-default for every unsupported field at once: the strongest
        # probe, since one call exercises every row the provider has.
        probe = {
            "accelerators": ["A100"],
            "accelerator_count": 4,
            "min_vram_gb": 79,
            "min_cuda": "12.9",
            "disk_gb": 321,
            "region": "kf-probe-region",
            "spot": True,
            "max_usd_per_hr": 9.75,
        }
        # Every UNSUPPORTED row must have a probe value, or the strongest-probe
        # claim above quietly stops being true for the row nobody added.
        assert unsupported <= set(probe), sorted(unsupported - set(probe))
        # The compute-level rows get the same treatment: every one of them
        # set to a non-default at once, so a substitute missing from any is
        # visible here rather than to the operator it refuses.
        compute_probe = {
            "mode": "serverless",
            "tags": {"probe": "value"},
            "heartbeat_mode": "graphql-tag",
            "warm_reuse_auto_attach": False,
        }
        compute_unsupported = {
            name: value
            for name, value in compute_probe.items()
            if declared.get(name) is FieldSupport.UNSUPPORTED
        }
        cfg = _cfg(
            provider,
            placement={k: probe[k] for k in unsupported},
            compute_level=compute_unsupported,
        )
        errored |= {
            gap.field
            for gap in evaluate_field_gaps(cfg)
            if gap.severity is Severity.ERROR
        }
    assert errored == {
        "compute.placement.accelerator_count",
        "compute.placement.region",
    }


# ---------------------------------------------------------------------------
# Compute-level rows (S2) — the block outside `placement`
# ---------------------------------------------------------------------------


def test_unsupported_compute_level_field_reports_its_real_path() -> None:
    """A compute-level finding names ``compute.mode``, not a placement path.

    Bug caught: the module hardcoded the ``compute.placement`` prefix, so the
    first compute-level row would have sent the operator to
    ``compute.placement.mode`` — a key that not only does not exist but is
    actively refused by ``PlacementConfig(extra="forbid")``, making the
    suggested fix impossible to apply.
    """
    result = UnsupportedFieldCheck().run(
        _cfg("skypilot", compute_level={"mode": "serverless"})
    )
    assert result.passed is False
    assert "compute.mode" in result.message
    assert "compute.placement.mode" not in result.message


def test_a_compute_level_field_left_at_its_default_is_not_a_finding() -> None:
    """The default comparison applies to the compute block too.

    Bug caught: reporting every UNSUPPORTED compute-level key regardless of
    value would fire on all 45 `mode: pod` configs and on every config that
    writes `warm_reuse_auto_attach: true` — findings for values the operator
    never chose, which is what the default comparison exists to prevent.
    """
    gaps = evaluate_field_gaps(
        _cfg("skypilot", compute_level={"mode": "pod", "warm_reuse_auto_attach": True})
    )
    assert [g.field for g in gaps if g.field.startswith("compute.")] == []


def test_warm_reuse_auto_attach_warns_naming_the_orchestrator() -> None:
    """No provider reads the flag, and the CLI honouring it is real coverage.

    Bug caught: declaring the flag UNSUPPORTED without a substitute would
    make an ERROR out of a key that works perfectly — it is simply honoured
    before any provider is called.
    """
    result = UnsupportedFieldCheck().run(
        _cfg("runpod", compute_level={"warm_reuse_auto_attach": False})
    )
    assert result.severity is Severity.WARN
    assert "compute.warm_reuse_auto_attach" in result.message
    assert "pre-launch warm scan" in result.message


def test_heartbeat_mode_is_consumed_on_runpod_and_warned_elsewhere() -> None:
    """Mirrors ``_adapters.build_heartbeat_endpoint_for``'s real dispatch.

    Bug caught: declaring the substrate CONSUMED everywhere would tell a
    skypilot operator their instance-side heartbeat is live when the dispatch
    raises for that provider — the difference between "the cluster is alive"
    and "the controller is alive".
    """
    runpod = evaluate_field_gaps(
        _cfg("runpod", compute_level={"heartbeat_mode": "graphql-tag"})
    )
    assert [g.field for g in runpod if g.field == "compute.heartbeat_mode"] == []

    result = UnsupportedFieldCheck().run(
        _cfg("skypilot", compute_level={"heartbeat_mode": "graphql-tag"})
    )
    assert result.severity is Severity.WARN
    assert "compute.heartbeat_mode" in result.message
    assert "orchestrator-clock heartbeat" in result.message


def test_local_is_told_its_image_goes_nowhere() -> None:
    """``image`` is UNSUPPORTED on local and every cfg writes one.

    Bug caught: treating a field with no default as "unwritten" would make
    the one provider that genuinely ignores the image say nothing about it.
    """
    result = UnsupportedFieldCheck().run(_cfg("local"))
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "compute.image" in result.message


@pytest.mark.parametrize("field", ["disk_gb", "accelerator_count"])
def test_local_placement_rows_warn_because_local_launches_nothing(field: str) -> None:
    """Pins the blanket-WARN policy for the unbilled, launch-nothing provider.

    ``local`` declares ``accelerators``, ``accelerator_count``, ``disk_gb``
    and ``spot`` all UNSUPPORTED, and no shipped config writes a placement
    block against it — so without this test the ``_PROVIDER_FALLBACK`` branch
    is never entered and the policy is unpinned in BOTH directions. The
    ``accelerator_count`` case is the sharp one: it is the ERROR row
    everywhere else, and here it must not refuse a dry run, because the local
    provider starts no container and allocates no hardware.
    """
    value = 200 if field == "disk_gb" else 4
    result = UnsupportedFieldCheck().run(_cfg("local", placement={field: value}))
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert f"compute.placement.{field}" in result.message
    assert "local starting nothing at all" in result.message
    assert "cost_rate_usd_per_hr=0.0" in result.message


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_every_violation_lands_in_one_result_at_the_worst_severity() -> None:
    """Catches an aggregation that reports only the first gap, or that lets a
    WARN mask an ERROR. The operator must see both lines and be refused."""
    cfg = _cfg(
        "skypilot",
        placement={"disk_gb": 150, "max_usd_per_hr": 1.0, "accelerator_count": 2},
    )
    result = UnsupportedFieldCheck().run(cfg)
    assert result.severity is Severity.ERROR
    assert "compute.placement.disk_gb" in result.message
    assert "compute.placement.max_usd_per_hr" in result.message
    assert "compute.placement.accelerator_count" in result.message
    assert result.message.count("[WARN]") == 2
    assert result.message.count("[ERROR]") == 1


def test_check_is_static_and_never_auto_fixes() -> None:
    """Catches an auto-fix that silently deletes the operator's value — the
    exact silent discard this check exists to end."""
    check = UnsupportedFieldCheck()
    assert check.category is CheckCategory.STATIC
    assert check.auto_fix(_cfg("skypilot", placement={"disk_gb": 150})) is None


# ---------------------------------------------------------------------------
# ForeignNamespaceCheck
# ---------------------------------------------------------------------------


def test_foreign_namespace_warns_and_names_it() -> None:
    """Catches a foreign namespace validating clean.

    ``backend_options.runpod.cloud_type`` under ``provider: skypilot`` is
    validated against RunPod's Options model and then never read by anything —
    the operator's secure-cloud pin is inert.
    """
    cfg = _cfg(
        "skypilot",
        backend_options={"runpod": {"cloud_type": "secure"}},
    )
    result = ForeignNamespaceCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "runpod" in result.message
    assert "cloud_type" in result.message


def test_the_selected_providers_own_namespace_passes() -> None:
    """Catches a check that warns on any namespace at all, which would fire on
    every config that uses the escape hatch as designed."""
    cfg = _cfg("skypilot", backend_options={"skypilot": {"retry_until_up": True}})
    assert ForeignNamespaceCheck().run(cfg).passed


def test_foreign_namespace_check_never_auto_fixes() -> None:
    """Catches an auto-fix silently dropping a namespace block the operator may
    have written for a sibling config they are about to switch to."""
    check = ForeignNamespaceCheck()
    assert check.severity is Severity.WARN
    assert check.category is CheckCategory.STATIC
    assert check.auto_fix(_cfg("skypilot", backend_options={"runpod": {}})) is None


# ---------------------------------------------------------------------------
# Nothing that ships today is refused
# ---------------------------------------------------------------------------


def _shipped_configs() -> list[Path]:
    """Return every shipped example cfg carrying a ``compute`` mapping.

    Recursive on purpose, unlike the top-level-only sweep in
    ``test_shipped_configs_capabilities.py``: 24 of the 27 configs that set
    ``placement.disk_gb`` live under ``grids/`` and ``extras/``, and they are
    exactly the ones this severity ruling exists to keep loadable.
    """
    out: list[Path] = []
    for path in sorted(CONFIG_DIR.rglob("*.y*ml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("compute"), dict):
            out.append(path)
    return out


@pytest.mark.parametrize("path", _shipped_configs(), ids=str)
def test_no_shipped_config_is_refused_by_either_check(path: Path) -> None:
    """Catches the severity table refusing a config that ships today.

    The whole amendment exists because a uniform ERROR would refuse 15 of
    them. This is the guard that keeps a future row from reintroducing that.
    """
    cfg = _parse_cfg_raw(path.read_text(encoding="utf-8"), yaml_path=path.resolve())
    for check in (UnsupportedFieldCheck(), ForeignNamespaceCheck()):
        if not check.applies_to(cfg):
            continue
        result = check.run(cfg)
        assert result.passed or result.severity is Severity.WARN, result.message


@pytest.mark.parametrize("path", _shipped_configs(), ids=str)
def test_no_shipped_config_carries_a_foreign_namespace(path: Path) -> None:
    """Catches a Task-4 namespace migration leaving a block behind.

    Task 4 moved the last vendor keys into their owners' namespaces; nothing
    shipped should now name a namespace other than its own provider.
    """
    cfg = _parse_cfg_raw(path.read_text(encoding="utf-8"), yaml_path=path.resolve())
    assert ForeignNamespaceCheck().run(cfg).passed


def test_both_checks_are_registered_for_doctor() -> None:
    """Catches the missing import in ``checks/__init__``: a check absent from
    the default registry passes its unit tests and never runs anywhere."""
    import kinoforge.validation.checks  # noqa: F401 — self-register built-ins

    names = default_registry().all_names()
    assert UnsupportedFieldCheck().name in names
    assert ForeignNamespaceCheck().name in names
