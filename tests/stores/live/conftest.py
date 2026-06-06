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
            pytest.skip(
                f"missing {AWS_KMS_KEY_FILE} — run pixi run cloud:bootstrap-kms"
            )
        import boto3

        try:
            boto3.client("sts").get_caller_identity()
        except Exception as exc:
            pytest.skip(f"AWS creds unusable: {exc}")
        return S3_BUCKET, AWS_KMS_KEY_FILE.read_text().strip()
    if cloud == "gcs":
        if not GCS_KMS_KEY_FILE.exists():
            pytest.skip(
                f"missing {GCS_KMS_KEY_FILE} — run pixi run cloud:bootstrap-kms"
            )
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
    axis = (
        request.node.callspec.params.get("axis")
        if hasattr(request.node, "callspec")
        else request.node.name
    )
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
    axis = (
        request.node.callspec.params.get("axis")
        if hasattr(request.node, "callspec")
        else request.node.name
    )
    target = FIXTURE_DIR_GCS / f"{axis}.json"
    yield client, recorder
    bucket, kms = gcs_live_bucket_and_kms
    recorder.flush(target, axis=axis, extra_subs={kms: "<GCS_KMS_KEY>"})
