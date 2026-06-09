"""LocalOutputSink.publish registers basename with RedactionRegistry."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from kinoforge.core.redaction import RedactionRegistry
from kinoforge.outputs.local import LocalOutputSink


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _publish(sink: LocalOutputSink, prompt: str = "a cinematic shot") -> str:
    return sink.publish(
        b"video bytes",
        prompt=prompt,
        extension=".mp4",
        provider="fake",
        model="m",
    )


def test_publish_registers_basename(tmp_path: Path) -> None:
    """publish() returns a path whose basename is redacted by the registry.

    Would-fail-bug: forgetting the post-write add() means every log line
    that prints the path or basename leaks the timestamp + prompt-slug
    derived filename of the published clip.
    """
    sink = LocalOutputSink(dir=tmp_path)
    path_str = _publish(sink)
    basename = Path(path_str).name
    redacted = RedactionRegistry.instance().redact(path_str)
    assert basename not in redacted
    assert "<output:" in redacted
    # The user-configured output dir path prefix remains visible.
    assert str(tmp_path) in redacted


def test_published_file_keeps_permissive_name(tmp_path: Path) -> None:
    """Registration does not rename the file on disk."""
    sink = LocalOutputSink(dir=tmp_path)
    path_str = _publish(sink)
    assert Path(path_str).is_file()
    # The on-disk filename keeps the prompt-slug-derived name verbatim.
    assert Path(path_str).read_bytes() == b"video bytes"


def test_publish_idempotent_re_register(tmp_path: Path) -> None:
    """Publishing twice with the same registered basename is idempotent."""
    sink = LocalOutputSink(dir=tmp_path)
    first = _publish(sink, prompt="same prompt")
    # Second publish writes a different file (collision-resolved name),
    # but if the registry pre-registers any colliding basename, add()
    # must remain idempotent rather than raising.
    second = _publish(sink, prompt="same prompt")
    assert first != second
    # Both basenames are registered.
    r = RedactionRegistry.instance()
    assert r.is_active
    assert Path(first).name not in r.redact(first)
    assert Path(second).name not in r.redact(second)
