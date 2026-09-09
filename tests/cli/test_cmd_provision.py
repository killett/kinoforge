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
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

import kinoforge._adapters  # noqa: F401 — side-effect: register builtins
from kinoforge.cli._commands import _cmd_provision
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.errors import CapacityError, ProvisionTimeout
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
        alive: bool = True,
        initial_status: str = "ready",
        get_instance_raises: BaseException | None = None,
        destroy_raises: BaseException | None = None,
    ) -> None:
        self.specs: list[InstanceSpec] = []
        self.get_instance_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self._result = result
        self._raises = raises
        self._on_create = on_create
        self._alive = alive
        self._initial_status = initial_status
        self._get_instance_raises = get_instance_raises
        self._destroy_raises = destroy_raises

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
            status=self._initial_status,
            created_at=0.0,
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=0.2,
        )

    def get_instance(self, instance_id: str) -> Instance:
        """Answer the refusal path's reconcile probe, and the readiness poll.

        ``alive=False`` raises ``KeyError``, which is the ONLY outcome the
        reconciler treats as "this instance definitively no longer exists".
        The created instance itself is returned ``status="ready"`` by default,
        so the provision loop never re-fetches it — a call here always comes
        from the reconcile UNLESS a test sets ``initial_status`` to something
        else, in which case this same method answers the readiness poll too.
        ``get_instance_raises`` simulates a readiness-poll transport failure.
        """
        self.get_instance_calls.append(instance_id)
        if self._get_instance_raises is not None:
            raise self._get_instance_raises
        if not self._alive:
            raise KeyError(instance_id)
        return Instance(
            id=instance_id,
            provider="runpod",
            status="ready",
            created_at=0.0,
            cost_rate_usd_per_hr=0.2,
        )

    def destroy_instance(self, instance_id: str) -> None:
        """Record the teardown attempt; optionally fail it too."""
        self.destroy_calls.append(instance_id)
        if self._destroy_raises is not None:
            raise self._destroy_raises


def _write_cfg(tmp_path: Path, cfg_text: str = _CFG) -> tuple[Path, Path]:
    """Materialise the config + state dir a ``provision`` invocation needs.

    Returns:
        ``(cfg_path, state_dir)``.
    """
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(cfg_text)
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


# ---------------------------------------------------------------------------
# A stale row must not refuse forever, and the refusal must name a recovery
# that works.
#
# `_recorded_instance_for_key` reads `ledger.entries()` raw, so a pod that died
# provider-side leaves a row that blocks every later `provision` — while the
# message offered only `destroy --id`, which fails against a pod that is
# already gone.
# ---------------------------------------------------------------------------


def _record_row_for_this_key(cfg_path: Path, state_dir: Path, instance_id: str) -> None:
    """Seed a ledger row carrying this cfg's capability key."""
    _ctx_for(cfg_path, state_dir).ledger().record(
        Instance(
            id=instance_id,
            provider="runpod",
            status="ready",
            created_at=0.0,
            tags={"kinoforge_key": _key_hash_of(cfg_path), "kinoforge_engine": "fake"},
            cost_rate_usd_per_hr=0.2,
        ),
        max_age_s=3600,
    )


