# Successful generations log — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up `/workspace/successful-generations.md` as the durable C-rule log of every qualifying kinoforge generation; wire reminders in `CLAUDE.md` + `PROGRESS.md`; add a top-level `kinoforge --version` CLI flag; re-fire the four known stacks one at a time with full metric capture.

**Architecture:** Three concentric layers landed as nine atomic commits. Layer A (Tasks 2–4): pure-markdown scaffolding — new log file, durability bullet, PROGRESS pointer. Layer B (Task 5): a single tiny CLI flag (`--version`) so future entries don't have to grep `pyproject.toml`. Layer C (Tasks 6–9): live-spend re-fires of fal/wan-t2v, Wan 2.1 14B i2v on RunPod+ComfyUI, Runway gen4.5, and Replicate seedance-1-lite — each producing one new entry that ships with one commit.

**Tech Stack:** Python 3.12 / pixi / ruff / mypy / pytest. CLI lives in `src/kinoforge/cli/_main.py`. ffprobe via `pixi run ffprobe`. Bash + git for commits.

---

## File structure

**Created:**
- `/workspace/successful-generations.md` — operator-facing log, ships empty (preamble + empty TOC)

**Modified:**
- `/workspace/CLAUDE.md` — append one bullet under `## Durability rules (always)`
- `/workspace/PROGRESS.md` — insert one pointer near the RESUME block; append one `### Phase 46` section
- `/workspace/src/kinoforge/cli/_main.py` — add `--version` flag to `_build_parser`; handle in `main`
- `/workspace/src/kinoforge/__init__.py` — expose `__version__` (resolved at import time) for both the CLI and any future programmatic consumer
- `/workspace/tests/test_cli.py` — add 2 unit tests for `--version` (metadata path + fallback path)

**Live-spend re-fires (Tasks 6–9)** each append one new section to `successful-generations.md` AND commit. No source-code change.

---

## Task 2: Create successful-generations.md scaffold

**Goal:** New file at repo root with the policy preamble + empty TOC. First-time readers learn the policy from the file alone.

**Files:**
- Create: `/workspace/successful-generations.md`

**Acceptance Criteria:**
- [ ] File exists at repo root.
- [ ] Preamble paragraph names: (a) C-rule, (b) `--ephemeral` exclusion, (c) pointer to `CLAUDE.md` Durability rules.
- [ ] `## Table of Contents` heading present.
- [ ] Placeholder line `(no entries yet — first qualifying generation lands here)` under TOC.
- [ ] `pixi run pre-commit run --files successful-generations.md` exits 0.

**Verify:** `test -f /workspace/successful-generations.md && rg -q '^## Table of Contents' /workspace/successful-generations.md && rg -q 'no entries yet' /workspace/successful-generations.md && echo OK`

**Steps:**

- [ ] **Step 1: Write the file**

```markdown
# Successful generations — kinoforge

This file records every qualifying successful kinoforge video generation.
A run qualifies if it introduces a new capability axis — a new mode
(t2v, i2v, flf2v, keyframe, ...), a new provider, engine, or model, or
materially changes the reproduction recipe. Same-tuple repeats get a
"See also" line under the existing TOC entry, not a new section.

Generations run with the `--ephemeral` flag (Layer 5b) MUST NOT appear
in this file under any circumstance — that flag's whole purpose is to
leave no record.

Future agents: see the **Durability rules** section of `/workspace/CLAUDE.md`
for the enforcement policy. The full schema and capture mechanics live
in `docs/superpowers/specs/2026-06-08-successful-generations-log-design.md`.

## Table of Contents

(no entries yet — first qualifying generation lands here)

---
```

- [ ] **Step 2: Pre-commit + commit**

