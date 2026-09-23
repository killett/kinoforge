"""LoRA-related cfg validation checks.

- LoraServerSupportCheck (STATIC ERROR): rejects a ``loras:`` block aimed at
  a diffusers server module with no LoRA surface, or naming a ``target``
  outside that server's model-family vocabulary. Without this check the
  stack is silently ignored on the pod — the operator gets a plausible
  video with no LoRA in it, discovered only by eye after a full boot.

  This check answers only what the controller can know WITHOUT a pod: does
  the named server module serve LoRAs, and what is its target universe (see
  ``kinoforge.core.lora_profiles``). It cannot know which checkpoint
  partitions a given pod actually loaded — H3's ``t2va`` workflow holds one
  partition, ``ref2va`` holds the other — so it does not attempt to be more
  precise than "wrong family / no support / typo". The pod narrows further
  at apply time.
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.core.lora_profiles import (
    client_profile_for_server_module,
    server_module_from_cfg,
)
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


class LoraServerSupportCheck:
    """STATIC ERROR — refuse a `loras:` stack the named server can't serve."""

    name: str = "lora_server_support"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff the cfg declares at least one LoRA on a diffusers engine.

        The registry this check consults (``kinoforge.core.lora_profiles``)
        is scoped to diffusers server modules; it has no opinion on
        ComfyUI's node-graph LoRA loading, which is a different serving
        path entirely. Firing here on a non-diffusers engine would reject
        cfgs whose LoRA support this check cannot see.
        """
        if cfg.engine.kind != "diffusers":
            return False
        return bool(getattr(cfg, "loras", []))

    def run(self, cfg: Config) -> CheckResult:
        """Refuse an unsupported server module, or a target outside its vocabulary."""
        module = server_module_from_cfg(cfg)
        profile = (
            client_profile_for_server_module(module) if module is not None else None
        )
        if profile is None or not profile.supported:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"cfg declares {len(cfg.loras)} LoRA(s) but server module "
                    f"{module!r} is not known to serve them — the stack would be "
                    f"silently ignored on the pod"
                ),
                fix_suggestion=(
                    "remove the `loras:` block, or point `server_cmd` at a server "
                    "with LoRA support (see kinoforge.core.lora_profiles)"
                ),
            )
        illegal = sorted(
            {
                lo.target
                for lo in cfg.loras
                if lo.target is not None and lo.target not in profile.target_universe
            }
        )
        if illegal:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"LoRA target(s) {illegal} are not in this model's vocabulary; "
                    f"legal targets: {list(profile.target_universe)}"
                ),
                fix_suggestion=(
                    f"set target to one of {list(profile.target_universe)}, or omit "
                    f"it to use the pod's default partition"
                ),
            )
        return CheckResult(
            name=self.name, passed=True, severity=self.severity, message="ok"
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — the operator chooses between removing loras and retargeting."""
        del cfg
        return None


register(LoraServerSupportCheck())
