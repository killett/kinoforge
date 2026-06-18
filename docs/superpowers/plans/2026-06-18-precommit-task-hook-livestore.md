# Live-task-store reader for superpowers transcript-reading hooks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Python helper + 5 refit hook scripts that read live task state from `~/.claude/tasks/<sessionId>/*.json` with transcript-parse fallback, install them under `~/.claude/hooks/`, swap `~/.claude/settings.json` to use the local copies, and file two upstream issues documenting the lag bug and a feature request for a documented `claude task list --json` CLI.

**Architecture:** Canonical source for the helper + 5 hook scripts + installer lives in this repo at `tools/local_hooks/`. Tests for the helper run in `tests/hooks/`. An install script copies the files to `~/.claude/hooks/` and rewrites `~/.claude/settings.json` paths from the marketplace originals to the local copies. Marketplace files are never touched. Each hook gains a `# variant=local` trace marker for unambiguous attribution.

**Tech Stack:** Python 3.13 (stdlib only), bash, `jq`, GitHub CLI (`gh`) for issue filing. No new dependencies in `pixi.toml`.

**User decisions (already made):**
- Scope = all 5 transcript-reading hooks via shared helper (brainstorm Option 2).
- Upstream = file issues at superpowers marketplace + anthropics/claude-code; no PRs (brainstorm Options A + 3).
- Pre-emptive approval to execute plan tasks in this session on `main` branch, autonomously.
- Live-store path `~/.claude/tasks/<sessionId>/<taskId>.json` is undocumented but confirmed at brainstorm time; helper has transcript fallback for path drift.
- Marketplace repo confirmed: `github.com/pcvelz/superpowers`.

---

## File structure

| File | Responsibility | Action |
|------|---------------|--------|
| `tools/local_hooks/lib/tasks_live_query.py` | Canonical helper — reads live task store, falls back to transcript | Create |
| `tools/local_hooks/pre-commit-check-tasks.sh` | Refit — shells out to helper, blocks commit if any task in_progress | Create |
| `tools/local_hooks/pre-task-blockedby-enforce.sh` | Refit — blocks TaskUpdate(in_progress) when blockers not completed | Create |
| `tools/local_hooks/post-task-complete-revalidate.sh` | Refit — blocks close of user-gate without evidence | Create |
| `tools/local_hooks/post-agent-return-validate.sh` | Refit — blocks subagent return missing evidence-token axes | Create |
| `tools/local_hooks/stop-revalidate-user-gates.sh` | Refit — blocks Stop when closed user-gate lacks evidence | Create |
| `tools/local_hooks/install.sh` | Installer — copies files into `~/.claude/hooks/`, backs up + swaps `~/.claude/settings.json` | Create |
| `tools/local_hooks/README.md` | How the helper + hooks work, installation, uninstall | Create |
| `tests/hooks/__init__.py` | pytest package marker | Create |
| `tests/hooks/test_tasks_live_query.py` | TDD harness for the helper | Create |
| `docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md` | Issue A body — superpowers marketplace | Create |
| `docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md` | Issue B body — anthropics/claude-code | Create |
| `PROGRESS.md` | Parked queue item 2 closeout note | Modify |

13 files total. 6 source + 1 installer + 1 readme + 2 test files + 2 issue drafts + 1 PROGRESS update.

---

## Task 0: Helper + tests (TDD)

**Goal:** Ship `tools/local_hooks/lib/tasks_live_query.py` with 5 passing tests covering live-store read, transcript fallback, malformed-JSON skip, dir-missing fallback, and `--no-live-store` flag.

**Files:**
- Create: `tools/local_hooks/lib/tasks_live_query.py`
- Create: `tests/hooks/__init__.py`
- Create: `tests/hooks/test_tasks_live_query.py`

**Acceptance Criteria:**
- [ ] `python3 tools/local_hooks/lib/tasks_live_query.py --help` exits 0 with usage.
- [ ] All 5 test cases pass in `tests/hooks/test_tasks_live_query.py`.
- [ ] `ruff check tools/local_hooks/lib/ tests/hooks/` clean.
- [ ] `mypy tools/local_hooks/lib/tasks_live_query.py` clean.
- [ ] Helper output JSON contains keys `source`, `session_id`, `tasks`, `in_progress_count`, `blocked_by_lookup`.
- [ ] Helper exits 0 on every input (fail-open contract).

**Verify:**
```
pixi run -- pytest tests/hooks/test_tasks_live_query.py -v --no-header
```
Expected: 5 passed.

**Steps:**

- [ ] **Step 1: Create the test file** — `tests/hooks/__init__.py` empty, `tests/hooks/test_tasks_live_query.py` verbatim:

