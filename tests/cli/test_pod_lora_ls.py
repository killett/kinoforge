"""kinoforge pod lora ls <pod_id> — direct pod-side inventory query."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from kinoforge.cli._commands import _cmd_pod_lora_ls


class _FakeInstance:
    def __init__(self, pod_id: str) -> None:
        self.id = pod_id


class _FakeProvider:
    def __init__(self, endpoints_map: dict[str, str], instance: _FakeInstance) -> None:
        self._endpoints = endpoints_map
        self._instance = instance

    def endpoints(self, instance: Any) -> dict[str, str]:
        return self._endpoints

    def get_instance(self, pod_id: str) -> _FakeInstance:
        if pod_id != self._instance.id:
            raise KeyError(pod_id)
        return self._instance


class _FakeLedger:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def entries(self) -> list[dict[str, Any]]:
        return self._entries


class _FakeCtx:
    def __init__(self, ledger: _FakeLedger) -> None:
        self._ledger = ledger

    def ledger(self) -> _FakeLedger:
        return self._ledger


def _args(pod_id: str) -> argparse.Namespace:
    return argparse.Namespace(pod_id=pod_id)


def _install_registry(
    monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider | None
) -> None:
    from kinoforge.core import registry as kf_registry

    def _get(name: str) -> Any:
        if provider is None:
            from kinoforge.core.errors import UnknownAdapter

            raise UnknownAdapter(name)
        return lambda: provider

    monkeypatch.setattr(kf_registry, "get_provider", _get, raising=False)


def test_pod_lora_ls_unknown_pod_returns_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pod absent from ledger → exit 1 with clear error.

    Bug: handler tries to deref None instance → AttributeError leaks
    instead of a clean exit code 1.
    """
    ctx = _FakeCtx(_FakeLedger([]))
    rc = _cmd_pod_lora_ls(_args("missing"), ctx)  # type: ignore[arg-type]
    assert rc == 1
    assert "missing" in capsys.readouterr().err


def test_pod_lora_ls_happy_renders_inventory(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy: GET /lora/inventory + render via shared section helper.

    Bug: handler returns the raw JSON dict instead of the pretty-printed
    section, so the operator sees curly braces instead of a table.
    """
    ledger = _FakeLedger(
        [
            {
                "id": "pod-a",
                "provider": "runpod",
                "tags": {"ports": "8000", "mode": "pod"},
                "created_at": 0.0,
            }
        ]
    )
    instance = _FakeInstance("pod-a")
    provider = _FakeProvider({"8000": "https://pod-a-8000.proxy.runpod.net"}, instance)
    _install_registry(monkeypatch, provider)

    inventory = {
        "inventory": [
            {
                "ref": "civitai:A@1",
                "filename": "a.s",
                "size_bytes": 100,
                "downloaded_at_local": "2026-06-20T10:00:00-07:00",
                "last_used_at_local": "2026-06-20T10:00:00-07:00",
                "adapter_name": "lora_0",
            }
        ],
        "free_bytes": 5000,
    }

    captured_url: dict[str, str] = {}

    def _fake_http_get(url: str) -> dict[str, Any]:
        captured_url["url"] = url
        return inventory

    monkeypatch.setattr(
        "kinoforge.cli._commands._http_get_json", _fake_http_get, raising=False
    )

    ctx = _FakeCtx(ledger)
    rc = _cmd_pod_lora_ls(_args("pod-a"), ctx)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert rc == 0
    assert captured_url["url"].endswith("/lora/inventory")
    assert "lora_0" in out
    assert "loras (1 resident" in out


def test_pod_lora_ls_pod_unreachable_returns_2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport error → exit 2 with clear stderr message.

    Bug: handler exits 0 on exception, masking the failure from CI.
    """
    ledger = _FakeLedger(
        [
            {
                "id": "pod-a",
                "provider": "runpod",
                "tags": {"ports": "8000", "mode": "pod"},
                "created_at": 0.0,
            }
        ]
    )
    instance = _FakeInstance("pod-a")
    provider = _FakeProvider({"8000": "https://pod-a-8000.proxy.runpod.net"}, instance)
    _install_registry(monkeypatch, provider)

    def _failing(url: str) -> dict[str, Any]:
        raise ConnectionError("ConnectionResetError")

    monkeypatch.setattr(
        "kinoforge.cli._commands._http_get_json", _failing, raising=False
    )

    ctx = _FakeCtx(ledger)
    rc = _cmd_pod_lora_ls(_args("pod-a"), ctx)  # type: ignore[arg-type]
    err = capsys.readouterr().err
    assert rc == 2
    assert "ConnectionResetError" in err or "unreachable" in err.lower()
