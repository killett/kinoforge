"""Snapshot + parametrised tests for ComfyUIEngine.render_provision."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.comfyui import ComfyUIEngine


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "custom_nodes": [],
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            }
        },
        "models": [],
    }


def test_render_provision_returns_rendered_provision() -> None:
    """Sanity — engine emits a RenderedProvision."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    """When cfg doesn't override image, default is the stock RunPod image."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    """cfg.engine.comfyui.image overrides the default."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["image"] = "custom/image:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "custom/image:v1"


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    """Script must fail-fast — set -euo pipefail at the top."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_clones_comfyui_with_guard() -> None:
    """Script clones default repo with idempotency guard."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "[ ! -d ComfyUI ] && git clone --depth 1" in rp.script
    assert "https://github.com/comfyanonymous/ComfyUI" in rp.script


def test_render_provision_respects_repo_branch_override() -> None:
    """cfg.engine.comfyui.repo + branch flow into clone line."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["repo"] = "https://github.com/forky/ComfyUI"
    cfg["engine"]["comfyui"]["branch"] = "experimental"
    rp = _make_engine().render_provision(cfg)
    assert "https://github.com/forky/ComfyUI" in rp.script
    assert "--branch experimental" in rp.script


def test_render_provision_runs_comfyui_requirements_install() -> None:
    """Script installs ComfyUI's own requirements.txt."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "pip install -q -r requirements.txt" in rp.script


def test_render_provision_custom_node_without_ref_uses_shallow_clone() -> None:
    """Without ref, shallow clone — fast + small disk."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {"git": "https://github.com/kijai/ComfyUI-KJNodes"}
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes"
    ) in rp.script
    assert (
        "[ -f custom_nodes/ComfyUI-KJNodes/requirements.txt ] && "
        "pip install -q -r custom_nodes/ComfyUI-KJNodes/requirements.txt || true"
    ) in rp.script


def test_render_provision_custom_node_with_ref_uses_full_clone_and_checkout() -> None:
    """With ref, full clone + git checkout for SHA pinning (Layer P T2 contract)."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {
            "git": "https://github.com/kijai/ComfyUI-KJNodes",
            "ref": "abc123def456",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes && "
        "cd custom_nodes/ComfyUI-KJNodes && git checkout abc123def456 && cd ../.."
    ) in rp.script


def test_render_provision_hf_model_with_auth_header_and_env_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF model emits curl with $HF_TOKEN header AND env_required includes HF_TOKEN."""
    # HuggingFaceSource self-registers on import; force-import.
    import kinoforge.sources.huggingface  # noqa: F401

    cfg = _minimal_cfg()
    cfg["models"] = [
        {
            "src": "hf:Kijai/WanVideo_comfy:wan2.1.safetensors",
            "target": "checkpoints",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert "HF_TOKEN" in rp.env_required
    # C28 C2: the bearer header lives inside the _kinoforge_download helper
    # via bash indirect expansion (`${!token_env}`). The model loop passes
    # the env-var NAME as the 4th arg so the helper looks the value up at
    # runtime. The literal `Bearer $HF_TOKEN` no longer appears in the
    # script body — verifying the indirect-expansion shape + the call-site
    # arg are equivalent assertions of "HF_TOKEN is wired".
    assert "Authorization: Bearer $token_val" in rp.script
    assert "_kinoforge_download " in rp.script
    assert rp.script.rstrip().count("'HF_TOKEN'") >= 1
    assert "[ ! -f models/checkpoints/wan2.1.safetensors ]" in rp.script
    assert "models/checkpoints/wan2.1.safetensors" in rp.script


def test_render_provision_no_models_means_no_env_required() -> None:
    """Empty models list yields empty env_required."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []


def test_render_provision_script_ends_with_exec_run_cmd() -> None:
    """Script's final line is exec python main.py … so it becomes PID 1."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith(
        "exec python main.py --listen 0.0.0.0 --port 8188"
    )


def test_render_provision_run_cmd_matches_launch_args() -> None:
    """run_cmd mirrors the launch_args list with python main.py prefix."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]


