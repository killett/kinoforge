"""CI invariant: every persistent-write site follows the canonical pattern.

Modeled on ``tests/test_core_invariant.py`` (Phase 9 Task 24). AST-based
scan of ``src/kinoforge/``. Fails the build on any merge that bypasses
the ``RedactionRegistry`` + ``EphemeralSession`` pattern.

Exemption tags (line-level comments on the offending call):
  ``# kinoforge:public-write``   — opt out of AC1 / AC7 for a specific call
  ``# kinoforge:public-name``    — opt out of AC2 for a specific put_bytes call
  ``# kinoforge:lora-redact-exempt`` — opt out of AC8 (observed LoRA refs)

Canonical reference: ``core/lifecycle.py::Ledger.record`` for the
write-pattern shape; ``core/opaque_names.py::opaque_store_name`` for
the put_bytes name shape.
"""

from __future__ import annotations

import ast
import pathlib
from typing import TypeGuard

SRC = pathlib.Path(__file__).parent.parent / "src" / "kinoforge"
EXEMPT_WRITE = "# kinoforge:public-write"
EXEMPT_NAME = "# kinoforge:public-name"
EXEMPT_LORA = "# kinoforge:lora-redact-exempt"
REFERENCE = "see core/lifecycle.py::Ledger.record for the canonical shape"


def _all_py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _line_text(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1]
    return ""


def _call_lines(source: str, start: int, end: int | None) -> str:
    """Return the joined text of every source line spanned by a call.

    Ruff-format can wrap a single ``Path.write_text(...)`` call across
    multiple physical lines, leaving an exemption tag on a line OTHER
    than ``node.lineno``. Joining the full extent makes the tag detection
    insensitive to line-wrapping. The line BEFORE the call is also
    included so a comment-on-the-preceding-line exemption tag (the only
    placement that survives ruff-format on a ``with`` statement) is
    discoverable.
    """
    lines = source.splitlines()
    last = end or start
    last = min(last, len(lines))
    first = max(1, start - 1)
    return "\n".join(lines[first - 1 : last])


def _is_call_to_method(node: ast.AST, method_name: str) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method_name
    )


def _enclosing_func(
    tree: ast.AST, target: ast.Call
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                if child is target:
                    return node
    return None


def _body_above(
    func: ast.FunctionDef | ast.AsyncFunctionDef, target: ast.Call
) -> list[ast.stmt]:
    """Statements anywhere in ``func`` that fully precede ``target`` by lineno.

    Walks every nested block (``for``/``if``/``with``/``try``) so a
    redact_json call inside a ``for`` loop above the put_json site is
    visible to the pattern-check.
    """
    above: list[ast.stmt] = []
    for node in ast.walk(func):
        if isinstance(node, ast.stmt) and node is not func:
            end = node.end_lineno or 0
            if end and end < target.lineno:
                above.append(node)
    return above


def _has_session_current_assign(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        if isinstance(stmt, ast.Assign | ast.AnnAssign):
            value = stmt.value if isinstance(stmt, ast.Assign) else stmt.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                inner = value.func.value
                if (
                    isinstance(inner, ast.Name)
                    and inner.id == "EphemeralSession"
                    and value.func.attr == "current"
                ):
                    return True
    return False


def _has_policy_guard(stmts: list[ast.stmt]) -> bool:
    """Return True if any nested ``ast.If`` references ``policy``.

    Walks recursively so an enclosing ``if not session.policy.<gate>:``
    around the persistent write counts, not just sibling-statement guards
    that physically precede the write line.
    """
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.If) and "policy" in ast.dump(node.test):
                return True
    return False


def _has_redact_json_call(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "redact_json":
                    return True
    return False


def test_ac1_put_json_canonical_pattern() -> None:
    """Every ``<store>.put_json(...)`` call must follow the canonical pattern.

    Catches put_json sites that landed without the
    ``session = EphemeralSession.current()`` + ``if session and not
    session.policy.<gate>:`` + ``redact_json`` pattern.
    """
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not _is_call_to_method(node, "put_json"):
                continue
            extent = _call_lines(source, node.lineno, node.end_lineno)
            if EXEMPT_WRITE in extent:
                continue
            func = _enclosing_func(tree, node)
            if func is None:
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_json outside a function"
                )
                continue
            stmts_above = _body_above(func, node)
            # Session assign + redact_json must precede the write call; the
            # policy guard may be the enclosing branch around the write
            # (the early-return / skip-when-ephemeral pattern), so we walk
            # the whole function body for it.
            missing: list[str] = []
            if not _has_session_current_assign(stmts_above):
                missing.append("EphemeralSession.current() assign")
            if not _has_policy_guard(list(func.body)):
                missing.append("if session and not session.policy.<gate> guard")
            if not _has_redact_json_call(stmts_above):
                missing.append("RedactionRegistry.instance().redact_json(...) call")
            if missing:
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_json missing {missing}"
                )
    assert not violations, (
        "Canonical write-site pattern violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\n{REFERENCE}\n"
        + f"Or add '{EXEMPT_WRITE}' on the put_json line for genuinely public writes."
    )


