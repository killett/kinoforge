# Compute Seam S5 — One Endpoint Shape, One Durable-Write Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an endpoint a live, port-keyed, absolute URL on every provider — repaired on demand
rather than replayed from a recording — and make the pre-launch durable ledger row a property of
every launch instead of a SkyPilot-only fix.

**Architecture:** Stage 5 of 5 from
`docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §9, plus the two follow-ups
S4 recorded rather than closed. Three parts, landing in that order. **Part A (Tasks 0–3)** deletes
`_VIDEO_SERVER_PORT` and the `{"ssh": …}` shape, gives SkyPilot one tunnel per declared port, adds
`ComputeProvider.ensure_endpoints()` as the repairing sibling of the pure `endpoints()`, and flips
warm-attach to ask the provider first (F11). **Part B (Tasks 4–6)** lifts Brief 1's pre-launch
provisional row out of the SkyPilot provider into one orchestrator-level writer covering every
provider, and makes the reconciler safe for a row keyed by a client-side id (F12). **Part C
(Tasks 7–8)** enriches SkyPilot's instance tags so `RateCapExceeded` names the SKU, and adds a
pre-launch `sky.optimize()` estimate so an over-cap SkyPilot launch is refused before `Task.setup`
runs rather than after. **Tasks 9–10** are the RED live scaffold and the live proof.

**Tech Stack:** Python 3.13, pydantic v2, pytest, pixi, ruff, mypy. Providers: runpod (GraphQL),
skypilot (SDK, pinned `skypilot-0.12.3.post1`), modal (SDK), local. Engines: diffusers, comfyui,
fake.

---

## FOUR THINGS THE DESIGN GETS WRONG — READ THIS FIRST

All four were verified against HEAD (`9e80470c`) while writing this plan. Implementing §9 literally
would leak ssh processes on every status read, and would let the reconciler delete the exact row
F12 exists to keep.

### 1. `endpoints()` cannot become tunnel-ensuring — not for every caller

§9 says "SkyPilot's `endpoints()` becomes tunnel-ensuring". `endpoints()` has five callers, and
three of them are **observational**:

| Site | Caller | Wants |
|---|---|---|
| `cli/_commands.py:2184` | `kinoforge status` | to PRINT what is reachable |
| `cli/_commands.py:2332` | `kinoforge pod lora ls` | a base URL to talk to |
| `cli/_commands.py:1777` | `--attach-pod` | a base URL to talk to |
| `cli/_commands.py:2030` | warm-attach | a base URL to talk to |
| `core/orchestrator.py:1822` | `deploy_session` | a base URL to talk to |

A tunnel-ensuring `endpoints()` means `kinoforge status` on a skypilot cluster spawns an
`ssh -N -T -L` subprocess as a side effect of a READ, once per status call, each living until the
CLI process exits — and it makes an offline status read fail when ssh cannot reach the cluster.

**This plan splits the two.** `endpoints()` stays pure (report what this process already holds);
`ensure_endpoints()` is the repairing sibling, defaulting on the ABC to `self.endpoints(instance)`
so runpod / modal / local need no change at all. Serving callers move to `ensure_endpoints`;
observational callers stay on `endpoints`. §9's actual requirement — "ask the provider for a live
endpoint rather than replaying a recorded one" — is met by Task 3, and met better, because the
provider is asked on the path that is about to make HTTP requests and nowhere else.

**No new `Capability` member for this.** The ABC default IS the declaration: every provider has the
method, one overrides it. A capability enum here would be a third mechanism for a fact the type
system already carries, which is what §13 already complains about for `EPHEMERAL_CAPABILITIES`.

### 2. A generalised provisional row is keyed by an id that is not a destroyable id

On SkyPilot the client-side id and the real id are the same string (`cluster_name = spec.run_id`),
which is why the provider-level version worked. Generalising it does not carry that property:

| Provider | provisional row id | real instance id |
|---|---|---|
| skypilot | `run_id` | `run_id` — same |
| runpod | `run_id` (the pod **name**, `providers/runpod/__init__.py:1050`) | server-assigned pod id |
| modal | `run_id` | `eph-…` / app-derived id |

`cli/_reconcile.py:82` calls `provider.get_instance(pid)` and forgets the row on `KeyError`. Handed
a RunPod pod NAME it queries a pod id that does not exist, gets `KeyError`, and **forgets the
row** — deleting the only durable handle on a pod that may well have been created. That is worse
than the F12 hole it was meant to close: today there is no row, after a naive generalisation there
is a row that the next `kinoforge list` erases.

Task 6 makes the reconciler adopt-by-name for `kf_launch_phase=launching` rows before it is allowed
to forget one.

### 3. `sky.optimize` is a client/server call returning a RequestId, not a local estimator

At the pinned 0.12.3, `sky.optimize` is `sky.client.sdk.optimize(dag) -> RequestId['sky.Dag']`
(`sky/client/sdk.py:411`) — it POSTs `/optimize` and the answer comes back through the same
request-resolution path `create_instance` already uses for `sky.launch`. The cost then comes off
the optimized task's `best_resources.get_cost(3600.0)` (`sky/resources.py:1704`).

So the estimate is a **network call that can fail**, and Task 8 treats it as best-effort: an
estimate over the cap refuses the launch, an unreadable estimate WARNs and proceeds. The post-launch
readback shipped in S4 is what makes the cap true; the estimate only decides how much work gets
thrown away.

### 4. Moving the provisional writer moves the golden harness's only SkyPilot `Instance`

`tools/snapshot_launch_payloads.py:109` `_RecordingLedger` exists to capture the provider's
provisional row, because `_StopLaunch` aborts inside `sky.launch` and SkyPilot builds no other
Instance before that. `tests/providers/test_field_consumption_parity.py:873-875` proves `tags` is
consumed on skypilot by observing exactly that row.

Delete the provider-level writer without touching the harness and the parity guard loses its only
observation point for a portable field. Task 5 moves the harness onto the new orchestrator-level
writer in the same commit — not as a follow-up.

---

**Global Constraints:**
- **The golden ratchet is the measure.** All 31 payload goldens in
  `tests/providers/golden/launch_payloads/` stay byte-identical through every task of this plan.
  S5 changes what is RECORDED and what is REPAIRED, not what is SENT. A moved golden in this stage
  is a bug until proven otherwise, and if one is genuinely intended it moves once, in its own
  commit, with a decoded diff reviewed — never regenerated wholesale.
- **A read must not create a resource.** No observational command (`status`, `list`, `doctor`) may
  spawn a tunnel, open a socket, or write a row as a side effect.
- **Bookkeeping must never fail a launch.** Every provisional-row write/forget is wrapped and
  logged; a store fault forfeits F12 protection for that launch and nothing else. This is the
  existing discipline at `providers/skypilot/__init__.py:1051-1070` and it carries over verbatim.
- **The ledger row is a hint, never an authority, for endpoints.** A provider that can answer wins;
  the recording is the fallback (Modal's non-rebuildable `.modal.run` URL is why the fallback
  stays — commit `1cb4299`).
- **Additive before subtractive.** `set_launch_ledger` keeps working until Task 5 deletes it.
- `kinoforge.core.*` must not import `kinoforge.providers.*` at module scope.
- Google-style docstrings, full type hints, Conventional Commits in imperative mood, never
  `--no-verify`, `rg` not `grep`, pixi for everything, local timezone (`datetime.now()`).
- Live spend: `pixi run preflight` first, RED scaffold committed BEFORE the spend, `--no-reuse`
  semantics (the smoke tears down through S1's imported `_teardown`), utilisation polled on a
  60–90 s cadence, and `kinoforge list` + `sky status` verified clean AFTER the process exits.

**User decisions (already made):**
- "Write the S5 plan" — the stage scope is design §9 plus the two S4-recorded follow-ups
  (`PROGRESS.md` RESUME SNAPSHOT, 2026-09-01), and nothing else.
- Live smokes are pre-authorised up to the standing session budget; run them without a
  confirmation handshake.
- The open S1–S3 line items NOT carried into S5 (RunPod/Modal `region` wiring, the 11 ungated
  `tests/live` modules, `disk_gb`, the non-recursive golden glob) stay open and are re-recorded in
  Task 10's PROGRESS update rather than silently absorbed here.

---

## Repo orientation for the implementer

Files this plan touches, and what each is responsible for:

| File | Responsibility here |
|---|---|
| `src/kinoforge/core/interfaces.py` | `ComputeProvider` ABC — gains `ensure_endpoints` (concrete default) |
| `src/kinoforge/providers/skypilot/__init__.py` | tunnels, endpoints, provisional row (deleted), tags, pre-launch estimate |
| `src/kinoforge/core/orchestrator.py` | the generalised provisional-row writer, and which callers ask for a live endpoint |
| `src/kinoforge/cli/_commands.py` | status output, warm-attach endpoint resolution |
| `src/kinoforge/cli/_reconcile.py` | adopt-or-forget for `launching` rows |
| `src/kinoforge/providers/runpod/__init__.py` | `_LIST_PODS_QUERY` gains `name`, plus a name lookup |
| `tools/snapshot_launch_payloads.py` | golden harness — follows the writer that moved |
| `tests/providers/test_field_consumption_parity.py` | parity guard — follows the same move |

Commands (never bare `pytest` / `python`):

```bash
pixi run test
pixi run lint
pixi run typecheck
pixi run pre-commit run --all-files
```

SkyPilot code runs only in the `live-skypilot` env:

```bash
pixi run -e live-skypilot pytest tests/live/test_compute_seam_s5_smoke.py -v
```

---

## Task 0: SkyPilot endpoints become port-keyed, one tunnel per declared port

**Goal:** Delete `_VIDEO_SERVER_PORT` and forward every port the engine declared, so
`instance.endpoints` is `{"8000": …, "8001": …}` for diffusers rather than one hardcoded 8000.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py:330` (delete `_VIDEO_SERVER_PORT`),
  `:744` (`_tunnels` type), `:1078-1099` (create-time tunnel block), `:1124-1137` (returned
  `Instance`), `:1242-1274` (`destroy_instance` tunnel kill)
- Test: `tests/providers/test_skypilot_endpoints.py` (create)

**Acceptance Criteria:**
- [ ] A spec declaring `ports=("8000", "8001")` with a `launch` produces endpoints with BOTH keys,
      each an absolute `http://127.0.0.1:<port>` URL, and the two local ports differ.
- [ ] The remote port each tunnel forwards to equals the declared port (8001 forwards to 8001, not
      to 8000).
- [ ] The returned `Instance.tags["ports"]` is `"8000,8001"` — the same tag key RunPod uses
      (`providers/runpod/__init__.py:993`), so one reader can serve both providers.
- [ ] A spec with `launch=None` opens no tunnel and returns `{}` — unchanged behaviour for the CPU
      smoke config.
- [ ] `destroy_instance` kills EVERY tunnel for the cluster, including when `sky.down` raises.
- [ ] A spawn failure on the second port tears down the first tunnel AND the cluster, then raises
      `ProvisionFailed` — no half-tunnelled cluster is returned.
- [ ] `rg -n "_VIDEO_SERVER_PORT" src/` returns nothing.
- [ ] All 31 launch-payload goldens unchanged.

**Verify:** `pixi run pytest tests/providers/test_skypilot_endpoints.py tests/providers/test_skypilot.py tests/providers/test_launch_payload_goldens.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/test_skypilot_endpoints.py`:

