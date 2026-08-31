"""Declaration <-> implementation parity (Brief 2, Task 1).

The regression guard that matters: adding a no-op override, or deleting a
real implementation, without editing the declaration must fail here.

Two kinds of guard live in this module and they are not interchangeable:

* **Method-identity parity** (``HEARTBEAT_READ``, ``RUNTIME_PROBE``) — the
  declaration is checked against whether an ABC default was overridden.
* **Wire-behaviour parity** (``JOB_TIMEOUT``, ``IDLE_AUTOSTOP``,
  ``ON_INSTANCE_DEADLINE``) — the declaration is checked against the bytes
  the provider actually sends. These three have no ABC method to compare
  against, so a table of expected declarations cannot cover them: it would
  be a copy of the declarations sitting beside the declarations, which is
  the string-table-beside-the-code pattern this brief exists to remove. The
  failure mode they guard is the F1 one: the declaration stays true while
  the enforcement it names quietly stops being wired.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.balance_endpoints import provider_balance_supported
from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
from kinoforge.core.interfaces import (
    ComputeProvider,
    InstanceSpec,
    Launch,
    Lifecycle,
    Offer,
    SetupStep,
)
from kinoforge.core.util_endpoints import provider_util_supported
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider

PROVIDERS = [LocalProvider, ModalProvider, RunPodProvider, SkyPilotProvider]
REGISTERED = ["local", "runpod", "skypilot", "modal"]


def _overrides(cls: type, method: str) -> bool:
    """True iff ``cls`` overrides ``ComputeProvider.<method>``."""
    return getattr(cls, method) is not getattr(ComputeProvider, method)


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_heartbeat_read_declared_iff_last_heartbeat_overridden(
    cls: type[ComputeProvider],
) -> None:
    """Catches a `return None` override added, or a real read deleted,
    without editing the declaration."""
    declared = Capability.HEARTBEAT_READ in cls.capabilities()
    assert declared is _overrides(cls, "last_heartbeat")


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_runtime_probe_declared_iff_probe_runtime_overridden(
    cls: type[ComputeProvider],
) -> None:
    """Same guard for the sweeper's probe substrate."""
    declared = Capability.RUNTIME_PROBE in cls.capabilities()
    assert declared is _overrides(cls, "probe_runtime")


def test_declared_matrix_matches_the_design_doc() -> None:
    """Pins the whole §7.1 matrix so a silent *widening* is caught.

    This is a copy of the declarations and cannot, on its own, tell whether
    any of them is true — it catches "someone added a capability" and
    nothing else. The declarations it cannot verify (``JOB_TIMEOUT``,
    ``IDLE_AUTOSTOP``, ``ON_INSTANCE_DEADLINE``) are verified against the
    wire by the three ``*_declaration_matches_*`` tests below; do not treat
    this test as covering them.
    """
    assert LocalProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.UTIL_SNAPSHOT,
            Capability.PAUSE_BILLING,
        }
    )
    assert RunPodProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.ON_INSTANCE_DEADLINE,
            Capability.JOB_TIMEOUT,
            Capability.PAUSE_BILLING,
            Capability.BALANCE_QUERY,
        }
    )
    assert ModalProvider.capabilities() == frozenset(
        {
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.IDLE_AUTOSTOP,
            Capability.ON_INSTANCE_DEADLINE,
        }
    )
    assert SkyPilotProvider.capabilities() == frozenset(
        {Capability.ON_INSTANCE_DEADLINE}
    )


def test_skypilot_idle_autostop_is_batch_only() -> None:
    """Catches a single-boolean regression: autostop is inert for server
    specs (F1) and real for batch specs (run_cmd=[])."""
    server = SkyPilotProvider.capabilities(WorkloadShape.SERVER)
    batch = SkyPilotProvider.capabilities(WorkloadShape.BATCH)
    assert Capability.IDLE_AUTOSTOP not in server
    assert Capability.IDLE_AUTOSTOP in batch


