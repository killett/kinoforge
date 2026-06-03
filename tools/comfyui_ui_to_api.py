r"""ComfyUI UI-format -> API-format JSON converter (offline shim).

Vendors tools/_vendored/seth_workflow_converter.py (Unlicense, see
LICENSES/SETH-UNLICENSE.txt). Decouples Seth's converter from a live
ComfyUI server by injecting a fake ``nodes`` module backed by a captured
/object_info JSON before importing the vendored module.

Usage:
    python tools/comfyui_ui_to_api.py \
        --ui-json <input.json> \
        --object-info <captured-object-info.json> \
        --out <output-api.json>
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from typing import Any


def install_fake_nodes_module(object_info_path: Path) -> types.ModuleType:
    """Inject a fake ``nodes`` module into sys.modules from a captured /object_info.

    MUST be called BEFORE importing tools._vendored.seth_workflow_converter;
    that module's top-level ``import nodes`` resolves against sys.modules.

    Args:
        object_info_path: Path to a captured /object_info JSON file.

    Returns:
        The fake module (returned for test introspection).
    """
    raw: dict[str, dict[str, Any]] = json.loads(
        object_info_path.read_text(encoding="utf-8")
    )

    mappings: dict[str, type] = {}
    display_names: dict[str, str] = {}

    for class_type, info in raw.items():
        input_block = info.get("input", {}) or {}
        input_order = info.get("input_order", {}) or {}

        ordered: dict[str, dict[str, Any]] = {}
        for section in ("required", "optional", "hidden"):
            section_block = input_block.get(section, {}) or {}
            keys = input_order.get(section, list(section_block.keys()))
            ordered_section: dict[str, Any] = {}
            for key in keys:
                if key in section_block:
                    val = section_block[key]
                    if isinstance(val, list):
                        val = tuple(val)
                    ordered_section[key] = val
            if ordered_section:
                ordered[section] = ordered_section

        # Closure capture via default arg -- avoids Python late-binding bug.
        stub = type(
            class_type,
            (),
            {
                "INPUT_TYPES": classmethod(lambda _cls, _s=ordered: _s),
                "RETURN_TYPES": tuple(info.get("output", [])),
                "RETURN_NAMES": tuple(info.get("output_name", info.get("output", []))),
                "FUNCTION": info.get("function", "run"),
                "CATEGORY": info.get("category", "uncategorized"),
                "OUTPUT_NODE": info.get("output_node", False),
            },
        )
        mappings[class_type] = stub
        display_names[class_type] = info.get("display_name", class_type)

    fake = types.ModuleType("nodes")
    fake.NODE_CLASS_MAPPINGS = mappings  # type: ignore[attr-defined]
    fake.NODE_DISPLAY_NAME_MAPPINGS = display_names  # type: ignore[attr-defined]
    sys.modules["nodes"] = fake
    return fake


def main() -> int:
    """CLI entrypoint: parse args, install fake nodes, run converter, write JSON."""
    parser = argparse.ArgumentParser(
        prog="comfyui_ui_to_api",
        description=(
            "Convert ComfyUI UI-format workflow JSON to API-format JSON, "
            "offline, using a captured /object_info dump."
        ),
    )
    parser.add_argument(
        "--ui-json",
        required=True,
        type=Path,
        help="Input UI-format workflow JSON.",
    )
    parser.add_argument(
        "--object-info",
        required=True,
        type=Path,
        help="Captured /object_info JSON for the workflow's pack-stack.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output API-format JSON path.",
    )
    args = parser.parse_args()

    install_fake_nodes_module(args.object_info)
    # Import AFTER nodes-module injection.
    from tools._vendored.seth_workflow_converter import WorkflowConverter

    ui_workflow = json.loads(args.ui_json.read_text(encoding="utf-8"))
    converter = WorkflowConverter()
    api_workflow = converter.convert_to_api(ui_workflow)

    args.out.write_text(
        json.dumps(api_workflow, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
