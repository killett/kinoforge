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


def test_helper_authorization_via_indirect_expansion() -> None:
    """Helper takes the token env var NAME as arg 4 and looks it up via `${!token_env}`."""
    script = _render()
    # The env var NAME is passed at the call site; the VALUE is looked up
    # inside the helper via bash indirect expansion (`${!token_env}`) so no
    # plaintext token VALUE ever appears in the script body.
    assert "local token_env=${4:-}" in script
    assert "${!token_env" in script
    assert "Authorization: Bearer ${!token_env}" in script


def test_helper_present_in_slim_mode_too() -> None:
    """Helper is unconditional — pre-baked image path also needs retry."""
    cfg = _min_cfg()
    cfg["engine"]["comfyui"]["image"] = "kinoforge/wan-comfyui:v0.3.10-088128b2-cu124"
    script = _render(cfg)
    assert "_kinoforge_download() {" in script


def _cfg_with_one_model() -> dict[str, Any]:
    # HuggingFaceSource self-registers on import; force-import here so the
    # resolver finds the `hf:` adapter for the model ref below.
    import kinoforge.sources.huggingface  # noqa: F401

    return {
        "engine": {
            "comfyui": {
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            },
        },
        "models": [
            {
                "src": "hf:Kijai/WanVideo_comfy:Wan2_1-T2V-14B_fp8_e4m3fn.safetensors",
                "target": "diffusion_models",
            },
        ],
    }


def test_model_loop_uses_helper_not_inline_curl() -> None:
    """C28 C2: model-download line emits `_kinoforge_download`, not inline curl.

    The C1 helper body itself contains `curl -L --fail` (that is the retried
    fetch). What must be GONE is the model-loop's INLINE call — historically
    a line of shape `[ ! -f ... ] && curl -L --fail ...`. The helper body
    line is recognisable by `if curl -L --fail`; any OTHER `curl -L --fail`
    line is the regression we want to catch.
    """
    script = _render(_cfg_with_one_model())
    bare_inline = [
        ln
        for ln in script.splitlines()
        if "curl -L --fail" in ln
        and "_kinoforge_download" not in ln
        and "if curl -L --fail" not in ln
    ]
    assert not bare_inline, (
        f"inline curl call still present in model-download loop: {bare_inline!r}"
    )
    assert "_kinoforge_download '" in script


def test_model_loop_call_shape_is_four_args() -> None:
    """Call shape: `_kinoforge_download '<url>' '<out>' '<sha>' '<token_env_name>'`."""
    cfg = _cfg_with_one_model()
    script = _render(cfg)
    call_lines = [ln for ln in script.splitlines() if "_kinoforge_download '" in ln]
    assert call_lines, "expected at least one _kinoforge_download call line"
    for ln in call_lines:
        body = ln.split("_kinoforge_download ", 1)[1]
        parts = body.split("'")
        # `'<url>' '<out>' '<sha>' '<token>'` → split('\'') yields 9 segments
        assert len(parts) == 9, f"malformed helper call: {ln!r}"


def test_model_loop_passes_token_env_name_for_hf_source() -> None:
    """HF source produces an Authorization header → call site carries 'HF_TOKEN'."""
    cfg = _cfg_with_one_model()
    script = _render(cfg)
    call_lines = [ln for ln in script.splitlines() if "_kinoforge_download '" in ln]
    assert any(ln.endswith("'HF_TOKEN'") for ln in call_lines), (
        f"expected the HF call line to end with the env-var-name arg "
        f"'HF_TOKEN', got: {call_lines!r}"
    )
