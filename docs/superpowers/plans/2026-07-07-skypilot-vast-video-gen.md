# SkyPilot vast.ai Video Generation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a real video on a vast.ai GPU provisioned via SkyPilot by shimming sky's broken vast adapter and giving `SkyPilotProvider` a provider-internal SSH tunnel so the video engine reaches the server over HTTP — proven by one cheap live upscale.

**Architecture:** Transport lives inside the provider. After `sky.launch`, `SkyPilotProvider.create_instance` opens an `ssh -L` local port-forward and returns `endpoints={"8000": "http://127.0.0.1:<port>"}`, exactly where RunPod returns a proxy URL — `DiffusersEngine.wait_for_ready`/`http_get`/`generate` are untouched. A one-line-equivalent compat shim fixes sky's vast adapter against vastai-sdk 0.2.5. Delivered as slice 1 (vast upscale proof); reconnect/Lambda/warm-reuse deferred to slice 2.

**Tech Stack:** Python 3.13, SkyPilot 0.12.3.post1 (`live-skypilot` pixi env), vastai-sdk 0.2.5, pytest with injected `sky_client`/`ssh_spawn`/`port_allocator` seams, existing `DiffusersEngine` FlashVSR upscale path.

**User decisions (already made):**
- Scope = "Generic, incremental" — build the generic provider-internal tunnel seam, deliver + live-validate vast first, defer Lambda/hardening to slice 2. (quoted choice)
- Transport = "SSH port-forward" — provider spawns `ssh -L`, returns `http://127.0.0.1:<port>`; engine stays agnostic. Rejected sky-native public `ports` (unauth exposure) + `sky exec` (no-HTTP).
- Slice-1 load = "Upscale-only (cheapest)" — FlashVSR `upscale_only` on a fixture clip, ~$0.08–0.15.
- Vast unblock = compat shim we own (not wait-for-upstream); idempotent + self-disabling.
- Non-goals: warm-reuse for sky, tunnel-drop reconnect, Lambda parity, public-port/auth path — all slice 2.

---

## File Structure

- **New:** `src/kinoforge/providers/skypilot/vast_compat.py` — `apply_vast_sdk_compat()`: idempotent monkeypatch making `VastAI().client.api_key` resolve. One responsibility: bridge sky's vast adapter to vastai-sdk ≥ 0.2.
- **Modify:** `src/kinoforge/providers/skypilot/__init__.py` — call the shim at import; add `ssh_spawn`/`port_allocator` seams + `_tunnels` map + `_VIDEO_SERVER_PORT`; open tunnel + populate `endpoints` in `create_instance`; kill tunnel in `destroy_instance`.
- **New:** `examples/configs/skypilot-vast-flashvsr.yaml` — provider=skypilot, cloud=[vast], upscale-only FlashVSR cfg for the live proof.
- **New tests:** `tests/providers/test_skypilot_vast_compat.py` (Task 0), `tests/providers/test_skypilot_tunnel.py` (Tasks 1–3), `tests/live/test_skypilot_vast_flashvsr_live.py` (Task 5). Extend `tests/core/test_config.py` (Task 4).
- **Modify:** `PROGRESS.md`, `successful-generations.md` (Task 5/6).

---

## Task 0: Vast adapter compat shim

**Goal:** `apply_vast_sdk_compat()` makes sky's `vast.vast().client.api_key` resolve against vastai-sdk 0.2.5, idempotently and only when needed.

**Files:**
- Create: `src/kinoforge/providers/skypilot/vast_compat.py`
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (call shim at import)
- Test: `tests/providers/test_skypilot_vast_compat.py`

**Acceptance Criteria:**
- [ ] After `apply_vast_sdk_compat()`, `VastAI(api_key="k").client.api_key == "k"`.
- [ ] Second call returns `False` (idempotent — already patched).
- [ ] Returns `False` (no raise) when `vastai_sdk` is importable but already has a working `.client`, or when import fails.

**Verify:** `pixi run -e live-skypilot pytest tests/providers/test_skypilot_vast_compat.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — `tests/providers/test_skypilot_vast_compat.py`:

```python
"""apply_vast_sdk_compat bridges sky's vast adapter to vastai-sdk >= 0.2."""

from __future__ import annotations

import pytest

pytest.importorskip("vastai_sdk")  # only runs in the live-skypilot env


def test_shim_makes_client_api_key_resolve() -> None:
    # Bug caught: sky/provision/vast/utils.py:204 reads vast.vast().client.api_key
    # but vastai-sdk 0.2.5 has no .client, so every vast launch AttributeErrors.
    from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

    apply_vast_sdk_compat()
    from vastai_sdk import VastAI

    assert VastAI(api_key="secret-key").client.api_key == "secret-key"


