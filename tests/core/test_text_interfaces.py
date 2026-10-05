"""The TextEngine seam: dataclasses, ABC defaults, registry trio, modes, error."""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import TextGenerationFailed, UnknownAdapter
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    TextEngine,
    TextHealth,
    TextJob,
    TextResult,
    required_image_roles,
)


class _MinimalEngine(TextEngine):
    """Smallest concrete TextEngine — exercises the ABC's default methods."""

    name = "minimal"
    requires_compute = False

    def health(self, instance, cfg):  # noqa: ANN001, ANN201
        return TextHealth(ready=True, model="m", supported_modes=frozenset({"t2t"}))

    def upload_image(self, instance, local_path, cfg):  # noqa: ANN001, ANN201
        return "/tmp/x.png"

    def complete(self, instance, job, cfg, *, cancel_token=None):  # noqa: ANN001, ANN201
        return TextResult(
            text="t", finish_reason="stop", usage={}, model="m", elapsed_s=0.0
        )

    def validate_spec(self, job):  # noqa: ANN001, ANN201
        return None

    def model_identity(self, cfg):  # noqa: ANN001, ANN201
        return "m"


@pytest.fixture
def _clean_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_text_engines", {})


def test_text_modes_require_no_image_roles() -> None:
    """Bug caught: validate_request KeyErrors on an unknown mode, or a mode
    table entry that demands a role no text request carries."""
    assert MODE_ROLE_REQUIREMENTS["t2t"] == {}
    assert MODE_ROLE_REQUIREMENTS["it2t"] == {}
    assert required_image_roles("t2t") == []
    assert required_image_roles("it2t") == []


def test_text_job_defaults_are_text_only() -> None:
    """Bug caught: a mutable default shared across jobs, or images defaulting to None."""
    job = TextJob(prompt="hi")
    assert job.images == ()
    assert job.system is None
    assert job.params == {}
    assert TextJob(prompt="a").params is not TextJob(prompt="b").params


def test_render_provision_default_raises() -> None:
    """Bug caught: a default that returns an empty fragment, hiding a missing override."""
    with pytest.raises(NotImplementedError):
        _MinimalEngine().render_provision({})


@pytest.mark.usefixtures("_clean_registry")
def test_duplicate_text_engine_registration_is_rejected() -> None:
    """Bug caught: overwrite semantics (register_engine's) silently rebinding
    the production text engine on an import-order accident."""
    registry.register_text_engine("minimal", _MinimalEngine)
    with pytest.raises(UnknownAdapter, match="already registered"):
        registry.register_text_engine("minimal", _MinimalEngine)


@pytest.mark.usefixtures("_clean_registry")
def test_unknown_text_engine_names_the_known_ones() -> None:
    """Bug caught: a bare KeyError, or a message that does not say what IS registered."""
    registry.register_text_engine("minimal", _MinimalEngine)
    with pytest.raises(
        UnknownAdapter, match=r"no text engine registered as 'nope'.*minimal"
    ):
        registry.get_text_engine("nope")
    assert registry.text_engine_names() == ["minimal"]
    assert registry.get_text_engine("minimal") is _MinimalEngine


def test_text_generation_failed_carries_job_and_server_error() -> None:
    """Bug caught: an error whose str() loses the pod's own message."""
    exc = TextGenerationFailed("j1", "CUDA out of memory")
    assert exc.job_id == "j1"
    assert exc.server_error == "CUDA out of memory"
    assert "j1" in str(exc)
    assert "CUDA out of memory" in str(exc)
