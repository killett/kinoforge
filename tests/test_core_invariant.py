"""Architectural invariant: kinoforge.core must never import concrete adapters.

Five tests:

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

4. core_does_not_import_image_engines — walks src/kinoforge/core and asserts
   that no line imports kinoforge.image_engines.*.  image_engines/ is
   registry-mediated; direct imports from core would break the registry
   indirection and force eager loading of every image engine.

5. engine_packages_confined_to_their_subdir — each engine adapter must remain
   within its own engines/<name>/ subtree or the adapter hub.  One explicit
   carve-out: image_engines/fal may import kinoforge.engines.fal.wire because
   wire.py is a shared pure-function helper module, not a full engine
   cross-dependency.

6. image_engine_fal_may_import_engines_fal_wire — smoke-import that verifies
   the cross-reference is live and the expected helper functions are present.
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
# Each entry: (pattern, list_of_allowed_dirs, vendor_name)
# A file is allowed if it lives under ANY of the allowed_dirs.
_VENDOR_PATTERNS: list[tuple[re.Pattern[str], list[Path], str]] = [
    (
        re.compile(r"^\s*(import|from)\s+(sky|skypilot)\b"),
        [SRC_ROOT / "providers" / "skypilot"],
        "sky/skypilot",
    ),
    (
        re.compile(r"^\s*(import|from)\s+runpod\b"),
        [SRC_ROOT / "providers" / "runpod"],
        "runpod",
    ),
    (
        # boto3 may also appear as a lazy import in core/auth.py (AWSSigV4 strategy)
        # and the bedrock_video engine adapter (_default_session_factory).
        # Lazy imports (inside method bodies) are fine; the subprocess-isolation test
        # in AC 8 verifies that boto3 never enters sys.modules at import time.
        re.compile(r"^\s*(import|from)\s+boto3\b"),
        [
            SRC_ROOT / "stores" / "s3",
            SRC_ROOT / "core",
            SRC_ROOT / "engines" / "bedrock_video",
        ],
        "boto3",
    ),
    (
        re.compile(r"^\s*(import|from)\s+google\.cloud\b"),
        [SRC_ROOT / "stores" / "gcs"],
        "google-cloud-storage",
    ),
    (
        re.compile(r"^\s*(import|from)\s+replicate\b"),
        [
            SRC_ROOT / "engines" / "replicate",
            SRC_ROOT / "image_engines" / "replicate",
        ],
        "replicate",
    ),
    (
        re.compile(r"^\s*(import|from)\s+runwayml\b"),
        [SRC_ROOT / "engines" / "runway"],
        "runwayml",
    ),
    (
        re.compile(r"^\s*(import|from)\s+fal_client\b"),
        [SRC_ROOT / "engines" / "fal"],
        "fal_client",
    ),
]


def test_vendor_imports_confined_to_adapter_packages() -> None:
    """Vendor SDK imports must only appear inside their respective adapter directories."""
    violations: list[str] = []

    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        for pattern, allowed_dirs, vendor_name in _VENDOR_PATTERNS:
            for lineno, line in enumerate(py_file.read_text().splitlines(), start=1):
                if pattern.match(line):
                    in_allowed = any(
                        _is_relative_to(py_file, allowed_dir)
                        for allowed_dir in allowed_dirs
                    )
                    if not in_allowed:
                        violations.append(
                            f"{vendor_name} import outside allowed dirs: "
                            f"{py_file}:{lineno}: {line.strip()}"
                        )

    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"Vendor SDK import(s) found outside their adapter package:\n  {detail}"
        )


def _is_relative_to(path: Path, base: Path) -> bool:
    """Return True if *path* is relative to *base* (Path.is_relative_to compat)."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# AC 3: core modules must not import concrete adapter namespaces
# ---------------------------------------------------------------------------