```python
"""Tests for the live-task-store reader helper at
tools/local_hooks/lib/tasks_live_query.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2] / "tools" / "local_hooks" / "lib" / "tasks_live_query.py"


def _run(args: list[str]) -> tuple[int, dict[str, object], str]:
    """Run the helper with args; return (returncode, parsed stdout JSON, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    payload: dict[str, object] = json.loads(proc.stdout) if proc.stdout else {}
    return proc.returncode, payload, proc.stderr


def _write_task(root: Path, session_id: str, tid: str, status: str, blocked_by: list[str] | None = None) -> None:
    """Write one task JSON into the live-store layout used by the harness."""
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.json").write_text(
        json.dumps(
            {
                "id": tid,
                "subject": f"Task {tid}",
                "description": f"Task {tid} description",
                "status": status,
                "blocks": [],
                "blockedBy": blocked_by or [],
            }
        )
    )


def test_live_store_read_returns_in_progress_count(tmp_path: Path) -> None:
    """Three tasks in the live store, one in_progress → in_progress_count == 1.

    Bug catch: a future edit that filters on status-equality with a typo
    ("inprogress" vs "in_progress") would silently report 0 and unblock
    a commit the user explicitly gated.
    """
    sid = "s-001"
    _write_task(tmp_path, sid, "1", "completed")
    _write_task(tmp_path, sid, "2", "in_progress", blocked_by=["1"])
    _write_task(tmp_path, sid, "3", "pending")
    rc, payload, _ = _run(["--session-id", sid, "--root", str(tmp_path)])
    assert rc == 0
    assert payload["source"] == "live-store"
    assert payload["in_progress_count"] == 1
    assert payload["session_id"] == sid
    tasks_by_id = {t["id"]: t for t in payload["tasks"]}  # type: ignore[union-attr]
    assert tasks_by_id["2"]["status"] == "in_progress"
    assert payload["blocked_by_lookup"] == {"2": ["1"]}


def test_live_store_dotfiles_excluded(tmp_path: Path) -> None:
    """The harness writes .lock and .highwatermark alongside task JSONs.
    They MUST NOT be loaded as tasks.

    Bug catch: a glob that picks up .lock returns binary garbage and
    json.loads raises — fail-open then reports zero tasks and unblocks
    a commit that should have been gated.
    """
    sid = "s-dot"
    d = tmp_path / sid
    d.mkdir()
    (d / ".lock").write_text("")
    (d / ".highwatermark").write_text("3")
    _write_task(tmp_path, sid, "1", "in_progress")
    rc, payload, _ = _run(["--session-id", sid, "--root", str(tmp_path)])
    assert rc == 0
    assert payload["source"] == "live-store"
    assert payload["in_progress_count"] == 1
    assert len(payload["tasks"]) == 1  # type: ignore[arg-type]


def test_live_store_missing_falls_back_to_transcript(tmp_path: Path) -> None:
    """Live store directory absent; a transcript with TaskCreate +
    TaskUpdate is provided → fallback parse counts in_progress correctly
    and source == 'transcript-fallback'.

    Bug catch: a future edit that always returns 0 when the live store
    is empty would silently drop the transcript-parse safety net,
    re-introducing the bug we just patched out.
    """
    sid = "s-fallback"
    transcript = tmp_path / f"{sid}.jsonl"
    # Two TaskCreate + a TaskUpdate to in_progress on task 2.
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TaskCreate", "input": {"subject": "A"}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TaskCreate", "input": {"subject": "B"}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "TaskUpdate", "input": {"taskId": "2", "status": "in_progress"}},
        ]}},
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines))
    rc, payload, _ = _run([
        "--session-id", sid,
        "--root", str(tmp_path),
        "--transcript", str(transcript),
    ])
    assert rc == 0
    assert payload["source"] == "transcript-fallback"
    assert payload["in_progress_count"] == 1


def test_malformed_json_in_one_task_file_is_skipped(tmp_path: Path) -> None:
    """One corrupt task JSON must not poison the rest of the read.

    Bug catch: a future edit that aborts the whole read on first
    JSONDecodeError would fail-open and unblock a commit despite a
    valid sibling task still being in_progress.
    """
    sid = "s-malformed"
    d = tmp_path / sid
    d.mkdir()
    (d / "1.json").write_text("{not valid json")
    _write_task(tmp_path, sid, "2", "in_progress")
    rc, payload, _ = _run(["--session-id", sid, "--root", str(tmp_path)])
    assert rc == 0
    assert payload["source"] == "live-store"
    assert payload["in_progress_count"] == 1
    assert len(payload["tasks"]) == 1  # type: ignore[arg-type]


def test_no_live_store_flag_forces_transcript_only(tmp_path: Path) -> None:
    """--no-live-store flag MUST skip the live store even when it
    exists. Used for testing the fallback path without uninstalling
    the live store.

    Bug catch: a future edit that ignores --no-live-store would hide
    bugs in the fallback parser since the live store always wins.
    """
    sid = "s-noflag"
    _write_task(tmp_path, sid, "1", "in_progress")  # would say in_progress=1
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": []}}))
    rc, payload, _ = _run([
        "--session-id", sid,
        "--root", str(tmp_path),
        "--transcript", str(transcript),
        "--no-live-store",
    ])
    assert rc == 0
    assert payload["source"] == "transcript-fallback"
    assert payload["in_progress_count"] == 0
```

- [ ] **Step 2: Run tests — confirm RED**

```
pixi run -- pytest tests/hooks/test_tasks_live_query.py -v --no-header
```
Expected: 5 errors / failures with `FileNotFoundError` (helper does not exist yet).

- [ ] **Step 3: Create the helper** — `tools/local_hooks/lib/tasks_live_query.py`:

