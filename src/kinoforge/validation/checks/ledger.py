"""LedgerStaleRowsCheck — PREFLIGHT WARN.

Reads the on-disk ledger and asks the provider whether each row's
pod still exists. Stale rows get surfaced with the suggested
``kinoforge forget --id <id>`` chord so the operator can clean up
before the next ``kinoforge list`` is polluted with ghost entries.

Empirically caught seven stale RunPod rows during the 2026-06-18
session (~$73 phantom estimated spend before the operator swept
them).

Both the ledger loader and provider factory are injectable so the
production wiring lives in the CLI layer (Task 10/11) rather than
imported here — keeps the check independent of SessionContext
construction details.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

_log = logging.getLogger(__name__)


def _empty_ledger_loader() -> list[dict[str, Any]]:
    """Default loader — returns empty.

    Production callers (the CLI wiring in Task 10/11) inject a real
    loader bound to the current SessionContext's ledger.
    """
    return []


def _unbound_provider_factory(name: str) -> Any:  # noqa: ANN401 — provider object is duck-typed
    """Default factory — raises so production callers must inject."""
    raise RuntimeError(
        f"no provider_factory wired for {name!r}; "
        "the CLI must inject a real factory at run time"
    )


class LedgerStaleRowsCheck:
    """PREFLIGHT WARN — surface ledger rows the provider no longer has."""

    name: str = "ledger_stale_rows"
    category: CheckCategory = CheckCategory.PREFLIGHT
    severity: Severity = Severity.WARN

    def __init__(
        self,
        *,
        ledger_loader: Callable[[], list[dict[str, Any]]] | None = None,
        provider_factory: Callable[[str], Any] | None = None,
    ) -> None:
        """Wire ledger + provider seams. Defaults are inert."""
        self._ledger_loader = ledger_loader or _empty_ledger_loader
        self._provider_factory = provider_factory or _unbound_provider_factory

    def applies_to(self, cfg: Config) -> bool:
        """Always applies — operates on the global ledger, not cfg."""
        return True

    def run(self, cfg: Config) -> CheckResult:
        """Load ledger, group by provider, compare to live instances."""
        try:
            entries = self._ledger_loader()
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=f"ledger unavailable: {exc}; skipping stale check",
            )
        if not entries:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=self.severity,
                message="ledger empty",
            )

        by_provider: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            by_provider.setdefault(str(entry.get("provider", "")), []).append(entry)

        stale: list[str] = []
        for provider_name, rows in by_provider.items():
            try:
                provider = self._provider_factory(provider_name)
                live_ids = {i.id for i in provider.list_instances()}
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "ledger_stale_rows: provider %s unreachable: %s",
                    provider_name,
                    exc,
                )
                continue
            for row in rows:
                rid = str(row.get("id", ""))
                if rid and rid not in live_ids:
                    stale.append(rid)

        if not stale:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=self.severity,
                message=f"{len(entries)} ledger row(s); none stale",
            )
        return CheckResult(
            name=self.name,
            passed=False,
            severity=self.severity,
            message=(f"{len(stale)} stale ledger row(s): " + ", ".join(stale)),
            fix_suggestion="; ".join(f"kinoforge forget --id {sid}" for sid in stale),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — operator must sign off on `kinoforge forget`."""
        return None


register(LedgerStaleRowsCheck())
