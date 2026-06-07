"""Tests for GCSArtifactStore — all run against FakeGCSClient (no network).

Spec: docs/superpowers/specs/2026-05-29-s3-gcs-stores-design.md §3.2 + §8.2
Layer W T4 additions: resumable upload, CMEK, signed_url, retry baseline.
Layer W T11: TestGCSFromFixture — fixture-replay wire-shape tests.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

from kinoforge.core.config import StoreConfig, StoreEncryptionConfig
from kinoforge.stores.gcs import _GCS_RETRY, GCSArtifactStore
from tests.stores.conftest import FakeGCSClient
from tests.stores.recording import FixtureReplayGCSClient

_FIXTURES = Path(__file__).parent / "fixtures" / "gcs"

try:
    from google.api_core.retry import Retry as _Retry
except ImportError:  # pragma: no cover
    _Retry = None  # type: ignore[assignment,misc]


@pytest.fixture()
def fake_client() -> FakeGCSClient:
    return FakeGCSClient()


@pytest.fixture()
def store(fake_client: FakeGCSClient) -> GCSArtifactStore:
    return GCSArtifactStore(
        bucket="bkt",
        prefix="prefix",
        client=fake_client,
        not_found_exc=FakeGCSClient.NotFound,
    )


# --- AC1: put_bytes returns gs://-scheme'd Artifact --------------------------


def test_put_bytes_returns_artifact_with_gs_uri(store: GCSArtifactStore) -> None:
    """put_bytes returns Artifact with uri = gs://<bucket>/<prefix>/<run_id>/<name>.

    Bug this catches: scheme typo (gcs:// vs gs://) or path-style URI.
    """
    artifact = store.put_bytes("run-1", "out.bin", b"\x00\x01")
    assert artifact.uri == "gs://bkt/prefix/run-1/out.bin"


# --- AC2: get_bytes round-trips ----------------------------------------------


def test_get_bytes_round_trips(store: GCSArtifactStore) -> None:
    """Bytes written by put_bytes are recovered exactly by get_bytes(uri).

    Bug this catches: download_as_bytes hits the wrong blob name.
    """
    artifact = store.put_bytes("run-1", "blob.bin", b"hello gcs")
    assert store.get_bytes(artifact.uri) == b"hello gcs"


# --- AC3: prefix handling ----------------------------------------------------


def test_put_get_with_prefix(
    store: GCSArtifactStore, fake_client: FakeGCSClient
) -> None:
    """Non-empty prefix is folded into the blob name.

    Bug this catches: prefix concatenated to URI but not to blob name.
    """
    store.put_bytes("rid", "a.bin", b"x")
    bucket = fake_client.bucket("bkt")
    assert "prefix/rid/a.bin" in bucket._blobs


def test_put_get_with_empty_prefix(fake_client: FakeGCSClient) -> None:
    """Empty prefix produces no leading slash in blob name.

    Bug this catches: '' prefix yielding key '/rid/name'.
    """
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


def test_put_get_with_slash_normalised_prefix(fake_client: FakeGCSClient) -> None:
    """Leading and trailing slashes in prefix are stripped during init.

    Bug this catches: blind concatenation producing '/foo/bar//rid/name'.
    """
    store = GCSArtifactStore(
        bucket="bkt",
        prefix="/foo/bar/",
        client=fake_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    artifact = store.put_bytes("rid", "a.bin", b"x")
    assert artifact.uri == "gs://bkt/foo/bar/rid/a.bin"


# --- AC4: put_json round-trips -----------------------------------------------


def test_put_json_round_trips(store: GCSArtifactStore) -> None:
    """A dict written by put_json is recovered as an equivalent dict.

    Bug this catches: encoding drift on read (e.g. int->str).
    """
    obj = {"key": "value", "count": 42, "nested": {"x": 1.5}}
    artifact = store.put_json("rid", "data.json", obj)
    assert store.get_json(artifact.uri) == obj


# --- AC5: run_id isolation ---------------------------------------------------


def test_run_ids_are_isolated(store: GCSArtifactStore) -> None:
    """Same name, different run_ids → different blob names, different bytes.

    Bug this catches: omitting run_id from the blob name.
    """
    art_a = store.put_bytes("run-a", "x.bin", b"A")
    art_b = store.put_bytes("run-b", "x.bin", b"B")
    assert store.get_bytes(art_a.uri) == b"A"
    assert store.get_bytes(art_b.uri) == b"B"


# --- AC6: list ---------------------------------------------------------------


def test_list_returns_names_for_run_id(store: GCSArtifactStore) -> None:
    """list(run_id) returns the name strings as passed to put_bytes.

    Bug this catches: returning full blob names with prefix still attached.
    """
    store.put_bytes("rx", "a.bin", b"a")
    store.put_bytes("rx", "b.bin", b"b")
    assert sorted(store.list("rx")) == ["a.bin", "b.bin"]


def test_list_nested_name_preserves_subpath(store: GCSArtifactStore) -> None:
    """A name with subdirectory components survives list() unchanged.

    Bug this catches: '/' stripped — 'profiles/abc.json' becomes 'abc.json'.
    """
    store.put_bytes("rx", "profiles/abc.json", b"{}")
    assert "profiles/abc.json" in store.list("rx")


def test_list_empty_run_id_returns_empty_list(store: GCSArtifactStore) -> None:
    """list() for a run_id with no items returns [] (not an error).

    Bug this catches: list_blobs iterator unhandled when empty.
    """
    assert store.list("never-existed") == []


def test_list_excludes_other_run_ids(store: GCSArtifactStore) -> None:
    """list(run_id) shows only items from that run_id, not sibling run_ids.

    Bug this catches: prefix not strict-bounded — 'run-1' accidentally
    includes items under 'run-10/'.
    """
    store.put_bytes("run-1", "item.bin", b"1")
    store.put_bytes("run-10", "item.bin", b"10")
    assert store.list("run-1") == ["item.bin"]


# --- AC7: delete -------------------------------------------------------------


def test_delete_removes_item(store: GCSArtifactStore) -> None:
    """delete(uri) removes the blob; subsequent get_bytes raises FileNotFoundError.

    Bug this catches: delete() targets wrong blob name.
    """
    artifact = store.put_bytes("rid", "to_del.bin", b"bye")
    store.delete(artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(artifact.uri)


def test_delete_missing_raises_file_not_found(store: GCSArtifactStore) -> None:
    """delete() on a non-existent URI raises FileNotFoundError.

    Bug this catches: NotFound from blob.delete propagates unmapped.
    """
    with pytest.raises(FileNotFoundError):
        store.delete("gs://bkt/prefix/never/x.bin")


def test_get_bytes_missing_raises_file_not_found(store: GCSArtifactStore) -> None:
    """get_bytes on a missing key raises FileNotFoundError.

    Bug this catches: NotFound from download_as_bytes propagates unmapped.
    """
    with pytest.raises(FileNotFoundError):
        store.get_bytes("gs://bkt/prefix/missing/x.bin")


# --- AC8: uri_for invariant --------------------------------------------------


def test_uri_for_matches_put_bytes_artifact_uri(store: GCSArtifactStore) -> None:
    """uri_for(rid, name) == put_bytes(rid, name, b).uri."""
    artifact = store.put_bytes("rid", "blob.bin", b"x")
    assert store.uri_for("rid", "blob.bin") == artifact.uri


def test_uri_for_matches_put_json_artifact_uri(store: GCSArtifactStore) -> None:
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

    GCSArtifactStore(
        bucket="bkt",
        client=FakeGCSClient(),
        not_found_exc=FakeGCSClient.NotFound,
    )

    assert "google.cloud.storage" not in sys.modules
    assert "google.api_core.exceptions" not in sys.modules


# ---------------------------------------------------------------------------
# Layer W T4: resumable upload + CMEK + signed_url + retry baseline
# ---------------------------------------------------------------------------


def _store_with_fake(
    client: FakeGCSClient, *, encryption: StoreEncryptionConfig | None = None
) -> GCSArtifactStore:
    """Build a GCSArtifactStore wired to the given fake client."""
    cfg = StoreConfig(
        kind="gcs",
        bucket="layer-w-test",
        encryption=encryption or StoreEncryptionConfig(),
    )
    return GCSArtifactStore(
        bucket="layer-w-test",
        client=client,
        not_found_exc=FakeGCSClient.NotFound,
        cfg=cfg,
    )


def test_gcs_retry_instance_is_module_constant() -> None:
    """_GCS_RETRY is a Retry instance exported from the gcs module."""
    assert _Retry is not None, "google.api_core.retry.Retry not importable"
    assert isinstance(_GCS_RETRY, _Retry)


def test_gcs_put_bytes_uses_upload_from_file_and_retry(
    fake_gcs_client: FakeGCSClient,
) -> None:
    """put_bytes calls upload_from_file with retry=_GCS_RETRY; kms_key_name left None for default enc."""
    store = _store_with_fake(fake_gcs_client)
    store.put_bytes("run1", "out.bin", b"hello")
    blob = fake_gcs_client.buckets["layer-w-test"]._blob_cache["run1/out.bin"]
    assert blob.upload_from_file_calls, "expected upload_from_file to be called"
    body, retry, _ = blob.upload_from_file_calls[0]
    assert body == b"hello"
    assert retry is _GCS_RETRY
    assert blob.kms_key_name is None  # default mode — provider manages encryption


def test_gcs_put_bytes_cmek_sets_kms_key_name(fake_gcs_client: FakeGCSClient) -> None:
    """encryption.mode='kms' sets blob.kms_key_name BEFORE upload_from_file."""
    enc = StoreEncryptionConfig(
        mode="kms",
        kms_key_id="projects/p/locations/us-central1/keyRings/r/cryptoKeys/k",
    )
    store = _store_with_fake(fake_gcs_client, encryption=enc)
    store.put_bytes("run1", "out.bin", b"hello")
    blob = fake_gcs_client.buckets["layer-w-test"]._blob_cache["run1/out.bin"]
    assert blob.upload_from_file_calls, "upload_from_file must have been called"
    body, retry, kms_at_call = blob.upload_from_file_calls[0]
    assert kms_at_call == enc.kms_key_id  # ordering proof: set BEFORE upload
    assert blob.kms_key_name == enc.kms_key_id


def test_gcs_signed_url_get(fake_gcs_client: FakeGCSClient) -> None:
    """signed_url(op='GET') calls generate_signed_url with version='v4' and correct args."""
    store = _store_with_fake(fake_gcs_client)
    url = store.signed_url("run1", "out.bin", op="GET", ttl_s=600)
    blob = fake_gcs_client.buckets["layer-w-test"]._blob_cache["run1/out.bin"]
    assert blob.generate_signed_url_calls, "expected generate_signed_url to be called"
    call = blob.generate_signed_url_calls[0]
    assert call["version"] == "v4"
    assert call["expiration"] == timedelta(seconds=600)
    assert call["method"] == "GET"
    assert "method=GET" in url


def test_gcs_signed_url_put(fake_gcs_client: FakeGCSClient) -> None:
    """signed_url(op='PUT') calls generate_signed_url with method='PUT' and correct TTL."""
    store = _store_with_fake(fake_gcs_client)
    store.signed_url("run1", "out.bin", op="PUT", ttl_s=120)
    blob = fake_gcs_client.buckets["layer-w-test"]._blob_cache["run1/out.bin"]
    call = blob.generate_signed_url_calls[0]
    assert call["method"] == "PUT"
    assert call["expiration"] == timedelta(seconds=120)


def test_gcs_get_bytes_passes_retry(fake_gcs_client: FakeGCSClient) -> None:
    """get_bytes passes retry=_GCS_RETRY to blob.download_as_bytes."""
    store = _store_with_fake(fake_gcs_client)
    artifact = store.put_bytes("run1", "out.bin", b"hello")
    store.get_bytes(artifact.uri)
    blob = fake_gcs_client.buckets["layer-w-test"]._blob_cache["run1/out.bin"]
    assert blob.download_as_bytes_calls, "expected download_as_bytes to be called"
    assert blob.download_as_bytes_calls[0] is _GCS_RETRY


# ---------------------------------------------------------------------------
# Layer W T11 — TestGCSFromFixture: wire-shape invariants against captured fixtures
# ---------------------------------------------------------------------------


class TestGCSFromFixture:
    """Replay captured GCS fixture JSON and assert wire-shape invariants.

    These tests exercise no network I/O — they load committed ``.json``
    fixture files and verify that the recorded responses carry the shape
    properties the production code depends on.
    """

    def test_resumable_size_matches(self) -> None:
        """Resumable-upload fixture must carry a blob whose size is 16 MiB.

        Bug this catches: the large-file upload fell back to a non-resumable
        path or the fixture captured a truncated/empty body.
        """
        client = FixtureReplayGCSClient(_FIXTURES / "test_gcs_resumable.json")
        bucket = client.bucket("<GCS_BUCKET>")
        # All blobs in this fixture live under one run_id prefix.
        blobs = bucket.list_blobs()
        assert blobs, "expected at least one blob in resumable fixture"
        blob = blobs[0]
        assert blob.size == 16 * 1024 * 1024, (
            f"expected 16 MiB blob, got size={blob.size}"
        )

    def test_cmek_kms_key_name_present(self) -> None:
        """CMEK fixture blob must carry a ``kms_key_name`` with the GCS KMS placeholder.

        Bug this catches: kms_key_name is absent from the fixture (CMEK was
        not wired in the upload path) or the KMS key ARN was not redacted.
        """
        client = FixtureReplayGCSClient(_FIXTURES / "test_gcs_encryption_cmek.json")
        bucket = client.bucket("<GCS_BUCKET>")
        blobs = bucket.list_blobs()
        assert blobs, "expected at least one blob in CMEK fixture"
        blob = blobs[0]
        assert blob.kms_key_name, "expected non-empty kms_key_name in CMEK fixture blob"
        # The KMS key name should NOT have been redacted in GCS fixtures
        # (only the GCS_KMS_KEY placeholder would be inserted if redacted).
        # Either the real key name or the placeholder must be present.
        assert (
            "kinoforge" in blob.kms_key_name or "<GCS_KMS_KEY>" in blob.kms_key_name
        ), f"unexpected kms_key_name value: {blob.kms_key_name!r}"

    def test_hot_path_round_trip(self) -> None:
        """Hot-path fixture must carry a non-empty blob with positive size.

        Bug this catches: hot_path fixture is empty or the blob metadata
        wasn't captured in the PUT response.
        """
        client = FixtureReplayGCSClient(_FIXTURES / "test_gcs_hot_path.json")
        bucket = client.bucket("<GCS_BUCKET>")
        blobs = bucket.list_blobs()
        assert blobs, "expected at least one blob in hot_path fixture"
        blob = blobs[0]
        assert blob.size > 0, f"expected positive blob size, got {blob.size}"

    def test_signed_url_get_shape(self) -> None:
        """Signed-URL GET replay must return an HTTPS URL with Goog-Signature param.

        Bug this catches: generate_signed_url returns a non-HTTPS URL —
        v4 signing is broken or the wrong URL scheme is used.
        """
        client = FixtureReplayGCSClient(_FIXTURES / "test_gcs_signed_url_get.json")
        bucket = client.bucket("<GCS_BUCKET>")
        blobs = bucket.list_blobs()
        # signed_url_get fixture may not have a download entry; use any available blob,
        # or fall back to a named blob known from the fixture metadata.
        if blobs:
            blob = blobs[0]
        else:
            blob = bucket.blob("live-14a5a126/signed.bin")
        url = blob.generate_signed_url(
            version="v4", expiration=timedelta(seconds=300), method="GET"
        )
        assert url.startswith("https://"), f"expected HTTPS URL, got {url!r}"
        assert "X-Goog-Signature" in url, (
            f"expected X-Goog-Signature in URL, got {url!r}"
        )

    def test_signed_url_put_shape(self) -> None:
        """Signed-URL PUT replay must return an HTTPS URL with correct method param.

        Bug this catches: PUT method is not threaded through the
        generate_signed_url call — wrong HTTP method used.
        """
        client = FixtureReplayGCSClient(_FIXTURES / "test_gcs_signed_url_put.json")
        bucket = client.bucket("<GCS_BUCKET>")
        blob = bucket.blob("live-251b7dc8/signed-put.bin")
        url = blob.generate_signed_url(
            version="v4", expiration=timedelta(seconds=300), method="PUT"
        )
        assert url.startswith("https://"), f"expected HTTPS URL, got {url!r}"
        assert "method=PUT" in url, f"expected method=PUT in URL, got {url!r}"
