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

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kinoforge.core.capabilities import (
    Capability,
    WorkloadShape,
    capabilities_for,
    provider_billed,
    provider_class_for,
    provider_registered,
)
from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

__all__ = [
    "Gap",
    "ProviderCapabilityCheck",
    "evaluate_capability_gaps",
    "infer_shape",
    "rate_source_declared",
]


@dataclass(frozen=True)
class Gap:
    """One guardrail the selected provider cannot enforce as written.

    Attributes:
        field: Dotted cfg path the operator wrote.
        risk: One-line description of what the guardrail prevents.
        missing: The capability that would enforce it.
        substitute: A declared capability that bounds the same risk, or None.
        severity: ERROR only when the risk is spend AND nothing substitutes;
            WARN otherwise.
        detail: Operator-facing explanation, including the numeric bound the
            substitute actually enforces.
        headline: Overrides the default "``<provider>`` cannot enforce
            ``<missing>``" phrasing. Set only where that phrasing would be a
            lie — the provider DOES declare the capability but keys it to a
            different cfg field (see :func:`_deadline_mismatch_gap`).
    """

    field: str
    risk: str
    missing: Capability
    substitute: Capability | None
    severity: Severity
    detail: str
    headline: str | None = None

    def render(self, provider: str) -> str:
        """Return the operator-facing one-liner for this gap.

        The severity is included per line: a mixed ERROR+WARN result is
        aggregated into ONE CheckResult carrying only the worst severity, so
        without it the operator cannot tell which line refused the load from
        the lines that are merely advisory.

        Args:
            provider: The provider kind the gap was evaluated against.

        Returns:
            ``"[ERROR] <field>: <headline> (<risk>) — <detail>"``.
        """
        head = self.headline or f"{provider} cannot enforce {self.missing.value}"
        return (
            f"[{self.severity.name}] {self.field}: {head} ({self.risk}) — {self.detail}"
        )


#: (dotted field, asserting leaf names, risk, primary capability,
#: accepted substitutes, spend_risk).
#:
#: ``leaves`` is a tuple because more than one lifecycle field can assert the
#: same guardrail: a row keyed only on a tuning parameter (``stall_window_s``)
#: misses the operator who wrote the enable flag (``stall_reap_enabled``) and
#: left the window at its default, which is the field that actually asserts
#: the guardrail exists.
_RISK_ROWS: tuple[
    tuple[str, tuple[str, ...], str, Capability, tuple[Capability, ...], bool], ...
] = (
    (
        "compute.lifecycle.max_lifetime",
        ("max_lifetime",),
        "the instance outlives the controller",
        Capability.ON_INSTANCE_DEADLINE,
        (),
        True,
    ),
    (
        "compute.lifecycle.idle_timeout",
        ("idle_timeout",),
        "the instance is alive but doing nothing",
        Capability.IDLE_AUTOSTOP,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.job_timeout",
        ("job_timeout",),
        "a single job runs away",
        Capability.JOB_TIMEOUT,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.heartbeat_interval_s",
        ("heartbeat_interval_s",),
        "the liveness signal is fiction",
        Capability.HEARTBEAT_READ,
        (),
        False,
    ),
    (
        "compute.lifecycle.stall_window_s",
        ("stall_reap_enabled", "stall_window_s"),
        "the GPU is idle mid-job",
        Capability.UTIL_SNAPSHOT,
        (),
        False,
    ),
    (
        "compute.lifecycle.restart_loop_window_s",
        ("restart_loop_reap_enabled", "restart_loop_window_s"),
        "the container is crash-looping, billing without progressing",
        Capability.UTIL_SNAPSHOT,
        (),
        False,
    ),
)

