# tools/comfyui_ui_to_api.py Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an offline, hermetic, deterministic ComfyUI UI-format → API-format JSON converter by vendoring Seth Robinson's `WorkflowConverter` (Unlicense) under `tools/_vendored/`, writing a kinoforge-owned shim that injects a fake `nodes` module backed by captured `/object_info` JSON, and producing the first pack-stack fixture + kijai workflow golden via a brief live RunPod capture.

**Architecture:** Phase A (T1–T3) is offline + hermetic — vendor Seth verbatim, write the pure-function pack-stack hash + 3 tests, write the shim + 6 unit tests. Phase B (T4–T5) is the one-shot live capture — env-gated `tools/capture_object_info.py` provisions a brief RunPod pod, GETs `/object_info`, destroys the pod; T5 commits the captured fixture (~1–2 MB). Phase C (T6) integrates — snapshot kijai's UI workflow, run converter to produce expected API JSON, lock down via golden test. Phase D (T7) closes — PROGRESS entry + tasks.json sync. Sub-plan close unblocks parent sub-plan T1.

**Tech Stack:** Python 3.13 / pixi / pytest / ruff / mypy / pre-commit. Vendored: `SethRobinson/comfyui-workflow-to-api-converter-endpoint@<HEAD-SHA>` (Unlicense). Read-only vendored data: `kijai/ComfyUI-WanVideoWrapper@088128b22.../example_workflows/wanvideo_2_1_14B_I2V_example_03.json`. Source sub-spec: `docs/superpowers/specs/2026-06-02-comfyui-ui-to-api-converter-design.md` (committed `d19993d`).

---

## Task ordering and dependencies

```
T1 (vendor Seth) ──┬──► T3 (shim + 6 tests) ──┐
                   │                          ├──► T6 (kijai snapshot + golden) ──► T7 (PROGRESS + tasks.json)
T2 (pack-stack hash + 3 tests) ──► T4 (capture tool live) ──► T5 (commit fixture) ─┘
```

- T1 + T2 are independent — can run in either order.
- T3 needs T1 (imports `tools._vendored.seth_workflow_converter`).
- T4 needs T2 (uses `pack_stack_hash` to derive the default `--out` path).
- T5 needs T4 (commits the file T4 wrote).
- T6 needs T3 (uses the shim to run conversion) + T5 (needs the captured fixture).
- T7 needs T6.

---

## Task 1: Vendor Seth's workflow_converter.py + LICENSES + tools/_vendored marker

**Goal:** Drop Seth Robinson's `workflow_converter.py` verbatim under `tools/_vendored/seth_workflow_converter.py`, pin its upstream SHA in `LICENSES/SETH-UNLICENSE.txt`, scaffold `tools/_vendored/__init__.py` as a 5-line attribution marker, and configure `pyproject.toml` to exclude `tools/_vendored/` from ruff + mypy.

