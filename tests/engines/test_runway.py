"""Tests for RunwayEngine + RunwayBackend via FakeRunwayClient."""

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
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# --- Fakes ---------------------------------------------------------------


class _FakeTask:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id", "TASK-1")
        self.status = data.get("status", "SUCCEEDED")
        self.output = data.get("output", ["https://x/v.mp4"])
        self.failure = data.get("failure")


class _FakeVideoAPI:
    def __init__(self, create_response: dict[str, Any]) -> None:
        self._create = create_response
        self.create_calls: list[dict[str, Any]] = []
        self.create_exc: Exception | None = None

    def create(self, **kw: Any) -> _FakeTask:
        if self.create_exc is not None:
            raise self.create_exc
        self.create_calls.append(kw)
        return _FakeTask(self._create)


class _FakeTasksAPI:
    def __init__(self, get_responses: list[dict[str, Any]]) -> None:
        self._gets = list(get_responses)
        self.get_exc: Exception | None = None

    def retrieve(self, task_id: str) -> _FakeTask:
        if self.get_exc is not None:
            raise self.get_exc
        if not self._gets:
            raise IndexError("FakeRunwayClient: ran out of retrieve responses")
        return _FakeTask(self._gets.pop(0))


class FakeRunwayClient:
    def __init__(
        self,
        *,
        create_response: dict[str, Any],
        get_responses: list[dict[str, Any]],
    ) -> None:
        self.text_to_video = _FakeVideoAPI(create_response)
        self.image_to_video = _FakeVideoAPI(create_response)
        self.tasks = _FakeTasksAPI(get_responses)


# --- Helpers -------------------------------------------------------------


def _job(
    spec: dict[str, Any] | None = None,
    assets: list[ConditioningAsset] | None = None,
) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="cat sitting", params={}, assets=list(assets or []))],
        spec=spec or {"model": "gen3a_turbo", "params": {}, "mode": "t2v"},
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
) -> tuple[Any, FakeRunwayClient]:
    from kinoforge.engines.runway import RunwayBackend

    client = FakeRunwayClient(
        create_response=create,
        get_responses=polls,
    )
    b = RunwayBackend(
        client_factory=lambda: client,
        sleep=lambda _s: None,
        max_poll=max_poll,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
    )
    return b, client


def _stub_runwayml_module() -> types.ModuleType:
    """Install a minimal ``runwayml`` module so the lazy import resolves."""

    class _APIError(Exception):
        def __init__(self, msg: str = "", *, status_code: int | None = None) -> None:
            super().__init__(msg)
            self.status_code = status_code

    class _AuthenticationError(_APIError):
        pass

    mod = types.ModuleType("runwayml")
    mod.APIError = _APIError  # type: ignore[attr-defined]
    mod.AuthenticationError = _AuthenticationError  # type: ignore[attr-defined]
    sys.modules["runwayml"] = mod
    return mod


@pytest.fixture(autouse=True)
def _runwayml_stub() -> Any:
    saved = sys.modules.get("runwayml")
    mod = _stub_runwayml_module()
    try:
        yield mod
    finally:
        if saved is not None:
            sys.modules["runwayml"] = saved
        else:
            sys.modules.pop("runwayml", None)


# --- Backend tests -------------------------------------------------------


