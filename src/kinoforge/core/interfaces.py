"""Abstract interfaces and data containers — the only module core logic depends on.

No concrete provider/source/engine may be imported here. Adapters depend on this
module, never the reverse.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --- compute axis -----------------------------------------------------------


@dataclass(frozen=True)
class HardwareRequirements:
    """Filter applied by ComputeProvider.find_offers; every field config-overridable.

    Attributes:
        min_vram_gb: Minimum GPU VRAM in GB; offers below this are excluded.
        min_cuda: Minimum CUDA version string (semantic compare, e.g. "12.8").
        max_cost_rate_usd_per_hr: Ceiling for pod-mode offers; serverless ignores.
        gpu_preference: Ordered preference list among surviving offers.
        disk_gb: Minimum container/instance disk in GB.
    """

    min_vram_gb: int = 48
    min_cuda: str = "12.8"
    max_cost_rate_usd_per_hr: float = 2.20
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
    """Cost-safety guardrails carried into an InstanceSpec (all seconds)."""

    idle_timeout_s: float = 2 * 3600
    job_timeout_s: float = 30 * 60
    time_buffer_s: float = 30 * 60
    max_lifetime_s: float = 5 * 3600
    budget_usd: float = 0.0
    max_workers: int = 1
    max_in_flight: int = 1


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
class CapabilityKey:
    """Full identity a ModelProfile depends on. derive() is the stable cache key.

    Attributes:
        base_model: Base-model vendor-neutral ref (e.g. "hf:org/m").
        loras: Ordered LoRA stack; order matters and contributes to the key.
        engine: Engine name (capability is engine-specific).
        precision: Precision/quantization (e.g. "fp16", "gguf-q8").
    """

    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""

    def derive(self) -> str:
        """Stable, order-sensitive sha256 over all fields (VAE excluded by design)."""
        payload = json.dumps(
            [self.base_model, list(self.loras), self.engine, self.precision],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


MODE_ROLE_REQUIREMENTS: dict[str, set[str]] = {
    "t2v": set(),
    "i2v": {"init_image"},
    "flf2v": {"first_frame", "last_frame"},
}


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


class ModelProfileProvider(ABC):
    """A cache of ModelProfiles keyed by CapabilityKey."""

    @abstractmethod
    def resolve(self, key: CapabilityKey) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def discover(  # noqa: D102
        self, key: CapabilityKey, engine: GenerationEngine, backend: GenerationBackend
    ) -> ModelProfile: ...

    @abstractmethod
    def verify(self, profile: ModelProfile, backend: GenerationBackend) -> None: ...  # noqa: D102


class GenerationBackend(ABC):
    """A live, ready engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def inspect_capabilities(self) -> ModelProfile: ...  # noqa: D102

    @abstractmethod
    def submit(self, job: GenerationJob) -> str: ...  # noqa: D102

    @abstractmethod
    def result(self, job_id: str) -> Artifact: ...  # noqa: D102

    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...  # noqa: D102


class GenerationEngine(ABC):
    """A swappable generation engine; owns its env setup; knows if it needs compute."""

    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None: ...  # noqa: D102

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


class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends."""

    @abstractmethod
    def add(self, backend: GenerationBackend) -> None: ...  # noqa: D102

    @abstractmethod
    def submit(self, job: GenerationJob) -> Future[Artifact]: ...  # noqa: D102

    @abstractmethod
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...  # noqa: D102


@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: typed input -> typed output over a shared context."""

    def run(self, ctx: object) -> object:
        """Execute the stage with the given context and return the result."""
        ...
