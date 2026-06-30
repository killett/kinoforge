"""SeedVR2Engine — extras-gated stub until Phase 2 vendoring lands.

The upstream ``ByteDance-Seed/SeedVR`` repository ships as research
scripts with no ``setup.py`` / ``pyproject.toml``, so ``pip install
seedvr @ git+...`` is not feasible. Until the Phase 2 workstream
vendors ``projects/inference_seedvr2_*.py`` + ``common/`` + ``models/``
into ``src/kinoforge/upscalers/seedvr2/_vendored/``, the four
heavyweight ABC methods (:meth:`render_provision`, :meth:`provision`,
:meth:`upscale`, :meth:`validate_spec`) raise :class:`ExtrasNotInstalled`.

:meth:`model_identity` stays pure cfg-parse so the output-sink filename
schema and the parametrized ABC contract test still pass.

The class still self-registers under ``"seedvr2"``; cfg-time
:mod:`kinoforge.validation` checks reject the engine choice before any
pod boot so the extras gap surfaces with a structured error rather than
an opaque bootstrap crash.
"""

from __future__ import annotations

from typing import Any, cast

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ExtrasNotInstalled, UnknownAdapter
from kinoforge.core.interfaces import (
    Instance,
    RenderedProvision,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget

_SUPPORTED_FACTORS: tuple[float, ...] = (2.0, 4.0)

_EXTRAS_HINT = (
    "video-coherent upscaling (SeedVR2) pending Phase 2 vendoring; "
    "use cfg.upscale.engine = 'spandrel' for v1, or track the Phase 2 "
    "workstream"
)


class SeedVR2Engine(UpscalerEngine):
    """SeedVR2 video upscaler — extras-gated stub until Phase 2 vendoring lands."""

    name = "seedvr2"
    requires_compute = True
    requires_local_weights = True
    supported_scales = tuple(
        ScaleTarget(kind="factor", value=v) for v in _SUPPORTED_FACTORS
    )

    def validate_spec(self, job: UpscaleJob) -> None:
        """Stub: raise ExtrasNotInstalled until Phase 2 vendoring lands."""
        del job
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return sink-filename slug ``"seedvr2-{variant}-{precision}"`` or ``""``.

        Pure cfg-parsing — must stay functional so the ABC contract test
        passes and the output-sink filename schema works even though the
        heavyweight ABC methods raise.
        """
        try:
            upscale_block = cast(dict[str, Any], cfg["upscale"])
            seedvr2_block = cast(dict[str, Any], upscale_block["seedvr2"])
            variant = str(seedvr2_block["variant"]).lower()
            precision = str(seedvr2_block["precision"])
            return f"seedvr2-{variant}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Stub: raise ExtrasNotInstalled until Phase 2 vendoring lands."""
        del cfg
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Stub: raise ExtrasNotInstalled until Phase 2 vendoring lands."""
        del instance, cfg, cancel_token
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        """Stub: raise ExtrasNotInstalled until Phase 2 vendoring lands."""
        del instance, job, cfg, cancel_token
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)


try:
    registry.register_upscaler("seedvr2", SeedVR2Engine)
except UnknownAdapter:
    pass
