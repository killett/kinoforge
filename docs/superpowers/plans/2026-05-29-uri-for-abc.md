# `ArtifactStore.uri_for` ABC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `ArtifactStore.uri_for(run_id, name) -> str` to the ABC, implement on `LocalArtifactStore`, refactor `JsonProfileCache` to use it, and remove the `hasattr(_path)` peek + all associated workaround machinery.

**Architecture:** New abstract method on `ArtifactStore` with a pure, no-I/O contract that must equal the `Artifact.uri` returned by `put_bytes`/`put_json` for the same args. `LocalArtifactStore` delegates to its existing private `_path` helper. `JsonProfileCache` drops `_uri_index`, `_uri_for`, and `_reconstruct_uri` entirely and calls `store.uri_for` directly at the single consumer site in `resolve()`.

**Tech Stack:** Python 3.12+, pixi-managed env, pytest, mypy strict, ruff strict, pydantic v2 (existing), stdlib `pathlib`.

**Spec:** `docs/superpowers/specs/2026-05-29-uri-for-abc.md` (committed at `712209b`). Unblocks GitHub issue #5 (S3/GCS stores).

---

## File Map

| Path | Change |
|---|---|
| `src/kinoforge/stores/base.py` | Add `uri_for` abstract method |
| `src/kinoforge/stores/local.py` | Add `uri_for` impl (4 lines, delegates to `_path`) |
| `src/kinoforge/core/profiles.py` | Delete `_uri_index` field, `_uri_for` helper, `_reconstruct_uri` method, `_uri_index` populate in `_persist`, URI Reconstruction docstring block; update `resolve()` to call `store.uri_for` |
| `tests/stores/test_local.py` | Add 6 new tests for `uri_for` contract |
| `tests/core/test_profiles.py` | Add 1 new cross-instance regression test |
| `PROGRESS.md` | Mark Layer A complete; update "Single next action" → Layer B |

---

## Task 1: Add `ArtifactStore.uri_for` ABC method + `LocalArtifactStore` impl + tests

**Goal:** New abstract method on `ArtifactStore`, working impl on `LocalArtifactStore`, six tests covering the contract.

**Files:**
- Modify: `src/kinoforge/stores/base.py` (add abstract method between `delete` and end-of-class, around line 103)
- Modify: `src/kinoforge/stores/local.py` (add public `uri_for` between `_path` at line 55 and `put_bytes` at line 61)
- Modify: `tests/stores/test_local.py` (add 6 tests, file currently has tests up to line ~160)

**Acceptance Criteria:**
- [ ] ABC has new `uri_for` abstract method with no-I/O contract documented
- [ ] `LocalArtifactStore.uri_for(run_id, name)` returns `str(self._path(run_id, name))`
- [ ] Test 1 (`test_uri_for_matches_put_bytes_artifact_uri`) — round-trip equality with `put_bytes`
- [ ] Test 2 (`test_uri_for_matches_put_json_artifact_uri`) — round-trip equality with `put_json`
- [ ] Test 3 (`test_uri_for_no_io_when_item_missing`) — returns a string for never-put name, does NOT raise
- [ ] Test 4 (`test_uri_for_stable_across_store_instances`) — two `LocalArtifactStore(same_root)` instances produce identical URIs
- [ ] Test 5 (`test_uri_for_nested_name_preserves_subpath`) — nested name URI ends with the subpath
- [ ] Test 6 (`test_uri_for_round_trip_via_cross_instance_read`) — inst A puts, inst B reads via `uri_for`
- [ ] `pixi run pre-commit run --files src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_local.py` → all hooks Passed
- [ ] `pixi run test` → all tests pass; coverage ≥ 90%

**Verify:** `pixi run test tests/stores/test_local.py -v` → 6 new tests + all existing pass.

**Steps:**

- [ ] **Step 1: Write the first failing test in `tests/stores/test_local.py`**

Append after the last test in the file:

```python
# --- AC7: uri_for contract ---------------------------------------------------


def test_uri_for_matches_put_bytes_artifact_uri(store: LocalArtifactStore) -> None:
    """uri_for(run_id, name) returns the same uri that put_bytes(run_id, name, ...).uri returns.

    Bug this catches: uri_for diverges from put-time URI -> JsonProfileCache cross-
    restart lookups read wrong path or miss entirely.
    """
    artifact = store.put_bytes("run-1", "blob.bin", b"x")
    assert store.uri_for("run-1", "blob.bin") == artifact.uri
```