#: Lifecycle rows evaluated even when the operator wrote nothing.
#:
#: All three spend rows are here. Every one of them carries a pydantic default
#: (``max_lifetime`` 5 h, ``idle_timeout`` 2 h, ``job_timeout`` 30 m), so every
#: provisioned instance asserts all three caps whether or not the YAML mentions
#: them — and the question "does anything actually enforce this cap?" is worth
#: answering in exactly the case where the operator never thought about it.
#:
#: The concrete gap this closes: a skypilot cfg that omits ``idle_timeout``
#: still gets ``autostop=120`` set on a cluster where it provably cannot fire
#: (F1). Before ``idle_timeout`` joined this set that shipped with zero
#: diagnostic.
#:
#: This does NOT refuse anything that ships today, and the reason is
#: structural rather than lucky: ``ON_INSTANCE_DEADLINE`` is an accepted
#: substitute on both the ``idle_timeout`` and ``job_timeout`` rows, every
#: BILLED provider declares it, and ``local`` is unbilled so the spend rows
#: skip it entirely. A gap with a substitute is WARN by construction, so no
#: registered provider can produce an ERROR from these two rows. Measured
#: across all four registered providers x both WorkloadShapes: zero ERRORs,
#: WARN only. Re-run that sweep before adding a row here — the property that
#: makes this safe is the substitute column, not the field list.
_ALWAYS_EVALUATED = frozenset(
    {
        "compute.lifecycle.max_lifetime",
        "compute.lifecycle.idle_timeout",
        "compute.lifecycle.job_timeout",
    }
)


def _runpod_deadline_phrase(lifecycle: Any) -> str:  # noqa: ANN401 — LifecycleConfig
    """Render runpod's real selfterm bound, which is NOT ``max_lifetime``.

    ``providers/runpod/selfterm.py`` states its own contract: two independent
    BOOT-RELATIVE caps, ``start + max_lifetime - time_buffer`` and
    ``start + 2 * idle_timeout``, whichever elapses first. So the enforced
    lifetime is ``min(2 * idle_timeout, max_lifetime - time_buffer)`` and
    printing ``max_lifetime`` overstates it — by an unbounded margin, and on
    common configs by all of it.

    Args:
        lifecycle: The cfg's ``LifecycleConfig``.

    Returns:
        A phrase naming the formula and the value it evaluates to.
    """
    effective = min(
        2.0 * lifecycle.idle_timeout, lifecycle.max_lifetime - lifecycle.time_buffer
    )
    phrase = f"at min(2*idle_timeout, max_lifetime-time_buffer)={effective}s"
    if effective <= 0:
        # Not a rounding curiosity: max_lifetime <= time_buffer means the pod's
        # own deadline is already past the moment it boots. Say so rather than
        # let "=0.0s" read as "unbounded".
        phrase += " — already elapsed at boot, so selfterm reaps immediately"
    return phrase


def _skypilot_deadline_phrase(lifecycle: Any) -> str:  # noqa: ANN401 — LifecycleConfig
    """Render skypilot's bound as a CEILING, not an exact deadline.

    ``watchdog.compute_deadline`` returns the EARLIER of the lifetime bound and
    a budget bound (``budget_usd / rate_usd_per_hr``). The offer's rate is not
    knowable at config-load time, so ``max_lifetime`` is an upper bound on the
    enforced deadline and nothing stronger — hence "at most".

    Args:
        lifecycle: The cfg's ``LifecycleConfig``.

    Returns:
        A phrase naming the ceiling.
    """
    return f"at most max_lifetime={lifecycle.max_lifetime}s"


def _modal_deadline_phrase(lifecycle: Any) -> str:  # noqa: ANN401 — LifecycleConfig
    """Render modal's bound, which derives from ``boot_timeout``.

    Args:
        lifecycle: The cfg's ``LifecycleConfig``.

    Returns:
        A phrase naming the field the function timeout is derived from.
    """
    return f"at boot_timeout={lifecycle.boot_timeout}s"


@dataclass(frozen=True)
class _DeadlineBound:
    """How one provider's instance-side deadline is really derived.

    Attributes:
        mechanism: Operator-facing name of the thing that terminates.
        derives_from_max_lifetime: Whether cfg's ``max_lifetime`` is an input
            to the enforced deadline at all. False triggers the
            :func:`_deadline_mismatch_gap` WARN — the operator wrote a
            guardrail value that never reaches the provider.
        source_field: The lifecycle field the deadline is derived from, used
            by that WARN. Only meaningful when the flag above is False.
        phrase: ``LifecycleConfig -> str``; renders the bound for the WARN
            line. Kept a callable, not a field name, because two of the three
            providers do not have a single field to name.
    """

    mechanism: str
    derives_from_max_lifetime: bool
    source_field: str
    phrase: Callable[[Any], str]


