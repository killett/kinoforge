"""Lockdown tests for ProvisionFailed and ProvisionTimeout."""

from __future__ import annotations

from kinoforge.core.errors import (
    KinoforgeError,
    ProvisionFailed,
    ProvisionTimeout,
)


def test_provision_failed_is_kinoforge_error() -> None:
    """ProvisionFailed must subclass KinoforgeError so orchestrator catch sites work."""
    exc = ProvisionFailed("pod 'abc' entered terminal status 'terminated' before ready")
    assert isinstance(exc, KinoforgeError)
    assert str(exc) == "pod 'abc' entered terminal status 'terminated' before ready"


def test_provision_timeout_is_kinoforge_error() -> None:
    """ProvisionTimeout must subclass KinoforgeError for symmetric catch."""
    exc = ProvisionTimeout("engine ready check timed out after 900s for pod 'abc'")
    assert isinstance(exc, KinoforgeError)
    assert str(exc) == "engine ready check timed out after 900s for pod 'abc'"


def test_provision_failed_and_timeout_are_distinct_classes() -> None:
    """Distinct classes so callers can branch on root cause (boot crash vs. slow)."""
    assert ProvisionFailed is not ProvisionTimeout  # type: ignore[comparison-overlap]
    assert not issubclass(ProvisionFailed, ProvisionTimeout)
    assert not issubclass(ProvisionTimeout, ProvisionFailed)
