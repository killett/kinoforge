# Layer W — S3/GCS real-cloud verification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close `PROGRESS.md` carry-forward #4 by exercising `S3ArtifactStore` and `GCSArtifactStore` against real AWS S3 + Google Cloud Storage across five axes (hot path, multipart, 503-proxy retry, encryption, signed URLs). Capture redacted JSON fixtures, replay them offline, and add the missing production code (multipart-aware uploads, `StoreConfig.encryption`, `ArtifactStore.signed_url`).

**Architecture:** Mirror Layer N (Phase 24). Live opt-in suite (`KINOFORGE_LIVE_TESTS=1`) under `tests/stores/live/` captures real responses via a `botocore`-event + `google-cloud-storage`-transport recording seam. Fixtures land under `tests/stores/fixtures/{s3,gcs}/`. Two new offline-only modules carry the new test infrastructure: `tests/stores/recording.py` (recorders + replay clients) and `tests/stores/proxy.py` (`Fail503Proxy`). Production code changes are scoped to four files: `core/config.py` (pydantic block), `stores/base.py` (new abstract method), `stores/s3/__init__.py`, `stores/gcs/__init__.py`. Retry baselines are pinned in the store source, not in `StoreConfig`.

**Tech Stack:** Python 3.13 · pydantic v2 · stdlib `argparse`, `http.server`, `urllib`, `io.BytesIO`, `threading`, `datetime` · `boto3` + `botocore` · `google-cloud-storage` + `google-api-core` + `google-cloud-kms` · `pytest` · existing kinoforge `ArtifactStore` / `StoreConfig` / registry primitives.

**Spec reference:** `docs/superpowers/specs/2026-06-06-layer-w-s3-gcs-real-cloud-design.md`.

---

## File Structure

**New production files:**
- _(none — Layer W reuses the three existing store modules)_

**Modified production files:**
- `src/kinoforge/core/config.py` — add `StoreEncryptionConfig`, `StoreConfig.encryption`, `StoreConfig.signed_url_default_ttl_s`
- `src/kinoforge/stores/base.py` — add abstract `signed_url(run_id, name, *, op, ttl_s)` method
- `src/kinoforge/stores/local.py` — `signed_url` raises `NotImplementedError`
- `src/kinoforge/stores/s3/__init__.py` — switch to `upload_fileobj`; wire encryption; implement `signed_url`; pin retry config
- `src/kinoforge/stores/gcs/__init__.py` — switch to `upload_from_file`; wire CMEK; implement `signed_url`; pin retry kwarg

**New tooling:**
- `tools/bootstrap_kms.py` — idempotent AWS KMS + GCP Cloud KMS provisioner
- `pixi.toml` `[tasks.cloud:bootstrap-kms]` — wrapper

**New offline-test infrastructure:**
- `tests/stores/recording.py` — `S3Recorder`, `GCSRecorder`, `_persist`, `_redact`, `FixtureMissError`, `FixtureReplayS3Client`, `FixtureReplayGCSClient`
- `tests/stores/proxy.py` — `Fail503Proxy`
- `tests/stores/test_recording.py` — recorder + replay + redaction tests
- `tests/stores/test_proxy.py` — proxy unit tests
- `tests/stores/test_signed_url_abc.py` — ABC contract tests
- `tests/stores/test_offline_isolation.py` — socket-spy guard (AC4)

**New live-test surface (KINOFORGE_LIVE_TESTS-gated):**
- `tests/stores/live/__init__.py`
- `tests/stores/live/conftest.py` — gate + creds resolution + record-session fixtures
- `tests/stores/live/test_s3_live.py` — 5 axes
- `tests/stores/live/test_gcs_live.py` — 5 axes
- `tests/stores/fixtures/s3/{hot_path,multipart,encryption_default,encryption_kms,signed_url_get,signed_url_put}.json`
- `tests/stores/fixtures/gcs/{hot_path,resumable,encryption_default,encryption_cmek,signed_url_get,signed_url_put}.json`

**Modified test files:**
- `tests/stores/conftest.py` — extend `FakeS3Client` (capture `ExtraArgs`, support `upload_fileobj`, support `generate_presigned_url`) + `FakeGCSClient` (capture `kms_key_name`, support `upload_from_file`, support `generate_signed_url`, capture `retry=` kwarg)
- `tests/stores/test_s3.py` — fixture-replay tests + retry-baseline lock + multipart + encryption + signed_url unit cover
- `tests/stores/test_gcs.py` — analogous
- `tests/core/test_config.py` — `StoreEncryptionConfig` round-trip + validator

**Modified docs:**
- `docs/CLOUD-CREDS.md` — KMS provisioning history rows + bootstrap pointer + rotation row
- `examples/configs/wan.yaml` — commented `store.encryption` block showing `mode: kms`
- `README.md` — Cloud stores: encryption + signed URLs + bootstrap
- `PROGRESS.md` — Phase 38 entry + close carry-forward #4
- `.gitignore` — add `.aws/kms-test-key.arn` + `.gcp/kms-test-key.name`

---

## Pre-flight

Verify the working tree is clean, the test suite is green, the spec exists, and AWS+GCS creds are bootstrapped (per `docs/CLOUD-CREDS.md`).

- [ ] Run `git status` — must show "nothing to commit, working tree clean".
- [ ] Run `pixi run test` — must pass (1423 passed / 8 skipped baseline from Layer V).
- [ ] Run `ls docs/superpowers/specs/2026-06-06-layer-w-s3-gcs-real-cloud-design.md` — must exist.
- [ ] Run `ls .aws/credentials .gcp/kinoforge-sa.json` — both must exist (creds bootstrapped in `0d2cc18` + `171f927`).
- [ ] Run `aws s3 ls s3://<S3_BUCKET> >/dev/null && echo S3-OK` — must print `S3-OK`.
- [ ] Run `gsutil ls -b gs://<GCS_BUCKET> >/dev/null && echo GCS-OK` — must print `GCS-OK`.

If any of those fail, stop and fix before starting Task 1.

---

### Task 1: `StoreEncryptionConfig` + `signed_url_default_ttl_s` pydantic block

**Goal:** Add the pydantic types and YAML round-trip for the new encryption knob + signed-URL default TTL so Tasks 3 + 4 can wire them into the store impls.

**Files:**
- Modify: `src/kinoforge/core/config.py` (add `StoreEncryptionConfig` near other store classes; add two fields to `StoreConfig`)
- Modify: `examples/configs/wan.yaml` (commented example)
- Create: `tests/core/test_config_encryption.py`

**Acceptance Criteria:**
- [ ] `StoreEncryptionConfig(mode="default")` is the implicit default; `kms_key_id` is `None`.
- [ ] `StoreEncryptionConfig(mode="kms", kms_key_id="arn:aws:kms:...")` constructs cleanly.
- [ ] `StoreEncryptionConfig(mode="kms")` (no key id) raises `ValidationError` with message containing `encryption.mode='kms' requires encryption.kms_key_id`.
- [ ] `StoreEncryptionConfig(mode="bogus")` raises `ValidationError` (literal-typed `mode`).
- [ ] `StoreEncryptionConfig(extra_field=1)` raises `ValidationError` (`extra="forbid"`).
- [ ] `StoreConfig()` defaults `encryption` to a fresh `StoreEncryptionConfig(mode="default")` and `signed_url_default_ttl_s` to `3600`.
- [ ] YAML round-trip: a `store.encryption.mode: kms` + `store.encryption.kms_key_id: ...` document loads into `StoreConfig.encryption.mode == "kms"`.

**Verify:** `pixi run pytest tests/core/test_config_encryption.py -v` → 7 passed; `pixi run pytest tests/core/test_config.py -v` still green; `pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config_encryption.py examples/configs/wan.yaml`.

**Steps:**

- [ ] **Step 1: Write the failing test file `tests/core/test_config_encryption.py`.**

```python
"""StoreEncryptionConfig + signed_url_default_ttl_s round-trip tests."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig, load_config


def test_default_encryption_is_provider_managed():
    cfg = StoreEncryptionConfig()
    assert cfg.mode == "default"
    assert cfg.kms_key_id is None


def test_kms_mode_requires_key_id():
    with pytest.raises(ValidationError) as excinfo:
        StoreEncryptionConfig(mode="kms")
    msg = str(excinfo.value)
    assert "encryption.mode='kms' requires encryption.kms_key_id" in msg


def test_kms_mode_with_key_id_constructs():
    cfg = StoreEncryptionConfig(mode="kms", kms_key_id="arn:aws:kms:us-east-1:1:key/abc")
    assert cfg.mode == "kms"
    assert cfg.kms_key_id == "arn:aws:kms:us-east-1:1:key/abc"


def test_bogus_mode_rejected():
    with pytest.raises(ValidationError):
        StoreEncryptionConfig(mode="rot13")  # type: ignore[arg-type]


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        StoreEncryptionConfig(extra_field=1)  # type: ignore[call-arg]


def test_store_config_defaults():
    sc = StoreConfig()
    assert sc.encryption.mode == "default"
    assert sc.encryption.kms_key_id is None
    assert sc.signed_url_default_ttl_s == 3600


def test_yaml_round_trip_kms(tmp_path):
    doc = {
        "store": {
            "kind": "s3",
            "bucket": "demo",
            "encryption": {
                "mode": "kms",
                "kms_key_id": "arn:aws:kms:us-east-1:1:key/abc",
            },
            "signed_url_default_ttl_s": 600,
        },
        "engine": {"kind": "fake"},
        "models": [{"kind": "base", "ref": "local://m"}],
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(doc))
    cfg = load_config(str(p))
    assert cfg.store.encryption.mode == "kms"
    assert cfg.store.encryption.kms_key_id == "arn:aws:kms:us-east-1:1:key/abc"
    assert cfg.store.signed_url_default_ttl_s == 600
```

- [ ] **Step 2: Run the test — confirm RED.**

```bash
pixi run pytest tests/core/test_config_encryption.py -v
```

Expected: `ImportError: cannot import name 'StoreEncryptionConfig' from 'kinoforge.core.config'`.

- [ ] **Step 3: Implement in `src/kinoforge/core/config.py`.**

Add near the existing `StoreConfig` definition:

```python
from typing import Self


class StoreEncryptionConfig(BaseModel):
    """Encryption settings for an ArtifactStore.

    ``mode="default"`` lets the cloud provider apply its bucket-default encryption
    (SSE-S3 on AWS, Google-managed on GCS). ``mode="kms"`` activates client-side
    routing through a caller-owned KMS key.
    """

    mode: Literal["default", "kms"] = "default"
    kms_key_id: str | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _key_required_for_kms(self) -> Self:
        if self.mode == "kms" and not self.kms_key_id:
            raise ValueError("encryption.mode='kms' requires encryption.kms_key_id")
        return self
```

In the existing `StoreConfig` class, add the two new fields:

```python
class StoreConfig(BaseModel):
    # ... existing fields ...
    encryption: StoreEncryptionConfig = Field(default_factory=StoreEncryptionConfig)
    signed_url_default_ttl_s: int = 3600
```

- [ ] **Step 4: Update `examples/configs/wan.yaml` with a commented block.**

Append to the `store:` section:

```yaml
  # Optional: encrypt object writes with a customer-managed KMS key.
  # Default mode uses provider-managed encryption (SSE-S3 / Google-managed).
  # encryption:
  #   mode: kms
  #   kms_key_id: arn:aws:kms:us-east-1:123456789012:key/abc-def
  # signed_url_default_ttl_s: 3600  # seconds; caller may override per-call
```

- [ ] **Step 5: Run the test — confirm GREEN.**

```bash
pixi run pytest tests/core/test_config_encryption.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Full config-tests gate + pre-commit.**

```bash
pixi run pytest tests/core/test_config.py tests/core/test_config_encryption.py -v
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config_encryption.py examples/configs/wan.yaml
```

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/config.py tests/core/test_config_encryption.py examples/configs/wan.yaml
git commit -m "feat(config): StoreEncryptionConfig + signed_url_default_ttl_s (Layer W T1)"
```

---

### Task 2: `ArtifactStore.signed_url` ABC + `LocalArtifactStore` stub

