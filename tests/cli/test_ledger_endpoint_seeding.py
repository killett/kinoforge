"""Seeding an Instance from its ledger row — the shared U3/U15 helper.

``provider.get_instance()`` returns a THIN instance: it is built from whatever
the provider's list/query API happens to return, which on Modal is
``modal app list`` (no URL at all) and on RunPod is a pod query whose selection
set omits the port spec. The create-time values live in the ledger row.

``_resolve_attach_pod`` learned to merge them back (U15, ``ccd4c5e7``); U3 is
the same gap on the read paths (``status``, ``pod lora ls``), so the merge is
now a shared helper. The four tests in ``test_resolve_attach_pod.py`` cover
precedence and the tag merge through that caller and are deliberately NOT
duplicated here — their staying green unmodified is what proves the extraction
changed no behaviour. What they do not cover is a ledger row that is not shaped
the way the writer intended, which is what this module adds.
"""

from __future__ import annotations

from typing import Any

from kinoforge.cli._commands import _seed_instance_from_ledger_entry
from kinoforge.core.interfaces import Instance


def _thin_instance() -> Instance:
    """An Instance shaped like ``get_instance`` returns one: no create-time data."""
    return Instance(
        id="pod-1",
        provider="modal",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )


def test_seeding_tolerates_a_ledger_row_of_the_wrong_shape() -> None:
    """A malformed row leaves the instance alone instead of raising.

    Bug caught: a traceback out of ``kinoforge status`` or ``pod lora ls`` on a
    hand-edited, truncated, or older-schema ledger row — ``tags`` a string,
    ``endpoints`` null. Both commands are what an operator reaches for while a
    pod is billing and something is already wrong, so the failure mode is a
    crash at exactly the moment the tool has to work. A bare ``dict(...)`` or
    ``.items()`` on either value raises ``TypeError`` / ``ValueError``.
    """
    inst = _thin_instance()
    entry: dict[str, Any] = {"id": "pod-1", "tags": "not-a-dict", "endpoints": None}

    _seed_instance_from_ledger_entry(inst, entry)

    assert inst.tags == {}
    assert inst.endpoints == {}


def test_seeding_stringifies_recorded_endpoint_keys_and_values() -> None:
    """Port keys survive a JSON round-trip as strings, and are used as such.

    A ledger row is JSON on disk, but it is also written in-process, where a
    port can arrive as an ``int``. Every consumer indexes the endpoint map with
    a string (``endpoints_map.get("8000")`` in ``pod lora ls``, ``"8000"`` in
    the util resolvers).

    Bug caught: an ``{8000: url}`` row seeding an int-keyed map, so
    ``.get("8000")`` misses and ``pod lora ls`` reports "no endpoint URL" for a
    pod whose URL it is holding — U3's symptom surviving U3's fix, for one
    row shape.
    """
    inst = _thin_instance()
    entry: dict[str, Any] = {
        "id": "pod-1",
        "endpoints": {8000: "https://x.modal.run"},
    }

    _seed_instance_from_ledger_entry(inst, entry)

    assert inst.endpoints == {"8000": "https://x.modal.run"}
    assert inst.endpoints.get("8000") == "https://x.modal.run"


def test_seeding_a_row_with_no_recorded_fields_is_a_no_op() -> None:
    """An entry carrying neither key leaves both maps empty.

    Bug caught: a helper that writes ``{}`` over live values it was given, or
    that inserts placeholder keys. The empty map is load-bearing downstream —
    ``_render_endpoints_for_status`` distinguishes "no live endpoint" from a
    recorded one, and ``_resolve_attach_pod`` REFUSES on an empty post-ensure
    map rather than falling back. A helper that manufactured a key would turn
    that refusal into a booked pod plus a connection error.
    """
    inst = _thin_instance()
    inst.endpoints = {"8000": "https://live.modal.run"}

    _seed_instance_from_ledger_entry(inst, {"id": "pod-1"})

    assert inst.endpoints == {"8000": "https://live.modal.run"}
    assert inst.tags == {}
