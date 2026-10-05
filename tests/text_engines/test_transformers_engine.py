"""TransformersTextEngine: fragment, port-aware client, result/error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import kinoforge._adapters  # noqa: F401 — self-register
from kinoforge.core import registry
from kinoforge.core.errors import TextGenerationFailed
from kinoforge.core.interfaces import Instance, TextJob
from kinoforge.text_engines import transformers as te


def _cfg(port: int | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "engine": {
            "kind": "diffusers",
            "precision": "bf16",
            "diffusers": {"capability": {"supported_modes": ["t2t"]}},
        },
        "models": [
            {"kind": "base", "ref": "hf:Qwen/Qwen3-0.6B", "target": "checkpoints"}
        ],
        "text": {"engine": "transformers", "params": {}},
    }
    if port is not None:
        cfg["text"]["port"] = port
    return cfg


def _instance(**endpoints: str) -> Instance:
    return Instance(
        id="pod1",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints=endpoints,
    )


def test_registered_under_transformers() -> None:
    """Bug caught: the _adapters import missing, so `text.engine: transformers` is UnknownAdapter."""
    assert registry.get_text_engine("transformers") is te.TransformersTextEngine


def test_fragment_exports_model_and_port() -> None:
    """Bug caught: exporting the raw `hf:` ref (from_pretrained cannot load it),
    or a fixed port that defeats the sidecar seam."""
    rp = te.TransformersTextEngine().render_provision(_cfg())
    assert "export KINOFORGE_TEXT_MODEL_ID=Qwen/Qwen3-0.6B" in rp.script
    assert "export KINOFORGE_TEXT_PORT=8000" in rp.script
    assert rp.ports == ["8000"]
    assert rp.env_required == ["HF_TOKEN"]
    rp2 = te.TransformersTextEngine().render_provision(_cfg(port=8002))
    assert "export KINOFORGE_TEXT_PORT=8002" in rp2.script
    assert rp2.ports == ["8002"]


def test_base_url_follows_text_port() -> None:
    """Bug caught: the mixin's hard-coded 8000 lookup ignoring text.port."""
    eng = te.TransformersTextEngine()
    inst = _instance(**{"8000": "https://a-8000.proxy", "8002": "https://a-8002.proxy"})
    eng._bind(_cfg(port=8002))  # noqa: SLF001
    assert eng._base_url(inst) == "https://a-8002.proxy"  # noqa: SLF001
    eng._bind(_cfg())  # noqa: SLF001
    assert eng._base_url(inst) == "https://a-8000.proxy"  # noqa: SLF001


def test_health_parses_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: modes left as a list (membership tests still work) but ready
    coerced wrongly, or the URL missing /health."""
    seen: list[str] = []

    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        seen.append(f"{method} {url}")
        return {
            "ready": True,
            "model": "Qwen/Qwen3-0.6B",
            "supported_modes": ["t2t"],
            "capabilities": ["text", "upload"],
        }

    monkeypatch.setattr(te, "_http_json", fake_http)
    h = te.TransformersTextEngine().health(_instance(**{"8000": "https://p"}), _cfg())
    assert seen == ["GET https://p/health"]
    assert h.ready is True and h.model == "Qwen/Qwen3-0.6B"
    assert h.supported_modes == frozenset({"t2t"})


def test_complete_posts_the_job_and_maps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug caught: polling /status/{id} (the generate contract) instead of
    /text/status/{id}; usage values left as strings."""
    calls: list[tuple[str, str, Any]] = []

    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        calls.append((method, url, payload))
        if method == "POST":
            return {"job_id": "j1"}
        return {
            "state": "done",
            "result": {
                "text": "hi",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": "4", "completion_tokens": 3},
                "model": "m",
            },
        }

    monkeypatch.setattr(te, "_http_json", fake_http)
    job = TextJob(
        prompt="p",
        system="s",
        images=("/tmp/kf-uploads/a.png",),
        params={"max_new_tokens": 8},
    )
    res = te.TransformersTextEngine().complete(
        _instance(**{"8000": "https://p"}), job, _cfg()
    )
    assert calls[0] == (
        "POST",
        "https://p/text",
        {
            "prompt": "p",
            "system": "s",
            "images": ["/tmp/kf-uploads/a.png"],
            "params": {"max_new_tokens": 8},
        },
    )
    assert calls[1][:2] == ("GET", "https://p/text/status/j1")
    assert res.text == "hi" and res.finish_reason == "stop" and res.model == "m"
    assert res.usage == {"prompt_tokens": 4, "completion_tokens": 3}
    assert res.elapsed_s >= 0.0


def test_server_error_raises_text_generation_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug caught: a generic KeyError on a missing `result` when state is error."""

    def fake_http(*, method: str, url: str, payload: Any = None) -> dict[str, Any]:
        return (
            {"job_id": "j9"}
            if method == "POST"
            else {"state": "error", "error": "RuntimeError: OOM"}
        )

    monkeypatch.setattr(te, "_http_json", fake_http)
    with pytest.raises(TextGenerationFailed, match="j9.*OOM"):
        te.TransformersTextEngine().complete(
            _instance(**{"8000": "https://p"}), TextJob(prompt="p"), _cfg()
        )


def test_upload_image_returns_a_pod_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug caught: handing the server a file:// URL it cannot open."""
    eng = te.TransformersTextEngine()
    monkeypatch.setattr(
        eng,
        "_upload_source",
        lambda inst, p, *, media: f"file:///tmp/kf-uploads/{p.name}",
    )
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG")
    assert (
        eng.upload_image(_instance(**{"8000": "https://p"}), png, _cfg())
        == "/tmp/kf-uploads/x.png"
    )


def test_model_identity_is_the_repo_tail_and_never_raises() -> None:
    """Bug caught (§17's `_unknown_` class): an identity that raises or returns the full ref."""
    eng = te.TransformersTextEngine()
    assert eng.model_identity(_cfg()) == "Qwen3-0.6B"
    assert eng.model_identity({"models": []}) == ""
