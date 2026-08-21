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

# Split at "PRIVATE KEY" so this file never carries a full BEGIN..END span —
# tests/test_source_audit.py's tracked-tree guard would flag the test that
# tests the guard. Mirrors tests/core/test_credential_patterns.py.
PEM_BLOCK = (
    "-----BEGIN RSA PRIVATE " + "KEY-----\n"
    "MIIEowIBAAKCAQEAxxxxSECRETBODYxxxx\n"
    "-----END RSA PRIVATE " + "KEY-----"
)


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
    """Removing a credential is a fix, not a leak.

    Uses a BARE, unmarked credential — no placeholder marker, no pragma.
    The original fixture planted the credential WITH the allow-secret
    pragma, which suppresses it regardless of whether deleted lines are
    scanned; that made the test pass even if deletion-handling were
    broken. This version actually discriminates: if scan_staged ever
    started reporting removed lines, this would fail.
    """
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "planted")
    (repo / "notes.md").write_text("clean line\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_staged_multiline_pem_is_detected(repo: Path) -> None:
    """A pasted private key spans lines (Critical 1).

    A per-added-line scan can never see the whole BEGIN..END span, so
    STRICT_PATTERNS' pem_private_key — the pattern that most needs to
    block a commit — would silently never fire through this path.
    """
    (repo / "notes.md").write_text(f"clean line\n{PEM_BLOCK}\n")
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["pem_private_key"]
    _path, finding = findings[0]
    assert "SECRETBODY" not in finding.redacted_excerpt
    assert "MIIEow" not in finding.redacted_excerpt
    assert scan_secrets.main(["--repo", str(repo)]) == 1


def test_double_plus_prefixed_credential_is_found(repo: Path) -> None:
    """A staged line literally starting with "++ " must still be scanned (Critical 2).

    Git renders such a line as "+++ KEY=..." once its own "+" added-line
    marker is prepended — indistinguishable, by naive prefix-sniffing,
    from a "+++ b/path" file header. The reviewer confirmed this made a
    real staged AWS-shaped key report clean.
    """
    (repo / "notes.md").write_text(f"clean line\n++ KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [(p, f.pattern_name) for p, f in findings] == [
        ("notes.md", "aws_access_key")
    ]


def test_double_plus_prefixed_line_does_not_corrupt_later_findings(repo: Path) -> None:
    """A "++ "-prefixed line before a credential must not misattribute it.

    Regression guard for the other half of Critical 2: prefix-sniffing
    that misreads "++ ..." as a "+++ " header also resets what the
    scanner believes the current file/line is, so a real finding a few
    lines later reports a garbage path.
    """
    (repo / "notes.md").write_text(
        f"clean line\n++ just noise, not a header\nKEY={AWS_KEY}\n"
    )
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert len(findings) == 1
    path, finding = findings[0]
    assert path == "notes.md"
    assert finding.line_no == 3
    assert finding.pattern_name == "aws_access_key"


def test_multi_hunk_single_file_reports_correct_absolute_lines(repo: Path) -> None:
    """Two separate hunks in one file must both be found at the right lines.

    This is where the run-start-plus-offset arithmetic breaks if it is
    wrong: the second hunk's finding must not be reported relative to the
    first hunk's start, or relative to line 1 of the whole file.
    """
    (repo / "notes.md").write_text("a\nb\nc\nd\ne\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "baseline")
    new_content = f"a\nKEY1={AWS_KEY}\nb\nc\nd\nKEY2={OTHER_KEY}\ne\n"
    (repo / "notes.md").write_text(new_content)
    _git(repo, "add", "notes.md")

    findings = scan_secrets.scan_staged(repo, paths=[])

    got = sorted((path, f.line_no, f.pattern_name) for path, f in findings)
    assert got == [
        ("notes.md", 2, "aws_access_key"),
        ("notes.md", 6, "aws_access_key"),
    ]


def test_stray_invalid_utf8_byte_does_not_drop_the_whole_file(repo: Path) -> None:
    """One bad byte must cost one garbled character, not the whole file (NEW-1).

    Strict UTF-8 decoding of the staged blob made a single invalid byte
    anywhere in an otherwise-text file make the WHOLE file look binary and
    be skipped — including a real credential elsewhere in it. This repo's
    realistic leak vector is a large pasted-terminal-output file, which
    routinely carries a stray mis-encoded byte without being remotely
    binary. The fix must decode with errors="replace" for anything that
    isn't actually binary (no NUL byte), so the credential later in the
    file is still found.
    """
    content = (
        b"line with a stray invalid byte: \xff\n"
        b"clean line\n" + f"KEY={AWS_KEY}\n".encode()
    )
    (repo / "notes.md").write_bytes(content)
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]


def test_binary_blob_is_recognized_by_nul_byte_not_strict_utf8(repo: Path) -> None:
    """`_read_staged_file`'s binary guard is a NUL-byte check (matches git's
    own binary-file heuristic), not "does strict UTF-8 decoding fail" —
    the fix for NEW-1 must not reintroduce a decode-based skip through
    the back door. Exercises the helper directly so this holds regardless
    of whether the diff layer ever hands it a binary path.
    """
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
    _git(repo, "add", "blob.bin")
    assert scan_secrets._read_staged_file(repo, "blob.bin") is None


def test_git_show_failure_for_a_scanned_path_exits_2_not_0(monkeypatch, repo):
    """A `git show` failure on a path this scanner is supposed to scan is a
    hard error, never "clean" (NEW-2).

    The old `_read_staged_file` caught RuntimeError internally and
    returned None, silently dropping the file with zero findings and zero
    warning — the exact failure class this round of review was meant to
    close. "Nothing to scan" (a path with no added ranges, e.g. a pure
    deletion) stays quiet; "could not scan something I was supposed to
    scan" must reach main()'s exit-2 handling instead.
    """
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")

    real_run_git_bytes = scan_secrets._run_git_bytes

    def _flaky_git_bytes(repo_arg: Path, args: list[str]) -> bytes:
        if args and args[0] == "show":
            raise RuntimeError("git show :notes.md failed: simulated corruption")
        return real_run_git_bytes(repo_arg, args)

    monkeypatch.setattr(scan_secrets, "_run_git_bytes", _flaky_git_bytes)

    assert scan_secrets.main(["--repo", str(repo)]) == 2


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
