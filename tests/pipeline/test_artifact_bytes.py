"""Layer R T3: shared artifact_bytes helper tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.interfaces import Artifact
from kinoforge.pipeline.artifact_bytes import artifact_bytes


def test_file_uri_reads_local_path(tmp_path: Path) -> None:
    """file:// uri must resolve to local read.
    Bug guard: a regex that fails to strip file:// would 404 on a real file."""
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    a = Artifact(filename="x.bin", uri=f"file://{p}")
    assert artifact_bytes(a) == b"hello"


def test_bare_path_uri_reads_local_path(tmp_path: Path) -> None:
    """Bare local-path uri (no scheme) must also read directly.
    Bug guard: requiring file:// prefix breaks LocalArtifactStore which returns plain paths."""
    p = tmp_path / "y.bin"
    p.write_bytes(b"world")
    a = Artifact(filename="y.bin", uri=str(p))
    assert artifact_bytes(a) == b"world"


def test_http_url_calls_seam_with_headers() -> None:
    """HTTP url must call the injected seam carrying artifact.headers verbatim.
    Bug guard: dropping headers would 403 on RunPod /view and similar auth-bearing endpoints."""
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append((url, dict(headers)))
        return b"DOWNLOADED"

    a = Artifact(
        filename="z.mp4",
        url="https://example.test/z.mp4",
        headers={"Authorization": "Bearer xyz"},
    )
    assert artifact_bytes(a, fetch) == b"DOWNLOADED"
    assert calls == [("https://example.test/z.mp4", {"Authorization": "Bearer xyz"})]


def test_http_url_seam_called_once_only() -> None:
    """Bug guard: a refactor that double-resolves the URL would inflate cost
    on real fal/runpod endpoints."""
    n = 0

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        nonlocal n
        n += 1
        return b"X"

    a = Artifact(filename="z.mp4", url="https://example.test/z.mp4")
    artifact_bytes(a, fetch)
    assert n == 1


def test_synthetic_fallback_no_uri_no_url() -> None:
    """When neither path resolves, fall back to deterministic synthetic bytes.
    Bug guard: dropping this branch breaks FakeEngine-driven unit tests that
    rely on flowing some bytes through to store.put_bytes."""
    a = Artifact(filename="abc.png", meta={"k": "v"})
    out = artifact_bytes(a)
    assert b"abc.png" in out
    assert b"k" in out and b"v" in out


def test_missing_file_uri_falls_through_to_url(tmp_path: Path) -> None:
    """Stale file:// uri must not short-circuit when URL is available.
    Bug guard: a stale .uri after store cleanup must NOT block fetching from .url."""

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        return b"FROM_URL"

    a = Artifact(
        filename="x.bin",
        uri=f"file://{tmp_path}/missing.bin",
        url="https://example.test/x.bin",
    )
    assert artifact_bytes(a, fetch) == b"FROM_URL"


def test_default_seam_used_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When http_get_bytes is None, the module's _default_http_get_bytes seam is used.
    Bug guard: a regression that bypasses the default would 403 on real proxies that
    reject the stdlib Python-urllib/* User-Agent."""
    from kinoforge.pipeline import artifact_bytes as mod

    captured: list[str] = []

    def fake_default(url: str, headers: dict[str, str]) -> bytes:
        captured.append(url)
        return b"DEFAULT"

    monkeypatch.setattr(mod, "_default_http_get_bytes", fake_default)
    a = Artifact(filename="x.mp4", url="https://example.test/x.mp4")
    assert artifact_bytes(a) == b"DEFAULT"
    assert captured == ["https://example.test/x.mp4"]


def test_empty_headers_dict_passed_when_artifact_has_no_headers() -> None:
    """When artifact.headers is empty, seam is called with {} not None.
    Bug guard: a None vs {} mismatch crashes a seam that does `dict(headers)`."""
    calls: list[dict[str, str]] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(dict(headers))
        return b""

    a = Artifact(filename="x", url="https://example.test/x")
    artifact_bytes(a, fetch)
    assert calls == [{}]