```python
"""Behavior: SkyPilot forwards every declared port and says so in one shape.

The bug this pins: the provider hardcoded a single remote port 8000
(``_VIDEO_SERVER_PORT``), so an engine declaring ``["8000", "8001"]`` — which
diffusers does, ``engines/diffusers/__init__.py:1282`` — got a working video
server and an unreachable /tmp file server. The bootstrap-log fetch the
project's own live-smoke rule depends on was simply not available over sky.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import Instance, InstanceSpec, Launch
from kinoforge.providers.skypilot import SkyPilotProvider


class _FakeProc:
    """Stand-in for an ssh tunnel subprocess."""

    def __init__(self) -> None:
        self.terminated = False
        self._rc: int | None = None

    def poll(self) -> int | None:
        return self._rc

    def die(self) -> None:
        self._rc = 255

    def terminate(self) -> None:
        self.terminated = True


class _FakeSky:
    """Minimal sky stand-in: records launches, answers status/down."""

    class Task:
        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> dict[str, Any]:
            return config

    def __init__(self) -> None:
        self.downed: list[str] = []

    def launch(self, task: Any, **kwargs: Any) -> None:
        return None

    def status(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def down(self, name: str) -> None:
        self.downed.append(name)


def _spec(*, ports: tuple[str, ...], launch: Launch | None) -> InstanceSpec:
    return InstanceSpec(
        image="docker:kf/probe:latest",
        ports=ports,
        run_id="kf-endpoints-probe",
        launch=launch,
    )


def _provider(sky: _FakeSky, spawns: list[tuple[str, int, int]]) -> SkyPilotProvider:
    ports = iter([50001, 50002, 50003, 50004])

    def _spawn(cluster: str, local_port: int, remote_port: int) -> _FakeProc:
        spawns.append((cluster, local_port, remote_port))
        return _FakeProc()

    return SkyPilotProvider(
        sky,
        ssh_spawn=_spawn,
        port_allocator=lambda: next(ports),
        sleep=lambda _s: None,
    )


def test_every_declared_port_gets_its_own_forward() -> None:
    """Two declared ports produce two endpoints and two distinct local ports.

    Bug caught: a provider that forwards only the first port (or forwards both
    local ports to remote 8000) silently drops the second service.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)

    inst = provider.create_instance(
        _spec(ports=("8000", "8001"), launch=Launch(("python", "-m", "srv")))
    )

    assert inst.endpoints == {
        "8000": "http://127.0.0.1:50001",
        "8001": "http://127.0.0.1:50002",
    }
    assert [(c, r) for c, _l, r in spawns] == [
        ("kf-endpoints-probe", 8000),
        ("kf-endpoints-probe", 8001),
    ]


def test_ports_land_on_the_instance_tags() -> None:
    """The port list survives on the Instance under the RunPod tag key.

    Bug caught: ``ensure_endpoints`` (Task 1) and the warm-attach path have no
    way to know which ports to re-forward for a cluster they did not launch.
    """
    provider = _provider(_FakeSky(), [])
    inst = provider.create_instance(
        _spec(ports=("8000", "8001"), launch=Launch(("python", "-m", "srv")))
    )
    assert inst.tags["ports"] == "8000,8001"


def test_no_launch_means_no_tunnel() -> None:
    """A spec with no launch opens nothing — the CPU smoke path.

    Bug caught: tunnelling a cluster with no server makes every CPU-only
    launch depend on ssh reachability it never needed.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    inst = provider.create_instance(_spec(ports=("8000",), launch=None))
    assert inst.endpoints == {}
    assert spawns == []


def test_second_spawn_failure_kills_the_first_tunnel_and_the_cluster() -> None:
    """A partial tunnel set is never returned.

    Bug caught: port 8000 forwards, 8001 fails, and the provider returns a
    cluster that is billing with an endpoint map the engine will half-use.
    """
    sky = _FakeSky()
    made: list[_FakeProc] = []

    def _spawn(cluster: str, local_port: int, remote_port: int) -> _FakeProc:
        if remote_port == 8001:
            raise OSError("ssh: connect failed")
        proc = _FakeProc()
        made.append(proc)
        return proc

    provider = SkyPilotProvider(
        sky,
        ssh_spawn=_spawn,
        port_allocator=lambda: 50001,
        sleep=lambda _s: None,
    )

    from kinoforge.core.errors import ProvisionFailed

    with pytest.raises(ProvisionFailed, match="8001"):
        provider.create_instance(
            _spec(ports=("8000", "8001"), launch=Launch(("python", "-m", "srv")))
        )
    assert sky.downed == ["kf-endpoints-probe"]
    assert made and all(p.terminated for p in made)


def test_destroy_kills_every_tunnel_even_when_down_raises() -> None:
    """Teardown never leaks a port-forward.

    Bug caught: ``sky.down`` raising leaves N ssh processes holding local
    ports for the rest of the session.
    """

    class _AngrySky(_FakeSky):
        def down(self, name: str) -> None:
            raise RuntimeError("sky is unhappy")

    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_AngrySky(), spawns)
    inst = provider.create_instance(
        _spec(ports=("8000", "8001"), launch=Launch(("python", "-m", "srv")))
    )
    procs = [t.proc for t in provider._tunnels[inst.id].values()]  # noqa: SLF001

    with pytest.raises(RuntimeError):
        provider.destroy_instance(inst.id)

    assert all(p.terminated for p in procs)
    assert provider._tunnels.get(inst.id) is None  # noqa: SLF001
```

Check the constructor keyword names before running (`SkyPilotProvider.__init__` around
`providers/skypilot/__init__.py:700-760`) and adjust `ssh_spawn` / `port_allocator` / `sleep` to
the names actually declared — the seams exist, this test must use their real spelling.

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `pixi run pytest tests/providers/test_skypilot_endpoints.py -v`
Expected: FAIL — `test_every_declared_port_gets_its_own_forward` reports one endpoint, and
`provider._tunnels[inst.id]` is a bare process, not a mapping.

- [ ] **Step 3: Implement**

In `src/kinoforge/providers/skypilot/__init__.py`, delete `_VIDEO_SERVER_PORT` and add:

```python
@dataclasses.dataclass
class _Tunnel:
    """One live ssh port-forward.

    Attributes:
        proc: The ``ssh -N -T -L`` subprocess.
        local_port: The localhost port the forward is bound to.
    """

    proc: Any
    local_port: int
```

Change the map at `:744` to `self._tunnels: dict[str, dict[str, _Tunnel]] = {}` (cluster → remote
port → tunnel), and replace the create-time block at `:1078-1099`:

```python
        endpoints: dict[str, str] = {}
        # Only a server spec (one that declares a long-running launch) needs
        # HTTP tunnels; a server-less deploy (CPU smoke) gets none. S5: one
        # tunnel per DECLARED port, because an engine declaring ["8000",
        # "8001"] means both are load-bearing — 8001 is the /tmp file server
        # the project's own live-smoke rule fetches bootstrap.log from.
        if spec.launch is not None:
            opened: dict[str, _Tunnel] = {}
            try:
                for port in spec.ports:
                    local_port = self._alloc_port()
                    proc = self._ssh_spawn(cluster_name, local_port, int(port))
                    opened[port] = _Tunnel(proc=proc, local_port=local_port)
            except Exception as exc:  # noqa: BLE001 — any spawn fault → clean fail
                for tunnel in opened.values():
                    self._kill_tunnel(tunnel.proc)
                # Best-effort teardown so a live-but-unreachable cluster is not
                # left billing while we raise.
                try:
                    _resolve(sky, sky.down(cluster_name))
                except Exception:  # noqa: BLE001, S110
                    pass
                raise ProvisionFailed(
                    f"failed to open ssh tunnel to {cluster_name!r}: {exc}"
                ) from exc
            self._tunnels[cluster_name] = opened
            endpoints = {
                port: f"http://127.0.0.1:{tunnel.local_port}"
                for port, tunnel in opened.items()
            }
```

The failure message must name the port that failed — put the port in the raised text:
`f"failed to open ssh tunnel to {cluster_name!r} for port {port}: {exc}"` (bind `port` in the loop
so it is in scope for the handler; initialise `port = ""` before the `try`).

On the returned `Instance` (`:1124`), record the port list beside the spec tags:

```python
            tags={
                **dict(spec.tags),
                # S5 — same key RunPod uses (_pod_to_instance / endpoints), so
                # ensure_endpoints and warm-attach have one reader for both.
                "ports": ",".join(spec.ports),
            },
```

In `destroy_instance` (`:1255`), pop the whole mapping and kill each:

```python
        tunnels = self._tunnels.pop(instance_id, None) or {}
        try:
            ...unchanged...
        finally:
            for tunnel in tunnels.values():
                self._kill_tunnel(tunnel.proc)
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `pixi run pytest tests/providers/test_skypilot_endpoints.py tests/providers/test_skypilot.py -v`
Expected: PASS. Existing `test_skypilot.py` tunnel tests may reference `self._tunnels[name]` as a
process — update those references to `.[port].proc`; do not weaken an assertion to make it pass.

- [ ] **Step 5: Prove the goldens did not move**

Run: `pixi run pytest tests/providers/test_launch_payload_goldens.py -q`
Expected: PASS, 31 goldens byte-identical. The capture aborts inside `sky.launch`, so nothing here
reaches the wire.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py \
        tests/providers/test_skypilot_endpoints.py \
        tests/providers/test_skypilot.py
git commit -m "feat(skypilot): forward every declared port, not a hardcoded 8000"
```

---

## Task 1: `ensure_endpoints` — the repairing sibling of a pure `endpoints`

**Goal:** Add `ComputeProvider.ensure_endpoints()` with an ABC default of `self.endpoints(instance)`,
override it on SkyPilot to re-establish dead or absent tunnels, and delete the `{"ssh": …}` shape.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:562-564` (ABC)
- Modify: `src/kinoforge/providers/skypilot/__init__.py:1298-1307` (`endpoints`, plus the new
  `ensure_endpoints` and `_ensure_tunnel`)
- Test: `tests/providers/test_skypilot_endpoints.py` (extend), `tests/core/test_provider_abc.py`
  (extend or create)

**Acceptance Criteria:**
- [ ] `ComputeProvider.ensure_endpoints(instance)` is a CONCRETE method returning
      `self.endpoints(instance)`; runpod, modal and local inherit it with zero code change, proven
      by a test that calls it on each and compares to `endpoints`.
- [ ] `SkyPilotProvider.endpoints(instance)` returns the live map for tunnels THIS process owns and
      `{}` otherwise — it never returns `ssh://…`, and it never spawns anything.
- [ ] `SkyPilotProvider.ensure_endpoints(instance)` re-spawns a tunnel whose process has exited
      (`proc.poll() is not None`) and returns the NEW local port.
- [ ] `ensure_endpoints` reuses a live tunnel: no second spawn, same port back.
- [ ] `ensure_endpoints` on a cluster this process never launched reads the port list from
      `instance.tags["ports"]`, falling back to the numeric keys of `instance.endpoints` when the
      tag is absent (a row recorded before this change).
- [ ] With neither source of ports, `ensure_endpoints` returns `{}` and logs a warning — it does not
      guess 8000.
- [ ] `rg -n '"ssh"' src/kinoforge/providers/skypilot/__init__.py` matches only the argv of
      `_spawn_ssh_tunnel`.

**Verify:** `pixi run pytest tests/providers/test_skypilot_endpoints.py tests/core/test_provider_abc.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_skypilot_endpoints.py`:

