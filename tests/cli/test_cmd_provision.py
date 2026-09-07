"""``kinoforge provision`` — InstanceSpec must carry the rendered payload.

Bug (pod ``forewgeluuy9qh``, 2026-07-03): the legacy ``_cmd_provision``
path built a bare ``InstanceSpec(image=..., offer=..., lifecycle=...)``
— no ports, no provision script, no env, no backend options. The pod booted
with no proxy endpoints and no bootstrap, and the subsequent
``wait_for_ready`` died with ``ProvisionFailed: ... has no endpoints``
after money was already committed.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

import kinoforge._adapters  # noqa: F401 — side-effect: register builtins
from kinoforge.cli._commands import _cmd_provision
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import (
    Instance,
    InstanceSpec,
    Offer,
    Placement,
    combine_steps,
)

_CFG = (
    "compute:\n"
    "  provider: runpod\n"
    "  image: runpod/pytorch:2.4.0\n"
    "  backend_options:\n"
    "    runpod:\n"
    "      cloud_type: secure\n"
    "  placement:\n"
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

    def find_offers(self, reqs: Placement) -> list[Offer]:
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
    """Bug caught: no setup steps → pod boots the bare image and
    the engine server never starts."""
    spy = _run(tmp_path)
    assert combine_steps(spy.specs[0].setup_steps) == "echo fake"


def test_provision_spec_threads_backend_options(tmp_path: Path) -> None:
    """Bug caught: cfg pins secure but the legacy path never copied it —
    long provisions land on community hosts that delete zero-volume
    pods on interruption (2026-07-03 incident class)."""
    spy = _run(tmp_path)
    assert spy.specs[0].backend_options["runpod"]["cloud_type"] == "secure"


# ---------------------------------------------------------------------------
# Durability: `provision` must never leave a billing instance no kinoforge
# command can name, and must not book a second one for a key that has one.
#
# Incident (2026-09-06, Modal): ``_cmd_provision`` called ``create_instance``
# and touched neither ``ctx.ledger()`` nor ``ctx.store()``. Modal returned an
# empty instance id, the CLI printed ``provisioned: instance=''`` and exited 0,
# and the running app — named with a bare ``kinoforge-`` prefix — existed in no
# ledger at all. $0.13 of unrecoverable spend, reapable only via a raw
# ``modal app stop``.
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A ``create_instance`` seam under the test's control.

    ``on_create`` fires at the exact moment money would be committed, which is
    what lets a test pin ORDERING rather than merely end state.
    """

    name = "runpod"

    def __init__(
        self,
        *,
        result: Instance | None = None,
        raises: BaseException | None = None,
        on_create: Callable[[InstanceSpec], None] | None = None,
    ) -> None:
        self.specs: list[InstanceSpec] = []
        self._result = result
        self._raises = raises
        self._on_create = on_create

    def find_offers(self, reqs: Placement) -> list[Offer]:
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
        if self._on_create is not None:
            self._on_create(spec)
        if self._raises is not None:
            raise self._raises
        return self._result or Instance(
            id="pod-spy",
            provider="runpod",
            status="ready",
            created_at=0.0,
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=0.2,
        )

    def get_instance(self, instance_id: str) -> Instance:
        raise AssertionError("ready instance must not be re-fetched")