- [ ] **Step 2: Run the test and confirm it FAILS**

```bash
pixi run test tests/stores/test_local.py::test_uri_for_matches_put_bytes_artifact_uri -v
```

Expected: `AttributeError: 'LocalArtifactStore' object has no attribute 'uri_for'` (or similar `AbstractMethodError`).

- [ ] **Step 3: Add the abstract method to `ArtifactStore` in `src/kinoforge/stores/base.py`**

Insert this method after the `delete` method (after line 103, before the closing of the class body):

```python
    @abstractmethod
    def uri_for(self, run_id: str, name: str) -> str:
        """Return the URI that would address ``(run_id, name)`` under this store.

        Pure: performs no I/O.  Does NOT check whether the item exists; callers
        that care about existence should use :meth:`list` or let
        :meth:`get_bytes` raise ``FileNotFoundError`` on miss.

        The returned URI MUST equal the ``uri`` field of the
        :class:`~kinoforge.core.interfaces.Artifact` that :meth:`put_bytes` or
        :meth:`put_json` would return for the same ``(run_id, name)`` pair — this
        is the invariant consumers rely on for cross-restart reads.

        Args:
            run_id: Opaque identifier grouping items from one pipeline run.
            name: Relative item name within the run.

        Returns:
            The absolute URI string.
        """
```

- [ ] **Step 4: Add the impl to `LocalArtifactStore` in `src/kinoforge/stores/local.py`**

Insert between the `_path` helper (ends at line 55) and `put_bytes` (line 61). The new method needs a blank line above and the `# ------` section separator updated. Add this after `_path`:

```python
    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the absolute filesystem path for ``(run_id, name)`` as a string.

        Pure: no FS I/O. Matches what :meth:`put_bytes` / :meth:`put_json` would
        return for the same args.

        Args:
            run_id: Run identifier.
            name: Item name; may contain forward slashes.

        Returns:
            The resolved absolute path as a str.
        """
        return str(self._path(run_id, name))
```

Move the existing `# ArtifactStore implementation` section separator that was above `put_bytes` so it doesn't duplicate. Final order: `_path` → `uri_for` → `put_bytes` (the existing separator above `put_bytes` can be deleted since `uri_for` already carries it).

- [ ] **Step 5: Run the test and confirm it PASSES**

```bash
pixi run test tests/stores/test_local.py::test_uri_for_matches_put_bytes_artifact_uri -v
```

Expected: 1 passed.

- [ ] **Step 6: Add tests 2–6**

Append after the test from Step 1:

```python
def test_uri_for_matches_put_json_artifact_uri(store: LocalArtifactStore) -> None:
    """uri_for(run_id, name) matches put_json(run_id, name, obj).uri.

    Bug this catches: put_json uses a different path than put_bytes; uri_for
    is wired to one but not both -> JSON profiles unreadable after restart.
    """
    artifact = store.put_json("run-1", "data.json", {"k": 1})
    assert store.uri_for("run-1", "data.json") == artifact.uri


def test_uri_for_no_io_when_item_missing(store: LocalArtifactStore) -> None:
    """uri_for returns a string for a never-put name; does NOT raise.

    Bug this catches: impl secretly stats the path, breaking the deterministic
    contract and adding I/O cost that future S3/GCS stores would inherit.
    """
    uri = store.uri_for("run-x", "never-existed.bin")
    assert isinstance(uri, str)
    assert uri != ""


def test_uri_for_stable_across_store_instances(tmp_path: Path) -> None:
    """Two LocalArtifactStore instances at the same root return identical URIs.

    Bug this catches: impl uses instance-local state (e.g. an in-memory counter
    in the URI) -> cross-restart reads break because the second process can't
    reconstruct the URI.
    """
    inst_a = LocalArtifactStore(tmp_path)
    inst_b = LocalArtifactStore(tmp_path)
    assert inst_a.uri_for("run-1", "x.bin") == inst_b.uri_for("run-1", "x.bin")


def test_uri_for_nested_name_preserves_subpath(store: LocalArtifactStore) -> None:
    """A name with subdirectory components survives uri_for unchanged.

    Bug this catches: uri_for flattens or strips the subpath -> nested names
    (e.g. profiles/abc.json) resolve to the wrong file.
    """
    uri = store.uri_for("run-1", "profiles/abc.json")
    # The URI on Linux/macOS is the absolute resolved path; assert the
    # last two segments match the name.
    p = Path(uri)
    assert p.name == "abc.json"
    assert p.parent.name == "profiles"


def test_uri_for_round_trip_via_cross_instance_read(tmp_path: Path) -> None:
    """Inst A puts; inst B reads via get_bytes(inst_b.uri_for(...)).

    Bug this catches: uri_for returns a URI that's structurally correct but
    points at a different file than the original put -> get_bytes fails or
    returns wrong bytes.
    """
    inst_a = LocalArtifactStore(tmp_path)
    inst_b = LocalArtifactStore(tmp_path)
    inst_a.put_bytes("run-1", "hello.bin", b"hello")
    uri = inst_b.uri_for("run-1", "hello.bin")
    assert inst_b.get_bytes(uri) == b"hello"
```

