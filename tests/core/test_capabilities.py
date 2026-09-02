"""Capability vocabulary + by-name lookup (Brief 2, Task 0)."""

from __future__ import annotations

import subprocess
import sys

from kinoforge.core.capabilities import (
    Capability,
    WorkloadShape,
    capabilities_for,
    provider_billed,
)
from kinoforge.core.interfaces import ComputeProvider


def test_capability_members_are_exactly_the_declared_eleven() -> None:
    """Vocabulary is closed — a stray member means an undesigned capability.

    Widened from eight to eleven by compute-seam S4, which split how a rate is
    known (RATE_READBACK vs RATE_DETERMINISTIC) from whether a catalog can be
    listed at all (CATALOG_ENUMERATION).
    """
    assert {c.value for c in Capability} == {
        "HEARTBEAT_READ",
        "RUNTIME_PROBE",
        "UTIL_SNAPSHOT",
        "IDLE_AUTOSTOP",
        "ON_INSTANCE_DEADLINE",
        "JOB_TIMEOUT",
        "PAUSE_BILLING",
        "BALANCE_QUERY",
        "RATE_READBACK",
        "RATE_DETERMINISTIC",
        "CATALOG_ENUMERATION",
    }


def test_workload_shape_members() -> None:
    assert {s.value for s in WorkloadShape} == {"server", "batch"}


def test_abc_default_declares_nothing() -> None:
    """A provider that forgets to declare must claim nothing, not everything."""
    assert ComputeProvider.capabilities() == frozenset()
    assert ComputeProvider.capabilities(WorkloadShape.BATCH) == frozenset()
    assert ComputeProvider.billed is True


def test_unknown_provider_name_returns_empty_not_raises() -> None:
    """Matches the old `name not in frozenset` semantics for unknown kinds."""
    assert capabilities_for("nope") == frozenset()
    assert provider_billed("nope") is True


def test_lookup_resolves_in_a_process_that_never_imported_providers() -> None:
    """Catches the reaper answering 'nothing is supported' for every provider
    when the composition root was never imported — a silent, total gate
    disabling. Asserts the MECHANISM only: at Task 0 every provider still
    inherits the empty ABC default, so the capability CONTENT assertion
    belongs to Task 1.
    """
    code = (
        "from kinoforge.core import registry;"
        "assert registry.provider_class('runpod') is None, 'pre-imported';"
        "from kinoforge.core.capabilities import capabilities_for;"
        "caps = capabilities_for('runpod');"
        "print(registry.provider_class('runpod') is not None, isinstance(caps, frozenset))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True True"
