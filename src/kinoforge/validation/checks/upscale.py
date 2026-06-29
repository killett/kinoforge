"""Upscaler-related cfg validation checks.

- SeedVR2ExtrasPendingCheck (PREFLIGHT ERROR): rejects
  ``cfg.upscale.engine == "seedvr2"`` until the Phase 2 vendoring
  workstream lands. Mirrors the ``ExtrasNotInstalled`` install_hint
  from :class:`kinoforge.upscalers.seedvr2.SeedVR2Engine` so the
  operator sees structured remediation at cfg-time, before any pod
  is created.
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


class SeedVR2ExtrasPendingCheck:
    """PREFLIGHT ERROR — refuse cfg.upscale.engine == 'seedvr2' until Phase 2 vendor."""

    name: str = "seedvr2_extras_pending"
    category: CheckCategory = CheckCategory.PREFLIGHT
    severity: Severity = Severity.ERROR

    _MESSAGE: str = (
        "kinoforge[seedvr] extras not installed — "
        "video-coherent upscaling (SeedVR2) pending Phase 2 vendoring; "
        "use cfg.upscale.engine = 'spandrel' for v1"
    )

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff cfg has an upscale block whose engine names the extras-stub."""
        if cfg.upscale is None:
            return False
        return cfg.upscale.engine == "seedvr2"

    def run(self, cfg: Config) -> CheckResult:
        """Always fail with the extras-not-installed remediation message."""
        del cfg
        return CheckResult(
            name=self.name,
            passed=False,
            severity=self.severity,
            message=self._MESSAGE,
            fix_suggestion=(
                "set cfg.upscale.engine = 'spandrel' (v1 default) or remove "
                "the upscale block until Phase 2 vendoring ships"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — operator must choose between spandrel and removing upscale."""
        del cfg
        return None


register(SeedVR2ExtrasPendingCheck())
