"""UnsupportedFieldCheck + ForeignNamespaceCheck — STATIC, no auto-fix.

Turns each provider's ``consumes()`` declaration into an operator-facing
finding. A portable field the selected provider declares UNSUPPORTED, set to a
value the operator actually wrote, is a misconfiguration — the alternative is
finding out from the invoice (design doc §4, verification finding F5).

Severity is by RISK COVERAGE, mirroring
:mod:`kinoforge.validation.checks.capabilities`:

* **ERROR** when nothing else in the cfg bounds the same risk.
  ``accelerator_count`` is the archetype — every provider pins one accelerator
  and no other field can deliver a second. S2 added the second such row,
  ``region``: RunPod's create mutation never sends a ``dataCenterId`` and
  Modal's ``@app.function(region=)`` is never passed, so a pinned region
  reaches nothing and NOTHING else in the cfg constrains where the run lands
  — data residency is not something a timeout or a rate cap can substitute
  for. No shipped config sets ``region`` on either provider, so this refuses
  nobody today; it refuses the operator who assumes a pin they wrote is being
  honoured. ``test_only_substitute_free_rows_are_errors`` pins the exact set,
  so a new row shipped without a substitute is caught rather than discovered
  by the operator it refuses.
* **WARN naming the substitute and the bound it actually enforces**
  otherwise. ``disk_gb`` names the provider's hardcoded value, so
  "you asked 150, sky gives 60" is visible at doctor time; skypilot's
  ``max_usd_per_hr`` (finding F4) names the instance-side deadline watchdog.

A uniform ERROR was rejected by operator ruling (2026-08-27): Task 5's
declarations surfaced two silent-ignores that predate this plan — ``disk_gb``
is UNSUPPORTED on all four providers and skypilot's ``max_usd_per_hr`` is F4
itself — and 15 shipped configs set one of them. Refusing them would punish
the operator for a gap in the providers.

Scope is ``compute.placement`` plus the launch-describing keys of the
``compute`` block itself — ``image``, ``mode``, ``tags``, ``heartbeat_mode``,
``warm_reuse_auto_attach`` (compute-seam S2). That widening is the deliberate
follow-up this module's earlier note promised, and it is what makes
``compute.mode`` reportable at all: before S2 the check stopped at
``compute.placement.*``, so 46 configs could write a key nothing read without
a single finding.

Each finding names its OWN dotted path (:data:`_COMPUTE_PATHS`) rather than a
hardcoded prefix. ``compute.placement.mode`` would send an operator to a key
that not only does not exist but is actively refused by
``PlacementConfig(extra="forbid")``.

The remaining ``InstanceSpec`` rows (``ports``, ``volume_gb``, ``env``, the
provision scripts) are still out of scope: they are not written under
``compute`` at all, so there is no operator-facing cfg path to report them
against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kinoforge.core.capabilities import consumes_for, provider_registered
from kinoforge.core.config import ComputeConfig, Config, PlacementConfig
from kinoforge.core.interfaces import FieldSupport
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

__all__ = [
    "ForeignNamespaceCheck",
    "Gap",
    "UnsupportedFieldCheck",
    "evaluate_field_gaps",
    "foreign_namespaces",
]

#: Compute-level fields that live directly under ``compute`` rather than under
#: ``compute.placement``. A finding that names the wrong path sends the
#: operator to a key that does not exist — and ``compute.placement.mode``
#: would be doubly misleading, because writing it there is itself refused.
_COMPUTE_PATHS: dict[str, str] = {
    "image": "compute.image",
    "mode": "compute.mode",
    "tags": "compute.tags",
    "heartbeat_mode": "compute.heartbeat_mode",
    "warm_reuse_auto_attach": "compute.warm_reuse_auto_attach",
}


def _dotted_path(field: str) -> str:
    """Return the cfg path an operator would have written for *field*.

    Args:
        field: A declared field name.

    Returns:
        The dotted path, defaulting to the placement block.
    """
    return _COMPUTE_PATHS.get(field, f"compute.placement.{field}")


#: Values a placement field carries when the operator wrote nothing.
#:
#: A pydantic instance, NOT ``model_fields_set``: the grid executor round-trips
#: each child cfg through ``model_dump()``, which repopulates
#: ``model_fields_set`` for every field — so a set-membership test reports
#: findings on values the operator never wrote. Comparing the VALUE cannot lie
#: that way. ``PlacementConfig`` rather than the ``Placement`` dataclass only
#: because ``accelerators`` is a list here and a tuple there;
#: ``test_placement_yaml_defaults_match_the_portable_dataclass`` pins them
#: equal so the choice cannot drift into meaning something.
_DEFAULTS = PlacementConfig()

#: Defaults for the compute-level rows, taken from ``ComputeConfig``'s field
#: declarations rather than an instance: ``provider`` and ``image`` are
#: required, so ``ComputeConfig()`` cannot be constructed the way
#: ``PlacementConfig()`` can. ``image`` is absent here on purpose — it has no
#: default, and :func:`_written_fields` treats it as always-written.
_COMPUTE_DEFAULTS: dict[str, Any] = {
    name: field.get_default()
    for name, field in ComputeConfig.model_fields.items()
    if name in {"mode", "tags", "heartbeat_mode", "warm_reuse_auto_attach"}
}

#: RunPod's create-pod mutation pins this literal (``providers/runpod``,
#: ``_create_pod``), with a TODO admitting ``placement.disk_gb`` should be
#: threaded through instead.
_RUNPOD_CONTAINER_DISK_GB = 250

#: SkyPilot's ``resources.setdefault("disk_size", 60 if is_gpu else 30)``.
#: The CPU arm is reached when ``_select_accelerator`` returns None (the S4
#: replacement for what used to be a short-circuit to the synthetic
#: ``sky-cpu-auto`` offer, which it does exactly when ``min_vram_gb == 0``.
_SKY_DISK_GB_GPU = 60
_SKY_DISK_GB_CPU = 30

#: Modal's ``startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800``,
#: which becomes ``@app.function(timeout=...)``.
_MODAL_TIMEOUT_FALLBACK_S = 1800

#: portable field -> what goes wrong when the provider ignores it.
_RISK: dict[str, str] = {
    "accelerators": "the run lands on an accelerator you did not ask for",
    "accelerator_count": "the run gets one accelerator, not the number asked for",
    "min_vram_gb": "the run lands on an accelerator with too little VRAM",
    "min_cuda": "the run lands on a host whose CUDA is below the floor",
    "disk_gb": "the run runs out of disk mid-download",
    "region": "the run lands in a region you did not choose",
    "spot": "the run pays the on-demand rate",
    "max_usd_per_hr": "the run books an instance above the rate ceiling",
    "image": "the run starts a container you did not choose, or none at all",
    "mode": "the run takes the pod branch when you asked for serverless",
    "tags": "the label never reaches the ledger, so runs cannot be told apart",
    "heartbeat_mode": "liveness is inferred from the controller, not the instance",
    "warm_reuse_auto_attach": "the warm-reuse scan does not do what the key says",
}


@dataclass(frozen=True)
class Gap:
    """One portable field the selected provider cannot honour as written.

    Attributes:
        field: Dotted cfg path the operator wrote.
        value: The value they wrote, rendered into the message so the
            severity line is actionable without opening the YAML.
        risk: One-line description of what the silent ignore causes.
        substitute: Phrase naming what bounds the same risk instead,
            INCLUDING the bound it actually enforces, or None when nothing
            does. None is what makes the gap an ERROR.
        severity: ERROR iff ``substitute`` is None.
        headline: Overrides the default "``<provider>`` does not read
            ``<field>``" phrasing. Used only by the undeclared-provider gap,
            which is about the declaration rather than about one field.
    """

    field: str
    value: Any
    risk: str
    substitute: str | None
    severity: Severity
    headline: str | None = None

    def render(self, provider: str) -> str:
        """Return the operator-facing one-liner for this gap.

        The severity is included per line because a mixed ERROR+WARN result
        aggregates into ONE CheckResult carrying only the worst severity —
        without it the operator cannot tell which line refused the load from
        the lines that are merely advisory.

        Args:
            provider: The provider kind the gap was evaluated against.

        Returns:
            ``"[ERROR] <field>=<value>: <headline> (<risk>) — <detail>"``.
        """
        head = self.headline or f"{provider} does not read it"
        detail = (
            f"bounded instead by {self.substitute}"
            if self.substitute is not None
            else "nothing else in this cfg bounds that risk"
        )
        return (
            f"[{self.severity.name}] {self.field}={self.value!r}: "
            f"{head} ({self.risk}) — {detail}"
        )


def _placement(cfg: Config) -> PlacementConfig:
    """Return the cfg's placement block.

    Args:
        cfg: A Config whose ``compute`` block is set.

    Returns:
        The ``compute.placement`` model (its own defaults when the YAML
        omitted the block entirely).
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    return cfg.compute.placement


