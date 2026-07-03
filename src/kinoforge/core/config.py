"""Pydantic v2 config model for kinoforge.

Loads a YAML config into a validated model that:
- Parses human-readable duration strings (e.g. "2h", "30m", "90s") to seconds.
- Rejects nonsensical cross-field combinations (idle >= lifetime, etc.).
- Derives a CapabilityKey for cache lookup.
- Exposes Lifecycle and HardwareRequirements with defaults applied.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kinoforge.core.errors import ConfigError
from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.interfaces import (
    HardwareRequirements as InterfaceHardwareRequirements,
)
from kinoforge.core.interfaces import Lifecycle as InterfaceLifecycle
from kinoforge.core.lora import LoraEntry
from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Policy, Verdict

# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)(h|m|s)$")
_MULTIPLIERS: dict[str, float] = {"h": 3600.0, "m": 60.0, "s": 1.0}


def parse_duration(s: str) -> float:
    """Parse a human-readable duration string into seconds.

    Args:
        s: A duration string of the form ``<int>(h|m|s)`` e.g. ``"2h"``,
           ``"30m"``, ``"90s"``.

    Returns:
        Number of seconds as a float.

    Raises:
        ConfigError: If the string does not match the expected format (e.g.
            bare integers like ``"120"`` are rejected).
    """
    m = _DURATION_RE.match(s)
    if m is None:
        raise ConfigError(
            f"invalid duration: {s!r} (use 2h / 30m / 90s — bare integers not accepted)"
        )
    value = float(m.group(1))
    unit = m.group(2)
    return value * _MULTIPLIERS[unit]


# ---------------------------------------------------------------------------
# Valid kind/target combinations
# ---------------------------------------------------------------------------

VALID_KIND_TARGETS: dict[str, set[str]] = {
    "base": {"diffusion_models", "checkpoints", "unet"},
    "lora": {"loras"},
    "vae": {"vae"},
    "text_encoder": {"text_encoders", "clip"},
    "clip_vision": {"clip_vision"},
}

KNOWN_ENGINES = {
    "comfyui",
    "diffusers",
    "hosted",
    "fake",
    "fal",
    "bedrock_video",
    # Layer 4 — hosted Bearer-key video providers; no engine-specific YAML block.
    "replicate",
    "runway",
}

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class LifecycleConfig(BaseModel):
    """YAML-facing lifecycle configuration (durations stored in seconds internally).

    Attributes:
        idle_timeout: Duration string or seconds; default 2h.
        job_timeout: Duration string or seconds; default 30m.
        time_buffer: Duration string or seconds; default 30m.
        max_lifetime: Duration string or seconds; default 5h.
        boot_timeout: Duration string or seconds; default 15m (900 s).
        budget: Monthly/run budget in USD (required — no default).
        max_in_flight: Maximum concurrent jobs sent to a single backend.
            Default 1 (sequential, equivalent to old SequentialPool behaviour).
            Raise to match your backend's parallel capacity (e.g. 4 for a
            ComfyUI server with 4-GPU concurrency).
    """

    idle_timeout: float = 2 * 3600.0
    job_timeout: float = 30 * 60.0
    time_buffer: float = 30 * 60.0
    max_lifetime: float = 5 * 3600.0
    budget: float
    max_in_flight: int = 1
    boot_timeout: float = 900.0
    heartbeat_interval_s: float | None = None
    grace_after_session_s: float = 1800.0
    stall_reap_enabled: bool = True
    stall_window_s: float = 600.0
    stall_gpu_threshold: float = 5.0
    stall_cpu_threshold: float = 20.0
    restart_loop_reap_enabled: bool = True
    restart_loop_window_s: float = 180.0
    restart_loop_uptime_threshold_s: float = 90.0
    # LoRA-flexible warm-reuse — staleness threshold for the matcher's
    # pod-side free-disk + inventory snapshot. ``0`` disables the
    # stale-check entirely (matcher trusts the ledger snapshot
    # indefinitely). Default 300 s matches typical inter-job spacing
    # for warm-reuse workflows.
    lora_swap_re_probe_after_s: float = 300.0

    @field_validator(
        "idle_timeout",
        "job_timeout",
        "time_buffer",
        "max_lifetime",
        "boot_timeout",
        mode="before",
    )
    @classmethod
    def _parse_duration_field(cls, v: str | float | int) -> float | str | int:
        """Parse duration strings; pass through numeric values unchanged."""
        if isinstance(v, str):
            return parse_duration(v)
        return v

    @field_validator("heartbeat_interval_s")
    @classmethod
    def _validate_heartbeat_interval_positive(cls, v: float | None) -> float | None:
        """Reject non-positive heartbeat_interval_s at load time.

        Layer U: HeartbeatLoop.__init__ also raises ValueError on
        non-positive values, but doing the check at config-load means
        the bad config is rejected before the orchestrator creates any
        instance — no chance of leaving a pod orphaned by a late
        ValueError after create_instance returns.
        """
        if v is not None and v <= 0:
            raise ValueError(f"heartbeat_interval_s must be > 0 when set; got {v}")
        return v

    @field_validator("grace_after_session_s")
    @classmethod
    def _validate_grace_non_negative(cls, v: float) -> float:
        """Reject negative grace at load time (Layer V).

        Negative grace would invert the row-5/row-6 boundary in
        ``classify`` and cause sentinel-stale pods to be classified
        LIVE forever — a paid-leak class of bug.
        """
        if v < 0:
            raise ValueError(f"grace_after_session_s must be >= 0; got {v}")
        return v

    @field_validator("stall_window_s")
    @classmethod
    def _validate_stall_window_non_negative(cls, v: float) -> float:
        """Reject negative stall_window_s at load time (C26)."""
        if v < 0:
            raise ValueError(f"stall_window_s must be >= 0; got {v}")
        return v

    @field_validator("lora_swap_re_probe_after_s")
    @classmethod
    def _validate_lora_swap_re_probe_non_negative(cls, v: float) -> float:
        """Reject negative lora_swap_re_probe_after_s at load time."""
        if v < 0:
            raise ValueError(f"lora_swap_re_probe_after_s must be >= 0; got {v}")
        return v

    @field_validator("stall_gpu_threshold", "stall_cpu_threshold")
    @classmethod
    def _validate_stall_threshold_range(cls, v: float) -> float:
        """Reject util thresholds outside [0, 100] at load time (C26)."""
        if v < 0 or v > 100:
            raise ValueError(f"stall threshold must be in [0, 100]; got {v}")
        return v

    @field_validator("restart_loop_window_s")
    @classmethod
    def _validate_restart_loop_window_non_negative(cls, v: float) -> float:
        """Reject negative restart_loop_window_s at load time (C27)."""
        if v < 0:
            raise ValueError(f"restart_loop_window_s must be >= 0; got {v}")
        return v

    @field_validator("restart_loop_uptime_threshold_s")
    @classmethod
    def _validate_restart_loop_uptime_threshold_non_negative(cls, v: float) -> float:
        """Reject negative restart_loop_uptime_threshold_s at load time (C27)."""
        if v < 0:
            raise ValueError(f"restart_loop_uptime_threshold_s must be >= 0; got {v}")
        return v


class ComfyUIEngineConfig(BaseModel):
    """ComfyUI-specific engine parameters.

    Attributes:
        version: ComfyUI version string.
        custom_nodes: List of custom-node entries, each carrying a ``"git"``
            URL and an optional ``"ref"`` SHA pin.  Passed verbatim to
            :func:`~kinoforge.engines.comfyui.nodes.clone_and_install`.
            Defaults to an empty list so existing configs without this block
            remain valid.
        poll_timeout_s: Hard upper bound (seconds) on a single
            ``ComfyUIBackend.result`` poll wait. Raises ``TimeoutError``
            with ``last_status`` + ``exec_node`` in the message when
            exceeded. Default 1800 s (30 min) covers Wan 14B on
            A5000-class GPUs (~25-40 min observed). Phase 51 raised the
            default after the previous 600 s value killed a healthy run
            mid-sampler — see ``PROGRESS.md`` Phase 51 for the incident.
            Lift further for slower setups; lower if pathological hangs
            must be caught faster.
    """

    version: str
    custom_nodes: list[dict[str, Any]] = Field(default_factory=list)
    poll_timeout_s: float = Field(default=1800.0, gt=0.0)


class HostedEngineConfig(BaseModel):
    """Hosted API engine parameters.

    Attributes:
        provider: Hosted provider name (e.g. "fal").
        endpoint: API endpoint path.
        api_key_env: Env-var name carrying the API credential.
        health_url: URL pinged by provision() to verify reachability.
        url_path: Dot-path walked over the provider response by
            :meth:`HostedAPIBackend.result` to extract the artifact URL.
            Defaults to ``""`` (no walk).
        asset_paths: Mapping from conditioning-asset role
            (e.g. ``"init_image"``) to a dot-path in the request body
            where the asset's URL is injected at submit time. Defaults
            to an empty mapping.
        prompt_body_key: Top-level key in the request body where the
            user prompt is written by ``HostedAPIBackend.submit`` when
            the spec does not carry an explicit ``"prompt"``. Defaults
            to ``"prompt"``; set to ``None`` (YAML ``null``) to disable
            routing for endpoints that reject unknown top-level fields.

    Migration (Layer M): the previously-required ``model`` field has been
    removed. Hosted model identity now lives at top-level ``spec.model``
    and is read both by ``HostedAPIBackend.submit`` (wire body) and by
    ``HostedAPIEngine.key_base(cfg)`` (cache identity). YAML that still
    carries ``engine.hosted.model`` raises a load-time ``ValidationError``
    with a guiding message via the ``model_validator`` below.

    Declaring ``url_path`` and ``asset_paths`` here is load-bearing:
    pydantic v2's default ``extra="ignore"`` silently drops any YAML
    key not present on the model. The orchestrator calls
    ``cfg.model_dump()`` before passing the dict to ``engine.backend()``,
    so an undeclared field never reaches the engine — see the
    fix(config) commit history for the original defect. Layer M
    tightens this further with ``extra="forbid"`` so future stale
    keys surface at load instead of being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    endpoint: str
    api_key_env: str = ""
    health_url: str = ""
    url_path: str = ""
    asset_paths: dict[str, str] = Field(default_factory=dict)
    prompt_body_key: str | None = "prompt"

    @model_validator(mode="before")
    @classmethod
    def _reject_stale_model_field(cls, data: object) -> object:
        """Raise with migration guidance when the stale ``model`` key is present.

        Layer M dropped ``HostedEngineConfig.model`` in favour of top-level
        ``spec.model`` as the single source of truth for hosted model
        identity. Without this validator the stale key would be caught
        by ``extra="forbid"`` but with a generic ``extra_forbidden``
        message that does not tell the user where to move the value.
        """
        if isinstance(data, dict) and "model" in data:
            raise ValueError(
                "engine.hosted.model is no longer supported; "
                "move the value to top-level spec.model"
            )
        return data

    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env_non_empty(cls, v: str) -> str:
        """Reject empty api_key_env at config load (Layer I Task 4 / Bug 7).

        Without this, a missing/empty api_key_env propagates to runtime as
        ``AuthError("missing ")`` with no context.  Catching it here turns
        the failure into a load-time pydantic ValidationError naming the
        offending field.
        """
        if not v:
            raise ValueError(
                "engine.hosted.api_key_env must be a non-empty string "
                "(name of the env var carrying the API credential)"
            )
        return v

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint_absolute_url(cls, v: str) -> str:
        """Reject relative endpoints at config load (Layer I Task 4 / Bug 2).

        A relative path like ``/fal-ai/x`` would crash urllib mid-flight
        with ``ValueError: unknown url type``; surface it at load instead.
        """
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"engine.hosted.endpoint must be an absolute http(s):// URL, got {v!r}"
            )
        return v


