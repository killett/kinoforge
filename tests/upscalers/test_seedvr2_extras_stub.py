"""Tests that SeedVR2Engine raises ExtrasNotInstalled until Phase 2 vendoring lands."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ExtrasNotInstalled
from kinoforge.core.interfaces import Artifact, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.seedvr2 import SeedVR2Engine


def _fake_job() -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
    )


def _fake_cfg() -> dict[str, object]:
    return {
        "upscale": {
            "engine": "seedvr2",
            "scale": "2x",
            "seedvr2": {"variant": "3B", "precision": "fp8"},
        }
    }


class TestExtrasNotInstalled:
    def test_render_provision_raises(self) -> None:
        # Bug caught: SeedVR2Engine.render_provision still calls
        # `pip install seedvr @ git+...` against the un-installable
        # upstream — pod boot fails with opaque error. Stub-raise
        # surfaces the gap at cfg-time instead.
        with pytest.raises(ExtrasNotInstalled, match="seedvr"):
            SeedVR2Engine().render_provision(_fake_cfg())

    def test_provision_raises(self) -> None:
        # Bug caught: post-boot provision call is also a code path we
        # cannot honour without the vendored upstream.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().provision(None, _fake_cfg())

    def test_upscale_raises(self) -> None:
        # Bug caught: the runtime upscale call would import SeedVR2Runtime
        # which would import the unavailable seedvr package.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().upscale(None, _fake_job(), _fake_cfg())

    def test_validate_spec_raises(self) -> None:
        # Bug caught: cfg-time validation could otherwise proceed past
        # a SeedVR2 cfg and waste cold-boot budget.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().validate_spec(_fake_job())


class TestStillFunctionalSurfaces:
    def test_model_identity_still_works(self) -> None:
        # Bug caught: model_identity is pure cfg-parsing — must NOT
        # raise so the ABC contract test stays GREEN. Used by the
        # output-sink filename schema.
        assert SeedVR2Engine().model_identity(_fake_cfg()) == "seedvr2-3b-fp8"

    def test_module_import_has_no_side_effects(self) -> None:
        # Bug caught: a regression that puts `from seedvr.inference import ...`
        # back at module-top of _runtime.py would crash `import kinoforge`
        # entirely on hosts without seedvr installed.
        import importlib

        import kinoforge.upscalers.seedvr2 as mod

        importlib.reload(mod)  # idempotent re-import must not raise
        from kinoforge.core import registry

        assert "seedvr2" in registry.upscaler_names()