```bash
git add /workspace/successful-generations.md
pixi run pre-commit run --files successful-generations.md
git commit -m "$(cat <<'EOF'
docs(log): scaffold successful-generations.md (Phase 46 Task 2)

Empty operator-facing log of every kinoforge generation that
introduces a new capability axis. Preamble documents the C-rule
dedup + --ephemeral exclusion. First qualifying re-fire (Task 6)
lands the first entry.

Spec: docs/superpowers/specs/2026-06-08-successful-generations-log-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Add CLAUDE.md durability-rules bullet

**Goal:** Make the log policy first-class in the document agents read at every session start.

**Files:**
- Modify: `/workspace/CLAUDE.md` (under `## Durability rules (always)`)

**Acceptance Criteria:**
- [ ] New bullet present under `## Durability rules (always)`.
- [ ] Bullet names: log file path, the four-tuple C-rule, `--ephemeral` exception, "See also" path for repeats.
- [ ] No other CLAUDE.md content changes.
- [ ] Pre-commit clean.

**Verify:** `rg -q 'successful-generations.md' /workspace/CLAUDE.md && rg -q '--ephemeral' /workspace/CLAUDE.md && echo OK`

**Steps:**

- [ ] **Step 1: Read the current Durability rules section**

```bash
rg -n -A 30 '^## Durability rules' /workspace/CLAUDE.md
```

Expected: locate the closing bullet so the new one inserts immediately after it (preserves the existing ordering).

