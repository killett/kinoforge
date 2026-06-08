"""SkyPilotProvider — multi-cloud GPU provisioning via the SkyPilot SDK.

The ``sky`` module is imported *lazily* inside :func:`_get_sky` and is NEVER
imported at module load time.  This keeps the provider importable in environments
where SkyPilot is not installed (e.g. CI, RunPod-only deployments).

Modern SkyPilot async API
-------------------------
Modern SkyPilot's top-level callables (:func:`sky.status`, :func:`sky.launch`,
:func:`sky.down`, sometimes :func:`sky.list_accelerators`) return a
``RequestId`` (a :class:`str` subclass) that the caller must resolve via
:func:`sky.stream_and_get` (or :func:`sky.get`) to obtain the typed payload.
The :func:`_resolve` helper in this module does that resolution; it is a
no-op when the call already returned a non-string payload (test fakes
return lists/dicts directly).

Read-path records are typed objects: :class:`sky.schemas.api.responses.StatusResponse`
exposes attributes (``.name``, ``.status``, ``.handle``); accelerator
records are :class:`sky.catalog.common.InstanceTypeInfo` NamedTuples;
test fakes inject dicts. The :func:`_record_field` adapter reads a field
from either shape uniformly.

Injectable client seam
-----------------------
Pass ``sky_client=<fake>`` to the constructor to replace every ``sky.*`` call
with a test double.  The interface expected of ``sky_client`` is:

.. code-block:: python

    class SkyClientProtocol(Protocol):
        class Task:
            @staticmethod
            def from_yaml_config(config: dict[str, Any]) -> Any: ...
        def list_accelerators(self, **kwargs: Any) -> dict[str, list[Any]]: ...
        def launch(self, task: Any, **kwargs: Any) -> Any: ...
        def status(self, **kwargs: Any) -> list[Any]: ...
        def down(self, cluster_id: str) -> Any: ...
        def stream_and_get(self, request_id: str) -> Any: ...  # only if RequestId returned

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
        import sky  # type: ignore[import-not-found, unused-ignore]  # noqa: I001
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
    """Structural interface expected of the injected ``sky_client``.

    Modern real SkyPilot returns :class:`RequestId` (a :class:`str` subclass)
    from :func:`sky.status`, :func:`sky.launch`, and :func:`sky.down`; the
    caller must resolve via :func:`sky.stream_and_get`. Test fakes typically
    return the payload directly (a list / dict). :func:`_resolve` handles
    both shapes — it is a no-op when fed a non-string.
    """

    def list_accelerators(
        self,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Return a mapping of accelerator name → instance-type-info records.

        Real return is ``dict[str, list[InstanceTypeInfo]]``; offline fakes
        may return a flat ``list[dict]``. Both are flattened by
        :meth:`SkyPilotProvider.find_offers`.
        """
        ...

    def launch(
        self,
        task: Any,  # noqa: ANN401
        /,
        **kwargs: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        """Launch a SkyPilot cluster; may return a ``RequestId`` or direct payload."""
        ...

    def status(self, **kwargs: Any) -> Any:  # noqa: ANN401
        """Return cluster status; may be a ``RequestId`` or a direct list."""
        ...

    def down(self, cluster_id: str, /) -> Any:  # noqa: ANN401
        """Tear down the named cluster; may return a ``RequestId``."""
        ...


# ---------------------------------------------------------------------------
# Dual-shape helpers (typed-record / dict-fake adapter + RequestId resolver)
# ---------------------------------------------------------------------------


def _record_field(record: Any, field: str, default: str = "") -> str:  # noqa: ANN401
    """Read ``field`` from a SkyPilot cluster / accelerator record.

    Modern :func:`sky.status` returns typed records (attribute access on
    :class:`StatusResponse`); :func:`sky.list_accelerators` returns
    :class:`InstanceTypeInfo` NamedTuples (attribute access). Test fakes
    return dicts (``.get()`` access). This helper handles both shapes.

    Args:
        record: Either a typed SDK record or a dict (e.g. a test fake).
        field: Field name to read.
        default: Fallback when the field is absent.

    Returns:
        The field value coerced to :class:`str`; ``default`` if absent or
        explicitly ``None``.
    """
    if isinstance(record, dict):
        value = record.get(field, default)
    else:
        value = getattr(record, field, default)
    return str(value) if value is not None else default


def _resolve(sky_module: Any, result: Any) -> Any:  # noqa: ANN401
    """Resolve a SkyPilot ``RequestId`` to its payload, or return as-is.

    Modern SkyPilot returns :class:`RequestId` (a :class:`str` subclass)
    from top-level calls; :func:`sky.stream_and_get` blocks and returns
    the typed payload. Test fakes and legacy versions return the payload
    directly — :func:`isinstance(result, str)` branches between them
    (``RequestId`` is a ``str`` subclass; lists/dicts/typed records are
    not strings).

    Args:
        sky_module: The injected ``sky_client`` or imported ``sky`` module
            (must expose ``stream_and_get`` when a string result is observed).
        result: Direct return of a ``sky.*`` call.

    Returns:
        The resolved payload. If ``result`` is not a string, ``result`` is
        returned unchanged.
    """
    if isinstance(result, str):
        return sky_module.stream_and_get(result)
    return result


def _coerce_float_field(record: Any, field: str) -> float:  # noqa: ANN401
    """Read ``field`` from ``record`` and coerce to ``float`` (0.0 on failure).

    Args:
        record: A typed SDK record or a dict.
        field: Field name to read.

    Returns:
        The field value as a :class:`float`; ``0.0`` if the field is absent,
        ``None``, or unparseable.
    """
    raw = _record_field(record, field, default="")
    if not raw:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


# VRAM fallback for GPU types whose SkyPilot catalog entries omit ``device_memory``.
# GCP's InstanceTypeInfo returns ``device_memory=None`` for NVIDIA GPUs; these values
# come from NVIDIA's official spec sheets. Extend as new GPU types are qualified.
_KNOWN_GPU_VRAM_GB: dict[str, int] = {
    "T4": 16,
    "A100": 40,  # A100-40GB SXM / PCIe variant; A100-80GB has separate entry
    "A100-80GB": 80,
    "V100": 16,
    "P100": 16,
    "L4": 24,
    "L40": 48,
    "H100": 80,
    "H100-80GB": 80,
    "A10G": 24,
    "A10": 24,
}


def _coerce_vram_gb(record: Any) -> int:  # noqa: ANN401
    """Extract VRAM in GB from either offline-fake or real-SDK records.

    Offline fakes expose ``vram_gb`` as an int directly. Modern
    :class:`InstanceTypeInfo` exposes ``device_memory`` as a free-form
    string like ``"80GB"`` or ``"24 GiB"`` (per-device VRAM); the leading
    integer is extracted. When neither field is present, looks up the
    accelerator name in :data:`_KNOWN_GPU_VRAM_GB` (GCP catalogs frequently
    omit ``device_memory``). Returns ``0`` when no usable value is found.

    Args:
        record: A typed SDK record or a dict.

    Returns:
        VRAM in whole gibibytes / gigabytes. ``0`` when absent or
        unparseable.
    """
    # Offline-fake path: plain int field.
    raw_int = _record_field(record, "vram_gb", default="")
    if raw_int:
        try:
            return int(float(raw_int))
        except (TypeError, ValueError):
            pass
    # Real-SDK path: free-form ``device_memory`` string.
    raw_dm = _record_field(record, "device_memory", default="")
    if raw_dm:
        # Pull the leading numeric prefix (handles ``"80GB"``, ``"24 GiB"``).
        digits = ""
        for ch in raw_dm.strip():
            if ch.isdigit() or ch == ".":
                digits += ch
            else:
                break
        if digits:
            try:
                return int(float(digits))
            except (TypeError, ValueError):
                pass
    # Fallback: look up by accelerator name (GCP catalog omits device_memory).
    accel_name = _record_field(record, "accelerator_name") or _record_field(
        record, "name"
    )
    if accel_name and accel_name in _KNOWN_GPU_VRAM_GB:
        return _KNOWN_GPU_VRAM_GB[accel_name]
    return 0


def _collapse_status(raw: str) -> str:
    """Collapse a SkyPilot enum-string status to its bare member name.

    ``str(ClusterStatus.UP)`` yields ``'ClusterStatus.UP'``; the bare
    ``'UP'`` is what :func:`_sky_status_to_kinoforge` looks up. Plain
    strings (e.g. ``'UP'`` from test fakes) pass through unchanged.

    Args:
        raw: Raw status string, either ``'<Enum>.MEMBER'`` or just ``'MEMBER'``.

    Returns:
        The bare member name (everything after the last ``.``).
    """
    return raw.rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------
# Status conversion
# ---------------------------------------------------------------------------


def _strip_trailing_exec(script: str) -> str:
    """Strip a final line of the form `[<prefix> && ]exec <args>` from *script*.

    ``RenderedProvision.script`` (Layer Q) ends with an ``exec <run_cmd>`` line
    so the run process becomes PID 1 on RunPod's single-dockerArgs path. On
    SkyPilot, ``Task.setup`` must terminate so ``Task.run`` can start — the
    trailing ``exec`` would prevent that. This helper removes the last line if
    it contains an ``exec`` invocation; otherwise the script is returned
    unchanged.

    Args:
        script: The rendered provision script.

    Returns:
        Script with the trailing ``exec`` line removed (or unchanged if no
        ``exec`` is found on the last non-empty line).
    """
    lines = script.rstrip("\n").split("\n")
    if not lines:
        return script
    last = lines[-1]
    if " exec " in last or last.startswith("exec ") or " && exec " in last:
        return "\n".join(lines[:-1])
    return script


_SKY_STATUS_MAP: dict[str, str] = {
    "UP": "ready",
    "INIT": "starting",
    "PENDING": "starting",
    "STOPPED": "stopped",
    "AUTOSTOPPING": "stopped",
    "TERMINATED": "terminated",
    "TERMINATING": "stopped",
}


def _sky_status_to_kinoforge(sky_status: str) -> str:
    """Map a SkyPilot cluster status string to a kinoforge status string.

    Accepts both bare member names (e.g. ``"UP"`` from test fakes) and
    enum-string forms (e.g. ``"ClusterStatus.UP"`` from real
    :class:`ClusterStatus`). The latter is collapsed via
    :func:`_collapse_status` before lookup.

    Args:
        sky_status: A SkyPilot status value (e.g. ``"UP"``, ``"INIT"``,
            ``"ClusterStatus.UP"``).

    Returns:
        One of ``"starting"``, ``"ready"``, ``"stopped"``, ``"terminated"``.
        Unknown values default to ``"starting"``.
    """
    return _SKY_STATUS_MAP.get(_collapse_status(sky_status).upper(), "starting")


def _cluster_record_to_instance(cluster: Any) -> Instance:  # noqa: ANN401
    """Convert a SkyPilot cluster record (typed or dict) to a kinoforge Instance.

    Reads ``name`` and ``status`` via :func:`_record_field`, which handles
    both modern :class:`StatusResponse` (attribute access) and legacy /
    fake dict records (``.get()`` access).

    Args:
        cluster: A SkyPilot cluster record — either a typed
            :class:`StatusResponse`-like object or a ``dict[str, Any]``.

    Returns:
        An :class:`~kinoforge.core.interfaces.Instance`.
    """
    cluster_name: str = _record_field(cluster, "name")
    sky_status: str = _record_field(cluster, "status")
    return Instance(
        id=cluster_name,
        provider="skypilot",
        status=_sky_status_to_kinoforge(sky_status),
        created_at=0.0,  # SkyPilot status() does not reliably return creation time
    )


def _normalize_image_id(image: str) -> str:
    """Normalize an image reference for ``sky.Task.resources.image_id``.

    SkyPilot rejects bare image names (e.g. ``"alpine:3"``) unless a
    ``cloud`` is also specified, because the image registry is per-cloud.
    Docker-prefixed names (``"docker:..."``) are cloud-agnostic and pass
    validation without requiring a cloud setting. Cloud-registry-qualified
    names (containing a host like ``"gcr.io/..."`` or ``"public.ecr.aws/..."``)
    are also passed through unchanged.

    Args:
        image: Raw image reference as provided by the caller.

    Returns:
        A normalized image_id suitable for ``sky.Task.resources.image_id``.

    Examples:
        >>> _normalize_image_id("alpine:3")
        'docker:alpine:3'
        >>> _normalize_image_id("docker:alpine:3")
        'docker:alpine:3'
        >>> _normalize_image_id("gcr.io/my-proj/my-img")
        'gcr.io/my-proj/my-img'
    """
    if image.startswith("docker:"):
        return image
    # Heuristic for cloud-registry-qualified names: leading segment is a
    # registry host (contains a dot) AND the image is path-qualified
    # (contains a slash), e.g. ``gcr.io/proj/img`` or ``public.ecr.aws/foo/bar``.
    # Versioned bare names like ``python:3.12-slim`` also contain a dot but
    # have no slash, so they are correctly treated as bare Docker Hub images.
    if "/" in image:
        first_segment = image.split("/", 1)[0]
        if "." in first_segment:
            return image  # e.g. gcr.io/..., public.ecr.aws/...
    return f"docker:{image}"


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
        sky_client: Any | None = None,  # noqa: ANN401
        *,
        clouds: list[str] | None = None,
        region: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initialise the provider.

        Args:
            sky_client: Injectable sky SDK client; when ``None`` the real
                ``sky`` module is used (lazily imported on demand). Typed
                as :data:`~typing.Any` because the actual ``sky`` module
                exposes far more attributes than :class:`_SkyClientProtocol`
                documents, and test fakes (which return raw payloads
                rather than ``RequestId`` strings) intentionally diverge
                from the modern signature; :class:`_SkyClientProtocol`
                documents the *minimum* surface the provider relies on.
            clouds: Optional list of cloud names (e.g. ``["gcp"]``) passed
                to :func:`sky.list_accelerators` to restrict catalog
                enumeration. When ``None``, SkyPilot queries all registered
                clouds. Pass ``["gcp"]`` in GCP-only environments to avoid
                spurious Kubernetes/SSH catalog errors.
            region: Optional cloud region (e.g. ``"us-west1"``) pinned onto
                ``resources.region`` of every launched task. When ``None``,
                SkyPilot's optimizer picks a region by quota/availability —
                which can land in surprising zones (e.g. ``asia-southeast1``)
                when only one region has spot quota. Set this to keep
                launches in the operator's preferred region.
            sleep: Injectable sleep used between destroy-poll iterations.
        """
        self._sky_client = sky_client
        self._clouds = clouds
        self._region = region
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
        """Return SkyPilot offers matching ``reqs``.

        For CPU-class workloads (``reqs.min_vram_gb == 0``) the returned
        offer is a synthetic ``sky-cpu-auto`` entry — SkyPilot picks the
        actual SKU from ``cpus``/``memory`` constraints set on the
        :class:`sky.Task` at launch time, not from a discrete catalog.
        Calling :func:`sky.list_accelerators` with its default
        ``gpus_only=True`` returns no candidates for CPU workloads, which
        would otherwise break ``find_offers`` for the CPU smoke and any
        downstream caller passing ``min_vram_gb=0``.

        For GPU workloads (``reqs.min_vram_gb > 0``) calls
        :func:`sky.list_accelerators` (the modern replacement for the
        removed ``sky.gpu_list``) to obtain available accelerators,
        converts each entry to an :class:`~kinoforge.core.interfaces.Offer`,
        then delegates filtering and sorting to
        :func:`~kinoforge.core.offers.filter_offers`.

        The accelerator return shape is ``dict[str, list[InstanceTypeInfo]]``:
        each key is an accelerator name (e.g. ``"A100"``) and each value is a
        list of candidate :class:`InstanceTypeInfo` records (one per
        region/instance type / cloud). Test fakes may return either this
        mapping shape or a flat ``list`` of dict records — both are handled.

        Args:
            reqs: Hardware requirements to filter against.

        Returns:
            Filtered and sorted list of :class:`~kinoforge.core.interfaces.Offer`
            objects. For ``reqs.min_vram_gb == 0`` the list always contains a
            single synthetic CPU offer.
        """
        sky = self._sky()
        if reqs.min_vram_gb == 0:
            # CPU short-circuit: ``list_accelerators(gpus_only=True)`` returns
            # an empty dict for CPU smokes, so synthesise a single offer that
            # signals downstream ``create_instance`` to set CPU resource
            # constraints on the Task (see create_instance below).
            return [
                Offer(
                    id="sky-cpu-auto",
                    gpu_type="",
                    vram_gb=0,
                    cuda="0.0",
                    cost_rate_usd_per_hr=0.05,
                    mode="pod",
                )
            ]
        raw = sky.list_accelerators(
            **({} if self._clouds is None else {"clouds": self._clouds})
        )
        resolved = _resolve(sky, raw)

        # Normalise to an iterable of records. Modern shape is a dict-of-list;
        # flat-list shape is accepted for test-fake convenience.
        records: list[Any] = []
        if isinstance(resolved, dict):
            for _accel_name, info_list in resolved.items():
                records.extend(info_list)
        else:
            records.extend(resolved)

        raw_offers: list[Offer] = []
        for info in records:
            # Prefer the modern ``accelerator_name`` field; fall back to legacy
            # ``name`` (used by offline fakes).
            gpu_name: str = _record_field(info, "accelerator_name") or _record_field(
                info, "name"
            )
            # ``device_memory`` (modern) is a free-form string like ``"80GB"``;
            # offline fakes provide ``vram_gb`` directly. Try the int field first,
            # then a light string parse of device_memory.
            vram_gb: int = _coerce_vram_gb(info)
            cuda: str = _record_field(info, "cuda", default="12.0")
            cost: float = _coerce_float_field(info, "price") or _coerce_float_field(
                info, "cost_rate_usd_per_hr"
            )
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

        Maps ``spec.lifecycle.idle_timeout_s`` to SkyPilot's
        ``idle_minutes_to_autostop`` parameter (whole minutes, int).

        Builds a YAML-shaped task config (``name`` / ``envs`` / ``resources``
        / ``setup`` / ``run``) and converts it to a :class:`sky.Task` via
        :meth:`sky.Task.from_yaml_config`. Modern :func:`sky.launch` accepts
        only :class:`sky.Task` / :class:`sky.Dag` — passing a raw dict raises
        ``TypeError``.

        Resource selection:
          * If ``spec.offer`` is the synthetic CPU offer
            (``offer.gpu_type == ""`` and ``offer.vram_gb == 0``) the task
            requests ``cpus="1+", memory="2+"`` so SkyPilot picks the
            cheapest CPU SKU.
          * Otherwise, if ``spec.offer.gpu_type`` is non-empty, the task
            requests ``accelerators="<gpu_type>:1"``.

        When ``spec.provision_script`` is set, it is mapped to ``setup``
        (after :func:`_strip_trailing_exec` removes RunPod's trailing
        ``exec`` line so the setup phase can terminate). When ``spec.run_cmd``
        is set, it is shell-quoted via :func:`shlex.quote` and joined into
        ``run``. Empty values for either field omit the key.

        Args:
            spec: Instance specification.

        Returns:
            An :class:`~kinoforge.core.interfaces.Instance` with
            ``status="starting"`` and ``provider="skypilot"``.
        """
        sky = self._sky()
        autostop_minutes: int = int(spec.lifecycle.idle_timeout_s / 60.0)

        resources: dict[str, Any] = {}
        if spec.image:
            resources["image_id"] = _normalize_image_id(spec.image)
        # Resource selection: CPU synthetic offer triggers ``cpus``/``memory``
        # so SkyPilot picks the cheapest CPU SKU; a GPU offer triggers
        # ``accelerators=<gpu_type>:1``.
        if spec.offer is not None and not spec.offer.gpu_type:
            resources["cpus"] = "1+"
            resources["memory"] = "2+"
        elif spec.offer is not None and spec.offer.gpu_type:
            resources["accelerators"] = f"{spec.offer.gpu_type}:1"

        task_config: dict[str, Any] = {
            "name": spec.run_id or "kinoforge-skypilot",
            "envs": dict(spec.env),
        }
        if resources:
            # Default disk_size conservatively to stay under GCP's default
            # ``SSD_TOTAL_GB=250`` quota. SkyPilot's own default is 256 GB,
            # which exceeds that quota on a fresh project.
            # GPU images (e.g. ``skypilot-gcp-gpu-ubuntu-241030``) ship with a
            # 50 GB base OS layer; GCP rejects disk_size < image size with a
            # ``400 Invalid`` error. GPU offers therefore default to 60 GB (a
            # small head-room above the 50 GB floor). CPU smokes use 30 GB
            # (the CPU base images are ~20 GB).
            is_gpu = bool(spec.offer is not None and spec.offer.gpu_type)
            default_disk_gb = 60 if is_gpu else 30
            resources.setdefault("disk_size", default_disk_gb)
            # Spot/preemptible: maps spec.spot → SkyPilot's ``use_spot``.
            # Preemptible T4 quota (``PREEMPTIBLE_NVIDIA_T4_GPUS``) is granted
            # separately from the on-demand GPU quota (``GPUS_ALL_REGIONS``).
            if spec.spot:
                resources["use_spot"] = True
            if self._region:
                resources["region"] = self._region
            task_config["resources"] = resources
        # Layer Q dual-exec hazard resolution: the script's trailing
        # ``exec <run_cmd>`` line is stripped before it becomes Task.setup so
        # setup can terminate normally; spec.run_cmd carries the long-running
        # process into Task.run.
        if spec.provision_script:
            task_config["setup"] = _strip_trailing_exec(spec.provision_script)
        if spec.run_cmd:
            task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)

        # Modern sky.launch requires a sky.Task — passing the dict directly
        # raises ``TypeError: launch() got an unexpected ... type``. Build the
        # Task via from_yaml_config, whose schema matches the dict shape above.
        task = sky.Task.from_yaml_config(task_config)
        cluster_name: str = spec.run_id or "skypilot-cluster"
        raw = sky.launch(
            task,
            cluster_name=cluster_name,
            idle_minutes_to_autostop=autostop_minutes,
        )
        # Resolve a possible RequestId — the launch payload itself is not used
        # because the cluster name we passed *is* the canonical id and the
        # payload is ``(Optional[job_id], Optional[ResourceHandle])`` on the
        # modern API.
        _resolve(sky, raw)
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
        clusters = _resolve(sky, sky.status())
        for cluster in clusters:
            if _record_field(cluster, "name") == instance_id:
                return _cluster_record_to_instance(cluster)
        raise KeyError(f"no SkyPilot cluster found: {instance_id!r}")

    def list_instances(self) -> list[Instance]:
        """Return all active SkyPilot clusters.

        Calls ``sky_client.status()`` and converts each entry to an
        :class:`~kinoforge.core.interfaces.Instance`.

        Returns:
            A (possibly empty) list of :class:`~kinoforge.core.interfaces.Instance`.
        """
        sky = self._sky()
        clusters = _resolve(sky, sky.status())
        return [_cluster_record_to_instance(c) for c in clusters]

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
        # Resolve the down RequestId so the call blocks until SkyPilot has
        # accepted (and committed to) the teardown — otherwise the provider
        # returns while teardown is still pending and the status-poll loop
        # may observe stale records.
        _resolve(sky, sky.down(instance_id))
        # Poll until the cluster disappears from the status listing.
        while True:
            clusters = _resolve(sky, sky.status())
            names = {_record_field(c, "name") for c in clusters}
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
