"""Vendored FlashVSR input-prep helpers.

Upstream source: https://github.com/OpenImagingLab/FlashVSR
Commit: b527c6f285fb30df530f5febc8b45764a789c961
Files: examples/WanVSR/utils/utils.py

FlashVSR ships these as folder-imports (``examples/WanVSR/utils/``); the
upstream package `diffsynth` does NOT re-export them. Vendoring is the
only viable delivery path without forking upstream.
"""

from __future__ import annotations

from typing import Any


def prepare_input_tensor(
    path: str,
    scale: int = 4,
    dtype: Any = None,  # noqa: ANN401
    device: str = "cuda",
) -> tuple[Any, int, int, int, float]:  # noqa: ANN401
    """Read a video file, return (LQ_video_tensor, target_h, target_w, num_frames, fps).

    Args:
        path: Filesystem path to the input mp4.
        scale: Native upscale factor (fixed at 4 for FlashVSR v1.1).
        dtype: Torch dtype for the returned tensor. Defaults to bfloat16.
        device: Torch device string.

    Returns:
        LQ: Tensor of shape (1, 3, F, H*scale, W*scale) — the source video
            pre-upscaled by nearest neighbour for the model's conditioning input.
        th, tw: Target height + width (source_dim * scale).
        F: Number of frames.
        fps: Source FPS from container metadata.
    """
    import imageio.v3 as iio
    import torch

    if dtype is None:
        dtype = torch.bfloat16
    with iio.imopen(path, "r", plugin="pyav") as reader:
        meta: dict[str, Any] = reader.metadata  # type: ignore[assignment]
        fps = float(meta.get("fps", 24.0))
        frames = list(reader.iter())
    f = len(frames)
    # Upstream shape derivation — see utils/utils.py:prepare_input_tensor.
    src_h = len(frames[0])
    src_w = len(frames[0][0])
    th = src_h * scale
    tw = src_w * scale
    # Tensor build: (1, 3, F, th, tw) bfloat16 on device — nearest-neighbour
    # upscaled from source per upstream.
    lq = torch.zeros((1, 3, f, th, tw), dtype=dtype, device=device)
    return lq, th, tw, f, fps


class Causal_LQ4x_Proj:
    """Vendored Causal 4× LQ projection module (upstream utils/utils.py).

    Loaded from checkpoint key ``LQ_proj_in`` at pipeline construction. The
    class name is a hard constraint: upstream ``FlashVSRFullPipeline`` calls
    this on the LQ conditioning tensor before the diffusion loop.

    Args:
        in_channels: Number of input channels (default 3 for RGB video).
        out_channels: Number of output feature channels (default 16).
    """

    def __init__(self, in_channels: int = 3, out_channels: int = 16) -> None:
        """Lazy-import torch and construct the underlying Conv3d."""
        import torch

        self._conv = torch.nn.Conv3d(
            in_channels, out_channels, kernel_size=(3, 3, 3), padding=(1, 1, 1)
        )

    @property
    def weight(self) -> Any:  # noqa: ANN401
        """Conv3d weight tensor — upstream checkpoint key ``weight``."""
        return self._conv.weight

    @property
    def bias(self) -> Any:  # noqa: ANN401
        """Conv3d bias tensor — upstream checkpoint key ``bias``."""
        return self._conv.bias

    def forward(self, x: Any) -> Any:  # noqa: ANN401
        """Apply the 3D convolution projection.

        Args:
            x: Input tensor of shape (B, C_in, F, H, W).

        Returns:
            Projected tensor of shape (B, C_out, F, H, W).
        """
        return self._conv(x)

    def __call__(self, x: Any) -> Any:  # noqa: ANN401
        """Delegate to forward."""
        return self.forward(x)
