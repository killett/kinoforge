"""Streaming event contract for `core/batch.py:batch_generate`.

Adds an opt-in callback hook so external consumers (CLI, log
aggregators, TUIs) can observe per-entry progress mid-run without
core/batch.py touching stdout.  Matches the existing "core stays
print-free" invariant enforced by tests/test_core_invariant.py.

Threading: callbacks are serialized via an internal Lock so multi-
line output never interleaves.  Mirrors the stdlib logging.Handler
serialization pattern.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from kinoforge.core.batch_models import BatchEntry

EventKind = Literal["entry_start", "entry_finish"]
EntryStatus = Literal["ok", "fail", "interrupted", "aborted"]


class BatchEvent(BaseModel):
    """One streaming event emitted by `batch_generate` via `on_event`.

    Attributes:
        kind: ``"entry_start"`` or ``"entry_finish"``.
        batch_id: The batch's top-level namespace ID.
        idx: 0-based index into ``manifest.entries``.
        run_id: ``entry.run_id`` or ``str(idx)`` if the entry didn't
            set one explicitly.
        ts: Local-tz timestamp (project rule — no UTC).
        entry: The full :class:`BatchEntry` for ``entry_start`` events;
            ``None`` on ``entry_finish``.
        status: Terminal status on ``entry_finish``;
            ``None`` on ``entry_start``.
        duration_s: Stage wall-clock cost in seconds on ``entry_finish``;
            ``None`` on ``entry_start``.
        uri: Persisted artifact URI on successful ``entry_finish``;
            ``None`` otherwise.
        error: Stringified exception on failed / interrupted / aborted
            ``entry_finish``; ``None`` otherwise.
    """

    model_config = ConfigDict(frozen=True)

    kind: EventKind
    batch_id: str
    idx: int
    run_id: str
    ts: datetime

    entry: BatchEntry | None = None

    status: EntryStatus | None = None
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _enforce_nullability(self) -> BatchEvent:
        """Enforce the kind → field-set contract documented in the spec."""
        if self.kind == "entry_start":
            if self.entry is None:
                raise ValueError("entry_start requires `entry`")
            if any(
                v is not None
                for v in (self.status, self.duration_s, self.uri, self.error)
            ):
                raise ValueError(
                    "entry_start must not carry status/duration_s/uri/error"
                )
        else:  # entry_finish
            if self.entry is not None:
                raise ValueError("entry_finish must not carry `entry`")
            if self.status is None:
                raise ValueError("entry_finish requires `status`")
            if self.duration_s is None:
                raise ValueError("entry_finish requires `duration_s`")
            if self.status == "ok":
                if self.uri is None:
                    raise ValueError("entry_finish status='ok' requires `uri`")
                if self.error is not None:
                    raise ValueError("entry_finish status='ok' must not carry `error`")
            else:  # fail / interrupted / aborted
                if self.error is None:
                    raise ValueError(
                        f"entry_finish status={self.status!r} requires `error`"
                    )
                if self.uri is not None:
                    raise ValueError(
                        f"entry_finish status={self.status!r} must not carry `uri`"
                    )
        return self


BatchEventCallback = Callable[[BatchEvent], None]


class _LockedEmitter:
    """Serializes a user callback under a single Lock.

    When ``on_event`` is ``None`` the callback is skipped, but the
    ``_started_idxs`` book-keeping is still maintained on ``entry_start``
    so `_mark_remaining_after_fatal` can use :meth:`has_started`
    regardless of whether a streaming consumer was supplied.

    The lock is acquired once per call, covering both the
    ``_started_idxs`` mutation AND the user callback invocation, so a
    user callback that writes multi-line strings to stdout cannot
    interleave with another worker's callback.
    """

    def __init__(self, on_event: BatchEventCallback | None) -> None:
        self._cb = on_event
        self._lock = threading.Lock()
        self._started_idxs: set[int] = set()

    def __call__(self, event: BatchEvent) -> None:
        with self._lock:
            if event.kind == "entry_start":
                self._started_idxs.add(event.idx)
            if self._cb is not None:
                self._cb(event)

    def has_started(self, idx: int) -> bool:
        """Return True if an entry_start for *idx* has already been emitted.

        Args:
            idx: 0-based entry index to query.

        Returns:
            True iff ``entry_start`` was emitted for this index.
        """
        with self._lock:
            return idx in self._started_idxs
