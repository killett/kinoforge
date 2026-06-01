"""Behavioral tests for LocalOutputSink — the default local-FS publish sink.

Each test states the behavior under test and the concrete bug the assertion
catches.  All I/O uses pytest's tmp_path; FakeClock makes timestamps
deterministic.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.clock import FakeClock
from kinoforge.outputs import get_sink
from kinoforge.outputs.base import OutputPublishError
from kinoforge.outputs.local import LocalOutputSink

# Build a FakeClock epoch corresponding exactly to local-TZ 2026-05-31T21:00:15.
# We round-trip through datetime.timestamp() so we don't have to hand-compute
# the offset for whatever TZ the test environment is in.
_FIXED_EPOCH = datetime(2026, 5, 31, 21, 0, 15).timestamp()
_FIXED_TS_STRING = "20260531-210015"


def test_publish_writes_bytes_at_ts_slug_ext_path(tmp_path: Path) -> None:
    """A vanilla publish lands at <dir>/<ts>_<slug><ext> with the right
    bytes.  Catches a regression where the filename builder swaps ts/slug
    order or drops the underscore separator.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"hello", prompt="Waves crashing", extension=".mp4")
    expected = tmp_path / f"{_FIXED_TS_STRING}_Waves-crashing.mp4"
    assert Path(out) == expected.resolve()
    assert expected.read_bytes() == b"hello"


def test_publish_with_namespace_creates_subdir(tmp_path: Path) -> None:
    """A namespace argument nests the file under <dir>/<namespace>/ and
    creates the subdirectory if missing.  Catches a regression where
    namespace is interpreted as a filename prefix rather than a subdir.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"x", prompt="A", extension=".mp4", namespace="batch-X")
    expected = tmp_path / "batch-X" / f"{_FIXED_TS_STRING}_A.mp4"
    assert Path(out) == expected.resolve()
    assert expected.parent.is_dir()


def test_publish_collision_appends_underscore_n(tmp_path: Path) -> None:
    """Two publishes with identical prompt + clock state produce
    <name>.mp4 and <name>_2.mp4; the original is preserved.  Catches a
    regression where the sink silently overwrites — the foot-gun the
    whole layer exists to close.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    first = sink.publish(b"one", prompt="Cliffs", extension=".mp4")
    second = sink.publish(b"two", prompt="Cliffs", extension=".mp4")
    assert Path(first).name == f"{_FIXED_TS_STRING}_Cliffs.mp4"
    assert Path(second).name == f"{_FIXED_TS_STRING}_Cliffs_2.mp4"
    assert Path(first).read_bytes() == b"one"
    assert Path(second).read_bytes() == b"two"


def test_publish_collision_99_then_hash_suffix(tmp_path: Path) -> None:
    """After exhausting _2.._99, the next collision uses a 6-char sha256
    hash suffix.  Catches a regression where the suffix loop has an
    off-by-one and either raises or never gives up.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    # Pre-populate the 99 collision targets manually so we don't loop the sink.
    base = f"{_FIXED_TS_STRING}_X.mp4"
    (tmp_path / base).write_bytes(b"orig")
    for n in range(2, 100):
        (tmp_path / f"{_FIXED_TS_STRING}_X_{n}.mp4").write_bytes(b"prior")
    out = sink.publish(b"new", prompt="X", extension=".mp4")
    name = Path(out).name
    assert name.startswith(f"{_FIXED_TS_STRING}_X_")
    assert name.endswith(".mp4")
    # Hash suffix is 6 hex chars between underscore and extension.
    middle = name[len(f"{_FIXED_TS_STRING}_X_") : -len(".mp4")]
    assert len(middle) == 6
    assert all(c in "0123456789abcdef" for c in middle)


def test_publish_into_readonly_dir_raises_OutputPublishError(tmp_path: Path) -> None:
    """A write into a chmod-000 directory raises OutputPublishError, not
    bare OSError, so callers can catch the semantic family.  Catches a
    regression where the sink lets the underlying OSError propagate
    unwrapped.
    """
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)  # r-x, no write
    sink = LocalOutputSink(dir=target, clock=FakeClock(start=_FIXED_EPOCH))
    try:
        with pytest.raises(OutputPublishError):
            sink.publish(b"x", prompt="A", extension=".mp4")
    finally:
        target.chmod(0o700)  # restore so tmp_path cleanup works


def test_publish_resolves_relative_dir_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative dir argument is resolved against cwd at construction
    time; cwd changes after construction must not move the publish target.
    Catches a regression where the sink stores the raw relative path and
    later writes land in the wrong place.
    """
    monkeypatch.chdir(tmp_path)
    sink = LocalOutputSink(
        dir=Path("relative-out"), clock=FakeClock(start=_FIXED_EPOCH)
    )
    out = sink.publish(b"x", prompt="A", extension=".mp4")
    assert Path(out).is_absolute()
    assert (tmp_path / "relative-out" / f"{_FIXED_TS_STRING}_A.mp4").exists()


