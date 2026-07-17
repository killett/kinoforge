"""FlashVSREngine — HTTP-aware UpscalerEngine impl backed by FlashVSR runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} + /upload endpoints.
Pod-HTTP client machinery (submit/poll loop, PUT /upload, Cloudflare UA gate)
is shared via :mod:`kinoforge.engines._pod_http`, which wraps
:func:`kinoforge.engines._proxy_retry.retry_proxy_call` for RunPod proxy
startup-window 404/502 tolerance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
    UnsupportedScaleError,
    UpscaleFailed,
)
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    RenderedProvision,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.engines._pod_http import PodHTTPClientMixin, http_json, submit_and_poll

_USER_AGENT = "kinoforge-flashvsr/0.1"


class FlashVSREngine(PodHTTPClientMixin, UpscalerEngine):
    """FlashVSR v1.1 streaming diffusion video upscaler."""

    name = "flashvsr"
    requires_compute = True
    requires_local_weights = True
    _pod_user_agent = _USER_AGENT
    # Native scale is hard-pinned to 4x by the upstream Causal_LQ4x_Proj weight
    # shape (config._validate_flashvsr_wiring + runtime _NATIVE_SCALE enforce it).
    # Declared explicitly — NOT the empty accept-any sentinel — so UpscaleStage's
    # height-target resolver can read the factor menu. Live smoke 2026-07-05 hit
    # 'supported_factors must be non-empty' with the old empty tuple.
    supported_scales: tuple[ScaleTarget, ...] = (ScaleTarget(kind="factor", value=4.0),)

    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-target + non-4x scales (spec §2 non-goal + native lock)."""
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale 4x"
            )
        if job.scale.value != 4.0:
            raise UnsupportedScaleError(scale=job.scale, engine_name="flashvsr")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return ``flashvsr-wan21-<precision>`` slug for the server LRU."""
        try:
            block = cast(
                dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"]
            )
            precision = str(block["precision"])
            return f"flashvsr-wan21-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit SM80+ guard + BSA-wheel curl + FlashVSR install + weights fetch.

        BSA install swapped from ``pip install git+...`` (25-45 min nvcc source
        compile on 4-CPU pods) to a ``curl`` + ``pip install --no-deps`` of a
        prebuilt wheel we host on HF Hub — see T7.5 in
        ``docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md``.
        Wheel URL is cfg-driven via ``FlashVSREngineConfig.bsa_wheel_url``.
        """
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"])
        bundle = str(block["weights_bundle"])
        long_video = "1" if block.get("long_video_mode") else "0"
        wheel_url = str(block["bsa_wheel_url"])
        # HF_HUB_OFFLINE=1 tail is upscale-only-pod hygiene (no accidental
        # Hub hits at inference). This script runs BEFORE the server exec
        # line, so on a co-resident pod the export would put the Wan 2.2
        # eager load offline too — OfflineModeIsEnabled crash-loop, pod
        # dk8otbrvddetmx 2026-07-03. Suppress when a diffusers engine
        # block is present without upscale_only (missing engine block =
        # bare upscale cfg = upscale-only semantics, keep the tail).
        engine_block = cast(dict[str, Any], cfg.get("engine") or {})
        diffusers_block = cast(dict[str, Any], engine_block.get("diffusers") or {})
        offline_tail = "diffusers" not in engine_block or bool(
            diffusers_block.get("upscale_only")
        )
        # pip parses distribution metadata (name, version, python tag, ABI,
        # platform) FROM the wheel filename — a generic 'bsa.whl' rename
        # fails immediately with "not a valid wheel filename". Preserve the
        # remote filename verbatim.
        wheel_name = wheel_url.rsplit("/", 1)[-1]
        script = "".join(
            [
                "set -euo pipefail\n",
                # SM80+ guard. At image-BUILD time (Modal's CPU image builder)
                # there is no CUDA device -> is_available() is False -> the guard
                # no-ops so the BSA wheel can still bake into the image. At
                # runtime (GPU present) it enforces SM80+. The Modal cfg pins
                # A100-80GB/H100 (SM80/SM90), so on the baked path the dropped
                # runtime check is belt-and-suspenders; RunPod still gets the
                # full runtime enforcement via the combined script.
                'python -c "import torch; '
                "cap = torch.cuda.get_device_capability() "
                "if torch.cuda.is_available() else None; "
                "assert cap is None or cap[0] >= 8, "
                "f'flashvsr: BSA needs SM80+, got {cap}'"
                '" || exit 87\n',
                # -L follows GitHub-release redirect to the S3-backed asset;
                # -f fail-fast on 4xx/5xx (silent HTML error page otherwise).
                f'curl -L -f -o "/tmp/{wheel_name}" "{wheel_url}"\n',
                f"pip install --no-deps /tmp/{wheel_name}\n",
                # FlashVSR repo tags no releases; pin to a specific commit SHA
                # so the pod's install path is reproducible. Bumping this pin
                # is a deliberate act — never let it drift with upstream main.
                # b527c6f2 = 2025-12-23, model bundle JunhaoZhuang/FlashVSR-v1.1
                # matches the pipeline surface at this commit.
                #
                # `--no-deps` because upstream's requirements.txt pins
                # `torch==2.6.0+cu124` (local +cu124 suffix), which pip
                # cannot resolve from PyPI. Runtime deps FlashVSR actually
                # needs at inference time are installed on the next line;
                # torch itself is already installed via the cfg's `pip:` block.
                #
                # `--no-build-isolation`: FlashVSR's setup.py imports
                # pkg_resources at build. With pip's default build isolation the
                # wheel builds in a fresh env that lacks setuptools unless the
                # project's build-system.requires lists it (it doesn't) →
                # "No module named 'pkg_resources'" on python:3.13-slim (Modal),
                # observed live 2026-07-10. --no-build-isolation reuses the main
                # env's setuptools/wheel (cfg `pip:` installs them; RunPod's
                # pytorch image already ships them), so the build finds them.
                "pip install --no-deps --no-build-isolation "
                '"git+https://github.com/OpenImagingLab/FlashVSR'
                '@b527c6f285fb30df530f5febc8b45764a789c961"\n',
                # Runtime deps for diffsynth (FlashVSR's inner package).
                # Pins match diffsynth 1.1.7's `install_requires` so
                # ModelManager + FlashVSRFullPipeline surfaces load
                # cleanly. transformers 5.x removed `PretrainedConfig`
                # from `transformers.modeling_utils` — diffsynth breaks
                # at `from transformers.modeling_utils import
                # PretrainedConfig`. Downgrade + pin.
                # peft 0.17 (not 0.16): the co-resident F-multi pod's
                # diffusers (cfg pip, floats latest) hard-requires
                # peft>=0.17 at import — 0.16 crash-loops the server at
                # startup (pod 1sjify76z1oqha, 2026-07-03). diffsynth's
                # peft surface loads fine on 0.17 (F-multi live smoke).
                #
                # transformers window >=4.48,<5 (not ==4.46.2): diffusers
                # latest imports Dinov2WithRegistersConfig (added 4.48)
                # inside the Wan VAE module (pod riwilukyvoq7iz), while
                # transformers 5.x drops PretrainedConfig from
                # modeling_utils, which diffsynth imports at module top.
                # The window is the co-residency intersection.
                'pip install "modelscope" '
                '"safetensors==0.5.3" "transformers>=4.48,<5" '
                '"accelerate==1.8.1" "peft==0.17.0" '
                '"einops==0.8.1" "ftfy==6.3.1" "sentencepiece==0.2.0" '
                '"imageio[ffmpeg,pyav]>=2.34" "av"\n',
                "python -m kinoforge.upscalers.flashvsr._fetch_weights "
                f"--bundle {bundle} --dest /workspace/models/flashvsr "
                f"--include-long-video {long_video}\n",
                # posi_prompt.pth is a precomputed CLIP-encoded prompt
                # tensor that FlashVSRFullPipeline.init_cross_kv() loads
                # by a HARDCODED RELATIVE PATH
                # (`../../examples/WanVSR/prompt_tensor/posi_prompt.pth`,
                # see diffsynth/pipelines/flashvsr_full.py:259). The runtime
                # bypasses the hardcoded path by passing the tensor
                # directly, so we just need to stage the file where
                # _runtime.py can find it. Fetch straight from the pinned
                # commit — small (< 1 MB), no HF Hub needed.
                'curl -L -f -o "/workspace/models/flashvsr/posi_prompt.pth" '
                '"https://raw.githubusercontent.com/OpenImagingLab/FlashVSR'
                "/b527c6f285fb30df530f5febc8b45764a789c961"
                '/examples/WanVSR/prompt_tensor/posi_prompt.pth"\n',
                # utils.py from the FlashVSR examples/ folder holds the
                # REAL `Causal_LQ4x_Proj` implementation the pipeline
                # expects (with `.clear_cache()`, `.stream_forward()`,
                # etc). Our vendored stub in _input_prep is insufficient
                # — pip install --no-deps skips the examples/ folder, so
                # fetch utils.py to a stable path the runtime can load
                # via importlib.
                'curl -L -f -o "/workspace/models/flashvsr/utils_upstream.py" '
                '"https://raw.githubusercontent.com/OpenImagingLab/FlashVSR'
                "/b527c6f285fb30df530f5febc8b45764a789c961"
                '/examples/WanVSR/utils/utils.py"\n',
            ]
            + (["export HF_HUB_OFFLINE=1\n"] if offline_tail else [])
        )
        return RenderedProvision(
            script=script,
            run_cmd=[],
            image="",
            ports=[],
            env_required=["HF_TOKEN"],
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """No-op — work is captured in :meth:`render_provision`."""
        del instance, cfg, cancel_token

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        """POST /upscale, poll /upscale/status/{id}, return UpscaleResult."""
        self.validate_spec(job)
        if instance is None:
            raise ValueError("FlashVSREngine requires a compute instance")
        base = self._base_url(instance)

        source_uri = job.source.uri
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("upscale", {})).get("flashvsr", {}),
        )
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
            "scale": f"{job.scale.value:g}x",
            "engine": "flashvsr",
            "flashvsr": block,
        }
        result, elapsed_s = submit_and_poll(
            label_prefix="flashvsr",
            base_url=base,
            endpoint="/upscale",
            payload=submit_payload,
            http_json=_http_json,
            make_error=lambda job_id, server_error: UpscaleFailed(
                job_id=job_id, server_error=server_error
            ),
            cancel_token=cancel_token,
        )
        return UpscaleResult(
            artifact=Artifact(
                uri=f"{base}/artifacts/{result['filename']}",
                sha256=result["sha256"],
                size=result["size"],
            ),
            input_resolution=tuple(result["input_resolution"]),
            output_resolution=tuple(result["output_resolution"]),
            elapsed_s=elapsed_s,
            engine_meta=result.get("engine_meta", {}),
        )


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Pod JSON call with the flashvsr User-Agent.

    Module-level seam — tests monkeypatch this name; keep it stable.
    Delegates to :func:`kinoforge.engines._pod_http.http_json`.
    """
    return http_json(method=method, url=url, payload=payload, user_agent=_USER_AGENT)
