# Layer P — RunPod Engine Integration (ComfyUI + Wan i2v) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship first real MP4 from kinoforge end-to-end on real cloud compute (RunPod pod + ComfyUI + Wan 2.2 i2v), driven through `orchestrator.generate()`, validated by a single in-process live test, and lock the captured ComfyUI HTTP shape into offline tests so future schema drift fails loudly.

**Architecture:** Layer-N-pattern autonomous live smoke. All offline scaffolding (graph_file loader, SHA pinning, tag-discovery helper, recording-seam ComfyUI extension, live-test skeleton, example YAML + graph placeholders) lands first. Then I (the agent) drive the live shake-out loop using committed `.env` creds: `KINOFORGE_LIVE_KEEP_POD=1` reuses warm pods across iterations to amortise the ~28 GB Wan weights download. First green MP4 captures ComfyUI fixtures + real custom-node SHAs + real model file set. Offline ComfyUIBackend tests refactor to load from captured fixtures (Layer N parity). README + PROGRESS + final gate + `--no-ff` merge.

**Tech Stack:** Python 3.13, pydantic v2, pytest, urllib (stdlib HTTP), RunPod GraphQL API, ComfyUI HTTP API (`/prompt`, `/history`, `/view`, `/upload/image`, `/system_stats`), HuggingFace resolve URLs, ComfyUI custom-nodes via git clone.

**Spec:** `docs/superpowers/specs/2026-06-01-layer-p-runpod-engine-integration-design.md` (committed `3c163b1`, self-review fix `84e96a4`)

**Branch:** `build/layer-p` off `main@7788f93`

---

## File structure

**New files** (created by tasks):

| Path | Owner task | Responsibility |
|---|---|---|
| `examples/configs/runpod-comfyui-wan.graph.json` | Task 6 / refined Task 7 | Hand-authored Wan 2.2 i2v ComfyUI workflow JSON |
| `tests/engines/conftest.py` | Task 8 | `_load_comfy_fixture` helper shared across `tests/engines/` |
| `tests/engines/fixtures/comfyui/prompt_submit.json` | Task 7 (live capture) | `POST /prompt` response |
| `tests/engines/fixtures/comfyui/history_done.json` | Task 7 | `GET /history/{id}` terminal response |
| `tests/engines/fixtures/comfyui/view.json` | Task 7 | `GET /view?...` metadata |
| `tests/engines/fixtures/comfyui/last_smoke.json` | Task 7 | Smoke metadata (artifact path, sha, size, capability_key) |
| `tests/live/test_comfyui_wan_live.py` | Task 5 | Single-function live E2E smoke |

**Modified files:**

| Path | Owner task | Change |
|---|---|---|
| `src/kinoforge/core/config.py` | Task 1 | Add `spec.graph_file` resolution in existing `model_validator(mode="after")` |
| `src/kinoforge/engines/comfyui/__init__.py` | Task 2 | Honor `ref` field in custom-node dict via `git checkout` post-clone |
| `src/kinoforge/providers/runpod/__init__.py` | Task 3 | Add `find_instance_by_tag(key, value) -> Instance \| None` |
| `tests/providers/conftest_runpod.py` | Task 4 | Refactor `_RecordingHTTPSeam` dispatch to accept a dispatcher callable; preserve Layer N behavior via wrapped helper |
| `tests/core/test_config.py` | Task 1 | +3 graph_file tests |
| `tests/engines/test_comfyui.py` | Task 2 + Task 8 + Task 9 | +2 SHA-pin tests (Task 2), refactor 23 inline-dict tests to `_load_comfy_fixture` (Task 8), +3 shape-lockdown tests (Task 9) |
| `tests/providers/test_runpod.py` | Task 3 | +3 tag-discovery tests |
| `tests/providers/test_runpod_conftest.py` | Task 4 | +2 ComfyUI dispatch tests |
| `examples/configs/runpod-comfyui-wan.yaml` | Task 6 + Task 7 | Fill `spec.graph_file`, `engine.comfyui.custom_nodes` SHA pins, real `models:` set |
| `README.md` | Task 10 | Extend "Real providers — RunPod" with engine-integration sub-section |
| `PROGRESS.md` | Task 10 | Phase 26 entry; close Layer-O carry-forward #1 |

---

## Task 1: `spec.graph_file` loader convention

**Goal:** YAML's `spec.graph_file: <path>` inlines a JSON file's contents into `cfg.spec.graph` at config load. Engine-agnostic. Mutually exclusive with `spec.graph`.

**Files:**
- Modify: `src/kinoforge/core/config.py:472-510` (extend the `Config` `model_validator(mode="after")` already at line 477)
- Modify: `tests/core/test_config.py` (add 3 tests)

**Acceptance Criteria:**
- [ ] Round-trip: YAML with `spec.graph_file: foo.json` (relative) → `cfg.spec["graph"]` equals the JSON contents; `cfg.spec` does NOT contain a `"graph_file"` key after load.
- [ ] Absolute `graph_file` path used verbatim.
- [ ] Both `graph_file` AND `graph` set → `ValidationError` with message mentioning `graph_file` and `graph` mutual exclusion.
- [ ] `graph_file` pointing to a non-existent file → `ValidationError` with the file path in the message.
- [ ] `graph_file` pointing to invalid JSON → `ValidationError` with the file path AND the JSON parse error in the message.
- [ ] Relative `graph_file` resolves against the YAML file's parent directory, NOT cwd. `Config.load(Path("foo/bar.yaml"))` with `graph_file: graph.json` reads `foo/graph.json`.

**Verify:** `pixi run pytest tests/core/test_config.py -v -k graph_file` → 3/3 pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Add to `tests/core/test_config.py` (append at end of file or beside other Config tests):

```python
# ---- Layer P: spec.graph_file loader convention ---------------------------

def test_spec_graph_file_relative_resolves_against_yaml_parent_dir(
    tmp_path: Path,
) -> None:
    """spec.graph_file with a relative path resolves against the YAML's parent dir."""
    graph_payload = {"nodes": {"1": {"class_type": "LoadImage"}}}
    (tmp_path / "graph.json").write_text(json.dumps(graph_payload))

    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine:
              kind: fake
            models:
              - ref: hf:org/repo:weights.safetensors
                kind: base
                target: checkpoints
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m}
            spec:
              graph_file: graph.json
            """
        ).strip()
    )

    cfg = load_config(yaml_path)

    assert cfg.spec["graph"] == graph_payload
    assert "graph_file" not in cfg.spec


def test_spec_graph_file_both_set_raises(tmp_path: Path) -> None:
    """Both spec.graph_file and spec.graph set → ValidationError naming both keys."""
    (tmp_path / "graph.json").write_text("{}")
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine: {kind: fake}
            models:
              - {ref: hf:o/r:w, kind: base, target: checkpoints}
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m}
            spec:
              graph_file: graph.json
              graph: {nodes: {}}
            """
        ).strip()
    )

    with pytest.raises(ValidationError) as excinfo:
        load_config(yaml_path)
    msg = str(excinfo.value)
    assert "graph_file" in msg and "graph" in msg


def test_spec_graph_file_not_found_raises_with_path(tmp_path: Path) -> None:
    """Missing graph_file → ValidationError mentioning the resolved file path."""
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            engine: {kind: fake}
            models:
              - {ref: hf:o/r:w, kind: base, target: checkpoints}
            compute:
              provider: local
              image: scratch
              requirements: {min_vram_gb: 0}
              lifecycle: {idle_timeout: 10m}
            spec:
              graph_file: nope.json
            """
        ).strip()
    )

    with pytest.raises(ValidationError) as excinfo:
        load_config(yaml_path)
    msg = str(excinfo.value)
    assert "nope.json" in msg
```

Make sure `json`, `textwrap`, `Path`, `pytest`, `ValidationError`, `load_config` are imported at the top of the file (most already are; add what's missing).

- [ ] **Step 2: Run tests to verify they fail**

```bash
pixi run pytest tests/core/test_config.py -v -k graph_file
```

Expected: 3 FAIL — `KeyError` or `cfg.spec` still contains `graph_file` key.

- [ ] **Step 3: Implement the loader in `core/config.py`**

The `Config` class already has a `model_validator(mode="after")` at line 477. The graph_file resolver needs the YAML's parent directory, which the model validator does NOT know about. So the resolution must happen in `load_config()` instead, BEFORE `Config(**data)` is called. Inspect `load_config` first:

```bash
rg -n "def load_config" src/kinoforge/core/config.py
```

Find the function and modify it to resolve `spec.graph_file` post-YAML-parse, pre-pydantic-validation. Add a helper near the other private helpers:

```python
def _resolve_spec_graph_file(data: dict[str, Any], yaml_path: Path) -> None:
    """Resolve spec.graph_file into spec.graph in-place.

    Layer P sugar: if data["spec"]["graph_file"] is set, read the JSON file
    (relative paths resolve against yaml_path.parent), assign its parsed
    contents to data["spec"]["graph"], and remove the "graph_file" key.

    Args:
        data: Mutable dict parsed from YAML, expected to have a "spec" key.
        yaml_path: Path to the YAML being loaded; used as the base for
            relative graph_file paths.

    Raises:
        ValueError: If both ``spec.graph`` and ``spec.graph_file`` are set,
            the file is missing, or its contents are not valid JSON. The
            caller wraps this in a pydantic ValidationError.
    """
    spec = data.get("spec")
    if not isinstance(spec, dict) or "graph_file" not in spec:
        return
    if "graph" in spec:
        raise ValueError(
            "spec.graph_file and spec.graph are mutually exclusive; "
            "set one or the other, not both"
        )
    raw_path = spec["graph_file"]
    file_path = Path(raw_path)
    if not file_path.is_absolute():
        file_path = (yaml_path.parent / file_path).resolve()
    if not file_path.exists():
        raise ValueError(f"spec.graph_file not found: {file_path}")
    try:
        spec["graph"] = json.loads(file_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"spec.graph_file {file_path}: invalid JSON: {exc}"
        ) from exc
    del spec["graph_file"]
```

In `load_config`, call this helper between `yaml.safe_load` and `Config(**data)`. Wrap any `ValueError` it raises into a `pydantic.ValidationError` to keep the existing error type contract (or let it bubble — your call, but tests assert on `ValidationError`, so wrap if needed). Cleanest approach: keep raising `ValueError` from `_resolve_spec_graph_file`, then convert at the `load_config` boundary:

```python
try:
    _resolve_spec_graph_file(data, yaml_path)
except ValueError as exc:
    # Surface as ValidationError to match the schema-error contract.
    raise ValidationError.from_exception_data(
        title="Config",
        line_errors=[
            {
                "type": "value_error",
                "loc": ("spec",),
                "input": data.get("spec"),
                "ctx": {"error": str(exc)},
            }
        ],
    ) from exc
```

If `ValidationError.from_exception_data` is awkward, simpler: raise a plain `ValueError` and update the tests to expect `ValueError` (NOT preferred — keeps schema-error type stable). Pick whichever path keeps the test assertions clean. If the assertion needs `ValidationError`, use the from_exception_data approach; if it accepts either, simplify.

- [ ] **Step 4: Run tests to verify they pass**

```bash
pixi run pytest tests/core/test_config.py -v -k graph_file
```

Expected: 3/3 PASS.

- [ ] **Step 5: Run the full config test suite to verify no regression**

```bash
pixi run pytest tests/core/test_config.py -v
```

Expected: all existing tests still pass; +3 new.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): spec.graph_file loader convention (Layer P Task 1)