def _runpod_disk(cfg: Config) -> str:
    """Name RunPod's hardcoded container disk and how it compares to the ask.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming ``containerDiskInGb`` and whether it is above or
        below what the operator asked for.
    """
    asked = _placement(cfg).disk_gb
    relation = "above" if _RUNPOD_CONTAINER_DISK_GB >= asked else "below"
    return (
        f"the create-pod mutation's hardcoded "
        f"containerDiskInGb={_RUNPOD_CONTAINER_DISK_GB} GB, {relation} "
        f"the {asked} GB you asked for"
    )


def _skypilot_disk(cfg: Config) -> str:
    """Name the ``disk_size`` SkyPilot really pins for THIS cfg.

    Which arm of ``setdefault("disk_size", 60 if is_gpu else 30)`` applies is
    knowable at load: ``_select_accelerator`` returns None (pre-S4: the
    synthetic ``sky-cpu-auto`` offer)
    offer (no ``gpu_type``, hence the 30 GB arm) exactly when
    ``min_vram_gb == 0``. Printing both numbers would leave the operator to
    guess which one caps their download.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming the single pinned value and how it compares to the
        ask.
    """
    placement = _placement(cfg)
    is_gpu = placement.min_vram_gb > 0
    pinned = _SKY_DISK_GB_GPU if is_gpu else _SKY_DISK_GB_CPU
    shape = "GPU" if is_gpu else "CPU"
    relation = "above" if pinned >= placement.disk_gb else "below"
    return (
        f"sky's own resources.setdefault(disk_size) — {pinned} GB for this "
        f"{shape} cfg (min_vram_gb={placement.min_vram_gb}), {relation} "
        f"the {placement.disk_gb} GB you asked for"
    )


