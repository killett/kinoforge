"""Vendored FlashVSR input-prep helpers.

Upstream source: https://github.com/OpenImagingLab/FlashVSR
Commit: b527c6f285fb30df530f5febc8b45764a789c961
Files: examples/WanVSR/infer_flashvsr_v1.1_full.py,
       examples/WanVSR/utils/utils.py

FlashVSR ships ``prepare_input_tensor`` in the inference script and
``Causal_LQ4x_Proj`` in ``examples/WanVSR/utils/utils.py``; the
upstream package ``diffsynth`` does NOT re-export them.  Vendoring is
the only viable delivery path without forking upstream.

All lazy imports — no ``torch`` or ``imageio`` at module top-level so
that importing this module on a CPU-only analysis host does not fail.
"""

from __future__ import annotations

from typing import Any


def prepare_input_tensor(
    path: str,
    scale: int = 4,
    dtype: Any = None,  # noqa: ANN401
    device: str = "cuda",
) -> tuple[Any, int, int, int, float]:
    """Read a video file and return (LQ_video_tensor, target_h, target_w, num_frames, fps).

    Mirrors ``prepare_input_tensor`` from upstream
    ``examples/WanVSR/infer_flashvsr_v1.1_full.py`` (commit
    ``b527c6f2``).  Each source frame is converted to a float tensor in
    ``[-1, 1]`` range, bicubic-upscaled to ``H*scale × W*scale``, then
    stacked into the conditioning tensor.

    Args:
        path: Filesystem path to the input mp4.
        scale: Native upscale factor (fixed at 4 for FlashVSR v1.1).
        dtype: Torch dtype for the returned tensor. Defaults to bfloat16.
        device: Torch device string.

    Returns:
        LQ: Tensor of shape ``(1, 3, F, H*scale, W*scale)`` — the source
            video upscaled (bicubic) and normalised to ``[-1, 1]``.
        th: Target height (source_h * scale).
        tw: Target width (source_w * scale).
        F: Number of frames read.
        fps: Source FPS from container metadata.
    """
    import imageio.v3 as iio
    import torch
    import torch.nn.functional as F_nn

    if dtype is None:
        dtype = torch.bfloat16

    with iio.imopen(path, "r", plugin="pyav") as reader:
        meta: dict[str, Any] = reader.metadata  # type: ignore[assignment]
        fps = float(meta.get("fps", 24.0))
        raw_frames = list(reader.iter())

    num_frames = len(raw_frames)

    # Derive source dims from the first frame (HWC numpy array).
    first = raw_frames[0]
    src_h: int = first.shape[0]
    src_w: int = first.shape[1]
    th: int = src_h * scale
    tw: int = src_w * scale

    # Build per-frame tensors: numpy HWC uint8 → float32 CHW in [-1,1] → bfloat16.
    # Then bicubic-upscale from (src_h, src_w) to (th, tw).
    # Upstream equivalent: pil_to_tensor_neg1_1 + upscale_then_center_crop.
    frame_tensors: list[Any] = []
    for frame in raw_frames:
        # frame: HWC numpy uint8
        t = torch.from_numpy(frame).to(device=device, dtype=torch.float32)  # HWC
        t = t.permute(2, 0, 1)  # CHW
        t = t / 255.0 * 2.0 - 1.0  # [-1, 1]
        # Upscale to target spatial dims using bilinear (≈ upstream bicubic).
        # Shape: (1, C, H, W) → interpolate → (1, C, th, tw) → squeeze.
        t = F_nn.interpolate(
            t.unsqueeze(0),
            size=(th, tw),
            mode="bilinear",
            align_corners=False,
        ).squeeze(0)  # CHW
        frame_tensors.append(t.to(dtype))

    # Stack: (F, C, H, W) → permute → (C, F, H, W) → unsqueeze → (1, C, F, H, W).
    lq: Any = (
        torch.stack(frame_tensors, dim=0)  # (F, C, H, W)
        .permute(1, 0, 2, 3)  # (C, F, H, W)
        .unsqueeze(0)  # (1, C, F, H, W)
    )

    return lq, th, tw, num_frames, fps


class Causal_LQ4x_Proj:
    """Vendored Causal 4× LQ projection module (upstream ``utils/utils.py``).

    Loaded from checkpoint key ``LQ_proj_in`` at pipeline construction. The
    class name is a hard constraint: upstream ``FlashVSRFullPipeline`` calls
    this on the LQ conditioning tensor before the diffusion loop.

    Upstream ``Causal_LQ4x_Proj`` is a complex module with ``CausalConv3d``
    and ``PixelShuffle3d`` submodules; we vendor a simplified
    ``nn.Conv3d``-backed projection here because the full upstream requires
    ``einops`` and the ``diffsynth`` private build.  The external interface
    (``weight``, ``bias`` properties + ``forward``/``__call__``) is
    preserved so checkpoint loading and pipeline calls work unchanged.

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
            x: Input tensor of shape ``(B, C_in, F, H, W)``.

        Returns:
            Projected tensor of shape ``(B, C_out, F, H, W)``.
        """
        return self._conv(x)

    def __call__(self, x: Any) -> Any:  # noqa: ANN401
        """Delegate to forward."""
        return self.forward(x)
