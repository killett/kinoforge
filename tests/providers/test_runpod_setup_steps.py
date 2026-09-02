"""Behavior: RunPod composes the launch line the engine used to embed.

The trailing ``exec`` is RunPod's own PID-1 convention — it makes the workload
the container's process 1 — so RunPod is the code that should write it. Before
compute-seam S3 the engine baked it into ``provision_script`` and every other
provider had to guess where the setup ended and the server began.

The assertions decode ``KINOFORGE_PROVISION_SCRIPT`` (gzip + base64) rather than
inspecting the envelope: what the pod actually runs is the point, and an
envelope-only assertion would pass against a script that never changed.
"""

from __future__ import annotations

import base64
import gzip
from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.core.interfaces import InstanceSpec, Launch, SetupStep
from kinoforge.providers.runpod import RunPodProvider

#: compute-seam S4: RunPod selects its own SKU inside create_instance, so every
#: transport a create test drives must answer the catalog query first.
_S4_GPU_TYPES: dict[str, object] = {
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
            # Wide enough that both shapes of shipped config find something: a
            # DEFAULT Placement (48 GB floor, $2.20 cap) and the cheap
            # interpolate configs (16 GB floor, $1.00 cap, named 24 GB SKUs).
            for name, vram, price in (
                ("NVIDIA RTX A4000", 16, 0.32),
                ("NVIDIA RTX A5000", 24, 0.44),
                ("NVIDIA GeForce RTX 4090", 24, 0.69),
                ("NVIDIA A100 80GB PCIe", 80, 1.64),
            )
        ]
    }
}


def _create_body(captured: list[Any]) -> dict[str, Any]:
    """Return the create-mutation body, skipping S4's catalog read."""
    for item in captured:
        body = item[1] if isinstance(item, tuple) else item
        if "gpuTypes" not in str(body.get("query", "")):
            return dict(body)
    raise AssertionError("no create mutation was sent")


_create_body_entry = _create_body


def _capture_post() -> tuple[
    list[dict[str, Any]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    """Return a capture list and a fake GraphQL transport writing into it."""
    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        if "gpuTypes" in str(body.get("query", "")):
            return _S4_GPU_TYPES

        if "gpuTypes" in str(body.get("query", "")):
            return _S4_GPU_TYPES
        captured.append(body)
        if "gpuTypes" in str(body.get("query", "")):
            return _S4_GPU_TYPES
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _provider(
    post: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> RunPodProvider:
    """Return a provider wired to *post* with no credentials or reads."""
    return RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})


def _spec(**kwargs: Any) -> InstanceSpec:
    """Return a minimal creatable spec, overridden by *kwargs*."""
    return InstanceSpec(
        image="runpod/pytorch:latest",
        **kwargs,
    )


def _decoded_script(body: dict[str, Any]) -> str:
    """Return the provision script RunPod put on the wire.

    Args:
        body: The captured GraphQL request body.

    Returns:
        The decompressed provision script.
    """
    env = {e["key"]: e["value"] for e in body["variables"]["input"]["env"]}
    blob = env["KINOFORGE_PROVISION_SCRIPT"]
    return gzip.decompress(base64.b64decode(blob)).decode("utf-8")


def test_runpod_appends_the_rendered_launch_to_the_steps() -> None:
    """Bug caught: composing setup and launch in the wrong order, or dropping
    the newline between them, produces a script whose last setup line and whose
    server command run as one unparseable command."""
    captured, post = _capture_post()
    _provider(post).create_instance(
        _spec(
            setup_steps=(SetupStep("step-one"), SetupStep("step-two")),
            launch=Launch(("serve", "--port", "8000"), workdir="/srv", exec_pid1=True),
        )
    )
    assert (
        _decoded_script(_create_body_entry(captured))
        == "step-one\nstep-two\ncd /srv && exec serve --port 8000"
    )


def test_runpod_refuses_steps_without_a_launch() -> None:
    """Bug caught: a container that provisions and then exits, with the pod
    still billing and nothing listening on 8000."""
    captured, post = _capture_post()
    with pytest.raises(ValueError, match="no launch"):
        _provider(post).create_instance(_spec(setup_steps=(SetupStep("step-one"),)))
    assert captured == [], "a pod must not be created for an unlaunchable spec"


def test_runpod_sends_no_script_at_all_for_a_spec_with_no_steps() -> None:
    """The bare ``deploy()`` path, which provisions nothing.

    Bug caught: encoding an EMPTY string instead of sending nothing wraps it in
    the base64/gzip decode-and-run dockerArgs command, so the pod boots running
    a no-op wrapper rather than the image's own entrypoint.
    """
    captured, post = _capture_post()
    _provider(post).create_instance(_spec())
    env = {
        e["key"]: e["value"]
        for e in _create_body_entry(captured)["variables"]["input"]["env"]
    }
    assert "KINOFORGE_PROVISION_SCRIPT" not in env
    assert _create_body_entry(captured)["variables"]["input"]["dockerArgs"] == ""
