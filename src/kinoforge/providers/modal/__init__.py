"""Modal serverless-GPU compute provider.

Deploys the kinoforge FastAPI generation server onto Modal as a named App whose
``@modal.web_server`` runs the same setup-steps-then-launch bundle that RunPod
runs, and returns the public ``.modal.run`` URL as ``endpoints["8000"]``. All Modal
and subprocess touchpoints sit behind injected callables for offline testing.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import CapacityError, TeardownError
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
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.providers.modal._app import (
    ModalAppRequest,
    build_modal_app,
    default_deploy,
    default_list,
    default_stop,
)
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG, modal_offers

_DESTROY_POLL_MAX_ITERS: int = 40  # 40 × 3s ≈ 120s upper bound (mirror SkyPilot)
# `modal app list` deliberately includes recently-stopped apps ("running,
# deployed or recently stopped", modal/cli/app.py:104), and `modal app stop`
# on an already-stopped one exits non-zero (modal/cli/app.py:513-520,
# "App is already stopped."). A stopped namesake must therefore never be
# preferred over the live app that shadows it. State strings are the CLI's
# own (modal/cli/app.py:41-49).
_STOPPED_APP_STATES: frozenset[str] = frozenset({"stopped", "stopping..."})


class ModalProvider(ComputeProvider):
    """Compute provider backed by Modal serverless GPUs."""

    name: str = "modal"

    class Options(BaseModel):
        """Modal accepts no backend options today — still forbids extras.

        An empty model is a real declaration ("this provider accepts no
        backend options"), not a placeholder for one that was never written.
        """

        model_config = ConfigDict(extra="forbid")

    @classmethod
    def validate_options(cls, raw: Mapping[str, Any]) -> ModalProvider.Options:
        """Parse *raw* into this provider's Options, forbidding unknown keys.

        Args:
            raw: The ``compute.backend_options["modal"]`` mapping.

        Returns:
            A validated :class:`ModalProvider.Options`.
        """
        return cls.Options.model_validate(dict(raw))

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Container-level idle scaledown + a function timeout deadline.

        IDLE_AUTOSTOP is ``_app.py`` ``scaledown_window`` (default 300 s);
        ON_INSTANCE_DEADLINE is the ``@app.function(timeout=...)`` cap. Modal
        does NOT honour cfg's ``lifecycle.job_timeout``, so JOB_TIMEOUT is
        absent, and there is no wire-level heartbeat read.

        RATE_DETERMINISTIC: the function runs on the GPU class kinoforge asked
        for, so ``MODAL_GPU_CATALOG``'s price for that class is the rate.
        CATALOG_ENUMERATION: that catalog is a real, listable table.
        """
        return frozenset(
            {
                Capability.RATE_DETERMINISTIC,
                Capability.CATALOG_ENUMERATION,
                Capability.RUNTIME_PROBE,
                Capability.UTIL_SNAPSHOT,
                Capability.IDLE_AUTOSTOP,
                Capability.ON_INSTANCE_DEADLINE,
            }
        )

    @classmethod
    def nothing_booked_errors(cls) -> tuple[type[BaseException], ...]:
        """Declare nothing beyond the portable ``CapacityError`` (S5, C1).

        ``create_instance`` shells out to ``modal deploy``, and a non-zero exit
        (or a timeout, or a Ctrl-C) does NOT prove the app was not created —
        Modal's control plane may have accepted the deploy and be scheduling
        containers while the CLI reports failure. Every failure here therefore
        keeps the provisional row, and ``cli/_reconcile``'s provider-agnostic
        age-out clears it once the launching grace window has passed.

        Returns:
            The empty tuple.
        """
        return ()

    @classmethod
    def consumes(cls) -> Mapping[str, FieldSupport]:
        """Declare what :class:`ModalAppRequest` and the catalog filter read.

        Derived by reading :meth:`create_instance` and
        :func:`kinoforge.providers.modal._catalog.modal_offers`.

        Modal schedules rather than books a host, so most of the placement
        block has nowhere to land: there is no disk knob, no spot pool, and
        the accelerator count is fixed at one per function. What survives is
        the catalog filter, which excludes candidates before the request is
        built.

        ``max_usd_per_hr`` is UNSUPPORTED, and NOT because the filter is
        skipped: every entry in ``MODAL_GPU_CATALOG`` carries
        ``mode="serverless"``, and :func:`~kinoforge.core.offers.filter_offers`
        applies its price ceiling only to ``mode == "pod"`` offers. The cap
        is handed over and then structurally ignored.

        ``heartbeat_mode`` is UNSUPPORTED, and bluntly so: the heartbeat
        dispatch in ``_adapters`` has no modal branch at all, so any
        non-``none`` value raises there. ``warm_reuse_auto_attach`` is
        UNSUPPORTED everywhere — the warm scan is a CLI decision taken before
        ``create_instance`` is called.

        ``mode`` is UNSUPPORTED from the other direction: every Modal app is
        serverless already, and the provider has no second arm to select, so
        neither value of the key changes anything.

        ``region`` is UNSUPPORTED for the same reason: Modal's
        ``@app.function`` does take a ``region=`` argument, and
        ``build_modal_app`` passes none, so a pinned region is dropped.
        Wiring it is deliberately out of S2 — new wire surface needs its own
        live proof.

        ``ports`` is UNSUPPORTED because the request has no port field —
        ``build_modal_app`` serves a single ``@web_server`` on 8000.

        Returns:
            The declared field-support mapping.
        """
        c, u = FieldSupport.CONSUMED, FieldSupport.UNSUPPORTED
        return {
            # -- placement -------------------------------------------------
            "accelerators": c,  # modal_offers ranks the catalog by preference
            "accelerator_count": u,  # one GPU per function, hardcoded
            "min_vram_gb": c,  # filter_offers excludes below the floor
            "min_cuda": c,  # filter_offers excludes below the floor
            "disk_gb": u,  # no disk knob on the request
            "region": u,  # @app.function(region=…) is never passed
            "spot": u,  # no spot pool
            "max_usd_per_hr": u,  # the catalog is serverless; the cap is skipped
            # -- compute ---------------------------------------------------
            "mode": u,  # every Modal app is serverless; the key selects nothing
            "heartbeat_mode": u,  # no branch at all in the heartbeat dispatch
            "warm_reuse_auto_attach": u,  # orchestrator-side scan, not provider
            # -- spec ------------------------------------------------------
            "image": c,  # ModalAppRequest.image
            "ports": u,  # the web_server port is fixed at 8000
            "volume_gb": u,  # the Volume is not sized from the spec
            "volume_mount": c,  # ModalAppRequest.volume_mount (+ HF_HOME)
            "env": c,  # ModalAppRequest.env
            "tags": c,  # Instance.tags
            "run_id": c,  # ModalAppRequest.run_id, and the app name
            "setup_steps": c,  # partitioned into the image bake + boot script
            "launch": c,  # ModalAppRequest.launch_line
            "lifecycle": c,  # scaledown_window_s + startup_timeout_s
            "backend_options": u,  # Options is empty: no knob to consume
        }

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
    def find_offers(self, placement: Placement) -> list[Offer]:
        """Return Modal catalog offers meeting ``reqs``."""
        return modal_offers(placement)

    # -- lifecycle ----------------------------------------------------------
    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Build + deploy a Modal App and return its HTTP endpoint.

        Args:
            spec: The instance spec (image, offer, setup_steps, launch, env).

        Returns:
            An ``Instance`` in ``starting`` state with ``endpoints["8000"]`` set.

        Raises:
            ValueError: If ``setup_steps``/``launch`` or ``offer`` is missing.
        """
        # A Modal container is a server or it is nothing: the app's web
        # endpoint IS the instance, so a spec with no launch would deploy an
        # app that answers nothing.
        if not spec.setup_steps or spec.launch is None:
            raise ValueError(
                "ModalProvider requires spec.setup_steps and spec.launch (the "
                f"server boot command); got setup_steps={len(spec.setup_steps)} "
                f"launch={spec.launch!r}"
            )
        # compute-seam S4: Modal picks its own GPU class from the portable
        # placement block. It schedules rather than books a host, so a caller
        # pre-deciding a SKU never had anything to pin here beyond the class
        # name this lookup produces.
        candidates = modal_offers(spec.placement)
        if not candidates:
            raise CapacityError(
                f"no Modal GPU class satisfies the placement "
                f"(min_vram_gb={spec.placement.min_vram_gb}, "
                f"accelerators={spec.placement.accelerators or '(any)'})"
            )
        chosen = candidates[0]

        # Ephemeral runs must not leak the subcommand/timestamp-bearing
        # run_id into the app name: `modal app stop` only STOPS an app, and
        # stopped apps linger in `modal app list` forever. The opaque id
        # becomes the Instance.id so ledger (memory-only), ephemeral-index,
        # destroy and probe all key off one consistent identifier.
        #
        # Spec A2 — the token is minted by the SESSION, not here. Minting it
        # inside create_instance meant the name did not exist until the create
        # was already in flight, so the pre-create ephemeral-index row (the
        # only durable trace an --ephemeral run leaves) could not name the app
        # it was protecting. `resource_name` returns spec.run_id unchanged
        # under the default policy, so the non-ephemeral shape is untouched.
        _eph = EphemeralSession.current()
        app_run_id = (
            _eph.resource_name(spec.run_id, self.name)
            if _eph is not None
            else spec.run_id
        )

        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        # Persist the HF cache onto the Modal Volume so a preempted/cold
        # container re-uses downloaded weights instead of re-fetching. The
        # server's own os.environ.setdefault("HF_HOME", ...) respects this.
        env.setdefault("HF_HOME", volume_mount)

        # Modal fast-boot: the container boots with the RUNTIME steps only —
        # the bakeable ones are baked into the image below, so re-running them
        # at container start would re-download everything and re-open the
        # preemption window (2026-07-09 FlashVSR failure).
        #
        # compute-seam S3: the partition is on the STEP's own flags, not on two
        # pre-split strings named after this pipeline. The two are independent
        # because a step can be both — the diffusers module embed has to exist
        # in the image for the build-phase weights fetch AND at container start
        # for the server. The build phase runs in isolation, so it carries its
        # own fail-fast line; the combined script's lives in the runtime
        # preamble, which a baked image never executes.
        boot_script = combine_steps(tuple(s for s in spec.setup_steps if s.runtime))
        bakeable = combine_steps(tuple(s for s in spec.setup_steps if s.bakeable))
        build_script = "set -euo pipefail\n" + bakeable if bakeable else None
        launch_line = render_launch(spec.launch)

        req = ModalAppRequest(
            run_id=app_run_id,
            image=spec.image,
            gpu=chosen.gpu_type,
            provision_script=boot_script,
            launch_line=launch_line,
            env=env,
            volume_mount=volume_mount,
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
            image_build_script=build_script,
        )
        app, server_fn = self._app_factory(req, self._modal_mod())
        url = self._deployer(app, server_fn)
        self._deployments[app_run_id] = {
            "app": app,
            "url": url,
            "name": f"kinoforge-{app_run_id}",
            # compute-seam S4: the booked GPU class is what realized_rate()
            # prices against. The Instance carries no accelerator field, so
            # without this the rate would have to be inferred from a number
            # that is itself the thing being verified.
            "gpu": req.gpu,
        }
        return Instance(
            id=app_run_id,
            provider=self.name,
            status="starting",
            created_at=self._clock(),
            endpoints={"8000": url},
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=chosen.cost_rate_usd_per_hr,
        )

    def realized_rate(self, instance: Instance) -> float | None:
        """Return the catalog price of the GPU class this app booked.

        Modal declares ``RATE_DETERMINISTIC``: the function runs on the GPU
        class the request named, so ``MODAL_GPU_CATALOG``'s price for that
        class IS the rate — no read off a live host happens or could. That
        makes this the ONLY thing able to enforce a cap on Modal, because
        every catalog entry is ``mode="serverless"`` and
        :func:`~kinoforge.core.offers.filter_offers` applies its price ceiling
        to ``mode == "pod"`` offers only.

        Args:
            instance: The deployed app to price.

        Returns:
            USD per hour from the catalog; the instance's own recorded rate
            when this process never deployed it (a warm attach); None when
            neither is known. Never raises.
        """
        gpu = str(self._deployments.get(instance.id, {}).get("gpu", ""))
        if gpu:
            for offer in MODAL_GPU_CATALOG:
                if offer.gpu_type == gpu:
                    return offer.cost_rate_usd_per_hr
        return instance.cost_rate_usd_per_hr or None

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
            # 0.0 sentinel, matching the RunPod list path: `modal app list`
            # does not return creation time, and stamping "now" would make
            # listed instances look forever-new to any age-based consumer
            # (the inverse failure of the conservative over-age 0.0).
            created_at=0.0,
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
        """Refuse: stopping a Modal app destroys it.

        This used to alias ``destroy_instance``, so a pause request silently
        destroyed the app and threw away the warm container.

        Args:
            instance_id: The run-id the caller wanted paused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            f"modal cannot pause billing for {instance_id!r}; stopping an app "
            f"destroys it — use `kinoforge destroy --id {instance_id}` if that "
            f"is what you want"
        )

    def _find_app_id(self, app_name: str) -> str | None:
        """Resolve ``app_name``'s live ``app_id`` from the listing (U17).

        Modal registers an App's NAME only once its deploy completes; an app
        whose deploying client died is addressable solely by its ``app_id``.
        ``description`` (``_rec_name``) is the join key back to that record
        even then, so matching on it recovers the id a name-only
        ``destroy --id`` cannot resolve.

        This lookup is an *enhancement* over the pre-U17 stop-by-name, so it
        must never make a destroy that used to work fail:

        * A listing that cannot be read at all (no ``modal`` binary,
          unauthenticated CLI, unparseable JSON) returns ``None`` — the
          caller falls back to the name path, exactly as before U17.
        * A malformed record is skipped, not fatal; the scan continues and
          can still resolve a match that sits after it.

        It still fails loudly when the shape is unusable *and* nothing was
        resolved: no match plus a bad record (or a non-list listing) raises
        a :class:`~kinoforge.core.errors.TeardownError` naming what it
        expected and what it found, rather than letting an untyped
        ``AttributeError``/``TypeError`` escape.

        Among several records sharing ``app_name`` — a reused or redeployed
        ``kinoforge-<run_id>`` description, with the stopped ones lingering
        in the listing — a record that is not stopped wins, mirroring
        Modal's own by-name resolution (modal/cli/app.py:84-91).

        Args:
            app_name: The ``kinoforge-<run_id>`` name to match on
                ``description``.

        Returns:
            The matching record's non-empty ``app_id``, preferring a
            non-stopped record; ``None`` when no record matches, no match
            carries an id, or the listing could not be read — the caller
            falls back to stopping by name in each case.

        Raises:
            TeardownError: The listing was not a list, or it contained a
                non-dict record AND no record matched ``app_name``.
        """
        try:
            records = self._lister()
        except Exception:  # noqa: BLE001 — degrade to the by-name stop
            return None
        if not isinstance(records, list):
            raise TeardownError(
                f"could not resolve app_id for {app_name!r}: expected a "
                f"list of records from `modal app list --json`, got "
                f"{type(records).__name__}"
            )
        matches: list[dict[str, Any]] = []
        bad_shape: str | None = None
        for rec in records:
            if not isinstance(rec, dict):
                if bad_shape is None:
                    bad_shape = type(rec).__name__
                continue
            if self._rec_name(rec) == app_name:
                matches.append(rec)
        if not matches:
            if bad_shape is not None:
                raise TeardownError(
                    f"could not resolve app_id for {app_name!r}: expected "
                    f"a dict record from `modal app list --json`, got "
                    f"{bad_shape}"
                )
            return None
        live_first = [
            r for r in matches if str(r.get("state", "")) not in _STOPPED_APP_STATES
        ] + [r for r in matches if str(r.get("state", "")) in _STOPPED_APP_STATES]
        for rec in live_first:
            app_id = str(rec.get("app_id") or "")
            if app_id:
                return app_id
        return None

    def destroy_instance(self, instance_id: str) -> None:
        """Stop the deployment and poll until gone (bounded).

        Raises:
            TeardownError: The stopper raised — reported with the app_id (or
                name) actually used — or ``_find_app_id`` found an unusable
                listing shape with nothing to resolve. Either way the
                deployment cache is still cleared (``finally``), matching
                pre-U17 behaviour of never leaving a stale in-process record
                behind on failure. A listing that simply cannot be read is
                NOT fatal: the stop falls back to the app name.
        """
        rec = self._deployments.get(instance_id)
        app_name = rec["name"] if rec else f"kinoforge-{instance_id}"
        try:
            app_id = self._find_app_id(app_name)
            target = app_id or app_name
            try:
                self._stopper(target)
            except TeardownError:
                # Already diagnosable — re-wrapping would nest
                # "failed to stop ...: failed to ...".
                raise
            except Exception as exc:  # noqa: BLE001 — CalledProcessError et al.
                raise TeardownError(
                    f"failed to stop modal app {app_name!r} (app_id={app_id!r}): {exc}"
                ) from exc
            for _ in range(_DESTROY_POLL_MAX_ITERS):
                try:
                    listing = self._lister()
                except Exception:  # noqa: BLE001 — the stop already succeeded
                    # Nothing left to confirm with; a listing failure after a
                    # successful stop must not become a teardown traceback.
                    break
                # Finding 2 (whole-branch review): "gone" must mean absent
                # from the listing entirely, or present only in a
                # confirmed-stopped state — NOT merely "not active". The
                # old check tested membership in `active`
                # ({"deployed", "running"}), so ANY other state — including
                # "initializing..." for a mid-deploy app stuck exactly in
                # the state U17 exists to reap — was trivially "not
                # active" and broke the poll on iteration 1 without the
                # stop having taken any visible effect. A state that is
                # present but neither active nor confirmed-stopped must
                # keep the poll going.
                #
                # Duplicate app names (a stop racing a redeploy — the same
                # class of race `_find_app_id` already handles) must not let
                # a stopped record silently mask a live one just because it
                # iterates later. Sort `live_first`, mirroring
                # `_find_app_id`'s existing shape, then `setdefault` so the
                # first (non-stopped, if any) record under a name wins the
                # dict key regardless of the listing's original order — a
                # plain dict comprehension would let whichever record came
                # last overwrite the other.
                live_first = [
                    r
                    for r in listing
                    if isinstance(r, dict)
                    and str(r.get("state", "")) not in _STOPPED_APP_STATES
                ] + [
                    r
                    for r in listing
                    if isinstance(r, dict)
                    and str(r.get("state", "")) in _STOPPED_APP_STATES
                ]
                states_by_name: dict[str, str] = {}
                for r in live_first:
                    states_by_name.setdefault(
                        self._rec_name(r), str(r.get("state", ""))
                    )
                state = states_by_name.get(app_name)
                if state is None or state in _STOPPED_APP_STATES:
                    break
                self._sleep(3.0)
        finally:
            self._deployments.pop(instance_id, None)

    # -- heartbeat (no-op; HEARTBEAT_READ not declared) ----------------------
    def heartbeat(self, instance_id: str) -> None:
        """No-op by design; ``HEARTBEAT_READ`` is not declared.

        ``HeartbeatLoop`` calls this on every provider unconditionally, so
        the method must exist — but Modal exposes no wire-level liveness
        read or write. What bounds a Modal run is the
        ``@app.function(timeout=...)`` deadline (``ON_INSTANCE_DEADLINE``)
        and ``scaledown_window`` (``IDLE_AUTOSTOP``), not this call.

        Args:
            instance_id: Unused.
        """
        return None

    # last_heartbeat: inherited ComputeProvider default (None — Modal
    # exposes no wire-level heartbeat read; loop substitutes its clock).

    # -- sweeper-ephemeral-reap substrate ------------------------------------
    def note_endpoints(self, instance_id: str, endpoints: Mapping[str, str]) -> None:
        """Prime the URL cache for a cross-process probe (reaper seam).

        Modal ``.modal.run`` URLs are NOT rebuildable from the app name
        (M5 lesson, commit 1cb4299) — in the sweeper process the only
        source is the EphemeralIndexRow's persisted endpoints, threaded
        here by ``reaper_actor._probe_with_cache``. Priming is skipped
        only when an existing record already carries a URL — a live
        deployment's URL is never clobbered, but a URL-less stub record
        (e.g. from a partial attach) still gets primed.

        Args:
            instance_id: The short run-id (without the ``kinoforge-`` prefix).
            endpoints: Port -> URL mapping from the EphemeralIndexRow.
        """
        url = endpoints.get("8000")
        existing = self._deployments.get(instance_id)
        if url and not (existing and existing.get("url")):
            if existing is not None:
                existing["url"] = url
            else:
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
            RuntimeError: The injected lister failed (e.g. ``modal`` CLI
                absent or ``modal app list`` crashed); propagates so the
                reaper records PROBE_FAILED.
            OSError: Subprocess/IO failure inside the lister; propagates
                for the same reason.
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
            # Per-call endpoint (unlike RunPod's cached self._util_endpoint):
            # the URL comes from the primed deployment record, not an
            # id-resolvable source, so a throwaway endpoint bound to that
            # URL is the simplest correct seam.
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


registry.register_provider("modal", lambda: ModalProvider(), ModalProvider)