#: provider kind -> how its ON_INSTANCE_DEADLINE bound is really computed.
#:
#: Design §6 requires an ON_INSTANCE_DEADLINE WARN to name the numeric bound it
#: actually enforces. No two providers compute it the same way, and NONE of
#: them simply enforces ``max_lifetime``:
#:
#: * runpod takes ``min(2 * idle_timeout, max_lifetime - time_buffer)``.
#: * skypilot takes the earlier of ``max_lifetime`` and a budget bound whose
#:   rate is unknown at load, so ``max_lifetime`` is a ceiling.
#: * modal never receives ``max_lifetime`` at all; its function timeout is
#:   derived from ``boot_timeout``.
#:
#: A provider absent from this table prints NO bound: a bare omission is more
#: honest than a wrong number, and that rule applies to the entries here too —
#: which is why none of them prints a bare ``max_lifetime``.
_DEADLINE_BOUND: dict[str, _DeadlineBound] = {
    "runpod": _DeadlineBound(
        mechanism="the in-pod selfterm watchdog (selfterm.RENDER)",
        derives_from_max_lifetime=True,
        source_field="max_lifetime",
        phrase=_runpod_deadline_phrase,
    ),
    "skypilot": _DeadlineBound(
        mechanism="the instance-side watchdog (watchdog.compute_deadline)",
        derives_from_max_lifetime=True,
        source_field="max_lifetime",
        phrase=_skypilot_deadline_phrase,
    ),
    "modal": _DeadlineBound(
        mechanism="Modal's @app.function(timeout=...)",
        derives_from_max_lifetime=False,
        source_field="boot_timeout",
        phrase=_modal_deadline_phrase,
    ),
}

#: primary capability -> operator-facing prose. ``{shape}`` is substituted
#: with the evaluated WorkloadShape so a shape-conditional declaration says
#: WHICH shape it was judged at.
_DETAIL: dict[Capability, str] = {
    Capability.HEARTBEAT_READ: (
        "no wire-level heartbeat read; last_heartbeat is the orchestrator "
        "clock, so it proves the controller is alive, not the instance"
    ),
    Capability.IDLE_AUTOSTOP: (
        "provider-side autostop does not fire at workload shape {shape}"
    ),
    Capability.JOB_TIMEOUT: "the provider does not enforce a per-job timeout",
    Capability.UTIL_SNAPSHOT: (
        "no utilisation wire path; the reaper's util-gated predicates "
        "return False, so stall / restart-loop detection never fires"
    ),
    Capability.ON_INSTANCE_DEADLINE: (
        "nothing on the instance terminates it if the controller dies"
    ),
}


def _substitute_bound(cfg: Config, substitute: Capability, provider: str) -> str:
    """Return the bound ``substitute`` actually enforces, if knowable.

    Design doc §6 requires a WARN to name the substitute *and the bound it
    actually enforces* — "bounded instead by ON_INSTANCE_DEADLINE" alone
    still leaves the operator guessing what caps the run.

    The bound is resolved PER PROVIDER via :data:`_DEADLINE_BOUND`, because no
    two providers compute it the same way and none of them simply enforces
    ``max_lifetime``: runpod takes
    ``min(2 * idle_timeout, max_lifetime - time_buffer)``, skypilot's is
    ``max_lifetime`` OR an earlier budget bound, and modal never receives
    ``max_lifetime`` at all. Printing ``max_lifetime`` unconditionally named a
    cap that no provider enforces.

    ``ON_INSTANCE_DEADLINE`` is the only capability any ``_RISK_ROWS``
    substitute column lists, so it is the only arm here; add a branch when a
    row adds a substitute, not before.

    Args:
        cfg: The loaded Config whose lifecycle carries the bound.
        substitute: The covering capability.
        provider: The provider kind whose enforcement is being described.

    Returns:
        A short suffix such as ``" at most max_lifetime=1800.0s"``, or the
        empty string when no bound is known for this provider/capability.
    """
    lifecycle = cfg.compute.lifecycle if cfg.compute is not None else None
    if lifecycle is None or substitute is not Capability.ON_INSTANCE_DEADLINE:
        return ""
    entry = _DEADLINE_BOUND.get(provider)
    if entry is None:
        return ""
    return f" {entry.phrase(lifecycle)}"


