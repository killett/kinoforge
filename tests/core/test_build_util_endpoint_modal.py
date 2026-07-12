"""Offline: modal is util-supported and the factory builds a ModalUtilEndpoint."""

from __future__ import annotations

from types import SimpleNamespace

from kinoforge._adapters import build_util_endpoint_for
from kinoforge.core.util_endpoints import provider_util_supported
from kinoforge.providers.modal.util import ModalUtilEndpoint


def test_modal_is_util_supported() -> None:
    assert provider_util_supported("modal") is True


def _modal_cfg(*, stall: bool = True) -> SimpleNamespace:
    lifecycle = SimpleNamespace(
        stall_reap_enabled=stall, restart_loop_reap_enabled=False
    )
    compute = SimpleNamespace(provider="modal", lifecycle=lifecycle)
    return SimpleNamespace(compute=compute)


def test_factory_builds_modal_endpoint_with_resolver() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(
        _modal_cfg(),  # type: ignore[arg-type]
        creds,  # type: ignore[arg-type]
        resolve_modal_endpoint=lambda _id: "https://x.modal.run",
    )
    assert isinstance(ep, ModalUtilEndpoint)


def test_factory_none_when_reap_disabled() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(
        _modal_cfg(stall=False),  # type: ignore[arg-type]
        creds,  # type: ignore[arg-type]
        resolve_modal_endpoint=lambda _id: "u",
    )
    assert ep is None


def test_factory_none_when_no_resolver() -> None:
    creds = SimpleNamespace(get=lambda _k: None)
    ep = build_util_endpoint_for(_modal_cfg(), creds)  # type: ignore[arg-type]
    assert ep is None
