"""Per-engine ``_delete`` + ``manual_cleanup_url`` tests for Replicate + Runway.

Covers the request shape (method, URL, Authorization header), the success
status set (200/204/404), the failure mapping to
``EphemeralDeleteHTTPError`` for everything else, and the browser-facing
manual cleanup URL.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import EphemeralDeleteHTTPError
from kinoforge.core.interfaces import ModelProfile

_PROBE = ModelProfile(
    name="probe",
    max_frames=120,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _replicate(*, status_code: int) -> tuple[Any, list[tuple[str, dict[str, str]]]]:
    """Build a ``ReplicateBackend`` with a fake ``http_delete`` recording calls."""
    from kinoforge.engines.replicate import ReplicateBackend

    calls: list[tuple[str, dict[str, str]]] = []

    def _fake(url: str, headers: dict[str, str]) -> int:
        calls.append((url, dict(headers)))
        return status_code

    backend = ReplicateBackend(
        client_factory=lambda: object(),
        sleep=lambda _s: None,
        max_poll=1,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        token="t-XXXX",
        http_delete=_fake,
    )
    return backend, calls


def _runway(*, status_code: int) -> tuple[Any, list[tuple[str, dict[str, str]]]]:
    """Build a ``RunwayBackend`` with a fake ``http_delete`` recording calls."""
    from kinoforge.engines.runway import RunwayBackend

    calls: list[tuple[str, dict[str, str]]] = []

    def _fake(url: str, headers: dict[str, str]) -> int:
        calls.append((url, dict(headers)))
        return status_code

    backend = RunwayBackend(
        client_factory=lambda: object(),
        sleep=lambda _s: None,
        max_poll=1,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        token="r-YYYY",
        http_delete=_fake,
    )
    return backend, calls


# --- Replicate ---------------------------------------------------------------


def test_replicate_delete_sends_correct_request() -> None:
    """DELETE + canonical URL + Bearer token in Authorization header.

    Would-fail-bug: a POST or PUT method, the wrong URL family, or a
    missing Authorization header would silently leave the prediction
    on Replicate's dashboard despite a "successful" exit.
    """
    backend, calls = _replicate(status_code=204)
    backend._delete("pred-abc")
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == "https://api.replicate.com/v1/predictions/pred-abc"
    assert headers["Authorization"] == "Bearer t-XXXX"


def test_replicate_delete_404_is_success() -> None:
    """404 = already-gone, treated as success — no raise.

    Would-fail-bug: raising on 404 would fail any retry attempt where
    the first call succeeded server-side but the response was lost
    in transit; the retry would then 404 and ephemeral would error.
    """
    backend, _ = _replicate(status_code=404)
    backend._delete("pred-abc")


def test_replicate_delete_503_raises_http_error() -> None:
    """Non-{200,204,404} → ``EphemeralDeleteHTTPError`` so retries fire.

    Would-fail-bug: a silent no-op on 5xx would let one transient HTTP
    503 leave a prompt-laden prediction on the provider's dashboard.
    """
    backend, _ = _replicate(status_code=503)
    with pytest.raises(EphemeralDeleteHTTPError, match="503"):
        backend._delete("pred-abc")


def test_replicate_manual_cleanup_url_shape() -> None:
    """Cleanup URL points at the browser-facing prediction dashboard.

    Would-fail-bug: an API-shaped URL in the error block would 404 in
    the browser when the operator pasted it from the spec §10.5 block.
    """
    from kinoforge.engines.replicate import ReplicateBackend

    assert (
        ReplicateBackend.manual_cleanup_url("pred-abc")
        == "https://replicate.com/predictions/pred-abc"
    )


# --- Runway ------------------------------------------------------------------


def test_runway_delete_sends_correct_request() -> None:
    """DELETE + canonical URL + Bearer token in Authorization header.

    Would-fail-bug: hitting ``app.runwayml.com`` instead of
    ``api.dev.runwayml.com`` would route through the browser endpoint
    and never reach the task-delete handler.
    """
    backend, calls = _runway(status_code=204)
    backend._delete("task-xyz")
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == "https://api.dev.runwayml.com/v1/tasks/task-xyz"
    assert headers["Authorization"] == "Bearer r-YYYY"


def test_runway_delete_404_is_success() -> None:
    """404 = already-gone, treated as success — no raise.

    Would-fail-bug: raising on 404 would surface a misleading "delete
    failed" UX when the record was actually scrubbed on the prior try.
    """
    backend, _ = _runway(status_code=404)
    backend._delete("task-xyz")


def test_runway_delete_500_raises() -> None:
    """5xx → ``EphemeralDeleteHTTPError``.

    Would-fail-bug: swallowing 500 would let intermittent Runway-side
    outages leave a prompt-laden task on the provider's dashboard.
    """
    backend, _ = _runway(status_code=500)
    with pytest.raises(EphemeralDeleteHTTPError):
        backend._delete("task-xyz")


def test_runway_manual_cleanup_url_shape() -> None:
    """Cleanup URL points at the browser-facing task dashboard.

    Would-fail-bug: returning the API URL would 404 in the operator's
    browser when they tried to finish a partial scrub by hand.
    """
    from kinoforge.engines.runway import RunwayBackend

    assert (
        RunwayBackend.manual_cleanup_url("task-xyz")
        == "https://app.runwayml.com/tasks/task-xyz"
    )


# --- Integration: result() actually wires _delete under ephemeral -----------


def _stub_status_path(backend: Any) -> None:
    """Make ``result()`` return immediately so the test reaches the delete hook."""
    backend._poll_one = lambda _c, _j: {
        "status": "succeeded",
        "output": "https://e/c.mp4",
    }
    backend._is_done = lambda _s: True
    backend._is_failed = lambda _s: (False, "")
    backend._extract_output_url = lambda _s: "https://e/c.mp4"


def test_replicate_result_fires_delete_under_ephemeral() -> None:
    """Active strict session → ``result()`` calls ``_delete`` after the artifact.

    Would-fail-bug: a result() path that returned the Artifact before
    invoking the retry chain would leave the prediction on Replicate's
    dashboard even with --ephemeral set.
    """
    from kinoforge.core.ephemeral import EphemeralSession

    backend, calls = _replicate(status_code=204)
    _stub_status_path(backend)
    with EphemeralSession(enabled=True):
        artifact = backend.result("pred-abc")
    assert calls and calls[0][0].endswith("/predictions/pred-abc")
    assert artifact.url == "https://e/c.mp4"


def test_runway_result_skips_delete_outside_ephemeral() -> None:
    """No active session → ``result()`` returns artifact, no DELETE fired.

    Would-fail-bug: a result() path that fired delete unconditionally
    would scrub every non-ephemeral caller's task.
    """
    backend, calls = _runway(status_code=204)
    _stub_status_path(backend)
    backend.result("task-xyz")
    assert calls == []


def test_replicate_retries_503_then_succeeds() -> None:
    """Two 503s + a 204 → three calls total via ``_delete_with_retries``.

    Would-fail-bug: a hardcoded single-shot retry would give up after
    one transient failure and raise ``EphemeralDeleteFailedError``
    despite the next call succeeding immediately.
    """
    from kinoforge.engines.replicate import ReplicateBackend

    statuses = [503, 503, 204]
    calls: list[tuple[str, dict[str, str]]] = []

    def _fake(url: str, headers: dict[str, str]) -> int:
        calls.append((url, dict(headers)))
        return statuses.pop(0)

    backend = ReplicateBackend(
        client_factory=lambda: object(),
        sleep=lambda _s: None,
        max_poll=1,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        token="t-XXXX",
        http_delete=_fake,
    )
    backend._delete_with_retries("pred-abc", retries=3, sleep_fn=lambda _s: None)
    assert len(calls) == 3


def test_runway_retries_giveup_raises_failed() -> None:
    """Three 503s exhaust the retry budget → ``EphemeralDeleteFailedError``.

    Would-fail-bug: a missing exhaustion-branch would loop forever or
    silently return after the last 503, leaving the task undeleted.
    """
    from kinoforge.core.errors import EphemeralDeleteFailedError
    from kinoforge.engines.runway import RunwayBackend

    statuses = [503, 503, 503]

    def _fake(url: str, headers: dict[str, str]) -> int:
        return statuses.pop(0)

    backend = RunwayBackend(
        client_factory=lambda: object(),
        sleep=lambda _s: None,
        max_poll=1,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
        token="r-YYYY",
        http_delete=_fake,
    )
    with pytest.raises(EphemeralDeleteFailedError) as exc:
        backend._delete_with_retries("task-xyz", retries=3, sleep_fn=lambda _s: None)
    assert "https://app.runwayml.com/tasks/task-xyz" in str(exc.value)