def _modal_disk(cfg: Config) -> str:
    """Name the Modal Volume that absorbs the downloads container disk would.

    Args:
        cfg: The loaded Config (unused; the mount is a request default, not a
            cfg value).

    Returns:
        A phrase naming the volume. Deliberately quotes no number: Modal
        exposes no disk knob at all, and a bare omission is more honest than
        a size this code cannot know.
    """
    del cfg
    return (
        "the Modal Volume mounted at the request's volume_mount, which the "
        "provider also exports as HF_HOME — model weights land on the "
        "network volume, not on container disk, and it is not sized from "
        "the spec"
    )


def _runpod_spot(cfg: Config) -> str:
    """Name the rate ceiling RunPod does honour, since its pods are on-demand.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming ``max_usd_per_hr`` and its value.
    """
    return (
        f"the rate ceiling max_usd_per_hr={_placement(cfg).max_usd_per_hr}/hr, "
        f"which runpod does honour (filter_offers excludes pod offers above "
        f"it) — the spot discount is unavailable, but the rate is still capped"
    )


def _skypilot_rate_cap(cfg: Config) -> str:
    """Name the instance-side watchdog and the arm of it that is really live.

    ``watchdog.compute_deadline`` takes the earlier of ``max_lifetime`` and a
    budget bound, but it guards the budget arm with
    ``if budget_usd > 0 and rate_usd_per_hr > 0``. At the common ``budget: 0``
    that arm never fires, so the run is bounded in TIME only and its dollar
    cost is unbounded — saying "bounded by budget/rate" there would name a cap
    that does not exist. Even with a budget set, the rate half is the booked
    offer's, unknowable at load, so the phrase says so rather than implying
    the dollar cap is unconditional.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming the watchdog and its live bound(s).
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    lifecycle = cfg.compute.lifecycle
    if lifecycle is None:
        return (
            "the instance-side deadline watchdog "
            "(providers/skypilot/watchdog.py), whose bound this cfg does not "
            "state — it writes no lifecycle block"
        )
    lifetime = f"max_lifetime={lifecycle.max_lifetime}s"
    if lifecycle.budget > 0:
        return (
            f"the instance-side deadline watchdog "
            f"(providers/skypilot/watchdog.py), which kills the instance at "
            f"whichever comes first: budget={lifecycle.budget} USD divided by "
            f"the booked rate (that arm needs a non-zero rate on the booked "
            f"offer, which is not knowable here), or {lifetime}"
        )
    return (
        f"the instance-side deadline watchdog "
        f"(providers/skypilot/watchdog.py) at {lifetime} — its budget arm is "
        f"inactive at lifecycle.budget 0, so nothing bounds this run in "
        f"dollars, only in time"
    )


def _modal_timeout_s(cfg: Config) -> int:
    """Return the seconds Modal really receives as its function timeout.

    ``ModalProvider.create_instance`` sends
    ``startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800``, and
    ``_app.py`` passes that straight to ``@app.function(timeout=...)``. The
    ``or 1800`` fallback matters: echoing a cfg's ``boot_timeout: 0`` would
    tell the operator the container dies immediately.

    Args:
        cfg: The loaded Config.

    Returns:
        The timeout in seconds.
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    lifecycle = cfg.compute.lifecycle
    boot = int(lifecycle.boot_timeout) if lifecycle is not None else 0
    return boot or _MODAL_TIMEOUT_FALLBACK_S


