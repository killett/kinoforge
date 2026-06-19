"""Lifecycle consistency checks.

- IdleTimeoutVsHeartbeatCheck (STATIC ERROR): assert
  ``cfg.lifecycle().idle_timeout_s >= 3 *
  cfg.lifecycle().heartbeat_interval_s``. This is the reaper's
  dead-man window (``heartbeat_interval_s * 3`` per
  ``src/kinoforge/core/reaper.py``); idle_timeout below that means the
  reaper would mark every pod as orphaned before a single missed
  heartbeat tick should plausibly fire.

- GraceAfterSessionTooTightCheck (STATIC WARN): catch
  grace_after_session_s explicitly set below 600 s, which blows the
  operator-typing-pace window between two `kinoforge generate`
  invocations. Caught 2026-06-18 Wan 14B smoke (300-s default + 5-min
  operator gap → ORPHAN_REAP → cold create). Default already bumped
  to 1800 in interfaces.py:94.
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


class IdleTimeoutVsHeartbeatCheck:
    """STATIC ERROR — idle_timeout_s >= 3 * heartbeat_interval_s."""

    name: str = "idle_timeout_vs_heartbeat"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff heartbeat_interval_s is set on the rendered lifecycle."""
        if cfg.compute is None or cfg.compute.lifecycle is None:
            return False
        return cfg.compute.lifecycle.heartbeat_interval_s is not None

    def run(self, cfg: Config) -> CheckResult:
        """Fail when idle_timeout_s < 3 * heartbeat_interval_s."""
        lc = cfg.lifecycle()
        idle = lc.idle_timeout_s
        hb = lc.heartbeat_interval_s
        assert hb is not None  # noqa: S101 — guarded by applies_to
        dead_man = 3 * hb
        if idle < dead_man:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    f"idle_timeout_s={idle}s is less than the reaper's "
                    f"dead-man window (3 * heartbeat_interval_s = "
                    f"{dead_man}s); pods would be reaped before a "
                    f"single missed heartbeat could fire"
                ),
                fix_suggestion=(
                    f"raise compute.lifecycle.idle_timeout to at least "
                    f"{dead_man}s, or lower heartbeat_interval_s"
                ),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=f"idle_timeout_s={idle}s >= dead_man={dead_man}s",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No safe auto-fix — operator chose both knobs."""
        return None


class GraceAfterSessionTooTightCheck:
    """STATIC WARN — grace_after_session_s >= 600 s floor."""

    name: str = "grace_after_session_too_tight"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    _FLOOR_S: int = 600

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff a lifecycle block exists on compute."""
        return cfg.compute is not None and cfg.compute.lifecycle is not None

    def run(self, cfg: Config) -> CheckResult:
        """Warn when rendered grace_after_session_s is below 600 s."""
        grace = cfg.lifecycle().grace_after_session_s
        if grace < self._FLOOR_S:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    f"compute.lifecycle.grace_after_session_s={grace}s is "
                    f"below the {self._FLOOR_S}s operator-typing-pace "
                    f"floor; cmd 2's classify chain risks ORPHAN_REAP "
                    f"before the operator has time to type it"
                ),
                fix_suggestion=(
                    f"raise compute.lifecycle.grace_after_session_s to "
                    f"at least {self._FLOOR_S}s (default is 1800s); "
                    "or accept that quick-fire CLI sequences may cold-create"
                ),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=f"grace_after_session_s={grace}s >= {self._FLOOR_S}s",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — operator's explicit choice is preserved (warned)."""
        return None


register(IdleTimeoutVsHeartbeatCheck())
register(GraceAfterSessionTooTightCheck())
