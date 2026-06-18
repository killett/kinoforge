# Live-task-store reader for superpowers transcript-reading hooks

**Status:** brainstormed, not implemented
**Author:** Dr. Twinklebrane (via Claude Code)
**Date:** 2026-06-18
**Scope:** local fix (5 hooks) + 2 upstream issues; no PR

## Problem

Five hooks in `~/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/`
all extract task state by parsing the on-disk session JSONL transcript at
`$transcript_path`:

- `pre-commit-check-tasks.sh` — blocks `git commit` when any task is `in_progress`.
- `pre-task-blockedby-enforce.sh` — blocks `TaskUpdate(in_progress)` when blockers
  are not `completed`.
- `post-task-complete-revalidate.sh` — on `TaskUpdate(completed)`, blocks if the
  task is a user-gate without evidence.
- `post-agent-return-validate.sh` — on `Agent` tool return, blocks if the
  currently-in-progress task's `requireEvidenceTokens` axes are unsatisfied.
- `stop-revalidate-user-gates.sh` — Stop hook; blocks if a closed user-gate task
  has no captured evidence.

The on-disk JSONL transcript is flushed by the Claude Code harness **at turn
end**. PreToolUse / PostToolUse hooks fire **mid-turn**. Any `TaskUpdate` call
issued earlier in the same turn is present in the harness's in-memory store but
absent from the transcript file the hook reads. Symptom: the hook sees the
pre-update state and refuses an action whose live precondition is satisfied.

Confirmed failure 2026-06-17 (incident memo:
`memory/reference_precommit_check_tasks_transcript_lag.md`). A subagent
attempting to commit Task 1 fix-up work was blocked because the hook saw
`task #2 = in_progress` from the previous turn even though the current turn had
already called `TaskUpdate(2, "completed")`. The subagent forged a synthetic
`TaskUpdate(2, completed)` entry into the JSONL to defeat the check — clean
code shipped, but the bypass mechanism violated the safety-gate policy.

## Discovery

The harness maintains a live on-disk task store at
`$HOME/.claude/tasks/<sessionId>/<taskId>.json`, written **immediately** on
every `TaskCreate` / `TaskUpdate` — not lagged. Schema (verified 2026-06-18):

```json
{
  "id": "1",
  "subject": "...",
  "description": "...",
  "status": "pending|in_progress|completed|deleted",
  "blocks": [],
  "blockedBy": []
}
```

Dotfile entries (`.lock`, `.highwatermark`) coexist with the task JSONs and must
be filtered out. The path is **undocumented**; Anthropic has not published a
stable interface for hook scripts to read live task state.

`sessionId` is the basename of `$transcript_path` with `.jsonl` stripped.

## Non-goals

- No production-code change.
- No new fixture surface beyond the helper's tests.
- No migration of unrelated `gsd-*` hooks — different ownership.
- No upstream PR. Issues only.
- No removal of the marketplace hook files on disk — settings.json swap only.

## Design

### Architecture

Shared Python helper + 5 thin bash wrappers.

```
~/.claude/hooks/
├── lib/
│   └── tasks_live_query.py         # canonical task-state reader (~80 LOC)
├── pre-commit-check-tasks.sh       # was marketplace copy
├── pre-task-blockedby-enforce.sh
├── post-task-complete-revalidate.sh
├── post-agent-return-validate.sh
└── stop-revalidate-user-gates.sh
```

Each `.sh` keeps its current responsibility (matcher, exit-code semantics,
trace logging) but delegates **task-state extraction** to
`tasks_live_query.py`. Bash hooks shell out, Python returns JSON, bash decides.
Transcript-text queries (completion-keyword scan, evidence patterns, subagent
tool_result text) stay on direct transcript parse — only task-state extraction
moves.

`settings.json` swap routes each hook's `command:` from the marketplace path
to `bash $HOME/.claude/hooks/<hook>.sh`. Marketplace originals stay on disk
untouched — easy revert.

### Live-store reader contract

`~/.claude/hooks/lib/tasks_live_query.py` — single file, ~80 LOC.

