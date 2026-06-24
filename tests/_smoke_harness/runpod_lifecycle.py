"""RunPod-specific lifecycle helpers shared across smoke tiers."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: Repo root resolved relative to this file: ``tests/_smoke_harness/`` →
#: parents[2] is the workspace root.
_REPO_DEFAULT: Path = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SweepResult:
    """Outcome of a :func:`destroy_all_active_pods` sweep.

    Pre-2026-06-24 the helper returned ``list[str]`` (IDs that left
    cleanly) and *silently* dropped per-pod exceptions to
    ``_log.warning(...)``.  pytest's default capture suppresses
    everything below ERROR, so a failed destroy was invisible to the
    caller — the load-bearing channel gap behind the
    destroy-on-teardown money leak.

    The ``failures`` channel surfaces per-pod exceptions to the smoke
    harness so the fixture's teardown path can either retry, escalate
    via a subprocess fallback, or fail the test loudly.

    ``__contains__`` is delegated to :attr:`destroyed` so existing
    call sites that do ``if pod_id in result:`` keep working without
    edits — a pod that raised is NOT considered destroyed for that
    membership check.
    """

    destroyed: list[str] = field(default_factory=list)
    failures: dict[str, BaseException] = field(default_factory=dict)

    def __contains__(self, item: object) -> bool:
        return item in self.destroyed

    def __iter__(self) -> Any:
        return iter(self.destroyed)


_PROXY_URL_PATTERN = "https://{pod_id}-{port}.proxy.runpod.net"


def resolve_proxy_url(pod_id: str, *, port: int = 8000) -> str:
    """Return the RunPod pod-proxy URL for ``port`` on ``pod_id``.

    Provider's ``endpoints()`` returned an empty port map immediately
    after ``kinoforge generate`` completed during T22 attempt 1 — the
    provider does not re-hydrate ``tags['ports']`` after the post-job
    ledger refresh. Constructing the URL directly side-steps the issue.
    """
    return _PROXY_URL_PATTERN.format(pod_id=pod_id, port=port)


def _get_runpod_provider() -> Any:
    """Test-seam — overridden in unit tests.

    Loads `.env` first because pytest CLI doesn't auto-load it and the
    RunPod provider 403s without RUNPOD_API_KEY in os.environ.
    """
    from kinoforge.core import registry as kf_registry
    from kinoforge.core.dotenv_loader import load_env_file
    from kinoforge.providers import runpod  # noqa: F401

    load_env_file()
    return kf_registry.get_provider("runpod")()


def destroy_all_active_pods(*, tag_filter: str | None = None) -> SweepResult:
    """Belt-and-suspenders sweep.

    Defends against the T22 attempt-2 failure mode: a smoke that crashes
    mid-cold-boot before its in-test ``pod_id`` variable is captured
    can leave a $1.39/hr A100 idle. Calling this in ``finally``
    catches every pod the orchestrator created during the smoke
    (it records BEFORE wait_for_ready).

    Args:
        tag_filter: When set, only pods whose
            ``tags.get("smoke_tier")`` equals ``tag_filter`` are
            destroyed. ``None`` reaps every active pod the provider
            knows about — appropriate when the smoke owns the workspace
            exclusively.

    Returns:
        :class:`SweepResult` whose ``destroyed`` list carries the IDs
        of pods that left cleanly and whose ``failures`` dict maps
        every pod_id whose destroy raised to the actual exception.
        Callers MUST inspect ``failures`` — pre-2026-06-24 the helper
        log-warned and dropped failures silently, which was the
        load-bearing channel gap behind the destroy-on-teardown money
        leak.
    """
    destroyed: list[str] = []
    failures: dict[str, BaseException] = {}
    try:
        provider = _get_runpod_provider()
        for inst in provider.list_instances():
            if tag_filter is not None and inst.tags.get("smoke_tier") != tag_filter:
                continue
            try:
                provider.destroy_instance(inst.id)
                destroyed.append(inst.id)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "destroy_all_active_pods: failed to destroy %s: %r",
                    inst.id,
                    exc,
                )
                failures[inst.id] = exc
    except Exception as exc:  # noqa: BLE001
        _log.warning("destroy_all_active_pods: sweep aborted: %r", exc)
        # No per-pod attribution available — surface under sentinel key
        # so the caller can still tell something went wrong.
        failures["<sweep>"] = exc
    return SweepResult(destroyed=destroyed, failures=failures)


def _kinoforge_destroy_subprocess(
    pod_id: str, repo_root: Path
) -> subprocess.CompletedProcess[str]:
    """Invoke ``pixi run kinoforge destroy --id <pod_id>`` as a subprocess.

    Separated from :func:`teardown_pod_or_raise` so tests can
    monkeypatch the subprocess call without faking subprocess
    machinery wholesale.
    """
    return subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def teardown_pod_or_raise(
    pod_id: str | None,
    *,
    repo_root: Path | None = None,
) -> None:
    """One-call sweep + targeted fallback + post-condition probe.

    The replacement for the silent ``destroy_all_active_pods()`` +
    silent ``subprocess.run(..., check=False)`` chain that allowed
    the 2026-06-23 destroy-on-teardown money leak.

    Sequence:
      1. Run :func:`destroy_all_active_pods` to clean up everything.
      2. If ``pod_id`` is supplied AND not in the sweep's destroyed
         list, fall back to ``pixi run kinoforge destroy --id`` and
         surface the subprocess output via :func:`print` (pytest
         captures it on test failure).
      3. Probe ``provider.get_instance(pod_id)``.  If the pod is
         still alive (or in a non-terminal status), raise
         :class:`AssertionError` with the full breadcrumb embedded
         so the operator sees the cause in the pytest output.

    Args:
        pod_id: The pod id the smoke owns.  ``None`` is acceptable
            when the smoke crashed before capturing the id; the
            sweep still runs and ``failures`` are surfaced but no
            post-condition probe fires.
        repo_root: Working directory for the ``pixi run`` subprocess.
            Defaults to the workspace root resolved from this module's
            location.

    Raises:
        AssertionError: ``pod_id`` is still visible to the provider
            after the sweep + fallback.
    """
    repo = repo_root if repo_root is not None else _REPO_DEFAULT
    result = destroy_all_active_pods()

    fallback: subprocess.CompletedProcess[str] | None = None
    if pod_id is not None and pod_id not in result.destroyed:
        fallback = _kinoforge_destroy_subprocess(pod_id, repo)
        # Surface output via stdout so pytest captures it on test
        # failure — the silent `subprocess.run(..., check=False)` of
        # the pre-fix path is the load-bearing bug we're closing.
        print(f"[teardown] kinoforge destroy --id {pod_id}: exit={fallback.returncode}")
        if fallback.stdout:
            print(f"[teardown stdout]\n{fallback.stdout}")
        if fallback.stderr:
            print(f"[teardown stderr]\n{fallback.stderr}", file=sys.stderr)

    if pod_id is None:
        # Nothing to probe.  Surface any sweep failures so callers
        # still see they happened even without a pod_id anchor.
        if result.failures:
            _log.warning(
                "teardown_pod_or_raise: sweep failures (no pod_id anchor): %r",
                {k: repr(v) for k, v in result.failures.items()},
            )
        return

    provider = _get_runpod_provider()
    try:
        inst = provider.get_instance(pod_id)
    except KeyError:
        return  # pod truly gone
    status = getattr(inst, "status", None)
    if status in {"stopped", "terminated"}:
        return

    msg_parts = [
        f"teardown failed: pod {pod_id!r} still alive (status={status!r})",
        f"sweep destroyed: {result.destroyed!r}",
        "sweep failures: " + repr({k: repr(v) for k, v in result.failures.items()}),
    ]
    if fallback is not None:
        msg_parts.append(
            f"fallback exit={fallback.returncode}; "
            f"stdout={fallback.stdout!r}; stderr={fallback.stderr!r}"
        )
    raise AssertionError("\n".join(msg_parts))


def _build_util_endpoint() -> Any:
    """Test-seam — overridden in unit tests.

    Loads `.env` first via kinoforge's loader because the PodStatPoller
    runs in a daemon thread spawned from pytest's main process, where
    `RUNPOD_API_KEY` may not yet be in os.environ when the test
    imports/runs (the pytest CLI doesn't auto-load `.env`).
    """
    from kinoforge.core.dotenv_loader import load_env_file
    from kinoforge.providers.runpod.util import RunPodGraphQLUtilEndpoint

    load_env_file()
    return RunPodGraphQLUtilEndpoint(api_key=os.environ["RUNPOD_API_KEY"])


class PodStatPoller(threading.Thread):
    """Background thread; logs GPU util + CPU + memory every interval.

    Per user-scope ``proactive-pod-stats`` memory: poll RunPod runtime
    every 60-90s during long smokes; surface GPU stalls + cost drift
    proactively without operator request.
    """

    def __init__(
        self, pod_id: str, log_path: Path, *, interval_s: float = 90.0
    ) -> None:
        super().__init__(daemon=True)
        self.pod_id = pod_id
        self.log_path = log_path
        self.interval_s = interval_s
        self._stop = threading.Event()

    def run(self) -> None:
        endpoint = _build_util_endpoint()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            while not self._stop.wait(self.interval_s):
                try:
                    snap = endpoint.read_util(self.pod_id)
                except Exception as exc:  # noqa: BLE001
                    f.write(f"[stat-poll] read_util raised {exc!r}\n")
                    f.flush()
                    continue
                if snap is None:
                    f.write("[stat-poll] runtime not yet visible\n")
                    f.flush()
                    continue
                f.write(
                    f"[stat-poll] gpu_util={snap.gpu_util_percent} "
                    f"cpu={snap.cpu_percent} mem={snap.memory_percent}\n"
                )
                f.flush()

    def stop(self) -> None:
        self._stop.set()
