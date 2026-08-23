# Least-Privilege Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the scoped AWS policy and a new minimum GCP role list the first thing a new operator pastes, and add two tests that stop the scrub and env-doc discipline from decaying back.

**Architecture:** Three independent seams. (1) A new `tools/scan_identifiers.py` mirrors the existing `tools/scan_secrets.py` shape — pattern list, `iter_*_findings`, `scan_all_tracked_*`, line pragma — and a lockdown test in `tests/` pins it over `git ls-files`. (2) A new `tools/render_aws_policy.py` substitutes the three placeholders in `.aws/policies/skypilot-minimal.template.json` at attach time and writes outside the repo, which is what lets the tracked file stay placeholder-clean AND be attachable. (3) `tools/validate_scoped_policy.py` exercises both scoped grants through IAM simulation only — no EC2 launch, no GCE instance, zero compute spend.

**Tech Stack:** Python 3.12+, pytest, boto3 (default pixi env), `google-cloud-resource-manager` (default pixi env), `python-dotenv`, ruff, mypy.

**Global Constraints:**
- Every new module needs full type hints and Google-style docstrings (repo-wide rule; `mypy .` runs at pre-commit).
- **No new file may contain a concrete cloud identifier.** Test files that need one plant it by string concatenation, exactly as `tests/test_source_audit.py:98` does (`"AKIA" + "QWERTYUIOPASDFGH"`). A literal would trip the very sweep this plan adds.
- Placeholder spelling is fixed: `<AWS_ACCOUNT>`, `<KMS_KEY_ID>`, `<S3_BUCKET_PREFIX>`, `<GCP_PROJECT>`, `<GCS_BUCKET>`, `<S3_BUCKET>`. `<GCS_KMS_KEYRING>` is retired.
- Fake-identifier conventions are fixed and already exist in-repo: GCP project `kinoforge-prod-deadbeef` (`tests/stores/test_recording.py:48-50`), AWS account `123456789012`, SA project part `proj`, buckets `bkt` / `bucket` / `my-bucket` / `layer-w-test` / `probe-discard`.
- Pre-commit does NOT run pytest (`.pre-commit-config.yaml` runs ruff, ruff-format, mypy, check-merge-conflict, check-added-large-files, check-toml, scan-secrets). A deliberately-RED test therefore commits cleanly, which Task 1 relies on.
- No live spend anywhere in this plan. Task 9's validation is IAM simulation and `testIamPermissions` only — both free API calls.

**User decisions (already made):**
- "IAM simulate, no launch" — validate the scoped policy statically; no EC2/GCE launch. Zero compute spend.
- "All tracked files" — the identifier sweep covers every `git ls-files` path, not just cloud config. This deliberately forces the GCP project-id rename in `tools/quota_burn_lib.py:266`.
- "Forward + marker, narrow reverse" — every `.env.example` key must be consumed or explicitly marked; the reverse check runs against a small curated operator-facing list, not all `KINOFORGE_*`.
- Escape hatch is a `kinoforge: allow-identifier` line pragma, **not** a directory exclusion. Approved explicitly: "a blanket exclusion is the decay the brief exists to stop."
- The GCP half of Task 9 (`testIamPermissions`) stays in scope.

---

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `tools/scan_identifiers.py` | **Create.** Concrete-cloud-identifier patterns + tracked-tree sweep. Mirrors `tools/scan_secrets.py`. | 0 |
| `tests/tools/test_scan_identifiers.py` | **Create.** Unit + planted-repo reverse tests for the scanner. | 0 |
| `tests/test_cloud_identifier_scrub.py` | **Create.** Repo lockdown over `git ls-files`. Sits at `tests/` root beside `test_source_audit.py`, its sibling guard. | 1 |
| ~30 tracked files | **Modify.** Scrub project id, bucket names, KMS UUID. | 2 |
| `.aws/policies/skypilot-minimal.template.json` | **Modify.** `<KMS_KEY_ID>` (Task 2), `<S3_BUCKET_PREFIX>` + rename to `.template.json` (Task 3). UNVALIDATED banner lives in the sibling `.aws/policies/README.md`, not a `_comment` key — IAM's policy grammar is closed and rejects arbitrary top-level keys. | 2, 3 |
| `tools/render_aws_policy.py` | **Create.** Placeholder substitution; refuses to emit a partially-rendered policy or write inside the repo. | 3 |
| `tests/tools/test_render_aws_policy.py` | **Create.** | 3 |
| `.gcp/policies/roles.txt` | **Create.** Runtime role list + bootstrap-only note. | 4 |
| `.gcp/README.md` | **Modify.** `securityAdmin` annotation → grant-bootstrap-revoke. | 4 |
| `.env.example` | **Modify.** AWS + GCP blocks inverted (Task 5), billing section (Task 6), `KINOFORGE_S3_BUCKET` + markers (Task 7). | 5, 6, 7 |
| `.aws/README.md` | **Modify.** Bootstrap step 1 stops recommending `AmazonS3FullAccess`. | 5 |
| `tests/test_env_example_drift.py` | **Create.** Parse + forward-consumption + narrow-reverse. | 7 |
| `tools/validate_scoped_policy.py` | **Create.** AWS simulate + GCP `testIamPermissions`, injected clients. | 8 |
| `tests/tools/test_validate_scoped_policy.py` | **Create.** SDK-free, fake clients. | 8 |

---

## Task 0: Identifier scanner module

**Goal:** A reusable scanner that finds concrete cloud identifiers in text, with a line pragma escape hatch, mirroring the shape of `tools/scan_secrets.py`.

**Files:**
- Create: `tools/scan_identifiers.py`
- Test: `tests/tools/test_scan_identifiers.py`

