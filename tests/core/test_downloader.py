"""Tests for the resumable, checksum-verifying downloader (Task 6).

Covers all 6 acceptance criteria:
1. download_one creates dest/<filename> with correct bytes.
2. Second call with matching sha256 makes ZERO HTTP requests.
3. Pre-existing .part file triggers Range resume; result matches; .part gone.
4. sha256 mismatch on completed download raises KinoforgeError; no dest file.
5. Corrupt .part followed by download_one yields a correct file (retry from scratch).
6. download_all fetches concurrently; returns Artifacts with uri set.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from kinoforge.core.downloader import RunAriaCallable, download_all, download_one
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import Artifact
from tests.conftest import HttpServerInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SAMPLE_DATA = b"Hello, kinoforge downloader!\n" * 1000  # ~29 KiB


# ---------------------------------------------------------------------------
# aria2c test helpers (T2-T4)
# ---------------------------------------------------------------------------


def _disabled_aria() -> str | None:
    """Return None to force the stdlib transport branch in tests."""
    return None


_DISABLED_ARIA: Callable[[], str | None] = _disabled_aria


def _make_aria_stub(bytes_to_write: bytes) -> RunAriaCallable:
    """Build a run_aria2 stub that writes `bytes_to_write` to part_path."""

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        part_path.write_bytes(bytes_to_write)

    return stub


def _failing_aria(exc_msg: str = "boom") -> RunAriaCallable:
    """Build a run_aria2 stub that always raises KinoforgeError."""

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        raise KinoforgeError(exc_msg)

    return stub


def _aria_writing_garbage_then_failing() -> RunAriaCallable:
    """Stub that writes garbage to part_path AND raises.

    Used by T3's A4 to prove the fallback path unlinks the .part before
    retrying via stdlib (else the stdlib branch would Range-resume off
    the garbage prefix).
    """

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        part_path.write_bytes(b"garbage prefix bytes")
        raise KinoforgeError("aria2c wrote then failed")

    return stub


# ---------------------------------------------------------------------------
# AC 1: basic download
# ---------------------------------------------------------------------------


def test_download_one_creates_file(http_server, tmp_path):
    """AC1: download_one writes the served bytes to dest/filename."""
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )
    result = download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    dest_file = tmp_path / "model.bin"
    assert dest_file.exists(), "dest file not created"
    assert dest_file.read_bytes() == SAMPLE_DATA, "file content mismatch"
    assert result.uri == str(dest_file), "returned Artifact.uri should be abs path"


# ---------------------------------------------------------------------------
# AC 2: idempotent — zero HTTP on second call
# ---------------------------------------------------------------------------


def test_download_one_skips_when_complete(http_server, tmp_path):
    """AC2: second call with sha256 match makes zero HTTP requests."""
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )
    # First call — downloads normally.
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    log_after_first = len(http_server.request_log)
    assert log_after_first >= 1, "expected at least one request on first call"

    # Second call — must NOT hit the server.
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    assert len(http_server.request_log) == log_after_first, (
        "second call made unexpected HTTP requests"
    )


# ---------------------------------------------------------------------------
# AC 3: resume from .part via Range
# ---------------------------------------------------------------------------


def test_download_one_resumes_from_part(http_server, tmp_path):
    """AC3: pre-existing .part triggers Range request; file matches; .part cleaned up."""
    http_server.serve_bytes("video.mp4", SAMPLE_DATA)
    artifact = Artifact(
        filename="video.mp4",
        url=f"{http_server.base_url}/video.mp4",
        sha256=_sha256(SAMPLE_DATA),
    )
    dest_file = tmp_path / "video.mp4"
    part_file = Path(str(dest_file) + ".part")

    # Write the first 1000 bytes as a pre-existing .part.
    n = 1000
    part_file.write_bytes(SAMPLE_DATA[:n])

    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)

    # The result file matches the full data.
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA

    # The .part file is gone.
    assert not part_file.exists(), ".part file should be removed after success"

    # At least one request had the Range header.
    range_requests = [
        entry for entry in http_server.request_log if entry[2].startswith("bytes=")
    ]
    assert range_requests, "expected at least one Range request during resume"
    assert range_requests[0][2] == f"bytes={n}-", (
        f"expected Range: bytes={n}-, got {range_requests[0][2]}"
    )


# ---------------------------------------------------------------------------
# AC 4: sha256 mismatch raises KinoforgeError; no dest file
# ---------------------------------------------------------------------------


def test_download_one_raises_on_sha_mismatch(http_server, tmp_path):
    """AC4: sha256 mismatch on completed download raises KinoforgeError; no dest file."""
    http_server.serve_bytes("weights.pt", SAMPLE_DATA)
    wrong_sha = "a" * 64  # definitely wrong
    artifact = Artifact(
        filename="weights.pt",
        url=f"{http_server.base_url}/weights.pt",
        sha256=wrong_sha,
    )
    dest_file = tmp_path / "weights.pt"

    with pytest.raises(KinoforgeError, match="sha256"):
        download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)

    assert not dest_file.exists(), "dest file must not exist after sha mismatch"


# ---------------------------------------------------------------------------
# AC 5: corrupt .part → correct file after retry
# ---------------------------------------------------------------------------


def test_download_one_handles_corrupt_part(http_server, tmp_path):
    """AC5: corrupt .part (first byte wrong) leads to correct file.

    Strategy: after appending, sha256 verify fails → delete .part, raise
    KinoforgeError.  Caller (or the test) re-runs; second call has no .part
    and downloads cleanly.
    """
    http_server.serve_bytes("lora.safetensors", SAMPLE_DATA)
    artifact = Artifact(
        filename="lora.safetensors",
        url=f"{http_server.base_url}/lora.safetensors",
        sha256=_sha256(SAMPLE_DATA),
    )
    dest_file = tmp_path / "lora.safetensors"
    part_file = Path(str(dest_file) + ".part")

    # Write a corrupt .part: first byte is wrong, rest is real data.
    corrupt = bytes([SAMPLE_DATA[0] ^ 0xFF]) + SAMPLE_DATA[1:]
    part_file.write_bytes(corrupt)

    # First call: assembles a bad file, detects sha mismatch, cleans up .part.
    with pytest.raises(KinoforgeError, match="sha256"):
        download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)

    assert not part_file.exists(), ".part should be deleted after sha mismatch"
    assert not dest_file.exists(), "dest file should not exist after mismatch"

    # Second call: no .part, downloads from scratch → correct file.
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    assert dest_file.read_bytes() == SAMPLE_DATA, "file incorrect after retry"


# ---------------------------------------------------------------------------
# AC 6: download_all — concurrent, returns Artifacts with uri set
# ---------------------------------------------------------------------------


def test_download_all_concurrent(http_server, tmp_path):
    """AC6: download_all fetches multiple artifacts concurrently; uri set on each."""
    names = [f"file_{i:02d}.bin" for i in range(6)]
    payloads = {name: os.urandom(4096) for name in names}
    for name, data in payloads.items():
        http_server.serve_bytes(name, data)

    artifacts = [
        Artifact(
            filename=name,
            url=f"{http_server.base_url}/{name}",
            sha256=_sha256(data),
        )
        for name, data in payloads.items()
    ]

    results = download_all(
        artifacts,
        tmp_path,
        max_workers=4,
        which_aria2=_DISABLED_ARIA,
    )

    assert len(results) == len(artifacts), "should return one result per input"
    for i, (name, data) in enumerate(payloads.items()):
        result = results[i]
        dest_file = tmp_path / name
        assert dest_file.exists(), f"{name} not found"
        assert dest_file.read_bytes() == data, f"{name} content mismatch"
        assert result.uri == str(dest_file), f"{name} uri not set correctly"


# ---------------------------------------------------------------------------
# AC 6b: download_one without sha256 — still works (no verification)
# ---------------------------------------------------------------------------


def test_download_one_no_sha256(http_server, tmp_path):
    """download_one succeeds and does not verify sha256 when artifact.sha256 is None."""
    http_server.serve_bytes("raw.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="raw.bin",
        url=f"{http_server.base_url}/raw.bin",
        sha256=None,
    )
    result = download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    dest_file = tmp_path / "raw.bin"
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert result.uri == str(dest_file)


# ---------------------------------------------------------------------------
# AC 6c: second call without sha256 is also idempotent (size-skip)
# ---------------------------------------------------------------------------


def test_download_one_no_sha256_skips_existing(http_server, tmp_path):
    """Without sha256, a second call skips (filename-based) without HTTP traffic."""
    http_server.serve_bytes("raw.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="raw.bin",
        url=f"{http_server.base_url}/raw.bin",
        sha256=None,
    )
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    count_after_first = len(http_server.request_log)

    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
    assert len(http_server.request_log) == count_after_first, (
        "second call without sha256 should not hit server"
    )


# ---------------------------------------------------------------------------
# Task 1: aria2c seams importable
# ---------------------------------------------------------------------------


def test_aria2c_seams_importable():
    """T1: the new aria2c module-level seams are importable.

    Bug this catches: a future refactor that renames or removes one of the
    seams without updating downstream callers (the seam contract is part of
    the public-ish API of this module).
    """
    from kinoforge.core.downloader import (
        RunAriaCallable,
        WhichCallable,
        _shutil_which_aria2,
        _subprocess_run_aria2,
    )

    # Trivial use to silence unused-import warnings and prove the names bind.
    assert callable(_shutil_which_aria2)
    assert callable(_subprocess_run_aria2)
    # Type aliases must resolve to collections.abc.Callable at runtime so a
    # regression that reassigns one to, say, `int` is caught.
    import collections.abc

    assert WhichCallable.__origin__ is collections.abc.Callable  # type: ignore[attr-defined]
    assert RunAriaCallable.__origin__ is collections.abc.Callable  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# T2 A1: aria2c used when detected; no stdlib fetch
# ---------------------------------------------------------------------------


def test_aria2c_used_when_detected(http_server, tmp_path):
    """T2 A1: aria2c writes the bytes; stdlib fetch is never called.

    Bug this catches: the aria2c branch silently falls through to the
    stdlib path, leaving the fast-path code dead and the test suite
    blind to performance regression.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    stdlib_calls: list[tuple[str, dict[str, str]]] = []

    def stdlib_spy(
        url: str, headers: dict[str, str]
    ) -> tuple[int, bytes, dict[str, str]]:
        stdlib_calls.append((url, dict(headers)))
        raise AssertionError("stdlib fetch must not be called on aria2c success path")

    result = download_one(
        artifact,
        tmp_path,
        fetch=stdlib_spy,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=_make_aria_stub(SAMPLE_DATA),
    )

    dest_file = tmp_path / "model.bin"
    part_file = Path(str(dest_file) + ".part")
    assert dest_file.exists(), "dest file not created"
    assert dest_file.read_bytes() == SAMPLE_DATA, "file content mismatch"
    assert not part_file.exists(), ".part not promoted"
    assert result.uri == str(dest_file)
    assert stdlib_calls == [], "stdlib fetch was called on aria2c success path"


