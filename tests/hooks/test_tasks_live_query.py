"""Tests for the live-task-store reader helper at
tools/local_hooks/lib/tasks_live_query.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

HELPER = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "local_hooks"
    / "lib"
    / "tasks_live_query.py"
)


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


def _write_task(
    root: Path,
    session_id: str,
    tid: str,
    status: str,
    blocked_by: list[str] | None = None,
) -> None:
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
    tasks_by_id = {t["id"]: t for t in payload["tasks"]}  # type: ignore[attr-defined]
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
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TaskCreate",
                        "input": {"subject": "A"},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TaskCreate",
                        "input": {"subject": "B"},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TaskUpdate",
                        "input": {"taskId": "2", "status": "in_progress"},
                    },
                ]
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines))
    rc, payload, _ = _run(
        [
            "--session-id",
            sid,
            "--root",
            str(tmp_path),
            "--transcript",
            str(transcript),
        ]
    )
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
    rc, payload, _ = _run(
        [
            "--session-id",
            sid,
            "--root",
            str(tmp_path),
            "--transcript",
            str(transcript),
            "--no-live-store",
        ]
    )
    assert rc == 0
    assert payload["source"] == "transcript-fallback"
    assert payload["in_progress_count"] == 0


def test_empty_live_store_dir_reports_live_store_source(tmp_path: Path) -> None:
    """When the live-store dir EXISTS but contains zero task JSON files,
    the helper MUST report source='live-store' with an empty task list —
    NOT fall through to transcript-fallback. The dir's existence is
    authoritative.

    Bug catch: a regression that conflates None (dir absent) with []
    (dir present, no tasks) would lie about the source on a brand-new
    session that hasn't called TaskCreate yet, and would silently
    read a stale transcript instead of trusting the live store.
    """
    sid = "s-empty"
    d = tmp_path / sid
    d.mkdir()
    # Dotfiles allowed; they should be filtered.
    (d / ".lock").write_text("")
    (d / ".highwatermark").write_text("1")
    # Provide a transcript that WOULD say in_progress=1 to prove the
    # empty live-store dir wins.
    transcript = tmp_path / f"{sid}.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "TaskCreate",
                            "input": {"subject": "X"},
                        },
                        {
                            "type": "tool_use",
                            "name": "TaskUpdate",
                            "input": {"taskId": "1", "status": "in_progress"},
                        },
                    ]
                },
            }
        )
    )
    rc, payload, _ = _run(
        [
            "--session-id",
            sid,
            "--root",
            str(tmp_path),
            "--transcript",
            str(transcript),
        ]
    )
    assert rc == 0
    assert payload["source"] == "live-store", (
        f"empty dir should report live-store, got {payload['source']!r}"
    )
    assert payload["in_progress_count"] == 0
    assert payload["tasks"] == []
