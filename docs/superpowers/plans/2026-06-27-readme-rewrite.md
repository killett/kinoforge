# README rewrite — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stale 2032-line `README.md` with a focused ~400-line entry-point and relocate every existing deep-dive section into `docs/<topic>.md` files, organised by operator concept.

**Architecture:** Two-tier doc surface. README is the entry-point (Quickstart → Install → Command cheatsheet → Configuration overview → Credentials → operator-concept index → Troubleshooting). Deep-dive content lives one file per operator concept under `docs/`, each absorbing the verbatim content from the current README with a `(Moved from README §<name> on 2026-06-27.)` header so stale links degrade into a clear pointer.

**Tech Stack:** Markdown, pre-commit hooks already configured in repo (`ruff`, `ruff-format`, `mypy`, `check-merge-conflict`, `check-added-large-files`, `check-toml`, `trailing-whitespace`, `end-of-file-fixer`).

**User decisions (already made):**
- Slim README + move deep dives to `docs/<topic>.md` (not a single big reference doc).
- Quickstart is a tiered ladder: local-fake → fal.ai → RunPod.
- `docs/` split is by operator concept (not by Layer name).
- No deep-dive content is dropped; every current `##` / `###` block has a documented destination.

**Source-of-truth pointers used by multiple tasks:**
- Current `README.md` at commit `5838dca` (HEAD of `main` at planning time, 2032 lines).
- CLI subcommand list: `src/kinoforge/cli/_main.py` (`add_parser(...)` calls).
- Pixi task list: `pixi.toml` `[tasks]` block.
- Spec: `docs/superpowers/specs/2026-06-27-readme-rewrite-design.md`.

**Section-to-file mapping (mechanical; do NOT alter without re-validating the spec):**

| Source-README line range | Source heading | Destination |
|---|---|---|
| L1–4 | `# kinoforge` intro paragraph | New `README.md` (rewritten) |
| L5–30 | `## Quickstart` (local-fake only) | DROPPED — replaced by new tiered Quickstart in new README |
| L31–108 | `## Operator commands` + `### kinoforge status` + `### Heartbeat persistence (Layer U)` | `docs/lifecycle.md` |
| L109–156 | `## Operator warm-reuse (B4)` + `### Discovery loop` + `### --force-attach matrix` | `docs/warm-reuse.md` |
| L157–365 | `## LoRA-flexible warm-reuse` + all `###` children (incl. `### Smoke test pyramid`) | `docs/warm-reuse.md` |
| L366–438 | `## Reaping orphan pods` + all `###` children (incl. `### kinoforge forget --id`) | `docs/lifecycle.md` |
| L439–486 | `## Sweeper daemon (B1 / Layer W)` | `docs/lifecycle.md` |
| L487–551 | `## Cost dashboard (B2 / Layer X)` + all `###` children | `docs/cost-and-spend.md` |
| L552–595 | `## Interrupting a generation` + `### Configurable ComfyUI poll timeout` | `docs/lifecycle.md` |
| L596–678 | `## Batch generation` + `### Streaming output` | `docs/batch-and-grid.md` |
| L679–728 | `## Breaking changes` + `### Layer T` + `### Layer M` | `docs/breaking-changes.md` |
| L729–749 | `## Configuration` | `docs/configuration.md` |
| L750–798 | `## Default test LoRAs (Wan 2.1 1.3B T2V)` + `###` children | `docs/warm-reuse.md` |
| L799–879 | `## Default test LoRA (Wan 2.2 T2V)` + `###` children | `docs/warm-reuse.md` |
| L880–910 | `### Concurrency` (under `## Configuration`) | `docs/configuration.md` |
| L911–1048 | `### Cloud-backed ledger` + `### Multi-host setup` + `### Migration from a local ledger` + `### Multi-node coordination` + `### Remote provisioning` | `docs/cloud-stores.md` |
| L1049–1066 | `### HuggingFace ref grammar` | `docs/configuration.md` |
| L1067–1078 | `## Cloud bootstrap (Layer W+α)` | `docs/credentials.md` |
| L1079–1153 | `## Credentials` + `### Precedence` + `### Known keys` + `### Never commit .env` + `### Credential safety in tests` + `### Faster downloads (aria2c)` | `docs/credentials.md` |
| L1154–1176 | `## Real providers — fal.ai` | `docs/engines.md` |
| L1177–1214 | `### Auth strategies` | `docs/credentials.md` |
| L1215–1293 | `## Hosted Bearer providers (Replicate / Runway)` + `###` children | `docs/engines.md` |
| L1294–1324 | `## Bedrock Video` + `### Bedrock Video probe` | `docs/engines.md` |
| L1325–1420 | `## Keyframe stage` + `### When to use it` + `### i2v` + `### flf2v` + `### Implementation note` | `docs/engines.md` |
| L1421–1504 | `### Real providers — RunPod` | `docs/engines.md` |
| L1505–1551 | `## Per-job spec & params` + `### Required spec.*` + `### Top-level params:` + `### On validate_spec failure` | `docs/configuration.md` |
| L1552–1619 | `## Extending` + `### New ComputeProvider` + `### New ModelSource` + `### New GenerationEngine` | `docs/extending.md` |
| L1620–1665 | `### Diffusers inference-server response contract` + `### Hosted response URL — url_path` + `### Cross-engine prompt routing` | `docs/engines.md` |
| L1666–1741 | `### Engine asset wiring — non-native multi-segment continuity` | `docs/engines.md` |
| L1742–1786 | `### New Splitter` + `### New ArtifactStore` | `docs/extending.md` |
| L1787–1835 | `## Output directory` + `### Configuring it` + `### --run-id change` | `docs/output-layout.md` |
| L1836–1880 | `## kinoforge grid` (top-level cells `generate:` / `path:`) | `docs/batch-and-grid.md` |
| L1881–1963 | `### lora_swap: cells` | `docs/warm-reuse.md` |
| L1964–1973 | `## Roadmap` | `docs/roadmap.md` |
| L1974–2008 | `## Cloud stores` | `docs/cloud-stores.md` |
| L2009–2028 | `## Releasing` | `docs/releasing.md` |
| L2029–EOF | `## Design references` | KEEP in new README as a small footer pointer block |

