# kinoforge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build kinoforge — a vendor-agnostic video-generation provisioning & orchestration system whose core never imports a concrete provider/source/engine, with the seams a future long-form pipeline bolts onto.

**Architecture:** Three swappable axes (compute, model source, generation engine), each a registry-discovered plugin resolved by name/scheme. Core depends only on `core/interfaces.py`. Single-clip happy path + every seam (ModelProfile cache, strategy decision point, BackendPool, Stage, ArtifactStore) built with exactly one real path; named future layers deferred behind interfaces. TDD red-first, fully offline via Local/Fake adapters + injectable clock.

**Tech Stack:** Python 3.12, pydantic v2, PyYAML (runtime); stdlib `urllib`/`threading`/`logging`/`concurrent.futures` for everything else; skypilot lazy/optional. pytest + pytest-cov, ruff (strict, google docstrings), mypy strict. pixi for deps/tasks.

**Conventions (from CLAUDE.md — apply to every task):**
- Red/green TDD: write failing test → confirm fail → minimal impl → confirm pass → refactor → confirm green. Use the `test-design` skill for every test (state behavior-under-test + a concrete failing bug; no weak assertions, no implementation mirroring, no over-mocking).
- All functions: type hints + Google-style docstrings. Pass `pixi run lint` + `pixi run typecheck`.
- Run `pixi run pre-commit run --all-files` before each commit; never commit failing hooks. Conventional Commits, imperative mood. Commit after every task (and ideally each green test).
- Update + commit `PROGRESS.md` after each task (durability rule).
- Conflict resolution: tests pass > mypy clean > ruff clean.
- Offline hard constraint: no test touches real cloud/network/GPU/weights. A localhost `http.server` fixture is permitted for the downloader (loopback, not "real network"). All vendor HTTP (RunPod/CivitAI/HF/hosted/SkyPilot) is mocked at the `urllib`/subprocess boundary.
- `# DEFERRED:` comment marks every deferred seam.

**Add dependencies first (once, before Task 1):**
```bash
pixi add pydantic pyyaml
pixi add --pypi skypilot   # optional extra; only imported in providers/skypilot/. If resolution is heavy, defer to Task 21 and gate the test behind importorskip.
```
Also create empty `src/kinoforge/__init__.py`, `src/kinoforge/__main__.py` (stub `def main(): ...` wired in Task 22), and `tests/__init__.py` if absent.

---

## File Structure (decomposition)

```
src/kinoforge/
  core/
    errors.py        typed exceptions (Task 1)
    interfaces.py    all ABCs + dataclasses; the ONLY thing core logic depends on (Task 1)
    registry.py      name+scheme -> impl; self-register (Task 2)
    credentials.py   EnvCredentialProvider (Task 3)
    config.py        pydantic v2 models, YAML load, cross-field validation, CapabilityKey derivation, duration parse (Task 4)
    offers.py        filter_offers(offers, reqs) pure helper (Task 5)
    downloader.py    parallel resumable checksum-verifying; stdlib + optional aria2c (Task 6)
    provisioner.py   shared provision steps + delegate to engine (Task 10)
    profiles.py      ModelProfileProvider impl: cache via ArtifactStore, resolve/discover/verify, single-flight (Task 12)
    validation.py    validate_request(profile, request) mode+role-authoritative (Task 13)
    strategy.py      decide(profile, segments, params, spec) -> list[GenerationJob] (Task 14)
    pool.py          BackendPool ABC + SequentialPool (Task 15)
    orchestrator.py  deploy + generate flows; discovery ordering; fail-hard teardown; dry-run (Task 16)
    lifecycle.py     effective_deadline, timers, dead-man liveness, sweeper, ledger, confirmed teardown, budget (Tasks 17-18)
    logging.py       stdlib structured logging (Task 1, tiny)
  providers/ local/ (Task 9), runpod/ (Task 20), skypilot/ (Task 21)
  sources/   http/ (Task 7), civitai/ (Task 19a), huggingface/ (Task 19b)
  engines/   fake/ (Task 8), comfyui/ (Task 20a), diffusers/ (Task 21a), hosted/ (Task 21b)
  pipeline/  stage.py (Task 15) + generate_clip.py (Task 15)
  stores/    base.py + local.py (Task 11)
  cli.py     (Task 22)  __main__.py
examples/configs/  wan.yaml, diffusers.yaml, hosted.yaml, local-fake.yaml (Task 23)
.github/workflows/ci.yml (Task 23)
tests/...   mirrors src; conftest.py with shared fakes (FakeProvider, FakeSource, FakeEngine, FakeClock)
```

---

## Phase 1 — Foundations (interfaces, registry, credentials, config)

### Task 1: Core interfaces, errors, logging

**Goal:** Define every ABC + dataclass the spec names, the typed error hierarchy, and a structured logger — the contract surface all later code references.

**Files:**
- Create: `src/kinoforge/core/errors.py`
- Create: `src/kinoforge/core/interfaces.py`
- Create: `src/kinoforge/core/logging.py`
- Test: `tests/core/test_interfaces.py`

