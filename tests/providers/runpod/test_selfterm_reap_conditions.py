"""Behavioral tests for the rendered in-pod self-terminator (audit B4).

Every other selfterm test in the repo asserts *substring presence* in the
rendered template — the script is never executed, so nothing constrains
*when* the watchdog actually reaps. That blind spot is what let audit bug
B4 live since ``1be572d``: the template defined a ``heartbeat()`` function
nobody called and a ``_job_start`` nobody assigned, so condition 2 was a
boot-relative cap wearing a dead-man's-switch docstring and condition 3
was unreachable.

These tests execute the rendered script in an isolated namespace and drive
``_check_and_reap`` against a fake clock, so they pin the reap CONDITIONS
rather than the text.

The script's ``if __name__ == "__main__"`` guard keeps the 15 s poll loop
from starting under ``exec``. ``time`` and ``urllib.request`` are looked up
as module globals at call time, so replacing them in the namespace after
exec redirects the script's own calls without patching its logic. The pod
environment must be patched *during* exec instead, because the script reads
``os.environ`` at import time.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, TypedDict
from unittest import mock

import pytest

from kinoforge.providers.runpod.selfterm import RENDER

_POD_ID = "podabc123"
_TERMINATE_KEY = "scoped-terminate-key"
_MAIN_API_KEY = "MAIN-API-KEY-MUST-NEVER-REACH-THE-POD"


class _LifecycleParams(TypedDict):
    """The lifecycle knobs ``RENDER`` still accepts after audit B4."""

    idle_timeout: float
    max_lifetime: float
    time_buffer: float


class _FakeClock:
    """Stand-in for the ``time`` module with an operator-driven wall clock."""

    def __init__(self, start: float) -> None:
        """Initialise the clock at ``start`` POSIX seconds."""
        self.now = start
        self.slept: list[float] = []

    def time(self) -> float:
        """Return the current fake wall-clock time in POSIX seconds."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Record a sleep without advancing the clock."""
        self.slept.append(seconds)


class _RecordingUrlopen:
    """Capture ``urllib.request.urlopen`` calls; optionally raise."""

    def __init__(self, error: Exception | None = None) -> None:
        """Configure the fake to raise ``error`` on every call, if given."""
        self.requests: list[Any] = []
        self._error = error

    def __call__(self, req: Any) -> Any:
        """Record ``req`` and either raise the configured error or respond."""
        self.requests.append(req)
        if self._error is not None:
            raise self._error
        return _FakeResponse()


class _FakeResponse:
    """Minimal context-manager response with a no-op ``read``."""

    def __enter__(self) -> _FakeResponse:
        """Enter the context manager."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Exit the context manager without suppressing exceptions."""
        return None

    def read(self) -> bytes:
        """Return an empty body."""
        return b""


class _Pod:
    """An executed selfterm script plus the fakes wired into it."""

    def __init__(self, namespace: dict[str, Any], clock: _FakeClock) -> None:
        """Bind the exec'd script ``namespace`` and its fake ``clock``."""
        self.ns = namespace
        self.clock = clock

    @property
    def urlopen(self) -> _RecordingUrlopen:
        """Return the recording urlopen wired into the script."""
        urlopen: _RecordingUrlopen = self.ns["urllib"].request.urlopen
        return urlopen

    def tick_at(self, elapsed: float) -> None:
        """Advance the fake clock to ``elapsed`` after boot and poll once."""
        self.clock.now = self.start_time + elapsed
        self.ns["_check_and_reap"]()

    @property
    def start_time(self) -> float:
        """Return the boot timestamp the script recorded at import."""
        return float(self.ns["_start_time"])

    @property
    def terminate_calls(self) -> int:
        """Return how many DELETE requests the watchdog has issued."""
        return len(self.urlopen.requests)


def _boot_pod(
    *,
    idle_timeout: float,
    max_lifetime: float,
    time_buffer: float,
    urlopen_error: Exception | None = None,
) -> _Pod:
    """Render, execute, and instrument a selfterm script.

    Args:
        idle_timeout: Idle-timeout lifecycle parameter in seconds.
        max_lifetime: Hard pod-age ceiling in seconds.
        time_buffer: Safety margin subtracted from ``max_lifetime``.
        urlopen_error: Exception the fake transport raises on every
            terminate attempt, or ``None`` for a successful DELETE.

    Returns:
        The booted pod wrapper with fake clock and transport attached.
    """
    script = RENDER(
        idle_timeout=idle_timeout,
        max_lifetime=max_lifetime,
        time_buffer=time_buffer,
    )
    namespace: dict[str, Any] = {"__name__": "selfterm_under_test"}
    pod_env = {
        "RUNPOD_POD_ID": _POD_ID,
        "RUNPOD_TERMINATE_KEY": _TERMINATE_KEY,
        "RUNPOD_API_KEY": _MAIN_API_KEY,
    }
    with mock.patch.dict("os.environ", pod_env, clear=False):
        exec(compile(script, "<selfterm>", "exec"), namespace)  # noqa: S102
    clock = _FakeClock(float(namespace["_start_time"]))
    namespace["time"] = clock
    namespace["urllib"] = SimpleNamespace(
        request=SimpleNamespace(
            urlopen=_RecordingUrlopen(urlopen_error),
            Request=urllib.request.Request,
        )
    )
    return _Pod(namespace, clock)


