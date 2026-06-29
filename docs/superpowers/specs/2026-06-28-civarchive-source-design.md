# civarchive source module — design (sub-project B)

**Date:** 2026-06-28
**Status:** brainstorm complete; awaiting user review before plan
**Sub-project:** B (follows sub-project A — LoRA URL normalization, shipped 2026-06-28)

## Problem

Sub-project A added `civarchive` as a third LoRA scheme alongside `civitai`
and `hf`, but only at the parse layer. Pasted civarchive.com URLs are
canonicalised to `civarchive:<modelId>@<versionId>` refs, and the CLI
heredoc + `LoraEntry` validator both accept them. At resolve time,
however, there is no `civarchive` `ModelSource` registered — the registry
raises a "no source handles ref" error. Civarchive refs are parse-OK,
resolve-fail.

This sub-project implements `CivArchiveSource`: a `ModelSource`
plug-in that resolves `civarchive:N@N` refs to one `Artifact` each, so
the existing downloader path can fetch the file body. It mirrors the
shape of `src/kinoforge/sources/civitai/__init__.py` but works against
civarchive's HTML rather than JSON because civarchive does not publish
a JSON API.

## Research findings (civarchive API surface)

Probed 2026-06-28 against `civarchive.com`:

- **ID scheme**: matches civitai exactly. `https://civarchive.com/models/<modelId>?modelVersionId=<versionId>` is the canonical model-version URL, and `modelId` / `versionId` are the same numeric IDs civitai uses (civarchive is, per its own About page, "Formerly CivitAI Archive").
- **No JSON metadata API**: `https://civarchive.com/api/v1/model-versions/<vid>` returns HTTP 404. Civarchive only serves HTML for metadata.
- **Download endpoint**: `https://civarchive.com/api/download/models/<versionId>` returns HTTP 307 redirect to the actual file host (often `civitai.com/api/download/models/<versionId>`; occasionally a HuggingFace mirror). Civarchive owns the indirection: when civitai removes content, civarchive can re-point the redirect target without changing the public URL.
- **Auth**: HTML pages are anonymously readable. Download endpoint inherits whatever auth the redirect target demands (civitai may require `CIVITAI_TOKEN` for gated content).
- **HTML structure**: plain server-rendered HTML. No `__NEXT_DATA__`, no JSON-LD, no Open Graph metadata. SHA256 hash is rendered as `<a href="/sha256/{hex64}">{hex64}</a>` (the href is structural — civarchive uses `/sha256/<hash>` for its own hash-keyed catalog). Filename is rendered as `<h4>{name}.{ext}</h4>`.

## Design

### Module layout

```
src/kinoforge/sources/civarchive/
  __init__.py                          # CivArchiveSource + self-register
src/kinoforge/_adapters.py             # + import civarchive
tests/sources/
  test_civarchive.py
  civarchive/
    fixtures/
      version_2474081.html             # pinned HTML snapshot for replay tests
tests/live/evidence/
  2026-06-28-civarchive-source/
    resolve.py
    evidence.md
    response_meta.json
```

One module file analogous to `civitai/__init__.py`. One unit test file. One
pinned HTML fixture for offline replay. One live-evidence directory
documenting the smoke run.

### Public interface

```python
# src/kinoforge/sources/civarchive/__init__.py

_REF_RE = re.compile(r"^civarchive:(\d+)(?:@(\d+))?$")

FetchHTMLCallable = Callable[[str, dict[str, str]], str]

def _urllib_fetch_html(url: str, headers: dict[str, str]) -> str:
    # Mirrors civitai's _urllib_fetch_json: same UA workaround,
    # same HTTPError mapping (401 -> AuthError, anything else -> KinoforgeError).
    # Returns the response body decoded as UTF-8 (no JSON parse).
    ...

class CivArchiveSource(ModelSource):
    scheme = "civarchive"

    def __init__(self, *, fetch: FetchHTMLCallable = _urllib_fetch_html) -> None: ...
    def handles(self, ref: str) -> bool: ...
    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]: ...

registry.register_source(CivArchiveSource())
```

`FetchHTMLCallable` differs from civitai's `FetchCallable` only in
return type (`str` HTML body vs `dict[str, Any]` parsed JSON). The
injection-for-tests pattern is identical.

### Resolve flow

```python
def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
    m = _REF_RE.match(ref)
    if m is None:
        raise ValueError(f"Not a valid civarchive ref: {ref!r}")

    model_id_str, version_id_str = m.group(1), m.group(2)
    if version_id_str is None:
        raise KinoforgeError(
            f"civarchive ref {ref!r} requires @<versionId>; "
            "civarchive does not expose a stable default-version selector"
        )

    page_url = (
        f"https://civarchive.com/models/{model_id_str}"
        f"?modelVersionId={version_id_str}"
    )
    html = self._fetch(page_url, {})  # anonymous; no auth header on HTML fetch

    sha256 = _extract_sha256(html)
    filename = _extract_filename(html)

    # Attach CIVITAI_TOKEN to Artifact.headers — mirrors civitai source.
    # civarchive's /api/download/models/<vid> 307-redirects to civitai or
    # HF; if/when the downloader honours Artifact.headers the token will
    # flow to the eventual file fetch.
    token = creds.get("CIVITAI_TOKEN")
    headers: dict[str, str] = (
        {"Authorization": f"Bearer {token}"} if token else {}
    )

    return [
        Artifact(
            url=f"https://civarchive.com/api/download/models/{version_id_str}",
            filename=filename,
            size=None,  # not reliably exposed in HTML
            sha256=sha256,
            headers=headers,
        )
    ]
```

