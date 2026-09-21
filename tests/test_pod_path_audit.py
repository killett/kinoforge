"""Lockdown: no module may use a provider's volume path as a runtime default.

``/workspace`` is RunPod's volume mount and ``/cache/hf`` is Modal's. A module
that names either as an ``os.environ.get`` / ``setdefault`` fallback is correct
on one provider, wrong on the others, and unwritable on every CI runner — which
is how CI went red on 2026-09-18 and stayed red for three days.

The dev container cannot reproduce that failure: ``/workspace`` is the repo root
here and is writable, so the offending test passed locally while spilling 281
stub files into the tree. This audit is the compensating control for an
environment difference we cannot reproduce. It is NOT equivalent to it — a path
spelled differently, built by string concatenation, or read through a helper
rather than ``os.environ.get`` directly is invisible here. A call the formatter
split across lines IS caught: the scan reads each file as one string precisely
so that it is. See the residual-risk note in the spec.

A fallback that is a bare identifier rather than a quoted literal — e.g.
``os.environ.get("X", _SOME_DEFAULT)`` — IS also caught: the scanner resolves
``_SOME_DEFAULT`` against a module-level ``NAME = "/..."`` assignment in the
same file and tests the resolved value. This closes the exact gap that let
``wan_t2v_server.py``'s ``_SPANDREL_WEIGHTS_DIR_DEFAULT`` and
``_FLASHVSR_WEIGHTS_DIR_DEFAULT`` escape earlier scans.

Two known violations are allowlisted **by name** (``_ALLOWLISTED_CONSTANTS``
below), not fixed here: ``_SPANDREL_WEIGHTS_DIR_DEFAULT`` and
``_FLASHVSR_WEIGHTS_DIR_DEFAULT`` in ``wan_t2v_server.py``. Both are tracked as
defect **U52** (PROGRESS.md STATUS INDEX) — the real fix spans seven files,
because provisioner-side writers in three engine modules stage weights at the
same paths the server reads, and moving only the read side desyncs the pair.
A green run of this audit is therefore **scoped, not total**: it means no
*new* provider-volume default was introduced, not that the file it guards is
free of them. The allowlist is deliberately narrow — keyed by exact constant
name, not by file — so a *different* named constant holding a provider path in
an allowlisted file is still caught; see
``test_allowlist_does_not_swallow_a_different_violation_in_the_same_file``.

Pairs with the autouse fixture in tests/conftest.py, which stops the spill
regardless of what any individual test does.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_SRC_ROOT: Path = _REPO_ROOT / "src" / "kinoforge"

# Volume mounts that belong to one provider each.
_PROVIDER_VOLUME_ROOTS: tuple[str, ...] = ("/workspace", "/cache/hf")

# The package that legitimately OWNS each root — the provider whose mount it is.
# Anywhere else, the literal is a guess about someone else's filesystem.
_OWNERS: dict[str, str] = {
    "/workspace": "providers/runpod/",
    "/cache/hf": "providers/modal/",
}

# Matches the fallback argument of os.environ.get(...) / os.environ.setdefault(...)
# — i.e. the value used when the env var is ABSENT. The same literal in a
# comment, a docstring, or a wire field the provider legitimately sends is fine.
_FALLBACK = re.compile(
    r"os\.environ\.(?:get|setdefault)\(\s*[^,()]+,\s*[\"'](?P<path>/[^\"']*)[\"']"
)

# Same call shape, but the fallback slot holds a bare identifier — e.g.
# ``os.environ.get("X", _SOME_DEFAULT)`` — instead of a quoted literal. The
# quote requirement in _FALLBACK means this never double-matches a call
# _FALLBACK already caught.
_FALLBACK_CONST = re.compile(
    r"os\.environ\.(?:get|setdefault)\(\s*[^,()]+,\s*(?P<const>[A-Za-z_][A-Za-z0-9_]*)\s*[,)]"
)

# Known violations that are real but deliberately out of scope for this
# branch — see U52 in PROGRESS.md's STATUS INDEX for the fix direction and
# why a server-only change would desync from the provisioner-side writers
# that stage weights at the same paths. Keyed by (file, constant name) so an
# allowlisted FILE does not blanket-suppress a DIFFERENT constant in it.
_ALLOWLISTED_CONSTANTS: dict[str, frozenset[str]] = {
    "engines/diffusers/servers/wan_t2v_server.py": frozenset(
        {"_SPANDREL_WEIGHTS_DIR_DEFAULT", "_FLASHVSR_WEIGHTS_DIR_DEFAULT"}
    ),
}


def _resolve_constant(source: str, name: str) -> str | None:
    """Resolve a bare identifier to a module-level string-literal assignment.

    Args:
        source: Whole file contents, searched as one string (see module
            docstring on why line-by-line scanning would miss the formatter's
            line-wrapped form).
        name: The identifier to resolve, e.g. ``_SPANDREL_WEIGHTS_DIR_DEFAULT``.

    Returns:
        The assigned string literal's value, or ``None`` if *name* is not
        assigned a string literal at module level (e.g. it is a parameter,
        an imported name, or assigned something other than a bare string).
    """
    match = re.search(
        rf"^{re.escape(name)}(?:\s*:\s*\w+)?\s*=\s*[\"'](?P<path>/[^\"']*)[\"']",
        source,
        re.MULTILINE,
    )
    return match.group("path") if match else None


def _scan(root: Path) -> list[tuple[str, int, str]]:
    """Report provider volume paths used as env fallbacks under *root*.

    Args:
        root: Package directory to walk for ``*.py`` files.

    Returns:
        ``(relative_path, line_number, offending_path)`` per violation,
        excluding files inside the owning provider's own package and the two
        constants named in ``_ALLOWLISTED_CONSTANTS`` (U52).
    """
    findings: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        source = path.read_text()
        # Searched as ONE string, not line by line. ruff-format splits an
        # over-long os.environ.get(...) across lines unprompted, and `\s` in
        # the pattern spans newlines, so a whole-file search still matches the
        # split form while a per-line scan silently misses it. That is the
        # likeliest way a real violation would escape, because nobody has to be
        # adversarial for it to happen — the formatter does it on its own.
        for match in _FALLBACK.finditer(source):
            found = match.group("path")
            lineno = source.count("\n", 0, match.start()) + 1
            for volume_root in _PROVIDER_VOLUME_ROOTS:
                if not found.startswith(volume_root):
                    continue
                if _OWNERS[volume_root] in rel:
                    continue  # the provider that owns this mount may name it
                findings.append((rel, lineno, found))
        # A quoted literal isn't the only way to spell the fallback slot — a
        # bare identifier naming a module-level constant is invisible to
        # _FALLBACK above. Resolve it and test the SAME way.
        for const_match in _FALLBACK_CONST.finditer(source):
            const_name = const_match.group("const")
            resolved = _resolve_constant(source, const_name)
            if resolved is None:
                continue  # not a string-literal module constant; not our business
            allowlisted = _ALLOWLISTED_CONSTANTS.get(rel, frozenset())
            if const_name in allowlisted:
                continue  # U52 — tracked, deliberately out of scope here
            lineno = source.count("\n", 0, const_match.start()) + 1
            for volume_root in _PROVIDER_VOLUME_ROOTS:
                if not resolved.startswith(volume_root):
                    continue
                if _OWNERS[volume_root] in rel:
                    continue
                findings.append((rel, lineno, resolved))
    return findings


def test_no_module_defaults_to_a_provider_volume_path() -> None:
    """Every module must let the provider name the pod's writable dirs.

    Catches the exact 2026-09-18 CI break returning under a new name: a server
    module — or any other — resolving /workspace into a constant at import.
    """
    findings = _scan(_SRC_ROOT)

    assert findings == [], (
        "provider volume path used as a runtime default:\n"
        + "\n".join(f"  {p}:{n} -> {v}" for p, n, v in findings)
        + "\nThe provider exports these via core.pod_paths.pod_path_env; "
        "fall back to pod-local scratch, never to another provider's mount."
    )


def test_audit_fires_on_a_planted_violation(tmp_path: Path) -> None:
    """Reverse-test: without it, the guard above could no-op forever.

    A guard that passes vacuously — wrong root, a regex that matches nothing,
    an rglob over an empty tree — looks exactly like a guard that works. This
    is the single most valuable test in the file, and it mirrors
    test_source_audit.py's planted-credential test for the same reason.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "rogue.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        'D = Path(os.environ.get("KINOFORGE_ARTIFACT_DIR", "/workspace/artifacts"))\n'
    )

    findings = _scan(tmp_path / "kinoforge")

    assert len(findings) == 1, findings
    rel, lineno, found = findings[0]
    assert rel == "engines/rogue.py"
    assert lineno == 3
    assert found == "/workspace/artifacts"


