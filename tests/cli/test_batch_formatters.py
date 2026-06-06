"""Tests for cli/batch_formatters.py (Layer L-T4 T4)."""

from __future__ import annotations

import io
import json
from datetime import datetime

import pytest

from kinoforge.cli.batch_formatters import (
    HumanFormatter,
    JsonlFormatter,
    NoOpFormatter,
    build_formatter,
)
from kinoforge.core.batch_events import BatchEvent
from kinoforge.core.batch_models import BatchEntry, BatchOutcome, BatchResult


def _start_event() -> BatchEvent:
    return BatchEvent(
        kind="entry_start",
        batch_id="b1",
        idx=2,
        run_id="alpha",
        ts=datetime(2026, 6, 5, 10, 30, 0),
        entry=BatchEntry(prompt="hello world", mode="t2v"),
    )


def _finish_ok_event() -> BatchEvent:
    return BatchEvent(
        kind="entry_finish",
        batch_id="b1",
        idx=2,
        run_id="alpha",
        ts=datetime(2026, 6, 5, 10, 30, 1, 500000),
        status="ok",
        duration_s=1.5,
        uri="local://b1/alpha/clip.mp4",
    )


def _sample_result() -> BatchResult:
    return BatchResult(
        batch_id="b1",
        started_at="2026-06-05T10:30:00",
        finished_at="2026-06-05T10:30:05",
        outcomes=[
            BatchOutcome(
                run_id="alpha",
                status="ok",
                duration_s=1.5,
                uri="local://b1/alpha/clip.mp4",
            ),
            BatchOutcome(
                run_id="beta", status="fail", duration_s=0.4, error="ValueError: nope"
            ),
        ],
    )


def test_human_emit_start() -> None:
    """Bug: emit silently drops events without writing.

    Without a START line per entry, operators watching a batch see only
    the header + summary — exactly the deferral this layer closes.
    """
    buf = io.StringIO()
    HumanFormatter(buf).emit(_start_event())
    line = buf.getvalue()
    assert line.startswith("[b1] [3/alpha] START ")
    assert "mode=t2v" in line
    assert "prompt='hello world'" in line
    assert line.endswith("\n")


def test_human_emit_finish_ok() -> None:
    """Bug: success line drops duration or uri.

    Without the uri, operators tailing the stream cannot tell which
    artifact path the entry produced.
    """
    buf = io.StringIO()
    HumanFormatter(buf).emit(_finish_ok_event())
    line = buf.getvalue()
    assert line.startswith("[b1] [3/alpha] OK ")
    assert "1.5s" in line
    assert "local://b1/alpha/clip.mp4" in line


def test_human_render_summary_matches_legacy_shape() -> None:
    """Bug: summary table drifts from the pre-Layer-L-T4 _cmd_batch shape.

    The block has shipped with this exact layout since Layer L; CI
    fixtures + ops dashboards parse the format.  A drift would silently
    break downstream consumers.
    """
    buf = io.StringIO()
    HumanFormatter(buf).render_summary(_sample_result())
    out = buf.getvalue()
    assert "\nsummary:\n" in out
    assert "alpha" in out and "OK" in out and "1.5s" in out
    assert "beta" in out and "FAIL" in out and "ValueError: nope" in out
    assert "batch-id: b1" in out
    assert "results:  1/2 ok, 1 failed" in out


def test_jsonl_emit_roundtrips() -> None:
    """Bug: JSONL line is unparseable or drops fields on dump.

    Locks the on-wire schema: every emitted line MUST be a valid
    BatchEvent that round-trips back to identity equality, so log
    aggregators can rely on the schema.
    """
    buf = io.StringIO()
    ev = _finish_ok_event()
    JsonlFormatter(buf).emit(ev)
    line = buf.getvalue().rstrip("\n")
    parsed = json.loads(line)
    assert parsed["kind"] == "entry_finish"
    assert parsed["status"] == "ok"
    assert parsed["uri"] == "local://b1/alpha/clip.mp4"
    # round-trip back to a BatchEvent
    restored = BatchEvent.model_validate_json(line)
    assert restored == ev


def test_jsonl_render_summary_emits_batch_summary_object() -> None:
    """Bug: JSONL stream lacks a terminal 'done' marker for consumers.

    Without the batch_summary line, a tail-following log consumer has
    no way to know the stream is complete (vs. a hung batch).
    """
    buf = io.StringIO()
    JsonlFormatter(buf).render_summary(_sample_result())
    line = buf.getvalue().rstrip("\n")
    parsed = json.loads(line)
    assert parsed["kind"] == "batch_summary"
    assert parsed["batch_id"] == "b1"
    assert parsed["entries"][0]["run_id"] == "alpha"


def test_noop_emit_writes_nothing() -> None:
    """Bug: --stream-format=none accidentally still streams.

    Operators who opt out of mid-run lines expect silence on the
    emit path.  Anything written would defeat the opt-out.
    """
    buf = io.StringIO()
    NoOpFormatter(buf).emit(_start_event())
    NoOpFormatter(buf).emit(_finish_ok_event())
    assert buf.getvalue() == ""


def test_noop_render_summary_matches_human() -> None:
    """Bug: --stream-format=none drops the final summary table.

    NoOpFormatter suppresses mid-run lines; it MUST still render the
    final operator summary identical to HumanFormatter so 'none'
    operators are not deprived of the result block.
    """
    buf_noop = io.StringIO()
    NoOpFormatter(buf_noop).render_summary(_sample_result())
    buf_human = io.StringIO()
    HumanFormatter(buf_human).render_summary(_sample_result())
    assert buf_noop.getvalue() == buf_human.getvalue()


def test_build_formatter_dispatch() -> None:
    """Bug: build_formatter silently routes 'xyz' to a default.

    Unknown kinds must raise so argparse's choices= safety net
    actually fires when a user mistypes.
    """
    assert isinstance(build_formatter("human"), HumanFormatter)
    assert isinstance(build_formatter("jsonl"), JsonlFormatter)
    assert isinstance(build_formatter("none"), NoOpFormatter)
    with pytest.raises(KeyError):
        build_formatter("xyz")