def _inside_class_suffix(tree: ast.AST, lineno: int, suffix: str) -> bool:
    """Return True if ``lineno`` falls inside a class whose name ends with ``suffix``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith(suffix):
            end = node.end_lineno or 0
            if node.lineno <= lineno <= end:
                return True
    return False


def test_ac2_put_bytes_uses_opaque_name() -> None:
    """Every ``<store>.put_bytes(run_id, name, ...)`` call uses ``opaque_store_name``.

    Else the file basename inside the store is prompt-derived and a
    directory listing leaks the prompt. ``put_bytes`` calls inside an
    ``ArtifactStore`` subclass are exempt — those ARE the public API
    plumbing where the caller already chose the name (canonical
    fixed-name writes like ``_ledger.json`` go through the store's own
    ``put_json`` → internal ``put_bytes`` chain).
    """
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not (_is_call_to_method(node, "put_bytes") and len(node.args) >= 2):
                continue
            extent = _call_lines(source, node.lineno, node.end_lineno)
            if EXEMPT_NAME in extent:
                continue
            if _inside_class_suffix(tree, node.lineno, "ArtifactStore"):
                continue
            name_arg = node.args[1]
            refs_opaque = False
            if isinstance(name_arg, ast.Call) and isinstance(name_arg.func, ast.Name):
                refs_opaque = name_arg.func.id == "opaque_store_name"
            elif isinstance(name_arg, ast.Name):
                func = _enclosing_func(tree, node)
                if func is not None:
                    for stmt in _body_above(func, node):
                        if isinstance(stmt, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == name_arg.id
                            for t in stmt.targets
                        ):
                            if isinstance(stmt.value, ast.Call) and isinstance(
                                stmt.value.func, ast.Name
                            ):
                                if stmt.value.func.id == "opaque_store_name":
                                    refs_opaque = True
            if not refs_opaque:
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_bytes name not from opaque_store_name"
                )
    assert not violations, (
        "put_bytes opaque-name violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\nUse opaque_store_name(payload, ext) or add '{EXEMPT_NAME}' on the put_bytes line."
    )


def _has_add_call_with_kind_output(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add"
        ):
            for kw in node.keywords:
                if (
                    kw.arg == "kind"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "output"
                ):
                    return True
    return False


def test_ac3_output_sink_registers_basename() -> None:
    """Every concrete ``*OutputSink.publish`` registers the basename.

    The downstream-log redaction case (output filename substituted after
    publish) depends on this registration.
    """
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef) and node.name.endswith("OutputSink")
            ):
                continue
            publish = next(
                (
                    m
                    for m in node.body
                    if isinstance(m, ast.FunctionDef) and m.name == "publish"
                ),
                None,
            )
            if publish is None:
                continue
            # ABC stub (no body / docstring + raise / ellipsis) — skip
            non_docstring = [
                s
                for s in publish.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            ]
            if not non_docstring or all(
                isinstance(s, ast.Pass | ast.Raise)
                or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
                for s in non_docstring
            ):
                continue
            if not _has_add_call_with_kind_output(publish):
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{publish.lineno}: "
                    f"{node.name}.publish missing RedactionRegistry.add(..., kind='output')"
                )
    assert not violations, "OutputSink basename-register violations:\n" + "\n".join(
        f"  {v}" for v in violations
    )


def test_ac4_save_fixture_checks_registry() -> None:
    """Every ``_save_fixture`` method body contains an ``is_active`` check.

    Fixture-saving paths must refuse to write when the redaction
    registry is loaded — else a captured fixture leaks the prompt.
    """
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_save_fixture"
                and "is_active" not in ast.unparse(node)
            ):
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: _save_fixture missing is_active check"
                )
    assert not violations, "_save_fixture violations:\n" + "\n".join(
        f"  {v}" for v in violations
    )


def test_ac5_cli_installs_log_filter() -> None:
    """The CLI entry point installs ``RedactingLogFilter``.

    Project uses ``cli/`` package, so the check inspects
    ``cli/_main.py`` (the argparse entry).
    """
    cli_main = (SRC / "cli" / "_main.py").read_text()
    assert "RedactingLogFilter" in cli_main, (
        "cli/_main.py does not reference RedactingLogFilter — log records "
        "from third-party libraries would emit prompt-laden strings."
    )


def test_ac6_artifact_stores_implement_delete_run_and_manual_cleanup() -> None:
    """Every concrete ``*ArtifactStore`` defines both cleanup methods.

    ``delete_run`` powers ``EphemeralSession.__exit__`` scrub;
    ``manual_cleanup_command`` powers the error block when scrub fails.
    """
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ClassDef)
                and node.name.endswith("ArtifactStore")
                and node.name != "ArtifactStore"
            ):
                continue
            methods = {m.name for m in node.body if isinstance(m, ast.FunctionDef)}
            missing = {"delete_run", "manual_cleanup_command"} - methods
            if missing:
                violations.append(
                    f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: "
                    f"{node.name} missing {sorted(missing)}"
                )
    assert not violations, "ArtifactStore cleanup-method violations:\n" + "\n".join(
        f"  {v}" for v in violations
    )


def _is_write_call(node: ast.AST) -> TypeGuard[ast.Call]:
    """True if ``node`` is a Call to ``.write_bytes``/``.write_text`` or ``open(...,'w'/'wb'/...)``.

    Walks the AST rather than scanning raw lines so writes hidden inside
    string literals (e.g. a Python snippet embedded in a subprocess script)
    are NOT counted as direct writes.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {
        "write_bytes",
        "write_text",
    }:
        return True
    if (
        isinstance(func, ast.Name)
        and func.id == "open"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and any(c in node.args[1].value for c in ("w", "a", "x"))
    ):
        return True
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "fdopen"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
        and any(c in node.args[1].value for c in ("w", "a", "x"))
    ):
        return True
    return False