Note that `Artifact.url` is the **civarchive** endpoint, not the resolved
download host. This is deliberate — civarchive's redirect chain owns
host indirection. If civarchive later re-points its mirror to a
different host (which is its archival purpose), persisted refs and
cached configs continue to resolve correctly.

### HTML parser helpers

Pure functions over the HTML body. Narrow regexes anchored on the most
structurally stable signals:

```python
# /sha256/<hex64> is part of civarchive's own URL routing; even a major
# UI redesign would likely preserve it.
_SHA256_HREF_RE = re.compile(r'href="/sha256/([0-9a-f]{64})"')

# Anchor on the <h4> wrapper specifically. The page also embeds HF
# mirror URLs as <a>...<filename>.safetensors</a>; those mirrors are
# uploaded under a *different* filename than the canonical civarchive
# file, so a broad `>...<` regex would non-deterministically pick the
# HF text instead of the civarchive filename. <h4> is currently
# load-bearing; if civarchive changes it, refresh the fixture and
# update this pattern in one PR.
_FILENAME_RE = re.compile(
    r'<h4>([^<>\s]+\.(?:safetensors|ckpt|pt|bin|gguf))</h4>'
)

def _extract_sha256(html: str) -> str:
    m = _SHA256_HREF_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing /sha256/ anchor — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)

def _extract_filename(html: str) -> str:
    m = _FILENAME_RE.search(html)
    if m is None:
        raise KinoforgeError(
            "civarchive HTML missing model filename — page layout may "
            "have changed; civarchive source parser needs maintenance"
        )
    return m.group(1)
```

Returns the **first** match for each. Multi-file model-versions
(unusual for LoRAs but possible for full checkpoints) are out of scope
for v1 — recorded under "Out of scope" below.

### Error handling

| Failure mode | Behavior | Error class |
|---|---|---|
| HTTP 401 on HTML fetch | (Civarchive is anonymous — should never happen) | `AuthError` (mirror civitai) |
| HTTP 404 on HTML fetch | "civarchive does not know model/version" | `KinoforgeError` |
| HTTP 5xx / network exception | "civarchive transient error" + cause | `KinoforgeError` (chained) |
| HTML 200 but no `/sha256/` anchor | "parser needs maintenance" | `KinoforgeError` |
| HTML 200 but no filename match | Same | `KinoforgeError` |
| Bare ref `civarchive:N` | "requires @<versionId>" | `KinoforgeError` (pre-HTTP) |
| Garbage ref / regex no-match | "Not a valid civarchive ref" | `ValueError` (matches civitai) |

**Privacy**: error messages may include the ref string
(`civarchive:N@N`) but never the full URL. Mirrors the lora-redaction
convention established in sub-project A. Privacy AST scan
(`tests/test_no_unredacted_writes.py` family) catches regressions.

### Tests

`tests/sources/test_civarchive.py` — unit tests, no network. Spy-fetch
injection pattern matches `tests/sources/test_civitai.py`.