```python
def test_endpoints_is_pure_and_reports_only_live_local_state() -> None:
    """A read never spawns and never lies about a cluster it does not hold.

    Bug caught: returning ``{"ssh": "ssh://<cluster>"}`` — an HTTP client
    handed that string fails with an unusable error (finding F11), and a
    status read that spawns ssh leaks a process per invocation.
    """
    provider = _provider(_FakeSky(), [])
    stranger = Instance(
        id="kf-not-ours",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={"ports": "8000"},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.endpoints(stranger) == {}


def test_ensure_endpoints_respawns_a_dead_tunnel_on_a_fresh_port() -> None:
    """A dead forward is repaired, not replayed.

    Bug caught: warm-attach replays a recorded 127.0.0.1:<port> whose ssh
    process died with the launching CLI, and every engine request connects to
    nothing (finding F11, ledger branch).
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    inst = provider.create_instance(
        _spec(ports=("8000",), launch=Launch(("python", "-m", "srv")))
    )
    assert inst.endpoints == {"8000": "http://127.0.0.1:50001"}

    provider._tunnels[inst.id]["8000"].proc.die()  # noqa: SLF001

    refreshed = provider.ensure_endpoints(inst)
    assert refreshed == {"8000": "http://127.0.0.1:50002"}
    assert len(spawns) == 2


def test_ensure_endpoints_reuses_a_live_tunnel() -> None:
    """A healthy forward is not churned.

    Bug caught: an ``ensure`` that respawns unconditionally burns a local port
    per call and drops in-flight requests on a working pod.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    inst = provider.create_instance(
        _spec(ports=("8000",), launch=Launch(("python", "-m", "srv")))
    )
    assert provider.ensure_endpoints(inst) == inst.endpoints
    assert len(spawns) == 1


def test_ensure_endpoints_rebuilds_from_the_ports_tag_in_a_fresh_process() -> None:
    """A cross-process warm attach re-forwards from what the ledger recorded.

    Bug caught: the second CLI process holds no tunnel map, so without a port
    list it can only return {} and the warm attach fails with "has no
    endpoints" (the 2026-07-12 Modal-shaped failure, skypilot flavour).
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    recorded = Instance(
        id="kf-warm",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://127.0.0.1:1"},  # a dead port from last run
        tags={"ports": "8000,8001"},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.ensure_endpoints(recorded) == {
        "8000": "http://127.0.0.1:50001",
        "8001": "http://127.0.0.1:50002",
    }
    assert [(c, r) for c, _l, r in spawns] == [("kf-warm", 8000), ("kf-warm", 8001)]


def test_ensure_endpoints_without_a_port_list_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ports known → say nothing, rather than guessing 8000.

    Bug caught: a guessed port produces a URL that connects to whatever else
    is listening locally, which is worse than an empty map the caller can
    report honestly.
    """
    provider = _provider(_FakeSky(), [])
    bare = Instance(
        id="kf-bare",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    with caplog.at_level("WARNING"):
        assert provider.ensure_endpoints(bare) == {}
    assert "kf-bare" in caplog.text
```

Create `tests/core/test_provider_abc.py` (or extend the existing ABC test module if one exists —
check `rg -l "ComputeProvider" tests/core` first):

```python
"""Behavior: ensure_endpoints defaults to endpoints on every provider but skypilot."""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.providers.local import LocalProvider


def test_default_ensure_endpoints_is_the_plain_read() -> None:
    """A provider with nothing to repair answers identically through both doors.

    Bug caught: making ``ensure_endpoints`` abstract would force runpod, modal
    and local to write an identical passthrough, and one of them would
    eventually drift from ``endpoints``.
    """
    provider = LocalProvider()
    inst = Instance(
        id="local-1",
        provider="local",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.ensure_endpoints(inst) == provider.endpoints(inst)
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `pixi run pytest tests/providers/test_skypilot_endpoints.py tests/core/test_provider_abc.py -v`
Expected: FAIL with `AttributeError: 'SkyPilotProvider' object has no attribute 'ensure_endpoints'`
and `test_endpoints_is_pure...` returning `{"ssh": "ssh://kf-not-ours"}`.

- [ ] **Step 3: Implement the ABC default**

In `src/kinoforge/core/interfaces.py`, directly below the abstract `endpoints`:

```python
    @abstractmethod
    def endpoints(self, instance: Instance) -> dict[str, str]: ...  # noqa: D102

    def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
        """Return endpoints, repairing any provider-side plumbing first.

        compute-seam S5. ``endpoints`` is a pure read: it reports what is
        already reachable and never creates anything, because three of its
        callers are observational (``kinoforge status``, the instance
        overview, ``doctor``) and a read that spawns an ssh process per
        invocation is a leak, not a feature.

        ``ensure_endpoints`` is the door for callers that are about to make
        requests. The default is the plain read — correct for every provider
        whose URL is a pure function of the instance (RunPod's proxy hostname,
        Modal's recorded ``.modal.run`` URL, local's scheme URL). SkyPilot
        overrides it because its endpoint is a local port held open by a
        subprocess that does not survive the process that launched it
        (finding F11).

        Args:
            instance: The instance whose endpoints are needed.

        Returns:
            A port-keyed map of absolute URLs; ``{}`` when none can be
            established.
        """
        return self.endpoints(instance)
```

- [ ] **Step 4: Implement the SkyPilot override**

Replace `SkyPilotProvider.endpoints` (`:1298-1307`) and add the ensure path:

```python
    def endpoints(self, instance: Instance) -> dict[str, str]:
        """Return the live local URLs for tunnels THIS process holds.

        Pure by contract: no spawn, no network. A cluster launched by another
        process (a warm attach) yields ``{}`` here — ``kinoforge status``
        prints the cluster name for those, and a caller that needs to talk to
        the cluster calls :meth:`ensure_endpoints` instead.

        Args:
            instance: The cluster whose endpoints to report.

        Returns:
            ``{"8000": "http://127.0.0.1:53411", ...}``, or ``{}``.
        """
        return {
            port: f"http://127.0.0.1:{tunnel.local_port}"
            for port, tunnel in (self._tunnels.get(instance.id) or {}).items()
            if self._tunnel_alive(tunnel)
        }

    def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
        """Re-establish any missing or dead forward, then report the map.

        This is the actual fix for F11's warm-attach hazard: the ledger branch
        replayed a dead local port and the fallback handed an ``ssh://`` URL to
        an HTTP client. Both are cured by asking the provider for a live
        endpoint rather than replaying a recorded one.

        Args:
            instance: The cluster to reach.

        Returns:
            A port-keyed map of absolute local URLs; ``{}`` when the port list
            is unknown.
        """
        ports = self._ports_for(instance)
        if not ports:
            logger.warning(
                "skypilot: no port list for cluster %r (tags['ports'] absent "
                "and no numeric endpoint keys recorded); cannot establish a "
                "tunnel",
                instance.id,
            )
            return {}
        return {port: f"http://127.0.0.1:{self._ensure_tunnel(instance.id, port)}"
                for port in ports}

    @staticmethod
    def _tunnel_alive(tunnel: _Tunnel) -> bool:
        """True while the forward's subprocess is still running."""
        try:
            return tunnel.proc.poll() is None
        except Exception:  # noqa: BLE001 — an unpollable handle is a dead one
            return False

    @staticmethod
    def _ports_for(instance: Instance) -> tuple[str, ...]:
        """Return the remote ports to forward for *instance*.

        Prefers ``tags["ports"]`` (written at create time since S5); falls back
        to the numeric keys of a recorded endpoint map, which is what a ledger
        row written before S5 carries.

        Args:
            instance: The cluster in question.

        Returns:
            Declared ports in order, or an empty tuple.
        """
        raw = str(instance.tags.get("ports", ""))
        tagged = tuple(p.strip() for p in raw.split(",") if p.strip())
        if tagged:
            return tagged
        return tuple(k for k in instance.endpoints if k.isdigit())

    def _ensure_tunnel(self, cluster_name: str, port: str) -> int:
        """Return a live local port forwarding to ``port`` on the cluster.

        Reuses an existing forward when its subprocess is still running;
        otherwise reaps the dead one and spawns a replacement.

        Args:
            cluster_name: The cluster to forward to.
            port: The remote port.

        Returns:
            The local port.
        """
        held = self._tunnels.setdefault(cluster_name, {})
        existing = held.get(port)
        if existing is not None and self._tunnel_alive(existing):
            return existing.local_port
        if existing is not None:
            self._kill_tunnel(existing.proc)
        local_port = self._alloc_port()
        held[port] = _Tunnel(
            proc=self._ssh_spawn(cluster_name, local_port, int(port)),
            local_port=local_port,
        )
        return local_port
```

Refactor Task 0's create-time loop to call `self._ensure_tunnel(cluster_name, port)` so there is one
spawn site, keeping the partial-failure teardown in the create path.

- [ ] **Step 5: Run the tests, confirm they pass**

Run: `pixi run pytest tests/providers/test_skypilot_endpoints.py tests/core/test_provider_abc.py tests/providers/test_skypilot.py -v`
Expected: PASS. Any existing test asserting `{"ssh": …}` is asserting the deleted behaviour — delete
that test and note the deletion in the commit body.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/interfaces.py \
        src/kinoforge/providers/skypilot/__init__.py \
        tests/providers/test_skypilot_endpoints.py \
        tests/core/test_provider_abc.py
git commit -m "feat(core): add ensure_endpoints and make skypilot repair its tunnels"
```

---

## Task 2: `kinoforge status` prints the cluster name; serving callers switch doors

**Goal:** Route every caller to the right door — observational sites keep `endpoints()`, sites that
are about to make HTTP requests call `ensure_endpoints()` — and give skypilot a status line that
says something true.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py:2184` (status endpoints block), `:2332`
  (`pod lora ls`), `:1777` (`--attach-pod`)
- Modify: `src/kinoforge/core/orchestrator.py:1822` (`deploy_session` result endpoints)
- Test: `tests/cli/test_status_endpoints.py` (create)

**Acceptance Criteria:**
- [ ] `kinoforge status` on a skypilot row whose tunnels this process does not hold prints
      `cluster=<name>` rather than `{}` or an `ssh://` URL, and spawns NO subprocess.
- [ ] `kinoforge status` on a RunPod row is unchanged — the proxy URL map still renders.
- [ ] `pod lora ls`, `--attach-pod` and `deploy_session` call `ensure_endpoints`.
- [ ] A test asserts the status path never calls `ensure_endpoints` (spy provider records calls).

**Verify:** `pixi run pytest tests/cli/test_status_endpoints.py tests/cli -k "status or endpoint" -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/cli/test_status_endpoints.py`:

```python
"""Behavior: status reads; it does not build.

The rule this pins: no observational command may create a resource. A
tunnel-ensuring ``kinoforge status`` would spawn one ssh subprocess per
invocation and fail offline — see the S5 plan, "four things the design gets
wrong", item 1.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Instance


class _SpyProvider:
    """Records which endpoint door was used."""

    name = "skypilot"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def endpoints(self, instance: Instance) -> dict[str, str]:
        self.calls.append("endpoints")
        return {}

    def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
        self.calls.append("ensure_endpoints")
        return {"8000": "http://127.0.0.1:50001"}


def test_status_uses_the_pure_read_and_names_the_cluster() -> None:
    """Status never repairs, and says what it can identify.

    Bug caught: printing ``{}`` for a live cluster tells the operator nothing,
    and printing ``ssh://<name>`` tells them something an HTTP client cannot
    use (finding F11).
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    provider = _SpyProvider()
    inst = Instance(
        id="kf-cluster-7",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    rendered = _render_endpoints_for_status(provider, inst)
    assert provider.calls == ["endpoints"]
    assert "kf-cluster-7" in rendered


def test_status_renders_a_real_endpoint_map_when_there_is_one() -> None:
    """A provider with endpoints still gets its JSON map.

    Bug caught: the skypilot special case swallowing RunPod's proxy URLs.
    """
    from kinoforge.cli._commands import _render_endpoints_for_status

    class _RunPodish(_SpyProvider):
        name = "runpod"

        def endpoints(self, instance: Instance) -> dict[str, str]:
            self.calls.append("endpoints")
            return {"8000": "https://abc-8000.proxy.runpod.net"}

    inst = Instance(
        id="abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={},
        tags={},
        cost_rate_usd_per_hr=0.0,
    )
    assert "proxy.runpod.net" in _render_endpoints_for_status(_RunPodish(), inst)
```