```python
#!/usr/bin/env python3
"""Live task-state reader for superpowers PreToolUse/PostToolUse/Stop hooks.

Reads the Claude Code harness's live on-disk task store at
``$HOME/.claude/tasks/<sessionId>/<taskId>.json`` — written immediately on
every ``TaskCreate`` / ``TaskUpdate`` call (unlike the session JSONL
transcript, which the harness flushes only at turn-end and which therefore
lags one turn behind same-turn TaskUpdate calls).

Falls back to a transcript-parse equivalent to the marketplace original
when the live store is missing, when ``--no-live-store`` is passed, or
when every live-store file is malformed.

Fail-open contract: any uncaught exception emits a no-tasks JSON
payload to stdout and a traceback to stderr; exit code is always 0.
Hooks default to "allow" on an empty task list, so a failed query
NEVER blocks legitimate work.

Path dependency: ``~/.claude/tasks/<sessionId>/`` is currently
undocumented; the fallback exists to keep the helper functional if
Anthropic moves the store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _empty_payload(source: str, session_id: str) -> dict[str, Any]:
    return {
        "source": source,
        "session_id": session_id,
        "tasks": [],
        "in_progress_count": 0,
        "blocked_by_lookup": {},
    }


def _read_live_store(root: Path, session_id: str) -> list[dict[str, Any]] | None:
    """Return the list of task dicts from the live store, or None if dir absent.

    Files starting with ``.`` (e.g. ``.lock``, ``.highwatermark``) are
    excluded. JSONDecodeError on any individual file skips that file
    and continues with the rest.
    """
    d = root / session_id
    if not d.is_dir():
        return None
    out: list[dict[str, Any]] = []
    for entry in sorted(d.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        try:
            out.append(json.loads(entry.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def _read_transcript_fallback(transcript: Path) -> list[dict[str, Any]]:
    """Parse the marketplace-style transcript JSONL into the same task-dict
    shape as the live store.

    Logic mirrors the original ``pre-commit-check-tasks.sh`` inline
    python: each ``TaskCreate`` increments the next-id counter; each
    ``TaskUpdate(taskId, status)`` overwrites that id's status. Tasks
    have no ``description`` here (transcript-parse can't recover the
    description from a TaskCreate without re-inspecting the message),
    so the returned dicts carry empty-string defaults.
    """
    tasks: dict[str, dict[str, Any]] = {}
    next_id = 1
    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            name = c.get("name", "")
            inp = c.get("input", {})
            if not isinstance(inp, dict):
                continue
            if name == "TaskCreate":
                tid = str(next_id)
                tasks[tid] = {
                    "id": tid,
                    "subject": str(inp.get("subject", "")),
                    "description": str(inp.get("description", "")),
                    "status": "pending",
                    "blocks": [],
                    "blockedBy": [],
                }
                next_id += 1
            elif name == "TaskUpdate":
                tid = str(inp.get("taskId", ""))
                if not tid:
                    continue
                status = inp.get("status", "")
                if tid not in tasks:
                    tasks[tid] = {
                        "id": tid,
                        "subject": "",
                        "description": "",
                        "status": "",
                        "blocks": [],
                        "blockedBy": [],
                    }
                if status:
                    tasks[tid]["status"] = str(status)
                try:
                    if int(tid) >= next_id:
                        next_id = int(tid) + 1
                except ValueError:
                    pass
    return list(tasks.values())


def _build_payload(source: str, session_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
    blocked_by_lookup = {
        str(t.get("id", "")): list(t.get("blockedBy", []) or [])
        for t in tasks
        if t.get("blockedBy")
    }
    return {
        "source": source,
        "session_id": session_id,
        "tasks": tasks,
        "in_progress_count": in_progress,
        "blocked_by_lookup": blocked_by_lookup,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", default=None)
    parser.add_argument(
        "--no-live-store",
        action="store_true",
        help="Skip the live-store read; force transcript fallback.",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("CLAUDE_TASKS_ROOT", str(Path.home() / ".claude" / "tasks")),
        help="Override the live-store root (default: $CLAUDE_TASKS_ROOT or ~/.claude/tasks).",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on bad args. Preserve --help (exit 0) but
        # convert bad-arg exits into the fail-open empty payload at
        # exit code 0 so a typo in the bash caller never blocks.
        if exc.code == 0:
            return 0
        json.dump(_empty_payload("argparse-error", ""), sys.stdout)
        return 0

    try:
        root = Path(args.root)
        tasks: list[dict[str, Any]] | None = None
        if not args.no_live_store:
            tasks = _read_live_store(root, args.session_id)
        if tasks is None or len(tasks) == 0:
            if args.transcript:
                fallback_tasks = _read_transcript_fallback(Path(args.transcript))
                json.dump(
                    _build_payload("transcript-fallback", args.session_id, fallback_tasks),
                    sys.stdout,
                )
                return 0
            json.dump(_empty_payload("transcript-fallback", args.session_id), sys.stdout)
            return 0
        json.dump(_build_payload("live-store", args.session_id, tasks), sys.stdout)
        return 0
    except Exception:  # noqa: BLE001 — fail-open is the contract
        traceback.print_exc(file=sys.stderr)
        json.dump(_empty_payload("fallback-failed", args.session_id), sys.stdout)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests — confirm GREEN**

```
pixi run -- pytest tests/hooks/test_tasks_live_query.py -v --no-header
```
Expected: 5 passed.

- [ ] **Step 5: Lint + typecheck**

```
pixi run -- ruff check tools/local_hooks/lib/ tests/hooks/
pixi run -- ruff format --check tools/local_hooks/lib/ tests/hooks/
pixi run -- mypy tools/local_hooks/lib/tasks_live_query.py
```
All three clean.

- [ ] **Step 6: Commit**

```bash
git add tools/local_hooks/lib/tasks_live_query.py tests/hooks/__init__.py tests/hooks/test_tasks_live_query.py
git commit -m "$(cat <<'EOF'
feat(hooks): tasks_live_query.py — live-store reader for superpowers hooks

Canonical helper for the 5 transcript-reading hooks in
~/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/.
Reads the Claude Code harness's live on-disk task store at
~/.claude/tasks/<sessionId>/<taskId>.json which is written immediately
on every TaskCreate/TaskUpdate — unlike the session JSONL transcript
the marketplace hooks parse, which lags one turn behind.

Fails open: any uncaught exception emits a no-tasks payload at exit 0
so a helper failure never blocks legitimate work.

Falls back to the marketplace's transcript-parse logic when the live
store is absent or --no-live-store is passed.

5 TDD tests covering live-store read, dotfile exclusion, malformed-JSON
skip, transcript fallback, and --no-live-store flag.

Spec: docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 1: Refit all 5 marketplace hooks

**Goal:** Create local-variant bash hook scripts at `tools/local_hooks/*.sh` that delegate task-state extraction to `tasks_live_query.py` while preserving each hook's matcher, exit-code semantics, trace logging, and env-var escape hatches.

