"""Unit tests for ``c30_probe.PodStatusPoller``."""

from __future__ import annotations

from typing import Any

from kinoforge.diagnostics.c30_probe import PodStatusPoller


class _ClockedClient:
    """Returns scripted GraphQL responses in order."""

    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[str] = []
        self.calls = 0

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        self.calls += 1
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


def _ok(uptime: int) -> dict[str, Any]:
    return {
        "data": {
            "pod": {
                "id": "p",
                "desiredStatus": "RUNNING",
                "runtime": {"uptimeInSeconds": uptime},
            }
        }
    }


def test_emits_expected_sample_count() -> None:
    sleeps: list[float] = []
    elapsed = [0.0, 30.0, 60.0, 90.0]
    client = _ClockedClient([_ok(1), _ok(31), _ok(61), _ok(91)])
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=90,
        interval_s=30,
        sleep=lambda s: sleeps.append(s),
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert len(trail) == 4
    assert sleeps == [30, 30, 30]


def test_uptime_propagates_when_runtime_missing() -> None:
    client = _ClockedClient(
        [
            _ok(1),
            {"data": {"pod": {"id": "p", "desiredStatus": "RUNNING", "runtime": None}}},
            _ok(61),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert [u for _, u in trail] == [1, None, 61]


def test_pod_null_yields_none_uptime() -> None:
    client = _ClockedClient(
        [
            {"data": {"pod": None}},
            _ok(31),
            _ok(61),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert trail[0][1] is None
    assert trail[1][1] == 31


def test_query_references_uptime_in_seconds() -> None:
    client = _ClockedClient([_ok(0)])
    elapsed = [0.0]
    PodStatusPoller(
        client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert "uptimeInSeconds" in client.queries[0]


def test_elapsed_seconds_match_clock() -> None:
    client = _ClockedClient([_ok(1), _ok(31)])
    elapsed = [100.0, 130.0]
    poller = PodStatusPoller(
        client,
        pod_id="p",
        window_s=30,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    )
    trail = poller.poll()
    assert [round(t) for t, _ in trail] == [0, 30]
