# Successful-generations log — design

**Status:** Approved 2026-06-08
**Author:** Claude Code (Opus 4.7, 1M ctx) for Dr. Twinklebrane
**Layer:** 6 (planned Phase 46)

## 1. Problem

The kinoforge project has produced 12 real videos to date across 4 stacks (fal-ai/wan-t2v, Wan 2.1 14B i2v on RunPod+ComfyUI, Runway gen4.5, Replicate bytedance/seedance-1-lite). Their existence is scattered across `PROGRESS.md` prose, per-layer commit messages, and the `output/` directory. None of the on-disk artifacts has the full metadata an operator would need to reproduce the run: exact command, YAML content at gen-time SHA, kinoforge version, region, env-var names, capability key, resolution, duration, frame count, container, codec, or — for RunPod non-serverless runs — the cost breakdown (pod $/hr × billable seconds, with spinup + generation + teardown decomposition).

We need a single durable file that records every qualifying success going forward, plus a policy that future agents (Claude Code, future operators) honour automatically.

## 2. Goals

1. **Single source of truth.** One file at the repo root, `successful-generations.md`, with a TOC of every qualifying run and a detailed section per run carrying every reproduction metric.
2. **Deduplication by C-rule.** One entry per unique `(provider, engine, model, mode)` tuple. Same-tuple later runs become "See also" lines under the existing TOC entry, not full new sections. This keeps the file scannable as the project grows.
3. **Self-describing.** The file's own preamble documents the policy, the C-rule, the schema, and the `--ephemeral` exception. A first-time reader of the file does not need to consult any other doc to know what it accepts.
4. **Reminder where agents will see it.** Per-session enforcement lives in `CLAUDE.md` (read at every session start per the Session Resume Protocol) and `PROGRESS.md` (operator-facing pointer near the RESUME block). Belt-and-braces.
5. **Ephemeral exception.** Generations invoked with the Layer-5b `--ephemeral` flag MUST NOT be logged. The flag's whole point is workspace lifetime / privacy; logging it would defeat that.
6. **No new tooling unless YAGNI passes.** Metric capture is ad-hoc using `pixi run ffprobe` and existing CLI surfaces. Only one small CLI side-quest is in scope (`kinoforge --version`) because its absence forces every future log entry to grep `pyproject.toml`.

## 3. Non-goals

- Backfilling historical artifacts. The pre-2026-06-08 videos lacked structured metric capture (no pod $/hr, no spinup-time records, partial cost data); reconstructing them from logs and git history would be lossy. The user will re-fire each qualifying stack and log it cleanly.
- A machine-readable JSON sidecar. The markdown is the artifact.
- A `tools/record_generation.py` helper. The agent gathers metrics each run using existing primitives; if drift surfaces, lift to tooling later.
- An automated visual-success check. "Operator visually confirmed" is the only success criterion that matters at this scale.
- Logging ephemeral runs. Explicit non-goal: never log them.

## 4. The C-rule (deduplication)

A run qualifies for a **new detailed section** if and only if its `(provider, engine, model, mode)` tuple does not match any existing entry in the file.

`mode ∈ {t2v, i2v, flf2v, keyframe, ...}` — extensible.

A run with a matching tuple but a different YAML, prompt, output_dir, or segment count gets a **"See also" line** under the existing TOC entry. It does not get a new section.

Future capability axes (new kinoforge subcommand, new audio mode, new stitcher, new output sink) are surfaced via the **Failure modes / capability notes** field on the existing entry, or — if they materially change the reproduction recipe — promoted to a new entry at the operator's discretion.

The agent decides at append time:

1. Read existing TOC entries.
2. For each, compare `(provider, engine, model, mode)` against the just-completed run.
3. If any matches → append a "See also" bullet under that TOC entry. Done.
4. If none match → write a new detailed section + new TOC entry.

## 5. File location and skeleton

**Path:** `/workspace/successful-generations.md` (repo root, alongside `PROGRESS.md`, `DESIGN.md`, `SPEC.md`, `README.md`, `CLAUDE.md`).

**Initial commit contents:**

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
for the enforcement policy.

## Table of Contents

(no entries yet — first qualifying generation lands here)

