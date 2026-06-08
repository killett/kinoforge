"""Tests for LumaEngine + LumaBackend via FakeLumaClient."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    ModelProfile,
    Segment,
)

_PROBE = ModelProfile(
    name="probe",
    max_frames=216,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# --- Fakes ---------------------------------------------------------------


class _FakeGeneration:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id", "GEN-1")
        self.state = data.get("state", "completed")
        self.assets = data.get("assets") or {}
        self.failure_reason = data.get("failure_reason")


class _FakeGenerationsAPI:
    def __init__(
        self,
        *,
        create_response: dict[str, Any],
        get_responses: list[dict[str, Any]],
    ) -> None:
        self._create = create_response
        self._gets = list(get_responses)
        self.create_calls: list[dict[str, Any]] = []
        self.create_exc: Exception | None = None
        self.get_exc: Exception | None = None

    def create(self, **kw: Any) -> _FakeGeneration:
        if self.create_exc is not None:
            raise self.create_exc
        self.create_calls.append(kw)
        return _FakeGeneration(self._create)

    def get(self, gen_id: str) -> _FakeGeneration:
        if self.get_exc is not None:
            raise self.get_exc
        if not self._gets:
            raise IndexError("FakeLumaClient: ran out of get responses")
        return _FakeGeneration(self._gets.pop(0))


class FakeLumaClient:
    def __init__(
        self,
        *,
        create_response: dict[str, Any],
        get_responses: list[dict[str, Any]],
    ) -> None:
        self.generations = _FakeGenerationsAPI(
            create_response=create_response,
            get_responses=get_responses,
        )


# --- Helpers -------------------------------------------------------------


def _job(
    spec: dict[str, Any] | None = None,
    assets: list[ConditioningAsset] | None = None,
) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="cat sitting", params={}, assets=list(assets or []))],
        spec=spec or {"model": "ray-2", "params": {}},
        params={},
    )


def _asset(role: str, uri: str) -> ConditioningAsset:
    return ConditioningAsset(
        kind="image",
        role=role,
        ref=Artifact(uri=uri, url=uri),
    )


def _backend(
    *,
    create: dict[str, Any],
    polls: list[dict[str, Any]],
    max_poll: int = 8,
) -> tuple[Any, FakeLumaClient]:
    from kinoforge.engines.luma import LumaBackend

    client = FakeLumaClient(
        create_response=create,
        get_responses=polls,
    )
    b = LumaBackend(
        client_factory=lambda: client,
        sleep=lambda _s: None,
        max_poll=max_poll,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
    )
    return b, client


def _stub_lumaai_module() -> types.ModuleType:
    class _APIError(Exception):
        def __init__(self, msg: str = "", *, status_code: int | None = None) -> None:
            super().__init__(msg)
            self.status_code = status_code

    mod = types.ModuleType("lumaai")
    mod.APIError = _APIError  # type: ignore[attr-defined]
    sys.modules["lumaai"] = mod
    return mod


@pytest.fixture(autouse=True)
def _lumaai_stub() -> Any:
    saved = sys.modules.get("lumaai")
    mod = _stub_lumaai_module()
    try:
        yield mod
    finally:
        if saved is not None:
            sys.modules["lumaai"] = saved
        else:
            sys.modules.pop("lumaai", None)


# --- Backend tests -------------------------------------------------------


def test_submit_sends_prompt_and_model_via_generations_create() -> None:
    b, client = _backend(
        create={"id": "g1", "state": "queued"},
        polls=[
            {"state": "completed", "assets": {"video": "https://x/v.mp4"}},
        ],
    )
    job_id = b.submit(_job(spec={"model": "ray-2", "params": {"resolution": "540p"}}))
    assert job_id == "g1"
    call = client.generations.create_calls[0]
    assert call["model"] == "ray-2"
    assert call["prompt"] == "cat sitting"
    assert call["resolution"] == "540p"


def test_result_uses_state_field_not_status() -> None:
    """Luma's status field is 'state' (not 'status'); only 'completed' is done."""
    b, _ = _backend(
        create={"id": "g1"},
        polls=[
            {"state": "queued"},
            {"state": "dreaming"},
            {"state": "completed", "assets": {"video": "https://x/v.mp4"}},
        ],
    )
    art = b.result("g1")
    assert art.url == "https://x/v.mp4"


