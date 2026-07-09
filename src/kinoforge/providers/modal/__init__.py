"""Modal serverless-GPU compute provider.

Deploys the kinoforge FastAPI generation server onto Modal as a named App whose
``@modal.web_server`` runs the same ``provision_script; exec run_cmd`` that RunPod
runs, and returns the public ``.modal.run`` URL as ``endpoints["8000"]``. All Modal
and subprocess touchpoints sit behind injected callables for offline testing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from kinoforge.core import registry
from kinoforge.core.interfaces import (
    ComputeProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
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

        req = ModalAppRequest(
            run_id=spec.run_id,
            image=spec.image,
            gpu=spec.offer.gpu_type,
            provision_script=spec.provision_script,
            run_cmd=list(spec.run_cmd),
            env=dict(spec.env),
            volume_mount=spec.volume_mount or "/cache/hf",
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
        )
        app, server_fn = self._app_factory(req, self._modal_mod())
        url = self._deployer(app, server_fn)
        self._deployments[spec.run_id] = {
            "app": app,
            "url": url,
            "name": f"kinoforge-{spec.run_id}",
        }
        return Instance(
            id=spec.run_id,
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

    def get_instance(self, instance_id: str) -> Instance:
        """Return the named deployment (implemented in Task 5)."""
        raise NotImplementedError  # pragma: no cover

    def list_instances(self) -> list[Instance]:
        """Return kinoforge-owned Modal deployments (implemented in Task 5)."""
        raise NotImplementedError  # pragma: no cover

    def stop_instance(self, instance_id: str) -> None:
        """Stop the named deployment (implemented in Task 5)."""
        raise NotImplementedError  # pragma: no cover

    def destroy_instance(self, instance_id: str) -> None:
        """Stop + poll until gone, bounded (implemented in Task 5)."""
        raise NotImplementedError  # pragma: no cover

    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the HTTP endpoint map (implemented in Task 5)."""
        raise NotImplementedError  # pragma: no cover

    # -- heartbeat (Modal owns liveness) ------------------------------------
    def heartbeat(self, instance_id: str) -> None:
        """No-op — Modal manages container liveness."""
        return None

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return ``None`` — Modal exposes no wire-level heartbeat read.

        Off-ABC but REQUIRED: ``HeartbeatLoop._tick_once`` calls it every tick.
        """
        return None


registry.register_provider("modal", lambda: ModalProvider())
