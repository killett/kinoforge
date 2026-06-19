"""HeartbeatIntervalRequiredCheck — STATIC ERROR + auto-fix.

Catches the cfg trap from the 2026-06-18 Wan 1.3B CLI warm-reuse
smoke: when ``compute.warm_reuse_auto_attach: true`` is set but
``compute.lifecycle.heartbeat_interval_s`` is unset, the
HeartbeatLoop never starts → no heartbeat_thread_tick sentinel
lands in the ledger → next CLI invocation's classify chain returns
HEARTBEAT_UNKNOWN → cold create. The bug is statically detectable
and the safe default is 30 s (matches every working example cfg).
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


class HeartbeatIntervalRequiredCheck:
    """STATIC ERROR — heartbeat_interval_s required when warm-reuse on."""

    name: str = "heartbeat_interval_required"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff warm_reuse_auto_attach is on AND a lifecycle block exists.

        Skipping cfgs with ``lifecycle is None`` preserves backward
        compat: such cfgs already used interface defaults and silently
        fell back to cold create — the operator never explicitly
        opted into warm-reuse for that cfg. This check catches the
        2026-06-18 smoke trap (operator added lifecycle + opted into
        warm-reuse but forgot heartbeat_interval_s), which is the
        narrowest interpretation that does not break every existing
        cfg without an explicit lifecycle block.
        """
        if cfg.compute is None or cfg.compute.lifecycle is None:
            return False
        return cfg.compute.warm_reuse_auto_attach is True

    def run(self, cfg: Config) -> CheckResult:
        """Fail when heartbeat_interval_s is unset under warm-reuse."""
        assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
        lc = cfg.compute.lifecycle
        if lc is None or lc.heartbeat_interval_s is None:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    "compute.lifecycle.heartbeat_interval_s is required "
                    "when compute.warm_reuse_auto_attach=true; without "
                    "it the HeartbeatLoop never starts and warm-reuse "
                    "falls back to cold create"
                ),
                fix_suggestion=("set compute.lifecycle.heartbeat_interval_s: 30"),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=f"heartbeat_interval_s={lc.heartbeat_interval_s}",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """Return cfg with heartbeat_interval_s defaulted to 30 s."""
        assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
        if cfg.compute.lifecycle is None:
            return None
        new_lifecycle = cfg.compute.lifecycle.model_copy(
            update={"heartbeat_interval_s": 30}
        )
        new_compute = cfg.compute.model_copy(update={"lifecycle": new_lifecycle})
        return cfg.model_copy(update={"compute": new_compute})


register(HeartbeatIntervalRequiredCheck())
