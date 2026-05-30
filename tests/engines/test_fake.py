"""Tests for the FakeEngine / FakeBackend test substrate.

Each test maps 1-to-1 with an Acceptance Criterion in the Task 8 spec.
"""

import hashlib
import importlib

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    ModelProfile,
    Segment,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_probe() -> ModelProfile:
    return ModelProfile(
        name="test-probe",
        max_frames=24,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _make_job(*prompts: str, spec: dict[str, object] | None = None) -> GenerationJob:
    segments = [Segment(prompt=p) for p in prompts]
    return GenerationJob(spec=spec or {}, segments=segments)


def _expected_filename(*prompts: str) -> str:
    combined = "|".join(prompts)
    hex12 = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:12]
    return f"clip-{hex12}.mp4"


# ---------------------------------------------------------------------------
# AC 1 — Construction and class-level attributes
# ---------------------------------------------------------------------------


def test_fake_engine_constructs_cleanly():
    """FakeEngine constructs without error and exposes class-level attrs."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )
    assert isinstance(engine, GenerationEngine)
    assert FakeEngine.name == "fake"
    assert FakeEngine.requires_compute is True
    assert FakeEngine.requires_local_weights is False
    # Instance attrs match class attrs (no shadowing)
    assert engine.name == "fake"
    assert engine.requires_compute is True
    assert engine.requires_local_weights is False


# ---------------------------------------------------------------------------
# AC 2 — backend().inspect_capabilities() returns the probe profile unchanged
# ---------------------------------------------------------------------------


def test_backend_inspect_capabilities_returns_probe_unchanged():
    """inspect_capabilities returns the injected probe profile with flags unchanged."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )
    backend = engine.backend(instance=None, cfg={})
    assert isinstance(backend, GenerationBackend)
    result = backend.inspect_capabilities()
    assert result is probe  # same object
    # Flags on the probe must remain False (not overwritten by declared_flags)
    assert result.supports_native_extension is False
    assert result.supports_joint_audio is False


# ---------------------------------------------------------------------------
# AC 3 — submit/result: non-empty job_id and deterministic Artifact.filename
# ---------------------------------------------------------------------------


def test_submit_returns_non_empty_job_id():
    """submit() returns a non-empty string."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )
    backend = engine.backend(instance=None, cfg={})
    job = _make_job("a cat on a surfboard")
    job_id = backend.submit(job)
    assert isinstance(job_id, str)
    assert len(job_id) > 0


def test_result_returns_artifact():
    """result() returns an Artifact instance."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )
    backend = engine.backend(instance=None, cfg={})
    job = _make_job("ocean waves at dusk")
    job_id = backend.submit(job)
    artifact = backend.result(job_id)
    assert isinstance(artifact, Artifact)


def test_artifact_filename_is_deterministic_sha256():
    """filename is clip-<sha256[:12]>.mp4 derived from joined segment prompts."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )
    backend = engine.backend(instance=None, cfg={})

    prompts = ("a mountain sunrise", "timelapse clouds")
    job = _make_job(*prompts)
    job_id = backend.submit(job)
    artifact = backend.result(job_id)

    expected = _expected_filename(*prompts)
    assert artifact.filename == expected


def test_artifact_filename_same_on_two_submit_result_rounds():
    """Two submit→result round-trips on equivalent jobs yield the same filename."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )

    prompts = ("first clip", "second clip")

    backend1 = engine.backend(instance=None, cfg={})
    job1 = _make_job(*prompts)
    a1 = backend1.result(backend1.submit(job1))

    backend2 = engine.backend(instance=None, cfg={})
    job2 = _make_job(*prompts)
    a2 = backend2.result(backend2.submit(job2))

    assert a1.filename == a2.filename


# ---------------------------------------------------------------------------
# AC 4 — validate_spec raises ValidationError on missing required keys
# ---------------------------------------------------------------------------


def test_validate_spec_passes_when_all_required_keys_present():
    """validate_spec does not raise when the spec contains all required keys."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys={"model", "steps"},
    )
    job = _make_job("a rainy street", spec={"model": "v1", "steps": 50})
    engine.validate_spec(job)  # must not raise


def test_validate_spec_raises_validation_error_on_missing_key():
    """validate_spec raises ValidationError when a required key is absent."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys={"model", "steps"},
    )
    job = _make_job("a rainy street", spec={"model": "v1"})  # missing "steps"
    with pytest.raises(ValidationError):
        engine.validate_spec(job)