**Files:**
- Create: `tools/__init__.py` (empty, makes `tools` a package; needed for `from tools._vendored ...` import)
- Create: `tools/_vendored/__init__.py` (5-line attribution marker)
- Create: `tools/_vendored/seth_workflow_converter.py` (VERBATIM copy of Seth's file at pinned upstream SHA)
- Create: `LICENSES/SETH-UNLICENSE.txt` (full Unlicense text + repo URL + pinned SHA + local-TZ capture date)
- Modify: `pyproject.toml` (add `tools/_vendored/` to ruff + mypy exclude lists)

**Acceptance Criteria:**
- [ ] `tools/_vendored/seth_workflow_converter.py` byte-equal to upstream at pinned SHA (verify with sha256sum).
- [ ] `LICENSES/SETH-UNLICENSE.txt` contains full Unlicense text + upstream URL `https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint` + pinned 40-char SHA + local-TZ capture date.
- [ ] `tools/_vendored/__init__.py` has 5-line docstring naming the vendored source + license + a pointer at `LICENSES/SETH-UNLICENSE.txt`.
- [ ] `pyproject.toml` ruff `exclude` list includes `tools/_vendored`.
- [ ] `pyproject.toml` mypy `exclude` includes `tools/_vendored`.
- [ ] `pixi run ruff check tools/` passes (skips vendored).
- [ ] `pixi run mypy tools/` passes (skips vendored).

**Verify:**
1. `python -c "import hashlib; print(hashlib.sha256(open('tools/_vendored/seth_workflow_converter.py','rb').read()).hexdigest())"` → matches upstream sha256.
2. `pixi run ruff check tools/` → no errors.
3. `pixi run mypy tools/` → no errors (or skips vendored cleanly).
4. `grep -c 'Unlicense' LICENSES/SETH-UNLICENSE.txt` → ≥ 1.

**Steps:**

- [ ] **Step 1: Confirm upstream SHA + grab the file.**

```bash
mkdir -p /tmp/seth-vendor
cd /tmp/seth-vendor
git clone --depth 1 https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint.git seth 2>&1 | tail -3
cd seth
UPSTREAM_SHA=$(git rev-parse HEAD)
LATEST_TAG=$(git tag --sort=-creatordate | head -1)
if [ -n "$LATEST_TAG" ]; then
  UPSTREAM_SHA=$(git rev-list -n 1 "$LATEST_TAG")
  echo "pinning tag $LATEST_TAG → $UPSTREAM_SHA"
else
  echo "no tags; pinning HEAD: $UPSTREAM_SHA"
fi
git checkout "$UPSTREAM_SHA"
UPSTREAM_SIZE=$(stat -c %s workflow_converter.py)
UPSTREAM_HASH=$(sha256sum workflow_converter.py | cut -d' ' -f1)
CAPTURE_AT=$(python -c "from datetime import datetime; print(datetime.now().astimezone().isoformat(timespec='minutes'))")
echo "SHA=$UPSTREAM_SHA  SIZE=$UPSTREAM_SIZE  HASH=$UPSTREAM_HASH  CAPTURED=$CAPTURE_AT"
```

Capture `UPSTREAM_SHA`, `UPSTREAM_HASH`, `CAPTURE_AT` for later steps.

- [ ] **Step 2: Vendor file + scaffold tools tree.**

```bash
cd /workspace
mkdir -p tools/_vendored LICENSES
touch tools/__init__.py
cp /tmp/seth-vendor/seth/workflow_converter.py tools/_vendored/seth_workflow_converter.py
# Verify byte equality:
diff /tmp/seth-vendor/seth/workflow_converter.py tools/_vendored/seth_workflow_converter.py && echo "VERBATIM ✓"
```

- [ ] **Step 3: Write `tools/_vendored/__init__.py`.**

```python
"""Vendored third-party tools.

Modules under this directory are vendored verbatim from upstream sources
and are NOT subject to kinoforge's lint/type-check tooling (excluded in
pyproject.toml). License attribution for each vendored source lives in
the corresponding LICENSES/<source>.txt at the repo root.

- seth_workflow_converter.py:
    SethRobinson/comfyui-workflow-to-api-converter-endpoint (Unlicense).
    See LICENSES/SETH-UNLICENSE.txt for terms + pinned SHA.
"""
```

- [ ] **Step 4: Write `LICENSES/SETH-UNLICENSE.txt`.**

```
This file documents the third-party source vendored at
tools/_vendored/seth_workflow_converter.py.

Upstream:    https://github.com/SethRobinson/comfyui-workflow-to-api-converter-endpoint
Pinned SHA:  <UPSTREAM_SHA from Step 1>
Captured:    <CAPTURE_AT from Step 1>
SHA256:      <UPSTREAM_HASH from Step 1>

----------------------------------------------------------------------
UNLICENSE
----------------------------------------------------------------------

This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or
distribute this software, either in source code form or as a compiled
binary, for any purpose, commercial or non-commercial, and by any
means.

In jurisdictions that recognize copyright laws, the author or authors
of this software dedicate any and all copyright interest in the
software to the public domain. We make this dedication for the benefit
of the public at large and to the detriment of our heirs and
successors. We intend this dedication to be an overt act of
relinquishment in perpetuity of all present and future rights to this
software under copyright law.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <https://unlicense.org>
```

Substitute the three placeholders with values from Step 1.

- [ ] **Step 5: Configure pyproject.toml excludes.**

Locate the existing `[tool.ruff]` block in `pyproject.toml`. If it has no `exclude`, add one. If it has an existing `exclude` list, append `"tools/_vendored"`. Same for `[tool.mypy]`.

```toml
[tool.ruff]
# ... existing config ...
exclude = [
    # ... existing entries ...
    "tools/_vendored",
]

[tool.mypy]
# ... existing config ...
exclude = [
    # ... existing entries ...
    "tools/_vendored/",
]
```

The exact append shape depends on whether the file uses list literals or string regex patterns. Use the existing convention.

- [ ] **Step 6: Verify lint + type-check are clean.**

```bash
pixi run ruff check tools/ 2>&1 | tail -5
pixi run mypy tools/ 2>&1 | tail -5
pixi run pytest 2>&1 | tail -3
```

Expected: ruff + mypy report no errors over `tools/`; pytest baseline still `979 passed, 3 skipped, 4 xfailed` (no new tests yet).

- [ ] **Step 7: Pre-commit + commit T1.**

```bash
git add tools/__init__.py tools/_vendored/__init__.py tools/_vendored/seth_workflow_converter.py LICENSES/SETH-UNLICENSE.txt pyproject.toml
pixi run pre-commit run --files tools/__init__.py tools/_vendored/__init__.py tools/_vendored/seth_workflow_converter.py LICENSES/SETH-UNLICENSE.txt pyproject.toml
git commit -m "$(cat <<'COMMIT'
feat(tools): vendor Seth's workflow_converter (Unlicense) + LICENSES file

T1 of the comfyui_ui_to_api converter sub-plan
(docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md).

- tools/_vendored/seth_workflow_converter.py: SethRobinson/comfyui-
  workflow-to-api-converter-endpoint vendored verbatim at upstream SHA
  <UPSTREAM_SHA>. Canonical UI->API logic; handles subgraphs (nested),
  COMFY_DYNAMICCOMBO_V3, GetNode/SetNode routing, reroute nodes,
  default values for required inputs, combo-value normalization,
  Unicode workflows. Re-implementing would be wasted work.
- tools/_vendored/__init__.py: 5-line attribution marker + pointer at
  LICENSES/SETH-UNLICENSE.txt.
- tools/__init__.py: empty marker (makes `tools` an importable package).
- LICENSES/SETH-UNLICENSE.txt: full Unlicense text + upstream URL +
  pinned SHA <UPSTREAM_SHA> + local-TZ capture timestamp + SHA256
  hash of the vendored file.
- pyproject.toml: tools/_vendored/ excluded from ruff + mypy. We do
  not lint or type-check vendored third-party code.

Closes sub-spec AC1, AC5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Substitute `<UPSTREAM_SHA>` with the value from Step 1.

Update tasks.json: T1 status → completed.

---

## Task 2: tools/_pack_stack.py + 3 unit tests

**Goal:** Write the pure `pack_stack_hash(comfyui_cfg: dict) -> str` function per sub-spec §5.1 + 3 unit tests per sub-spec §7.1.

**Files:**
- Create: `tools/_pack_stack.py` (~30 LOC)
- Create: `tests/tools/__init__.py` (empty marker)
- Create: `tests/tools/test_pack_stack_hash.py` (~50 LOC, 3 tests)

**Acceptance Criteria:**
- [ ] `tools/_pack_stack.py` exports `pack_stack_hash(comfyui_cfg: dict[str, Any]) -> str` (12-char lowercase hex).
- [ ] Pure function — no I/O, no global state.
- [ ] Deterministic — same input → same output.
- [ ] Insensitive to `custom_nodes` ordering (sorted by `git` URL ascending before hashing).
- [ ] 3 unit tests PASS: empty stack stable hash, order insensitivity, ref bump changes hash.
- [ ] mypy + ruff clean.

**Verify:** `pixi run pytest tests/tools/test_pack_stack_hash.py -v` → 3 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests first (TDD red).**

Create `tests/tools/__init__.py` (empty).

Create `tests/tools/test_pack_stack_hash.py`:

```python
"""Lockdown tests for tools._pack_stack.pack_stack_hash."""
from __future__ import annotations

from tools._pack_stack import pack_stack_hash


def test_empty_pack_stack_has_stable_hash() -> None:
    """Empty comfyui_cfg always hashes to the same pinned value.

    A drift here means the canonical-form algorithm changed (or the
    hash length / case convention shifted). The pinned value is the
    SHA256 prefix of the literal string "comfyui@\\n" — derive it
    manually if this test ever needs re-baselining.
    """
    h = pack_stack_hash({"version": "", "custom_nodes": []})
    # Recomputable from spec: sha256("comfyui@")[:12]
    assert h == "6090a25b3b25", f"empty pack-stack hash drifted: {h!r}"


def test_pack_stack_hash_is_order_insensitive() -> None:
    """Two equivalent stacks in different YAML orders produce the same hash."""
    cfg_a = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abc"},
            {"git": "https://github.com/kijai/B", "ref": "def"},
        ],
    }
    cfg_b = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/B", "ref": "def"},
            {"git": "https://github.com/kijai/A", "ref": "abc"},
        ],
    }
    assert pack_stack_hash(cfg_a) == pack_stack_hash(cfg_b)


def test_pack_stack_hash_changes_on_ref_bump() -> None:
    """Bumping any pinned ref produces a different hash."""
    base = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abc"},
        ],
    }
    bumped = {
        "version": "0.3.10",
        "custom_nodes": [
            {"git": "https://github.com/kijai/A", "ref": "abd"},  # one char
        ],
    }
    assert pack_stack_hash(base) != pack_stack_hash(bumped)
```

Note: the first test's pinned hash value `6090a25b3b25` is the actual SHA256 prefix of `"comfyui@"` (no trailing newline). If implementing strictly per §5.1 (`parts = ["comfyui@"]; "\n".join(parts) = "comfyui@"`), this is correct. Re-derive if §5.1 changes.

To compute the expected value at implementation time:

```bash
python -c "import hashlib; print(hashlib.sha256(b'comfyui@').hexdigest()[:12])"
```

Substitute the actual output if it differs.

- [ ] **Step 2: Confirm RED.**

```bash
pixi run pytest tests/tools/test_pack_stack_hash.py -v
```

Expected: ALL 3 FAIL with `ModuleNotFoundError: No module named 'tools._pack_stack'` (or similar import error). This is the TDD red signal — function doesn't exist yet.

- [ ] **Step 3: Write `tools/_pack_stack.py` implementation.**

```python
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
```

- [ ] **Step 4: Confirm GREEN.**

```bash
pixi run pytest tests/tools/test_pack_stack_hash.py -v
```

Expected: 3 passed. If `test_empty_pack_stack_has_stable_hash` fails because the literal pinned hex string doesn't match, fix the test's expected value to whatever `sha256("comfyui@")[:12]` actually evaluates to — see Step 1 derivation command.

- [ ] **Step 5: Type-check + commit.**

```bash
pixi run mypy tools/_pack_stack.py tests/tools/test_pack_stack_hash.py 2>&1 | tail -5
pixi run ruff check tools/_pack_stack.py tests/tools/test_pack_stack_hash.py 2>&1 | tail -5
pixi run pre-commit run --files tools/_pack_stack.py tests/tools/__init__.py tests/tools/test_pack_stack_hash.py
git add tools/_pack_stack.py tests/tools/__init__.py tests/tools/test_pack_stack_hash.py
git commit -m "$(cat <<'COMMIT'
feat(tools): pack_stack_hash pure function + 3 unit tests

