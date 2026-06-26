"""AC-P3-5 — no module outside resolve_active_lora_stack may branch on
cli_loras / args.loras and then run cfg-vs-vault precedence logic.

Allow-list:
  - src/kinoforge/cli/_commands.py: the _cmd_generate function may
    conditionally call parse_loras_heredoc and pass the result as a
    kwarg, but MUST NOT itself implement cfg-vs-vault precedence.

Violation = an `if cli_loras is not None` / `if args.loras is not None`
branch in any module under src/kinoforge/ that ALSO references both
`cfg.loras` and `vault.loras` (or `.loras` on a name resembling vault).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ALLOWED = {
    # parse-and-thread hop only; AST scan tolerates `args.loras` branch
    # in this single file because the body does not also reference
    # vault.loras / cfg.loras precedence.
    Path("src/kinoforge/cli/_commands.py"),
}


def _references_both_cfg_and_vault_loras(node: ast.AST) -> bool:
    sources: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "loras":
            if isinstance(sub.value, ast.Name):
                sources.add(sub.value.id)
            elif isinstance(sub.value, ast.Attribute):
                sources.add(sub.value.attr)
    return any("cfg" in s for s in sources) and any("vault" in s for s in sources)


def _conditional_on_cli_loras(test_node: ast.AST) -> bool:
    for sub in ast.walk(test_node):
        if isinstance(sub, ast.Attribute) and sub.attr in {"loras", "cli_loras"}:
            return True
        if isinstance(sub, ast.Name) and sub.id == "cli_loras":
            return True
    return False


def test_no_precedence_branches_outside_resolver() -> None:
    src_root = Path("src/kinoforge")
    resolver = Path("src/kinoforge/core/lora.py")
    violations: list[str] = []

    for py_file in src_root.rglob("*.py"):
        rel = py_file.relative_to(src_root.parent.parent)
        if rel == resolver:
            continue  # the legitimate site
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.If) and _conditional_on_cli_loras(node.test)):
                continue
            if not _references_both_cfg_and_vault_loras(node):
                continue
            if rel in _ALLOWED:
                violations.append(
                    f"{rel}:{node.lineno} — allow-listed file but "
                    f"if-branch on cli_loras references both cfg.loras "
                    f"and vault.loras"
                )
                continue
            violations.append(
                f"{rel}:{node.lineno} — non-resolver module branches on "
                f"cli_loras and reads both cfg.loras and vault.loras; "
                f"precedence logic must live in resolve_active_lora_stack only"
            )

    assert not violations, "\n".join(violations)
