"""Pure HTTP-shape helpers for FalBackend (Layer I Task 11)."""

from __future__ import annotations

import pytest

from kinoforge.engines.fal.wire import (
    FalStatus,
    build_response_url,
    build_status_url,
    extract_result_url,
    interpret_status,
)


def test_build_status_url_uses_response_when_present() -> None:
    """build_status_url prefers the server-supplied status_url over construction.

    Bug catch: a helper that always reconstructs the URL would discard the
    server's canonical URL, breaking providers that route requests through
    request-id-specific status hosts.
    """
    url = build_status_url(
        submit_response={
            "request_id": "r1",
            "status_url": "https://q.fal/x/status",
        },
        queue_base="https://q.fal",
        endpoint="endpoint",
        request_id="r1",
    )
    assert url == "https://q.fal/x/status"


def test_build_status_url_falls_back_to_construction() -> None:
    """When submit_response omits status_url, build one from queue_base + endpoint.

    Bug catch: a helper that returned "" or None on missing status_url would
    cause the poll loop to GET an empty URL.
    """
    url = build_status_url(
        submit_response={"request_id": "r1"},
        queue_base="https://queue.fal.run",
        endpoint="fal-ai/wan",
        request_id="r1",
    )
    assert url == "https://queue.fal.run/fal-ai/wan/requests/r1/status"


def test_build_response_url_uses_response_when_present() -> None:
    """build_response_url prefers the server-supplied response_url.

    Bug catch: ignoring the server URL means we GET the wrong endpoint for
    providers that use dedicated result hosts.
    """
    url = build_response_url(
        submit_response={
            "request_id": "r1",
            "response_url": "https://q.fal/x",
        },
        queue_base="https://q.fal",
        endpoint="endpoint",
        request_id="r1",
    )
    assert url == "https://q.fal/x"


def test_build_response_url_falls_back_to_construction() -> None:
    """When submit_response omits response_url, build one.

    Bug catch: empty-string return would yield a 404 on result fetch.
    """
    url = build_response_url(
        submit_response={"request_id": "r1"},
        queue_base="https://queue.fal.run",
        endpoint="fal-ai/wan",
        request_id="r1",
    )
    assert url == "https://queue.fal.run/fal-ai/wan/requests/r1"


def test_extract_result_url_walks_dot_path() -> None:
    """Extract a nested URL via dot-path walk.

    Bug catch: a walker that only handles top-level keys would miss
    realistic provider response shapes like {"video": {"url": "..."}}.
    """
    data = {"video": {"url": "https://media.fal/v.mp4", "size": 1234}}
    assert extract_result_url(data, "video.url") == "https://media.fal/v.mp4"


def test_extract_result_url_raises_on_missing_path() -> None:
    """A missing dot-path step must raise KinoforgeError, not return ''.

    Bug catch: silently returning '' would let result() succeed with an
    Artifact pointing at nothing — surface ambiguity should be a hard
    error in the fal wire helper.
    """
    from kinoforge.core.errors import KinoforgeError

    data: dict[str, object] = {"video": {}}
    with pytest.raises(KinoforgeError) as exc:
        extract_result_url(data, "video.url")
    assert "url_path" in str(exc.value)


def test_interpret_status_recognizes_canonical_states() -> None:
    """COMPLETED, IN_QUEUE, IN_PROGRESS, FAILED all recognized.

    Bug catch: a mis-mapped table that classifies IN_QUEUE as UNKNOWN would
    cause every job to hard-error on the first poll instead of waiting.
    """
    assert interpret_status("COMPLETED") is FalStatus.COMPLETED
    assert interpret_status("IN_QUEUE") is FalStatus.PENDING
    assert interpret_status("IN_PROGRESS") is FalStatus.PENDING
    assert interpret_status("FAILED") is FalStatus.FAILED


def test_interpret_status_unknown_returns_unknown_marker() -> None:
    """An unknown status string returns FalStatus.UNKNOWN, not an exception.

    Bug catch: raising inside the helper would conflate "we don't know what
    this string means" with "the API failed", losing context in the engine
    error message.
    """
    assert interpret_status("SOMETHING_ELSE") is FalStatus.UNKNOWN
