"""P3 integration tests — CLI override flows through to set_stack wire body.

Spec §11.6.
"""

from __future__ import annotations

from kinoforge._adapters import build_set_stack_request
from kinoforge.cli.loras_arg import parse_loras_heredoc
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack


class _Cfg:
    loras: list[LoraEntry] = [LoraEntry(ref="civitai:cfg@1", strength=0.5)]


def test_end_to_end_cli_loras_override_cfg_drives_set_stack_request() -> None:
    """CLI override → resolver → build_set_stack_request → wire body."""
    cli = parse_loras_heredoc("civitai:1111@2222 0.7 h\ncivitai:3333@4444 1.2 l\n")
    active = resolve_active_lora_stack(_Cfg(), None, cli_loras=cli)

    request = build_set_stack_request(active, download_specs={})

    assert len(request.target) == 2
    assert request.target[0].ref == "civitai:1111@2222"
    assert request.target[0].strength == 0.7
    assert request.target[1].ref == "civitai:3333@4444"
    assert request.target[1].strength == 1.2


def test_cli_loras_capability_key_derivation_uses_cli_refs_not_cfg_refs() -> None:
    """Resolved stack refs derive from CLI input, not cfg."""
    cli = parse_loras_heredoc("civitai:cli@1 1.0 h\ncivitai:cli@2 1.0 l\n")
    active = resolve_active_lora_stack(_Cfg(), None, cli_loras=cli)
    assert [lo.ref for lo in active] == ["civitai:cli@1", "civitai:cli@2"]


def test_cli_loras_warm_attach_swap_succeeds_when_only_lora_stack_differs() -> None:
    """Warm pod with different active stack → is_stack_match False → set_stack swap."""
    from kinoforge.core.warm_reuse.matcher import is_stack_match

    class _ActiveEntry:
        def __init__(self, ref: str, branch: str, strength: float) -> None:
            self.ref = ref
            self.branch = branch
            self.last_strength = strength

    active = [_ActiveEntry("civitai:old@1", "high_noise", 1.0)]
    target = parse_loras_heredoc("civitai:new@1 1.0 h\n")

    assert not is_stack_match(active, target)
