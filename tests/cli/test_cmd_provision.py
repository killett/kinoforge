"""``kinoforge provision`` — InstanceSpec must carry the rendered payload.

Bug (pod ``forewgeluuy9qh``, 2026-07-03): the legacy ``_cmd_provision``
path built a bare ``InstanceSpec(image=..., offer=..., lifecycle=...)``
— no ports, no provision script, no env, no cloud_type. The pod booted
with no proxy endpoints and no bootstrap, and the subsequent
``wait_for_ready`` died with ``ProvisionFailed: ... has no endpoints``
after money was already committed.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import kinoforge._adapters  # noqa: F401 — side-effect: register builtins
from kinoforge.cli._commands import _cmd_provision
from kinoforge.cli.context import SessionContext
from kinoforge.core.interfaces import (
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Offer,
)

_CFG = (
    "compute:\n"
    "  provider: runpod\n"
    "  image: runpod/pytorch:2.4.0\n"
    "  cloud_type: secure\n"
    "  requirements:\n"
    "    min_vram_gb: 16\n"
    '    min_cuda: "12.4"\n'
    "    max_usd_per_hr: 0.50\n"
    "    disk_gb: 40\n"
    "  lifecycle:\n"
    "    budget: 1.0\n"
    "engine:\n"
    "  kind: fake\n"
    "  precision: fp16\n"
    "models:\n"
    "  - ref: hf:org/m\n"
    "    kind: base\n"
    "    target: checkpoints\n"
)


class _SpyProvider:
    """Records the created spec; returns a ready instance immediately."""

    def __init__(self) -> None:
        self.specs: list[InstanceSpec] = []

    def find_offers(self, reqs: HardwareRequirements) -> list[Offer]:
        return [
            Offer(
                id="NVIDIA RTX A5000",
                gpu_type="NVIDIA RTX A5000",
                vram_gb=24,
                cuda="12.4",
                cost_rate_usd_per_hr=0.2,
            )
        ]

    def create_instance(self, spec: InstanceSpec) -> Instance:
        self.specs.append(spec)
        return Instance(
            id="pod-spy",
            provider="runpod",
            status="ready",
            created_at=0.0,
            cost_rate_usd_per_hr=0.2,
            endpoints={"8000": "https://pod-spy-8000.proxy.runpod.net"},
        )

    def get_instance(self, instance_id: str) -> Instance:
        raise AssertionError("ready instance must not be re-fetched")


def _run(tmp_path: Path) -> _SpyProvider:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    spy = _SpyProvider()
    with (
        patch("kinoforge._adapters.build_provider_for", return_value=spy),
        # Weight download is out of scope — the behaviour under test is
        # the InstanceSpec wire shape; the hf: ref would hit the network.
        patch("kinoforge.core.provisioner.provision"),
    ):
        rc = _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)
    assert rc == 0
    assert len(spy.specs) == 1
    return spy


def test_provision_spec_carries_rendered_ports(tmp_path: Path) -> None:
    """Bug caught: no ports → RunPod exposes no proxy endpoints and
    every downstream ready-URL construction fails post-spend."""
    spy = _run(tmp_path)
    assert tuple(spy.specs[0].ports) == ("8000",)


def test_provision_spec_carries_bootstrap_script(tmp_path: Path) -> None:
    """Bug caught: no provision_script → pod boots the bare image and
    the engine server never starts."""
    spy = _run(tmp_path)
    assert spy.specs[0].provision_script == "echo fake"


def test_provision_spec_threads_cloud_type(tmp_path: Path) -> None:
    """Bug caught: cfg pins secure but the legacy path never copied it —
    long provisions land on community hosts that delete zero-volume
    pods on interruption (2026-07-03 incident class)."""
    spy = _run(tmp_path)
    assert spy.specs[0].cloud_type == "secure"