def _modal_spot(cfg: Config) -> str:
    """Name the same function timeout that covers Modal's missing rate cap.

    Modal has no spot pool, so ``spot: true`` never arrives — but the harm is
    a discount the run does not get, not an unbounded run, and the bound that
    covers ``max_usd_per_hr`` covers this identically: duration x the booked
    rate. Two rows sharing one substitute cannot disagree on severity, which
    is why this is a WARN rather than the ERROR it shipped as first.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming the timeout and being explicit that what is lost is
        the discount, not the ceiling.
    """
    timeout = _modal_timeout_s(cfg)
    return (
        f"Modal's @app.function(timeout={timeout}s), set from "
        f"lifecycle.boot_timeout — modal has no spot pool at all, so what is "
        f"lost is the discount, not a bound: the run is still capped at the "
        f"booked on-demand rate x {timeout}s, though the rate itself is "
        f"uncapped"
    )


def _modal_rate_cap(cfg: Config) -> str:
    """Name Modal's function timeout, which bounds total spend but not rate.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming ``@app.function(timeout=...)`` and the number of
        seconds Modal really receives, including the ``or 1800`` fallback the
        provider applies to a zero.
    """
    timeout = _modal_timeout_s(cfg)
    return (
        f"Modal's @app.function(timeout={timeout}s), set from "
        f"lifecycle.boot_timeout — it bounds total spend at the booked rate "
        f"x {timeout}s rather than capping the rate itself"
    )


