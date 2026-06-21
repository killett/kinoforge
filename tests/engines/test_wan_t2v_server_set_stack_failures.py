"""POST /lora/set_stack failure paths."""

from __future__ import annotations

import asyncio
import types
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException

import kinoforge.engines.diffusers.servers.wan_t2v_server as s


@pytest.fixture
def server_with_stubs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, Any]:
    """Same shape as the happy-path fixture; tests override behaviors per case."""
    s._inventory.clear()
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path)
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 10_000_000)

    class _Stub:
        def __init__(self) -> None:
            self.unloaded: bool = False
            self.loaded: list[tuple[str, str]] = []
            self.adapters: list[str] = []

        def unload_lora_weights(self) -> None:
            self.unloaded = True

        def load_lora_weights(self, path: str, adapter_name: str) -> None:
            self.loaded.append((path, adapter_name))

        def set_adapters(self, names: list[str]) -> None:
            self.adapters = list(names)

        def delete_adapters(self, names: list[str]) -> None:
            pass

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    return s, stub


def _spec(filename: str, size_hint: int = 100) -> s.ArtifactDownloadSpec:
    return s.ArtifactDownloadSpec(
        url=f"https://x/{filename}", headers={}, filename=filename, size_hint=size_hint
    )


def test_download_fail_no_eviction_502(
    server_with_stubs: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: error path raises bare Exception instead of HTTPException, so the
    operator's CLI sees a generic 500 with no structured failure breakdown."""
    s, _ = server_with_stubs

    def _fail_b(spec: Any, dest_dir: Any) -> tuple[str, int]:
        if spec.filename == "b.s":
            raise RuntimeError("simulated 504")
        return f"{dest_dir}/{spec.filename}", 100

    monkeypatch.setattr(s, "_download_one", _fail_b)
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(s.set_stack(req))
    assert ei.value.status_code == 502
    detail = cast(dict[str, Any], ei.value.detail)
    assert detail["error"] == "lora_download_failed"
    assert detail["evict_completed"] == []
    assert detail["download_failed"] == "B"
    assert "504" in detail["underlying"]


def test_download_fail_after_eviction_502(
    server_with_stubs: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: evict_completed list omitted from the body, so the orchestrator
    cannot distinguish degraded from clean-fail."""
    s, _ = server_with_stubs
    s._inventory["X"] = {
        "ref": "X",
        "filename": "x.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/x.s",
        "downloaded_at_local": "old",
        "last_used_at_local": "old",
        "adapter_name": "lora_0",
    }

    def _fail_new(spec: Any, dest_dir: Any) -> tuple[str, int]:
        raise RuntimeError("CivitAI 504")

    monkeypatch.setattr(s, "_download_one", _fail_new)
    req = s.SetStackRequest(
        target_refs=["B"], download_specs={"B": _spec("b.s", size_hint=100)}
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(s.set_stack(req))
    assert ei.value.status_code == 502
    detail = cast(dict[str, Any], ei.value.detail)
    assert detail["evict_completed"] == ["X"]
    assert detail["download_failed"] == "B"


def test_disk_full_mid_download_507(
    server_with_stubs: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: ENOSPC mapped to 502 (download failed) rather than 507 (insufficient
    storage), so the orchestrator's classifier can't tell a transient throttle
    from a fatal disk-full."""
    s, _ = server_with_stubs

    def _enospc(spec: Any, dest_dir: Any) -> tuple[str, int]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(s, "_download_one", _enospc)
    req = s.SetStackRequest(target_refs=["B"], download_specs={"B": _spec("b.s")})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(s.set_stack(req))
    assert ei.value.status_code == 507
    detail = cast(dict[str, Any], ei.value.detail)
    assert detail["error"] == "disk_full"


def test_vram_oom_rollback_200_with_swap_rejected(
    server_with_stubs: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: VRAM OOM raises 500 instead of rolling back; pod left in unknown
    state, orchestrator destroys an otherwise-healthy pod."""
    s, stub = server_with_stubs

    def _fake_dl(spec: Any, dest_dir: Any) -> tuple[str, int]:
        return f"{dest_dir}/{spec.filename}", 100

    monkeypatch.setattr(s, "_download_one", _fake_dl)
    call_count = {"n": 0}

    def _oom_then_ok(self: Any, names: list[str]) -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("CUDA out of memory")
        self.adapters = list(names)

    stub.set_adapters = types.MethodType(_oom_then_ok, stub)

    s._inventory["A"] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0",
    }
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    resp = asyncio.run(s.set_stack(req))
    assert resp.swap_rejected is not None
    assert resp.swap_rejected.reason == "vram_oom"
    assert "B" in resp.swap_rejected.target_refs_dropped
    refs = {e.ref for e in resp.inventory}
    assert refs == {"A"}


def test_failed_download_cleans_up_partial_file(
    server_with_stubs: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bug: download wrapper leaves *.partial files on disk after failure,
    leaking space + confusing future operators."""
    s, _ = server_with_stubs

    class _FakeResp:
        def __init__(self) -> None:
            self._calls = 0

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, n: int) -> bytes:
            self._calls += 1
            if self._calls > 1:
                raise RuntimeError("connection reset")
            return b"x" * 1024

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResp())

    spec = _spec("z.s")
    with pytest.raises(RuntimeError):
        s._download_one(spec, tmp_path)
    assert not (tmp_path / "z.s.partial").exists()
    assert not (tmp_path / "z.s").exists()
