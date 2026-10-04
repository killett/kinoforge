"""SpandrelRuntime — video (per-frame) and still-image upscale around the spandrel library.

spandrel is the architecture-agnostic super-resolution runtime used by
chaiNNer + ComfyUI. Loads RealESRGAN / ESRGAN / SwinIR / OmniSR / etc.
from .pth or .safetensors weights via auto-detection.

Used by the diffusers wan_t2v_server's LRU model registry (T9) — the
runtime instance lives inside ``_LOADED[name].pipe`` and is dispatched
by the ``spandrel-*`` model-name prefix.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_log = logging.getLogger("kinoforge.upscalers.spandrel.runtime")

_TILE_OVERLAP = 32
"""Pixels of context around each still-image tile; discarded on assembly."""


def tile_upscale(
    img: np.ndarray,
    *,
    scale: int,
    tile: int,
    overlap: int,
    infer: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    """Upscale a uint8 HxWx3 still by *scale*, tile by tile, through *infer*.

    Each tile is padded by *overlap* on every interior edge (clamped at the
    image border, never zero-padded), inferred alone as a 1-image NHWC batch,
    and only the tile's own area — at ``scale`` times its geometry — is copied
    into the output canvas. ``tile <= 0`` or an image no larger than one tile
    goes through in a single call.

    Pure numpy, so it is testable without torch; the runtime passes a bound
    ``_infer`` as *infer*.

    Args:
        img: Input image, ``(H, W, 3)`` uint8.
        scale: Integer factor the model multiplies each dimension by.
        tile: Tile edge in input pixels; ``0`` means whole image.
        overlap: Context pixels around each tile.
        infer: ``(N, h, w, 3) uint8 -> (N, h*scale, w*scale, 3) uint8``.

    Returns:
        The upscaled image, ``(H*scale, W*scale, 3)`` uint8.
    """
    h, w, _ = img.shape
    if tile <= 0 or (h <= tile and w <= tile):
        return infer(img[None])[0]  # type: ignore[no-any-return]
    out = np.zeros((h * scale, w * scale, 3), dtype=np.uint8)
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
            py0, px0 = max(y0 - overlap, 0), max(x0 - overlap, 0)
            py1, px1 = min(y1 + overlap, h), min(x1 + overlap, w)
            patch = infer(np.ascontiguousarray(img[py0:py1, px0:px1])[None])[0]
            iy0, ix0 = (y0 - py0) * scale, (x0 - px0) * scale
            out[y0 * scale : y1 * scale, x0 * scale : x1 * scale] = patch[
                iy0 : iy0 + (y1 - y0) * scale, ix0 : ix0 + (x1 - x0) * scale
            ]
    return out


def _to_rgb(img: np.ndarray, source: Path) -> np.ndarray:
    """Return *img* as uint8 HxWx3: broadcast greyscale, drop alpha (logged).

    Raises:
        ValueError: Channel count other than 1 (2-D), 3 or 4.
    """
    if img.ndim == 2:
        return np.repeat(img[:, :, None], 3, axis=2)
    if img.ndim == 3 and img.shape[2] == 4:
        _log.info("dropping alpha channel of %s before upscale", source.name)
        return np.ascontiguousarray(img[:, :, :3])
    if img.ndim == 3 and img.shape[2] == 3:
        return img
    raise ValueError(
        f"{source.name}: expected a greyscale, RGB or RGBA image, got shape {img.shape}"
    )


class SpandrelRuntime:
    """Loads a spandrel model; upscales video per frame or a still image tiled.

    Two public entry points share one inference core (``_infer``):

    * :meth:`upscale` — the live-proven video path. Frames are batched
      through the model whole (NOT tiled) and re-encoded to mp4.
    * :meth:`upscale_image` — a still image, cut by :func:`tile_upscale` into
      ``tile_size`` squares with :data:`_TILE_OVERLAP` px of context and
      written as ``<stem>.upscaled.png``.

    Args:
        weights_path: Local path to the weights file (.pth / .safetensors).
        precision: ``"fp16"`` or ``"fp32"``. fp16 halves VRAM on consumer GPUs
            but some architectures emit subtle artifacts; fp32 is the safe default.
        tile_size: Tile edge in pixels for the STILL-IMAGE path; ``0`` means
            whole-image. The video path ignores it (frames are small).
        batch_size: Frames per CUDA batch on the video path.

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

    # ------------------------------------------------------------------ video

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
        self._check_scale(scale)

        import imageio.v3 as iio

        frames_in = iio.imread(video_path, plugin="FFMPEG")
        try:
            metadata = iio.immeta(video_path, plugin="FFMPEG")
            fps = float(metadata.get("fps", 16))
        except Exception:  # noqa: BLE001 — fall back to a sane default
            fps = 16.0

        device, dtype = self._place_model()
        out_frames: list[np.ndarray] = []
        for i in range(0, len(frames_in), self._batch):
            out_frames.extend(
                list(self._infer(frames_in[i : i + self._batch], device, dtype))
            )

        out_path = video_path.with_suffix(".upscaled.mp4")
        iio.imwrite(
            out_path,
            np.stack(out_frames),
            fps=fps,
            codec="libx264",
            macro_block_size=1,
        )
        return out_path

    # ------------------------------------------------------------------ image

    def upscale_image(
        self, image_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Upscale one still image; write ``<stem>.upscaled.png`` beside it.

        Args:
            image_path: Local PNG or JPEG.
            scale: Same contract as :meth:`upscale` — factor only, must match
                the weights' declared scale.
            params: Reserved; ignored in v1.

        Returns:
            Path to the upscaled PNG (always RGB, lossless).

        Raises:
            NotYetImplementedError: ``scale.kind == "height"``.
            UnsupportedScaleError: ``scale.value != self._scale``.
            ValueError: The image has a channel count other than 1, 3 or 4.
        """
        del params
        self._check_scale(scale)

        import imageio.v3 as iio

        img = _to_rgb(np.asarray(iio.imread(image_path)), image_path)
        device, dtype = self._place_model()
        out = tile_upscale(
            img,
            scale=int(round(self._scale)),
            tile=self._tile,
            overlap=_TILE_OVERLAP,
            infer=lambda batch: self._infer(batch, device, dtype),
        )
        out_path = image_path.with_suffix(".upscaled.png")
        iio.imwrite(out_path, out)
        return out_path

    # ------------------------------------------------------------------ shared

    def _check_scale(self, scale: ScaleTarget) -> None:
        """Refuse height targets and factors the loaded weights do not provide."""
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred; "
                "use --scale Nx for v1"
            )
        if scale.value != self._scale:
            raise UnsupportedScaleError(scale=scale, engine_name="spandrel")

    def _place_model(self) -> tuple[Any, Any]:
        """Move the raw nn.Module to (device, dtype) atomically; return both.

        Spandrel's ``ModelLoader().load_from_file`` returns an
        ``ImageModelDescriptor`` whose ``.to()`` is ambiguous — a dtype-only
        cast can silently drop the device back to CPU on some versions,
        producing "Expected all tensors on same device, but found cpu and
        cuda:0" at the first conv2d. Unwrap to the raw nn.Module and issue a
        single combined ``.to(device=..., dtype=...)`` so both settings land
        atomically without descriptor round-tripping.
        """
        import torch

        raw_model = getattr(self._model, "model", self._model)
        device = (
            next(raw_model.parameters()).device
            if hasattr(raw_model, "parameters")
            else "cpu"
        )
        # Prefer cuda if available and model was ever moved off cpu; otherwise
        # honor the parameter-inferred device. This defends against a
        # descriptor.to("cuda") that didn't actually move the wrapped module.
        if torch.cuda.is_available() and str(device) == "cpu":
            device = torch.device("cuda")
        dtype = torch.float16 if self._precision == "fp16" else torch.float32
        raw_model.to(device=device, dtype=dtype)
        return device, dtype

    def _infer(
        self,
        batch_np: np.ndarray,
        device: Any,  # noqa: ANN401
        dtype: Any,  # noqa: ANN401
    ) -> np.ndarray:
        """Run one uint8 NHWC batch through the model; return uint8 NHWC."""
        import torch

        batch_t = (
            torch.from_numpy(np.ascontiguousarray(batch_np))
            .permute(0, 3, 1, 2)
            .to(device=device, dtype=dtype)
            / 255.0
        )
        with torch.no_grad():
            out_t = self._model(batch_t)
        return (  # type: ignore[no-any-return]
            (out_t.clamp(0.0, 1.0) * 255.0)
            .to(torch.uint8)
            .permute(0, 2, 3, 1)
            .cpu()
            .numpy()
        )

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between cuda/cpu."""
        self._model.to(device)