- [ ] **Step 7: Run all new tests; confirm all 6 PASS**

```bash
pixi run test tests/stores/test_local.py -v
```

Expected: previously-passing tests still pass + 6 new tests pass.

- [ ] **Step 8: Run full pre-commit + full test suite**

```bash
pixi run pre-commit run --files src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_local.py
pixi run test
```

Expected: ruff + ruff-format + mypy + the other 3 hooks all Passed; full test suite green.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_local.py
git commit -m "$(cat <<'EOF'
feat(stores): add ArtifactStore.uri_for(run_id, name) abstract method + LocalArtifactStore impl

Pure no-I/O contract: uri_for must equal the uri field of the Artifact that
put_bytes/put_json would return for the same (run_id, name). LocalArtifactStore
delegates to its existing private _path helper. Unblocks JsonProfileCache
refactor (next commit) and future S3/GCS stores (issue #5).

Refs #6

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Refactor `JsonProfileCache` to use `store.uri_for`; delete `_uri_index`, `_uri_for`, `_reconstruct_uri`

**Goal:** Remove every workaround in `JsonProfileCache` that existed because `uri_for` was missing. Add the missing cross-instance regression test.

**Files:**
- Modify: `src/kinoforge/core/profiles.py` (delete `_uri_index` field + init, `_uri_for` method lines 183-201, `_reconstruct_uri` method lines 203-229, `_uri_index` populate in `_persist` line 181, URI Reconstruction docstring block lines 23-30, class-docstring `_uri_index` mention; update `resolve()` to call `store.uri_for`)
- Modify: `tests/core/test_profiles.py` (add cross-instance regression test)

**Acceptance Criteria:**
- [ ] `grep -n "_path\b" src/kinoforge/core/profiles.py` → 0 matches
- [ ] `grep -n "_uri_index\|_reconstruct_uri\|_uri_for" src/kinoforge/core/profiles.py` → 0 matches
- [ ] `resolve()` reads URI via `self._store.uri_for(self._run_id, name)`
- [ ] New test `test_resolve_works_across_jsonprofilecache_instances` — persist with cache A, read with cache B on same store + run_id, profile equal
- [ ] All existing `test_profiles.py` tests still pass
- [ ] `pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_profiles.py` → green
- [ ] `pixi run test-cov` → coverage ≥ 90%

**Verify:** `pixi run test tests/core/test_profiles.py -v && grep -nE "_path|_uri_index|_reconstruct_uri|_uri_for" src/kinoforge/core/profiles.py` → tests pass + grep returns 0 matches.

**Steps:**

- [ ] **Step 1: Add the cross-instance regression test (green-first, refactor safety net)**

Append to `tests/core/test_profiles.py`:

```python
# ---------------------------------------------------------------------------
# Cross-instance regression — exercises uri_for path for profile lookups
# ---------------------------------------------------------------------------


def test_resolve_works_across_jsonprofilecache_instances(tmp_path: Path) -> None:
    """A fresh JsonProfileCache reads a profile persisted by a prior instance.

    Bug this catches: the cache leaks _uri_index into the contract; restarting
    the process (a fresh cache pointed at the same store + run_id) breaks
    lookups. Pre-uri_for this worked only via hasattr(_path) peek; post-refactor
    it must work via store.uri_for(run_id, name).
    """
    from kinoforge.core.profiles import JsonProfileCache

    store = LocalArtifactStore(tmp_path)
    key = _make_key()
    probe = _make_probe(max_frames=24, fps=8)
    engine = _FakeEngine()
    backend = _CountingBackend(probe)

    cache_a = JsonProfileCache(store=store)
    persisted = cache_a.discover(key, engine, backend)

    # Brand-new cache instance on the same store + default run_id.
    cache_b = JsonProfileCache(store=store)
    recovered = cache_b.resolve(key)

    assert recovered == persisted
```

- [ ] **Step 2: Run the new test on the CURRENT (pre-refactor) code; confirm it PASSES**

```bash
pixi run test tests/core/test_profiles.py::test_resolve_works_across_jsonprofilecache_instances -v
```

Expected: 1 passed (the existing `hasattr(_path)` peek still works). This is the safety net for the refactor — if it goes red after the deletion, the refactor broke cross-restart reads.

- [ ] **Step 3: Apply the deletions in `src/kinoforge/core/profiles.py`**

Make all of the following edits in one pass:

**3a. Replace the URI Reconstruction docstring block (lines 23-30) with a single line.**

Old:
```python
URI reconstruction
------------------
``ArtifactStore.get_json`` takes a ``uri`` (not a ``(run_id, name)`` pair).
``JsonProfileCache`` keeps an in-process ``_uri_index`` dict seeded by every
``_persist`` call.  On a fresh instance (e.g. after process restart) the
index is empty; ``resolve`` falls back to ``_reconstruct_uri`` which derives
the URI from ``LocalArtifactStore._path`` when available, ensuring
cross-restart reads work for the local store.
```

New:
```python
URI lookup
----------
URIs are resolved via ``ArtifactStore.uri_for(run_id, name)`` — pure, no I/O,
deterministic. No in-process cache is needed.
```

**3b. Remove `_uri_index` from the class docstring's `Attributes:` block (around line 124).**

Find the Attributes block listing `_store`, `_run_id`, `_uri_index`, `_lock`, `_inflight`. Remove the `_uri_index:` entry.

**3c. Remove `_uri_index` field init in `__init__` (line 150).**

Delete `self._uri_index: dict[str, str] = {}`.

**3d. Remove `_uri_index` populate in `_persist` (line 181).**

Old:
```python
        artifact = self._store.put_json(self._run_id, name, _profile_to_dict(profile))
        self._uri_index[name] = artifact.uri
```

New:
```python
        self._store.put_json(self._run_id, name, _profile_to_dict(profile))
```

**3e. Delete the `_uri_for` method entirely (lines 183-201).**

**3f. Delete the `_reconstruct_uri` method entirely (lines 203-229).**

**3g. Update `resolve()` (around lines 235-260) to call `store.uri_for`.**

Old (the body):
```python
        name = self._profile_name(key)
        # Check store listing to detect cross-restart case where _uri_index
        # is empty but the file exists on disk.
        listed = self._store.list(self._run_id)
        if name not in listed:
            raise ProfileNotCached(
                f"no cached profile for capability key {key.derive()!r}; "
                "call discover() to populate the cache"
            )
        uri = self._uri_for(name)
        raw = self._store.get_json(uri)
        return _dict_to_profile(raw)
```

New (the body):
```python
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

- [ ] **Step 4: Run the cross-instance regression test; confirm it still PASSES**

```bash
pixi run test tests/core/test_profiles.py::test_resolve_works_across_jsonprofilecache_instances -v
```

Expected: 1 passed. If it fails, the refactor broke the contract — fix before continuing.

- [ ] **Step 5: Run the full profiles test file**

```bash
pixi run test tests/core/test_profiles.py -v
```

Expected: every test still passes. If any test asserts on `_uri_index` directly, delete that test (grep first to confirm: `grep -n "_uri_index" tests/core/test_profiles.py` → expect 0 matches today per earlier scan; if anything turns up it's recent breakage).

- [ ] **Step 6: Verify the grep acceptance criteria**

```bash
grep -n "_path\b" src/kinoforge/core/profiles.py
grep -n "_uri_index\|_reconstruct_uri\|_uri_for" src/kinoforge/core/profiles.py
```

Expected: both return 0 matches. Note `\b` on `_path` so we don't match `_path_something`.

- [ ] **Step 7: Run the full test suite and pre-commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_profiles.py
pixi run test
pixi run test-cov
```

Expected: pre-commit all hooks Passed; full test suite green; coverage ≥ 90%.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/profiles.py tests/core/test_profiles.py
git commit -m "$(cat <<'EOF'
refactor(profiles): use store.uri_for instead of LocalArtifactStore._path peek

Deletes the hasattr(_path) workaround, the _uri_index in-process cache,
the _uri_for helper, and the _reconstruct_uri method. JsonProfileCache.resolve
now calls store.uri_for(run_id, name) directly — same call site, no caching
needed (uri_for is pure O(1) string work). Adds a cross-instance regression
test that exercises the refactor path; this case previously had no coverage.

Closes #6

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update `PROGRESS.md` — mark Layer A complete, point next action at Layer B

**Goal:** Per project `CLAUDE.md` durability rules — keep the recovery index current. Single next action now points at Layer B (continuity, issue #1).

**Files:**
- Modify: `PROGRESS.md` (append under "Post-MVP" section; update "Single next action")

**Acceptance Criteria:**
- [ ] New entry under "Post-MVP" naming Layer A with both Task 1 + Task 2 commit SHAs
- [ ] "Single next action" section updated to point at Layer B (continuity, issue #1)
- [ ] `pixi run pre-commit run --files PROGRESS.md` green

**Verify:** `git diff PROGRESS.md` shows the two additions; reading the file end-to-end still parses as a coherent recovery index.

**Steps:**

- [ ] **Step 1: Capture both Layer A commit SHAs**

```bash
git log --oneline -5
```

Identify the two SHAs from Task 1 (`feat(stores): add ArtifactStore.uri_for ...`) and Task 2 (`refactor(profiles): use store.uri_for ...`). Call them `<TASK1_SHA>` and `<TASK2_SHA>` below.

- [ ] **Step 2: Append Layer A entry to `PROGRESS.md` under the "Post-MVP" section**

Locate the existing "Post-MVP" block (currently has "Phase 10 — prompt splitter" subsection). Add a new subsection after it:

```markdown
### Phase 11 — uri_for ABC (deferred layer A, GitHub issue #6)
- [x] Task 1: Add `ArtifactStore.uri_for` ABC method + LocalArtifactStore impl + tests — commit `<TASK1_SHA>`
- [x] Task 2: Refactor JsonProfileCache to use `store.uri_for`; delete `_uri_index`, `_uri_for`, `_reconstruct_uri` — commit `<TASK2_SHA>` (closes #6)
```

- [ ] **Step 3: Update "Single next action" section**

Find the existing "## Single next action" block. Replace its body with:

```markdown
**Layer A (uri_for ABC, issue #6) complete.** All acceptance criteria met:
`pixi run pre-commit run --all-files` clean; `pixi run test-cov` reports 90%+
coverage; both `grep _path` and `grep _uri_index|_reconstruct_uri|_uri_for`
return 0 matches in `src/kinoforge/core/profiles.py`. Issue #6 closed.
Issue #5 (S3/GCS stores) is now unblocked.

**Next: Layer B — Continuity / stitching fallback (GitHub issue #1).**
The prompt splitter (Phase 10) ships N-segment plans where segments 1..N-1
have empty `assets`. Layer B fills them with the previous segment's tail
frame as the `init_image` `ConditioningAsset` so non-native engines chain
visually. Touches `core/strategy.py` non-native branch + adds
`extract_last_frame` on the `GenerationEngine` ABC. Begin with the
`superpowers-extended-cc:brainstorming` skill.
```

- [ ] **Step 4: Run pre-commit on the file**

```bash
pixi run pre-commit run --files PROGRESS.md
```

Expected: all hooks Passed.

- [ ] **Step 5: Commit**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): mark Layer A (uri_for ABC) complete

Layer A acceptance pass green: pre-commit clean, coverage >= 90%,
both grep checks (no _path; no _uri_index/_reconstruct_uri/_uri_for in
profiles.py) return 0 matches. Issue #6 closed. Issue #5 (S3/GCS)
unblocked. Single next action points at Layer B (continuity, issue #1).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## End-of-plan acceptance check

After all three tasks ship, run:

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run test-cov
pixi run typecheck
pixi run lint
git log --oneline -5
git status
```

Expected:
- All hooks Passed.
- All tests pass; net +7 tests (6 in `test_local.py`, 1 in `test_profiles.py`).
- Coverage ≥ 90%.
- mypy + ruff strict clean.
- 3 new commits on `main` (feat + refactor + docs).
- Working tree clean.

GitHub issue #6 closes automatically when commit 2 is pushed (via `Closes #6` trailer).
