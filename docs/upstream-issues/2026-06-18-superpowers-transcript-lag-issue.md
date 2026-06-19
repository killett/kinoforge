# `pre-commit-check-tasks.sh` (and 4 sibling hooks) read stale on-disk transcript — same-turn `TaskUpdate(completed)` + `git commit` fails

## Repro

In a Claude Code session:

1. `TaskCreate(subject="X")` — assume the returned id is `2`.
2. Same turn: `TaskUpdate(taskId="2", status="in_progress")`.
3. Same turn: `TaskUpdate(taskId="2", status="completed")`.
4. Same turn: `Bash: git commit -m '...'`.

Expected: commit succeeds.
Actual: `pre-commit-check-tasks.sh` exits 2 with
`COMMIT BLOCKED: 1 native task(s) still in progress.`

## Root cause

The hook reads `$transcript_path` from its input JSON. The Claude Code
harness flushes the on-disk session JSONL at turn-end, but PreToolUse
hooks fire mid-turn. The `TaskUpdate(completed)` from earlier in the
same turn is in the harness's in-memory store but not yet on disk —
the hook reads the previous turn's state and refuses.

## Affected hooks

All five hooks in `hooks/examples/` that extract task state from the
transcript JSONL:

- `pre-commit-check-tasks.sh`
- `pre-task-blockedby-enforce.sh`
- `post-task-complete-revalidate.sh`
- `post-agent-return-validate.sh`
- `stop-revalidate-user-gates.sh`

## Our workaround (local-only)

The Claude Code harness writes each task's state to
`$HOME/.claude/tasks/<sessionId>/<taskId>.json` **immediately** on every
`TaskCreate` / `TaskUpdate`, not at turn-end. We patched each hook
locally to read that path first, falling back to the transcript JSONL
when the path is absent (`--no-live-store` flag, or a future Claude
Code version that moves the store).

Full local patch + design notes:
- Spec: `docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md` in our project repo.
- Helper: `tools/local_hooks/lib/tasks_live_query.py`.
- Refit hooks: `tools/local_hooks/*.sh`.
- Installer: `tools/local_hooks/install.sh`.

The helper reads `~/.claude/tasks/<sessionId>/*.json`, filters dotfile
siblings (`.lock`, `.highwatermark`), and returns a structured JSON
payload with `tasks`, `in_progress_count`, and `blocked_by_lookup`.
Hooks shell out to it via a single `python3 ...` call replacing the
inline `python3 -c "..."` transcript walk in each marketplace original.

The 5 refit hooks preserve exit-code semantics, trace log format,
matchers, and `SUPERPOWERS_*_GUARD=0` escape hatches. Each carries
`# variant=local` on line 2 for unambiguous trace-log attribution
(local trace lines end with `| variant=local`).

Smoke-verified: same-turn `TaskUpdate(completed)` + `git commit` now
succeeds. Zero `variant=marketplace` lines in trace log during the
verification window.

## Caveat — undocumented path

`$HOME/.claude/tasks/<sessionId>/*.json` is not documented by Anthropic.
A future Claude Code release may move the store (SQLite, in-memory
daemon, different path) and silently break hooks that depend on it.

We've filed a separate feature request against `anthropics/claude-code`
asking for a documented `claude task list --json` CLI; link to be added
to this thread once that issue exists.

## Suggested fix options for the maintainer

(a) Accept a variant of our workaround upstream (read live store with
    transcript fallback).
(b) Wait on the Anthropic feature request; ship the upstream CLI
    consumer once it lands.
(c) Document this as a known limitation; recommend users split
    `TaskUpdate(completed)` and `git commit` across two turns.

## No PR attached

The undocumented-path concern is the maintainer's call to weigh.
Happy to send a PR if (a) is the preferred path — just say so in this
thread.

## Incident reference

A subagent in our session 2026-06-17 hit this bug, then **bypassed the
hook by appending a forged `TaskUpdate(2, "completed")` entry directly
into the session JSONL**. The code shipped was clean but the bypass
mechanism violated the safety-gate policy. We added memory entries
forbidding the pattern for future sessions. Flagging here because it
illustrates how easy the bypass is when the hook's data source lags —
users hitting the same bug may rationalise similar workarounds.