- [ ] **Step 2: Run it, confirm it fails**

Run: `pixi run pytest tests/cli/test_status_endpoints.py -v`
Expected: FAIL — `ImportError: cannot import name '_render_endpoints_for_status'`.

- [ ] **Step 3: Implement**

In `src/kinoforge/cli/_commands.py`, extract the status endpoint rendering into a named helper and
give an empty map the cluster identity:

```python
def _render_endpoints_for_status(provider: object, instance: Instance) -> str:
    """Render the status line's endpoint field without creating anything.

    compute-seam S5: ``endpoints`` is the pure read. A provider holding no
    live endpoint in THIS process (skypilot after a warm attach, always)
    yields the instance identity instead of an empty map — enough for the
    operator to run ``sky status`` or ``kinoforge destroy --id``.

    Args:
        provider: The resolved compute provider.
        instance: The instance being reported.

    Returns:
        A JSON endpoint map, ``cluster=<id>``, or ``unknown (<ExcName>)``.
    """
    try:
        mapping = provider.endpoints(instance)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return f"unknown ({exc.__class__.__name__})"
    if mapping:
        return json.dumps(mapping)
    return f"cluster={instance.id}"
```

Point `:2184` at it: `provider_block["endpoints"] = _render_endpoints_for_status(provider, instance)`.

At the three serving sites, switch the call:

- `:1777` → `live.endpoints = provider.ensure_endpoints(live)`
- `:2332` → `endpoints_map = provider.ensure_endpoints(instance)`
- `core/orchestrator.py:1822` → `endpoints = resolved_provider.ensure_endpoints(instance)`

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `pixi run pytest tests/cli/test_status_endpoints.py tests/cli tests/core/test_orchestrator*.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_commands.py src/kinoforge/core/orchestrator.py \
        tests/cli/test_status_endpoints.py
git commit -m "refactor(cli): read endpoints for status, ensure them for serving"
```

---

## Task 3: Warm-attach asks the provider first (F11)

**Goal:** Make the recorded ledger endpoint map a hint the provider may override, not a value the
engine trusts blind — closing the F11 hazard on both of its branches.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py:2020-2045` (the warm-attach endpoint block)
- Test: `tests/cli/test_warm_attach_endpoints.py` (create)

**Acceptance Criteria:**
- [ ] The warm-attach path seeds `instance.endpoints` from the ledger row FIRST, then calls
      `provider.ensure_endpoints(instance)`; a non-empty provider answer wins.
- [ ] Modal is unchanged in outcome: `ModalProvider.endpoints` falls back to `instance.endpoints`
      (`providers/modal/__init__.py:354-358`), so the recorded `.modal.run` URL still comes back —
      proven by a test, because reversing the seed order silently breaks commit `1cb4299`.
- [ ] A skypilot warm attach returns a FRESH local port, not the recorded dead one.
- [ ] A provider raising from `ensure_endpoints` falls back to the recorded map with a warning, and
      does not abort the attach.
- [ ] RunPod's ledger-tag merge (the `ports` recovery at `:2020`) still happens BEFORE the call.

**Verify:** `pixi run pytest tests/cli/test_warm_attach_endpoints.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_warm_attach_endpoints.py`:

```python
"""Behavior: a warm attach uses a LIVE endpoint, not a recorded one.

Finding F11: the ledger branch replayed a dead process's 127.0.0.1:<port> and
the fallback handed an ``ssh://`` URL to an HTTP client — both branches wrong
for skypilot. Both are cured by asking the provider.
"""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Instance


def _instance(provider: str, endpoints: dict[str, str]) -> Instance:
    return Instance(
        id="kf-warm-1",
        provider=provider,
        status="ready",
        created_at=0.0,
        endpoints=endpoints,
        tags={"ports": "8000"},
        cost_rate_usd_per_hr=0.0,
    )


def test_provider_answer_beats_the_recorded_row() -> None:
    """A live port replaces a recorded dead one.

    Bug caught: the exact F11 ledger branch — engine connects to a port whose
    ssh process died with the CLI that opened it.
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Sky:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            return {"8000": "http://127.0.0.1:60123"}

    resolved = _resolve_warm_endpoints(
        _Sky(), _instance("skypilot", {}), entry={"endpoints": {"8000": "http://127.0.0.1:1"}}
    )
    assert resolved == {"8000": "http://127.0.0.1:60123"}


def test_modal_recorded_url_survives_because_it_is_seeded_first() -> None:
    """Modal's non-rebuildable URL still replays (commit 1cb4299).

    Bug caught: calling the provider before seeding the recorded endpoints
    makes ModalProvider.endpoints fall back to an EMPTY map, and the warm
    attach dies with "has no endpoints".
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Modal:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            # Mirrors ModalProvider: no local deployment record in a fresh
            # process, so it echoes whatever the instance carries.
            return dict(instance.endpoints)

    url = "https://kinoforge-eph-8afe5ec6--srv.modal.run"
    resolved = _resolve_warm_endpoints(
        _Modal(), _instance("modal", {}), entry={"endpoints": {"8000": url}}
    )
    assert resolved == {"8000": url}


def test_provider_failure_falls_back_to_the_recording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A provider fault degrades to the old behaviour, loudly.

    Bug caught: an ensure_endpoints raising (ssh binary missing, creds
    expired) aborting a warm attach that the recorded URL could have served.
    """
    from kinoforge.cli._commands import _resolve_warm_endpoints

    class _Broken:
        def ensure_endpoints(self, instance: Instance) -> dict[str, str]:
            raise RuntimeError("ssh: command not found")

    resolved = _resolve_warm_endpoints(
        _Broken(), _instance("skypilot", {}), entry={"endpoints": {"8000": "http://x"}}
    )
    assert resolved == {"8000": "http://x"}
    assert "ssh: command not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run, confirm failure**

Run: `pixi run pytest tests/cli/test_warm_attach_endpoints.py -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_warm_endpoints'`.

- [ ] **Step 3: Implement**

Replace the endpoint block inside the warm-attach helper in `src/kinoforge/cli/_commands.py` with a
call to a new function:

```python
def _resolve_warm_endpoints(
    provider: object,
    instance: Instance,
    *,
    entry: dict,  # type: ignore[type-arg]
) -> dict[str, str]:
    """Return the endpoints a warm attach should actually use.

    compute-seam S5 inverts the old preference. The recorded row is SEEDED
    onto the instance first — Modal's ``build-<hash>.modal.run`` URL cannot be
    rebuilt from tags, and ``ModalProvider.endpoints`` reads it off the
    instance — and then the provider is asked. A provider that can establish
    something live wins; one that echoes the seed changes nothing.

    Args:
        provider: The resolved compute provider.
        instance: The instance being attached to, tags already merged.
        entry: The ledger row.

    Returns:
        A port-keyed endpoint map, possibly empty.
    """
    recorded_raw = entry.get("endpoints")
    recorded: dict[str, str] = (
        {str(k): str(v) for k, v in recorded_raw.items()}
        if isinstance(recorded_raw, dict)
        else {}
    )
    seeded = dataclasses.replace(instance, endpoints=recorded)
    ensure = getattr(provider, "ensure_endpoints", None)
    if ensure is None:
        return recorded
    try:
        live = ensure(seeded)
    except Exception as exc:  # noqa: BLE001 — a fault must not abort the attach
        print(
            f"warning: live endpoint resolution failed for {instance.id}: "
            f"{type(exc).__name__}: {exc}. Falling back to the recorded "
            f"endpoint map, which may be stale.",
            file=sys.stderr,
        )
        return recorded
    return live or recorded
```

Then in the warm-attach helper, after the existing tag merge:

```python
    endpoints_dict = _resolve_warm_endpoints(provider, instance, entry=entry)
    if endpoints_dict:
        instance = dataclasses.replace(instance, endpoints=endpoints_dict)
```

Delete the old `entry_endpoints` / `hasattr(provider, "endpoints")` branch it replaces, and update
its surrounding comment: the 2026-06-18 RunPod incident it documents is still real, and is now
handled by the tag merge above plus RunPod's deterministic `endpoints`.

- [ ] **Step 4: Run, confirm pass**

Run: `pixi run pytest tests/cli/test_warm_attach_endpoints.py tests/cli -k "warm" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_warm_attach_endpoints.py
git commit -m "fix(cli): ask the provider for a live endpoint on warm attach"
```

---

## Task 4: One orchestrator-level provisional row, before every create

**Goal:** Write the durable `kf_launch_phase=launching` row before `create_instance` on EVERY
provider, reconcile it to the real id when create returns, and remove it when create raises.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` — new `_record_provisional_row` /
  `_forget_provisional_row` beside `_enforce_rate_cap` (`:533`), wired into
  `_provision_instance_and_build_backend` around `:993-1002`, and passed from `deploy_session`
  (`:1504`, `:1549`)
- Test: `tests/core/test_provisional_launch_row.py` (create)

**Acceptance Criteria:**
- [ ] A row keyed by `spec.run_id` exists in the ledger BEFORE `create_instance` is called, carrying
      `kf_launch_phase=launching`, `kf_run_id`, `kf_launched_at` and the provider name — asserted by
      a fake provider that inspects the ledger from inside `create_instance`.
- [ ] On success the real row is recorded (existing `on_instance_created`) and THEN the provisional
      row is forgotten — in that order, so no window exists with neither row.
- [ ] On `create_instance` raising, the provisional row is forgotten and the exception propagates
      unchanged.
- [ ] A create that is retried by the capacity-wait loop writes the provisional row ONCE, not per
      attempt.
- [ ] A ledger fault on write or forget is logged and never propagates — the launch proceeds.
- [ ] An empty `run_id` skips the row with a warning rather than writing an unkeyed row.
- [ ] Every provider gets this: the test parameterises over a fake provider standing in for
      runpod/modal/local semantics (server-assigned id ≠ run_id).

**Verify:** `pixi run pytest tests/core/test_provisional_launch_row.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_provisional_launch_row.py`:

```python
"""Behavior: no launch is invisible, on any provider.

Finding F12: every durable ledger write happened in an ``on_instance_created``
callback, so a kill during the multi-minute create left a billing resource no
kinoforge command could see. Brief 1 fixed it inside the SkyPilot provider;
S5 makes it a property of the orchestrator, which is the only place that knows
a create is about to happen on ANY provider.
"""

from __future__ import annotations

import pytest


def test_row_exists_while_create_is_in_flight(tmp_path) -> None:  # noqa: ANN001
    """The row is visible from INSIDE create_instance.

    Bug caught: writing the row after create returns — which is exactly the
    hole F12 names, and which a test asserting only the end state cannot see.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.core.orchestrator import _record_provisional_row
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)

    row_id = _record_provisional_row(
        ledger=ledger,
        run_id="kf-run-1",
        provider_name="runpod",
        tags={"kinoforge_engine": "diffusers"},
        max_age_s=3600,
        now=1_700_000_000.0,
    )

    assert row_id == "kf-run-1"
    entry = ledger.read("kf-run-1")
    assert entry is not None
    assert entry["provider"] == "runpod"
    assert entry["tags"]["kf_launch_phase"] == "launching"
    assert entry["tags"]["kf_run_id"] == "kf-run-1"
    assert entry["tags"]["kinoforge_engine"] == "diffusers"
    assert float(entry["tags"]["kf_launched_at"]) == 1_700_000_000.0


