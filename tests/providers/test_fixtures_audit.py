"""Lockdown: no committed *.json fixture under tests/ may contain a credential.

Pairs with the runtime _RecordingHTTPSeam backstop (which catches NEW leaks at
capture time) to catch PRE-EXISTING leaks and any future drift.  Pattern set
comes from tests/providers/conftest_runpod.py::_CREDENTIAL_PATTERNS.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.providers.conftest_runpod import LeakHit, _audit_for_leaks

_TESTS_ROOT: Path = Path(__file__).resolve().parents[1]


def _format_offenders(offenders: list[tuple[Path, list[LeakHit]]]) -> str:
    """Build a human-readable multi-line block describing every leak."""
    lines = [f"Found credential leaks in {len(offenders)} fixture file(s):"]
    for path, hits in offenders:
        rel = path.relative_to(_TESTS_ROOT.parent)
        lines.append(f"  {rel}:")
        for hit in hits:
            lines.append(
                f"    - {hit.pattern_name} at {hit.json_pointer}: {hit.match_snippet!r}"
            )
    lines.append(
        "Either rotate the leaked credential AND scrub the fixture, or update "
        "the redactor to cover the shape and regenerate."
    )
    return "\n".join(lines)


def test_no_committed_fixture_contains_a_credential() -> None:
    """Every committed *.json under tests/ must pass _audit_for_leaks."""
    offenders: list[tuple[Path, list[LeakHit]]] = []
    for path in _TESTS_ROOT.rglob("*.json"):
        try:
            with path.open() as f:
                payload = json.load(f)
        except json.JSONDecodeError:
            # Non-JSON file accidentally suffixed .json — skip and let other
            # tests catch the malformation.
            continue
        hits = _audit_for_leaks(payload)
        if hits:
            offenders.append((path, hits))
    assert not offenders, _format_offenders(offenders)
