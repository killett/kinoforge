# tests/providers/runpod/test_heartbeat.py
"""RunPod GraphQL-dockerArgs heartbeat satisfier wire-shape tests (B5a Task b).

Tests the precise GraphQL payload shape produced by
:class:`RunPodGraphQLHeartbeatEndpoint` via a spy ``http_post`` seam, so
upstream wire drift (RunPod schema change, field rename, missing field)
fails loud rather than silently.

2026-06-12 wire-shape amendment (Task f live smoke): RunPod's
``PodEditJobInput`` has no ``tags`` field.  The implementation now uses
``dockerArgs`` as the carrier — writes a compact JSON blob
``{"_kinoforge_hb": "<ISO8601>"}`` and reads it back from the same field.
These tests reflect that corrected wire shape.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.heartbeat import (
    _HEARTBEAT_JSON_KEY,
    RunPodGraphQLHeartbeatEndpoint,
)


def _make_endpoint(
    responses: list[dict[str, Any]],
) -> tuple[RunPodGraphQLHeartbeatEndpoint, list[tuple[str, dict[str, Any]]]]:
    """Build an endpoint with a spy ``http_post`` returning ``responses`` in order.

    Returns the endpoint and a captured ``[(url, payload), ...]`` list so
    tests can introspect the precise wire shape.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    response_iter = iter(responses)

    def spy_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, payload))
        return next(response_iter)

    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake",
        graphql_url="https://api.runpod.io/graphql",
        http_post=spy_post,
    )
    return endpoint, calls


def test_write_posts_pod_edit_job_mutation_with_docker_args() -> None:
    """write must POST a podEditJob mutation carrying the heartbeat in dockerArgs.

    Bug catch: RunPod's PodEditJobInput has no ``tags`` field (confirmed
    2026-06-12 live smoke).  The implementation now uses ``dockerArgs`` as a
    compact JSON carrier: ``{"_kinoforge_hb": "<ISO8601>"}``.  A payload that
    uses the old ``tags`` key silently gets rejected by the RunPod API with
    BAD_USER_INPUT, so this test asserts the corrected wire shape.
    """
    endpoint, calls = _make_endpoint([{"data": {"podEditJob": {"id": "pod-x"}}}])
    ts = datetime(2026, 6, 12, 14, 23, 5, tzinfo=timezone(timedelta(hours=-7)))

    endpoint.write("pod-x", ts)

    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.runpod.io/graphql"
    assert "podEditJob" in payload["query"]
    variables = payload["variables"]
    assert variables["input"]["podId"] == "pod-x"
    # The heartbeat is encoded as a JSON blob in dockerArgs, NOT as a tags list.
    docker_args_raw = variables["input"]["dockerArgs"]
    assert isinstance(docker_args_raw, str)
    docker_args = json.loads(docker_args_raw)
    assert docker_args[_HEARTBEAT_JSON_KEY] == ts.isoformat()
    # Old tags-based shape must be absent
    assert "tags" not in variables["input"]


def test_write_raises_transport_error_on_graphql_errors() -> None:
    """GraphQL responses with an ``errors`` array must surface as
    TransportError — silently swallowing would let a typo or schema
    change kill heartbeats without operator visibility."""
    endpoint, _ = _make_endpoint(
        [{"errors": [{"message": "field 'podEditJob' missing on Mutation"}]}]
    )

    with pytest.raises(TransportError, match="podEditJob"):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_write_raises_transport_error_when_seam_raises() -> None:
    """The injected ``http_post`` may raise (HTTP non-2xx maps to its own
    exception in the prod seam). The endpoint must re-raise as
    TransportError so consumers can branch on the substrate exception
    rather than the transport's vendor type."""

    def explode(_url: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("HTTP 502 Bad Gateway")

    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake",
        graphql_url="https://api.runpod.io/graphql",
        http_post=explode,
    )
    with pytest.raises(TransportError, match="502"):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_read_returns_parsed_datetime_from_docker_args() -> None:
    """The full write→read round trip on the wire.

    The read path queries ``pod { dockerArgs }`` and parses the JSON blob
    written by :meth:`write`.
    """
    ts_iso = "2026-06-12T14:23:05-07:00"
    docker_args_value = json.dumps({_HEARTBEAT_JSON_KEY: ts_iso}, separators=(",", ":"))
    endpoint, calls = _make_endpoint(
        [
            {
                "data": {
                    "pod": {
                        "id": "pod-x",
                        "dockerArgs": docker_args_value,
                    }
                }
            }
        ]
    )

    got = endpoint.read("pod-x")

    assert got is not None
    assert got.isoformat() == ts_iso
    assert got.utcoffset() == timedelta(hours=-7)  # tzinfo preserved
    # Verify the read payload shape
    assert len(calls) == 1
    url, payload = calls[0]
    assert "pod(" in payload["query"]
    assert payload["variables"]["podId"] == "pod-x"
    # Read query must request dockerArgs, not tags
    assert "dockerArgs" in payload["query"]
    assert "tags" not in payload["query"]


