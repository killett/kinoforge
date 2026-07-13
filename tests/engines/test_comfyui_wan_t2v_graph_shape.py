"""Offline graph-shape assertions for runpod-comfyui-wan-t2v.graph.json.

Locks down the structural diff vs the i2v graph it was derived from:
no image-input nodes, has WanVideoEmptyEmbeds for text-only sampler
conditioning, model checkpoint is the T2V fp8 weight. Runs at unit-test
speed (no network, no ComfyUI) so a typo in the graph trips before
~$0.30 of RunPod spend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

GRAPH_PATH = Path("examples/configs/runpod-comfyui-wan-t2v.graph.json")


@pytest.fixture(scope="module")
def graph() -> dict[str, dict[str, Any]]:
    """Parse the t2v API graph once.

    Returns:
        The deserialised graph dict (excluding the `_meta` header).
    """
    raw = json.loads(GRAPH_PATH.read_text())
    return {k: v for k, v in raw.items() if k != "_meta"}


def test_graph_file_exists_and_parses() -> None:
    """Graph file is on disk and is valid JSON."""
    assert GRAPH_PATH.exists(), f"graph missing: {GRAPH_PATH}"
    json.loads(GRAPH_PATH.read_text())


def test_no_image_input_nodes(graph: dict[str, dict[str, Any]]) -> None:
    """T2V must NOT carry the i2v-only image-input pipeline.

    A regression that left LoadImage in would re-introduce the i2v
    asset_node_ids contract and break t2v's text-only call shape.
    """
    forbidden = {
        "LoadImage",
        "CLIPVisionLoader",
        "WanVideoImageToVideoEncode",
        "WanVideoClipVisionEncode",
        "ImageResizeKJv2",
    }
    present = {node["class_type"] for node in graph.values()}
    leaked = forbidden & present
    assert not leaked, f"t2v graph contains i2v-only node classes: {leaked}"


def test_has_empty_embeds_node(graph: dict[str, dict[str, Any]]) -> None:
    """WanVideoEmptyEmbeds must be present (provides image_embeds for t2v)."""
    empties = [
        nid for nid, n in graph.items() if n["class_type"] == "WanVideoEmptyEmbeds"
    ]
    assert len(empties) == 1, (
        f"expected exactly 1 WanVideoEmptyEmbeds node; got {len(empties)}: {empties}"
    )


def test_sampler_image_embeds_points_at_empty_embeds(
    graph: dict[str, dict[str, Any]],
) -> None:
    """WanVideoSampler.image_embeds must consume the EmptyEmbeds output.

    If the rewiring missed the sampler, ComfyUI would error mid-prompt
    with a missing-input fault inside the kijai node.
    """
    samplers = [n for n in graph.values() if n["class_type"] == "WanVideoSampler"]
    assert len(samplers) == 1, "expected exactly 1 WanVideoSampler"
    image_embeds_ref = samplers[0]["inputs"]["image_embeds"]
    assert isinstance(image_embeds_ref, list) and len(image_embeds_ref) == 2, (
        f"sampler.image_embeds must be [node_id, slot]; got {image_embeds_ref!r}"
    )
    upstream_id = image_embeds_ref[0]
    upstream = graph[upstream_id]
    assert upstream["class_type"] == "WanVideoEmptyEmbeds", (
        f"sampler.image_embeds upstream is {upstream['class_type']}, "
        f"want WanVideoEmptyEmbeds"
    )


def test_model_checkpoint_is_t2v(graph: dict[str, dict[str, Any]]) -> None:
    """The diffusion checkpoint must be the T2V weight, not the I2V one."""
    loaders = [n for n in graph.values() if n["class_type"] == "WanVideoModelLoader"]
    assert len(loaders) == 1, "expected exactly 1 WanVideoModelLoader"
    model_name = loaders[0]["inputs"]["model"]
    assert model_name == "Wan2_1-T2V-14B_fp8_e4m3fn.safetensors", (
        f"unexpected diffusion checkpoint: {model_name!r}"
    )


def test_empty_embeds_shape_matches_params(
    graph: dict[str, dict[str, Any]],
) -> None:
    """EmptyEmbeds widget defaults match the YAML params block.

    If the graph and YAML disagree on width/height/num_frames, the
    sampler runs at the EmptyEmbeds shape and the YAML lies. Keep them
    in sync — a runtime override path can come later if needed.
    """
    import yaml

    yaml_cfg = yaml.safe_load(
        Path("examples/configs/runpod-comfyui-wan-2_1-14b-t2v.yaml").read_text()
    )
    expected = {
        "width": yaml_cfg["params"]["width"],
        "height": yaml_cfg["params"]["height"],
        "num_frames": yaml_cfg["params"]["num_frames"],
    }
    empties = [n for n in graph.values() if n["class_type"] == "WanVideoEmptyEmbeds"]
    got = {k: empties[0]["inputs"][k] for k in expected}
    assert got == expected, f"EmptyEmbeds shape {got!r} != YAML params {expected!r}"