class DiffusersEngineConfig(BaseModel):
    """DiffusersEngine-specific parameters.

    Attributes:
        base_url: Optional override for the inference server URL.
            ``DiffusersEngine.backend()`` derives the live URL from
            ``instance.endpoints["diffusers"]`` when available; an
            empty string defers to that path.
        pip: Pip packages installed by :meth:`DiffusersEngine.provision`.
        server_cmd: Argv to launch the local inference server.
        asset_paths: Mapping from conditioning-asset role to a dot-path
            in the request body for URL injection at submit time.
        prompt_body_key: Top-level key in the request body where the
            user prompt is written by ``DiffusersBackend.submit`` when
            the spec does not carry an explicit ``"prompt"``. Defaults
            to ``"prompt"``; set to ``None`` (YAML ``null``) to disable
            routing for endpoints that reject unknown top-level fields.

    See :class:`HostedEngineConfig` for the rationale behind declaring
    every YAML-consumed field: without this model, ``EngineConfig``
    would have no ``diffusers`` field at all and the entire block
    would be stripped by pydantic before reaching the engine.
    """

    base_url: str = ""
    pip: list[str] = Field(default_factory=list)
    server_cmd: list[str] = Field(default_factory=list)
    asset_paths: dict[str, str] = Field(default_factory=dict)
    prompt_body_key: str | None = "prompt"
    embed_modules: list[str] = Field(default_factory=list)
    embed_files: list[str] = Field(default_factory=list)  # Single-file
    # embeds for dotted module paths whose leaf is a .py file (e.g.
    # ``"kinoforge.core.errors"`` → embeds errors.py without dragging in
    # the rest of kinoforge/core/. Use when the on-pod runtime needs a
    # specific module but embedding its whole package would bust the
    # 64KB env-var ceiling.
    upscale_only: bool = False  # When True, render_provision emits
    # KINOFORGE_SKIP_WAN_LOAD=1 so the in-pod wan_t2v_server starts in
    # upscale-only mode (no eager WanPipeline.from_pretrained call).


