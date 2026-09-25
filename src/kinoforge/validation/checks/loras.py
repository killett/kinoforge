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

- LoraStackConflictCheck (PREFLIGHT ERROR) and LoraRefsResolvableCheck
  (NETWORK ERROR): move three LoRA-resolution failure classes ahead of
  ``create_instance`` (U57). All three used to fire only from inside
  ``core/lora_apply.ensure_lora_stack``, which ``deploy_session`` calls AFTER
  the pod reports ready — loud (D7) but not cheap, since an H200 is already
  billing by then. None of the three needs anything the pod reports back.

  Both read the CLI/vault stack off the ambient ``EphemeralSession``, exactly
  as ``ensure_lora_stack`` and ``core/warm_reuse/integration`` do, so the
  check protocol stays ``run(cfg)``.
"""

from __future__ import annotations

from typing import Any

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
        """Refuse an unresolvable/unsupported server module, or an illegal target."""
        module = server_module_from_cfg(cfg)
        if module is None:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"cfg declares {len(cfg.loras)} LoRA(s) but "
                    f"engine.diffusers.server_cmd names no server module — "
                    f"nothing can serve them"
                ),
                fix_suggestion=(
                    "declare `engine.diffusers.server_cmd: [python, -m, "
                    "<server module>]`, or remove the `loras:` block"
                ),
            )
        profile = client_profile_for_server_module(module)
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


def _active_session() -> Any:  # noqa: ANN401 — EphemeralSession, imported lazily
    # `Any` rather than the real type: importing `EphemeralSession` at module
    # scope for an annotation would put a core import on a path that must stay
    # cheap, and the two attributes read here are duck-typed anyway.
    """Return the ambient EphemeralSession, or None.

    Imported lazily and defensively: these checks run inside ``load_config``'s
    validation pipeline, and a session is optional on every path.

    Returns:
        The current session, or ``None`` when there is none.
    """
    try:
        from kinoforge.core.ephemeral import EphemeralSession

        return EphemeralSession.current()
    except Exception:  # noqa: BLE001 — a check must never crash the pipeline
        return None


class LoraStackConflictCheck:
    """PREFLIGHT ERROR — refuse a diverging cfg/vault stack before any spend.

    ``resolve_active_lora_stack`` raises ``LoraStackConflict`` when cfg.loras
    and vault.loras are both non-empty with diverging ref SETS. That raise was
    first reachable from ``ensure_lora_stack``, i.e. after the pod booted.

    PREFLIGHT rather than STATIC on purpose: the vault lives on the ambient
    ``EphemeralSession``, which does not exist at ``load_config`` time, so a
    STATIC check could never see the half of the input that creates the
    conflict.

    ``_cmd_generate``'s existing eager ``resolve_active_lora_stack`` call does
    NOT already cover this. It runs only inside ``if _raw_loras is not None``
    — i.e. only when ``--loras`` was passed — and in that case ``cli_loras is
    not None``, so the resolver returns early and the conflict branch is
    unreachable by construction.
    """

    name: str = "lora_stack_conflict"
    category: CheckCategory = CheckCategory.PREFLIGHT
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff this run has a LoRA stack from any source.

        A cfg with no ``loras:`` can still conflict only if the vault supplies
        one, and a vault-only stack cannot diverge from an empty cfg — the
        resolver returns it unchanged. So a cfg-side stack is the necessary
        condition, and checking it keeps this off every LoRA-less run.
        """
        return bool(getattr(cfg, "loras", []))

    def run(self, cfg: Config) -> CheckResult:
        """Resolve the stack exactly as the run will, and report a conflict."""
        from kinoforge.core.errors import LoraStackConflict
        from kinoforge.core.lora import resolve_active_lora_stack

        session = _active_session()
        vault = getattr(session, "vault", None) if session is not None else None
        cli_loras = getattr(session, "cli_loras", None) if session is not None else None
        try:
            resolve_active_lora_stack(cfg, vault, cli_loras=cli_loras)
        except LoraStackConflict:
            # Privacy: counts, never refs. The exception's own message lists
            # both ref sets — right for a traceback under an active
            # RedactionRegistry, wrong here, because `_cmd_generate` prints
            # this straight to stderr and a vault ref is a secret. Same rule
            # `resolve_download_specs` already follows.
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"cfg declares {len(cfg.loras)} LoRA(s) and the loaded vault "
                    f"declares a different set — the run cannot tell which is "
                    f"authoritative (refs omitted: a vault ref is a secret)"
                ),
                fix_suggestion=(
                    "remove the `loras:` block and let the vault be the sole "
                    "source, or pass `--loras` to override both for this run"
                ),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=Severity.ERROR,
            message="LoRA stack resolves without a cfg/vault conflict",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — only the operator knows which stack is authoritative."""
        del cfg
        return None


class LoraRefsResolvableCheck:
    """NETWORK ERROR — refuse an unfetchable LoRA ref before any spend.

    Covers the two DETERMINISTIC failures ``resolve_download_specs`` raises:
    a ref that resolves to zero downloadable artifacts (``ValidationError``)
    and a ref no registered source handles (``UnknownAdapter``). Both depend
    only on the ref plus credentials, so both are answerable before
    ``create_instance``.

    Anything else a source raises — a timeout, an auth blip, a 5xx — is
    UNCERTAINTY, not a verdict on the ref, and is deliberately allowed to
    pass. Failing on it would convert a retryable condition into a hard
    preflight block for a stack that is perfectly valid; the post-boot apply
    still fails loudly (D7) if the ref really is bad.
    """

    name: str = "lora_refs_resolvable"
    category: CheckCategory = CheckCategory.NETWORK
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff there is a stack to resolve — no LoRAs, no network call."""
        if getattr(cfg, "loras", []):
            return True
        session = _active_session()
        for attr in ("cli_loras", "vault"):
            source = getattr(session, attr, None) if session is not None else None
            entries = source if attr == "cli_loras" else getattr(source, "loras", None)
            if entries:
                return True
        return False

    def run(self, cfg: Config) -> CheckResult:
        """Resolve every ref's download spec, reporting the deterministic failures."""
        from kinoforge.core.credentials import EnvCredentialProvider
        from kinoforge.core.errors import UnknownAdapter, ValidationError
        from kinoforge.core.lora import resolve_active_lora_stack
        from kinoforge.core.lora_apply import resolve_download_specs

        session = _active_session()
        vault = getattr(session, "vault", None) if session is not None else None
        cli_loras = getattr(session, "cli_loras", None) if session is not None else None
        try:
            stack = resolve_active_lora_stack(cfg, vault, cli_loras=cli_loras)
        except Exception:  # noqa: BLE001 — LoraStackConflictCheck owns that report
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.ERROR,
                message="stack did not resolve; see lora_stack_conflict",
            )
        if not stack:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.ERROR,
                message="no LoRA refs to resolve",
            )
        try:
            # Same credential fallback `ensure_lora_stack` uses at apply
            # time — a preflight that resolved under different credentials
            # than the run would be answering a different question.
            resolve_download_specs(
                [lo.ref for lo in stack], creds=EnvCredentialProvider()
            )
        except (ValidationError, UnknownAdapter) as exc:
            # `resolve_download_specs` already names the stack POSITION rather
            # than the ref, for the same privacy reason, so its message is
            # safe to surface verbatim. `UnknownAdapter` is not equally
            # careful, so it is summarised rather than quoted.
            detail = (
                str(exc)
                if isinstance(exc, ValidationError)
                else "no registered source handles one of the refs"
            )
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"a LoRA ref cannot be fetched, so the pod could not load "
                    f"the stack: {detail}"
                ),
                fix_suggestion=(
                    "check the ref spelling and that its source scheme is "
                    "registered; a private repo also needs credentials"
                ),
            )
        except Exception:  # noqa: BLE001 — transient: uncertainty is not a verdict
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.ERROR,
                message=(
                    "LoRA ref resolution was inconclusive (transient source "
                    "failure); the post-boot apply still fails loudly if a ref "
                    "is bad"
                ),
            )
        return CheckResult(
            name=self.name,
            passed=True,
            severity=Severity.ERROR,
            message=f"all {len(stack)} LoRA ref(s) resolve to a downloadable artifact",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — an unfetchable ref cannot be guessed at."""
        del cfg
        return None


register(LoraStackConflictCheck())
register(LoraRefsResolvableCheck())
