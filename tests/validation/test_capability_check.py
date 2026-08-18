"""ProviderCapabilityCheck (Brief 2, Task 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.config import load_config
from kinoforge.core.errors import ValidationError
from kinoforge.validation.checks.capabilities import (
    ProviderCapabilityCheck,
    evaluate_capability_gaps,
    infer_shape,
)
from kinoforge.validation.protocol import Severity

_CFG_TEMPLATE = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: {provider}
  image: "example/image:latest"
  mode: pod
  lifecycle:
    idle_timeout: 180
    max_lifetime: 1800
    budget: 0.10
    heartbeat_interval_s: 30
"""


def _write_cfg(tmp_path: Path, *, provider: str) -> Path:
    """Write a minimal valid cfg pinned to ``provider`` and return its path.

    Args:
        tmp_path: pytest tmp_path fixture directory.
        provider: ``compute.provider`` value to write.

    Returns:
        Path to the written YAML file.
    """
    path = tmp_path / f"{provider}.yaml"
    path.write_text(_CFG_TEMPLATE.format(provider=provider))
    return path


def test_heartbeat_gap_on_skypilot_is_a_warn_naming_the_clock(tmp_path: Path) -> None:
    """Catches a severity mapping that errors on a covered risk, and a
    message that does not tell the operator what the signal really means."""
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    hb = [g for g in gaps if g.field == "compute.lifecycle.heartbeat_interval_s"]
    assert len(hb) == 1
    assert hb[0].severity is Severity.WARN
    assert hb[0].missing is Capability.HEARTBEAT_READ
    result = ProviderCapabilityCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "orchestrator clock" in result.message


def test_idle_timeout_gap_names_the_substitute_and_its_bound(tmp_path: Path) -> None:
    """Catches a WARN that says 'unsupported' without telling the operator
    what actually bounds the run.

    The bound assertion specifically catches `_substitute_bound` regressing
    to `return ""`: design §6 requires the WARN to name the substitute AND
    the numeric bound it actually enforces, and naming the capability alone
    still leaves the operator guessing what caps the run.
    """
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    idle = [g for g in gaps if g.field == "compute.lifecycle.idle_timeout"]
    assert len(idle) == 1
    assert idle[0].substitute is Capability.ON_INSTANCE_DEADLINE
    assert idle[0].severity is Severity.WARN
    assert "ON_INSTANCE_DEADLINE" in idle[0].detail
    assert "at most max_lifetime=1800.0s" in idle[0].detail
    assert "at most max_lifetime=1800.0s" in ProviderCapabilityCheck().run(cfg).message


def test_uncovered_spend_risk_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a missing risk row letting an unbounded-spend config launch:
    a billed provider with no deadline and no autostop must not load."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError) as exc:
        load_config(_write_cfg(tmp_path, provider="skypilot"))
    assert "ON_INSTANCE_DEADLINE" in str(exc.value)
    assert "skypilot" in str(exc.value)


def test_unbilled_provider_skips_the_spend_rows(tmp_path: Path) -> None:
    """Catches `billed` being ignored, which would error every local run."""
    cfg = load_config(_write_cfg(tmp_path, provider="local"))
    # Stronger than filtering for max_lifetime: local declares HEARTBEAT_READ
    # and UTIL_SNAPSHOT, so with the spend rows correctly skipped NOTHING is
    # left to report. Any gap at all is a regression.
    assert evaluate_capability_gaps(cfg, WorkloadShape.SERVER) == []


def test_unregistered_provider_is_not_a_capability_gap(tmp_path: Path) -> None:
    """Catches the check claiming an unregistered provider "cannot enforce
    ON_INSTANCE_DEADLINE". There is no declaration to compare against, and
    `get_provider` already refuses the name at launch with the accurate
    message; a capability ERROR here would preempt it with a wrong one."""
    cfg = load_config(_write_cfg(tmp_path, provider="not-a-real-provider"))
    assert evaluate_capability_gaps(cfg, WorkloadShape.SERVER) == []
    assert ProviderCapabilityCheck().applies_to(cfg) is False


