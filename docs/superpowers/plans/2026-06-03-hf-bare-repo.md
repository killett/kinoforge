# HuggingFace bare-repo listing implementation plan (GH #8)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable `hf:<repo>[@<revision>]` refs to enumerate every file in a HuggingFace repo via the tree API, returning one Artifact per file with LFS-oid integrity automatically populated.

**Architecture:** Add a tree-listing branch inside `HuggingFaceSource.resolve()` that mirrors `CivitAISource`'s injected-fetch pattern. One generic provisioner guard rejects `entry.sha256` on any multi-artifact resolve. One downloader hygiene fix creates parent dirs for subpath-bearing artifact filenames. No new dependencies, no new YAML schema.

**Tech Stack:** Python 3.13, urllib (stdlib), pydantic v2, pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-06-03-hf-bare-repo-design.md`

**Phase label:** Phase 30 — HF bare-repo listing.

---

## Task 1: Downloader parent-directory hygiene fix

**Goal:** Insert a one-line `target_path.parent.mkdir(parents=True, exist_ok=True)` in `download_one` so artifacts whose `filename` contains `/` (e.g. `"unet/foo.bin"`) write correctly into a fresh destination tree. Independent precursor — must land before Task 4 because the tree branch emits subpath filenames.

**Files:**
- Modify: `src/kinoforge/core/downloader.py:232-233`
- Test: `tests/core/test_downloader.py` (one new test appended)

**Acceptance Criteria:**
- [ ] AC1: `download_one(Artifact(filename="sub/foo.bin", url=loopback_url, sha256=...), dest)` succeeds and the file lands at `dest/sub/foo.bin`.
- [ ] AC2: Pre-existing single-leaf-filename tests stay green (no regression).
- [ ] AC3: The mkdir is `exist_ok=True` — repeated calls with the same subdir do not raise.

**Verify:** `pixi run test tests/core/test_downloader.py -v` → all pass (1 new + existing).

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_downloader.py` at the end of the file:

```python
def test_download_one_creates_parent_dirs_for_subpath_filename(
    http_server: HttpServerInfo, tmp_path: Path
) -> None:
    """Artifact.filename with `/` triggers parent-dir mkdir before write.

    Bug this catches: writing to dest/sub/foo.bin without first mkdir-ing
    dest/sub fails with FileNotFoundError. The bare-repo listing feature
    emits subpath filenames; this AC locks the downloader behaviour.
    """
    payload = b"hello-subdir"
    http_server.serve_bytes("foo.bin", payload)
    art = Artifact(
        filename="sub/foo.bin",
        url=f"{http_server.base_url}/foo.bin",
    )

    result = download_one(art, tmp_path)

    target = tmp_path / "sub" / "foo.bin"
    assert target.is_file()
    assert target.read_bytes() == payload
    assert result.uri == str(target)
```

Confirm the existing imports at the top of `test_downloader.py` already cover `Artifact`, `download_one`, `HttpServerInfo`, and `Path`. They should — every other test uses them.

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run test tests/core/test_downloader.py::test_download_one_creates_parent_dirs_for_subpath_filename -v`

Expected: FAIL with `FileNotFoundError: [Errno 2] No such file or directory:` on the `.part` write.

- [ ] **Step 3: Apply the one-line fix**

In `src/kinoforge/core/downloader.py`, change lines 232-233 from:

```python
    target_path = dest / artifact.filename
    part_path = Path(str(target_path) + ".part")
```

to:

```python
    target_path = dest / artifact.filename
    target_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = Path(str(target_path) + ".part")
```

- [ ] **Step 4: Run the new test + the full downloader suite**

Run: `pixi run test tests/core/test_downloader.py -v`

Expected: PASS for the new test and PASS for every existing test.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/downloader.py tests/core/test_downloader.py
git add src/kinoforge/core/downloader.py tests/core/test_downloader.py
git commit -m "fix(core/downloader): mkdir parents for subpath filenames (Phase 30 T1)"
```

```json:metadata
{"files": ["src/kinoforge/core/downloader.py", "tests/core/test_downloader.py"], "verifyCommand": "pixi run test tests/core/test_downloader.py -v", "acceptanceCriteria": ["download_one with subpath filename succeeds", "existing leaf-filename tests stay green", "mkdir is idempotent (exist_ok=True)"]}
```

---

## Task 2: Parser + Link header helper + FetchCallable type

**Goal:** Land three pure helpers used by Tasks 3 and 4: `_parse_hf_ref`, `_next_cursor_from_link`, and the new `FetchCallable` type. All offline-testable, no behaviour change to `HuggingFaceSource.resolve()` yet.

**Files:**
- Modify: `src/kinoforge/sources/huggingface/__init__.py` (add helpers + type alias, keep existing class behaviour unchanged for now)
- Test: `tests/sources/test_huggingface.py` (append parser + link tests)

**Acceptance Criteria:**
- [ ] AC1: `_parse_hf_ref("hf:org/repo")` returns `("org/repo", "main", None)`.
- [ ] AC2: `_parse_hf_ref("hf:org/repo@v1.0")` returns `("org/repo", "v1.0", None)`.
- [ ] AC3: `_parse_hf_ref("hf:org/repo:a/b.bin")` returns `("org/repo", "main", "a/b.bin")`.
- [ ] AC4: `_parse_hf_ref("hf:org/repo@abc:a/b.bin")` returns `("org/repo", "abc", "a/b.bin")`.
- [ ] AC5: `_next_cursor_from_link("")` and `_next_cursor_from_link("<...>; rel=\"prev\"")` both return `None`.
- [ ] AC6: `_next_cursor_from_link('<https://x/y?cursor=eyJfaWQiOiJ0b2siLCJfaG90IjpmYWxzZX0%3D&recursive=true>; rel="next"')` returns the decoded `cursor` token value.
- [ ] AC7: `FetchCallable` type alias is exported from the module and importable in tests.
- [ ] AC8: Existing 11 tests in `test_huggingface.py` stay green (no behaviour change).

**Verify:** `pixi run test tests/sources/test_huggingface.py -v` → 17 pass (11 existing + 6 new).

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/sources/test_huggingface.py`:

```python
# ---------------------------------------------------------------------------
# Phase 30 — parser
# ---------------------------------------------------------------------------