- [ ] **Step 2: Edit the file** — insert this bullet as the final item in the Durability rules list (use Edit tool, with an `old_string` long enough to be unique — include the preceding bullet's tail to anchor):

```markdown
- **Log every qualifying successful generation.** Any kinoforge generation
  that produces a video AND introduces a new capability axis (new mode
  — t2v / i2v / flf2v / keyframe — new provider, engine, model, or
  YAML shape that changes the reproduction recipe, new kinoforge command,
  etc.) AND was NOT run with the `--ephemeral` flag MUST get a new
  detailed section in `/workspace/successful-generations.md` per the
  schema in that file's preamble. Same-tuple `(provider, engine, model,
  mode)` repeats get a "See also" line under the existing TOC entry, not
  a new section. Generations invoked with `--ephemeral` must NEVER appear
  in that file.
```

- [ ] **Step 3: Pre-commit + commit**

```bash
git add /workspace/CLAUDE.md
pixi run pre-commit run --files CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(claude): durability rule — log every new-capability generation (Phase 46 Task 3)

Add a Durability-rules bullet that mandates appending to
successful-generations.md for any kinoforge run that introduces a new
capability axis and was not run with --ephemeral. Same-tuple repeats
become "See also" lines, not new sections.

Spec: docs/superpowers/specs/2026-06-08-successful-generations-log-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Add PROGRESS.md log pointer + Phase 46 entry

**Goal:** Operator-facing pointer near the RESUME block + a Phase 46 entry summarising this layer's work.

**Files:**
- Modify: `/workspace/PROGRESS.md`

**Acceptance Criteria:**
- [ ] One-line pointer present near the `### RESUME — START HERE` heading.
- [ ] `### Phase 46 — Successful-generations log scaffold` section appended after the most recent phase entry (currently Phase 45 / Layer 5b).
- [ ] Phase 46 entry lists Tasks 2–9 with their commit SHAs filled in as work lands (placeholders OK at first-commit time; backfilled by Task 9).
- [ ] Pre-commit clean.

**Verify:** `rg -q 'successful-generations.md' /workspace/PROGRESS.md && rg -q '^### Phase 46' /workspace/PROGRESS.md && echo OK`

**Steps:**

- [ ] **Step 1: Insert the RESUME-block pointer**

Use Edit with a unique anchor (the `### RESUME — START HERE` heading). Insert above the `**Where we are` line:

```markdown
**Successful generations log:** see `successful-generations.md` (added
Phase 46). Per `CLAUDE.md` Durability rules, every new-capability success
gets a new entry unless `--ephemeral` was passed; same-tuple repeats get
a "See also" line.
```

- [ ] **Step 2: Append the Phase 46 section** at the end of `PROGRESS.md`

```markdown
### Phase 46 — Successful-generations log scaffold

Layer 6. Stands up `successful-generations.md` as the durable C-rule
log of every kinoforge generation that introduces a new capability axis.
Adds reminders to `CLAUDE.md` (Durability rules bullet) and the RESUME
block above. Adds a top-level `kinoforge --version` CLI flag so future
log entries don't have to grep `pyproject.toml`. Closes with four
live-spend re-fires (one per known stack) — each appends one entry +
commits atomically.

Spec: `docs/superpowers/specs/2026-06-08-successful-generations-log-design.md`.
Plan: `docs/superpowers/plans/2026-06-08-successful-generations-log.md`.

- [ ] Task 2: `successful-generations.md` scaffold — commit `<sha>`
- [ ] Task 3: `CLAUDE.md` Durability bullet — commit `<sha>`
- [ ] Task 4: `PROGRESS.md` pointer + this section — commit `<sha>`
- [ ] Task 5: `kinoforge --version` flag + 2 tests — commit `<sha>`
- [ ] Task 6: fal-ai/wan-t2v re-fire + entry #1 — commit `<sha>`
- [ ] Task 7: Wan 2.1 14B i2v on RunPod+ComfyUI re-fire + entry #2 — commit `<sha>`
- [ ] Task 8: Runway gen4.5 t2v re-fire + entry #3 — commit `<sha>`
- [ ] Task 9: Replicate seedance-1-lite t2v re-fire + entry #4 — commit `<sha>`

**Live-spend budget (Tasks 6–9):** ~$2 of ~$10.88 remaining.

**Carry-forwards:** none — Layer 6 is self-contained and additive.
```

- [ ] **Step 3: Pre-commit + commit**

```bash
git add /workspace/PROGRESS.md
pixi run pre-commit run --files PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): Phase 46 — successful-generations log + RESUME pointer (Task 4)

Add a one-line pointer near the RESUME block to the new log file,
plus a Phase 46 entry summarising the layer's nine tasks. SHA
backfill happens as each downstream task lands its commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Implement `kinoforge --version` CLI flag + tests

**Goal:** `kinoforge --version` prints `kinoforge X.Y.Z` and exits 0; resolves via `importlib.metadata.version("kinoforge")` with a `pyproject.toml` fallback for un-installed source trees.

**Files:**
- Modify: `/workspace/src/kinoforge/__init__.py` (add `__version__`)
- Modify: `/workspace/src/kinoforge/cli/_main.py:179` (`_build_parser`) and the `main()` dispatch
- Modify: `/workspace/tests/test_cli.py` (add 2 tests)

**Acceptance Criteria:**
- [ ] `pixi run kinoforge --version` prints `kinoforge 0.1.0\n` (or whatever `pyproject.toml` declares) and exits 0.
- [ ] `--version` is processed BEFORE any subcommand validation (so `kinoforge --version` works without `--config`).
- [ ] Two tests: one for metadata path, one forcing the fallback via monkeypatch of `importlib.metadata.version` raising `PackageNotFoundError`.
- [ ] `pixi run test` green, `pixi run lint` clean, `pixi run typecheck` clean, `pixi run pre-commit run --all-files` clean.

**Verify:** `pixi run kinoforge --version && pixi run pytest tests/test_cli.py -k version -v`

**Steps:**

- [ ] **Step 1: RED — write the two failing tests**

Append to `/workspace/tests/test_cli.py`:

```python
def test_version_flag_prints_metadata_version(capsys, monkeypatch):
    """--version surfaces importlib.metadata.version when the package is installed."""
    from kinoforge.cli import main

    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    # Format: 'kinoforge X.Y.Z\n' where X.Y.Z matches semver-ish (digits.dots only).
    import re
    assert re.match(r"^kinoforge \d+\.\d+\.\d+\n?$", captured.out), captured.out


def test_version_flag_falls_back_to_pyproject_when_metadata_missing(
    capsys, monkeypatch
):
    """When importlib.metadata can't resolve the package, fall back to pyproject.toml."""
    from kinoforge.cli import main
    import importlib.metadata

    def _raise(_pkg):
        raise importlib.metadata.PackageNotFoundError("kinoforge")

    monkeypatch.setattr("importlib.metadata.version", _raise)

    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    import re
    assert re.match(r"^kinoforge \d+\.\d+\.\d+\n?$", captured.out), captured.out
```

- [ ] **Step 2: Confirm RED**

```bash
pixi run pytest tests/test_cli.py -k version -v
```

Expected: 2 failures (`--version` not recognised by argparse).

- [ ] **Step 3: GREEN — add `__version__` to `src/kinoforge/__init__.py`**

Read the current `__init__.py`; if it does not already define `__version__`, prepend:

```python
"""kinoforge — GPU video generation orchestrator."""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path


def _resolve_version() -> str:
    """Resolve kinoforge's version string.

    Primary: ``importlib.metadata.version`` (works for both editable and
    wheel installs). Fallback: parse ``pyproject.toml`` from the repo
    root — used when running from an un-installed source tree (e.g.
    test envs that exercise the fallback path).
    """
    try:
        return importlib.metadata.version("kinoforge")
    except importlib.metadata.PackageNotFoundError:
        # Walk up from this file to find pyproject.toml.
        here = Path(__file__).resolve()
        for parent in here.parents:
            pyproject = parent / "pyproject.toml"
            if pyproject.is_file():
                data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
                version = data.get("project", {}).get("version")
                if isinstance(version, str):
                    return version
                break
        return "0.0.0+unknown"


__version__ = _resolve_version()
```

If `__init__.py` already has content, prepend the import + `_resolve_version` + `__version__` assignment near the top, preserving the rest.

- [ ] **Step 4: GREEN — wire the CLI flag**

In `/workspace/src/kinoforge/cli/_main.py`, inside `_build_parser` (line 179+), after the existing `--debug-show-secrets` argument and BEFORE `sub = parser.add_subparsers(...)`, add:

```python
    parser.add_argument(
        "--version",
        action="version",
        version=f"kinoforge {__version__}",
        help="print kinoforge version and exit",
    )
```

At the top of `_main.py`, add the import:

```python
from kinoforge import __version__
```

(argparse's `action="version"` handles printing + sys.exit(0) inside `parse_args` — `main()` does NOT need a separate dispatch branch. The existing `main()` body can stay as-is.)

- [ ] **Step 5: Confirm GREEN**

```bash
pixi run pytest tests/test_cli.py -k version -v
```

Expected: 2 passes.

- [ ] **Step 6: Full quality gate**

```bash
pixi run lint && pixi run typecheck && pixi run test && pixi run pre-commit run --all-files
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/__init__.py src/kinoforge/cli/_main.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): add `kinoforge --version` flag (Phase 46 Task 5)

