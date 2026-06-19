"""LedgerStaleRowsCheck tests."""

from __future__ import annotations

from typing import Any

from kinoforge.core.config import load_config
from kinoforge.validation.checks.ledger import LedgerStaleRowsCheck
from kinoforge.validation.protocol import CheckCategory, Severity

_CFG = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: runpod
  image: "runpod/pytorch:latest"
  mode: pod
  lifecycle:
    budget: 1.0
"""


def _ledger_loader(entries: list[dict[str, Any]]) -> Any:
    return lambda: entries


def _provider_factory(live_ids: set[str]) -> Any:
    class _Stub:
        def list_instances(self) -> list[Any]:
            return [type("I", (), {"id": i})() for i in live_ids]

    def factory(name: str) -> Any:
        return _Stub()

    return factory


def test_check_metadata() -> None:
    check = LedgerStaleRowsCheck(
        ledger_loader=_ledger_loader([]),
        provider_factory=_provider_factory(set()),
    )
    assert check.name == "ledger_stale_rows"
    assert check.category == CheckCategory.PREFLIGHT
    assert check.severity == Severity.WARN


def test_passes_when_no_stale_rows() -> None:
    cfg = load_config(_CFG)
    entries = [{"id": "alive1", "provider": "runpod"}]
    check = LedgerStaleRowsCheck(
        ledger_loader=_ledger_loader(entries),
        provider_factory=_provider_factory({"alive1"}),
    )
    result = check.run(cfg)
    assert result.passed is True


def test_warns_when_ledger_has_stale_row() -> None:
    cfg = load_config(_CFG)
    entries = [
        {"id": "alive1", "provider": "runpod"},
        {"id": "ghost9", "provider": "runpod"},
    ]
    check = LedgerStaleRowsCheck(
        ledger_loader=_ledger_loader(entries),
        provider_factory=_provider_factory({"alive1"}),
    )
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity == Severity.WARN
    assert "ghost9" in result.message
    assert "kinoforge forget --id ghost9" in (result.fix_suggestion or "")
