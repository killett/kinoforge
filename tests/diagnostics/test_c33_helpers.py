"""Unit tests for C33 additions to ``c30_probe.py``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import (
    GraphQLError,
    PodStatusPollerExtended,
    Verdict_P0,
    Verdict_P1,
    _classify_p0,
    _classify_p1,
    issue_single_pod_edit_job,
    snapshot_last_started_at,
)


class _ScriptedClient:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append((query, dict(variables)))
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


# ---- snapshot_last_started_at ---------------------------------------------


def test_snapshot_returns_iso_when_present() -> None:
    client = _ScriptedClient(
        [{"data": {"pod": {"id": "p", "lastStartedAt": "2026-06-15T08:30:00.123Z"}}}]
    )
    got = snapshot_last_started_at(client, "p")
    assert got == "2026-06-15T08:30:00.123Z"
    assert "lastStartedAt" in client.queries[0][0]


def test_snapshot_returns_none_when_pod_gone() -> None:
    client = _ScriptedClient([{"data": {"pod": None}}])
    assert snapshot_last_started_at(client, "p") is None


def test_snapshot_returns_none_when_field_missing() -> None:
    client = _ScriptedClient([{"data": {"pod": {"id": "p"}}}])
    assert snapshot_last_started_at(client, "p") is None


# ---- PodStatusPollerExtended ----------------------------------------------


def _ok_ext(
    uptime: int | None, last_started_at: str | None, status: str = "RUNNING"
) -> dict[str, Any]:
    runtime = {"uptimeInSeconds": uptime} if uptime is not None else None
    return {
        "data": {
            "pod": {
                "id": "p",
                "desiredStatus": status,
                "lastStartedAt": last_started_at,
                "runtime": runtime,
            }
        }
    }


def test_extended_poller_returns_four_tuples() -> None:
    client = _ScriptedClient(
        [
            _ok_ext(1, "2026-06-15T08:00:00Z"),
            _ok_ext(31, "2026-06-15T08:00:00Z"),
            _ok_ext(61, "2026-06-15T08:00:00Z"),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert len(trail) == 3
    assert all(len(t) == 4 for t in trail)
    assert [(t[1], t[2], t[3]) for t in trail] == [
        (1, "2026-06-15T08:00:00Z", "RUNNING"),
        (31, "2026-06-15T08:00:00Z", "RUNNING"),
        (61, "2026-06-15T08:00:00Z", "RUNNING"),
    ]


def test_extended_poller_handles_null_runtime() -> None:
    client = _ScriptedClient([_ok_ext(None, "2026-06-15T08:00:00Z")])
    elapsed = [0.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert trail[0][1] is None
    assert trail[0][2] == "2026-06-15T08:00:00Z"


def test_extended_poller_handles_pod_gone() -> None:
    client = _ScriptedClient([{"data": {"pod": None}}])
    elapsed = [0.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert trail == [(0.0, None, None, None)]


# ---- issue_single_pod_edit_job --------------------------------------------


def test_pod_edit_job_returns_response_dict() -> None:
    client = _ScriptedClient([{"data": {"podEditJob": {"id": "p"}}}])
    resp = issue_single_pod_edit_job(
        client, pod_id="p", new_docker_args="bash -c sleep"
    )
    assert resp == {"data": {"podEditJob": {"id": "p"}}}
    sent_query, sent_vars = client.queries[0]
    assert "podEditJob" in sent_query
    assert sent_vars == {"input": {"podId": "p", "dockerArgs": "bash -c sleep"}}


def test_pod_edit_job_raises_on_errors_block() -> None:
    client = _ScriptedClient(
        [{"errors": [{"message": "boom", "extensions": {"code": "BAD"}}]}]
    )
    with pytest.raises(GraphQLError) as exc:
        issue_single_pod_edit_job(client, pod_id="p", new_docker_args="x")
    assert exc.value.code == "BAD"


# ---- _classify_p0 ----------------------------------------------------------


def _p0(advances: int, negatives: int) -> dict[str, Any]:
    return {
        "n_last_started_at_advances": advances,
        "n_negative_uptime_samples": negatives,
    }


def test_classify_p0_two_advances_is_real_restart() -> None:
    assert _classify_p0(_p0(2, 0)) is Verdict_P0.ORPHAN_REAL_RESTART


def test_classify_p0_three_advances_is_real_restart() -> None:
    assert _classify_p0(_p0(3, 5)) is Verdict_P0.ORPHAN_REAL_RESTART


def test_classify_p0_one_advance_no_negatives_is_ambiguous() -> None:
    assert _classify_p0(_p0(1, 0)) is Verdict_P0.AMBIGUOUS


def test_classify_p0_one_advance_with_negatives_is_ambiguous() -> None:
    assert _classify_p0(_p0(1, 3)) is Verdict_P0.AMBIGUOUS


def test_classify_p0_zero_advances_with_negatives_is_quirk() -> None:
    assert _classify_p0(_p0(0, 5)) is Verdict_P0.ORPHAN_QUIRK


def test_classify_p0_zero_advances_no_negatives_is_quirk() -> None:
    assert _classify_p0(_p0(0, 0)) is Verdict_P0.ORPHAN_QUIRK


# ---- _classify_p1 ----------------------------------------------------------


def _p1(advanced: bool, reset: bool, monotonic: bool) -> dict[str, Any]:
    return {
        "last_started_at_advanced": advanced,
        "uptime_reset_observed": reset,
        "uptime_monotonic_for_90s": monotonic,
    }


def test_classify_p1_advanced_and_reset_is_confirmed() -> None:
    assert (
        _classify_p1(_p1(advanced=True, reset=True, monotonic=False))
        is Verdict_P1.CONFIRMED
    )


def test_classify_p1_stable_and_monotonic_is_denied() -> None:
    assert (
        _classify_p1(_p1(advanced=False, reset=False, monotonic=True))
        is Verdict_P1.DENIED
    )


def test_classify_p1_advanced_without_reset_is_ambiguous() -> None:
    assert (
        _classify_p1(_p1(advanced=True, reset=False, monotonic=False))
        is Verdict_P1.AMBIGUOUS
    )


def test_classify_p1_reset_without_advance_is_ambiguous() -> None:
    assert (
        _classify_p1(_p1(advanced=False, reset=True, monotonic=False))
        is Verdict_P1.AMBIGUOUS
    )


def test_classify_p1_stable_but_not_monotonic_is_ambiguous() -> None:
    assert (
        _classify_p1(_p1(advanced=False, reset=False, monotonic=False))
        is Verdict_P1.AMBIGUOUS
    )