def test_provision_proceeds_when_the_recorded_instance_is_gone_provider_side(
    tmp_path: Path,
) -> None:
    """A dead pod's row must not refuse provisioning forever.

    Bug caught: the refusal read the ledger with no reconciliation, so a row
    whose pod the provider no longer has blocked `provision` permanently — and
    the only recovery it named (`destroy --id`) fails against a pod that does
    not exist. The row must be dropped and the create must happen.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    _record_row_for_this_key(cfg_path, state_dir, "pod-died-provider-side")
    provider = _FakeProvider(alive=False)

    rc, provision_mock = _invoke_provision(cfg_path, state_dir, provider)

    assert rc == 0, "a stale row still refuses"
    assert provider.get_instance_calls == ["pod-died-provider-side"], (
        "the refusal did not re-check the row against the provider"
    )
    assert len(provider.specs) == 1
    provision_mock.assert_called_once()
    assert [e["id"] for e in _ctx_for(cfg_path, state_dir).ledger().entries()] == [
        "pod-spy"
    ], "the dead row survived the reconcile"


def test_provision_still_refuses_when_the_probe_cannot_confirm_the_pod_is_gone(
    tmp_path: Path,
) -> None:
    """Uncertainty is not permission to double-book.

    Bug caught: treating any probe outcome other than "definitely alive" as
    "gone" would turn a transient auth/transport fault into a second billed
    GPU — the exact failure the refusal exists to prevent. Only a ``KeyError``
    (the provider's authoritative "no such instance") may clear the row.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    _record_row_for_this_key(cfg_path, state_dir, "pod-uncertain")
    provider = _FakeProvider()

    rc, provision_mock = _invoke_provision(cfg_path, state_dir, provider)

    assert rc != 0
    assert provider.specs == [], "an unconfirmed row let a second instance through"
    provision_mock.assert_not_called()
    assert [e["id"] for e in _ctx_for(cfg_path, state_dir).ledger().entries()] == [
        "pod-uncertain"
    ]


def test_the_refusal_names_a_recovery_that_works_on_a_stale_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The message must name the commands that clear a row, not only destroy.

    Bug caught: `destroy --id <id>` is the only recovery offered, and it fails
    against a pod that no longer exists — leaving an operator with a permanent
    refusal and no documented way out. Providers the reconcile above cannot
    probe (modal) reach exactly this branch.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    _record_row_for_this_key(cfg_path, state_dir, "pod-uncertain")

    rc, _ = _invoke_provision(cfg_path, state_dir, _FakeProvider())

    assert rc != 0
    err = capsys.readouterr().err
    assert "kinoforge destroy --id pod-uncertain" in err
    assert "kinoforge list" in err, "no recovery named for a row the probe cannot clear"
    assert "kinoforge reap --apply" in err


# ---------------------------------------------------------------------------
# Mode routing on the `provision` path.
#
# Replacing the hand-rolled InstanceSpec with `build_instance_spec` (commit
# `8191bd1b`) newly stamps `tags["mode"]` from `compute.mode`, and RunPod
# branches on exactly that tag. A config with `mode: serverless` therefore
# stopped taking the pod branch on `provision` — a different billable resource
# shape, arrived at as a side effect of a durability change. The direction is
# right (it is what `deploy` has always done since S2), so it is pinned here
# rather than left to be rediscovered from a bill.
# ---------------------------------------------------------------------------

_CFG_SERVERLESS = _CFG.replace(
    "  image: runpod/pytorch:2.4.0\n",
    "  image: runpod/pytorch:2.4.0\n  mode: serverless\n",
)

_CFG_DIAGNOSTIC = _CFG + "diagnostic_mode: true\n"

#: The S4 catalog read a create must answer before it reaches either branch.
_GPU_TYPES: dict[str, object] = {
    "data": {
        "gpuTypes": [
            {
                "id": "NVIDIA RTX A4000",
                "displayName": "NVIDIA RTX A4000",
                "memoryInGb": 16,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": 0.32,
                    "uninterruptablePrice": 0.32,
                },
            }
        ]
    }
}


def _catalog_post(_url: str, body: dict[str, object]) -> dict[str, object]:
    """Answer the catalog query; every other call gets an empty response."""
    if "gpuTypes" in str(body.get("query", "")):
        return _GPU_TYPES
    return {}


def _spec_from_provision(tmp_path: Path, cfg_text: str) -> InstanceSpec:
    """Return the spec ``provision`` hands the provider for *cfg_text*."""
    cfg_path, state_dir = _write_cfg(tmp_path, cfg_text)
    provider = _FakeProvider()
    rc, _ = _invoke_provision(cfg_path, state_dir, provider)
    assert rc == 0
    assert len(provider.specs) == 1
    return provider.specs[0]