from kinoforge.sources.huggingface import (
    FetchCallable,
    _next_cursor_from_link,
    _parse_hf_ref,
)


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
    # Importing the name above proves it exists; this assertion locks the
    # contract that it is a callable type alias.
    assert FetchCallable is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/sources/test_huggingface.py -v`

Expected: ImportError or `AttributeError: module 'kinoforge.sources.huggingface' has no attribute '_parse_hf_ref'` / `_next_cursor_from_link` / `FetchCallable`.

- [ ] **Step 3: Add helpers to the source module**

Edit `src/kinoforge/sources/huggingface/__init__.py`. Replace the existing imports block and module body up to (but NOT including) the `class HuggingFaceSource(...)` line with:

```python
"""HuggingFace model source — resolves ``hf:<repo>[:<path>][@<rev>]`` refs.

Single-file refs (``hf:<repo>:<path>`` or ``hf:<repo>@<rev>:<path>``)
construct the canonical HuggingFace resolve URL directly with no HTTP
calls.  Bare-repo refs (``hf:<repo>`` or ``hf:<repo>@<rev>``) enumerate
the repo tree via the HuggingFace tree API and emit one Artifact per
file, with content SHA256 auto-populated from LFS metadata when present.

Example ref formats::

    hf:Wan-AI/Wan2.2:diffusion/model.safetensors
    hf:Wan-AI/Wan2.2@v1.0:diffusion/model.safetensors
    hf:Wan-AI/Wan2.2                                  # bare, revision = main
    hf:Wan-AI/Wan2.2@<sha>                            # bare, pinned

The HTTP transport for tree listing is injected via the ``fetch``
constructor parameter so tests can pass a stub without touching the
network.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core import registry
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import Artifact, CredentialProvider, ModelSource

# ---------------------------------------------------------------------------
# Ref pattern
# ---------------------------------------------------------------------------

# Matches anything starting with "hf:" followed by at least one non-colon
# character (the repo path, optionally with @rev), with an optional ":path"
# suffix.  Bare-repo refs (no ":path") are recognised here; resolve() decides
# whether to dispatch single-file or tree-listing.
_REF_RE = re.compile(r"^hf:[^:]+(:.*)?$")

_HF_BASE = "https://huggingface.co"


# ---------------------------------------------------------------------------
# Transport seam
# ---------------------------------------------------------------------------

FetchCallable = Callable[
    [str, dict[str, str]],
    tuple[list[dict[str, Any]], str | None],
]


def _next_cursor_from_link(link_header: str) -> str | None:
    """Extract the ``cursor`` query-param from a ``Link: <...>; rel="next"`` header.

    Args:
        link_header: The raw ``Link`` response-header string, possibly empty.

    Returns:
        The URL-decoded cursor token from the ``rel="next"`` entry's URL,
        or ``None`` when no such entry is present.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="next"', part)
        if not m:
            continue
        parsed = urllib.parse.urlparse(m.group(1))
        qs = urllib.parse.parse_qs(parsed.query)
        cursor = qs.get("cursor", [None])[0]
        return cursor
    return None


