"""Pydantic v2 config model for kinoforge.

Loads a YAML config into a validated model that:
- Parses human-readable duration strings (e.g. "2h", "30m", "90s") to seconds.
- Rejects nonsensical cross-field combinations (idle >= lifetime, etc.).
- Derives a CapabilityKey for cache lookup.
- Exposes Lifecycle and HardwareRequirements with defaults applied.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from kinoforge.core.errors import ConfigError
from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.interfaces import (
    HardwareRequirements as InterfaceHardwareRequirements,
)
from kinoforge.core.interfaces import Lifecycle as InterfaceLifecycle

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
}

KNOWN_ENGINES = {"comfyui", "diffusers", "hosted", "fake"}

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
        budget: Monthly/run budget in USD (required — no default).
    """

    idle_timeout: float = 2 * 3600.0
    job_timeout: float = 30 * 60.0
    time_buffer: float = 30 * 60.0
    max_lifetime: float = 5 * 3600.0
    budget: float

    @field_validator(
        "idle_timeout", "job_timeout", "time_buffer", "max_lifetime", mode="before"
    )
    @classmethod
    def _parse_duration_field(cls, v: str | float | int) -> float | str | int:
        """Parse duration strings; pass through numeric values unchanged."""
        if isinstance(v, str):
            return parse_duration(v)
        return v


class ComfyUIEngineConfig(BaseModel):
    """ComfyUI-specific engine parameters.

    Attributes:
        version: ComfyUI version string.
    """

    version: str


class HostedEngineConfig(BaseModel):
    """Hosted API engine parameters.

    Attributes:
        provider: Hosted provider name (e.g. "fal").
        endpoint: API endpoint path.
        model: Model identifier on the provider.
    """

    provider: str
    endpoint: str
    model: str


class EngineConfig(BaseModel):
    """Top-level engine block.

    Attributes:
        kind: Engine name; must be one of the known engine types.
        precision: Precision/quantization string (e.g. "fp16", "gguf-q8").
        comfyui: ComfyUI-specific config, required when kind == "comfyui".
        hosted: Hosted API config, required when kind == "hosted".
    """

    kind: str
    precision: str
    comfyui: ComfyUIEngineConfig | None = None
    hosted: HostedEngineConfig | None = None


class ModelEntry(BaseModel):
    """A single model entry in the models list.

    Attributes:
        ref: Vendor-neutral model reference (e.g. "hf:org/m").
        kind: One of "base", "lora", "vae".
        target: Download target directory name.
        sha256: Optional content hash for integrity verification.
    """

    ref: str
    kind: Literal["base", "lora", "vae"]
    target: str
    sha256: str | None = None


class RequirementsConfig(BaseModel):
    """Hardware requirements override block.

    Attributes:
        min_vram_gb: Minimum GPU VRAM in GB.
        min_cuda: Minimum CUDA version string.
        max_cost_rate_usd_per_hr: Ceiling on cost rate.
        gpu_preference: Ordered list of preferred GPU types.
        disk_gb: Minimum disk in GB.
    """

    min_vram_gb: int = 48
    min_cuda: str = "12.8"
    max_cost_rate_usd_per_hr: float = 2.20
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
    """

    provider: str
    image: str
    mode: str = "pod"
    requirements: RequirementsConfig = RequirementsConfig()
    lifecycle: LifecycleConfig | None = None


# ---------------------------------------------------------------------------
# Top-level Config model
# ---------------------------------------------------------------------------


class Config(BaseModel):
    """Top-level kinoforge configuration.

    Attributes:
        engine: Engine configuration block.
        models: List of model entries.
        compute: Optional compute block (omitted for hosted engines).
        lifecycle_cfg: Top-level lifecycle config (used for hosted engines).
            Loaded from the YAML ``lifecycle:`` key via an alias.
    """

    engine: EngineConfig
    models: list[ModelEntry]
    compute: ComputeConfig | None = None
    lifecycle_cfg: LifecycleConfig | None = Field(default=None, alias="lifecycle")

    model_config = {"populate_by_name": True}

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

        # Validate kind/target consistency for each model entry
        for entry in self.models:
            allowed = VALID_KIND_TARGETS.get(entry.kind, set())
            if entry.target not in allowed:
                raise ValueError(
                    f"inconsistent model entry: kind={entry.kind!r} target={entry.target!r}; "
                    f"allowed targets for {entry.kind!r}: {sorted(allowed)}"
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
        LoRA entries are included in declaration order.

        Returns:
            A CapabilityKey with base_model, loras, engine, and precision.

        Raises:
            ConfigError: If no base model is found in the models list.
        """
        base_ref: str | None = None
        loras: list[str] = []
        for entry in self.models:
            if entry.kind == "base":
                base_ref = entry.ref
            elif entry.kind == "lora":
                loras.append(entry.ref)
            # vae: skip entirely

        if base_ref is None:
            raise ConfigError("no model entry with kind: base found in config")

        return CapabilityKey(
            base_model=base_ref,
            loras=tuple(loras),
            engine=self.engine.kind,
            precision=self.engine.precision,
        )

    def lifecycle(self) -> InterfaceLifecycle:
        """Return an InterfaceLifecycle with defaults applied.

        Prefers compute.lifecycle when present (non-hosted path);
        falls back to top-level lifecycle (hosted path).
        Defaults are: idle_timeout=2h, job_timeout=30m, time_buffer=30m, max_lifetime=5h.

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
        )

    def hardware_requirements(self) -> InterfaceHardwareRequirements:
        """Return HardwareRequirements with defaults applied.

        Pulls from compute.requirements when present; returns all-defaults otherwise.
        Defaults: min_vram_gb=48, min_cuda="12.8", max_cost_rate_usd_per_hr=2.20,
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
            max_cost_rate_usd_per_hr=r.max_cost_rate_usd_per_hr,
            gpu_preference=tuple(r.gpu_preference),
            disk_gb=r.disk_gb,
        )


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

    # Determine whether this is raw YAML text or a file path
    if isinstance(text_or_path, Path):
        text = text_or_path.read_text(encoding="utf-8")
    elif "\n" in str(text_or_path) or not Path(str(text_or_path)).exists():
        text = str(text_or_path)
    else:
        text = Path(str(text_or_path)).read_text(encoding="utf-8")

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config must be a YAML mapping at the top level")

    try:
        return Config.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise ConfigError(str(exc)) from exc
