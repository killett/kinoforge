"""U32, generalised: no BAKEABLE step may reference an unbound variable.

U32 was one unguarded ``${HF_TOKEN}`` in one bakeable step, and it cost two
dead Modal image builds before anything was created. Nothing stopped the next
one, so this closes the CLASS rather than the instance —
``tests/upscalers/test_spandrel_provision_set_u_safe.py`` widened from that one
upscaler to every shipped config.

**The asymmetry that makes this worth running.** Provision scripts run under
``set -euo pipefail``. RunPod runs the whole script at container start, where
the run env is present, so a bare ``${HF_TOKEN}`` is harmless there and no
RunPod test can see the defect. Modal is the only provider that runs the
bakeable subset at IMAGE-BUILD time, where run secrets do not exist — and
``set -u`` turns the missing variable into ``HF_TOKEN: unbound variable``,
exit 1, a failed build, and no pod. So the guard is scoped to bakeable steps
on purpose: a runtime-only step referencing a run-provided variable is correct,
and flagging it would be a false alarm.

**Who decides whether an expansion is guarded: bash, not a regex.** For every
variable the steps reference, the check runs the expansion exactly as written
through real ``bash -u`` with an empty environment. ``${VAR:-}``,
``${VAR-x}``, ``${#VAR}`` and friends therefore pass because bash says they
do, not because a pattern was written to allow them.

**What it does not reach**, stated so the green is not over-read:

- Only variables that appear in the rendered text. A step that fetches a
  script and runs it can still reference anything at build time.
- Only configs that render offline. Engines whose ``render_provision`` raises
  (hosted / fal / bedrock — they provision nothing) are skipped by
  construction, and configs the registry cannot build are reported, not
  silently dropped.
- Nothing about whether a variable holds the RIGHT value — only that reading
  it does not abort the script.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

# Importing the CLI package is what registers the engines, upscalers and
# interpolators; `kinoforge.core.registry` alone starts empty, and a config
# would fail with UnknownAdapter rather than render.
import kinoforge.cli  # noqa: F401
from kinoforge.core.config import load_config
from kinoforge.core.registry import get_engine

if TYPE_CHECKING:
    from kinoforge.core.interfaces import SetupStep

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "configs"

#: Variables the IMAGE-BUILD environment genuinely provides, so referencing one
#: unguarded is safe. Deliberately EMPTY: today every bakeable reference is
#: either guarded (``${PYTHONPATH:-}``) or assigned in the same script, so
#: nothing needs an exemption. An addition here is a claim about what the
#: builder's environment guarantees and needs the evidence written beside it.
_BAKE_TIME_VARIABLES: frozenset[str] = frozenset()

#: ``$NAME`` or ``${NAME...}``. Positional and special parameters (``$1``,
#: ``$@``, ``$?``, ``$$``) are excluded by requiring a letter or underscore
#: first — bash never leaves those unset, so they are not this defect.
_REFERENCE = re.compile(
    r"\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)[^}]*\}|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)

#: ``NAME=``, ``export NAME=``, ``local NAME=``, and ``read NAME``. A variable
#: the script sets before it reads is bound by the time ``set -u`` looks.
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+|local\s+|declare\s+(?:-\w+\s+)*)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)="
    r"|^\s*read\s+(?:-\w+\s+)*(?P<read>[A-Za-z_][A-Za-z0-9_]*)"
    r"|^\s*for\s+(?P<loop>[A-Za-z_][A-Za-z0-9_]*)\s+in\b",
    re.MULTILINE,
)

#: ``<<'EOF'`` / ``<<"EOF"`` — a QUOTED heredoc delimiter. bash performs no
#: expansion inside such a body, so a ``${VAR}`` there is literal text (an
#: embedded Python or YAML document, say) and must not be flagged. An
#: UNQUOTED ``<<EOF`` body IS expanded, so those lines stay in scope.
_QUOTED_HEREDOC = re.compile(r"<<-?\s*(?:'(?P<sq>[^']+)'|\"(?P<dq>[^\"]+)\")")


def _expanded_lines(script: str) -> list[str]:
    """Return the script's lines minus the bodies of quoted heredocs.

    Args:
        script: A bash fragment, possibly multi-line.

    Returns:
        The lines bash would actually perform parameter expansion on.
    """
    kept: list[str] = []
    terminator: str | None = None
    for line in script.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        kept.append(line)
        match = _QUOTED_HEREDOC.search(line)
        if match:
            terminator = match.group("sq") or match.group("dq")
    return kept


def _unguarded_expansions(script: str) -> list[tuple[str, str]]:
    """Return ``(variable, expansion)`` pairs the script reads without a guard.

    "Without a guard" is decided by real bash: each expansion is run verbatim
    under ``set -u`` with an empty environment, and only the ones that actually
    abort are returned.

    Args:
        script: A bash fragment, possibly multi-line.

    Returns:
        Every unguarded reference, in encounter order, deduplicated by
        expansion text.
    """
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to judge these expansions"

    lines = _expanded_lines(script)
    assigned = {
        m.group("name") or m.group("read") or m.group("loop")
        for m in _ASSIGNMENT.finditer("\n".join(lines))
    }

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        for match in _REFERENCE.finditer(line):
            name = match.group("braced") or match.group("bare")
            expansion = match.group(0)
            if name in assigned or name in _BAKE_TIME_VARIABLES:
                continue
            if expansion in seen:
                continue
            seen.add(expansion)
            # `set -u` is the whole point: without it bash expands an unset
            # variable to the empty string and exits 0, and this check would
            # wave everything through. Unquoted, so an expansion carrying its
            # own quotes (`${VAR:-"x"}`) stays balanced as written.
            probe = subprocess.run(  # noqa: S603
                [bash, "-c", f"set -u\n: {expansion}"],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin"},  # every candidate deliberately unset
                check=False,
            )
            if probe.returncode != 0:
                found.append((name, expansion))
    return found


def _bakeable_steps(cfg_path: Path) -> tuple[SetupStep, ...]:
    """Render *cfg_path* and return only the steps a builder would bake."""
    cfg = load_config(cfg_path).model_dump()
    kind = (cfg.get("engine") or {}).get("kind")
    rendered = get_engine(str(kind))().render_provision(cfg)
    return tuple(step for step in rendered.setup_steps if step.bakeable)


def _provisioning_cfgs() -> list[Path]:
    """Shipped configs whose engine renders a provision script at all.

    Hosted engines (fal, bedrock, replicate, runway, the generic hosted one)
    provision nothing and raise ``NotImplementedError``; there is no image to
    bake, so they are outside this guard rather than exempted from it.
    """
    provisioning = {"diffusers", "comfyui"}
    found: list[Path] = []
    for path in sorted(_EXAMPLES.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        engine = doc.get("engine")
        if isinstance(engine, dict) and engine.get("kind") in provisioning:
            found.append(path)
    return found


_CFGS = _provisioning_cfgs()


def test_the_sweep_actually_covers_the_shipped_configs() -> None:
    """The parametrisation must not quietly collapse to nothing.

    Bug caught: a rename of ``engine.kind``, a moved examples directory, or a
    glob that stops matching turns every case below into zero cases, and the
    suite reports green while checking nothing. U25's lesson — a guard that
    cannot be shown to run is not yet a guard.
    """
    assert len(_CFGS) >= 20, f"only {len(_CFGS)} provisioning configs found: {_CFGS}"


def test_the_sweep_has_expansions_to_judge() -> None:
    """The bakeable steps must actually contain variable references.

    Bug caught: the per-config assertions passing vacuously. If the engines
    stopped emitting any ``$VAR`` in a bakeable step — or ``bakeable`` stopped
    being set, or the heredoc rule swallowed every line — every case above
    would check an empty list and still report green. Today it is
    ``export PYTHONPATH=/tmp/kfsrv:${PYTHONPATH:-}`` doing the work, on every
    diffusers config.
    """
    with_refs = [
        path
        for path in _CFGS
        if any(
            _REFERENCE.search(line)
            for step in _bakeable_steps(path)
            for line in _expanded_lines(step.script)
        )
    ]

    assert len(with_refs) >= 15, (
        f"only {len(with_refs)} of {len(_CFGS)} configs bake a step that reads "
        f"any variable — the sweep above is close to vacuous: {with_refs}"
    )


@pytest.mark.parametrize("cfg_path", _CFGS, ids=lambda p: p.name)
def test_bakeable_steps_reference_no_unbound_variable(cfg_path: Path) -> None:
    """Every bakeable step survives ``set -u`` with an empty environment.

    Bug caught: U32 exactly — the spandrel weights fetch interpolated
    ``${HF_TOKEN}`` with no default, and because Modal bakes that step the
    image build died with ``HF_TOKEN: unbound variable`` before a pod existed.
    Two live builds died there on 2026-09-11. The same shape in any future
    bakeable step fails here instead, offline and for free.
    """
    steps = _bakeable_steps(cfg_path)
    offenders: list[tuple[str, str]] = []
    for step in steps:
        offenders.extend(_unguarded_expansions(step.script))

    assert not offenders, (
        f"{cfg_path.name} bakes a step that reads an unset variable under "
        f"set -u — this is the U32 Modal image-build failure. Guard it with "
        f"${{VAR:-}} (or give the value a default): {offenders}"
    )


def test_the_check_flags_the_pre_u32_spandrel_shape() -> None:
    """Falsification: the exact line U32 shipped must be reported.

    Bug caught: the checker silently passing everything — a heredoc rule that
    swallows real lines, a regex that stops matching ``${VAR}``, an assignment
    scan that marks everything as locally bound. Without this, the sweep above
    would go green on a codebase full of unguarded expansions and nobody would
    know. The input is the literal shape that failed live, not a paraphrase.
    """
    pre_fix = 'curl -fsSL -H "Authorization: Bearer ${HF_TOKEN}" -o /tmp/w.pth "$URL"'

    offenders = _unguarded_expansions(pre_fix)

    assert ("HF_TOKEN", "${HF_TOKEN}") in offenders, offenders


def test_the_check_accepts_the_guards_bash_accepts() -> None:
    """A guarded or locally-assigned reference must not be reported.

    Bug caught: the opposite failure — a checker so blunt that the only way to
    keep it green is to stop writing variables at all. ``${VAR:-}`` is the fix
    U32 actually shipped and ``export PYTHONPATH=/tmp/kfsrv:${PYTHONPATH:-}``
    is on every diffusers config today; if either were flagged the guard would
    be turned off within a week.
    """
    guarded = "\n".join(
        [
            'echo "${HF_TOKEN:-}"',
            'echo "${HF_TOKEN-fallback}"',
            "export PYTHONPATH=/tmp/kfsrv:${PYTHONPATH:-}",
            "MODEL_DIR=/workspace/models",
            'mkdir -p "${MODEL_DIR}"',
            "cat <<'PY' > /tmp/x.py",
            'print("${NOT_A_SHELL_VAR}")',
            "PY",
        ]
    )

    assert _unguarded_expansions(guarded) == []