**Acceptance Criteria:**
- [ ] Eight named patterns exist: `aws_account_in_arn`, `aws_account_labelled`, `aws_account_in_prose`, `kms_key_uuid`, `gcp_service_account_email`, `gcp_project_id`, `gcp_billing_account`, `cloud_bucket_uri` (the last two added in Task 2's follow-up round, per the human-approved scope addition)
- [ ] Reserved/fake values do not fire: AWS `123456789012`, GCP project `kinoforge-prod-deadbeef`, SA project part `proj`, buckets `bkt`/`bucket`/`my-bucket`/`layer-w-test`/`probe-discard`
- [ ] `kinoforge: allow-identifier` on a line suppresses every finding on that line
- [ ] `scan_all_tracked_identifiers` enumerates via `git ls-files -z`, skips binary (NUL in first 8000 bytes), skips `FileNotFoundError`, raises `RuntimeError` on any other read failure
- [ ] Reverse test: a planted identifier in a throwaway git repo is found end-to-end through `scan_all_tracked_identifiers`

**Verify:** `pixi run python -m pytest tests/tools/test_scan_identifiers.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_scan_identifiers.py`:

```python
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
        (f"billing_dataset = '{_REAL_PROJECT}.all_billing_data'", "gcp_project_id", _REAL_PROJECT),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/tools/test_scan_identifiers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.scan_identifiers'`

- [ ] **Step 3: Write minimal implementation**

Create `tools/scan_identifiers.py`:

```python
"""Lockdown: no tracked file may contain a concrete cloud identifier.

Sibling of `tools/scan_secrets.py`. That module answers "is this a
credential?"; this one answers "is this a real account, key, project,
service account, or bucket?" — identifiers that are not secrets but whose
presence in a tracked file is exactly the discipline `.gitignore:90-91`
already enforces for two files and nothing else.

Escape hatch is a per-line pragma (`kinoforge: allow-identifier`), matching
the `kinoforge: allow-secret` idiom in
`src/kinoforge/core/credential_patterns.py:304`. Deliberately NOT a
directory exclusion: a blanket exclusion is the decay this guard exists to
stop.

See `docs/superpowers/specs/2026-08-21-least-privilege-onboarding-design.md`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ALLOW_PRAGMA = "kinoforge: allow-identifier"

# AWS's reserved-for-documentation account, plus the all-zero form. Both
# appear throughout tracked examples and are not identifiers of anything.
_RESERVED_AWS_ACCOUNTS = frozenset({"123456789012", "000000000000"})

# Fake project parts already used by tracked tests and docs.
_FAKE_GCP_PROJECTS = frozenset(
    {"proj", "kinoforge-prod-deadbeef", "example", "test", "fake"}
)

# Test doubles in use across tests/ and docs/. Adding a name here is a
# deliberate act — that is the point.
_ALLOWED_BUCKETS = frozenset(
    {"bkt", "bucket", "my-bucket", "layer-w-test", "probe-discard"}
)


@dataclass(frozen=True)
class IdentifierPattern:
    """A named concrete-identifier shape plus its exemptions.

    Attributes:
        name: Stable pattern name, reported in findings.
        regex: Compiled pattern whose group 1 is the identifier value.
        allowed: Values that must never be reported.
    """

    name: str
    regex: re.Pattern[str]
    allowed: frozenset[str]


@dataclass(frozen=True)
class IdentifierFinding:
    """One concrete identifier found in text.

    Attributes:
        pattern_name: Which :class:`IdentifierPattern` fired.
        line_no: 1-indexed line number.
        value: The concrete identifier as matched.
    """

    pattern_name: str
    line_no: int
    value: str


IDENTIFIER_PATTERNS: tuple[IdentifierPattern, ...] = (
    IdentifierPattern(
        name="aws_account_in_arn",
        # Anchored to ARN position. A bare \d{12} matches 53 hash fragments
        # in pixi.lock alone, which would make this guard unshippable.
        regex=re.compile(r"arn:aws[a-z-]*:[a-z0-9*-]*:[a-z0-9-]*:(\d{12})"),
        allowed=_RESERVED_AWS_ACCOUNTS,
    ),
    IdentifierPattern(
        name="aws_account_labelled",
        regex=re.compile(r"""(?i)account[_ -]?id["']?\s*[:=]\s*["']?(\d{12})"""),
        allowed=_RESERVED_AWS_ACCOUNTS,
    ),
    IdentifierPattern(
        name="aws_account_in_prose",
        # Bare prose, not assignment/ARN syntax: "AWS account 123456789012",
        # "account 123456789012 (us-west-2)". Anchored to the word "account"
        # followed directly by whitespace and 12 digits -- NOT to bare
        # \d{12}, which matches 53 hash fragments in pixi.lock alone (see
        # aws_account_in_arn's comment). "account_id"/"account-id" belong to
        # aws_account_labelled above; the whitespace requirement here means
        # this pattern never double-fires on that shape.
        regex=re.compile(r"(?i)\baccount\s+(\d{12})\b"),
        allowed=_RESERVED_AWS_ACCOUNTS,
    ),
    IdentifierPattern(
        name="gcp_billing_account",
        # GCP billing account resource name: billingAccounts/XXXXXX-XXXXXX-XXXXXX
        # (three 6-character hex groups). Distinct from gcp_project_id --
        # billing accounts are a different real-world resource with their
        # own leak risk (F4 in the verification doc's scope-addition note).
        regex=re.compile(
            r"billingAccounts/([0-9A-Fa-f]{6}-[0-9A-Fa-f]{6}-[0-9A-Fa-f]{6})"
        ),
        allowed=frozenset(),
    ),
    IdentifierPattern(
        name="kms_key_uuid",
        regex=re.compile(
            r"key/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        ),
        allowed=frozenset({"00000000-0000-0000-0000-000000000000"}),
    ),
    IdentifierPattern(
        name="gcp_service_account_email",
        regex=re.compile(r"[a-z0-9-]+@([a-z0-9-]+)\.iam\.gserviceaccount\.com"),
        allowed=_FAKE_GCP_PROJECTS,
    ),
    IdentifierPattern(
        name="gcp_project_id",
        # The negative lookahead hands a project id embedded in a service
        # account email to `gcp_service_account_email`, which is the more
        # specific pattern. Without it one such line yields two findings for
        # a single identifier -- noise in the lockdown test's failure list.
        regex=re.compile(
            r"\b(kinoforge-prod-[0-9a-z]{8})\b(?!\.iam\.gserviceaccount\.com)"
        ),
        allowed=frozenset({"kinoforge-prod-deadbeef"}),
    ),
    IdentifierPattern(
        name="cloud_bucket_uri",
        regex=re.compile(r"(?:gs|s3)://([a-z0-9][a-z0-9._-]{2,62})"),
        allowed=_ALLOWED_BUCKETS,
    ),
)


def iter_identifier_findings(
    text: str, *, patterns: tuple[IdentifierPattern, ...] = IDENTIFIER_PATTERNS
) -> Iterator[IdentifierFinding]:
    """Yield every concrete identifier in *text*.

    Suppression is per-line: a line carrying :data:`ALLOW_PRAGMA` yields
    nothing, and neighbouring lines are unaffected.

    Args:
        text: Content to scan.
        patterns: Pattern tier to apply. Defaults to
            :data:`IDENTIFIER_PATTERNS`.

    Yields:
        One :class:`IdentifierFinding` per non-exempt match.
    """
    for line_no, line in enumerate(text.splitlines(), start=1):
        if ALLOW_PRAGMA in line.lower():
            continue
        for pattern in patterns:
            for match in pattern.regex.finditer(line):
                value = match.group(1)
                if value in pattern.allowed:
                    continue
                yield IdentifierFinding(
                    pattern_name=pattern.name, line_no=line_no, value=value
                )


def scan_all_tracked_identifiers(
    repo: Path, *, patterns: tuple[IdentifierPattern, ...] = IDENTIFIER_PATTERNS
) -> list[tuple[str, IdentifierFinding]]:
    """Scan the working-tree content of every tracked file.

    Binary detection and decoding mirror
    :func:`tools.scan_secrets.scan_all_tracked` exactly, and for the same
    reason: this is a standing guard with no layer behind it, so one stray
    byte must cost one garbled character, not a file's whole coverage.

    A tracked path absent from the working tree is skipped — that is a
    legitimate mid-rebase state. A path that exists but cannot be read is a
    hard error; treating it as "no findings" is the fail-open this guard
    exists to prevent.

    Args:
        repo: Repository working directory.
        patterns: Pattern tier to apply.

    Returns:
        ``(path, IdentifierFinding)`` pairs.

    Raises:
        RuntimeError: A tracked file exists but could not be read.
    """
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    results: list[tuple[str, IdentifierFinding]] = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        target = repo / rel
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot read tracked file {rel}: {exc}") from exc
        if b"\x00" in raw[:8000]:
            continue
        text = raw.decode("utf-8", errors="replace")
        results.extend(
            (rel, finding)
            for finding in iter_identifier_findings(text, patterns=patterns)
        )
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/tools/test_scan_identifiers.py -v`
Expected: PASS (all tests)

Then: `pixi run pre-commit run --files tools/scan_identifiers.py tests/tools/test_scan_identifiers.py`
Expected: ruff, ruff-format, mypy, scan-secrets all Passed.

- [ ] **Step 5: Commit**

```bash
git add tools/scan_identifiers.py tests/tools/test_scan_identifiers.py
git commit -m "feat(scrub): scan tracked files for concrete cloud identifiers"
```

---

## Task 1: Repo lockdown test (committed RED)

**Goal:** A standing guard over `git ls-files` that fails on HEAD, documenting the ~30 dirty files that Task 2 cleans.

**Files:**
- Create: `tests/test_cloud_identifier_scrub.py`

**Acceptance Criteria:**
- [ ] `test_no_tracked_file_contains_a_concrete_identifier` fails on HEAD, listing each `path:line [pattern] value`
- [ ] Failure message names the fix: use the established fake, a `<PLACEHOLDER>`, or the `kinoforge: allow-identifier` pragma
- [ ] The RED output is captured into the commit message as the record of what Task 2 must clean
- [ ] `pixi run pre-commit run --all-files` still passes (pre-commit does not run pytest)

**Verify:** `pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v` → FAIL, with a finding list

**Steps:**

- [ ] **Step 1: Write the test (expected to fail)**

Create `tests/test_cloud_identifier_scrub.py`:

```python
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
```

- [ ] **Step 2: Run test and capture the RED output**

Run: `pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v 2>&1 | tail -60`
Expected: FAIL. Save the finding list — it is the Task 2 worklist. Expected shape (counts from the design's measurement, exact numbers may shift):
- `gcp_project_id` — 10 files
- `cloud_bucket_uri` — 20 files
- `kms_key_uuid` — 3 files
- `aws_account_in_arn` / `aws_account_labelled` / `gcp_service_account_email` — 0 files

- [ ] **Step 3: Confirm pre-commit is clean despite the RED test**

Run: `pixi run pre-commit run --files tests/test_cloud_identifier_scrub.py`
Expected: all hooks Passed. Pre-commit runs ruff/ruff-format/mypy/scan-secrets, not pytest — a RED test is committable, which is what lets the guard land before the cleanup.

- [ ] **Step 4: Commit RED**

```bash
git add tests/test_cloud_identifier_scrub.py
git commit -m "test(scrub): add the tracked-tree identifier lockdown (RED)

Fails on HEAD. The finding list is the Task 2 worklist: gcp_project_id in
10 files, cloud_bucket_uri in 20, kms_key_uuid in 3. AWS account ids and SA
emails are already clean once the patterns are ARN/label-anchored."
```

---

## Task 2: Scrub every tracked concrete identifier

**Goal:** Turn Task 1's lockdown green by replacing every concrete identifier with the established fake or a placeholder.

**Files:**
- Modify: `.aws/policies/skypilot-minimal.template.json:163` (KMS UUID → `<KMS_KEY_ID>`)
- Modify: `tools/quota_burn_lib.py:266` (project id → `kinoforge-prod-deadbeef`)
- Modify: `tests/tools/test_quota_burn_gcp.py`, `tests/tools/test_quota_burn_cli.py`, `tests/tools/test_quota_burn_submit.py` (project id)
- Modify: `PROGRESS.md`, `docs/quota-justification-gcp.md`, `docs/CLOUD-CREDS.md`, `docs/cloud-stores.md`
- Modify: `docs/superpowers/plans/2026-06-10-gpu-quota-utilization-burn.md` (+ its `.tasks.json`), `docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-design.md`
- Modify: `examples/configs/bedrock-nova-reel-t2v.yaml`, `examples/configs/bedrock-luma-ray-t2v.yaml`
- Modify: `tests/core/test_config.py`, `tests/engines/test_bedrock_video.py`, `tests/engines/test_bedrock_video_replay.py`, `tests/live/test_luma_ray_live.py`, `tests/live/test_nova_reel_live.py`, `tests/live/_c33_probe_h_evidence.json`
- Modify: `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md` (pragma, not scrub)
- Modify: any further path the Task 1 RED output names

**Acceptance Criteria:**
- [ ] `test_no_tracked_file_contains_a_concrete_identifier` passes
- [ ] GCP project ids → `kinoforge-prod-deadbeef` (the convention at `tests/stores/test_recording.py:48-50`)
- [ ] Bucket names → `<GCS_BUCKET>` / `<S3_BUCKET>` in docs and example configs; `bkt`-family doubles in tests
- [ ] KMS UUID → `<KMS_KEY_ID>` in the policy file
- [ ] The `kinoforge: allow-identifier` pragma appears ONLY in the F10 section of the verification doc, where quoting the leaked value is the finding itself
- [ ] Full suite still green — the quota-burn tests assert on the project id and must be renamed consistently on both sides
- [ ] `pixi run kinoforge validate --config examples/configs/bedrock-nova-reel-t2v.yaml` still parses (placeholder bucket is a string; validation must not regress)

**Verify:** `pixi run python -m pytest tests/test_cloud_identifier_scrub.py tests/tools/ tests/core/test_config.py tests/engines/ -v` → all pass

**Steps:**

- [ ] **Step 1: Re-run the guard to get the exact current worklist**

```bash
pixi run python -c "
from pathlib import Path
from tools.scan_identifiers import scan_all_tracked_identifiers
for path, f in scan_all_tracked_identifiers(Path('.')):
    print(f'{path}:{f.line_no} [{f.pattern_name}] {f.value}')
"
```

Work from this output, not from the file list above — the list above is a snapshot and may have drifted.

- [ ] **Step 2: Scrub the GCP project id**

The production-code site is the one that matters. `tools/quota_burn_lib.py:266`:

```python
    billing_dataset: str = "kinoforge-prod-deadbeef.all_billing_data",
```

A fake default fails loudly instead of silently pointing at a real project. Then rename every assertion side in `tests/tools/test_quota_burn_{gcp,cli,submit}.py` to match — these tests assert on the literal, so both sides move together or the suite goes red.

Docs (`PROGRESS.md`, `docs/quota-justification-gcp.md`, the two 2026-06-10 superpowers docs and the `.tasks.json`) take the same rename.

- [ ] **Step 3: Scrub bucket names**

Docs and shipped example configs take placeholders — an example config naming the operator's real bucket is the exact defect the brief targets:

```yaml
# examples/configs/bedrock-nova-reel-t2v.yaml
store:
  kind: s3
  bucket: <S3_BUCKET>
```

Tests take the existing doubles (`bkt`, `bucket`) rather than placeholders, because a test needs a syntactically valid bucket name. Update the assertion side in the same edit.

`tests/live/_c33_probe_h_evidence.json` is captured live evidence — replace the bucket name with `<S3_BUCKET>`; nothing asserts on it.

- [ ] **Step 4: Scrub the KMS UUID**

`.aws/policies/skypilot-minimal.template.json`, `KMSLayerW` statement:

```json
      "Resource": [
        "arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/<KMS_KEY_ID>"
      ]
```

`PROGRESS.md`'s occurrence takes the same placeholder.

- [ ] **Step 5: Apply the pragma to the verification doc**

`docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md`, F10 section only. Scrubbing there would make the finding self-referentially meaningless — "the real project id `<GCP_PROJECT>` appears in 9 tracked files" states nothing. Append the pragma to each evidence line:

```
      "Resource": [                                    # kinoforge: allow-identifier
```

Use it nowhere else. Every other occurrence in that doc that is not load-bearing evidence gets scrubbed normally.

- [ ] **Step 6: Run the guard and the affected suites**

Run: `pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v`
Expected: PASS

Run: `pixi run python -m pytest tests/tools/ tests/core/test_config.py tests/engines/ tests/stores/ -v`
Expected: PASS — catches a half-done rename where the fixture moved but the assertion did not.

Run: `pixi run test`
Expected: PASS (full non-live suite).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "fix(scrub): placeholder every concrete cloud identifier

Turns the tracked-tree identifier lockdown green. The load-bearing change
is tools/quota_burn_lib.py:266, a production default naming the real
project's billing dataset; it now takes the kinoforge-prod-deadbeef fake
already established in tests/stores/test_recording.py.

Bucket names were NOT clean, contrary to F10's sweep: two shipped example
configs named the operator's real Bedrock and Nova Reel buckets.

The F10 evidence lines in the verification doc take the
kinoforge: allow-identifier pragma instead of a scrub -- placeholdering the
value there would erase the finding."
```

---

## Task 3: Placeholder-render tool for the AWS policy

**Goal:** Make `.aws/policies/skypilot-minimal.template.json` attachable without ever writing a concrete identifier into the tree.

**Files:**
- Create: `tools/render_aws_policy.py`
- Test: `tests/tools/test_render_aws_policy.py`
- Modify: `.aws/policies/skypilot-minimal.template.json` (`<GCS_KMS_KEYRING>` → `<S3_BUCKET_PREFIX>`)
- Create: `.aws/policies/README.md` (UNVALIDATED banner — not a `_comment` key in the JSON; IAM's policy grammar is closed and rejects arbitrary top-level keys)

**Acceptance Criteria:**
- [ ] `render()` substitutes `<AWS_ACCOUNT>`, `<KMS_KEY_ID>`, `<S3_BUCKET_PREFIX>`
- [ ] Raises `ValueError` naming the survivors if any `<...>` placeholder remains after substitution
- [ ] Raises `ValueError` if the output path resolves inside the repo root — a rendered policy must never become a tracked-file candidate
- [ ] Raises `FileNotFoundError` with a remediation hint when `<KMS_KEY_ID>` is needed and `.aws/kms-test-key.arn` is absent
- [ ] Rendered output parses as JSON and contains no `<`
- [ ] `.aws/policies/skypilot-minimal.template.json` uses `<S3_BUCKET_PREFIX>` in the S3 ARNs; the UNVALIDATED banner lives in the sibling `.aws/policies/README.md`, not a `_comment` key inside the JSON — IAM's policy grammar is closed and rejects arbitrary top-level keys, so that was never an option once tested against the documented grammar
- [ ] The tracked policy file still passes the Task 1 lockdown

**Verify:** `pixi run python -m pytest tests/tools/test_render_aws_policy.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_render_aws_policy.py`:

```python
"""Tests for the AWS scoped-policy renderer.

The renderer exists because a tracked policy file cannot be both
placeholder-clean and directly attachable. Every guarantee below is one
half of that trade.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.render_aws_policy import render, resolve_kms_key_id

_ACCOUNT = "9" + "18273645" + "019"
_KEY_ID = "4b0dbe0c-" + "3a76-401a-" + "ac2e-" + "d0d949b9fa3e"

_TEMPLATE = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "IAMForSkyPilotRoles",
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": ["arn:aws:iam::<AWS_ACCOUNT>:role/skypilot-*"],
            },
            {
                "Sid": "S3KinoforgeBuckets",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": ["arn:aws:s3:::<S3_BUCKET_PREFIX>-*/*"],
            },
            {
                "Sid": "KMSLayerW",
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": ["arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/<KMS_KEY_ID>"],
            },
        ],
    }
)


def test_render_substitutes_all_three_placeholders() -> None:
    """The rendered policy is attachable: no placeholder survives.

    A bug that would fail this: substituting <AWS_ACCOUNT> only at its
    first occurrence, leaving the KMS ARN malformed and the attach 400ing
    with an unhelpful message.
    """
    out = render(
        _TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
    )
    assert "<" not in out
    parsed = json.loads(out)
    resources = [s["Resource"][0] for s in parsed["Statement"]]
    assert resources == [
        f"arn:aws:iam::{_ACCOUNT}:role/skypilot-*",
        "arn:aws:s3:::kf-example-*/*",
        f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/{_KEY_ID}",
    ]


def test_render_refuses_a_surviving_placeholder() -> None:
    """A half-rendered policy must never reach `aws iam put-user-policy`.

    A bug that would fail this: adding a fourth placeholder to the tracked
    template and forgetting to teach the renderer about it — AWS would
    then reject the attach with a malformed-ARN error that points nowhere
    near the actual cause.
    """
    template = _TEMPLATE.replace("skypilot-*", "<UNEXPECTED>-*")
    with pytest.raises(ValueError, match="<UNEXPECTED>"):
        render(
            template, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="kf-example"
        )


def test_render_rejects_an_empty_bucket_prefix() -> None:
    """An empty prefix silently widens the S3 grant to `arn:aws:s3:::-*`.

    A bug that would fail this: defaulting `bucket_prefix` to "" and
    producing a policy whose scope is not what the operator thinks.
    """
    with pytest.raises(ValueError, match="bucket_prefix"):
        render(_TEMPLATE, account=_ACCOUNT, kms_key_id=_KEY_ID, bucket_prefix="")


def test_resolve_kms_key_id_reads_the_gitignored_arn_file(tmp_path: Path) -> None:
    """The key id comes from the untracked ARN file, never from the tree.

    Same file `tools/cloud_perms_probe.py:68` already reads.
    """
    arn_file = tmp_path / "kms-test-key.arn"
    arn_file.write_text(f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/{_KEY_ID}\n")
    assert resolve_kms_key_id(arn_file) == _KEY_ID


def test_resolve_kms_key_id_errors_with_a_remediation_hint(tmp_path: Path) -> None:
    """A missing ARN file must say what to do, not just what failed.

    A bug that would fail this: letting the bare FileNotFoundError escape,
    which tells a new operator nothing about `tools/bootstrap_kms.py`.
    """
    with pytest.raises(FileNotFoundError, match="kms-test-key.arn"):
        resolve_kms_key_id(tmp_path / "absent.arn")
```

Add to the same file the in-repo-output guard, which needs the CLI entry point:

```python
def test_main_refuses_to_write_inside_the_repo(tmp_path: Path) -> None:
    """A rendered policy inside the tree becomes a tracked-file candidate.

    A bug that would fail this: accepting `--out .aws/policies/rendered.json`,
    which reintroduces exactly the concrete-identifier leak the sibling
    scrub test exists to stop.
    """
    from tools.render_aws_policy import main

    with pytest.raises(ValueError, match="inside the repository"):
        main(
            [
                "--account",
                _ACCOUNT,
                "--kms-key-id",
                _KEY_ID,
                "--bucket-prefix",
                "kf-example",
                "--out",
                str(Path(__file__).resolve().parents[2] / "rendered.json"),
            ]
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/tools/test_render_aws_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.render_aws_policy'`

- [ ] **Step 3: Write minimal implementation**

Create `tools/render_aws_policy.py`:

```python
"""Render `.aws/policies/skypilot-minimal.template.json` into an attachable policy.

The tracked policy carries `<AWS_ACCOUNT>`, `<KMS_KEY_ID>` and
`<S3_BUCKET_PREFIX>` placeholders, so it cannot be handed to
`aws iam put-user-policy` directly — AWS rejects a malformed ARN. This
module substitutes them and writes the result OUTSIDE the repository, which
is what lets the tracked file stay clean under
`tests/test_cloud_identifier_scrub.py` while the attach still works.

Usage::

    python tools/render_aws_policy.py \\
      --bucket-prefix my-prefix \\
      --out /tmp/skypilot-minimal.rendered.json

`--account` defaults to the caller's own account via `sts:GetCallerIdentity`;
`--kms-key-id` defaults to the key id parsed out of the gitignored
`.aws/kms-test-key.arn`.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_POLICY_PATH: Path = _REPO_ROOT / ".aws" / "policies" / "skypilot-minimal.template.json"
_KMS_ARN_FILE: Path = _REPO_ROOT / ".aws" / "kms-test-key.arn"

_PLACEHOLDER_RE = re.compile(r"<[A-Z_]+>")


def render(
    policy_text: str, *, account: str, kms_key_id: str, bucket_prefix: str
) -> str:
    """Substitute every placeholder in *policy_text*.

    Args:
        policy_text: Raw contents of the tracked policy template.
        account: 12-digit AWS account id.
        kms_key_id: Bare KMS key UUID (not the full ARN).
        bucket_prefix: S3 bucket-name prefix the policy is scoped to.

    Returns:
        The rendered policy JSON as text.

    Raises:
        ValueError: *bucket_prefix* is empty, or a placeholder survived
            substitution.
    """
    if not bucket_prefix:
        raise ValueError("bucket_prefix must be non-empty; an empty prefix widens the S3 grant")

    out = (
        policy_text.replace("<AWS_ACCOUNT>", account)
        .replace("<KMS_KEY_ID>", kms_key_id)
        .replace("<S3_BUCKET_PREFIX>", bucket_prefix)
    )
    survivors = sorted(set(_PLACEHOLDER_RE.findall(out)))
    if survivors:
        raise ValueError(
            f"placeholder(s) survived rendering: {', '.join(survivors)}. "
            "Teach render() about them before attaching — AWS rejects a "
            "malformed ARN with an error that points nowhere near the cause."
        )
    json.loads(out)  # fail here, not at the AWS API boundary
    return out


def resolve_kms_key_id(arn_file: Path = _KMS_ARN_FILE) -> str:
    """Parse the bare key UUID out of the gitignored KMS ARN file.

    Args:
        arn_file: Path to the file holding the full KMS key ARN. Defaults
            to `.aws/kms-test-key.arn`, the same file
            `tools/cloud_perms_probe.py:68` reads.

    Returns:
        The bare key UUID.

    Raises:
        FileNotFoundError: The ARN file is absent.
        ValueError: The file contents are not a KMS key ARN.
    """
    if not arn_file.exists():
        raise FileNotFoundError(
            f"{arn_file} is absent. It is gitignored by design; create it with "
            "`pixi run python tools/bootstrap_kms.py`, or pass --kms-key-id."
        )
    text = arn_file.read_text().strip()
    _, _, key_id = text.partition(":key/")
    if not key_id:
        raise ValueError(f"{arn_file} does not contain a KMS key ARN")
    return key_id


def _default_account() -> str:
    """Return the caller's AWS account id via STS.

    Returns:
        The 12-digit account id.
    """
    import boto3

    identity: dict[str, Any] = boto3.client("sts").get_caller_identity()
    return str(identity["Account"])


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code.

    Raises:
        ValueError: The requested output path is inside the repository.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", default=None)
    parser.add_argument("--kms-key-id", default=None)
    parser.add_argument("--bucket-prefix", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out_path = Path(args.out).resolve()
    if out_path.is_relative_to(_REPO_ROOT):
        raise ValueError(
            f"refusing to write {out_path} inside the repository — a rendered "
            "policy holds concrete identifiers and must never become a tracked "
            "file. Use a path under /tmp."
        )

    account = args.account or _default_account()
    kms_key_id = args.kms_key_id or resolve_kms_key_id()
    rendered = render(
        _POLICY_PATH.read_text(),
        account=account,
        kms_key_id=kms_key_id,
        bucket_prefix=args.bucket_prefix,
    )
    out_path.write_text(rendered)
    out_path.chmod(0o600)
    print(f"rendered policy written to {out_path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/tools/test_render_aws_policy.py -v`
Expected: PASS

- [ ] **Step 5: Fix the policy file's placeholder names and add the banner**

In `.aws/policies/skypilot-minimal.template.json`, `S3KinoforgeBuckets` statement, replace both `<GCS_KMS_KEYRING>` occurrences:

```json
      "Resource": [
        "arn:aws:s3:::<S3_BUCKET_PREFIX>-*",
        "arn:aws:s3:::<S3_BUCKET_PREFIX>-*/*",
        "arn:aws:s3:::skypilot-*",
        "arn:aws:s3:::skypilot-*/*"
      ]
```

A GCS-flavoured placeholder naming an S3 bucket prefix is the F10 cosmetic bug; `<S3_BUCKET_PREFIX>` is what `.aws/README.md:65,80-81,93,115` already uses.

Do NOT add a `_comment` key to the JSON. IAM's policy grammar is closed
(`policy = { <version_block?>, <id_block?>, <statement_block> }`,
documented, and identity-based policies explicitly forbid even the
optional `Id` block) — an arbitrary top-level key is a plausible
`MalformedPolicyDocument` rejection, not a safe bet to embed in the
attachable artifact. Create the sibling file instead:

`.aws/policies/README.md`:

```markdown
# AWS scoped IAM policy templates

## `skypilot-minimal.template.json`

**UNVALIDATED against a real SkyPilot launch.** Simulate-validated only
(`tools/validate_scoped_policy.py`). Placeholders are rendered by
`tools/render_aws_policy.py`; do not attach this file directly.
```

(Full text — including why the banner lives here and not as a `_comment`
key — is written once `render_aws_policy.py` exists, so it can name it by
path. See the shipped `.aws/policies/README.md` for the actual wording.)

- [ ] **Step 6: Verify the rendered output still parses cleanly**

Run:

```bash
pixi run python tools/render_aws_policy.py \
  --account 123456789012 \
  --kms-key-id 00000000-0000-0000-0000-000000000000 \
  --bucket-prefix kf-example \
  --out /tmp/render-check.json
pixi run python -c "import json;d=json.load(open('/tmp/render-check.json'));print(sorted(d))"
```

Expected: `['Statement', 'Version']`, no exception — the JSON carries no
banner key at all; the banner lives in `.aws/policies/README.md`. Confirm
that file exists and states UNVALIDATED:

```bash
test -f .aws/policies/README.md && grep -q UNVALIDATED .aws/policies/README.md
echo "readme ok: $?"
```

Expected: `readme ok: 0`.

Run: `pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v`
Expected: PASS — the policy file is still placeholder-clean.

- [ ] **Step 7: Commit**

```bash
git add tools/render_aws_policy.py tests/tools/test_render_aws_policy.py .aws/policies/skypilot-minimal.template.json .aws/policies/README.md
git commit -m "feat(aws): render the scoped policy instead of attaching it raw

The tracked policy carries placeholders, so
\`put-user-policy --policy-document file://...\` has never been able to work
against it. The renderer substitutes account, KMS key id and bucket prefix,
refuses to emit a half-rendered policy, and refuses to write inside the repo
so the concrete result never becomes a tracked file.

Also retires <GCS_KMS_KEYRING> -- a GCS-flavoured placeholder naming an S3
bucket prefix -- for <S3_BUCKET_PREFIX>, matching .aws/README.md."
```

---

## Task 4: GCP minimum roles + the securityAdmin revoke

**Goal:** Give GCP the counterpart to the AWS policy file, and turn the `securityAdmin` annotation from a feature into a revoke instruction.

**Files:**
- Create: `.gcp/policies/roles.txt`
- Modify: `.gcp/README.md:22-35` (the Service account section)
- Modify: `.gitignore` (unignore `.gcp/policies/`, mirroring the existing `!.aws/policies/` at line 46)

**Acceptance Criteria:**
- [ ] `.gcp/policies/roles.txt` lists exactly `roles/compute.instanceAdmin.v1`, `roles/iam.serviceAccountUser`, `roles/storage.admin`
- [ ] The file states the `compute.admin` → `compute.instanceAdmin.v1` drop and its grounding (`tools/cloud_perms_probe.py:315-318`)
- [ ] The file states the firewall/VPC caveat and carries the UNVALIDATED banner
- [ ] The file documents grant-bootstrap-revoke for `roles/iam.securityAdmin` with the exact revoke command
- [ ] `.gcp/README.md` no longer describes `securityAdmin` as a self-grant convenience
- [ ] `.gcp/policies/roles.txt` is tracked (`git ls-files` shows it) and passes the Task 1 lockdown
- [ ] `.gcp/kinoforge-sa.json`, `.gcp/gcloud-config/`, `.gcp/perms-snapshot.json` remain ignored

**Verify:** `git check-ignore -v .gcp/policies/roles.txt` → exit 1 (not ignored); `git check-ignore -v .gcp/kinoforge-sa.json` → exit 0 (still ignored)

**Steps:**

- [ ] **Step 1: Unignore the new policies directory**

`.gitignore` currently has, around line 40-46:

```
.gcp/*
.aws/*
...
!.gcp/README.md
!.aws/README.md
...
!.aws/policies/
```

Add directly beneath the `!.aws/policies/` line:

```
!.gcp/policies/
```

- [ ] **Step 2: Verify the ignore rules do what you think**

```bash
git check-ignore -v .gcp/policies/roles.txt ; echo "roles.txt ignored? rc=$?"
git check-ignore -v .gcp/kinoforge-sa.json ; echo "sa key ignored? rc=$?"
git check-ignore -v .gcp/perms-snapshot.json ; echo "snapshot ignored? rc=$?"
```

Expected: `roles.txt` rc=1 (NOT ignored — it is meant to be tracked); the SA key and the snapshot both rc=0 (still ignored). If the SA key comes back rc=1, stop — the negation is too broad and would track a credential.

- [ ] **Step 3: Write `.gcp/policies/roles.txt`**

```
# kinoforge — minimum GCP roles for a SkyPilot launch plus GCS.
#
# Counterpart to .aws/policies/skypilot-minimal.template.json. Grant these to the
# runner service account and nothing else.
#
# STATUS: UNVALIDATED against a real SkyPilot launch. Checked only by
# tools/validate_scoped_policy.py, which calls projects.testIamPermissions --
# that proves the roles carry the permissions claimed, not that a launch
# succeeds under them. See the caveat below.

roles/compute.instanceAdmin.v1
roles/iam.serviceAccountUser
roles/storage.admin

# --- Why instanceAdmin.v1 and not compute.admin -----------------------------
#
# The project's own permission gate has never asked for compute.admin.
# tools/cloud_perms_probe.py:315-318 declares:
#
#     _GCP_REQUIRED_ROLES = (
#         "roles/compute.instanceAdmin.v1",
#         "roles/iam.serviceAccountUser",
#     )
#
# roles/storage.admin is added here because the probe covers compute only,
# and kinoforge's GCS store needs bucket + object operations.
#
# CAVEAT: compute.instanceAdmin.v1 does NOT grant firewall or VPC mutation,
# and SkyPilot opens ports on the instances it launches. That is the most
# likely first failure of a real launch under this role set, and it is the
# concrete reason for the UNVALIDATED banner above. If a launch fails on a
# compute.firewalls.* permission, add roles/compute.securityAdmin (firewall
# rules only) rather than widening back to compute.admin.
#
# --- Bootstrap-only: roles/iam.securityAdmin --------------------------------
#
# roles/iam.securityAdmin lets an identity modify IAM bindings on the
# project. An identity that can modify IAM bindings can grant itself
# anything, so this is project-owner under a quieter name -- and the runner's
# key lives in a .env on a laptop.
#
# It is needed ONLY while binding the roles above, and NEVER at runtime:
# nothing in src/ or tools/ calls setIamPolicy or add-iam-policy-binding.
#
# Grant it, bootstrap, revoke it:
#
#   PID=$(gcloud config get-value project)
#   SA=kinoforge-runner@${PID}.iam.gserviceaccount.com
#
#   # 1. grant (as an operator with resourcemanager.projectIamAdmin)
#   gcloud projects add-iam-policy-binding "$PID" \
#     --member="serviceAccount:$SA" --role=roles/iam.securityAdmin
#
#   # 2. bootstrap: bind the runtime roles listed above
#   while read -r ROLE; do
#     case "$ROLE" in ''|'#'*) continue ;; esac
#     gcloud projects add-iam-policy-binding "$PID" \
#       --member="serviceAccount:$SA" --role="$ROLE"
#   done < .gcp/policies/roles.txt
#
#   # 3. REVOKE -- do not skip this step
#   gcloud projects remove-iam-policy-binding "$PID" \
#     --member="serviceAccount:$SA" --role=roles/iam.securityAdmin
#
# roles/iam.serviceAccountAdmin and roles/serviceusage.serviceUsageAdmin are
# bootstrap-only on the same terms: bound today, referenced by no code, and
# to be revoked once the runtime roles above are in place. roles/viewer is
# not needed at all.
#
# If your runner service account still has roles/iam.securityAdmin bound
# right now, run step 3 today. Confirm with:
#
#   gcloud projects get-iam-policy "$PID" \
#     --flatten="bindings[].members" \
#     --filter="bindings.members:$SA" \
#     --format="value(bindings.role)"
```

- [ ] **Step 4: Rewrite `.gcp/README.md` Service account section**

Replace lines 28-35 (the `Roles (granted on the project)` list) with:

```markdown
- Roles — see `.gcp/policies/roles.txt` for the authoritative list and the
  rationale. Runtime set:
  - `roles/compute.instanceAdmin.v1`
  - `roles/iam.serviceAccountUser`
  - `roles/storage.admin`
- **Bootstrap-only, revoke after binding:** `roles/iam.securityAdmin`,
  `roles/iam.serviceAccountAdmin`, `roles/serviceusage.serviceUsageAdmin`.

  `roles/iam.securityAdmin` can modify IAM bindings, so an identity holding
  it can grant itself any role in the project — project-owner under a
  quieter name, with the key sitting in a `.env` on a laptop. It is required
  only while binding the runtime roles and never at runtime; nothing in
  `src/` or `tools/` calls `setIamPolicy`. The revoke command is in
  `.gcp/policies/roles.txt`. **If it is still bound, revoke it now.**
```

The current annotation — `← self-grant capability; additional roles can be added without re-auth` — is deleted. It describes the risk as a convenience.

- [ ] **Step 5: Verify**

Run: `pixi run python -m pytest tests/test_cloud_identifier_scrub.py tests/test_source_audit.py -v`
Expected: PASS — the new file must not introduce a concrete identifier or a credential shape.

Run: `git status --short .gcp/`
Expected: `roles.txt` shows as untracked-and-addable; no SA key, config dir, or snapshot appears.

- [ ] **Step 6: Commit**

```bash
git add .gitignore .gcp/policies/roles.txt .gcp/README.md
git commit -m "docs(gcp): publish the minimum role set, revoke securityAdmin

roles.txt is the GCP counterpart to .aws/policies/skypilot-minimal.template.json.
compute.admin drops to compute.instanceAdmin.v1 on the authority of
tools/cloud_perms_probe.py:315-318, which is what the project's own
permission gate has always required.

roles/iam.securityAdmin is bootstrap-only: nothing in src/ or tools/ calls
setIamPolicy. .gcp/README.md described it as a self-grant convenience; it is
now documented as grant-bootstrap-revoke with the revoke command inline."
```

---

## Task 5: Invert the AWS and GCP onboarding order

**Goal:** The scoped path is what a new operator reads and pastes first; the wide grants stay available, second, labelled with their consequence.

**Files:**
- Modify: `.env.example:54-94` (GCP and AWS blocks)
- Modify: `.aws/README.md:19-41` (Bootstrap section)

**Acceptance Criteria:**
- [ ] `.env.example`'s AWS block leads with `render_aws_policy.py` + `put-user-policy`
- [ ] The FullAccess recipe appears below, labelled a bootstrap shortcut, with its consequence stated
- [ ] `IAMFullAccess` and `ServiceQuotasFullAccess` are named as what the project actually ran on (per F9), and marked not-recommended
- [ ] `.env.example`'s GCP block loops over `.gcp/policies/roles.txt` instead of hardcoding `roles/compute.admin`
- [ ] `.env.example`'s GCP block includes the `securityAdmin` revoke step
- [ ] `.aws/README.md` bootstrap step 1 no longer says "attach `AmazonS3FullAccess`"
- [ ] `.env.example` still parses: `pixi run python -c "from dotenv import dotenv_values; print(len(dotenv_values('.env.example')))"` prints the key count without raising

**Verify:** `pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v && pixi run python -c "from dotenv import dotenv_values; assert dotenv_values('.env.example')"` → PASS

**Steps:**

- [ ] **Step 1: Rewrite the GCP block** (`.env.example:54-70`)

Replace the `To mint a key:` recipe with:

```
# To mint a key with least privilege:
#   PID=$(gcloud config get-value project)
#   SA=kinoforge-runner@${PID}.iam.gserviceaccount.com
#   gcloud iam service-accounts create kinoforge-runner
#
#   # Bind exactly the roles in .gcp/policies/roles.txt -- that file is the
#   # authoritative list and explains why each one is there.
#   while read -r ROLE; do
#     case "$ROLE" in ''|'#'*) continue ;; esac
#     gcloud projects add-iam-policy-binding "$PID" \
#       --member="serviceAccount:$SA" --role="$ROLE"
#   done < .gcp/policies/roles.txt
#
#   gcloud iam service-accounts keys create .gcp/kinoforge-sa.json --iam-account="$SA"
#   chmod 600 .gcp/kinoforge-sa.json    # .gcp/ is in .gitignore
#
# Binding those roles needs roles/iam.securityAdmin (or an operator account
# with projectIamAdmin). If you grant securityAdmin to the runner SA to do
# it, REVOKE IT IMMEDIATELY AFTER -- an identity that can modify IAM
# bindings can grant itself anything, and this key lives on a laptop:
#   gcloud projects remove-iam-policy-binding "$PID" \
#     --member="serviceAccount:$SA" --role=roles/iam.securityAdmin
```

- [ ] **Step 2: Rewrite the AWS block** (`.env.example:84-91`)

Replace the FullAccess recipe with the scoped path first:

```
# To mint a headless IAM user with least privilege (the default path):
#   aws iam create-user --user-name kinoforge-runner
#
#   # The tracked policy carries placeholders, so it cannot be attached
#   # directly. Render it first; the rendered file holds concrete
#   # identifiers and must stay outside the repo.
#   pixi run python tools/render_aws_policy.py \
#     --bucket-prefix <your-bucket-prefix> \
#     --out /tmp/skypilot-minimal.rendered.json
#
#   aws iam put-user-policy --user-name kinoforge-runner \
#     --policy-name KinoforgeSkypilotMinimal \
#     --policy-document file:///tmp/skypilot-minimal.rendered.json
#
#   aws iam create-access-key --user-name kinoforge-runner   # prints key + secret
#
# Confirm the scope is sufficient before deleting the rendered file:
#   pixi run python tools/validate_scoped_policy.py --cloud aws
#
# ---------------------------------------------------------------------------
# BOOTSTRAP SHORTCUT -- acceptable ONLY in an AWS account holding nothing else
# ---------------------------------------------------------------------------
# These three grants let this key launch any instance type in any region on
# your bill, read and write every bucket in the account, and invoke every
# Bedrock model. Use them only to get unblocked, and swap to the scoped
# policy above before the account holds anything you care about.
#
#   for P in AmazonEC2FullAccess AmazonS3FullAccess AmazonBedrockFullAccess; do
#     aws iam attach-user-policy --user-name kinoforge-runner \
#         --policy-arn arn:aws:iam::aws:policy/$P
#   done
#
# For the record: this project actually ran on AmazonEC2FullAccess +
# AmazonS3FullAccess + IAMFullAccess + ServiceQuotasFullAccess. IAMFullAccess
# in particular lets the key rewrite its own permissions. Reproducing that set
# is NOT recommended; it is documented so the history is not misleading.
```

- [ ] **Step 3: Rewrite `.aws/README.md` bootstrap step 1**

Replace lines 21-23:

```markdown
1. AWS Console → IAM → Users → **Add user**
   - User name: `kinoforge-ci`
   - Permissions: **none yet.** Create the user bare, then attach the scoped
     policy via the CLI path in "SkyPilot policy — apply instructions" below.
     `AmazonS3FullAccess` is the fallback if you are blocked, not the default —
     see the same section.
```

Then, in the "SkyPilot policy — apply instructions" section (currently line 88), put the CLI path above the existing console-paste steps:

```markdown
Preferred — CLI, one command after rendering:

```bash
pixi run python tools/render_aws_policy.py \
  --bucket-prefix <your-bucket-prefix> \
  --out /tmp/skypilot-minimal.rendered.json
aws iam put-user-policy --user-name kinoforge-ci \
  --policy-name KinoforgeSkypilotMinimal \
  --policy-document file:///tmp/skypilot-minimal.rendered.json
rm /tmp/skypilot-minimal.rendered.json
```

The policy file is NOT attachable as tracked — it carries `<AWS_ACCOUNT>`,
`<KMS_KEY_ID>` and `<S3_BUCKET_PREFIX>` placeholders, and AWS rejects a
malformed ARN. Render first. Alternative — GUI:
```

…leaving the existing numbered console steps in place beneath, but pointing
step 4 at the *rendered* file rather than the tracked one.

- [ ] **Step 4: Verify**

```bash
pixi run python -c "from dotenv import dotenv_values; d=dotenv_values('.env.example'); print(len(d), 'keys')"
pixi run python -m pytest tests/test_cloud_identifier_scrub.py tests/test_source_audit.py -v
```

Expected: key count prints (39 today), both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add .env.example .aws/README.md
git commit -m "docs(onboarding): lead with the scoped policy, not FullAccess

Whichever instruction appears first is what gets pasted, so the scoped
policy has effectively not existed. Both .env.example and .aws/README.md now
open with render-and-attach; the FullAccess recipe stays below with its
consequence stated rather than a 'tighten later' parenthetical.

Also records the policy set the project actually ran on -- which included
IAMFullAccess -- so the documented environment matches the real one."
```

---

## Task 6: Billing-alert onboarding step

**Goal:** Put a spending ceiling in the onboarding path, because nothing in this repo can enforce one.

**Files:**
- Modify: `.env.example` — new section immediately after the compute-provider block, before "Artifact storage"

**Acceptance Criteria:**
- [ ] The section sits between credential creation and artifact storage, not appended at the end
- [ ] AWS Budgets and GCP Budgets-and-alerts both have concrete console paths
- [ ] Per-provider caps for RunPod, Modal and Replicate are named
- [ ] The section states why a console budget is the only real ceiling, referencing what kinoforge cannot do
- [ ] `GCP_BILLING_ACCOUNT_ID` is cross-referenced, not duplicated
- [ ] `.env.example` still parses

**Verify:** `pixi run python -c "from dotenv import dotenv_values; assert dotenv_values('.env.example')"` → no output, exit 0

**Steps:**

- [ ] **Step 1: Insert the section** (after the Modal block, before `# Artifact storage`)

```
# -----------------------------------------------------------------------------
# Billing alerts — set these up NOW, alongside the keys above
# -----------------------------------------------------------------------------
# Nothing in this repository can enforce a monthly ceiling. The reasons are
# concrete, not hypothetical:
#   - `est_spend` and `kinoforge list` grow on wall-clock alone. They look
#     identical whether the GPU is at 100% or dead at 0%.
#   - The sweeper reaps only what it can see. An instance killed mid-launch,
#     before the ledger write, is invisible to every kinoforge command while
#     still billing.
#   - A scoped credential limits WHAT can be launched, never HOW MUCH.
#
# The cloud console is the only place a hard ceiling exists. Do this once,
# per account, before the first live run:
#
# AWS:    Billing and Cost Management → Budgets → Create budget
#         → Cost budget → monthly amount → alert at 50% / 80% / 100%
#         → email recipient. Also enable "Receive Free Tier alerts".
# GCP:    Billing → Budgets & alerts → Create budget → scope to the project
#         → monthly amount → thresholds 50% / 90% / 100%.
#         The budget attaches to GCP_BILLING_ACCOUNT_ID (see the GCP billing
#         section at the bottom of this file). A budget alert does NOT stop
#         spend by itself — pair it with a Pub/Sub kill switch if you need
#         enforcement rather than notification.
# Azure:  Cost Management + Billing → Budgets → Add.
#
# Per-provider caps, where the provider offers one:
# RunPod:    console → Billing → set a spend limit and auto-top-up ceiling.
# Modal:     Settings → Usage & Billing → spend limit.
# Replicate: Account → Billing → spending limit.
# Runway / Luma / fal: prepaid credit only — the balance IS the ceiling.
#
# There is no env var here on purpose. This is console-only configuration,
# and putting it in the onboarding path is the only way it happens before
# an incident rather than after one.
```

- [ ] **Step 2: Verify**

```bash
pixi run python -c "from dotenv import dotenv_values; d=dotenv_values('.env.example'); print(len(d), 'keys')"
pixi run python -m pytest tests/test_cloud_identifier_scrub.py -v
```

Expected: same key count as before this task (the section adds only comments), test PASS.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(onboarding): add billing alerts alongside credential creation

Placed between key creation and artifact storage so it reads as part of
minting a credential rather than as a post-incident action. States plainly
why the console is the only real ceiling: est_spend is wall-clock, the
sweeper reaps only what the ledger knows about, and a scoped credential
bounds what launches, never how much."
```

---

## Task 7: `.env.example` drift test

**Goal:** Catch documentation drift in both directions — keys documented but consumed by nothing, and operator-facing vars consumed but documented nowhere.

**Files:**
- Create: `tests/test_env_example_drift.py`
- Modify: `.env.example` (add `# UNIMPLEMENTED` markers to 11 keys; add `KINOFORGE_S3_BUCKET`)

**Acceptance Criteria:**
- [ ] `.env.example` parses through `dotenv_values` — the same parser `kinoforge.core.dotenv_loader:57` uses
- [ ] `RUNPOD_TERMINATE_KEY=${RUNPOD_API_KEY}` is asserted to expand, not just to be present
- [ ] Every `KEY=` is referenced in tracked non-doc source, or carries `# UNIMPLEMENTED` / `# OPTIONAL` on the line directly above
- [ ] The 11 currently-unconsumed keys (Azure + R2 + B2 store vars) get `# UNIMPLEMENTED` markers
- [ ] `KINOFORGE_S3_BUCKET` is added to `.env.example` — it is read at `src/kinoforge/stores/s3/__init__.py:239` and documented nowhere
- [ ] The reverse check runs against a small hand-curated `_OPERATOR_FACING_VARS`, not all `KINOFORGE_*`

**Verify:** `pixi run python -m pytest tests/test_env_example_drift.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/test_env_example_drift.py`:

```python
"""`.env.example` must stay in sync with what the code actually reads.

Two directions, two different failure modes. A key documented but consumed
by nothing sends an operator hunting for a credential they do not need. A
var consumed but documented nowhere makes a feature look broken.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE: Path = _REPO_ROOT / ".env.example"

_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
_MARKER_RE = re.compile(r"#\s*(UNIMPLEMENTED|OPTIONAL)\b")

# Directories whose mention of a var does not count as consumption — a doc
# naming a key proves nothing about whether code reads it.
_DOC_PREFIXES = ("docs/", "PROGRESS.md", "README.md", "successful-generations.md")

# Vars an operator must set to use a documented kinoforge feature. Hand
# curated on purpose: adding a member is a deliberate act, which is what
# makes the reverse check meaningful. Internal knobs (KINOFORGE_LIVE_TESTS,
# KINOFORGE_DIAG_BUCKET, KINOFORGE_PROVISION_B64_*, KINOFORGE_SKIP_*) are
# out of scope by construction — they are not credentials or endpoints an
# operator configures.
_OPERATOR_FACING_VARS = frozenset(
    {
        "HF_TOKEN",
        "RUNPOD_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KINOFORGE_GCS_BUCKET",
        "KINOFORGE_S3_BUCKET",
        "GCP_BILLING_ACCOUNT_ID",
    }
)


def _tracked_source_text() -> str:
    """Return the concatenated text of every tracked non-doc file.

    Returns:
        Concatenation of tracked file contents, excluding `.env.example`
        itself and the doc surfaces in :data:`_DOC_PREFIXES`.
    """
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    chunks: list[str] = []
    for rel in listing.split("\0"):
        if not rel or rel == ".env.example":
            continue
        if rel.startswith(_DOC_PREFIXES):
            continue
        raw = (_REPO_ROOT / rel)
        try:
            data = raw.read_bytes()
        except FileNotFoundError:
            continue
        if b"\x00" in data[:8000]:
            continue
        chunks.append(data.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _keys_with_marker() -> dict[str, bool]:
    """Map each documented key to whether it carries an exemption marker.

    The marker must sit on the line directly above the `KEY=` line. An
    inline trailing comment is not accepted: `KEY= # UNIMPLEMENTED` parses
    ambiguously across dotenv implementations.

    Returns:
        `{key: has_marker}` for every `KEY=` in `.env.example`.
    """
    lines = _ENV_EXAMPLE.read_text().splitlines()
    out: dict[str, bool] = {}
    for idx, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if not match:
            continue
        above = lines[idx - 1] if idx else ""
        out[match.group(1)] = bool(_MARKER_RE.search(above))
    return out


def test_env_example_parses_as_dotenv() -> None:
    """The template must load through the same parser the CLI uses.

    A bug that would fail this: an unquoted `#` inside a value, or a stray
    backtick, which turns a documented key into a silently-missing one at
    runtime.
    """
    parsed = dotenv_values(_ENV_EXAMPLE)
    assert parsed, ".env.example produced no keys"
    assert "RUNPOD_API_KEY" in parsed


def test_runpod_terminate_key_expands_rather_than_being_literal() -> None:
    """The `${RUNPOD_API_KEY}` reference must actually interpolate.

    `.env.example:44-52` promises the terminate key reuses the main key via
    expansion. A bug that would fail this: quoting the value so dotenv
    treats `${RUNPOD_API_KEY}` as a literal string, which would embed the
    seven characters `${RUNP...` into pod env instead of the key.
    """
    parsed = dotenv_values(_ENV_EXAMPLE)
    assert "$" not in (parsed.get("RUNPOD_TERMINATE_KEY") or "")


def test_every_documented_key_is_consumed_or_marked() -> None:
    """A documented key with no consumer sends operators hunting for nothing.

    A bug that would fail this: adding a credential block for a provider
    whose adapter was never written, with no marker saying so.
    """
    source = _tracked_source_text()
    unexplained = [
        key
        for key, marked in _keys_with_marker().items()
        if not marked and key not in source
    ]
    assert not unexplained, (
        f"{len(unexplained)} key(s) in .env.example are read by no tracked "
        f"source file and carry no marker: {sorted(unexplained)}\n"
        "Fix: either wire the key up, or put "
        "`# UNIMPLEMENTED — no kinoforge code reads this yet` on the line "
        "directly above it."
    )


def test_every_operator_facing_var_is_documented() -> None:
    """A var the code reads but the template omits makes a feature look broken.

    Seeded by KINOFORGE_S3_BUCKET, read at
    `src/kinoforge/stores/s3/__init__.py:239` and documented nowhere before
    this test existed.

    A bug that would fail this: adding a new store that reads
    `KINOFORGE_<X>_BUCKET` and forgetting the `.env.example` entry.
    """
    documented = set(_keys_with_marker())
    missing = sorted(_OPERATOR_FACING_VARS - documented)
    assert not missing, (
        f"operator-facing var(s) absent from .env.example: {missing}\n"
        "Fix: add a documented block for each, or drop it from "
        "_OPERATOR_FACING_VARS if it is not something an operator sets."
    )


def test_curated_list_only_holds_vars_the_code_reads() -> None:
    """Guards the curated list against its own drift.

    Without this, a var could be deleted from the codebase and linger in
    `_OPERATOR_FACING_VARS` forever, forcing `.env.example` to document
    something that no longer exists.
    """
    source = _tracked_source_text()
    stale = sorted(v for v in _OPERATOR_FACING_VARS if v not in source)
    assert not stale, f"_OPERATOR_FACING_VARS names vars no tracked source reads: {stale}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/test_env_example_drift.py -v`
Expected: 2 failures —
- `test_every_documented_key_is_consumed_or_marked` names 11 keys: `AZURE_SUBSCRIPTION_ID`, `AZURE_STORAGE_CONNECTION_STRING`, `KINOFORGE_AZURE_CONTAINER`, `KINOFORGE_AZURE_PREFIX`, `KINOFORGE_R2_ACCOUNT_ID`, `KINOFORGE_R2_BUCKET`, `KINOFORGE_R2_PREFIX`, `B2_APPLICATION_KEY_ID`, `KINOFORGE_B2_S3_ENDPOINT`, `KINOFORGE_B2_BUCKET`, `KINOFORGE_B2_PREFIX`
- `test_every_operator_facing_var_is_documented` names `KINOFORGE_S3_BUCKET`

- [ ] **Step 3: Add markers to the unconsumed keys**

For each of the 11, put the marker on the line directly above. Example, in the R2 block:

```
# UNIMPLEMENTED — no kinoforge code reads this yet (no R2 store adapter).
KINOFORGE_R2_ACCOUNT_ID=
KINOFORGE_R2_ACCESS_KEY_ID=
# UNIMPLEMENTED — no kinoforge code reads this yet (no R2 store adapter).
KINOFORGE_R2_BUCKET=
# UNIMPLEMENTED — no kinoforge code reads this yet (no R2 store adapter).
KINOFORGE_R2_PREFIX=
```

Note `KINOFORGE_R2_ACCESS_KEY_ID` and `KINOFORGE_R2_SECRET_ACCESS_KEY` ARE referenced in tracked source and need no marker — mark only what the RED output actually named.

- [ ] **Step 4: Document `KINOFORGE_S3_BUCKET`**

In the "Artifact storage" section, beside the GCS block:

```
# S3 — AWS Simple Storage Service  (auths via the AWS chain above)
# Bucket must exist; the credentials above need read + write on it.
KINOFORGE_S3_BUCKET=
KINOFORGE_S3_PREFIX=
```

Check whether `KINOFORGE_S3_PREFIX` is actually read before adding it — if
`rg -n 'KINOFORGE_S3_PREFIX' src/` finds nothing, either omit it or give it
an `# UNIMPLEMENTED` marker. Do not document a key on the assumption that a
sibling exists.

- [ ] **Step 5: Run test to verify it passes**

Run: `pixi run python -m pytest tests/test_env_example_drift.py -v`
Expected: PASS (all 5)

Run: `pixi run test`
Expected: PASS (full non-live suite).

- [ ] **Step 6: Commit**

```bash
git add tests/test_env_example_drift.py .env.example
git commit -m "test(env): catch .env.example drift in both directions

Forward: 11 documented keys -- the Azure, R2 and B2 store vars -- are read
by no tracked source, and now say so with an UNIMPLEMENTED marker instead of
sending an operator hunting for a credential they do not need.

Reverse: KINOFORGE_S3_BUCKET is read at stores/s3/__init__.py:239 and was
documented nowhere. The reverse check runs against a small curated list, so
internal knobs stay out of scope and adding a member stays deliberate."
```

---

## Task 8: Scoped-policy validation tool

**Goal:** A tool that exercises both scoped grants through IAM simulation, with injected clients so its own tests need no SDK and no network.

**Files:**
- Create: `tools/validate_scoped_policy.py`
- Test: `tests/tools/test_validate_scoped_policy.py`

**Acceptance Criteria:**
- [ ] `validate_aws` runs the two-pass simulation — wildcard actions against `*`, KMS actions against the key ARN — mirroring `tools/cloud_perms_probe.py:217-253`
- [ ] Every action in `_REQUIRED_AWS_ACTIONS` is simulated; any non-`allowed` decision is reported by name
- [ ] The throwaway IAM user is deleted even when simulation raises
- [ ] `validate_gcp` calls `testIamPermissions` and reports each missing permission by name
- [ ] Both take injected clients; the tests import no boto3 and no google SDK
- [ ] Exit code 0 only when nothing is denied or missing

**Verify:** `pixi run python -m pytest tests/tools/test_validate_scoped_policy.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_validate_scoped_policy.py`:

```python
"""Tests for the scoped-policy validator.

SDK-free by construction: every client is a fake, mirroring the injection
style of `tests/tools/test_cloud_perms_probe.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from tools.validate_scoped_policy import validate_aws, validate_gcp

_ACCOUNT = "9" + "18273645" + "019"
_KEY_ARN = f"arn:aws:kms:us-east-1:{_ACCOUNT}:key/" + "4b0dbe0c-" + "3a76-401a-ac2e-d0d949b9fa3e"


class _FakeIam:
    """Fake IAM client recording calls and returning canned decisions."""

    def __init__(self, decisions: dict[str, str], *, raise_on_simulate: bool = False):
        self.decisions = decisions
        self.raise_on_simulate = raise_on_simulate
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.simulate_calls: list[dict[str, Any]] = []

    def create_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.created.append(kwargs["UserName"])
        return {"User": {"Arn": f"arn:aws:iam::{_ACCOUNT}:user/{kwargs['UserName']}"}}

    def put_user_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        return {}

    def simulate_principal_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.simulate_calls.append(kwargs)
        if self.raise_on_simulate:
            raise RuntimeError("simulate exploded")
        return {
            "EvaluationResults": [
                {"EvalActionName": a, "EvalDecision": self.decisions.get(a, "allowed")}
                for a in kwargs["ActionNames"]
            ]
        }

    def delete_user_policy(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        return {}

    def delete_user(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        self.deleted.append(kwargs["UserName"])
        return {}


def test_all_allowed_reports_exit_zero() -> None:
    """A fully-permitted policy validates clean.

    A bug that would fail this: treating the string "allowed" as falsy, or
    comparing against "Allowed" with a capital A, which AWS does not return.
    """
    iam = _FakeIam(decisions={})
    result = validate_aws(
        iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN
    )
    assert result["exit_code"] == 0
    assert result["denied"] == []


def test_denied_actions_are_reported_by_name() -> None:
    """An operator needs to know WHICH action the scoped policy misses.

    A bug that would fail this: reporting a bare boolean, which turns the
    fix into guesswork across 15 actions.
    """
    iam = _FakeIam(decisions={"ec2:RunInstances": "implicitDeny"})
    result = validate_aws(
        iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN
    )
    assert result["exit_code"] == 1
    assert result["denied"] == ["ec2:RunInstances"]


def test_kms_actions_are_simulated_against_the_key_arn() -> None:
    """KMS needs its own pass — the simulator rejects a mixed resource list.

    A bug that would fail this: folding kms:Encrypt into the wildcard pass,
    which resolves to implicitDeny against a resource-scoped policy and
    reports a false failure. This is the exact two-pass split
    `tools/cloud_perms_probe.py:201-241` already documents.
    """
    iam = _FakeIam(decisions={})
    validate_aws(iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN)

    kms_calls = [c for c in iam.simulate_calls if "ResourceArns" in c]
    assert len(kms_calls) == 1
    assert kms_calls[0]["ResourceArns"] == [_KEY_ARN]
    assert set(kms_calls[0]["ActionNames"]) == {"kms:Encrypt", "kms:Decrypt"}

    wildcard_calls = [c for c in iam.simulate_calls if "ResourceArns" not in c]
    assert len(wildcard_calls) == 1
    assert "kms:Encrypt" not in wildcard_calls[0]["ActionNames"]


def test_throwaway_user_is_deleted_even_when_simulation_raises() -> None:
    """A leaked probe user is a standing credential-shaped liability.

    A bug that would fail this: putting the delete after the simulate call
    without a finally, so any transient API error leaves the user behind.
    """
    iam = _FakeIam(decisions={}, raise_on_simulate=True)
    with pytest.raises(RuntimeError, match="simulate exploded"):
        validate_aws(
            iam, policy_document="{}", user_name="probe", kms_key_arn=_KEY_ARN
        )
    assert iam.deleted == ["probe"]


class _FakeProjects:
    """Fake resourcemanager client returning a fixed permission grant."""

    def __init__(self, granted: set[str]):
        self.granted = granted

    def test_iam_permissions(self, *, resource: str, permissions: list[str]) -> Any:  # noqa: ANN401
        class _Resp:
            def __init__(self, perms: list[str]):
                self.permissions = perms

        return _Resp([p for p in permissions if p in self.granted])


def test_gcp_missing_permissions_are_reported_by_name() -> None:
    """The GCP half must name what the role set does not supply.

    A bug that would fail this: asserting only on the count, which cannot
    tell an operator whether to add compute.securityAdmin or storage.admin.
    """
    required = ["compute.instances.create", "compute.firewalls.create"]
    client = _FakeProjects(granted={"compute.instances.create"})
    result = validate_gcp(
        client, project="kinoforge-prod-deadbeef", permissions=required
    )
    assert result["exit_code"] == 1
    assert result["missing"] == ["compute.firewalls.create"]


def test_gcp_all_granted_reports_exit_zero() -> None:
    """The happy path returns 0 and an empty missing list."""
    required = ["compute.instances.create"]
    client = _FakeProjects(granted=set(required))
    result = validate_gcp(
        client, project="kinoforge-prod-deadbeef", permissions=required
    )
    assert result == {"exit_code": 0, "missing": [], "granted": required}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/tools/test_validate_scoped_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.validate_scoped_policy'`

- [ ] **Step 3: Write minimal implementation**

Create `tools/validate_scoped_policy.py`:

```python
"""Validate the scoped AWS policy and GCP role list without launching anything.

Simulation only — `iam:SimulatePrincipalPolicy` on AWS,
`projects.testIamPermissions` on GCP. Both are free API calls. No EC2
instance, no GCE instance, no compute spend.

What this proves: the policy's own logic grants the actions kinoforge asks
for. What it does NOT prove: that a real SkyPilot launch succeeds. Simulation
cannot see an undocumented API call sky makes at launch time — see the
firewall caveat in `.gcp/policies/roles.txt`. Both artifacts stay marked
UNVALIDATED for that reason.

Usage::

    pixi run python tools/validate_scoped_policy.py --cloud aws
    pixi run python tools/validate_scoped_policy.py --cloud gcp
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from tools.cloud_perms_probe import (
    _AWS_KMS_ACTIONS,
    _GCP_REQUIRED_ROLES,
    _REQUIRED_AWS_ACTIONS,
)

# Permissions the roles in .gcp/policies/roles.txt are claimed to supply.
# Deliberately a small, load-bearing subset: one permission per capability
# kinoforge actually exercises.
GCP_REQUIRED_PERMISSIONS: tuple[str, ...] = (
    "compute.instances.create",
    "compute.instances.delete",
    "compute.instances.list",
    "compute.disks.create",
    "iam.serviceAccounts.actAs",
    "storage.buckets.get",
    "storage.objects.create",
    "storage.objects.get",
)


def validate_aws(
    iam: Any,  # noqa: ANN401
    *,
    policy_document: str,
    user_name: str,
    kms_key_arn: str | None,
    required_actions: tuple[str, ...] = _REQUIRED_AWS_ACTIONS,
) -> dict[str, Any]:
    """Attach *policy_document* to a throwaway user and simulate every action.

    The user is created bare and carries only the inline policy under test,
    so the simulation reflects that policy alone rather than the union with
    whatever the caller already holds.

    Args:
        iam: IAM client (injected so tests need no SDK).
        policy_document: Rendered policy JSON.
        user_name: Throwaway IAM user name.
        kms_key_arn: Full KMS key ARN for the resource-scoped pass, or None.
        required_actions: Actions to simulate.

    Returns:
        Dict with `exit_code`, `denied`, and `simulated`.

    Raises:
        Exception: Re-raises any client error after deleting the user.
    """
    created = iam.create_user(UserName=user_name)
    principal_arn = created["User"]["Arn"]
    policy_name = "KinoforgeScopeProbe"
    try:
        iam.put_user_policy(
            UserName=user_name,
            PolicyName=policy_name,
            PolicyDocument=policy_document,
        )
        wildcard = [a for a in required_actions if a not in _AWS_KMS_ACTIONS]
        kms = [a for a in required_actions if a in _AWS_KMS_ACTIONS]

        # Pass 1: wildcard-resource actions. Pass 2: KMS against the key ARN.
        # The simulator rejects a mixed list of "*" and specific ARNs, so the
        # split is required, not stylistic.
        sim = iam.simulate_principal_policy(
            PolicySourceArn=principal_arn, ActionNames=wildcard
        )
        simulated: dict[str, str] = {
            e["EvalActionName"]: e["EvalDecision"] for e in sim["EvaluationResults"]
        }
        if kms:
            sim2 = iam.simulate_principal_policy(
                PolicySourceArn=principal_arn,
                ActionNames=kms,
                ResourceArns=[kms_key_arn] if kms_key_arn else ["*"],
            )
            for entry in sim2["EvaluationResults"]:
                simulated[entry["EvalActionName"]] = entry["EvalDecision"]
    finally:
        # A leaked probe user is a standing liability; delete it on every path.
        try:
            iam.delete_user_policy(UserName=user_name, PolicyName=policy_name)
        except Exception:  # noqa: BLE001, S110
            pass
        iam.delete_user(UserName=user_name)

    denied = sorted(a for a, d in simulated.items() if d != "allowed")
    return {
        "exit_code": 1 if denied else 0,
        "denied": denied,
        "simulated": simulated,
    }


def validate_gcp(
    projects_client: Any,  # noqa: ANN401
    *,
    project: str,
    permissions: list[str] | tuple[str, ...] = GCP_REQUIRED_PERMISSIONS,
) -> dict[str, Any]:
    """Test which of *permissions* the calling identity holds on *project*.

    Args:
        projects_client: resourcemanager ProjectsClient (injected).
        project: GCP project id.
        permissions: Permissions to test.

    Returns:
        Dict with `exit_code`, `missing`, and `granted`.
    """
    wanted = list(permissions)
    response = projects_client.test_iam_permissions(
        resource=f"projects/{project}", permissions=wanted
    )
    granted = list(response.permissions)
    missing = [p for p in wanted if p not in granted]
    return {"exit_code": 1 if missing else 0, "missing": missing, "granted": granted}


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", choices=("aws", "gcp"), required=True)
    parser.add_argument("--policy-file", default=None, help="rendered AWS policy JSON")
    parser.add_argument("--user-name", default="kinoforge-scope-probe")
    parser.add_argument("--project", default=None, help="GCP project id")
    args = parser.parse_args(argv)

    if args.cloud == "aws":
        import boto3

        from tools.cloud_perms_probe import _load_kms_key_arn
        from tools.render_aws_policy import _POLICY_PATH

        if not args.policy_file:
            parser.error("--policy-file is required for --cloud aws (render it first)")
        result = validate_aws(
            boto3.client("iam"),
            policy_document=open(args.policy_file).read(),  # noqa: SIM115, PTH123
            user_name=args.user_name,
            kms_key_arn=_load_kms_key_arn(),
        )
        print(json.dumps({"policy_template": str(_POLICY_PATH), **result}, indent=2))  # noqa: T201
        return int(result["exit_code"])

    from google.cloud import resourcemanager_v3

    if not args.project:
        parser.error("--project is required for --cloud gcp")
    result = validate_gcp(
        resourcemanager_v3.ProjectsClient(), project=args.project
    )
    print(json.dumps({"roles": list(_GCP_REQUIRED_ROLES), **result}, indent=2))  # noqa: T201
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/tools/test_validate_scoped_policy.py -v`
Expected: PASS (all 6)

Run: `pixi run pre-commit run --files tools/validate_scoped_policy.py tests/tools/test_validate_scoped_policy.py`
Expected: all Passed. If mypy objects to the `Any` client params, keep them — the injection style is deliberate and matches `tools/cloud_perms_probe.py:321-330`.

- [ ] **Step 5: Commit**

```bash
git add tools/validate_scoped_policy.py tests/tools/test_validate_scoped_policy.py
git commit -m "feat(cloud): validate scoped grants by simulation, not by launching

Attaches the rendered policy to a throwaway IAM user carrying nothing else,
simulates every action in _REQUIRED_AWS_ACTIONS, and deletes the user on
every path including the raising one. The GCP half calls
testIamPermissions against the roles.txt set.

Both are free API calls -- no EC2 instance, no GCE instance, no compute
spend. Simulation proves the policy's logic, not that a launch succeeds;
both artifacts stay marked UNVALIDATED for that reason."
```

---

## Task 9: Run the validation and record the result

**Goal:** Actually exercise both scoped grants against the real IAM and Resource Manager APIs, and write down what came back.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `.aws/policies/skypilot-minimal.template.json` (banner reflects the actual result)
- Modify: `.gcp/policies/roles.txt` (same)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT entry)

**Acceptance Criteria:**
- [ ] `pixi run python tools/validate_scoped_policy.py --cloud aws --policy-file <rendered> --confirm-live` was run against real AWS and its JSON output captured verbatim
- [ ] `pixi run python tools/validate_scoped_policy.py --cloud gcp --project <project> --confirm-live` was run against real GCP and its JSON output captured verbatim
- [ ] Every action listed under `denied` (AWS), every action listed under `ungranted` (AWS), and every permission under `missing` (GCP) is either fixed in the policy/role list, or written down as a known and accepted gap with the reason — including an accepted type-mismatch `denied` entry per Step 3's disambiguation procedure, or an `ungranted` KMS action when Step 2's render resolves no KMS key id at all (not merely when `--kms-key-id` is omitted — see Step 2's fallback behavior, and the Verify block below).
- [ ] The throwaway IAM user does not exist afterwards: `aws iam get-user --user-name kinoforge-scope-probe` returns `NoSuchEntity`
- [ ] The rendered policy file under `/tmp` is deleted
- [ ] Both banners state the real outcome — "simulate-clean, launch-unvalidated" or "simulate-denied on N actions" — not an aspiration
- [ ] Zero compute spend: no EC2 instance and no GCE instance was created at any point

**Verify:**
```bash
pixi run python tools/validate_scoped_policy.py --cloud aws \
  --policy-file /tmp/skypilot-minimal.rendered.json --confirm-live ; echo "aws rc=$?"
pixi run python tools/validate_scoped_policy.py --cloud gcp \
  --project "$(pixi run -e live-skypilot gcloud config get-value project)" --confirm-live ; echo "gcp rc=$?"
pixi run -e live-skypilot aws iam get-user --user-name kinoforge-scope-probe 2>&1 | tail -2
```
Expected — GCP: `missing: []` (rc=0), or an explicit list to reconcile per the
acceptance criteria above.

Expected — AWS: `denied: []` and `ungranted: []` (rc=0), or an explicit
list under either key to reconcile per the acceptance criteria above —
including a `denied` entry accepted as a type-mismatch per Step 3's
disambiguation procedure, which is a pass, not a halt condition, even
though it leaves `denied` non-empty.

Which of those two shapes to expect from `ungranted` specifically
depends on whether Step 2's render actually resolved a KMS key id —
**not** on whether `--kms-key-id` was passed. `render_aws_policy.py`
falls back to `resolve_kms_key_id()` (reading `.aws/kms-test-key.arn`)
whenever `--kms-key-id` is omitted, so omitting the flag is NOT the same
as having no key. `.aws/kms-test-key.arn` is gitignored and untracked,
so its presence is a workspace-state fact, not something this document
can assert on your behalf — check which branch actually applies before
running Step 3, with either of:

```bash
test -f .aws/kms-test-key.arn && echo "key resolves" || echo "no key -- KMSLayerW will be dropped"
# or, after Step 2 has already rendered:
grep -q KMSLayerW /tmp/skypilot-minimal.rendered.json && echo "included" || echo "dropped"
```

- **A key id resolved** — either `--kms-key-id` was passed, or
  `.aws/kms-test-key.arn` exists (true in this workspace today, so this
  is the outcome Step 2's literal command as written actually produces):
  `KMSLayerW` is included in the render, and `ungranted: []` is the
  expected, correct result. An `ungranted` KMS entry here would mean
  something is actually wrong.
- **No key id resolved** — `--kms-key-id` omitted AND
  `.aws/kms-test-key.arn` absent (only reachable if that file is removed
  from this workspace): `KMSLayerW` is dropped from the render, and
  `ungranted: ["kms:Decrypt", "kms:Encrypt"]`, rc=1 is the correct,
  expected outcome for THAT render — not a failure to chase. Record it
  as-is; do not add a KMS statement or widen anything just to force
  `ungranted: []`.

Either way: `NoSuchEntity` for the probe user afterward, and inspect
`detail` (present for every simulated action, including `ungranted`
ones, carrying real per-resource records — not just the key present with
an empty list) if anything needs a closer look — see Step 3's
disambiguation procedure for `denied` entries specifically.

```json:metadata
{"userGate": true, "tags": ["user-gate"], "gateScope": "task", "failurePolicy": "halt", "requireEvidenceTokens": [["aws", "SimulatePrincipalPolicy", "denied"], ["gcp", "testIamPermissions", "missing"]]}
```

**Steps:**

- [ ] **Step 1: Confirm credentials exist before doing anything else**

```bash
pixi run -e live-skypilot aws sts get-caller-identity
pixi run -e live-skypilot gcloud config list account --format='value(core.account)'
```

Identity probes only — never inspect a key value. If AWS credentials are
absent or expired, STOP and report it. Phase 53 abandoned this cloud surface
on 2026-06-17, so a dead credential is a plausible outcome, not a bug: record
"AWS validation not run — no working credential at HEAD" in the banner and
proceed to the GCP half rather than minting a new key.

- [ ] **Step 2: Render the policy**

```bash
pixi run python tools/render_aws_policy.py \
  --bucket-prefix "$(pixi run python -c "
import os;print(os.environ.get('KINOFORGE_S3_BUCKET','kinoforge').split('-')[0])
")" \
  --out /tmp/skypilot-minimal.rendered.json
```

If `.aws/kms-test-key.arn` is absent, pass `--kms-key-id` explicitly rather
than creating a KMS key — a new key is spend, and the simulation only needs a
syntactically valid ARN to scope pass 2.

This step is already live, not just Step 3: run without `--account`, as
above, `render_aws_policy.py` calls `_default_account()`, which fires a
real `sts:GetCallerIdentity` to resolve the account id for the rendered
ARNs. Free and read-only — Step 1 already probes the same identity — but
nothing happens live for the first time in Step 3; it happens here.

- [ ] **Step 3: Run the AWS validation**

```bash
pixi run python tools/validate_scoped_policy.py --cloud aws \
  --policy-file /tmp/skypilot-minimal.rendered.json --confirm-live
```

Capture the full JSON. Expect denials — this policy has never been attached
to anything. Each one is a real gap in a hand-written policy, so add the
missing action to the appropriate `Sid` in
`.aws/policies/skypilot-minimal.template.json`, re-render, re-run. Iterate until
`denied` is empty or the remainder is understood.

Do NOT widen a `Resource` to `"*"` to clear a denial. If an action genuinely
needs a wider resource, say so in the banner instead — a scoped policy that
was quietly widened to pass its own test is worse than an honest failing one.

**Before touching the policy for any `denied` S3 or IAM action, disambiguate
type-mismatch from a real gap.** `IAMForSkyPilotRoles` groups role AND
instance-profile ARNs together; `S3KinoforgeBuckets` groups bucket AND
object ARNs together. A denial there can mean either "the policy really
doesn't grant this" or "one of the ARNs in the group is the wrong *type*
for this action (e.g. `s3:PutObject` evaluated against a bucket-level ARN,
which has no valid meaning), and the reduction (any resource denies ⇒
denied) reported it anyway." Settle this with data already in hand, not
guesswork — and NOT with a fresh `simulate-principal-policy` call: by the
time the captured JSON is being read, `validate_aws`'s `finally` has
already deleted the probe user, so the `PolicySourceArn` that API needs
no longer exists. Trying it produces a `NoSuchEntity`-shaped failure that
reads as a new problem, not the disambiguation step it actually is.

1. Read the denied action's entry in the JSON output's `detail` map — it
   already carries one record per resource actually evaluated. This is
   normally sufficient on its own.
2. If every resource for that action shows a deny, it's a real gap — fix
   per the normal iterate-and-re-render flow above.
3. If SOME resources show `allowed` alongside the deny, that alone is
   enough to call it a type mismatch (a deny confined to a resource of a
   type the action could never apply to — bucket ARN for an object
   action, or vice versa), not a policy defect. Record it as such in the
   banner rather than adding a resource entry that's already effectively
   covered by the correctly-typed ARN in the same statement.
4. Only if a genuinely fresh live check is wanted beyond what `detail`
   already shows, the right command is `pixi run -e live-skypilot aws iam
   simulate-custom-policy --policy-input-list
   file:///tmp/skypilot-minimal.rendered.json --action-names <action>
   --resource-arns <one-arn>` — evaluated against the policy document
   directly, so it needs no principal and works fine after the probe
   user is gone. Still free; still one ARN per call. (The `aws` binary
   lives only in the `live-skypilot` pixi env — a bare `aws` here gets
   `command not found`.)

- [ ] **Step 4: Run the GCP validation**

```bash
PID=$(pixi run -e live-skypilot gcloud config get-value project)
pixi run python tools/validate_scoped_policy.py --cloud gcp --project "$PID" --confirm-live
```

Note the caveat this measures against: the calling identity today holds
`compute.admin` and `securityAdmin`, so a clean result here proves the
permission NAMES are right, not that the narrower role set supplies them. To
measure the narrow set properly, bind `roles.txt` to a second service account
and run as that identity. If that is not possible in this session, record the
weaker claim in the banner rather than overstating it.

- [ ] **Step 5: Confirm the probe user is gone and clean up**

```bash
pixi run -e live-skypilot aws iam get-user --user-name kinoforge-scope-probe 2>&1 | tail -2
rm -f /tmp/skypilot-minimal.rendered.json
```

Expected: `NoSuchEntity`. If the user still exists, delete it explicitly —
`validate_aws`'s `finally` should have handled it, and a survivor means a bug
worth fixing in Task 8 before closing this task.

- [ ] **Step 6: Rewrite both banners with the real outcome**

Task 3 moved the AWS banner out of the policy JSON: IAM's policy grammar
is closed and rejects arbitrary top-level keys, so `_comment` was never
actually an option once tested against the documented grammar (see
`.aws/policies/README.md`'s own explanation). Rewrite the
`## skypilot-minimal.template.json` section of `.aws/policies/README.md`
to one of:

```
**UNVALIDATED against a real SkyPilot launch.** Simulate-validated 2026-08-21
(`tools/validate_scoped_policy.py`): all N required actions allowed. NOT
exercised against a real SkyPilot launch -- simulation cannot see sky's
undocumented launch-time calls. Placeholders are rendered by
`tools/render_aws_policy.py`; do not attach this file directly.
```

…or, if denials remain:

```
Simulate-validated 2026-08-21: DENIED on <actions>. Known gap, see
PROGRESS.md. NOT exercised against a real launch. Render with
`tools/render_aws_policy.py`; do not attach this file directly.
```

`.gcp/policies/roles.txt`'s STATUS block gets the matching treatment,
including whether the run measured the narrow role set or only the
permission names (Step 4's caveat).

- [ ] **Step 7: Record in PROGRESS.md and commit**

Add a RESUME SNAPSHOT entry naming: both JSON outputs, any remaining gaps,
the confirmation that no instance was launched, and the Step 4 caveat if it
applied.

```bash
git add .aws/policies/skypilot-minimal.template.json .gcp/policies/roles.txt PROGRESS.md
git commit -m "docs(cloud): record the scoped-grant simulation result

First time either artifact has been exercised against a real API. Both
banners now state the measured outcome instead of an aspiration, and both
keep the launch-unvalidated caveat -- simulation proves the policy's logic,
not that sky's launch-time calls succeed.

Zero compute spend: SimulatePrincipalPolicy and testIamPermissions only.
Throwaway IAM user confirmed deleted."
```

---

## Self-Review

**Spec coverage**

| Spec section | Task |
|---|---|
| §5.1 renderer | 3 |
| §5.2 policy placeholder edits | 2 (KMS), 3 (`<S3_BUCKET_PREFIX>` + banner) |
| §5.3 `.env.example` AWS block | 5 |
| §5.4 `.aws/README.md` | 5 |
| §6 `roles.txt` | 4 |
| §6.1 securityAdmin revoke | 4 |
| §7 identifier sweep | 0, 1 |
| §7.1 scrub targets | 2 |
| §8 drift test | 7 |
| §9 validation tool + banners | 8, 9 |
| §10 billing alerts | 6 |
| §11 testing posture | RED-first in 1 and 7; unit coverage in 0, 3, 8 |

No gaps.

**Placeholder scan:** No "TBD", no "add appropriate error handling", no "similar to Task N". Every code step carries the actual code. Task 2 is the one task whose file list is a snapshot rather than an enumeration — Step 1 of that task regenerates the authoritative list from the scanner, which is the correct handling for a worklist that shifts as earlier tasks land.

**Type consistency:** `iter_identifier_findings` / `scan_all_tracked_identifiers` / `IdentifierFinding.pattern_name` / `.line_no` / `.value` / `ALLOW_PRAGMA` are used identically in Tasks 0, 1, 2. `render()` and `resolve_kms_key_id()` signatures match between Task 3's test and implementation, and `_POLICY_PATH` is imported under that exact name in Task 8. `validate_aws` / `validate_gcp` return-key names (`exit_code`, `denied`, `missing`, `granted`, `simulated`) match across Task 8's tests, implementation, and Task 9's verification.

**One known risk carried deliberately:** Task 3 adds a `_comment` key to the policy JSON. IAM accepts unknown top-level keys today, but that is observed behaviour, not a documented contract. Task 3 Step 6 checks it parses; Task 9 Step 3 is where a real `put-user-policy` would reject it, and the fallback (move the banner beside the file rather than drop it) is written into Task 3.
