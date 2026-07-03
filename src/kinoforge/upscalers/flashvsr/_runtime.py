"""FlashVSRRuntime — wrapper around diffsynth.FlashVSRFullPipeline.

Lazy-imports diffsynth + torch + imageio so the kinoforge-default env
does not need FlashVSR deps installed. Satisfies the LRU LoadedModel
contract used by wan_t2v_server's model registry via the ``flashvsr-*``
slug prefix (T5 server dispatch delta).

Upstream reference:
    https://github.com/OpenImagingLab/FlashVSR
    Commit b527c6f2 — examples/WanVSR/infer_flashvsr_v1.1_full.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_log = logging.getLogger(__name__)

# Upstream Causal_LQ4x_Proj weight shape hard-pins the native scale at 4.
_NATIVE_SCALE = 4.0
_STREAMING_DMD_FILE = "diffusion_pytorch_model_streaming_dmd.safetensors"
_VAE_FILE = "Wan2.1_VAE.pth"

# Upstream default sparse_ratio used to derive topk_ratio per-resolution.
# See infer_flashvsr_v1.1_full.py: sparse_ratio=2.0,
# topk_ratio = sparse_ratio * 768 * 1280 / (th * tw).
_SPARSE_RATIO = 2.0
_KV_RATIO = 3.0
_LOCAL_RANGE = 11


class FlashVSRRuntime:
    """Loads FlashVSR weights + runs streaming VSR via diffsynth.

    Native upscale factor is FIXED at 4× (upstream ``Causal_LQ4x_Proj``).

    Args:
        weights_dir: Local dir holding the 2-file (lite) or 4-file
            (long-video) bundle downloaded via ``_fetch_weights``.
        precision: ``"bfloat16"`` (upstream default), ``"fp16"``, or
            ``"fp32"``.
        window_size: Streaming attention window (frames). Currently
            unused by the FullPipeline path — kept for API compatibility
            with legacy cfgs. Warns if != 24.
        tile_size: Whole-frame if 0; else spatial tiling for VRAM
            headroom (passed to the pipeline as ``tiled=True``).
        long_video_mode: When ``True``, enables LCSA + TCDecoder (needs
            the 4-file bundle). Currently informational only — the
            FullPipeline path handles both modes internally.

    Raises:
        ImportError: ``diffsynth`` package not installed in the env.
    """

    def __init__(
        self,
        weights_dir: Path,
        precision: Literal["bfloat16", "fp16", "fp32"],
        window_size: int,
        tile_size: int,
        long_video_mode: bool,
    ) -> None:
        """Lazy-import diffsynth + torch, load weights via ModelManager."""
        import torch
        from diffsynth import (  # type: ignore[import-not-found]
            FlashVSRFullPipeline,
            ModelManager,
        )

        from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj

        if precision == "bfloat16":
            dtype = torch.bfloat16
        elif precision == "fp16":
            dtype = torch.float16
        else:
            dtype = torch.float32

        mm = ModelManager(torch_dtype=dtype, device="cpu")
        mm.load_models(
            [
                str(weights_dir / _STREAMING_DMD_FILE),
                str(weights_dir / _VAE_FILE),
            ]
        )
        pipe = FlashVSRFullPipeline.from_model_manager(mm, device="cuda")

        # Upstream pipeline lifecycle (infer_flashvsr_v1.1_full.py::init_pipeline):
        #   pipe.denoising_model().LQ_proj_in = Causal_LQ4x_Proj(...)
        #   pipe.denoising_model().LQ_proj_in.load_state_dict(...)  # if .ckpt present
        #   pipe.vae.model.encoder = None   # VAE encoder teardown (VRAM saving)
        #   pipe.vae.model.conv1 = None
        #   pipe.to('cuda')
        #   pipe.enable_vram_management(num_persistent_param_in_dit=None)
        #   pipe.init_cross_kv()
        #   pipe.load_models_to_device(["dit", "vae"])

        # Inject LQ_proj_in on the denoising model's LQ conditioning path.
        # Upstream: Causal_LQ4x_Proj(in_dim=3, out_dim=1536, layer_num=1)
        lq_proj = Causal_LQ4x_Proj(in_dim=3, out_dim=1536, layer_num=1).to(
            "cuda", dtype=torch.bfloat16
        )
        lq_ckpt = weights_dir / "LQ_proj_in.ckpt"
        if lq_ckpt.exists():
            lq_proj.load_state_dict(
                torch.load(str(lq_ckpt), map_location="cpu"), strict=True
            )
        pipe.denoising_model().LQ_proj_in = lq_proj
        pipe.denoising_model().LQ_proj_in.to("cuda")

        # VAE encoder teardown — VRAM optimisation (upstream init_pipeline).
        pipe.vae.model.encoder = None
        pipe.vae.model.conv1 = None

        pipe.to("cuda")
        pipe.enable_vram_management(num_persistent_param_in_dit=None)
        pipe.init_cross_kv()
        pipe.load_models_to_device(["dit", "vae"])

        self._pipe = pipe
        self._window = window_size
        self._tile = tile_size
        self._precision = precision
        self._long_video_mode = long_video_mode
        self._native_scale: float = _NATIVE_SCALE

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Run streaming VSR on ``video_path``; return sibling ``.flashvsr.mp4``."""
        import imageio.v3 as iio

        from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

        if scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr: height-target scale ({int(scale.value)}p) not yet "
                "wired; use --scale 4x"
            )
        if scale.value != self._native_scale:
            raise UnsupportedScaleError(scale=scale, engine_name="flashvsr")
        if params.get("prompt"):
            _log.warning(
                "flashvsr: params['prompt'] ignored — model has no text encoder"
            )
        if self._window != 24:
            _log.warning(
                "flashvsr: window_size=%d ignored by FullPipeline path (upstream fixed)",
                self._window,
            )

        import torch

        lq, th, tw, num_frames, fps = prepare_input_tensor(
            str(video_path), scale=int(self._native_scale)
        )
        # topk_ratio derived from resolution per upstream recommendation:
        # sparse_ratio * 768 * 1280 / (th * tw) with sparse_ratio=2.0.
        topk_ratio = _SPARSE_RATIO * 768 * 1280 / (th * tw)

        with torch.no_grad():
            out_tensor = self._pipe(
                prompt="",
                negative_prompt="",
                cfg_scale=1.0,
                num_inference_steps=1,
                seed=0,
                tiled=bool(self._tile),
                LQ_video=lq,
                num_frames=num_frames,
                height=th,
                width=tw,
                is_full_block=False,
                if_buffer=True,
                topk_ratio=topk_ratio,
                kv_ratio=_KV_RATIO,
                local_range=_LOCAL_RANGE,
                color_fix=True,
            )

        out = video_path.with_suffix(".flashvsr.mp4")
        # Upstream tensor2video: rearrange "C T H W -> T H W C", denormalise
        # from [-1, 1] float → uint8 [0, 255]: (x + 1) * 127.5.
        # The pipe returns shape (1, 3, F, H, W) in [-1, 1] float.
        import numpy as np

        arr: Any = out_tensor.cpu().float().numpy()  # (1, 3, F, H, W) float in [-1,1]
        if hasattr(arr, "shape") and len(arr.shape) == 5:
            # Denormalise [-1,1] → [0,255], then rearrange (1,3,F,H,W) → (F,H,W,3).
            video = ((arr[0] + 1.0) * 127.5).clip(0, 255).astype(np.uint8)
            video = video.transpose(1, 2, 3, 0)  # (3, F, H, W) → (F, H, W, 3)
        else:
            # Stub or unexpected shape — pass through as-is.
            video = arr
        iio.imwrite(str(out), video, fps=fps, plugin="pyav")
        return out

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying pipe between cuda/cpu.

        Delegates to ``pipe.to(device)`` (``nn.Module.to``-compatible
        device-move API).  ``load_models_to_device`` takes a list of model
        *names* and is a construction-time loader, NOT a device-move call.
        Passing a device string there would iterate the string char-by-char
        (Python treats ``str`` as iterable) and silently break VRAM
        management on every LRU eviction.
        """
        self._pipe.to(device)

    @property
    def vram_bytes(self) -> int:
        """Wan 2.1 1.3B backbone bfloat16 ≈ 2.6 GB + streaming state ≈ 4-8 GB peak."""
        return int(8 * 1024**3)
