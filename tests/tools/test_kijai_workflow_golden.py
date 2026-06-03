"""Integration test: kijai UI workflow -> API JSON via the converter shim.

Locks down the end-to-end converter behavior against a real-world
ComfyUI workflow that ships with kijai's WanVideoWrapper repo.

Pipeline under test:
1. Load the kijai UI workflow JSON (snapshot at the pinned SHA
   ``088128b224242e110d3906c6750e9a3a348a659b``).
2. Inject a fake ``nodes`` module backed by the captured
   ``/object_info`` fixture for the workflow's pack stack.
3. Run :class:`WorkflowConverter.convert_to_api` from the vendored
   ``seth_workflow_converter``.
4. Strict dict equality against the committed golden.

A failure here means the converter output drifted — either Seth's
upstream changed, our shim's behavior changed, or the
``/object_info`` capture is now out of sync with kijai's pinned SHA.
Regenerate the golden deliberately by re-running
``tools/comfyui_ui_to_api.py`` and inspecting the diff before
committing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_UI_PATH = _FIXTURES_DIR / "kijai_wanvideo_2_1_14B_i2v.ui.json"
_GOLDEN_PATH = _FIXTURES_DIR / "kijai_wanvideo_2_1_14B_i2v.expected_api.json"
_OBJECT_INFO_PATH = (
    Path(__file__).parent.parent
    / "fixtures"
    / "comfyui"
    / "object_info"
    / "f96322b59043.json"
)


def test_kijai_workflow_converts_to_expected_api_json() -> None:
    """Converter output for the kijai i2v workflow matches the committed golden.

    Bug it catches:
        - Upstream change to Seth's converter that silently alters the
          API output shape (e.g. renames a class_type field, reorders
          inputs, changes None vs absent for missing widgets).
        - A regression in ``install_fake_nodes_module`` that swallows or
          mis-types the outer ``INPUT_TYPES`` wrapper (list vs tuple).
        - A re-capture of ``/object_info`` against a different
          ``pack-stack-hash`` that's accidentally pointed at by this
          test — the diff would show every node's widget-value mapping
          changing, not just an isolated field.

    The golden was originally generated at converter HEAD a2aef96 (T5
    commit of the first /object_info fixture, pack-stack 3f7108bde103).
    Regenerated 2026-06-03 against /object_info f96322b59043, which
    carries the full kijai NODE_CLASS_MAPPINGS + VideoHelperSuite — the
    earlier fixture had silently dropped 11 of the kijai WanVideo*
    classes due to a transient pip-dep/ComfyUI version skew at capture
    time, since resolved upstream. New golden's widget values for
    WanVideoTextEncode (positive_prompt/negative_prompt),
    WanVideoSampler (steps/cfg/seed/scheduler), etc. now reflect the
    runnable workflow shape.

    Any planned shim or vendored-converter update MUST regenerate the
    golden in the same commit so the diff is auditable.
    """
    # Defer import: install_fake_nodes_module must run BEFORE the
    # vendored converter import so the fake `nodes` module is in
    # sys.modules at converter-import time.
    #
    # State-leak guard: tests/tools/test_comfyui_ui_to_api.py also
    # calls install_fake_nodes_module with a *different* fixture. The
    # vendored converter does `import nodes` at module top — Python
    # caches the binding, so the second install_fake_nodes_module call
    # swaps sys.modules['nodes'] but the converter still references the
    # first fake. Evict the vendored module from sys.modules before
    # re-importing so its top-level `import nodes` re-resolves against
    # our just-installed fake.
    import sys

    from tools.comfyui_ui_to_api import install_fake_nodes_module

    install_fake_nodes_module(_OBJECT_INFO_PATH)
    sys.modules.pop("tools._vendored.seth_workflow_converter", None)

    from tools._vendored.seth_workflow_converter import WorkflowConverter

    ui_workflow = json.loads(_UI_PATH.read_text(encoding="utf-8"))
    converter = WorkflowConverter()
    actual_api = converter.convert_to_api(ui_workflow)

    expected_api = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))

    assert actual_api == expected_api, (
        "Converter output diverged from committed golden. "
        "Regenerate via `python tools/comfyui_ui_to_api.py "
        f"--ui-json {_UI_PATH.relative_to(Path.cwd())} "
        f"--object-info {_OBJECT_INFO_PATH.relative_to(Path.cwd())} "
        f"--out {_GOLDEN_PATH.relative_to(Path.cwd())}` "
        "and review the diff before committing."
    )


def test_kijai_ui_snapshot_node_count_locked_to_upstream() -> None:
    """Bug it catches: an accidental re-fetch of the UI workflow from a
    different kijai SHA replacing the snapshot in-place. The pinned SHA
    ``088128b22`` has exactly 26 nodes; any drift means the snapshot is
    no longer matched to the captured ``/object_info`` and the golden
    test above will silently regenerate against the wrong baseline.
    """
    ui = json.loads(_UI_PATH.read_text(encoding="utf-8"))
    assert isinstance(ui.get("nodes"), list)
    assert len(ui["nodes"]) == 26, (
        f"UI snapshot has {len(ui['nodes'])} nodes; expected 26 at "
        "kijai SHA 088128b224242e110d3906c6750e9a3a348a659b"
    )


@pytest.mark.parametrize(
    "fixture_path",
    [_UI_PATH, _GOLDEN_PATH, _OBJECT_INFO_PATH],
)
def test_fixture_files_exist_and_parse_as_json(fixture_path: Path) -> None:
    """Bug it catches: a refactor or rename that breaks one fixture path
    silently (e.g. moves /object_info under a different directory). All
    three fixtures must be present + parseable for the integration test
    above to be meaningful.
    """
    assert fixture_path.is_file(), f"missing fixture: {fixture_path}"
    json.loads(fixture_path.read_text(encoding="utf-8"))
