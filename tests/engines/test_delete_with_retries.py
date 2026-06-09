"""Tests for ``RemoteSubmitPollBackend._delete_with_retries`` + result() integration."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import (
    EphemeralDeleteFailedError,
    EphemeralDeleteHTTPError,
)
from kinoforge.core.interfaces import ModelProfile
from kinoforge.core.remote_backend import RemoteSubmitPollBackend

_PROBE = ModelProfile(
    name="probe",
    max_frames=120,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


class _FakeBackend(RemoteSubmitPollBackend):
    """Concrete subclass that records every ``_delete`` call.

    All five wire-shape abstracts are stubbed: ``_submit`` is unused by
    these tests; ``_poll_one`` / ``_is_done`` / ``_extract_output_url``
    drive the single ``result()`` integration test by returning a done
    status immediately so the test reaches the delete hook without
    spending real polls.
    """

    def __init__(self, *, delete_responses: list[Any]) -> None:
        super().__init__(
            client_factory=lambda: object(),
            sleep=lambda _s: None,
            max_poll=1,
            poll_interval_s=0.0,
            probe_profile=_PROBE,
        )
        self._responses = list(delete_responses)
        self.delete_calls: list[str] = []

    def _submit(self, client: object, job: Any) -> str:
        return "job-1"

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        return {"status": "done"}

    def _is_done(self, status: dict[str, Any]) -> bool:
        return True

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        return "https://e.example/clip.mp4"

    def _delete(self, job_id: str) -> None:
        self.delete_calls.append(job_id)
        if not self._responses:
            return
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://example.com/jobs/{job_id}"


def test_delete_success_no_retry() -> None:
    """First attempt succeeds — no sleep, no retry.

    Would-fail-bug: a loop that always slept once before the first call
    would slow every clean scrub by a full backoff tick.
    """
    backend = _FakeBackend(delete_responses=[None])
    sleeps: list[float] = []
    backend._delete_with_retries("job-1", retries=3, sleep_fn=sleeps.append)
    assert backend.delete_calls == ["job-1"]
    assert sleeps == []


def test_delete_transient_then_success() -> None:
    """Two transient HTTP errors then success — sleeps 1.0 then 2.0.

    Would-fail-bug: sleeping AFTER the last attempt wastes a backoff
    tick on a no-op; sleeping BEFORE the first attempt slows clean
    scrubs.
    """
    backend = _FakeBackend(
        delete_responses=[
            EphemeralDeleteHTTPError("503"),
            EphemeralDeleteHTTPError("503"),
            None,
        ]
    )
    sleeps: list[float] = []
    backend._delete_with_retries("job-1", retries=3, sleep_fn=sleeps.append)
    assert backend.delete_calls == ["job-1", "job-1", "job-1"]
    assert sleeps == [1.0, 2.0]


def test_delete_terminal_failure_raises_with_manual_url() -> None:
    """All retries exhausted → ``EphemeralDeleteFailedError`` with manual URL.

    Would-fail-bug: swallowing the last HTTP error would let the CLI
    exit 0 while the prompt-laden provider record survived.
    """
    backend = _FakeBackend(
        delete_responses=[
            EphemeralDeleteHTTPError("503"),
            EphemeralDeleteHTTPError("503"),
            EphemeralDeleteHTTPError("503"),
        ]
    )
    with pytest.raises(EphemeralDeleteFailedError) as exc:
        backend._delete_with_retries("job-1", retries=3, sleep_fn=lambda _s: None)
    msg = str(exc.value)
    assert "https://example.com/jobs/job-1" in msg
    assert exc.value.attempts == 3
    assert exc.value.provider == "_fake"
    assert exc.value.job_id == "job-1"
    assert "503" in exc.value.last_error
    # D14 — no output-file enumeration.
    assert "preserved" not in msg.lower()
    assert "output/" not in msg


def test_result_calls_delete_under_ephemeral() -> None:
    """Active session + ``delete_on_completion=True`` → delete fires after poll.

    Would-fail-bug: a result() that called _delete BEFORE building the
    artifact would lose the provider output URL — the Artifact would
    point at an already-scrubbed prediction.
    """
    backend = _FakeBackend(delete_responses=[None])
    with EphemeralSession(enabled=True):
        artifact = backend.result("job-1")
    assert backend.delete_calls == ["job-1"]
    assert artifact.url == "https://e.example/clip.mp4"


def test_result_skips_delete_under_default_mode() -> None:
    """Active session with the default policy: delete does NOT fire.

    Would-fail-bug: a result() that gated only on session presence (not
    on the policy) would scrub every non-ephemeral caller's prediction.
    """
    backend = _FakeBackend(delete_responses=[])
    with EphemeralSession(enabled=False):
        backend.result("job-1")
    assert backend.delete_calls == []


def test_result_skips_delete_with_no_session() -> None:
    """No active session: delete does NOT fire.

    Would-fail-bug: a result() that fell through to the delete branch
    when ``EphemeralSession.current()`` is None would crash before
    returning the Artifact.
    """
    backend = _FakeBackend(delete_responses=[])
    backend.result("job-1")
    assert backend.delete_calls == []
