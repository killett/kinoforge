"""Tests for ReplicateImageEngine + ReplicateImageBackend."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError
from kinoforge.core.interfaces import ImageJob

# --- Fakes ---------------------------------------------------------------


class _FakePrediction:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id", "IMG-1")
        self.status = data.get("status", "succeeded")
        self.output = data.get("output", "https://x/img.png")
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

    def create(self, **kw: Any) -> _FakePrediction:
        self.create_calls.append(kw)
        return _FakePrediction(self._create)

    def get(self, pred_id: str) -> _FakePrediction:
        return _FakePrediction(self._gets.pop(0))


class FakeReplicateImageClient:
    def __init__(
        self,
        *,
        create_response: dict[str, Any],
        get_responses: list[dict[str, Any]],
    ) -> None:
        self.predictions = _FakePredictionsAPI(
            create_response=create_response,
            get_responses=get_responses,
        )


def _backend(
    *,
    create: dict[str, Any],
    polls: list[dict[str, Any]],
) -> tuple[Any, FakeReplicateImageClient]:
    from kinoforge.image_engines.replicate import ReplicateImageBackend

    client = FakeReplicateImageClient(
        create_response=create,
        get_responses=polls,
    )
    b = ReplicateImageBackend(
        client_factory=lambda: client,
        sleep=lambda _s: None,
        max_poll=4,
        poll_interval_s=0.0,
    )
    return b, client


def _stub_replicate() -> types.ModuleType:
    class _ReplicateError(Exception):
        pass

    mod = types.ModuleType("replicate")
    exc_mod = types.ModuleType("replicate.exceptions")
    exc_mod.ReplicateError = _ReplicateError  # type: ignore[attr-defined]
    mod.exceptions = exc_mod  # type: ignore[attr-defined]
    sys.modules["replicate"] = mod
    sys.modules["replicate.exceptions"] = exc_mod
    return mod


@pytest.fixture(autouse=True)
def _replicate_stub() -> Any:
    saved = sys.modules.get("replicate")
    mod = _stub_replicate()
    try:
        yield mod
    finally:
        if saved is not None:
            sys.modules["replicate"] = saved
        else:
            sys.modules.pop("replicate", None)
        sys.modules.pop("replicate.exceptions", None)


# --- Tests ---------------------------------------------------------------


def test_submit_sends_prompt_and_model_via_predictions_create() -> None:
    """ImageJob is adapted to a single-segment GenerationJob for the inner backend."""
    b, client = _backend(
        create={"id": "img1", "status": "starting"},
        polls=[{"status": "succeeded", "output": "https://x/img.png"}],
    )
    job_id = b.submit(
        ImageJob(
            spec={"model": "black-forest-labs/flux-schnell"},
            prompt="a small cat",
            params={},
        )
    )
    assert job_id == "img1"
    call = client.predictions.create_calls[0]
    assert call["version"] == "black-forest-labs/flux-schnell"
    assert call["input"]["prompt"] == "a small cat"


def test_result_polls_until_succeeded_and_returns_artifact() -> None:
    b, _ = _backend(
        create={"id": "img1"},
        polls=[
            {"status": "starting"},
            {"status": "succeeded", "output": "https://x/img.png"},
        ],
    )
    b.submit(
        ImageJob(spec={"model": "m"}, prompt="cat", params={}),
    )
    art = b.result("img1")
    assert art.url == "https://x/img.png"
    assert art.meta == {"job_id": "img1"}


def test_extract_output_url_handles_list_output() -> None:
    """flux-schnell returns ``output`` as a list with one URL — unwrap [0]."""
    b, _ = _backend(
        create={"id": "img1"},
        polls=[
            {
                "status": "succeeded",
                "output": ["https://x/first.png"],
            }
        ],
    )
    b.submit(ImageJob(spec={"model": "m"}, prompt="cat", params={}))
    assert b.result("img1").url == "https://x/first.png"


def test_engine_self_registers_under_replicate_image_registry() -> None:
    """Side-effect registration on the IMAGE-engine registry."""
    import kinoforge.image_engines.replicate  # noqa: F401
    from kinoforge.core.registry import get_image_engine

    factory = get_image_engine("replicate")
    instance = factory()
    from kinoforge.image_engines.replicate import ReplicateImageEngine

    assert isinstance(instance, ReplicateImageEngine)


def test_engine_provision_raises_auth_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    from kinoforge.image_engines.replicate import ReplicateImageEngine

    e = ReplicateImageEngine(
        auth=Bearer(
            env_var="REPLICATE_API_TOKEN",
            credential_provider=EnvCredentialProvider(),
        )
    )
    with pytest.raises(AuthError):
        e.provision(None, {"spec": {"model": "m"}})
