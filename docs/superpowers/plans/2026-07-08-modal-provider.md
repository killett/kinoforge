# Modal serverless-GPU provider — Implementation Plan (spec 1: provider + Milestone 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Modal as a kinoforge `ComputeProvider` that deploys the existing FastAPI generation server onto Modal's serverless GPUs, and prove it live with a Wan 2.1 T2V-1.3B generation.

**Architecture:** Option A — one generic, config-driven Modal App. `create_instance` builds a `modal.App` programmatically (image from the config's registry tag, GPU from the resolved offer, a Volume for the HF weight cache), whose `@modal.web_server(8000)` function runs the SAME `provision_script; exec run_cmd` that RunPod runs — reusing `engine.render_provision` and the FastAPI server verbatim. The public `.modal.run` URL is returned as `endpoints={"8000": url}`, so all downstream HTTP code is unchanged. All Modal/subprocess touchpoints sit behind injected callables so the whole provider is unit-tested offline with fakes; real Modal is exercised only in the two live-gate tasks.

**Tech Stack:** Python 3.11, `modal` SDK (PyPI, in a new `live-modal` pixi feature env), Pydantic config, pytest. Mirrors `src/kinoforge/providers/skypilot/__init__.py`.

**User decisions (already made):**
- Delivery mechanism: "A — Generic config-driven app" (reuse `render_provision` + the FastAPI server; rejected direct-function-calls and static-per-model-modules as divergent code paths).
- Scope: "Provider + Milestone 1 only" (full `ModalProvider` + Wan 2.1 T2V-1.3B live proof; Milestones 2–4 are follow-up specs).

---

## Design facts locked by research (do not re-derive)

**kinoforge patterns (verbatim-extracted):**
- Registry: `registry.register_provider("modal", _default_factory)` at module bottom (overwrites; no decorator). `get_provider(name)()` in `build_provider_for` resolves any registered name — **no `build_provider_for` edit needed** (Modal omits `cloud:`, so the skypilot `_clouds` pin branch is not entered). The ONLY wiring edit is adding `import kinoforge.providers.modal` to `src/kinoforge/_adapters.py`.
- `provider: modal` is already schema-valid (`ComputeConfig.provider: str`). **There is no `compute.gpu` field** — GPU selection is `compute.requirements.gpu_preference: list[str]`.
- `compute.cloud` for a non-sky cloud FAILS `SkyPilotCloudPinSupportedCheck` — **Modal configs must omit `cloud:`**.
- `last_heartbeat(instance_id) -> None` is mandatory (off-ABC; absence `AttributeError`s every `HeartbeatLoop._tick_once`).
- Bounded destroy poll: `_DESTROY_POLL_MAX_ITERS = 40` (× 3 s ≈ 120 s). Never `while True`.
- `filter_offers` applies the `max_usd_per_hr` cap ONLY when `Offer.mode == "pod"`. Modal offers use `mode="serverless"` to bypass the cap (Modal is priced serverless).

Dataclass fields (from `src/kinoforge/core/interfaces.py`):
- `Offer(id, gpu_type, vram_gb, cuda, cost_rate_usd_per_hr, mode="pod")` (frozen).
- `Instance(id, provider, status, created_at, endpoints={}, tags={}, cost_rate_usd_per_hr=0.0)`.
- `InstanceSpec(image, offer=None, ports=(), volume_gb=0, volume_mount="", lifecycle, env={}, tags={}, run_id="", provision_script=None, run_cmd=None, spot=False, diagnostic_env={}, restart_policy="always", cloud_type="any")`.
- `RenderedProvision(script, run_cmd, image, ports, env_required)` (frozen).
- `HardwareRequirements(min_vram_gb=48, min_cuda="12.8", max_usd_per_hr=2.20, gpu_preference=(), disk_gb=100)`.

**Modal SDK facts (from docs, 2026-07-08 — corrections flagged ⚠️):**
- Programmatic deploy: `app = modal.App(name=f"kinoforge-{run_id}", image=img)`; `with modal.enable_output(): app.deploy()`. Persistent named deploy → **stable `.modal.run` URL that survives process exit** (ephemeral `app.run()` does NOT).
- ⚠️ Locally/dynamically-built functions must be decorated `@app.function(serialized=True)` so Modal cloudpickles the function instead of importing it by module reference. **Use `serialized=True`** for our runtime-built app.
- `@modal.web_server(port, *, startup_timeout=5.0, label=None)` — decorated fn runs once at container start and must spawn a process binding `0.0.0.0:<port>` (non-blocking `subprocess.Popen`; Modal polls the port until `startup_timeout`). ⚠️ Default `startup_timeout` is 5 s — MUST override (weights cold start).
- URL: `server_fn.get_web_url()` (⚠️ `.web_url` is legacy). Format `https://<workspace>--<app>-<function>.modal.run`.
- `modal.Image.from_registry(tag, add_python="3.11")` then chain `.env({...})` / `.pip_install(...)`.
- `modal.Volume.from_name(name, create_if_missing=True)`; mount `@app.function(volumes={mount: vol})`. Persists across deployments.
- ⚠️ GPU strings: `"T4"`, `"L4"`, `"A10"` (NOT `"A10G"`), `"L40S"`, `"A100-40GB"`, `"A100-80GB"`, `"H100"`. Pin exact strings (bare `"A100"` may auto-upgrade).
- ⚠️ Scale-to-zero: `scaledown_window=<seconds>` on `@app.function` (renamed from `container_idle_timeout`). `min_containers=0` default.
- ⚠️ No documented pure-Python stop/list. Teardown/inventory shell out (bounded via subprocess `timeout=`): `modal app stop <name> --yes` and `modal app list --json`, both inheriting `MODAL_TOKEN_ID/SECRET` from `env=os.environ`.
- Per-run config reaches the remote container via a `modal.Secret.from_dict({...})` passed to `@app.function(secrets=[...])` — no image rebuild.
- Auth: SDK + CLI read `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` from env automatically (present in `.env`, verified).
- Pricing snapshot ($/hr): T4 0.59, L4 0.80, A10 1.10, L40S 1.95, A100-40GB 2.10, A100-80GB 2.50, H100 3.95.

---

## File structure

- `src/kinoforge/providers/modal/__init__.py` — `ModalProvider` (ABC impl, injected seams, self-registration).
- `src/kinoforge/providers/modal/_catalog.py` — static Modal GPU offer catalog + `modal_offers(reqs)`.
- `src/kinoforge/providers/modal/_app.py` — `ModalAppRequest` dataclass + `build_modal_app(req, modal_mod)` (the reuse hinge) + default deploy/stop/list callables.
- `src/kinoforge/_adapters.py` — add one import line.
- `pixi.toml` — `[feature.live-modal.*]` + `[environments] live-modal`.
- `examples/configs/modal-wan-t2v-1_3b.yaml` — Milestone 1 config.
- Tests: `tests/providers/modal/test_catalog.py`, `test_app.py`, `test_provider.py`, `tests/test_modal_config.py`, `tests/live/test_modal_transport_smoke.py`, `tests/live/test_modal_wan_t2v_1_3b.py`.

---

### Task 0: pixi `live-modal` feature env + `modal` dependency

**Goal:** A `live-modal` pixi env exists with the `modal` SDK importable, mirroring `live-skypilot`.

**Files:**
- Modify: `pixi.toml` (add `[feature.live-modal.pypi-dependencies]` + `[environments]` entry, near lines 236–247)

**Acceptance Criteria:**
- [ ] `pixi run -e live-modal python -c "import modal; print(modal.__version__)"` prints a version.
- [ ] `pixi.lock` regenerated and staged alongside `pixi.toml`.
- [ ] `default` env is unchanged (modal NOT added there).

**Verify:** `pixi run -e live-modal python -c "import modal; print(modal.__version__)"` → prints e.g. `1.x.y`

**Steps:**

- [ ] **Step 1: Add the feature + environment to `pixi.toml`.** After the `[feature.live-hosted.pypi-dependencies]` block, add:

```toml
[feature.live-modal.pypi-dependencies]
# Modal serverless-GPU SDK (not on conda-forge). Auth via MODAL_TOKEN_ID/SECRET
# from .env (activation not required — SDK reads env vars directly).
modal = ">=1.0"
```

And under `[environments]` (which currently has `live-skypilot` / `live-hosted`), add:

```toml
live-modal = { features = ["live-modal"] }
```

- [ ] **Step 2: Resolve + install.**

Run: `pixi install -e live-modal`
Expected: solves and writes `pixi.lock` (may take a minute).

- [ ] **Step 3: Verify import.**

Run: `pixi run -e live-modal python -c "import modal; print(modal.__version__)"`
Expected: a version string, no ImportError.

- [ ] **Step 4: Commit (stage BOTH pixi.toml and pixi.lock — see memory `feedback_pre_commit_stages_pixi_lock`).**

```bash
git add pixi.toml pixi.lock
git commit -m "chore(pixi): add live-modal feature env with modal SDK"
```

---

### Task 1: Modal GPU offer catalog + `modal_offers`

**Goal:** A pure function returns the static Modal GPU catalog as `Offer`s filtered by `HardwareRequirements`, with `mode="serverless"`.

**Files:**
- Create: `src/kinoforge/providers/modal/__init__.py` (empty package marker for now; provider added Task 2)
- Create: `src/kinoforge/providers/modal/_catalog.py`
- Create: `tests/providers/modal/__init__.py`
- Create: `tests/providers/modal/test_catalog.py`

**Acceptance Criteria:**
- [ ] Catalog contains T4, L4, A10, L40S, A100-40GB, A100-80GB, H100 with correct VRAM + `$/hr` + Modal GPU string as `id`/`gpu_type`.
- [ ] `modal_offers(reqs)` filters by `min_vram_gb` and orders by `gpu_preference` (delegates to `filter_offers`).
- [ ] All offers carry `mode="serverless"` (so `filter_offers` does not apply the `$/hr` cost cap).

**Verify:** `pixi run pytest tests/providers/modal/test_catalog.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/providers/modal/test_catalog.py
"""Behavior: the static Modal GPU catalog and requirement-filtering."""
from kinoforge.core.interfaces import HardwareRequirements
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG, modal_offers


def test_catalog_has_expected_gpu_strings_and_vram():
    # Bug caught: using the AWS-ism "A10G" (Modal rejects it) or wrong VRAM.
    by_id = {o.id: o for o in MODAL_GPU_CATALOG}
    assert "A10" in by_id and "A10G" not in by_id
    assert by_id["A10"].vram_gb == 24
    assert by_id["A100-80GB"].vram_gb == 80
    assert {"T4", "L4", "A10", "L40S", "A100-40GB", "A100-80GB", "H100"} <= set(by_id)


def test_all_offers_are_serverless_mode():
    # Bug caught: mode="pod" would make filter_offers apply the $/hr cap and
    # silently drop pricier GPUs Modal can actually serve.
    assert all(o.mode == "serverless" for o in MODAL_GPU_CATALOG)


def test_modal_offers_filters_by_vram_and_orders_by_preference():
    reqs = HardwareRequirements(
        min_vram_gb=40, gpu_preference=("A100-80GB", "A100-40GB")
    )
    offers = modal_offers(reqs)
    ids = [o.id for o in offers]
    assert "T4" not in ids and "A10" not in ids  # 16/24GB dropped by min_vram
    assert ids[0] == "A100-80GB"  # preference ordering wins
```

- [ ] **Step 2: Run — confirm it fails.**

Run: `pixi run pytest tests/providers/modal/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.providers.modal._catalog`

- [ ] **Step 3: Implement.**

```python
# src/kinoforge/providers/modal/_catalog.py
"""Static Modal GPU offer catalog.

Modal is serverless — there is no live "offers" API. Pricing is a fixed table
(snapshot 2026-07-08, https://modal.com/pricing). Offers are ``mode="serverless"``
so :func:`filter_offers` does not apply the pod ``max_usd_per_hr`` cap.
"""
from __future__ import annotations

from kinoforge.core.interfaces import HardwareRequirements, Offer
from kinoforge.core.offers import filter_offers

#: Modal GPU catalog: (Modal gpu-string, VRAM GB, $/hr snapshot).
_MODAL_GPUS: tuple[tuple[str, int, float], ...] = (
    ("T4", 16, 0.59),
    ("L4", 24, 0.80),
    ("A10", 24, 1.10),
    ("L40S", 48, 1.95),
    ("A100-40GB", 40, 2.10),
    ("A100-80GB", 80, 2.50),
    ("H100", 80, 3.95),
)

#: All Modal containers ship CUDA 12.x drivers; report a conservative baseline.
_MODAL_CUDA = "12.4"

MODAL_GPU_CATALOG: tuple[Offer, ...] = tuple(
    Offer(
        id=name,
        gpu_type=name,
        vram_gb=vram,
        cuda=_MODAL_CUDA,
        cost_rate_usd_per_hr=cost,
        mode="serverless",
    )
    for name, vram, cost in _MODAL_GPUS
)


def modal_offers(reqs: HardwareRequirements) -> list[Offer]:
    """Return catalog offers filtered/ordered per ``reqs``.

    Args:
        reqs: Hardware requirements from the resolved config.

    Returns:
        Offers meeting ``min_vram_gb``/``min_cuda``, ordered by ``gpu_preference``.
    """
    return filter_offers(list(MODAL_GPU_CATALOG), reqs)
```

Also create empty `src/kinoforge/providers/modal/__init__.py` and `tests/providers/modal/__init__.py`.

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/providers/modal/test_catalog.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/providers/modal/ tests/providers/modal/
git commit -m "feat(modal): static GPU offer catalog + modal_offers filter"
```

---

### Task 2: `ModalProvider` skeleton — registry, seams, heartbeat, find_offers

**Goal:** `ModalProvider` registers as `"modal"`, exposes injected seams, and implements `find_offers`, `heartbeat` (no-op), and `last_heartbeat` (→ None). Resolvable via the registry.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py`
- Modify: `src/kinoforge/_adapters.py` (add import)
- Create: `tests/providers/modal/test_provider.py`

**Acceptance Criteria:**
- [ ] `registry.get_provider("modal")()` returns a `ModalProvider` (after importing `kinoforge._adapters`).
- [ ] `ModalProvider().find_offers(reqs)` returns the filtered catalog.
- [ ] `last_heartbeat("x")` returns `None`; `heartbeat("x")` is a no-op (no raise).
- [ ] `__init__` accepts injectable `app_factory`, `deployer`, `stopper`, `lister`, `sleep`, `clock` seams.

**Verify:** `pixi run pytest tests/providers/modal/test_provider.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/providers/modal/test_provider.py
"""Behavior: ModalProvider registration, offers, and heartbeat semantics."""
from kinoforge.core import registry
from kinoforge.core.interfaces import HardwareRequirements
from kinoforge.providers.modal import ModalProvider


def test_registry_resolves_modal():
    import kinoforge._adapters  # noqa: F401  # triggers self-registration
    provider = registry.get_provider("modal")()
    assert isinstance(provider, ModalProvider)
    assert provider.name == "modal"


def test_find_offers_returns_filtered_catalog():
    offers = ModalProvider().find_offers(HardwareRequirements(min_vram_gb=80))
    assert {o.id for o in offers} == {"A100-80GB", "H100"}


def test_last_heartbeat_is_none_and_heartbeat_is_noop():
    # Bug caught: a missing last_heartbeat AttributeError's every HeartbeatLoop
    # tick (the gen §21 SkyPilot failure); heartbeat must not raise either.
    provider = ModalProvider()
    assert provider.last_heartbeat("pod-x") is None
    provider.heartbeat("pod-x")  # must not raise
```

- [ ] **Step 2: Run — confirm it fails.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -v`
Expected: FAIL — `ImportError: cannot import name 'ModalProvider'`

- [ ] **Step 3: Implement the skeleton.**

```python
# src/kinoforge/providers/modal/__init__.py
"""Modal serverless-GPU compute provider.

Deploys the kinoforge FastAPI generation server onto Modal as a named App whose
``@modal.web_server`` runs the same ``provision_script; exec run_cmd`` that RunPod
runs, and returns the public ``.modal.run`` URL as ``endpoints["8000"]``. All Modal
and subprocess touchpoints sit behind injected callables for offline testing.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from kinoforge.core import registry
from kinoforge.core.interfaces import (
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)
from kinoforge.providers.modal._app import (
    ModalAppRequest,
    build_modal_app,
    default_deploy,
    default_list,
    default_stop,
)
from kinoforge.providers.modal._catalog import modal_offers

_DESTROY_POLL_MAX_ITERS: int = 40  # 40 × 3s ≈ 120s upper bound (mirror SkyPilot)


class ModalProvider:
    """Compute provider backed by Modal serverless GPUs."""

    name: str = "modal"

    def __init__(
        self,
        *,
        app_factory: Callable[[ModalAppRequest, Any], tuple[Any, Any]] = build_modal_app,
        deployer: Callable[[Any, Any], str] = default_deploy,
        stopper: Callable[[str], None] = default_stop,
        lister: Callable[[], list[dict[str, Any]]] = default_list,
        modal_module: Any | None = None,  # noqa: ANN401
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialise the provider with injectable Modal/subprocess seams.

        Args:
            app_factory: Builds ``(app, server_fn)`` from a request + modal module.
            deployer: Deploys an app and returns its public web URL.
            stopper: Stops a named deployed app (bounded).
            lister: Returns deployed-app records (``modal app list --json``).
            modal_module: The ``modal`` SDK module (lazy-imported if None).
            sleep: Sleep function (injected in tests).
            clock: Monotonic-ish clock returning epoch seconds.
        """
        self._app_factory = app_factory
        self._deployer = deployer
        self._stopper = stopper
        self._lister = lister
        self._modal = modal_module
        self._sleep = sleep
        self._clock = clock
        #: run_id -> {"app": app, "url": url} for endpoints() / destroy().
        self._deployments: dict[str, dict[str, Any]] = {}

    # -- offers -------------------------------------------------------------
    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        """Return Modal catalog offers meeting ``reqs``."""
        return modal_offers(reqs)

    # -- heartbeat (Modal owns liveness) ------------------------------------
    def heartbeat(self, instance_id: str) -> None:
        """No-op — Modal manages container liveness."""
        return None

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return ``None`` — Modal exposes no wire-level heartbeat read.

        Off-ABC but REQUIRED: ``HeartbeatLoop._tick_once`` calls it every tick.
        """
        return None


registry.register_provider("modal", lambda: ModalProvider())
```

Add to `src/kinoforge/_adapters.py` (alongside the other provider imports ~line 47):

```python
import kinoforge.providers.modal  # noqa: F401
```

NOTE: Task 2 depends on Task 3's `_app.py` symbols (`ModalAppRequest`, `build_modal_app`, `default_deploy/stop/list`). Implement Task 3 first if executing strictly by TDD, OR stub `_app.py` with the signatures now and fill bodies in Task 3. The blockedBy graph orders Task 2 AFTER Task 3.

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/providers/modal/__init__.py src/kinoforge/_adapters.py tests/providers/modal/test_provider.py
git commit -m "feat(modal): ModalProvider skeleton + registry registration + find_offers/heartbeat"
```

---

### Task 3: `build_modal_app` — the reuse hinge (image/gpu/volume/secret/web_server)

**Goal:** A pure builder constructs a `modal.App` (using an injected modal module) whose `serialized` `@modal.web_server(8000)` function runs the config's `provision_script; exec run_cmd`, with the config image, resolved GPU string, HF-cache Volume, and per-run Secret.

**Files:**
- Create: `src/kinoforge/providers/modal/_app.py`
- Create: `tests/providers/modal/test_app.py`

**Acceptance Criteria:**
- [ ] `build_modal_app(req, fake_modal)` calls `Image.from_registry(req.image, add_python="3.11")`, `App(name=f"kinoforge-{req.run_id}", image=...)`, and `Volume.from_name("kinoforge-hf-cache", create_if_missing=True)`.
- [ ] The function is registered with `gpu=req.gpu` (exact Modal string), `serialized=True`, `scaledown_window=req.scaledown_window_s`, `volumes={req.volume_mount: vol}`, and a `Secret` carrying the base64 provision payload + `req.env`.
- [ ] `@modal.web_server(8000, startup_timeout=req.startup_timeout_s)` wraps the function; the boot payload contains BOTH `req.provision_script` and the joined `req.run_cmd`.
- [ ] `default_deploy` / `default_stop` / `default_list` exist with the documented Modal calls.

**Verify:** `pixi run pytest tests/providers/modal/test_app.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test** (drives a fake modal module recording calls).

```python
# tests/providers/modal/test_app.py
"""Behavior: build_modal_app wires image/gpu/volume/secret/web_server correctly."""
import base64

import pytest

from kinoforge.providers.modal._app import ModalAppRequest, build_modal_app


class _FakeWebServer:
    def __init__(self):
        self.calls = []

    def __call__(self, port, *, startup_timeout=5.0, label=None):
        self.calls.append({"port": port, "startup_timeout": startup_timeout})
        return lambda fn: fn  # passthrough decorator


class _FakeApp:
    def __init__(self, name, image):
        self.name = name
        self.image = image
        self.function_kwargs = None

    def function(self, **kwargs):
        self.function_kwargs = kwargs
        return lambda fn: fn  # passthrough decorator


class _FakeModal:
    def __init__(self):
        self.web_server = _FakeWebServer()
        self.from_registry_args = None
        self.volume_args = None
        self.secret_dict = None
        self.App = self._make_app

    def _make_app(self, name, image):
        self.app = _FakeApp(name, image)
        return self.app

    class Image:
        _outer = None

        @staticmethod
        def from_registry(tag, add_python=None):
            _FakeModal._last.from_registry_args = {"tag": tag, "add_python": add_python}
            return f"image::{tag}"

    class Volume:
        @staticmethod
        def from_name(name, create_if_missing=False):
            _FakeModal._last.volume_args = {
                "name": name,
                "create_if_missing": create_if_missing,
            }
            return f"volume::{name}"

    class Secret:
        @staticmethod
        def from_dict(d):
            _FakeModal._last.secret_dict = d
            return "secret::obj"


@pytest.fixture
def fake_modal():
    m = _FakeModal()
    _FakeModal._last = m  # let nested static methods record onto the instance
    return m


def _req():
    return ModalAppRequest(
        run_id="run123",
        image="runpod/pytorch:2.4.0-cuda12.4",
        gpu="A10",
        provision_script="echo provisioning; pip install foo",
        run_cmd=["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"],
        env={"HF_HOME": "/cache/hf"},
        volume_mount="/cache/hf",
        scaledown_window_s=300,
        startup_timeout_s=1800,
    )


def test_build_wires_image_app_volume(fake_modal):
    build_modal_app(_req(), fake_modal)
    assert fake_modal.from_registry_args == {
        "tag": "runpod/pytorch:2.4.0-cuda12.4",
        "add_python": "3.11",
    }
    assert fake_modal.app.name == "kinoforge-run123"
    assert fake_modal.volume_args == {
        "name": "kinoforge-hf-cache",
        "create_if_missing": True,
    }


def test_function_kwargs_carry_gpu_serialized_scaledown_volume(fake_modal):
    build_modal_app(_req(), fake_modal)
    kw = fake_modal.app.function_kwargs
    assert kw["gpu"] == "A10"
    assert kw["serialized"] is True  # cloudpickle the runtime-built fn
    assert kw["scaledown_window"] == 300
    assert kw["volumes"] == {"/cache/hf": "volume::A10".replace("A10", "kinoforge-hf-cache")}
    assert kw["secrets"] == ["secret::obj"]


def test_web_server_port_and_timeout(fake_modal):
    build_modal_app(_req(), fake_modal)
    assert fake_modal.web_server.calls == [{"port": 8000, "startup_timeout": 1800}]


def test_secret_payload_contains_provision_and_run_cmd(fake_modal):
    # Bug caught: dropping run_cmd (server never launches) or the provision
    # script (deps/weights never installed) → dead container at startup.
    build_modal_app(_req(), fake_modal)
    payload_b64 = fake_modal.secret_dict["KINOFORGE_PROVISION_B64"]
    decoded = base64.b64decode(payload_b64).decode()
    assert "echo provisioning" in decoded
    assert "wan_t2v_server" in decoded  # run_cmd exec'd
    assert fake_modal.secret_dict["HF_HOME"] == "/cache/hf"  # env passed through
```

- [ ] **Step 2: Run — confirm it fails.**

Run: `pixi run pytest tests/providers/modal/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError: ...modal._app`

- [ ] **Step 3: Implement.**

```python
# src/kinoforge/providers/modal/_app.py
"""Modal App construction (the Option-A reuse hinge) + default deploy/stop/list.

``build_modal_app`` builds a ``modal.App`` whose serialized ``web_server`` runs the
same ``provision_script; exec run_cmd`` bundle that RunPod runs, so the existing
FastAPI server and ``render_provision`` machinery are reused verbatim. Per-run
config reaches the remote container through a ``modal.Secret`` (no image rebuild).
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModalAppRequest:
    """Everything needed to build one Modal generation App."""

    run_id: str
    image: str
    gpu: str
    provision_script: str
    run_cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    volume_mount: str = "/cache/hf"
    scaledown_window_s: int = 300
    startup_timeout_s: int = 1800


_VOLUME_NAME = "kinoforge-hf-cache"


def _boot_payload(req: ModalAppRequest) -> str:
    """Compose the container boot script: run provision, then exec the server."""
    exec_line = "exec " + shlex.join(req.run_cmd)
    return f"{req.provision_script}\n{exec_line}\n"


def build_modal_app(req: ModalAppRequest, modal_mod: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """Build ``(app, server_fn)`` for ``req`` using ``modal_mod``.

    Args:
        req: The per-run app request.
        modal_mod: The ``modal`` SDK module (or a fake in tests).

    Returns:
        The constructed app and its decorated web-server function.
    """
    image = modal_mod.Image.from_registry(req.image, add_python="3.11")
    app = modal_mod.App(name=f"kinoforge-{req.run_id}", image=image)
    volume = modal_mod.Volume.from_name(_VOLUME_NAME, create_if_missing=True)

    payload_b64 = base64.b64encode(_boot_payload(req).encode()).decode()
    secret = modal_mod.Secret.from_dict({**req.env, "KINOFORGE_PROVISION_B64": payload_b64})

    @app.function(
        gpu=req.gpu,
        serialized=True,  # cloudpickle this runtime-built fn (not import-by-ref)
        scaledown_window=req.scaledown_window_s,
        volumes={req.volume_mount: volume},
        secrets=[secret],
    )
    @modal_mod.web_server(8000, startup_timeout=req.startup_timeout_s)
    def server() -> None:
        # Runs INSIDE the Modal container at startup. Decode the boot script,
        # write it, and launch (non-blocking) so it binds 0.0.0.0:8000.
        script = base64.b64decode(os.environ["KINOFORGE_PROVISION_B64"]).decode()
        with open("/tmp/kinoforge_boot.sh", "w") as fh:
            fh.write(script)
        subprocess.Popen(["bash", "/tmp/kinoforge_boot.sh"])  # noqa: S603,S607

    return app, server


# --- default (live) seams -------------------------------------------------

def default_deploy(app: Any, server_fn: Any) -> str:  # noqa: ANN401
    """Deploy ``app`` and return the public web URL (survives process exit)."""
    import modal

    with modal.enable_output():
        app.deploy()
    return server_fn.get_web_url()


def default_stop(app_name: str) -> None:
    """Stop a deployed app via the CLI (bounded by subprocess timeout)."""
    subprocess.run(
        ["modal", "app", "stop", app_name, "--yes"],
        check=True,
        timeout=120,
        env=os.environ.copy(),
    )


def default_list() -> list[dict[str, Any]]:
    """Return deployed-app records via ``modal app list --json``."""
    out = subprocess.run(
        ["modal", "app", "list", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env=os.environ.copy(),
    )
    return json.loads(out.stdout or "[]")
```

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/providers/modal/test_app.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/providers/modal/_app.py tests/providers/modal/test_app.py
git commit -m "feat(modal): build_modal_app reuse hinge + default deploy/stop/list seams"
```

---

### Task 4: `create_instance` — deploy + return HTTP endpoint

**Goal:** `create_instance(spec)` builds the app request from the spec + offer, deploys via the injected seam, and returns `Instance(status="starting", endpoints={"8000": url})`.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py`
- Modify: `tests/providers/modal/test_provider.py`

**Acceptance Criteria:**
- [ ] `create_instance` maps `spec.offer.gpu_type` → the Modal `gpu` string, passes `spec.provision_script`, `spec.run_cmd`, `spec.env`, and lifecycle idle → `scaledown_window_s`.
- [ ] Returns `Instance(id=spec.run_id, provider="modal", status="starting", endpoints={"8000": <url>}, cost_rate=offer rate)`.
- [ ] The deployment is recorded in `self._deployments[run_id]` for `endpoints()`/`destroy()`.
- [ ] Raises a clear error if `spec.run_cmd`/`spec.provision_script` is None (Modal needs a server to host).

**Verify:** `pixi run pytest tests/providers/modal/test_provider.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test** (append to `test_provider.py`).

```python
def test_create_instance_deploys_and_returns_endpoint():
    from kinoforge.core.interfaces import InstanceSpec, Offer, Lifecycle

    captured = {}

    def fake_factory(req, modal_mod):
        captured["req"] = req
        return ("APP", "SERVERFN")

    def fake_deploy(app, server_fn):
        captured["deployed"] = (app, server_fn)
        return "https://ws--kinoforge-run777-server.modal.run"

    provider = ModalProvider(app_factory=fake_factory, deployer=fake_deploy)
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-cuda12.4",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="run777",
        provision_script="echo hi",
        run_cmd=["python", "-m", "server"],
        env={"HF_HOME": "/cache/hf"},
        lifecycle=Lifecycle(idle_timeout_s=300),
    )
    inst = provider.create_instance(spec)

    assert inst.id == "run777"
    assert inst.provider == "modal"
    assert inst.status == "starting"
    assert inst.endpoints == {"8000": "https://ws--kinoforge-run777-server.modal.run"}
    assert inst.cost_rate_usd_per_hr == 1.10
    assert captured["req"].gpu == "A10"
    assert captured["req"].scaledown_window_s == 300


def test_create_instance_requires_run_cmd():
    from kinoforge.core.interfaces import InstanceSpec, Offer

    provider = ModalProvider()
    spec = InstanceSpec(
        image="img",
        offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
        run_id="r",
        provision_script="echo hi",
        run_cmd=None,  # no server → invalid for Modal
    )
    import pytest

    with pytest.raises(ValueError, match="run_cmd"):
        provider.create_instance(spec)
```

- [ ] **Step 2: Run — confirm it fails.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -k create_instance -v`
Expected: FAIL — `AttributeError: 'ModalProvider' object has no attribute 'create_instance'`

- [ ] **Step 3: Implement** (add to `ModalProvider`).

```python
    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Build + deploy a Modal App and return its HTTP endpoint.

        Args:
            spec: The instance spec (image, offer, provision_script, run_cmd, env).

        Returns:
            An ``Instance`` in ``starting`` state with ``endpoints["8000"]`` set.

        Raises:
            ValueError: If ``run_cmd`` or ``provision_script`` is missing.
        """
        if not spec.run_cmd or not spec.provision_script:
            raise ValueError(
                "ModalProvider requires spec.run_cmd and spec.provision_script "
                "(the server boot command); got run_cmd=%r" % (spec.run_cmd,)
            )
        if spec.offer is None:
            raise ValueError("ModalProvider requires spec.offer (GPU selection)")

        req = ModalAppRequest(
            run_id=spec.run_id,
            image=spec.image,
            gpu=spec.offer.gpu_type,
            provision_script=spec.provision_script,
            run_cmd=list(spec.run_cmd),
            env=dict(spec.env),
            volume_mount=spec.volume_mount or "/cache/hf",
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
        )
        app, server_fn = self._app_factory(req, self._modal_mod())
        url = self._deployer(app, server_fn)
        self._deployments[spec.run_id] = {"app": app, "url": url, "name": f"kinoforge-{spec.run_id}"}
        return Instance(
            id=spec.run_id,
            provider=self.name,
            status="starting",
            created_at=self._clock(),
            endpoints={"8000": url},
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=spec.offer.cost_rate_usd_per_hr,
        )

    def _modal_mod(self) -> Any:  # noqa: ANN401
        """Lazy-import the real ``modal`` module unless one was injected."""
        if self._modal is None:
            import modal

            self._modal = modal
        return self._modal
```

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_provider.py
git commit -m "feat(modal): create_instance deploys app + returns .modal.run HTTP endpoint"
```

---

### Task 5: `get/list/stop/destroy_instance` + `endpoints` (bounded teardown)

**Goal:** Inventory + teardown via the injected `lister`/`stopper` seams, with a bounded destroy poll and an `endpoints` reader.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py`
- Modify: `tests/providers/modal/test_provider.py`

**Acceptance Criteria:**
- [ ] `endpoints(instance)` returns the recorded `{"8000": url}` (from `_deployments`, falling back to `instance.endpoints`).
- [ ] `list_instances()` maps `lister()` records → `Instance`s (kinoforge-tagged app names only, `name` starts with `kinoforge-`).
- [ ] `get_instance(id)` returns the matching `Instance` or raises the standard not-found path.
- [ ] `stop_instance`/`destroy_instance` call `stopper(app_name)`; destroy polls `lister()` until the app is gone, bounded by `_DESTROY_POLL_MAX_ITERS`, and returns (idempotent) after the bound.

**Verify:** `pixi run pytest tests/providers/modal/test_provider.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test** (append).

```python
def test_endpoints_returns_recorded_url():
    from kinoforge.core.interfaces import Instance

    provider = ModalProvider()
    provider._deployments["r"] = {"url": "https://x.modal.run", "name": "kinoforge-r"}
    inst = Instance(id="r", provider="modal", status="ready", created_at=0.0)
    assert provider.endpoints(inst) == {"8000": "https://x.modal.run"}


def test_list_instances_filters_kinoforge_apps():
    records = [
        {"name": "kinoforge-run1", "state": "deployed"},
        {"name": "someone-elses-app", "state": "deployed"},
    ]
    provider = ModalProvider(lister=lambda: records)
    ids = [i.id for i in provider.list_instances()]
    assert ids == ["run1"]  # only kinoforge-prefixed, prefix stripped


def test_destroy_is_bounded_when_app_never_disappears():
    # Bug caught: an unbounded poll (the gen §21 --no-reuse hang) if the app
    # never leaves the list. Must return after _DESTROY_POLL_MAX_ITERS.
    stop_calls = []
    sleeps = []
    provider = ModalProvider(
        lister=lambda: [{"name": "kinoforge-r", "state": "deployed"}],  # never gone
        stopper=lambda name: stop_calls.append(name),
        sleep=lambda s: sleeps.append(s),
    )
    provider._deployments["r"] = {"url": "u", "name": "kinoforge-r"}
    provider.destroy_instance("r")  # must return, not hang
    assert stop_calls == ["kinoforge-r"]
    assert len(sleeps) == 40  # _DESTROY_POLL_MAX_ITERS


def test_destroy_returns_early_when_app_gone():
    provider = ModalProvider(
        lister=lambda: [],  # already gone
        stopper=lambda name: None,
        sleep=lambda s: (_ for _ in ()).throw(AssertionError("should not sleep")),
    )
    provider._deployments["r"] = {"url": "u", "name": "kinoforge-r"}
    provider.destroy_instance("r")  # returns immediately, no sleep
```

- [ ] **Step 2: Run — confirm it fails.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -k "endpoints or list_instances or destroy" -v`
Expected: FAIL — missing methods.

- [ ] **Step 3: Implement** (add to `ModalProvider`).

```python
    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the HTTP endpoint map for ``instance``."""
        rec = self._deployments.get(instance.id)
        if rec and rec.get("url"):
            return {"8000": rec["url"]}
        return dict(instance.endpoints)

    def _record_to_instance(self, rec: dict[str, Any]) -> Instance:
        name = rec.get("name", "")
        run_id = name[len("kinoforge-") :]
        state = rec.get("state", "")
        status = "ready" if state in {"deployed", "running"} else "stopped"
        return Instance(
            id=run_id,
            provider=self.name,
            status=status,
            created_at=self._clock(),
        )

    def list_instances(self) -> list[Instance]:
        """Return kinoforge-owned Modal deployments."""
        return [
            self._record_to_instance(r)
            for r in self._lister()
            if str(r.get("name", "")).startswith("kinoforge-")
        ]

    def get_instance(self, instance_id: str) -> Instance:
        """Return the named deployment or raise ``KeyError``-style not-found."""
        for inst in self.list_instances():
            if inst.id == instance_id:
                return inst
        raise KeyError(f"no modal deployment for run_id={instance_id!r}")

    def stop_instance(self, instance_id: str) -> None:
        """Stop (== destroy for Modal) the named deployment."""
        self.destroy_instance(instance_id)

    def destroy_instance(self, instance_id: str) -> None:
        """Stop the deployment and poll until gone (bounded)."""
        rec = self._deployments.get(instance_id)
        app_name = rec["name"] if rec else f"kinoforge-{instance_id}"
        try:
            self._stopper(app_name)
            for _ in range(_DESTROY_POLL_MAX_ITERS):
                names = {str(r.get("name", "")) for r in self._lister()}
                if app_name not in names:
                    break
                self._sleep(3.0)
        finally:
            self._deployments.pop(instance_id, None)
```

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/providers/modal/test_provider.py -v`
Expected: PASS (all)

- [ ] **Step 5: Full offline suite + lint/type gate, then commit.**

```bash
pixi run pytest tests/providers/modal/ -v
pixi run lint && pixi run typecheck
git add src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_provider.py
git commit -m "feat(modal): inventory + bounded teardown (get/list/stop/destroy) + endpoints"
```

---

### Task 6: Milestone 1 config + `build_provider_for` characterization test

**Goal:** A `modal-wan-t2v-1_3b.yaml` config exists and resolves through `build_provider_for` to a `ModalProvider`; the config loads without error and omits `cloud:`.

**Files:**
- Create: `examples/configs/modal-wan-t2v-1_3b.yaml`
- Create: `tests/test_modal_config.py`

**Acceptance Criteria:**
- [ ] Config sets `compute.provider: modal`, `mode: t2v`, diffusers engine + `wan_t2v_server`, `models: [hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers]`, `requirements.min_vram_gb: 24`, `gpu_preference` in Modal strings, and NO `cloud:` key.
- [ ] Loading the config + calling `build_provider_for(cfg)` returns a `ModalProvider` (no `SkyPilotCloudPinSupportedCheck` failure).
- [ ] `cfg.spec` targets Wan 2.1 1.3B at a small resolution/frame count (cheap smoke).

**Verify:** `pixi run pytest tests/test_modal_config.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/test_modal_config.py
"""Behavior: the Modal T2V config loads and resolves to a ModalProvider."""
from pathlib import Path

from kinoforge._adapters import build_provider_for
from kinoforge.core.config import load_config  # adjust to the real loader entrypoint
from kinoforge.providers.modal import ModalProvider

CFG = Path("examples/configs/modal-wan-t2v-1_3b.yaml")


def test_config_resolves_to_modal_provider():
    cfg = load_config(CFG)
    assert cfg.compute.provider == "modal"
    assert cfg.compute.cloud is None  # MUST omit cloud (non-sky)
    provider = build_provider_for(cfg)
    assert isinstance(provider, ModalProvider)


def test_config_targets_wan21_1_3b_cheaply():
    cfg = load_config(CFG)
    assert cfg.compute.requirements.min_vram_gb <= 24
    assert any("Wan2.1-T2V-1.3B" in m.ref for m in cfg.models)
```

NOTE: reconcile `load_config` with the real loader entrypoint in `src/kinoforge/core/config.py` (grep for how other config tests load YAML — do not invent the function name).

- [ ] **Step 2: Run — confirm it fails** (config file absent).

Run: `pixi run pytest tests/test_modal_config.py -v`
Expected: FAIL — file not found / provider mismatch.

- [ ] **Step 3: Write the config** (mirror `wan21-1_3b-strength-grid.yaml`, minus LoRA grid, provider→modal, cloud omitted, Modal GPU strings).

```yaml
# Wan 2.1 T2V-1.3B on Modal serverless GPU (Milestone 1 live proof).
# Delivery: Option-A generic Modal app — the diffusers wan_t2v_server runs on a
# Modal web_server(8000) via the same provision_script; exec run_cmd as RunPod.
# NOTE: NO `cloud:` key — that field is SkyPilot-only and fails validation here.

mode: t2v
prompt: "PLACEHOLDER — the live smoke passes --prompt from examples/configs/prompts/field-realistic.txt per memory feedback_standard_test_prompt; this field is unused by `kinoforge generate` (argparse requires --prompt)."

engine:
  kind: diffusers
  precision: fp16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"]
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "peft>=0.13"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules: ["kinoforge.engines.diffusers.servers"]

models:
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
    kind: base
    target: checkpoints

compute:
  provider: modal
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_usd_per_hr: 1.20
    gpu_preference:
      - "A10"
      - "L4"
      - "A100-40GB"
    disk_gb: 40
  lifecycle:
    idle_timeout: 5m
    job_timeout: 10m
    time_buffer: 2m
    max_lifetime: 40m
    boot_timeout: 30m
    budget: 2.0

spec:
  model: "Wan2.1-T2V-1.3B-Diffusers"
  pipeline: "WanPipeline"
  scheduler: "UniPCMultistepScheduler"
  width: 480
  height: 480
  num_frames: 33
  fps: 16
```

Reconcile the `prompt:` requirement against how `wan21-1_3b-strength-grid.yaml` handles it — if the loader requires a non-placeholder prompt, paste the standard field-realistic prompt verbatim (as that file does) instead of the placeholder note.

- [ ] **Step 4: Run — confirm pass.**

Run: `pixi run pytest tests/test_modal_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit.**

```bash
git add examples/configs/modal-wan-t2v-1_3b.yaml tests/test_modal_config.py
git commit -m "feat(config): Modal Wan 2.1 T2V-1.3B config + build_provider_for characterization"
```

---

### Task 7: LIVE transport smoke — prove the Modal deploy mechanism for pennies  ⟦USER-GATE⟧

**Goal:** Empirically prove — with a trivial hello-world web server, before any weights spend — that `ModalProvider` can deploy a Modal App, return a live `.modal.run` URL that serves HTTP, and tear it down cleanly.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (brief: "validate the whole path for pennies… start with the smallest model"; standing autonomous-spend authorization). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_modal_transport_smoke.py`

**Acceptance Criteria:**
- [ ] Against the REAL modal SDK (`pixi run -e live-modal`), `ModalProvider.create_instance` with a trivial `run_cmd` (a one-line Python HTTP server on `0.0.0.0:8000`) returns an `Instance` whose `endpoints["8000"]` is a `https://…modal.run` URL.
- [ ] `GET <url>/` (or `/health`) returns HTTP 200 within the startup window.
- [ ] `destroy_instance` returns within the bounded poll AND `modal app list --json` no longer lists `kinoforge-<run_id>` afterward.
- [ ] Total spend is a few cents (cheapest GPU or CPU-only; trivial server, seconds of runtime).

**Verify:** `pixi run -e live-modal pytest tests/live/test_modal_transport_smoke.py -v -s` → PASS; then `pixi run -e live-modal modal app list --json` shows no `kinoforge-` app.

**Steps:**

- [ ] **Step 1: Preflight + commit the RED scaffold BEFORE any live call** (durability rule — commit the failing test first).

```bash
pixi run preflight   # creds present, clean tree
git add tests/live/test_modal_transport_smoke.py
git commit -m "test(live): RED scaffold for Modal transport smoke (hello-world deploy)"
```

- [ ] **Step 2: Write the live smoke** (trivial server; no kinoforge engine, no weights).

```python
# tests/live/test_modal_transport_smoke.py
"""LIVE: prove Modal deploy → live URL → teardown with a trivial HTTP server.

Runs only under `pixi run -e live-modal`. Marked `live` so the default suite skips.
"""
import time
import urllib.request

import pytest

pytestmark = pytest.mark.live


def test_modal_transport_end_to_end():
    from kinoforge.core.dotenv_loader import load_env_file
    from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer
    from kinoforge.providers.modal import ModalProvider

    load_env_file()
    provider = ModalProvider()
    # Trivial server: Python stdlib http.server on 0.0.0.0:8000, no provisioning.
    spec = InstanceSpec(
        image="python:3.11-slim",
        offer=Offer("T4", "T4", 16, "12.4", 0.59, mode="serverless"),
        run_id=f"smoke{int(time.time())}",
        provision_script="echo 'no provisioning needed'",
        run_cmd=["python", "-m", "http.server", "8000", "--bind", "0.0.0.0"],
        env={},
        lifecycle=Lifecycle(idle_timeout_s=60, boot_timeout_s=300),
    )
    inst = provider.create_instance(spec)
    try:
        url = inst.endpoints["8000"]
        assert url.startswith("https://") and url.endswith(".modal.run")
        # Poll until the server answers (bounded).
        deadline = time.time() + 300
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    last = resp.status
                    if last == 200:
                        break
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)
            time.sleep(5)
        assert last == 200, f"server never returned 200; last={last}"
    finally:
        provider.destroy_instance(inst.id)

    # Confirm teardown.
    names = {r.get("name") for r in provider._lister()}
    assert f"kinoforge-{spec.run_id}" not in names
```

- [ ] **Step 3: Run the live smoke** (monitor during — poll the URL; if no 200 within the window, capture Modal logs `modal app logs kinoforge-<run_id>`, stop the app, fail fast).

Run: `pixi run -e live-modal pytest tests/live/test_modal_transport_smoke.py -v -s`
Expected: PASS.

- [ ] **Step 4: Verify no orphaned app + no ledger ghost.**

```bash
pixi run -e live-modal modal app list --json
pixi run kinoforge list
```
Expected: no `kinoforge-smoke*` app; `kinoforge list` clean.

- [ ] **Step 5: Commit the GREEN result.**

```bash
git add tests/live/test_modal_transport_smoke.py
git commit -m "test(live): Modal transport smoke green — deploy/serve/teardown proven"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "verifyCommand": "pixi run -e live-modal pytest tests/live/test_modal_transport_smoke.py -v -s", "acceptanceCriteria": ["endpoints['8000'] is a https .modal.run URL", "GET url returns 200 within startup window", "destroy returns bounded AND app no longer listed", "spend is a few cents"], "modelTier": "standard"}
```

---

### Task 8: LIVE Milestone 1 proof — Wan 2.1 T2V-1.3B generation  ⟦USER-GATE⟧

**Goal:** Produce a real Wan 2.1 T2V-1.3B video via `kinoforge generate` on Modal, frame-QA it, and log it as the first Modal-provider generation.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (brief Milestone 1: "FIRST end-to-end proof of the Modal transport + the diffusers server on Modal"; ordering: "do NOT start a later milestone until this is live-green, frame-QA'd, and logged"). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_modal_wan_t2v_1_3b.py` (RED scaffold / xfail live marker)
- Modify: `successful-generations.md` (new Modal-provider section — done during close-out)

**Acceptance Criteria:**
- [ ] `pixi run -e live-modal kinoforge generate --config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v --prompt "$(cat examples/configs/prompts/field-realistic.txt)" --no-reuse` produces a playable mp4 (`ffprobe` reports 480×480, ~33 frames).
- [ ] Frames extracted (`kinoforge.core.frames.ffmpeg_frames_by_count`) and eyeballed: coherent, prompt-adherent, not corrupt (per the mandatory visual-QA rule). Verdict recorded.
- [ ] After the run, `pixi run kinoforge list` shows no instances AND `modal app list --json` shows no `kinoforge-` app (teardown verified per `--no-reuse` rule).
- [ ] A new section is added to `successful-generations.md` (new provider axis = Modal) per that file's schema.
- [ ] Total spend within the $30 credit (this run: pennies–~$1 including cold-start weight download).

**Verify:** the generate command above exits 0 with a playable 480×480 mp4; frame-QA verdict recorded; `kinoforge list` + `modal app list` both clean.

**Steps:**

- [ ] **Step 1: Preflight + commit the RED scaffold BEFORE the live spend.**

```bash
pixi run preflight
git add tests/live/test_modal_wan_t2v_1_3b.py
git commit -m "test(live): RED scaffold for Modal Wan 2.1 T2V-1.3B Milestone 1 proof"
```

The scaffold marks the live proof `xfail`/`live` and documents the exact generate command + expected artifact shape (so a mid-spend crash loses no work):

```python
# tests/live/test_modal_wan_t2v_1_3b.py
"""LIVE Milestone 1: Wan 2.1 T2V-1.3B on Modal. Driven manually via the CLI;
this file records the contract + a smoke assertion on the produced artifact."""
import pytest

pytestmark = pytest.mark.live

GENERATE_CMD = (
    "pixi run -e live-modal kinoforge generate "
    "--config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v "
    '--prompt "$(cat examples/configs/prompts/field-realistic.txt)" --no-reuse'
)


@pytest.mark.xfail(reason="live proof driven via CLI; see PROGRESS + successful-generations")
def test_modal_wan_t2v_1_3b_contract():
    raise AssertionError("run GENERATE_CMD live; assert 480x480 ~33f mp4 + frame-QA")
```

- [ ] **Step 2: Run the live generation** with polling per the live-smoke monitoring rule (surface GPU/CPU/mem, not just spend; treat GPU 0% for ≥3 probes as dead → pull Modal logs, stop, fail fast).

Run:
```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-1_3b.yaml \
  --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --no-reuse
```
Expected: completes with an artifact URI; mp4 downloaded locally.

- [ ] **Step 3: Frame-QA the output** (mandatory before reporting green).

```bash
pixi run python -c "from kinoforge.core.frames import ffmpeg_frames_by_count; ffmpeg_frames_by_count('<artifact.mp4>', 5, '/tmp/claude-1000/-workspace/<session>/scratchpad/modal_m1_frames')"
```
Read the frames; record a verdict (coherence / artifacts / prompt adherence). Flag ⚠️ if anything is off.

- [ ] **Step 4: Verify teardown (both surfaces).**

```bash
pixi run kinoforge list
pixi run -e live-modal modal app list --json
```
Expected: `No running instances.` + `No instances recorded in ledger.`; no `kinoforge-` app. If either shows a survivor: `pixi run kinoforge destroy --id <id>` / `modal app stop kinoforge-<run_id> --yes`.

- [ ] **Step 5: Log the generation + update PROGRESS, then commit.**

Add a Modal section to `successful-generations.md` (schema per its preamble: provider=modal, engine=diffusers, model=Wan2.1-T2V-1.3B, mode=t2v, GPU, cost, frame-QA verdict, repro command). Update `PROGRESS.md` RESUME SNAPSHOT (Milestone 1 live-green). Commit:

```bash
git add successful-generations.md PROGRESS.md tests/live/test_modal_wan_t2v_1_3b.py
git commit -m "docs(gen): Modal Milestone 1 — Wan 2.1 T2V-1.3B live-green + frame-QA"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "verifyCommand": "pixi run -e live-modal kinoforge generate --config examples/configs/modal-wan-t2v-1_3b.yaml --mode t2v --prompt \"$(cat examples/configs/prompts/field-realistic.txt)\" --no-reuse", "acceptanceCriteria": ["playable 480x480 ~33f mp4 produced", "frames eyeballed coherent + prompt-adherent, verdict recorded", "kinoforge list AND modal app list both clean after", "successful-generations.md Modal section added", "spend within $30 credit"], "modelTier": "standard"}
```

---

## Task dependency graph

- Task 0 → (Tasks 1, 6 can start after; 1 is code-independent of 0 but the suite runs in default env)
- Task 1 → Task 3 (catalog used by nothing in _app, but keep 1 first) ; Task 3 → Task 2 (provider imports _app symbols)
- Task 2 → Task 4 → Task 5
- Task 6 depends on Task 2 (build_provider_for resolves ModalProvider)
- Task 7 depends on Tasks 4 + 5 (create + destroy) and Task 0 (live-modal env)
- Task 8 depends on Task 6 (config) + Task 7 (transport proven) + Task 0

## Self-review

- **Spec coverage:** §2 architecture → Tasks 3+4; §3 ABC mapping → Tasks 1,2,4,5; §4 seams/testing → all unit tasks; §5 config → Task 6; §6 lifecycle/billing → Tasks 4 (scaledown) + 5 (bounded stop); §7 registration/pixi → Tasks 0+2; §8 live proof → Tasks 7+8; §9 plan-time research → resolved in "Design facts locked" (serialized=True, scaledown_window, get_web_url, shell-out stop/list). No gaps.
- **Placeholder scan:** the config `prompt:` field carries a note + reconcile instruction (the smoke passes `--prompt` from the standard file); `load_config` flagged to reconcile with the real loader name. These are explicit reconcile-at-execution notes, not silent TBDs.
- **Type consistency:** `ModalAppRequest` fields match between `_app.py` and `create_instance`; `Offer.mode="serverless"` consistent; `_DESTROY_POLL_MAX_ITERS=40` consistent with the bounded-poll test asserting 40 sleeps; `get_web_url()` (not `.web_url`) used consistently.
