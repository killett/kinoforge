"""RunPod-specific lifecycle helpers shared across smoke tiers."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

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


def destroy_all_active_pods(*, tag_filter: str | None = None) -> list[str]:
    """Belt-and-suspenders sweep. Returns IDs of pods successfully destroyed.

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
        IDs of pods that were destroyed without raising. Pods that
        raised during destroy are logged + omitted (the sweep does
        not abort on first failure).
    """
    destroyed: list[str] = []
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
    except Exception as exc:  # noqa: BLE001
        _log.warning("destroy_all_active_pods: sweep aborted: %r", exc)
    return destroyed


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
