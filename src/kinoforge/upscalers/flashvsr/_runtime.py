"""FlashVSRRuntime — streaming diffusion VSR wrapper around StreamingDMDPipeline.

Lazy-imports ``flashvsr.pipeline`` so the kinoforge-default env doesn't
need FlashVSR deps installed. Satisfies the LRU LoadedModel contract used
by wan_t2v_server's model registry via the ``flashvsr-*`` slug prefix
(T5 server dispatch delta).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_log = logging.getLogger(__name__)


class FlashVSRRuntime:
    """Loads FlashVSR weights + runs streaming VSR through wan_t2v_server.

    Args:
        weights_dir: Local dir holding the 2-file (lite) or 4-file
            (long-video) bundle downloaded via ``_fetch_weights``.
        precision: ``"fp16"`` (DMD-native) or ``"fp32"``.
        window_size: Streaming attention window (frames).
        tile_size: ``0`` = whole-frame; ``>0`` = spatial tiling for VRAM headroom.
        long_video_mode: ``True`` enables LCSA + TCDecoder (needs 4-file bundle).

    Raises:
        ImportError: ``flashvsr`` package not installed in the current env.
    """

    def __init__(
        self,
        weights_dir: Path,
        precision: Literal["fp16", "fp32"],
        window_size: int,
        tile_size: int,
        long_video_mode: bool,
    ) -> None:
        """Lazy-import flashvsr + torch, load weights via ``from_pretrained``."""
        import torch
        from flashvsr.pipeline import (  # type: ignore[import-not-found]
            StreamingDMDPipeline,
        )

        dtype = torch.float16 if precision == "fp16" else torch.float32
        self._pipe = StreamingDMDPipeline.from_pretrained(
            str(weights_dir),
            torch_dtype=dtype,
            enable_lcsa=long_video_mode,
        )
        self._window = window_size
        self._tile = tile_size
        # Native scale from checkpoint. Attribute name pinned by
        # StreamingDMDPipeline; if upstream renames to `.upscale_factor`
        # adjust here and update the test stub.
        self._native_scale: float = float(self._pipe.scale)

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Run streaming VSR on ``video_path``; return sibling ``.flashvsr.mp4``."""
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr: height-target scale ({int(scale.value)}p) not yet "
                "wired; use --scale Nx"
            )
        if scale.value != self._native_scale:
            raise UnsupportedScaleError(scale=scale, engine_name="flashvsr")
        if params.get("prompt"):
            _log.warning(
                "flashvsr: params['prompt'] ignored — model has no text encoder"
            )
        out = video_path.with_suffix(".flashvsr.mp4")
        self._pipe.stream_upscale(
            input_path=str(video_path),
            output_path=str(out),
            window_size=self._window,
            tile=self._tile or None,
        )
        return out

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between cuda/cpu."""
        self._pipe.to(device)

    @property
    def vram_bytes(self) -> int:
        """Wan 2.1 1.3B backbone fp16 ≈ 2.6 GB + streaming state ≈ 4-8 GB peak."""
        return int(8 * 1024**3)
