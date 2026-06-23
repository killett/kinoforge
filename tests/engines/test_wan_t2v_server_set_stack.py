"""POST /lora/set_stack — happy path + idempotence."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

import kinoforge.engines.diffusers.servers.wan_t2v_server as s


@pytest.fixture
def server_with_stubs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Any, list[str], Any]:
    """Stub pipe, _download_one, _disk_free_bytes; reset _inventory."""
    s._inventory.clear()
    download_log: list[str] = []

    def _fake_download(spec: s.ArtifactDownloadSpec, dest_dir: Path) -> tuple[str, int]:
        download_log.append(spec.filename)
        target = tmp_path / spec.filename
        target.write_bytes(b"x" * 100)
        return str(target), 100

    monkeypatch.setattr(s, "_download_one", _fake_download)
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 10_000_000)
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path)

    class _Stub:
        def __init__(self) -> None:
            self.unloaded: bool = False
            self.loaded: list[tuple[str, str]] = []
            self.adapters: list[str] = []
            self.deleted: list[str] = []

        def unload_lora_weights(self) -> None:
            self.unloaded = True

        def load_lora_weights(self, path: str, adapter_name: str) -> None:
            self.loaded.append((path, adapter_name))

        def set_adapters(
            self,
            names: list[str],
            adapter_weights: list[float] | None = None,  # noqa: ARG002
        ) -> None:
            self.adapters = list(names)

        def delete_adapters(self, names: list[str]) -> None:
            self.deleted.extend(names)

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    return s, download_log, stub


def _spec(filename: str, size_hint: int = 100) -> s.ArtifactDownloadSpec:
    return s.ArtifactDownloadSpec(
        url=f"https://x/{filename}", headers={}, filename=filename, size_hint=size_hint
    )


def test_set_stack_from_empty_downloads_all(
    server_with_stubs: tuple[Any, list[str], Any],
) -> None:
    """Empty inventory + 2 target refs → download both + load both + set both.

    Bug: starting from empty inventory, server skips the download because
    the eviction set is empty → pipeline left with no adapters.
    """
    s, download_log, stub = server_with_stubs
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    resp = asyncio.run(s.set_stack(req))
    assert sorted(download_log) == ["a.s", "b.s"]
    assert {e.ref for e in resp.inventory} == {"A", "B"}
    assert stub.adapters == ["lora_0_a", "lora_1_a"]
    assert resp.swap_rejected is None


def test_set_stack_to_empty_evicts_all(
    server_with_stubs: tuple[Any, list[str], Any],
) -> None:
    """target_refs=[] → unload + drop existing inventory.

    Bug: target_refs=[] is treated as 'no-op'; existing adapters stay.
    """
    s, _, stub = server_with_stubs
    s._inventory[("A", "auto")] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "branch": "auto",
    }
    req = s.SetStackRequest(target_refs=[], download_specs={})
    resp = asyncio.run(s.set_stack(req))
    assert resp.inventory == []
    assert stub.unloaded is True


def test_set_stack_idempotent_on_same_stack(
    server_with_stubs: tuple[Any, list[str], Any],
) -> None:
    """Re-applying same stack → no redownload but pipeline still re-set.

    Bug: re-applying the same stack triggers a redundant download.
    """
    s, download_log, stub = server_with_stubs
    req = s.SetStackRequest(target_refs=["A"], download_specs={"A": _spec("a.s")})
    asyncio.run(s.set_stack(req))
    download_log.clear()
    stub.loaded.clear()
    asyncio.run(s.set_stack(req))
    assert download_log == []
    assert stub.adapters == ["lora_0_a"]


def test_set_stack_overlap_downloads_only_new(
    server_with_stubs: tuple[Any, list[str], Any],
) -> None:
    """Pre-existing A, target [A,B] → download only B.

    Bug: server downloads both, re-fetching A wastefully.
    """
    s, download_log, stub = server_with_stubs
    s._inventory[("A", "auto")] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "branch": "auto",
    }
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    asyncio.run(s.set_stack(req))
    assert download_log == ["b.s"]
    assert set(s._inventory.keys()) == {("A", "auto"), ("B", "auto")}
    assert stub.adapters == ["lora_0_a", "lora_1_a"]


def test_set_stack_tight_disk_evicts_lru(
    server_with_stubs: tuple[Any, list[str], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """50 free bytes + 100-byte download → evict LRU A first.

    Bug: tight-disk branch never invoked because free_bytes check uses the
    wrong comparison; everything goes through the no-evict path until a
    download fails for ENOSPC.
    """
    s, download_log, _ = server_with_stubs
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 50)
    s._inventory[("A", "auto")] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "2026-06-20T09:00:00-07:00",
        "last_used_at_local": "2026-06-20T09:00:00-07:00",
        "adapter_name": "lora_0_a",
        "branch": "auto",
    }
    req = s.SetStackRequest(
        target_refs=["B"],
        download_specs={"B": _spec("b.s", size_hint=100)},
    )
    asyncio.run(s.set_stack(req))
    assert ("A", "auto") not in s._inventory
    assert ("B", "auto") in s._inventory
    assert download_log == ["b.s"]
