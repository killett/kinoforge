"""Opt-in live smoke: a SkyPilot cluster dies without its client.

Launches the cheapest AWS CPU SKU in us-west-2 with a 5-minute deadline and
a run command that sleeps far past it, then drops every in-process handle —
no destroy call is ever made. Pass condition: EC2 itself reports the instance
``shutting-down`` or ``terminated``. ``sky status`` is deliberately NOT the
oracle: the cluster vanishing from sky's local state would prove nothing
about the money.

Gated by (module-level skip if any is missing):
  - ``KINOFORGE_LIVE_TESTS=1``
  - AWS credentials reachable by boto3/awscli
  - ``import sky`` succeeds (use ``pixi run -e live-skypilot``)

Cost: < $0.05 (cheapest CPU SKU ~$0.01/hr, <= ~12 min wall-clock).
Design: docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import time
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
    # get_credentials() only resolves the local chain (env vars, shared
    # credentials file, instance profile config) — it makes no network
    # call, so this check is safe to run unconditionally at import time.
    _REASONS.append(
        "AWS credentials not resolved by boto3 "
        "(see AWS_SHARED_CREDENTIALS_FILE / AWS_CONFIG_FILE)"
    )

try:
    import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: F401
except ImportError:
    _REASONS.append("skypilot not installed (use `pixi run -e live-skypilot`)")

if _REASONS:
    pytest.skip(
        "SkyPilot watchdog smoke skipped: " + " / ".join(_REASONS),
        allow_module_level=True,
    )

# Imports below are evaluated only when the skip gate above passes.
from kinoforge.core.interfaces import (  # noqa: E402
    HardwareRequirements,
    InstanceSpec,
    Lifecycle,
)
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402

_log = logging.getLogger(__name__)

_REGION = "us-west-2"
_DEADLINE_S = 300.0
#: Deadline + skylet tick (<=60 s) + terminate + generous slack.
_KILL_TIMEOUT_S = 900.0
_POLL_INTERVAL_S = 30.0
_DEAD_STATES = {"shutting-down", "terminated"}


def _ec2_states(cluster_name: str) -> list[str]:
    """Return EC2 instance states tagged with this sky cluster name.

    Args:
        cluster_name: SkyPilot cluster name, matched against the
            ``ray-cluster-name`` tag SkyPilot writes onto every EC2
            instance it provisions.

    Returns:
        List of EC2 ``State.Name`` strings for matching instances. Empty
        when the AWS CLI call fails or no instances match (both are
        treated as "already reaped" by the caller).
    """
    completed = subprocess.run(
        [
            "aws",
            "ec2",
            "describe-instances",
            "--region",
            _REGION,
            "--filters",
            f"Name=tag:ray-cluster-name,Values={cluster_name}",
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
        _log.warning("describe-instances failed: %s", completed.stderr.strip())
        return []
    return list(json.loads(completed.stdout or "[]"))


def _teardown(cluster_name: str, tunnel: Any) -> None:
    """Teardown that must run whatever the assertions did.

    Idempotent: ``sky.down`` on an already-terminated cluster raises, and
    that exception is swallowed so it never masks the test's own result.
    A genuine survivor still fails loudly via the final ``RuntimeError``.

    Args:
        cluster_name: SkyPilot cluster name to tear down.
        tunnel: SSH tunnel handle to kill first, or ``None`` if already
            killed (or never opened).

    Raises:
        RuntimeError: An EC2 instance tagged with ``cluster_name`` is
            still alive (not ``shutting-down``/``terminated``) after both
            teardown attempts.
    """
    if tunnel is not None:
        try:
            tunnel.kill()
        except Exception as exc:  # noqa: BLE001
            _log.warning("tunnel kill raised: %r", exc)
    try:
        sky.down(cluster_name, purge=True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("sky.down raised (expected when already gone): %r", exc)
    survivors = [s for s in _ec2_states(cluster_name) if s not in _DEAD_STATES]
    if survivors:
        raise RuntimeError(
            f"cluster {cluster_name!r} survived teardown with states {survivors!r} "
            f"— destroy it by hand in {_REGION}"
        )


def test_skypilot_cluster_dies_without_its_client() -> None:
    """A 5-minute deadline kills the instance with no client involvement.

    A bug this catches: the arming step never reaching the instance, the
    watchdog dying with the setup shell, or stage 1 firing with no stage 2
    — each of which leaves a cluster billing indefinitely once the CLI is
    gone.
    """
    cluster_name = f"kinoforge-wd-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(clouds=["aws"], region=_REGION, retry_until_up=True)
    tunnel: Any = None
    try:
        offers = provider.find_offers(
            HardwareRequirements(min_vram_gb=0, min_cuda="0.0")
        )
        assert offers, "no CPU offer surfaced from find_offers"
        spec = InstanceSpec(
            run_id=cluster_name,
            image="",
            env={},
            tags={"smoke": "skypilot-watchdog"},
            lifecycle=Lifecycle(idle_timeout_s=600, max_lifetime_s=_DEADLINE_S),
            offer=offers[0],
            provision_script="",
            # Never terminates -> the cluster can never go idle. This is the
            # exact server-mode shape that makes autostop inert (F1).
            run_cmd=["sleep", "3600"],
        )
        _log.info(
            "launching %s in %s with a %.0fs deadline",
            cluster_name,
            _REGION,
            _DEADLINE_S,
        )
        launched_at = time.time()
        provider.create_instance(spec)

        # The client is now gone: kill the tunnel, never call destroy.
        tunnel = provider._tunnels.pop(cluster_name, None)  # noqa: SLF001
        if tunnel is not None:
            tunnel.kill()
            tunnel = None

        deadline = time.time() + _KILL_TIMEOUT_S
        states: list[str] = []
        while time.time() < deadline:
            states = _ec2_states(cluster_name)
            _log.info("t+%.0fs ec2 states=%r", time.time() - launched_at, states)
            if states and all(s in _DEAD_STATES for s in states):
                break
            if not states:
                break  # already reaped and de-registered
            time.sleep(_POLL_INTERVAL_S)

        elapsed = time.time() - launched_at
        assert states == [] or all(s in _DEAD_STATES for s in states), (
            f"instance still alive {elapsed:.0f}s after launch: states={states!r}"
        )
        _log.info(
            "SMOKE RESULT cluster=%s wall_clock_to_termination=%.0fs states=%r",
            cluster_name,
            elapsed,
            states,
        )
    finally:
        _teardown(cluster_name, tunnel)
