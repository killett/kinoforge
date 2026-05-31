"""Tests for SequentialPool (BackendPool implementation).

AC 1: submit(job).result() returns the Artifact backend.result(backend.submit(job)) would return;
      Future is done() immediately.
AC 2: map(jobs) returns Artifacts in INPUT ORDER.
AC 3: Pool-swap — _ListPool works in the same GenerateClipStage with identical results.
AC 4: add(backend) increments internal list; submitting picks the first registered backend.
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest

from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
    GenerationBackend,
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.core.pool import SequentialPool
from kinoforge.engines.fake import FakeBackend
from kinoforge.stores.local import LocalArtifactStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile(*, supports_native_extension: bool = False) -> ModelProfile:
    return ModelProfile(
        name="test",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=supports_native_extension,
        supports_joint_audio=False,
    )


def _simple_job(prompt: str = "test prompt") -> GenerationJob:
    return GenerationJob(
        spec={},
        segments=[Segment(prompt=prompt)],
        params={},
    )


# ---------------------------------------------------------------------------
# Tiny alternative pool for AC 3 (pool-swap test)
# ---------------------------------------------------------------------------


class _ListPool(BackendPool):
    """Minimal alternative BackendPool for pool-swap AC test."""

    def __init__(self, backend: GenerationBackend) -> None:
        self._backends: list[GenerationBackend] = [backend]

    def add(self, backend: GenerationBackend, *, max_in_flight: int = 1) -> None:  # noqa: D102
        self._backends.append(backend)

    def submit(self, job: GenerationJob) -> Future[Artifact]:
        backend = self._backends[0]
        job_id = backend.submit(job)
        artifact = backend.result(job_id)
        fut: Future[Artifact] = Future()
        fut.set_result(artifact)
        return fut

    def map(self, jobs: list[GenerationJob]) -> list[Artifact]:
        return [self.submit(j).result() for j in jobs]

    def close(self) -> None:  # noqa: D102
        return None


# ---------------------------------------------------------------------------
# AC 1: submit returns a resolved Future immediately
# ---------------------------------------------------------------------------


def test_submit_returns_done_future_with_correct_artifact():
    """Future from submit() is immediately done and holds the right Artifact."""
    probe = _profile()
    backend = FakeBackend(probe=probe)

    # Independently compute what the Artifact should be
    job = _simple_job("hello world")
    direct_job_id = backend.submit(job)
    expected = backend.result(direct_job_id)

    # A fresh backend for SequentialPool (no cross-contamination)
    backend2 = FakeBackend(probe=probe)
    pool = SequentialPool(backend2)
    job2 = _simple_job("hello world")
    fut = pool.submit(job2)

    assert isinstance(fut, Future)
    assert fut.done()
    actual = fut.result()
    assert actual.filename == expected.filename


# ---------------------------------------------------------------------------
# AC 2: map returns Artifacts in input order
# ---------------------------------------------------------------------------


def test_map_returns_artifacts_in_input_order():
    """map() preserves job order regardless of submission timing."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    pool = SequentialPool(backend)

    prompts = ["alpha", "beta", "gamma"]
    jobs = [_simple_job(p) for p in prompts]

    results = pool.map(jobs)

    # Recompute expected filenames independently
    expected_names = []
    for p in prompts:
        j = _simple_job(p)
        jid = backend.submit(j)
        expected_names.append(backend.result(jid).filename)

    assert [r.filename for r in results] == expected_names


# ---------------------------------------------------------------------------
# AC 3: pool-swap — _ListPool produces same results in GenerateClipStage
# ---------------------------------------------------------------------------


def test_pool_swap_same_result(tmp_path: Path) -> None:
    """GenerateClipStage is identical with SequentialPool or _ListPool."""
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.pipeline.generate_clip import GenerateClipStage

    probe = _profile(supports_native_extension=False)
    store1 = LocalArtifactStore(tmp_path / "run1")
    store2 = LocalArtifactStore(tmp_path / "run2")

    backend_a = FakeBackend(probe=probe)
    backend_b = FakeBackend(probe=probe)

    pool_sequential = SequentialPool(backend_a)
    pool_list = _ListPool(backend_b)

    from kinoforge.engines.fake import FakeEngine

    stage_kwargs = dict(
        profile=probe,
        store=store1,
        run_id="swap-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=FakeEngine(
            probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
        ),
    )

    stage_s = GenerateClipStage(pool=pool_sequential, **stage_kwargs)  # type: ignore[arg-type]
    stage_l = GenerateClipStage(pool=pool_list, **stage_kwargs)  # type: ignore[arg-type]
    stage_l.store = store2

    request = GenerationRequest(prompt="hello pool swap", mode="t2v")

    result_s = stage_s.run(request)
    result_l = stage_l.run(request)

    # Both produce an artifact stored in their respective stores; bytes are equal
    bytes_s = store1.get_bytes(result_s.uri)
    bytes_l = store2.get_bytes(result_l.uri)
    assert bytes_s == bytes_l


# ---------------------------------------------------------------------------
# AC 4: add(backend) increments list; submit picks first registered
# ---------------------------------------------------------------------------


def test_add_increments_backends_and_submit_uses_first():
    """add() appends a backend; submit always uses backends[0]."""
    probe = _profile()

    pool = SequentialPool()  # constructed with no backend
    assert len(pool._backends) == 0

    backend1 = FakeBackend(probe=probe)
    backend2 = FakeBackend(probe=probe)
    pool.add(backend1)
    pool.add(backend2)
    assert len(pool._backends) == 2

    # Submitting goes through backend1 (index 0)
    job = _simple_job("first backend")
    fut = pool.submit(job)
    assert fut.done()
    # backend1._jobs should have one entry; backend2 should be empty
    assert len(backend1._jobs) == 1
    assert len(backend2._jobs) == 0


def test_submit_no_backends_raises():
    """SequentialPool with no backends raises RuntimeError on submit."""
    pool = SequentialPool()
    job = _simple_job()
    with pytest.raises(RuntimeError, match="no registered backend"):
        pool.submit(job)


# ---------------------------------------------------------------------------
# Layer G: close() + context-manager parity
# ---------------------------------------------------------------------------


def test_sequential_pool_close_is_noop():
    """SequentialPool.close() is safe to call on an empty pool (no exception)."""
    pool = SequentialPool()
    pool.close()  # no exception = passing the no-op contract


def test_sequential_pool_close_is_idempotent():
    """Calling close() twice is a no-op (no exception)."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    pool = SequentialPool(backend)
    pool.close()
    pool.close()  # must not raise


def test_sequential_pool_as_context_manager_calls_close():
    """`with SequentialPool() as pool:` exits cleanly and pool is closed."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    closed_called: list[bool] = []

    class _SpyPool(SequentialPool):
        def close(self) -> None:
            closed_called.append(True)
            super().close()

    with _SpyPool(backend) as pool:
        assert isinstance(pool, _SpyPool)
        assert pool.submit(_simple_job("hi")).result() is not None
    assert closed_called == [True]


def test_sequential_pool_add_accepts_max_in_flight_kwarg():
    """add(backend, max_in_flight=N) is accepted; SequentialPool ignores N."""
    probe = _profile()
    backend = FakeBackend(probe=probe)
    pool = SequentialPool()
    pool.add(backend, max_in_flight=4)  # must not raise
    assert len(pool._backends) == 1
    # Still uses _backends[0] regardless of cap; verify by submitting.
    result = pool.submit(_simple_job("after-add")).result()
    assert result is not None
