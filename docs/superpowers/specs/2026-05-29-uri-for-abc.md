# Design — `ArtifactStore.uri_for` ABC method (Layer A, issue #6)

**Status:** validated 2026-05-29. Locked.
**Tracks:** GitHub issue [#6](https://github.com/killett/kinoforge/issues/6).
**Unblocks:** issue #5 (S3 / GCS artifact stores).
**Prior art:** `handoff_20260530-014826.md` §8.1 (`hasattr(_path)` peek), §7 (deferred-layer table).
**Brainstormed via:** `superpowers-extended-cc:brainstorming`.

---

## 1. Problem

`JsonProfileCache._reconstruct_uri` (`src/kinoforge/core/profiles.py:203-229`) peeks at
`LocalArtifactStore._path` via `hasattr` to rebuild URIs on cross-restart reads. This
couples the cache to one concrete store implementation. Adding an S3 or GCS store
would require either:

- another `hasattr` branch per store class (combinatorial blow-up), or
- a clean `uri_for(run_id, name) -> str` method on the `ArtifactStore` ABC that every
  store impl satisfies.

This spec is the second option. Issue #5 (S3/GCS) is gated on it.

## 2. Goal

Add `ArtifactStore.uri_for(run_id, name) -> str` to the `ArtifactStore` ABC. Implement it
on `LocalArtifactStore`. Refactor `JsonProfileCache` to use it, removing every workaround
piece (`_uri_index` field, `_uri_for` helper, `_reconstruct_uri` method, the
`hasattr(_path)` peek). Stay under ABC + impl + one consumer refactor — no S3/GCS work
in this layer.

## 3. Non-goals

- Implementing S3 / GCS stores (issue #5).
- Cross-process discovery lock (issue #7).
- Changing the `ProfileNotCached` raise condition for the legitimate cache-miss case in
  `JsonProfileCache.resolve` (`name not in store.list(run_id)`). That branch stays.

## 4. Design decisions (locked)

### 4.1 `uri_for` is pure: no I/O, no existence check

`store.uri_for(run_id, name)` returns the deterministic URI for that `(run_id, name)`
pair without touching the backing store. For `LocalArtifactStore` that's the resolved
absolute path string; for a future `S3ArtifactStore` it would be `s3://bucket/key`.
Callers that care about existence use `list()` or let `get_bytes` raise
`FileNotFoundError`.

**Rationale:** matches how cloud-object URLs are typically computed (no HEAD round-trip),
makes the method cheap enough to call eagerly, and decouples URI lookup from network
state for stores that go off-box.

**Rejected alternative:** existence-checked `uri_for` that raises `FileNotFoundError` on
miss. Couples URI lookup to I/O cost; clutters cloud-store implementations.

### 4.2 Full cleanup of `JsonProfileCache`'s URI-reconstruction machinery

`_uri_index` (dict field), `_uri_for` (helper method), `_reconstruct_uri` (method), and
the `_uri_index` population inside `_persist` all get deleted. `resolve()` inlines the
call: `uri = self._store.uri_for(self._run_id, name)`. Module-level docstring loses its
"URI reconstruction" section.

**Rationale:** with `uri_for` available, every line of that machinery is dead. Caching
the URI lookup is premature optimisation — local store: one `Path.resolve()` + `str()`;
S3 store: one `f"s3://..."`. No I/O on either.

**Rejected alternative:** minimal refactor — keep `_uri_index` as a put-side cache, just
retarget `_reconstruct_uri` to call `uri_for`. Leaves dead machinery in place; smaller
diff but worse end-state.

### 4.3 `LocalArtifactStore._path` stays private

`uri_for` is the public API. `_path` continues to exist as the implementation detail
that `uri_for` (and `put_bytes`, `delete`, etc.) delegate to.

### 4.4 Contract invariant

For every store impl: `store.uri_for(run_id, name) == store.put_bytes(run_id, name, b).uri`
for any bytes `b`. Same for `put_json`. Tested at the LocalArtifactStore level; future
S3/GCS impls must satisfy it.

## 5. Interface change

### 5.1 `src/kinoforge/stores/base.py`

Add one new abstract method between `delete` and end-of-class:

```python
@abstractmethod
def uri_for(self, run_id: str, name: str) -> str:
    """Return the URI that would address (run_id, name) under this store.

    Pure: performs no I/O. Does NOT check whether the item exists; callers
    that care about existence should use list() or get_bytes (which raises
    FileNotFoundError on miss).

    The returned URI MUST equal the uri field of the Artifact that put_bytes
    or put_json would return for the same (run_id, name) pair — this is the
    invariant consumers rely on for cross-restart reads.

    Args:
        run_id: Opaque identifier grouping items from one pipeline run.
        name: Relative item name within the run.

    Returns:
        The absolute URI string.
    """
```

### 5.2 `src/kinoforge/stores/local.py`

Add public `uri_for` between the existing `_path` helper and `put_bytes`:

```python
def uri_for(self, run_id: str, name: str) -> str:
    """Return the absolute filesystem path for (run_id, name) as a string.

    Args:
        run_id: Run identifier.
        name: Item name; may contain forward slashes.

    Returns:
        The resolved absolute path as a str. Matches what put_bytes /
        put_json would return for the same args.
    """
    return str(self._path(run_id, name))
```

Self-registration line at module bottom unchanged.

### 5.3 `src/kinoforge/core/profiles.py`

Delete:

- `_uri_index: dict[str, str]` field + its init (line ~150) + its class-docstring mention
- `_uri_for` method (lines 183-201)
- `_reconstruct_uri` method (lines 203-229)
- `self._uri_index[name] = artifact.uri` line in `_persist` (line ~181)
- Module-level docstring "URI reconstruction" block (lines 23-30) — replace with a
  one-liner noting URIs come from `store.uri_for`

Update `resolve()`:

```python
def resolve(self, key: CapabilityKey) -> ModelProfile:
    name = self._profile_name(key)
    if name not in self._store.list(self._run_id):
        raise ProfileNotCached(
            f"no cached profile for capability key {key.derive()!r}; "
            "call discover() to populate the cache"
        )
    uri = self._store.uri_for(self._run_id, name)
    raw = self._store.get_json(uri)
    return _dict_to_profile(raw)
```

Net diff: ~50 LOC deleted, ~3 added.

## 6. Test plan (TDD red-first)

### 6.1 New tests in `tests/stores/test_local.py`

Each test has a `# Bug:` comment naming the regression it would catch (per the
`test-design` skill).

1. `test_uri_for_matches_put_bytes_artifact_uri` — round-trip equality with `put_bytes`.
2. `test_uri_for_matches_put_json_artifact_uri` — same for `put_json`.
3. `test_uri_for_no_io_when_item_missing` — call on never-put name; returns a string,
   does not raise.
4. `test_uri_for_stable_across_store_instances` — two `LocalArtifactStore(same_root)`
   instances return identical URIs (cross-restart guarantee).
5. `test_uri_for_nested_name_preserves_subpath` — nested name (`profiles/abc.json`)
   round-trips intact.
6. `test_uri_for_round_trip_via_cross_instance_read` — inst A `put_bytes`; inst B
   `get_bytes(inst_b.uri_for(...))` returns the same bytes.

### 6.2 `tests/core/test_profiles.py` adjustments

Grep confirms today's test suite has **no explicit cross-restart test** — every test
creates a single `JsonProfileCache` per case. The `_reconstruct_uri` codepath was
therefore exercised by no test; the refactor adds the missing coverage.

- **Add `test_resolve_works_across_jsonprofilecache_instances`** — persist with
  cache A, instantiate fresh cache B on same store + run_id, call `B.resolve(key)`,
  assert it returns the same profile. **Bug:** `_uri_index` cache leaks into the
  contract (would have caught the original `hasattr(_path)` peek as well).
- Delete any test that asserts on `_uri_index` field state — that field is gone.
  (Grep confirms: no current test references `_uri_index` or `_reconstruct_uri`,
  so probably nothing to delete. Verify during refactor.)
- No "store without `_path` raises ProfileNotCached" test currently exists; nothing
  to delete on that branch.

### 6.3 Red-first sequence

1. Add test 6.1.1 → run pytest → RED (`AttributeError: 'LocalArtifactStore' has no
   attribute 'uri_for'`).
2. Add `uri_for` to ABC + `LocalArtifactStore` → run pytest → GREEN.
3. Add tests 6.1.2-6.1.6 → run pytest → all GREEN.
4. Apply §5.3 refactor to `JsonProfileCache` → run pytest → existing tests still GREEN;
   delete any obsolete `_uri_index`/`_reconstruct_uri` tests.
5. `pixi run test-cov` → coverage ≥ 90%. `pixi run pre-commit run --all-files` clean.

## 7. Commits (atomic, conventional)

1. `feat(stores): add ArtifactStore.uri_for(run_id, name) abstract method + LocalArtifactStore impl`
   — Touches `src/kinoforge/stores/base.py`, `src/kinoforge/stores/local.py`,
   `tests/stores/test_local.py`.
2. `refactor(profiles): use store.uri_for instead of LocalArtifactStore._path peek`
   — Touches `src/kinoforge/core/profiles.py`, `tests/core/test_profiles.py`.
3. `docs(progress): mark Layer A (uri_for ABC) complete`
   — Touches `PROGRESS.md`; appends Layer A entry under Post-MVP; updates "Single next
   action" to point at Layer B (issue #1, continuity).

## 8. Verification (acceptance criteria)

Every item must hold on the final commit:

1. `pixi run test` — 378+ tests pass; coverage ≥ 90%.
2. `pixi run typecheck` — mypy strict clean.
3. `pixi run lint` — ruff clean.
4. `pixi run pre-commit run --all-files` — all hooks Passed.
5. `grep -n "_path\b" src/kinoforge/core/profiles.py` — zero matches (peek removed).
6. `grep -n "_uri_index\|_reconstruct_uri\|_uri_for" src/kinoforge/core/profiles.py`
   — zero matches (dead machinery removed).
7. GitHub issue #6 closes via commit message reference (`Closes #6`) on commit 2.
8. CI matrix green on ubuntu-latest + macos-latest (Windows declined,
   see `windows-migration-cancelled.md`).

## 9. Risk register

- **Risk:** A test elsewhere depends on `_uri_index` or `_reconstruct_uri` as a private
  affordance. **Mitigation:** test #4 in red-first sequence catches it; delete the
  offending test if its coverage is dead.
- **Risk:** Adding an abstract method breaks instantiation of test-local `ArtifactStore`
  subclasses. **Mitigation:** grep confirmed only `LocalArtifactStore` subclasses the
  ABC; no test fakes affected.
- **Risk:** Future store impls forget `uri_for`. **Mitigation:** abstract → Python
  refuses instantiation. Enforced at instantiation time, not runtime.

## 10. Out of scope (explicitly deferred)

- `ArtifactStore.exists(run_id, name) -> bool` — could be a follow-up if a use case
  emerges. Today consumers use `name in store.list(run_id)` or trust
  `get_bytes`'s `FileNotFoundError`.
- Migrating `LocalArtifactStore` URIs to a scheme (`file://...`) — current bare-path
  contract preserved for backward compat.
- S3 / GCS impls (issue #5).