# ---------------------------------------------------------------------------
# T2 A2: aria2c skipped when binary not detected
# ---------------------------------------------------------------------------


def test_aria2c_skipped_when_not_detected(http_server, tmp_path):
    """T2 A2: which_aria2 returns None -> run_aria2 is never invoked.

    Bug this catches: auto-detect breaks (e.g. shutil.which call removed)
    and operators without aria2c hit a regression.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    aria_calls: list[tuple[str, Path, dict[str, str]]] = []

    def aria_spy(url: str, part_path: Path, headers: dict[str, str]) -> None:
        aria_calls.append((url, part_path, dict(headers)))

    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: None,
        run_aria2=aria_spy,
    )

    dest_file = tmp_path / "model.bin"
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert aria_calls == [], "aria2c was invoked despite which_aria2 returning None"
    # Stdlib path served the file via the loopback http_server.
    assert len(http_server.request_log) >= 1


# ---------------------------------------------------------------------------
# T2 A5: aria2c "succeeds" but sha256 mismatches
# ---------------------------------------------------------------------------


def test_aria2c_sha256_mismatch_raises(http_server, tmp_path):
    """T2 A5: aria2c wrote bytes; sha256 verify fails; .part deleted.

    Bug this catches: aria2c "succeeds" with corrupt bytes (CDN edge bug,
    truncated response) and wrong weights end up in the cache.
    """
    http_server.serve_bytes("weights.pt", SAMPLE_DATA)
    artifact = Artifact(
        filename="weights.pt",
        url=f"{http_server.base_url}/weights.pt",
        sha256=_sha256(SAMPLE_DATA),  # expected
    )

    wrong_bytes = SAMPLE_DATA + b"trailing garbage"  # sha will mismatch

    with pytest.raises(KinoforgeError, match="sha256"):
        download_one(
            artifact,
            tmp_path,
            which_aria2=lambda: "/usr/bin/aria2c",
            run_aria2=_make_aria_stub(wrong_bytes),
        )

    dest_file = tmp_path / "weights.pt"
    part_file = Path(str(dest_file) + ".part")
    assert not dest_file.exists(), "target file must not exist after sha mismatch"
    assert not part_file.exists(), ".part must be cleaned up after sha mismatch"


# ---------------------------------------------------------------------------
# T2 A6: aria2c + sha256 None path
# ---------------------------------------------------------------------------


def test_aria2c_no_sha256_succeeds(http_server, tmp_path):
    """T2 A6: when sha256 is None, aria2c bytes are accepted as-is; second
    call is a zero-HTTP skip via the filename-based skip-path.

    Bug this catches: the sha256-verify code accidentally runs even when
    sha is None (e.g. an over-eager refactor), corrupting the no-sha
    contract.
    """
    http_server.serve_bytes("raw.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="raw.bin",
        url=f"{http_server.base_url}/raw.bin",
        sha256=None,
    )

    aria_call_count = [0]

    def counting_aria(url: str, part_path: Path, headers: dict[str, str]) -> None:
        aria_call_count[0] += 1
        part_path.write_bytes(SAMPLE_DATA)

    # First call: aria2c writes, no verify, atomic rename.
    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=counting_aria,
    )
    dest_file = tmp_path / "raw.bin"
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert aria_call_count[0] == 1

    # Second call: skip-path; aria2c MUST NOT be called.
    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=counting_aria,
    )
    assert aria_call_count[0] == 1, "second call hit aria2c instead of skip-path"


# ---------------------------------------------------------------------------
# T3 A3: aria2c failure falls back to stdlib + WARNING log
# ---------------------------------------------------------------------------


def test_aria2c_failure_falls_back_to_stdlib(http_server, tmp_path, caplog):
    """T3 A3: aria2c raises -> WARNING logged, stdlib fetch produces the file.

    Bug this catches: subprocess failure becomes a hard error and operators
    lose their existing single-connection download path.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    with caplog.at_level("WARNING", logger="kinoforge.core.downloader"):
        result = download_one(
            artifact,
            tmp_path,
            which_aria2=lambda: "/usr/bin/aria2c",
            run_aria2=_failing_aria("simulated aria2c exit 22"),
        )

    dest_file = tmp_path / "model.bin"
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert result.uri == str(dest_file)

    # WARNING log records contain the contract substrings.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "expected a WARNING log on aria2c fallback"
    msg = warnings[0].getMessage()
    assert "aria2c" in msg, f"WARNING message missing 'aria2c': {msg!r}"
    assert "fallback" in msg.lower(), f"WARNING message missing 'fallback': {msg!r}"


