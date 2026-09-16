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


# ---------------------------------------------------------------------------
# U47: a check may register another check while applicable() is iterating
# ---------------------------------------------------------------------------


class _LazyRegisteringCheck:
    """A Check whose ``applies_to`` registers another check, as a real one does.

    This is not a contrived shape. ``provider_capabilities.applies_to`` calls
    into the provider registry, and importing that registers
    ``skypilot_cloud_pin_supported`` and ``runpod_capacity_hint`` as a side
    effect — so the registry genuinely grows mid-pass the first time validation
    runs before the providers have been imported.
    """

    name: str = "lazy_registering"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    def __init__(self, registry: CheckRegistry, late: Check) -> None:
        self._registry = registry
        self._late = late
        self._fired = False

    def applies_to(self, cfg: object) -> bool:
        """Register the late check exactly once, then apply."""
        if not self._fired:
            self._fired = True
            self._registry.register(self._late)
        return True

    def run(self, cfg: object) -> CheckResult:  # pragma: no cover — not called
        return CheckResult(
            name=self.name, passed=True, severity=self.severity, message="ok"
        )

    def auto_fix(self, cfg: object) -> object | None:  # pragma: no cover
        return None


def test_applicable_survives_a_check_that_registers_during_applies_to() -> None:
    """``applicable`` must not blow up when the registry grows mid-pass.

    Bug caught, and it was live on HEAD: ``applicable`` iterated
    ``self._checks.values()`` directly, so the lazy provider-registry import
    inside ``provider_capabilities.applies_to`` raised
    ``RuntimeError: dictionary changed size during iteration`` out of
    ``validate_for_generate`` — before a single CheckResult existed. The
    operator sees a Python-internals traceback where a validation report
    belongs, and no check has actually run.
    """
    reg = CheckRegistry()
    late = _FakeCheck(name="late_arrival")
    reg.register(_LazyRegisteringCheck(reg, late))

    applicable = reg.applicable(object())

    assert [c.name for c in applicable] == ["lazy_registering", "late_arrival"]


def test_a_check_registered_mid_pass_is_still_evaluated() -> None:
    """The newcomer must be evaluated in the SAME pass, not silently dropped.

    Bug caught: the obvious fix — snapshot ``list(self._checks.values())`` and
    iterate that — stops the crash and quietly skips every check registered
    during the pass. For the two real ones this would mean
    ``skypilot_cloud_pin_supported`` and ``runpod_capacity_hint`` never running
    on the first validation, which for ``validate_for_load`` is the only
    validation there is. Trading a loud crash for silent under-validation is
    the worse bug, because nothing reports it.

    Asserted via ``applies_to`` being consulted on the newcomer, which a
    snapshot-based fix never does.
    """
    reg = CheckRegistry()
    late = _FakeCheck(name="late_arrival", applies=False)
    reg.register(_LazyRegisteringCheck(reg, late))

    applicable = reg.applicable(object())

    assert [c.name for c in applicable] == ["lazy_registering"], (
        "the late check declines to apply, so it must be ABSENT — and the only "
        "way to know that is to have asked it"
    )
    assert "late_arrival" in reg.all_names()