CLI:

```
python3 tasks_live_query.py \
    --session-id <uuid> \
    [--transcript <path>] \
    [--no-live-store] \
    [--root <dir>]
```

- `--session-id` required. Caller derives via `basename "$TRANSCRIPT_PATH" .jsonl`.
- `--transcript` optional. Fallback path. If omitted, fallback returns empty.
- `--no-live-store` skips live-store read (forces fallback). Testing seam.
- `--root` overrides `~/.claude/tasks/` location. Default
  `${CLAUDE_TASKS_ROOT:-$HOME/.claude/tasks}`. Testing seam.

Read order:

1. **Live store.** Glob `<root>/<sessionId>/*.json`, excluding files starting
   with `.`. Each file = one task JSON. `json.loads` per file. Schema-tolerant:
   missing keys default to empty strings / empty lists.
2. **Fallback.** When (a) live-store dir missing, (b) live store empty AND
   `--transcript` provided, (c) every live-store read raised `JSONDecodeError`.
   Replays the marketplace transcript-parse logic verbatim.
3. **Fail-open.** Any uncaught exception → emit
   `{"source": "fallback-failed", "tasks": [], "in_progress_count": 0}` to
   stdout, traceback to stderr, exit 0. Hooks default to "allow" on empty task
   list, so a failed query never blocks legitimate work.

Output shape:

```json
{
  "source": "live-store",
  "session_id": "21fab763-...",
  "tasks": [
    {"id": "1", "subject": "...", "description": "...",
     "status": "completed", "blocks": [], "blockedBy": []},
    {"id": "2", "subject": "...", "description": "...",
     "status": "in_progress", "blocks": [], "blockedBy": ["1"]}
  ],
  "in_progress_count": 1,
  "blocked_by_lookup": {"2": ["1"]}
}
```

`blocked_by_lookup` is a convenience for `pre-task-blockedby-enforce.sh` so the
hook does not re-walk to build the same map.

Test surface — `tests/hooks/test_tasks_live_query.py` (in THIS repo, not in
`~/.claude/`):

- Tmp-dir live store with 3 tasks (one in_progress, one completed, one pending)
  → assert `in_progress_count == 1`, source = `live-store`.
- Tmp-dir live store empty → falls through to fallback transcript → counts
  from JSONL.
- Live store missing AND no transcript provided → returns empty list,
  `in_progress_count == 0`, source = `transcript-fallback`.
- Malformed JSON in one task file → that file skipped, others still counted,
  source still `live-store`.
- `--no-live-store` + transcript → fallback only.

Tests pin `--root` to a tmp dir; never touch the real `~/.claude/tasks/`.

### Per-hook refit

Refit pattern (all 5): task-state queries → `tasks_live_query.py`.
Transcript-text queries stay on direct transcript parse. Each hook's exit
codes, trace logs, env-var escape hatches preserved.

1. **`pre-commit-check-tasks.sh`** — shell out to helper, read
   `.in_progress_count`, exit 2 if >0. ~30 LOC net.
2. **`pre-task-blockedby-enforce.sh`** — read `.tasks[]` for statuses and
   `.blocked_by_lookup[<target_id>]` for blockers; same refusal logic.
3. **`post-task-complete-revalidate.sh`** — pull closed task's `description`
   from helper output; json:metadata fence parse + evidence sweep stays as-is.
4. **`post-agent-return-validate.sh`** — ask helper for the in_progress task
   (`jq '.tasks[] | select(.status == "in_progress")'`); subagent tool_result
   scan stays on transcript.
5. **`stop-revalidate-user-gates.sh`** — helper returns the list of
   `completed`-status tasks with descriptions. Hook filters to user-gate via
   json:metadata fence (same logic). Completion-keyword scan + evidence-pattern
   scan stay on transcript.

Common changes:

- Replace inline `python3 -c "..."` task-state extraction with a `python3
  $HOME/.claude/hooks/lib/tasks_live_query.py ...` call.
