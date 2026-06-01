"""Unit tests for tests/providers/conftest_runpod.py (Layer N Task 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.providers.conftest_runpod import (
    _COMFY_DISPATCH,
    _load_fixture,
    _RecordingHTTPSeam,
    _redact,
)


def test_redact_replaces_secret_field_names() -> None:
    body = {"apiKey": "sk-real-secret", "podId": "abc123"}
    out = _redact(body)
    assert out["apiKey"] == "<REDACTED>"
    assert out["podId"] == "abc123"


def test_redact_is_case_insensitive_and_recursive() -> None:
    body = {
        "data": {
            "Token": "bearer-x",
            "pod": {"password": "pw", "imageName": "foo:bar"},
        },
        "Secret_Tail": "y",
    }
    out = _redact(body)
    assert out["data"]["Token"] == "<REDACTED>"
    assert out["data"]["pod"]["password"] == "<REDACTED>"
    assert out["data"]["pod"]["imageName"] == "foo:bar"
    assert out["Secret_Tail"] == "<REDACTED>"


def test_redact_does_not_match_partial_word_collisions() -> None:
    body = {"checkpoint": "ok", "keypoints": "ok", "passport": "ok"}
    out = _redact(body)
    assert out == body


def test_load_fixture_reads_response_block(tmp_path: Path, monkeypatch: Any) -> None:
    fixture_dir = tmp_path / "fixtures" / "runpod"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "demo.json").write_text(
        json.dumps(
            {
                "_meta": {"captured_at": "2026-05-31", "operation": "demo"},
                "response": {"data": {"k": "v"}},
            }
        )
    )
    import tests.providers.conftest_runpod as conf

    monkeypatch.setattr(conf, "_FIXTURE_DIR", fixture_dir)
    out = _load_fixture("demo.json")
    assert out == {"data": {"k": "v"}}


def test_load_fixture_missing_raises_with_capture_hint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import tests.providers.conftest_runpod as conf

    monkeypatch.setattr(conf, "_FIXTURE_DIR", tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        _load_fixture("missing.json")
    msg = str(exc.value)
    assert "KINOFORGE_LIVE_TESTS=1" in msg
    assert "KINOFORGE_SAVE_FIXTURES=1" in msg
    assert "missing.json" in msg


def test_recording_seam_dispatches_to_named_files(tmp_path: Path, caplog: Any) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, body))
        return {
            "data": {
                "podFindAndDeployOnDemand": {
                    "id": "pod-1",
                    "apiKey": "sk-leak",
                }
            }
        }

    def fake_get(url: str) -> dict[str, Any]:
        calls.append((url, None))
        if "gpuTypes" in url:
            return {"data": {"gpuTypes": [{"id": "g1", "memoryInGb": 24}]}}
        return {"data": {"unrecognized_root_field": []}}

    seam = _RecordingHTTPSeam(fake_post, fake_get, out_dir=tmp_path)
    seam.http_post(
        "https://api.runpod.io/graphql",
        {"query": "mutation { podFindAndDeployOnDemand(input: $i) { id } }"},
    )
    seam.http_get("https://api.runpod.io/graphql?query={ gpuTypes { id } }")
    with caplog.at_level(logging.WARNING):
        seam.http_get("https://api.runpod.io/graphql?query={ mystery { x } }")

    seam.flush()

    create = json.loads((tmp_path / "create_pod.json").read_text())
    assert (
        create["response"]["data"]["podFindAndDeployOnDemand"]["apiKey"] == "<REDACTED>"
    )
    assert create["_meta"]["operation"] == "create_pod"
    gpu = json.loads((tmp_path / "gpu_types.json").read_text())
    assert gpu["_meta"]["operation"] == "gpu_types"
    assert gpu["response"]["data"]["gpuTypes"][0]["id"] == "g1"
    unknowns = list(tmp_path.glob("unknown_*.json"))
    assert len(unknowns) == 1
    assert any("unrecognized" in rec.message.lower() for rec in caplog.records)


def test_recording_seam_redacts_credentials_in_query_string(tmp_path: Path) -> None:
    """A GET URL with ?api_key=… must not leak the secret into _meta.request_query."""

    def fake_get(url: str) -> dict[str, Any]:
        return {"data": {"gpuTypes": []}}

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {}

    seam = _RecordingHTTPSeam(fake_post, fake_get, out_dir=tmp_path)
    seam.http_get(
        "https://api.runpod.io/graphql?query={ gpuTypes { id } }&api_key=sk-leaky-leak"
    )
    seam.flush()

    payload = json.loads((tmp_path / "gpu_types.json").read_text())
    assert "sk-leaky-leak" not in payload["_meta"]["request_query"]
    assert "<REDACTED>" in payload["_meta"]["request_query"]


def test_starter_fixtures_load() -> None:
    """Every named starter fixture loads cleanly and carries a valid _meta block."""
    from tests.providers.conftest_runpod import _FIXTURE_DIR

    for name in (
        "gpu_types.json",
        "list_pods.json",
        "get_pod.json",
        "create_pod.json",
        "terminate_pod.json",
    ):
        payload = _load_fixture(name)
        assert payload, f"{name} loaded empty"
        with (_FIXTURE_DIR / name).open() as f:
            raw = json.load(f)
        meta = raw["_meta"]
        assert meta["operation"] == name.removesuffix(".json"), (
            f"{name}: _meta.operation drifted from filename"
        )
        for key in ("captured_at", "git_sha", "request_query"):
            assert key in meta, f"{name}: missing _meta.{key}"


def test_recording_seam_comfyui_prompt_dispatch(tmp_path: Path) -> None:
    """POST /prompt with comfyui dispatch → writes prompt_submit.json with body in _meta."""

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"prompt_id": "p-123", "number": 1, "node_errors": {}}

    seam = _RecordingHTTPSeam(
        fake_post,
        lambda url: {},
        tmp_path,
        dispatch=_COMFY_DISPATCH,
    )

    response = seam.http_post(
        "http://10.0.0.1:8188/prompt",
        {"prompt": {"1": {"class_type": "LoadImage"}}, "client_id": "kf"},
    )
    seam.flush()

    assert response["prompt_id"] == "p-123"
    fixture_path = tmp_path / "prompt_submit.json"
    assert fixture_path.exists()
    captured = json.loads(fixture_path.read_text())
    assert captured["response"]["prompt_id"] == "p-123"
    assert "prompt" in captured["_meta"]["request_body"]


def test_recording_seam_comfyui_history_last_poll_wins(tmp_path: Path) -> None:
    """3 polls of /history/{id} → last response wins in history_done.json."""
    poll_data: list[dict[str, Any]] = [
        {"p-123": {"status": {"completed": False}, "outputs": {}}},
        {"p-123": {"status": {"completed": False}, "outputs": {}}},
        {
            "p-123": {
                "status": {"completed": True},
                "outputs": {"9": {"images": [{"filename": "out.png"}]}},
            }
        },
    ]
    polls = iter(poll_data)

    def fake_get(url: str) -> dict[str, Any]:
        return next(polls)

    seam = _RecordingHTTPSeam(
        lambda u, b: {},
        fake_get,
        tmp_path,
        dispatch=_COMFY_DISPATCH,
    )

    for _ in range(3):
        seam.http_get("http://10.0.0.1:8188/history/p-123")
    seam.flush()

    captured = json.loads((tmp_path / "history_done.json").read_text())
    assert captured["response"]["p-123"]["status"]["completed"] is True
