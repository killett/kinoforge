# HuggingFaceSource bare-repo listing (GitHub issue #8)

**Date:** 2026-06-03
**Layer label:** post-Phase-29 #1 candidate
**Scope:** widen `HuggingFaceSource.resolve()` to enumerate a whole HF repo on
a bare `hf:<repo>` ref, plus one generic provisioner guard and a one-line
downloader hygiene fix.
**Issue:** GH #8 — HuggingFaceSource bare-repo listing.
**Motivation:** today every file in a HuggingFace bundle (SDXL, Flux, Wan,
Hunyuan) has to be enumerated by hand in YAML — typical configs contain 5–15
lines of near-duplicate `hf:<repo>:<path>` entries. Bare-repo support
collapses the bundle to one ref. As a side benefit we reach for per-file
LFS-oid integrity verification (free from the HF tree API) and surface a
latent multi-artifact `entry.sha256` bug that has been silent in `CivitAISource`.

---

## 1. Decisions locked during brainstorming

| Q | Topic | Decision | Rationale |
|---|---|---|---|
| Q1 | Scope | Mirror CivitAI: `hf:<repo>` → all files, no filter knobs | Smallest layer that closes #8; YAGNI on `include`/`exclude` until a real user asks. Additive migration path to filters later. |
| Q2 | Revision pinning | `@<rev>` suffix; default `main`; optional | Mirrors CivitAI's "default first version" UX. Reproducibility is opt-in via `@<commit-sha>`. |
| Q3 | Integrity | Read `lfs.oid` onto `Artifact.sha256` for every LFS file; raise `ValidationError` when `entry.sha256` is set on a multi-artifact resolve | LFS oid is content SHA256, free from the tree API. Single YAML `sha256:` cannot honestly cover N moving files. |
| Q4 | Subdir handling | Preserve repo subdirs in `Artifact.filename`; one-line `target_path.parent.mkdir(parents=True, exist_ok=True)` in the downloader | Diffusion bundles depend on `unet/`, `vae/`, `text_encoder/` layout; flattening loses semantics and collides on sibling `config.json`s. Existing single-file refs keep their leaf-flatten contract. |
| Q5 | Recursion + pagination | `?recursive=true` + cursor-loop until `Link: rel="next"` absent | One round trip per page; correct for large repos (Hunyuan, multi-shard quants) without silent truncation. |
| Q6 | Errors | Mirror CivitAI: 401 → `AuthError`; any other non-2xx → `KinoforgeError("HuggingFace HTTP {code}")`; empty tree → `[]` | Symmetric posture; 403 gated-model nuance deferred. |
| Q7 | Architecture | Source + generic provisioner check | Provisioner guard is source-agnostic, closes latent CivitAI bug as side effect, source stays free of YAML knowledge. |

---

## 2. Architecture

Three files touched:

- `src/kinoforge/sources/huggingface/__init__.py` — `HuggingFaceSource` gains
  a `fetch` constructor seam (mirror `CivitAISource`), a `_parse_hf_ref`
  helper, a `_fetch_tree` paginating helper, and a tree-listing branch in
  `resolve()`. ~80 LOC added.
- `src/kinoforge/core/provisioner.py` — one conditional in the Step 1 merge
  loop: if a source returns more than one artifact AND the originating
  `ModelEntry` has `sha256` set, raise `ValidationError`. Source-agnostic.
  ~5 LOC added.
- `src/kinoforge/core/downloader.py` — one-line
  `target_path.parent.mkdir(parents=True, exist_ok=True)` so subpath-bearing
  filenames work. Benefits any future source that emits subpath filenames.

No new dependencies. No new Python modules. No new YAML schema fields.

Data flow:

