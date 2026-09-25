"""Behavior: the job worker does not outlive the server that started it.

U50. ``wan_t2v_server``'s job worker is a daemon thread started at the startup
event with no shutdown handler, and it reads ``ARTIFACT_DIR`` dynamically at
write time. So a fire-and-forget ``/generate`` test — one that asserts a
``job_id`` came back and never polls ``/status``, while ``FakePipe.__call__``
sleeps 50 ms — leaves the worker alive past teardown, where it writes to the
baseline ``monkeypatch`` has already restored.

**This is the mechanism that put 281 stub mp4s in ``/workspace/artifacts``
between June and September 2026.** Task 4 of the pod-path plan defanged the
CONSEQUENCE — the fallback moved to ``/tmp/kf-artifacts``, so the repo tree no
longer grows — but the thread-lifecycle defect itself was untouched, and the
suite-wide autouse fixture added at the same time measurably WIDENED the race:
before, only the ~10 ``fresh_server`` modules patched-and-reverted these
attributes; afterwards every test does.

The filing also left a standing warning, which this file is the answer to: do
NOT write an acceptance criterion asserting the scratch dirs are absent after a
suite run. That bar is unreachable while the thread outlives teardown, and one
such criterion was already written and had to be relaxed. The right assertion
is on the THREAD, not on the filesystem — a directory's absence is a
downstream, timing-dependent shadow of the real invariant.
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def server_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """A freshly reloaded server module with a fake pipeline, NOT yet started."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)

    frames = np.zeros((3, 8, 8, 3), dtype=np.uint8)

    class _Out:
        def __init__(self) -> None:
            self.frames = [frames]

    class _Pipe:
        model_id = "fake-wan"

        def __call__(self, **kwargs: Any) -> _Out:
            time.sleep(0.05)
            return _Out()

        def to(self, device: str) -> Any:
            return self

    monkeypatch.setattr(srv, "_load_pipeline", lambda: _Pipe())
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(srv, "LORAS_DIR", tmp_path / "loras")
    return srv


def _run_ready(srv: Any) -> Any:
    """Enter the app's lifespan and wait for the startup event to finish."""
    client = TestClient(srv.app)
    client.__enter__()
    for _ in range(200):
        if srv.ready.is_set():
            break
        time.sleep(0.01)
    assert srv.ready.is_set(), "server never became ready"
    return client


def test_the_worker_is_not_alive_after_the_server_shuts_down(
    server_module: Any,
) -> None:
    """The defect itself, asserted on the thread rather than its debris.

    Bug caught: no shutdown handler. The worker blocks forever on
    ``_q.get()``, survives the TestClient's lifespan exit, and any later job
    it picks up writes through a module attribute the test framework has
    already reverted — the 281-mp4 mechanism.
    """
    srv = server_module
    client = _run_ready(srv)
    # Hold our OWN reference: a clean retirement clears the module handle, so
    # reading `srv._worker_thread` afterwards would test the bookkeeping
    # rather than the thread, and would pass against a handler that merely
    # nulled the attribute while leaving the thread running.
    worker = srv._worker_thread
    assert worker is not None
    assert worker.is_alive()

    client.__exit__(None, None, None)

    assert not worker.is_alive(), (
        "the job worker outlived the server; it will write artifacts through "
        "module attributes the caller has already restored"
    )
    assert srv._worker_thread is None, (
        "a retired worker must not leave a stale handle a restart could reuse"
    )


def test_a_queued_job_still_completes_before_shutdown_returns(
    server_module: Any,
) -> None:
    """Draining, not abandoning — the sentinel goes BEHIND queued work.

    Bug caught: shutting the worker down by setting a flag it checks before
    ``_q.get()``, or by clearing the queue. Either would abandon a job that
    was already accepted, so a ``/generate`` that returned a ``job_id`` would
    silently never produce an artifact — trading a teardown race for lost
    work, which is worse.
    """
    srv = server_module
    client = _run_ready(srv)

    resp = client.post(
        "/generate",
        json={"prompt": "a test", "height": 8, "width": 8, "num_frames": 3},
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    client.__exit__(None, None, None)

    assert srv.jobs[job_id].status in {"done", "error"}, (
        f"job {job_id} was accepted but abandoned at shutdown "
        f"(status={srv.jobs[job_id].status})"
    )


def test_shutdown_is_bounded_when_the_worker_will_not_finish(
    server_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wedged worker must not hang the process.

    Bug caught: an unbounded ``join()``. A real render runs for minutes and a
    genuinely stuck one runs forever, so an unbounded join turns "shut down"
    into "hang" — on a pod that is still billing, and in the test suite into
    a run that never ends. The thread is a daemon, so giving up is safe.

    Driven by a pipeline that blocks on an event nobody sets, which is the
    realistic shape (a hung CUDA call), not by patching the join itself.
    """
    srv = server_module
    wedged = threading.Event()

    class _Wedged:
        model_id = "fake-wan"

        def __call__(self, **kwargs: Any) -> Any:
            wedged.wait(timeout=30)
            raise AssertionError("unreachable in this test")

        def to(self, device: str) -> Any:
            return self

    monkeypatch.setattr(srv, "_load_pipeline", lambda: _Wedged())
    client = _run_ready(srv)
    client.post(
        "/generate",
        json={"prompt": "a test", "height": 8, "width": 8, "num_frames": 3},
    )
    # Let the worker actually pick the job up and block inside the pipe.
    time.sleep(0.2)

    started = time.monotonic()
    try:
        client.__exit__(None, None, None)
    finally:
        wedged.set()
    elapsed = time.monotonic() - started

    assert elapsed < 20.0, (
        f"shutdown blocked {elapsed:.1f}s on a wedged worker; the join must be "
        f"bounded or a stuck render hangs a pod that is still billing"
    )
