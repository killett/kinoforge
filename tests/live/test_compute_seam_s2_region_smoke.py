"""S2 live smoke: a region written in YAML pins the real launch.

This is the one claim S1's smoke structurally could not make. That smoke
constructed ``SkyPilotProvider(clouds=["aws"], region="us-west-2")`` itself,
so all it could prove was that a pin passed to the constructor reaches
``resources.region`` — the constructor knob was never the part in doubt.
Verification finding F6 was that NO config path reached it, which is exactly
what a smoke holding the pin in its own hand cannot test.

So the difference here is one line and the whole point of S2: the provider is
built by :func:`kinoforge._adapters.build_provider_for`, the real composition
root, with NO region and NO cloud passed by this file. Every pin under test
comes from ``examples/configs/skypilot-cpu.yaml``:

    compute.placement.region: us-west-2
    compute.backend_options.skypilot.clouds: ["aws"]

Three claims, in order, each blind to the others:

  1. **cfg-region** — ``build_provider_for`` alone puts ``us-west-2`` on
     ``provider._region``. Cheap, offline-shaped, and it fails before a cent
     is spent if the composition root stops reading the field.
  2. **yaml-pinned** — the launch payload SkyPilot really builds carries
     ``resources.region == "us-west-2"``, captured at the live
     ``sky.Task.from_yaml_config`` seam. An attribute that never reaches the
     wire would satisfy claim 1 and fail here.
  3. **realized-az** — EC2 reports the instance's availability zone inside
     ``us-west-2``. This is the only claim that survives a lie anywhere in
     the stack: it asks the cloud where the box actually is. An empty EC2
     answer FAILS rather than passing vacuously — "we never saw the box" is
     not evidence that it landed correctly.

Server readiness is NOT asserted, for the same reason S1 does not assert it:
this config's engine block is a ComfyUI placeholder whose setup script
downloads a Wan shard with a synthetic ``HF_TOKEN`` on a 2-vCPU box. That
cannot succeed and is not what is being paid for. The smoke stops the moment
the cluster is UP and the AZ has been read.

Teardown is S1's convergent ``_teardown``, IMPORTED rather than reimplemented
(the plan's stated preference): it carries the hard-won lesson that a
one-shot check can read clean while the SkyPilot API server — a separate
process that outlives pytest — is still about to create the instance.

Credential safety: the spec comes from
``tools.snapshot_launch_payloads.build_spec``, which resolves every
credential-shaped env var to the repo-wide synthetic stub, so no real
credential reaches the payload, a log line, or the evidence file.

Cost ceiling: < $0.05. c6i.large is ~$0.085/hr and the smoke abandons the
launch as soon as the AZ is known; ``lifecycle.max_lifetime: 30m`` from the
config arms the instance-side watchdog even if this process is killed.

Gated on KINOFORGE_LIVE_TESTS=1, AWS credentials, the ``aws`` CLI, and
``import sky`` (``pixi run -e live-skypilot``).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.live

_REASONS: list[str] = []
if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    _REASONS.append("KINOFORGE_LIVE_TESTS=1 required")

try:
    import boto3
except ImportError:
    boto3 = None

if boto3 is None:
    _REASONS.append("boto3 not installed")
elif boto3.Session().get_credentials() is None:
    _REASONS.append(
        "AWS credentials not resolved by boto3 "
        "(see AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE)"
    )

if shutil.which("aws") is None:
    _REASONS.append("aws CLI binary not found on PATH")

try:
    import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: F401
except ImportError:
    _REASONS.append("skypilot not installed (use `pixi run -e live-skypilot`)")

if _REASONS:
    pytest.skip(
        "S2 region smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge._adapters import build_provider_for  # noqa: E402
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

# S1's smoke owns the teardown convergence, the EC2 oracle and the util probe.
# Imported, not copied: a second teardown implementation is a second place for
# the 2026-08-27 "clean read while the launch is still in flight" bug to come
# back. Importing it also means this module inherits S1's identical skip gate,
# which has already run above.
from tests.live.test_compute_seam_s1_smoke import (  # noqa: E402
    _AZ_QUERY,
    _TYPE_QUERY,
    _aws_ec2_query,
    _capture_setup_log,
    _InputRecordingSky,
    _is_stalled,
    _now_local,
    _probe_util,
    _sky_status_of,
    _teardown,
)
from tools.snapshot_launch_payloads import build_spec  # noqa: E402

_log = logging.getLogger(__name__)

#: What the CONFIG pins. Duplicated here as the expectation rather than read
#: from the cfg: reading it back out of the same file the launch used would
#: make the assertion tautological — it would pass for any region as long as
#: the two agreed.
_EXPECTED_REGION = "us-west-2"
_EXPECTED_CLOUD = "aws"
_EXPECTED_SKU = "c6i.large"

_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_EVIDENCE_PATH = Path("tests/live/_s2_smoke_evidence.json")
_STATE_DIR = Path(".kinoforge")

#: Project rule: poll utilisation on a 60-90 s cadence, never elapsed spend.
_UTIL_POLL_INTERVAL_S = 75.0
_CAPTURE_TIMEOUT_S = 120.0
_CREATE_TIMEOUT_S = 1500.0
_STALL_CONSECUTIVE_PROBES = 3
_READY_STATUSES = {"ready", "running", "UP"}

#: c6i.large on-demand in us-west-2, for the spend line in the evidence. The
#: figure is a published list price, not a measurement, so the evidence names
#: it as an estimate rather than implying the invoice was read.
_SKU_USD_PER_HR = 0.085

#: The runner exports `pixi run preflight`'s exit code here. The test cannot
#: invoke preflight itself: preflight's clean-tree check would fail against
#: the very evidence file this test is about to rewrite. Absent is recorded as
#: "not-recorded", never silently as a pass.
_PREFLIGHT_RC_ENV = "KINOFORGE_S2_PREFLIGHT_RC"
_PREFLIGHT_LOG_ENV = "KINOFORGE_S2_PREFLIGHT_LOG"


def _preflight_record() -> dict[str, Any]:
    """Return what the runner reported about ``pixi run preflight``.

    Returns:
        ``{"exit_code": int | "not-recorded", "log": str | None}``.
    """
    raw = os.getenv(_PREFLIGHT_RC_ENV)
    record: dict[str, Any] = {"exit_code": "not-recorded", "log": None}
    if raw is not None and raw.strip().lstrip("-").isdigit():
        record["exit_code"] = int(raw.strip())
    log = os.getenv(_PREFLIGHT_LOG_ENV)
    if log:
        record["log"] = log[-2000:]
    return record


def test_s2_region_pinned_in_yaml_reaches_the_cloud() -> None:
    """A region written ONLY in the config lands the instance in that region.

    Bug caught: `placement.region` validates, is declared CONSUMED, and stops
    somewhere between the composition root and EC2 — an operator's pin that
    looks honoured everywhere except where the box actually boots. The three
    claims in the module docstring are checked in cost order, cheapest first,
    so a break in the composition root fails before any spend.
    """
    cluster_name = f"kinoforge-s2-smoke-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S2 — a region pinned in YAML reaches the cloud",
        "task": "task-7 (S2 region + compute surface)",
        "config": str(_CONFIG_PATH),
        "cluster_name": cluster_name,
        "difference_from_s1": (
            "the provider is built by kinoforge._adapters.build_provider_for; "
            "this file passes NO region and NO cloud. S1's smoke pinned both "
            "on the constructor, so it could only prove a constructor pin "
            "reaches resources — not that any config path does."
        ),
        "expected_region": _EXPECTED_REGION,
        "expected_cloud": _EXPECTED_CLOUD,
        "expected_sku": _EXPECTED_SKU,
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        "utilisation_samples": [],
        "cfg_region": {"status": "not-reached"},
        "yaml_pinned": {"status": "not-reached"},
        "realized_az": {"status": "not-reached"},
        "cluster_state": {"observed_ready": False, "statuses_seen": []},
    }

    # --- Claim 1: cfg-region — the composition root reads the field --------
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    evidence["cfg_region"] = {
        "status": "read",
        "provider_region": provider._region,
        "provider_clouds": provider._clouds,
        "at": _now_local(),
    }
    assert provider._region == _EXPECTED_REGION, (
        f"build_provider_for left _region={provider._region!r}; the config "
        f"pins {_EXPECTED_REGION!r} and nothing here passes a region, so the "
        f"composition root is not reading compute.placement.region"
    )
    assert provider._clouds == [_EXPECTED_CLOUD], (
        f"build_provider_for left _clouds={provider._clouds!r}; a region "
        f"without its cloud is the ambiguity S2's config comment warns about"
    )

    # The recording proxy observes the real sky seam. It is attached AFTER the
    # composition root has finished, and records only — it pins nothing, so
    # claims 2 and 3 still rest entirely on what the config supplied.
    recording_sky = _InputRecordingSky(sky)
    provider._sky_client = recording_sky
    provider.set_launch_ledger(Ledger(store=LocalArtifactStore(_STATE_DIR)))

    spec = dataclasses.replace(build_spec(cfg), run_id=cluster_name)

    create_result: dict[str, Any] = {}
    create_exc: list[BaseException] = []

    def _do_create() -> None:
        try:
            create_result["instance"] = provider.create_instance(spec)
        except BaseException as exc:  # noqa: BLE001 — surfaced on the main thread
            create_exc.append(exc)

    create_thread = threading.Thread(target=_do_create, daemon=True)
    launched_at = time.time()
    try:
        _log.info("launching %s with cfg-supplied pins only", cluster_name)
        evidence["launched_at"] = _now_local()
        create_thread.start()

        # --- Claim 2: yaml-pinned — the wire carries the cfg's region ------
        capture_deadline = time.time() + _CAPTURE_TIMEOUT_S
        while (
            "task_config" not in recording_sky.captured
            and time.time() < capture_deadline
            and (create_thread.is_alive() or not create_exc)
        ):
            time.sleep(1.0)
        why = f" (create_instance raised: {create_exc[0]!r})" if create_exc else ""
        assert "task_config" in recording_sky.captured, (
            f"the sky seam was never reached within {_CAPTURE_TIMEOUT_S:.0f}s, "
            f"so there is no payload to check the region against{why}"
        )
        resources = dict(recording_sky.captured["task_config"].get("resources", {}))
        evidence["yaml_pinned"] = {
            "status": "captured",
            "resources": resources,
            "at": _now_local(),
        }
        assert resources.get("region") == _EXPECTED_REGION, (
            f"resources.region is {resources.get('region')!r}, not "
            f"{_EXPECTED_REGION!r} — the cfg's pin reached the provider "
            f"attribute but not the launch payload"
        )
        assert resources.get("cloud") == _EXPECTED_CLOUD, (
            f"resources.cloud is {resources.get('cloud')!r}, not {_EXPECTED_CLOUD!r}"
        )
        evidence["yaml_pinned"]["status"] = "match"
        _log.info("YAML PIN ON THE WIRE: %r", resources)

        # --- The cluster has to exist before EC2 can be asked where it is --
        consecutive_stalled = 0
        observed_ready = False
        create_deadline = time.time() + _CREATE_TIMEOUT_S
        while time.time() < create_deadline:
            time.sleep(_UTIL_POLL_INTERVAL_S)
            elapsed = time.time() - launched_at
            status = _sky_status_of(provider, cluster_name)
            sample = _probe_util(cluster_name)
            sample.update(
                {
                    "at": _now_local(),
                    "elapsed_s": round(elapsed, 1),
                    "sky_status": status,
                    "create_thread_alive": create_thread.is_alive(),
                }
            )
            evidence["utilisation_samples"].append(sample)
            evidence["cluster_state"]["statuses_seen"].append(status)
            print(
                f"[poll] t+{elapsed:.0f}s status={status} "
                f"cpu_idle={sample['cpu_idle_pct']} "
                f"mem={sample['mem_used_mb']}/{sample['mem_total_mb']}MB",
                flush=True,
            )

            if status in _READY_STATUSES:
                observed_ready = True
                evidence["cluster_state"]["ready_at"] = _now_local()
                evidence["cluster_state"]["ready_after_s"] = round(elapsed, 1)
                break

            if not create_thread.is_alive():
                status = _sky_status_of(provider, cluster_name)
                evidence["cluster_state"]["statuses_seen"].append(status)
                if status in _READY_STATUSES:
                    observed_ready = True
                    evidence["cluster_state"]["ready_at"] = _now_local()
                    evidence["cluster_state"]["ready_after_s"] = round(elapsed, 1)
                break

            consecutive_stalled = consecutive_stalled + 1 if _is_stalled(sample) else 0
            if consecutive_stalled >= _STALL_CONSECUTIVE_PROBES:
                evidence["stall"] = {
                    "detected_at": _now_local(),
                    "elapsed_s": round(elapsed, 1),
                    "consecutive_probes": consecutive_stalled,
                    "remote_log_tail": _capture_setup_log(cluster_name),
                }
                pytest.fail(
                    f"boot appears stalled: {consecutive_stalled} consecutive "
                    f"near-zero-CPU probes at t+{elapsed:.0f}s for {cluster_name!r}"
                )

        evidence["cluster_state"]["observed_ready"] = observed_ready
        if create_exc:
            # EXPECTED on this config (ComfyUI/Wan setup on a CPU box with a
            # synthetic token). Recorded, never fatal on its own.
            evidence["create_instance_exception"] = repr(create_exc[0])[:600]
        if create_result:
            evidence["create_instance_returned"] = str(create_result["instance"].status)

        if not observed_ready:
            evidence["cluster_state"]["remote_log_tail"] = _capture_setup_log(
                cluster_name
            )
            reason = f"create_instance raised {create_exc[0]!r}; " if create_exc else ""
            pytest.fail(
                f"{reason}cluster {cluster_name!r} never reached UP within "
                f"{_CREATE_TIMEOUT_S:.0f}s; statuses seen="
                f"{evidence['cluster_state']['statuses_seen']!r}"
            )

        # --- Claim 3: realized-az — ask the cloud where the box really is --
        azs = sorted({str(z) for z in _aws_ec2_query(cluster_name, _AZ_QUERY)})
        skus = sorted({str(t) for t in _aws_ec2_query(cluster_name, _TYPE_QUERY)})
        evidence["realized_az"] = {
            "status": "read",
            "availability_zones": azs,
            "sku_launched": skus,
            "at": _now_local(),
        }
        assert azs, (
            f"EC2 reported no availability zone for {cluster_name!r}; an empty "
            f"answer means the instance was never observed, which is not "
            f"evidence that the region pin worked"
        )
        assert all(z.startswith(_EXPECTED_REGION) for z in azs), (
            f"EC2 reports {azs!r} for {cluster_name!r}; the config pinned "
            f"{_EXPECTED_REGION!r} and the launch landed elsewhere"
        )
        evidence["realized_az"]["status"] = "match"
        assert skus == [_EXPECTED_SKU], (
            f"expected the launch to land on exactly [{_EXPECTED_SKU!r}] but "
            f"EC2 reports {skus!r} — the cost envelope assumes that SKU"
        )
        _log.info("SMOKE RESULT cluster=%s landed in %r on %r", cluster_name, azs, skus)
    finally:
        try:
            evidence["teardown"] = _teardown(
                provider, cluster_name, create_thread=create_thread
            )
        except Exception as exc:  # noqa: BLE001 — recorded, then re-raised below
            evidence["teardown"] = {"error": repr(exc)}
            evidence["outcome"] = "TEARDOWN-FAILED"
            evidence["finished_at"] = evidence["captured_at"] = _now_local()
            _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
            raise
        billable_s = time.time() - launched_at
        # PROVEN requires ALL THREE claims. A file can never read green off
        # the cheap offline one alone.
        evidence["outcome"] = (
            "PROVEN"
            if (
                evidence["cfg_region"].get("status") == "read"
                and evidence["yaml_pinned"].get("status") == "match"
                and evidence["realized_az"].get("status") == "match"
            )
            else "NOT-PROVEN"
        )
        evidence["finished_at"] = evidence["captured_at"] = _now_local()
        evidence["billable_wall_clock_s"] = round(billable_s, 1)
        evidence["estimated_spend_usd"] = round(
            billable_s / 3600.0 * _SKU_USD_PER_HR, 4
        )
        evidence["spend_basis"] = (
            f"list price {_SKU_USD_PER_HR} USD/hr for {_EXPECTED_SKU} in "
            f"{_EXPECTED_REGION}, times billable wall clock — an estimate, "
            f"not a reading of the invoice"
        )
        _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"[evidence] wrote {_EVIDENCE_PATH}", flush=True)
