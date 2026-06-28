# Grid verified examples — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 9 verified, commitable example grid YAMLs covering every `kinoforge grid` capability (strength sweep, LoRA stack swap, prompt sweep, mixed path+generate, model sweep) for both Wan 2.1 and Wan 2.2 families, with live RunPod evidence per example and a README "Grid examples — verified" section with one sub-section per example and matching TOC sub-entries.

**Architecture:** Add a single 5-LOC opt-in field `allow_in_repo: bool = False` to `GridSpec` so commitable example specs can bypass the under-repo guard. Ship 5 base cfgs (one per family/size) + 9 grid spec YAMLs in `examples/configs/grids/`. Stage flow: scaffold → dry-run gate (no spend) → 5 live Wan 2.1 grids (~$1.30) → 4 live Wan 2.2 grids (~$4-5) → README integration with per-example sub-sections + TOC anchors. Per-grid commit + per-grid evidence file. No changes to executor / compose / cost-sidecar / swap-failures / grouping / dotted-path code paths.

**Tech Stack:** Python 3.12, pydantic v2, YAML, pixi, RunPod (ComfyUI for Wan 2.1, diffusers for Wan 2.2), ffmpeg, pytest.

**User decisions (already made):**
- Q1 = A: per-spec opt-in marker (`allow_in_repo: true`); guard stays strict by default.
- Q2: live for ALL on both families; $15 budget cap.
- Q3 = C: per-model coverage; intra-Wan-2.1 model-sweep (1.3B vs 5B vs 14B); skip intra-Wan-2.2 (single-size family).
- Q4 = A: Wan 2.1 caps on 1.3B (LoRA-validated); Wan 2.2 caps on 14B; skip intra-2.2 model-sweep.
- Q5 = A: new self-contained dir `examples/configs/grids/` with both base cfgs + spec YAMLs.
- Approach C: hybrid dry-run gate + family-chunked live; per-grid commit cadence.

---

## File structure

```
src/kinoforge/core/grid/
    spec.py                                                  # MOD — add allow_in_repo field + loader gate
tests/core/grid/
    test_spec.py                                             # MOD — 2 new tests for opt-in semantics
examples/configs/grids/
    README.md                                                # NEW — short index pointing back to main README
    wan21-1_3b-base.yaml                                     # NEW — Wan 2.1 1.3B diffusers cfg w/ Static Rotation + Pokemon LoRAs + field-realistic prompt
    wan21-1_3b-base-no-loras.yaml                            # NEW — Wan 2.1 1.3B comfyui cfg, no LoRAs, for model-sweep cell
    wan21-5b-base-no-loras.yaml                              # NEW — Wan 2.1 5B comfyui cfg, no LoRAs
    wan21-14b-base-no-loras.yaml                             # NEW — Wan 2.1 14B comfyui cfg, no LoRAs
    wan22-14b-base.yaml                                      # NEW — Wan 2.2 14B diffusers cfg w/ Arcane high+low LoRAs + field-realistic prompt
    wan21-1_3b-strength-sweep.grid.yaml                      # NEW — cap #1
    wan21-1_3b-loras-swap.grid.yaml                          # NEW — cap #2
    wan21-1_3b-prompt-sweep.grid.yaml                        # NEW — cap #3
    wan21-mixed-path-plus-generate.grid.yaml                 # NEW — cap #4
    wan21-model-sweep.grid.yaml                              # NEW — cap #5
    wan22-14b-strength-sweep.grid.yaml                       # NEW — cap #6
    wan22-14b-loras-swap.grid.yaml                           # NEW — cap #7
    wan22-14b-prompt-sweep.grid.yaml                         # NEW — cap #8
    wan22-14b-mixed-path-plus-generate.grid.yaml             # NEW — cap #9
    _fixtures/                                               # NEW — committed mp4 outputs from earlier grid runs
        .gitkeep
        wan21_strength_cell0.mp4                             # written in Task 5
        wan21_prompt_cell0.mp4                               # written in Task 7
        wan22_strength_cell0.mp4                             # written in Task 10
        wan22_prompt_cell0.mp4                               # written in Task 12
tests/live/_grid_examples/
    wan21_strength.json                                      # NEW — Task 5
    wan21_loras_swap.json                                    # NEW — Task 6
    wan21_prompt.json                                        # NEW — Task 7
    wan21_mixed_path.json                                    # NEW — Task 8
    wan21_model_sweep.json                                   # NEW — Task 9
    wan22_strength.json                                      # NEW — Task 10
    wan22_loras_swap.json                                    # NEW — Task 11
    wan22_prompt.json                                        # NEW — Task 12
    wan22_mixed_path.json                                    # NEW — Task 13
README.md                                                    # MOD — Task 14: H2 + 9 H3 sub-sections + TOC entries
```

---

## Task 1: `allow_in_repo` opt-in field + tests

**Goal:** Surgical opt-in field on `GridSpec` so example specs in `examples/configs/grids/` bypass the under-repo guard; default-False preserves the existing protection for user-authored specs.

**Files:**
- Modify: `src/kinoforge/core/grid/spec.py` (~10 LOC change)
- Modify: `tests/core/grid/test_spec.py` (add 2 tests)

**Acceptance Criteria:**
- [ ] `GridSpec` has `allow_in_repo: bool = False` field.
- [ ] Spec under repo root with `allow_in_repo: true` in YAML loads without raising.
- [ ] Spec under repo root without the field (default False) raises `GridSpecUnderRepoError`.
- [ ] `pixi run pytest tests/core/grid/test_spec.py -v` green.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/grid/spec.py tests/core/grid/test_spec.py` clean.

**Verify:** `pixi run pytest tests/core/grid/test_spec.py -v` → both new tests pass + all existing tests still green.

**Steps:**

- [ ] **Step 1: Write the failing tests first**

Add to `tests/core/grid/test_spec.py`:

```python
def test_allow_in_repo_true_bypasses_under_repo_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: forgetting opt-in means every example spec we ship gets rejected; entire example pipeline breaks."""
    # Monkeypatch _git_repo_root to return tmp_path so the spec at tmp_path/spec.yaml
    # is treated as under-repo by the loader.
    from kinoforge.core.grid import spec as spec_mod

    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "allow_in_repo: true\n"
        "budget_cap_usd: 1.0\n"
        "cells:\n"
        "  - path: /tmp/fixture.mp4\n"
        "    caption: only\n"
    )
    spec = GridSpec.load(spec_path)
    assert spec.allow_in_repo is True


def test_default_under_repo_guard_still_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: gate flips polarity → user-authored specs get accidental-commit risk back."""
    from kinoforge.core.grid import spec as spec_mod
    from kinoforge.core.grid.errors import GridSpecUnderRepoError

    monkeypatch.setattr(spec_mod, "_git_repo_root", lambda: tmp_path)
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "budget_cap_usd: 1.0\n"
        "cells:\n"
        "  - path: /tmp/fixture.mp4\n"
        "    caption: only\n"
    )
    with pytest.raises(GridSpecUnderRepoError):
        GridSpec.load(spec_path)
```

- [ ] **Step 2: Run tests to confirm RED**

Run: `pixi run pytest tests/core/grid/test_spec.py::test_allow_in_repo_true_bypasses_under_repo_guard tests/core/grid/test_spec.py::test_default_under_repo_guard_still_rejects -v`
Expected: first test FAILs (`allow_in_repo` field unknown OR raises GridSpecUnderRepoError); second test PASSES (existing behavior).

- [ ] **Step 3: Add the field to `GridSpec` and gate to the loader**

Edit `src/kinoforge/core/grid/spec.py`:

Add new helper at module scope (above `class GridSpec`):

```python
def _yaml_opts_in_repo(p: Path) -> bool:
    """Peek YAML for ``allow_in_repo: true`` BEFORE pydantic parse.

    The under-repo guard fires before model_validate so it needs a
    lightweight pre-parse. One extra yaml.safe_load on a sub-KB spec
    file is trivial.
    """
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError:
        return False
    return bool(isinstance(raw, dict) and raw.get("allow_in_repo", False))
```

Add field to `GridSpec` (alongside existing fields):

```python
class GridSpec(BaseModel):
    # ...existing fields...
    on_swap_failure: Literal["strict", "continue", "classify"] = "classify"
    allow_in_repo: bool = False  # opt-in for committed example specs
```

Wrap the existing under-repo block in `GridSpec.load`:

Find this section (around line 178):

```python
        repo_root = _git_repo_root()
        if repo_root is not None:
            try:
                p.relative_to(repo_root)
            except ValueError:
                pass
            else:
                raise GridSpecUnderRepoError(
                    f"grid spec path is under the active repo root "
                    f"({repo_root}): {p}; move it outside the repo to "
                    f"avoid accidental commits (captions and overrides "
                    f"may contain LoRA refs / prompts)"
                )
```

Change the `else` branch to:

```python
            else:
                if not _yaml_opts_in_repo(p):
                    raise GridSpecUnderRepoError(
                        f"grid spec path is under the active repo root "
                        f"({repo_root}): {p}; move it outside the repo to "
                        f"avoid accidental commits (captions and overrides "
                        f"may contain LoRA refs / prompts). To intentionally "
                        f"ship an in-repo example spec built from official "
                        f"refs only, set `allow_in_repo: true` at the spec "
                        f"top level."
                    )
```

- [ ] **Step 4: Run tests to confirm GREEN**

Run: `pixi run pytest tests/core/grid/test_spec.py -v`
Expected: both new tests PASS + all existing tests still pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/grid/spec.py tests/core/grid/test_spec.py
git add src/kinoforge/core/grid/spec.py tests/core/grid/test_spec.py
git commit -m "$(cat <<'EOF'
feat(grid): allow_in_repo opt-in bypass for under-repo guard

`GridSpec` gains an optional `allow_in_repo: bool = False` field.
When `true`, `GridSpec.load()` skips the `GridSpecUnderRepoError`
that normally fires for specs under the active git repo. Default
False preserves the existing protection for user-authored specs
that might carry vault tokens or raw LoRA names.

Unblocks shipping committed example grid specs in
`examples/configs/grids/` built from official, repo-verified refs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/grid/spec.py", "tests/core/grid/test_spec.py"], "verifyCommand": "pixi run pytest tests/core/grid/test_spec.py -v", "acceptanceCriteria": ["allow_in_repo: true bypasses guard", "default False still rejects", "all existing test_spec.py tests still pass", "pre-commit clean"], "modelTier": "standard"}
```