Top-level --version action prints "kinoforge X.Y.Z" and exits 0.
Source of truth: importlib.metadata.version (handles editable +
wheel installs uniformly); falls back to parsing pyproject.toml
when the package metadata is unavailable (un-installed source
trees, fresh test envs).

Exposes __version__ on the kinoforge package so future programmatic
consumers — including the successful-generations log capture
workflow that motivated this flag — can read it without a CLI
roundtrip.

Two unit tests cover both resolution paths.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Re-fire fal-ai/wan-t2v + log entry #1

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run `kinoforge generate` against fal.ai's queue API with `examples/configs/fal.yaml`. Capture every schema field. Append the first detailed section to `successful-generations.md`. Operator visually confirms the output before close.

**Files:**
- Modify: `/workspace/successful-generations.md` (append TOC entry + new `## 1.` section)

**Acceptance Criteria:**
- [ ] An MP4 (or fal-native container) artifact exists at the sink-recorded path with size > 0.
- [ ] `pixi run ffprobe -v quiet -print_format json -show_format -show_streams <artifact>` returns valid JSON with non-zero `format.duration` and a video stream.
- [ ] New `## 1. \`YYYY-MM-DD HH:MM:SS\` — fal-ai/wan-t2v — t2v` section present with every schema field populated (stack triple, mode, kinoforge version, gen-time SHA, local-TZ timestamp, layer/phase link, exact command, full YAML pasted at gen-time SHA, prompt-file pointer, env-var names, region, capability key, artifact metadata, cost, success criterion, failure-modes section even if empty).
- [ ] New TOC entry under `## Table of Contents` linking to the section, timestamp-prefixed.
- [ ] Operator (Dr. Twinklebrane) visually confirms the output video matches intent.
- [ ] Pre-commit clean.

