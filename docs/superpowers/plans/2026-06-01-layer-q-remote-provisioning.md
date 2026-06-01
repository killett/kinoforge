# Layer Q — Cross-engine cross-provider remote provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the cross-engine cross-provider remote-provisioning layer (`engine.render_provision` + `InstanceSpec.provision_script` + `engine.wait_for_ready`) that unblocks Layer P Task 7 item #3 and gives every future cloud engine/provider a single canonical bootstrap path.

**Architecture:** Engine emits a self-contained bash script + run cmd + image + ports + required cred env vars via `render_provision(cfg)`. Orchestrator validates creds + attaches the rendered payload to `InstanceSpec` before `provider.create_instance`. Provider injects via its native boot path (RunPod via base64 env var + `dockerArgs`; SkyPilot via `Task.setup`/`Task.run`; LocalProvider silently ignores). Engine `provision()` for the remote case calls `wait_for_ready` which polls an engine-specific ready endpoint until HTTP-200, status flips terminal, or `cfg.lifecycle.boot_timeout_s` elapses. Local users see zero behavioural change.

**Tech Stack:** Python 3.11+, pydantic v2, pytest, ruff, mypy. New imports: `base64`, `shlex` (stdlib). No new third-party deps.

**Spec:** `docs/superpowers/specs/2026-06-01-layer-q-remote-provisioning-design.md`.

---

## File structure

**New code:**

| File | Purpose |
|---|---|
| `tests/core/test_rendered_provision.py` | `RenderedProvision` dataclass shape; `InstanceSpec.provision_script` / `run_cmd` field defaults |
| `tests/core/test_provision_errors.py` | `ProvisionFailed` + `ProvisionTimeout` subclass `KinoforgeError` |
| `tests/core/test_lifecycle_boot_timeout.py` | `LifecycleConfig.boot_timeout_s` field round-trips through YAML |
| `tests/engines/test_comfyui_render_provision.py` | snapshot + parametrised `ComfyUIEngine.render_provision` tests |
| `tests/engines/test_comfyui_wait_for_ready.py` | 3 polling/timeout tests for `ComfyUIEngine.wait_for_ready` |
| `tests/engines/test_comfyui_provision_branch.py` | `ComfyUIEngine.provision` branches local-vs-remote correctly |
| `tests/engines/test_diffusers_render_provision.py` | parity for diffusers |
| `tests/engines/test_diffusers_wait_for_ready.py` | parity for diffusers |
| `tests/engines/test_diffusers_provision_branch.py` | parity for diffusers |
| `tests/providers/test_runpod_provision_script.py` | RunPod base64 env-var injection + `dockerArgs` assembly |
| `tests/providers/test_skypilot_provision_script.py` | SkyPilot `Task.setup`/`Task.run` mapping |
| `tests/providers/test_local_ignores_provision_script.py` | LocalProvider regression: silently ignores fields |
| `tests/core/test_orchestrator_render_provision.py` | orchestrator wiring + cred-validate + teardown branches |

**Modified code:**

| File | Notes |
|---|---|
| `src/kinoforge/core/interfaces.py` | Add `RenderedProvision` frozen dataclass (~30 LOC); add `InstanceSpec.provision_script: str \| None = None` and `InstanceSpec.run_cmd: list[str] \| None = None` (2 LOC); add `GenerationEngine.render_provision` + `wait_for_ready` abstract methods (~25 LOC) |
| `src/kinoforge/core/errors.py` | Add `ProvisionFailed` and `ProvisionTimeout` (~10 LOC) |
| `src/kinoforge/core/config.py` | Add `LifecycleConfig.boot_timeout_s: float = 900.0` (1 LOC); thread through `Config.lifecycle()` (1 LOC); add `Lifecycle.boot_timeout_s` field on interfaces.py (1 LOC) |
| `src/kinoforge/engines/comfyui/__init__.py` | Add `render_provision` (~80 LOC) + `wait_for_ready` (~30 LOC) + `provision` local-branch guard (~5 LOC) + module constant `_READY_POLL_INTERVAL_S` (1 LOC) + `_get_instance` constructor seam |
| `src/kinoforge/engines/diffusers/__init__.py` | Parity: `render_provision` (~60 LOC) + `wait_for_ready` (~30 LOC) + `provision` branch + seam |
| `src/kinoforge/providers/runpod/__init__.py` | Extend `_create_pod` to read `spec.provision_script` + `spec.run_cmd`; base64-encode script; assemble `dockerArgs` (~30 LOC) |
| `src/kinoforge/providers/skypilot/__init__.py` | Extend `create_instance` to map `provision_script` → `setup`, `run_cmd` → `run` (~15 LOC) |
| `src/kinoforge/core/orchestrator.py` | Extend `_provision_instance_and_build_backend` (~50 LOC): render → cred-validate → spec.replace → create_instance → wait_for_ready; teardown guard for `ProvisionFailed` / `ProvisionTimeout` |
| `PROGRESS.md` | Layer Q closure block (~35 LOC) |
| `README.md` | "Remote provisioning" subsection (~25 LOC) |

**Unchanged:**

- `src/kinoforge/engines/hosted/__init__.py` — `requires_compute=False`; never receives a remote instance.
- `src/kinoforge/providers/local/__init__.py` — `create_instance` body unchanged; new spec fields are silently ignored because the existing code never reads them. Verified by a regression test only.
- `src/kinoforge/engines/comfyui/__init__.py` body of `provision`'s local branch — wrapped in `if instance is None or instance.provider == "local"` guard; unchanged inside.

---

## Task 1: Foundations — dataclass, spec fields, errors, cfg field

**Goal:** Land the shape-only scaffolding that every later task consumes: `RenderedProvision` dataclass, `InstanceSpec` field extensions, two new error classes, and the `boot_timeout_s` lifecycle field. No behaviour, no engine impl.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`
- Modify: `src/kinoforge/core/errors.py`
- Modify: `src/kinoforge/core/config.py`
- Create: `tests/core/test_rendered_provision.py`
- Create: `tests/core/test_provision_errors.py`
- Create: `tests/core/test_lifecycle_boot_timeout.py`

**Acceptance Criteria:**
- [ ] `RenderedProvision(script, run_cmd, image, ports, env_required)` frozen dataclass exists in `kinoforge.core.interfaces`.
- [ ] `InstanceSpec.provision_script: str | None = None` and `InstanceSpec.run_cmd: list[str] | None = None` fields exist; default to `None`; existing constructions of `InstanceSpec` keep working unchanged.
- [ ] `Lifecycle.boot_timeout_s: float = 900.0` field exists on the dataclass at `interfaces.py:50` (matches the existing `_s`-suffix convention there).
- [ ] `LifecycleConfig.boot_timeout: float = 900.0` field exists in `config.py`; `Config.lifecycle()` threads it through.
- [ ] `ProvisionFailed(KinoforgeError)` and `ProvisionTimeout(KinoforgeError)` exist; both importable from `kinoforge.core.errors`.

**Verify:** `pixi run pytest tests/core/test_rendered_provision.py tests/core/test_provision_errors.py tests/core/test_lifecycle_boot_timeout.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing test for `RenderedProvision`**

Create `tests/core/test_rendered_provision.py`:

```python
"""Lockdown tests for the RenderedProvision dataclass and InstanceSpec field extensions."""

from __future__ import annotations

import dataclasses

import pytest

from kinoforge.core.interfaces import InstanceSpec, RenderedProvision


def test_rendered_provision_carries_all_five_fields() -> None:
    """RenderedProvision must expose script, run_cmd, image, ports, env_required."""
    rp = RenderedProvision(
        script="set -e\necho hi\n",
        run_cmd=["python", "main.py"],
        image="runpod/pytorch:latest",
        ports=["8188"],
        env_required=["HF_TOKEN"],
    )
    assert rp.script == "set -e\necho hi\n"
    assert rp.run_cmd == ["python", "main.py"]
    assert rp.image == "runpod/pytorch:latest"
    assert rp.ports == ["8188"]
    assert rp.env_required == ["HF_TOKEN"]


def test_rendered_provision_is_frozen() -> None:
    """RenderedProvision must be immutable so engines cannot mutate after render."""
    rp = RenderedProvision(
        script="", run_cmd=[], image="", ports=[], env_required=[]
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rp.script = "mutated"  # type: ignore[misc]


def test_instance_spec_provision_script_defaults_to_none() -> None:
    """Existing InstanceSpec callers must keep working without touching new fields."""
    spec = InstanceSpec(image="runpod/pytorch:latest")
    assert spec.provision_script is None
    assert spec.run_cmd is None


def test_instance_spec_accepts_provision_script_and_run_cmd() -> None:
    """Spec carries the rendered payload when callers populate the fields."""
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        provision_script="set -e\ngit clone ...\n",
        run_cmd=["python", "main.py", "--listen", "0.0.0.0"],
    )
    assert spec.provision_script == "set -e\ngit clone ...\n"
    assert spec.run_cmd == ["python", "main.py", "--listen", "0.0.0.0"]
```

- [ ] **Step 2: Run RED — verify import failure**

Run: `pixi run pytest tests/core/test_rendered_provision.py -v`
Expected: FAIL with `ImportError: cannot import name 'RenderedProvision' from 'kinoforge.core.interfaces'`.

- [ ] **Step 3: Add `RenderedProvision` dataclass + `InstanceSpec` fields to `interfaces.py`**

Edit `src/kinoforge/core/interfaces.py`. Add the dataclass between the existing `Lifecycle` block (around line 50) and the `InstanceSpec` block (around line 63). Add the two new fields at the end of `InstanceSpec`:

```python
# Insert AFTER the existing Lifecycle dataclass (after line ~60).
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
```

Then extend `InstanceSpec` (around line 75):

```python
@dataclass
class InstanceSpec:
    # ... existing fields ...
    image: str
    offer: Offer | None = None
    ports: tuple[str, ...] = ()
    volume_gb: int = 0
    volume_mount: str = ""
    lifecycle: Lifecycle = field(default_factory=Lifecycle)
    env: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    # NEW — Layer Q
    provision_script: str | None = None
    run_cmd: list[str] | None = None
```

Add a `boot_timeout_s` field to the existing `Lifecycle` dataclass (around line 50):

```python
@dataclass
class Lifecycle:
    idle_timeout_s: float = 2 * 3600
    job_timeout_s: float = 30 * 60
    time_buffer_s: float = 30 * 60
    max_lifetime_s: float = 5 * 3600
    budget_usd: float = 0.0
    max_workers: int = 1
    max_in_flight: int = 1
    # NEW — Layer Q
    boot_timeout_s: float = 900.0
```

- [ ] **Step 4: Run GREEN for test 1**

Run: `pixi run pytest tests/core/test_rendered_provision.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Write failing test for new errors**

Create `tests/core/test_provision_errors.py`:

```python
"""Lockdown tests for ProvisionFailed and ProvisionTimeout."""

from __future__ import annotations

from kinoforge.core.errors import (
    KinoforgeError,
    ProvisionFailed,
    ProvisionTimeout,
)


def test_provision_failed_is_kinoforge_error() -> None:
    """ProvisionFailed must subclass KinoforgeError so orchestrator catch sites work."""
    exc = ProvisionFailed("pod 'abc' entered terminal status 'terminated' before ready")
    assert isinstance(exc, KinoforgeError)
    assert str(exc) == "pod 'abc' entered terminal status 'terminated' before ready"


def test_provision_timeout_is_kinoforge_error() -> None:
    """ProvisionTimeout must subclass KinoforgeError for symmetric catch."""
    exc = ProvisionTimeout("engine ready check timed out after 900s for pod 'abc'")
    assert isinstance(exc, KinoforgeError)
    assert str(exc) == "engine ready check timed out after 900s for pod 'abc'"


def test_provision_failed_and_timeout_are_distinct_classes() -> None:
    """Distinct classes so callers can branch on root cause (boot crash vs. slow)."""
    assert ProvisionFailed is not ProvisionTimeout
    assert not issubclass(ProvisionFailed, ProvisionTimeout)
    assert not issubclass(ProvisionTimeout, ProvisionFailed)
```

- [ ] **Step 6: Run RED**

Run: `pixi run pytest tests/core/test_provision_errors.py -v`
Expected: FAIL with `ImportError: cannot import name 'ProvisionFailed' from 'kinoforge.core.errors'`.

- [ ] **Step 7: Add error classes to `errors.py`**

Edit `src/kinoforge/core/errors.py`. Append at the end of the file:

```python
class ProvisionFailed(KinoforgeError):
    """Pod boot script crashed — provider reported terminal status before ready."""


class ProvisionTimeout(KinoforgeError):
    """Ready check never returned success within ``boot_timeout_s``."""