def test_runpod_job_timeout_declaration_matches_the_create_payload() -> None:
    """``JOB_TIMEOUT`` on runpod <-> ``executionTimeoutMs`` on the wire.

    RunPod is the only provider declaring JOB_TIMEOUT, and the only thing
    that makes the declaration true is this field on the serverless-endpoint
    create mutation. Bug caught: someone drops ``executionTimeoutMs`` from
    the payload (or stops deriving it from ``lifecycle.job_timeout_s``, e.g.
    hardcoding a constant) while the declaration keeps telling config
    validation that ``compute.lifecycle.job_timeout`` is enforced — so a cfg
    asking for a 10-minute job cap launches with no cap and no diagnostic.
    """
    captured: list[dict[str, Any]] = []

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        del url
        captured.append(body)
        return {"data": {"saveTemplate": {"id": "ep-1"}}}

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    provider.create_instance(
        InstanceSpec(
            image="runpod/pytorch:latest",
            offer=Offer(
                id="NVIDIA A100 80GB PCIe",
                gpu_type="NVIDIA A100 80GB PCIe",
                vram_gb=80,
                cuda="12.4",
                cost_rate_usd_per_hr=1.64,
                mode="serverless",
            ),
            lifecycle=Lifecycle(job_timeout_s=1234.0),
            tags={"mode": "serverless"},
        )
    )
    payload = captured[-1]["variables"]["input"]
    declared = Capability.JOB_TIMEOUT in RunPodProvider.capabilities()
    assert declared is ("executionTimeoutMs" in payload)
    assert payload["executionTimeoutMs"] == 1234 * 1000


def test_modal_idle_autostop_declaration_matches_the_built_app_request() -> None:
    """``IDLE_AUTOSTOP`` on modal <-> ``scaledown_window_s`` on the app request.

    Scope, stated precisely because the factory is stubbed here: this asserts
    the PROVIDER half of the wiring — that ``ModalProvider`` derives
    ``ModalAppRequest.scaledown_window_s`` from ``lifecycle.idle_timeout_s``
    rather than leaving it at ``_app.py``'s 300 s default. It does NOT reach
    ``@app.function``; the request-to-decorator hop
    (``scaledown_window=req.scaledown_window_s``) is covered by
    ``tests/providers/modal/test_app.py``, which drives the real
    ``build_modal_app`` against a fake modal module.

    Bug caught: the derivation dropped or hardcoded while modal keeps
    declaring IDLE_AUTOSTOP, so a cfg asking for a 7-minute idle cap silently
    runs on a 5-minute one — or, if the field is dropped outright, on none.
    """
    captured: dict[str, Any] = {}

    def _factory(req: Any, modal_mod: Any) -> tuple[Any, Any]:
        del modal_mod
        captured["req"] = req
        return (object(), object())

    provider = ModalProvider(
        app_factory=_factory,
        deployer=lambda _app, _fn: "https://x--kinoforge-r1-server.modal.run",
    )
    provider.create_instance(
        InstanceSpec(
            image="img:latest",
            offer=Offer("A10", "A10", 24, "12.4", 1.10, mode="serverless"),
            run_id="r1",
            setup_steps=(SetupStep("echo hi"),),
            launch=Launch(("python", "-m", "server")),
            lifecycle=Lifecycle(idle_timeout_s=420.0),
        )
    )
    declared = Capability.IDLE_AUTOSTOP in ModalProvider.capabilities()
    assert declared is (captured["req"].scaledown_window_s == 420)


