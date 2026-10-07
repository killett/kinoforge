"""Behavior: a RunPod config's self-terminator cap must outlast its own boot.

RunPod has no provider-side idle autostop, so kinoforge arms a pod-side
self-terminator instead. ``providers/runpod/selfterm.py`` states its contract:
two independent BOOT-RELATIVE caps, ``start + max_lifetime - time_buffer`` and
``start + 2 * idle_timeout``, whichever elapses first. The enforced lifetime is
therefore::

    min(2 * idle_timeout, max_lifetime - time_buffer)

Both caps are measured from pod start, NOT from "ready". So when that minimum
is at or below ``boot_timeout``, the pod is armed to kill itself before — or
exactly as — the boot it was given permission to take completes. The operator
pays for a full weight fetch and gets a pod that terminates at the moment it
becomes useful, and the failure reads like a provider-side reclamation rather
than an arithmetic inversion in the config.

The `kinoforge text` branch hit this on all three of its new configs and fixed
them by hand (``e301a6a0``). A hand fix to whichever configs are under the
microscope is how a class defect survives — U42 and U53 are both on record in
this repo for exactly that — so this file makes the invariant a property of the
RunPod config set.

**Scope: RunPod only.** Modal and SkyPilot derive their deadline from
``boot_timeout`` rather than from the idle/lifetime pair (see
``validation/checks/capabilities.py`` and the per-provider deadline notes
there), so ``min(2*idle, max_lifetime-buffer) <= boot_timeout`` is not a defect
on those providers and they are deliberately out of scope here. Twelve of them
would trip this arithmetic; none is a bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import Config, load_config

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "examples" / "configs"

#: Pre-existing offenders, measured 2026-10-05 by sweeping the tree with this
#: file's own helpers. They PREDATE this guard — all four are Wan 2.1 1.3B
#: RunPod configs carrying ``idle_timeout: 10m`` / ``max_lifetime: 1h`` /
#: ``time_buffer: 2m`` against ``boot_timeout: 30m``, i.e. a 1200 s cap under
#: an 1800 s boot. They are FOLLOW-UPS, deliberately NOT fixed in the
#: `kinoforge text` fix wave (that wave changes no config and moves no launch
#: golden). The guard-the-guard test below asserts every entry still exists AND
#: still offends, so this allowlist can only ever shrink: fix a config and its
#: name must come out of here.
LEGACY_INVERTED: frozenset[str] = frozenset(
    {
        "runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml",
        "runpod-diffusers-wan-2_1-1_3b-base.yaml",
        "runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml",
        "runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml",
    }
)


def _runpod_configs() -> list[Path]:
    """Every shipped RunPod config that declares a lifecycle, recursively.

    Recursive on purpose, and for the reason
    ``test_runpod_long_boot_needs_secure_pool.py`` records: U53's second wave
    found three configs under ``grids/`` that a non-recursive ``glob`` had
    hidden from its guard for a whole release, and they were the ones closest
    to the limit. Non-kinoforge YAMLs under the same tree (grid specs, batch
    manifests — different schemas) raise on load and are skipped, as are the
    rare configs that set no ``compute.lifecycle`` at all (nothing to check).
    """
    found: list[Path] = []
    for path in sorted(_CONFIG_DIR.rglob("*.yaml")):
        try:
            cfg: Config = load_config(path)
        except Exception:  # noqa: BLE001 — a grid spec need not be a full cfg
            continue
        compute = cfg.compute
        if compute is None or compute.provider != "runpod":
            continue
        if compute.lifecycle is None:
            continue
        found.append(path)
    return found


def _cap_and_boot(cfg_path: Path) -> tuple[float, float]:
    """Return ``(selfterm_cap_s, boot_timeout_s)`` for one RunPod config.

    Durations come from the validated :class:`Config` model, which has already
    parsed ``"20m"``-style strings into seconds — hand-parsing the YAML text
    would re-implement (and could disagree with) ``parse_duration``.

    Args:
        cfg_path: Path to a RunPod config declaring ``compute.lifecycle``.

    Returns:
        The enforced self-terminator lifetime and the declared boot timeout,
        both in seconds.
    """
    compute = load_config(cfg_path).compute
    assert compute is not None  # noqa: S101 — _runpod_configs filtered already
    lc = compute.lifecycle
    assert lc is not None  # noqa: S101 — _runpod_configs filtered already
    cap = min(2.0 * lc.idle_timeout, lc.max_lifetime - lc.time_buffer)
    return cap, lc.boot_timeout


_CONFIGS = _runpod_configs()


def test_the_discovery_actually_finds_runpod_configs() -> None:
    """Guard the guard: an empty or thin sweep makes every case below vacuous.

    Bug caught: a discovery helper that silently matches (almost) nothing — a
    renamed key, a moved directory, a ``load_config`` regression that makes
    every file unloadable — leaving a green test that checks no configs at all.
    That is exactly how U53's ``grids/`` configs escaped their guard.
    """
    assert len(_CONFIGS) >= 10, f"only found {len(_CONFIGS)} RunPod configs"


def test_every_legacy_inverted_entry_still_exists_and_still_offends() -> None:
    """Guard the guard: the allowlist may only shrink, never go stale.

    Bug caught: a config that gets FIXED (or renamed, or deleted) while its
    name stays in ``LEGACY_INVERTED``. The exemption would then silently cover
    nothing — and, worse, a future regression that re-inverted that same file
    would be waved through by a name still sitting in the allowlist. Requiring
    each entry to exist AND still offend forces the allowlist to track reality:
    fix a config and this test fails until its name comes out.
    """
    discovered = {p.name: p for p in _CONFIGS}
    missing = sorted(LEGACY_INVERTED - discovered.keys())
    assert not missing, (
        f"LEGACY_INVERTED names configs the sweep no longer finds: {missing}. "
        "Renamed or deleted? Remove them from the allowlist."
    )
    no_longer_offending = sorted(
        name
        for name in LEGACY_INVERTED
        if _cap_and_boot(discovered[name])[0] > _cap_and_boot(discovered[name])[1]
    )
    assert not no_longer_offending, (
        f"these configs no longer invert the cap: {no_longer_offending}. "
        "Delete them from LEGACY_INVERTED — the allowlist only shrinks."
    )


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=lambda p: p.stem)
def test_runpod_selfterm_cap_outlasts_the_declared_boot(cfg_path: Path) -> None:
    """The armed self-terminator must not fire before the boot it permits.

    Bug caught: the inversion the `kinoforge text` configs shipped with — a
    ``30m`` ``boot_timeout`` under a ``min(2*10m, 1h-2m) = 20m`` cap. Both caps
    are boot-relative, so the pod self-destructs 10 minutes before the boot it
    was allowed to take can finish. Every run on such a config pays for the
    full weight fetch and gets nothing, and the symptom (a pod that vanishes
    mid-boot) is indistinguishable by eye from RunPod's community-pool
    reclamation — the wrong diagnosis the repo has already made once.
    """
    if cfg_path.name in LEGACY_INVERTED:
        pytest.skip(f"{cfg_path.name} is a pre-existing offender (LEGACY_INVERTED)")
    cap, boot = _cap_and_boot(cfg_path)
    assert cap > boot, (
        f"{cfg_path.name}: the self-terminator cap min(2*idle_timeout, "
        f"max_lifetime-time_buffer)={cap:.0f}s does not clear "
        f"boot_timeout={boot:.0f}s, so the pod is armed to kill itself before "
        "it finishes booting — raise idle_timeout/max_lifetime or lower "
        "boot_timeout"
    )
