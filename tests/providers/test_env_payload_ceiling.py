"""Behavior: no RunPod diffusers config's rendered env payload silently grows
past a committed baseline, and no RunPod diffusers config is ever allowed to
cross RunPod's undocumented create-mutation limit.

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
``minimax_h3_server.py``, ``_lora.py`` and ``_av_io.py`` — ~39.7 KB per config,
for modules only the Modal-only H3 server imports. The configs now name the
three server modules they actually import via ``embed_files``, which took the
worst config from 127,917 B to 88,301 B and every config under the ceiling.
``tests/providers/test_pod_embed_closure.py`` is what keeps them there; this
module guards the byte budget, that one guards the embed set.

Fix-wave-2 (also 2026-09-23) found three more RunPod diffusers configs that
had neither the fix nor any guard: ``grids/runpod-diffusers-wan-2_1-1_3b-
base.yaml``, ``grids/runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml`` and
``grids/runpod-diffusers-wan-2_2-14b-base.yaml`` measured 89,393 / 89,393 /
89,397 B — only 11,603 B under the ceiling — because
:func:`_runpod_diffusers_pod_configs` was built on
``tools.snapshot_launch_payloads.compute_configs()``, whose glob is
non-recursive and never walked ``grids/``. They now carry the same
``embed_files`` swap and this module discovers them directly (see that
function's docstring for the discovery rules, including the two
``extras/`` configs that are excluded because they cannot be measured in
this environment). The guard now covers sixteen configs, not thirteen.

What the three tests below actually assert:

1. ``test_baseline_covers_every_shipped_runpod_diffusers_config`` — every
   RunPod diffusers pod config in the tree has a ``_BASELINE_BYTES`` entry,
   and no stale entry survives a deleted/renamed config. A config with no
   baseline is a config nothing is measuring.
2. ``test_rendered_env_does_not_exceed_its_baseline`` — the RATCHET. A
   config's current measured size may not exceed its committed baseline;
   growth beyond that recorded state is what fails. Before U53 this ratchet
   held even while 8 configs sat over the ceiling — its job was never to
   enforce the ceiling itself, only to stop things from getting silently
   worse. Any bump of ``_BASELINE_BYTES`` must be a deliberate, reviewed
   act, not a reflex to unblock a failing test.
3. ``test_no_runpod_diffusers_config_crosses_the_ceiling`` — a guard that
   CANNOT be satisfied merely by editing ``_BASELINE_BYTES``. Parametrised
   over ``_configs_by_stem()`` (every RunPod diffusers config actually on
   disk, unconditionally — the dated frozen five-name exemption list this
   replaces existed only because eight configs were over the ceiling
   and could not be asserted about; U53 closed that breach), not over
   ``_BASELINE_BYTES`` itself — deleting a baseline entry must not be able
   to delete a config's coverage here too. However high a later commit
   bumps a config's entry in ``_BASELINE_BYTES``, this test independently
   re-measures the config and fails the instant it lands at/over the
   ceiling — a baseline bump alone can raise the ratchet's tolerance but
   can never raise this test's tolerance.

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
from kinoforge.core.errors import ExtrasNotInstalled
from tools.snapshot_launch_payloads import CONFIG_DIR, capture_payload

#: RunPod's ``podFindAndDeployOnDemand`` returns a raw HTTP 500 (no GraphQL
#: ``errors[]`` body) once the total env payload crosses roughly this many
#: bytes. Root-caused 2026-07-05 (CLAUDE.md "Known infra gotchas"); not an
#: RunPod-documented constant, just where it broke in practice.
_RUNPOD_CEILING_BYTES = 101_000

#: Committed snapshot of each shipped RunPod diffusers pod config's measured
#: rendered-env size, in bytes, re-measured 2026-09-23 after the U53 needs-only
#: embed fix (was ~39.7 KB higher per config; 8 entries were over the ceiling).
#: The ratchet test below asserts current measurements never exceed these.
#: The three ``*-base``/``*-base-no-loras`` entries are the ``grids/`` configs
#: U53 fix-wave-2 found un-guarded (Finding 1) — discovery used to be
#: non-recursive and never saw them; they measured 89,393 / 89,393 / 89,397 B
#: before the same ``embed_files`` swap applied here.
#:
#: The six FlashVSR/spandrel entries carry a second, smaller bump from the
#: SAME fix-wave-2 session: correcting a stale "64KB env-var ceiling"
#: comment in ``upscalers/flashvsr/_fetch_weights.py`` and
#: ``upscalers/spandrel/_engine.py`` grew those files' own source bytes —
#: and both are whole-package ``embed_modules`` entries on these configs, so
#: the comment text itself rides onto the pod. Deliberate, reviewed, and
#: nowhere near the ceiling (worst case 88,701 B, 12,299 B of headroom); the
#: alternative (reverting a factual correction to avoid a baseline bump) was
#: rejected as worse. (Also why those two files' own corrected comments cite
#: an approximate byte range rather than a pinned figure — a pinned number
#: goes stale the instant the comment reporting it changes length.)
_BASELINE_BYTES: dict[str, int] = {
    "runpod-diffusers-flashvsr-1080p-upscale": 88_647,
    "runpod-diffusers-flashvsr-x4-torch26-upscale": 88_679,
    "runpod-diffusers-flashvsr-x4-upscale": 88_647,
    "runpod-diffusers-rife-60fps-interpolate": 76_215,
    "runpod-diffusers-spandrel-x2-upscale": 73_479,
    "runpod-diffusers-wan-2_1-1_3b-base": 49_673,
    "runpod-diffusers-wan-2_1-1_3b-base-no-loras": 49_673,
    "runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke": 49_673,
    "runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid": 49_673,
    "runpod-diffusers-wan-2_2-14b-base": 49_673,
    "runpod-diffusers-wan-2_2-14b-t2v": 49_665,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale": 88_701,
    "runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale": 88_701,
    "runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release": 49_673,
    "runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale": 73_537,
    "runpod-diffusers-wan-2_2-14b-t2v-strength-grid": 49_673,
}


def _runpod_diffusers_pod_configs() -> list[Path]:
    """Return every RunPod diffusers pod config, including ones under subdirs.

    Unlike ``tools.snapshot_launch_payloads.compute_configs()`` — whose
    ``CONFIG_DIR.glob("*.yaml")`` is non-recursive — this walks
    ``examples/configs`` with ``rglob`` so configs living under a
    subdirectory are discovered too. That non-recursion is exactly why three
    RunPod diffusers pod configs under ``grids/`` (U53 fix-wave-2 Finding 1:
    ``grids/runpod-diffusers-wan-2_1-1_3b-base.yaml``,
    ``grids/runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml``,
    ``grids/runpod-diffusers-wan-2_2-14b-base.yaml``) still carried the
    whole-package ``embed_modules`` and sat un-guarded — measured at 89,393 /
    89,393 / 89,397 B, tighter to the ceiling than any of the thirteen
    top-level configs this module used to cover alone.

    Filters down to ``provider == "runpod"``, ``engine.kind == "diffusers"``,
    and pod mode — excluding the one serverless RunPod diffusers config
    (``runpod-diffusers-serverless.yaml``), which routes through a different
    GraphQL mutation (``saveTemplate``, U35) that carries no ``env`` at all,
    so there is nothing here for this guard to measure.

    Two kinds of YAML under ``examples/configs`` are not ``Config`` objects
    at all and are excluded by shape rather than by catching their
    ``ConfigError``: grid *definitions* (``grids/*.grid.yaml`` — a distinct
    schema that references the Config files above by path, not a config
    itself) and batch manifests (everything under ``manifests/`` — a list of
    per-run overrides). Excluding these by name/location, rather than
    swallowing the ``ConfigError`` they'd raise, keeps a genuinely malformed
    Config file loud instead of silently vanishing from the guard.

    Two more configs load fine as valid Configs but cannot be *measured* in
    this environment: ``extras/runpod-diffusers-seedvr2-3b-upscale.yaml`` and
    ``extras/runpod-diffusers-wan-2_2-14b-t2v-seedvr2-upscale.yaml`` both
    select ``upscale.engine: seedvr2``, and rendering their payload raises
    ``ExtrasNotInstalled`` (``kinoforge[seedvr]`` is a stub pending Phase 2
    vendoring — see ``src/kinoforge/upscalers/seedvr2/__init__.py``). Caught
    narrowly — ``ExtrasNotInstalled`` only, nothing broader — so a config
    that fails to render for any other reason still fails loudly here rather
    than silently dropping out of the guard, which is this whole finding in
    miniature.

    Returns:
        Sorted config paths.
    """
    out: list[Path] = []
    for p in sorted(CONFIG_DIR.rglob("*.yaml")):
        if p.name.endswith(".grid.yaml") or "manifests" in p.parts:
            continue
        cfg = load_config(str(p))
        if cfg.compute is None or cfg.compute.provider != "runpod":
            continue
        if cfg.engine.kind != "diffusers":
            continue
        if cfg.compute.mode == "serverless":
            continue
        try:
            capture_payload(p)
        except ExtrasNotInstalled:
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

    Before U53 this did NOT forbid the breach documented in the module
    docstring; a config already over budget stayed over budget here and the
    assertion still passed, because ``measured <= baseline`` where baseline
    already reflected that config's own over-ceiling state. That breach is
    now closed, but the same logic still applies to whatever grows next:
    only growth beyond the recorded baseline fails here, regardless of
    where that baseline sits relative to the ceiling — enforcing the
    ceiling itself is the other test's job.

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


@pytest.mark.parametrize("stem", sorted(_configs_by_stem()))
def test_no_runpod_diffusers_config_crosses_the_ceiling(stem: str) -> None:
    """EVERY RunPod diffusers config measures under the create-mutation ceiling.

    Unconditional, and deliberately not satisfiable by editing
    ``_BASELINE_BYTES`` — it re-measures the config and compares against the
    hard-coded :data:`_RUNPOD_CEILING_BYTES`. This replaces the dated frozen
    five-name exemption list, which existed only because eight
    configs were over the ceiling and could not be asserted about; U53 closed
    that breach on 2026-09-23, so the exemption is gone and the guard applies
    to every discovered config.

    Parametrised over :func:`_configs_by_stem` (what's actually on disk), not
    over ``sorted(_BASELINE_BYTES)`` — a baseline entry can only ever be
    bumped upward by a reviewer, never used to widen this test's tolerance,
    but *deleting* an entry from ``_BASELINE_BYTES`` would silently drop a
    still-shipped config from a parametrisation keyed on that dict. Keying on
    the discovered configs instead means the only way to stop this test
    covering a config is to delete the config itself.

    Bug caught: a new embed (a module under ``servers/``, a widened pip list, a
    longer boot script) pushes a config back over the edge. The ratchet test
    above would pass if someone bumped that config's baseline; this one cannot
    be widened at all, and the failure it prevents is a raw HTTP 500 at pod
    create with no GraphQL error body to explain it.

    Args:
        stem: Config filename stem, one entry of :func:`_configs_by_stem`.
    """
    cfg_path = _configs_by_stem()[stem]
    measured = _rendered_env_bytes(cfg_path)
    assert measured < _RUNPOD_CEILING_BYTES, (
        f"{stem}: rendered env {measured} B crossed the ~{_RUNPOD_CEILING_BYTES} "
        f"B RunPod create-mutation ceiling (CLAUDE.md 'Known infra gotchas') "
        f"— this WILL raw-500 the pod create with no GraphQL error body to "
        f"explain why. Shrink what the config embeds; do not raise the ceiling."
    )