def _branch_taken(spec: InstanceSpec) -> tuple[bool, bool]:
    """Feed *spec* to a real RunPodProvider; return ``(serverless, pod)``."""
    from kinoforge.providers.runpod import RunPodProvider

    provider = RunPodProvider(http_post=_catalog_post, http_get=lambda _url: {})
    with (
        patch.object(RunPodProvider, "_create_serverless") as serverless,
        patch.object(RunPodProvider, "_create_pod") as pod,
    ):
        serverless.return_value = Mock(id="sl-1")
        pod.return_value = Mock(id="pod-1")
        provider.create_instance(spec)
    return bool(serverless.called), bool(pod.called)


def test_provision_stamps_the_compute_mode_tag(tmp_path: Path) -> None:
    """`compute.mode` reaches the spec `provision` books with.

    Bug caught: the hand-rolled InstanceSpec carried no tags at all, so RunPod
    fell back to its own ``.get("mode", "pod")`` and `mode: serverless` was
    silently ignored on this command while `deploy` honoured it — the two
    commands agreeing only by coincidence.
    """
    spec = _spec_from_provision(tmp_path, _CFG_SERVERLESS)
    assert spec.tags["mode"] == "serverless"


def test_provision_default_mode_is_stamped_as_pod(tmp_path: Path) -> None:
    """The default arrives as a written tag, not an absent key.

    Bug caught: a `setdefault`/`update` mix-up that stamped "serverless" for
    every cfg would pass the serverless test alone and move every pod config
    onto a branch that bills differently.
    """
    spec = _spec_from_provision(tmp_path, _CFG)
    assert spec.tags["mode"] == "pod"


def test_provision_serverless_spec_reaches_the_serverless_branch(
    tmp_path: Path,
) -> None:
    """The stamped tag routes, rather than merely being present.

    Bug caught: reading the tag name off the code proves nothing about which
    resource RunPod books. This runs the spec `provision` produced through the
    real `create_instance` and captures WHICH branch executed.
    """
    serverless, pod = _branch_taken(_spec_from_provision(tmp_path, _CFG_SERVERLESS))
    assert serverless
    assert not pod


def test_provision_default_spec_reaches_the_pod_branch(tmp_path: Path) -> None:
    """The mirror: the 45 pod configs must not start routing elsewhere."""
    serverless, pod = _branch_taken(_spec_from_provision(tmp_path, _CFG))
    assert pod
    assert not serverless


def test_provision_honours_diagnostic_mode_in_the_cfg(tmp_path: Path) -> None:
    """`diagnostic_mode: true` now overlays `restart_policy: never` here too.

    The same swap to `build_instance_spec` brought C28's diagnostic overlay
    onto this path, where the hand-rolled spec had never applied it. Note the
    trigger: `--diagnostic-mode` is a `deploy`-only CLI flag, so on `provision`
    the only way to set it is the config file.

    Bug caught: a diagnostic provision whose container restarts on failure
    destroys the boot log the flag exists to preserve.
    """
    spec = _spec_from_provision(tmp_path, _CFG_DIAGNOSTIC)
    assert spec.backend_options["runpod"]["restart_policy"] == "never"


def test_provision_leaves_restart_policy_alone_without_diagnostic_mode(
    tmp_path: Path,
) -> None:
    """The overlay is opt-in; an ordinary provision keeps RunPod's default.

    Bug caught: applying `restart_policy: never` unconditionally would stop
    every pod recovering from a transient boot failure.
    """
    spec = _spec_from_provision(tmp_path, _CFG)
    assert "restart_policy" not in spec.backend_options.get("runpod", {})


