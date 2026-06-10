"""Drift guard — the user-scope Claude Code redact hook MUST carry every
pattern defined in :mod:`tools._redact`.

The hook lives at ``~/.claude/hooks/redact_secrets.py`` (user-scope, not
in the repo). This test locates it via ``$HOME`` and asserts that every
regex source-string in ``tools/_redact.py._CREDENTIAL_PATTERNS`` also
appears in the hook's ``CREDENTIAL_PATTERNS``. We compare pattern source
strings because the same regex compiled twice yields different objects.

Behavior on a host without the hook installed:

* Silently ``pytest.skip`` — typical on CI runners where Claude Code is
  not installed.
* Set ``KINOFORGE_REQUIRE_REDACT_HOOK=1`` to upgrade the skip to a hard
  failure (intended for local pre-push and for the dev container where
  the hook is expected to be present).

When a new pattern lands in either side, the rule is: add to
``tools/_redact.py._CREDENTIAL_PATTERNS`` first, then mirror to the hook
file. The test will keep passing as long as the hook is a SUPERSET.
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

import pytest

HOOK_PATH = Path.home() / ".claude" / "hooks" / "redact_secrets.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    """Load a Python module from an arbitrary filesystem path.

    Args:
        path: Absolute path to the ``.py`` file.
        name: Name to register the module under in ``sys.modules`` —
            arbitrary, only matters for re-importation within this
            process; the test uses ``"_redact_hook"`` to avoid clashing
            with anything real.

    Returns:
        The fully-executed module object.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, (
        f"could not build import spec for {path}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hook_patterns_superset_of_project_redact() -> None:
    """Every regex source in :data:`tools._redact._CREDENTIAL_PATTERNS`
    must also appear in the hook's :data:`CREDENTIAL_PATTERNS`.

    A failing assertion lists exactly the missing source strings — copy
    them into ``~/.claude/hooks/redact_secrets.py`` (or update the
    project list if you really meant to remove a pattern from the
    redactor).
    """
    if not HOOK_PATH.exists():
        if os.getenv("KINOFORGE_REQUIRE_REDACT_HOOK") == "1":
            pytest.fail(
                f"KINOFORGE_REQUIRE_REDACT_HOOK=1 but hook not installed at {HOOK_PATH}"
            )
        pytest.skip(f"redact hook not installed at {HOOK_PATH}")

    hook = _load_module(HOOK_PATH, "_redact_hook")
    from tools import _redact as proj

    hook_sources = {pat.pattern for _name, pat in hook.CREDENTIAL_PATTERNS}
    proj_sources = {pat.pattern for _name, pat in proj._CREDENTIAL_PATTERNS}

    missing = proj_sources - hook_sources
    assert not missing, (
        f"hook is missing {len(missing)} pattern(s) carried by "
        f"tools/_redact.py — add them to {HOOK_PATH}:\n"
        + "\n".join(f"  {p!r}" for p in sorted(missing))
    )
