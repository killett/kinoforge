"""Unit tests for tests/providers/conftest_runpod.py (Layer N Task 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.providers.conftest_runpod import (
    _COMFY_DISPATCH,
    CredentialLeakError,
    LeakHit,
    _audit_for_leaks,
    _is_credential_name,
    _load_fixture,
    _RecordingHTTPSeam,
    _redact,
    _redact_all,
    _redact_credential_patterns,
    _redact_kv_shape,
    _redact_string,
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


# ---------------------------------------------------------------------------
# Pass 1 — _redact_kv_shape + _is_credential_name (Layer P Task 7 bug-fix #1)
# ---------------------------------------------------------------------------


def test_redact_kv_shape_runpod_env_leak_regression() -> None:
    """PROGRESS:213 canonical RED: RunPod env array leaks the value side."""
    body = {
        "variables": {
            "input": {
                "env": [
                    {"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"},
                    {"key": "HF_TOKEN", "value": "hf_REAL12345"},
                    {"key": "PYTHONUNBUFFERED", "value": "1"},
                ]
            }
        }
    }
    out = _redact_kv_shape(body)
    env = out["variables"]["input"]["env"]
    assert env[0]["key"] == "RUNPOD_API_KEY"
    assert env[0]["value"] == "<REDACTED>"
    assert env[1]["key"] == "HF_TOKEN"
    assert env[1]["value"] == "<REDACTED>"
    assert env[2]["key"] == "PYTHONUNBUFFERED"
    assert env[2]["value"] == "1"


def test_redact_kv_shape_allows_extra_keys_in_item() -> None:
    body = {"env": [{"key": "API_KEY", "value": "secret", "comment": "main key"}]}
    out = _redact_kv_shape(body)
    assert out["env"][0]["value"] == "<REDACTED>"
    assert out["env"][0]["comment"] == "main key"


def test_redact_kv_shape_passes_non_credential_names() -> None:
    body = {"env": [{"key": "IMAGE_NAME", "value": "alpine:latest"}]}
    out = _redact_kv_shape(body)
    assert out["env"][0]["value"] == "alpine:latest"


def test_redact_kv_shape_requires_list_parent() -> None:
    body = {"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"}
    out = _redact_kv_shape(body)
    assert out == body


def test_is_credential_name_matches_protected_vocab() -> None:
    """AC #5 + #6: whole-word + suffix vocab; non-credential names pass through."""
    # AC #5 — must be True.
    assert _is_credential_name("RUNPOD_API_KEY")
    assert _is_credential_name("HF_TOKEN")
    assert _is_credential_name("FAL_KEY")
    assert _is_credential_name("AWS_SECRET_ACCESS_KEY")
    assert _is_credential_name("DB_PASSWORD")
    assert _is_credential_name("SSH_PASSPHRASE")
    # AC #6 — must be False.
    assert not _is_credential_name("PYTHONUNBUFFERED")
    assert not _is_credential_name("IMAGE_NAME")
    assert not _is_credential_name("GPU_COUNT")
    assert not _is_credential_name("keypoints")
    assert not _is_credential_name("checkpoints")


# ---------------------------------------------------------------------------
# Pass 3 — _redact_credential_patterns + _redact_string (Layer P Task 7 bug-fix #1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "needle"),
    [
        ("rpa_token", "rpa_AB12cdEF34GhIj"),
        ("hf_token", "hf_AbCdEf12345678"),
        ("fal_key", "fal_key_xY7zPQ9ABCDEFGH"),
        ("bearer_auth", "Bearer eyJhbGciOiJIUzI1NiJ9.foo"),
        ("sk_openai", "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("sk_anthropic", "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("aws_akia", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIAIOSFODNN7EXAMPLE"),
        (
            "pem_private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\nXXXX\n-----END RSA PRIVATE KEY-----",
        ),
    ],
)
def test_credential_pattern_matcher_catches_each_format(
    label: str, needle: str
) -> None:
    prose = f"prefix [{needle}] suffix"
    out = _redact_string(prose)
    assert needle not in out, f"{label}: needle {needle!r} survived in {out!r}"
    assert "<REDACTED>" in out


@pytest.mark.parametrize(
    "haystack",
    [
        "please ask-me about checkpoints, no sk-x here",
        "this is sk-only-4chars",
        "sk-",
        "sk-aaa",
    ],
)
def test_sk_guard_against_false_positives(haystack: str) -> None:
    out = _redact_string(haystack)
    assert out == haystack, f"false positive: {haystack!r} → {out!r}"


def test_credential_pattern_matcher_recurses_into_nested_structure() -> None:
    payload = {"a": {"b": ["c", {"d": "rpa_REAL_TOKEN_12345"}]}}
    out = _redact_credential_patterns(payload)
    assert out["a"]["b"][1]["d"] == "<REDACTED>"


# ---------------------------------------------------------------------------
# Composition — _redact_all (Layer P Task 7 bug-fix #1 Task 3)
# ---------------------------------------------------------------------------


def test_redact_all_composition_handles_runpod_env_shape() -> None:
    body = {"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"}]}
    out = _redact_all(body)
    assert out["env"][0]["value"] == "<REDACTED>"