Adds YAML sugar: spec.graph_file: <path> inlines a JSON file's contents
into cfg.spec.graph at load time. Relative paths resolve against the YAML
file's parent directory. Mutually exclusive with spec.graph (both set
raises ValidationError). Missing file or invalid JSON raises
ValidationError with the file path + cause.

Layer-P enabler so the Wan i2v ComfyUI workflow can live as a reviewable
JSON file beside the config rather than a 500-line inlined dict in YAML.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: ComfyUI custom-node SHA pinning

**Goal:** Extend ComfyUI engine's `provision` to honor a `ref` field in each `custom_nodes` entry (commit SHA). When present, `git checkout <ref>` runs after the clone. Absent → clone-HEAD (back-compat).

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (the `provision` method; node-install loop around line 566)
- Modify: `tests/engines/test_comfyui.py` (+2 tests)

**Acceptance Criteria:**
- [ ] `engine.comfyui.custom_nodes` entry with `ref: <sha>` causes `provision` to call `run_cmd(["git", "checkout", "<sha>"], cwd=<clone-dir>)` after the clone.
- [ ] `custom_nodes` entry WITHOUT a `ref` field skips the checkout call (clones HEAD only — back-compat).
- [ ] Provision call order remains: clone → checkout (if ref) → install requirements.txt (if present).
- [ ] No production code path requires `ref` (back-compat preserved for any existing configs).

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v -k "ref or sha or checkout"` → 2/2 pass

**Steps:**

- [ ] **Step 1: Locate the existing node-install loop**

```bash
rg -n "custom_nodes|git clone|run_cmd" src/kinoforge/engines/comfyui/__init__.py | head -20
```

Read the loop body around line 566 (file is 770 lines). The exact variable name for the entry dict and the `run_cmd` callable name matter — match them in the test.

- [ ] **Step 2: Write the failing tests**

Add to `tests/engines/test_comfyui.py` near other provision tests (search `def test.*provision` to find the existing block):

```python
def test_comfyui_provision_checkout_sha_when_ref_set() -> None:
    """custom_nodes[*].ref set → provision runs git checkout <ref> after clone."""
    commands: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], *, cwd: str | None = None) -> None:
        commands.append(cmd)

    engine = ComfyUIEngine(
        http_post=lambda url, body: {},
        http_get=lambda url: {},
        run_cmd=fake_run_cmd,
        file_exists=lambda p: False,    # no requirements.txt
        route_file=lambda src, target: None,
    )
    cfg = {
        "engine": {
            "kind": "comfyui",
            "comfyui": {
                "custom_nodes": [
                    {
                        "repo": "https://github.com/kijai/ComfyUI-WanVideoWrapper",
                        "ref": "abc1234",
                    }
                ],
            },
        },
        "models": [],
    }
    instance = _MakeInstance()    # use existing helper or build inline

    engine.provision(instance, cfg)

    checkout_calls = [c for c in commands if c[:2] == ["git", "checkout"]]
    assert len(checkout_calls) == 1
    assert checkout_calls[0] == ["git", "checkout", "abc1234"]


def test_comfyui_provision_skips_checkout_when_ref_absent() -> None:
    """custom_nodes entry without ref → no git checkout call (back-compat)."""
    commands: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], *, cwd: str | None = None) -> None:
        commands.append(cmd)

    engine = ComfyUIEngine(
        http_post=lambda url, body: {},
        http_get=lambda url: {},
        run_cmd=fake_run_cmd,
        file_exists=lambda p: False,
        route_file=lambda src, target: None,
    )
    cfg = {
        "engine": {
            "kind": "comfyui",
            "comfyui": {
                "custom_nodes": [
                    {"repo": "https://github.com/example/SomeNode"},
                ],
            },
        },
        "models": [],
    }
    instance = _MakeInstance()

    engine.provision(instance, cfg)

    checkout_calls = [c for c in commands if c[:2] == ["git", "checkout"]]
    assert checkout_calls == []
