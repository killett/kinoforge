"""Wire-shape unit tests for the C25 Branch B dockerArgs preserve-and-merge satisfier.

C33-m supersession (2026-06-17): the marker-write contract was DISABLED
because every ``podEditJob`` mutation triggers a container-level restart
on the RunPod side (see ``tests/live/_c33_probe_m_evidence.json``). The
six ``test_write_*`` tests that pinned the mutation wire-shape are now
``xfail`` and document the IDEAL contract a future non-mutating satisfier
(B5b ``selfterm-http`` or equivalent) must satisfy. A new ``test_write_*``
test below pins the current no-op + WARNING behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

PHASE24_BASH = (
    'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
    '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
)


class _SpyPost:
    """Records call sequence; canned responses queued in order."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses

    def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self._responses[len(self.calls) - 1]


def _ep(spy: _SpyPost) -> RunPodGraphQLHeartbeatEndpoint:
    return RunPodGraphQLHeartbeatEndpoint(api_key="sk-test", http_post=spy)


def _query_resp(docker_args: str | None) -> dict[str, Any]:
    if docker_args is None:
        return {"data": {"pod": None}}
    return {"data": {"pod": {"id": "pod-x", "dockerArgs": docker_args}}}


def _ok_mutation() -> dict[str, Any]:
    return {"data": {"podEditJob": {"id": "pod-x"}}}


_C33_XFAIL = pytest.mark.xfail(
    reason=(
        "C33-m (2026-06-17): write() is a NO-OP because podEditJob triggers "
        "container restart on RunPod. This test pins the IDEAL preserve-and-merge "
        "wire shape that a future non-mutating satisfier (B5b selfterm-http) must "
        "implement. See tests/live/_c33_probe_m_evidence.json."
    ),
    strict=True,
)


@_C33_XFAIL
def test_write_does_read_then_mutation() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH), _ok_mutation()])
    ep = _ep(spy)
    ep.write("pod-x", datetime(2026, 6, 13, 11, tzinfo=UTC))
    assert len(spy.calls) == 2
    assert "pod(input:" in spy.calls[0][1]["query"]
    assert "podEditJob" in spy.calls[1][1]["query"]


@_C33_XFAIL
def test_write_preserves_bash_base() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written.startswith(PHASE24_BASH)
    assert f"# _kinoforge_hb:{ts.isoformat()}" in written


@_C33_XFAIL
def test_write_strips_stale_marker_before_appending() -> None:
    stale = f"{PHASE24_BASH} # _kinoforge_hb:2026-01-01T00:00:00-07:00"
    spy = _SpyPost([_query_resp(stale), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written.count("_kinoforge_hb:") == 1
    assert ts.isoformat() in written
    assert "2026-01-01" not in written


@_C33_XFAIL
def test_write_bare_pod_produces_no_op_command() -> None:
    spy = _SpyPost([_query_resp(""), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written == f": # _kinoforge_hb:{ts.isoformat()}"


@_C33_XFAIL
def test_write_idempotent_on_repeated_same_ts() -> None:
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    first = f"{PHASE24_BASH} # _kinoforge_hb:{ts.isoformat()}"
    spy = _SpyPost(
        [
            _query_resp(PHASE24_BASH),
            _ok_mutation(),
            _query_resp(first),
            _ok_mutation(),
        ]
    )
    ep = _ep(spy)
    ep.write("pod-x", ts)
    ep.write("pod-x", ts)
    second = spy.calls[3][1]["variables"]["input"]["dockerArgs"]
    assert second == first


def test_write_is_noop_post_c33m(caplog: pytest.LogCaptureFixture) -> None:
    """C33-m: write() must NOT call _http_post (would mutate dockerArgs → restart).

    Bug catch: any code path that mutates dockerArgs while a pod is alive
    triggers a server-side container restart, kills bash, and starts a
    permanent ~31 s restart loop. The C33 (m) and (n) live probes proved
    this for both during-provision and post-provision phases. Write() must
    be a NO-OP and log WARNING the first time it's called per instance.
    """
    import logging

    spy = _SpyPost([])  # Will raise IndexError if anything is sent.
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, tzinfo=UTC)
    with caplog.at_level(
        logging.WARNING, logger="kinoforge.providers.runpod.heartbeat"
    ):
        ep.write("pod-x", ts)
        ep.write("pod-x", ts)  # second call: still no-op, but no second warning.

    # No HTTP POSTs were made (write is a true no-op).
    assert spy.calls == []
    # WARNING was emitted exactly once for the instance.
    c33_warnings = [
        rec
        for rec in caplog.records
        if "C33-m" in rec.getMessage() and "pod-x" in rec.getMessage()
    ]
    assert len(c33_warnings) == 1, [r.getMessage() for r in c33_warnings]
    # Confirm cache hit on second call (different instance → second warning).
    ep.write("pod-y", ts)
    c33_warnings = [rec for rec in caplog.records if "C33-m" in rec.getMessage()]
    assert len(c33_warnings) == 2, [r.getMessage() for r in c33_warnings]


def test_read_extracts_marker_from_bash_tail() -> None:
    iso = "2026-06-13T11:16:26-07:00"
    docker_args = f"{PHASE24_BASH} # _kinoforge_hb:{iso}"
    spy = _SpyPost([_query_resp(docker_args)])
    ep = _ep(spy)
    got = ep.read("pod-x")
    assert got == datetime.fromisoformat(iso)
    assert got is not None
    assert got.tzinfo is not None


def test_read_no_marker_returns_none() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_mid_string_hash_does_not_match() -> None:
    misleading = 'bash -c "echo # _kinoforge_hb:foo && bash /tmp/p.sh"'
    spy = _SpyPost([_query_resp(misleading)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_corrupted_iso_raises_transport_error() -> None:
    docker_args = f"{PHASE24_BASH} # _kinoforge_hb:not-an-iso"
    spy = _SpyPost([_query_resp(docker_args)])
    ep = _ep(spy)
    with pytest.raises(TransportError, match="corrupted heartbeat marker"):
        ep.read("pod-x")


# Standard arms preserved from B5a -----------------------------------------------


def test_read_pod_null_returns_none() -> None:
    spy = _SpyPost([_query_resp(None)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


@_C33_XFAIL
def test_write_graphql_errors_raises_transport_error() -> None:
    spy = _SpyPost(
        [
            _query_resp(PHASE24_BASH),
            {"errors": [{"message": "boom"}]},
        ]
    )
    ep = _ep(spy)
    with pytest.raises(TransportError, match="podEditJob failed"):
        ep.write("pod-x", datetime(2026, 6, 13, 11, tzinfo=UTC))
