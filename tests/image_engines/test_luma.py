"""Tests for LumaImageEngine + LumaImageBackend (raw-REST, no SDK)."""

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
    from kinoforge.image_engines.luma import LumaImageBackend

    return LumaImageBackend(client_factory=lambda: fake, sleep=lambda _s: None)


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
    assert path == "/dream-machine/v1/generations/image"
    assert body == {
        "prompt": "a lighthouse at dawn",
        "model": "photon-1",
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
                "assets": {"video": None, "image": "https://cdn.luma/img.png"},
            },
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    art = backend.result("gen-1")
    assert art.url == "https://cdn.luma/img.png"
    assert fake.get_calls == [
        "/dream-machine/v1/generations/gen-1",
        "/dream-machine/v1/generations/gen-1",
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


def test_delete_calls_generation_endpoint() -> None:
    """Bug caught: _delete left as a scaffold raise (replicate-sibling
    copy/paste) — Luma documents DELETE and ephemeral mode relies on it."""
    fake = _FakeLumaHttp()
    backend = _backend(fake)
    backend._inner._delete("gen-9")
    assert fake.delete_calls == ["/dream-machine/v1/generations/gen-9"]


def test_validate_spec_requires_model_and_prompt() -> None:
    from kinoforge.image_engines.luma import LumaImageEngine

    engine = LumaImageEngine(
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
    from kinoforge.image_engines.luma import LumaImageEngine

    monkeypatch.delenv("LUMAAI_API_KEY", raising=False)
    engine = LumaImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    with pytest.raises(AuthError):
        engine.backend(None, {})


def test_provision_rejects_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: hosted image engine silently accepting a compute pod."""
    from kinoforge.image_engines.luma import LumaImageEngine

    monkeypatch.setenv("LUMAAI_API_KEY", "k")
    engine = LumaImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    with pytest.raises(KinoforgeError, match="instance must be None"):
        engine.provision(object(), {})  # type: ignore[arg-type]


def test_model_identity_reads_spec_model() -> None:
    from kinoforge.image_engines.luma import LumaImageEngine

    engine = LumaImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider()
        )
    )
    assert engine.model_identity({"spec": {"model": "uni-1.1"}}) == "uni-1.1"
    assert engine.model_identity({}) == ""


def test_registry_registration() -> None:
    import kinoforge._adapters  # noqa: F401 — side-effect registration
    from kinoforge.core import registry
    from kinoforge.image_engines.luma import LumaImageEngine

    assert isinstance(registry.get_image_engine("luma")(), LumaImageEngine)
