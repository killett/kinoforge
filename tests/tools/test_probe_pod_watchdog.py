"""Unit tests for :mod:`tools.probe_pod_watchdog` core logic.

Live probe verification (the actual RunPod boot + observe) is a
deliberately-uncovered live-only operation. These tests cover the
polling-loop branching: pass-on-disappear vs fail-on-persist + the
fallback DELETE contract.
"""

from __future__ import annotations


class _FakeSleep:
    """Records every sleep duration without actually sleeping."""

    def __init__(self) -> None:
        self.durations: list[float] = []

    def __call__(self, secs: float) -> None:
        self.durations.append(secs)


class _SequencedPodLister:
    """Returns a different pod list on each successive call.

    Each entry is the list of active pod IDs at the time of that poll.
    Once the sequence is exhausted, returns the last entry forever.
    """

    def __init__(self, sequence: list[list[str]]) -> None:
        self._sequence = list(sequence)
        self.calls: int = 0

    def __call__(self) -> list[str]:
        self.calls += 1
        if not self._sequence:
            return []
        if len(self._sequence) == 1:
            return list(self._sequence[0])
        return list(self._sequence.pop(0))


def test_run_probe_returns_zero_when_pod_disappears_within_wall_cap() -> None:
    """Bug it catches: a probe loop that doesn't distinguish "pod gone"
    from "list call failed" — both cases would yield an empty list, but
    only the former proves selfterm fired. The pod-gone path must return
    exit 0 AND surface "PASS" in the checklist so the operator has an
    unambiguous signal.
    """
    from tools.probe_pod_watchdog import run_probe

    # Pod alive on first two polls, gone on the third.
    lister = _SequencedPodLister(
        [["probe-pod-1"], ["probe-pod-1"], []],
    )
    destroy_calls: list[str] = []

    def _record_destroy(pid: str) -> int:
        destroy_calls.append(pid)
        return 204

    code, lines = run_probe(
        create_pod_and_get_id=lambda: "probe-pod-1",
        list_pod_ids=lister,
        destroy_pod=_record_destroy,
        sleep=_FakeSleep(),
        wall_cap_s=60.0,
        poll_interval_s=5.0,
    )

    assert code == 0
    joined = "\n".join(lines)
    assert "GONE" in joined
    assert "selfterm fired" in joined
    assert "PASS" in joined
    # Fallback DELETE must NOT be called when selfterm fired naturally.
    assert destroy_calls == []


def test_run_probe_returns_nonzero_and_destroys_pod_when_it_persists() -> None:
    """Bug it catches: a probe that times out without cleaning up its own
    probe pod — the pod then continues billing past the wall cap and
    becomes a second leak source. Probe must fall back to DELETE on
    failure to guarantee zero residual cost regardless of selfterm
    behavior.
    """
    from tools.probe_pod_watchdog import run_probe

    # Pod alive every poll — never goes away.
    lister = _SequencedPodLister([["probe-pod-2"]])
    destroy_calls: list[str] = []

    def _record_destroy(pid: str) -> int:
        destroy_calls.append(pid)
        return 204

    code, lines = run_probe(
        create_pod_and_get_id=lambda: "probe-pod-2",
        list_pod_ids=lister,
        destroy_pod=_record_destroy,
        sleep=_FakeSleep(),
        wall_cap_s=30.0,
        poll_interval_s=5.0,
    )

    assert code == 1
    joined = "\n".join(lines)
    assert "STILL ACTIVE" in joined
    assert "FAIL" in joined
    assert "fallback DELETE" in joined
    # Exactly one destroy on the probe pod — no double-DELETE.
    assert destroy_calls == ["probe-pod-2"]


def test_run_probe_polls_at_interval_and_records_each_alive_check() -> None:
    """Bug it catches: a probe that increments elapsed time independently
    from actual sleep calls — a refactor that uses ``time.time()`` instead
    of the injected sleep seam would silently break tests that bound the
    wall cap. Lock down: each iteration consumes one sleep call AND emits
    one "alive at <s>s" line until the pod disappears.
    """
    from tools.probe_pod_watchdog import run_probe

    lister = _SequencedPodLister(
        [["probe-pod-3"], ["probe-pod-3"], ["probe-pod-3"], []],
    )
    sleep_spy = _FakeSleep()

    code, lines = run_probe(
        create_pod_and_get_id=lambda: "probe-pod-3",
        list_pod_ids=lister,
        destroy_pod=lambda _pid: 204,
        sleep=sleep_spy,
        wall_cap_s=120.0,
        poll_interval_s=5.0,
    )

    assert code == 0
    # 3 alive polls + 1 disappear poll = 4 sleeps total.
    assert sleep_spy.durations == [5.0, 5.0, 5.0, 5.0]
    # Three "alive at" lines + one "GONE" line.
    alive_lines = [ln for ln in lines if "alive at" in ln]
    assert len(alive_lines) == 3
    assert "alive at 5s" in alive_lines[0]
    assert "alive at 10s" in alive_lines[1]
    assert "alive at 15s" in alive_lines[2]
