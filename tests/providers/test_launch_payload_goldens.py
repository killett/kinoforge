"""Behavior: the wire payload for every top-level example config is frozen.

Scope, stated precisely because it is easy to over-read: ``compute_configs()``
globs ``examples/configs/*.yaml`` NON-recursively, so the 31 goldens cover the
top-level configs that carry a ``compute:`` block. Configs under
``examples/configs/grids/`` and ``examples/configs/extras/`` are NOT covered —
a wire change for one of those does not trip this test.

This is the S1..S5 ratchet. The compute-seam rework changes the SHAPE of
``ComputeConfig`` and ``InstanceSpec``; it must not change what any provider
actually sends. A stage that silently drops ``cloudType``, reorders ``env``,
loses the provision script, or stops pinning ``resources.cloud`` fails here
rather than on an invoice.

Regenerate deliberately with ``pixi run python tools/snapshot_launch_payloads.py``
and review the diff — never regenerate to make this test pass.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from kinoforge.core.config import load_config
from tools.snapshot_launch_payloads import (
    EXCLUDED_CONFIGS,
    FROZEN_EPOCH,
    GOLDEN_DIR,
    GOLDEN_RUN_ID,
    capture_payload,
    compute_configs,
    golden_path_for,
    serialize,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_CONFIG_DIR = Path("examples/configs")
_CONFIGS = compute_configs()

_REGEN = (
    "run `pixi run python tools/snapshot_launch_payloads.py` and REVIEW the "
    "diff before committing — a regenerated golden is a reviewed act, not a "
    "way to make this test pass"
)


def _stems() -> set[str]:
    return {p.stem for p in _CONFIGS}


# ---------------------------------------------------------------------------
# The ratchet itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=lambda p: p.stem)
def test_launch_payload_matches_golden(cfg_path: Path) -> None:
    """Re-capturing a config reproduces its committed golden byte for byte.

    Bug caught: an S1..S5 refactor of ``ComputeConfig`` / ``InstanceSpec``
    drops or renames a field on the way to the provider — e.g. ``cloud_type``
    stops reaching RunPod's ``cloudType`` and every long pod silently lands on
    community hosts again (the 2026-07-03 three-pod deletion), or SkyPilot
    stops pinning ``resources.cloud`` and the optimizer relocates the launch
    to a different cloud.
    """
    golden_path = golden_path_for(cfg_path)
    assert golden_path.exists(), f"no golden for {cfg_path}; {_REGEN}"
    assert serialize(capture_payload(cfg_path)) == golden_path.read_text(), (
        f"launch payload for {cfg_path} changed; if that change is intended, {_REGEN}"
    )


def test_every_compute_config_has_a_golden() -> None:
    """Every example config with a ``compute:`` block is covered.

    Bug caught: a new provider config is added during S2..S5 and never enters
    the ratchet, so the stage that breaks it has nothing to fail against.
    """
    missing = sorted(p.name for p in _CONFIGS if not golden_path_for(p).exists())
    assert not missing, f"configs with no golden: {missing}; {_REGEN}"


def test_no_orphan_goldens() -> None:
    """No golden survives its config.

    Bug caught: a config is renamed or deleted and its golden lingers. The
    suite stays green while covering a file nobody launches any more, and the
    real successor config quietly has no coverage.
    """
    orphans = sorted({g.stem for g in GOLDEN_DIR.glob("*.json")} - _stems())
    assert not orphans, f"goldens with no config: {orphans}; {_REGEN}"


# ---------------------------------------------------------------------------
# Exclusions must stay visible and must expire
# ---------------------------------------------------------------------------


def test_excluded_configs_exist_and_still_fail_to_capture() -> None:
    """Each exclusion names a real file that genuinely cannot be captured.

    Bug caught: a config that broke under an S2..S5 change gets parked in
    ``EXCLUDED_CONFIGS`` to make the suite green. This test re-runs the
    capture for every excluded config: the moment one starts working, the
    exclusion must be deleted and the config brought into the ratchet.
    """
    for name, reason in sorted(EXCLUDED_CONFIGS.items()):
        path = _CONFIG_DIR / name
        assert path.exists(), f"EXCLUDED_CONFIGS names a missing config: {name}"
        assert reason.strip(), f"exclusion of {name} carries no reason"
        with pytest.raises(Exception, match=r".+"):
            capture_payload(path)


# ---------------------------------------------------------------------------
# A golden that captured {} would pass every test above and prove nothing
# ---------------------------------------------------------------------------


def _payloads_by_provider(provider: str) -> list[tuple[Path, dict[str, Any]]]:
    import json

    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        payload: dict[str, Any] = json.loads(path.read_text())
        if payload.get("provider") == provider:
            out.append((path, payload))
    return out


def _require(
    path: Path, payload: dict[str, Any], checks: dict[str, Callable[[Any], bool]]
) -> None:
    for pointer, predicate in checks.items():
        node: Any = payload
        for key in pointer.split("/"):
            assert isinstance(node, dict) and key in node, (
                f"{path.name}: golden has no {pointer!r} — the capture seam was "
                f"not reached, so this golden proves nothing"
            )
            node = node[key]
        assert predicate(node), (
            f"{path.name}: {pointer!r} is empty/implausible: {node!r}"
        )


def test_runpod_goldens_carry_a_real_create_pod_input() -> None:
    """Every RunPod golden holds a populated ``variables.input``.

    Bug caught: the ``http_post`` seam stops being reached (an early return in
    ``create_instance``, a provider that starts batching creates, a fake that
    swallows the call) and the goldens regenerate to ``{}``. Byte-identity
    would still pass on the empty tree while covering nothing.
    """
    goldens = _payloads_by_provider("runpod")
    assert len(goldens) >= 5, (
        f"expected the RunPod configs to be covered, got {goldens}"
    )
    for path, payload in goldens:
        _require(
            path,
            payload,
            {
                "input/imageName": lambda v: isinstance(v, str) and ":" in v,
                "input/cloudType": lambda v: v in {"ALL", "SECURE", "COMMUNITY"},
                "input/dockerArgs": lambda v: isinstance(v, str),
                "input/ports": lambda v: isinstance(v, str),
                "input/gpuCount": lambda v: v == 1,
                "input/name": lambda v: v == GOLDEN_RUN_ID,
                "input/env": lambda v: bool(
                    isinstance(v, list)
                    and v
                    and all(set(e) == {"key", "value"} and e["value"] for e in v)
                ),
            },
        )


def test_runpod_goldens_carry_the_provision_script_on_the_wire() -> None:
    """A RunPod pod golden ships its provision script inside ``env``.

    Bug caught: ``rendered.script`` stops reaching ``spec.provision_script``
    (the empty-string -> None coercion in ``build_instance_spec`` is the
    fragile seam). ``dockerArgs`` then collapses to ``""`` and the pod boots
    the bare image with no server — a failure that costs a full cold boot to
    discover live.
    """
    import base64
    import gzip

    longest = 0
    for path, payload in _payloads_by_provider("runpod"):
        env = {e["key"]: e["value"] for e in payload["input"]["env"]}
        assert "KINOFORGE_PROVISION_SCRIPT" in env, (
            f"{path.name}: no provision script on the wire"
        )
        assert "gzip -d" in payload["input"]["dockerArgs"], (
            f"{path.name}: dockerArgs does not decode the gzipped script"
        )
        # The wire value must survive exactly the decode dockerArgs performs.
        script = gzip.decompress(
            base64.b64decode(env["KINOFORGE_PROVISION_SCRIPT"])
        ).decode("utf-8")
        assert script.strip(), f"{path.name}: provision script decodes to nothing"
        longest = max(longest, len(script))
    # The heavy diffusers/comfyui bootstraps are tens of KB; a tree where every
    # script had collapsed to a stub would still pass the per-file checks above.
    assert longest > 20_000, (
        f"no RunPod golden carries a full engine bootstrap (longest {longest} chars)"
    )


def test_skypilot_goldens_carry_a_real_task_config() -> None:
    """Every SkyPilot golden holds a populated task config + launch kwargs.

    Bug caught: the resource block stops being built (``spec.offer`` lost on
    the way through a reshaped ``InstanceSpec``), so ``resources`` disappears
    and SkyPilot's optimizer picks whatever it likes — including a GPU nobody
    asked for and a disk size that trips the GCP SSD quota.
    """
    goldens = _payloads_by_provider("skypilot")
    assert len(goldens) >= 3, (
        f"expected the SkyPilot configs to be covered, got {goldens}"
    )
    for path, payload in goldens:
        _require(
            path,
            payload,
            {
                "task_config/name": lambda v: v == GOLDEN_RUN_ID,
                "task_config/resources": lambda v: (
                    isinstance(v, dict)
                    and "disk_size" in v
                    and ("accelerators" in v or "cpus" in v)
                ),
                "task_config/setup": lambda v: (
                    isinstance(v, str) and "kinoforge watchdog arm" in v
                ),
                "task_config/envs": lambda v: isinstance(v, dict),
                "launch_kwargs/cluster_name": lambda v: v == GOLDEN_RUN_ID,
                "launch_kwargs/down": lambda v: v is True,
                "launch_kwargs/idle_minutes_to_autostop": lambda v: isinstance(v, int),
            },
        )


def test_modal_goldens_carry_a_real_app_request() -> None:
    """Every Modal golden holds a populated ``ModalAppRequest``.

    Bug caught: the fast-boot split regresses — ``image_build_script`` stops
    being carried, so the heavy installs move back into container start and
    re-open the preemption window that killed the 2026-07-09 FlashVSR run.
    """
    goldens = _payloads_by_provider("modal")
    assert len(goldens) >= 3, f"expected the Modal configs to be covered, got {goldens}"
    for path, payload in goldens:
        _require(
            path,
            payload,
            {
                "request/run_id": lambda v: v == GOLDEN_RUN_ID,
                "request/image": lambda v: isinstance(v, str) and ":" in v,
                "request/gpu": lambda v: isinstance(v, str) and bool(v),
                "request/provision_script": lambda v: (
                    isinstance(v, str) and len(v) > 500
                ),
                "request/launch_line": lambda v: isinstance(v, str) and bool(v),
                "request/scaledown_window_s": lambda v: isinstance(v, int) and v > 0,
                "request/startup_timeout_s": lambda v: isinstance(v, int) and v > 0,
            },
        )


# ---------------------------------------------------------------------------
# Determinism — a golden that diffs on every run is worse than none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stem",
    ["runpod-diffusers-rife-60fps-interpolate", "skypilot-gpu"],
)
def test_capture_freezes_the_clock(stem: str) -> None:
    """Capturing twice under a *running* clock still yields identical bytes.

    The wall clock is patched here to jump an hour between reads, so only
    ``capture_payload``'s own freeze can keep the two captures equal.

    Bug caught: the freeze is dropped from the harness. RunPod's
    ``KINOFORGE_PROVISION_SCRIPT`` is ``base64(gzip(script))`` and gzip stamps
    ``time.time()`` into its header, while SkyPilot stamps the watchdog
    deadline into ``setup`` — both would then change on every regeneration,
    so every golden would diff for reasons unrelated to the compute seam and
    the ratchet would be abandoned as noise.
    """
    path = _CONFIG_DIR / f"{stem}.yaml"
    ticking = itertools.count(2_000_000_000.0, 3600.0)
    with mock.patch.object(time, "time", side_effect=lambda: next(ticking)):
        first = serialize(capture_payload(path))
        second = serialize(capture_payload(path))
    assert first == second


def test_frozen_epoch_actually_lands_in_the_payload() -> None:
    """SkyPilot's watchdog deadline is derived from :data:`FROZEN_EPOCH`.

    ``skypilot-gpu.yaml`` sets ``budget: 0.10`` and ``max_usd_per_hr: 1.00``,
    so the budget deadline is ``0.10 / 1.00 * 3600 = 360 s`` past launch —
    earlier than the 1800 s ``max_lifetime`` — giving ``1756000000 + 360``.

    compute-seam S4 changed the rate this is computed from. Pre-S4 it was the
    harness's synthetic offer price ($1.64/hr); the provider has no offer now
    and cannot know the realized rate at create time, so it uses the ceiling
    the operator set. That is the conservative direction: the cap is an upper
    bound on the billed rate, so budget/cap is a LOWER bound on the time the
    budget lasts, and the deadline lands early rather than late.

    Bug caught: the freeze silently stops applying to the watchdog (e.g. the
    provider starts reading a monotonic clock), so the deadline in the golden
    is a real wall-clock instant and the golden rots into a value nobody can
    reproduce.
    """
    cfg_path = _CONFIG_DIR / "skypilot-gpu.yaml"
    payload = capture_payload(cfg_path)
    setup: str = payload["task_config"]["setup"]
    cap = load_config(str(cfg_path)).placement().max_usd_per_hr
    assert f"'{FROZEN_EPOCH + 0.1 / cap * 3600.0!r}'" in setup, (
        "watchdog deadline is not the frozen-epoch-derived value"
    )


# ---------------------------------------------------------------------------
# Credential safety
# ---------------------------------------------------------------------------


def test_goldens_carry_no_credential_shapes() -> None:
    """No committed golden matches a blocking credential pattern.

    Bug caught: a future capture reads a real credential provider instead of
    the stub, and a live ``HF_TOKEN`` / ``RUNPOD_TERMINATE_KEY`` lands in a
    committed fixture. Belt to the pre-commit scanner's braces.
    """
    from kinoforge.core.credential_patterns import iter_findings

    offenders: list[str] = []
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        for finding in iter_findings(path.read_text()):
            offenders.append(f"{path.name}:{finding.line_no} {finding.pattern_name}")
    assert not offenders, f"credential shapes in goldens: {offenders}"
