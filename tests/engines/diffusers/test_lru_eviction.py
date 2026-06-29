"""Tests for the in-process LRU model registry + hard-floor VRAM eviction."""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import VRAMEvictionFailed


@pytest.fixture
def _fake_cuda(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Patch torch.cuda.mem_get_info and torch.cuda.empty_cache.

    Returns a single-element list of ``free_bytes`` so tests can mutate
    it to simulate VRAM consumption.
    """
    free = [10 * 1024**3]
    total = 24 * 1024**3

    # Build a fake torch module so the server's lazy `import torch` lands
    # on our stub even on hosts without torch installed.
    fake_cuda = types.SimpleNamespace(
        mem_get_info=lambda: (free[0], total),
        empty_cache=lambda: None,
    )
    fake_torch = types.SimpleNamespace(cuda=fake_cuda)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return free


def _fake_pipe(vram_bytes: int) -> MagicMock:
    p = MagicMock()
    p.vram_bytes = vram_bytes
    p.on_device = "cuda"
    return p


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    srv._LOADED.clear()
    yield
    srv._LOADED.clear()


class TestSingleModel:
    def test_first_load_no_eviction(self, _fake_cuda: list[int]) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ):
            entry = asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))
            assert entry["name"] == "wan-t2v-a14b-fp8"
            assert entry["on_device"] == "cuda"
            assert entry["vram_bytes"] == 5 * 1024**3


class TestEviction:
    def test_lru_evicts_when_tight(self, _fake_cuda: list[int]) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        # Pre-load wan-t2v-a14b-fp8 on CUDA (5 GB).
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ):
            asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))

        # Tight headroom: only 1 GB free now (below 2 GB margin).
        _fake_cuda[0] = 1 * 1024**3
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(8 * 1024**3)
        ):
            # Eviction empties_cache callback grows free for the loop to exit.
            calls = {"empty": 0}

            def _empty_cache() -> None:
                calls["empty"] += 1
                _fake_cuda[0] = 10 * 1024**3

            import torch

            torch.cuda.empty_cache = _empty_cache
            entry = asyncio.run(srv._ensure_on_gpu("seedvr2-3b-fp8"))

        assert entry["name"] == "seedvr2-3b-fp8"
        assert entry["on_device"] == "cuda"
        assert srv._LOADED["wan-t2v-a14b-fp8"]["on_device"] == "cpu"


class TestHardFloor:
    def test_target_exceeds_capacity_raises(self, _fake_cuda: list[int]) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        _fake_cuda[0] = 4 * 1024**3
        # Pretend model needs 80 GB — total GPU stub is 24 GB; refuse.
        with (
            patch.object(
                srv,
                "_load_model_to_gpu",
                return_value=_fake_pipe(80 * 1024**3),
            ),
            pytest.raises(VRAMEvictionFailed, match="exceeds GPU capacity"),
        ):
            asyncio.run(srv._ensure_on_gpu("wan-t2v-huge"))


class TestNoChurn:
    def test_repeated_ensure_no_reload(self, _fake_cuda: list[int]) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        _fake_cuda[0] = 20 * 1024**3
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ) as loader:
            asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))
            asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))
            asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))
        assert loader.call_count == 1


class TestLRUOrder:
    def test_lru_evicts_least_recent_first(self, _fake_cuda: list[int]) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        # Load A, then B with plenty of room (touch B last → A is LRU).
        _fake_cuda[0] = 20 * 1024**3
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ):
            asyncio.run(srv._ensure_on_gpu("wan-t2v-a14b-fp8"))
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ):
            asyncio.run(srv._ensure_on_gpu("seedvr2-3b-fp8"))

        # Now load a third tight enough to require one eviction. A is LRU.
        _fake_cuda[0] = 1 * 1024**3
        import torch

        def _empty_cache() -> None:
            _fake_cuda[0] = 10 * 1024**3

        torch.cuda.empty_cache = _empty_cache
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(3 * 1024**3)
        ):
            asyncio.run(srv._ensure_on_gpu("seedvr2-7b-fp16"))

        assert srv._LOADED["wan-t2v-a14b-fp8"]["on_device"] == "cpu"
        assert srv._LOADED["seedvr2-3b-fp8"]["on_device"] == "cuda"