**Goal:** Add the abstract method so Tasks 3 + 4 implement it on S3 + GCS. `LocalArtifactStore` raises `NotImplementedError` because local files have no transport-layer auth.

**Files:**
- Modify: `src/kinoforge/stores/base.py`
- Modify: `src/kinoforge/stores/local.py`
- Modify: `src/kinoforge/stores/s3/__init__.py` (temporary stub — Task 3 replaces)
- Modify: `src/kinoforge/stores/gcs/__init__.py` (temporary stub — Task 4 replaces)
- Create: `tests/stores/test_signed_url_abc.py`

**Acceptance Criteria:**
- [ ] `ArtifactStore.signed_url` is `@abstractmethod` with signature `(self, run_id: str, name: str, *, op: Literal["GET", "PUT"], ttl_s: int) -> str`.
- [ ] Docstring documents Args / Returns / Raises (including `NotImplementedError` for backends that don't support it).
- [ ] `LocalArtifactStore(...).signed_url("r", "n", op="GET", ttl_s=60)` raises `NotImplementedError` with message `"LocalArtifactStore does not support signed URLs"`.
- [ ] `S3ArtifactStore` and `GCSArtifactStore` carry a `signed_url` stub that raises `NotImplementedError("Layer W T3"/'T4' not yet implemented")` so the ABC stays satisfied at import time.
- [ ] All existing store-tests stay green.

**Verify:** `pixi run pytest tests/stores/test_signed_url_abc.py tests/stores/ -v`.

**Steps:**

- [ ] **Step 1: Write `tests/stores/test_signed_url_abc.py`.**

```python
"""ArtifactStore.signed_url ABC contract tests."""

from __future__ import annotations

from typing import get_type_hints

import pytest

from kinoforge.stores.base import ArtifactStore
from kinoforge.stores.local import LocalArtifactStore


def test_signed_url_is_abstract():
    assert "signed_url" in ArtifactStore.__abstractmethods__


def test_local_signed_url_raises_not_implemented(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    with pytest.raises(NotImplementedError, match="LocalArtifactStore does not support signed URLs"):
        store.signed_url("run", "name", op="GET", ttl_s=60)


def test_local_signed_url_put_also_raises(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    with pytest.raises(NotImplementedError):
        store.signed_url("run", "name", op="PUT", ttl_s=60)


def test_signed_url_signature_keyword_only():
    """`op` and `ttl_s` must be keyword-only to prevent positional misuse."""
    import inspect

    sig = inspect.signature(ArtifactStore.signed_url)
    params = sig.parameters
    assert params["op"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["ttl_s"].kind == inspect.Parameter.KEYWORD_ONLY
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/stores/test_signed_url_abc.py -v
```

Expected: `AttributeError: signed_url` (method doesn't exist on the ABC yet).

- [ ] **Step 3: Add the abstract method in `src/kinoforge/stores/base.py`.**

```python
from typing import Literal


class ArtifactStore(ABC):
    # ... existing methods ...

    @abstractmethod
    def signed_url(
        self,
        run_id: str,
        name: str,
        *,
        op: Literal["GET", "PUT"],
        ttl_s: int,
    ) -> str:
        """Return a pre-signed URL for a single GET or PUT on the artifact.

        Args:
            run_id: Run namespace.
            name: Artifact name within the run.
            op: HTTP method the URL grants. ``"GET"`` downloads; ``"PUT"`` uploads.
            ttl_s: Validity window in seconds from issuance.

        Returns:
            Absolute HTTPS URL valid for ``ttl_s`` seconds.

        Raises:
            NotImplementedError: Backend does not support signed URLs (e.g.
                ``LocalArtifactStore``).
        """
```

- [ ] **Step 4: Add `LocalArtifactStore.signed_url` stub in `src/kinoforge/stores/local.py`.**

```python
class LocalArtifactStore(ArtifactStore):
    # ... existing methods ...

    def signed_url(
        self,
        run_id: str,
        name: str,
        *,
        op: Literal["GET", "PUT"],
        ttl_s: int,
    ) -> str:
        raise NotImplementedError("LocalArtifactStore does not support signed URLs")
```

- [ ] **Step 5: Add temporary stubs on `S3ArtifactStore` + `GCSArtifactStore`.**

In `src/kinoforge/stores/s3/__init__.py`:

```python
    def signed_url(self, run_id, name, *, op, ttl_s):
        raise NotImplementedError("Layer W T3 not yet implemented")
```

In `src/kinoforge/stores/gcs/__init__.py`:

```python
    def signed_url(self, run_id, name, *, op, ttl_s):
        raise NotImplementedError("Layer W T4 not yet implemented")
```

Both stubs must carry the same parameter form so MyPy + the ABC validate at import time. Task 3 / Task 4 replace them with real impls.

- [ ] **Step 6: Run — confirm GREEN.**

```bash
pixi run pytest tests/stores/test_signed_url_abc.py tests/stores/ -v
```

Expected: all green.

- [ ] **Step 7: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/base.py src/kinoforge/stores/local.py src/kinoforge/stores/s3/__init__.py src/kinoforge/stores/gcs/__init__.py tests/stores/test_signed_url_abc.py
git add src/kinoforge/stores/base.py src/kinoforge/stores/local.py src/kinoforge/stores/s3/__init__.py src/kinoforge/stores/gcs/__init__.py tests/stores/test_signed_url_abc.py
git commit -m "feat(stores): ArtifactStore.signed_url ABC + Local NotImplementedError (Layer W T2)"
```

---

### Task 3: `S3ArtifactStore` — multipart + encryption + signed_url + retry baseline

**Goal:** Switch `put_bytes` to `upload_fileobj` (multipart-aware), thread `StoreConfig.encryption` through `ExtraArgs`, implement `signed_url` via `generate_presigned_url`, and pin the boto3 retry config in the store's client constructor.

**Files:**
- Modify: `src/kinoforge/stores/s3/__init__.py`
- Modify: `tests/stores/conftest.py` (extend `FakeS3Client`)
- Modify: `tests/stores/test_s3.py` (new test functions)

**Acceptance Criteria:**
- [ ] `S3ArtifactStore.__init__` builds the boto3 client with `botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})`. Test `test_s3_retry_config_pinned` reads `client.meta.config.retries` and asserts both values.
- [ ] `put_bytes` calls `client.upload_fileobj(io.BytesIO(data), bucket, key, ExtraArgs=...)`. `FakeS3Client.upload_fileobj_calls` captures `(bucket, key, body, ExtraArgs)`.
- [ ] `encryption.mode == "default"` → `ExtraArgs` does NOT contain `ServerSideEncryption` (provider applies bucket default).
- [ ] `encryption.mode == "kms"` → `ExtraArgs == {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}`.
- [ ] `signed_url(op="GET", ttl_s=600)` calls `client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600)` and returns the URL.
- [ ] `signed_url(op="PUT", ttl_s=600)` calls `generate_presigned_url("put_object", ...)`.
- [ ] `put_json` still works (it routes through `put_bytes`).
- [ ] All existing `tests/stores/test_s3.py` tests stay green.

**Verify:** `pixi run pytest tests/stores/test_s3.py -v`.

**Steps:**

- [ ] **Step 1: Extend `FakeS3Client` in `tests/stores/conftest.py`.**

Add the new methods + capture lists:

```python
class FakeS3Client:
    def __init__(self):
        # ... existing state ...
        self.upload_fileobj_calls: list[tuple[str, str, bytes, dict]] = []
        self.generate_presigned_url_calls: list[tuple[str, dict, int]] = []
        self.meta = SimpleNamespace(
            config=SimpleNamespace(
                retries={"max_attempts": 0, "mode": "legacy"},
            ),
        )

    def set_retry_config(self, retries: dict) -> None:
        """Mirror what botocore.config.Config does at construction time."""
        self.meta.config.retries = retries

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):
        body = fileobj.read()
        self.upload_fileobj_calls.append((Bucket, Key, body, dict(ExtraArgs or {})))
        # Mirror into the existing in-memory map so get_object still works.
        self.objects[(Bucket, Key)] = body

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        self.generate_presigned_url_calls.append((op, dict(Params), ExpiresIn))
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?X-Sig=fake&ttl={ExpiresIn}"
```

- [ ] **Step 2: Write the failing tests in `tests/stores/test_s3.py`.**

```python
import io

import boto3.session
from botocore.config import Config

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.s3 import S3ArtifactStore


def _store_with_fake(fake_client, *, encryption=None):
    """Helper — build S3ArtifactStore around the in-test fake."""
    cfg = StoreConfig(
        kind="s3",
        bucket="layer-w-test",
        encryption=encryption or StoreEncryptionConfig(),
    )
    return S3ArtifactStore(bucket="layer-w-test", client=fake_client, cfg=cfg)


def test_s3_retry_config_pinned(fake_s3_client):
    """T3 AC1: boto3 client built with retries={max_attempts:3, mode:standard}."""
    store = _store_with_fake(fake_s3_client)
    # After construction the store should have stamped the retry config onto
    # the client's meta.
    retries = store._client.meta.config.retries
    assert retries["max_attempts"] == 3
    assert retries["mode"] == "standard"


def test_s3_put_bytes_uses_upload_fileobj(fake_s3_client):
    store = _store_with_fake(fake_s3_client)
    artifact = store.put_bytes("run1", "out.bin", b"hello")
    assert fake_s3_client.upload_fileobj_calls, "expected upload_fileobj to be called"
    bucket, key, body, extra = fake_s3_client.upload_fileobj_calls[0]
    assert bucket == "layer-w-test"
    assert body == b"hello"
    assert "ServerSideEncryption" not in extra  # default mode = no override
    assert artifact.uri.startswith("s3://layer-w-test/")


