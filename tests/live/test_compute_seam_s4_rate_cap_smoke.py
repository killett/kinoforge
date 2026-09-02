"""Live smoke: compute-seam S4 — a violated rate cap tears the instance down.

The claim, in one sentence: with ``placement.max_usd_per_hr`` set BELOW what the
cloud actually bills, kinoforge reads the rate off the launched instance,
destroys it, and raises — instead of proceeding at the higher rate while every
surface reports the number it asked for.

That last clause is finding F4, and it is why an offline test cannot close this.
The offline suite proves the branch logic against a fake provider; only a live
cluster proves that ``SkyPilotProvider.realized_rate`` finds a REAL number on a
REAL handle. A ``None`` here would exercise only the unreadable branch, which is
not the claim, so the test refuses to pass on one.

Three claims, in cost order:

1. **offline** — the cap is read out of the config object the orchestrator reads
   it from (``cfg.placement().max_usd_per_hr``), pinned below ``c6i.large``'s
   real rate. Checked before any spend.
2. **readback** — ``realized_rate`` returns a real float for the live cluster,
   cross-checked against the AWS published list price for the instance type EC2
   reports. Two independent sources, both recorded.
3. **enforcement** — ``_enforce_rate_cap`` (the orchestrator's own function, not
   a re-implementation of it) destroys the cluster and raises
   ``RateCapExceeded`` carrying that real number.

This smoke covers the POST-LAUNCH arm on purpose. compute-seam S5 Task 8 added
a PRE-launch refusal to ``SkyPilotProvider.create_instance``: it bounds the
launch from sky's accelerator catalog and raises ``PreLaunchRateCapExceeded``
before ``sky.launch`` when that bound already exceeds the cap. A $0.01 cap
trips it, so the test forces the estimate unreadable
(``_estimate_hourly_rate -> None``, the documented WARN-and-proceed path) to
keep reaching the readback. The pre-launch arm has its own live smoke; between
them both arms are covered, and neither test is weakened to accommodate the
other. Nothing about the readback, the enforcement or the teardown is stubbed
here.

Why the enforcement function is driven directly rather than through
``kinoforge generate``: the full path would additionally run ``engine.provision``
against a comfyui setup that is known to fail on this CPU box (it fetches Wan
weights with a synthetic token — see the S3 evidence), so the run would die of
an unrelated cause before ever reaching the check. ``_enforce_rate_cap`` IS the
enforcement path; nothing about it is stubbed here, and the provider, the
cluster, the handle and the teardown are all real.

Operational notes inherited from S1/S2/S3, and they are not optional:

* Teardown is S1's convergent ``_teardown``, IMPORTED rather than
  reimplemented, and verified AFTER the process exits via ``kinoforge list``
  and ``sky status``. The enforcement path's own ``destroy_instance`` runs
  first; this is the belt to its braces.
* Utilisation is polled on a 60-90 s cadence. Spend is never the health
  signal — it climbs identically whether the box is working or dead.
* Do not run other ``aws`` commands against this account while the smoke is
  in flight (S2 pushed its own AZ query past a 120 s ceiling that way).

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
        "S4 rate-cap smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge._adapters import build_provider_for  # noqa: E402
from kinoforge.core.capabilities import Capability  # noqa: E402
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.errors import RateCapExceeded  # noqa: E402
from kinoforge.core.interfaces import Instance  # noqa: E402
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.core.orchestrator import (  # noqa: E402
    _enforce_rate_cap,
    _record_provisional_row,
)
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

# S1's smoke owns the teardown convergence, the EC2 oracle and the util probe.
# Imported, not copied: a second teardown implementation is a second place for
# the 2026-08-27 "clean read while the launch is still in flight" bug to come
# back.
from tests.live.test_compute_seam_s1_smoke import (  # noqa: E402
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
_EVIDENCE_PATH = Path("tests/live/_s4_rate_cap_evidence.json")
_STATE_DIR = Path(".kinoforge")

#: Project rule: poll utilisation on a 60-90 s cadence, never elapsed spend.
_UTIL_POLL_INTERVAL_S = 75.0
#: S2 reached UP at t+1474 s on this same config and SKU. A ceiling, not an
#: expected duration.
_READY_TIMEOUT_S = 2100.0
_READY_STATUSES = {"ready", "running", "UP"}

#: c6i.large on-demand in us-west-2 — the independent cross-check for the
#: readback. A published list price, not a reading of the invoice.
_SKU_USD_PER_HR = 0.085
_EXPECTED_SKU = "c6i.large"
#: How far the readback may sit from the published price before the
#: cross-check is treated as disagreement rather than rounding. SkyPilot's
#: catalog and the AWS price list are separately maintained snapshots.
_RATE_AGREEMENT_TOLERANCE = 0.30

#: BELOW the real rate, on purpose: this smoke exists to violate the cap.
_CAP_USD_PER_HR = 0.01

#: The runner exports `pixi run preflight`'s exit code here. The test cannot
#: invoke preflight itself: preflight's clean-tree check would fail against the
#: very evidence file this test is about to rewrite. Absent is recorded as
#: "not-recorded", never silently as a pass.
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


def _ec2_type_or_error(cluster_name: str) -> str:
    """Return the EC2-reported instance type, or a recorded error string.

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        The instance type EC2 reports, ``"<none>"`` when the query succeeded
        and found nothing, or ``"<unreadable: ...>"`` when the oracle itself
        could not be read. Never raises — a missing cross-check must not mask
        the rate-cap result it is there to corroborate.
    """
    try:
        types = sorted({str(t) for t in _aws_ec2_query(cluster_name, _TYPE_QUERY)})
    except Ec2QueryFailed as exc:
        return f"<unreadable: {repr(exc)[:120]}>"
    return types[0] if types else "<none>"


def test_s4_a_violated_rate_cap_destroys_the_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap below the billed rate tears the cluster down and raises.

    Bug caught, and it is F4 itself: before S4 the cap was a filter over a
    catalog SkyPilot's optimizer never consulted, so a cluster billing above it
    ran to completion while the ledger, est_spend and `kinoforge list` all
    reported the asked-for number. A filter cannot see the optimizer's choice;
    only a readback can, and a readback is worthless unless something acts on
    it.
    """
    cluster_name = f"kinoforge-s4-cap-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S4 — a violated rate cap tears the instance down",
        "task": "task-5 (S4 Part A live proof)",
        "config": str(_CONFIG_PATH),
        "cluster_name": cluster_name,
        "engine": cfg.engine.kind,
        "why_this_shape": (
            "_enforce_rate_cap is driven directly against a REAL provider and "
            "a REAL launched cluster. The full generate path would first run "
            "engine.provision against a comfyui setup known to fail on this "
            "CPU box (it fetches Wan weights with a synthetic token), so the "
            "run would die of an unrelated cause before reaching the check. "
            "Nothing about the enforcement path itself is stubbed."
        ),
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        "utilisation_samples": [],
        "cap": {"status": "not-reached"},
        "readback": {"status": "not-reached"},
        "enforcement": {"status": "not-reached"},
        "cluster_state": {"observed_ready": False, "statuses_seen": []},
    }

    # --- Claim 1: offline — the cap comes from the config, and is below cost --
    assert cfg.compute is not None
    cfg.compute.placement.max_usd_per_hr = _CAP_USD_PER_HR
    cap = cfg.placement().max_usd_per_hr
    assert cap == pytest.approx(_CAP_USD_PER_HR), (
        f"the cap override did not reach cfg.placement(): got {cap!r}. The "
        f"orchestrator reads it from there, so a smoke that passed a literal "
        f"instead would prove nothing about the plumbing."
    )
    assert cap < _SKU_USD_PER_HR, (
        f"cap {cap} is not below {_EXPECTED_SKU}'s published rate "
        f"{_SKU_USD_PER_HR}; this smoke would then prove nothing"
    )
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)
    assert Capability.RATE_READBACK in provider.capabilities(), (
        "skypilot must declare RATE_READBACK for the unreadable-rate branch "
        "to be a teardown rather than a shrug"
    )
    evidence["cap"] = {
        "status": "from-config",
        "max_usd_per_hr": cap,
        "published_sku_rate_usd_per_hr": _SKU_USD_PER_HR,
        "at": _now_local(),
    }

    # compute-seam S5 Task 8 added a PRE-launch refusal: create_instance now
    # bounds the launch from sky's catalog and raises before sky.launch when
    # the bound already exceeds the cap. A $0.01 cap trips it, which would end
    # this smoke at create_instance and leave the POST-launch readback — the
    # only thing this test exists to prove — unexercised. Forcing the estimate
    # unreadable takes the documented WARN-and-proceed path, which is exactly
    # the pre-Task-8 behaviour this smoke was written against. Nothing about
    # the readback, the enforcement or the teardown is stubbed.
    monkeypatch.setattr(
        SkyPilotProvider,
        "_estimate_hourly_rate",
        lambda self, accelerator, placement: None,
        raising=True,
    )

    spec = dataclasses.replace(build_spec(cfg), run_id=cluster_name)
    # F12 — the durable pre-launch row. compute-seam S5 Task 5 deleted the
    # provider-side writer, so a smoke that drives create_instance directly
    # (bypassing deploy_session) writes it the same way the orchestrator does.
    # Rooted at the CLI's default state dir so it is discoverable without flags.
    _record_provisional_row(
        ledger=Ledger(store=LocalArtifactStore(_STATE_DIR)),
        run_id=spec.run_id,
        provider_name=provider.name,
        tags=dict(spec.tags),
        max_age_s=int(spec.lifecycle.max_lifetime_s),
        now=time.time(),
    )

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
        _log.info("launching %s to violate a $%.2f/hr cap", cluster_name, cap)
        evidence["launched_at"] = _now_local()
        create_thread.start()

        # --- wait for the cluster to exist, polling utilisation throughout ---
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
            # claims here are about the rate and the teardown, neither of which
            # depends on the setup script succeeding.
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
                f"{_READY_TIMEOUT_S:.0f}s; there is no launched instance to "
                f"read a rate off, so the cap claim cannot be made"
            )

        # --- Claim 2: readback — a real number, cross-checked -----------------
        instance: Instance = create_result.get("instance") or Instance(
            id=cluster_name,
            provider="skypilot",
            status="ready",
            created_at=launched_at,
        )
        realized = provider.realized_rate(instance)
        ec2_type = _ec2_type_or_error(cluster_name)
        evidence["readback"] = {
            "status": "read",
            "realized_usd_per_hr": realized,
            "cross_check": {
                "ec2_instance_type": ec2_type,
                "published_usd_per_hr": _SKU_USD_PER_HR,
                "source": "AWS on-demand list price, us-west-2",
            },
            "at": _now_local(),
        }
        assert realized is not None, (
            "realized_rate returned None for a live UP cluster. That would "
            "exercise only the unreadable branch, which is not this claim — "
            "the point is that a REAL number is readable off the handle."
        )
        assert realized > 0.0, f"realized rate {realized!r} is not a real price"
        drift = abs(realized - _SKU_USD_PER_HR) / _SKU_USD_PER_HR
        evidence["readback"]["drift_vs_published"] = round(drift, 4)
        assert drift <= _RATE_AGREEMENT_TOLERANCE, (
            f"readback {realized} disagrees with the published "
            f"{_EXPECTED_SKU} price {_SKU_USD_PER_HR} by {drift:.0%}; either "
            f"the optimizer booked something else (EC2 says {ec2_type!r}) or "
            f"the readback is reading the wrong cluster"
        )
        evidence["readback"]["status"] = "cross-checked"
        _log.info("READBACK realized=%.4f USD/hr (ec2=%s)", realized, ec2_type)

        # --- Claim 3: enforcement — destroy, then raise -----------------------
        with pytest.raises(RateCapExceeded) as ei:
            _enforce_rate_cap(provider=provider, instance=instance, cap=cap)
        exc = ei.value
        evidence["enforcement"] = {
            "status": "raised",
            "realized": exc.realized,
            "cap": exc.cap,
            "instance_id": exc.instance_id,
            "placement_summary": exc.placement_summary,
            "message": str(exc),
            "at": _now_local(),
        }
        assert exc.realized is not None and exc.realized == pytest.approx(realized)
        assert exc.cap == pytest.approx(cap)
        assert exc.instance_id == instance.id
        assert "TEARDOWN ALSO FAILED" not in str(exc), (
            "the enforcement path could not destroy the cluster it refused; "
            "the instance is still billing"
        )
        evidence["enforcement"]["status"] = "destroyed-and-raised"
        _log.info("ENFORCED: %s", exc)
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
                evidence["cap"].get("status") == "from-config"
                and evidence["readback"].get("status") == "cross-checked"
                and evidence["enforcement"].get("status") == "destroyed-and-raised"
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