# ---------------------------------------------------------------------------
# U21 — everything after the provisional-row collapse ran unguarded: a raise
# from the readiness poll or the provisioner left a created, billing pod with
# nothing tearing it down, and the readiness loop had no deadline at all — a
# pod stuck in "starting" spun forever at 2s/turn. The fix reuses
# ``orchestrator.deploy``'s destroy-on-error shape verbatim (log naming the
# instance + the error, attempt destroy, log a SECOND failure separately
# without masking the first, re-raise the original) and its bounded
# ``_wait_for_provider_ready`` helper for the readiness poll.
# ---------------------------------------------------------------------------


class _NeverReadyProvider:
    """A pod that never leaves "starting".

    ``poll_cap`` fails the test fast with a clear ``AssertionError`` instead
    of hanging forever if the readiness loop regresses to unbounded — the
    exact bug this test exists to catch.
    """

    name = "runpod"

    def __init__(self, *, poll_cap: int = 5) -> None:
        self.specs: list[InstanceSpec] = []
        self.get_instance_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self._poll_cap = poll_cap

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
            id="pod-never-ready",
            provider="runpod",
            status="starting",
            created_at=0.0,
            tags=dict(spec.tags),
            cost_rate_usd_per_hr=0.2,
        )

    def get_instance(self, instance_id: str) -> Instance:
        self.get_instance_calls.append(instance_id)
        if len(self.get_instance_calls) > self._poll_cap:
            raise AssertionError(
                f"test guard: readiness loop polled more than {self._poll_cap} "
                "times — the deadline bound is not working"
            )
        return Instance(
            id=instance_id,
            provider="runpod",
            status="starting",
            created_at=0.0,
            cost_rate_usd_per_hr=0.2,
        )

    def destroy_instance(self, instance_id: str) -> None:
        self.destroy_calls.append(instance_id)


#: Same compute cfg as ``_CFG``, but with an instant readiness deadline so the
#: never-ready test needs no real sleep at all: the deadline is already in
#: the past on the very first check, before any poll or sleep happens.
_CFG_INSTANT_BOOT_TIMEOUT = _CFG.replace(
    "  lifecycle:\n    budget: 1.0\n",
    "  lifecycle:\n    budget: 1.0\n    boot_timeout: 0\n",
)


def test_provision_destroys_pod_when_readiness_poll_raises(tmp_path: Path) -> None:
    """A raise from ``provider.get_instance`` during the readiness poll must
    destroy the pod before the exception propagates.

    Bug caught: the pre-fix ``while instance.status != "ready":`` loop had no
    guard at all — this raise walked straight out of ``_cmd_provision`` with
    the pod created, paid-for, and never torn down. A test that only asserted
    the exception propagated would pass on that unfixed code; asserting
    ``destroy_calls`` is what discriminates the fix.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    ctx = _ctx_for(cfg_path, state_dir)
    provider = _FakeProvider(
        initial_status="starting",
        get_instance_raises=RuntimeError("get_instance transport error"),
    )

    with (
        patch("kinoforge._adapters.build_provider_for", return_value=provider),
        patch("kinoforge.core.provisioner.provision"),
        pytest.raises(RuntimeError, match="get_instance transport error"),
    ):
        _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)

    assert provider.destroy_calls == ["pod-spy"], (
        f"expected the pod destroyed after the readiness poll raised, got "
        f"destroy_calls={provider.destroy_calls!r}"
    )


def test_provision_destroys_pod_when_provisioner_raises(tmp_path: Path) -> None:
    """A raise from ``provision(...)`` (the weight-download / bootstrap step)
    after the pod is ready must also destroy the pod.

    Bug caught: an implementation that wraps ONLY the readiness loop in
    try/except (and leaves the ``provision(...)`` call after it unguarded,
    matching the pre-fix code's structure) would let this raise propagate
    with the pod never destroyed. This discriminates the two failure sites
    the acceptance criteria name explicitly: "the readiness poll or the
    provisioner".
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    ctx = _ctx_for(cfg_path, state_dir)
    provider = _FakeProvider()  # ready immediately — no readiness loop at all

    with (
        patch("kinoforge._adapters.build_provider_for", return_value=provider),
        patch(
            "kinoforge.core.provisioner.provision",
            side_effect=RuntimeError("weight download failed"),
        ),
        pytest.raises(RuntimeError, match="weight download failed"),
    ):
        _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)

    assert provider.destroy_calls == ["pod-spy"], (
        f"expected the pod destroyed after the provisioner raised, got "
        f"destroy_calls={provider.destroy_calls!r}"
    )


