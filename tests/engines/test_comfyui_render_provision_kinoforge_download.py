"""C28 C1 — ``_kinoforge_download`` helper rendered into the provision script.

Pure-bash function with 3-attempt retry, exponential backoff (5/10/15 s),
sha256 verification (optional), partial-file cleanup between attempts, and
optional Authorization-bearer header derived from ``$HF_TOKEN``. Helper is
unconditional — emitted into every render_provision script regardless of
slim-mode, diagnostic_mode, or which image the cfg selects — because the
spec wants the curl-retry path to be available for ANY download the boot
script issues.
"""

from __future__ import annotations

from typing import Any

from kinoforge.engines.comfyui import ComfyUIEngine


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _min_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            },
        },
        "models": [],
    }


def _render(cfg: dict[str, Any] | None = None) -> str:
    return _make_engine().render_provision(cfg or _min_cfg()).script


def test_helper_present() -> None:
    assert "_kinoforge_download() {" in _render()


def test_helper_three_attempts() -> None:
    script = _render()
    assert "for attempt in 1 2 3" in script


def test_helper_exponential_backoff() -> None:
    script = _render()
    assert "sleep $((5 * attempt))" in script


def test_helper_cleans_partial_between_attempts() -> None:
    script = _render()
    assert 'rm -f "${out}.partial"' in script


def test_helper_sha_verify_branch() -> None:
    script = _render()
    assert "sha256sum" in script
    assert "$actual" in script
    assert "$expected_sha" in script


def test_helper_authorization_optional() -> None:
    """`${HF_TOKEN:+...}` expansion form so absence skips the bearer header."""
    script = _render()
    assert '${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"}' in script


def test_helper_present_in_slim_mode_too() -> None:
    """Helper is unconditional — pre-baked image path also needs retry."""
    cfg = _min_cfg()
    cfg["engine"]["comfyui"]["image"] = "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"
    script = _render(cfg)
    assert "_kinoforge_download() {" in script