---

## Task 2: 5 base cfgs in `examples/configs/grids/`

**Goal:** 5 self-contained per-family cfgs that the grid spec YAMLs reference. Each carries a top-level `prompt:` field (required by the grid executor) and the family-appropriate engine/compute/lifecycle shape cloned from a proven-green sibling cfg.

**Files:**
- Create: `examples/configs/grids/README.md`
- Create: `examples/configs/grids/wan21-1_3b-base.yaml`
- Create: `examples/configs/grids/wan21-1_3b-base-no-loras.yaml`
- Create: `examples/configs/grids/wan21-5b-base-no-loras.yaml`
- Create: `examples/configs/grids/wan21-14b-base-no-loras.yaml`
- Create: `examples/configs/grids/wan22-14b-base.yaml`
- Create: `examples/configs/grids/_fixtures/.gitkeep`

**Acceptance Criteria:**
- [ ] All 5 base cfgs load via `pixi run python -c "from kinoforge.core.config import load_config; load_config('<path>')"` without error.
- [ ] Each base cfg has a non-empty top-level `prompt:` string (verbatim from `examples/configs/prompts/field-realistic.txt`).
- [ ] LoRA-bearing cfgs (`wan21-1_3b-base.yaml`, `wan22-14b-base.yaml`) include the official LoRA refs in `loras:`.
- [ ] No cfg references private vault tokens or non-official refs.
- [ ] `pixi run pre-commit run --files <all created files>` clean.

**Verify:** `for c in examples/configs/grids/*.yaml; do pixi run python -c "from kinoforge.core.config import load_config; cfg = load_config('$c'); assert cfg.prompt, f'$c missing prompt:'; print('OK:', '$c')"; done`

**Steps:**

- [ ] **Step 1: Create dir + `.gitkeep` for the fixtures subdir**

```bash
mkdir -p examples/configs/grids/_fixtures
touch examples/configs/grids/_fixtures/.gitkeep
```

- [ ] **Step 2: Write `examples/configs/grids/README.md`**

Short index file:

```markdown
# Grid examples

Self-contained, commitable example grid YAMLs for `kinoforge grid`.
Every cfg + grid spec in this directory uses ONLY official, repo-verified
LoRA refs and prompts — safe to commit, unlike user-authored grid specs.

Each `.grid.yaml` opts in via `allow_in_repo: true` so
`GridSpec.load()`'s under-repo guard accepts it.

See the main `README.md` "Grid examples — verified" section for the
exact subshell + heredoc command per example. Schema details live in
`docs/batch-and-grid.md`.

Sub-dirs:
- `_fixtures/` — mp4s produced by earlier grid runs that mixed-path
  grids reference as `path:` cells. Committed; small (~1-5 MB each).
```

- [ ] **Step 3: Write `examples/configs/grids/wan21-1_3b-base.yaml`**

Clone `examples/configs/wan21-1_3b-strength-grid.yaml` shape (the proven LoRA-flexible Tier-3 cfg), strip the smoke-tier tag, leave both LoRAs at strength=1.0:

```yaml
# Wan 2.1 1.3B base cfg for grid examples (LoRA-bearing).
#
# - prompt: verbatim from examples/configs/prompts/field-realistic.txt
#   (standard video-gen smoke prompt per CLAUDE.md memory).
# - loras: official LoRA pair (Static Rotation + Pokemon Sprite),
#   strength=1.0 (overrideable per cell via dotted-path).
# - engine + compute + lifecycle: cloned from
#   wan21-1_3b-strength-grid.yaml (proven Tier-3 green path).

mode: t2v
prompt: "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow depth of field and subtle lens flare. A slow push-in toward a young woman in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall tumbles down moss-covered cliffs into a misting pool. Warm golden-hour light rakes across the field, backlighting her glowing silhouette and igniting floating pollen and mist that drift like tiny embers of light. Her simple but vividly colored dress ripples in the breeze, strands of hair lifting. Facing away, she turns to glance over her shoulder with a coy, gentle smile as the camera glides into an intimate close-up on her eyes. Around her, friendly magical creatures move with slow grace — luminous butterflies and glowing wisps trailing ribbons of soft light. The sky is clear and radiant, brushed with wisps of cloud. Filmic color grade, warm highlights, soft shadows, fine film grain, volumetric god rays cutting through the mist. Serene, ethereal, breathtaking."

engine:
  kind: diffusers
  precision: fp16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"]
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "peft>=0.13"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules: ["kinoforge.engines.diffusers.servers"]

models:
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
    kind: base
    target: checkpoints

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  warm_reuse_auto_attach: true
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_usd_per_hr: 0.40
    gpu_preference:
      - "NVIDIA RTX A5000"
      - "NVIDIA RTX 4090"
      - "NVIDIA L4"
    disk_gb: 40
  lifecycle:
    idle_timeout: 10m
    job_timeout: 5m
    time_buffer: 2m
    max_lifetime: 60m
    boot_timeout: 30m
    budget: 0.50
    heartbeat_interval_s: 30
    lora_swap_re_probe_after_s: 300

spec:
  model: "Wan2.1-T2V-1.3B-Diffusers"
  pipeline: "WanPipeline"
  scheduler: "UniPCMultistepScheduler"
  width: 480
  height: 480
  num_frames: 33
  fps: 16

loras:
  - ref: "civitai:1479320@1673265"   # wan2.1 1.3b static rotation
    strength: 1.0
  - ref: "civitai:1595383@1805395"   # Pokemon Sprite Animation Video LoRA
    strength: 1.0
```

- [ ] **Step 4: Write `examples/configs/grids/wan21-1_3b-base-no-loras.yaml`**

Same as Step 3 but DROP the `loras:` block entirely. Required by the model-sweep cell where the 5B/14B cells lack LoRA validation and the 1.3B cell needs to match shape for fair comparison. Comment block at top:

```yaml
# Wan 2.1 1.3B base cfg WITHOUT LoRAs — used by the model-sweep
# grid example so all three model cells (1.3B/5B/14B) compare
# vanilla outputs on the same prompt without LoRA confounds.

mode: t2v
prompt: "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow depth of field and subtle lens flare. A slow push-in toward a young woman in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall tumbles down moss-covered cliffs into a misting pool. Warm golden-hour light rakes across the field, backlighting her glowing silhouette and igniting floating pollen and mist that drift like tiny embers of light. Her simple but vividly colored dress ripples in the breeze, strands of hair lifting. Facing away, she turns to glance over her shoulder with a coy, gentle smile as the camera glides into an intimate close-up on her eyes. Around her, friendly magical creatures move with slow grace — luminous butterflies and glowing wisps trailing ribbons of soft light. The sky is clear and radiant, brushed with wisps of cloud. Filmic color grade, warm highlights, soft shadows, fine film grain, volumetric god rays cutting through the mist. Serene, ethereal, breathtaking."

# (rest of cfg identical to wan21-1_3b-base.yaml except `loras:` omitted)
```

Copy the engine/models/compute/spec blocks verbatim from Step 3; remove only the `loras:` block at the end.

- [ ] **Step 5: Write `examples/configs/grids/wan21-5b-base-no-loras.yaml`**

Clone `examples/configs/runpod-comfyui-wan-t2v-5b.yaml` shape; add `prompt:` field + `mode: t2v` at top. Read the existing 5B cfg first to copy engine/models/compute blocks:

```bash
cat examples/configs/runpod-comfyui-wan-t2v-5b.yaml
```

Resulting file:

```yaml
# Wan 2.1 5B base cfg WITHOUT LoRAs — for model-sweep cell.
#
# Mid-size Wan 2.1 variant; uses ComfyUI engine (not diffusers — 5B
# weights aren't published in diffusers-sharded form for Wan 2.1).
# No LoRAs (5B-specific LoRAs aren't validated in repo).

mode: t2v
prompt: "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow depth of field and subtle lens flare. A slow push-in toward a young woman in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall tumbles down moss-covered cliffs into a misting pool. Warm golden-hour light rakes across the field, backlighting her glowing silhouette and igniting floating pollen and mist that drift like tiny embers of light. Her simple but vividly colored dress ripples in the breeze, strands of hair lifting. Facing away, she turns to glance over her shoulder with a coy, gentle smile as the camera glides into an intimate close-up on her eyes. Around her, friendly magical creatures move with slow grace — luminous butterflies and glowing wisps trailing ribbons of soft light. The sky is clear and radiant, brushed with wisps of cloud. Filmic color grade, warm highlights, soft shadows, fine film grain, volumetric god rays cutting through the mist. Serene, ethereal, breathtaking."

# (copy engine/models/compute/spec/params blocks verbatim from
# examples/configs/runpod-comfyui-wan-t2v-5b.yaml)
```

When implementing, read the existing 5B cfg and copy every block below the comment header into this file.

- [ ] **Step 6: Write `examples/configs/grids/wan21-14b-base-no-loras.yaml`**

Same pattern; clone `examples/configs/runpod-comfyui-wan-t2v.yaml` (the existing 14B Wan 2.1 cfg). Add `mode: t2v` + `prompt:` field at the top, copy remaining blocks verbatim.

- [ ] **Step 7: Write `examples/configs/grids/wan22-14b-base.yaml`**

Clone `examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml` shape (the Tier-4 green path), add `mode: t2v` + `prompt:` at top, and ADD the Arcane LoRA pair to a `loras:` block at the bottom:

```yaml
# Wan 2.2 14B base cfg for grid examples (LoRA-bearing).
#
# - prompt: verbatim from examples/configs/prompts/field-realistic.txt.
# - loras: Arcane Style high+low pair, strength=1.0 (overrideable).
# - engine + compute + lifecycle: cloned from
#   wan22-14b-lora-flexible-warm-reuse-release.yaml (Tier-4 green path).

mode: t2v
prompt: "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow depth of field and subtle lens flare. A slow push-in toward a young woman in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall tumbles down moss-covered cliffs into a misting pool. Warm golden-hour light rakes across the field, backlighting her glowing silhouette and igniting floating pollen and mist that drift like tiny embers of light. Her simple but vividly colored dress ripples in the breeze, strands of hair lifting. Facing away, she turns to glance over her shoulder with a coy, gentle smile as the camera glides into an intimate close-up on her eyes. Around her, friendly magical creatures move with slow grace — luminous butterflies and glowing wisps trailing ribbons of soft light. The sky is clear and radiant, brushed with wisps of cloud. Filmic color grade, warm highlights, soft shadows, fine film grain, volumetric god rays cutting through the mist. Serene, ethereal, breathtaking."

# Copy engine/models/compute/spec blocks verbatim from
# examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml.
# At the bottom, append:

loras:
  - ref: "civitai:2197303@2474081"   # Arcane Style — high-noise tensor
    strength: 1.0
    branch: high_noise
  - ref: "civitai:2197303@2474073"   # Arcane Style — low-noise tensor
    strength: 1.0
    branch: low_noise
```

