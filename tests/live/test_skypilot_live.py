"""Opt-in live smoke: SkyPilot CPU lifecycle (Phase 31).

Validates the lazy `sky` SDK path of :class:`SkyPilotProvider` against
real GCP, captures SDK return shapes as JSON fixtures, and tears down
with a four-tier safety net.

Gated by three preconditions (module-level skip if any are missing):

- ``KINOFORGE_LIVE_TESTS=1`` — global live-test gate (project convention).
- ``GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json`` — GCP service account.
- ``import sky`` succeeds — requires ``pixi run -e live-skypilot ...``.

Cost: <= $0.05 per run (smallest GCP CPU SKU, ~30 min wall-clock max,
autostop=1).

Fixtures land in ``tests/providers/fixtures/skypilot/*.json`` (last-call-
wins per method). See
``docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md`` for
the design.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_REASONS: list[str] = []
if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    _REASONS.append("KINOFORGE_LIVE_TESTS=1 required")
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    _REASONS.append("GOOGLE_APPLICATION_CREDENTIALS must be set")
try:
    import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: F401
except ImportError:
    _REASONS.append(
        "skypilot[gcp] not installed in the active env "
        "(use `pixi run -e live-skypilot`)"
    )

if _REASONS:
    pytest.skip(
        "SkyPilot live smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
# ``sky`` is already bound from the try-block above; ``noqa: E402`` covers
# the kinoforge imports which must come after the module-level skip gate.
from kinoforge.core.interfaces import (  # noqa: E402
    HardwareRequirements,
    InstanceSpec,
    Lifecycle,
)
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from tests.live._skypilot_recorder import _RecordingProxy  # noqa: E402

_log = logging.getLogger(__name__)

FIXTURE_DIR = Path(__file__).parent.parent / "providers" / "fixtures" / "skypilot"
_POLL_INTERVAL_S: float = 5.0
_READY_TIMEOUT_S: float = 600.0  # 10 min
_DESTROY_TIMEOUT_S: float = 300.0  # 5 min

# CPU smoke: zero VRAM requirement; min_cuda kept permissive.
HW_REQS_CPU = HardwareRequirements(min_vram_gb=0, min_cuda="0.0")

# T4 smoke (Layer W+β): 16 GB VRAM, modern CUDA. min_vram_gb=8 is
# safely below the T4's 16 GB and excludes accelerators smaller than T4.
HW_REQS_T4 = HardwareRequirements(min_vram_gb=8, min_cuda="11.0")

_GPU_FIXTURE_DIR = FIXTURE_DIR / "gpu"
_GPU_READY_TIMEOUT_S: float = 900.0  # 15 min — GPU provision slower than CPU
_SSH_TIMEOUT_S: float = 60.0


def _poll_until_ready(
    provider: SkyPilotProvider,
    instance_id: str,
    timeout_s: float,
) -> None:
    """Poll ``provider.list_instances()`` until the cluster reports ready.

    Args:
        provider: The SkyPilot provider instance.
        instance_id: Cluster name to wait for.
        timeout_s: Maximum seconds to wait.

    Raises:
        TimeoutError: cluster did not reach a ready state within ``timeout_s``.
    """
    start = time.time()
    while time.time() - start < timeout_s:
        instances = provider.list_instances()
        for inst in instances:
            if inst.id == instance_id:
                elapsed = int(time.time() - start)
                _log.info("cluster status=%s elapsed=%ds", inst.status, elapsed)
                if inst.status in {"ready", "running", "UP"}:
                    return
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(
        f"cluster {instance_id!r} did not reach a ready state within {timeout_s}s"
    )


def _gcloud_path() -> str:
    """Return absolute path to the ``gcloud`` binary.

    Prefers ``shutil.which("gcloud")`` (PATH resolution); falls back to
    ``~/google-cloud-sdk/bin/gcloud`` (the kinoforge container's standard
    install location). The pixi live-skypilot env does not include gcloud
    on PATH, so the fallback is the load-bearing path in practice.

    Returns:
        Absolute path string suitable for passing as ``argv[0]`` to
        :func:`subprocess.run`.
    """
    resolved = shutil.which("gcloud")
    if resolved:
        return resolved
    fallback = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"
    return str(fallback)


def _ssh_capture_stdout(
    cluster_name: str,
    cmd: str,
    timeout_s: float = _SSH_TIMEOUT_S,
) -> str:
    """SSH to ``cluster_name`` (sky-managed ~/.ssh/config) and capture stdout.

    Args:
        cluster_name: SkyPilot cluster name (also the SSH host alias sky
            writes into ``~/.ssh/config`` after a successful launch).
        cmd: Shell command to run on the remote host.
        timeout_s: Wall-clock seconds before the SSH call is killed.

    Returns:
        Decoded stdout (utf-8, trailing newline preserved).

    Raises:
        subprocess.CalledProcessError: SSH exit code was non-zero.
        subprocess.TimeoutExpired: ``timeout_s`` elapsed before the
            command returned.
    """
    completed = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", cluster_name, cmd],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_s,
    )
    return completed.stdout


def _t4_smoke_spec(cluster_name: str, offer: Any) -> InstanceSpec:
    """Build the GPU-smoke ``InstanceSpec`` for a given T4 offer.

    Mirrors the CPU-smoke shape with GPU image + GPU autostop.

    Args:
        cluster_name: SkyPilot cluster name (also the SSH host alias).
        offer: A ``ComputeOffer`` with a T4 GPU.

    Returns:
        ``InstanceSpec`` ready to pass to ``provider.create_instance``.
    """
    lifecycle = Lifecycle(idle_timeout_s=180, max_lifetime_s=1800)
    return InstanceSpec(
        run_id=cluster_name,
        image="",  # empty → SkyPilot picks its default GPU VM image per cloud
        env={},
        tags={"layer": "layer-w-beta-smoke"},
        lifecycle=lifecycle,
        offer=offer,
        provision_script="",
        run_cmd=["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        spot=True,  # use preemptible — GCP GPUS_ALL_REGIONS=0; PREEMPTIBLE_T4=1
    )


def _teardown(provider: SkyPilotProvider, cluster_name: str) -> None:
    """Four-tier teardown.

    Each tier catches its own exception so the next tier always runs. The
    final tier re-raises if any GCP VMs labelled with the cluster name
    still exist after all three preceding tiers have attempted teardown.

    Tier 1: ``provider.destroy_instance`` (cooperates with the recording proxy).
    Tier 2: direct ``sky.down(cluster_name, purge=True)`` (bypass the provider).
    Tier 3: ``gcloud compute instances list/delete`` filtered by cluster label.
    Tier 4: re-raise ``RuntimeError`` if survivors remain.

    Args:
        provider: SkyPilot provider whose API can still be used.
        cluster_name: Cluster name for both provider and gcloud lookups.

    Raises:
        RuntimeError: If GCP VMs survive after all three teardown tiers.
    """
    try:
        _log.info("tearing down via provider.destroy_instance")
        provider.destroy_instance(cluster_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("provider.destroy_instance raised: %r", exc)

    try:
        _log.info("tearing down via direct sky.down")
        sky.down(cluster_name, purge=True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sky.down raised: %r", exc)

    try:
        _log.info("tearing down via gcloud nuclear")
        listing = subprocess.run(
            [
                _gcloud_path(),
                "compute",
                "instances",
                "list",
                "--filter",
                f"labels.skypilot-cluster={cluster_name}",
                "--format",
                "value(name,zone)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        for line in listing.stdout.strip().splitlines():
            if not line.strip():
                continue
            name, _, zone = line.partition("\t")
            subprocess.run(
                [
                    _gcloud_path(),
                    "compute",
                    "instances",
                    "delete",
                    name.strip(),
                    "--zone",
                    zone.strip(),
                    "--quiet",
                ],
                check=False,
                timeout=120,
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("gcloud nuclear teardown raised: %r", exc)

    survivors = subprocess.run(
        [
            _gcloud_path(),
            "compute",
            "instances",
            "list",
            "--filter",
            f"labels.skypilot-cluster={cluster_name}",
            "--format",
            "value(name)",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if survivors.stdout.strip():
        raise RuntimeError(
            f"CLEANUP FAILED — manual VM deletion required: {survivors.stdout.strip()}"
        )
    _log.info("teardown complete cluster=%s", cluster_name)


def test_skypilot_live_e2e_cpu_lifecycle_smoke() -> None:
    """End-to-end live smoke: find_offers → create → status → endpoints → down.

    Validates the lazy ``sky`` SDK path. Fixtures are written by the
    :class:`_RecordingProxy` wrapping every ``sky.*`` call; the test
    asserts the high-level provider contract (cluster reaches a ready
    state, list contains the cluster, endpoints formed, teardown succeeds).
    """
    cluster_name = f"kinoforge-skypilot-smoke-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(sky_client=_RecordingProxy(sky, FIXTURE_DIR))

    try:
        offers = provider.find_offers(HW_REQS_CPU)
        _log.info("find_offers returned %d offers", len(offers))
        assert offers, "expected at least one CPU offer from sky.gpu_list()"

        lifecycle = Lifecycle(idle_timeout_s=60, max_lifetime_s=1800)
        spec = InstanceSpec(
            run_id=cluster_name,
            image="debian:12-slim",
            env={},
            tags={"layer": "phase-31-smoke"},
            lifecycle=lifecycle,
            offer=offers[0],
            provision_script="",
            run_cmd=["sleep", "60"],
        )
        _log.info(
            "launching cluster=%s region=us-central1 cpus=1+ memory=2+ autostop=1",
            cluster_name,
        )
        inst = provider.create_instance(spec)

        _poll_until_ready(provider, inst.id, timeout_s=_READY_TIMEOUT_S)

        ep = provider.endpoints(inst)
        assert ep["ssh"].startswith("ssh://"), f"bad endpoint: {ep!r}"

        listed = provider.list_instances()
        assert any(i.id == inst.id for i in listed), (
            f"cluster {inst.id!r} missing from list_instances()"
        )
    finally:
        _teardown(provider, cluster_name)


@pytest.mark.parametrize("cloud", ["gcp"])
def test_skypilot_live_e2e_t4_gpu_lifecycle_smoke(cloud: str) -> None:
    """End-to-end live smoke: T4 GPU lifecycle via ``providers/skypilot``.

    Layer W+β. Cheapest GPU test that exercises the adapter end-to-end:
    ``find_offers`` (T4 surfaced) → ``create_instance`` (accelerators=T4:1)
    → poll until ready → SSH ``nvidia-smi`` → assert T4 in stdout →
    4-tier teardown.

    Parametrize prepared for AWS at Layer W+β2; today only ``gcp`` is in
    the parameter list because the AWS GPU vCPU quota request
    (``L-DB2E81BA``, AWS case
    ``cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR``) is still pending.

    Cost ceiling: $0.10 GCP per run. Typical: $0.03–$0.06 on
    ``n1-standard-4 + nvidia-tesla-t4`` for ~5–10 min wall-clock.
    """
    cluster_name = f"kinoforge-w-beta-t4-{cloud}-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(
        sky_client=_RecordingProxy(sky, _GPU_FIXTURE_DIR),
        clouds=["gcp"],
    )

    try:
        offers = provider.find_offers(HW_REQS_T4)
        _log.info("find_offers returned %d offers", len(offers))
        t4_offers = [o for o in offers if "T4" in (getattr(o, "gpu_type", "") or "")]
        if not t4_offers:
            pytest.skip(
                f"no T4 offer surfaced for cloud={cloud!r}; "
                "check sky.list_accelerators() / region quota"
            )
        offer = t4_offers[0]
        _log.info("picked T4 offer: %r", offer)

        spec = _t4_smoke_spec(cluster_name, offer)
        _log.info("launching cluster=%s accelerators=T4:1 autostop=3min", cluster_name)
        inst = provider.create_instance(spec)

        _poll_until_ready(provider, inst.id, timeout_s=_GPU_READY_TIMEOUT_S)

        stdout = _ssh_capture_stdout(
            cluster_name,
            "nvidia-smi --query-gpu=name --format=csv,noheader",
        )
        _log.info("nvidia-smi stdout: %r", stdout)
        assert "T4" in stdout, f"expected T4 in nvidia-smi output, got: {stdout!r}"
    finally:
        _teardown(provider, cluster_name)