**Files:**
- Create: `tools/local_hooks/pre-commit-check-tasks.sh`
- Create: `tools/local_hooks/pre-task-blockedby-enforce.sh`
- Create: `tools/local_hooks/post-task-complete-revalidate.sh`
- Create: `tools/local_hooks/post-agent-return-validate.sh`
- Create: `tools/local_hooks/stop-revalidate-user-gates.sh`

**Acceptance Criteria:**
- [ ] All 5 scripts start with `#!/usr/bin/env bash` and `# variant=local`.
- [ ] Each script shells out to `python3 "$HELPER_ROOT/lib/tasks_live_query.py" --session-id "$SESSION_ID" --transcript "$TRANSCRIPT_PATH"` for task-state extraction.
- [ ] Each script preserves the marketplace original's exit-code semantics (0 allow, 2 block, JSON ALLOW shape) and trace-log format.
- [ ] `bash -n tools/local_hooks/*.sh` exits 0 (syntax check on all 5).
- [ ] No `python3 -c "..."` inline task-state parsing remains in any of the 5 scripts.

**Verify:**
```
for f in tools/local_hooks/*.sh; do bash -n "$f" || echo "SYNTAX FAIL: $f"; done
grep -l "python3 -c" tools/local_hooks/*.sh && echo "INLINE PYTHON FOUND" || echo OK
grep -L "variant=local" tools/local_hooks/*.sh
```
Expected: no SYNTAX FAIL output, `OK`, no missing-marker output.

**Steps:**

- [ ] **Step 1: Create `tools/local_hooks/pre-commit-check-tasks.sh`**

```bash
#!/usr/bin/env bash
# variant=local
# PreToolUse hook: block git commit while a native task is in progress.
# Local refit of superpowers-extended-cc marketplace original — delegates
# task-state extraction to tools/local_hooks/lib/tasks_live_query.py
# which reads the Claude Code harness's live on-disk task store at
# ~/.claude/tasks/<sessionId>/<taskId>.json instead of the lagging
# session JSONL transcript. See:
#   docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md

INPUT=$(cat)
ALLOW='{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
[[ "$TOOL_NAME" != "Bash" ]] && echo "$ALLOW" && exit 0

COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
echo "$COMMAND" | grep -qE '(^|[;&|(]|&&|\|\|)[[:space:]]*git[[:space:]]+commit([[:space:]]|[;&|)]|$)' || { echo "$ALLOW"; exit 0; }

TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
[[ -z "$TRANSCRIPT_PATH" || ! -f "$TRANSCRIPT_PATH" ]] && echo "$ALLOW" && exit 0

SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)

if [[ -z "$RESULT" ]]; then
    echo "$ALLOW"; exit 0
fi

OPEN_TASKS=$(echo "$RESULT" | jq -r '.in_progress_count // 0')

if [[ "$OPEN_TASKS" -gt 0 ]]; then
    SOURCE=$(echo "$RESULT" | jq -r '.source // "?"')
    echo "COMMIT BLOCKED: $OPEN_TASKS native task(s) still in progress (source=$SOURCE, variant=local). Finish the current task before committing." >&2
    exit 2
fi

echo "$ALLOW"
exit 0
```

- [ ] **Step 2: Create `tools/local_hooks/pre-task-blockedby-enforce.sh`**

Copy `pre-task-blockedby-enforce.sh` from the marketplace verbatim, then replace ONLY the inline `python3 -c "..."` block that builds the `{id → {status, blockedBy}}` map with a `tasks_live_query.py` invocation. The hook's trace logic, env-var escape hatch, refusal logic, exit codes stay identical.

Concretely: after `TRANSCRIPT_PATH=$(...)` extraction, insert:

```bash
SESSION_ID=$(basename "$TRANSCRIPT_PATH" .jsonl)
RESULT=$(python3 "$HELPER_ROOT/lib/tasks_live_query.py" \
    --session-id "$SESSION_ID" \
    --transcript "$TRANSCRIPT_PATH" 2>/dev/null)
[[ -z "$RESULT" ]] && { echo "$ALLOW"; exit 0; }

# Build STATUS_OF (id → status) and BLOCKED_BY (id → comma list) from RESULT.
STATUS_OF=$(echo "$RESULT" | jq -r '.tasks[] | "\(.id):\(.status)"')
BLOCKED_BY=$(echo "$RESULT" | jq -r '.blocked_by_lookup // {} | to_entries[] | "\(.key):\(.value | join(","))"')
```

Then the rest of the hook's refusal logic reads `STATUS_OF` / `BLOCKED_BY` instead of the python-built dict. Add the `# variant=local` marker on line 2 and add `HELPER_ROOT="${KINOFORGE_LOCAL_HOOKS_DIR:-$HOME/.claude/hooks}"` near the top.

Full source to write — copy `cat ~/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/pre-task-blockedby-enforce.sh` as the starting point, apply the changes above, save to `tools/local_hooks/pre-task-blockedby-enforce.sh`. After writing, run `bash -n tools/local_hooks/pre-task-blockedby-enforce.sh` to syntax-check; if it fails, re-read the marketplace source and patch the refusal logic to match the new variable names.

- [ ] **Step 3: Create `tools/local_hooks/post-task-complete-revalidate.sh`**

Same pattern as Step 2: copy the marketplace original verbatim, add `# variant=local` marker + `HELPER_ROOT` env var, replace the inline `python3 -c "..."` task-state extraction with a `tasks_live_query.py` call that returns the closed task's `description`. The json:metadata fence parse + evidence-pattern sweep + trace logic stay verbatim.

The closed task's description comes from `RESULT`:

```bash
CLOSED_TASK_ID=$(echo "$INPUT" | jq -r '.tool_input.taskId // empty')
DESCRIPTION=$(echo "$RESULT" | jq -r ".tasks[] | select(.id == \"$CLOSED_TASK_ID\") | .description")
```

