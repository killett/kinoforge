#!/usr/bin/env python3
"""Block a commit whose staged content carries a credential.

``.gitignore`` matches paths; it cannot stop a credential pasted into an
already-tracked file — the realistic leak vector in a repo where
``PROGRESS.md`` is hundreds of KB of pasted terminal output. This scanner
closes that gap at ``git commit``.

It reads **staged** content, not the working tree, so ``git add -p`` is
handled correctly: only the hunks actually being committed are scanned.
The staged diff (``git diff --cached --unified=0``) is used only to learn
*which line ranges changed* — from the ``@@ -a,b +c,d @@`` hunk-header
counts, never from the content of the added lines themselves, so a staged
line that happens to read like a diff header (e.g. literally ``++
KEY=...``, which becomes ``+++ KEY=...`` once git prepends its own ``+``
marker) cannot be misread as one. The actual text scanned comes from
``git show :<path>`` — the staged blob — sliced to those ranges. A blob
is treated as binary (and skipped) when it carries a NUL byte in its
first 8000 bytes, mirroring git's own binary-file heuristic; anything
else is decoded leniently (``errors="replace"``) so a stray mis-encoded
byte costs one garbled character, not the whole file's scan coverage. A
path git itself cannot read back is a hard error, not a skip — see
``_read_staged_file``.

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

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

ScanResult = list[tuple[str, Finding]]


def _run_git_bytes(repo: Path, args: list[str]) -> bytes:
    """Run a git command in *repo* and return raw stdout bytes.

    Always passes ``-c core.quotePath=false`` so a non-ASCII path comes
    back as literal UTF-8 rather than an escaped, double-quoted C-string
    (git's default) — callers that parse ``+++ b/<path>`` headers or path
    listings need the literal path, not its quoted form.

    Args:
        repo: Repository working directory.
        args: Arguments after ``git``.

    Returns:
        Raw stdout bytes, undecoded.

    Raises:
        RuntimeError: If git exits non-zero. The message carries git's
            stderr passed through :func:`redact_string`.
    """
    proc = subprocess.run(  # noqa: S603
        ["git", "-c", "core.quotePath=false", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {redact_string(stderr)}")
    return proc.stdout


def _run_git(repo: Path, args: list[str]) -> str:
    """Run a git command in *repo* and return decoded stdout.

    Args:
        repo: Repository working directory.
        args: Arguments after ``git``.

    Returns:
        stdout decoded as UTF-8 with ``errors="replace"`` — diff output can
        legitimately carry undecodable bytes (e.g. binary hunks); precise
        decoding of a specific staged blob is :func:`_read_staged_file`'s
        job, not this general-purpose helper's.

    Raises:
        RuntimeError: If git exits non-zero. See :func:`_run_git_bytes`.
    """
    return _run_git_bytes(repo, args).decode("utf-8", errors="replace")


def _read_staged_file(repo: Path, path: str) -> str | None:
    """Read *path*'s STAGED (index) content as text.

    Uses ``git show :<path>`` rather than the working tree. The working
    tree can differ from what is about to be committed — partial staging
    (``git add -p``) depends on that divergence, so the scanner must read
    the same bytes ``git commit`` would.

    Binary detection mirrors git's own heuristic rather than strict UTF-8
    decoding: a NUL byte anywhere in the blob's first 8000 bytes marks it
    binary (git's core binary-file detection uses the same signal and the
    same threshold). Everything else is decoded with ``errors="replace"``.
    Strict decoding was tried first and rejected: this repo's realistic
    leak vector is a large pasted-terminal-output file, which can carry a
    handful of stray mis-encoded bytes without being remotely binary — a
    single bad byte anywhere in an otherwise-text file made strict
    decoding drop the ENTIRE file, including a real credential elsewhere
    in it. Replacement costs one garbled character, not full scan
    coverage.

    Args:
        repo: Repository working directory.
        path: Repo-relative path, as reported by the staged diff.

    Returns:
        The decoded text, or ``None`` when the blob looks binary (a NUL
        byte in its first 8000 bytes) — there is nothing to scan and
        nothing meaningful to decode.

    Raises:
        RuntimeError: If ``git show :<path>`` fails to resolve the path
            (corrupted object, an unmerged/conflicted stage, a gitlink
            oddity, a race between the diff that produced this path's
            added ranges and this read). This is deliberately NOT caught
            here: a path only reaches this function because the staged
            diff says it has content to scan, so failing to read it is a
            hard error, not "clean" — callers let it propagate up to
            :func:`main`'s exit-2 handling.
    """
    raw = _run_git_bytes(repo, ["show", f":{path}"])
    if b"\x00" in raw[:8000]:
        return None
    return raw.decode("utf-8", errors="replace")


def iter_staged_added_ranges(
    repo: Path, paths: list[str]
) -> list[tuple[str, int, int]]:
    """Learn which ``(path, start_line, count)`` ranges the staged diff adds.

    Parses ONLY the diff's structural headers — ``diff --git``, ``+++ ``,
    and ``@@ ... @@`` — never the content of an added line. That makes it
    immune to a staged line whose text happens to look like a header: git
    renders a staged line that literally starts with ``++ `` as
    ``+++ ...`` once it prepends its own added-line marker, which a
    naive ``line.startswith("+++ ")`` check misreads as a ``+++ b/path``
    file header. The distinguishing signal used here instead: a genuine
    ``+++ `` header for a file appears exactly once, immediately after
    that file's ``diff --git`` line and before its first ``@@`` hunk
    header. Any ``+++ ``-shaped line seen *after* a hunk header has
    already been seen for that file is added content, not a header, and
    is deliberately ignored.

    ``@@ -a,b +c,d @@`` means exactly ``d`` added lines starting at line
    ``c`` of the new file (``d`` omitted means 1; ``d == 0`` means a pure
    deletion — no added-line range). Those counts are trusted directly;
    the lines that follow a hunk header are not re-inspected here at all.

    Args:
        repo: Repository working directory.
        paths: Optional pathspec limiting the diff (pre-commit passes the
            staged filenames). Empty means the whole staged diff.

    Returns:
        ``(path, start_line, count)`` triples in diff order. A
        deletion-only hunk, a deleted file (``+++ /dev/null``), or a
        binary file (git reports ``Binary files … differ`` with no hunk
        headers at all) contributes nothing.
    """
    args = ["diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff"]
    if paths:
        args += ["--", *paths]
    diff = _run_git(repo, args)

    ranges: list[tuple[str, int, int]] = []
    current_file = ""
    seen_hunk_for_file = False
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current_file = ""
            seen_hunk_for_file = False
            continue
        if raw.startswith("+++ ") and not seen_hunk_for_file:
            target = raw[4:].strip()
            current_file = (
                ""
                if target == "/dev/null"
                else target[2:]
                if target.startswith("b/")
                else target
            )
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            seen_hunk_for_file = True
            start = int(hunk.group(1))
            count = 1 if hunk.group(2) is None else int(hunk.group(2))
            if count > 0 and current_file:
                ranges.append((current_file, start, count))
            continue
    return ranges


def scan_staged(repo: Path, paths: list[str], *, strict: bool = True) -> ScanResult:
    r"""Scan staged added-line RUNS for credential shapes.

    Each contiguous run of added lines (one per diff hunk) is scanned as
    a SINGLE block — its lines joined with ``"\n"`` and passed to
    :func:`iter_findings` once — rather than line by line. A per-line
    scan can never see a match that spans lines (``pem_private_key``
    needs its BEGIN and END markers in the same match), so a pasted
    private key would be reported clean even though it is exactly the
    shape this scanner most needs to catch.

    Run content is read from the STAGED blob
    (:func:`_read_staged_file`), sliced by the ``(start, count)`` range
    :func:`iter_staged_added_ranges` reports — never taken from the diff
    text directly, which is what keeps range-finding immune to added
    content shaped like a diff header.

    Blind spot, by design, not a bug to "fix": a ``pem_private_key`` whose
    BEGIN and END markers land in two separate, non-adjacent hunks of the
    *same commit* is invisible here — each hunk is scanned as its own
    block (see above), so neither half alone matches the whole-span
    pattern. This composes correctly with :func:`scan_all_tracked` (the
    tracked-tree guard in ``tests/test_source_audit.py``), which scans
    each file's full post-commit content in one pass and *does* see the
    reassembled key. The two-layer design is intentional — this function
    only needs to be immune to `--no-verify` failing *silently*, not to
    catch every shape, because the tracked-tree guard is the backstop.

    Args:
        repo: Repository working directory.
        paths: Pathspec limiting the diff; empty scans everything staged.
        strict: Use the strict tier (default). False sweeps with every
            pattern, including the deliberately loose redaction ones.

    Returns:
        ``(path, Finding)`` pairs, with ``Finding.line_no`` mapped back
        to the absolute line number in the post-commit file. Empty means
        clean. A path whose staged blob looks binary (NUL byte in the
        first 8000 bytes) is skipped — see :func:`_read_staged_file`.

    Raises:
        RuntimeError: Propagated from :func:`_read_staged_file` when a
            path with added ranges cannot be read. A path is only asked
            for here because it has something to scan, so this is never
            swallowed as "no findings" — see :func:`main`'s exit-2
            handling.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    ranges_by_file: dict[str, list[tuple[int, int]]] = {}
    for path, start, count in iter_staged_added_ranges(repo, paths):
        ranges_by_file.setdefault(path, []).append((start, count))

    results: ScanResult = []
    for path, ranges in ranges_by_file.items():
        text = _read_staged_file(repo, path)
        if text is None:
            continue
        lines = text.split("\n")
        for start, count in ranges:
            block = "\n".join(lines[start - 1 : start - 1 + count])
            for finding in iter_findings(block, patterns=patterns):
                results.append(
                    (path, finding._replace(line_no=start + finding.line_no - 1))
                )
    return results


def scan_all_tracked(repo: Path, *, strict: bool = True) -> ScanResult:
    """Scan the working-tree content of every tracked file.

    Binary detection and decoding mirror :func:`_read_staged_file` exactly
    (same NUL-byte-in-first-8000-bytes heuristic, same ``errors="replace"``
    decode) and for the same reason: this is the standing guard with no
    layer behind it — it is what still catches a credential committed with
    ``--no-verify``, so a single stray mis-encoded byte must cost one
    garbled character, not the whole file's scan coverage. The sibling bug
    (strict UTF-8 decoding silently dropping a whole file on one bad byte)
    was fixed here in commit ``8f1f4307`` for the staged-content path and
    never propagated to this function — this is that propagation.

    A tracked path *absent* from the working tree (``FileNotFoundError``)
    is skipped, not an error: that is a legitimate state mid-rebase or
    mid-stash-pop, and there is nothing to scan. A tracked path that
    *exists* but cannot be read for any other reason (permissions, a race,
    an unreadable special file) is a hard error — silently treating an
    unreadable file as "no findings" is exactly the fail-open this guard
    exists to prevent.

    Args:
        repo: Repository working directory.
        strict: See :func:`scan_staged`.

    Returns:
        ``(path, Finding)`` pairs. A tracked file that looks binary (a NUL
        byte in its first 8000 bytes) is skipped — there is nothing
        text-shaped to scan.

    Raises:
        RuntimeError: A tracked file exists but could not be read.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    listing = _run_git(repo, ["ls-files", "-z"])
    results: ScanResult = []
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