---
```

The file starts empty (modulo the preamble). The first re-run task appends entry #1.

## 6. Entry schema (locked)

Every detailed section uses this schema, in this order:

```markdown
## N. `YYYY-MM-DD HH:MM:SS` — <provider>/<engine>/<model> — <mode>

| Field | Value |
|---|---|
| **Stack triple** | `<provider> / <engine> / <model>` |
| **Mode** | t2v / i2v / flf2v / keyframe |
| **kinoforge version** | `vX.Y.Z` (from `kinoforge --version` at gen-time SHA) |
| **First-success SHA** | `<40-hex>` |
| **Date (local TZ)** | YYYY-MM-DD HH:MM:SS ±HHMM (generation-finished wall-clock) |
| **Layer / phase** | [Phase N (Layer X)](PROGRESS.md#phase-n-...) |

### Exact command

```bash
kinoforge generate --config examples/configs/foo.yaml [...]
```

### YAML config(s)

**`examples/configs/foo.yaml`** at SHA `<sha>`:

```yaml
<full file contents pasted in>
```

(Repeat for every YAML the command reads.)

### Prompt

- **Source:** `prompt-field-realistic.txt` (committed file in repo) — referenced by filename only per project policy.
- **Or, for ad-hoc prompts:** prompt body pasted inline in a fenced block.

### Env vars / secret names (names only — never values)

- `RUNPOD_API_KEY` (RunPod control plane)
- `RUNPOD_TERMINATE_KEY` (self-terminate scope)
- `FAL_KEY` / `REPLICATE_API_TOKEN` / `RUNWAYML_API_SECRET` / ...

### Region

`us-west-2` / `us-west1` / fal default / etc.

### Capability key

`<64-hex>` from the profile cache (proof of profile freshness at gen time).

### Output artifact

- **Path:** `/workspace/output/<filename>` (or sink-configured path)
- **File size:** N bytes
- **Container / codec:** from `pixi run ffprobe -v quiet -print_format json -show_format -show_streams <path>`
- **Resolution:** WxH
- **Duration:** N.NN s
- **Frame count:** N (from ffprobe `nb_frames` — fall back to round(duration × fps) if `nb_frames` is `N/A`)

### Cost

- **Total:** $X.XX

**Breakdown (RunPod non-serverless only):**
- Pod $/hr: $X.XX (from `find_offers` rate-card at gen-time)
- Spinup (create → ready): NN s
- Generation wall (submit → artifact-saved): NN s
- Teardown (destroy call → `destroy_confirmed`): NN s
- Total billable wall: NN s = spinup + generation + teardown
- Cost = ($/hr ÷ 3600) × total_billable_s = $X.XX

**Hosted / Bearer / queue APIs:** single per-prediction line item. Use provider-reported metrics (`metrics.predict_time` × rate-card for Replicate; Runway response duration × rate-card; fal credit balance delta).

### Success criterion

Operator visually confirmed output matches intent. Optional: specific automated check (e.g. ffprobe duration matches YAML `duration_seconds`).

### Failure modes encountered before success

- (Bug-catch trail — one bullet each, link to fix commit SHAs.)
```

**Notes on schema fields:**

- **Timestamp format:** `YYYY-MM-DD HH:MM:SS` 24-hour, local TZ (per project memory `feedback_local_timezone_only`). Time is generation-finished wall-clock — the moment the artifact was saved to disk, not the submit moment.
- **Section header anchor:** GitHub-flavoured markdown auto-generates the anchor from the heading. The TOC link must match: `[label](#N-yyyy-mm-dd-hhmmss--providerenginemodel--mode)` — colons drop, spaces become hyphens, slashes drop, double-hyphens collapse to single by GitHub's heuristic. The agent should verify the rendered anchor by manual eyeball or `python -c "import urllib.parse; ..."` if uncertain.
- **kinoforge version source:** `kinoforge --version` (added in Section 8). Until that lands, agents fall back to `rg '^version' pyproject.toml`.
- **Capability key:** from the JSON profile cache at `<state_dir>/profiles/*.json` after the run. If the cache file is not yet readable (cold start), agents can retrieve it from the `kinoforge` log stream emitted at discover-or-resolve time.

## 7. Table-of-Contents format

```markdown
## Table of Contents

1. `2026-06-08 14:32:17` — [fal-ai/wan-t2v — t2v](#1-2026-06-08-143217--fal-aiwan-t2v--t2v)
   - See also: `2026-06-08 14:55:01` — Layer-K YAML upgrade (same tuple)
2. `2026-06-08 15:01:44` — [Wan 2.1 14B i2v on RunPod+ComfyUI — i2v](#2-2026-06-08-150144--wan-21-14b-i2v-on-runpodcomfyui--i2v)
3. `2026-06-08 15:30:09` — [Runway gen4.5 (Bearer) — t2v](#3-2026-06-08-153009--runway-gen45-bearer--t2v)
4. `2026-06-08 16:00:12` — [Replicate bytedance/seedance-1-lite (Bearer) — t2v](#4-2026-06-08-160012--replicate-bytedanceseedance-1-lite-bearer--t2v)
```

The timestamp prefix is required on every TOC entry and is repeated in the section header so the anchor and the operator's eyeball both stay in sync.

## 8. Side-quest: `kinoforge --version` CLI flag

The schema requires every entry to carry the kinoforge version. The current CLI has no `--version` surface; agents would have to `rg '^version' pyproject.toml` per entry, which is brittle (drifts if the file moves) and unergonomic.

**Add a top-level `--version` flag** to the kinoforge CLI:

- Resolves the installed version via `importlib.metadata.version("kinoforge")` first. This handles editable installs, wheels, and any future packaging surface uniformly.
- Falls back to parsing `pyproject.toml` only if `importlib.metadata` raises `PackageNotFoundError` (e.g. running from an un-installed source tree under a fresh test env).
- Prints `kinoforge X.Y.Z` to stdout and exits 0.
- Two unit tests: one for the metadata path (default in installed env), one forcing the fallback path via monkeypatch.

**Out of scope:** `kinoforge version` subcommand (the flag is enough); machine-readable JSON output (`--version --format=json`) — YAGNI; build-info beyond the version string — YAGNI.

## 9. Reminder placement

Belt-and-braces. Both surfaces get the rule.

### 9.1 `CLAUDE.md` — `## Durability rules (always)` new bullet

```markdown
- **Log every qualifying successful generation.** Any kinoforge generation
  that produces a video AND introduces a new capability axis (new mode,
  provider, engine, model, YAML shape that changes the reproduction recipe,
  new kinoforge command, ...) AND was NOT run with the `--ephemeral`
  flag MUST get a new detailed section in `successful-generations.md` per
  the schema in that file's preamble. Same-tuple repeats get a "See also"
  line under the existing TOC entry, not a new section. Ephemeral runs
  must NEVER appear in the file.
```

This bullet lives in the canonical Durability section that the session-resume protocol reads first.

### 9.2 `PROGRESS.md` — one-line pointer near the RESUME block

```markdown
**Successful generations log:** see `successful-generations.md` (added
Phase 46). Per `CLAUDE.md` Durability rules, every new-capability success
gets a new entry unless `--ephemeral` was passed; same-tuple repeats get
a "See also" line.
```

### 9.3 `successful-generations.md` — self-describing preamble

Already covered in Section 5. The file documents its own policy so a reader who lands on it directly (search hit, GitHub browse) does not need to consult `CLAUDE.md` to use it.

## 10. Metric capture mechanics (ad-hoc)

For each successful generation, the agent runs these commands and pastes the values into the new entry:

| Field | Command |
|---|---|
| File size | `stat -c %s <path>` |
| ffprobe | `pixi run ffprobe -v quiet -print_format json -show_format -show_streams <path>` |
| Resolution | parse `streams[0].width × streams[0].height` from ffprobe JSON |
| Duration | parse `format.duration` from ffprobe JSON |
| Frame count | parse `streams[0].nb_frames`; fall back to `round(duration × eval(streams[0].avg_frame_rate))` if `nb_frames == "N/A"` |
| Container / codec | parse `format.format_name` and `streams[0].codec_name` |
| Git SHA at gen-time | `git rev-parse HEAD` |
| kinoforge version | `kinoforge --version` (after Section 8 lands) |
| Pod $/hr (RunPod) | from the rate-card returned by `RunPodProvider.find_offers` — captured in the `Instance.spec` at create time |
| Spinup time (RunPod) | `Instance.created_at` until the first `state == "RUNNING"` poll succeeds in `Provider.get_instance` |
| Generation wall | submit-call timestamp until `Artifact` returned by `Stage.run` |
| Teardown time (RunPod) | `destroy_instance` call until `destroy_confirmed` returns |
| Total billable (RunPod) | spinup + generation + teardown (RunPod meters wall-clock from create to destroy-confirmed) |

The agent computes the cost as `($/hr ÷ 3600) × total_billable_s` and pastes both the formula and the result.

For hosted / Bearer / queue runs, the agent records the provider-reported metric (Replicate `metrics.predict_time`, Runway response duration field, fal credit-balance delta) and the rate-card lookup, then computes per-prediction cost as `metric × rate`.

## 11. Phase 46 work breakdown (single layer)

Implementation lands as one layer with the following ordered atomic commits:

1. Spec doc commit (this file).
2. `successful-generations.md` scaffold (preamble + empty TOC + placeholder).
3. `CLAUDE.md` durability bullet append.
4. `PROGRESS.md` pointer + Phase 46 entry append.
5. `kinoforge --version` CLI flag + 2 unit tests + pre-commit clean.
6. fal-ai/wan-t2v re-run + entry #1 append + commit.
7. Wan 2.1 14B i2v on RunPod+ComfyUI re-run + entry #2 append + commit. (Live-spend; pre-flight check first.)
8. Runway gen4.5 t2v re-run + entry #3 append + commit. (Live-spend; pre-flight check first.)
9. Replicate seedance-1-lite t2v re-run + entry #4 append + commit. (Live-spend; pre-flight check first.)

Items 6–9 are wall-clock-bound and may surface bugs (Layer 4's `f20a70d` caught four). Each gets its own commit and its own bug-catch trail in the entry's "Failure modes" field.

## 12. Test plan

**Spec-doc-only tests:** none — markdown.

**`kinoforge --version` tests** (in `tests/test_cli.py` or a new `tests/test_version.py`):

1. `test_version_flag_prints_metadata_version`: invokes the CLI with `--version`, asserts stdout matches `r"^kinoforge \d+\.\d+\.\d+\n?$"` and exit code is 0. Uses the default `importlib.metadata.version` path.
2. `test_version_flag_falls_back_to_pyproject_when_metadata_missing`: monkeypatches `importlib.metadata.version` to raise `PackageNotFoundError`, asserts the CLI still prints a valid `kinoforge X.Y.Z` line from `pyproject.toml`.

**End-to-end smoke** (manual, post-merge): operator runs each of the four re-fires (tasks 6–9) and visually confirms each new entry renders correctly on GitHub (TOC links resolve, fenced code blocks display).

## 13. Risk and rollback

**Risk:** the agent appends an entry to the file for a run that was actually `--ephemeral`. Mitigation: the durability bullet in `CLAUDE.md` is the first place a session-resuming agent reads; the file's own preamble repeats the exception; the C-rule check happens BEFORE the agent decides to append. If the operator catches a wrongly-logged ephemeral run after the fact, the fix is a `git revert` of the offending commit and an apology.

**Risk:** the C-rule dedup decision is judgement-laden (what counts as "materially changes the reproduction recipe"?). Mitigation: when uncertain, the agent asks the operator. The "See also" path is the conservative default; the operator can always promote a See-also to a new entry later.

**Risk:** a RunPod cost capture misses one of spinup / generation / teardown. Mitigation: the schema enforces all four numbers must be present and the formula must be shown; a missing field is a visible gap on review.

**Rollback:** every Phase-46 commit is small and atomic. Reverting any single step (e.g. `kinoforge --version` flag misfires in CI) doesn't poison the rest. The scaffolding (file, reminders) is purely additive — it can sit empty indefinitely.

## 14. Open questions

None at design time. All locked through the brainstorming exchange:

- C-rule (one-per-tuple, See-also for repeats) — locked.
- Schema fields (incl. kinoforge version, timestamp prefix) — locked.
- Reminder placement (CLAUDE.md + PROGRESS.md + self-preamble) — locked.
- `kinoforge --version` side-quest in scope — locked.
- Backfill out of scope — locked.
- Helper tool out of scope — locked.
- Live-spend pre-flight required before each re-fire — implicit from CLAUDE.md spend-policy rules.