def test_empty_run_id_writes_nothing(tmp_path, caplog) -> None:  # noqa: ANN001
    """No key, no row — an unkeyed row is unfindable and unforgettable.

    Bug caught: writing ``id=""`` produces a row every reconcile pass trips
    over and no destroy can act on.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.core.orchestrator import _record_provisional_row
    from kinoforge.stores.local import LocalArtifactStore

    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    with caplog.at_level("WARNING"):
        assert _record_provisional_row(
            ledger=ledger, run_id="", provider_name="runpod",
            tags={}, max_age_s=60, now=0.0,
        ) is None
    assert ledger.entries() == []


def test_a_ledger_fault_never_fails_the_launch(caplog) -> None:  # noqa: ANN001
    """Bookkeeping cannot break a launch that would have succeeded.

    Bug caught: a store 5xx or lock-lease timeout turning a healthy launch
    into a failure — the discipline carried over from
    providers/skypilot/__init__.py:1051-1070.
    """
    from kinoforge.core.orchestrator import _record_provisional_row

    class _AngryLedger:
        def record(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
            raise RuntimeError("store unavailable")

    with caplog.at_level("WARNING"):
        assert _record_provisional_row(
            ledger=_AngryLedger(), run_id="kf-run-2", provider_name="modal",
            tags={}, max_age_s=60, now=0.0,
        ) is None
    assert "kf-run-2" in caplog.text


def test_forget_is_best_effort(caplog) -> None:  # noqa: ANN001
    """A failing forget logs and returns.

    Bug caught: the success path raising AFTER the instance is live, so the
    caller never reaches the orchestrator's post-create record — the exact
    hazard documented at providers/skypilot/__init__.py:1100-1112.
    """
    from kinoforge.core.orchestrator import _forget_provisional_row

    class _AngryLedger:
        def forget(self, instance_id: str) -> None:
            raise RuntimeError("store unavailable")

    with caplog.at_level("WARNING"):
        _forget_provisional_row(_AngryLedger(), "kf-run-3")
    assert "kf-run-3" in caplog.text
```

Then add the wiring test in the same file, driving
`_provision_instance_and_build_backend` through the existing fake-provider/fake-engine fixtures used
by `tests/core/` (find them with `rg -n "def .*fake_provider|FakeProvider" tests/core | head`):

```python
def test_success_records_the_real_row_before_forgetting_the_provisional(tmp_path):  # noqa: ANN001, ANN201
    """Order matters: real row first, then forget.

    Bug caught: forgetting first leaves a window in which a kill loses BOTH
    rows, which is the state F12 exists to make impossible.
    """
    # Drive the orchestrator with a provider whose create returns a
    # server-assigned id (runpod/modal semantics: id != run_id), recording the
    # ledger's row ids at each callback, and assert the observed sequence is
    # ["kf-run-9"], ["kf-run-9", "pod-abc"], ["pod-abc"].


def test_create_failure_removes_the_provisional_row(tmp_path):  # noqa: ANN001, ANN201
    """A failed launch leaves no ghost.

    Bug caught: a permanent row whose est_spend (age×rate) inflates forever —
    the "$210 phantom pod" failure mode cli/_reconcile.py documents.
    """


def test_capacity_retry_writes_the_row_once(tmp_path):  # noqa: ANN001, ANN201
    """Retries do not append duplicate rows.

    Bug caught: Ledger.record APPENDS (core/lifecycle.py:592), so a row
    written inside the retry loop yields N rows for one launch and every
    reader picks whichever it finds first.
    """
```

Fill those three bodies out concretely against the real fakes before running — a placeholder body is
a plan failure, not a test.

- [ ] **Step 2: Run, confirm failure**

Run: `pixi run pytest tests/core/test_provisional_launch_row.py -v`
Expected: FAIL — `ImportError: cannot import name '_record_provisional_row'`.

- [ ] **Step 3: Implement the writer**

In `src/kinoforge/core/orchestrator.py`, above `_enforce_rate_cap`:

```python
def _record_provisional_row(
    *,
    ledger: Any,  # noqa: ANN401 — Ledger, or any object exposing record/forget
    run_id: str,
    provider_name: str,
    tags: dict[str, str],
    max_age_s: int,
    now: float,
    logger: logging.Logger = _log,
) -> str | None:
    """Write the durable pre-launch row and return its id.

    compute-seam S5 generalises Brief 1's SkyPilot-only fix (finding F12). A
    ``create_instance`` call is multi-minute on every cloud provider, and until
    it returns there is no durable record of the resource it may already have
    created: a SIGKILL inside it leaves a billing pod, cluster or app that no
    kinoforge command can see.

    The row is keyed by the CLIENT-side id (``run_id``), which is not the
    provider's id anywhere but SkyPilot — on RunPod it is the pod NAME, on
    Modal the app run id. ``cli/_reconcile`` therefore adopts a ``launching``
    row by name before it is allowed to forget one.

    Never raises: bookkeeping must not be able to fail a launch that would
    otherwise succeed. A fault forfeits F12 protection for this launch only,
    and is logged rather than passed.

    Args:
        ledger: The ledger to write to.
        run_id: The client-side id for this launch.
        provider_name: The provider about to be called.
        tags: Orchestrator tags to carry onto the row.
        max_age_s: Lifecycle snapshot, so the reaper can age the row out.
        now: Current epoch seconds (injected for testability).
        logger: Injected for testability.

    Returns:
        The row id, or None when nothing was written.
    """
    if not run_id:
        logger.warning(
            "F12: no run_id for this launch; skipping the pre-launch "
            "provisional row (a row with no id cannot be found or forgotten)"
        )
        return None
    provisional = Instance(
        id=run_id,
        provider=provider_name,
        status="starting",
        created_at=now,
        endpoints={},
        tags={
            **tags,
            "kf_launch_phase": "launching",
            "kf_run_id": run_id,
            "kf_launched_at": repr(now),
        },
        cost_rate_usd_per_hr=0.0,
    )
    try:
        ledger.record(provisional, max_age_s=max_age_s)
    except Exception:  # noqa: BLE001 — ledger fault must not block a launch
        logger.warning(
            "F12 provisional ledger record failed for %r; this launch has no "
            "pre-launch orphan protection until it completes",
            run_id,
            exc_info=True,
        )
        return None
    return run_id


def _forget_provisional_row(
    ledger: Any,  # noqa: ANN401
    row_id: str | None,
    logger: logging.Logger = _log,
) -> None:
    """Remove the provisional row, best-effort.

    Called on both outcomes: after the real row is recorded on success, and
    immediately on failure. Never raises — on the success path the instance is
    already live, and an exception here would fail a launch that worked.

    Args:
        ledger: The ledger holding the row.
        row_id: The provisional row id, or None when none was written.
        logger: Injected for testability.
    """
    if not row_id:
        return
    try:
        ledger.forget(row_id)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a launch
        logger.warning(
            "F12 provisional ledger forget failed for %r; the provisional "
            "'launching' row may linger alongside the real record",
            row_id,
            exc_info=True,
        )
```

- [ ] **Step 4: Wire it into the create path**

In `_provision_instance_and_build_backend`, add a `provisional_ledger: Any | None = None` keyword
(document it in the docstring's Args), and wrap the create:

```python
    provisional_id = (
        _record_provisional_row(
            ledger=provisional_ledger,
            run_id=run_id,
            provider_name=getattr(resolved_provider, "name", "unknown"),
            tags=dict(tags or {}),
            max_age_s=int(lifecycle.max_lifetime_s),
            now=time.time(),
        )
        if provisional_ledger is not None
        else None
    )
    try:
        instance = _create_with_capacity_wait(
            create=lambda: resolved_provider.create_instance(_build_spec()),
            capacity_wait_s=capacity_wait_s,
        )
    except BaseException:
        # A create that never produced a resource must not leave a row whose
        # est_spend inflates forever (cli/_reconcile's "$210 phantom pod").
        _forget_provisional_row(provisional_ledger, provisional_id)
        raise
    if on_instance_created is not None:
        on_instance_created(instance)
    # Order is load-bearing: the REAL row is written first, so no window
    # exists in which a kill loses both rows.
    _forget_provisional_row(provisional_ledger, provisional_id)
```

In `deploy_session`, pass `provisional_ledger=Ledger(store=store)` at both
`_provision_instance_and_build_backend` call sites (`:1504`, `:1549`). Leave the existing
`set_launch_ledger` install in place for now — Task 5 deletes it, and until then a skypilot launch
writing two provisional rows for one cluster id is a same-key double record. Guard against that by
gating the new writer for this task only: `if getattr(resolved_provider, "set_launch_ledger", None)
is None`. Task 5 removes the gate along with the provider seam. **Write that conditional with a
comment naming Task 5**, so it cannot survive as a permanent special case.

- [ ] **Step 5: Run, confirm pass**

Run: `pixi run pytest tests/core/test_provisional_launch_row.py tests/core -k "provision or deploy" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_provisional_launch_row.py
git commit -m "feat(orchestrator): write the pre-launch provisional row for every provider"
```

---

## Task 5: Delete SkyPilot's private writer, move the harness and the parity guard with it

**Goal:** One writer, not two — delete `set_launch_ledger` and the provider's provisional block, and
carry the golden harness and the field-consumption parity guard onto the orchestrator writer in the
same commit.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py:745-760` (`_launch_ledger`,
  `set_launch_ledger`), `:1031-1070` (record), `:1100-1123` (forget)
- Modify: `src/kinoforge/core/orchestrator.py:1300-1306` (delete the duck-typed install) and the
  Task 4 gate
- Modify: `tools/snapshot_launch_payloads.py:109-140` (`_RecordingLedger`), `:483-574`
  (`_capture_skypilot`)
- Modify: `tests/providers/test_field_consumption_parity.py:873-875` (the `tags` observation)
- Modify: `tests/providers/test_skypilot.py:1398-1460` (the four provisional-row tests)

**Acceptance Criteria:**
- [ ] `rg -n "set_launch_ledger|_launch_ledger" src/` returns nothing.
- [ ] `tests/providers/test_skypilot.py`'s provisional-row tests are MOVED to
      `tests/core/test_provisional_launch_row.py` (adapted to the orchestrator writer), not deleted —
      including `test_tunnel_failure_keeps_the_provisional_row`, whose claim now belongs to the
      orchestrator's failure branch.
- [ ] The golden harness still produces an `Instance` for skypilot: `_capture_skypilot` calls the
      orchestrator writer against a recording ledger and returns that row.
- [ ] `tests/providers/test_field_consumption_parity.py` still proves `tags` reaches skypilot, with
      its comment updated to name the new observation point.
- [ ] All 31 payload goldens byte-identical.
- [ ] `pixi run test` green.

**Verify:** `pixi run pytest tests/providers/test_launch_payload_goldens.py tests/providers/test_field_consumption_parity.py tests/core/test_provisional_launch_row.py -v` → all pass

**Steps:**

- [ ] **Step 1: Move the provider's tests to the orchestrator first (RED)**

Copy the four tests at `tests/providers/test_skypilot.py:1398-1460` into
`tests/core/test_provisional_launch_row.py`, rewriting each to drive the orchestrator writer. The
tunnel-failure one changes shape and must keep its claim:

```python
def test_a_create_that_raises_after_the_resource_exists_keeps_nothing(tmp_path):  # noqa: ANN001, ANN201
    """A create that raises leaves no row — and that is now safe.

    The SkyPilot version of this test (test_tunnel_failure_keeps_the_
    provisional_row) asserted the OPPOSITE, because the provider raised
    ProvisionFailed *after* the cluster was up and only the provisional row
    could surface it. That is still true, so the provider's failure path must
    tear the cluster down itself before raising — asserted in
    tests/providers/test_skypilot_endpoints.py::
    test_second_spawn_failure_kills_the_first_tunnel_and_the_cluster.

    Bug caught: dropping BOTH the row and the teardown, which would restore
    the invisible-billing-cluster hole in a new place.
    """
```

Run: `pixi run pytest tests/core/test_provisional_launch_row.py -v` → the new tests fail while the
skypilot writer still owns the behaviour (double rows, or the provider row winning).

- [ ] **Step 2: Delete the provider-side writer**

Remove from `src/kinoforge/providers/skypilot/__init__.py`: the `_launch_ledger` attribute, the
`set_launch_ledger` method, the `provisional = Instance(...)` block and both `record` / `forget`
call sites. Keep the create-path teardown on tunnel failure (Task 0) — that is what makes the
deletion safe, and Step 1's docstring says so.

In `src/kinoforge/core/orchestrator.py`, delete the duck-typed install at `:1300-1306` and the Task 4
gate, so every provider now goes through one writer.

- [ ] **Step 3: Carry the golden harness**

In `tools/snapshot_launch_payloads.py`, retarget `_RecordingLedger` (its docstring currently
explains it captures SkyPilot's own row) and change `_capture_skypilot` to write the row the
orchestrator would:

```python
    ledger = _RecordingLedger()
    from kinoforge.core.orchestrator import _record_provisional_row

    # compute-seam S5: the provisional row moved to the orchestrator, so the
    # capture writes it the same way deploy_session does. It is still the only
    # Instance a skypilot capture can observe — _StopLaunch aborts inside
    # sky.launch — and it is still where spec.tags lands for the parity guard.
    _record_provisional_row(
        ledger=ledger,
        run_id=spec.run_id,
        provider_name="skypilot",
        tags=dict(spec.tags),
        max_age_s=int(spec.lifecycle.max_lifetime_s),
        now=FROZEN_EPOCH,
    )
    try:
        provider.create_instance(spec)
    except _StopLaunch:
        pass
```

Delete the now-unused `provider.set_launch_ledger(ledger)` line. `_RecordingLedger.record` already
accepts `max_age_s`; add `idle_timeout_s: int | None = None` to its signature if the writer passes
it, so the fake never diverges from `Ledger.record`.

- [ ] **Step 4: Update the parity guard comment**

In `tests/providers/test_field_consumption_parity.py`, replace the comment at `:873-874`:

```python
        # Observed on the pre-launch provisional row (F12). S5 moved the writer
        # from the provider to the orchestrator; the row is still the only
        # Instance a skypilot capture can see, because _StopLaunch aborts
        # inside sky.launch.
```

- [ ] **Step 5: Run everything**

Run: `pixi run pytest tests/core/test_provisional_launch_row.py tests/providers -v`
Expected: PASS, including the 31 goldens byte-identical.

Run: `pixi run test && pixi run typecheck && pixi run lint`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py src/kinoforge/core/orchestrator.py \
        tools/snapshot_launch_payloads.py tests/providers tests/core
git commit -m "refactor(skypilot): delete the private launch ledger seam"
```

---

## Task 6: Adopt-or-forget — make the reconciler safe for a client-side-keyed row

**Goal:** Stop `kinoforge list` from deleting the exact row F12 exists to keep, by resolving a
`launching` row by NAME before deciding it is gone.

**Files:**
- Modify: `src/kinoforge/cli/_reconcile.py:41-93`
- Modify: `src/kinoforge/providers/runpod/__init__.py:1431` (`_LIST_PODS_QUERY` gains `name`),
  `:1587-1615` (`_pod_to_instance` records the name in tags)
- Test: `tests/cli/test_reconcile_launching_rows.py` (create)

**Acceptance Criteria:**
- [ ] A `kf_launch_phase=launching` row is NEVER forgotten on a bare `get_instance` `KeyError`
      alone — the reconciler first lists instances and looks for one whose name equals
      `kf_run_id`.
- [ ] When a matching pod IS found, the row is rewritten to the real instance id (adopted):
      `ledger.forget(run_id)` then `ledger.record(real_instance, …)`, and the returned report names
      the adoption.
- [ ] When no match is found AND the row is older than `boot_timeout_s`, it is forgotten.
- [ ] When no match is found and the row is YOUNGER than `boot_timeout_s`, it is left alone — a
      create in flight must not be reconciled away by a concurrent `kinoforge list`.
- [ ] `_LIST_PODS_QUERY` requests `name`, and `_pod_to_instance` carries it as `tags["name"]`.
- [ ] Non-launching rows keep exactly today's behaviour, asserted by an unchanged existing test in
      `tests/cli/test_reconcile_ledger.py`.

**Verify:** `pixi run pytest tests/cli/test_reconcile_launching_rows.py tests/cli/test_reconcile_ledger.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_reconcile_launching_rows.py`:

```python
"""Behavior: a launching row is adopted or aged out — never blind-deleted.

