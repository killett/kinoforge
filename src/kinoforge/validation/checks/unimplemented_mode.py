"""ServerlessUnimplementedCheck — refuse ``mode: serverless`` on RunPod (U46).

The create for that mode is invalid GraphQL: ``_CREATE_SERVERLESS_MUTATION``
declares ``EndpointInput!`` but calls ``saveTemplate``, which takes
``SaveTemplateInput``, so RunPod answers ``GRAPHQL_VALIDATION_FAILED`` as a raw
**HTTP 400**. ``CLAUDE.md`` records that exact shape as the one that reads like
an outage rather than a malformed request — the operator goes hunting RunPod
status pages for a typo in our own document.

Refusing it is the honest response rather than fixing the mutation, because
there is no working feature behind the bug. Measured 2026-09-14: no serverless
worker handler exists anywhere in ``src/``, ``runpod`` is not a dependency,
nothing consumes a serverless instance, and ``_create_serverless`` returns an
``Instance`` with no endpoints at all. The mismatch is protocol-level — a RunPod
serverless worker polls RunPod's job queue through a handler, while every
kinoforge engine is an HTTP server reached through the POD proxy.

**Why PREFLIGHT and not STATIC**, which is the category the invariant otherwise
belongs to: a STATIC ERROR rejects ``load_config`` itself. That would break
``tools/snapshot_launch_payloads.capture_payload``, force
``runpod-diffusers-serverless.yaml`` back into ``EXCLUDED_CONFIGS``, and
silently undo U35 — taking the only serverless wire kinoforge has back out of
the launch-payload ratchet. Refusing the operator and freezing the wire are
different jobs, and only the first should happen at generate time. Do not
"correct" this to STATIC without reading
``test_the_shipped_serverless_config_still_LOADS``.
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register

#: Provider + compute-mode pairs kinoforge routes but cannot actually run,
#: mapped to the item recording why. Keyed on ``cfg.compute.mode`` — the
#: OPERATOR's declaration — and never on ``Offer.mode``, which is a different
#: field that Modal's entire catalog sets to "serverless" quite legitimately.
_UNIMPLEMENTED: dict[tuple[str, str], str] = {
    ("runpod", "serverless"): "U46",
}


class ServerlessUnimplementedCheck:
    """PREFLIGHT ERROR — refuse a provider/mode pair that cannot run."""

    name: str = "unimplemented_compute_mode"
    category: CheckCategory = CheckCategory.PREFLIGHT
    severity: Severity = Severity.ERROR

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff the cfg names both a provider and a compute mode."""
        return cfg.compute is not None and bool(cfg.compute.mode)

    def run(self, cfg: Config) -> CheckResult:
        """Fail when the cfg's (provider, mode) pair is known-unrunnable."""
        assert cfg.compute is not None  # noqa: S101 — guarded by applies_to
        provider = str(cfg.compute.provider)
        mode = str(cfg.compute.mode)
        item = _UNIMPLEMENTED.get((provider, mode))
        if item is None:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=self.severity,
                message=f"compute.mode={mode!r} is implemented for {provider!r}",
            )
        return CheckResult(
            name=self.name,
            passed=False,
            severity=self.severity,
            message=(
                f"compute.mode={mode!r} is not implemented for provider "
                f"{provider!r}: no worker handler exists and no engine can "
                f"reach a serverless endpoint, so the create would fail with a "
                f"raw HTTP 400 that reads like a provider outage ({item})"
            ),
            fix_suggestion="set compute.mode: pod",
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """No auto-fix — switching an operator's compute mode is their call.

        Rewriting ``serverless`` to ``pod`` silently changes what gets booked
        and what it costs. The refusal names the one-line fix instead.
        """
        return None


register(ServerlessUnimplementedCheck())