# ---------------------------------------------------------------------------
# Condition: boot-relative cap (2 * idle_timeout)
# ---------------------------------------------------------------------------
# Params chosen so the cap (2*100 = 200 s) fires long before the effective
# deadline (10000 - 0 = 10000 s), isolating one condition per test.

_CAP_FIRST: _LifecycleParams = {
    "idle_timeout": 100.0,
    "max_lifetime": 10_000.0,
    "time_buffer": 0.0,
}


def test_pod_survives_one_second_before_the_boot_cap() -> None:
    """A pod one second short of 2*idle_timeout must not be reaped.

    Bug caught: a cap computed as ``idle_timeout`` rather than
    ``2 * idle_timeout`` — every pod would die at half its budgeted
    lifetime, mid-render, and the controller would only see it vanish.
    """
    pod = _boot_pod(**_CAP_FIRST)

    pod.tick_at(199.0)

    assert pod.terminate_calls == 0


def test_pod_is_reaped_exactly_at_the_boot_cap() -> None:
    """At exactly 2*idle_timeout after boot the pod terminates itself.

    Bug caught: the money backstop silently never firing (condition
    dropped, or reordered behind an unreachable branch), leaving an
    orphaned pod billing until RunPod's own limits stop it.
    """
    pod = _boot_pod(**_CAP_FIRST)

    pod.tick_at(200.0)

    assert pod.terminate_calls == 1


def test_repeated_ticks_below_the_cap_do_not_postpone_it() -> None:
    """Polling activity must not extend the boot-relative cap.

    Bug caught: reintroducing audit B4 — a tick-side refresh such as
    ``_last_heartbeat = now`` inside the watchdog loop. Nothing in the
    pod writes a real liveness signal, so such a refresh makes the cap
    unreachable and pod lifetime silently unbounded. Fifty sub-cap polls
    must leave the reap at 200 s exactly where it was.
    """
    pod = _boot_pod(**_CAP_FIRST)

    for elapsed in range(0, 200, 4):
        pod.tick_at(float(elapsed))
    assert pod.terminate_calls == 0, "reaped early while below the cap"

    pod.tick_at(200.0)

    assert pod.terminate_calls == 1


# ---------------------------------------------------------------------------
# Condition: effective deadline (max_lifetime - time_buffer)
# ---------------------------------------------------------------------------
# Params chosen so the deadline (1000 - 200 = 800 s) fires long before the
# cap (2*100000 = 200000 s), isolating the other condition.

_DEADLINE_FIRST: _LifecycleParams = {
    "idle_timeout": 100_000.0,
    "max_lifetime": 1_000.0,
    "time_buffer": 200.0,
}


def test_pod_survives_one_second_before_the_effective_deadline() -> None:
    """At 799 s with a deadline of 800 s the pod keeps running.

    Bug caught: an off-by-one or premature-deadline error that reaps a
    pod still inside its paid-for budget.
    """
    pod = _boot_pod(**_DEADLINE_FIRST)

    pod.tick_at(799.0)

    assert pod.terminate_calls == 0


def test_pod_is_reaped_at_max_lifetime_minus_time_buffer() -> None:
    """The deadline is ``max_lifetime - time_buffer``, hand-computed as 800 s.

    Bug caught: the sign flipping to ``max_lifetime + time_buffer`` (or
    the buffer being dropped), so the pod outlives its cost budget by the
    safety margin that exists precisely to prevent that.
    """
    pod = _boot_pod(**_DEADLINE_FIRST)

    pod.tick_at(800.0)

    assert pod.terminate_calls == 1


