"""Declaration <-> implementation parity (Brief 2, Task 1).

The regression guard that matters: adding a no-op override, or deleting a
real implementation, without editing the declaration must fail here.
"""

from __future__ import annotations

import pytest

from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.interfaces import ComputeProvider
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider

PROVIDERS = [LocalProvider, ModalProvider, RunPodProvider, SkyPilotProvider]


def _overrides(cls: type, method: str) -> bool:
    """True iff ``cls`` overrides ``ComputeProvider.<method>``."""
    return getattr(cls, method) is not getattr(ComputeProvider, method)


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_heartbeat_read_declared_iff_last_heartbeat_overridden(
    cls: type[ComputeProvider],
) -> None:
    """Catches a `return None` override added, or a real read deleted,
    without editing the declaration."""
    declared = Capability.HEARTBEAT_READ in cls.capabilities()
    assert declared is _overrides(cls, "last_heartbeat")


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_runtime_probe_declared_iff_probe_runtime_overridden(
    cls: type[ComputeProvider],
) -> None:
    """Same guard for the sweeper's probe substrate."""
    declared = Capability.RUNTIME_PROBE in cls.capabilities()
    assert declared is _overrides(cls, "probe_runtime")


def test_declared_matrix_matches_the_design_doc() -> None:
    """Pins the whole §7.1 matrix so a silent widening is caught even where
    no ABC method exists to compare against."""
    assert LocalProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.UTIL_SNAPSHOT,
            Capability.PAUSE_BILLING,
        }
    )
    assert RunPodProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.ON_INSTANCE_DEADLINE,
            Capability.JOB_TIMEOUT,
            Capability.PAUSE_BILLING,
            Capability.BALANCE_QUERY,
        }
    )
    assert ModalProvider.capabilities() == frozenset(
        {
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.IDLE_AUTOSTOP,
            Capability.ON_INSTANCE_DEADLINE,
        }
    )
    assert SkyPilotProvider.capabilities() == frozenset(
        {Capability.ON_INSTANCE_DEADLINE}
    )


def test_skypilot_idle_autostop_is_batch_only() -> None:
    """Catches a single-boolean regression: autostop is inert for server
    specs (F1) and real for batch specs (run_cmd=[])."""
    server = SkyPilotProvider.capabilities(WorkloadShape.SERVER)
    batch = SkyPilotProvider.capabilities(WorkloadShape.BATCH)
    assert Capability.IDLE_AUTOSTOP not in server
    assert Capability.IDLE_AUTOSTOP in batch


def test_declared_capabilities_survive_a_lazy_composition_root_import() -> None:
    """The Task 0 lookup test pinned the mechanism; this pins the CONTENT
    through the same lazy path. Catches a declaration that only resolves
    when something else has already imported the providers."""
    import subprocess
    import sys

    code = (
        "from kinoforge.core.capabilities import Capability, capabilities_for;"
        "print(Capability.HEARTBEAT_READ in capabilities_for('runpod'),"
        " Capability.HEARTBEAT_READ in capabilities_for('skypilot'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True False"


def test_billed_flags() -> None:
    """Catches local being treated as a billed provider, which would error
    every local config on the spend rows."""
    assert LocalProvider.billed is False
    assert all(
        c.billed is True for c in (RunPodProvider, ModalProvider, SkyPilotProvider)
    )


def test_parity_check_catches_a_declaration_without_an_implementation() -> None:
    """The parity test must not pass vacuously."""

    class LyingProvider(ComputeProvider):
        @classmethod
        def capabilities(
            cls, shape: WorkloadShape = WorkloadShape.SERVER
        ) -> frozenset[Capability]:
            return frozenset({Capability.RUNTIME_PROBE})

    declared = Capability.RUNTIME_PROBE in LyingProvider.capabilities()
    assert declared is True
    assert _overrides(LyingProvider, "probe_runtime") is False
    # Parity would be violated -> the real parametrized test would fail.
    assert declared is not _overrides(LyingProvider, "probe_runtime")
