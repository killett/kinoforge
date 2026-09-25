"""Behavior: a RunPod diffusers pod embeds exactly the ``servers/`` modules its
entry points import — no unimported module rides along, and no imported module
is missing.

Why this guard exists
---------------------
``_render_embed_lines`` (``engines/diffusers/__init__.py``) walks a PACKAGE
DIRECTORY, so ``embed_modules: ["kinoforge.engines.diffusers.servers"]`` shipped
all seven files in that package to every pod. Three of them —
``minimax_h3_server.py``, ``_lora.py``, ``_av_io.py`` — exist for the MiniMax-H3
server, which runs on Modal only, and are imported by no RunPod config. They cost
~39.7 KB of rendered env per config and pushed 8 of 13 shipped configs past the
~101 KB point where ``podFindAndDeployOnDemand`` returns a raw HTTP 500 with no
GraphQL error body (CLAUDE.md "Known infra gotchas"; U53). Fix-wave-2 found three
more RunPod diffusers configs, under ``grids/``, that carried the same
whole-package embed and had never been discovered by this guard at all — see
``tests.providers.test_env_payload_ceiling._runpod_diffusers_pod_configs`` for
the discovery rules this module now inherits.

Fixing the configs without a guard invites the fat straight back: the next module
added under ``servers/`` would ride onto every pod again, and the only symptom is
an unexplained 500 at create. Hence both directions are asserted.

The closure rule, and why it is NOT module-level-only
----------------------------------------------------
The closure follows ALL imports, including ones nested inside functions, and is
then restricted to modules under ``kinoforge.engines.diffusers.servers``.

An earlier draft used module-level imports only. That is wrong.
``wan_t2v_server``'s sole module-level ``kinoforge`` import is
``servers._video_io``; ``servers._util_stats`` is imported lazily inside a
function — yet every pod needs it, because it backs the ``/util`` route that
CLAUDE.md's live-smoke polling rule depends on. Module-level-only would declare
``_util_stats`` unimported and delete it from every guarded config.

The restriction to ``servers/`` does the narrowing the module-level rule was
reaching for: ``wan_t2v_server`` also lazily imports the flashvsr / rife /
spandrel / seedvr2 runtimes, but those live in ``upscalers.*`` and
``interpolators.*``, outside the restriction, and stay declared per config via
whole-package ``embed_modules`` entries this guard does not touch.

``if TYPE_CHECKING:`` BODIES are skipped — those imports never execute. Their
``else:`` branches are NOT, and neither is ``if not TYPE_CHECKING:``: both run
at runtime, and dropping them would under-count the closure, which
``test_no_unimported_servers_module_is_embedded`` would then read as licence to
delete a genuinely-needed module from every config (U62). The guard is matched
on the bare name, not by substring, for exactly that reason.

That precision cuts BOTH ways, and the other direction is the one that costs
money. A COMPOUND guard — ``if TYPE_CHECKING or X:``, ``if typing.TYPE_CHECKING
is True:`` — is no longer recognised, so its type-only imports now enter the
closure and every guarded config would be told to embed modules no pod loads,
pushing rendered env back toward the ~101 KB create ceiling U53 just cleared.
There are ZERO such sites today (checked 2026-09-24: all 30 ``TYPE_CHECKING``
``If`` nodes under ``src/kinoforge`` are a bare ``ast.Name``, none with an
``orelse``), which is why the precise match is safe to make. Write a compound
guard under ``servers/`` and this is what will surprise you.

Soundness assumption — RE-CHECK THIS IF IT EVER FAILS ODDLY
-----------------------------------------------------------
A static AST closure cannot see ``importlib.import_module`` with a computed name.
Checked 2026-09-23: the only such calls reachable from an embedded module are
``wan_t2v_server.py:1054`` and ``minimax_h3_server.py:442`` (both resolve the
operator-supplied ``KINOFORGE_DIFFUSERS_LOAD_STUB`` dotted path, a test seam) and
``upscalers/flashvsr/_runtime.py:362`` (resolves the third-party
``diffsynth.models.wan_video_dit``). None names a ``kinoforge.*`` module. If a
future module dynamically imports a sibling under ``servers/``, this guard will
happily delete it and the pod will die at first request — so a new
``import_module`` call in this package must be added to this list or given a
static import.
"""

from __future__ import annotations

import ast
import base64
import gzip
import re
from pathlib import Path

import pytest

from tests.providers.test_env_payload_ceiling import _runpod_diffusers_pod_configs
from tools.snapshot_launch_payloads import capture_payload

#: Repo ``src/`` root — module-name-to-path resolution walks this, not sys.path,
#: so the guard reads the SHIPPED source rather than whatever is importable.
_SRC = Path(__file__).resolve().parents[2] / "src"