```

- [ ] **Step 8: Run GREEN**

Run: `pixi run pytest tests/core/test_provision_errors.py -v`
Expected: 3 PASS.

- [ ] **Step 9: Write failing test for `boot_timeout_s` cfg field**

Create `tests/core/test_lifecycle_boot_timeout.py`:

```python
"""Lockdown tests for LifecycleConfig.boot_timeout round-trip + interfaces.Lifecycle.boot_timeout_s."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Lifecycle


def test_interfaces_lifecycle_has_boot_timeout_s_default_900() -> None:
    """The interfaces dataclass carries the seam consumed by engine.wait_for_ready."""
    lc = Lifecycle()
    assert lc.boot_timeout_s == 900.0


def test_load_config_yaml_default_boot_timeout(tmp_path: Path) -> None:
    """When YAML omits boot_timeout, lifecycle() returns 900s."""
    yaml = textwrap.dedent(
        """\
        engine:
          kind: base
          precision: fp16
          comfyui:
            version: "1.0"
        models:
          - ref: "hf:foo/bar:weight.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
        """
    )
    cfg_path = tmp_path / "k.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.lifecycle().boot_timeout_s == 900.0


def test_load_config_yaml_overrides_boot_timeout(tmp_path: Path) -> None:
    """YAML override of boot_timeout flows through Config.lifecycle()."""
    yaml = textwrap.dedent(
        """\
        engine:
          kind: base
          precision: fp16
          comfyui:
            version: "1.0"
        models:
          - ref: "hf:foo/bar:weight.safetensors"
            kind: base
            target: checkpoints
        compute:
          provider: runpod
          image: runpod/pytorch:latest
          lifecycle:
            budget: 5.0
            boot_timeout: 1800
        """
    )
    cfg_path = tmp_path / "k.yaml"
    cfg_path.write_text(yaml)
    cfg = load_config(cfg_path)
    assert cfg.lifecycle().boot_timeout_s == 1800.0
```

- [ ] **Step 10: Run RED**

Run: `pixi run pytest tests/core/test_lifecycle_boot_timeout.py -v`
Expected: FAIL — `Lifecycle()` has no `boot_timeout_s` attribute (Step 3 already added the interfaces dataclass field, but the cfg-side field doesn't exist yet and `lifecycle()` doesn't thread it through).

- [ ] **Step 11: Extend `LifecycleConfig` + `Config.lifecycle()` in `config.py`**

Edit `src/kinoforge/core/config.py`. In the `LifecycleConfig` class around line 78, add the field:

```python
class LifecycleConfig(BaseModel):
    # ... existing fields ...
    idle_timeout: float = 2 * 3600.0
    job_timeout: float = 30 * 60.0
    time_buffer: float = 30 * 60.0
    max_lifetime: float = 5 * 3600.0
    budget: float
    max_in_flight: int = 1
    # NEW — Layer Q
    boot_timeout: float = 900.0
```

In the `Config.lifecycle()` method around line 585, thread the new field into the returned `InterfaceLifecycle`:

```python
def lifecycle(self) -> InterfaceLifecycle:
    """..."""
    lc = self._effective_lifecycle_config()
    if lc is None:
        return InterfaceLifecycle()

    return InterfaceLifecycle(
        idle_timeout_s=lc.idle_timeout,
        job_timeout_s=lc.job_timeout,
        time_buffer_s=lc.time_buffer,
        max_lifetime_s=lc.max_lifetime,
        budget_usd=lc.budget,
        max_in_flight=lc.max_in_flight,
        # NEW — Layer Q
        boot_timeout_s=lc.boot_timeout,
    )
```

- [ ] **Step 12: Run GREEN**

Run: `pixi run pytest tests/core/test_lifecycle_boot_timeout.py -v`
Expected: 3 PASS.

- [ ] **Step 13: Full-file gate**

Run: `pixi run pytest tests/core/test_rendered_provision.py tests/core/test_provision_errors.py tests/core/test_lifecycle_boot_timeout.py -v && pixi run typecheck && pixi run lint`
Expected: all green.

- [ ] **Step 14: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/errors.py src/kinoforge/core/config.py \
  tests/core/test_rendered_provision.py tests/core/test_provision_errors.py tests/core/test_lifecycle_boot_timeout.py
git commit -m "$(cat <<'EOF'
feat(core): Layer Q foundations — RenderedProvision + spec fields + boot_timeout + errors

- RenderedProvision frozen dataclass (script, run_cmd, image, ports, env_required)
- InstanceSpec.provision_script + run_cmd fields (default None)
- interfaces.Lifecycle.boot_timeout_s = 900.0
- LifecycleConfig.boot_timeout = 900.0 + Config.lifecycle() thread-through
- ProvisionFailed + ProvisionTimeout under KinoforgeError

10 net new tests; mypy + ruff + pre-commit clean.

Scaffolding only — no behaviour yet. Tasks 2–7 wire the engine and provider sides.
EOF
)"
```

---

## Task 2: GenerationEngine ABC additions — `render_provision` + `wait_for_ready`

**Goal:** Add the two new abstract methods to the `GenerationEngine` ABC so subclasses (ComfyUI in Task 3, Diffusers in Task 4) are forced to implement them. Defaults raise `NotImplementedError` so HostedAPIEngine and FakeEngine keep working without touching them.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:313` (`GenerationEngine` class)
- Modify: `src/kinoforge/engines/hosted/__init__.py` (override `render_provision` to raise; `requires_compute=False` means it's never called in practice, but mypy needs an override)
- Modify: `src/kinoforge/engines/fake/__init__.py` (override `render_provision` to return a stub `RenderedProvision`; `wait_for_ready` to no-op)
- Create: `tests/core/test_engine_abc_render_provision.py`

**Acceptance Criteria:**
- [ ] `GenerationEngine.render_provision(cfg) -> RenderedProvision` exists as an abstract method with concrete `NotImplementedError` default.
- [ ] `GenerationEngine.wait_for_ready(instance, *, http_get, sleep, get_instance, timeout_s) -> None` exists as an abstract method with concrete `NotImplementedError` default.
- [ ] `HostedAPIEngine.render_provision` raises `NotImplementedError(f"{cls.__name__} does not support remote provisioning")`.
- [ ] `FakeEngine.render_provision` returns `RenderedProvision(script="echo fake", run_cmd=["sleep", "infinity"], image="fake:latest", ports=["8000"], env_required=[])` so tests can spy.
- [ ] `FakeEngine.wait_for_ready` is a no-op (used by orchestrator-wiring tests).
- [ ] All existing engine tests continue to pass.

**Verify:** `pixi run pytest tests/core/test_engine_abc_render_provision.py tests/engines/test_fake.py tests/engines/test_hosted.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing test for ABC defaults + Fake override**

Create `tests/core/test_engine_abc_render_provision.py`:

```python
"""Lockdown tests for the new GenerationEngine ABC methods."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from kinoforge.core.interfaces import Instance, RenderedProvision
from kinoforge.engines.fake import FakeEngine
from kinoforge.engines.hosted import HostedAPIEngine


def test_fake_engine_render_provision_returns_stub_payload() -> None:
    """FakeEngine returns a deterministic stub so orchestrator-wiring tests can spy."""
    engine = FakeEngine()
    rp = engine.render_provision({})
    assert isinstance(rp, RenderedProvision)
    assert rp.script == "echo fake"
    assert rp.run_cmd == ["sleep", "infinity"]
    assert rp.image == "fake:latest"
    assert rp.ports == ["8000"]
    assert rp.env_required == []


def test_fake_engine_wait_for_ready_returns_immediately() -> None:
    """FakeEngine never blocks; the orchestrator's wiring is exercised."""
    engine = FakeEngine()
    instance = Instance(
        id="fake-1", provider="local", status="ready", created_at=0.0
    )
    calls: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"ok": True}

    def _sleep(_: float) -> None:
        pass

    def _get_instance(_: str) -> Instance:
        return instance

    engine.wait_for_ready(
        instance,
        http_get=_http_get,
        sleep=_sleep,
        get_instance=_get_instance,
        timeout_s=10.0,
    )
    assert calls == []  # no polling needed for fake


def test_hosted_engine_render_provision_raises_not_implemented() -> None:
    """HostedAPIEngine has requires_compute=False; render_provision must refuse."""
    engine = HostedAPIEngine(
        creds=None,  # type: ignore[arg-type]
        http_get=lambda _: {},
        http_post=lambda _url, _body: {},
    )
    with pytest.raises(NotImplementedError, match="does not support remote provisioning"):
        engine.render_provision({})
```

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/core/test_engine_abc_render_provision.py -v`
Expected: FAIL with `AttributeError: 'FakeEngine' object has no attribute 'render_provision'`.

- [ ] **Step 3: Add ABC methods to `GenerationEngine`**

Edit `src/kinoforge/core/interfaces.py:313`. Inside the `GenerationEngine` ABC body (after the existing `declared_flags` method, around line 332), add:

```python
class GenerationEngine(ABC):
    # ... existing methods ...

    def render_provision(self, cfg: dict[str, object]) -> "RenderedProvision":
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

    def wait_for_ready(
        self,
        instance: "Instance",
        *,
        http_get: "Callable[[str], dict[str, Any]]",
        sleep: "Callable[[float], None]",
        get_instance: "Callable[[str], Instance]",
        timeout_s: float,
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

        Raises:
            NotImplementedError: Subclass did not override.
            ProvisionFailed: Pod boot script crashed (status flipped terminal).
            ProvisionTimeout: Ready check never returned success within ``timeout_s``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement wait_for_ready"
        )
```

Add the necessary imports at the top of the file if not already present (`Callable` from `typing`).

- [ ] **Step 4: Override `render_provision` in `FakeEngine`**

Edit `src/kinoforge/engines/fake/__init__.py`. Inside the `FakeEngine` class, add:

```python
def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
    """Return a deterministic stub RenderedProvision for tests.

    Args:
        cfg: Unused.

    Returns:
        A fixed RenderedProvision; tests assert on the values directly.
    """
    del cfg
    return RenderedProvision(
        script="echo fake",
        run_cmd=["sleep", "infinity"],
        image="fake:latest",
        ports=["8000"],
        env_required=[],
    )


def wait_for_ready(
    self,
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
) -> None:
    """No-op for the fake engine — used for orchestrator-wiring tests."""
    del instance, http_get, sleep, get_instance, timeout_s
```

Add the `RenderedProvision` import at the top of the module:

```python
from kinoforge.core.interfaces import (
    # ... existing imports ...
    RenderedProvision,
)
```

- [ ] **Step 5: Override `render_provision` in `HostedAPIEngine`** to raise the documented error explicitly (so the message contains the concrete subclass name even though the ABC default would do the same):

Edit `src/kinoforge/engines/hosted/__init__.py`. Inside the `HostedAPIEngine` class, add:

```python
def render_provision(self, cfg: dict[str, Any]) -> "RenderedProvision":
    """Hosted engines have ``requires_compute=False`` — refuse remote provisioning.

    Args:
        cfg: Unused.

    Raises:
        NotImplementedError: Hosted engines are always remote-already; no boot.
    """
    del cfg
    raise NotImplementedError(
        "HostedAPIEngine does not support remote provisioning"
    )
```

- [ ] **Step 6: Run GREEN**

Run: `pixi run pytest tests/core/test_engine_abc_render_provision.py -v`
Expected: 3 PASS.

- [ ] **Step 7: Regression sweep**

Run: `pixi run pytest tests/engines/ -q`
Expected: All existing engine tests pass (Fake + Hosted + ComfyUI + Diffusers unchanged regression coverage).

- [ ] **Step 8: Full gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/engines/fake/__init__.py src/kinoforge/engines/hosted/__init__.py tests/core/test_engine_abc_render_provision.py`
Expected: green.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/engines/fake/__init__.py \
  src/kinoforge/engines/hosted/__init__.py tests/core/test_engine_abc_render_provision.py
git commit -m "$(cat <<'EOF'
feat(core): Layer Q — GenerationEngine.render_provision + wait_for_ready ABC

- GenerationEngine.render_provision(cfg) -> RenderedProvision (default raises)
- GenerationEngine.wait_for_ready(instance, *, http_get, sleep, get_instance, timeout_s) (default raises)
- FakeEngine returns stub RenderedProvision + no-op wait_for_ready for orchestrator-wiring tests
- HostedAPIEngine.render_provision raises with concrete class name in message

3 net new tests; existing engine tests unchanged.

ComfyUI + Diffusers overrides land in Tasks 3 + 4.
EOF
)"
```

---

## Task 3: ComfyUIEngine — `render_provision` + `wait_for_ready` + `provision` branch

**Goal:** Implement the remote-provisioning surface for ComfyUI. Render a full bootstrap script (clone ComfyUI, clone custom nodes with optional SHA pin, download weights with `$HF_TOKEN` / `$CIVITAI_TOKEN` auth headers, `exec python main.py`). Wait-for-ready polls `GET /system_stats`. `provision()` branches on `instance.provider == "local"`.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (add `render_provision`, `wait_for_ready`, branch `provision`; add `_get_instance` constructor seam; module constant `_READY_POLL_INTERVAL_S`)
- Create: `tests/engines/test_comfyui_render_provision.py`
- Create: `tests/engines/test_comfyui_wait_for_ready.py`
- Create: `tests/engines/test_comfyui_provision_branch.py`

