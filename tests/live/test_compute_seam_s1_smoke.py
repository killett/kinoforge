"""S1 live smoke: a migrated config launches what it launched pre-S1.

Cheapest CPU SKU on purpose — S1 changes config/spec SHAPE, and the thing worth
paying to verify is that the shape reaches a real provider intact. GPU adds
cost, not signal.

Config under test: ``examples/configs/skypilot-cpu.yaml``. It already exists,
is CPU-only, and already has a committed golden
(``tests/providers/golden/launch_payloads/skypilot-cpu.json``) produced by
``tools/snapshot_launch_payloads.py`` — this smoke deliberately does NOT add
a second config; it proves the SAME shape the golden ratchet already pins
also reaches a real cloud unmodified.

**This is not a workload test.** The config's engine block is a ComfyUI
placeholder whose provision script clones ComfyUI and downloads a
``Wan2.2-T2V-A14B`` shard. On a 2-vCPU CPU box, with the synthetic
``HF_TOKEN`` this smoke deliberately keeps (see "Credential safety" below),
that download is pointless and expected to fail. So the smoke asserts only
what S1 actually risks:

  1. the config loads under the new surface (``compute.placement`` /
     ``compute.backend_options``) and builds an ``InstanceSpec``;
  2. ``sky`` accepts the resources (cloud/region/disk/cpus) and the cluster
     reaches ``UP`` on real AWS;
  3. the launch payload matches the committed golden for that config;
  4. teardown leaves no billing resource behind.

Server readiness is explicitly NOT asserted. ``sky.launch`` raising because
``Task.setup`` died on the model download is an EXPECTED outcome here, not a
smoke failure: it is caught, the cluster state is checked directly, and the
run proceeds to the payload comparison and teardown. Correspondingly the
smoke stops the moment both claims are proven — as soon as the cluster is
observed ``UP`` it tears down rather than waiting out a setup phase that
cannot succeed. Waiting for a server that can never start would burn the
budget for no signal.

What "same config" means here: the exact ``InstanceSpec`` the golden's
``tools.snapshot_launch_payloads.build_spec`` builds for this config, launched
for real instead of aborted after capture. Two things legitimately cannot be
byte-identical to the frozen golden, and are normalised before comparison —
see ``_normalize`` for the precise fields and why:

  1. ``task_config.name`` / ``launch_kwargs.cluster_name`` — the golden pins
     the synthetic ``"golden-run"``; a live cluster needs a fresh, unique
     name so repeated smoke runs never collide.
  2. The watchdog deadline epoch baked into ``task_config.setup`` (three
     repeated occurrences of the same literal) — the golden freezes
     ``time.time()``; a live run needs a genuine future deadline, not a
     value already hours in the past that would self-terminate the
     instance seconds after boot.
  3. ``task_config.resources.cloud`` / ``resources.region``. This smoke
     pins ``clouds=["aws"], region="us-west-2"`` on the provider itself so
     the launch lands on a known, cheap, real SKU. That was a smoke-only
     addition when written — the config was cloud-agnostic — and
     compute-seam S2 has since moved both pins into the config, so they now
     appear on the golden as well. Either way they are popped before
     comparison rather than asserted against here, and WHERE the launch
     landed is checked separately by ``_assert_launch_target``.

Credential safety: the spec is built via
``tools.snapshot_launch_payloads.build_spec``, which resolves every
credential-shaped env var to the repo-wide synthetic stub
(``kinoforge-prod-deadbeef``) — never a real one. That keeps ``envs.HF_TOKEN``
byte-identical to the golden with zero normalisation, and guarantees no real
credential ever reaches the captured payload, a log line, or the evidence
file this smoke writes. The tradeoff is the expected setup failure described
above, which this smoke is structured to tolerate rather than paper over with
a real token on a CPU box.

Gated on KINOFORGE_LIVE_TESTS=1 like every live test here, plus AWS
credentials reachable, the ``aws`` CLI on PATH (the EC2 nuclear-teardown
tier shells out to it, mirroring ``test_skypilot_watchdog_smoke.py``), and
``import sky`` succeeding (``pixi run -e live-skypilot``).

Cost ceiling: < $1 (cheapest AWS CPU SKU ~$0.09/hr; the smoke abandons the
launch as soon as the cluster is UP, and ``lifecycle.max_lifetime: 30m`` from
the config arms the instance-side watchdog even if this process dies mid-run).
Design: .superpowers/sdd/2026-08-24-compute-seam-s1-portable-core/task-8-brief.md
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime
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
    # Resolves only the local chain (env vars, shared credentials file,
    # instance profile config) — no network call, safe at import time.
    _REASONS.append(
        "AWS credentials not resolved by boto3 "
        "(see AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE)"
    )

if shutil.which("aws") is None:
    # The EC2 nuclear-teardown tier shells out to the aws CLI directly (not
    # boto3), mirroring test_skypilot_watchdog_smoke.py's _ec2_states.
    _REASONS.append("aws CLI binary not found on PATH")

try:
    import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: F401
except ImportError:
    _REASONS.append("skypilot not installed (use `pixi run -e live-skypilot`)")

if _REASONS:
    pytest.skip(
        "S1 compute-seam smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge.core.config import load_config  # noqa: E402
from kinoforge.core.credential_patterns import redact_string  # noqa: E402
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.core.orchestrator import _record_provisional_row  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402
from tools.snapshot_launch_payloads import build_spec, golden_path_for  # noqa: E402

_log = logging.getLogger(__name__)

_REGION = "us-west-2"
_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_GOLDEN_PATH = golden_path_for(_CONFIG_PATH)

#: Raw observations from the live run, written unconditionally (success or
#: failure) so the evidence file records what actually happened rather than
#: only what a green run looks like.
_EVIDENCE_PATH = Path("tests/live/_s1_smoke_evidence.json")

#: Default kinoforge state dir (matches the CLI's ``--state-dir`` default),
#: so a ledger row this test leaves behind is exactly where
#: ``kinoforge list`` / ``kinoforge forget`` already look for it. Mirrors
#: test_skypilot_watchdog_smoke.py.
_STATE_DIR = Path(".kinoforge")

#: How often cluster status + utilisation are polled while create_instance is
#: in flight. Project rule: 60-90 s cadence, never spend/elapsed as the health
#: signal.
_UTIL_POLL_INTERVAL_S = 75.0
#: How long to wait for the recording proxy to observe
#: ``sky.Task.from_yaml_config``. It is the first thing create_instance does
#: after the provisional ledger write, so this is generous by an order of
#: magnitude; it exists only so a hang inside provider construction surfaces
#: as a clear failure instead of blocking on the full create timeout.
_CAPTURE_TIMEOUT_S = 120.0
#: Bounds the whole wait for the cluster to reach UP. Sized for a cold AWS
#: provision plus the ~7.4 GB compressed docker image this config's engine
#: pins (``runpod/pytorch:...-cuda12.4.1-devel``), which SkyPilot pulls before
#: it marks the cluster UP. The smoke abandons the launch the moment UP is
#: observed, so this is a ceiling, not an expected duration.
_CREATE_TIMEOUT_S = 1500.0
#: Consecutive near-zero-CPU probes before a boot is treated as stalled
#: rather than merely slow — matches the project's "0% GPU for >=3
#: consecutive probes = dead worker" rule, ported to a CPU-only box.
_STALL_CONSECUTIVE_PROBES = 3
#: A `top` idle% at or above this is read as "doing essentially nothing".
_STALL_IDLE_PCT_FLOOR = 99.0

#: Teardown is convergent, not one-shot (see :func:`_teardown` for the live
#: leak that forced this). Ceiling on the whole settle loop, the gap between
#: passes, and how many consecutive all-clear passes are required before the
#: cluster is believed gone.
_TEARDOWN_SETTLE_S = 900.0
_TEARDOWN_PASS_INTERVAL_S = 20.0
_TEARDOWN_CLEAN_PASSES = 2

#: Ceiling on the remote-log tail that reaches the committed evidence file.
#: Redaction runs first (see :func:`_capture_setup_log`); this bounds size.
_REMOTE_LOG_MAX_CHARS = 4000

#: `pixi run preflight` is a listed acceptance criterion of this gate, but it
#: is an operator-run command, not something the test can invoke: its
#: clean-tree check would fail against the very evidence file this test is
#: about to rewrite. The runner therefore exports its exit code and captured
#: output here, and the evidence records them. Absent => recorded as
#: "not-recorded", never silently as a pass.
_PREFLIGHT_RC_ENV = "KINOFORGE_S1_PREFLIGHT_RC"
_PREFLIGHT_LOG_ENV = "KINOFORGE_S1_PREFLIGHT_LOG"

#: The SKU this smoke expects sky's optimizer to choose from ``cpus: "1+"`` /
#: ``memory: "2+"`` on AWS us-west-2 — the brief's cheapest-CPU target, and
#: the SKU test_skypilot_watchdog_smoke.py uses.
_EXPECTED_SKU = "c6i.large"

#: kinoforge status strings that mean "the cluster exists and is UP at the
#: provider". ``_sky_status_to_kinoforge`` maps sky's ``UP`` to ``"ready"``;
#: the others are accepted defensively in case that mapping widens.
_READY_STATUSES = {"ready", "running", "UP"}

_DEADLINE_LITERAL_RE = re.compile(r"'([0-9]+\.[0-9]+)' > \"\$KF_WD_DIR/deadline\.tmp\"")
_TOP_IDLE_RE = re.compile(r"([\d.]+)\s*id\b")
_MEM_RE = re.compile(r"Mem:\s+(\d+)\s+(\d+)")
_NAME_SENTINEL = "<normalized-cluster-name>"
_DEADLINE_SENTINEL = "<normalized-launch-deadline-epoch>"

#: aws CLI EC2 tag-filter teardown fallback — same tag SkyPilot writes onto
#: every EC2 instance it provisions (see test_skypilot_watchdog_smoke.py's
#: _ec2_states for the wildcard-suffix rationale).
_DEAD_STATES = {"shutting-down", "terminated"}


def _preflight_record() -> dict[str, Any]:
    """Return what the runner reported about ``pixi run preflight``.

    Returns:
        ``{"command", "exit_code", "passed", "output"}``. ``exit_code`` is
        ``None`` and ``passed`` is ``"not-recorded"`` when the runner did not
        export it — an unrecorded gate is reported as unrecorded, never as a
        pass.
    """
    raw_rc = os.getenv(_PREFLIGHT_RC_ENV)
    log_path = os.getenv(_PREFLIGHT_LOG_ENV)
    output: str | None = None
    if log_path and Path(log_path).exists():
        # Redacted for the same reason as the remote log tail: this text is
        # committed. preflight prints only variable NAMES today, never values.
        output = redact_string(Path(log_path).read_text())[-2000:]
    try:
        exit_code = int(raw_rc) if raw_rc is not None else None
    except ValueError:
        exit_code = None
    return {
        "command": "pixi run preflight",
        "exit_code": exit_code,
        "passed": "not-recorded" if exit_code is None else exit_code == 0,
        "note": (
            "run by the operator BEFORE this process started; it cannot be "
            "invoked from inside the test because its clean-tree check would "
            "fail against the evidence file this test rewrites"
        ),
        "output": output,
    }


def _now_local() -> str:
    """Return an ISO-8601 timestamp in the machine's LOCAL timezone.

    Returns:
        e.g. ``"2026-08-27T13:04:11.123456+01:00"``. Local, never UTC —
        project-wide convention for every filename, id, log and doc.
    """
    return datetime.now().astimezone().isoformat()


class _InputRecordingSky:
    """Wraps the real ``sky`` module; records inputs, changes nothing.

    Unlike ``tools.snapshot_launch_payloads``'s ``_CapturingSky`` (which
    fabricates a response and raises to abort before any real work) or
    ``tests/live/_skypilot_recorder.py``'s ``_RecordingProxy`` (which records
    *return* shapes for fixture regeneration), this proxy exists purely to
    observe what ``SkyPilotProvider.create_instance`` sends on the wire while
    letting every call reach the real ``sky`` module for real — the whole
    point being a live cluster that ALSO proves the payload shape.
    """

    def __init__(self, real_sky: Any) -> None:  # noqa: ANN401
        """Wrap ``real_sky``, starting with nothing captured.

        Args:
            real_sky: The genuine ``sky`` module (imported under
                ``pixi run -e live-skypilot``).
        """
        self._real = real_sky
        self.captured: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Delegate any attribute SkyPilotProvider reads besides Task/launch."""
        return getattr(self._real, name)

    @property
    def Task(self) -> Any:  # noqa: ANN401, N802 — mirrors sky.Task's capital name
        """Return a stand-in exposing ``from_yaml_config`` that records + delegates."""
        real_task = self._real.Task
        captured = self.captured

        class _TaskProxy:
            @staticmethod
            def from_yaml_config(config: dict[str, Any]) -> Any:  # noqa: ANN401
                """Record a SNAPSHOT of ``config``, then build the real Task.

                The snapshot is not defensive style, it is required.
                ``sky.Task.from_yaml_config`` consumes its argument in place —
                verified against sky 0.12.3: every one of ``name``/``run``/
                ``setup``/``envs``/``resources`` is ``config.pop``-ed, leaving
                the caller's dict EMPTY on return. Recording the reference
                (as the first version of this smoke did) hands the golden
                comparison a dict that the very next line guts, so what gets
                compared depends on a thread race rather than on the payload.
                """
                captured["task_config"] = copy.deepcopy(config)
                return real_task.from_yaml_config(config)

        return _TaskProxy

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record a snapshot of ``kwargs``, then perform the real launch.

        Recorded LAST of the two captures, so a waiter that requires both
        ``task_config`` and ``launch_kwargs`` is guaranteed to be looking at
        a complete payload rather than a half-built one.
        """
        self.captured["launch_kwargs"] = copy.deepcopy(kwargs)
        return self._real.launch(task, **kwargs)


def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the fields that legitimately differ between a live run and the golden.

    See the module docstring for exactly which three things are normalised
    and why. Anything else that differs is a real regression, not noise —
    that is the entire point of running this smoke.

    Args:
        payload: A ``{"provider", "seam", "task_config", "launch_kwargs"}``
            dict, either captured live or loaded from the golden file.

    Returns:
        A deep copy of ``payload`` with the three volatile fields replaced
        by fixed sentinels (name/cluster_name) or popped (cloud/region).

    Raises:
        AssertionError: The watchdog deadline literal was not found in
            ``task_config.setup`` — the RENDER_ARM template shape changed
            and this normaliser no longer knows what it is looking at,
            which is a reason to stop and look, not to silently pass.
    """
    normalized: dict[str, Any] = json.loads(json.dumps(payload))
    task_config = normalized["task_config"]
    task_config["name"] = _NAME_SENTINEL

    setup = task_config["setup"]
    match = _DEADLINE_LITERAL_RE.search(setup)
    assert match, (
        "could not find the watchdog deadline literal in task_config.setup; "
        "the RENDER_ARM template shape may have changed — update "
        "_DEADLINE_LITERAL_RE rather than weakening this check"
    )
    task_config["setup"] = setup.replace(match.group(1), _DEADLINE_SENTINEL)

    resources = task_config.get("resources", {})
    # Popped from BOTH sides, not compared. They were smoke-only pins when
    # this was written (the config was cloud-agnostic and only this file
    # pinned aws/us-west-2); compute-seam S2 moved both pins into the config
    # itself, so the golden now carries them too. Popping stays correct
    # either way — and stays necessary, because this normaliser must not be
    # the thing that decides WHERE a launch landed. That is asserted
    # separately by :func:`_assert_launch_target` against the LIVE payload
    # before it gets here; otherwise a regression that launched in another
    # region on a different SKU would satisfy every comparison in this file.
    # S2's own smoke (test_compute_seam_s2_region_smoke.py) is the one that
    # proves the CONFIG supplies those pins.
    resources.pop("cloud", None)
    resources.pop("region", None)

    launch_kwargs = normalized["launch_kwargs"]
    # Replace, never create. Assigning into a missing key would synthesise the
    # same sentinel on BOTH sides and manufacture agreement about a field that
    # had actually been dropped from the launch call. Normalisation may only
    # neutralise known noise; it may never invent a value.
    assert "cluster_name" in launch_kwargs, (
        "launch_kwargs has no 'cluster_name' — normalising would invent one on "
        "both sides and hide the fact that it went missing from the launch call"
    )
    launch_kwargs["cluster_name"] = _NAME_SENTINEL
    return normalized


