# CivArchive Source Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `CivArchiveSource` (a `ModelSource` plug-in) so `civarchive:N@N` refs from sub-project A resolve to one `Artifact` each, unblocking the downloader path for civarchive-hosted LoRAs.

**Architecture:** New module `src/kinoforge/sources/civarchive/__init__.py` mirroring `civitai/__init__.py` shape. Resolves by HTML-scraping `civarchive.com/models/<id>?modelVersionId=<vid>` because civarchive publishes no JSON metadata API. Two stdlib regexes (sha256 anchored on `/sha256/<hex64>` href; filename anchored on `<h4>...ext</h4>`) extract integrity + name. `Artifact.url` stays as the civarchive endpoint so civarchive's 307 redirect chain owns host indirection. Bare `civarchive:N` refs rejected at resolve (symmetric with sub-project A's URL-normalize rejection).

**Tech Stack:** Python 3.x stdlib (`urllib.request`, `re`); pytest for unit tests; pre-existing kinoforge `ModelSource` ABC and registry.

**User decisions (already made):**
- Metadata strategy: hybrid HTML scrape (sha256 + filename; keep `Artifact.url` as the civarchive endpoint, not the resolved download host).
- Bare-ref policy: reject `civarchive:N` at resolve with a clear `KinoforgeError`; no scraping of model-default-version selectors.
- Parser tool: narrow stdlib regex on structural anchors. Filename regex MUST anchor on `<h4>...</h4>` to avoid the HF-mirror-link false positive (HF mirror text and civarchive filename differ).
- Auth model: HTML fetch is anonymous (`headers={}`). `CIVITAI_TOKEN`, when present in creds, attached to `Artifact.headers` for the eventual download — mirrors `CivitAISource` exactly.
- Live evidence: one $0 anonymous HTTP GET against `civarchive:2197303@2474081`. No live file download (downloader's separate concern).

**Spec:** `docs/superpowers/specs/2026-06-28-civarchive-source-design.md` (commit `53424dc`).

---

## File structure

| Path | Responsibility | Status |
|------|----------------|--------|
| `src/kinoforge/sources/civarchive/__init__.py` | Module: `_REF_RE`, `_urllib_fetch_html`, `_extract_sha256`, `_extract_filename`, `CivArchiveSource`, self-register | Create |
| `src/kinoforge/_adapters.py` | Add `import kinoforge.sources.civarchive` next to existing civitai/hf | Modify (1 line) |
| `tests/sources/civarchive/__init__.py` | Empty `__init__` so pytest discovers the dir | Create |
| `tests/sources/civarchive/fixtures/version_2474081.html` | Pinned HTML snapshot of `civarchive.com/models/2197303?modelVersionId=2474081` for replay tests | Create |
| `tests/sources/test_civarchive.py` | Unit tests (no network) covering `handles`, `resolve`, both extractors, auth header, error mapping, self-register | Create |
| `tests/live/evidence/2026-06-28-civarchive-source/resolve.py` | Live-resolve script ($0, anonymous GET) | Create |
| `tests/live/evidence/2026-06-28-civarchive-source/evidence.md` | Human-readable smoke evidence | Create |
| `tests/live/evidence/2026-06-28-civarchive-source/response_meta.json` | Status + response headers from the smoke fetch | Create |
| `docs/warm-reuse.md` | Replace the "not yet implemented" stub at lines 146–149 | Modify |
| `PROGRESS.md` | Close sub-project B workstream block; drop priority pointer at lines 45–77; update active workstream pointer | Modify |

Each file has one responsibility. The module file is the only meaningful surface; everything else is tests, fixtures, or docs.

---

## Task 0: HTML extractor helpers — pure functions + RED-GREEN tests

**Goal:** Land `_extract_sha256` and `_extract_filename` as pure functions over a string with full unit coverage. No `CivArchiveSource` class yet; no fetch; no registry.

**Files:**
- Create: `src/kinoforge/sources/civarchive/__init__.py` (helpers + module docstring + `_REF_RE` only — `CivArchiveSource` placeholder added in Task 3)
- Create: `tests/sources/civarchive/__init__.py` (empty file so pytest discovers)
- Create: `tests/sources/test_civarchive.py` (extractor unit tests only)

**Acceptance Criteria:**
- [ ] `_extract_sha256(html)` returns the 64-char lowercase hex when `<a href="/sha256/HEX">` appears.
- [ ] `_extract_sha256(html)` raises `KinoforgeError` with the literal phrase "parser needs maintenance" when no `/sha256/` href is present.
- [ ] `_extract_sha256` returns the **first** match when multiple `/sha256/...` hrefs are present (documents v1 contract).
- [ ] `_extract_filename(html)` returns the filename text inside `<h4>NAME.EXT</h4>` for ext in `{safetensors, ckpt, pt, bin, gguf}`.
- [ ] `_extract_filename(html)` raises `KinoforgeError` with the literal phrase "parser needs maintenance" when no matching `<h4>` is present.
- [ ] `_extract_filename(html)` ignores `.safetensors` text inside `<a>` tags (HF-mirror anchor text) — anchored on `<h4>` specifically.
- [ ] `_REF_RE` matches `civarchive:111`, `civarchive:111@222`; does NOT match `civitai:111`, `civarchive:abc`, `hf:org/repo`.

**Verify:** `pixi run pytest tests/sources/test_civarchive.py -v` → all extractor tests PASS

**Steps:**

- [ ] **Step 1: Create empty test package init**

```python
# tests/sources/civarchive/__init__.py
```

(Empty file — required for pytest discovery of the fixtures dir as a package adjacent to the tests.)

- [ ] **Step 2: Write the failing tests**

```python
# tests/sources/test_civarchive.py
"""Tests for the CivArchive model source.

This module is built up across plan tasks 0, 3, 4 — the extractor-only
helpers land first, then resolve(), then self-registration. Tests are
grouped by AC section.
"""

from __future__ import annotations

import re

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
```

- [ ] **Step 3: Run tests — confirm RED (module does not yet exist)**

Run: `pixi run pytest tests/sources/test_civarchive.py -v`
Expected: `ImportError` or `ModuleNotFoundError` on `from kinoforge.sources.civarchive import ...`. All tests fail to collect.

- [ ] **Step 4: Write the minimal implementation**

```python
# src/kinoforge/sources/civarchive/__init__.py
"""CivArchive model source — resolves ``civarchive:<modelId>@<versionId>`` refs.

CivArchive is the archival successor to CivitAI for content that may have been
removed from the original host. It shares CivitAI's numeric ``modelId`` /
``versionId`` scheme but publishes **no JSON metadata API**: this module
HTML-scrapes the canonical model-version page for the SHA256 hash and the
filename, then builds one :class:`~kinoforge.core.interfaces.Artifact` whose
``url`` points at ``civarchive.com/api/download/models/<vid>``. That endpoint
307-redirects to whichever host CivArchive currently mirrors (civitai.com,
HuggingFace, or its own ``/sha256/`` store) — keeping ``Artifact.url`` at the
abstract CivArchive endpoint preserves cache validity across CivArchive
re-mirror events.

Parser anchors (do NOT loosen without re-pinning the fixture):

- SHA256: ``<a href="/sha256/<hex64>">`` — the ``/sha256/`` URL path is part
  of CivArchive's own routing.
- Filename: ``<h4>NAME.EXT</h4>`` for EXT in
  ``{safetensors, ckpt, pt, bin, gguf}``.

The HTML transport is injected via the ``fetch`` constructor parameter so tests
can pass a spy/stub without touching the network at all.
"""

from __future__ import annotations

import re

from kinoforge.core.errors import KinoforgeError

# ---------------------------------------------------------------------------
# Ref pattern (mirrors civitai source)
# ---------------------------------------------------------------------------

_REF_RE = re.compile(r"^civarchive:(\d+)(?:@(\d+))?$")

# ---------------------------------------------------------------------------
# HTML parser helpers
# ---------------------------------------------------------------------------

_SHA256_HREF_RE = re.compile(r'href="/sha256/([0-9a-f]{64})"')
_FILENAME_RE = re.compile(
    r"<h4>([^<>\s]+\.(?:safetensors|ckpt|pt|bin|gguf))</h4>"
)


def _extract_sha256(html: str) -> str:
    """Extract the sha256 hex from the CivArchive HTML body.

    Args:
        html: HTML text from a CivArchive model-version page.

    Returns:
        The 64-char lowercase hex hash.

    Raises:
        KinoforgeError: No ``/sha256/<hex64>`` anchor is present.
    """
    m = _SHA256_HREF_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing /sha256/ anchor — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)


def _extract_filename(html: str) -> str:
    """Extract the model filename from the CivArchive HTML body.

    Args:
        html: HTML text from a CivArchive model-version page.

    Returns:
        The filename (e.g. ``"model.safetensors"``).

    Raises:
        KinoforgeError: No ``<h4>filename.ext</h4>`` anchor is present
            for a recognised model file extension.
    """
    m = _FILENAME_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing model filename — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)
```

- [ ] **Step 5: Run tests — confirm GREEN**

Run: `pixi run pytest tests/sources/test_civarchive.py -v`
Expected: all 11 tests in this task PASS (5 ref-pattern + 5 sha256 + 6 filename = actually 12; if a single ext test is parametrised differently, the count may shift — verify all PASS regardless of count).

- [ ] **Step 6: Format + lint**

Run: `pixi run pre-commit run --files src/kinoforge/sources/civarchive/__init__.py tests/sources/test_civarchive.py tests/sources/civarchive/__init__.py`
Expected: PASS (or auto-fixes applied; re-stage and re-run if so).

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/sources/civarchive/__init__.py \
        tests/sources/civarchive/__init__.py \
        tests/sources/test_civarchive.py
git commit -m "feat(civarchive): HTML extractor helpers _extract_sha256 + _extract_filename"
```

---

## Task 1: Pin HTML fixture from live civarchive page

**Goal:** Capture the live HTML for `civarchive:2197303@2474081`, scrub nothing (civarchive HTML has no PII), commit as a binary-stable fixture for replay tests in Task 3.

**Files:**
- Create: `tests/sources/civarchive/fixtures/version_2474081.html`

**Acceptance Criteria:**
- [ ] File exists at the exact path above.
- [ ] File contains the literal hex `67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d` somewhere in the body (allows Task 3's resolve test to assert).
- [ ] File contains `<h4>wan2.2_t2v_arcanestyle_high.safetensors</h4>` (or the exact `<h4>` wrapper around that filename).
- [ ] File is < 500 KB (pre-commit `check-added-large-files` limit).

**Verify:** `grep -l "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d" tests/sources/civarchive/fixtures/version_2474081.html` returns the file path; file size < 500 KB.

**Steps:**

- [ ] **Step 1: Create the fixtures dir**

```bash
mkdir -p tests/sources/civarchive/fixtures
```

- [ ] **Step 2: Fetch the live HTML and write to disk**

Use a one-shot Python snippet (no network test framework needed — this is the offline-fixture pin, not a test):

```python
# scripts/_one_shot/pin_civarchive_fixture.py  (delete after use; do NOT commit this script)
import pathlib
from urllib.request import Request, urlopen

url = "https://civarchive.com/models/2197303?modelVersionId=2474081"
req = Request(url, headers={"User-Agent": "kinoforge-smoke/0.1"})
with urlopen(req) as resp:  # noqa: S310
    body = resp.read()

out = pathlib.Path("tests/sources/civarchive/fixtures/version_2474081.html")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(body)
print(f"wrote {len(body)} bytes to {out}")
```

Or directly via shell:

```bash
mkdir -p tests/sources/civarchive/fixtures
pixi run python -c "
from urllib.request import Request, urlopen
import pathlib
req = Request(
    'https://civarchive.com/models/2197303?modelVersionId=2474081',
    headers={'User-Agent': 'kinoforge-smoke/0.1'},
)
body = urlopen(req).read()
out = pathlib.Path('tests/sources/civarchive/fixtures/version_2474081.html')
out.write_bytes(body)
print(f'wrote {len(body)} bytes to {out}')
"
```

- [ ] **Step 3: Verify fixture contains the expected anchors**

```bash
grep -q "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d" \
    tests/sources/civarchive/fixtures/version_2474081.html \
    && echo "sha256 OK"

grep -q "wan2.2_t2v_arcanestyle_high.safetensors" \
    tests/sources/civarchive/fixtures/version_2474081.html \
    && echo "filename OK"

# Hard upper bound for pre-commit
wc -c < tests/sources/civarchive/fixtures/version_2474081.html
```

Expected: both `OK` lines print; size < 500000 bytes.

- [ ] **Step 4: Commit**

```bash
git add tests/sources/civarchive/fixtures/version_2474081.html
git commit -m "test(civarchive): pin live HTML fixture for version 2474081 replay tests"
```

---

## Task 2: HTTP fetch transport — `_urllib_fetch_html`

**Goal:** Land the stdlib HTML-fetch transport mirroring `_urllib_fetch_json`'s shape: Cloudflare-friendly UA, `HTTPError 401 → AuthError`, anything else → `KinoforgeError`. Inject via constructor in Task 3.

**Files:**
- Modify: `src/kinoforge/sources/civarchive/__init__.py` (add transport + `FetchHTMLCallable` type alias)
- Modify: `tests/sources/test_civarchive.py` (add transport-level tests using a stub urlopen)

**Acceptance Criteria:**
- [ ] `_urllib_fetch_html(url, headers)` returns the UTF-8-decoded body string on HTTP 200.
- [ ] Adds `User-Agent: kinoforge-smoke/0.1` to the request (Cloudflare gating same as civitai).
- [ ] Raises `AuthError` with the URL on HTTP 401.
- [ ] Raises `KinoforgeError` with the URL on any other HTTP error (404, 5xx).
- [ ] Caller-supplied `headers` are merged with the UA (UA does not overwrite a caller-supplied `User-Agent` — civitai source uses `{"User-Agent": "...", **headers}` so caller wins; mirror that order).
- [ ] `FetchHTMLCallable` type alias is exported.

**Verify:** `pixi run pytest tests/sources/test_civarchive.py -v -k "fetch"` → all transport tests PASS

**Steps:**

- [ ] **Step 1: Write the failing transport tests**

Append to `tests/sources/test_civarchive.py`:

```python
# ---------------------------------------------------------------------------
# _urllib_fetch_html — transport
# ---------------------------------------------------------------------------

from urllib.error import HTTPError
from urllib.request import Request

from kinoforge.core.errors import AuthError
from kinoforge.sources.civarchive import _urllib_fetch_html


class _StubResponse:
    """Minimal urlopen() context-manager stub."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_StubResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fetch_html_returns_decoded_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 OK → decoded UTF-8 body string."""
    captured: dict[str, object] = {}

    def fake_urlopen(req: Request) -> _StubResponse:
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return _StubResponse(b"hello <h4>x.safetensors</h4>")

    monkeypatch.setattr(
        "kinoforge.sources.civarchive.urlopen", fake_urlopen
    )

    body = _urllib_fetch_html("https://civarchive.com/foo", {})

    assert body == "hello <h4>x.safetensors</h4>"
    # Cloudflare-bypass UA always present.
    assert captured["headers"].get("User-agent") == "kinoforge-smoke/0.1"  # urllib title-cases header keys


def test_fetch_html_401_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(
        "kinoforge.sources.civarchive.urlopen", fake_urlopen
    )

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

    monkeypatch.setattr(
        "kinoforge.sources.civarchive.urlopen", fake_urlopen
    )

    with pytest.raises(KinoforgeError) as exc:
        _urllib_fetch_html("https://civarchive.com/x", {})
    assert "404" in str(exc.value)
    assert not isinstance(exc.value, AuthError)


def test_fetch_html_caller_headers_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caller-supplied headers are sent on the request."""
    captured: dict[str, object] = {}

    def fake_urlopen(req: Request) -> _StubResponse:
        captured["headers"] = dict(req.headers)
        return _StubResponse(b"")

    monkeypatch.setattr(
        "kinoforge.sources.civarchive.urlopen", fake_urlopen
    )

    _urllib_fetch_html("https://civarchive.com/x", {"X-Custom": "yes"})

    assert captured["headers"].get("X-custom") == "yes"
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run pytest tests/sources/test_civarchive.py -v -k "fetch"`
Expected: `ImportError` on `_urllib_fetch_html` or `urlopen` symbols not yet defined.

- [ ] **Step 3: Add transport to the module**

Update `src/kinoforge/sources/civarchive/__init__.py` — add imports + helpers BEFORE the existing `_REF_RE`:

```python
"""CivArchive model source — resolves ``civarchive:<modelId>@<versionId>`` refs.
...  (docstring unchanged)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core.errors import AuthError, KinoforgeError

# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------

FetchHTMLCallable = Callable[[str, dict[str, str]], str]


def _urllib_fetch_html(url: str, headers: dict[str, str]) -> str:
    """Fetch *url* with GET, return the response body as a UTF-8 string.

    Args:
        url: The endpoint URL.
        headers: HTTP request headers to include. The default
            ``User-Agent: kinoforge-smoke/0.1`` is prepended but a
            caller-supplied ``User-Agent`` overrides it.

    Returns:
        Decoded UTF-8 body.

    Raises:
        AuthError: The server returned HTTP 401.
        KinoforgeError: Any other non-2xx HTTP error or network failure.
    """
    # CivArchive is Cloudflare-fronted (same gating as civitai); the default
    # Python-urllib UA gets a 403. Mirror the civitai source pattern.
    request_headers = {"User-Agent": "kinoforge-smoke/0.1", **headers}
    req = Request(url, headers=request_headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310 — only civarchive.com HTTPS URLs used
            body: bytes = resp.read()
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(
                f"CivArchive 401 Unauthorized for {url}"
            ) from exc
        raise KinoforgeError(f"CivArchive HTTP {exc.code} for {url}") from exc
    return body.decode("utf-8")
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run pytest tests/sources/test_civarchive.py -v`
Expected: all extractor tests from Task 0 still PASS; new transport tests PASS.

- [ ] **Step 5: Format + lint**

Run: `pixi run pre-commit run --files src/kinoforge/sources/civarchive/__init__.py tests/sources/test_civarchive.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/sources/civarchive/__init__.py tests/sources/test_civarchive.py
git commit -m "feat(civarchive): _urllib_fetch_html transport with AuthError/KinoforgeError mapping"
```

---

## Task 3: `CivArchiveSource` class — `handles()` + `resolve()` with fixture replay

**Goal:** Land the `CivArchiveSource` class so refs route from the registry to its `resolve()`, which builds one `Artifact` from the pinned HTML fixture in tests. Bare-ref + invalid-ref + auth-header behaviour all locked in.

**Files:**
- Modify: `src/kinoforge/sources/civarchive/__init__.py` (add `CivArchiveSource`, but NOT the self-register call yet — that lands in Task 4)
- Modify: `tests/sources/test_civarchive.py` (add Source-level tests)

**Acceptance Criteria:**
- [ ] `CivArchiveSource.scheme == "civarchive"`.
- [ ] `handles("civarchive:111@222")` and `handles("civarchive:111")` both return True; `handles("civitai:111")` and `handles("civarchive:abc")` return False.
- [ ] `resolve("civarchive:111", creds)` raises `KinoforgeError` whose message contains "requires @<versionId>" — and the injected spy fetch is **never called** (assertion on `spy.calls == []`).
- [ ] `resolve("garbage", creds)` raises `ValueError` whose message contains "Not a valid civarchive ref".
- [ ] `resolve("civarchive:2197303@2474081", creds)` against the pinned `version_2474081.html` fixture returns `[Artifact(url='https://civarchive.com/api/download/models/2474081', filename='wan2.2_t2v_arcanestyle_high.safetensors', size=None, sha256='67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d', headers={}, uri='', meta={})]`.
- [ ] When `CIVITAI_TOKEN=foobar` is in the creds, `Artifact.headers == {"Authorization": "Bearer foobar"}`. Empty creds → `Artifact.headers == {}`.
- [ ] The HTML fetch is invoked exactly once per resolve, with URL `https://civarchive.com/models/<modelId>?modelVersionId=<vid>` and `headers={}` (no auth on the HTML fetch).
- [ ] Privacy: no error message in any test contains the literal substring `https://civarchive.com` (mirrors the lora-redaction convention).

**Verify:** `pixi run pytest tests/sources/test_civarchive.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing Source-level tests**

Append to `tests/sources/test_civarchive.py`:

```python
# ---------------------------------------------------------------------------
# CivArchiveSource — handles() + resolve()
# ---------------------------------------------------------------------------

import pathlib

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.interfaces import Artifact
from kinoforge.sources.civarchive import CivArchiveSource


_FIXTURE_PATH = (
    pathlib.Path(__file__).parent / "civarchive" / "fixtures" / "version_2474081.html"
)
_FIXTURE_HTML = _FIXTURE_PATH.read_text()


class SpyHTMLFetch:
    """Injectable fetch that returns canned HTML strings and records calls."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._responses = responses

    def __call__(self, url: str, headers: dict[str, str]) -> str:
        self.calls.append((url, headers))
        if url not in self._responses:
            raise KeyError(f"spy: no canned response for {url}")
        return self._responses[url]


def _make_creds(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> EnvCredentialProvider:
    if token is None:
        monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CIVITAI_TOKEN", token)
    return EnvCredentialProvider()


# --- scheme + handles ---


def test_scheme_attribute_is_civarchive() -> None:
    """scheme class attribute is 'civarchive'."""
    assert CivArchiveSource.scheme == "civarchive"


def test_handles_canonical_with_version() -> None:
    src = CivArchiveSource()
    assert src.handles("civarchive:111@222") is True


def test_handles_bare_model_only() -> None:
    """Bare ref is parse-accepted; resolve() raises separately."""
    src = CivArchiveSource()
    assert src.handles("civarchive:111") is True


def test_handles_rejects_civitai_scheme() -> None:
    src = CivArchiveSource()
    assert src.handles("civitai:111@222") is False


def test_handles_rejects_garbage() -> None:
    src = CivArchiveSource()
    assert src.handles("civarchive:abc@xyz") is False


# --- resolve: error paths ---


def test_resolve_bare_ref_raises_pre_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare civarchive:N rejected BEFORE any HTTP fetch."""
    spy = SpyHTMLFetch({})
    src = CivArchiveSource(fetch=spy)
    creds = _make_creds(monkeypatch, None)

    with pytest.raises(KinoforgeError) as exc:
        src.resolve("civarchive:111", creds)

    msg = str(exc.value)
    assert "requires @<versionId>" in msg
    # Bug this catches: validating bare-ref after the HTTP fetch, wasting a
    # request and producing a confusing parser-failure error on a page that
    # was never going to yield a single version.
    assert spy.calls == []
    # Privacy invariant.
    assert "https://civarchive.com" not in msg


def test_resolve_invalid_ref_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refs that don't match _REF_RE raise ValueError."""
    src = CivArchiveSource(fetch=SpyHTMLFetch({}))
    creds = _make_creds(monkeypatch, None)

    with pytest.raises(ValueError) as exc:
        src.resolve("garbage", creds)
    assert "Not a valid civarchive ref" in str(exc.value)


# --- resolve: happy path from pinned fixture ---


def _version_url(model_id: int, version_id: int) -> str:
    return (
        f"https://civarchive.com/models/{model_id}"
        f"?modelVersionId={version_id}"
    )


def test_resolve_returns_one_artifact_from_pinned_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay the pinned HTML; assert the exact Artifact contents."""
    page_url = _version_url(2197303, 2474081)
    spy = SpyHTMLFetch({page_url: _FIXTURE_HTML})
    src = CivArchiveSource(fetch=spy)
    creds = _make_creds(monkeypatch, None)

    artifacts = src.resolve("civarchive:2197303@2474081", creds)

    assert artifacts == [
        Artifact(
            filename="wan2.2_t2v_arcanestyle_high.safetensors",
            url="https://civarchive.com/api/download/models/2474081",
            size=None,
            sha256="67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d",
            headers={},
        )
    ]


def test_resolve_html_fetch_is_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML fetch carries no auth headers — civarchive HTML is anonymous."""
    # Bug this catches: leaking CIVITAI_TOKEN to civarchive.com (a foreign
    # host) by attaching the bearer header to the HTML request as well as
    # the Artifact.
    page_url = _version_url(2197303, 2474081)
    spy = SpyHTMLFetch({page_url: _FIXTURE_HTML})
    src = CivArchiveSource(fetch=spy)
    creds = _make_creds(monkeypatch, "secret-token-do-not-leak")

    src.resolve("civarchive:2197303@2474081", creds)

    assert len(spy.calls) == 1
    fetched_url, fetched_headers = spy.calls[0]
    assert fetched_url == page_url
    assert fetched_headers == {}  # NO Authorization on HTML fetch


def test_resolve_attaches_civitai_token_to_artifact_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CIVITAI_TOKEN, if present, propagates to Artifact.headers for the downloader."""
    page_url = _version_url(2197303, 2474081)
    spy = SpyHTMLFetch({page_url: _FIXTURE_HTML})
    src = CivArchiveSource(fetch=spy)
    creds = _make_creds(monkeypatch, "abc123")

    artifacts = src.resolve("civarchive:2197303@2474081", creds)

    assert artifacts[0].headers == {"Authorization": "Bearer abc123"}


def test_resolve_no_token_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No CIVITAI_TOKEN → empty Artifact.headers."""
    page_url = _version_url(2197303, 2474081)
    spy = SpyHTMLFetch({page_url: _FIXTURE_HTML})
    src = CivArchiveSource(fetch=spy)
    creds = _make_creds(monkeypatch, None)

    artifacts = src.resolve("civarchive:2197303@2474081", creds)

    assert artifacts[0].headers == {}


# --- resolve: privacy invariant for every error path ---


def test_resolve_error_messages_never_leak_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No error from any resolve path contains the literal civarchive URL."""
    # Bug this catches: surfacing the fetched URL inside KinoforgeError
    # messages and breaking the lora-redaction convention established by
    # sub-project A.
    src = CivArchiveSource(fetch=SpyHTMLFetch({}))
    creds = _make_creds(monkeypatch, None)

    with pytest.raises((KinoforgeError, ValueError)) as exc:
        src.resolve("civarchive:111", creds)
    assert "https://civarchive.com" not in str(exc.value)

    with pytest.raises((KinoforgeError, ValueError)) as exc:
        src.resolve("garbage", creds)
    assert "https://civarchive.com" not in str(exc.value)
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run pytest tests/sources/test_civarchive.py -v`
Expected: `ImportError` on `CivArchiveSource`.

- [ ] **Step 3: Add the Source class**

Append to `src/kinoforge/sources/civarchive/__init__.py` (after the existing helpers, BEFORE any registry call):

```python
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource

# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


class CivArchiveSource(ModelSource):
    """Resolves ``civarchive:<modelId>@<versionId>`` refs via HTML scrape.

    CivArchive publishes no JSON metadata API; this Source fetches the
    canonical model-version HTML page and parses the SHA256 + filename out
    of structural anchors. ``Artifact.url`` is kept at
    ``civarchive.com/api/download/models/<vid>`` so CivArchive's own 307
    redirect chain owns host indirection (preserving cache validity across
    re-mirror events).

    The optional ``fetch`` parameter injects the HTTP transport. Tests pass
    a spy that returns canned HTML; the default ``_urllib_fetch_html`` uses
    ``urllib.request`` from the stdlib.

    Bare refs (``civarchive:N`` with no ``@<versionId>``) are
    parse-accepted (``handles`` returns True) but rejected at resolve with
    a clear error — symmetric with the URL-normalisation layer from
    sub-project A.

    Attributes:
        scheme: Registry scheme key — ``"civarchive"``.
    """

    scheme = "civarchive"

    def __init__(
        self,
        *,
        fetch: FetchHTMLCallable = _urllib_fetch_html,
    ) -> None:
        """Initialise the source with an optional transport override.

        Args:
            fetch: Callable ``(url, headers) -> str`` used to perform HTTP
                requests. Defaults to :func:`_urllib_fetch_html`.
        """
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        """Return True when *ref* matches ``civarchive:<digits>[@<digits>]``.

        Args:
            ref: The model reference string to test.

        Returns:
            True if *ref* is a well-formed civarchive ref.
        """
        return _REF_RE.match(ref) is not None

    def resolve(
        self, ref: str, creds: CredentialProvider
    ) -> list[Artifact]:
        """Resolve a CivArchive ref to one :class:`Artifact`.

        Args:
            ref: The model reference string (e.g.
                ``"civarchive:2197303@2474081"``).
            creds: Credential provider; ``CIVITAI_TOKEN`` (if present) is
                copied into ``Artifact.headers`` for the eventual download.

        Returns:
            A single-element list containing the resolved
            :class:`Artifact`.

        Raises:
            ValueError: *ref* does not match the civarchive regex.
            KinoforgeError: *ref* is a bare ``civarchive:N`` (no
                ``@<versionId>``), or the upstream HTTP fetch failed, or
                the HTML did not contain a recognised SHA256 / filename
                anchor.
            AuthError: The HTTP fetch returned 401 (unexpected for the
                anonymous HTML endpoint, retained for symmetry with the
                civitai source).
        """
        m = _REF_RE.match(ref)
        if m is None:
            raise ValueError(f"Not a valid civarchive ref: {ref!r}")

        model_id_str, version_id_str = m.group(1), m.group(2)
        if version_id_str is None:
            raise KinoforgeError(
                f"civarchive ref {ref!r} requires @<versionId>; "
                "civarchive does not expose a stable default-version "
                "selector"
            )

        # HTML fetch is anonymous — civarchive does not gate metadata pages.
        page_url = (
            f"https://civarchive.com/models/{model_id_str}"
            f"?modelVersionId={version_id_str}"
        )
        html = self._fetch(page_url, {})

        sha256 = _extract_sha256(html)
        filename = _extract_filename(html)

        # CIVITAI_TOKEN is attached to Artifact.headers — mirrors the
        # CivitAI source pattern so the downloader can authenticate when
        # CivArchive's 307 lands on civitai.com.
        token: str | None = creds.get("CIVITAI_TOKEN")
        headers: dict[str, str] = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )

        return [
            Artifact(
                filename=filename,
                url=(
                    f"https://civarchive.com/api/download/models/"
                    f"{version_id_str}"
                ),
                size=None,
                sha256=sha256,
                headers=headers,
            )
        ]
```

- [ ] **Step 4: Run tests — confirm GREEN**

Run: `pixi run pytest tests/sources/test_civarchive.py -v`
Expected: all tests PASS, including the fixture-replay equality assertion.

- [ ] **Step 5: Format + lint**

Run: `pixi run pre-commit run --files src/kinoforge/sources/civarchive/__init__.py tests/sources/test_civarchive.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/sources/civarchive/__init__.py tests/sources/test_civarchive.py
git commit -m "feat(civarchive): CivArchiveSource — handles + resolve from pinned HTML fixture"
```

---

## Task 4: Self-registration + adapter wire-up

**Goal:** Wire `CivArchiveSource` into the registry on module import, and add the import line to `_adapters.py` so `source_for_ref("civarchive:N@N")` returns it without an explicit registration call.

**Files:**
- Modify: `src/kinoforge/sources/civarchive/__init__.py` (add `registry.register_source` call at module bottom)
- Modify: `src/kinoforge/_adapters.py` (add `import kinoforge.sources.civarchive` line)
- Modify: `tests/sources/test_civarchive.py` (add self-registration test)

**Acceptance Criteria:**
- [ ] `import kinoforge.sources.civarchive` (without `_adapters` import) registers the source: `registry.source_for_ref("civarchive:1@2").scheme == "civarchive"`.
- [ ] After `import kinoforge._adapters`, `source_for_ref("civarchive:1@2").handles("civarchive:1@2")` is True.
- [ ] No regression: `pixi run pytest tests/sources/test_civitai.py tests/sources/test_huggingface.py tests/sources/test_civarchive.py -v` all PASS.

**Verify:** `pixi run pytest tests/sources/ -v` → all source tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing self-registration test**

Append to `tests/sources/test_civarchive.py`:

```python
# ---------------------------------------------------------------------------
# Self-registration on import
# ---------------------------------------------------------------------------

import importlib

import kinoforge.sources.civarchive  # noqa: F401 — populates registry on import
from kinoforge.core import registry


def test_self_registers_on_import() -> None:
    """Importing the module registers a CivArchiveSource."""
    importlib.reload(kinoforge.sources.civarchive)
    src = registry.source_for_ref("civarchive:1@2")
    # Bug this catches: self-registration being conditional on a flag, so
    # repeated imports leave the registry empty.
    assert src.scheme == "civarchive"
    assert src.handles("civarchive:1@2") is True


def test_adapter_module_imports_civarchive() -> None:
    """kinoforge._adapters imports the civarchive source."""
    # Bug this catches: forgetting to add the import line in _adapters.py,
    # leaving the kinoforge CLI without civarchive routing despite the
    # source module existing.
    import kinoforge._adapters  # noqa: F401

    src = registry.source_for_ref("civarchive:1@2")
    assert src.scheme == "civarchive"
```

- [ ] **Step 2: Run tests — confirm RED**

Run: `pixi run pytest tests/sources/test_civarchive.py -v -k "register"`
Expected: `UnknownAdapter: no model source handles ref: 'civarchive:1@2'`.

- [ ] **Step 3: Add the self-register call**

Append to `src/kinoforge/sources/civarchive/__init__.py`:

```python
from kinoforge.core import registry

# Self-register on import so a single ``import kinoforge.sources.civarchive``
# is enough for ``source_for_ref()`` to route CivArchive refs without an
# explicit register call. Mirrors the civitai pattern.
registry.register_source(CivArchiveSource())
```

- [ ] **Step 4: Wire into the adapter module**

Edit `src/kinoforge/_adapters.py` — add the import next to `kinoforge.sources.civitai`:

```python
# Sources
import kinoforge.sources.civarchive  # noqa: F401
import kinoforge.sources.civitai  # noqa: F401
import kinoforge.sources.http  # noqa: F401
import kinoforge.sources.huggingface  # noqa: F401
```

(The exact placement next to other source imports preserves alphabetical order; do not move other imports.)

- [ ] **Step 5: Run the full source-test suite**

Run: `pixi run pytest tests/sources/ -v`
Expected: every test in `test_civarchive.py`, `test_civitai.py`, `test_huggingface.py`, `test_http.py` PASS.

- [ ] **Step 6: Format + lint**

Run: `pixi run pre-commit run --files src/kinoforge/sources/civarchive/__init__.py src/kinoforge/_adapters.py tests/sources/test_civarchive.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/sources/civarchive/__init__.py \
        src/kinoforge/_adapters.py \
        tests/sources/test_civarchive.py
git commit -m "feat(civarchive): self-register source + wire into _adapters"
```

---

## Task 5: Live evidence smoke ($0, anonymous)

**Goal:** One live HTTP GET against `civarchive.com` to confirm the pinned fixture is faithful to the live page shape, with persistent evidence committed to `tests/live/evidence/2026-06-28-civarchive-source/`. No live file download — that's the downloader's separate concern.

**Files:**
- Create: `tests/live/evidence/2026-06-28-civarchive-source/resolve.py`
- Create: `tests/live/evidence/2026-06-28-civarchive-source/evidence.md`
- Create: `tests/live/evidence/2026-06-28-civarchive-source/response_meta.json`

**Acceptance Criteria:**
- [ ] `resolve.py` runs end-to-end with `pixi run python tests/live/evidence/2026-06-28-civarchive-source/resolve.py` (no flag, no env required beyond defaults).
- [ ] Script writes `response_meta.json` with at least: `{status: 200, content_type: "...", content_length: <int>, fetched_at: "<local-tz timestamp>"}`.
- [ ] Script writes `evidence.md` with: date, ref, fetched URL, the resolved `Artifact` repr (filename, url, sha256), and a `Verdict:` line that says `PASS` if the live sha256 + filename match the pinned fixture, `FIXTURE-DRIFT` otherwise.
- [ ] Live SHA256 observed equals `67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d` (the value pinned in the fixture).
- [ ] Live filename observed equals `wan2.2_t2v_arcanestyle_high.safetensors`.

**Verify:** `pixi run python tests/live/evidence/2026-06-28-civarchive-source/resolve.py` runs, exits 0, prints `Verdict: PASS`; the three evidence files exist after the run.

**Steps:**

- [ ] **Step 1: Write `resolve.py`**

```python
# tests/live/evidence/2026-06-28-civarchive-source/resolve.py
"""Live evidence smoke for CivArchiveSource (sub-project B).

One $0 anonymous HTTP GET against civarchive.com. Confirms the pinned
HTML fixture is faithful to the live page shape on the smoke date.

Run from repo root:

    pixi run python tests/live/evidence/2026-06-28-civarchive-source/resolve.py

Writes three files alongside this script:
    response_meta.json  — HTTP status + response headers + timestamp
    evidence.md         — human-readable summary + verdict
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from datetime import datetime
from urllib.request import Request, urlopen

# Project root on path so the kinoforge package resolves regardless of cwd.
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[3]))  # repo root