def test_validate_spec_raises_for_each_missing_key():
    """ValidationError message names at least one missing key."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys={"model", "steps", "seed"},
    )
    job = _make_job("test", spec={})
    with pytest.raises(ValidationError) as exc_info:
        engine.validate_spec(job)
    msg = str(exc_info.value)
    # At least one missing key should appear in the error message
    assert any(k in msg for k in ("model", "steps", "seed"))


# ---------------------------------------------------------------------------
# AC 5 — declared_flags returns the injected map (or {} for unknown keys)
# ---------------------------------------------------------------------------


def test_declared_flags_returns_stored_map():
    """declared_flags returns the map for a registered CapabilityKey."""
    from kinoforge.engines.fake import FakeEngine

    key = CapabilityKey(base_model="hf:org/m", engine="fake")
    flags = {"supports_native_extension": True}
    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={key.derive(): flags},
        required_spec_keys=set(),
    )
    assert engine.declared_flags(key) == flags


def test_declared_flags_returns_empty_for_unknown_key():
    """declared_flags returns {} for a key not in the map."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    key = CapabilityKey(base_model="hf:unknown/model", engine="fake")
    assert engine.declared_flags(key) == {}


# ---------------------------------------------------------------------------
# AC 6 — profile_for raises NotImplementedError (DEFERRED)
# ---------------------------------------------------------------------------


def test_profile_for_raises_not_implemented():
    """profile_for raises NotImplementedError until Task 12 wires the real cache."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    key = CapabilityKey(base_model="hf:org/m", engine="fake")
    with pytest.raises(NotImplementedError):
        engine.profile_for(key)


# ---------------------------------------------------------------------------
# AC 7 — provision is a no-op
# ---------------------------------------------------------------------------


def test_provision_is_noop():
    """provision() completes without error and returns None."""
    from kinoforge.engines.fake import FakeEngine

    engine = FakeEngine(
        probe_profile=_make_probe(),
        declared_flags_map={},
        required_spec_keys=set(),
    )
    engine.provision(instance=None, cfg={})  # must not raise; returns None


# ---------------------------------------------------------------------------
# AC 8 — importing the module registers the engine in the registry
# ---------------------------------------------------------------------------


def test_import_registers_fake_engine_in_registry():
    """Importing kinoforge.engines.fake registers 'fake' in the engine registry."""
    # Ensure the module is imported (may already be from earlier tests)
    importlib.import_module("kinoforge.engines.fake")

    from kinoforge.core import registry

    factory = registry.get_engine("fake")
    engine = factory()
    assert isinstance(engine, GenerationEngine)
    assert engine.name == "fake"


def test_default_probe_is_plausible():
    """The default-registered fake engine has a plausible ModelProfile."""
    importlib.import_module("kinoforge.engines.fake")

    from kinoforge.core import registry

    engine = registry.get_engine("fake")()
    backend = engine.backend(instance=None, cfg={})
    profile = backend.inspect_capabilities()
    assert profile.name == "fake"
    assert profile.max_frames > 0
    assert profile.fps > 0
    assert len(profile.supported_modes) > 0
    assert profile.max_resolution[0] > 0 and profile.max_resolution[1] > 0


# ---------------------------------------------------------------------------
# Extra — capabilities() and endpoints() contract
# ---------------------------------------------------------------------------


def test_backend_capabilities_returns_model_profile():
    """capabilities() returns a ModelProfile."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )
    backend = engine.backend(instance=None, cfg={})
    caps = backend.capabilities()
    assert isinstance(caps, ModelProfile)


def test_backend_endpoints_returns_dict_with_generate():
    """endpoints() returns at least a 'generate' key."""
    from kinoforge.engines.fake import FakeEngine

    probe = _make_probe()
    engine = FakeEngine(
        probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
    )
    backend = engine.backend(instance=None, cfg={})
    eps = backend.endpoints()
    assert "generate" in eps


# ---------------------------------------------------------------------------
# extract_last_frame override
# ---------------------------------------------------------------------------


def test_fake_engine_extract_last_frame_returns_init_image_asset() -> None:
    """FakeEngine.extract_last_frame returns a deterministic init_image asset.

    Bug this catches: override returns wrong kind/role, or filename is not
    derived deterministically from input (breaks cross-instance test
    reproducibility).
    """
    from kinoforge.core.interfaces import (
        Artifact,
        ConditioningAsset,
        ModelProfile,
    )
    from kinoforge.engines.fake import FakeEngine

    probe = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    engine = FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )

    input_artifact = Artifact(filename="clip-deadbeef0123.mp4")
    asset = engine.extract_last_frame(input_artifact)

    assert isinstance(asset, ConditioningAsset)
    assert asset.kind == "image"
    assert asset.role == "init_image"
    assert asset.ref.filename == "clip-deadbeef0123.mp4.tail.png"
    assert asset.ref.meta == {"derived_from": "clip-deadbeef0123.mp4"}
