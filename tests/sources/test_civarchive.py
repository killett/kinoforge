"""Tests for the CivArchive model source.

This module is built up across plan tasks 0, 3, 4 — the extractor-only
helpers land first, then resolve(), then self-registration. Tests are
grouped by AC section.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import KinoforgeError
from kinoforge.sources.civarchive import (
    _REF_RE,
    _extract_filename,
    _extract_sha256,
)

# ---------------------------------------------------------------------------
# Ref pattern
# ---------------------------------------------------------------------------


def test_ref_re_matches_canonical_with_version() -> None:
    """_REF_RE accepts civarchive:<digits>@<digits>."""
    # Bug this catches: regex requiring version always, breaking parse-accept
    # of bare civarchive:N refs that resolve() raises on separately.
    assert _REF_RE.match("civarchive:111@222") is not None


def test_ref_re_matches_bare_model_only() -> None:
    """_REF_RE accepts civarchive:<digits> with no @<vid>."""
    assert _REF_RE.match("civarchive:111") is not None


def test_ref_re_rejects_civitai_scheme() -> None:
    """_REF_RE rejects civitai: refs (separate scheme, distinct resolver)."""
    # Bug this catches: matching on "civi" prefix and routing civitai refs
    # to civarchive by mistake.
    assert _REF_RE.match("civitai:111@222") is None


def test_ref_re_rejects_non_numeric_id() -> None:
    """_REF_RE rejects refs with non-digit model or version IDs."""
    assert _REF_RE.match("civarchive:abc@222") is None
    assert _REF_RE.match("civarchive:111@xyz") is None


def test_ref_re_rejects_hf_scheme() -> None:
    """_REF_RE rejects hf: refs."""
    assert _REF_RE.match("hf:org/repo") is None


# ---------------------------------------------------------------------------
# _extract_sha256
# ---------------------------------------------------------------------------


_VALID_HASH = "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d"


def test_extract_sha256_returns_hex_from_anchor_href() -> None:
    """Extracts the hex hash from the /sha256/<hex64> anchor href."""
    html = f'<a href="/sha256/{_VALID_HASH}">{_VALID_HASH}</a>'
    assert _extract_sha256(html) == _VALID_HASH


def test_extract_sha256_lowercase_only() -> None:
    """Uppercase hex in the href is NOT matched (civarchive uses lowercase).

    This documents the v1 contract: the regex character class is `[0-9a-f]`,
    not case-insensitive. If civarchive ever serves uppercase hex (no
    precedent observed), this test will fail and the regex will need to
    grow `re.IGNORECASE`.
    """
    html = f'<a href="/sha256/{_VALID_HASH.upper()}">...</a>'
    with pytest.raises(KinoforgeError) as exc:
        _extract_sha256(html)
    assert "parser needs maintenance" in str(exc.value)


def test_extract_sha256_missing_anchor_raises() -> None:
    """Raises KinoforgeError mentioning 'parser needs maintenance'."""
    # Bug this catches: silently returning empty string when no hash anchor
    # is present, leading to sha256="" propagating into Artifact and a
    # confusing downstream "sha256 mismatch" error.
    html = "<html><body>no hash here</body></html>"
    with pytest.raises(KinoforgeError) as exc:
        _extract_sha256(html)
    assert "parser needs maintenance" in str(exc.value)


def test_extract_sha256_returns_first_match() -> None:
    """Multiple /sha256/ anchors → returns the first."""
    # Documents v1 contract: multi-file model-versions are out of scope.
    # The Source returns one Artifact built from the first hash + first
    # filename. If/when multi-file support is added, this test must change.
    second_hash = "a" * 64
    html = (
        f'<a href="/sha256/{_VALID_HASH}">{_VALID_HASH}</a>'
        f'<a href="/sha256/{second_hash}">{second_hash}</a>'
    )
    assert _extract_sha256(html) == _VALID_HASH


def test_extract_sha256_rejects_short_hex() -> None:
    """A 63-char hex href is not a valid sha256 anchor."""
    # Bug this catches: regex using `+` instead of `{64}` for the hex
    # quantifier, accepting any-length hex including truncations.
    short_hex = "a" * 63
    html = f'<a href="/sha256/{short_hex}">...</a>'
    with pytest.raises(KinoforgeError):
        _extract_sha256(html)


# ---------------------------------------------------------------------------
# _extract_filename
# ---------------------------------------------------------------------------


def test_extract_filename_from_h4() -> None:
    """Extracts a .safetensors filename inside <h4>...</h4>."""
    html = "<h4>wan2.2_t2v_arcanestyle_high.safetensors</h4>"
    assert _extract_filename(html) == "wan2.2_t2v_arcanestyle_high.safetensors"


def test_extract_filename_accepts_all_known_extensions() -> None:
    """Each of safetensors / ckpt / pt / bin / gguf is recognised inside <h4>."""
    for ext in ("safetensors", "ckpt", "pt", "bin", "gguf"):
        html = f"<h4>foo.{ext}</h4>"
        assert _extract_filename(html) == f"foo.{ext}"


def test_extract_filename_rejects_unknown_extension() -> None:
    """A `.zip` filename inside <h4> is NOT matched (not in the allowlist)."""
    # Bug this catches: regex using `.\w+` instead of explicit extension
    # allowlist, mis-identifying archive files as model files.
    html = "<h4>archive.zip</h4>"
    with pytest.raises(KinoforgeError):
        _extract_filename(html)


def test_extract_filename_anchored_on_h4_not_anchor_text() -> None:
    """HF mirror anchor text BEFORE the <h4> is ignored."""
    # Bug this catches: a broad `>NAME.safetensors<` regex would match the
    # HF mirror anchor's *display text* (a renamed mirror filename like
    # Arcane_style__t2v__HIGH.safetensors) instead of the canonical
    # civarchive filename (wan2.2_t2v_arcanestyle_high.safetensors).
    html = (
        '<a href="https://huggingface.co/x/y/resolve/main/wrong_mirror.safetensors">'
        "wrong_mirror.safetensors</a>"
        "<h4>right_civarchive.safetensors</h4>"
    )
    assert _extract_filename(html) == "right_civarchive.safetensors"


def test_extract_filename_missing_h4_raises() -> None:
    """No <h4>filename.ext</h4> anchor → KinoforgeError."""
    html = "<html><body>no filename here</body></html>"
    with pytest.raises(KinoforgeError) as exc:
        _extract_filename(html)
    assert "parser needs maintenance" in str(exc.value)
