"""Behavior: an unserveable LoRA stack is refused at config load.

Each case here is money: the failure it prevents otherwise surfaces after an
H200 has booked, or not at all (a video generated without the LoRA).
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.checks.loras import LoraServerSupportCheck
from kinoforge.validation.protocol import Severity


def _cfg(server_module: str, loras: list[dict[str, str]]) -> Config:
    """Build the minimum diffusers cfg that validates, for a given server + loras.

    Args:
        server_module: Dotted module the ``-m`` server_cmd launches.
        loras: Raw ``loras:`` block entries.

    Returns:
        A validated :class:`Config`.
    """
    return Config.model_validate(
        {
            "mode": "t2v",
            "engine": {
                "kind": "diffusers",
                "precision": "fp8",
                "diffusers": {
                    "server_cmd": ["python", "-m", server_module],
                    "base_url": "http://localhost:8000",
                },
            },
            "models": [
                {"ref": "hf:org/repo", "kind": "base", "target": "diffusion_models"}
            ],
            "loras": loras,
        }
    )


def test_unregistered_server_with_loras_is_an_error() -> None:
    """A stack aimed at a server with no LoRA surface fails before spend."""
    cfg = _cfg("pkg.mod.some_new_server", [{"ref": "civitai:1@2"}])
    check = LoraServerSupportCheck()
    assert check.applies_to(cfg) is True
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert "some_new_server" in result.message


def test_wan_target_on_h3_server_is_an_error() -> None:
    """Wan's vocabulary on H3 is refused, and the message names H3's targets."""
    cfg = _cfg(
        "kinoforge.engines.diffusers.servers.minimax_h3_server",
        [{"ref": "hf:o/r:f.safetensors", "target": "high_noise"}],
    )
    result = LoraServerSupportCheck().run(cfg)
    assert result.passed is False
    assert "transformer" in result.message


def test_valid_h3_stack_passes() -> None:
    """A legal H3 stack is not obstructed."""
    cfg = _cfg(
        "kinoforge.engines.diffusers.servers.minimax_h3_server",
        [{"ref": "hf:o/r:f.safetensors", "target": "transformer"}],
    )
    assert LoraServerSupportCheck().run(cfg).passed is True


def test_empty_stack_does_not_apply() -> None:
    """No LoRAs, no opinion — the check must not fire on ordinary configs."""
    cfg = _cfg("pkg.mod.some_new_server", [])
    assert LoraServerSupportCheck().applies_to(cfg) is False
