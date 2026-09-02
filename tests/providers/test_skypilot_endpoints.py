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

from kinoforge.core.interfaces import InstanceSpec, Launch, Placement
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
        # Name the accelerator so _select_accelerator takes the "operator
        # named it" path with zero catalog read — Placement's default
        # min_vram_gb=48 would otherwise force a sky.list_accelerators()
        # call the minimal _FakeSky above does not implement, which is
        # incidental to what this file tests (tunnel/endpoint shape).
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
