"""Tests for LocalArtifactStore — all ACs tested against a tmp_path root."""

from pathlib import Path

import pytest

from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalArtifactStore:
    """Return a LocalArtifactStore rooted at a fresh temporary directory."""
    return LocalArtifactStore(tmp_path)


# --- AC1: put_bytes / get_bytes round-trip -----------------------------------


def test_put_bytes_returns_artifact_with_uri(store: LocalArtifactStore) -> None:
    """put_bytes returns an Artifact; uri must be non-empty.

    Bug this catches: returning an Artifact with uri="" (forgetting to set it).
    """
    artifact = store.put_bytes("run-1", "out.bin", b"\x00\x01\x02")
    assert artifact.uri != ""


def test_get_bytes_round_trips(store: LocalArtifactStore) -> None:
    """Bytes written by put_bytes are recovered exactly by get_bytes(uri).

    Bug this catches: reading from the wrong path or encoding bytes as text.
    """
    data = b"hello artifact store"
    artifact = store.put_bytes("run-1", "blob.bin", data)
    assert store.get_bytes(artifact.uri) == data


# --- AC2: put_json / get_json round-trip -------------------------------------


def test_put_json_round_trips(store: LocalArtifactStore) -> None:
    """A dict written by put_json is recovered as an equivalent dict by get_json.

    Bug this catches: re-encoding on read causing type drift (e.g. int -> str).
    """
    obj = {"key": "value", "count": 42, "nested": {"x": 1.5}}
    artifact = store.put_json("run-1", "data.json", obj)
    recovered = store.get_json(artifact.uri)
    assert recovered == obj


# --- AC3: run_id isolation ---------------------------------------------------


def test_run_ids_are_isolated(store: LocalArtifactStore) -> None:
    """Two run_ids with the same name do not share storage.

    Bug this catches: omitting run_id from the path so both writes go to the
    same file; the second write clobbers the first.
    """
    store.put_bytes("run-a", "x", b"A")
    store.put_bytes("run-b", "x", b"B")
    art_a = store.put_bytes("run-a", "x", b"A")
    art_b = store.put_bytes("run-b", "x", b"B")
    assert store.get_bytes(art_a.uri) == b"A"
    assert store.get_bytes(art_b.uri) == b"B"


def test_storage_location_under_run_id(
    store: LocalArtifactStore, tmp_path: Path
) -> None:
    """Items land under <root>/<run_id>/<name> on disk.

    Bug this catches: a flat layout that ignores run_id and name hierarchy.
    """
    artifact = store.put_bytes("my-run", "profiles/abc.json", b"{}")
    stored_path = Path(artifact.uri)
    # The stored path must be inside tmp_path / "my-run"
    assert stored_path.is_relative_to(tmp_path / "my-run")
    # And it must end with the name component
    assert stored_path.name == "abc.json"


# --- AC4: list ---------------------------------------------------------------


def test_list_returns_names_for_run_id(store: LocalArtifactStore) -> None:
    """list(run_id) returns the names of items stored under that run_id.

    Bug this catches: returning absolute paths instead of relative names.
    """
    store.put_bytes("run-x", "a.bin", b"a")
    store.put_bytes("run-x", "b.bin", b"b")
    names = store.list("run-x")
    assert sorted(names) == ["a.bin", "b.bin"]


def test_list_nested_name_returns_relative_subpath(store: LocalArtifactStore) -> None:
    """A name with subdirectory components (e.g. profiles/abc.json) appears as-is.

    Bug this catches: stripping subpath so nested items are listed incorrectly.
    """
    store.put_bytes("run-x", "profiles/abc.json", b"{}")
    names = store.list("run-x")
    assert "profiles/abc.json" in names


def test_list_empty_run_id_returns_empty_list(store: LocalArtifactStore) -> None:
    """list() for a run_id with no items returns an empty list (not an error).

    Bug this catches: raising FileNotFoundError when the run_id dir doesn't exist yet.
    """
    result = store.list("run-that-never-existed")
    assert result == []


def test_list_excludes_other_run_ids(store: LocalArtifactStore) -> None:
    """list(run_id) shows only items from that run_id, not sibling run_ids.

    Bug this catches: a list() that scans the whole root and returns all items.
    """
    store.put_bytes("run-1", "item.bin", b"1")
    store.put_bytes("run-2", "item.bin", b"2")
    assert store.list("run-1") == ["item.bin"]


# --- AC5: delete -------------------------------------------------------------


def test_delete_removes_item(store: LocalArtifactStore) -> None:
    """delete(uri) removes the file; a subsequent get_bytes raises FileNotFoundError.

    Bug this catches: delete() that marks the item but doesn't unlink the file.
    """
    artifact = store.put_bytes("run-1", "to_delete.bin", b"bye")
    store.delete(artifact.uri)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(artifact.uri)


def test_delete_missing_raises_file_not_found(store: LocalArtifactStore) -> None:
    """delete() on a non-existent URI raises FileNotFoundError.

    Bug this catches: silently ignoring a missing file (missing_ok=True).
    """
    with pytest.raises(FileNotFoundError):
        store.delete("/tmp/kinoforge_nonexistent_file_xyzzy.bin")


# --- AC6: self-registration under "local" ------------------------------------


def test_local_store_self_registers() -> None:
    """Importing LocalArtifactStore registers it as "local" in the store registry.

    Bug this catches: forgetting the register_store("local", ...) call in local.py.
    """
    from kinoforge.core.registry import get_store

    factory = get_store("local")
    assert callable(factory)