def test_skypilot_on_instance_deadline_declaration_matches_the_rendered_setup() -> None:
    """``ON_INSTANCE_DEADLINE`` on skypilot <-> the watchdog arm prelude.

    This declaration is load-bearing beyond skypilot itself: it is the ONLY
    reason ``max_lifetime`` does not ERROR every shipped skypilot config, and
    the only substitute that downgrades the ``idle_timeout`` / ``job_timeout``
    rows from ERROR to WARN. Nothing else on a skypilot cluster bounds spend.

    Bug caught: the arm block stops being prepended to ``Task.setup`` — moved
    below the provision script (where a failing provision skips it), made
    conditional on ``provision_script`` again, or dropped in a refactor —
    while the declaration keeps five configs loading clean. Asserted on the
    setup PREFIX, not by substring: Brief 1's post-mortem has the watchdog's
    stage-1 terminate returning rc=0 without terminating anything, which is
    exactly how an arm block that merely exists somewhere gets trusted.
    """

    class _FakeTask:
        def __init__(self, config: dict[str, Any]) -> None:
            self.config = config

    class _FakeTaskNamespace:
        def from_yaml_config(self, config: dict[str, Any]) -> _FakeTask:
            return _FakeTask(config)

    class _FakeSky:
        def __init__(self) -> None:
            self.launches: list[dict[str, Any]] = []
            self.Task = _FakeTaskNamespace()  # noqa: N815 — mirrors sky.Task

        def launch(self, task: Any, **kwargs: Any) -> tuple[None, None]:
            del kwargs
            self.launches.append(task.config)
            return (None, None)

        def status(self) -> list[dict[str, Any]]:
            return []

        def down(self, name: str) -> None: ...

    sky = _FakeSky()
    SkyPilotProvider(sky_client=sky).create_instance(
        InstanceSpec(image="img:latest", setup_steps=(SetupStep("echo provision"),))
    )
    setup = sky.launches[0]["setup"]
    declared = Capability.ON_INSTANCE_DEADLINE in SkyPilotProvider.capabilities()
    assert declared is setup.startswith("# --- kinoforge watchdog arm")
    # The provision script still runs, i.e. arming did not displace it.
    assert "echo provision" in setup


def test_declared_capabilities_survive_a_lazy_composition_root_import() -> None:
    """The Task 0 lookup test pinned the mechanism; this pins the CONTENT
    through the same lazy path. Catches a declaration that only resolves
    when something else has already imported the providers."""
    import subprocess
    import sys

    code = (
        "from kinoforge.core.capabilities import Capability, capabilities_for;"
        "print(Capability.HEARTBEAT_READ in capabilities_for('runpod'),"
        " Capability.HEARTBEAT_READ in capabilities_for('skypilot'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True False"


def test_billed_flags() -> None:
    """Catches local being treated as a billed provider, which would error
    every local config on the spend rows."""
    assert LocalProvider.billed is False
    assert all(
        c.billed is True for c in (RunPodProvider, ModalProvider, SkyPilotProvider)
    )


def test_parity_check_catches_a_declaration_without_an_implementation() -> None:
    """The parity test must not pass vacuously."""

    class LyingProvider(ComputeProvider):
        @classmethod
        def capabilities(
            cls, shape: WorkloadShape = WorkloadShape.SERVER
        ) -> frozenset[Capability]:
            return frozenset({Capability.RUNTIME_PROBE})

    declared = Capability.RUNTIME_PROBE in LyingProvider.capabilities()
    assert declared is True
    assert _overrides(LyingProvider, "probe_runtime") is False
    # Parity would be violated -> the real parametrized test would fail.
    assert declared is not _overrides(LyingProvider, "probe_runtime")


@pytest.mark.parametrize(
    ("name", "expected"),
    [("local", True), ("runpod", True), ("skypilot", False), ("modal", False)],
)
def test_heartbeat_predicate_per_provider(name: str, expected: bool) -> None:
    """Catches the derivation diverging from the declaration it replaced.

    Asserts hardcoded per-provider expectations, NOT
    ``HEARTBEAT_READ in capabilities_for(name)`` — the predicate's body is
    that same expression, so comparing the two would be a tautology that
    cannot fail.
    """
    assert provider_heartbeat_supported(name) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [("local", True), ("modal", True), ("runpod", True), ("skypilot", False)],
)
def test_util_predicate_per_provider(name: str, expected: bool) -> None:
    """Catches the derivation diverging from the declaration it replaced.

    Asserts hardcoded per-provider expectations, NOT
    ``UTIL_SNAPSHOT in capabilities_for(name)`` — the predicate's body is
    that same expression, so comparing the two would be a tautology that
    cannot fail.
    """
    assert provider_util_supported(name) is expected


def test_predicate_answers_are_unchanged_from_the_string_tables() -> None:
    """Pins the pre-change behaviour: a derivation that silently widens or
    narrows support would change reaper verdicts on live rows."""
    assert {n for n in REGISTERED if provider_heartbeat_supported(n)} == {
        "local",
        "runpod",
    }
    assert {n for n in REGISTERED if provider_util_supported(n)} == {
        "local",
        "modal",
        "runpod",
    }
    assert {n for n in REGISTERED if provider_balance_supported(n)} == {"runpod"}