**Verify:** `rg -q '^## 1\. \`[0-9-]+ [0-9:]+\` — fal-ai/wan-t2v' /workspace/successful-generations.md && rg -q 'fal-ai/wan-t2v.*t2v' /workspace/successful-generations.md && echo OK`

**Steps:**

- [ ] **Step 1: Preflight** — confirm credentials present, no orphan pods, clean tree

```bash
pixi run preflight
```

Expected: exit 0. If non-zero, fix before proceeding (do NOT bypass).

- [ ] **Step 2: Capture pre-run metadata**

```bash
GEN_SHA=$(git rev-parse HEAD)
KINOFORGE_VERSION=$(pixi run kinoforge --version | awk '{print $2}')
FAL_YAML_SHA=$(git log -1 --format=%H -- examples/configs/fal.yaml)
echo "gen-time SHA: $GEN_SHA"
echo "kinoforge version: $KINOFORGE_VERSION"
echo "fal.yaml last-touched SHA: $FAL_YAML_SHA"
```

- [ ] **Step 3: Fire the generation**

```bash
SUBMIT_TS=$(date '+%Y-%m-%d %H:%M:%S')
pixi run kinoforge generate \
  --config examples/configs/fal.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode t2v
FINISH_TS=$(date '+%Y-%m-%d %H:%M:%S')
echo "submit: $SUBMIT_TS"
echo "finish: $FINISH_TS"
```

Expected: exit 0; an `Artifact` line in stdout naming the sink path.

- [ ] **Step 4: ffprobe the artifact**

```bash
ARTIFACT=<paste sink path from Step 3 output>
SIZE=$(stat -c %s "$ARTIFACT")
pixi run ffprobe -v quiet -print_format json -show_format -show_streams "$ARTIFACT" > /tmp/ffprobe-task6.json
cat /tmp/ffprobe-task6.json
```

Extract `streams[0].width`, `streams[0].height`, `format.duration`, `streams[0].nb_frames` (or compute from `avg_frame_rate × duration`), `format.format_name`, `streams[0].codec_name`.

- [ ] **Step 5: Get capability key**

```bash
ls /workspace/.kinoforge/profiles/*.json | head
# pick the freshest; the filename (minus .json) is the capability key
```

- [ ] **Step 6: Visual confirm** — ask the operator (Dr. Twinklebrane) to open the artifact and confirm it matches intent. Record the confirmation literal in the success-criterion field.

- [ ] **Step 7: Append the entry**

Add a new TOC entry under `## Table of Contents`:

```markdown
1. `<FINISH_TS>` — [fal-ai/wan-t2v — t2v](#1-<anchor-form>)
```

(Replace `(no entries yet — first qualifying generation lands here)` with the new entry.)

Append the detailed section per the schema in the spec (`docs/superpowers/specs/2026-06-08-successful-generations-log-design.md` §6). All fields populated from the captures above. Failure-modes section: empty list if nothing surfaced, or one-bullet-per-fix with commit SHA if anything did.

- [ ] **Step 8: Pre-commit + commit**

