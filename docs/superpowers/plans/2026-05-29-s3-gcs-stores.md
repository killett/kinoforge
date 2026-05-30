# S3 + GCS Artifact Stores Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two new concrete `ArtifactStore` implementations — `S3ArtifactStore` and `GCSArtifactStore` — shipped together as sibling adapters, plus optional config-driven CLI selection that stays backwards compatible with today's `LocalArtifactStore(state_dir)` default.

**Architecture:** Approach A from the spec: two independent siblings under `src/kinoforge/stores/<name>/`, no shared cloud-base class. Each self-registers via `register_store(name, factory)` and is wired through `_adapters.py`. Both use injected client seams so tests stay 100% offline. `core/config.py` gains an optional `StoreConfig` pydantic block; `cli.py` gains a `_build_store(cfg, state_dir)` helper that honours the block but falls back to `LocalArtifactStore(state_dir)` when absent.

**Tech Stack:** Python 3.12+, pixi-managed env, `boto3` + `google-cloud-storage` (both conda-forge), `pydantic` v2, pytest, mypy strict, ruff strict.

**Spec:** `docs/superpowers/specs/2026-05-29-s3-gcs-stores-design.md` (committed at `1f8016d`, self-review fixes at `e95b94d`). Closes GitHub issue #5.

---

## File Map

| Path | Change |
|---|---|
| `src/kinoforge/stores/s3/__init__.py` | **New file.** `S3ArtifactStore` + `register_store("s3", ...)` |
| `src/kinoforge/stores/gcs/__init__.py` | **New file.** `GCSArtifactStore` + `register_store("gcs", ...)` |
| `src/kinoforge/_adapters.py` | Add 2 import lines under the `# Stores` section |
| `src/kinoforge/core/config.py` | Add `StoreConfig` pydantic model + `Config.store` field |
| `src/kinoforge/cli.py` | Add `_build_store(cfg, state_dir)` helper; swap 2 `LocalArtifactStore(state_dir)` call sites; replace 1 `store._path(...)` peek with `store.uri_for(...)` |
| `tests/stores/conftest.py` | **New file.** Shared `FakeS3Client` + `FakeGCSClient` + bytes-body + bucket / blob doubles |
| `tests/stores/test_s3.py` | **New file.** 17 tests against `FakeS3Client` |
| `tests/stores/test_gcs.py` | **New file.** 17 tests against `FakeGCSClient` |
| `tests/test_core_invariant.py` | Extend `_VENDOR_PATTERNS` with `boto3` + `google.cloud` entries |
| `tests/core/test_config.py` | Add 6 `StoreConfig` schema tests |
| `tests/test_cli.py` | Add 3 store-selection tests (default-local + explicit-S3 + uri_for-not-peek) |
| `examples/configs/wan.yaml` | Append commented `store:` block (one config; others prove default backwards compat) |
| `pixi.toml` | Add `boto3 = "*"` + `google-cloud-storage = "*"` under `[dependencies]` |
| `README.md` | Drop "S3/GCS stores" from Roadmap; extend "Extending" section to list 3 stores |
| `PROGRESS.md` | Append "Phase 13 — S3/GCS stores (deferred layer C, GitHub issue #5)" block; repoint "Single next action" |

---

## Task 1: `S3ArtifactStore` — store impl + deps + invariant patterns + adapters wire + 17 tests

**Goal:** Ship `S3ArtifactStore` end-to-end. Land both pixi deps (`boto3` + `google-cloud-storage`) and both new `_VENDOR_PATTERNS` entries in this task so the floor is laid for Task 2's GCS work without a separate infrastructure-only commit. After this task, S3 stores fully function through the registry and via direct construction; Task 2 mirrors the same shape for GCS.

**Files:**
- Create: `src/kinoforge/stores/s3/__init__.py`
- Modify: `src/kinoforge/_adapters.py` (add S3 import — and GCS import is added in Task 2)
- Modify: `pixi.toml` (add both `boto3` and `google-cloud-storage` under `[dependencies]`)
- Modify: `tests/test_core_invariant.py` (extend `_VENDOR_PATTERNS` with both boto3 + google.cloud entries)
- Create: `tests/stores/conftest.py` (shared `FakeS3Client` + `FakeGCSClient` + helper doubles)
- Create: `tests/stores/test_s3.py` (17 tests)

**Acceptance Criteria:**
- [ ] `S3ArtifactStore(ArtifactStore)` implements all 7 abstract methods: `put_bytes`, `get_bytes`, `put_json`, `get_json`, `list`, `delete`, `uri_for`
- [ ] `uri_for(run_id, name)` returns `f"s3://{bucket}/{prefix/run_id/name}"` with no leading/trailing slashes and no double slashes
- [ ] `put_bytes(...).uri == uri_for(...)` for the same `(run_id, name)` (cross-method invariant)
- [ ] `put_json(...).uri == uri_for(...)` for the same `(run_id, name)`
- [ ] `get_bytes` on missing key raises `FileNotFoundError(uri)`
- [ ] `delete` on missing key raises `FileNotFoundError(uri)`
- [ ] `S3ArtifactStore.__init__(client=fake)` does NOT trigger `import boto3` (lazy gate proven by sys.modules snapshot test)
- [ ] Self-registers under `"s3"` in the registry; default factory raises a helpful `RuntimeError` when `KINOFORGE_S3_BUCKET` env is unset
- [ ] `pixi.toml` declares both `boto3 = "*"` and `google-cloud-storage = "*"` under `[dependencies]`
- [ ] `_VENDOR_PATTERNS` in `tests/test_core_invariant.py` includes entries confining `boto3` to `src/kinoforge/stores/s3/` and `google.cloud` to `src/kinoforge/stores/gcs/`
- [ ] `_adapters.py` includes `import kinoforge.stores.s3` under the `# Stores` section
- [ ] All 17 `test_s3.py` tests pass; existing 395 tests still pass
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test-cov` reports coverage ≥ 90%

**Verify:** `pixi run test tests/stores/test_s3.py tests/test_core_invariant.py -v && pixi run test` → 17 new tests + existing core-invariant tests + full suite green.

**Steps:**

- [ ] **Step 1: Update `pixi.toml` — add both cloud SDKs as conda-forge deps**

Open `/workspace/pixi.toml`. Locate the `[dependencies]` block (around lines 39-63). After the last existing line (`types-pyyaml = ">=6.0.12.20260518,<7"`), add these two lines:

```toml
boto3 = "*"
google-cloud-storage = "*"
```

The two libs land together (even though GCS isn't used until Task 2) so the lockfile resolves once.

- [ ] **Step 2: Regenerate the lockfile**

```bash
pixi install
```

Expected: pixi resolves and updates `pixi.lock` to include `boto3`, `botocore`, `google-cloud-storage`, `google-cloud-core`, `google-api-core`, and their transitive deps. May take 30-60s.

- [ ] **Step 3: Create `tests/stores/conftest.py` with both fake clients**

Create `/workspace/tests/stores/conftest.py`:

```python
"""Shared in-memory test doubles for S3ArtifactStore + GCSArtifactStore tests.

Both fakes implement only the surface the stores actually call. Real cloud
SDKs are never imported; tests pass `client=fake` (and for GCS, also
`not_found_exc=Fake.NotFound`) to bypass the lazy-import gates in the store
constructors.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


# ---------------------------------------------------------------------------
# S3 fakes
# ---------------------------------------------------------------------------


class _NoSuchKeyError(Exception):
    """Stand-in for boto3.client('s3').exceptions.NoSuchKey."""


class _ClientErrorFake(Exception):
    """Stand-in for botocore.exceptions.ClientError.

    Carries a `response` dict shaped like boto3's so the store's error mapping
    code path is exercised exactly as the real SDK would trigger it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _S3Exceptions:
    """Stand-in for the `.exceptions` namespace on a real boto3 S3 client."""

    NoSuchKey = _NoSuchKeyError
    ClientError = _ClientErrorFake