class FalEngineConfig(BaseModel):
    """fal.ai engine parameters (queue API).

    Attributes:
        endpoint: fal model path, e.g. ``"fal-ai/wan/v2.2/t2v"``. Prepended
            by ``queue_base`` at submit time.
        queue_base: Base URL of the fal queue API. Defaults to the public
            ``https://queue.fal.run``; rarely overridden.
        api_key_env: Env-var name carrying the FAL_KEY. Defaults to ``"FAL_KEY"``.
        url_path: Dot-path walked over the response body by
            :meth:`FalBackend.result` to extract the artifact URL.
        asset_paths: Mapping from conditioning-asset role to a dot-path in
            the request body where the asset's URL is injected at submit time.
        health_url: Optional URL pinged by :meth:`FalEngine.provision`; empty
            disables the health probe (fal has no documented health endpoint).
    """

    endpoint: str
    queue_base: str = "https://queue.fal.run"
    api_key_env: str = "FAL_KEY"
    url_path: str
    asset_paths: dict[str, str] = Field(default_factory=dict)
    health_url: str = ""

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("engine.fal.endpoint must be a non-empty model path")
        return v

    @field_validator("url_path")
    @classmethod
    def _check_url_path_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "engine.fal.url_path must be a non-empty dot-path (e.g. 'video.url')"
            )
        return v

    @field_validator("queue_base")
    @classmethod
    def _check_queue_base_url(cls, v: str) -> str:
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"engine.fal.queue_base must be an absolute http(s):// URL, got {v!r}"
            )
        return v

    @field_validator("api_key_env")
    @classmethod
    def _check_api_key_env_non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("engine.fal.api_key_env must be a non-empty env-var name")
        return v


class BedrockVideoEngineConfig(BaseModel):
    """AWS Bedrock generic video-engine parameters.

    This config serves Nova Reel, Luma Ray v2, and any future Bedrock
    async-video model.  Model-specific request shape lives in
    ``model_input_template``; the engine substitutes ``"${PROMPT}"``
    recursively at submit time.

    Attributes:
        region_name: AWS region where the model is available (required).
        model_id: Bedrock model identifier (required — no default, because
            the engine serves multiple models).
        output_s3_uri: S3 prefix Bedrock writes generated MP4s into. Must
            start with ``s3://``.  Bedrock async invocations require an S3
            output destination (no inline response shape).
        output_kms_key_id: Optional SSE-KMS key ARN if the output bucket
            uses customer-managed encryption.
        model_input_template: Free-form dict forwarded verbatim as
            ``modelInput`` to ``StartAsyncInvoke``, after recursively
            substituting every string value equal to ``"${PROMPT}"`` with
            the resolved prompt.
        declared_flags_map: Per-capability-key strategy-flag overrides
            (matches sibling-engine convention).
    """

    model_config = ConfigDict(extra="forbid")

    region_name: str
    model_id: str
    output_s3_uri: str
    output_kms_key_id: str | None = None
    model_input_template: dict[str, Any]
    declared_flags_map: dict[str, dict[str, bool]] = Field(default_factory=dict)

    @field_validator("output_s3_uri")
    @classmethod
    def _check_output_s3_uri(cls, v: str) -> str:
        if not v.startswith("s3://"):
            raise ValueError(
                f"engine.bedrock_video.output_s3_uri must start with 's3://', got {v!r}"
            )
        return v


class SeedVR2EngineConfig(BaseModel):
    """SeedVR2-specific config; required when ``upscale.engine == "seedvr2"``.

    ``weights_ref`` defaults to ``None``; a ``model_validator`` populates the
    variant-derived ref (``"hf:ByteDance-Seed/SeedVR2-{variant}"``) so the
    common case stays one line in cfg. Explicit overrides (fork weights,
    pinned snapshots) are preserved unchanged.

    Attributes:
        variant: Model size; ``"3B"`` (default) or ``"7B"``.
        precision: ``"fp8"`` (default) or ``"fp16"``.
        tile_size: Optional VRAM-vs-throughput knob; engine default when None.
        steps: Optional denoise-step override; engine default when None.
        weights_ref: HF / vendor-neutral ref. Auto-populated from ``variant``
            when None.
    """

    variant: Literal["3B", "7B"] = "3B"
    precision: Literal["fp8", "fp16"] = "fp8"
    tile_size: int | None = None
    steps: int | None = None
    weights_ref: str | None = None

    @model_validator(mode="after")
    def _fill_weights_ref(self) -> Self:
        if self.weights_ref is None:
            object.__setattr__(
                self,
                "weights_ref",
                f"hf:ByteDance-Seed/SeedVR2-{self.variant}",
            )
        return self


class SpandrelEngineConfig(BaseModel):
    """Spandrel-specific config; required when ``upscale.engine == "spandrel"``.

    Attributes:
        model_url: Source ref for the SR weights (``hf:org/repo/file.pth``,
            ``civitai:<id>@<vid>``, ``civarchive:<id>@<vid>``, plain http(s)).
            Resolved via :mod:`kinoforge.upscalers.spandrel._fetch_weights`
            during pod provision.
        arch: Architecture token used for the model-identity slug
            (``"realesrgan"``, ``"esrgan"``, ``"swinir"``, ...). spandrel
            auto-detects the actual architecture from the weights file; this
            value is only the user-facing identifier surfaced in the sink
            filename schema.
        precision: ``"fp16"`` (default) or ``"fp32"``.
        tile_size: Frame-tile dimension in pixels for VRAM headroom.
        batch_size: Frames per CUDA batch.
    """

    model_url: str
    arch: str = "realesrgan"
    precision: Literal["fp16", "fp32"] = "fp16"
    tile_size: int = 512
    batch_size: int = 4


_FLASHVSR_VALID_TILE_SIZES = (0, 256, 384, 512, 768)
_FLASHVSR_WINDOW_MIN = 8
_FLASHVSR_WINDOW_MAX = 64
_FLASHVSR_VALID_SCHEMES = ("hf:", "http://", "https://", "civitai:", "civarchive:")
_FLASHVSR_BSA_WHEEL_SCHEMES = ("hf:", "http://", "https://")
_FLASHVSR_DEFAULT_BSA_WHEEL_URL = (
    "https://github.com/killett/kinoforge-artifacts/releases/download/"
    "bsa-cu128-torch2.8-v1/"
    "block_sparse_attn-0.0.1-cp311-cp311-linux_x86_64.whl"
)


