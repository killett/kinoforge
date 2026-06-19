# Doctor-smoke xfail cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive `tests/live/test_doctor_examples_live.py` from 9/21 clean → 21/21 clean by fixing the 12 xfailed example cfgs.

**Architecture:** Five independent edits across the example-cfg surface and the validation registry. (1) Filter `ModelRefReachableCheck` to engine kinds that actually HEAD-fetch the ref. (2) Move batch-manifest YAML out of the flat-Config glob path. (3) Pin Wan2.2 refs to a real HF Hub file. (4) Collapse `nova_reel` cfg onto the registered `bedrock_video` engine. (5) Delete the xfail dict and re-confirm with a live run.

**Tech Stack:** Python, pytest, ruff, mypy. Validation seam: `kinoforge.validation`.

**User decisions (already made):**
- Skip brainstorm — work is mechanical edits, not a design exercise. ("just skip straight to a plan")
- `nova_reel` is reduced to a cfg-level rename onto `bedrock_video`; no new engine module ships in this PR.

---

## Spec / context pointers

- Live smoke: `tests/live/test_doctor_examples_live.py` (xfail dict at lines 33-66, `KNOWN_ENGINES` reference at `src/kinoforge/core/config.py:72-82`).
- Validation registry entry-point: `src/kinoforge/validation/__init__.py` (`validate_for_doctor`).
- PROGRESS narrative anchor: §"Cfg validation Check Registry SHIPPED 2026-06-19" — five sub-buckets the xfails fall into.

## File-structure deltas

- **Edit** `src/kinoforge/validation/checks/models.py:71-75` — narrow `applies_to` to fetching engines.
- **Add** `tests/validation/test_model_ref_reachable_applies.py` — unit test for the new filter.
- **Move** `examples/configs/batch-prompts.yaml` → `examples/configs/manifests/batch-prompts.yaml`.
- **Move** `examples/configs/runpod-comfyui-wan-manifest.yaml` → `examples/configs/manifests/runpod-comfyui-wan-manifest.yaml`.
- **Edit** `tests/live/test_doctor_examples_live.py:26` — switch `rglob` → `glob` so the new subdir is excluded.
- **Edit** `tests/integration/test_no_unknown_slug_for_example_configs.py:25-29` — drop now-irrelevant skip entries.
- **Edit** `tests/test_examples.py:369+` — repoint manifest-load tests at the new paths.
- **Edit** 4 wan cfgs (`wan.yaml`, `skypilot.yaml`, `skypilot-gpu.yaml`, `skypilot-lambda.yaml`) — repoint `models[].ref`.
- **Edit** `examples/configs/nova-reel.yaml` — `kind: nova_reel` → `kind: bedrock_video`, restructure engine block.
- **Edit** `tests/live/test_nova_reel_live.py` — assertions + `get_engine` call match the new kind.
- **Edit** `tests/integration/test_no_unknown_slug_for_example_configs.py:27-29` — drop `nova-reel.yaml` from the skip list.
- **Edit** `tests/live/test_doctor_examples_live.py:33-66` — delete `_KNOWN_BROKEN`, drop the request.applymarker call.
- **Update** `PROGRESS.md` — close the "fix 12 xfailed example cfgs" workstream.

---

## Task 1: Filter ModelRefReachableCheck to fetching engine kinds

**Goal:** Hosted, fal, replicate, runway, bedrock_video, and fake engines do not fetch `models[].ref` themselves — the wire identifier lives on `spec.model` / `model_id`. Make `ModelRefReachableCheck.applies_to` return False for those kinds so placeholder refs in those cfgs do not break doctor.

**Files:**
- Modify: `src/kinoforge/validation/checks/models.py` (lines 71-75)
- Create: `tests/validation/test_model_ref_reachable_applies.py`

**Acceptance Criteria:**
- [ ] `ModelRefReachableCheck.applies_to(cfg)` returns False when `cfg.engine.kind` is one of `{"hosted", "fal", "replicate", "runway", "bedrock_video", "fake"}`.
- [ ] Returns True (existing behaviour) for `comfyui`, `diffusers`.
- [ ] New unit test covers all 8 engine kinds (6 skip + 2 fetch).

