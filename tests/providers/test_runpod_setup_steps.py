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

from kinoforge.core.interfaces import InstanceSpec, Launch, Offer, SetupStep
from kinoforge.providers.runpod import RunPodProvider


def _capture_post() -> tuple[
    list[dict[str, Any]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    """Return a capture list and a fake GraphQL transport writing into it."""
    captured: list[dict[str, Any]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append(body)
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
        offer=Offer(
            id="NVIDIA A100 80GB PCIe",
            gpu_type="NVIDIA A100 80GB PCIe",
            vram_gb=80,
            cuda="12.4",
            cost_rate_usd_per_hr=1.64,
        ),
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
        _decoded_script(captured[0])
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
    env = {e["key"]: e["value"] for e in captured[0]["variables"]["input"]["env"]}
    assert "KINOFORGE_PROVISION_SCRIPT" not in env
    assert captured[0]["variables"]["input"]["dockerArgs"] == ""
