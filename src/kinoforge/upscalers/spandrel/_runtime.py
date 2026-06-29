"""SpandrelRuntime — frame-loop video upscale wrapper around the spandrel library.

spandrel is the architecture-agnostic super-resolution runtime used by
chaiNNer + ComfyUI. Loads RealESRGAN / ESRGAN / SwinIR / OmniSR / etc.
from .pth or .safetensors weights via auto-detection.

Used by the diffusers wan_t2v_server's LRU model registry (T9) — the
runtime instance lives inside ``_LOADED[name].pipe`` and is dispatched
by the ``spandrel-*`` model-name prefix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import numpy as np

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class SpandrelRuntime:
    """Loads a spandrel model and runs tiled per-frame video upscale.

    Args:
        weights_path: Local path to the weights file (.pth / .safetensors).
        precision: ``"fp16"`` or ``"fp32"``. fp16 halves VRAM on consumer GPUs
            but some architectures emit subtle artifacts; fp32 is the safe default.
        tile_size: Frame-tile dimension in pixels for VRAM headroom. spandrel
            handles tiling internally; this controls the max per-tile pixel count.
        batch_size: Frames per CUDA batch. Higher = better throughput, more VRAM.

    Raises:
        ImportError: ``spandrel`` package not installed.
    """

    def __init__(
        self,
        weights_path: Path,
        precision: Literal["fp16", "fp32"],
        tile_size: int,
        batch_size: int,
    ) -> None:
        """Lazy-import spandrel and load the weights from disk."""
        from spandrel import ModelLoader  # type: ignore[import-not-found]

        self._model = ModelLoader().load_from_file(str(weights_path))
        self._scale: float = float(self._model.scale)
        self._tile = tile_size
        self._batch = batch_size
        self._precision: Literal["fp16", "fp32"] = precision

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Decode, batch frames through model, re-encode mp4.

        Args:
            video_path: Local mp4 to upscale.
            scale: ``ScaleTarget``. Only ``kind="factor"`` supported in v1;
                ``"height"`` raises ``NotYetImplementedError``. ``scale.value``
                MUST match ``self._scale`` (declared by the weights).
            params: Reserved for engine overrides; ignored in v1.

        Returns:
            Path to the upscaled mp4 (sibling of input, ``<stem>.upscaled.mp4``).

        Raises:
            NotYetImplementedError: ``scale.kind == "height"``.
            UnsupportedScaleError: ``scale.value != self._scale``.
        """
        del params  # reserved for future engine overrides
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred; "
                "use --scale Nx for v1"
            )
        if scale.value != self._scale:
            raise UnsupportedScaleError(scale=scale, engine_name="spandrel")

        import imageio.v3 as iio
        import torch

        frames_in = iio.imread(video_path, plugin="FFMPEG")
        try:
            metadata = iio.immeta(video_path, plugin="FFMPEG")
            fps = float(metadata.get("fps", 16))
        except Exception:  # noqa: BLE001 — fall back to a sane default
            fps = 16.0

        device = (
            next(self._model.parameters()).device
            if hasattr(self._model, "parameters")
            else "cpu"
        )
        dtype = torch.float16 if self._precision == "fp16" else torch.float32

        out_frames: list[np.ndarray] = []
        for i in range(0, len(frames_in), self._batch):
            batch_np = frames_in[i : i + self._batch]
            batch_t = (
                torch.from_numpy(batch_np)
                .permute(0, 3, 1, 2)
                .to(device=device, dtype=dtype)
                / 255.0
            )
            with torch.no_grad():
                out_t = self._model(batch_t)
            out_np = (
                (out_t.clamp(0.0, 1.0) * 255.0)
                .to(torch.uint8)
                .permute(0, 2, 3, 1)
                .cpu()
                .numpy()
            )
            out_frames.extend(list(out_np))

        out_path = video_path.with_suffix(".upscaled.mp4")
        iio.imwrite(
            out_path,
            np.stack(out_frames),
            fps=fps,
            codec="libx264",
            macro_block_size=1,
        )
        return out_path

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between cuda/cpu."""
        self._model.to(device)
