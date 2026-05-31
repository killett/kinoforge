"""Pure HTTP-shape helpers for the fal.ai queue adapter.

Kept I/O-free so the wire shape is testable in isolation without HTTP spies.
"""

from __future__ import annotations

import enum
from typing import Any

from kinoforge.core.errors import KinoforgeError


class FalStatus(enum.Enum):
    """Canonical fal queue status classes.

    Maps the fal-side strings (``"IN_QUEUE"``, ``"IN_PROGRESS"``, ``"COMPLETED"``,
    ``"FAILED"``) to a 4-way classification the poll loop branches on.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


_PENDING_STATES = frozenset({"IN_QUEUE", "IN_PROGRESS"})
_COMPLETED_STATES = frozenset({"COMPLETED"})
_FAILED_STATES = frozenset({"FAILED"})


def interpret_status(status_str: str) -> FalStatus:
    """Classify a fal status string into one of the 4 :class:`FalStatus` members.

    Args:
        status_str: The raw status string from the fal queue API.

    Returns:
        The matching :class:`FalStatus` member, or :attr:`FalStatus.UNKNOWN`
        if the input does not match any canonical state.
    """
    if status_str in _COMPLETED_STATES:
        return FalStatus.COMPLETED
    if status_str in _PENDING_STATES:
        return FalStatus.PENDING
    if status_str in _FAILED_STATES:
        return FalStatus.FAILED
    return FalStatus.UNKNOWN


def build_status_url(
    *,
    submit_response: dict[str, Any],
    queue_base: str,
    endpoint: str,
    request_id: str,
) -> str:
    """Return the URL to poll for status.

    Prefers ``submit_response["status_url"]`` when present (the server's
    canonical URL); falls back to the constructed URL otherwise.

    Args:
        submit_response: Decoded JSON body of the submit POST response.
        queue_base: Queue API base URL (e.g. ``"https://queue.fal.run"``).
        endpoint: Endpoint path (e.g. ``"fal-ai/wan/v2.2/t2v"``).
        request_id: The request ID returned by the submit response.

    Returns:
        The status-poll URL.
    """
    server_url = submit_response.get("status_url")
    if isinstance(server_url, str) and server_url:
        return server_url
    return f"{queue_base.rstrip('/')}/{endpoint}/requests/{request_id}/status"


def build_response_url(
    *,
    submit_response: dict[str, Any],
    queue_base: str,
    endpoint: str,
    request_id: str,
) -> str:
    """Return the URL to GET for the final result.

    Prefers ``submit_response["response_url"]`` when present; falls back to
    the constructed URL otherwise.

    Args:
        submit_response: Decoded JSON body of the submit POST response.
        queue_base: Queue API base URL.
        endpoint: Endpoint path.
        request_id: The request ID returned by the submit response.

    Returns:
        The result-fetch URL.
    """
    server_url = submit_response.get("response_url")
    if isinstance(server_url, str) and server_url:
        return server_url
    return f"{queue_base.rstrip('/')}/{endpoint}/requests/{request_id}"


def extract_result_url(data: dict[str, Any], url_path: str) -> str:
    """Walk a dot-path through ``data`` and return the URL string at that path.

    Args:
        data: The decoded JSON result body.
        url_path: Dot-separated key path (e.g. ``"video.url"``).

    Returns:
        The string at the walked path.

    Raises:
        KinoforgeError: The path is missing in the data or does not
            terminate at a string.
    """
    current: Any = data
    for part in url_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KinoforgeError(
                f"fal response missing url_path {url_path!r} at component {part!r}"
            )
        current = current[part]
    if not isinstance(current, str):
        raise KinoforgeError(
            f"fal response url_path {url_path!r} did not terminate at a string "
            f"(got {type(current).__name__})"
        )
    return current