def _deadline_mismatch_gap(
    cfg: Config, provider: str, set_fields: set[str]
) -> Gap | None:
    """Return a WARN when ``max_lifetime`` is not an input to the deadline.

    Modal genuinely terminates the container at its ``@app.function(timeout=)``
    deadline, so ``ON_INSTANCE_DEADLINE`` stays declared. But that timeout is
    derived from ``boot_timeout``, and ``max_lifetime`` is never sent to Modal.
    An operator who writes ``max_lifetime: 90m`` and is capped at 45 m should
    learn it at load, with both numbers, rather than after the container dies.

    The trigger is ``derives_from_max_lifetime``, not "the bound equals
    ``max_lifetime``". runpod's bound is not ``max_lifetime`` either, but
    ``max_lifetime`` IS one of its inputs and the WARN line already prints the
    real formula — so there is nothing here the operator has not been told.

    Args:
        cfg: The loaded Config.
        provider: The provider kind.
        set_fields: Lifecycle field names the operator wrote explicitly.

    Returns:
        A WARN Gap, or None when ``max_lifetime`` reaches the provider at all,
        the provider has no known bound, or the cfg never wrote
        ``max_lifetime``.
    """
    lifecycle = cfg.compute.lifecycle if cfg.compute is not None else None
    entry = _DEADLINE_BOUND.get(provider)
    if lifecycle is None or entry is None or "max_lifetime" not in set_fields:
        return None
    if entry.derives_from_max_lifetime:
        return None
    bound_value = getattr(lifecycle, entry.source_field, None)
    if bound_value is None:
        return None
    return Gap(
        field="compute.lifecycle.max_lifetime",
        risk="the instance outlives the controller",
        missing=Capability.ON_INSTANCE_DEADLINE,
        substitute=Capability.ON_INSTANCE_DEADLINE,
        severity=Severity.WARN,
        headline=(f"{provider} enforces ON_INSTANCE_DEADLINE, but not at max_lifetime"),
        detail=(
            f"the enforced deadline is {entry.mechanism}, derived from "
            f"{entry.source_field}={bound_value}s; this cfg's "
            f"max_lifetime={lifecycle.max_lifetime}s never reaches {provider}, "
            f"so the instance is capped at {bound_value}s"
        ),
    )


def rate_source_declared(provider_cls: type) -> list[Gap]:
    """Return an ERROR gap when *provider_cls* declares no rate source.

    A provider that declares neither ``RATE_READBACK`` nor
    ``RATE_DETERMINISTIC`` cannot answer what a launched instance bills, so
    ``max_usd_per_hr`` cannot be enforced against it — and the only honest
    thing the enforcement point could do with that silence is destroy every
    instance the provider ever launches. The refusal belongs here, at load,
    where it costs nothing.

    Args:
        provider_cls: The provider class to inspect.

    Returns:
        One ERROR gap, or an empty list when a rate source is declared.
    """
    declare = getattr(provider_cls, "capabilities", None)
    declared: frozenset[Capability] = declare() if declare is not None else frozenset()
    if declared & {Capability.RATE_READBACK, Capability.RATE_DETERMINISTIC}:
        return []
    name = getattr(provider_cls, "name", provider_cls.__name__)
    return [
        Gap(
            field="compute.placement.max_usd_per_hr",
            risk="the run books an instance above the rate ceiling",
            missing=Capability.RATE_READBACK,
            substitute=None,
            severity=Severity.ERROR,
            headline=(f"{name} declares neither RATE_READBACK nor RATE_DETERMINISTIC"),
            detail=(
                f"{name} declares neither RATE_READBACK nor RATE_DETERMINISTIC, "
                f"so kinoforge cannot know what a launched instance bills and "
                f"max_usd_per_hr cannot be enforced. A provider that chooses "
                f"its own SKU declares RATE_READBACK and implements "
                f"realized_rate(); one that books the SKU it is handed "
                f"declares RATE_DETERMINISTIC."
            ),
        )
    ]