def test_submit_t2v_dispatches_to_text_to_video() -> None:
    """Mode 't2v' → text_to_video.create (not image_to_video.create)."""
    b, client = _backend(
        create={"id": "t1", "status": "PENDING"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(_job(spec={"model": "gen3a_turbo", "mode": "t2v"}))
    assert len(client.text_to_video.create_calls) == 1
    assert len(client.image_to_video.create_calls) == 0
    call = client.text_to_video.create_calls[0]
    assert call["model"] == "gen3a_turbo"
    assert call["prompt_text"] == "cat sitting"


def test_submit_i2v_dispatches_to_image_to_video() -> None:
    """Mode 'i2v' → image_to_video.create (not text_to_video.create)."""
    b, client = _backend(
        create={"id": "t2", "status": "PENDING"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(_job(spec={"model": "gen4_turbo", "mode": "i2v"}))
    assert len(client.image_to_video.create_calls) == 1
    assert len(client.text_to_video.create_calls) == 0


def test_submit_flf2v_dispatches_to_image_to_video() -> None:
    """Mode 'flf2v' also routes to image_to_video (first+last frames)."""
    b, client = _backend(
        create={"id": "t3", "status": "PENDING"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(_job(spec={"model": "gen4_turbo", "mode": "flf2v"}))
    assert len(client.image_to_video.create_calls) == 1


def test_result_polls_until_succeeded_uppercase() -> None:
    """Status enum is UPPERCASE; 'SUCCEEDED' returns the output URL."""
    b, _ = _backend(
        create={"id": "t1"},
        polls=[
            {"status": "PENDING"},
            {"status": "RUNNING"},
            {"status": "SUCCEEDED", "output": ["https://x/v.mp4"]},
        ],
    )
    art = b.result("t1")
    assert art.url == "https://x/v.mp4"


def test_result_raises_kinoforge_error_on_failed_uppercase() -> None:
    """'FAILED' surfaces as KinoforgeError carrying the failure reason."""
    b, _ = _backend(
        create={"id": "t1"},
        polls=[{"status": "FAILED", "failure": "Content moderation"}],
    )
    with pytest.raises(KinoforgeError, match="Content moderation"):
        b.result("t1")


def test_result_raises_timeout_after_max_poll() -> None:
    b, _ = _backend(
        create={"id": "t1"},
        polls=[{"status": "RUNNING"}] * 4,
        max_poll=4,
    )
    with pytest.raises(TimeoutError):
        b.result("t1")


def test_extract_output_url_from_list_first_element() -> None:
    """Runway always returns output as a list; extractor takes [0]."""
    b, _ = _backend(
        create={"id": "t1"},
        polls=[
            {
                "status": "SUCCEEDED",
                "output": ["https://x/first.mp4", "https://x/extra.mp4"],
            }
        ],
    )
    assert b.result("t1").url == "https://x/first.mp4"


def test_inject_assets_init_image_maps_to_prompt_image() -> None:
    b, client = _backend(
        create={"id": "t1"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(
        _job(
            spec={"model": "gen4_turbo", "mode": "i2v"},
            assets=[_asset("init_image", "https://k/init.png")],
        )
    )
    assert client.image_to_video.create_calls[0]["prompt_image"] == "https://k/init.png"


def test_inject_assets_start_image_maps_to_first_image() -> None:
    b, client = _backend(
        create={"id": "t1"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(
        _job(
            spec={"model": "gen4_turbo", "mode": "flf2v"},
            assets=[_asset("start_image", "https://k/first.png")],
        )
    )
    assert client.image_to_video.create_calls[0]["first_image"] == "https://k/first.png"


def test_inject_assets_end_image_maps_to_last_image() -> None:
    b, client = _backend(
        create={"id": "t1"},
        polls=[{"status": "SUCCEEDED", "output": ["https://x/v.mp4"]}],
    )
    b.submit(
        _job(
            spec={"model": "gen4_turbo", "mode": "flf2v"},
            assets=[_asset("end_image", "https://k/last.png")],
        )
    )
    assert client.image_to_video.create_calls[0]["last_image"] == "https://k/last.png"


# --- Engine tests --------------------------------------------------------


def test_engine_provision_raises_auth_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNWAYML_API_SECRET", raising=False)
    from kinoforge.engines.runway import RunwayEngine

    e = RunwayEngine(
        auth=Bearer(
            env_var="RUNWAYML_API_SECRET",
            credential_provider=EnvCredentialProvider(),
        )
    )
    with pytest.raises(AuthError):
        e.provision(None, {"spec": {"model": "m"}})


def test_engine_self_registers_under_runway() -> None:
    import kinoforge.engines.runway  # noqa: F401
    from kinoforge.core.registry import get_engine

    factory = get_engine("runway")
    instance = factory()
    from kinoforge.engines.runway import RunwayEngine

    assert isinstance(instance, RunwayEngine)


def test_submit_auth_failure_mapped_to_auth_error() -> None:
    """SDK APIError 401 surfaces as AuthError."""
    b, client = _backend(
        create={"id": "t1"},
        polls=[],
    )
    import runwayml

    client.text_to_video.create_exc = runwayml.AuthenticationError(
        "Unauthorized", status_code=401
    )
    with pytest.raises(AuthError):
        b.submit(_job(spec={"model": "gen3a_turbo", "mode": "t2v"}))