def test_read_returns_none_when_pod_destroyed() -> None:
    """A read after the pod is destroyed returns ``data.pod == null``;
    the satisfier must surface this as ``None``, NOT as TransportError —
    pod-gone is a valid 'no heartbeat available' answer."""
    endpoint, _ = _make_endpoint([{"data": {"pod": None}}])
    assert endpoint.read("ghost-pod") is None


def test_read_returns_none_when_heartbeat_absent() -> None:
    """Pod is alive but the heartbeat dockerArgs was never written.
    Returns None (never-written invariant), not TransportError.

    Covers two sub-cases:
    - dockerArgs is None / empty string — not yet written at all.
    - dockerArgs is a non-kinoforge docker command string — should not
      be mistaken for a heartbeat payload.
    """
    # Sub-case 1: dockerArgs absent (None)
    endpoint, _ = _make_endpoint(
        [{"data": {"pod": {"id": "pod-x", "dockerArgs": None}}}]
    )
    assert endpoint.read("pod-x") is None

    # Sub-case 2: dockerArgs is a real docker command, not JSON
    endpoint2, _ = _make_endpoint(
        [{"data": {"pod": {"id": "pod-x", "dockerArgs": "python /workspace/run.py"}}}]
    )
    assert endpoint2.read("pod-x") is None

    # Sub-case 3: dockerArgs is valid JSON but lacks the kinoforge key
    endpoint3, _ = _make_endpoint(
        [{"data": {"pod": {"id": "pod-x", "dockerArgs": '{"other_key": "value"}'}}}]
    )
    assert endpoint3.read("pod-x") is None


def test_read_raises_transport_error_on_iso_parse_failure() -> None:
    """A corrupted dockerArgs (kinoforge key present but value not ISO) is
    loud-on-violation — should never happen in production but a silent
    fall-through could cascade into 'permanent HEARTBEAT_UNKNOWN' across
    the ledger."""
    corrupted_docker_args = json.dumps(
        {_HEARTBEAT_JSON_KEY: "not-an-iso-date"}, separators=(",", ":")
    )
    endpoint, _ = _make_endpoint(
        [
            {
                "data": {
                    "pod": {
                        "id": "pod-x",
                        "dockerArgs": corrupted_docker_args,
                    }
                }
            }
        ]
    )
    with pytest.raises(TransportError, match="corrupted heartbeat"):
        endpoint.read("pod-x")


def test_read_raises_transport_error_on_graphql_errors() -> None:
    """Same surface as write: GraphQL errors array → TransportError."""
    endpoint, _ = _make_endpoint([{"errors": [{"message": "rate limit exceeded"}]}])
    with pytest.raises(TransportError, match="rate limit"):
        endpoint.read("pod-x")


def test_default_http_post_uses_stdlib() -> None:
    """No new SDK dependency. With http_post=None the constructor must
    pick a stdlib-backed callable, not silently fail or import httpx."""
    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake", graphql_url="https://api.runpod.io/graphql"
    )
    # Just verify the attribute resolves to a callable; we don't fire it.
    assert callable(endpoint._http_post)