**Acceptance Criteria:**
- [ ] `ComfyUIEngine.render_provision(cfg)` returns a `RenderedProvision` whose `script` clones ComfyUI from `cfg["engine"]["comfyui"]["repo"]` (default `https://github.com/comfyanonymous/ComfyUI`), branch from `cfg["engine"]["comfyui"]["branch"]` (default `master`), runs `pip install -q -r requirements.txt`, clones each `cfg["engine"]["comfyui"]["custom_nodes"]` entry (with optional `ref` SHA checkout), installs each custom-node `requirements.txt` if present, downloads each `cfg["models"]` entry with the source-registry-derived URL and auth header (HF / CivitAI), and ends with `exec python main.py <launch_args>`.
- [ ] Script body is wrapped in `set -euo pipefail` at the top.
- [ ] Idempotency guards: `[ ! -d ComfyUI ]`, `[ ! -d custom_nodes/<name> ]`, `[ ! -f <subdir>/<filename> ]` — all clones / downloads are no-ops on warm pods.
- [ ] `RenderedProvision.env_required` contains every cred env var referenced in the rendered auth headers (e.g. `["CIVITAI_TOKEN", "HF_TOKEN"]` for a mixed-source workflow).
- [ ] `RenderedProvision.image` defaults to `cfg["engine"]["comfyui"]["image"]` or the stock `"runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"`.
- [ ] `RenderedProvision.ports` reflects the port parsed from `--port` in `launch_args`, defaulting to `["8188"]`.
- [ ] `RenderedProvision.run_cmd` is `["python", "main.py", *launch_args]`.
- [ ] `ComfyUIEngine.wait_for_ready(instance, http_get=..., sleep=..., get_instance=..., timeout_s=...)`:
  - Returns when `http_get(<comfyui_url>/system_stats)` does not raise.
  - Raises `ProvisionFailed` when `get_instance(id).status` is `"terminated"` or `"stopped"` between polls.
  - Raises `ProvisionTimeout` when `timeout_s` elapses with no ready response.
  - Sleeps `_READY_POLL_INTERVAL_S = 5.0` seconds between polls.
- [ ] `ComfyUIEngine.provision(instance, cfg)` branches on `instance is None or instance.provider == "local"`:
  - Local branch: existing body unchanged (clone_and_install + route_file + run_cmd).
  - Remote branch: calls `self.wait_for_ready(instance, http_get=self._http_get, sleep=self._sleep, get_instance=self._get_instance, timeout_s=cfg["lifecycle"]["boot_timeout_s"])`.
- [ ] `ComfyUIEngine.__init__` accepts a new `get_instance: Callable[[str], Instance] | None = None` kwarg; default is a stub that raises `NotImplementedError("ComfyUIEngine.get_instance seam not wired — orchestrator must inject provider.get_instance")`.

**Verify:** `pixi run pytest tests/engines/test_comfyui_render_provision.py tests/engines/test_comfyui_wait_for_ready.py tests/engines/test_comfyui_provision_branch.py tests/engines/test_comfyui.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the `render_provision` failing tests**

Create `tests/engines/test_comfyui_render_provision.py`:

```python
"""Snapshot + parametrised tests for ComfyUIEngine.render_provision."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.comfyui import ComfyUIEngine


def _make_engine() -> ComfyUIEngine:
    return ComfyUIEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "comfyui": {
                "custom_nodes": [],
                "launch_args": ["--listen", "0.0.0.0", "--port", "8188"],
            }
        },
        "models": [],
    }


def test_render_provision_returns_rendered_provision() -> None:
    """Sanity — engine emits a RenderedProvision."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    """When cfg doesn't override image, default is the stock RunPod image."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    """cfg.engine.comfyui.image overrides the default."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["image"] = "custom/image:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "custom/image:v1"


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    """Script must fail-fast — set -euo pipefail at the top."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_clones_comfyui_with_guard() -> None:
    """Script clones default repo with idempotency guard."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "[ ! -d ComfyUI ] && git clone --depth 1" in rp.script
    assert "https://github.com/comfyanonymous/ComfyUI" in rp.script


def test_render_provision_respects_repo_branch_override() -> None:
    """cfg.engine.comfyui.repo + branch flow into clone line."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["repo"] = "https://github.com/forky/ComfyUI"
    cfg["engine"]["comfyui"]["branch"] = "experimental"
    rp = _make_engine().render_provision(cfg)
    assert "https://github.com/forky/ComfyUI" in rp.script
    assert "--branch experimental" in rp.script


def test_render_provision_runs_comfyui_requirements_install() -> None:
    """Script installs ComfyUI's own requirements.txt."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert "pip install -q -r requirements.txt" in rp.script


