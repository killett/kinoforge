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
    """ImageProfile stores all three fields without transformation.
    Bug guard: field rename or silent default would break profile-cache reads."""
    p = ImageProfile(name="x", max_resolution=(1024, 1024), supported_modes={"t2i"})
    assert p.name == "x"
    assert p.max_resolution == (1024, 1024)
    assert p.supported_modes == {"t2i"}


def test_image_job_minimal() -> None:
    """ImageJob carries spec + prompt and defaults params to empty dict.
    Bug guard: missing default_factory on params would raise or share state."""
    j = ImageJob(spec={"model": "m"}, prompt="hello")
    assert j.spec == {"model": "m"}
    assert j.prompt == "hello"
    assert j.params == {}


def test_image_backend_is_abstract() -> None:
    """ImageBackend must reject direct instantiation.
    Bug guard: silently dropping ABC base would let partial implementations instantiate."""
    with pytest.raises(TypeError):
        ImageBackend()  # type: ignore[abstract]


def test_image_engine_is_abstract() -> None:
    """ImageEngine must reject direct instantiation.
    Bug guard: silently dropping ABC base would let partial implementations instantiate."""
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


def test_register_and_get_image_engine_round_trip() -> None:
    """Bug guard: registry mediation breaks if register/get use different dicts or different key normalisation."""
    from kinoforge.core import registry
    from kinoforge.core.interfaces import (
        Artifact,
        CapabilityKey,
        ImageBackend,
        ImageEngine,
        ImageProfile,
        Instance,
    )

    class _StubBackend(ImageBackend):
        def capabilities(self) -> ImageProfile:
            return ImageProfile(name="x", max_resolution=(1, 1), supported_modes=set())

        def inspect_capabilities(self) -> ImageProfile:
            return self.capabilities()

        def submit(self, job: ImageJob) -> str:
            return "id"

        def result(self, job_id: str) -> Artifact:
            return Artifact(filename="x.png")

        def endpoints(self) -> dict[str, str]:
            return {}

    class _StubEngine(ImageEngine):
        name = "_stub_T1_test"
        requires_compute = False
        requires_local_weights = False

        def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
            return None

        def backend(
            self, instance: Instance | None, cfg: dict[str, object]
        ) -> ImageBackend:
            return _StubBackend()

        def profile_for(self, key: CapabilityKey) -> ImageProfile:
            return _StubBackend().capabilities()

        def validate_spec(self, job: ImageJob) -> None:
            return None

        def model_identity(self, cfg: dict[str, object]) -> str:  # noqa: D102
            return ""

    registry.register_image_engine("_stub_T1_test", lambda: _StubEngine())
    factory = registry.get_image_engine("_stub_T1_test")
    assert factory().name == "_stub_T1_test"


def test_get_image_engine_unknown_raises() -> None:
    """Bug guard: silent KeyError on unknown name would surface as opaque traceback far from the typo."""
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    with pytest.raises(UnknownAdapter):
        registry.get_image_engine("does-not-exist-T1")