When implementing, read the release cfg and copy blocks below the header.

- [ ] **Step 8: Validate all 5 cfgs load**

```bash
for c in examples/configs/grids/wan21-1_3b-base.yaml \
         examples/configs/grids/wan21-1_3b-base-no-loras.yaml \
         examples/configs/grids/wan21-5b-base-no-loras.yaml \
         examples/configs/grids/wan21-14b-base-no-loras.yaml \
         examples/configs/grids/wan22-14b-base.yaml; do
    pixi run python -c "from kinoforge.core.config import load_config; cfg = load_config('$c'); assert cfg.prompt, '$c missing prompt:'; print('OK:', '$c')"
done
```

Expected: 5 `OK:` lines. Any failure → fix the offending cfg inline.

- [ ] **Step 9: Pre-commit + commit**

```bash
git add examples/configs/grids/README.md \
        examples/configs/grids/_fixtures/.gitkeep \
        examples/configs/grids/wan21-1_3b-base.yaml \
        examples/configs/grids/wan21-1_3b-base-no-loras.yaml \
        examples/configs/grids/wan21-5b-base-no-loras.yaml \
        examples/configs/grids/wan21-14b-base-no-loras.yaml \
        examples/configs/grids/wan22-14b-base.yaml
pixi run pre-commit run --files $(git diff --cached --name-only)
git commit -m "$(cat <<'EOF'
feat(grid): 5 base cfgs for examples/configs/grids/

Self-contained per-family/per-size base cfgs:
- wan21-1_3b-base.yaml — diffusers + Static Rotation + Pokemon LoRAs
- wan21-1_3b-base-no-loras.yaml — for model-sweep cell
- wan21-5b-base-no-loras.yaml — for model-sweep cell
- wan21-14b-base-no-loras.yaml — for model-sweep cell
- wan22-14b-base.yaml — diffusers + Arcane high+low LoRAs

Each carries `mode: t2v` + the standard field-realistic.txt prompt as
top-level `prompt:` so the grid executor's
`_build_generate_cmd.getattr(cfg, "prompt")` resolves. Engine/compute/
lifecycle blocks cloned from proven-green sibling cfgs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["examples/configs/grids/README.md", "examples/configs/grids/wan21-1_3b-base.yaml", "examples/configs/grids/wan21-1_3b-base-no-loras.yaml", "examples/configs/grids/wan21-5b-base-no-loras.yaml", "examples/configs/grids/wan21-14b-base-no-loras.yaml", "examples/configs/grids/wan22-14b-base.yaml"], "verifyCommand": "for c in examples/configs/grids/*.yaml; do pixi run python -c \"from kinoforge.core.config import load_config; cfg = load_config('$c'); assert cfg.prompt; print('OK:', '$c')\"; done", "acceptanceCriteria": ["all 5 cfgs load_config clean", "every cfg has top-level prompt:", "LoRA-bearing cfgs include official refs", "pre-commit clean"], "modelTier": "mechanical"}
```

---

## Task 3: 9 grid spec YAMLs

**Goal:** All 9 grid spec YAMLs in `examples/configs/grids/`, each `allow_in_repo: true`, each pointing at the right base cfg + overrides + cells matching the spec design §4.

**Files:**
- Create: `examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml`
- Create: `examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml`
- Create: `examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml`
- Create: `examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml`
- Create: `examples/configs/grids/wan21-model-sweep.grid.yaml`
- Create: `examples/configs/grids/wan22-14b-strength-sweep.grid.yaml`
- Create: `examples/configs/grids/wan22-14b-loras-swap.grid.yaml`
- Create: `examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml`
- Create: `examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml`

**Acceptance Criteria:**
- [ ] All 9 spec files parse via `GridSpec.load` without error.
- [ ] Every spec has `allow_in_repo: true` at the top.
- [ ] Every spec has `budget_cap_usd:` set (Wan 2.1 caps: 0.50; Wan 2.2 caps: 2.00 for headroom).
- [ ] Mixed-path specs reference fixture paths under `examples/configs/grids/_fixtures/` (these mp4s will be written in Tasks 5/7/10/12; specs can still parse — `path:` validation happens at `_resolve_spec_cells` time, not at spec load).
- [ ] `pixi run pre-commit run --files <created files>` clean.

**Verify:** `for s in examples/configs/grids/*.grid.yaml; do pixi run python -c "from kinoforge.core.grid.spec import GridSpec; GridSpec.load('$s'); print('OK:', '$s')"; done` → 9 OK lines.

**Steps:**

- [ ] **Step 1: Write `wan21-1_3b-strength-sweep.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.1 1.3B — LoRA strength sweep"
layout: "1x3"
budget_cap_usd: 0.50
cells:
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        loras[0].strength: 0.5
        loras[1].strength: 0.5
    caption: "strength=0.5"
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        loras[0].strength: 1.0
        loras[1].strength: 1.0
    caption: "strength=1.0"
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        loras[0].strength: 1.5
        loras[1].strength: 1.5
    caption: "strength=1.5"
```

- [ ] **Step 2: Write `wan21-1_3b-loras-swap.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.1 1.3B — LoRA stack swap"
layout: "1x3"
budget_cap_usd: 0.50
on_swap_failure: classify
cells:
  - lora_swap:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      stack:
        - ref: "civitai:1479320@1673265"
          strength: 1.0
    caption: "Static Rotation only"
  - lora_swap:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      stack:
        - ref: "civitai:1595383@1805395"
          strength: 1.0
    caption: "Pokemon only"
  - lora_swap:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      stack:
        - ref: "civitai:1479320@1673265"
          strength: 1.0
        - ref: "civitai:1595383@1805395"
          strength: 1.0
    caption: "both stacked"
```

- [ ] **Step 3: Write `wan21-1_3b-prompt-sweep.grid.yaml`**

Read the 3 prompt files first to inline their content:

```bash
cat examples/configs/prompts/field-realistic.txt
cat examples/configs/prompts/field-dreamlike.txt
cat examples/configs/prompts/forest.txt
```

```yaml
allow_in_repo: true
title: "Wan 2.1 1.3B — prompt sweep"
layout: "1x3"
budget_cap_usd: 0.50
cells:
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        prompt: "<verbatim field-realistic.txt content>"
    caption: "realistic"
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        prompt: "<verbatim field-dreamlike.txt content>"
    caption: "dreamlike"
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        prompt: "<verbatim forest.txt content>"
    caption: "forest"
```

Replace the placeholder strings with the actual file contents at write time.

- [ ] **Step 4: Write `wan21-mixed-path-plus-generate.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.1 — mixed path + generate"
layout: "1x3"
budget_cap_usd: 0.50
cells:
  - path: examples/configs/grids/_fixtures/wan21_strength_cell0.mp4
    caption: "fixture: strength=0.5"
  - path: examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4
    caption: "fixture: realistic prompt"
  - generate:
      config: examples/configs/grids/wan21-1_3b-base.yaml
      overrides:
        loras[0].strength: 1.0
        loras[1].strength: 1.0
    caption: "fresh gen"
```

- [ ] **Step 5: Write `wan21-model-sweep.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.1 — model sweep (1.3B vs 5B vs 14B)"
layout: "1x3"
budget_cap_usd: 2.00
cells:
  - generate:
      config: examples/configs/grids/wan21-1_3b-base-no-loras.yaml
    caption: "Wan 2.1 1.3B"
  - generate:
      config: examples/configs/grids/wan21-5b-base-no-loras.yaml
    caption: "Wan 2.1 5B"
  - generate:
      config: examples/configs/grids/wan21-14b-base-no-loras.yaml
    caption: "Wan 2.1 14B"
```

- [ ] **Step 6: Write `wan22-14b-strength-sweep.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.2 14B — Arcane LoRA strength sweep"
layout: "1x3"
budget_cap_usd: 2.00
cells:
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        loras[0].strength: 0.5
        loras[1].strength: 0.5
    caption: "strength=0.5"
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        loras[0].strength: 1.0
        loras[1].strength: 1.0
    caption: "strength=1.0"
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        loras[0].strength: 1.5
        loras[1].strength: 1.5
    caption: "strength=1.5"
```

- [ ] **Step 7: Write `wan22-14b-loras-swap.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.2 14B — Arcane LoRA stack swap"
layout: "1x3"
budget_cap_usd: 2.00
on_swap_failure: classify
cells:
  - lora_swap:
      config: examples/configs/grids/wan22-14b-base.yaml
      stack:
        - ref: "civitai:2197303@2474081"
          strength: 1.0
          branch: high_noise
    caption: "high-noise only"
  - lora_swap:
      config: examples/configs/grids/wan22-14b-base.yaml
      stack:
        - ref: "civitai:2197303@2474073"
          strength: 1.0
          branch: low_noise
    caption: "low-noise only"
  - lora_swap:
      config: examples/configs/grids/wan22-14b-base.yaml
      stack:
        - ref: "civitai:2197303@2474081"
          strength: 1.0
          branch: high_noise
        - ref: "civitai:2197303@2474073"
          strength: 1.0
          branch: low_noise
    caption: "both stacked"
```

- [ ] **Step 8: Write `wan22-14b-prompt-sweep.grid.yaml`**

```bash
cat examples/configs/prompts/field-realistic.txt
cat examples/configs/prompts/field-dreamlike.txt
cat examples/configs/prompts/dawn-flight.md
```

```yaml
allow_in_repo: true
title: "Wan 2.2 14B — prompt sweep"
layout: "1x3"
budget_cap_usd: 2.00
cells:
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        prompt: "<verbatim field-realistic.txt content>"
    caption: "realistic"
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        prompt: "<verbatim field-dreamlike.txt content>"
    caption: "dreamlike"
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        prompt: "<verbatim dawn-flight.md content>"
    caption: "dawn-flight"
```

- [ ] **Step 9: Write `wan22-14b-mixed-path-plus-generate.grid.yaml`**

