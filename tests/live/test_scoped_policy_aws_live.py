"""Live proof that a real SkyPilot launch survives the scoped AWS policy.

This is the claim `.aws/policies/README.md` could not make until now. That
file already proves the policy's *logic* — 15 actions simulate `allowed`,
concrete-ARN probes come back `implicitDeny` outside the intended scope — and
says so at length. Simulation cannot see an API call SkyPilot makes that
nobody thought to list, which is exactly the failure mode the UNVALIDATED
banner was warning about: an opaque mid-launch denial, and a tired operator
whose shortest path out is `AdministratorAccess`.

So this test attaches the rendered policy to a throwaway principal that holds
**nothing else**, and launches the cheapest CPU box AWS sells in us-west-2
under it.

Seven claims, each able to fail on its own, and each pinned against a
different lie:

1. **principal isolation** — the probe user's attached-policy list is exactly
   the probe policy, with no inline policies and no groups. Without this, a
   future "just attach ReadOnlyAccess to get past it" turns a green run into
   a run that proves nothing.
2. **credential isolation** — `sts:GetCallerIdentity`, executed inside the
   launch subprocess's own environment, returns the probe user. pixi's
   `[activation.env]` exports `AWS_SHARED_CREDENTIALS_FILE` pointing at the
   workspace's real `kinoforge-ci` credential; inheriting it would run the
   whole launch as an admin and report a triumphant false green. The
   subprocess environment is therefore built from an allow-list, never
   inherited.
3. **server isolation** — no SkyPilot API server is alive when the launch
   starts. sky's server is a SEPARATE, long-lived process that holds whatever
   environment it was born with (this workspace had one running continuously
   since 2026-08-27). A surviving server would serve the launch under the
   ambient credential and make claim 2 a decoration.
4. **the launch succeeds** — subprocess rc 0.
5. **no denial anywhere in the log** — rc 0 is not enough on its own. sky has
   fallback paths that swallow a denial and continue, so a policy gap can
   hide behind a zero exit code.
6. **the box was real** — EC2, queried under the AMBIENT identity (a
   different principal from the one under test), reports a tagged instance.
   An empty answer FAILS: "we never saw a box" is not evidence that the
   launch worked.
7. **negative control** — the same launch under a principal holding NO policy
   is denied. Without this, claims 4-6 could be green because of some
   account-wide grant that has nothing to do with the file under test.

Teardown is convergent and runs in `finally`: sky down under the probe
credential, force-terminate by tag under the ambient one, then delete the key,
the user and the policy. It is verified after the fact, never from a mid-run
log line — the project has paid for that lesson twice.

Cost: c6i.large is ~$0.085/hr and the box lives for the length of one `echo`.
Budget a few cents per attempt, and expect several attempts: surfacing the
denials one at a time IS the deliverable.

Run::

    KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest \\
        tests/live/test_scoped_policy_aws_live.py -v -s
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
import pytest

from kinoforge.core.credential_patterns import redact_string

_log = logging.getLogger(__name__)

pytestmark = pytest.mark.live

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGION = "us-west-2"

#: Written unconditionally — a failed run's evidence is the point of the
#: exercise, not a consolation prize.
_EVIDENCE_PATH = Path("tests/live/_scoped_policy_aws_live_evidence.json")

#: Names for the throwaway artifacts. Suffixed per run so a leaked artifact
#: from an earlier attempt can never be silently reused as this run's
#: "isolated" principal.
_RUN_ID = secrets.token_hex(4)
_USER_NAME = f"kinoforge-scope-live-{_RUN_ID}"
_CONTROL_USER_NAME = f"kinoforge-scope-ctl-{_RUN_ID}"
_POLICY_NAME = f"KinoforgeSkyPilotMinimalLive{_RUN_ID}"
_CLUSTER = f"kf-scope-{_RUN_ID}"
_CONTROL_CLUSTER = f"kf-scope-ctl-{_RUN_ID}"

#: The renderer needs a bucket prefix; `kinoforge` is what `.aws/README.md`
#: documents, and it only affects which S3 ARNs the policy names.
_BUCKET_PREFIX = "kinoforge"

#: How long a freshly-minted access key may take to become usable, and how
#: long an attached policy may take to take effect. IAM is eventually
#: consistent; both are polled rather than slept through.
_IAM_PROPAGATION_TIMEOUT_S = 180.0
_IAM_POLL_INTERVAL_S = 5.0

#: Ceiling on the launch itself. A c6i.large provision plus sky's runtime
#: setup is ~5 min; this is a hang detector, not an expected duration.
_LAUNCH_TIMEOUT_S = 1500.0
#: The negative control should fail in well under a minute — it never reaches
#: provisioning. A long timeout here would just delay a fast, cheap answer.
_CONTROL_TIMEOUT_S = 420.0

_TEARDOWN_SETTLE_S = 900.0
_TEARDOWN_PASS_INTERVAL_S = 20.0
_TEARDOWN_CLEAN_PASSES = 2
_DEAD_STATES = {"shutting-down", "terminated"}

#: Bounds the captured log that reaches the committed evidence file.
_LOG_MAX_CHARS = 20000

#: The evidence file is TRACKED, and `tests/test_cloud_identifier_scrub.py`
#: refuses a concrete AWS account id in any tracked file. Every ARN this test
#: records — the probe user, the probe policy, `sts:GetCallerIdentity` — embeds
#: one, so the account id is substituted out on the way to disk. Caught by that
#: guard on the first run, which is the guard working: redacting credentials
#: was never the whole job.
_ACCOUNT_SENTINEL = "<AWS_ACCOUNT>"
_ACCOUNT_ID_RE = re.compile(r"\b\d{12}\b")

#: Three spellings of the same event. AWS is not consistent about which one a
#: given service emits, and matching only one would let the others through.
_DENIAL_RE = re.compile(
    r"(AccessDenied\w*|UnauthorizedOperation|"
    r"not authorized to perform[:\s]+\S+|"
    r"You are not authorized to perform this operation)",
)
#: The action name inside a denial, when AWS names one. This is the payload
#: the README's denial log is built from.
_DENIED_ACTION_RE = re.compile(
    r"not authorized to perform[:\s]+([A-Za-z0-9]+:[A-Za-z0-9_*]+)"
)

#: A permission failure that never reaches AWS in a form `_DENIAL_RE` can see.
#: SkyPilot catches some denials and re-raises them in its own words: the
#: 2026-09-04 negative control died on ``RuntimeError: Failed to retrieve AWS
#: regions. Please ensure that the `ec2:DescribeRegions` action is enabled for
#: your AWS account in IAM.`` — which names the exact missing permission while
#: containing none of AWS's three denial spellings.
#:
#: Kept SEPARATE from `_DENIAL_RE` rather than merged into it, and used only
#: by the negative control. The positive test must keep asserting on the
#: narrow AWS strings: a sky-worded permission complaint there would be a
#: finding, and folding this pattern in would let one through.
_SKY_PERMISSION_FAILURE_RE = re.compile(
    r"(action is enabled for your AWS account in IAM|"
    r"Failed to retrieve AWS regions|"
    r"credentials are not set up|Cloud access is not set up)",
)


class Ec2QueryFailed(RuntimeError):
    """An EC2 read could not be completed — state UNKNOWN, never 'clean'."""


@dataclass
class ProbePrincipal:
    """A throwaway IAM user, its key, and whatever policy it holds."""

    user_name: str
    user_arn: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    policy_arn: str | None = None


def _now_local() -> str:
    """Return an ISO-8601 timestamp in the machine's LOCAL timezone.

    Returns:
        Local-time ISO-8601 string, per the project's local-timezone rule.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _skip_reason() -> str | None:
    """Return why this module cannot run live, or ``None`` if it can.

    Returns:
        A human-readable skip reason, or ``None`` when every precondition
        (the opt-in flag, plus the aws and sky CLIs on PATH) holds. boto3 is
        not checked: ``tests/live/conftest.py`` imports it unconditionally,
        so collection has already failed if it is missing.
    """
    if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
        return "KINOFORGE_LIVE_TESTS != 1"
    for binary in ("aws", "sky"):
        if shutil.which(binary) is None:
            return f"{binary} not on PATH (use `pixi run -e live-skypilot`)"
    return None