class _BytesBody:
    """Stand-in for the StreamingBody returned in get_object()['Body']."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _S3Paginator:
    """Stand-in for the paginator returned by client.get_paginator('list_objects_v2')."""

    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self._objects = objects

    def paginate(self, *, Bucket: str, Prefix: str) -> Iterator[dict[str, Any]]:
        contents = [
            {"Key": k}
            for (b, k) in sorted(self._objects)
            if b == Bucket and k.startswith(Prefix)
        ]
        # Single page is sufficient for test workloads; real S3 paginates
        # beyond 1000 keys but S3ArtifactStore.list iterates the paginator
        # generically so the shape is enough.
        yield {"Contents": contents}


class FakeS3Client:
    """In-memory stand-in for boto3.client('s3') covering the S3ArtifactStore surface."""

    exceptions = _S3Exceptions()

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict[str, Any]:
        self._objects[(Bucket, Key)] = Body
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.NoSuchKey()
        return {"Body": _BytesBody(self._objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self._objects:
            raise self.exceptions.ClientError("NoSuchKey")
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self._objects.pop((Bucket, Key), None)
        return {}

    def get_paginator(self, op: str) -> _S3Paginator:
        assert op == "list_objects_v2", f"unexpected paginator op: {op!r}"
        return _S3Paginator(self._objects)


# ---------------------------------------------------------------------------
# GCS fakes (used by Task 2's tests; landed here in Task 1 to avoid two
# conftest.py edits)
# ---------------------------------------------------------------------------


class _GCSNotFound(Exception):
    """Stand-in for google.api_core.exceptions.NotFound."""


class _FakeBlob:
    def __init__(self, bucket: "_FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, data: bytes) -> None:
        self.bucket._blobs[self.name] = data

    def download_as_bytes(self) -> bytes:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        return self.bucket._blobs[self.name]

    def delete(self) -> None:
        if self.name not in self.bucket._blobs:
            raise _GCSNotFound()
        del self.bucket._blobs[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket._blobs


class _FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self._blobs: dict[str, bytes] = {}

    def blob(self, key: str) -> _FakeBlob:
        return _FakeBlob(self, key)

    def list_blobs(self, *, prefix: str) -> Iterator[_FakeBlob]:
        for k in sorted(self._blobs):
            if k.startswith(prefix):
                yield _FakeBlob(self, k)


class FakeGCSClient:
    """In-memory stand-in for google.cloud.storage.Client."""

    NotFound = _GCSNotFound

    def __init__(self) -> None:
        self._buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self._buckets.setdefault(name, _FakeBucket(name))
```

- [ ] **Step 4: Write the first failing test in `tests/stores/test_s3.py`**

Create `/workspace/tests/stores/test_s3.py`:

```python
"""Tests for S3ArtifactStore — all run against FakeS3Client (no network).

Spec: docs/superpowers/specs/2026-05-29-s3-gcs-stores-design.md §3.1 + §8.2
"""

from __future__ import annotations

import sys

import pytest

from tests.stores.conftest import FakeS3Client


@pytest.fixture()
def fake_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture()
def store(fake_client: FakeS3Client):  # noqa: ANN201
    from kinoforge.stores.s3 import S3ArtifactStore

    return S3ArtifactStore(bucket="bkt", prefix="prefix", client=fake_client)


# --- AC1: put_bytes returns a properly-scheme'd Artifact ---------------------


def test_put_bytes_returns_artifact_with_s3_uri(store) -> None:  # noqa: ANN001
    """put_bytes returns Artifact with uri = s3://<bucket>/<prefix>/<run_id>/<name>.

    Bug this catches: returning a path-style uri ("/bucket/...") or omitting the scheme.
    """
    artifact = store.put_bytes("run-1", "out.bin", b"\x00\x01")
    assert artifact.uri == "s3://bkt/prefix/run-1/out.bin"
```

- [ ] **Step 5: Run the first test — confirm it FAILS**

```bash
pixi run test tests/stores/test_s3.py::test_put_bytes_returns_artifact_with_s3_uri -v
```

Expected: `ModuleNotFoundError: No module named 'kinoforge.stores.s3'`.

- [ ] **Step 6: Create `src/kinoforge/stores/s3/__init__.py`**

Create `/workspace/src/kinoforge/stores/s3/__init__.py`:

```python
"""Amazon S3-backed ArtifactStore.

Self-registers under ``"s3"`` on import via the store registry.  The default
zero-arg factory reads ``KINOFORGE_S3_BUCKET`` (+ optional
``KINOFORGE_S3_PREFIX``) from the environment; library users wanting full
control construct ``S3ArtifactStore(bucket=..., prefix=..., client=...)``
directly.

The ``client`` parameter is injected by tests so the lazy ``import boto3``
inside ``__init__`` never fires under the test path.
"""

from __future__ import annotations

import json
import os
from typing import Any

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore


class S3ArtifactStore(ArtifactStore):
    """ArtifactStore backed by S3.

    Storage layout: ``s3://<bucket>/<prefix>/<run_id>/<name>``.

    Attributes:
        bucket: Target S3 bucket name.
        prefix: Optional key prefix; normalised to have no leading or trailing slash.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
    ) -> None:
        """Initialise the store.

        Args:
            bucket: Target S3 bucket name.
            prefix: Optional key prefix.  Leading and trailing slashes are stripped.
            client: Optional boto3 S3 client.  When ``None``, a real client is
                lazily constructed via ``boto3.client("s3")`` (uses the SDK
                default credential chain).
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            import boto3  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

            client = boto3.client("s3")
        self._client: Any = client

    def _key(self, run_id: str, name: str) -> str:
        """Return the absolute object key for ``(run_id, name)``."""
        parts = [p for p in (self.prefix, run_id, name) if p]
        return "/".join(parts)

    @staticmethod
    def _split_uri(uri: str) -> tuple[str, str]:
        """Split an ``s3://bucket/key`` URI into ``(bucket, key)``."""
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// uri: {uri!r}")
        bucket, _, key = uri[len("s3://") :].partition("/")
        return bucket, key

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the S3 URI for ``(run_id, name)`` — pure, no I/O."""
        return f"s3://{self.bucket}/{self._key(run_id, name)}"

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>``."""
        key = self._key(run_id, name)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return Artifact(uri=f"s3://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        """Read the bytes at ``uri``; raise FileNotFoundError on miss."""
        bucket, key = self._split_uri(uri)
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None
        return resp["Body"].read()  # type: ignore[no-any-return]

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise ``obj`` as UTF-8 JSON and persist under ``<run_id>/<name>``."""
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at ``uri``."""
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        """Enumerate names stored under ``run_id`` (relative to ``<prefix>/<run_id>/``)."""
        run_prefix = self._key(run_id, "") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                names.append(obj["Key"][len(run_prefix) :])
        return names

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise FileNotFoundError on miss."""
        bucket, key = self._split_uri(uri)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except self._client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"artifact not found: {uri!r}") from None
            raise
        self._client.delete_object(Bucket=bucket, Key=key)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402


def _default_factory() -> S3ArtifactStore:
    """Zero-arg factory reading bucket + prefix from env."""
    bucket = os.environ.get("KINOFORGE_S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            "S3ArtifactStore default factory needs KINOFORGE_S3_BUCKET; "
            "either set the env var or construct S3ArtifactStore(bucket=...) directly."
        )
    return S3ArtifactStore(
        bucket=bucket, prefix=os.environ.get("KINOFORGE_S3_PREFIX", "")
    )


register_store("s3", _default_factory)
```

- [ ] **Step 7: Run the first test — confirm it PASSES**

```bash
pixi run test tests/stores/test_s3.py::test_put_bytes_returns_artifact_with_s3_uri -v
```

Expected: 1 passed.

- [ ] **Step 8: Wire `s3` into `_adapters.py`**

Open `/workspace/src/kinoforge/_adapters.py`. Under the `# Stores` section (currently containing only `import kinoforge.stores.local`), add the S3 import:

```python
# Stores
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3     # noqa: F401
```

(GCS import lands in Task 2.)

- [ ] **Step 9: Extend `_VENDOR_PATTERNS` in `tests/test_core_invariant.py`**

Open `/workspace/tests/test_core_invariant.py`. Locate `_VENDOR_PATTERNS` (around lines 66-77). Add two new entries after the existing `runpod` entry so the full list reads:

```python
_VENDOR_PATTERNS: list[tuple[re.Pattern[str], Path, str]] = [
    (
        re.compile(r"^\s*(import|from)\s+(sky|skypilot)\b"),
        SRC_ROOT / "providers" / "skypilot",
        "sky/skypilot",
    ),
    (
        re.compile(r"^\s*(import|from)\s+runpod\b"),
        SRC_ROOT / "providers" / "runpod",
        "runpod",
    ),
    (
        re.compile(r"^\s*(import|from)\s+boto3\b"),
        SRC_ROOT / "stores" / "s3",
        "boto3",
    ),
    (
        re.compile(r"^\s*(import|from)\s+google\.cloud\b"),
        SRC_ROOT / "stores" / "gcs",
        "google-cloud-storage",
    ),
]
```

The `google.cloud` pattern lands in this task even though `stores/gcs/__init__.py` doesn't exist yet — the regex only matches *existing* `import` lines, so adding the pattern without an importer is a no-op until Task 2 lands the GCS file.

- [ ] **Step 10: Add the remaining 16 tests to `tests/stores/test_s3.py`**

