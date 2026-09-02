"""Live smoke: compute-seam S5 — endpoint shape, ledger shape, pre-launch refusal.

S5 changed three things a golden cannot see, because none of them is in the
payload kinoforge sends:

* an engine that declares TWO ports gets TWO tunnels, and ``ensure_endpoints``
  can rebuild BOTH from ``tags["ports"]`` after the forwards die (Tasks 0-3);
* one launch leaves exactly ONE ledger row, because the orchestrator writes the
  provisional ``kf_launch_phase=launching`` row and collapses it onto the real
  one (Tasks 4-6);
* an over-cap plan is refused BEFORE ``sky.launch``, so the refusal costs
  nothing instead of discarding a completed ``Task.setup`` (Task 8).

Three claims, in cost order:

1. **offline, free** — ``ensure_endpoints`` on a fabricated ``Instance`` whose
   ``tags["ports"]`` names ``8000,8001`` forwards both, and forwards them AGAIN
   onto fresh local ports once the first pair is dead. No cloud, no network:
   the ssh-spawn and port-allocator seams are injected. This is the offline
   twin of claim 3, and it is what fails first if ``_ports_for`` ever goes back
   to reading only ``instance.endpoints`` (a warm-attached instance has none).
2. **pre-launch refusal, ~$0 because NOTHING is booked** — the estimate is read
   from the REAL sky catalog, asserted to be a positive finite float, sanity-
   bounded against the published SKU price, and then a cap BELOW it must raise
   ``PreLaunchRateCapExceeded`` while ``sky status`` (an independent oracle,
   not merely the absence of an exception) shows no cluster. A cap ABOVE it
   must NOT refuse, so the claim cannot be satisfied by refusing everything.
3. **tunnel repair, ~$0.04 (ceiling ~$0.064)** — a real ``c6i.large`` serving
   HTTP on 8000 and
   8001, both endpoints answering 200, both forwards killed the way a dying
   CLI kills them, and ``ensure_endpoints`` handing back DIFFERENT local ports
   that both answer 200 again. Plus: exactly one ledger row for the cluster
   after the collapse, and it is the real row, not the ``launching`` one.

Why claim 2 does not use ``examples/configs/skypilot-gpu.yaml``
--------------------------------------------------------------
The pre-launch estimate is INERT for two whole classes of config, by design:
``_estimate_hourly_rate`` returns ``None`` for a CPU-only placement (sky's
accelerator catalog has nothing to say about CPU SKUs) and ``None`` for a spot
placement (the parse reads the on-demand ``price`` column, and pricing spot off
it would bound in the wrong direction). ``skypilot-cpu.yaml`` is the first
case, so it cannot exercise this claim at all.

``skypilot-gpu.yaml`` is GPU and non-spot, but it pins NO cloud — and probed at
the installed pin (2026-09-02) ``_select_accelerator`` on it does not return an
accelerator, it RAISES: with no ``clouds`` filter, ``sky.list_accelerators``
fans out to every registered cloud and dies on
``ImportError('Failed to import dependencies for Kubernetes')``. There is no
price to read, so that config cannot produce the positive float this claim
requires either.

So claim 2 builds its spec from ``skypilot-cpu.yaml`` — the config that pins
``clouds: ["aws"]`` and ``region: us-west-2``, which is the only cloud this
smoke holds credentials for — with its placement replaced by one that NAMES an
accelerator. A named accelerator takes the branch of ``_select_accelerator``
that reads no catalog at all, so what is priced is exactly what would be
launched. Probed live at the same pin, ``_estimate_hourly_rate("T4", ...)``
against ``clouds=["aws"]`` returns ``0.526`` — the published AWS on-demand
price of ``g4dn.xlarge`` in ``us-west-2``, which is the cross-check
:data:`_PUBLISHED_T4_USD_PER_HR` encodes.

Why claim 2 fences ``sky.launch`` even though it expects a refusal
-----------------------------------------------------------------
Both arms of claim 2 run against a provider whose ``sky.launch`` raises
:class:`_LaunchReached` instead of launching. That is not stubbing the thing
under test: the refusal happens strictly BEFORE ``sky.launch`` in
``create_instance``, so the fence sits downstream of it and cannot cause it.
What the fence buys is that a REGRESSION in the refusal fails the test loudly
(``_LaunchReached`` where ``PreLaunchRateCapExceeded`` was required) instead of
booking a live ``g4dn.xlarge`` at $0.526/hr to prove the point. The
"nothing was booked" assertion is still made against ``sky status`` and the EC2
oracle, neither of which is this harness's own code.

What claim 3 actually launches, and what it costs
-------------------------------------------------
NOT ``ubuntu:22.04``. The config's ``compute.image`` says that, but
``build_instance_spec`` resolves ``rendered.image or image`` and the comfyui
engine's render wins, so the spec this smoke launches carries
``runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`` (probed
2026-09-02). Two consequences, and both matter:

* python3.11 IS on that image. The interpreter-resolution shim in
  :data:`_SERVE_SCRIPT` is belt-and-braces against a future engine render, not
  a workaround for a missing interpreter.
* SkyPilot pulls that multi-GB CUDA-devel layer onto a 2-vCPU ``c6i.large``
  before it marks the cluster UP. That pull is why S2 measured UP at t+1474 s
  on this exact config and SKU, and it is why the instance-side watchdog
  deadline has to be raised deliberately for this smoke — see
  :data:`_WATCHDOG_MAX_LIFETIME_S`.

Realistic spend for claim 3 is therefore **~$0.04**, not a cent: ~1474 s of
boot plus the assertion sequence and teardown at $0.085/hr. The ceiling is
~$0.064, set by :data:`_WATCHDOG_MAX_LIFETIME_S` (2700 s), which bounds the
cluster's life even if this process dies.

Known live risk for claim 3, stated up front
--------------------------------------------
The forwards are ``ssh -L <local>:localhost:<remote>`` through sky's generated
ssh config. If sky's ssh alias were to land on the HOST VM while the servers
run inside the container, ``localhost:8000`` on the remote end would have no
listener and both HTTP checks would fail for a reason that has nothing to do
with S5. The seam itself is not speculative — entry #21 of
``successful-generations.md`` drove a whole FlashVSR upscale over exactly this
``ssh -L`` tunnel on a SkyPilot cluster — but that was a different cloud and
image, so both :func:`_remote_listeners` (``ss -ltn``: is anything listening on
this side of the boundary?) and :func:`_capture_setup_log` (the job log, where
``kinoforge-s5 serving with $PY`` lands if the servers started at all) are
captured on EVERY failed HTTP check. Together they separate "S5 is broken" from
"ssh landed on the host VM" from "the servers never started".

Operational notes inherited from S1/S2/S3/S4, and they are not optional:

* Teardown is S1's convergent ``_teardown``, IMPORTED rather than
  reimplemented. It forgets the ledger row only on a CONFIRMED-dead teardown,
  so a clean run leaves ``kinoforge list`` empty and a failed one leaves the
  row that names what still needs killing.
* Utilisation is polled on a 75 s cadence by :class:`_UtilPoller`, which
  surfaces ``gpu_util_percent`` / ``cpu_percent`` / ``memory_percent``. Spend
  is NEVER the health signal — it climbs identically whether the box is working
  or dead. ``gpu_util_percent`` is ``None`` here and says so: this is a CPU
  SKU, and ``cpu_percent`` is the signal that matters on it.
* The stall rule is applied ONLY while the launch is still in flight. After the
  cluster is up, two ``http.server`` processes serving nothing are SUPPOSED to
  look idle, and failing on that would be a false alarm by construction.
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
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
    import sky  # type: ignore[import-not-found, unused-ignore]
except ImportError:
    # Bound to None rather than left unbound so a reader (or a linter) sees a
    # defined name; the gate below makes the module skip before anything uses it.
    sky = None
    _REASONS.append("skypilot not installed (use `pixi run -e live-skypilot`)")

if _REASONS:
    pytest.skip(
        "S5 smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge._adapters import build_provider_for  # noqa: E402
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.interfaces import Instance, Launch  # noqa: E402
from kinoforge.core.lifecycle import LAUNCH_PHASE_TAG, Ledger  # noqa: E402
from kinoforge.core.orchestrator import (  # noqa: E402
    _collapse_provisional_row,
    _record_provisional_row,
)
from kinoforge.providers.skypilot import (  # noqa: E402
    PreLaunchRateCapExceeded,
    SkyPilotProvider,
)
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

# S1's smoke OWNS the teardown convergence, the EC2 oracle, the util probe and
# the local-timezone stamp. Imported, not copied: a second teardown
# implementation is a second place for the 2026-08-27 "clean read while the
# launch is still in flight" bug to come back.
from tests.live.test_compute_seam_s1_smoke import (  # noqa: E402
    _ID_QUERY,
    _REGION,
    _TYPE_QUERY,
    Ec2QueryFailed,
    _aws_ec2_query,
    _capture_setup_log,
    _is_stalled,
    _now_local,
    _probe_util,
    _sky_status_of,
    _teardown,
)
from tools.snapshot_launch_payloads import build_spec  # noqa: E402

_log = logging.getLogger(__name__)

_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_ENDPOINT_EVIDENCE_PATH = Path("tests/live/_s5_endpoint_evidence.json")
_REFUSAL_EVIDENCE_PATH = Path("tests/live/_s5_prelaunch_refusal_evidence.json")
_STATE_DIR = Path(".kinoforge")

#: Project rule: poll utilisation on a 60-90 s cadence, never elapsed spend.
_UTIL_POLL_INTERVAL_S = 75.0
#: Consecutive near-zero-CPU probes, WHILE THE LAUNCH IS STILL IN FLIGHT,
#: before the boot is treated as dead rather than slow. Not applied afterwards:
#: two idle ``http.server`` processes are supposed to look like nothing.
_STALL_CONSECUTIVE_PROBES = 3
#: The instance-side watchdog deadline this smoke launches with, RAISED from
#: the config's own ``max_lifetime: 30m``. Deliberate, and the numbers force it:
#: ``watchdog.compute_deadline`` returns ``min(max_lifetime_s, budget/rate)`` =
#: ``min(1800, 0.5/0.5*3600) = 1800`` for this config, S2 measured UP on this
#: exact config and SKU at t+1474 s, and the assertion sequence after that
#: (two HTTP settles at up to 180 s each, the tunnel kill, then two more) can
#: legitimately want another ~750 s. 1474 + 750 > 1800, so at the shipped
#: deadline a boot only 20% slower than S2's would be killed MID-ASSERTION and
#: the smoke would report an S5 failure that is really a watchdog expiry —
#: after the money is spent. 2700 s leaves ~1200 s of headroom past a
#: S2-speed boot. The budget arm does not bind (3600 s), so this is the whole
#: bound; the cost of raising it is a worst-case ceiling of ~$0.064 instead of
#: ~$0.043, which is the right trade for not paying for an uninterpretable run.
_WATCHDOG_MAX_LIFETIME_S = 2700.0
#: Ceiling on the wait for ``create_instance`` to return. Deliberately BELOW
#: :data:`_WATCHDOG_MAX_LIFETIME_S` so that when it fires the box is still
#: alive: ``_capture_setup_log`` can still read the remote log, and the
#: utilisation samples still describe a running machine. A create timeout at or
#: above the watchdog deadline is dead code — the watchdog would have killed
#: the cluster first and the diagnosis would be gone. S2 reached UP at
#: t+1474 s; the S4 rate-cap smoke took 302 s. A ceiling, not an expected
#: duration.
_CREATE_TIMEOUT_S = 1500.0
#: How long an endpoint may take to answer before the check gives up. The run
#: command has to start two servers after sky.launch returns.
_HTTP_SETTLE_S = 180.0
_HTTP_POLL_INTERVAL_S = 3.0
#: How long a terminated forward may take to actually leave ``poll() is None``.
_TUNNEL_DEATH_TIMEOUT_S = 30.0

#: The two ports claim 3's engine declares. 8001 is not decorative: the
#: project's own live-smoke rule fetches ``bootstrap.log`` from a second
#: file-server port, which is the case S5 Task 0 existed to make work.
_PORTS: tuple[str, str] = ("8000", "8001")

#: The SKU sky's optimizer picks for this config's ``cpus: "1+"`` on AWS
#: us-west-2, and the basis of the spend estimate. S1, S2, S3 and S4 all
#: measured it.
_CPU_SKU = "c6i.large"
_CPU_SKU_USD_PER_HR = 0.085

#: Claim 2 prices THIS accelerator. Named rather than inferred, so
#: ``_select_accelerator`` takes its no-catalog-read branch and what is priced
#: is exactly what would have been launched.
_PRICED_ACCELERATOR = "T4"
#: AWS on-demand list price for ``g4dn.xlarge`` (1x T4) in us-west-2 — the
#: independent cross-check on the catalog readback. A published price, not a
#: reading of the invoice.
_PUBLISHED_T4_USD_PER_HR = 0.526
_ESTIMATE_AGREEMENT_TOLERANCE = 0.30
#: A cap BELOW any real T4 price, so the refusal must fire.
_CAP_BELOW_ESTIMATE = 0.01
#: A cap ABOVE it, so the refusal must NOT fire. Without this arm the claim is
#: satisfied by a provider that refuses everything.
_CAP_ABOVE_ESTIMATE = 5.00

_PREFLIGHT_RC_ENV = "KINOFORGE_S5_PREFLIGHT_RC"
_PREFLIGHT_LOG_ENV = "KINOFORGE_S5_PREFLIGHT_LOG"

#: Claim 3's run command. ``render_launch`` joins ``argv`` VERBATIM (it does not
#: shell-quote — see ``core/interfaces.render_launch``), so the script is quoted
#: here explicitly.
#:
#: The interpreter is resolved rather than named. Not because the image lacks
#: one — it does not. The image that actually launches is NOT the config's
#: ``compute.image: ubuntu:22.04``: ``build_instance_spec`` takes
#: ``rendered.image or image`` and the comfyui engine's render wins, so the spec
#: carries ``runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04`` (probed
#: 2026-09-02), which ships python3.11. The resolution chain is therefore
#: belt-and-braces against a future engine render, not a fix for a missing
#: interpreter, and the ``echo`` is the breadcrumb that says which one won.
_SERVE_SCRIPT = (
    'PY="$(command -v python3 || command -v python)"; '
    'test -n "$PY" || PY="$HOME/miniconda3/bin/python3"; '
    'echo "kinoforge-s5 serving with $PY"; '
    '"$PY" -m http.server 8000 --directory /tmp & '
    '"$PY" -m http.server 8001 --directory /tmp & '
    "wait"
)

#: Shared by claims 1 and 3 so one file carries both halves of the endpoint
#: story. Written after each claim, so a run that stops early still records
#: what it proved rather than nothing.
_ENDPOINT_EVIDENCE: dict[str, Any] = {
    "smoke": "compute-seam S5 — endpoint shape and ledger shape",
    "task": "task-10 (S5 live proof); scaffold committed RED by task-9",
    "config": str(_CONFIG_PATH),
    "declared_ports": list(_PORTS),
    "claim_1_offline": {"status": "not-reached"},
    "claim_3_live": {"status": "not-reached"},
    "started_at": _now_local(),
}


def _preflight_record() -> dict[str, Any]:
    """Return what the runner reported about ``pixi run preflight``.

    Returns:
        ``{"exit_code": int | "not-recorded", "log": str | None}``. An
        unrecorded gate is reported as unrecorded, never as a pass.
    """
    raw = os.getenv(_PREFLIGHT_RC_ENV)
    record: dict[str, Any] = {"exit_code": "not-recorded", "log": None}
    if raw is not None and raw.strip().lstrip("-").isdigit():
        record["exit_code"] = int(raw.strip())
    log = os.getenv(_PREFLIGHT_LOG_ENV)
    if log:
        record["log"] = log[-2000:]
    return record


def _write_endpoint_evidence() -> None:
    """Flush :data:`_ENDPOINT_EVIDENCE` to disk with a local-timezone stamp."""
    _ENDPOINT_EVIDENCE["captured_at"] = _now_local()
    _ENDPOINT_EVIDENCE_PATH.write_text(json.dumps(_ENDPOINT_EVIDENCE, indent=2) + "\n")
    print(f"[evidence] wrote {_ENDPOINT_EVIDENCE_PATH}", flush=True)


class _FakeProc:
    """A stand-in for an ``ssh -L`` subprocess handle, for the offline claim.

    Implements only what :class:`~kinoforge.providers.skypilot.SkyPilotProvider`
    reads off a tunnel process: ``poll`` (liveness) and ``terminate``.
    """

    def __init__(self, remote_port: int, local_port: int) -> None:
        """Record which forward this stands for, and start it alive.

        Args:
            remote_port: The remote port being forwarded.
            local_port: The local port it is bound to.
        """
        self.remote_port = remote_port
        self.local_port = local_port
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        """Return None while alive, an exit code once terminated.

        Returns:
            ``None`` until :meth:`terminate` runs, then ``-15``.
        """
        return self.returncode

    def terminate(self) -> None:
        """Kill the forward, the way a dying CLI's process teardown would."""
        self.terminated = True
        self.returncode = -15


