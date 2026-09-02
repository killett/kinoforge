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

Cost model
----------
``idle_timeout_s`` is still mapped to SkyPilot's ``idle_minutes_to_autostop``,
but that mechanism is INERT for kinoforge's server-mode deploys: the launch
becomes ``Task.run``, a job that never terminates, so ``is_cluster_idle()``
is permanently False and the 60 s ``AutostopEvent`` tick resets the idleness
timer forever (finding F1, verified against skypilot-0.12.3.post1). It
remains correct for the one-shot configs whose ``run`` terminates.

The real guardrail is the instance-side deadline watchdog
(:mod:`kinoforge.providers.skypilot.watchdog`), armed at the top of
``Task.setup`` and enforced on the machine, so it survives the orchestrator
process dying. ``down=True`` makes any autostop that DOES fire terminate
rather than stop (a stopped cluster keeps billing its disk — finding F2).

Self-registers under ``"skypilot"`` when this module is imported.
"""

from __future__ import annotations

import dataclasses
import logging
import socket
import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.errors import CapacityError, KinoforgeError, ProvisionFailed
from kinoforge.core.interfaces import (
    ComputeProvider,
    FieldSupport,
    Instance,
    InstanceSpec,
    Offer,
    Placement,
    combine_steps,
    render_launch,
)
from kinoforge.core.offers import filter_offers
from kinoforge.providers.skypilot import watchdog
from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

logger = logging.getLogger(__name__)

# Bridge sky's vast adapter to vastai-sdk >= 0.2 as soon as the provider is
# imported; no-op when vastai_sdk is absent (default env) or already correct.
apply_vast_sdk_compat()

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


# ---------------------------------------------------------------------------
# Provider-internal SSH tunnel (HTTP-over-sky seam)
# ---------------------------------------------------------------------------

#: Max status polls in destroy_instance before returning even if the cluster is
#: still listed. Bounds teardown so a cloud that is slow to deprovision (or a
#: stale status listing) can never hang --no-reuse forever (observed as a ~7-min
#: Lambda hang 2026-07-08). destroy_confirmed re-verifies via list_instances and
#: retries, so returning unconfirmed here is safe, not a leak.
_DESTROY_POLL_MAX_ITERS: int = 40  # 40 × 3s ≈ 120s upper bound


@dataclasses.dataclass
class _Tunnel:
    """One live ssh port-forward.

    Attributes:
        proc: The ``ssh -N -T -L`` subprocess.
        local_port: The localhost port the forward is bound to.
    """

    proc: Any
    local_port: int


def _alloc_free_port() -> int:
    """Return an ephemeral free localhost TCP port for a tunnel's local end."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _spawn_ssh_tunnel(cluster_name: str, local_port: int, remote_port: int) -> Any:  # noqa: ANN401
    """Spawn a background ``ssh -N -L`` port-forward to ``cluster_name``.

    Relies on sky's generated SSH config making ``cluster_name`` resolvable.
    ``ExitOnForwardFailure`` makes ssh exit (not hang) if the forward can't bind.
    """
    return subprocess.Popen(  # noqa: S603 — fixed argv, cluster_name from our own run_id
        [  # noqa: S607 — ssh resolved via PATH inside the live-skypilot env
            "ssh",
            "-N",
            "-T",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"{local_port}:localhost:{remote_port}",
            cluster_name,
        ]
    )


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


class _LaunchLedger(Protocol):
    """The slice of :class:`~kinoforge.core.lifecycle.Ledger` used pre-launch.

    Narrowed to the exact keyword the provider calls (``max_age_s``) rather
    than a permissive ``**kwargs: Any`` — the latter type-checks against
    anything and would never catch a signature drift on either side. See
    ``tests/providers/test_skypilot.py::test_ledger_protocol_matches_the_real_ledger``
    for the mypy-visible proof that :class:`~kinoforge.core.lifecycle.Ledger`
    still satisfies this Protocol.
    """

    def record(self, instance: Instance, *, max_age_s: int | None = None) -> None:
        """Append a row for *instance*."""

    def forget(self, instance_id: str) -> None:
        """Remove the row for *instance_id*."""


# ---------------------------------------------------------------------------
# SkyPilotProvider
# ---------------------------------------------------------------------------


