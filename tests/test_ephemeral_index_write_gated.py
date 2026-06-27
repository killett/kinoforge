"""AST invariant: every EphemeralIndex.add(...) is gated on EphemeralSession.current().

Models pattern after tests/test_no_unredacted_writes.py.

Exemption tag (line-level comment on the offending call):
  ``# kinoforge:ephemeral-index-write-exempt`` — opt out for a specific call.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).parent.parent / "src" / "kinoforge"
EXEMPT_TAG = "# kinoforge:ephemeral-index-write-exempt"
REFERENCE = (
    "see cli/_commands.py::_cmd_generate cold-create branch for the canonical "
    "gated-write shape"
)


def _all_py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _call_text(source: str, start: int, end: int | None) -> str:
    lines = source.splitlines()
    last = end or start
    last = min(last, len(lines))
    first = max(1, start - 1)
    return "\n".join(lines[first - 1 : last])


def _is_add_call_on_ephemeral_index(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "add":
        return False
    recv = node.func.value
    if isinstance(recv, ast.Name) and "ephemeral_index" in recv.id.lower():
        return True
    if (
        isinstance(recv, ast.Call)
        and isinstance(recv.func, ast.Name)
        and recv.func.id == "EphemeralIndex"
    ):
        return True
    return False


def _enclosing_if_mentions_session_current(tree: ast.AST, target: ast.Call) -> bool:
    """Walk parent chain; return True iff any enclosing `if` mentions EphemeralSession.current()."""
    for parent in ast.walk(tree):
        if isinstance(parent, ast.If):
            for child in ast.walk(parent):
                if child is target:
                    cond_src = ast.unparse(parent.test)
                    if "EphemeralSession.current()" in cond_src:
                        return True
    return False


def test_every_ephemeral_index_add_is_session_gated() -> None:
    """Bug: ungated add() leaks index rows into non-ephemeral runs.

    Failure means a code path now writes the discovery seam without
    checking EphemeralSession.current() — violates the visibility
    contract that the index is the ephemeral-only discovery seam.
    """
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node  # type: ignore[attr-defined]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_add_call_on_ephemeral_index(node):
                continue
            call_src = _call_text(source, node.lineno, node.end_lineno)
            if EXEMPT_TAG in call_src:
                continue
            if not _enclosing_if_mentions_session_current(tree, node):
                violations.append(
                    f"{path.relative_to(SRC.parent)}:{node.lineno}: "
                    f"EphemeralIndex.add() outside `if EphemeralSession.current() is not None:` "
                    f"branch. {REFERENCE}."
                )

    assert not violations, "\n".join(violations)
