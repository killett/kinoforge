"""Regression lock: every shipped example config produces a non-empty model
identity.

Bug this catches: a future YAML shape change (renamed field, moved block,
new engine type) silently strips identity for an example config, putting
``unknown`` back in the filename schema for the next live smoke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Trigger self-registration of every engine.
import kinoforge._adapters  # noqa: F401
from kinoforge.core import registry
from kinoforge.core.config import load_config

_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "configs"
_SKIP_YAMLS = {
    "local-fake.yaml",  # intentional fake; identity doesn't matter.
    # TODO(T7.6.4): these cfgs still carry scale=2x; renamed to -x4.yaml in Task 4.
    "upscale-flashvsr-x2.yaml",
    "wan-with-upscale-flashvsr.yaml",
}


def _collect_example_configs() -> list[Path]:
    return sorted(
        p
        for p in _EXAMPLE_DIR.glob("**/*.yaml")
        if p.name not in _SKIP_YAMLS and "manifests" not in p.parts
    )


@pytest.mark.parametrize(
    "config_path",
    _collect_example_configs(),
    ids=lambda p: p.name,
)
def test_example_config_produces_non_empty_model_identity(config_path: Path) -> None:
    """Every shipped example config yields a non-empty model identity.

    Args:
        config_path: Path to the example YAML under ``examples/configs/``.
    """
    cfg = load_config(str(config_path))

    if cfg.engine.kind == "fake":
        pytest.skip("fake engine — identity intentionally absent")

    try:
        engine_factory = registry.get_engine(cfg.engine.kind)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"engine {cfg.engine.kind!r} not registered — skip: {exc}")

    engine = engine_factory()
    identity = engine.model_identity(cfg.model_dump())
    assert identity, (
        f"{config_path.name}: engine {cfg.engine.kind!r} returned empty "
        f"model_identity — would surface as 'unknown' in sink filename"
    )