class SkyPilotProvider(ComputeProvider):
    """ComputeProvider backed by the SkyPilot multi-cloud SDK.

    The ``sky`` module is imported lazily — only when a method actually needs it
    and no ``sky_client`` has been injected.  Inject ``sky_client=<fake>`` to run
    without SkyPilot installed (all tests use this path).

    ``idle_timeout_s`` is mapped to SkyPilot's ``autostop`` (in minutes), but
    the real cost guardrail is the instance-side deadline watchdog armed at
    the top of every launched cluster's ``Task.setup``. See the module
    docstring's "Cost model" section for why.

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

    class Options(BaseModel):
        """Options only SkyPilot honours. Unknown keys are a config error.

        Attributes:
            clouds: Optional list of sky cloud names (e.g. ``["lambda"]``,
                ``["lambda", "vast"]``) pinned onto
                :attr:`SkyPilotProvider._clouds` by
                :func:`kinoforge._adapters.build_provider_for`. ``None``
                lets sky consider every enabled cloud and pick by price.
            retry_until_up: Whether ``sky.launch`` loops with backoff
                across zones/preemption-retry until provisioning succeeds.
                This is SkyPilot's analogue of RunPod's
                ``capacity_wait_s`` retry loop.
        """

        model_config = ConfigDict(extra="forbid")

        clouds: list[str] | None = None
        retry_until_up: bool = False

        @field_validator("clouds")
        @classmethod
        def _reject_empty_clouds(cls, v: list[str] | None) -> list[str] | None:
            """Reject an empty cloud list.

            The operator meant ``clouds: null`` (or forgot to populate the
            entry); ``sky.launch`` with zero clouds silently falls back to
            "every enabled cloud", which is the opposite of a pin.

            Args:
                v: The raw ``clouds`` value.

            Returns:
                *v* unchanged when it is ``None`` or non-empty.

            Raises:
                ValueError: *v* is an empty list.
            """
            if v is not None and not v:
                raise ValueError(
                    "clouds must be a non-empty list of sky cloud names "
                    "or null; got an empty list"
                )
            return v

    @classmethod
    def validate_options(cls, raw: Mapping[str, Any]) -> SkyPilotProvider.Options:
        """Parse *raw* into this provider's Options, forbidding unknown keys.

        Args:
            raw: The ``compute.backend_options["skypilot"]`` mapping.

        Returns:
            A validated :class:`SkyPilotProvider.Options`.
        """
        return cls.Options.model_validate(dict(raw))

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Instance-side deadline always; autostop only for batch specs.

        ON_INSTANCE_DEADLINE is the watchdog armed at the top of ``Task.setup``
        (providers/skypilot/watchdog.py). IDLE_AUTOSTOP holds only at BATCH:
        a server spec's launch becomes a never-terminating ``Task.run``,
        so ``job_lib.is_cluster_idle()`` is permanently False (verification
        doc F1) and the 60 s AutostopEvent tick resets the timer forever.

        RATE_READBACK, not RATE_DETERMINISTIC: the launch pins an accelerator
        NAME and sky's optimizer then picks cloud, region and SKU on its own,
        so the only honest price is one read back off the launched cluster
        (:meth:`realized_rate`). CATALOG_ENUMERATION is absent for the same
        reason — there is no catalog here to list, only an optimizer that
        takes constraints.
        """
        caps = {Capability.ON_INSTANCE_DEADLINE, Capability.RATE_READBACK}
        if shape is WorkloadShape.BATCH:
            caps.add(Capability.IDLE_AUTOSTOP)
        return frozenset(caps)

    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """Declare what the Task config and the catalog filter read.

        SkyPilot honours the setup/run pair and the resource block it builds
        in :meth:`create_instance`. ``ports`` / ``volume_*`` are UNSUPPORTED
        because the tunnel is opened by kinoforge over ssh rather than
        declared to sky, and no volume is attached at all — declaring them
        CONSUMED would be the exact lie this table exists to prevent.

        Two rows are deliberately narrower than they look:

        * ``max_usd_per_hr`` is UNSUPPORTED. ``find_offers`` does hand it to
          :func:`~kinoforge.core.offers.filter_offers`, but the launch pins
          only the accelerator NAME — sky's optimizer then picks cloud,
          region and SKU on its own, and has been observed booking a $1.99
          Lambda A100 under a $1.00 ceiling (2026-07-07). Read-and-then-
          overridden is not honoured; this is verification finding F4 and it
          stays UNSUPPORTED until S4 wires the realized-rate check.
        * ``disk_gb`` is UNSUPPORTED: ``resources`` never receives the
          operator's value, so the ``setdefault("disk_size", 60 if gpu else
          30)`` below always wins. Shipped skypilot configs DO set
          ``placement.disk_gb`` today; every one of them is being ignored.

        ``min_vram_gb`` by contrast IS honoured: the accelerator name that
        survives the VRAM floor is the name pinned on the wire, so wherever
        sky books it the VRAM comes with it. It also has a second, direct
        read — ``min_vram_gb == 0`` short-circuits to the synthetic CPU
        offer, which is what makes the task request ``cpus``/``memory``.

        ``heartbeat_mode`` is UNSUPPORTED: sky exposes no per-cluster tag
        store to read a heartbeat out of, and
        ``_adapters.build_heartbeat_endpoint_for`` raises for any non-``none``
        value on this provider. ``warm_reuse_auto_attach`` is UNSUPPORTED
        everywhere — the warm scan is a CLI decision taken before
        ``create_instance`` is called at all.

        ``mode`` is UNSUPPORTED: sky books an instance and kinoforge runs a
        server on it. There is no serverless arm to route to, so
        ``mode: serverless`` on a skypilot cfg describes nothing this
        provider can do.

        ``backend_options`` is consumed by the SkyPilot namespace's owner,
        :func:`kinoforge._adapters.build_provider_for`, which turns
        ``clouds`` / ``retry_until_up`` into constructor arguments; this
        provider reads them off ``self`` rather than off ``spec``.
        ``region`` arrives by that same route — ``build_provider_for`` reads
        ``cfg.placement().region`` onto ``self._region``, which
        :meth:`create_instance` pins onto ``resources["region"]``.

        Returns:
            The declared field-support mapping.
        """
        c, u = FieldSupport.CONSUMED, FieldSupport.UNSUPPORTED
        return {
            # -- placement -------------------------------------------------
            "accelerators": c,  # find_offers ranks the catalog by preference
            "accelerator_count": u,  # accelerators=f"{gpu_type}:1", hardcoded
            "min_vram_gb": c,  # filter_offers floor + the CPU short-circuit
            "min_cuda": c,  # filter_offers excludes below the floor
            "disk_gb": u,  # disk_size is 60/30 by fiat
            "region": c,  # resources["region"], via the _adapters wiring
            "spot": c,  # resources["use_spot"]
            "max_usd_per_hr": u,  # F4: the optimizer never sees the cap
            # -- compute ---------------------------------------------------
            "mode": u,  # sky books an instance; there is no serverless arm
            "heartbeat_mode": u,  # no substrate; the dispatch raises for skypilot
            "warm_reuse_auto_attach": u,  # orchestrator-side scan, not provider
            # -- spec ------------------------------------------------------
            "image": c,  # resources["image_id"], docker:-normalised
            "ports": u,  # the tunnel is ssh-side, never declared to sky
            "volume_gb": u,  # no volume is attached
            "volume_mount": u,  # no volume is attached
            "env": c,  # task_config["envs"]
            "tags": c,  # the F12 provisional row, then Instance.tags
            "run_id": c,  # task name + cluster_name
            "setup_steps": c,  # combined into Task.setup
            "launch": c,  # rendered into Task.run
            "lifecycle": c,  # idle_minutes_to_autostop + the watchdog deadline
            "backend_options": c,  # cloud pin + retry_until_up, via _adapters
        }

    def __init__(
        self,
        sky_client: Any | None = None,  # noqa: ANN401
        *,
        clouds: list[str] | None = None,
        region: str | None = None,
        retry_until_up: bool = False,
        autodown: bool = True,
        sleep: Callable[[float], None] = time.sleep,
        ssh_spawn: Callable[[str, int, int], Any] | None = None,
        port_allocator: Callable[[], int] | None = None,
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
            retry_until_up: When ``True``, passed to ``sky.launch`` so
                SkyPilot loops with backoff across zones / preemption-retry
                until provisioning succeeds. Required when spot capacity
                is bursty (the typical case for preemptible GPUs); the
                default-False single-attempt path bails on the first
                ``ResourcesUnavailableError`` and is only safe when the
                operator knows capacity is currently available.
            autodown: When ``True`` (default) ``down=True`` is passed to
                ``sky.launch`` so an autostop TERMINATES the cluster instead
                of stopping it — a stopped cluster keeps billing its disk
                (finding F2). Set ``False`` only for a workflow that
                genuinely needs stop-and-restart onto the same disk;
                kinoforge has none today (``stop_instance`` is a no-op).
            sleep: Injectable sleep used between destroy-poll iterations.
            ssh_spawn: Injectable ``(cluster_name, local_port, remote_port) ->
                proc`` seam opening the provider-internal ``ssh -L`` tunnel;
                defaults to :func:`_spawn_ssh_tunnel`. Tests pass a fake proc.
            port_allocator: Injectable ``() -> int`` seam picking the tunnel's
                free local port; defaults to :func:`_alloc_free_port`.
        """
        self._sky_client = sky_client
        self._clouds = clouds
        self._region = region
        self._retry_until_up = retry_until_up
        self._autodown = autodown
        self._sleep = sleep
        self._ssh_spawn: Callable[[str, int, int], Any] = (
            ssh_spawn if ssh_spawn is not None else _spawn_ssh_tunnel
        )
        self._alloc_port: Callable[[], int] = (
            port_allocator if port_allocator is not None else _alloc_free_port
        )
        #: cluster_name -> remote_port -> live tunnel (killed on destroy).
        self._tunnels: dict[str, dict[str, _Tunnel]] = {}
        #: Optional ledger for the pre-launch provisional row (F12). ``None``
        #: until :meth:`set_launch_ledger` is called; every existing
        #: construction of this provider leaves it unset, so behaviour is
        #: unchanged without an explicit opt-in.
        self._launch_ledger: _LaunchLedger | None = None

    def set_launch_ledger(self, ledger: _LaunchLedger) -> None:
        """Install the ledger used for the pre-launch provisional row.

        Duck-typed rather than an ABC method: :class:`ComputeProvider` is out
        of scope for this change, and ``kinoforge.core`` must not import
        provider modules. The orchestrator calls this via ``getattr``.

        Args:
            ledger: A :class:`~kinoforge.core.lifecycle.Ledger`-shaped object.
        """
        self._launch_ledger = ledger

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

    def _candidate_accelerators(self, placement: Placement) -> list[Offer]:
        """Return the accelerators sky lists that satisfy *placement*, ranked.

        The catalog read behind :meth:`_select_accelerator`, kept separate so
        the filtering and the choosing can each be tested for what they do.
        Not public: SkyPilot has no bookable catalog — ``sky.list_accelerators``
        describes what the optimizer may consider, and the optimizer still
        picks cloud, region and instance type for itself.

        Args:
            placement: The portable resource block to filter and rank by.

        Returns:
            Offers passing the VRAM/CUDA/price filters, ranked by
            ``placement.accelerators``; empty when nothing clears them.
        """
        sky = self._sky()
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

        candidates: list[Offer] = []
        for info in records:
            # Prefer the modern ``accelerator_name`` field; fall back to legacy
            # ``name`` (used by offline fakes).
            gpu_name: str = _record_field(info, "accelerator_name") or _record_field(
                info, "name"
            )
            # ``device_memory`` (modern) is a free-form string like ``"80GB"``;
            # offline fakes provide ``vram_gb`` directly.
            candidates.append(
                Offer(
                    id=gpu_name,
                    gpu_type=gpu_name,
                    vram_gb=_coerce_vram_gb(info),
                    cuda=_record_field(info, "cuda", default="12.0"),
                    cost_rate_usd_per_hr=(
                        _coerce_float_field(info, "price")
                        or _coerce_float_field(info, "cost_rate_usd_per_hr")
                    ),
                    mode="pod",
                )
            )
        return filter_offers(candidates, placement)

    def _select_accelerator(self, placement: Placement) -> str | None:
        """Return the accelerator name to pin, or None for a CPU-only task.

        compute-seam S4: selection is the provider's business, so this is a
        private step of :meth:`create_instance` rather than the public
        ``find_offers`` it replaced. SkyPilot has no bookable catalog to
        enumerate — its optimizer takes constraints and picks cloud, region and
        instance type itself — so publishing one invited callers to treat a
        synthetic list as inventory.

        Three cases, in the order they are cheapest to answer:

        * ``min_vram_gb == 0`` and no named accelerator -> None. The task then
          requests ``cpus``/``memory`` and sky picks the cheapest CPU SKU.
          This is the ``examples/configs/skypilot-cpu.yaml`` path, which both
          the S2 and S3 live smokes ran on.
        * an operator-named accelerator -> the first one, with NO catalog read
          at all: they said what they want.
        * a VRAM floor with no name -> the cheapest accelerator sky lists that
          clears it. Two shipped configs (``skypilot-gpu``,
          ``skypilot-lambda-comfyui``) are this shape, so the enumeration
          cannot simply be deleted.

        Args:
            placement: The portable resource block.

        Returns:
            An accelerator name, or None for a CPU-only task.

        Raises:
            CapacityError: A VRAM/CUDA floor no listed accelerator clears.
                Refused here rather than launched without an accelerator,
                which would book a CPU box for a GPU render and fail minutes
                later at model load, on a cluster that is billing.
        """
        if placement.accelerators:
            return placement.accelerators[0]
        if placement.min_vram_gb == 0:
            return None
        surviving = self._candidate_accelerators(placement)
        if not surviving:
            raise CapacityError(
                f"no accelerator sky lists clears the placement floor "
                f"(min_vram_gb={placement.min_vram_gb}, "
                f"min_cuda={placement.min_cuda!r}); name one in "
                f"compute.placement.accelerators or lower the floor"
            )
        # Cheapest first among survivors: filter_offers ranks by the operator's
        # accelerators list, which is empty on this branch by construction.
        return min(surviving, key=lambda o: o.cost_rate_usd_per_hr).gpu_type

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Launch a SkyPilot cluster from ``spec``.

        Maps ``spec.lifecycle.idle_timeout_s`` to SkyPilot's
        ``idle_minutes_to_autostop`` parameter (whole minutes, int).

        Builds a YAML-shaped task config (``name`` / ``envs`` / ``resources``
        / ``setup`` / ``run``) and converts it to a :class:`sky.Task` via
        :meth:`sky.Task.from_yaml_config`. Modern :func:`sky.launch` accepts
        only :class:`sky.Task` / :class:`sky.Dag` — passing a raw dict raises
        ``TypeError``.

        Resource selection (compute-seam S4 — from ``spec.placement``, via
        :meth:`_select_accelerator`):
          * No named accelerator and ``min_vram_gb == 0`` -> the task requests
            ``cpus="1+", memory="2+"`` so SkyPilot picks the cheapest CPU SKU.
          * Otherwise the task requests ``accelerators="<name>:1"``, where the
            name is the operator's first ``placement.accelerators`` entry, or
            the cheapest accelerator sky lists that clears the VRAM floor.

        ``setup`` always starts with the instance-side deadline watchdog's
        arming step (:func:`kinoforge.providers.skypilot.watchdog.RENDER_ARM`)
        — present even when the spec carries no setup, so a
        provision-script-less deploy (e.g. the CPU smoke) still carries a
        deadline. ``spec.setup_steps`` are combined and appended after the
        arming step, and ``spec.launch`` is rendered into ``run``. The split
        is the engine's, not a guess: ``Task.setup`` must terminate for
        ``Task.run`` to start, and before compute-seam S3 this provider tried
        to find the boundary by substring-matching ``" exec "`` on the script's
        last line — which left the diffusers server inside ``setup`` and
        stripped comfyui's ``cd`` out of ``run``.

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
        # compute-seam S4: CPU-vs-GPU comes from PLACEMENT, not from a
        # synthetic offer a caller handed down. Pre-S4 the signal was
        # ``spec.offer.gpu_type`` being empty, which find_offers produced from
        # exactly the same ``min_vram_gb == 0`` test now made directly.
        accelerator = self._select_accelerator(spec.placement)
        if accelerator is None:
            resources["cpus"] = "1+"
            resources["memory"] = "2+"
        else:
            resources["accelerators"] = f"{accelerator}:1"

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
            is_gpu = accelerator is not None
            default_disk_gb = 60 if is_gpu else 30
            resources.setdefault("disk_size", default_disk_gb)
            # Spot/preemptible: maps spec.placement.spot → SkyPilot's
            # ``use_spot``. Preemptible T4 quota (``PREEMPTIBLE_NVIDIA_T4_GPUS``)
            # is granted separately from the on-demand GPU quota
            # (``GPUS_ALL_REGIONS``).
            if spec.placement.spot:
                resources["use_spot"] = True
            if self._region:
                resources["region"] = self._region
            # Pin the LAUNCH cloud to the operator's clouds set. The
            # _clouds filter only narrows find_offers' CATALOG enumeration; sky's
            # optimizer otherwise still launches on the globally-cheapest cloud
            # for the accelerator (observed 2026-07-07: a clouds=["vast"]
            # config provisioned a Lambda A100 at $1.99, defeating the vast pin
            # and the price cap). One cloud → ``cloud``; several → ``any_of``.
            if self._clouds:
                if len(self._clouds) == 1:
                    resources["cloud"] = self._clouds[0]
                else:
                    resources["any_of"] = [{"cloud": c} for c in self._clouds]
            task_config["resources"] = resources
        # Layer Q's dual-exec hazard is resolved at the source as of S3: the
        # engine emits setup and launch as separate values, so Task.setup
        # terminates because it contains no server, not because this provider
        # managed to spot one and cut it off.
        launch_epoch = time.time()
        deadline_epoch = watchdog.compute_deadline(
            launch_epoch=launch_epoch,
            max_lifetime_s=spec.lifecycle.max_lifetime_s,
            budget_usd=spec.lifecycle.budget_usd,
            # compute-seam S4: there is no offer to price from here, and the
            # realized rate is not readable until the cluster exists. The
            # operator's ceiling is the conservative stand-in: a rate that is
            # too HIGH only moves the budget deadline earlier, which is the
            # safe direction for a guardrail that has to survive the
            # controller dying.
            rate_usd_per_hr=spec.placement.max_usd_per_hr,
        )
        # The watchdog is armed at the TOP of setup, before the provision
        # script's installs: SkyPilot autostop cannot fire for a server-mode
        # deploy (F1), so this is the only guardrail that survives the
        # orchestrator dying.
        setup_parts: list[str] = [
            watchdog.RENDER_ARM(deadline_epoch=deadline_epoch, now=launch_epoch)
        ]
        # compute-seam S3: the steps ARE the setup and the launch IS the run.
        # No guessing where one ends and the other begins — the substring
        # heuristic that used to do the guessing (`_strip_trailing_exec`) was
        # wrong on both shipped engines, leaving the diffusers server inside
        # Task.setup (which then never terminated) and stripping comfyui's
        # `cd /workspace/ComfyUI` so Task.run ran main.py from the login dir.
        if spec.setup_steps:
            setup_parts.append(combine_steps(spec.setup_steps))
        task_config["setup"] = "\n".join(setup_parts)
        if spec.launch is not None:
            # NOT a shlex.quote-joined argv: re-deriving the line is what
            # dropped comfyui's workdir and its exec.
            task_config["run"] = render_launch(spec.launch)

        # Modern sky.launch requires a sky.Task — passing the dict directly
        # raises ``TypeError: launch() got an unexpected ... type``. Build the
        # Task via from_yaml_config, whose schema matches the dict shape above.
        task = sky.Task.from_yaml_config(task_config)
        cluster_name: str = spec.run_id or "skypilot-cluster"
        launch_kwargs: dict[str, Any] = {
            "cluster_name": cluster_name,
            "idle_minutes_to_autostop": autostop_minutes,
            "down": self._autodown,
        }
        if self._retry_until_up:
            launch_kwargs["retry_until_up"] = True

        # F12 — durable record BEFORE the launch. sky.launch is a multi-minute
        # call; a SIGKILL inside it would otherwise leave a billing cluster
        # that no kinoforge command can see. Forgotten on the success path so
        # the orchestrator's post-create record is the only surviving row.
        provisional = Instance(
            id=cluster_name,
            provider=self.name,
            status="starting",
            created_at=launch_epoch,
            endpoints={},
            tags={
                **dict(spec.tags),
                "kf_launch_phase": "launching",
                "kf_cloud": ",".join(self._clouds) if self._clouds else "auto",
                "kf_run_id": spec.run_id or cluster_name,
                "kf_launched_at": repr(launch_epoch),
                "kf_deadline_epoch": repr(deadline_epoch),
            },
            cost_rate_usd_per_hr=0.0,
        )
        if self._launch_ledger is not None:
            # Bookkeeping must never be able to fail a launch that would
            # otherwise have succeeded: a transient store 5xx, a lock-lease
            # timeout, or an unwritable local path must not propagate out of
            # create_instance. Unlike the ``forget`` failure below, a failed
            # ``record`` here is not dangerous — it just silently forfeits
            # F12 protection for *this* launch — but it is still logged
            # loudly rather than passed, since a quiet failure here is
            # indistinguishable from "the ledger is fine and simply chose
            # not to write."
            try:
                self._launch_ledger.record(
                    provisional, max_age_s=int(spec.lifecycle.max_lifetime_s)
                )
            except Exception:  # noqa: BLE001 — ledger fault must not block launch
                logger.warning(
                    "F12 provisional ledger record failed for %r; this launch "
                    "has no pre-launch orphan protection until it completes",
                    cluster_name,
                    exc_info=True,
                )
        raw = sky.launch(task, **launch_kwargs)
        # Resolve a possible RequestId — the launch payload itself is not used
        # because the cluster name we passed *is* the canonical id and the
        # payload is ``(Optional[job_id], Optional[ResourceHandle])`` on the
        # modern API.
        _resolve(sky, raw)
        endpoints: dict[str, str] = {}
        # Only a server spec (one that declares a long-running launch) needs
        # HTTP tunnels; a server-less deploy (CPU smoke) gets none. S5: one
        # tunnel per DECLARED port, because an engine declaring ["8000",
        # "8001"] means both are load-bearing — 8001 is the /tmp file server
        # the project's own live-smoke rule fetches bootstrap.log from.
        if spec.launch is not None:
            opened: dict[str, _Tunnel] = {}
            port = ""
            try:
                for port in spec.ports:
                    local_port = self._alloc_port()
                    proc = self._ssh_spawn(cluster_name, local_port, int(port))
                    opened[port] = _Tunnel(proc=proc, local_port=local_port)
            except Exception as exc:  # noqa: BLE001 — any spawn fault → clean fail
                for tunnel in opened.values():
                    self._kill_tunnel(tunnel.proc)
                # Best-effort teardown so a live-but-unreachable cluster is not
                # left billing while we raise.
                try:
                    _resolve(sky, sky.down(cluster_name))
                except Exception:  # noqa: BLE001, S110
                    pass
                raise ProvisionFailed(
                    f"failed to open ssh tunnel to {cluster_name!r} for port "
                    f"{port}: {exc}"
                ) from exc
            self._tunnels[cluster_name] = opened
            endpoints = {
                port: f"http://127.0.0.1:{tunnel.local_port}"
                for port, tunnel in opened.items()
            }
        # Success: hand the record over to the orchestrator's post-create
        # write. Deliberately NOT in a finally — the tunnel-failure path
        # must keep its row, because a failed best-effort ``sky.down``
        # leaves a live cluster that only this row can surface.
        #
        # This is the damaging failure direction: the cluster is already UP
        # and its tunnel handle is already in ``self._tunnels``. A ``forget``
        # exception must not propagate — that would fail a launch that
        # actually succeeded, and the caller would never reach the
        # orchestrator's post-create record, leaving a stale
        # ``kf_launch_phase=launching`` row as the only (misleading) trace
        # of a healthy cluster. Bookkeeping must never be able to fail the
        # launch, so this is logged loudly rather than passed.
        if self._launch_ledger is not None:
            try:
                self._launch_ledger.forget(cluster_name)
            except Exception:  # noqa: BLE001 — ledger fault must not block launch
                logger.warning(
                    "F12 provisional ledger forget failed for %r; the "
                    "provisional 'launching' row may linger alongside the "
                    "real post-create record",
                    cluster_name,
                    exc_info=True,
                )
        return Instance(
            id=cluster_name,
            provider=self.name,
            status="starting",
            created_at=time.time(),
            endpoints=endpoints,
            tags={
                **dict(spec.tags),
                # S5 — same key RunPod uses (_pod_to_instance / endpoints), so
                # ensure_endpoints and warm-attach have one reader for both.
                "ports": ",".join(spec.ports),
            },
            # compute-seam S4: 0.0, not spec.offer's price. That assignment
            # WAS finding F4 — it reported the rate kinoforge asked for while
            # the optimizer booked whatever it liked (a $1.99 Lambda A100 under
            # a $1.09 ceiling). A provider that cannot know the rate at create
            # time must not guess one; the orchestrator fills this in from
            # realized_rate() immediately after the cap check.
            cost_rate_usd_per_hr=0.0,
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

    def realized_rate(self, instance: Instance) -> float | None:
        """Return the rate the optimizer's chosen resources will bill at.

        The launch payload is discarded by :meth:`create_instance` (the cluster
        name is the canonical id), so the handle is re-read from ``status()``.
        That also makes this correct on a warm attach, where no launch payload
        exists at all.

        This is the whole point of SkyPilot declaring ``RATE_READBACK``: the
        launch pins an accelerator NAME and the optimizer picks cloud, region
        and SKU itself, so the asked-for catalog price is not the billed price
        (verification finding F4 — a $1.99 Lambda A100 under a $1.09 ceiling).

        Args:
            instance: The cluster to price.

        Returns:
            USD per hour, or None when the cluster, its handle or its price is
            unreadable. Never raises — the caller tears the instance down on
            None rather than crashing mid-launch.
        """
        sky = self._sky()
        try:
            clusters = _resolve(sky, sky.status())
        except Exception:  # noqa: BLE001 — an unreadable rate is not a crash
            return None
        for cluster in clusters or []:
            if _record_field(cluster, "name") != instance.id:
                continue
            handle = (
                cluster.get("handle")
                if isinstance(cluster, dict)
                else getattr(cluster, "handle", None)
            )
            launched = getattr(handle, "launched_resources", None)
            if launched is None:
                return None
            try:
                return float(launched.get_cost(3600))
            except Exception:  # noqa: BLE001 — same reason
                return None
        return None

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
        """Refuse: SkyPilot has no pause-billing primitive.

        A cluster is either UP or torn down. This used to be a silent no-op,
        so ``kinoforge stop --id`` reported success while the cluster kept
        billing. PAUSE_BILLING is not declared; the honest answer is a
        refusal that names the operation that does work.

        Args:
            instance_id: The cluster name the caller wanted paused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            f"skypilot cannot pause billing for {instance_id!r}; "
            f"use `kinoforge destroy --id {instance_id}` to tear the cluster down"
        )

    @staticmethod
    def _kill_tunnel(tunnel: Any) -> None:  # noqa: ANN401
        """Terminate a tunnel subprocess best-effort (never raises)."""
        try:
            tunnel.terminate()
        except Exception:  # noqa: BLE001, S110 — teardown must not mask the real error
            pass

    def destroy_instance(self, instance_id: str) -> None:
        """Tear down a SkyPilot cluster and poll until it is confirmed gone.

        Calls ``sky_client.down(instance_id)`` once, then polls
        ``sky_client.status()`` until the cluster no longer appears.
        Idempotent: if the cluster is already absent the call returns immediately.

        Args:
            instance_id: The SkyPilot cluster name to destroy.
        """
        sky = self._sky()
        # Pop the whole tunnel map up front and kill every one of them in a
        # finally, so a failing sky.down (or status-poll) never leaks any of
        # the ssh port-forwards — S5: there can be more than one per cluster.
        tunnels = self._tunnels.pop(instance_id, None) or {}
        try:
            # Resolve the down RequestId so the call blocks until SkyPilot has
            # accepted (and committed to) the teardown — otherwise the provider
            # returns while teardown is still pending and the status-poll loop
            # may observe stale records.
            _resolve(sky, sky.down(instance_id))
            # Poll until the cluster disappears — BOUNDED. sky.down was already
            # accepted above; if the cluster is slow to leave the status listing
            # we return after the bound rather than hang forever (the caller's
            # destroy_confirmed re-verifies + retries).
            for _ in range(_DESTROY_POLL_MAX_ITERS):
                clusters = _resolve(sky, sky.status())
                names = {_record_field(c, "name") for c in clusters}
                if instance_id not in names:
                    return  # confirmed gone
                self._sleep(3.0)
        finally:
            for tunnel in tunnels.values():
                self._kill_tunnel(tunnel.proc)

    def heartbeat(self, instance_id: str) -> None:
        """No-op by design; ``HEARTBEAT_READ`` is not declared.

        ``HeartbeatLoop`` calls this on every provider unconditionally, so
        the method must exist — but nothing is written to or read from the
        cluster. SkyPilot exposes no wire-level liveness signal, and
        autostop cannot stand in for one at SERVER shape: a server spec's
        the launch becomes a never-terminating ``Task.run``, so
        ``is_cluster_idle()`` is permanently False (verification doc F1),
        which is exactly why :meth:`capabilities` refuses to declare
        ``IDLE_AUTOSTOP`` at that shape. What actually bounds a skypilot run
        is the instance-side watchdog (``ON_INSTANCE_DEADLINE``, see
        ``providers/skypilot/watchdog.py``).

        Args:
            instance_id: Unused.
        """

    # last_heartbeat: inherited ComputeProvider default (None). The 2026-06
    # AttributeError-every-tick incident that motivated adding it (and now
    # the ABC default) is summarized on ComputeProvider.last_heartbeat.

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

