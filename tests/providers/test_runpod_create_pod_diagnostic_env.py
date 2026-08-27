"""C28 A1.5 / compute-seam S1 Task 7 — diagnostic env reaches the RunPod wire.

Before Task 7, ``InstanceSpec.diagnostic_env`` was a distinct overlay field
that ``RunPodProvider._assemble_create_env`` merged into the pod env via
``setdefault``. Task 7 folded that overlay into ``spec.env`` at
``build_instance_spec`` time instead, so the merge now happens once, upstream
of every provider, and ``RunPodProvider`` just forwards ``spec.env``
unmodified.

This module proves the wire is unchanged end-to-end: build a real spec via
``build_instance_spec`` with ``cfg.diagnostic_mode = True`` and a diagnostic
overlay (mirroring what ``orchestrator._build_diagnostic_env`` produces), then
capture what ``RunPodProvider.create_instance`` actually sends. This exercises
the full cfg -> spec -> wire path directly (in the spirit of
``tools/snapshot_launch_payloads.capture_payload``) rather than adding a new
shipped example config purely to grow the golden ratchet: none of the 31
existing example configs sets ``diagnostic_mode: true``, and the golden
snapshot tool's ``build_spec`` helper hardcodes ``diagnostic_env=None``
regardless of ``cfg.diagnostic_mode``, so a config-based golden would not
actually exercise this path without also changing that tool — out of scope
for this task.

The kinoforge_secret_never_print rule means tests use synthetic fixture
values; no real keys leak via assertions or captured commit messages.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer, RenderedProvision
from kinoforge.core.spec_builder import build_instance_spec
from kinoforge.providers.runpod import RunPodProvider

_CFG = "examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml"


def _capture_post() -> tuple[
    list[tuple[str, dict[str, Any]]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append((url, body))
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _offer() -> Offer:
    return Offer(
        id="NVIDIA RTX 4090",
        gpu_type="NVIDIA RTX 4090",
        vram_gb=24,
        cuda="12.8",
        cost_rate_usd_per_hr=0.30,
    )


def _rendered() -> RenderedProvision:
    return RenderedProvision(
        script="#!/bin/bash\necho hi\nexec python -m server",
        run_cmd=["python", "-m", "server"],
        image="img:tag",
        ports=["8000/http"],
        env_required=[],
    )


def _build_spec(
    *,
    diagnostic_mode: bool,
    env: dict[str, str] | None = None,
    diagnostic_env: dict[str, str] | None = None,
) -> InstanceSpec:
    cfg = load_config(_CFG)
    cfg.diagnostic_mode = diagnostic_mode
    return build_instance_spec(
        cfg=cfg,
        rendered=_rendered(),
        offer=_offer(),
        engine_name="diffusers",
        key_hash="abc123",
        image="fallback:img",
        lifecycle=Lifecycle(),
        env=env or {"HF_TOKEN": "fixture-hf-token"},
        run_id="run-1",
        diagnostic_env=diagnostic_env,
    )


def _env_keys(body: dict[str, Any]) -> set[str]:
    env_list = body["variables"]["input"]["env"]
    return {e["key"] for e in env_list}


def _env_value(body: dict[str, Any], key: str) -> str:
    env_list = body["variables"]["input"]["env"]
    return next(e["value"] for e in env_list if e["key"] == key)


def test_default_diagnostic_env_empty_does_not_inject_diag_keys() -> None:
    """diagnostic_mode off -> wire env contains only user + selfterm/terminate keys."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = _build_spec(diagnostic_mode=False)
    p.create_instance(spec)
    keys = _env_keys(captured[0][1])
    assert "HF_TOKEN" in keys
    assert "KINOFORGE_DIAG_BUCKET" not in keys
    assert "AWS_ACCESS_KEY_ID" not in keys


def test_diagnostic_env_overlay_merged_into_pod_env() -> None:
    """diagnostic_mode on -> diagnostic keys appear on the wire alongside user env."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = _build_spec(
        diagnostic_mode=True,
        diagnostic_env={
            "KINOFORGE_DIAG_BUCKET": "<DIAG_BUCKET>",
            "KINOFORGE_DIAG_PREFIX": "boot-logs/run-xyz",
            "AWS_ACCESS_KEY_ID": "AKIA-FIXTURE",
            "AWS_SECRET_ACCESS_KEY": "fixture-secret",
            "AWS_DEFAULT_REGION": "us-west-2",
        },
    )
    p.create_instance(spec)
    keys = _env_keys(captured[0][1])
    assert "HF_TOKEN" in keys
    for k in (
        "KINOFORGE_DIAG_BUCKET",
        "KINOFORGE_DIAG_PREFIX",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_DEFAULT_REGION",
    ):
        assert k in keys, f"missing diag key on wire: {k}"


def test_diagnostic_env_does_not_overwrite_user_env() -> None:
    """User-explicit env wins over diagnostic overlay (``setdefault`` semantics)."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = _build_spec(
        diagnostic_mode=True,
        env={"KINOFORGE_DIAG_BUCKET": "user-override-bucket"},
        diagnostic_env={"KINOFORGE_DIAG_BUCKET": "default-overlay-bucket"},
    )
    p.create_instance(spec)
    assert _env_value(captured[0][1], "KINOFORGE_DIAG_BUCKET") == "user-override-bucket"