class _LaunchReached(RuntimeError):
    """``sky.launch`` was reached — i.e. the pre-launch refusal did NOT fire.

    Claim 2's fence. Raised in place of a real launch so that a regression in
    the refusal costs a failing assertion rather than a live ``g4dn.xlarge``.
    """


class _AbortAtLaunch:
    """Delegates every ``sky`` call to the real module except ``launch``.

    ``Task.from_yaml_config``, ``status`` and ``list_accelerators`` are all
    local, cheap and REAL through this proxy — only the one call that would
    book a resource is fenced.
    """

    def __init__(self, real_sky: Any) -> None:  # noqa: ANN401
        """Wrap the genuine sky module.

        Args:
            real_sky: The genuine ``sky`` module.
        """
        self._real = real_sky
        self.launch_attempts: list[str] = []

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Delegate every attribute except ``launch`` to the real module.

        Args:
            name: Attribute name.

        Returns:
            The real module's attribute.
        """
        return getattr(self._real, name)

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Refuse to launch, recording the cluster name that would have booked.

        Args:
            task: The built ``sky.Task`` (unused).
            **kwargs: The launch kwargs; ``cluster_name`` is recorded.

        Raises:
            _LaunchReached: Always. Reaching here means the pre-launch cap
                check let an over-cap plan through.
        """
        cluster_name = str(kwargs.get("cluster_name", "<unnamed>"))
        self.launch_attempts.append(cluster_name)
        raise _LaunchReached(
            f"sky.launch was reached for cluster {cluster_name!r} — nothing "
            f"was booked because this smoke fences the call, but the "
            f"pre-launch cap check did not refuse it"
        )