registry.register_provider("skypilot", lambda: SkyPilotProvider(), SkyPilotProvider)


# ---------------------------------------------------------------------------
# Validation Check — co-located with provider per the kinoforge.validation
# Check Registry pattern. Migrated from the pydantic _validate_cloud
# field_validator on ComputeConfig (Task 9 of the cfg-validation plan) so
# the rejection shows up in `kinoforge doctor` output alongside every
# other validation rule.
# ---------------------------------------------------------------------------


from kinoforge.validation.protocol import (  # noqa: E402
    CheckCategory as _CC,
)
from kinoforge.validation.protocol import (  # noqa: E402
    CheckResult as _CR,
)
from kinoforge.validation.protocol import (  # noqa: E402
    Severity as _SEV,
)
from kinoforge.validation.registry import register as _register  # noqa: E402

_SUPPORTED_CLOUDS = frozenset(
    {"aws", "gcp", "azure", "lambda", "vast", "kubernetes", "runpod"}
)


class SkyPilotCloudPinSupportedCheck:
    """STATIC ERROR — every pinned cloud must be in the supported set.

    Reads ``compute.backend_options.skypilot.clouds`` (compute-seam S1;
    the pin used to live at the portable ``compute.cloud``).
    """

    name: str = "skypilot_cloud_pin_supported"
    category: _CC = _CC.STATIC
    severity: _SEV = _SEV.ERROR

    def applies_to(self, cfg: Any) -> bool:  # noqa: ANN401 — Check Protocol
        """Apply iff the skypilot namespace pins a non-null cloud list."""
        return (
            cfg.compute is not None
            and cfg.backend_options_for("skypilot").clouds is not None
        )

    def run(self, cfg: Any) -> _CR:  # noqa: ANN401 — Check Protocol
        """Reject any cloud entry outside _SUPPORTED_CLOUDS."""
        clouds = cfg.backend_options_for("skypilot").clouds or []
        bad = [c for c in clouds if c not in _SUPPORTED_CLOUDS]
        if bad:
            return _CR(
                name=self.name,
                passed=False,
                severity=self.severity,
                message=(
                    f"compute.backend_options.skypilot.clouds has unsupported "
                    f"entr(ies): {bad}; supported set is "
                    f"{sorted(_SUPPORTED_CLOUDS)}"
                ),
                fix_suggestion=(
                    "remove the unsupported entries, or expand the "
                    "SkyPilot provider's _SUPPORTED_CLOUDS set after "
                    "a parity smoke"
                ),
            )
        return _CR(
            name=self.name,
            passed=True,
            severity=self.severity,
            message=f"{len(clouds)} cloud(s) all supported",
        )

    def auto_fix(self, cfg: Any) -> Any | None:  # noqa: ANN401 — Check Protocol
        """No safe auto-fix — operator's cloud choice is deliberate."""
        return None


_register(SkyPilotCloudPinSupportedCheck())