def test_audit_fires_on_modals_mount_too(tmp_path: Path) -> None:
    """Both provider roots are guarded, not just RunPod's.

    Catches a scanner that hardcodes /workspace — which would let the same
    defect reappear spelled as Modal's mount, on a RunPod server module.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "rogue.py").write_text(
        'import os\nH = os.environ.setdefault("HF_HOME", "/cache/hf")\n'
    )

    findings = _scan(tmp_path / "kinoforge")

    assert [f[2] for f in findings] == ["/cache/hf"]


def test_audit_sees_a_violation_the_formatter_split_across_lines(
    tmp_path: Path,
) -> None:
    """A long call that ruff-format wrapped is still caught.

    Catches scanning line by line. ruff-format splits an over-long
    ``os.environ.get(...)`` across lines on its own, which would leave a real
    violation invisible to a per-line scan. This is the likeliest way one would
    actually escape, because nobody has to be adversarial for it to happen.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "wrapped.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "ARTIFACT_DIR = Path(\n"
        "    os.environ.get(\n"
        '        "KINOFORGE_ARTIFACT_DIR_WITH_A_LONG_NAME", "/workspace/artifacts"\n'
        "    )\n"
        ")\n"
    )

    findings = _scan(tmp_path / "kinoforge")

    assert len(findings) == 1, findings
    rel, lineno, found = findings[0]
    assert rel == "engines/wrapped.py"
    assert lineno == 5, "the line number must point at the os.environ.get call"
    assert found == "/workspace/artifacts"


