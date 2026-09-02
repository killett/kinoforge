"""Opt-in live smoke: a SkyPilot cluster dies without its client.

Launches the cheapest AWS CPU SKU in us-west-2 with a 15-minute deadline and
a run command that sleeps far past it, then drops every in-process handle —
no destroy call is ever made. Pass condition: EC2 itself reports the instance
``shutting-down``/``terminated`` (or, for a stage-2-only rescue,
``stopping``/``stopped`` — see ``_HALTED_STATES``). ``sky status`` is
deliberately NOT the oracle: the cluster vanishing from sky's local state
would prove nothing about the money.

Deadline rationale (900 s, not the workstream's usual few-minute smoke
window): the watchdog must prove the FULL claim end-to-end, not just that it
fires. At ~900 s the cluster has finished provisioning and reached RUNNING,
``sleep 3600`` has started as PID 1, the client handle has already been
dropped (tunnel killed, no destroy call) — and only THEN does the on-instance
deadline watchdog fire. A shorter deadline risks firing mid-provision, which
would prove nothing about a client-abandoned *running* cluster, the actual
risk this workstream exists to close. ``retry_until_up`` is deliberately
NOT passed: it would let ``sky.launch`` retry across a capacity miss,
re-arming the on-instance deadline on each attempt while this test's
Python-side wall clock keeps counting from the first attempt — the two
clocks would drift apart. A capacity miss here is routed into the EC2 poll
(see ``create_failed`` below) rather than silently retried.

Gated by (module-level skip if any is missing):
  - ``KINOFORGE_LIVE_TESTS=1``
  - AWS credentials reachable by boto3/awscli
  - the ``aws`` CLI binary on PATH (the EC2-oracle queries shell out to it)
  - ``import sky`` succeeds (use ``pixi run -e live-skypilot``)

Cost: < $0.05 (cheapest CPU SKU ~$0.01/hr, <= ~31 min wall-clock — the
worst-case ``_KILL_TIMEOUT_S`` if the smoke lands in the stage-2-only path).
Design: docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md
"""

from __future__ import annotations

import json
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

if shutil.which("aws") is None:
    # The EC2 oracle (_ec2_states) and teardown both shell out to the aws
    # CLI directly (not boto3) — a present boto3 credential chain does not
    # guarantee the binary is on PATH, and without it every oracle query
    # would fail with a confusing FileNotFoundError instead of a clean skip.
    _REASONS.append("aws CLI binary not found on PATH")

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
    InstanceSpec,
    Launch,
    Lifecycle,
    Placement,
)
from kinoforge.core.lifecycle import Ledger  # noqa: E402
from kinoforge.providers.skypilot import SkyPilotProvider  # noqa: E402
from kinoforge.stores.local import LocalArtifactStore  # noqa: E402

_log = logging.getLogger(__name__)

_REGION = "us-west-2"
_DEADLINE_S = 900.0
#: Deadline + skylet tick (<=60 s) + stage-2 grace (600 s, see
#: ``watchdog.RENDER_WATCHDOG``'s ``grace_before_halt_s`` default — raised
#: from 120 s after the 2026-08-15 live run showed the autodown teardown
#: taking longer than that end to end) + generous slack.
_KILL_TIMEOUT_S = _DEADLINE_S + 60.0 + 600.0 + 300.0  # 1860 s (31 min)
_POLL_INTERVAL_S = 30.0
#: Fully torn down — compute AND disk gone (or on the way).
_DEAD_STATES = {"shutting-down", "terminated"}
#: Stage-2 rescue landed a HALT (``sudo shutdown -h now``) rather than a
#: terminate. AWS's default ``InstanceInitiatedShutdownBehavior`` is "stop",
#: so an in-instance ``shutdown -h`` yields ``stopping``/``stopped``, not
#: ``terminated``. Compute billing stops here; the EBS volume does not —
#: this is a distinct, loggable outcome, not a silent pass or fail.
_HALTED_STATES = {"stopping", "stopped"}
#: A snapshot in one of these states proves the instance genuinely existed —
#: required before an empty/dead snapshot may be trusted as "was reaped"
#: rather than "never came up" (wrong region, tag-key drift, expired creds).
_LIVE_STATES = {"pending", "running"}

#: A single `aws ec2 describe-instances` call has been observed to fail
#: transiently mid-run (2026-08-15 run 2: rc=255, a malformed/truncated XML
#: response from the EC2 API, ~13 minutes into a 15-minute-deadline poll
#: loop). Retrying a handful of times absorbs that kind of blip without
#: masking a genuine, persistent failure (bad region, expired creds), which
#: still exhausts the budget and raises within roughly a minute.
_EC2_QUERY_RETRIES = 3
_EC2_QUERY_RETRY_BACKOFF_S = 5.0