def test_render_provision_custom_node_without_ref_uses_shallow_clone() -> None:
    """Without ref, shallow clone — fast + small disk."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {"git": "https://github.com/kijai/ComfyUI-KJNodes"}
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone --depth 1 https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes"
    ) in rp.script
    assert (
        "[ -f custom_nodes/ComfyUI-KJNodes/requirements.txt ] && "
        "pip install -q -r custom_nodes/ComfyUI-KJNodes/requirements.txt || true"
    ) in rp.script


def test_render_provision_custom_node_with_ref_uses_full_clone_and_checkout() -> None:
    """With ref, full clone + git checkout for SHA pinning (Layer P T2 contract)."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["custom_nodes"] = [
        {
            "git": "https://github.com/kijai/ComfyUI-KJNodes",
            "ref": "abc123def456",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert (
        "[ ! -d custom_nodes/ComfyUI-KJNodes ] && "
        "git clone https://github.com/kijai/ComfyUI-KJNodes "
        "custom_nodes/ComfyUI-KJNodes && "
        "cd custom_nodes/ComfyUI-KJNodes && git checkout abc123def456 && cd ../.."
    ) in rp.script


def test_render_provision_hf_model_with_auth_header_and_env_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HF model emits curl with $HF_TOKEN header AND env_required includes HF_TOKEN."""
    # HuggingFaceSource self-registers on import; force-import.
    import kinoforge.sources.huggingface  # noqa: F401

    cfg = _minimal_cfg()
    cfg["models"] = [
        {
            "src": "hf:Kijai/WanVideo_comfy:wan2.1.safetensors",
            "target": "checkpoints",
        }
    ]
    rp = _make_engine().render_provision(cfg)
    assert "HF_TOKEN" in rp.env_required
    assert "-H \"Authorization: Bearer $HF_TOKEN\"" in rp.script
    assert "[ ! -f models/checkpoints/wan2.1.safetensors ]" in rp.script
    assert "models/checkpoints/wan2.1.safetensors" in rp.script


def test_render_provision_no_models_means_no_env_required() -> None:
    """Empty models list yields empty env_required."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []


def test_render_provision_script_ends_with_exec_run_cmd() -> None:
    """Script's final line is exec python main.py … so it becomes PID 1."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith(
        "exec python main.py --listen 0.0.0.0 --port 8188"
    )


def test_render_provision_run_cmd_matches_launch_args() -> None:
    """run_cmd mirrors the launch_args list with python main.py prefix."""
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]


def test_render_provision_port_parsed_from_launch_args() -> None:
    """--port arg in launch_args is reflected on ports."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0", "--port", "9999"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8188_when_absent() -> None:
    """When --port not in launch_args, default 8188."""
    cfg = _minimal_cfg()
    cfg["engine"]["comfyui"]["launch_args"] = ["--listen", "0.0.0.0"]
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8188"]
```

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/engines/test_comfyui_render_provision.py -v`
Expected: FAIL — `render_provision` raises `NotImplementedError` from the ABC default (Task 2).

- [ ] **Step 3: Implement `render_provision` on `ComfyUIEngine`**

Edit `src/kinoforge/engines/comfyui/__init__.py`. Add a module constant near the existing constants:

```python
_DEFAULT_RUNPOD_IMAGE: str = (
    "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
)
_READY_POLL_INTERVAL_S: float = 5.0
```

Add the method body to the `ComfyUIEngine` class. Place it AFTER the existing `provision()` method:

```python
def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
    """Render a self-contained first-boot bootstrap script for a remote pod.

    The rendered script is idempotent on warm pods: each clone/download is
    guarded by ``[ ! -d ... ]`` / ``[ ! -f ... ]``. Credentials are referenced
    via ``$VAR`` and lifted onto ``spec.env`` by the orchestrator; the script
    string never contains plaintext token values.

    Args:
        cfg: Runtime configuration dict, same shape as ``provision``.

    Returns:
        A :class:`RenderedProvision` ready for orchestrator wiring.
    """
    engine_block = cfg.get("engine", {})
    comfyui_cfg: dict[str, Any] = (
        engine_block.get("comfyui", {}) if isinstance(engine_block, dict) else {}
    )
    repo: str = comfyui_cfg.get("repo", "https://github.com/comfyanonymous/ComfyUI")
    branch: str = comfyui_cfg.get("branch", "master")
    custom_nodes: list[dict[str, Any]] = list(comfyui_cfg.get("custom_nodes", []))
    launch_args_raw: list[str] = list(comfyui_cfg.get("launch_args", []))
    if not launch_args_raw:
        launch_args_raw = ["--listen", "0.0.0.0", "--port", "8188"]

    image: str = comfyui_cfg.get("image", _DEFAULT_RUNPOD_IMAGE)
    models_raw: list[dict[str, Any]] = list(cfg.get("models", []))

    lines: list[str] = [
        "set -euo pipefail",
        "cd /workspace",
        f"[ ! -d ComfyUI ] && git clone --depth 1 --branch {branch} {repo} ComfyUI",
        "cd ComfyUI && pip install -q -r requirements.txt",
    ]

    for node in custom_nodes:
        node_url: str = node["git"]
        node_name: str = node_url.rstrip("/").split("/")[-1]
        if node_name.endswith(".git"):
            node_name = node_name[: -len(".git")]
        ref: str | None = node.get("ref")
        if ref:
            lines.append(
                f"[ ! -d custom_nodes/{node_name} ] && "
                f"git clone {node_url} custom_nodes/{node_name} && "
                f"cd custom_nodes/{node_name} && git checkout {ref} && cd ../.."
            )
        else:
            lines.append(
                f"[ ! -d custom_nodes/{node_name} ] && "
                f"git clone --depth 1 {node_url} custom_nodes/{node_name}"
            )
        lines.append(
            f"[ -f custom_nodes/{node_name}/requirements.txt ] && "
            f"pip install -q -r custom_nodes/{node_name}/requirements.txt || true"
        )

    env_required: list[str] = []
    for entry in models_raw:
        src_ref: str = entry["src"]
        target: str = entry["target"]
        subdir = TARGET_TO_SUBDIR.get(target, f"models/{target}")
        source = registry.get_source_for_ref(src_ref)
        artifacts = source.resolve(src_ref, _NullCredProvider())
        artifact = artifacts[0]
        filename: str = entry.get("filename") or artifact.filename
        auth_header = ""
        for hk, hv in (artifact.headers or {}).items():
            if hk.lower() == "authorization":
                env_var = _extract_env_var(hv)
                if env_var:
                    env_required.append(env_var)
                    auth_header = f' -H "Authorization: Bearer ${env_var}"'
        lines.append(f"mkdir -p {subdir}")
        lines.append(
            f"[ ! -f {subdir}/{filename} ] && "
            f"curl -L --fail{auth_header} '{artifact.url}' -o {subdir}/{filename}"
        )

    port: str = _extract_port(launch_args_raw)
    run_cmd = ["python", "main.py"] + launch_args_raw
    lines.append(
        f"cd /workspace/ComfyUI && exec {' '.join(run_cmd)}"
    )

    return RenderedProvision(
        script="\n".join(lines),
        run_cmd=run_cmd,
        image=image,
        ports=[port],
        env_required=sorted(set(env_required)),
    )
```

Add the supporting module-level helpers and imports at the top of the file:

```python
from kinoforge.core.interfaces import (
    # ... existing imports ...
    RenderedProvision,
)
from kinoforge.core import registry


def _extract_env_var(header_value: str) -> str | None:
    """Parse 'Bearer $VAR' / 'Bearer ${VAR}' and return the VAR name, or None."""
    import re
    match = re.match(r"Bearer\s+\$\{?([A-Z_][A-Z0-9_]*)\}?", header_value)
    return match.group(1) if match else None


def _extract_port(launch_args: list[str]) -> str:
    """Return the value following '--port' in launch_args, default '8188'."""
    for i, arg in enumerate(launch_args[:-1]):
        if arg == "--port":
            return launch_args[i + 1]
    return "8188"


class _NullCredProvider:
    """Stub CredentialProvider used at render time; never returns real creds.

    ``render_provision`` calls ``source.resolve(ref, creds)`` to compute URLs +
    auth-header SHAPES (e.g. ``"Bearer $HF_TOKEN"``). The actual credential
    value is lifted onto ``spec.env`` by the orchestrator, NOT into the script.
    Returning None here is correct: the source uses cred presence to decide
    whether to include the header at all; we want the header rendered with the
    env-var reference regardless.
    """

    def get(self, key: str) -> str | None:
        return f"${key}"
```

NOTE: `_NullCredProvider.get` returns the literal string `"$KEY"` so the existing source code (which substitutes the cred value into the Authorization header) ends up with `"Bearer $HF_TOKEN"` — a literal env-var reference that bash will expand at runtime. This matches the spec's design (`render_provision` output never carries plaintext credentials).

If `_extract_env_var` parsing of `"Bearer $HF_TOKEN"` returns `"HF_TOKEN"`, both halves agree.

- [ ] **Step 4: Run GREEN**

Run: `pixi run pytest tests/engines/test_comfyui_render_provision.py -v`
Expected: all PASS (15 tests).

- [ ] **Step 5: Write the `wait_for_ready` failing tests**

Create `tests/engines/test_comfyui_wait_for_ready.py`:

```python
"""Tests for ComfyUIEngine.wait_for_ready (engine-specific readiness polling)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.comfyui import ComfyUIEngine


def _instance(status: str = "ready") -> Instance:
    return Instance(
        id="pod-abc",
        provider="runpod",
        status=status,
        created_at=0.0,
        endpoints={"8188": "https://pod-abc-8188.proxy.runpod.net"},
    )


def test_wait_for_ready_returns_when_first_http_get_succeeds() -> None:
    """First poll: http_get returns; method returns immediately."""
    inst = _instance()
    calls: list[str] = []

    def _http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"ok": True}

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=_http_get,
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert calls == ["https://pod-abc-8188.proxy.runpod.net/system_stats"]


def test_wait_for_ready_polls_until_http_get_stops_raising() -> None:
    """Endpoint not up yet → retry after sleep → eventually OK."""
    inst = _instance()
    attempt = {"n": 0}

    def _http_get(url: str) -> dict[str, Any]:
        attempt["n"] += 1
        if attempt["n"] < 3:
            raise ConnectionError("pod not up yet")
        return {"ok": True}

    sleeps: list[float] = []

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=_http_get,
        sleep=sleeps.append,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert attempt["n"] == 3
    # Two failed polls → two sleeps before the third succeeds.
    assert sleeps == [5.0, 5.0]


def test_wait_for_ready_raises_provision_failed_on_terminal_status() -> None:
    """Pod boot script crashed → status flips terminated → fast-fail."""
    inst = _instance("starting")
    terminated_inst = dataclasses.replace(inst, status="terminated")
    statuses = iter([inst, terminated_inst])

    def _http_get(url: str) -> dict[str, Any]:
        raise ConnectionError("not ready")

    with pytest.raises(ProvisionFailed) as exc_info:
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            inst,
            http_get=_http_get,
            sleep=lambda _: None,
            get_instance=lambda _: next(statuses),
            timeout_s=60.0,
        )
    assert "pod-abc" in str(exc_info.value)
    assert "terminated" in str(exc_info.value)


def test_wait_for_ready_raises_provision_timeout_after_deadline() -> None:
    """Endpoint never comes up → deadline crossed → ProvisionTimeout."""
    inst = _instance("starting")

    times = iter([0.0, 2.0, 12.0])  # exceeds timeout_s=10.0 on third tick

    def _http_get(url: str) -> dict[str, Any]:
        raise ConnectionError("not ready")

    import kinoforge.engines.comfyui as comfyui_mod
    real_monotonic = comfyui_mod.time.monotonic  # type: ignore[attr-defined]

    def _monotonic() -> float:
        return next(times)

    comfyui_mod.time.monotonic = _monotonic  # type: ignore[attr-defined]
    try:
        with pytest.raises(ProvisionTimeout) as exc_info:
            ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
                inst,
                http_get=_http_get,
                sleep=lambda _: None,
                get_instance=lambda _: inst,
                timeout_s=10.0,
            )
    finally:
        comfyui_mod.time.monotonic = real_monotonic  # type: ignore[attr-defined]
    assert "pod-abc" in str(exc_info.value)
    assert "10" in str(exc_info.value)
```

- [ ] **Step 6: Run RED**

Run: `pixi run pytest tests/engines/test_comfyui_wait_for_ready.py -v`
Expected: FAIL — `wait_for_ready` raises `NotImplementedError`.

- [ ] **Step 7: Implement `wait_for_ready`**

Edit `src/kinoforge/engines/comfyui/__init__.py`. Add the method to the `ComfyUIEngine` class, AFTER `render_provision`:

```python
def wait_for_ready(
    self,
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
) -> None:
    """Poll ``GET <comfyui>/system_stats`` until 200, status terminal, or timeout.

    Args:
        instance: The just-created compute instance.
        http_get: HTTP GET seam — raises on error, returns dict on success.
        sleep: Sleep seam used between polls.
        get_instance: Provider lookup for status checks between polls.
        timeout_s: Maximum total wait.

    Raises:
        ProvisionFailed: Pod entered terminal status before ready.
        ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
    """
    # Pick the comfyui port from instance.endpoints — first matching port wins;
    # fallback to '8188' if the dict is keyed by something else.
    port_key = "8188" if "8188" in instance.endpoints else next(iter(instance.endpoints), "8188")
    base = instance.endpoints.get(port_key, "")
    ready_url = f"{base.rstrip('/')}/system_stats"

    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            http_get(ready_url)
            return
        except Exception:
            pass
        current = get_instance(instance.id)
        if current.status in ("terminated", "stopped"):
            raise ProvisionFailed(
                f"pod {instance.id!r} entered terminal status "
                f"{current.status!r} before ready"
            )
        sleep(_READY_POLL_INTERVAL_S)
    elapsed = time.monotonic() - start
    raise ProvisionTimeout(
        f"engine ready check timed out after {elapsed:.0f}s for pod {instance.id!r}"
    )
```

Add the necessary imports at the top of the file:

```python
import time
from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
```

- [ ] **Step 8: Run GREEN**

Run: `pixi run pytest tests/engines/test_comfyui_wait_for_ready.py -v`
Expected: 4 PASS.

- [ ] **Step 9: Write the `provision`-branch failing tests**

Create `tests/engines/test_comfyui_provision_branch.py`:

```python
"""Tests for ComfyUIEngine.provision's local-vs-remote branch."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.engines.comfyui import ComfyUIEngine


def test_provision_with_none_instance_runs_local_body() -> None:
    """instance=None → original local code path (run_cmd called)."""
    calls: list[tuple[list[str], str]] = []

    def _run_cmd(argv: list[str], cwd: str) -> None:
        calls.append((argv, cwd))

    engine = ComfyUIEngine(
        run_cmd=_run_cmd,
        probe_profile=None,  # type: ignore[arg-type]
    )
    engine.provision(None, {"engine": {"comfyui": {"custom_nodes": [], "launch_args": []}}, "models": []})
    assert len(calls) > 0  # launch step always runs in local body


def test_provision_with_local_provider_runs_local_body() -> None:
    """instance.provider == 'local' → original local code path."""
    calls: list[tuple[list[str], str]] = []
    engine = ComfyUIEngine(
        run_cmd=lambda argv, cwd: calls.append((argv, cwd)),
        probe_profile=None,  # type: ignore[arg-type]
    )
    inst = Instance(id="local-1", provider="local", status="ready", created_at=0.0)
    engine.provision(inst, {"engine": {"comfyui": {"custom_nodes": [], "launch_args": []}}, "models": []})
    assert len(calls) > 0


def test_provision_with_remote_provider_calls_wait_for_ready_not_local_body() -> None:
    """instance.provider == 'runpod' → wait_for_ready, NO subprocess calls."""
    run_cmd_calls: list[Any] = []
    http_get_calls: list[str] = []

    engine = ComfyUIEngine(
        run_cmd=lambda argv, cwd: run_cmd_calls.append((argv, cwd)),
        http_get=lambda url: (http_get_calls.append(url), {"ok": True})[1],
        sleep=lambda _: None,
        probe_profile=None,  # type: ignore[arg-type]
        get_instance=lambda _: inst,
    )
    inst = Instance(
        id="pod-x",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8188": "https://pod-x-8188.proxy.runpod.net"},
    )
    cfg = {"lifecycle": {"boot_timeout_s": 30.0}}
    engine.provision(inst, cfg)
    assert run_cmd_calls == []  # remote branch: no subprocess
    assert http_get_calls == ["https://pod-x-8188.proxy.runpod.net/system_stats"]


def test_provision_remote_uses_boot_timeout_from_cfg_lifecycle() -> None:
    """cfg.lifecycle.boot_timeout_s flows through to wait_for_ready's timeout_s."""
    seen_timeout: list[float] = []

    class _SpyEngine(ComfyUIEngine):
        def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):  # type: ignore[override]
            seen_timeout.append(timeout_s)

    engine = _SpyEngine(probe_profile=None)  # type: ignore[arg-type]
    inst = Instance(
        id="pod-x", provider="runpod", status="ready", created_at=0.0,
        endpoints={"8188": "https://x"},
    )
    engine.provision(inst, {"lifecycle": {"boot_timeout_s": 1234.0}})
    assert seen_timeout == [1234.0]


def test_provision_remote_default_boot_timeout_when_cfg_absent() -> None:
    """No cfg.lifecycle → default 900.0."""
    seen_timeout: list[float] = []

    class _SpyEngine(ComfyUIEngine):
        def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):  # type: ignore[override]
            seen_timeout.append(timeout_s)

    engine = _SpyEngine(probe_profile=None)  # type: ignore[arg-type]
    inst = Instance(
        id="pod-x", provider="runpod", status="ready", created_at=0.0,
        endpoints={"8188": "https://x"},
    )
    engine.provision(inst, {})
    assert seen_timeout == [900.0]
```

- [ ] **Step 10: Run RED**

Run: `pixi run pytest tests/engines/test_comfyui_provision_branch.py -v`
Expected: FAIL — `provision()` runs the existing local body unconditionally on the remote instance test; the `wait_for_ready` test never sees a call.

- [ ] **Step 11: Wire constructor seam + branch `provision`**

Edit `src/kinoforge/engines/comfyui/__init__.py`. Extend the `ComfyUIEngine.__init__` signature with `get_instance`:

```python
def __init__(
    self,
    *,
    run_cmd: Callable[[list[str], str], None] = _default_run_cmd,
    file_exists: Callable[[str], bool] = os.path.isfile,
    route_file: Callable[[str, str], None] = _default_route_file,
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]] = _default_http_post,
    http_get: Callable[[str], dict[str, Any]] = _default_http_get,
    http_get_bytes: Callable[[str, dict[str, str] | None], bytes] = _default_http_get_bytes,
    http_post_file: Callable[[str, str, bytes, str], dict[str, Any]] = _default_http_post_file,
    ffmpeg_run: Callable[[list[str], bytes], bytes] = _default_ffmpeg_run,
    sleep: Callable[[float], None] = time.sleep,
    probe_profile: ModelProfile,
    flags_table: dict[str, dict[str, bool]] | None = None,
    comfyui_root: str = "ComfyUI",
    # NEW — Layer Q
    get_instance: Callable[[str], Instance] | None = None,
) -> None:
    # ... existing assignments ...
    self._get_instance = get_instance or _default_get_instance
```

Add the module-level default seam:

```python
def _default_get_instance(_: str) -> Instance:
    """Stub seam — orchestrator must inject provider.get_instance for remote provision."""
    raise NotImplementedError(
        "ComfyUIEngine.get_instance seam not wired — "
        "orchestrator must inject provider.get_instance"
    )
```

Modify `provision` to branch:

```python
def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
    """Provision ComfyUI on the local machine OR wait for a remote pod to be ready.

    Branches on ``instance.provider``: for ``None`` and ``"local"``, runs the
    original local code path (git clone + pip install + route_file + launch).
    For any other provider, polls :meth:`wait_for_ready`.
    """
    if instance is None or instance.provider == "local":
        # existing local body — unchanged, just indented under the branch
        engine_block = cfg.get("engine", {})
        comfyui_cfg: dict[str, Any] = (
            engine_block.get("comfyui", {}) if isinstance(engine_block, dict) else {}
        )
        custom_nodes: list[dict[str, Any]] = comfyui_cfg.get("custom_nodes", [])
        launch_args: list[str] = comfyui_cfg.get("launch_args", [])
        models_raw = cfg.get("models", [])
        models: list[dict[str, Any]] = (
            list(models_raw) if isinstance(models_raw, list) else []
        )
        clone_and_install(
            node_entries=custom_nodes,
            comfyui_root=self._comfyui_root,
            run_cmd=self._run_cmd,
            file_exists=self._file_exists,
        )
        for entry in models:
            src: str = entry["src"]
            target: str = entry["target"]
            subdir = TARGET_TO_SUBDIR.get(target, f"models/{target}")
            dst_dir = os.path.join(self._comfyui_root, subdir)
            self._route_file(src, dst_dir)
        self._run_cmd(
            ["python", "main.py"] + list(launch_args),
            self._comfyui_root,
        )
        return

    # Remote branch: script already ran via provider boot path; just wait.
    lifecycle_block = cfg.get("lifecycle", {})
    boot_timeout_s = float(
        lifecycle_block.get("boot_timeout_s", 900.0)
        if isinstance(lifecycle_block, dict)
        else 900.0
    )
    self.wait_for_ready(
        instance,
        http_get=self._http_get,
        sleep=self._sleep,
        get_instance=self._get_instance,
        timeout_s=boot_timeout_s,
    )
```

- [ ] **Step 12: Run GREEN**

Run: `pixi run pytest tests/engines/test_comfyui_provision_branch.py -v`
Expected: 5 PASS.

- [ ] **Step 13: Full ComfyUI regression**

Run: `pixi run pytest tests/engines/test_comfyui.py tests/engines/test_comfyui_render_provision.py tests/engines/test_comfyui_wait_for_ready.py tests/engines/test_comfyui_provision_branch.py -v`
Expected: every test passes, including the pre-existing local-path coverage.

- [ ] **Step 14: Full gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui_render_provision.py tests/engines/test_comfyui_wait_for_ready.py tests/engines/test_comfyui_provision_branch.py`
Expected: green.

- [ ] **Step 15: Commit**

```bash
git add src/kinoforge/engines/comfyui/__init__.py \
  tests/engines/test_comfyui_render_provision.py \
  tests/engines/test_comfyui_wait_for_ready.py \
  tests/engines/test_comfyui_provision_branch.py
git commit -m "$(cat <<'EOF'
feat(engines/comfyui): Layer Q — render_provision + wait_for_ready + remote-branch provision

- render_provision(cfg) emits idempotent bash bootstrap: git clone ComfyUI,
  clone custom nodes (shallow or full-with-ref-checkout), install requirements,
  curl weights with $HF_TOKEN / $CIVITAI_TOKEN headers, exec python main.py
- env_required computed from artifact.headers Authorization patterns
- wait_for_ready polls GET <comfyui>/system_stats; ProvisionFailed on terminal
  status; ProvisionTimeout after boot_timeout_s
- provision() branches on instance is None or instance.provider == "local";
  local body unchanged
- _get_instance constructor seam injected by orchestrator (Task 7)

24 net new tests; existing local-path tests unchanged.
EOF
)"
```

---

## Task 4: DiffusersEngine — `render_provision` + `wait_for_ready` + `provision` branch

**Goal:** Parity with Task 3 for the Diffusers engine. Script renders pip install of declared deps + launches the diffusers HTTP server via the same `exec` pattern. Ready check polls `GET /health`. `provision()` branches identically.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Create: `tests/engines/test_diffusers_render_provision.py`
- Create: `tests/engines/test_diffusers_wait_for_ready.py`
- Create: `tests/engines/test_diffusers_provision_branch.py`

**Acceptance Criteria:**
- [ ] `DiffusersEngine.render_provision(cfg)`:
  - Script body starts with `set -euo pipefail`.
  - Runs `pip install -q <pkg1> <pkg2> ...` for each entry in `cfg["engine"]["diffusers"]["pip"]`.
  - Ends with `exec <server_cmd>` where `server_cmd` is `cfg["engine"]["diffusers"]["server_cmd"]`.
  - `image` defaults to `cfg["engine"]["diffusers"]["image"]` or stock `"runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"`.
  - `ports` parsed from `cfg["engine"]["diffusers"]["base_url"]` (extract port from `http://host:PORT/...`); default `["8000"]` when absent.
  - `run_cmd` equals `cfg["engine"]["diffusers"]["server_cmd"]`.
  - `env_required` is `[]` (Diffusers doesn't auth on model downloads — diffusers handles its own hub auth via cached `HUGGINGFACE_HUB_TOKEN`; orchestrator may still lift that env var separately).
- [ ] `DiffusersEngine.wait_for_ready` polls `GET <base_url>/health` with the same `ProvisionFailed` / `ProvisionTimeout` semantics as Task 3.
- [ ] `DiffusersEngine.provision` branches on `instance is None or instance.provider == "local"` (local body unchanged: pip install + server_cmd locally).
- [ ] `DiffusersEngine.__init__` accepts `get_instance` kwarg, same default-stub pattern.

**Verify:** `pixi run pytest tests/engines/test_diffusers_render_provision.py tests/engines/test_diffusers_wait_for_ready.py tests/engines/test_diffusers_provision_branch.py tests/engines/test_diffusers.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing tests for Diffusers `render_provision`**

Create `tests/engines/test_diffusers_render_provision.py`:

```python
"""Snapshot tests for DiffusersEngine.render_provision."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import RenderedProvision
from kinoforge.engines.diffusers import DiffusersEngine


def _make_engine() -> DiffusersEngine:
    return DiffusersEngine(probe_profile=None)  # type: ignore[arg-type]


def _minimal_cfg() -> dict[str, Any]:
    return {
        "engine": {
            "diffusers": {
                "base_url": "http://localhost:8000",
                "pip": [],
                "server_cmd": ["python", "-m", "diffusers_server"],
            }
        }
    }


def test_render_provision_returns_rendered_provision() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert isinstance(rp, RenderedProvision)


def test_render_provision_script_starts_with_set_euo_pipefail() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.startswith("set -euo pipefail")


def test_render_provision_script_runs_pip_install_for_each_dep() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["pip"] = ["diffusers==0.27.0", "transformers", "accelerate"]
    rp = _make_engine().render_provision(cfg)
    assert "pip install -q diffusers==0.27.0 transformers accelerate" in rp.script


def test_render_provision_script_ends_with_exec_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.script.rstrip().endswith("exec python -m diffusers_server")


def test_render_provision_run_cmd_matches_server_cmd() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.run_cmd == ["python", "-m", "diffusers_server"]


def test_render_provision_default_image_is_stock_runpod_pytorch() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.image == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_render_provision_image_override_from_cfg() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["image"] = "myorg/diffusers-base:v1"
    rp = _make_engine().render_provision(cfg)
    assert rp.image == "myorg/diffusers-base:v1"


def test_render_provision_port_parsed_from_base_url() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = "http://localhost:9999"
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["9999"]


def test_render_provision_port_defaults_to_8000_when_base_url_missing() -> None:
    cfg = _minimal_cfg()
    cfg["engine"]["diffusers"]["base_url"] = ""
    rp = _make_engine().render_provision(cfg)
    assert rp.ports == ["8000"]


def test_render_provision_env_required_is_empty() -> None:
    rp = _make_engine().render_provision(_minimal_cfg())
    assert rp.env_required == []
```

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/engines/test_diffusers_render_provision.py -v`
Expected: FAIL — `render_provision` raises `NotImplementedError`.

- [ ] **Step 3: Implement `render_provision` on `DiffusersEngine`**

Edit `src/kinoforge/engines/diffusers/__init__.py`. Add module-level constants:

```python
_DEFAULT_RUNPOD_IMAGE: str = (
    "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
)
_READY_POLL_INTERVAL_S: float = 5.0
```

Add method to the `DiffusersEngine` class:

```python
def render_provision(self, cfg: dict[str, Any]) -> RenderedProvision:
    """Render a first-boot bootstrap script for a remote Diffusers pod.

    Args:
        cfg: Runtime configuration dict.

    Returns:
        A :class:`RenderedProvision` ready for orchestrator wiring.
    """
    engine_block = cfg.get("engine", {})
    diffusers_cfg: dict[str, Any] = (
        engine_block.get("diffusers", {}) if isinstance(engine_block, dict) else {}
    )
    pip_deps: list[str] = list(diffusers_cfg.get("pip", []))
    server_cmd: list[str] = list(diffusers_cfg.get("server_cmd", []))
    base_url: str = str(diffusers_cfg.get("base_url", ""))
    image: str = diffusers_cfg.get("image", _DEFAULT_RUNPOD_IMAGE)

    lines: list[str] = ["set -euo pipefail"]
    if pip_deps:
        lines.append("pip install -q " + " ".join(pip_deps))
    if server_cmd:
        lines.append("exec " + " ".join(server_cmd))

    port = _extract_port_from_base_url(base_url)
    return RenderedProvision(
        script="\n".join(lines),
        run_cmd=server_cmd,
        image=image,
        ports=[port],
        env_required=[],
    )


def _extract_port_from_base_url(base_url: str) -> str:
    """Return the port in ``http(s)://host:PORT/...``, default '8000'."""
    if not base_url:
        return "8000"
    import re
    m = re.search(r":(\d+)", base_url)
    return m.group(1) if m else "8000"
```

Add imports:

```python
from kinoforge.core.interfaces import (
    # ... existing ...
    RenderedProvision,
)
```

- [ ] **Step 4: Run GREEN**

Run: `pixi run pytest tests/engines/test_diffusers_render_provision.py -v`
Expected: 10 PASS.

- [ ] **Step 5: Write failing tests for `wait_for_ready`**

Create `tests/engines/test_diffusers_wait_for_ready.py`:

```python
"""Tests for DiffusersEngine.wait_for_ready."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def _instance(status: str = "ready") -> Instance:
    return Instance(
        id="pod-d",
        provider="runpod",
        status=status,
        created_at=0.0,
        endpoints={"8000": "https://pod-d-8000.proxy.runpod.net"},
    )


def test_wait_for_ready_returns_on_first_success() -> None:
    inst = _instance()
    seen: list[str] = []
    DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        inst,
        http_get=lambda url: (seen.append(url), {"ok": True})[1],
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        timeout_s=60.0,
    )
    assert seen == ["https://pod-d-8000.proxy.runpod.net/health"]


def test_wait_for_ready_raises_provision_failed_on_terminal_status() -> None:
    inst = _instance("starting")
    terminated = dataclasses.replace(inst, status="terminated")
    statuses = iter([inst, terminated])

    with pytest.raises(ProvisionFailed):
        DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            inst,
            http_get=lambda _: (_ for _ in ()).throw(ConnectionError("no")),
            sleep=lambda _: None,
            get_instance=lambda _: next(statuses),
            timeout_s=60.0,
        )


def test_wait_for_ready_raises_provision_timeout_after_deadline() -> None:
    inst = _instance("starting")

    times = iter([0.0, 2.0, 12.0])
    import kinoforge.engines.diffusers as diff_mod
    real = diff_mod.time.monotonic  # type: ignore[attr-defined]
    diff_mod.time.monotonic = lambda: next(times)  # type: ignore[attr-defined]
    try:
        with pytest.raises(ProvisionTimeout):
            DiffusersEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
                inst,
                http_get=lambda _: (_ for _ in ()).throw(ConnectionError("no")),
                sleep=lambda _: None,
                get_instance=lambda _: inst,
                timeout_s=10.0,
            )
    finally:
        diff_mod.time.monotonic = real  # type: ignore[attr-defined]
```

- [ ] **Step 6: Run RED**

Run: `pixi run pytest tests/engines/test_diffusers_wait_for_ready.py -v`
Expected: FAIL — `wait_for_ready` raises `NotImplementedError`.

- [ ] **Step 7: Implement `wait_for_ready`**

Edit `src/kinoforge/engines/diffusers/__init__.py`. Add method to the class:

```python
def wait_for_ready(
    self,
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
) -> None:
    """Poll ``GET <base_url>/health`` until 200, terminal, or timeout.

    Mirror of :meth:`ComfyUIEngine.wait_for_ready` with diffusers-specific
    ready URL.
    """
    port_key = next(iter(instance.endpoints), "8000")
    base = instance.endpoints.get(port_key, "")
    ready_url = f"{base.rstrip('/')}/health"

    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            http_get(ready_url)
            return
        except Exception:
            pass
        current = get_instance(instance.id)
        if current.status in ("terminated", "stopped"):
            raise ProvisionFailed(
                f"pod {instance.id!r} entered terminal status "
                f"{current.status!r} before ready"
            )
        sleep(_READY_POLL_INTERVAL_S)
    elapsed = time.monotonic() - start
    raise ProvisionTimeout(
        f"engine ready check timed out after {elapsed:.0f}s for pod {instance.id!r}"
    )
```

Add imports:

```python
import time
from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
```

- [ ] **Step 8: Run GREEN**

Run: `pixi run pytest tests/engines/test_diffusers_wait_for_ready.py -v`
Expected: 3 PASS.

- [ ] **Step 9: Write the `provision`-branch failing tests**

Create `tests/engines/test_diffusers_provision_branch.py`:

```python
"""Tests for DiffusersEngine.provision local-vs-remote branch."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def test_provision_with_none_instance_runs_local_body() -> None:
    calls: list[Any] = []
    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: calls.append((argv, cwd)),
        probe_profile=None,  # type: ignore[arg-type]
    )
    cfg = {"engine": {"diffusers": {"pip": ["diffusers"], "server_cmd": ["python", "-m", "x"]}}}
    engine.provision(None, cfg)
    # local body runs pip + server_cmd
    assert len(calls) == 2


