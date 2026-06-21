"""civitai.resolve wraps CivitAISource."""

from __future__ import annotations

import pytest

from tests._smoke_harness import civitai


def test_resolve_picks_safetensors_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper picks first artifact regardless of extension → png preview."""

    class _Art:
        def __init__(self, filename: str) -> None:
            self.url = f"https://x/{filename}"
            self.filename = filename
            self.headers = {"Authorization": "Bearer k"}
            self.size = 1024

    arts = [
        _Art("preview.png"),
        _Art("model.safetensors"),
        _Art("readme.txt"),
    ]

    class _Source:
        def resolve(self, *_args: object, **_kw: object) -> list[_Art]:
            return arts

    monkeypatch.setattr(civitai, "_civitai_source_factory", lambda: _Source())
    spec = civitai.resolve("civitai:1@2")
    assert spec["url"] == "https://x/model.safetensors"
    assert spec["filename"] == "model.safetensors"
    assert spec["size_hint"] == 1024
    assert spec["headers"] == {"Authorization": "Bearer k"}


def test_resolve_falls_back_to_first_when_no_safetensors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper raises when only .ckpt / .pt artifacts exist."""

    class _Art:
        def __init__(self, filename: str) -> None:
            self.url = f"https://x/{filename}"
            self.filename = filename
            self.headers: dict[str, str] = {}
            self.size = 1

    class _Source:
        def resolve(self, *_args: object, **_kw: object) -> list[_Art]:
            return [_Art("only.ckpt")]

    monkeypatch.setattr(civitai, "_civitai_source_factory", lambda: _Source())
    spec = civitai.resolve("civitai:1@2")
    assert spec["filename"] == "only.ckpt"