def test_provision_never_ready_pod_hits_deadline_instead_of_looping(
    tmp_path: Path,
) -> None:
    """A pod stuck in "starting" must not spin forever — it hits a deadline,
    raises naming the last status seen, and the pod is destroyed.

    Bug caught: the pre-fix loop had no timeout and no iteration cap at all —
    the same money leak as an unguarded raise, but with no exception to even
    report it. ``boot_timeout: 0`` makes the deadline already-elapsed on the
    very first check, so the fixed code needs no real sleep to prove this;
    the unfixed code ignores ``boot_timeout`` entirely and keeps polling
    every 2 real seconds until ``_NeverReadyProvider``'s poll cap trips.
    """
    cfg_path, state_dir = _write_cfg(tmp_path, _CFG_INSTANT_BOOT_TIMEOUT)
    ctx = _ctx_for(cfg_path, state_dir)
    provider = _NeverReadyProvider()

    with (
        patch("kinoforge._adapters.build_provider_for", return_value=provider),
        patch("kinoforge.core.provisioner.provision") as provision_mock,
        pytest.raises(ProvisionTimeout) as excinfo,
    ):
        _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)

    message = str(excinfo.value)
    assert "pod-never-ready" in message, (
        f"error must name the instance; got {message!r}"
    )
    assert "starting" in message, (
        f"error must name the last status seen; got {message!r}"
    )
    provision_mock.assert_not_called()
    assert provider.destroy_calls == ["pod-never-ready"], (
        f"expected the pod destroyed at the deadline, got "
        f"destroy_calls={provider.destroy_calls!r}"
    )


def test_provision_reports_a_failing_destroy_without_masking_the_original_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A destroy that itself fails must not hide the original error.

    Bug caught: a bare ``except Exception: provider.destroy_instance(...);
    raise`` with no inner try/except around the destroy call would let a
    SECOND exception from ``destroy_instance`` replace the original one on
    its way out — the operator would see "destroy failed" and never learn
    what actually broke, nor whether the pod is still up. Both must reach the
    operator: the ORIGINAL error via the raised exception, the destroy
    failure via the log — mirroring ``orchestrator.deploy``'s shape exactly.
    """
    cfg_path, state_dir = _write_cfg(tmp_path)
    ctx = _ctx_for(cfg_path, state_dir)
    provider = _FakeProvider(destroy_raises=RuntimeError("destroy also failed"))

    with (
        patch("kinoforge._adapters.build_provider_for", return_value=provider),
        patch(
            "kinoforge.core.provisioner.provision",
            side_effect=RuntimeError("weight download failed"),
        ),
        caplog.at_level(logging.ERROR, logger="kinoforge.cli._commands"),
        pytest.raises(RuntimeError, match="weight download failed") as excinfo,
    ):
        _cmd_provision(argparse.Namespace(config=str(cfg_path)), ctx)

    # The ORIGINAL error, not the destroy error, is what propagated.
    assert "destroy also failed" not in str(excinfo.value), (
        "the destroy failure replaced the original error on its way out"
    )
    assert provider.destroy_calls == ["pod-spy"]

    # The destroy failure must still reach the operator — via the log, since
    # only one exception can propagate.
    messages = [r.getMessage() for r in caplog.records]
    assert any("destroy also failed" in m for m in messages), (
        f"destroy failure was never logged; operator has no way to learn the "
        f"pod may still be up. Log messages: {messages!r}"
    )
    assert any("weight download failed" in m for m in messages), (
        f"original error was not logged either; log messages: {messages!r}"
    )
