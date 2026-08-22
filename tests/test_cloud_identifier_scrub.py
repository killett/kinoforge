"""Lockdown: no tracked file may contain a concrete cloud identifier.

Sibling of `tests/test_source_audit.py`, which does the same job for
credential shapes. Both are standing guards over `git ls-files`, so both
fire even when the committer used `--no-verify`.

Scope is every tracked file, deliberately. A test scoped to the two files
already scrubbed by hand would catch nothing — the leak this exists to
stop lives in `tools/`, in a production default parameter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.scan_identifiers import ALLOW_PRAGMA, scan_all_tracked_identifiers

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def _run_git(repo: Path, *args: str) -> None:
    """Run a git command in *repo*, raising on failure.

    Args:
        repo: Working directory to run git in.
        *args: Arguments after ``git`` (e.g. ``"init", "-q"``).

    Raises:
        subprocess.CalledProcessError: If git exits non-zero.
    """
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


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


def test_scrub_fires_on_a_planted_identifier(tmp_path: Path) -> None:
    """Reverse-test: without it, the guard above could no-op forever.

    Same idiom as `test_audit_fires_on_a_planted_credential` in
    `tests/test_source_audit.py` — "the single most valuable test in this
    file, because a guard that passes vacuously looks identical to a guard
    that works." Task 0's `test_scan_fires_on_a_planted_identifier` already
    exercises :func:`scan_all_tracked_identifiers` against a `tmp_path`
    repo, but it calls the function directly with an explicit repo
    argument — it never goes through *this file's* `_REPO_ROOT` wiring.
    That gap is latent while the guard above is genuinely RED, but the
    moment it turns green, a regression that makes the sweep return ``[]``
    (wrong cwd, `git ls-files` returning empty, a bad repo root) becomes
    indistinguishable from "the repo is clean." Goes through
    :func:`scan_all_tracked_identifiers` end-to-end against a real
    throwaway git repo, exactly as the module under test will be invoked.
    """
    _run_git(tmp_path, "init", "-q")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")

    leak_file = tmp_path / "rogue.md"
    planted_project = "kinoforge-prod-" + "cafef00d"
    planted = f"Some prose.\n\nA literal: {planted_project}\n\nMore.\n"
    leak_file.write_text(planted)
    _run_git(tmp_path, "add", "rogue.md")
    _run_git(tmp_path, "commit", "-q", "-m", "plant a cloud identifier")

    findings = scan_all_tracked_identifiers(tmp_path)
    assert len(findings) == 1
    path, finding = findings[0]
    assert path == "rogue.md"
    assert finding.pattern_name == "gcp_project_id"
    assert finding.line_no == 3
    assert finding.value == planted_project


def test_repo_root_resolves_to_a_real_populated_repo() -> None:
    """Guards against `_REPO_ROOT` silently pointing at a near-empty tree.

    If `_REPO_ROOT` misresolved to a directory that is not a git repo at
    all, `git ls-files` would exit non-zero inside
    :func:`scan_all_tracked_identifiers` and `check=True` would raise —
    that failure mode is already loud. The failure mode that is NOT loud
    is `_REPO_ROOT` resolving to a *valid* repo that just happens to
    enumerate few or no files (e.g. a path computed one `parents[]` index
    off, landing in an empty subdirectory that still has its own `.git`
    from a nested checkout) — `git ls-files` then succeeds with an
    empty or tiny list, `scan_all_tracked_identifiers` returns `[]`, and
    the guard above passes vacuously, looking identical to "the repo is
    clean." This test pins that `_REPO_ROOT` is this repository
    specifically (has `pyproject.toml` and `.git`) and that `git
    ls-files` under it enumerates a non-trivial tree.
    """
    assert (_REPO_ROOT / "pyproject.toml").is_file()
    assert (_REPO_ROOT / ".git").exists()

    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files"],  # noqa: S607
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tracked_paths = [line for line in listing.splitlines() if line]
    assert len(tracked_paths) > 300, (
        f"expected a few hundred+ tracked files under {_REPO_ROOT}, found "
        f"{len(tracked_paths)} — _REPO_ROOT may be resolving to the wrong "
        "directory"
    )
