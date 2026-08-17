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
    someone deliberately built.

    Skypilot does NOT declare HEARTBEAT_READ, so the message must say
    exactly that — this is the call-site-bug arm.
    """
    provider = SkyPilotProvider()
    with pytest.raises(ValueError, match="does not declare Capability.HEARTBEAT_READ"):
        provider.set_heartbeat_endpoint(object())


def test_refusal_message_does_not_contradict_a_declaring_provider() -> None:
    """A provider that DECLARES HEARTBEAT_READ and still hits the ABC default
    is a different, louder bug — and must not be told it does not declare it.

    Bug caught: the guard is "did not override", but the message asserted
    "does not declare". ``LocalProvider`` declares ``HEARTBEAT_READ`` and
    inherits this method today, so the old message would have accused a
    class of not declaring a capability its own ``capabilities()`` returns —
    sending whoever debugs it to the declaration instead of to the missing
    ``set_heartbeat_endpoint`` implementation.
    """
    from kinoforge.core.capabilities import Capability
    from kinoforge.providers.local import LocalProvider

    assert Capability.HEARTBEAT_READ in LocalProvider.capabilities()
    with pytest.raises(ValueError) as exc:
        LocalProvider().set_heartbeat_endpoint(object())
    message = str(exc.value)
    assert "declares Capability.HEARTBEAT_READ" in message
    assert "does not implement set_heartbeat_endpoint" in message
    assert "does not declare" not in message


def test_set_heartbeat_endpoint_none_still_passes() -> None:
    """The clear path stays a no-op — it discards nothing.

    ``set_heartbeat_endpoint`` is annotated ``-> None`` (mypy's
    func-returns-value check forbids asserting on its return value), so the
    only observable behavior on the clear path is "does not raise".
    """
    SkyPilotProvider().set_heartbeat_endpoint(None)  # must not raise


def _stop_ctx_with_ledger_row(instance_id: str, provider_name: str) -> Any:
    """Fake SessionContext exposing a single ledger row.

    Mirrors the ``_FakeCtx`` pattern used in ``tests/cli/test_cmd_reap.py``:
    a MagicMock ``ledger()`` accessor whose ``entries()`` returns a fixed
    list of ledger-row dicts.
    """
    entries = [{"id": instance_id, "provider": provider_name}]
    ledger = MagicMock()
    ledger.entries.return_value = entries
    ctx = MagicMock()
    ctx.ledger.return_value = ledger
    return ctx


def _stop_ctx_with_skypilot_row() -> Any:
    """Fake SessionContext exposing a single skypilot ledger row."""
    return _stop_ctx_with_ledger_row("kf-cluster", "skypilot")


def _stop_args(instance_id: str) -> argparse.Namespace:
    """Build the ``argparse.Namespace`` ``_cmd_stop`` expects."""
    return argparse.Namespace(id=instance_id)


def _fake_pause_provider_class(name: str, calls: list[str]) -> type:
    """Build a fake provider class recording stop/destroy calls.

    Declares only ``ON_INSTANCE_DEADLINE`` — never ``PAUSE_BILLING`` — so the
    CLI's capability pre-check must be what refuses the call. If the
    pre-check were deleted or fell through, ``stop_instance`` would run and
    (per the real skypilot/modal implementations) raise
    ``NotImplementedError`` instead of returning cleanly, which would also
    surface as a non-clean exit — the pre-check is what keeps ``calls``
    empty and produces the routed message, not incidental luck.

    Args:
        name: The provider name to stamp on the fake (``"skypilot"`` or
            ``"modal"``).
        calls: Shared list the fake mutates so the test can assert on call
            order/absence.

    Returns:
        A fake provider class usable as ``registry.get_provider`` /
        ``registry.provider_class``'s return value.
    """

    class _FakeProvider:
        pass

    _FakeProvider.name = name  # type: ignore[attr-defined]

    def _capabilities(cls: type, shape: object = None) -> frozenset[Any]:
        from kinoforge.core.capabilities import Capability

        return frozenset({Capability.ON_INSTANCE_DEADLINE})

    def _stop_instance(self: object, instance_id: str) -> None:
        calls.append(f"stop:{instance_id}")
        raise NotImplementedError("use destroy")

    def _destroy_instance(self: object, instance_id: str) -> None:
        calls.append(f"destroy:{instance_id}")

    _FakeProvider.capabilities = classmethod(_capabilities)  # type: ignore[attr-defined]
    _FakeProvider.stop_instance = _stop_instance  # type: ignore[attr-defined]
    _FakeProvider.destroy_instance = _destroy_instance  # type: ignore[attr-defined]
    return _FakeProvider


def test_cli_stop_on_skypilot_row_refuses_without_destroying(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches a pre-check that falls through and turns a pause request into
    a teardown — and catches a pre-check that refuses silently (deleted or
    garbled print), which the pre-fix version of this test could not."""
    from kinoforge.cli import _commands
    from kinoforge.core import registry as core_registry

    calls: list[str] = []
    fake_provider = _fake_pause_provider_class("skypilot", calls)

    # `_cmd_stop` and `capabilities_for` each do their own lazy
    # `from kinoforge.core import registry` inside the function body, so the
    # patch target is the real `kinoforge.core.registry` module — not a
    # `_commands.registry` module-level attribute, which does not exist.
    monkeypatch.setattr(core_registry, "get_provider", lambda name: fake_provider)
    monkeypatch.setattr(core_registry, "provider_class", lambda name: fake_provider)

    rc = _commands._cmd_stop(_stop_args("kf-cluster"), _stop_ctx_with_skypilot_row())

    assert rc != 0
    assert calls == []
    stderr = capsys.readouterr().err
    assert "skypilot" in stderr
    assert "cannot pause billing" in stderr
    assert "kinoforge destroy --id kf-cluster" in stderr