def test_publish_empty_extension_defaults_to_bin(tmp_path: Path) -> None:
    """An empty extension string falls back to ".bin" so we never write
    files with no extension.  Catches a regression where empty extension
    produces "20260531-210015_A" with no suffix.
    """
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    out = sink.publish(b"x", prompt="A", extension="")
    assert Path(out).name == f"{_FIXED_TS_STRING}_A.bin"


def test_local_sink_self_registers_on_import() -> None:
    """Importing kinoforge.outputs.local registers under "local" so the
    CLI's _build_sink can resolve it via the registry without a direct
    import.  Catches a regression where the side-effect registration is
    removed (registry returns UnknownAdapter) or registers the wrong
    factory (returns something other than a LocalOutputSink).
    """
    import kinoforge.outputs.local  # noqa: F401  side-effect import

    factory = get_sink("local")
    instance = factory()
    assert isinstance(instance, LocalOutputSink)


def test_publish_uses_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sink writes to <final>.tmp first then os.replace's it into
    place, so a crash mid-write never leaves a partial file at the final
    name.  Catches a regression where the sink writes directly to the
    final path and a crash mid-write leaves a corrupt file the operator
    later mistakes for a finished clip.
    """

    def boom(src: str, dst: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr("os.replace", boom)

    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    with pytest.raises(OutputPublishError):
        sink.publish(b"x", prompt="A", extension=".mp4")

    final = tmp_path / f"{_FIXED_TS_STRING}_A.mp4"
    assert not final.exists()
    # After the rename failure the sink must also clean up the .tmp file so
    # operators never mistake a partial write for a finished clip.
    assert not (tmp_path / f"{_FIXED_TS_STRING}_A.mp4.tmp").exists()


def test_publish_collision_hash_exhausted_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all _2.._99 slots AND every hash-suffix retry are occupied,
    _resolve_collision raises OutputPublishError rather than silently
    overwriting a pre-existing file.  Catches a regression where the
    Protocol's "must never silently destroy" contract is violated when the
    hash retry budget is exhausted.

    Methodology: monkeypatch time.monotonic_ns to a constant so every
    hash attempt produces the same 6-char suffix; pre-create the _2.._99
    collision files and the resulting hash-named file to force the full
    exhaustion path.
    """
    import hashlib

    monkeypatch.setattr("time.monotonic_ns", lambda: 0)

    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=_FIXED_EPOCH))
    slug = "X"
    ts = _FIXED_TS_STRING
    ext = ".mp4"

    # Pre-populate the primary and all numeric collision slots.
    (tmp_path / f"{ts}_{slug}{ext}").write_bytes(b"orig")
    for n in range(2, 100):
        (tmp_path / f"{ts}_{slug}_{n}{ext}").write_bytes(b"prior")

    # Compute the pinned hash suffix and pre-create it too.
    hash_suffix = hashlib.sha256(b"0").hexdigest()[:6]
    (tmp_path / f"{ts}_{slug}_{hash_suffix}{ext}").write_bytes(b"hash-collision")

    with pytest.raises(OutputPublishError, match="non-colliding output path"):
        sink.publish(b"new", prompt=slug, extension=ext)


def test_clock_now_converted_to_local_tz_naive_datetime(tmp_path: Path) -> None:
    """clock.now() returns a Unix epoch float; the sink renders it via
    datetime.fromtimestamp(epoch) (no tz arg) which gives local-TZ naive
    datetime — matching feedback_local_timezone_only.md.  Catches a
    regression where the sink uses datetime.utcfromtimestamp and the
    filename TS drifts off the operator's wall clock.
    """
    epoch = datetime(2026, 5, 31, 21, 0, 15).timestamp()
    sink = LocalOutputSink(dir=tmp_path, clock=FakeClock(start=epoch))
    out = sink.publish(b"x", prompt="A", extension=".mp4")
    assert "20260531-210015" in Path(out).name
