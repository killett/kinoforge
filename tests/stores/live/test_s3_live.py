"""S3 real-cloud smoke + fixture capture (5 axes + retry + cleanup)."""

from __future__ import annotations

import urllib.request
import uuid

import boto3
import pytest
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
    store = S3ArtifactStore(
        bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
    )
    run = _run_id()
    try:
        store.put_bytes(run, "hello.bin", b"hello world")
        assert store.get_bytes(store.uri_for(run, "hello.bin")) == b"hello world"
        assert "hello.bin" in store.list(run)
    finally:
        try:
            store.delete(store.uri_for(run, "hello.bin"))
        except Exception:
            pass


def test_s3_multipart(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(
        bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
    )
    run = _run_id()
    big = b"x" * (16 * 1024 * 1024)
    try:
        store.put_bytes(run, "big.bin", big)
        head = client.head_object(Bucket=bucket, Key=f"{run}/big.bin")
        etag = head["ETag"].strip('"')
        assert "-" in etag, f"expected multipart ETag with -N suffix, got {etag}"
    finally:
        try:
            store.delete(store.uri_for(run, "big.bin"))
        except Exception:
            pass


def test_s3_encryption_default(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(
        bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
    )
    run = _run_id()
    try:
        store.put_bytes(run, "default.bin", b"plaintext")
        head = client.head_object(Bucket=bucket, Key=f"{run}/default.bin")
        assert head.get("ServerSideEncryption") == "AES256"
    finally:
        try:
            store.delete(store.uri_for(run, "default.bin"))
        except Exception:
            pass


def test_s3_encryption_kms(s3_record_session, s3_live_bucket_and_kms):
    bucket, kms = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    cfg = StoreConfig(
        kind="s3",
        bucket=bucket,
        encryption=StoreEncryptionConfig(mode="kms", kms_key_id=kms),
    )
    store = S3ArtifactStore(bucket=bucket, client=client, cfg=cfg)
    run = _run_id()
    try:
        store.put_bytes(run, "kms.bin", b"sensitive")
        head = client.head_object(Bucket=bucket, Key=f"{run}/kms.bin")
        assert head.get("ServerSideEncryption") == "aws:kms"
        assert head.get("SSEKMSKeyId", "").endswith(kms.split("/")[-1])
    finally:
        try:
            store.delete(store.uri_for(run, "kms.bin"))
        except Exception:
            pass


def test_s3_signed_url_get(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(
        bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
    )
    run = _run_id()
    try:
        store.put_bytes(run, "signed.bin", b"signed-get-payload")
        url = store.signed_url(run, "signed.bin", op="GET", ttl_s=300)
        with urllib.request.urlopen(url) as resp:
            assert resp.read() == b"signed-get-payload"
    finally:
        try:
            store.delete(store.uri_for(run, "signed.bin"))
        except Exception:
            pass


def test_s3_signed_url_put(s3_record_session, s3_live_bucket_and_kms):
    bucket, _ = s3_live_bucket_and_kms
    session, _ = s3_record_session
    client = session.client("s3")
    store = S3ArtifactStore(
        bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
    )
    run = _run_id()
    try:
        url = store.signed_url(run, "signed-put.bin", op="PUT", ttl_s=300)
        # S3 signed PUTs require Content-Type to match the signature scope.
        # Pass Content-Type="" to suppress urllib's default
        # "application/x-www-form-urlencoded" which would break SigV4.
        req = urllib.request.Request(
            url,
            data=b"signed-put-payload",
            method="PUT",
            headers={"Content-Type": ""},
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status in (200, 204)
        assert (
            store.get_bytes(store.uri_for(run, "signed-put.bin"))
            == b"signed-put-payload"
        )
    finally:
        try:
            store.delete(store.uri_for(run, "signed-put.bin"))
        except Exception:
            pass


@pytest.mark.xfail(
    reason=(
        "SigV4 signs Host: localhost:<port>; proxy rewrites Host to "
        "s3.us-east-1.amazonaws.com which breaks signature validation. "
        "Retry logic is verified offline via test_proxy.py + FakeS3Client unit tests."
    ),
    strict=False,
)
def test_s3_retry_via_proxy(s3_live_bucket_and_kms):
    """Retry axis is NOT captured into a fixture — the proxy IS the verification."""
    bucket, _ = s3_live_bucket_and_kms
    target_endpoint = "https://s3.us-east-1.amazonaws.com"
    with Fail503Proxy(target_endpoint, fail_count=2) as proxy:
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=proxy.endpoint,
            config=BotocoreConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": "path"},
                signature_version="s3v4",
            ),
        )
        store = S3ArtifactStore(
            bucket=bucket, client=client, cfg=StoreConfig(kind="s3", bucket=bucket)
        )
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