_SKIP = _skip_reason()

#: Accumulates across both tests in this module and is written out by the
#: session-scoped fixture below, so a crash mid-run still leaves a record.
_EVIDENCE: dict[str, Any] = {
    "run_id": _RUN_ID,
    "started_at": _now_local(),
    "region": _REGION,
    "claims": {},
}


@pytest.fixture(scope="module", autouse=True)
def _evidence_writer() -> Iterator[None]:
    """Write the evidence file after the module's tests finish, always.

    MERGES into whatever is already on disk rather than replacing it. The two
    tests here cost different amounts — the positive one books an EC2
    instance, the control books nothing — so re-running the cheap one alone
    is the normal way to iterate. A plain overwrite would silently delete the
    expensive claim's record every time that happens.

    Each claim carries the run id that produced it, so a merged file never
    implies that two claims came from the same run when they did not.

    Yields:
        None. The write happens on the way out, on success or failure.
    """
    try:
        yield
    finally:
        merged: dict[str, Any] = {}
        if _EVIDENCE_PATH.exists():
            try:
                merged = json.loads(_EVIDENCE_PATH.read_text())
            except json.JSONDecodeError:
                _log.warning("existing evidence file did not parse — replacing it")
                merged = {}
        claims = dict(merged.get("claims", {}))
        for name, claim in _EVIDENCE["claims"].items():
            claims[name] = {**claim, "run_id": _RUN_ID, "recorded_at": _now_local()}
        # Rebuilt rather than updated in place: a per-file "run_id" left over
        # from an earlier write would name the wrong run for whichever claims
        # this pass did not touch.
        merged = {
            "claims": claims,
            "last_run_id": _RUN_ID,
            "last_started_at": _EVIDENCE["started_at"],
            "last_finished_at": _now_local(),
            "region": _REGION,
        }
        serialized = json.dumps(merged, indent=2, sort_keys=True)
        # Substituted on the serialized text rather than field by field: the
        # account id turns up inside ARNs, inside `sts:GetCallerIdentity`'s
        # bare `Account`, and inside log tails, and a per-field scrubber would
        # have to be kept in step with every one of those.
        serialized = _ACCOUNT_ID_RE.sub(_ACCOUNT_SENTINEL, serialized)
        _EVIDENCE_PATH.write_text(serialized + "\n")
        _log.info("evidence written to %s", _EVIDENCE_PATH)