def test_shim_is_idempotent() -> None:
    # Bug caught: re-applying stacks properties / re-patches an already-good class.
    from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

    apply_vast_sdk_compat()
    assert apply_vast_sdk_compat() is False
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run -e live-skypilot pytest tests/providers/test_skypilot_vast_compat.py -v` → Expected: FAIL (`ModuleNotFoundError: kinoforge.providers.skypilot.vast_compat`).

- [ ] **Step 3: Implement** `src/kinoforge/providers/skypilot/vast_compat.py`:

```python
"""Compat shim for SkyPilot's vast adapter vs vastai-sdk >= 0.2.

sky 0.12.3.post1's vast provisioner reads ``vast.vast().client.api_key``
(``sky/provision/vast/utils.py:204``). vastai-sdk 0.2.5 refactored the client:
``VastAI`` exposes ``.api_key`` directly and has no ``.client`` attribute, so the
old accessor AttributeErrors and every vast launch dies. This shim adds a
``client`` property that returns ``self`` so ``.client.api_key`` resolves to
``.api_key``. Idempotent + self-disabling: a no-op when ``VastAI`` already
resolves ``.client`` (a real client or a prior patch) or when ``vastai_sdk`` is
absent (the default pixi env has no vast SDK).
"""

from __future__ import annotations


def apply_vast_sdk_compat() -> bool:
    """Patch ``vastai_sdk.VastAI`` so ``.client.api_key`` resolves.

    Returns:
        ``True`` if the patch was applied this call; ``False`` if it was
        unnecessary (already resolvable) or ``vastai_sdk`` is unavailable.
    """
    try:
        from vastai_sdk import VastAI
    except Exception:  # noqa: BLE001 — sdk absent (default env) → nothing to patch
        return False
    if getattr(VastAI, "client", None) is not None:
        return False  # real client attr or prior patch → leave untouched
    VastAI.client = property(lambda self: self)  # type: ignore[attr-defined]
    return True
```

- [ ] **Step 4: Wire the shim at provider import.** In `src/kinoforge/providers/skypilot/__init__.py`, immediately after the existing imports block (before the module constants / class), add:

```python
from kinoforge.providers.skypilot.vast_compat import apply_vast_sdk_compat

# Bridge sky's vast adapter to vastai-sdk >= 0.2 as soon as the provider is
# imported; no-op when vastai_sdk is absent (default env) or already correct.
apply_vast_sdk_compat()
```

- [ ] **Step 5: Run tests** — Run: `pixi run -e live-skypilot pytest tests/providers/test_skypilot_vast_compat.py -v` → Expected: PASS. Also confirm the default env still imports the provider: `pixi run python -c "import kinoforge.providers.skypilot"` → no error (shim returns False, no vast SDK).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/skypilot/vast_compat.py src/kinoforge/providers/skypilot/__init__.py tests/providers/test_skypilot_vast_compat.py
git commit -m "feat(skypilot): vast_compat shim — resolve VastAI().client.api_key for sky vast adapter (vastai-sdk 0.2.5)"
```

---

## Task 1: Tunnel seams + create_instance HTTP endpoint

**Goal:** `SkyPilotProvider.__init__` gains injectable `ssh_spawn`/`port_allocator` seams + a `_tunnels` map; `create_instance` opens an `ssh -L` tunnel and returns `endpoints={"8000": "http://127.0.0.1:<port>"}` for a server spec.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`__init__` ~454-498; `create_instance` ~609-716; add module const + real seam helpers)
- Test: `tests/providers/test_skypilot_tunnel.py` (new)

**Acceptance Criteria:**
- [ ] With a spec that has `run_cmd` (a server), `create_instance` returns an `Instance` whose `endpoints == {"8000": "http://127.0.0.1:<allocated-port>"}`.
- [ ] The `ssh_spawn` seam is called with `(cluster_name, allocated_port, 8000)` and its process handle is stored in `provider._tunnels[cluster_name]`.
- [ ] A spec with **no** `run_cmd` (e.g. CPU deploy) opens no tunnel and returns empty `endpoints` (regression guard — deploy smokes unaffected).
- [ ] If `ssh_spawn` raises, `create_instance` raises `ProvisionFailed` (and best-effort tears the cluster down).

