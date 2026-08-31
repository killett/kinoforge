"""C28 A1.5 / compute-seam S1 Task 7 — diagnostic env reaches the RunPod wire.

Before Task 7, ``InstanceSpec.diagnostic_env`` was a distinct overlay field
that ``RunPodProvider._assemble_create_env`` merged into the pod env via
``setdefault``. Task 7 folded that overlay into ``spec.env`` at
``build_instance_spec`` time instead, so the merge now happens once, upstream
of every provider, and ``RunPodProvider`` just forwards ``spec.env``
unmodified.

This module proves the wire two ways.

First, build a real spec via ``build_instance_spec`` with
``cfg.diagnostic_mode = True`` and a hand-supplied diagnostic overlay, then
capture what ``RunPodProvider.create_instance`` actually sends — the full
cfg -> spec -> wire path, driven directly rather than through a shipped
config, since none of the 31 existing example configs sets
``diagnostic_mode: true`` and adding one purely to serve this test is scope
this plan did not ask for.

Second (added in review — see ``tools/snapshot_launch_payloads._diagnostic_env``),
drive the exact same ``capture_launch`` substrate that produces every one of
the 31 golden files, with ``mutate_cfg`` flipping ``diagnostic_mode`` on an
already-shipped config. Before that harness change, ``build_spec`` hardcoded
``diagnostic_env=None`` regardless of ``cfg.diagnostic_mode``, so a
diagnostic-mode capture was structurally impossible — the diagnostic path was
invisible to the ratchet and every later compute-seam stage built on it. This
closes that gap without inventing a shipped ``diagnostic_mode: true`` config.

The kinoforge_secret_never_print rule means tests use synthetic fixture
values; no real keys leak via assertions or captured commit messages.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import (
    InstanceSpec,
    Launch,
    Lifecycle,
    Offer,
    RenderedProvision,
)
from kinoforge.core.spec_builder import build_instance_spec
from kinoforge.providers.runpod import RunPodProvider
from tools.snapshot_launch_payloads import GOLDEN_RUN_ID, STUB_SECRET, capture_launch

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
        launch=Launch(("python", "-m", "server")),
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
    """diagnostic_mode on -> every diagnostic value lands on the wire unmangled.

    Bug caught: a presence-only check (``k in keys``) passes even if the merge
    silently swapped, truncated, or blanked a value — the pre-Task-7 path
    merged with ``setdefault`` too, so presence alone cannot distinguish
    "merged correctly" from "merged some wrong value". Every key here is
    compared against the exact overlay value supplied.
    """
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    overlay = {
        "KINOFORGE_DIAG_BUCKET": "<DIAG_BUCKET>",
        "KINOFORGE_DIAG_PREFIX": "boot-logs/run-xyz",
        "AWS_ACCESS_KEY_ID": "AKIA-FIXTURE",
        "AWS_SECRET_ACCESS_KEY": "fixture-secret",
        "AWS_DEFAULT_REGION": "us-west-2",
    }
    spec = _build_spec(diagnostic_mode=True, diagnostic_env=overlay)
    p.create_instance(spec)
    body = captured[0][1]
    assert "HF_TOKEN" in _env_keys(body)
    for key, expected_value in overlay.items():
        assert _env_value(body, key) == expected_value, (
            f"wire value for {key} does not match"
        )


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


def _turn_on_diagnostic_mode(cfg: Any) -> Any:  # noqa: ANN401 — Config, tool-provided
    return cfg.model_copy(update={"diagnostic_mode": True})


def test_capture_launch_diagnostic_mode_produces_expected_wire_env() -> None:
    """The golden ratchet's own substrate can now see a diagnostic-mode payload.

    Bug this closes (found in review): ``tools/snapshot_launch_payloads.build_spec``
    used to hardcode ``diagnostic_env=None`` no matter what ``cfg.diagnostic_mode``
    said, so ``capture_launch`` — the exact function every golden file in
    ``tests/providers/golden/launch_payloads/`` is generated from — could never
    produce a diagnostic-mode payload. That made the diagnostic path invisible to
    the ratchet and to every later compute-seam stage built on top of it. Driving
    a real ``capture_launch`` call with ``diagnostic_mode`` flipped on via
    ``mutate_cfg`` (the same seam ``test_field_consumption_parity.py`` uses for
    ``backend_options``) proves the gap is closed, without adding a shipped
    ``diagnostic_mode: true`` example config.
    """
    launch = capture_launch(
        Path(_CFG),
        mutate_cfg=_turn_on_diagnostic_mode,
    )
    env = {e["key"]: e["value"] for e in launch.payload["input"]["env"]}
    assert env["KINOFORGE_DIAG_BUCKET"] == "<DIAG_BUCKET>"
    assert env["KINOFORGE_DIAG_PREFIX"] == f"boot-logs/{GOLDEN_RUN_ID}"
    assert env["AWS_DEFAULT_REGION"] == "us-west-2"
    assert env["AWS_ACCESS_KEY_ID"] == STUB_SECRET
    assert env["AWS_SECRET_ACCESS_KEY"] == STUB_SECRET


def test_capture_launch_default_mode_still_omits_diagnostic_overlay() -> None:
    """Sanity check for the harness change: an unmutated capture stays clean.

    Bug caught: a ``_diagnostic_env`` that returns a non-empty dict even when
    ``cfg.diagnostic_mode`` is ``False`` would silently inject diagnostic keys
    into every one of the 31 non-diagnostic goldens — this is the test that
    would have caught that before a golden regen was even attempted.
    """
    launch = capture_launch(Path(_CFG))
    env = {e["key"]: e["value"] for e in launch.payload["input"]["env"]}
    assert "KINOFORGE_DIAG_BUCKET" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