T2 of the comfyui_ui_to_api converter sub-plan.

- tools/_pack_stack.py: 12-char SHA256-prefix hash of canonical
  "comfyui@<version> + sorted <git>@<ref>" string. Pure function, no
  I/O, deterministic, sort-key-order-insensitive. Will be used by
  capture_object_info.py to derive the default fixture path and by
  future workflow tooling to look up captures by pack stack.
- tests/tools/test_pack_stack_hash.py: 3 tests — empty-stack pinned
  value, order insensitivity, ref-bump-changes-hash.
- tests/tools/__init__.py: package marker for pytest discovery.

Closes sub-spec AC3, AC9.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T2 status → completed.

---

## Task 3: tools/comfyui_ui_to_api.py shim + 6 unit tests + minimal fixture

**Goal:** Write the `install_fake_nodes_module` + CLI per sub-spec §5.2 + §5.3; 6 shim unit tests per §7.2; the hand-curated `minimal_object_info.json` fixture used by 5 of them.

**Files:**
- Create: `tools/comfyui_ui_to_api.py` (~120 LOC: install_fake_nodes_module + main + argparse)
- Create: `tests/tools/test_comfyui_ui_to_api.py` (~180 LOC, 6 tests)
- Create: `tests/tools/fixtures/minimal_object_info.json` (~50 lines, 3 hand-curated classes)
- Create: `tests/tools/fixtures/__init__.py` (empty) — only if pytest auto-collects fail otherwise; usually fixtures dirs don't need init.

**Acceptance Criteria:**
- [ ] `install_fake_nodes_module(object_info_path: Path) -> types.ModuleType` installs a fake `nodes` module in `sys.modules`.
- [ ] List → tuple cast applied at OUTER type-spec wrapper only; inner option lists preserved as lists.
- [ ] `input_order` respected — INPUT_TYPES dict iterates per `input_order` keys.
- [ ] Idempotent — calling twice replaces cleanly.
- [ ] Missing optional fields default safely (`RETURN_TYPES=()`, `OUTPUT_NODE=False`, etc.).
- [ ] CLI `main()` parses args, calls install, imports vendored converter, runs convert_to_api, writes UTF-8 JSON.
- [ ] All 6 unit tests PASS.
- [ ] mypy + ruff clean.

**Verify:** `pixi run pytest tests/tools/test_comfyui_ui_to_api.py -v` → 6 passed.

**Steps:**

- [ ] **Step 1: Write the minimal_object_info fixture.**

Create `tests/tools/fixtures/minimal_object_info.json`:

```json
{
  "LoadImage": {
    "input": {
      "required": {
        "image": [
          ["a.png", "b.png"],
          {"image_upload": true}
        ]
      }
    },
    "input_order": {"required": ["image"]},
    "output": ["IMAGE", "MASK"],
    "output_name": ["IMAGE", "MASK"],
    "function": "load_image",
    "category": "image",
    "output_node": false,
    "display_name": "Load Image"
  },
  "CLIPTextEncode": {
    "input": {
      "required": {
        "text": ["STRING", {"multiline": true, "default": ""}],
        "clip": ["CLIP", {}]
      }
    },
    "input_order": {"required": ["text", "clip"]},
    "output": ["CONDITIONING"],
    "output_name": ["CONDITIONING"],
    "function": "encode",
    "category": "conditioning",
    "output_node": false,
    "display_name": "CLIP Text Encode (Prompt)"
  },
  "Note": {
    "input": {},
    "input_order": {},
    "output": [],
    "function": "note",
    "category": "utils"
  }
}
```

- [ ] **Step 2: Write the failing tests (TDD red).**

Create `tests/tools/test_comfyui_ui_to_api.py`:

```python
"""Lockdown tests for tools.comfyui_ui_to_api shim."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path("tests/tools/fixtures")
MINIMAL_OBJECT_INFO = FIXTURES_DIR / "minimal_object_info.json"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
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
    assert set(mappings.keys()) == {"LoadImage", "CLIPTextEncode", "Note"}


def test_INPUT_TYPES_returns_classmethod_with_correct_required_section() -> None:
    """INPUT_TYPES() returns the captured schema with outer tuples + inner lists preserved."""
    from tools.comfyui_ui_to_api import install_fake_nodes_module

    install_fake_nodes_module(MINIMAL_OBJECT_INFO)
    cls = sys.modules["nodes"].NODE_CLASS_MAPPINGS["LoadImage"]
    schema = cls.INPUT_TYPES()
    assert "required" in schema
    image_spec = schema["required"]["image"]
    # Outer wrapper is a tuple (per ComfyUI runtime convention).
    assert isinstance(image_spec, tuple), f"outer wrapper must be tuple, got {type(image_spec)}"
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


def test_cli_main_writes_api_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end CLI: synthetic UI JSON + minimal_object_info -> API JSON."""
    from tools.comfyui_ui_to_api import main

    # Build a tiny 2-node UI workflow that ONLY references nodes the fixture knows.
    ui_workflow = {
        "id": "test-workflow",
        "revision": 1,
        "last_node_id": 2,
        "last_link_id": 0,
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
                    {"name": "IMAGE", "type": "IMAGE", "links": []},
                    {"name": "MASK", "type": "MASK", "links": []},
                ],
                "properties": {},
                "widgets_values": ["a.png"],
            },
            {
                "id": 2,
                "type": "Note",
                "pos": [300, 0],
                "size": [200, 100],
                "flags": {},
                "order": 1,
                "mode": 0,
                "inputs": [],
                "outputs": [],
                "properties": {},
                "widgets_values": ["hello"],
            },
        ],
        "links": [],
        "groups": [],
        "config": {},
        "extra": {},
        "version": 0.4,
    }

    ui_path = tmp_path / "ui.json"
    out_path = tmp_path / "out.json"
    ui_path.write_text(json.dumps(ui_workflow))

    monkeypatch.setattr(
        sys, "argv",
        [
            "comfyui_ui_to_api",
            "--ui-json", str(ui_path),
            "--object-info", str(MINIMAL_OBJECT_INFO),
            "--out", str(out_path),
        ],
    )
    rc = main()
    assert rc == 0
    api = json.loads(out_path.read_text())
    # API format: dict keyed by stringified node IDs.
    assert isinstance(api, dict)
    assert "1" in api
    assert api["1"]["class_type"] == "LoadImage"
    assert "2" in api
    assert api["2"]["class_type"] == "Note"
```

- [ ] **Step 3: Confirm RED.**

```bash
pixi run pytest tests/tools/test_comfyui_ui_to_api.py -v
```

Expected: ALL 6 FAIL with `ModuleNotFoundError: No module named 'tools.comfyui_ui_to_api'`.

- [ ] **Step 4: Write `tools/comfyui_ui_to_api.py`.**

```python
"""ComfyUI UI-format → API-format JSON converter (offline shim).

Vendors tools/_vendored/seth_workflow_converter.py (Unlicense, see
LICENSES/SETH-UNLICENSE.txt). Decouples Seth's converter from a live
ComfyUI server by injecting a fake `nodes` module backed by a captured
/object_info JSON before importing the vendored module.

Usage:
    python tools/comfyui_ui_to_api.py \\
        --ui-json <input.json> \\
        --object-info <captured-object-info.json> \\
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
    """Inject a fake `nodes` module into sys.modules from a captured /object_info.

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

        stub = type(
            class_type,
            (),
            {
                "INPUT_TYPES": classmethod(lambda _cls, _s=ordered: _s),
                "RETURN_TYPES": tuple(info.get("output", [])),
                "RETURN_NAMES": tuple(
                    info.get("output_name", info.get("output", []))
                ),
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
    from tools._vendored.seth_workflow_converter import WorkflowConverter  # noqa: E402

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
```

