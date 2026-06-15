"""Unit tests for ``c30_probe.destroy_with_retry``."""

from __future__ import annotations

from typing import Any

from kinoforge.diagnostics.c30_probe import destroy_with_retry


class _FakeClient:
    """Scripted client returning queued ``myself.pods`` results in order."""

    def __init__(self, list_results: list[list[str]]) -> None:
        self._results = list(list_results)
        self.terminates: list[str] = []
        self.lists = 0

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "podTerminate" in query:
            self.terminates.append(variables.get("podId", ""))
            return {"data": {"podTerminate": None}}
        if "myself" in query:
            ids = self._results[self.lists] if self.lists < len(self._results) else []
            self.lists += 1
            return {"data": {"myself": {"pods": [{"id": pid} for pid in ids]}}}
        raise AssertionError(f"unexpected query: {query[:80]}")


def test_returns_after_first_terminate_when_absent() -> None:
    client = _FakeClient([[]])
    n = destroy_with_retry(
        client, pod_id="p", attempts=5, sleep_s=0, sleep=lambda _: None
    )
    assert n == 1
    assert client.terminates == ["p"]


def test_retries_when_pod_still_present() -> None:
    client = _FakeClient([["p"], ["p"], []])
    n = destroy_with_retry(
        client, pod_id="p", attempts=5, sleep_s=0, sleep=lambda _: None
    )
    assert n == 3
    assert client.terminates == ["p", "p", "p"]


def test_gives_up_after_max_attempts_without_raising() -> None:
    client = _FakeClient([["p"]] * 10)
    n = destroy_with_retry(
        client, pod_id="p", attempts=4, sleep_s=0, sleep=lambda _: None
    )
    assert n == 4


def test_does_not_terminate_unrelated_pods() -> None:
    client = _FakeClient([["other-pod"]])
    n = destroy_with_retry(
        client, pod_id="p", attempts=3, sleep_s=0, sleep=lambda _: None
    )
    assert n == 1
    assert client.terminates == ["p"]
