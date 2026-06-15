"""Unit tests for C30 A2-A6 provision-line constants (walk-down rungs)."""

from __future__ import annotations

from kinoforge.diagnostics.c30_probe import (
    _C28_PHASE_A_CUSTOM_NODES,
    _C28_PHASE_A_MODELS,
    _KINOFORGE_DOWNLOAD_HELPER_LINES,
    PROVISION_A2_LINES,
    PROVISION_A3_LINES,
    PROVISION_A4_LINES,
    PROVISION_A5_LINES,
    PROVISION_A6_LINES,
)


def _non_sleep(lines: list[str]) -> list[str]:
    return [line for line in lines if line != "sleep 600"]


def test_all_constants_are_non_empty_lists() -> None:
    for rung in (
        PROVISION_A2_LINES,
        PROVISION_A3_LINES,
        PROVISION_A4_LINES,
        PROVISION_A5_LINES,
        PROVISION_A6_LINES,
    ):
        assert isinstance(rung, list)
        assert rung, "rung must be non-empty"
        assert all(isinstance(line, str) for line in rung)


def test_a2_is_minimal_sleep() -> None:
    """A2 = stock pod, cd, sleep. Provision pre-amble alone after trap."""
    assert PROVISION_A2_LINES[-1] == "sleep 600"
    assert any(line.startswith("cd ") for line in PROVISION_A2_LINES)
    # A2 must NOT clone, pip-install, or download anything.
    joined = "\n".join(PROVISION_A2_LINES)
    assert "git clone" not in joined
    assert "pip install" not in joined
    assert "_kinoforge_download" not in joined


def test_a3_extends_a2_with_clone() -> None:
    """A3 = A2 (sans final sleep) + ComfyUI clone + sleep."""
    a2_pre = _non_sleep(PROVISION_A2_LINES)
    a3_pre = _non_sleep(PROVISION_A3_LINES)
    for line in a2_pre:
        assert line in a3_pre, f"A3 missing A2 line: {line!r}"
    joined = "\n".join(PROVISION_A3_LINES)
    assert "git clone --depth 1 --branch master" in joined
    assert "comfyanonymous/ComfyUI" in joined
    assert "pip install" not in joined
    assert PROVISION_A3_LINES[-1] == "sleep 600"


def test_a4_extends_a3_with_pip_install() -> None:
    """A4 = A3 (sans sleep) + requirements pip install + sleep."""
    a3_pre = _non_sleep(PROVISION_A3_LINES)
    a4_pre = _non_sleep(PROVISION_A4_LINES)
    for line in a3_pre:
        assert line in a4_pre
    joined = "\n".join(PROVISION_A4_LINES)
    assert "pip install -q -r requirements.txt" in joined
    # No custom-node clones yet.
    assert "custom_nodes/" not in joined
    assert PROVISION_A4_LINES[-1] == "sleep 600"


def test_a5_extends_a4_with_three_custom_nodes() -> None:
    """A5 = A4 (sans sleep) + all three Kijai/Kosinkadink nodes + sleep."""
    a4_pre = _non_sleep(PROVISION_A4_LINES)
    a5_pre = _non_sleep(PROVISION_A5_LINES)
    for line in a4_pre:
        assert line in a5_pre
    joined = "\n".join(PROVISION_A5_LINES)
    assert len(_C28_PHASE_A_CUSTOM_NODES) == 3
    for url, ref in _C28_PHASE_A_CUSTOM_NODES:
        assert url in joined
        assert ref in joined
        node_name = url.rstrip("/").rsplit("/", 1)[-1]
        assert f"custom_nodes/{node_name}" in joined
    assert "_kinoforge_download" not in joined  # no model downloads yet
    assert PROVISION_A5_LINES[-1] == "sleep 600"


def test_a6_replaces_sleep_with_exec_and_includes_helper_and_models() -> None:
    """A6 = download helper + A5 (sans sleep) + 3 model dl + exec python main.py."""
    a5_pre = _non_sleep(PROVISION_A5_LINES)
    a6 = PROVISION_A6_LINES
    # A6 must include the helper definition and all A5 work lines.
    for helper_line in _KINOFORGE_DOWNLOAD_HELPER_LINES:
        assert helper_line in a6
    for line in a5_pre:
        assert line in a6
    joined = "\n".join(a6)
    # Three model entries.
    assert len(_C28_PHASE_A_MODELS) == 3
    for url, subdir, filename in _C28_PHASE_A_MODELS:
        assert url in joined
        assert f"{subdir}/{filename}" in joined
    # HF_TOKEN env name passes through as the bearer-env arg.
    assert "HF_TOKEN" in joined
    # Final action is the ComfyUI launch, not a sleep.
    assert a6[-1].startswith("cd /workspace/ComfyUI && exec ")
    assert "python main.py" in a6[-1]
    assert "--listen 0.0.0.0" in a6[-1]
    assert "--port 8188" in a6[-1]
    # A6 has no trailing sleep.
    assert "sleep 600" not in a6


def test_models_resolve_to_hf_kijai_repo() -> None:
    for url, _subdir, _filename in _C28_PHASE_A_MODELS:
        assert url.startswith("https://huggingface.co/Kijai/WanVideo_comfy/")