- [ ] **Step 4: Create `tools/local_hooks/post-agent-return-validate.sh`**

Same pattern. The marketplace original walks the transcript to find "the currently in_progress task"; replace with:

```bash
IN_PROGRESS=$(echo "$RESULT" | jq -r '.tasks[] | select(.status == "in_progress") | "\(.id)\t\(.description)"' | head -1)
[[ -z "$IN_PROGRESS" ]] && { echo "$ALLOW"; exit 0; }
TASK_ID=$(echo "$IN_PROGRESS" | cut -f1)
DESCRIPTION=$(echo "$IN_PROGRESS" | cut -f2-)
```

The subagent-tool_result scan continues to walk the transcript (transcript-text query, not task-state).

- [ ] **Step 5: Create `tools/local_hooks/stop-revalidate-user-gates.sh`**

Same pattern. The marketplace original walks the transcript for completed-status TaskUpdates AND for completion-keywords; only the FIRST half (completed-status enumeration) moves to the helper:

```bash
COMPLETED_TASKS=$(echo "$RESULT" | jq -r '.tasks[] | select(.status == "completed") | "\(.id)\t\(.description)"')
```

The completion-keyword scan + evidence-pattern scan on the last assistant message stay on transcript.

- [ ] **Step 6: Make all 5 hooks executable + syntax-check**

```bash
chmod +x tools/local_hooks/*.sh
for f in tools/local_hooks/*.sh; do bash -n "$f" || echo "SYNTAX FAIL: $f"; done
grep -l "python3 -c" tools/local_hooks/*.sh && echo "INLINE PYTHON FOUND" || echo OK
grep -L "variant=local" tools/local_hooks/*.sh
```
Expected: no SYNTAX FAIL lines, `OK`, no missing-marker output.

- [ ] **Step 7: Commit**