**Verify:** `pixi run pytest tests/validation/test_model_ref_reachable_applies.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/validation/test_model_ref_reachable_applies.py
"""ModelRefReachableCheck.applies_to filters by engine kind.

Hosted / fal / replicate / runway / bedrock_video / fake engines do
not HEAD-fetch ``cfg.models[].ref`` themselves — the wire identifier
lives on ``spec.model`` or ``engine.<kind>.model_id``. Skipping the
check for those kinds lets their cfgs ship informational refs (e.g.
``bedrock://amazon.nova-reel-v1:1``) without doctor failing.
"""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config, EngineConfig, LifecycleConfig, ModelEntry
from kinoforge.validation.checks.models import ModelRefReachableCheck


def _cfg(kind: str) -> Config:
    return Config(
        engine=EngineConfig(kind=kind, precision="fp16"),
        models=[ModelEntry(ref="hf:owner/repo:file.safetensors", kind="base", target="checkpoints")],
        lifecycle=LifecycleConfig(budget=1.0),
    )


@pytest.mark.parametrize("kind", ["hosted", "fal", "replicate", "runway", "bedrock_video", "fake"])
def test_skips_for_non_fetching_engines(kind: str) -> None:
    """Non-fetching engines must not trigger the HEAD probe."""
    assert ModelRefReachableCheck().applies_to(_cfg(kind)) is False


@pytest.mark.parametrize("kind", ["comfyui", "diffusers"])
def test_applies_for_fetching_engines(kind: str) -> None:
    """Engines that pull weights from the ref must still be checked."""
    assert ModelRefReachableCheck().applies_to(_cfg(kind)) is True
```

- [ ] **Step 2: Run it — expect failures on the skip cases.**

Run: `pixi run pytest tests/validation/test_model_ref_reachable_applies.py -v`
Expected: 6 FAIL (skip cases — current `applies_to` returns True for any cfg with a network-scheme ref), 2 PASS (fetching cases).

- [ ] **Step 3: Narrow `applies_to` in `src/kinoforge/validation/checks/models.py`.**

Replace lines 71-75 with:

```python
    _NON_FETCHING_ENGINES: frozenset[str] = frozenset(
        {"hosted", "fal", "replicate", "runway", "bedrock_video", "fake"}
    )

    def applies_to(self, cfg: Config) -> bool:
        """Apply iff the engine fetches the ref and at least one ref carries a network scheme.

        Non-fetching engines (hosted shim, fal queue, Replicate / Runway
        Bearer providers, Bedrock video, fake) do not HEAD-resolve the
        ref — their wire identifier lives on ``spec.model`` or the
        engine sub-block. Skip them to avoid flagging informational
        placeholders.
        """
        if cfg.engine.kind in self._NON_FETCHING_ENGINES:
            return False
        if not cfg.models:
            return False
        return any(m.ref.startswith(("hf:", "https://", "http://")) for m in cfg.models)
```

Promote `_NON_FETCHING_ENGINES` to a class attribute so the test can also reference it if needed and so the constant lives next to the logic that consumes it.

- [ ] **Step 4: Run the test — expect all pass.**

Run: `pixi run pytest tests/validation/test_model_ref_reachable_applies.py -v`
Expected: 8 PASS.

- [ ] **Step 5: Run the validation regression suite — expect no other test breaks.**

Run: `pixi run pytest tests/validation/ -v`
Expected: all pre-existing tests still pass.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/validation/checks/models.py tests/validation/test_model_ref_reachable_applies.py
git commit -m "feat(validation): skip model_ref_reachable for non-fetching engine kinds

hosted / fal / replicate / runway / bedrock_video / fake engines do
not HEAD-fetch the models[].ref — wire identity lives on spec.model
or the engine sub-block. Skipping their cfgs lets informational
placeholders ship without breaking doctor.