#: The only package this guard governs. See the module docstring.
_SERVERS_PKG = "kinoforge.engines.diffusers.servers"


def _module_path(mod: str) -> Path | None:
    """Resolve a dotted module name to its file under ``src/``.

    Args:
        mod: Dotted module name, e.g. ``kinoforge.core.errors``.

    Returns:
        Path to the ``.py`` file (or the package ``__init__.py``), or ``None``
        when the name is not a module — e.g. a symbol pulled in by
        ``from x import y`` where ``y`` is a function, not a submodule.
    """
    rel = Path(mod.replace(".", "/"))
    for candidate in (_SRC / rel.with_suffix(".py"), _SRC / rel / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _kinoforge_imports(path: Path) -> set[str]:
    """Return every ``kinoforge.*`` name imported by ``path``, nested included.

    Walks the whole AST so imports inside functions count (``_util_stats`` is
    one). Skips ``if TYPE_CHECKING:`` bodies, whose imports never execute.
    Relative imports are resolved against the file's own package.

    Args:
        path: Source file to scan.

    Returns:
        Dotted names — a mix of real modules and symbols imported from them;
        :func:`_module_path` filters the non-modules out downstream.
    """
    return _imports_in_source(
        path.read_text(encoding="utf-8"),
        pkg=".".join(path.relative_to(_SRC).parts[:-1]),
    )


def _imports_in_source(source: str, *, pkg: str) -> set[str]:
    """Return every ``kinoforge.*`` name imported by *source*.

    Split out of :func:`_kinoforge_imports` so the walk can be driven by
    fixture source rather than by a file under ``src/``. U62 and U63 were both
    verified LATENT against the shipped tree, so a test driving real files
    would pass on a broken walk — fixture source is the only way to watch
    either defect fail.

    Args:
        source: Python source to parse.
        pkg: Dotted package the source lives in, for resolving relative
            imports.

    Returns:
        Dotted names — a mix of real modules and symbols imported from them.
    """
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            # The BODY never executes, but the ``else:`` does — U62. Returning
            # without recursing here dropped both that branch's runtime
            # imports and (via the old substring test) every import under
            # ``if not TYPE_CHECKING:``.
            for runtime_stmt in node.orelse:
                visit(runtime_stmt)
            return
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kinoforge"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.rsplit(".", node.level - 1)[0] if node.level > 1 else pkg
                mod = f"{base}.{node.module}" if node.module else base
            else:
                mod = node.module or ""
            if mod.startswith("kinoforge"):
                found.add(mod)
                found.update(f"{mod}.{a.name}" for a in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return found


def _is_type_checking_guard(test: ast.expr) -> bool:
    """Return whether *test* is the bare ``TYPE_CHECKING`` guard.

    Matched precisely rather than by substring (U62). ``"TYPE_CHECKING" in
    ast.unparse(node.test)`` also matched ``if not TYPE_CHECKING:``, whose
    body DOES execute at runtime, and dropped its imports outright.

    Args:
        test: The ``If`` node's test expression.

    Returns:
        True for ``TYPE_CHECKING`` and ``typing.TYPE_CHECKING``; False for any
        compound or negated expression mentioning the name.
    """
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _is_docstring_only(body: list[ast.stmt]) -> bool:
    """Return whether *body* is at most a single docstring.

    Extracted from :func:`test_servers_package_init_stays_docstring_only` so
    the predicate can be driven by fixture source; see U63.

    Args:
        body: Top-level statements of a parsed module.

    Returns:
        True when the module holds nothing but (optionally) a docstring.
    """
    if not body:
        return True
    if len(body) > 1:
        return False
    stmt = body[0]
    # U63: ``isinstance(stmt, ast.Expr)`` alone accepts a bare expression or a
    # discarded call — and a discarded call is exactly the shape of code that
    # would matter on a pod, which receives an empty stand-in for this file.
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _closure(roots: list[str]) -> set[str]:
    """Transitively expand ``roots`` over ``kinoforge.*`` imports.

    Args:
        roots: Dotted module names to start from.

    Returns:
        Every resolvable module reachable from ``roots``, roots included.
    """
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        path = _module_path(mod)
        if path is None:
            continue
        seen.add(mod)
        todo.extend(_kinoforge_imports(path))
    return seen


def _rendered_script(cfg_path: Path) -> str:
    """Return the provision script a config would send, decoded.

    ``_encode_provision_script`` gzips then base64s the script into
    ``KINOFORGE_PROVISION_SCRIPT``; this reverses both.

    Args:
        cfg_path: Path to a RunPod diffusers pod config.

    Returns:
        The decoded bash provision script.
    """
    env = {e["key"]: e["value"] for e in capture_payload(cfg_path)["input"]["env"]}
    blob = env["KINOFORGE_PROVISION_SCRIPT"]
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


def _entry_points(script: str) -> list[str]:
    """Return the dotted modules the provision script runs via ``python -m``.

    Discovered rather than hardcoded so a config that changes its server_cmd or
    adds a build-phase ``python -m`` step is covered without editing this test.

    Args:
        script: Decoded provision script.

    Returns:
        Dotted ``kinoforge.*`` module names, in first-appearance order.
    """
    seen: list[str] = []
    for match in re.finditer(r"python3?\s+-m\s+(kinoforge[\w.]*)", script):
        mod = match.group(1)
        if mod not in seen:
            seen.append(mod)
    return seen


def _embedded_servers_modules(script: str) -> set[str]:
    """Return which ``servers/`` modules the script writes onto the pod.

    Both embed renderers end each write with ``> /tmp/kfsrv/<rel path>.py``;
    empty ancestor ``__init__.py`` files are created with ``touch`` and are
    deliberately NOT counted — they carry no code, only package-ness.

    Args:
        script: Decoded provision script.

    Returns:
        Dotted module names under :data:`_SERVERS_PKG`.
    """
    out: set[str] = set()
    pkg_rel = _SERVERS_PKG.replace(".", "/")
    for match in re.finditer(rf"> /tmp/kfsrv/({pkg_rel}/[\w/]+)\.py", script):
        out.add(match.group(1).replace("/", "."))
    return {m for m in out if not m.endswith(".__init__")}


def _servers_closure(cfg_path: Path) -> set[str]:
    """Return the ``servers/`` modules a config's pod entry points import.

    Args:
        cfg_path: Path to a RunPod diffusers pod config.

    Returns:
        Dotted module names under :data:`_SERVERS_PKG`.
    """
    script = _rendered_script(cfg_path)
    reachable = _closure(_entry_points(script))
    return {m for m in reachable if m.startswith(_SERVERS_PKG) and m != _SERVERS_PKG}


_CONFIGS = sorted(_runpod_diffusers_pod_configs())
_IDS = [p.stem for p in _CONFIGS]


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=_IDS)
def test_every_imported_servers_module_is_embedded(cfg_path: Path) -> None:
    """Nothing the pod imports from ``servers/`` is missing from the payload.

    Bug caught: trimming the embed set too far. The pod boots, ``/health``
    reports ready, and the first request that reaches the missing module dies
    with ``ModuleNotFoundError`` — a create-time HTTP 500 traded for a
    runtime failure after the GPU is already billing. This is the direction
    that protects against the fix in this plan being overzealous.

    Args:
        cfg_path: One shipped RunPod diffusers pod config.
    """
    script = _rendered_script(cfg_path)
    missing = sorted(_servers_closure(cfg_path) - _embedded_servers_modules(script))
    assert not missing, (
        f"{cfg_path.stem}: pod entry points {_entry_points(script)} import "
        f"{missing} from {_SERVERS_PKG}, but the provision script does not "
        f"embed them — the pod will boot and then fail at first use. Add them "
        f"to the config's embed_files."
    )


def test_servers_package_init_stays_docstring_only() -> None:
    """``servers/__init__.py`` must never carry real code — pods never see it.

    Both embed renderers (:func:`_render_embed_single_file` and the whole-
    package renderer it replaced) ``touch`` an EMPTY ``__init__.py`` for
    every ancestor package directory on the pod — they never write this
    file's actual bytes. That is correct today because the real file is
    docstring-only, so an empty stand-in changes nothing observable. But if
    anyone later adds an import, a constant, or a re-export to this
    ``__init__.py`` for real code to depend on, every pod would silently
    receive an EMPTY file instead — no error at render time, no error at
    embed-closure time (this package itself is excluded from
    :func:`_servers_closure`, see ``m != _SERVERS_PKG`` above), just a
    ``NameError``/``ImportError`` at first use on a pod that already booted
    and is already billing. This test is the guard against that: it fails
    the moment the source file stops being docstring-only, before anyone
    ships a config that depends on it.
    """
    path = _module_path(_SERVERS_PKG)
    assert path is not None, f"{_SERVERS_PKG} did not resolve to a source file"
    body = ast.parse(path.read_text(encoding="utf-8")).body
    assert _is_docstring_only(body), (
        f"{path} has non-docstring top-level statements: {body!r}. Pods never "
        f"receive this file's real content — they get an empty `touch`ed "
        f"stand-in (see _render_embed_single_file) — so any code added here "
        f"would silently never reach a pod. Move it into a module that is "
        f"actually embedded, or add it to every config's embed_files."
    )


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=_IDS)
def test_no_unimported_servers_module_is_embedded(cfg_path: Path) -> None:
    """No ``servers/`` module the pod never imports rides along in the payload.

    Bug caught: the U53 breach itself, and its return. A package-directory
    embed ships every file in ``servers/``, so adding one module there inflates
    every RunPod diffusers config's env payload — whose only symptom past
    ~101 KB is a raw HTTP 500 at create with no GraphQL error body, the shape
    that cost a full session to root-cause on 2026-07-05.

    Args:
        cfg_path: One shipped RunPod diffusers pod config.
    """
    script = _rendered_script(cfg_path)
    extra = sorted(_embedded_servers_modules(script) - _servers_closure(cfg_path))
    assert not extra, (
        f"{cfg_path.stem}: provision script embeds {extra} from {_SERVERS_PKG}, "
        f"which no pod entry point ({_entry_points(script)}) imports. Each "
        f"unimported module is dead weight against RunPod's ~101 KB "
        f"create-mutation ceiling (U53). Embed only what is imported — see "
        f"docs/superpowers/specs/2026-09-23-u53-needs-only-embed-design.md."
    )