- [ ] **Step 5: Confirm GREEN.**

```bash
pixi run pytest tests/tools/test_comfyui_ui_to_api.py -v
```

Expected: 6 passed. If `test_cli_main_writes_api_json` fails because Seth's `WorkflowConverter` requires something on its input UI JSON that the synthetic one lacks (e.g. `definitions`, `subgraphs`), check Seth's `convert_to_api()` for required keys and add missing fields to the synthetic UI JSON. The first 5 tests are pure-shim and shouldn't touch Seth's code.

- [ ] **Step 6: Type-check + commit.**

```bash
pixi run mypy tools/comfyui_ui_to_api.py tests/tools/test_comfyui_ui_to_api.py 2>&1 | tail -5
pixi run ruff check tools/comfyui_ui_to_api.py tests/tools/test_comfyui_ui_to_api.py 2>&1 | tail -5
pixi run pre-commit run --files tools/comfyui_ui_to_api.py tests/tools/test_comfyui_ui_to_api.py tests/tools/fixtures/minimal_object_info.json
git add tools/comfyui_ui_to_api.py tests/tools/test_comfyui_ui_to_api.py tests/tools/fixtures/minimal_object_info.json
git commit -m "$(cat <<'COMMIT'
feat(tools): comfyui_ui_to_api shim — install_fake_nodes_module + CLI + 6 tests

T3 of the comfyui_ui_to_api converter sub-plan.

- tools/comfyui_ui_to_api.py: kinoforge-owned shim around Seth's
  vendored converter. install_fake_nodes_module() builds a fake
  `nodes` module from a captured /object_info JSON and injects it
  into sys.modules BEFORE the vendored converter's top-level
  `import nodes` resolves. Outer type-spec wrapper list -> tuple
  (matches ComfyUI runtime); inner option lists preserved. input_order
  respected via dict-insertion order (Python 3.7+ insertion-order
  semantics). main() provides a CLI: --ui-json/--object-info/--out.
- tests/tools/test_comfyui_ui_to_api.py: 6 unit tests covering all
  shim behaviors + one end-to-end CLI smoke against the minimal
  fixture.
- tests/tools/fixtures/minimal_object_info.json: 3-class hand-curated
  /object_info stub (LoadImage, CLIPTextEncode, Note).

Closes sub-spec AC2, AC10.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T3 status → completed.

---

## Task 4: tools/capture_object_info.py + first live pack-stack capture

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Write `tools/capture_object_info.py` per sub-spec §5.4 — env-gated live capture tool that provisions a brief RunPod pod via the Layer Q `render_provision` / `wait_for_ready` orchestrator path, GETs `/object_info`, writes the fixture file, destroys the pod in a finally clause. Use the tool ONCE during this task to capture the first pack-stack fixture for `examples/configs/runpod-comfyui-wan.yaml`.

**Files:**
- Create: `tools/capture_object_info.py` (~150 LOC)
- Output (not committed in T4; T5 commits): `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json`

**Acceptance Criteria:**
- [ ] `tools/capture_object_info.py` accepts `--workflow-yaml` (required) + optional `--out` (default derived via `pack_stack_hash`).
- [ ] Fails fast (exit 1, no provisioning) if any of `KINOFORGE_LIVE_TESTS=1`, `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`, `HF_TOKEN` is unset.
- [ ] Uses Layer Q orchestrator path: render_provision → validate env_required → create_instance → engine.provision (with wait_for_ready against `/system_stats`, boot_timeout_s honored) → backend.
- [ ] GETs `/object_info` from `backend._base_url`, parses JSON.
- [ ] Writes fixture as pretty-printed, sort-keyed JSON (`indent=2, sort_keys=True, ensure_ascii=False`) so cross-capture diffs show only added/removed classes.
- [ ] Finally clause destroys the pod via `provider.destroy_instance(instance.id)` regardless of capture success.
- [ ] Tool runs to completion against `examples/configs/runpod-comfyui-wan.yaml` and produces a fixture file with ≥ 100 class entries (real ComfyUI + kijai + KJNodes + VHS will be in the hundreds).
- [ ] No paid pods left running at task close — verify via `pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml` → zero rows.
- [ ] Total live spend ≤ $0.50.

**Verify:**
1. Tool runs end-to-end: `pixi run python tools/capture_object_info.py --workflow-yaml examples/configs/runpod-comfyui-wan.yaml`. Output stdout contains: `wrote tests/fixtures/comfyui/object_info/<hash>.json (<bytes>, N classes, pack-stack-hash=<hash>)`.
2. Fixture file exists, size > 100 KB, JSON valid, ≥ 100 top-level class-type entries: `python -c "import json; d = json.load(open('tests/fixtures/comfyui/object_info/<hash>.json')); print(len(d))"` → integer ≥ 100.
3. `pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml` → zero rows.

**Steps:**

- [ ] **Step 1: Write `tools/capture_object_info.py`.**

```python
"""Capture /object_info from a brief RunPod pod for a kinoforge workflow YAML.

Usage:
    pixi run python tools/capture_object_info.py \\
        --workflow-yaml examples/configs/runpod-comfyui-wan.yaml \\
        [--out tests/fixtures/comfyui/object_info/<hash>.json]

Env-gated like other live tools: KINOFORGE_LIVE_TESTS=1 +
RUNPOD_API_KEY + RUNPOD_TERMINATE_KEY + HF_TOKEN must all be set.
Setting the env vars IS authorization — no interactive prompt.

Default --out path: tests/fixtures/comfyui/object_info/<pack-stack-hash>.json
where pack-stack-hash is derived via tools._pack_stack.pack_stack_hash
on the YAML's engine.comfyui block.

Cost: ~$0.10 per capture (pod boots, /object_info responds, pod
destroyed). Captures once per pack-stack version; many workflows share
the same fixture by pack-stack-hash equality.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _require_env(keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        print(
            f"capture_object_info: missing env vars: {missing}",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="capture_object_info")
    parser.add_argument(
        "--workflow-yaml",
        required=True,
        type=Path,
        help="Path to a kinoforge workflow YAML.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output path. Defaults to "
            "tests/fixtures/comfyui/object_info/<pack-stack-hash>.json."
        ),
    )
    args = parser.parse_args()

    _require_env(
        (
            "KINOFORGE_LIVE_TESTS",
            "RUNPOD_API_KEY",
            "RUNPOD_TERMINATE_KEY",
            "HF_TOKEN",
        )
    )

    # Deferred imports — only load kinoforge/_adapters after env-gate passes.
    import kinoforge._adapters  # noqa: F401 — register sources/engines/providers
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.registry import get_engine, get_provider

    from tools._pack_stack import pack_stack_hash

    cfg = load_config(args.workflow_yaml)
    stack_hash = pack_stack_hash(cfg.engine.comfyui.model_dump())

    out_path: Path = args.out or Path(
        f"tests/fixtures/comfyui/object_info/{stack_hash}.json"
    )

    engine_kind = cfg.engine.kind  # "comfyui"
    provider_kind = cfg.compute.provider  # "runpod"

    engine = get_engine(engine_kind)
    provider = get_provider(provider_kind)
    cred_provider = EnvCredentialProvider()
    cfg_dict = cfg.model_dump()
    # Lift the resolved lifecycle onto cfg_dict per Layer Q convention so
    # render_provision + wait_for_ready see boot_timeout_s.
    import dataclasses

    cfg_dict["lifecycle"] = dataclasses.asdict(cfg.lifecycle())

    # Layer Q seam: orchestrator wires engine.attach_get_instance with the
    # provider's get_instance. capture_object_info does the same thing.
    engine.attach_get_instance(provider.get_instance)  # type: ignore[attr-defined]

    rendered = engine.render_provision(cfg_dict)
    # Validate env_required against EnvCredentialProvider.
    env_kv: dict[str, str] = {}
    for var in rendered.env_required:
        val = cred_provider.get(var)
        if val is None:
            print(
                f"capture_object_info: env_required {var!r} not set",
                file=sys.stderr,
            )
            return 1
        env_kv[var] = val

    from kinoforge.core.interfaces import InstanceSpec  # local import

    spec = InstanceSpec(
        engine=engine_kind,
        image=rendered.image,
        ports=rendered.ports,
        env=env_kv,
        provision_script=rendered.script,
        run_cmd=rendered.run_cmd,
        requirements=cfg.compute.requirements.model_dump(),
        lifecycle=cfg.lifecycle(),
        tags={"kinoforge_purpose": "object_info_capture"},
    )

    instance = provider.create_instance(spec)
    try:
        engine.provision(instance, cfg_dict)
        backend = engine.backend(instance, cfg_dict)

        pod_base_url = backend._base_url  # ComfyUIBackend exposes this
        request = urllib.request.Request(
            f"{pod_base_url}/object_info",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-capture/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                object_info = json.loads(resp.read())
        except urllib.error.URLError as exc:
            print(
                f"capture_object_info: GET /object_info failed: {exc}",
                file=sys.stderr,
            )
            return 1

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(
                object_info,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        size = out_path.stat().st_size
        print(
            f"wrote {out_path} ({size} bytes, "
            f"{len(object_info)} classes, pack-stack-hash={stack_hash})"
        )
        return 0
    finally:
        try:
            provider.destroy_instance(instance.id)
            print(f"destroyed pod {instance.id}", file=sys.stderr)
        except Exception as exc:
            # Surface the failure but do not mask the primary exit code.
            print(
                f"capture_object_info: WARNING — destroy_instance failed: {exc}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    sys.exit(main())
```

Note: the exact import shapes for `InstanceSpec`, `attach_get_instance`, `EnvCredentialProvider` need to match the actual kinoforge code. If a name doesn't resolve, grep the codebase: `rg -n "class InstanceSpec" src/`, `rg -n "attach_get_instance" src/`, `rg -n "class EnvCredentialProvider" src/`. Adjust import paths in the script.

- [ ] **Step 2: Smoke-test the tool offline (env-gate path only — no provisioning).**

```bash
# Confirm env-gate exits cleanly when env is unset:
unset KINOFORGE_LIVE_TESTS
pixi run python tools/capture_object_info.py --workflow-yaml examples/configs/runpod-comfyui-wan.yaml
echo "exit=$?"
```

Expected: stderr says "missing env vars: ['KINOFORGE_LIVE_TESTS', ...]", exit code 1. NO provisioning attempt.

- [ ] **Step 3: Pre-flight ledger check before live run.**

```bash
pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml
```

Expected: zero rows. If any rows show, destroy them first to start clean:
`pixi run python -m kinoforge destroy <pod_id> --config examples/configs/runpod-comfyui-wan.yaml`.

- [ ] **Step 4: Run the live capture.**

This is the live spend. Pod boots, captures `/object_info`, destroys.

```bash
export KINOFORGE_LIVE_TESTS=1
export RUNPOD_API_KEY="..."        # set externally; never commit
export RUNPOD_TERMINATE_KEY="..."
export HF_TOKEN="..."
pixi run python tools/capture_object_info.py \
    --workflow-yaml examples/configs/runpod-comfyui-wan.yaml \
    2>&1 | tee /tmp/capture-attempt.log
```

Watch stdout for:
- "wrote tests/fixtures/comfyui/object_info/<hash>.json (<bytes>, N classes, pack-stack-hash=<hash>)"
- stderr: "destroyed pod <id>"

Time budget: typical kijai+KJNodes cold boot ~5–10 min. Cost ~$0.10.

If it fails mid-boot:
- Check stderr for the failure mode (capacity / wait_for_ready timeout / HF gated repo 401 / etc.).
- Inspect ledger: `pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml` — if a pod is still up, destroy it before re-running.
- The finally clause SHOULD have destroyed the pod even on failure; confirm via the ledger.

Re-runs are safe — every run captures fresh; idempotent at the file level (overwrites the fixture).

- [ ] **Step 5: Verify the captured fixture is sensible.**

```bash
FIXTURE_HASH=$(python -c "
import dataclasses, json
from pathlib import Path
from kinoforge.core.config import load_config
from tools._pack_stack import pack_stack_hash
cfg = load_config(Path('examples/configs/runpod-comfyui-wan.yaml'))
print(pack_stack_hash(cfg.engine.comfyui.model_dump()))
")
FIXTURE_PATH="tests/fixtures/comfyui/object_info/${FIXTURE_HASH}.json"
ls -la "$FIXTURE_PATH"
python -c "
import json
d = json.load(open('$FIXTURE_PATH'))
print(f'classes: {len(d)}')
present = [c for c in ['LoadImage', 'CLIPLoader', 'CLIPTextEncode', 'WanVideoSampler', 'WanVideoModelLoader', 'WanVideoVAELoader', 'ImageResizeKJv2', 'VHS_VideoCombine'] if c in d]
print(f'spot-check: {len(present)}/8 expected classes found: {present}')
"
```

Expected: classes ≥ 100; spot-check finds ≥ 6 of the 8 listed canonical classes (the 2 missing are tolerable variance in node-pack provenance).

- [ ] **Step 6: Post-run ledger check.**

```bash
pixi run python -m kinoforge list --config examples/configs/runpod-comfyui-wan.yaml
```

Expected: zero rows. If any pod is still up, destroy it immediately:
`pixi run python -m kinoforge destroy <pod_id> --config examples/configs/runpod-comfyui-wan.yaml`.

- [ ] **Step 7: Commit T4 (the TOOL only — fixture commits in T5).**

```bash
git add tools/capture_object_info.py
pixi run pre-commit run --files tools/capture_object_info.py
git commit -m "$(cat <<'COMMIT'
feat(tools): capture_object_info live /object_info capture tool

T4 of the comfyui_ui_to_api converter sub-plan.

- tools/capture_object_info.py: env-gated tool that provisions a brief
  RunPod pod via the Layer Q render_provision / wait_for_ready
  orchestrator path, GETs /object_info, writes a pretty-printed
  sort-keyed JSON fixture, destroys the pod in a finally clause.
- Default --out derives the path from
  tools._pack_stack.pack_stack_hash applied to the YAML's
  engine.comfyui block, so workflows sharing pack stacks share
  fixtures.
- Env-gate: KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY +
  RUNPOD_TERMINATE_KEY + HF_TOKEN. Fails fast if any unset.
- Cost ~$0.10 per capture.

One-shot capture against examples/configs/runpod-comfyui-wan.yaml
performed during this task; fixture commits in T5.

Closes sub-spec AC4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T4 status → completed.

---

## Task 5: Commit the captured /object_info fixture

**Goal:** Commit the fixture file produced by T4 as its own atomic commit so the tool commit and the data commit are reviewable separately.

**Files:**
- Create: `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json` (~1–2 MB, written by T4)
- Create: `tests/fixtures/__init__.py` (empty marker — only if pytest needs it; many projects skip this)
- Create: `tests/fixtures/comfyui/__init__.py` (empty marker — only if pytest needs it)
- Create: `tests/fixtures/comfyui/object_info/__init__.py` (empty marker — only if pytest needs it)

**Acceptance Criteria:**
- [ ] The fixture file exists and is committed.
- [ ] File size > 100 KB (real /object_info dumps for kijai stacks are typically 1–2 MB).
- [ ] JSON valid; `python -c "import json; json.load(open(...))"` succeeds.
- [ ] ≥ 100 top-level class-type entries.
- [ ] Pretty-printed (indent=2), sort-keyed (so future-diff stability holds).

**Verify:**
1. `git log --oneline -1 tests/fixtures/comfyui/object_info/` → shows T5's commit.
2. `ls -la tests/fixtures/comfyui/object_info/*.json` → file present, size > 100 KB.
3. `python -c "import json; d = json.load(open('tests/fixtures/comfyui/object_info/<hash>.json')); print(len(d))"` → ≥ 100.

**Steps:**

- [ ] **Step 1: Decide whether `__init__.py` markers are needed.**

If kinoforge's existing test layout has data fixtures without `__init__.py` markers (e.g. `tests/providers/fixtures/runpod/*.json` per recon), skip the marker files. Quick check:

```bash
ls tests/providers/fixtures/ 2>/dev/null | head -5
find tests/ -name '__init__.py' | head
```

If no markers exist in similar data-fixture directories, omit them here too.

- [ ] **Step 2: Confirm pre-commit hooks tolerate the fixture file size.**

The check-added-large-files hook is set to 500 KB per recon (it was visible in earlier pre-commit output). A ~1–2 MB fixture file will trigger that hook.

Decide: bump the hook threshold for this fixture-class, or add an explicit exemption, or use git LFS.

Recommended: amend `.pre-commit-config.yaml` to either (a) raise the threshold globally or (b) exclude `tests/fixtures/comfyui/object_info/*.json` from the hook.

```yaml
# .pre-commit-config.yaml — find the check-added-large-files entry
  - id: check-added-large-files
    args: [--maxkb=2048]   # was 500
    # OR:
    exclude: ^tests/fixtures/comfyui/object_info/.*\.json$
```

Pick whichever fits the project's pattern. Make this a separate commit BEFORE T5's data commit:

```bash
git add .pre-commit-config.yaml
pixi run pre-commit run --files .pre-commit-config.yaml
git commit -m "$(cat <<'COMMIT'
chore(pre-commit): permit /object_info fixture files up to 2 MB

ComfyUI /object_info dumps captured via tools/capture_object_info.py
typical sizes 1-2 MB. Used as offline fixtures for the converter shim
(tools/comfyui_ui_to_api.py); cannot reasonably be smaller. Hook
threshold raised from 500 KB to 2048 KB to accommodate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

(If using the `exclude` approach, adapt the commit subject + body accordingly.)

- [ ] **Step 3: Commit the fixture file.**

```bash
FIXTURE_HASH=$(python -c "
from pathlib import Path
from kinoforge.core.config import load_config
from tools._pack_stack import pack_stack_hash
cfg = load_config(Path('examples/configs/runpod-comfyui-wan.yaml'))
print(pack_stack_hash(cfg.engine.comfyui.model_dump()))
")
git add tests/fixtures/comfyui/object_info/"${FIXTURE_HASH}".json
pixi run pre-commit run --files tests/fixtures/comfyui/object_info/"${FIXTURE_HASH}".json
git commit -m "$(cat <<COMMIT
test(fixtures): /object_info capture for kijai wanvideo i2v pack stack

T5 of the comfyui_ui_to_api converter sub-plan.

Captured via tools/capture_object_info.py against
examples/configs/runpod-comfyui-wan.yaml. Pack-stack-hash:
${FIXTURE_HASH}. Pack stack:
- ComfyUI version: 0.3.10 (per YAML)
- kijai/ComfyUI-WanVideoWrapper @ 088128b224242e110d3906c6750e9a3a348a659b
- kijai/ComfyUI-KJNodes @ 369c8aee9ad4641823d0ffd7035076bcd297b6f2

File is pretty-printed + sort-keyed for diff stability across future
re-captures. Consumed by tests/tools/test_kijai_workflow_golden.py
(T6) and by any future workflow tooling that resolves /object_info by
pack-stack-hash equality.

Closes sub-spec AC6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Note: HEREDOC delimiter is unquoted (no single quotes) so `${FIXTURE_HASH}` expands.

Update tasks.json: T5 status → completed.

---

## Task 6: Snapshot kijai UI workflow + produce golden + integration test

**Goal:** Vendor kijai's UI workflow at the pinned SHA, run the converter to produce the expected API JSON, commit both as test fixtures, write `tests/tools/test_kijai_workflow_golden.py` (1 integration test that locks the full pipeline).

**Files:**
- Create: `tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json` (snapshot from `/tmp/kijai-pull/wrapper/example_workflows/wanvideo_2_1_14B_I2V_example_03.json` at SHA `088128b22`)
- Create: `tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json` (produced by running the converter; reviewed before commit)
- Create: `tests/tools/test_kijai_workflow_golden.py` (~50 LOC, 1 integration test)

**Acceptance Criteria:**
- [ ] Kijai UI snapshot byte-equal to upstream at SHA `088128b22`.
- [ ] Expected API JSON contains 26 nodes (matching `EXPECTED_NODE_COUNT` in `tests/examples/test_runpod_comfyui_wan_graph.py`).
- [ ] Expected API JSON's class types are a subset of `EXPECTED_CLASS_TYPES` from the same lockdown file (no surprise class names).
- [ ] `test_kijai_workflow_converts_to_expected_api_json` PASSES.
- [ ] Test asserts strict dict equality between converter output and the committed golden.

**Verify:**
1. `pixi run pytest tests/tools/test_kijai_workflow_golden.py -v` → 1 passed.
2. `python -c "import json; d = json.load(open('tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json')); print(len(d))"` → 26.
3. `python -c "
import json
g = json.load(open('tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json'))
ctypes = {n['class_type'] for n in g.values()}
expected = {'CLIPLoader', 'CLIPTextEncode', 'CLIPVisionLoader', 'ImageResizeKJv2', 'LoadImage', 'LoadWanVideoT5TextEncoder', 'Note', 'VHS_VideoCombine', 'WanVideoBlockSwap', 'WanVideoClipVisionEncode', 'WanVideoDecode', 'WanVideoImageToVideoEncode', 'WanVideoLoraSelect', 'WanVideoModelLoader', 'WanVideoSampler', 'WanVideoSetBlockSwap', 'WanVideoTextEmbedBridge', 'WanVideoTextEncode', 'WanVideoTorchCompileSettings', 'WanVideoVAELoader', 'WanVideoVRAMManagement'}
extra = ctypes - expected
print(f'class types in golden: {len(ctypes)}; not in EXPECTED_CLASS_TYPES: {sorted(extra)}')
"` → `not in EXPECTED_CLASS_TYPES: []`.

**Steps:**

- [ ] **Step 1: Re-clone kijai upstream + snapshot the UI workflow.**

```bash
mkdir -p /tmp/kijai-snapshot
cd /tmp/kijai-snapshot
git clone --depth 1 https://github.com/kijai/ComfyUI-WanVideoWrapper.git wrapper 2>&1 | tail -3
cd wrapper
git checkout 088128b224242e110d3906c6750e9a3a348a659b
ls example_workflows/ | rg -i 'i2v'
```

Locate `wanvideo_2_1_14B_I2V_example_03.json` (or whatever file the T1 subagent of the parent sub-plan identified — it was named per the recon report). Verify it has 26 nodes:

```bash
python -c "
import json
from pathlib import Path
p = Path('example_workflows/wanvideo_2_1_14B_I2V_example_03.json')
d = json.loads(p.read_text())
print(f'top keys: {list(d.keys())}')
print(f'node count: {len(d[\"nodes\"])}')
print(f'unique node types: {len({n[\"type\"] for n in d[\"nodes\"]})}')
"
```

Expected: top keys include `nodes` + `links`; node count = 26; unique types = 21.

If the file name differs from `wanvideo_2_1_14B_I2V_example_03.json`, substitute below.

- [ ] **Step 2: Copy the snapshot into the kinoforge tree.**

```bash
cd /workspace
mkdir -p tests/tools/fixtures
cp /tmp/kijai-snapshot/wrapper/example_workflows/wanvideo_2_1_14B_I2V_example_03.json \
   tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json
```

- [ ] **Step 3: Run the converter to produce the expected API JSON.**

Use the fixture committed in T5.

```bash
FIXTURE_HASH=$(python -c "
from pathlib import Path
from kinoforge.core.config import load_config
from tools._pack_stack import pack_stack_hash
cfg = load_config(Path('examples/configs/runpod-comfyui-wan.yaml'))
print(pack_stack_hash(cfg.engine.comfyui.model_dump()))
")
pixi run python tools/comfyui_ui_to_api.py \
    --ui-json tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json \
    --object-info "tests/fixtures/comfyui/object_info/${FIXTURE_HASH}.json" \
    --out tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json
```

Verify the output shape:

```bash
python -c "
import json
g = json.load(open('tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json'))
print(f'top keys: {len(g)} (expected 26)')
ctypes = {n['class_type'] for n in g.values()}
print(f'class types: {len(ctypes)} (expected 21)')
print(f'sample node: {json.dumps(g[next(iter(g))], indent=2)[:300]}')
"
```

Expected: `top keys: 26`, `class types: 21`. If mismatched, the conversion produced unexpected output — investigate before committing. Common failure modes:
- Pack-stack fixture is missing a node class kijai's workflow uses (would surface as Seth's converter raising or returning extra/missing nodes).
- Seth's converter version handles a node type differently than expected (compare against the EXPECTED_NODE_COUNT + EXPECTED_CLASS_TYPES set in parent test file).

**Review the produced file before committing.** Eyeball-check a few node entries to make sure inputs are reasonable.

- [ ] **Step 4: Write the integration test.**

Create `tests/tools/test_kijai_workflow_golden.py`:

```python
"""Integration lockdown: kijai UI workflow + pack-stack fixture -> expected API JSON.

