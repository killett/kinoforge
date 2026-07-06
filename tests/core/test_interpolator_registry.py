"""Interpolator registry + interface shape."""

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    InterpolateJob,
    InterpolateResult,
    InterpolatorEngine,
)


class _FakeRife(InterpolatorEngine):
    name = "fake-rife"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def provision(self, instance, cfg, *, cancel_token=None): ...

    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        raise NotImplementedError

    def validate_spec(self, job): ...

    def model_identity(self, cfg):
        return "fake-rife"


def test_register_and_get_roundtrip():
    name = "fake-rife-rt"
    registry.register_interpolator(name, _FakeRife)
    assert registry.get_interpolator(name)().name == "fake-rife"
    assert name in registry.interpolator_names()


def test_duplicate_registration_rejected():
    registry.register_interpolator("dup-rife", _FakeRife)
    with pytest.raises(UnknownAdapter):
        registry.register_interpolator("dup-rife", _FakeRife)


def test_unknown_get_raises():
    with pytest.raises(UnknownAdapter):
        registry.get_interpolator("nope-rife")


def test_job_and_result_fields():
    job = InterpolateJob(source=Artifact(uri="file:///in.mp4"), target_fps=60.0)
    assert job.target_fps == 60.0
    res = InterpolateResult(
        artifact=Artifact(uri="file:///out.mp4"),
        input_fps=16.0,
        output_fps=60.0,
        input_frame_count=16,
        output_frame_count=60,
        elapsed_s=1.0,
    )
    assert res.output_fps == 60.0