def test_render_provision_port_parsed_from_launch_args() -> None:
    """--port arg in launch_args is reflected on ports."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0", "--port", "9999"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8188_when_absent() -> None:
    """When --port not in launch_args, default 8188."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8188"]


def test_render_provision_civitai_model_stubs_resolved_url_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CivitAI path: source.resolve hits stubbed fetch, returns artifact with Bearer $CIVITAI_TOKEN."""
    import kinoforge.sources.civitai as civitai_mod
    from kinoforge.core.interfaces import Artifact

    # Stub the CivitAI source's resolve to skip live HTTP.
    def _fake_resolve(self, ref, creds):  # noqa: ANN001
        return [
            Artifact(
                filename="civitai_model.safetensors",
                url="https://civitai.com/api/download/models/123",
                headers={"Authorization": "Bearer $CIVITAI_TOKEN"},
            )
        ]

    monkeypatch.setattr(civitai_mod.CivitAISource, "resolve", _fake_resolve)
    import kinoforge.sources.civitai  # noqa: F401

    cfg = _minimal_cfg()
    cfg["models"] = [
        {
            "src": "civitai:123",
            "target": "checkpoints",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert "CIVITAI_TOKEN" in rp.env_required
    # C28 C2: bearer header via `${!token_env}` inside the helper; the
    # CIVITAI_TOKEN env var NAME is passed as the 4th arg of the call site.
    assert "Authorization: Bearer $token_val" in rp.script
    assert rp.script.rstrip().count("'CIVITAI_TOKEN'") >= 1
    assert "models/checkpoints/civitai_model.safetensors" in rp.script


def test_render_provision_launches_selfterm_watchdog_before_clone() -> None:
    """Bug it catches: render_provision injects KINOFORGE_SELFTERM_SCRIPT into
    the env (via RunPodProvider.create_instance) but never starts the watchdog
    inside the pod. Without the launch, all three pod-side cost guards
    (max-lifetime, dead-man heartbeat, job_timeout) sit in env as dead config
    and a leaked pod billed ~3h after a controller crash before manual REST
    DELETE intervention. Lockdown pins the write-to-tmp + nohup launch lines
    AND requires them to appear before the first git clone, since the clone
    can hang and is exactly the phase where the watchdog must already be
    alive.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    # Required substrings — write-to-tmp + detached background launch.
    assert "open('/tmp/selfterm.py','w')" in script
    assert "os.environ['KINOFORGE_SELFTERM_SCRIPT']" in script
    assert "nohup python3 /tmp/selfterm.py" in script
    # Ordering: selfterm-launch line MUST precede first ComfyUI git clone.
    selfterm_idx = script.index("nohup python3 /tmp/selfterm.py")
    clone_idx = script.index("git clone --depth 1 --branch")
    assert selfterm_idx < clone_idx, (
        "selfterm watchdog must launch before git clone — clone phase can hang"
    )


def test_render_provision_selfterm_launch_is_optional_when_env_unset() -> None:
    """Bug it catches: a launch line that fails-hard (set -e + missing env var
    after 'set -euo pipefail') when KINOFORGE_SELFTERM_SCRIPT isn't injected
    (e.g. LocalProvider runs, or a non-selfterm provider). The launch line
    must guard with `if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then ... fi`
    so the bootstrap stays portable.
    """
    rp = _make_engine().render_provision(_minimal_cfg())
    script = rp.script
    assert 'if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}" ]; then' in script
    # Guard wraps the launch — both the python3 -c writer and the nohup
    # launch must be inside the if-fi block.
    if_start = script.index('if [ -n "${KINOFORGE_SELFTERM_SCRIPT:-}"')
    fi_end = script.index("fi", if_start)
    launch_pos = script.index("nohup python3 /tmp/selfterm.py")
    assert if_start < launch_pos < fi_end
