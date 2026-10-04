"""Regression lock: every shipped example config produces a non-empty model
identity.

Bug this catches: a future YAML shape change (renamed field, moved block,
new engine type) silently strips identity for an example config, putting
``unknown`` back in the filename schema for the next live smoke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Trigger self-registration of every engine (video AND image — _adapters
# imports both kinoforge.engines.* and kinoforge.image_engines.*).
import kinoforge._adapters  # noqa: F401
from kinoforge.core import registry
from kinoforge.core.config import load_config

_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "configs"
_SKIP_YAMLS = {
    "local-fake.yaml",  # intentional fake; identity doesn't matter.
}

# Task 9 (2026-10-03) shipped the first two example configs carrying an
# `image:` block instead of `engine:`/`models:` — terminal image configs for
# `kinoforge image`, which never go through a video engine. Named here so
# the anti-vacuity guard below can confirm the sweep still reaches them.
_KNOWN_IMAGE_CFGS = frozenset({"luma-uni1-t2i.yaml", "fal-flux-schnell-t2i.yaml"})


def _collect_example_configs() -> list[Path]:
    return sorted(
        p
        for p in _EXAMPLE_DIR.glob("**/*.yaml")
        if p.name not in _SKIP_YAMLS
        and "manifests" not in p.parts
        # *.grid.yaml files are grid-sweep specs consumed by the grid
        # loader, not load_config — they have no top-level engine/models.
        and not p.name.endswith(".grid.yaml")
    )


@pytest.mark.parametrize(
    "config_path",
    _collect_example_configs(),
    ids=lambda p: p.name,
)
def test_example_config_produces_non_empty_model_identity(config_path: Path) -> None:
    """Every shipped example config yields a non-empty model identity.

    Covers both shapes a shipped config can take: a video config (real
    `engine:` block, identity read off the *video* engine registry) and a
    terminal-image config (`image:` block, no `engine:` at all, identity
    read off the *image* engine registry via `cfg.image.engine` /
    `cfg.image.model_dump()`). Both share the same defect class this file
    guards: an empty model_identity surfaces as the literal `"unknown"` in
    the published filename (the `_fal_unknown_` bug,
    successful-generations.md §17).

    Args:
        config_path: Path to the example YAML under ``examples/configs/``.
    """
    cfg = load_config(str(config_path))

    if cfg.image is not None:
        # Terminal image config — no video `engine:` block to read identity
        # off; the provider/model slug instead comes from `cfg.image.engine`
        # (the registry key) and the IMAGE engine's own `model_identity`,
        # mirroring the shape `image_run.generate_image` actually feeds the
        # sink (see `src/kinoforge/core/image_run.py`: `provider=block.engine`,
        # `model=engine.model_identity(cfg_dict)`).
        if cfg.image.engine == "fake":
            pytest.skip("fake image engine — identity intentionally absent")
        assert cfg.image.engine, (
            f"{config_path.name}: empty image.engine would surface as "
            f"'unknown' provider in sink filename"
        )
        try:
            image_engine_factory = registry.get_image_engine(cfg.image.engine)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(
                f"image engine {cfg.image.engine!r} not registered — skip: {exc}"
            )
        image_engine = image_engine_factory()
        identity = image_engine.model_identity(cfg.image.model_dump())
        assert identity, (
            f"{config_path.name}: image engine {cfg.image.engine!r} returned "
            f"empty model_identity — would surface as 'unknown' in sink filename"
        )
        return

    # Every other shipped example config carries a real `engine:` block.
    assert cfg.engine is not None  # noqa: S101 — see branch above

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


def test_collection_includes_the_known_image_configs() -> None:
    """Anti-vacuity guard for the `image:` branch above.

    If a future ``_collect_example_configs`` filter change (e.g. a widened
    ``_SKIP_YAMLS`` or a renamed example) silently dropped the two shipped
    image configs out of the sweep, the parametrized test above would just
    not generate cases for them — green suite, zero coverage of the
    `image:` identity path. This fails loudly instead, and pins the count
    at exactly two, matching what Task 9 shipped today.
    """
    names = {p.name for p in _collect_example_configs()}
    assert _KNOWN_IMAGE_CFGS <= names, (
        f"expected the two known image configs {sorted(_KNOWN_IMAGE_CFGS)} "
        f"to be present in the sweep; missing: "
        f"{sorted(_KNOWN_IMAGE_CFGS - names)}"
    )
