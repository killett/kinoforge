"""Upscale every planned still in a directory on ONE pod.

The shape is :mod:`kinoforge.core.batch`: open :func:`deploy_session` once
and do per-item work inside it, so the boot, the profile verify and the
heartbeat are paid once per directory rather than once per image. ``single``
(``--no-reuse``) is forwarded to the session, whose ``finally`` destroys the
pod exactly once at exit — whatever happened in between.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.cancel import CancelToken
from kinoforge.core.config import Config
from kinoforge.core.errors import (
    BudgetExceeded,
    Cancelled,
    CapabilityMismatch,
    KinoforgeError,
    TeardownError,
)
from kinoforge.core.image_dir import ImageDirItem, ImageDirPlan, prepare_upload
from kinoforge.core.interfaces import (
    ComputeProvider,
    GenerationEngine,
    GenerationRequest,
    Instance,
    PipelineState,
    UpscalerEngine,
)
from kinoforge.core.media import local_artifact
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger("kinoforge.core.upscale_dir")

ItemOutcome = Literal["written", "failed", "aborted"]
"""Terminal state of one pending item."""

ItemCallback = Callable[[ImageDirItem, ItemOutcome, str | None], None]
"""``on_item(item, outcome, reason)`` — fires exactly once per pending item, in order."""

_FATAL: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    Cancelled,
    BudgetExceeded,
    CapabilityMismatch,
    TeardownError,
)
_HEALTH_USER_AGENT = "kinoforge-upscale-dir/0.1"


class PodDead(KinoforgeError):
    """The pod stopped answering ``/health`` after an item failed; the rest were aborted."""


@dataclass(frozen=True)
class ImageDirResult:
    """Per-item outcomes for one directory run.

    Attributes:
        plan: The plan that was executed.
        outcomes: ``(item, outcome, reason)`` for every pending item, in order.
    """

    plan: ImageDirPlan
    outcomes: tuple[tuple[ImageDirItem, ItemOutcome, str | None], ...]

    def _count(self, outcome: ItemOutcome) -> int:
        return sum(1 for _, o, _ in self.outcomes if o == outcome)

    @property
    def written(self) -> int:
        """Items whose output was written."""
        return self._count("written")

    @property
    def failed(self) -> int:
        """Items that raised and were skipped."""
        return self._count("failed")

    @property
    def aborted(self) -> int:
        """Items never attempted because the run stopped."""
        return self._count("aborted")


def _default_health_probe(url: str) -> dict[str, Any]:
    from kinoforge.engines._pod_http import http_json

    return http_json(method="GET", url=url, payload=None, user_agent=_HEALTH_USER_AGENT)


def _health_url(instance: Instance | None) -> str | None:
    if instance is None:
        return None
    endpoints = instance.endpoints or {}
    base = endpoints.get("8000") or next(iter(endpoints.values()), "")
    return f"{base.rstrip('/')}/health" if base else None


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def upscale_image_dir(
    cfg: Config,
    plan: ImageDirPlan,
    *,
    store: ArtifactStore,
    run_id: str,
    state_dir: Path,
    instance: Instance | None = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,
    on_instance_created: Callable[[Instance], None] | None = None,
    on_item: ItemCallback | None = None,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    upscaler: UpscalerEngine | None = None,
    health_probe: Callable[[str], Any] = _default_health_probe,
) -> tuple[ImageDirResult, Instance | None]:
    """Upscale every ``pending`` item of *plan* inside one deploy session.

    Per item: :func:`prepare_upload`, seed a ``PipelineState`` with the
    input artifact (``meta["media"] = "image"``), run the shared
    ``UpscaleStage``, fetch the result bytes, write them atomically to
    ``item.output``. A per-item exception is recorded as ``failed`` and the
    run continues; after any failure the pod's ``/health`` is probed once and,
    if it does not answer, the rest are ``aborted`` and :class:`PodDead` is
    raised. ``KeyboardInterrupt``, ``Cancelled`` and the batch-fatal errors
    abort the rest and re-raise.

    Args:
        cfg: Loaded config; ``cfg.upscale`` must be present.
        plan: From :func:`kinoforge.core.image_dir.plan_image_dir`.
        store: Artifact store (profile cache, ephemeral registration).
        run_id: Namespace tag forwarded to the session.
        state_dir: kinoforge state root.
        instance: Pre-resolved warm pod, or ``None`` to cold-create.
        cancel_token: Cooperative cancellation (the CLI's SIGINT token).
        single: ``--no-reuse`` — the session destroys the pod once at exit.
        on_instance_created: Forwarded to the session (launch-row upgrade).
        on_item: Fires once per pending item, in order, including the
            ``aborted`` ones before an exception propagates.
        provider: Test-injection ``ComputeProvider`` for the session.
        engine: Test-injection ``GenerationEngine`` for the session.
        upscaler: Test-injection ``UpscalerEngine``; defaults to the
            registry's ``cfg.upscale.engine``.
        health_probe: ``(url) -> json``; raises when the pod is unreachable.

    Returns:
        ``(result, instance)`` — the session's instance so the CLI can stamp
        the ledger exactly as it does for a single image.

    Raises:
        PodDead: The pod stopped answering after an item failed.
        Cancelled: The cancel token fired mid-run.
        BudgetExceeded, CapabilityMismatch, TeardownError: Batch-fatal.
    """
    from kinoforge.core import orchestrator as _orch
    from kinoforge.core import registry as _registry
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.scale_target import ScaleTarget
    from kinoforge.pipeline.upscale import UpscaleStage

    if cfg.upscale is None:
        raise ValueError("upscale_image_dir needs an `upscale:` block")
    pending = plan.pending
    outcomes: list[tuple[ImageDirItem, ItemOutcome, str | None]] = []

    def _emit(item: ImageDirItem, outcome: ItemOutcome, reason: str | None) -> None:
        outcomes.append((item, outcome, reason))
        if on_item is not None:
            on_item(item, outcome, reason)

    def _abort_rest(items: tuple[ImageDirItem, ...], reason: str) -> None:
        for it in items:
            _emit(it, "aborted", reason)

    if not pending:
        return ImageDirResult(plan, ()), instance

    with _orch.deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        run_id=run_id,
        state_dir=state_dir,
        instance=instance,
        cancel_token=cancel_token,
        single=single,
        on_instance_created=on_instance_created,
    ) as session:
        eph = EphemeralSession.current()
        if eph is not None:
            eph.register_store(store, run_id)
        up = (
            upscaler
            if upscaler is not None
            else _registry.get_upscaler(cfg.upscale.engine)()
        )
        stage = UpscaleStage(
            engine=up,
            scale=ScaleTarget.parse(cfg.upscale.scale),
            instance=session.instance,
            cfg=_orch._cfg_dict(cfg),  # noqa: SLF001 — same dict generate() hands its stages
            cancel_token=cancel_token,
        )
        with tempfile.TemporaryDirectory(prefix="kf-image-dir-") as scratch_str:
            scratch = Path(scratch_str)
            for idx, item in enumerate(pending):
                try:
                    upload = prepare_upload(item, scratch)
                    state = PipelineState(
                        request=GenerationRequest(prompt="", mode="upscale"),
                        artifacts={"clip": local_artifact(upload, "image")},
                    )
                    state = stage.run(state)
                    body = _orch.fetch_artifact_bytes(state.artifacts["upscaled"])
                    _write_atomic(item.output, body)
                except _FATAL as exc:
                    _abort_rest(pending[idx:], type(exc).__name__)
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad file must not end the run
                    reason = f"{type(exc).__name__}: {exc}"
                    _log.warning("%s failed: %s", item.source, reason)
                    _emit(item, "failed", reason)
                    url = _health_url(session.instance)
                    if url is not None:
                        try:
                            health_probe(url)
                        except Exception as probe_exc:  # noqa: BLE001 — unreachable is the signal
                            rest = pending[idx + 1 :]
                            _abort_rest(rest, "pod stopped answering /health")
                            pod_id = (
                                session.instance.id if session.instance else "<none>"
                            )
                            raise PodDead(
                                f"pod {pod_id} stopped answering /health after "
                                f"{item.source.name} failed ({type(probe_exc).__name__}); "
                                f"{len(rest)} item(s) aborted"
                            ) from exc
                    continue
                _emit(item, "written", None)
        return ImageDirResult(plan, tuple(outcomes)), session.instance
