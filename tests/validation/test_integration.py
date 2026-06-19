"""End-to-end tests for validate_for_generate + validate_for_doctor.

Per-check tests live under tests/validation/checks/. These tests
exercise the orchestration layer: how multiple checks compose, how
auto-fix retries work, when errors raise vs return.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.validation import (
    ValidationReport,
    validate_for_doctor,
    validate_for_generate,
)
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import CheckRegistry


class _PassingCheck:
    def __init__(self, name: str, category: CheckCategory) -> None:
        self.name = name
        self.category = category
        self.severity = Severity.ERROR

    def applies_to(self, cfg: object) -> bool:
        return True

    def run(self, cfg: object) -> CheckResult:
        return CheckResult(
            name=self.name, passed=True, severity=self.severity, message="ok"
        )

    def auto_fix(self, cfg: object) -> object | None:
        return None


class _FailingCheck:
    def __init__(self, name: str, severity: Severity) -> None:
        self.name = name
        self.category = CheckCategory.STATIC
        self.severity = severity

    def applies_to(self, cfg: object) -> bool:
        return True

    def run(self, cfg: object) -> CheckResult:
        return CheckResult(
            name=self.name,
            passed=False,
            severity=self.severity,
            message="boom",
            fix_suggestion="set X: Y",
        )

    def auto_fix(self, cfg: object) -> object | None:
        return None


class _AutoFixCheck:
    """Returns a NEW cfg sentinel on auto_fix; passes the second time."""

    def __init__(self) -> None:
        self.name = "autofix"
        self.category = CheckCategory.STATIC
        self.severity = Severity.ERROR
        self._fixed_cfg = object()

    def applies_to(self, cfg: object) -> bool:
        return True

    def run(self, cfg: object) -> CheckResult:
        if cfg is self._fixed_cfg:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=self.severity,
                message="ok",
                auto_fix_applied=True,
            )
        return CheckResult(
            name=self.name, passed=False, severity=self.severity, message="bad"
        )

    def auto_fix(self, cfg: object) -> object | None:
        return self._fixed_cfg


def test_validate_for_generate_returns_ok_report_when_all_checks_pass() -> None:
    reg = CheckRegistry()
    reg.register(_PassingCheck("a", CheckCategory.STATIC))
    reg.register(_PassingCheck("b", CheckCategory.NETWORK))
    report = validate_for_generate(cfg=object(), registry=reg)
    assert report.ok is True
    assert report.errors == []
    assert len(report.results) == 2


def test_validate_for_generate_raises_on_unfixable_error() -> None:
    reg = CheckRegistry()
    reg.register(_FailingCheck("boom", Severity.ERROR))
    with pytest.raises(ValidationError, match="boom"):
        validate_for_generate(cfg=object(), registry=reg)


def test_validate_for_generate_auto_fixes_static_error() -> None:
    reg = CheckRegistry()
    afc = _AutoFixCheck()
    reg.register(afc)
    report = validate_for_generate(cfg=object(), registry=reg)
    assert report.ok is True
    assert len(report.auto_fixes) == 1
    assert report.auto_fixes[0].name == "autofix"
    assert report.auto_fixes[0].auto_fix_applied is True
    assert report.cfg is afc._fixed_cfg


def test_validate_for_doctor_never_raises_returns_full_report() -> None:
    reg = CheckRegistry()
    reg.register(_FailingCheck("err", Severity.ERROR))
    reg.register(_FailingCheck("warn", Severity.WARN))
    report = validate_for_doctor(cfg=object(), registry=reg)
    assert isinstance(report, ValidationReport)
    assert report.ok is False
    assert len(report.errors) == 1
    assert len(report.warnings) == 1