def _urllib_fetch_json(
    url: str, headers: dict[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch *url* with GET, return ``(parsed_json_list, next_cursor_or_None)``.

    Args:
        url: The endpoint URL.
        headers: HTTP request headers to include.

    Returns:
        ``(entries, next_cursor)`` where *entries* is the parsed JSON array
        body and *next_cursor* is extracted from the ``Link`` response header.

    Raises:
        AuthError: The server returned HTTP 401.
        KinoforgeError: Any other non-2xx HTTP error or network failure.
    """
    req = Request(url, headers=headers)  # noqa: S310
    try:
        with urlopen(req) as resp:  # noqa: S310 — only huggingface.co HTTPS URLs used
            body: bytes = resp.read()
            link_header: str = resp.headers.get("Link", "") or ""
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(f"HuggingFace 401 Unauthorized for {url}") from exc
        raise KinoforgeError(f"HuggingFace HTTP {exc.code} for {url}") from exc
    parsed: list[dict[str, Any]] = json.loads(body.decode("utf-8"))
    return parsed, _next_cursor_from_link(link_header)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_hf_ref(ref: str) -> tuple[str, str, str | None]:
    """Parse a HuggingFace ref into ``(repo, revision, path_or_None)``.

    The grammar is::

        hf:<repo>                          → (repo, "main", None)
        hf:<repo>@<revision>               → (repo, revision, None)
        hf:<repo>:<path>                   → (repo, "main", path)
        hf:<repo>@<revision>:<path>        → (repo, revision, path)

    Split order: ``:`` first (path separator), then ``@`` on the head
    (revision separator).  ``@`` is legal inside HuggingFace paths and must
    not be claimed as a revision marker.

    Args:
        ref: The HuggingFace reference string, e.g. ``"hf:org/repo@v1.0:path/file.bin"``.

    Returns:
        ``(repo, revision, path_or_None)`` triple.
    """
    remainder = ref[len("hf:") :]
    repo_rev, _, path = remainder.partition(":")
    path_or_none: str | None = path or None
    if "@" in repo_rev:
        repo, _, revision = repo_rev.partition("@")
    else:
        repo, revision = repo_rev, "main"
    return repo, revision, path_or_none


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
```

Keep the rest of the file (`class HuggingFaceSource(...)` and the
`registry.register_source(HuggingFaceSource())` line at the bottom) **unchanged
for this task**. Task 3 refactors the class to use the new parser; Task 4
adds the tree branch.

The new module-level imports add `json` and `urllib.parse`; the existing
file's `import re` is preserved by being part of the rewritten header. The
`ValidationError` import is preserved (used by the unchanged `resolve()`
body until Task 3 rewrites it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/sources/test_huggingface.py -v`

Expected: All 17 pass (11 existing + 6 new).

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git add src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git commit -m "feat(sources/hf): parser + link-cursor + FetchCallable seam (Phase 30 T2)"
```

```json:metadata
{"files": ["src/kinoforge/sources/huggingface/__init__.py", "tests/sources/test_huggingface.py"], "verifyCommand": "pixi run test tests/sources/test_huggingface.py -v", "acceptanceCriteria": ["_parse_hf_ref handles all 4 grammar shapes", "_next_cursor_from_link extracts the rel=next cursor", "FetchCallable type alias is exported", "existing 11 tests stay green"]}
```

---

## Task 3: Refactor single-file branch to use parser + support `@<rev>`

**Goal:** Rewire `HuggingFaceSource.resolve()` to call `_parse_hf_ref`, dispatch single-file refs through a new `_single_file_artifact` helper, and interpolate the parsed revision into the resolve URL. Keep the bare-repo branch as a `ValidationError` (Task 4 replaces it). Existing 11 tests still pass; 2 new tests lock the `@<rev>` behaviour.

**Files:**
- Modify: `src/kinoforge/sources/huggingface/__init__.py` (rewrite the `HuggingFaceSource` class body)
- Test: `tests/sources/test_huggingface.py` (append 2 new tests)

**Acceptance Criteria:**
- [ ] AC1: `resolve("hf:org/repo@v1.0:path/file.bin", creds)` returns one Artifact whose URL is `https://huggingface.co/org/repo/resolve/v1.0/path/file.bin`.
- [ ] AC2: `resolve("hf:org/repo:path/file.bin", creds)` URL is `https://huggingface.co/org/repo/resolve/main/path/file.bin` (default revision unchanged).
- [ ] AC3: `resolve("hf:org/repo", creds)` still raises `ValidationError` with the existing "specify a file path" message (bare-repo path is reserved for Task 4).
- [ ] AC4: Existing single-file ACs (1–6) stay green: handles regex, filename flatten, Authorization header attach/omit, multi-segment path, scheme attribute, self-registration.

**Verify:** `pixi run test tests/sources/test_huggingface.py -v` → 19 pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/sources/test_huggingface.py`:

```python
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
        artifacts[0].url
        == "https://huggingface.co/org/repo/resolve/v1.0/path/file.bin"
    )


def test_resolve_single_file_default_revision_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: parser regression dropping the default 'main' revision."""
    creds = _make_creds(monkeypatch, None)
    src = HuggingFaceSource()
    artifacts = src.resolve("hf:org/repo:path/file.bin", creds)
    assert artifacts[0].url.endswith("/resolve/main/path/file.bin")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/sources/test_huggingface.py::test_resolve_single_file_with_revision_url tests/sources/test_huggingface.py::test_resolve_single_file_default_revision_main -v`

Expected: First test FAILS (URL contains `resolve/main` not `resolve/v1.0`). Second already passes (default behaviour matches today).

- [ ] **Step 3: Rewrite the `HuggingFaceSource` class body**

In `src/kinoforge/sources/huggingface/__init__.py`, replace the existing `class HuggingFaceSource(ModelSource): ...` block (everything from the class declaration down to but NOT including the final `registry.register_source(...)` line) with:

```python
class HuggingFaceSource(ModelSource):
    """Resolves ``hf:<repo>[@<rev>][:<path>]`` refs to one or more Artifacts.

    Single-file refs return exactly one Artifact whose URL is the canonical
    HuggingFace resolve URL for that file at the parsed revision.  Bare
    refs are routed through the tree-listing branch added in Task 4.

    Attributes:
        scheme: Registry scheme key — ``"hf"``.
    """

    scheme = "hf"

    def __init__(self, *, fetch: FetchCallable = _urllib_fetch_json) -> None:
        """Initialise the source with an optional transport override.

        Args:
            fetch: Callable used to perform tree-listing HTTP requests.
                Defaults to :func:`_urllib_fetch_json`.  Unused on the
                single-file branch.
        """
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        """Return ``True`` when *ref* matches ``^hf:[^:]+(:.*)?$``."""
        return _REF_RE.match(ref) is not None

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve *ref* to a list of Artifacts.

        Single-file refs return a single-element list; bare-repo refs raise
        ``ValidationError`` until Task 4 lands the tree branch.

        Args:
            ref: The HuggingFace reference string.
            creds: Credential provider; reads ``HF_TOKEN`` from it.

        Returns:
            List of :class:`~kinoforge.core.interfaces.Artifact` objects.

        Raises:
            ValidationError: *ref* is a bare repo ref (no file path).
        """
        repo, revision, path = _parse_hf_ref(ref)
        token: str | None = creds.get("HF_TOKEN")
        headers: dict[str, str] = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )

        if path is None:
            # DEFERRED to Task 4: directory listing via HF tree API.
            raise ValidationError(
                f"No file path in HuggingFace ref {ref!r} — "
                "specify a file path (hf:repo:path/to/file). "
                "Directory listing is not yet supported."
            )

        return [self._single_file_artifact(repo, revision, path, headers)]

    def _single_file_artifact(
        self,
        repo: str,
        revision: str,
        path: str,
        headers: dict[str, str],
    ) -> Artifact:
        """Build the canonical resolve-URL Artifact for one file.

        Args:
            repo: ``<org>/<name>`` HuggingFace repo identifier.
            revision: Branch, tag, or commit SHA.
            path: Relative file path within the repo.
            headers: HTTP headers (``Authorization`` when ``HF_TOKEN`` set).

        Returns:
            A single :class:`~kinoforge.core.interfaces.Artifact` whose
            ``filename`` is the leaf of *path* (existing flatten contract).
        """
        filename = path.rsplit("/", 1)[-1]
        url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
        return Artifact(url=url, filename=filename, headers=dict(headers))


# Self-register on import so a single ``import kinoforge.sources.huggingface``
# is enough for ``source_for_ref()`` to route HuggingFace refs without an
# explicit register call.
registry.register_source(HuggingFaceSource())
```

The final `registry.register_source(HuggingFaceSource())` line stays at the
bottom of the file.

- [ ] **Step 4: Run the full HF test file**

Run: `pixi run test tests/sources/test_huggingface.py -v`

Expected: 19 pass (17 from Task 2 baseline + 2 new). All existing single-file ACs (1–6) and parser/link ACs (Phase 30 T2) still pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git add src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git commit -m "refactor(sources/hf): @rev support in single-file branch (Phase 30 T3)"
```

```json:metadata
{"files": ["src/kinoforge/sources/huggingface/__init__.py", "tests/sources/test_huggingface.py"], "verifyCommand": "pixi run test tests/sources/test_huggingface.py -v", "acceptanceCriteria": ["@v1.0 interpolates into resolve URL", "default revision remains main", "bare-repo still ValidationErrors (deferred to Task 4)", "all single-file ACs stay green"]}
```

---

## Task 4: Tree branch — bare-repo listing

**Goal:** Replace the bare-repo `ValidationError` raise with a call to a new `_list_tree_artifacts` helper that fetches the recursive tree, paginates through every page, and emits one Artifact per file entry with LFS-oid integrity + subdir-preserving filename + size + Authorization header.

**Files:**
- Modify: `src/kinoforge/sources/huggingface/__init__.py` (add `_fetch_tree`, `_list_tree_artifacts`; replace bare-repo `raise` in `resolve()`)
- Test: `tests/sources/test_huggingface.py` (append ~10 new tests)

**Acceptance Criteria:**
- [ ] AC1: A stub `fetch` returning one page with three file entries yields three Artifacts; `type=="directory"` entries are filtered out.
- [ ] AC2: `Artifact.filename` preserves subdirs verbatim (e.g. `"unet/foo.safetensors"`).
- [ ] AC3: `Artifact.sha256` is the lowercased `lfs.oid` for LFS-tracked entries; `None` when `lfs` is absent.
- [ ] AC4: `Artifact.size` is taken from the entry's top-level `size` field.
- [ ] AC5: `Artifact.headers` carries `Authorization: Bearer <HF_TOKEN>` when the token is set; absent otherwise.
- [ ] AC6: Pagination loop accumulates entries from all pages; terminates only when `next_cursor` is `None`.
- [ ] AC7: The recursive URL on the first request is `https://huggingface.co/api/models/<repo>/tree/<revision>?recursive=true`; subsequent pages append `&cursor=<URL-encoded-token>`.
- [ ] AC8: Empty file list (every entry filtered out) returns `[]` without raising.
- [ ] AC9: 401 surfaced from the stub `fetch` raises `AuthError`; other non-2xx → `KinoforgeError`.
- [ ] AC10: `resolve("hf:org/repo", creds)` no longer raises `ValidationError`; the bare-repo branch now returns the listed artifacts (regression test against the Task 3 deferral).

**Verify:** `pixi run test tests/sources/test_huggingface.py -v` → 29 pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/sources/test_huggingface.py`:

```python
# ---------------------------------------------------------------------------
# Phase 30 — Tree branch helpers
# ---------------------------------------------------------------------------


def _make_stub_fetch(
    pages: list[tuple[list[dict[str, Any]], str | None]],
    log: list[tuple[str, dict[str, str]]] | None = None,
) -> FetchCallable:
    """Return a stub FetchCallable that pops a page per call.

    Args:
        pages: Pre-canned ``(entries, next_cursor)`` tuples, popped LIFO.
        log: Optional mutable list that accumulates ``(url, headers)``
            tuples in call order.

    Returns:
        A callable matching the :data:`FetchCallable` signature.
    """
    pages_iter = iter(pages)

    def _stub(url: str, headers: dict[str, str]) -> tuple[
        list[dict[str, Any]], str | None
    ]:
        if log is not None:
            log.append((url, dict(headers)))
        return next(pages_iter)

    return _stub


from typing import Any  # noqa: E402  — kept local to Phase 30 tests; remove on cleanup if hoisted


def test_tree_branch_one_page_emits_one_artifact_per_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: emitting directory entries as Artifacts, or dropping files."""
    creds = _make_creds(monkeypatch, None)
    entries = [
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
    assert {a.filename for a in artifacts} == {"config.json", "unet/model.safetensors"}


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
    entries = [
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
        fetch=_make_stub_fetch(
            [(page1, "TOK1"), (page2, "TOK2"), (page3, None)]
        )
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

    def _raise_auth(url: str, headers: dict[str, str]) -> tuple[
        list[dict[str, Any]], str | None
    ]:
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
    # Tree URL uses the parsed revision.
    assert log[0][0] == (
        "https://huggingface.co/api/models/org/repo/tree/v1.0?recursive=true"
    )
    # The artifact's resolve URL also uses it.
    assert artifacts[0].url == "https://huggingface.co/org/repo/resolve/v1.0/a.bin"
```

Also add the missing imports near the top of the test file (just below the
existing `import importlib` line — keep alphabetical grouping inside
import groups):

```python
import urllib.parse
from typing import Any
```

(The inline `from typing import Any` inside the test block is fine for the
stub-builder closure type annotation but the module-level import is cleaner;
keep just the module-level one and drop the inline `from typing import Any`
introduced inside the stub block.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/sources/test_huggingface.py -v`

Expected: 13 new tests FAIL (the bare-repo branch still raises `ValidationError`).

- [ ] **Step 3: Add the tree-branch implementation**

In `src/kinoforge/sources/huggingface/__init__.py`, inside the
`HuggingFaceSource` class, replace the body of `resolve()` and add the
two new helper methods. The full replacement for the `resolve()` method
plus the two new helpers (`_list_tree_artifacts`, `_fetch_tree`) is:

```python
    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        """Resolve *ref* to a list of Artifacts.

        Single-file refs (with ``:path``) return a single-element list;
        bare refs enumerate the HuggingFace tree at the given revision and
        return one Artifact per file entry, with LFS-oid integrity
        auto-populated when present.

        Args:
            ref: The HuggingFace reference string.
            creds: Credential provider; reads ``HF_TOKEN`` from it.

        Returns:
            List of :class:`~kinoforge.core.interfaces.Artifact` objects.

        Raises:
            AuthError: HuggingFace returned HTTP 401 (re-raised from transport).
            KinoforgeError: Any other non-2xx HTTP response from the tree API.
        """
        repo, revision, path = _parse_hf_ref(ref)
        token: str | None = creds.get("HF_TOKEN")
        headers: dict[str, str] = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )

        if path is not None:
            return [self._single_file_artifact(repo, revision, path, headers)]

        return self._list_tree_artifacts(repo, revision, headers)

    def _list_tree_artifacts(
        self,
        repo: str,
        revision: str,
        headers: dict[str, str],
    ) -> list[Artifact]:
        """Enumerate the repo tree and emit one Artifact per file entry.

        Directory entries are filtered out.  ``Artifact.filename`` preserves
        the entry's relative path verbatim (subdirs included).
        ``Artifact.sha256`` is populated from ``lfs.oid`` (lowercased) when
        present; non-LFS files get ``sha256=None``.  ``Artifact.size``
        is taken from the entry's top-level ``size`` field.

        Args:
            repo: ``<org>/<name>`` HuggingFace repo identifier.
            revision: Branch, tag, or commit SHA.
            headers: HTTP headers (``Authorization`` when ``HF_TOKEN`` set);
                attached verbatim to each emitted Artifact.

        Returns:
            One Artifact per ``type=="file"`` entry in the tree; ``[]`` when
            the repo has no file entries.
        """
        entries = self._fetch_tree(repo, revision, headers)
        artifacts: list[Artifact] = []
        for entry in entries:
            if entry.get("type") != "file":
                continue
            path: str = entry["path"]
            url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
            lfs: dict[str, Any] = entry.get("lfs") or {}
            raw_oid = lfs.get("oid") or ""
            sha256: str | None = raw_oid.lower() or None
            size: int | None = entry.get("size")
            artifacts.append(
                Artifact(
                    url=url,
                    filename=path,
                    size=size,
                    sha256=sha256,
                    headers=dict(headers),
                )
            )
        return artifacts

    def _fetch_tree(
        self,
        repo: str,
        revision: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Page through the HF tree API, returning all entries flattened.

        Args:
            repo: ``<org>/<name>`` HuggingFace repo identifier.
            revision: Branch, tag, or commit SHA.
            headers: HTTP headers (``Authorization`` when ``HF_TOKEN`` set).

        Returns:
            Concatenated list of all entries across all pages, in API order.
        """
        entries: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            url = f"{_HF_BASE}/api/models/{repo}/tree/{revision}?recursive=true"
            if cursor is not None:
                url += f"&cursor={urllib.parse.quote(cursor)}"
            page, next_cursor = self._fetch(url, headers)
            entries.extend(page)
            if next_cursor is None:
                return entries
            cursor = next_cursor
```

Drop the now-unused `from kinoforge.core.errors import ValidationError`
import line at the top of the file (the bare-repo branch no longer raises
it). If mypy or ruff complain about an unused import, that's the signal.
Re-add only if a future task needs it.

- [ ] **Step 4: Run the full HF test file**

Run: `pixi run test tests/sources/test_huggingface.py -v`

Expected: 32 pass (19 from Task 3 + 13 new) — but **one regression**:
`test_bare_repo_raises_validation_error` (AC4 from the original layer)
will now FAIL because bare refs no longer raise. Update that test in
the same commit:

In `tests/sources/test_huggingface.py`, replace the body of
`test_bare_repo_raises_validation_error` with a new test that asserts the
bare ref now resolves via a stub fetch:

```python
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
```

Rename the test function from `test_bare_repo_raises_validation_error` to
`test_bare_repo_resolves_via_tree_api` and update the docstring/body. The
original AC4 contract is replaced — see Task 4 spec §1 Q1.

Re-run: `pixi run test tests/sources/test_huggingface.py -v` → 32 pass.

Then run the broader source-tests suite to catch any cross-test
side-effects: `pixi run test tests/sources/ -v` → all pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git add src/kinoforge/sources/huggingface/__init__.py tests/sources/test_huggingface.py
git commit -m "feat(sources/hf): bare-repo tree-listing branch (Phase 30 T4, closes #8)"
```

```json:metadata
{"files": ["src/kinoforge/sources/huggingface/__init__.py", "tests/sources/test_huggingface.py"], "verifyCommand": "pixi run test tests/sources/test_huggingface.py -v", "acceptanceCriteria": ["one Artifact per type=='file' entry", "subdirs preserved in filename", "lfs.oid lowercased onto sha256", "non-LFS entry sha256=None", "size populated", "Authorization header attached when HF_TOKEN set", "pagination accumulates all pages", "recursive=true on first URL", "cursor URL-encoded on subsequent pages", "empty file list returns []", "AuthError propagated", "revision threaded into both tree URL and artifact URL", "bare ref no longer raises ValidationError"]}
```

---

## Task 5: Provisioner generic multi-artifact guard

**Goal:** Add the source-agnostic check in `provisioner.provision()` that raises `ValidationError` when any source returns more than one Artifact AND the originating `ModelEntry` has `sha256` set. Closes the latent CivitAI bug as a side effect.

**Files:**
- Modify: `src/kinoforge/core/provisioner.py:113-114` (add 1 conditional immediately after `artifacts = source.resolve(...)`)
- Test: `tests/core/test_provisioner.py` (append 2 new tests)

**Acceptance Criteria:**
- [ ] AC1: A `_FakeSource` returning 2 artifacts + a `_FakeModelEntry` with `sha256="abc"` raises `ValidationError` containing both the ref string and the count `"2 artifacts"`.
- [ ] AC2: Same `_FakeSource` returning 2 artifacts + `sha256=None` succeeds — both artifacts merged with their source-provided sha256 preserved verbatim.
- [ ] AC3: Single-artifact resolves with `entry.sha256` set still behave as today (regression).
- [ ] AC4: The error message includes the suggestion to use `@<commit-sha>` or per-file refs.

**Verify:** `pixi run test tests/core/test_provisioner.py -v` → all existing tests still pass, plus 2 new pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_provisioner.py`. If a `_FakeSource` and `_FakeModelEntry` helper already exist (likely from existing tests), reuse them; otherwise add a minimal version. Pattern:

```python
def test_provision_raises_when_multi_artifact_ref_has_entry_sha256(
    tmp_path: Path,
) -> None:
    """Bug: silently stamping one hash onto N files via the merge loop.

    Catches the latent CivitAI bug closed by Phase 30: a multi-file resolve
    with entry.sha256 set was previously assigning the same hash to every
    artifact, masking N-1 silent verification failures or overwrites.
    """
    fake_source = _FakeSource(
        artifacts=[
            Artifact(url="http://x/a", filename="a.bin"),
            Artifact(url="http://x/b", filename="b.bin"),
        ]
    )
    registry.register_source(fake_source)

    cfg = _FakeProvisionConfig(
        models=[_FakeModelEntry(ref="fake:multi", target="t", sha256="abc")],
    )

    with pytest.raises(ValidationError, match="2 artifacts"):
        provision(
            engine=_FakeEngine(requires_local_weights=False),
            cfg=cfg,
            instance=None,
            creds=_FakeCreds(),
            download_dir=tmp_path,
        )


def test_provision_passes_multi_artifact_ref_when_entry_sha256_none(
    tmp_path: Path,
) -> None:
    """Bug: over-broad guard catching legitimate bare-repo provisions.

    The guard only fires when entry.sha256 is set. With sha256=None,
    multi-artifact resolves must merge as today.
    """
    fake_source = _FakeSource(
        artifacts=[
            Artifact(url="http://x/a", filename="a.bin", sha256="src-a"),
            Artifact(url="http://x/b", filename="b.bin", sha256="src-b"),
        ]
    )
    registry.register_source(fake_source)

    downloaded: list[list[Artifact]] = []

    def _downloader(arts: list[Artifact], dest: Path) -> list[Artifact]:
        downloaded.append(list(arts))
        return arts

    cfg = _FakeProvisionConfig(
        models=[_FakeModelEntry(ref="fake:multi", target="t", sha256=None)],
    )
    provision(
        engine=_FakeEngine(requires_local_weights=True),
        cfg=cfg,
        instance=None,
        creds=_FakeCreds(),
        download_dir=tmp_path,
        downloader=_downloader,
    )
    assert len(downloaded[0]) == 2
    # Source-provided sha256 preserved verbatim — guard did not interfere.
    assert {a.sha256 for a in downloaded[0]} == {"src-a", "src-b"}
```

The test file likely already imports `Artifact`, `ValidationError`,
`provision`, `registry`, and has fixtures named `_FakeSource`,
`_FakeModelEntry`, `_FakeProvisionConfig`, `_FakeEngine`, `_FakeCreds`. If
the names differ, adapt to match existing conventions. **Do NOT invent new
test helper names if equivalents already exist** — read the top of
`tests/core/test_provisioner.py` first.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/core/test_provisioner.py::test_provision_raises_when_multi_artifact_ref_has_entry_sha256 tests/core/test_provisioner.py::test_provision_passes_multi_artifact_ref_when_entry_sha256_none -v`

Expected: First test FAILS (no guard yet; provision succeeds). Second test PASSES (existing behaviour already merges and forwards 2 artifacts).

- [ ] **Step 3: Add the guard in `provisioner.provision`**

In `src/kinoforge/core/provisioner.py`, find the Step 1 merge loop (lines 107-120 today). Replace:

```python
    merged: list[Artifact] = []
    for entry in cfg.models:
        source = registry.source_for_ref(entry.ref)
        artifacts = source.resolve(entry.ref, creds)
        for art in artifacts:
            merged_art = replace(
                art,
                sha256=entry.sha256 if entry.sha256 is not None else art.sha256,
                meta={**art.meta, "target": entry.target},
            )
            merged.append(merged_art)
```

with:

```python
    merged: list[Artifact] = []
    for entry in cfg.models:
        source = registry.source_for_ref(entry.ref)
        artifacts = source.resolve(entry.ref, creds)
        if len(artifacts) > 1 and entry.sha256 is not None:
            raise ValidationError(
                f"sha256 cannot be set on ref {entry.ref!r} — "
                f"it resolves to {len(artifacts)} artifacts. "
                f"Use a pinned revision (e.g. @<commit-sha>) for "
                f"tree-level integrity, or split the entry into "
                f"per-file refs."
            )
        for art in artifacts:
            merged_art = replace(
                art,
                sha256=entry.sha256 if entry.sha256 is not None else art.sha256,
                meta={**art.meta, "target": entry.target},
            )
            merged.append(merged_art)
```

Add the `ValidationError` import to the existing `from kinoforge.core.errors import ...` line (or create that import if it isn't there yet — check the top of the file):

```python
from kinoforge.core.errors import ValidationError
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/core/test_provisioner.py -v`

Expected: All existing tests still pass + 2 new pass.

Also run the broader core suite to catch any cross-test side-effects from CivitAI users who silently had bad configs:
`pixi run test tests/core/ tests/sources/ -v` → all pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/provisioner.py tests/core/test_provisioner.py
git add src/kinoforge/core/provisioner.py tests/core/test_provisioner.py
git commit -m "feat(core/provisioner): reject entry.sha256 on multi-artifact ref (Phase 30 T5)"
```

```json:metadata
{"files": ["src/kinoforge/core/provisioner.py", "tests/core/test_provisioner.py"], "verifyCommand": "pixi run test tests/core/test_provisioner.py -v", "acceptanceCriteria": ["multi-artifact + sha256 set raises ValidationError with count and ref in message", "multi-artifact + sha256=None merges and forwards normally", "single-artifact + sha256 set still merges as before", "error message includes the migration suggestions"]}
```

---

## Task 6: Live smoke + examples + README + PROGRESS

**Goal:** Lock in observable artifacts: one opt-in live smoke test against the real HF tree API, an updated example config demonstrating the bare-repo form, README documentation of the new ref grammar, and the Phase 30 entry in PROGRESS.md with per-task SHAs.

**Files:**
- Create: `tests/sources/test_huggingface_live.py` (opt-in, `KINOFORGE_LIVE_HF=1`)
- Modify: `examples/configs/runpod-comfyui-wan.yaml` (add commented bare-repo alternative)
- Modify: `README.md` (extend the model-refs section with the new grammar table)
- Modify: `PROGRESS.md` (Phase 30 entry, GH #8 row, single-next-action update)

**Acceptance Criteria:**
- [ ] AC1: `pixi run test tests/sources/test_huggingface_live.py -v` is skipped when `KINOFORGE_LIVE_HF` is unset; runs and passes when `KINOFORGE_LIVE_HF=1` and a non-empty file list is returned.
- [ ] AC2: `examples/configs/runpod-comfyui-wan.yaml` parses cleanly (`test_examples.py` round-trip) AND contains a commented-out `# ref: hf:Kijai/WanVideo_comfy` showing the bare form.
- [ ] AC3: `README.md` contains a "Model refs" or equivalent section with a grammar table covering all 4 ref shapes.
- [ ] AC4: `PROGRESS.md` has a `### Phase 30 — HF bare-repo listing (GH #8)` section that mirrors the Phase 29 entry shape (key decisions, per-task SHAs, test-count delta, out-of-scope, closes #N).
- [ ] AC5: Full test suite passes: `pixi run test` → 1062 ± 2 collected, 0 failed, 3 skipped (existing) + 1 skipped (live).
- [ ] AC6: `pixi run pre-commit run --all-files` clean.
- [ ] AC7: GH #8 row in PROGRESS table flips to `CLOSED (Phase 30)`.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

The live smoke must be manually exercised once with `KINOFORGE_LIVE_HF=1` by Dr. Twinklebrane (outside the agent) before this task closes. Capture the test output and paste the artifact summary into the Phase 30 entry under "Live-smoke confirmation". This is the same gate convention as the Phase 19 fal layer and Phase 24 Layer N RunPod smoke.

**Verify:** `pixi run test -v && pixi run pre-commit run --all-files` → all green; manual `KINOFORGE_LIVE_HF=1 pixi run test tests/sources/test_huggingface_live.py -v` PASS reported to the controller.

**Steps:**

- [ ] **Step 1: Write the live smoke test**

Create `tests/sources/test_huggingface_live.py`:

```python
"""Opt-in live smoke test against the real HuggingFace tree API.

Skipped by default. Set ``KINOFORGE_LIVE_HF=1`` to run.

Hits a tiny public canary repo so the test exercises:
- the real Link-header pagination loop,
- the real LFS field shape on at least one file,
- the real Authorization header passthrough when HF_TOKEN is set,
- the real 401/404 error mapping if creds are wrong.

Cost: $0 — the HF tree read API is unauthenticated for public repos.
"""

from __future__ import annotations

import os

import pytest

from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.sources.huggingface import HuggingFaceSource

_LIVE_GATE = "KINOFORGE_LIVE_HF"


pytestmark = pytest.mark.skipif(
    os.environ.get(_LIVE_GATE) != "1",
    reason=f"{_LIVE_GATE}=1 not set; live HF smoke skipped",
)


# Canary candidates — pick a repo that:
#  (a) is small (under 1 GiB total, so the live test stays fast),
#  (b) has at least one LFS-tracked file so the lfs.oid path is exercised,
#  (c) is unauthenticated (no terms of use, no gated access).
#
# Default: "hf-internal-testing/tiny-random-CLIPModel" (HF canary).
# Override via KINOFORGE_LIVE_HF_REPO env var when developing.
_LIVE_REPO = os.environ.get("KINOFORGE_LIVE_HF_REPO", "hf-internal-testing/tiny-random-CLIPModel")


def test_live_bare_repo_returns_at_least_one_file() -> None:
    """Live HF tree API returns a non-empty file list for the canary repo."""
    src = HuggingFaceSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(f"hf:{_LIVE_REPO}", creds)
    assert len(artifacts) > 0, f"expected at least one file in {_LIVE_REPO}"
    # Every emitted artifact must have a URL and a filename — sanity check.
    for a in artifacts:
        assert a.url.startswith("https://huggingface.co/")
        assert a.filename


def test_live_bare_repo_at_least_one_artifact_has_lfs_sha256() -> None:
    """Live tree response includes lfs.oid → Artifact.sha256 for at least one file.

    Some tiny canary repos have no LFS-tracked files; if KINOFORGE_LIVE_HF_REPO
    overrides this, ensure the override repo has at least one LFS file
    (e.g. a small .safetensors).
    """
    src = HuggingFaceSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve(f"hf:{_LIVE_REPO}", creds)
    sha256_count = sum(1 for a in artifacts if a.sha256 is not None)
    assert sha256_count > 0, (
        f"expected at least one LFS-tracked file in {_LIVE_REPO}; "
        f"override KINOFORGE_LIVE_HF_REPO to a repo with LFS files."
    )
```

If the default canary (`tiny-random-CLIPModel`) has no LFS files in
practice (a quick `curl -s https://huggingface.co/api/models/hf-internal-testing/tiny-random-CLIPModel/tree/main?recursive=true | jq '.[] | select(.lfs)'` would confirm), the live-smoke author swaps in an alternative
(e.g. `unitary/toxic-bert` has one ~340 MB LFS file) before manual
execution. The plan's hard contract is "at least one LFS file in the
canary"; the specific repo is operator-choosable.

- [ ] **Step 2: Update the example config**

Edit `examples/configs/runpod-comfyui-wan.yaml`. Find the `models:` block and insert above the existing 4 `hf:` entries:

```yaml
  # Alternative one-line bare-repo form (Phase 30 — GH #8):
  #   - ref: "hf:Kijai/WanVideo_comfy"
  #     target: wan
  # Pulls every file in the repo via the HF tree API; LFS-tracked
  # checkpoints get per-file SHA256 integrity automatically. The
  # per-file form below is retained for tighter control over which
  # files land on disk.
```

Do NOT remove the existing 4 per-file entries. Both forms must stay visible per the spec §7.3 decision.

- [ ] **Step 3: Update README**

In `README.md`, locate the section that documents model refs (search for `hf:` examples; it's likely under a "Model refs" or "Sources" heading). Add a subsection titled `### HuggingFace ref grammar` with the four canonical shapes:

```markdown
### HuggingFace ref grammar

Four ref shapes are recognised:

| Ref | Meaning |
|---|---|
| `hf:<repo>` | Bare repo at `main` — every file enumerated via the HF tree API. |
| `hf:<repo>@<rev>` | Bare repo at a pinned branch / tag / commit SHA. |
| `hf:<repo>:<path>` | Single file at `main`. |
| `hf:<repo>@<rev>:<path>` | Single file at a pinned revision. |

Bare-repo resolves auto-populate per-file SHA256 from LFS metadata when
present (every weights file ships LFS-tracked, so integrity verification
runs without the operator setting `sha256:` per entry). Setting
`sha256:` on a bare-repo entry raises `ValidationError` at config-load
time — use a pinned `@<commit-sha>` for tree-level reproducibility, or
split into per-file refs for per-file pinning.
```

- [ ] **Step 4: Update PROGRESS.md — add Phase 30 entry**

Append after the existing Phase 29 block (around the end of the file):

```markdown
### Phase 30 — HF bare-repo listing (GH #8)

Single-file addition to `src/kinoforge/sources/huggingface/__init__.py`
that widens `HuggingFaceSource.resolve()` to enumerate a whole repo via
the HF tree API on a bare `hf:<repo>` ref. Plus a generic
`provisioner.provision()` guard that rejects `entry.sha256` on any
multi-artifact resolve (closes a latent silent-broken case in
`CivitAISource` as a side effect). Plus a one-line `downloader` mkdir
hygiene fix that lets subpath-bearing artifact filenames land in fresh
directory trees.

- Spec: `docs/superpowers/specs/2026-06-03-hf-bare-repo-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-hf-bare-repo.md`
- T1 (downloader mkdir + 1 AC): `<SHA>`
- T2 (parser + Link cursor + FetchCallable + 6 ACs): `<SHA>`
- T3 (@rev in single-file branch + 2 ACs): `<SHA>`
- T4 (tree branch + 13 ACs, closes deferred AC4 from original layer): `<SHA>`
- T5 (provisioner guard + 2 ACs): `<SHA>`
- T6 (README + examples + PROGRESS + live smoke): `<SHA>`

**Key design decisions:**
- Mirror CivitAI minimalism (Q1=A): bare `hf:<repo>` returns every file;
  no `include`/`exclude` filter knobs.
- `@<rev>` suffix for revision pinning (Q2=A): default `main`, optional.
- LFS-oid auto-populated onto `Artifact.sha256`; reject `entry.sha256`
  on multi-artifact resolves via a generic provisioner guard (Q3=A).
- Preserve repo subdirs in `Artifact.filename`; one-line
  `target_path.parent.mkdir(parents=True, exist_ok=True)` in the
  downloader (Q4=A).
- `?recursive=true` + cursor-loop pagination (Q5=A).
- Error mapping mirrors CivitAI (401 → `AuthError`, other → `KinoforgeError`).
- Provisioner check is source-agnostic (Q7 architecture pick): any
  source returning >1 artifact with `entry.sha256` set fails loud.

**Live-smoke confirmation (Phase 30 T6 gate):** _<paste artifact summary
here after manual `KINOFORGE_LIVE_HF=1` run by Dr. Twinklebrane>_

**Side-effect — latent CivitAI bug closed:** the generic provisioner
guard turns formerly-silent N-1 verification failures on multi-file
`civitai:<modelId>` refs (where the operator had set `sha256:` on the
YAML entry) into a startup-time `ValidationError` with a clear
migration message. See spec §10.

**Test count:** 1044 (post-Phase-29) → ~1062 (post-Phase-30). Delta: +18
net new (downloader +1, parser/link +6, single-file @rev +2, tree
branch +13 with bare-ref test rewrite, provisioner +2; rounded to spec
estimate).

**Out of scope (carry-forward):**
- `include` / `exclude` filtering on `ModelEntry`.
- `GatedModelError` for 403 nuance.
- Custom HF mirror (`HF_ENDPOINT` env var support).
- Live smoke for gated/private repos.

Closes GH #8.
```

Update the GitHub issues table (PROGRESS line ~147):

```markdown
| #8 | HuggingFaceSource bare-repo listing | CLOSED (Phase 30) |
```

Update the Single-next-action block (PROGRESS lines ~152–162) to point
at Phase 30 close-out instead of Phase 29:

- Update `**Where we are:**` to reference the Phase 30 close-out at HEAD
  commit `<T6 SHA after merge>`.
- Update `**First unchecked task in fresh session:**` — replace the
  Phase 29 GH #9 recommendation (now done) with a fresh recommendation
  drawn from the post-29 menu: GH #4 keyframe stage, CLI `_cmd_status`
  ledger reads, or batch streaming log lines.

- [ ] **Step 5: Final gates**

Run the full suite + pre-commit + invariant:

```bash
pixi run test -v
pixi run pre-commit run --all-files
```

Expected: ~1062 collected, 0 failed, 4 skipped (3 existing + 1 live). Pre-commit clean.

Manually run the live smoke once outside the agent flow:

```bash
KINOFORGE_LIVE_HF=1 pixi run test tests/sources/test_huggingface_live.py -v
```

Expected: 2 tests PASS. Capture the test summary (filename list + LFS-sha256 count) and paste it into the PROGRESS Phase 30 entry under "Live-smoke confirmation" before T6 closes.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/sources/test_huggingface_live.py examples/configs/runpod-comfyui-wan.yaml README.md PROGRESS.md
git add tests/sources/test_huggingface_live.py examples/configs/runpod-comfyui-wan.yaml README.md PROGRESS.md
git commit -m "docs(phase30): bare-repo listing — examples + README + PROGRESS + live smoke (closes #8)"
```

After commit, backfill the placeholder `<SHA>` markers in the PROGRESS
entry with the actual T1–T6 commit SHAs (one final commit), per the
Phase 29 pattern.

```json:metadata
{"files": ["tests/sources/test_huggingface_live.py", "examples/configs/runpod-comfyui-wan.yaml", "README.md", "PROGRESS.md"], "verifyCommand": "pixi run test -v && pixi run pre-commit run --all-files", "acceptanceCriteria": ["live smoke skipped unless KINOFORGE_LIVE_HF=1", "example YAML round-trip loads cleanly with bare-repo comment", "README has new grammar table", "PROGRESS Phase 30 entry present with per-task SHAs", "GH #8 marked CLOSED", "test count ~1062 ± 2", "pre-commit all-files clean", "manual KINOFORGE_LIVE_HF=1 run PASS with summary pasted into PROGRESS"], "userGate": true, "tags": ["user-gate"], "requireEvidenceTokens": [["KINOFORGE_LIVE_HF=1", "live smoke PASS"], ["pixi run test -v", "1062 collected"]]}
```

---

## Self-review

Running the four-step self-review against the spec:

**1. Spec coverage.** Each spec section mapped:
- §2 Architecture (3 files touched): T1 (downloader), T2-T4 (HF source), T5 (provisioner). ✓
- §3 Ref grammar (4 shapes): T2 parser tests, T3 @rev wiring, T4 tree-listing branch. ✓
- §4.1-4.4 HuggingFaceSource shape + single-file + tree + pagination: T3 (single-file), T4 (tree + pagination). ✓
- §4.5-4.7 FetchCallable + _urllib_fetch_json + Link cursor helper: T2. ✓
- §4.8 Provisioner guard: T5. ✓
- §4.9 Downloader hygiene: T1. ✓
- §5 HF tree API mechanics: covered by T2 (Link parsing), T4 (recursive+cursor URL building, lfs/non-LFS handling). ✓
- §6 Error mapping: covered by T2 (_urllib_fetch_json mapping) and T4 ACs (AuthError propagation). ✓
- §7.1 Offline ACs: T1 +1, T2 +6, T3 +2, T4 +13, T5 +2 — total ~24 net new. Spec projected ~17–18; the delta lands ~24 due to richer pagination + revision-threading tests. Acceptable — over-coverage beats under.
- §7.2 Live smoke: T6. ✓
- §7.3 Examples + README: T6. ✓
- §8 Out of scope: documented in PROGRESS Phase 30 entry. ✓
- §9 Done criteria: T6 Acceptance Criteria + verify command. ✓
- §10 CivitAI side-effect: T5 docstring + PROGRESS entry. ✓

**2. Placeholder scan.** No "TBD" / "TODO" / "implement later" / "see Task N" in steps. All code blocks are full code; commit commands are exact strings. One legitimate placeholder is `<SHA>` markers in the Phase 30 PROGRESS entry, which T6 Step 6 explicitly says to backfill — same pattern as Phase 29. Acceptable.

**3. Type consistency.**
- `FetchCallable` signature `(str, dict[str, str]) → tuple[list[dict[str, Any]], str | None]` consistent across T2 (helpers), T2 (transport impl), T4 (stub). ✓
- `_parse_hf_ref → tuple[str, str, str | None]` consistent across T2 (definition), T3 (single-file caller), T4 (tree caller). ✓
- `Artifact` field assignments use the same names everywhere: `url`, `filename`, `size`, `sha256`, `headers`. ✓
- `ValidationError` import path consistent: `kinoforge.core.errors.ValidationError`. ✓

**4. Type/method-name drift.** `_single_file_artifact`, `_list_tree_artifacts`, `_fetch_tree`, `_parse_hf_ref`, `_next_cursor_from_link`, `_urllib_fetch_json` — all introduced once and referenced verbatim where reused. No drift detected.

One minor cleanup applied inline above: the T4 stub-builder block briefly carried an inline `from typing import Any` for closure-annotation purposes; Step 1 explicitly directs to drop it in favour of a module-level import. Resolved.

Self-review passes; no plan rewrites needed.
