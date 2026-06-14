"""Lint the kinoforge/wan-comfyui Dockerfile structure.

Validates the static shape without running `docker build` (which would
require Docker on the test host). The actual build smoke runs in CI via
the workflow_dispatch action.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DF = Path("docker/wan-comfyui/Dockerfile")


@pytest.fixture
def dockerfile_body() -> str:
    return _DF.read_text()


def test_dockerfile_exists() -> None:
    assert _DF.is_file(), f"missing Dockerfile at {_DF}"


def test_dockerfile_pins_all_required_refs(dockerfile_body: str) -> None:
    for arg in ("COMFYUI_REF", "KIJAI_WAN_REF", "KJNODES_REF", "VHS_REF"):
        assert f"ARG {arg}" in dockerfile_body, f"missing ARG {arg}"


def test_dockerfile_uses_runpod_pytorch_base(dockerfile_body: str) -> None:
    first_from = next(
        ln for ln in dockerfile_body.splitlines() if ln.startswith("FROM ")
    )
    assert first_from.startswith("FROM runpod/pytorch:2.4.0"), first_from


def test_dockerfile_pre_installs_awscli_for_diagnostic_trap(
    dockerfile_body: str,
) -> None:
    """C28 A2 trap PUTs via `aws s3 cp`; base image lacks the CLI."""
    assert "pip install --no-cache-dir awscli" in dockerfile_body, (
        "trap needs awscli pre-installed; base image runpod/pytorch:2.4.0 "
        "does not ship it (root cause of Phase A NO_REPRODUCTION outcome)"
    )


def test_dockerfile_has_build_time_import_smoke(dockerfile_body: str) -> None:
    """A broken pip combo must fail `docker build`, not pod boot."""
    assert 'python -c "import sys; sys.path.insert(0,' in dockerfile_body
    assert "import comfy" in dockerfile_body


def test_dockerfile_stamps_image_tag_env(dockerfile_body: str) -> None:
    """KINOFORGE_IMAGE_TAG lets the Phase B smoke verify the slim image booted."""
    assert "ARG IMAGE_TAG" in dockerfile_body
    assert "ENV KINOFORGE_IMAGE_TAG=" in dockerfile_body


def test_dockerfile_clones_all_four_custom_nodes(dockerfile_body: str) -> None:
    """Custom-node refs must be applied via `git checkout` at the pinned SHA."""
    nodes = (
        "ComfyUI-WanVideoWrapper",
        "ComfyUI-KJNodes",
        "ComfyUI-VideoHelperSuite",
    )
    for node in nodes:
        assert node in dockerfile_body, f"missing custom-node clone for {node}"
    for ref_var in ("KIJAI_WAN_REF", "KJNODES_REF", "VHS_REF"):
        assert f"git checkout ${{{ref_var}}}" in dockerfile_body, (
            f"custom-node missing `git checkout ${{{ref_var}}}` step"
        )
