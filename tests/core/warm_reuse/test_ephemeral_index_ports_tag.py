"""An index row must carry the ports tag its provider needs to derive endpoints.

Found live 2026-09-11 on pod ``85ms24bc6kuc1m``, running U24's owed swap-group
proof. Cell 0 cold-booted fine; cell 1 then refused to attach::

    pod 85ms24bc6kuc1m has no endpoints: runpod.ensure_endpoints returned an
    empty map, given a seed carrying ports=['8000', '8001'] ... cannot
    --attach-pod.

Nothing was missing from the recording. ``EphemeralIndexRow.endpoints`` held
both URLs and ``_seed_instance_from_ledger_entry`` replayed them — that is what
``ports=['8000','8001']`` in the message is reporting. The gap is one step
later: ``--attach-pod`` calls ``ensure_endpoints``, the REPAIRING door, and
RunPod derives proxy URLs from ``instance.tags["ports"]``, which
``to_entry_dict`` does not emit. So the provider was asked to rebuild endpoints
without being told which ports to rebuild, returned ``{}``, and the attach door
refused — correctly, on the evidence it had.

Fixing it at the tag rather than by falling back to the recorded map is
deliberate: a RunPod proxy URL rebuilt from pod id + port is DERIVATION, not
recollection (the distinction U3 drew), so the repairing door keeps its meaning
and SkyPilot's dead ``127.0.0.1`` tunnels still get refused.
"""

from __future__ import annotations

from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndexRow


def _row(**over: object) -> EphemeralIndexRow:
    """Build an index row with sensible defaults.

    Args:
        **over: Field overrides.

    Returns:
        An ``EphemeralIndexRow``.
    """
    fields: dict[str, object] = {
        "id": "85ms24bc6kuc1m",
        "warm_attach_key": "a" * 16,
        "kinoforge_key": "b" * 12,
        "endpoints": {
            "8000": "https://85ms24bc6kuc1m-8000.proxy.runpod.net",
            "8001": "https://85ms24bc6kuc1m-8001.proxy.runpod.net",
        },
        "provider": "runpod",
        "created_at_local": "2026-09-11T21:57:10",
    }
    fields.update(over)
    return EphemeralIndexRow(**fields)  # type: ignore[arg-type]


class TestEntryDictCarriesPorts:
    """The entry dict must let a provider re-derive live endpoints."""

    def test_ports_tag_is_derived_from_the_recorded_endpoints(self) -> None:
        """``tags["ports"]`` names every recorded port, comma-separated.

        Catches exactly what shipped: no ``ports`` tag at all, so RunPod's
        ``ensure_endpoints`` had nothing to rebuild from and every
        ``--attach-pod`` under ``--ephemeral`` refused. The comma-separated
        shape is the provider's own parser contract (``ports_raw.split(",")``),
        not a format invented here.
        """
        entry = _row().to_entry_dict()

        assert entry["tags"]["ports"] == "8000,8001", entry["tags"]

    def test_existing_kinoforge_key_tag_survives(self) -> None:
        """Adding ports does not displace the tag that was already there.

        Catches replacing the tags dict wholesale: ``_scan_warm_candidates``
        reads ``tags.kinoforge_key``, so losing it would trade one broken
        attach path for another.
        """
        entry = _row().to_entry_dict()

        assert entry["tags"]["kinoforge_key"] == "b" * 12, entry["tags"]

    def test_row_with_no_endpoints_emits_no_ports_tag(self) -> None:
        """A row that recorded nothing claims nothing.

        Catches emitting ``"ports": ""``, which would hand the provider an
        empty-but-present field and turn a clear "nothing recorded" into a
        silently empty derivation. A mid-boot row legitimately has no
        endpoints yet (spec A2 writes the row BEFORE create returns).
        """
        entry = _row(endpoints={}).to_entry_dict()

        assert "ports" not in entry["tags"], entry["tags"]

    def test_runpod_can_actually_derive_endpoints_from_this_entry(self) -> None:
        """End-to-end: the real provider rebuilds URLs from the entry's tags.

        This is the assertion that matters. A string-equality test on
        ``"8000,8001"`` would still pass if the provider expected a different
        shape (a list, or space separation) — and that mismatch is precisely
        how the original bug presented: everything looked recorded, and the
        derivation still produced ``{}``. Driving the real
        ``RunPodProvider.endpoints`` is the only way to catch it.
        """
        from kinoforge.core.interfaces import Instance
        from kinoforge.providers.runpod import RunPodProvider

        entry = _row().to_entry_dict()
        inst = Instance(
            id=str(entry["id"]),
            provider="runpod",
            status="running",
            created_at=1757627830.0,
            tags=dict(entry["tags"]),
        )

        got = RunPodProvider().endpoints(inst)

        assert got == {
            "8000": "https://85ms24bc6kuc1m-8000.proxy.runpod.net",
            "8001": "https://85ms24bc6kuc1m-8001.proxy.runpod.net",
        }, got
