"""Unit tests for kinoforge.core.lora._normalize_ref (URL → canonical ref).

Privacy invariant pinned per spec §"Privacy": ValueErrors raised by
_normalize_ref must NOT include the URL text in the message. Only the
scheme name + missing-param description.
"""

from __future__ import annotations

import logging

import pytest

from kinoforge.core.lora import _normalize_ref


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Canonical refs pass through unchanged.
        ("civitai:1234@5678", "civitai:1234@5678"),
        ("civarchive:111@222", "civarchive:111@222"),
        ("hf:Org/Repo:file.safetensors", "hf:Org/Repo:file.safetensors"),
        ("hf:Org/Repo", "hf:Org/Repo"),
        ("file:/local/path.safetensors", "file:/local/path.safetensors"),
        # Civitai URL — the user's exact example.
        (
            "https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081",
            "civitai:2197303@2474081",
        ),
        # Tolerates extra query params + capitalised host/scheme.
        (
            "https://civitai.com/models/2197303?utm_source=x&modelVersionId=2474081",
            "civitai:2197303@2474081",
        ),
        (
            "HTTPS://Civitai.com/models/1?modelVersionId=2",
            "civitai:1@2",
        ),
        # Civarchive URL — the user's exact example.
        (
            "https://civarchive.com/models/2197303?modelVersionId=2474081",
            "civarchive:2197303@2474081",
        ),
        # HF blob URL — main branch, no warning expected.
        (
            "https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors",
            "hf:Org/Repo:sub/file.safetensors",
        ),
        # HF bare-repo URL — explicitly OUT of scope, passes through.
        (
            "https://huggingface.co/Org/Repo",
            "https://huggingface.co/Org/Repo",
        ),
        # Unknown host — passthrough.
        (
            "https://example.com/random/path",
            "https://example.com/random/path",
        ),
    ],
)
def test_normalize_ref(raw: str, expected: str) -> None:
    assert _normalize_ref(raw) == expected


def test_normalize_civitai_url_without_modelVersionId_raises() -> None:
    """Bug catch: bare model URL is ambiguous (could be any version). Reject
    with a clear error; the error message must NOT echo the URL itself."""
    with pytest.raises(ValueError) as excinfo:
        _normalize_ref("https://civitai.com/models/2197303/arcane-style")
    msg = str(excinfo.value)
    assert "civitai URL missing required ?modelVersionId=" in msg
    # Privacy invariant — URL text must NOT appear in the message.
    assert "civitai.com" not in msg
    assert "2197303" not in msg
    assert "arcane-style" not in msg


def test_normalize_civarchive_url_without_modelVersionId_raises() -> None:
    with pytest.raises(ValueError) as excinfo:
        _normalize_ref("https://civarchive.com/models/2197303")
    msg = str(excinfo.value)
    assert "civarchive URL missing required ?modelVersionId=" in msg
    assert "civarchive.com" not in msg
    assert "2197303" not in msg


def test_normalize_hf_blob_url_main_branch_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug catch: emitting a branch-drop warning on the common `main` path
    would spam every operator who pastes a normal HF URL."""
    caplog.set_level(logging.WARNING, logger="kinoforge.core.lora")
    out = _normalize_ref(
        "https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors"
    )
    assert out == "hf:Org/Repo:sub/file.safetensors"
    drop_warnings = [
        r for r in caplog.records if "branch=" in r.message and "dropped" in r.message
    ]
    assert not drop_warnings, (
        f"main branch must NOT trigger a drop warning; got: "
        f"{[r.message for r in drop_warnings]}"
    )


def test_normalize_hf_blob_url_non_main_branch_warns_and_drops_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug catch: silently dropping a non-main branch would surprise an
    operator who pinned a specific branch on purpose."""
    caplog.set_level(logging.WARNING, logger="kinoforge.core.lora")
    out = _normalize_ref("https://huggingface.co/Org/Repo/blob/dev/file.safetensors")
    # Canonical hf: ref doesn't encode branch — `main` is the implicit pin.
    assert out == "hf:Org/Repo:file.safetensors"
    drop_warnings = [r for r in caplog.records if "branch=dev dropped" in r.message]
    assert len(drop_warnings) == 1, (
        f"expected exactly one branch-drop warning; got: "
        f"{[r.message for r in caplog.records]}"
    )
