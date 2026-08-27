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
  3. ``task_config.resources.cloud`` / ``resources.region`` — present only
     on the live payload. The base config is written cloud-agnostic (see its
     header); this smoke pins ``clouds=["aws"], region="us-west-2"`` on the
     provider itself (exactly as the config's own comment says region
     pinning is "enforced by the smoke test itself, not by this YAML") so
     the launch lands on a known, cheap, real SKU (``c6i.large``, the same
     one ``test_skypilot_watchdog_smoke.py`` uses). That pin is a smoke-only
     addition, not part of what the S1 shape ratchet certifies, so it is
     popped before comparison rather than asserted against.

Credential safety: the spec is built via
``tools.snapshot_launch_payloads.build_spec``, which resolves every
credential-shaped env var to the repo-wide synthetic stub
(``kinoforge-prod-deadbeef``) — never a real one. That keeps ``envs.HF_TOKEN``
byte-identical to the golden with zero normalisation, and guarantees no real
credential ever reaches the captured payload, a log line, or the evidence
file this smoke's follow-up run writes. The tradeoff: the ComfyUI engine's
model download in ``task_config.setup`` will almost certainly fail 401/403
against a fake bearer token — see the module-level fragility note near
``_STALL_CONSECUTIVE_PROBES`` below, and the Task 8 report's "expected
fragility" section.

Gated on KINOFORGE_LIVE_TESTS=1 like every live test here, plus AWS
credentials reachable, the ``aws`` CLI on PATH (the EC2 nuclear-teardown
tier shells out to it, mirroring ``test_skypilot_watchdog_smoke.py``), and
``import sky`` succeeding (``pixi run -e live-skypilot``).

Cost ceiling: < $1 (cheapest AWS CPU SKU ~$0.09/hr, 30 min max_lifetime from
the config's own ``lifecycle.max_lifetime: 30m``, watchdog-enforced even if
this process dies mid-run).
Design: docs/superpowers/sdd/2026-08-24-compute-seam-s1-portable-core/task-8-brief.md
"""

from __future__ import annotations

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
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402
from tools.snapshot_launch_payloads import build_spec, golden_path_for  # noqa: E402

_log = logging.getLogger(__name__)

_REGION = "us-west-2"
_CONFIG_PATH = Path("examples/configs/skypilot-cpu.yaml")
_GOLDEN_PATH = golden_path_for(_CONFIG_PATH)

#: Default kinoforge state dir (matches the CLI's ``--state-dir`` default),
#: so a ledger row this test leaves behind is exactly where
#: ``kinoforge list`` / ``kinoforge forget`` already look for it. Mirrors
#: test_skypilot_watchdog_smoke.py.
_STATE_DIR = Path(".kinoforge")

#: How often utilisation is polled while create_instance (which may block
#: for the full setup+run duration — see the module docstring's credential
#: note on why the download is expected to fail fast instead) is in flight.
#: Project rule: 60-90 s cadence, never spend/elapsed as the health signal.
_UTIL_POLL_INTERVAL_S = 75.0
#: Bounds the whole create_instance() wait. Generous: a CPU box has no model
#: download to wait out on the *success* path (the stub token makes it fail
#: fast), but setup + apt/pip installs alone can take several minutes.
_CREATE_TIMEOUT_S = 900.0
#: After create_instance returns (or is abandoned to the EC2 fallback
#: below), how long to wait for list_instances() to report ready.
_READY_POLL_TIMEOUT_S = 300.0
_READY_POLL_INTERVAL_S = 15.0
#: Consecutive near-zero-CPU probes before a boot is treated as stalled
#: rather than merely slow — matches the project's "0% GPU for >=3
#: consecutive probes = dead worker" rule, ported to a CPU-only box.
_STALL_CONSECUTIVE_PROBES = 3
#: A `top` idle% at or above this is read as "doing essentially nothing".
_STALL_IDLE_PCT_FLOOR = 99.0

_READY_STATUSES = {"ready", "running", "UP"}

_DEADLINE_LITERAL_RE = re.compile(r"'([0-9]+\.[0-9]+)' > \"\$KF_WD_DIR/deadline\.tmp\"")
_TOP_IDLE_RE = re.compile(r"([\d.]+)\s*id\b")
_NAME_SENTINEL = "<normalized-cluster-name>"
_DEADLINE_SENTINEL = "<normalized-launch-deadline-epoch>"

#: aws CLI EC2 tag-filter teardown fallback — same tag SkyPilot writes onto
#: every EC2 instance it provisions (see test_skypilot_watchdog_smoke.py's
#: _ec2_states for the wildcard-suffix rationale).
_DEAD_STATES = {"shutting-down", "terminated"}


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
                """Record ``config`` verbatim, then build the real Task from it."""
                captured["task_config"] = config
                return real_task.from_yaml_config(config)

        return _TaskProxy

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record ``kwargs``, then perform the real launch."""
        self.captured["launch_kwargs"] = kwargs
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
    # Smoke-only pins (see module docstring point 3) — never present on the
    # golden, which captures the config as authored (cloud-agnostic).
    resources.pop("cloud", None)
    resources.pop("region", None)

    normalized["launch_kwargs"]["cluster_name"] = _NAME_SENTINEL
    return normalized