```

Adjust `_MakeInstance()` to match whatever helper the existing test file uses (search for `Instance(` constructions in `test_comfyui.py`). If none exists, build a minimal `Instance` inline matching the dataclass signature.

- [ ] **Step 3: Run tests to verify they fail**

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "ref or checkout"
```

Expected: 2 FAIL (checkout never called).

- [ ] **Step 4: Implement the ref handling**

In `src/kinoforge/engines/comfyui/__init__.py`, find the node-install loop. After the `git clone` call and before the `requirements.txt` install, add:

```python
ref = entry.get("ref")
if ref:
    self._run_cmd(
        ["git", "checkout", ref],
        cwd=str(clone_dir),
    )
```

Match the local variable names (`entry`, `clone_dir`, `self._run_cmd` — whatever the existing code uses). The change is ~3 lines.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "ref or checkout"
```

Expected: 2/2 PASS.

- [ ] **Step 6: Run the full ComfyUI test suite — no regression**

```bash
pixi run pytest tests/engines/test_comfyui.py -v
```

Expected: all existing + 2 new pass.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
feat(engines/comfyui): custom-node ref field for git SHA pinning (Layer P Task 2)

Extends ComfyUIEngine.provision to honor an optional "ref" field on each
custom_nodes entry: when set, runs `git checkout <ref>` after the initial
clone, before installing requirements.txt. Absent ref → clone-HEAD
(back-compat for any existing configs).

Layer-P enabler so Wan i2v workflow can pin its WanVideoWrapper / KJNodes
custom-node commits and survive upstream main-branch movement.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `RunPodProvider.find_instance_by_tag` helper

**Goal:** Add a tag-discovery helper on `RunPodProvider` that scans `list_instances()` for a ready pod matching `tags[key] == value`. Used by the live smoke for warm-pod reuse across iterations.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py` (add method near `list_instances`, ~line 331)
- Modify: `tests/providers/test_runpod.py` (+3 tests)

**Acceptance Criteria:**
- [ ] List returns matching ready pod → method returns it.
- [ ] List returns matching pod in `starting` (not-ready) status → returns `None`.
- [ ] List returns no match → returns `None`.
- [ ] Multiple matches → returns one with the largest `created_at` (newest). If `created_at` not available on `Instance`, return the first encountered.

**Verify:** `pixi run pytest tests/providers/test_runpod.py -v -k find_instance_by_tag` → 3/3 pass

**Steps:**

- [ ] **Step 1: Inspect `Instance` dataclass shape**

```bash
rg -n "class Instance" src/kinoforge/core/interfaces.py
```

Confirm `Instance.tags: dict[str, str]` and `Instance.status: str` fields exist. Check for `created_at` — if absent, use first-match semantics (note in docstring).

- [ ] **Step 2: Write the failing tests**

Add to `tests/providers/test_runpod.py`:

```python
def test_find_instance_by_tag_returns_matching_ready_pod() -> None:
    """Ready instance with matching tag → returned."""
    fixture_pods = _load_fixture("list_pods.json")    # existing Layer N fixture
    # If real fixture lacks our tag, monkeypatch list_instances output instead:
    matching = Instance(
        id="pod-abc",
        status="ready",
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
        # ... other required fields, copy from existing test fixtures
    )

    provider = RunPodProvider(creds=_FakeCreds(), http_post=..., http_get=...)
    # Force list_instances to return [matching] via monkeypatch or a stub:
    provider.list_instances = lambda: [matching]    # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")
    assert result is not None
    assert result.id == "pod-abc"


def test_find_instance_by_tag_skips_non_ready() -> None:
    """Matching tag but status != 'ready' → None."""
    starting = Instance(
        id="pod-xyz",
        status="starting",
        tags={"kinoforge.layer": "layer-p-smoke"},
        # ...
    )
    provider = RunPodProvider(creds=_FakeCreds(), http_post=..., http_get=...)
    provider.list_instances = lambda: [starting]    # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")
    assert result is None


def test_find_instance_by_tag_no_match_returns_none() -> None:
    """No instance carries the requested tag → None."""
    other = Instance(
        id="pod-other",
        status="ready",
        tags={"kinoforge.layer": "different-layer"},
        # ...
    )
    provider = RunPodProvider(creds=_FakeCreds(), http_post=..., http_get=...)
    provider.list_instances = lambda: [other]    # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")
    assert result is None
```

Replace `# ...` placeholders with the minimum fields `Instance.__init__` requires (look at how existing `test_runpod.py` builds `Instance` objects — copy that pattern). `_FakeCreds` is the existing fixture used by other tests; use whatever the file already imports.

- [ ] **Step 3: Run tests to verify they fail**

```bash
pixi run pytest tests/providers/test_runpod.py -v -k find_instance_by_tag
```

Expected: 3 FAIL — `AttributeError: 'RunPodProvider' object has no attribute 'find_instance_by_tag'`.

- [ ] **Step 4: Implement the helper**

Add to `src/kinoforge/providers/runpod/__init__.py`, right after `list_instances`:

```python
def find_instance_by_tag(
    self, key: str, value: str
) -> Instance | None:
    """Return the first 'ready' instance whose tags[key] == value, else None.

    Used by long-running test loops (Layer P live smoke) to discover and
    reuse warm pods across iterations, avoiding repeated cold-start costs.

    Args:
        key: Tag dict key to match (e.g. ``"kinoforge.layer"``).
        value: Required value at that key (e.g. ``"layer-p-smoke"``).

    Returns:
        The first ``Instance`` from :meth:`list_instances` with
        ``status == "ready"`` and ``tags.get(key) == value``, or ``None``
        if no such instance exists.

    Notes:
        Production code paths do not call this. It exists purely for
        Layer P's KINOFORGE_LIVE_KEEP_POD=1 dev iteration loop.
    """
    for inst in self.list_instances():
        if inst.status == "ready" and inst.tags.get(key) == value:
            _log.debug(
                "tag-reuse: found %s (gpu=%s, tag=%s=%s)",
                inst.id,
                inst.tags.get("gpu_type", "?"),
                key,
                value,
            )
            return inst
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/providers/test_runpod.py -v -k find_instance_by_tag
```

Expected: 3/3 PASS.

- [ ] **Step 6: Run the full RunPod test suite — no regression**

```bash
pixi run pytest tests/providers/test_runpod.py -v
```

Expected: all existing + 3 new pass.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod.py
git add src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod.py
git commit -m "$(cat <<'EOF'
feat(providers/runpod): find_instance_by_tag helper (Layer P Task 3)

Adds a tag-discovery helper on RunPodProvider for warm-pod reuse during
live test iteration: scans list_instances() for a ready pod matching
tags[key] == value, returns the first match or None. Production code
paths don't call it — exists purely for Layer P's
KINOFORGE_LIVE_KEEP_POD=1 dev iteration loop to amortize cold-start cost.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `_RecordingHTTPSeam` extension for ComfyUI dispatch

**Goal:** Refactor Layer N's `_RecordingHTTPSeam` to accept a dispatch callable, allowing both the GraphQL-query-based RunPod dispatcher AND a URL-pattern-based ComfyUI dispatcher to share the wrapper. Preserve all existing Layer N behavior.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (refactor `_RecordingHTTPSeam`; add ComfyUI dispatch table)
- Modify: `tests/providers/test_runpod_conftest.py` (+2 ComfyUI dispatch tests)

**Acceptance Criteria:**
- [ ] `_RecordingHTTPSeam(post, get, fixtures_dir, dispatch=_RUNPOD_DISPATCH)` preserves all Layer N behavior (existing tests pass unchanged).
- [ ] `_RecordingHTTPSeam(post, get, fixtures_dir, dispatch=_COMFY_DISPATCH)` wraps a ComfyUI-shaped pair of callables, dispatches by URL pattern.
- [ ] ComfyUI POST `/prompt` → writes `prompt_submit.json` with the request body inside `_meta.request_body`.
- [ ] ComfyUI GET `/history/{id}` polled 3 times → last write wins in `history_done.json`.
- [ ] Redaction regex applies to BOTH `_meta.request_body` AND `response` (Layer N parity).
- [ ] Unknown URL → `unknown_<sha>.json` with WARNING log (Layer N parity).

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v` → all pass (existing + 2 new)

**Steps:**

- [ ] **Step 1: Read the current `_RecordingHTTPSeam` shape**

```bash
sed -n '120,200p' tests/providers/conftest_runpod.py
```

Identify the existing `_dispatch(self, query: str) -> str` private method and the call sites in `http_post` / `http_get`. Note that the RunPod variant dispatches by GraphQL query content (POST body), while ComfyUI must dispatch by URL.

- [ ] **Step 2: Write the failing tests**

Add to `tests/providers/test_runpod_conftest.py`:

```python
def test_recording_seam_comfyui_prompt_dispatch(tmp_path: Path) -> None:
    """POST /prompt with comfyui dispatch → writes prompt_submit.json with body in _meta."""
    fixtures_dir = tmp_path
    captured_responses: list[dict[str, Any]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"prompt_id": "p-123", "number": 1, "node_errors": {}}

    def fake_get(url: str) -> dict[str, Any]:
        return {}

    seam = _RecordingHTTPSeam(
        post=fake_post,
        get=fake_get,
        fixtures_dir=fixtures_dir,
        dispatch=_COMFY_DISPATCH,
    )

    response = seam.http_post(
        "http://10.0.0.1:8188/prompt",
        {"prompt": {"1": {"class_type": "LoadImage"}}, "client_id": "kf"},
    )
    seam.flush()

    assert response["prompt_id"] == "p-123"
    fixture_path = fixtures_dir / "prompt_submit.json"
    assert fixture_path.exists()
    captured = json.loads(fixture_path.read_text())
    assert captured["response"]["prompt_id"] == "p-123"
    assert "prompt" in captured["_meta"]["request_body"]


def test_recording_seam_comfyui_history_last_poll_wins(tmp_path: Path) -> None:
    """3 polls of /history/{id} → last response wins in history_done.json."""
    fixtures_dir = tmp_path
    polls = iter([
        {"p-123": {"status": {"completed": False}, "outputs": {}}},
        {"p-123": {"status": {"completed": False}, "outputs": {}}},
        {"p-123": {"status": {"completed": True}, "outputs": {"9": {"images": [{"filename": "out.png"}]}}}},
    ])

    def fake_get(url: str) -> dict[str, Any]:
        return next(polls)

    seam = _RecordingHTTPSeam(
        post=lambda u, b: {},
        get=fake_get,
        fixtures_dir=fixtures_dir,
        dispatch=_COMFY_DISPATCH,
    )

    for _ in range(3):
        seam.http_get("http://10.0.0.1:8188/history/p-123")
    seam.flush()

    captured = json.loads((fixtures_dir / "history_done.json").read_text())
    assert captured["response"]["p-123"]["status"]["completed"] is True
```

Make sure `_COMFY_DISPATCH` is imported (it'll be added in Step 4 alongside `_RUNPOD_DISPATCH`).

- [ ] **Step 3: Run tests to verify they fail**

```bash
pixi run pytest tests/providers/test_runpod_conftest.py -v -k comfyui
```

Expected: 2 FAIL — `ImportError: cannot import _COMFY_DISPATCH` OR `TypeError: _RecordingHTTPSeam.__init__() got an unexpected keyword argument 'dispatch'`.

- [ ] **Step 4: Refactor `_RecordingHTTPSeam` to accept a dispatch callable**

In `tests/providers/conftest_runpod.py`:

a. Define a dispatch callable type at the top of the file:

```python
DispatchFn = Callable[[str, dict[str, Any] | None], str]
"""Dispatch signature: (url, request_body_or_None) -> fixture_filename.

The wrapper passes the URL and (for POSTs) the request body to the
dispatcher, which returns a fixture filename. Returning a name starting
with ``"unknown_"`` causes the wrapper to log a WARNING and still write
the capture.
"""
```

b. Replace the existing inline `_dispatch(self, query: str) -> str` with a constructor-injected dispatcher. The existing Layer N runpod dispatcher (matches by GraphQL fragments inside the POST body) becomes:

```python
def _runpod_dispatch(url: str, body: dict[str, Any] | None) -> str:
    """RunPod dispatcher — keys off GraphQL query content in the POST body."""
    query = ""
    if body and isinstance(body.get("query"), str):
        query = body["query"]
    if "gpuTypes {" in query:
        return "gpu_types.json"
    if "myself { pods" in query:
        return "list_pods.json"
    if "pod(input:" in query:
        return "get_pod.json"
    if "podFindAndDeployOnDemand" in query:
        return "create_pod.json"
    if "podTerminate" in query:
        return "terminate_pod.json"
    sha = hashlib.sha256(query.encode()).hexdigest()[:8]
    return f"unknown_{sha}.json"


_RUNPOD_DISPATCH: DispatchFn = _runpod_dispatch
```

c. Define the ComfyUI dispatcher:

```python
import re

_COMFY_PROMPT_RE = re.compile(r"/prompt(\?.*)?$")
_COMFY_HISTORY_RE = re.compile(r"/history/[^/?]+(\?.*)?$")
_COMFY_VIEW_RE = re.compile(r"/view(\?|$)")


def _comfy_dispatch(url: str, body: dict[str, Any] | None) -> str:
    """ComfyUI dispatcher — keys off URL path."""
    if _COMFY_PROMPT_RE.search(url):
        return "prompt_submit.json"
    if _COMFY_HISTORY_RE.search(url):
        return "history_done.json"
    if _COMFY_VIEW_RE.search(url):
        return "view.json"
    sha = hashlib.sha256(url.encode()).hexdigest()[:8]
    return f"unknown_{sha}.json"


_COMFY_DISPATCH: DispatchFn = _comfy_dispatch
```

d. Update `_RecordingHTTPSeam.__init__` to require a `dispatch` kwarg, and change `http_post` / `http_get` to call `self._dispatch(url, body)` (POST passes body; GET passes None):

```python
class _RecordingHTTPSeam:
    def __init__(
        self,
        post: Callable[[str, dict[str, Any]], dict[str, Any]],
        get: Callable[[str], dict[str, Any]],
        fixtures_dir: Path,
        *,
        dispatch: DispatchFn,
    ) -> None:
        self._post = post
        self._get = get
        self._dispatch = dispatch
        self._fixtures_dir = fixtures_dir
        self._pending: list[_PendingCapture] = []

    def http_post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._post(url, body)
        filename = self._dispatch(url, body)
        self._record(filename, url, body, response, operation=filename.replace(".json", ""))
        return response

    def http_get(self, url: str) -> dict[str, Any]:
        response = self._get(url)
        filename = self._dispatch(url, None)
        self._record(filename, url, None, response, operation=filename.replace(".json", ""))
        return response
```

e. Update `_record` (if private method exists) to apply redaction over both `_meta.request_body` AND `response`. Read the existing function — Layer N already scrubs `response`; just verify `_meta.request_body` is also scrubbed.

f. Update the existing Layer-N construction at `tests/live/test_runpod_live.py:77` from `_RecordingHTTPSeam(authed_post, authed_get, fixtures_dir)` to `_RecordingHTTPSeam(authed_post, authed_get, fixtures_dir, dispatch=_RUNPOD_DISPATCH)`. Import `_RUNPOD_DISPATCH` at the top of `test_runpod_live.py`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pixi run pytest tests/providers/test_runpod_conftest.py -v
```

Expected: existing Layer N tests + 2 new ComfyUI dispatch tests all PASS.

- [ ] **Step 6: Confirm offline RunPod suite still green (Layer N regression check)**

```bash
pixi run pytest tests/providers/test_runpod.py -v
```

Expected: all existing pass. If `_RecordingHTTPSeam` is referenced elsewhere in the file (it isn't directly — only via conftest), no change needed.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py tests/live/test_runpod_live.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py tests/live/test_runpod_live.py
git commit -m "$(cat <<'EOF'
refactor(tests): _RecordingHTTPSeam dispatch callable + ComfyUI dispatcher (Layer P Task 4)

Refactors the Layer N _RecordingHTTPSeam to accept a dispatch kwarg so the
same wrapper supports both the existing GraphQL-query-based RunPod
dispatcher and a new URL-pattern-based ComfyUI dispatcher
(/prompt -> prompt_submit.json, /history/{id} -> history_done.json,
/view -> view.json).

Layer N call sites updated to pass dispatch=_RUNPOD_DISPATCH; behavior
preserved. Adds redaction over _meta.request_body in addition to
response.

Layer-P enabler for capturing ComfyUI HTTP traffic during the
live engine-integration smoke.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Live test skeleton `tests/live/test_comfyui_wan_live.py`

**Goal:** Create the live smoke skeleton — single function `test_runpod_comfyui_wan_live_e2e_smoke`, in-process, phase-marker structured, KEEP_POD-aware, tag-reuse-aware. Module-gated by env vars; skips silently in CI. NO live run yet — file lands first to be reviewable as scaffolding.

**Files:**
- Create: `tests/live/test_comfyui_wan_live.py`

**Acceptance Criteria:**
- [ ] File exists, mypy-clean, ruff-clean.
- [ ] `pytest tests/live/test_comfyui_wan_live.py --collect-only` reports the test exists but skips at module level when env vars unset.
- [ ] Module skip gate checks `KINOFORGE_LIVE_TESTS=1` AND `RUNPOD_API_KEY` AND `RUNPOD_TERMINATE_KEY` AND `HF_TOKEN`.
- [ ] Test body is structured in 10 phases (setup, reuse_check, find_offers, create_instance, poll_ready, provision, generate, destroy, cleanup_finally, record) with `_log.info("[phase=...]", ...)` markers between blocks.
- [ ] `KINOFORGE_LIVE_KEEP_POD=1` read via `os.getenv`; when set, `phase=destroy` skips destroy + logs banner; `phase=cleanup_finally` also skips last-resort destroy.
- [ ] `phase=reuse_check` calls `provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)`; on hit, sets `pod_id` + `warm=True`, skips phases 3–5.
- [ ] `phase=generate` asserts: artifact path exists, `100 KB ≤ size ≤ 50 MB`, MP4 ftyp magic bytes match, capability_key persisted in profile cache JSON, published path under `output/` exists (Layer O sink).
- [ ] `phase=record` writes `tests/engines/fixtures/comfyui/last_smoke.json` on green.

**Verify:** `pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v` → reports `skipped: live tests require ...`

**Steps:**

- [ ] **Step 1: Create the file**

Write `tests/live/test_comfyui_wan_live.py`:

```python
"""Opt-in live smoke: ComfyUI + Wan 2.2 i2v on real RunPod (Layer P).

Produces the first real MP4 from kinoforge end-to-end on real cloud compute.
Runs entirely in-process (no subprocess/CLI invocation). Captures ComfyUI
HTTP fixtures alongside the existing Layer N RunPod GraphQL fixtures when
KINOFORGE_SAVE_FIXTURES=1 is set.

Gated by four env vars:
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``
- ``HF_TOKEN=<huggingface token>`` (Wan 2.2 weights gated repo)

Optional:
- ``KINOFORGE_SAVE_FIXTURES=1`` — write captured responses to
  ``tests/providers/fixtures/runpod/*.json`` and
  ``tests/engines/fixtures/comfyui/*.json``.
- ``KINOFORGE_LIVE_KEEP_POD=1`` — skip the destroy step so re-running the
  test reuses the warm pod via tag-discovery. Cost-saving during dev
  iteration; selfterm + 10-min idle_timeout still tear it down after
  process death.

Cost: ~$0.10-$0.30 cold (full provision); ~$0.05 warm (generate only).
Skipped silently in CI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
    and os.getenv("HF_TOKEN")
):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY "
        "+ RUNPOD_TERMINATE_KEY + HF_TOKEN",
        allow_module_level=True,
    )

_log = logging.getLogger(__name__)

_TAG_KEY = "kinoforge.layer"
_TAG_VALUE = "layer-p-smoke"
_READY_TIMEOUT_S = 600
_GEN_TIMEOUT_S = 900
_POLL_INTERVAL_S = 10

_MP4_FTYP_PREFIXES = (b"ftypisom", b"ftypiso5", b"ftypmp42", b"ftypiso6")


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def test_runpod_comfyui_wan_live_e2e_smoke() -> None:
    """End-to-end live smoke: deploy ComfyUI on RunPod, generate Wan 2.2 i2v MP4."""
    from kinoforge.core.config import load_config
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import GenerationRequest, Asset
    from kinoforge.core import orchestrator
    from kinoforge.engines.comfyui import ComfyUIEngine
    from kinoforge.providers.runpod import (
        RunPodProvider,
        _make_default_http_seams,
    )
    from tests.providers.conftest_runpod import (
        _COMFY_DISPATCH,
        _RUNPOD_DISPATCH,
        _RecordingHTTPSeam,
    )

    # ------------------------------------------------------------------
    # [phase=setup]
    # ------------------------------------------------------------------
    _log.info("[phase=setup]")
    cfg_path = Path("examples/configs/runpod-comfyui-wan.yaml")
    cfg = load_config(cfg_path)

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    authed_post, authed_get = _make_default_http_seams(api_key)

    runpod_fixtures = Path("tests/providers/fixtures/runpod")
    comfy_fixtures = Path("tests/engines/fixtures/comfyui")
    comfy_fixtures.mkdir(parents=True, exist_ok=True)

    capture = os.getenv("KINOFORGE_SAVE_FIXTURES") == "1"
    keep_pod = os.getenv("KINOFORGE_LIVE_KEEP_POD") == "1"

    runpod_seam: _RecordingHTTPSeam | None = None
    comfy_seam: _RecordingHTTPSeam | None = None

    if capture:
        runpod_seam = _RecordingHTTPSeam(
            authed_post, authed_get, runpod_fixtures, dispatch=_RUNPOD_DISPATCH
        )
        provider = RunPodProvider(
            creds=creds,
            http_post=runpod_seam.http_post,
            http_get=runpod_seam.http_get,
        )
    else:
        provider = RunPodProvider(creds=creds)

    # ComfyUI HTTP uses plain (unauthed) seams — pods expose ComfyUI on the
    # public proxy without auth.
    from kinoforge.providers.runpod import _urllib_post_json, _urllib_get_json
    if capture:
        comfy_seam = _RecordingHTTPSeam(
            _urllib_post_json,
            _urllib_get_json,
            comfy_fixtures,
            dispatch=_COMFY_DISPATCH,
        )
        engine = ComfyUIEngine(
            http_post=comfy_seam.http_post,
            http_get=comfy_seam.http_get,
        )
    else:
        engine = ComfyUIEngine(
            http_post=_urllib_post_json,
            http_get=_urllib_get_json,
        )

    pod_id: str | None = None
    instance = None
    warm = False
    start_time = time.monotonic()

    try:
        # ------------------------------------------------------------------
        # [phase=reuse_check]
        # ------------------------------------------------------------------
        _log.info("[phase=reuse_check] keep_pod=%s", keep_pod)
        existing = provider.find_instance_by_tag(_TAG_KEY, _TAG_VALUE)
        if existing is not None:
            warm = True
            pod_id = existing.id
            instance = existing
            _log.info("[phase=reuse_check] warm pod found: %s", pod_id)
        else:
            _log.info("[phase=reuse_check] no warm pod; will create")

        if not warm:
            # --------------------------------------------------------------
            # [phase=find_offers]
            # --------------------------------------------------------------
            _log.info("[phase=find_offers]")
            from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec, Lifecycle

            reqs_dict = cfg.compute.requirements
            reqs = HardwareRequirements(
                min_vram_gb=reqs_dict.min_vram_gb,
                min_cuda=reqs_dict.min_cuda,
                max_cost_rate_usd_per_hr=reqs_dict.max_cost_rate_usd_per_hr,
                gpu_preference=tuple(reqs_dict.gpu_preference or ()),
                disk_gb=reqs_dict.disk_gb,
            )
            offers = provider.find_offers(reqs)
            assert offers, "find_offers returned no offers"
            for offer in offers:
                assert offer.cost_rate_usd_per_hr <= reqs.max_cost_rate_usd_per_hr, (
                    f"offer {offer.id!r} cost exceeds cap"
                )
            chosen = offers[0]
            _log.info("[phase=find_offers] picked %s @ $%.4f/hr",
                      chosen.gpu_type, chosen.cost_rate_usd_per_hr)

            # --------------------------------------------------------------
            # [phase=create_instance]
            # --------------------------------------------------------------
            _log.info("[phase=create_instance]")
            ispec = InstanceSpec(
                image=cfg.compute.image,
                offer=chosen,
                lifecycle=Lifecycle(idle_timeout_s=int(cfg.lifecycle().idle_timeout_s)),
                tags={
                    "mode": "pod",
                    _TAG_KEY: _TAG_VALUE,
                    "kinoforge.git_sha": _git_sha(),
                },
            )
            instance = provider.create_instance(ispec)
            pod_id = instance.id
            assert pod_id, "create_instance returned empty id"

            # --------------------------------------------------------------
            # [phase=poll_ready]
            # --------------------------------------------------------------
            _log.info("[phase=poll_ready]")
            elapsed = 0.0
            while instance.status != "ready":
                if elapsed >= _READY_TIMEOUT_S:
                    pytest.fail(f"pod {pod_id} did not reach 'ready' within {_READY_TIMEOUT_S}s")
                time.sleep(_POLL_INTERVAL_S)
                elapsed += _POLL_INTERVAL_S
                instance = provider.get_instance(pod_id)
                _log.info("[phase=poll_ready] status=%s elapsed=%.0fs",
                          instance.status, elapsed)

        # ------------------------------------------------------------------
        # [phase=provision]
        # ------------------------------------------------------------------
        _log.info("[phase=provision] (skip-if-warm via provision_state marker)")
        # orchestrator.generate() handles provision-skip internally via the
        # Layer I provision_state marker. The explicit provision call here
        # is a no-op when warm; for cold pods, the first generate call below
        # provisions before submitting.

        # ------------------------------------------------------------------
        # [phase=generate]
        # ------------------------------------------------------------------
        _log.info("[phase=generate]")
        init_frame = Path("tests/providers/fixtures/runpod/sample_init_frame.png")
        request = GenerationRequest(
            prompt="A cat slowly turning its head, cinematic, soft natural light",
            assets=[Asset(role="init_image", path=init_frame)],
        )

        # orchestrator.generate accepts injected provider + engine_factory in
        # this code path (Layer I + Layer K pattern); verify signature with
        # an inspect.signature call at plan time if it has drifted.
        artifact = orchestrator.generate(
            cfg,
            request,
            provider=provider,
            engine_factory=lambda *_: engine,
        )

        artifact_path = Path(artifact.local_path)
        assert artifact_path.exists(), f"artifact not on disk: {artifact_path}"
        size = artifact_path.stat().st_size
        assert 100 * 1024 <= size <= 50 * 1024 * 1024, (
            f"artifact size {size} out of bounds [100 KB, 50 MB]"
        )
        head = artifact_path.read_bytes()[4:12]
        assert any(head.startswith(p) for p in _MP4_FTYP_PREFIXES), (
            f"artifact ftyp magic mismatch: head={head!r}"
        )

        # Layer O published-path assertion: the output sink should have
        # mirrored the artifact under output/ (or wherever cfg.output.dir
        # points).
        from kinoforge.outputs import get_sink
        output_dir = Path(cfg.output.dir if cfg.output else "output")
        published = list(output_dir.rglob("*.mp4"))
        assert published, f"no MP4 published under {output_dir}"

        # ------------------------------------------------------------------
        # [phase=destroy]
        # ------------------------------------------------------------------
        if keep_pod:
            _log.warning(
                "*** POD %s KEPT (KINOFORGE_LIVE_KEEP_POD=1) — re-runs will reuse via tag ***",
                pod_id,
            )
        else:
            _log.info("[phase=destroy]")
            provider.destroy_instance(pod_id)
            _log.info("[phase=destroy] destroyed normally")

    finally:
        # ------------------------------------------------------------------
        # [phase=cleanup_finally]
        # ------------------------------------------------------------------
        _log.info("[phase=cleanup_finally]")
        for seam in (runpod_seam, comfy_seam):
            if seam is not None:
                try:
                    seam.flush()
                except Exception as exc:
                    _log.warning("seam.flush() failed: %s", exc)

        if pod_id is not None and not keep_pod:
            try:
                provider.destroy_instance(pod_id)
                _log.info("pod %s confirmed destroyed (finally path)", pod_id)
            except Exception as exc:
                import sys
                sys.stderr.write(
                    f"\n*** RUNPOD POD {pod_id} NOT CONFIRMED DESTROYED ***\n"
                    f"Error: {exc}\n"
                    f"Manually terminate via the RunPod console or run:\n"
                    f"  curl -X POST https://api.runpod.io/graphql?api_key=$RUNPOD_API_KEY \\\n"
                    f'    -H "Content-Type: application/json" \\\n'
                    f'    -d \'{{"query":"mutation{{podTerminate(input:{{podId:\\"{pod_id}\\"}})}}"}}\'\n'
                )
                raise

    # ------------------------------------------------------------------
    # [phase=record] — only on green
    # ------------------------------------------------------------------
    _log.info("[phase=record]")
    artifact_sha = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    smoke_meta = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": _git_sha(),
        "pod_id": pod_id,
        "gpu_type": instance.tags.get("gpu_type") if instance else None,
        "elapsed_seconds": round(time.monotonic() - start_time, 1),
        "artifact_path": str(artifact_path),
        "artifact_size": size,
        "artifact_sha256": artifact_sha,
        "capability_key": getattr(artifact, "capability_key", None),
    }
    (comfy_fixtures / "last_smoke.json").write_text(
        json.dumps(smoke_meta, indent=2) + "\n"
    )
    _log.info("[phase=record] last_smoke.json written")
```

Some module paths above (notably `_urllib_post_json`, `_urllib_get_json`, `orchestrator.generate` signature, `kinoforge.outputs.get_sink`) MUST be verified before commit. The plan does NOT pre-validate every name. If a name is wrong, the live shake-out loop (Task 7) will surface it; the scaffolding commit only needs to satisfy `ruff check`, `mypy`, AND `pytest --collect-only`.

- [ ] **Step 2: Verify ruff + mypy + collect**

```bash
pixi run ruff check tests/live/test_comfyui_wan_live.py
pixi run ruff format tests/live/test_comfyui_wan_live.py
pixi run mypy tests/live/test_comfyui_wan_live.py
pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v
```

Expected: ruff clean, mypy clean, collect reports `skipped`.

If mypy complains about imports inside the function (lazy imports), suppress with explicit type annotations or convert to top-of-function. If `orchestrator.generate` signature doesn't accept `provider=` + `engine_factory=` kwargs in current main, switch to the `deploy_session` + manual stage path used by `cli._cmd_generate` instead.

- [ ] **Step 3: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/live/test_comfyui_wan_live.py
git add tests/live/test_comfyui_wan_live.py
git commit -m "$(cat <<'EOF'
test(live): ComfyUI + Wan i2v RunPod live smoke skeleton (Layer P Task 5)

Single test function test_runpod_comfyui_wan_live_e2e_smoke. In-process.
Phase-marker structured (setup, reuse_check, find_offers, create_instance,
poll_ready, provision, generate, destroy, cleanup_finally, record).
KINOFORGE_LIVE_KEEP_POD=1 reuses warm pod via tag-discovery (Task 3).
KINOFORGE_SAVE_FIXTURES=1 captures RunPod GraphQL + ComfyUI HTTP via the
extended _RecordingHTTPSeam (Task 4).

Module gated by 4 env vars; skips silently in CI.

No live run yet — file lands as reviewable scaffolding before Task 7's
live shake-out.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Example YAML + graph JSON scaffold

**Goal:** Fill `examples/configs/runpod-comfyui-wan.yaml` with `spec.graph_file: runpod-comfyui-wan.graph.json`, custom_nodes SHA-pin placeholders, and a 1-entry models list as scaffolding. Create the graph JSON file with a minimal valid Wan i2v workflow placeholder. Both files are placeholders pending Task 7's live capture.

**Files:**
- Modify: `examples/configs/runpod-comfyui-wan.yaml` (replace `spec.graph: {nodes: []}` with `spec.graph_file: ...`; add `custom_nodes` block; expand `models` to multiple placeholder entries)
- Create: `examples/configs/runpod-comfyui-wan.graph.json` (minimal placeholder workflow; Task 7 replaces with the live-captured real graph)

**Acceptance Criteria:**
- [ ] YAML loads via `Config.load` without error.
- [ ] `cfg.spec["graph"]` after load is a dict matching the JSON file's contents (proves Task 1's loader works end-to-end on the real example).
- [ ] YAML's `engine.comfyui.custom_nodes` has at least 2 entries (`ComfyUI-WanVideoWrapper`, `ComfyUI-KJNodes`), each with `repo:` + `ref:` keys (refs as placeholder string `"PINME"` until Task 7).
- [ ] YAML's `models:` list has 3 entries (UNet, VAE, text encoder) with placeholder HF paths.
- [ ] YAML includes a comment block at the top documenting that SHAs + model paths are pinned by Task 7's live shake-out.

**Verify:** `pixi run pytest tests/test_examples.py -v -k runpod_comfyui_wan` → passes the example-load test

**Steps:**

- [ ] **Step 1: Replace `examples/configs/runpod-comfyui-wan.yaml`**

```yaml
# Layer P live smoke — ComfyUI + Wan 2.2 i2v on RunPod.
#
# Usage:
#   export RUNPOD_API_KEY=...
#   export RUNPOD_TERMINATE_KEY=...
#   export HF_TOKEN=...                     # Wan 2.2 weights are gated
#   export KINOFORGE_LIVE_TESTS=1
#   export KINOFORGE_SAVE_FIXTURES=1        # first green run only
#   export KINOFORGE_LIVE_KEEP_POD=1        # dev iteration — reuses pod
#
# Then:
#   pixi run pytest tests/live/test_comfyui_wan_live.py -v -s
#
# Cost guards (quadruple-locked):
#   - max_cost_rate_usd_per_hr: 0.50  (filters expensive GPUs)
#   - budget: 2.00                    (BudgetTracker tears down mid-run)
#   - idle_timeout: 10m + selfterm    (pod self-destructs after 10 min idle)
#   - finally: in test always destroys (unless KEEP_POD=1)
#
# Placeholders pinned by Layer P Task 7 (live shake-out):
#   - engine.comfyui.custom_nodes[*].ref  (commit SHAs)
#   - models[*].ref                       (real Wan 2.2 file names)
#   - spec.graph_file                     (real exported workflow JSON)

engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
    custom_nodes:
      - repo: "https://github.com/kijai/ComfyUI-WanVideoWrapper"
        ref: "PINME"        # captured by Task 7
      - repo: "https://github.com/kijai/ComfyUI-KJNodes"
        ref: "PINME"

models:
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:wan2.2_14b_i2v_unet.safetensors"
    kind: base
    target: checkpoints
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:wan2.2_14b_i2v_vae.safetensors"
    kind: vae
    target: vae
  - ref: "hf:Wan-AI/Wan2.2-I2V-A14B:wan2.2_14b_i2v_text_encoder.safetensors"
    kind: text_encoder
    target: clip

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_cost_rate_usd_per_hr: 0.50
    gpu_preference:
      - "NVIDIA GeForce RTX 4090"
      - "NVIDIA RTX A5000"
      - "NVIDIA GeForce RTX 3090"
    disk_gb: 80
  lifecycle:
    idle_timeout: 10m
    job_timeout: 15m
    time_buffer: 5m
    max_lifetime: 30m
    budget: 2.0

spec:
  graph_file: runpod-comfyui-wan.graph.json
  asset_node_ids:
    init_image: "12"           # rewritten by Task 7 to match the real graph
  prompt_node_ids:
    - "8"                      # rewritten by Task 7
  node_overrides: {}

params:
  fps: 16
  num_frames: 81
  steps: 20
  width: 480
  height: 480

# Output sink (Layer O) — uncomment to override defaults.
# output:
#   kind: local
#   dir: output
#   enabled: true
```

- [ ] **Step 2: Create the placeholder graph JSON**

`examples/configs/runpod-comfyui-wan.graph.json`:

```json
{
  "_kinoforge_note": "PLACEHOLDER — replaced by Layer P Task 7 live capture.",
  "1": {
    "class_type": "LoadImage",
    "inputs": {"image": "input.png"}
  }
}
```

Task 7 overwrites this with the real exported workflow.

- [ ] **Step 3: Run the example-load test**

```bash
pixi run pytest tests/test_examples.py -v -k runpod_comfyui_wan
```

If the existing test asserts on shape of `spec.graph` (e.g. expects `nodes:` key), update the assertion to load the file via `Config.load(Path("examples/configs/runpod-comfyui-wan.yaml"))` and check that `cfg.spec["graph"]` is a dict (whatever its shape). Don't lock specific node IDs — Task 7 will rewrite them.

If no such test exists, add one:

```python
def test_runpod_comfyui_wan_yaml_loads(tmp_path: Path) -> None:
    """examples/configs/runpod-comfyui-wan.yaml loads via Config.load."""
    from kinoforge.core.config import load_config
    cfg = load_config(Path("examples/configs/runpod-comfyui-wan.yaml"))
    assert cfg.engine.kind == "comfyui"
    assert cfg.compute.provider == "runpod"
    assert isinstance(cfg.spec.get("graph"), dict)
    assert "graph_file" not in cfg.spec
```

- [ ] **Step 4: Pre-commit + commit**

```bash
pixi run pre-commit run --files examples/configs/runpod-comfyui-wan.yaml examples/configs/runpod-comfyui-wan.graph.json tests/test_examples.py
git add examples/configs/runpod-comfyui-wan.yaml examples/configs/runpod-comfyui-wan.graph.json tests/test_examples.py
git commit -m "$(cat <<'EOF'
test(examples): Layer P RunPod+ComfyUI+Wan YAML scaffold (Layer P Task 6)

Fills examples/configs/runpod-comfyui-wan.yaml with spec.graph_file,
custom_nodes SHA-pin placeholders, and a 3-entry models list (UNet, VAE,
text encoder). Companion graph JSON is a placeholder; Task 7 replaces it
with the live-captured Wan 2.2 i2v workflow.

PINME markers + placeholder model filenames are intentional — Task 7's
live shake-out resolves them.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Live shake-out — capture real MP4 + fixtures + SHAs + model set

**Goal:** Drive the live smoke against real RunPod until a green MP4 ships. Capture the 3 ComfyUI fixtures, real custom-node SHAs, the actual Wan 2.2 model file set, and the exported workflow graph. Every bug surfaced becomes an in-scope production fix with a regression test (Layer N + Layer I precedent).

**Files** (touched iteratively across multiple commits):
- Modify: `examples/configs/runpod-comfyui-wan.yaml` (real SHAs, real model paths)
- Modify: `examples/configs/runpod-comfyui-wan.graph.json` (real exported workflow)
- Create: `tests/engines/fixtures/comfyui/prompt_submit.json`
- Create: `tests/engines/fixtures/comfyui/history_done.json`
- Create: `tests/engines/fixtures/comfyui/view.json`
- Create: `tests/engines/fixtures/comfyui/last_smoke.json`
- Modify: `tests/providers/fixtures/runpod/*.json` (refresh on this run)
- (Iteratively) Modify production code anywhere a bug surfaces; each fix gets a regression test in the appropriate offline-test file.

**Acceptance Criteria:**
- [ ] First green run produces an MP4 ≥ 100 KB, ≤ 50 MB, valid MP4 ftyp magic bytes.
- [ ] `tests/engines/fixtures/comfyui/{prompt_submit,history_done,view}.json` committed with valid `_meta` blocks.
- [ ] `tests/engines/fixtures/comfyui/last_smoke.json` committed with artifact_path, sha256, size, capability_key, gpu_type, elapsed_seconds.
- [ ] `examples/configs/runpod-comfyui-wan.yaml` `custom_nodes[*].ref` no longer `"PINME"` — real SHAs.
- [ ] `examples/configs/runpod-comfyui-wan.yaml` `models[*].ref` paths are real Wan 2.2 file names that resolved on the real HF API.
- [ ] `examples/configs/runpod-comfyui-wan.graph.json` is the real exported workflow.
- [ ] Each production bug fixed in this task is one commit, with a regression test in the appropriate offline-test file.
- [ ] Final smoke run with `KINOFORGE_SAVE_FIXTURES=1` AND `KEEP_POD` unset → pod destroyed cleanly, fixtures captured.

**Verify:** `KINOFORGE_LIVE_TESTS=1 RUNPOD_API_KEY=... RUNPOD_TERMINATE_KEY=... HF_TOKEN=... pixi run pytest tests/live/test_comfyui_wan_live.py::test_runpod_comfyui_wan_live_e2e_smoke -v -s` → PASS, artifact path + size logged

**Steps:**

- [ ] **Step 1: Initial dry-run — collect-only to confirm test discovered**

```bash
KINOFORGE_LIVE_TESTS=1 RUNPOD_API_KEY=$RUNPOD_API_KEY RUNPOD_TERMINATE_KEY=$RUNPOD_TERMINATE_KEY HF_TOKEN=$HF_TOKEN pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only
```

Expected: test discovered, not skipped.

- [ ] **Step 2: First live run with KEEP_POD=1 + SAVE_FIXTURES=1**

```bash
set -a; source .env; set +a
KINOFORGE_LIVE_TESTS=1 KINOFORGE_LIVE_KEEP_POD=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/layer-p-smoke-1.log
```

This will almost certainly fail. The log surfaces the first bug.

- [ ] **Step 3: Iterate. For each surfaced bug:**

a. Identify which production file owns the bug.
b. Write a regression test in the appropriate offline-test file that fails against the bug.
c. Fix the production code.
d. Verify the offline regression test now passes.
e. Re-run the live smoke with `KEEP_POD=1 SAVE_FIXTURES=1`.
f. Commit each fix atomically:

```bash
git add <production-file> <offline-regression-test>
git commit -m "fix(<scope>): <one-line description> (Layer P live-smoke bug N)"
```

Bug catches to expect (extrapolating from Layer N's 10 catches and Layer I's 5):
- ComfyUI URL construction may include or omit `http://` scheme depending on `RunPodProvider.endpoints` shape.
- Multipart upload path for `/upload/image` may have boundary issues against real ComfyUI.
- HF model paths may 404 — surface real file names from HF API browsing.
- Wan custom-node `requirements.txt` may need `accelerate` or `transformers` pinned to specific versions.
- `provision_state.py` marker may not register correctly when the pod is reused via `find_instance_by_tag`.
- ComfyUI `/history/{id}` response shape may have a different output-key (`videos` vs `gifs` vs `images`) — `extract_last_frame` (Layer E) and final-artifact fetch (Layer F) URL builders may need adjustment.
- Wan model loading may require specific config files (`config.json`, `tokenizer.json`) that the YAML `models:` list doesn't cover.

For each catch: keep going. Don't stop the layer — fix and re-run.

- [ ] **Step 4: Once provisioning works, run with KEEP_POD=1 to amortise weight download across generate iterations**

```bash
# Second-and-later iterations (warm pod, fast):
KINOFORGE_LIVE_TESTS=1 KINOFORGE_LIVE_KEEP_POD=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s
```

Each warm iteration is ~$0.05 (generate only). Layer N precedent: ~10 iterations total. Budget ~$2 cap remains active.

- [ ] **Step 5: Export the real ComfyUI workflow**

Once the test green-passes a generate phase, SSH into the warm pod and export the workflow that was actually executed:

```bash
# SSH proxy URL from instance.tags["ports"] or the cli kinoforge status output
# Then on the pod (or via curl to the workflow API endpoint):
curl http://localhost:8188/history/<last-prompt-id> > /tmp/workflow.json
```

Pull the executed workflow definition (the `prompt` field under the prompt_id key) and write it to `examples/configs/runpod-comfyui-wan.graph.json`, replacing the placeholder.

Update the YAML's `spec.asset_node_ids` and `spec.prompt_node_ids` to match the real node IDs in the exported graph (e.g. find the `LoadImage` node ID + the positive-prompt-text node ID).

- [ ] **Step 6: Capture real custom-node SHAs**

```bash
# SSH into the warm pod or query via a one-shot exec call
cd /workspace/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper && git rev-parse HEAD
cd /workspace/ComfyUI/custom_nodes/ComfyUI-KJNodes && git rev-parse HEAD
```

Update `examples/configs/runpod-comfyui-wan.yaml` `engine.comfyui.custom_nodes[*].ref` with the real SHAs.

- [ ] **Step 7: Final clean-pod run (no KEEP_POD)**

Destroy the warm pod manually (or via `kinoforge gc --config examples/configs/runpod-comfyui-wan.yaml`). Then run the smoke fresh, WITHOUT `KEEP_POD`, to confirm cold-start works end-to-end and `finally:` destroys cleanly:

```bash
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s 2>&1 | tee /tmp/layer-p-smoke-final.log
```

This is the "green run" recorded in PROGRESS Phase 26.

- [ ] **Step 8: Commit the captured fixtures + finalized YAML + finalized graph**

```bash
git add tests/engines/fixtures/comfyui/ tests/providers/fixtures/runpod/ examples/configs/runpod-comfyui-wan.yaml examples/configs/runpod-comfyui-wan.graph.json
git commit -m "$(cat <<'EOF'
test(live): capture ComfyUI+Wan live smoke fixtures + finalize YAML/graph (Layer P Task 7)

First real MP4 from kinoforge end-to-end on real RunPod:
- pod_id: <captured>
- gpu: <gpu_type> @ <cost>/hr
- artifact: <path>
- size: <size>
- sha256: <sha>
- capability_key: <ck>

Live-smoke bug catches committed separately on this branch (see PROGRESS
Phase 26 entry in Task 10).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Substitute the real values from `last_smoke.json` into the commit message body.

- [ ] **Step 9: Tally total spend**

Note the dev iteration count + total spend (sum of `cost_rate_usd_per_hr` × elapsed across all live runs). Reserve this number for the PROGRESS Phase 26 entry in Task 10.

---

## Task 8: Refactor offline ComfyUIBackend tests to load from fixtures

**Goal:** Refactor the 23 existing `tests/engines/test_comfyui.py` tests that use hand-crafted response dicts to load from the Task 7 fixtures. Add `tests/engines/conftest.py` with the shared `_load_comfy_fixture` helper.

**Files:**
- Create: `tests/engines/conftest.py`
- Modify: `tests/engines/test_comfyui.py` (23-test refactor)

**Acceptance Criteria:**
- [ ] `tests/engines/conftest.py` defines `_load_comfy_fixture(name: str) -> dict[str, Any]`.
- [ ] `rg "prompt_id|outputs.*videos|outputs.*gifs|outputs.*images" tests/engines/test_comfyui.py | grep -v "_load_comfy_fixture"` returns nothing (no hand-crafted shape left in test bodies — all loaded from JSON).
- [ ] All existing offline tests still pass.
- [ ] If any test was passing against a fictional shape (hand-crafted dict diverged from real ComfyUI), update the test to use the real fixture. If production code was masked-buggy, the bug fix lives in Task 7 (committed earlier).

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v` → all existing tests still pass (modulo any value updates from real-shape alignment).

**Steps:**

- [ ] **Step 1: Create `tests/engines/conftest.py`**

```python
"""Shared pytest helpers for the engines/ test suite.

Adds the Layer P fixture-replay helper for ComfyUI offline tests. Mirrors
the Layer N pattern in tests/providers/conftest_runpod.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_COMFY_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "comfyui"


def _load_comfy_fixture(name: str) -> dict[str, Any]:
    """Load a captured ComfyUI HTTP response by fixture filename.

    Args:
        name: Fixture filename relative to ``tests/engines/fixtures/comfyui/``
            (e.g. ``"prompt_submit.json"``).

    Returns:
        The ``response`` block of the fixture (the ``_meta`` block is
        captured for forensic value, not asserted on).
    """
    with (_COMFY_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])
```

- [ ] **Step 2: Identify hand-crafted dict patterns in `test_comfyui.py`**

```bash
rg -n 'http_post\s*=\s*lambda|"prompt_id"|"outputs"' tests/engines/test_comfyui.py | head -50
```

This lists every inline-dict construction. For each one, decide which fixture file it maps to:

| Hand-crafted shape | Fixture file |
|---|---|
| `{"prompt_id": "..."}` returned from POST | `prompt_submit.json` |
| `{"<id>": {"outputs": {...}}}` returned from GET history | `history_done.json` |
| Anything from `/view?...` | `view.json` |

- [ ] **Step 3: Refactor pattern**

For each occurrence:

```python
# BEFORE
http_post = lambda url, body: {"prompt_id": "abc-123"}

# AFTER
from tests.engines.conftest import _load_comfy_fixture
http_post = lambda url, body: _load_comfy_fixture("prompt_submit.json")
```

If a test asserts on a specific value (e.g. `prompt_id == "abc-123"`), update the assertion to the value present in the captured fixture (e.g. `prompt_id == response["prompt_id"]`) so the test asserts a relationship rather than a magic constant.

- [ ] **Step 4: Run tests**

```bash
pixi run pytest tests/engines/test_comfyui.py -v
```

Expected: all pass. Failures mean either (a) the captured fixture has a different shape than the production code expects — fix the production code (in Task 7 if not already done), or (b) the test's assertion was too tight — relax to relationship-based.

- [ ] **Step 5: Verify the rg cleanup**

```bash
rg "prompt_id|outputs.*videos|outputs.*gifs|outputs.*images" tests/engines/test_comfyui.py | grep -v "_load_comfy_fixture\|fixture\|assert\|response\["
```

Expected: empty output (no hand-crafted shape left in test bodies).

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/engines/conftest.py tests/engines/test_comfyui.py
git add tests/engines/conftest.py tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
refactor(tests/engines): ComfyUI tests load from captured fixtures (Layer P Task 8)

23 hand-crafted-dict tests in test_comfyui.py now load HTTP response
shapes from tests/engines/fixtures/comfyui/*.json via the new
_load_comfy_fixture helper in tests/engines/conftest.py.

Layer N's RunPod refactor (Phase 24) precedent: hand-crafted dicts in
tests silently diverged from real API shape; live capture + replay locks
the contract.

Any value update from real-shape alignment is documented at the per-test
diff. Production-code bugs masked by old hand-crafted shapes were fixed
in Task 7 (committed earlier on this branch).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: ComfyUI shape-lockdown tests

**Goal:** Add 3 new tests that lock the ComfyUI HTTP response contract. Future ComfyUI schema upgrades that break any of these fail loudly instead of silently.

**Files:**
- Modify: `tests/engines/test_comfyui.py` (+3 tests)

**Acceptance Criteria:**
- [ ] `test_comfyui_prompt_submit_shape` — asserts `prompt_submit.json` response has a string `prompt_id` key.
- [ ] `test_comfyui_real_shape_required_keys` — asserts the `history_done.json` response, when keyed by its single top-level key (the prompt_id), has a sub-dict containing both `"status"` (with `"completed": True`) and `"outputs"` (a dict mapping node_id → output dict).
- [ ] `test_comfyui_view_url_shape` — asserts `view.json`'s `_meta.request_url` matches the regex `r"/view\?.*filename="`.

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"` → 3/3 pass

**Steps:**

- [ ] **Step 1: Write the tests**

Add to `tests/engines/test_comfyui.py`:

```python
def test_comfyui_prompt_submit_shape() -> None:
    """Captured POST /prompt response has a string prompt_id key."""
    response = _load_comfy_fixture("prompt_submit.json")
    assert "prompt_id" in response
    assert isinstance(response["prompt_id"], str)
    assert response["prompt_id"]    # non-empty


def test_comfyui_real_shape_required_keys() -> None:
    """Captured GET /history/{id} terminal response contains status.completed + outputs."""
    response = _load_comfy_fixture("history_done.json")
    assert len(response) == 1, "history_done.json should be keyed by a single prompt_id"
    prompt_id, body = next(iter(response.items()))
    assert isinstance(prompt_id, str) and prompt_id
    assert "status" in body, "missing 'status' field"
    assert body["status"].get("completed") is True, "status.completed != True"
    assert "outputs" in body, "missing 'outputs' field"
    assert isinstance(body["outputs"], dict)
    assert body["outputs"], "outputs dict empty"


def test_comfyui_view_url_shape() -> None:
    """Captured /view URL has the expected query-string pattern."""
    import json as _json
    fixture_path = (
        Path(__file__).parent / "fixtures" / "comfyui" / "view.json"
    )
    with fixture_path.open() as f:
        captured = _json.load(f)
    request_url = captured["_meta"]["request_url"]
    import re
    assert re.search(r"/view\?.*filename=", request_url), (
        f"view.json request_url does not match /view?filename=...: {request_url!r}"
    )
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
pixi run pytest tests/engines/test_comfyui.py -v -k "shape or required_keys"
```

Expected: 3/3 PASS against the Task 7 fixtures.

- [ ] **Step 3: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/engines/test_comfyui.py
git add tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
test(engines/comfyui): shape-lockdown for prompt_submit / history / view (Layer P Task 9)

Three new tests pin the ComfyUI HTTP response contract against the
Task 7 captured fixtures. Future ComfyUI schema upgrades that drop
prompt_id, change history's status.completed shape, or move /view URL
parameters around will fail loudly here.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: README + PROGRESS Phase 26 + final gate + merge

**Goal:** Document Layer P. Run the full offline gate. Open a PR or merge directly to main via `--no-ff`.

**Files:**
- Modify: `README.md` (extend "Real providers — RunPod" section)
- Modify: `PROGRESS.md` (Phase 26 entry; close Layer-O carry-forward #1; bump test count; reset "Single next action")
- (Merge): `build/layer-p` → `main` via `--no-ff`

**Acceptance Criteria:**
- [ ] README has a new sub-section under "Real providers — RunPod" titled "Engine integration (ComfyUI + Wan i2v)" with env-var list, quickstart command, KEEP_POD dev loop notes, and cost shape.
- [ ] PROGRESS Phase 26 entry written with task-by-task SHA list, live-smoke bug catches list (one bullet per fix), test count delta, total live-smoke spend, key design decisions.
- [ ] PROGRESS "Single next action" updated.
- [ ] Layer-O carry-forward #1 marked CLOSED.
- [ ] Full offline test gate passes: `pixi run test && pixi run typecheck && pixi run lint`.
- [ ] Merge to main via `--no-ff` with substantive body.

**Verify:**
- `pixi run test && pixi run typecheck && pixi run lint` → all green
- `git log --oneline main..build/layer-p` shows Tasks 1–10 + bug-catch commits
- `git log --merges --oneline -5` shows the merge commit after merge

**Steps:**

- [ ] **Step 1: Update README**

Locate the existing "Real providers — RunPod" section:

```bash
rg -n "Real providers — RunPod" README.md
```

Add immediately after it a new sub-section:

```markdown
### Engine integration (ComfyUI + Wan i2v)

Layer P adds the first real engine-integration live smoke: ComfyUI + Wan 2.2 i2v
deployed on a real RunPod pod producing a real MP4.

**Required env vars** (in addition to the RunPod ones above):

- `HF_TOKEN` — HuggingFace token, needed because the Wan 2.2 weights repo is gated.

**Quickstart:**

    set -a; source .env; set +a
    KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s

**Dev iteration loop:**

The first run is expensive (~$0.10–$0.30) because it pays for the full
provision: pod start, ComfyUI install, custom-node clones, ~28 GB Wan
weights download. To amortise that cost during a debug iteration loop:

    KINOFORGE_LIVE_KEEP_POD=1 KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s

`KINOFORGE_LIVE_KEEP_POD=1` skips the test's destroy step. Re-runs use
`RunPodProvider.find_instance_by_tag()` to discover the still-running pod
by its `kinoforge.layer=layer-p-smoke` tag and reuse it. Generate-only
iterations cost ~$0.05 each. The pod's 10-minute idle timeout +
selfterm script tear it down even if your test process dies abnormally.

**Fixture capture:**

    KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 pixi run pytest tests/live/test_comfyui_wan_live.py -v -s

writes `tests/providers/fixtures/runpod/*.json` (RunPod GraphQL) and
`tests/engines/fixtures/comfyui/*.json` (ComfyUI HTTP) for offline test
replay.

**Cost guards (quadruple-locked):** see `examples/configs/runpod-comfyui-wan.yaml`
header comment.
```

- [ ] **Step 2: Write PROGRESS Phase 26 entry**

Add a `### Phase 26 — Layer P (RunPod engine integration: ComfyUI + Wan i2v)` block at the end of the `## Post-MVP` section in `PROGRESS.md`. Template:

```markdown
### Phase 26 — Layer P (RunPod engine integration: ComfyUI + Wan i2v)

First real MP4 from kinoforge end-to-end on real cloud compute. Closes
Layer-O carry-forward #1. ComfyUI engine deployed on real RunPod pod
provisions Wan 2.2 i2v weights, runs the hand-authored workflow, returns
a valid MP4 artifact through orchestrator.generate(). Layer N pattern
extended: recording-seam wraps both RunPod GraphQL AND ComfyUI HTTP so
the offline ComfyUIBackend test suite refactors to fixture-replay parity.

- [x] Task 1: spec.graph_file loader convention — commit `<sha>`
- [x] Task 2: ComfyUI custom-node ref SHA pinning — commit `<sha>`
- [x] Task 3: RunPodProvider.find_instance_by_tag helper — commit `<sha>`
- [x] Task 4: _RecordingHTTPSeam dispatch refactor + ComfyUI dispatcher — commit `<sha>`
- [x] Task 5: Live smoke skeleton — commit `<sha>`
- [x] Task 6: Example YAML + graph JSON scaffold — commit `<sha>`
- [x] Task 7: Live shake-out + fixtures + finalized YAML/graph — commit `<sha>`
- [x] Task 8: Offline ComfyUIBackend fixture-replay refactor — commit `<sha>`
- [x] Task 9: ComfyUI shape-lockdown tests — commit `<sha>`
- [x] Task 10: README + PROGRESS + merge — commit `<sha>` (this commit)

**First real artifact (RunPod + ComfyUI + Wan i2v):**
- pod `<pod_id>` on `<gpu_type>` @ $`<cost_rate>`/hr
- artifact: `<path>` (`<size>` bytes, sha256 `<sha>`)
- capability_key: `<ck>`
- captured `<timestamp>` at git SHA `<sha>`
- total live-smoke spend: ~$`<spend>` across `<n>` iterations

**Live-smoke bug catches integrated (`<n>` production fixes):**

1. `<commit>` — `<one-line>`
2. `<commit>` — `<one-line>`
… (one bullet per bug-catch commit on the branch)

**Key design decisions / deviations from spec:**

- (none, or list what diverged)

**Test count:** 823 pre-Layer-P → ~836 post-Layer-P (+13 net: 3 graph_file + 2 SHA-pin + 3 tag-discovery + 2 ComfyUI dispatch + 3 shape-lockdown; refactor of 23 tests is net 0).

**Out of scope (Layer P+ candidates):**

- Diffusers / Hosted engine on real RunPod (separate future layers).
- Serverless mode live smoke (PROGRESS:407 Layer N out-of-scope item still open).
- SkyPilot SDK smoke (PROGRESS:114 carry-forward #2).
- S3/GCS medium-fidelity tests (PROGRESS:114 carry-forward #3).
- Streaming per-entry log lines in `kinoforge batch` (PROGRESS:162).
- Batch CLI live smoke (Q8=A in Layer P brainstorm).
```

Substitute every `<...>` placeholder with real values from `tests/engines/fixtures/comfyui/last_smoke.json` and the branch's bug-catch commit list.

Also:
- Update PROGRESS:152 ("Single next action") to point at Layer Q candidate.
- Update PROGRESS:113 carry-forward list: mark Layer-O carry-forward #1 CLOSED.

- [ ] **Step 3: Full offline gate**

```bash
pixi run test
pixi run typecheck
pixi run lint
```

All must pass.

- [ ] **Step 4: Final pre-commit + commit doc updates**

```bash
pixi run pre-commit run --all-files
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: Layer P — README engine-integration sub-section + PROGRESS Phase 26 (Layer P Task 10)

Documents the ComfyUI + Wan i2v live smoke: env vars, quickstart,
KEEP_POD dev loop, fixture capture. Closes Layer-O carry-forward #1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: Merge to main via `--no-ff`**

```bash
git checkout main
git pull --ff-only
git merge --no-ff build/layer-p -m "$(cat <<'EOF'
Merge branch 'build/layer-p': RunPod engine integration (Layer P)

First real MP4 from kinoforge end-to-end on real cloud compute:
ComfyUI + Wan 2.2 i2v on real RunPod pod produces a valid MP4 artifact.
Closes Layer-O carry-forward #1.

Tasks 1–10 all complete. Live-smoke bug catches integrated as
in-branch production fixes (see PROGRESS Phase 26 for details).

All 14 ACs from the design spec satisfied:
- AC1 graph_file loader, AC2 graph committed, AC3 SHA pinning,
- AC4 tag discovery, AC5 ComfyUI HTTP capture, AC6 real MP4,
- AC7 fixtures+refactor, AC8 shape lockdown, AC9 warm-pod reuse,
- AC10 cost-safety quadruple-lock, AC11 CI offline-green,
- AC12 docs, AC13 bug fixes folded back, AC14 core invariant preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Backfill the merge SHA in PROGRESS**

```bash
MERGE_SHA=$(git rev-parse HEAD)
# Edit PROGRESS.md Task 10 line to substitute the real merge SHA
```

Edit `PROGRESS.md` to insert the merge SHA into the Task 10 line and the "Single next action" block. Commit:

```bash
git add PROGRESS.md
git commit -m "chore(docs): backfill Layer P merge commit SHA in PROGRESS"
```

---

## Self-review

**1. Spec coverage:**

| Spec section | Owning task |
|---|---|
| §1 Architecture / file inventory | Tasks 1–10 (one per file) |
| §2 `spec.graph_file` loader | Task 1 |
| §3 Custom-node SHA pinning | Task 2 |
| §4 Tag-discovery helper | Task 3 |
| §5 ComfyUI HTTP recording seam extension | Task 4 |
| §6 Live smoke control flow | Task 5 (skeleton) + Task 7 (live drive) |
| §7 Wan i2v graph + model set | Task 6 (scaffold) + Task 7 (real values) |
| §8 Offline ComfyUIBackend refactor | Task 8 |
| §9 ACs 1–14 | Tasks 1, 6 / 2 / 3 / 4 / 5 / 7 / 7+8 / 9 / 7 / 5+7 / 10 / 10 / 7 / Tasks 1+4 |
| §10 Non-goals / risk / open knobs | (not implemented; spec-only) |

No spec section uncovered.

**2. Placeholder scan:** No `TBD`, `TODO`, `implement later` in task bodies. Two intentional placeholders in YAML / graph JSON files (`PINME` SHAs + placeholder graph) — both explicitly resolved in Task 7.

**3. Type consistency:** `find_instance_by_tag(key: str, value: str) -> Instance | None` consistent across Task 3 implementation, Task 5 skeleton call site, Task 3 tests. `_load_comfy_fixture(name: str) -> dict[str, Any]` consistent between Task 8 conftest and Task 9 shape-lockdown tests. `_COMFY_DISPATCH` + `_RUNPOD_DISPATCH` named consistently across Task 4 conftest and Task 5 skeleton imports.

**4. User-gate check:** Trigger scan against user's brief — user explicitly said "There is no need for a user gate." Task 7 is the obvious candidate, but the user has authorized the agent to drive the live smoke autonomously. NO tasks tagged `userGate: true`.

---

## Execution