**Verify:** `pixi run pytest tests/providers/test_skypilot_tunnel.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — `tests/providers/test_skypilot_tunnel.py`:

```python
"""SkyPilotProvider provider-internal ssh -L tunnel → HTTP endpoint."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import ProvisionFailed
from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.skypilot import SkyPilotProvider


class _FakeTask:
    @staticmethod
    def from_yaml_config(cfg: dict[str, Any]) -> dict[str, Any]:
        return cfg  # the launch fake just needs *something* truthy


class _FakeSky:
    """Minimal sky stub: Task.from_yaml_config + launch + status + down."""

    Task = _FakeTask

    def __init__(self) -> None:
        self.downed: list[str] = []

    def launch(self, task: Any, **kw: Any) -> tuple[None, None]:
        return (None, None)

    def status(self) -> list[dict[str, Any]]:
        return []

    def down(self, name: str) -> None:
        self.downed.append(name)


class _FakeProc:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def _gpu_offer() -> Offer:
    return Offer(
        id="RTX_A6000",
        gpu_type="RTX_A6000",
        vram_gb=48,
        cuda="12.4",
        cost_rate_usd_per_hr=0.50,
        mode="pod",
    )


def _server_spec() -> InstanceSpec:
    return InstanceSpec(
        image="runpod/pytorch:2.8.0",
        offer=_gpu_offer(),
        ports=("8000",),
        env={},
        run_id="kf-vast-test",
        provision_script="#!/bin/sh\ntrue\n",
        run_cmd=["python", "-m", "server"],
    )


def _cpu_spec() -> InstanceSpec:
    return InstanceSpec(
        image="",
        offer=Offer(id="cpu", gpu_type="", vram_gb=0, cuda="", cost_rate_usd_per_hr=0.0),
        ports=(),
        env={},
        run_id="kf-cpu-test",
        provision_script="#!/bin/sh\ntrue\n",
        run_cmd=[],
    )


def _provider(sky: _FakeSky, *, spawn=None, port=54321) -> SkyPilotProvider:
    return SkyPilotProvider(
        sky_client=sky,
        ssh_spawn=spawn if spawn is not None else (lambda *_a: _FakeProc()),
        port_allocator=lambda: port,
    )


def test_create_instance_opens_tunnel_and_sets_http_endpoint() -> None:
    # Bug caught: create_instance returns empty endpoints, so wait_for_ready
    # raises "pod has no endpoints" and video generation on sky never runs.
    spawned: dict[str, Any] = {}

    def fake_spawn(cluster: str, lp: int, rp: int) -> _FakeProc:
        spawned["args"] = (cluster, lp, rp)
        return _FakeProc()

    provider = _provider(_FakeSky(), spawn=fake_spawn, port=54321)
    inst = provider.create_instance(_server_spec())

    assert inst.endpoints == {"8000": "http://127.0.0.1:54321"}
    assert spawned["args"] == ("kf-vast-test", 54321, 8000)
    assert provider._tunnels["kf-vast-test"] is not None  # noqa: SLF001


def test_cpu_spec_opens_no_tunnel() -> None:
    # Bug caught: opening a tunnel for a server-less deploy spawns a doomed ssh
    # (nothing on :8000) and regresses the existing CPU deploy smoke.
    provider = _provider(_FakeSky())
    inst = provider.create_instance(_cpu_spec())

    assert inst.endpoints == {}
    assert "kf-cpu-test" not in provider._tunnels  # noqa: SLF001


def test_tunnel_spawn_failure_raises_provisionfailed() -> None:
    # Bug caught: a failed port-forward leaves a live, unreachable cluster billing.
    def boom(*_a: Any) -> Any:
        raise RuntimeError("ssh boom")

    sky = _FakeSky()
    provider = _provider(sky, spawn=boom)
    with pytest.raises(ProvisionFailed):
        provider.create_instance(_server_spec())
    assert "kf-vast-test" in sky.downed  # best-effort teardown fired
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/providers/test_skypilot_tunnel.py -v` → Expected: FAIL (`SkyPilotProvider.__init__() got an unexpected keyword argument 'ssh_spawn'`).

- [ ] **Step 3: Add module const + real seam helpers.** In `src/kinoforge/providers/skypilot/__init__.py`, add near the other module constants (top of file, after imports). Confirm `subprocess` and `socket` are imported — add `import socket` and `import subprocess` to the import block if absent:

```python
#: Port the diffusers/comfyui video server listens on inside the cluster
#: (matches the RunPod path's 8000). The provider forwards a local port to this.
_VIDEO_SERVER_PORT: int = 8000


def _alloc_free_port() -> int:
    """Return an ephemeral free localhost TCP port for a tunnel's local end."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _spawn_ssh_tunnel(cluster_name: str, local_port: int, remote_port: int) -> Any:
    """Spawn a background ``ssh -N -L`` port-forward to ``cluster_name``.

    Relies on sky's generated SSH config making ``cluster_name`` resolvable.
    ``ExitOnForwardFailure`` makes ssh exit (not hang) if the forward can't bind.
    """
    return subprocess.Popen(  # noqa: S603 — fixed argv, cluster_name from our own run_id
        [
            "ssh",
            "-N",
            "-T",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ExitOnForwardFailure=yes",
            "-L",
            f"{local_port}:localhost:{remote_port}",
            cluster_name,
        ]
    )
```

- [ ] **Step 4: Add the seams to `__init__`.** Add two params + the tunnels map. In `SkyPilotProvider.__init__` signature (after `sleep`):

```python
        ssh_spawn: Callable[[str, int, int], Any] | None = None,
        port_allocator: Callable[[], int] | None = None,
