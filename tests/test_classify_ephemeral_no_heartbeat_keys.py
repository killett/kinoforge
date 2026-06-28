"""AST invariant: _classify_ephemeral consumes no heartbeat substrate keys.

Spec: docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md §5.9
"""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_KEYS = frozenset(
    {
        "last_heartbeat",
        "heartbeat_thread_tick",
        "session_claim",
        "restart_count",
        "last_status",
    }
)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found in reaper.py")


def _string_subscript_keys(node: ast.AST) -> set[str]:
    """Collect every string-literal subscript key accessed inside ``node``."""
    keys: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            slice_node = sub.slice
            if isinstance(slice_node, ast.Constant) and isinstance(
                slice_node.value, str
            ):
                keys.add(slice_node.value)
        if isinstance(sub, ast.Call) and (
            isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and sub.args
            and isinstance(sub.args[0], ast.Constant)
            and isinstance(sub.args[0].value, str)
        ):
            keys.add(sub.args[0].value)
    return keys


def test_classify_ephemeral_does_not_read_heartbeat_keys() -> None:
    """Refactor guard — ephemeral branch must stay heartbeat-free."""
    path = (
        Path(__file__).resolve().parents[1] / "src" / "kinoforge" / "core" / "reaper.py"
    )
    tree = ast.parse(path.read_text())
    func = _find_function(tree, "_classify_ephemeral")
    keys = _string_subscript_keys(func)
    leaked = keys & _FORBIDDEN_KEYS
    assert not leaked, (
        f"_classify_ephemeral leaked heartbeat keys {sorted(leaked)} — "
        f"ephemeral branch must stay heartbeat-free per spec §5.9."
    )


def test_classify_ephemeral_function_exists() -> None:
    """Rename guard — test fails loudly if _classify_ephemeral is gone."""
    path = (
        Path(__file__).resolve().parents[1] / "src" / "kinoforge" / "core" / "reaper.py"
    )
    tree = ast.parse(path.read_text())
    _find_function(tree, "_classify_ephemeral")  # raises AssertionError if absent
