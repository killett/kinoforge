"""Banner test forbidding any read of ``Pod.uptimeSeconds`` in production code.

C33 Q1/Q3 evidence (`tests/live/_c33_probe_q1_evidence.json`, sweep
2026-06-13) proved RunPod's top-level ``Pod.uptimeSeconds`` GraphQL
field is a fully broken stub — 154/154 samples returned exactly ``0``
across 12 GPU types over a 16-hour window. The investigation
implication (PROGRESS C33 Q3 (d)): no production code currently reads
the field; only the Q1 probe under ``tests/live/`` explicitly tested
it.

This test locks that absence in. If a future PR introduces
``pod.get("uptimeSeconds")`` in a stall classifier, embeds
``uptimeSeconds`` in a production GraphQL query, or otherwise touches
the broken stub from ``src/``, this test fails LOUD with file:line
context so the developer is forced to either prove the read is
legitimate (e.g. nested ``runtime.uptimeSeconds`` on a different
record type) or pick a different signal.

Why a banner test, not a runtime guard:
  * The bug is silent at runtime (returns 0, never raises). A unit
    test of a stall classifier would not catch the regression because
    a mocked GraphQL response would still claim the field is "valid".
  * The signal that something is wrong is the presence of the string
    itself in production code — caught at lint time, not under load.

If a legitimate use case ever appears, amend this test with the
specific allowlisted file:string pair and a comment explaining why
the read is safe (e.g. confirmed-not-stub via a second probe sweep).
"""

from __future__ import annotations

from pathlib import Path

_FORBIDDEN_SUBSTRING = "uptimeSeconds"
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def test_no_uptimeseconds_reads_in_production_code() -> None:
    """``Pod.uptimeSeconds`` is a broken RunPod GraphQL stub — never read it."""
    matches: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_SUBSTRING in line:
                rel = path.relative_to(_SRC_ROOT.parent)
                matches.append(f"{rel}:{lineno}: {line.strip()}")

    assert not matches, (
        f"Found {len(matches)} read(s) of the broken-stub field "
        f"`Pod.uptimeSeconds` in production code under {_SRC_ROOT}:\n"
        + "\n".join(f"  {m}" for m in matches)
        + "\n\nC33 Q1/Q3 sweep (154/154 samples == 0, 16 hours, 12 GPU "
        "types) proved this field is broken. Either prove the read is "
        "legitimate (different record type, confirmed not the broken "
        "stub) and amend this test with an allowlist entry, or pick a "
        "different signal — `pod.runtime.uptimeSeconds` is one candidate, "
        "wall-clock-vs-`lastStartedAt` is another."
    )