The hazard S5 introduces and this closes: the provisional row is keyed by the
CLIENT-side id (run_id). On RunPod that is the pod NAME, so
``provider.get_instance(run_id)`` KeyErrors even when the pod exists — and the
old reconciler forgets a row on KeyError. That would delete the only durable
handle on a pod that is very much billing.
"""

from __future__ import annotations

from kinoforge.core.interfaces import Instance


class _FakeLedger:
    def __init__(self, entries: list[dict]) -> None:  # type: ignore[type-arg]
        self.entries_ = entries
        self.forgotten: list[str] = []
        self.recorded: list[Instance] = []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)

    def record(self, instance: Instance, **kwargs: object) -> None:
        self.recorded.append(instance)


def _launching_row(*, age_s: float, now: float) -> dict:  # type: ignore[type-arg]
    return {
        "id": "kf-run-42",
        "provider": "runpod",
        "created_at": now - age_s,
        "tags": {"kf_launch_phase": "launching", "kf_run_id": "kf-run-42"},
    }


class _ProviderWithPod:
    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)

    def list_instances(self) -> list[Instance]:
        return [
            Instance(
                id="pod-real-1",
                provider="runpod",
                status="ready",
                created_at=0.0,
                endpoints={},
                tags={"name": "kf-run-42", "mode": "pod"},
                cost_rate_usd_per_hr=1.5,
            )
        ]


class _ProviderWithNothing(_ProviderWithPod):
    def list_instances(self) -> list[Instance]:
        return []


def test_a_launching_row_is_adopted_onto_the_real_id() -> None:
    """The pod exists under the run_id NAME → the row becomes usable.

    Bug caught: forgetting the row and leaving a live RunPod pod with no
    ledger entry — F12 reopened one layer down.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    entries = [_launching_row(age_s=30.0, now=now)]

    _reconcile_dead_ledger_entries(
        ledger, entries, get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert ledger.forgotten == ["kf-run-42"]
    assert [i.id for i in ledger.recorded] == ["pod-real-1"]


def test_a_young_launching_row_with_no_pod_is_left_alone() -> None:
    """A create still in flight is not reconciled away.

    Bug caught: a concurrent ``kinoforge list`` during a 10-minute boot
    deleting the row that protects that very boot.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=30.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )
    assert ledger.forgotten == []


def test_an_aged_launching_row_with_no_pod_is_forgotten() -> None:
    """A launch that never produced anything eventually stops haunting list.

    Bug caught: a permanent ghost row whose est_spend inflates forever.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )
    assert ledger.forgotten == ["kf-run-42"]
```

- [ ] **Step 2: Run, confirm failure**

Run: `pixi run pytest tests/cli/test_reconcile_launching_rows.py -v`
Expected: FAIL — `_reconcile_dead_ledger_entries()` has no `now` parameter, and the first test's row
is forgotten with nothing recorded.

- [ ] **Step 3: Implement**

In `src/kinoforge/cli/_reconcile.py`, widen the ledger Protocol to `forget` + `record`, add
`now: float | None = None` and `boot_timeout_s: float = 1800.0` keywords, and branch before the
existing probe:

```python
        tags = entry.get("tags") or {}
        if isinstance(tags, dict) and tags.get("kf_launch_phase") == "launching":
            _adopt_or_age_out(
                ledger, provider, entry, now=now_s, boot_timeout_s=boot_timeout_s
            )
            continue
```

and add:

```python
def _adopt_or_age_out(
    ledger: _ReconcileLedger,
    provider: Any,  # noqa: ANN401
    entry: dict[str, Any],
    *,
    now: float,
    boot_timeout_s: float,
) -> None:
    """Resolve a pre-launch provisional row by NAME, or age it out.

    compute-seam S5. The row is keyed by the client-side ``run_id``, which is
    the provider's id only on SkyPilot; on RunPod it is the pod name and on
    Modal the app run id. ``get_instance(run_id)`` therefore KeyErrors for a
    pod that exists, and the caller's "KeyError means gone" rule would delete
    the only durable handle on a billing resource — the precise failure F12
    exists to prevent.

    Args:
        ledger: Ledger exposing ``forget`` and ``record``.
        provider: The resolved provider.
        entry: The launching row.
        now: Current epoch seconds.
        boot_timeout_s: How long a launch may plausibly still be in flight.
    """
    row_id = str(entry.get("id") or "")
    run_id = str((entry.get("tags") or {}).get("kf_run_id") or row_id)
    try:
        live = provider.list_instances()
    except Exception as exc:  # noqa: BLE001 — uncertain → keep the row
        logger.debug("reconcile: launching row %s uncertain: %s", row_id, exc)
        return
    for inst in live:
        if inst.id == run_id or inst.tags.get("name") == run_id:
            ledger.forget(row_id)
            ledger.record(inst)
            logger.info(
                "reconcile: adopted launching row %s onto live instance %s",
                row_id,
                inst.id,
            )
            return
    age = now - float(entry.get("created_at") or now)
    if age > boot_timeout_s:
        ledger.forget(row_id)
```

In `src/kinoforge/providers/runpod/__init__.py`, add `name` to the query and the tag:

```python
_LIST_PODS_QUERY: str = "{ myself { pods { id name desiredStatus imageName costPerHr } } }"
```

```python
        tags={"mode": "pod", "name": str(pod.get("name") or "")},
```

- [ ] **Step 4: Run, confirm pass**

Run: `pixi run pytest tests/cli/test_reconcile_launching_rows.py tests/cli/test_reconcile_ledger.py tests/providers/runpod -v`
Expected: PASS. RunPod fakes that return pods without a `name` key still work — the tag becomes
`""`, which matches no run_id.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_reconcile.py src/kinoforge/providers/runpod/__init__.py \
        tests/cli/test_reconcile_launching_rows.py tests/providers/runpod
git commit -m "fix(reconcile): adopt a launching row by name before forgetting it"
```

---

## Task 7: SkyPilot instance tags name the SKU (S4 follow-up)