| Test | Verifies |
|---|---|
| `test_handles_canonical_with_version` | `handles("civarchive:111@222")` returns True |
| `test_handles_bare_model_only` | `handles("civarchive:111")` returns True (regex accepts; resolve will raise) |
| `test_handles_rejects_civitai_scheme` | `handles("civitai:111@222")` returns False |
| `test_handles_rejects_garbage` | `handles("civarchive:abc@xyz")` returns False |
| `test_resolve_bare_ref_raises_pre_http` | `resolve("civarchive:111", ...)` raises `KinoforgeError` mentioning "requires @<versionId>"; spy fetch is never called |
| `test_resolve_invalid_ref_raises_value_error` | `resolve("garbage", ...)` raises `ValueError` |
| `test_resolve_returns_one_artifact_from_pinned_fixture` | Inject spy returning `version_2474081.html` fixture; assert exact `Artifact(url='https://civarchive.com/api/download/models/2474081', filename='wan2.2_t2v_arcanestyle_high.safetensors', sha256='67cf1c234f8930472437c3fb9f940d1e05c95261a749c75956831b4ee25fba4d', size=None, headers={})` |
| `test_resolve_attaches_civitai_token_to_headers` | Inject creds with `CIVITAI_TOKEN=<value>`; assert `Artifact.headers == {"Authorization": "Bearer <value>"}` |
| `test_resolve_no_token_no_auth_header` | Empty creds; assert `Artifact.headers == {}` |
| `test_resolve_404_raises_kinoforge_error` | Spy raises `HTTPError(404)`; assert `KinoforgeError` (not `AuthError`) |
| `test_resolve_401_raises_auth_error` | Spy raises `HTTPError(401)`; assert `AuthError` |
| `test_extract_sha256_missing_raises` | Pure-function test on HTML without `/sha256/` anchor → `KinoforgeError` mentions "parser needs maintenance" |
| `test_extract_filename_missing_raises` | Same shape for filename |
| `test_extract_sha256_finds_first_match` | Documents v1 contract: first match wins |
| `test_extract_filename_anchored_on_h4_not_anchor_text` | HTML embeds an HF mirror `<a>...wrong.safetensors</a>` **before** the civarchive `<h4>right.safetensors</h4>`; assert `_extract_filename` returns `right.safetensors`. Locks the `<h4>` anchor against the HF-link false positive |
| `test_sha256_returned_lowercase` | Returned hash is lowercase hex (matches civitai convention) |
| `test_self_registers_on_import` | `import kinoforge.sources.civarchive`; assert `"civarchive" in registry._SOURCES` |
| `test_ref_no_url_leakage_in_errors` | Privacy AST scan: error messages never contain `https://civarchive.com` |

**Live evidence** — one-shot, $0 spend (anonymous HTTP GET only):

`tests/live/evidence/2026-06-28-civarchive-source/resolve.py` runs
`CivArchiveSource().resolve("civarchive:2197303@2474081", EnvCredentialProvider())`
against real `civarchive.com`. Prints the resolved `Artifact`, writes
`response_meta.json` (status code + response headers) and `evidence.md`
(human-readable summary with date, ref, resolved fields).

Confirms the pinned HTML fixture is faithful to the live page shape;
documents which sha256 + filename were observed on the smoke date.

Live download of the file body is **out of scope** for this
sub-project — it belongs to the downloader's separate workstream.

**Fixture maintenance contract**: `version_2474081.html` is committed
verbatim from a live capture. No PII scrubbing required (civarchive
HTML exposes no user-specific data). If civarchive redesigns and
fixture replay breaks, refresh the fixture and update both regexes in
one PR.

### Integration touch-points

- `src/kinoforge/_adapters.py` — add `import kinoforge.sources.civarchive  # noqa: F401` next to the existing civitai + huggingface imports (lines 46 / 50).
- `src/kinoforge/core/lora.py` — no changes (sub-project A already accepts civarchive URLs).
- `src/kinoforge/cli/loras_arg.py` — no changes (sub-project A already lists civarchive in `_KNOWN_SCHEMES`).
- `docs/warm-reuse.md` — update the stub at lines 146–149 that currently warns "civarchive refs will fail at resolution time"; replace with confirmation that civarchive refs now resolve via HTML scrape with sha256 integrity.
- `PROGRESS.md` — at ship-time, close the sub-project B workstream block and drop the top-priority pointer at lines 45–77.
- Sweeper, orchestrator, downloader — no changes. `CivArchiveSource` is a pure `ModelSource` plug-in; invariants flow through existing registry and downloader paths unchanged.

## Out of scope (recorded for future work)

- **Multi-file model-versions** — regex returns first match for sha256 and filename. Models that ship multiple files (e.g. fp16 + fp8 variants) only yield the first.
- **Live file download** — civarchive 307-redirects to civitai or HF for the file body. The downloader's redirect handling is its own concern. CIVITAI_TOKEN propagation across cross-host redirects depends on `Artifact.headers` actually being applied by the downloader, which currently looks like a pre-existing gap at `src/kinoforge/core/downloader.py:297`. Tracked separately.
- **Trigger-word extraction** — civarchive HTML exposes the LoRA trigger word but the `Artifact` schema has no slot for it. LoRA trigger-word storage is a separate workstream.
- **`/sha256/<hash>` direct lookup** — civarchive supports hash-keyed access. A future `civarchive-sha256:<hex64>` scheme could resolve unversioned by hash. Defer until a use-case arrives.
- **Alternate mirror discovery** — page lists HF and civitai mirror URLs. Capturing them as fallback URLs in `Artifact` would require a richer `Artifact` shape (list of candidate URLs). Defer.
- **Cache-Control / If-Modified-Since** — HTML fetch refetches every resolve. Acceptable because resolve runs rarely (cold cache fills only).

## Acceptance criteria

A `LoraEntry(ref="https://civarchive.com/models/2197303?modelVersionId=2474081")`
normalised by sub-project A and passed through the resolver yields one
`Artifact` whose `sha256` matches the live civarchive HTML, whose
`filename` ends in `.safetensors`, and whose `url` points at
`https://civarchive.com/api/download/models/2474081`. The downloader
can take that `Artifact` and verify the eventual file body's sha256
against `Artifact.sha256` without further civarchive-source
involvement.
