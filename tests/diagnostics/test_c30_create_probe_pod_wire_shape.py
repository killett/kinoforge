"""Wire-shape assertions for ``c30_probe.create_probe_pod``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import (
    _C28_TRAP_PREAMBLE_LINES,
    GraphQLError,
    create_probe_pod,
)


class _CapturingClient:
    """Capture the GraphQL mutation payload(s) and return a canned response."""

    def __init__(self, pod_id: str = "pod-abc") -> None:
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self._pod_id = pod_id

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append((query, variables))
        return {"data": {"podFindAndDeployOnDemand": {"id": self._pod_id}}}


def test_preamble_contains_trap_function() -> None:
    text = "\n".join(_C28_TRAP_PREAMBLE_LINES)
    assert "_kinoforge_diag_capture()" in text
    assert "trap '_kinoforge_diag_capture $?' EXIT" in text
    assert "aws s3 cp /tmp/diag.txt" in text


def test_a1a_no_port_payload() -> None:
    client = _CapturingClient()
    pod_id = create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports=None,
        provision_script="echo a1a && sleep 600",
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1a-20260614T120000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    assert pod_id == "pod-abc"
    assert len(client.payloads) == 1
    _, vars_ = client.payloads[0]
    assert "ports" not in vars_["input"]
    docker_args = vars_["input"]["dockerArgs"]
    assert docker_args.startswith('bash -c "')
    assert "_kinoforge_diag_capture()" in docker_args
    assert "echo a1a && sleep 600" in docker_args


def test_a1b_port_declared_payload() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports="8188/http",
        provision_script="echo a1b && sleep 600",
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1b-20260614T130000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    assert vars_["input"]["ports"] == "8188/http"


def test_a1c_listener_payload_has_http_server_in_args() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        ports="8188/http",
        provision_script="python3 -m http.server 8188 & sleep 600",
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a1c-20260614T140000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    assert "python3 -m http.server 8188" in vars_["input"]["dockerArgs"]


class _ErrorClient:
    """Returns the RunPod SUPPLY_CONSTRAINT-shaped GraphQL error response."""

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "errors": [
                {
                    "message": "There are no longer any instances available "
                    "with the requested specifications.",
                    "extensions": {"code": "SUPPLY_CONSTRAINT"},
                }
            ],
            "data": {"podFindAndDeployOnDemand": None},
        }


def test_graphql_error_surfaces_with_code() -> None:
    """Supply-constraint errors must surface as GraphQLError(code='SUPPLY_CONSTRAINT')."""
    with pytest.raises(GraphQLError) as ei:
        create_probe_pod(
            _ErrorClient(),
            image="x",
            ports=None,
            provision_script="sleep 1",
            env={},
            gpu_type_id="NVIDIA RTX A2000",
            run_id="r",
            diag_bucket="b",
        )
    assert ei.value.code == "SUPPLY_CONSTRAINT"
    assert "instances available" in str(ei.value)


def test_null_data_without_errors_still_raises() -> None:
    """``data.podFindAndDeployOnDemand=None`` with no errors block also raises."""

    class _NullData:
        def execute(self, q: str, v: dict[str, Any]) -> dict[str, Any]:
            return {"data": {"podFindAndDeployOnDemand": None}}

    with pytest.raises(GraphQLError):
        create_probe_pod(
            _NullData(),
            image="x",
            ports=None,
            provision_script="sleep 1",
            env={},
            gpu_type_id="g",
            run_id="r",
            diag_bucket="b",
        )


def test_diag_env_propagated_to_input() -> None:
    client = _CapturingClient()
    create_probe_pod(
        client,
        image="ubuntu:22.04",
        ports=None,
        provision_script="apt-get install -y awscli && sleep 600",
        env={"KINOFORGE_DIAG_BUCKET": "kinoforge-pod-diagnostics", "EXTRA": "ok"},
        gpu_type_id="NVIDIA RTX A2000",
        run_id="c30-a0prime-20260614T150000",
        diag_bucket="kinoforge-pod-diagnostics",
    )
    _, vars_ = client.payloads[0]
    env_list = vars_["input"]["env"]
    keys = {e["key"]: e["value"] for e in env_list}
    assert keys["KINOFORGE_DIAG_BUCKET"] == "kinoforge-pod-diagnostics"
    assert keys["KINOFORGE_DIAG_PREFIX"] == "boot-logs/c30-a0prime-20260614T150000"
    assert keys["EXTRA"] == "ok"
