"""Abstract interfaces and data containers — the only module core logic depends on.

No concrete provider/source/engine may be imported here. Adapters depend on this
module, never the reverse.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, runtime_checkable

from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.scale_target import ScaleTarget

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.runtime_probe import RuntimeProbe

# --- compute axis -----------------------------------------------------------


@dataclass(frozen=True)
class HardwareRequirements:
    """Filter applied by ComputeProvider.find_offers; every field config-overridable.

    Attributes:
        min_vram_gb: Minimum GPU VRAM in GB; offers below this are excluded.
        min_cuda: Minimum CUDA version string (semantic compare, e.g. "12.8").
        max_usd_per_hr: Ceiling for pod-mode offers; serverless ignores.
        gpu_preference: Ordered preference list among surviving offers.
        disk_gb: Minimum container/instance disk in GB.
    """

    min_vram_gb: int = 48
    min_cuda: str = "12.8"
    max_usd_per_hr: float = 2.20
    gpu_preference: tuple[str, ...] = ()
    disk_gb: int = 100


@dataclass(frozen=True)
class Offer:
    """A bookable compute offer returned by a provider."""

    id: str
    gpu_type: str
    vram_gb: int
    cuda: str
    cost_rate_usd_per_hr: float
    mode: str = "pod"  # "pod" | "serverless"


@dataclass
class Lifecycle:
    """Cost-safety guardrails carried into an InstanceSpec (all seconds).

    Attributes:
        heartbeat_interval_s: Layer U — seconds between background
            HeartbeatLoop ticks inside an active deploy_session.
            ``None`` (the default) disables the feature, preserving
            backwards-compatibility for every existing YAML config.
            Operator guidance: values < 10 risk lock contention at scale.
        grace_after_session_s: Layer V — post-session warm-reuse window
            within which a sentinel-stale, pod-up entry is treated as
            LIVE rather than ORPHAN_REAP. Default 1800 (30 minutes).
            Prevents the reaper from racing a legitimate session start
            on a warm-reused pod whose first HeartbeatLoop tick has not
            yet fired. Default was 300 s before 2026-06-18 when the Wan
            14B warm-reuse smoke caught the trap: operator-typing-pace
            (~5 min between cmd 1 finish and cmd 2 start) was crossing
            the boundary and forcing cold create. 1800 matches typical
            "fire two related commands while skim-reading the output"
            cadence with headroom for follow-up `kinoforge list` /
            `kinoforge status` interludes.
    """

    idle_timeout_s: float = 2 * 3600
    job_timeout_s: float = 30 * 60
    time_buffer_s: float = 30 * 60
    max_lifetime_s: float = 5 * 3600
    budget_usd: float = 0.0
    max_workers: int = 1
    max_in_flight: int = 1
    boot_timeout_s: float = 900.0
    # C26 — populated by Config.lifecycle() from compute.lifecycle when set.
    stall_window_s: float | None = None
    stall_gpu_threshold: float = 5.0
    stall_cpu_threshold: float = 20.0
    # C27 — sibling util-aware predicate (chronic container restart loop).
    restart_loop_window_s: float | None = None
    restart_loop_uptime_threshold_s: float = 90.0
    heartbeat_interval_s: float | None = None
    grace_after_session_s: float = 1800.0
    # LoRA-flexible warm-reuse: staleness threshold for the matcher's
    # pod-side free-disk + inventory snapshot. 0 disables the stale check.
    lora_swap_re_probe_after_s: float = 300.0


@dataclass(frozen=True)
class RenderedProvision:
    """Engine-emitted bootstrap payload for a remote pod / VM.

    Attributes:
        script: Self-contained bash script. Must be idempotent on warm pods.
            Reference credentials only via ``$VAR``; never embed literal
            credential values. The orchestrator lifts ``env_required``
            entries onto ``spec.env`` before pod creation.
        run_cmd: Long-running command launched after the script completes.
            Convention: the script ends with ``exec <run_cmd>`` so the run
            cmd becomes the container's PID 1.
        image: Container image to boot. Defaults to a stock provider image
            (see engine impl).
        ports: Ports the engine listens on. Provider exposes via its native
            mechanism (RunPod proxy, Sky port forward).
        env_required: Names of credential env vars the script references.
            Orchestrator validates each is reachable via the configured
            ``CredentialProvider`` before ``provider.create_instance``;
            lifts onto ``spec.env``.
    """

    script: str
    run_cmd: list[str]
    image: str
    ports: list[str]
    env_required: list[str]


@dataclass
class InstanceSpec:
    """Everything needed to create an instance, including guardrails + tags."""

    image: str
    offer: Offer | None = None
    ports: tuple[str, ...] = ()
    volume_gb: int = 0
    volume_mount: str = ""
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    env: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    provision_script: str | None = None
    run_cmd: list[str] | None = None
    spot: bool = False  # Request a spot/preemptible instance when True
    # C28 A1.5: diagnostic env overlay merged into pod env via setdefault
    # (user-supplied `env` always wins). Default empty = no behavioural change.
    diagnostic_env: dict[str, str] = field(default_factory=dict)
    # C28 A3: when "never" AND provider schema supports it, request the
    # provider NOT to auto-restart this pod on container exit. Default
    # "always" preserves pre-C28 behaviour. RunPod schema probed by the A0
    # sidecar (tests/live/_c28_runpod_input_schema_probe.json); if the field
    # is absent the provider warns + skips on the wire.
    restart_policy: Literal["always", "never"] = "always"
    # 2026-07-03: host-pool pin. "any" preserves the historical
    # cloudType=ALL behaviour (cheapest capacity, often community hosts —
    # whose interruption DELETES zero-volume pods outright; three BSA
    # wheel builds died that way). "secure" pins dedicated hosts for
    # long-running one-shot workloads; "community" forces the cheap pool.
    cloud_type: Literal["any", "secure", "community"] = "any"


@dataclass
class Instance:
    """A created compute instance."""

    id: str
    provider: str
    status: str  # "starting" | "ready" | "stopped" | "terminated"
    created_at: float
    endpoints: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    cost_rate_usd_per_hr: float = 0.0


@dataclass
class Artifact:
    """Addressable content handle: download target, store item, or generated output.

    The download case carries url/filename/size/sha256/headers; the store case carries
    a uri set by the ArtifactStore once materialized.
    """

    filename: str = ""
    url: str = ""
    size: int | None = None
    sha256: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    uri: str = ""
    meta: dict = field(default_factory=dict)  # type: ignore[type-arg]


class CredentialProvider(ABC):
    """Resolves named secrets; env-backed by default."""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the secret for ``key`` or ``None`` if unset."""


