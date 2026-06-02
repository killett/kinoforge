"""SkyPilotProvider — multi-cloud GPU provisioning via the SkyPilot SDK.

The ``sky`` module is imported *lazily* inside :func:`_get_sky` and is NEVER
imported at module load time.  This keeps the provider importable in environments
where SkyPilot is not installed (e.g. CI, RunPod-only deployments).

Injectable client seam
-----------------------
Pass ``sky_client=<fake>`` to the constructor to replace every ``sky.*`` call
with a test double.  The interface expected of ``sky_client`` is:

.. code-block:: python

    class SkyClientProtocol(Protocol):
        def gpu_list(self) -> list[dict]: ...
        def launch(self, task_config: Any, **kwargs: Any) -> dict: ...
        def status(self) -> list[dict]: ...
        def down(self, cluster_id: str) -> None: ...

When ``sky_client is None`` the real path is taken: every method calls
:func:`_get_sky` on-demand to obtain the real ``sky`` module.

Autostop trade-off
------------------
SkyPilotProvider maps ``spec.lifecycle.idle_timeout_s`` to SkyPilot's native
``autostop`` parameter (converted from seconds to minutes).  This delegates
cluster termination to SkyPilot's built-in auto-stop mechanism, providing
multi-cloud reach but **cannot** replicate the fine-grained in-pod self-terminator
model used by RunPodProvider (no dead-man heartbeat, no job-in-flight awareness).
That is the cost of inheriting SkyPilot's cloud-portability: the timer model is
provider-owned, not kinoforge-owned.

Self-registers under ``"skypilot"`` when this module is imported.
"""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kinoforge.core import registry
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.offers import filter_offers

if TYPE_CHECKING:
    pass  # No runtime-conditional imports needed here

# ---------------------------------------------------------------------------
# Lazy-import helper (the ONLY place sky may be referenced in kinoforge)
# ---------------------------------------------------------------------------


def _get_sky() -> Any:  # noqa: ANN401
    """Lazily import the real ``sky`` SDK (never at module load).

    Returns:
        The ``sky`` module.

    Raises:
        KinoforgeError: If skypilot is not installed in this environment.
    """
    try:
        import sky  # type: ignore[import-not-found]  # noqa: I001
    except ImportError as exc:
        raise KinoforgeError(
            "skypilot is not installed; install via `pixi add --pypi skypilot`"
        ) from exc
    return sky


# ---------------------------------------------------------------------------
# Injectable client Protocol (for type-checker + documentation)
# ---------------------------------------------------------------------------