```bash
git add tools/local_hooks/*.sh
git commit -m "$(cat <<'EOF'
feat(hooks): refit 5 marketplace hooks to use tasks_live_query.py

Local-variant copies of:
  - pre-commit-check-tasks.sh
  - pre-task-blockedby-enforce.sh
  - post-task-complete-revalidate.sh
  - post-agent-return-validate.sh
  - stop-revalidate-user-gates.sh

Each hook keeps its exit-code semantics, trace log format, matcher,
and env-var escape hatch. Only the task-state extraction moves —
inline 'python3 -c "..."' parsing of the lagging transcript is
replaced by a single shell-out to tasks_live_query.py.

Each script carries '# variant=local' on line 2 for trace-log
attribution.

Spec: docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Install script + executable installation

**Goal:** Ship `tools/local_hooks/install.sh` which copies the canonical helper + 5 hooks + a README into `~/.claude/hooks/`, backs up `~/.claude/settings.json`, and rewrites the 5 hook command paths from the marketplace to the local copies. Then run it.

**Files:**
- Create: `tools/local_hooks/install.sh`
- Create: `tools/local_hooks/README.md`
- Modify: `~/.claude/settings.json` (via the install script)
- Create (via install script): `~/.claude/settings.json.pre-hook-swap-2026-06-18.bak`, `~/.claude/hooks/lib/tasks_live_query.py`, `~/.claude/hooks/*.sh` (5 files), `~/.claude/hooks/lib/README.md`

**Acceptance Criteria:**
- [ ] `bash tools/local_hooks/install.sh --dry-run` prints the actions it would take and exits 0 without modifying anything.
- [ ] `bash tools/local_hooks/install.sh` exits 0 on a clean run.
- [ ] After install, `~/.claude/hooks/lib/tasks_live_query.py` exists and `python3 ~/.claude/hooks/lib/tasks_live_query.py --help` exits 0.
- [ ] After install, all 5 `~/.claude/hooks/<hook>.sh` files exist, are executable, and `bash -n` clean.
- [ ] After install, `~/.claude/settings.json.pre-hook-swap-2026-06-18.bak` exists and is byte-identical to the pre-install settings.json.
- [ ] After install, `grep -c 'plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/\(pre-commit-check-tasks\|pre-task-blockedby-enforce\|post-task-complete-revalidate\|post-agent-return-validate\|stop-revalidate-user-gates\)' ~/.claude/settings.json` returns `0` — every reference to the 5 marketplace hooks is gone.
- [ ] After install, `grep -c '$HOME/.claude/hooks/\(pre-commit-check-tasks\|pre-task-blockedby-enforce\|post-task-complete-revalidate\|post-agent-return-validate\|stop-revalidate-user-gates\)\.sh' ~/.claude/settings.json` returns `5` — every replacement landed.
- [ ] `python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))"` exits 0 — settings.json is still valid JSON.

**Verify:**
```
bash tools/local_hooks/install.sh --dry-run
bash tools/local_hooks/install.sh
ls -la ~/.claude/hooks/lib/tasks_live_query.py ~/.claude/hooks/*.sh ~/.claude/settings.json.pre-hook-swap-2026-06-18.bak
python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))" && echo "JSON OK"
```
Expected: dry-run prints actions; live install exits 0; all 7 install-artifact paths print; JSON OK.

**Steps:**

- [ ] **Step 1: Create `tools/local_hooks/install.sh`**

```bash
#!/usr/bin/env bash
# Install the live-task-store reader + 5 refit hooks into ~/.claude/hooks/
# and swap ~/.claude/settings.json to use the local copies.
#
# Usage:
#   bash tools/local_hooks/install.sh            # live install
#   bash tools/local_hooks/install.sh --dry-run  # print intended actions only
#
# Idempotent: re-running on a system that's already installed copies
# the (possibly updated) sources but does NOT make a second backup
# (the first one stays as the original-marketplace reference).
#
# Uninstall: cp $HOME/.claude/settings.json.pre-hook-swap-2026-06-18.bak \
#                 $HOME/.claude/settings.json

set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SRC="$(cd "$(dirname "$0")" && pwd)"
DST="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
BACKUP="$HOME/.claude/settings.json.pre-hook-swap-2026-06-18.bak"

HOOKS=(
    pre-commit-check-tasks.sh
    pre-task-blockedby-enforce.sh
    post-task-complete-revalidate.sh
    post-agent-return-validate.sh
    stop-revalidate-user-gates.sh
)

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

echo "Source dir : $SRC"
echo "Dest dir   : $DST"
echo "Settings   : $SETTINGS"
echo "Backup path: $BACKUP"
echo

# 1. Create destination dirs.
run "mkdir -p $DST/lib"

# 2. Copy helper + README.
run "cp $SRC/lib/tasks_live_query.py $DST/lib/tasks_live_query.py"
run "cp $SRC/README.md $DST/lib/README.md"

# 3. Copy + chmod each hook.
for hook in "${HOOKS[@]}"; do
    run "cp $SRC/$hook $DST/$hook"
    run "chmod +x $DST/$hook"
done

# 4. Back up settings.json (only if backup doesn't already exist).
if [[ ! -f "$BACKUP" ]]; then
    run "cp $SETTINGS $BACKUP"
else
    echo "Backup already exists at $BACKUP — preserving original."
fi

# 5. Rewrite settings.json paths.
# Use python for safe JSON edit instead of sed.
PY_REWRITE=$(cat <<'PY'
import json, os, sys
from pathlib import Path

settings_path = Path(os.environ["SETTINGS"])
home = os.environ["HOME"]
hooks = os.environ["HOOKS"].split()
data = json.loads(settings_path.read_text())

marketplace_prefix = "/home/claudeuser/.claude/plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples"

def walk(node):
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str):
            for hook in hooks:
                old = f"bash {marketplace_prefix}/{hook}"
                new = f"bash {home}/.claude/hooks/{hook}"
                if cmd == old:
                    node["command"] = new
                    return
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)

walk(data)
settings_path.write_text(json.dumps(data, indent=2) + "\n")
PY
)

if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: would rewrite hook command paths in $SETTINGS"
else
    SETTINGS="$SETTINGS" HOOKS="${HOOKS[*]}" python3 -c "$PY_REWRITE"
fi

echo
echo "Install complete. Verify with:"
echo "  python3 $DST/lib/tasks_live_query.py --help"
echo "  grep -c '\\$HOME/.claude/hooks' $SETTINGS"
```

- [ ] **Step 2: Create `tools/local_hooks/README.md`**

```markdown
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
```

- [ ] **Step 3: Dry-run install**

```bash
bash tools/local_hooks/install.sh --dry-run
```
Expected: prints "DRY-RUN: ..." lines for each action; exits 0; nothing on disk changes.

- [ ] **Step 4: Live install**

```bash
bash tools/local_hooks/install.sh
```
Expected: exits 0; backup file present; hook files copied; settings.json rewritten.

- [ ] **Step 5: Verify install artifacts**

```bash
ls -la ~/.claude/hooks/lib/tasks_live_query.py ~/.claude/hooks/*.sh ~/.claude/settings.json.pre-hook-swap-2026-06-18.bak
python3 -c "import json; json.load(open('$HOME/.claude/settings.json'))" && echo "JSON OK"
grep -c 'plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/pre-commit-check-tasks.sh\|plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/pre-task-blockedby-enforce.sh\|plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/post-task-complete-revalidate.sh\|plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/post-agent-return-validate.sh\|plugins/marketplaces/superpowers-extended-cc-marketplace/hooks/examples/stop-revalidate-user-gates.sh' ~/.claude/settings.json
grep -c '\.claude/hooks/pre-commit-check-tasks\|\.claude/hooks/pre-task-blockedby-enforce\|\.claude/hooks/post-task-complete-revalidate\|\.claude/hooks/post-agent-return-validate\|\.claude/hooks/stop-revalidate-user-gates' ~/.claude/settings.json
```
Expected: paths all exist; JSON OK; first grep returns 0; second grep returns 5.

- [ ] **Step 6: Commit the installer + README**

```bash
git add tools/local_hooks/install.sh tools/local_hooks/README.md
git commit -m "$(cat <<'EOF'
feat(hooks): install script + README for local hook refit

bash tools/local_hooks/install.sh copies the helper + 5 hooks into
~/.claude/hooks/ and rewrites the 5 hook command paths in
~/.claude/settings.json. Idempotent. Dry-run mode for previewing
actions. settings.json edited via python's json module (no sed)
so the JSON stays valid.

Backup at ~/.claude/settings.json.pre-hook-swap-2026-06-18.bak.
Uninstall is a single cp command back from the backup.

README documents install, uninstall, file inventory, and the
undocumented-path limitation.

Spec: docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Smoke-verify each hook fires the local variant

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Confirm each of the 5 installed hooks fires from `~/.claude/hooks/` (not the marketplace) by triggering each one's matcher and capturing the trace-log line carrying `variant=local`.

**Files:**
- Modify: none (read-only verification).

**Acceptance Criteria:**
- [ ] `/tmp/claude-hooks/user-gate-trace.log` contains at least one `variant=local` line per hook in the 5 minutes following the smoke trigger (5 distinct hooks × ≥1 line each = 5 lines).
- [ ] The same-turn TaskUpdate(completed) + `git commit` scenario from the 2026-06-17 incident now succeeds (commit completes without `COMMIT BLOCKED:` stderr).
- [ ] No hook prints a `variant=marketplace` line in the same window (marketplace copies are no longer wired in).

**Verify:**
```
tail -200 /tmp/claude-hooks/user-gate-trace.log | grep "variant=local" | head -20
tail -200 /tmp/claude-hooks/user-gate-trace.log | grep "variant=marketplace" || echo "no marketplace lines"
```
Expected: ≥5 distinct lines on the first grep; "no marketplace lines" on the second.

**Steps:**

- [ ] **Step 1: Trigger `pre-commit-check-tasks.sh`**

Approach: create an in-progress task, then attempt a commit. The hook should refuse with `COMMIT BLOCKED:` stderr + the `variant=local` trace line.

```bash
# In a new TaskCreate / TaskUpdate(in_progress) flow within this session,
# the coordinator marks a temporary canary task in_progress, attempts a
# dry-run commit, captures the trace line, then marks the task completed.
```

(The execution subagent runs this; the trace line + refused commit are the evidence.)

- [ ] **Step 2: Trigger `pre-task-blockedby-enforce.sh`**

Approach: TaskCreate two tasks where task B is blockedBy task A. Attempt TaskUpdate(B, in_progress) while A is still pending. Hook should refuse + trace line.

- [ ] **Step 3: Trigger `post-task-complete-revalidate.sh`**

Approach: TaskCreate a task with `userGate: true` in its description's json:metadata fence. TaskUpdate(completed) without evidence. Hook should block.

- [ ] **Step 4: Trigger `post-agent-return-validate.sh`**

Approach: TaskCreate a task with `requireEvidenceTokens` in metadata. TaskUpdate(in_progress). Dispatch an Agent that returns text missing the tokens. Hook should block.

- [ ] **Step 5: Trigger `stop-revalidate-user-gates.sh`**

Stop hook fires on session-stop. Requires a closed user-gate without evidence. Reuse Task 3's setup; let the Stop signal arrive.

- [ ] **Step 6: Same-turn TaskUpdate+commit regression check**

The exact failure mode from 2026-06-17. Create a task, mark it in_progress, do some work, mark it completed in the same turn as a `git commit`. With the live store reader installed, the commit succeeds.

- [ ] **Step 7: Capture evidence**

After Steps 1-6, run:

```bash
tail -500 /tmp/claude-hooks/user-gate-trace.log | grep -E "variant=local|variant=marketplace" | tee /tmp/local-hooks-smoke.log
```

Append the captured lines to the task close report. **Required tokens in the close window:** `variant=local` AND `no-marketplace` (the latter being your literal stamp confirming the second grep was empty).

```json:metadata
{"userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["variant=local"], ["no-marketplace"]]}
```

---

## Task 4: File Issue A (superpowers marketplace)

**Goal:** Open a GitHub issue at `pcvelz/superpowers` documenting the transcript-lag bug across 5 hooks, with repro, root cause, our local workaround, and a recommendation that the maintainer either (a) accept the workaround approach or (b) wait on the Anthropic feature request before fixing.

**Files:**
- Create: `docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md` (issue body, committed for provenance)
- External: GitHub issue at `pcvelz/superpowers`

**Acceptance Criteria:**
- [ ] `docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md` exists with the full issue body.
- [ ] Issue body covers: repro, root cause, 5 affected hooks, local workaround, undocumented-path caveat, suggested fix options, explicit "no PR attached".
- [ ] Issue is filed at `pcvelz/superpowers` via `gh issue create`.
- [ ] Returned issue URL is captured for the PROGRESS.md closeout (Task 6).

**Verify:**
```
cat docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md | wc -l
gh issue list -R pcvelz/superpowers --author "@me" --search "transcript" --json url,title,createdAt
```
Expected: file ≥30 lines; gh returns the new issue at top.

**Steps:**

- [ ] **Step 1: Write the issue body** to `docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md`. Content sections (write verbatim with concrete details):

````markdown
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

All five hooks in
`hooks/examples/` that extract task state from the transcript JSONL:

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
- Spec: `docs/superpowers/specs/2026-06-18-precommit-task-hook-livestore-design.md`
  in our project repo at <link to be inserted once we publish>.
- Helper: `tools/local_hooks/lib/tasks_live_query.py`.

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
forbidding the pattern for future sessions
([`feedback_never_bypass_safety_hooks`](https://...),
[`reference_precommit_check_tasks_transcript_lag`](https://...)).
Flagging here because it illustrates how easy the bypass is when the
hook's data source lags — users hitting the same bug may rationalise
similar workarounds.
````

- [ ] **Step 2: File the issue**

```bash
gh issue create -R pcvelz/superpowers \
    --title "pre-commit-check-tasks.sh (and 4 sibling hooks) read stale on-disk transcript — same-turn TaskUpdate(completed) + git commit fails" \
    --body-file docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md \
    > /tmp/issue-a-url.txt
cat /tmp/issue-a-url.txt
```
Expected: stdout shows the new issue URL (e.g. `https://github.com/pcvelz/superpowers/issues/N`).

- [ ] **Step 3: Commit the issue body for provenance**

```bash
git add docs/upstream-issues/2026-06-18-superpowers-transcript-lag-issue.md
git commit -m "docs(upstream): issue body for superpowers transcript-lag bug

Filed at: $(cat /tmp/issue-a-url.txt)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: File Issue B (anthropics/claude-code)

**Goal:** Open a feature-request issue at `anthropics/claude-code` asking for a documented `claude task list --json` CLI so hooks can query the live task store without depending on an undocumented filesystem path.

**Files:**
- Create: `docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md` (issue body, committed for provenance)
- External: GitHub issue at `anthropics/claude-code`

**Acceptance Criteria:**
- [ ] `docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md` exists with the full issue body.
- [ ] Issue body covers: context, current workaround, proposed CLI shape, alternative interfaces, use cases (the 5 superpowers hooks + general pattern).
- [ ] Issue is filed at `anthropics/claude-code` via `gh issue create`.
- [ ] Issue URL captured.

**Verify:**
```
cat docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md | wc -l
gh issue list -R anthropics/claude-code --author "@me" --search "task list" --json url,title,createdAt
```
Expected: file ≥40 lines; gh returns the new issue.

**Steps:**

- [ ] **Step 1: Write the issue body** to `docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md`:

````markdown
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

Filed companion bug at `pcvelz/superpowers` documenting the
transcript-lag failure in the marketplace hooks:
<insert Issue A URL here>.
````

- [ ] **Step 2: File the issue**

```bash
gh issue create -R anthropics/claude-code \
    --title "Feature request: documented CLI for hook scripts to query the live task store" \
    --body-file docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md \
    > /tmp/issue-b-url.txt
cat /tmp/issue-b-url.txt
```
Expected: stdout shows the new issue URL.

- [ ] **Step 3: Commit the issue body**

```bash
git add docs/upstream-issues/2026-06-18-anthropics-tasks-cli-feature-request.md
git commit -m "docs(upstream): feature request body for anthropics/claude-code tasks CLI

Filed at: $(cat /tmp/issue-b-url.txt)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: PROGRESS.md closeout

**Goal:** Update PROGRESS.md "Parked queue" item 2 to record that the local patch shipped, with both upstream issue URLs.

**Files:**
- Modify: `PROGRESS.md` (the parked-queue block on lines ~40-55)

**Acceptance Criteria:**
- [ ] Item 2 in the "Parked / do-not-forget queue" section no longer says "Brainstorm pending".
- [ ] Item 2 carries `Local patch shipped 2026-06-18` plus URLs for Issue A and Issue B.
- [ ] No other PROGRESS.md content changed.

**Verify:**
```
grep -n "Hook patch shipped\|Brainstorm pending" PROGRESS.md | head
```
Expected: "Hook patch shipped" matches; "Brainstorm pending" does not.

**Steps:**

- [ ] **Step 1: Edit the Parked queue item 2 block**

Replace the line ending in `operator has not yet authorised the workstream as of this anchor.` with:

```
   open an upstream issue. Local patch shipped 2026-06-18 — helper at
   `tools/local_hooks/lib/tasks_live_query.py`, 5 refit hooks at
   `tools/local_hooks/*.sh`, install script at `tools/local_hooks/install.sh`.
   Upstream Issue A (superpowers): <ISSUE_A_URL>.
   Upstream Issue B (anthropics/claude-code feature request): <ISSUE_B_URL>.
   Memory ref [[reference-precommit-check-tasks-transcript-lag]] still applies
   for sessions without the local patch installed.
```

Substitute the real URLs captured in Tasks 4 and 5.

- [ ] **Step 2: Commit**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): close out Parked queue item 2 — hook patch shipped

Replaces the "Brainstorm pending" placeholder under Parked queue
item 2 with a shipped-confirmation referencing the new
tools/local_hooks/ tree, the install script, and both upstream
issue URLs (superpowers marketplace + anthropics/claude-code
feature request).

Memory ref reference_precommit_check_tasks_transcript_lag stays
applicable for sessions on other machines that haven't run the
install script.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-review

**Spec coverage:**
- Spec § "Architecture" → Task 0 (helper) + Task 1 (5 hooks) + Task 2 (install).
- Spec § "Live-store reader contract" → Task 0 (helper code + 5 tests).
- Spec § "Per-hook refit" → Task 1 (5 sub-steps).
- Spec § "settings.json swap" → Task 2 Steps 3-5 (dry-run, live install, verification).
- Spec § "Upstream deliverables" → Tasks 4 + 5.
- Spec § "Success criteria" items 1-5 → all covered (Task 0 AC, Task 1 AC, Task 2 AC, Task 3 AC, Task 6 AC).
- Spec § "Risk" → Task 0's transcript-fallback test + Task 2's settings.json backup.

**Placeholder scan:** Task 1 Steps 2-5 use phrases like "Same pattern as Step 2: copy the marketplace original verbatim, ..." — that's not a placeholder per se but it's not "complete code in every step" either. Acceptable here because (a) the marketplace originals are local files the subagent will literally `cat` to seed the new file, and (b) the spec-prescribed change in each step is a single localised insertion. The subagent's task is mechanical copy + insert.

Task 3 Steps 1-5 describe HIGH-LEVEL triggers ("create an in-progress task and attempt a commit") without showing the exact tool call sequence. This is intentional — the smoke test runs IN-SESSION via the subagent's own `TaskCreate` + `TaskUpdate` calls, not via copy-paste-able commands. The acceptance criteria specify the observable (trace-log line with `variant=local`) which is the actual gate.

**Type consistency:** Helper JSON output schema (`source`, `session_id`, `tasks`, `in_progress_count`, `blocked_by_lookup`) is identical across spec, tests, and the 5 hook scripts.

**User-gate scan:**
- Task 3 ("Smoke-verify each hook fires the local variant") matches Nouns bucket (`smoke test`, `verification`) + Scope bucket (`before` is implicit in "after install"). Tagged `userGate: true` with `requireEvidenceTokens: [["variant=local"], ["no-marketplace"]]`. Banner added.
- Other tasks: normal verbs only. No tagging.

User-gate hook is already registered (pre-emptive check earlier in this session at `pcvelz/superpowers`'s `post-task-complete-revalidate.sh` line in settings.json). No heads-up note needed.

---

## Out-of-scope (deferred)

- PR against `pcvelz/superpowers` or `anthropics/claude-code`.
- Migration of unrelated `gsd-*` hooks under different ownership.
- A test harness that simulates the harness's task-store write timing.
- A general-purpose "live-store" library shared with non-superpowers hook ecosystems.
- Sandboxing the install script's `python3 -c "..."` settings.json rewriter into a separate file.