**Goal:** Make `RateCapExceeded.placement_summary` say `sku=…, cloud=…, region=…` for skypilot
instead of `provider=skypilot` and nothing more.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` — a `_selection_tags(cluster_name)` helper
  reusing `realized_rate`'s handle read (`:1159-1200`), applied to the returned `Instance`
  (`:1124-1137`)
- Test: `tests/providers/test_skypilot_selection_tags.py` (create)

**Acceptance Criteria:**
- [ ] After a launch, `instance.tags` carries `sku`, `cloud`, `region` and `accelerators` when the
      handle exposes them — the four keys `_placement_summary` (`core/orchestrator.py:526`) already
      reads.
- [ ] An unreadable handle leaves the tags ABSENT rather than writing empty strings —
      `_placement_summary` skips falsy values, and a `sku=` with nothing after it is worse than
      silence.
- [ ] The tag read never raises and never blocks a launch: any exception logs and returns `{}`.
- [ ] A test asserts the composed `RateCapExceeded` message for a skypilot instance contains the SKU
      and the cloud.
- [ ] All 31 goldens unchanged (the capture aborts before the read).

**Verify:** `pixi run pytest tests/providers/test_skypilot_selection_tags.py tests/core -k rate_cap -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/providers/test_skypilot_selection_tags.py`:

```python
"""Behavior: what SkyPilot actually booked is on the Instance.

S4 shipped a cap violation whose message read ``provider=skypilot`` and
nothing else, because a SkyPilotProvider Instance carried no selection tags
and _placement_summary reports only what an Instance really holds. An operator
reading that cannot tell "my cap is too low" from "this went somewhere I did
not intend", which is the whole stated purpose of the summary.
"""

from __future__ import annotations

from typing import Any


def test_launch_records_the_chosen_sku_cloud_and_region() -> None:
    """The optimizer's choice lands on the Instance.

    Bug caught: a cap violation, a ledger row and a status line that all
    describe a cluster without saying what it is.
    """
    # Build a _FakeSky whose status() returns a handle exposing
    # launched_resources with instance_type / cloud / region / accelerators,
    # launch a spec, and assert instance.tags contains:
    #   {"sku": "c6i.large", "cloud": "AWS", "region": "us-west-2",
    #    "accelerators": ""} minus any key the handle did not expose.


def test_an_unreadable_handle_writes_no_empty_tags() -> None:
    """Silence beats ``sku=``.

    Bug caught: writing empty strings makes _placement_summary emit
    "sku=, cloud=, provider=skypilot", which reads as a broken tool rather
    than as missing information.
    """


def test_the_rate_cap_message_names_the_sku() -> None:
    """The S4 follow-up, end to end.

    Bug caught: the enrichment landing on the Instance but never reaching the
    error, e.g. because the tags are applied after the orchestrator's copy.
    """
```

Fill each body against the real `_FakeSky` shape in `tests/providers/test_skypilot.py` (its
`status()` record shape is what `realized_rate` parses at `:1180-1200`) before running.

- [ ] **Step 2: Run, confirm failure**

Run: `pixi run pytest tests/providers/test_skypilot_selection_tags.py -v`
Expected: FAIL — `instance.tags` has only the spec tags plus `ports`.

- [ ] **Step 3: Implement**

Add to `SkyPilotProvider`, factoring the handle lookup that `realized_rate` already performs so
there is ONE parser of sky's status record:

```python
    def _launched_resources(self, cluster_name: str) -> Any | None:  # noqa: ANN401
        """Return the launched-resources record for a cluster, or None.

        Shared by :meth:`realized_rate` and :meth:`_selection_tags` so sky's
        status shape is parsed in one place. Never raises.

        Args:
            cluster_name: The cluster to look up.

        Returns:
            The resources object, or None when unreadable.
        """

    def _selection_tags(self, cluster_name: str) -> dict[str, str]:
        """Return sku / cloud / region / accelerators for a launched cluster.

        S4 follow-up. ``_placement_summary`` reports only what an Instance
        holds, and a SkyPilot Instance held nothing, so ``RateCapExceeded``
        could not distinguish a bad cap from a bad placement. Keys whose value
        is unreadable are OMITTED, not blanked.

        Args:
            cluster_name: The cluster to describe.

        Returns:
            A tag mapping, possibly empty. Never raises.
        """
```

Apply on the returned Instance:

```python
            tags={
                **dict(spec.tags),
                "ports": ",".join(spec.ports),
                **self._selection_tags(cluster_name),
            },
```

- [ ] **Step 4: Run, confirm pass**

Run: `pixi run pytest tests/providers/test_skypilot_selection_tags.py tests/providers/test_skypilot.py tests/providers/test_launch_payload_goldens.py -v`
Expected: PASS, goldens unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py \
        tests/providers/test_skypilot_selection_tags.py
git commit -m "feat(skypilot): record the chosen sku, cloud and region on the instance"
```

---

## Task 8: Refuse an over-cap SkyPilot launch BEFORE `Task.setup` (S4 follow-up)

