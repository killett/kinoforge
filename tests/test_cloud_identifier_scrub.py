"""Lockdown: no tracked file may contain a concrete cloud identifier.

Sibling of `tests/test_source_audit.py`, which does the same job for
credential shapes. Both are standing guards over `git ls-files`, so both
fire even when the committer used `--no-verify`.

Scope is every tracked file, deliberately. A test scoped to the two files
already scrubbed by hand would catch nothing — the leak this exists to
stop lives in `tools/`, in a production default parameter.
"""

from __future__ import annotations

from pathlib import Path

from tools.scan_identifiers import ALLOW_PRAGMA, scan_all_tracked_identifiers

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def test_no_tracked_file_contains_a_concrete_identifier() -> None:
    """Every tracked file must be free of concrete cloud identifiers.

    Covers 12-digit AWS account ids in ARN position, KMS key UUIDs,
    service-account emails, GCP project ids, and GCS/S3 bucket names.
    """
    findings = scan_all_tracked_identifiers(_REPO_ROOT)
    detail = "\n".join(
        f"  {path}:{f.line_no} [{f.pattern_name}] {f.value}" for path, f in findings
    )
    assert not findings, (
        f"Found {len(findings)} concrete cloud identifier(s) in tracked files:\n"
        f"{detail}\n"
        "Fix: use the established fake (kinoforge-prod-deadbeef, 123456789012, "
        "bkt), a <PLACEHOLDER>, or — only where quoting the real value IS the "
        f"point — the `{ALLOW_PRAGMA}` pragma on that line."
    )