def _assert_launch_target(live_payload: dict[str, Any]) -> dict[str, str]:
    """Assert the live launch was pinned to THIS smoke's cloud and region.

    The golden is cloud-agnostic, so :func:`_normalize` pops ``cloud`` and
    ``region`` from both sides — which means the payload comparison alone
    proves what went on the wire but says nothing about where it landed.
    This is the missing half.

    Args:
        live_payload: The captured live payload, BEFORE normalisation.

    Returns:
        The ``{"cloud", "region"}`` pair that was asserted, for the evidence.

    Raises:
        AssertionError: The launch was not pinned to ``aws``/``_REGION``.
    """
    resources = live_payload["task_config"].get("resources", {})
    cloud = resources.get("cloud")
    region = resources.get("region")
    assert cloud == "aws", (
        f"live launch was pinned to cloud {cloud!r}, not 'aws' — the smoke's "
        f"own clouds pin is not reaching resources.cloud"
    )
    assert region == _REGION, (
        f"live launch was pinned to region {region!r}, not {_REGION!r} — the "
        f"smoke's own region pin is not reaching resources.region, so the run "
        f"could land anywhere and the golden comparison would not notice"
    )
    return {"cloud": cloud, "region": region}


_STATE_QUERY = "Reservations[].Instances[].State.Name"
_TYPE_QUERY = "Reservations[].Instances[].InstanceType"
_AZ_QUERY = "Reservations[].Instances[].Placement.AvailabilityZone"
_ID_QUERY = "Reservations[].Instances[].InstanceId"


