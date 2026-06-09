"""GenerateClipStage uses opaque_store_name at every put_bytes call site.

The end-to-end behaviour is pinned by test_generate_clip.py
(``test_stage_chain_persists_tail_via_store`` +
``test_stage_non_native_i2v_n3_chains_segs_1_and_2``), which assert that
every persisted store-side name matches ``<16hex>.<ext>``. This file
holds the source-level shape check that backs CI invariant AC2 (Task 19).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from kinoforge.core.artifacts import opaque_store_name

# Regex matching opaque_store_name output (16 hex + optional 1-5-char ext).
_OPAQUE_RE = re.compile(r"^[0-9a-f]{16}(?:\.[A-Za-z0-9]{1,5})?$")

_STAGE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "kinoforge"
    / "pipeline"
    / "generate_clip.py"
)


def test_opaque_name_is_deterministic() -> None:
    """Same payload + ext → same name."""
    assert opaque_store_name(b"abc", ".mp4") == opaque_store_name(b"abc", ".mp4")


def test_opaque_name_matches_invariant_shape() -> None:
    """The shape every store-side filename must match under AC2."""
    assert _OPAQUE_RE.fullmatch(opaque_store_name(b"abc", ".mp4"))


def test_generate_clip_stage_imports_opaque_store_name() -> None:
    """The stage module imports the helper.

    Would-fail-bug: dropping the import and inlining a stale filename
    expression would silently regress to leaky on-disk names.
    """
    tree = ast.parse(_STAGE_PATH.read_text())
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "kinoforge.core.artifacts":
                imported |= any(
                    alias.name == "opaque_store_name" for alias in node.names
                )
    assert imported, "generate_clip.py must import opaque_store_name"


def test_generate_clip_stage_uses_opaque_at_every_put_bytes() -> None:
    """Every ``store.put_bytes`` call in generate_clip.py must pass a name
    that came from ``opaque_store_name`` — assert by static analysis on
    the AST so the test is robust against refactors that reorder
    statements.

    Would-fail-bug: a regression that passes ``artifact.filename`` (or
    any other prompt-derived expression) to ``put_bytes`` would put
    prompt-derived material onto disk on every clip.
    """
    tree = ast.parse(_STAGE_PATH.read_text())
    # Find the set of Name targets assigned the return of opaque_store_name(...)
    opaque_targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Name) and func.id == "opaque_store_name":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        opaque_targets.add(target.id)

    # Now find every store.put_bytes(...) call. The SECOND positional arg
    # (name) must be a Name node whose id is in opaque_targets.
    bad_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "put_bytes"):
            continue
        if len(node.args) < 2:
            continue
        name_arg = node.args[1]
        if isinstance(name_arg, ast.Name) and name_arg.id in opaque_targets:
            continue
        bad_calls.append(ast.unparse(node))

    assert not bad_calls, (
        "every store.put_bytes(run_id, name, payload) call in generate_clip.py "
        f"must take name from opaque_store_name; offenders: {bad_calls}"
    )
