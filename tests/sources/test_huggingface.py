"""Tests for the HuggingFace model source (AC 1–6)."""

from __future__ import annotations

import importlib
import urllib.parse
from typing import Any

import pytest

import kinoforge.sources.huggingface  # noqa: F401  — registers the source on import
from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError
from kinoforge.sources.huggingface import (
    FetchCallable,
    HuggingFaceSource,
    _next_cursor_from_link,
    _parse_hf_ref,
)

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
# AC4 — bare repo ref resolves via tree API (Phase 30 T4 — was raise)
# ---------------------------------------------------------------------------


def test_bare_repo_resolves_via_tree_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare repo refs enumerate the tree, no longer raise ValidationError.

    Bug this catches: regression to the Task 3 deferred-raise behaviour.
    """
    creds = _make_creds(monkeypatch, None)
    entries = [
        {"type": "file", "path": "weights.safetensors", "size": 1, "oid": "b"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert len(artifacts) == 1
    assert artifacts[0].filename == "weights.safetensors"


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


# ---------------------------------------------------------------------------
# Phase 30 — parser
# ---------------------------------------------------------------------------


def test_parse_bare_ref_no_revision() -> None:
    """Bug: defaulting revision to empty or None instead of 'main'."""
    assert _parse_hf_ref("hf:org/repo") == ("org/repo", "main", None)


def test_parse_bare_ref_with_revision() -> None:
    """Bug: splitting on '@' before ':' would misparse 'hf:org/repo@v1.0'."""
    assert _parse_hf_ref("hf:org/repo@v1.0") == ("org/repo", "v1.0", None)


def test_parse_single_file_no_revision() -> None:
    """Bug: dropping the multi-segment path when splitting."""
    assert _parse_hf_ref("hf:org/repo:a/b.bin") == ("org/repo", "main", "a/b.bin")


def test_parse_single_file_with_revision() -> None:
    """Bug: parsing order — must split on ':' first, then '@' on the head."""
    assert _parse_hf_ref("hf:org/repo@abc:a/b.bin") == ("org/repo", "abc", "a/b.bin")


# ---------------------------------------------------------------------------
# Phase 30 — Link header cursor extraction
# ---------------------------------------------------------------------------


def test_next_cursor_empty_link_header_returns_none() -> None:
    """Bug: KeyError or AttributeError on empty Link header."""
    assert _next_cursor_from_link("") is None


def test_next_cursor_no_next_rel_returns_none() -> None:
    """Bug: matching any rel-value rather than rel='next' specifically."""
    link = '<https://x/y?cursor=tok>; rel="prev"'
    assert _next_cursor_from_link(link) is None


def test_next_cursor_extracts_cursor_query_param() -> None:
    """Bug: regexing for `cursor=` in the raw Link string and missing URL-encoding."""
    link = '<https://x/y?cursor=eyJfaWQiOiJ0b2sifQ%3D%3D&recursive=true>; rel="next"'
    cursor = _next_cursor_from_link(link)
    # parse_qs URL-decodes the value, so the trailing == is restored.
    assert cursor == "eyJfaWQiOiJ0b2sifQ=="


# ---------------------------------------------------------------------------
# Phase 30 — FetchCallable type is exported
# ---------------------------------------------------------------------------


def test_fetch_callable_type_importable() -> None:
    """Bug: FetchCallable not exported from the module."""
    assert FetchCallable is not None


# ---------------------------------------------------------------------------
# Phase 30 — @<rev> interpolation in single-file branch
# ---------------------------------------------------------------------------


def test_resolve_single_file_with_revision_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: hardcoded /resolve/main/ ignoring parsed revision."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/repo@v1.0:path/file.bin", creds)
    assert len(artifacts) == 1
    assert (
        artifacts[0].url == "https://huggingface.co/org/repo/resolve/v1.0/path/file.bin"
    )


def test_resolve_single_file_default_revision_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: parser regression dropping the default 'main' revision."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/repo:path/file.bin", creds)
    assert artifacts[0].url.endswith("/resolve/main/path/file.bin")


# ---------------------------------------------------------------------------
# Phase 30 — Tree branch helpers + stub fetch
# ---------------------------------------------------------------------------


def _make_stub_fetch(
    pages: list[tuple[list[dict[str, Any]], str | None]],
    log: list[tuple[str, dict[str, str]]] | None = None,
) -> FetchCallable:
    """Return a stub FetchCallable that pops a page per call.

    Args:
        pages: Pre-canned ``(entries, next_cursor)`` tuples, popped in order.
        log: Optional mutable list that accumulates ``(url, headers)``
            tuples in call order.

    Returns:
        A callable matching the :data:`FetchCallable` signature.
    """
    pages_iter = iter(pages)

    def _stub(
        url: str, headers: dict[str, str]
    ) -> tuple[list[dict[str, Any]], str | None]:
        if log is not None:
            log.append((url, dict(headers)))
        return next(pages_iter)

    return _stub


def test_tree_branch_one_page_emits_one_artifact_per_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: emitting directory entries as Artifacts, or dropping files."""
    creds = _make_creds(monkeypatch, None)
    entries: list[dict[str, Any]] = [
        {"type": "file", "path": "config.json", "size": 12, "oid": "blob1"},
        {"type": "directory", "path": "unet", "oid": "blob2"},
        {
            "type": "file",
            "path": "unet/model.safetensors",
            "size": 999,
            "oid": "blob3",
            "lfs": {
                "oid": "ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890",
                "size": 999,
                "pointerSize": 134,
            },
        },
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert len(artifacts) == 2
    assert {a.filename for a in artifacts} == {
        "config.json",
        "unet/model.safetensors",
    }


def test_tree_branch_filename_preserves_subdirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: rsplit('/')[-1] would flatten 'unet/foo.safetensors' to 'foo.safetensors'."""
    creds = _make_creds(monkeypatch, None)
    entries = [
        {
            "type": "file",
            "path": "unet/foo.safetensors",
            "size": 1,
            "oid": "b",
        },
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert artifacts[0].filename == "unet/foo.safetensors"


def test_tree_branch_lfs_oid_lowercased_onto_sha256(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: forwarding LFS oid verbatim; downloader's sha256_file output is lowercase."""
    creds = _make_creds(monkeypatch, None)
    entries: list[dict[str, Any]] = [
        {
            "type": "file",
            "path": "weights.safetensors",
            "size": 1,
            "oid": "blob",
            "lfs": {"oid": "ABC123DEF", "size": 1, "pointerSize": 134},
        },
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert artifacts[0].sha256 == "abc123def"


def test_tree_branch_non_lfs_entry_sha256_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: KeyError on small files lacking 'lfs', or defaulting to git-blob oid."""
    creds = _make_creds(monkeypatch, None)
    entries = [
        {"type": "file", "path": "config.json", "size": 12, "oid": "blob"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert artifacts[0].sha256 is None


def test_tree_branch_size_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug: dropping size, breaking downloader skip-path size heuristics."""
    creds = _make_creds(monkeypatch, None)
    entries = [
        {"type": "file", "path": "a.bin", "size": 4242, "oid": "blob"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert artifacts[0].size == 4242


def test_tree_branch_authorization_header_attached_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: skipping the auth header on tree artifacts."""
    creds = _make_creds(monkeypatch, "hf-secret")
    entries = [
        {"type": "file", "path": "a.bin", "size": 1, "oid": "blob"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert artifacts[0].headers.get("Authorization") == "Bearer hf-secret"


def test_tree_branch_no_authorization_header_when_token_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: emitting an empty 'Bearer ' Authorization header."""
    creds = _make_creds(monkeypatch, None)
    entries = [
        {"type": "file", "path": "a.bin", "size": 1, "oid": "blob"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert "Authorization" not in artifacts[0].headers


def test_tree_branch_pagination_accumulates_all_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: returning only page 1, dropping pages 2..N silently."""
    creds = _make_creds(monkeypatch, None)
    page1 = [{"type": "file", "path": "a.bin", "size": 1, "oid": "1"}]
    page2 = [{"type": "file", "path": "b.bin", "size": 1, "oid": "2"}]
    page3 = [{"type": "file", "path": "c.bin", "size": 1, "oid": "3"}]
    src = HuggingFaceSource(
        fetch=_make_stub_fetch([(page1, "TOK1"), (page2, "TOK2"), (page3, None)])
    )
    artifacts = src.resolve("hf:org/repo", creds)
    assert [a.filename for a in artifacts] == ["a.bin", "b.bin", "c.bin"]


def test_tree_branch_pagination_terminates_when_cursor_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: infinite loop when next_cursor stays None on the first page."""
    creds = _make_creds(monkeypatch, None)
    entries = [{"type": "file", "path": "a.bin", "size": 1, "oid": "1"}]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)]))
    artifacts = src.resolve("hf:org/repo", creds)
    assert len(artifacts) == 1


def test_tree_branch_first_url_uses_recursive_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: omitting recursive=true would only list the repo root."""
    creds = _make_creds(monkeypatch, None)
    log: list[tuple[str, dict[str, str]]] = []
    src = HuggingFaceSource(fetch=_make_stub_fetch([([], None)], log=log))
    src.resolve("hf:org/repo", creds)
    assert log[0][0] == (
        "https://huggingface.co/api/models/org/repo/tree/main?recursive=true"
    )


def test_tree_branch_paginated_url_appends_url_encoded_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: forwarding the cursor without URL-encoding (tokens contain '+', '/', '=')."""
    creds = _make_creds(monkeypatch, None)
    log: list[tuple[str, dict[str, str]]] = []
    cursor_raw = "a/b+c="
    src = HuggingFaceSource(
        fetch=_make_stub_fetch([([], cursor_raw), ([], None)], log=log)
    )
    src.resolve("hf:org/repo", creds)
    assert "&cursor=" in log[1][0]
    encoded = urllib.parse.quote(cursor_raw)
    assert log[1][0].endswith(f"&cursor={encoded}")


def test_tree_branch_empty_file_list_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: raising on empty repo instead of returning []."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource(fetch=_make_stub_fetch([([], None)]))
    assert src.resolve("hf:org/repo", creds) == []


def test_tree_branch_propagates_auth_error_from_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: swallowing AuthError instead of re-raising."""
    creds = _make_creds(monkeypatch, "bad-token")

    def _raise_auth(
        url: str, headers: dict[str, str]
    ) -> tuple[list[dict[str, Any]], str | None]:
        raise AuthError("HuggingFace 401 Unauthorized for ...")

    src = HuggingFaceSource(fetch=_raise_auth)
    with pytest.raises(AuthError, match="401"):
        src.resolve("hf:org/repo", creds)


def test_tree_branch_revision_threaded_into_url_and_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: tree URL using 'main' and Artifact URL also using 'main' when ref pins @<rev>."""
    creds = _make_creds(monkeypatch, None)
    log: list[tuple[str, dict[str, str]]] = []
    entries = [
        {"type": "file", "path": "a.bin", "size": 1, "oid": "blob"},
    ]
    src = HuggingFaceSource(fetch=_make_stub_fetch([(entries, None)], log=log))
    artifacts = src.resolve("hf:org/repo@v1.0", creds)
    assert log[0][0] == (
        "https://huggingface.co/api/models/org/repo/tree/v1.0?recursive=true"
    )
    assert artifacts[0].url == "https://huggingface.co/org/repo/resolve/v1.0/a.bin"
