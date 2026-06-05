"""Layer R T7: FalImageEngine offline tests with injected HTTP seams."""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    CapabilityKey,
    ImageJob,
    ImageProfile,
)


def _engine_module():
    return importlib.import_module("kinoforge.image_engines.fal")


def _engine():
    _engine_module()
    return registry.get_image_engine("fal")()


def test_self_registers_under_fal() -> None:
    eng = _engine()
    assert eng.name == "fal"


def test_provision_without_fal_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAL_KEY", raising=False)
    eng = _engine()
    with pytest.raises(AuthError):
        eng.provision(None, {})


def test_provision_with_fal_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAL_KEY", "tk-test")
    eng = _engine()
    eng.provision(None, {})  # no raise


def test_profile_for_static_shape() -> None:
    eng = _engine()
    p = eng.profile_for(CapabilityKey(base_model="fal-ai/flux-schnell", engine="fal"))
    assert isinstance(p, ImageProfile)
    assert p.max_resolution == (1024, 1024)
    assert p.supported_modes == {"t2i"}
    assert p.name == "fal-ai/flux-schnell"


def test_validate_spec_empty_prompt_raises() -> None:
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={"model": "x"}, prompt=""))


def test_validate_spec_missing_model_raises() -> None:
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={}, prompt="x"))


def _build_backend(monkeypatch, http_post=None, http_get=None, sleep=None):
    monkeypatch.setenv("FAL_KEY", "tk-test")
    from kinoforge.image_engines.fal import FalImageBackend

    profile = ImageProfile(
        name="fal", max_resolution=(1024, 1024), supported_modes={"t2i"}
    )
    return FalImageBackend(
        cfg={"model": "fal-ai/flux-schnell"},
        creds=EnvCredentialProvider(),
        profile_to_return=profile,
        http_post=http_post or (lambda url, body, headers: {"request_id": "req-1"}),
        http_get=http_get or (lambda url, headers: {}),
        sleep=sleep or (lambda s: None),
    )


def test_submit_posts_with_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object], dict[str, object]]] = []

    def post(url, body, headers):
        calls.append((url, dict(body), dict(headers)))
        return {"request_id": "req-1"}

    backend = _build_backend(monkeypatch, http_post=post)
    rid = backend.submit(ImageJob(spec={"model": "fal-ai/flux-schnell"}, prompt="cat"))
    assert rid == "req-1"
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == "https://queue.fal.run/fal-ai/flux-schnell"
    assert body == {"prompt": "cat"}
    assert headers["Authorization"] == "Key tk-test"
    assert headers["Content-Type"] == "application/json"


def test_submit_merges_spec_input_and_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: spec.input + params merge order must be explicit."""
    captured = {}

    def post(url, body, headers):
        captured.update(body)
        return {"request_id": "r"}

    backend = _build_backend(monkeypatch, http_post=post)
    backend.submit(
        ImageJob(
            spec={"model": "fal-ai/flux-schnell", "input": {"image_size": "square_hd"}},
            prompt="x",
            params={"seed": 42},
        )
    )
    assert captured == {"prompt": "x", "image_size": "square_hd", "seed": 42}


def test_submit_no_fal_key_raises_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAL_KEY", raising=False)
    from kinoforge.image_engines.fal import FalImageBackend

    backend = FalImageBackend(
        cfg={"model": "x"},
        creds=EnvCredentialProvider(),
        profile_to_return=ImageProfile(
            name="x", max_resolution=(1024, 1024), supported_modes={"t2i"}
        ),
        http_post=lambda *a, **kw: {"request_id": "r"},
    )
    with pytest.raises(AuthError):
        backend.submit(ImageJob(spec={"model": "x"}, prompt="x"))


def test_result_polls_until_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: result must poll, not assume immediate completion."""
    status_responses = iter(
        [
            {"status": "IN_PROGRESS"},
            {"status": "IN_PROGRESS"},
            {"status": "COMPLETED"},
            {"images": [{"url": "https://fal.media/img/abc.png"}]},
        ]
    )

    def get(url, headers):
        return next(status_responses)

    sleeps: list[float] = []
    backend = _build_backend(
        monkeypatch, http_get=get, sleep=lambda s: sleeps.append(s)
    )
    art = backend.result("req-1")
    assert art.url == "https://fal.media/img/abc.png"
    assert art.filename == "abc.png"
    assert len(sleeps) == 2  # two IN_PROGRESS polls before COMPLETED


def test_result_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url, headers):
        return {"status": "ERROR", "error": "model not found"}

    backend = _build_backend(monkeypatch, http_get=get)
    with pytest.raises(KinoforgeError):
        backend.result("req-1")


def test_result_no_images_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: empty images array must fail loud rather than crash on indexing."""
    responses = iter(
        [
            {"status": "COMPLETED"},
            {"images": []},
        ]
    )

    def get(url, headers):
        return next(responses)

    backend = _build_backend(monkeypatch, http_get=get)
    with pytest.raises(KinoforgeError):
        backend.result("req-1")


def test_endpoints_static_queue_url(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _build_backend(monkeypatch)
    assert backend.endpoints() == {"queue": "https://queue.fal.run"}