def test_upscale_only_cfg_is_server_shaped_and_still_warns_on_idle_timeout() -> None:
    """Catches `infer_shape` guessing BATCH from `upscale_only`.

    `engines/diffusers/__init__.py:1274` sets `run_cmd=server_cmd`
    unconditionally, so an upscale-only cfg still provisions a long-lived
    server; the `run_cmd=[]` renders live in pipeline STAGES that run against
    an already-provisioned instance (`orchestrator.py:2008-2022`, `:2031-2035`)
    and never provision anything. Guessing BATCH made this cfg report
    idle_timeout as ENFORCED on a cluster where skypilot autostop is provably
    inert — a guardrail claimed but not held, which is the exact failure this
    check exists to surface.
    """
    cfg = load_config(
        Path("examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml")
    )
    assert cfg.engine.diffusers is not None
    assert cfg.engine.diffusers.upscale_only is True
    assert infer_shape(cfg) is WorkloadShape.SERVER

    gaps = evaluate_capability_gaps(cfg, infer_shape(cfg))
    idle = [g for g in gaps if g.field == "compute.lifecycle.idle_timeout"]
    assert len(idle) == 1
    assert idle[0].missing is Capability.IDLE_AUTOSTOP
    assert idle[0].substitute is Capability.ON_INSTANCE_DEADLINE
    assert idle[0].severity is Severity.WARN
    assert "server" in idle[0].detail


def test_check_never_auto_fixes_and_load_preserves_guardrail_values(
    tmp_path: Path,
) -> None:
    """Catches a future auto-fix that silences the diagnostic by rewriting
    the guardrail — how the current state became invisible."""
    path = _write_cfg(tmp_path, provider="skypilot")
    cfg = load_config(path)
    assert ProviderCapabilityCheck().auto_fix(cfg) is None
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    assert cfg.compute.lifecycle.idle_timeout == 180.0
    assert cfg.compute.lifecycle.max_lifetime == 1800.0


_MODAL_CFG = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: modal
  image: "example/image:latest"
  mode: pod
  lifecycle:
    idle_timeout: 180
    max_lifetime: 5400
    boot_timeout: 2700
    budget: 0.10
    heartbeat_interval_s: 30