```
YAML model entry (ref: hf:org/repo[@rev])
        │
        ▼
registry.source_for_ref(ref) → HuggingFaceSource     (unchanged)
        │
        ▼
HuggingFaceSource.resolve(ref, creds)                 (modified)
   ├─ _parse_hf_ref(ref) → (repo, revision, path|None)
   ├─ if path is not None:  emit single Artifact      (existing behaviour)
   └─ if path is None:      _fetch_tree(repo, revision, headers)
                            ├─ loops ?cursor= pagination
                            └─ emits one Artifact per type=="file" entry
                                 - filename = entry["path"]            (preserves subdirs)
                                 - sha256   = entry["lfs"]["oid"]      (when LFS)
                                 - size     = entry["size"]
                                 - headers  = {"Authorization": ...}   (when HF_TOKEN set)
        │
        ▼
provisioner.provision()  Step 1 merge                 (new check)
   if len(artifacts) > 1 and entry.sha256 is not None:
       raise ValidationError("sha256 cannot be set on multi-artifact ref ...")
        │
        ▼
downloader(merged, dest)                              (one-line fix)
   for each artifact:
     target_path = dest / artifact.filename            (may include subdirs)
     target_path.parent.mkdir(parents=True, exist_ok=True)
     ... existing skip/verify/aria2c/stdlib paths ...
```

Invariants preserved:

- Core never imports a concrete provider/source/engine. The provisioner
  guard is generic — no HF knowledge crosses the boundary.
- `Artifact` dataclass shape unchanged.
- Existing `hf:repo:path/file` refs behave identically; existing example
  configs need no migration.
- Aria2c transport branch (Phase 29) inherits the mkdir fix without further
  change.

---

## 3. Ref grammar

Four canonical shapes:

```
hf:<repo>                          # bare; revision = "main"
hf:<repo>@<revision>               # bare; revision pinned
hf:<repo>:<path>                   # single file; revision = "main"   (existing)
hf:<repo>@<revision>:<path>        # single file; revision pinned    (new)
```

- `<repo>` = `<org>/<name>`. One `/` required.
- `<revision>` = branch / tag / 40-char commit SHA. Passed through verbatim;
  HF surfaces a 404 if invalid.
- `<path>` = everything after the first `:`. May contain `/`.

The existing `_REF_RE = r"^hf:[^:]+(:.*)?$"` already matches all four shapes
(the test `test_handles_bare_repo_ref` proves it). No regex change.

### 3.1 `_parse_hf_ref`

```python
def _parse_hf_ref(ref: str) -> tuple[str, str, str | None]:
    """Returns (repo, revision, path_or_None)."""
    remainder = ref[len("hf:"):]
    repo_rev, _, path = remainder.partition(":")
    path = path or None
    if "@" in repo_rev:
        repo, _, revision = repo_rev.partition("@")
    else:
        repo, revision = repo_rev, "main"
    return repo, revision, path
```

Split order matters: split on `:` first (path separator), then on `@`
(revision separator). `@` is legal inside HuggingFace paths and must not be
claimed as a revision marker.

---

## 4. Components & contracts

### 4.1 `HuggingFaceSource` shape

```python
class HuggingFaceSource(ModelSource):
    scheme = "hf"

    def __init__(self, *, fetch: FetchCallable = _urllib_fetch_json) -> None:
        self._fetch = fetch

    def handles(self, ref: str) -> bool:
        return _REF_RE.match(ref) is not None     # unchanged

    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]:
        repo, revision, path = _parse_hf_ref(ref)
        token = creds.get("HF_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        if path is not None:
            return [self._single_file_artifact(repo, revision, path, headers)]
        return self._list_tree_artifacts(repo, revision, headers)
```

### 4.2 Single-file branch

```python
def _single_file_artifact(
    self, repo: str, revision: str, path: str, headers: dict[str, str]
) -> Artifact:
    filename = path.rsplit("/", 1)[-1]
    url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
    return Artifact(url=url, filename=filename, headers=dict(headers))
```

Only change from today: `revision` interpolated into the URL instead of
hardcoded `main`. Leaf-flatten contract preserved for existing refs.

### 4.3 Tree branch