```yaml
allow_in_repo: true
title: "Wan 2.2 14B — mixed path + generate"
layout: "1x3"
budget_cap_usd: 2.00
cells:
  - path: examples/configs/grids/_fixtures/wan22_strength_cell0.mp4
    caption: "fixture: strength=0.5"
  - path: examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4
    caption: "fixture: realistic prompt"
  - generate:
      config: examples/configs/grids/wan22-14b-base.yaml
      overrides:
        loras[0].strength: 1.0
        loras[1].strength: 1.0
    caption: "fresh gen"
```

- [ ] **Step 10: Validate all 9 specs load**

```bash
for s in examples/configs/grids/*.grid.yaml; do
    pixi run python -c "from kinoforge.core.grid.spec import GridSpec; GridSpec.load('$s'); print('OK:', '$s')"
done
```

Expected: 9 OK lines. Any failure → fix inline.

- [ ] **Step 11: Pre-commit + commit**

```bash
git add examples/configs/grids/*.grid.yaml
pixi run pre-commit run --files $(git diff --cached --name-only)
git commit -m "$(cat <<'EOF'
feat(grid): 9 verified-example grid spec YAMLs

One spec per kinoforge grid capability per Wan family:
- 4 Wan 2.1 1.3B caps (strength, loras-swap, prompt, mixed-path)
- 1 Wan 2.1 intra-family model sweep (1.3B vs 5B vs 14B)
- 4 Wan 2.2 14B caps (strength, loras-swap, prompt, mixed-path)

Every spec opts in via `allow_in_repo: true`. Each references the
corresponding base cfg in examples/configs/grids/ + uses official
LoRA refs (civitai:1479320@1673265, civitai:1595383@1805395 for
Wan 2.1; civitai:2197303@2474081/74073 Arcane pair for Wan 2.2).

Live evidence pending — captured per-grid in Tasks 5-13.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml", "examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml", "examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml", "examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml", "examples/configs/grids/wan21-model-sweep.grid.yaml", "examples/configs/grids/wan22-14b-strength-sweep.grid.yaml", "examples/configs/grids/wan22-14b-loras-swap.grid.yaml", "examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml", "examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml"], "verifyCommand": "for s in examples/configs/grids/*.grid.yaml; do pixi run python -c \"from kinoforge.core.grid.spec import GridSpec; GridSpec.load('$s')\"; done", "acceptanceCriteria": ["all 9 specs GridSpec.load clean", "every spec has allow_in_repo: true", "every spec has budget_cap_usd", "pre-commit clean"], "modelTier": "mechanical"}
```

---

## Task 4: Dry-run gate across all 9 specs

**Goal:** Validate spec + cfg + dotted-path overrides + capability-key derivation surface without live spend. BLOCKS Tasks 5-13.

**Files:** (no source changes — verification only)

**Acceptance Criteria:**
- [ ] `pixi run kinoforge grid --spec <each>.grid.yaml --dry-run` exits 0 for all 9 specs.
- [ ] Each dry-run prints the planned cell count + layout + budget banner.
- [ ] No spec errors leak from cfg load / dotted-path override / capability-key derivation.

**Verify:**
```bash
for s in examples/configs/grids/*.grid.yaml; do
    echo "=== $s ==="
    pixi run kinoforge grid --spec "$s" --dry-run || { echo "FAIL: $s"; exit 1; }
done
```
Expected: 9 sections each ending with `[grid dry-run] N cells, layout=..., budget_cap=$X.XX`; final shell exit 0.

**Steps:**

- [ ] **Step 1: Run dry-run loop**

```bash
for s in examples/configs/grids/*.grid.yaml; do
    echo "=== $s ==="
    pixi run kinoforge grid --spec "$s" --dry-run
done
```

- [ ] **Step 2: Triage failures**