# ---------------------------------------------------------------------------
# Rendering and principal lifecycle (all under the AMBIENT credential).
# ---------------------------------------------------------------------------


def _render_policy(out_dir: Path) -> Path:
    """Render the tracked template to a concrete policy document.

    Uses the documented renderer rather than substituting placeholders here,
    so what gets attached is what `.aws/README.md` tells an operator to
    attach. The renderer refuses to write inside the repository, which is why
    *out_dir* is a temp dir.

    Args:
        out_dir: Directory to write the rendered document into.

    Returns:
        Path to the rendered JSON.

    Raises:
        RuntimeError: The renderer exited non-zero.
    """
    out = out_dir / "skypilot-minimal.rendered.json"
    completed = subprocess.run(
        [
            "python",
            str(_REPO_ROOT / "tools" / "render_aws_policy.py"),
            "--bucket-prefix",
            _BUCKET_PREFIX,
            "--out",
            str(out),
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"render_aws_policy.py rc={completed.returncode}: "
            f"{redact_string(completed.stderr)[:500]}"
        )
    return out


def _compact_len(path: Path) -> int:
    """Return the policy's size the way IAM counts it — whitespace excluded.

    Args:
        path: Rendered policy document.

    Returns:
        Character count of the compact JSON encoding.
    """
    return len(json.dumps(json.loads(path.read_text()), separators=(",", ":")))


@contextmanager
def _probe_principal(
    *, user_name: str, policy_document: Path | None
) -> Iterator[ProbePrincipal]:
    """Create a throwaway IAM user holding at most one policy, then delete it.

    Args:
        user_name: Name for the throwaway user.
        policy_document: Rendered policy to create and attach, or ``None``
            for the negative control (a user with no permissions at all).

    Yields:
        The created principal, including a usable access key.
    """
    iam = boto3.client("iam")
    policy_arn: str | None = None
    access_key_id: str | None = None
    try:
        user_arn = str(iam.create_user(UserName=user_name)["User"]["Arn"])
        _log.info("created probe user %s", user_name)
        if policy_document is not None:
            policy_arn = str(
                iam.create_policy(
                    PolicyName=_POLICY_NAME,
                    PolicyDocument=policy_document.read_text(),
                    Description="kinoforge scoped-policy live validation — throwaway",
                )["Policy"]["Arn"]
            )
            iam.attach_user_policy(UserName=user_name, PolicyArn=policy_arn)
            _log.info("attached %s to %s", policy_arn, user_name)
        key = iam.create_access_key(UserName=user_name)["AccessKey"]
        access_key_id = str(key["AccessKeyId"])
        yield ProbePrincipal(
            user_name=user_name,
            user_arn=user_arn,
            access_key_id=access_key_id,
            secret_access_key=str(key["SecretAccessKey"]),
            policy_arn=policy_arn,
        )
    finally:
        # Order matters: the key has to go before the user, and the policy
        # cannot be deleted while it is attached to anything.
        if access_key_id is not None:
            _swallow(
                iam.delete_access_key, UserName=user_name, AccessKeyId=access_key_id
            )
        if policy_arn is not None:
            _swallow(iam.detach_user_policy, UserName=user_name, PolicyArn=policy_arn)
        _swallow(iam.delete_user, UserName=user_name)
        if policy_arn is not None:
            _swallow(iam.delete_policy, PolicyArn=policy_arn)
        _log.info("probe principal %s cleaned up", user_name)


def _swallow(fn: Any, **kwargs: Any) -> None:  # noqa: ANN401
    """Call *fn*, logging and discarding any error.

    Used only in teardown, where one failing cleanup step must not prevent
    the remaining ones from running. Every failure is logged loudly — the
    final IAM sweep at the end of the test is what actually asserts the
    account is clean.

    Args:
        fn: The boto3 call to make.
        **kwargs: Its keyword arguments.
    """
    try:
        fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 — teardown must be total
        _log.warning("cleanup call %s failed: %r", getattr(fn, "__name__", fn), exc)


def _grants_of(user_name: str) -> dict[str, list[str]]:
    """Return every permission source bound to *user_name*.

    Args:
        user_name: IAM user to inspect.

    Returns:
        ``{"attached": [...arns], "inline": [...names], "groups": [...names]}``.
    """
    iam = boto3.client("iam")
    return {
        "attached": [
            str(p["PolicyArn"])
            for p in iam.list_attached_user_policies(UserName=user_name)[
                "AttachedPolicies"
            ]
        ],
        "inline": [
            str(n) for n in iam.list_user_policies(UserName=user_name)["PolicyNames"]
        ],
        "groups": [
            str(g["GroupName"])
            for g in iam.list_groups_for_user(UserName=user_name)["Groups"]
        ],
    }


# ---------------------------------------------------------------------------
# The isolated launch environment.
# ---------------------------------------------------------------------------


def _isolated_env(principal: ProbePrincipal, home: Path) -> dict[str, str]:
    """Build the launch subprocess's environment from an ALLOW-LIST.

    Deliberately not ``os.environ.copy()`` with a few keys removed. pixi's
    ``[feature.live-skypilot.activation]`` exports
    ``AWS_SHARED_CREDENTIALS_FILE``, ``AWS_CONFIG_FILE`` and
    ``AWS_PROFILE``-adjacent variables that all resolve to the workspace's
    real ``kinoforge-ci`` credential. A subtractive list silently stops
    protecting the moment pixi adds another one; an allow-list does not.

    Args:
        principal: The probe principal whose key the subprocess must use.
        home: Isolated HOME, so ``~/.sky`` and ``~/.aws`` are this run's own.

    Returns:
        The complete environment for the subprocess.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USER": os.environ.get("USER", "kinoforge"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        "AWS_ACCESS_KEY_ID": principal.access_key_id,
        "AWS_SECRET_ACCESS_KEY": principal.secret_access_key,
        "AWS_DEFAULT_REGION": _REGION,
        "AWS_REGION": _REGION,
        # Nothing about this run should reach SkyPilot's usage telemetry.
        "SKYPILOT_DISABLE_USAGE_COLLECTION": "1",
    }


def _caller_identity_in(env: dict[str, str]) -> dict[str, str]:
    """Return `sts:GetCallerIdentity` as seen from inside *env*.

    Args:
        env: The subprocess environment to probe.

    Returns:
        The parsed identity document, or ``{"error": ...}`` when the call
        could not be made — reported, never silently treated as a match.
    """
    completed = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        return {"error": redact_string(completed.stderr.strip())[:400]}
    try:
        return {str(k): str(v) for k, v in json.loads(completed.stdout).items()}
    except json.JSONDecodeError as exc:
        return {"error": f"unparseable: {exc!r}"}


def _await_usable_key(principal: ProbePrincipal, env: dict[str, str]) -> dict[str, Any]:
    """Poll until the freshly-minted key resolves to the probe user.

    A brand-new access key is not usable instantly, and neither is a
    just-attached policy. Polling the real call is the only honest way to
    know both have landed; a fixed sleep either wastes time or races.

    Args:
        principal: The principal whose ARN the identity must match.
        env: The isolated environment carrying that principal's key.

    Returns:
        ``{"identity", "waited_s", "matched"}`` — ``matched`` False means the
        key never resolved, which the caller reports rather than retries away.
    """
    started = time.time()
    identity: dict[str, str] = {}
    while time.time() - started < _IAM_PROPAGATION_TIMEOUT_S:
        identity = _caller_identity_in(env)
        if identity.get("Arn") == principal.user_arn:
            return {
                "identity": identity,
                "waited_s": round(time.time() - started, 1),
                "matched": True,
            }
        time.sleep(_IAM_POLL_INTERVAL_S)
    return {
        "identity": identity,
        "waited_s": round(time.time() - started, 1),
        "matched": False,
    }


def _sky_server_pids() -> list[int]:
    """Return PIDs of any running SkyPilot API server.

    Read from the process table rather than by asking sky, because the
    question is precisely whether a server this run does not control is
    alive. `sky api status` would answer from the client's own config.

    Returns:
        Matching PIDs; empty means none are running.
    """
    completed = subprocess.run(
        ["ps", "-eo", "pid,args"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    pids: list[int] = []
    for line in completed.stdout.splitlines():
        if "sky.server.server" not in line:
            continue
        head = line.strip().split(None, 1)[0]
        if head.isdigit():
            pids.append(int(head))
    return pids


def _stop_sky_server() -> dict[str, Any]:
    """Stop any SkyPilot API server and confirm the process is gone.

    Returns:
        ``{"pids_before", "pids_after", "stopped"}``.
    """
    before = _sky_server_pids()
    subprocess.run(
        ["sky", "api", "stop"], capture_output=True, text=True, timeout=180, check=False
    )
    deadline = time.time() + 60
    after = _sky_server_pids()
    while after and time.time() < deadline:
        time.sleep(2)
        after = _sky_server_pids()
    return {"pids_before": before, "pids_after": after, "stopped": not after}


def _launch(
    principal: ProbePrincipal, home: Path, cluster: str, *, timeout_s: float
) -> dict[str, Any]:
    """Run one `sky launch` under *principal* and capture everything it said.

    The task is a bare ``echo`` on the smallest CPU SKU: the claim under test
    is about IAM, and a heavier workload would only add cost and cold-pull
    time to every iteration. ``--down`` makes sky tear the cluster down when
    the job ends, which is belt-and-braces on top of this module's teardown.

    Args:
        principal: Principal whose credential the launch must use.
        home: Isolated HOME for sky state.
        cluster: SkyPilot cluster name.
        timeout_s: Hard ceiling on the subprocess.

    Returns:
        A record of the attempt: rc, elapsed, redacted log tail, the denial
        strings found and the action names inside them.
    """
    task_path = home / "probe-task.yaml"
    task_path.write_text(
        "resources:\n"
        "  cloud: aws\n"
        f"  region: {_REGION}\n"
        "  cpus: 2+\n"
        "  disk_size: 50\n"
        "\n"
        "run: |\n"
        "  echo kinoforge-scope-probe-ok\n"
    )
    env = _isolated_env(principal, home)
    started = time.time()
    try:
        completed = subprocess.run(
            ["sky", "launch", "-c", cluster, "-y", "--down", str(task_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        rc = completed.returncode
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        rc = None
        output = (
            (exc.stdout or b"").decode(errors="replace")
            + "\n"
            + ((exc.stderr or b"").decode(errors="replace"))
        )
        timed_out = True

    redacted = redact_string(output)
    denials = sorted(set(_DENIAL_RE.findall(redacted)))
    actions = sorted(set(_DENIED_ACTION_RE.findall(redacted)))
    sky_permission_failures = sorted(set(_SKY_PERMISSION_FAILURE_RE.findall(redacted)))
    return {
        "cluster": cluster,
        "returncode": rc,
        "timed_out": timed_out,
        "elapsed_s": round(time.time() - started, 1),
        "denials": denials,
        "denied_actions": actions,
        "sky_permission_failures": sky_permission_failures,
        "log_tail": redacted[-_LOG_MAX_CHARS:],
    }


# ---------------------------------------------------------------------------
# Teardown — ambient credential, convergent, verified after the fact.
# ---------------------------------------------------------------------------


def _ec2_query(cluster: str, query: str) -> list[Any]:
    """Run one tag-filtered ``describe-instances`` projection, ambient creds.

    Mirrors the S1 smoke's helper, including the trailing wildcard: SkyPilot
    tags instances ``ray-cluster-name = <cluster>-<8 hex>``, not the bare
    name.

    Args:
        cluster: SkyPilot cluster name.
        query: JMESPath projection over ``Reservations[].Instances[]``.

    Returns:
        The parsed list. Empty means the query ran and found nothing; it
        never doubles as an error signal.

    Raises:
        Ec2QueryFailed: The read could not be completed.
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
                f"Name=tag:ray-cluster-name,Values={cluster}*",
                "--query",
                query,
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise Ec2QueryFailed(f"describe-instances could not run: {exc!r}") from exc
    if completed.returncode != 0:
        raise Ec2QueryFailed(
            f"describe-instances rc={completed.returncode}: {completed.stderr.strip()[:300]}"
        )
    try:
        return list(json.loads(completed.stdout or "[]"))
    except json.JSONDecodeError as exc:
        raise Ec2QueryFailed(
            f"describe-instances stdout did not parse: {exc!r}"
        ) from exc


def _ec2_states(cluster: str) -> list[str]:
    """Return EC2 instance states tagged with *cluster*.

    Args:
        cluster: SkyPilot cluster name.

    Returns:
        ``State.Name`` strings; empty means none found.

    Raises:
        Ec2QueryFailed: Propagated from :func:`_ec2_query`.
    """
    return [
        str(s) for s in _ec2_query(cluster, "Reservations[].Instances[].State.Name")
    ]


def _force_terminate(cluster: str) -> list[str]:
    """Terminate every instance tagged with *cluster*, under ambient creds.

    Deliberately NOT run under the probe credential: teardown must not
    depend on the artifact under test. If the scoped policy turns out to be
    missing ``ec2:TerminateInstances``, that is a finding to record — not a
    reason for a live box to survive the run.

    Args:
        cluster: SkyPilot cluster name.

    Returns:
        The instance ids a terminate was issued for.

    Raises:
        Ec2QueryFailed: The id lookup could not be read.
    """
    ids = [str(i) for i in _ec2_query(cluster, "Reservations[].Instances[].InstanceId")]
    if ids:
        _log.warning("force-terminating %r for %s", ids, cluster)
        subprocess.run(
            [
                "aws",
                "ec2",
                "terminate-instances",
                "--region",
                _REGION,
                "--instance-ids",
                *ids,
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    return ids


def _teardown(principal: ProbePrincipal, home: Path, cluster: str) -> dict[str, Any]:
    """Tear the cluster down convergently and prove it is gone.

    One pass is not enough and the project has the receipts: `sky.launch`
    runs inside an API server that outlives this process, so a single "is
    anything running?" read can come back clean while the server is still
    seconds away from creating the instance.

    Args:
        principal: Probe principal, used for the polite ``sky down``.
        home: Isolated HOME holding this run's sky state.
        cluster: Cluster to remove.

    Returns:
        A record of every pass, for the evidence file.

    Raises:
        RuntimeError: Something is still alive after the settle window, or
            EC2 could not be read (unknown is treated as alive).
    """
    record: dict[str, Any] = {"passes": [], "started_at": _now_local()}
    env = _isolated_env(principal, home)
    subprocess.run(
        ["sky", "down", "-y", cluster],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    deadline = time.time() + _TEARDOWN_SETTLE_S
    clean_streak = 0
    while time.time() < deadline:
        entry: dict[str, Any] = {"at": _now_local()}
        readable = True
        alive: list[str] = []
        try:
            entry["forced"] = _force_terminate(cluster)
            states = _ec2_states(cluster)
            entry["states"] = states
            alive = [s for s in states if s not in _DEAD_STATES]
        except Ec2QueryFailed as exc:
            readable = False
            entry["states"] = None
            entry["error"] = str(exc)
        entry["readable"] = readable
        record["passes"].append(entry)
        if readable and not alive:
            clean_streak += 1
            if clean_streak >= _TEARDOWN_CLEAN_PASSES:
                break
        else:
            clean_streak = 0
        time.sleep(_TEARDOWN_PASS_INTERVAL_S)

    try:
        final = _ec2_states(cluster)
        final_readable = True
    except Ec2QueryFailed as exc:
        final, final_readable = [], False
        record["final_error"] = str(exc)
    survivors = [s for s in final if s not in _DEAD_STATES]
    record["final_states"] = final if final_readable else None
    record["final_readable"] = final_readable
    record["finished_at"] = _now_local()
    if survivors or not final_readable:
        detail = (
            f"states {survivors!r}"
            if final_readable
            else "EC2 UNREADABLE — treat as live"
        )
        raise RuntimeError(
            f"cluster {cluster!r} survived teardown ({detail}) — kill it by hand"
        )
    return record


def _account_is_clean() -> dict[str, Any]:
    """Return whether this run's IAM artifacts are gone from the account.

    Returns:
        ``{"users_present", "policies_present", "clean"}``.
    """
    iam = boto3.client("iam")
    users: list[str] = []
    for name in (_USER_NAME, _CONTROL_USER_NAME):
        try:
            iam.get_user(UserName=name)
            users.append(name)
        except iam.exceptions.NoSuchEntityException:
            pass
    policies = [
        str(p["PolicyName"])
        for p in iam.list_policies(Scope="Local", MaxItems=1000)["Policies"]
        if str(p["PolicyName"]) == _POLICY_NAME
    ]
    return {
        "users_present": users,
        "policies_present": policies,
        "clean": not users and not policies,
    }


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")
def test_negative_control_bare_principal_is_denied() -> None:
    """A principal with NO policy cannot launch — so the policy is load-bearing.

    Runs first and costs nothing: it never reaches provisioning. Without it,
    a green result from the real test could just mean the account grants
    everything to everyone, and the file under test would be decorative.
    """
    record: dict[str, Any] = {}
    with _probe_principal(
        user_name=_CONTROL_USER_NAME, policy_document=None
    ) as principal:
        home = Path(tempfile.mkdtemp(prefix="kf-scope-ctl-"))
        try:
            record["identity_wait"] = _await_usable_key(
                principal, _isolated_env(principal, home)
            )
            record["sky_server"] = _stop_sky_server()
            record["launch"] = _launch(
                principal, home, _CONTROL_CLUSTER, timeout_s=_CONTROL_TIMEOUT_S
            )
            try:
                record["observed_instances"] = _ec2_query(
                    _CONTROL_CLUSTER, "Reservations[].Instances[].InstanceId"
                )
                record["observed_readable"] = True
            except Ec2QueryFailed as exc:
                record["observed_instances"] = None
                record["observed_readable"] = False
                record["observed_error"] = str(exc)
        finally:
            record["teardown"] = _teardown(principal, home, _CONTROL_CLUSTER)
            shutil.rmtree(home, ignore_errors=True)
            _EVIDENCE["claims"]["negative_control"] = record

    launch = record["launch"]
    assert launch["returncode"] != 0, (
        "a principal holding NO policy launched successfully — the scoped "
        "policy is not what is gating these launches, and this whole "
        f"validation proves nothing. rc={launch['returncode']}"
    )
    # Independent of anything sky printed: EC2, read under the AMBIENT
    # identity, must show the bare principal booked nothing. A non-zero exit
    # from a process that had already created an instance is a very
    # different — and much more expensive — result than the one claimed here.
    assert record["observed_readable"], (
        f"EC2 unreadable, so the control is unconfirmed: {record.get('observed_error')!r}"
    )
    assert record["observed_instances"] == [], (
        "the bare principal exited non-zero but left EC2 instances behind: "
        f"{record['observed_instances']!r}"
    )
    # And the failure has to be ABOUT permissions. A network blip also exits
    # non-zero and would satisfy both assertions above while proving nothing.
    # sky does not always relay AWS's own wording — the 2026-09-04 run died on
    # sky's own "Failed to retrieve AWS regions … ec2:DescribeRegions" — so
    # either spelling counts, but one of them must be there.
    assert launch["denials"] or launch["sky_permission_failures"], (
        "the bare principal failed, but for no permission-shaped reason. "
        "That is a different failure from the one this control observes.\n"
        f"tail:\n{launch['log_tail'][-2000:]}"
    )


@pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")
def test_scoped_policy_survives_a_real_cpu_launch() -> None:
    """The rendered scoped policy — and nothing else — carries a real launch.

    The claim `.aws/policies/README.md` has never been able to make. Every
    assertion below is checked after teardown has run, so a failure can
    never leave a billing instance behind while pytest unwinds.
    """
    render_dir = Path(tempfile.mkdtemp(prefix="kf-scope-render-"))
    record: dict[str, Any] = {}
    try:
        policy_path = _render_policy(render_dir)
        record["policy_compact_chars"] = _compact_len(policy_path)
        record["policy_sids"] = [
            str(s.get("Sid", "<unnamed>"))
            for s in json.loads(policy_path.read_text())["Statement"]
        ]

        with _probe_principal(
            user_name=_USER_NAME, policy_document=policy_path
        ) as principal:
            home = Path(tempfile.mkdtemp(prefix="kf-scope-home-"))
            try:
                record["grants"] = _grants_of(principal.user_name)
                record["expected_policy_arn"] = principal.policy_arn
                env = _isolated_env(principal, home)
                record["identity_wait"] = _await_usable_key(principal, env)
                record["sky_server"] = _stop_sky_server()
                record["launch"] = _launch(
                    principal, home, _CLUSTER, timeout_s=_LAUNCH_TIMEOUT_S
                )
                try:
                    record["observed_instances"] = _ec2_query(
                        _CLUSTER,
                        "Reservations[].Instances[].[InstanceId,InstanceType,State.Name]",
                    )
                    record["observed_readable"] = True
                except Ec2QueryFailed as exc:
                    record["observed_instances"] = None
                    record["observed_readable"] = False
                    record["observed_error"] = str(exc)
            finally:
                record["teardown"] = _teardown(principal, home, _CLUSTER)
                shutil.rmtree(home, ignore_errors=True)
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)
        record["account_clean"] = _account_is_clean()
        _EVIDENCE["claims"]["scoped_launch"] = record

    grants = record["grants"]
    assert grants["attached"] == [record["expected_policy_arn"]], (
        "the probe principal holds attached policies other than the one "
        f"under test: {grants['attached']!r}"
    )
    assert grants["inline"] == [], (
        f"probe principal has inline policies: {grants['inline']!r}"
    )
    assert grants["groups"] == [], f"probe principal is in groups: {grants['groups']!r}"

    identity = record["identity_wait"]
    assert identity["matched"], (
        "the launch environment never resolved to the probe principal "
        f"({identity['identity']!r}) — anything it did was done as someone else"
    )

    assert record["sky_server"]["stopped"], (
        "a SkyPilot API server survived `sky api stop` "
        f"(pids {record['sky_server']['pids_after']!r}); it would have served "
        "this launch under the ambient credential"
    )

    launch = record["launch"]
    assert launch["denials"] == [], (
        "the launch hit AWS denials under the scoped policy:\n"
        f"  denials: {launch['denials']}\n"
        f"  actions: {launch['denied_actions']}\n"
        f"{launch['log_tail'][-3000:]}"
    )
    assert launch["returncode"] == 0, (
        f"sky launch failed under the scoped policy (rc={launch['returncode']}, "
        f"timed_out={launch['timed_out']}):\n{launch['log_tail'][-3000:]}"
    )

    assert record["observed_readable"], (
        "EC2 could not be read, so the launch is unconfirmed: "
        f"{record.get('observed_error')!r}"
    )
    assert record["observed_instances"], (
        "sky reported success but EC2 shows no instance tagged for this "
        "cluster — a green exit code over a launch that never booked "
        "anything is exactly the vacuous pass this assertion exists to catch"
    )

    assert record["account_clean"]["clean"], (
        f"throwaway IAM artifacts survived: {record['account_clean']!r}"
    )
