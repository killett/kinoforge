"""Tests for download_one(opaque_name=True) path."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kinoforge.core.downloader import download_one
from kinoforge.core.interfaces import Artifact
from kinoforge.core.redaction import RedactionRegistry
from tests.conftest import HttpServerInfo

SAMPLE_DATA = b"this is some sample model bytes payload"


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _disabled_aria() -> str | None:
    return None


_DISABLED_ARIA: Callable[[], str | None] = _disabled_aria


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_opaque_name_writes_sha_filename(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """opaque_name=True names the file ``<sha256>.bin`` instead of the
    Artifact.filename.

    Would-fail-bug: writing the prompt-derived filename onto disk would
    leak the LoRA reference into ``ls`` output / `find` / OS-level audit
    log on every download.
    """
    http_server.serve_bytes("my_secret_lora_v3.safetensors", SAMPLE_DATA)
    artifact = Artifact(
        filename="my_secret_lora_v3.safetensors",
        url=f"{http_server.base_url}/my_secret_lora_v3.safetensors",
        sha256=_sha256(SAMPLE_DATA),
    )
    result = download_one(
        artifact, tmp_path, which_aria2=_DISABLED_ARIA, opaque_name=True
    )
    expected = tmp_path / f"{_sha256(SAMPLE_DATA)}.bin"
    assert expected.exists()
    assert not (tmp_path / "my_secret_lora_v3.safetensors").exists()
    assert result.uri == str(expected)


def test_opaque_name_requires_sha(http_server: HttpServerInfo, tmp_path: Path) -> None:
    """opaque_name=True with no sha256 raises ValueError before any network."""
    artifact = Artifact(
        filename="anything.safetensors",
        url=f"{http_server.base_url}/anything.safetensors",
        sha256=None,
    )
    with pytest.raises(ValueError, match="sha256"):
        download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA, opaque_name=True)
    # No file written.
    assert not any(tmp_path.iterdir())


def test_opaque_name_registers_filename_with_registry(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """Artifact.filename is registered with the RedactionRegistry under
    kind=lora:filename before the download begins."""
    http_server.serve_bytes("my_secret_lora_v3.safetensors", SAMPLE_DATA)
    artifact = Artifact(
        filename="my_secret_lora_v3.safetensors",
        url=f"{http_server.base_url}/my_secret_lora_v3.safetensors",
        sha256=_sha256(SAMPLE_DATA),
    )
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA, opaque_name=True)
    out = RedactionRegistry.instance().redact(
        "downloading my_secret_lora_v3.safetensors"
    )
    assert "my_secret_lora_v3" not in out
    assert "<lora:filename:" in out


def test_part_file_uses_opaque_shape(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """The resume .part filename derives from the sha shape too — the
    original filename never lands on disk even mid-download.

    Would-fail-bug: a .part file named after Artifact.filename would leak
    via crash dumps or ls of the dest dir before the atomic-rename.
    """
    http_server.serve_bytes("my_secret_lora_v3.safetensors", SAMPLE_DATA)
    artifact = Artifact(
        filename="my_secret_lora_v3.safetensors",
        url=f"{http_server.base_url}/my_secret_lora_v3.safetensors",
        sha256=_sha256(SAMPLE_DATA),
    )
    sha = _sha256(SAMPLE_DATA)
    # Pre-seed a half-byte .part so the resume path engages.
    pre_part = tmp_path / f"{sha}.bin.part"
    pre_part.parent.mkdir(parents=True, exist_ok=True)
    pre_part.write_bytes(SAMPLE_DATA[:5])
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA, opaque_name=True)
    # After atomic promote the .part is gone — but the original-filename
    # .part path must never have existed.
    assert not (tmp_path / "my_secret_lora_v3.safetensors.part").exists()
    assert (tmp_path / f"{sha}.bin").exists()


def test_default_opaque_name_false_preserves_behavior(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """Existing callers (no opaque_name kwarg) keep the existing behavior."""
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )
    download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)  # default
    assert (tmp_path / "model.bin").exists()
    # Registry untouched on the default path.
    assert not RedactionRegistry.instance().is_active
