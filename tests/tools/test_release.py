"""Unit tests for tools/release.py — pure-function pieces only.

Subprocess wrappers (`_run`, `_commit_and_tag`, `_assert_clean_tree`) are
not exercised here; they are operator-facing and integration-tested by
the end-to-end ``pixi run release`` invocation. The tests below pin the
logic that catches caller mistakes (bad semver, backward bump, missing
version line, wrong substitution count).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import release as rel

# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["0.0.0", "1.2.3", "10.20.30"])
def test_parse_version_accepts_semver(text: str) -> None:
    """Well-formed semver returns a 3-tuple of ints."""
    parts = rel._parse_version(text)
    assert isinstance(parts, tuple)
    assert len(parts) == 3
    assert all(isinstance(p, int) for p in parts)


@pytest.mark.parametrize(
    "text",
    ["0.1", "v0.1.0", "0.1.0-rc1", "0.1.0.0", "abc", "", "0.1.a"],
)
def test_parse_version_rejects_non_semver(text: str) -> None:
    """Non-X.Y.Z input exits non-zero with a clear error."""
    with pytest.raises(SystemExit):
        rel._parse_version(text)


# ---------------------------------------------------------------------------
# _read_current_version + _bump_pyproject (round-trip via tmp_path)
# ---------------------------------------------------------------------------


def _write_pyproject(tmp_path: Path, version: str, comment: str = "") -> Path:
    """Build a minimal pyproject with a versioned PEP-621 line."""
    suffix = f"  # {comment}" if comment else ""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "kinoforge"\n'
        f'version = "{version}"{suffix}\n'
        'requires-python = ">=3.12,<3.14"\n'
    )
    return pyproject


def test_read_current_version_extracts_pinned_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads back the exact X.Y.Z value from the pinned line."""
    pyproject = _write_pyproject(tmp_path, "1.2.3", comment="PEP 621")
    monkeypatch.setattr(rel, "_PYPROJECT", pyproject)
    assert rel._read_current_version() == "1.2.3"


def test_read_current_version_missing_line_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a `version = "X.Y.Z"` line, the helper exits non-zero.

    Catches the case where someone hand-edits pyproject and drops the
    PEP-621 line — better to fail loud than silently produce a wrong commit.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "kinoforge"\n')
    monkeypatch.setattr(rel, "_PYPROJECT", pyproject)
    with pytest.raises(SystemExit, match="version"):
        rel._read_current_version()


def test_bump_pyproject_preserves_comment_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trailing comment on the version line survives the bump.

    The real pyproject.toml carries a long inline comment explaining why
    the version is pinned even without a [build-system]. Regression-test
    that we don't truncate it.
    """
    comment = "Required by PEP 621. Long explanation here."
    pyproject = _write_pyproject(tmp_path, "0.4.0", comment=comment)
    monkeypatch.setattr(rel, "_PYPROJECT", pyproject)

    rel._bump_pyproject("0.5.0")

    new_text = pyproject.read_text()
    assert 'version = "0.5.0"' in new_text
    assert comment in new_text, "trailing comment must survive the bump"
    assert 'version = "0.4.0"' not in new_text


def test_bump_pyproject_only_touches_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If multiple matching lines exist, the helper aborts before commit.

    Catches the disaster scenario where pyproject has a second `version =`
    line (e.g., inside a `[tool.poetry]` block or similar) — silent
    multi-line replace would mask intent.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "kinoforge"\n'
        'version = "0.4.0"\n'
        "[tool.something]\n"
        'version = "0.4.0"\n'
    )
    monkeypatch.setattr(rel, "_PYPROJECT", pyproject)

    with pytest.raises(SystemExit, match="!= 1"):
        rel._bump_pyproject("0.5.0")

    # Original content untouched — abort happens *after* the regex sub
    # determines the count, before write_text.
    # (Implementation could be tightened to guarantee atomicity; today
    # the regex.subn returns the rewritten text but we never call
    # write_text on the bad count.)
    assert pyproject.read_text().count('version = "0.4.0"') == 2


def test_bump_pyproject_missing_line_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No version line → exit non-zero, no write."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "kinoforge"\n')
    monkeypatch.setattr(rel, "_PYPROJECT", pyproject)

    with pytest.raises(SystemExit, match="!= 1"):
        rel._bump_pyproject("0.5.0")


# ---------------------------------------------------------------------------
# _assert_forward_bump
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "new"),
    [("0.4.0", "0.5.0"), ("0.4.0", "0.4.1"), ("0.4.0", "1.0.0"), ("0.4.9", "0.5.0")],
)
def test_assert_forward_bump_accepts_real_forward_moves(current: str, new: str) -> None:
    rel._assert_forward_bump(current, new)  # no raise


@pytest.mark.parametrize(
    ("current", "new"),
    [("0.5.0", "0.4.0"), ("0.5.0", "0.5.0"), ("1.0.0", "0.9.9"), ("0.4.1", "0.4.0")],
)
def test_assert_forward_bump_rejects_backward_or_same(current: str, new: str) -> None:
    """Backward + same-version bumps are caller mistakes — refuse them."""
    with pytest.raises(SystemExit, match="must be >"):
        rel._assert_forward_bump(current, new)