def test_redact_all_composition_is_idempotent() -> None:
    body = {
        "data": {
            "token": "raw-secret",
            "env": [{"key": "HF_TOKEN", "value": "hf_REAL12345"}],
            "log": "container started with key=rpa_REAL12345 bearer=Bearer abcdefghij",
        }
    }
    once = _redact_all(body)
    twice = _redact_all(once)
    assert once == twice


def test_redact_all_preserves_existing_key_name_walker_behavior() -> None:
    body = {
        "data": {
            "Token": "bearer-x",
            "pod": {"password": "pw", "imageName": "foo:bar"},
        },
        "Secret_Tail": "y",
    }
    out = _redact_all(body)
    assert out["data"]["Token"] == "<REDACTED>"
    assert out["data"]["pod"]["password"] == "<REDACTED>"
    assert out["data"]["pod"]["imageName"] == "foo:bar"
    assert out["Secret_Tail"] == "<REDACTED>"


# ---------------------------------------------------------------------------
# Audit primitives — LeakHit + _audit_for_leaks + CredentialLeakError
# (Layer P Task 7 bug-fix #1 Task 4)
# ---------------------------------------------------------------------------


def test_audit_for_leaks_returns_empty_for_clean_payload() -> None:
    payload = {"data": {"gpuTypes": [{"id": "g1", "memoryInGb": 24}]}}
    assert _audit_for_leaks(payload) == []


def test_audit_for_leaks_reports_pattern_name_and_pointer() -> None:
    payload = {"data": {"deep": {"k": "rpa_REAL_TOKEN_12345"}}}
    hits = _audit_for_leaks(payload)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.pattern_name == "rpa_token"
    assert hit.json_pointer == "/data/deep/k"
    assert hit.match_snippet.startswith("rpa_")
    assert len(hit.match_snippet) <= 32


def test_audit_for_leaks_handles_list_indices_in_pointer() -> None:
    payload = {"env": [{"key": "X", "value": "hf_REAL_TOKEN_1234567"}]}
    hits = _audit_for_leaks(payload)
    assert len(hits) == 1
    assert hits[0].json_pointer == "/env/0/value"
    assert hits[0].pattern_name == "hf_token"


def test_credential_leak_error_is_exception_subclass() -> None:
    err = CredentialLeakError([], "x.json")
    assert isinstance(err, Exception)
    assert not isinstance(err, AssertionError)


def test_credential_leak_error_str_format() -> None:
    hits = [
        LeakHit("rpa_token", "/response/env/0/value", "rpa_AB12cdEF34GhIj"),
        LeakHit("hf_token", "/response/env/3/value", "hf_xY7zPQ9ABCDEFGH"),
    ]
    err = CredentialLeakError(hits, "create_pod.json")
    text = str(err)
    assert "refusing to write" in text
    assert "create_pod.json" in text
    assert "rpa_token" in text
    assert "/response/env/0/value" in text
    assert "rpa_AB12cdEF34GhIj" in text
    assert "hf_token" in text


# ---------------------------------------------------------------------------
# flush() backstop — _audit_for_leaks gates write_text
# (Layer P Task 7 bug-fix #1 Task 5)
# ---------------------------------------------------------------------------


def test_flush_raises_credential_leak_error_when_redactor_gapped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _redact_all is bypassed but a leak is present, _audit_for_leaks catches it."""
    import tests.providers.conftest_runpod as conf

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"text": "Authorization: Bearer abcdef1234567890"}

    def fake_get(url: str) -> dict[str, Any]:
        return {}

    # Simulate a redactor gap: identity passthrough.
    monkeypatch.setattr(conf, "_redact_all", lambda x: x)

    seam = conf._RecordingHTTPSeam(fake_post, fake_get, out_dir=tmp_path)
    seam.http_post(
        "https://api.runpod.io/graphql",
        {"query": "mutation { podFindAndDeployOnDemand(input: $i) { id } }"},
    )

    with pytest.raises(conf.CredentialLeakError) as exc_info:
        seam.flush()

    assert exc_info.value.filename == "create_pod.json"
    assert any(h.pattern_name == "bearer_auth" for h in exc_info.value.hits)
    # No fixture should have been written.
    assert not (tmp_path / "create_pod.json").exists()
