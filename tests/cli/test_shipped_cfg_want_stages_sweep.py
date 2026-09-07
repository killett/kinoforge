"""Sweep every shipped config through the warm-attach ``/health`` stage gate.

Why this file exists. The U14 fix replaced ``_cfg_want_stages``'s narrow local
derivation with a delegation to ``Config.capability_key().stages``. Replacing a
narrow derivation with a wider delegate widens the *input domain*, and the diff
shows only the deletion — not the stages the delegate emits that the local
version never could. That is precisely how U19 shipped inside the U14 fix for
the length of one review: the key also appends ``"interpolate"``, no pod can
advertise that term, and every ``kinoforge interpolate`` warm-attach would have
been refused with ``stage-mismatch`` and cold-booted a duplicate GPU.

The check that would have caught it is a sweep of every shipped config. The U14
entry in ``PROGRESS.md`` claims that sweep was run and that exactly eight
upscale-only cfgs changed. This file *is* that sweep, so the claim is
verifiable rather than asserted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.cli._commands import _cfg_want_stages
from kinoforge.core.config import load_config
from kinoforge.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples" / "configs"

# The closed vocabulary the in-pod server can actually put in /health's
# ``capabilities[]``. Transcribed BY HAND from the return values of
# ``_capability_for_model`` (src/kinoforge/engines/diffusers/servers/
# wan_t2v_server.py) rather than imported, so a change on either side breaks
# this test instead of the two silently agreeing on a new term.
# ``"upload"`` is always advertised but is never a stage a cfg *wants*.
ADVERTISABLE_STAGES = frozenset({"t2v", "upscale"})

# The eight configs the U14 delegation changed, listed by hand from the
# repo's upscale cfg filenames. Every one is ``upscale_only: true`` with
# ``models: []`` — a pod that by design never loads a Wan pipeline, so the
# pre-fix ``("t2v", "upscale")`` demanded a stage its own pod could never
# report and made the pod structurally unattachable.
U14_CHANGED_CFGS = frozenset(
    {
        "modal-diffusers-flashvsr-1080p-upscale.yaml",
        "modal-diffusers-flashvsr-x4-upscale.yaml",
        "runpod-diffusers-flashvsr-1080p-upscale.yaml",
        "runpod-diffusers-flashvsr-x4-torch26-upscale.yaml",
        "runpod-diffusers-flashvsr-x4-upscale.yaml",
        "runpod-diffusers-spandrel-x2-upscale.yaml",
        "skypilot-lambda-diffusers-flashvsr-upscale.yaml",
        "skypilot-vast-diffusers-flashvsr-upscale.yaml",
    }
)

# Measured 2026-09-07: 48 of the 59 YAML files under examples/configs are
# kinoforge configs; the other 11 are grid specs and batch manifests, which
# are different schemas and raise ConfigError by design. The floor guards
# against a load_config regression turning the whole sweep vacuous.
MIN_LOADABLE_CFGS = 40


def _want_stages_pre_u14(cfg: Any) -> tuple[str, ...]:  # noqa: ANN401 — duck-typed Config
    """The derivation ``49394b1d`` replaced, transcribed verbatim.

    Frozen historical baseline, not a reimplementation: the point of the
    comparison is to show which shipped configs moved when the delegation
    landed. Copied from ``git show 49394b1d^:src/kinoforge/cli/_commands.py``.
    """
    if getattr(cfg, "upscale", None) is not None:
        return ("t2v", "upscale")
    return ()


def _sweep() -> tuple[dict[str, Any], list[Path]]:
    """Load every shipped YAML; return (loaded cfgs by name, unloadable paths)."""
    loaded: dict[str, Any] = {}
    unloadable: list[Path] = []
    for path in sorted(EXAMPLES_DIR.rglob("*.yaml")):
        try:
            loaded[path.name] = load_config(str(path))
        except ConfigError:
            unloadable.append(path)
    return loaded, unloadable


@pytest.fixture(scope="module")
def swept() -> tuple[dict[str, Any], list[Path]]:
    return _sweep()


def test_the_sweep_actually_loads_the_shipped_configs(
    swept: tuple[dict[str, Any], list[Path]],
) -> None:
    """Anti-vacuity guard for the two assertions below.

    A ``load_config`` regression that raised on every real cfg would make both
    sweeps iterate an empty mapping and pass while proving nothing. This fails
    instead, and names what could not be loaded.
    """
    loaded, unloadable = swept
    assert len(loaded) >= MIN_LOADABLE_CFGS, (
        f"only {len(loaded)} shipped configs loaded (floor {MIN_LOADABLE_CFGS}); "
        f"the want-stages sweeps below would pass vacuously. Unloadable: "
        f"{[str(p.relative_to(REPO_ROOT)) for p in unloadable]}"
    )
    # Everything that does not load must be a different schema, not a broken cfg.
    unexpected = [
        p
        for p in unloadable
        if p.parent.name not in {"grids", "manifests"} and ".grid." not in p.name
    ]
    assert unexpected == [], (
        "these files under examples/configs are not grid specs or manifests but "
        f"still failed to load as configs: {[str(p) for p in unexpected]}"
    )


def test_no_shipped_cfg_demands_a_stage_no_pod_can_advertise(
    swept: tuple[dict[str, Any], list[Path]],
) -> None:
    """Every cfg's want-stages must be satisfiable by a healthy pod.

    ``_scan_warm_candidates`` refuses any candidate whose ``/health``
    ``capabilities[]`` is not a superset of ``_cfg_want_stages(cfg)``. A stage
    the server has no vocabulary for can therefore never be satisfied, so
    gating on it refuses EVERY candidate and cold-boots a duplicate GPU beside
    the idle pod — U14's leak, and U19 is the same leak one command over.

    This fails if ``"interpolate"`` is dropped from
    ``_HEALTH_UNGATEABLE_STAGES`` before ``_capability_for_model`` learns the
    ``rife-`` prefix (the half-done U19 remedy), and it fails if any future
    cfg family introduces a stage term the pod cannot report.
    """
    loaded, _ = swept
    offenders = {
        name: sorted(set(_cfg_want_stages(cfg)) - ADVERTISABLE_STAGES)
        for name, cfg in loaded.items()
        if set(_cfg_want_stages(cfg)) - ADVERTISABLE_STAGES
    }
    assert offenders == {}, (
        "these shipped configs demand /health capabilities the in-pod server "
        f"can never advertise (advertisable={sorted(ADVERTISABLE_STAGES)}): "
        f"{offenders}. Warm-attach would refuse every candidate and cold-boot "
        "a duplicate GPU for each of them."
    )


def test_the_u14_delegation_changed_exactly_the_eight_upscale_only_cfgs(
    swept: tuple[dict[str, Any], list[Path]],
) -> None:
    """Pin the enumerated claim in ``PROGRESS.md``'s U14 entry.

    Fails if a ninth shipped config enters the changed set (a new
    ``upscale_only`` cfg added without review, or a widening of
    ``capability_key().stages`` that starts gating a t2v cfg), and fails with
    an empty set if the delegation is reverted.
    """
    loaded, _ = swept
    changed = {
        name
        for name, cfg in loaded.items()
        if _want_stages_pre_u14(cfg) != _cfg_want_stages(cfg)
    }
    assert changed == set(U14_CHANGED_CFGS), (
        f"unexpected: {sorted(changed - U14_CHANGED_CFGS)}; "
        f"no longer changed: {sorted(U14_CHANGED_CFGS - changed)}"
    )
    # And the direction of every change is the phantom t2v being dropped.
    for name in sorted(changed):
        assert _cfg_want_stages(loaded[name]) == ("upscale",), (
            f"{name} changed to {_cfg_want_stages(loaded[name])}, not "
            "('upscale',) — the U14 claim is that these cfgs drop their "
            "phantom t2v requirement, nothing more"
        )
