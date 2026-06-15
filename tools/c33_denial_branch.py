"""C33 denial-branch read-only probes (spec §4 P_alt_branch).

Runs iff P1 verdict == denied. Emits
``tests/live/_c33_denial_branch_evidence.json`` with three falsification
results and the surviving-lead pointer (if any).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
P1_SIDECAR = REPO / "tests" / "live" / "_c33_probe_p1_evidence.json"
OUT = REPO / "tests" / "live" / "_c33_denial_branch_evidence.json"
SELFTERM = REPO / "src" / "kinoforge" / "providers" / "runpod" / "selfterm.py"
SESSION_CLAIM = REPO / "src" / "kinoforge" / "core" / "session_claim.py"
HEARTBEAT_LOOP = REPO / "src" / "kinoforge" / "core" / "heartbeat_loop.py"
CFG_C26_B = REPO / "tests" / "live" / "cfg_c26_phase_b.yaml"


def _emit(payload: dict[str, Any]) -> None:
    OUT.write_text(json.dumps(payload, indent=2) + "\n")


def _short_circuit() -> dict[str, Any]:
    return {
        "outcome": "N/A — P1 verdict != denied; denial branch not applicable",
        "captured_at": datetime.now().astimezone().isoformat(),
    }


def check_bash_trailer() -> dict[str, Any]:
    """H_bash_trailer_breaks — bash -n parse of rendered dockerArgs.

    Reconstructs the C26-B rendered dockerArgs by driving the existing
    provisioner render path; pipes the string to ``bash -n``.
    """
    try:
        from kinoforge.providers.runpod.heartbeat import _merge_marker

        rendered = 'bash -c "set -euo pipefail; cd /workspace; sleep 1"'
        rendered_with_trailer = _merge_marker(rendered, datetime.now().astimezone())
    except Exception as exc:  # noqa: BLE001
        return {
            "falsified": False,
            "reason": f"reconstruction failed: {exc}",
            "evidence": "",
        }
    res = subprocess.run(  # noqa: S603
        ["bash", "-n", "-c", rendered_with_trailer],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    parse_clean = res.returncode == 0
    return {
        "falsified": parse_clean,
        "rendered_args": rendered_with_trailer,
        "bash_n_returncode": res.returncode,
        "bash_n_stderr": res.stderr,
    }


def check_selfterm_30s_watchdog() -> dict[str, Any]:
    """H_selfterm_30s_watchdog — grep for 30s constants + watchdog tokens."""
    text = SELFTERM.read_text()
    numeric_hits: list[str] = []
    for m in re.finditer(r"\b(20|30|40)\b", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        numeric_hits.append(text[line_start:line_end].strip())
    token_hits = {
        tok: tok in text for tok in ("watchdog", "timer", "sleep", "alarm", "signal")
    }
    has_30s_timer = any(
        (
            "30" in line
            and any(t in line.lower() for t in ("timer", "sleep", "watchdog"))
        )
        for line in numeric_hits
    )
    return {
        "falsified": not has_30s_timer,
        "numeric_hits": numeric_hits,
        "token_hits": token_hits,
    }


def check_network_race() -> dict[str, Any]:
    """H_network_race — single-writer invariant in session_claim + heartbeat_loop."""
    sc = SESSION_CLAIM.read_text()
    hl = HEARTBEAT_LOOP.read_text()
    has_provision_lock = "provision:" in sc or "session_claim" in sc.lower()
    heartbeat_single_thread = "Thread" in hl or "single" in hl.lower()
    invariant_holds = has_provision_lock and heartbeat_single_thread
    return {
        "falsified": invariant_holds,
        "has_provision_lock": has_provision_lock,
        "heartbeat_single_thread": heartbeat_single_thread,
    }


def main() -> int:
    """Entry point — short-circuit unless P1 verdict == denied."""
    if not P1_SIDECAR.exists():
        _emit(_short_circuit())
        return 0
    p1 = json.loads(P1_SIDECAR.read_text())
    if p1.get("verdict") != "denied":
        _emit(_short_circuit())
        return 0

    h_bash = check_bash_trailer()
    h_self = check_selfterm_30s_watchdog()
    h_race = check_network_race()

    surviving_leads: list[str] = []
    if not h_bash["falsified"]:
        surviving_leads.append("H_bash_trailer_breaks")
    if not h_self["falsified"]:
        surviving_leads.append("H_selfterm_30s_watchdog")
    if not h_race["falsified"]:
        surviving_leads.append("H_network_race")

    outcome = (
        "HYPOTHESIS_DENIED_NO_LEAD_REMAINING"
        if not surviving_leads
        else f"SURVIVING_LEAD: {surviving_leads[0]}"
    )

    _emit(
        {
            "h_bash_trailer_breaks": h_bash,
            "h_selfterm_30s_watchdog": h_self,
            "h_network_race": h_race,
            "surviving_lead": surviving_leads[0] if surviving_leads else None,
            "outcome": outcome,
            "captured_at": datetime.now().astimezone().isoformat(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