The expected golden file is produced ONCE via:
    pixi run python tools/comfyui_ui_to_api.py \\
        --ui-json tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json \\
        --object-info tests/fixtures/comfyui/object_info/<pack-stack-hash>.json \\
        --out tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json

Eyeballed for sanity before commit. Any subsequent drift in:
  - Seth's vendored converter behaviour (re-vendoring or version bump)
  - The kijai UI workflow shape (re-snapshot at a different SHA)
  - The captured /object_info fixture (re-capture against a newer pack stack)
flips this test. That is the entire point — get a loud signal on any
behaviour change so we can decide whether to re-baseline or to fix.

Re-baseline procedure:
    1. Re-run the command at the top of this docstring.
    2. `git diff tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json`
       — eyeball.
    3. Commit: `chore(tests): re-baseline kijai golden against Seth v<X.Y.Z>`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

FIXTURES_DIR = Path("tests/tools/fixtures")
KIJAI_UI_PATH = FIXTURES_DIR / "kijai_wanvideo_2_1_14B_i2v.ui.json"
EXPECTED_API_PATH = FIXTURES_DIR / "kijai_wanvideo_2_1_14B_i2v.expected_api.json"


def _resolve_pack_stack_fixture() -> Path:
    from kinoforge.core.config import load_config

    from tools._pack_stack import pack_stack_hash

    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))
    stack_hash = pack_stack_hash(cfg.engine.comfyui.model_dump())
    return Path(f"tests/fixtures/comfyui/object_info/{stack_hash}.json")


