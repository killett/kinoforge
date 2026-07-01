"""FlashVSR _fetch_weights CLI: bundle selection + SHA256 verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kinoforge.core.errors import FlashVSRWeightsIncomplete
from kinoforge.upscalers.flashvsr import _fetch_weights as fw

BASE_FILES = (
    "diffusion_pytorch_model_streaming_dmd.safetensors",
    "Wan2.1_VAE.pth",
)
LONG_VIDEO_FILES = ("LQ_proj_in.ckpt", "TCDecoder.ckpt")


def _fake_bytes(name: str) -> bytes:
    """Deterministic per-file bytes for hash assertions."""
    return f"flashvsr::{name}".encode()


def _fake_sha(name: str) -> str:
    return hashlib.sha256(_fake_bytes(name)).hexdigest()


@pytest.fixture
def fake_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, dict[str, str]]:
    """Inject a manifest matching the deterministic _fake_bytes."""
    manifest = {
        name: {"sha256": _fake_sha(name)} for name in BASE_FILES + LONG_VIDEO_FILES
    }
    monkeypatch.setattr(fw, "_load_manifest", lambda: manifest)
    return manifest


def test_lite_bundle_fetches_two_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_manifest: dict[str, dict[str, str]],
) -> None:
    """RED: --include-long-video 0 stops at BASE_FILES.

    Bug caught: off-by-one on the include flag → fetches all 4 files always,
    wastes ~4 GB HF pull on cold boot.
    """
    calls: list[str] = []

    def fake_download(ref: str, filename: str, dest: Path) -> Path:
        calls.append(filename)
        p = dest / filename
        p.write_bytes(_fake_bytes(filename))
        return p

    monkeypatch.setattr(fw, "_download_one", fake_download)
    rc = fw.main(
        [
            "--bundle",
            "hf:JunhaoZhuang/FlashVSR-v1.1",
            "--dest",
            str(tmp_path),
            "--include-long-video",
            "0",
        ]
    )
    assert rc == 0
    assert set(calls) == set(BASE_FILES)


def test_full_bundle_fetches_four_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_manifest: dict[str, dict[str, str]],
) -> None:
    """RED: --include-long-video 1 fetches BASE + LONG_VIDEO."""
    calls: list[str] = []

    def fake_download(ref: str, filename: str, dest: Path) -> Path:
        calls.append(filename)
        p = dest / filename
        p.write_bytes(_fake_bytes(filename))
        return p

    monkeypatch.setattr(fw, "_download_one", fake_download)
    rc = fw.main(
        [
            "--bundle",
            "hf:JunhaoZhuang/FlashVSR-v1.1",
            "--dest",
            str(tmp_path),
            "--include-long-video",
            "1",
        ]
    )
    assert rc == 0
    assert set(calls) == set(BASE_FILES + LONG_VIDEO_FILES)


def test_sha_mismatch_raises_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_manifest: dict[str, dict[str, str]],
) -> None:
    """RED: post-download hash mismatch raises FlashVSRWeightsIncomplete.

    Bug caught: silent tolerance of corrupted download → runtime tensor
    shape errors that mask themselves as generic torch failures.
    """

    def bad_download(ref: str, filename: str, dest: Path) -> Path:
        p = dest / filename
        p.write_bytes(b"CORRUPT")
        return p

    monkeypatch.setattr(fw, "_download_one", bad_download)
    with pytest.raises(FlashVSRWeightsIncomplete):
        fw.main(
            [
                "--bundle",
                "hf:JunhaoZhuang/FlashVSR-v1.1",
                "--dest",
                str(tmp_path),
                "--include-long-video",
                "0",
            ]
        )


def test_module_does_not_import_kinoforge_core_registry() -> None:
    """RED: pod-safe import surface — registry must NOT be pulled.

    Bug caught: accidental `from kinoforge.core.registry import ...` reintroduces
    the P2-era embed-tree bloat that busted the 64 KB pod env-var ceiling.
    """
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; import kinoforge.upscalers.flashvsr._fetch_weights; "
            "assert 'kinoforge.core.registry' not in sys.modules, "
            "'registry leaked into pod-safe module'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_unknown_scheme_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RED: bundle ref with unknown scheme fails resolver.

    Bug caught: `gs://bucket/...` silently attempted → HTTP resolver
    returns confusing 400.
    """
    # Manifest lookup happens before the download loop; stub to avoid
    # touching the real packaged file (which will exist after this task
    # lands but keeps the test hermetic).
    monkeypatch.setattr(
        fw,
        "_load_manifest",
        lambda: {name: {"sha256": _fake_sha(name)} for name in BASE_FILES},
    )
    with pytest.raises(ValueError, match="unsupported"):
        fw.main(
            [
                "--bundle",
                "gs://bucket/flashvsr",
                "--dest",
                str(tmp_path),
                "--include-long-video",
                "0",
            ]
        )


def test_manifest_shipped_in_package() -> None:
    """RED: real manifest is packaged.

    Bug caught: forgetting to add weights_manifest.json to package
    manifest → pod runs but `_load_manifest()` raises FileNotFoundError.
    """
    manifest = fw._load_manifest()
    for name in BASE_FILES + LONG_VIDEO_FILES:
        assert name in manifest
        assert isinstance(manifest[name]["sha256"], str)
        assert len(manifest[name]["sha256"]) == 64  # sha256 hex length