def test_provision_with_local_provider_runs_local_body() -> None:
    calls: list[Any] = []
    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: calls.append((argv, cwd)),
        probe_profile=None,  # type: ignore[arg-type]
    )
    inst = Instance(id="local-1", provider="local", status="ready", created_at=0.0)
    cfg = {"engine": {"diffusers": {"pip": [], "server_cmd": ["python", "-m", "x"]}}}
    engine.provision(inst, cfg)
    assert len(calls) == 1


def test_provision_with_remote_provider_calls_wait_for_ready_not_local_body() -> None:
    run_cmd_calls: list[Any] = []
    http_get_calls: list[str] = []
    inst = Instance(
        id="pod-d",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-d-8000.proxy.runpod.net"},
    )
    engine = DiffusersEngine(
        run_cmd=lambda argv, cwd: run_cmd_calls.append((argv, cwd)),
        http_get=lambda url: (http_get_calls.append(url), {"ok": True})[1],
        sleep=lambda _: None,
        get_instance=lambda _: inst,
        probe_profile=None,  # type: ignore[arg-type]
    )
    engine.provision(inst, {"lifecycle": {"boot_timeout_s": 30.0}})
    assert run_cmd_calls == []
    assert http_get_calls == ["https://pod-d-8000.proxy.runpod.net/health"]
