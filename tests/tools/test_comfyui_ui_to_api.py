"""Lockdown tests for tools.comfyui_ui_to_api shim."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES_DIR = Path("tests/tools/fixtures")
MINIMAL_OBJECT_INFO = FIXTURES_DIR / "minimal_object_info.json"


@pytest.fixture(autouse=True)
def _restore_sys_modules() -> Iterator[None]:
    """Each test gets a fresh sys.modules['nodes'] state."""
    saved = sys.modules.pop("nodes", None)
    yield
    sys.modules.pop("nodes", None)
    if saved is not None:
        sys.modules["nodes"] = saved


def test_install_fake_nodes_module_populates_NODE_CLASS_MAPPINGS() -> None:
    """install_fake_nodes_module makes nodes.NODE_CLASS_MAPPINGS available."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    install_fake_nodes_module(MINIMAL_OBJECT_INFO)
    assert "nodes" in sys.modules
    mappings = sys.modules["nodes"].NODE_CLASS_MAPPINGS
    assert set(mappings.keys()) == {"LoadImage", "CLIPTextEncode", "Note", "SaveImage"}


def test_INPUT_TYPES_returns_classmethod_with_correct_required_section() -> None:
    """INPUT_TYPES() returns the captured schema with outer tuples + inner lists preserved."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    install_fake_nodes_module(MINIMAL_OBJECT_INFO)
    cls = sys.modules["nodes"].NODE_CLASS_MAPPINGS["LoadImage"]
    schema = cls.INPUT_TYPES()
    assert "required" in schema
    image_spec = schema["required"]["image"]
    # Outer wrapper is a tuple (per ComfyUI runtime convention).
    assert isinstance(image_spec, tuple), (
        f"outer wrapper must be tuple, got {type(image_spec)}"
    )
    # Inner first element (COMBO option list) preserved as list.
    assert isinstance(image_spec[0], list), (
        f"inner COMBO options must stay list, got {type(image_spec[0])}"
    )
    assert image_spec[0] == ["a.png", "b.png"]


def test_input_order_is_respected(tmp_path: Path) -> None:
    """input_order overrides input dict insertion order in INPUT_TYPES output."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    fixture = tmp_path / "ordered.json"
    fixture.write_text(
        json.dumps(
            {
                "Reordered": {
                    "input": {
                        "required": {
                            "z_param": ["STRING", {}],
                            "a_param": ["INT", {}],
                        }
                    },
                    "input_order": {"required": ["a_param", "z_param"]},
                }
            }
        )
    )
    install_fake_nodes_module(fixture)
    cls = sys.modules["nodes"].NODE_CLASS_MAPPINGS["Reordered"]
    keys = list(cls.INPUT_TYPES()["required"].keys())
    assert keys == ["a_param", "z_param"], (
        f"input_order ignored: got {keys}, expected ['a_param', 'z_param']"
    )


def test_idempotent_install_replaces_cleanly(tmp_path: Path) -> None:
    """Calling install twice replaces sys.modules['nodes'] with the second fixture."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    fixture_b = tmp_path / "second.json"
    fixture_b.write_text(json.dumps({"OnlyB": {"input": {}, "output": ["X"]}}))

    install_fake_nodes_module(MINIMAL_OBJECT_INFO)
    assert "LoadImage" in sys.modules["nodes"].NODE_CLASS_MAPPINGS

    install_fake_nodes_module(fixture_b)
    assert "LoadImage" not in sys.modules["nodes"].NODE_CLASS_MAPPINGS
    assert "OnlyB" in sys.modules["nodes"].NODE_CLASS_MAPPINGS


def test_missing_optional_fields_default_safely(tmp_path: Path) -> None:
    """Classes with no output/output_name/output_node default safely."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    fixture = tmp_path / "bare.json"
    fixture.write_text(json.dumps({"Bare": {"input": {}}}))

    install_fake_nodes_module(fixture)
    cls = sys.modules["nodes"].NODE_CLASS_MAPPINGS["Bare"]
    assert cls.RETURN_TYPES == (), f"expected () got {cls.RETURN_TYPES!r}"
    assert cls.RETURN_NAMES == (), f"expected () got {cls.RETURN_NAMES!r}"
    assert cls.OUTPUT_NODE is False, f"expected False got {cls.OUTPUT_NODE!r}"
    assert cls.FUNCTION == "run", f"expected 'run' got {cls.FUNCTION!r}"


def test_cli_main_writes_api_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end CLI: synthetic UI JSON + minimal_object_info -> API JSON.

    Uses a LoadImage -> SaveImage edge so Seth's converter retains both nodes
    (LoadImage has a connected output; SaveImage is an OUTPUT_NODE).
    """
    from tools.comfyui_ui_to_api import main

    ui_workflow = {
        "id": "test-workflow",
        "revision": 1,
        "last_node_id": 2,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "LoadImage",
                "pos": [0, 0],
                "size": [200, 100],
                "flags": {},
                "order": 0,
                "mode": 0,
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
                "properties": {},
                "widgets_values": ["a.png"],
            },
            {
                "id": 2,
                "type": "SaveImage",
                "pos": [300, 0],
                "size": [200, 100],
                "flags": {},
                "order": 1,
                "mode": 0,
                "inputs": [
                    {"name": "images", "type": "IMAGE", "link": 1},
                ],
                "outputs": [],
                "properties": {},
                "widgets_values": ["ComfyUI"],
            },
        ],
        # Link format: [link_id, source_id, source_slot, target_id, target_slot, type]
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }

    ui_path = tmp_path / "ui.json"
    out_path = tmp_path / "out.json"
    ui_path.write_text(json.dumps(ui_workflow))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comfyui_ui_to_api",
            "--ui-json",
            str(ui_path),
            "--object-info",
            str(MINIMAL_OBJECT_INFO),
            "--out",
            str(out_path),
        ],
    )
    rc = main()
    assert rc == 0
    api = json.loads(out_path.read_text())
    # API format: dict keyed by stringified node IDs.
    assert isinstance(api, dict)
    assert "1" in api, f"LoadImage missing from API output: {list(api.keys())}"
    assert api["1"]["class_type"] == "LoadImage"
    assert "2" in api, f"SaveImage missing from API output: {list(api.keys())}"
    assert api["2"]["class_type"] == "SaveImage"