def _local_inert(cfg: Config) -> str:
    """Explain why every placement field is inert — and harmless — on local.

    The local provider starts no container, allocates nothing and reports
    ``cost_rate_usd_per_hr`` as the literal 0.0. Each risk in :data:`_RISK`
    is a LAUNCH risk, and local never launches, so the harm the row describes
    cannot occur. That is real coverage rather than an excuse, and it is why
    local rows warn instead of refusing a dry run.

    Args:
        cfg: The loaded Config (unused).

    Returns:
        The coverage phrase.
    """
    del cfg
    return (
        "local starting nothing at all: it allocates no hardware, runs no "
        "container and reports cost_rate_usd_per_hr=0.0, so no launch-time "
        "risk this field guards against can occur"
    )


def _orchestrator_owns_warm_reuse(cfg: Config) -> str:
    """Name the code that really honours ``warm_reuse_auto_attach``.

    No provider reads the flag and none should: the pre-launch warm scan runs
    in the CLI and decides whether ``create_instance`` is called at all. So
    the risk "the key does nothing" is fully covered — by the orchestrator,
    not by the provider — and the row is a WARN rather than a refusal.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming the orchestrator-side owner and its real effect.
    """
    flag = cfg.compute.warm_reuse_auto_attach if cfg.compute is not None else True
    return (
        f"the CLI's pre-launch warm scan (cli/_commands.py), which honours "
        f"warm_reuse_auto_attach={flag} BEFORE any provider call — the flag "
        f"decides whether create_instance runs at all, so no provider ever "
        f"needs to read it"
    )


def _controller_clock_heartbeat(cfg: Config) -> str:
    """Name what stands in for a wire-level heartbeat on this provider.

    Args:
        cfg: The loaded Config.

    Returns:
        A phrase naming the orchestrator-clock fallback and the lifecycle
        bound that actually kills a run when liveness cannot be observed.
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    lifecycle = cfg.compute.lifecycle
    bound = (
        f"max_lifetime={lifecycle.max_lifetime}s"
        if lifecycle is not None
        else "no lifecycle block, so no stated bound"
    )
    return (
        f"the orchestrator-clock heartbeat the reaper falls back to, which "
        f"proves the CONTROLLER is alive rather than the instance — the "
        f"instance itself is bounded only by {bound}"
    )


def _mode_is_the_only_shape(cfg: Config) -> str:
    """Name the single instance shape a provider with no second arm offers.

    Args:
        cfg: The loaded Config (unused; the shape is a provider property).

    Returns:
        A phrase saying which shape the run gets regardless of the key.
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    provider = cfg.compute.provider
    shape = "serverless" if provider == "modal" else "a booked instance"
    return (
        f"{provider} offering exactly one instance shape ({shape}); the run "
        f"is not silently placed on the OTHER branch, because there is none"
    )


#: (provider, portable field) -> phrase naming the substitute AND its bound.
#:
#: A row absent here is an ERROR by construction: "nothing bounds this risk"
#: is the default claim, and it is the safe direction — adding a substitute
#: requires pointing at the code that enforces it, while omitting one only
#: costs an over-strict refusal that a reader can trace.
#:
#: Two rows may not share a substitute and disagree on severity. That was the
#: 2026-08-27 review finding against modal: ``spot`` was refused while
#: ``max_usd_per_hr`` warned, though ``@app.function(timeout=)`` bounds both
#: identically — and ``spot``'s harm (a missed discount) is strictly smaller.
_SUBSTITUTE: dict[tuple[str, str], Callable[[Config], str]] = {
    ("runpod", "disk_gb"): _runpod_disk,
    ("runpod", "spot"): _runpod_spot,
    ("skypilot", "disk_gb"): _skypilot_disk,
    ("skypilot", "max_usd_per_hr"): _skypilot_rate_cap,
    ("modal", "disk_gb"): _modal_disk,
    ("modal", "spot"): _modal_spot,
    ("modal", "max_usd_per_hr"): _modal_rate_cap,
    # compute-level rows (S2). `warm_reuse_auto_attach` is UNSUPPORTED on all
    # four providers by design, so every one of them needs the orchestrator
    # substitute or a flag that works would start refusing loads.
    ("runpod", "warm_reuse_auto_attach"): _orchestrator_owns_warm_reuse,
    ("skypilot", "warm_reuse_auto_attach"): _orchestrator_owns_warm_reuse,
    ("modal", "warm_reuse_auto_attach"): _orchestrator_owns_warm_reuse,
    ("skypilot", "heartbeat_mode"): _controller_clock_heartbeat,
    ("modal", "heartbeat_mode"): _controller_clock_heartbeat,
    ("skypilot", "mode"): _mode_is_the_only_shape,
    ("modal", "mode"): _mode_is_the_only_shape,
}

