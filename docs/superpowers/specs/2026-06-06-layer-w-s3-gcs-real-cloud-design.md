# Layer W — S3 / GCS real-cloud verification

Phase 38. Closes `PROGRESS.md` carry-forward #4 ("S3ArtifactStore + GCSArtifactStore never
hit real cloud — fake clients don't simulate multipart edge cases, transient retries,
SSE/KMS, signed URLs").

This is a five-axis verification layer modelled on **Layer N** (Phase 24, RunPod
cloud-fidelity hardening). Each axis has both a live opt-in test (gated by
`KINOFORGE_LIVE_TESTS=1`) and an offline replay test backed by a committed fixture.
The layer ships against AWS S3 and Google Cloud Storage in parallel — one set of
spec sections per axis, two SDK adapters per section.

## 1. Goal

Three load-bearing outcomes:

1. **Real-cloud verification.** `S3ArtifactStore` and `GCSArtifactStore` end-to-end
   pass against `s3://<S3_BUCKET>` (`us-east-1`) and
   `gs://<GCS_BUCKET>` (`us-central1`) — the buckets bootstrapped
   in commit `0d2cc18` / `171f927`. Layer N taught us the offline fakes drift from
   reality; this layer fixes that for the storage substrate.

2. **Production-feature parity.** Production code grows three missing features:
   multipart-aware uploads, client-controlled encryption (SSE-KMS / GCS CMEK), and
   signed URLs (GET + PUT). Every feature is exercised against real cloud before
   the layer merges.

3. **Replay lockdown.** Live captures persist as JSON fixtures under
   `tests/stores/fixtures/{s3,gcs}/`. Offline tests load them via fixture-replay
   clients, so post-merge SDK drift surfaces as a test failure instead of a silent
   production regression.

## 2. Non-goals

- **Azure Blob, Backblaze B2, Cloudflare R2 stores.** Each is a separate layer when
  the demand arrives.
- **DSSE-KMS** (S3 dual-layer KMS) and **CSEK** (GCS customer-supplied encryption
  keys). SSE-KMS + CMEK is the 95 % case. Add when a caller asks.
- **Bucket-level default encryption (`PutBucketEncryption`).** Operator concern,
  not a kinoforge code path.
- **Multipart resumability across process restarts.** SDK retries cover transient
  network failure; surviving a kill -9 is a separate problem.
- **Cross-region replication and versioning.** Buckets have neither configured.
- **Signed-URL custom headers** (Content-Type pinning, response-content-disposition
  overrides). No current caller needs them.
- **Streaming uploads from a file path.** Current ABC is bytes-in-memory; broadening
  it conflicts with the in-memory `_artifact_bytes` chain shipped in Layer M.

## 3. Five-axis matrix

| Axis             | S3 surface                                   | GCS surface                                   |
|------------------|----------------------------------------------|-----------------------------------------------|
| Hot path         | put_object / get_object / list_objects_v2 / delete_object | upload_from_file / download_as_bytes / list_blobs / blob.delete |
| Multipart        | `upload_fileobj` (auto-multipart > ~8 MiB)   | `upload_from_file` (resumable > ~5 MiB)       |
| Transient retry  | `botocore` retry config + local 503 proxy    | `google-cloud-storage` retry config + same proxy |
| Encryption       | SSE-S3 default + SSE-KMS customer-managed    | Google-managed default + CMEK customer-managed |
| Signed URL       | `generate_presigned_url("get_object"\|"put_object")` | `blob.generate_signed_url(version="v4", method=...)` |

Each axis becomes one parametrised live test per cloud and one fixture-replay
offline test per cloud.

## 4. Production code changes

### 4.0 Retry configuration (precondition for §4.1 and §5.3)

Retry behaviour is required to be deterministic for the §5.3 503-proxy axis. Both
stores rely on SDK defaults augmented at construction time, not a new kinoforge
knob:

- **S3.** `S3ArtifactStore.__init__` builds its boto3 client with
  `botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})`. The
  value is hard-coded in the store (no caller override). The §5.3 test asserts
  the actual `client.meta.config.retries` matches.