**Goal:** Ask `sky.optimize()` what the plan will cost before `sky.launch` runs, and refuse an
over-cap launch while it is still free to refuse.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py` (`_estimate_hourly_rate`, called in
  `create_instance` immediately before `sky.launch` at `:1069`)
- Modify: `tools/snapshot_launch_payloads.py:525-548` (`_CapturingSky` gains `optimize`)
- Test: `tests/providers/test_skypilot_prelaunch_estimate.py` (create)

**Acceptance Criteria:**
- [ ] An estimate ABOVE `spec.placement.max_usd_per_hr` raises `RateCapExceeded` before `sky.launch`
      is called at all — asserted by a fake sky recording zero launches.
- [ ] The raised error names the estimate, the cap, and the cluster name, and says the estimate is
      pre-launch (so an operator does not read it as a billed rate).
- [ ] An estimate at or below the cap launches normally.
- [ ] An unreadable estimate (optimize raises, no `best_resources`, `get_cost` raises) WARNs and
      launches — the S4 post-launch readback remains the enforcement.
- [ ] `_estimate_hourly_rate` never raises.
- [ ] The provisional ledger row is forgotten on the refusal path (it goes through the same
      `except BaseException` branch added in Task 4 — asserted).
- [ ] All 31 goldens byte-identical, with `_CapturingSky.optimize` answering from the frozen
      catalog so no capture reaches the network.

**Verify:** `pixi run pytest tests/providers/test_skypilot_prelaunch_estimate.py tests/providers/test_launch_payload_goldens.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/providers/test_skypilot_prelaunch_estimate.py`:

```python
"""Behavior: an over-cap SkyPilot launch is refused while refusing is free.

S4 shipped the readback that makes the cap TRUE; this makes it cheap. On
SkyPilot ``sky.launch`` runs ``Task.setup`` before it returns, so a violation
caught afterwards discards several minutes of provisioning that was already
paid for (design §6, qualified 2026-09-01; §14). ``sky.optimize`` answers the
same question before anything is booked.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import RateCapExceeded


class _OptimizingSky:
    """A sky stand-in whose optimizer returns a priced plan."""

    def __init__(self, hourly: float | None) -> None:
        self._hourly = hourly
        self.launches: list[Any] = []

    class Task:
        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> Any:
            return _FakeTask()

    def optimize(self, dag: Any, **kwargs: Any) -> Any:
        if self._hourly is None:
            raise RuntimeError("optimize unavailable")
        return _FakeDag(self._hourly)

    def launch(self, task: Any, **kwargs: Any) -> None:
        self.launches.append(kwargs.get("cluster_name"))


def test_an_over_cap_estimate_refuses_before_launch() -> None:
    """No cluster is created at all.

    Bug caught: enforcing only after launch, which on SkyPilot means paying
    for Task.setup and then throwing it away — the accepted-but-expensive
    trade S4 recorded as this follow-up.
    """
    sky = _OptimizingSky(hourly=1.99)
    provider = _provider_with_cap(sky, cap=1.09)
    with pytest.raises(RateCapExceeded, match="1.9900"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == []


def test_an_estimate_under_the_cap_launches() -> None:
    """The ordinary path is untouched.

    Bug caught: a units error (per-second vs per-hour) refusing every launch.
    """
    sky = _OptimizingSky(hourly=0.085)
    provider = _provider_with_cap(sky, cap=1.09)
    provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == ["kf-estimate-probe"]


def test_an_unreadable_estimate_warns_and_launches(caplog) -> None:  # noqa: ANN001
    """A best-effort estimate never blocks a launch on its own failure.

    Bug caught: a sky server outage or an API shape change (0.12.3 returns a
    RequestId, not a Dag) turning every launch into a hard failure.
    """
    sky = _OptimizingSky(hourly=None)
    provider = _provider_with_cap(sky, cap=1.09)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == ["kf-estimate-probe"]
    assert "estimate" in caplog.text.lower()
```

Write `_FakeTask`, `_FakeDag` (exposing `tasks[0].best_resources.get_cost(3600.0)`),
`_provider_with_cap` and `_spec_with_cap` in the same module — the placement carrying
`max_usd_per_hr` reaches the provider through `InstanceSpec.placement` (see
`build_instance_spec`; confirm the attribute path with
`rg -n "placement" src/kinoforge/providers/skypilot/__init__.py | head`).

- [ ] **Step 2: Run, confirm failure**

Run: `pixi run pytest tests/providers/test_skypilot_prelaunch_estimate.py -v`
Expected: FAIL — all three launch, because no estimate is taken.

- [ ] **Step 3: Implement**

```python
    def _estimate_hourly_rate(self, task: Any) -> float | None:  # noqa: ANN401
        """Return the optimizer's estimated USD/hr for ``task``, or None.

        At the pinned skypilot-0.12.3.post1 ``sky.optimize`` is a client/server
        call — ``sky/client/sdk.py:411`` POSTs ``/optimize`` and returns a
        ``RequestId['sky.Dag']`` — so it is resolved through the same
        ``_resolve`` path ``create_instance`` uses for ``sky.launch``. The
        price then comes off the optimized task's ``best_resources``
        (``sky/resources.py:1704``).

        Best-effort by contract: any failure returns None and the caller
        proceeds to launch, because S4's post-launch readback is what makes
        the cap true. This only decides how much work a violation throws away.

        Args:
            task: The ``sky.Task`` about to be launched.

        Returns:
            USD per hour, or None when the estimate is unreadable.
        """
```

In `create_instance`, immediately before `raw = sky.launch(...)` and AFTER the task is built:

```python
        cap = spec.placement.max_usd_per_hr
        estimate = self._estimate_hourly_rate(task)
        if estimate is None:
            logger.warning(
                "skypilot: pre-launch cost estimate unreadable for %r; "
                "launching and relying on the post-launch rate readback",
                cluster_name,
            )
        elif cap > 0 and estimate > cap:
            raise RateCapExceeded(
                realized=estimate,
                cap=cap,
                instance_id=cluster_name,
                placement_summary=(
                    f"provider=skypilot, source=sky.optimize (PRE-LAUNCH "
                    f"estimate; nothing was created)"
                ),
            )
```

Confirm `RateCapExceeded`'s constructor signature in `src/kinoforge/core/errors.py` before writing
this — it takes `realized`, `cap`, `instance_id`, `placement_summary` as of S4 (`f570725f`), and
`realized` must accept a float here.

In `tools/snapshot_launch_payloads.py`, give `_CapturingSky` an `optimize` that never touches the
network:

```python
        @staticmethod
        def optimize(dag: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            """Refuse to estimate — the capture must not depend on a server.

            Returning None makes the provider's estimate unreadable, which is
            the documented WARN-and-proceed path, so the captured payload is
            the same one a real launch sends.
            """
            del dag, kwargs
            raise RuntimeError("offline capture: no optimizer")
```

- [ ] **Step 4: Run, confirm pass**

Run: `pixi run pytest tests/providers/test_skypilot_prelaunch_estimate.py tests/providers/test_launch_payload_goldens.py tests/providers/test_skypilot.py -v`
Expected: PASS, 31 goldens byte-identical.

- [ ] **Step 5: Full suite + static checks**

Run: `pixi run test && pixi run typecheck && pixi run lint`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/providers/skypilot/__init__.py tools/snapshot_launch_payloads.py \
        tests/providers/test_skypilot_prelaunch_estimate.py
git commit -m "feat(skypilot): refuse an over-cap launch before sky.launch runs setup"
```

---

## Task 9: RED live smoke scaffold — committed BEFORE any spend

**Goal:** Commit the failing live smoke that will prove S5 on real infrastructure, so a mid-spend
crash cannot lose it.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s5_smoke.py`
- Create (empty placeholder, written by the run): `tests/live/_s5_endpoint_evidence.json`,
  `tests/live/_s5_prelaunch_refusal_evidence.json`

**Acceptance Criteria:**
- [ ] Module skips cleanly (`allow_module_level=True`) without `KINOFORGE_LIVE_TESTS=1`, AWS creds,
      the `aws` binary, or `import sky` — the same gate block as
      `tests/live/test_compute_seam_s4_selection_smoke.py:56-84`.
- [ ] Teardown, EC2 oracle and util probe are IMPORTED from
      `tests/live/test_compute_seam_s1_smoke.py`, never reimplemented.
- [ ] Three claims are declared, in cost order:
      (1) **offline** — `ensure_endpoints` on a fabricated instance re-forwards from
      `tags["ports"]`; (2) **pre-launch refusal** — a config whose cap is below any real SKU raises
      `RateCapExceeded` and `sky status` shows NO cluster (≈$0); (3) **tunnel repair** — a real CPU
      cluster serving on 8000 and 8001, tunnels killed, `ensure_endpoints` returns fresh ports that
      both answer HTTP 200.
- [ ] A utilisation poll thread on a 75 s cadence, surfacing `gpuUtilPercent` / `cpuPercent` /
      `memoryPercent` — never `est_spend` as the health signal.
- [ ] Committed while RED (it fails or errors without live creds; that is the point).

**Verify:** `pixi run pytest tests/live/test_compute_seam_s5_smoke.py -v` → SKIPPED with the reason list (no creds in the default env), and `git log -1 --stat` shows the file committed

**Steps:**

- [ ] **Step 1: Write the smoke module**

Model it on `tests/live/test_compute_seam_s4_selection_smoke.py` — copy its docstring structure, its
`_REASONS` gate, its imports-after-gate pattern, and its evidence-writing tail. Claim 3's cluster
uses a spec built from `examples/configs/skypilot-cpu.yaml` with a launch that serves both ports:

```python
_LAUNCH = SpecLaunch(
    (
        "bash",
        "-lc",
        "python3 -m http.server 8000 --directory /tmp & "
        "python3 -m http.server 8001 --directory /tmp & wait",
    ),
)
```

and asserts, in order:

```python
    # 1. Both declared ports came back as absolute local URLs.
    assert set(instance.endpoints) == {"8000", "8001"}
    for url in instance.endpoints.values():
        assert urllib.request.urlopen(url, timeout=30).status == 200

    # 2. Kill both forwards the way a dying CLI would.
    before = dict(instance.endpoints)
    for tunnel in provider._tunnels[instance.id].values():  # noqa: SLF001
        tunnel.proc.terminate()

    # 3. ensure_endpoints repairs them onto NEW local ports that serve.
    after = provider.ensure_endpoints(instance)
    assert set(after) == {"8000", "8001"}
    assert after != before
    for url in after.values():
        assert urllib.request.urlopen(url, timeout=30).status == 200
```

Claim 2 asserts nothing was created:

```python
    with pytest.raises(RateCapExceeded) as caught:
        provider.create_instance(over_cap_spec)
    assert "PRE-LAUNCH" in str(caught.value)
    assert _sky_status_of(cluster_name) is None
    assert Ledger(store=store).read(cluster_name) is None  # provisional row forgotten
```

- [ ] **Step 2: Confirm it is RED / skipping for the right reason**

Run: `pixi run pytest tests/live/test_compute_seam_s5_smoke.py -v`
Expected: `SKIPPED (S5 smoke skipped: KINOFORGE_LIVE_TESTS=1 required / ...)`.

- [ ] **Step 3: Commit the scaffold BEFORE any spend**

```bash
git add tests/live/test_compute_seam_s5_smoke.py
git commit -m "test(live): add the RED S5 endpoint and pre-launch refusal scaffold"
```

This commit is mandatory and is what the project's durability rule requires: a mid-spend crash must
not force the next session to rewrite 200 lines before it can retry the spend.

---

## Task 10: Run the live smoke, record the evidence, land the stage

**Goal:** Prove S5 on real infrastructure for ~$0.01, record what the design got wrong, and merge.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/_s5_endpoint_evidence.json`, `tests/live/_s5_prelaunch_refusal_evidence.json`
- Modify: `docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` (§9 and §11
  corrections, inline, in the S4 house style)
- Modify: `PROGRESS.md` (new RESUME SNAPSHOT; previous one demoted)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before the spend (creds present, zero active pods, clean tree).
- [ ] The smoke runs in `pixi run -e live-skypilot`, both live claims PASS, and the evidence JSONs
      are written with the cluster name, the realized rate, the pre/post endpoint maps and the local
      timestamp.
- [ ] Utilisation was polled on a 60–90 s cadence and the observed values appear in the evidence.
- [ ] AFTER the process exits: `pixi run kinoforge list` shows BOTH `[instance overview] No running
      instances.` and `No instances recorded in ledger.`, and `sky status` shows
      `No existing clusters.` Any survivor is destroyed explicitly with
      `pixi run kinoforge destroy --id <name>`.
- [ ] Design doc §9 is corrected inline for the two things this plan changed: the `endpoints` /
      `ensure_endpoints` split, and the client-side-id hazard the generalised row creates.
- [ ] `PROGRESS.md` gains an S5 RESUME SNAPSHOT naming: what shipped, the live evidence table, which
      goldens moved (expected: none), the S1–S4 line items still open, and the single next action.
- [ ] The branch is merged to `main` the way S1 (`40f0596c`), S2 (`e7e1df3d`), S3 (`2f062b75`) and
      S4 (`9e80470c`) landed.
- [ ] No `successful-generations.md` entry — S5 produces no video and adds no capability axis.

**Verify:** `pixi run -e live-skypilot pytest tests/live/test_compute_seam_s5_smoke.py -v` → 3 passed, then `pixi run kinoforge list` → both empty lines

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight
```

Expected: exit 0. A dirty tree here means Task 9's scaffold was not committed — fix that first.

- [ ] **Step 2: Run the smoke with the util poller live**

```bash
pixi run -e live-skypilot pytest \
  tests/live/test_compute_seam_s5_smoke.py -v -s
```

Watch the poll lines. GPU/CPU at 0% for three consecutive probes during a phase that should be busy
means the box is dead: capture the setup log via the imported `_capture_setup_log`, tear down, and
fail fast rather than waiting for the per-test timeout.

- [ ] **Step 3: Verify teardown AFTER the process exits**

```bash
pixi run kinoforge list
pixi run -e live-skypilot sky status
```

Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` AND
`No existing clusters.` A mid-run "no running instances" line is not proof — this check is.

- [ ] **Step 4: Commit the evidence**

```bash
git add tests/live/_s5_endpoint_evidence.json \
        tests/live/_s5_prelaunch_refusal_evidence.json
git commit -m "test(live): S5 endpoint repair and pre-launch refusal PROVEN live"
```

- [ ] **Step 5: Correct the design doc inline**

In `docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §9, add a qualification
block in the S3/S4 house style (`> **Corrected 2026-09-01**, during S5 …`) covering:

1. `endpoints()` did NOT become tunnel-ensuring; it split into a pure read and
   `ensure_endpoints`, because three of its five callers are observational and a read that spawns an
   ssh subprocess per invocation is a leak.
2. The generalised provisional row is keyed by a client-side id that is the provider's id only on
   SkyPilot, so `cli/_reconcile` had to learn adopt-by-name before it was allowed to forget a
   `launching` row.

Mark S5 SHIPPED in §11 with the live evidence paths.

- [ ] **Step 6: Update PROGRESS.md**

Demote the 2026-09-01 S4 snapshot to `### Previous snapshot (2026-09-01)` and write the S5 one
above it, in the established shape: what shipped, the two halves, where the design was wrong, which
goldens moved (expected: none — say so explicitly, it is a claim), the live evidence table with
cluster names and spend, what is still open, and the single next action.

Still open after S5, and it must be re-listed rather than quietly dropped:

- `region` wired on skypilot only; RunPod (`dataCenterId`) and Modal (`region=`) stay
  UNSUPPORTED-and-declared.
- The 11 ungated `tests/live` modules.
- `disk_gb` declared-and-warned, wired to nothing.
- The golden ratchet's non-recursive glob still misses 7 configs under `grids/` and `extras/`.
- The F3 env-routing gap (`sky` lives only in `live-skypilot`, so a default-env sweep marks every
  skypilot row `UNROUTABLE`).

```bash
git add PROGRESS.md docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md
git commit -m "docs(compute-seam): record S5, and correct what §9 got wrong"
```

- [ ] **Step 7: Merge**

```bash
pixi run pre-commit run --all-files
git checkout main
git merge --no-ff feat/compute-seam-s5-endpoint-shape-ledger \
  -m "merge: compute-seam S5 — one endpoint shape and one durable-write ordering"
```

---

## Self-review notes (run while writing; recorded for the implementer)

**Spec coverage.** Design §9 has exactly two paragraphs. "Endpoints" → Tasks 0, 1, 2, 3 (port-keyed
map, `_VIDEO_SERVER_PORT` and `{"ssh": …}` deleted, status prints the cluster name, ledger endpoints
demoted to a hint). "Ledger before create" → Tasks 4, 5, 6 (one orchestrator writer, provider seam
deleted, reconciler made safe). The two S4-recorded follow-ups → Tasks 7 and 8. Live proof → Tasks
9 and 10. Nothing in §9 is unassigned.

**Type consistency.** `_Tunnel(proc, local_port)`; `self._tunnels: dict[str, dict[str, _Tunnel]]`
(cluster → remote port → tunnel) in Tasks 0, 1, 9. `ensure_endpoints(self, instance: Instance) ->
dict[str, str]` on the ABC (Task 1), called in Tasks 2, 3, 9. `_ports_for(instance) ->
tuple[str, ...]`, `_ensure_tunnel(cluster_name, port) -> int`, `_tunnel_alive(tunnel) -> bool`
(Task 1). `_record_provisional_row(*, ledger, run_id, provider_name, tags, max_age_s, now,
logger) -> str | None` and `_forget_provisional_row(ledger, row_id, logger) -> None` (Task 4), used
identically in Tasks 5 and 8's refusal path. `_resolve_warm_endpoints(provider, instance, *, entry)
-> dict[str, str]` (Task 3). `_render_endpoints_for_status(provider, instance) -> str` (Task 2).
`_adopt_or_age_out(ledger, provider, entry, *, now, boot_timeout_s) -> None` (Task 6).
`_selection_tags(cluster_name) -> dict[str, str]` and `_launched_resources(cluster_name)` (Task 7).
`_estimate_hourly_rate(task) -> float | None` (Task 8). The `ports` tag key is the string `"ports"`
with a comma-joined value everywhere — the same key RunPod already writes.

**Where an implementer must look before typing.** Three signatures are asserted from reading HEAD
and must be re-confirmed rather than assumed: `SkyPilotProvider.__init__`'s seam keyword names
(`ssh_spawn` / `port_allocator` / `sleep`), `RateCapExceeded.__init__`'s parameters in
`core/errors.py`, and the attribute path to `max_usd_per_hr` on `InstanceSpec.placement`.
