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


class TestFreshLoadMovesToCuda:
    def test_ensure_on_gpu_calls_to_cuda_on_freshly_loaded_pipe(
        self, _fake_cuda: list[int]
    ) -> None:
        # Bug caught: _ensure_on_gpu records on_device="cuda" in LoadedModel
        # metadata but never calls pipe.to("cuda") on the freshly-constructed
        # runtime. SpandrelRuntime / SeedVR2Runtime load weights on CPU by
        # default → inference runs on CPU (glacial or unsupported for fp16),
        # while /health lies that the pipe is on cuda. Regression: 2026-06-30
        # T15 attempt 4 hung at progress=0.0 for 10 min with GPU util = 0%.
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        pipe = _fake_pipe(2 * 1024**3)
        with patch.object(srv, "_load_model_to_gpu", return_value=pipe):
            entry = asyncio.run(srv._ensure_on_gpu("spandrel-realesrgan-fp16"))

        assert entry["on_device"] == "cuda"
        pipe.to.assert_any_call("cuda")


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


class TestSpandrelDispatch:
    def test_spandrel_prefix_loads_via_spandrel_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        # Bug caught: prefix dispatch misses spandrel-* and the LRU
        # registry tries to load WanPipeline against a spandrel slug.
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        # Pre-create a dummy weights file under a tmp dir and redirect
        # the on-pod weights-dir lookup to it via the env-var seam.
        weights = tmp_path / "RealESRGAN_x2plus.pth"
        weights.write_bytes(b"")
        monkeypatch.setenv("KINOFORGE_SPANDREL_WEIGHTS_DIR", str(tmp_path))

        # Stub the SpandrelRuntime constructor so we don't pull torch or
        # the real spandrel package into the unit test.
        fake_runtime = MagicMock(name="SpandrelRuntime")
        captured_kwargs: dict[str, Any] = {}

        def fake_ctor(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return fake_runtime

        import kinoforge.upscalers.spandrel._runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "SpandrelRuntime", fake_ctor)

        pipe = srv._load_model_to_gpu("spandrel-realesrgan-fp16")
        assert pipe is fake_runtime
        assert captured_kwargs["weights_path"] == weights
        assert captured_kwargs["precision"] == "fp16"

    def test_spandrel_missing_weights_raises_filenotfound(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        # Bug caught: dispatch attempts SpandrelRuntime construction
        # before checking that any weights file actually landed, so the
        # operator sees a confusing "ImportError loading spandrel" instead
        # of "weights missing — _fetch_weights didn't run".
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        monkeypatch.setenv("KINOFORGE_SPANDREL_WEIGHTS_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError, match="spandrel weights not found"):
            srv._load_model_to_gpu("spandrel-realesrgan-fp16")


class TestFlashVSRDispatch:
    def test_flashvsr_prefix_loads_via_flashvsr_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        # Bug caught: prefix dispatch misses flashvsr-* and the LRU registry
        # tries to load WanPipeline against a flashvsr slug (or worse, silently
        # falls through to the "unknown model name" raise, breaking the
        # multi-stage capability advertisement).
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        monkeypatch.setenv("KINOFORGE_FLASHVSR_WEIGHTS_DIR", str(tmp_path))

        fake_runtime = MagicMock(name="FlashVSRRuntime")
        captured_kwargs: dict[str, Any] = {}

        def fake_ctor(**kwargs: Any) -> Any:
            captured_kwargs.update(kwargs)
            return fake_runtime

        import kinoforge.upscalers.flashvsr._runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "FlashVSRRuntime", fake_ctor)

        pipe = srv._load_model_to_gpu("flashvsr-wan21-fp16")
        assert pipe is fake_runtime
        assert captured_kwargs["weights_dir"] == tmp_path
        assert captured_kwargs["precision"] == "fp16"
        # Server defaults window=24, tile=0, long_video_mode=False; per-request
        # overrides land via the params dict, not the loader.
        assert captured_kwargs["window_size"] == 24
        assert captured_kwargs["tile_size"] == 0
        assert captured_kwargs["long_video_mode"] is False
