"""Recorder + redaction unit tests (no real network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from tests.stores.recording import (
    FixtureMissError,
    GCSRecorder,
    S3Recorder,
    _GCSRecordingAdapter,
    _persist,
    _redact,
    _ReplayResponse,
)

# ---------------------------------------------------------------------------
# _redact tests
# ---------------------------------------------------------------------------


def test_redact_strips_account_id() -> None:
    payload = {"Bucket": "<S3_BUCKET>", "Body": "ok"}
    out = _redact(payload)
    assert "<AWS_ACCOUNT>" not in json.dumps(out)
    assert "<AWS_ACCOUNT>" in out["Bucket"]


def test_redact_strips_project_id() -> None:
    payload = {"resource": "projects/<GCP_PROJECT>/buckets/foo"}
    out = _redact(payload)
    assert "<GCP_PROJECT>" not in json.dumps(out)
    assert "<GCP_PROJECT>" in out["resource"]


def test_redact_strips_prod_project_id_shape() -> None:
    # Lockdown for the generalized regex (2026-06-09 GCP account swap):
    # `kinoforge-(dev|prod)-<8hex>`. Without the alternation the prod-suffix
    # variant would slip through fixture redaction.
    payload = {"resource": "projects/<GCP_PROJECT>/buckets/foo"}
    out = _redact(payload)
    assert "<GCP_PROJECT>" not in json.dumps(out)
    assert "<GCP_PROJECT>" in out["resource"]


def test_redact_strips_signature_query_param() -> None:
    payload = {
        "url": "https://s3.amazonaws.com/foo?X-Amz-Signature=ababab1234&Expires=42"
    }
    out = _redact(payload)
    assert "<REDACTED>" in out["url"]
    assert "Expires=42" in out["url"]


def test_redact_drops_authorization_header() -> None:
    payload = {"Authorization": "Bearer secret", "Other": "ok"}
    out = _redact(payload)
    assert "Authorization" not in out
    assert out["Other"] == "ok"


def test_redact_drops_x_amz_security_token() -> None:
    payload = {"X-Amz-Security-Token": "tok123", "Keep": "yes"}
    out = _redact(payload)
    assert "X-Amz-Security-Token" not in out
    assert out["Keep"] == "yes"


def test_redact_drops_x_goog_authorization() -> None:
    payload = {"X-Goog-Authorization": "GoogleAuth abc", "Keep": "yes"}
    out = _redact(payload)
    assert "X-Goog-Authorization" not in out
    assert out["Keep"] == "yes"


def test_redact_strips_goog_signature_query_param() -> None:
    payload = {
        "url": "https://storage.googleapis.com/b/o?X-Goog-Signature=xyz123&X-Goog-Expires=3600"
    }
    out = _redact(payload)
    assert "<REDACTED>" in out["url"]
    assert "X-Goog-Expires=3600" in out["url"]


def test_redact_strips_goog_credential_query_param() -> None:
    payload = {
        "url": "https://storage.googleapis.com/b/o?x-goog-credential=sa%40proj.iam.gserviceaccount.com%2F20260606%2F"
    }
    out = _redact(payload)
    assert "<REDACTED>" in out["url"]


def test_redact_substitutes_kms_key() -> None:
    payload = {"SSEKMSKeyId": "arn:aws:kms:us-east-1:1:key/abcde"}
    out = _redact(
        payload, extra_subs={"arn:aws:kms:us-east-1:1:key/abcde": "<S3_KMS_KEY>"}
    )
    assert out["SSEKMSKeyId"] == "<S3_KMS_KEY>"


def test_redact_extra_subs_applied_before_account_rule() -> None:
    """Regression: KMS ARN must be substituted before account-id regex strips the account id."""
    payload = {"SSEKMSKeyId": "arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/abcdef-1234"}
    out = _redact(
        payload,
        extra_subs={
            "arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/abcdef-1234": "<S3_KMS_KEY>"
        },
    )
    assert out["SSEKMSKeyId"] == "<S3_KMS_KEY>"
    # The original UUID + account id must both be gone.
    blob = json.dumps(out)
    assert "abcdef-1234" not in blob
    assert "<AWS_ACCOUNT>" not in blob


def test_redact_roundtrip_no_secrets_remain() -> None:
    """Full round-trip: a payload with every redactable token leaves no secret."""
    payload = {
        "Authorization": "AWS4-HMAC-SHA256 Credential=ABC",
        "X-Amz-Security-Token": "SESSION_TOKEN",
        "X-Goog-Authorization": "Bearer GOOGLE_TOKEN",
        "url": (
            "https://s3.amazonaws.com/<S3_BUCKET>/obj"
            "?X-Amz-Signature=deadbeef1234"
            "&X-Amz-Credential=AKIA/20260606/us-east-1"
            "&X-Goog-Signature=cafebabe5678"
            "&x-goog-credential=sa%40<GCP_PROJECT>.iam.gserviceaccount.com"
        ),
        "kms_key": "arn:aws:kms:us-east-1:1:key/REAL_KEY",
    }
    out = _redact(
        payload,
        extra_subs={"arn:aws:kms:us-east-1:1:key/REAL_KEY": "<S3_KMS_KEY>"},
    )
    serialised = json.dumps(out)
    # All secrets gone
    assert "<AWS_ACCOUNT>" not in serialised
    assert "<GCP_PROJECT>" not in serialised
    assert "deadbeef1234" not in serialised
    assert "cafebabe5678" not in serialised
    assert "REAL_KEY" not in serialised
    assert "SESSION_TOKEN" not in serialised
    assert "GOOGLE_TOKEN" not in serialised
    assert "Authorization" not in serialised


# ---------------------------------------------------------------------------
# _persist tests
# ---------------------------------------------------------------------------


def test_persist_writes_meta_block(tmp_path: Path) -> None:
    target = tmp_path / "fx.json"
    _persist("hot_path", [], target, cloud="s3", axis="hot_path")
    body = json.loads(target.read_text())
    meta = body["_meta"]
    assert meta["cloud"] == "s3"
    assert meta["axis"] == "hot_path"
    assert meta["label"] == "hot_path"
    assert meta["git_sha"]
    # captured_at_local must contain "T" (ISO date-time separator) — local TZ
    assert "T" in meta["captured_at_local"]
    # Must NOT end with +00:00 (UTC offset) — should be naive local
    assert not meta["captured_at_local"].endswith("+00:00")
    # entries must be a list (never double-wrapped)
    assert isinstance(body["entries"], list)


def test_persist_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "fx.json"
    _persist("ax", [], target, cloud="gcs", axis="ax")
    assert target.exists()


def test_persist_applies_redaction(tmp_path: Path) -> None:
    target = tmp_path / "fx.json"
    payload = [{"Bucket": "<S3_BUCKET>"}]
    _persist("ax", payload, target, cloud="s3", axis="ax")
    raw = target.read_text()
    assert "<AWS_ACCOUNT>" not in raw
    assert "<AWS_ACCOUNT>" in raw


# ---------------------------------------------------------------------------
# FixtureMissError tests
# ---------------------------------------------------------------------------


def test_fixture_miss_error_is_lookup_error() -> None:
    err = FixtureMissError("no match for GetObject")
    assert isinstance(err, LookupError)
    assert "GetObject" in str(err)


# ---------------------------------------------------------------------------
# S3Recorder tests
# ---------------------------------------------------------------------------


def test_s3_recorder_replay_raises_on_miss(tmp_path: Path) -> None:
    fx_path = tmp_path / "miss.json"
    # Fixture JSON must have "entries" key (list) under the root.
    fx_path.write_text(json.dumps({"_meta": {}, "entries": []}))
    rec = S3Recorder(mode="replay", fixture_path=fx_path)
    with pytest.raises(FixtureMissError):
        rec._before_send(
            operation_name="GetObject",
            params={"Bucket": "b", "Key": "k"},
            request=None,
        )


def test_s3_recorder_replay_returns_fixture(tmp_path: Path) -> None:
    """Replay mode returns an AWSResponse-shaped object when match_key hits."""
    import base64 as _b64

    op = "GetObject"
    params: dict[str, str] = {"Bucket": "b", "Key": "k"}
    # Build a recorder just to derive the match key deterministically.
    rec_build = S3Recorder(mode="record")
    key = rec_build._match_key(op, params)
    # Fixture stores body as base64 so the JSON is all strings/ints.
    body_b64 = _b64.b64encode(b"hello").decode("ascii")
    stored_response = [200, {"Content-Type": "application/octet-stream"}, body_b64]

    fx_path = tmp_path / "replay.json"
    fx_path.write_text(
        json.dumps(
            {
                "_meta": {},
                "entries": [
                    {
                        "match_key": key,
                        "parsed_response_http_form": stored_response,
                    }
                ],
            }
        )
    )
    rec = S3Recorder(mode="replay", fixture_path=fx_path)
    result = rec._before_send(operation_name=op, params=params, request=None)
    # Must return an AWSResponse-shaped object, not the raw list.
    assert isinstance(result, _ReplayResponse)
    assert result.status_code == 200
    assert result.headers == {"Content-Type": "application/octet-stream"}
    assert result.content == b"hello"


def test_s3_recorder_record_mode_captures(tmp_path: Path) -> None:
    """Record mode: _after_call appends an entry; flush writes redacted JSON."""
    rec = S3Recorder(mode="record")

    # Simulate the after-call hook being invoked.
    http_resp = SimpleNamespace(
        status_code=200,
        headers={"Authorization": "AWS4 secret", "Content-Type": "application/xml"},
        content=b"<ListBucketResult/>",
    )
    context: dict[str, object] = {
        "_kinoforge_op": "ListObjectsV2",
        "_kinoforge_params": {"Bucket": "mybucket"},
    }
    rec._after_call(
        http_response=http_resp,
        parsed={"Contents": []},
        model=None,
        context=context,
    )

    assert len(rec.captured) == 1
    entry = rec.captured[0]
    assert entry["operation"] == "ListObjectsV2"
    assert "match_key" in entry

    # flush writes redacted JSON
    target = tmp_path / "out.json"
    rec.flush(target, axis="hot_path")
    written = json.loads(target.read_text())
    assert written["_meta"]["cloud"] == "s3"
    assert written["_meta"]["axis"] == "hot_path"
    # entries must be a flat list — never double-wrapped
    assert isinstance(written["entries"], list)
    assert len(written["entries"]) > 0
    assert "match_key" in written["entries"][0]


def test_s3_recorder_before_send_stashes_context() -> None:
    """Record mode: _before_send stashes op+params on request.context."""
    rec = S3Recorder(mode="record")
    ctx: dict[str, object] = {}
    request = SimpleNamespace(context=ctx)
    result = rec._before_send(
        operation_name="PutObject",
        params={"Bucket": "b", "Key": "k"},
        request=request,
    )
    assert result is None  # record mode: does not short-circuit
    assert ctx["_kinoforge_op"] == "PutObject"
    assert ctx["_kinoforge_params"] == {"Bucket": "b", "Key": "k"}


# ---------------------------------------------------------------------------
# GCSRecorder tests
# ---------------------------------------------------------------------------


def test_gcs_recorder_replay_raises_on_miss(tmp_path: Path) -> None:
    fx_path = tmp_path / "miss.json"
    fx_path.write_text(json.dumps({"_meta": {}, "entries": []}))
    rec = GCSRecorder(mode="replay", fixture_path=fx_path)

    inner = requests.adapters.HTTPAdapter()
    adapter = _GCSRecordingAdapter(rec, inner)

    req = SimpleNamespace(
        method="GET",
        url="https://storage.googleapis.com/foo/bar",
        body=None,
        headers={},
    )
    with pytest.raises(FixtureMissError):
        adapter.send(req)


def test_gcs_recorder_replay_returns_fixture(tmp_path: Path) -> None:
    """Replay mode returns a Response built from the fixture entry."""
    method = "GET"
    url = "https://storage.googleapis.com/bucket/obj"
    body_bytes = None

    rec_build = GCSRecorder(mode="record")
    key = rec_build._match_key(method, url, body_bytes)

    fx_path = tmp_path / "replay.json"
    fx_path.write_text(
        json.dumps(
            {
                "_meta": {},
                "entries": [
                    {
                        "match_key": key,
                        "status": 200,
                        "headers": {"Content-Type": "application/octet-stream"},
                        "body_b64": "aGVsbG8=",  # b"hello"
                    }
                ],
            }
        )
    )
    rec = GCSRecorder(mode="replay", fixture_path=fx_path)
    inner = requests.adapters.HTTPAdapter()
    adapter = _GCSRecordingAdapter(rec, inner)

    req = SimpleNamespace(method=method, url=url, body=None, headers={})
    resp = adapter.send(req)
    assert resp.status_code == 200
    assert resp.content == b"hello"


def test_gcs_recorder_record_mode_captures(tmp_path: Path) -> None:
    """Record mode: adapter.send appends to captured; flush writes JSON."""

    class _FakeInnerAdapter:
        def send(self, request: Any, **kwargs: Any) -> Any:
            import requests as _requests

            r = _requests.Response()
            r.status_code = 200
            r.headers["Content-Type"] = "application/json"
            r._content = b'{"kind": "storage#object"}'
            return r

        def close(self) -> None:
            pass

    rec = GCSRecorder(mode="record")
    adapter = _GCSRecordingAdapter(rec, _FakeInnerAdapter())

    req = SimpleNamespace(
        method="PUT",
        url="https://storage.googleapis.com/bucket/obj",
        body=b"payload",
        headers={},
    )
    resp = adapter.send(req)
    assert resp.status_code == 200
    assert len(rec.captured) == 1

    target = tmp_path / "gcs_out.json"
    rec.flush(target, axis="hot_path")
    written = json.loads(target.read_text())
    assert written["_meta"]["cloud"] == "gcs"
    assert written["_meta"]["axis"] == "hot_path"
    # entries must be a flat list — never double-wrapped
    assert isinstance(written["entries"], list)
    assert len(written["entries"]) > 0
    assert "match_key" in written["entries"][0]


# ---------------------------------------------------------------------------
# _ReplayResponse unit tests (Finding 2 — AWSResponse-shaped object)
# ---------------------------------------------------------------------------


def test_replay_response_attributes() -> None:
    """_ReplayResponse exposes .status_code, .headers, .content like AWSResponse."""
    resp = _ReplayResponse(200, {"Content-Type": "text/plain"}, b"body data")
    assert resp.status_code == 200
    assert resp.headers == {"Content-Type": "text/plain"}
    assert resp.content == b"body data"


def test_s3_recorder_replay_decodes_base64_body(tmp_path: Path) -> None:
    """Replay returns _ReplayResponse with decoded body bytes, not raw base64."""
    import base64 as _b64

    raw_body = b"\x00\x01\x02\x03\xff"
    body_b64 = _b64.b64encode(raw_body).decode("ascii")
    op = "GetObject"
    params: dict[str, str] = {"Bucket": "b", "Key": "binary"}
    rec_build = S3Recorder(mode="record")
    key = rec_build._match_key(op, params)

    fx_path = tmp_path / "binary.json"
    fx_path.write_text(
        json.dumps(
            {
                "_meta": {},
                "entries": [
                    {
                        "match_key": key,
                        "parsed_response_http_form": [206, {}, body_b64],
                    }
                ],
            }
        )
    )
    rec = S3Recorder(mode="replay", fixture_path=fx_path)
    result = rec._before_send(operation_name=op, params=params, request=None)
    assert isinstance(result, _ReplayResponse)
    assert result.status_code == 206
    assert result.content == raw_body


# ---------------------------------------------------------------------------
# GCS file-like body tests (Finding 3)
# ---------------------------------------------------------------------------


def test_gcs_recording_adapter_file_like_body_match_key(tmp_path: Path) -> None:
    """File-like body yields same match key as equivalent bytes body."""
    import io

    rec = GCSRecorder(mode="record")
    body_content = b"file content for upload"
    method = "PUT"
    url = "https://storage.googleapis.com/bucket/obj"

    # Match key from bytes body
    key_bytes = rec._match_key(method, url, body_content)

    # Build a fixture replay (we just need _GCSRecordingAdapter.send to compute key)
    # Simulate what send() does with a file-like body: it reads it.
    class _FakeInnerAdapter:
        def send(self, request: Any, **kwargs: Any) -> Any:
            import requests as _requests

            r = _requests.Response()
            r.status_code = 200
            r._content = b"ok"
            return r

        def close(self) -> None:
            pass

    rec2 = GCSRecorder(mode="record")
    adapter = _GCSRecordingAdapter(rec2, _FakeInnerAdapter())

    stream = io.BytesIO(body_content)
    req = SimpleNamespace(
        method=method,
        url=url,
        body=stream,
        headers={},
    )
    adapter.send(req)
    assert len(rec2.captured) == 1
    assert rec2.captured[0]["match_key"] == key_bytes
    # Stream should be reset to position 0 after send
    assert stream.tell() == 0


def test_gcs_recording_adapter_file_like_replay_hits(tmp_path: Path) -> None:
    """Replay mode with a file-like body request correctly hits the fixture."""
    import io

    body_content = b"upload data"
    method = "PUT"
    url = "https://storage.googleapis.com/bucket/upload"

    rec_build = GCSRecorder(mode="record")
    key = rec_build._match_key(method, url, body_content)

    import base64 as _b64

    fx_path = tmp_path / "filelike.json"
    fx_path.write_text(
        json.dumps(
            {
                "_meta": {},
                "entries": [
                    {
                        "match_key": key,
                        "status": 200,
                        "headers": {"ETag": "abc123"},
                        "body_b64": _b64.b64encode(b"").decode("ascii"),
                    }
                ],
            }
        )
    )
    rec = GCSRecorder(mode="replay", fixture_path=fx_path)
    inner = requests.adapters.HTTPAdapter()
    adapter = _GCSRecordingAdapter(rec, inner)

    req = SimpleNamespace(
        method=method,
        url=url,
        body=io.BytesIO(body_content),
        headers={},
    )
    resp = adapter.send(req)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# FixtureReplay clients — Layer W T11 working implementations
# ---------------------------------------------------------------------------


def test_fixture_replay_s3_client_constructs_from_empty_fixture(tmp_path: Path) -> None:
    """FixtureReplayS3Client must construct without error from an empty fixture.

    Bug this catches: constructor raises on missing or empty entries list.
    """
    from tests.stores.recording import FixtureReplayS3Client

    fx = tmp_path / "empty.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    client = FixtureReplayS3Client(fx)
    assert client.meta.config.retries["max_attempts"] == 3
    assert client.meta.config.retries["mode"] == "standard"


def test_fixture_replay_s3_client_generate_presigned_url(tmp_path: Path) -> None:
    """FixtureReplayS3Client.generate_presigned_url returns an HTTPS URL with bucket.

    Bug this catches: URL is non-HTTPS or omits the bucket name.
    """
    from tests.stores.recording import FixtureReplayS3Client

    fx = tmp_path / "empty.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    client = FixtureReplayS3Client(fx)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": "my-bucket", "Key": "my/key"},
        ExpiresIn=600,
    )
    assert url.startswith("https://")
    assert "my-bucket" in url


def test_fixture_replay_gcs_client_constructs_from_empty_fixture(
    tmp_path: Path,
) -> None:
    """FixtureReplayGCSClient must construct without error from an empty fixture.

    Bug this catches: constructor raises on missing or empty entries list.
    """
    from tests.stores.recording import FixtureReplayGCSClient

    fx = tmp_path / "empty.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    client = FixtureReplayGCSClient(fx)
    bucket = client.bucket("any-bucket")
    assert bucket.list_blobs() == []


# ---------------------------------------------------------------------------
# _s3_op_fingerprint — Bug 1 regression: UploadPart vs PutObject collision
# ---------------------------------------------------------------------------


def test_s3_op_fingerprint_putobject_with_checksum_not_uploadpart() -> None:
    """PutObject response with ETag+ChecksumCRC32 must NOT be misclassified as UploadPart.

    Regression: when botocore checksum validation is enabled, PutObject responses
    carry ETag + ChecksumCRC32 + ResponseMetadata — the same shape as UploadPart.
    The fix pivots on params['PartNumber']: absent on PutObject, present on UploadPart.
    """
    from tests.stores.recording import _s3_op_fingerprint

    entry_put = {
        "params": {"Bucket": "b", "Key": "k"},  # no PartNumber
        "parsed_response": {
            "ETag": '"abc123"',
            "ChecksumCRC32": "AAAA",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        },
    }
    assert _s3_op_fingerprint(entry_put) == "PutObject"


def test_s3_op_fingerprint_uploadpart_with_checksum_classified_correctly() -> None:
    """UploadPart response with ETag+ChecksumCRC32 IS correctly classified as UploadPart.

    Regression: same parsed_response shape as PutObject with checksum enabled;
    the fix uses params['PartNumber'] (present for UploadPart) to disambiguate.
    """
    from tests.stores.recording import _s3_op_fingerprint

    entry_upload_part = {
        "params": {"Bucket": "b", "Key": "k", "UploadId": "uid", "PartNumber": 1},
        "parsed_response": {
            "ETag": '"abc123"',
            "ChecksumCRC32": "AAAA",
            "ResponseMetadata": {"HTTPStatusCode": 200},
        },
    }
    assert _s3_op_fingerprint(entry_upload_part) == "UploadPart"


def test_s3_op_fingerprint_uploadpart_no_checksum_still_uploadpart() -> None:
    """UploadPart without ChecksumCRC32 is still classified via PartNumber in params."""
    from tests.stores.recording import _s3_op_fingerprint

    entry = {
        "params": {"Bucket": "b", "Key": "k", "UploadId": "uid", "PartNumber": 3},
        "parsed_response": {
            "ETag": '"deadbeef"',
            "ResponseMetadata": {"HTTPStatusCode": 200},
        },
    }
    assert _s3_op_fingerprint(entry) == "UploadPart"


def test_s3_op_fingerprint_putobject_no_checksum_still_putobject() -> None:
    """PutObject without ChecksumCRC32 is still classified as PutObject."""
    from tests.stores.recording import _s3_op_fingerprint

    entry = {
        "params": {"Bucket": "b", "Key": "k"},
        "parsed_response": {
            "ETag": '"deadbeef"',
            "ResponseMetadata": {"HTTPStatusCode": 200},
        },
    }
    assert _s3_op_fingerprint(entry) == "PutObject"


# ---------------------------------------------------------------------------
# FixtureReplayGCSClient — Bug 2 regression: PUT-before-GET ordering
# ---------------------------------------------------------------------------


def test_fixture_replay_gcs_client_put_before_get_returns_real_bytes(
    tmp_path: Path,
) -> None:
    """download_as_bytes() must return real bytes even when PUT precedes GET in fixture.

    Regression: the old single-pass _parse_entries built the blob with _content=b""
    because the download_cache was empty when the PUT entry was processed; the
    subsequent GET entry stashed bytes that were never applied back to the blob.

    Fix: two-pass parse — pass 1 populates download_cache from all GET entries,
    pass 2 constructs blobs from PUT entries with the cache already populated.
    """
    import base64
    import json

    from tests.stores.recording import FixtureReplayGCSClient

    blob_name = "artifacts/model.bin"
    bucket_name = "kinoforge-dev-bucket"
    real_bytes = b"real content bytes for model"

    # Build a minimal fixture: PUT entry first, then GET entry (normal write→read order).
    put_meta = {
        "name": blob_name,
        "bucket": bucket_name,
        "size": str(len(real_bytes)),
        "kmsKeyName": "",
    }
    put_entry = {
        "method": "PUT",
        "url": f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o",
        "status": 200,
        "body_b64": base64.b64encode(json.dumps(put_meta).encode()).decode("ascii"),
        "headers": {},
        "match_key": "PUT:https://...:0000000000000000",
    }

    get_url = f"https://storage.googleapis.com/download/storage/v1/b/{bucket_name}/o/{blob_name}"
    get_entry = {
        "method": "GET",
        "url": get_url,
        "status": 200,
        "body_b64": base64.b64encode(real_bytes).decode("ascii"),
        "headers": {},
        "match_key": "GET:https://...:aaaaaaaaaaaaaaaa",
    }

    fixture = {"_meta": {}, "entries": [put_entry, get_entry]}
    fx_path = tmp_path / "put_before_get.json"
    fx_path.write_text(json.dumps(fixture))

    client = FixtureReplayGCSClient(fx_path)
    downloaded = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    assert downloaded == real_bytes, (
        f"Expected {real_bytes!r}, got {downloaded!r}. "
        "Two-pass _parse_entries fix may not be applied correctly."
    )


def test_fixture_replay_gcs_client_get_before_put_also_works(
    tmp_path: Path,
) -> None:
    """download_as_bytes() also works in the GET-before-PUT ordering (was always fine)."""
    import base64
    import json

    from tests.stores.recording import FixtureReplayGCSClient

    blob_name = "checkpoints/step100.pt"
    bucket_name = "kinoforge-dev-bucket"
    real_bytes = b"checkpoint bytes"

    put_meta = {
        "name": blob_name,
        "bucket": bucket_name,
        "size": str(len(real_bytes)),
        "kmsKeyName": "",
    }
    get_url = f"https://storage.googleapis.com/download/storage/v1/b/{bucket_name}/o/{blob_name}"

    get_entry = {
        "method": "GET",
        "url": get_url,
        "status": 200,
        "body_b64": base64.b64encode(real_bytes).decode("ascii"),
        "headers": {},
        "match_key": "GET:https://...:bbbbbbbbbbbbbbbb",
    }
    put_entry = {
        "method": "PUT",
        "url": f"https://storage.googleapis.com/upload/storage/v1/b/{bucket_name}/o",
        "status": 200,
        "body_b64": base64.b64encode(json.dumps(put_meta).encode()).decode("ascii"),
        "headers": {},
        "match_key": "PUT:https://...:1111111111111111",
    }

    fixture = {"_meta": {}, "entries": [get_entry, put_entry]}
    fx_path = tmp_path / "get_before_put.json"
    fx_path.write_text(json.dumps(fixture))

    client = FixtureReplayGCSClient(fx_path)
    downloaded = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    assert downloaded == real_bytes


def test_fixture_replay_gcs_blob_signed_url_shape(tmp_path: Path) -> None:
    """FixtureReplayGCSBlob.generate_signed_url returns HTTPS URL with method param.

    Bug this catches: synthesised signed URL does not start with https:// or
    omits the X-Goog-Signature marker.
    """
    from datetime import timedelta

    from tests.stores.recording import FixtureReplayGCSClient

    fx = tmp_path / "empty.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    client = FixtureReplayGCSClient(fx)
    blob = client.bucket("bkt").blob("some/key")
    url = blob.generate_signed_url(
        version="v4", expiration=timedelta(seconds=300), method="GET"
    )
    assert url.startswith("https://")
    assert "X-Goog-Signature" in url