Append the following to `/workspace/tests/stores/test_s3.py` (the file already has the round-trip test #1 from Step 4):

```python
# --- AC2: get_bytes round-trips ----------------------------------------------


def test_get_bytes_round_trips(store) -> None:  # noqa: ANN001
    """Bytes written by put_bytes are recovered exactly by get_bytes(uri).

    Bug this catches: reading from the wrong key or wrong bucket.
    """
    artifact = store.put_bytes("run-1", "blob.bin", b"hello s3")
    assert store.get_bytes(artifact.uri) == b"hello s3"


# --- AC3: prefix handling ----------------------------------------------------


def test_put_get_with_prefix(store, fake_client) -> None:  # noqa: ANN001
    """Non-empty prefix is folded into the object Key, not the URI separately.

    Bug this catches: storing under <run_id>/<name> ignoring prefix; or
    prepending prefix as a separate URI path segment with stray slashes.
    """
    store.put_bytes("rid", "a.bin", b"x")
    # Key stored in fake should include prefix.
    assert ("bkt", "prefix/rid/a.bin") in fake_client._objects


def test_put_get_with_empty_prefix(fake_client) -> None:  # noqa: ANN001
    """Empty prefix produces no double slashes and no leading slash in key.

    Bug this catches: '' prefix yielding key '/<run>/<name>' (S3 silently
    accepts this, but `list` and cross-instance reads break).
    """
    from kinoforge.stores.s3 import S3ArtifactStore

    store = S3ArtifactStore(bucket="bkt", prefix="", client=fake_client)
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "s3://bkt/rid/a.bin"
    assert ("bkt", "rid/a.bin") in fake_client._objects


def test_put_get_with_slash_normalised_prefix(fake_client) -> None:  # noqa: ANN001
    """Leading and trailing slashes in prefix are stripped during init.

    Bug this catches: user passes '/foo/bar/' as prefix, store concatenates
    blindly, producing key '/foo/bar//rid/name'.
    """
    from kinoforge.stores.s3 import S3ArtifactStore

    store = S3ArtifactStore(bucket="bkt", prefix="/foo/bar/", client=fake_client)
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "s3://bkt/foo/bar/rid/a.bin"


# --- AC4: put_json round-trips -----------------------------------------------


def test_put_json_round_trips(store) -> None:  # noqa: ANN001
    """A dict written by put_json is recovered as an equivalent dict.

    Bug this catches: re-encoding on read causing type drift (e.g. int -> str).
    """
    obj = {"key": "value", "count": 42, "nested": {"x": 1.5}}
    artifact = store.put_json("rid", "data.json", obj)
    assert store.get_json(artifact.uri) == obj


# --- AC5: run_id isolation ---------------------------------------------------


def test_run_ids_are_isolated(store) -> None:  # noqa: ANN001
    """Same name, different run_ids → different keys / different bytes.

    Bug this catches: omitting run_id from the key so the two writes clobber.
    """
    art_a = store.put_bytes("run-a", "x.bin", b"A")
    art_b = store.put_bytes("run-b", "x.bin", b"B")
    assert store.get_bytes(art_a.uri) == b"A"
    assert store.get_bytes(art_b.uri) == b"B"


# --- AC6: list ---------------------------------------------------------------


def test_list_returns_names_for_run_id(store) -> None:  # noqa: ANN001
    """list(run_id) returns the name strings as passed to put_bytes.

    Bug this catches: returning full object Keys instead of name-relative paths.
    """
    store.put_bytes("rx", "a.bin", b"a")
    store.put_bytes("rx", "b.bin", b"b")
    assert sorted(store.list("rx")) == ["a.bin", "b.bin"]


def test_list_nested_name_preserves_subpath(store) -> None:  # noqa: ANN001
    """A name with subdirectory components survives list() unchanged.

    Bug this catches: list() strips '/' so 'profiles/abc.json' becomes 'abc.json'.
    """
    store.put_bytes("rx", "profiles/abc.json", b"{}")
    assert "profiles/abc.json" in store.list("rx")


def test_list_empty_run_id_returns_empty_list(store) -> None:  # noqa: ANN001
    """list() for a run_id with no items returns [] (not an error).

    Bug this catches: raising on empty page or on missing 'Contents' key.
    """
    assert store.list("never-existed") == []


def test_list_excludes_other_run_ids(store) -> None:  # noqa: ANN001
    """list(run_id) shows only items from that run_id, not sibling run_ids.

    Bug this catches: prefix not strict-bounded by trailing '/'; 'run-1' would
    accidentally include items under 'run-10/'.
    """
    store.put_bytes("run-1", "item.bin", b"1")
    store.put_bytes("run-10", "item.bin", b"10")
    assert store.list("run-1") == ["item.bin"]


# --- AC7: delete -------------------------------------------------------------


def test_delete_removes_item(store) -> None:  # noqa: ANN001
    """delete(uri) removes the object; subsequent get_bytes raises FileNotFoundError.

    Bug this catches: delete() silently no-ops when the key is missing in fake,
    or doesn't actually pop from the underlying dict.
    """
    artifact = store.put_bytes("rid", "to_del.bin", b"bye")
    store.delete(artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(artifact.uri)


def test_delete_missing_raises_file_not_found(store) -> None:  # noqa: ANN001
    """delete() on a non-existent URI raises FileNotFoundError.

    Bug this catches: silently ignoring missing keys (S3 delete_object is
    idempotent — without the head_object check the ABC contract is violated).
    """
    with pytest.raises(FileNotFoundError):
        store.delete("s3://bkt/prefix/never/x.bin")


def test_get_bytes_missing_raises_file_not_found(store) -> None:  # noqa: ANN001
    """get_bytes on a missing key raises FileNotFoundError.

    Bug this catches: NoSuchKey propagates unmapped, breaking caller's
    ABC-contract expectations.
    """
    with pytest.raises(FileNotFoundError):
        store.get_bytes("s3://bkt/prefix/missing/x.bin")


# --- AC8: uri_for invariant --------------------------------------------------


def test_uri_for_matches_put_bytes_artifact_uri(store) -> None:  # noqa: ANN001
    """uri_for(rid, name) == put_bytes(rid, name, b).uri (cross-method invariant).

    Bug this catches: uri_for diverges from put-time URI — JsonProfileCache
    cross-restart reads break against this store.
    """
    artifact = store.put_bytes("rid", "blob.bin", b"x")
    assert store.uri_for("rid", "blob.bin") == artifact.uri


def test_uri_for_matches_put_json_artifact_uri(store) -> None:  # noqa: ANN001
    """uri_for(rid, name) == put_json(rid, name, obj).uri.

    Bug this catches: put_json uses a different key shape than put_bytes;
    uri_for is wired to one path but not the other.
    """
    artifact = store.put_json("rid", "data.json", {"k": 1})
    assert store.uri_for("rid", "data.json") == artifact.uri


# --- AC9: self-registration --------------------------------------------------


def test_s3_store_self_registers_under_s3() -> None:
    """Importing kinoforge.stores.s3 registers it under "s3" in the registry.

    Bug this catches: forgetting the register_store("s3", ...) call at the
    module bottom.
    """
    import kinoforge.stores.s3  # noqa: F401 — side-effect import
    from kinoforge.core.registry import get_store

    factory = get_store("s3")
    assert callable(factory)


# --- AC10: lazy SDK import gate ----------------------------------------------


def test_lazy_sdk_import_not_triggered_when_client_injected() -> None:
    """Construction with client=fake never imports boto3.

    Bug this catches: __init__ imports boto3 eagerly (e.g. at module top
    level) — defeats the offline-test invariant and also slows CLI startup.
    """
    sys.modules.pop("boto3", None)
    # We do NOT pop the store module — its import is fine; only boto3 must
    # remain absent because the fake bypasses the lazy gate.
    from kinoforge.stores.s3 import S3ArtifactStore

    S3ArtifactStore(bucket="bkt", client=FakeS3Client())

    assert "boto3" not in sys.modules
```

- [ ] **Step 11: Run all 17 S3 tests — confirm they PASS**

```bash
pixi run test tests/stores/test_s3.py -v
```

Expected: 17 passed.

- [ ] **Step 12: Confirm `_VENDOR_PATTERNS` extension passes (boto3 confined to stores/s3/)**

```bash
pixi run test tests/test_core_invariant.py -v
```

Expected: 3 passed (all pre-existing invariant tests still green; new patterns don't add violations because the lazy `import boto3` line is inside `src/kinoforge/stores/s3/__init__.py` — within the allowed dir).

- [ ] **Step 13: Run the full test suite + pre-commit + coverage**

```bash
pixi run pre-commit run --files pixi.toml src/kinoforge/stores/s3/__init__.py src/kinoforge/_adapters.py tests/stores/conftest.py tests/stores/test_s3.py tests/test_core_invariant.py
pixi run test
pixi run test-cov
```

Expected: pre-commit all hooks Passed; full suite green (395 pre-existing + 17 S3 = 412+); coverage ≥ 90%.

- [ ] **Step 14: Commit**

```bash
git add pixi.toml pixi.lock src/kinoforge/stores/s3/__init__.py src/kinoforge/_adapters.py tests/stores/conftest.py tests/stores/test_s3.py tests/test_core_invariant.py
git commit -m "$(cat <<'EOF'
feat(stores): add S3ArtifactStore + shared cloud-test fakes + invariant patterns

S3ArtifactStore satisfies the full 7-method ArtifactStore ABC including
uri_for(rid, name) -> 's3://<bucket>/<prefix>/<rid>/<name>'. Uses boto3
default credential chain when client=None; tests inject FakeS3Client to
keep the suite offline. Self-registers under "s3"; default factory reads
KINOFORGE_S3_BUCKET + optional KINOFORGE_S3_PREFIX from env.

Bundles both cloud-SDK pixi deps (boto3 + google-cloud-storage) and both
_VENDOR_PATTERNS entries (boto3 -> stores/s3, google.cloud -> stores/gcs)
to land the floor for Task 2's GCS work without an infrastructure-only
commit.

tests/stores/conftest.py introduces FakeS3Client + FakeGCSClient + helper
doubles shared between this task and Task 2's GCS tests.

Refs #5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `GCSArtifactStore` — store impl + adapters wire + 17 tests

**Goal:** Mirror Task 1 for GCS. Same 17-test surface, gs:// scheme, GCS-specific SDK shape (`Client.bucket(name).blob(key).upload_from_string()` / `.download_as_bytes()` / `.delete()`). After this task, both cloud stores are fully functional through the registry.

**Files:**
- Create: `src/kinoforge/stores/gcs/__init__.py`
- Modify: `src/kinoforge/_adapters.py` (add the GCS import line)
- Create: `tests/stores/test_gcs.py` (17 tests)

**Acceptance Criteria:**
- [ ] `GCSArtifactStore(ArtifactStore)` implements all 7 abstract methods
- [ ] `uri_for(run_id, name)` returns `f"gs://{bucket}/{prefix/run_id/name}"`
- [ ] `put_bytes(...).uri == uri_for(...)` and `put_json(...).uri == uri_for(...)`
- [ ] `get_bytes` / `delete` on missing key → `FileNotFoundError(uri)`
- [ ] Constructing with `client=fake` AND `not_found_exc=FakeGCSClient.NotFound` does NOT trigger `import google.cloud.storage` OR `import google.api_core.exceptions` (both lazy gates proven by sys.modules snapshot test)
- [ ] Self-registers under `"gcs"`; default factory raises a helpful `RuntimeError` when `KINOFORGE_GCS_BUCKET` env is unset
- [ ] `_adapters.py` includes `import kinoforge.stores.gcs` under the `# Stores` section
- [ ] All 17 `test_gcs.py` tests pass; existing tests still pass
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test-cov` reports coverage ≥ 90%

**Verify:** `pixi run test tests/stores/test_gcs.py tests/test_core_invariant.py -v && pixi run test` → 17 new tests + invariant + full suite green.

**Steps:**

- [ ] **Step 1: Write the first failing test in `tests/stores/test_gcs.py`**

Create `/workspace/tests/stores/test_gcs.py`:

```python
"""Tests for GCSArtifactStore — all run against FakeGCSClient (no network).

Spec: docs/superpowers/specs/2026-05-29-s3-gcs-stores-design.md §3.2 + §8.2
"""

from __future__ import annotations

import sys

import pytest

from tests.stores.conftest import FakeGCSClient


@pytest.fixture()
def fake_client() -> FakeGCSClient:
    return FakeGCSClient()


@pytest.fixture()
def store(fake_client: FakeGCSClient):  # noqa: ANN201
    from kinoforge.stores.gcs import GCSArtifactStore

    return GCSArtifactStore(
        bucket="bkt",
        prefix="prefix",
        client=fake_client,
        not_found_exc=FakeGCSClient.NotFound,
    )


# --- AC1: put_bytes returns gs://-scheme'd Artifact --------------------------


def test_put_bytes_returns_artifact_with_gs_uri(store) -> None:  # noqa: ANN001
    """put_bytes returns Artifact with uri = gs://<bucket>/<prefix>/<run_id>/<name>.

    Bug this catches: scheme typo (gcs:// vs gs://) or path-style URI.
    """
    artifact = store.put_bytes("run-1", "out.bin", b"\x00\x01")
    assert artifact.uri == "gs://bkt/prefix/run-1/out.bin"
```

- [ ] **Step 2: Run the first test — confirm it FAILS**

```bash
pixi run test tests/stores/test_gcs.py::test_put_bytes_returns_artifact_with_gs_uri -v
```

Expected: `ModuleNotFoundError: No module named 'kinoforge.stores.gcs'`.

- [ ] **Step 3: Create `src/kinoforge/stores/gcs/__init__.py`**

Create `/workspace/src/kinoforge/stores/gcs/__init__.py`:

```python
"""Google Cloud Storage-backed ArtifactStore.

Self-registers under ``"gcs"`` on import.  Default zero-arg factory reads
``KINOFORGE_GCS_BUCKET`` (+ optional ``KINOFORGE_GCS_PREFIX``); library users
construct ``GCSArtifactStore(bucket=..., prefix=..., client=..., not_found_exc=...)``
directly.

Both the SDK client AND the ``NotFound`` exception class are injectable so
tests pass *both* parameters to bypass the two lazy-import gates inside
``__init__``.
"""

from __future__ import annotations

import json
import os
from typing import Any

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore


class GCSArtifactStore(ArtifactStore):
    """ArtifactStore backed by Google Cloud Storage.

    Storage layout: ``gs://<bucket>/<prefix>/<run_id>/<name>``.

    Attributes:
        bucket: Target GCS bucket name.
        prefix: Optional key prefix; normalised to have no leading or trailing slash.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        not_found_exc: type[BaseException] | None = None,
    ) -> None:
        """Initialise the store.

        Args:
            bucket: Target GCS bucket name.
            prefix: Optional key prefix.  Leading and trailing slashes are stripped.
            client: Optional ``google.cloud.storage.Client``.  When ``None``,
                a real client is lazily constructed (uses gcloud ADC).
            not_found_exc: Optional exception class to catch as "missing key".
                When ``None``, lazily imports ``google.api_core.exceptions.NotFound``.
                Tests must pass both ``client`` AND ``not_found_exc`` to bypass
                both lazy-import gates.
        """
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            from google.cloud import storage  # noqa: PLC0415 — lazy

            client = storage.Client()
        if not_found_exc is None:
            from google.api_core.exceptions import NotFound  # noqa: PLC0415 — lazy

            not_found_exc = NotFound
        self._bucket_handle: Any = client.bucket(bucket)
        self._not_found_exc: type[BaseException] = not_found_exc

    def _key(self, run_id: str, name: str) -> str:
        """Return the absolute object key for ``(run_id, name)``."""
        parts = [p for p in (self.prefix, run_id, name) if p]
        return "/".join(parts)

    @staticmethod
    def _split_uri(uri: str) -> tuple[str, str]:
        """Split a ``gs://bucket/key`` URI into ``(bucket, key)``."""
        if not uri.startswith("gs://"):
            raise ValueError(f"not a gs:// uri: {uri!r}")
        bucket, _, key = uri[len("gs://") :].partition("/")
        return bucket, key

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        """Return the GCS URI for ``(run_id, name)`` — pure, no I/O."""
        return f"gs://{self.bucket}/{self._key(run_id, name)}"

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        """Write ``data`` under ``<bucket>/<prefix>/<run_id>/<name>`` (auto-multipart over SDK threshold)."""
        key = self._key(run_id, name)
        self._bucket_handle.blob(key).upload_from_string(data)
        return Artifact(uri=f"gs://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        """Read the bytes at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            return self._bucket_handle.blob(key).download_as_bytes()  # type: ignore[no-any-return]
        except self._not_found_exc:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        """Serialise ``obj`` as UTF-8 JSON and persist under ``<run_id>/<name>``."""
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        """Deserialise and return the JSON object stored at ``uri``."""
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        """Enumerate names stored under ``run_id`` (relative to ``<prefix>/<run_id>/``)."""
        run_prefix = self._key(run_id, "") + "/"
        return [
            blob.name[len(run_prefix) :]
            for blob in self._bucket_handle.list_blobs(prefix=run_prefix)
        ]

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise FileNotFoundError on miss."""
        _, key = self._split_uri(uri)
        try:
            self._bucket_handle.blob(key).delete()
        except self._not_found_exc:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402


def _default_factory() -> GCSArtifactStore:
    """Zero-arg factory reading bucket + prefix from env."""
    bucket = os.environ.get("KINOFORGE_GCS_BUCKET")
    if not bucket:
        raise RuntimeError(
            "GCSArtifactStore default factory needs KINOFORGE_GCS_BUCKET; "
            "either set the env var or construct GCSArtifactStore(bucket=...) directly."
        )
    return GCSArtifactStore(
        bucket=bucket, prefix=os.environ.get("KINOFORGE_GCS_PREFIX", "")
    )


register_store("gcs", _default_factory)
```

- [ ] **Step 4: Run the first test — confirm it PASSES**

```bash
pixi run test tests/stores/test_gcs.py::test_put_bytes_returns_artifact_with_gs_uri -v
```

Expected: 1 passed.

- [ ] **Step 5: Wire `gcs` into `_adapters.py`**

Open `/workspace/src/kinoforge/_adapters.py`. The `# Stores` section currently reads (after Task 1):

```python
# Stores
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3     # noqa: F401
```

Add the GCS line:

```python
# Stores
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3     # noqa: F401
import kinoforge.stores.gcs    # noqa: F401
```

- [ ] **Step 6: Add the remaining 16 tests to `tests/stores/test_gcs.py`**

Append the following to `/workspace/tests/stores/test_gcs.py`:

```python
# --- AC2: get_bytes round-trips ----------------------------------------------


def test_get_bytes_round_trips(store) -> None:  # noqa: ANN001
    """Bytes written by put_bytes are recovered exactly by get_bytes(uri).

    Bug this catches: download_as_bytes hits the wrong blob name.
    """
    artifact = store.put_bytes("run-1", "blob.bin", b"hello gcs")
    assert store.get_bytes(artifact.uri) == b"hello gcs"


# --- AC3: prefix handling ----------------------------------------------------


def test_put_get_with_prefix(store, fake_client) -> None:  # noqa: ANN001
    """Non-empty prefix is folded into the blob name.

    Bug this catches: prefix concatenated to URI but not to blob name.
    """
    store.put_bytes("rid", "a.bin", b"x")
    bucket = fake_client.bucket("bkt")
    assert "prefix/rid/a.bin" in bucket._blobs


def test_put_get_with_empty_prefix(fake_client) -> None:  # noqa: ANN001
    """Empty prefix produces no leading slash in blob name.

    Bug this catches: '' prefix yielding key '/rid/name'.
    """
    from kinoforge.stores.gcs import GCSArtifactStore

    store = GCSArtifactStore(
        bucket="bkt",
        prefix="",
        client=fake_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "gs://bkt/rid/a.bin"
    bucket = fake_client.bucket("bkt")
    assert "rid/a.bin" in bucket._blobs


def test_put_get_with_slash_normalised_prefix(fake_client) -> None:  # noqa: ANN001
    """Leading and trailing slashes in prefix are stripped during init.

    Bug this catches: blind concatenation producing '/foo/bar//rid/name'.
    """
    from kinoforge.stores.gcs import GCSArtifactStore

    store = GCSArtifactStore(
        bucket="bkt",
        prefix="/foo/bar/",
        client=fake_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "gs://bkt/foo/bar/rid/a.bin"


# --- AC4: put_json round-trips -----------------------------------------------


def test_put_json_round_trips(store) -> None:  # noqa: ANN001
    """A dict written by put_json is recovered as an equivalent dict.

    Bug this catches: encoding drift on read (e.g. int->str).
    """
    obj = {"key": "value", "count": 42, "nested": {"x": 1.5}}
    artifact = store.put_json("rid", "data.json", obj)
    assert store.get_json(artifact.uri) == obj


# --- AC5: run_id isolation ---------------------------------------------------


def test_run_ids_are_isolated(store) -> None:  # noqa: ANN001
    """Same name, different run_ids → different blob names, different bytes.

    Bug this catches: omitting run_id from the blob name.
    """
    art_a = store.put_bytes("run-a", "x.bin", b"A")
    art_b = store.put_bytes("run-b", "x.bin", b"B")
    assert store.get_bytes(art_a.uri) == b"A"
    assert store.get_bytes(art_b.uri) == b"B"


# --- AC6: list ---------------------------------------------------------------


def test_list_returns_names_for_run_id(store) -> None:  # noqa: ANN001
    """list(run_id) returns the name strings as passed to put_bytes.

    Bug this catches: returning full blob names with prefix still attached.
    """
    store.put_bytes("rx", "a.bin", b"a")
    store.put_bytes("rx", "b.bin", b"b")
    assert sorted(store.list("rx")) == ["a.bin", "b.bin"]


def test_list_nested_name_preserves_subpath(store) -> None:  # noqa: ANN001
    """A name with subdirectory components survives list() unchanged.

    Bug this catches: '/' stripped — 'profiles/abc.json' becomes 'abc.json'.
    """
    store.put_bytes("rx", "profiles/abc.json", b"{}")
    assert "profiles/abc.json" in store.list("rx")


def test_list_empty_run_id_returns_empty_list(store) -> None:  # noqa: ANN001
    """list() for a run_id with no items returns [] (not an error).

    Bug this catches: list_blobs iterator unhandled when empty.
    """
    assert store.list("never-existed") == []


def test_list_excludes_other_run_ids(store) -> None:  # noqa: ANN001
    """list(run_id) shows only items from that run_id, not sibling run_ids.

    Bug this catches: prefix not strict-bounded — 'run-1' accidentally
    includes items under 'run-10/'.
    """
    store.put_bytes("run-1", "item.bin", b"1")
    store.put_bytes("run-10", "item.bin", b"10")
    assert store.list("run-1") == ["item.bin"]


# --- AC7: delete -------------------------------------------------------------


def test_delete_removes_item(store) -> None:  # noqa: ANN001
    """delete(uri) removes the blob; subsequent get_bytes raises FileNotFoundError.

    Bug this catches: delete() targets wrong blob name.
    """
    artifact = store.put_bytes("rid", "to_del.bin", b"bye")
    store.delete(artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(artifact.uri)


def test_delete_missing_raises_file_not_found(store) -> None:  # noqa: ANN001
    """delete() on a non-existent URI raises FileNotFoundError.

    Bug this catches: NotFound from blob.delete propagates unmapped.
    """
    with pytest.raises(FileNotFoundError):
        store.delete("gs://bkt/prefix/never/x.bin")


def test_get_bytes_missing_raises_file_not_found(store) -> None:  # noqa: ANN001
    """get_bytes on a missing key raises FileNotFoundError.

    Bug this catches: NotFound from download_as_bytes propagates unmapped.
    """
    with pytest.raises(FileNotFoundError):
        store.get_bytes("gs://bkt/prefix/missing/x.bin")


# --- AC8: uri_for invariant --------------------------------------------------


def test_uri_for_matches_put_bytes_artifact_uri(store) -> None:  # noqa: ANN001
    """uri_for(rid, name) == put_bytes(rid, name, b).uri."""
    artifact = store.put_bytes("rid", "blob.bin", b"x")
    assert store.uri_for("rid", "blob.bin") == artifact.uri


def test_uri_for_matches_put_json_artifact_uri(store) -> None:  # noqa: ANN001
    """uri_for(rid, name) == put_json(rid, name, obj).uri."""
    artifact = store.put_json("rid", "data.json", {"k": 1})
    assert store.uri_for("rid", "data.json") == artifact.uri


# --- AC9: self-registration --------------------------------------------------


def test_gcs_store_self_registers_under_gcs() -> None:
    """Importing kinoforge.stores.gcs registers it under "gcs" in the registry.

    Bug this catches: forgetting register_store("gcs", ...) at module bottom.
    """
    import kinoforge.stores.gcs  # noqa: F401 — side-effect import
    from kinoforge.core.registry import get_store

    factory = get_store("gcs")
    assert callable(factory)


# --- AC10: dual lazy-import gate --------------------------------------------


def test_lazy_sdk_import_not_triggered_when_both_injected() -> None:
    """Constructing with client=fake AND not_found_exc=fake never imports SDK.

    Bug this catches: __init__ imports google.cloud.storage or
    google.api_core.exceptions eagerly — defeats offline-test invariant.
    Both lazy gates must hold.
    """
    sys.modules.pop("google.cloud.storage", None)
    sys.modules.pop("google.api_core.exceptions", None)
    from kinoforge.stores.gcs import GCSArtifactStore

    GCSArtifactStore(
        bucket="bkt",
        client=FakeGCSClient(),
        not_found_exc=FakeGCSClient.NotFound,
    )

    assert "google.cloud.storage" not in sys.modules
    assert "google.api_core.exceptions" not in sys.modules
```

- [ ] **Step 7: Run all 17 GCS tests — confirm they PASS**

```bash
pixi run test tests/stores/test_gcs.py -v
```

Expected: 17 passed.

- [ ] **Step 8: Confirm `_VENDOR_PATTERNS` extension still passes (google.cloud confined to stores/gcs/)**

```bash
pixi run test tests/test_core_invariant.py -v
```

Expected: 3 passed. The `google.cloud` pattern entry (landed in Task 1) now has a real importer in `src/kinoforge/stores/gcs/__init__.py` — which is the allowed dir, so no violation.

- [ ] **Step 9: Run pre-commit + full suite + coverage**

```bash
pixi run pre-commit run --files src/kinoforge/stores/gcs/__init__.py src/kinoforge/_adapters.py tests/stores/test_gcs.py
pixi run test
pixi run test-cov
```

Expected: pre-commit all Passed; full suite green (412 + 17 = 429+); coverage ≥ 90%.

- [ ] **Step 10: Commit**

```bash
git add src/kinoforge/stores/gcs/__init__.py src/kinoforge/_adapters.py tests/stores/test_gcs.py
git commit -m "$(cat <<'EOF'
feat(stores): add GCSArtifactStore (mirror of S3, gs:// scheme)

GCSArtifactStore satisfies the 7-method ArtifactStore ABC including
uri_for(rid, name) -> 'gs://<bucket>/<prefix>/<rid>/<name>'. Both SDK
client AND NotFound exception class are injected via __init__ kwargs so
the two lazy-import gates (google.cloud.storage + google.api_core.exceptions)
hold under the offline-test path. Self-registers under "gcs"; default
factory reads KINOFORGE_GCS_BUCKET + optional KINOFORGE_GCS_PREFIX.

17 tests mirror the test_s3.py shape — 1:1 ACs except for the dual-gate
sys.modules snapshot test, which now snapshots both SDK module names.

Refs #5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `StoreConfig` pydantic block + tests + YAML example

**Goal:** Add the optional `store:` config block users will eventually fill to switch CLI from `LocalArtifactStore` to S3 or GCS. Pure schema work — the wiring into the CLI happens in Task 4. After this task, `Config.store` exists with sensible defaults and validates required-field invariants.

**Files:**
- Modify: `src/kinoforge/core/config.py` (add `StoreConfig` class + `Config.store` field)
- Modify: `tests/core/test_config.py` (add 6 new `StoreConfig` tests)
- Modify: `examples/configs/wan.yaml` (append a commented `store:` block as documentation)

**Acceptance Criteria:**
- [ ] `StoreConfig` exists with `kind: Literal["local", "s3", "gcs"]` (default `"local"`), `root: Path | None` (default `None`), `bucket: str | None` (default `None`), `prefix: str` (default `""`)
- [ ] `model_validator(mode="after")` raises `ValueError` when `kind in {"s3","gcs"}` and `bucket` is unset
- [ ] Same validator raises `ValueError` when `kind == "local"` and `bucket` is set
- [ ] `Config.store: StoreConfig = Field(default_factory=StoreConfig)` — absent block parses as `kind="local"`, `root=None`
- [ ] All 6 new tests pass; all existing config tests still pass
- [ ] `examples/configs/wan.yaml` ends with a commented `store:` block matching spec §4 YAML examples
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test-cov` reports coverage ≥ 90%

**Verify:** `pixi run test tests/core/test_config.py -v && pixi run test` → 6 new tests + existing config tests + full suite green.

**Steps:**

- [ ] **Step 1: Locate where to add `StoreConfig` and write the failing tests first**

Add these 6 tests to `/workspace/tests/core/test_config.py`. Append them at the end of the file (after the existing tests):

```python
# ---------------------------------------------------------------------------
# StoreConfig — Phase 13 / Layer C
# ---------------------------------------------------------------------------


def test_default_store_is_local_kind() -> None:
    """When no store block is present, Config.store defaults to kind='local'.

    Bug this catches: default_factory not wired, or default kind != 'local' —
    breaking backwards compat for every pre-Layer-C config file.
    """
    import yaml

    from kinoforge.core.config import load_config

    cfg_yaml = yaml.safe_dump(
        {
            "models": [],
            "engine": {"name": "fake"},
            "lifecycle": {"idle_timeout": "10m", "max_lifetime": "1h"},
            "requirements": {"min_vram": 16, "gpu_preference": ["A100", "H100"]},
            "compute": {"provider": "local"},
        }
    )
    cfg = load_config(cfg_yaml)
    assert cfg.store.kind == "local"
    assert cfg.store.root is None
    assert cfg.store.bucket is None
    assert cfg.store.prefix == ""


def test_s3_kind_requires_bucket() -> None:
    """store.kind='s3' without store.bucket raises pydantic ValidationError.

    Bug this catches: validator silently accepts incomplete config, leading
    to runtime failure deep inside generate() instead of upfront load error.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="bucket"):
        StoreConfig(kind="s3")


def test_gcs_kind_requires_bucket() -> None:
    """store.kind='gcs' without store.bucket raises pydantic ValidationError.

    Bug this catches: validator handles only the s3 case.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="bucket"):
        StoreConfig(kind="gcs")


def test_local_kind_rejects_bucket() -> None:
    """store.kind='local' with store.bucket set raises pydantic ValidationError.

    Bug this catches: validator only guards one direction — users who mistype
    kind='local' but include bucket get a silently misconfigured store.
    """
    from pydantic import ValidationError

    from kinoforge.core.config import StoreConfig

    with pytest.raises(ValidationError, match="local"):
        StoreConfig(kind="local", bucket="should-not-be-here")


def test_prefix_defaults_to_empty_string() -> None:
    """store.prefix defaults to '' when absent (not None, not 'default').

    Bug this catches: prefix typed as Optional with None default, breaking
    string-concat in store._key.
    """
    from kinoforge.core.config import StoreConfig

    cfg = StoreConfig(kind="s3", bucket="b")
    assert cfg.prefix == ""


def test_parses_full_s3_block_from_yaml() -> None:
    """A full store block round-trips through load_config.

    Bug this catches: pydantic discriminator gets stuck on kind='s3' or the
    StoreConfig field isn't merged into Config correctly.
    """
    import yaml

    from kinoforge.core.config import load_config

    cfg_yaml = yaml.safe_dump(
        {
            "models": [],
            "engine": {"name": "fake"},
            "lifecycle": {"idle_timeout": "10m", "max_lifetime": "1h"},
            "requirements": {"min_vram": 16, "gpu_preference": ["A100", "H100"]},
            "compute": {"provider": "local"},
            "store": {
                "kind": "s3",
                "bucket": "my-org-kinoforge",
                "prefix": "prod/runs",
            },
        }
    )
    cfg = load_config(cfg_yaml)
    assert cfg.store.kind == "s3"
    assert cfg.store.bucket == "my-org-kinoforge"
    assert cfg.store.prefix == "prod/runs"
```

If `pytest` is not yet imported at the top of `test_config.py`, the import statement is already there (per existing tests using it); confirm with a quick grep before proceeding.

- [ ] **Step 2: Run the new tests — confirm they FAIL**

```bash
pixi run test tests/core/test_config.py::test_default_store_is_local_kind tests/core/test_config.py::test_s3_kind_requires_bucket -v
```

Expected: AttributeError or ValidationError on missing `StoreConfig` / missing `Config.store`.

- [ ] **Step 3: Add `StoreConfig` + `Config.store` to `src/kinoforge/core/config.py`**

Open `/workspace/src/kinoforge/core/config.py`. Locate the existing `Config` pydantic model. Before the `Config` class definition, add the new `StoreConfig` class. Verify the existing imports include `Literal`, `BaseModel`, `Field`, `model_validator`, and `Path` — add any missing ones.

```python
class StoreConfig(BaseModel):
    """Optional artifact-store selector.

    Absent block defaults to ``kind="local"``, ``root=None`` — the CLI then
    constructs ``LocalArtifactStore(state_dir)`` from the ``--state-dir``
    argument, matching the pre-Layer-C behaviour.

    Attributes:
        kind: One of ``"local"``, ``"s3"``, ``"gcs"``. Defaults to ``"local"``.
        root: Local-store root directory. Optional; ``None`` → CLI's ``--state-dir``.
        bucket: Cloud bucket name. Required when ``kind in {"s3", "gcs"}``;
            rejected when ``kind == "local"``.
        prefix: Cloud key prefix. Defaults to empty string.
    """

    kind: Literal["local", "s3", "gcs"] = "local"
    root: Path | None = None
    bucket: str | None = None
    prefix: str = ""

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> "StoreConfig":
        """Enforce kind ↔ bucket cross-field invariants."""
        if self.kind in ("s3", "gcs") and not self.bucket:
            raise ValueError(f"store.kind={self.kind!r} requires store.bucket")
        if self.kind == "local" and self.bucket:
            raise ValueError("store.kind='local' does not accept store.bucket")
        return self
```

Then add a new field to the existing `Config` class (keep all existing fields intact — add `store` after them):

```python
    store: StoreConfig = Field(default_factory=StoreConfig)
```

- [ ] **Step 4: Run the 6 new tests — confirm they PASS**

```bash
pixi run test tests/core/test_config.py -v -k "store"
```

Expected: 6 passed.

- [ ] **Step 5: Append the commented `store:` example to `examples/configs/wan.yaml`**

Open `/workspace/examples/configs/wan.yaml`. Append the following block at the very end of the file (preserve the existing content; this is documentation only):

```yaml

# --- Optional: artifact-store selection (Phase 13 / Layer C) -----------------
# Absent block → LocalArtifactStore(state_dir) where state_dir is --state-dir.
#
# Explicit local with custom root:
# store:
#   kind: local
#   root: ./my-custom-root
#
# S3:
# store:
#   kind: s3
#   bucket: my-org-kinoforge
#   prefix: prod/runs
#
# GCS:
# store:
#   kind: gcs
#   bucket: my-org-kinoforge
#   prefix: prod/runs
```

Other example configs (`diffusers.yaml`, `hosted.yaml`, `local-fake.yaml`) are NOT modified — they prove that the absent-block default still parses correctly via the existing example-parsing tests in `tests/test_examples.py`.

- [ ] **Step 6: Run the full suite + pre-commit + coverage**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py examples/configs/wan.yaml
pixi run test
pixi run test-cov
```

Expected: pre-commit all Passed; full suite green (429 + 6 = 435+); coverage ≥ 90%.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/config.py tests/core/test_config.py examples/configs/wan.yaml
git commit -m "$(cat <<'EOF'
feat(config): add optional StoreConfig pydantic block

New Config.store: StoreConfig field, default kind='local' so configs
without a store block parse unchanged. kind in {s3, gcs} requires bucket;
kind='local' rejects bucket. Pure schema work; CLI wiring lands in the
next commit. examples/configs/wan.yaml gains a commented store: block
documenting the three forms (local / s3 / gcs).

Refs #5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: CLI `_build_store` helper + 3 call-site swaps + 3 tests + Layer-A `_path` peek fix

**Goal:** Wire the new `StoreConfig` into the CLI so end-users can switch stores via YAML. Backwards-compatible: absent `store:` block → `LocalArtifactStore(state_dir)` as today. Same task fixes the Layer-A leftover `store._path(run_id, name)` peek at `cli.py:441` by routing through `store.uri_for(...)`.

**Files:**
- Modify: `src/kinoforge/cli.py` (add `_build_store(cfg, state_dir)`; swap 2 call sites; replace 1 `_path` peek)
- Modify: `tests/test_cli.py` (add 3 tests; preserve all existing CLI tests)

**Acceptance Criteria:**
- [ ] `_build_store(cfg: Config, state_dir: Path) -> ArtifactStore` exists in `cli.py`
- [ ] When `cfg.store.kind == "local"`, returns `LocalArtifactStore(cfg.store.root or state_dir)`
- [ ] When `cfg.store.kind == "s3"`, lazily imports `S3ArtifactStore` and returns `S3ArtifactStore(bucket=cfg.store.bucket, prefix=cfg.store.prefix)`
- [ ] When `cfg.store.kind == "gcs"`, lazily imports `GCSArtifactStore` and returns `GCSArtifactStore(bucket=cfg.store.bucket, prefix=cfg.store.prefix)`
- [ ] `_cmd_generate` at `cli.py:265` now calls `_build_store(cfg, state_dir)`
- [ ] `_cmd_gc` at `cli.py:434` now calls `_build_store(cfg, state_dir)`
- [ ] `_cmd_gc` at `cli.py:441` now calls `store.uri_for(run_id, name)` (replaces `str(store._path(run_id, name))`)
- [ ] `_ledger(state_dir)` at `cli.py:41-51` is UNCHANGED — ledger stays local-backed per spec §5.2
- [ ] Test: `cli_generate_uses_local_when_store_block_absent` passes
- [ ] Test: `cli_generate_uses_s3_when_store_kind_s3` passes
- [ ] Test: `cli_gc_uses_store_uri_for_not_path_peek` passes (proves the peek is gone)
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test-cov` reports coverage ≥ 90%

**Verify:** `pixi run test tests/test_cli.py -v && pixi run test` → 3 new tests + all 8 existing CLI tests + full suite green.

**Steps:**

- [ ] **Step 1: Read the current CLI structure for the 3 sites to modify**

```bash
grep -n "LocalArtifactStore\|store\._path\|_cmd_generate\|_cmd_gc" /workspace/src/kinoforge/cli.py
```

Expected output identifies:
- Line 34: `from kinoforge.stores.local import LocalArtifactStore`
- Line 50: `store = LocalArtifactStore(state_dir)` inside `_ledger` (NOT changed)
- Line 265: `store = LocalArtifactStore(state_dir)` inside `_cmd_generate` (CHANGE)
- Line 434: `store = LocalArtifactStore(state_dir)` inside `_cmd_gc` (CHANGE)
- Line 441: `uri = str(store._path(run_id, name))` inside `_cmd_gc` loop (CHANGE — replace with `store.uri_for`)

If line numbers have drifted, locate the surrounding context for each change before applying it.

- [ ] **Step 2: Write the first failing test in `tests/test_cli.py`**

Open `/workspace/tests/test_cli.py`. Append the following 3 tests at the end of the file:

```python
# ---------------------------------------------------------------------------
# Phase 13 / Layer C — store selection via config
# ---------------------------------------------------------------------------


def test_cli_generate_uses_local_when_store_block_absent(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """Absent store: block → CLI uses LocalArtifactStore(state_dir).

    Bug this catches: _build_store regression breaks backwards compat for
    every config file written before Phase 13.
    """
    from kinoforge.cli import _build_store
    from kinoforge.core.config import Config, StoreConfig
    from kinoforge.stores.local import LocalArtifactStore

    cfg = Config.model_construct(store=StoreConfig())  # defaults: local, root=None
    store = _build_store(cfg, tmp_path)

    assert isinstance(store, LocalArtifactStore)
    assert store.root == tmp_path.resolve()


def test_cli_generate_uses_s3_when_store_kind_s3(tmp_path) -> None:  # noqa: ANN001
    """store.kind='s3' → _build_store returns an S3ArtifactStore.

    Bug this catches: _build_store branch missing or constructs with the
    wrong bucket/prefix arguments.
    """
    from kinoforge.cli import _build_store
    from kinoforge.core.config import Config, StoreConfig
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    cfg = Config.model_construct(
        store=StoreConfig(kind="s3", bucket="my-bkt", prefix="some/prefix")
    )
    # Monkeypatch boto3.client to a no-arg fake so the real SDK isn't touched
    # during construction. _build_store doesn't inject client= — the lazy
    # gate inside S3ArtifactStore.__init__ would fire; we satisfy it by
    # putting a fake in sys.modules under boto3 so the import resolves.
    import sys
    import types

    fake_boto3 = types.SimpleNamespace(client=lambda _: FakeS3Client())
    sys.modules["boto3"] = fake_boto3  # type: ignore[assignment]
    try:
        store = _build_store(cfg, tmp_path)
    finally:
        sys.modules.pop("boto3", None)

    assert isinstance(store, S3ArtifactStore)
    assert store.bucket == "my-bkt"
    assert store.prefix == "some/prefix"


def test_cli_gc_uses_store_uri_for_not_path_peek(
    tmp_path, monkeypatch
) -> None:  # noqa: ANN001
    """cli._cmd_gc calls store.uri_for(...) — never store._path(...) anymore.

    Bug this catches: Layer A's cleanup pattern was applied to JsonProfileCache
    but missed cli.py:441; this test pins the fix so a future refactor can't
    silently reintroduce the private-attr peek.
    """
    import re

    cli_src = Path("/workspace/src/kinoforge/cli.py").read_text()

    # The private-attr peek must be gone.
    assert "store._path" not in cli_src, "cli.py still calls store._path; Layer C should have replaced it with store.uri_for"

    # And uri_for must be called somewhere in the file.
    assert re.search(r"\.uri_for\s*\(", cli_src), "cli.py never calls .uri_for(...)"
```

- [ ] **Step 3: Run the new tests — confirm they FAIL**

```bash
pixi run test tests/test_cli.py -v -k "store_block_absent or store_kind_s3 or uri_for_not_path_peek"
```

Expected: 3 failures —
- `test_cli_generate_uses_local_when_store_block_absent` → `ImportError: cannot import name '_build_store'`
- Same for the s3 test
- `test_cli_gc_uses_store_uri_for_not_path_peek` → `AssertionError: cli.py still calls store._path`

- [ ] **Step 4: Add `_build_store` to `src/kinoforge/cli.py`**

Open `/workspace/src/kinoforge/cli.py`. Locate the import block near the top (around lines 30-40). Confirm `ArtifactStore` is importable; if not, add:

```python
from kinoforge.stores.base import ArtifactStore
```

Then add the helper function. A good place is just below the existing `_ledger` helper (around line 52), so it sits next to other store-related helpers. The full helper:

```python
def _build_store(cfg: "Config", state_dir: Path) -> ArtifactStore:
    """Construct the artifact store for this run.

    Honours ``cfg.store.kind``; falls back to ``LocalArtifactStore(state_dir)``
    when ``cfg.store`` is at its defaults (``kind='local'``, ``root=None``) —
    i.e. when no ``store:`` block is present in the YAML config.

    Args:
        cfg: Loaded kinoforge ``Config``.
        state_dir: Path to the operator state directory (``--state-dir`` arg).

    Returns:
        A fresh ``ArtifactStore`` instance.

    Raises:
        UnknownAdapter: ``cfg.store.kind`` is not one of ``local | s3 | gcs``.
    """
    sc = cfg.store
    if sc.kind == "local":
        return LocalArtifactStore(sc.root or state_dir)
    if sc.kind == "s3":
        from kinoforge.stores.s3 import S3ArtifactStore  # noqa: PLC0415 — lazy

        assert sc.bucket is not None  # validated by StoreConfig._check_kind_requirements
        return S3ArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    if sc.kind == "gcs":
        from kinoforge.stores.gcs import GCSArtifactStore  # noqa: PLC0415 — lazy

        assert sc.bucket is not None
        return GCSArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    raise UnknownAdapter(f"unknown store kind: {sc.kind!r}")
```

The `"Config"` forward-string annotation avoids a top-level import of `Config` if the file doesn't already import it at module scope — most CLI files import `Config` lazily inside command bodies. Add a real `from kinoforge.core.config import Config` to the top of `cli.py` if it isn't there yet.

- [ ] **Step 5: Swap the 2 call sites in `_cmd_generate` and `_cmd_gc`**

At `cli.py:265` (inside `_cmd_generate`), change:

```python
    store = LocalArtifactStore(state_dir)
```

to:

```python
    store = _build_store(cfg, state_dir)
```

At `cli.py:434` (inside `_cmd_gc`), change:

```python
    store = LocalArtifactStore(state_dir)
```

to:

```python
    store = _build_store(cfg, state_dir)
```

`_cmd_gc` does not currently load a `cfg`. Check whether `cfg` is already in scope inside `_cmd_gc`. If not, load it the same way `_cmd_generate` does — locate `_cmd_generate`'s `cfg = load_config(...)` line and copy the pattern into `_cmd_gc` before the new `_build_store` call. If `_cmd_gc` doesn't take `--config`, add the argument to its parser block (around line 136 `p_gc = sub.add_parser("gc", ...)`) so it gains a `--config` flag that defaults to the same path the other commands use.

- [ ] **Step 6: Replace the `_path` peek at `cli.py:441`**

Change:

```python
            uri = str(store._path(run_id, name))
```

to:

```python
            uri = store.uri_for(run_id, name)
```

`store.uri_for` is part of the public ABC (Layer A) and is already a string, so the `str(...)` wrapper is unnecessary.

- [ ] **Step 7: Run the 3 new tests — confirm they PASS**

```bash
pixi run test tests/test_cli.py -v -k "store_block_absent or store_kind_s3 or uri_for_not_path_peek"
```

Expected: 3 passed.

- [ ] **Step 8: Run the full CLI test suite — confirm no regressions**

```bash
pixi run test tests/test_cli.py -v
```

Expected: all CLI tests pass (11 total: 8 pre-existing + 3 new).

- [ ] **Step 9: Run pre-commit + full suite + coverage**

```bash
pixi run pre-commit run --files src/kinoforge/cli.py tests/test_cli.py
pixi run test
pixi run test-cov
```

Expected: pre-commit all Passed; full suite green (435 + 3 = 438+); coverage ≥ 90%.

- [ ] **Step 10: Commit**

```bash
git add src/kinoforge/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): wire StoreConfig into CLI + drop Layer-A leftover _path peek

New _build_store(cfg, state_dir) helper honours cfg.store.kind; absent
block falls back to LocalArtifactStore(state_dir) for backwards compat.
Two _cmd_generate / _cmd_gc construction sites now route through the
helper. Lazy SDK imports keep startup fast and offline-test invariants
intact.

cli.py:441 _cmd_gc now calls store.uri_for(run_id, name) instead of the
private store._path peek — completes the Layer-A cleanup that landed in
JsonProfileCache but missed this call site. Pinned by a test that greps
the source to forbid the regression.

Ledger remains local-backed (cli._ledger unchanged) per spec §5.2 —
cloud-backed ledger intersects issue #7 (cross-process discovery lock)
and is explicitly out of scope.

Refs #5

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: README + PROGRESS docs

**Goal:** Per project `CLAUDE.md` durability rules — keep README's Roadmap honest and update the recovery index. After this task, anyone resuming the project sees Layer C complete + the next layer (probably ComfyUI/Diffusers/Hosted `extract_last_frame` impls, or the next axis layer) staged.

**Files:**
- Modify: `README.md` (drop "S3/GCS stores" from Roadmap; extend the "Extending" section to list three stores)
- Modify: `PROGRESS.md` (append Phase 13 block; repoint "Single next action")

**Acceptance Criteria:**
- [ ] `README.md` Roadmap section no longer lists "S3/GCS artifact stores" as future work
- [ ] `README.md` "Extending" section lists the three concrete stores (`local`, `s3`, `gcs`)
- [ ] `PROGRESS.md` gains "Phase 13 — S3/GCS stores (deferred layer C, GitHub issue #5)" subsection with all 5 Task SHAs
- [ ] `PROGRESS.md` "Single next action" body rewritten — Layer C done + next-layer pointer
- [ ] `pixi run pre-commit run --files README.md PROGRESS.md` green

**Verify:** `git diff README.md PROGRESS.md && pixi run pre-commit run --files README.md PROGRESS.md` → diff shows the two updates; pre-commit green.

**Steps:**

- [ ] **Step 1: Capture the 4 implementation-task SHAs**

```bash
git log --oneline -10
```

Identify the SHAs for:
- Task 1: `feat(stores): add S3ArtifactStore ...` → `<TASK1_SHA>`
- Task 2: `feat(stores): add GCSArtifactStore ...` → `<TASK2_SHA>`
- Task 3: `feat(config): add optional StoreConfig pydantic block` → `<TASK3_SHA>`
- Task 4: `feat(cli): wire StoreConfig into CLI ...` → `<TASK4_SHA>`

- [ ] **Step 2: Update `README.md` — drop "S3/GCS stores" from Roadmap; extend "Extending"**

Open `/workspace/README.md`. Locate the Roadmap section. Find the bullet that mentions S3/GCS artifact stores (likely phrased as "S3 / GCS artifact stores" or similar). Delete that bullet.

Locate the "Extending" section. Find where artifact stores are listed (likely a sub-bullet under the swappable axes). The current text mentions `LocalArtifactStore` only; update to include all three. The line should look something like:

```markdown
- **Artifact store:** `LocalArtifactStore` (filesystem), `S3ArtifactStore` (`s3://`), `GCSArtifactStore` (`gs://`). Register via `register_store(name, factory)`.
```

If the README structure differs from the above, follow the spirit: drop S3/GCS from Roadmap, list all three in Extending. Don't restructure other sections.

- [ ] **Step 3: Append Phase 13 subsection to `PROGRESS.md`**

Locate the existing "Phase 12 — continuity fallback" block under "## Post-MVP". Add directly after it:

```markdown
### Phase 13 — S3 / GCS artifact stores (deferred layer C, GitHub issue #5)
- [x] Task 1: S3ArtifactStore + deps + invariant patterns + adapters wire + 17 tests — commit `<TASK1_SHA>`
- [x] Task 2: GCSArtifactStore + adapters wire + 17 tests — commit `<TASK2_SHA>`
- [x] Task 3: StoreConfig pydantic block + 6 tests + YAML example — commit `<TASK3_SHA>`
- [x] Task 4: CLI _build_store + 3 call-site swaps + 3 tests + Layer-A _path peek fix — commit `<TASK4_SHA>` (closes #5)
```

- [ ] **Step 4: Rewrite "Single next action" section body**

Find the existing "## Single next action" section. Replace its body (keep the heading) with:

```markdown
**Layer C (S3 / GCS stores, issue #5) complete.** All acceptance criteria
met: `pixi run pre-commit run --all-files` clean; `pixi run test-cov`
reports 90%+ coverage; both `S3ArtifactStore` and `GCSArtifactStore`
satisfy the full 7-method `ArtifactStore` ABC with their respective
`s3://` / `gs://` URI schemes; CLI gains an optional `store:` config block
(backwards compatible by default); Layer-A's leftover `cli.py:441`
`store._path` peek now routes through `store.uri_for(...)`. Issue #5
closed. Stitching of N intermediate continuity artifacts remains
deferred (separate issue).

**Next: pick from the layered roadmap.** Three plausible next layers:

1. **ComfyUI / Diffusers / Hosted `extract_last_frame` implementations**
   (no GitHub issue yet; smaller per-engine follow-ups). Worth doing
   before the first real-cloud user trips the post-Layer-B
   `NotImplementedError` on a multi-segment non-native run. Requires
   per-engine decisions on extraction mechanism (PIL? ffmpeg via
   engine's own runtime? hosted-API endpoint?).

2. **Layer #4 — Concurrent backend scheduler (GitHub issue #3).**
   Drop-in `ConcurrentPool` behind the existing `BackendPool` ABC. Pure
   dispatch concern; no other modules touched.

3. **Layer #5 — Keyframe / image-generation upstream Stage (GitHub
   issue #4).** Composable with the splitter via `segments_override`.
   Forces the engine-kind ADR (image-generation engines vs
   video-generation engines on the same `kind` axis vs split axes).

Begin the chosen layer with the
`superpowers-extended-cc:brainstorming` skill.
```

- [ ] **Step 5: Run pre-commit**

```bash
pixi run pre-commit run --files README.md PROGRESS.md
```

Expected: all hooks Passed.

- [ ] **Step 6: Commit**

```bash
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): mark Layer C (S3/GCS stores) complete

Layer C acceptance pass green: pre-commit clean, coverage >= 90%, both
S3ArtifactStore and GCSArtifactStore implement the 7-method ABC with
s3:// / gs:// URI schemes; CLI gains optional StoreConfig block with
backwards-compatible default; Layer-A's leftover cli.py:441 _path peek
routed through store.uri_for. Issue #5 closed.

README Roadmap drops S3/GCS; Extending section lists all three concrete
stores. PROGRESS Phase 13 subsection records the 4 implementation
commits. Single next action repointed at the three candidate next
layers (per-engine extract_last_frame impls / concurrent pool /
keyframe stage) for the next brainstorm to choose between.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## End-of-plan acceptance check

After all 5 tasks ship, run:

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run test-cov
pixi run typecheck
pixi run lint
grep -n "store\._path" src/kinoforge/cli.py  # expect 0 matches
git log --oneline main..HEAD
git status
```

Expected:
- All hooks Passed.
- All tests pass. Net new: 17 (S3) + 17 (GCS) + 6 (StoreConfig) + 3 (CLI) = **+43 tests**.
- Coverage ≥ 90%.
- mypy + ruff strict clean.
- `store._path` grep → 0 matches in `cli.py`.
- 5 implementation commits on the build branch ahead of main.
- Working tree clean.

GitHub issue #5 closes automatically when the Task 4 commit reaches `origin/main` (via the `Closes #5` trailer that was attached at merge time — note the per-task commits only carry `Refs #5`; the layer-wrapping merge commit produced by `superpowers-extended-cc:finishing-a-development-branch` is where the `Closes #5` trailer lands, matching the Layer A / Layer B pattern).
