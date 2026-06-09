"""Tests for S3ArtifactStore — all run against FakeS3Client (no network).

Spec: docs/superpowers/specs/2026-05-29-s3-gcs-stores-design.md §3.1 + §8.2
Layer W T3: multipart + encryption + signed_url + retry pin.
Layer W T11: TestS3FromFixture — fixture-replay wire-shape tests.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.s3 import S3ArtifactStore
from tests.stores.conftest import FakeS3Client
from tests.stores.recording import FixtureReplayS3Client

_FIXTURES = Path(__file__).parent / "fixtures" / "s3"


@pytest.fixture()
def fake_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture()
def store(fake_client: FakeS3Client) -> S3ArtifactStore:
    return S3ArtifactStore(bucket="bkt", prefix="prefix", client=fake_client)


# --- AC1: put_bytes returns a properly-scheme'd Artifact ---------------------


def test_put_bytes_returns_artifact_with_s3_uri(store: S3ArtifactStore) -> None:
    """put_bytes returns Artifact with uri = s3://<bucket>/<prefix>/<run_id>/<name>.

    Bug this catches: returning a path-style uri ("/bucket/...") or omitting the scheme.
    """
    artifact = store.put_bytes("run-1", "out.bin", b"\x00\x01")
    assert artifact.uri == "s3://bkt/prefix/run-1/out.bin"


# --- AC2: get_bytes round-trips ----------------------------------------------


def test_get_bytes_round_trips(store: S3ArtifactStore) -> None:
    """Bytes written by put_bytes are recovered exactly by get_bytes(uri).

    Bug this catches: reading from the wrong key or wrong bucket.
    """
    artifact = store.put_bytes("run-1", "blob.bin", b"hello s3")
    assert store.get_bytes(artifact.uri) == b"hello s3"


# --- AC3: prefix handling ----------------------------------------------------


def test_put_get_with_prefix(store: S3ArtifactStore, fake_client: FakeS3Client) -> None:
    """Non-empty prefix is folded into the object Key, not the URI separately.

    Bug this catches: storing under <run_id>/<name> ignoring prefix; or
    prepending prefix as a separate URI path segment with stray slashes.
    """
    store.put_bytes("rid", "a.bin", b"x")
    # Key stored in fake should include prefix.
    assert ("bkt", "prefix/rid/a.bin") in fake_client._objects


def test_put_get_with_empty_prefix(fake_client: FakeS3Client) -> None:
    """Empty prefix produces no double slashes and no leading slash in key.

    Bug this catches: '' prefix yielding key '/<run>/<name>' (S3 silently
    accepts this, but `list` and cross-instance reads break).
    """
    store = S3ArtifactStore(bucket="bkt", prefix="", client=fake_client)
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "s3://bkt/rid/a.bin"
    assert ("bkt", "rid/a.bin") in fake_client._objects


def test_put_get_with_slash_normalised_prefix(fake_client: FakeS3Client) -> None:
    """Leading and trailing slashes in prefix are stripped during init.

    Bug this catches: user passes '/foo/bar/' as prefix, store concatenates
    blindly, producing key '/foo/bar//rid/name'.
    """
    store = S3ArtifactStore(bucket="bkt", prefix="/foo/bar/", client=fake_client)
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "s3://bkt/foo/bar/rid/a.bin"


# --- AC4: put_json round-trips -----------------------------------------------


def test_put_json_round_trips(store: S3ArtifactStore) -> None:
    """A dict written by put_json is recovered as an equivalent dict.

    Bug this catches: re-encoding on read causing type drift (e.g. int -> str).
    """
    obj = {"key": "value", "count": 42, "nested": {"x": 1.5}}
    artifact = store.put_json("rid", "data.json", obj)
    assert store.get_json(artifact.uri) == obj


# --- AC5: run_id isolation ---------------------------------------------------


def test_run_ids_are_isolated(store: S3ArtifactStore) -> None:
    """Same name, different run_ids → different keys / different bytes.

    Bug this catches: omitting run_id from the key so the two writes clobber.
    """
    art_a = store.put_bytes("run-a", "x.bin", b"A")
    art_b = store.put_bytes("run-b", "x.bin", b"B")
    assert store.get_bytes(art_a.uri) == b"A"
    assert store.get_bytes(art_b.uri) == b"B"


# --- AC6: list ---------------------------------------------------------------


def test_list_returns_names_for_run_id(store: S3ArtifactStore) -> None:
    """list(run_id) returns the name strings as passed to put_bytes.

    Bug this catches: returning full object Keys instead of name-relative paths.
    """
    store.put_bytes("rx", "a.bin", b"a")
    store.put_bytes("rx", "b.bin", b"b")
    assert sorted(store.list("rx")) == ["a.bin", "b.bin"]


def test_list_nested_name_preserves_subpath(store: S3ArtifactStore) -> None:
    """A name with subdirectory components survives list() unchanged.

    Bug this catches: list() strips '/' so 'profiles/abc.json' becomes 'abc.json'.
    """
    store.put_bytes("rx", "profiles/abc.json", b"{}")
    assert "profiles/abc.json" in store.list("rx")


def test_list_empty_run_id_returns_empty_list(store: S3ArtifactStore) -> None:
    """list() for a run_id with no items returns [] (not an error).

    Bug this catches: raising on empty page or on missing 'Contents' key.
    """
    assert store.list("never-existed") == []


def test_list_excludes_other_run_ids(store: S3ArtifactStore) -> None:
    """list(run_id) shows only items from that run_id, not sibling run_ids.

    Bug this catches: prefix not strict-bounded by trailing '/'; 'run-1' would
    accidentally include items under 'run-10/'.
    """
    store.put_bytes("run-1", "item.bin", b"1")
    store.put_bytes("run-10", "item.bin", b"10")
    assert store.list("run-1") == ["item.bin"]


# --- AC7: delete -------------------------------------------------------------


def test_delete_removes_item(store: S3ArtifactStore) -> None:
    """delete(uri) removes the object; subsequent get_bytes raises FileNotFoundError.

    Bug this catches: delete() silently no-ops when the key is missing in fake,
    or doesn't actually pop from the underlying dict.
    """
    artifact = store.put_bytes("rid", "to_del.bin", b"bye")
    store.delete(artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(artifact.uri)


def test_delete_missing_raises_file_not_found(store: S3ArtifactStore) -> None:
    """delete() on a non-existent URI raises FileNotFoundError.

    Bug this catches: silently ignoring missing keys (S3 delete_object is
    idempotent — without the head_object check the ABC contract is violated).
    """
    with pytest.raises(FileNotFoundError):
        store.delete("s3://bkt/prefix/never/x.bin")


def test_get_bytes_missing_raises_file_not_found(store: S3ArtifactStore) -> None:
    """get_bytes on a missing key raises FileNotFoundError.

    Bug this catches: NoSuchKey propagates unmapped, breaking caller's
    ABC-contract expectations.
    """
    with pytest.raises(FileNotFoundError):
        store.get_bytes("s3://bkt/prefix/missing/x.bin")


# --- AC8: uri_for invariant --------------------------------------------------


def test_uri_for_matches_put_bytes_artifact_uri(store: S3ArtifactStore) -> None:
    """uri_for(rid, name) == put_bytes(rid, name, b).uri (cross-method invariant).

    Bug this catches: uri_for diverges from put-time URI — JsonProfileCache
    cross-restart reads break against this store.
    """
    artifact = store.put_bytes("rid", "blob.bin", b"x")
    assert store.uri_for("rid", "blob.bin") == artifact.uri


def test_uri_for_matches_put_json_artifact_uri(store: S3ArtifactStore) -> None:
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
    S3ArtifactStore(bucket="bkt", client=FakeS3Client())

    assert "boto3" not in sys.modules


# ---------------------------------------------------------------------------
# Layer W T3 — retry pin + multipart + encryption + signed_url
# ---------------------------------------------------------------------------


def _store_with_fake(
    fake_client: FakeS3Client, *, encryption: StoreEncryptionConfig | None = None
) -> S3ArtifactStore:
    """Build S3ArtifactStore around an in-test fake with optional encryption config."""
    cfg = StoreConfig(
        kind="s3",
        bucket="layer-w-test",
        encryption=encryption or StoreEncryptionConfig(),
    )
    return S3ArtifactStore(bucket="layer-w-test", client=fake_client, cfg=cfg)


def test_s3_retry_config_pinned(fake_s3_client: FakeS3Client) -> None:
    """T3 AC1: boto3 client built with retries={max_attempts:3, mode:standard}."""
    store = _store_with_fake(fake_s3_client)
    retries = store._client.meta.config.retries
    assert retries["max_attempts"] == 3
    assert retries["mode"] == "standard"


def test_s3_retry_config_pinned_on_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """T3 AC1: boto3.client('s3') is called with pinned retry config.

    Unlike test_s3_retry_config_pinned, this test verifies the actual
    boto3.client call (not just the fake's stamp). Uses monkeypatch to
    capture the config kwarg without a live boto3 import.
    """
    captured_configs: list[Any] = []

    def fake_boto3_client(
        service_name: str = "", *args: Any, config: Any = None, **kwargs: Any
    ) -> Any:
        """Capture the config kwarg and return a minimal mock."""
        captured_configs.append(config)
        # Return a minimal object with meta.config so __init__ doesn't crash.
        return type(
            "Client",
            (),
            {
                "meta": type("Meta", (), {"config": config})(),
                "set_retry_config": lambda self, retries: None,
            },
        )()

    # Inject fake boto3 module into sys.modules before S3ArtifactStore imports it.
    fake_boto3_module = type("Module", (), {"client": fake_boto3_client})()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3_module)

    # Construct without injecting a client — forces the lazy import path.
    S3ArtifactStore(bucket="test-bucket")
    assert len(captured_configs) == 1
    config = captured_configs[0]
    assert hasattr(config, "retries")
    assert config.retries["max_attempts"] == 3
    assert config.retries["mode"] == "standard"


def test_s3_put_bytes_uses_upload_fileobj(fake_s3_client: FakeS3Client) -> None:
    """T3 AC2+3: put_bytes calls upload_fileobj; default encryption adds no ExtraArgs."""
    store = _store_with_fake(fake_s3_client)
    artifact = store.put_bytes("run1", "out.bin", b"hello")
    assert fake_s3_client.upload_fileobj_calls, "expected upload_fileobj to be called"
    bucket, key, body, extra = fake_s3_client.upload_fileobj_calls[0]
    assert bucket == "layer-w-test"
    assert body == b"hello"
    assert "ServerSideEncryption" not in extra  # default mode — no override
    assert artifact.uri.startswith("s3://layer-w-test/")


def test_s3_put_bytes_kms_extra_args(fake_s3_client: FakeS3Client) -> None:
    """T3 AC4: KMS mode injects ServerSideEncryption + SSEKMSKeyId into ExtraArgs."""
    enc = StoreEncryptionConfig(
        mode="kms", kms_key_id="arn:aws:kms:us-east-1:1:key/abc"
    )
    store = _store_with_fake(fake_s3_client, encryption=enc)
    store.put_bytes("run1", "out.bin", b"hello")
    _, _, _, extra = fake_s3_client.upload_fileobj_calls[0]
    assert extra["ServerSideEncryption"] == "aws:kms"
    assert extra["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:1:key/abc"


def test_s3_signed_url_get(fake_s3_client: FakeS3Client) -> None:
    """T3 AC5: signed_url GET maps to generate_presigned_url('get_object', ...)."""
    store = _store_with_fake(fake_s3_client)
    url = store.signed_url("run1", "out.bin", op="GET", ttl_s=600)
    op, params, ttl = fake_s3_client.generate_presigned_url_calls[0]
    assert op == "get_object"
    assert params == {"Bucket": "layer-w-test", "Key": "run1/out.bin"}
    assert ttl == 600
    assert url.startswith("https://layer-w-test.s3.amazonaws.com/run1/out.bin?")


def test_s3_signed_url_put(fake_s3_client: FakeS3Client) -> None:
    """T3 AC6: signed_url PUT maps to generate_presigned_url('put_object', ...)."""
    store = _store_with_fake(fake_s3_client)
    store.signed_url("run1", "out.bin", op="PUT", ttl_s=120)
    op, _, ttl = fake_s3_client.generate_presigned_url_calls[0]
    assert op == "put_object"
    assert ttl == 120


# ---------------------------------------------------------------------------
# Layer W T11 — TestS3FromFixture: wire-shape invariants against captured fixtures
# ---------------------------------------------------------------------------


class TestS3FromFixture:
    """Replay captured S3 fixture JSON and assert wire-shape invariants.

    These tests exercise no network I/O — they load committed ``.json``
    fixture files and verify that the recorded responses carry the shape
    properties the production code depends on.
    """

    def test_multipart_etag_has_dash_suffix(self) -> None:
        """Multipart-upload ETag must carry a ``-N`` part-count suffix.

        Bug this catches: S3 records a simple hex ETag for multipart uploads
        (which would mean the SDK silently fell back to single-part) — the
        ``-2`` suffix confirms two parts were actually uploaded.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_multipart.json")
        head = client.head_object(Bucket="any", Key="any")
        etag = head["ETag"].strip('"')
        assert "-" in etag, f"expected multipart ETag with -N suffix, got {etag!r}"

    def test_kms_response_carries_aws_kms(self) -> None:
        """KMS-encrypted HeadObject must carry ``aws:kms`` SSE type and redacted key.

        Bug this catches: wrong SSE mode recorded (AES256 instead of aws:kms),
        indicating the KMS ExtraArgs were not forwarded to upload_fileobj.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_encryption_kms.json")
        head = client.head_object(Bucket="any", Key="any")
        assert head.get("ServerSideEncryption") == "aws:kms", (
            f"expected aws:kms, got {head.get('ServerSideEncryption')!r}"
        )
        key_id: str = head.get("SSEKMSKeyId", "")
        assert "<S3_KMS_KEY>" in key_id, (
            f"expected redacted placeholder in SSEKMSKeyId, got {key_id!r}"
        )

    def test_default_encryption_is_aes256(self) -> None:
        """Default-encryption HeadObject must carry ``AES256`` (bucket-default SSE).

        Bug this catches: no SSE field in the response — bucket is not
        configured with default encryption at rest.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_encryption_default.json")
        head = client.head_object(Bucket="any", Key="any")
        assert head.get("ServerSideEncryption") == "AES256", (
            f"expected AES256, got {head.get('ServerSideEncryption')!r}"
        )

    def test_hot_path_round_trip(self) -> None:
        """Hot-path fixture must carry a HeadObject with ETag and ContentLength > 0.

        Bug this catches: hot_path fixture is empty or mis-classified — no
        HeadObject entry means the recording seam broke during capture.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_hot_path.json")
        head = client.head_object(Bucket="any", Key="any")
        assert head.get("ETag"), "expected non-empty ETag in hot_path HeadObject"
        assert int(head.get("ContentLength", 0)) > 0, (
            "expected ContentLength > 0 in hot_path HeadObject"
        )

    def test_signed_url_get_shape(self) -> None:
        """Signed-URL GET fixture must yield a valid HTTPS URL containing the bucket.

        Bug this catches: generate_presigned_url returns a non-HTTPS URL or
        omits the bucket name — signed URL generation is broken.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_signed_url_get.json")
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "<GCS_KMS_KEYRING>-<AWS_ACCOUNT>", "Key": "k"},
            ExpiresIn=300,
        )
        assert url.startswith("https://"), f"expected HTTPS URL, got {url!r}"
        # The bucket name has the <AWS_ACCOUNT> placeholder after redaction.
        assert "<GCS_KMS_KEYRING>" in url, f"expected bucket name in URL, got {url!r}"

    def test_signed_url_put_shape(self) -> None:
        """Signed-URL PUT fixture must yield a valid HTTPS URL containing the bucket.

        Bug this catches: put_object presigned URL generation broken —
        same root cause as GET but exercised on the write path.
        """
        client = FixtureReplayS3Client(_FIXTURES / "test_s3_signed_url_put.json")
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": "<GCS_KMS_KEYRING>-<AWS_ACCOUNT>", "Key": "k"},
            ExpiresIn=300,
        )
        assert url.startswith("https://"), f"expected HTTPS URL, got {url!r}"
        assert "<GCS_KMS_KEYRING>" in url, f"expected bucket name in URL, got {url!r}"
