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
from pathlib import Path

import pytest

from kinoforge.core.downloader import download_all, download_one
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import Artifact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SAMPLE_DATA = b"Hello, kinoforge downloader!\n" * 1000  # ~29 KiB


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
    result = download_one(artifact, tmp_path)
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
    download_one(artifact, tmp_path)
    log_after_first = len(http_server.request_log)
    assert log_after_first >= 1, "expected at least one request on first call"

    # Second call — must NOT hit the server.
    download_one(artifact, tmp_path)
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

    download_one(artifact, tmp_path)

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
        download_one(artifact, tmp_path)

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
        download_one(artifact, tmp_path)

    assert not part_file.exists(), ".part should be deleted after sha mismatch"
    assert not dest_file.exists(), "dest file should not exist after mismatch"

    # Second call: no .part, downloads from scratch → correct file.
    download_one(artifact, tmp_path)
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

    results = download_all(artifacts, tmp_path, max_workers=4)

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
    result = download_one(artifact, tmp_path)
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
    download_one(artifact, tmp_path)
    count_after_first = len(http_server.request_log)

    download_one(artifact, tmp_path)
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
