# Design — S3 + GCS Artifact Stores (Layer C, GitHub issue #5)

**Status:** Approved 2026-05-29.
**Scope:** Two new concrete `ArtifactStore` implementations (`S3ArtifactStore`,
`GCSArtifactStore`) shipped together in one layer. Both self-register via the
store registry. CLI gains an optional `store:` config block; backwards compatible
when the block is absent.
**Unblocked by:** Layer A (`ArtifactStore.uri_for` ABC, issue #6, commit `dd08f0c`).
**Closes:** GitHub issue #5.

## 1. Locked decisions

| # | Topic | Decision | Why |
|---|---|---|---|
| 1 | Scope | S3 + GCS shipped together | One layer, parity from day one; cred / multipart / error decisions made once. |
| 2 | SDK | `boto3` + `google-cloud-storage`, **sync** | Matches current sync `ArtifactStore` ABC. No downstream rewrite. Both on conda-forge. |
| 3 | Credentials | SDK default chain only | Honours env vars, `~/.aws/credentials`, EC2 IAM role, GCS ADC, `GOOGLE_APPLICATION_CREDENTIALS`, GCE metadata. Zero kinoforge cred code. |
| 4 | Multipart | SDK auto (`upload_fileobj` / `upload_from_string`) | Both SDKs auto-switch over their default thresholds. No kinoforge knob. |
| 5 | Tests | Injected client seam + spy fakes (`FakeS3Client`, `FakeGCSClient`) | Matches every adapter pair in repo. Fully offline. No moto / localstack. |
| 6 | CLI | New optional `store:` config block | Backwards compatible: absent block → `LocalArtifactStore(state_dir)` as today. Also fixes `cli.py:441` `store._path` peek. |
| 7 | Layout | `<scheme>://<bucket>/<prefix>/<run_id>/<name>` | Mirrors `LocalArtifactStore`'s `<root>/<run_id>/<name>`. Empty prefix allowed. |
| 8 | Errors | Misses → `FileNotFoundError(uri)`; everything else propagates | Honours ABC contract minimally. SDK retries already cover transient errors. |

## 2. File layout

```
src/kinoforge/stores/
  base.py                 # unchanged
  local.py                # unchanged
  s3/__init__.py          # S3ArtifactStore + register_store("s3", ...)
  gcs/__init__.py         # GCSArtifactStore + register_store("gcs", ...)

src/kinoforge/_adapters.py     # 2 new imports under Stores section
src/kinoforge/core/config.py   # new StoreConfig pydantic block; Config.store field
src/kinoforge/cli.py           # new _build_store(cfg, state_dir); 2 call-site swaps + 1 _path peek fix

tests/stores/
  conftest.py             # FakeS3Client + FakeGCSClient (shared fixtures)
  test_s3.py              # ~16 tests
  test_gcs.py             # ~16 tests
tests/test_core_invariant.py   # extend _VENDOR_PATTERNS w/ boto3 + google.cloud
tests/core/test_config.py      # ~6 new StoreConfig tests
tests/cli/test_cli.py          # ~3 new store-selection tests (if cli test file exists; else
                               # add a slim new tests/cli/__init__.py + tests/cli/test_cli.py)

examples/configs/                       # add optional store: block as commented example to ONE config
                                        # (e.g. wan.yaml). Other configs prove the no-block default still works.
pixi.toml                               # add boto3 + google-cloud-storage to [dependencies]
README.md                               # Roadmap: drop "S3/GCS stores"; Extending: list 3 stores
PROGRESS.md                             # new Phase 13 entry; Single next action repointed
```

`pyproject.toml dependencies = []` is left untouched — project convention is to
declare runtime deps in `pixi.toml` only.

## 3. Concrete class signatures

### 3.1 `S3ArtifactStore`

```python
# src/kinoforge/stores/s3/__init__.py
"""Amazon S3-backed ArtifactStore.

Self-registers under ``"s3"`` on import via the store registry.  The default
zero-arg factory reads ``KINOFORGE_S3_BUCKET`` (+ optional
``KINOFORGE_S3_PREFIX``) from the environment; library users wanting full
control construct ``S3ArtifactStore(bucket=..., prefix=..., client=...)``
directly.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from kinoforge.core.interfaces import Artifact
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client  # type: ignore[import-not-found]


class S3ArtifactStore(ArtifactStore):
    """ArtifactStore backed by S3.

    Storage layout: ``s3://<bucket>/<prefix>/<run_id>/<name>``.

    Attributes:
        bucket: Target S3 bucket name.
        prefix: Optional key prefix; normalised to have no leading/trailing slash.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: "S3Client | None" = None,
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
        """Split an ``s3://bucket/key`` URI into (bucket, key)."""
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3:// uri: {uri!r}")
        bucket, _, key = uri[len("s3://"):].partition("/")
        return bucket, key

    # ------------------------------------------------------------------
    # ArtifactStore implementation
    # ------------------------------------------------------------------

    def uri_for(self, run_id: str, name: str) -> str:
        return f"s3://{self.bucket}/{self._key(run_id, name)}"

    def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
        key = self._key(run_id, name)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return Artifact(uri=f"s3://{self.bucket}/{key}")

    def get_bytes(self, uri: str) -> bytes:
        bucket, key = self._split_uri(uri)
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
        except self._client.exceptions.NoSuchKey:
            raise FileNotFoundError(f"artifact not found: {uri!r}") from None
        return resp["Body"].read()  # type: ignore[no-any-return]

    def put_json(self, run_id: str, name: str, obj: dict) -> Artifact:  # type: ignore[type-arg]
        return self.put_bytes(run_id, name, json.dumps(obj).encode("utf-8"))

    def get_json(self, uri: str) -> dict:  # type: ignore[type-arg]
        return json.loads(self.get_bytes(uri).decode("utf-8"))  # type: ignore[no-any-return]

    def list(self, run_id: str) -> list[str]:
        run_prefix = self._key(run_id, "") + "/"
        paginator = self._client.get_paginator("list_objects_v2")
        names: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=run_prefix):
            for obj in page.get("Contents", []):
                names.append(obj["Key"][len(run_prefix):])
        return names

    def delete(self, uri: str) -> None:
        bucket, key = self._split_uri(uri)
        try:
            self._client.head_object(Bucket=bucket, Key=key)
        except self._client.exceptions.ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(f"artifact not found: {uri!r}") from None
            raise
        self._client.delete_object(Bucket=bucket, Key=key)


# Self-registration -----------------------------------------------------------

from kinoforge.core.registry import register_store  # noqa: E402


def _default_factory() -> S3ArtifactStore:
    bucket = os.environ.get("KINOFORGE_S3_BUCKET")
    if not bucket:
        raise RuntimeError(
            "S3ArtifactStore default factory needs KINOFORGE_S3_BUCKET; "
            "either set the env var or construct S3ArtifactStore(bucket=...) directly."
        )
    return S3ArtifactStore(bucket=bucket, prefix=os.environ.get("KINOFORGE_S3_PREFIX", ""))


register_store("s3", _default_factory)
```

### 3.2 `GCSArtifactStore`

Symmetric to `S3ArtifactStore`. Key differences:

- Lazy import: `from google.cloud import storage` inside `__init__` when
  `client is None`.
- Constructor: `(bucket: str, prefix: str = "", *, client: "storage.Client | None" = None,
  not_found_exc: type[BaseException] | None = None)`. `not_found_exc` defaults to the
  real `google.api_core.exceptions.NotFound` via lazy import; fakes inject
  `FakeGCSClient.NotFound`. Symmetric to S3 catching exceptions via
  `self._client.exceptions.NoSuchKey`.
- URI scheme: `gs://`.
- Bucket handle pattern: `self._bucket = client.bucket(bucket)`.
- `put_bytes` → `self._bucket.blob(key).upload_from_string(data)`.
- `get_bytes` → `self._bucket.blob(key).download_as_bytes()` (catch `not_found_exc`).
- `list` → `self._bucket.list_blobs(prefix=run_prefix)`, strip prefix.
- `delete` → `blob.delete()` after `blob.exists()` check; catch `not_found_exc`.
- Default factory reads `KINOFORGE_GCS_BUCKET` + optional `KINOFORGE_GCS_PREFIX`.

### 3.3 ABC compliance proof

All 7 abstract methods implemented in both classes:
`put_bytes`, `get_bytes`, `put_json`, `get_json`, `list`, `delete`, `uri_for`.

Cross-method invariant: `store.put_bytes(rid, name, b).uri == store.uri_for(rid, name)`
holds for both stores (verified by test #14 per store). Same for `put_json`.

`uri_for` is pure — no I/O. Honours Layer A's contract.

## 4. Config schema

```python
# src/kinoforge/core/config.py

from typing import Literal
from pydantic import BaseModel, Field, model_validator
from pathlib import Path


class StoreConfig(BaseModel):
    """Optional artifact-store selector.

    Absent block defaults to ``kind="local"``, ``root=None`` — CLI then constructs
    ``LocalArtifactStore(state_dir)`` from the ``--state-dir`` argument, matching
    today's behaviour.
    """

    kind: Literal["local", "s3", "gcs"] = "local"
    root: Path | None = None
    bucket: str | None = None
    prefix: str = ""

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> "StoreConfig":
        if self.kind in ("s3", "gcs") and not self.bucket:
            raise ValueError(f"store.kind={self.kind!r} requires store.bucket")
        if self.kind == "local" and self.bucket:
            raise ValueError("store.kind='local' does not accept store.bucket")
        return self


class Config(BaseModel):
    # ... existing fields ...
    store: StoreConfig = Field(default_factory=StoreConfig)
```

YAML examples (added to `examples/configs/wan.yaml` as commented options; other
example configs remain unchanged to prove default backwards compat):

```yaml
# Default (absent block) — preserves pre-Layer-C behaviour:
#   LocalArtifactStore(state_dir) where state_dir is the --state-dir CLI arg.

# Explicit local with custom root:
# store:
#   kind: local
#   root: ./my-custom-root

# S3:
# store:
#   kind: s3
#   bucket: my-org-kinoforge
#   prefix: prod/runs

# GCS:
# store:
#   kind: gcs
#   bucket: my-org-kinoforge
#   prefix: prod/runs
```

## 5. CLI wiring

```python
# src/kinoforge/cli.py — new helper

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
        assert sc.bucket is not None  # validated by StoreConfig
        return S3ArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    if sc.kind == "gcs":
        from kinoforge.stores.gcs import GCSArtifactStore  # noqa: PLC0415
        assert sc.bucket is not None
        return GCSArtifactStore(bucket=sc.bucket, prefix=sc.prefix)
    raise UnknownAdapter(f"unknown store kind: {sc.kind!r}")
```

### 5.1 Call-site swaps

| Site | Before | After |
|---|---|---|
| `cli.py:265` (`_cmd_generate`) | `store = LocalArtifactStore(state_dir)` | `store = _build_store(cfg, state_dir)` |
| `cli.py:434` (`_cmd_gc`) | `store = LocalArtifactStore(state_dir)` | `store = _build_store(cfg, state_dir)` |
| `cli.py:441` (`_cmd_gc` inner loop) | `uri = str(store._path(run_id, name))` | `uri = store.uri_for(run_id, name)` |

### 5.2 Ledger stays local

`_ledger(state_dir)` at `cli.py:41-51` is **not** changed. The lifecycle ledger is
*operator* state (which instances exist, which are reapable), distinct from *run*
output. Cloud-backed ledger intersects issue #7 (cross-process discovery lock) and
is explicitly out of scope.

## 6. Adapter wiring + invariant extension

### 6.1 `_adapters.py`

```python
# Stores section — 2 new lines added:
import kinoforge.stores.local  # noqa: F401
import kinoforge.stores.s3     # noqa: F401
import kinoforge.stores.gcs    # noqa: F401
```

### 6.2 `test_core_invariant.py::_VENDOR_PATTERNS`

```python
_VENDOR_PATTERNS: list[tuple[re.Pattern[str], Path, str]] = [
    # ... existing sky/skypilot + runpod entries unchanged ...
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

Catches `import boto3`, `from boto3 import ...`, `import google.cloud.storage`,
`from google.cloud import storage`. The regex is line-based — lazy imports
inside `__init__` bodies still match it and the allowed-dir check still passes
for lines inside `stores/s3/` or `stores/gcs/`.

### 6.3 AC 1 (subprocess-isolation) + AC 3 (no-adapter-import-in-core) — no change

`kinoforge.stores` is an axis sibling, not a forbidden adapter (per the module
docstring of `test_core_invariant.py`). Core continues to legitimately import
`kinoforge.stores.base`. The new `kinoforge.stores.s3` / `.gcs` modules will not
leak into core's `sys.modules` because nothing in `kinoforge.core` imports them —
only `_adapters.py` (CLI-side) does.

## 7. Dependency adds

`pixi.toml [dependencies]` (conda-forge):

```toml
boto3 = "*"
google-cloud-storage = "*"
```

**Mandatory, not optional.** Rationale: keeping these as extras means the stores
would have to lazy-detect SDK presence and raise friendly errors; CI would have
to matrix with/without extras; users would hit `ImportError` deep inside a
generation run. Both libs are pure-Python on the import path that matters and
widely deployed.

## 8. Test strategy

### 8.1 Spy fakes (shared `tests/stores/conftest.py`)

`FakeS3Client` implements the boto3 surface used by `S3ArtifactStore`:
`put_object`, `get_object`, `head_object`, `delete_object`, `get_paginator`,
`.exceptions.NoSuchKey`, `.exceptions.ClientError`. In-memory
`dict[(bucket, key), bytes]`.

`FakeGCSClient` implements the `google.cloud.storage.Client` surface used by
`GCSArtifactStore`: `.bucket(name) -> _FakeBucket`. `_FakeBucket` exposes
`.blob(key) -> _FakeBlob` and `.list_blobs(prefix=...) -> Iterator[_FakeBlob]`.
`_FakeBlob` exposes `.upload_from_string`, `.download_as_bytes`, `.delete`,
`.exists`. `FakeGCSClient.NotFound` is the exception class injected via the
store's `not_found_exc=` parameter.

Both fakes live in `tests/stores/conftest.py`. Pytest fixtures `s3_store` and
`gcs_store` wire a fresh store + fake client per test.

### 8.2 Per-store test list (16 tests each; differences only in scheme + SDK shape)

1. `put_bytes_returns_artifact_with_<s3|gs>_uri`
2. `get_bytes_round_trips`
3. `put_get_with_prefix` — non-empty prefix folded into key
4. `put_get_with_empty_prefix` — no double slash, no leading slash
5. `put_get_with_slash_normalised_prefix` — leading/trailing slashes stripped
6. `put_json_round_trips`
7. `run_ids_are_isolated` — same name, different run_id → different keys
8. `list_returns_names_for_run_id`
9. `list_nested_name_preserves_subpath`
10. `list_empty_run_id_returns_empty_list`
11. `list_excludes_other_run_ids` — strict-prefix correctness
12. `delete_removes_item`
13. `delete_missing_raises_file_not_found`
14. `get_bytes_missing_raises_file_not_found`
15. `uri_for_matches_put_bytes_artifact_uri` (and `put_json` variant)
16. `self_registers_under_<s3|gcs>_in_registry`

Plus one cross-cutting test per store:

17. `lazy_sdk_import_not_triggered_when_client_injected` — snapshot `sys.modules`
    before constructing with `client=fake`; assert `boto3` (resp.
    `google.cloud.storage`) not in `sys.modules` afterwards. Proves the lazy
    import gate.

### 8.3 `StoreConfig` tests (added to `tests/core/test_config.py`)

1. `default_store_is_local_kind`
2. `s3_kind_requires_bucket` (raises pydantic `ValidationError`)
3. `gcs_kind_requires_bucket`
4. `local_kind_rejects_bucket`
5. `prefix_defaults_to_empty_string`
6. `parses_full_s3_block_from_yaml` (round-trips a YAML doc through `load_config`)

### 8.4 CLI tests (add to existing CLI test file; create `tests/cli/test_cli.py` if absent)

1. `cli_generate_uses_local_when_store_block_absent` — backwards compat
2. `cli_generate_uses_s3_when_store_kind_s3` — monkeypatch `S3ArtifactStore` to a
   spy; assert it was constructed with the YAML bucket+prefix
3. `cli_gc_uses_store_uri_for_not_path_peek` — patch `LocalArtifactStore.uri_for`
   to a spy and assert it was called; proves the `cli.py:441` `_path` peek fix

### 8.5 Coverage projection

Current: exactly 90% (the gate). Layer C adds ~250 covered lines (S3 + GCS impls)
across ~38 new tests; ratio holds at or above 90%.

### 8.6 Zero real-cloud / network in any test

No moto, no localstack, no fake-gcs-server, no real `boto3.client('s3')`, no real
`storage.Client()`. Test #17 (lazy-import gate) provides the cross-check.

## 9. Error mapping

| SDK error | Store translates to | Notes |
|---|---|---|
| `botocore.exceptions.ClientError` w/ `Error.Code in ("404", "NoSuchKey", "NotFound")` | `FileNotFoundError(uri)` | `get_bytes`, `delete`. |
| boto3 `s3.exceptions.NoSuchKey` | `FileNotFoundError(uri)` | `get_bytes`. |
| `google.api_core.exceptions.NotFound` | `FileNotFoundError(uri)` | `get_bytes`, `delete`. |
| All other SDK errors (auth, throttling, network) | propagated as-is | Callers can catch SDK exceptions directly if they want. |
| `ValueError` from `_split_uri` | propagated as-is | Programming error, not a store contract violation. |

## 10. Process retrospective scope

This layer must continue the brainstorm → spec → plan → execute → ship discipline
from Layers A + B (handoff §11). Specifically:

- Two-stage review per task (spec compliance + code quality, in that order).
- TDD red-first even for tiny helpers.
- `--no-ff` merge with substantive body referencing `Closes #5`.
- Tasks.json snapshot synced via separate chore commit after merge.
- One atomic commit per task; HEREDOC commit messages.

## 11. Out of scope (explicit defer list)

| Item | Why deferred |
|---|---|
| Ledger on cloud-store backend | Distinct from artifact storage; intersects issue #7 (cross-process discovery lock). Ledger stays local-only this layer. |
| Azure Blob, Backblaze B2, R2, MinIO | Pattern proven by S3 + GCS; sibling adapters trivially addable later as separate layers. |
| Configurable multipart threshold | SDK defaults are correct for current workloads. |
| Retry / backoff knobs | Both SDKs already retry transient errors. |
| Signed URLs / presigned downloads | New ABC method needed; no current consumer. |
| KMS / customer-managed encryption | SDKs support via `ExtraArgs` / `encryption_key`; expose only if a real workload needs it. |
| Cross-process write coordination | Issue #7. Layer C guarantees per-call atomicity via SDK-native `put_object` / `upload_from_string` only. |
| `cli._cmd_status` ledger query | Known-limitation §8.2 carryover, untouched. |
| Engine `extract_last_frame` rollouts (ComfyUI / Diffusers / Hosted) | Known-limitation §8.7 carryover, untouched. |
| Shared `CloudArtifactStore` middle base class | Approach B rejected. Re-evaluate only if a third cloud store ever lands. |

## 12. Acceptance criteria (per-task ACs land in the plan; layer-level here)

- L1. `pixi run pre-commit run --all-files` clean.
- L2. `pixi run typecheck` (mypy strict) clean across the new stores + config + cli + tests.
- L3. `pixi run test-cov` at or above 90% coverage.
- L4. `S3ArtifactStore` and `GCSArtifactStore` each pass their 17-test suite against in-memory fakes.
- L5. `StoreConfig` passes its 6-test schema suite.
- L6. CLI integration passes its 3-test suite, including backwards-compat default.
- L7. `test_core_invariant.py` passes after `_VENDOR_PATTERNS` extension; `boto3` + `google.cloud` confined to `stores/s3/` + `stores/gcs/`.
- L8. `cli.py:441` `store._path` peek replaced with `store.uri_for(...)`.
- L9. `_adapters.py` imports both new modules; their `register_store("s3"|"gcs", ...)` self-registration fires on CLI startup.
- L10. README Roadmap drops "S3/GCS stores"; Extending section lists three stores.
- L11. PROGRESS.md gains Phase 13 entry; Single next action repointed at the next layer.
- L12. `--no-ff` merge commit on `main` carries `Closes #5` trailer.