- **GCS.** `GCSArtifactStore` calls `blob.upload_from_file(...)` and
  `blob.download_as_bytes(...)` with an explicit
  `retry=google.api_core.retry.Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)`
  on every read and write path. The §5.3 test asserts the kwarg flows through.

No public API change; default behaviour is unchanged for callers. Future
work can promote either value to `StoreConfig` if a caller asks.

### 4.1 Multipart-aware upload

**S3.** Replace `client.put_object(Body=data, ...)` in
`src/kinoforge/stores/s3/__init__.py` with
`client.upload_fileobj(io.BytesIO(data), bucket, key, ExtraArgs=...)`. boto3's
`TransferConfig` defaults (`multipart_threshold=8 MiB`, `multipart_chunksize=8 MiB`)
handle the threshold; no kinoforge knob.

**GCS.** Replace `blob.upload_from_string(data, ...)` in
`src/kinoforge/stores/gcs/__init__.py` with
`blob.upload_from_file(io.BytesIO(data), ...)`. The GCS client uses resumable
uploads above ~5 MiB automatically.

**`put_json` routes through `put_bytes`** so a single change covers both
serialisation paths. No ABC change.

**Verification:** 16 MiB payload round-trip per cloud. S3 ETag carries a `-N`
suffix on multipart writes; assert it. GCS exposes
`blob.metadata` after upload; assert the upload session id is set on the resumable
path.

### 4.2 `StoreConfig.encryption`

New pydantic block in `src/kinoforge/core/config.py`:

```python
class StoreEncryptionConfig(BaseModel):
    mode: Literal["default", "kms"] = "default"
    kms_key_id: str | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _key_required_for_kms(self) -> Self:
        if self.mode == "kms" and not self.kms_key_id:
            raise ValueError("encryption.mode='kms' requires encryption.kms_key_id")
        return self


class StoreConfig(BaseModel):
    ...
    encryption: StoreEncryptionConfig = Field(default_factory=StoreEncryptionConfig)
    signed_url_default_ttl_s: int = 3600
```

**Field semantics.**

- `mode="default"` — provider-managed encryption (SSE-S3 / Google-managed). Today's
  behaviour. No `ExtraArgs` / `kms_key_name` is set; provider applies the bucket
  default.
- `mode="kms"` — customer-managed. `kms_key_id` holds the S3 KMS ARN or the GCS
  Cloud KMS resource name (`projects/.../locations/.../keyRings/.../cryptoKeys/...`).
  Same field for both clouds; store adapter knows which form to parse.

**Wiring.**

- S3: pass `ExtraArgs={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key_id}`
  to `upload_fileobj` when `mode="kms"`. Default mode passes empty `ExtraArgs`.
- GCS: set `blob.kms_key_name = kms_key_id` before `upload_from_file` when
  `mode="kms"`. Default mode leaves it `None`.

**Read-side.** Decryption is automatic — the provider returns plaintext on the
read path as long as the caller's identity has `kms:Decrypt` (S3) or
`cloudkms.cryptoKeyEncrypterDecrypter` (GCS). No store code change needed beyond
ensuring the `get_object` / `download_as_bytes` path is otherwise unmodified.

### 4.3 `signed_url` ABC

New abstract method on `ArtifactStore` (`src/kinoforge/stores/base.py`):

```python
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
        op: HTTP method the URL grants. Either ``"GET"`` (download) or ``"PUT"``
            (upload). PUT URLs grant unauthenticated upload to the resolved key.
        ttl_s: Validity window in seconds from issuance.

    Returns:
        Absolute HTTPS URL valid for ``ttl_s`` seconds.

    Raises:
        NotImplementedError: Backend does not support signed URLs (e.g.
            ``LocalArtifactStore``).
    """
```

**Implementations.**

- `LocalArtifactStore`: raises
  `NotImplementedError("LocalArtifactStore does not support signed URLs")`. Local
  files have no transport-layer auth.