class _UtilPoller:
    """Polls host utilisation on a fixed cadence for as long as it is running.

    Runs as a daemon thread rather than inline in the wait loop so utilisation
    keeps being surfaced during the endpoint and tunnel-repair phases too, not
    only while the launch is in flight.

    Attributes:
        samples: Every observation taken, in order, for the evidence file.
        consecutive_stalled: How many probes in a row read as doing essentially
            nothing. Meaningful ONLY while a boot is in flight.
    """

    def __init__(
        self,
        cluster_name: str,
        *,
        started_at: float,
        interval_s: float = _UTIL_POLL_INTERVAL_S,
    ) -> None:
        """Prepare a poller for ``cluster_name``.

        Args:
            cluster_name: The cluster (and ssh host alias) to probe.
            started_at: Epoch seconds the launch began, for ``elapsed_s``.
            interval_s: Seconds between probes. Project rule: 60-90 s.
        """
        self._cluster_name = cluster_name
        self._started_at = started_at
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name="kf-s5-util-poll", daemon=True
        )
        self.samples: list[dict[str, Any]] = []
        self.consecutive_stalled = 0

    def start(self) -> None:
        """Begin polling. The first probe lands one interval from now."""
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and wait briefly for the thread to notice.

        Safe to call when :meth:`start` never ran or raised: joining a thread
        that was never started raises ``RuntimeError``, and this is called from
        the ``finally`` that also tears the cluster down — so an unguarded join
        would propagate out of the ``finally`` BEFORE ``_teardown`` and leave a
        live cluster billing. ``ident`` is None until the thread actually
        starts, which is exactly the discriminator needed.
        """
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=30.0)

    def _loop(self) -> None:
        """Probe on the cadence until stopped, appending to :attr:`samples`."""
        while not self._stop.wait(self._interval_s):
            try:
                sample = _probe_util(self._cluster_name)
            except Exception as exc:  # noqa: BLE001 — a probe must never crash
                sample = {"reachable": False, "note": f"probe raised {exc!r}"}
            idle = sample.get("cpu_idle_pct")
            total = sample.get("mem_total_mb")
            used = sample.get("mem_used_mb")
            sample.update(
                {
                    "at": _now_local(),
                    "elapsed_s": round(time.time() - self._started_at, 1),
                    # Named exactly as the project's polling rule names them.
                    # gpu_util_percent is None and SAYS so: this is a CPU SKU,
                    # so cpu_percent is the health signal on this box.
                    "gpu_util_percent": None,
                    "cpu_percent": (
                        round(100.0 - float(idle), 1) if idle is not None else None
                    ),
                    "memory_percent": (
                        round(float(used) / float(total) * 100.0, 1)
                        if total and used is not None
                        else None
                    ),
                    "health_signal": "cpu_percent (CPU SKU — no GPU on this box)",
                }
            )
            if _is_stalled(sample):
                self.consecutive_stalled += 1
            else:
                self.consecutive_stalled = 0
            self.samples.append(sample)
            print(
                f"[poll] t+{sample['elapsed_s']:.0f}s "
                f"gpu={sample['gpu_util_percent']} "
                f"cpu={sample['cpu_percent']}% "
                f"mem={sample['memory_percent']}%",
                flush=True,
            )


def _http_ok(url: str, *, deadline_s: float = _HTTP_SETTLE_S) -> dict[str, Any]:
    """Poll ``url`` until it answers, and report the final observation.

    Args:
        url: Absolute local URL to fetch.
        deadline_s: How long to keep retrying before giving up.

    Returns:
        ``{"url", "status", "error", "attempts", "waited_s"}``. ``status`` is
        None when nothing ever answered — recorded as unknown, never as a pass.
    """
    started = time.time()
    attempts = 0
    last_error: str | None = None
    while time.time() - started < deadline_s:
        attempts += 1
        try:
            with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
                return {
                    "url": url,
                    "status": int(response.status),
                    "error": None,
                    "attempts": attempts,
                    "waited_s": round(time.time() - started, 1),
                }
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = repr(exc)[:200]
            time.sleep(_HTTP_POLL_INTERVAL_S)
    return {
        "url": url,
        "status": None,
        "error": last_error,
        "attempts": attempts,
        "waited_s": round(time.time() - started, 1),
    }


def _remote_listeners(cluster_name: str) -> str:
    """Best-effort ``ss -ltn`` on the cluster, for diagnosing a dead endpoint.

    Tells "S5's tunnel bookkeeping is broken" apart from "the servers are on
    the other side of the container boundary from where ssh landed" — see the
    module docstring's live-risk note.

    Args:
        cluster_name: The cluster (and ssh host alias) to inspect.

    Returns:
        Whatever came back, truncated; a marker string when it could not run.
    """
    try:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                cluster_name,
                "ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        return f"<listener probe failed: {exc!r}>"
    return ((completed.stdout or "") + (completed.stderr or ""))[:2000]


def _rows_for(ledger: Ledger, instance_id: str) -> list[dict[str, Any]]:
    """Return EVERY ledger row carrying ``instance_id``.

    ``Ledger.read`` returns the FIRST match, so it cannot see a duplicate —
    and a duplicate is exactly the S5 Task 4/5 regression this looks for.

    Args:
        ledger: The ledger to scan.
        instance_id: The id to match on.

    Returns:
        All matching entries, in ledger order.
    """
    return [e for e in ledger.entries() if e.get("id") == instance_id]


def _local_ports_of(endpoints: dict[str, str]) -> dict[str, int | None]:
    """Return the ``port -> local port`` view of an endpoint map.

    Parsed, not split on the last colon: ``rsplit(":", 1)`` records a
    plausible-looking number for any shape that is not
    ``http://127.0.0.1:<port>``, which is precisely the case this evidence
    exists to make visible. ``urlsplit(...).port`` returns None instead, so a
    malformed endpoint reads as unknown rather than as a port.

    Args:
        endpoints: ``{"8000": "http://127.0.0.1:53411", ...}``.

    Returns:
        ``{"8000": 53411, ...}``; None for any URL with no parseable port.
    """
    parsed: dict[str, int | None] = {}
    for port, url in endpoints.items():
        try:
            parsed[port] = urllib.parse.urlsplit(url).port
        except ValueError:
            # urlsplit raises on an out-of-range port literal.
            parsed[port] = None
    return parsed


# ---------------------------------------------------------------------------
# Claim 1 — offline, free
# ---------------------------------------------------------------------------


def test_s5_claim1_ensure_endpoints_reforwards_every_declared_port() -> None:
    """``ensure_endpoints`` rebuilds BOTH declared forwards onto fresh ports.

    Bug caught, and it is invisible to every golden: before S5 the provider
    held ONE tunnel per cluster and ``ensure_endpoints`` had no port list to
    work from other than ``instance.endpoints`` — which a warm-attached
    instance does not have, because the forwards belonged to a process that has
    exited. Either regression (dropping ``tags["ports"]`` as the source, or
    collapsing the per-port map back to a single forward) makes this return a
    one-key map or an empty one, while the launch payload stays byte-identical.

    Also asserts the REPAIR half: with both forwards dead, a second call must
    hand back DIFFERENT local ports. Returning the recorded-but-dead ports is
    finding F11 exactly, and it is what made the warm-attach path hand an HTTP
    client a socket nothing was listening on.
    """
    cluster_name = f"kinoforge-s5-offline-{secrets.token_hex(4)}"
    spawned: list[tuple[str, int, int]] = []
    procs: list[_FakeProc] = []
    allocated: list[int] = []
    next_port = [40000]

    def _fake_spawn(cluster: str, local_port: int, remote_port: int) -> _FakeProc:
        """Record the forward instead of running ssh.

        Args:
            cluster: Cluster the forward targets.
            local_port: Local port chosen by the allocator.
            remote_port: Remote port being forwarded.

        Returns:
            A fake, live process handle.
        """
        spawned.append((cluster, local_port, remote_port))
        proc = _FakeProc(remote_port=remote_port, local_port=local_port)
        procs.append(proc)
        return proc

    def _fake_alloc() -> int:
        """Hand out a fresh, never-repeated local port.

        Returns:
            A monotonically increasing port number.
        """
        next_port[0] += 1
        allocated.append(next_port[0])
        return next_port[0]

    provider = SkyPilotProvider(
        object(),  # sky client — unreachable on this path, and must stay so
        clouds=["aws"],
        region=_REGION,
        ssh_spawn=_fake_spawn,
        port_allocator=_fake_alloc,
    )
    # A warm-attach shaped instance: the ports are known ONLY from the tag,
    # and endpoints is empty because the forwards died with another process.
    instance = Instance(
        id=cluster_name,
        provider="skypilot",
        status="ready",
        created_at=time.time(),
        endpoints={},
        tags={"ports": ",".join(_PORTS)},
    )

    first = provider.ensure_endpoints(instance)
    assert set(first) == set(_PORTS), (
        f"ensure_endpoints must forward every port named in tags['ports'] "
        f"({list(_PORTS)!r}); it returned {sorted(first)!r}. An empty map means "
        f"the tag is no longer the source of the port list; a one-key map means "
        f"the per-port tunnel map collapsed back to one forward per cluster"
    )
    for port, url in first.items():
        assert url.startswith("http://127.0.0.1:"), (
            f"endpoint for port {port} is {url!r}, not an absolute local http "
            f"URL — F11's other half was handing callers an ssh:// URL"
        )
    assert len(set(first.values())) == len(_PORTS), (
        f"two declared ports must map to two DISTINCT local URLs; got {first!r}"
    )
    assert [(c, r) for c, _l, r in spawned] == [
        (cluster_name, int(p)) for p in _PORTS
    ], (
        f"expected one ssh forward per declared port, in declaration order; "
        f"spawned {spawned!r}"
    )

    # Kill both forwards the way a dying CLI does, then repair.
    for proc in procs:
        proc.terminate()
    assert all(p.terminated for p in procs)

    second = provider.ensure_endpoints(instance)
    assert set(second) == set(_PORTS), (
        f"after both forwards died, ensure_endpoints must rebuild both; got "
        f"{sorted(second)!r}"
    )
    assert not set(second.values()) & set(first.values()), (
        f"the repaired endpoints reuse a dead local port — {first!r} -> "
        f"{second!r}. Replaying a recorded-but-dead port IS finding F11"
    )
    assert len(spawned) == 2 * len(_PORTS), (
        f"a repair must respawn every dead forward; ssh was spawned "
        f"{len(spawned)} times for {2 * len(_PORTS)} expected forwards"
    )

    _ENDPOINT_EVIDENCE["claim_1_offline"] = {
        "status": "PROVEN",
        "cluster_name": cluster_name,
        "declared_ports": list(_PORTS),
        "endpoints_before_kill": first,
        "endpoints_after_repair": second,
        "local_ports_before": _local_ports_of(first),
        "local_ports_after": _local_ports_of(second),
        "ssh_forwards_spawned": [
            {"cluster": c, "local_port": lp, "remote_port": rp} for c, lp, rp in spawned
        ],
        "at": _now_local(),
    }
    _write_endpoint_evidence()


# ---------------------------------------------------------------------------
# Claim 2 — pre-launch refusal, ~$0 because nothing is booked
# ---------------------------------------------------------------------------


def test_s5_claim2_over_cap_plan_is_refused_before_anything_is_booked() -> None:
    """An over-cap plan is refused pre-launch; an in-cap one is not.

    Bug caught, and it is the one Task 8 originally shipped: a no-op refusal.
    ``sky.optimize`` returns ``None`` at the installed pin, so the first
    implementation could never produce a number and never refused anything — and
    a smoke that asserted only "no cluster was created" would have reported
    GREEN on it, because a code path that does nothing creates nothing either.

    So this asserts the number FIRST: ``_estimate_hourly_rate`` must return a
    positive finite float against the REAL sky catalog, and a ``None`` fails the
    test rather than skipping it. It is then cross-checked against the published
    AWS price for the SKU, so a catalog that starts answering with a plausible-
    looking wrong number (a per-card price, an 8-GPU instance price) is caught
    too. Only then is the refusal driven, and only then is "nothing was booked"
    asserted — from ``sky status`` and the EC2 oracle, which are not this
    harness's own code.

    The final arm drives a cap ABOVE the estimate and requires that the launch
    is NOT refused. Without it, a provider that raised
    ``PreLaunchRateCapExceeded`` unconditionally would satisfy every other
    assertion here.
    """
    refused_cluster = f"kinoforge-s5-refuse-{secrets.token_hex(4)}"
    allowed_cluster = f"kinoforge-s5-allow-{secrets.token_hex(4)}"
    print(refused_cluster, allowed_cluster, flush=True)  # hard-kill breadcrumb

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))
    base_spec = build_spec(cfg)
    reference_provider = build_provider_for(cfg)
    assert isinstance(reference_provider, SkyPilotProvider)

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S5 — an over-cap plan is refused before launch",
        "task": "task-10 (S5 live proof); scaffold committed RED by task-9",
        "config": str(_CONFIG_PATH),
        "priced_accelerator": _PRICED_ACCELERATOR,
        "clusters": {"refused": refused_cluster, "allowed": allowed_cluster},
        "claim": (
            "the pre-launch estimate is a REAL number read from sky's catalog; "
            "a cap below it refuses before sky.launch and books nothing; a cap "
            "above it does not refuse"
        ),
        "why_not_skypilot_gpu_yaml": (
            "skypilot-gpu.yaml pins no cloud, so _select_accelerator's catalog "
            "read fans out to every registered cloud and raises ImportError on "
            "Kubernetes at the installed pin — no price, no claim. "
            "skypilot-cpu.yaml is CPU-only, for which _estimate_hourly_rate "
            "returns None BY DESIGN. This claim therefore uses "
            "skypilot-cpu.yaml's AWS/us-west-2 pins with a placement that "
            "NAMES an accelerator."
        ),
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        # Nothing is booked on this path, so there is no box to poll. Recorded
        # explicitly rather than omitted, so the absence is a statement.
        "utilisation_samples": [],
        "utilisation_note": (
            "no cluster is created on this path; there is nothing to poll. The "
            "75 s utilisation poller runs on claim 3, which has a live box."
        ),
        "estimate": {"status": "not-reached"},
        "refusal": {"status": "not-reached"},
        "nothing_booked": {"status": "not-reached"},
        "in_cap_arm": {"status": "not-reached"},
    }

    gpu_placement = dataclasses.replace(
        base_spec.placement,
        accelerators=(_PRICED_ACCELERATOR,),
        min_vram_gb=16,
        spot=False,
        max_usd_per_hr=_CAP_BELOW_ESTIMATE,
    )

    dead_thread = threading.Thread(target=lambda: None)
    dead_thread.start()
    dead_thread.join()

    try:
        # --- (a) the estimate is a REAL number ---------------------------
        selected = reference_provider._select_accelerator(gpu_placement)  # noqa: SLF001
        assert selected == _PRICED_ACCELERATOR, (
            f"a named accelerator must be honoured verbatim (that is the "
            f"no-catalog-read branch this claim relies on); "
            f"_select_accelerator returned {selected!r}"
        )
        estimate = reference_provider._estimate_hourly_rate(  # noqa: SLF001
            selected, gpu_placement
        )
        evidence["estimate"] = {
            "status": "read",
            "accelerator": selected,
            "usd_per_hr": estimate,
            "published_usd_per_hr": _PUBLISHED_T4_USD_PER_HR,
            "published_source": (
                "AWS on-demand list price, g4dn.xlarge (1x T4), us-west-2"
            ),
            "clouds": ["aws"],
            "at": _now_local(),
        }
        assert estimate is not None, (
            "_estimate_hourly_rate returned None against the REAL sky catalog "
            "for a named, non-spot accelerator on a pinned cloud. None is the "
            "documented WARN-and-proceed path, so a refusal built on it can "
            "never fire — this is the exact no-op the first Task 8 "
            "implementation shipped, and it must FAIL here, not skip"
        )
        assert isinstance(estimate, float)
        assert estimate == estimate and estimate not in (  # noqa: PLR0124 — NaN check
            float("inf"),
            float("-inf"),
        ), f"estimate {estimate!r} is not finite"
        assert estimate > 0.0, (
            f"estimate {estimate!r} is not positive; a zero would read as "
            f"'free' and vouch for any cap"
        )
        drift = abs(estimate - _PUBLISHED_T4_USD_PER_HR) / _PUBLISHED_T4_USD_PER_HR
        evidence["estimate"]["drift_vs_published"] = round(drift, 4)
        assert drift <= _ESTIMATE_AGREEMENT_TOLERANCE, (
            f"catalog estimate {estimate} disagrees with the published "
            f"g4dn.xlarge price {_PUBLISHED_T4_USD_PER_HR} by {drift:.0%} — a "
            f"per-card price or an 8-GPU instance price would land here"
        )
        assert estimate > _CAP_BELOW_ESTIMATE, (
            f"the below-cap arm needs {_CAP_BELOW_ESTIMATE} to be under the "
            f"estimate; estimate is {estimate}"
        )
        assert estimate < _CAP_ABOVE_ESTIMATE, (
            f"the in-cap arm needs {_CAP_ABOVE_ESTIMATE} to be over the "
            f"estimate; estimate is {estimate}"
        )
        evidence["estimate"]["status"] = "positive-finite-and-agrees"

        # --- (c) a cap BELOW it refuses ----------------------------------
        # sky.launch is fenced on BOTH arms: the refusal fires strictly before
        # it, so the fence cannot cause the refusal — it only means a
        # REGRESSION in the refusal costs a failing assertion instead of a live
        # g4dn.xlarge at $0.526/hr.
        refusing_sky = _AbortAtLaunch(sky)
        refusing_provider = SkyPilotProvider(
            refusing_sky, clouds=["aws"], region=_REGION
        )
        refused_spec = dataclasses.replace(
            base_spec, run_id=refused_cluster, placement=gpu_placement
        )
        with pytest.raises(PreLaunchRateCapExceeded) as caught:
            refusing_provider.create_instance(refused_spec)
        message = str(caught.value)
        representation = repr(caught.value)
        evidence["refusal"] = {
            "status": "raised",
            "type": type(caught.value).__name__,
            "cap_usd_per_hr": _CAP_BELOW_ESTIMATE,
            "str": message,
            "repr": representation[:600],
            "sky_launch_attempts": list(refusing_sky.launch_attempts),
            "at": _now_local(),
        }
        assert not refusing_sky.launch_attempts, (
            f"sky.launch was reached for {refusing_sky.launch_attempts!r} — the "
            f"refusal fired only AFTER the launch call, which is the S4 "
            f"post-launch story, not the pre-launch one"
        )
        assert "PRE-LAUNCH" in message, (
            f"the refusal must read as a pre-launch estimate; str(exc) is {message!r}"
        )
        assert "nothing was launched" in message, (
            f"the refusal must say nothing was launched; str(exc) is {message!r}"
        )
        assert refused_cluster in message
        # BOTH renderings. The base class's __init__ already handed the S4
        # post-launch wording to Exception.__init__, so repr() and args[0] read
        # from there rather than from __str__ — a __str__-only override leaves
        # "instance destroyed" in the durable evidence file describing a
        # cluster that was never booked.
        assert "destroyed" not in message, (
            f"str(exc) claims a resource was destroyed; nothing was ever "
            f"booked. Message: {message!r}"
        )
        assert "destroyed" not in representation, (
            f"repr(exc) claims a resource was destroyed; nothing was ever "
            f"booked. This is what reaches the evidence file. Repr: "
            f"{representation!r}"
        )
        assert "PRE-LAUNCH" in representation, (
            f"repr(exc) does not carry the pre-launch wording: {representation!r}"
        )
        evidence["refusal"]["status"] = "pre-launch-and-well-worded"

        # --- (d) nothing was booked, per INDEPENDENT oracles --------------
        sky_status = _sky_status_of(refusing_provider, refused_cluster)
        # ``ec2_error`` is BOUND here rather than written straight into the
        # evidence: the dict below replaces the whole key, so an error written
        # in the except branch would be erased a few lines later and the
        # ``assert ec2_readable`` that follows would fail with its cause gone.
        ec2_error: str | None = None
        try:
            ec2_ids = [str(i) for i in _aws_ec2_query(refused_cluster, _ID_QUERY)]
            ec2_readable = True
        except Ec2QueryFailed as exc:
            ec2_ids = []
            ec2_readable = False
            ec2_error = str(exc)
        ledger = Ledger(store=LocalArtifactStore(_STATE_DIR))
        ledger_rows = _rows_for(ledger, refused_cluster)
        evidence["nothing_booked"] = {
            "status": "read",
            "sky_status": sky_status,
            "ec2_instance_ids": ec2_ids if ec2_readable else None,
            "ec2_readable": ec2_readable,
            "ec2_query_error": ec2_error,
            "ledger_rows": ledger_rows,
            "at": _now_local(),
        }
        assert sky_status is None, (
            f"sky lists cluster {refused_cluster!r} as {sky_status!r} after a "
            f"refusal that claimed nothing was launched — the absence of an "
            f"exception is not evidence, this is"
        )
        assert ec2_readable, (
            f"the EC2 oracle could not be read ({ec2_error}), so what exists "
            f"is UNKNOWN — which is not the same as nothing"
        )
        assert ec2_ids == [], (
            f"EC2 reports instances {ec2_ids!r} tagged for a cluster that was "
            f"refused before launch"
        )
        assert ledger_rows == [], (
            f"a refused launch left {len(ledger_rows)} ledger row(s) for "
            f"{refused_cluster!r}: {ledger_rows!r}. S5 Task 5 deleted the "
            f"provider-side provisional writer precisely so a refusal cannot "
            f"leave a phantom 'launching' row in `kinoforge list`"
        )
        evidence["nothing_booked"]["status"] = "nothing-exists"

        # --- (e) a cap ABOVE it does NOT refuse ---------------------------
        allowing_sky = _AbortAtLaunch(sky)
        allowing_provider = SkyPilotProvider(
            allowing_sky, clouds=["aws"], region=_REGION
        )
        allowed_spec = dataclasses.replace(
            base_spec,
            run_id=allowed_cluster,
            placement=dataclasses.replace(
                gpu_placement, max_usd_per_hr=_CAP_ABOVE_ESTIMATE
            ),
        )
        with pytest.raises(_LaunchReached):
            allowing_provider.create_instance(allowed_spec)
        allowed_sky_status = _sky_status_of(allowing_provider, allowed_cluster)
        evidence["in_cap_arm"] = {
            "status": "not-refused",
            "cap_usd_per_hr": _CAP_ABOVE_ESTIMATE,
            "estimate_usd_per_hr": estimate,
            "sky_launch_attempts": list(allowing_sky.launch_attempts),
            "sky_status": allowed_sky_status,
            "note": (
                "reaching the fenced sky.launch IS the assertion: the cap "
                "check let an in-budget plan through. Nothing was booked "
                "because the fence raises in place of the launch."
            ),
            "at": _now_local(),
        }
        assert allowing_sky.launch_attempts == [allowed_cluster], (
            f"an in-cap plan must reach sky.launch; launch attempts were "
            f"{allowing_sky.launch_attempts!r}. A provider that refuses "
            f"everything would satisfy every other assertion in this test"
        )
        assert allowed_sky_status is None, (
            f"sky lists {allowed_cluster!r} as {allowed_sky_status!r}; the "
            f"launch fence should have made booking impossible"
        )
    finally:
        # Safety net, not bookkeeping: if a regression DID book something
        # despite the fence, this is what kills it. Convergent, and it forgets
        # the ledger row only on a confirmed-dead teardown.
        teardown_errors: list[str] = []
        for name in (refused_cluster, allowed_cluster):
            try:
                evidence.setdefault("teardown", {})[name] = _teardown(
                    reference_provider, name, create_thread=dead_thread
                )
            except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
                evidence.setdefault("teardown", {})[name] = {"error": repr(exc)}
                evidence["outcome"] = "TEARDOWN-FAILED"
                teardown_errors.append(f"{name}: {exc!r}")
        evidence.setdefault(
            "outcome",
            "PROVEN"
            if (
                evidence["estimate"].get("status") == "positive-finite-and-agrees"
                and evidence["refusal"].get("status") == "pre-launch-and-well-worded"
                and evidence["nothing_booked"].get("status") == "nothing-exists"
                and evidence["in_cap_arm"].get("status") == "not-refused"
            )
            else "NOT-PROVEN",
        )
        evidence["finished_at"] = evidence["captured_at"] = _now_local()
        evidence["estimated_spend_usd"] = 0.0
        evidence["spend_basis"] = (
            "no resource is created on this path: sky.launch is fenced on both "
            "arms and the refusal fires before it. Zero, not 'about zero'."
        )
        _REFUSAL_EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"[evidence] wrote {_REFUSAL_EVIDENCE_PATH}", flush=True)
        if teardown_errors:
            # Raised from the finally on purpose, the same way S1's smoke does
            # it: this path is supposed to book nothing, so a teardown that
            # could not confirm that means something IS alive and billing.
            # Masking a prior assertion is the lesser harm — a survivor must
            # never be reported quietly.
            raise RuntimeError(
                "S5 claim-2 safety-net teardown could not confirm the clusters "
                f"are gone: {'; '.join(teardown_errors)} — destroy them by hand "
                f"in {_REGION}"
            )


# ---------------------------------------------------------------------------
# Claim 3 — tunnel repair on a live cluster, ~$0.04 (ceiling ~$0.064)
# ---------------------------------------------------------------------------


def test_s5_claim3_live_cluster_serves_both_ports_and_repairs_both_tunnels() -> None:
    """Two declared ports serve, both forwards die, and both come back live.

    Bug caught, and no golden can see it: the launch payload is identical
    whether the provider opens one tunnel or two, and identical whether
    ``ensure_endpoints`` can rebuild them or hands back a dead local port. Only
    a real cluster with a real listener on each port can tell the difference
    between "an endpoint map was returned" and "the endpoints work".

    Three things are asserted that the offline twin cannot reach:

    1. ``instance.endpoints`` carries BOTH declared ports and both answer HTTP
       200 through the ssh forwards the provider opened.
    2. After both forwards are terminated the way a dying CLI terminates them,
       ``ensure_endpoints`` returns DIFFERENT local ports that both answer 200
       again. Same local port back would be F11 — a recorded-but-dead socket.
    3. The cluster has EXACTLY ONE ledger row after the collapse, and it is the
       real row rather than the ``kf_launch_phase=launching`` one. SkyPilot is
       the shape where both rows share a key, so a ``forget`` that matched on
       id alone would delete both and leave a live cluster invisible; a
       collapse that never ran would leave two rows and ``kinoforge list``
       double-counting one cluster.
    """
    cluster_name = f"kinoforge-s5-tunnel-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    cfg = load_config(str(_CONFIG_PATH))
    provider = build_provider_for(cfg)
    assert isinstance(provider, SkyPilotProvider)

    # The engine's own setup_steps are dropped: this config's ComfyUI/Wan
    # provision cannot succeed on a CPU box with a synthetic HF token (see the
    # S1/S3 evidence) and it is the slow, expensive part of the boot. It is
    # also entirely irrelevant to what this claim measures. The provider still
    # emits the instance-side watchdog arm at the top of Task.setup, so the
    # cluster is still bounded if this process dies.
    #
    # The instance-side watchdog deadline is raised from the config's own
    # 30 min to _WATCHDOG_MAX_LIFETIME_S. Deliberate and load-bearing — see
    # that constant: at 30 min a boot only 20% slower than S2's measured
    # 1474 s would be killed mid-assertion, and the smoke would report an S5
    # failure that is really a watchdog expiry, after the money is spent.
    base_spec = build_spec(cfg)
    spec = dataclasses.replace(
        base_spec,
        run_id=cluster_name,
        ports=_PORTS,
        setup_steps=(),
        launch=Launch(argv=("bash", "-lc", shlex.quote(_SERVE_SCRIPT))),
        lifecycle=dataclasses.replace(
            base_spec.lifecycle, max_lifetime_s=_WATCHDOG_MAX_LIFETIME_S
        ),
    )

    claim: dict[str, Any] = {
        "status": "not-reached",
        "cluster_name": cluster_name,
        "expected_sku": _CPU_SKU,
        "launched_image": spec.image,
        "image_source": (
            "engine render (build_instance_spec takes `rendered.image or "
            "image`), NOT compute.image — see the module docstring"
        ),
        "watchdog_max_lifetime_s": _WATCHDOG_MAX_LIFETIME_S,
        "create_timeout_s": _CREATE_TIMEOUT_S,
        "preflight": _preflight_record(),
        "run_command": _SERVE_SCRIPT,
        "utilisation_samples": [],
        "endpoints_at_create": {},
        "http_before": [],
        "endpoints_after_repair": {},
        "http_after": [],
        "ledger": {"status": "not-reached"},
        "started_at": _now_local(),
    }
    _ENDPOINT_EVIDENCE["claim_3_live"] = claim

    ledger = Ledger(store=LocalArtifactStore(_STATE_DIR))
    # F12 — the durable pre-launch row, written exactly the way the
    # orchestrator writes it. S5 Task 5 deleted the provider-side writer, so a
    # smoke driving create_instance directly must do this itself or there is no
    # provisional row for the collapse assertion to be about.
    provisional_id = _record_provisional_row(
        ledger=ledger,
        run_id=spec.run_id,
        provider_name=provider.name,
        tags=dict(spec.tags),
        max_age_s=int(spec.lifecycle.max_lifetime_s),
        now=time.time(),
    )
    claim["provisional_row_id"] = provisional_id

    create_result: dict[str, Any] = {}
    create_exc: list[BaseException] = []

    def _do_create() -> None:
        """Run ``create_instance`` off the main thread, capturing its outcome."""
        try:
            create_result["instance"] = provider.create_instance(spec)
        except BaseException as exc:  # noqa: BLE001 — surfaced on the main thread
            create_exc.append(exc)

    create_thread = threading.Thread(target=_do_create, daemon=True)
    launched_at = time.time()
    poller = _UtilPoller(cluster_name, started_at=launched_at)
    try:
        _log.info("launching %s serving ports %r", cluster_name, _PORTS)
        claim["launched_at"] = _now_local()
        create_thread.start()
        poller.start()

        deadline = time.time() + _CREATE_TIMEOUT_S
        while create_thread.is_alive() and time.time() < deadline:
            time.sleep(5.0)
            if poller.consecutive_stalled >= _STALL_CONSECUTIVE_PROBES:
                claim["stall"] = {
                    "detected_at": _now_local(),
                    "consecutive_probes": poller.consecutive_stalled,
                    "remote_log_tail": _capture_setup_log(cluster_name),
                }
                pytest.fail(
                    f"boot appears stalled: {poller.consecutive_stalled} "
                    f"consecutive near-zero-CPU probes for {cluster_name!r}"
                )
        claim["create_returned_after_s"] = round(time.time() - launched_at, 1)
        if create_exc:
            claim["create_instance_exception"] = repr(create_exc[0])[:600]
        if create_thread.is_alive():
            claim["remote_log_tail"] = _capture_setup_log(cluster_name)
            pytest.fail(
                f"create_instance did not return within {_CREATE_TIMEOUT_S:.0f}s "
                f"for {cluster_name!r}; sky status="
                f"{_sky_status_of(provider, cluster_name)!r}"
            )
        assert not create_exc, (
            f"create_instance raised {create_exc[0]!r}; this claim needs the "
            f"Instance it returns, because instance.endpoints IS the thing "
            f"under test"
        )
        instance: Instance = create_result["instance"]
        # How much cluster life is left before the instance-side watchdog
        # autodowns the box. Recorded HERE, at the moment the assertions are
        # about to start, so a failure late in the sequence is self-diagnosing:
        # a small or negative number means the watchdog expired mid-assertion
        # and the failure says nothing about S5.
        claim["deadline_remaining_s"] = round(
            _WATCHDOG_MAX_LIFETIME_S - (time.time() - launched_at), 1
        )

        # --- 0. the box the spend estimate is priced off -------------------
        # S1 makes this assertion and says why: `resources.cpus: "1+"` lets
        # sky's optimizer pick, so a regression that quietly booked a bigger
        # box is invisible to every other check in this file — and every
        # USD figure below is computed from _CPU_SKU_USD_PER_HR.
        sku_launched = sorted(
            {str(t) for t in _aws_ec2_query(cluster_name, _TYPE_QUERY)}
        )
        claim["sku_launched"] = sku_launched
        claim["sku_tag"] = instance.tags.get("sku")
        assert sku_launched == [_CPU_SKU], (
            f"EC2 reports {sku_launched!r} for {cluster_name!r}, not "
            f"[{_CPU_SKU!r}]. An empty list means the box was never observed; "
            f"any other value means sky's optimizer booked a SKU this smoke's "
            f"cost envelope does not cover, and every estimated_spend_usd "
            f"below (priced at {_CPU_SKU_USD_PER_HR} USD/hr) is wrong"
        )
        assert instance.tags.get("sku") == _CPU_SKU, (
            f"instance.tags['sku'] is {instance.tags.get('sku')!r} while EC2 "
            f"reports {sku_launched!r} — Task 7 put the booked SKU on the "
            f"Instance, and an omitted or disagreeing tag means "
            f"_selection_tags could not read the handle, so what a "
            f"RateCapExceeded would report about this cluster is unknown"
        )

        # --- 1. both declared ports came back, and both serve --------------
        claim["endpoints_at_create"] = dict(instance.endpoints)
        claim["tags_ports"] = instance.tags.get("ports")
        assert instance.tags.get("ports") == ",".join(_PORTS), (
            f"instance.tags['ports'] is {instance.tags.get('ports')!r}, not "
            f"{','.join(_PORTS)!r} — that tag is the ONLY thing a later "
            f"ensure_endpoints (or a warm attach in another process) can "
            f"rebuild the forwards from"
        )
        assert set(instance.endpoints) == set(_PORTS), (
            f"create_instance returned endpoints for {sorted(instance.endpoints)!r}, "
            f"not {sorted(_PORTS)!r} — an engine declaring two ports means both "
            f"are load-bearing"
        )
        before = dict(instance.endpoints)
        for port, url in before.items():
            record = _http_ok(url)
            claim["http_before"].append({"port": port, **record})
            if record["status"] != 200:
                claim["remote_listeners"] = _remote_listeners(cluster_name)
                claim["remote_log_tail"] = _capture_setup_log(cluster_name)
            assert record["status"] == 200, (
                f"endpoint {url!r} for remote port {port} did not answer 200 "
                f"within {_HTTP_SETTLE_S:.0f}s (last error {record['error']!r}). "
                f"A returned endpoint map that does not serve is exactly the "
                f"failure S5 exists to prevent"
            )

        # --- 3. exactly ONE ledger row, and it is the real one -------------
        # Written and collapsed the way the orchestrator does it: the real row
        # first, so no window exists in which a kill loses both.
        ledger.record(instance, max_age_s=int(spec.lifecycle.max_lifetime_s))
        _collapse_provisional_row(ledger, provisional_id, instance.id)
        rows = _rows_for(ledger, cluster_name)
        claim["ledger"] = {
            "status": "read",
            "row_count": len(rows),
            "rows": rows,
            "at": _now_local(),
        }
        assert len(rows) == 1, (
            f"expected exactly ONE ledger row for {cluster_name!r} after the "
            f"collapse; found {len(rows)}: {rows!r}. Two rows means the "
            f"collapse did not run (kinoforge list double-counts one cluster); "
            f"zero means it deleted both, which on SkyPilot's same-key shape "
            f"leaves a live billing cluster with no durable handle"
        )
        surviving = rows[0]
        assert LAUNCH_PHASE_TAG not in (surviving.get("tags") or {}), (
            f"the surviving row is still the provisional one "
            f"({LAUNCH_PHASE_TAG}={surviving.get('tags', {}).get(LAUNCH_PHASE_TAG)!r}) "
            f"— the real row was the one dropped, so every reader now sees a "
            f"placeholder for a live cluster"
        )
        claim["ledger"]["status"] = "one-real-row"

        # --- 2. kill both forwards, then repair ----------------------------
        held = provider._tunnels.get(cluster_name) or {}  # noqa: SLF001
        assert set(held) == set(_PORTS), (
            f"the provider holds forwards for {sorted(held)!r}, not {sorted(_PORTS)!r}"
        )
        for tunnel in held.values():
            tunnel.proc.terminate()  # noqa: SLF001 — the way a dying CLI does it
        death_deadline = time.time() + _TUNNEL_DEATH_TIMEOUT_S
        while time.time() < death_deadline and any(
            t.proc.poll() is None for t in held.values()
        ):
            time.sleep(1.0)
        claim["tunnels_killed"] = {
            port: {"returncode": tunnel.proc.poll()} for port, tunnel in held.items()
        }
        assert all(t.proc.poll() is not None for t in held.values()), (
            "the ssh forwards did not die after terminate(); the repair claim "
            "would be vacuous because nothing needed repairing"
        )

        after = provider.ensure_endpoints(instance)
        claim["endpoints_after_repair"] = dict(after)
        claim["local_ports_before"] = _local_ports_of(before)
        claim["local_ports_after"] = _local_ports_of(after)
        assert set(after) == set(_PORTS), (
            f"ensure_endpoints rebuilt {sorted(after)!r}, not {sorted(_PORTS)!r} "
            f"— both declared ports must come back"
        )
        assert after != before, (
            f"ensure_endpoints handed back the SAME local endpoints after both "
            f"forwards died: {before!r}. Replaying a dead local port IS "
            f"finding F11"
        )
        assert not set(after.values()) & set(before.values()), (
            f"a repaired endpoint reuses a dead local port: {before!r} -> {after!r}"
        )
        for port, url in after.items():
            record = _http_ok(url)
            claim["http_after"].append({"port": port, **record})
            if record["status"] != 200:
                # Both, symmetrically with the pre-repair check: the listener
                # probe answers "is anything bound on this side of the
                # container boundary?" and the job log answers "did the
                # servers ever start?". Either alone leaves the other
                # explanation open.
                claim["remote_listeners_after"] = _remote_listeners(cluster_name)
                claim["remote_log_tail_after"] = _capture_setup_log(cluster_name)
            assert record["status"] == 200, (
                f"repaired endpoint {url!r} for remote port {port} did not "
                f"answer 200 within {_HTTP_SETTLE_S:.0f}s (last error "
                f"{record['error']!r}) — a fresh port that does not serve is "
                f"no better than a dead one"
            )
        claim["status"] = "PROVEN"
        _log.info("S5 tunnel repair proven for %s", cluster_name)
    finally:
        # Nothing may stand between entering this `finally` and _teardown. The
        # poller is bookkeeping; the teardown is the only thing keeping a
        # cluster from outliving this process. `stop()` is already guarded
        # against a never-started thread, and this is the second belt: any
        # fault in the poller or in serialising its samples is recorded and
        # stepped over, never propagated past the teardown below.
        try:
            poller.stop()
            claim["utilisation_samples"] = poller.samples
        except Exception as exc:  # noqa: BLE001 — must never preempt teardown
            claim["util_poller_error"] = repr(exc)[:300]
        try:
            claim["teardown"] = _teardown(
                provider, cluster_name, create_thread=create_thread
            )
        except Exception as exc:  # noqa: BLE001 — recorded, then re-raised below
            claim["teardown"] = {"error": repr(exc)}
            claim["status"] = "TEARDOWN-FAILED"
            claim["finished_at"] = _now_local()
            _write_endpoint_evidence()
            raise
        billable_s = time.time() - launched_at
        claim["finished_at"] = _now_local()
        claim["billable_wall_clock_s"] = round(billable_s, 1)
        claim["estimated_spend_usd"] = round(
            billable_s / 3600.0 * _CPU_SKU_USD_PER_HR, 4
        )
        claim["spend_basis"] = (
            f"list price {_CPU_SKU_USD_PER_HR} USD/hr for {_CPU_SKU} in "
            f"{_REGION} (the SKU asserted against the EC2 oracle above), times "
            f"billable wall clock — an estimate, not a reading of the invoice. "
            f"Ceiling is {_WATCHDOG_MAX_LIFETIME_S / 3600.0 * _CPU_SKU_USD_PER_HR:.3f} "
            f"USD, set by the instance-side watchdog at "
            f"{_WATCHDOG_MAX_LIFETIME_S:.0f}s"
        )
        _ENDPOINT_EVIDENCE["outcome"] = (
            "PROVEN"
            if (
                _ENDPOINT_EVIDENCE["claim_1_offline"].get("status") == "PROVEN"
                and claim.get("status") == "PROVEN"
            )
            else "NOT-PROVEN"
        )
        _write_endpoint_evidence()
