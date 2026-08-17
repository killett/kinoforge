"""ProviderCapabilityCheck — STATIC, no auto-fix.

Compares the guardrails a cfg asserts against what the selected provider
declares it can enforce (design doc
docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md).

Severity is risk-covered downgrade: ERROR when no declared capability bounds
the same SPEND risk, WARN naming the substitute when one does. Rows whose
risk is not spend (a wrong warm-attach, a missed stall) never escalate past
WARN — design doc §6.1 requires that nothing shipping today is refused.
Controller-side reaping never counts as coverage — it requires an operator
to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.capabilities import (
    Capability,
    WorkloadShape,
    capabilities_for,
    provider_billed,
    provider_registered,
)
from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


@dataclass(frozen=True)
class Gap:
    """One guardrail the selected provider cannot enforce.

    Attributes:
        field: Dotted cfg path the operator wrote.
        risk: One-line description of what the guardrail prevents.
        missing: The capability that would enforce it.
        substitute: A declared capability that bounds the same risk, or None.
        severity: ERROR only when the risk is spend AND nothing substitutes;
            WARN otherwise.
        detail: Operator-facing explanation, including the numeric bound the
            substitute actually enforces.
    """

    field: str
    risk: str
    missing: Capability
    substitute: Capability | None
    severity: Severity
    detail: str


#: field -> (risk, primary capability, accepted substitutes, spend_risk)
_RISK_ROWS: tuple[tuple[str, str, Capability, tuple[Capability, ...], bool], ...] = (
    (
        "compute.lifecycle.max_lifetime",
        "the instance outlives the controller",
        Capability.ON_INSTANCE_DEADLINE,
        (),
        True,
    ),
    (
        "compute.lifecycle.idle_timeout",
        "the instance is alive but doing nothing",
        Capability.IDLE_AUTOSTOP,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.job_timeout",
        "a single job runs away",
        Capability.JOB_TIMEOUT,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.heartbeat_interval_s",
        "the liveness signal is fiction",
        Capability.HEARTBEAT_READ,
        (),
        False,
    ),
    (
        "compute.lifecycle.stall_window_s",
        "the GPU is idle mid-job",
        Capability.UTIL_SNAPSHOT,
        (),
        False,
    ),
)

_ALWAYS_EVALUATED = frozenset({"compute.lifecycle.max_lifetime"})

_DETAIL: dict[Capability, str] = {
    Capability.HEARTBEAT_READ: (
        "no wire-level heartbeat read; last_heartbeat is the orchestrator "
        "clock, so it proves the controller is alive, not the instance"
    ),
    Capability.IDLE_AUTOSTOP: (
        "provider-side autostop does not fire for this workload shape"
    ),
    Capability.JOB_TIMEOUT: "the provider does not enforce a per-job timeout",
    Capability.UTIL_SNAPSHOT: "no utilisation wire path; stall detection cannot run",
    Capability.ON_INSTANCE_DEADLINE: (
        "nothing on the instance terminates it if the controller dies"
    ),
}


def _substitute_bound(cfg: Config, substitute: Capability) -> str:
    """Return the numeric bound ``substitute`` actually enforces, if knowable.

    Args:
        cfg: The loaded Config whose lifecycle carries the bound.
        substitute: The covering capability.

    Returns:
        A short parenthetical such as ``" at max_lifetime=1800.0s"``, or the
        empty string when no numeric bound maps onto the capability.
    """
    lifecycle = cfg.compute.lifecycle if cfg.compute is not None else None
    if lifecycle is None:
        return ""
    if substitute is Capability.ON_INSTANCE_DEADLINE:
        return f" at max_lifetime={lifecycle.max_lifetime}s"
    if substitute is Capability.IDLE_AUTOSTOP:
        return f" at idle_timeout={lifecycle.idle_timeout}s"
    if substitute is Capability.JOB_TIMEOUT:
        return f" at job_timeout={lifecycle.job_timeout}s"
    return ""


def evaluate_capability_gaps(cfg: Config, shape: WorkloadShape) -> list[Gap]:
    """Return every guardrail ``cfg`` asserts that its provider cannot enforce.

    Args:
        cfg: A loaded Config with a ``compute`` block.
        shape: Workload shape the declaration is evaluated at.

    Returns:
        Gaps in ``_RISK_ROWS`` order; empty when the provider covers
        everything the cfg asks for, and empty for an unregistered provider
        (no declaration exists to compare against — ``registry.get_provider``
        refuses that name at launch with the accurate message).
    """
    if cfg.compute is None:
        return []
    provider = cfg.compute.provider
    if not provider_registered(provider):
        return []
    declared = capabilities_for(provider, shape)
    billed = provider_billed(provider)
    lifecycle = cfg.compute.lifecycle
    set_fields = set(lifecycle.model_fields_set) if lifecycle is not None else set()

    gaps: list[Gap] = []
    for field, risk, primary, substitutes, spend_risk in _RISK_ROWS:
        leaf = field.rsplit(".", 1)[1]
        asserted = field in _ALWAYS_EVALUATED or leaf in set_fields
        if not asserted:
            continue
        if spend_risk and not billed:
            continue
        if primary in declared:
            continue
        covering = next((s for s in substitutes if s in declared), None)
        gaps.append(
            Gap(
                field=field,
                risk=risk,
                missing=primary,
                substitute=covering,
                severity=(
                    Severity.ERROR if covering is None and spend_risk else Severity.WARN
                ),
                detail=_DETAIL[primary]
                + (
                    f"; bounded instead by {covering.value}"
                    f"{_substitute_bound(cfg, covering)}"
                    if covering is not None
                    else ""
                ),
            )
        )
    return gaps


def _infer_shape(cfg: Config) -> WorkloadShape:
    """Infer the workload shape from cfg (see Task 5 for the launch re-check).

    BATCH iff the rendered provision will carry ``run_cmd=[]`` — upscale-only
    diffusers cfgs and interpolate-only cfgs. Mirrors the upscale-only
    predicate already used in ``core/config.py``.

    Args:
        cfg: The loaded Config.

    Returns:
        The inferred workload shape.
    """
    diffusers = cfg.engine.diffusers if cfg.engine is not None else None
    if diffusers is not None and diffusers.upscale_only:
        return WorkloadShape.BATCH
    if cfg.interpolate is not None and not cfg.models:
        return WorkloadShape.BATCH
    return WorkloadShape.SERVER


class ProviderCapabilityCheck:
    """STATIC — report guardrails the selected provider cannot enforce."""

    name: str = "provider_capabilities"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    def applies_to(self, cfg: Config) -> bool:
        """Apply to any cfg with a compute block naming a registered provider.

        An unregistered provider is skipped rather than reported as
        declaring nothing: emitting "``foo`` enforces every guardrail" would
        be a lie and emitting a gap would preempt the accurate
        unknown-provider refusal raised by ``registry.get_provider``.

        Args:
            cfg: The candidate Config.

        Returns:
            True iff ``cfg.compute`` is set and its provider is registered.
        """
        return cfg.compute is not None and provider_registered(cfg.compute.provider)

    def run(self, cfg: Config) -> CheckResult:
        """Aggregate every gap into one result at the highest severity found.

        Args:
            cfg: The Config to evaluate.

        Returns:
            One CheckResult; ``severity`` is ERROR iff any gap is ERROR.
        """
        gaps = evaluate_capability_gaps(cfg, _infer_shape(cfg))
        provider = cfg.compute.provider if cfg.compute is not None else "?"
        if not gaps:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=(f"{provider} enforces every guardrail this cfg asserts"),
            )
        worst = (
            Severity.ERROR
            if any(g.severity is Severity.ERROR for g in gaps)
            else Severity.WARN
        )
        lines = [
            f"{g.field}: {provider} cannot enforce {g.missing.value} "
            f"({g.risk}) — {g.detail}"
            for g in gaps
        ]
        return CheckResult(
            name=self.name,
            passed=False,
            severity=worst,
            message="\n  ".join([f"{provider} guardrail gaps:", *lines]),
            fix_suggestion=(
                "drop the guardrail, or select a provider that declares it "
                "(see docs/lifecycle.md for the capability matrix)"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """Never auto-fix — silently rewriting a guardrail is the bug.

        Args:
            cfg: The Config that failed the check.

        Returns:
            Always ``None``.
        """
        return None


register(ProviderCapabilityCheck())