def test_audit_ignores_the_owning_provider_and_plain_prose(tmp_path: Path) -> None:
    """The owner may name its own mount; comments are never violations.

    Catches an over-broad audit. If this guard flagged RunPod's own module or
    every explanatory comment mentioning /workspace, it would be disabled
    within a week — and a disabled guard protects nothing.
    """
    root = tmp_path / "kinoforge"
    owner = root / "providers" / "runpod"
    owner.mkdir(parents=True)
    (owner / "__init__.py").write_text(
        'import os\nM = os.environ.get("MOUNT", "/workspace")\n'
    )
    prose = root / "engines"
    prose.mkdir(parents=True)
    (prose / "documented.py").write_text(
        "# /workspace is the RunPod volume mount; we do not hardcode it.\n"
        '"""Docstring mentioning /workspace/artifacts for context."""\n'
    )

    assert _scan(root) == []


def test_audit_fires_on_a_named_constant_fallback(tmp_path: Path) -> None:
    """A bare-identifier fallback resolving to a provider path IS caught.

    Catches the exact 2026-09-21 escape: ``wan_t2v_server.py`` fell back to
    ``os.environ.get("VAR", _SOME_DEFAULT)`` where ``_SOME_DEFAULT`` was a
    module-level constant holding ``"/workspace/..."``. The old ``_FALLBACK``
    regex required a quoted literal in the fallback slot, so a bare identifier
    escaped it entirely even though the resolved value was exactly the
    defect the audit exists to catch.
    """
    pkg = tmp_path / "kinoforge" / "engines"
    pkg.mkdir(parents=True)
    (pkg / "rogue_const.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        '_WEIGHTS_DIR_DEFAULT = "/workspace/models/rogue"\n'
        "\n"
        "\n"
        "def weights_dir() -> Path:\n"
        "    return Path(\n"
        '        os.environ.get("KINOFORGE_ROGUE_WEIGHTS_DIR", _WEIGHTS_DIR_DEFAULT)\n'
        "    )\n"
    )

    findings = _scan(tmp_path / "kinoforge")

    assert len(findings) == 1, findings
    rel, lineno, found = findings[0]
    assert rel == "engines/rogue_const.py"
    assert lineno == 9, "line number must point at the os.environ.get call"
    assert found == "/workspace/models/rogue"


def test_allowlist_does_not_swallow_a_different_violation_in_the_same_file(
    tmp_path: Path,
) -> None:
    """Allowlisting is keyed by constant NAME, not by file.

    Catches an over-broad allowlist — one that suppressed every violation in
    an allowlisted file rather than only the two named constants. That would
    be worse than the gap it closes: a fresh violation in the same file could
    ship silently forever. Plants a fake ``wan_t2v_server.py`` at the SAME
    relative path the real allowlist entry names, carrying one allowlisted
    constant (must stay clean) and one NOT allowlisted (must still fire).
    """
    pkg = tmp_path / "kinoforge" / "engines" / "diffusers" / "servers"
    pkg.mkdir(parents=True)
    (pkg / "wan_t2v_server.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        '_SPANDREL_WEIGHTS_DIR_DEFAULT = "/workspace/models/spandrel"\n'
        '_NOT_ALLOWLISTED_DEFAULT = "/workspace/models/not-allowlisted"\n'
        "\n"
        "\n"
        "def spandrel_dir() -> Path:\n"
        "    return Path(\n"
        '        os.environ.get("A", _SPANDREL_WEIGHTS_DIR_DEFAULT)\n'
        "    )\n"
        "\n"
        "\n"
        "def other_dir() -> Path:\n"
        "    return Path(\n"
        '        os.environ.get("B", _NOT_ALLOWLISTED_DEFAULT)\n'
        "    )\n"
    )

    findings = _scan(tmp_path / "kinoforge")

    assert len(findings) == 1, findings
    rel, _lineno, found = findings[0]
    assert rel == "engines/diffusers/servers/wan_t2v_server.py"
    assert found == "/workspace/models/not-allowlisted"


def test_allowlisted_constants_are_real_wan_t2v_server_violations() -> None:
    """The allowlist names two constants that actually exist and resolve.

    Catches a stale or typo'd allowlist entry — one naming a constant that no
    longer exists (renamed, deleted) in the file it claims to cover, which
    would silently allowlist nothing while looking like it allowlists
    something.
    """
    server_path = _SRC_ROOT / "engines" / "diffusers" / "servers" / "wan_t2v_server.py"
    source = server_path.read_text()
    rel = "engines/diffusers/servers/wan_t2v_server.py"

    for const_name in _ALLOWLISTED_CONSTANTS[rel]:
        resolved = _resolve_constant(source, const_name)
        assert resolved is not None, f"{const_name} no longer resolves"
        assert resolved.startswith("/workspace"), (
            f"{const_name} resolved to {resolved!r}, not a /workspace path — "
            "allowlist entry may be stale"
        )
