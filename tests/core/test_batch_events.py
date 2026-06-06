"""Tests for core/batch_events.py — BatchEvent + _LockedEmitter."""

from __future__ import annotations

import threading
import time
from datetime import datetime

import pytest
from pydantic import ValidationError

from kinoforge.core.batch_events import BatchEvent, _LockedEmitter
from kinoforge.core.batch_models import BatchEntry


def _make_entry() -> BatchEntry:
    return BatchEntry(prompt="hi", mode="t2v")


def _now() -> datetime:
    return datetime.now()


def test_event_frozen() -> None:
    """Bug: mutating a streamed event leaks state to other subscribers.

    A frozen model is the contract that prevents an enterprising
    formatter from rewriting an event's status after the fact.
    """
    ev = BatchEvent(
        kind="entry_start",
        batch_id="b",
        idx=0,
        run_id="0",
        ts=_now(),
        entry=_make_entry(),
    )
    with pytest.raises(ValidationError):
        ev.idx = 99


def test_event_json_roundtrip() -> None:
    """Bug: JSONL formatter line drift between python versions.

    Locks the on-wire shape: dump → validate is identity-equal,
    so JSONL consumers can rely on the schema.
    """
    ev = BatchEvent(
        kind="entry_finish",
        batch_id="b",
        idx=2,
        run_id="alpha",
        ts=_now(),
        status="ok",
        duration_s=1.5,
        uri="local://x/y",
    )
    blob = ev.model_dump_json()
    restored = BatchEvent.model_validate_json(blob)
    assert restored == ev


def test_locked_emitter_serializes() -> None:
    """Bug: two workers emitting concurrently produce interleaved stdout.

    Records (t_enter, t_exit) per callback call.  Asserts no two
    windows overlap — i.e. the user callback is called sequentially.
    """
    barrier = threading.Barrier(4)
    log: list[tuple[float, float]] = []
    log_lock = threading.Lock()

    def cb(_event: BatchEvent) -> None:
        t0 = time.monotonic()
        time.sleep(0.02)  # widen the window to surface real overlaps
        t1 = time.monotonic()
        with log_lock:
            log.append((t0, t1))

    emit = _LockedEmitter(cb)

    def worker(i: int) -> None:
        barrier.wait()
        emit(
            BatchEvent(
                kind="entry_start",
                batch_id="b",
                idx=i,
                run_id=str(i),
                ts=_now(),
                entry=_make_entry(),
            )
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_sorted = sorted(log, key=lambda w: w[0])
    for (_, e1), (s2, _) in zip(log_sorted, log_sorted[1:], strict=False):
        assert e1 <= s2, f"overlap: window ending {e1} vs next start {s2}"


def test_locked_emitter_none_callback_noop() -> None:
    """Bug: None callback path raises instead of being a silent no-op.

    The opt-out path (on_event=None) must accept events silently AND
    still track _started_idxs so _mark_remaining_after_fatal can use
    has_started() regardless of whether a user callback was supplied.
    """
    emit = _LockedEmitter(None)
    emit(
        BatchEvent(
            kind="entry_start",
            batch_id="b",
            idx=7,
            run_id="7",
            ts=_now(),
            entry=_make_entry(),
        )
    )
    assert emit.has_started(7) is True
    assert emit.has_started(99) is False


def test_started_idxs_tracking() -> None:
    """Bug: has_started misreports because the set isn't mutated.

    Every entry_start adds; entry_finish does NOT add.
    """
    seen: list[int] = []
    emit = _LockedEmitter(lambda ev: seen.append(ev.idx))

    emit(
        BatchEvent(
            kind="entry_start",
            batch_id="b",
            idx=1,
            run_id="1",
            ts=_now(),
            entry=_make_entry(),
        )
    )
    emit(
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=1,
            run_id="1",
            ts=_now(),
            status="ok",
            duration_s=0.0,
            uri="local://x",
        )
    )

    assert emit.has_started(1) is True
    assert emit.has_started(2) is False
    assert seen == [1, 1]  # callback fires for BOTH kinds; idx=1 each time


def test_field_nullability_rules() -> None:
    """Bug: silent acceptance of malformed events corrupts JSONL output.

    Per spec §3.3:
      * entry_start MUST carry entry; MUST NOT carry status/duration_s/uri/error
      * entry_finish MUST carry status + duration_s; MUST NOT carry entry
      * uri set iff status=="ok"; error set iff status in {fail, interrupted, aborted}
    """
    # entry_start with status set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_start",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            entry=_make_entry(),
            status="ok",
        )
    # entry_finish with entry set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            entry=_make_entry(),
            status="ok",
            duration_s=0.0,
            uri="local://x",
        )
    # entry_finish status="ok" without uri -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="ok",
            duration_s=0.0,
        )
    # entry_finish status="fail" without error -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="fail",
            duration_s=0.5,
        )
    # entry_start without entry -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_start",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
        )
    # entry_finish without status -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            duration_s=0.5,
            error="boom",
        )
    # entry_finish without duration_s -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="ok",
            uri="local://x",
        )
    # entry_finish status="ok" with error set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="ok",
            duration_s=0.0,
            uri="local://x",
            error="should not be here",
        )
    # entry_finish status="interrupted" with uri set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="interrupted",
            duration_s=0.5,
            uri="local://x",
            error="batch aborted",
        )
    # entry_finish status="aborted" with uri set -> reject
    with pytest.raises(ValidationError):
        BatchEvent(
            kind="entry_finish",
            batch_id="b",
            idx=0,
            run_id="0",
            ts=_now(),
            status="aborted",
            duration_s=0.0,
            uri="local://x",
            error="batch aborted",
        )