# ---------------------------------------------------------------------------
# U62 / U63 — guard-strength tests. These test THIS MODULE's own helpers, not
# the shipped tree: both defects were verified LATENT against the tree at
# filing time, so a test driving real source files would pass on a broken
# helper. Driving the helper with fixture source is the only way to watch
# either one fail.
# ---------------------------------------------------------------------------


def test_a_runtime_import_in_the_else_of_a_type_checking_guard_is_kept() -> None:
    """U62. ``if TYPE_CHECKING: ... else: <import>`` executes the else at runtime.

    The skip must not swallow the ``orelse`` branch. A lost runtime import
    makes the closure too small, and
    :func:`test_no_unimported_servers_module_is_embedded` would then demand
    that module's REMOVAL from every config — producing a pod that boots
    clean and dies at first request.
    """
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from kinoforge.engines.diffusers.servers import _typing_only\n"
        "else:\n"
        "    from kinoforge.engines.diffusers.servers import _runtime_fallback\n"
    )

    found = _imports_in_source(source, pkg="kinoforge.engines.diffusers.servers")

    assert "kinoforge.engines.diffusers.servers._runtime_fallback" in found


def test_an_import_under_a_negated_type_checking_guard_is_kept() -> None:
    """U62. ``if not TYPE_CHECKING:`` runs at runtime — its imports must count.

    The filed defect: the skip is ``"TYPE_CHECKING" in ast.unparse(node.test)``,
    a substring test that matches the NEGATED guard too and drops an import
    that always executes.
    """
    source = (
        "from typing import TYPE_CHECKING\n"
        "if not TYPE_CHECKING:\n"
        "    from kinoforge.engines.diffusers.servers import _runtime_only\n"
    )

    found = _imports_in_source(source, pkg="kinoforge.engines.diffusers.servers")

    assert "kinoforge.engines.diffusers.servers._runtime_only" in found


