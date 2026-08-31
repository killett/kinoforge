"""Live smoke: compute-seam S3 — Task.setup is setup, Task.run is the launch.

Three claims, checked in cost order so a break in the composition root fails
before any spend:

1. **offline** — ``render_launch(spec.launch)`` computed from the config alone.
   This is the independently-derived expectation the live capture is compared
   against, so the wire assertion compares two values rather than a value
   against a literal somebody could edit to match.
2. **wire** — the captured ``task_config["setup"]`` contains NO line that is
   the launch command, and ``task_config["run"]`` equals the offline value.
   Before S3 both halves were false on this very config: SkyPilot rebuilt
   ``run`` by shell-quoting ``run_cmd``, which dropped comfyui's
   ``cd /workspace/ComfyUI &&`` and its ``exec``, so ``main.py`` ran from the
   login directory where it does not exist.
3. **live** — the cluster reaches UP. ``Task.setup`` terminating is a
   precondition for ``Task.run`` ever starting, and a setup that still
   contained a server command could not terminate at all.

``skypilot-cpu.yaml`` is deliberately the comfyui config: comfyui is the engine
whose launch carries a ``workdir``, so it is the one where the old
``_strip_trailing_exec`` heuristic was destructive rather than merely inert.

**Explicitly out of scope, and stated in the evidence:** the diffusers half of
the S3 fix — the double launch, where the server command stayed inside
``Task.setup`` AND was repeated in ``Task.run`` — is proven OFFLINE by Task 5's
regenerated goldens and its unit tests. Proving it live needs a GPU config and
roughly $1; that is a deliberate deferral, not an oversight.

Operational notes inherited from S1/S2, and they are not optional:

* Teardown is S1's convergent ``_teardown``, IMPORTED rather than
  reimplemented, and verified AFTER the process exits via ``kinoforge list``
  and ``sky status``.
* Utilisation is polled on a 60-90 s cadence. Spend is never the health
  signal — it climbs identically whether the box is working or dead.
* Do not run other ``aws`` commands against this account while the smoke is
  in flight. The CLI is slow enough here that a concurrent describe-instances
  is what pushed S2's AZ query over its 120 s ceiling.

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
        "S3 setup/run smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge._adapters import build_provider_for  # noqa: E402
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.interfaces import combine_steps, render_launch  # noqa: E402
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

# S1's smoke owns the teardown convergence, the EC2 oracle and the util probe.
# Imported, not copied: a second teardown implementation is a second place for
# the 2026-08-27 "clean read while the launch is still in flight" bug to come
# back. Importing it also means this module inherits S1's identical skip gate,
# which has already run above.
from tests.live.test_compute_seam_s1_smoke import (  # noqa: E402
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

_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_EVIDENCE_PATH = Path("tests/live/_s3_smoke_evidence.json")
_STATE_DIR = Path(".kinoforge")

#: Project rule: poll utilisation on a 60-90 s cadence, never elapsed spend.
_UTIL_POLL_INTERVAL_S = 75.0
_CAPTURE_TIMEOUT_S = 120.0
#: S2 reached UP at t+1474 s on this same config and SKU against a 1500 s
#: ceiling. Sized for a cold ~7.4 GB image pull, and still a ceiling rather
#: than an expected duration.
_CREATE_TIMEOUT_S = 2100.0
_STALL_CONSECUTIVE_PROBES = 3
_READY_STATUSES = {"ready", "running", "UP"}

#: c6i.large on-demand in us-west-2, for the spend line in the evidence. A
#: published list price, not a reading of the invoice.
_SKU_USD_PER_HR = 0.085

#: The runner exports `pixi run preflight`'s exit code here. The test cannot
#: invoke preflight itself: preflight's clean-tree check would fail against
#: the very evidence file this test is about to rewrite. Absent is recorded as
#: "not-recorded", never silently as a pass.
_PREFLIGHT_RC_ENV = "KINOFORGE_S3_PREFLIGHT_RC"
_PREFLIGHT_LOG_ENV = "KINOFORGE_S3_PREFLIGHT_LOG"


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


def test_s3_setup_terminates_and_the_launch_is_task_run() -> None:
    """The engine's setup/run split reaches the wire, and the cluster boots.

    Bug caught, and it was live at HEAD on this exact config: SkyPilot could
    not tell setup from launch, so it guessed by substring-matching ``" exec "``
    on the script's last line. For comfyui the guess fired and took
    ``cd /workspace/ComfyUI`` with it; for diffusers it did not fire at all and
    left the server inside ``Task.setup``, which then could never terminate.
    """
    cluster_name = f"kinoforge-s3-smoke-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S3 — Task.setup is setup, Task.run is the launch",
        "task": "task-8 (S3 setup/run split)",
        "config": str(_CONFIG_PATH),
        "cluster_name": cluster_name,
        "engine": cfg.engine.kind,
        "why_this_config": (
            "comfyui is the engine whose launch carries a workdir, so it is "
            "the one the old _strip_trailing_exec heuristic actively "
            "corrupted: it removed `cd /workspace/ComfyUI && exec python "
            "main.py ...` wholesale and rebuilt Task.run from run_cmd, "
            "losing the cd."
        ),
        "out_of_scope": (
            "the diffusers-on-SkyPilot DOUBLE LAUNCH (server command left "
            "inside Task.setup AND repeated in Task.run) is fixed and proven "
            "OFFLINE by Task 5's regenerated goldens and unit tests. Proving "
            "it live needs a GPU config and roughly $1; deliberate deferral, "
            "not an oversight."
        ),
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        "utilisation_samples": [],
        "offline_expectation": {"status": "not-reached"},
        "wire_split": {"status": "not-reached"},
        "cluster_state": {"observed_ready": False, "statuses_seen": []},
    }

    # --- Claim 1: offline — derive the expectation from the config alone ---
    # Computed BEFORE the launch and from the engine's own render, so the wire
    # assertion below compares two independently-derived values. Asserting the
    # wire against a literal would pass for any launch as long as somebody
    # edited the literal to match.
    spec = dataclasses.replace(build_spec(cfg), run_id=cluster_name)
    assert spec.launch is not None, (
        f"{_CONFIG_PATH} rendered no launch, so there is nothing for Task.run "
        f"to carry and this smoke would prove nothing"
    )
    expected_run = render_launch(spec.launch)
    expected_setup_tail = combine_steps(spec.setup_steps)
    evidence["offline_expectation"] = {
        "status": "computed",
        "expected_run": expected_run,
        "launch_workdir": spec.launch.workdir,
        "launch_exec_pid1": spec.launch.exec_pid1,
        "setup_step_count": len(spec.setup_steps),
        "at": _now_local(),
    }
    # The workdir is the byte the old strip discarded. If it is absent the
    # rest of this smoke would pass while proving nothing about the bug.
    assert spec.launch.workdir == "/workspace/ComfyUI", (
        f"expected comfyui's launch to carry workdir=/workspace/ComfyUI, got "
        f"{spec.launch.workdir!r} — without it this smoke cannot observe the "
        f"regression it exists to catch"
    )

    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)

    # The recording proxy observes the real sky seam. It is attached AFTER the
    # composition root has finished, and records only — it pins nothing.
    recording_sky = _InputRecordingSky(sky)
    provider._sky_client = recording_sky
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
        _log.info("launching %s to observe the setup/run split", cluster_name)
        evidence["launched_at"] = _now_local()
        create_thread.start()

        # --- Claim 2: wire — setup holds no launch, run holds exactly it ---
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
            f"so there is no payload to check the split against{why}"
        )
        task_config = dict(recording_sky.captured["task_config"])
        setup = str(task_config.get("setup", ""))
        run = str(task_config.get("run", ""))
        setup_lines = [ln for ln in setup.rstrip().split("\n") if ln.strip()]
        evidence["wire_split"] = {
            "status": "captured",
            "run": run,
            "setup_last_line": setup_lines[-1] if setup_lines else "",
            "setup_line_count": len(setup_lines),
            "at": _now_local(),
        }

        # 2a: run IS the rendered launch, workdir and exec included.
        assert run == expected_run, (
            f"task_config['run'] is {run!r} but the config's own launch "
            f"renders to {expected_run!r}. That gap is the live bug: the "
            f"provider rebuilt the line instead of carrying it."
        )
        # 2b: and the launch is NOT also sitting inside setup. A setup that
        # runs the server can never terminate, so Task.run would never start.
        offenders = [ln for ln in setup_lines if ln.strip() == expected_run.strip()]
        assert not offenders, (
            f"task_config['setup'] still contains the launch command "
            f"{expected_run!r}; setup cannot terminate while it is running "
            f"the server, so Task.run would never start"
        )
        # 2c: setup is the watchdog arm plus the steps, and stops there.
        assert setup.endswith(expected_setup_tail), (
            "task_config['setup'] does not end with the engine's combined "
            "steps; something is being appended after them"
        )
        evidence["wire_split"]["status"] = "match"
        _log.info("SPLIT ON THE WIRE: run=%r", run)

        # --- Claim 3: live — the cluster reaches UP -------------------------
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
            # EXPECTED on this config: the comfyui setup fetches Wan weights
            # with a synthetic token onto a CPU box and fails partway. The
            # claims here are about the SPLIT and about the cluster booting,
            # neither of which depends on the setup script succeeding.
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
        _log.info("SMOKE RESULT cluster=%s reached UP", cluster_name)
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
                evidence["offline_expectation"].get("status") == "computed"
                and evidence["wire_split"].get("status") == "match"
                and evidence["cluster_state"].get("observed_ready") is True
            )
            else "NOT-PROVEN"
        )
        evidence["finished_at"] = evidence["captured_at"] = _now_local()
        evidence["billable_wall_clock_s"] = round(billable_s, 1)
        evidence["estimated_spend_usd"] = round(
            billable_s / 3600.0 * _SKU_USD_PER_HR, 4
        )
        evidence["spend_basis"] = (
            f"list price {_SKU_USD_PER_HR} USD/hr for c6i.large in us-west-2, "
            f"times billable wall clock — an estimate, not a reading of the "
            f"invoice"
        )
        _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"[evidence] wrote {_EVIDENCE_PATH}", flush=True)
