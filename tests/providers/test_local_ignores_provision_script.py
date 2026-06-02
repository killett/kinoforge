"""Regression: LocalProvider silently ignores spec.provision_script + run_cmd."""

from __future__ import annotations

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.local import LocalProvider


def test_local_provider_ignores_provision_script_and_run_cmd() -> None:
    """create_instance must accept the new spec fields without error or behaviour change."""
    p = LocalProvider()
    spec = InstanceSpec(
        image="ignored",
        provision_script="set -e\necho should-not-run",
        run_cmd=["never", "executed"],
    )
    instance = p.create_instance(spec)
    assert instance.provider == "local"
    assert instance.status == "ready"
