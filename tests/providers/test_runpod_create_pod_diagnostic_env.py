"""C28 A1.5 — ``_create_pod`` overlays ``InstanceSpec.diagnostic_env``.

The overlay merges via ``setdefault``: any key already present in
``spec.env`` wins. This guarantees the diagnostic overlay never clobbers
operator-supplied env vars (e.g. ``HF_TOKEN``) but does inject the C28
diagnostic plumbing (``KINOFORGE_DIAG_*``, ``AWS_*``) when the caller
opts in via ``cfg.diagnostic_mode``.

The kinoforge_secret_never_print rule means tests use synthetic fixture
values; no real keys leak via assertions or captured commit messages.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


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


def _env_keys(body: dict[str, Any]) -> set[str]:
    env_list = body["variables"]["input"]["env"]
    return {e["key"] for e in env_list}


def _env_value(body: dict[str, Any], key: str) -> str:
    env_list = body["variables"]["input"]["env"]
    return next(e["value"] for e in env_list if e["key"] == key)


def test_default_diagnostic_env_empty_does_not_inject_diag_keys() -> None:
    """Empty overlay → wire env contains only user + selfterm/terminate keys."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        env={"HF_TOKEN": "fixture-hf-token"},
    )
    p.create_instance(spec)
    keys = _env_keys(captured[0][1])
    assert "HF_TOKEN" in keys
    assert "KINOFORGE_DIAG_BUCKET" not in keys
    assert "AWS_ACCESS_KEY_ID" not in keys


def test_diagnostic_env_overlay_merged_into_pod_env() -> None:
    """Non-empty overlay → diagnostic keys appear on the wire alongside user env."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        env={"HF_TOKEN": "fixture-hf-token"},
        diagnostic_env={
            "KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics",
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
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        env={"KINOFORGE_DIAG_BUCKET": "user-override-bucket"},
        diagnostic_env={"KINOFORGE_DIAG_BUCKET": "default-overlay-bucket"},
    )
    p.create_instance(spec)
    assert _env_value(captured[0][1], "KINOFORGE_DIAG_BUCKET") == "user-override-bucket"
