"""Tests for UpscaleConfig.chunk_frames / chunk_overlap."""

from __future__ import annotations

import pytest

from kinoforge.core.config import SpandrelEngineConfig, UpscaleConfig
from kinoforge.core.errors import ConfigError


def _spandrel_block() -> SpandrelEngineConfig:
    return SpandrelEngineConfig(
        model_url="hf:foo/bar.pth",
        arch="realesrgan",
        precision="fp16",
        tile_size=512,
        batch_size=4,
    )


def test_chunking_is_off_by_default() -> None:
    # Bug caught: a default chunk_frames turns every proven single-call
    # upscale into a split + re-encode path nobody asked for.
    cfg = UpscaleConfig(engine="spandrel", scale="2x", spandrel=_spandrel_block())
    assert cfg.chunk_frames is None
    assert cfg.chunk_overlap == 8


def test_chunk_frames_and_overlap_round_trip() -> None:
    # Bug caught: the fields are declared but dropped by an extra="ignore"
    # or shadowed by a validator that resets them.
    cfg = UpscaleConfig(
        engine="spandrel",
        scale="2x",
        spandrel=_spandrel_block(),
        chunk_frames=69,
        chunk_overlap=8,
    )
    assert (cfg.chunk_frames, cfg.chunk_overlap) == (69, 8)


@pytest.mark.parametrize(
    ("chunk_frames", "chunk_overlap"),
    [(0, 8), (-1, 8), (8, 8), (8, 9), (8, -1)],
)
def test_degenerate_chunking_is_refused_at_config_time(
    chunk_frames: int, chunk_overlap: int
) -> None:
    # Bug caught: overlap >= chunk_frames (every chunk's kept range empty)
    # accepted here and only refused by the planner after the pod is booked.
    with pytest.raises(ConfigError):
        UpscaleConfig(
            engine="spandrel",
            scale="2x",
            spandrel=_spandrel_block(),
            chunk_frames=chunk_frames,
            chunk_overlap=chunk_overlap,
        )