@runtime_checkable
class _SkyClientProtocol(Protocol):
    """Structural interface expected of the injected ``sky_client``."""

    def gpu_list(self) -> list[dict[str, Any]]:
        """Return a list of available GPU dicts."""
        ...

    def launch(self, task_config: Any, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        """Launch a SkyPilot cluster and return a result dict."""
        ...

    def status(self) -> list[dict[str, Any]]:
        """Return a list of cluster status dicts."""
        ...

    def down(self, cluster_id: str) -> None:
        """Tear down the named cluster."""
        ...


# ---------------------------------------------------------------------------
# Status conversion
# ---------------------------------------------------------------------------

_SKY_STATUS_MAP: dict[str, str] = {
    "UP": "ready",
    "INIT": "starting",
    "STOPPED": "stopped",
    "TERMINATED": "terminated",
    "TERMINATING": "stopped",
}


def _sky_status_to_kinoforge(sky_status: str) -> str:
    """Map a SkyPilot cluster status string to a kinoforge status string.

    Args:
        sky_status: A SkyPilot status value (e.g. ``"UP"``, ``"INIT"``).

    Returns:
        One of ``"starting"``, ``"ready"``, ``"stopped"``, ``"terminated"``.
    """
    return _SKY_STATUS_MAP.get(sky_status.upper(), "starting")


def _cluster_dict_to_instance(cluster: dict[str, Any]) -> Instance:
    """Convert a SkyPilot status-dict entry to a kinoforge Instance.

    Args:
        cluster: A dict with at least ``name`` and ``status`` keys.

    Returns:
        An :class:`~kinoforge.core.interfaces.Instance`.
    """
    cluster_name: str = str(cluster.get("name", ""))
    sky_status: str = str(cluster.get("status", ""))
    return Instance(
        id=cluster_name,
        provider="skypilot",
        status=_sky_status_to_kinoforge(sky_status),
        created_at=0.0,  # SkyPilot status() does not reliably return creation time
    )


# ---------------------------------------------------------------------------
# SkyPilotProvider
# ---------------------------------------------------------------------------


class SkyPilotProvider(ComputeProvider):
    """ComputeProvider backed by the SkyPilot multi-cloud SDK.

    The ``sky`` module is imported lazily — only when a method actually needs it
    and no ``sky_client`` has been injected.  Inject ``sky_client=<fake>`` to run
    without SkyPilot installed (all tests use this path).

    ``idle_timeout_s`` is mapped to SkyPilot's ``autostop`` (in minutes).  See
    module docstring for the trade-off versus RunPodProvider's in-pod self-terminator.

    Args:
        sky_client: Optional injectable sky-SDK stub.  When ``None``, every
            method calls :func:`_get_sky` to obtain the real ``sky`` module.
        sleep: Callable invoked between destroy-poll iterations.
            Defaults to :func:`time.sleep`.

    Example:
        >>> from kinoforge.providers.skypilot import SkyPilotProvider
        >>> p = SkyPilotProvider()
        >>> p.name
        'skypilot'
    """

    name: str = "skypilot"

    def __init__(
        self,
        sky_client: _SkyClientProtocol | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the provider.

        Args:
            sky_client: Injectable sky SDK client; when ``None`` the real
                ``sky`` module is used (lazily imported on demand).
            sleep: Injectable sleep used between destroy-poll iterations.
        """
        self._sky_client = sky_client
        self._sleep = sleep

    # ------------------------------------------------------------------
    # Private helper — resolve sky seam
    # ------------------------------------------------------------------

    def _sky(self) -> Any:  # noqa: ANN401
        """Return the injected client or the lazily-imported real sky module.

        Returns:
            The sky client: either the injected test double or the real
            ``sky`` module obtained via :func:`_get_sky`.
        """
        if self._sky_client is not None:
            return self._sky_client
        return _get_sky()

    # ------------------------------------------------------------------
    # ComputeProvider interface
    # ------------------------------------------------------------------

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return SkyPilot GPU offers matching ``reqs``.

        Calls ``sky_client.gpu_list()`` to obtain available GPU types, converts
        each entry to an :class:`~kinoforge.core.interfaces.Offer`, then
        delegates filtering and sorting to
        :func:`~kinoforge.core.offers.filter_offers`.

        Args:
            reqs: Hardware requirements to filter against.

        Returns:
            Filtered and sorted list of :class:`~kinoforge.core.interfaces.Offer`
            objects.
        """
        sky = self._sky()
        gpu_entries: list[dict[str, Any]] = sky.gpu_list()
        raw_offers: list[Offer] = []
        for gpu in gpu_entries:
            gpu_name: str = str(gpu.get("name", ""))
            vram_gb: int = int(gpu.get("vram_gb", 0))
            cuda: str = str(gpu.get("cuda", "12.0"))
            cost: float = float(gpu.get("cost_rate_usd_per_hr", 0.0))
            raw_offers.append(
                Offer(
                    id=gpu_name,
                    gpu_type=gpu_name,
                    vram_gb=vram_gb,
                    cuda=cuda,
                    cost_rate_usd_per_hr=cost,
                    mode="pod",
                )
            )
        return filter_offers(raw_offers, reqs)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Launch a SkyPilot cluster from ``spec``.

        Maps ``spec.lifecycle.idle_timeout_s`` to SkyPilot's ``autostop``
        parameter (in whole minutes).

        Args:
            spec: Instance specification.

        Returns:
            An :class:`~kinoforge.core.interfaces.Instance` with
            ``status="starting"`` and ``provider="skypilot"``.
        """
        sky = self._sky()
        autostop_minutes: float = spec.lifecycle.idle_timeout_s / 60.0
        task_config: dict[str, Any] = {
            "image": spec.image,
            "run_id": spec.run_id,
            "env": dict(spec.env),
            "tags": dict(spec.tags),
        }
        # NEW — Layer Q
        if spec.provision_script is not None:
            task_config["setup"] = spec.provision_script
        if spec.run_cmd is not None:
            task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)
        result: dict[str, Any] = sky.launch(task_config, autostop=autostop_minutes)
        cluster_name: str = str(
            result.get("cluster_name", spec.run_id or "skypilot-cluster")
        )
        return Instance(
            id=cluster_name,
            provider=self.name,
            status="starting",
            created_at=time.time(),
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=(
                spec.offer.cost_rate_usd_per_hr if spec.offer else 0.0
            ),
        )

    def get_instance(self, instance_id: str) -> Instance:
        """Return the cluster matching ``instance_id`` from ``sky_client.status()``.

        Args:
            instance_id: The SkyPilot cluster name to look up.

        Returns:
            The matching :class:`~kinoforge.core.interfaces.Instance`.

        Raises:
            KeyError: No cluster named ``instance_id`` is found in the status list.
        """
        sky = self._sky()
        clusters: list[dict[str, Any]] = sky.status()
        for cluster in clusters:
            if cluster.get("name") == instance_id:
                return _cluster_dict_to_instance(cluster)
        raise KeyError(f"no SkyPilot cluster found: {instance_id!r}")

    def list_instances(self) -> list[Instance]:
        """Return all active SkyPilot clusters.

        Calls ``sky_client.status()`` and converts each entry to an
        :class:`~kinoforge.core.interfaces.Instance`.

        Returns:
            A (possibly empty) list of :class:`~kinoforge.core.interfaces.Instance`.
        """
        sky = self._sky()
        clusters: list[dict[str, Any]] = sky.status()
        return [_cluster_dict_to_instance(c) for c in clusters]

    def stop_instance(self, instance_id: str) -> None:
        """No-op for SkyPilot: use destroy_instance or rely on autostop.

        SkyPilot's autostop handles idle termination.  There is no separate
        'pause billing' primitive without tearing down the cluster.

        Args:
            instance_id: Unused.
        """
        # SkyPilot clusters are either UP or torn down; no intermediate pause.

    def destroy_instance(self, instance_id: str) -> None:
        """Tear down a SkyPilot cluster and poll until it is confirmed gone.

        Calls ``sky_client.down(instance_id)`` once, then polls
        ``sky_client.status()`` until the cluster no longer appears.
        Idempotent: if the cluster is already absent the call returns immediately.

        Args:
            instance_id: The SkyPilot cluster name to destroy.
        """
        sky = self._sky()
        sky.down(instance_id)
        # Poll until the cluster disappears from the status listing.
        while True:
            clusters: list[dict[str, Any]] = sky.status()
            names = {str(c.get("name", "")) for c in clusters}
            if instance_id not in names:
                return  # confirmed gone
            self._sleep(3.0)

    def heartbeat(self, instance_id: str) -> None:
        """No-op: SkyPilot manages cluster liveness via autostop.

        Args:
            instance_id: Unused.
        """
        # Autostop is set at launch time; no heartbeat mechanism is needed.

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the SSH endpoint for ``instance``.

        Args:
            instance: The cluster whose endpoint to return.

        Returns:
            ``{"ssh": "ssh://<instance.id>"}``
        """
        return {"ssh": f"ssh://{instance.id}"}


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

registry.register_provider("skypilot", lambda: SkyPilotProvider())