def _write_cfg(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise the config + state dir a ``provision`` invocation needs.

    Returns:
        ``(cfg_path, state_dir)``.
    """
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(_CFG)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return cfg_path, state_dir


def _ctx_for(cfg_path: Path, state_dir: Path) -> SessionContext:
    """Build a SessionContext the way ``kinoforge`` does for one invocation.

    A FRESH context per call is deliberate: it resolves the store exactly as a
    separate ``kinoforge list`` process would, so no assertion below can be
    satisfied by an in-memory object the provision invocation happens to hold.
    """
    return SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)


def _invoke_provision(
    cfg_path: Path, state_dir: Path, provider: _FakeProvider
) -> tuple[int, Mock]:
    """Run ``_cmd_provision`` against *provider*.

    Returns:
        ``(exit_code, provision_mock)`` — the mock stands in for the weight
        download, which is out of scope here and would otherwise hit the network.
    """
    ctx = _ctx_for(cfg_path, state_dir)
    with (
        patch("kinoforge._adapters.build_provider_for", return_value=provider),
        patch("kinoforge.core.provisioner.provision") as provision_mock,
    ):
        rc = _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)
    return rc, provision_mock


def _key_hash_of(cfg_path: Path) -> str:
    """Return the capability-key hash the ledger row for this cfg carries."""
    return load_config(cfg_path).capability_key().derive()[:12]


def test_provision_writes_a_provisional_row_before_creating_the_instance(
    tmp_path: Path,
) -> None:
    """The durable row must exist BEFORE money is committed.

    Bug caught: ``_cmd_provision`` called ``create_instance`` with no ledger
    write at all, so a crash between create and return left a billing pod that
    ``kinoforge list`` could not see — the 2026-09-06 Modal $0.13 leak,
    recoverable only via a raw ``modal app stop``. A row written AFTER create
    returns also fails this test, which is the point: the ordering is the fix.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    seen: list[list[dict[str, Any]]] = []

    provider = _FakeProvider(
        on_create=lambda spec: seen.append(
            _ctx_for(cfg_path, state_dir).ledger().entries()
        )
    )
    rc, _ = _invoke_provision(cfg_path, state_dir, provider)

    assert rc == 0
    assert seen and seen[0], (
        "no row was readable through SessionContext.ledger() while "
        "create_instance was running — provision committed money with nothing "
        "durable naming what it was about to book"
    )
    row = seen[0][0]
    assert row["tags"]["kf_launch_phase"] == "launching"
    assert row["id"] == provider.specs[0].run_id, (
        "the row must be keyed by the client-side run id the provider names the "
        "resource with — that name is what cli/_reconcile adopts it by"
    )
    assert row["tags"]["kf_run_id"] == provider.specs[0].run_id
    assert row["provider"] == "runpod"
    assert provider.specs[0].run_id.startswith("kinoforge-provision-"), (
        "the run id becomes the provider-side resource name, so it must say "
        "which command booked it — a kinoforge-deploy-* app booked by "
        "provision sends the operator looking for a deploy that never ran"
    )


def test_provision_collapses_the_provisional_row_onto_the_real_one(
    tmp_path: Path,
) -> None:
    """A successful provision leaves exactly one row: the real instance.

    Bug caught: writing the provisional row and never collapsing it, which
    leaves a permanent ``launching`` ghost whose ``est_spend`` inflates forever
    beside the live pod (cli/_reconcile's "$210 phantom pod"); or collapsing it
    without recording the real row, which is the pre-fix invisibility again.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)

    rc, _ = _invoke_provision(cfg_path, state_dir, _FakeProvider())

    assert rc == 0
    entries = _ctx_for(cfg_path, state_dir).ledger().entries()
    assert [e["id"] for e in entries] == ["pod-spy"]
    assert "kf_launch_phase" not in entries[0]["tags"]
    assert entries[0]["tags"]["kinoforge_key"] == _key_hash_of(cfg_path), (
        "the real row must carry the capability key, or the double-book refusal "
        "cannot recognise provision's own instance on the next invocation"
    )


def test_provision_keeps_the_provisional_row_when_create_raises(
    tmp_path: Path,
) -> None:
    """A failed create must leave the row for the reconciler (ruling C1).

    Bug caught: deleting the row on any raise. A raise is not evidence the
    provider booked nothing — a timeout or a Ctrl-C lands here while the
    provider's API server goes on creating the resource — so deleting it
    strands a billing pod permanently, which is the F12 hole in reverse.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    boom = RuntimeError("provider API timed out mid-create")

    with pytest.raises(RuntimeError, match="timed out mid-create"):
        _invoke_provision(cfg_path, state_dir, _FakeProvider(raises=boom))

    entries = _ctx_for(cfg_path, state_dir).ledger().entries()
    assert len(entries) == 1, (
        f"expected the launching row to survive, got {[e['id'] for e in entries]}"
    )
    assert entries[0]["tags"]["kf_launch_phase"] == "launching"


def test_provision_forgets_the_provisional_row_when_nothing_was_booked(
    tmp_path: Path,
) -> None:
    """``CapacityError`` PROVES no resource exists, so the row must go.

    Bug caught: the mirror of the test above — never forgetting anything. A row
    kept for a create that booked nothing is a phantom whose ``est_spend``
    climbs on wall-clock forever and which the operator must ``kinoforge
    forget`` by hand.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)

    with pytest.raises(CapacityError):
        _invoke_provision(
            cfg_path, state_dir, _FakeProvider(raises=CapacityError("no A5000s"))
        )

    assert _ctx_for(cfg_path, state_dir).ledger().entries() == []


def test_provision_refuses_an_empty_instance_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An id-less instance is not a record.

    Bug caught: Modal returned an empty id, ``_cmd_provision`` printed
    ``provisioned: instance=''``, exited 0, and recorded nothing — the id needed
    to reap the running app never existed. Exiting 0 on an unnameable instance
    is the failure; so is recording a real row keyed by ``''``.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    idless = Instance(id="", provider="modal", status="ready", created_at=0.0, tags={})

    rc, provision_mock = _invoke_provision(
        cfg_path, state_dir, _FakeProvider(result=idless)
    )

    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err.lower() or "no id" in err.lower(), (
        f"stderr must say the provider returned no usable id; got {err!r}"
    )
    provision_mock.assert_not_called()

    entries = _ctx_for(cfg_path, state_dir).ledger().entries()
    assert len(entries) == 1, (
        f"expected only the launching row, got {[e['id'] for e in entries]}"
    )
    assert entries[0]["tags"]["kf_launch_phase"] == "launching"
    assert entries[0]["id"], "a row with an empty id can be neither found nor forgotten"
    assert entries[0]["id"] in err, (
        "the operator's only handle on the resource is the run id the provider "
        "named it with, so the error must print it"
    )


def test_provision_refuses_when_an_instance_for_this_key_already_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``provision`` implies re-provision, not an unconditional second create.

    Bug caught: ``_cmd_provision`` created unconditionally, so a second
    invocation against a config whose instance was already up booked — and
    billed for — a duplicate GPU, with the operator's only clue being two rows
    in ``kinoforge list``.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    _ctx_for(cfg_path, state_dir).ledger().record(
        Instance(
            id="pod-already-live",
            provider="runpod",
            status="ready",
            created_at=0.0,
            tags={"kinoforge_key": _key_hash_of(cfg_path), "kinoforge_engine": "fake"},
            cost_rate_usd_per_hr=0.2,
        ),
        max_age_s=3600,
    )
    provider = _FakeProvider()

    rc, provision_mock = _invoke_provision(cfg_path, state_dir, provider)

    assert rc != 0
    assert provider.specs == [], "provision booked a second instance for a live key"
    provision_mock.assert_not_called()
    assert "pod-already-live" in capsys.readouterr().err, (
        "refusing without naming the instance leaves the operator no way to act"
    )
    assert [e["id"] for e in _ctx_for(cfg_path, state_dir).ledger().entries()] == [
        "pod-already-live"
    ]


def test_provision_proceeds_when_the_existing_row_is_for_another_key(
    tmp_path: Path,
) -> None:
    """Only THIS capability key blocks — a busy ledger is not a refusal.

    Bug caught: refusing whenever the ledger is non-empty, which would make
    ``provision`` unusable on any machine already running an unrelated pod and
    would pass the double-book test above for the wrong reason.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    _ctx_for(cfg_path, state_dir).ledger().record(
        Instance(
            id="pod-unrelated",
            provider="runpod",
            status="ready",
            created_at=0.0,
            tags={"kinoforge_key": "0123456789ab", "kinoforge_engine": "other"},
        ),
        max_age_s=3600,
    )

    provider = _FakeProvider()
    rc, _ = _invoke_provision(cfg_path, state_dir, provider)

    assert rc == 0
    assert len(provider.specs) == 1
    assert sorted(
        e["id"] for e in _ctx_for(cfg_path, state_dir).ledger().entries()
    ) == [
        "pod-spy",
        "pod-unrelated",
    ]