class Ec2QueryFailed(Exception):
    """The EC2 oracle could not be read at all.

    Distinct from "queried successfully, zero instances". Collapsing the two
    is how a teardown reports green over a live box: expired credentials or a
    throttled API for ~40 s returns nothing, and if that reads as an empty
    inventory then two consecutive failures satisfy the all-clear gate while
    a ``c6i.large`` bills. Silence from the oracle is not an answer from it.
    """


def _aws_ec2_query(cluster_name: str, query: str) -> list[Any]:
    """Run one tag-filtered ``aws ec2 describe-instances`` projection.

    Mirrors ``test_skypilot_watchdog_smoke.py``'s ``_ec2_states`` helper
    (including the trailing-wildcard rationale: SkyPilot tags instances
    ``ray-cluster-name = <cluster_name>-<8 hex>``, not the bare name).

    Args:
        cluster_name: SkyPilot cluster name.
        query: A JMESPath projection over ``Reservations[].Instances[]``.

    Returns:
        The parsed JSON list. An EMPTY list means the query succeeded and
        found nothing — it never doubles as an error code.

    Raises:
        Ec2QueryFailed: The CLI exited non-zero, timed out, or returned
            output that would not parse. Callers must treat this as "state
            unknown", never as "nothing is running".
    """
    try:
        completed = subprocess.run(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--region",
                _REGION,
                "--filters",
                f"Name=tag:ray-cluster-name,Values={cluster_name}*",
                "--query",
                query,
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log.warning("aws ec2 describe-instances could not run: %r", exc)
        raise Ec2QueryFailed(f"describe-instances could not run: {exc!r}") from exc
    if completed.returncode != 0:
        _log.warning(
            "aws ec2 describe-instances failed (rc=%d): %s",
            completed.returncode,
            completed.stderr.strip(),
        )
        raise Ec2QueryFailed(
            f"describe-instances rc={completed.returncode}: "
            f"{completed.stderr.strip()[:300]}"
        )
    try:
        return list(json.loads(completed.stdout or "[]"))
    except json.JSONDecodeError as exc:
        _log.warning("aws ec2 describe-instances returned unparseable stdout")
        raise Ec2QueryFailed(
            f"describe-instances stdout did not parse: {exc!r}"
        ) from exc


def _ec2_states(cluster_name: str) -> list[str]:
    """Return EC2 instance states tagged with this sky cluster name.

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        List of EC2 ``State.Name`` strings; empty means the query succeeded
        and found none.

    Raises:
        Ec2QueryFailed: The oracle could not be read — propagated, never
            flattened into an empty list.
    """
    return [str(s) for s in _aws_ec2_query(cluster_name, _STATE_QUERY)]


def _probe_util(cluster_name: str) -> dict[str, Any]:
    """Best-effort CPU/memory snapshot of the cluster host over SSH.

    ``ssh <cluster_name>`` resolves through the ssh config SkyPilot generates
    for the cluster and lands on the **host VM**, so this observes the docker
    image pull and the sky runtime setup too — not only what happens inside
    the task container.

    Args:
        cluster_name: SkyPilot cluster name (also the ssh host alias).

    Returns:
        ``{"reachable": bool, "cpu_idle_pct": float | None,
        "mem_used_mb": int | None, "mem_total_mb": int | None,
        "raw": str | None, "note": str | None}``. Unreachable is NOT itself
        a stall signal — only sustained near-zero CPU on a cluster that is
        already UP is (see :func:`_is_stalled`).
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
                r"top -bn1 | awk '/Cpu\(s\)/{print}'; free -m | awk '/Mem:/{print}'",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001 — unreachable-during-boot is expected
        return {
            "reachable": False,
            "cpu_idle_pct": None,
            "mem_used_mb": None,
            "mem_total_mb": None,
            "raw": None,
            "note": f"ssh not reachable yet ({exc!r})",
        }
    if completed.returncode != 0:
        return {
            "reachable": False,
            "cpu_idle_pct": None,
            "mem_used_mb": None,
            "mem_total_mb": None,
            "raw": None,
            "note": f"ssh rc={completed.returncode}: {completed.stderr.strip()[:200]}",
        }
    raw = completed.stdout.strip()
    idle_match = _TOP_IDLE_RE.search(raw)
    mem_match = _MEM_RE.search(raw)
    return {
        "reachable": True,
        "cpu_idle_pct": float(idle_match.group(1)) if idle_match else None,
        "mem_total_mb": int(mem_match.group(1)) if mem_match else None,
        "mem_used_mb": int(mem_match.group(2)) if mem_match else None,
        "raw": raw,
        "note": None if idle_match else "top idle% did not parse",
    }


def _is_stalled(sample: dict[str, Any]) -> bool:
    """Return whether one utilisation sample reads as "doing essentially nothing".

    Args:
        sample: A :func:`_probe_util` return value.

    Returns:
        ``True`` when the ``top`` idle percentage parsed and is at/above
        :data:`_STALL_IDLE_PCT_FLOOR`. An unreachable or unparseable sample
        returns ``False`` — absence of evidence is not evidence of a stall
        by itself (mirrors the EC2-oracle "unknown != terminated" distinction
        in the watchdog smoke).
    """
    idle = sample.get("cpu_idle_pct")
    if idle is None:
        return False
    return float(idle) >= _STALL_IDLE_PCT_FLOOR


def _capture_setup_log(cluster_name: str) -> str:
    """Best-effort fetch of the remote setup/run log for a stalled-boot diagnosis.

    Exact sky log paths are a documented unknown (see the Task 8 report's
    fragility section) — this is deliberately tolerant of failure; its only
    job is to get *something* into the test log and the evidence file before
    teardown destroys the box.

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        Whatever came back (possibly empty), truncated for the evidence file.
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
                "tail -n 60 ~/.sky/sky_logs/*/*.log 2>/dev/null; "
                "tail -n 40 /var/log/cloud-init-output.log 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort only
        return f"<log capture failed: {exc!r}>"
    text = (completed.stdout or "") + "\n--- stderr ---\n" + (completed.stderr or "")
    # Redact BEFORE the value reaches a log line or the evidence file, which
    # is committed to git. Today this is belt-and-braces: envs.HF_TOKEN is the
    # synthetic stub, so nothing real is on that box to leak. It is here for
    # the day someone reopens the real-token question — at that moment this
    # path would otherwise tail a setup log containing a live bearer token
    # straight into a tracked JSON file, and "rotate first, clean second" is
    # a much worse position than never writing it. Truncated too: a bounded
    # tail keeps a runaway log from bloating the committed artifact.
    redacted = redact_string(text)[-_REMOTE_LOG_MAX_CHARS:]
    _log.warning("remote log for %s (redacted):\n%s", cluster_name, redacted)
    return redacted


def _force_terminate(cluster_name: str) -> list[str]:
    """Terminate every EC2 instance tagged with this cluster, via the aws CLI.

    Used as the FIRST teardown tier whenever ``sky.launch`` is still in
    flight: it stops billing immediately without contending for SkyPilot's
    per-cluster lock, which a concurrent ``sky.down`` would have to wait on.

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        The instance ids the terminate call was issued for (possibly empty).

    Raises:
        Ec2QueryFailed: The instance-id lookup could not be read.
    """
    ids = [str(i) for i in _aws_ec2_query(cluster_name, _ID_QUERY)]
    if ids:
        _log.warning("force-terminating EC2 instances %r for %s", ids, cluster_name)
        subprocess.run(
            ["aws", "ec2", "terminate-instances", "--region", _REGION, "--instance-ids"]
            + ids,
            capture_output=True,
            text=True,
            timeout=120,
        )
    return ids


def _teardown(
    provider: SkyPilotProvider,
    cluster_name: str,
    *,
    create_thread: threading.Thread,
) -> dict[str, Any]:
    """Tear down convergently: keep killing until nothing can come back.

    A single-pass teardown is WRONG here, and the first live run proved it.
    When the test aborts early (a failed assertion seconds after
    ``create_thread.start()``), ``sky.launch`` is still provisioning: sky has
    not registered the cluster yet and EC2 has no tagged instance yet, so a
    one-shot "is anything running?" check reads clean and the teardown
    reports success — while the SkyPilot API server, which is a SEPARATE
    process and outlives this pytest run, goes on to create the instance
    seconds later. Observed 2026-08-27: teardown finished at 04:14:59
    declaring nothing alive, and ``sky status`` at 04:15:12 showed
    ``kinoforge-s1-smoke-3d077623`` in ``INIT`` on a live ``c6i.large``.
    "Nothing is running yet" is not the same claim as "nothing is running".

    So this loops: each pass force-terminates any tagged EC2 instance
    (immediate, and free of SkyPilot's per-cluster lock, which an in-flight
    launch can hold), then ``sky.down``s the cluster if sky lists it at all.
    It only concludes when BOTH oracles read clean on consecutive passes AND
    the launch thread is no longer running to create anything new.

    Args:
        provider: The SkyPilot provider whose API can still be used.
        cluster_name: Cluster name for both provider and EC2 lookups.
        create_thread: The thread running ``create_instance``. While it is
            alive a launch may still materialise an instance, so a clean
            read is not yet trustworthy.

    Returns:
        A record of every pass, for the evidence file.

    Raises:
        RuntimeError: Something tagged with ``cluster_name`` is still alive
            after the settle window — surfaced loudly rather than swallowed,
            per the project's "verify teardown, don't trust a mid-run log
            line" rule.
    """
    record: dict[str, Any] = {"started_at": _now_local(), "passes": []}
    deadline = time.time() + _TEARDOWN_SETTLE_S
    clean_streak = 0

    while time.time() < deadline:
        entry: dict[str, Any] = {"at": _now_local()}
        # ``ec2_readable`` gates the all-clear below. A pass that could not
        # read the EC2 oracle proves nothing about what is running, so it
        # must reset the streak rather than contribute to it.
        ec2_readable = True
        states: list[str] = []
        alive: list[str] = []
        try:
            entry["forced_instance_ids"] = _force_terminate(cluster_name)
        except Ec2QueryFailed as exc:
            ec2_readable = False
            entry["forced_instance_ids"] = None
            entry["ec2_query_error"] = str(exc)

        sky_status = _sky_status_of(provider, cluster_name)
        entry["sky_status"] = sky_status
        if sky_status is not None:
            try:
                provider.destroy_instance(cluster_name)
                entry["destroy_instance"] = "ok"
            except Exception as exc:  # noqa: BLE001
                entry["destroy_instance"] = f"raised: {exc!r}"

        try:
            states = _ec2_states(cluster_name)
            alive = [s for s in states if s not in _DEAD_STATES]
            entry["ec2_states"] = states
        except Ec2QueryFailed as exc:
            ec2_readable = False
            entry["ec2_states"] = None
            entry["ec2_query_error"] = str(exc)

        entry["ec2_readable"] = ec2_readable
        entry["launch_thread_alive"] = create_thread.is_alive()
        record["passes"].append(entry)
        _log.info(
            "teardown pass: sky=%s ec2=%s alive=%r launching=%s readable=%s",
            sky_status,
            states if ec2_readable else "<unreadable>",
            alive,
            entry["launch_thread_alive"],
            ec2_readable,
        )

        if (
            ec2_readable
            and not alive
            and sky_status is None
            and not entry["launch_thread_alive"]
        ):
            clean_streak += 1
            if clean_streak >= _TEARDOWN_CLEAN_PASSES:
                break
        else:
            clean_streak = 0
        time.sleep(_TEARDOWN_PASS_INTERVAL_S)

    try:
        final_states = _ec2_states(cluster_name)
        final_readable = True
    except Ec2QueryFailed as exc:
        # Unknown, not clear. Falls through to the survivor check below, which
        # treats an unreadable final oracle as a failure — the safe direction:
        # a false alarm costs a manual look, a false all-clear costs a live box.
        final_states = []
        final_readable = False
        record["final_ec2_query_error"] = str(exc)
    final_sky = _sky_status_of(provider, cluster_name)
    record["final_ec2_states"] = final_states if final_readable else None
    record["final_ec2_readable"] = final_readable
    record["final_sky_status"] = final_sky
    record["clean_streak"] = clean_streak
    record["finished_at"] = _now_local()

    survivors = [s for s in final_states if s not in _DEAD_STATES]
    teardown_clean = final_readable and not survivors and final_sky is None

    # A provisional "launching" ledger row is written before sky.launch (F12).
    # compute-seam S5 moved that writer OUT of the provider and into the
    # orchestrator, and these smokes call create_instance directly — so no
    # deploy_session collapse ever runs and NOTHING but this teardown removes
    # the row, on the success path or any other. `kinoforge list` would
    # otherwise report it as a live instance forever.
    #
    # Dropping it is correct ONLY once the instance is confirmed dead. Doing it
    # unconditionally would erase the row in exactly the case it exists for:
    # teardown fails, a box keeps billing, and the operator's mandated
    # `pixi run kinoforge list` prints "No instances recorded in ledger." —
    # a clean bill of health over a live instance. That row is Brief 1's F12
    # protection; it must outlive a failed teardown so the ledger still names
    # what needs killing.
    if teardown_clean:
        try:
            Ledger(store=LocalArtifactStore(_STATE_DIR)).forget(cluster_name)
            record["ledger_forget"] = "ok"
        except Exception as exc:  # noqa: BLE001 — best-effort bookkeeping
            _log.warning("ledger forget failed for %s: %r", cluster_name, exc)
            record["ledger_forget"] = f"raised: {exc!r}"
    else:
        record["ledger_forget"] = "skipped — teardown not confirmed clean"

    if not teardown_clean:
        detail = (
            f"ec2 states {survivors!r}"
            if final_readable
            else "ec2 state UNREADABLE (query failed — treat as live)"
        )
        raise RuntimeError(
            f"cluster {cluster_name!r} survived teardown "
            f"({detail}, sky status {final_sky!r}) "
            f"— destroy it by hand in {_REGION}"
        )
    _log.info("teardown complete cluster=%s", cluster_name)
    return record


def _sky_status_of(provider: SkyPilotProvider, cluster_name: str) -> str | None:
    """Return the kinoforge status of ``cluster_name``, or ``None`` if absent.

    Args:
        provider: Provider whose ``list_instances`` reads ``sky.status()``.
        cluster_name: Cluster to look for.

    Returns:
        The mapped status string, ``None`` when sky does not list the
        cluster, or ``"<error>"`` when the status call itself failed (which
        must not be confused with "the cluster is gone").
    """
    try:
        for inst in provider.list_instances():
            if inst.id == cluster_name:
                return str(inst.status)
    except Exception as exc:  # noqa: BLE001 — a status read must never fail the run
        _log.warning("sky status read failed: %r", exc)
        return "<error>"
    return None


def test_s1_migrated_cpu_config_matches_golden_and_boots_live() -> None:
    """A live launch of ``skypilot-cpu.yaml`` matches its golden and reaches UP.

    Two independent claims, checked separately and in that order so a boot
    failure cannot mask a payload-shape regression, and vice versa:

    1. The exact ``task_config``/``launch_kwargs`` SkyPilot puts on the wire
       for this config, captured from the REAL ``sky.Task.from_yaml_config``
       + ``sky.launch`` call site, equals the committed golden once the
       three legitimately-volatile fields are normalised (see
       ``_normalize``). Checked as soon as the recording proxy has something
       captured — seconds in, before any waiting — so a shape regression
       fails fast and cheaply regardless of what the cluster does next.
    2. The cluster reaches ``UP`` at the provider (``sky status`` via
       ``provider.list_instances``), with utilisation polled on a 75 s
       cadence while waiting. NOT server readiness: this config's ComfyUI/Wan
       setup script is expected to fail on a CPU box with a synthetic HF
       token, and ``sky.launch`` raising for that reason is tolerated, not
       treated as a smoke failure.
    3. It landed WHERE this smoke pinned it and on the SKU the cost envelope
       assumes: ``resources.cloud``/``region`` on the live payload before
       normalisation discards them, and the instance type / AZ EC2 actually
       reports. Claims 1 and 2 are both blind to this — ``_normalize`` pops
       cloud and region from both sides, and "a cluster is UP" says nothing
       about its size or location — so without this a run that booked an
       ``m5.4xlarge`` in ``us-east-1`` would satisfy every other assertion
       in the file.

    Teardown runs in ``finally`` either way, and the evidence file is written
    unconditionally.
    """
    cluster_name = f"kinoforge-s1-smoke-{secrets.token_hex(4)}"
    print(cluster_name, flush=True)  # breadcrumb for a hard-kill mid-poll

    assert _CONFIG_PATH.exists(), f"missing config: {_CONFIG_PATH}"
    assert _GOLDEN_PATH.exists(), f"missing golden: {_GOLDEN_PATH}"
    golden_payload: dict[str, Any] = json.loads(_GOLDEN_PATH.read_text())

    cfg = load_config(str(_CONFIG_PATH))
    spec = build_spec(cfg)
    spec = dataclasses.replace(spec, run_id=cluster_name)

    recording_sky = _InputRecordingSky(sky)
    provider = SkyPilotProvider(recording_sky, clouds=["aws"], region=_REGION)
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

    evidence: dict[str, Any] = {
        "smoke": "compute-seam S1 — migrated config launches what it launched pre-S1",
        "task": "task-8 (S1 portable core)",
        "config": str(_CONFIG_PATH),
        "golden": str(_GOLDEN_PATH),
        "cluster_name": cluster_name,
        "region_requested": _REGION,
        "clouds_requested": ["aws"],
        "expected_sku": _EXPECTED_SKU,
        "preflight": _preflight_record(),
        "started_at": _now_local(),
        "utilisation_samples": [],
        "payload_comparison": {"status": "not-reached"},
        "cluster_state": {"observed_ready": False, "statuses_seen": []},
    }

    create_result: dict[str, Any] = {}
    create_exc: list[BaseException] = []

    def _do_create() -> None:
        try:
            create_result["instance"] = provider.create_instance(spec)
        except BaseException as exc:  # noqa: BLE001 — surfaced on the main thread below
            create_exc.append(exc)

    create_thread = threading.Thread(target=_do_create, daemon=True)
    launched_at = time.time()
    try:
        _log.info("launching %s in %s (clouds=['aws'])", cluster_name, _REGION)
        evidence["launched_at"] = _now_local()
        create_thread.start()

        # --- Claim 1: wire-shape parity, seconds in, before any waiting ---
        capture_deadline = time.time() + _CAPTURE_TIMEOUT_S
        # BOTH captures are required. The golden pins task_config AND
        # launch_kwargs; sky.launch is entered a beat after
        # Task.from_yaml_config returns, so waiting only for task_config
        # compares a payload whose launch_kwargs half is still ``{}`` and
        # reports a mismatch that is purely a race in this harness.
        needed = ("task_config", "launch_kwargs")
        while (
            not all(k in recording_sky.captured for k in needed)
            and time.time() < capture_deadline
            and (create_thread.is_alive() or not create_exc)
        ):
            time.sleep(1.0)
        why = f" (create_instance raised: {create_exc[0]!r})" if create_exc else ""
        missing = [k for k in needed if k not in recording_sky.captured]
        assert not missing, (
            f"the sky seam was never fully reached within "
            f"{_CAPTURE_TIMEOUT_S:.0f}s — {missing!r} never captured, so there "
            f"is no complete payload to compare against the golden{why}"
        )
        live_payload = {
            "provider": "skypilot",
            "seam": "sky.Task.from_yaml_config + sky.launch(**kwargs)",
            "task_config": recording_sky.captured["task_config"],
            "launch_kwargs": recording_sky.captured.get("launch_kwargs", {}),
        }
        # Assert WHERE before normalisation discards it. _normalize pops
        # cloud/region from both sides, so without this the whole file would
        # pass for a launch that landed in another region on another SKU.
        launch_target = _assert_launch_target(live_payload)
        evidence["launch_target_asserted"] = launch_target
        live_norm = _normalize(live_payload)
        golden_norm = _normalize(golden_payload)
        evidence["payload_comparison"] = {
            "status": "match" if live_norm == golden_norm else "mismatch",
            "normalised_fields": [
                "task_config.name",
                "launch_kwargs.cluster_name",
                "task_config.setup:<watchdog deadline epoch>",
                "task_config.resources.cloud (smoke-only pin)",
                "task_config.resources.region (smoke-only pin)",
            ],
            "live_resources": dict(live_payload["task_config"].get("resources", {})),
            "live_launch_kwargs": dict(live_payload["launch_kwargs"]),
            "task_config_keys": sorted(live_payload["task_config"]),
            "setup_sha_equal_after_normalisation": (
                live_norm["task_config"]["setup"] == golden_norm["task_config"]["setup"]
            ),
            "compared_at": _now_local(),
        }
        assert live_norm == golden_norm, (
            "live task_config/launch_kwargs diverged from the golden after "
            "normalising cluster name, deadline epoch, and the smoke-only "
            "cloud/region pins — the migrated config no longer puts the "
            "same thing on the wire"
        )
        _log.info("PAYLOAD MATCHES GOLDEN for %s", _CONFIG_PATH)

        # --- Claim 2: the cluster reaches UP on real AWS -------------------
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
            _log.info(
                "t+%.0fs status=%s reachable=%s cpu_idle=%s mem=%s/%s MB",
                elapsed,
                status,
                sample["reachable"],
                sample["cpu_idle_pct"],
                sample["mem_used_mb"],
                sample["mem_total_mb"],
            )
            print(
                f"[poll] t+{elapsed:.0f}s status={status} "
                f"cpu_idle={sample['cpu_idle_pct']} "
                f"mem={sample['mem_used_mb']}/{sample['mem_total_mb']}MB",
                flush=True,
            )

            if status in _READY_STATUSES:
                # Both claims are now proven. Everything after this point is
                # a ComfyUI/Wan setup phase that cannot succeed on a CPU box
                # with a stub token — waiting it out would be pure spend.
                observed_ready = True
                evidence["cluster_state"]["ready_at"] = _now_local()
                evidence["cluster_state"]["ready_after_s"] = round(elapsed, 1)
                break

            if not create_thread.is_alive():
                # create_instance finished (almost certainly by raising) and
                # the cluster is still not UP. One last read in case the
                # status lagged, then stop paying to re-ask.
                status = _sky_status_of(provider, cluster_name)
                evidence["cluster_state"]["statuses_seen"].append(status)
                if status in _READY_STATUSES:
                    observed_ready = True
                    evidence["cluster_state"]["ready_at"] = _now_local()
                    evidence["cluster_state"]["ready_after_s"] = round(elapsed, 1)
                break

            # Stall rule: only meaningful once the box is reachable. A
            # near-idle host while a docker pull / setup is supposedly in
            # flight means the boot died, not that it is being patient.
            if _is_stalled(sample):
                consecutive_stalled += 1
            else:
                consecutive_stalled = 0
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
        # The SKU / AZ actually launched — read from EC2, not from what we
        # asked for, so this records reality rather than intent. Asserted, not
        # merely recorded: `resources.cpus: "1+"` lets sky's optimizer pick,
        # and a regression that quietly booked a bigger box would otherwise be
        # invisible to every check in this file.
        sku_launched = sorted(
            {str(t) for t in _aws_ec2_query(cluster_name, _TYPE_QUERY)}
        )
        azs = sorted({str(z) for z in _aws_ec2_query(cluster_name, _AZ_QUERY)})
        evidence["sku_launched"] = sku_launched
        evidence["availability_zones"] = azs
        if create_exc:
            # EXPECTED on this config: the ComfyUI/Wan setup script cannot
            # succeed on a CPU box with a synthetic HF token. Recorded, never
            # fatal on its own — the cluster state above is the real oracle.
            evidence["create_instance_exception"] = repr(create_exc[0])[:600]
            _log.warning(
                "create_instance raised %r for %s — expected for this config; "
                "cluster state is the oracle",
                create_exc[0],
                cluster_name,
            )
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

        # --- Claim 3: it landed WHERE we pinned it, on the SKU we costed ---
        # An empty list must FAIL, not pass vacuously. `_aws_ec2_query` now
        # raises rather than returning [] on an unreadable answer, but an
        # honest empty answer here would still mean "we never saw the box we
        # are making claims about", which is not evidence of anything.
        assert sku_launched == [_EXPECTED_SKU], (
            f"expected the launch to land on exactly [{_EXPECTED_SKU!r}] but EC2 "
            f"reports {sku_launched!r} for {cluster_name!r} — an empty list means "
            f"the instance was never observed, and any other value means sky's "
            f"optimizer booked a SKU this smoke's cost envelope does not cover"
        )
        assert azs and all(z.startswith(_REGION) for z in azs), (
            f"expected every AZ to be inside {_REGION!r} but EC2 reports {azs!r} "
            f"for {cluster_name!r} — an empty list means no instance was observed"
        )
        _log.info(
            "SMOKE RESULT cluster=%s reached UP on %r in %r",
            cluster_name,
            sku_launched,
            azs,
        )
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
        # ``outcome`` / ``captured_at`` follow the convention the other live
        # evidence files in this directory use (see _c26_phase_a_smoke_evidence
        # .json). PROVEN requires BOTH claims, so a file can never read green
        # off one of them alone.
        evidence["outcome"] = (
            "PROVEN"
            if (
                evidence["payload_comparison"].get("status") == "match"
                and evidence["cluster_state"].get("observed_ready")
            )
            else "NOT-PROVEN"
        )
        evidence["finished_at"] = evidence["captured_at"] = _now_local()
        evidence["billable_wall_clock_s"] = round(time.time() - launched_at, 1)
        _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2) + "\n")
        print(f"[evidence] wrote {_EVIDENCE_PATH}", flush=True)
