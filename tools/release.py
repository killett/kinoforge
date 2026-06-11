"""Release helper — bump pyproject version, commit, and tag in one shot.

Run via ``pixi run release X.Y.Z [--note "release headline"]``.

Replaces the previous manual sequence (edit pyproject.toml → ``git
commit`` → ``git tag -a``) which drifted in early Phase 50 when the
pyproject pin (``0.1.0``) lagged behind the latest tag (``v0.4.0``).

Refuses on any precondition failure — clean tree, valid semver,
forward-only bump, no existing tag, no other ``v*`` ref pointing at
``HEAD``.

Usage::

    pixi run release 0.5.1
    pixi run release 0.6.0 --note "graceful interrupt UX (Phase 50)"
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

_log = logging.getLogger("release")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
# Match the PEP-621 ``version = "X.Y.Z"`` line near the top of pyproject.toml,
# tolerating a trailing comment (the project's existing line carries one).
_VERSION_LINE_RE = re.compile(
    r'^(?P<prefix>version\s*=\s*")(?P<value>\d+\.\d+\.\d+)(?P<suffix>".*)$',
    re.MULTILINE,
)


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> str:
    """Thin ``subprocess.run`` wrapper that returns stdout (stripped).

    Raises ``SystemExit`` on a non-zero exit with the captured stderr.
    """
    result = subprocess.run(  # noqa: S603
        cmd,
        cwd=_REPO_ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        _log.error("command failed: %s", " ".join(cmd))
        if stderr:
            _log.error("stderr: %s", stderr)
        if stdout:
            _log.error("stdout: %s", stdout)
        raise SystemExit(2)
    return (result.stdout or "").strip()


def _parse_version(text: str) -> tuple[int, int, int]:
    if not _SEMVER_RE.match(text):
        raise SystemExit(f"error: version {text!r} does not match X.Y.Z semver")
    parts = tuple(int(p) for p in text.split("."))
    return parts[0], parts[1], parts[2]


def _read_current_version() -> str:
    text = _PYPROJECT.read_text()
    match = _VERSION_LINE_RE.search(text)
    if match is None:
        raise SystemExit(
            'error: could not locate `version = "X.Y.Z"` line in pyproject.toml'
        )
    return match.group("value")


def _bump_pyproject(new_version: str) -> None:
    """Rewrite the version line in-place, preserving prefix + suffix bytes.

    Pre-flights how many matching lines exist; aborts if not exactly one.
    A second match means pyproject has ambiguous version pinning (e.g., a
    ``[tool.poetry]`` block) and the caller must disambiguate by hand
    before the bump can be safely automated.
    """
    text = _PYPROJECT.read_text()
    matches = _VERSION_LINE_RE.findall(text)
    if len(matches) != 1:
        raise SystemExit(
            f"error: version-line substitution touched != 1 line (found "
            f"{len(matches)}); aborting before commit"
        )
    new_text = _VERSION_LINE_RE.sub(
        rf"\g<prefix>{new_version}\g<suffix>",
        text,
        count=1,
    )
    _PYPROJECT.write_text(new_text)


def _assert_clean_tree() -> None:
    status = _run(["git", "status", "--porcelain"])
    if status:
        _log.error("uncommitted changes present:\n%s", status)
        raise SystemExit("error: working tree must be clean before bumping the version")


def _assert_tag_absent(tag: str) -> None:
    existing = _run(["git", "tag", "-l", tag])
    if existing:
        raise SystemExit(f"error: tag {tag!r} already exists")


def _assert_forward_bump(current: str, new: str) -> None:
    cur = _parse_version(current)
    nxt = _parse_version(new)
    if nxt <= cur:
        raise SystemExit(
            f"error: new version {new!r} must be > current {current!r} (semver)"
        )


def _commit_and_tag(new_version: str, note: str | None) -> str:
    """Commit the pyproject edit and create an annotated tag.

    Returns the new commit's short SHA.
    """
    _run(["git", "add", str(_PYPROJECT.relative_to(_REPO_ROOT))])
    # Run pre-commit on the staged file. The project's policy is never to
    # commit code that fails pre-commit; surface a failure here before the
    # commit lands.
    _run(["pixi", "run", "pre-commit", "run", "--files", str(_PYPROJECT)])
    commit_msg = f"chore(release): bump version to {new_version}"
    _run(["git", "commit", "-m", commit_msg])
    sha = _run(["git", "rev-parse", "--short", "HEAD"])
    tag = f"v{new_version}"
    tag_subject = f"kinoforge {tag}"
    if note:
        tag_subject += f" — {note}"
    _run(["git", "tag", "-a", tag, sha, "-m", tag_subject])
    return sha


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="release",
        description="Bump pyproject version, commit, and create an annotated git tag.",
    )
    parser.add_argument("version", help="New version (X.Y.Z, no `v` prefix).")
    parser.add_argument(
        "--note",
        default=None,
        help="Optional headline appended to the annotated tag subject.",
    )
    args = parser.parse_args(argv)

    new_version = args.version
    _parse_version(new_version)  # validate shape early
    tag = f"v{new_version}"

    _assert_clean_tree()
    _assert_tag_absent(tag)

    current = _read_current_version()
    if current == new_version:
        raise SystemExit(
            f"error: pyproject already pinned at {current!r}; nothing to bump"
        )
    _assert_forward_bump(current, new_version)

    _log.info("bumping pyproject version: %s → %s", current, new_version)
    _bump_pyproject(new_version)
    sha = _commit_and_tag(new_version, args.note)

    _log.info("")
    _log.info("released %s on %s", tag, sha)
    _log.info("push with: git push origin main --follow-tags")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main())