class FlashVSREngineConfig(BaseModel):
    """FlashVSR v1.1 engine params — validated at cfg-load-time.

    See docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §4.

    The native upscale factor is FIXED at 4× by the upstream
    ``Causal_LQ4x_Proj`` weight shape (``native_scale = 4``).
    ``UpscaleConfig._validate_flashvsr_wiring`` refuses any non-4× ``scale``
    value at cfg-load time so the pod never cold-boots for a doomed run.

    Attributes:
        weights_bundle: Source ref for the 2-file (lite) or 4-file (long-video)
            bundle (``hf:JunhaoZhuang/FlashVSR-v1.1`` or plain http(s)).
            Resolved via :mod:`kinoforge.upscalers.flashvsr._fetch_weights`
            during pod provision.
        precision: ``"bfloat16"`` (upstream default, recommended),
            ``"fp16"`` (legacy DMD path), or ``"fp32"``.  Cast in the runtime
            at ``ModelManager(torch_dtype=...)``.  ``"bf16"`` (short form) is
            NOT accepted — upstream never used it.
        window_size: Streaming attention window in frames (``[8, 64]``).
        tile_size: Spatial tile in pixels for VRAM headroom. ``0`` = whole-frame;
            allowlist ``{0, 256, 384, 512, 768}`` chosen to align with the BSA
            block-size grid (mis-aligned values crash or produce border seams).
        long_video_mode: When ``True``, enables LCSA + TCDecoder — needs the
            4-file bundle. When ``False``, the 2-file lite bundle is enough.
        bsa_wheel_url: Prebuilt Block-Sparse-Attention wheel URL, fetched by
            provision via ``curl`` + ``pip install --no-deps``. Default is the
            kinoforge-hosted HF Hub wheel built at BSA commit ``3453bbb1``
            against ``runpod/pytorch:2.8.0-py3.11-cuda12.8.1`` — see T7.5 in
            ``docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md``.
            Allowed schemes: ``hf:``, ``http://``, ``https://``. ``git+`` and
            ``file://`` are rejected at cfg-time to prevent a 25-min-late pip
            error on a scheme ``curl`` cannot handle.
    """

    weights_bundle: str
    precision: str = "bfloat16"
    window_size: int = 24
    tile_size: int = 0
    long_video_mode: bool = False
    bsa_wheel_url: str = _FLASHVSR_DEFAULT_BSA_WHEEL_URL

    @field_validator("weights_bundle")
    @classmethod
    def _validate_scheme(cls, v: str) -> str:
        if not any(v.startswith(s) for s in _FLASHVSR_VALID_SCHEMES):
            raise ConfigError(
                f"flashvsr weights_bundle {v!r}: unsupported scheme "
                f"(supported: {_FLASHVSR_VALID_SCHEMES})"
            )
        return v

    @field_validator("bsa_wheel_url")
    @classmethod
    def _validate_bsa_wheel_url(cls, v: str) -> str:
        if not any(v.startswith(s) for s in _FLASHVSR_BSA_WHEEL_SCHEMES):
            raise ConfigError(
                f"flashvsr bsa_wheel_url {v!r}: unsupported scheme "
                f"(supported: {_FLASHVSR_BSA_WHEEL_SCHEMES})"
            )
        return v

    @field_validator("precision")
    @classmethod
    def _validate_precision(cls, v: str) -> str:
        if v not in ("bfloat16", "fp16", "fp32"):
            raise ConfigError(
                f"flashvsr precision {v!r} not in ('bfloat16', 'fp16', 'fp32')"
            )
        return v

    @field_validator("window_size")
    @classmethod
    def _validate_window(cls, v: int) -> int:
        if not (_FLASHVSR_WINDOW_MIN <= v <= _FLASHVSR_WINDOW_MAX):
            raise ConfigError(
                f"flashvsr window_size {v} out of range "
                f"[{_FLASHVSR_WINDOW_MIN}, {_FLASHVSR_WINDOW_MAX}]"
            )
        return v

    @field_validator("tile_size")
    @classmethod
    def _validate_tile(cls, v: int) -> int:
        if v not in _FLASHVSR_VALID_TILE_SIZES:
            raise ConfigError(
                f"flashvsr tile_size {v} not in {_FLASHVSR_VALID_TILE_SIZES}"
            )
        return v


class UpscaleConfig(BaseModel):
    """Top-level ``upscale:`` block; presence activates the in-pipeline UpscaleStage.

    Attributes:
        engine: Upscaler name (registry key). v1 supports ``"spandrel"``
            (default fallback) and ``"flashvsr"`` (v1 default diffusion VSR);
            ``"seedvr2"`` is extras-gated until Phase 2 vendoring lands.
        scale: ScaleTarget grammar string (``"2x"`` | ``"4x"`` | ``"1080p"`` ...).
            Consumers call ``ScaleTarget.parse(scale)``; the height branch
            raises ``NotYetImplementedError`` in v1. For ``engine=flashvsr``,
            height-target is refused at cfg-time.
        seedvr2: SeedVR2-specific block; required when ``engine == "seedvr2"``.
        spandrel: Spandrel-specific block; required when ``engine == "spandrel"``.
        flashvsr: FlashVSR-specific block; required when ``engine == "flashvsr"``.
    """

    engine: str
    scale: str
    seedvr2: SeedVR2EngineConfig | None = None
    spandrel: SpandrelEngineConfig | None = None
    flashvsr: FlashVSREngineConfig | None = None

    @model_validator(mode="after")
    def _validate_flashvsr_wiring(self) -> Self:
        if self.engine != "flashvsr":
            return self
        if self.flashvsr is None:
            raise ConfigError("engine=flashvsr requires a cfg.upscale.flashvsr block")
        # Lazy import to avoid top-level cycle with scale_target.
        from kinoforge.core.scale_target import ScaleTarget

        parsed = ScaleTarget.parse(self.scale)
        if parsed.kind == "height":
            raise ConfigError(
                f"engine=flashvsr: height-target scale ({self.scale!r}) "
                "not yet wired; use --scale Nx (factor form)"
            )
        if parsed.value != 4.0:
            raise ConfigError(
                f"engine=flashvsr fixed at native 4x upscale; got {self.scale!r}. "
                "Use engine=spandrel for other factors."
            )
        return self


class EngineConfig(BaseModel):
    """Top-level engine block.

    Attributes:
        kind: Engine name; must be one of the known engine types.
        precision: Precision/quantization string (e.g. "fp16", "gguf-q8").
        comfyui: ComfyUI-specific config, required when kind == "comfyui".
        hosted: Hosted API config, required when kind == "hosted".
        diffusers: Diffusers-specific config, optional even when
            kind == "diffusers" (all fields default to empty).
        fal: fal.ai queue-API config, required when kind == "fal".
        bedrock_video: AWS Bedrock generic video config, required when
            kind == "bedrock_video".  Covers Nova Reel, Luma Ray v2, and
            any future Bedrock async-video model via ``model_input_template``.
    """

    kind: str
    precision: str
    comfyui: ComfyUIEngineConfig | None = None
    hosted: HostedEngineConfig | None = None
    diffusers: DiffusersEngineConfig | None = None
    fal: FalEngineConfig | None = None
    bedrock_video: BedrockVideoEngineConfig | None = None


class ModelEntry(BaseModel):
    """A single model entry in the models list.

    Attributes:
        ref: Vendor-neutral model reference (e.g. "hf:org/m").
        kind: One of "base", "vae", "text_encoder", "clip_vision".
            ``"lora"`` was removed in P1 (2026-06-21); LoRAs live under
            the top-level ``Config.loras`` block. Legacy cfgs are
            auto-promoted by ``Config._promote_legacy_kind_lora_to_loras_block``.
        target: Download target directory name.
        sha256: Optional content hash for integrity verification.
    """

    ref: str
    kind: Literal["base", "vae", "text_encoder", "clip_vision"]
    target: str
    sha256: str | None = None


class RequirementsConfig(BaseModel):
    """Hardware requirements override block.

    Attributes:
        min_vram_gb: Minimum GPU VRAM in GB.
        min_cuda: Minimum CUDA version string.
        max_usd_per_hr: Ceiling on cost rate.
        gpu_preference: Ordered list of preferred GPU types.
        disk_gb: Minimum disk in GB.
    """

    min_vram_gb: int = 48
    min_cuda: str = "12.8"
    max_usd_per_hr: float = 2.20
    gpu_preference: list[str] = []
    disk_gb: int = 100