---

## Task 1: Scaffold `docs/<topic>.md` skeleton

**Goal:** Create all 14 destination files under `docs/` with a redirect header so subsequent migration tasks can append content into a known location, and so any operator landing via a stale README anchor finds a clear pointer rather than a 404.

**Files:**
- Create: `docs/configuration.md`
- Create: `docs/credentials.md`
- Create: `docs/lifecycle.md`
- Create: `docs/warm-reuse.md`
- Create: `docs/engines.md`
- Create: `docs/cost-and-spend.md`
- Create: `docs/batch-and-grid.md`
- Create: `docs/cloud-stores.md`
- Create: `docs/extending.md`
- Create: `docs/output-layout.md`
- Create: `docs/troubleshooting.md`
- Create: `docs/breaking-changes.md`
- Create: `docs/roadmap.md`
- Create: `docs/releasing.md`

**Acceptance Criteria:**
- [ ] All 14 files exist.
- [ ] Each file starts with `# <Title>` line followed by `(Moved from README §<source-section-name> on 2026-06-27. See <link-back-to-README>.)` redirect-header block. Files absorbing multiple sources list each in the redirect header.
- [ ] `docs/troubleshooting.md` carries `(New as of 2026-06-27 README rewrite.)` instead of a moved-from header.
- [ ] Body below the header is empty (subsequent tasks fill it).
- [ ] `pixi run pre-commit run --files docs/*.md` is clean.

**Verify:**

```bash
ls docs/{configuration,credentials,lifecycle,warm-reuse,engines,cost-and-spend,batch-and-grid,cloud-stores,extending,output-layout,troubleshooting,breaking-changes,roadmap,releasing}.md
# expected: all 14 paths print, no "No such file" line
```

**Steps:**

- [ ] **Step 1:** Write `docs/configuration.md`:

```markdown
# Configuration

(Moved from README §Configuration, §Concurrency, §HuggingFace ref grammar, §Per-job spec & params on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 2 -->
```

- [ ] **Step 2:** Write `docs/credentials.md`:

```markdown
# Credentials

(Moved from README §Credentials (full deep dive incl. Precedence, Known keys, .env safety, test safety, aria2c), §Cloud bootstrap (Layer W+α), §Auth strategies on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 3 -->
```

- [ ] **Step 3:** Write `docs/lifecycle.md`:

```markdown
# Lifecycle, reaping, and the sweeper daemon

(Moved from README §Operator commands (status, Heartbeat persistence Layer U), §Reaping orphan pods (incl. forget), §Sweeper daemon (B1 / Layer W), §Interrupting a generation, §Configurable ComfyUI poll timeout on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 4 -->
```

- [ ] **Step 4:** Write `docs/warm-reuse.md`:

```markdown
# Warm-reuse, LoRA-flexible swaps, and smoke pyramid

(Moved from README §Operator warm-reuse (B4), §LoRA-flexible warm-reuse (Wan 2.2 + Diffusers) incl. eviction policy, per-pod swap lock, --loras, --dry-run-swap, pod lora ls, failure modes, configuration knob, deferred limitations, smoke test pyramid; §Default test LoRAs (Wan 2.1 1.3B T2V); §Default test LoRA (Wan 2.2 T2V); §kinoforge grid lora_swap: cells on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 5 -->
```

- [ ] **Step 5:** Write `docs/engines.md`:

```markdown
# Engines and providers

(Moved from README §Real providers — fal.ai, §Hosted Bearer providers (Replicate / Runway), §Bedrock Video, §Keyframe stage, §Real providers — RunPod, §Diffusers inference-server response contract, §Hosted response URL — url_path, §Cross-engine prompt routing, §Engine asset wiring on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 6 -->
```

- [ ] **Step 6:** Write `docs/cost-and-spend.md`:

```markdown
# Cost dashboard and spend controls

(Moved from README §Cost dashboard (B2 / Layer X) incl. balance read-out, caching, Prometheus textfile, Replicate throttle on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 7 -->
```

- [ ] **Step 7:** Write `docs/batch-and-grid.md`:

```markdown
# Batch and grid

(Moved from README §Batch generation (incl. Streaming output), §kinoforge grid (top-level generate: / path: cells; the lora_swap: cells variant lives in [warm-reuse.md](warm-reuse.md)) on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 8 -->
```

- [ ] **Step 8:** Write `docs/cloud-stores.md`:

```markdown
# Cloud stores and multi-host coordination

(Moved from README §Cloud-backed ledger, §Multi-host setup, §Migration from a local ledger, §Multi-node coordination, §Remote provisioning, §Cloud stores on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 9 -->
```

- [ ] **Step 9:** Write `docs/extending.md`:

```markdown
# Extending kinoforge

(Moved from README §Extending: add a provider/source/engine (ComputeProvider, ModelSource, GenerationEngine, Splitter, ArtifactStore) on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 10 -->
```

- [ ] **Step 10:** Write `docs/output-layout.md`:

```markdown
# Output directory layout

(Moved from README §Output directory (incl. Configuring it and --run-id change) on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 11 -->
```

- [ ] **Step 11:** Write `docs/troubleshooting.md`:

```markdown
# Troubleshooting

(New as of 2026-06-27 README rewrite. Top-tier short table lives in [../README.md](../README.md#troubleshooting); this file is the deeper symptom catalogue.)

<!-- body filled by Task 12 -->
```

- [ ] **Step 12:** Write `docs/breaking-changes.md`:

```markdown
# Breaking changes

(Moved from README §Breaking changes (Layer T cloud store.kind routing, Layer M engine.hosted.model removed) on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 11 (alongside output-layout, roadmap, releasing) -->
```

- [ ] **Step 13:** Write `docs/roadmap.md`:

```markdown
# Roadmap

(Moved from README §Roadmap (deferred layers and their seams) on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 11 -->
```

- [ ] **Step 14:** Write `docs/releasing.md`:

```markdown
# Releasing kinoforge

(Moved from README §Releasing on 2026-06-27. See [../README.md](../README.md).)

<!-- body filled by Task 11 -->
```

- [ ] **Step 15:** Stage + pre-commit + commit:

```bash
git add docs/configuration.md docs/credentials.md docs/lifecycle.md docs/warm-reuse.md \
        docs/engines.md docs/cost-and-spend.md docs/batch-and-grid.md docs/cloud-stores.md \
        docs/extending.md docs/output-layout.md docs/troubleshooting.md docs/breaking-changes.md \
        docs/roadmap.md docs/releasing.md
pixi run pre-commit run --files docs/configuration.md docs/credentials.md docs/lifecycle.md \
        docs/warm-reuse.md docs/engines.md docs/cost-and-spend.md docs/batch-and-grid.md \
        docs/cloud-stores.md docs/extending.md docs/output-layout.md docs/troubleshooting.md \
        docs/breaking-changes.md docs/roadmap.md docs/releasing.md
git commit -m "docs(scaffold): create docs/<topic>.md skeleton for README rewrite"
```

---

## Task 2: Migrate `docs/configuration.md` body

**Goal:** Move the verbatim configuration content from `README.md` into `docs/configuration.md` so the engineer reading about YAML shapes has a single home for it.

**Files:**
- Modify: `docs/configuration.md` (append body below redirect header).
- Source-only-read: `README.md` (do not edit here; Task 14 rewrites the README).