```

- [ ] **Step 10: Run RED**

Run: `pixi run pytest tests/engines/test_diffusers_provision_branch.py -v`
Expected: FAIL — `provision()` runs local body on remote instance.

- [ ] **Step 11: Wire constructor seam + branch `provision` on `DiffusersEngine`**

Apply the same shape changes as Task 3, Step 11:
- Extend `__init__` with `get_instance: Callable[[str], Instance] | None = None`.
- Add `_default_get_instance` module-level stub.
- Modify `provision()` to branch on `instance is None or instance.provider == "local"`:
  - Local branch: existing body (pip install + server_cmd) — unchanged.
  - Remote branch: extract `boot_timeout_s` from cfg and call `self.wait_for_ready(...)`.

- [ ] **Step 12: Run GREEN**

Run: `pixi run pytest tests/engines/test_diffusers_provision_branch.py -v`
Expected: 3 PASS.

- [ ] **Step 13: Full Diffusers regression**

Run: `pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_render_provision.py tests/engines/test_diffusers_wait_for_ready.py tests/engines/test_diffusers_provision_branch.py -v`
Expected: all PASS.

- [ ] **Step 14: Full gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers_render_provision.py tests/engines/test_diffusers_wait_for_ready.py tests/engines/test_diffusers_provision_branch.py`
Expected: green.

- [ ] **Step 15: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py \
  tests/engines/test_diffusers_render_provision.py \
  tests/engines/test_diffusers_wait_for_ready.py \
  tests/engines/test_diffusers_provision_branch.py
git commit -m "$(cat <<'EOF'
feat(engines/diffusers): Layer Q — render_provision + wait_for_ready + remote-branch provision

- Parity with Task 3 ComfyUI surface.
- render_provision emits pip install + exec server_cmd; image from cfg or stock
- wait_for_ready polls GET /health
- provision() branches on instance is None or instance.provider == "local"

16 net new tests; existing local-path tests unchanged.
EOF
)"
```

---

## Task 5: RunPodProvider `_create_pod` — provision-script encoding

**Goal:** When `spec.provision_script` is set, RunPod's pod-create mutation must base64-encode the script into env var `KINOFORGE_PROVISION_SCRIPT` and use `dockerArgs` to decode + run it. When unset, the existing behaviour (empty `dockerArgs`, image's default ENTRYPOINT) is preserved.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py`
- Create: `tests/providers/test_runpod_provision_script.py`

**Acceptance Criteria:**
- [ ] When `spec.provision_script is None` and `spec.run_cmd is None`, the GraphQL request body has `dockerArgs == ""` (unchanged behaviour).
- [ ] When `spec.provision_script` is set, the GraphQL request body has:
  - `env[i].key == "KINOFORGE_PROVISION_SCRIPT"` with `value == base64(spec.provision_script)`.
  - `dockerArgs == 'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh && chmod +x /tmp/p.sh && bash /tmp/p.sh"'` (literal).
- [ ] When `spec.image` is set, it appears in `body.variables.input.imageName` (already covered but assert lockdown).
- [ ] Existing env vars (`RUNPOD_TERMINATE_KEY`, `KINOFORGE_SELFTERM_SCRIPT`) still present.
- [ ] `RUNPOD_API_KEY` still stripped (cred safety preserved).
- [ ] Fixture-audit lockdown (bug-fix #1) continues to pass — base64 of a script containing `$HF_TOKEN` literal yields ASCII that does NOT match the cred-leak patterns (rpa_, hf_, etc.). NEW unit test verifies this.

**Verify:** `pixi run pytest tests/providers/test_runpod_provision_script.py tests/providers/test_runpod.py tests/providers/test_fixtures_audit.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/providers/test_runpod_provision_script.py`:

```python
"""Tests for RunPodProvider._create_pod provision-script encoding."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


def _capture_post() -> tuple[
    list[tuple[str, dict[str, Any]]],
    "Callable[[str, dict[str, Any]], dict[str, Any]]",
]:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append((url, body))
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _offer() -> Offer:
    return Offer(
        id="NVIDIA RTX 4090",
        gpu_type="NVIDIA RTX 4090",
        vram_gb=24,
        cuda="12.8",
        cost_rate_usd_per_hr=0.30,
    )


def test_create_pod_without_provision_script_emits_empty_docker_args() -> None:
    """When spec.provision_script is None, dockerArgs stays empty."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(image="runpod/pytorch:latest", offer=_offer())
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["dockerArgs"] == ""


def test_create_pod_with_provision_script_base64_encodes_into_env_var() -> None:
    """spec.provision_script flows into KINOFORGE_PROVISION_SCRIPT as base64."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    script = "set -euo pipefail\ncd /workspace\necho ok"
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script=script,
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    env_list = body["variables"]["input"]["env"]
    env_map = {item["key"]: item["value"] for item in env_list}
    assert "KINOFORGE_PROVISION_SCRIPT" in env_map
    decoded = base64.b64decode(env_map["KINOFORGE_PROVISION_SCRIPT"]).decode("utf-8")
    assert decoded == script


def test_create_pod_with_provision_script_assembles_docker_args() -> None:
    """dockerArgs is the exact bash one-liner that decodes + runs the script."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script="echo hi",
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["dockerArgs"] == (
        'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
        '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
    )


def test_create_pod_image_name_preserved() -> None:
    """spec.image flows to imageName regardless of provision_script."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="custom/image:v1",
        offer=_offer(),
        provision_script="echo",
        run_cmd=["echo"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["imageName"] == "custom/image:v1"


def test_create_pod_strips_runpod_api_key_from_env() -> None:
    """Cred-safety: RUNPOD_API_KEY never enters env even when caller sets it."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        env={"RUNPOD_API_KEY": "should-not-leak", "HF_TOKEN": "hf_xxxxxxxxxxxxxx"},
        provision_script="echo",
        run_cmd=["echo"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    env_list = body["variables"]["input"]["env"]
    env_map = {item["key"]: item["value"] for item in env_list}
    assert "RUNPOD_API_KEY" not in env_map
    assert env_map["HF_TOKEN"] == "hf_xxxxxxxxxxxxxx"


def test_create_pod_base64_envelope_does_not_match_credential_leak_patterns() -> None:
    """Base64-encoded script containing $HF_TOKEN literal does NOT look like a cred."""
    from tests.providers.conftest_runpod import _audit_for_leaks  # type: ignore[import-not-found]

    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    script = (
        "set -euo pipefail\n"
        'curl -L -H "Authorization: Bearer $HF_TOKEN" '
        '"https://hf.co/file" -o w.safetensors\n'
        "exec python main.py"
    )
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script=script,
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    hits = _audit_for_leaks(body)
    assert hits == [], f"leak detected in encoded script: {hits!r}"
```

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/providers/test_runpod_provision_script.py -v`
Expected: FAIL — `dockerArgs` is empty and `KINOFORGE_PROVISION_SCRIPT` env var is missing.

- [ ] **Step 3: Extend `_create_pod` in `providers/runpod/__init__.py`**

Edit the `_create_pod` method around line 450. After the existing env-dict construction (after `env.pop("RUNPOD_API_KEY", None)` at line 477), insert:

```python
# NEW — Layer Q: provision_script + run_cmd injection
if spec.provision_script is not None:
    encoded = base64.b64encode(spec.provision_script.encode("utf-8")).decode("ascii")
    env["KINOFORGE_PROVISION_SCRIPT"] = encoded
    docker_args = (
        'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
        '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
    )
else:
    docker_args = ""
```

Replace the existing `"dockerArgs": "",` line in the body dict with `"dockerArgs": docker_args,`.

Add `import base64` at the top of the file.

- [ ] **Step 4: Run GREEN**

Run: `pixi run pytest tests/providers/test_runpod_provision_script.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Existing RunPod tests still pass**

Run: `pixi run pytest tests/providers/test_runpod.py tests/providers/test_fixtures_audit.py -v`
Expected: PASS (no regressions; `dockerArgs == ""` preserved when spec doesn't set provision_script).

- [ ] **Step 6: Full gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod_provision_script.py`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod_provision_script.py
git commit -m "$(cat <<'EOF'
feat(providers/runpod): Layer Q — provision_script base64 env-var injection

- _create_pod reads spec.provision_script + spec.run_cmd
- Script base64-encoded into KINOFORGE_PROVISION_SCRIPT env var (avoids
  dockerArgs length limit + shell-escape hell)
- dockerArgs assembled as: bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh && ... && bash /tmp/p.sh"
- When provision_script is None: dockerArgs stays "" (unchanged behaviour)
- RUNPOD_API_KEY still stripped from env (cred safety preserved)
- Fixture-audit regression confirmed: base64-encoded $HF_TOKEN literal does
  NOT match any cred-leak pattern from bug-fix #1

6 net new tests; existing runpod + fixtures-audit suites unchanged.
EOF
)"
```

---

## Task 6: SkyPilotProvider `create_instance` mapping + LocalProvider regression

**Goal:** SkyPilot `Task.setup` ← `spec.provision_script`; `Task.run` ← shell-quoted `spec.run_cmd`. LocalProvider regression-locks that the new spec fields are silently ignored.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py`
- Create: `tests/providers/test_skypilot_provision_script.py`
- Create: `tests/providers/test_local_ignores_provision_script.py`

**Acceptance Criteria:**
- [ ] When `spec.provision_script` is set, `sky_client.launch` is called with a `task_config` containing `"setup": <spec.provision_script>`.
- [ ] When `spec.run_cmd` is set, `task_config["run"]` is the shell-quoted joined cmd: `" ".join(shlex.quote(c) for c in spec.run_cmd)`.
- [ ] When both are `None`, neither key appears in `task_config` (no regression for existing SkyPilot callers).
- [ ] `LocalProvider.create_instance` ignores `spec.provision_script` and `spec.run_cmd` — no subprocess invoked, no error raised, status returned unchanged.

**Verify:** `pixi run pytest tests/providers/test_skypilot_provision_script.py tests/providers/test_skypilot.py tests/providers/test_local.py tests/providers/test_local_ignores_provision_script.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing tests for SkyPilot**

Create `tests/providers/test_skypilot_provision_script.py`:

```python
"""Tests for SkyPilotProvider create_instance setup/run mapping."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec, Offer
from kinoforge.providers.skypilot import SkyPilotProvider


class _FakeSky:
    """Minimal sky-client stub that records launches."""

    def __init__(self) -> None:
        self.launches: list[tuple[dict[str, Any], float]] = []

    def launch(self, task_config: dict[str, Any], *, autostop: float) -> dict[str, Any]:
        self.launches.append((task_config, autostop))
        return {"cluster_name": "fake-cluster"}

    def status(self) -> list[dict[str, Any]]:
        return []

    def down(self, name: str) -> None: ...

    def gpu_list(self) -> list[dict[str, Any]]:
        return []


def test_create_instance_without_provision_script_omits_setup_run() -> None:
    """Default callers (pre-Layer-Q) keep working: no setup/run keys in task_config."""
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(image="img:latest")
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert "setup" not in task_config
    assert "run" not in task_config


def test_create_instance_with_provision_script_maps_to_setup() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        provision_script="set -e\necho hi\n",
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["setup"] == "set -e\necho hi\n"


def test_create_instance_with_run_cmd_maps_shell_quoted_to_run() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        run_cmd=["python", "main.py", "--listen", "0.0.0.0"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    assert task_config["run"] == "python main.py --listen 0.0.0.0"


def test_create_instance_with_args_containing_spaces_shell_quotes_them() -> None:
    sky = _FakeSky()
    p = SkyPilotProvider(sky_client=sky)
    spec = InstanceSpec(
        image="img:latest",
        run_cmd=["python", "-c", "print('hello world')"],
    )
    p.create_instance(spec)
    task_config = sky.launches[0][0]
    # shlex.quote wraps the arg with single quotes when it contains shell meta-chars
    assert task_config["run"] == "python -c 'print('\"'\"'hello world'\"'\"')'"
```

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/providers/test_skypilot_provision_script.py -v`
Expected: FAIL — `setup` / `run` keys not present.

- [ ] **Step 3: Extend SkyPilot `create_instance`**

Edit `src/kinoforge/providers/skypilot/__init__.py` around line 249:

```python
import shlex

def create_instance(self, spec: InstanceSpec) -> Instance:
    """..."""
    sky = self._sky()
    autostop_minutes: float = spec.lifecycle.idle_timeout_s / 60.0
    task_config: dict[str, Any] = {
        "image": spec.image,
        "run_id": spec.run_id,
        "env": dict(spec.env),
        "tags": dict(spec.tags),
    }
    # NEW — Layer Q
    if spec.provision_script is not None:
        task_config["setup"] = spec.provision_script
    if spec.run_cmd is not None:
        task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)
    result: dict[str, Any] = sky.launch(task_config, autostop=autostop_minutes)
    # ... rest unchanged ...
```

- [ ] **Step 4: Run GREEN**

Run: `pixi run pytest tests/providers/test_skypilot_provision_script.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Write LocalProvider regression test**

Create `tests/providers/test_local_ignores_provision_script.py`:

```python
"""Regression: LocalProvider silently ignores spec.provision_script + run_cmd."""

from __future__ import annotations

from kinoforge.core.interfaces import InstanceSpec
from kinoforge.providers.local import LocalProvider


def test_local_provider_ignores_provision_script_and_run_cmd() -> None:
    """create_instance must accept the new spec fields without error or behaviour change."""
    p = LocalProvider()
    spec = InstanceSpec(
        image="ignored",
        provision_script="set -e\necho should-not-run",
        run_cmd=["never", "executed"],
    )
    instance = p.create_instance(spec)
    assert instance.provider == "local"
    assert instance.status == "ready"
```

- [ ] **Step 6: Run + verify (likely already GREEN)**

Run: `pixi run pytest tests/providers/test_local_ignores_provision_script.py -v`
Expected: PASS without code changes — LocalProvider never reads the new fields.

- [ ] **Step 7: Full sweep + gate**

Run: `pixi run pytest tests/providers/ -q && pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/providers/skypilot/__init__.py tests/providers/test_skypilot_provision_script.py tests/providers/test_local_ignores_provision_script.py`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py \
  tests/providers/test_skypilot_provision_script.py \
  tests/providers/test_local_ignores_provision_script.py
git commit -m "$(cat <<'EOF'
feat(providers): Layer Q — SkyPilot setup/run mapping + LocalProvider regression

- SkyPilotProvider.create_instance maps spec.provision_script -> Task.setup,
  spec.run_cmd -> Task.run (shlex.quote-joined)
- When spec fields are None, neither key appears (pre-Layer-Q caller parity)
- LocalProvider regression locks that the new fields are silently ignored

5 net new tests.
EOF
)"
```

---

## Task 7: Orchestrator wiring — `_provision_instance_and_build_backend` extension

**Goal:** Tie everything together. Orchestrator calls `engine.render_provision(cfg)`, validates `env_required`, builds the spec with the rendered payload, calls `provider.create_instance`, wires `provider.get_instance` onto the engine, then calls `engine.provision(instance, cfg)` (which dispatches to `wait_for_ready` for remote). Teardown guard extended for `ProvisionFailed` / `ProvisionTimeout`. Caller-supplied-instance contract (Layer P Task 7 item #2) preserved.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:273` (`_provision_instance_and_build_backend`)
- Create: `tests/core/test_orchestrator_render_provision.py`

