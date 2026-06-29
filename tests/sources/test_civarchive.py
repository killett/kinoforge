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


_H2_PREFIX = '<h2 class="font-semibold text-xl flex items-center gap-2">'
_H2_SUFFIX = '<a title="Search for this file"></a>'


def test_extract_filename_from_h2_anchor() -> None:
    """Extracts a .safetensors filename from the civarchive <h2> file header."""
    html = f"{_H2_PREFIX}wan2.2_t2v_arcanestyle_high.safetensors{_H2_SUFFIX}"
    assert _extract_filename(html) == "wan2.2_t2v_arcanestyle_high.safetensors"


def test_extract_filename_accepts_all_known_extensions() -> None:
    """Each of safetensors / ckpt / pt / bin / gguf is recognised."""
    for ext in ("safetensors", "ckpt", "pt", "bin", "gguf"):
        html = f"{_H2_PREFIX}foo.{ext}{_H2_SUFFIX}"
        assert _extract_filename(html) == f"foo.{ext}"


def test_extract_filename_rejects_unknown_extension() -> None:
    """A `.zip` filename is NOT matched (not in the allowlist)."""
    # Bug this catches: regex using `.\w+` instead of explicit extension
    # allowlist, mis-identifying archive files as model files.
    html = f"{_H2_PREFIX}archive.zip{_H2_SUFFIX}"
    with pytest.raises(KinoforgeError):
        _extract_filename(html)


def test_extract_filename_ignores_mirror_paragraph() -> None:
    """Mirror-section <p class="font-medium ...">NAME</p> is ignored.

    Civarchive renders mirror filenames inside a `<p>` further down the
    page. The h2 anchor is the primary filename; mirror paragraphs MUST
    NOT shadow it.
    """
    # Bug this catches: a broad `>NAME.safetensors<` regex would match the
    # mirror paragraph's display text and shadow the canonical h2 anchor.
    html = (
        '<p class="font-medium text-neutral-400">'
        "wrong_mirror.safetensors</p>"
        f"{_H2_PREFIX}right_civarchive.safetensors{_H2_SUFFIX}"
    )
    assert _extract_filename(html) == "right_civarchive.safetensors"


def test_extract_filename_missing_h2_raises() -> None:
    """No <h2>filename.ext<a anchor → KinoforgeError."""
    html = "<html><body>no filename here</body></html>"
    with pytest.raises(KinoforgeError) as exc:
        _extract_filename(html)
    assert "parser needs maintenance" in str(exc.value)


def test_extract_filename_requires_trailing_anchor() -> None:
    """Filename without the trailing `<a` link is not matched.

    Bug this catches: matching ``<h2 ...>NAME</h2>`` without requiring the
    civarchive "Search for this file" link that follows the filename. A
    mirror page might place a filename inside an h2 without the search
    link; the trailing `<a` requirement keeps that from being mistaken for
    the canonical entry.
    """
    html = f"{_H2_PREFIX}foo.safetensors</h2>"
    with pytest.raises(KinoforgeError):
        _extract_filename(html)


# ---------------------------------------------------------------------------
# _urllib_fetch_html — transport
# ---------------------------------------------------------------------------

from urllib.error import HTTPError  # noqa: E402
from urllib.request import Request  # noqa: E402

from kinoforge.core.errors import AuthError  # noqa: E402
from kinoforge.sources.civarchive import _urllib_fetch_html  # noqa: E402


class _StubResponse:
    """Minimal urlopen() context-manager stub."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _StubResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fetch_html_returns_decoded_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 OK → decoded UTF-8 body string."""
    captured_url: list[str] = []
    captured_headers: list[dict[str, str]] = []

    def fake_urlopen(req: Request) -> _StubResponse:
        captured_url.append(req.full_url)
        captured_headers.append(dict(req.headers))
        return _StubResponse(b"hello <h2>x.safetensors</h2>")

    monkeypatch.setattr("kinoforge.sources.civarchive.urlopen", fake_urlopen)

    body = _urllib_fetch_html("https://civarchive.com/foo", {})

    assert body == "hello <h2>x.safetensors</h2>"
    # urllib title-cases header keys.
    assert captured_headers[0].get("User-agent") == "kinoforge-smoke/0.1"


def test_fetch_html_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 → AuthError mentioning the URL."""

    # Bug this catches: catching all HTTPErrors as KinoforgeError, hiding the
    # AuthError signal the credential layer relies on.
    def fake_urlopen(req: Request) -> _StubResponse:
        raise HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr("kinoforge.sources.civarchive.urlopen", fake_urlopen)

    with pytest.raises(AuthError) as exc:
        _urllib_fetch_html("https://civarchive.com/x", {})
    assert "401" in str(exc.value)


def test_fetch_html_404_raises_kinoforge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404 → KinoforgeError (NOT AuthError)."""

    def fake_urlopen(req: Request) -> _StubResponse:
        raise HTTPError(
            url=req.full_url,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr("kinoforge.sources.civarchive.urlopen", fake_urlopen)

    with pytest.raises(KinoforgeError) as exc:
        _urllib_fetch_html("https://civarchive.com/x", {})
    assert "404" in str(exc.value)
    assert not isinstance(exc.value, AuthError)


def test_fetch_html_caller_headers_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-supplied headers are sent on the request."""
    captured_headers: list[dict[str, str]] = []

    def fake_urlopen(req: Request) -> _StubResponse:
        captured_headers.append(dict(req.headers))
        return _StubResponse(b"")

    monkeypatch.setattr("kinoforge.sources.civarchive.urlopen", fake_urlopen)

    _urllib_fetch_html("https://civarchive.com/x", {"X-Custom": "yes"})

    assert captured_headers[0].get("X-custom") == "yes"
