"""Tests for ReplicateEngine + ReplicateBackend via FakeReplicateClient."""

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


class _FakePrediction:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id", "PRED-1")
        self.status = data.get("status", "succeeded")
        self.output = data.get("output", "https://x/v.mp4")
        self.error = data.get("error")


class _FakePredictionsAPI:
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

    def create(self, **kw: Any) -> _FakePrediction:
        if self.create_exc is not None:
            raise self.create_exc
        self.create_calls.append(kw)
        return _FakePrediction(self._create)

    def get(self, pred_id: str) -> _FakePrediction:
        if self.get_exc is not None:
            raise self.get_exc
        if not self._gets:
            raise IndexError("FakeReplicateClient: ran out of get responses")
        return _FakePrediction(self._gets.pop(0))


class FakeReplicateClient:
    def __init__(
        self,
        *,
        predictions_create_response: dict[str, Any],
        predictions_get_responses: list[dict[str, Any]],
    ) -> None:
        self.predictions = _FakePredictionsAPI(
            create_response=predictions_create_response,
            get_responses=predictions_get_responses,
        )


# --- Helpers -------------------------------------------------------------


def _job(
    spec: dict[str, Any] | None = None,
    assets: list[ConditioningAsset] | None = None,
) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="cat sitting", params={}, assets=list(assets or []))],
        spec=spec or {"model": "wan-video/wan-t2v", "params": {}},
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
) -> tuple[Any, FakeReplicateClient]:
    from kinoforge.engines.replicate import ReplicateBackend

    client = FakeReplicateClient(
        predictions_create_response=create,
        predictions_get_responses=polls,
    )
    b = ReplicateBackend(
        client_factory=lambda: client,
        sleep=lambda _s: None,
        max_poll=max_poll,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
    )
    return b, client


def _stub_replicate_module() -> types.ModuleType:
    """Install a minimal ``replicate`` module so the lazy import resolves.

    We only need ``replicate.exceptions.ReplicateError`` to exist as a class
    for the ``isinstance`` check in ``_raise_for_sdk_error``; the backend's
    own SDK calls are routed through the injected ``FakeReplicateClient``.
    """

    class _ReplicateError(Exception):
        def __init__(self, msg: str = "", *, status: int | None = None) -> None:
            super().__init__(msg)
            self.status = status

    mod = types.ModuleType("replicate")
    exc_mod = types.ModuleType("replicate.exceptions")
    exc_mod.ReplicateError = _ReplicateError  # type: ignore[attr-defined]
    mod.exceptions = exc_mod  # type: ignore[attr-defined]
    sys.modules["replicate"] = mod
    sys.modules["replicate.exceptions"] = exc_mod
    return mod


@pytest.fixture(autouse=True)
def _replicate_stub() -> Any:
    """Always provide a stub ``replicate`` module to the lazy import sites."""
    saved_replicate = sys.modules.get("replicate")
    saved_exc = sys.modules.get("replicate.exceptions")
    mod = _stub_replicate_module()
    try:
        yield mod
    finally:
        if saved_replicate is not None:
            sys.modules["replicate"] = saved_replicate
        else:
            sys.modules.pop("replicate", None)
        if saved_exc is not None:
            sys.modules["replicate.exceptions"] = saved_exc
        else:
            sys.modules.pop("replicate.exceptions", None)


# --- Backend tests -------------------------------------------------------


def test_submit_returns_prediction_id_and_sends_prompt_and_model() -> None:
    """Submit forwards prompt + model + extra params to predictions.create."""
    b, client = _backend(
        create={"id": "abc123", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/v.mp4"}],
    )
    job_id = b.submit(_job(spec={"model": "wan-video/wan-t2v", "params": {"fps": 24}}))
    assert job_id == "abc123"
    assert len(client.predictions.create_calls) == 1
    call = client.predictions.create_calls[0]
    assert call["model"] == "wan-video/wan-t2v"
    assert call["input"]["prompt"] == "cat sitting"
    assert call["input"]["fps"] == 24


def test_result_polls_until_succeeded() -> None:
    """Result polls until status=='succeeded' and returns the URL."""
    b, _ = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[
            {"status": "starting"},
            {"status": "processing"},
            {"status": "succeeded", "output": "https://x/v.mp4"},
        ],
    )
    art = b.result("p1")
    assert art.url == "https://x/v.mp4"
    assert art.meta == {"job_id": "p1"}


def test_result_raises_kinoforge_error_on_failed_status() -> None:
    """status=='failed' surfaces as KinoforgeError carrying the reason."""
    b, _ = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "failed", "error": "NSFW input"}],
    )
    with pytest.raises(KinoforgeError, match="NSFW input"):
        b.result("p1")


def test_result_raises_timeout_after_max_poll() -> None:
    """Never-completing job hits TimeoutError after max_poll iterations."""
    b, _ = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "processing"}] * 4,
        max_poll=4,
    )
    with pytest.raises(TimeoutError):
        b.result("p1")


