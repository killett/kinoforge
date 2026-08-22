"""Drift guard — the Claude Code hooks MUST carry every shared pattern.

The hooks run under bare ``python3`` with no project on ``sys.path``, so
they cannot import :mod:`kinoforge.core.credential_patterns`. The
duplication is deliberate; this test is what keeps it honest.

**Fail-closed by design.** The previous version called ``pytest.skip``
when the hook file was missing, so a fresh clone got no transcript
protection, a green suite, and no signal. The repo hook is committed, so
its absence is now a hard failure. The user-scope hook keeps an opt-out —
``KINOFORGE_SKIP_USER_REDACT_HOOK=1`` — for environments where Claude
Code genuinely is not installed (CI runners, bare containers).
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

import pytest

from kinoforge.core.credential_patterns import CREDENTIAL_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_HOOK = REPO_ROOT / ".claude" / "hooks" / "redact_secrets.py"
USER_HOOK = Path.home() / ".claude" / "hooks" / "redact_secrets.py"
SKIP_USER_HOOK_ENV = "KINOFORGE_SKIP_USER_REDACT_HOOK"


def _load_module(path: Path, name: str) -> types.ModuleType:
    """Load a Python module from an arbitrary filesystem path.

    Args:
        path: Absolute path to the ``.py`` file.
        name: Name to register the module under; arbitrary.

    Returns:
        The fully-executed module object.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"no import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_superset(hook_path: Path, module_name: str) -> None:
    """Assert the hook at *hook_path* carries every shared pattern source."""
    hook = _load_module(hook_path, module_name)
    hook_sources = {pat.pattern for _name, pat in hook.CREDENTIAL_PATTERNS}
    shared_sources = {p.regex.pattern for p in CREDENTIAL_PATTERNS}
    missing = shared_sources - hook_sources
    assert not missing, (
        f"{hook_path} is missing {len(missing)} pattern(s) from "
        "src/kinoforge/core/credential_patterns.py:\n"
        + "\n".join(f"  {p!r}" for p in sorted(missing))
    )


def test_repo_hook_exists() -> None:
    """The in-repo hook is committed; its absence is a real regression.

    This is the inversion of the old fail-open guard: a fresh clone must
    not be able to lose transcript protection silently.
    """
    assert REPO_HOOK.is_file(), (
        f"{REPO_HOOK} is missing — a clone of this repo has no transcript "
        "scrubbing. Restore it; do not delete this test."
    )


def test_repo_hook_is_superset_of_shared_list() -> None:
    """Every shared pattern must reach the in-repo transcript hook."""
    _assert_superset(REPO_HOOK, "_repo_redact_hook")


def test_user_hook_present_or_explicitly_opted_out() -> None:
    """The user-scope hook is what actually runs on the operator's machine.

    Absent + no opt-out = failure. That is the point: a defence that
    silently is not installed provides confidence without coverage.
    """
    if os.getenv(SKIP_USER_HOOK_ENV) == "1":
        pytest.skip(f"{SKIP_USER_HOOK_ENV}=1 — user-scope hook check opted out")
    assert USER_HOOK.is_file(), (
        f"user-scope redact hook not installed at {USER_HOOK}. Copy "
        f"{REPO_HOOK} there, or set {SKIP_USER_HOOK_ENV}=1 if Claude Code "
        "is not installed in this environment."
    )
    _assert_superset(USER_HOOK, "_user_redact_hook")


def test_opt_out_variable_is_documented() -> None:
    """An undocumented opt-out becomes folklore, then becomes fail-open."""
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()
    assert SKIP_USER_HOOK_ENV in claude_md


def test_old_fail_open_variable_is_gone() -> None:
    """The old fail-open opt-out variable gated a skip; nothing may resurrect it.

    Deliberately never spells the banned name as a literal in this
    docstring or anywhere else in this module — this test reads its own
    source file (``Path(__file__)``), so a literal occurrence here would
    make the assertion fail against itself.
    """
    banned = "KINOFORGE_REQ" + "UIRE_REDACT_HOOK"
    for path in (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "AGENTS.md", Path(__file__)):
        assert banned not in path.read_text()