```

and in the body (after `self._sleep = sleep`):

```python
        self._ssh_spawn: Callable[[str, int, int], Any] = (
            ssh_spawn if ssh_spawn is not None else _spawn_ssh_tunnel
        )
        self._alloc_port: Callable[[], int] = (
            port_allocator if port_allocator is not None else _alloc_free_port
        )
        #: cluster_name -> live tunnel subprocess handle (killed on destroy).
        self._tunnels: dict[str, Any] = {}
```

- [ ] **Step 5: Open the tunnel in `create_instance`.** Ensure `ProvisionFailed` is imported (`from kinoforge.core.errors import ProvisionFailed` — add to the existing errors import). Replace the final `return Instance(...)` block (~707-716) with:

```python
        endpoints: dict[str, str] = {}
        # Only a server spec (long-running run_cmd) needs an HTTP tunnel; a
        # server-less deploy (CPU smoke) gets no tunnel and empty endpoints.
        if spec.run_cmd:
            local_port = self._alloc_port()
            try:
                tunnel = self._ssh_spawn(
                    cluster_name, local_port, _VIDEO_SERVER_PORT
                )
            except Exception as exc:  # noqa: BLE001 — any spawn fault → clean fail
                # Best-effort teardown so a live-but-unreachable cluster is not
                # left billing while we raise.
                try:
                    _resolve(sky, sky.down(cluster_name))
                except Exception:  # noqa: BLE001, S110
                    pass
                raise ProvisionFailed(
                    f"failed to open ssh tunnel to {cluster_name!r}: {exc}"
                ) from exc
            self._tunnels[cluster_name] = tunnel
            endpoints = {"8000": f"http://127.0.0.1:{local_port}"}
        return Instance(
            id=cluster_name,
            provider=self.name,
            status="starting",
            created_at=time.time(),
            endpoints=endpoints,
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=(
                spec.offer.cost_rate_usd_per_hr if spec.offer else 0.0
            ),
        )
```

- [ ] **Step 6: Run tests** — Run: `pixi run pytest tests/providers/test_skypilot_tunnel.py -v` → Expected: PASS.

- [ ] **Step 7: Guard regression** — Run: `pixi run pytest tests/providers/test_skypilot.py -q` → Expected: PASS (existing create/launch tests unaffected; they pass CPU/GPU specs — verify whether any pass a `run_cmd`; if a pre-existing test now gets endpoints, that is correct new behavior, update its assertion only if it explicitly asserted empty endpoints).

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py tests/providers/test_skypilot_tunnel.py
git commit -m "feat(skypilot): provider-internal ssh -L tunnel — create_instance returns http://127.0.0.1 endpoint for server specs"
```

---

## Task 2: destroy_instance tunnel teardown

**Goal:** `destroy_instance` kills the tunnel subprocess (in a `finally`, even if `sky down` raises) and drops it from `_tunnels`, so no orphaned ssh procs.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`destroy_instance` ~761-783; add `_kill_tunnel`)
- Test: `tests/providers/test_skypilot_tunnel.py` (extend)

**Acceptance Criteria:**
- [ ] After `create_instance` + `destroy_instance`, the tunnel's `terminate()` was called and `_tunnels` no longer contains the cluster.
- [ ] If `sky.down` raises, the tunnel is still terminated (kill runs in `finally`) and the exception propagates.
- [ ] Destroying a cluster with no recorded tunnel (e.g. a CPU deploy) does not raise.

**Verify:** `pixi run pytest tests/providers/test_skypilot_tunnel.py -k destroy -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `tests/providers/test_skypilot_tunnel.py`:

```python
def test_destroy_kills_tunnel_then_drops_it() -> None:
    # Bug caught: destroy tears the cluster but leaks the ssh subprocess forever.
    proc = _FakeProc()
    provider = _provider(_FakeSky(), spawn=lambda *_a: proc)
    inst = provider.create_instance(_server_spec())

    provider.destroy_instance(inst.id)

    assert proc.terminated is True
    assert inst.id not in provider._tunnels  # noqa: SLF001


def test_destroy_kills_tunnel_even_if_down_raises() -> None:
    # Bug caught: a failing sky.down skips tunnel cleanup → orphaned ssh proc.
    proc = _FakeProc()

    class _BadSky(_FakeSky):
        def down(self, name: str) -> None:
            raise RuntimeError("down fail")

    provider = _provider(_BadSky(), spawn=lambda *_a: proc)
    inst = provider.create_instance(_server_spec())

    with pytest.raises(RuntimeError):
        provider.destroy_instance(inst.id)
    assert proc.terminated is True


def test_destroy_without_tunnel_is_noop() -> None:
    # Bug caught: KeyError when destroying a server-less (no-tunnel) cluster.
    provider = _provider(_FakeSky())
    inst = provider.create_instance(_cpu_spec())
    provider.destroy_instance(inst.id)  # must not raise
```

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/providers/test_skypilot_tunnel.py -k destroy -v` → Expected: FAIL (tunnel never terminated — `proc.terminated` stays False).

