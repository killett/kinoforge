# Differential test: marketplace vs. local-refit hooks

**Date:** 2026-06-18
**Test script:** `tests/hooks/differential_test.sh`
**Captured log:** `/tmp/diff-test.log` (regenerated on each run)

## Purpose

Prove that the refit hooks at `~/.claude/hooks/` correctly handle states
where the marketplace originals at
`~/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/`
exhibit the transcript-lag bug. Each test builds a synthetic transcript
JSONL + a synthetic on-disk task store (`~/.claude/tasks/<sid>/<tid>.json`),
pipes the same hook-input JSON to both hooks, and compares exit codes +
stderr.

The marketplace hook reads the on-disk JSONL transcript. The refit hook
shells out to `tools/local_hooks/lib/tasks_live_query.py`, which reads
the on-disk live task store with transcript fallback.

## Test matrix

| # | Hook | State scenario | Marketplace expected | Refit expected |
|---|------|---|---|---|
| 1 | `pre-commit-check-tasks.sh` | Transcript: task#1 `in_progress`. Live store: task#1 `completed`. Mid-turn commit. | BLOCK (rc=2) — sees stale `in_progress` | ALLOW (rc=0) — sees live `completed` |
| 2 | `pre-task-blockedby-enforce.sh` | Transcript: no `addBlockedBy` flushed yet. Live store: task#3 `blockedBy=["2"]`, task#2 `in_progress`. Attempt `TaskUpdate(3, in_progress)`. | ALLOW (rc=0) — no blocker info in transcript, false negative | BLOCK (rc=2) — live store knows the dependency |
| 3 | `pre-commit-check-tasks.sh` | Transcript empty (fresh session). Live store: task#1 `in_progress`. Commit attempt. | ALLOW (rc=0) — false negative on empty transcript | BLOCK (rc=2) — live store has the task |
| 4 | `pre-commit-check-tasks.sh` (baseline) | Both data sources agree: task#1 `in_progress`. | BLOCK (rc=2) | BLOCK (rc=2) — refit must not falsely allow when agreement holds |

## Results — 2026-06-18

### Test 1 — same-turn `TaskUpdate(completed)` + commit (the original 2026-06-17 incident)

**Marketplace:** `exit=2`, stderr `COMMIT BLOCKED: 1 native task(s) still in progress. Finish the current task before committing.`
**Refit:** `exit=0`, no stderr.

**VERDICT: PROVEN.** Marketplace blocks. Refit allows. The transcript-lag
bug that caused the 2026-06-17 incident is fixed. This is the test the
entire workstream exists for.

### Test 2 — same-turn `addBlockedBy` missing from transcript

**Marketplace:** `exit=0`, no stderr. Walks the transcript, reconstructs
task#3 with no blockers, allows.
**Refit:** `exit=2`, stderr `BLOCKED-BY DEPENDENCY NOT COMPLETED — SELF-ASSESS
BEFORE PROCEEDING / You tried to move Task #3 ('blocked') into in_progress,
but its blockedBy list still points at tasks that are not completed: Task #2
[in_progress]: blocker`.

**VERDICT: PROVEN.** Marketplace silently allows work on a task whose
prerequisites are not done, because the `blockedBy` edge was set
this-turn and isn't in the transcript yet. The refit reads the live
store and correctly refuses. **Direction inversion** of the lag bug —
marketplace's false-negatives are arguably worse than its false-positives
because the operator never sees them.

### Test 3 — empty transcript, live store has `in_progress`

**Marketplace:** `exit=0`, no stderr. Empty transcript → 0 tasks counted
→ allow.
**Refit:** `exit=2`, stderr `COMMIT BLOCKED: 1 native task(s) still in
progress (source=live-store, variant=local). Finish the current task
before committing.`

**VERDICT: PROVEN.** Marketplace cannot block on a `TaskCreate` +
`TaskUpdate(in_progress)` pair that happened in the very turn the
commit is attempted, because the transcript file has not been written
yet at all. The refit reads the live store and sees the task.

### Test 4 — baseline agreement

**Marketplace:** `exit=2`.
**Refit:** `exit=2`.

**VERDICT: PASS.** Both hooks block when both data sources agree on
`in_progress`. The refit does not introduce false-positives on the
common path.

## Summary

| Test | Marketplace | Refit | Outcome |
|------|---|---|---|
| 1: same-turn close + commit | BLOCK (false +) | ALLOW (correct) | Lag bug FIXED |
| 2: same-turn addBlockedBy missing | ALLOW (false -) | BLOCK (correct) | Marketplace under-enforcement caught |
| 3: empty transcript + live in_progress | ALLOW (false -) | BLOCK (correct) | Marketplace under-enforcement caught |
| 4: baseline agreement | BLOCK | BLOCK | Refit does not over-trigger on common path |

4 / 4 expected outcomes. Zero false-positives or false-negatives on the
refit across the matrix.

## How to reproduce

```bash
bash tests/hooks/differential_test.sh
```

The script:
1. `mktemp -d` for a per-run isolated work dir.
2. Builds 4 independent (`session_id`, transcript JSONL, live-store
   directory) triples for each test.
3. Pipes a synthetic hook-input JSON to both hooks per test.
4. Captures exit code + stderr.
5. Tabulates verdicts.

The marketplace hook reads `$transcript_path` directly. The refit hook
reads the live store via the `CLAUDE_TASKS_ROOT` env var (default
`$HOME/.claude/tasks/`), which `tasks_live_query.py` honours.

## Limitations

- `post-task-complete-revalidate.sh`, `post-agent-return-validate.sh`,
  `stop-revalidate-user-gates.sh` are NOT covered here. Those hooks
  depend on the assistant's message text scanning, which is harder to
  synthesise in a unit-style test. They WERE smoke-tested live in the
  Task 3 user-gate flow (see `/tmp/local-hooks-smoke.log`); their trace
  log entries carry `variant=local`.
- Test 2's marketplace branch reflects what the marketplace
  `pre-task-blockedby-enforce.sh` does TODAY against a transcript that
  shows no `addBlockedBy`. The marketplace original walks `TaskCreate`
  inputs and `TaskUpdate.addBlockedBy` calls; if a future Claude Code
  release populates `blockedBy` directly in `TaskCreate.input.blockedBy`
  arrays, marketplace might catch Test 2's case too. The refit will
  still match because it reads the canonical state file.
- The differential test is a unit-style synthetic — not a full
  integration test of a real Claude Code session. The 2026-06-18
  same-turn `TaskUpdate(7, completed)` + `git commit` cycle that landed
  as commit `10700bb` is the full-integration counterpart of Test 1.
