"""Modal serverless-GPU compute provider.

Deploys the kinoforge FastAPI generation server onto Modal as a named App whose
``@modal.web_server`` runs the same ``provision_script; exec run_cmd`` that RunPod
runs, and returns the public ``.modal.run`` URL as ``endpoints["8000"]``. All Modal
and subprocess touchpoints sit behind injected callables for offline testing.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from kinoforge.core import registry
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.providers.modal._app import (
    ModalAppRequest,
    build_modal_app,
    default_deploy,
    default_list,
    default_stop,
)
from kinoforge.providers.modal._catalog import modal_offers

_DESTROY_POLL_MAX_ITERS: int = 40  # 40 × 3s ≈ 120s upper bound (mirror SkyPilot)


class ModalProvider(ComputeProvider):
    """Compute provider backed by Modal serverless GPUs."""

    name: str = "modal"

    def __init__(
        self,
        *,
        app_factory: Callable[
            [ModalAppRequest, Any], tuple[Any, Any]
        ] = build_modal_app,
        deployer: Callable[[Any, Any], str] = default_deploy,
        stopper: Callable[[str], None] = default_stop,
        lister: Callable[[], list[dict[str, Any]]] = default_list,
        modal_module: Any | None = None,  # noqa: ANN401
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the provider with injectable Modal/subprocess seams.

        Args:
            app_factory: Builds ``(app, server_fn)`` from a request + modal module.
            deployer: Deploys an app and returns its public web URL.
            stopper: Stops a named deployed app (bounded).
            lister: Returns deployed-app records (``modal app list --json``).
            modal_module: The ``modal`` SDK module (lazy-imported if None).
            sleep: Sleep function (injected in tests).
            clock: Monotonic-ish clock returning epoch seconds.
        """
        self._app_factory = app_factory
        self._deployer = deployer
        self._stopper = stopper
        self._lister = lister
        self._modal = modal_module
        self._sleep = sleep
        self._clock = clock
        #: run_id -> {"app": app, "url": url} for endpoints() / destroy().
        self._deployments: dict[str, dict[str, Any]] = {}

    # -- offers -------------------------------------------------------------
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return Modal catalog offers meeting ``reqs``."""
        return modal_offers(reqs)

    # -- lifecycle ----------------------------------------------------------
    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Build + deploy a Modal App and return its HTTP endpoint.

        Args:
            spec: The instance spec (image, offer, provision_script, run_cmd, env).

        Returns:
            An ``Instance`` in ``starting`` state with ``endpoints["8000"]`` set.

        Raises:
            ValueError: If ``run_cmd``/``provision_script`` or ``offer`` is missing.
        """
        if not spec.run_cmd or not spec.provision_script:
            raise ValueError(
                "ModalProvider requires spec.run_cmd and spec.provision_script "
                f"(the server boot command); got run_cmd={spec.run_cmd!r}"
            )
        if spec.offer is None:
            raise ValueError("ModalProvider requires spec.offer (GPU selection)")

        # Ephemeral runs must not leak the subcommand/timestamp-bearing
        # run_id into the app name: `modal app stop` only STOPS an app, and
        # stopped apps linger in `modal app list` forever. Mirror RunPod's
        # pod_name_includes_alias handling (runpod/__init__.py:814-821)
        # with an opaque token. The opaque id becomes the Instance.id so
        # ledger (memory-only), ephemeral-index, destroy and probe all key
        # off one consistent identifier.
        _eph = EphemeralSession.current()
        if _eph is not None and not _eph.policy.pod_name_includes_alias:
            app_run_id = f"eph-{secrets.token_hex(4)}"
        else:
            app_run_id = spec.run_id

        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        # Persist the HF cache onto the Modal Volume so a preempted/cold
        # container re-uses downloaded weights instead of re-fetching. The
        # server's own os.environ.setdefault("HF_HOME", ...) respects this.
        env.setdefault("HF_HOME", volume_mount)

        # Modal fast-boot: the container boots with the RUNTIME script only —
        # the build script is baked into the image below, so re-running it at
        # container start would re-download everything and re-open the
        # preemption window (2026-07-09 FlashVSR failure). Non-splitting engines
        # leave runtime_provision_script None and fall back to the combined one.
        boot_script = spec.runtime_provision_script or spec.provision_script

        req = ModalAppRequest(
            run_id=app_run_id,
            image=spec.image,
            gpu=spec.offer.gpu_type,
            provision_script=boot_script,
            run_cmd=list(spec.run_cmd),
            env=env,
            volume_mount=volume_mount,
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
            image_build_script=spec.image_build_script,
        )
        app, server_fn = self._app_factory(req, self._modal_mod())
        url = self._deployer(app, server_fn)
        self._deployments[app_run_id] = {
            "app": app,
            "url": url,
            "name": f"kinoforge-{app_run_id}",
        }
        return Instance(
            id=app_run_id,
            provider=self.name,
            status="starting",
            created_at=self._clock(),
            endpoints={"8000": url},
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=spec.offer.cost_rate_usd_per_hr,
        )

    def _modal_mod(self) -> Any:  # noqa: ANN401
        """Return the injected/real ``modal`` module (``None`` if unavailable).

        The real ``build_modal_app`` needs a live ``modal`` module, present only
        in the ``live-modal`` env. Offline tests inject ``app_factory`` and ignore
        the module, so a missing SDK degrades to ``None`` rather than raising.
        """
        if self._modal is None:
            try:
                import modal
            except ImportError:
                return None
            self._modal = modal
        return self._modal

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the HTTP endpoint map for ``instance``."""
        rec = self._deployments.get(instance.id)
        if rec and rec.get("url"):
            return {"8000": rec["url"]}
        return dict(instance.endpoints)

    @staticmethod
    def _rec_name(rec: dict[str, Any]) -> str:
        """App name from a ``modal app list`` record.

        The real ``modal app list --json`` exposes the deploy name under
        ``description``; unit fakes use ``name``. Accept either.
        """
        return str(rec.get("description") or rec.get("name") or "")

    @staticmethod
    def _rec_active(rec: dict[str, Any]) -> bool:
        """True while the app is live (``modal app list`` keeps stopped apps)."""
        return str(rec.get("state", "")) in {"deployed", "running"}

    def _record_to_instance(self, rec: dict[str, Any]) -> Instance:
        """Map a ``modal app list`` record onto an :class:`Instance`."""
        run_id = self._rec_name(rec)[len("kinoforge-") :]
        return Instance(
            id=run_id,
            provider=self.name,
            status="ready",
            created_at=self._clock(),
        )

    def list_instances(self) -> list[Instance]:
        """Return kinoforge-owned Modal deployments that are still active.

        Stopped apps linger in ``modal app list``; excluding them keeps the
        ledger/teardown checks honest.
        """
        return [
            self._record_to_instance(r)
            for r in self._lister()
            if self._rec_name(r).startswith("kinoforge-") and self._rec_active(r)
        ]

    def get_instance(self, instance_id: str) -> Instance:
        """Return the named deployment or raise ``KeyError``-style not-found."""
        for inst in self.list_instances():
            if inst.id == instance_id:
                return inst
        raise KeyError(f"no modal deployment for run_id={instance_id!r}")

    def stop_instance(self, instance_id: str) -> None:
        """Stop (== destroy for Modal) the named deployment."""
        self.destroy_instance(instance_id)

    def destroy_instance(self, instance_id: str) -> None:
        """Stop the deployment and poll until gone (bounded)."""
        rec = self._deployments.get(instance_id)
        app_name = rec["name"] if rec else f"kinoforge-{instance_id}"
        try:
            self._stopper(app_name)
            for _ in range(_DESTROY_POLL_MAX_ITERS):
                active = {
                    self._rec_name(r) for r in self._lister() if self._rec_active(r)
                }
                if app_name not in active:  # absent OR transitioned to stopped
                    break
                self._sleep(3.0)
        finally:
            self._deployments.pop(instance_id, None)

    # -- heartbeat (Modal owns liveness) ------------------------------------
    def heartbeat(self, instance_id: str) -> None:
        """No-op — Modal manages container liveness."""
        return None

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return ``None`` — Modal exposes no wire-level heartbeat read.

        Off-ABC but REQUIRED: ``HeartbeatLoop._tick_once`` calls it every tick.
        """
        return None

    # -- sweeper-ephemeral-reap substrate ------------------------------------
    def note_endpoints(self, instance_id: str, endpoints: Mapping[str, str]) -> None:
        """Prime the URL cache for a cross-process probe (reaper seam).

        Modal ``.modal.run`` URLs are NOT rebuildable from the app name
        (M5 lesson, commit 1cb4299) — in the sweeper process the only
        source is the EphemeralIndexRow's persisted endpoints, threaded
        here by ``reaper_actor._probe_with_cache``. Never overwrites a
        live deployment record.

        Args:
            instance_id: The short run-id (without the ``kinoforge-`` prefix).
            endpoints: Port -> URL mapping from the EphemeralIndexRow.
        """
        url = endpoints.get("8000")
        if url and instance_id not in self._deployments:
            self._deployments[instance_id] = {
                "app": None,
                "url": url,
                "name": f"kinoforge-{instance_id}",
            }

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:
        """Live runtime probe: app existence + /util snapshot.

        Outcomes:
          * app not in the active list → ``found=False`` (reaper: GC_404)
          * active, URL known, /util ok → fully populated probe
          * active, URL unknown or /util raised → ``found=True`` with util
            fields None + ``error`` set (partial probe — conservative, the
            reaper cannot false-reap on it)

        A lister failure PROPAGATES (reaper classifies PROBE_FAILED) —
        fabricating ``found=False`` there would GC rows of live apps.

        Args:
            pod_id: The short instance id (without the ``kinoforge-`` prefix).

        Returns:
            A :class:`~kinoforge.core.runtime_probe.RuntimeProbe`.

        Raises:
            Exception: Any exception raised by the lister propagates; the
                reaper catches and records PROBE_FAILED.
        """
        from kinoforge.providers.modal.util import ModalUtilEndpoint

        now_local = datetime.now().isoformat()
        active_ids = {inst.id for inst in self.list_instances()}
        if pod_id not in active_ids:
            return RuntimeProbe(
                pod_id=pod_id,
                found=False,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
            )
        rec = self._deployments.get(pod_id)
        url = rec.get("url") if rec else None
        if not url:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error="no endpoint known for /util (note_endpoints not primed)",
            )
        try:
            snapshot = ModalUtilEndpoint(resolve_endpoint=lambda _id: url).read_util(
                pod_id
            )
        except Exception as exc:  # noqa: BLE001 — TransportError et al.
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error=f"{type(exc).__name__}: {exc}",
            )
        if snapshot is None:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=None,
                gpu_util_pct=None,
                cpu_pct=None,
                cost_per_hr=None,
                probed_at_local=now_local,
                error="/util returned 404 on an active app",
            )
        return RuntimeProbe(
            pod_id=pod_id,
            found=True,
            container_uptime_s=(
                float(snapshot.uptime_seconds)
                if snapshot.uptime_seconds is not None
                else None
            ),
            gpu_util_pct=snapshot.gpu_util_percent,
            cpu_pct=snapshot.cpu_percent,
            cost_per_hr=None,
            probed_at_local=now_local,
        )


registry.register_provider("modal", lambda: ModalProvider())
