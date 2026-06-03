"""Lockdown tests for examples/configs/runpod-comfyui-wan.graph.json.

These tests assert the committed ComfyUI API-format graph is structurally
valid AND that the example YAML's asset_node_ids / prompt_node_ids dicts
reference node IDs that actually exist in the graph. Run offline — no pod.

The test_prompt_node_ids_is_dict_and_references_existing_nodes case also
locks the pre-existing list-vs-dict type bug in the YAML (prompt_node_ids
was ``["8"]`` — a list — but Layer J's ``engines.comfyui`` calls
``.items()`` on the value and would crash at runtime; T3 of item #3 flipped
it to ``{positive: "16"}``).

Source-of-truth graph is the API JSON emitted from kijai's
``wanvideo_2_1_14B_I2V_example_03.json`` UI workflow at pinned SHA
``088128b224242e110d3906c6750e9a3a348a659b`` by ``tools/comfyui_ui_to_api.py``.
Node count is 15 (Seth's converter drops non-runtime UI nodes such as
``Note`` and control-flow placeholders from the 26-node UI source) —
see PROGRESS sub-plan ``comfyui_ui_to_api`` T6 closure block.
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")

EXPECTED_NODE_COUNT = 15
EXPECTED_CLASS_TYPES = {
    "CLIPLoader",
    "CLIPTextEncode",
    "CLIPVisionLoader",
    "ImageResizeKJv2",
    "LoadImage",
    "LoadWanVideoT5TextEncoder",
    "Note",
    "VHS_VideoCombine",
    "WanVideoBlockSwap",
    "WanVideoClipVisionEncode",
    "WanVideoDecode",
    "WanVideoImageToVideoEncode",
    "WanVideoLoraSelect",
    "WanVideoModelLoader",
    "WanVideoSampler",
    "WanVideoSetBlockSwap",
    "WanVideoTextEmbedBridge",
    "WanVideoTextEncode",
    "WanVideoTorchCompileSettings",
    "WanVideoVAELoader",
    "WanVideoVRAMManagement",
}


def test_graph_shape_api_format() -> None:
    """Graph is a dict-of-dict; every value has class_type + inputs; node count == 15."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    assert isinstance(graph, dict), f"graph must be dict, got {type(graph)}"
    assert len(graph) == EXPECTED_NODE_COUNT, (
        f"expected {EXPECTED_NODE_COUNT} nodes, got {len(graph)}"
    )
    for node_id, node in graph.items():
        assert isinstance(node_id, str), f"node id {node_id!r} must be str"
        assert "class_type" in node, f"node {node_id!r} missing class_type"
        assert "inputs" in node, f"node {node_id!r} missing inputs"
        assert isinstance(node["inputs"], dict), (
            f"node {node_id!r} inputs must be dict, got {type(node['inputs'])}"
        )


def test_graph_class_types_within_expected_set() -> None:
    """Every class_type in the graph is in the expected kijai+core set."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    actual = {node["class_type"] for node in graph.values()}
    unexpected = actual - EXPECTED_CLASS_TYPES
    assert not unexpected, f"unexpected class_types in graph: {sorted(unexpected)}"


def test_asset_node_ids_reference_existing_nodes() -> None:
    """Every asset_node_ids[<role>] is a key in the graph dict."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    asset_node_ids = cfg.spec["asset_node_ids"]
    for role, node_id in asset_node_ids.items():
        assert node_id in graph, (
            f"asset role {role!r} -> node {node_id!r} not in graph "
            f"(graph keys: {sorted(graph.keys())})"
        )


def test_prompt_node_ids_is_dict_and_references_existing_nodes() -> None:
    """prompt_node_ids must be dict (Layer J), not list; every value in graph."""
    cfg = load_config(YAML_PATH)
    graph = cfg.spec["graph"]
    prompt_node_ids = cfg.spec["prompt_node_ids"]
    assert isinstance(prompt_node_ids, dict), (
        f"prompt_node_ids must be dict (Layer J expects .items()), "
        f"got {type(prompt_node_ids).__name__}: {prompt_node_ids!r}"
    )
    for role, node_id in prompt_node_ids.items():
        assert node_id in graph, (
            f"prompt role {role!r} -> node {node_id!r} not in graph "
            f"(graph keys: {sorted(graph.keys())})"
        )