Unblocks the hosted.yaml / fal.yaml / cost.yaml / local-fake.yaml
xfails in tests/live/test_doctor_examples_live.py."
```

---

## Task 2: Move batch-manifest YAML out of `examples/configs/` flat glob

**Goal:** Two example files (`batch-prompts.yaml`, `runpod-comfyui-wan-manifest.yaml`) are top-level YAML lists, not Config mappings. They cannot pass `kinoforge doctor`. Move them under `examples/configs/manifests/` and have the doctor live-smoke exclude that subdir while still recursing into `comparison/` (which holds 2 real Config cfgs: `replicate-t2v.yaml`, `runway-t2v.yaml`).

**Files:**
- Move: `examples/configs/batch-prompts.yaml` → `examples/configs/manifests/batch-prompts.yaml`
- Move: `examples/configs/runpod-comfyui-wan-manifest.yaml` → `examples/configs/manifests/runpod-comfyui-wan-manifest.yaml`
- Modify: `tests/live/test_doctor_examples_live.py` (line 26 — exclude `manifests/` subdir)
- Modify: `tests/integration/test_no_unknown_slug_for_example_configs.py` (remove the two manifest entries from the skip list)
- Modify: `tests/test_examples.py` (any path-references to the moved files)

**Acceptance Criteria:**
- [ ] Both manifest YAML files live under `examples/configs/manifests/`.
- [ ] `tests/test_examples.py` still loads both files via `load_manifest` after the path change.
- [ ] The doctor live-smoke parametrize list no longer enumerates the two manifests.
- [ ] `test_no_unknown_slug_for_example_configs.py` no longer skips them (they fall outside the cfg glob).

**Verify:** `pixi run pytest tests/test_examples.py tests/integration/test_no_unknown_slug_for_example_configs.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Move the files.**

```bash
mkdir -p examples/configs/manifests
git mv examples/configs/batch-prompts.yaml examples/configs/manifests/batch-prompts.yaml
git mv examples/configs/runpod-comfyui-wan-manifest.yaml examples/configs/manifests/runpod-comfyui-wan-manifest.yaml
```

- [ ] **Step 2: Exclude the `manifests/` subdir from the doctor glob.**

Edit `tests/live/test_doctor_examples_live.py` line 26 to filter `rglob` output by path parts (keeps `comparison/replicate-t2v.yaml` + `comparison/runway-t2v.yaml` in scope; drops anything under `manifests/`):

```python
# was:
_EXAMPLES = sorted((_REPO_ROOT / "examples/configs").rglob("*.yaml"))
# becomes:
_EXAMPLES = sorted(
    p for p in (_REPO_ROOT / "examples/configs").rglob("*.yaml")
    if "manifests" not in p.parts
)
```

Plain `glob("*.yaml")` would over-exclude the two `comparison/` cfgs.

- [ ] **Step 3: Repoint `tests/test_examples.py` path references.**

Locate every occurrence in the file:

```bash
rg -n 'batch-prompts\.yaml|runpod-comfyui-wan-manifest\.yaml' tests/test_examples.py
```

Replace each `examples/configs/batch-prompts.yaml` with `examples/configs/manifests/batch-prompts.yaml`, and likewise for the runpod manifest. Show the diff for each occurrence before saving — no other content change.

- [ ] **Step 4: Drop the two skip entries from `test_no_unknown_slug_for_example_configs.py`.**

Remove lines 25-26 (the `batch-prompts.yaml` and `runpod-comfyui-wan-manifest.yaml` skip entries). Leave the `nova-reel.yaml` entry — Task 4 removes that separately.

- [ ] **Step 5: Update README + PROGRESS references if any user-facing doc points at the old paths.**

```bash
rg -n 'examples/configs/(batch-prompts\.yaml|runpod-comfyui-wan-manifest\.yaml)' README.md docs/ PROGRESS.md
```

Repoint any hits to the `manifests/` subdir. Skip historical plan / spec docs under `docs/superpowers/plans/` and `docs/superpowers/specs/` — those are frozen.

- [ ] **Step 6: Run the regression slice.**