def test_an_import_inside_a_real_type_checking_body_is_still_dropped() -> None:
    """U62 negative control — the skip must keep working.

    Over-correcting into "recurse everything" would pull type-only imports
    into the closure and demand modules be embedded that no pod ever loads,
    re-inflating the very payload U53 shrank.
    """
    source = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from kinoforge.engines.diffusers.servers import _typing_only\n"
    )

    found = _imports_in_source(source, pkg="kinoforge.engines.diffusers.servers")

    assert "kinoforge.engines.diffusers.servers._typing_only" not in found


@pytest.mark.parametrize(
    "source",
    ["1 + 1\n", "print('side effect')\n"],
    ids=["bare-expression", "discarded-call"],
)
def test_a_lone_non_string_expression_is_not_docstring_only(source: str) -> None:
    """U63. ``isinstance(body[0], ast.Expr)`` accepts what it must reject.

    A discarded call is exactly the shape of code that would matter on a pod
    — and pods receive an empty ``touch``ed stand-in for this file, so it
    would silently never run there.
    """
    assert not _is_docstring_only(ast.parse(source).body)


@pytest.mark.parametrize(
    "source",
    ['"""A docstring."""\n', ""],
    ids=["real-docstring", "empty-file"],
)
def test_a_docstring_or_an_empty_file_is_docstring_only(source: str) -> None:
    """U63 negative control — the tightened predicate must not reject the
    healthy file, nor an empty one (which is what a pod actually receives).
    """
    assert _is_docstring_only(ast.parse(source).body)
