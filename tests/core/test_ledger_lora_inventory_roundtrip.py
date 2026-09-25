"""Behavior: a persisted ``lora_inventory`` ref must survive the ledger round trip.

U54. ``Ledger._write_entries`` runs ``RedactionRegistry.redact_json()`` over the
WHOLE payload before every disk write. Separately, ``_register_observed_lora_refs``
registers every observed LoRA ref under the ``lora:ref`` kind — unconditionally,
not vault-gated — immediately before ``ledger.touch(..., lora_inventory=...)``,
so those refs become live tokens. The same write that persists the inventory then
substituted each ref with a ``<lora:ref:…>`` placeholder **in the persisted data
itself**, not just in logs.

Redaction is one-way: ``_read_entries_from_disk`` does not reverse it. So a FRESH
process reading a row a PRIOR process wrote sees placeholders, and
``warm_reuse/matcher.py`` compares them against raw cfg refs from
``resolve_active_lora_stack``. Every ref mismatches, so the matcher plans to
re-download everything already on the pod and evict everything it should keep —
and the eviction it plans names a placeholder, which no pod can act on.

**The decision this pins, stated plainly.** One-way redaction of a field the
system must READ BACK is a bug independent of confidentiality policy: the field
becomes useless, and a corrupted value is not a privacy win over an absent one.
Where an operator genuinely needs refs off disk the existing mechanism is
``--ephemeral``, whose ``policy.ledger_record=False`` skips the write entirely
(``_write_entries`` returns before ``redact_json`` is ever reached). So the
exemption is scoped to ``lora_inventory.*.ref`` — the one field that must round
trip — and NOT to the inventory block as a whole, because an operator-authored
``label`` alongside it has no round-trip requirement and stays redacted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore

_REF = "hf:lightx2v/Minimax-h3-Turbo:minimax_h3_fl2v_turbo_8step_v1.0_bf16.safetensors"
_SECRET_PROMPT = "a closely held prompt the operator vaulted"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _ledger(tmp_path: Path) -> Ledger:
    return Ledger(store=LocalArtifactStore(root=tmp_path), run_id="r1")


def _seed(tmp_path: Path, inventory: list[dict[str, object]]) -> Ledger:
    """Record one instance and attach *inventory* to it, as the swap paths do."""
    ledger = _ledger(tmp_path)
    ledger.record(
        Instance(id="pod1", provider="runpod", status="ready", created_at=1.0),
        idle_timeout_s=600,
        max_age_s=3600,
    )
    ledger.touch("pod1", lora_inventory=inventory)
    return ledger


def _persisted(tmp_path: Path) -> dict[str, object]:
    raw = json.loads((tmp_path / "r1" / "ledger.json").read_text())
    return next(e for e in raw["entries"] if e["id"] == "pod1")


def test_a_registered_lora_ref_survives_the_ledger_round_trip(tmp_path: Path) -> None:
    """The defect itself, at the layer that persists it.

    Bug caught: ``redact_json`` walking into ``lora_inventory[*].ref``. With the
    ref registered — which ``_register_observed_lora_refs`` does on any run that
    has LoRAs, vault or not — the persisted value becomes ``<lora:ref:…>`` and
    the next process's matcher can neither match it nor evict by it.
    """
    RedactionRegistry.instance().add(_REF, kind="lora:ref")

    _seed(tmp_path, [{"ref": _REF, "strength": 1.0}])

    assert _persisted(tmp_path)["lora_inventory"] == [{"ref": _REF, "strength": 1.0}]


def test_the_reader_sees_the_same_ref_a_fresh_process_would(tmp_path: Path) -> None:
    """Cross-process is the shape that actually bites.

    Bug caught: a fix applied only to the in-memory copy. The matcher's
    ledger path (`matcher.py`: ``entry.get("lora_inventory")``) reads what a
    PRIOR process wrote to disk, so the assertion has to come from a ledger
    built fresh over the same store rather than from the writer's own cache.
    """
    RedactionRegistry.instance().add(_REF, kind="lora:ref")
    _seed(tmp_path, [{"ref": _REF, "strength": 1.0}])

    reader = _ledger(tmp_path)
    row = reader.read("pod1")

    assert row is not None
    assert [e["ref"] for e in row["lora_inventory"]] == [_REF]


def test_a_vaulted_prompt_is_still_redacted_in_the_persisted_ledger(
    tmp_path: Path,
) -> None:
    """Negative control — the ledger's redaction must not be disabled wholesale.

    Bug caught: dropping the ``redact_json`` call, or exempting the whole
    payload. The prompt is what ledger redaction exists for; a fix that buys
    ref fidelity by spilling it is strictly worse than the defect.
    """
    RedactionRegistry.instance().add(_SECRET_PROMPT, kind="prompt:positive")
    ledger = _ledger(tmp_path)
    ledger.record(
        Instance(
            id="pod1",
            provider="runpod",
            status="ready",
            created_at=1.0,
            tags={"label": _SECRET_PROMPT},
        ),
        idle_timeout_s=600,
        max_age_s=3600,
    )

    assert _SECRET_PROMPT not in (tmp_path / "r1" / "ledger.json").read_text()


def test_a_label_inside_the_inventory_is_still_redacted(tmp_path: Path) -> None:
    """The exemption is scoped to ``ref``, not to the inventory block.

    Bug caught: exempting ``lora_inventory`` wholesale. A ``label`` is
    operator-authored text with no round-trip requirement — nothing compares
    or evicts by it — so it has no claim on the exemption, and sweeping it in
    would widen a confidentiality carve-out further than its justification
    reaches.
    """
    RedactionRegistry.instance().add(_SECRET_PROMPT, kind="lora:label")

    _seed(tmp_path, [{"ref": _REF, "label": _SECRET_PROMPT}])

    assert _SECRET_PROMPT not in (tmp_path / "r1" / "ledger.json").read_text()


def test_a_ref_key_outside_the_inventory_is_still_redacted(tmp_path: Path) -> None:
    """The exemption is a PATH, not a key name.

    Bug caught: matching the leaf key ``ref`` anywhere in the payload. The
    ledger carries other structures, and a future one holding a ``ref`` field
    would silently inherit a carve-out nobody reasoned about.
    """
    RedactionRegistry.instance().add(_SECRET_PROMPT, kind="prompt:positive")
    ledger = _ledger(tmp_path)
    ledger.record(
        Instance(
            id="pod1",
            provider="runpod",
            status="ready",
            created_at=1.0,
            tags={"ref": _SECRET_PROMPT},
        ),
        idle_timeout_s=600,
        max_age_s=3600,
    )

    assert _SECRET_PROMPT not in (tmp_path / "r1" / "ledger.json").read_text()
