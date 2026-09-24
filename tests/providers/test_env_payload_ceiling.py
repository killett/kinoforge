"""Behavior: no RunPod diffusers config's rendered env payload silently grows
past a committed baseline, and no config currently safe under RunPod's
undocumented create-mutation limit is ever allowed to cross it.

--- Why this guard is a RATCHET, not a flat budget (read before touching it) --

Task 9's brief specified a single hard budget: every RunPod diffusers
config's rendered env must stay under 90 KB. That number predates
measurement. Two measurements taken since (documented in
``.superpowers/sdd/2026-09-22-h3-lora-shared-seam/task-5-report.md`` and
this task's own ``task-9-report.md``) found the largest RunPod diffusers
configs already at ~113-128 KB of rendered env — comfortably past both the
90 KB budget and the ~101 KB ceiling at which RunPod's
``podFindAndDeployOnDemand`` mutation starts returning a raw HTTP 500
(CLAUDE.md "Known infra gotchas"). A flat "< 90 KB" assertion against that
reality is vacuous: every RunPod diffusers config using the
FlashVSR/RIFE/spandrel bootstraps would need to be exempted, leaving nothing
for the assertion to actually guard.

That breach is CLOSED as of 2026-09-23 (U53). The cause was not the encoding
but what got embedded: ``embed_modules: ["kinoforge.engines.diffusers.servers"]``
walks a package DIRECTORY, so every RunPod diffusers pod carried
``minimax_h3_server.py``, ``_lora.py`` and ``_av_io.py`` — ~39.4 KB per config,
for modules only the Modal-only H3 server imports. The configs now name the
three server modules they actually import via ``embed_files``, which took the
worst config from 127,917 B to 88,575 B and every config under the ceiling.
``tests/providers/test_pod_embed_closure.py`` is what keeps them there; this
module guards the byte budget, that one guards the embed set.

What the three tests below actually assert:

1. ``test_baseline_covers_every_shipped_runpod_diffusers_config`` — every
   RunPod diffusers pod config in the tree has a ``_BASELINE_BYTES`` entry,
   and no stale entry survives a deleted/renamed config. A config with no
   baseline is a config nothing is measuring.
2. ``test_rendered_env_does_not_exceed_its_baseline`` — the RATCHET. A
   config's current measured size may not exceed its committed baseline.
   This does not forbid the breach above (already true for 8 configs); it
   forbids the breach growing without a deliberate, reviewed bump of
   ``_BASELINE_BYTES``.
3. ``test_no_runpod_diffusers_config_crosses_the_ceiling`` — a guard that
   CANNOT be satisfied merely by editing ``_BASELINE_BYTES``. Parametrised
   over every entry in ``_BASELINE_BYTES`` (i.e. every RunPod diffusers
   config, unconditionally — the dated frozen five-name exemption list this
   replaces existed only because eight configs were over the ceiling
   and could not be asserted about; U53 closed that breach). However high a
   later commit bumps a config's entry in ``_BASELINE_BYTES``, this test
   independently re-measures the config and fails the instant it lands
   at/over the ceiling — a baseline bump alone can raise the ratchet's
   tolerance but can never raise this test's tolerance.

Measurement method (matches how ``tools/snapshot_launch_payloads.py`` freezes
the launch-payload goldens, so the numbers here are directly comparable to
those goldens and to Task 5's report): ``capture_payload`` drives each
config's config-selected provider through an injected transport (frozen
clock, synthetic credentials, no network) and returns the exact
``variables.input`` dict RunPod's ``podFindAndDeployOnDemand`` mutation would
carry. ``_rendered_env_bytes`` sums ``len(key) + len(value)`` over every
``input.env`` entry — the RunPod wire shape (a list of ``{key, value}``
pairs) — not just the ``KINOFORGE_PROVISION_SCRIPT`` blob alone, since it is
the total request body RunPod's HTTP layer chokes on.

To re-baseline after a deliberate change to what a RunPod diffusers config
embeds, run::

    pixi run python -c "
    from tests.providers.test_env_payload_ceiling import (
        _rendered_env_bytes, _runpod_diffusers_pod_configs)
    for p in _runpod_diffusers_pod_configs():
        print(f'{_rendered_env_bytes(p):>10}  {p.stem}')
    "

and copy the numbers into ``_BASELINE_BYTES`` by hand — never edit this
file's assertions to make it pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from tools.snapshot_launch_payloads import capture_payload, compute_configs

#: RunPod's ``podFindAndDeployOnDemand`` returns a raw HTTP 500 (no GraphQL
#: ``errors[]`` body) once the total env payload crosses roughly this many
#: bytes. Root-caused 2026-07-05 (CLAUDE.md "Known infra gotchas"); not an
#: RunPod-documented constant, just where it broke in practice.
_RUNPOD_CEILING_BYTES = 101_000

#: Committed snapshot of each shipped RunPod diffusers pod config's measured
#: rendered-env size, in bytes, re-measured 2026-09-23 after the U53 needs-only
#: embed fix (was ~39.4 KB higher per config; 8 entries were over the ceiling).
#: The ratchet test below asserts current measurements never exceed these.
_BASELINE_BYTES: dict[str, int] = {
    "runpod-diffusers-flashvsr-1080p-upscale": 88_243,
    "runpod-diffusers-flashvsr-x4-torch26-upscale": 88_275,
    "runpod-diffusers-flashvsr-x4-upscale": 88_243,
    "runpod-diffusers-rife-60fps-interpolate": 76_215,
    "runpod-diffusers-spandrel-x2-upscale": 73_123,
    "runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke": 49_673,
    "runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid": 49_673,
    "runpod-diffusers-wan-2_2-14b-t2v": 49_665,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale": 88_301,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale": 88_301,
    "runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release": 49_673,
    "runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale": 73_173,
    "runpod-diffusers-wan-2_2-14b-t2v-strength-grid": 49_673,
}


def _runpod_diffusers_pod_configs() -> list[Path]:
    """Return the shipped example configs RunPod boots as diffusers pods.

    Filters ``compute_configs()`` (every example config with a ``compute:``
    block) down to ``provider == "runpod"``, ``engine.kind == "diffusers"``,
    and pod mode — excluding the one serverless RunPod diffusers config
    (``runpod-diffusers-serverless.yaml``), which routes through a different
    GraphQL mutation (``saveTemplate``, U35) that carries no ``env`` at all,
    so there is nothing here for this guard to measure.

    Returns:
        Sorted config paths.
    """
    out: list[Path] = []
    for p in compute_configs():
        cfg = load_config(str(p))
        if cfg.compute is None or cfg.compute.provider != "runpod":
            continue
        if cfg.engine.kind != "diffusers":
            continue
        if cfg.compute.mode == "serverless":
            continue
        out.append(p)
    return out


def _rendered_env_bytes(cfg_path: Path) -> int:
    """Measure a config's rendered RunPod env payload, the way RunPod packs it.

    Args:
        cfg_path: Path to a RunPod diffusers pod config.

    Returns:
        ``sum(len(key) + len(value) for each input.env entry)`` — the total
        bytes RunPod's ``podFindAndDeployOnDemand`` mutation carries for the
        env block, not just the provision-script value alone.
    """
    payload = capture_payload(cfg_path)
    env_list = payload["input"]["env"]
    env = {e["key"]: e["value"] for e in env_list}
    return sum(len(k) + len(v) for k, v in env.items())


def _configs_by_stem() -> dict[str, Path]:
    """Map every discovered RunPod diffusers pod config to its path by stem.

    Returns:
        ``{stem: path}`` for every config :func:`_runpod_diffusers_pod_configs`
        returns.
    """
    return {p.stem: p for p in _runpod_diffusers_pod_configs()}


def test_baseline_covers_every_shipped_runpod_diffusers_config() -> None:
    """Every RunPod diffusers pod config has a baseline entry, and no more.

    Bug caught: a new RunPod diffusers config ships with no
    ``_BASELINE_BYTES`` entry — it would silently sit outside every guard in
    this module, exactly the kind of hole a flat, unmeasured budget would
    leave open. The reverse direction (a stale entry for a deleted/renamed
    config) is caught too, so ``_BASELINE_BYTES`` cannot drift from what is
    actually shipped.
    """
    present = set(_configs_by_stem())
    baselined = set(_BASELINE_BYTES)
    missing = sorted(present - baselined)
    orphaned = sorted(baselined - present)
    assert not missing, (
        f"no _BASELINE_BYTES entry for: {missing}; measure with "
        f"_rendered_env_bytes and add it deliberately"
    )
    assert not orphaned, (
        f"_BASELINE_BYTES entries for configs that no longer exist or no "
        f"longer qualify: {orphaned}; remove them"
    )


@pytest.mark.parametrize("stem", sorted(_BASELINE_BYTES))
def test_rendered_env_does_not_exceed_its_baseline(stem: str) -> None:
    """A config's rendered env may not grow past its committed baseline.

    The ratchet: catches the next embed addition (a new module under
    ``kinoforge.engines.diffusers.servers``, a widened pip list, a longer
    boot script) crossing further past RunPod's create-mutation ceiling,
    whose only symptom at runtime is an unexplained raw HTTP 500 at pod
    create with no GraphQL error body — the failure mode that cost a full
    session to root-cause on 2026-07-05.

    Does NOT forbid the pre-existing breach documented in the module
    docstring; a config already over budget stays over budget here and this
    assertion still passes, because ``measured <= baseline`` where baseline
    already reflects that config's own over-ceiling state. Growth beyond
    that recorded state is what fails.

    Args:
        stem: Config filename stem, one entry of ``_BASELINE_BYTES``.
    """
    cfg_path = _configs_by_stem()[stem]
    measured = _rendered_env_bytes(cfg_path)
    baseline = _BASELINE_BYTES[stem]
    assert measured <= baseline, (
        f"{stem}: rendered env grew to {measured} B from a committed "
        f"baseline of {baseline} B (+{measured - baseline} B). If this "
        f"growth is intentional, review what grew and bump "
        f"_BASELINE_BYTES[{stem!r}] deliberately — do not raise it to make "
        f"this test pass without reading the diff that caused it."
    )


@pytest.mark.parametrize("stem", sorted(_BASELINE_BYTES))
def test_no_runpod_diffusers_config_crosses_the_ceiling(stem: str) -> None:
    """EVERY RunPod diffusers config measures under the create-mutation ceiling.

    Unconditional, and deliberately not satisfiable by editing
    ``_BASELINE_BYTES`` — it re-measures the config and compares against the
    hard-coded :data:`_RUNPOD_CEILING_BYTES`. This replaces the dated frozen
    five-name exemption list, which existed only because eight
    configs were over the ceiling and could not be asserted about; U53 closed
    that breach on 2026-09-23, so the exemption is gone and the guard applies
    to all thirteen.

    Bug caught: a new embed (a module under ``servers/``, a widened pip list, a
    longer boot script) pushes a config back over the edge. The ratchet test
    above would pass if someone bumped that config's baseline; this one cannot
    be widened at all, and the failure it prevents is a raw HTTP 500 at pod
    create with no GraphQL error body to explain it.

    Args:
        stem: Config filename stem, one entry of ``_BASELINE_BYTES``.
    """
    cfg_path = _configs_by_stem()[stem]
    measured = _rendered_env_bytes(cfg_path)
    assert measured < _RUNPOD_CEILING_BYTES, (
        f"{stem}: rendered env {measured} B crossed the ~{_RUNPOD_CEILING_BYTES} "
        f"B RunPod create-mutation ceiling (CLAUDE.md 'Known infra gotchas') "
        f"— this WILL raw-500 the pod create with no GraphQL error body to "
        f"explain why. Shrink what the config embeds; do not raise the ceiling."
    )