def test_kijai_workflow_converts_to_expected_api_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full pipeline: kijai UI JSON + pack-stack fixture -> expected API JSON."""
    pack_stack_fixture = _resolve_pack_stack_fixture()
    assert pack_stack_fixture.exists(), (
        f"pack-stack fixture missing at {pack_stack_fixture} — "
        f"run tools/capture_object_info.py (T4) or rebase from main"
    )
    assert KIJAI_UI_PATH.exists(), f"kijai UI snapshot missing at {KIJAI_UI_PATH}"
    assert EXPECTED_API_PATH.exists(), (
        f"expected golden missing at {EXPECTED_API_PATH}"
    )

    out = tmp_path / "out.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "comfyui_ui_to_api",
            "--ui-json",
            str(KIJAI_UI_PATH),
            "--object-info",
            str(pack_stack_fixture),
            "--out",
            str(out),
        ],
    )
    from tools.comfyui_ui_to_api import main

    rc = main()
    assert rc == 0

    actual = json.loads(out.read_text())
    expected = json.loads(EXPECTED_API_PATH.read_text())
    assert actual == expected, (
        "kijai workflow conversion drift — "
        f"actual-only-keys={sorted(set(actual) - set(expected))[:5]}, "
        f"expected-only-keys={sorted(set(expected) - set(actual))[:5]}"
    )
```

- [ ] **Step 5: Verify GREEN.**

```bash
pixi run pytest tests/tools/test_kijai_workflow_golden.py -v
```

Expected: 1 passed.

If it fails with "kijai workflow conversion drift" — the converter's output differs from what we just generated. Re-run the converter from Step 3 and confirm bytes are identical to the committed golden:

```bash
diff <(pixi run python tools/comfyui_ui_to_api.py \
    --ui-json tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json \
    --object-info "tests/fixtures/comfyui/object_info/${FIXTURE_HASH}.json" \
    --out /dev/stdout) \
  tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json
```

(May not be diff-able via /dev/stdout if the CLI writes a trailing newline differently; alternative: write to `/tmp/redo.json` and diff that.)

- [ ] **Step 6: Type-check + commit T6.**

```bash
pixi run mypy tests/tools/test_kijai_workflow_golden.py 2>&1 | tail -5
pixi run ruff check tests/tools/test_kijai_workflow_golden.py 2>&1 | tail -5
pixi run pre-commit run --files \
    tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json \
    tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json \
    tests/tools/test_kijai_workflow_golden.py
git add \
    tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json \
    tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json \
    tests/tools/test_kijai_workflow_golden.py
git commit -m "$(cat <<'COMMIT'
test(tools): kijai UI snapshot + expected API golden + integration test

T6 of the comfyui_ui_to_api converter sub-plan.

- tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json: snapshot of
  kijai's UI-format workflow vendored read-only from
  kijai/ComfyUI-WanVideoWrapper@088128b224242e110d3906c6750e9a3a348a659b
  example_workflows/wanvideo_2_1_14B_I2V_example_03.json. 26 nodes, 21
  unique class types.
- tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json:
  golden API JSON produced by running the converter once against the
  pack-stack fixture committed in T5, reviewed for sanity before commit.
- tests/tools/test_kijai_workflow_golden.py: 1 integration test that
  re-runs the converter and asserts dict equality with the committed
  golden. Any drift in Seth's converter, the kijai UI fixture, or the
  /object_info fixture flips the test — by design, gives a loud signal
  before any silent behaviour change ships.

Re-baseline procedure documented in the test's docstring.

Closes sub-spec AC7, AC8, AC11.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T6 status → completed.

---

## Task 7: PROGRESS entry + sub-plan tasks.json final sync + final gate

**Goal:** Append a sub-plan closure block to `PROGRESS.md` mirroring the parent sub-plan template; sync `docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md.tasks.json` final state; final pre-commit + pytest gate; commit T7.

**Files:**
- Modify: `PROGRESS.md` (append sub-plan closure block)
- Modify: `docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md.tasks.json` (final sync)

**Acceptance Criteria:**
- [ ] PROGRESS sub-plan closure block contains: sub-spec path + commit SHAs; sub-plan path + commit SHAs; atomic commit list (T1-T7); live cost from T4; pack-stack-hash from T5; test count delta; key design decisions block.
- [ ] PROGRESS notes that parent sub-plan T1 is now UNBLOCKED.
- [ ] `pixi run pytest` total count: baseline 979 + 10 (3 hash + 6 shim + 1 golden) = 989 passed; 3 skipped; 4 xfailed unchanged.
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] tasks.json on disk matches in-memory state.

**Verify:**
1. `pixi run pytest 2>&1 | tail -5` → `989 passed, 3 skipped, 4 xfailed`.
2. `pixi run pre-commit run --all-files 2>&1 | tail -10` → all Passed.
3. `grep -A2 "comfyui_ui_to_api converter" PROGRESS.md` → closure block visible.
4. Parent sub-plan tasks #16 (sub-plan tracker) and #9 (parent T1) status reflected accurately in TaskList.

**Steps:**

- [ ] **Step 1: Run the full offline suite.**

```bash
pixi run pytest 2>&1 | tail -5
```

Expected: `989 passed, 3 skipped, 4 xfailed`. If counts don't match, investigate before writing PROGRESS — an off-by-one means a test was double-counted, dropped, or new xfail introduced.

```bash
pixi run pre-commit run --all-files 2>&1 | tail -10
```

Expected: every hook Passed/Skipped, no Failed.

- [ ] **Step 2: Compose the PROGRESS closure block.**

Append to `PROGRESS.md` (right after the existing Layer Q / parent sub-plan sections), using the parent sub-plan closure block as a template:

```markdown
**comfyui_ui_to_api converter sub-plan — ✅ CLOSED 2026-06-XX at HEAD `<final-sha>`.**

Sub-plan spawned from the Layer P Task 7 item #3 resume sub-plan T1
BLOCKED state (kijai upstream ships UI-format workflows only at SHA
088128b22). Ports SethRobinson/comfyui-workflow-to-api-converter-endpoint
(Unlicense) as the offline UI→API converter so kinoforge can convert
ComfyUI editor workflows hermetically.

- Sub-spec: `docs/superpowers/specs/2026-06-02-comfyui-ui-to-api-converter-design.md` (`d19993d`)
- Sub-plan: `docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md` (+ `.tasks.json`) (`<sub-plan-sha>`)
- T1 `<sha>` — `feat(tools): vendor Seth's workflow_converter (Unlicense) + LICENSES file`
- T2 `<sha>` — `feat(tools): pack_stack_hash pure function + 3 unit tests`
- T3 `<sha>` — `feat(tools): comfyui_ui_to_api shim — install_fake_nodes_module + CLI + 6 tests`
- T4 `<sha>` — `feat(tools): capture_object_info live /object_info capture tool`
- T5 `<sha>` — `chore(pre-commit): permit /object_info fixture files up to 2 MB` (or exclude-pattern variant)
- T5 `<sha>` — `test(fixtures): /object_info capture for kijai wanvideo i2v pack stack` (pack-stack-hash `<hash>`)
- T6 `<sha>` — `test(tools): kijai UI snapshot + expected API golden + integration test`
- T7 (this commit) — PROGRESS closure block + tasks.json sync

**First /object_info capture:** `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json` (`<size>` bytes, `<N>` classes). Pack stack: ComfyUI 0.3.10 + kijai/ComfyUI-WanVideoWrapper@088128b22 + kijai/ComfyUI-KJNodes@369c8ae. Captured 2026-06-XX at `<local-tz timestamp>`. Cost ≈ $`<cost>` (well under the $0.50 AC13 cap).

**Key design decisions:**

- Vendor Seth's `workflow_converter.py` verbatim (Q1 in 2026-06-02 brainstorm) rather than re-implement. ~400 LOC of conversion logic covers subgraphs (nested), COMFY_DYNAMICCOMBO_V3, GetNode/SetNode routing, reroute nodes, default values for required inputs, combo-value normalization, Unicode. Pinned upstream SHA `<UPSTREAM_SHA>`; re-vendoring is a one-line file copy + LICENSES SHA bump.
- `/object_info` captured live (Q2) from a brief RunPod pod via the existing Layer Q render_provision / wait_for_ready orchestrator path. Reusable `tools/capture_object_info.py` ships today; every future pack-stack capture is one command.
- Per-pack-stack full dump (Q3) under `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json`. Hash = 12-char SHA256 prefix of canonical `comfyui@<version> + sorted <git>@<ref>` string (`tools/_pack_stack.py`). Workflows sharing pack stacks share fixtures.
- Shim unit + kijai golden test strategy (Q4). 3 hash + 6 shim + 1 golden = +10 offline tests; no new xfail/skip.
- Minimal CLI (Q5): `--ui-json`, `--object-info`, `--out`. `_meta` header is consumer concern, not converter concern.
- Reusable capture tool committed (Q6).
- License attribution: `LICENSES/SETH-UNLICENSE.txt` + `tools/_vendored/__init__.py` docstring pointer. Upgrade procedure documented in sub-spec §6.

**Test count:** 979 baseline + 10 = 989 passed + 3 skipped + 4 xfailed.

**Unblocks:** Parent sub-plan Layer P Task 7 item #3 resume T1 (kijai pinned-pull + _meta strip in core/config). T1's blocker resolves; T1 resumes from Step 2 of its plan (the converter emits the API JSON; T1 wraps with `_meta` header + adds the strip in `core/config._resolve_spec_graph_file`).

**Out of scope (deferred):**
- AST-walking source as a /object_info fallback (sub-spec §3).
- Auto-deriving _meta fields from CLI flags or input path (sub-spec §3).
- `tools/capture_object_info.py` running in CI (sub-spec §3).
- Vendor `pydn/ComfyUI-to-Python-Extension` for API→Python direction (different problem).
```

Fill placeholders with actual SHA / hash / cost / size values from prior tasks.

- [ ] **Step 3: Sync sub-plan tasks.json.**

```bash
python - <<'PY'
import json
from pathlib import Path
from datetime import datetime

p = Path("docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md.tasks.json")
data = json.loads(p.read_text())
for t in data["tasks"]:
    if t["status"] in ("pending", "in_progress"):
        t["status"] = "completed"
data["lastUpdated"] = datetime.now().astimezone().isoformat(timespec="minutes")
p.write_text(json.dumps(data, indent=2) + "\n")
print("synced")
PY
```

- [ ] **Step 4: Final gate.**

```bash
pixi run pytest 2>&1 | tail -5
pixi run pre-commit run --all-files 2>&1 | tail -15
```

Both must be clean. If pre-commit auto-fixes any file (whitespace/EOF), re-stage and re-run.

- [ ] **Step 5: Commit T7.**

```bash
git add PROGRESS.md docs/superpowers/plans/2026-06-02-comfyui-ui-to-api-converter.md.tasks.json
git commit -m "$(cat <<'COMMIT'
docs(progress): comfyui_ui_to_api converter sub-plan closure

T7 of the comfyui_ui_to_api converter sub-plan.

Closure block per parent sub-plan template: sub-spec + sub-plan refs,
atomic commit list T1-T7, /object_info fixture evidence (path, size,
class count, pack-stack-hash, capture timestamp, cost), key design
decisions block, test count delta (979 -> 989, +10 net offline),
unblocks-parent statement.

tasks.json synced — all 7 sub-plan tasks completed.

Parent sub-plan Layer P Task 7 item #3 resume T1 is now UNBLOCKED;
resume from its Step 2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
COMMIT
)"
```

Update tasks.json: T7 status → completed. Update parent sub-plan tracking task #16 status → completed. Parent T1 (#9) is now unblocked (blockedBy=[16] resolves).

---

## Post-plan checklist (sub-plan executor)

- [ ] Local `main` is N commits ahead of `origin/main` at sub-plan close. Push when convenient outside the container: `git push origin main`.
- [ ] No new xfail or skip introduced; the 4 prior xfailed tests in `tests/examples/test_runpod_comfyui_wan_graph.py` are still xfailed (they flip in parent sub-plan T2).
- [ ] No paid pods running (`pixi run python -m kinoforge list ...` returns zero rows).
- [ ] Parent sub-plan Layer P Task 7 item #3 resume T1 is unblocked. Coordinator dispatches parent T1's implementer subagent from Step 2 (the kijai pull is no longer the blocker — the converter is now available).