"""


def test_modal_deadline_bound_is_boot_timeout_and_the_mismatch_warns(
    tmp_path: Path,
) -> None:
    """Modal's deadline is derived from ``boot_timeout``, never ``max_lifetime``.

    ``ModalProvider`` wires only ``scaledown_window_s`` and
    ``startup_timeout_s``; ``max_lifetime`` is never sent to Modal. Bug
    caught (and the reason this test exists): ``_substitute_bound``
    formatting ``max_lifetime`` unconditionally, so a cfg asking for 90 min
    was told its run was "bounded ... at max_lifetime=5400.0s" when Modal
    kills the container at 2700 s. Both numbers must appear, and the WARN
    must say which one is enforced.
    """
    path = tmp_path / "modal.yaml"
    path.write_text(_MODAL_CFG)
    cfg = load_config(path)
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)

    deadline = [g for g in gaps if g.field == "compute.lifecycle.max_lifetime"]
    assert len(deadline) == 1
    assert deadline[0].severity is Severity.WARN
    assert "boot_timeout=2700.0s" in deadline[0].detail
    assert "max_lifetime=5400.0s" in deadline[0].detail
    assert "capped at 2700.0s" in deadline[0].detail
    # The declaration itself is honest and stays: Modal's function timeout
    # really does terminate the container.
    assert [g for g in gaps if g.severity is Severity.ERROR] == []
    rendered = deadline[0].render("modal")
    assert "modal cannot enforce ON_INSTANCE_DEADLINE" not in rendered
    assert "but not at max_lifetime" in rendered


def test_runpod_deadline_bound_is_the_real_selfterm_formula(tmp_path: Path) -> None:
    """runpod's selfterm bound is NOT ``max_lifetime`` and must not print it.

    ``providers/runpod/selfterm.py`` states its own contract: two BOOT-RELATIVE
    caps, ``max_lifetime - time_buffer`` and ``2 * idle_timeout``, whichever
    elapses first. On this very cfg (idle_timeout 180, max_lifetime 1800,
    time_buffer at its 1800 s default) the real lifetime is
    ``min(360, 0) == 0`` — the pod's deadline is already past at boot.

    Bug caught, and it is the one this test previously CAUSED: printing
    ``at max_lifetime=1800.0s``, which overstated the enforced bound by the
    whole of it and was asserted as correct here. The WARN must name the
    formula and its value, and must not claim ``max_lifetime`` is the cap.
    """
    cfg = load_config(_write_cfg(tmp_path, provider="runpod"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    idle = [g for g in gaps if g.field == "compute.lifecycle.idle_timeout"]
    assert len(idle) == 1
    assert idle[0].substitute is Capability.ON_INSTANCE_DEADLINE
    detail = idle[0].detail
    assert "min(2*idle_timeout, max_lifetime-time_buffer)=0.0s" in detail
    assert "already elapsed at boot" in detail
    # The overstatement must be gone, in every phrasing.
    assert "at max_lifetime=1800.0s" not in detail
    assert "at most max_lifetime" not in detail


def test_runpod_bound_tracks_idle_timeout_when_that_is_the_binding_cap(
    tmp_path: Path,
) -> None:
    """The other arm of runpod's ``min``: ``2 * idle_timeout`` binds.

    Catches the formula being hardcoded to one arm — a renderer that always
    reports ``max_lifetime - time_buffer`` would print 5400.0s here while
    selfterm actually reaps at 600.0s.
    """
    path = tmp_path / "runpod-idle.yaml"
    path.write_text(
        _CFG_TEMPLATE.format(provider="runpod").replace(
            "    idle_timeout: 180\n    max_lifetime: 1800\n",
            "    idle_timeout: 300\n    max_lifetime: 7200\n    time_buffer: 1800\n",
        )
    )
    cfg = load_config(path)
    idle = [
        g
        for g in evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
        if g.field == "compute.lifecycle.idle_timeout"
    ]
    assert len(idle) == 1
    # min(2*300, 7200-1800) == 600, not 5400.
    assert "=600.0s" in idle[0].detail
    assert "already elapsed at boot" not in idle[0].detail


def test_skypilot_bound_is_phrased_as_a_ceiling_not_an_exact_deadline(
    tmp_path: Path,
) -> None:
    """``compute_deadline`` returns the EARLIER of lifetime and budget bounds.

    The offer's ``rate_usd_per_hr`` is unknowable at config-load time, so
    ``max_lifetime`` is an upper bound on the enforced deadline and nothing
    stronger. Bug caught: the WARN asserting ``max_lifetime`` as the exact
    deadline, which a budget-bounded cluster falsifies.
    """
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    idle = [
        g
        for g in evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
        if g.field == "compute.lifecycle.idle_timeout"
    ]
    assert len(idle) == 1
    assert "at most max_lifetime=1800.0s" in idle[0].detail


def test_unresolvable_bound_prints_no_number_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider with no known deadline field prints NO bound.

    Design decision this pins: a bare omission is more honest than a wrong
    number. Bug caught: a future provider added to the matrix but not to
    ``_DEADLINE_BOUND`` silently inheriting some other provider's field.
    """
    from kinoforge.validation.checks import capabilities as mod

    monkeypatch.delitem(mod._DEADLINE_BOUND, "skypilot")
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    idle = [
        g
        for g in evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
        if g.field == "compute.lifecycle.idle_timeout"
    ]
    assert len(idle) == 1
    assert "ON_INSTANCE_DEADLINE" in idle[0].detail
    assert " at max_lifetime" not in idle[0].detail
    assert "1800" not in idle[0].detail


_UTIL_GUARDRAIL_CFG = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: skypilot
  image: "example/image:latest"
  mode: pod
  lifecycle:
    idle_timeout: 180
    max_lifetime: 1800
    budget: 0.10
    heartbeat_interval_s: 30
    stall_reap_enabled: true
    restart_loop_reap_enabled: true
