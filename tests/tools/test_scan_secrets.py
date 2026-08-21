"""Staging semantics of the pre-commit credential scanner.

These tests build real throwaway git repos and run real `git diff
--cached`. Mocking git would test nothing: the entire point of the unit
is its interaction with the staging area.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import scan_secrets

AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
OTHER_KEY = "ASIA" + "ZXCVBNMASDFGHJKL"


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside *repo* and return stdout."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one committed file and identity configured."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "notes.md").write_text("clean line\n")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_staged_credential_blocks(repo: Path) -> None:
    """The core case: a real-shaped key in staged content exits 1."""
    (repo / "notes.md").write_text(f"clean line\nexport KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]
    assert scan_secrets.main(["--repo", str(repo)]) == 1


def test_placeholder_marked_value_does_not_block(repo: Path) -> None:
    """Same value, strong marker word present on the line -> commit proceeds.

    Without this, .env.example and the design docs block every commit and
    everyone learns --no-verify. Uses a STRONG placeholder marker
    ("placeholder") which suppresses line-scoped (anywhere on the line,
    including a trailing comment) — unlike the WEAK markers (example,
    sample, fake, dummy, test), which only suppress when the marker text
    is inside the matched credential-shaped text itself.
    """
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}  # placeholder only\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []
    assert scan_secrets.main(["--repo", str(repo)]) == 0


def test_weak_marker_in_trailing_comment_does_not_suppress(repo: Path) -> None:
    """A WEAK marker word ("example") in a trailing comment must NOT suppress.

    Regression guard for the placeholder-marker split: only STRONG markers
    are line-scoped. WEAK markers must appear inside the matched text
    itself (e.g. the canonical AWS docs fixture AKIA...EXAMPLE) to suppress.
    A real key with an ordinary chatty comment like "# example bucket" must
    still block.
    """
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}  # example only\n")
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]


def test_only_the_staged_hunk_is_scanned(repo: Path) -> None:
    """Partial staging: `git add -p` stages a hunk, not a file.

    Stage a clean edit, then dirty the working tree with a credential.
    A working-tree scanner would block here; a staged-content scanner
    must not.
    """
    (repo / "notes.md").write_text("clean line\nsecond clean line\n")
    _git(repo, "add", "notes.md")
    (repo / "notes.md").write_text(f"clean line\nsecond clean line\nKEY={AWS_KEY}\n")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_unstaged_removal_does_not_hide_a_staged_credential(repo: Path) -> None:
    """Inverse of the above — the staged bytes are what gets committed."""
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    (repo / "notes.md").write_text("clean line\n")  # working tree now clean
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]


def test_binary_file_is_skipped_without_crashing(repo: Path) -> None:
    """A staged PNG-ish blob must not raise UnicodeDecodeError."""
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
    _git(repo, "add", "blob.bin")
    assert scan_secrets.scan_staged(repo, paths=[]) == []
    assert scan_secrets.main(["--repo", str(repo)]) == 0


def test_deleted_lines_are_not_findings(repo: Path) -> None:
    """Removing a credential is a fix, not a leak."""
    (repo / "notes.md").write_text(
        f"clean line\nKEY={AWS_KEY}  # kinoforge: allow-secret\n"
    )
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "planted")
    (repo / "notes.md").write_text("clean line\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_pragma_suppresses_a_bare_synthetic_token(repo: Path) -> None:
    """Escape hatch, line-scoped and visible in review."""
    (repo / "notes.md").write_text(f"KEY={OTHER_KEY}  # kinoforge: allow-secret\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_path_filter_limits_the_scan(repo: Path) -> None:
    """pre-commit passes filenames; the scan must honour them."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    (repo / "other.md").write_text("clean\n")
    _git(repo, "add", "notes.md", "other.md")
    assert scan_secrets.scan_staged(repo, paths=["other.md"]) == []
    assert scan_secrets.scan_staged(repo, paths=["notes.md"]) != []


def test_git_failure_exits_2_not_0(tmp_path: Path) -> None:
    """A broken git invocation must never be reported as 'clean'."""
    assert scan_secrets.main(["--repo", str(tmp_path / "not-a-repo")]) == 2


def test_report_never_prints_the_credential(repo, capsys):
    """The scanner must not leak what it caught into a terminal or CI log."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.main(["--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert AWS_KEY not in captured.out + captured.err
    assert "aws_access_key" in captured.out
    assert "notes.md:1" in captured.out


def test_stdin_mode(monkeypatch, capsys):
    """--stdin gives an ad-hoc check without touching a repo."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(f"KEY={AWS_KEY}\n"))
    assert scan_secrets.main(["--stdin"]) == 1


def test_all_tracked_scans_the_repo(repo: Path) -> None:
    """--all-tracked is the standing guard's engine."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "planted")
    findings = scan_secrets.scan_all_tracked(repo)
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]