**Acceptance Criteria:**
- [ ] `CapabilityKey.derive()` is a stable, order-sensitive hash over `base_model`, `loras` (ordered), `engine`, `precision`; VAE never participates (it isn't a field).
- [ ] Reordering `loras` changes the derived key; identical fields produce identical keys across instances.
- [ ] `ModelProfile.max_segment_seconds == max_frames / fps`.
- [ ] `MODE_ROLE_REQUIREMENTS == {"t2v": set(), "i2v": {"init_image"}, "flf2v": {"first_frame", "last_frame"}}`.
- [ ] Module imports with zero dependencies on any `providers`/`sources`/`engines` module (enforced by a test that asserts no such names appear in `sys.modules` after import — see Task 24 for the full invariant check).
- [ ] Error classes subclass a common `KinoforgeError`.

**Verify:** `pixi run test tests/core/test_interfaces.py` → all pass; `pixi run typecheck` → clean.

**Steps:**

- [ ] **Step 1: Write failing tests** (`tests/core/test_interfaces.py`)

```python
import math
from kinoforge.core.interfaces import (
    CapabilityKey, ModelProfile, MODE_ROLE_REQUIREMENTS,
)
from kinoforge.core import errors


def test_capability_key_is_order_sensitive_over_loras():
    a = CapabilityKey(base_model="wan2.2", loras=("svi", "detail"), engine="comfyui", precision="fp16")
    b = CapabilityKey(base_model="wan2.2", loras=("detail", "svi"), engine="comfyui", precision="fp16")
    # Bug this catches: a derive() that sorts/sets loras would collapse stack order, which changes capability.
    assert a.derive() != b.derive()


def test_capability_key_is_stable_across_instances():
    a = CapabilityKey(base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16")
    b = CapabilityKey(base_model="wan2.2", loras=("svi",), engine="comfyui", precision="fp16")
    # Bug this catches: derive() keyed on id()/object identity instead of field values.
    assert a.derive() == b.derive()


def test_capability_key_distinguishes_engine_and_precision():
    base = dict(base_model="wan2.2", loras=("svi",))
    assert CapabilityKey(**base, engine="comfyui", precision="fp16").derive() \
        != CapabilityKey(**base, engine="diffusers", precision="fp16").derive()
    assert CapabilityKey(**base, engine="comfyui", precision="fp16").derive() \
        != CapabilityKey(**base, engine="comfyui", precision="gguf-q8").derive()


def test_max_segment_seconds_is_frames_over_fps():
    p = ModelProfile(name="wan", max_frames=81, fps=16, supported_modes={"t2v"},
                     max_resolution=(1280, 720), supports_native_extension=False,
                     supports_joint_audio=False)
    assert math.isclose(p.max_segment_seconds, 81 / 16)


def test_mode_role_requirements_table_is_authoritative():
    # Bug this catches: per-model role logic creeping in instead of one shared table.
    assert MODE_ROLE_REQUIREMENTS == {
        "t2v": set(), "i2v": {"init_image"}, "flf2v": {"first_frame", "last_frame"},
    }


def test_errors_share_common_base():
    assert issubclass(errors.ProfileNotCached, errors.KinoforgeError)
    assert issubclass(errors.ConfigError, errors.KinoforgeError)
```

- [ ] **Step 2: Run, confirm fail** — `pixi run test tests/core/test_interfaces.py` → ImportError / fail.

- [ ] **Step 3: Implement `errors.py`**

```python
"""Typed exception hierarchy for kinoforge."""


class KinoforgeError(Exception):
    """Base class for all kinoforge errors."""


class ConfigError(KinoforgeError):
    """Configuration is invalid or internally inconsistent."""


class AuthError(KinoforgeError):
    """A credential is missing or rejected by a provider/source."""


class CapacityError(KinoforgeError):
    """No compute offer satisfies the hardware requirements."""


class ProfileNotCached(KinoforgeError):
    """A ModelProfile was requested at plan time but is not in the cache."""


class CapabilityMismatch(KinoforgeError):
    """A live model contradicts its cached profile (verify drift)."""


class ValidationError(KinoforgeError):
    """A GenerationRequest or engine spec failed validation."""


class BudgetExceeded(KinoforgeError):
    """Estimated spend crossed the configured budget ceiling."""


class TeardownError(KinoforgeError):
    """destroy_instance could not confirm termination."""


class UnknownAdapter(KinoforgeError):
    """No registered provider/source/engine matches the requested name/scheme."""
```

- [ ] **Step 4: Implement `interfaces.py`** — dataclasses + ABCs. (Full contract; later tasks rely on these exact names/signatures.)

```python
"""Abstract interfaces and data containers — the only module core logic depends on.

No concrete provider/source/engine may be imported here. Adapters depend on this
module, never the reverse.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --- compute axis -----------------------------------------------------------


@dataclass(frozen=True)
class HardwareRequirements:
    """Filter applied by ComputeProvider.find_offers; every field config-overridable."""

    min_vram_gb: int = 48
    min_cuda: str = "12.8"
    max_cost_rate_usd_per_hr: float = 2.20  # pod-mode only; ignored for serverless
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
    """Cost-safety guardrails carried into an InstanceSpec (seconds)."""

    idle_timeout_s: float = 2 * 3600
    job_timeout_s: float = 30 * 60
    time_buffer_s: float = 30 * 60
    max_lifetime_s: float = 5 * 3600
    budget_usd: float = 0.0
    # serverless proactive caps
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

    Download case carries url/filename/size/sha256/headers; store case carries uri.
    """

    filename: str = ""
    url: str = ""
    size: int | None = None
    sha256: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    uri: str = ""          # ArtifactStore location once materialized
    meta: dict = field(default_factory=dict)


class CredentialProvider(ABC):
    """Resolves named secrets; env-backed by default."""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """Return the secret for ``key`` or ``None`` if unset."""


class ComputeProvider(ABC):
    """A place to run GPU workloads. Instances created with cost guardrails."""

    name: str

    @abstractmethod
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]: ...
    @abstractmethod
    def create_instance(self, spec: InstanceSpec) -> Instance: ...
    @abstractmethod
    def get_instance(self, instance_id: str) -> Instance: ...
    @abstractmethod
    def list_instances(self) -> list[Instance]: ...
    @abstractmethod
    def stop_instance(self, instance_id: str) -> None: ...
    @abstractmethod
    def destroy_instance(self, instance_id: str) -> None: ...
    @abstractmethod
    def heartbeat(self, instance_id: str) -> None: ...
    @abstractmethod
    def endpoints(self, instance: Instance) -> dict[str, str]: ...


class ModelSource(ABC):
    """Resolves a vendor-neutral ref into downloadable Artifact(s)."""

    scheme: str

    @abstractmethod
    def handles(self, ref: str) -> bool: ...
    @abstractmethod
    def resolve(self, ref: str, creds: CredentialProvider) -> list[Artifact]: ...


# --- generation layer -------------------------------------------------------


@dataclass(frozen=True)
class CapabilityKey:
    """Full identity a ModelProfile depends on. derive() is the stable cache key."""

    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""

    def derive(self) -> str:
        """Stable, order-sensitive hash over all fields (VAE excluded by design)."""
        payload = "\x1f".join([
            self.base_model,
            "\x1e".join(self.loras),
            self.engine,
            self.precision,
        ])
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

    kind: str   # open enum: "image" | "audio" | "video" | ...
    role: str
    ref: Artifact
    meta: dict = field(default_factory=dict)


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
    params: dict = field(default_factory=dict)


@dataclass
class GenerationJob:
    """One unit of work: engine-interpreted spec + neutral params + ordered segments."""

    spec: dict
    segments: list[Segment]
    params: dict = field(default_factory=dict)


class ModelProfileProvider(ABC):
    """A cache of ModelProfiles keyed by CapabilityKey (see profiles.py)."""

    @abstractmethod
    def resolve(self, key: CapabilityKey) -> ModelProfile: ...
    @abstractmethod
    def discover(
        self, key: CapabilityKey, engine: GenerationEngine, backend: GenerationBackend
    ) -> ModelProfile: ...
    @abstractmethod
    def verify(self, profile: ModelProfile, backend: GenerationBackend) -> None: ...


class GenerationBackend(ABC):
    """A live, ready engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ModelProfile: ...
    @abstractmethod
    def inspect_capabilities(self) -> ModelProfile: ...
    @abstractmethod
    def submit(self, job: GenerationJob) -> str: ...
    @abstractmethod
    def result(self, job_id: str) -> Artifact: ...
    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...


class GenerationEngine(ABC):
    """A swappable generation engine; owns its env setup; knows if it needs compute."""

    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(self, instance: Instance | None, cfg: dict) -> None: ...
    @abstractmethod
    def backend(self, instance: Instance | None, cfg: dict) -> GenerationBackend: ...
    @abstractmethod
    def profile_for(self, key: CapabilityKey) -> ModelProfile: ...
    @abstractmethod
    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]: ...
    @abstractmethod
    def validate_spec(self, job: GenerationJob) -> None: ...


class BackendPool(ABC):
    """Dispatches jobs across one or more GenerationBackends."""

    @abstractmethod
    def add(self, backend: GenerationBackend) -> None: ...
    @abstractmethod
    def submit(self, job: GenerationJob) -> Future[Artifact]: ...
    @abstractmethod
    def map(self, jobs: list[GenerationJob]) -> list[Artifact]: ...


@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: typed input -> typed output over a shared context."""

    def run(self, ctx: object) -> object: ...
```

- [ ] **Step 5: Implement `logging.py`** — thin stdlib structured logger.

```python
"""Structured stdlib logging helper."""
import logging

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger; idempotent across calls."""
    global _CONFIGURED
    if not _CONFIGURED:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root = logging.getLogger("kinoforge")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        _CONFIGURED = True
    return logging.getLogger(f"kinoforge.{name}")
```

- [ ] **Step 6: Run tests → pass. Commit.**

```bash
git add src/kinoforge/core/errors.py src/kinoforge/core/interfaces.py src/kinoforge/core/logging.py tests/core/test_interfaces.py
git commit -m "feat: add core interfaces, error hierarchy, structured logging"
```

---

### Task 2: Registry (name + scheme routing)

**Goal:** A registry that maps provider/engine names and source schemes to implementations via explicit `register_*` calls; core resolves by name/scheme only.

**Files:**
- Create: `src/kinoforge/core/registry.py`
- Test: `tests/core/test_registry.py`

**Acceptance Criteria:**
- [ ] `register_provider/source/engine` then `get_provider/source/engine` round-trips an instance/factory.
- [ ] Sources resolve by `scheme` AND by `handles(ref)` dispatch (`source_for_ref("hf:x")` returns the hf source).
- [ ] Unknown name/scheme raises `UnknownAdapter` with the offending name in the message.
- [ ] Re-registering the same name overwrites (idempotent self-register on re-import).

**Verify:** `pixi run test tests/core/test_registry.py` → pass.

**Steps:**

- [ ] **Step 1: Failing tests**

```python
import pytest
from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter


class _Src:
    scheme = "fake"
    def handles(self, ref): return ref.startswith("fake:")


def test_provider_round_trips():
    registry.register_provider("dummy", lambda: "P")
    assert registry.get_provider("dummy")() == "P"


def test_unknown_provider_raises_named():
    with pytest.raises(UnknownAdapter, match="nope"):
        registry.get_provider("nope")


def test_source_dispatch_by_ref():
    s = _Src()
    registry.register_source(s)
    assert registry.source_for_ref("fake:123") is s


def test_unknown_scheme_raises():
    with pytest.raises(UnknownAdapter):
        registry.source_for_ref("nosuchscheme:1")
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `registry.py`**

```python
"""Runtime registry: name (providers/engines) + scheme (sources) -> impl.

Adapters self-register via register_* at import time. Core resolves by
name/scheme only and must never import a concrete adapter module.
"""
from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import ComputeProvider, GenerationEngine, ModelSource

_providers: dict[str, Callable[[], ComputeProvider]] = {}
_engines: dict[str, Callable[[], GenerationEngine]] = {}
_sources: list[ModelSource] = []


def register_provider(name: str, factory: Callable[[], ComputeProvider]) -> None:
    """Register a compute provider factory under ``name`` (overwrites)."""
    _providers[name] = factory


def get_provider(name: str) -> Callable[[], ComputeProvider]:
    """Return the provider factory for ``name`` or raise UnknownAdapter."""
    try:
        return _providers[name]
    except KeyError:
        raise UnknownAdapter(f"no compute provider registered: {name!r}") from None


def register_engine(name: str, factory: Callable[[], GenerationEngine]) -> None:
    """Register a generation engine factory under ``name`` (overwrites)."""
    _engines[name] = factory


def get_engine(name: str) -> Callable[[], GenerationEngine]:
    """Return the engine factory for ``name`` or raise UnknownAdapter."""
    try:
        return _engines[name]
    except KeyError:
        raise UnknownAdapter(f"no generation engine registered: {name!r}") from None


def register_source(source: ModelSource) -> None:
    """Register a model source (replacing any with the same scheme)."""
    global _sources
    _sources = [s for s in _sources if s.scheme != source.scheme] + [source]


def source_for_ref(ref: str) -> ModelSource:
    """Return the source whose handles(ref) is True or raise UnknownAdapter."""
    for s in _sources:
        if s.handles(ref):
            return s
    raise UnknownAdapter(f"no model source handles ref: {ref!r}")
```

- [ ] **Step 4: Run → pass. Commit** `feat: add adapter registry with name+scheme routing`.

---

### Task 3: Env-backed credential provider

**Goal:** `EnvCredentialProvider.get(key)` reads from `os.environ`; secrets never come from config.

**Files:**
- Create: `src/kinoforge/core/credentials.py`
- Test: `tests/core/test_credentials.py`

**Acceptance Criteria:**
- [ ] `get("RUNPOD_API_KEY")` returns the env value when set, `None` when unset.
- [ ] Implements the `CredentialProvider` ABC.

**Verify:** `pixi run test tests/core/test_credentials.py` → pass.

**Steps:**

- [ ] **Step 1: Failing test**

```python
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.interfaces import CredentialProvider


def test_reads_from_environment(monkeypatch):
    monkeypatch.setenv("CIVITAI_TOKEN", "secret-123")
    assert EnvCredentialProvider().get("CIVITAI_TOKEN") == "secret-123"


def test_missing_key_is_none(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    assert EnvCredentialProvider().get("NOPE_KEY") is None


def test_is_credential_provider():
    assert isinstance(EnvCredentialProvider(), CredentialProvider)
```

- [ ] **Step 2: Run → fail. Step 3: Implement**

```python
"""Environment-variable credential provider."""
import os

from kinoforge.core.interfaces import CredentialProvider


class EnvCredentialProvider(CredentialProvider):
    """Resolves secrets from process environment variables."""

    def get(self, key: str) -> str | None:
        """Return ``os.environ[key]`` or None if unset."""
        return os.environ.get(key)
```

- [ ] **Step 4: Run → pass. Commit** `feat: add env-backed credential provider`.

---

### Task 4: Config model (pydantic v2 + YAML + cross-field validation + CapabilityKey derivation)

**Goal:** Typed config loaded from YAML, secrets-free, that rejects nonsensical combinations, parses durations, derives `CapabilityKey`, and exposes `Lifecycle`.

**Files:**
- Create: `src/kinoforge/core/config.py`
- Test: `tests/core/test_config.py`
- Test fixtures: inline YAML strings (no external files).

**Acceptance Criteria:**
- [ ] Durations `"2h"`, `"30m"`, `"90s"` parse to seconds; bare ints rejected with `ConfigError`.
- [ ] Validation rejects: `idle_timeout >= max_lifetime`; `job_timeout > max_lifetime`; `compute:` present when `engine.kind == "hosted"`; a model entry whose `kind`/`target` are inconsistent (e.g. `kind: base` with `target: loras`); unknown `engine.kind` / `compute.provider` (clear error).
- [ ] Defaults applied when unspecified: `min_vram_gb=48`, `min_cuda="12.8"`, `max_cost_rate_usd_per_hr=2.20`, `disk_gb=100`, `idle_timeout=2h`, `job_timeout=30m`, `time_buffer=30m`, `max_lifetime=5h`; `budget` required (missing → `ConfigError`).
- [ ] `Config.capability_key()` returns a `CapabilityKey` whose `base_model` is the base ref, `loras` is the ordered tuple of lora refs in declaration order, `engine` = `engine.kind`, `precision` = `engine.precision`; `vae` entries excluded.
- [ ] `Config.lifecycle()` returns an `interfaces.Lifecycle` with seconds populated.
- [ ] `Config.hardware_requirements()` returns `HardwareRequirements`.

**Verify:** `pixi run test tests/core/test_config.py` → pass; `pixi run typecheck` → clean.

**Steps:**

- [ ] **Step 1: Failing tests** (representative; add one per AC)

```python
import pytest
from kinoforge.core.config import load_config, parse_duration
from kinoforge.core.errors import ConfigError

HOSTED = """
engine: {kind: hosted, precision: "", hosted: {provider: fal, endpoint: "x", model: ltx-2}}
lifecycle: {budget: 25.0}
models: [{ref: "hf:org/m", kind: base, target: diffusion_models}]
"""

WAN = """
engine:
  kind: comfyui
  precision: fp16
  comfyui: {version: v0.3.40}
models:
  - {ref: "hf:Wan-AI/Wan2.2-T2V-A14B", kind: base, target: diffusion_models}
  - {ref: "civitai:1234@5678", kind: lora, target: loras}
  - {ref: "https://e/x.vae", kind: vae, target: vae, sha256: abc}
compute:
  provider: runpod
  image: "img:tag"
  mode: pod
  requirements: {gpu_preference: ["RTX 4090"]}
  lifecycle: {idle_timeout: 2h, job_timeout: 30m, max_lifetime: 5h, budget: 25.0}
"""


def test_parse_duration_units():
    assert parse_duration("2h") == 2 * 3600
    assert parse_duration("30m") == 30 * 60
    assert parse_duration("90s") == 90


def test_bare_int_duration_rejected():
    with pytest.raises(ConfigError):
        parse_duration("120")


def test_idle_ge_lifetime_rejected():
    bad = WAN.replace("idle_timeout: 2h", "idle_timeout: 6h")
    with pytest.raises(ConfigError, match="idle_timeout"):
        load_config(bad)


def test_job_gt_lifetime_rejected():
    bad = WAN.replace("job_timeout: 30m", "job_timeout: 6h")
    with pytest.raises(ConfigError, match="job_timeout"):
        load_config(bad)


def test_compute_present_for_hosted_rejected():
    bad = HOSTED + "compute: {provider: runpod, image: x, lifecycle: {budget: 1.0}}\n"
    with pytest.raises(ConfigError, match="hosted"):
        load_config(bad)


def test_inconsistent_kind_target_rejected():
    bad = WAN.replace("kind: base, target: diffusion_models", "kind: base, target: loras")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_unknown_engine_kind_rejected():
    with pytest.raises(ConfigError, match="engine"):
        load_config(WAN.replace("kind: comfyui", "kind: bogus"))


def test_budget_required():
    with pytest.raises(ConfigError, match="budget"):
        load_config(WAN.replace(", budget: 25.0", ""))


def test_capability_key_derivation_orders_loras_and_excludes_vae():
    cfg = load_config(WAN)
    key = cfg.capability_key()
    assert key.base_model == "hf:Wan-AI/Wan2.2-T2V-A14B"
    assert key.loras == ("civitai:1234@5678",)  # vae excluded, order preserved
    assert key.engine == "comfyui"
    assert key.precision == "fp16"


def test_lifecycle_defaults_applied():
    cfg = load_config(HOSTED)
    lc = cfg.lifecycle()
    assert lc.idle_timeout_s == 2 * 3600
    assert lc.job_timeout_s == 30 * 60
    assert lc.max_lifetime_s == 5 * 3600
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement `config.py`** — pydantic v2 models + `model_validator(mode="after")` for cross-field rules. Key points for the implementer:
  - `parse_duration(s: str) -> float`: regex `^(\d+)(h|m|s)$`; raise `ConfigError` otherwise. Use a pydantic field validator wrapping it for all `*_timeout`/`time_buffer` fields (accept `str`, store `float` seconds).
  - `KNOWN_ENGINES = {"comfyui", "diffusers", "hosted", "fake"}`, `KNOWN_PROVIDERS = {"runpod", "local", "skypilot", "fake"}`. Validate `engine.kind`/`compute.provider` membership in the after-validator (registry isn't populated at config-load time, so validate against these sets and let the registry give the runtime error too).
  - `VALID_KIND_TARGETS = {"base": {"diffusion_models", "checkpoints", "unet"}, "lora": {"loras"}, "vae": {"vae"}}` — reject inconsistent pairings.
  - After-validator raises `ConfigError` for: hosted+compute; `idle_timeout_s >= max_lifetime_s`; `job_timeout_s > max_lifetime_s`; missing budget; bad kind/target.
  - Convert pydantic `ValidationError` to `ConfigError` at the `load_config` boundary so callers catch one type.
  - `capability_key()`: base ref from the single `kind == base` model; `loras` tuple from `kind == lora` entries in order; exclude `vae`.
  - `lifecycle()` → `interfaces.Lifecycle`; `hardware_requirements()` → `interfaces.HardwareRequirements`.

  (Implementer writes the full pydantic models: `EngineConfig`, `ModelEntry`, `ComputeConfig`, `RequirementsConfig`, `LifecycleConfig`, `Config`. Each field typed + Google docstring. `load_config(text_or_path)` accepts a YAML string or a path.)

- [ ] **Step 4: Run → pass. Commit** `feat: add validated pydantic config model with CapabilityKey derivation`.

---

## Phase 2 — Downloader + HTTP source

### Task 5: `filter_offers` pure helper

**Goal:** A pure function providers call to apply `HardwareRequirements` (DRY; satisfies the `find_offers` filtering DoD independently of any provider).

**Files:**
- Create: `src/kinoforge/core/offers.py`
- Test: `tests/core/test_offers.py`

**Acceptance Criteria:**
- [ ] Excludes offers with `vram_gb < min_vram_gb` or `cuda < min_cuda` (semantic version compare, not string).
- [ ] Excludes **pod**-mode offers with `cost_rate > max_cost_rate_usd_per_hr`; does NOT exclude **serverless** offers on cost.
- [ ] Among survivors, orders by `gpu_preference` (listed GPUs first, in listed order; unlisted appended preserving input order).
- [ ] With empty `gpu_preference`, input order is preserved.

**Verify:** `pixi run test tests/core/test_offers.py` → pass.

**Steps:**

- [ ] **Step 1: Failing tests**

```python
from kinoforge.core.interfaces import HardwareRequirements, Offer
from kinoforge.core.offers import filter_offers


def _o(id, gpu, vram, cuda, cost, mode="pod"):
    return Offer(id=id, gpu_type=gpu, vram_gb=vram, cuda=cuda, cost_rate_usd_per_hr=cost, mode=mode)


def test_excludes_undersized_vram_and_old_cuda():
    offers = [_o("a", "RTX 4090", 24, "12.8", 1.0), _o("b", "RTX 4090", 48, "12.1", 1.0),
              _o("c", "RTX 4090", 48, "12.8", 1.0)]
    reqs = HardwareRequirements(min_vram_gb=48, min_cuda="12.8", max_cost_rate_usd_per_hr=2.20)
    assert [o.id for o in filter_offers(offers, reqs)] == ["c"]


def test_cost_filter_pod_only():
    offers = [_o("pod", "X", 48, "12.8", 3.0, mode="pod"),
              _o("sl", "X", 48, "12.8", 3.0, mode="serverless")]
    reqs = HardwareRequirements(max_cost_rate_usd_per_hr=2.20)
    ids = [o.id for o in filter_offers(offers, reqs)]
    assert "pod" not in ids and "sl" in ids


def test_gpu_preference_orders_survivors():
    offers = [_o("a", "RTX 5090", 48, "12.8", 1.0), _o("b", "RTX 4090", 48, "12.8", 1.0)]
    reqs = HardwareRequirements(gpu_preference=("RTX 4090", "RTX 5090"))
    assert [o.gpu_type for o in filter_offers(offers, reqs)] == ["RTX 4090", "RTX 5090"]
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — version compare via `tuple(int(p) for p in cuda.split("."))`; stable preference sort.

```python
"""Pure offer-filtering helper applied by ComputeProvider.find_offers."""
from __future__ import annotations

from kinoforge.core.interfaces import HardwareRequirements, Offer


def _cuda_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def filter_offers(offers: list[Offer], reqs: HardwareRequirements) -> list[Offer]:
    """Return offers meeting reqs, ordered by gpu_preference then input order."""
    kept: list[Offer] = []
    for o in offers:
        if o.vram_gb < reqs.min_vram_gb:
            continue
        if _cuda_tuple(o.cuda) < _cuda_tuple(reqs.min_cuda):
            continue
        if o.mode == "pod" and o.cost_rate_usd_per_hr > reqs.max_cost_rate_usd_per_hr:
            continue
        kept.append(o)
    if not reqs.gpu_preference:
        return kept

    def rank(o: Offer) -> int:
        return reqs.gpu_preference.index(o.gpu_type) if o.gpu_type in reqs.gpu_preference else len(reqs.gpu_preference)

    return sorted(kept, key=rank)  # sorted() is stable -> preserves input order within a rank
```

- [ ] **Step 4: Run → pass. Commit** `feat: add pure offer-filtering helper`.

---

### Task 6: Downloader (parallel, resumable, checksum-verifying)

**Goal:** A stdlib threaded ranged-GET downloader that skips already-complete files (size+checksum), resumes partial downloads via HTTP `Range`, verifies sha256, and uses `aria2c` if present.

**Files:**
- Create: `src/kinoforge/core/downloader.py`
- Test: `tests/core/test_downloader.py`
- Test helper: `tests/conftest.py` → `http_server` fixture (a `ThreadingHTTPServer` on `127.0.0.1:0` serving a temp dir with `Range` support — Python's `SimpleHTTPRequestHandler` supports Range in 3.12? No: it does NOT. Implement a tiny handler that honors `Range`.)

**Acceptance Criteria:**
- [ ] Downloads a file from the loopback server to a target path; content matches.
- [ ] Re-running with the file already fully present and correct sha256 makes **zero** range requests (idempotent skip) — assert via a request counter on the handler.
- [ ] A partial `.part` file resumes from its offset (server receives a `Range: bytes=<n>-` request) and completes correctly.
- [ ] sha256 mismatch raises `KinoforgeError`; a corrupt `.part` (wrong leading bytes) is discarded and re-fetched rather than appended to.
- [ ] `download_all(artifacts, dest)` fetches multiple artifacts concurrently (ThreadPool) and returns materialized `Artifact`s with `uri` set.

**Verify:** `pixi run test tests/core/test_downloader.py` → pass.

**Steps:**

- [ ] **Step 1: Implement the `http_server` fixture** (conftest) — a Range-aware handler with a class-level `request_log` list; yields `base_url` + `log`.

- [ ] **Step 2: Failing tests** — happy download, idempotent skip (assert `log` empty on 2nd run), resume (pre-write a `.part`, assert a Range request appears), checksum mismatch raises.

- [ ] **Step 3: Implement `downloader.py`** — key behaviors for the implementer:
  - `sha256_file(path) -> str` streaming.
  - `download_one(artifact, dest, *, fetch=_urllib_fetch) -> Artifact`: compute target path; if exists and (no sha256 or sha256 matches) → return immediately (skip); else if `.part` exists → send `Range: bytes=<len>-`, append; else full GET. Verify sha256 if provided; on mismatch raise `KinoforgeError`. Rename `.part` → final on success.
  - The actual HTTP via `urllib.request.Request` with `Range`/auth headers; inject `fetch` so tests can drive the loopback server (no mocking of business logic, just the transport seam).
  - `download_all(artifacts, dest, *, max_workers=4)`: `ThreadPoolExecutor`.
  - `aria2c`: `shutil.which("aria2c")`; if present, a `_aria2_fetch`; **guard the aria2 path behind availability and don't require it in tests** (mark `# DEFERRED:`-style note that aria2 is opportunistic).

- [ ] **Step 4: Run → pass. Commit** `feat: add resumable checksum-verifying downloader`.

---

### Task 7: HTTPSource

**Goal:** `HTTPSource` resolves `https://…[?sha256=]` refs into a single `Artifact`; self-registers.

**Files:**
- Create: `src/kinoforge/sources/__init__.py`, `src/kinoforge/sources/http/__init__.py`
- Test: `tests/sources/test_http.py`

**Acceptance Criteria:**
- [ ] `handles("https://x/y.safetensors")` True; `handles("hf:org/m")` False.
- [ ] `resolve()` returns one `Artifact` with `url` set, `filename` derived from the URL path, and `sha256` carried from the config entry (passed via the ref or a paired arg — decide: source `resolve(ref, creds)` only gets the ref, so encode optional sha as the model entry's field handled by the provisioner; HTTPSource itself just sets url+filename).
- [ ] Importing `kinoforge.sources.http` registers the source (`source_for_ref("https://…")` returns it).

**Verify:** `pixi run test tests/sources/test_http.py` → pass.

**Steps:** standard red→green. Implement `HTTPSource(ModelSource)` with `scheme="https"`, `handles` checking `ref.startswith(("https://", "http://"))`, `resolve` building `Artifact(url=ref, filename=ref.split("/")[-1].split("?")[0])`. Register at import. Commit `feat: add HTTP model source`.

> Note on sha256: the config `ModelEntry.sha256` rides with the entry; the provisioner (Task 10) merges it onto the resolved `Artifact` before download. HTTPSource stays dumb about checksums beyond passing through any `?sha256=` query param if present.

---

## Phase 3 — Fake engine, local provider, provisioner, artifact store

### Task 8: FakeEngine + FakeBackend (test substrate)

**Goal:** A deterministic, GPU-free engine/backend pair: declarable flags, fake `inspect_capabilities`, deterministic artifact from `result`. Lives in `engines/fake/` (shipped, not test-only — it's a real adapter proving the no-weights path).

**Files:**
- Create: `src/kinoforge/engines/__init__.py`, `src/kinoforge/engines/fake/__init__.py`
- Test: `tests/engines/test_fake.py`

**Acceptance Criteria:**
- [ ] `FakeEngine(name="fake", requires_compute=True, requires_local_weights=False)`; constructable with an injected `ModelProfile` and a `declared_flags` map keyed by `CapabilityKey.derive()`.
- [ ] `backend(...).inspect_capabilities()` returns the injected probe profile (probeable fields only; flags False here — flags come from `declared_flags`).
- [ ] `backend.submit(job)` returns a job id; `result(id)` returns a deterministic `Artifact` whose content is a function of the job's segment prompts (so tests can assert determinism).
- [ ] `validate_spec` raises `ValidationError` when a required key (configurable set, default `set()`) is missing.
- [ ] Self-registers under name `"fake"`.

**Verify:** `pixi run test tests/engines/test_fake.py` → pass.

**Steps:** red→green. The FakeBackend stores submitted jobs in a dict; `result` synthesizes `Artifact(filename=f"clip-{hash}.mp4", meta={"prompts": [...]})`. Make probe profile and declared flags constructor args so other tests configure them. Commit `feat: add FakeEngine/FakeBackend test substrate`.

---

### Task 9: LocalProvider (+ injectable clock)

**Goal:** A `ComputeProvider` that simulates instances in-process with an injectable clock, so deploy/lifecycle/discovery are testable offline. Uses `filter_offers`.

**Files:**
- Create: `src/kinoforge/providers/__init__.py`, `src/kinoforge/providers/local/__init__.py`
- Create: `src/kinoforge/core/clock.py` (`Clock` protocol + `RealClock` + `FakeClock` with `advance()`)
- Test: `tests/providers/test_local.py`, `tests/core/test_clock.py`

**Acceptance Criteria:**
- [ ] `find_offers(reqs)` returns a synthetic local offer list filtered through `filter_offers` (defaults applied).
- [ ] `create_instance(spec)` returns a `ready` `Instance` recorded in `list_instances()`; `get_instance` round-trips; `stop_instance`/`destroy_instance` transition status; destroyed instances leave `list_instances`.
- [ ] `destroy_instance` is idempotent and confirms removal.
- [ ] `heartbeat(id)` records a timestamp from the injected clock.
- [ ] `FakeClock.advance(s)` moves `.now()` forward deterministically.
- [ ] Self-registers under `"local"`.

**Verify:** `pixi run test tests/providers/test_local.py tests/core/test_clock.py` → pass.

**Steps:** red→green. `FakeClock` is the linchpin for Task 17-18. Commit `feat: add LocalProvider and injectable clock`.

---

### Task 10: Provisioner (shared steps + engine delegation)

**Goal:** `provision(engine, cfg, instance, *, creds, downloader)` resolves model refs via the registry, merges per-entry sha256, downloads in parallel **only if `engine.requires_local_weights`**, runs the optional `post_provision` hook, then calls `engine.provision(instance, cfg)`.

**Files:**
- Create: `src/kinoforge/core/provisioner.py`
- Test: `tests/core/test_provisioner.py`

**Acceptance Criteria:**
- [ ] With an engine where `requires_local_weights=True`, each model ref is resolved through `source_for_ref` and downloaded (assert downloader called with the resolved artifacts; sha256 from config merged on).
- [ ] With `requires_local_weights=False` (hosted/fake), **no** download occurs but refs are still parsed (assert downloader NOT called, `engine.provision` still called).
- [ ] `post_provision` hook path, when set, is invoked (mock subprocess); absent → skipped.
- [ ] `engine.provision(instance, cfg)` is always delegated last.

**Verify:** `pixi run test tests/core/test_provisioner.py` → pass.

**Steps:** red→green using FakeEngine + FakeSource (registered) + a spy downloader. Commit `feat: add core provisioner orchestrating shared provision steps`.

---

### Task 11: ArtifactStore (base + local)

**Goal:** `ArtifactStore` ABC + `LocalArtifactStore` writing under `run_id` namespaces; read/write bytes + JSON; supports the profile cache (Task 12) and clip outputs.

**Files:**
- Create: `src/kinoforge/stores/__init__.py`, `src/kinoforge/stores/base.py`, `src/kinoforge/stores/local.py`
- Test: `tests/stores/test_local.py`

**Acceptance Criteria:**
- [ ] `put_bytes(run_id, name, data)` → `Artifact` with `uri`; `get_bytes(uri)` round-trips.
- [ ] `put_json`/`get_json` round-trip dicts.
- [ ] Items land under `<root>/<run_id>/...`; two run_ids are isolated.
- [ ] `list(run_id)` enumerates items; `delete(uri)` removes one.
- [ ] Self-registers under `"local"` (store registry mirrors source/provider pattern — add `register_store`/`get_store` to `registry.py` here).

**Verify:** `pixi run test tests/stores/test_local.py` → pass.

**Steps:** red→green. Extend `registry.py` with store registration (TDD: add a registry test too). Commit `feat: add local ArtifactStore with run-scoped namespacing`.

---

## Phase 4 — Profiles, validation, strategy, pool, pipeline, orchestrator

### Task 12: ModelProfileProvider (cache + discover + verify + single-flight)

**Goal:** A profile cache keyed by `CapabilityKey.derive()`, persisted via `ArtifactStore`; `resolve` is a no-compute cache read; `discover` single-flight probes a backend + merges declared flags + persists; `verify` re-probes probeable fields and raises `CapabilityMismatch` on drift.

**Files:**
- Create: `src/kinoforge/core/profiles.py`
- Test: `tests/core/test_profiles.py`

**Acceptance Criteria:**
- [ ] `resolve(key)` on a miss raises `ProfileNotCached` (no backend, no model touched).
- [ ] `resolve(key)` on a hit returns the profile with **no compute** — a test passes no backend at all and reads `max_segment_seconds`.
- [ ] `discover(key, engine, backend)` calls `backend.inspect_capabilities()` exactly once, merges `engine.declared_flags(key)` (native/joint flags), persists, returns; a later `resolve` returns the same profile.
- [ ] Capability-key distinctness: base vs base+lora, and same base under two engines, produce distinct cache entries; the lora variant can carry `supports_native_extension=True` while bare base is False.
- [ ] Single-flight: two threads calling a `resolve-or-discover` helper for the same uncached key trigger exactly ONE `inspect_capabilities()`; both get the same persisted profile (assert a call counter == 1).
- [ ] Undeclared flags default False AND emit a WARNING naming the CapabilityKey (assert via `caplog`).
- [ ] `verify(profile, backend)`: matching probe passes; a probe disagreeing on frames/fps/resolution/modes raises `CapabilityMismatch` (flags not checked — document).

**Verify:** `pixi run test tests/core/test_profiles.py` → pass.

**Steps:** red→green. Single-flight via a `threading.Lock` + per-key `dict[str, threading.Event]` or a lock-per-key map; the provider exposes `resolve_or_discover(key, engine, backend)` used by the orchestrator. Persist as JSON via the store (key = `profiles/<derive>.json`). Use `FakeEngine`/`FakeBackend` with a call-counting `inspect_capabilities`. Commit `feat: add self-populating ModelProfile cache with single-flight discovery`.

---

### Task 13: Request validation (mode + role-authoritative)

**Goal:** `validate_request(profile, request)` enforces mode ∈ `supported_modes` and the `MODE_ROLE_REQUIREMENTS[mode]` role contract; single-asset modes may default a lone image to `init_image`.

**Files:**
- Create: `src/kinoforge/core/validation.py`
- Test: `tests/core/test_validation.py`

**Acceptance Criteria:**
- [ ] Mode not in `supported_modes` → `ValidationError`.
- [ ] flf2v requires exactly one `image` `first_frame` and one `image` `last_frame`; missing/duplicated/wrong-kind → `ValidationError`.
- [ ] Two `init_image`s in i2v → `ValidationError` (duplicated required role).
- [ ] A lone image with no role in a single-asset mode (i2v) defaults to `init_image` and passes; multi-asset modes require explicit roles.
- [ ] An asset whose `kind` the engine can't handle is rejected — modeled via an `accepted_kinds` arg (engine-declared); audio asset to image-only engine → `ValidationError`.

**Verify:** `pixi run test tests/core/test_validation.py` → pass.

**Steps:** red→green pure function. Commit `feat: add mode + role-authoritative request validation`.

---

### Task 14: Strategy decision point

**Goal:** Pure `decide(profile, segments, params, spec) -> list[GenerationJob]`: native-extension True → ONE job with all N segments; False → N single-segment jobs. Audio flag selects joint vs separate stage (separate **stubbed**).

**Files:**
- Create: `src/kinoforge/core/strategy.py`
- Test: `tests/core/test_strategy.py`

**Acceptance Criteria:**
- [ ] `supports_native_extension=True` → `len(result) == 1` and that job carries all segments in order.
- [ ] `supports_native_extension=False` → `len(result) == N`, each a single-segment job; per-segment prompts/assets/params preserved.
- [ ] `Segment.params` override `GenerationJob.params` (segment-wins merge) in both branches.
- [ ] `supports_joint_audio=False` routes to the (stubbed) separate-audio path marker; `=True` keeps audio in the job. The separate-audio path is `# DEFERRED:` (raises `NotImplementedError` only if actually invoked to produce audio — packaging still works).
- [ ] Function is pure (no I/O, no globals).

**Verify:** `pixi run test tests/core/test_strategy.py` → pass.

**Steps:** red→green, both branches explicitly tested. Commit `feat: add long-video strategy decision point`.

---

### Task 15: BackendPool/SequentialPool + Stage + GenerateClipStage

**Goal:** `BackendPool` ABC + `SequentialPool` returning resolved Futures; `Stage` protocol + `GenerateClipStage` that submits a `GenerationJob` through a pool and writes the result `Artifact` to the `ArtifactStore`.

**Files:**
- Create: `src/kinoforge/core/pool.py`
- Create: `src/kinoforge/pipeline/__init__.py`, `src/kinoforge/pipeline/stage.py`, `src/kinoforge/pipeline/generate_clip.py`
- Test: `tests/core/test_pool.py`, `tests/pipeline/test_generate_clip.py`

**Acceptance Criteria:**
- [ ] `SequentialPool.submit(job)` returns a `Future` already `done()` with the backend's `result` Artifact.
- [ ] `map(jobs)` returns results in input order.
- [ ] Pool-swap: a second `BackendPool` impl (a trivial `ListPool` in the test) satisfies the same `submit`/`map` contract with no caller changes — assert the GenerateClipStage works with either.
- [ ] `GenerateClipStage.run(request)` runs discovery→validate→strategy→submit→result→store and returns an `Artifact` whose `uri` is in the store (e2e against LocalProvider+FakeEngine).
- [ ] The stage submits ONE call for a native-extension FakeEngine and N for a non-native one (delegates to `strategy.decide`).

**Verify:** `pixi run test tests/core/test_pool.py tests/pipeline/test_generate_clip.py` → pass.

**Steps:** red→green. `SequentialPool` runs jobs inline through `backend.submit`/`result`, wraps in a resolved `Future`. `GenerateClipStage` takes a backend (or pool), store, profile (or profile provider + key), and a request; for the single-clip happy path it builds one Segment from the request (assets copied in) then calls `strategy.decide`. Commit `feat: add SequentialPool, Stage protocol, and GenerateClipStage`.

---

### Task 16: Orchestrator (deploy + generate flows, discovery ordering, fail-hard teardown, dry-run)

**Goal:** Tie the pieces into the two flows. `deploy(config, *, dry_run)`: load→derive key→(if `requires_compute`) find_offers→create_instance(guardrails+self-term cred)→poll ready→report endpoints; hosted skips compute; `--dry-run` makes zero network/provider calls and prints the neutral plan. `generate(config, request)`: the guaranteed discovery ordering (resolve→discover-on-miss→validate_request→split(stub:1 segment)→provision backend→verify→strategy→pool→result→store), with **fail-hard teardown** when `verify` raises.

**Files:**
- Create: `src/kinoforge/core/orchestrator.py`
- Test: `tests/core/test_orchestrator.py`

**Acceptance Criteria:**
- [ ] `deploy(cfg, dry_run=True)` returns/prints a vendor- & engine-neutral plan and the provider's `create_instance`/`find_offers` are **never called** (spy asserts zero calls).
- [ ] `deploy` for a hosted engine (`requires_compute=False`) provisions **no** instance (provider never constructed) yet returns endpoints from the engine.
- [ ] `generate` runs the full ordering for an uncached key: discover populates the profile, request is validated, splitter stub yields 1 segment, backend provisioned, verify runs, a clip Artifact is produced in the store.
- [ ] Fail-hard: when `verify` raises `CapabilityMismatch`, `generate` re-raises AND `provider.destroy_instance` (or serverless stop) is invoked exactly once — assert teardown called (spy), no clip produced.
- [ ] The splitter stub is marked `# DEFERRED:` and returns exactly one segment with the request assets copied in.

**Verify:** `pixi run test tests/core/test_orchestrator.py` → pass.

**Steps:** red→green using LocalProvider + FakeEngine + FakeSource + in-memory store + FakeClock. Commit `feat: add orchestrator deploy/generate flows with fail-hard discovery ordering`.

---

## Phase 5 — Cost-safety

### Task 17: Effective-deadline math + timers + warm reuse + drain + liveness

**Goal:** `core/lifecycle.py` part 1: `effective_deadline(segments, job_timeout, time_buffer)`, idle warm-reuse window, `max_lifetime` graceful drain, dead-man liveness rule — all driven by the injectable clock against LocalProvider.

**Files:**
- Create: `src/kinoforge/core/lifecycle.py`
- Test: `tests/core/test_lifecycle.py`

**Acceptance Criteria (maps DoD a–d, i):**
- [ ] `effective_deadline(n, job_timeout, buffer) == n * job_timeout + buffer`; a 4-segment native job gets ≈ `4 × job_timeout`, not a flat cap.
- [ ] Warm reuse: a job arriving within `idle_timeout` reuses the instance (no new `create_instance`); after `idle_timeout` of no jobs the instance is reaped (clock-advanced test).
- [ ] Graceful drain: at `max_lifetime` the instance stops accepting NEW jobs but the in-flight job finishes (or hits its effective deadline) before teardown — never killed mid-job.
- [ ] In-flight liveness: a single job running > 2× idle window does NOT trip the dead-man's switch (in-flight under deadline = liveness); an idle pod past the window with no `heartbeat()` DOES self-terminate.
- [ ] Defaults: `idle_timeout=2h`, `job_timeout=30m`, `max_lifetime=5h` when unset.

**Verify:** `pixi run test tests/core/test_lifecycle.py -k 'deadline or reuse or drain or liveness'` → pass.

**Steps:** red→green, clock mocked via `FakeClock`. Commit `feat: add lifecycle deadlines, warm reuse, graceful drain, dead-man liveness`.

---

### Task 18: Sweeper, ledger, confirmed teardown, budget

**Goal:** `core/lifecycle.py` part 2: a persistent ledger of launched instances (via ArtifactStore/local file), a provider-aware sweeper (`reap`), confirmed teardown (poll+retry+alert), and a budget ceiling.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py`
- Test: `tests/core/test_lifecycle_sweeper.py`

**Acceptance Criteria (maps DoD e–g):**
- [ ] Confirmed teardown: `destroy_confirmed(provider, id)` polls until the instance is gone; if still present after retries it raises `TeardownError` and logs an alert (assert with a provider whose first `destroy` is a no-op).
- [ ] Sweeper: given a provider listing an orphaned/over-age instance, `reap(provider, policy, clock)` destroys it without the orchestrator running (assert destroy called for the over-age one, not the fresh one).
- [ ] Budget: when estimated cumulative spend (`age_hours × cost_rate`) crosses `budget`, the instance is killed (`BudgetExceeded` path → teardown).
- [ ] Ledger records every created instance with tags + created_at and survives a fresh load (write/read round-trip).

**Verify:** `pixi run test tests/core/test_lifecycle_sweeper.py` → pass.

**Steps:** red→green. Sweeper is provider-aware via the `ComputeProvider.list_instances` abstraction (SkyPilot reconciliation is the adapter's concern, Task 21). Commit `feat: add ledger, provider-aware sweeper, confirmed teardown, budget ceiling`.

---

## Phase 6 — More sources

### Task 19a: CivitAISource

**Goal:** `CivitAISource` resolves `civitai:<modelId>[@<versionId>]` to `Artifact`(s) via the CivitAI REST API (HTTP mocked in tests), token from creds.

**Files:** Create `src/kinoforge/sources/civitai/__init__.py`; Test `tests/sources/test_civitai.py`.

**Acceptance Criteria:**
- [ ] `handles("civitai:1234@5678")` True; parses modelId/versionId.
- [ ] `resolve` (with a mocked HTTP fetch returning a sample API JSON) yields `Artifact`(s) with download url + filename + sha256 from the API payload; token added to headers from `creds.get("CIVITAI_TOKEN")`.
- [ ] Missing token where the API needs auth → `AuthError` (simulate 401 from the mocked fetch).
- [ ] Self-registers under scheme `"civitai"`.

**Verify:** `pixi run test tests/sources/test_civitai.py` → pass.

**Steps:** red→green; inject the HTTP fetch fn (same seam as downloader) so no network. Use a small captured sample of the CivitAI version JSON shape in the test. Commit `feat: add CivitAI model source`.

### Task 19b: HuggingFaceSource

**Goal:** `HuggingFaceSource` resolves `hf:<repo>[:<path>]` to `Artifact`(s); token from `HF_TOKEN`; stdlib urllib against the HF resolve URL pattern (`https://huggingface.co/<repo>/resolve/main/<path>`).

**Files:** Create `src/kinoforge/sources/huggingface/__init__.py`; Test `tests/sources/test_huggingface.py`.

**Acceptance Criteria:**
- [ ] `handles("hf:org/model")` True; `hf:org/model:subdir/file.safetensors` parses repo + path.
- [ ] With an explicit path → one Artifact at the resolve URL with correct filename. (Repo-wide listing is `# DEFERRED:` — single-file path is the one real path; a bare repo ref raises a clear "specify a file path" error for now, or lists via the API if trivially mockable — choose single-file-required to stay minimal.)
- [ ] Token added to headers when present.
- [ ] Self-registers under scheme `"hf"`.

**Verify:** `pixi run test tests/sources/test_huggingface.py` → pass.

**Steps:** red→green. Commit `feat: add HuggingFace model source`.

---

## Phase 7 — ComfyUI engine + RunPod provider

### Task 20a: ComfyUIEngine (+ git node installer)

**Goal:** `ComfyUIEngine` (`requires_compute=True`, `requires_local_weights=True`): `provision` installs pinned ComfyUI + custom nodes (git clone + requirements/install.py) + routes model files to target subdirs + launches with configured flags; `backend()` drives the ComfyUI HTTP graph API (`/prompt`, `/history`); `inspect_capabilities` reads model/workflow metadata; `validate_spec` checks the graph-template keys.

**Files:** Create `src/kinoforge/engines/comfyui/__init__.py`, `.../nodes.py` (git installer); Test `tests/engines/test_comfyui.py`.

**Acceptance Criteria:**
- [ ] `provision` is fully driven by cfg; all subprocess/git/HTTP calls are injected seams (no real git/network in tests). Asserts: clones each `custom_nodes[].git`, installs requirements, routes each model `target` to the right subdir, launches with `launch_args`.
- [ ] `backend().submit(job)` POSTs a graph derived from `job.spec` (template + node overrides) to `/prompt` (mocked) and `result()` polls `/history` (mocked) → `Artifact`.
- [ ] `validate_spec` raises `ValidationError` for a spec missing the graph template / required node ids.
- [ ] `declared_flags(key)` returns the engine's per-key native/joint declarations from cfg (so a WanVideoWrapper+SVI key can declare `supports_native_extension=True`).
- [ ] Self-registers under `"comfyui"`.

**Verify:** `pixi run test tests/engines/test_comfyui.py` → pass.

**Steps:** red→green with all I/O behind injected callables (`run_cmd`, `http_post`, `http_get`). Commit `feat: add ComfyUI engine with git node installer`.

### Task 20b: RunPodProvider (pod + serverless)

**Goal:** `RunPodProvider` implementing `ComputeProvider` for both **pod** and **serverless** modes (mode from config), with the in-pod self-terminator install at create (pod mode), least-privilege terminate-only credential injection, serverless proactive caps, and `endpoints` via the `https://{id}-{port}.proxy.runpod.net` pattern. All RunPod REST via an injected HTTP seam.

**Files:** Create `src/kinoforge/providers/runpod/__init__.py`, `.../selfterm.py` (in-pod script template); Test `tests/providers/test_runpod.py`.

**Acceptance Criteria:**
- [ ] `find_offers` (mocked GPU-types API) filters via `filter_offers` incl. CUDA version.
- [ ] `create_instance` (pod): posts a create with guardrail env + injects the terminate-only cred via `CredentialProvider` as an env secret (assert the main `RUNPOD_API_KEY` is NOT used as the in-pod cred; a distinct scoped key name is injected), and installs the self-term script. Cred never appears in returned `Instance` or any logged config.
- [ ] `create_instance` (serverless): no in-pod timers; sets `max_workers`/`max_in_flight`/per-request deadline from `Lifecycle`.
- [ ] `endpoints(instance)` produces the proxy URL pattern for each configured port.
- [ ] `destroy_instance` polls the get API until the pod is gone (mocked to disappear after N polls).
- [ ] Self-registers under `"runpod"`.

**Verify:** `pixi run test tests/providers/test_runpod.py` → pass.

**Steps:** red→green; HTTP seam injected, no network. The self-term script is a string template (Linux, runs in-pod) — tested for content (contains max_lifetime drain, deadline enforcement, heartbeat), not executed. Commit `feat: add RunPod provider (pod + serverless) with self-terminating instances`.

---

## Phase 8 — Diffusers + Hosted engines, SkyPilot provider

### Task 21a: DiffusersEngine

**Goal:** `DiffusersEngine` (`requires_compute=True`, `requires_local_weights=True`): `provision` installs pip deps + caches weights + starts a small headless job server; `backend` submits to that server (injected HTTP seam); `validate_spec` checks pipeline/scheduler keys.

**Files:** Create `src/kinoforge/engines/diffusers/__init__.py`; Test `tests/engines/test_diffusers.py`.

**Acceptance Criteria:** provision driven by cfg (seams injected, no torch/network); submit/result round-trip via mocked server; `validate_spec` rejects bad `spec`; self-registers `"diffusers"`.

**Verify:** `pixi run test tests/engines/test_diffusers.py` → pass. Commit `feat: add Diffusers engine`.

### Task 21b: HostedAPIEngine (no-compute path)

**Goal:** `HostedAPIEngine` (`requires_compute=False`, `requires_local_weights=False`): `provision` validates creds/endpoint only; `backend` calls the remote API (mocked); key-base derived from `hosted.model`; `models:` block validation-only, never fetched.

**Files:** Create `src/kinoforge/engines/hosted/__init__.py`; Test `tests/engines/test_hosted.py`.

**Acceptance Criteria:**
- [ ] `requires_compute is False` and the generate path runs with NO instance (test passes `instance=None` end-to-end → Artifact).
- [ ] `provision` validates endpoint+creds (missing cred → `AuthError`); never downloads weights.
- [ ] `validate_spec` checks the hosted payload keys; self-registers `"hosted"`.

**Verify:** `pixi run test tests/engines/test_hosted.py` → pass. Commit `feat: add hosted-API engine proving the no-compute path`.

### Task 21c: SkyPilotProvider (lazy import)

**Goal:** `SkyPilotProvider` wrapping SkyPilot; the only place `import sky`/`skypilot` appears; maps `idle_timeout`→autostop; sweeper reconciles via `sky status`; documents the timer-model trade-off.

**Files:** Create `src/kinoforge/providers/skypilot/__init__.py`; Test `tests/providers/test_skypilot.py`.

**Acceptance Criteria:**
- [ ] `import skypilot` (or `sky`) appears ONLY in this module (verified by Task 24 invariant test).
- [ ] The SkyPilot SDK is accessed behind a lazy import + an injected client seam so tests run without skypilot installed (`pytest.importorskip` not needed — the client is injected/mocked).
- [ ] `create_instance` maps `idle_timeout`→autostop; `list_instances` reflects `sky status`; `destroy_instance` confirms via status.
- [ ] Self-registers under `"skypilot"`.

**Verify:** `pixi run test tests/providers/test_skypilot.py` → pass. Commit `feat: add SkyPilot provider with autostop mapping`.

---

## Phase 9 — CLI, examples, README, CI

### Task 22: CLI + `__main__`

**Goal:** `argparse` CLI: `deploy | provision | generate | list | status | stop | destroy | reap | gc`, each `--config` driven, `--dry-run` where sensible; surfaces running instances + age + estimated spend on every invocation; refuses a duplicate pod for the same job.

**Files:** Create `src/kinoforge/cli.py`; Modify `src/kinoforge/__main__.py`; Test `tests/test_cli.py`.

**Acceptance Criteria:**
- [ ] `kinoforge deploy --config <wan.yaml> --dry-run` prints a correct vendor/engine-neutral plan with zero provider/network calls (assert via spies; capture stdout).
- [ ] `generate` against local+fake config produces a clip artifact (e2e through the orchestrator).
- [ ] `gc --run <id>` and `gc --older-than <dur>` invoke the store GC; `reap` invokes the sweeper.
- [ ] Each invocation prints running instances + age + estimated spend (from the ledger).
- [ ] Unknown engine/provider/scheme in config → clear error, non-zero exit.

**Verify:** `pixi run test tests/test_cli.py` → pass; `python -m kinoforge deploy --config examples/configs/local-fake.yaml --dry-run` prints a plan.

**Steps:** red→green; import the adapter packages in one place (`cli.py` or a `kinoforge/_adapters.py`) so they self-register — this is the ONE allowed place that imports concrete adapters (it is not `core/`). Commit `feat: add CLI with dry-run, gc, reap, and spend visibility`.

### Task 23: Examples, README, CI

**Goal:** Example configs proving config-only swaps; README (quickstart, extend-a-provider/source/engine guide, roadmap of deferred layers, SkyPilot design-credit); GitHub Actions CI running lint+types+tests on Linux/macOS/Windows.

**Files:** Create `examples/configs/{wan,diffusers,hosted,local-fake}.yaml`; `README.md`; `.github/workflows/ci.yml`; Test `tests/test_examples.py`.

**Acceptance Criteria:**
- [ ] Each example config loads + validates via `load_config` (test parametrized over the four files).
- [ ] Swapping `compute.provider` runpod↔local, `engine.kind` comfyui↔diffusers, and adding a different-scheme model are all config-only (a test loads variant strings and asserts no code path branches on the concrete name beyond the registry).
- [ ] README contains the required sections (quickstart, extending guide, roadmap naming deferred layers + their seams, SkyPilot credit). (Spot-checked by a test asserting key headings exist.)
- [ ] CI workflow runs `pixi run lint`, `pixi run typecheck`, `pixi run test` on `ubuntu-latest`, `macos-latest`, `windows-latest`.

**Verify:** `pixi run test tests/test_examples.py` → pass; `pixi run lint && pixi run typecheck && pixi run test` → all green locally.

**Steps:** Write configs first (red test loads them), then README, then CI. Commit `feat: add example configs, README, and 3-OS CI`.

### Task 24: Core-invariant guard test

**Goal:** Lock principle 1 with an automated test: importing `kinoforge.core.*` pulls in NO `providers`/`sources`/`engines` module, and concrete vendor imports live only in their adapter packages.

**Files:** Create `tests/test_core_invariant.py`.

**Acceptance Criteria:**
- [ ] After `import kinoforge.core.orchestrator` (+ other core modules) in a fresh subprocess, no `kinoforge.providers.*`/`kinoforge.sources.*`/`kinoforge.engines.*` appears in `sys.modules`.
- [ ] A static scan asserts `import sky`/`skypilot` text appears only under `src/kinoforge/providers/skypilot/`, and `runpod`/`civitai`/`comfyui` vendor imports only under their packages.
- [ ] No `core/` file contains `import kinoforge.providers`/`.sources`/`.engines`.

**Verify:** `pixi run test tests/test_core_invariant.py` → pass.

**Steps:** Use a subprocess (`python -c`) so import side effects are isolated; use `rg`/file scan for the static check. This is the reviewer's enforced invariant, made executable. Commit `test: enforce core-never-imports-vendor invariant`.

---

## Final acceptance pass

After Task 24:
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] `pixi run test-cov` green; review coverage on core logic.
- [ ] Walk the SPEC.md "Definition of done" list; tick each against its test.
- [ ] Update + commit `PROGRESS.md` to "all phases complete".

---

## Self-review notes (author)

- **Spec coverage:** every DoD bullet maps to a task — dry-run plan (T16/T22), local+fake e2e (T15/T16), config-only swaps (T23), hosted no-compute (T21b), capability discovery + max_segment_seconds (T12), plan-time cache hit (T12), key distinctness (T12), self-healing discovery (T12), single-flight (T12), fail-hard teardown (T16), find_offers filtering (T5), mode/role validation (T13), validate_spec (T8/T20a/T21), strategy both branches (T14), segment packaging (T14/T15), declared-flags merge + under-use warning (T12), e2e artifact via store (T15), SequentialPool swap (T15), cost-safety a–i (T17/T18 + config T4), 3-OS CI (T23), README extend guide (T23). Core-never-imports invariant (T24).
- **Deferred seams** (`# DEFERRED:`): splitter (T16 stub), stitching/continuity + separate-audio (T14), concurrent pool (T15), keyframe upstream Stage (T15 note), S3/GCS stores (T11), cross-process discovery lock (T12), HF repo-wide listing (T19b), aria2c opportunistic (T6).
- **Type consistency:** all later tasks reference the exact names from Task 1 (`CapabilityKey`, `ModelProfile`, `Lifecycle`, `Offer`, `Instance`, `GenerationJob`, `Segment`, `GenerationRequest`, `Artifact`, `ConditioningAsset`, `BackendPool`, `Stage`).
- **Dependency justifications:** pydantic v2 (typed config), PyYAML (load), skypilot (one provider, lazy). Everything else stdlib (urllib, threading, concurrent.futures, logging, hashlib, argparse).
