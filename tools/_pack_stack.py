"""Pure pack-stack hash for kinoforge ComfyUI workflows.

Used by tools/comfyui_ui_to_api.py + tools/capture_object_info.py to
derive the canonical fixture path tests/fixtures/comfyui/object_info/
<pack-stack-hash>.json from a workflow YAML's engine.comfyui block.
"""

from __future__ import annotations

import hashlib
from typing import Any


def pack_stack_hash(comfyui_cfg: dict[str, Any]) -> str:
    """Deterministic 12-char hash of the engine.comfyui pack stack.

    Computed from:
      - engine.comfyui.version (e.g. "0.3.10") — the ComfyUI base version
      - engine.comfyui.custom_nodes[*].git + .ref, sorted by git URL
        ascending

    Identical pack stacks across workflows produce identical hashes
    → fixture sharing under
    tests/fixtures/comfyui/object_info/<hash>.json.

    Args:
        comfyui_cfg: The engine.comfyui pydantic-dump dict (or
            equivalent dict-shaped object). Read keys: "version" (str),
            "custom_nodes" (list of dicts with "git"/"ref" keys).

    Returns:
        12-character lowercase hex string (SHA256 prefix).
    """
    version = comfyui_cfg.get("version", "")
    nodes = comfyui_cfg.get("custom_nodes", []) or []
    parts = [f"comfyui@{version}"]
    for n in sorted(nodes, key=lambda x: x.get("git", "")):
        parts.append(f"{n.get('git', '')}@{n.get('ref', '')}")
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