#: After `sky.down` returns, the EC2 instance can still be caught mid
#: state-transition (``running`` -> ``shutting-down`` -> ``terminated``) for
#: a few seconds. Poll instead of single-shot re-checking so that window
#: isn't misread as a survivor (2026-08-15 run 2: the immediate re-check
#: caught the instance still ``running`` a moment before it reached
#: ``terminated``).
_TEARDOWN_POLL_TIMEOUT_S = 180.0
_TEARDOWN_POLL_INTERVAL_S = 15.0

#: Default kinoforge state dir (matches the CLI's ``--state-dir`` default),
#: so a ledger row this test leaves behind is exactly where
#: ``kinoforge list`` / ``kinoforge forget`` already look for it.
_STATE_DIR = Path(".kinoforge")


def _ec2_states(cluster_name: str) -> list[str]:
    """Return EC2 instance states tagged with this sky cluster name.

    Args:
        cluster_name: SkyPilot cluster name, matched against the
            ``ray-cluster-name`` tag SkyPilot writes onto every EC2
            instance it provisions.

    Returns:
        List of EC2 ``State.Name`` strings for matching instances. Empty
        means AWS reports zero instances tagged with this cluster name.

    Raises:
        RuntimeError: The ``aws`` CLI call itself failed on every one of
            ``_EC2_QUERY_RETRIES`` attempts (non-zero exit — wrong region,
            expired creds, throttling, ... — or output that fails to parse
            as JSON). Raising here instead of returning ``[]`` matters: a
            swallowed failure is indistinguishable from "genuinely no
            instances", which would let a live instance queried under
            broken credentials read as "already reaped" — a false PASS
            with the meter still running. A single transient failure (a
            malformed/truncated API response, observed live 2026-08-15) is
            NOT raised immediately — see ``_EC2_QUERY_RETRIES`` — so a
            one-off blip can't kill an otherwise-healthy 15-minute run.
    """
    # SkyPilot tags instances `ray-cluster-name = <cluster_name>-<8 hex>`,
    # NOT the bare cluster name (observed live 2026-08-15:
    # `kinoforge-wd-13b6aaeb-a01d6e69` for cluster `kinoforge-wd-13b6aaeb`).
    # An exact-match filter therefore NEVER matches and every poll silently
    # returns an empty list, driving the test straight to the "unknown"
    # verdict after burning the entire poll budget with no instance ever
    # observed. The trailing `*` wildcard (verified against the live API)
    # matches the suffixed tag while still being specific to this run's
    # cluster name.
    args = [
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
    ]
    last_error = ""
    last_rc = 0
    for attempt in range(1, _EC2_QUERY_RETRIES + 1):
        # `capture_output=True` gives separate `.stdout` / `.stderr` pipes
        # (no merging) — the JSON we parse below comes from `.stdout` only,
        # so any `DEBUG - ...` logging the aws CLI writes to `.stderr` on a
        # failure (confirmed at awscli/logger.py: `set_stream_logger`
        # defaults its handler to `sys.stderr`) can never land inside the
        # parsed payload.
        completed = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if completed.returncode != 0:
            last_rc = completed.returncode
            last_error = completed.stderr.strip()
            _log.warning(
                "aws ec2 describe-instances attempt %d/%d failed (rc=%d): %s",
                attempt,
                _EC2_QUERY_RETRIES,
                last_rc,
                last_error,
            )
        else:
            try:
                return list(json.loads(completed.stdout or "[]"))
            except json.JSONDecodeError as exc:
                last_rc = completed.returncode
                last_error = f"non-JSON stdout ({exc}): {completed.stdout!r}"
                _log.warning(
                    "aws ec2 describe-instances attempt %d/%d returned unparseable "
                    "stdout: %s",
                    attempt,
                    _EC2_QUERY_RETRIES,
                    last_error,
                )
        if attempt < _EC2_QUERY_RETRIES:
            time.sleep(_EC2_QUERY_RETRY_BACKOFF_S)
    raise RuntimeError(
        f"aws ec2 describe-instances failed on all {_EC2_QUERY_RETRIES} attempts "
        f"(last rc={last_rc}): {last_error}"
    )


def _classify(states: list[str], *, observed_live: bool) -> str:
    """Classify one EC2 poll snapshot.

    Args:
        states: Current ``_ec2_states`` result.
        observed_live: Whether a prior snapshot in this same poll loop ever
            contained a ``pending``/``running`` state — the only evidence
            that the instance genuinely existed before it (maybe) vanished.

    Returns:
        ``"terminated"`` — all states dead, or the tag query is empty AND a
        live state was observed earlier (real reap, not a no-op query).
        ``"halted"`` — all states are a stage-2 HALT outcome.
        ``"unknown"`` — empty query with no prior live observation; this is
        NOT evidence of death, it is evidence of nothing.
        ``"alive"`` — anything else (still pending/running, or mixed).
    """
    if not states:
        return "terminated" if observed_live else "unknown"
    if all(s in _DEAD_STATES for s in states):
        return "terminated"
    if all(s in _HALTED_STATES for s in states):
        return "halted"
    return "alive"


