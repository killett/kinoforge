"""Lockdown: every module under tests/live/ must be gated out of default runs.

Money invariant, not style: an ungated live test under ``tests/live/`` runs
during a plain ``pixi run test`` — locally that can spend real provider money
(the tests/live conftest auto-loads ``.env`` creds), and in CI it fails on
missing creds. On 2026-07-03 two live smokes shipped with no gate and broke
main CI (run 28683391787).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = REPO_ROOT / "tests" / "live"

# A module counts as gated when it references any of these. Text scan on
# purpose (same idiom as test_no_unredacted_writes) — coarse but cheap, and
# it catches the observed failure mode: a module with NO gating signal at all.
_GATE_SIGNALS = (
    "KINOFORGE_LIVE_TESTS",  # module-level env gate (preferred for new tests)
    "skipif",  # pytestmark / decorator conditional skip (e.g. on RUNPOD_API_KEY)
    "pytest.skip",  # imperative module- or fixture-level skip
    "importorskip",
    "mark.live",  # deselected by the default task's -m "not live"
    "kinoforge:ci-safe",  # explicit reviewed opt-out: fast, no network, no spend
)


def test_every_live_module_declares_a_gate() -> None:
    """Bug caught: a new tests/live module with no gate at all — it runs (and
    can spend money) under a plain ``pixi run test``."""
    ungated = [
        str(p.relative_to(REPO_ROOT))
        for p in sorted(LIVE_DIR.glob("test_*.py"))
        if not any(sig in p.read_text() for sig in _GATE_SIGNALS)
    ]
    assert not ungated, (
        "tests/live modules with no gating signal — add a KINOFORGE_LIVE_TESTS "
        "module gate, a `live` marker, or a reviewed '# kinoforge:ci-safe' tag: "
        f"{ungated}"
    )


def test_default_pixi_test_tasks_deselect_live_marker() -> None:
    """Bug caught: someone drops ``-m 'not live'`` from the default task and
    live-marked smokes silently rejoin plain ``pixi run test`` runs."""
    tasks = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())["tasks"]
    for name in ("test", "test-cov"):
        task = tasks[name]
        cmd = task if isinstance(task, str) else task["cmd"]
        assert "not live" in cmd, (
            f"pixi task {name!r} must deselect live tests with -m 'not live'; got: {cmd!r}"
        )
