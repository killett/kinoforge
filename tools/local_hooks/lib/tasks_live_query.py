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
    """Parse the marketplace-style transcript JSONL into task-dict shape.

    Produces the same task-dict shape as the live store.

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


def _build_payload(
    source: str, session_id: str, tasks: list[dict[str, Any]]
) -> dict[str, Any]:
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
    """Entry point: parse args, query task state, emit JSON to stdout.

    Always exits 0 (fail-open contract).
    """
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
        default=os.environ.get(
            "CLAUDE_TASKS_ROOT", str(Path.home() / ".claude" / "tasks")
        ),
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
        if not args.session_id.strip():
            raise ValueError("--session-id must be a non-empty string")
        root = Path(args.root)
        tasks: list[dict[str, Any]] | None = None
        if not args.no_live_store:
            tasks = _read_live_store(root, args.session_id)
        if tasks is None:
            # Live store dir absent — fall through to transcript fallback.
            if args.transcript:
                fallback_tasks = _read_transcript_fallback(Path(args.transcript))
                json.dump(
                    _build_payload(
                        "transcript-fallback", args.session_id, fallback_tasks
                    ),
                    sys.stdout,
                )
                return 0
            json.dump(
                _empty_payload("transcript-fallback", args.session_id), sys.stdout
            )
            return 0
        # Live store dir present (possibly empty list) — authoritative.
        json.dump(_build_payload("live-store", args.session_id, tasks), sys.stdout)
        return 0
    except Exception:  # noqa: BLE001 — fail-open is the contract
        traceback.print_exc(file=sys.stderr)
        json.dump(_empty_payload("fallback-failed", args.session_id), sys.stdout)
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
