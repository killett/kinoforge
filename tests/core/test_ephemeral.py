"""Tests for EphemeralSession / EphemeralPolicy / EPHEMERAL_CAPABILITIES."""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from kinoforge.core.ephemeral import (
    DEFAULT_POLICY,
    EPHEMERAL_CAPABILITIES,
    STRICT_POLICY,
    EphemeralPolicy,
    EphemeralSession,
)


def test_policy_is_frozen() -> None:
    """Policy is immutable — mutation attempts raise.

    Would-fail-bug: a non-frozen dataclass would let an engine flip
    ``ledger_record=False`` to ``True`` mid-run and leak the prompt.
    """
    with pytest.raises(AttributeError):
        DEFAULT_POLICY.ledger_record = False  # type: ignore[misc]


def test_default_policy_all_gates_open() -> None:
    """Default mode: writes happen, no delete-on-completion.

    Would-fail-bug: a default policy with ``ledger_record=False`` would
    silently break every non-ephemeral run.
    """
    assert DEFAULT_POLICY.ledger_record is True
    assert DEFAULT_POLICY.profile_cache_persist is True
    assert DEFAULT_POLICY.batch_summary_write is True
    assert DEFAULT_POLICY.cost_sidecar_write is True
    assert DEFAULT_POLICY.heartbeat_ledger_touch is True
    assert DEFAULT_POLICY.delete_on_completion is False
    assert DEFAULT_POLICY.delete_retries == 0
    assert DEFAULT_POLICY.memory_only_run_id is False
    assert DEFAULT_POLICY.pod_name_includes_alias is True
    assert DEFAULT_POLICY.force_debug_show_secrets_off is False


def test_strict_policy_all_gates_closed() -> None:
    """Ephemeral mode: skips writes, deletes on completion.

    Would-fail-bug: a strict policy that left ``profile_cache_persist=True``
    would persist a vault-loaded prompt-derived capability_key to disk.
    """
    assert STRICT_POLICY.ledger_record is False
    assert STRICT_POLICY.profile_cache_persist is False
    assert STRICT_POLICY.batch_summary_write is False
    assert STRICT_POLICY.cost_sidecar_write is False
    assert STRICT_POLICY.heartbeat_ledger_touch is False
    assert STRICT_POLICY.delete_on_completion is True
    assert STRICT_POLICY.delete_retries == 3
    assert STRICT_POLICY.memory_only_run_id is True
    assert STRICT_POLICY.pod_name_includes_alias is False
    assert STRICT_POLICY.force_debug_show_secrets_off is True


def test_session_current_none_outside_with() -> None:
    """``current()`` reports ``None`` when no with-block is active.

    Would-fail-bug: a module-level singleton would report a session even
    outside any with-block, making the write-site gates fire spuriously.
    """
    assert EphemeralSession.current() is None


def test_session_current_inside_with() -> None:
    """``current()`` returns the active session inside its with-block.

    Would-fail-bug: ``__enter__`` returning a different object would mean
    write-site checks compare against the wrong instance.
    """
    with EphemeralSession(enabled=True) as s:
        assert EphemeralSession.current() is s


def test_session_enabled_uses_strict_policy() -> None:
    """``enabled=True`` selects the strict policy.

    Would-fail-bug: an inverted boolean would route the strict (no-leak)
    policy to non-ephemeral callers and the open policy to ephemeral.
    """
    with EphemeralSession(enabled=True) as s:
        assert s.policy == STRICT_POLICY


def test_session_disabled_uses_default_policy() -> None:
    """``enabled=False`` selects the default policy.

    Would-fail-bug: an inverted boolean would deny every persistent write
    even outside ephemeral mode.
    """
    with EphemeralSession(enabled=False) as s:
        assert s.policy == DEFAULT_POLICY


def test_session_propagates_through_threadpool() -> None:
    """contextvars propagate the active session into ``ThreadPoolExecutor`` workers.

    Would-fail-bug: using ``threading.local`` would lose the session in
    workers, so ``ConcurrentPool``'s per-clip jobs would silently drop the
    ephemeral policy and persist redactable bytes.
    """
    with EphemeralSession(enabled=True):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            results = list(
                ex.map(lambda _: EphemeralSession.current() is not None, range(4))
            )
    assert all(results)
    assert EphemeralSession.current() is None


def test_session_register_store_appends(tmp_path: Path) -> None:
    """``register_store`` queues a (store, run_id) pair for Task-15 cleanup.

    Would-fail-bug: a no-op ``register_store`` would leave the cleanup
    list empty and ``__exit__`` would have nothing to delete.
    """
    from kinoforge.stores.local import LocalArtifactStore

    with EphemeralSession(enabled=True) as s:
        assert s.in_memory_ledger == {}
        assert s.in_memory_profiles == {}
        store = LocalArtifactStore(tmp_path)
        s.register_store(store, "run-1")
        assert (store, "run-1") in s._registered_stores


def test_capability_table_contents() -> None:
    """Pre-flight table contents match spec Appendix B.

    Would-fail-bug: ``("fal", None): True`` would let the CLI green-light
    an ephemeral run on a provider that cannot honour delete-on-completion.
    """
    assert EPHEMERAL_CAPABILITIES[("comfyui", "runpod")] is True
    assert EPHEMERAL_CAPABILITIES[("comfyui", "local")] is True
    assert EPHEMERAL_CAPABILITIES[("comfyui", "skypilot")] is True
    assert EPHEMERAL_CAPABILITIES[("diffusers", "runpod")] is True
    assert EPHEMERAL_CAPABILITIES[("diffusers", "local")] is True
    assert EPHEMERAL_CAPABILITIES[("diffusers", "skypilot")] is True
    assert EPHEMERAL_CAPABILITIES[("replicate", None)] is True
    assert EPHEMERAL_CAPABILITIES[("runway", None)] is True
    assert EPHEMERAL_CAPABILITIES[("fal", None)] is False
    assert EPHEMERAL_CAPABILITIES[("luma", None)] is False
    assert EPHEMERAL_CAPABILITIES[("hosted", None)] is False
    assert EPHEMERAL_CAPABILITIES[("fake", "local")] is True
    assert len(EPHEMERAL_CAPABILITIES) == 12


def test_policy_dataclass_fields_count() -> None:
    """Policy carries exactly 10 fields (spec §8.1).

    Would-fail-bug: a missed field added without a default would silently
    accept whatever the constructor passed and could regress a gate.
    """
    import dataclasses

    fields = dataclasses.fields(EphemeralPolicy)
    assert len(fields) == 10
    names = {f.name for f in fields}
    assert names == {
        "ledger_record",
        "profile_cache_persist",
        "batch_summary_write",
        "cost_sidecar_write",
        "heartbeat_ledger_touch",
        "delete_on_completion",
        "delete_retries",
        "memory_only_run_id",
        "pod_name_includes_alias",
        "force_debug_show_secrets_off",
    }
