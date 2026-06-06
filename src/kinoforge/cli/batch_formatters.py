"""CLI streaming formatters consuming BatchEvent (Layer L-T4 T4).

Three formatters share a small interface:
  * ``emit(event: BatchEvent) -> None`` — write one line per
    streaming event.
  * ``render_summary(result: BatchResult) -> None`` — write the
    final summary block once batch_generate returns.

``HumanFormatter`` carries the summary-table layout lifted verbatim
from the pre-Layer-L-T4 ``cli/_commands.py:_cmd_batch`` block so the
on-screen result block doesn't drift.  ``JsonlFormatter`` emits one
JSON object per event line plus a terminal ``{"kind":"batch_summary",
...}`` object.  ``NoOpFormatter`` suppresses ``emit`` but delegates
``render_summary`` to ``HumanFormatter`` — operators opting out of
mid-run lines still want the result block.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from kinoforge.core.batch_events import BatchEvent
from kinoforge.core.batch_models import BatchResult


class HumanFormatter:
    """Operator-friendly streaming lines + final summary table."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        """Initialise with the given output stream."""
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        """Write one human-readable line per BatchEvent."""
        prefix = f"[{event.batch_id}] [{event.idx + 1}/{event.run_id}]"
        if event.kind == "entry_start":
            entry = event.entry
            mode = entry.mode if entry is not None else "?"
            prompt = (entry.prompt or "")[:60] if entry is not None else ""
            self._stream.write(f"{prefix} START mode={mode} prompt={prompt!r}\n")
        else:
            status = (event.status or "?").upper()
            dur = f"{event.duration_s:.1f}s" if event.duration_s is not None else "—"
            tail = event.uri or event.error or ""
            self._stream.write(f"{prefix} {status} {dur} {tail}\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        """Final summary table — verbatim layout from pre-Layer-L-T4 _cmd_batch.

        Auto-sizes the run_id column to the widest entry + 1; status
        column is fixed 12-wide (max label "interrupted" is 11).
        """
        rid_width = max((len(o.run_id) for o in result.outcomes), default=1) + 1
        self._stream.write("\nsummary:\n")
        for o in result.outcomes:
            status_label = o.status.upper()
            duration = f"{o.duration_s:.1f}s" if o.duration_s is not None else "—"
            detail = o.uri if o.uri else (o.error or "")
            self._stream.write(
                f"  {o.run_id:<{rid_width}s} {status_label:<12s} "
                f"{duration:<8s} {detail}\n"
            )
        self._stream.write(f"batch-id: {result.batch_id}\n")
        n_ok = sum(1 for o in result.outcomes if o.status == "ok")
        n_fail = len(result.outcomes) - n_ok
        self._stream.write(
            f"results:  {n_ok}/{len(result.outcomes)} ok, {n_fail} failed\n"
        )
        self._stream.flush()


class JsonlFormatter:
    """Machine-readable JSONL — one event per line, terminal batch_summary object."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        """Initialise with the given output stream."""
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        """Write one JSON line for the event."""
        self._stream.write(event.model_dump_json() + "\n")
        self._stream.flush()

    def render_summary(self, result: BatchResult) -> None:
        """Write a terminal batch_summary JSON object."""
        payload = {"kind": "batch_summary", **result.to_dict()}
        self._stream.write(json.dumps(payload) + "\n")
        self._stream.flush()


class NoOpFormatter:
    """``--stream-format=none``: suppress mid-run lines; keep summary."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        """Initialise with the given output stream."""
        self._stream = stream

    def emit(self, event: BatchEvent) -> None:
        """Intentional no-op: operators opted out of mid-run streaming."""
        return None

    def render_summary(self, result: BatchResult) -> None:
        """Delegate to HumanFormatter so the final block is unchanged."""
        HumanFormatter(self._stream).render_summary(result)


_Formatter = HumanFormatter | JsonlFormatter | NoOpFormatter
_DISPATCH: dict[str, type[_Formatter]] = {
    "human": HumanFormatter,
    "jsonl": JsonlFormatter,
    "none": NoOpFormatter,
}


def build_formatter(kind: str, stream: TextIO = sys.stdout) -> _Formatter:
    """Return a fresh formatter for the given kind.

    Args:
        kind: One of ``"human"``, ``"jsonl"``, or ``"none"``.
        stream: Output stream (default: stdout).

    Returns:
        A formatter instance of the corresponding class.

    Raises:
        KeyError: ``kind`` is not in ``{"human", "jsonl", "none"}``.
    """
    return _DISPATCH[kind](stream)