**Acceptance Criteria:**
- [ ] `docs/configuration.md` contains the verbatim text from current `README.md` lines 729–749 (## Configuration), 880–910 (### Concurrency), 1049–1066 (### HuggingFace ref grammar), 1505–1551 (## Per-job spec & params + 3 ### children) — in that order, demoted to `## ` and `### ` under the file's `# Configuration` H1.
- [ ] No content edits or rewrites; whitespace + heading-level only.
- [ ] All cross-links in the moved content that point to `examples/configs/...` become `../examples/configs/...` (one path-level up since the file moved one directory down).
- [ ] Pre-commit clean.

**Verify:**

```bash
test "$(rg -c '^## (Configuration|Concurrency|HuggingFace ref grammar|Per-job spec)' docs/configuration.md)" -ge 4
test "$(rg -c '^### Required `spec\.\*`' docs/configuration.md)" -eq 1
rg 'examples/configs' docs/configuration.md | rg -v '\.\./examples/configs' && echo FAIL || echo OK
# expected: 4 ## headings present; the spec.* ### present; OK printed.
```

**Steps:**

- [ ] **Step 1:** Append section in this order, taking the body verbatim from the current README:
  - `## Configuration` ← README L729–749 (drop the original `## ` prefix; current README's `## Configuration` becomes the file's `## Configuration` since file H1 is already `# Configuration`).
  - `## Concurrency` ← README L880–910 (was `### Concurrency` under `## Configuration` parent; promote one level so it sits as a sibling here).
  - `## HuggingFace ref grammar` ← README L1049–1066 (same promotion).
  - `## Per-job spec & params` ← README L1505–1551 (including its 3 `### ` children, demoted to `### ` under the now-`## ` parent — no change needed since they were already `### `).

- [ ] **Step 2:** Path-rewrite: every `examples/configs/...` link in the appended content becomes `../examples/configs/...` (because `docs/configuration.md` is one directory below the repo root).

  ```bash
  rg -l 'examples/configs' docs/configuration.md
  # Edit each occurrence: examples/configs/ → ../examples/configs/
  ```

- [ ] **Step 3:** Pre-commit + commit:

  ```bash
  pixi run pre-commit run --files docs/configuration.md
  git add docs/configuration.md
  git commit -m "docs(configuration): migrate Configuration + Concurrency + HF refs + spec/params from README"
  ```

---

## Task 3: Migrate `docs/credentials.md` body

**Goal:** Consolidate every credentials- and auth-related section into one file.

**Files:**
- Modify: `docs/credentials.md`.

**Acceptance Criteria:**
- [ ] `docs/credentials.md` contains the verbatim text from current `README.md`:
  - L1067–1078 (`## Cloud bootstrap (Layer W+α)`)
  - L1079–1153 (`## Credentials` + 5 `### ` children: Precedence, Known keys, Never commit .env, Credential safety in tests, Faster downloads (aria2c))
  - L1177–1214 (`### Auth strategies` — promote to `## Auth strategies`)
- [ ] Path-rewrite: every `AGENTS.md` link becomes `../AGENTS.md`; every `examples/...` becomes `../examples/...`; every `tests/...` becomes `../tests/...`; every `src/...` becomes `../src/...`; every `docs/...` cross-link stays unchanged.
- [ ] Pre-commit clean.

**Verify:**

```bash
test "$(rg -c '^## (Cloud bootstrap|Credentials|Auth strategies)' docs/credentials.md)" -ge 3
test "$(rg -c 'FAL_KEY|CIVITAI_TOKEN|HF_TOKEN|RUNPOD_API_KEY' docs/credentials.md)" -ge 4
rg '\bAGENTS\.md\b' docs/credentials.md | rg -v '\.\./AGENTS\.md' && echo FAIL || echo OK
```

**Steps:**

- [ ] **Step 1:** Append, in this order: Cloud bootstrap → Credentials (full block) → Auth strategies (promoted to `##`).
- [ ] **Step 2:** Apply path rewrites listed in Acceptance Criteria.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/credentials.md
  git add docs/credentials.md
  git commit -m "docs(credentials): consolidate Credentials + Cloud bootstrap + Auth strategies from README"
  ```

---

## Task 4: Migrate `docs/lifecycle.md` body

**Goal:** One home for everything operator-lifecycle related: status / heartbeat / reap / forget / sweeper / interrupt / poll-timeout.

**Files:**
- Modify: `docs/lifecycle.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L31–108 (`## Operator commands` + `### kinoforge status --id` + `### Heartbeat persistence (Layer U)`) — promote `## Operator commands` H2 to `## Operator commands` directly under the file H1; the two `### ` children stay `### `.
  2. README L366–438 (`## Reaping orphan pods` + 7 `### ` children incl. `### kinoforge forget --id <id>`).
  3. README L439–486 (`## Sweeper daemon (B1 / Layer W)`).
  4. README L552–595 (`## Interrupting a generation` + `### Configurable ComfyUI poll timeout`).
- [ ] Path-rewrite per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
for h in 'Operator commands' 'Reaping orphan pods' 'Sweeper daemon' 'Interrupting a generation' ; do
  rg -q "^## $h" docs/lifecycle.md || { echo "MISSING: $h"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Append 4 blocks in order above.
- [ ] **Step 2:** Path rewrites.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/lifecycle.md
  git add docs/lifecycle.md
  git commit -m "docs(lifecycle): migrate Operator commands + Reaping + Sweeper + Interrupting from README"
  ```

---

## Task 5: Migrate `docs/warm-reuse.md` body

**Goal:** One home for every warm-reuse mechanic (B4, LoRA-flexible, default test LoRAs, smoke pyramid, `lora_swap:` grid cells).

**Files:**
- Modify: `docs/warm-reuse.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L109–156 (`## Operator warm-reuse (B4)` + 2 `### ` children).
  2. README L157–365 (`## LoRA-flexible warm-reuse (Wan 2.2 + Diffusers)` + 8 `### ` children incl. `### Smoke test pyramid`).
  3. README L750–798 (`## Default test LoRAs (Wan 2.1 1.3B T2V)` + 3 `### ` children).
  4. README L799–879 (`## Default test LoRA (Wan 2.2 T2V)` + 4 `### ` children).
  5. README L1881–1963 (`### lora_swap: cells — warm-attach LoRA-stack swaps`) promoted to `## lora_swap: grid cells` (since it leaves the `## kinoforge grid` parent that lives in batch-and-grid.md).
- [ ] Path rewrites per Task 3 rules.
- [ ] Internal cross-link added at the top of the migrated `## lora_swap: grid cells` section pointing back to [Grid composition](batch-and-grid.md#kinoforge-grid--composed-side-by-side-mp4-from-n-generations) so the operator can find the `generate:` / `path:` cell variants.
- [ ] Pre-commit clean.

**Verify:**

```bash
for h in 'Operator warm-reuse' 'LoRA-flexible warm-reuse' 'Default test LoRAs' 'Default test LoRA' 'lora_swap: grid cells' ; do
  rg -q "^## $h" docs/warm-reuse.md || { echo "MISSING: $h"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Append 5 blocks in order.
- [ ] **Step 2:** Promote `### lora_swap: cells` to `## lora_swap: grid cells` and prepend a one-line back-link to `batch-and-grid.md`.
- [ ] **Step 3:** Path rewrites.
- [ ] **Step 4:**

  ```bash
  pixi run pre-commit run --files docs/warm-reuse.md
  git add docs/warm-reuse.md
  git commit -m "docs(warm-reuse): migrate B4 + LoRA-flexible + default test LoRAs + lora_swap cells from README"
  ```

---

## Task 6: Migrate `docs/engines.md` body

**Goal:** One home for every engine adapter operator concern: fal, Hosted Bearer (Replicate / Runway), Bedrock Video, Keyframe, RunPod ComfyUI, Diffusers response contract, cross-engine routing, asset wiring.

**Files:**
- Modify: `docs/engines.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L1154–1176 (`## Real providers — fal.ai`).
  2. README L1215–1293 (`## Hosted Bearer providers (Replicate / Runway)` + 3 `### ` children).
  3. README L1294–1324 (`## Bedrock Video` + `### Bedrock Video probe`).
  4. README L1325–1420 (`## Keyframe stage` + 4 `### ` children).
  5. README L1421–1504 (`### Real providers — RunPod`) promoted to `## Real providers — RunPod`.
  6. README L1620–1665 (`### Diffusers inference-server response contract` + `### Hosted response URL — url_path` + `### Cross-engine prompt routing`) — all 3 promoted to `## `.
  7. README L1666–1741 (`### Engine asset wiring — non-native multi-segment continuity`) promoted to `## `.
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
for h in 'Real providers — fal.ai' 'Hosted Bearer providers' 'Bedrock Video' 'Keyframe stage' \
         'Real providers — RunPod' 'Diffusers inference-server response contract' \
         'Hosted response URL' 'Cross-engine prompt routing' 'Engine asset wiring' ; do
  rg -q "^## $h" docs/engines.md || { echo "MISSING: $h"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Append 7 blocks in order, promoting heading levels per Acceptance Criteria.
- [ ] **Step 2:** Path rewrites.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/engines.md
  git add docs/engines.md
  git commit -m "docs(engines): migrate fal + Bearer + Bedrock + Keyframe + RunPod + Diffusers contract + asset wiring from README"
  ```

---

## Task 7: Migrate `docs/cost-and-spend.md` body

**Goal:** One home for cost dashboard mechanics and spend controls.

**Files:**
- Modify: `docs/cost-and-spend.md`.

**Acceptance Criteria:**
- [ ] Body = verbatim README L487–551 (`## Cost dashboard (B2 / Layer X)` + 4 `### ` children: Balance read-out, Caching, Prometheus textfile-collector cron pattern, Replicate throttle warning).
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
rg -q '^## Cost dashboard' docs/cost-and-spend.md
rg -q '^### Balance read-out' docs/cost-and-spend.md
rg -q '^### Replicate throttle warning' docs/cost-and-spend.md
echo OK
```

**Steps:**

- [ ] **Step 1:** Append the block.
- [ ] **Step 2:** Path rewrites.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/cost-and-spend.md
  git add docs/cost-and-spend.md
  git commit -m "docs(cost-and-spend): migrate Cost dashboard + Replicate throttle from README"
  ```

---

## Task 8: Migrate `docs/batch-and-grid.md` body

**Goal:** One home for batch + grid orchestration (lora_swap cells already in warm-reuse.md per Task 5).

**Files:**
- Modify: `docs/batch-and-grid.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L596–678 (`## Batch generation` + `### Streaming output`).
  2. README L1836–1880 (`## kinoforge grid — composed side-by-side mp4 from N generations`) — STOP before the `### lora_swap: cells` block (that block lives in warm-reuse.md per Task 5).
- [ ] A pointer added under the grid `## ` header: `> The lora_swap: cell variant lives in [warm-reuse.md](warm-reuse.md#lora_swap-grid-cells).`
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
rg -q '^## Batch generation' docs/batch-and-grid.md
rg -q '^## `kinoforge grid`' docs/batch-and-grid.md
rg -q 'lora_swap: cell variant lives in' docs/batch-and-grid.md
# lora_swap cells body must NOT appear here (only the back-link)
test "$(rg -c '^## lora_swap' docs/batch-and-grid.md)" -eq 0
echo OK
```

**Steps:**

- [ ] **Step 1:** Append the 2 blocks (stop before the `### lora_swap:` line in the grid block).
- [ ] **Step 2:** Insert back-link to warm-reuse.md directly under the grid `## ` header.
- [ ] **Step 3:** Path rewrites.
- [ ] **Step 4:**

  ```bash
  pixi run pre-commit run --files docs/batch-and-grid.md
  git add docs/batch-and-grid.md
  git commit -m "docs(batch-and-grid): migrate Batch + Streaming + kinoforge grid from README"
  ```

---

## Task 9: Migrate `docs/cloud-stores.md` body

**Goal:** One home for cloud-backed ledger, multi-host coordination, multi-node locks, remote provisioning, cloud-store backends.

**Files:**
- Modify: `docs/cloud-stores.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L911–1048 (`### Cloud-backed ledger` + `### Multi-host setup` + `### Migration from a local ledger` + `### Multi-node coordination` + `### Remote provisioning`) — all 5 promoted to `## ` (they lose their `## Configuration` parent).
  2. README L1974–2008 (`## Cloud stores`).
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
for h in 'Cloud-backed ledger' 'Multi-host setup' 'Migration from a local ledger' \
         'Multi-node coordination' 'Remote provisioning' 'Cloud stores' ; do
  rg -q "^## $h" docs/cloud-stores.md || { echo "MISSING: $h"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Append 6 blocks in order, promoting heading levels per Acceptance Criteria.
- [ ] **Step 2:** Path rewrites.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/cloud-stores.md
  git add docs/cloud-stores.md
  git commit -m "docs(cloud-stores): migrate Cloud-backed ledger + Multi-host + Multi-node + Remote provisioning + Cloud stores from README"
  ```

---

## Task 10: Migrate `docs/extending.md` body

**Goal:** One home for the contributor-facing extension API.

**Files:**
- Modify: `docs/extending.md`.

**Acceptance Criteria:**
- [ ] Body contains, in this order:
  1. README L1552–1619 (`## Extending: add a provider/source/engine` + `### New ComputeProvider` + `### New ModelSource` + `### New GenerationEngine`).
  2. README L1742–1786 (`### New Splitter` + `### New ArtifactStore`) — siblings of the above; keep at `### `.
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean.

**Verify:**

```bash
rg -q '^## Extending' docs/extending.md
for h in 'New ComputeProvider' 'New ModelSource' 'New GenerationEngine' 'New Splitter' 'New ArtifactStore' ; do
  rg -q "^### $h" docs/extending.md || { echo "MISSING: $h"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Append blocks in order.
- [ ] **Step 2:** Path rewrites.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/extending.md
  git add docs/extending.md
  git commit -m "docs(extending): migrate Extending: add a provider/source/engine from README"
  ```

---

## Task 11: Migrate the four small files (`output-layout`, `breaking-changes`, `roadmap`, `releasing`)

**Goal:** One commit for the four small docs/ files — each absorbs <100 lines, so batching avoids commit noise.

**Files:**
- Modify: `docs/output-layout.md`
- Modify: `docs/breaking-changes.md`
- Modify: `docs/roadmap.md`
- Modify: `docs/releasing.md`

**Acceptance Criteria:**
- [ ] `docs/output-layout.md` = README L1787–1835 (`## Output directory` + `### Configuring it` + `### --run-id change`).
- [ ] `docs/breaking-changes.md` = README L679–728 (`## Breaking changes` + `### Layer T` + `### Layer M`).
- [ ] `docs/roadmap.md` = README L1964–1973 (`## Roadmap (deferred layers and their seams)`).
- [ ] `docs/releasing.md` = README L2009–2028 (`## Releasing`).
- [ ] Each file's `## ` heading from the source becomes a `## ` under the file's H1 (so it appears once below the file title).
- [ ] Path rewrites per Task 3 rules.
- [ ] Pre-commit clean across all four files.

**Verify:**

```bash
rg -q '^## Output directory' docs/output-layout.md
rg -q '^## Breaking changes' docs/breaking-changes.md
rg -q '^## Roadmap' docs/roadmap.md
rg -q '^## Releasing' docs/releasing.md
echo OK
```

**Steps:**

- [ ] **Step 1:** Append the appropriate block into each file.
- [ ] **Step 2:** Path rewrites in all four.
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/output-layout.md docs/breaking-changes.md docs/roadmap.md docs/releasing.md
  git add docs/output-layout.md docs/breaking-changes.md docs/roadmap.md docs/releasing.md
  git commit -m "docs(misc): migrate Output directory + Breaking changes + Roadmap + Releasing from README"
  ```

---

## Task 12: Write `docs/troubleshooting.md` body

**Goal:** Create the deeper symptom catalogue. The README will carry only the top ~10 high-frequency entries; this file is the full reference.

**Files:**
- Modify: `docs/troubleshooting.md`.

**Acceptance Criteria:**
- [ ] File contains five `## ` sections in this order, each with at least 3 symptom entries:
  - `## Install and environment`
  - `## Credentials and .env`
  - `## Compute and provider lifecycle`
  - `## Engine and generation failures`
  - `## Cost and spend leaks`
- [ ] Each entry uses the table shape `Symptom | Likely cause | Fix (with link to source-of-truth section)`.
- [ ] Every Fix that references another `docs/<topic>.md` section uses a relative link that resolves on GitHub render.
- [ ] No NEW failure modes invented — every entry is sourced from either the current README's existing failure-mode tables, the project's `CLAUDE.md`, the `successful-generations.md` log, or `PROGRESS.md`. If a candidate symptom is not documented in one of those places, it does not go in this file.
- [ ] Pre-commit clean.

**Symptom-mining commands (use these to enumerate the inputs before writing entries):**

```bash
rg -n 'WARN|ERROR|advisory:|error:|Exception|raise' README.md | head -40
rg -n '^\| `' README.md | head -60          # existing failure-mode tables in README
rg -n 'troubleshoot|symptom|failure mode' CLAUDE.md PROGRESS.md AGENTS.md
```

**Verify:**

```bash
for h in 'Install and environment' 'Credentials and .env' 'Compute and provider lifecycle' \
         'Engine and generation failures' 'Cost and spend leaks' ; do
  rg -q "^## $h" docs/troubleshooting.md || { echo "MISSING: $h"; exit 1; }
done
# each section has >= 3 table rows (lines starting with "| `" or "| symbol")
test "$(rg -c '^\| ' docs/troubleshooting.md)" -ge 18
echo OK
```

**Steps:**

- [ ] **Step 1:** Run the three symptom-mining commands above; transcribe each finding to the appropriate `## ` section.
- [ ] **Step 2:** Cross-link each Fix entry to either a `docs/<topic>.md` section or to the source file (`CLAUDE.md`, `successful-generations.md`).
- [ ] **Step 3:**

  ```bash
  pixi run pre-commit run --files docs/troubleshooting.md
  git add docs/troubleshooting.md
  git commit -m "docs(troubleshooting): write deeper symptom catalogue (5 sections, sourced from README + CLAUDE.md + PROGRESS.md)"
  ```

---

## Task 13: Write the new compact `README.md`

**Goal:** Replace the 2032-line `README.md` with a focused ≤500-line entry-point matching the spec's outline.

**Files:**
- Modify: `README.md` (full rewrite).

**Acceptance Criteria:**
- [ ] File length ≤ 500 lines.
- [ ] Top of file contains, in this order: H1 + one-paragraph what-it-is + Table of contents (links to every `## ` heading in the file + the operator-concept pointers to `docs/<topic>.md`).
- [ ] `## Quickstart` is a 3-step tiered ladder (Step 1: local-fake; Step 2: fal.ai with FAL_KEY; Step 3: pointer to RunPod / `docs/engines.md`).
- [ ] `## Installation` has prereqs (git, pixi link, POSIX shell), clone + `pixi install`, optional feature envs (`live-skypilot`, `live-hosted`), optional system tools (`aria2c`), verify steps (`pixi run kinoforge --help`; `pixi run test`).
- [ ] `## Command cheatsheet` has TWO tables: subcommand table + pixi-task table.
  - Subcommand table includes every verb whose `add_parser(...)` call appears in `src/kinoforge/cli/_main.py`: deploy, provision, doctor, generate, list, status, stop, destroy, forget, reap, gc, cost, pod lora ls, sweeper {start,stop,status,metrics}, batch, grid.
  - Pixi-task table includes every entry under `[tasks]` in `pixi.toml` that an operator would invoke: `test`, `test-cov`, `test-live`, `test-live-skypilot`, `probe-hosted`, `preflight`, `release`, `probe-watchdog`, `lint`, `format`, `typecheck`, `pre-commit`, `pre-commit-all`, `pre-commit-install`, `smoke-local`, `smoke-21b-live`, `smoke-wan22-live`, `smoke-leak-sweep`.
- [ ] `## Configuration at a glance` is a brief summary + a pointer to `docs/configuration.md`.
- [ ] `## Credentials` keeps the .env workflow + Known-keys table inline (operators hit this on day 1).
- [ ] `## Operator concepts` lists one paragraph per concept with a link to `docs/<topic>.md`, covering: lifecycle, warm-reuse, engines, cost-and-spend, batch-and-grid, cloud-stores, output-layout, breaking-changes, roadmap.
- [ ] `## Troubleshooting` has the top-tier table from the spec (10 entries) + a pointer to `docs/troubleshooting.md`.
- [ ] `## Contributing / extending` links to `docs/extending.md` and `AGENTS.md`.
- [ ] `## Releasing` links to `docs/releasing.md`.
- [ ] `## License` block referencing SPDX + `LICENSE` file.
- [ ] No `## ` heading inside the README references a deep-dive concept whose body is in `docs/` (the body lives there, not here).

**Verify (automated):**

```bash
# 1) Length budget
test "$(wc -l < README.md)" -le 500 || { echo "README too long"; exit 1; }

# 2) Every CLI subcommand appears in README cheatsheet
for verb in deploy provision doctor generate list status stop destroy forget \
            reap gc cost batch grid sweeper 'pod lora ls' ; do
  rg -q "kinoforge $verb" README.md || { echo "MISSING verb: $verb"; exit 1; }
done

# 3) Every pixi task we promised appears
for task in test test-cov test-live test-live-skypilot probe-hosted preflight release \
            probe-watchdog lint format typecheck pre-commit pre-commit-all \
            pre-commit-install smoke-local smoke-21b-live smoke-wan22-live smoke-leak-sweep ; do
  rg -q "pixi run $task" README.md || { echo "MISSING pixi task: $task"; exit 1; }
done

# 4) Every operator-concept link points to an existing docs/ file
for f in configuration credentials lifecycle warm-reuse engines cost-and-spend \
         batch-and-grid cloud-stores extending output-layout troubleshooting \
         breaking-changes roadmap releasing ; do
  rg -q "docs/${f}\.md" README.md || { echo "MISSING docs link: $f"; exit 1; }
  test -f "docs/${f}.md" || { echo "MISSING docs file: $f"; exit 1; }
done
echo OK
```

**Steps:**

- [ ] **Step 1:** Run an authoritative pull of the subcommand list:

  ```bash
  rg -n 'add_parser\(' src/kinoforge/cli/_main.py
  ```

  Verify the resulting list matches the verbs enumerated in Acceptance Criteria. If a new verb has landed since planning, add a row.

- [ ] **Step 2:** Run an authoritative pull of the pixi-task list:

  ```bash
  rg -n '^[a-z][a-z0-9-]* = ' pixi.toml | head -100
  ```

  Cross-check against the cheatsheet list.

- [ ] **Step 3:** Replace `README.md` with the new content per the outline in the spec. Use Markdown auto-anchors (no manual `<a name="…">`).

- [ ] **Step 4:** Run the four verify commands; iterate until all four print OK.

- [ ] **Step 5:**

  ```bash
  pixi run pre-commit run --files README.md
  git add README.md
  git commit -m "docs(readme): rewrite as focused entry-point (Quickstart + cheatsheet + concept index)"
  ```

---

## Task 14: Link-rewrite sweep across `PROGRESS.md`, `AGENTS.md`, `CLAUDE.md`, specs, plans

**Goal:** Every existing pointer that referenced a README section now relocated to `docs/<topic>.md` is updated to point at the new home. Stale anchors do not become 404s.

**Files:**
- Modify (only if a stale README anchor is found): `PROGRESS.md`, `AGENTS.md`, `CLAUDE.md`, `docs/superpowers/specs/*.md`, `docs/superpowers/plans/*.md`, `successful-generations.md`.

**Acceptance Criteria:**
- [ ] No occurrence of `README.md#<anchor>` anywhere in the repo points at a section that no longer exists in `README.md`.
- [ ] Every reference to a moved section now points at `docs/<topic>.md#<anchor>` (or just `docs/<topic>.md` when the anchor is the file's H1).
- [ ] References in `docs/superpowers/specs/2026-06-27-readme-rewrite-design.md` and `docs/superpowers/plans/2026-06-27-readme-rewrite.md` are NOT rewritten (they reference the pre-rewrite README intentionally and the redirect headers in `docs/<topic>.md` already cover any link drift).

**Sweep commands:**

```bash
# 1. Find every README anchor reference repo-wide
rg -n 'README\.md#' --type md

# 2. For each match, decide:
#    - If the anchor still resolves in the new README → leave it.
#    - If the anchor is in a moved section → rewrite to docs/<topic>.md[#<anchor>].
#    - If the anchor referenced a section that is now a top-level docs/<file>.md → drop the anchor and link to the file root.

# 3. Verify no anchor points at a heading that does not exist:
for f in $(rg -l 'README\.md#' --type md) ; do
  rg -o 'README\.md#[a-z0-9-]+' "$f" | sort -u | while read ref ; do
    anchor="${ref#README.md#}"
    rg -q "^#+ .*$(echo "$anchor" | tr - ' ')" README.md \
      || echo "STALE in $f: $ref"
  done
done
```

**Verify:**

```bash
# The 'STALE in ...' loop above prints nothing.
# (Above script's stdout is empty when sweep is complete.)
```

**Steps:**

- [ ] **Step 1:** Run the discovery `rg` above; collect the unique set of stale-anchor references.
- [ ] **Step 2:** For each reference, look up the new home in the section-mapping table at the top of this plan and rewrite the link.
- [ ] **Step 3:** Re-run the verification loop until silent.
- [ ] **Step 4:**

  ```bash
  pixi run pre-commit run
  git add -u
  git commit -m "docs(links): repoint stale README anchors at docs/<topic>.md after rewrite"
  ```

  (Use `git add -u` so only modified files are staged; do NOT use `git add -A` because secret-bearing files are gitignored but `.env` etc. should never enter staging by accident.)

---

## Task 15: Final acceptance verification

**Goal:** Run every acceptance check from the spec's `Acceptance` section in one sweep; print a green-line report; fail loudly if any check trips.

**Files:**
- Read-only: every file written above.

**Acceptance Criteria (from spec, repeated here):**
- [ ] `README.md` ≤ 500 lines.
- [ ] README opens with a paragraph, a TOC, and a Quickstart whose Step 1 succeeds with zero credentials.
- [ ] Every `kinoforge` subcommand listed in `cli/_main.py` appears in the cheatsheet table.
- [ ] Every pixi `[tasks]` entry appears in the pixi-task table.
- [ ] Every operator concept in the README points to exactly one `docs/<topic>.md` file that exists in the same commit.
- [ ] No deep-dive section in the current README is dropped without a documented destination in `docs/`.
- [ ] `pixi run pre-commit run --all-files` is clean.

**Verify (single script):**

```bash
set -e

# 1. Length
test "$(wc -l < README.md)" -le 500

# 2. Step 1 of Quickstart actually runs (uses local-fake, no creds required)
pixi run python -m kinoforge --state-dir /tmp/kinoforge-readme-verify-$$ \
  generate --config examples/configs/local-fake.yaml \
  --prompt "verify" --mode t2v --run-id rmverify --no-reuse
rm -rf /tmp/kinoforge-readme-verify-$$

# 3. All CLI verbs present (re-runs Task-13 Verify block 2)
for verb in deploy provision doctor generate list status stop destroy forget \
            reap gc cost batch grid sweeper 'pod lora ls' ; do
  rg -q "kinoforge $verb" README.md
done

# 4. All pixi tasks present (re-runs Task-13 Verify block 3)
for task in test test-cov test-live test-live-skypilot probe-hosted preflight release \
            probe-watchdog lint format typecheck pre-commit pre-commit-all \
            pre-commit-install smoke-local smoke-21b-live smoke-wan22-live smoke-leak-sweep ; do
  rg -q "pixi run $task" README.md
done

# 5. All operator-concept files exist + are linked
for f in configuration credentials lifecycle warm-reuse engines cost-and-spend \
         batch-and-grid cloud-stores extending output-layout troubleshooting \
         breaking-changes roadmap releasing ; do
  rg -q "docs/${f}\.md" README.md
  test -f "docs/${f}.md"
done

# 6. No stale README anchors anywhere
for f in $(rg -l 'README\.md#' --type md) ; do
  rg -o 'README\.md#[a-z0-9-]+' "$f" | sort -u | while read ref ; do
    anchor="${ref#README.md#}"
    rg -q "^#+ .*$(echo "$anchor" | tr - ' ')" README.md \
      || { echo "STALE in $f: $ref" ; exit 1 ; }
  done
done

# 7. Pre-commit clean
pixi run pre-commit run --all-files

echo "ACCEPTANCE: all checks PASS"
```

**Steps:**

- [ ] **Step 1:** Run the script above end-to-end. If any check fails, return to the relevant task and fix.
- [ ] **Step 2:** No commit needed if all checks pass without changing files. If the pre-commit clean check fixes formatting drift, commit:

  ```bash
  git add -u
  git commit -m "docs(readme-rewrite): final formatting fixups for acceptance"
  ```

- [ ] **Step 3:** Update `PROGRESS.md` to reference the new spec + plan in its `## Active workstream` block (this is the durability-rule update, separate commit):

  ```bash
  # Edit PROGRESS.md to add a new ## Active workstream entry pointing at
  # docs/superpowers/specs/2026-06-27-readme-rewrite-design.md and
  # docs/superpowers/plans/2026-06-27-readme-rewrite.md with status CLOSED.
  pixi run pre-commit run --files PROGRESS.md
  git add PROGRESS.md
  git commit -m "docs(progress): README rewrite workstream CLOSED (15 tasks all green)"
  ```
