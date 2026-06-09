"""Layer R T6: FakeImageEngine tests."""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageEngine,
    ImageJob,
    ImageProfile,
)


def _engine() -> ImageEngine:
    importlib.import_module("kinoforge.image_engines.fake")
    return registry.get_image_engine("fake")()


def test_self_registers_under_fake() -> None:
    """Module import side-effects must populate the registry.
    Bug guard: a missing register_image_engine call leaves consumers stranded."""
    importlib.import_module("kinoforge.image_engines.fake")
    factory = registry.get_image_engine("fake")
    eng = factory()
    assert eng.name == "fake"


def test_engine_flags() -> None:
    """Hosted-shape flags: no compute, no local weights.
    Bug guard: requires_compute=True would force orchestrator to spin up a paid GPU pod."""
    eng = _engine()
    assert eng.requires_compute is False
    assert eng.requires_local_weights is False


def test_submit_id_deterministic_for_same_inputs() -> None:
    """Same (prompt, spec) → same submit id (16 hex chars).
    Bug guard: nondeterministic ids break replay-based tests."""
    eng = _engine()
    backend = eng.backend(None, {})
    id1 = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    id2 = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    assert id1 == id2
    assert len(id1) == 16


def test_submit_id_differs_for_different_prompts() -> None:
    """Different prompts must produce different ids.
    Bug guard: collision would cause persistence overwrites."""
    eng = _engine()
    backend = eng.backend(None, {})
    a = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    b = backend.submit(ImageJob(spec={"model": "m"}, prompt="dog"))
    assert a != b


def test_result_returns_filename_matching_id() -> None:
    """Artifact.filename is keyed off the submit id.
    Bug guard: a hash mismatch between submit and result would break end-to-end traceability."""
    eng = _engine()
    backend = eng.backend(None, {})
    job_id = backend.submit(ImageJob(spec={"model": "m"}, prompt="x"))
    art = backend.result(job_id)
    assert isinstance(art, Artifact)
    assert art.filename == f"fake-image-{job_id}.png"


def test_validate_spec_missing_model_raises() -> None:
    """validate_spec rejects jobs without required spec keys.
    Bug guard: silent acceptance leads to malformed downstream HTTP bodies."""
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={}, prompt="x"))


def test_profile_for_returns_default_image_profile() -> None:
    """Default ImageProfile shape matches spec (1024x1024 t2i).
    Bug guard: a regression to video-shaped fields (max_frames/fps) would crash discover."""
    eng = _engine()
    p = eng.profile_for(CapabilityKey(base_model="m", engine="fake"))
    assert isinstance(p, ImageProfile)
    assert p.max_resolution == (1024, 1024)
    assert "t2i" in p.supported_modes


# ---------------------------------------------------------------------------
# Layer 8 — model_identity
# ---------------------------------------------------------------------------


def test_fake_image_engine_model_identity_returns_spec_model_slug() -> None:
    """FakeImageEngine reads model slug from spec.model for offline test pins.

    Bug catch: returns empty string even when spec.model is set, breaking
    per-engine assertions in downstream orchestrator tests.
    """
    eng = _engine()
    cfg: dict[str, object] = {"spec": {"model": "fake-image"}}
    assert eng.model_identity(cfg) == "fake-image"


def test_fake_image_engine_model_identity_empty_on_missing_spec() -> None:
    """FakeImageEngine returns empty string when spec or model is absent.

    Bug catch: KeyError raised on bare cfg breaks slug derivation for all image jobs.
    """
    eng = _engine()
    assert eng.model_identity({}) == ""
    assert eng.model_identity({"spec": {}}) == ""
