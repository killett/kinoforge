"""Unit + reverse tests for the concrete-cloud-identifier scanner.

Every identifier this file plants is built by concatenation, never written
as a literal. A literal would be found by `tests/test_cloud_identifier_scrub.py`
sweeping this very file — the same idiom `tests/test_source_audit.py:98` uses
for credential shapes, and for the same reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.scan_identifiers import (
    ALLOW_PRAGMA,
    IDENTIFIER_PATTERNS,
    iter_identifier_findings,
    scan_all_tracked_identifiers,
)

# Built by concatenation so this file stays clean under its own sweep.
_REAL_ACCOUNT = "9" + "18273645" + "019"
_REAL_PROJECT = "kinoforge-prod-" + "0dd" + "b375e"
_REAL_UUID = "4b0dbe0c-" + "3a76-401a-" + "ac2e-" + "d0d949b9fa3e"
_REAL_BUCKET_GS = "acme" + "-render-output"
_REAL_BUCKET_S3 = "acme" + "-prod"
_REAL_BILLING_ACCOUNT = "01522C-" + "EC9AA4-" + "64A7D5"


def _run_git(repo: Path, *args: str) -> None:
    """Run a git command in *repo*, raising on failure.

    Args:
        repo: Working directory to run git in.
        *args: Arguments after ``git``.

    Raises:
        subprocess.CalledProcessError: If git exits non-zero.
    """
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_pattern_names_are_the_eight_declared_classes() -> None:
    """Guards against a refactor that empties or renames the pattern tier."""
    names = {p.name for p in IDENTIFIER_PATTERNS}
    assert names == {
        "aws_account_in_arn",
        "aws_account_labelled",
        "aws_account_in_prose",
        "kms_key_uuid",
        "gcp_service_account_email",
        "gcp_project_id",
        "gcp_billing_account",
        "cloud_bucket_uri",
    }


@pytest.mark.parametrize(
    ("text", "expected_pattern", "expected_value"),
    [
        (
            f"arn:aws:kms:us-east-1:{_REAL_ACCOUNT}:key/alias-x",
            "aws_account_in_arn",
            _REAL_ACCOUNT,
        ),
        (f'account_id = "{_REAL_ACCOUNT}"', "aws_account_labelled", _REAL_ACCOUNT),
        (f"key/{_REAL_UUID}", "kms_key_uuid", _REAL_UUID),
        (
            f"kinoforge-runner@{_REAL_PROJECT}.iam.gserviceaccount.com",
            "gcp_service_account_email",
            _REAL_PROJECT,
        ),
        (
            f"billing_dataset = '{_REAL_PROJECT}.all_billing_data'",
            "gcp_project_id",
            _REAL_PROJECT,
        ),
        (
            f"gs://{_REAL_BUCKET_GS}/x",
            "cloud_bucket_uri",
            _REAL_BUCKET_GS,
        ),
        (
            f"s3://{_REAL_BUCKET_S3}/artifacts",
            "cloud_bucket_uri",
            _REAL_BUCKET_S3,
        ),
        (
            f"- **AWS account {_REAL_ACCOUNT}** (us-west-2):",
            "aws_account_in_prose",
            _REAL_ACCOUNT,
        ),
        (
            f"Budget `billingAccounts/{_REAL_BILLING_ACCOUNT}/budgets/c3a`",
            "gcp_billing_account",
            _REAL_BILLING_ACCOUNT,
        ),
    ],
)
def test_concrete_identifier_is_found(
    text: str, expected_pattern: str, expected_value: str
) -> None:
    """Each identifier class fires on a concrete value.

    A bug that would fail this: dropping the `aws_account_labelled` pattern,
    or writing the bucket regex so it only matches `s3://` and not `gs://`.
    """
    findings = list(iter_identifier_findings(text))
    assert [(f.pattern_name, f.value) for f in findings] == [
        (expected_pattern, expected_value)
    ]


@pytest.mark.parametrize(
    "text",
    [
        "arn:aws:iam::123456789012:role/skypilot-x",
        "arn:aws:iam::<AWS_ACCOUNT>:role/skypilot-x",
        "key/<KMS_KEY_ID>",
        "kinoforge-runner@proj.iam.gserviceaccount.com",
        "kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com",
        "dataset = 'kinoforge-prod-deadbeef.all_billing_data'",
        "gs://bkt/x",
        "s3://bucket/x",
        "s3://my-bucket/x",
        "s3://layer-w-test/x",
        "s3://probe-discard/x",
        "gs://<GCS_BUCKET>/x",
        "- **AWS account 123456789012** (us-west-2):",
        "Budget `billingAccounts/<GCP_BILLING_ACCOUNT>/budgets/c3a`",
    ],
)
def test_reserved_and_placeholder_values_do_not_fire(text: str) -> None:
    """Doc-reserved, fake, and placeholder values must stay silent.

    A bug that would fail this: forgetting to exempt AWS's
    reserved-for-documentation account, which would light up every tracked
    example and make the guard unshippable.
    """
    assert list(iter_identifier_findings(text)) == []


def test_project_id_inside_a_service_account_email_reports_once() -> None:
    """A project id inside an SA email belongs to the SA-email pattern alone.

    A bug that would fail this: dropping the negative lookahead from
    `gcp_project_id`, which makes one line yield two findings for a single
    identifier and pads the lockdown test's failure list with duplicates.
    """
    text = f"kinoforge-runner@{_REAL_PROJECT}.iam.gserviceaccount.com"
    findings = list(iter_identifier_findings(text))
    assert [f.pattern_name for f in findings] == ["gcp_service_account_email"]


def test_account_id_assignment_does_not_also_fire_prose_pattern() -> None:
    """`account_id = "..."` belongs to `aws_account_labelled` alone.

    A bug that would fail this: loosening `aws_account_in_prose`'s
    whitespace requirement so it also matches the underscore in
    `account_id`, double-reporting one identifier under two pattern names.
    """
    text = f'account_id = "{_REAL_ACCOUNT}"'
    findings = list(iter_identifier_findings(text))
    assert [f.pattern_name for f in findings] == ["aws_account_labelled"]


def test_allow_pragma_suppresses_the_line() -> None:
    """The pragma is the only escape hatch; a directory exclusion is not.

    A bug that would fail this: matching the pragma case-sensitively, or
    checking it against the whole file rather than the matched line.
    """
    line = f"key/{_REAL_UUID}  # {ALLOW_PRAGMA} — quoted as F10 evidence"
    assert list(iter_identifier_findings(line)) == []


def test_pragma_does_not_leak_to_neighbouring_lines() -> None:
    """Suppression is per-line, not per-file.

    A bug that would fail this: an `if ALLOW_PRAGMA in text` guard at the
    top of the scan, which would silence an entire document.
    """
    text = f"key/{_REAL_UUID}  # {ALLOW_PRAGMA}\nkey/{_REAL_UUID}\n"
    findings = list(iter_identifier_findings(text))
    assert len(findings) == 1
    assert findings[0].line_no == 2


def test_scan_fires_on_a_planted_identifier(tmp_path: Path) -> None:
    """Reverse test: without it, the repo lockdown could no-op forever.

    Goes through `scan_all_tracked_identifiers` end-to-end against a real
    throwaway git repo rather than calling `iter_identifier_findings` on a
    string, so a regression that returns `[]` vacuously (wrong cwd, empty
    `git ls-files`, bad repo root) still fails. Mirrors
    `tests/test_source_audit.py:76-110`.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "rogue.md").write_text(f"Prose.\n\nProject: {_REAL_PROJECT}\n")
    _run_git(tmp_path, "add", "rogue.md")
    _run_git(tmp_path, "commit", "-q", "-m", "plant an identifier")

    findings = scan_all_tracked_identifiers(tmp_path)
    assert len(findings) == 1
    path, finding = findings[0]
    assert path == "rogue.md"
    assert finding.pattern_name == "gcp_project_id"
    assert finding.line_no == 3


