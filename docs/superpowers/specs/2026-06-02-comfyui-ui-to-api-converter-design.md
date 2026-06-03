# tools/comfyui_ui_to_api.py — ComfyUI UI→API offline converter

Sub-spec spawned from parent sub-plan `docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md` Task 1 BLOCKED state (kijai upstream ships UI-format workflows only at the pinned SHA, no API-format files anywhere in the tree). Parent sub-plan §Step 1b directed: "DO NOT ad-hoc the converter inline; pause and brainstorm a focused mini-spec + mini-plan for the converter, ship it, then resume parent T1 from Step 2."

Parent sub-plan: `docs/superpowers/plans/2026-06-02-layer-p-task7-item3-resume.md`
Parent sub-spec (Layer P Task 7 item #3): `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md` (amended 2026-06-02 at `23b1501`, polished at `744575c`)
Parent project (kinoforge): anticipates LOTS of ComfyUI workflows over time → converter is high-leverage reusable infrastructure, not a one-off.

## 1. Goal

Ship an offline, hermetic, deterministic ComfyUI UI-format → API-format JSON converter usable from kinoforge's `spec.graph_file` pipeline and from any future workflow author who designs in the ComfyUI editor. Concretely:

- Vendor Seth Robinson's `SethRobinson/comfyui-workflow-to-api-converter-endpoint` (Unlicense, public domain) into the kinoforge tree under `tools/_vendored/`. Seth's `WorkflowConverter.convert_to_api()` is the canonical UI→API conversion logic in the wild — handles subgraphs (including nested), COMFY_DYNAMICCOMBO_V3, GetNode/SetNode routing, reroute nodes, default values for required inputs, combo-value normalization, Unicode (Chinese/Japanese/emoji), 200 KB workflows. Re-implementing is wasted work.
- Decouple the vendored module from its hard `import nodes` dependency by injecting a fake `nodes` module into `sys.modules` whose `NODE_CLASS_MAPPINGS` is backed by a captured `/object_info` JSON.
- Capture `/object_info` once per pack-stack version via a reusable `tools/capture_object_info.py` tool that provisions a brief RunPod pod through the existing Layer Q `render_provision`/`wait_for_ready` path.
- First consumer: parent sub-plan T1 (the kijai `wanvideo_2_1_14B_I2V_example_03.json` workflow at SHA `088128b22`). Convert it to a 26-node API JSON whose class types match the existing `EXPECTED_CLASS_TYPES` set in `tests/examples/test_runpod_comfyui_wan_graph.py:35-57`.
- Future workflow author flow: design in ComfyUI UI → export UI JSON → run converter against the appropriate pack-stack fixture → commit. New pack-stack version → one-time `capture_object_info.py` invocation.

## 2. Scope decisions (locked in brainstorm)

| Q | Decision | Reason |
|---|---|---|
| Q1 | Vendor entire `workflow_converter.py` + write a thin kinoforge shim (~100 LOC) | Tracks upstream Seth's bug fixes via re-vendoring; all of his edge-case handling (subgraphs, COMFY_DYNAMICCOMBO_V3, GetNode/SetNode, rerouting, Unicode) comes for free; smallest new code surface kinoforge owns |
| Q2 | Live RunPod pod for the first `/object_info` capture, via existing Layer Q orchestrator path | Canonical source from real ComfyUI runtime; reusable tool sets precedent for every future workflow's pack-stack capture; ~$0.10 cost per pack-stack version |
| Q3 | Per-pack-stack full dump under `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json`; pack-stack-hash derived from sorted `comfyui@version + <git>@<ref>` pairs | Workflows sharing a pack stack share one fixture; dedup scales with N workflows; full dump is future-proof against Seth's converter starting to use a field we'd otherwise trim |
| Q4 | Shim unit tests + kijai golden integration test | Shim is kinoforge-owned and unit-testable; golden locks correctness of the whole pipeline against the first consumer workflow |
| Q5 | Minimal CLI: `--ui-json`, `--object-info`, `--out` | Tight scope; `_meta` header is workflow-author concern, not converter concern; non-breaking to add flags later |
| Q6 | Reusable `tools/capture_object_info.py` committed | "Lots of workflows" path benefits from a one-command capture for every future pack-stack |

## 3. Out of scope

- **Wrapping `_meta` header into the converter output.** Converter emits raw API JSON. `_meta` is the parent sub-plan T1's concern (added post-conversion via a small inline Python step). Future tooling under a follow-up sub-plan if it proves repetitive.
- **Auto-deriving `_meta.source_repo` / `source_sha` / `source_path`** from CLI flags or input path.
- **AST-walking source for `INPUT_TYPES`** as a fallback when live capture isn't available. Fully offline alternative is a future layer if needed.
- **`tools/capture_object_info.py` running in CI.** Capture is operator-invoked, not automated. CI just consumes committed fixtures.
- **Caching `/object_info` across multiple pod boots.** Each capture spins a fresh pod; no shared state.
- **Multi-pack-stack composition** (one workflow that mixes packs from multiple separately-captured fixtures). One workflow → one pack stack → one fixture.
- **Vendor `pydn/ComfyUI-to-Python-Extension`** for the API→Python direction. Different problem, out of scope.
- **Linting Seth's vendored code.** `tools/_vendored/` is excluded from ruff + mypy via `pyproject.toml` excludes.
- **`tools/capture_object_info.py` supporting non-RunPod providers.** SkyPilot / other compute providers via the same Layer Q surface are a follow-up.

## 4. Architecture

### 4.1 File layout

```
tools/
├── _vendored/
│   ├── __init__.py                              # 5-line marker docstring naming vendored sources + license pointer
│   └── seth_workflow_converter.py               # Seth's 72 KB file VERBATIM at a pinned upstream SHA, header preserved
├── comfyui_ui_to_api.py                         # kinoforge shim — install_fake_nodes_module + CLI (~100 LOC)
├── capture_object_info.py                       # /object_info capture tool, provisions pod via Layer Q (~150 LOC)
└── _pack_stack.py                               # pack_stack_hash() pure function (~30 LOC)

tests/
├── fixtures/
│   └── comfyui/
│       └── object_info/
│           └── <pack-stack-hash>.json           # ~1-2 MB live capture per pack-stack version, one per unique stack
└── tools/
    ├── __init__.py
    ├── test_pack_stack_hash.py                  # 3 unit tests (~50 LOC)
    ├── test_comfyui_ui_to_api.py                # 5-6 shim unit tests (~150 LOC)
    ├── test_kijai_workflow_golden.py            # 1 integration test (~30 LOC)
    └── fixtures/
        ├── minimal_object_info.json             # 3-class hand-curated stub for shim unit tests
        ├── kijai_wanvideo_2_1_14B_i2v.ui.json   # snapshot of kijai's UI workflow at SHA 088128b22 (vendored read-only input)
        └── kijai_wanvideo_2_1_14B_i2v.expected_api.json  # committed golden for the integration test

LICENSES/
└── SETH-UNLICENSE.txt                           # full Unlicense text + upstream URL + pinned SHA + capture date
```

### 4.2 Responsibility lines

| File | Responsibility | Owns |
|---|---|---|
| `tools/_vendored/seth_workflow_converter.py` | UI→API conversion logic | Seth (vendored verbatim) |
| `tools/comfyui_ui_to_api.py` | Shim: fake `nodes` module + CLI | kinoforge |
| `tools/capture_object_info.py` | Live `/object_info` capture | kinoforge |
| `tools/_pack_stack.py` | Pure hash function | kinoforge |
| `tests/fixtures/comfyui/object_info/*.json` | Captured `/object_info` data, committed | kinoforge (data) |
| `tests/tools/fixtures/kijai_*.ui.json` | Snapshot of upstream UI workflow | kijai (data, vendored read-only) |
| `tests/tools/fixtures/kijai_*.expected_api.json` | Golden output | kinoforge (data) |

## 5. Component specs

### 5.1 `tools/_pack_stack.py` — `pack_stack_hash`

```python
from __future__ import annotations
import hashlib
from typing import Any


def pack_stack_hash(comfyui_cfg: dict[str, Any]) -> str:
    """Deterministic 12-char hash of the engine.comfyui pack stack.

    Computed from:
      - engine.comfyui.version (e.g. "0.3.10") — the ComfyUI base version
      - engine.comfyui.custom_nodes[*].git + .ref, sorted by git URL ascending

    Identical pack stacks across workflows produce identical hashes
    → fixture sharing under tests/fixtures/comfyui/object_info/<hash>.json.

    Args:
        comfyui_cfg: The engine.comfyui pydantic-dump dict (or equivalent
            dict-shaped object). Read keys: "version" (str), "custom_nodes"
            (list of dicts with "git"/"ref" keys).

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

**Invariants:**
- Pure function, no I/O.
- Deterministic — same input dict → same output bytes.
- Sort key is `git` URL ascending. Order of `custom_nodes` in the YAML doesn't affect the hash.
- Only fields that affect `/object_info` output are hashed. `engine.comfyui.precision` and other non-loading fields are excluded.
- 12 chars = 48 bits of collision resistance. Plenty for hundreds of pack stacks.

### 5.2 `tools/comfyui_ui_to_api.py` — `install_fake_nodes_module`

```python
from __future__ import annotations
import json
import sys
import types
from pathlib import Path
from typing import Any


def install_fake_nodes_module(object_info_path: Path) -> types.ModuleType:
    """Inject a fake `nodes` module into sys.modules backed by captured /object_info.

    MUST be called BEFORE importing tools._vendored.seth_workflow_converter; that
    module's top-level `import nodes` resolves against sys.modules.

    Args:
        object_info_path: Path to a captured /object_info JSON fixture.

    Returns:
        The fake module (for test introspection).
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
                    # /object_info uses JSON lists; ComfyUI runtime INPUT_TYPES
                    # returns tuples for the OUTER type-spec wrapper. Inner data
                    # lists (e.g. COMBO option arrays) stay as lists.
                    if isinstance(val, list):
                        val = tuple(val)
                    ordered_section[key] = val
            if ordered_section:
                ordered[section] = ordered_section

        # Closure capture via default arg — avoids Python late-binding bug.
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
```

**Locked behavior (pinned by unit tests):**
1. `list → tuple` cast applied at the OUTER type-spec wrapper only. Inner option lists inside COMBO specs (e.g. `["model_a.safetensors", "model_b.safetensors"]`) preserved as lists, since Seth's converter reads them as data.
2. `input_order` respected — the returned `INPUT_TYPES()` dict iterates keys in the order ComfyUI runtime would.
3. Missing optional `/object_info` fields default safely (`RETURN_TYPES=()`, `OUTPUT_NODE=False`, etc.) — no `KeyError`.
4. Idempotent — calling `install_fake_nodes_module(p2)` after `install_fake_nodes_module(p1)` replaces the `sys.modules["nodes"]` entry cleanly with `p2`'s data.

### 5.3 `tools/comfyui_ui_to_api.py` — CLI

```python
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="comfyui_ui_to_api",
        description="Convert ComfyUI UI-format workflow JSON to API-format JSON.",
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

**Exit codes:**
- `0`: success
- `2`: argparse usage error (default behavior)
- Non-zero (uncaught exception → traceback to stderr): conversion failure inside Seth's code (CLI does not swallow)

### 5.4 `tools/capture_object_info.py` — live capture tool

Provisions a RunPod pod via the existing Layer Q `render_provision` / `wait_for_ready` orchestrator path, GETs `/object_info`, writes the fixture, destroys the pod.

```python
"""Capture /object_info from a brief RunPod pod for a kinoforge workflow YAML.

Usage:
    pixi run python tools/capture_object_info.py \
        --workflow-yaml examples/configs/runpod-comfyui-wan.yaml \
        [--out tests/fixtures/comfyui/object_info/<hash>.json]

Env-gated like other live tools:
KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY + RUNPOD_TERMINATE_KEY + HF_TOKEN
must all be set; capture proceeds without an interactive prompt.

Default --out path: tests/fixtures/comfyui/object_info/<pack_stack_hash>.json
(hash derived via tools._pack_stack.pack_stack_hash on the YAML's
engine.comfyui block).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# (imports kinoforge.core.* deferred until env-gate passes)


def main() -> int:
    parser = argparse.ArgumentParser(prog="capture_object_info")
    parser.add_argument("--workflow-yaml", required=True, type=Path)
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

    required_env = (
        "KINOFORGE_LIVE_TESTS",
        "RUNPOD_API_KEY",
        "RUNPOD_TERMINATE_KEY",
        "HF_TOKEN",
    )
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(
            f"capture_object_info: missing env vars: {missing}",
            file=sys.stderr,
        )
        return 1

    import kinoforge._adapters  # noqa: F401 — register sources/engines/providers
    from kinoforge.core.config import load_config
    from tools._pack_stack import pack_stack_hash

    cfg = load_config(args.workflow_yaml)
    stack_hash = pack_stack_hash(cfg.engine.comfyui.model_dump())

    out_path = args.out or (
        Path(f"tests/fixtures/comfyui/object_info/{stack_hash}.json")
    )

    # Provision pod via Layer Q surface. The exact call shape is sub-plan
    # detail; the contract this spec pins is:
    #   1. Call render_provision on a ComfyUIEngine seeded with cfg.
    #   2. Validate env_required against EnvCredentialProvider.
    #   3. Construct InstanceSpec with provision_script + run_cmd + image +
    #      ports + env from RenderedProvision and from os.environ.
    #   4. Provider.create_instance(spec) -> Instance.
    #   5. Engine.provision(instance, cfg_dict) which calls wait_for_ready
    #      against /system_stats with cfg.lifecycle().boot_timeout_s.
    #   6. Backend = engine.backend(instance, cfg_dict); backend._base_url
    #      is the pod's proxy URL (e.g. https://<id>-8188.proxy.runpod.net).
    # Implementation may delegate to a public helper in core/orchestrator if
    # one exists, or open-code the 6-step sequence; sub-plan picks. The
    # try/finally around steps 4-7 (where step 7 is GET + write) destroys
    # the pod via provider.destroy_instance(instance.id) on any exit path.

    pod_base_url = backend._base_url  # e.g. https://<id>-8188.proxy.runpod.net

    request = urllib.request.Request(
        f"{pod_base_url}/object_info",
        headers={"Content-Type": "application/json", "User-Agent": "kinoforge-capture/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        object_info = json.loads(resp.read())

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(object_info, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {out_path} ({out_path.stat().st_size} bytes, "
        f"{len(object_info)} classes, pack-stack-hash={stack_hash})"
    )

    # provider.destroy_instance(instance.id) in finally block
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Properties:**
- Env-gated; fails fast if any of the 4 env vars is missing (no spend).
- Reuses Layer Q orchestrator surface — no duplicate provisioning logic.
- Writes pretty-printed sort-keyed JSON so future diffs across captures show only added/removed classes, no key reordering noise.
- Cost ~$0.10 per capture (a few minutes of pod time).
- Finally-clause destroys the pod even if capture fails mid-flight.

## 6. License attribution

Seth's repo is Unlicense (public domain). Attribution is courtesy, not legally required. We give it anyway because it's good form.

**File-level attribution:**

1. `tools/_vendored/seth_workflow_converter.py` — file vendored VERBATIM. Seth's existing docstring header at the top of the file already references his GitHub repo and the Unlicense; preserved unchanged.
2. `LICENSES/SETH-UNLICENSE.txt` (new) — full Unlicense text + Seth's repo URL + pinned upstream commit SHA at time of vendor + local-TZ capture date.
3. `tools/_vendored/__init__.py` — 5-line docstring naming the vendored sources, their licenses, and a pointer at `LICENSES/SETH-UNLICENSE.txt`.
4. `tools/comfyui_ui_to_api.py` — kinoforge-owned. Module docstring includes a one-line note: *"Vendors tools/_vendored/seth_workflow_converter.py (Unlicense, see LICENSES/SETH-UNLICENSE.txt)."*

**Upgrade procedure (documented here for repo posterity):**

To pull a new Seth release:
1. Copy `workflow_converter.py` from upstream at a new pinned SHA → overwrite `tools/_vendored/seth_workflow_converter.py`.
2. Update SHA + local-TZ capture date in `LICENSES/SETH-UNLICENSE.txt`.
3. Re-run `pixi run pytest tests/tools/test_kijai_workflow_golden.py`. If it passes, commit with subject `chore(_vendored): bump Seth converter to <new-version>`. If it fails, examine the diff, decide whether to re-baseline `kijai_wanvideo_2_1_14B_i2v.expected_api.json` per §7 procedure, commit with subject `chore(_vendored): bump Seth converter to <new-version> + re-baseline kijai golden`.

## 7. Test strategy

### 7.1 `tests/tools/test_pack_stack_hash.py` — 3 unit tests

1. `test_empty_pack_stack_has_stable_hash` — `{"version": "", "custom_nodes": []}` → pinned known hex string. Failure means the canonical-form algorithm changed.
2. `test_pack_stack_hash_is_order_insensitive` — two dicts with `custom_nodes` in different order but identical content → identical hash. Failure means the sort key is wrong.
3. `test_pack_stack_hash_changes_on_ref_bump` — bumping one entry's `ref` field → different hash. Failure means hash isn't actually deriving from refs.

### 7.2 `tests/tools/test_comfyui_ui_to_api.py` — 6 shim unit tests

Uses `tests/tools/fixtures/minimal_object_info.json` (3 hand-curated node-class entries: `LoadImage`, `CLIPTextEncode`, `Note`).

1. `test_install_fake_nodes_module_populates_NODE_CLASS_MAPPINGS` — call shim, assert `sys.modules["nodes"].NODE_CLASS_MAPPINGS` has all 3 keys.
2. `test_INPUT_TYPES_returns_classmethod_with_correct_required_section` — call `mappings["LoadImage"].INPUT_TYPES()`, assert `required.image` is a TUPLE whose first element is a LIST (inner COMBO option list preserved).
3. `test_input_order_is_respected` — class with `input_order.required = ["b", "a"]` → INPUT_TYPES() dict iterates keys in `["b", "a"]` order, not insertion order from `input.required`.
4. `test_idempotent_install_replaces_cleanly` — call install twice with different fixtures; second `sys.modules["nodes"]` reflects second fixture only.
5. `test_missing_optional_fields_default_safely` — class with no `output`/`output_name`/`output_node` → stub has `RETURN_TYPES=()`, `OUTPUT_NODE=False`.
6. `test_cli_main_writes_api_json` — synthetic 2-node UI JSON + minimal_object_info → run `main()` with mocked argv → assert output file contains expected dict-keyed API JSON.

### 7.3 `tests/tools/test_kijai_workflow_golden.py` — 1 integration test

```python
import json
import sys
from pathlib import Path

KIJAI_UI_PATH = Path("tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json")
EXPECTED_API_PATH = Path("tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json")
PACK_STACK_FIXTURE_PATH = Path("tests/fixtures/comfyui/object_info/<pack-stack-hash>.json")


def test_kijai_workflow_converts_to_expected_api_json(tmp_path: Path) -> None:
    """Full pipeline: kijai UI JSON + pack-stack fixture → expected API JSON.

    The expected golden was produced once during sub-plan T4 and reviewed
    before commit. Any subsequent change in Seth's converter behavior, in
    the kijai UI fixture, or in the captured /object_info fixture flips
    this test — by design.
    """
    out = tmp_path / "out.json"
    saved_argv = sys.argv
    try:
        sys.argv = [
            "comfyui_ui_to_api",
            "--ui-json",
            str(KIJAI_UI_PATH),
            "--object-info",
            str(PACK_STACK_FIXTURE_PATH),
            "--out",
            str(out),
        ]
        from tools.comfyui_ui_to_api import main

        rc = main()
    finally:
        sys.argv = saved_argv
    assert rc == 0
    actual = json.loads(out.read_text())
    expected = json.loads(EXPECTED_API_PATH.read_text())
    assert actual == expected, (
        "kijai workflow conversion drift — "
        f"actual-only-keys={sorted(set(actual) - set(expected))[:5]}, "
        f"expected-only-keys={sorted(set(expected) - set(actual))[:5]}"
    )
```

**Why dict equality, not class-types-subset:** if Seth's converter changes a single default value, a single coercion, or a single subgraph expansion, the golden flips. That's the point — gives a loud signal on any behavior change.

**Re-baseline procedure (documented here):**
If Seth ships a bug-fix release that legitimately changes converter output:
1. Run `pixi run python tools/comfyui_ui_to_api.py --ui-json tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json --object-info tests/fixtures/comfyui/object_info/<hash>.json --out tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json`.
2. `git diff tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json` — eyeball the diff.
3. Commit: `chore(tests): re-baseline kijai golden against Seth v<X.Y.Z>`.

## 8. Acceptance criteria

| AC | Description |
|---|---|
| AC1 | `tools/_vendored/seth_workflow_converter.py` byte-equal to upstream at the pinned SHA; SHA + local-TZ capture date recorded in `LICENSES/SETH-UNLICENSE.txt` |
| AC2 | `tools/comfyui_ui_to_api.py` shim implements `install_fake_nodes_module` per §5.2 contract; passes 6 unit tests in `tests/tools/test_comfyui_ui_to_api.py` |
| AC3 | `tools/_pack_stack.py` exports `pack_stack_hash(comfyui_cfg: dict) -> str` matching §5.1 algorithm; passes 3 unit tests in `tests/tools/test_pack_stack_hash.py` |
| AC4 | `tools/capture_object_info.py` provisions pod via Layer Q orchestrator path, captures `/object_info`, destroys pod (finally-clause guarantee), writes pretty-printed sort-keyed JSON |
| AC5 | `LICENSES/SETH-UNLICENSE.txt` present with full Unlicense text + upstream URL + pinned SHA + local-TZ capture date |
| AC6 | Live capture executed once during sub-plan; `tests/fixtures/comfyui/object_info/<pack-stack-hash>.json` committed (file size ~1-2 MB, JSON valid, ≥100 class entries) |
| AC7 | Snapshot of kijai's UI workflow at SHA `088128b224242e110d3906c6750e9a3a348a659b` committed read-only at `tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.ui.json` (vendored as-is from the upstream `example_workflows/` directory) |
| AC8 | Kijai workflow golden `tests/tools/fixtures/kijai_wanvideo_2_1_14B_i2v.expected_api.json` committed (produced via converter run during sub-plan, reviewed before commit) |
| AC9 | `tests/tools/test_pack_stack_hash.py` (3 tests) PASS |
| AC10 | `tests/tools/test_comfyui_ui_to_api.py` (6 shim unit tests) PASS |
| AC11 | `tests/tools/test_kijai_workflow_golden.py` (1 integration test) PASS |
| AC12 | `pixi run mypy . && pixi run ruff check . && pixi run pre-commit run --all-files` clean. `tools/_vendored/` excluded from ruff + mypy via `pyproject.toml` excludes |
| AC13 | Sub-plan live cost ≤ $0.50 (one pod, ~2-5 min runtime, A40 or RTX 4090 tier) |
| AC14 | Sub-plan PROGRESS entry appended to `PROGRESS.md` mirroring parent sub-plan closure shape (sub-spec path + SHA, sub-plan path + SHA, atomic commit list, cost, key design decisions) |
| AC15 | Total offline-test count delta: +10 (3 hash + 6 shim + 1 golden); no new xfail/skip introduced |

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Seth's vendored code uses a field of /object_info that we forget to surface in the shim | Full /object_info dump committed; nothing trimmed; shim populates `RETURN_TYPES` / `RETURN_NAMES` / `FUNCTION` / `CATEGORY` / `OUTPUT_NODE` in addition to `INPUT_TYPES` |
| Closure late-binding bug in the shim's `lambda _cls, _s=ordered: _s` line | Default-arg capture pattern explicitly chosen; one unit test (test_install_fake_nodes_module_populates_NODE_CLASS_MAPPINGS) iterates ALL classes' INPUT_TYPES and asserts uniqueness; would catch a late-binding regression |
| Pack-stack-hash collision when scaling to 100+ workflows | 12-char = 48 bits = ~1e14 unique stacks; collision probability for tens of stacks is < 1e-10. If feared, bump prefix length later — non-breaking via fixture rename |
| `/object_info` capture pod fails to boot (capacity, network) | Capture tool retries via Layer P Task 7 item #1's offer-retry mechanism (already on main); finally-clause still tears down |
| Seth ships a v2.4+ release that breaks the golden test | Re-baseline procedure documented in §7.3; flip is an intentional signal, not a failure |
| Live capture leaks a pod | Layer P bug-fix #1 (`_RecordingHTTPSeam` redaction) is on main and protects ANY fixture written under tests/; selfterm + idle_timeout backstops on the pod side |
| `tools/_vendored/seth_workflow_converter.py` has a hardcoded path I missed when checking offline-viability | Shim unit tests + golden test together would surface any missed runtime coupling; re-vendoring procedure includes the test gate |

## 10. Commit shape

Atomic commits per task; final task closes sub-plan in PROGRESS.

| Phase | Subject |
|---|---|
| T1 | `feat(tools): vendor Seth's workflow_converter under tools/_vendored/` + add LICENSES/SETH-UNLICENSE.txt |
| T2 | `feat(tools): pack_stack_hash pure function + 3 unit tests` |
| T3 | `feat(tools): comfyui_ui_to_api shim — install_fake_nodes_module + CLI + 5-6 unit tests` |
| T4 | `feat(tools): capture_object_info live capture tool` |
| T5 | `test(fixtures): commit captured /object_info for kijai wanvideo i2v pack stack` |
| T6 | `test(tools): kijai UI workflow snapshot + expected API golden + 1 integration test` |
| T7 | `docs(progress): comfyui_ui_to_api converter sub-plan closure` |

## 11. Open questions

None at sub-spec time. All design questions resolved during brainstorm (Q1-Q6 in §2). The Seth upstream SHA pin choice is sub-plan execution detail (T1 picks latest tag or HEAD with rationale in commit).
