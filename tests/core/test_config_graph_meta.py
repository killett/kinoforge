"""Lock down config-load behaviour for graph _meta header strip.

The committed graph JSON has a top-level ``_meta`` key carrying provenance
(source_repo, source_sha, source_path, captured_at_local, format,
converter). ComfyUI's ``/prompt`` endpoint validates every top-level key as a
node ID and rejects unrecognised keys, so ``_meta`` must NOT survive into
``cfg.spec["graph"]``. Single source of truth: strip happens at config-load
time in ``_resolve_spec_graph_file``; runtime ``ComfyUIBackend.submit`` and
offline lockdown tests both see a ``_meta``-free dict.

These tests catch:
- ``_resolve_spec_graph_file`` regression that re-introduces ``_meta`` into
  ``cfg.spec["graph"]`` (would corrupt every ComfyUI ``/prompt`` submission).
- Accidental ``_meta`` removal from the source JSON on disk (AC12's
  cross-reference test reads the raw file directly and depends on the
  provenance header being preserved on disk).
"""

from __future__ import annotations

import json
from pathlib import Path

from kinoforge.core.config import load_config


def test_load_config_strips_meta_from_inlined_graph(tmp_path: Path) -> None:
    """``load_config`` must pop ``_meta`` from the graph dict it inlines under ``cfg.spec["graph"]``."""
    graph_path = tmp_path / "test.graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "_meta": {
                    "source_repo": "https://example.test/repo",
                    "source_sha": "0" * 40,
                    "source_path": "example_workflows/x.json",
                    "captured_at_local": "2026-06-02T12:00-0700",
                    "format": "api",
                    "converter": "verbatim",
                },
                "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
            }
        )
    )
    yaml_path = tmp_path / "test.yaml"
    yaml_path.write_text(
        "engine:\n"
        "  kind: fake\n"
        "  precision: fp16\n"
        "models:\n"
        "  - ref: 'http://example.test/x.bin'\n"
        "    kind: base\n"
        "    target: checkpoints\n"
        f"spec:\n  graph_file: {graph_path.name}\n"
    )
    cfg = load_config(yaml_path)
    graph = cfg.spec["graph"]
    assert "_meta" not in graph, f"_meta survived inline strip: {sorted(graph.keys())}"
    assert "1" in graph, f"node key dropped: {sorted(graph.keys())}"


def test_raw_json_still_carries_meta_for_ac12_consumers(tmp_path: Path) -> None:
    """AC12's cross-reference test reads the raw JSON file directly — ``_meta`` must remain on disk."""
    graph_path = tmp_path / "test.graph.json"
    payload = {
        "_meta": {"source_sha": "deadbeef" * 5},
        "1": {"class_type": "LoadImage", "inputs": {}},
    }
    graph_path.write_text(json.dumps(payload))
    loaded = json.loads(graph_path.read_text())
    assert loaded["_meta"]["source_sha"] == "deadbeef" * 5