class ComputeConfig(BaseModel):
    """The compute block describing where workloads run.

    Attributes:
        provider: Compute provider name (e.g. "runpod").
        image: Container image reference.
        mode: Instance mode; "pod" or "serverless".
        requirements: Hardware requirements override.
        lifecycle: Lifecycle guardrails (budget required here for non-hosted).
        heartbeat_mode: Heartbeat substrate gate (B5a). Value space is the
            union across all providers; provider-mode compatibility is
            checked at adapter-dispatch time. Default ``"none"`` preserves
            pre-B5a no-op heartbeat behaviour.
        warm_reuse_auto_attach: B3 auto-discovery toggle. When ``True``
            (default), ``kinoforge generate`` / ``batch`` scans the ledger
            for warm pods matching the current capability_key on every
            fresh-shell invocation and attaches transparently. Set to
            ``False`` per-project to disable; ``--no-reuse`` on the CLI
            overrides this on a per-invocation basis.
        cloud: Phase 53 Stage C — optional list of sky cloud names
            (e.g. ``["lambda"]``, ``["lambda", "vast"]``) pinned onto
            :class:`~kinoforge.providers.skypilot.SkyPilotProvider` at
            instantiation time. ``None`` (default) preserves pre-Stage-C
            behaviour — sky considers every enabled cloud and picks by
            price. Ignored by non-skypilot providers.
    """

    provider: str
    image: str
    mode: str = "pod"
    requirements: RequirementsConfig = RequirementsConfig()
    lifecycle: LifecycleConfig | None = None
    heartbeat_mode: str = "none"
    warm_reuse_auto_attach: bool = True
    cloud: list[str] | None = None

    @field_validator("cloud")
    @classmethod
    def _validate_cloud(cls, v: list[str] | None) -> list[str] | None:
        """Reject empty-list cloud entries.

        Operator likely meant ``cloud: null`` or forgot to populate the
        entry; sky.launch with zero clouds would silently fall back.

        The membership check (each entry must be in the supported sky
        cloud set) moved to ``SkyPilotCloudPinSupportedCheck`` in
        ``kinoforge.providers.skypilot`` (Task 9 of the cfg-validation
        Check Registry plan). It now shows up in ``kinoforge doctor``
        output alongside every other validation rule.
        """
        if v is None:
            return v
        if not v:
            raise ValueError(
                "cloud must be a non-empty list of sky cloud names "
                "or null; got an empty list"
            )
        return v

    @field_validator("heartbeat_mode")
    @classmethod
    def _validate_heartbeat_mode(cls, v: str) -> str:
        """Reject heartbeat_mode values outside the union of supported literals.

        Provider-specific compatibility (e.g. RunPod accepts ``"none"`` +
        ``"graphql-tag"`` only) is verified by
        :func:`kinoforge._adapters.build_heartbeat_endpoint_for` at
        orchestrator dispatch time, where the per-provider module IS
        importable. Config-load can't gate on provider without violating
        core-import-ban.
        """
        allowed = {"none", "graphql-tag", "selfterm-http", "ssh-touch"}
        if v not in allowed:
            raise ValueError(
                f"heartbeat_mode must be one of {sorted(allowed)}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Keyframe generation config (Layer R)
# ---------------------------------------------------------------------------


class KeyframeRoleOverride(BaseModel):
    """Per-role keyframe overrides (prompt / spec / params).

    All fields optional; populated only where the user wants to deviate
    from the top-level keyframe defaults.
    """

    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class KeyframeConfig(BaseModel):
    """Keyframe-generation block for image-engine pipeline head.

    Presence opts the orchestrator into constructing a KeyframeStage at the
    head of the pipeline.

    Required: ``engine`` (image-engine registry name).
    Required by validator: either ``prompt`` (top-level default) OR
    ``roles.<name>.prompt`` for at least one role.
    """

    engine: str
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    roles: dict[str, KeyframeRoleOverride] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_prompt(self) -> KeyframeConfig:
        """Require at least one non-empty prompt (top-level or per-role)."""
        has_top = bool(self.prompt and self.prompt.strip())
        has_role = any((r.prompt and r.prompt.strip()) for r in self.roles.values())
        if not has_top and not has_role:
            raise ValueError(
                "keyframe block requires either top-level `prompt` "
                "or at least one `roles.<role>.prompt`"
            )
        return self

    @model_validator(mode="after")
    def _role_names_known(self) -> KeyframeConfig:
        """Reject role names not defined in MODE_ROLE_REQUIREMENTS."""
        from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS

        known = {role for roles in MODE_ROLE_REQUIREMENTS.values() for role in roles}
        unknown = set(self.roles) - known
        if unknown:
            raise ValueError(
                f"keyframe.roles contains unknown role(s): {sorted(unknown)}; "
                f"known: {sorted(known)}"
            )
        return self

    def capability_key(self) -> CapabilityKey:
        """Derive a CapabilityKey for image-engine cache lookup.

        Returns:
            A CapabilityKey with base_model and precision from ``spec``,
            loras empty, and engine from ``self.engine``.
        """
        return CapabilityKey(
            base_model=str(self.spec.get("model", "")),
            loras=(),
            engine=self.engine,
            precision=str(self.spec.get("precision", "")),
        )


# ---------------------------------------------------------------------------
# Top-level Config model
# ---------------------------------------------------------------------------


class SplitterConfig(BaseModel):
    """Splitter selection block (optional in YAML; defaults to heuristic).

    Attributes:
        kind: The registry key of the splitter to use. Unknown kinds are
            permitted at load time and surface as ``UnknownAdapter`` at
            ``generate()``, matching engine/provider behaviour.
    """

    kind: str = "heuristic"


class StoreEncryptionConfig(BaseModel):
    """Encryption settings for an ArtifactStore.

    ``mode="default"`` lets the cloud provider apply its bucket-default encryption
    (SSE-S3 on AWS, Google-managed on GCS). ``mode="kms"`` activates client-side
    routing through a caller-owned KMS key.
    """

    mode: Literal["default", "kms"] = "default"
    kms_key_id: str | None = None
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _key_required_for_kms(self) -> Self:
        if self.mode == "kms" and not self.kms_key_id:
            raise ValueError("encryption.mode='kms' requires encryption.kms_key_id")
        return self


class StoreConfig(BaseModel):
    """Optional artifact-store selector.

    Absent block defaults to ``kind="local"``, ``root=None`` — the CLI then
    constructs ``LocalArtifactStore(state_dir)`` from the ``--state-dir``
    argument, matching the pre-Layer-C behaviour.

    Attributes:
        kind: One of ``"local"``, ``"s3"``, ``"gcs"``. Defaults to ``"local"``.
        root: Local-store root directory. Optional; ``None`` → CLI's ``--state-dir``.
        bucket: Cloud bucket name. Required when ``kind in {"s3", "gcs"}``;
            rejected when ``kind == "local"``.
        prefix: Cloud key prefix. Defaults to empty string.
        encryption: Encryption configuration block (Layer W). Defaults to
            ``StoreEncryptionConfig(mode="default")``.
        signed_url_default_ttl_s: Default time-to-live in seconds for signed URLs
            (Layer W). Defaults to 3600 (1 hour).
    """

    kind: Literal["local", "s3", "gcs"] = "local"
    root: Path | None = None
    bucket: str | None = None
    prefix: str = ""
    encryption: StoreEncryptionConfig = Field(default_factory=StoreEncryptionConfig)
    signed_url_default_ttl_s: int = 3600

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> StoreConfig:
        """Enforce kind <-> bucket cross-field invariants."""
        if self.kind in ("s3", "gcs") and not self.bucket:
            raise ValueError(f"store.kind={self.kind!r} requires store.bucket")
        if self.kind == "local" and self.bucket:
            raise ValueError("store.kind='local' does not accept store.bucket")
        return self


class OutputConfig(BaseModel):
    """Optional user-facing output-dir block.

    Absent block defaults to ``kind="local"``, ``dir=Path("output")``,
    ``enabled=True`` — the CLI then constructs a ``LocalOutputSink``
    rooted at ``cwd / "output"`` (Layer O design §5).

    Attributes:
        kind: Registry key of the output sink.  Only ``"local"`` ships
            in v1; cloud-native sinks (S3 mirror, webhook POST) are a
            future layer.
        dir: Local-sink destination directory.  Relative paths are
            resolved against cwd at sink construction.
        enabled: When ``False``, the CLI builds ``sink=None`` and the
            stage skips the publish call (today's behavior).
    """

    kind: Literal["local"] = "local"
    dir: Path = Path("output")
    enabled: bool = True


class SweeperConfig(BaseModel):
    """YAML surface for the Layer W sweeper daemon.

    Default sleeps at 60s — gentle on RunPod GraphQL (B5a smoke measured
    P50=460ms, P99=583ms; ~100x headroom at 60s). Two opt-in policy
    flags extend DEFAULT_APPLY_POLICY:

    - ``include_orphans`` → adds ``ORPHAN_REAP``
    - ``force_forget`` → adds ``UNROUTABLE``

    ``host`` defaults to ``socket.gethostname()`` at CLI level when None.
    """

    interval_s: float = 60.0
    include_orphans: bool = False
    force_forget: bool = False
    host: str | None = None

    @field_validator("interval_s")
    @classmethod
    def _validate_interval(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"sweeper.interval_s must be > 0; got {v}")
        return v


class Config(BaseModel):
    """Top-level kinoforge configuration.

    Attributes:
        mode: Generation mode string (e.g. "t2v", "i2v", "flf2v"). Optional;
            omitting defaults to engine-default behaviour. Keyframe configs
            should always set this so orchestrators and validators can apply
            the correct role contract without inspecting assets at load time.
        prompt: Top-level default prompt string. Optional; per-segment prompts
            in a batch manifest override this. Included as a convenience field
            so operator-facing example configs can carry a representative prompt
            without requiring a manifest file for single-shot runs.
        engine: Engine configuration block.
        models: List of model entries.
        compute: Optional compute block (omitted for hosted engines).
        lifecycle_cfg: Top-level lifecycle config (used for hosted engines).
            Loaded from the YAML ``lifecycle:`` key via an alias.
        splitter: Splitter selection block (defaults to heuristic).
        store: Artifact store selector block (defaults to kind='local').
        output: User-facing output sink block (defaults to kind='local',
            dir='output', enabled=True).
        spec: Engine-agnostic pipeline spec forwarded verbatim to the
            generation job; arbitrary nested structure, defaults to ``{}``.
        params: Engine-agnostic generation parameters forwarded verbatim to
            the generation job; arbitrary nested structure, defaults to ``{}``.
    """

    mode: str | None = None
    prompt: str | None = None
    engine: EngineConfig
    models: list[ModelEntry]
    loras: list[LoraEntry] = []
    compute: ComputeConfig | None = None
    lifecycle_cfg: LifecycleConfig | None = Field(default=None, alias="lifecycle")
    splitter: SplitterConfig = Field(default_factory=SplitterConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    keyframe: KeyframeConfig | None = None
    upscale: UpscaleConfig | None = None
    sweeper: SweeperConfig = Field(default_factory=SweeperConfig)
    # C28 A1.5: opt-in diagnostic mode. When True the engine's render_provision
    # prepends an EXIT trap that captures the boot log + system snapshot and
    # uploads to S3, AND the orchestrator overlays AWS + KINOFORGE_DIAG_* env
    # onto spec.diagnostic_env so the trap finds the bucket. Default False =
    # zero behavioural change.
    diagnostic_mode: bool = False

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _promote_legacy_kind_lora_to_loras_block(cls, data: Any) -> Any:  # noqa: ANN401
        """Auto-migrate legacy cfgs that put LoRAs under ``models:``.

        Reads ``models: [{kind: lora, ...}, ...]``, moves each LoRA
        entry into a new top-level ``loras:`` block (with default
        ``strength=1.0``), removes them from ``models:``. Existing
        explicit ``loras:`` entries win on ordering — they come first
        in the resulting list.

        Emits a ``DeprecationWarning`` when promotion fires so
        operators see which cfgs still ship the legacy shape.

        See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.4.
        """
        if not isinstance(data, dict):
            return data
        models = data.get("models") or []
        legacy_loras = [
            m for m in models if isinstance(m, dict) and m.get("kind") == "lora"
        ]
        if not legacy_loras:
            return data
        non_lora_models = [
            m for m in models if not (isinstance(m, dict) and m.get("kind") == "lora")
        ]
        promoted: list[dict[str, Any]] = []
        for m in legacy_loras:
            entry: dict[str, Any] = {"ref": m["ref"]}
            if m.get("sha256") is not None:
                entry["sha256"] = m["sha256"]
            promoted.append(entry)
        data["models"] = non_lora_models
        data["loras"] = list(data.get("loras") or []) + promoted
        import warnings

        warnings.warn(
            f"cfg uses legacy `models: [{{kind: lora}}, ...]` shape; "
            f"promoted {len(promoted)} entries to top-level `loras:` "
            f"block. Update the cfg to the new shape.",
            DeprecationWarning,
            stacklevel=2,
        )
        return data

    @model_validator(mode="after")
    def _validate_cross_fields(self) -> Self:
        """Validate cross-field constraints after all fields are populated."""
        # Validate engine kind is known
        if self.engine.kind not in KNOWN_ENGINES:
            raise ValueError(
                f"engine.kind {self.engine.kind!r} is unknown; "
                f"valid engines: {sorted(KNOWN_ENGINES)}"
            )

        # Hosted engine must not have a compute block
        if self.engine.kind == "hosted" and self.compute is not None:
            raise ValueError("compute: must not be set when engine.kind == 'hosted'")

        # engine.kind == "fal" requires the engine.fal block.
        if self.engine.kind == "fal" and self.engine.fal is None:
            raise ValueError("engine.kind == 'fal' requires the engine.fal block")
        # engine.kind == "fal" must not have a compute block (hosted-like).
        if self.engine.kind == "fal" and self.compute is not None:
            raise ValueError("compute: must not be set when engine.kind == 'fal'")

        # Validate kind/target consistency for each model entry
        for entry in self.models:
            allowed = VALID_KIND_TARGETS.get(entry.kind, set())
            if entry.target not in allowed:
                raise ValueError(
                    f"inconsistent model entry: kind={entry.kind!r} target={entry.target!r}; "
                    f"allowed targets for {entry.kind!r}: {sorted(allowed)}"
                )

        # Base count: 1 (single-diffusion, e.g. Wan 2.1) or 2 (dual-diffusion,
        # e.g. Wan 2.2 14B HIGH + LOW stages). CapabilityKey concatenates both
        # refs in sorted order when 2 are present (see capability_key()).
        # Upscale-only diffusers cfgs (engine.diffusers.upscale_only=true,
        # `kinoforge upscale`) carry no base model — the spandrel weights
        # land via cfg.upscale.spandrel.model_url at provision time, not via
        # cfg.models[]. Skip the base-count gate in that case.
        upscale_only = (
            self.engine.diffusers is not None and self.engine.diffusers.upscale_only
        )
        base_count = sum(1 for e in self.models if e.kind == "base")
        if base_count == 0 and not upscale_only:
            raise ValueError(
                "models: must contain at least one entry with kind: base (found 0)"
            )
        if base_count > 2:
            raise ValueError(
                f"models: must contain 1 or 2 entries with kind: base "
                f"(found {base_count}); dual-diffusion architectures use 2, "
                f"single-diffusion uses 1"
            )

        # Validate lifecycle timing constraints
        lc = self._effective_lifecycle_config()
        if lc is not None:
            if lc.idle_timeout >= lc.max_lifetime:
                raise ValueError(
                    f"idle_timeout ({lc.idle_timeout}s) must be < "
                    f"max_lifetime ({lc.max_lifetime}s)"
                )
            if lc.job_timeout > lc.max_lifetime:
                raise ValueError(
                    f"job_timeout ({lc.job_timeout}s) must be <= "
                    f"max_lifetime ({lc.max_lifetime}s)"
                )

        return self

    def _effective_lifecycle_config(self) -> LifecycleConfig | None:
        """Return the effective lifecycle config (compute path or top-level hosted path).

        Returns:
            The LifecycleConfig that applies, or None if no lifecycle is configured.
        """
        if self.compute is not None and self.compute.lifecycle is not None:
            return self.compute.lifecycle
        return self.lifecycle_cfg

    def capability_key(self) -> CapabilityKey:
        """Derive a CapabilityKey from the config for cache lookup.

        VAE entries are excluded from the key (they don't affect generation capability).
        LoRA entries are included in declaration order. When two ``kind: base``
        entries are present (dual-diffusion, e.g. Wan 2.2 14B HIGH + LOW),
        their refs are concatenated in sorted order with a ``|`` separator
        so the fingerprint is stable regardless of declaration order.

        Returns:
            A CapabilityKey with base_model, loras, engine, and precision.

        Raises:
            ConfigError: If no base model is found in the models list.
        """
        # P1 (2026-06-21): LoRA refs source from self.loras (new top-level
        # block). Strength is deliberately excluded — mutable per-run
        # parameter applied via /lora/set_stack on warm-attach, not part
        # of the identity hash. Same-refs / different-strength runs
        # reuse the warm pod. See spec §7.
        base_refs: list[str] = []
        loras: list[str] = [lo.ref for lo in self.loras]
        for entry in self.models:
            if entry.kind == "base":
                base_refs.append(entry.ref)
            # vae / text_encoder / clip_vision: skip entirely

        upscale_only = (
            self.engine.diffusers is not None and self.engine.diffusers.upscale_only
        )
        if not base_refs and not upscale_only:
            raise ConfigError("no model entry with kind: base found in config")

        if base_refs:
            base_model = (
                base_refs[0] if len(base_refs) == 1 else "|".join(sorted(base_refs))
            )
        else:
            # Upscale-only cfgs derive identity from the upscaler weights ref
            # so two pods running spandrel against different SR weights stay
            # distinct in the matcher.
            assert self.upscale is not None  # noqa: S101 — guarded by upscale_only branch
            if self.upscale.spandrel is not None:
                base_model = self.upscale.spandrel.model_url
            elif self.upscale.seedvr2 is not None:
                base_model = self.upscale.seedvr2.weights_ref or ""
            elif self.upscale.flashvsr is not None:
                base_model = self.upscale.flashvsr.weights_bundle
            else:
                base_model = self.upscale.engine

        # 2026-06-28: stages / upscaler factors. Pure-generate cfgs (no
        # upscale block) leave stages=() so derive() preserves the legacy
        # hash space.
        stages: list[str] = []
        upscaler = ""
        upscaler_precision = ""
        if self.upscale is not None:
            if not upscale_only:
                stages.append("t2v")
            stages.append("upscale")
            upscaler = self.upscale.engine
            if self.upscale.seedvr2 is not None:
                upscaler_precision = (
                    f"{self.upscale.seedvr2.variant.lower()}-"
                    f"{self.upscale.seedvr2.precision}"
                )
            elif self.upscale.spandrel is not None:
                upscaler_precision = self.upscale.spandrel.precision
            elif self.upscale.flashvsr is not None:
                upscaler_precision = self.upscale.flashvsr.precision

        return CapabilityKey(
            base_model=base_model,
            loras=tuple(loras),
            engine=self.engine.kind,
            precision=self.engine.precision,
            stages=tuple(stages),
            upscaler=upscaler,
            upscaler_precision=upscaler_precision,
        )

    def lifecycle(self) -> InterfaceLifecycle:
        """Return an InterfaceLifecycle with defaults applied.

        Prefers compute.lifecycle when present (non-hosted path);
        falls back to top-level lifecycle (hosted path).
        Defaults are: idle_timeout=2h, job_timeout=30m, time_buffer=30m, max_lifetime=5h, boot_timeout=15m.

        Returns:
            An interfaces.Lifecycle populated with seconds values.
        """
        lc = self._effective_lifecycle_config()
        if lc is None:
            # Return pure defaults
            return InterfaceLifecycle()

        return InterfaceLifecycle(
            idle_timeout_s=lc.idle_timeout,
            job_timeout_s=lc.job_timeout,
            time_buffer_s=lc.time_buffer,
            max_lifetime_s=lc.max_lifetime,
            budget_usd=lc.budget,
            max_in_flight=lc.max_in_flight,
            boot_timeout_s=lc.boot_timeout,
            heartbeat_interval_s=lc.heartbeat_interval_s,
            grace_after_session_s=lc.grace_after_session_s,
            stall_window_s=lc.stall_window_s if lc.stall_reap_enabled else None,
            stall_gpu_threshold=lc.stall_gpu_threshold,
            stall_cpu_threshold=lc.stall_cpu_threshold,
            restart_loop_window_s=(
                lc.restart_loop_window_s if lc.restart_loop_reap_enabled else None
            ),
            restart_loop_uptime_threshold_s=lc.restart_loop_uptime_threshold_s,
            lora_swap_re_probe_after_s=lc.lora_swap_re_probe_after_s,
        )

    def hardware_requirements(self) -> InterfaceHardwareRequirements:
        """Return HardwareRequirements with defaults applied.

        Pulls from compute.requirements when present; returns all-defaults otherwise.
        Defaults: min_vram_gb=48, min_cuda="12.8", max_usd_per_hr=2.20,
        disk_gb=100, gpu_preference=().

        Returns:
            An interfaces.HardwareRequirements instance.
        """
        if self.compute is None:
            return InterfaceHardwareRequirements()

        r = self.compute.requirements
        return InterfaceHardwareRequirements(
            min_vram_gb=r.min_vram_gb,
            min_cuda=r.min_cuda,
            max_usd_per_hr=r.max_usd_per_hr,
            gpu_preference=tuple(r.gpu_preference),
            disk_gb=r.disk_gb,
        )


# ---------------------------------------------------------------------------
# spec.graph_file loader helper
# ---------------------------------------------------------------------------


def _resolve_spec_graph_file(data: dict[str, Any], yaml_path: Path) -> None:
    """Inline ``spec.graph_file`` into ``spec.graph`` in-place.

    When ``data["spec"]`` contains a ``"graph_file"`` key, reads the referenced
    JSON file (resolving relative paths against ``yaml_path.parent``), parses it,
    and stores the result as ``data["spec"]["graph"]``, then removes the
    ``"graph_file"`` key.

    This is a no-op when neither ``"graph_file"`` nor ``"graph"`` is present in
    ``spec``, and when only ``"graph"`` is present.

    Args:
        data: The raw YAML dict (mutated in place — only ``data["spec"]`` is
            touched).
        yaml_path: Path to the YAML file; used to resolve relative
            ``graph_file`` paths against its parent directory.  Need not be
            absolute, but must point to the correct parent.  When loading from
            a raw YAML string (no file backing), the sentinel value
            ``Path.cwd() / "<string>"`` is passed; in that case relative
            ``graph_file`` paths are rejected with a :exc:`ConfigError`.

    Raises:
        ConfigError: If both ``graph_file`` and ``graph`` are set, if the
            referenced file does not exist, or if the file contains invalid JSON.
    """
    spec = data.get("spec")
    if not isinstance(spec, dict):
        return
    if "graph_file" not in spec:
        return

    if "graph" in spec:
        raise ConfigError(
            "spec: cannot set both 'graph_file' and 'graph'; use one or the other"
        )

    graph_file_str: str = spec["graph_file"]
    graph_file_path = Path(graph_file_str)

    # If we came from a raw YAML string (no file backing), the sentinel
    # yaml_path has name "<string>" and parent=cwd.  Relative graph_file paths
    # would silently resolve against cwd — confusing.  Force absolute paths in
    # this mode.
    if yaml_path.name == "<string>" and not graph_file_path.is_absolute():
        raise ConfigError(
            "spec.graph_file requires a file-based config when using a relative "
            "path; pass an absolute path or load from a YAML file"
        )

    if not graph_file_path.is_absolute():
        graph_file_path = yaml_path.parent / graph_file_path

    if not graph_file_path.exists():
        raise ConfigError(f"spec.graph_file: file not found: {graph_file_path}")

    try:
        raw_json = graph_file_path.read_text(encoding="utf-8")
        graph_data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"spec.graph_file: invalid JSON in {graph_file_path}: {exc}"
        ) from exc

    # Strip the optional top-level _meta provenance header before runtime.
    # ComfyUI's /prompt endpoint validates every top-level key as a node ID
    # and rejects unrecognised keys; on-disk JSON retains _meta for AC12's
    # SHA cross-reference test that reads the raw file directly.
    if isinstance(graph_data, dict):
        graph_data.pop("_meta", None)

    spec["graph"] = graph_data
    del spec["graph_file"]


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_config(text_or_path: str | Path) -> Config:
    """Load a kinoforge YAML config from a string or file path.

    Accepts either a YAML string (detected by the presence of a newline or
    absence of an existing file at the given string path) or a
    ``pathlib.Path`` / ``str`` filepath.

    Args:
        text_or_path: A YAML string or a path to a YAML file.

    Returns:
        A validated :class:`Config` instance.

    Raises:
        ConfigError: If the YAML is malformed, a required field is missing,
            or any cross-field validation constraint is violated.
    """
    import pydantic

    # Determine whether this is raw YAML text or a file path; track the resolved
    # path for spec.graph_file relative-path resolution.
    yaml_path: Path
    if isinstance(text_or_path, Path):
        yaml_path = text_or_path.resolve()
        text = text_or_path.read_text(encoding="utf-8")
    elif "\n" in str(text_or_path) or not Path(str(text_or_path)).exists():
        text = str(text_or_path)
        yaml_path = Path.cwd() / "<string>"
    else:
        yaml_path = Path(str(text_or_path)).resolve()
        text = yaml_path.read_text(encoding="utf-8")

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping at the top level")

    # Inline spec.graph_file before schema validation so the model sees spec.graph.
    _resolve_spec_graph_file(raw, yaml_path)

    try:
        cfg = Config.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise ConfigError(str(exc)) from exc

    # Run the STATIC-only Check Registry pass (Task 10). NETWORK +
    # PREFLIGHT categories deliberately do NOT fire here — they belong
    # to `kinoforge generate` pre-flight and `kinoforge doctor`, which
    # call validate_for_generate / validate_for_doctor respectively.
    # Local import: kinoforge.validation imports kinoforge.core.errors,
    # so a top-level import is safe — but built-in checks live under
    # kinoforge.validation.checks and import kinoforge.core.config; the
    # transitive cycle would deadlock module init without the lazy hop.
    # Provider check registrations go through the canonical adapter hub
    # so the `core/ → providers/` layering invariant stays intact.
    import kinoforge._adapters  # noqa: F401 — self-register every adapter (incl. provider checks)
    import kinoforge.validation.checks  # noqa: F401 — self-register built-ins
    from kinoforge.validation import validate_for_load

    report = validate_for_load(cfg)
    return report.cfg if isinstance(report.cfg, Config) else cfg


def _parse_cfg_raw(text: str, *, yaml_path: Path | None = None) -> Config:
    """Parse the cfg via Pydantic only, without the Check Registry pass.

    Used by ``kinoforge doctor`` so the full validation report can be
    assembled instead of raising on the first STATIC error. Production
    callers should use :func:`load_config` instead.

    Args:
        text: YAML text body.
        yaml_path: Optional resolved path the YAML came from; needed so
            ``spec.graph_file`` relative references resolve against the
            cfg's directory rather than ``<string>``.
    """
    import pydantic

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping at the top level")
    _resolve_spec_graph_file(raw, yaml_path or Path.cwd() / "<string>")
    try:
        return Config.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise ConfigError(str(exc)) from exc


def sweeper_policy_from_cfg(cfg: Config) -> Policy:
    """Build the Layer W daemon's Policy from cfg.sweeper.

    Starts with Layer V :data:`DEFAULT_APPLY_POLICY` (IDLE_REAP,
    OVERAGE_REAP, STALE_LEDGER) and unions the two opt-in verdicts based
    on YAML flags.

    Args:
        cfg: Loaded :class:`Config`; ``cfg.sweeper`` is consulted.

    Returns:
        :class:`Policy` with the resulting frozenset.
    """
    act = set(DEFAULT_APPLY_POLICY.act_verdicts)
    if cfg.sweeper.include_orphans:
        act.add(Verdict.ORPHAN_REAP)
    if cfg.sweeper.force_forget:
        act.add(Verdict.UNROUTABLE)
    return Policy(act_verdicts=frozenset(act))