def test_result_raises_kinoforge_error_on_failed_state() -> None:
    b, _ = _backend(
        create={"id": "g1"},
        polls=[{"state": "failed", "failure_reason": "NSFW"}],
    )
    with pytest.raises(KinoforgeError, match="NSFW"):
        b.result("g1")


def test_result_raises_timeout_after_max_poll() -> None:
    b, _ = _backend(
        create={"id": "g1"},
        polls=[{"state": "queued"}] * 4,
        max_poll=4,
    )
    with pytest.raises(TimeoutError):
        b.result("g1")


def test_extract_output_url_from_assets_video() -> None:
    """Luma nests the output under assets.video — not a top-level field."""
    b, _ = _backend(
        create={"id": "g1"},
        polls=[
            {
                "state": "completed",
                "assets": {"video": "https://x/nested.mp4", "image": "ignored"},
            }
        ],
    )
    assert b.result("g1").url == "https://x/nested.mp4"


def test_extract_output_url_empty_when_no_assets_video() -> None:
    """Done with empty assets → empty URL (caller may treat as failure)."""
    b, _ = _backend(
        create={"id": "g1"},
        polls=[{"state": "completed", "assets": {}}],
    )
    assert b.result("g1").url == ""


def test_inject_assets_init_image_maps_to_keyframes_frame0() -> None:
    b, client = _backend(
        create={"id": "g1"},
        polls=[{"state": "completed", "assets": {"video": "https://x/v.mp4"}}],
    )
    b.submit(_job(assets=[_asset("init_image", "https://k/init.png")]))
    keyframes = client.generations.create_calls[0]["keyframes"]
    assert keyframes["frame0"] == {"type": "image", "url": "https://k/init.png"}


def test_inject_assets_start_image_maps_to_keyframes_frame0() -> None:
    """start_image and init_image both target frame0 (semantically equivalent for Luma)."""
    b, client = _backend(
        create={"id": "g1"},
        polls=[{"state": "completed", "assets": {"video": "https://x/v.mp4"}}],
    )
    b.submit(_job(assets=[_asset("start_image", "https://k/s.png")]))
    keyframes = client.generations.create_calls[0]["keyframes"]
    assert keyframes["frame0"] == {"type": "image", "url": "https://k/s.png"}


def test_inject_assets_end_image_maps_to_keyframes_frame1() -> None:
    b, client = _backend(
        create={"id": "g1"},
        polls=[{"state": "completed", "assets": {"video": "https://x/v.mp4"}}],
    )
    b.submit(_job(assets=[_asset("end_image", "https://k/e.png")]))
    keyframes = client.generations.create_calls[0]["keyframes"]
    assert keyframes["frame1"] == {"type": "image", "url": "https://k/e.png"}


# --- Engine tests --------------------------------------------------------


def test_engine_provision_raises_auth_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUMAAI_API_KEY", raising=False)
    from kinoforge.engines.luma import LumaEngine

    e = LumaEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY",
            credential_provider=EnvCredentialProvider(),
        )
    )
    with pytest.raises(AuthError):
        e.provision(None, {"spec": {"model": "ray-2"}})


def test_engine_self_registers_under_luma() -> None:
    import kinoforge.engines.luma  # noqa: F401
    from kinoforge.core.registry import get_engine

    factory = get_engine("luma")
    instance = factory()
    from kinoforge.engines.luma import LumaEngine

    assert isinstance(instance, LumaEngine)


def test_submit_auth_failure_mapped_to_auth_error() -> None:
    b, client = _backend(
        create={"id": "g1"},
        polls=[],
    )
    import lumaai

    client.generations.create_exc = lumaai.APIError("Unauthorized", status_code=401)
    with pytest.raises(AuthError):
        b.submit(_job())