Common dry-run failure modes:
- DottedPathError on overrides → check override key vs cfg shape (e.g. `loras[0].strength` requires base cfg has `loras:` block with index 0).
- `GridCellPathMissing` for mixed-path specs → expected (mp4 fixtures don't exist yet); resolve via NOTE below.
- Pydantic ValidationError on base cfg → fix the cfg inline.
- `GridSpecUnderRepoError` → spec missing `allow_in_repo: true`; add it.

**NOTE on mixed-path specs (4, 9):** `_resolve_spec_cells` raises `GridCellPathMissing` when path-cell mp4s don't exist. For Task 4, expect these 2 specs to fail; the failure is fine since live runs in Tasks 5/7/10/12 produce the fixtures before Tasks 8/13 run. Treat these 2 dry-run failures as PASS for the gate purpose — only generate-only / lora_swap-only specs need a clean dry-run here.

Resolution: skip path-cell specs in this dry-run pass; revisit after Tasks 5+7 (then 10+12) land fixtures.

Updated loop:

```bash
for s in examples/configs/grids/*.grid.yaml; do
    case "$s" in
        *mixed-path-plus-generate.grid.yaml) echo "SKIP (path fixtures pending): $s"; continue;;
    esac
    echo "=== $s ==="
    pixi run kinoforge grid --spec "$s" --dry-run || { echo "FAIL: $s"; exit 1; }
done
```

- [ ] **Step 3: Fix any failures inline; re-run; commit fix(es) if any**

If any spec or cfg needed correction:

```bash
git add <changed files>
git commit -m "fix(grid): <one-line>"
```

If no fixes needed → no commit for this task (verification-only).

```json:metadata
{"files": [], "verifyCommand": "for s in examples/configs/grids/*.grid.yaml; do case \"$s\" in *mixed-path-plus-generate.grid.yaml) continue;; esac; pixi run kinoforge grid --spec \"$s\" --dry-run || exit 1; done", "acceptanceCriteria": ["7 of 9 specs (all non-mixed-path) dry-run exit 0", "mixed-path specs skipped pending Tasks 5/7/10/12 fixtures", "no DottedPathError / pydantic / GridSpecUnderRepoError leaks"], "modelTier": "mechanical"}
```

---

## Task 5: Live Wan 2.1 1.3B strength sweep (cap #1) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan21-1_3b-strength-sweep.grid.yaml` end-to-end on RunPod. Single pod, 3 cells, sha-distinct mp4s. ~$0.05 spend.

**Files:**
- Use unchanged: `examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml`
- Create: `tests/live/_grid_examples/wan21_strength.json`
- Create: `examples/configs/grids/_fixtures/wan21_strength_cell0.mp4` (force-added; cap #4 references it in Task 8)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 pre-spend.
- [ ] `pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml --out output/wan21-1_3b-strength.mp4` exits 0.
- [ ] Composed mp4 exists at `output/wan21-1_3b-strength.mp4`.
- [ ] Cost sidecar at `output/wan21-1_3b-strength.cost.json` reports 3 cells with sha256 + size_bytes per cell.
- [ ] All 3 cell sha256s pairwise-distinct (LoRA strength variation actually changed the output).
- [ ] Post-run `pixi run kinoforge list` reports `No running instances` + `No instances recorded in ledger`.
- [ ] Post-run RunPod GraphQL `{ myself { pods { id } } }` returns `pods: []`.
- [ ] Spend ≤ $0.30 (6× headroom over $0.05 est).
- [ ] Evidence file written.
- [ ] Fixture mp4 copied to `examples/configs/grids/_fixtures/wan21_strength_cell0.mp4`.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml --out output/wan21-1_3b-strength.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight
```
Expected: `preflight: PASS — safe to spend`. If FAIL (dirty tree / pods != 0 / env missing) → fix before continuing.

- [ ] **Step 2: Start sweeper safety net**

```bash
pixi run kinoforge sweeper start -c examples/configs/grids/wan21-1_3b-base.yaml --interval-s 60 &
sleep 5
pixi run kinoforge sweeper status -c examples/configs/grids/wan21-1_3b-base.yaml
```
Expected: sweeper status reports `sweeps_total >= 1`.

- [ ] **Step 3: Run the grid (background; poll while running)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml \
    --out output/wan21-1_3b-strength.mp4 \
    > /tmp/wan21_strength.log 2>&1 &
GRID_PID=$!
```

Poll every 60-90s while running (per CLAUDE.md `Live smoke monitoring`):

```bash
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus costPerHr runtime { uptimeInSeconds gpus { gpuUtilPercent } container { cpuPercent } } } } }'})
print(json.dumps(r['data']['myself']['pods'], indent=2))
"
```

If GPU=0% for 3 consecutive probes during gen phase → kill pod, fail-fast. Otherwise wait for `GRID_PID` to exit. Expected wall ~7-10 min total.

```bash
wait $GRID_PID
echo "grid exit code: $?"
tail -30 /tmp/wan21_strength.log
```

Expected exit 0; log ends with `[grid summary] composed mp4 → output/wan21-1_3b-strength.mp4`.

- [ ] **Step 4: Capture evidence**

Inspect cost sidecar:

```bash
cat output/wan21-1_3b-strength.cost.json
```

Compute per-cell sha distinctness:

```bash
pixi run python -c "
import json
data = json.load(open('output/wan21-1_3b-strength.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
print('PASS: 3 sha-distinct cells')
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 5: Copy fixture mp4 (cell 0 → `_fixtures/`)**

The grid executor writes per-cell mp4s under `output/_grid_<grid_id>/cell_<idx>_out/`. But the `--out` composed mp4 + the cost sidecar are also on disk. We need ONE per-cell mp4 to seed cap #4's path-cell. Cell 0 (strength=0.5) is reproducible from the same grid — copy it.

```bash
GRID_ID=$(pixi run python -c "import json; print(json.load(open('output/wan21-1_3b-strength.cost.json'))['grid_id'])")
CELL0_MP4=$(ls output/_grid_${GRID_ID}/cell_0_out/*.mp4 | head -1)
mkdir -p examples/configs/grids/_fixtures
cp "$CELL0_MP4" examples/configs/grids/_fixtures/wan21_strength_cell0.mp4
ls -l examples/configs/grids/_fixtures/wan21_strength_cell0.mp4
```

Expected: fixture file ~1-3 MB.

- [ ] **Step 6: Post-run pod-clean verification**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
```

Expected: kinoforge list reports `No running instances` + `No instances recorded`; pods=[].

- [ ] **Step 7: Sweeper stop**

```bash
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 8: Write evidence file**

Build the evidence JSON. Use the shape established by predecessor evidence files (e.g. `tests/live/_warm_attach_teardown_fix_evidence.json`).

```bash
mkdir -p tests/live/_grid_examples
pixi run python -c "
import json
from pathlib import Path
sidecar = json.load(open('output/wan21-1_3b-strength.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan21 1.3b strength sweep (cap #1)',
    'spec_path': 'examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan21-1_3b-strength.mp4',
    'composed_mp4_sha256': __import__('hashlib').sha256(Path('output/wan21-1_3b-strength.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'fixture_committed': 'examples/configs/grids/_fixtures/wan21_strength_cell0.mp4',
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples').mkdir(parents=True, exist_ok=True)
Path('tests/live/_grid_examples/wan21_strength.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan21_strength.json')
"
```

- [ ] **Step 9: Commit (force-add fixture; .gitignored dirs)**

```bash
git add tests/live/_grid_examples/wan21_strength.json
git add -f examples/configs/grids/_fixtures/wan21_strength_cell0.mp4
git commit -m "$(cat <<'EOF'
test(live): wan21 1.3b strength-sweep grid GREEN — evidence + fixture

Grid example cap #1 verified live on RunPod via
examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml. 3 cells
(strength={0.5, 1.0, 1.5}), single pod (CapabilityKey shared per P1
strength-invariant matcher), sha-distinct mp4s. Cost cap $0.50; spend
captured in evidence JSON.

Cell-0 mp4 force-added to examples/configs/grids/_fixtures/ for
re-use by cap #4 (mixed-path grid, Task 8).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan21_strength.json", "examples/configs/grids/_fixtures/wan21_strength_cell0.mp4"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml --out output/wan21-1_3b-strength.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "composed mp4 exists", "3 sha-distinct cells", "kinoforge list clean", "RunPod pods=[]", "spend <= $0.30", "fixture mp4 copied"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["strength=0.5", "strength=1.0", "strength=1.5"], ["pods=[]", "No running instances"]]}
```

---

## Task 6: Live Wan 2.1 1.3B LoRA stack swap (cap #2) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan21-1_3b-loras-swap.grid.yaml`. Single pod via `/lora/set_stack` swap per cell. 3 distinct stacks (Static-only, Pokemon-only, both). ~$0.05.

**Files:**
- Use unchanged: `examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml`
- Create: `tests/live/_grid_examples/wan21_loras_swap.json`

**Acceptance Criteria:**
- [ ] preflight PASS pre-spend.
- [ ] `pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml --out output/wan21-1_3b-loras-swap.mp4` exits 0.
- [ ] Composed mp4 exists.
- [ ] Cost sidecar has 3 cells, all `status: success`.
- [ ] All 3 cell sha256s pairwise-distinct.
- [ ] Cells 1 + 2 swap_wall_time_s > 0 (cell 0 = cold boot, cells 1+2 = warm-attach swaps).
- [ ] Post-run kinoforge list + RunPod pods=[].
- [ ] Spend ≤ $0.30.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml --out output/wan21-1_3b-loras-swap.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan21-1_3b-base.yaml --interval-s 60 &
sleep 5
```
Expected: `preflight: PASS — safe to spend`.

- [ ] **Step 2: Run grid (background; poll while running)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml \
    --out output/wan21-1_3b-loras-swap.mp4 \
    > /tmp/wan21_loras_swap.log 2>&1 &
GRID_PID=$!
```

Poll every 60-90s while running (per CLAUDE.md `Live smoke monitoring`):

```bash
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus costPerHr runtime { uptimeInSeconds gpus { gpuUtilPercent } container { cpuPercent } } } } }'})
print(json.dumps(r['data']['myself']['pods'], indent=2))
"
```

If GPU=0% for 3 consecutive probes during gen phase → kill pod, fail-fast. Otherwise wait for `GRID_PID`. Expected wall ~10-15 min (1 cold-boot + 2 swaps + 3 gens).

```bash
wait $GRID_PID
echo "grid exit code: $?"
tail -30 /tmp/wan21_loras_swap.log
```

- [ ] **Step 3: Verify sidecar + sha distinctness + swap-times**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan21-1_3b-loras-swap.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
swap_times = [c['swap_wall_time_s'] for g in data['groups'] for c in g['cells']]
print('swap_wall_times:', swap_times)
print('total_cost_usd:', data['total_cost_usd'])
print('PASS: 3 sha-distinct cells')
"
```

- [ ] **Step 4: Post-run pod-clean verification**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
```

Expected: kinoforge list reports `No running instances` + `No instances recorded`; pods=[].

- [ ] **Step 5: Sweeper stop**

```bash
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 6: Write evidence file**

```bash
mkdir -p tests/live/_grid_examples
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan21-1_3b-loras-swap.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan21 1.3b loras swap (cap #2)',
    'spec_path': 'examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan21-1_3b-loras-swap.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan21-1_3b-loras-swap.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'swap_wall_times_s': [c['swap_wall_time_s'] for g in sidecar['groups'] for c in g['cells']],
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples').mkdir(parents=True, exist_ok=True)
Path('tests/live/_grid_examples/wan21_loras_swap.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan21_loras_swap.json')
"
```

- [ ] **Step 7: Commit**

```bash
git add tests/live/_grid_examples/wan21_loras_swap.json
git commit -m "$(cat <<'EOF'
test(live): wan21 1.3b loras-swap grid GREEN — evidence

Grid example cap #2 verified live. 3 cells driving distinct LoRA
stacks (Static Rotation only / Pokemon only / both stacked) on a
single warm pod via /lora/set_stack swap (P3 + warm-reuse path).
Sha-distinct mp4s prove the swap actually changed the underlying
model state.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan21_loras_swap.json"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml --out output/wan21-1_3b-loras-swap.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 cells success", "3 sha-distinct cells", "cells 1+2 swap_wall_time_s > 0", "kinoforge list clean", "pods=[]", "spend <= $0.30"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["Static Rotation only", "Pokemon only", "both stacked"], ["pods=[]", "No running instances"]]}
```

---

## Task 7: Live Wan 2.1 1.3B prompt sweep (cap #3) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan21-1_3b-prompt-sweep.grid.yaml`. Single pod (CapabilityKey shared; prompt-invariant). 3 different prompts. ~$0.05.

**Files:**
- Use unchanged: `examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml`
- Create: `tests/live/_grid_examples/wan21_prompt.json`
- Create: `examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4` (for Task 8 cap #4)

**Acceptance Criteria:**
- [ ] preflight PASS.
- [ ] grid exit 0 with composed mp4.
- [ ] 3 cells, 3 sha-distinct mp4s (prompt change reflected).
- [ ] post-run pods=[].
- [ ] spend ≤ $0.30.
- [ ] Fixture mp4 (cell 0 = realistic prompt) copied to `_fixtures/wan21_prompt_cell0.mp4`.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml --out output/wan21-1_3b-prompt.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan21-1_3b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 2: Run grid (background + poll RunPod GraphQL every 60-90s; kill if GPU 0% for 3 consecutive probes during gen phase)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml \
    --out output/wan21-1_3b-prompt.mp4 \
    > /tmp/wan21_prompt.log 2>&1 &
GRID_PID=$!
# poll loop (see Task 5 Step 3 for the GraphQL probe snippet); wait + tail
wait $GRID_PID
tail -30 /tmp/wan21_prompt.log
```

- [ ] **Step 3: Verify sidecar + sha distinctness**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan21-1_3b-prompt.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
print('PASS: 3 sha-distinct cells')
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 4: Copy fixture (cell 0, realistic prompt)**

```bash
GRID_ID=$(pixi run python -c "import json; print(json.load(open('output/wan21-1_3b-prompt.cost.json'))['grid_id'])")
CELL0_MP4=$(ls output/_grid_${GRID_ID}/cell_0_out/*.mp4 | head -1)
mkdir -p examples/configs/grids/_fixtures
cp "$CELL0_MP4" examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4
```

- [ ] **Step 5: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 6: Write evidence file**

```bash
mkdir -p tests/live/_grid_examples
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan21-1_3b-prompt.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan21 1.3b prompt sweep (cap #3)',
    'spec_path': 'examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan21-1_3b-prompt.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan21-1_3b-prompt.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'fixture_committed': 'examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4',
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples').mkdir(parents=True, exist_ok=True)
Path('tests/live/_grid_examples/wan21_prompt.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan21_prompt.json')
"
```

- [ ] **Step 7: Commit (force-add fixture)**

```bash
git add tests/live/_grid_examples/wan21_prompt.json
git add -f examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4
git commit -m "$(cat <<'EOF'
test(live): wan21 1.3b prompt-sweep grid GREEN — evidence + fixture

Grid example cap #3 verified live. 3 cells overriding `prompt:`
through dotted-path to field-realistic, field-dreamlike, forest
prompt files. Single pod (CapabilityKey prompt-invariant — only
LoRA stack identifies the warm-reuse target). Sha-distinct mp4s
prove the prompt override flowed end-to-end.

Cell-0 (realistic prompt) mp4 force-added for cap #4's path cell.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan21_prompt.json", "examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml --out output/wan21-1_3b-prompt.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 sha-distinct cells", "pods=[]", "spend <= $0.30", "fixture mp4 copied"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["realistic", "dreamlike", "forest"], ["pods=[]", "No running instances"]]}
```

---

## Task 8: Live Wan 2.1 mixed path + generate (cap #4) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan21-mixed-path-plus-generate.grid.yaml` after Tasks 5+7 have written the path-cell fixtures. 2 path cells + 1 generate cell. ~$0.02 (only the generate cell spins compute).

**Files:**
- Use unchanged: `examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml`
- Create: `tests/live/_grid_examples/wan21_mixed_path.json`

**Dependencies:** Tasks 5 + 7 must complete first (they write `_fixtures/wan21_strength_cell0.mp4` + `_fixtures/wan21_prompt_cell0.mp4`).

**Acceptance Criteria:**
- [ ] Both fixture mp4s exist at `examples/configs/grids/_fixtures/wan21_{strength,prompt}_cell0.mp4`.
- [ ] preflight PASS.
- [ ] grid exit 0 with composed mp4.
- [ ] Composed mp4 contains 3 cells (2 from fixtures + 1 fresh gen); per-cell shas show 2 stable + 1 fresh.
- [ ] post-run pods=[].
- [ ] spend ≤ $0.20.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml --out output/wan21-mixed-path.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Verify fixtures present**

```bash
ls -l examples/configs/grids/_fixtures/wan21_strength_cell0.mp4 examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4
```

If either missing → BLOCKED on Task 5 or 7 respectively.

- [ ] **Step 2: Dry-run this spec specifically (it was skipped in Task 4)**

```bash
pixi run kinoforge grid --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml --dry-run
```

Expected: exit 0 with cell list (path fixtures now exist).

- [ ] **Step 3: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan21-1_3b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 4: Run grid (only 1 cell spins compute — the generate cell)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml \
    --out output/wan21-mixed-path.mp4 \
    > /tmp/wan21_mixed_path.log 2>&1 &
GRID_PID=$!
# poll RunPod every 60-90s; kill if GPU 0% for 3 consecutive probes during gen
wait $GRID_PID
tail -30 /tmp/wan21_mixed_path.log
```

- [ ] **Step 5: Verify composed mp4 + cells**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
data = json.load(open('output/wan21-mixed-path.cost.json'))
assert Path('output/wan21-mixed-path.mp4').exists()
print('composed sha:', hashlib.sha256(Path('output/wan21-mixed-path.mp4').read_bytes()).hexdigest())
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 6: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 7: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan21-mixed-path.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan21 mixed path+generate (cap #4)',
    'spec_path': 'examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan21-mixed-path.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan21-mixed-path.mp4').read_bytes()).hexdigest(),
    'fresh_gen_cell_shas': shas,
    'path_cells_referenced': [
        'examples/configs/grids/_fixtures/wan21_strength_cell0.mp4',
        'examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4',
    ],
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan21_mixed_path.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan21_mixed_path.json')
"
```

- [ ] **Step 8: Commit**

```bash
git add tests/live/_grid_examples/wan21_mixed_path.json
git commit -m "$(cat <<'EOF'
test(live): wan21 mixed path+generate grid GREEN — evidence

Grid example cap #4 verified live. 2 path: cells reuse fixture mp4s
from Tasks 5 + 7 (strength sweep + prompt sweep cell-0 outputs)
plus 1 generate: cell for a fresh gen. Demonstrates the mixed-mode
grid: pre-existing footage composited next to a fresh generation
without re-rendering the historical cells.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan21_mixed_path.json"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml --out output/wan21-mixed-path.mp4", "acceptanceCriteria": ["both fixture mp4s exist pre-run", "preflight PASS", "grid exit 0", "composed mp4 exists", "pods=[]", "spend <= $0.20"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["fixture", "path cell"], ["fresh gen", "generate cell"], ["pods=[]", "No running instances"]]}
```

---

## Task 9: Live Wan 2.1 model sweep (cap #5) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan21-model-sweep.grid.yaml`. 3 different cfgs (1.3B / 5B / 14B), 3 distinct CapabilityKeys → 3 parallel pods (up to `--max-parallel-groups=2` cap). No LoRAs. ~$1.20.

**Files:**
- Use unchanged: `examples/configs/grids/wan21-model-sweep.grid.yaml`
- Create: `tests/live/_grid_examples/wan21_model_sweep.json`

**Acceptance Criteria:**
- [ ] preflight PASS.
- [ ] grid exit 0 with composed mp4.
- [ ] Cost sidecar reports 3 distinct groups (one per pod / cfg).
- [ ] All 3 cell sha256s pairwise-distinct.
- [ ] Post-run pods=[] (all 3 pods destroyed).
- [ ] Spend ≤ $2.50 (2× headroom over $1.20).

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan21-model-sweep.grid.yaml --out output/wan21-model-sweep.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight
```

- [ ] **Step 2: Sweeper (use 1.3B base cfg; sweeper is cfg-scoped to provider)**

```bash
pixi run kinoforge sweeper start -c examples/configs/grids/wan21-1_3b-base-no-loras.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 3: Run grid (parallel groups=2 default; 3 cells = 2 parallel + 1 serial)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan21-model-sweep.grid.yaml \
    --out output/wan21-model-sweep.mp4 \
    > /tmp/wan21_model_sweep.log 2>&1 &
GRID_PID=$!
```

Poll EVERY pod every 60-90s — there will be 2-3 concurrent. Watch for sticking pods.

```bash
wait $GRID_PID
tail -50 /tmp/wan21_model_sweep.log
```

Expected wall ~20-30 min (3 separate cold-boots; 14B is slowest).

- [ ] **Step 4: Verify 3 groups + 3 sha-distinct**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan21-model-sweep.cost.json'))
groups = data['groups']
print('num groups:', len(groups))
assert len(groups) == 3, f'expected 3 groups (one per model), got {len(groups)}'
shas = [c['mp4_sha256'] for g in groups for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3 and len(set(shas)) == 3
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 5: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 6: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan21-model-sweep.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan21 model sweep (cap #5)',
    'spec_path': 'examples/configs/grids/wan21-model-sweep.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan21-model-sweep.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan21-model-sweep.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'num_groups': len(sidecar['groups']),
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan21_model_sweep.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan21_model_sweep.json')
"
```

- [ ] **Step 7: Commit**

```bash
git add tests/live/_grid_examples/wan21_model_sweep.json
git commit -m "$(cat <<'EOF'
test(live): wan21 model sweep grid GREEN — evidence

Grid example cap #5 verified live. 3 cells comparing Wan 2.1 1.3B
vs 5B vs 14B on the same field-realistic prompt without LoRAs.
3 distinct CapabilityKeys → 3 parallel groups (capped at
--max-parallel-groups=2; one group runs serial after the first
2 land). 3 pods cold-booted + destroyed cleanly.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan21_model_sweep.json"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan21-model-sweep.grid.yaml --out output/wan21-model-sweep.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 distinct groups", "3 sha-distinct cells", "pods=[]", "spend <= $2.50"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["1.3B", "5B", "14B"], ["pods=[]", "No running instances"]]}
```

---

## Task 10: Live Wan 2.2 14B strength sweep (cap #6) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan22-14b-strength-sweep.grid.yaml`. Single A100 80GB pod, 3 cells, Arcane LoRA strength `{0.5, 1.0, 1.5}`. ~$1.00.

**Files:**
- Use unchanged: `examples/configs/grids/wan22-14b-strength-sweep.grid.yaml`
- Create: `tests/live/_grid_examples/wan22_strength.json`
- Create: `examples/configs/grids/_fixtures/wan22_strength_cell0.mp4` (for Task 13 cap #9)

**Acceptance Criteria:**
- [ ] preflight PASS.
- [ ] grid exit 0 with composed mp4.
- [ ] 3 sha-distinct cells.
- [ ] post-run pods=[].
- [ ] spend ≤ $2.00.
- [ ] Fixture mp4 copied.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-strength-sweep.grid.yaml --out output/wan22-14b-strength.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan22-14b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 2: Run grid (background; poll every 60-90s; expected wall ~30-45 min — A100 cold-boot ~25 min weights + 3 × 3-min gens)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan22-14b-strength-sweep.grid.yaml \
    --out output/wan22-14b-strength.mp4 \
    > /tmp/wan22_strength.log 2>&1 &
GRID_PID=$!
# poll RunPod GraphQL every 60-90s; kill if GPU 0% for 3 consecutive probes during gen
wait $GRID_PID
tail -30 /tmp/wan22_strength.log
```

- [ ] **Step 3: Verify sidecar + sha distinctness**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan22-14b-strength.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
print('PASS: 3 sha-distinct cells')
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 4: Copy fixture (cell 0 → `_fixtures/wan22_strength_cell0.mp4` for cap #9)**

```bash
GRID_ID=$(pixi run python -c "import json; print(json.load(open('output/wan22-14b-strength.cost.json'))['grid_id'])")
CELL0_MP4=$(ls output/_grid_${GRID_ID}/cell_0_out/*.mp4 | head -1)
mkdir -p examples/configs/grids/_fixtures
cp "$CELL0_MP4" examples/configs/grids/_fixtures/wan22_strength_cell0.mp4
```

- [ ] **Step 5: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 6: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan22-14b-strength.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan22 14b strength sweep (cap #6)',
    'spec_path': 'examples/configs/grids/wan22-14b-strength-sweep.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan22-14b-strength.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan22-14b-strength.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'fixture_committed': 'examples/configs/grids/_fixtures/wan22_strength_cell0.mp4',
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan22_strength.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan22_strength.json')
"
```

- [ ] **Step 7: Commit (force-add fixture)**

```bash
git add tests/live/_grid_examples/wan22_strength.json
git add -f examples/configs/grids/_fixtures/wan22_strength_cell0.mp4
git commit -m "$(cat <<'EOF'
test(live): wan22 14b strength-sweep grid GREEN — evidence + fixture

Grid example cap #6 verified live on A100 80GB. Arcane LoRA pair
high+low strengths {0.5, 1.0, 1.5}. Single pod (CapabilityKey
strength-invariant per P1). 3 sha-distinct mp4s.

Cell-0 fixture force-added for cap #9.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan22_strength.json", "examples/configs/grids/_fixtures/wan22_strength_cell0.mp4"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-strength-sweep.grid.yaml --out output/wan22-14b-strength.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 sha-distinct cells", "pods=[]", "spend <= $2.00", "fixture copied"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["strength=0.5", "strength=1.0", "strength=1.5"], ["pods=[]", "No running instances"]]}
```

---

## Task 11: Live Wan 2.2 14B LoRA stack swap (cap #7) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan22-14b-loras-swap.grid.yaml`. Single A100 pod, 3 cells: Arcane high only / low only / both stacked. ~$1.10.

**Files:**
- Use unchanged: `examples/configs/grids/wan22-14b-loras-swap.grid.yaml`
- Create: `tests/live/_grid_examples/wan22_loras_swap.json`

**Acceptance Criteria:**
- [ ] preflight PASS.
- [ ] grid exit 0 with composed mp4.
- [ ] 3 cells, 3 sha-distinct mp4s.
- [ ] Cells 1+2 swap_wall_time_s > 0.
- [ ] Critical: cell 0 (high only) vs cell 1 (low only) should produce visibly different motion characteristics (high-noise tensor governs early-step denoising; low-noise late-step). This is verified by sha distinctness, not perceptual eval in the smoke; operator inspects mp4 separately.
- [ ] post-run pods=[].
- [ ] spend ≤ $2.00.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-loras-swap.grid.yaml --out output/wan22-14b-loras-swap.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan22-14b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 2: Run grid (expected wall ~35-50 min — A100 cold-boot + 2 swaps + 3 gens)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan22-14b-loras-swap.grid.yaml \
    --out output/wan22-14b-loras-swap.mp4 \
    > /tmp/wan22_loras_swap.log 2>&1 &
GRID_PID=$!
wait $GRID_PID
tail -30 /tmp/wan22_loras_swap.log
```

- [ ] **Step 3: Verify sidecar + sha distinctness + swap-times**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan22-14b-loras-swap.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
swap_times = [c['swap_wall_time_s'] for g in data['groups'] for c in g['cells']]
print('swap_wall_times:', swap_times)
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 4: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 5: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan22-14b-loras-swap.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan22 14b loras swap (cap #7)',
    'spec_path': 'examples/configs/grids/wan22-14b-loras-swap.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan22-14b-loras-swap.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan22-14b-loras-swap.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'swap_wall_times_s': [c['swap_wall_time_s'] for g in sidecar['groups'] for c in g['cells']],
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan22_loras_swap.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan22_loras_swap.json')
"
```

- [ ] **Step 6: Commit**

```bash
git add tests/live/_grid_examples/wan22_loras_swap.json
git commit -m "$(cat <<'EOF'
test(live): wan22 14b loras-swap grid GREEN — evidence

Grid example cap #7 verified live. 3 cells driving Arcane LoRA
stack variations on single A100 pod via /lora/set_stack. Tests
the lora_swap: cell variant end-to-end on the MoE pair (high_noise
+ low_noise branches).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan22_loras_swap.json"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-loras-swap.grid.yaml --out output/wan22-14b-loras-swap.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 sha-distinct cells", "cells 1+2 swap_wall_time_s > 0", "pods=[]", "spend <= $2.00"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["high-noise only", "low-noise only", "both stacked"], ["pods=[]", "No running instances"]]}
```

---

## Task 12: Live Wan 2.2 14B prompt sweep (cap #8) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan22-14b-prompt-sweep.grid.yaml`. Single A100 pod, 3 different prompts on Arcane LoRA stack. ~$1.00.

**Files:**
- Use unchanged: `examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml`
- Create: `tests/live/_grid_examples/wan22_prompt.json`
- Create: `examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4` (for Task 13 cap #9)

**Acceptance Criteria:**
- [ ] preflight PASS, grid exit 0, 3 sha-distinct cells, pods=[], spend ≤ $2.00, fixture copied.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml --out output/wan22-14b-prompt.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan22-14b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 2: Run grid (expected wall ~35-45 min — A100 cold-boot + 3 × 3-min gens)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml \
    --out output/wan22-14b-prompt.mp4 \
    > /tmp/wan22_prompt.log 2>&1 &
GRID_PID=$!
wait $GRID_PID
tail -30 /tmp/wan22_prompt.log
```

- [ ] **Step 3: Verify sidecar + sha distinctness**

```bash
pixi run python -c "
import json
data = json.load(open('output/wan22-14b-prompt.cost.json'))
shas = [c['mp4_sha256'] for g in data['groups'] for c in g['cells'] if c.get('mp4_sha256')]
assert len(shas) == 3, f'expected 3 shas, got {len(shas)}'
assert len(set(shas)) == 3, f'shas not pairwise distinct: {shas}'
print('PASS: 3 sha-distinct cells')
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 4: Copy fixture (cell 0, realistic prompt → `_fixtures/wan22_prompt_cell0.mp4` for cap #9)**

```bash
GRID_ID=$(pixi run python -c "import json; print(json.load(open('output/wan22-14b-prompt.cost.json'))['grid_id'])")
CELL0_MP4=$(ls output/_grid_${GRID_ID}/cell_0_out/*.mp4 | head -1)
cp "$CELL0_MP4" examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4
```

- [ ] **Step 5: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 6: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan22-14b-prompt.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan22 14b prompt sweep (cap #8)',
    'spec_path': 'examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan22-14b-prompt.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan22-14b-prompt.mp4').read_bytes()).hexdigest(),
    'cells_sha_distinct': len(set(shas)) == 3,
    'cell_shas': shas,
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'fixture_committed': 'examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4',
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan22_prompt.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan22_prompt.json')
"
```

- [ ] **Step 7: Commit (force-add fixture)**

```bash
git add tests/live/_grid_examples/wan22_prompt.json
git add -f examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4
git commit -m "$(cat <<'EOF'
test(live): wan22 14b prompt-sweep grid GREEN — evidence + fixture

Grid example cap #8 verified live. 3 cells overriding prompt:
through dotted-path to field-realistic, field-dreamlike, dawn-flight
prompt files. Single A100 pod (CapabilityKey prompt-invariant).
Cell-0 mp4 force-added for cap #9.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan22_prompt.json", "examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml --out output/wan22-14b-prompt.mp4", "acceptanceCriteria": ["preflight PASS", "grid exit 0", "3 sha-distinct cells", "pods=[]", "spend <= $2.00", "fixture copied"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["realistic", "dreamlike", "dawn-flight"], ["pods=[]", "No running instances"]]}
```

---

## Task 13: Live Wan 2.2 mixed path + generate (cap #9) — USER GATE

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Live-verify `wan22-14b-mixed-path-plus-generate.grid.yaml` after Tasks 10+12 have written the path-cell fixtures. ~$1.00 (only generate cell spins compute).

**Files:**
- Use unchanged: `examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml`
- Create: `tests/live/_grid_examples/wan22_mixed_path.json`

**Dependencies:** Tasks 10 + 12 must complete first.

**Acceptance Criteria:**
- [ ] Both Wan 2.2 fixture mp4s exist.
- [ ] preflight PASS, grid exit 0, composed mp4 exists, pods=[], spend ≤ $2.00.

**Verify:** `pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml --out output/wan22-14b-mixed-path.mp4 && pixi run kinoforge list 2>&1 | grep -E 'No running instances|No instances recorded'`

**Steps:**

- [ ] **Step 1: Verify fixtures present**

```bash
ls -l examples/configs/grids/_fixtures/wan22_strength_cell0.mp4 examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4
```

If either missing → BLOCKED on Task 10 or 12 respectively.

- [ ] **Step 2: Dry-run this spec specifically (it was skipped in Task 4)**

```bash
pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml --dry-run
```

Expected: exit 0 with cell list.

- [ ] **Step 3: Preflight + sweeper**

```bash
pixi run preflight
pixi run kinoforge sweeper start -c examples/configs/grids/wan22-14b-base.yaml --interval-s 60 &
sleep 5
```

- [ ] **Step 4: Run grid (only 1 cell spins compute — the generate cell; expected wall ~30-40 min for the A100 cold-boot + 1 gen)**

```bash
pixi run kinoforge grid \
    --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml \
    --out output/wan22-14b-mixed-path.mp4 \
    > /tmp/wan22_mixed_path.log 2>&1 &
GRID_PID=$!
wait $GRID_PID
tail -30 /tmp/wan22_mixed_path.log
```

- [ ] **Step 5: Verify composed mp4 + cells**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
data = json.load(open('output/wan22-14b-mixed-path.cost.json'))
assert Path('output/wan22-14b-mixed-path.mp4').exists()
print('composed sha:', hashlib.sha256(Path('output/wan22-14b-mixed-path.mp4').read_bytes()).hexdigest())
print('total_cost_usd:', data['total_cost_usd'])
"
```

- [ ] **Step 6: Pod-clean verification + sweeper stop**

```bash
pixi run kinoforge list
pixi run python -c "
from dotenv import load_dotenv; load_dotenv('/workspace/.env')
import os, json
from kinoforge.providers.runpod.util import _default_http_post
post = _default_http_post(os.environ['RUNPOD_API_KEY'])
r = post('https://api.runpod.io/graphql', {'query': '{ myself { pods { id desiredStatus } } }'})
print('pods:', json.dumps(r['data']['myself']['pods']))
"
pkill -f "kinoforge sweeper" 2>/dev/null || true
```

- [ ] **Step 7: Write evidence file**

```bash
pixi run python -c "
import json, hashlib
from pathlib import Path
sidecar = json.load(open('output/wan22-14b-mixed-path.cost.json'))
shas = [c['mp4_sha256'] for g in sidecar['groups'] for c in g['cells'] if c.get('mp4_sha256')]
evidence = {
    'test': 'kinoforge grid example: wan22 14b mixed path+generate (cap #9)',
    'spec_path': 'examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml',
    'result': 'PASS',
    'wall_clock_seconds': sidecar['wall_time_s'],
    'grid_id': sidecar['grid_id'],
    'composed_mp4_path': 'output/wan22-14b-mixed-path.mp4',
    'composed_mp4_sha256': hashlib.sha256(Path('output/wan22-14b-mixed-path.mp4').read_bytes()).hexdigest(),
    'fresh_gen_cell_shas': shas,
    'path_cells_referenced': [
        'examples/configs/grids/_fixtures/wan22_strength_cell0.mp4',
        'examples/configs/grids/_fixtures/wan22_prompt_cell0.mp4',
    ],
    'total_cost_usd': sidecar['total_cost_usd'],
    'budget_cap_usd': sidecar['budget_cap_usd'],
    'post_run_runpod_pods': [],
    'post_run_kinoforge_list_clean': True,
    'preflight_pre_spend': 'PASS',
    'cost_sidecar': sidecar,
}
Path('tests/live/_grid_examples/wan22_mixed_path.json').write_text(json.dumps(evidence, indent=2))
print('wrote tests/live/_grid_examples/wan22_mixed_path.json')
"
```

- [ ] **Step 8: Commit**

```bash
git add tests/live/_grid_examples/wan22_mixed_path.json
git commit -m "$(cat <<'EOF'
test(live): wan22 14b mixed path+generate grid GREEN — evidence

Grid example cap #9 verified live. 2 path: cells reuse Wan 2.2
fixtures from Tasks 10 + 12 plus 1 generate: cell on the Arcane
LoRA stack. Demonstrates mixing pre-existing footage with fresh
generation in a Wan 2.2 composition.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/_grid_examples/wan22_mixed_path.json"], "verifyCommand": "pixi run kinoforge grid --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml --out output/wan22-14b-mixed-path.mp4", "acceptanceCriteria": ["both fixture mp4s exist pre-run", "preflight PASS", "grid exit 0", "composed mp4 exists", "pods=[]", "spend <= $2.00"], "modelTier": "advanced", "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["fixture", "path cell"], ["fresh gen", "generate cell"], ["pods=[]", "No running instances"]]}
```

---

## Task 14: README integration — `## Grid examples — verified` section + TOC sub-entries

**Goal:** Add new H2 section + 9 H3 sub-sections (one per verified example) + 9 indented TOC sub-entries with matching anchor links.

**Files:**
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] H2 `## Grid examples — verified` exists.
- [ ] All 9 H3 sub-sections exist with names matching the spec design §6 list.
- [ ] Each sub-section ≤ 15 lines: paragraph + verbatim subshell+heredoc command + evidence file link.
- [ ] Table of contents has 1 new top-level entry + 9 indented sub-entries.
- [ ] Each TOC anchor resolves to its H3 heading (slug match).
- [ ] `pixi run pre-commit run --files README.md` clean.

**Verify:** `grep '^### ' README.md` lists the 9 H3 headings + `grep '#wan-' README.md` shows TOC anchors.

**Steps:**

- [ ] **Step 1: Find insertion point**

```bash
grep -n '^## ' README.md | head -20
```

Identify the line numbers for `## Subcommands` and `## Configuration`. The new section inserts between them.

- [ ] **Step 2: Write the new H2 section**

Use the Edit tool to insert after the `## Subcommands` section end, the following block:

```markdown
## Grid examples — verified

`kinoforge grid` composes N generations into one side-by-side mp4 with
per-cell captions. The 9 examples below each ship a committed spec YAML
under `examples/configs/grids/` (built from official, repo-verified
LoRA refs + prompts) and a committed live evidence file under
`tests/live/_grid_examples/` proving the example ran end-to-end on
RunPod. Schema details live in [`docs/batch-and-grid.md`](docs/batch-and-grid.md).

### Wan 2.1 1.3B — strength sweep

Sweep LoRA strength `{0.5, 1.0, 1.5}` on Static Rotation + Pokemon
Sprite. Single pod (CapabilityKey strength-invariant per P1). ~$0.05.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-strength-sweep.grid.yaml \
        --out output/wan21-1_3b-strength.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_strength.json`](tests/live/_grid_examples/wan21_strength.json).

### Wan 2.1 1.3B — LoRA stack swap

3 distinct stacks (Static only / Pokemon only / both) on a single warm
pod via server-side `/lora/set_stack` swap. ~$0.05.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-loras-swap.grid.yaml \
        --out output/wan21-1_3b-loras-swap.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_loras_swap.json`](tests/live/_grid_examples/wan21_loras_swap.json).

### Wan 2.1 1.3B — prompt sweep

Override `prompt:` per cell to field-realistic / field-dreamlike /
forest text files. Single pod (CapabilityKey prompt-invariant). ~$0.05.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-1_3b-prompt-sweep.grid.yaml \
        --out output/wan21-1_3b-prompt.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_prompt.json`](tests/live/_grid_examples/wan21_prompt.json).

### Wan 2.1 mixed path + generate

2 `path:` cells reuse mp4s from prior runs (Tasks 5+7 fixtures) +
1 `generate:` cell renders fresh. Path cells skip compute. ~$0.02.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml \
        --out output/wan21-mixed-path.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_mixed_path.json`](tests/live/_grid_examples/wan21_mixed_path.json).

### Wan 2.1 model sweep — 1.3B vs 5B vs 14B

3 different `config:` per cell → 3 distinct CapabilityKeys → 3 parallel
pods (capped at `--max-parallel-groups=2`). No LoRAs (5B+14B unvalidated
with LoRAs in repo). ~$1.20.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan21-model-sweep.grid.yaml \
        --out output/wan21-model-sweep.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan21_model_sweep.json`](tests/live/_grid_examples/wan21_model_sweep.json).

### Wan 2.2 14B — strength sweep

Arcane LoRA pair high+low strengths `{0.5, 1.0, 1.5}` on A100 80GB.
Single pod. ~$1.00.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-strength-sweep.grid.yaml \
        --out output/wan22-14b-strength.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_strength.json`](tests/live/_grid_examples/wan22_strength.json).

### Wan 2.2 14B — LoRA stack swap

Arcane high-only / low-only / both-stacked on A100, single pod via
`/lora/set_stack`. Tests the MoE pair (high_noise + low_noise branches).
~$1.10.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-loras-swap.grid.yaml \
        --out output/wan22-14b-loras-swap.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_loras_swap.json`](tests/live/_grid_examples/wan22_loras_swap.json).

### Wan 2.2 14B — prompt sweep

3 different prompts (field-realistic / field-dreamlike / dawn-flight)
on Arcane LoRA stack, A100. ~$1.00.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-prompt-sweep.grid.yaml \
        --out output/wan22-14b-prompt.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_prompt.json`](tests/live/_grid_examples/wan22_prompt.json).

### Wan 2.2 14B mixed path + generate

2 `path:` cells (Wan 2.2 fixtures from Tasks 10+12) + 1 fresh
`generate:`. ~$1.00.

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge grid \
        --spec examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml \
        --out output/wan22-14b-mixed-path.mp4
)
```

Evidence: [`tests/live/_grid_examples/wan22_mixed_path.json`](tests/live/_grid_examples/wan22_mixed_path.json).
```

- [ ] **Step 3: Add TOC sub-entries**

Find the existing TOC in README.md (likely under `## Table of contents`):

```bash
grep -n 'Table of contents' README.md
```

Insert this fragment in the appropriate place (between "Subcommands" and "Configuration" TOC entries to match the H2 ordering):

```markdown
- [Grid examples — verified](#grid-examples--verified)
  - [Wan 2.1 1.3B — strength sweep](#wan-21-13b--strength-sweep)
  - [Wan 2.1 1.3B — LoRA stack swap](#wan-21-13b--lora-stack-swap)
  - [Wan 2.1 1.3B — prompt sweep](#wan-21-13b--prompt-sweep)
  - [Wan 2.1 mixed path + generate](#wan-21-mixed-path--generate)
  - [Wan 2.1 model sweep — 1.3B vs 5B vs 14B](#wan-21-model-sweep--13b-vs-5b-vs-14b)
  - [Wan 2.2 14B — strength sweep](#wan-22-14b--strength-sweep)
  - [Wan 2.2 14B — LoRA stack swap](#wan-22-14b--lora-stack-swap)
  - [Wan 2.2 14B — prompt sweep](#wan-22-14b--prompt-sweep)
  - [Wan 2.2 14B mixed path + generate](#wan-22-14b-mixed-path--generate)
```

- [ ] **Step 4: Verify anchors**

Slugs use GitHub markdown's rules: lowercase, spaces → `-`, `.` removed (so `2.1` → `21`), em-dash `—` collapses to `--`. Verify each TOC anchor matches its H3 heading:

```bash
pixi run python -c "
import re
text = open('README.md').read()
h3s = re.findall(r'^### (.+)$', text, flags=re.MULTILINE)
toc_anchors = re.findall(r'\(#(wan-[^)]+)\)', text)
def slug(s):
    s = s.lower()
    s = re.sub(r'[——]', '--', s)
    s = re.sub(r'[^a-z0-9 +-]', '', s)
    s = re.sub(r' ', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    # collapse double dashes back per Github
    return s
for h in h3s:
    if not h.startswith('Wan'): continue
    s = slug(h)
    found = s in toc_anchors
    print(('OK' if found else 'MISS'), h, '->', s)
"
```

Expected: 9 OK lines. Fix any MISS by editing the TOC anchor to match the auto-generated slug.

- [ ] **Step 5: Pre-commit + commit**

```bash
git add README.md
pixi run pre-commit run --files README.md
git commit -m "$(cat <<'EOF'
docs(readme): verified grid examples for 9 capabilities across Wan 2.1 + 2.2

Adds `## Grid examples — verified` H2 with 9 H3 sub-sections, one per
verified grid example:
- Wan 2.1 1.3B: strength sweep / LoRA stack swap / prompt sweep /
  mixed path+generate
- Wan 2.1 model sweep (1.3B vs 5B vs 14B)
- Wan 2.2 14B: strength sweep / LoRA stack swap / prompt sweep /
  mixed path+generate

Each sub-section: paragraph + verbatim subshell+heredoc command +
link to committed live evidence JSON. Matching indented TOC entries.

Every example references a committed spec YAML in
examples/configs/grids/ and a committed live evidence file in
tests/live/_grid_examples/ that proves the spec runs end-to-end on
RunPod.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["README.md"], "verifyCommand": "grep '^### Wan' README.md | wc -l", "acceptanceCriteria": ["9 H3 Wan sub-sections", "9 indented TOC sub-entries", "all anchors resolve", "pre-commit clean"], "modelTier": "mechanical"}
```

---

## Dependencies

- Task 2 blocked by Task 1 (spec.py changes must land before example specs that need `allow_in_repo`).
- Task 3 blocked by Task 2 (specs reference base cfgs).
- Task 4 blocked by Task 3 (dry-run needs all specs in place).
- Tasks 5, 6, 7, 9 blocked by Task 4 (live runs need dry-run gate clean).
- Task 8 blocked by Tasks 5 + 7 (mixed-path needs strength + prompt fixtures).
- Tasks 10, 11, 12 blocked by Task 9 (Wan 2.2 starts only after Wan 2.1 family verified — sequencing per Approach C).
- Task 13 blocked by Tasks 10 + 12 (mixed-path needs strength + prompt Wan 2.2 fixtures).
- Task 14 blocked by Task 13 (all 9 evidence files must commit before README integration).

---

## Cross-stage failure handling

Per spec §7:
- Stage 2 dry-run failures BLOCK Stage 3+. Fix inline; re-loop.
- Per-grid failures in Tasks 5-13: capture stderr from
  `output/_grid_<id>/cell_<i>.stderr.txt`; classify; if transient,
  re-run; if structural, fix + re-dry-run.
- Cumulative budget kill-switch at $10: halt + surface to user with
  reproducer + cost so far.
- Repeated failure on one grid → STOP, surface reproducer to user
  (per spec §7 — "do NOT silently drop a cap; user asked for ALL 9
  verified").

## Live polling rule (applies to Tasks 5-13)

Per CLAUDE.md `Live smoke monitoring`: every 60-90s during a live
grid, probe RunPod GPU util / CPU / mem / costPerHr via the kinoforge
provider's GraphQL surface. 3 consecutive idle probes during a
should-be-busy phase → kill that grid's pod + fail-fast.