def _reads_lora_inventory(tree: ast.AST) -> bool:
    """True when this module observes a pod LoRA inventory in any form.

    Inventory-observer signals (any one is enough):
        - ``Attribute(attr='inventory')`` access (``snapshot.inventory``,
          ``resp.inventory``).
        - Literal ``.get('inventory'...)`` or ``.get('lora_inventory'...)``
          call.
        - Literal subscript ``["inventory"]`` or ``["lora_inventory"]``.
        - **P1 (2026-06-21):** any function whose signature carries a
          ``LoraInventoryEntry`` annotation. The matcher's
          ``is_stack_match(active: list[LoraInventoryEntry], ...)``
          receives the inventory directly from the caller — without
          this signal a new helper taking ``LoraInventoryEntry``
          could read ``.ref`` / ``.last_strength`` without tripping
          the ac8 scan.

    A single signal is enough. The matcher's ref-consumption shim
    iterates entries by index, so requiring a separate ``.ref`` signal
    would let the matcher silently drop the redaction-registration call
    without tripping the invariant.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "inventory":
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in {"inventory", "lora_inventory"}
        ):
            return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value in {"inventory", "lora_inventory"}
        ):
            return True
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            params = list(node.args.args) + list(node.args.kwonlyargs)
            for arg in params:
                if arg.annotation is None:
                    continue
                if "LoraInventoryEntry" in ast.unparse(arg.annotation):
                    return True
    return False


def test_ac8_inventory_readers_register_observed_refs() -> None:
    """Every module that reads pod LoRA inventory + iterates refs must register them.

    Bug: a new CLI handler, sweeper, or status renderer pulls
    ``/lora/inventory`` (directly or via the ledger snapshot), reads each
    entry's ``ref``, and ships the prompt-laden refs into a log line or
    output sink WITHOUT first registering them with
    ``RedactionRegistry``. The redacting log filter then passes the refs
    through unchanged because they were never tokenised.

    Each offending module must contain
    ``_register_observed_lora_refs(`` somewhere in its source, OR carry
    the ``# kinoforge:lora-redact-exempt`` tag on one of its lines.

    Exempt by virtue of being the helper / contract itself:
        - ``core/warm_reuse/redaction.py`` (defines the helper)
    """
    helper_owner = SRC / "core" / "warm_reuse" / "redaction.py"
    violations: list[str] = []
    for path in _all_py_files():
        if path == helper_owner:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        if not _reads_lora_inventory(tree):
            continue
        if "_register_observed_lora_refs(" in source:
            continue
        if EXEMPT_LORA in source:
            continue
        violations.append(
            f"{path.relative_to(SRC.parent.parent)}: module reads LoRA "
            f"inventory + ref fields without _register_observed_lora_refs(...) "
            f"and without a '{EXEMPT_LORA}' tag"
        )
    assert not violations, (
        "Observed LoRA-ref registration violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\n\nAdd _register_observed_lora_refs({'inventory': inventory}) "
        + f"before refs are rendered/logged, or '{EXEMPT_LORA}' on the line "
        + "that justifies the bypass (e.g. fixture-only path)."
    )


def test_ac9_lora_inventory_writes_route_through_ledger_touch() -> None:
    """``put_json`` callers must NOT carry a literal ``"lora_inventory"`` key.

    Bug: a new code path writes the inventory snapshot directly into the
    artifact store via ``store.put_json({..., "lora_inventory": [...]})``,
    bypassing ``Ledger.touch`` — which is the single chokepoint where
    the AC1 RedactionRegistry + EphemeralSession.policy gate runs. The
    raw refs land in the store unredacted.

    Allowed: ``ledger.touch(pod_id, lora_inventory=...)`` (kwarg path
    through the canonical writer).
    """
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not _is_call_to_method(node, "put_json"):
                continue
            extent = _call_lines(source, node.lineno, node.end_lineno)
            if EXEMPT_LORA in extent:
                continue
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if not isinstance(arg, ast.Dict):
                    continue
                for k in arg.keys:
                    if (
                        isinstance(k, ast.Constant)
                        and isinstance(k.value, str)
                        and k.value == "lora_inventory"
                    ):
                        violations.append(
                            f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: "
                            f"put_json carries literal 'lora_inventory' key — "
                            f"use Ledger.touch(..., lora_inventory=...) instead"
                        )
    assert not violations, (
        "Direct lora_inventory put_json violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + "\n\nRoute lora_inventory updates through Ledger.touch so the "
        + "canonical RedactionRegistry + EphemeralSession.policy gate runs."
    )


def test_ac8_lora_inventory_entry_param_trips_scan() -> None:
    """P1 (2026-06-21) extension: a function whose signature carries a
    ``LoraInventoryEntry`` parameter is treated as reading the pod
    inventory.

    Bug this catches: a future helper takes ``list[LoraInventoryEntry]``
    directly (matcher's ``is_stack_match`` shape) and reads ``.ref``
    inside without registering. Pre-P1 the ac8 scan only fired on
    ``.inventory`` attribute / subscript / ``.get('inventory')`` signals
    so the new helper would slip through.
    """
    # Module-pretend AST: imports the type + receives it as a param.
    fixture = (
        "from typing import Iterable\n"
        "from kinoforge.engines.diffusers.servers.wan_t2v_server "
        "import LoraInventoryEntry\n"
        "\n"
        "def silently_logs_refs(entries: list[LoraInventoryEntry]) -> None:\n"
        "    for e in entries:\n"
        "        print(e.ref)\n"
    )
    tree = ast.parse(fixture)
    assert _reads_lora_inventory(tree) is True, (
        "Scan must detect functions taking LoraInventoryEntry as a "
        "parameter; otherwise a new helper accessing .ref bypasses "
        "the ac8 redaction invariant."
    )

    # Negative control: same fixture without the LoraInventoryEntry type.
    plain = (
        "def safe_helper(refs: list[str]) -> None:\n"
        "    for r in refs:\n"
        "        print(r)\n"
    )
    assert _reads_lora_inventory(ast.parse(plain)) is False


def test_ac8_lora_inventory_entry_branch_field_param_trips_scan() -> None:
    """P2 (2026-06-22) extension: a function whose signature carries a
    ``LoraInventoryEntry`` parameter is still treated as reading the
    pod inventory even when the helper consumes the new ``.branch``
    field instead of ``.ref``.

    Bug this catches: a future helper reads ``entry.branch`` (P2's
    per-LoRA routing instruction — low-entropy enum but still part of
    the inventory surface) AND ``entry.ref``, ships both into a
    structured log line WITHOUT registering refs, and silently leaks
    prompt-laden refs through the redaction filter. Pinning the
    signature-level trigger means the P2 branch field doesn't carve
    out an inventory-consuming code path that bypasses the ac8
    invariant.
    """
    fixture = (
        "from kinoforge.engines.diffusers.servers.wan_t2v_server "
        "import LoraInventoryEntry\n"
        "\n"
        "def routing_audit_log(entries: list[LoraInventoryEntry]) -> None:\n"
        "    for e in entries:\n"
        "        print({'ref': e.ref, 'branch': e.branch})\n"
    )
    tree = ast.parse(fixture)
    assert _reads_lora_inventory(tree) is True, (
        "Scan must detect functions taking LoraInventoryEntry as a "
        "parameter even when they consume .branch (P2) alongside .ref."
    )


def test_ac8_exempt_tag_count_is_audit_friendly() -> None:
    """The lora-redact-exempt tag must be rare so future uses are reviewable.

    Bug: a refactor sprinkles the exempt tag across many files to
    silence the AC8 invariant rather than wiring the helper at the
    right level — making future audits impossible.

    Allowed: the tag string literal in this invariant test file
    itself (the audit point) and at most ONE additional
    legitimate exemption elsewhere in the tree (e.g. a
    fixture-only path that has documented its bypass).
    """
    src_hits = 0
    for path in _all_py_files():
        if EXEMPT_LORA in path.read_text():
            src_hits += 1
    assert src_hits <= 1, (
        f"'{EXEMPT_LORA}' appears in {src_hits} src/kinoforge/ files — "
        "audit-friendliness budget is 1. Refactor to share the registration "
        "helper at a higher level instead of bypassing the invariant."
    )


def test_ac7_no_path_write_outside_store_and_sink() -> None:
    """Direct file writes outside an ``ArtifactStore`` or ``OutputSink`` are violations.

    Catches ``Path.write_bytes`` / ``Path.write_text`` / ``open(...,'w')``
    / ``os.fdopen(...,'w')`` calls that skip the store layer and would
    leave prompt-derived bytes on disk that ``__exit__`` cleanup never
    sees. String-literal embedded writes (e.g. a Python snippet inside a
    subprocess script) are NOT counted — they execute inside a remote
    pod, not on the operator's host.
    """
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not _is_write_call(node):
                continue
            extent = _call_lines(source, node.lineno, node.end_lineno)
            if EXEMPT_WRITE in extent:
                continue
            if _inside_class_suffix(
                tree, node.lineno, "ArtifactStore"
            ) or _inside_class_suffix(tree, node.lineno, "OutputSink"):
                continue
            violations.append(
                f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: "
                f"write outside store/sink — {_line_text(source, node.lineno).strip()}"
            )
    assert not violations, "Direct-write violations:\n" + "\n".join(
        f"  {v}" for v in violations
    )