Run: `pixi run pytest tests/test_examples.py tests/integration/test_no_unknown_slug_for_example_configs.py -v`
Expected: all pass.

- [ ] **Step 7: Commit.**

```bash
git add examples/configs/manifests/ examples/configs/batch-prompts.yaml examples/configs/runpod-comfyui-wan-manifest.yaml tests/live/test_doctor_examples_live.py tests/integration/test_no_unknown_slug_for_example_configs.py tests/test_examples.py README.md PROGRESS.md
git commit -m "refactor(examples): move batch manifests to examples/configs/manifests/

batch-prompts.yaml and runpod-comfyui-wan-manifest.yaml are top-level
YAML lists (manifest format), not Config mappings. They fail
'kinoforge doctor' by construction. Move them into a manifests/
subdir and switch the doctor live-smoke glob to non-recursive so
the subdir is naturally excluded from the cfg sweep.

Drops the two corresponding skip entries from
tests/integration/test_no_unknown_slug_for_example_configs.py."
```

---

## Task 3: Pin Wan2.2 model refs to a real HF Hub file

**Goal:** Four cfgs reference `hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors`, but the repo never hosted that filename — the model ships sharded under `high_noise_model/` and `low_noise_model/` directories. Pin each ref to a real shard so HEAD returns 200.

**Background (HF Hub probe, 2026-06-19):** `Wan-AI/Wan2.2-T2V-A14B` exposes 12 shards across two subfolders (`high_noise_model/diffusion_pytorch_model-0000{1..6}-of-00006.safetensors` and the same under `low_noise_model/`), plus per-folder `diffusion_pytorch_model.safetensors.index.json` manifests. Pick the high-noise shard 1 as the canonical reachability anchor — small enough to HEAD fast, exists for both halves of the model, distinct from the index JSON so the loader still sees a `.safetensors` extension.

**Files:**
- Modify: `examples/configs/wan.yaml` (models[0].ref)
- Modify: `examples/configs/skypilot.yaml` (models[0].ref)
- Modify: `examples/configs/skypilot-gpu.yaml` (models[0].ref)
- Modify: `examples/configs/skypilot-lambda.yaml` (models[0].ref)

**Acceptance Criteria:**
- [ ] All four cfgs reference `hf:Wan-AI/Wan2.2-T2V-A14B:high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors`.
- [ ] `curl -sI https://huggingface.co/Wan-AI/Wan2.2-T2V-A14B/resolve/main/high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors` returns 200/302.
- [ ] `wan.yaml` carries a comment block explaining the sharded layout so the next operator does not invent a new wrong filename.

**Verify:**

```bash
pixi run python -c "
from kinoforge.core.config import load_config
for p in ['examples/configs/wan.yaml','examples/configs/skypilot.yaml','examples/configs/skypilot-gpu.yaml','examples/configs/skypilot-lambda.yaml']:
    cfg = load_config(p)
    print(p, cfg.models[0].ref)
"
```

Expected: every line prints the same sharded ref, no exception.

**Steps:**

- [ ] **Step 1: Edit `examples/configs/wan.yaml`.**

Replace the `models` block first entry:

```yaml
models:
  # Wan-AI/Wan2.2-T2V-A14B ships as a sharded diffusers model:
  #   high_noise_model/diffusion_pytorch_model-{00001..00006}-of-00006.safetensors
  #   low_noise_model/diffusion_pytorch_model-{00001..00006}-of-00006.safetensors
  # The ref below points at the first high-noise shard as the
  # reachability anchor; the engine layer downloads the full set via
  # the per-folder safetensors.index.json. Do NOT shorten the ref to
  # bare `wan2.2_14b.safetensors` — that filename does not exist on
  # the Hub.
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors"
    kind: base
    target: checkpoints
```

Preserve the rest of the `models:` block (the CivitAI LoRA + the HTTPS VAE entries — unchanged).

- [ ] **Step 2: Edit `examples/configs/skypilot.yaml`.**

Replace the single `models[0].ref` line with the same sharded path:

```yaml
models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors"
    kind: base
    target: checkpoints
```

No additional comment block needed here — the canonical explanation lives in `wan.yaml`.

- [ ] **Step 3: Edit `examples/configs/skypilot-gpu.yaml`.** Same single-line change as Step 2.

- [ ] **Step 4: Edit `examples/configs/skypilot-lambda.yaml`.** Same single-line change as Step 2.

- [ ] **Step 5: Verify the new URL resolves (network).**

```bash
curl -sI "https://huggingface.co/Wan-AI/Wan2.2-T2V-A14B/resolve/main/high_noise_model/diffusion_pytorch_model-00001-of-00006.safetensors" | head -1
```

Expected: `HTTP/2 200` or `HTTP/2 302` (302 redirects to LFS — both count as PASS per `_PASS_CODES` in `models.py`).

- [ ] **Step 6: Verify each cfg loads.**

```bash
pixi run python -c "
from kinoforge.core.config import load_config
for p in ['examples/configs/wan.yaml','examples/configs/skypilot.yaml','examples/configs/skypilot-gpu.yaml','examples/configs/skypilot-lambda.yaml']:
    print(p, load_config(p).models[0].ref)
"
```

Expected: 4 lines, all printing the sharded ref.

- [ ] **Step 7: Commit.**

```bash
git add examples/configs/wan.yaml examples/configs/skypilot.yaml examples/configs/skypilot-gpu.yaml examples/configs/skypilot-lambda.yaml
git commit -m "fix(examples): pin Wan2.2 model refs to a real sharded HF file

The 'wan2.2_14b.safetensors' filename never existed on
Wan-AI/Wan2.2-T2V-A14B. Repo ships as a sharded diffusers model
under high_noise_model/ and low_noise_model/ subfolders. Pin the
canonical reachability anchor (high-noise shard 1) and document the
sharded layout in wan.yaml so the next operator does not invent
a different wrong filename."
```

---

## Task 4: Collapse `nova-reel.yaml` onto the registered `bedrock_video` engine

**Goal:** `nova-reel.yaml` declares `engine.kind: nova_reel`, which is not in `KNOWN_ENGINES`. The registered engine that backs Bedrock Nova Reel is `bedrock_video` (`src/kinoforge/engines/bedrock_video/__init__.py:425`). Convert the cfg + the live smoke to the registered kind; no new engine module ships in this PR.

**Files:**
- Modify: `examples/configs/nova-reel.yaml` (engine block restructure)
- Modify: `tests/live/test_nova_reel_live.py` (assertion + `get_engine` call updates)
- Modify: `tests/integration/test_no_unknown_slug_for_example_configs.py` (drop `nova-reel.yaml` skip entry)

**Acceptance Criteria:**
- [ ] `nova-reel.yaml` declares `engine.kind: bedrock_video` and nests its parameters under `engine.bedrock_video:`.
- [ ] `cfg.engine.kind == "bedrock_video"` for the loaded cfg.
- [ ] `tests/integration/test_no_unknown_slug_for_example_configs.py` runs `nova-reel.yaml` instead of skipping it.
- [ ] `tests/live/test_nova_reel_live.py` still parses but its `get_engine` call resolves to `"bedrock_video"`.

**Verify:** `pixi run pytest tests/integration/test_no_unknown_slug_for_example_configs.py -v` → all pass, no skips for `nova-reel.yaml`.

**Steps:**

- [ ] **Step 1: Edit `examples/configs/nova-reel.yaml`.**

Replace the `engine` block (and only the `engine` block) with:

```yaml
engine:
  kind: bedrock_video
  precision: fp16
  bedrock_video:
    region_name: us-east-1
    model_id: amazon.nova-reel-v1:1
    output_s3_uri: s3://kinoforge-nova-reel-output/
    duration_seconds: 6
    fps: 24
    dimension: 1280x720
    prompt_body_key: prompt
```

Keep the file's top-of-file comment block, the `models:`, `lifecycle:`, `spec:`, and `params:` blocks unchanged.

