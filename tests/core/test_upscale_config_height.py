"""UpscaleConfig now accepts a height-target scale for flashvsr."""

import pytest

from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig
from kinoforge.core.errors import ConfigError


def _cfg(scale: str) -> UpscaleConfig:
    return UpscaleConfig(
        engine="flashvsr",
        scale=scale,
        flashvsr=FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1"),
    )


@pytest.mark.parametrize("scale", ["1080p", "720p", "4x"])
def test_height_and_4x_accepted(scale: str) -> None:
    # Behaviour: height targets + the native 4x factor pass. Bug caught: the old
    # cfg-time refusal of the height branch still firing.
    assert _cfg(scale).scale == scale


def test_non_4x_factor_still_refused() -> None:
    # Behaviour: flashvsr is 4x-native; a 3x factor is nonsense. Bug caught:
    # accidentally widening the relax to allow arbitrary factors.
    with pytest.raises(ConfigError, match="4x"):
        _cfg("3x")
