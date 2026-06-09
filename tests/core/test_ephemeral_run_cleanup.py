"""Tests for ``EphemeralSession.__exit__`` store cleanup + cleanup errors."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import EphemeralStoreCleanupFailedError
from kinoforge.stores.local import LocalArtifactStore


def test_exit_calls_delete_run_for_every_registered_store(tmp_path: Path) -> None:
    """Strict policy: every registered store gets ``delete_run`` on exit.

    Would-fail-bug: a session that only scrubbed the first registered store
    would leak the second store's run bytes after ``__exit__`` returned.
    """
    s_a = LocalArtifactStore(root=tmp_path / "a")
    s_b = LocalArtifactStore(root=tmp_path / "b")
    s_a.put_json("r1", "x.json", {"k": "v"})
    s_b.put_json("r2", "y.json", {"k": "v"})
    assert (tmp_path / "a" / "r1").is_dir()
    assert (tmp_path / "b" / "r2").is_dir()
    with EphemeralSession(enabled=True) as sess:
        sess.register_store(s_a, "r1")
        sess.register_store(s_b, "r2")
    assert not (tmp_path / "a" / "r1").exists()
    assert not (tmp_path / "b" / "r2").exists()


def test_default_mode_does_not_cleanup(tmp_path: Path) -> None:
    """Default policy (``delete_on_completion=False``): scrub does NOT fire.

    Would-fail-bug: a session that scrubbed under the default policy would
    delete every non-ephemeral caller's artifacts on context exit.
    """
    s = LocalArtifactStore(root=tmp_path)
    s.put_json("r1", "x.json", {"k": "v"})
    assert (tmp_path / "r1").is_dir()
    with EphemeralSession(enabled=False) as sess:
        sess.register_store(s, "r1")
    assert (tmp_path / "r1").is_dir()


def test_cleanup_runs_even_after_exception(tmp_path: Path) -> None:
    """The with-block raising still triggers ``__exit__`` — must scrub anyway.

    Would-fail-bug: gating cleanup on a clean exit would leak prompt-laden
    bytes whenever a downstream stage raised mid-generate.
    """
    s = LocalArtifactStore(root=tmp_path)
    s.put_json("r1", "x.json", {"k": "v"})
    with pytest.raises(RuntimeError, match="downstream-failure"):
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(s, "r1")
            raise RuntimeError("downstream-failure")
    assert not (tmp_path / "r1").exists()


def test_cleanup_failure_raises_with_command(tmp_path: Path) -> None:
    """``delete_run`` raising → ``EphemeralStoreCleanupFailedError``.

    The error carries ``.cleanup_command`` (the store's
    ``manual_cleanup_command`` for the run) so the operator can finish
    the scrub by hand.

    Would-fail-bug: swallowing the cleanup failure would let the CLI exit
    0 while leaving prompt-laden bytes on disk.
    """
    store = MagicMock()
    store.manual_cleanup_command.return_value = 'rm -rf "/some/path"'
    store.delete_run.side_effect = PermissionError("denied")
    with pytest.raises(EphemeralStoreCleanupFailedError) as exc_info:
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(store, "r1")
    assert 'rm -rf "/some/path"' in str(exc_info.value)
    assert exc_info.value.cleanup_command == 'rm -rf "/some/path"'
    assert exc_info.value.run_id == "r1"
    assert isinstance(exc_info.value.original_error, PermissionError)


def test_error_block_does_not_list_output_files() -> None:
    """Per D14: error block has no preserved-file enumeration.

    Spec §10.5 forbids leaking the on-disk filenames into the cleanup
    error message — those names may themselves be prompt-derived.

    Would-fail-bug: a ``_format`` that walked the store's leftover files
    and listed them in the error message would re-leak the prompt.
    """
    store = MagicMock()
    store.manual_cleanup_command.return_value = 'rm -rf "/x"'
    store.delete_run.side_effect = PermissionError("denied")
    try:
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(store, "r1")
    except EphemeralStoreCleanupFailedError as e:
        msg = str(e)
        assert "preserved" not in msg.lower()
        assert "output/" not in msg
        assert "ERROR: --ephemeral could not delete" in msg
        assert "To finish the scrub, run:" in msg
        assert 'rm -rf "/x"' in msg
    else:
        pytest.fail("expected EphemeralStoreCleanupFailedError")
