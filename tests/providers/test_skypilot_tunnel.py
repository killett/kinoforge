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
        offer=Offer(
            id="cpu", gpu_type="", vram_gb=0, cuda="", cost_rate_usd_per_hr=0.0
        ),
        ports=(),
        env={},
        run_id="kf-cpu-test",
        provision_script="#!/bin/sh\ntrue\n",
        run_cmd=[],
    )


def _provider(
    sky: _FakeSky,
    *,
    spawn: Any | None = None,
    port: int = 54321,
) -> SkyPilotProvider:
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


def test_destroy_poll_is_bounded_when_cluster_never_disappears() -> None:
    # Bug caught (2026-07-08): an unbounded `while True` poll on sky.status()
    # hangs teardown forever when the cloud is slow to deprovision — observed as
    # a ~7-min --no-reuse hang that kept a Lambda A100 billing until manual kill.
    from kinoforge.providers.skypilot import _DESTROY_POLL_MAX_ITERS

    class _StuckSky(_FakeSky):
        def status(self) -> list[dict[str, Any]]:
            return [{"name": "kf-vast-test"}]  # cluster never disappears

    sleeps: list[float] = []
    sky = _StuckSky()
    provider = SkyPilotProvider(
        sky_client=sky,
        sleep=lambda s: sleeps.append(s),
        ssh_spawn=lambda *_a: _FakeProc(),
        port_allocator=lambda: 54321,
    )
    inst = provider.create_instance(_server_spec())
    provider.destroy_instance(inst.id)  # must RETURN (not hang) — down was issued

    assert 1 <= len(sleeps) <= _DESTROY_POLL_MAX_ITERS
    assert sky.downed == ["kf-vast-test"]  # sky.down was still issued


def test_last_heartbeat_returns_none() -> None:
    # Bug caught: HeartbeatLoop._tick_once calls provider.last_heartbeat every
    # tick; SkyPilotProvider lacked it -> AttributeError spam on every heartbeat
    # -enabled session (validation auto-sets heartbeat_interval_s).
    provider = SkyPilotProvider(sky_client=_FakeSky())
    assert provider.last_heartbeat("any-cluster") is None


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
        Task = _RecTask  # type: ignore[assignment]

    provider = _provider(_RecSky())
    spec = _server_spec()  # provision_script + run_cmd both set
    # Provision script whose LAST line is the Layer-Q ``exec <run_cmd>`` hand-off.
    # SkyPilot's setup phase must terminate for run to start, so create_instance
    # strips that trailing exec line before mapping the script to Task.setup.
    spec.provision_script = "#!/bin/sh\nsetup-step\nexec python -m server\n"
    provider.create_instance(spec)

    cfg = captured["cfg"]
    # setup == provision_script with the trailing exec line removed (Component C).
    assert cfg["setup"] == "#!/bin/sh\nsetup-step"
    assert "exec" not in cfg["setup"]
    assert cfg["run"] == "python -m server"  # shlex-quoted join of run_cmd
    assert cfg["resources"]["accelerators"] == "RTX_A6000:1"


class _RecTask:
    @staticmethod
    def from_yaml_config(cfg: dict[str, Any]) -> dict[str, Any]:
        _RecTask.captured = cfg  # type: ignore[attr-defined]
        return cfg


class _RecSky(_FakeSky):
    Task = _RecTask  # type: ignore[assignment]


def test_launch_pins_single_cloud_from_clouds_filter() -> None:
    # Bug caught (live, 2026-07-07): without a cloud pin on the sky Task, the
    # optimizer picks the cheapest cloud across ALL enabled infra (observed:
    # Lambda A100 $1.99), silently defeating a compute.cloud=["vast"] pin — the
    # vast proof provisioned Lambda and blew the $1 cap. find_offers' _clouds
    # filter only narrows the CATALOG; the launch must be pinned too.
    sky = _RecSky()
    provider = SkyPilotProvider(
        sky_client=sky,
        clouds=["vast"],
        ssh_spawn=lambda *_a: _FakeProc(),
        port_allocator=lambda: 54321,
    )
    provider.create_instance(_server_spec())
    assert _RecTask.captured["resources"]["cloud"] == "vast"  # type: ignore[attr-defined]


def test_launch_pins_multiple_clouds_via_any_of() -> None:
    # Bug caught: a two-cloud pin (["lambda","vast"]) collapsed to one cloud (or
    # none) lets the optimizer stray outside the operator's allowed set.
    sky = _RecSky()
    provider = SkyPilotProvider(
        sky_client=sky,
        clouds=["lambda", "vast"],
        ssh_spawn=lambda *_a: _FakeProc(),
        port_allocator=lambda: 54321,
    )
    provider.create_instance(_server_spec())
    res = _RecTask.captured["resources"]  # type: ignore[attr-defined]
    assert "cloud" not in res  # single-cloud key not used for a multi-set
    assert res["any_of"] == [{"cloud": "lambda"}, {"cloud": "vast"}]


def test_launch_no_cloud_pin_when_clouds_unset() -> None:
    # Regression guard: an unpinned provider must NOT inject a cloud key — the
    # existing multi-cloud optimizer behavior for Lambda-era configs stays.
    sky = _RecSky()
    provider = SkyPilotProvider(
        sky_client=sky,
        ssh_spawn=lambda *_a: _FakeProc(),
        port_allocator=lambda: 54321,
    )
    provider.create_instance(_server_spec())
    res = _RecTask.captured["resources"]  # type: ignore[attr-defined]
    assert "cloud" not in res
    assert "any_of" not in res