def test_extract_output_url_from_string_output() -> None:
    """Replicate may return output as a single string URL."""
    b, _ = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/single.mp4"}],
    )
    assert b.result("p1").url == "https://x/single.mp4"


def test_extract_output_url_from_list_output() -> None:
    """Replicate may return output as a list of URLs; unwrap [0]."""
    b, _ = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[
            {
                "status": "succeeded",
                "output": ["https://x/first.mp4", "https://x/extra.mp4"],
            }
        ],
    )
    assert b.result("p1").url == "https://x/first.mp4"


def test_inject_assets_init_image_maps_to_image_input() -> None:
    """role 'init_image' → input['image']."""
    b, client = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/v.mp4"}],
    )
    b.submit(_job(assets=[_asset("init_image", "https://k/init.png")]))
    assert client.predictions.create_calls[0]["input"]["image"] == "https://k/init.png"


def test_inject_assets_start_image_maps_to_start_image_input() -> None:
    """role 'start_image' → input['start_image']."""
    b, client = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/v.mp4"}],
    )
    b.submit(_job(assets=[_asset("start_image", "https://k/start.png")]))
    assert (
        client.predictions.create_calls[0]["input"]["start_image"]
        == "https://k/start.png"
    )


def test_inject_assets_end_image_maps_to_end_image_input() -> None:
    """role 'end_image' → input['end_image']."""
    b, client = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/v.mp4"}],
    )
    b.submit(_job(assets=[_asset("end_image", "https://k/end.png")]))
    assert (
        client.predictions.create_calls[0]["input"]["end_image"] == "https://k/end.png"
    )


# --- Engine tests --------------------------------------------------------


def test_engine_provision_raises_auth_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AuthError when REPLICATE_API_TOKEN is unset."""
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    from kinoforge.engines.replicate import ReplicateEngine

    e = ReplicateEngine(
        auth=Bearer(
            env_var="REPLICATE_API_TOKEN",
            credential_provider=EnvCredentialProvider(),
        )
    )
    with pytest.raises(AuthError):
        e.provision(None, {"spec": {"model": "m"}})


def test_engine_self_registers_under_replicate() -> None:
    """Side-effect registration: get_engine('replicate') returns the factory."""
    import kinoforge.engines.replicate  # noqa: F401 — triggers registration
    from kinoforge.core.registry import get_engine

    factory = get_engine("replicate")
    instance = factory()
    from kinoforge.engines.replicate import ReplicateEngine

    assert isinstance(instance, ReplicateEngine)


def test_submit_min_interval_blocks_burst_submits() -> None:
    """Second submit within ``submit_min_interval_s`` sleeps until the floor.

    Bug it catches: a regression where the backend ignores the throttle floor
    and fires two submits back-to-back, triggering Replicate's burst-of-1
    429 throttle when the account's rate-limit subsystem reports < $5 credit.
    """
    from kinoforge.engines.replicate import ReplicateBackend

    client = FakeReplicateClient(
        predictions_create_response={"id": "p1", "status": "starting"},
        predictions_get_responses=[],
    )
    sleeps: list[float] = []
    # Monotonic clock starts at 100.0, advances by 0.0 between sleeps.
    # The backend must call _sleep when consecutive submits are < interval.
    now = [100.0]

    def _mono() -> float:
        return now[0]

    def _sleep(s: float) -> None:
        sleeps.append(s)
        now[0] += s

    b = ReplicateBackend(
        client_factory=lambda: client,
        sleep=_sleep,
        max_poll=8,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        submit_min_interval_s=10.0,
        monotonic=_mono,
    )
    b.submit(_job())
    # No sleep on first submit — throttle floor only applies when there's
    # a previous submit timestamp to space against.
    assert sleeps == []
    b.submit(_job())
    # Second submit within the same monotonic instant — must sleep exactly
    # 10.0 s before firing.
    assert sleeps == [10.0]


def test_submit_min_interval_zero_disables_throttle() -> None:
    """Passing ``submit_min_interval_s=0`` skips the floor entirely.

    Bug it catches: the constant slips into the call path even when an
    operator explicitly opts out (e.g. after Replicate clears the throttle).
    """
    from kinoforge.engines.replicate import ReplicateBackend

    client = FakeReplicateClient(
        predictions_create_response={"id": "p1", "status": "starting"},
        predictions_get_responses=[],
    )
    sleeps: list[float] = []
    b = ReplicateBackend(
        client_factory=lambda: client,
        sleep=lambda s: sleeps.append(s),
        max_poll=8,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        submit_min_interval_s=0.0,
    )
    b.submit(_job())
    b.submit(_job())
    b.submit(_job())
    assert sleeps == []


def test_submit_auth_failure_mapped_to_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 401/403 ReplicateError surfaces as AuthError, not KinoforgeError."""
    b, client = _backend(
        create={"id": "p1", "status": "starting"},
        polls=[],
    )
    import replicate

    client.predictions.create_exc = replicate.exceptions.ReplicateError(
        "Unauthorized", status=401
    )
    with pytest.raises(AuthError):
        b.submit(_job())