- `S3ArtifactStore`:
  `client.generate_presigned_url(op_to_botocore[op], Params={"Bucket": ..., "Key": ...}, ExpiresIn=ttl_s)`.
  Mapping: `"GET" → "get_object"`, `"PUT" → "put_object"`.
- `GCSArtifactStore`:
  `bucket.blob(key).generate_signed_url(version="v4", expiration=timedelta(seconds=ttl_s), method=op)`.

Both SDK calls are pure (no network). Real-cloud verification = generate the URL,
hit it with `urllib.request`, assert 200 (GET) / 200 or 204 (PUT) + body bytes
match.

**`StoreConfig.signed_url_default_ttl_s = 3600`** is a caller convenience; callers
may override per call. Never read by the store itself — the ABC requires explicit
`ttl_s`.

## 5. Test architecture

### 5.1 Recording seam

`tests/stores/recording.py` carries two recorders, one per cloud.

**S3 recorder.** Wraps a `boto3.Session` via `botocore` event hooks:

- `before-send.s3.*` — capture `(operation_name, params, request body hash)`.
- `after-call.s3.*` — capture `parsed_response` (a `dict`, JSON-serialisable).

Two modes:

- `record` — forward to real AWS, capture, append to in-memory fixture buffer,
  flush to disk via `_persist(...)` at end of fixture scope.
- `replay` — match incoming `(operation_name, params_hash)` against fixture
  entries; return stored `parsed_response`; raise `FixtureMissError` on miss.

**GCS recorder.** Wraps `google.cloud.storage.Client._http` (an
`AuthorizedSession`) with a custom `requests.adapters.HTTPAdapter` subclass:

- Records `(method, url, body_hash)` → `(status, headers, body)`.
- Same record / replay mode split.

**Shared `_persist`.** Writes fixtures as JSON with `_meta = {"git_sha", "captured_at_local", "kinoforge_version", "cloud", "axis"}`. `captured_at_local`
follows the memory rule (`datetime.now()`, local TZ, never UTC).

**Redaction (record mode only).** Strip in order, never mutate the live request
in flight:

1. `Authorization`, `X-Amz-Security-Token`, `X-Goog-Authorization` headers.
2. Query params `X-Amz-Signature=*`, `X-Goog-Signature=*`,
   `X-Amz-Credential=*`, `x-goog-credential=*`.
3. Account id `<AWS_ACCOUNT>` → `<AWS_ACCOUNT>`.
4. Project id `<GCP_PROJECT>` → `<GCP_PROJECT>`.
5. KMS key ARNs and Cloud KMS resource names → `<S3_KMS_KEY>` / `<GCS_KMS_KEY>`.
6. Pre-signed URLs in fixture bodies — strip the signed segment, keep only the
   `host + path` shape.

Redaction is the LAST step before disk write; replay-time matching keys are
computed BEFORE redaction so identical replay still works.

### 5.2 Live opt-in suite

```
tests/stores/live/
  __init__.py
  conftest.py          # gate + creds + record-mode sessions + KMS-key resolution
  test_s3_live.py      # 5 parametrised axes
  test_gcs_live.py     # 5 parametrised axes
```

**Gating.** `conftest.py` enforces `KINOFORGE_LIVE_TESTS=1` AND the cloud-specific
preconditions:

- S3: `os.environ` carries usable AWS creds (boto3 default chain test ping
  against `sts:GetCallerIdentity`) AND `.aws/kms-test-key.arn` exists.
- GCS: `GOOGLE_APPLICATION_CREDENTIALS` resolves AND `.gcp/kms-test-key.name`
  exists.

Missing preconditions → `pytest.skip` with a message pointing to
`docs/CLOUD-CREDS.md`.

**Bucket layout.** Object keys namespaced under `live/<axis>/<test-name>/<run-id>/`.
The 1-day bucket lifecycle auto-cleans anything we leak. Tests still attempt
`delete_object` in `finally` for cleanliness.