class ComputeProvider(ABC):
    """A place to run GPU workloads. Instances created with cost guardrails."""

    name: str

    @abstractmethod
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]: ...  # noqa: D102

    @abstractmethod
    def create_instance(self, spec: InstanceSpec) -> Instance: ...  # noqa: D102

    @abstractmethod
    def get_instance(self, instance_id: str) -> Instance: ...  # noqa: D102

    @abstractmethod
    def list_instances(self) -> list[Instance]: ...  # noqa: D102

    @abstractmethod
    def stop_instance(self, instance_id: str) -> None: ...  # noqa: D102

    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None: ...  # noqa: D102

    @abstractmethod
    def heartbeat(self, instance_id: str) -> None: ...  # noqa: D102

    def probe_runtime(self, pod_id: str) -> RuntimeProbe | None:  # noqa: B027
        """Live runtime probe for sweeper-ephemeral-reap.

        Default returns ``None`` ("substrate missing"). RunPodProvider
        overrides; SkyPilot / Local inherit the default. Sweeper treats
        ``None`` as a WARN-once skip per spec 2026-06-28.

        Args:
            pod_id: Provider-side pod identifier.

        Returns:
            A :class:`RuntimeProbe` populated from a live provider query,
            or ``None`` when the provider lacks runtime-probe substrate.
        """
        return None

    def set_heartbeat_endpoint(  # noqa: B027
        self,
        endpoint: object | None,
    ) -> None:
        """Install a HeartbeatEndpoint post-construction (B5a).

        Default implementation is a no-op so providers that do not yet
        support the heartbeat substrate (e.g. SkyPilot pre-B5b, Local)
        silently accept the call. RunPodProvider overrides to wire the
        endpoint into its ``heartbeat()`` / ``last_heartbeat()`` paths.

        ``endpoint`` is typed as ``object | None`` (not
        ``HeartbeatEndpoint | None``) to keep ``core/interfaces.py`` free
        of any heartbeat-module import — the Protocol satisfaction is
        verified at the call site, not the type-system seam.

        Args:
            endpoint: A :class:`HeartbeatEndpoint`-Protocol-satisfying
                instance, or ``None`` to clear.
        """
        # Default: ignore. Providers that wire heartbeat override.

    @abstractmethod
    def endpoints(self, instance: Instance) -> dict[str, str]: ...  # noqa: D102


