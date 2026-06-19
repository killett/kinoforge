"""Check Protocol + result types for the kinoforge cfg validation registry.

Establishes the vocabulary every check shares: category (STATIC /
NETWORK / PREFLIGHT), severity (ERROR / WARN), the CheckResult shape,
and the Check Protocol itself.

Design spec: docs/superpowers/specs/2026-06-18-cfg-validation-check-registry-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class CheckCategory(Enum):
    """When a check fires.

    STATIC    — internal cfg consistency; no I/O. Fast; runs on every
                cfg load.
    NETWORK   — HEAD/GET against an external resource (Docker registry,
                HF Hub, GitHub). Slow; runs on doctor (full) + generate
                pre-flight (subset).
    PREFLIGHT — external state check (ledger, provider capacity).
                Runs on doctor + generate pre-flight.
    """

    STATIC = "static"
    NETWORK = "network"
    PREFLIGHT = "preflight"


class Severity(Enum):
    """How a check's failure is surfaced.

    ERROR — rejects load (or fails generate pre-flight). The operator
            must fix the cfg before the next attempt.
    WARN  — logged + included in the report, but does NOT reject. Used
            for advisory checks (custom-node SHA archived, capacity
            hint, stale ledger rows).
    """

    ERROR = "error"
    WARN = "warn"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single Check.run() invocation.

    Attributes:
        name: The check's id (must match the originating Check.name).
        passed: True iff the cfg satisfies the check's invariant.
        severity: Carried forward even when passed=True for
            consistent log-line formatting.
        message: Operator-facing one-liner. Should include the cfg
            path/field that triggered the result.
        auto_fix_applied: True iff the surrounding pipeline applied
            an auto-fix derived from this check before the result was
            captured.
        fix_suggestion: Optional concrete fix hint (e.g.
            ``"set compute.lifecycle.heartbeat_interval_s: 30"``).
    """

    name: str
    passed: bool
    severity: Severity
    message: str
    auto_fix_applied: bool = False
    fix_suggestion: str | None = None


@runtime_checkable
class Check(Protocol):
    """Provider-agnostic substrate for a single cfg validation rule.

    Implementations live under ``kinoforge.validation.checks`` (built-ins)
    or in provider/engine modules (which self-register at import time).
    """

    name: str
    category: CheckCategory
    severity: Severity

    def applies_to(self, cfg: Any) -> bool:  # noqa: ANN401 — see class docstring
        """Cheap guard. Return True iff this check applies to the cfg.

        Must NOT perform I/O. The registry calls ``applies_to`` to
        skip ``run`` entirely on checks that don't apply, which is
        what keeps fast paths fast.

        ``cfg`` is typed ``Any`` so implementations can specialise to
        ``kinoforge.core.config.Config`` without violating Protocol
        parameter contravariance.
        """
        ...

    def run(self, cfg: Any) -> CheckResult:  # noqa: ANN401 — see class docstring
        """Execute the check.

        May do I/O if the category is NETWORK or PREFLIGHT. Must
        return a CheckResult; never raise on a validation failure
        (raise only on internal bugs).
        """
        ...

    def auto_fix(self, cfg: Any) -> Any | None:  # noqa: ANN401 — see class docstring
        """Return a NEW cfg with the issue auto-fixed, or None.

        ``None`` signals the check has no safe default. Honoured only
        for STATIC category checks (network/preflight failures cannot
        be silently auto-fixed without operator authorisation).
        """
        ...