```python
def _list_tree_artifacts(
    self, repo: str, revision: str, headers: dict[str, str]
) -> list[Artifact]:
    entries = self._fetch_tree(repo, revision, headers)
    artifacts: list[Artifact] = []
    for entry in entries:
        if entry.get("type") != "file":
            continue
        path = entry["path"]
        url = f"{_HF_BASE}/{repo}/resolve/{revision}/{path}"
        lfs = entry.get("lfs") or {}
        sha256 = (lfs.get("oid") or "").lower() or None
        size = entry.get("size")
        artifacts.append(
            Artifact(
                url=url,
                filename=path,          # preserve subdirs
                size=size,
                sha256=sha256,
                headers=dict(headers),
            )
        )
    return artifacts
```

Notes:

- `entry["path"]` written verbatim onto `Artifact.filename` — preserves
  subdir layout (e.g. `unet/diffusion_pytorch_model.safetensors`).
- `lfs.oid` lowercased onto `sha256`; absent (non-LFS file) → `None`.
- `entry.get("type") != "file"` filters out `directory` entries the
  recursive listing may surface.
- One `Artifact` per file; empty repo → `[]` (no special case).

### 4.4 `_fetch_tree` paginating loop

```python
def _fetch_tree(
    self, repo: str, revision: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
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

### 4.5 `FetchCallable` contract

```python
FetchCallable = Callable[
    [str, dict[str, str]],
    tuple[list[dict[str, Any]], str | None],
]
```

Returns `(page_entries, next_cursor_or_None)`. The seam is only ever
called from `_fetch_tree`, so the return type is the tree-page list, not a
generic `list | dict` union. CivitAI's `dict`-returning seam is unaffected
— the seam is private to `HuggingFaceSource`. Symmetry to CivitAI is on
the *injection pattern*, not the signature.

### 4.6 Default `_urllib_fetch_json` transport

```python
def _urllib_fetch_json(
    url: str, headers: dict[str, str]
) -> tuple[list[dict[str, Any]], str | None]:
    req = Request(url, headers=headers)
    try:
        with urlopen(req) as resp:
            body = resp.read()
            link_header = resp.headers.get("Link", "")
    except HTTPError as exc:
        if exc.code == 401:
            raise AuthError(f"HuggingFace 401 Unauthorized for {url}") from exc
        raise KinoforgeError(f"HuggingFace HTTP {exc.code} for {url}") from exc
    parsed = json.loads(body.decode("utf-8"))
    next_cursor = _next_cursor_from_link(link_header)
    return parsed, next_cursor
```

### 4.7 `_next_cursor_from_link` helper

```python
def _next_cursor_from_link(link_header: str) -> str | None:
    """Extract the `cursor` query param from the rel='next' URL in a Link header."""
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
```

### 4.8 Provisioner generic guard

In `core/provisioner.py`, immediately after `artifacts = source.resolve(...)`:

```python
artifacts = source.resolve(entry.ref, creds)
if len(artifacts) > 1 and entry.sha256 is not None:
    raise ValidationError(
        f"sha256 cannot be set on ref {entry.ref!r} — "
        f"it resolves to {len(artifacts)} artifacts. "
        f"Use a pinned revision (e.g. @<commit-sha>) for tree-level integrity, "
        f"or split the entry into per-file refs."
    )
```

Source-agnostic. Closes a latent silent-broken case in `CivitAISource` too:
any operator who previously set `sha256:` on a `civitai:<modelId>` ref that
returned multiple files was getting the same hash stamped onto every file,
silently failing N-1 of them or silently overwriting them. The guard turns
those into a startup `ValidationError`.

### 4.9 Downloader hygiene fix

`src/kinoforge/core/downloader.py`, immediately after computing `target_path`:

```python
target_path = dest / artifact.filename
target_path.parent.mkdir(parents=True, exist_ok=True)
part_path = Path(str(target_path) + ".part")
```

Idempotent (no-op when `artifact.filename` is a bare leaf), race-safe
(`exist_ok=True`), applies before either transport branch (stdlib or aria2c).

---

## 5. HF tree API mechanics

### 5.1 Endpoint

```
GET https://huggingface.co/api/models/{repo}/tree/{revision}
    ?recursive=true
    [&cursor=<token>]