def _ec2_states(cluster_name: str) -> list[str]:
    """Return EC2 instance states tagged with this sky cluster name.

    Mirrors ``test_skypilot_watchdog_smoke.py``'s helper of the same name
    (including the trailing-wildcard rationale: SkyPilot tags instances
    ``ray-cluster-name = <cluster_name>-<8 hex>``, not the bare name).

    Args:
        cluster_name: SkyPilot cluster name.

    Returns:
        List of EC2 ``State.Name`` strings; empty means none found.
    """
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
            "Reservations[].Instances[].State.Name",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        _log.warning(
            "aws ec2 describe-instances failed (rc=%d): %s",
            completed.returncode,
            completed.stderr.strip(),
        )
        return []
    try:
        return list(json.loads(completed.stdout or "[]"))
    except json.JSONDecodeError:
        _log.warning("aws ec2 describe-instances returned unparseable stdout")
        return []


def _probe_util(cluster_name: str) -> str | None:
    """Best-effort CPU/memory snapshot over SSH.

    Args:
        cluster_name: SkyPilot cluster name (also the SSH host alias sky
            writes into ``~/.ssh/config`` once the cluster is provisioned).

    Returns:
        The raw ``top``/``free`` summary line pair, or ``None`` when the
        cluster is not yet SSH-reachable — NOT itself a stall signal, only
        sustained near-zero CPU across ``_STALL_CONSECUTIVE_PROBES`` is.
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
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 — unreachable-during-boot is expected
        _log.info("util probe: ssh not reachable yet for %s (%r)", cluster_name, exc)
        return None
    if completed.returncode != 0:
        _log.info(
            "util probe: ssh to %s exited rc=%d stderr=%s",
            cluster_name,
            completed.returncode,
            completed.stderr.strip(),
        )
        return None
    return completed.stdout.strip()


def _is_stalled(sample: str | None) -> bool:
    """Return whether one utilisation sample reads as "doing essentially nothing".

    Args:
        sample: A ``_probe_util`` return value.

    Returns:
        ``True`` when the ``top`` idle percentage parses and is at/above
        :data:`_STALL_IDLE_PCT_FLOOR`. An unparseable or missing sample
        returns ``False`` — absence of evidence is not evidence of a stall
        by itself (mirrors the EC2-oracle "unknown != terminated" distinction
        in the watchdog smoke).
    """
    if sample is None:
        return False
    match = _TOP_IDLE_RE.search(sample)
    if not match:
        return False
    return float(match.group(1)) >= _STALL_IDLE_PCT_FLOOR


def _capture_setup_log(cluster_name: str) -> None:
    """Best-effort fetch of the remote setup/run log for a stalled-boot diagnosis.

    Exact sky log paths are a documented unknown (see the Task 8 report's
    fragility section) — this is deliberately tolerant of failure; its only
    job is to get *something* into the test log before teardown destroys the
    evidence.

    Args:
        cluster_name: SkyPilot cluster name.
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
                "find ~/sky_logs -name '*.log' -newer /tmp 2>/dev/null "
                "-exec tail -n 80 {} + 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        _log.warning(
            "stalled-boot setup log for %s (rc=%d):\n%s\nstderr:\n%s",
            cluster_name,
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort only
        _log.warning("could not fetch setup log for %s: %r", cluster_name, exc)