```bash
git add /workspace/successful-generations.md
pixi run pre-commit run --files successful-generations.md
git commit -m "$(cat <<'EOF'
docs(log): entry #1 — fal-ai/wan-t2v t2v (Phase 46 Task 6)

First live re-fire under the C-rule log: fal.ai queue API +
fal-ai/wan-t2v + t2v mode. ffprobe-verified artifact, cost noted,
operator visually confirmed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 9: Backfill the Phase 46 SHA in PROGRESS.md** (Task 4's bullet for Task 6)

```bash
# Edit PROGRESS.md: replace the `<sha>` placeholder for Task 6 with the just-committed SHA.
# Amend? No — separate commit per CLAUDE.md "create new commits" rule.
git add /workspace/PROGRESS.md
git commit -m "chore(progress): backfill Task 6 SHA (Phase 46)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Re-fire Wan 2.1 14B i2v on RunPod+ComfyUI + log entry #2

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run `kinoforge generate` against RunPod + ComfyUI + kijai WanVideoWrapper + Wan 2.1 14B i2v. Capture the full schema PLUS the RunPod cost decomposition (pod $/hr · spinup_s · gen_s · teardown_s · total_billable_s). Append entry #2.

**Files:**
- Modify: `/workspace/successful-generations.md` (append TOC entry + new `## 2.` section)

**Acceptance Criteria:**
- [ ] An MP4 artifact exists at the sink-recorded path with size > 0.
- [ ] ffprobe returns valid JSON with non-zero duration and a video stream.
- [ ] New `## 2.` section present with EVERY schema field populated; RunPod cost-breakdown sub-table includes pod $/hr, spinup_s (create → first `RUNNING` poll), generation_s (submit → artifact saved), teardown_s (destroy call → `destroy_confirmed`), total_billable_s, and the computed cost.
- [ ] Pod was destroyed cleanly (`pixi run kinoforge list` reports zero active instances after the run).
- [ ] New TOC entry under `## Table of Contents` linking to the section, timestamp-prefixed.
- [ ] Operator (Dr. Twinklebrane) visually confirms the i2v output matches the init frame's subject.
- [ ] Pre-commit clean.

**Verify:** `rg -q '^## 2\. \`[0-9-]+ [0-9:]+\` — Wan 2\.1 14B i2v on RunPod\+ComfyUI' /workspace/successful-generations.md && pixi run kinoforge list | rg -q 'no active instances' && echo OK`

**Steps:**

- [ ] **Step 1: Preflight + zero-pod check**

```bash
pixi run preflight
```

Expected: exit 0 (which already includes the "zero active pods" check).

- [ ] **Step 2: Capture pre-run metadata**

```bash
GEN_SHA=$(git rev-parse HEAD)
KINOFORGE_VERSION=$(pixi run kinoforge --version | awk '{print $2}')
WAN_YAML_SHA=$(git log -1 --format=%H -- examples/configs/runpod-comfyui-wan.yaml)
WAN_GRAPH_SHA=$(git log -1 --format=%H -- examples/configs/runpod-comfyui-wan.graph.json)
```

- [ ] **Step 3: Fire the generation, capturing timestamps at every state transition**

```bash
SUBMIT_TS=$(date '+%Y-%m-%d %H:%M:%S')
T_SUBMIT_S=$(date +%s)
pixi run kinoforge generate \
  --config examples/configs/runpod-comfyui-wan.yaml \
  --prompt "$(cat prompt-field-realistic.txt)" \
  --mode i2v 2>&1 | tee /tmp/run-task7.log
T_FINISH_S=$(date +%s)
FINISH_TS=$(date '+%Y-%m-%d %H:%M:%S')
```

- [ ] **Step 4: Mine the timing data from the run log**

The CLI's structured-logging output stamps each lifecycle event with an ISO timestamp:
- `pod created` event → `created_at`
- first `state=RUNNING` poll succeeded → `ready_at`
- `submit` returned → `submit_response_at`
- `result` returned the artifact → `artifact_at`
- `destroy_instance` returned → `destroy_called_at`
- `destroy_confirmed` returned → `destroyed_at`

Compute:
- `spinup_s = ready_at - created_at`
- `generation_s = artifact_at - submit_response_at`
- `teardown_s = destroyed_at - destroy_called_at`
- `total_billable_s = destroyed_at - created_at`

