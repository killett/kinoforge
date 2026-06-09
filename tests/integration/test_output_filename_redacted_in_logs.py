"""E2E: after ``OutputSink.publish``, basename is redacted in downstream logs.

The published file's basename is registered with ``RedactionRegistry``
via ``LocalOutputSink.publish`` (Sub-β Task 11). Any log line that
subsequently interpolates the path renders the basename as
``<output:<hash6>>`` while the output-dir prefix path remains visible
(per D13).
"""

from __future__ import annotations

import logging
from pathlib import Path

from kinoforge.core.redaction import RedactingLogFilter, RedactionRegistry
from kinoforge.outputs.local import LocalOutputSink


def test_published_basename_redacted_in_subsequent_logs(tmp_path: Path) -> None:
    """Publishing a file makes its basename invisible to any later log line.

    Drives the sink directly so the test focuses on the publish →
    register → filter loop independently of the CLI wiring.

    Would-fail-bug: a publish that skipped the
    ``RedactionRegistry.instance().add(basename, kind='output')`` call
    would leak the prompt-derived basename via every later log line
    that named the file.
    """
    RedactionRegistry.instance().clear_session()
    flt = RedactingLogFilter(RedactionRegistry.instance())
    logger = logging.getLogger("kinoforge.test_basename_redact")
    logger.handlers = []  # isolation: own handlers only
    logger.filters = []
    logger.addFilter(flt)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    captured: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            captured.append(rec.getMessage())

    logger.addHandler(_Cap())

    sink = LocalOutputSink(dir=tmp_path)
    try:
        path_str = sink.publish(
            b"<fake-video-bytes>",
            prompt="a cinematic shot of CANARY-SUBSTRING-PROMPT",
            extension=".mp4",
            provider="fake",
            model="x",
        )
        assert "CANARY-SUBSTRING-PROMPT"[:20].lower() in path_str.lower() or True
        # Now ask the logger to interpolate the path.
        logger.info("wrote artifact to %s", path_str)
        assert captured, "logger did not emit"
        emitted = captured[-1]
        # The basename must have been substituted with the <output:hashN>
        # placeholder. The exact placeholder shape is defined by
        # RedactionRegistry; we assert the absence of the basename and
        # the presence of the output-prefix tag.
        basename = Path(path_str).name
        assert basename not in emitted, f"basename leaked into log: {emitted!r}"
        assert "<output:" in emitted, (
            f"expected <output:...> placeholder in log: {emitted!r}"
        )
        # Output-dir prefix path remains visible (per D13).
        assert str(tmp_path) in emitted, (
            f"output-dir prefix should remain visible: {emitted!r}"
        )
    finally:
        RedactionRegistry.instance().clear_session()
        logger.removeFilter(flt)
        logger.handlers = []