# ---------------------------------------------------------------------------
# T3 A4: aria2c failure unlinks poisoned .part before fallback
# ---------------------------------------------------------------------------


def test_aria2c_failure_unlinks_part_before_fallback(http_server, tmp_path):
    """T3 A4: a failed aria2c that wrote garbage bytes must NOT cause the
    stdlib fallback to Range-resume off the garbage prefix.

    Bug this catches: the fallback path inherits the poisoned .part,
    producing a corrupt assembled file that fails sha256 verify on every
    retry forever.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    # Snapshot the loopback server's request log so we can detect any
    # Range header sent by the fallback.
    pre_count = len(http_server.request_log)

    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=_aria_writing_garbage_then_failing(),
    )

    dest_file = tmp_path / "model.bin"
    part_file = Path(str(dest_file) + ".part")
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA, (
        "fallback produced corrupt bytes — likely Range-resumed off the "
        "garbage prefix from the failed aria2c run"
    )
    assert not part_file.exists()

    # Fallback request MUST NOT carry a Range header (no resume off garbage).
    new_requests = http_server.request_log[pre_count:]
    range_requests = [entry for entry in new_requests if entry[2].startswith("bytes=")]
    assert range_requests == [], (
        "fallback sent a Range header — poisoned .part was not unlinked"
    )


# ---------------------------------------------------------------------------
# T4 A7: download_all forwards the seams to every download_one call
# ---------------------------------------------------------------------------


def test_download_all_uses_aria2c_per_artifact(http_server, tmp_path):
    """T4 A7: download_all forwards which_aria2 + run_aria2 so every
    artifact takes the aria2c path; stdlib fetch is never called.

    Bug this catches: download_all forgets to forward the seams kwargs
    and the parallel-files path silently degrades to stdlib transport.
    """
    names = [f"file_{i:02d}.bin" for i in range(3)]
    payloads = {name: os.urandom(4096) for name in names}
    for name, data in payloads.items():
        http_server.serve_bytes(name, data)

    artifacts = [
        Artifact(
            filename=name,
            url=f"{http_server.base_url}/{name}",
            sha256=_sha256(data),
        )
        for name, data in payloads.items()
    ]

    aria_calls: list[tuple[str, Path, dict[str, str]]] = []

    def fan_aria(url: str, part_path: Path, headers: dict[str, str]) -> None:
        # Pick the payload by URL suffix — matches what aria2c-on-real-CDN
        # would do.
        for name, data in payloads.items():
            if url.endswith(name):
                aria_calls.append((url, part_path, dict(headers)))
                part_path.write_bytes(data)
                return
        raise AssertionError(f"unexpected URL in aria2c stub: {url!r}")

    stdlib_calls: list[tuple[str, dict[str, str]]] = []

    def stdlib_spy(
        url: str, headers: dict[str, str]
    ) -> tuple[int, bytes, dict[str, str]]:
        stdlib_calls.append((url, dict(headers)))
        raise AssertionError("stdlib fetch must not be called in A7")

    results = download_all(
        artifacts,
        tmp_path,
        max_workers=4,
        fetch=stdlib_spy,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=fan_aria,
    )

    assert len(results) == 3
    assert len(aria_calls) == 3, f"expected 3 aria2c calls, got {len(aria_calls)}"
    assert stdlib_calls == [], "stdlib fetch was called despite aria2c success"
    for i, (name, data) in enumerate(payloads.items()):
        dest_file = tmp_path / name
        assert dest_file.read_bytes() == data
        assert results[i].uri == str(dest_file)


def test_download_one_creates_parent_dirs_for_subpath_filename(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """Artifact.filename with `/` triggers parent-dir mkdir before write.

    Bug this catches: writing to dest/sub/foo.bin without first mkdir-ing
    dest/sub fails with FileNotFoundError. The bare-repo listing feature
    emits subpath filenames; this AC locks the downloader behaviour.

    Forces the stdlib branch (``which_aria2=_DISABLED_ARIA``) because the
    aria2c fast-path creates missing parent dirs natively, masking the
    bug. Without the parent.mkdir in ``download_one``, the stdlib branch
    raises ``FileNotFoundError`` on ``part_path.open("wb")``.
    """
    payload = b"hello-subdir"
    http_server.serve_bytes("foo.bin", payload)
    art = Artifact(
        filename="sub/foo.bin",
        url=f"{http_server.base_url}/foo.bin",
    )

    result = download_one(art, tmp_path, which_aria2=_DISABLED_ARIA)

    target = tmp_path / "sub" / "foo.bin"
    assert target.is_file()
    assert target.read_bytes() == payload
    assert result.uri == str(target)