#: provider -> substitute applied to EVERY unsupported field of that provider.
#:
#: Only ``local`` qualifies, and only because it is unbilled and launches
#: nothing (see :func:`_local_inert`). A billed provider must never be added
#: here: it would silently downgrade every future ERROR row to a WARN.
_PROVIDER_FALLBACK: dict[str, Callable[[Config], str]] = {"local": _local_inert}


def _written_fields(cfg: Config) -> list[tuple[str, Any]]:
    """Return the fields whose value differs from the default.

    Covers the placement block and the launch-describing compute-level keys
    (:data:`_COMPUTE_PATHS`). ``image`` has no default — every config states
    it — so it counts as written whenever the provider does not consume it,
    which is exactly the ``local`` case worth reporting.

    Args:
        cfg: A Config whose ``compute`` block is set.

    Returns:
        ``(field name, value)`` pairs in declaration order — placement first,
        then compute-level — so the rendered message is deterministic.
    """
    assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
    placement = _placement(cfg)
    written: list[tuple[str, Any]] = []
    for name in PlacementConfig.model_fields:
        value = getattr(placement, name)
        if value != getattr(_DEFAULTS, name):
            written.append((name, value))
    for name in _COMPUTE_PATHS:
        value = getattr(cfg.compute, name)
        if name == "image" or value != _COMPUTE_DEFAULTS.get(name):
            written.append((name, value))
    return written


def evaluate_field_gaps(cfg: Config) -> list[Gap]:
    """Return every portable field ``cfg`` writes that its provider ignores.

    Args:
        cfg: A loaded Config.

    Returns:
        Gaps in ``PlacementConfig`` declaration order; empty when the
        provider consumes everything the cfg wrote, and empty for a cfg with
        no compute block or an unregistered provider (there is no declaration
        to compare against, and ``registry.get_provider`` already refuses an
        unknown name at launch with the accurate message). A REGISTERED
        provider whose ``consumes()`` is empty yields a single gap refusing
        the load, matching the ``capabilities()`` precedent that an undeclared
        provider claims nothing.
    """
    if cfg.compute is None:
        return []
    provider = cfg.compute.provider
    if not provider_registered(provider):
        return []
    declared = consumes_for(provider)
    if not declared:
        return [
            Gap(
                field="compute.provider",
                value=provider,
                risk="every portable field is silently discarded",
                substitute=None,
                severity=Severity.ERROR,
                headline=(
                    f"{provider} declares no field support — its consumes() "
                    f"mapping is empty"
                ),
            )
        ]

    gaps: list[Gap] = []
    for name, value in _written_fields(cfg):
        # A field missing from a NON-empty declaration is undeclared, which
        # the parity test forbids; treat it as unsupported rather than as
        # consumed, so a half-written declaration fails loudly.
        if declared.get(name, FieldSupport.UNSUPPORTED) is FieldSupport.CONSUMED:
            continue
        phrase = _SUBSTITUTE.get((provider, name)) or _PROVIDER_FALLBACK.get(provider)
        substitute = phrase(cfg) if phrase is not None else None
        gaps.append(
            Gap(
                field=_dotted_path(name),
                value=value,
                risk=_RISK.get(name, "the value is silently discarded"),
                substitute=substitute,
                severity=Severity.WARN if substitute else Severity.ERROR,
            )
        )
    return gaps


