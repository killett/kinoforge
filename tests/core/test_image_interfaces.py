"""Layer R T1: image-side ABCs + helper smoke tests."""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import (
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    required_image_roles,
)


def test_image_profile_fields() -> None:
    p = ImageProfile(name="x", max_resolution=(1024, 1024), supported_modes={"t2i"})
    assert p.name == "x"
    assert p.max_resolution == (1024, 1024)
    assert p.supported_modes == {"t2i"}


def test_image_job_minimal() -> None:
    j = ImageJob(spec={"model": "m"}, prompt="hello")
    assert j.spec == {"model": "m"}
    assert j.prompt == "hello"
    assert j.params == {}


def test_image_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        ImageBackend()  # type: ignore[abstract]


def test_image_engine_is_abstract() -> None:
    with pytest.raises(TypeError):
        ImageEngine()  # type: ignore[abstract]


def test_required_image_roles_dispatch() -> None:
    """For each known mode the helper returns the image-kind roles in
    insertion order. Schema-shape-agnostic: works whether MODE_ROLE_REQUIREMENTS
    is dict[str, set[str]] (pre-T2) or dict[str, dict[str, str]] (post-T2).
    Bug guard: a flf2v that loses ordering would break continuity dispatch.
    """
    assert required_image_roles("t2v") == []
    assert required_image_roles("i2v") == ["init_image"]
    assert required_image_roles("flf2v") == ["first_frame", "last_frame"]
    assert required_image_roles("unknown") == []
