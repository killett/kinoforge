"""Cfg validation Check Registry — public API.

Two entry points:

  - validate_for_generate(cfg) runs STATIC + PREFLIGHT + NETWORK-subset.
    Auto-fixes STATIC failures with a single retry. Raises
    kinoforge.core.errors.ValidationError if any ERROR-severity check
    still fails after auto-fix.

  - validate_for_doctor(cfg) runs all categories, all checks. Never
    raises. Returns the full ValidationReport for the CLI to format.

Design spec: docs/superpowers/specs/2026-06-18-cfg-validation-check-registry-design.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from kinoforge.core.errors import ValidationError
from kinoforge.validation.protocol import (
    Check,
    CheckCategory,
    CheckResult,
    Severity,
)
from kinoforge.validation.registry import CheckRegistry, default_registry

__all__ = [
    "Check",
    "CheckCategory",
    "CheckResult",
    "CheckRegistry",
    "Severity",
    "ValidationReport",
    "validate_for_doctor",
    "validate_for_generate",
]

_log = logging.getLogger(__name__)


@dataclass
class ValidationReport:
    """Outcome of a validation pass.

    Attributes:
        cfg: The final cfg (post-auto-fix). May be the same object as
            the input cfg if no auto-fixes fired.
        results: Every CheckResult produced during this pass, in
            registry order.
        auto_fixes: Subset of ``results`` where ``auto_fix_applied`` is
            True. Convenience accessor for the CLI's INFO logging.
        errors: Subset of ``results`` where ``passed`` is False AND
            ``severity`` is ERROR.
        warnings: Subset of ``results`` where ``passed`` is False AND
            ``severity`` is WARN.
    """

    cfg: object
    results: list[CheckResult] = field(default_factory=list)
    auto_fixes: list[CheckResult] = field(default_factory=list)
    errors: list[CheckResult] = field(default_factory=list)
    warnings: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff no ERROR-severity checks failed."""
        return not self.errors

    def format(self) -> str:
        """Render the report as an operator-facing string.

        Used by ValidationError formatting and by `kinoforge doctor`.
        """
        lines: list[str] = []
        for r in self.errors:
            lines.append(f"  ✗ {r.name}")
            lines.append(f"    {r.message}")
            if r.fix_suggestion:
                lines.append(f"    fix: {r.fix_suggestion}")
        for r in self.warnings:
            lines.append(f"  ⚠ {r.name}")
            lines.append(f"    {r.message}")
            if r.fix_suggestion:
                lines.append(f"    suggested: {r.fix_suggestion}")
        return "\n".join(lines)


def _run_with_autofix(check: Check, cfg: object) -> tuple[CheckResult, object]:
    """Run a STATIC check; if it fails, attempt auto_fix exactly once.

    Returns:
        (result, cfg_after) — ``cfg_after`` is the original cfg unless
        a successful auto-fix replaced it.
    """
    result = check.run(cfg)
    if result.passed:
        return result, cfg
    new_cfg = check.auto_fix(cfg)
    if new_cfg is None:
        return result, cfg
    retry = check.run(new_cfg)
    if retry.passed:
        return (
            CheckResult(
                name=retry.name,
                passed=True,
                severity=retry.severity,
                message=retry.message,
                auto_fix_applied=True,
                fix_suggestion=result.fix_suggestion,
            ),
            new_cfg,
        )
    _log.warning(
        "auto_fix for %s returned a new cfg that still fails the check; "
        "operator-facing error reflects the original failure",
        check.name,
    )
    return result, cfg


def _categorise(
    results: list[CheckResult],
) -> tuple[list[CheckResult], list[CheckResult], list[CheckResult]]:
    auto_fixes = [r for r in results if r.auto_fix_applied]
    errors = [r for r in results if not r.passed and r.severity == Severity.ERROR]
    warnings = [r for r in results if not r.passed and r.severity == Severity.WARN]
    return auto_fixes, errors, warnings


def validate_for_generate(
    cfg: object, *, registry: CheckRegistry | None = None
) -> ValidationReport:
    """Validate cfg in the `kinoforge generate` context.

    Runs STATIC + PREFLIGHT + NETWORK categories. Auto-fixes are
    attempted for STATIC failures only. If any ERROR-severity result
    remains after auto-fix, raises ``ValidationError`` carrying the
    formatted report.

    Args:
        cfg: The kinoforge Config object.
        registry: Optional CheckRegistry. Defaults to the module-level
            registry populated by built-in + provider checks.

    Returns:
        ValidationReport on success.

    Raises:
        ValidationError: At least one ERROR-severity check failed and
            had no successful auto-fix.
    """
    reg = registry or default_registry()
    results: list[CheckResult] = []

    for check in reg.applicable(cfg, categories=frozenset({CheckCategory.STATIC})):
        result, cfg = _run_with_autofix(check, cfg)
        results.append(result)

    for check in reg.applicable(cfg, categories=frozenset({CheckCategory.PREFLIGHT})):
        results.append(check.run(cfg))

    for check in reg.applicable(cfg, categories=frozenset({CheckCategory.NETWORK})):
        results.append(check.run(cfg))

    auto_fixes, errors, warnings = _categorise(results)
    report = ValidationReport(
        cfg=cfg,
        results=results,
        auto_fixes=auto_fixes,
        errors=errors,
        warnings=warnings,
    )

    for af in auto_fixes:
        _log.info("auto-fixed: %s — %s", af.name, af.message)
    for w in warnings:
        _log.warning("%s: %s", w.name, w.message)

    if errors:
        raise ValidationError(f"cfg validation failed\n{report.format()}")
    return report


def validate_for_doctor(
    cfg: object, *, registry: CheckRegistry | None = None
) -> ValidationReport:
    """Validate cfg in the `kinoforge doctor` context.

    Runs every applicable check across every category. Does NOT raise
    on errors — returns the report so the CLI can format + decide the
    exit code.

    Args:
        cfg: The kinoforge Config object.
        registry: Optional CheckRegistry override.

    Returns:
        ValidationReport, including any errors and warnings.
    """
    reg = registry or default_registry()
    results: list[CheckResult] = []
    for check in reg.applicable(cfg):
        results.append(check.run(cfg))

    auto_fixes, errors, warnings = _categorise(results)
    return ValidationReport(
        cfg=cfg,
        results=results,
        auto_fixes=auto_fixes,
        errors=errors,
        warnings=warnings,
    )