class ModelSource(ABC):
    """Resolves a vendor-neutral ref into downloadable Artifact(s)."""

    scheme: str

    @abstractmethod
    def handles(self, ref: str) -> bool: ...  # noqa: D102

    @abstractmethod
    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]: ...  # noqa: D102


# --- generation layer -------------------------------------------------------


@dataclass(frozen=True)
class WarmAttachKey:
    """The slow-to-rebuild part of a pod's identity.

    Carries (base_model, engine, precision) — the expensive bytes that
    a warm pod has already paid for and which a new generation job
    should NOT trigger re-download for. See
    docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md.

    Extended in 2026-06-28 with stages / upscaler / upscaler_precision
    factors. derive() participates in the hash only when non-default,
    preserving the legacy hash space for pure-generate pods.

    Attributes:
        base_model: Base-model vendor-neutral ref (e.g. "hf:org/m").
        engine: Engine name (capability is engine-specific).
        precision: Precision/quantization (e.g. "fp16", "gguf-q8").
        stages: Pipeline stages this pod actually supports. Empty tuple
            preserves the legacy hash space.
        upscaler: Upscaler registry key when ``stages`` includes ``"upscale"``.
        upscaler_precision: Variant+precision slug for the upscaler.
    """

    base_model: str
    engine: str = ""
    precision: str = ""
    stages: tuple[str, ...] = ()
    upscaler: str = ""
    upscaler_precision: str = ""

    def derive(self) -> str:
        """Backward-compat hash; conditional-extend mirrors CapabilityKey."""
        base: list[object] = [self.base_model, self.engine, self.precision]
        if self.stages or self.upscaler or self.upscaler_precision:
            base.extend([list(self.stages), self.upscaler, self.upscaler_precision])
        payload = json.dumps(base, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoraStack:
    """The cheap-to-swap part of a pod's identity. Ordered.

    Order matters: LoraStack(refs=("a","b")) != LoraStack(refs=("b","a")).
    The order participates in CapabilityKey identity and in pipeline
    adapter ordering (set_adapters([...]) applies in list order).
    """

    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityKey:
    """Full identity a ModelProfile depends on. derive() is the stable cache key.

    Composite over WarmAttachKey + LoraStack + (new in 2026-06-28) stage tags.
    derive() produces byte-identical output to the pre-stages-factor
    implementation when ``stages == ()`` AND ``upscaler == ""`` AND
    ``upscaler_precision == ""``, so every existing ledger entry keeps matching.

    Attributes:
        base_model: Base-model vendor-neutral ref (e.g. "hf:org/m").
        loras: Ordered LoRA stack; order matters and contributes to the key.
        engine: Engine name (capability is engine-specific).
        precision: Precision/quantization (e.g. "fp16", "gguf-q8").
        stages: Pipeline stages this cfg/pod actually supports. Empty tuple
            preserves the legacy hash space (a pure-generate pod or cfg with
            no upscale block). Non-empty values participate in the hash.
        upscaler: Upscaler registry key when ``stages`` includes ``"upscale"``.
        upscaler_precision: Variant+precision slug for the upscaler
            (e.g. ``"3b-fp8"`` for SeedVR2 3B FP8).
    """

    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""
    stages: tuple[str, ...] = ()
    upscaler: str = ""
    upscaler_precision: str = ""

    def derive(self) -> str:
        """Stable, order-sensitive sha256 over all fields.

        Backward-compat invariant: when ``stages == ()`` AND ``upscaler == ""``
        AND ``upscaler_precision == ""``, derive() returns byte-identical
        output to the pre-change implementation. Enforced by the
        conditional-extend below — the legacy payload shape is preserved
        whenever the new fields are at their defaults.
        """
        base: list[object] = [
            self.base_model,
            list(self.loras),
            self.engine,
            self.precision,
        ]
        if self.stages or self.upscaler or self.upscaler_precision:
            base.extend([list(self.stages), self.upscaler, self.upscaler_precision])
        payload = json.dumps(base, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def warm_attach_key(self) -> WarmAttachKey:
        """Return the WarmAttachKey factor with upscale-aware fields populated."""
        return WarmAttachKey(
            base_model=self.base_model,
            engine=self.engine,
            precision=self.precision,
            stages=self.stages,
            upscaler=self.upscaler,
            upscaler_precision=self.upscaler_precision,
        )

    def lora_stack(self) -> LoraStack:
        """Return the LoraStack factor (ordered LoRA refs)."""
        return LoraStack(refs=self.loras)


@dataclass
class ModelProfile:
    """Capabilities read at plan time from the cache keyed by CapabilityKey."""

    name: str
    max_frames: int
    fps: int
    supported_modes: set[str]
    max_resolution: tuple[int, int]
    supports_native_extension: bool
    supports_joint_audio: bool

    @property
    def max_segment_seconds(self) -> float:
        """Per-clip length budget = max_frames / fps."""
        return self.max_frames / self.fps


MODE_ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "t2v": {},
    "i2v": {"init_image": "image"},
    "flf2v": {"first_frame": "image", "last_frame": "image"},
}


# --- image generation siblings (Layer R) --------------------------------------


@dataclass
class ImageProfile:
    """Capabilities of an image-generation model, read at plan time from cache.

    Sibling of ModelProfile (the video one) but image-shaped only.
    No fps / max_frames / native_extension / joint_audio.
    """

    name: str
    max_resolution: tuple[int, int]
    supported_modes: set[str]


@dataclass
class ImageJob:
    """One image-generation unit of work.

    Sibling of GenerationJob but no segments concept — one prompt → one image.
    """

    spec: dict  # type: ignore[type-arg]
    prompt: str
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


class ImageBackend(ABC):
    """A live, ready image engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ImageProfile:
        """Return the in-force profile (cached/configured) for this backend."""

    @abstractmethod
    def inspect_capabilities(self) -> ImageProfile:
        """Live-probe the backend to discover capabilities fresh (for profile-cache discover/verify)."""

    @abstractmethod
    def submit(self, job: ImageJob) -> str: ...  # noqa: D102

    @abstractmethod
    def result(self, job_id: str) -> Artifact: ...  # noqa: D102

    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...  # noqa: D102


class ImageEngine(ABC):
    """A swappable image-generation engine; owns its env setup; knows if it needs compute."""

    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(  # noqa: D102
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def backend(  # noqa: D102
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend: ...

    @abstractmethod
    def profile_for(self, key: CapabilityKey) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def validate_spec(self, job: ImageJob) -> None: ...  # noqa: D102

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return a human-readable model slug for keyframe sink filenames.

        Display-only; independent of CapabilityKey / cache identity. Image
        engines return the most specific human-grep-able surface they
        natively interpret: hosted -> ``cfg["spec"]["model"]``, fal ->
        ``cfg["engine"]["fal"]["endpoint"]``.

        MUST NOT raise on a missing / empty source — return ``""`` instead.
        The orchestrator logs a single WARNING and the keyframe sink falls
        back to the literal ``"unknown"``.

        Args:
            cfg: Runtime configuration dict (same shape the image engine
                receives in ``backend()`` and ``validate_spec()``; for the
                keyframe path this is the keyframe sub-cfg).

        Returns:
            Engine-native raw slug (slugified downstream by the sink) or
            ``""`` when the underlying field is absent / empty.
        """
        ...


def required_image_roles(mode: str) -> list[str]:
    """Return ordered list of image-kind roles required by ``mode``.

    Order is dict-insertion order from MODE_ROLE_REQUIREMENTS so flf2v always
    returns [first_frame, last_frame], never [last_frame, first_frame].

    Schema-shape-agnostic: handles BOTH the pre-T2 ``dict[str, set[str]]`` shape
    and the post-T2 ``dict[str, dict[str, str]]`` shape. After T2 the kind
    filter is meaningful; before T2 every role is treated as image-kind
    (correct because all current roles are image-kind today).
    """
    roles = MODE_ROLE_REQUIREMENTS.get(mode, ())
    if isinstance(roles, dict):
        return [role for role, kind in roles.items() if kind == "image"]
    return list(roles)


@dataclass(frozen=True)
class PipelineState:
    """State threaded between pipeline stages.

    Frozen wrapper; stages produce a new state via ``dataclasses.replace``.
    The artifacts dict is mutable in-place (matches the project pattern where
    dataclass.replace handles top-level swaps but contained collections may
    be mutated for clarity).

    Keys in ``artifacts`` are stage-defined names. KeyframeStage writes
    ``keyframe-<role>`` (e.g. ``keyframe-init_image``, ``keyframe-first_frame``).
    GenerateClipStage writes ``clip``. Future stages: ``audio``, ``upscaled``,
    ``stitched``, etc.
    """

    request: GenerationRequest
    artifacts: dict[str, Artifact] = field(default_factory=dict)


@dataclass
class ConditioningAsset:
    """A non-text input filling a model slot identified by ``role``."""

    kind: str  # open enum: "image" | "audio" | "video" | ...
    role: str
    ref: Artifact
    meta: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass
class GenerationRequest:
    """Top-level input: one prompt, an explicit mode, role-tagged assets."""

    prompt: str
    mode: str
    assets: list[ConditioningAsset] = field(default_factory=list)


@dataclass
class Segment:
    """One clip's worth of plan: prompt + effective assets + per-clip overrides."""

    prompt: str
    assets: list[ConditioningAsset] = field(default_factory=list)
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass
class GenerationJob:
    """One unit of work: engine-interpreted spec + neutral params + ordered segments."""

    spec: dict  # type: ignore[type-arg]
    segments: list[Segment]
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class UpscaleJob:
    """One unit of upscale work — engine-agnostic.

    No prompt, no segments, no LoRA stack — upscaling is video-in / video-out.

    Attributes:
        source: Input video Artifact (uri set by ArtifactStore or pointing at a
            local path readable by the engine).
        scale: ScaleTarget. v1 engines MUST raise NotYetImplementedError on
            ``kind="height"``.
        params: Engine-specific overrides (e.g. tile_size, steps, denoise);
            engines validate via ``validate_spec``.
    """

    source: Artifact
    scale: ScaleTarget
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class UpscaleResult:
    """Output of one upscale job.

    Attributes:
        artifact: Rendered upscaled video.
        input_resolution: ``(width, height)`` measured from the source clip.
        output_resolution: ``(width, height)`` of the rendered output.
        elapsed_s: Wall-clock seconds spent inside the engine.
        engine_meta: Engine-specific telemetry (e.g. SeedVR2 tile count,
            denoise steps used). Free-form open dict.
    """

    artifact: Artifact
    input_resolution: tuple[int, int]
    output_resolution: tuple[int, int]
    elapsed_s: float
    engine_meta: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class InterpolateJob:
    """One unit of frame-interpolation work — engine-agnostic.

    Attributes:
        source: Input video Artifact (uri local or pod-fetchable).
        target_fps: Requested output frame rate.
        params: Engine-specific overrides (model tag, precision); engines
            validate via ``validate_spec``.
    """

    source: Artifact
    target_fps: float
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class InterpolateResult:
    """Output of one interpolation job.

    Attributes:
        artifact: Rendered interpolated video.
        input_fps: Probed source frame rate.
        output_fps: Delivered frame rate.
        input_frame_count: Source frame count.
        output_frame_count: Delivered frame count.
        elapsed_s: Wall-clock seconds inside the engine.
        engine_meta: Free-form engine telemetry.
    """

    artifact: Artifact
    input_fps: float
    output_fps: float
    input_frame_count: int
    output_frame_count: int
    elapsed_s: float
    engine_meta: dict = field(default_factory=dict)  # type: ignore[type-arg]


class ModelProfileProvider(ABC):
    """A cache of ModelProfiles keyed by CapabilityKey."""

    @abstractmethod
    def resolve(self, key: CapabilityKey) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def discover(  # noqa: D102
        self, key: CapabilityKey, engine: GenerationEngine, backend: GenerationBackend
    ) -> ModelProfile: ...

    @abstractmethod
    def verify(  # noqa: D102
        self,
        profile: ModelProfile,
        backend: GenerationBackend,
        *,
        engine: GenerationEngine | None = None,
        key: CapabilityKey | None = None,
    ) -> None: ...


class ImageProfileProvider(ABC):
    """A cache of ImageProfiles keyed by CapabilityKey (image-side)."""

    @abstractmethod
    def resolve(self, key: CapabilityKey) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def discover(  # noqa: D102
        self, key: CapabilityKey, engine: ImageEngine, backend: ImageBackend
    ) -> ImageProfile: ...

    @abstractmethod
    def verify(  # noqa: D102
        self,
        profile: ImageProfile,
        backend: ImageBackend,
        *,
        engine: ImageEngine | None = None,
        key: CapabilityKey | None = None,
    ) -> None: ...


class Splitter(ABC):
    """Convert a long-form prompt into ordered ``Segment`` objects.

    A splitter is a pure function: deterministic, side-effect-free, no I/O.
    The output list must contain at least one ``Segment``; each segment carries
    only ``prompt``.  ``assets`` and ``params`` default to empty — asset
    attachment is performed by the orchestrator and per-segment param merging
    by ``strategy.decide``.

    Attributes:
        name: The registry key under which the splitter is registered.
    """

    name: str

    @abstractmethod
    def split(  # noqa: D102
        self,
        prompt: str,
        profile: ModelProfile,
        params: dict,  # type: ignore[type-arg]
    ) -> list[Segment]: ...


class GenerationBackend(ABC):
    """A live, ready engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def inspect_capabilities(self) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def submit(  # noqa: D102
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str: ...

    @abstractmethod
    def result(  # noqa: D102
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact: ...

    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...  # noqa: D102


class GenerationEngine(ABC):
    """A swappable generation engine; owns its env setup; knows if it needs compute."""

    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(  # noqa: D102
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def backend(  # noqa: D102
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> GenerationBackend: ...

    @abstractmethod
    def profile_for(self, key: CapabilityKey) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]: ...  # noqa: D102

    @abstractmethod
    def validate_spec(self, job: GenerationJob) -> None: ...  # noqa: D102

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return a human-readable model slug for sink filenames.

        Display-only; independent of CapabilityKey / cache identity (see
        ``HostedAPIEngine.key_base``).  Engines return the most specific
        human-grep-able surface they natively interpret: hosted ->
        ``cfg["spec"]["model"]``, fal -> ``cfg["engine"]["fal"]["endpoint"]``,
        comfyui -> filename stem of the ``kind == "base"`` entry in
        ``cfg["models"]``, etc.

        ``cfg`` is the same dict shape the engine receives in ``backend()``
        and ``validate_spec()``.  For the keyframe path that is the keyframe
        sub-cfg the stage feeds into the image engine, not the top-level
        Config.

        MUST NOT raise on a missing / empty source — return ``""`` instead.
        The orchestrator logs a single WARNING and the sink falls back to
        the literal ``"unknown"``.

        Args:
            cfg: Runtime configuration dict.

        Returns:
            Engine-native raw slug (slugified downstream by the sink) or
            ``""`` when the underlying field is absent / empty.
        """
        ...

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Decode the last frame of a rendered clip as PNG bytes.

        Default raises; subclass to enable continuity for this engine.

        Args:
            artifact: A clip Artifact returned by backend.result(). The
                ``url`` field (populated by the engine's ``result()`` impl,
                not the ``uri`` field which is set later by ArtifactStore
                materialization) must point at a fetchable location of the
                rendered video bytes.

        Returns:
            PNG-encoded bytes of the last frame.

        Raises:
            NotImplementedError: Engine doesn't support tail-frame extraction.
            FrameExtractionError: Extraction failed at fetch or decode time.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support tail-frame extraction"
        )

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit the first-boot bootstrap payload for this engine.

        Engines that support remote provisioning (ComfyUI, Diffusers) override
        this. Engines with ``requires_compute=False`` (Hosted) raise
        ``NotImplementedError``. The orchestrator only calls this for engines
        with remote-capable providers.

        Args:
            cfg: Runtime configuration dict (same shape passed to ``provision``).

        Returns:
            A :class:`RenderedProvision` ready to attach to :class:`InstanceSpec`.

        Raises:
            NotImplementedError: Engine does not support remote provisioning.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def attach_get_instance(
        self,
        get_instance: Callable[[str], Instance],
    ) -> None:
        """Wire the provider's ``get_instance`` lookup onto this engine.

        The orchestrator calls this immediately after ``provider.create_instance``
        and before ``engine.provision`` so that :meth:`wait_for_ready` can poll
        the provider for status updates between HTTP-ready checks.

        Default impl sets ``self._get_instance``; engines that don't need the
        seam (e.g. HostedAPIEngine, FakeEngine) can keep the default — the
        write is harmless when ``wait_for_ready`` is never called.

        Args:
            get_instance: Provider seam — ``(instance_id) -> Instance``.
        """
        self._get_instance = get_instance  # noqa: SLF001

    def wait_for_ready(
        self,
        instance: Instance,
        *,
        http_get: Callable[[str], dict[str, Any]],
        sleep: Callable[[float], None],
        get_instance: Callable[[str], Instance],
        timeout_s: float,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """Poll until the engine reports ready, status flips terminal, or timeout.

        Concrete engines (ComfyUI: GET /system_stats; Diffusers: GET /health)
        override this. Default raises ``NotImplementedError`` so an engine
        missing the override fails loudly rather than silently never-readying.

        Args:
            instance: The just-created compute instance.
            http_get: Injectable HTTP GET seam.
            sleep: Injectable sleep used between polls.
            get_instance: Injectable provider lookup for status checks.
            timeout_s: Maximum total wait before raising ``ProvisionTimeout``.
            cancel_token: C29 cooperative cancellation seam. Concrete impls
                check ``cancel_token.raise_if_set()`` at the top of each poll
                iteration so a boot-phase reap raises ``Cancelled`` cleanly.
                Default ``None`` preserves pre-C29 callers.

        Raises:
            NotImplementedError: Subclass did not override.
            ProvisionFailed: Pod boot script crashed (status flipped terminal).
            ProvisionTimeout: Ready check never returned success within ``timeout_s``.
            Cancelled: ``cancel_token`` was set during the wait.
        """
        del cancel_token
        raise NotImplementedError(
            f"{type(self).__name__} does not support wait_for_ready"
        )


class UpscalerEngine(ABC):
    """A swappable video upscaler; owns env setup; declares supported scales.

    No prompt, no segments, no LoRA stack — upscaling is video-in/video-out.
    Separate from GenerationEngine because the surfaces don't overlap.

    Attributes:
        name: Registry key (e.g. ``"seedvr2"``).
        requires_compute: True when this engine needs a remote pod.
        requires_local_weights: True when the engine downloads weights into
            the pod's weight directory.
        supported_scales: Declared support; matcher pre-flight + ``validate_spec``
            consult this. Empty tuple means "engine claims to accept any
            ScaleTarget" (use sparingly).
    """

    name: str
    requires_compute: bool
    requires_local_weights: bool
    supported_scales: tuple[ScaleTarget, ...]

    @abstractmethod
    def provision(  # noqa: D102
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def upscale(  # noqa: D102
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult: ...

    @abstractmethod
    def validate_spec(self, job: UpscaleJob) -> None:
        """Raise on engine-unsupportable job. SeedVR2 3B refuses scale='3x'."""
        ...

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. ``'seedvr2-3b-fp8'``). MUST NOT raise on missing fields."""
        ...

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit boot payload. Default raises; remote-capable engines override."""
        del cfg
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def attach_get_instance(
        self,
        get_instance: Callable[[str], Instance],
    ) -> None:
        """Wire provider lookup; mirrors GenerationEngine.attach_get_instance."""
        self._get_instance = get_instance  # noqa: SLF001


class InterpolatorEngine(ABC):
    """A swappable frame interpolator; owns env setup; declares capability.

    Video-in / video-out at a higher frame rate. Separate from UpscalerEngine
    because the surfaces don't overlap.

    Attributes:
        name: Registry key (e.g. ``"rife"``).
        requires_compute: True when this engine needs a remote pod.
        requires_local_weights: True when it downloads weights on the pod.
        capability: How it reaches an intermediate time (arbitrary vs
            recursive-2x); the stage's fps resolver consults this.
    """

    name: str
    requires_compute: bool
    requires_local_weights: bool
    capability: InterpCapability

    @abstractmethod
    def provision(  # noqa: D102
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def interpolate(  # noqa: D102
        self,
        instance: Instance | None,
        job: InterpolateJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> InterpolateResult: ...

    @abstractmethod
    def validate_spec(self, job: InterpolateJob) -> None:
        """Raise on an engine-unsupportable job."""
        ...

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. ``'rife-v4.9'``). MUST NOT raise."""
        ...

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit boot payload. Default raises; remote engines override."""
        del cfg
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def attach_get_instance(
        self,
        get_instance: Callable[[str], Instance],
    ) -> None:
        """Wire provider lookup; mirrors UpscalerEngine.attach_get_instance."""
        self._get_instance = get_instance  # noqa: SLF001


class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends.

    Implementations may call ``backend.submit`` / ``backend.result`` from
    multiple threads concurrently; backends MUST be thread-safe (no shared
    mutable state across calls).
    """

    @abstractmethod
    def add(self, backend: GenerationBackend, *, max_in_flight: int = 1) -> None: ...  # noqa: D102

    @abstractmethod
    def submit(  # noqa: D102
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Future[Artifact]: ...

    @abstractmethod
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...  # noqa: D102

    @abstractmethod
    def close(  # noqa: D102
        self,
        *,
        cancel_pending: bool = False,
        timeout: float | None = None,
    ) -> None: ...

    def __enter__(self) -> Self:  # noqa: D105
        return self

    def __exit__(self, *_exc: object) -> None:  # noqa: D105
        self.close()


@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: PipelineState in, PipelineState out."""

    def run(self, state: PipelineState) -> PipelineState:
        """Execute the stage with the given state and return the updated state."""
        ...
