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

from kinoforge.core.interfaces import Instance, InstanceSpec, Launch, Placement
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
    """Minimal sky stand-in: records launches + catalog reads, answers status/down."""

    class Task:
        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> dict[str, Any]:
            return config

    def __init__(self) -> None:
        self.downed: list[str] = []
        self.catalog_calls: list[dict[str, Any]] = []

    def launch(self, task: Any, **kwargs: Any) -> None:
        return None

    def list_accelerators(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        """Answer the S5 pre-launch cost estimate with a PRICED catalog.

        Not scenery. ``create_instance`` reads this whenever
        ``placement.max_usd_per_hr > 0``, which is every spec here (Placement
        defaults to $2.20). A fake without it makes the estimate unreadable, so
        all 24 tests in this file silently run the degraded
        "launch anyway and rely on the post-launch readback" branch and the
        Task-8 seam is never exercised at all.

        $1.20 is deliberately UNDER the default cap: these tests are about
        tunnels and endpoint shape, so the estimate must pass and let the
        launch proceed. The refusal direction is
        ``tests/providers/test_skypilot_prelaunch_estimate.py``'s subject.
        """
        self.catalog_calls.append(dict(kwargs))
        return {
            "A100": [
                {
                    "accelerator_name": "A100",
                    "vram_gb": 80,
                    "cuda": "12.8",
                    "price": 1.20,
                }
            ]
        }

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
        # Name the accelerator so _select_accelerator takes the "operator
        # named it" path — Placement's default min_vram_gb=48 would otherwise
        # route selection through the catalog, which is incidental to what
        # this file tests (tunnel/endpoint shape). The catalog is still read
        # once per create, by the S5 pre-launch cost estimate; _FakeSky
        # answers that with a priced record under the default cap.
        placement=Placement(accelerators=("A100",)),
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
    """A spec with no launch opens nothing, and says so in its tags.

    Bug caught: tunnelling a cluster with no server makes every CPU-only
    launch depend on ssh reachability it never needed.

    The ``ports`` tag is gated on exactly the same condition as the tunnel
    loop, and that is what the second assertion pins. ``_ports_for`` reads
    that tag to decide which forwards ``ensure_endpoints`` should rebuild, so
    writing it unconditionally means a later warm attach to a server-less
    cluster spawns ssh forwards to remote ports nothing is listening on and
    reports them as working endpoints.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    inst = provider.create_instance(_spec(ports=("8000",), launch=None))
    assert inst.endpoints == {}
    assert spawns == []
    assert "ports" not in inst.tags
    # And the consequence, end to end: nothing to rebuild, nothing spawned.
    assert provider.ensure_endpoints(inst) == {}
    assert spawns == []


def test_create_prices_the_launch_before_it_launches() -> None:
    """The S5 pre-launch estimate arm is LIVE in this file's fakes.

    Bug caught: a ``_FakeSky`` without ``list_accelerators``. ``create_instance``
    reads the catalog whenever ``placement.max_usd_per_hr > 0``, and an
    ``AttributeError`` there is swallowed into "estimate unreadable" — so every
    other test in this file would go on passing while quietly exercising the
    degraded branch instead of the real one. This is the tripwire for that.
    """
    sky = _FakeSky()
    provider = _provider(sky, [])
    provider.create_instance(
        _spec(ports=("8000",), launch=Launch(("python", "-m", "srv")))
    )
    assert sky.catalog_calls, "the pre-launch cost estimate never read a catalog"
    # Narrowed to what is actually being booked: the whole-INSTANCE price of a
    # 1-accelerator record is the only number comparable to the cap.
    assert sky.catalog_calls[0]["name_filter"] == "A100"
    assert sky.catalog_calls[0]["quantity_filter"] == 1


def test_a_reused_tunnel_survives_a_later_calls_spawn_failure() -> None:
    """A REUSED forward belongs to the call that opened it, not to this one.

    ``cluster_name`` is stable across calls, so a second ``create_instance``
    for one cluster meets the live forwards a first, already-succeeded call
    opened. ``_ensure_tunnel`` reuses them rather than respawning.

    Bug caught: recording reused ports alongside freshly spawned ones in the
    failure-cleanup list. The second call's failure on port 8001 then kills
    port 8000's tunnel — which the first call's caller is happily using — and,
    the cluster now holding no tunnel at all, best-effort ``sky.down``s
    compute that is still in use. The sibling test above covers DISJOINT port
    sets; only an overlapping one reaches the reuse path.
    """
    sky = _FakeSky()
    made: dict[int, _FakeProc] = {}

    def _spawn(cluster: str, local_port: int, remote_port: int) -> _FakeProc:
        if remote_port == 8001:
            raise OSError("ssh: connect failed")
        proc = _FakeProc()
        made[remote_port] = proc
        return proc

    ports = iter([50001, 50002, 50003])
    provider = SkyPilotProvider(
        sky,
        ssh_spawn=_spawn,
        port_allocator=lambda: next(ports),
        sleep=lambda _s: None,
    )

    first = provider.create_instance(
        _spec(ports=("8000",), launch=Launch(("python", "-m", "srv")))
    )
    assert first.endpoints == {"8000": "http://127.0.0.1:50001"}

    from kinoforge.core.errors import ProvisionFailed

    # Same run id, and port 8000 again — so 8000 is REUSED, not spawned.
    with pytest.raises(ProvisionFailed, match="8001"):
        provider.create_instance(
            _spec(ports=("8000", "8001"), launch=Launch(("python", "-m", "srv")))
        )

    assert made[8000].terminated is False, (
        "a reused tunnel from an earlier successful call was torn down"
    )
    assert provider._tunnels[first.id]["8000"].proc is made[8000]  # noqa: SLF001
    assert sky.downed == [], "a cluster still serving a live tunnel was downed"


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


def test_second_create_under_the_same_run_id_spares_the_first_calls_tunnel() -> None:
    """A second launch's spawn failure never tears down a sibling call's tunnel.

    Bug caught: ``cluster_name`` is stable across calls (derived from
    ``spec.run_id``), so popping the *whole* per-cluster tunnel dict on a
    create failure — Task 0's shape, before this fix — kills tunnels a
    prior, already-succeeded ``create_instance`` call left live for the same
    cluster, and the failure path's best-effort ``sky.down`` then tears down
    the compute backing them too. Only tunnels opened by *this* call may be
    killed; the cluster may only be downed when no tunnel this process holds
    for it survives the failure.
    """
    sky = _FakeSky()
    made: dict[int, _FakeProc] = {}

    def _spawn(cluster: str, local_port: int, remote_port: int) -> _FakeProc:
        if remote_port == 8002:
            raise OSError("ssh: connect failed")
        proc = _FakeProc()
        made[remote_port] = proc
        return proc

    ports = iter([50001, 50002, 50003])
    provider = SkyPilotProvider(
        sky,
        ssh_spawn=_spawn,
        port_allocator=lambda: next(ports),
        sleep=lambda _s: None,
    )

    first = provider.create_instance(
        _spec(ports=("8000",), launch=Launch(("python", "-m", "srv")))
    )
    assert first.endpoints == {"8000": "http://127.0.0.1:50001"}

    from kinoforge.core.errors import ProvisionFailed

    with pytest.raises(ProvisionFailed, match="8002"):
        provider.create_instance(
            _spec(ports=("8001", "8002"), launch=Launch(("python", "-m", "srv")))
        )

    # The first call's tunnel is untouched: still tracked, still alive.
    assert provider._tunnels[first.id]["8000"].proc is made[8000]  # noqa: SLF001
    assert made[8000].terminated is False
    # The second call's own new tunnel (8001) is killed on the 8002 failure,
    # same as the existing single-call partial-failure contract.
    assert made[8001].terminated is True
    # The cluster is still live — sky.down must NOT be called, because a
    # tunnel from the first call still depends on it.
    assert sky.downed == []


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


def test_endpoints_is_pure_and_reports_only_live_local_state() -> None:
    """A read never spawns and never lies about a cluster it does not hold.

    Bug caught: returning ``{"ssh": "ssh://<cluster>"}`` — an HTTP client
    handed that string fails with an unusable error (finding F11), and a
    status read that spawns ssh leaks a process per invocation.

    The spawn recorder is NAMED and asserted. Passing an anonymous ``[]`` (as
    this did) throws away the only evidence of the purity half of the claim,
    so an ``endpoints()`` that quietly ensured a tunnel — the exact regression
    ``ensure_endpoints`` was split out to prevent — would pass: it would
    return a map for a stranger and only the ``== {}`` line would catch it,
    and only by accident.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
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
    assert spawns == [], "the pure read spawned an ssh tunnel"


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


def test_ensure_endpoints_falls_back_to_endpoint_keys_when_the_ports_tag_is_empty() -> (
    None
):
    """An unconditionally-written empty ``tags["ports"]`` is treated as absent.

    Bug caught: Task 0 writes the ``"ports"`` tag on every created Instance,
    even a spec with no declared ports — so ``tags["ports"] == ""`` is the
    COMMON shape for that case, not an edge case. A ``_ports_for`` that
    treated ``""`` as "one port named the empty string" (rather than falling
    through to the numeric-endpoint-key fallback) would silently try to open
    a tunnel to remote port ``""``.
    """
    spawns: list[tuple[str, int, int]] = []
    provider = _provider(_FakeSky(), spawns)
    recorded = Instance(
        id="kf-empty-tag",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://127.0.0.1:1"},  # a dead port from last run
        tags={"ports": ""},
        cost_rate_usd_per_hr=0.0,
    )
    assert provider.ensure_endpoints(recorded) == {
        "8000": "http://127.0.0.1:50001",
    }
    assert [(c, r) for c, _l, r in spawns] == [("kf-empty-tag", 8000)]