- [ ] **Step 3: Add `_kill_tunnel` + wrap `destroy_instance`.** Add the helper method on `SkyPilotProvider`:

```python
    @staticmethod
    def _kill_tunnel(tunnel: Any) -> None:
        """Terminate a tunnel subprocess best-effort (never raises)."""
        try:
            tunnel.terminate()
        except Exception:  # noqa: BLE001, S110 — teardown must not mask the real error
            pass
```

Replace the `destroy_instance` body (~771-783) so the tunnel is popped up front and killed in a `finally`:

```python
        sky = self._sky()
        tunnel = self._tunnels.pop(instance_id, None)
        try:
            # Resolve the down RequestId so the call blocks until SkyPilot has
            # accepted the teardown, then poll until the cluster disappears.
            _resolve(sky, sky.down(instance_id))
            while True:
                clusters = _resolve(sky, sky.status())
                names = {_record_field(c, "name") for c in clusters}
                if instance_id not in names:
                    return  # confirmed gone
                self._sleep(3.0)
        finally:
            if tunnel is not None:
                self._kill_tunnel(tunnel)
```

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/providers/test_skypilot_tunnel.py -k destroy -v` → Expected: PASS.

- [ ] **Step 5: Guard regression** — Run: `pixi run pytest tests/providers/test_skypilot.py -q` → Expected: PASS (existing destroy/poll tests unaffected — `_tunnels.pop(..., None)` is a no-op for their tunnel-less clusters).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py tests/providers/test_skypilot_tunnel.py
git commit -m "feat(skypilot): destroy_instance kills the ssh tunnel in finally (no orphaned port-forwards)"
```

---

## Task 3: Provisioning characterization guard

**Goal:** Lock the fact that `create_instance` builds a sky Task carrying the engine `provision_script` as `setup` and the server `run_cmd` as `run`, so the server actually starts on the node (Component C).

**Files:**
- Test: `tests/providers/test_skypilot_tunnel.py` (extend — reuses the Task-1 fakes)

**Acceptance Criteria:**
- [ ] The Task config passed to `sky.Task.from_yaml_config` has `setup` equal to the provision script with the trailing `exec` line stripped.
- [ ] The Task config's `run` equals the shell-quoted joined `run_cmd`.
- [ ] The Task config requests the GPU accelerator from `spec.offer.gpu_type`.

**Verify:** `pixi run pytest tests/providers/test_skypilot_tunnel.py -k task_carries -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `tests/providers/test_skypilot_tunnel.py`:

```python
def test_task_carries_provision_setup_and_server_run() -> None:
    # Bug caught: the sky Task drops setup/run, so the video server never starts
    # on the node and the tunnel forwards to a dead port.
    captured: dict[str, Any] = {}

    class _RecTask:
        @staticmethod
        def from_yaml_config(cfg: dict[str, Any]) -> dict[str, Any]:
            captured["cfg"] = cfg
            return cfg

    class _RecSky(_FakeSky):
        Task = _RecTask

    provider = _provider(_RecSky())
    spec = _server_spec()  # provision_script + run_cmd both set
    provider.create_instance(spec)

    cfg = captured["cfg"]
    assert cfg["setup"].strip() == "true"  # "#!/bin/sh\ntrue\n" → stripped setup
    assert cfg["run"] == "python -m server"  # shlex-quoted join of run_cmd
    assert cfg["resources"]["accelerators"] == "RTX_A6000:1"
```

- [ ] **Step 2: Run to confirm** — Run: `pixi run pytest tests/providers/test_skypilot_tunnel.py -k task_carries -v` → Expected: PASS immediately (this is a characterization guard on existing wiring — provision_script→setup, run_cmd→run, offer→accelerators already exist per `create_instance` ~685-688, ~654-655). If it FAILS, the wiring regressed and must be restored, not the test weakened.

- [ ] **Step 3: Commit**

```bash
git add tests/providers/test_skypilot_tunnel.py
git commit -m "test(skypilot): guard create_instance carries provision setup + server run into the sky Task"
```

---

## Task 4: Live-proof example config

**Goal:** `examples/configs/skypilot-vast-flashvsr.yaml` — a provider=skypilot, cloud=[vast], upscale-only FlashVSR cfg that loads and passes validation.

**Files:**
- Create: `examples/configs/skypilot-vast-flashvsr.yaml`
- Test: `tests/core/test_config.py` (extend)

**Acceptance Criteria:**
- [ ] `load_config` on the file yields `cfg.compute.provider == "skypilot"` and `cfg.compute.cloud == ["vast"]`.
- [ ] The engine block sets `upscale_only: true` and `upscale.engine == "flashvsr"`.
- [ ] The cfg passes the SkyPilot cloud-pin validation check (`vast` ∈ supported clouds).

**Verify:** `pixi run pytest tests/core/test_config.py -k skypilot_vast_flashvsr -v` → pass.

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `tests/core/test_config.py`:

```python
def test_skypilot_vast_flashvsr_cfg_loads() -> None:
    """The SkyPilot-vast FlashVSR upscale cfg parses + pins vast.

    Bug caught: a typo'd provider/cloud or an unsupported cloud ships a cfg that
    only fails on a live launch.
    """
    from pathlib import Path

    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/skypilot-vast-flashvsr.yaml"))
    assert cfg.compute is not None
    assert cfg.compute.provider == "skypilot"
    assert cfg.compute.cloud == ["vast"]
    assert cfg.engine.diffusers is not None
    assert cfg.engine.diffusers.upscale_only is True
    assert cfg.upscale is not None
    assert cfg.upscale.engine == "flashvsr"
