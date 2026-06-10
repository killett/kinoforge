"""RemoteSubmitPollBackend honors cancel_token in .result() / .submit().

Covers Replicate / Runway / Luma / Fal in one shot — every Bearer-API
hosted provider subclasses RemoteSubmitPollBackend, so adding cancel
honoring to the shared poll loop closes the cancel-honoring loop for
all four concretes at once.

Uses a minimal ``_FakeRemoteBackend`` subclass to exercise the base
poll loop without any concrete provider plumbing.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from kinoforge.core import Cancelled, CancelToken
from kinoforge.core.interfaces import GenerationJob, ModelProfile
from kinoforge.core.remote_backend import RemoteSubmitPollBackend


def _probe() -> ModelProfile:
    """Minimal ModelProfile required by the RemoteSubmitPollBackend ctor."""
    return ModelProfile(
        name="fake-remote",
        max_frames=24,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


class _FakeRemoteBackend(RemoteSubmitPollBackend):
    """Minimal concrete for ABC coverage; ``_poll_one`` never returns done.

    Tracks every ``_poll_one`` call in ``poll_calls`` so the tests can
    assert ZERO calls on a pre-set token, and a small bounded number
    (< 10) when the token is set mid-wait.
    """

    def __init__(self, *, poll_interval_s: float) -> None:
        super().__init__(
            client_factory=lambda: object(),
            sleep=lambda _s: (
                None
            ),  # intentionally non-blocking; cancel path uses token.wait
            max_poll=10_000,
            poll_interval_s=poll_interval_s,
            probe_profile=_probe(),
        )
        self.poll_calls = 0
        self.submit_calls = 0

    def _submit(self, client: object, job: GenerationJob) -> str:
        self.submit_calls += 1
        return "rj-1"

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        self.poll_calls += 1
        return {"status": "running"}

    def _is_done(self, status: dict[str, Any]) -> bool:
        return False

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        raise AssertionError("_extract_output_url must not be called when not done")

    def _delete(self, job_id: str) -> None:
        raise AssertionError("_delete must not be called under no ephemeral session")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return ""


def _make_job() -> GenerationJob:
    """Minimal GenerationJob — fields unused by _FakeRemoteBackend hooks."""
    return GenerationJob(spec={}, segments=[], params={})


def test_result_raises_cancelled_on_preset_token() -> None:
    """Pre-set token short-circuits before the first ``_poll_one`` call.

    Bug: today ``RemoteSubmitPollBackend.result`` accepts a
    ``cancel_token`` kwarg purely for ABC parity (Task 1 added
    ``del cancel_token``); Ctrl-C against a Replicate / Runway / Luma /
    Fal generation hangs forever because the poll loop never consults
    the token.
    """
    backend = _FakeRemoteBackend(poll_interval_s=0.01)
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.result("rj-1", cancel_token=token)

    assert backend.poll_calls == 0, (
        f"expected zero _poll_one calls on pre-set token, got {backend.poll_calls}"
    )


def test_result_honors_token_set_mid_wait() -> None:
    """Token set after one poll tick raises Cancelled within ~poll_interval_s.

    Bug: ``self._sleep(self._poll_interval_s)`` in the base poll loop is
    a plain ``time.sleep`` and is NOT interruptible. Replacing it with
    ``token.wait(poll_interval_s)`` lets a sibling thread (the CLI's
    SIGINT handler in production) unblock the wait promptly.
    """
    backend = _FakeRemoteBackend(poll_interval_s=0.05)
    token = CancelToken()

    def _setter() -> None:
        time.sleep(0.1)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()

    start = time.monotonic()
    with pytest.raises(Cancelled):
        backend.result("rj-1", cancel_token=token)
    elapsed = time.monotonic() - start

    # Bounded poll count: at 50ms interval with cancellation after 100ms,
    # the loop should run a handful of ticks at most. A high count would
    # indicate the wait is not interruptible.
    assert backend.poll_calls < 10, (
        f"too many _poll_one ticks ({backend.poll_calls}) — wait not interruptible"
    )
    assert elapsed < 1.0, (
        f"result() took {elapsed:.2f}s — should have cancelled within ~poll_interval_s"
    )


def test_submit_raises_cancelled_on_preset_token() -> None:
    """``submit`` checks the token before invoking ``_submit``.

    Symmetry with Task 2's ``ComfyUIBackend.submit``: an operator who
    presses Ctrl-C between job construction and the first network call
    should not pay for a wasted provider-side submission.
    """
    backend = _FakeRemoteBackend(poll_interval_s=0.01)
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.submit(_make_job(), cancel_token=token)

    assert backend.submit_calls == 0, (
        f"expected zero _submit calls on pre-set token, got {backend.submit_calls}"
    )


def test_result_no_token_preserves_legacy_path() -> None:
    """Legacy callers (no ``cancel_token``) still poll using ``self._sleep``.

    Existing engine tests (``test_replicate.py`` etc.) inject
    ``sleep=lambda s: None`` to keep the loop instant; the token-aware
    wait fallback must not regress that contract. The ``_FakeRemoteBackend``
    above uses the same sleep injection, so a no-token call should burn
    through ``max_poll`` iterations and raise ``TimeoutError`` (the
    base-class behavior at line 263), NOT ``Cancelled``.
    """
    backend = _FakeRemoteBackend(poll_interval_s=0.01)
    # max_poll=10_000 is too many for a fast unit test; rebuild with a small cap.
    backend._max_poll = 5  # noqa: SLF001 — direct ctor knob

    with pytest.raises(TimeoutError):
        backend.result("rj-1")  # NO cancel_token kwarg

    assert backend.poll_calls == 5, (
        f"expected 5 poll iterations (max_poll=5), got {backend.poll_calls}"
    )
