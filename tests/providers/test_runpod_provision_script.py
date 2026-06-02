"""Tests for RunPodProvider._create_pod provision-script encoding."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


def _capture_post() -> tuple[
    list[tuple[str, dict[str, Any]]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append((url, body))
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _offer() -> Offer:
    return Offer(
        id="NVIDIA RTX 4090",
        gpu_type="NVIDIA RTX 4090",
        vram_gb=24,
        cuda="12.8",
        cost_rate_usd_per_hr=0.30,
    )


def test_create_pod_without_provision_script_emits_empty_docker_args() -> None:
    """When spec.provision_script is None, dockerArgs stays empty."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(image="runpod/pytorch:latest", offer=_offer())
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["dockerArgs"] == ""


def test_create_pod_with_provision_script_base64_encodes_into_env_var() -> None:
    """spec.provision_script flows into KINOFORGE_PROVISION_SCRIPT as base64."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    script = "set -euo pipefail\ncd /workspace\necho ok"
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script=script,
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    env_list = body["variables"]["input"]["env"]
    env_map = {item["key"]: item["value"] for item in env_list}
    assert "KINOFORGE_PROVISION_SCRIPT" in env_map
    decoded = base64.b64decode(env_map["KINOFORGE_PROVISION_SCRIPT"]).decode("utf-8")
    assert decoded == script


def test_create_pod_with_provision_script_assembles_docker_args() -> None:
    """dockerArgs is the exact bash one-liner that decodes + runs the script."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script="echo hi",
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["dockerArgs"] == (
        'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
        '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
    )


def test_create_pod_image_name_preserved() -> None:
    """spec.image flows to imageName regardless of provision_script."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="custom/image:v1",
        offer=_offer(),
        provision_script="echo",
        run_cmd=["echo"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    assert body["variables"]["input"]["imageName"] == "custom/image:v1"


def test_create_pod_strips_runpod_api_key_from_env() -> None:
    """Cred-safety: RUNPOD_API_KEY never enters env even when caller sets it."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        env={"RUNPOD_API_KEY": "should-not-leak", "HF_TOKEN": "hf_xxxxxxxxxxxxxx"},
        provision_script="echo",
        run_cmd=["echo"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    env_list = body["variables"]["input"]["env"]
    env_map = {item["key"]: item["value"] for item in env_list}
    assert "RUNPOD_API_KEY" not in env_map
    assert env_map["HF_TOKEN"] == "hf_xxxxxxxxxxxxxx"


def test_create_pod_with_script_but_no_run_cmd_still_encodes_script() -> None:
    """run_cmd is irrelevant to the provider — only spec.provision_script gates encoding.

    The engine bakes `exec <run_cmd>` into the rendered script; the provider does not
    look at spec.run_cmd at all.
    """
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script="set -euo pipefail\necho ok",
        run_cmd=None,  # explicitly None — provider must still encode + assemble docker_args
    )
    p.create_instance(spec)
    body = captured[0][1]
    env_list = body["variables"]["input"]["env"]
    env_map = {item["key"]: item["value"] for item in env_list}
    assert "KINOFORGE_PROVISION_SCRIPT" in env_map
    assert body["variables"]["input"]["dockerArgs"] == (
        'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
        '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
    )


def test_create_pod_base64_envelope_does_not_match_credential_leak_patterns() -> None:
    """Base64-encoded script containing $HF_TOKEN literal does NOT look like a cred."""
    from tests.providers.conftest_runpod import _audit_for_leaks

    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    script = (
        "set -euo pipefail\n"
        'curl -L -H "Authorization: Bearer $HF_TOKEN" '
        '"https://hf.co/file" -o w.safetensors\n'
        "exec python main.py"
    )
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        provision_script=script,
        run_cmd=["python", "main.py"],
    )
    p.create_instance(spec)
    body = captured[0][1]
    hits = _audit_for_leaks(body)
    assert hits == [], f"leak detected in encoded script: {hits!r}"