**Acceptance Criteria:**
- [ ] Orchestrator calls `engine.render_provision(cfg_dict)` exactly once per provision (cache-miss branch + cache-hit branch where applicable).
- [ ] For each name in `rendered.env_required`, orchestrator calls `creds.get(name)`; raises `AuthError(f"missing required env var: {name}")` if any returns `None`; does NOT call `provider.create_instance` in that case.
- [ ] `_build_spec` is extended to populate `image`, `ports`, `env`, `provision_script`, `run_cmd` from `rendered`. The existing `kinoforge_engine` / `kinoforge_key` tags + caller-supplied `tags=` merge behaviour is preserved.
- [ ] After `create_instance` returns, orchestrator passes `provider.get_instance` to the engine via a private attribute (`engine._get_instance = provider.get_instance`) BEFORE calling `engine.provision`. NOTE: this is the resolution of the spec's "Open knob" — we pick the in-place-mutation path because it keeps the engine ABC stable and avoids a `with_get_instance` builder.
- [ ] `engine.provision(instance, cfg_dict)` is called once.
- [ ] When `provision` raises `ProvisionFailed`, `ProvisionTimeout`, `CapabilityMismatch`, or `ValidationError`:
  - The orchestrator-created instance IS destroyed (`provider.destroy_instance`).
  - The exception propagates unchanged.
