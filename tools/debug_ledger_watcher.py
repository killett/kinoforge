"""Snapshot .kinoforge/_lifecycle/ledger.json on a fixed interval.

Diagnostic tool — used during live RunPod runs to investigate ledger
entry lifecycle (when entries land, when they disappear, when
heartbeat_thread_tick fires). Stays on the host; never edits state.

Run as: ``python tools/debug_ledger_watcher.py [--state-dir DIR] [--interval-s N]``.
Output: one line per poll on stdout — ``[TS] sha=<12> entries=N <per-entry>``.

When the ledger file is missing, the line is ``[TS] LEDGER FILE MISSING``.
Stops on KeyboardInterrupt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path


def _summarize_entry(entry: dict[str, object]) -> str:
    """Render one ledger entry as a short trailer line."""
    iid = str(entry.get("id", "?"))[:16]
    hb = entry.get("heartbeat_thread_tick")
    ss = entry.get("session_start")
    se = entry.get("session_end")
    return f"  {iid:<16} hb_tick={hb!s:<18} session_start={ss!s:<18} session_end={se!s}"


def _snapshot(ledger_path: Path) -> None:
    """Print one timestamped snapshot of ``ledger_path``."""
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not ledger_path.exists():
        print(f"[{ts}] LEDGER FILE MISSING ({ledger_path})", flush=True)
        return
    raw = ledger_path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()[:12]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[{ts}] sha={sha} PARSE_ERROR: {exc}", flush=True)
        return
    entries = data.get("entries", [])
    print(f"[{ts}] sha={sha} entries={len(entries)}", flush=True)
    for entry in entries:
        print(_summarize_entry(entry), flush=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        default="/workspace/.kinoforge",
        help="kinoforge --state-dir (default: /workspace/.kinoforge)",
    )
    parser.add_argument(
        "--interval-s",
        type=float,
        default=5.0,
        help="poll cadence in seconds (default: 5.0)",
    )
    args = parser.parse_args(argv)
    ledger = Path(args.state_dir) / "_lifecycle" / "ledger.json"
    print(
        f"[watcher] tracking {ledger} every {args.interval_s}s",
        flush=True,
    )
    try:
        while True:
            _snapshot(ledger)
            time.sleep(args.interval_s)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