- [ ] **Step 5: Capture pod $/hr from the offer rate-card**

```bash
rg -i 'priceperhour|costperhr|\$.*hour|dollars_per_hour' /tmp/run-task7.log
```

(The provider's `find_offers` response carries this; it gets logged at `INFO` level.)

- [ ] **Step 6: Compute cost**

```python
cost_usd = (pod_hourly / 3600.0) * total_billable_s
```

- [ ] **Step 7: ffprobe + size + capability key** — same as Task 6 Step 4–5.

- [ ] **Step 8: Confirm pod destroyed**

```bash
pixi run kinoforge list
```

Expected: zero active instances. If non-zero, manually destroy with `pixi run kinoforge destroy --id <id>` BEFORE logging the entry (so the cost reflects reality, not an orphan).

- [ ] **Step 9: Visual confirm** — operator opens the artifact + confirms.

- [ ] **Step 10: Append entry #2** following the spec schema. RunPod cost-breakdown sub-table:

```markdown
**Breakdown (RunPod non-serverless):**
- Pod $/hr: $<value>
- Spinup (create → ready): <spinup_s> s
- Generation wall (submit → artifact saved): <generation_s> s
- Teardown (destroy call → confirmed gone): <teardown_s> s
- Total billable wall: <total_billable_s> s = spinup + generation + teardown
- Cost = ($/hr ÷ 3600) × <total_billable_s> = $<cost_usd>
```

- [ ] **Step 11: Pre-commit + commit + PROGRESS SHA backfill** (same as Task 6 Steps 8–9, with subject `entry #2 — Wan 2.1 14B i2v on RunPod+ComfyUI`).

---

## Task 8: Re-fire Runway gen4.5 t2v + log entry #3

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run `kinoforge generate` against the Runway Bearer API with `examples/configs/comparison/runway-t2v.yaml`. Capture the full schema. Note the `.bin` container quirk from Layer 4 if it recurs. Append entry #3.

**Files:**
- Modify: `/workspace/successful-generations.md` (append TOC entry + new `## 3.` section)

**Acceptance Criteria:**
- [ ] An artifact exists at the sink-recorded path with size > 0; ffprobe confirms ISO Media / MP4 container regardless of file extension.
- [ ] New `## 3.` section present with every schema field; cost = provider-reported wall × rate-card (or response-body cost field if present).
- [ ] New TOC entry, timestamp-prefixed.
- [ ] Operator visually confirms.
- [ ] Pre-commit clean.

**Verify:** `rg -q '^## 3\. \`[0-9-]+ [0-9:]+\` — Runway gen4\.5' /workspace/successful-generations.md && echo OK`

**Steps:**

- [ ] **Step 1: Preflight (hosted-only check)**

```bash
pixi run preflight --check-hosted
```

- [ ] **Step 2–6: Same pattern as Task 6 Steps 2–6** (capture pre-run metadata, fire generation, ffprobe, capability key, visual confirm) with `--config examples/configs/comparison/runway-t2v.yaml --mode t2v`.

- [ ] **Step 7: Capture cost**

Runway responses surface duration + resolution; multiply by the rate-card lookup in the YAML (`spec.params.cost_per_second` if present) or by Runway's published gen4.5 rate (~$0.05/s at 1280×720). Record the formula AND the inputs so the math is auditable.

- [ ] **Step 8–9: Append entry + commit + PROGRESS backfill** (same pattern as Task 6 Steps 7–9).

---

## Task 9: Re-fire Replicate bytedance/seedance-1-lite t2v + log entry #4 + finalise PROGRESS

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run `kinoforge generate` against the Replicate Bearer API with `examples/configs/comparison/replicate-t2v.yaml`. Capture cost from `metrics.predict_time` × rate-card. Append entry #4. Confirm all four entries render correctly on GitHub. Close out Phase 46.

**Files:**
- Modify: `/workspace/successful-generations.md` (append TOC entry + new `## 4.` section)
- Modify: `/workspace/PROGRESS.md` (close Phase 46 — flip the trailing "Single next action" if appropriate)

**Acceptance Criteria:**
- [ ] An MP4 artifact exists at the sink-recorded path with size > 0; ffprobe valid.
- [ ] New `## 4.` section present with every schema field; cost computed from `metrics.predict_time × seedance-1-lite rate-card`.
- [ ] New TOC entry, timestamp-prefixed.
- [ ] Operator visually confirms.
- [ ] All four entries (`## 1`, `## 2`, `## 3`, `## 4`) link correctly from the TOC (anchor format matches GitHub's auto-generated slugs).
- [ ] PROGRESS Phase 46 section: all `<sha>` placeholders for Tasks 2–9 backfilled with real SHAs.
- [ ] Pre-commit clean.

**Verify:** `rg -q '^## 4\. \`[0-9-]+ [0-9:]+\` — Replicate bytedance/seedance-1-lite' /workspace/successful-generations.md && ! rg -q '<sha>' /workspace/PROGRESS.md && echo OK`

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight --check-hosted
```

- [ ] **Step 2–7: Same pattern as Task 6** with `--config examples/configs/comparison/replicate-t2v.yaml --mode t2v`. Replicate's response object includes `metrics.predict_time` (seconds the model actually ran); multiply by the seedance-1-lite rate (~$0.003/s).

- [ ] **Step 8: Append entry #4 + commit** (same pattern as Tasks 6–8).

- [ ] **Step 9: Anchor-render check**

Push to a scratch branch and open the file on GitHub (or render locally via `pixi run python -c "from markdown import markdown; ..."`); click each TOC link; confirm it jumps to the right section.

- [ ] **Step 10: Backfill all remaining PROGRESS Phase 46 SHAs in one commit**

```bash
# Edit /workspace/PROGRESS.md: replace every remaining `<sha>` in the Phase 46 section.
git add /workspace/PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): Phase 46 close-out — all Task SHAs backfilled (Task 9)

Layer 6 ships. Four entries in successful-generations.md (fal,
RunPod+ComfyUI+Wan i2v, Runway gen4.5, Replicate seedance-1-lite),
CLAUDE.md durability rule active, kinoforge --version flag landed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review checklist

- [x] Every spec section has a task: §5 (location/scaffold) → Task 2; §6 (schema) → Tasks 6–9 use schema; §7 (TOC) → Tasks 6–9 append; §8 (`--version`) → Task 5; §9 (reminders) → Tasks 3, 4; §10 (capture mechanics) → Tasks 6–9 Steps; §11 (work breakdown) → Tasks 2–9; §12 (test plan) → Task 5 ACs.
- [x] No placeholders ("TBD", "implement later") in steps. The `<sha>` and `<value>` markers in Tasks 6–9 are runtime captures, not unfilled-by-author placeholders.
- [x] Type / signature consistency: `__version__` defined in `__init__.py`, consumed in `_main.py`. Single import line.
- [x] User-gate banners present on Tasks 6–9 (the four smoke re-fires); absent on Tasks 2–5 (scaffolding only, no operator visual gate).

---

## Live-spend budget summary

| Task | Stack | Estimated $ | Provider | Notes |
|---|---|---|---|---|
| 6 | fal-ai/wan-t2v | ~$0.05–0.30 | fal queue | Fal credit, no API key spend |
| 7 | Wan 2.1 14B i2v on RunPod+ComfyUI | ~$0.30–0.80 | RunPod T4 pod | Cost = $/hr × (spinup + gen + teardown); previous run was ~$0.74 |
| 8 | Runway gen4.5 t2v | ~$1.25 | Runway Bearer | Previous run was ~$1.25 |
| 9 | Replicate seedance-1-lite t2v | ~$0.10 | Replicate Bearer | Previous run was ~$0.10 |
| **Total** | | **~$2.00** | | of ~$10.88 session budget |
