"""Tests for register_upscaler / get_upscaler / upscaler_names."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import (
    StageMismatch,
    UnknownAdapter,
    UpscaleFailed,
    VRAMEvictionFailed,
)
from kinoforge.core.interfaces import UpscalerEngine

if TYPE_CHECKING:
    pass


class _FakeEngine(UpscalerEngine):
    """Minimal concrete impl for registry tests."""

    name = "_fake_upscaler"
    requires_compute = False
    requires_local_weights = False
    supported_scales = ()

    def provision(self, instance, cfg, *, cancel_token=None):
        return None

    def upscale(self, instance, job, cfg, *, cancel_token=None):
        raise NotImplementedError

    def validate_spec(self, job):
        return None

    def model_identity(self, cfg):
        return "_fake_upscaler"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/restore the module-level _upscalers dict so tests don't leak."""
    snap = dict(registry._upscalers)
    registry._upscalers.clear()
    yield
    registry._upscalers.clear()
    registry._upscalers.update(snap)


class TestRegisterUpscaler:
    def test_register_and_get(self) -> None:
        registry.register_upscaler("fake", _FakeEngine)
        eng = registry.get_upscaler("fake")()
        assert eng.name == "_fake_upscaler"

    def test_duplicate_raises(self) -> None:
        registry.register_upscaler("fake", _FakeEngine)
        with pytest.raises(UnknownAdapter, match="already registered"):
            registry.register_upscaler("fake", _FakeEngine)

    def test_get_missing_raises(self) -> None:
        with pytest.raises(UnknownAdapter, match="no upscaler registered"):
            registry.get_upscaler("nope")

    def test_names_sorted(self) -> None:
        registry.register_upscaler("zeta", _FakeEngine)
        registry.register_upscaler("alpha", _FakeEngine)
        assert registry.upscaler_names() == ["alpha", "zeta"]


class TestNewErrors:
    def test_upscale_failed_carries_context(self) -> None:
        err = UpscaleFailed(job_id="j-123", server_error="cuda OOM")
        assert "j-123" in str(err)
        assert "cuda OOM" in str(err)
        assert err.job_id == "j-123"
        assert err.server_error == "cuda OOM"

    def test_vram_eviction_failed_carries_context(self) -> None:
        err = VRAMEvictionFailed(model="seedvr2-7b", reason="target exceeds GPU")
        assert "seedvr2-7b" in str(err)
        assert err.model == "seedvr2-7b"
        assert err.reason == "target exceeds GPU"

    def test_stage_mismatch_carries_axes(self) -> None:
        err = StageMismatch(want=("t2v", "upscale"), have=("t2v",))
        assert err.want == ("t2v", "upscale")
        assert err.have == ("t2v",)
        assert "t2v" in str(err)
