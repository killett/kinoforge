"""Tests for RemoteSubmitPollBackend + RemoteSubmitPollEngine ABCs."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.errors import AuthError, ConfigError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.core.remote_backend import (
    RemoteSubmitPollBackend,
    RemoteSubmitPollEngine,
)

_PROFILE = ModelProfile(
    name="probe",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _job(spec: dict[str, Any] | None = None) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="cat", params={}, assets=[])],
        spec=spec or {"model": "demo", "params": {}},
        params={},
    )


class _Backend(RemoteSubmitPollBackend):
    """Concrete subclass for hook-driven tests."""

    def __init__(
        self,
        *,
        statuses: list[dict[str, Any]],
        submit_id: str = "JOB-1",
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._statuses = list(statuses)
        self._submit_id = submit_id
        self.submit_calls: list[GenerationJob] = []

    def _submit(self, client: Any, job: GenerationJob) -> str:
        self.submit_calls.append(job)
        return self._submit_id

    def _poll_one(self, client: Any, job_id: str) -> dict[str, Any]:
        return self._statuses.pop(0)

    def _is_done(self, status: dict[str, Any]) -> bool:
        return status.get("state") == "done"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        if status.get("state") == "failed":
            return True, str(status.get("reason", "unknown"))
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        return str(status.get("url", ""))

    def _extract_filename(self, status: dict[str, Any]) -> str:
        return str(status.get("filename", ""))


def _backend_factory(
    *,
    statuses: list[dict[str, Any]],
    sleeps: list[float] | None = None,
    **kw: Any,
) -> _Backend:
    sleep_calls = sleeps if sleeps is not None else []

    def _sleep(s: float) -> None:
        sleep_calls.append(s)

    return _Backend(
        statuses=statuses,
        client_factory=lambda: object(),
        sleep=_sleep,
        max_poll=4,
        poll_interval_s=0.25,
        probe_profile=_PROFILE,
        **kw,
    )


def test_submit_returns_job_id_from_hook() -> None:
    b = _backend_factory(statuses=[{"state": "done", "url": "https://x/v.mp4"}])
    job = _job()
    assert b.submit(job) == "JOB-1"
    assert b.submit_calls == [job]


def test_result_polls_until_done_and_returns_artifact() -> None:
    b = _backend_factory(
        statuses=[
            {"state": "running"},
            {"state": "running"},
            {"state": "done", "url": "https://x/v.mp4", "filename": "v.mp4"},
        ]
    )
    art = b.result("JOB-1")
    assert isinstance(art, Artifact)
    assert art.url == "https://x/v.mp4"
    assert art.filename == "v.mp4"
    assert art.meta == {"job_id": "JOB-1"}


def test_result_raises_kinoforge_error_on_failed() -> None:
    b = _backend_factory(
        statuses=[{"state": "failed", "reason": "OOM"}],
    )
    with pytest.raises(KinoforgeError, match="OOM"):
        b.result("JOB-1")


def test_result_raises_timeout_after_max_poll() -> None:
    b = _backend_factory(
        statuses=[{"state": "running"}] * 4,
    )
    with pytest.raises(TimeoutError):
        b.result("JOB-1")


def test_sleep_is_injected_not_real() -> None:
    sleeps: list[float] = []
    b = _backend_factory(
        statuses=[
            {"state": "running"},
            {"state": "done", "url": "https://x/v.mp4"},
        ],
        sleeps=sleeps,
    )
    b.result("JOB-1")
    assert sleeps == [0.25]


def test_capabilities_returns_probe() -> None:
    b = _backend_factory(statuses=[{"state": "done"}])
    assert b.capabilities() is _PROFILE
    assert b.inspect_capabilities() is _PROFILE


def test_endpoints_default_empty() -> None:
    b = _backend_factory(statuses=[{"state": "done"}])
    assert b.endpoints() == {}


# --- Engine ABC -----------------------------------------------------------


class _Engine(RemoteSubmitPollEngine):
    name = "demo"

    def _build_client_factory(self, cfg, creds):
        return lambda: object()

    def _build_backend(self, cfg, instance):
        return _backend_factory(statuses=[{"state": "done"}])


def test_engine_provision_rejects_non_none_instance() -> None:
    e = _Engine(auth=Bearer(env_var="X"))
    with pytest.raises(KinoforgeError):
        e.provision(object(), {"engine": {"demo": {}}, "spec": {"model": "m"}})  # type: ignore[arg-type]


def test_engine_provision_raises_auth_error_when_creds_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEMO_KEY", raising=False)
    e = _Engine(auth=Bearer(env_var="DEMO_KEY"))
    with pytest.raises(AuthError):
        e.provision(None, {"engine": {"demo": {}}, "spec": {"model": "m"}})


def test_engine_key_base_raises_config_error_on_missing_spec_model() -> None:
    e = _Engine(auth=Bearer(env_var="X"))
    with pytest.raises(ConfigError):
        e.key_base({"engine": {"demo": {}}, "spec": {}})


def test_engine_key_base_returns_spec_model() -> None:
    e = _Engine(auth=Bearer(env_var="X"))
    assert e.key_base({"spec": {"model": "wan-t2v"}}) == "wan-t2v"
