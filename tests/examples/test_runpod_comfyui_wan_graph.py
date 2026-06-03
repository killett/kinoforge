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
Node count is 14: Seth's converter drops non-runtime UI nodes such
as ``Note`` and control-flow placeholders from the 26-node UI source
(yielding 15), and the parent item #3 T6 wave additionally drops
node 69 (``WanVideoLoraSelect``) — the LoRA was a speed optimizer
whose widget mapping had drifted and whose underlying weight wasn't
in our model download list — see PROGRESS sub-plan
``comfyui_ui_to_api`` T6 closure block + the parent T6 bug-catch
trail for the graph regeneration details.
"""

from __future__ import annotations

import json
from pathlib import Path

from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")
GRAPH_PATH = Path("examples/configs/runpod-comfyui-wan.graph.json")
KIJAI_REPO_HINT = "kijai/ComfyUI-WanVideoWrapper"

EXPECTED_NODE_COUNT = 14
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
    """Graph is a dict-of-dict; every value has class_type + inputs; node count == 14."""
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


def test_kijai_sha_pin_cross_reference() -> None:
    """The kijai custom_nodes ref in the YAML must match _meta.source_sha in the graph file.

    Both are pinned at the same SHA to lock the workflow JSON to the
    custom_nodes implementation that produced it. Drift between them is
    silent at runtime (the graph still POSTs cleanly) but the workflow
    is only guaranteed to render correctly against the kijai nodes at
    the SHA the workflow was captured against — caught here.
    """
    raw_graph = json.loads(GRAPH_PATH.read_text())
    meta = raw_graph.get("_meta")
    assert meta is not None, (
        f"{GRAPH_PATH} missing _meta header (set by T1 of item #3 resume)"
    )
    graph_sha = meta.get("source_sha")
    assert isinstance(graph_sha, str) and len(graph_sha) == 40, (
        f"_meta.source_sha must be 40-char SHA, got {graph_sha!r}"
    )

    cfg = load_config(YAML_PATH)
    assert cfg.engine.comfyui is not None, "engine.comfyui block missing from YAML"
    custom_nodes = cfg.engine.comfyui.custom_nodes
    kijai_entries = [cn for cn in custom_nodes if KIJAI_REPO_HINT in cn.get("git", "")]
    assert len(kijai_entries) == 1, (
        f"expected exactly one kijai entry in custom_nodes, got "
        f"{[cn.get('git') for cn in custom_nodes]}"
    )
    yaml_sha = kijai_entries[0].get("ref")

    assert graph_sha == yaml_sha, (
        f"kijai SHA drift between graph _meta ({graph_sha!r}) and YAML "
        f"custom_nodes[kijai].ref ({yaml_sha!r}); rerun graph capture or "
        f"bump YAML"
    )


def test_yaml_env_required_locked_to_hf_token() -> None:
    """The example YAML must declare exactly HF_TOKEN as env_required.

    All three model entries pull from ``hf:Kijai/WanVideo_comfy:*`` refs;
    HuggingFaceSource attaches ``Authorization: Bearer $HF_TOKEN`` headers,
    which ``render_provision`` collects + dedupes into ``env_required``. Any
    future YAML edit that drops an HF-gated model OR adds a non-HF
    cred-bearing model would silently change this list; catch it here.
    """
    import kinoforge._adapters  # noqa: F401 — register HF/RunPod/ComfyUI adapters by side effect
    from kinoforge.engines.comfyui import ComfyUIEngine

    cfg = load_config(YAML_PATH)
    engine = ComfyUIEngine()
    rendered = engine.render_provision(cfg.model_dump())
    assert rendered.env_required == ["HF_TOKEN"], (
        f"YAML env_required drift: expected ['HF_TOKEN'], got {rendered.env_required!r}"
    )