def foreign_namespaces(cfg: Config) -> list[tuple[str, list[str]]]:
    """Return ``backend_options`` namespaces that are not the running provider.

    Args:
        cfg: A loaded Config.

    Returns:
        ``(provider kind, sorted option names)`` for each namespace owned by
        a registered provider other than the selected one, in cfg order. An
        unregistered namespace never appears: ``ComputeConfig`` already
        refuses one at parse time, naming it.
    """
    if cfg.compute is None:
        return []
    selected = cfg.compute.provider
    return [
        (name, sorted(options))
        for name, options in cfg.compute.backend_options.items()
        if name != selected and provider_registered(name)
    ]


class UnsupportedFieldCheck:
    """STATIC — report portable fields the selected provider cannot honour."""

    name: str = "unsupported_fields"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    def applies_to(self, cfg: Config) -> bool:
        """Apply to any cfg with a compute block naming a registered provider.

        An unregistered provider is skipped rather than reported as declaring
        nothing: there is no declaration to compare against, and a refusal
        here would preempt ``registry.get_provider``'s accurate
        unknown-provider message with a misleading one.

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
            One CheckResult; ``severity`` is ERROR iff any gap is ERROR, so
            the operator sees every ignored field at once instead of fixing
            them one load at a time.
        """
        gaps = evaluate_field_gaps(cfg)
        provider = cfg.compute.provider if cfg.compute is not None else "?"
        if not gaps:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=f"{provider} reads every portable field this cfg sets",
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
            message="\n  ".join([f"{provider} field-support gaps:", *lines]),
            fix_suggestion=(
                "drop the field, or select a provider that declares it "
                "CONSUMED (each provider's consumes() is the matrix)"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """Never auto-fix — silently rewriting the operator's value is the bug.

        Args:
            cfg: The Config that failed the check.

        Returns:
            Always ``None``.
        """
        del cfg
        return None


class ForeignNamespaceCheck:
    """STATIC — report ``backend_options`` blocks for another provider."""

    name: str = "foreign_backend_namespace"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    def applies_to(self, cfg: Config) -> bool:
        """Apply to any cfg with a compute block.

        Deliberately not gated on ``backend_options`` being non-empty: the
        pass line is what tells a doctor reader that the namespaces WERE
        checked.

        Args:
            cfg: The candidate Config.

        Returns:
            True iff ``cfg.compute`` is set.
        """
        return cfg.compute is not None

    def run(self, cfg: Config) -> CheckResult:
        """Report every namespace owned by a provider that is not running.

        Always WARN: the block is validated against its owner's Options model
        at parse time, so it is well-formed — it is simply never read, which
        is worth saying but not worth refusing a run over (an operator who
        keeps both namespaces while switching providers is doing something
        reasonable).

        Args:
            cfg: The Config to evaluate.

        Returns:
            One CheckResult naming every foreign namespace and its keys.
        """
        selected = cfg.compute.provider if cfg.compute is not None else "?"
        foreign = foreign_namespaces(cfg)
        if not foreign:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=(
                    f"every compute.backend_options namespace belongs to the "
                    f"selected provider ({selected})"
                ),
            )
        lines = [
            f"[WARN] compute.backend_options.{name}: "
            f"{', '.join(options) or '(empty)'} — {name} is not the selected "
            f"provider ({selected}), so nothing reads this block"
            for name, options in foreign
        ]
        return CheckResult(
            name=self.name,
            passed=False,
            severity=Severity.WARN,
            message="\n  ".join(
                [f"{selected} is running, but foreign namespaces are set:", *lines]
            ),
            fix_suggestion=(
                "delete the block, or set compute.provider to the namespace "
                "you meant to configure"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """Never auto-fix — the block may be there for a deliberate switch.

        Args:
            cfg: The Config that failed the check.

        Returns:
            Always ``None``.
        """
        del cfg
        return None


register(UnsupportedFieldCheck())
register(ForeignNamespaceCheck())