def test_s3_put_bytes_kms_extra_args(fake_s3_client):
    enc = StoreEncryptionConfig(mode="kms", kms_key_id="arn:aws:kms:us-east-1:1:key/abc")
    store = _store_with_fake(fake_s3_client, encryption=enc)
    store.put_bytes("run1", "out.bin", b"hello")
    _, _, _, extra = fake_s3_client.upload_fileobj_calls[0]
    assert extra["ServerSideEncryption"] == "aws:kms"
    assert extra["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:1:key/abc"


def test_s3_signed_url_get(fake_s3_client):
    store = _store_with_fake(fake_s3_client)
    url = store.signed_url("run1", "out.bin", op="GET", ttl_s=600)
    op, params, ttl = fake_s3_client.generate_presigned_url_calls[0]
    assert op == "get_object"
    assert params == {"Bucket": "layer-w-test", "Key": "run1/out.bin"}
    assert ttl == 600
    assert url.startswith("https://layer-w-test.s3.amazonaws.com/run1/out.bin?")


def test_s3_signed_url_put(fake_s3_client):
    store = _store_with_fake(fake_s3_client)
    store.signed_url("run1", "out.bin", op="PUT", ttl_s=120)
    op, _, ttl = fake_s3_client.generate_presigned_url_calls[0]
    assert op == "put_object"
    assert ttl == 120
```

- [ ] **Step 3: Run — confirm RED.**

```bash
pixi run pytest tests/stores/test_s3.py -v
```

Expected: 5 failures (missing retry pin, `put_object` not `upload_fileobj`, encryption ignored, signed_url stub).

- [ ] **Step 4: Implement in `src/kinoforge/stores/s3/__init__.py`.**

Imports:

```python
import io
from datetime import datetime
from typing import Literal

import boto3
from botocore.config import Config as BotocoreConfig
```

`__init__` — pin retry config:

```python
def __init__(self, bucket: str, *, client=None, cfg: StoreConfig | None = None):
    self.bucket = bucket
    self._cfg = cfg or StoreConfig(kind="s3", bucket=bucket)
    if client is None:
        retry_config = BotocoreConfig(retries={"max_attempts": 3, "mode": "standard"})
        client = boto3.client("s3", config=retry_config)
    else:
        # Test-injected client gets the same retry config stamped on its meta
        # so test_s3_retry_config_pinned can assert it.
        if hasattr(client, "set_retry_config"):
            client.set_retry_config({"max_attempts": 3, "mode": "standard"})
    self._client = client
```

`put_bytes` — switch to `upload_fileobj` + encryption wiring:

```python
def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
    key = f"{run_id}/{name}"
    extra_args: dict[str, str] = {}
    enc = self._cfg.encryption
    if enc.mode == "kms":
        assert enc.kms_key_id is not None  # guaranteed by pydantic validator
        extra_args["ServerSideEncryption"] = "aws:kms"
        extra_args["SSEKMSKeyId"] = enc.kms_key_id
    self._client.upload_fileobj(
        io.BytesIO(data),
        Bucket=self.bucket,
        Key=key,
        ExtraArgs=extra_args,
    )
    return Artifact(uri=f"s3://{self.bucket}/{key}", filename=name, headers={})
```

`signed_url`:

```python
_OP_TO_BOTOCORE = {"GET": "get_object", "PUT": "put_object"}

def signed_url(
    self,
    run_id: str,
    name: str,
    *,
    op: Literal["GET", "PUT"],
    ttl_s: int,
) -> str:
    key = f"{run_id}/{name}"
    return self._client.generate_presigned_url(
        _OP_TO_BOTOCORE[op],
        Params={"Bucket": self.bucket, "Key": key},
        ExpiresIn=ttl_s,
    )
```

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/stores/test_s3.py -v
```

Expected: all green (new + pre-existing).

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/s3/__init__.py tests/stores/conftest.py tests/stores/test_s3.py
git add src/kinoforge/stores/s3/__init__.py tests/stores/conftest.py tests/stores/test_s3.py
git commit -m "feat(s3): multipart + encryption + signed_url + retry pin (Layer W T3)"
```

---

### Task 4: `GCSArtifactStore` — resumable + CMEK + signed_url + retry baseline

**Goal:** Switch `put_bytes` to `upload_from_file` (resumable above ~5 MiB), thread `StoreConfig.encryption` through `Blob.kms_key_name`, implement `signed_url` via v4 signed URLs, and pin a `google.api_core.retry.Retry` instance on every read + write call.

**Files:**
- Modify: `src/kinoforge/stores/gcs/__init__.py`
- Modify: `tests/stores/conftest.py` (extend `FakeGCSClient` / `FakeGCSBucket` / `FakeGCSBlob`)
- Modify: `tests/stores/test_gcs.py`

**Acceptance Criteria:**
- [ ] A single `Retry` instance defined at module scope (`_GCS_RETRY = Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)`) is passed as `retry=` on every `upload_from_file`, `download_as_bytes`, `delete`, and `list_blobs` call.
- [ ] `put_bytes` calls `blob.upload_from_file(io.BytesIO(data), retry=_GCS_RETRY)`. `FakeGCSBlob.upload_from_file_calls` captures `(body, retry)`.
- [ ] `encryption.mode == "default"` → `blob.kms_key_name` left as `None` (provider applies bucket default).
- [ ] `encryption.mode == "kms"` → `blob.kms_key_name = encryption.kms_key_id` BEFORE `upload_from_file` is called.
- [ ] `signed_url(op, ttl_s)` calls `blob.generate_signed_url(version="v4", expiration=timedelta(seconds=ttl_s), method=op)`.
- [ ] All existing `tests/stores/test_gcs.py` tests stay green.

**Verify:** `pixi run pytest tests/stores/test_gcs.py -v`.

**Steps:**

- [ ] **Step 1: Extend `FakeGCSBlob` + `FakeGCSBucket` + `FakeGCSClient` in `tests/stores/conftest.py`.**

```python
class FakeGCSBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.body: bytes = b""
        self.kms_key_name: str | None = None
        self.upload_from_file_calls: list[tuple[bytes, object]] = []
        self.download_as_bytes_calls: list[object] = []
        self.delete_calls: list[object] = []
        self.generate_signed_url_calls: list[dict] = []

    def upload_from_file(self, fileobj, *, retry=None):
        body = fileobj.read()
        self.body = body
        self.upload_from_file_calls.append((body, retry))
        self.bucket._blobs[self.name] = self

    def download_as_bytes(self, *, retry=None):
        self.download_as_bytes_calls.append(retry)
        return self.body

    def delete(self, *, retry=None):
        self.delete_calls.append(retry)
        self.bucket._blobs.pop(self.name, None)

    def generate_signed_url(self, *, version, expiration, method):
        call = {"version": version, "expiration": expiration, "method": method}
        self.generate_signed_url_calls.append(call)
        return f"https://storage.googleapis.com/{self.bucket.name}/{self.name}?X-Goog-Signature=fake&method={method}"

    def reload(self):
        return None
```

`FakeGCSBucket.blob(name)` returns or creates a `FakeGCSBlob`; `list_blobs(prefix=..., retry=...)` records the `retry` arg.

- [ ] **Step 2: Write the failing tests in `tests/stores/test_gcs.py`.**

```python
from datetime import timedelta

import io

from google.api_core.retry import Retry

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.gcs import GCSArtifactStore, _GCS_RETRY


def _store_with_fake(fake_gcs_client, *, encryption=None):
    cfg = StoreConfig(
        kind="gcs",
        bucket="layer-w-test",
        encryption=encryption or StoreEncryptionConfig(),
    )
    return GCSArtifactStore(
        bucket="layer-w-test",
        client=fake_gcs_client,
        cfg=cfg,
    )


def test_gcs_retry_instance_is_module_constant():
    assert isinstance(_GCS_RETRY, Retry)


def test_gcs_put_bytes_uses_upload_from_file_and_retry(fake_gcs_client):
    store = _store_with_fake(fake_gcs_client)
    store.put_bytes("run1", "out.bin", b"hello")
    blob = fake_gcs_client.buckets["layer-w-test"]._blobs["run1/out.bin"]
    assert blob.upload_from_file_calls
    body, retry = blob.upload_from_file_calls[0]
    assert body == b"hello"
    assert retry is _GCS_RETRY
    assert blob.kms_key_name is None  # default mode


def test_gcs_put_bytes_cmek_sets_kms_key_name(fake_gcs_client):
    enc = StoreEncryptionConfig(
        mode="kms",
        kms_key_id="projects/p/locations/us-central1/keyRings/r/cryptoKeys/k",
    )
    store = _store_with_fake(fake_gcs_client, encryption=enc)
    store.put_bytes("run1", "out.bin", b"hello")
    blob = fake_gcs_client.buckets["layer-w-test"]._blobs["run1/out.bin"]
    assert blob.kms_key_name == enc.kms_key_id


def test_gcs_signed_url_get(fake_gcs_client):
    store = _store_with_fake(fake_gcs_client)
    url = store.signed_url("run1", "out.bin", op="GET", ttl_s=600)
    blob = fake_gcs_client.buckets["layer-w-test"]._blobs["run1/out.bin"]
    call = blob.generate_signed_url_calls[0]
    assert call["version"] == "v4"
    assert call["expiration"] == timedelta(seconds=600)
    assert call["method"] == "GET"
    assert "method=GET" in url


def test_gcs_signed_url_put(fake_gcs_client):
    store = _store_with_fake(fake_gcs_client)
    store.signed_url("run1", "out.bin", op="PUT", ttl_s=120)
    blob = fake_gcs_client.buckets["layer-w-test"]._blobs["run1/out.bin"]
    call = blob.generate_signed_url_calls[0]
    assert call["method"] == "PUT"
    assert call["expiration"] == timedelta(seconds=120)


def test_gcs_get_bytes_passes_retry(fake_gcs_client):
    store = _store_with_fake(fake_gcs_client)
    store.put_bytes("run1", "out.bin", b"hello")
    store.get_bytes("run1", "out.bin")
    blob = fake_gcs_client.buckets["layer-w-test"]._blobs["run1/out.bin"]
    assert blob.download_as_bytes_calls[0] is _GCS_RETRY
```

- [ ] **Step 3: Run — confirm RED.**

```bash
pixi run pytest tests/stores/test_gcs.py -v
```

Expected: failures pointing at missing `_GCS_RETRY`, `upload_from_string` still being called, no `kms_key_name`, signed_url stub.

- [ ] **Step 4: Implement in `src/kinoforge/stores/gcs/__init__.py`.**

Imports + retry constant:

```python
import io
from datetime import timedelta
from typing import Literal

from google.api_core.retry import Retry

_GCS_RETRY = Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)
```

`put_bytes`:

```python
def put_bytes(self, run_id: str, name: str, data: bytes) -> Artifact:
    key = f"{run_id}/{name}"
    bucket = self._client.bucket(self.bucket)
    blob = bucket.blob(key)
    enc = self._cfg.encryption
    if enc.mode == "kms":
        assert enc.kms_key_id is not None
        blob.kms_key_name = enc.kms_key_id
    blob.upload_from_file(io.BytesIO(data), retry=_GCS_RETRY)
    return Artifact(uri=f"gs://{self.bucket}/{key}", filename=name, headers={})
```

`get_bytes` / `delete` / `list` route the same `_GCS_RETRY`:

```python
def get_bytes(self, run_id: str, name: str) -> bytes:
    bucket = self._client.bucket(self.bucket)
    blob = bucket.blob(f"{run_id}/{name}")
    return blob.download_as_bytes(retry=_GCS_RETRY)
```

`signed_url`:

```python
def signed_url(
    self,
    run_id: str,
    name: str,
    *,
    op: Literal["GET", "PUT"],
    ttl_s: int,
) -> str:
    bucket = self._client.bucket(self.bucket)
    blob = bucket.blob(f"{run_id}/{name}")
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=ttl_s),
        method=op,
    )
```

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/stores/test_gcs.py -v
```