def test_cli_stop_on_modal_row_refuses_without_destroying(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Modal-side analog: the pause pre-check must refuse a modal ledger row
    the same way it refuses a skypilot one — non-zero exit, zero destroy
    calls, and a routed stderr message naming ``destroy``. Nothing exercised
    the modal side of this pre-check before this test."""
    from kinoforge.cli import _commands
    from kinoforge.core import registry as core_registry

    calls: list[str] = []
    fake_provider = _fake_pause_provider_class("modal", calls)

    monkeypatch.setattr(core_registry, "get_provider", lambda name: fake_provider)
    monkeypatch.setattr(core_registry, "provider_class", lambda name: fake_provider)

    ctx = _stop_ctx_with_ledger_row("eph-abc123", "modal")
    rc = _commands._cmd_stop(_stop_args("eph-abc123"), ctx)

    assert rc != 0
    assert calls == []
    stderr = capsys.readouterr().err
    assert "modal" in stderr
    assert "cannot pause billing" in stderr
    assert "kinoforge destroy --id eph-abc123" in stderr


def test_cli_stop_answers_from_the_class_without_constructing_the_provider(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The PAUSE_BILLING pre-check must run BEFORE the provider is built.

    Whether a provider can pause billing is a class-level question. Bug
    caught: constructing the provider first, so a provider whose ``__init__``
    needs credentials (or any other environment the operator does not have)
    tracebacks out of ``kinoforge stop`` instead of printing the routed
    "use destroy" message — an unhandled exception in place of a diagnostic,
    on a question that never required reaching the provider at all.
    """
    from kinoforge.cli import _commands
    from kinoforge.core import registry as core_registry

    constructed: list[str] = []

    class _CredHungryProvider:
        name = "skypilot"

        def __init__(self) -> None:
            constructed.append("init")
            raise RuntimeError("SKYPILOT_API_KEY is not set")

        @classmethod
        def capabilities(cls, shape: object = None) -> frozenset[Any]:
            from kinoforge.core.capabilities import Capability

            return frozenset({Capability.ON_INSTANCE_DEADLINE})

    monkeypatch.setattr(core_registry, "get_provider", lambda name: _CredHungryProvider)
    monkeypatch.setattr(
        core_registry, "provider_class", lambda name: _CredHungryProvider
    )

    rc = _commands._cmd_stop(_stop_args("kf-cluster"), _stop_ctx_with_skypilot_row())

    assert rc != 0
    assert constructed == []
    assert "cannot pause billing" in capsys.readouterr().err