from kinoforge.core.credentials import EnvCredentialProvider  # noqa: E402
from kinoforge.sources.civarchive import CivArchiveSource  # noqa: E402

REF = "civarchive:2197303@2474081"
PAGE_URL = "https://civarchive.com/models/2197303?modelVersionId=2474081"
EXPECTED_SHA256 = "67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d"
EXPECTED_FILENAME = "wan2.2_t2v_arcanestyle_high.safetensors"


def main() -> int:
    fetched_at = datetime.now().isoformat(timespec="seconds")

    # 1) Live HTML fetch (captured for response_meta).
    req = Request(PAGE_URL, headers={"User-Agent": "kinoforge-smoke/0.1"})
    with urlopen(req) as resp:  # noqa: S310 — public civarchive URL
        status = resp.status
        content_type = resp.headers.get("Content-Type", "")
        body = resp.read()
    content_length = len(body)

    # 2) Resolve via the production CivArchiveSource (with default fetch).
    src = CivArchiveSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(REF, creds)
    assert len(artifacts) == 1
    artifact = artifacts[0]

    # 3) Verdict — compare live values against pinned fixture expectations.
    pass_sha = artifact.sha256 == EXPECTED_SHA256
    pass_name = artifact.filename == EXPECTED_FILENAME
    verdict = "PASS" if pass_sha and pass_name else "FIXTURE-DRIFT"

    # 4) Persist response_meta.json.
    (HERE / "response_meta.json").write_text(
        json.dumps(
            {
                "status": status,
                "content_type": content_type,
                "content_length": content_length,
                "fetched_at": fetched_at,
            },
            indent=2,
        )
        + "\n"
    )

    # 5) Persist evidence.md.
    art_repr = json.dumps(dataclasses.asdict(artifact), indent=2)
    (HERE / "evidence.md").write_text(
        f"""# CivArchive source — live evidence

**Date:** {fetched_at}
**Ref:** `{REF}`
**Fetched URL:** `{PAGE_URL}`
**HTTP status:** {status}
**Response Content-Type:** `{content_type}`
**Body length:** {content_length} bytes

## Resolved Artifact

```json
{art_repr}
```

## Verdict

**{verdict}**

- sha256 == pinned fixture: {pass_sha}
- filename == pinned fixture: {pass_name}

Pinned fixture: `tests/sources/civarchive/fixtures/version_2474081.html`.
If verdict is `FIXTURE-DRIFT`, the fixture is stale: refresh it and
re-run unit tests before relying on this evidence.
"""
    )

    print(f"Verdict: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Pre-flight — commit the scaffold BEFORE running the live spend**

Per project CLAUDE.md durability rules ("Commit RED scaffolds before any live spend"), commit `resolve.py` BEFORE invoking it, even though this is a $0 anonymous HTTP smoke. Discipline applies regardless of spend size.

```bash
git add tests/live/evidence/2026-06-28-civarchive-source/resolve.py
git commit -m "test(live): RED scaffold for civarchive source resolve smoke"
```

- [ ] **Step 3: Run the live smoke**

```bash
pixi run python tests/live/evidence/2026-06-28-civarchive-source/resolve.py
```

Expected stdout: `Verdict: PASS`
Exit code: 0
Files written: `response_meta.json`, `evidence.md` alongside `resolve.py`.

If verdict is `FIXTURE-DRIFT`, civarchive's HTML has changed since the Task 1 fixture pin — STOP, refresh the fixture (Task 1 step 2), re-run Task 3's unit tests, and retry the smoke. Do not commit drift evidence as PASS.

- [ ] **Step 4: Commit the evidence**

```bash
git add tests/live/evidence/2026-06-28-civarchive-source/evidence.md \
        tests/live/evidence/2026-06-28-civarchive-source/response_meta.json
git commit -m "test(live): GREEN evidence — civarchive source resolves 2197303@2474081"
```

---

## Task 6: Documentation updates + workstream close

**Goal:** Replace the "not yet implemented" stubs in `docs/warm-reuse.md` with confirmation that civarchive refs resolve. Close the sub-project B workstream block in `PROGRESS.md`. Drop the top-priority pointer block that pinned sub-project B as the next initiative.

**Files:**
- Modify: `docs/warm-reuse.md` (lines 146–149 area — replace the stub)
- Modify: `PROGRESS.md` (close workstream block; drop the pointer at lines 45–77; update active workstream line)

**Acceptance Criteria:**
- [ ] `docs/warm-reuse.md` no longer contains the phrase "civarchive source not yet implemented".
- [ ] `docs/warm-reuse.md` mentions that civarchive refs resolve via HTML scrape with sha256 integrity verification.
- [ ] `PROGRESS.md` has a closed "civarchive source (Sub-project B) SHIPPED" block in the same shape as the existing "LoRA URL normalization (Sub-project A) SHIPPED" block (around line 20).
- [ ] `PROGRESS.md` no longer contains the "Sub-project B — civarchive source module" pointer at lines 45–77.
- [ ] `PROGRESS.md` "Active workstream" line is updated (either "No active workstream — next initiative TBD" or pointer to whatever the operator chooses).

**Verify:** `rg -n "civarchive source not yet implemented" docs/ PROGRESS.md` returns no matches; `rg -n "Sub-project B" PROGRESS.md` shows a SHIPPED block (not a pointer block).

**Steps:**

- [ ] **Step 1: Replace the stub in `docs/warm-reuse.md`**

Find the current block at lines 146–149 of `docs/warm-reuse.md`:

```
`civarchive:<id>@<vid>` is parse-accepted by this release but the
[...]. Until that ships, civarchive refs
will fail at resolution time with a clear "civarchive source not yet
implemented" error.
```

Replace with:

```
`civarchive:<id>@<vid>` resolves via an HTML scrape of the civarchive
model-version page. SHA256 integrity is verified post-download against
the hash captured from the page's `/sha256/` anchor. CivArchive's
`/api/download/models/<vid>` endpoint 307-redirects to whichever host
currently mirrors the file (civitai.com, HuggingFace, or CivArchive's
own `/sha256/` store) — kinoforge keeps `Artifact.url` at the abstract
CivArchive endpoint so persisted refs remain valid across re-mirror
events.
```

(Adjust line wrapping to match the surrounding doc's prevailing column width if different.)

- [ ] **Step 2: Update `PROGRESS.md`**

Two surgical edits:

1. **Add a SHIPPED block** at the same level as the existing "LoRA URL normalization (Sub-project A) SHIPPED" block. The new block names the sub-project, the commit range, the design doc + plan paths, and the per-task ship line (one bullet per Task 0–5; Task 6 is the docs change being written right now, mention it inline). Mirror the prose voice of the existing sub-project A block.

2. **Remove the pointer block** at the current lines 45–77 (the entire "NEXT SESSION — TOP PRIORITY" / "Sub-project B — civarchive source module" stanza). Replace with whatever is appropriate for the post-ship state — usually a single line restoring "No active workstream — next initiative TBD" or pointing at whatever the operator wants next.

3. **Update the "Active workstream" line** near the top so it reflects post-ship state.

Pre-edit, read both blocks via `Read` so the byte-exact strings line up:

```bash
pixi run python -c "
import pathlib
p = pathlib.Path('PROGRESS.md').read_text()
print('--- lines 13-20:')
print('\n'.join(p.splitlines()[12:20]))
print('--- lines 45-77:')
print('\n'.join(p.splitlines()[44:77]))
"
```

Then Edit() each region.

- [ ] **Step 3: Verify the doc invariants**

```bash
# No stale "not yet implemented" copy anywhere.
rg -n "civarchive source not yet implemented" docs/ PROGRESS.md && exit 1 || echo "stub gone"

# A SHIPPED block exists for sub-project B.
rg -n "Sub-project B.*SHIPPED|civarchive.*SHIPPED" PROGRESS.md
```

Expected: "stub gone" line printed; SHIPPED block grep matches.

- [ ] **Step 4: Format + lint**

Run: `pixi run pre-commit run --files docs/warm-reuse.md PROGRESS.md`
Expected: PASS (markdown changes only).

- [ ] **Step 5: Commit docs**

```bash
git add docs/warm-reuse.md
git commit -m "docs(civarchive): replace 'not yet implemented' stub with HTML-scrape resolver note"

git add PROGRESS.md
git commit -m "docs(progress): SHIPPED — civarchive source module (sub-project B)"
```

(Two commits so the warm-reuse doc edit and the PROGRESS update remain individually revertable.)

---

## Self-review

**Spec coverage** — checked every section of `docs/superpowers/specs/2026-06-28-civarchive-source-design.md` against tasks:

| Spec section | Implementing task(s) |
|---|---|
| Module layout | Task 0 (helpers + module init) + Task 1 (fixture dir) |
| Public interface (`CivArchiveSource`) | Task 3 |
| Resolve flow (with bare-ref reject + auth header) | Task 3 |
| HTML parser helpers (sha256 + filename regex) | Task 0 |
| Error handling matrix | Task 0 (extractors), Task 2 (transport), Task 3 (resolve dispatch) |
| Tests (every row of the unit-test table) | Task 0 + Task 2 + Task 3 + Task 4 |
| Live evidence | Task 5 |
| Integration touch-points (`_adapters.py`, docs, PROGRESS) | Task 4 + Task 6 |
| Out of scope | not implemented (correctly) |

No gaps.

**Placeholder scan** — no TBD / TODO / "implement later" strings. Every code block is complete. Every regex pattern is the exact production pattern.

**Type consistency** — symbol names align across tasks: `_REF_RE`, `_SHA256_HREF_RE`, `_FILENAME_RE`, `_urllib_fetch_html`, `_extract_sha256`, `_extract_filename`, `FetchHTMLCallable`, `CivArchiveSource`. The `Artifact` constructor calls in tests vs implementation use the same kwargs (`filename`, `url`, `size`, `sha256`, `headers`).
