"""kinoforge status --id renders a LoRA inventory section when present."""

from __future__ import annotations

from typing import Any

from kinoforge.cli._commands import _render_lora_inventory_section
from kinoforge.core.redaction import RedactionRegistry


def _inv(
    ref: str,
    *,
    size: int = 100,
    last_used: str = "2026-06-20T10:00:00-07:00",
    adapter: str = "lora_0",
) -> dict[str, Any]:
    return {
        "ref": ref,
        "filename": f"{ref}.s",
        "size_bytes": size,
        "loras_dir_path": f"/loras/{ref}.s",
        "downloaded_at_local": last_used,
        "last_used_at_local": last_used,
        "adapter_name": adapter,
    }


def test_empty_inventory_returns_none() -> None:
    """No inventory → no section emitted (clean omission, not 'loras: []').

    Bug: renderer prints an empty 'loras (0 resident, 0 used):' header on
    cold pods, padding every status output with noise.
    """
    assert _render_lora_inventory_section([], free_bytes=5000) is None
    assert _render_lora_inventory_section(None, free_bytes=5000) is None


def test_section_header_shows_count_used_and_free() -> None:
    """Header: '  loras (N resident, X used, Y free):'.

    Bug: header omits free_bytes, hiding disk-pressure context from the
    operator triaging a failing swap.
    """
    section = _render_lora_inventory_section(
        [_inv("A", size=2048), _inv("B", size=4096)], free_bytes=8 * 1024 * 1024
    )
    assert section is not None
    first = section.splitlines()[0]
    assert "loras (2 resident" in first
    assert "used" in first
    assert "free" in first


def test_section_header_omits_free_when_missing() -> None:
    """Optional free_bytes is omitted from the header when unknown.

    Bug: renderer prints 'free None' or crashes on the f-string interpolation
    when the snapshot field is missing.
    """
    section = _render_lora_inventory_section([_inv("A")], free_bytes=None)
    assert section is not None
    assert "free" not in section.splitlines()[0]


def test_one_line_per_lora_sorted_newest_used_first() -> None:
    """Rows are sorted newest-last-used first; each row carries ref + size +
    adapter.

    Bug: renderer sorts by ref name (alphabetical), hiding the recency
    signal that the matcher uses for LRU eviction decisions.
    """
    inv = [
        _inv("OLD", last_used="2026-06-20T08:00:00-07:00", adapter="lora_2"),
        _inv("NEW", last_used="2026-06-20T11:00:00-07:00", adapter="lora_0"),
        _inv("MID", last_used="2026-06-20T10:00:00-07:00", adapter="lora_1"),
    ]
    section = _render_lora_inventory_section(inv, free_bytes=1)
    assert section is not None
    body = section.splitlines()[1:]
    assert len(body) == 3
    # newest first
    assert "NEW" in body[0]
    assert "MID" in body[1]
    assert "OLD" in body[2]
    # adapter names present
    assert "lora_0" in body[0]


def test_refs_pass_through_redaction_registry() -> None:
    """Registered LoRA refs are rendered as their redaction token.

    Bug: renderer emits the raw vendor ref, leaking which models the
    operator is using to anyone reading the status output.
    """
    RedactionRegistry.instance().clear_session()
    RedactionRegistry.instance().add_many([("civitai:2197303@2474081", "lora:ref")])
    try:
        section = _render_lora_inventory_section(
            [_inv("civitai:2197303@2474081")], free_bytes=1
        )
        assert section is not None
        assert "civitai:2197303@2474081" not in section
        assert "<lora:ref" in section or "<lora:ref:" in section
    finally:
        RedactionRegistry.instance().clear_session()
