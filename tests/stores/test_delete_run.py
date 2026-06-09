"""Tests for ArtifactStore.delete_run + manual_cleanup_command.

Covers all three concrete stores (Local + S3 + GCS).
"""

from pathlib import Path

from kinoforge.stores.gcs import GCSArtifactStore
from kinoforge.stores.local import LocalArtifactStore
from kinoforge.stores.s3 import S3ArtifactStore
from tests.stores.conftest import FakeGCSClient, FakeS3Client


def test_local_delete_run_removes_directory(tmp_path: Path) -> None:
    """delete_run wipes ``<root>/<run_id>/`` recursively.

    Would-fail-bug: a stub that left subdirectories untouched would let
    an EphemeralSession claim cleanup while artifacts remained on disk.
    """
    store = LocalArtifactStore(root=tmp_path)
    store.put_json("run-1", "ledger.json", {"k": "v"})
    store.put_bytes("run-1", "abc.mp4", b"video bytes")
    assert (tmp_path / "run-1").exists()
    store.delete_run("run-1")
    assert not (tmp_path / "run-1").exists()


def test_local_delete_run_idempotent_on_missing(tmp_path: Path) -> None:
    """Calling delete_run on an absent run_id is a no-op, not an error.

    Would-fail-bug: EphemeralSession.__exit__ would crash on cleanup of a
    run that never wrote anything.
    """
    store = LocalArtifactStore(root=tmp_path)
    store.delete_run("never-existed")  # no raise


def test_local_manual_cleanup_command_shape(tmp_path: Path) -> None:
    """rm -rf <absolute path>, double-quoted."""
    store = LocalArtifactStore(root=tmp_path)
    cmd = store.manual_cleanup_command("abc-123")
    assert cmd.startswith("rm -rf ")
    assert f'"{tmp_path / "abc-123"}"' in cmd


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def test_s3_delete_run_paginates_and_deletes(fake_s3_client: FakeS3Client) -> None:
    """delete_run wipes every object under the run prefix.

    Would-fail-bug: a stub that only ran one delete_objects call would leak
    every object past index 1000 in a 2500-object run.
    """
    store = S3ArtifactStore(bucket="b", prefix="kf", client=fake_s3_client)
    for i in range(2500):
        store.put_json("r1", f"file-{i}.json", {"i": i})
    store.delete_run("r1")
    assert store.list("r1") == []


def test_s3_delete_run_empty_prefix_idempotent(fake_s3_client: FakeS3Client) -> None:
    """delete_run on an absent run_id is a no-op."""
    store = S3ArtifactStore(bucket="b", prefix="kf", client=fake_s3_client)
    store.delete_run("never-existed")  # no raise


def test_s3_manual_cleanup_command_shape(fake_s3_client: FakeS3Client) -> None:
    """aws s3 rm s3://<bucket>/<prefix>/<run_id>/ --recursive."""
    store = S3ArtifactStore(bucket="my-bucket", prefix="kf", client=fake_s3_client)
    assert (
        store.manual_cleanup_command("r1")
        == "aws s3 rm s3://my-bucket/kf/r1/ --recursive"
    )


# ---------------------------------------------------------------------------
# GCS
# ---------------------------------------------------------------------------


def test_gcs_delete_run_lists_then_batches(fake_gcs_client: FakeGCSClient) -> None:
    """delete_run wipes every blob under the run prefix.

    Would-fail-bug: forgetting to materialise the list_blobs generator before
    delete_blobs would consume it as an empty iterable on the second sweep.
    """
    store = GCSArtifactStore(
        bucket="b",
        prefix="kf",
        client=fake_gcs_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    store.put_json("r1", "a.json", {"k": 1})
    store.put_json("r1", "b.json", {"k": 2})
    store.delete_run("r1")
    assert store.list("r1") == []


def test_gcs_delete_run_empty_prefix_idempotent(
    fake_gcs_client: FakeGCSClient,
) -> None:
    """delete_run on an absent prefix is a no-op."""
    store = GCSArtifactStore(
        bucket="b",
        prefix="kf",
        client=fake_gcs_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    store.delete_run("never-existed")  # no raise


def test_gcs_manual_cleanup_command_shape(
    fake_gcs_client: FakeGCSClient,
) -> None:
    """gcloud storage rm -r gs://<bucket>/<prefix>/<run_id>/."""
    store = GCSArtifactStore(
        bucket="my-bucket",
        prefix="kf",
        client=fake_gcs_client,
        not_found_exc=FakeGCSClient.NotFound,
    )
    assert (
        store.manual_cleanup_command("r1")
        == "gcloud storage rm -r gs://my-bucket/kf/r1/"
    )
