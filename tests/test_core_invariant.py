"""Architectural invariant: kinoforge.core must never import concrete adapters.

Three tests:

1. subprocess_isolation — imports core modules in a fresh interpreter and
   asserts that no kinoforge.providers.*, kinoforge.sources.*, or
   kinoforge.engines.* module ends up in sys.modules.

2. static_vendor_import_scan — walks the entire src/kinoforge tree and
   asserts that vendor SDK imports (sky/skypilot, runpod) are confined to
   their respective adapter packages.

3. no_adapter_import_in_core — walks src/kinoforge/core and asserts that no
   line imports kinoforge.providers, kinoforge.sources, or
   kinoforge.engines.  Imports from kinoforge.stores are explicitly allowed
   (stores is an axis sibling, not a forbidden concrete adapter).
"""

import re
import subprocess
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "kinoforge"
CORE_ROOT = SRC_ROOT / "core"


# ---------------------------------------------------------------------------
# AC 1: subprocess isolation
# ---------------------------------------------------------------------------


def test_core_imports_no_provider_source_engine_modules() -> None:
    """Importing core modules in a fresh subprocess must not load any adapter namespaces."""
    script = (
        "import kinoforge.core.orchestrator; "
        "import kinoforge.core.config; "
        "import kinoforge.core.lifecycle; "
        "import kinoforge.core.provisioner; "
        "import sys; "
        "print('|'.join("
        "m for m in sys.modules "
        "if m.startswith('kinoforge.providers') "
        "or m.startswith('kinoforge.sources') "
        "or m.startswith('kinoforge.engines')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    leaked = result.stdout.strip()
    if leaked:
        offending = "\n  ".join(leaked.split("|"))
        raise AssertionError(
            f"Core import leaked concrete adapter modules into sys.modules:\n  {offending}"
        )


# ---------------------------------------------------------------------------
# AC 2: vendor SDK imports confined to the right adapter package
# ---------------------------------------------------------------------------

# Patterns: an import line that pulls in a vendor SDK at module top level.
_VENDOR_PATTERNS: list[tuple[re.Pattern[str], Path, str]] = [
    (
        re.compile(r"^\s*(import|from)\s+(sky|skypilot)\b"),
        SRC_ROOT / "providers" / "skypilot",
        "sky/skypilot",
    ),
    (
        re.compile(r"^\s*(import|from)\s+runpod\b"),
        SRC_ROOT / "providers" / "runpod",
        "runpod",
    ),
    (
        re.compile(r"^\s*(import|from)\s+boto3\b"),
        SRC_ROOT / "stores" / "s3",
        "boto3",
    ),
    (
        re.compile(r"^\s*(import|from)\s+google\.cloud\b"),
        SRC_ROOT / "stores" / "gcs",
        "google-cloud-storage",
    ),
]


def test_vendor_imports_confined_to_adapter_packages() -> None:
    """Vendor SDK imports must only appear inside their respective adapter directories."""
    violations: list[str] = []

    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        for pattern, allowed_dir, vendor_name in _VENDOR_PATTERNS:
            for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
                if pattern.match(line):
                    try:
                        py_file.relative_to(allowed_dir)
                    except ValueError:
                        violations.append(
                            f"{vendor_name} import outside {allowed_dir.relative_to(SRC_ROOT.parent.parent)}: "
                            f"{py_file}:{lineno}: {line.strip()}"
                        )

    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"Vendor SDK import(s) found outside their adapter package:\n  {detail}"
        )


# ---------------------------------------------------------------------------
# AC 3: core modules must not import concrete adapter namespaces
# ---------------------------------------------------------------------------

_FORBIDDEN_CORE_IMPORTS = re.compile(
    r"""
    ^\s*                        # optional leading whitespace
    (import|from)               # import keyword
    \s+
    kinoforge\.(providers|sources|engines)  # forbidden namespace
    (\s|\.|$)                   # followed by space, dot, or EOL
    """,
    re.VERBOSE,
)


def test_no_adapter_imports_in_core() -> None:
    """No file under src/kinoforge/core/ may import from providers/sources/engines."""
    violations: list[str] = []

    for py_file in sorted(CORE_ROOT.rglob("*.py")):
        for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
            if _FORBIDDEN_CORE_IMPORTS.match(line):
                violations.append(f"{py_file}:{lineno}: {line.strip()}")

    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"Forbidden adapter import(s) found in kinoforge.core:\n  {detail}"
        )


# ---------------------------------------------------------------------------
# AC 4: engine packages confined to their own subdirectory
# ---------------------------------------------------------------------------

# Each engine adapter must remain confined to its own ``engines/<name>/``
# subdirectory.  The sole permitted exception is ``src/kinoforge/_adapters.py``
# — the adapter self-registration hub that imports every engine for its
# side-effect registration call.
_ENGINE_ALLOWLIST: set[str] = {"comfyui", "diffusers", "hosted", "fake", "fal"}

_ADAPTER_HUB = SRC_ROOT / "_adapters.py"


def test_engine_packages_confined_to_their_subdir() -> None:
    """Each allowlisted engine must only be imported from its own subdir or the adapter hub."""
    violations: list[str] = []

    for engine_name in sorted(_ENGINE_ALLOWLIST):
        engine_dir = SRC_ROOT / "engines" / engine_name
        pattern = re.compile(
            rf"^\s*(import|from)\s+kinoforge\.engines\.{re.escape(engine_name)}(\s|\.|$)"
        )
        for py_file in sorted(SRC_ROOT.rglob("*.py")):
            # The adapter hub is permitted to import every engine.
            if py_file == _ADAPTER_HUB:
                continue
            # The engine's own subdir is permitted to import itself.
            try:
                py_file.relative_to(engine_dir)
                continue
            except ValueError:
                pass
            for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
                if pattern.match(line):
                    violations.append(
                        f"engines.{engine_name} imported outside its package "
                        f"({engine_dir.relative_to(SRC_ROOT.parent.parent)}): "
                        f"{py_file}:{lineno}: {line.strip()}"
                    )

    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"Engine package(s) leaked outside their subdirectory:\n  {detail}"
        )