def test_binary_file_is_skipped(tmp_path: Path) -> None:
    """A NUL in the first 8000 bytes means there is nothing text-shaped.

    A bug that would fail this: decoding blindly and reporting garbage
    matches out of a compiled artifact.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")

    (tmp_path / "blob.bin").write_bytes(b"\x00" + _REAL_PROJECT.encode())
    _run_git(tmp_path, "add", "blob.bin")
    _run_git(tmp_path, "commit", "-q", "-m", "plant a binary")

    assert scan_all_tracked_identifiers(tmp_path) == []


def test_unreadable_tracked_file_is_a_hard_error(tmp_path: Path) -> None:
    """Fail closed: an unreadable file must not be reported as clean.

    A bug that would fail this: a bare `except OSError: continue`, which is
    exactly the fail-open `tools/scan_secrets.py` was fixed for in 8f1f4307.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")

    target = tmp_path / "locked.md"
    target.write_text("prose\n")
    _run_git(tmp_path, "add", "locked.md")
    _run_git(tmp_path, "commit", "-q", "-m", "add a file")
    target.chmod(0o000)

    try:
        with pytest.raises(RuntimeError, match="cannot read tracked file"):
            scan_all_tracked_identifiers(tmp_path)
    finally:
        target.chmod(0o644)


def test_missing_tracked_file_is_skipped_not_raised(tmp_path: Path) -> None:
    """Mid-rebase / mid-stash-pop states have nothing to scan, not an error.

    A bug that would fail this: treating `FileNotFoundError` like any other
    `OSError` and turning a normal git state into a crash.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")

    target = tmp_path / "gone.md"
    target.write_text("prose\n")
    _run_git(tmp_path, "add", "gone.md")
    _run_git(tmp_path, "commit", "-q", "-m", "add a file")
    target.unlink()

    assert scan_all_tracked_identifiers(tmp_path) == []
