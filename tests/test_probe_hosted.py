"""Unit tests for tools/probe_hosted.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._fixtures.fake_auth import FakeAuthStrategy
from tools.probe_hosted import (
    ProbeResult,
    probe_strategies,
    write_snapshot,
)


def test_probe_strategies_all_pass() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(fake_identity="id-1")),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert all(r.ok for r in results)
    assert [r.name for r in results] == ["hosted", "veo"]
    assert [r.identity for r in results] == ["id-1", "id-2"]


def test_probe_strategies_fails_on_missing_creds() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(credentials_ok=False)),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert results[0].ok is False
    assert (
        "missing" in (results[0].reason or "").lower()
        or "disabled" in (results[0].reason or "").lower()
    )
    assert results[1].ok is True


def test_write_snapshot_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = [
        ProbeResult(name="hosted", ok=True, identity="id-1", reason=None),
        ProbeResult(name="veo", ok=False, identity=None, reason="boom"),
    ]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    body = json.loads(snap_path.read_text())
    assert body["git_sha"] == "deadbeef"
    assert body["strategies"] == [
        {"name": "hosted", "ok": True, "identity": "id-1", "reason": None},
        {"name": "veo", "ok": False, "identity": None, "reason": "boom"},
    ]
    assert "captured_at" in body


def test_write_snapshot_uses_tmp_then_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No partial snapshot survives a crash mid-write."""
    results = [ProbeResult(name="hosted", ok=True, identity="id", reason=None)]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    # Tmp file must have been removed (rename = atomic).
    tmp_files = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert tmp_files == []


def test_probe_exit_code_zero_on_all_pass(tmp_path: Path) -> None:
    """Run the tool entrypoint with FakeAuthStrategy injected."""
    from tools.probe_hosted import run

    strategies = [("hosted", FakeAuthStrategy())]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code == 0


def test_probe_exit_code_nonzero_on_any_fail(tmp_path: Path) -> None:
    from tools.probe_hosted import run

    strategies = [
        ("hosted", FakeAuthStrategy()),
        ("veo", FakeAuthStrategy(credentials_ok=False)),
    ]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code != 0


# ---------------------------------------------------------------------------
# Layer 3 — --check-bedrock-model-access flag
# ---------------------------------------------------------------------------


class _FakeBedrockControlClient:
    """Stand-in for boto3 bedrock (control plane) client."""

    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        raise_on_list: Exception | None = None,
    ) -> None:
        self._models = models or []
        self._raise = raise_on_list

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        if self._raise is not None:
            raise self._raise
        return {"modelSummaries": self._models}


class _FakeBedrockRuntimeClient:
    """Stand-in for boto3 bedrock-runtime (data plane) client.

    Raises ``exc`` on ``start_async_invoke`` if provided; otherwise succeeds.
    """

    def __init__(self, raise_on_invoke: Exception | None = None) -> None:
        self._raise = raise_on_invoke

    def start_async_invoke(self, **kwargs: Any) -> dict[str, Any]:
        if self._raise is not None:
            raise self._raise
        return {"invocationArn": "arn:aws:bedrock:us-west-2::async-invoke/probe-ok"}


def test_check_bedrock_model_access_passes_when_model_listed() -> None:
    from tools.probe_hosted import check_bedrock_model_access

    fake_control = _FakeBedrockControlClient(
        models=[{"modelId": "luma.ray-v2:0", "modelLifecycle": {"status": "ACTIVE"}}]
    )

    # Runtime raises a ValidationException that is NOT "Operation not allowed" → access ok
    class _BodyValidationError(Exception):
        pass

    _BodyValidationError.__name__ = "ValidationException"

    fake_runtime = _FakeBedrockRuntimeClient(
        raise_on_invoke=_BodyValidationError("Invalid request: missing required field")
    )
    result = check_bedrock_model_access(fake_control, fake_runtime, "luma.ray-v2:0")
    assert result.ok is True
    assert "luma.ray-v2:0" in (result.identity or "")


def test_check_bedrock_model_access_fails_when_model_missing() -> None:
    from tools.probe_hosted import check_bedrock_model_access

    fake_control = _FakeBedrockControlClient(
        models=[
            {"modelId": "amazon.titan-text-v1", "modelLifecycle": {"status": "ACTIVE"}}
        ]
    )
    fake_runtime = _FakeBedrockRuntimeClient()
    result = check_bedrock_model_access(fake_control, fake_runtime, "luma.ray-v2:0")
    assert result.ok is False
    assert "luma.ray-v2:0" in (result.reason or "")


def test_check_bedrock_model_access_detects_authorization_denial() -> None:
    """Stage-2 gate: 'Operation not allowed' → account-level access not granted."""
    from tools.probe_hosted import check_bedrock_model_access

    fake_control = _FakeBedrockControlClient(
        models=[{"modelId": "luma.ray-v2:0", "modelLifecycle": {"status": "ACTIVE"}}]
    )
    fake_runtime = _FakeBedrockRuntimeClient(
        raise_on_invoke=Exception(
            "Operation not allowed: authorizationStatus=NOT_AUTHORIZED"
        )
    )
    result = check_bedrock_model_access(fake_control, fake_runtime, "luma.ray-v2:0")
    assert result.ok is False
    assert "account-level access not granted" in (result.reason or "")


def test_check_bedrock_model_access_passes_when_runtime_validates_body() -> None:
    """Stage-2 pass: any ValidationException that is NOT 'Operation not allowed'."""
    from tools.probe_hosted import check_bedrock_model_access

    fake_control = _FakeBedrockControlClient(
        models=[{"modelId": "luma.ray-v2:0", "modelLifecycle": {"status": "ACTIVE"}}]
    )
    # A generic validation error unrelated to account-level gating.
    fake_runtime = _FakeBedrockRuntimeClient(
        raise_on_invoke=Exception("ValidationException: modelInput must not be empty")
    )
    result = check_bedrock_model_access(fake_control, fake_runtime, "luma.ray-v2:0")
    assert result.ok is True
    assert result.identity == "luma.ray-v2:0"


def test_probe_cli_invokes_check_bedrock_model_access_when_flag_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: extra_checks kwarg fires the bedrock probe alongside strategy health checks."""
    from tools.probe_hosted import ProbeResult, run

    strategies = [("bedrock_video", FakeAuthStrategy())]
    # Inject fakes so no real AWS call happens.
    captured: list[ProbeResult] = []

    def fake_bedrock_check(
        control: object, runtime: object, model_id: str
    ) -> ProbeResult:
        captured.append(
            ProbeResult(
                name=f"bedrock:{model_id}", ok=True, identity=model_id, reason=None
            )
        )
        return captured[-1]

    monkeypatch.setattr(
        "tools.probe_hosted.check_bedrock_model_access", fake_bedrock_check
    )

    extra = [
        (
            "bedrock:luma.ray-v2:0",
            lambda: fake_bedrock_check(None, None, "luma.ray-v2:0"),
        )
    ]
    exit_code = run(
        strategies, snapshot_path=tmp_path / "probe.json", extra_checks=extra
    )
    assert exit_code == 0
    assert len(captured) == 1