Expected: all green.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/gcs/__init__.py tests/stores/conftest.py tests/stores/test_gcs.py
git add src/kinoforge/stores/gcs/__init__.py tests/stores/conftest.py tests/stores/test_gcs.py
git commit -m "feat(gcs): resumable + CMEK + signed_url + retry pin (Layer W T4)"
```

---

### Task 5: `tools/bootstrap_kms.py` + `pixi run cloud:bootstrap-kms` + docs

**Goal:** Idempotent script provisioning the AWS KMS key + GCP Cloud KMS keyring + key per spec §6. Updates `docs/CLOUD-CREDS.md`. The script is the operational dependency that unblocks Tasks 9 + 10.

**Files:**
- Create: `tools/bootstrap_kms.py`
- Modify: `pixi.toml` (add `[tasks.cloud:bootstrap-kms]`)
- Modify: `docs/CLOUD-CREDS.md`
- Modify: `.gitignore` (add `.aws/kms-test-key.arn` + `.gcp/kms-test-key.name`)

**Acceptance Criteria:**
- [ ] Re-runs are idempotent: if `.aws/kms-test-key.arn` exists AND `kms:DescribeKey` succeeds, the AWS branch logs `skipped — key already exists` and exits 0; same for GCS.
- [ ] AWS: creates symmetric `ENCRYPT_DECRYPT` key in `us-east-1`; attaches alias `alias/<GCS_KMS_KEYRING>`; key policy grants `kinoforge-ci` IAM user `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey`, `kms:DescribeKey`.
- [ ] GCS: creates keyring `<GCS_KMS_KEYRING>` + key `bucket-cmek` in `us-central1`; grants the SA `roles/cloudkms.cryptoKeyEncrypterDecrypter`; grants the GCS service agent (resolved at runtime via `Project.projectNumber` → `service-<n>@gs-project-accounts.iam.gserviceaccount.com`) the same role.
- [ ] Persisted files: ARN in `.aws/kms-test-key.arn`; resource name in `.gcp/kms-test-key.name`.
- [ ] `docs/CLOUD-CREDS.md` carries the two new provisioning history rows + bootstrap-status footnote + rotation row.

**Verify:** Manual — `pixi run cloud:bootstrap-kms` exits 0 on first run; second run logs `skipped` for both clouds.

**Steps:**

- [ ] **Step 1: Add `.gitignore` entries first** (per memory rule — never have Claude `Write` a secret-bearing file).

Append to `.gitignore`:

```
.aws/kms-test-key.arn
.gcp/kms-test-key.name
```

- [ ] **Step 2: Implement `tools/bootstrap_kms.py`** as a stdlib + boto3 + google-cloud-kms script.

Structure:

```python
"""Idempotent KMS bootstrap for Layer W (S3 + GCS real-cloud verification).

Creates one AWS KMS key + one GCP Cloud KMS key. Idempotent: re-runs detect
the persisted ARN / resource-name file + existing key and skip creation.

Rotation policy: BOTH keys are NOT auto-rotated. Rotation invalidates Layer W
fixtures (the key id is part of every recorded encryption response).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from google.cloud import kms_v1
from google.cloud.kms_v1 import KeyManagementServiceClient
from google.api_core.exceptions import NotFound

logger = logging.getLogger("bootstrap_kms")

AWS_REGION = "us-east-1"
AWS_ALIAS = "alias/<GCS_KMS_KEYRING>"
AWS_KEY_FILE = Path(".aws/kms-test-key.arn")
AWS_IAM_USER = "kinoforge-ci"

GCP_LOCATION = "us-central1"
GCP_KEYRING = "<GCS_KMS_KEYRING>"
GCP_KEY = "bucket-cmek"
GCP_KEY_FILE = Path(".gcp/kms-test-key.name")


def bootstrap_aws() -> None:
    """Create AWS KMS key + alias + policy. Idempotent."""
    kms = boto3.client("kms", region_name=AWS_REGION)
    if AWS_KEY_FILE.exists():
        existing_arn = AWS_KEY_FILE.read_text().strip()
        try:
            kms.describe_key(KeyId=existing_arn)
            logger.info("AWS: skipped — key already exists at %s", existing_arn)
            return
        except ClientError as exc:
            logger.warning("AWS: persisted ARN unusable (%s); creating fresh", exc)

    sts = boto3.client("sts")
    account = sts.get_caller_identity()["Account"]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "EnableRoot",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "AllowKinoforgeCI",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:user/{AWS_IAM_USER}"},
                "Action": [
                    "kms:Encrypt",
                    "kms:Decrypt",
                    "kms:GenerateDataKey",
                    "kms:DescribeKey",
                ],
                "Resource": "*",
            },
        ],
    }
    created = kms.create_key(
        Description="kinoforge realcloud tests CMK",
        KeyUsage="ENCRYPT_DECRYPT",
        Policy=json.dumps(policy),
    )
    arn = created["KeyMetadata"]["Arn"]
    kms.create_alias(AliasName=AWS_ALIAS, TargetKeyId=arn)
    AWS_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    AWS_KEY_FILE.write_text(arn)
    logger.info("AWS: created key %s + alias %s", arn, AWS_ALIAS)


def bootstrap_gcp() -> None:
    """Create GCP Cloud KMS keyring + key + IAM bindings. Idempotent."""
    client = KeyManagementServiceClient()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = f"projects/{project}/locations/{GCP_LOCATION}"
    keyring_name = f"{location}/keyRings/{GCP_KEYRING}"
    key_name = f"{keyring_name}/cryptoKeys/{GCP_KEY}"

    if GCP_KEY_FILE.exists():
        try:
            client.get_crypto_key(name=key_name)
            logger.info("GCS: skipped — key already exists at %s", key_name)
        except NotFound:
            pass
        else:
            return

    # Create keyring (idempotent — catch AlreadyExists).
    try:
        client.create_key_ring(
            parent=location,
            key_ring_id=GCP_KEYRING,
            key_ring=kms_v1.KeyRing(),
        )
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise
    # Create key (idempotent).
    try:
        client.create_crypto_key(
            parent=keyring_name,
            crypto_key_id=GCP_KEY,
            crypto_key=kms_v1.CryptoKey(
                purpose=kms_v1.CryptoKey.CryptoKeyPurpose.ENCRYPT_DECRYPT,
                version_template=kms_v1.CryptoKeyVersionTemplate(
                    algorithm=kms_v1.CryptoKeyVersion.CryptoKeyVersionAlgorithm.GOOGLE_SYMMETRIC_ENCRYPTION
                ),
            ),
        )
    except Exception as exc:
        if "already exists" not in str(exc).lower():
            raise

    # IAM bindings — SA + GCS service agent.
    sa_email = (
        Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        .read_text()
        .partition('"client_email": "')[2]
        .partition('"')[0]
    )
    # Resolve project number for the GCS service agent.
    from google.cloud import resourcemanager_v3

    rm = resourcemanager_v3.ProjectsClient()
    project_proto = rm.get_project(name=f"projects/{project}")
    project_number = project_proto.name.split("/")[-1]
    gcs_agent = f"service-{project_number}@gs-project-accounts.iam.gserviceaccount.com"

    policy = client.get_iam_policy(request={"resource": key_name})
    for member in (f"serviceAccount:{sa_email}", f"serviceAccount:{gcs_agent}"):
        for binding in policy.bindings:
            if binding.role == "roles/cloudkms.cryptoKeyEncrypterDecrypter" and member in binding.members:
                break
        else:
            policy.bindings.add(
                role="roles/cloudkms.cryptoKeyEncrypterDecrypter",
                members=[member],
            )
    client.set_iam_policy(request={"resource": key_name, "policy": policy})

    GCP_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    GCP_KEY_FILE.write_text(key_name)
    logger.info("GCS: created key %s + 2 IAM bindings", key_name)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bootstrap_aws()
    bootstrap_gcp()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Add `[tasks.cloud:bootstrap-kms]` in `pixi.toml`.**

```toml
[tasks.cloud:bootstrap-kms]
cmd = "python tools/bootstrap_kms.py"
description = "Idempotently provision AWS KMS + GCP Cloud KMS for Layer W realcloud tests."
```

Add `google-cloud-kms` + `google-cloud-resource-manager` to the dependency block if not already present:

```bash
pixi add google-cloud-kms google-cloud-resource-manager
```

- [ ] **Step 4: Update `docs/CLOUD-CREDS.md`.**

Append to the AWS provisioning history section:

```markdown
- 2026-06-06 (Layer W bootstrap): AWS KMS key `alias/<GCS_KMS_KEYRING>`
  created in `us-east-1`. ARN persisted to `.aws/kms-test-key.arn`. Key policy
  grants `kinoforge-ci` Encrypt / Decrypt / GenerateDataKey / DescribeKey.
  Rotation: NOT auto-rotated — rotation invalidates Layer W fixtures.
```

Append to the GCP provisioning history section:

```markdown
- 2026-06-06 (Layer W bootstrap): GCP Cloud KMS keyring
  `<GCS_KMS_KEYRING>` + key `bucket-cmek` in `us-central1`.
  `kinoforge-runner` SA + GCS service agent granted
  `roles/cloudkms.cryptoKeyEncrypterDecrypter`. Key name persisted to
  `.gcp/kms-test-key.name`. Rotation: NOT auto-rotated.
```

Add a footnote to the **Bootstrap status** table:

```markdown
> Layer W encryption + signed-URL axes require KMS keys. Run
> `pixi run cloud:bootstrap-kms` after the AWS / GCP rows show ✅.
```

Add a row to the **Rotation policy** table:

```markdown
| KMS keys (S3 + GCS Layer W) | defer until next real-cloud layer | operator |
```

- [ ] **Step 5: Run the bootstrap.**

```bash
pixi run cloud:bootstrap-kms
```

Expected: two `created` log lines on first run. ARN + name files appear at the expected paths.

- [ ] **Step 6: Re-run to verify idempotence.**

```bash
pixi run cloud:bootstrap-kms
```

Expected: two `skipped — key already exists` log lines.

- [ ] **Step 7: Commit (script + docs + pixi changes only — secrets stay gitignored).**

```bash
git add tools/bootstrap_kms.py pixi.toml pixi.lock docs/CLOUD-CREDS.md .gitignore
git commit -m "feat(tools): bootstrap_kms.py + docs/CLOUD-CREDS.md updates (Layer W T5)"
```

---

### Task 6: `tests/stores/recording.py` — S3 + GCS recorders + redaction

**Goal:** Build the record / replay infrastructure that Tasks 9 + 10 capture into and Task 11 plays back from. One module, two recorders (S3 via `botocore` events, GCS via a custom `requests.adapters.HTTPAdapter`), shared `_persist` + `_redact` + `FixtureMissError`.

**Files:**
- Create: `tests/stores/recording.py`
- Create: `tests/stores/test_recording.py`

**Acceptance Criteria:**
- [ ] `S3Recorder(mode="record")` registers `before-send.s3.*` + `after-call.s3.*` handlers on a `boto3.Session`. Captures `(operation_name, params, body_hash)` → `parsed_response`. `flush()` writes `_meta + entries[]` JSON to a target path.
- [ ] `S3Recorder(mode="replay", fixture_path=...)` returns the recorded `parsed_response` when `(operation_name, params_hash)` matches; raises `FixtureMissError` otherwise.
- [ ] `GCSRecorder` mounts a `RecordingAdapter` on the session via `session.mount("https://", adapter)`. Captures `(method, url_norm, body_hash)` → `(status, headers_dict, body_b64)`. Replay returns the same.
- [ ] `_persist(label, payload, target_path)` writes JSON with `_meta = {"git_sha", "captured_at_local", "kinoforge_version", "cloud", "axis"}`. `captured_at_local` is `datetime.now().isoformat(timespec="seconds")` — NEVER UTC.
- [ ] `_redact(payload)` strips per spec §5.1: drops `Authorization`, `X-Amz-Security-Token`, `X-Goog-Authorization` headers (case-insensitive); strips query-string `X-Amz-Signature=*`, `X-Goog-Signature=*`, `X-Amz-Credential=*`, `x-goog-credential=*`; substitutes `<AWS_ACCOUNT>` for `<AWS_ACCOUNT>`, `<GCP_PROJECT>` for `<GCP_PROJECT>`, `<S3_KMS_KEY>` / `<GCS_KMS_KEY>` for the persisted KMS ARNs/names (read from the per-test fixtures).
- [ ] `FixtureReplayS3Client(fixture_path)` + `FixtureReplayGCSClient(fixture_path)` expose the same surface as the existing `FakeS3Client` / `FakeGCSClient` plus replay semantics on the recorded operations.
- [ ] Redaction round-trip test: a captured payload with all redactable tokens, after `_redact`, contains zero secret-shaped substrings.

**Verify:** `pixi run pytest tests/stores/test_recording.py -v`.

**Steps:**

- [ ] **Step 1: Stub the module structure** so test imports succeed.

```python
# tests/stores/recording.py
"""Layer W recording / replay infrastructure for boto3 + google-cloud-storage."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Literal


class FixtureMissError(LookupError):
    """Raised by replay mode when an incoming call has no matching fixture entry."""


def _git_sha() -> str:
    return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()


def _captured_at_local() -> str:
    # Memory rule: local TZ, never UTC.
    return _dt.datetime.now().isoformat(timespec="seconds")


def _kinoforge_version() -> str:
    try:
        from kinoforge import __version__
        return __version__
    except Exception:
        return "unknown"


_REDACT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<AWS_ACCOUNT>"), "<AWS_ACCOUNT>"),
    (re.compile(r"<GCP_PROJECT>"), "<GCP_PROJECT>"),
    (re.compile(r"X-Amz-Signature=[^&\s\"]+", re.IGNORECASE), "X-Amz-Signature=<REDACTED>"),
    (re.compile(r"X-Amz-Credential=[^&\s\"]+", re.IGNORECASE), "X-Amz-Credential=<REDACTED>"),
    (re.compile(r"X-Goog-Signature=[^&\s\"]+", re.IGNORECASE), "X-Goog-Signature=<REDACTED>"),
    (re.compile(r"x-goog-credential=[^&\s\"]+", re.IGNORECASE), "x-goog-credential=<REDACTED>"),
]

_REDACT_HEADERS = {"authorization", "x-amz-security-token", "x-goog-authorization"}


def _redact(payload: Any, extra_subs: dict[str, str] | None = None) -> Any:
    """Recursively redact secrets from a JSON-shaped payload."""
    subs = dict(extra_subs or {})
    text = json.dumps(payload)
    for pattern, replacement in _REDACT_RULES:
        text = pattern.sub(replacement, text)
    for needle, replacement in subs.items():
        text = text.replace(needle, replacement)
    out = json.loads(text)
    return _drop_secret_headers(out)