**Axis coverage.** 5 axes × 2 clouds × ≥ 1 assertion-cluster per axis ≈ 10 live
test functions. Each captures one fixture file
(`tests/stores/fixtures/{s3,gcs}/<axis>.json`).

### 5.3 Local 503 retry proxy

`tests/stores/proxy.py` ships `Fail503Proxy(target_endpoint, fail_count=N)`:

- In-process `http.server.ThreadingHTTPServer` on `localhost:0`.
- Records request count. First `N` requests respond `503 Service Unavailable`
  with an empty body. Requests `N+1`+ are transparently forwarded to
  `target_endpoint` via `urllib.request` (full body relay, preserved headers
  except `Host`).
- Test reads `proxy.port` and configures the SDK client to use the proxy as the
  endpoint URL (S3: `endpoint_url=`; GCS:
  `client_options=ClientOptions(api_endpoint=...)`).

Retry-axis test pattern:

1. Spin proxy with `fail_count=2`.
2. Build S3 client with `botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})`.
3. Call `store.put_bytes(...)`.
4. Assert `proxy.request_count >= 3` (two 503s + one success).
5. Assert returned URI is well-formed; download via raw GET to confirm body match.

**GCS equivalent.** `google-cloud-storage` retries are per-call, not per-client.
The store's `put_bytes` passes
`retry=google.api_core.retry.Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)`
to `blob.upload_from_file(...)` / `blob.download_as_bytes(...)`. The assertion
locks the `Retry` instance configuration in the store source — a future change
that drops the `retry=` kwarg fails the offline assertion before the layer can
regress.

No fixture is captured for this axis — the proxy IS the verification surface.

### 5.4 Offline-test refactor

**Keep `FakeS3Client` and `FakeGCSClient`** (`tests/stores/conftest.py`) for the
behavioural unit tests already passing. They are the right surface for round-trip,
missing-key, list-empty checks.

**Augment with fixture-replay clients.** New `FixtureReplayS3Client(fixture_path)`
and `FixtureReplayGCSClient(fixture_path)` in `tests/stores/recording.py`. Tests
that care about wire shape (encryption response keys, multipart ETag suffix,
signed-URL output structure) load the replay client.

**Drop sites.** Any FakeS3Client / FakeGCSClient assertion that hardcodes
real-cloud wire detail (e.g. `assert resp["ServerSideEncryption"] == "aws:kms"`)
gets ported to a `FixtureReplay`-backed test in `test_s3.py` / `test_gcs.py`.
Behavioural assertions on the fakes stay.

### 5.5 Fixture layout

```
tests/stores/fixtures/
  s3/
    hot_path.json
    multipart.json
    encryption_default.json
    encryption_kms.json
    signed_url_get.json
    signed_url_put.json
  gcs/
    hot_path.json
    resumable.json
    encryption_default.json
    encryption_cmek.json
    signed_url_get.json
    signed_url_put.json
```

12 fixture files, ~10–50 KB each. Committed.

### 5.6 Test-count delta

- +10 live tests (gated, do not run on CI).
- +24 offline tests (~5 axes × 2 clouds × ~2 assertion clusters per axis).
- Pre-Layer-W baseline: 1423 passed + 8 skipped. Post-Layer-W target:
  ~1457 passed + 8 skipped + 10 KINOFORGE_LIVE_TESTS-skipped.

## 6. Bootstrap — `pixi run cloud:bootstrap-kms`

New pixi task wrapping `tools/bootstrap_kms.py`. Idempotent. Skips creation when
the key already exists by reading the persisted ARN / resource name and
attempting `kms:DescribeKey` / `cloudkms.cryptoKeys.get`.

### 6.1 AWS KMS

1. Create symmetric encryption key in `us-east-1` (matches bucket region) with
   description `kinoforge realcloud tests CMK`.
2. Create alias `alias/kinoforge-realcloud-tests`.
3. Attach key policy granting `kinoforge-ci` IAM user
   `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey`, `kms:DescribeKey`.
   Root account retains admin per AWS default policy.
