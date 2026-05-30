"""Tests for the HuggingFace model source (AC 1–6)."""

from __future__ import annotations

import importlib

import pytest

import kinoforge.sources.huggingface  # noqa: F401  — registers the source on import
from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import ValidationError
from kinoforge.sources.huggingface import HuggingFaceSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_creds(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> EnvCredentialProvider:
    """Return a credential provider backed by monkeypatched env.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        token: Token value, or None to delete the env var.

    Returns:
        An :class:`~kinoforge.core.credentials.EnvCredentialProvider`.
    """
    if token is None:
        monkeypatch.delenv("HF_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HF_TOKEN", token)
    return EnvCredentialProvider()


# ---------------------------------------------------------------------------
# AC1 — handles()
# ---------------------------------------------------------------------------


def test_handles_ref_with_path() -> None:
    """handles() returns True for hf:org/model:path/file.safetensors."""
    src = HuggingFaceSource()
    # Bug this catches: regex requiring a path segment, rejecting bare repo refs.
    assert src.handles("hf:org/model:path/file.safetensors") is True


def test_handles_bare_repo_ref() -> None:
    """handles() returns True for hf:org/model (no path)."""
    src = HuggingFaceSource()
    # Bug this catches: regex insisting on a path component and returning False
    # for bare repo refs, which should also be recognised (resolve raises later).
    assert src.handles("hf:org/model") is True


def test_handles_rejects_civitai_scheme() -> None:
    """handles() returns False for a CivitAI ref."""
    src = HuggingFaceSource()
    # Bug this catches: matching on "hf" substring inside foreign refs.
    assert src.handles("civitai:1") is False


def test_handles_rejects_https_url() -> None:
    """handles() returns False for an https:// URL."""
    src = HuggingFaceSource()
    # Bug this catches: treating any colon-containing string as an hf ref.
    assert src.handles("https://x") is False


# ---------------------------------------------------------------------------
# AC2 — resolve() returns correct Artifact
# ---------------------------------------------------------------------------


def test_resolve_returns_one_artifact_with_correct_url_and_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve() returns one Artifact with correct URL and filename."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:Wan-AI/Wan2.2:diffusion/model.safetensors", creds)

    # Bug this catches: returning an empty list or multiple artifacts.
    assert len(artifacts) == 1
    a = artifacts[0]
    # Bug: wrong URL template, e.g. missing /resolve/main/.
    assert (
        a.url
        == "https://huggingface.co/Wan-AI/Wan2.2/resolve/main/diffusion/model.safetensors"
    )
    # Bug: using the full path instead of just the final filename component.
    assert a.filename == "model.safetensors"


# ---------------------------------------------------------------------------
# AC3 — Authorization header set iff HF_TOKEN present
# ---------------------------------------------------------------------------


def test_resolve_artifact_has_auth_header_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With HF_TOKEN set, Artifact.headers contains Authorization: Bearer <token>."""
    creds = _make_creds(monkeypatch, "hf-secret-token")
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/model:file.bin", creds)

    # Bug this catches: reading HF_TOKEN from os.environ directly instead of creds,
    # or building the header dict but not attaching it to the Artifact.
    assert artifacts[0].headers.get("Authorization") == "Bearer hf-secret-token"


def test_resolve_artifact_has_no_auth_header_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without HF_TOKEN, Artifact.headers has no Authorization key."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/model:file.bin", creds)

    # Bug: always injecting an empty-string Authorization header.
    assert "Authorization" not in artifacts[0].headers


# ---------------------------------------------------------------------------
# AC4 — bare repo ref raises ValidationError
# ---------------------------------------------------------------------------


def test_bare_repo_raises_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve() raises ValidationError when no file path is provided."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    # Bug this catches: treating a bare repo as a valid resolvable ref, e.g.
    # by constructing a URL for the repo root instead of raising.
    with pytest.raises(ValidationError, match="specify a file path"):
        src.resolve("hf:org/model", creds)


# ---------------------------------------------------------------------------
# AC5 — multi-segment paths
# ---------------------------------------------------------------------------


def test_multi_segment_path_url_and_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """hf:org/model:a/b/c/d.bin → correct URL suffix and filename."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/model:a/b/c/d.bin", creds)

    # Bug this catches: splitting on wrong delimiter and including only the first
    # path segment, or misidentifying filename as full path.
    assert artifacts[0].url.endswith("/a/b/c/d.bin")
    assert artifacts[0].filename == "d.bin"


# ---------------------------------------------------------------------------
# AC6 — self-registration on import
# ---------------------------------------------------------------------------


def test_self_registers_on_import() -> None:
    """Importing kinoforge.sources.huggingface registers the source under 'hf'."""
    importlib.reload(kinoforge.sources.huggingface)
    src = registry.source_for_ref("hf:o/m:f")
    # Bug this catches: self-registration being conditional or using the wrong scheme,
    # leaving 'hf' refs unroutable.
    assert src.scheme == "hf"
    assert src.handles("hf:o/m:f") is True


def test_scheme_attribute_is_hf() -> None:
    """scheme class attribute is 'hf'."""
    assert HuggingFaceSource.scheme == "hf"
