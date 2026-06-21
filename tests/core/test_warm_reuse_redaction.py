"""warm_reuse.redaction._register_observed_lora_refs."""

from __future__ import annotations

import pytest

from kinoforge.core.redaction import RedactionRegistry
from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Wipe the process-wide registry between tests so kinds don't leak."""
    RedactionRegistry.instance().clear_session()


def test_registers_each_inventory_ref() -> None:
    """Bug: helper iterates wrong attribute (e.g. .refs instead of .inventory),
    silently registering nothing."""
    snap = {
        "inventory": [
            {"ref": "civitai:A@1"},
            {"ref": "civitai:B@2"},
        ]
    }
    _register_observed_lora_refs(snap)
    out = RedactionRegistry.instance().redact("downloading civitai:A@1 to /loras")
    assert "civitai:A@1" not in out
    out2 = RedactionRegistry.instance().redact("downloading civitai:B@2 to /loras")
    assert "civitai:B@2" not in out2


def test_idempotent() -> None:
    """Bug: helper appends duplicates, blowing up the registry size."""
    snap = {"inventory": [{"ref": "civitai:A@1"}]}
    _register_observed_lora_refs(snap)
    _register_observed_lora_refs(snap)
    out = RedactionRegistry.instance().redact("civitai:A@1")
    assert "civitai:A@1" not in out


def test_empty_inventory_noop() -> None:
    """Bug: helper raises on empty/missing inventory instead of returning."""
    _register_observed_lora_refs({"inventory": []})
    out = RedactionRegistry.instance().redact("civitai:A@1")
    assert "civitai:A@1" in out


def test_short_ref_is_skipped_not_raised() -> None:
    """Refs shorter than the registry's MIN_TOKEN_LEN (4) silently skip.

    Bug: helper forwards a 1-3 char ref straight to RedactionRegistry.add
    which raises ``ValueError: redaction token must be at least 4 chars``.
    A malformed pod response (or fixture data) then crashes the matcher
    + integration helper instead of degrading.
    """
    snap = {
        "inventory": [
            {"ref": "A"},
            {"ref": "civitai:long@1"},
            {"ref": "BC"},
        ]
    }
    _register_observed_lora_refs(snap)
    out = RedactionRegistry.instance().redact("civitai:long@1 A BC")
    assert "civitai:long@1" not in out
    # Short refs pass through unchanged — registry never tokenised them.
    assert "A" in out
    assert "BC" in out


def test_accepts_object_attribute_shape() -> None:
    """Bug: helper only handles dict-shaped snapshots, breaking when the
    /lora/inventory response object is passed in directly (it has a
    .inventory attribute, not a key)."""

    class _Entry:
        def __init__(self, ref: str) -> None:
            self.ref = ref

    class _Snap:
        inventory = [_Entry("civitai:C@3")]

    _register_observed_lora_refs(_Snap())
    out = RedactionRegistry.instance().redact("civitai:C@3")
    assert "civitai:C@3" not in out