- [ ] **Step 2: Edit `tests/live/test_nova_reel_live.py` line 77.**

```python
# was:
assert cfg.engine.kind == "nova_reel"
# becomes:
assert cfg.engine.kind == "bedrock_video"
```

- [ ] **Step 3: Edit the engine factory call at `tests/live/test_nova_reel_live.py:87`.**

```python
# was:
engine_factory = get_engine("nova_reel")
# becomes:
engine_factory = get_engine("bedrock_video")
```

- [ ] **Step 4: Drop the `nova-reel.yaml` skip entry from `tests/integration/test_no_unknown_slug_for_example_configs.py`.**

Remove lines 27-29 (the comment + `"nova-reel.yaml"` skip-list entry).

- [ ] **Step 5: Run the integration regression.**

```bash
pixi run pytest tests/integration/test_no_unknown_slug_for_example_configs.py -v
```

Expected: all pass, `nova-reel.yaml` is now tested (not skipped).

- [ ] **Step 6: Run the cfg load sanity check.**

```bash
pixi run python -c "from kinoforge.core.config import load_config; cfg = load_config('examples/configs/nova-reel.yaml'); print(cfg.engine.kind)"
```

Expected: prints `bedrock_video`.

- [ ] **Step 7: Commit.**

```bash
git add examples/configs/nova-reel.yaml tests/live/test_nova_reel_live.py tests/integration/test_no_unknown_slug_for_example_configs.py
git commit -m "refactor(examples): collapse nova-reel.yaml onto bedrock_video engine

The registered engine that drives Bedrock Nova Reel is
'bedrock_video' (src/kinoforge/engines/bedrock_video/__init__.py).
'nova_reel' was a planned engine kind that never shipped its own
module — point the cfg + live smoke at the existing engine kind
instead. No engine code change."
```

---

## Task 5: Drop the doctor-smoke xfail dict + run live smoke

**Goal:** Tasks 1-4 cover the five xfail buckets. Remove `_KNOWN_BROKEN` from `tests/live/test_doctor_examples_live.py`, drop the `request.applymarker` branch, and run the live smoke to confirm every remaining example cfg passes `kinoforge doctor`.

**Files:**
- Modify: `tests/live/test_doctor_examples_live.py` (delete xfail dict, simplify test body)
- Modify: `PROGRESS.md` (close the workstream)