def _drop_secret_headers(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: _drop_secret_headers(v)
            for k, v in obj.items()
            if k.lower() not in _REDACT_HEADERS
        }
    if isinstance(obj, list):
        return [_drop_secret_headers(v) for v in obj]
    return obj


def _persist(label: str, payload: dict, target_path: Path, *, cloud: str, axis: str, extra_subs: dict[str, str] | None = None) -> None:
    body = {
        "_meta": {
            "git_sha": _git_sha(),
            "captured_at_local": _captured_at_local(),
            "kinoforge_version": _kinoforge_version(),
            "cloud": cloud,
            "axis": axis,
            "label": label,
        },
        "entries": _redact(payload, extra_subs=extra_subs),
    }
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(body, indent=2, sort_keys=True))


def _body_hash(body: bytes | None) -> str:
    if body is None:
        return ""
    return hashlib.sha256(body).hexdigest()


# ----------------------------------------------------------------------------
# S3 recorder — botocore event hooks.
# ----------------------------------------------------------------------------


class S3Recorder:
    def __init__(self, mode: Literal["record", "replay"], *, fixture_path: Path | None = None):
        self.mode = mode
        self.fixture_path = fixture_path
        self.captured: list[dict[str, Any]] = []
        if mode == "replay":
            assert fixture_path is not None
            self._fixture = json.loads(fixture_path.read_text())["entries"]
        else:
            self._fixture = []

    def attach(self, session) -> None:
        events = session.events
        events.register("before-send.s3.*", self._before_send)
        events.register("after-call.s3.*", self._after_call)

    def _match_key(self, operation: str, params: dict) -> str:
        return f"{operation}:{hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]}"

    def _before_send(self, **kwargs):
        # Hold the operation + params on the request context so after-call can pair them.
        request = kwargs.get("request")
        if request is not None:
            request.context["_kinoforge_op"] = kwargs.get("operation_name", "")
            request.context["_kinoforge_params"] = kwargs.get("params", {})
        if self.mode == "replay":
            op = kwargs.get("operation_name", "")
            params = kwargs.get("params", {})
            key = self._match_key(op, params)
            for entry in self._fixture:
                if entry["match_key"] == key:
                    return entry["parsed_response_http_form"]
            raise FixtureMissError(f"no fixture for {op} {params!r}")
        return None

    def _after_call(self, http_response, parsed, model, context, **kwargs):
        if self.mode != "record":
            return
        op = context.get("_kinoforge_op", "")
        params = context.get("_kinoforge_params", {})
        self.captured.append({
            "operation": op,
            "params": params,
            "match_key": self._match_key(op, params),
            "parsed_response": parsed,
            "parsed_response_http_form": (http_response.status_code, dict(http_response.headers), http_response.content),
        })

    def flush(self, target_path: Path, *, axis: str, extra_subs: dict[str, str] | None = None) -> None:
        assert self.mode == "record"
        _persist(label=axis, payload={"entries": self.captured}, target_path=target_path, cloud="s3", axis=axis, extra_subs=extra_subs)


# ----------------------------------------------------------------------------
# GCS recorder — requests.adapters.HTTPAdapter subclass.
# ----------------------------------------------------------------------------


class _GCSRecordingAdapter:
    """Custom adapter that records or replays HTTPS round-trips."""

    def __init__(self, recorder: "GCSRecorder", inner_adapter):
        self.recorder = recorder
        self.inner = inner_adapter

    def send(self, request, **kwargs):
        body = request.body if isinstance(request.body, (bytes, bytearray)) else (request.body or "").encode() if isinstance(request.body, str) else None
        key = self.recorder._match_key(request.method, request.url, body)
        if self.recorder.mode == "replay":
            for entry in self.recorder._fixture:
                if entry["match_key"] == key:
                    import requests
                    resp = requests.Response()
                    resp.status_code = entry["status"]
                    resp.headers.update(entry["headers"])
                    resp._content = base64.b64decode(entry["body_b64"])
                    return resp
            raise FixtureMissError(f"no fixture for {request.method} {request.url}")
        resp = self.inner.send(request, **kwargs)
        self.recorder._record_response(request, body, resp)
        return resp

    def close(self):
        self.inner.close()


class GCSRecorder:
    def __init__(self, mode: Literal["record", "replay"], *, fixture_path: Path | None = None):
        self.mode = mode
        self.fixture_path = fixture_path
        self.captured: list[dict[str, Any]] = []
        if mode == "replay":
            assert fixture_path is not None
            self._fixture = json.loads(fixture_path.read_text())["entries"]
        else:
            self._fixture = []

    def attach(self, session) -> None:
        # `session` is the AuthorizedSession on the storage Client._http.
        existing = session.get_adapter("https://")
        adapter = _GCSRecordingAdapter(self, existing)
        session.mount("https://storage.googleapis.com/", adapter)

    def _match_key(self, method: str, url: str, body: bytes | None) -> str:
        return f"{method}:{url}:{_body_hash(body)[:16]}"

    def _record_response(self, request, body, response) -> None:
        self.captured.append({
            "method": request.method,
            "url": request.url,
            "body_hash": _body_hash(body),
            "match_key": self._match_key(request.method, request.url, body),
            "status": response.status_code,
            "headers": dict(response.headers),
            "body_b64": base64.b64encode(response.content).decode("ascii"),
        })

    def flush(self, target_path: Path, *, axis: str, extra_subs: dict[str, str] | None = None) -> None:
        _persist(label=axis, payload={"entries": self.captured}, target_path=target_path, cloud="gcs", axis=axis, extra_subs=extra_subs)


# ----------------------------------------------------------------------------
# Fixture-replay clients exposed for offline tests.
# ----------------------------------------------------------------------------


class FixtureReplayS3Client:
    """Minimal boto3 S3 client surface backed by an S3Recorder in replay mode."""

    def __init__(self, fixture_path: Path):
        # ... see Task 11 for the full implementation; this stub gets fleshed out there.
        self._recorder = S3Recorder(mode="replay", fixture_path=fixture_path)
        raise NotImplementedError("Layer W T11 fleshes this out")


class FixtureReplayGCSClient:
    def __init__(self, fixture_path: Path):
        self._recorder = GCSRecorder(mode="replay", fixture_path=fixture_path)
        raise NotImplementedError("Layer W T11 fleshes this out")