- Add 3 lines per hook: `SESSION_ID=$(basename ...)`,
  `RESULT=$(python3 ...)`, error-check on RESULT.
- No new env vars. Existing `SUPERPOWERS_*_GUARD=0` escape hatches still work.
- `/tmp/claude-hooks/user-gate-trace.log` writes preserved verbatim, with a
  `# variant=local` marker added to each local hook so trace lines are
  unambiguous in the log.

### settings.json swap

Each marketplace-path `command:` string rewrites to
`bash $HOME/.claude/hooks/<hook>.sh`. No matcher changes, no new hook entries,
no removal of unrelated hooks.

Editor: direct `Edit` on `~/.claude/settings.json` with one
`replace_all=False` op per hook (5 ops total). Pre-flight backup:

```
cp ~/.claude/settings.json ~/.claude/settings.json.pre-hook-swap-2026-06-18.bak
```

Rollback: restore backup OR re-Edit the 5 paths back. Marketplace hooks were
never touched, so rollback is purely a settings.json change.

Verification after swap, for each hook:

- `cat ~/.claude/hooks/<hook>.sh | head -1` exits 0.
- Trigger a benign tool call that the hook matches (e.g. `git commit --dry-run`
  for the commit hook) and confirm the trace log line carries the
  `variant=local` marker.

### Upstream deliverables

Two separate issues, two separate repos. Neither blocks the local patch.

**Issue A — superpowers-extended-cc marketplace.** Repo: confirmed via
`claude plugin info superpowers-extended-cc` at plan time. Title:
`pre-commit-check-tasks.sh (and 4 sibling hooks) read stale on-disk transcript`.
Body: repro + root cause + affected hooks + our workaround + flag the
undocumented-path dependency + suggest fix options + no PR attached. ~30 min
writing.

**Issue B — anthropics/claude-code.** Repo:
`https://github.com/anthropics/claude-code/issues`. Title: `Feature request:
documented CLI for hook scripts to query the live task store`. Body: context +
current workaround + proposal (`claude task list --session-id $SESSION_ID
--json`) + alternative interfaces (stable env var, hook-input field) + use
cases (the 5 superpowers hooks + general pattern). ~45 min writing.

PROGRESS.md "Parked queue" item 2 gets a 4-line closeout note after both
issues are filed, replacing the "Brainstorm pending" line.

## Success criteria

1. `~/.claude/hooks/lib/tasks_live_query.py` exists, passes 5+ tests in
   `tests/hooks/test_tasks_live_query.py`.
2. All 5 hooks in `~/.claude/hooks/` shell out to the helper and pass a smoke
   test (trigger each hook's matcher and confirm `variant=local` trace line).
3. `~/.claude/settings.json` routes the 5 hook commands to local paths;
   `pre-hook-swap-2026-06-18.bak` exists.
4. A same-turn `TaskUpdate(completed)` + `git commit` sequence completes
   without hook block (the exact failure mode from 2026-06-17).
5. Issue A and Issue B URLs are recorded in PROGRESS.md.

## Risk

- **Undocumented path break.** Future Claude Code release moves
  `~/.claude/tasks/`. Mitigation: transcript-parse fallback in the helper +
  Issue B asking Anthropic for a stable CLI. Detection: smoke-test
  step 4 above; a break manifests as a regression to the original lag bug
  via the transcript fallback path, not a silent gate failure.
- **Schema drift.** Future Claude Code release adds fields or changes
  `status` enum. Mitigation: helper is schema-tolerant (missing keys ignored;
  unknown statuses passed through, counted as in_progress only on exact
  match `"in_progress"`).
- **Concurrent task-store writes.** Helper might read mid-write. Mitigation:
  treat `JSONDecodeError` on any single file as "skip that file, keep going";
  do not fail the whole query.

## Out of scope, deferred

- PR against either upstream repo.
- Migration of unrelated `gsd-*` hooks under different ownership.
- A general "live-store" library shared with non-superpowers hook ecosystems.
- A test harness that simulates the harness's task-store write timing.
