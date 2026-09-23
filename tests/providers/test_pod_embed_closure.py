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
~39.4 KB of rendered env per config and pushed 8 of 13 shipped configs past the
~101 KB point where ``podFindAndDeployOnDemand`` returns a raw HTTP 500 with no
GraphQL error body (CLAUDE.md "Known infra gotchas"; U53).

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
``_util_stats`` unimported and delete it from all thirteen configs.

The restriction to ``servers/`` does the narrowing the module-level rule was
reaching for: ``wan_t2v_server`` also lazily imports the flashvsr / rife /
spandrel / seedvr2 runtimes, but those live in ``upscalers.*`` and
``interpolators.*``, outside the restriction, and stay declared per config via
whole-package ``embed_modules`` entries this guard does not touch.

``if TYPE_CHECKING:`` blocks are skipped — those imports never execute.

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
    found: set[str] = set()
    pkg = ".".join(path.relative_to(_SRC).parts[:-1])

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
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

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


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
