"""C28 A2 — ``render_provision`` EXIT trap pre-amble, gated on ``diagnostic_mode``.

The trap is pure-additive: when ``cfg.diagnostic_mode`` is falsy or absent,
the rendered script is byte-identical to the pre-C28 baseline. When True,
the script is prepended with a bash EXIT trap that captures stdout/stderr
to ``/tmp/boot.log`` and uploads a diagnostic snapshot to S3 on shell exit.

The trap MUST NOT echo secret material; AWS credentials are wired only as
process env vars consumed by ``aws s3 cp``.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.engines.comfyui import ComfyUIEngine


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "custom_nodes": [],
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            },
        },
        "models": [],
    }


@pytest.fixture
def engine() -> ComfyUIEngine:
    return _make_engine()


def test_no_diagnostic_mode_no_trap(engine: ComfyUIEngine) -> None:
    out = engine.render_provision(_minimal_cfg())
    assert "_kinoforge_diag_capture" not in out.script
    head_lines = out.script.splitlines()[:5]
    assert not any("trap " in ln for ln in head_lines)


def test_no_diagnostic_mode_script_byte_identical_to_baseline(
    engine: ComfyUIEngine,
) -> None:
    """Pure-additive: explicit ``diagnostic_mode: False`` matches the default."""
    out_default = engine.render_provision(_minimal_cfg())
    out_false = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": False})
    assert out_default.script == out_false.script


def test_diagnostic_mode_emits_trap_preamble(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    head = "\n".join(out.script.splitlines()[:25])
    assert "trap '_kinoforge_diag_capture $?' EXIT" in head
    assert "exec > >(tee -a /tmp/boot.log) 2>&1" in head


def test_trap_captures_required_sections(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    for marker in (
        "===== rc =====",
        "===== last_line =====",
        "===== nvidia-smi =====",
        "===== df -h =====",
        "===== free -m =====",
        "===== ls -la models/diffusion_models =====",
        "===== dpkg -l torch =====",
        "===== boot.log =====",
    ):
        assert marker in out.script, f"missing trap section: {marker}"


def test_trap_references_diag_env_vars(engine: ComfyUIEngine) -> None:
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    assert "${KINOFORGE_DIAG_BUCKET" in out.script
    assert "${KINOFORGE_DIAG_PREFIX" in out.script


def test_trap_never_echoes_access_key_names(engine: ComfyUIEngine) -> None:
    """Script body must not name AWS access-key env vars — they live in env only."""
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    assert "KINOFORGE_DIAG_ACCESS_KEY" not in out.script
    assert "KINOFORGE_DIAG_SECRET_KEY" not in out.script
    assert "AWS_ACCESS_KEY_ID" not in out.script
    assert "AWS_SECRET_ACCESS_KEY" not in out.script


def test_trap_preamble_pre_installs_awscli(engine: ComfyUIEngine) -> None:
    """`runpod/pytorch:2.4` base image lacks awscli; trap pre-installs it.

    Without this, the `aws s3 cp` line in the trap silently fails with
    `command not found` and `|| true` swallows ENOENT — leaving rc!=0
    with NO sidecar object to classify against (Phase A v1 Hn outcome).
    """
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    head_block = "\n".join(out.script.splitlines()[:6])
    assert "command -v aws" in head_block, (
        f"expected awscli presence-check in trap pre-amble head; head:\n{head_block}"
    )
    assert "pip install -q awscli" in head_block


def test_trap_aws_cp_swallows_errors(engine: ComfyUIEngine) -> None:
    """A failed PUT must NOT propagate; otherwise it overrides the real rc."""
    out = engine.render_provision({**_minimal_cfg(), "diagnostic_mode": True})
    cp_lines = [ln for ln in out.script.splitlines() if "aws s3 cp" in ln]
    assert cp_lines, "expected at least one `aws s3 cp` line in the trap"
    for ln in cp_lines:
        assert ln.rstrip().endswith("|| true"), (
            f"line missing `|| true` swallow: {ln!r}"
        )
