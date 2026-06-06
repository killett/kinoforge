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
    _persist("hot_path", {"entries": []}, target, cloud="s3", axis="hot_path")
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


def test_persist_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "fx.json"
    _persist("ax", {"entries": []}, target, cloud="gcs", axis="ax")
    assert target.exists()


def test_persist_applies_redaction(tmp_path: Path) -> None:
    target = tmp_path / "fx.json"
    payload = {"entries": [{"Bucket": "<S3_BUCKET>"}]}
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
    """Replay mode returns the stored http-form list when match_key hits."""
    import base64 as _b64

    op = "GetObject"
    params: dict[str, str] = {"Bucket": "b", "Key": "k"}
    # Build a recorder just to derive the match key deterministically.
    rec_build = S3Recorder(mode="record")
    key = rec_build._match_key(op, params)
    # Fixture stores body as base64 so the JSON is all strings/ints.
    body_b64 = _b64.b64encode(b"hello").decode("ascii")
    expected_response = [200, {"Content-Type": "application/octet-stream"}, body_b64]

    fx_path = tmp_path / "replay.json"
    fx_path.write_text(
        json.dumps(
            {
                "_meta": {},
                "entries": [
                    {
                        "match_key": key,
                        "parsed_response_http_form": expected_response,
                    }
                ],
            }
        )
    )
    rec = S3Recorder(mode="replay", fixture_path=fx_path)
    result = rec._before_send(operation_name=op, params=params, request=None)
    assert result == expected_response


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


# ---------------------------------------------------------------------------
# FixtureReplay stubs — raise NotImplementedError at __init__
# ---------------------------------------------------------------------------


def test_fixture_replay_s3_client_raises_not_implemented(tmp_path: Path) -> None:
    from tests.stores.recording import FixtureReplayS3Client

    fx = tmp_path / "stub.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    with pytest.raises(NotImplementedError, match="T11"):
        FixtureReplayS3Client(fx)


def test_fixture_replay_gcs_client_raises_not_implemented(tmp_path: Path) -> None:
    from tests.stores.recording import FixtureReplayGCSClient

    fx = tmp_path / "stub.json"
    fx.write_text(json.dumps({"_meta": {}, "entries": []}))
    with pytest.raises(NotImplementedError, match="T11"):
        FixtureReplayGCSClient(fx)