def _teardown(provider: SkyPilotProvider, cluster_name: str) -> None:
    """Tiered teardown: provider API, then a direct EC2 nuclear fallback.

    Args:
        provider: The SkyPilot provider whose API can still be used.
        cluster_name: Cluster name for both provider and EC2 lookups.

    Raises:
        RuntimeError: An EC2 instance tagged with ``cluster_name`` is still
            alive after both tiers — surfaced loudly rather than swallowed,
            per the project's "verify teardown, don't trust a mid-run log
            line" rule.
    """
    try:
        _log.info("tearing down via provider.destroy_instance")
        provider.destroy_instance(cluster_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("provider.destroy_instance raised: %r", exc)

    states = _ec2_states(cluster_name)
    if states and not all(s in _DEAD_STATES for s in states):
        _log.warning(
            "provider.destroy_instance left states=%r for %s; forcing "
            "aws ec2 terminate-instances",
            states,
            cluster_name,
        )
        ids_completed = subprocess.run(
            [
                "aws",
                "ec2",
                "describe-instances",
                "--region",
                _REGION,
                "--filters",
                f"Name=tag:ray-cluster-name,Values={cluster_name}*",
                "--query",
                "Reservations[].Instances[].InstanceId",
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        try:
            ids = list(json.loads(ids_completed.stdout or "[]"))
        except json.JSONDecodeError:
            ids = []
        if ids:
            subprocess.run(
                [
                    "aws",
                    "ec2",
                    "terminate-instances",
                    "--region",
                    _REGION,
                    "--instance-ids",
                ]
                + ids,
                capture_output=True,
                text=True,
                timeout=120,
            )

    deadline = time.time() + 180.0
    states = _ec2_states(cluster_name)
    while (
        states and not all(s in _DEAD_STATES for s in states) and time.time() < deadline
    ):
        time.sleep(15.0)
        states = _ec2_states(cluster_name)

    survivors = [s for s in states if s not in _DEAD_STATES]
    if survivors:
        raise RuntimeError(
            f"cluster {cluster_name!r} survived teardown with states {survivors!r} "
            f"— destroy it by hand in {_REGION}"
        )
    _log.info("teardown complete cluster=%s", cluster_name)


def test_s1_migrated_cpu_config_matches_golden_and_boots_live() -> None:
    """A live launch of ``skypilot-cpu.yaml`` matches its golden and boots.

    Two independent claims, checked separately so a boot failure (e.g. the
    stub HF_TOKEN 401ing the model download — see the module docstring)
    cannot mask a payload-shape regression, and vice versa:

    1. The exact ``task_config``/``launch_kwargs`` SkyPilot puts on the wire
       for this config, captured from the REAL ``sky.Task.from_yaml_config``
       + ``sky.launch`` call site, equals the committed golden once the
       three legitimately-volatile fields are normalised (see
       ``_normalize``). Checked as soon as the recording proxy has
       something captured — before waiting on readiness — so a shape
       regression fails fast regardless of what the cluster does next.
    2. The cluster actually reaches a ready state on real AWS infrastructure
       in us-west-2, with utilisation polled (never spend/elapsed) while
       waiting, and torn down whether or not it got there.
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
    provider.set_launch_ledger(Ledger(store=LocalArtifactStore(_STATE_DIR)))

    create_result: dict[str, Any] = {}
    create_exc: list[BaseException] = []

    def _do_create() -> None:
        try:
            create_result["instance"] = provider.create_instance(spec)
        except BaseException as exc:  # noqa: BLE001 — surfaced on the main thread below
            create_exc.append(exc)

    try:
        _log.info(
            "launching %s in %s (clouds=['aws']) — cheapest CPU SKU expected c6i.large",
            cluster_name,
            _REGION,
        )
        launched_at = time.time()
        create_thread = threading.Thread(target=_do_create, daemon=True)
        create_thread.start()

        consecutive_stalled = 0
        create_deadline = time.time() + _CREATE_TIMEOUT_S
        while create_thread.is_alive() and time.time() < create_deadline:
            time.sleep(_UTIL_POLL_INTERVAL_S)
            elapsed = time.time() - launched_at
            sample = _probe_util(cluster_name)
            _log.info("t+%.0fs create_instance in flight; util=%r", elapsed, sample)
            if _is_stalled(sample):
                consecutive_stalled += 1
            else:
                consecutive_stalled = 0
            if consecutive_stalled >= _STALL_CONSECUTIVE_PROBES:
                _capture_setup_log(cluster_name)
                pytest.fail(
                    f"boot appears stalled: {consecutive_stalled} consecutive "
                    f"near-zero-CPU probes at t+{elapsed:.0f}s for {cluster_name!r}"
                )
        create_thread.join(timeout=max(0.0, create_deadline - time.time()))

        # --- Claim 1: wire-shape parity, independent of boot success -----
        assert "task_config" in recording_sky.captured, (
            "sky.Task.from_yaml_config was never reached — no payload to "
            "compare against the golden"
        )
        live_payload = {
            "provider": "skypilot",
            "seam": "sky.Task.from_yaml_config + sky.launch(**kwargs)",
            "task_config": recording_sky.captured["task_config"],
            "launch_kwargs": recording_sky.captured.get("launch_kwargs", {}),
        }
        assert _normalize(live_payload) == _normalize(golden_payload), (
            "live task_config/launch_kwargs diverged from the golden after "
            "normalising cluster name, deadline epoch, and the smoke-only "
            "cloud/region pins — the migrated config no longer puts the "
            "same thing on the wire"
        )

        # --- Claim 2: it actually boots -----------------------------------
        if create_exc:
            # Mirrors test_skypilot_watchdog_smoke.py: don't abort on a
            # create_instance exception — a cluster that came up and then
            # failed mid-create must still be verified directly, not
            # silently written off.
            _log.warning(
                "create_instance raised %r for %s — falling through to a "
                "direct readiness probe instead of aborting",
                create_exc[0],
                cluster_name,
            )

        ready_deadline = time.time() + _READY_POLL_TIMEOUT_S
        status: str | None = None
        while time.time() < ready_deadline:
            for inst in provider.list_instances():
                if inst.id == cluster_name:
                    status = inst.status
            elapsed = time.time() - launched_at
            _log.info("t+%.0fs cluster=%s status=%s", elapsed, cluster_name, status)
            if status in _READY_STATUSES:
                break
            time.sleep(_READY_POLL_INTERVAL_S)

        if status not in _READY_STATUSES:
            _capture_setup_log(cluster_name)
            reason = f"create_instance raised {create_exc[0]!r}; " if create_exc else ""
            pytest.fail(
                f"{reason}cluster {cluster_name!r} never reached a ready state "
                f"within {_READY_POLL_TIMEOUT_S:.0f}s of create_instance "
                f"returning; last status={status!r}"
            )
        _log.info("SMOKE RESULT cluster=%s reached status=%s", cluster_name, status)
    finally:
        _teardown(provider, cluster_name)
