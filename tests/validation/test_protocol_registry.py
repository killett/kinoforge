"""Protocol + Registry unit tests for kinoforge.validation."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.validation.protocol import (
    Check,
    CheckCategory,
    CheckResult,
    Severity,
)
from kinoforge.validation.registry import CheckRegistry


class _FakeCheck:
    """Minimal Check satisfier used by registry tests."""

    def __init__(
        self,
        *,
        name: str = "fake",
        category: CheckCategory = CheckCategory.STATIC,
        applies: bool = True,
    ) -> None:
        self.name = name
        self.category = category
        self.severity = Severity.ERROR
        self._applies = applies

    def applies_to(self, cfg: object) -> bool:
        return self._applies

    def run(self, cfg: object) -> CheckResult:
        return CheckResult(
            name=self.name, passed=True, severity=self.severity, message="ok"
        )

    def auto_fix(self, cfg: object) -> object | None:
        return None


def test_check_result_is_frozen_dataclass() -> None:
    r = CheckResult(name="x", passed=True, severity=Severity.WARN, message="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.passed = False  # type: ignore[misc]


def test_check_is_runtime_checkable_protocol() -> None:
    fake = _FakeCheck()
    assert isinstance(fake, Check)


def test_registry_rejects_duplicate_names() -> None:
    reg = CheckRegistry()
    reg.register(_FakeCheck(name="a"))
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(_FakeCheck(name="a"))


def test_registry_applicable_filters_by_category() -> None:
    reg = CheckRegistry()
    reg.register(_FakeCheck(name="s", category=CheckCategory.STATIC))
    reg.register(_FakeCheck(name="n", category=CheckCategory.NETWORK))
    got = reg.applicable(cfg=None, categories=frozenset({CheckCategory.NETWORK}))
    assert [c.name for c in got] == ["n"]


def test_registry_applicable_filters_by_applies_to() -> None:
    reg = CheckRegistry()
    reg.register(_FakeCheck(name="match", applies=True))
    reg.register(_FakeCheck(name="skip", applies=False))
    got = reg.applicable(cfg=None)
    assert [c.name for c in got] == ["match"]
