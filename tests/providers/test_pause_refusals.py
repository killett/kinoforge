"""Pause + endpoint-install refusals (Brief 2, Task 3)."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def test_skypilot_stop_refuses_instead_of_silently_doing_nothing() -> None:
    """Catches the current lie: `kinoforge stop` reports success while the
    cluster keeps billing."""
    with pytest.raises(NotImplementedError, match="destroy"):
        SkyPilotProvider().stop_instance("kf-cluster")


def test_modal_stop_refuses_instead_of_destroying() -> None:
    """Catches pause-means-destroy: asking to pause must not lose the app
    and the warm container."""
    with pytest.raises(NotImplementedError, match="destroy"):
        ModalProvider().stop_instance("eph-abc123")


def test_set_heartbeat_endpoint_rejects_a_wired_endpoint_it_would_discard() -> None:
    """Catches the uniform install path silently dropping an endpoint that
    someone deliberately built."""
    provider = SkyPilotProvider()
    with pytest.raises(ValueError, match="HEARTBEAT_READ"):
        provider.set_heartbeat_endpoint(object())


def test_set_heartbeat_endpoint_none_still_passes() -> None:
    """The clear path stays a no-op — it discards nothing.

    ``set_heartbeat_endpoint`` is annotated ``-> None`` (mypy's
    func-returns-value check forbids asserting on its return value), so the
    only observable behavior on the clear path is "does not raise".
    """
    SkyPilotProvider().set_heartbeat_endpoint(None)  # must not raise


def _stop_ctx_with_skypilot_row() -> Any:
    """Fake SessionContext exposing a single skypilot ledger row.

    Mirrors the ``_FakeCtx`` pattern used in ``tests/cli/test_cmd_reap.py``:
    a MagicMock ``ledger()`` accessor whose ``entries()`` returns a fixed
    list of ledger-row dicts.
    """
    entries = [{"id": "kf-cluster", "provider": "skypilot"}]
    ledger = MagicMock()
    ledger.entries.return_value = entries
    ctx = MagicMock()
    ctx.ledger.return_value = ledger
    return ctx


def _stop_args(instance_id: str) -> argparse.Namespace:
    """Build the ``argparse.Namespace`` ``_cmd_stop`` expects."""
    return argparse.Namespace(id=instance_id)


def test_cli_stop_on_skypilot_row_refuses_without_destroying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a pre-check that falls through and turns a pause request into
    a teardown."""
    from kinoforge.cli import _commands
    from kinoforge.core import registry as core_registry

    calls: list[str] = []

    class _FakeProvider:
        name = "skypilot"

        @classmethod
        def capabilities(cls, shape=None):  # noqa: ANN001, ANN206
            from kinoforge.core.capabilities import Capability

            return frozenset({Capability.ON_INSTANCE_DEADLINE})

        def stop_instance(self, instance_id: str) -> None:
            calls.append(f"stop:{instance_id}")
            raise NotImplementedError("use destroy")

        def destroy_instance(self, instance_id: str) -> None:
            calls.append(f"destroy:{instance_id}")

    # `_cmd_stop` and `capabilities_for` each do their own lazy
    # `from kinoforge.core import registry` inside the function body, so the
    # patch target is the real `kinoforge.core.registry` module — not a
    # `_commands.registry` module-level attribute, which does not exist.
    monkeypatch.setattr(core_registry, "get_provider", lambda name: _FakeProvider)
    monkeypatch.setattr(core_registry, "provider_class", lambda name: _FakeProvider)

    rc = _commands._cmd_stop(_stop_args("kf-cluster"), _stop_ctx_with_skypilot_row())
    assert rc != 0
    assert calls == []