def _is_terminal(states: list[str]) -> bool:
    """Return whether every tagged instance has reached a genuinely dead state.

    Empty ``states`` (no matching instance found at all) also counts as
    terminal — nothing left to poll for.

    Args:
        states: Current ``_ec2_states`` result.

    Returns:
        ``True`` when ``states`` is empty or every entry is in
        ``_DEAD_STATES``.
    """
    return not states or all(s in _DEAD_STATES for s in states)


def _teardown(cluster_name: str, tunnel: Any) -> None:
    """Teardown that must run whatever the assertions did.

    Idempotent: ``sky.down`` on an already-terminated cluster raises, and
    that exception is swallowed so it never masks the test's own result.
    A genuine survivor still fails loudly via the final ``RuntimeError`` —
    note that ``_ec2_states`` itself now raises (rather than returning
    ``[]``) on a broken query, so a failing describe-instances call can no
    longer be misread as a clean teardown either.

    ``sky.down`` returning does not mean the EC2 instance has finished its
    state transition. Two distinct in-progress shapes have been observed
    live, and neither is a survivor by itself:

    - ``running``/``pending`` for a few seconds right after ``sky.down``
      returns (observed live 2026-08-15 run 2: an immediate re-check read a
      genuinely-tearing-down instance as a survivor).
    - ``stopping``/``stopped`` when the smoke's own poll loop landed the
      stage-2-only halt outcome (a local ``shutdown -h now``, not a
      terminate — see ``_HALTED_STATES``): from there ``sky.down`` still has
      to drive the instance through ``shutting-down`` -> ``terminated``, so
      those states are ALSO still-in-progress, not an immediate survivor
      (observed live 2026-08-15 run 3: the poll used to stop the moment
      ``states`` left ``_LIVE_STATES``, i.e. the instant it saw
      ``stopping``, and raised ``RuntimeError("... survived teardown with
      states ['stopping']")`` even though ``sky.down`` had been issued and
      the instance terminated moments later).

    So the survivor check polls for up to ``_TEARDOWN_POLL_TIMEOUT_S``,
    waiting out any transient non-terminal snapshot (``_LIVE_STATES`` OR
    ``_HALTED_STATES``) via :func:`_is_terminal`, before deciding. The
    window still raises loudly if it expires with the instance in any
    non-terminal state — a stuck ``stopping`` is a real survivor, just not
    an instant one.

    Args:
        cluster_name: SkyPilot cluster name to tear down.
        tunnel: SSH tunnel handle to kill first, or ``None`` if already
            killed (or never opened).

    Raises:
        RuntimeError: An EC2 instance tagged with ``cluster_name`` is still
            alive or merely halted (not ``shutting-down``/``terminated``)
            after both teardown attempts and the poll window above — a
            stopped-but-not-terminated instance still bills its EBS volume,
            so it counts as a survivor here even though the main test's
            poll loop treats "halted" as a legitimate stage-2 outcome.
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

    states = _ec2_states(cluster_name)
    poll_deadline = time.time() + _TEARDOWN_POLL_TIMEOUT_S
    while not _is_terminal(states) and time.time() < poll_deadline:
        _log.info(
            "teardown poll: cluster=%s still transitioning states=%r — "
            "waiting up to %.0fs more",
            cluster_name,
            states,
            poll_deadline - time.time(),
        )
        time.sleep(_TEARDOWN_POLL_INTERVAL_S)
        states = _ec2_states(cluster_name)

    survivors = [s for s in states if s not in _DEAD_STATES]
    if survivors:
        raise RuntimeError(
            f"cluster {cluster_name!r} survived teardown with states {survivors!r} "
            f"— destroy it by hand in {_REGION}"
        )


def test_skypilot_cluster_dies_without_its_client() -> None:
    """A 15-minute deadline kills the instance with no client involvement.

    A bug this catches: the arming step never reaching the instance, the
    watchdog dying with the setup shell, or stage 1 firing with no stage 2
    — each of which leaves a cluster billing indefinitely once the CLI is
    gone.
    """
    cluster_name = f"kinoforge-wd-{secrets.token_hex(4)}"
    # Breadcrumb for a hard process kill mid-poll that skips the `finally`
    # entirely (e.g. SIGKILL): the cluster name lands in pytest's captured
    # stdout even without `-s`, so it can still be found and destroyed by
    # hand instead of existing only inside a variable no one can read.
    print(cluster_name, flush=True)

    provider = SkyPilotProvider(clouds=["aws"], region=_REGION)
    # F12 — durable provisional row BEFORE the multi-minute sky.launch, so a
    # process death during provisioning still leaves something `kinoforge
    # list` / `kinoforge forget` can find and name. Rooted at the CLI's
    # default state dir so it is discoverable without special flags.
    provider.set_launch_ledger(Ledger(store=LocalArtifactStore(_STATE_DIR)))

    tunnel: Any = None
    try:
        # compute-seam S4: a CPU placement selects no accelerator, which is
        # the decision the old "one synthetic CPU offer" assertion stood in
        # for. The spec below carries that placement, so the provider makes
        # the same call at launch.
        cpu_placement = Placement(min_vram_gb=0, min_cuda="0.0")
        assert provider._select_accelerator(cpu_placement) is None  # noqa: SLF001
        spec = InstanceSpec(
            placement=cpu_placement,
            run_id=cluster_name,
            image="",
            env={},
            tags={"smoke": "skypilot-watchdog"},
            lifecycle=Lifecycle(idle_timeout_s=600, max_lifetime_s=_DEADLINE_S),
            # Never terminates -> the cluster can never go idle. This is the
            # exact server-mode shape that makes autostop inert (F1).
            launch=Launch(("sleep", "3600")),
        )
        _log.info(
            "launching %s in %s with a %.0fs deadline",
            cluster_name,
            _REGION,
            _DEADLINE_S,
        )
        launched_at = time.time()

        create_failed: Exception | None = None
        try:
            provider.create_instance(spec)
        except Exception as exc:  # noqa: BLE001
            # Don't abort: a cluster that came up and then died mid-create
            # (e.g. the RPC timed out after the instance was already
            # running) must still be verified via EC2, not silently lost.
            create_failed = exc
            _log.warning(
                "create_instance raised %r for cluster=%s — falling through "
                "to the EC2 poll instead of aborting",
                exc,
                cluster_name,
            )
        else:
            # The client is now gone: kill the tunnel, never call destroy.
            tunnel = provider._tunnels.pop(cluster_name, None)  # noqa: SLF001
            assert tunnel is not None, (
                f"no tunnel found for {cluster_name!r} in provider._tunnels "
                "after a successful create_instance — cannot prove the "
                "client actually disconnected, which is the exact property "
                "this smoke exists to prove"
            )
            tunnel.kill()
            tunnel = None

        deadline = time.time() + _KILL_TIMEOUT_S
        states: list[str] = []
        observed_live = False
        elapsed = 0.0
        verdict = "alive"
        while time.time() < deadline:
            states = _ec2_states(cluster_name)
            elapsed = time.time() - launched_at
            if any(s in _LIVE_STATES for s in states):
                observed_live = True
            _log.info(
                "t+%.0fs ec2 states=%r observed_live=%s", elapsed, states, observed_live
            )
            if elapsed >= _DEADLINE_S:
                verdict = _classify(states, observed_live=observed_live)
                if verdict in {"terminated", "halted"}:
                    break
            time.sleep(_POLL_INTERVAL_S)
        else:
            elapsed = time.time() - launched_at
            verdict = _classify(states, observed_live=observed_live)

        if verdict == "unknown":
            reason = (
                f"create_instance raised {create_failed!r}; " if create_failed else ""
            )
            pytest.fail(
                f"{reason}never observed a live (pending/running) EC2 instance "
                f"for {cluster_name!r} after {elapsed:.0f}s — cannot distinguish "
                f"'terminated' from 'never launched'; last states={states!r}"
            )
        assert elapsed >= _DEADLINE_S, (
            f"verdict {verdict!r} reached at {elapsed:.0f}s, before the "
            f"{_DEADLINE_S:.0f}s deadline — too early to credit the watchdog"
        )
        assert verdict in {"terminated", "halted"}, (
            f"instance still alive {elapsed:.0f}s after launch: states={states!r}"
        )
        if verdict == "halted":
            _log.warning(
                "SMOKE RESULT (STAGE-2-ONLY HALT, not terminated) cluster=%s "
                "wall_clock=%.0fs states=%r — compute stopped billing, EBS "
                "volume may still exist; teardown will attempt full "
                "termination via sky.down",
                cluster_name,
                elapsed,
                states,
            )
        else:
            _log.info(
                "SMOKE RESULT cluster=%s wall_clock_to_termination=%.0fs states=%r",
                cluster_name,
                elapsed,
                states,
            )
    finally:
        _teardown(cluster_name, tunnel)
