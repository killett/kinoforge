# Local refit of superpowers-extended-cc transcript-reading hooks

Five PreToolUse / PostToolUse / Stop hooks shipped by the
superpowers-extended-cc marketplace parse the on-disk session JSONL
transcript to recover task state. The harness flushes that JSONL at
turn-end, so any same-turn `TaskUpdate(...)` is invisible to the
hook — it sees the previous turn's state and refuses actions whose
live precondition is satisfied.

This directory ships a refit: the inline transcript-parse moves to
`lib/tasks_live_query.py`, which reads the harness's live on-disk
task store at `~/.claude/tasks/<sessionId>/<taskId>.json` and falls
back to the marketplace's transcript-parse logic when the live store
is missing.

## Install

    bash tools/local_hooks/install.sh

Copies the helper + 5 hooks into `~/.claude/hooks/`, backs up
`~/.claude/settings.json` to `*.pre-hook-swap-2026-06-18.bak`, and
rewrites the 5 hook command paths.

## Uninstall

    cp ~/.claude/settings.json.pre-hook-swap-2026-06-18.bak \
       ~/.claude/settings.json

The marketplace hook files were never modified, so this single
command fully reverts.

## Files

| File | Purpose |
|------|---------|
| `lib/tasks_live_query.py` | Live-store reader; falls back to transcript |
| `pre-commit-check-tasks.sh` | Blocks `git commit` while any task is in_progress |
| `pre-task-blockedby-enforce.sh` | Blocks `TaskUpdate(in_progress)` when blockers not completed |
| `post-task-complete-revalidate.sh` | Blocks close of user-gate without evidence |
| `post-agent-return-validate.sh` | Blocks subagent return missing evidence-token axes |
| `stop-revalidate-user-gates.sh` | Blocks Stop when closed user-gate lacks evidence |
| `install.sh` | Install script |

## Spec

Full design: `docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md`.

## Limitations

`~/.claude/tasks/<sessionId>/*.json` is currently undocumented. The
transcript-fallback path keeps the hook functional if Anthropic
moves the store. See the spec's "Risk" section + the linked upstream
feature request for the long-term plan.