Headers:
  Authorization: Bearer <HF_TOKEN>   (only when token present)
```

- `{repo}` interpolated literally (no URL-encoding of the `/`).
- `{revision}` passed through verbatim. Operator-supplied via YAML.
- `?recursive=true` → flat list covering entire tree.
- `?cursor=<token>` → URL-encoded; tokens are opaque and may contain
  reserved chars.

### 5.2 Per-entry response shape

```json
{
  "type": "file",                  // or "directory"
  "path": "unet/diffusion_pytorch_model.safetensors",
  "size": 13435320336,
  "oid": "<git-blob-sha-NOT-content>",
  "lfs": {
    "oid": "<sha256-of-content>",  // 64 hex chars
    "size": 13435320336,
    "pointerSize": 134
  }
}
```

- Small files (non-LFS, e.g. `config.json`, `README.md`): no `lfs` key.
  `oid` is a git-blob SHA, NOT a content hash. Not used.
- Large files (LFS-tracked, all `*.safetensors`, `*.bin`, `*.gguf`):
  `lfs.oid` is the SHA256 of the file content; lowercased onto
  `Artifact.sha256`.
- Directory entries (`type=="directory"`) appear in the recursive response
  and are filtered out.

### 5.3 Pagination

HF returns the next-cursor via the `Link` response header:

```
Link: <https://huggingface.co/api/models/.../tree/main?recursive=true&cursor=eyJ...>; rel="next"
```

When `rel="next"` is absent, the listing is complete. `_next_cursor_from_link`
extracts the `cursor` query param from the URL inside the rel="next" entry.

---

## 6. Error mapping

| HTTP | Action | Exception | Message |
|---|---|---|---|
| 2xx | parse + return | — | — |
| 401 | raise | `AuthError` | `"HuggingFace 401 Unauthorized for {url}"` |
| 403 | raise | `KinoforgeError` | `"HuggingFace HTTP 403 for {url}"` (operator reads code; gated-model nuance deferred) |
| 404 | raise | `KinoforgeError` | `"HuggingFace HTTP 404 for {url}"` (covers bad repo and bad revision) |
| 5xx | raise | `KinoforgeError` | `"HuggingFace HTTP 5xx for {url}"` |
| Network failure | propagate | `URLError` | unchanged from stdlib |

Mirrors CivitAI's posture exactly. No retry layer at the source.

Empty tree (zero `type=="file"` entries) → return `[]`. Downstream failure
surfaces in the engine with a more informative "weights missing" error than
the source could synthesise.

---

## 7. Testing

### 7.1 Offline ACs

| File | Tests added | Catches |
|---|---|---|
| `tests/sources/test_huggingface.py` | ~14 | parser, single-file revision, tree resolve, pagination, LFS, error mapping, helpers |
| `tests/core/test_provisioner.py` | 2 | multi-artifact `entry.sha256` reject; multi-artifact `entry.sha256=None` pass |
| `tests/core/test_downloader.py` | 1 | subpath filename triggers parent mkdir |

`test_huggingface.py` breakdown:

| Group | Test |
|---|---|
| Parser (`_parse_hf_ref`) | `hf:org/repo` → `(org/repo, main, None)` |
|  | `hf:org/repo@v1.0` → `(org/repo, v1.0, None)` |
|  | `hf:org/repo:a/b.bin` → `(org/repo, main, a/b.bin)` |
|  | `hf:org/repo@sha:a/b.bin` → `(org/repo, sha, a/b.bin)` |
| Single-file branch | revision interpolated into URL |
|  | existing flatten contract preserved (`filename = leaf`) |
| Tree branch | one Artifact per `type=="file"` entry |
|  | `type=="directory"` filtered |
|  | `filename` preserves subdirs (`unet/foo.safetensors`) |
|  | `lfs.oid` lowercased onto `Artifact.sha256` |
|  | non-LFS entry → `sha256=None` |
|  | `size` populated |
|  | `HF_TOKEN` header attached when present |
| Pagination | multi-page fetch accumulates entries from each page |
|  | terminates when next-cursor is `None` |
| Errors | 401 → `AuthError` |
|  | 5xx → `KinoforgeError` with code in msg |
| `_next_cursor_from_link` | rel="next" present → cursor extracted |
|  | absent / empty → `None` |

Existing 11 tests in `test_huggingface.py` (AC1–6 from the original layer) stay
green. Total in this file post-layer: ~25 tests.

### 7.2 Live smoke (opt-in)

`KINOFORGE_LIVE_HF=1`-gated test hitting the real HF tree API for one
tiny public repo. Cost $0 — read API is unauthenticated for public repos.

Suggested canary target: `hf-internal-testing/tiny-random-CLIPModel`
(HF's own stable test repo). The plan-phase task picks a final target;
if the chosen repo has no LFS-tracked files, the test confirms only the
HTTP shape and pagination loop — and the spec calls for a second canary
target with at least one LFS file < 100 MB to also cover the
`lfs.oid → sha256` path. Both confirmed during plan execution.

Skipped by default. Follows Phase 19 fal / Layer N RunPod precedent.

### 7.3 Examples + README

- `examples/configs/runpod-comfyui-wan.yaml` — add a commented-out
  alternative entry showing the bare-repo form alongside the existing 4
  per-file entries. Keep both forms visible.
- `README.md` — extend the model-refs documentation with the new grammar
  table and the bare-repo behaviour. ~15 lines.

### 7.4 Test count projection

Current: 1044 (post Phase 29). Target: ~1062 (+18 net).

---

## 8. Out of scope (carry-forward candidates)

| Item | Why deferred |
|---|---|
| `include` / `exclude` filtering on `ModelEntry` | Locked decision Q1: mirror CivitAI minimalism. Migration path is purely additive. |
| `GatedModelError` (403 nuance) | One-line wrapper if/when a user hits it. CivitAI doesn't differentiate either. |
| Sibling listing for `civitai:` refs | Out of scope — CivitAI already returns multiple files per ref. |
| `revision` as a separate `ModelEntry` YAML key | Locked decision: revision is embedded in the ref string. YAML stays one-line. |
| Live smoke for gated/private repos | Requires real credentials + acceptance of model terms. Public-repo smoke covers the API shape. |
| Concurrent tree fetches across N entries | Not observed in current example configs. |
| Custom HF mirror (`HF_ENDPOINT` env var) | Common HF SDK feature; deferred unless requested. |

---

## 9. Done criteria

Layer ships when:

1. All offline ACs green.
2. Live smoke green when `KINOFORGE_LIVE_HF=1` set (manual verification by
   the project owner).
3. `pixi run pre-commit run --all-files` clean.
4. PROGRESS entry written, GH #8 referenced for close in commit/PR body.
5. README updated with new grammar.
6. Test count matches projection ± 2.
7. `tests/test_core_invariant.py` still green (no new core → adapter imports
   crept in).

---

## 10. Side-effect: latent CivitAI bug closed

Pre-layer, the `core/provisioner.py` Step 1 merge loop stamped a single
`entry.sha256` onto every `Artifact` returned by `CivitAISource.resolve()`.
For multi-file model versions (checkpoints often ship with a config + a
`.safetensors`), this meant every file got verified against the same hash,
either failing N-1 of them or silently overwriting them depending on
download order.

The Section 4.8 guard turns those silently-broken configs into a startup-time
`ValidationError` with a clear migration message. Anyone hitting this fix
should either drop the YAML `sha256:` line (CivitAI's own `hashes.SHA256` is
already attached per-file by the source) or split into per-file refs.
