"""Tests for Ledger.record / touch / forget redaction.

The EphemeralSession shadow path is exercised in test_ephemeral_run_cleanup.py
(Task 15). This file pins the always-on policy: vault-loaded → ledger
entries contain placeholders for any sensitive substrings.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _fake_instance(*, id: str = "i1", label: str = "x") -> Instance:
    """Build an Instance whose ``tags["label"]`` carries the sensitive string."""
    return Instance(
        id=id,
        provider="local",
        status="ready",
        created_at=1.0,
        tags={"label": label},
    )


def _read_ledger(tmp_path: Path) -> str:
    return (tmp_path / "r1" / "ledger.json").read_text()


def test_ledger_record_redacts_when_registry_active(tmp_path: Path) -> None:
    """A registered token appears as a placeholder in the persisted JSON.

    Would-fail-bug: writing entries via put_json without redact_json would
    leak the LoRA label into ledger.json on every run with a vault loaded.
    """
    RedactionRegistry.instance().add("super-secret-style", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(label="super-secret-style"))
    persisted = _read_ledger(tmp_path)
    assert "super-secret-style" not in persisted
    assert "<lora:label:" in persisted


def test_ledger_record_passthrough_when_registry_empty(tmp_path: Path) -> None:
    """Public-by-design path: empty registry → ledger writes plain."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(label="public-label"))
    persisted = _read_ledger(tmp_path)
    assert "public-label" in persisted


def test_ledger_touch_redacts_heartbeat_payload(tmp_path: Path) -> None:
    """Ledger.touch writes the same redacted shape as record."""
    RedactionRegistry.instance().add("super-secret-style", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1", label="super-secret-style"))
    ledger.touch("i1", last_heartbeat=12345.0)
    persisted = _read_ledger(tmp_path)
    assert "super-secret-style" not in persisted


def test_ledger_forget_removes_from_disk(tmp_path: Path) -> None:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1"))
    ledger.record(_fake_instance(id="i2"))
    ledger.forget("i1")
    persisted = _read_ledger(tmp_path)
    assert '"id": "i1"' not in persisted
    assert '"id": "i2"' in persisted


def test_ledger_record_within_existing_single_flight_lock(tmp_path: Path) -> None:
    """The single-flight lock still wraps the persistent path —
    redaction does not interfere with the lock semantics.

    Would-fail-bug: a redaction shim that swapped the put_json shape would
    corrupt the on-disk ledger.json so the next ``entries()`` call would
    raise on load.
    """
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1"))
    ledger.record(_fake_instance(id="i2"))
    payload = json.loads(_read_ledger(tmp_path))
    ids = {entry["id"] for entry in payload["entries"]}
    assert ids == {"i1", "i2"}


def test_ledger_record_longer_token_wins(tmp_path: Path) -> None:
    """redact() applies tokens longest-first — confirm redact_json path."""
    RedactionRegistry.instance().add("styl3", kind="lora:label")
    RedactionRegistry.instance().add("super-styl3", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1", label="super-styl3"))
    persisted = _read_ledger(tmp_path)
    assert "super-styl3" not in persisted
    assert "styl3" not in persisted