def test_effective_deadline_is_exposed_as_an_absolute_timestamp() -> None:
    """``effective_deadline()`` returns boot + max_lifetime - time_buffer.

    Bug caught: the helper returning a relative duration (800) while
    callers compare it against ``time.time()`` — a mismatch that makes
    the deadline fire immediately at boot and kills every pod on the
    first watchdog tick.

    Asserted as a delta, not as an absolute timestamp: ``approx`` applies
    a *relative* tolerance, and against a ~1.7e9 POSIX value that is
    ±1700 s — wide enough to swallow a wrong ``time_buffer`` sign. A
    mutation run proved the absolute form passed under that very bug.
    """
    pod = _boot_pod(**_DEADLINE_FIRST)

    elapsed_to_deadline = pod.ns["effective_deadline"]() - pod.start_time

    assert elapsed_to_deadline == pytest.approx(800.0)


def test_boot_cap_deadline_is_exposed_as_an_absolute_timestamp() -> None:
    """``boot_cap_deadline()`` returns boot + 2 * idle_timeout.

    Bug caught: the same relative-vs-absolute mismatch on the cap helper,
    which would reap every pod on its first tick; also a cap computed
    from ``idle_timeout`` instead of ``2 * idle_timeout``. Asserted as a
    delta for the ``approx`` tolerance reason above.
    """
    pod = _boot_pod(**_CAP_FIRST)

    elapsed_to_cap = pod.ns["boot_cap_deadline"]() - pod.start_time

    assert elapsed_to_cap == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Termination transport
# ---------------------------------------------------------------------------


def test_terminate_issues_a_scoped_delete_to_the_rest_pods_endpoint() -> None:
    """Reaping DELETEs ``rest.runpod.io/v1/pods/{id}`` with the scoped key.

    Bug caught: the live 2026-06-03 defect — the watchdog called the
    SERVERLESS endpoint ``POST api.runpod.io/v2/{id}/stop``, which 4xxs
    against a pod ID and is swallowed by the broad except, so the pod
    survived past its deadline and kept billing.
    """
    pod = _boot_pod(**_CAP_FIRST)

    pod.tick_at(200.0)

    (request,) = pod.urlopen.requests
    assert request.full_url == f"https://rest.runpod.io/v1/pods/{_POD_ID}"
    assert request.get_method() == "DELETE"
    assert request.get_header("Authorization") == f"Bearer {_TERMINATE_KEY}"


def test_terminate_never_authenticates_with_the_main_api_key() -> None:
    """The pod-side reap uses the scoped key only, never RUNPOD_API_KEY.

    Bug caught: the template falling back to ``RUNPOD_API_KEY`` when the
    scoped key is absent — that key grants full account control and the
    pod environment is readable via the RunPod REST pod-detail endpoint.
    """
    pod = _boot_pod(**_CAP_FIRST)

    pod.tick_at(200.0)

    (request,) = pod.urlopen.requests
    assert _MAIN_API_KEY not in str(request.headers)
    assert _MAIN_API_KEY not in request.full_url


def test_transport_failure_is_swallowed_and_retried_on_the_next_tick() -> None:
    """A failing DELETE must not escape ``_check_and_reap``.

    Bug caught: an exception propagating out of the watchdog loop kills
    the only in-pod thread that can reap, so a transient RunPod 5xx at
    the deadline converts into a pod that bills indefinitely.
    """
    boom = urllib.error.HTTPError(
        url="https://rest.runpod.io/v1/pods/x",
        code=503,
        msg="Service Unavailable",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    pod = _boot_pod(**_CAP_FIRST, urlopen_error=boom)

    pod.tick_at(200.0)
    pod.tick_at(215.0)

    assert pod.terminate_calls == 2


# ---------------------------------------------------------------------------
# Dead-surface lockdown (audit B4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol", ["heartbeat", "_last_heartbeat", "_job_start", "_JOB_TIMEOUT"]
)
def test_rendered_script_defines_no_dead_liveness_surface(symbol: str) -> None:
    """The template carries no unwired heartbeat or job-timeout state.

    Bug caught: audit B4 itself returning — a ``heartbeat()`` nobody
    calls and a ``_job_start`` nobody assigns, which make the reap
    conditions read as activity-driven when they are purely
    boot-relative. Readers then trust a dead-man's switch that does not
    exist.
    """
    pod = _boot_pod(**_CAP_FIRST)

    assert symbol not in pod.ns


def test_render_rejects_a_job_timeout_argument() -> None:
    """``job_timeout`` is gone from the RENDER surface, not merely unused.

    Bug caught: keeping the parameter after deleting its only consumer —
    callers would go on threading ``spec.lifecycle.job_timeout_s`` into a
    script that ignores it, implying a per-job limit the pod never
    enforces.
    """
    with pytest.raises(TypeError):
        RENDER(  # type: ignore[call-arg]
            idle_timeout=100.0,
            max_lifetime=10_000.0,
            job_timeout=900.0,
            time_buffer=0.0,
        )
