"""Behavior: a RunPod config with a long boot must pin the SECURE pool.

Found live on 2026-09-25 while setting up U60's Wan 2.2 re-proof. The
`target:`-spelled config inherited its compute block from
`runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml`, which pins no
`cloud_type`. RunPod's community pool reclaims pods minutes after create, so
the pod was deleted before it finished pulling weights:

    [10:39:44] gpu=0.0% cpu=2.0% mem=0.0%
    [10:40:59] gpu=0.0% cpu=0.0% mem=0.0%   <<< CPU 0% and memory FLAT
    ... 13 probes, ~15 min, never moved ...
    ProvisionFailed: pod '178qwug7mlvifd' boot stalled

`bootstrap.log` on port 8001 was unreachable throughout — the container never
came up. `kinoforge list` then reconciled the row as "pod gone provider-side".
Cost ~$0.39, and nothing to do with the feature under test.

This is U42's mechanism, which was fixed once: that filing records "the cfg was
the only RunPod one not pinned to secure". It was not the only one. The fix
reached the 1.3B swap grid and left SEVENTEEN unpinned — found by this test, not
by eye: a hand audit of the obvious siblings surfaced three. They include every
Wan 2.2 A14B config, i.e. the LONGEST boots (a ~70 GB weight fetch, 25-30
minutes), where reclamation is both likeliest and most expensive. A one-off fix
to whichever config happened to be under the microscope is how a class defect
survives; this test makes it a property of the config set instead.

The threshold is the boot itself, not the GPU or the model: a pod that needs
more than ten minutes to become ready is one the community pool will not keep.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONFIG_DIR = Path(__file__).resolve().parents[1] / "examples" / "configs"

#: Above this, the community pool's reclamation window is a real risk. Ten
#: minutes is the figure the 2026-07-03 incident note settled on.
_LONG_BOOT_S = 600


def _boot_timeout_s(raw: dict[str, object]) -> float | None:
    """Return the cfg's declared boot timeout in seconds, if it sets one."""
    compute = raw.get("compute") or {}
    assert isinstance(compute, dict)
    lifecycle = compute.get("lifecycle") or {}
    assert isinstance(lifecycle, dict)
    value = lifecycle.get("boot_timeout")
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if text.endswith("m"):
        return float(text[:-1]) * 60.0
    if text.endswith("h"):
        return float(text[:-1]) * 3600.0
    if text.endswith("s"):
        return float(text[:-1])
    return float(text)


def _runpod_configs() -> list[Path]:
    """Every shipped RunPod config, recursively.

    Recursive on purpose: U53's second wave found three configs under
    ``grids/`` that a non-recursive ``glob`` had hidden from its guard for an
    entire release, and they were the ones closest to the limit.
    """
    found = []
    for path in sorted(_CONFIG_DIR.rglob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text())
        except Exception:  # noqa: BLE001 — a grid spec need not be a full cfg
            continue
        if not isinstance(raw, dict):
            continue
        if ((raw.get("compute") or {}).get("provider")) == "runpod":
            found.append(path)
    return found


_CONFIGS = _runpod_configs()


def test_the_discovery_actually_finds_runpod_configs() -> None:
    """Guard the guard: an empty sweep would make every case below vacuous.

    Bug caught: a discovery helper that silently matches nothing — a renamed
    key, a moved directory — leaving a green test that checks no files at all.
    That is precisely how U53's `grids/` configs escaped their guard.
    """
    assert len(_CONFIGS) >= 10, f"only found {len(_CONFIGS)} RunPod configs"


@pytest.mark.parametrize("cfg_path", _CONFIGS, ids=lambda p: p.stem)
def test_a_long_boot_runpod_config_pins_the_secure_pool(cfg_path: Path) -> None:
    """A pod that takes >10 min to boot must not be booked on the community pool.

    Bug caught: exactly the 2026-09-25 stall. The pod is reclaimed mid-fetch,
    the util probe reads 0%/0%/0% until the stall detector fires, and the
    operator pays for a boot that could never have completed — on the configs
    with the longest, most expensive boots.
    """
    raw = yaml.safe_load(cfg_path.read_text())
    boot = _boot_timeout_s(raw)
    if boot is None or boot <= _LONG_BOOT_S:
        pytest.skip(f"boot_timeout {boot}s is not a long boot")

    backend = ((raw.get("compute") or {}).get("backend_options")) or {}
    cloud_type = (backend.get("runpod") or {}).get("cloud_type")

    assert cloud_type == "secure", (
        f"{cfg_path.name} declares boot_timeout={boot:.0f}s but pins "
        f"cloud_type={cloud_type!r}. RunPod's community pool reclaims pods "
        f"minutes after create, so this boot cannot reliably finish — set "
        f"compute.backend_options.runpod.cloud_type: secure"
    )