def evaluate_capability_gaps(cfg: Config, shape: WorkloadShape) -> list[Gap]:
    """Return every guardrail ``cfg`` asserts that its provider cannot enforce.

    Args:
        cfg: A loaded Config with a ``compute`` block.
        shape: Workload shape the declaration is evaluated at.

    Returns:
        Gaps in ``_RISK_ROWS`` order; empty when the provider covers
        everything the cfg asks for, and empty for an unregistered provider
        (no declaration exists to compare against — ``registry.get_provider``
        refuses that name at launch with the accurate message). A provider
        that DECLARES ``ON_INSTANCE_DEADLINE`` but keys it to a cfg field
        other than ``max_lifetime`` yields the WARN from
        :func:`_deadline_mismatch_gap` instead of nothing.
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
    for field, leaves, risk, primary, substitutes, spend_risk in _RISK_ROWS:
        asserted = field in _ALWAYS_EVALUATED or any(
            leaf in set_fields for leaf in leaves
        )
        if not asserted:
            continue
        if spend_risk and not billed:
            continue
        if primary in declared:
            if primary is Capability.ON_INSTANCE_DEADLINE:
                mismatch = _deadline_mismatch_gap(cfg, provider, set_fields)
                if mismatch is not None:
                    gaps.append(mismatch)
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
                detail=_DETAIL[primary].format(shape=shape.value)
                + (
                    f"; bounded instead by {covering.value}"
                    f"{_substitute_bound(cfg, covering, provider)}"
                    if covering is not None
                    else ""
                ),
            )
        )
    # compute-seam S4: a provider that cannot price a launched instance cannot
    # have max_usd_per_hr enforced against it at all. Billed-gated like every
    # other spend row — an unbilled provider has no rate to verify.
    provider_cls = provider_class_for(provider)
    if billed and provider_cls is not None:
        gaps.extend(rate_source_declared(provider_cls))
    return gaps


def infer_shape(cfg: Config) -> WorkloadShape:
    """Infer the workload shape from cfg — always SERVER, deliberately.

    Nothing in kinoforge renders a launch-less InstanceSpec that *provisions*
    an instance:

    * the diffusers engine sets ``launch`` from ``server_cmd`` whenever that
      is non-empty; ``upscale_only`` only adds ``KINOFORGE_SKIP_WAN_LOAD=1``
      to the env, leaving the long-lived server process in place.
    * The launch-less renders in ``upscalers/*/_engine.py`` and
      ``interpolators/rife/_engine.py`` belong to pipeline STAGES, which the
      orchestrator constructs with ``instance=session.instance``
      (``core/orchestrator.py:2008-2022`` and ``:2031-2035``) — an
      already-provisioned instance. They never provision a pod.

    So guessing BATCH from ``upscale_only`` or an interpolate block reports a
    guardrail as ENFORCED on a cluster where it is provably inert — on
    ``skypilot-lambda-diffusers-flashvsr-upscale.yaml`` it silently passed
    ``idle_timeout`` even though skypilot autostop cannot fire against a
    never-terminating ``Task.run``. That is precisely the dishonesty this
    design exists to end, so the inference refuses to guess.

    :class:`WorkloadShape` and skypilot's BATCH-only ``IDLE_AUTOSTOP``
    declaration stay: the substrate claim is real, and BATCH arises from the
    authoritative ``spec.launch`` at launch time rather than from a cfg guess
    here.

    Args:
        cfg: The loaded Config.

    Returns:
        Always :attr:`WorkloadShape.SERVER`.
    """
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
        gaps = evaluate_capability_gaps(cfg, infer_shape(cfg))
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
        lines = [g.render(provider) for g in gaps]
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