"""


def test_util_gated_reap_guardrails_warn_on_a_provider_with_no_util_wire(
    tmp_path: Path,
) -> None:
    """Both util-gated reap predicates need a risk row, keyed on the ENABLE flag.

    ``_stall_reap_predicate`` and ``_restart_loop_reap_predicate`` both
    return False outright when ``provider_util_supported`` is False, so on
    skypilot both guardrails are silently inert. Two bugs caught:

    1. ``restart_loop_*`` having no risk row at all — an operator turning on
       crash-loop reaping on skypilot got zero diagnostic.
    2. The stall row keyed only on ``stall_window_s``, a tuning parameter.
       This cfg leaves the window at its default and writes only
       ``stall_reap_enabled``, which is the field that actually asserts the
       guardrail — so a row keyed on the window alone reports nothing here.
    """
    path = tmp_path / "util.yaml"
    path.write_text(_UTIL_GUARDRAIL_CFG)
    cfg = load_config(path)
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    by_field = {g.field: g for g in gaps}

    for field in (
        "compute.lifecycle.stall_window_s",
        "compute.lifecycle.restart_loop_window_s",
    ):
        assert field in by_field, sorted(by_field)
        assert by_field[field].missing is Capability.UTIL_SNAPSHOT
        # Not spend risk -> never refuses a load that ships today.
        assert by_field[field].severity is Severity.WARN
        assert "util" in by_field[field].detail


def test_every_gap_line_carries_its_own_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mixed ERROR+WARN result must say which line refused the load.

    The check aggregates every gap into ONE CheckResult carrying only the
    worst severity. Bug caught: identical-looking lines, so an operator
    facing a refused load cannot tell the fatal row from the advisory ones
    and has to guess which guardrail to drop.
    """
    from kinoforge.providers.skypilot import SkyPilotProvider

    # Load with the real declaration in effect (ON_INSTANCE_DEADLINE covers
    # max_lifetime), THEN strip it so the mixed-severity result exists to be
    # rendered rather than refusing the load before we can look at it.
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert {g.severity for g in gaps} == {Severity.ERROR, Severity.WARN}

    message = ProviderCapabilityCheck().run(cfg).message
    lines = [ln for ln in message.splitlines() if "compute.lifecycle." in ln]
    assert len(lines) == len(gaps)
    for gap, line in zip(gaps, lines, strict=True):
        assert line.strip().startswith(f"[{gap.severity.name}]")
    assert any(ln.strip().startswith("[ERROR]") for ln in lines)
    assert any(ln.strip().startswith("[WARN]") for ln in lines)


_SKYPILOT_NO_IDLE_CFG = """\
engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: skypilot
  image: "example/image:latest"
  mode: pod
  lifecycle:
    max_lifetime: 28800
    budget: 0.10
"""


def test_unwritten_spend_guardrails_are_still_reported(tmp_path: Path) -> None:
    """The spend rows are evaluated whether or not the YAML writes them.

    This is the gap ``_ALWAYS_EVALUATED`` was extended to close. A skypilot
    cfg that never mentions ``idle_timeout`` still gets ``autostop=120`` set
    on a cluster where autostop provably cannot fire (F1) — the operator did
    not ask for the guardrail, but the guardrail is set on their behalf and
    is inert, and before the extension that shipped with zero diagnostic.

    Same for ``job_timeout``: skypilot enforces no per-job timeout, and the
    cfg carries the 30 m pydantic default regardless.
    """
    path = tmp_path / "no-idle.yaml"
    path.write_text(_SKYPILOT_NO_IDLE_CFG)
    cfg = load_config(path)
    assert cfg.compute is not None
    assert cfg.compute.lifecycle is not None
    # The operator genuinely did not write these.
    assert "idle_timeout" not in cfg.compute.lifecycle.model_fields_set
    assert "job_timeout" not in cfg.compute.lifecycle.model_fields_set

    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    fields = {g.field for g in gaps}
    assert "compute.lifecycle.idle_timeout" in fields
    assert "compute.lifecycle.job_timeout" in fields


def test_always_evaluated_spend_rows_never_error_on_any_registered_provider(
    tmp_path: Path,
) -> None:
    """The property that makes ``_ALWAYS_EVALUATED`` safe, asserted directly.

    Reporting a guardrail the operator never wrote must never REFUSE their
    config (design §6.1: nothing shipping today is refused). That holds for a
    structural reason, not by luck: ``ON_INSTANCE_DEADLINE`` is an accepted
    substitute on both the ``idle_timeout`` and ``job_timeout`` rows, every
    billed provider declares it, and ``local`` is unbilled so the spend rows
    skip it entirely — and a gap with a substitute is WARN by construction.

    Bug caught: a future provider added without ``ON_INSTANCE_DEADLINE``, or
    the substitute column being emptied on either row, which would turn every
    config that omits these fields into a hard load failure.
    """
    for provider in ("local", "runpod", "skypilot", "modal"):
        path = tmp_path / f"{provider}-bare.yaml"
        path.write_text(_SKYPILOT_NO_IDLE_CFG.replace("skypilot", provider))
        cfg = load_config(path)
        for shape in (WorkloadShape.SERVER, WorkloadShape.BATCH):
            gaps = evaluate_capability_gaps(cfg, shape)
            errors = [
                (g.field, g.severity) for g in gaps if g.severity is Severity.ERROR
            ]
            assert errors == [], f"{provider}/{shape.value} produced {errors}"