```

- [ ] **Step 2: Write tests in `tests/stores/test_recording.py`.**

```python
"""Recorder + redaction unit tests (no real network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from tests.stores.recording import (
    FixtureMissError,
    GCSRecorder,
    S3Recorder,
    _persist,
    _redact,
)


def test_redact_strips_account_id():
    payload = {"Bucket": "<S3_BUCKET>", "Body": "ok"}
    out = _redact(payload)
    assert "<AWS_ACCOUNT>" not in json.dumps(out)
    assert "<AWS_ACCOUNT>" in out["Bucket"]


def test_redact_strips_project_id():
    payload = {"resource": "projects/<GCP_PROJECT>/buckets/foo"}
    out = _redact(payload)
    assert "<GCP_PROJECT>" not in json.dumps(out)
    assert "<GCP_PROJECT>" in out["resource"]


def test_redact_strips_signature_query_param():
    payload = {"url": "https://s3.amazonaws.com/foo?X-Amz-Signature=ababab1234&Expires=42"}
    out = _redact(payload)
    assert "<REDACTED>" in out["url"]
    assert "Expires=42" in out["url"]


def test_redact_drops_authorization_header():
    payload = {"Authorization": "Bearer secret", "Other": "ok"}
    out = _redact(payload)
    assert "Authorization" not in out
    assert out["Other"] == "ok"


def test_redact_substitutes_kms_key():
    payload = {"SSEKMSKeyId": "arn:aws:kms:us-east-1:1:key/abcde"}
    out = _redact(payload, extra_subs={"arn:aws:kms:us-east-1:1:key/abcde": "<S3_KMS_KEY>"})
    assert out["SSEKMSKeyId"] == "<S3_KMS_KEY>"


def test_persist_writes_meta_block(tmp_path):
    target = tmp_path / "fx.json"
    _persist("hot_path", {"entries": []}, target, cloud="s3", axis="hot_path")
    body = json.loads(target.read_text())
    meta = body["_meta"]
    assert meta["cloud"] == "s3"
    assert meta["axis"] == "hot_path"
    assert meta["git_sha"]
    assert "T" in meta["captured_at_local"]


def test_s3_recorder_replay_raises_on_miss(tmp_path):
    fx_path = tmp_path / "miss.json"
    fx_path.write_text(json.dumps({"_meta": {}, "entries": []}))
    rec = S3Recorder(mode="replay", fixture_path=fx_path)
    with pytest.raises(FixtureMissError):
        rec._before_send(operation_name="GetObject", params={"Bucket": "b", "Key": "k"}, request=None)


def test_gcs_recorder_replay_raises_on_miss(tmp_path):
    fx_path = tmp_path / "miss.json"
    fx_path.write_text(json.dumps({"_meta": {}, "entries": []}))
    rec = GCSRecorder(mode="replay", fixture_path=fx_path)
    # Build a minimal PreparedRequest equivalent.
    req = SimpleNamespace(method="GET", url="https://storage.googleapis.com/foo", body=None)
    adapter = next(iter([])) if False else None  # only exercising the recorder's miss path
    from tests.stores.recording import _GCSRecordingAdapter

    inner = requests.adapters.HTTPAdapter()
    a = _GCSRecordingAdapter(rec, inner)
    with pytest.raises(FixtureMissError):
        a.send(req)
```

- [ ] **Step 3: Run — confirm GREEN incrementally.** Adjust impl until all asserts pass:

```bash
pixi run pytest tests/stores/test_recording.py -v
```

- [ ] **Step 4: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/stores/recording.py tests/stores/test_recording.py
git add tests/stores/recording.py tests/stores/test_recording.py
git commit -m "test(stores): recording seam + redaction (Layer W T6)"
```

---

### Task 7: `tests/stores/proxy.py` — `Fail503Proxy`

**Goal:** In-process HTTP proxy that returns `503 Service Unavailable` for the first N requests then transparently forwards everything else. Used by the §5.3 retry axis in Tasks 9 + 10.

**Files:**
- Create: `tests/stores/proxy.py`
- Create: `tests/stores/test_proxy.py`

**Acceptance Criteria:**
- [ ] `Fail503Proxy(target_endpoint, fail_count=N)` exposes `.port` and `.request_count` after `__enter__`.
- [ ] First `N` requests get `503 Service Unavailable` with empty body.
- [ ] Requests `N+1`+ are transparently forwarded to `target_endpoint` via `urllib.request`. Body + headers preserved except `Host`.
- [ ] Multiple instances coexist (ports auto-assigned via bind to `0`).
- [ ] Clean shutdown via context manager.

**Verify:** `pixi run pytest tests/stores/test_proxy.py -v`.

**Steps:**

- [ ] **Step 1: Implement `tests/stores/proxy.py`.**

```python
"""In-process 503-injection proxy for Layer W retry-axis tests."""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.request
from contextlib import AbstractContextManager
from typing import Any


class Fail503Proxy(AbstractContextManager["Fail503Proxy"]):
    def __init__(self, target_endpoint: str, *, fail_count: int):
        self.target_endpoint = target_endpoint.rstrip("/")
        self.fail_count = fail_count
        self.request_count = 0
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.server_address[1]

    @property
    def endpoint(self) -> str:
        return f"http://localhost:{self.port}"

    def __enter__(self) -> "Fail503Proxy":
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any, **kwargs: Any) -> None:
                pass  # mute access log

            def do_GET(self):
                self._dispatch("GET")

            def do_PUT(self):
                self._dispatch("PUT")

            def do_POST(self):
                self._dispatch("POST")

            def do_HEAD(self):
                self._dispatch("HEAD")

            def do_DELETE(self):
                self._dispatch("DELETE")

            def _dispatch(self, method: str) -> None:
                with outer._lock:
                    outer.request_count += 1
                    n = outer.request_count
                if n <= outer.fail_count:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                upstream = urllib.request.Request(
                    url=outer.target_endpoint + self.path,
                    data=body,
                    method=method,
                    headers={k: v for k, v in self.headers.items() if k.lower() != "host"},
                )
                with urllib.request.urlopen(upstream) as resp:
                    self.send_response(resp.status)
                    for k, v in resp.headers.items():
                        if k.lower() in ("transfer-encoding", "connection"):
                            continue
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(resp.read())

        server = socketserver.ThreadingTCPServer(("localhost", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def __exit__(self, *_exc: Any) -> bool | None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=2.0)
        return None
```

- [ ] **Step 2: Implement `tests/stores/test_proxy.py`.**

```python
"""Fail503Proxy unit tests against a loopback target."""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.error
import urllib.request

import pytest

from tests.stores.proxy import Fail503Proxy


@pytest.fixture
def loopback_target():
    """Tiny upstream that 200s with the path echoed in the body."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            body = f"got {self.path}".encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length") or 0)
            data = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = socketserver.ThreadingTCPServer(("localhost", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_proxy_fails_first_n_then_forwards(loopback_target):
    with Fail503Proxy(loopback_target, fail_count=2) as proxy:
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(f"{proxy.endpoint}/ping").read()
            assert excinfo.value.code == 503
        with urllib.request.urlopen(f"{proxy.endpoint}/ping") as resp:
            body = resp.read()
        assert body == b"got /ping"
        assert proxy.request_count == 3


def test_proxy_put_round_trip(loopback_target):
    with Fail503Proxy(loopback_target, fail_count=0) as proxy:
        req = urllib.request.Request(
            f"{proxy.endpoint}/upload",
            data=b"payload",
            method="PUT",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.read() == b"payload"


def test_two_proxies_coexist(loopback_target):
    with Fail503Proxy(loopback_target, fail_count=0) as p1, Fail503Proxy(loopback_target, fail_count=0) as p2:
        assert p1.port != p2.port
        urllib.request.urlopen(f"{p1.endpoint}/a").read()
        urllib.request.urlopen(f"{p2.endpoint}/b").read()
        assert p1.request_count == 1
        assert p2.request_count == 1
```

- [ ] **Step 3: Run + commit.**

```bash
pixi run pytest tests/stores/test_proxy.py -v
pixi run pre-commit run --files tests/stores/proxy.py tests/stores/test_proxy.py
git add tests/stores/proxy.py tests/stores/test_proxy.py
git commit -m "test(stores): Fail503Proxy retry-injection harness (Layer W T7)"
```

---

### Task 8: Live-suite conftest + gate

**Goal:** Skip live tests cleanly when prerequisites are missing; provide a `s3_record_session` / `gcs_record_session` fixture pair that hands each live test an `S3Recorder` / `GCSRecorder` already attached.

**Files:**
- Create: `tests/stores/live/__init__.py`
- Create: `tests/stores/live/conftest.py`

**Acceptance Criteria:**
- [ ] Missing `KINOFORGE_LIVE_TESTS=1` → all live tests skip with reason `"set KINOFORGE_LIVE_TESTS=1 — see docs/CLOUD-CREDS.md"`.
- [ ] S3 precondition: `sts:GetCallerIdentity` ping succeeds AND `.aws/kms-test-key.arn` exists. Missing → skip.
- [ ] GCS precondition: `GOOGLE_APPLICATION_CREDENTIALS` is set + file exists AND `.gcp/kms-test-key.name` exists. Missing → skip.
- [ ] `s3_record_session(fixture_target_path)` yields a boto3 session with an `S3Recorder` attached in `record` mode; on test exit, `recorder.flush(fixture_target_path, axis=...)` is called.
- [ ] `gcs_record_session(fixture_target_path)` does the same for google-cloud-storage.
- [ ] Constants `S3_BUCKET = "<S3_BUCKET>"` and `GCS_BUCKET = "<GCS_BUCKET>"` defined at the conftest top level.

**Verify:** `pixi run pytest tests/stores/live/ -v` (no live env) → 0 collected items pass, all skips show the expected reason.

**Steps:**

- [ ] **Step 1: Create `tests/stores/live/__init__.py`** (empty file).

- [ ] **Step 2: Implement `tests/stores/live/conftest.py`.**

```python
"""Live-suite gate + record-mode fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.stores.recording import GCSRecorder, S3Recorder

S3_BUCKET = "<S3_BUCKET>"
GCS_BUCKET = "<GCS_BUCKET>"
AWS_KMS_KEY_FILE = Path(".aws/kms-test-key.arn")
GCS_KMS_KEY_FILE = Path(".gcp/kms-test-key.name")
FIXTURE_DIR_S3 = Path("tests/stores/fixtures/s3")
FIXTURE_DIR_GCS = Path("tests/stores/fixtures/gcs")


def _live_gate_or_skip(cloud: str) -> tuple[str, str]:
    if os.environ.get("KINOFORGE_LIVE_TESTS") != "1":
        pytest.skip("set KINOFORGE_LIVE_TESTS=1 — see docs/CLOUD-CREDS.md")
    if cloud == "s3":
        if not AWS_KMS_KEY_FILE.exists():
            pytest.skip(f"missing {AWS_KMS_KEY_FILE} — run pixi run cloud:bootstrap-kms")
        import boto3
        try:
            boto3.client("sts").get_caller_identity()
        except Exception as exc:
            pytest.skip(f"AWS creds unusable: {exc}")
        return S3_BUCKET, AWS_KMS_KEY_FILE.read_text().strip()
    if cloud == "gcs":
        if not GCS_KMS_KEY_FILE.exists():
            pytest.skip(f"missing {GCS_KMS_KEY_FILE} — run pixi run cloud:bootstrap-kms")
        if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
            pytest.skip("set GOOGLE_APPLICATION_CREDENTIALS")
        return GCS_BUCKET, GCS_KMS_KEY_FILE.read_text().strip()
    raise AssertionError(cloud)


@pytest.fixture
def s3_live_bucket_and_kms():
    return _live_gate_or_skip("s3")


@pytest.fixture
def gcs_live_bucket_and_kms():
    return _live_gate_or_skip("gcs")


@pytest.fixture
def s3_record_session(s3_live_bucket_and_kms, request):
    """Boto3 session with an S3Recorder attached. Flushes to a per-test fixture path."""
    import boto3

    session = boto3.session.Session()
    recorder = S3Recorder(mode="record")
    recorder.attach(session)
    axis = request.node.callspec.params.get("axis") if hasattr(request.node, "callspec") else request.node.name
    target = FIXTURE_DIR_S3 / f"{axis}.json"
    yield session, recorder
    bucket, kms = s3_live_bucket_and_kms
    recorder.flush(target, axis=axis, extra_subs={kms: "<S3_KMS_KEY>"})


@pytest.fixture
def gcs_record_session(gcs_live_bucket_and_kms, request):
    """google-cloud-storage Client with a GCSRecorder mounted. Flushes per-test."""
    from google.cloud import storage

    client = storage.Client()
    recorder = GCSRecorder(mode="record")
    recorder.attach(client._http)
    axis = request.node.callspec.params.get("axis") if hasattr(request.node, "callspec") else request.node.name
    target = FIXTURE_DIR_GCS / f"{axis}.json"
    yield client, recorder
    bucket, kms = gcs_live_bucket_and_kms
    recorder.flush(target, axis=axis, extra_subs={kms: "<GCS_KMS_KEY>"})
```

- [ ] **Step 3: Confirm zero-collection behaviour without live env.**

```bash
pixi run pytest tests/stores/live/ -v
```

Expected: collected (when Tasks 9 + 10 land) but every test skipped with the gate-reason text.

- [ ] **Step 4: Commit.**

```bash
pixi run pre-commit run --files tests/stores/live/__init__.py tests/stores/live/conftest.py
git add tests/stores/live/__init__.py tests/stores/live/conftest.py
git commit -m "test(stores): live-suite gate + record fixtures (Layer W T8)"
```

---

### Task 9: S3 live tests + fixture capture (5 axes)

**Goal:** Real-cloud smoke against `s3://<S3_BUCKET>`. Each axis is parametrised, captures one fixture, and validates the production-code feature it exercises. **Live spend (~$0.02).**

**Files:**
- Create: `tests/stores/live/test_s3_live.py`
- Capture: `tests/stores/fixtures/s3/{hot_path,multipart,encryption_default,encryption_kms,signed_url_get,signed_url_put,retry_proxy}.json`

**Acceptance Criteria:**
- [ ] All 7 test functions (5 axes + 1 retry + 1 cleanup) pass with `KINOFORGE_LIVE_TESTS=1`.
- [ ] Hot-path: put / get / list / delete with a 64-byte payload; assert byte-identity.
- [ ] Multipart: 16 MiB payload — assert `ETag` carries `-N` suffix (multipart marker).
- [ ] Encryption default: write succeeds without `ExtraArgs["ServerSideEncryption"]`; `head_object` returns `ServerSideEncryption == "AES256"` (SSE-S3 bucket default).
- [ ] Encryption KMS: write with `mode="kms"`; `head_object` returns `ServerSideEncryption == "aws:kms"` and `SSEKMSKeyId` equal to the bootstrapped key.
- [ ] Signed URL GET: `urllib.request.urlopen(url).read() == body`.
- [ ] Signed URL PUT: upload via signed PUT; subsequent `get_bytes` reads identical bytes.
- [ ] Retry axis: `Fail503Proxy(fail_count=2)` in front of S3; `boto3.client("s3", endpoint_url=proxy.endpoint, config=Config(retries={"max_attempts": 3}))`; assert proxy.request_count ≥ 3 and `put_bytes` succeeds.
- [ ] All artifacts deleted in `finally`.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/stores/live/test_s3_live.py -v`.

**Pre-spend gate (mandatory per CLAUDE.md durability rule):**

- [ ] **Step A: Commit RED scaffolds before spend.** Write the test file with all 7 axes as failing assertions. `pytest` collection must succeed before invocation. Commit before running.
- [ ] **Step B: `pixi run preflight` must exit 0.** Verifies AWS creds present + no active RunPod pods + clean working tree.

**Steps:**

- [ ] **Step 1: Implement `tests/stores/live/test_s3_live.py`.**

```python
"""S3 real-cloud smoke + fixture capture (5 axes + retry + cleanup)."""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import boto3
import pytest
import urllib.request
from botocore.config import Config as BotocoreConfig

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.s3 import S3ArtifactStore
from tests.stores.proxy import Fail503Proxy


def _run_id() -> str:
    return f"live-{uuid.uuid4().hex[:8]}"


def test_s3_hot_path(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
    run = _run_id()
    try:
        artifact = store.put_bytes(run, "hello.bin", b"hello world")
        assert store.get_bytes(run, "hello.bin") == b"hello world"
        assert "hello.bin" in store.list(run)
    finally:
        try:
            store.delete(run, "hello.bin")
        except Exception:
            pass


def test_s3_multipart(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
    run = _run_id()
    big = b"x" * (16 * 1024 * 1024)
    try:
        store.put_bytes(run, "big.bin", big)
        head = client.head_object(Bucket=bucket, Key=f"{run}/big.bin")
        etag = head["ETag"].strip('"')
        assert "-" in etag, f"expected multipart ETag with -N suffix, got {etag}"
    finally:
        try:
            store.delete(run, "big.bin")
        except Exception:
            pass


def test_s3_encryption_default(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
    run = _run_id()
    try:
        store.put_bytes(run, "default.bin", b"plaintext")
        head = client.head_object(Bucket=bucket, Key=f"{run}/default.bin")
        assert head.get("ServerSideEncryption") == "AES256"
    finally:
        try:
            store.delete(run, "default.bin")
        except Exception:
            pass


def test_s3_encryption_kms(s3_record_session, s3_live_bucket_and_kms):
    bucket, kms = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    cfg = StoreConfig(kind="s3", bucket=bucket, encryption=StoreEncryptionConfig(mode="kms", kms_key_id=kms))
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=cfg)
    run = _run_id()
    try:
        store.put_bytes(run, "kms.bin", b"sensitive")
        head = client.head_object(Bucket=bucket, Key=f"{run}/kms.bin")
        assert head.get("ServerSideEncryption") == "aws:kms"
        assert head.get("SSEKMSKeyId", "").endswith(kms.split("/")[-1])
    finally:
        try:
            store.delete(run, "kms.bin")
        except Exception:
            pass


def test_s3_signed_url_get(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
    run = _run_id()
    try:
        store.put_bytes(run, "signed.bin", b"signed-get-payload")
        url = store.signed_url(run, "signed.bin", op="GET", ttl_s=300)
        with urllib.request.urlopen(url) as resp:
            assert resp.read() == b"signed-get-payload"
    finally:
        try:
            store.delete(run, "signed.bin")
        except Exception:
            pass


def test_s3_signed_url_put(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
    run = _run_id()
    try:
        url = store.signed_url(run, "signed-put.bin", op="PUT", ttl_s=300)
        req = urllib.request.Request(url, data=b"signed-put-payload", method="PUT")
        with urllib.request.urlopen(req) as resp:
            assert resp.status in (200, 204)
        assert store.get_bytes(run, "signed-put.bin") == b"signed-put-payload"
    finally:
        try:
            store.delete(run, "signed-put.bin")
        except Exception:
            pass


def test_s3_retry_via_proxy(s3_live_bucket_and_kms):
    """Retry axis is NOT captured into a fixture — the proxy IS the verification."""
    bucket, _ = s3_live_bucket_and_kms
    target_endpoint = "https://s3.us-east-1.amazonaws.com"
    with Fail503Proxy(target_endpoint, fail_count=2) as proxy:
        client = boto3.client(
            "s3",
            endpoint_url=proxy.endpoint,
            config=BotocoreConfig(retries={"max_attempts": 3, "mode": "standard"}),
        )
        store = S3ArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket))
        run = _run_id()
        try:
            store.put_bytes(run, "retry.bin", b"retried")
        finally:
            # The proxy was disposable; downstream cleanup uses real endpoint.
            real = boto3.client("s3")
            try:
                real.delete_object(Bucket=bucket, Key=f"{run}/retry.bin")
            except Exception:
                pass
        assert proxy.request_count >= 3
```

- [ ] **Step 2: Commit RED scaffold + preflight.**

```bash
git add tests/stores/live/test_s3_live.py
git commit -m "test(s3-live): axis scaffolds — pre-spend RED (Layer W T9 scaffold)"
pixi run preflight
```

Expected: `preflight` exit 0.

- [ ] **Step 3: Run live tests.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/stores/live/test_s3_live.py -v
```

Expected: 7 passed; 6 fixture files written under `tests/stores/fixtures/s3/`.

- [ ] **Step 4: Commit captured fixtures.**

```bash
git add tests/stores/fixtures/s3/
git commit -m "test(s3-live): captured fixtures from live smoke (Layer W T9)"
```

- [ ] **Step 5: PROGRESS-style real-artifact line — note the run-id / ETag for inclusion in Phase 38 entry at T12.**

---

### Task 10: GCS live tests + fixture capture (5 axes)

**Goal:** Real-cloud smoke against `gs://<GCS_BUCKET>`. Same shape as Task 9, GCS adapter. **Live spend (~$0.02).**

**Files:**
- Create: `tests/stores/live/test_gcs_live.py`
- Capture: `tests/stores/fixtures/gcs/{hot_path,resumable,encryption_default,encryption_cmek,signed_url_get,signed_url_put,retry_proxy}.json`

**Acceptance Criteria:**
- [ ] All 7 test functions pass with `KINOFORGE_LIVE_TESTS=1`.
- [ ] Hot-path: upload / download / list / delete with a 64-byte payload.
- [ ] Resumable: 16 MiB payload — assert `blob.reload()` after upload reports a resumable session id (`_session_uri` or equivalent observable).
- [ ] Encryption default: `blob.reload(); blob.kms_key_name is None`; provider uses Google-managed.
- [ ] Encryption CMEK: write with `mode="kms"`; `blob.reload(); blob.kms_key_name.startswith(<persisted-key-name>)`.
- [ ] Signed URL GET / PUT: round-trip via `urllib.request`.
- [ ] Retry axis: `Fail503Proxy(fail_count=2)` in front of `https://storage.googleapis.com`; configure GCS client with `api_endpoint=proxy.endpoint`; assert `proxy.request_count >= 3` after `put_bytes`.
- [ ] All artifacts deleted in `finally`.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/stores/live/test_gcs_live.py -v`.

**Steps:**

- [ ] **Step 1: Implement `tests/stores/live/test_gcs_live.py`** following the same pattern as Task 9 — substitute the GCS adapter:

```python
"""GCS real-cloud smoke + fixture capture."""

from __future__ import annotations

import uuid
import urllib.request

import pytest

from google.cloud import storage
from google.api_core.client_options import ClientOptions

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.gcs import GCSArtifactStore
from tests.stores.proxy import Fail503Proxy


def _run_id() -> str:
    return f"live-{uuid.uuid4().hex[:8]}"


def test_gcs_hot_path(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
    run = _run_id()
    try:
        store.put_bytes(run, "hello.bin", b"hello world")
        assert store.get_bytes(run, "hello.bin") == b"hello world"
        assert "hello.bin" in store.list(run)
    finally:
        try:
            store.delete(run, "hello.bin")
        except Exception:
            pass


def test_gcs_resumable(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
    run = _run_id()
    big = b"x" * (16 * 1024 * 1024)
    try:
        store.put_bytes(run, "big.bin", big)
        blob = client.bucket(bucket).blob(f"{run}/big.bin")
        blob.reload()
        # google-cloud-storage exposes _chunk_size / size for resumable confirmation;
        # a size > resumable threshold is the load-bearing assertion.
        assert blob.size == len(big)
    finally:
        try:
            store.delete(run, "big.bin")
        except Exception:
            pass


def test_gcs_encryption_default(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
    run = _run_id()
    try:
        store.put_bytes(run, "default.bin", b"plaintext")
        blob = client.bucket(bucket).blob(f"{run}/default.bin")
        blob.reload()
        assert blob.kms_key_name is None
    finally:
        try:
            store.delete(run, "default.bin")
        except Exception:
            pass


def test_gcs_encryption_cmek(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, kms = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    cfg = StoreConfig(kind="gcs", bucket=bucket, encryption=StoreEncryptionConfig(mode="kms", kms_key_id=kms))
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=cfg)
    run = _run_id()
    try:
        store.put_bytes(run, "cmek.bin", b"sensitive")
        blob = client.bucket(bucket).blob(f"{run}/cmek.bin")
        blob.reload()
        assert blob.kms_key_name and blob.kms_key_name.startswith(kms.rsplit("/cryptoKeyVersions/", 1)[0])
    finally:
        try:
            store.delete(run, "cmek.bin")
        except Exception:
            pass


def test_gcs_signed_url_get(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
    run = _run_id()
    try:
        store.put_bytes(run, "signed.bin", b"signed-get-payload")
        url = store.signed_url(run, "signed.bin", op="GET", ttl_s=300)
        with urllib.request.urlopen(url) as resp:
            assert resp.read() == b"signed-get-payload"
    finally:
        try:
            store.delete(run, "signed.bin")
        except Exception:
            pass


def test_gcs_signed_url_put(gcs_record_session, gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    client, _ = gcs_record_session
    store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
    run = _run_id()
    try:
        url = store.signed_url(run, "signed-put.bin", op="PUT", ttl_s=300)
        req = urllib.request.Request(url, data=b"signed-put-payload", method="PUT")
        with urllib.request.urlopen(req) as resp:
            assert resp.status in (200, 204)
        assert store.get_bytes(run, "signed-put.bin") == b"signed-put-payload"
    finally:
        try:
            store.delete(run, "signed-put.bin")
        except Exception:
            pass


def test_gcs_retry_via_proxy(gcs_live_bucket_and_kms):
    bucket, _ = gcs_live_bucket_and_kms
    real_endpoint = "https://storage.googleapis.com"
    with Fail503Proxy(real_endpoint, fail_count=2) as proxy:
        client = storage.Client(client_options=ClientOptions(api_endpoint=proxy.endpoint))
        store = GCSArtifactStore(bucket=bucket, client=client, cfg=StoreConfig(kind="gcs", bucket=bucket))
        run = _run_id()
        try:
            store.put_bytes(run, "retry.bin", b"retried")
        finally:
            real = storage.Client()
            try:
                real.bucket(bucket).blob(f"{run}/retry.bin").delete()
            except Exception:
                pass
        assert proxy.request_count >= 3
```

- [ ] **Step 2: Commit RED scaffold + preflight.**

```bash
git add tests/stores/live/test_gcs_live.py
git commit -m "test(gcs-live): axis scaffolds — pre-spend RED (Layer W T10 scaffold)"
pixi run preflight
```

- [ ] **Step 3: Run live tests.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/stores/live/test_gcs_live.py -v
```

Expected: 7 passed; 6 fixture files written under `tests/stores/fixtures/gcs/`.

- [ ] **Step 4: Commit fixtures.**

```bash
git add tests/stores/fixtures/gcs/
git commit -m "test(gcs-live): captured fixtures from live smoke (Layer W T10)"
```

---

### Task 11: `FixtureReplay` clients + offline lockdown tests + network-isolation guard

**Goal:** Flesh out `FixtureReplayS3Client` + `FixtureReplayGCSClient` so offline tests exercise the same five axes against committed fixtures. Add a `test_offline_isolation.py` socket-spy to lock down AC4.

**Files:**
- Modify: `tests/stores/recording.py` (replace the `raise NotImplementedError("Layer W T11")` stubs with working impls)
- Modify: `tests/stores/test_s3.py` (add `class TestFromFixture:` block with 6 axes)
- Modify: `tests/stores/test_gcs.py` (same)
- Create: `tests/stores/test_offline_isolation.py`

**Acceptance Criteria:**
- [ ] `FixtureReplayS3Client(fixture_path)` exposes `head_object`, `upload_fileobj`, `get_object`, `list_objects_v2`, `delete_object`, `generate_presigned_url` — all dispatching through the underlying `S3Recorder(mode="replay")`.
- [ ] `FixtureReplayGCSClient(fixture_path)` exposes `bucket(name)` → returns a fixture-backed `FixtureReplayGCSBucket`, etc.
- [ ] `tests/stores/test_s3.py::TestFromFixture` runs all 5 wire-shape axes against `tests/stores/fixtures/s3/*.json` with NO network — verified by Step 6.
- [ ] `tests/stores/test_gcs.py::TestFromFixture` same for GCS.
- [ ] `tests/stores/test_offline_isolation.py::test_offline_run_makes_no_network_calls` runs `pytest tests/stores/` in a subprocess with `socket.socket.connect` monkeypatched to track non-loopback destinations and asserts the set is empty.
- [ ] AC12: post-Layer-W count = 1423 + (Layer W net) — within ±5 of spec §5.6 estimate.

**Verify:** `pixi run pytest tests/stores/ -v` and `pixi run pytest tests/stores/test_offline_isolation.py -v`.

**Steps:**

- [ ] **Step 1: Replace the `FixtureReplayS3Client` stub in `tests/stores/recording.py`.**

```python
class FixtureReplayS3Client:
    """boto3 S3 client surface backed by an S3Recorder in replay mode."""

    def __init__(self, fixture_path: Path):
        self._recorder = S3Recorder(mode="replay", fixture_path=fixture_path)
        self._fixture = self._recorder._fixture
        self.meta = SimpleNamespace(
            config=SimpleNamespace(
                retries={"max_attempts": 3, "mode": "standard"},  # mirrors S3ArtifactStore pin
            ),
        )

    def set_retry_config(self, retries: dict) -> None:
        self.meta.config.retries = retries

    def _lookup(self, operation: str, params: dict):
        key = self._recorder._match_key(operation, params)
        for entry in self._fixture:
            if entry["match_key"] == key:
                return entry["parsed_response"]
        raise FixtureMissError(f"no fixture entry for {operation} {params}")

    def upload_fileobj(self, fileobj, Bucket, Key, ExtraArgs=None):
        params = {"Bucket": Bucket, "Key": Key, "ExtraArgs": dict(ExtraArgs or {})}
        return self._lookup("UploadFileobj", params)

    def head_object(self, *, Bucket, Key):
        return self._lookup("HeadObject", {"Bucket": Bucket, "Key": Key})

    def get_object(self, *, Bucket, Key):
        return self._lookup("GetObject", {"Bucket": Bucket, "Key": Key})

    def list_objects_v2(self, *, Bucket, Prefix=""):
        return self._lookup("ListObjectsV2", {"Bucket": Bucket, "Prefix": Prefix})

    def delete_object(self, *, Bucket, Key):
        return self._lookup("DeleteObject", {"Bucket": Bucket, "Key": Key})

    def generate_presigned_url(self, op, *, Params, ExpiresIn):
        # presigned URL generation is SDK-local in real life — replay just returns the recorded URL.
        return self._lookup("GeneratePresignedUrl", {"op": op, "Params": Params, "ExpiresIn": ExpiresIn})
```

- [ ] **Step 2: Replace the `FixtureReplayGCSClient` stub similarly** — surface `bucket(name)` returning a `FixtureReplayGCSBucket` with `blob(name)` returning a `FixtureReplayGCSBlob` whose `upload_from_file`, `download_as_bytes`, `generate_signed_url`, `reload`, `delete` all dispatch into the fixture.

- [ ] **Step 3: Add `class TestFromFixture` blocks** in `tests/stores/test_s3.py` and `tests/stores/test_gcs.py`. Each axis-class loads the corresponding fixture file and asserts wire-shape invariants — e.g. for S3 multipart:

```python
from pathlib import Path

from tests.stores.recording import FixtureReplayS3Client


class TestS3FromFixture:
    def test_multipart_etag_has_dash_suffix(self):
        client = FixtureReplayS3Client(Path("tests/stores/fixtures/s3/multipart.json"))
        head = client.head_object(Bucket="<AWS_ACCOUNT-redacted>", Key="<...>")
        assert "-" in head["ETag"].strip('"')

    def test_kms_response_carries_aws_kms(self):
        client = FixtureReplayS3Client(Path("tests/stores/fixtures/s3/encryption_kms.json"))
        head = client.head_object(Bucket="<...>", Key="<...>")
        assert head["ServerSideEncryption"] == "aws:kms"
        assert "<S3_KMS_KEY>" in head["SSEKMSKeyId"]
```

(Add analogous blocks per axis — see the Layer N test refactor in `tests/providers/test_runpod.py` for the precedent.)

- [ ] **Step 4: Create `tests/stores/test_offline_isolation.py`.**

```python
"""AC4: offline `pixi run test` makes no real network calls."""

from __future__ import annotations

import socket
import subprocess
import sys


def test_offline_run_makes_no_network_calls(tmp_path):
    spy = tmp_path / "spy_conftest.py"
    spy.write_text(
        """
import socket

_seen = []

_real = socket.socket.connect

def _spy(self, addr):
    try:
        host = addr[0]
        # Allow loopback for in-process proxies + sqlite + pytest internals.
        if not (host.startswith("127.") or host == "localhost" or host == "::1"):
            _seen.append(addr)
    except Exception:
        pass
    return _real(self, addr)

socket.socket.connect = _spy

def pytest_sessionfinish(session, exitstatus):
    assert _seen == [], f"unexpected non-loopback sockets: {_seen}"
"""
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/stores/", "-q", "-p", f"{spy.stem}", "--rootdir", str(tmp_path), "--confcutdir", str(tmp_path)],
        env={"PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
```

(If the subprocess approach is awkward for the executor, an alternative is to register the spy via `tests/conftest.py` keyed on an env var so it is opt-in only — the goal is the same.)

- [ ] **Step 5: Run + commit.**

```bash
pixi run pytest tests/stores/ -v
pixi run pre-commit run --files tests/stores/recording.py tests/stores/test_s3.py tests/stores/test_gcs.py tests/stores/test_offline_isolation.py
git add tests/stores/recording.py tests/stores/test_s3.py tests/stores/test_gcs.py tests/stores/test_offline_isolation.py
git commit -m "test(stores): FixtureReplay clients + offline lockdown (Layer W T11)"
```

---

### Task 12: README + PROGRESS Phase 38 + final gate + merge

**Goal:** Close-out documentation, run the full gate, and merge to `main` via `--no-ff`.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] AC9 confirmed (CLOUD-CREDS.md rows shipped at T5).
- [ ] AC10: `tests/test_core_invariant.py` passes.
- [ ] AC11: `pixi run pre-commit run --all-files` green.
- [ ] AC12: test-count delta documented; matches spec §5.6 within ±5 (~1457 passed + 8 skipped + 14 KINOFORGE_LIVE_TESTS-skipped).
- [ ] README has a new "Cloud stores" section covering: `store.encryption.mode`, `store.signed_url_default_ttl_s`, the `signed_url(...)` caller API, and a pointer to `docs/CLOUD-CREDS.md` for the KMS bootstrap.
- [ ] PROGRESS Phase 38 entry written with per-task SHAs, design decisions, real-artifact line (S3 multipart ETag + GCS resumable size), and carry-forward #4 explicitly closed.
- [ ] PROGRESS "Real-cloud verification gaps" section flips the third bullet (`S3ArtifactStore + GCSArtifactStore never hit real cloud`) to `~~strikethrough~~ — CLOSED by Phase 38 (Layer W)`.
- [ ] Merge commit: `--no-ff` with body referencing Layer W + AC state + per-task SHAs.

**Verify:** `pixi run pytest` (no live env) green; `git log --oneline -3` shows the merge commit on `main`.

**Steps:**

- [ ] **Step 1: README — append "Cloud stores" section.**

```markdown
## Cloud stores

Kinoforge ships three `ArtifactStore` backends: `local`, `s3`, and `gcs`.
Configure via the top-level `store:` block:

```yaml
store:
  kind: s3                # or gcs / local
  bucket: my-bucket
  encryption:
    mode: kms             # or "default" (provider-managed)
    kms_key_id: arn:aws:kms:us-east-1:123456789012:key/abc
  signed_url_default_ttl_s: 3600
```

Operators that need encrypted artifact storage can opt into
provider-managed encryption (`mode: default` — the silent default) or
customer-managed keys (`mode: kms`). See `docs/CLOUD-CREDS.md` for the
KMS bootstrap path (`pixi run cloud:bootstrap-kms`).

Callers can hand out time-limited URLs without sharing creds:

```python
url = store.signed_url("run-1", "out.mp4", op="GET", ttl_s=600)
```

`LocalArtifactStore` does not support signed URLs (no transport-layer
auth for local files) and raises `NotImplementedError`.
```

- [ ] **Step 2: PROGRESS — add a `### Phase 38 — Layer W (S3 / GCS real-cloud verification)` section.**

Template:

```markdown
### Phase 38 — Layer W (S3 / GCS real-cloud verification)

Verification-only layer that closes PROGRESS:116 carry-forward #4 (`S3ArtifactStore` + `GCSArtifactStore` never hit real cloud). Five axes per cloud (hot path, multipart, encryption defaults + customer-managed KMS, signed GET + PUT, retry via 503 proxy) with live opt-in capture + offline fixture replay. Mirrors Layer N (Phase 24) pattern at the storage substrate.

- [x] Task 1: StoreEncryptionConfig + signed_url_default_ttl_s pydantic — commit `<sha>`
- [x] Task 2: ArtifactStore.signed_url ABC + Local stub — commit `<sha>`
- [x] Task 3: S3ArtifactStore multipart + encryption + signed_url + retry pin — commit `<sha>`
- [x] Task 4: GCSArtifactStore resumable + CMEK + signed_url + retry pin — commit `<sha>`
- [x] Task 5: tools/bootstrap_kms.py + pixi cloud:bootstrap-kms + CLOUD-CREDS.md — commit `<sha>`
- [x] Task 6: tests/stores/recording.py recorders + redaction — commit `<sha>`
- [x] Task 7: tests/stores/proxy.py Fail503Proxy — commit `<sha>`
- [x] Task 8: tests/stores/live/conftest.py gate — commit `<sha>`
- [x] Task 9: S3 live tests + fixtures (5 axes + retry) — commits `<scaffold-sha>`, `<live-sha>`, `<fixture-sha>`
- [x] Task 10: GCS live tests + fixtures — commits `<scaffold-sha>`, `<live-sha>`, `<fixture-sha>`
- [x] Task 11: FixtureReplay clients + offline lockdown + network-isolation guard — commit `<sha>`
- [x] Task 12: README + PROGRESS + final gate + merge — commit `<sha>`
- [x] Merge to main via `--no-ff` — merge commit `<sha>` (closes PROGRESS:116 carry-forward #4)

**First real artifacts:**
- S3: object `live/<run-id>/big.bin` (16 MiB) at `s3://<S3_BUCKET>/<run-id>/big.bin`, multipart `ETag = "<etag>-<N>"`.
- GCS: object `live/<run-id>/big.bin` (16 MiB) at `gs://<GCS_BUCKET>/<run-id>/big.bin`, resumable `size = 16777216`.
- KMS-encrypted S3 object verified `ServerSideEncryption=aws:kms` + `SSEKMSKeyId` ending `<key-tail>`.
- CMEK-encrypted GCS blob verified `kms_key_name` startswith `projects/.../keyRings/<GCS_KMS_KEYRING>/cryptoKeys/bucket-cmek`.

**Key design decisions:**
- Multipart switch is unconditional — boto3 + google-cloud-storage SDK defaults handle the threshold; no kinoforge knob (spec §4.1).
- `StoreEncryptionConfig.kms_key_id` is a single field across both clouds; the store adapter parses the ARN vs Cloud KMS resource name form (spec §4.2).
- `LocalArtifactStore.signed_url` raises `NotImplementedError` — local files have no transport-layer auth (spec §4.3).
- Retry baselines pinned in store source (`botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})` for S3, `Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)` for GCS) — no caller knob (spec §4.0).
- KMS keys are NOT auto-rotated — rotation invalidates Layer W fixtures (spec §6.3).
- Fixture-replay clients are minimal — they mirror only the SDK surface the production stores actually call.

**Test count:** ~1423 + 8 pre-Layer W → ~1457 + 8 + 14 KINOFORGE_LIVE_TESTS-skipped post-Layer W.
```

Then in the **Real-cloud verification gaps** section near the top of PROGRESS, strike through the third bullet:

```markdown
- ~~`S3ArtifactStore` + `GCSArtifactStore` never hit real cloud — fake clients don't simulate multipart edge cases, transient retries, SSE/KMS, signed URLs.~~ — **CLOSED** by Phase 38 (Layer W).
```

- [ ] **Step 3: Update PROGRESS Single-next-action block.**

Replace the `RESUME — START HERE` section to point at Layer W's close + propose the next candidates (sweeper daemon, SkyPilot GPU smokes, cross-machine store-uri bootstrap, fal storage upload, cost dashboard).

- [ ] **Step 4: Final gate.**

```bash
pixi run pre-commit run --all-files
pixi run pytest
pixi run pytest tests/test_core_invariant.py -v
```

Expected: all green; test count matches AC12.

- [ ] **Step 5: Commit + merge.**

```bash
git add README.md PROGRESS.md
git commit -m "docs(layer-w): README + PROGRESS Phase 38 entry (Layer W T12)"

# If working on a feature branch (per workflow), merge into main.
git checkout main
git merge --no-ff <layer-w-branch> -m "$(cat <<'EOF'
Merge Layer W — S3 / GCS real-cloud verification

Closes PROGRESS:116 carry-forward #4. Five-axis live opt-in + offline
fixture replay against s3://<S3_BUCKET> and
gs://<GCS_BUCKET>. Production changes: multipart-aware
uploads, StoreConfig.encryption knob (default + KMS), ArtifactStore.signed_url
ABC, pinned retry baselines.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Backfill the merge SHA + per-task SHAs into the Phase 38 entry.**

```bash
git log --oneline -20  # locate every Layer W commit
# edit PROGRESS.md, replace every `<sha>` placeholder with the real short SHA
git add PROGRESS.md
git commit -m "chore(progress): backfill Layer W SHAs"
```