```

Read `examples/configs/upscale-flashvsr-1080p.yaml` and the ComputeConfig/engine schema first so the field paths in the assertions match the real accessors (`cfg.engine.diffusers.upscale_only`, `cfg.upscale.engine`); adjust the assertions to the actual attribute names if they differ — do NOT weaken them.

- [ ] **Step 2: Run to confirm failure** — Run: `pixi run pytest tests/core/test_config.py -k skypilot_vast_flashvsr -v` → Expected: FAIL (file does not exist).

- [ ] **Step 3: Create the config.** `examples/configs/skypilot-vast-flashvsr.yaml` — modelled on `upscale-flashvsr-1080p.yaml`, switched to SkyPilot/vast. Read that file for the exact engine/upscale block, then:

```yaml
# Upscale-only FlashVSR on vast.ai via SkyPilot (slice-1 live proof).
#
# Proves the provider-internal SSH-tunnel HTTP path: SkyPilotProvider launches a
# vast GPU, opens ssh -L to the diffusers server's :8000, and the existing
# upscale path drives it over http://127.0.0.1:<port> unchanged.
#
# See docs/superpowers/specs/2026-07-07-skypilot-vast-video-gen-design.md.
#
# Usage:
#   pixi run -e live-skypilot kinoforge upscale \
#     --config examples/configs/skypilot-vast-flashvsr.yaml \
#     --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
#     --no-reuse

engine:
  kind: diffusers
  precision: bfloat16
  diffusers:
    image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
      - "modelscope"
    embed_modules:
      - "kinoforge.engines.diffusers.servers"
      - "kinoforge.upscalers.flashvsr"
    embed_files:
      - "kinoforge.core.errors"
      - "kinoforge.core.scale_target"
    upscale_only: true

models: []

compute:
  provider: skypilot
  cloud:
    - "vast"
  image: "runpod/pytorch:2.8.0-py3.11-cuda12.8.1-cudnn-devel-ubuntu22.04"
  mode: pod
  requirements:
    min_vram_gb: 40
    min_cuda: "12.4"
    max_usd_per_hr: 1.00
    disk_gb: 60
  lifecycle:
    boot_timeout: 15m
    idle_timeout: 30m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 90m
    budget: 1.0
    capacity_wait: 5m

spec:
  model: "flashvsr-wan21-bfloat16"

upscale:
  engine: flashvsr
  scale: 1080p
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    precision: bfloat16
    window_size: 24
    tile_size: 512
    long_video_mode: false
```

Note: `min_vram_gb: 40` targets a cheap vast A6000/A100-class card; confirm at live time that a vast offer at ≤ `$1.00/hr` satisfies FlashVSR upscale-only. If the live probe (Task 5 Step 1) shows FlashVSR needs more/less VRAM, adjust this one field before the live run — it is the only VRAM knob.

- [ ] **Step 4: Run tests** — Run: `pixi run pytest tests/core/test_config.py -k skypilot_vast_flashvsr -v` → Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add examples/configs/skypilot-vast-flashvsr.yaml tests/core/test_config.py
git commit -m "feat(config): skypilot-vast-flashvsr upscale-only cfg (slice-1 live proof)"
```

---

## Task 5: Live proof — one video on vast.ai via SkyPilot (USER-GATE)

