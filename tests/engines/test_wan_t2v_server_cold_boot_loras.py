"""Cold-boot LoRA loading on the Diffusers Wan T2V server.

Mocks diffusers + the HTTP download path; verifies the server's
_load_pipeline correctly:
- handles initial_lora_stack=None (back-compat, zero LoRAs).
- handles empty list (explicit zero LoRAs).
- handles 2-LoRA stack: download + load + set_adapters call ordering.
- bubbles download failures as RuntimeError naming the failed ref.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def mock_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Stub out WanPipeline construction + the pipe instance's LoRA methods."""
    calls: dict[str, list[Any]] = {"load_lora": [], "set_adapters": []}

    class _StubPipe:
        def load_lora_weights(self, path: str, adapter_name: str) -> None:
            """Record the call so the test can pin ordering."""
            calls["load_lora"].append((path, adapter_name))

        def set_adapters(self, names: list[str]) -> None:
            """Record the adapter-name list passed to set_adapters."""
            calls["set_adapters"].append(list(names))

        def to(self, *args: Any, **kwargs: Any) -> _StubPipe:
            """Pretend-move the stub pipeline; returns self for chaining."""
            return self

    def _fake_diffusers_load() -> _StubPipe:
        return _StubPipe()

    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._inventory.clear()
    monkeypatch.setattr(s, "_diffusers_load", _fake_diffusers_load, raising=False)
    return calls


@pytest.fixture
def mock_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub the LoRA download helper to write a small file + return its path."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    def _fake_download(spec: Any, dest_dir: Path) -> tuple[str, int]:
        dest = tmp_path / spec.filename
        dest.write_bytes(b"x" * 1024)
        return str(dest), 1024

    monkeypatch.setattr(s, "_download_one", _fake_download, raising=False)
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path, raising=False)


def test_load_pipeline_no_initial_stack(
    mock_pipeline: dict[str, list[Any]], mock_download: None
) -> None:
    """Bug: refactor adds an initial_lora_stack=None default but
    accidentally calls pipe.load_lora_weights with None, crashing
    every existing cold-boot."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._load_pipeline(initial_lora_stack=None)
    assert mock_pipeline["load_lora"] == []
    assert mock_pipeline["set_adapters"] == []


def test_load_pipeline_empty_initial_stack(
    mock_pipeline: dict[str, list[Any]], mock_download: None
) -> None:
    """Bug: empty list incorrectly triggers set_adapters([]) which some
    pipelines reject; or worse, treats [] as 'load all from a default
    directory'."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._load_pipeline(initial_lora_stack=[])
    assert mock_pipeline["load_lora"] == []
    assert mock_pipeline["set_adapters"] == []


def test_load_pipeline_two_lora_stack(
    mock_pipeline: dict[str, list[Any]], mock_download: None
) -> None:
    """Verifies download → load_lora → set_adapters call ordering AND
    that the inventory is populated with size_bytes from the actual
    download (not the spec's size_hint).

    Bug: server uses spec.size_hint instead of the bytes-on-disk count.
    """
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    spec_a = s.ArtifactDownloadSpec(
        url="https://x/a", headers={}, filename="a.safetensors", size_hint=999_999
    )
    spec_b = s.ArtifactDownloadSpec(
        url="https://x/b", headers={}, filename="b.safetensors", size_hint=999_999
    )
    s._load_pipeline(
        initial_lora_stack=[("civitai:A@1", spec_a), ("civitai:B@2", spec_b)]
    )
    assert len(mock_pipeline["load_lora"]) == 2
    assert mock_pipeline["load_lora"][0][1] == "lora_0"
    assert mock_pipeline["load_lora"][1][1] == "lora_1"
    assert mock_pipeline["set_adapters"] == [["lora_0", "lora_1"]]
    assert "civitai:A@1" in s._inventory
    assert s._inventory["civitai:A@1"]["size_bytes"] == 1024
    assert s._inventory["civitai:A@1"]["adapter_name"] == "lora_0"


def test_load_pipeline_download_failure_bubbles(
    mock_pipeline: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: download failure during cold-boot is silently swallowed,
    leaving the server running with a partially-populated inventory."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    def _failing_download(spec: Any, dest_dir: Path) -> tuple[str, int]:
        raise RuntimeError("simulated 504")

    monkeypatch.setattr(s, "_download_one", _failing_download, raising=False)
    spec = s.ArtifactDownloadSpec(
        url="https://x/a", headers={}, filename="a.safetensors", size_hint=1
    )
    with pytest.raises(RuntimeError, match="civitai:A@1"):
        s._load_pipeline(initial_lora_stack=[("civitai:A@1", spec)])