**Acceptance Criteria:**
- [ ] `_KNOWN_BROKEN` dict deleted; `request` fixture no longer used; `xfail` import (if any) gone.
- [ ] `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_doctor_examples_live.py -v` reports every cfg as PASS — zero xfail, zero fail.
- [ ] `PROGRESS.md` workstream entry is closed with the commit SHAs from Tasks 1-4 and a one-line summary.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_doctor_examples_live.py -v` → all PASS, no XFAIL / XPASS.

**Steps:**

- [ ] **Step 1: Strip `_KNOWN_BROKEN` from `tests/live/test_doctor_examples_live.py`.**

The final file should look like:

```python
"""Live network smoke: every example cfg passes ``kinoforge doctor``.

Gated by ``KINOFORGE_LIVE_TESTS=1``. No pod creation — HEAD probes
only (image registry, HF Hub, GitHub commit URLs). Validates that
none of the example cfgs ship a placeholder image / unreachable
model ref / archived custom-node SHA.

The reference cfg surface lives at ``examples/configs/**/*.yaml``
EXCLUDING ``examples/configs/manifests/`` — that subdir holds batch
manifest YAML (top-level list, not a Config mapping) which is
exercised by ``tests/test_examples.py`` instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kinoforge.providers.runpod  # noqa: F401 — self-register RunPod check
import kinoforge.providers.skypilot  # noqa: F401 — self-register SkyPilot check
import kinoforge.validation.checks  # noqa: F401 — self-register built-ins
from kinoforge.core.config import _parse_cfg_raw
from kinoforge.validation import validate_for_doctor

_LIVE = os.environ.get("KINOFORGE_LIVE_TESTS") == "1"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES = sorted(
    p for p in (_REPO_ROOT / "examples/configs").rglob("*.yaml")
    if "manifests" not in p.parts
)


@pytest.mark.skipif(not _LIVE, reason="KINOFORGE_LIVE_TESTS not set")
@pytest.mark.parametrize(
    "cfg_path", _EXAMPLES, ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_example_cfg_passes_doctor(cfg_path: Path) -> None:
    """Every example cfg passes the full doctor validation pass."""
    cfg = _parse_cfg_raw(cfg_path.read_text(), yaml_path=cfg_path)
    report = validate_for_doctor(cfg)
    error_names = [r.name for r in report.errors]
    assert not report.errors, (
        f"{cfg_path.relative_to(_REPO_ROOT)} failed doctor: "
        f"{error_names}\n{report.format()}"
    )
```

Note: `request: pytest.FixtureRequest` parameter is removed; `_KNOWN_BROKEN` deleted; module docstring updated; glob already switched to `rglob` + `manifests` filter in Task 2 — re-confirm the line still excludes that subdir.

- [ ] **Step 2: Static slice — confirm the file imports clean.**

```bash
pixi run python -c "import tests.live.test_doctor_examples_live as m; print(len(m._EXAMPLES))"
```

Expected: prints `19` — 21 total cfgs under `examples/configs/` (recursive) minus 2 manifests rehomed under `manifests/` (now excluded by the `"manifests" not in p.parts` filter). `nova-reel.yaml` stays; `comparison/replicate-t2v.yaml` + `comparison/runway-t2v.yaml` stay.

- [ ] **Step 3: Live smoke. Network-on.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_doctor_examples_live.py -v
```

Expected: 19 PASSED, 0 XFAIL, 0 XPASS, 0 FAIL.

If a cfg still errors:
1. Capture the failure output verbatim into the conversation.
2. Identify which of the five buckets it belongs to.
3. Patch the specific cfg / check; commit; re-run.
Do NOT re-add `_KNOWN_BROKEN`.

- [ ] **Step 4: Full regression suite (offline).**

```bash
pixi run pytest -q --ignore=tests/live --ignore=tests/core/test_pool_cancel.py
```

(`test_pool_cancel.py` excluded per the parked thread-leak workstream — full suite hangs ~24 min on the leaked non-daemon thread.)
Expected: green.

- [ ] **Step 5: Update PROGRESS.md.**

Locate the "Cfg validation Check Registry SHIPPED 2026-06-19" block. Append a CLOSED entry below the existing follow-up bullets:

```markdown
**12 xfailed example cfgs CLOSED 2026-06-19.**
Plan `docs/superpowers/plans/2026-06-19-doctor-smoke-xfail-cleanup.md`
landed on `main`. Five edits unblocked all 12 entries: (1)
`ModelRefReachableCheck.applies_to` filters non-fetching engine
kinds (commit <SHA-1>); (2) batch manifests moved to
`examples/configs/manifests/` and doctor glob switched to
non-recursive (commit <SHA-2>); (3) Wan2.2 refs repointed to a real
sharded HF file (commit <SHA-3>); (4) `nova-reel.yaml` collapsed
onto `bedrock_video` (commit <SHA-4>); (5) `_KNOWN_BROKEN` deleted +
live smoke re-run green (commit <SHA-5>). Doctor live smoke now
19/19 clean (21 cfgs recursive minus 2 manifests rehomed under
`examples/configs/manifests/`).
```

Replace each `<SHA-N>` placeholder with the actual commit SHA from each prior task before committing PROGRESS.md.

- [ ] **Step 6: Commit.**

```bash
git add tests/live/test_doctor_examples_live.py PROGRESS.md
git commit -m "test(validation): drop doctor-smoke xfail dict — live run 19/19 clean

Tasks 1-4 cleared every entry in _KNOWN_BROKEN. Remove the dict and
the request.applymarker branch; doctor live smoke now reports all
cfgs PASS with KINOFORGE_LIVE_TESTS=1."
```
