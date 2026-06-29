"""Tests for the /health-driven matcher pre-flight (T14).

The pre-flight refines the warm-attach decision: even if a candidate
pod's ledger ``cap_key`` matches the cfg, the pod can still be the
wrong target if its loaders half-failed (cfg wanted SeedVR2 + Wan but
only Wan came up). ``/health`` reports actually-loaded capabilities so
the matcher can refuse with a deterministic ``stage-mismatch`` rather
than discovering the gap on the first /upscale POST.
"""

from __future__ import annotations

from unittest.mock import patch


class TestHealthPreflight:
    def test_capability_superset_passes(self) -> None:
        # Bug caught: helper returns False on equal-set match because
        # it required strict equality instead of subset semantics → a
        # Wan+SeedVR2 pod is wrongly refused for an upscale-only cfg.
        from kinoforge.cli._commands import _health_preflight_ok

        with patch(
            "kinoforge.cli._commands._http_get_json",
            return_value={"capabilities": ["t2v", "upscale"], "ready": True},
        ):
            assert (
                _health_preflight_ok(
                    proxy_url="https://pod.example",
                    want_stages=("upscale",),
                )
                is True
            )

    def test_capability_missing_refused(self) -> None:
        # Bug caught: helper accepts any reachable pod regardless of
        # capabilities (e.g. boolean default to True) → upscale cfg
        # gets attached to a Wan-only pod, /upscale POSTs return 400
        # asynchronously, wasting one warm-attach cycle.
        from kinoforge.cli._commands import _health_preflight_ok

        with patch(
            "kinoforge.cli._commands._http_get_json",
            return_value={"capabilities": ["t2v"], "ready": True},
        ):
            assert (
                _health_preflight_ok(
                    proxy_url="https://pod.example",
                    want_stages=("upscale",),
                )
                is False
            )

    def test_health_unreachable_returns_none(self) -> None:
        # Bug caught: helper raises instead of soft-returning None, so a
        # transient network blip on a healthy pod synthesises a hard
        # STAGE_MISMATCH refusal. The contract is: unreachable /health
        # → fall through to existing fallback verdict machinery, not
        # invent a verdict from missing data.
        from kinoforge.cli._commands import _health_preflight_ok

        with patch(
            "kinoforge.cli._commands._http_get_json",
            side_effect=ConnectionError("refused"),
        ):
            assert (
                _health_preflight_ok(
                    proxy_url="https://pod.example",
                    want_stages=("upscale",),
                )
                is None
            )

    def test_empty_want_stages_short_circuits_true(self) -> None:
        # Bug caught: helper still hits the network when want_stages is
        # empty → a Wan-only cfg with no stage requirement burns one
        # /health round-trip per matcher iteration. Asserts the helper
        # returns True without calling _http_get_json at all.
        from kinoforge.cli._commands import _health_preflight_ok

        with patch(
            "kinoforge.cli._commands._http_get_json",
            side_effect=AssertionError("HTTP must not be called"),
        ):
            assert (
                _health_preflight_ok(
                    proxy_url="https://pod.example",
                    want_stages=(),
                )
                is True
            )

    def test_malformed_health_missing_capabilities_refuses(self) -> None:
        # Bug caught: helper treats absence of ``capabilities`` key as
        # "permissive default" (returns True) rather than "pod is on an
        # older server build that doesn't report capabilities, treat as
        # uncovered" — leading to attaches against pods we can't verify.
        # Conservative-on-ignorance: missing capabilities key → False
        # when want_stages is non-empty (the older server isn't a known-
        # capable substrate for the cfg).
        from kinoforge.cli._commands import _health_preflight_ok

        with patch(
            "kinoforge.cli._commands._http_get_json",
            return_value={"ready": True},  # no capabilities key at all
        ):
            assert (
                _health_preflight_ok(
                    proxy_url="https://pod.example",
                    want_stages=("upscale",),
                )
                is False
            )


class TestCfgWantStages:
    def test_pure_t2v_cfg_has_no_required_stages(self) -> None:
        # Bug caught: helper hardcodes ("t2v",) for any cfg with an
        # engine block → the preflight starts refusing every Wan-only
        # pod whose /health was added in this workstream but whose
        # capabilities derivation rolled out one build later. Returning
        # () for pure-t2v matches the current capability_key() behavior.
        from kinoforge.cli._commands import _cfg_want_stages
        from kinoforge.core.config import Config

        cfg = Config.model_validate(
            {
                "engine": {"kind": "diffusers", "precision": "fp8"},
                "models": [
                    {
                        "kind": "base",
                        "ref": "hf:Wan-AI/Wan2.2-T2V",
                        "target": "diffusion_models",
                    }
                ],
                "compute": {"provider": "fake", "image": "fake:latest"},
            }
        )
        assert _cfg_want_stages(cfg) == ()

    def test_upscale_attached_cfg_requires_both_stages(self) -> None:
        # Bug caught: helper only reports "upscale" for upscale-attached
        # cfgs and forgets "t2v" → matcher would accept an upscale-only
        # pod (no Wan) for a t2v+upscale cfg.
        from kinoforge.cli._commands import _cfg_want_stages
        from kinoforge.core.config import Config

        cfg = Config.model_validate(
            {
                "engine": {"kind": "diffusers", "precision": "fp8"},
                "models": [
                    {
                        "kind": "base",
                        "ref": "hf:Wan-AI/Wan2.2-T2V",
                        "target": "diffusion_models",
                    }
                ],
                "compute": {"provider": "fake", "image": "fake:latest"},
                "upscale": {
                    "engine": "seedvr2",
                    "scale": "2x",
                    "seedvr2": {"variant": "3B", "precision": "fp8"},
                },
            }
        )
        assert _cfg_want_stages(cfg) == ("t2v", "upscale")