- [ ] Caller-supplied-instance contract preserved: when `instance is not None` arg is passed (item #2 path), orchestrator skips `render_provision` + `create_instance` and calls `engine.provision(instance, cfg)` directly (which handles the no-op poll for warm pods). Teardown is also skipped on failure.

**Verify:** `pixi run pytest tests/core/test_orchestrator_render_provision.py tests/core/test_orchestrator.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing wiring tests**

Create `tests/core/test_orchestrator_render_provision.py`:

```python
"""Tests for orchestrator _provision_instance_and_build_backend Layer Q wiring."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import (
    AuthError,
    CapabilityMismatch,
    ProvisionFailed,
    ProvisionTimeout,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Instance,
    InstanceSpec,
    Offer,
    RenderedProvision,
)
from kinoforge.core.orchestrator import _provision_instance_and_build_backend


@pytest.fixture
def fake_engine() -> MagicMock:
    engine = MagicMock()
    engine.name = "fakeengine"
    engine.render_provision.return_value = RenderedProvision(
        script="echo hi",
        run_cmd=["python", "-m", "x"],
        image="fake:latest",
        ports=["8000"],
        env_required=["HF_TOKEN"],
    )
    return engine


@pytest.fixture
def fake_provider() -> MagicMock:
    provider = MagicMock()
    provider.name = "fakeprovider"
    provider.find_offers.return_value = [
        Offer(
            id="X1", gpu_type="X1", vram_gb=24, cuda="12.8",
            cost_rate_usd_per_hr=0.30,
        )
    ]
    provider.create_instance.return_value = Instance(
        id="inst-1", provider="fakeprovider", status="ready", created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    provider.get_instance.return_value = Instance(
        id="inst-1", provider="fakeprovider", status="ready", created_at=0.0,
        endpoints={"8000": "https://inst-1-8000"},
    )
    return provider


def test_orchestrator_calls_render_provision_once(fake_engine, fake_provider):
    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import CapabilityKey

    # ... build minimal cfg, key, creds ...
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="should-be-overridden")

    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    fake_engine.render_provision.assert_called_once()


def test_orchestrator_validates_env_required_via_creds(fake_engine, fake_provider):
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    creds.get.assert_any_call("HF_TOKEN")


def test_orchestrator_raises_auth_error_when_env_required_missing(fake_engine, fake_provider):
    creds = MagicMock()
    creds.get = MagicMock(return_value=None)
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(AuthError, match="HF_TOKEN"):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=Path("/tmp"),
            for_discovery=False,
        )
    fake_provider.create_instance.assert_not_called()


def test_orchestrator_spec_carries_rendered_provision_payload(fake_engine, fake_provider):
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
    spec_arg: InstanceSpec = fake_provider.create_instance.call_args[0][0]
    assert spec_arg.image == "fake:latest"
    assert spec_arg.provision_script == "echo hi"
    assert spec_arg.run_cmd == ["python", "-m", "x"]
    assert spec_arg.env.get("HF_TOKEN") == "hf_REAL"


def test_orchestrator_destroys_on_provision_failed(fake_engine, fake_provider):
    fake_engine.provision.side_effect = ProvisionFailed("boot crashed")
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(ProvisionFailed):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=Path("/tmp"),
            for_discovery=False,
        )
    fake_provider.destroy_instance.assert_called_once_with("inst-1")


def test_orchestrator_destroys_on_provision_timeout(fake_engine, fake_provider):
    fake_engine.provision.side_effect = ProvisionTimeout("ran out")
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(ProvisionTimeout):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=cfg,
            run_id="run-1",
            key=key,
            creds=creds,
            store=store,
            state_dir=Path("/tmp"),
            for_discovery=False,
        )
    fake_provider.destroy_instance.assert_called_once_with("inst-1")


def test_orchestrator_wires_get_instance_onto_engine_before_provision(fake_engine, fake_provider):
    """engine._get_instance is set to provider.get_instance before engine.provision runs."""
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    cfg = MagicMock()
    cfg.lifecycle.return_value = MagicMock(idle_timeout_s=3600.0, boot_timeout_s=900.0)
    cfg.hardware_requirements.return_value = MagicMock()
    cfg.compute = MagicMock(image="x")
    store = MagicMock()
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    def _provision_check(instance, cfg_dict):
        assert fake_engine._get_instance is fake_provider.get_instance

    fake_engine.provision.side_effect = _provision_check

    _provision_instance_and_build_backend(
        resolved_engine=fake_engine,
        resolved_provider=fake_provider,
        cfg=cfg,
        run_id="run-1",
        key=key,
        creds=creds,
        store=store,
        state_dir=Path("/tmp"),
        for_discovery=False,
    )
```

NOTE: These tests use heavy `MagicMock`. The orchestrator currently constructs cfg dicts via `_cfg_dict(cfg)`; the implementation will need to make sure `cfg.lifecycle().boot_timeout_s` lives somewhere the engine can read it from `cfg_dict["lifecycle"]["boot_timeout_s"]`.

- [ ] **Step 2: Run RED**

Run: `pixi run pytest tests/core/test_orchestrator_render_provision.py -v`
Expected: FAIL — wiring not present; `render_provision` not called; spec doesn't carry the rendered payload; etc.

- [ ] **Step 3: Extend `_provision_instance_and_build_backend`**

Edit `src/kinoforge/core/orchestrator.py:273`. Inside the function body, before the `_build_spec` definition (currently at line 329), add the render + cred validation:

```python
def _provision_instance_and_build_backend(
    *,
    resolved_engine: GenerationEngine,
    resolved_provider: ComputeProvider,
    cfg: Config,
    run_id: str,
    key: CapabilityKey,
    creds: CredentialProvider | None,
    store: ArtifactStore,
    state_dir: Path,
    for_discovery: bool,
    tags: dict[str, str] | None = None,
) -> tuple[Instance, GenerationBackend]:
    """..."""
    hw_reqs = cfg.hardware_requirements()
    offers = resolved_provider.find_offers(hw_reqs)
    if not offers:
        prefix = "for discovery " if for_discovery else ""
        raise CapacityError(
            f"no offers available {prefix}from provider "
            f"{getattr(resolved_provider, 'name', repr(resolved_provider))!r}"
        ) from None
    lifecycle = cfg.lifecycle()
    image = cfg.compute.image if cfg.compute is not None else ""
    key_hash = _key_hash(key)
    cfg_dict = _cfg_dict(cfg)

    # NEW — Layer Q: render provision payload + validate creds
    rendered = resolved_engine.render_provision(cfg_dict)
    rendered_env: dict[str, str] = {}
    for var in rendered.env_required:
        value = creds.get(var) if creds is not None else None
        if value is None:
            raise AuthError(f"missing required env var: {var}")
        rendered_env[var] = value

    def _build_spec(offer: Offer) -> InstanceSpec:
        merged_tags: dict[str, str] = {
            "kinoforge_engine": resolved_engine.name,
            "kinoforge_key": key_hash,
        }
        if tags:
            merged_tags.update(tags)
        return InstanceSpec(
            image=rendered.image or image,
            offer=offer,
            ports=tuple(rendered.ports),
            lifecycle=lifecycle,
            tags=merged_tags,
            env=dict(rendered_env),
            run_id=run_id,
            provision_script=rendered.script,
            run_cmd=rendered.run_cmd,
        )

    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )
    while instance.status != "ready":
        instance = resolved_provider.get_instance(instance.id)

    # NEW — Layer Q: wire provider.get_instance onto engine before remote provision
    resolved_engine._get_instance = resolved_provider.get_instance  # type: ignore[attr-defined]

    try:
        _provision_compute_once(
            engine=resolved_engine,
            cfg=cfg,
            instance=instance,
            creds=creds,
            store=store,
            state_dir=state_dir,
            capability_key_hex=key.derive(),
        )
    except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
        resolved_provider.destroy_instance(instance.id)
        raise

    backend = resolved_engine.backend(instance, cfg_dict)
    return instance, backend
```

Add imports:

```python
from kinoforge.core.errors import (
    AuthError,
    CapabilityMismatch,
    CapacityError,
    ProvisionFailed,
    ProvisionTimeout,
    ValidationError,
)
from kinoforge.core.interfaces import RenderedProvision
```

NOTE: `_provision_compute_once` is the existing wrapper around `engine.provision`. If `ProvisionFailed` / `ProvisionTimeout` are raised by `engine.provision`, the try/except above catches them.

Also confirm: `_cfg_dict(cfg)` includes `lifecycle.boot_timeout_s` so the engine reads it correctly. If not, extend it in the same task (and add a regression test).

- [ ] **Step 4: Run GREEN**

Run: `pixi run pytest tests/core/test_orchestrator_render_provision.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Full orchestrator regression**

Run: `pixi run pytest tests/core/test_orchestrator.py tests/core/test_orchestrator_render_provision.py -v`
Expected: all PASS.

- [ ] **Step 6: Full suite gate (catches cross-test fallout)**

Run: `pixi run pytest -q`
Expected: same pass count as before + new Layer Q tests. The 4 pre-existing RED scaffold tests in `tests/examples/test_runpod_comfyui_wan_graph.py` remain RED (intentional per PROGRESS:191).

- [ ] **Step 7: Type + lint gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_render_provision.py`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_render_provision.py
git commit -m "$(cat <<'EOF'
feat(core/orchestrator): Layer Q — render_provision + cred validate + spec.replace + teardown guard

- _provision_instance_and_build_backend calls engine.render_provision(cfg)
- env_required validated against creds; AuthError before create_instance if any missing
- _build_spec populates image/ports/env/provision_script/run_cmd from rendered
- provider.get_instance assigned to engine._get_instance before engine.provision
- ProvisionFailed/ProvisionTimeout/CapabilityMismatch/ValidationError → destroy
  orchestrator-created instance + re-raise

7 net new tests; existing orchestrator suite unchanged.
EOF
)"
```

---

## Task 8: README + PROGRESS + final gate + merge

**Goal:** Document the layer, close PROGRESS, run full project gate, prepare branch for merge. NOTE: Layer Q itself does not include a live-smoke test (that's AC #15 in the spec which validates during Layer P Task 7 item #3 resumption, not Layer Q closure).

**Files:**
- Modify: `README.md` (add "Remote provisioning" subsection)
- Modify: `PROGRESS.md` (add Layer Q closure block under Layer P branch state)

**Acceptance Criteria:**
- [ ] `README.md` has a new "Remote provisioning" subsection explaining the `engine.render_provision` surface, the no-SSH design, and the bootstrap-script model.
- [ ] `PROGRESS.md` has a Layer Q closure block: date, sub-spec + sub-plan paths, T1–T8 commit SHA table, key design decisions, test count delta with actual figure.
- [ ] `pixi run pytest -q` → 888 + ~58 new offline tests = ~946 PASS + 4 RED scaffold (intentional).
- [ ] `pixi run pre-commit run --all-files` → clean.
- [ ] `pixi run typecheck` → clean.
- [ ] Branch `build/layer-p` ready for either continuation (Layer P Task 7 item #3 resumes) or `--no-ff` merge to `main` once item #3 ships its own sub-plan and the layer-q work is independently valuable.

**Verify:** `pixi run pytest -q && pixi run pre-commit run --all-files && pixi run typecheck` → all exit 0.

**Steps:**

- [ ] **Step 1: Add README section**

Edit `README.md`. Add a new subsection (place it near the "Compute providers" / "Engines" cluster, or after the existing "Multi-node coordination" section):

```markdown
### Remote provisioning

Engines that talk to a remote pod (ComfyUI, Diffusers on RunPod / SkyPilot)
bootstrap via `engine.render_provision(cfg)`. The engine emits a self-
contained bash script that clones its repo, installs dependencies, downloads
weights, and launches the inference HTTP server. The orchestrator validates
declared credential env vars, attaches the rendered payload to
`InstanceSpec`, and the provider injects it via its native boot-script
mechanism (RunPod base64-encoded env var + `dockerArgs`; SkyPilot
`Task.setup` / `Task.run`).

After the pod boots, `engine.provision(instance, cfg)` polls an engine-
specific ready endpoint (ComfyUI: `/system_stats`; Diffusers: `/health`)
until HTTP-200, the pod status flips terminal, or
`cfg.lifecycle.boot_timeout_s` (default 900s) elapses. Failures raise
`ProvisionFailed` (terminal status) or `ProvisionTimeout` (deadline).

No SSH required. Local users see zero behavioural change — engines branch
on `instance.provider == "local"` and run the existing local bootstrap.

Credentials referenced by the script (e.g. `$HF_TOKEN`) are lifted from
the configured `CredentialProvider` onto `spec.env` by the orchestrator.
The script string never carries plaintext token values.
```

- [ ] **Step 2: Add PROGRESS Layer Q closure block**

Edit `PROGRESS.md`. Add a section after the Layer P bug-fix #1 closure block (around the current line 246, before "Hard prerequisite for resuming any live capture" or in a new dedicated header):

```markdown
**Layer Q — cross-engine cross-provider remote provisioning — ✅ CLOSED 2026-06-01 at HEAD `<short-sha>`.**

Sub-spec + sub-plan + 8 task commits unblock Layer P Task 7 item #3 and
ship the canonical cross-engine cross-provider bootstrap surface.

- Sub-spec: `docs/superpowers/specs/2026-06-01-layer-q-remote-provisioning-design.md` (`edbe5a6`)
- Sub-plan: `docs/superpowers/plans/2026-06-01-layer-q-remote-provisioning.md` (+ `.tasks.json`) (`<plan-sha>`)
- T1 `<sha>` — foundations (RenderedProvision + spec fields + boot_timeout + errors)
- T2 `<sha>` — GenerationEngine ABC additions (render_provision + wait_for_ready)
- T3 `<sha>` — ComfyUI render_provision + wait_for_ready + provision branch
- T4 `<sha>` — Diffusers parity
- T5 `<sha>` — RunPod _create_pod base64 + dockerArgs encoding
- T6 `<sha>` — SkyPilot setup/run mapping + LocalProvider regression
- T7 `<sha>` — Orchestrator render → validate → spec.replace → create → wait_for_ready → teardown guard
- T8 (this commit) — README + PROGRESS + final gate

Test count 888 → <final> offline (+<delta> net new). typecheck/lint/pre-commit
all-files clean. The 4 pre-existing failures in
`tests/examples/test_runpod_comfyui_wan_graph.py` (intentional RED scaffold
from `9d2a9bf`, see PROGRESS:191) are NOT regressions — they transition GREEN
only when Layer P Task 7 item #3 resumes against Layer Q's HEAD.

**Key design decisions:**

- Approach B (engine renders + provider injects). No SSH dep; no paramiko.
- Full bootstrap — script owns engine clone + custom-node clone + weight download.
  Stock RunPod / SkyPilot images work without custom kinoforge images.
- Engine owns `wait_for_ready` because engine knows its own readiness criterion
  (ComfyUI: `/system_stats`; Diffusers: `/health`).
- Credentials referenced via `$VAR` in the rendered script; never substituted as
  literal values. Orchestrator validates `env_required` + lifts onto `spec.env`.
- `engine.provision()` branches on `instance is None or instance.provider == "local"`;
  local users see zero behavioural change.
- `provider.get_instance` is wired onto `engine._get_instance` by the orchestrator
  immediately before `engine.provision`; resolves the spec's "Open knob" via
  in-place attribute assignment rather than a builder method.

**Unblocks:** Layer P Task 7 item #3 (workflow API JSON + first green MP4) and
item #4 (live unknowns surfacing). The item #3 sub-plan re-opens against Layer Q's
HEAD; its blocker status updates accordingly.

**Out of scope (deferred follow-ups):**
- Ad-hoc remote shell (Approach A): `paramiko` / `sky exec` for arbitrary
  post-provision commands.
- kinoforge-published base images + `skip_engine_clone` toggle.
- Pod boot-log tailing for debugging.
```

- [ ] **Step 3: Stage docs**

```bash
git add README.md PROGRESS.md
```

- [ ] **Step 4: Run full project gate**

Run:
```bash
pixi run pytest -q && pixi run pre-commit run --all-files && pixi run typecheck
```
Expected: all exit 0. Capture the actual final pytest count for the commit message.

- [ ] **Step 5: Backfill PROGRESS placeholders**

Replace `<short-sha>`, `<plan-sha>`, per-task `<sha>` slots, `<final>`, and
`<delta>` in PROGRESS.md with the real values from `git log --oneline` and the
captured pytest count. Re-stage:

```bash
git add PROGRESS.md
```

- [ ] **Step 6: Commit closure**

```bash
git commit -m "$(cat <<'EOF'
docs(progress): Layer Q closure — cross-engine cross-provider remote provisioning

8 tasks shipped:
- T1 foundations (RenderedProvision + spec fields + boot_timeout + errors)
- T2 GenerationEngine ABC (render_provision + wait_for_ready)
- T3 ComfyUI render_provision + wait_for_ready + remote-branch provision
- T4 Diffusers parity
- T5 RunPod _create_pod base64 + dockerArgs encoding
- T6 SkyPilot setup/run + LocalProvider regression
- T7 Orchestrator wiring (render → validate → spec.replace → wait_for_ready)
- T8 README + PROGRESS

Test count 888 → <final> offline (+<delta> net new). Full gate clean.

Unblocks Layer P Task 7 item #3.

Out-of-scope follow-ups documented: ad-hoc remote shell, base-image toggle,
boot-log tailing.
EOF
)"
```

---

## Self-review

Spec coverage check (against `docs/superpowers/specs/2026-06-01-layer-q-remote-provisioning-design.md`):

| Spec AC | Plan Task |
|---|---|
| AC #1 `RenderedProvision` + spec fields exist | Task 1 |
| AC #2 `render_provision` + `wait_for_ready` ABC | Task 2 |
| AC #3 ComfyUI `render_provision` snapshots | Task 3 |
| AC #4 ComfyUI `wait_for_ready` polling/timeout | Task 3 |
| AC #5 Diffusers parity | Task 4 |
| AC #6 `provision` local-vs-remote branches | Tasks 3 + 4 |
| AC #7 RunPod `_create_pod` provision-script encoding | Task 5 |
| AC #8 SkyPilot setup/run mapping | Task 6 |
| AC #9 LocalProvider silent-ignore regression | Task 6 |
| AC #10 `LifecycleConfig.boot_timeout` round-trip | Task 1 |
| AC #11 Orchestrator wiring | Task 7 |
| AC #12 Orchestrator `AuthError` when env missing | Task 7 |
| AC #13 Orchestrator destroy-on-failure for created instances | Task 7 |
| AC #14 Fixture audit continues to pass | Task 5 (regression test) |
| AC #15 Layer P item #3 live smoke | NOT in Layer Q — validates during item #3 resumption |

**Coverage:** 14/15 ACs implemented inside Layer Q; AC #15 explicitly belongs to the item #3 resumption per the spec's "Non-goals" section. No gaps.

Placeholder scan: no "TBD", "fill in details", or vague AC. PROGRESS placeholders (`<short-sha>`, `<final>`, `<delta>`) are backfilled in Task 8 Step 5 with explicit instructions.

Type consistency:
- `RenderedProvision` field names match across spec, Task 1 dataclass, and all consumer tasks.
- `wait_for_ready` keyword args (`http_get`, `sleep`, `get_instance`, `timeout_s`) match across ABC default (Task 2), ComfyUI impl (Task 3), Diffusers impl (Task 4), and orchestrator wiring (Task 7).
- `instance.provider == "local"` branch criterion appears in spec D7, Task 3 Step 11, Task 4 Step 11, and matches the existing `LocalProvider.name = "local"`.
- `boot_timeout_s` is float-typed throughout: `Lifecycle.boot_timeout_s: float = 900.0` (Task 1) → `cfg["lifecycle"]["boot_timeout_s"]` (Task 3) → `wait_for_ready(timeout_s=float)` (Tasks 3-4) → assigned `boot_timeout_s=lc.boot_timeout` from `LifecycleConfig.boot_timeout: float` (Task 1).

User-gate scan: spec mentions "Layer P Task 7 item #3 live smoke runs on a real RunPod pod end-to-end" but the live smoke is NOT in Layer Q's scope (per spec Non-goals + spec D2 + this plan's Self-review). No user-gate tasks tagged in Layer Q.

---

## Execution

Plan complete and saved to `docs/superpowers/plans/2026-06-01-layer-q-remote-provisioning.md`.
