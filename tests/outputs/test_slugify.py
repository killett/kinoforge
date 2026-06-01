"""Behavioral tests for slugify() — the ASCII-conservative prompt slugger.

Each test states (1) what behavior is under test and (2) the concrete bug
the assertion catches if the implementation regresses.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import UnknownAdapter
from kinoforge.outputs import get_sink, register_sink
from kinoforge.outputs.base import OutputPublishError, slugify


def test_slugify_typical_prompt_truncated_to_20() -> None:
    """A normal English prompt is truncated to 20 chars with whitespace
    replaced by dashes.  Catches a regression where the truncation step
    swaps order with the dash replacement and produces partial-word
    artifacts.
    """
    assert slugify("Waves crashing on basalt cliffs at dusk!") == "Waves-crashing-on-ba"


def test_slugify_emoji_only_input_falls_back_to_clip() -> None:
    """Emoji + punctuation strips to nothing; fallback "clip" prevents an
    empty filename.  Catches a regression where the empty-result branch is
    forgotten and the sink writes "<ts>_.mp4".
    """
    assert slugify("🌊 ???") == "clip"


def test_slugify_empty_input_falls_back_to_clip() -> None:
    """Empty prompt → "clip".  Catches a regression where slugify of "" raises
    IndexError on a tail-strip step.
    """
    assert slugify("") == "clip"


def test_slugify_caps_length_at_max_chars() -> None:
    """Very long inputs are truncated to exactly max_chars (default 20).
    Catches a regression where a missing truncate step lets a 1 KB prompt
    produce a 1 KB filename that exceeds the 255-byte FS limit.
    """
    out = slugify("a" * 1000)
    assert len(out) == 20
    assert out[-1] not in ("-", ".")


def test_slugify_drops_accents_via_ascii_encode() -> None:
    """Latin-1 accents survive NFC normalization but get dropped by the
    ASCII encode.  Catches a regression where bytes-style encoding leaks
    through and produces shell-unsafe filenames.
    """
    assert slugify("café résumé") == "caf-rsum"


def test_slugify_pure_whitespace_or_dashes_falls_back() -> None:
    """Whitespace-only or dash-only input strips to empty → "clip".
    Catches a regression where the strip step runs before the collapse
    step and leaves a stray "-".
    """
    assert slugify("  ---  ") == "clip"


def test_slugify_replaces_forbidden_chars_with_dash() -> None:
    """Filesystem-forbidden chars (NUL, /) are replaced with "-".  Catches
    a regression where a forbidden char survives and the sink raises
    OSError on write.
    """
    assert slugify("foo/bar\x00baz") == "foo-bar-baz"


def test_slugify_collapses_runs_of_dashes() -> None:
    """Repeated dashes in the input → single dash in the output.  Catches
    a regression where the collapse step is forgotten and filenames carry
    "Waves---crashing" eyesores.
    """
    assert slugify("multi---dashes") == "multi-dashes"


def test_slugify_strips_leading_dot() -> None:
    """A leading "." is stripped to avoid creating a hidden file.  Catches
    a regression where ".hidden" produces ".hidden.mp4" (hidden on Linux).
    """
    assert slugify(".hidden") == "hidden"


def test_slugify_no_trailing_dash_after_truncation() -> None:
    """When truncation lands on a dash, the dash is stripped post-cut.
    Catches a regression where the post-truncate strip step is skipped
    and filenames end "Waves-crashing-on-ba-.mp4".
    """
    # "Waves crashing on ba!" → "Waves-crashing-on-ba-" (21 chars before truncate)
    assert slugify("Waves crashing on ba!") == "Waves-crashing-on-ba"


def test_output_publish_error_subclasses_runtime_error() -> None:
    """OutputPublishError is a RuntimeError so callers can catch the
    broad family.  Catches a regression where the exception base class
    changes silently and a downstream `except RuntimeError` stops catching.
    """
    e = OutputPublishError("disk full")
    assert isinstance(e, RuntimeError)
    assert str(e) == "disk full"


class _FakeSink:
    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
    ) -> str:
        return "/fake"


def test_register_and_get_sink_roundtrip() -> None:
    """Registering a sink factory makes it retrievable by name; unknown
    names raise UnknownAdapter (matches Splitter/Store registry behavior).
    Catches a regression where the registry silently swallows lookup
    misses and returns None.
    """
    register_sink("__test_sink", _FakeSink)
    assert isinstance(get_sink("__test_sink")(), _FakeSink)
    with pytest.raises(UnknownAdapter):
        get_sink("__does_not_exist")
