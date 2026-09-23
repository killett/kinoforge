"""P3 integration tests — CLI override flows through to set_stack wire body.

Spec §11.6.

Asserts end-to-end that ``--loras`` heredoc input survives
``parse_loras_heredoc`` → ``resolve_active_lora_stack`` → the wire body
:class:`~kinoforge.engines.diffusers.DiffusersBackend.set_lora_stack`
actually POSTs to the pod: ref, strength, and stack order must all come
through unchanged. The assertion goes through ``DiffusersBackend`` — the
one payload builder production uses — rather than a second, adapter-layer
builder that used to duplicate this conversion and silently dropped the
``branch``/``target`` routing field (removed in Task 8 of the H3
lora-shared-seam plan). The capture-seam pattern (inject ``http_post`` to
record the posted body) mirrors ``tests/engines/test_diffusers_set_lora_stack.py``.
"""

from __future__ import annotations

from typing import Any

from kinoforge.cli.loras_arg import parse_loras_heredoc
from kinoforge.core.interfaces import ModelProfile
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack
from kinoforge.engines.diffusers import DiffusersBackend


def _profile() -> ModelProfile:
    """Return a minimal ModelProfile for the test backend.

    Returns:
        A ``ModelProfile`` sufficient to construct ``DiffusersBackend``;
        no field here influences ``set_lora_stack``'s wire body.
    """
    return ModelProfile(
        name="wan-2.2",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1024, 1024),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


class _Cfg:
    loras: list[LoraEntry] = [LoraEntry(ref="civitai:cfg@1", strength=0.5)]


def test_end_to_end_cli_loras_override_cfg_drives_set_stack_request() -> None:
    """CLI override → resolver → DiffusersBackend.set_lora_stack → wire body."""
    cli = parse_loras_heredoc("civitai:1111@2222 0.7 h\ncivitai:3333@4444 1.2 l\n")
    active = resolve_active_lora_stack(_Cfg(), None, cli_loras=cli)

    captured: dict[str, Any] = {}

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"job_id": "s-e2e"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "done",
            "inventory": [],
            "free_bytes": 0,
            "swap_rejected": None,
            "error": None,
        }

    backend = DiffusersBackend(
        http_post=_post,
        http_get=_get,
        base_url="http://pod",
        probe_profile=_profile(),
        sleep=lambda s: None,
        poll_timeout_s=10.0,
        poll_interval_s=0.0,
    )
    backend.set_lora_stack(pod_id="pod-e2e", active_stack=active, download_specs={})

    target = captured["body"]["target"]
    assert len(target) == 2
    assert target[0]["ref"] == "civitai:1111@2222"
    assert target[0]["strength"] == 0.7
    assert target[1]["ref"] == "civitai:3333@4444"
    assert target[1]["strength"] == 1.2


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