4. Write key ARN to `.aws/kms-test-key.arn` (gitignored).
5. Cost: $1/mo while the key exists. Scheduled deletion is recoverable for 7–30
   days.

### 6.2 GCP Cloud KMS

1. Create keyring `kinoforge-realcloud-tests` in `us-central1` (matches bucket
   region).
2. Create key `bucket-cmek` with purpose `ENCRYPT_DECRYPT`, default protection
   level `SOFTWARE`.
3. Grant `kinoforge-runner@<GCP_PROJECT>.iam.gserviceaccount.com` the role
   `roles/cloudkms.cryptoKeyEncrypterDecrypter` on the key.
4. Grant the **GCS service agent**
   (`service-<PROJECT_NUMBER>@gs-project-accounts.iam.gserviceaccount.com`) the
   same role — required for GCS to perform server-side encryption with the key
   on the SA's behalf.
5. Write the key resource name
   (`projects/.../locations/.../keyRings/.../cryptoKeys/.../cryptoKeyVersions/...`,
   primary-version form) to `.gcp/kms-test-key.name` (gitignored).
6. Cost: ~$0.06/mo per active key version.

### 6.3 Rotation policy

Both keys are explicitly NOT auto-rotated. Rotation invalidates the captured
fixtures (KMS key id is part of every recorded encryption response). Documented
inline in `tools/bootstrap_kms.py` header and in `docs/CLOUD-CREDS.md` rotation
table.

## 7. `docs/CLOUD-CREDS.md` updates

1. **AWS provisioning history** — append:
   - 2026-06-06 (Layer W bootstrap): KMS key `alias/kinoforge-realcloud-tests`
     created in `us-east-1`. ARN persisted to `.aws/kms-test-key.arn`. Policy
     grants `kinoforge-ci` Encrypt/Decrypt/GenerateDataKey/DescribeKey.

2. **GCP provisioning history** — append:
   - 2026-06-06 (Layer W bootstrap): Cloud KMS keyring
     `kinoforge-realcloud-tests` + key `bucket-cmek` created in `us-central1`.
     `kinoforge-runner` SA + GCS service agent granted
     `roles/cloudkms.cryptoKeyEncrypterDecrypter`. Key name persisted to
     `.gcp/kms-test-key.name`.

3. **Bootstrap-status footnote** — add note "Layer W encryption + signed-URL
   axes require KMS keys. Run `pixi run cloud:bootstrap-kms` after the
   AWS/GCP rows show ✅."

4. **Rotation table** — add row "KMS keys (S3 + GCS): defer until next
   real-cloud layer; rotation invalidates Layer W fixtures."

## 8. Acceptance criteria

- AC1: All 5 axes pass on real S3 (`KINOFORGE_LIVE_TESTS=1` + AWS creds + KMS
  key).
- AC2: All 5 axes pass on real GCS (`KINOFORGE_LIVE_TESTS=1` +
  `GOOGLE_APPLICATION_CREDENTIALS` + KMS key).
- AC3: Fixtures captured under `tests/stores/fixtures/{s3,gcs}/` and committed.
- AC4: `pixi run test` (no live env) runs against fixtures; no real network
  traffic. Caught by the existing test_core_invariant.py + a new explicit
  network-isolation assertion in the offline suite.
- AC5: `S3ArtifactStore.put_bytes` and `GCSArtifactStore.put_bytes` use
  multipart-aware uploads. 16 MiB payload round-trip succeeds. S3 ETag carries
  `-N` multipart suffix; GCS upload uses the resumable session id path.
- AC6: `StoreConfig.encryption.mode in {"default", "kms"}` round-trips through
  YAML. `mode="kms"` without `kms_key_id` raises `ValidationError` at YAML load.
- AC7: `store.signed_url(...)` returns a URL that real cloud accepts within
  `ttl_s` for the matching op. GET fetches the bytes; PUT uploads bytes that a
  subsequent `get_bytes` reads back identically.
