"""Live smoke: compute-seam S4 — the inverted selection still books the right box.

The claim: with ``find_offers`` off the ABC and ``InstanceSpec.offer`` deleted,
nobody hands SkyPilot a SKU any more — it reads ``placement`` and its optimizer
picks. This proves the picking still lands on the same box S2 and S3 measured:
``c6i.large`` in ``us-west-2a``, reached ready, priced under the config's cap.

The golden ratchet cannot make this claim. It asserts the PAYLOAD kinoforge
sends; what a cloud does with that payload is a different question, and the
whole point of a declarative placer is that the answer is not in the request.

Three claims, in cost order:

1. **offline** — ``build_spec`` produces a spec with no ``offer`` attribute at
   all, and the CPU placement it carries (``min_vram_gb == 0``, no named
   accelerator) selects NO accelerator. Free, checked before any spend.
2. **live** — the cluster reaches ready and EC2 reports instance type
   ``c6i.large`` in availability zone ``us-west-2a`` — the same SKU and AZ the
   two prior stages measured, so a change here is attributable to S4.
3. **rate** — ``realized_rate`` returns a real number for the live cluster,
   recorded beside the AWS published price, and it PASSES the config's cap
   (``max_usd_per_hr: 0.50``). Task 5 covered the violation path; this is the
   other arm, and the one every ordinary run takes.

Operational notes inherited from S1/S2/S3, and they are not optional:

* Teardown is S1's convergent ``_teardown``, IMPORTED rather than
  reimplemented, and verified AFTER the process exits via ``kinoforge list``
  and ``sky status``.
* Utilisation is polled on a 60-90 s cadence. Spend is never the health
  signal — it climbs identically whether the box is working or dead.
* Do not run other ``aws`` commands against this account while the smoke is in
  flight (S2 pushed its own AZ query past a 120 s ceiling that way).

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
        "S4 selection smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge._adapters import build_provider_for  # noqa: E402
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.interfaces import Instance  # noqa: E402
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

# S1's smoke owns the teardown convergence, the EC2 oracle and the util probe.
# Imported, not copied: a second teardown implementation is a second place for
# the 2026-08-27 "clean read while the launch is still in flight" bug to come
# back.
from tests.live.test_compute_seam_s1_smoke import (  # noqa: E402
    _AZ_QUERY,
    _TYPE_QUERY,
    Ec2QueryFailed,
    _aws_ec2_query,
    _capture_setup_log,
    _now_local,
    _probe_util,
    _sky_status_of,
    _teardown,
)
from tools.snapshot_launch_payloads import build_spec  # noqa: E402

_log = logging.getLogger(__name__)

_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_EVIDENCE_PATH = Path("tests/live/_s4_selection_evidence.json")
_STATE_DIR = Path(".kinoforge")

#: Project rule: poll utilisation on a 60-90 s cadence, never elapsed spend.
_UTIL_POLL_INTERVAL_S = 75.0
#: S2 reached UP at t+1474 s on this same config and SKU; the S4 rate-cap smoke
#: took 302 s. A ceiling, not an expected duration.
_READY_TIMEOUT_S = 2100.0
_READY_STATUSES = {"ready", "running", "UP"}

#: What S2 and S3 both measured. The point of the smoke is that S4's inversion
#: did not move either.
_EXPECTED_SKU = "c6i.large"
_EXPECTED_AZ = "us-west-2a"
#: c6i.large on-demand in us-west-2 — the independent cross-check for the
#: readback. A published list price, not a reading of the invoice.
_SKU_USD_PER_HR = 0.085
_RATE_AGREEMENT_TOLERANCE = 0.30

_PREFLIGHT_RC_ENV = "KINOFORGE_S4_PREFLIGHT_RC"
_PREFLIGHT_LOG_ENV = "KINOFORGE_S4_PREFLIGHT_LOG"


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


def _ec2_facts(cluster_name: str) -> dict[str, Any]:
    """Return the EC2-reported instance type and AZ, or a recorded error.

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        ``{"instance_types": [...], "azs": [...]}``, or ``{"error": ...}`` when
        the oracle itself could not be read. Never raises — an unreadable
        oracle must be recorded as unknown, never as agreement.
    """
    try:
        return {
            "instance_types": sorted(
                {str(v) for v in _aws_ec2_query(cluster_name, _TYPE_QUERY)}
            ),
            "azs": sorted({str(v) for v in _aws_ec2_query(cluster_name, _AZ_QUERY)}),
        }
    except Ec2QueryFailed as exc:
        return {"error": repr(exc)[:200]}


def test_s4_the_inverted_path_still_books_c6i_large() -> None:
    """Selection moved inside the provider and the same box comes back.

    Bug caught, and it would be invisible offline: S4 deletes the field a
    caller used to pin a SKU with, and moves the CPU-vs-GPU decision onto
    ``placement``. Get that decision wrong and this config asks for an
    accelerator — the goldens would still pass (they pin the payload, and the
    payload would be self-consistently wrong), while the launch quietly books a
    GPU box at ~20x the price.
    """
    cluster_name = f"kinoforge-s4-sel-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))
    cap = cfg.placement().max_usd_per_hr

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S4 — the inverted path still books the right box",
        "task": "task-11 (S4 Part B live proof)",
        "config": str(_CONFIG_PATH),
        "cluster_name": cluster_name,
        "engine": cfg.engine.kind,
        "claim": (
            "with find_offers off the ABC and InstanceSpec.offer deleted, "
            "nobody hands SkyPilot a SKU; it reads placement and its optimizer "
            "picks. This asserts the pick still lands on the SKU and AZ S2 and "
            "S3 both measured — which the golden ratchet cannot assert, "
            "because it pins the request rather than what the cloud does with "
            "it."
        ),
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        "utilisation_samples": [],
        "selection": {"status": "not-reached"},
        "booked": {"status": "not-reached"},
        "rate": {"status": "not-reached"},
        "cluster_state": {"observed_ready": False, "statuses_seen": []},
    }

    # --- Claim 1: offline — no offer is passed, and no accelerator selected ---
    spec = dataclasses.replace(build_spec(cfg), run_id=cluster_name)
    assert not hasattr(spec, "offer"), (
        "InstanceSpec still carries an offer; this smoke is about the path "
        "where nobody hands the provider a SKU"
    )
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    selected = provider._select_accelerator(spec.placement)  # noqa: SLF001
    evidence["selection"] = {
        "status": "computed",
        "accelerator": selected,
        "placement": {
            "min_vram_gb": spec.placement.min_vram_gb,
            "accelerators": list(spec.placement.accelerators),
            "region": spec.placement.region,
            "max_usd_per_hr": cap,
        },
        "at": _now_local(),
    }
    assert selected is None, (
        f"a CPU placement must select no accelerator; got {selected!r}. That "
        f"is the S4 branch this config exercises, and getting it wrong books a "
        f"GPU box for a CPU smoke."
    )

    provider.set_launch_ledger(Ledger(store=LocalArtifactStore(_STATE_DIR)))

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
        _log.info("launching %s through the inverted selection path", cluster_name)
        evidence["launched_at"] = _now_local()
        create_thread.start()

        observed_ready = False
        deadline = time.time() + _READY_TIMEOUT_S
        while time.time() < deadline:
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

        evidence["cluster_state"]["observed_ready"] = observed_ready
        if create_exc:
            # EXPECTED on this config: the comfyui setup fetches Wan weights
            # with a synthetic token onto a CPU box and fails partway. The
            # claims here are about WHICH BOX was booked, which does not depend
            # on the setup script succeeding.
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
                f"{_READY_TIMEOUT_S:.0f}s; there is no booked box to identify"
            )

        # --- Claim 2: live — EC2 says c6i.large in us-west-2a ---------------
        facts = _ec2_facts(cluster_name)
        evidence["booked"] = {"status": "read", **facts, "at": _now_local()}
        assert "error" not in facts, (
            f"the EC2 oracle could not be read ({facts.get('error')}), so what "
            f"was booked is unknown — which is not the same as correct"
        )
        assert facts["instance_types"] == [_EXPECTED_SKU], (
            f"expected the inverted path to still book {_EXPECTED_SKU!r}; EC2 "
            f"reports {facts['instance_types']!r}"
        )
        assert facts["azs"] == [_EXPECTED_AZ], (
            f"expected {_EXPECTED_AZ!r} (what S2 and S3 both measured); EC2 "
            f"reports {facts['azs']!r}"
        )
        evidence["booked"]["status"] = "matches-s2-and-s3"
        _log.info("BOOKED %s in %s", facts["instance_types"], facts["azs"])

        # --- Claim 3: rate — a real readback, under the cap ------------------
        instance: Instance = create_result.get("instance") or Instance(
            id=cluster_name,
            provider="skypilot",
            status="ready",
            created_at=launched_at,
        )
        realized = provider.realized_rate(instance)
        evidence["rate"] = {
            "status": "read",
            "realized_usd_per_hr": realized,
            "cap_usd_per_hr": cap,
            "published_usd_per_hr": _SKU_USD_PER_HR,
            "source": "AWS on-demand list price, us-west-2",
            "at": _now_local(),
        }
        assert realized is not None, (
            "realized_rate returned None for a live UP cluster; the ordinary "
            "path has to produce a real number, or the cap check that follows "
            "it in the orchestrator would tear down every healthy launch"
        )
        drift = abs(realized - _SKU_USD_PER_HR) / _SKU_USD_PER_HR
        evidence["rate"]["drift_vs_published"] = round(drift, 4)
        assert drift <= _RATE_AGREEMENT_TOLERANCE, (
            f"readback {realized} disagrees with the published "
            f"{_EXPECTED_SKU} price {_SKU_USD_PER_HR} by {drift:.0%}"
        )
        assert realized <= cap, (
            f"realized {realized} exceeds this config's own cap {cap}; that is "
            f"Task 5's path, not this one"
        )
        evidence["rate"]["status"] = "under-cap"
        _log.info("REALIZED %.4f USD/hr under cap %.4f", realized, cap)
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
        evidence["outcome"] = (
            "PROVEN"
            if (
                evidence["selection"].get("status") == "computed"
                and evidence["booked"].get("status") == "matches-s2-and-s3"
                and evidence["rate"].get("status") == "under-cap"
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
            f"us-west-2, times billable wall clock — an estimate, not a "
            f"reading of the invoice"
        )
        _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"[evidence] wrote {_EVIDENCE_PATH}", flush=True)
