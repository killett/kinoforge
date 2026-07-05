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


#: Token block size of the Block-Sparse-Attention kernel. FlashVSR derives
#: its block mask over 128-token blocks (window_size = win_f*h*w // 128 in
#: wan_video_dit.model_fn_wan_video); the dense fallback expands the mask
#: back to token granularity with the same constant.
_BSA_BLOCK_SIZE = 128


def _dense_masked_attention(
    q: Any,  # noqa: ANN401
    k: Any,  # noqa: ANN401
    v: Any,  # noqa: ANN401
    num_heads: int,
    attention_mask: Any,  # noqa: ANN401
    block_size: int = _BSA_BLOCK_SIZE,
) -> Any:  # noqa: ANN401
    """Dense reference implementation of BSA's masked attention.

    Debug-only path for the corruption root-cause matrix: computes exact
    softmax attention in fp32 with the block mask expanded to token
    granularity, replacing the ``block_sparse_attn_func`` CUDA kernel. Slow
    and memory-hungry by design — evidence quality over speed.

    Args:
        q: Query tensor ``(B, S_q, num_heads*d)`` (already RoPE'd +
            window-reordered by the caller, same as the BSA path receives).
        k: Key tensor ``(B, S_kv, num_heads*d)``.
        v: Value tensor ``(B, S_kv, num_heads*d)``.
        num_heads: Attention head count.
        attention_mask: Boolean block mask ``(B, num_heads, Qb, Kb)`` over
            ``block_size``-token blocks (True = attend).
        block_size: Tokens per mask block.

    Returns:
        Attention output ``(B, S_q, num_heads*d)`` in ``q``'s dtype.
    """
    import torch

    bsz, s_q, dim = q.shape
    d = dim // num_heads
    s_kv = k.shape[1]
    qh = q.view(bsz, s_q, num_heads, d).permute(0, 2, 1, 3).float()
    kh = k.view(bsz, s_kv, num_heads, d).permute(0, 2, 1, 3).float()
    vh = v.view(bsz, s_kv, num_heads, d).permute(0, 2, 1, 3).float()

    mask_tok = attention_mask.repeat_interleave(block_size, dim=-2)
    mask_tok = mask_tok.repeat_interleave(block_size, dim=-1)
    mask_tok = mask_tok[:, :, :s_q, :s_kv].to(torch.bool)

    scale = d**-0.5
    out = torch.empty_like(qh)
    # Per-head loop keeps peak memory at one (S_q, S_kv) fp32 score matrix.
    for h in range(num_heads):
        scores = torch.einsum("bqd,bkd->bqk", qh[:, h], kh[:, h]) * scale
        scores = scores.masked_fill(~mask_tok[:, h], float("-inf"))
        attn = torch.softmax(scores, dim=-1)
        # Rows whose mask is entirely False softmax to NaN — zero them so
        # they contribute nothing (matches the sparse kernel's behaviour).
        attn = torch.nan_to_num(attn, nan=0.0)
        out[:, h] = torch.einsum("bqk,bkd->bqd", attn, vh[:, h])
    return out.permute(0, 2, 1, 3).reshape(bsz, s_q, dim).to(q.dtype)