- AC8: Local 503 proxy forces ≥ 2 retries on both clouds. SDK retry config is
  the §4.0 baseline: `botocore.config.Config(retries={"max_attempts": 3, "mode": "standard"})`
  for S3, `google.api_core.retry.Retry(initial=0.1, maximum=2.0, multiplier=2.0, deadline=30.0)`
  for GCS. Assertion locks both values in the store source.
- AC9: `docs/CLOUD-CREDS.md` carries KMS provisioning history rows, bootstrap
  pointer, and rotation note.
- AC10: `tests/test_core_invariant.py` passes (no new core-import-ban
  violations).
- AC11: `pre-commit run --all-files` green.
- AC12: Test count delta matches §5.6 within ±5.

## 9. Risks and mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| `botocore` / `google-cloud-storage` minor-version SDK drift breaks fixture replay | medium | Pin SDK minor versions in `pixi.toml`. Add version-lockdown assertion in fixture-replay clients. |
| KMS-key revocation between fixture capture and offline replay | low | Replay reads from fixture text only; live tests skip cleanly with clear error. |
| Botocore event hooks (`before-send`, `after-call`) shift API shape | low | Pin botocore minor. Single recorder module — one place to fix. |
| Live-smoke cost creep | low | Estimated ≤ $0.05 per full run (KMS + ~16 MiB transfer). Budget exposure on 100 reruns < $5. Documented. |
| New operator forks the repo, hits bucket-name collision | low | Bucket names are account-pinned; collisions impossible across operators. Bootstrap doc covers new-operator path. |
| GCS service agent IAM grant missed → upload fails with cryptic 403 | medium | Bootstrap script grants both bindings (SA + GCS service agent). Live test surfaces the failure with a redirect to the script. |
| Signed-URL TTL clock skew between client and cloud | low | TTLs are ≥ 60 s in tests; cloud clocks are NTP-synced. |

## 10. Out of scope (carried forward)

- Streaming uploads from file paths (ABC stays bytes-in-memory).
- Multipart upload resumability across process restart.
- DSSE-KMS (S3) and CSEK (GCS).
- Azure / B2 / R2 stores.
- Cross-region replication, versioning, lifecycle config beyond the existing
  1-day cleanup.
- Bucket-level default encryption (`PutBucketEncryption`).
- Signed-URL custom response headers.

## 11. Implementation order (one task per item)

Numbered for the implementation plan to consume directly.

1. `StoreEncryptionConfig` + `StoreConfig.encryption` + `signed_url_default_ttl_s`
   pydantic fields. Round-trip tests against YAML.
2. ABC change: `ArtifactStore.signed_url(...)` abstract method + `LocalArtifactStore`
   `NotImplementedError` stub + ABC unit tests.
3. `S3ArtifactStore`: multipart switch + encryption wiring + `signed_url` impl.
   Unit tests against `FakeS3Client` extended for encryption ExtraArgs +
   `generate_presigned_url`.
4. `GCSArtifactStore`: resumable switch + CMEK wiring + `signed_url` impl. Unit
   tests against `FakeGCSClient` extended for `kms_key_name` + `generate_signed_url`.
5. `tools/bootstrap_kms.py` + `pixi run cloud:bootstrap-kms` task. Idempotent.
   `docs/CLOUD-CREDS.md` updates.
6. `tests/stores/recording.py`: S3 + GCS recorders + redaction + `_persist`.
   Recorder unit tests against a fake transport.
7. `tests/stores/proxy.py`: `Fail503Proxy`. Unit test for the proxy alone (in-process
   loopback target).
8. `tests/stores/live/conftest.py` + gate.
9. Live tests — S3: 5 axes, fixtures captured. Commit fixture files.
10. Live tests — GCS: 5 axes, fixtures captured. Commit fixture files.
11. `FixtureReplayS3Client` + `FixtureReplayGCSClient` + offline tests against
    every fixture file.
12. README "Cloud stores" section update + PROGRESS Phase 38 entry + merge to
    `main` via `--no-ff`.
