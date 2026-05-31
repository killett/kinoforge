"""Tests for core/assets.py helpers."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.assets import asset_bytes, find_asset, set_by_dot_path
from kinoforge.core.errors import AssetFetchError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    Segment,
)


def _job(assets: list[ConditioningAsset]) -> GenerationJob:
    """Build a minimal job with one segment carrying ``assets``."""
    return GenerationJob(
        spec={},
        segments=[Segment(prompt="p", assets=assets)],
        params={},
    )


def _asset(role: str, uri: str = "file:///tmp/x.png") -> ConditioningAsset:
    return ConditioningAsset(
        kind="image",
        role=role,
        ref=Artifact(filename="x.png", uri=uri),
    )


def test_find_asset_returns_match() -> None:
    a = _asset("init_image")
    job = _job([_asset("first_frame"), a, _asset("last_frame")])
    # Bug catch: substring/prefix matching would return the first asset
    # whose role *contains* "init_image" (none here) or worse silently
    # match "first_frame" if comparison is broken.
    assert find_asset(job, "init_image") is a


def test_find_asset_returns_none_for_missing() -> None:
    job = _job([_asset("first_frame")])
    # Bug catch: KeyError or IndexError on missing role instead of
    # graceful None.
    assert find_asset(job, "init_image") is None


def test_find_asset_raises_on_duplicate_role() -> None:
    a1 = _asset("init_image", "file:///a.png")
    a2 = _asset("init_image", "file:///b.png")
    job = _job([a1, a2])
    # Bug catch: silent "pick first" would let a splitter bug ship
    # uncaught — caller has no way to know two distinct images were
    # meant for the same slot.
    with pytest.raises(ValidationError, match="duplicate"):
        find_asset(job, "init_image")


def test_asset_bytes_http_dispatches_to_fetcher() -> None:
    called: list[str] = []

    def fake_fetcher(url: str) -> bytes:
        called.append(url)
        return b"HTTP_PAYLOAD"

    out = asset_bytes("https://example.com/x.png", http_get_bytes=fake_fetcher)
    # Bug catch: file-path fallback for http URI would read 0 bytes from
    # a "/example.com/x.png" path that may or may not exist locally.
    assert out == b"HTTP_PAYLOAD"
    assert called == ["https://example.com/x.png"]


def test_asset_bytes_file_reads_filesystem(tmp_path: Path) -> None:
    p = tmp_path / "asset.png"
    p.write_bytes(b"LOCAL_PAYLOAD")
    uri = p.as_uri()  # file:///...

    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("http fetcher must not be called for file://")

    # Bug catch: urlparse drops the leading slash on file URIs in some
    # naive impls (path becomes "tmp/asset.png" → not found).
    assert asset_bytes(uri, http_get_bytes=fail_fetcher) == b"LOCAL_PAYLOAD"


def test_asset_bytes_unsupported_scheme_raises() -> None:
    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("must not be called for unsupported scheme")

    # Bug catch: silent fallthrough to http_get_bytes would yield an
    # opaque urllib failure 30s later instead of an early typed error.
    with pytest.raises(AssetFetchError, match="unsupported scheme"):
        asset_bytes("s3://bucket/key", http_get_bytes=fail_fetcher)


def test_asset_bytes_wraps_http_error() -> None:
    def raising_fetcher(url: str) -> bytes:
        raise urllib.error.URLError("dns fail")

    # Bug catch: raw URLError leaking to caller breaks the typed-error
    # contract used by orchestrator (matches Layer E discipline).
    with pytest.raises(AssetFetchError, match="dns fail"):
        asset_bytes("https://example.com/x.png", http_get_bytes=raising_fetcher)


def test_asset_bytes_wraps_file_not_found(tmp_path: Path) -> None:
    missing = (tmp_path / "nope.png").as_uri()

    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("must not be called for file://")

    # Bug catch: bare FileNotFoundError leaks; should be AssetFetchError.
    with pytest.raises(AssetFetchError, match="nope.png"):
        asset_bytes(missing, http_get_bytes=fail_fetcher)


def test_set_by_dot_path_simple() -> None:
    body: dict[str, Any] = {}
    set_by_dot_path(body, "key", 42)
    # Bug catch: a "single-key always treated as nested" impl would
    # create {"k": {"e": {"y": 42}}} or similar nonsense.
    assert body == {"key": 42}


def test_set_by_dot_path_nested_creates_intermediates() -> None:
    body: dict[str, Any] = {"existing": {"left": "alone"}}
    set_by_dot_path(body, "a.b.c", "deep")
    # Bug catch: missing-intermediate KeyError on write, or overwriting
    # the sibling "existing" branch.
    assert body == {
        "existing": {"left": "alone"},
        "a": {"b": {"c": "deep"}},
    }