_FORBIDDEN_CORE_IMPORTS = re.compile(
    r"""
    ^\s*                        # optional leading whitespace
    (import|from)               # import keyword
    \s+
    kinoforge\.(providers|sources|engines|image_engines)  # forbidden namespace
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
# AC 3b: core modules must not import image_engines either
# ---------------------------------------------------------------------------


def test_core_does_not_import_image_engines() -> None:
    """Layer R: image_engines/ is registry-mediated. core/ must not import them.

    Bug guard: a direct import from core would break the registry indirection
    and force all consumers to eagerly load every image engine.
    """
    forbidden = "kinoforge.image_engines"
    offenders: list[str] = []
    for py_file in sorted(CORE_ROOT.rglob("*.py")):
        text = py_file.read_text()
        if forbidden in text:
            offenders.append(str(py_file))
    assert offenders == [], f"core/ files importing image_engines: {offenders}"


# ---------------------------------------------------------------------------
# AC 4: engine packages confined to their own subdirectory
# ---------------------------------------------------------------------------

# Each engine adapter must remain confined to its own ``engines/<name>/``
# subdirectory.  The sole permitted exception is ``src/kinoforge/_adapters.py``
# — the adapter self-registration hub that imports every engine for its
# side-effect registration call.
#
# Carve-out: ``image_engines/fal`` is allowed to import
# ``kinoforge.engines.fal.wire`` (and only that module).  ``wire.py``
# contains shared pure-function HTTP-shape helpers used by both the video and
# image fal adapters; it carries no provider/engine dependency of its own.
# Any other cross-adapter import from image_engines/ into engines/ is still a
# violation.
_ENGINE_ALLOWLIST: set[str] = {"comfyui", "diffusers", "hosted", "fake", "fal"}

_ADAPTER_HUB = SRC_ROOT / "_adapters.py"

# Files that are explicitly allowed to import a specific engines.* module.
# Each entry: (importer_file, allowed_import_pattern_regex_fragment)
_ENGINE_CROSS_REF_ALLOWLIST: list[tuple[Path, re.Pattern[str]]] = [
    (
        SRC_ROOT / "image_engines" / "fal" / "__init__.py",
        re.compile(
            r"^\s*(import|from)\s+kinoforge\.engines\.fal\.wire(\s|\.|$)"
            r"|^\s*(import|from)\s+kinoforge\.engines\.fal\s+import\s+wire\b"
        ),
    ),
]


def test_engine_packages_confined_to_their_subdir() -> None:
    """Each allowlisted engine must only be imported from its own subdir or the adapter hub.

    Carve-out: ``image_engines/fal/__init__.py`` may import
    ``kinoforge.engines.fal.wire`` (``from kinoforge.engines.fal import wire``
    or ``from kinoforge.engines.fal.wire import ...``).  ``wire.py`` is a
    pure-function helper module shared between the video and image fal
    adapters; it is not a full engine cross-dependency.  All other
    cross-adapter imports from ``image_engines/`` into ``engines/`` are still
    violations.
    """
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
                    # Check whether this (file, line) is in the cross-ref allowlist.
                    allowed = any(
                        py_file == allowed_file and allowed_pattern.match(line)
                        for allowed_file, allowed_pattern in _ENGINE_CROSS_REF_ALLOWLIST
                    )
                    if allowed:
                        continue
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


# ---------------------------------------------------------------------------
# AC 6: smoke-import verifying the image_engines/fal cross-reference is live
# ---------------------------------------------------------------------------


def test_image_engine_fal_may_import_engines_fal_wire() -> None:
    """Sibling-adapter cross-reference for shared pure-function helpers is allowed.

    Bug guard: a refactor that breaks this import would silently re-implement
    wire helpers in the image engine instead of reusing the canonical copy.
    """
    from kinoforge.engines.fal import wire  # noqa: PLC0415

    assert hasattr(wire, "build_status_url")
    assert hasattr(wire, "build_response_url")
    assert hasattr(wire, "interpret_status")


# ---------------------------------------------------------------------------
# AC 7: core/reaper.py purity contract (Layer V)
# ---------------------------------------------------------------------------

_REAPER_FORBIDDEN_IMPORTS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(import|from)\s+urllib\b"),
    re.compile(r"^\s*(import|from)\s+subprocess\b"),
    re.compile(r"^\s*(import|from)\s+threading\b"),
    re.compile(r"^\s*(import|from)\s+time\b"),
    re.compile(r"^\s*(import|from)\s+pathlib\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.providers\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.sources\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.engines\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.stores\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.cli\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.core\.lifecycle\b"),
]


def test_core_reaper_module_is_pure() -> None:
    """Layer V: core/reaper.py is pure — no I/O, no Ledger, no adapters.

    The sentinel-gate decision logic lives in classify(). Any I/O
    import here would let a future contributor reach into the ledger
    or a provider from inside classify(), violating the purity
    contract documented in spec §3.4. The contract is enforced
    architecturally so docstring vigilance is not load-bearing.
    """
    reaper_path = SRC_ROOT / "core" / "reaper.py"
    violations: list[str] = []
    for lineno, line in enumerate(reaper_path.read_text().splitlines(), start=1):
        for pattern in _REAPER_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{reaper_path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"core/reaper.py must be pure — forbidden import(s) found:\n  {detail}"
        )


# ---------------------------------------------------------------------------
# AC 7 (Layer 1): AuthStrategy ABC stable surface
# ---------------------------------------------------------------------------


def test_auth_strategy_abc_stable_surface() -> None:
    """Lock AuthStrategy ABC public method signatures against silent drift.

    To intentionally evolve the ABC, regenerate
    tests/fixtures/auth_strategy_baseline.json in the same commit:

        python -c "
        import inspect, json
        from pathlib import Path
        from kinoforge.core.auth import AuthStrategy
        sigs = {n: str(inspect.signature(getattr(AuthStrategy, n)))
                for n in ('credentials_present', 'health_check',
                          'redact_patterns', 'apply', 'client_kwargs')}
        Path('tests/fixtures/auth_strategy_baseline.json').write_text(
            json.dumps(sigs, indent=2) + '\n')
        "
    """
    import inspect
    import json

    from kinoforge.core.auth import AuthStrategy

    actual = {
        name: str(inspect.signature(getattr(AuthStrategy, name)))
        for name in (
            "credentials_present",
            "health_check",
            "redact_patterns",
            "apply",
            "client_kwargs",
        )
    }
    baseline_path = Path(__file__).parent / "fixtures" / "auth_strategy_baseline.json"
    expected = json.loads(baseline_path.read_text())

    assert actual == expected, (
        f"AuthStrategy ABC drifted from baseline.\n"
        f"  expected: {json.dumps(expected, indent=2)}\n"
        f"  actual:   {json.dumps(actual, indent=2)}\n"
        f"If intentional, regenerate {baseline_path} in the same commit."
    )


# ---------------------------------------------------------------------------
# Layer 4: RemoteSubmitPollBackend + RemoteSubmitPollEngine ABCs locked
# ---------------------------------------------------------------------------


def test_remote_submit_poll_backend_abc_stable_surface() -> None:
    """Lock the RemoteSubmitPollBackend + Engine public surface against drift.

    Any change here is intentional contract drift — update the
    baseline fixture in the same commit so reviewers can see the
    surface change.
    """
    import inspect
    import json

    from kinoforge.core.remote_backend import (
        RemoteSubmitPollBackend,
        RemoteSubmitPollEngine,
    )

    def _sig(cls: type) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in sorted(dir(cls)):
            if name.startswith("__"):
                continue
            obj = getattr(cls, name)
            if not callable(obj):
                continue
            try:
                out[name] = str(inspect.signature(obj))
            except (ValueError, TypeError):
                out[name] = "<unintrospectable>"
        return out

    actual = {
        "RemoteSubmitPollBackend": _sig(RemoteSubmitPollBackend),
        "RemoteSubmitPollEngine": _sig(RemoteSubmitPollEngine),
    }
    baseline_path = (
        Path(__file__).parent / "fixtures" / "remote_backend_abc_surface.json"
    )
    expected = json.loads(baseline_path.read_text())

    assert actual == expected, (
        "RemoteSubmitPollBackend / RemoteSubmitPollEngine public surface "
        "drifted from the locked baseline. If this is intentional, "
        f"regenerate {baseline_path} in the same commit.\n"
        f"  expected: {json.dumps(expected, indent=2, sort_keys=True)}\n"
        f"  actual:   {json.dumps(actual, indent=2, sort_keys=True)}"
    )


# ---------------------------------------------------------------------------
# AC 8 (Layer 1): core/auth.py does not eagerly load vendor SDKs
# ---------------------------------------------------------------------------


def test_core_auth_does_not_leak_sdk_imports() -> None:
    """Lazy SDK imports — boto3 / google.auth / botocore stay out of sys.modules.

    Verifies that importing kinoforge.core.auth in a fresh interpreter does
    NOT pull in any of the vendor SDKs the concrete strategies depend on.
    Construction of strategies is also exercised to ensure lazy paths fire
    only when methods are called, not at instantiation.

    We diff sys.modules before vs after the import+construction so that
    environment-level pre-loads (e.g. google.cloud namespace packages
    installed by sitecustomize) do not produce false positives.
    """
    script = (
        "import sys; "
        "_before = set(sys.modules); "
        "import kinoforge.core.auth as a; "
        "a.Bearer(env_var='FAL_KEY'); "
        "a.GCPServiceAccount(); "
        "a.AWSSigV4(region_name='us-east-1'); "
        "_added = set(sys.modules) - _before; "
        "print('|'.join(m for m in _added "
        "if m == 'boto3' or m.startswith('botocore') "
        "or m == 'google.auth' or m.startswith('google.cloud')))"
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
            f"core.auth import leaked vendor SDK modules into sys.modules:\n  {offending}"
        )


# ---------------------------------------------------------------------------
# B1 — core/sweeper_metrics.py purity contract (Layer W)
# ---------------------------------------------------------------------------

_SWEEPER_METRICS_FORBIDDEN_IMPORTS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(import|from)\s+threading\b"),
    re.compile(r"^\s*(import|from)\s+subprocess\b"),
    re.compile(r"^\s*(import|from)\s+time\b"),
    re.compile(r"^\s*(import|from)\s+pathlib\b"),
    re.compile(r"^\s*(import|from)\s+urllib\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.core\.lifecycle\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.providers\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.sources\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.engines\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.stores\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.cli\b"),
]


# ---------------------------------------------------------------------------
# C26 — core/util_endpoints.py + core/util_counter.py purity contract
# ---------------------------------------------------------------------------

_UTIL_FORBIDDEN_IMPORTS: list[re.Pattern[str]] = [
    re.compile(r"^\s*(import|from)\s+urllib\b"),
    re.compile(r"^\s*(import|from)\s+subprocess\b"),
    re.compile(r"^\s*(import|from)\s+threading\b"),
    re.compile(r"^\s*(import|from)\s+pathlib\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.providers\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.sources\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.engines\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.stores\b"),
    re.compile(r"^\s*(import|from)\s+kinoforge\.cli\b"),
]


def test_core_util_endpoints_module_is_pure() -> None:
    """C26: core/util_endpoints.py is pure (Protocol + frozen dataclass + gate)."""
    path = SRC_ROOT / "core" / "util_endpoints.py"
    violations: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for pattern in _UTIL_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"core/util_endpoints.py must be pure — forbidden import(s):\n  {detail}"
        )


def test_core_util_counter_module_is_pure() -> None:
    """C26: core/util_counter.py is pure (state-machine helper)."""
    path = SRC_ROOT / "core" / "util_counter.py"
    violations: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for pattern in _UTIL_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            f"core/util_counter.py must be pure — forbidden import(s):\n  {detail}"
        )


def test_runpod_util_satisfier_is_in_vendor_scan_path() -> None:
    """C26: vendor-SDK confinement test must scan providers/runpod/util.py.

    Regression guard: if a future refactor moved the new util satisfier
    out of providers/runpod/, the vendor-SDK confinement test would
    silently stop checking it. Verify the file path is exercised.
    """
    util_path = SRC_ROOT / "providers" / "runpod" / "util.py"
    assert util_path.exists(), f"expected {util_path} to exist for vendor scan"
    # The actual confinement check happens in
    # test_vendor_imports_confined_to_adapter_packages above. This test
    # is the existence-of-file regression guard.


def test_core_sweeper_metrics_module_is_pure() -> None:
    """Layer W: core/sweeper_metrics.py is pure — no I/O, no ledger import.

    The three renderers (human / JSON / Prom) take their input as a dict
    argument; any I/O here would couple the dashboard's output format
    to a specific storage backend. Architecturally enforced so a future
    contributor cannot reach into ledger.json directly.
    """
    path = SRC_ROOT / "core" / "sweeper_metrics.py"
    violations: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        for pattern in _SWEEPER_METRICS_FORBIDDEN_IMPORTS:
            if pattern.match(line):
                violations.append(f"{path}:{lineno}: {line.strip()}")
                break
    if violations:
        detail = "\n  ".join(violations)
        raise AssertionError(
            "core/sweeper_metrics.py must be pure — forbidden import(s) found:\n  "
            + detail
        )