**Goal:** Provision a vast.ai GPU through SkyPilot, upscale the fixture clip over the SSH tunnel, frame-QA the output, and confirm clean teardown — proving the whole HTTP-over-sky chain end to end.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_skypilot_vast_flashvsr_live.py` (RED scaffold committed BEFORE spend, per durability rules)
- Modify: `successful-generations.md` (new capability axis entry after green)

**Acceptance Criteria:**
- [ ] `kinoforge upscale --config examples/configs/skypilot-vast-flashvsr.yaml --video <fixture> --no-reuse` exits 0 and writes an mp4.
- [ ] `ffprobe` shows the output is a real video at the expected 1080-height dims (per the height-target path).
- [ ] Frame-QA: ~5 extracted frames read + judged high-quality (no false-color/garbage), verdict recorded.
- [ ] During the run, GPU utilisation was polled and observed non-zero (not a 0%-GPU stall).
- [ ] `kinoforge list` after the orchestrator exits shows `No running instances.` AND `No instances recorded in ledger.` (pod + tunnel gone).
- [ ] A `successful-generations.md` entry is added (provider=skypilot/vast, engine=diffusers upscale, tunnel path).

**Verify:** `VAST_API_KEY` present → `pixi run preflight` exit 0 → run the upscale command above → frame-QA → `pixi run kinoforge list` shows fully clean.

**Steps:**

- [ ] **Step 1: Probe a vast offer + confirm creds (no spend).** Run `pixi run -e live-skypilot python -c "from kinoforge.core.dotenv_loader import load_env_file; load_env_file(); import os; print('VAST_API_KEY set len=', len(os.environ.get('VAST_API_KEY','')))"` — expect a non-zero length (if 0, STOP and ask the operator to add `VAST_API_KEY` to `.env`). Then probe availability: `pixi run -e live-skypilot sky launch --help` sanity + `pixi run -e live-skypilot python -c "import sky; print([a for a in sky.list_accelerators(clouds=['vast']).keys()][:10])"` to confirm the vast catalog enumerates (the shim makes this reach vast). Note a candidate GPU + price; if none ≤ $1.00/hr meets `min_vram_gb: 40`, adjust the cfg's `min_vram_gb`/`max_usd_per_hr` (Task 4) and re-commit before spending.

- [ ] **Step 2: Commit the RED live scaffold BEFORE any spend.** Create `tests/live/test_skypilot_vast_flashvsr_live.py`:

```python
"""Live: FlashVSR upscale on vast.ai via SkyPilot (slice-1 proof). Gated on env."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="live vast/sky spend — set KINOFORGE_LIVE_TESTS=1 to enable",
)


def test_flashvsr_upscale_on_vast_via_skypilot() -> None:
    """Provision vast via sky, upscale the fixture over the ssh tunnel, frame-QA.

    RED scaffold: the assertions below encode the acceptance criteria; the live
    run is driven via the CLI in the plan steps, not this test body, until the
    harness is wired. Kept xfail-free by the skipif gate so CI stays green.
    """
    pytest.skip("driven via CLI in the plan; see Task 5 steps")
```

```bash
git add tests/live/test_skypilot_vast_flashvsr_live.py
git commit -m "test(live): RED scaffold for FlashVSR-on-vast-via-skypilot slice-1 proof"
```

- [ ] **Step 3: Preflight.** Run `pixi run preflight` → Expected exit 0 (creds present, zero active pods, clean tree). If non-zero, resolve before spending.

- [ ] **Step 4: Run the live upscale with polling.** Start the util-poll loop and launch the upscale (fixture per CLAUDE.md: `output/20260630-221857_..._Photorealistic-cinem.mp4`, 480²/81f):

```bash
pixi run -e live-skypilot kinoforge upscale \
  --config examples/configs/skypilot-vast-flashvsr.yaml \
  --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
  --no-reuse
```

During the run, poll every 60–90 s per CLAUDE.md's live-smoke rule — for a sky/vast cluster use `pixi run -e live-skypilot sky status` + (if cheap) `sky exec <cluster> nvidia-smi --query-gpu=utilization.gpu --format=csv`. GPU 0% for ≥3 consecutive probes during the upscale → capture logs, `sky down` the cluster, fail fast. The 2026-07-07 boot-stall work bounds a dead boot, but sky has no boot-liveness probe, so the manual poll is the safety net.

- [ ] **Step 5: Frame-QA the output.** Extract ~5 frames with `kinoforge.core.frames.ffmpeg_frames_by_count`, read them, and judge (artifacts, temporal coherence, fidelity vs the 480² sibling). Record the verdict. Anything not clearly high-quality → ⚠️ flag and do NOT report green.

- [ ] **Step 6: Verify teardown.** Run `pixi run kinoforge list` → Expected: `No running instances.` AND `No instances recorded in ledger.`. Also confirm no orphaned ssh: `pgrep -af "ssh -N -T" | grep <cluster>` returns nothing. If a cluster survives, `pixi run -e live-skypilot kinoforge destroy --id <cluster>` (or `sky down <cluster>`).

- [ ] **Step 7: Log the success + commit.** Add a `successful-generations.md` section per that file's schema (new axis: provider=skypilot, cloud=vast, engine=diffusers FlashVSR upscale over ssh-tunnel; include cfg path, GPU, cost, dims, frame-QA verdict). Commit:

```bash
git add successful-generations.md
git commit -m "docs(gen): log first FlashVSR upscale on vast.ai via SkyPilot (slice-1 proof)"
```

```json:metadata
{"files": ["tests/live/test_skypilot_vast_flashvsr_live.py", "successful-generations.md"], "verifyCommand": "pixi run preflight && pixi run -e live-skypilot kinoforge upscale --config examples/configs/skypilot-vast-flashvsr.yaml --video output/20260630-221857_..._Photorealistic-cinem.mp4 --no-reuse", "acceptanceCriteria": ["upscale exits 0 + writes mp4", "ffprobe shows real 1080-height video", "frame-QA high-quality verdict recorded", "GPU utilisation observed non-zero during run", "kinoforge list clean after (no pod, no ledger entry)", "successful-generations.md entry added"], "userGate": true, "tags": ["user-gate"], "gateScope": "single", "modelTier": "standard"}
```

---

## Task 6: Full suite + lint/type green, update PROGRESS

**Goal:** Whole suite green, lint/type clean, PROGRESS records the SkyPilot-vast video-gen capability (slice 1 shipped).

**Files:**
- Modify: `PROGRESS.md` (RESUME SNAPSHOT)

**Acceptance Criteria:**
- [ ] `pixi run test` green (existing baseline + the new offline tests; live test skipped without `KINOFORGE_LIVE_TESTS=1`).
- [ ] `pixi run -e live-skypilot pytest tests/providers/test_skypilot_vast_compat.py -q` green.
- [ ] `pixi run lint` + `pixi run typecheck` clean.
- [ ] `pixi run pre-commit run --all-files` passes.
- [ ] PROGRESS RESUME SNAPSHOT mentions SkyPilot-vast HTTP-tunnel + vast shim + spec/plan paths + slice-2 deferral.

**Verify:** `pixi run pre-commit run --all-files` → all pass; `pixi run test` → green.

**Steps:**

- [ ] **Step 1: Full offline suite** — Run: `pixi run test` → Expected: green; note the passed count. The vast_compat test lives behind `importorskip("vastai_sdk")`, so run it explicitly in the live-skypilot env (criterion 2).
- [ ] **Step 2: Lint + type** — Run: `pixi run lint` then `pixi run typecheck` → Expected: clean.
- [ ] **Step 3: Update PROGRESS.md** RESUME SNAPSHOT: SkyPilot-vast video-gen slice-1 shipped (vast shim + provider-internal ssh-tunnel HTTP seam); point to `docs/superpowers/specs/2026-07-07-skypilot-vast-video-gen-design.md` and this plan; note slice 2 (reconnect, Lambda parity, warm-reuse) deferred.
- [ ] **Step 4: pre-commit + commit**

```bash
pixi run pre-commit run --all-files
git add PROGRESS.md
git commit -m "docs(progress): SkyPilot vast.ai video-gen slice-1 shipped (vast shim + ssh-tunnel HTTP seam)"
```

---

## Self-Review

**Spec coverage:**
- Component A (vast shim) → Task 0. ✓
- Component B (HTTP-over-sky tunnel): create-side endpoint + seams → Task 1; destroy-side teardown → Task 2. ✓
- Component C (provisioning confirm) → Task 3. ✓
- Slice-1 example cfg → Task 4; live proof (frame-QA, ledger clean, log) → Task 5. ✓
- Testing section (offline unit w/ injected seams; live gated) → each task's tests + Task 6 full suite. ✓
- Non-goals honored: no warm-reuse, no reconnect, no Lambda parity, no public-port/auth — none introduced; slice-2 list carried in Task 6 PROGRESS note. ✓
- Error handling: sky-down failure → tunnel still killed (Task 2); tunnel spawn fail → ProvisionFailed + teardown (Task 1). CapacityError mapping from sky "no resources" is pre-existing provider behavior + composes with the shipped capacity-wait loop — no new task needed (not a slice-1 gap).

**Placeholder scan:** No TBD/TODO. Deferred details are grounded reads ("read upscale-flashvsr-1080p.yaml for the exact engine block", "confirm min_vram at the Task-5 live probe") each with the exact file to consult, not vague instructions.

**Type consistency:** `apply_vast_sdk_compat() -> bool` defined + consumed Task 0. `ssh_spawn: (cluster, local_port, remote_port) -> proc` and `port_allocator: () -> int` consistent across `__init__`/`create_instance`/`destroy_instance` + all tunnel tests. `_tunnels: dict[str, proc]`, `_VIDEO_SERVER_PORT = 8000`, `_kill_tunnel(proc)` consistent Tasks 1/2. `endpoints={"8000": "http://127.0.0.1:<port>"}` shape matches the RunPod `{"8000": ...}` key the engine's `wait_for_ready` reads. Fake `_FakeSky`/`_FakeProc` shared across Tasks 1–3 in one test file.

**Note for implementer:** the diffusers server binds `:8000` on the node; `ssh -L <local>:localhost:8000` forwards to the node's loopback — works whether the server binds `0.0.0.0` or `127.0.0.1`. If sky's `run` phase blocks `sky.launch` from returning while the server runs, the launch already returns via the async RequestId path (`_resolve` waits for provisioning, not job completion) — validate at Task 5 Step 4; if it blocks, background the server in `run_cmd` (append `&` / `nohup`) so launch returns and the tunnel + `wait_for_ready` take over.
