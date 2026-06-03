"""Opt-in live smoke: RunPod pod lifecycle (Layer N Task 4, in-process rewrite).

Validates the 8 Layer-N production fixes against the real RunPod GraphQL API.
Runs entirely in-process (no subprocess/CLI invocation).

Gated by three env vars:
- ``KINOFORGE_LIVE_TESTS=1`` (global on/off)
- ``RUNPOD_API_KEY=<real key>``
- ``RUNPOD_TERMINATE_KEY=<scoped terminate-only key>``

Optional:
- ``KINOFORGE_SAVE_FIXTURES=1`` — additionally write captured responses to
  ``tests/providers/fixtures/runpod/*.json``.  Pair with a clean staging
  area; the diff is the AC4 review surface.

Cost: ~$0.10-$0.30 per run (pod lifecycle only, no generation).  Skipped
silently in CI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and os.getenv("RUNPOD_API_KEY")
    and os.getenv("RUNPOD_TERMINATE_KEY")
):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + RUNPOD_API_KEY "
        "+ RUNPOD_TERMINATE_KEY",
        allow_module_level=True,
    )

_log = logging.getLogger(__name__)

_POLL_INTERVAL_S: int = 5
_READY_TIMEOUT_S: int = 300  # 5 minutes


def test_runpod_live_e2e_pod_lifecycle_smoke() -> None:
    """End-to-end live smoke: find_offers → create → poll ready → list → destroy.

    Validates the 8 Layer-N production fixes against the real RunPod GraphQL
    API.  Runs entirely in-process — fixtures are captured by the recording
    seam because all HTTP calls go through the provider instance directly.
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import (
        HardwareRequirements,
        InstanceSpec,
        Lifecycle,
    )
    from kinoforge.providers.runpod import (
        RunPodProvider,
        _make_default_http_seams,
    )
    from tests.providers.conftest_runpod import _RUNPOD_DISPATCH, _RecordingHTTPSeam

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    authed_post, authed_get = _make_default_http_seams(api_key)

    fixtures_dir = Path("tests/providers/fixtures/runpod")
    capture = os.getenv("KINOFORGE_SAVE_FIXTURES") == "1"

    seam: _RecordingHTTPSeam | None
    if capture:
        _seam = _RecordingHTTPSeam(
            authed_post, authed_get, fixtures_dir, dispatch=_RUNPOD_DISPATCH
        )
        seam = _seam
        provider = RunPodProvider(
            creds=creds,
            http_post=_seam.http_post,
            http_get=_seam.http_get,
        )
    else:
        seam = None
        provider = RunPodProvider(creds=creds)

    pod_id: str | None = None
    start_time = time.monotonic()

    try:
        # ------------------------------------------------------------------
        # 1. find_offers
        # ------------------------------------------------------------------
        reqs = HardwareRequirements(
            min_vram_gb=24,
            max_usd_per_hr=0.50,
        )
        offers = provider.find_offers(reqs)
        assert offers, "find_offers returned no offers"
        for offer in offers:
            assert offer.cost_rate_usd_per_hr <= 0.50, (
                f"offer {offer.id!r} cost {offer.cost_rate_usd_per_hr:.4f} "
                f"exceeds cap 0.50"
            )

        # ------------------------------------------------------------------
        # 2. create_instance
        # ------------------------------------------------------------------
        spec = InstanceSpec(
            image="alpine:latest",
            offer=offers[0],
            lifecycle=Lifecycle(idle_timeout_s=600),
            tags={"mode": "pod"},
        )
        instance = provider.create_instance(spec)
        pod_id = instance.id
        assert pod_id, "create_instance returned empty id"
        _log.info("created pod %s (gpu=%s)", pod_id, offers[0].gpu_type)

        # ------------------------------------------------------------------
        # 3. poll until ready or 5-min timeout
        # ------------------------------------------------------------------
        elapsed = 0.0
        status = instance.status
        while status not in ("ready", "terminated", "stopped"):
            if elapsed >= _READY_TIMEOUT_S:
                break
            time.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
            instance = provider.get_instance(pod_id)
            status = instance.status
            _log.info("pod %s status=%s (%.0fs elapsed)", pod_id, status, elapsed)

        assert status == "ready", (
            f"pod {pod_id!r} did not reach 'ready' within "
            f"{_READY_TIMEOUT_S}s; final status={status!r}"
        )

        # ------------------------------------------------------------------
        # 4. list_instances — new pod must appear
        # ------------------------------------------------------------------
        all_instances = provider.list_instances()
        ids = [inst.id for inst in all_instances]
        assert pod_id in ids, f"pod {pod_id!r} not found in list_instances(); got {ids}"

        # ------------------------------------------------------------------
        # 5. destroy_instance (normal path)
        # ------------------------------------------------------------------
        provider.destroy_instance(pod_id)
        _log.info("pod %s destroyed (normal path)", pod_id)

    finally:
        # ------------------------------------------------------------------
        # Flush recording seam (success OR failure)
        # ------------------------------------------------------------------
        if seam is not None:
            try:
                seam.flush()
            except Exception as flush_exc:  # noqa: BLE001
                _log.warning("seam.flush() failed: %s", flush_exc)

        # ------------------------------------------------------------------
        # Last-resort destroy
        # ------------------------------------------------------------------
        if pod_id is not None:
            try:
                provider.destroy_instance(pod_id)
                _log.info("pod %s confirmed destroyed (finally path)", pod_id)
            except Exception as destroy_exc:  # noqa: BLE001
                import sys

                sys.stderr.write(
                    f"\n*** RUNPOD POD {pod_id} NOT CONFIRMED DESTROYED ***\n"
                    f"Error: {destroy_exc}\n"
                    f"Manually terminate via the RunPod console or run:\n"
                    f"  curl -X POST https://api.runpod.io/graphql \\\n"
                    f'    -H "Authorization: Bearer $RUNPOD_API_KEY" \\\n'
                    f'    -d \'{{"query":"mutation{{podTerminate('
                    f'input:{{podId:\\"{pod_id}\\"}})}}"}}\'\n'
                )
                raise

    # ------------------------------------------------------------------
    # 6. Write last_smoke.json
    # ------------------------------------------------------------------
    import subprocess

    elapsed_total = time.monotonic() - start_time
    try:
        git_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:  # noqa: BLE001
        git_sha = "unknown"

    smoke_meta = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_sha": git_sha,
        "pod_id": pod_id,
        "gpu_type": offers[0].gpu_type,
        "cost_rate_usd_per_hr": offers[0].cost_rate_usd_per_hr,
        "elapsed_seconds": round(elapsed_total, 1),
    }
    last_smoke_path = fixtures_dir / "last_smoke.json"
    last_smoke_path.write_text(
        json.dumps(smoke_meta, indent=2, sort_keys=False) + "\n",
    )
    _log.info("last_smoke.json written to %s", last_smoke_path)
