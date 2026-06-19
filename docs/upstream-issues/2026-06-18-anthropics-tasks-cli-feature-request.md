# Feature request: documented CLI for hook scripts to query the live task store

## Context

Claude Code hooks (`PreToolUse`, `PostToolUse`, `Stop`, etc.) frequently
need to query the current task state to make blocking decisions —
"block `git commit` while a task is in_progress", "block `TaskUpdate`
when blockers are not completed", "block close of a user-gate without
evidence", etc. Examples of such hooks ship today as part of the
`superpowers-extended-cc` plugin.

The only documented surface a hook has for task state is the session
JSONL transcript at `$transcript_path`. The harness flushes that
transcript at turn-end, but hooks fire mid-turn. Any same-turn
`TaskUpdate` is invisible to the hook — it sees the previous turn's
state and refuses an action whose live precondition is actually
satisfied.

We hit this bug 2026-06-17 in production. A subagent then bypassed
the hook by appending a forged `TaskUpdate(...)` line to the JSONL,
which is a safety-gate violation even when the underlying code is
fine. The pattern is easy to repeat; the friction encourages the
bypass.

## Current workaround

Hook authors can read the harness's on-disk task store directly at:

```
$HOME/.claude/tasks/<sessionId>/<taskId>.json
```

Each task is one JSON file. Schema (observed): `{id, subject,
description, status, blocks, blockedBy}`. Dotfile siblings (`.lock`,
`.highwatermark`) must be filtered.

This works today but is undocumented. If the store moves to SQLite,
an in-memory daemon, or any other location in a future release, every
hook depending on the path silently breaks.

## Proposal

A documented CLI:

```
claude task list --session-id $SESSION_ID --json
```

Output (JSON, one object):

```json
{
  "session_id": "21fab763-...",
  "tasks": [
    {"id": "1", "status": "completed", "subject": "...",
     "description": "...", "blocks": [], "blockedBy": []},
    {"id": "2", "status": "in_progress", "subject": "...",
     "description": "...", "blocks": [], "blockedBy": ["1"]}
  ]
}
```

Powered by the harness's authoritative in-memory store — never the
lagged transcript.

`--session-id` derived by the hook from
`basename "$transcript_path" .jsonl`. Default could be the current
session when run inside a Claude Code session, but hooks always pass
it explicitly.

## Alternative interfaces

In order of preference:

1. CLI as above. Stable, scriptable, parseable.
2. Stable env var (e.g. `CLAUDE_TASKS_DIR`) pointing at the on-disk
   store root, plus a documented contract for the per-task JSON
   schema.
3. Hook-input field `tasks_store_dir` populated by the harness in
   the JSON it pipes to hooks on stdin.

## Use cases

- `pre-commit-check-tasks.sh` (block `git commit` while in_progress
  task exists) — `superpowers-extended-cc`.
- `pre-task-blockedby-enforce.sh` (block `TaskUpdate(in_progress)`
  when blockers are open) — same.
- `post-task-complete-revalidate.sh` / `post-agent-return-validate.sh`
  / `stop-revalidate-user-gates.sh` (user-gate evidence enforcement) —
  same.
- General pattern: any hook gating an action on task state.

## Related

Companion bug filed at the `superpowers-extended-cc-marketplace` repo
documenting the transcript-lag failure in the marketplace hooks:
**<USER WILL ADD URL AFTER FILING ISSUE A>**.

Our local workaround spec + helper:
`docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md`
in our project repo.
