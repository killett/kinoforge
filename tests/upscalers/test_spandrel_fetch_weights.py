"""Tests for the spandrel _fetch_weights CLI module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kinoforge.core.interfaces import Artifact


def test_argparse_rejects_missing_url(tmp_path: Path) -> None:
    # Bug caught: --url accidentally given a default value instead of
    # required=True; misconfigured cfgs silently fetch the wrong file.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kinoforge.upscalers.spandrel._fetch_weights",
            "--dest",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "url" in proc.stderr.lower()


def test_argparse_rejects_missing_dest() -> None:
    # Bug caught: --dest defaults to cwd, weights land in the wrong dir
    # at provision time on a pod with surprising cwd.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kinoforge.upscalers.spandrel._fetch_weights",
            "--url",
            "hf:fake/file.pth",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "dest" in proc.stderr.lower()


def test_dispatch_to_source_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: the CLI bypasses the existing resolver and writes a
    # bespoke urllib download path that drops auth headers for HF, etc.
    # Asserts the resolver chain is the seam.
    from kinoforge.upscalers.spandrel import _fetch_weights

    dummy = tmp_path / "src" / "model.pth"
    dummy.parent.mkdir(parents=True)
    payload = b"dummy weights"
    dummy.write_bytes(payload)

    captured_urls: list[str] = []

    def fake_resolve(url: str) -> list[Artifact]:
        captured_urls.append(url)
        return [
            Artifact(
                url=f"file://{dummy}",
                filename="model.pth",
                size=len(payload),
                sha256=None,
                headers={},
            )
        ]

    monkeypatch.setattr(_fetch_weights, "_resolve_source", fake_resolve)

    dest_dir = tmp_path / "dest"
    rc = _fetch_weights.main(
        ["--url", "hf:fake/repo/model.pth", "--dest", str(dest_dir)]
    )
    assert rc == 0
    assert captured_urls == ["hf:fake/repo/model.pth"]
    assert (dest_dir / "model.pth").exists()
    assert (dest_dir / "model.pth").read_bytes() == payload


def test_resolver_failure_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: a malformed --url (unknown scheme) is silently
    # accepted, exit 0, weights file absent — pod boot proceeds and
    # crashes at first /upscale.
    from kinoforge.core.errors import UnknownAdapter
    from kinoforge.upscalers.spandrel import _fetch_weights

    def bad_resolve(url: str) -> list[Artifact]:
        raise UnknownAdapter(f"no source handles: {url}")

    monkeypatch.setattr(_fetch_weights, "_resolve_source", bad_resolve)
    rc = _fetch_weights.main(
        ["--url", "junk:foo/bar.pth", "--dest", str(tmp_path / "dest")]
    )
    assert rc != 0
