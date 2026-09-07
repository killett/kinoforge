"""Both providers use the name the CONTROLLER reserved, not one of their own.

Spec A2 fix round 1. The pre-create ephemeral index row exists to be a handle
on a resource that is already billing. That only works if the id in the row is
the name the provider actually gives the resource. Under STRICT_POLICY
(``pod_name_includes_alias=False``) each provider used to mint its own
``secrets.token_hex(4)`` *inside* ``create_instance``, so the controller could
not know the name until the create had already returned — the exact window the
row exists to cover.

Each test below computes the name from the session BEFORE calling the provider,
then asserts the provider used that string. A provider that mints its own token
fails, however well-formed its name is.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import InstanceSpec, Launch, Lifecycle, SetupStep
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider

_RUN_ID = "generate-20260906-021513"

#: compute-seam S4 — RunPod picks its own SKU inside ``create_instance``, so
#: every create test must answer the catalog query first. Wide enough for the
#: DEFAULT Placement (48 GB floor, $2.20 cap).
_GPU_TYPES: dict[str, object] = {
    "data": {
        "gpuTypes": [
            {
                "id": name,
                "displayName": name,
                "memoryInGb": vram,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": price,
                    "uninterruptablePrice": price,
                },
            }
            for name, vram, price in (
                ("NVIDIA RTX A5000", 24, 0.44),
                ("NVIDIA A100 80GB PCIe", 80, 1.64),
            )
        ]
    }
}


def _capture_post() -> tuple[
    list[dict[str, Any]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    """Return (captured bodies, an ``_http_post`` stand-in)."""
    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        del url
        captured.append(body)
        if "gpuTypes" in str(body.get("query", "")):
            return _GPU_TYPES
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _created_pod_name(captured: list[dict[str, Any]]) -> str:
    """Return the ``name`` the create mutation put on the wire."""
    body = next(b for b in captured if "gpuTypes" not in str(b.get("query", "")))
    name: str = body["variables"]["input"]["name"]
    return name


def test_modal_uses_the_name_the_controller_reserved() -> None:
    """Bug caught: ``ModalProvider`` minting ``eph-{token_hex(4)}`` itself, so
    the row written before the create names an app that does not exist and the
    one that does exist is nameless. On Modal the opaque name IS the instance
    id, so this is also the id ``kinoforge destroy --id`` needs.
    """

    def fake_factory(req: Any, modal_mod: Any) -> tuple[str, str]:
        del modal_mod
        captured["req"] = req
        return ("APP", "SERVERFN")

    captured: dict[str, Any] = {}
    provider = ModalProvider(
        app_factory=fake_factory,
        deployer=lambda app, fn: "https://ws--kinoforge-eph-server.modal.run",
    )
    spec = InstanceSpec(
        image="python:3.13-slim",
        run_id=_RUN_ID,
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "-m", "server")),
        lifecycle=Lifecycle(idle_timeout_s=300),
    )

    with EphemeralSession(enabled=True) as session:
        reserved = session.resource_name(_RUN_ID, "modal")
        inst = provider.create_instance(spec)

    assert inst.id == reserved, (
        f"the controller reserved {reserved!r} before the create; the provider "
        f"named the app {inst.id!r} — the pre-create row names nothing"
    )
    assert captured["req"].run_id == reserved
    assert provider._deployments[reserved]["name"] == f"kinoforge-{reserved}"


def test_runpod_uses_the_name_the_controller_reserved() -> None:
    """Bug caught: ``RunPodProvider`` minting ``kinoforge-{token_hex(4)}``
    itself. RunPod returns its own pod id, so this NAME is the only thing tying
    the pre-create row to the pod an operator sees in the console — and it is
    what ``cli/_reconcile._adopt_launching_row`` matches ``tags["name"]``
    against.
    """
    captured, http_post = _capture_post()
    provider = RunPodProvider(creds=None, http_post=http_post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-cuda12.4",
        run_id=_RUN_ID,
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "-m", "server")),
        lifecycle=Lifecycle(idle_timeout_s=300),
    )

    with EphemeralSession(enabled=True) as session:
        reserved = session.resource_name(_RUN_ID, "runpod")
        provider.create_instance(spec)

    assert _created_pod_name(captured) == reserved, (
        f"the controller reserved {reserved!r} before the create; RunPod was "
        f"asked for {_created_pod_name(captured)!r}, so the pre-create row "
        f"names a pod RunPod never heard of"
    )


def test_runpod_name_is_unchanged_without_ephemeral() -> None:
    """Bug caught: the controller-side mint bleeding into ordinary runs, which
    would break every log line, teardown message and launching-row adoption
    that names a pod by its run id.
    """
    captured, http_post = _capture_post()
    provider = RunPodProvider(creds=None, http_post=http_post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:2.4.0-cuda12.4",
        run_id=_RUN_ID,
        setup_steps=(SetupStep("echo hi"),),
        launch=Launch(("python", "-m", "server")),
        lifecycle=Lifecycle(idle_timeout_s=300),
    )

    provider.create_instance(spec)

    assert _created_pod_name(captured) == _RUN_ID
