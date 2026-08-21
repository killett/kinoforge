#!/usr/bin/env python3
"""Block a commit whose staged content carries a credential.

``.gitignore`` matches paths; it cannot stop a credential pasted into an
already-tracked file — the realistic leak vector in a repo where
``PROGRESS.md`` is hundreds of KB of pasted terminal output. This scanner
closes that gap at ``git commit``.

It reads **staged** content (``git diff --cached --unified=0``), not the
working tree, so ``git add -p`` is handled correctly: only the hunks
actually being committed are scanned. Binary files are skipped
structurally — git emits ``Binary files … differ`` and no ``+`` lines, so
there is nothing to decode and nothing to crash on.

Patterns come from :mod:`kinoforge.core.credential_patterns`, strict tier
only. The loose tier exists for redaction, where over-matching is
cosmetic; blocking a commit on a false match teaches ``--no-verify``,
after which this file protects nothing.

Exit codes: ``0`` clean, ``1`` findings, ``2`` usage or git error. A git
error is never reported as clean.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from kinoforge.core.credential_patterns import (
    ALLOW_PRAGMA,
    CREDENTIAL_PATTERNS,
    STRICT_PATTERNS,
    Finding,
    iter_findings,
    redact_string,
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ScanResult = list[tuple[str, Finding]]


def _run_git(repo: Path, args: list[str]) -> str:
    """Run a git command in *repo* and return decoded stdout.

    Args:
        repo: Repository working directory.
        args: Arguments after ``git``.

    Returns:
        stdout decoded as UTF-8 with ``errors="replace"`` — diff output can
        legitimately carry undecodable bytes.

    Raises:
        RuntimeError: If git exits non-zero. The message carries git's
            stderr passed through :func:`redact_string`.
    """
    proc = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {redact_string(stderr)}")
    return proc.stdout.decode("utf-8", errors="replace")


def iter_staged_added_lines(repo: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """Collect every line the staged diff **adds**.

    Args:
        repo: Repository working directory.
        paths: Optional pathspec limiting the diff (pre-commit passes the
            staged filenames). Empty means the whole staged diff.

    Returns:
        ``(path, line_no, text)`` triples, where ``line_no`` is the line
        number in the post-commit file. Deleted lines are excluded —
        removing a credential is a fix, not a leak. Binary files
        contribute nothing, since git emits no ``+`` lines for them.
    """
    args = ["diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff"]
    if paths:
        args += ["--", *paths]
    diff = _run_git(repo, args)

    added: list[tuple[str, int, str]] = []
    current = ""
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current = (
                ""
                if target == "/dev/null"
                else target[2:]
                if target.startswith("b/")
                else target
            )
            continue
        if raw.startswith("--- "):
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith("+") and current:
            added.append((current, line_no, raw[1:]))
            line_no += 1
    return added


def scan_staged(repo: Path, paths: list[str], *, strict: bool = True) -> ScanResult:
    """Scan staged added-lines for credential shapes.

    Args:
        repo: Repository working directory.
        paths: Pathspec limiting the diff; empty scans everything staged.
        strict: Use the strict tier (default). False sweeps with every
            pattern, including the deliberately loose redaction ones.

    Returns:
        ``(path, Finding)`` pairs in diff order. Empty means clean.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    results: ScanResult = []
    for path, line_no, text in iter_staged_added_lines(repo, paths):
        for finding in iter_findings(text, patterns=patterns):
            results.append((path, finding._replace(line_no=line_no)))
    return results


def scan_all_tracked(repo: Path, *, strict: bool = True) -> ScanResult:
    """Scan the working-tree content of every tracked file.

    Args:
        repo: Repository working directory.
        strict: See :func:`scan_staged`.

    Returns:
        ``(path, Finding)`` pairs. Files whose bytes are not valid UTF-8
        are skipped — they are binary, and a binary blob cannot carry a
        pasted terminal line.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    listing = _run_git(repo, ["ls-files", "-z"])
    results: ScanResult = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        target = repo / rel
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        results.extend(
            (rel, finding) for finding in iter_findings(text, patterns=patterns)
        )
    return results


def report(results: ScanResult, *, source: str) -> None:
    """Print a redacted report of *results* to stdout.

    Args:
        results: Findings to describe.
        source: Human label for what was scanned ("staged content",
            "tracked files").

    Notes:
        Every excerpt is already redacted by
        :func:`kinoforge.core.credential_patterns.iter_findings`; this
        function never has the raw value to leak.
    """
    noun = "match" if len(results) == 1 else "matches"
    print(
        f"tools/scan_secrets.py: {len(results)} credential-shaped {noun} in {source}\n"
    )
    for path, finding in results:
        print(
            f"  {path}:{finding.line_no}  {finding.pattern_name}  {finding.redacted_excerpt}"
        )
    print(
        "\nCommit blocked. If this is a placeholder, mark it "
        "(example / xxxx / <PLACEHOLDER> / ${VAR}) or add the pragma comment "
        f"`{ALLOW_PRAGMA}` on that line.\n"
        "If it is real: ROTATE the credential first, then remove it from the "
        "staged content. By the time you are reading this the value has "
        "already been in a shell buffer."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``. Positional
            arguments are staged paths (pre-commit passes filenames).

    Returns:
        Process exit code — 0 clean, 1 findings, 2 usage or git error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="staged paths to limit the scan to")
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument(
        "--all-tracked", action="store_true", help="scan every tracked file"
    )
    parser.add_argument("--stdin", action="store_true", help="scan text piped on stdin")
    parser.add_argument(
        "--tier",
        choices=("strict", "all"),
        default="strict",
        help="pattern tier; 'all' includes the loose redaction patterns",
    )
    args = parser.parse_args(argv)
    strict = args.tier == "strict"
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS

    if args.stdin:
        results = [
            ("<stdin>", f) for f in iter_findings(sys.stdin.read(), patterns=patterns)
        ]
        source = "stdin"
    else:
        repo = Path(args.repo).resolve()
        try:
            if args.all_tracked:
                results = scan_all_tracked(repo, strict=strict)
                source = "tracked files"
            else:
                results = scan_staged(repo, args.paths, strict=strict)
                source = "staged content"
        except (RuntimeError, OSError) as exc:
            print(f"tools/scan_secrets.py: {redact_string(str(exc))}", file=sys.stderr)
            return 2

    if not results:
        return 0
    report(results, source=source)
    return 1


if __name__ == "__main__":
    sys.exit(main())
