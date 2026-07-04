"""Tests for LumaAgentsImageEngine + backend (agents API, raw REST)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import ImageJob


class _FakeLumaHttp:
    """Records requests; replays canned generation states."""

    def __init__(
        self,
        *,
        submit_response: dict[str, Any] | None = None,
        get_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.submit_response = submit_response or {"id": "gen-1", "state": "dreaming"}
        self.get_responses = list(get_responses or [])
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append((path, body))
        return self.submit_response

    def get_json(self, path: str) -> dict[str, Any]:
        self.get_calls.append(path)
        return self.get_responses.pop(0)

    def delete(self, path: str) -> None:
        self.delete_calls.append(path)


def _backend(fake: _FakeLumaHttp) -> Any:
    from kinoforge.image_engines.luma_agents import LumaAgentsImageBackend

    return LumaAgentsImageBackend(client_factory=lambda: fake, sleep=lambda _s: None)


def _job(**spec_extra: Any) -> ImageJob:
    return ImageJob(
        spec={"model": "photon-1", **spec_extra},
        prompt="a lighthouse at dawn",
    )


def test_submit_posts_image_generation_body() -> None:
    """Bug caught: wrong endpoint path or ref fields leaking into the body."""
    fake = _FakeLumaHttp()
    job_id = _backend(fake).submit(_job(params={"aspect_ratio": "16:9"}))
    assert job_id == "gen-1"
    path, body = fake.post_calls[0]
    assert path == "/v1/generations"
    assert body == {
        "prompt": "a lighthouse at dawn",
        "model": "photon-1",
        "type": "image",
        "aspect_ratio": "16:9",
    }
    assert "image_ref" not in body and "style_ref" not in body


def test_poll_dreaming_then_completed_returns_image_artifact() -> None:
    """Bug caught: treating 'dreaming' as terminal, or reading the wrong
    assets key (assets.video is null for image generations)."""
    fake = _FakeLumaHttp(
        get_responses=[
            {"id": "gen-1", "state": "dreaming", "assets": None},
            {
                "id": "gen-1",
                "state": "completed",
                "output": [{"url": "https://cdn.luma/img.png"}],
            },
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    art = backend.result("gen-1")
    assert art.url == "https://cdn.luma/img.png"
    assert fake.get_calls == [
        "/v1/generations/gen-1",
        "/v1/generations/gen-1",
    ]


def test_poll_failed_raises_with_failure_reason() -> None:
    """Bug caught: swallowing failure_reason strands live-smoke debugging."""
    fake = _FakeLumaHttp(
        get_responses=[
            {"id": "gen-1", "state": "failed", "failure_reason": "nsfw filter"}
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    with pytest.raises(KinoforgeError, match="nsfw filter"):
        backend.result("gen-1")


def test_delete_raises_not_implemented() -> None:
    """Bug caught: ephemeral mode silently 'succeeding' a delete against
    an API with NO documented DELETE endpoint — must raise loudly so the
    ephemeral policy surfaces the manual-cleanup URL instead."""
    fake = _FakeLumaHttp()
    backend = _backend(fake)
    with pytest.raises(NotImplementedError, match="dashboard"):
        backend._inner._delete("gen-9")
    assert fake.delete_calls == []


def test_validate_spec_requires_model_and_prompt() -> None:
    from kinoforge.image_engines.luma_agents import LumaAgentsImageEngine

    engine = LumaAgentsImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    with pytest.raises(ValidationError, match="spec.model"):
        engine.validate_spec(ImageJob(spec={}, prompt="p"))
    with pytest.raises(ValidationError, match="prompt"):
        engine.validate_spec(ImageJob(spec={"model": "photon-1"}, prompt=""))


def test_backend_without_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.image_engines.luma_agents import LumaAgentsImageEngine

    monkeypatch.delenv("LUMAAI_API_KEY", raising=False)
    engine = LumaAgentsImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    with pytest.raises(AuthError):
        engine.backend(None, {})


def test_provision_rejects_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: hosted image engine silently accepting a compute pod."""
    from kinoforge.image_engines.luma_agents import LumaAgentsImageEngine

    monkeypatch.setenv("LUMAAI_API_KEY", "k")
    engine = LumaAgentsImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    with pytest.raises(KinoforgeError, match="instance must be None"):
        engine.provision(object(), {})  # type: ignore[arg-type]


def test_model_identity_reads_spec_model() -> None:
    from kinoforge.image_engines.luma_agents import LumaAgentsImageEngine

    engine = LumaAgentsImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    assert engine.model_identity({"spec": {"model": "uni-1.1"}}) == "uni-1.1"
    assert engine.model_identity({}) == ""


def test_registry_registration() -> None:
    import kinoforge._adapters  # noqa: F401 — side-effect registration
    from kinoforge.core import registry
    from kinoforge.image_engines.luma_agents import LumaAgentsImageEngine

    assert isinstance(registry.get_image_engine("luma_agents")(), LumaAgentsImageEngine)


def test_extract_output_url_assets_image_fallback() -> None:
    """Bug caught: docs vs wire drift — if the agents API ever returns the
    legacy dream-machine `assets.image` shape, the URL must still extract."""
    fake = _FakeLumaHttp(
        get_responses=[
            {
                "id": "gen-1",
                "state": "completed",
                "assets": {"video": None, "image": "https://cdn.luma/legacy.png"},
            }
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    art = backend.result("gen-1")
    assert art.url == "https://cdn.luma/legacy.png"