def _make_dense_flash_attention(orig: Any) -> Any:  # noqa: ANN401
    """Wrap wan_video_dit.flash_attention, replacing only the masked branch.

    Unmasked calls (cross-attention, any non-BSA path) delegate to the
    original so the experiment isolates exactly one variable: the
    block-sparse CUDA kernel.

    Args:
        orig: The original ``flash_attention`` function being replaced.

    Returns:
        A drop-in replacement callable.
    """

    def patched(
        q: Any = None,  # noqa: ANN401
        k: Any = None,  # noqa: ANN401
        v: Any = None,  # noqa: ANN401
        num_heads: int = 0,
        compatibility_mode: bool = False,
        attention_mask: Any = None,  # noqa: ANN401
        return_KV: bool = False,  # noqa: N803 — upstream kwarg name
    ) -> Any:  # noqa: ANN401
        if attention_mask is not None:
            return _dense_masked_attention(q, k, v, num_heads, attention_mask)
        return orig(
            q=q,
            k=k,
            v=v,
            num_heads=num_heads,
            compatibility_mode=compatibility_mode,
            attention_mask=attention_mask,
            return_KV=return_KV,
        )

    return patched


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
        import importlib.util

        import torch
        from diffsynth import (  # type: ignore[import-not-found]
            FlashVSRFullPipeline,
            ModelManager,
        )

        # Prefer upstream Causal_LQ4x_Proj (staged by render_provision at
        # /workspace/models/flashvsr/utils_upstream.py) — it has the full
        # `.clear_cache()`, `.stream_forward()`, etc surface the pipeline
        # actually invokes. Fall back to the vendored stub when the file
        # is missing (unit tests, dry-run local use).
        upstream_utils_path = weights_dir / "utils_upstream.py"
        if upstream_utils_path.exists():
            spec = importlib.util.spec_from_file_location(
                "_flashvsr_upstream_utils", str(upstream_utils_path)
            )
            if spec is None or spec.loader is None:
                raise ImportError(
                    f"flashvsr: could not load upstream utils.py from "
                    f"{upstream_utils_path}"
                )
            _upstream = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_upstream)
            Causal_LQ4x_Proj = _upstream.Causal_LQ4x_Proj
        else:
            from kinoforge.upscalers.flashvsr._input_prep import (
                Causal_LQ4x_Proj,
            )

        if precision == "bfloat16":
            dtype = torch.bfloat16
        elif precision == "fp16":
            dtype = torch.float16
        else:
            dtype = torch.float32
        self._dtype = dtype

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
        # dtype follows user precision so the projection stays on the same
        # dtype as the rest of the pipeline (avoids fp32/bf16 mismatch).
        lq_proj = Causal_LQ4x_Proj(in_dim=3, out_dim=1536, layer_num=1).to(
            "cuda", dtype=self._dtype
        )
        lq_ckpt = weights_dir / "LQ_proj_in.ckpt"
        if lq_ckpt.exists():
            lq_proj.load_state_dict(
                torch.load(str(lq_ckpt), map_location="cpu"), strict=True
            )
        pipe.denoising_model().LQ_proj_in = lq_proj
        # NOTE: no second .to("cuda") call here — the .to("cuda", dtype=...)
        # above already placed lq_proj on device; the assignment on the
        # preceding line preserves device/dtype (no copy occurs).

        # VAE encoder teardown — VRAM optimisation (upstream init_pipeline).
        pipe.vae.model.encoder = None
        pipe.vae.model.conv1 = None

        pipe.to("cuda")
        pipe.enable_vram_management(num_persistent_param_in_dit=None)
        # init_cross_kv defaults to loading `posi_prompt.pth` from a
        # HARDCODED relative path (`../../examples/WanVSR/prompt_tensor/
        # posi_prompt.pth`, see diffsynth/pipelines/flashvsr_full.py:259).
        # Bypass the hardcoded path by loading the tensor ourselves from
        # the weights_dir (staged by render_provision) and passing
        # `context_tensor=` directly. When the file is absent (unit test
        # stubs), fall back to the upstream default so mocked pipes work.
        posi_prompt = weights_dir / "posi_prompt.pth"
        if posi_prompt.exists():
            ctx = torch.load(str(posi_prompt), map_location="cpu")
            pipe.init_cross_kv(context_tensor=ctx)
        else:
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

        attention_impl = params.get("attention_impl")
        if attention_impl not in (None, "bsa", "dense"):
            raise ValueError(
                f"attention_impl must be 'bsa', 'dense', or absent; "
                f"got {attention_impl!r}"
            )

        lq, th, tw, num_frames, fps = prepare_input_tensor(
            str(video_path), scale=int(self._native_scale)
        )
        # topk_ratio derived from resolution per upstream recommendation:
        # sparse_ratio * 768 * 1280 / (th * tw) with sparse_ratio=2.0.
        topk_ratio = _SPARSE_RATIO * 768 * 1280 / (th * tw)

        pipe_kwargs: dict[str, Any] = {
            "prompt": "",
            "negative_prompt": "",
            "cfg_scale": 1.0,
            "num_inference_steps": 1,
            "seed": 0,
            "tiled": bool(self._tile),
            "LQ_video": lq,
            "num_frames": num_frames,
            "height": th,
            "width": tw,
            "is_full_block": False,
            "if_buffer": True,
            "topk_ratio": topk_ratio,
            "kv_ratio": _KV_RATIO,
            "local_range": _LOCAL_RANGE,
            "color_fix": True,
        }
        # Debug-matrix seam: per-request kwargs replace the baseline above.
        pipe_kwargs.update(params.get("pipe_overrides") or {})

        dit_mod: Any = None
        orig_flash_attention: Any = None
        if attention_impl == "dense":
            import importlib

            dit_mod = importlib.import_module("diffsynth.models.wan_video_dit")
            orig_flash_attention = dit_mod.flash_attention
            dit_mod.flash_attention = _make_dense_flash_attention(orig_flash_attention)
            _log.info("flashvsr: attention_impl=dense — BSA kernel bypassed")

        try:
            with torch.no_grad():
                out_tensor = self._pipe(**pipe_kwargs)
        finally:
            if dit_mod is not None:
                dit_mod.flash_attention = orig_flash_attention

        out = video_path.with_suffix(".flashvsr.mp4")
        # Upstream tensor2video: rearrange "C T H W -> T H W C", denormalise
        # from [-1, 1] float → uint8 [0, 255]: (x + 1) * 127.5.
        # The pipe returns shape (1, 3, F, H, W) in [-1, 1] float.
        import numpy as np

        arr: Any = out_tensor.cpu().float().numpy()
        if params.get("debug_stats"):
            try:
                _stats_arr = np.asarray(arr, dtype=np.float32)
                _log.info(
                    "flashvsr debug_stats output shape=%s min=%.6f max=%.6f "
                    "mean=%.6f std=%.6f nan_count=%d",
                    tuple(_stats_arr.shape),
                    float(np.nanmin(_stats_arr)),
                    float(np.nanmax(_stats_arr)),
                    float(np.nanmean(_stats_arr)),
                    float(np.nanstd(_stats_arr)),
                    int(np.isnan(_stats_arr).sum()),
                )
            except Exception:  # noqa: BLE001 — diagnostics must never kill the job
                _log.exception("flashvsr debug_stats failed (non-fatal)")
        # Upstream tensor2video expects (C, T, H, W). Some FlashVSR
        # pipeline paths return the 4D version, others return (1, C, T,
        # H, W). Reduce to 4D (C, T, H, W) before denorm+rearrange.
        if hasattr(arr, "shape") and len(arr.shape) == 5:
            arr4d = arr[0]  # (C, T, H, W)
        elif hasattr(arr, "shape") and len(arr.shape) == 4:
            arr4d = arr  # already (C, T, H, W)
        else:
            # Stub or unexpected shape — pass through as-is (test path).
            video = arr
            arr4d = None
        if arr4d is not None:
            # Denormalise [-1,1] → [0,255], then rearrange (C,T,H,W)→(T,H,W,C).
            video = ((arr4d + 1.0) * 127.5).clip(0, 255).astype(np.uint8)
            video = video.transpose(1, 2, 3, 0)  # (T, H, W, C)
        # `codec=` is required when writing via the pyav plugin — without
        # it, imageio passes codec=None down to
        # `avcodec_find_encoder_by_name(None)` which raises
        # `TypeError: expected bytes, NoneType found`. libx264 is the
        # standard mp4 encoder; 22nd live smoke surfaced this.
        iio.imwrite(str(out), video, fps=fps, plugin="pyav", codec="libx264")
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
