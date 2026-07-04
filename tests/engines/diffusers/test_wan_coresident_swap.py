"""Eager Wan pipe ↔ upscaler LRU swap (F-multi co-residency).

Root cause (pod 1ee3p98cogzxct, 2026-07-03): the eager Wan pipeline is a
module global OUTSIDE ``_LOADED``, so ``_ensure_on_gpu("flashvsr-…")``
saw zero eviction victims and FlashVSR OOM'd against Wan's ~75 GiB
(``CUDA out of memory. Tried to allocate 1.44 GiB … 77.81 GiB in use``).
The P4 design promises the LRU registry swaps Wan ↔ FlashVSR between
stages — these tests pin that promise for the eager pipe.
"""

from __future__ import annotations

import asyncio
import gc as _gc
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def _fake_cuda(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake torch.cuda with mutable free-memory + allocation counters."""
    # free=1GiB mirrors pod 1ee3p98cogzxct at OOM time (1.32 GiB free of
    # 79.14) — below the 2 GiB headroom margin, so eviction must fire.
    state = {"free": 1 * 1024**3, "total": 80 * 1024**3, "allocated": 70 * 1024**3}
    fake_cuda = types.SimpleNamespace(
        mem_get_info=lambda: (state["free"], state["total"]),
        empty_cache=lambda: None,
        memory_allocated=lambda: state["allocated"],
    )
    fake_torch = types.SimpleNamespace(cuda=fake_cuda)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    return state


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    srv._LOADED.clear()
    srv._WAN_REGISTRY_NAME = None
    yield
    srv._LOADED.clear()
    srv._WAN_REGISTRY_NAME = None


def _wan_pipe() -> MagicMock:
    p = MagicMock()
    p.calls = []
    p.to = MagicMock(side_effect=lambda dev: p.calls.append(f"to:{dev}"))
    return p


class TestEagerRegistration:
    def test_register_eager_wan_adds_registry_entry(
        self, _fake_cuda: dict[str, Any]
    ) -> None:
        """Bug caught: eager pipe invisible to the LRU → upscale OOM."""
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        pipe = _wan_pipe()
        srv._register_eager_wan(pipe)

        assert srv._WAN_REGISTRY_NAME is not None
        entry = srv._LOADED[srv._WAN_REGISTRY_NAME]
        assert entry["pipe"] is pipe
        assert entry["on_device"] == "cuda"
        # vram_bytes measured from live allocation, not guessed: the
        # eviction hard-floor check needs a real number for re-promotion.
        assert entry["vram_bytes"] == 70 * 1024**3


class TestUpscaleEvictsEagerWan:
    def test_flashvsr_load_drops_wan_to_disk(self, _fake_cuda: dict[str, Any]) -> None:
        """Bug caught: FlashVSR loads next to resident Wan → CUDA OOM.

        Wan is DISK-dropped, never CPU-moved: pods pin only 32 GiB min
        RAM (minMemoryInGb), so a ~70 GiB .to("cpu") would OOM-kill the
        container on small-RAM hosts; device_map="cuda" pipes also
        raise on .to(). Reload comes from the pod-local HF cache.
        """
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        wan = _wan_pipe()
        srv._register_eager_wan(wan)
        srv.pipe = wan

        def _free_on_drop() -> None:
            _fake_cuda["free"] = 75 * 1024**3

        monkey_gc = patch.object(_gc, "collect", side_effect=_free_on_drop)

        flash = MagicMock()
        flash.vram_bytes = 8 * 1024**3
        with monkey_gc, patch.object(srv, "_load_model_to_gpu", return_value=flash):
            entry = asyncio.run(srv._ensure_on_gpu("flashvsr-wan21-bfloat16"))

        assert entry["on_device"] == "cuda"
        assert srv._WAN_REGISTRY_NAME is not None
        wan_entry = srv._LOADED[srv._WAN_REGISTRY_NAME]
        assert wan_entry["on_device"] == "disk"
        assert wan_entry["pipe"] is None
        # The module-global reference must ALSO drop or the CUDA tensors
        # stay alive and no VRAM is actually freed.
        assert srv.pipe is None
        wan.to.assert_not_called()


class TestWorkerPromotesWanBack:
    def test_promote_evicts_upscaler_before_reloading_wan(
        self, _fake_cuda: dict[str, Any]
    ) -> None:
        """Bug caught: reloading a ~70 GiB Wan pipe BEFORE freeing the
        upscaler OOMs on the way back (78 GiB peak on an 80 GiB card).
        Eviction must complete before the reload fires; the reloaded
        pipe must land in BOTH the registry entry and the module global
        the worker loop reads.
        """
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        order: list[str] = []

        wan = MagicMock()
        srv._register_eager_wan(wan)
        assert srv._WAN_REGISTRY_NAME is not None
        srv._LOADED[srv._WAN_REGISTRY_NAME]["on_device"] = "disk"
        srv._LOADED[srv._WAN_REGISTRY_NAME]["pipe"] = None
        srv.pipe = None

        flash = MagicMock()

        def _flash_to(dev: str) -> None:
            order.append(f"flash:to:{dev}")
            if dev == "cpu":
                _fake_cuda["free"] = 75 * 1024**3

        flash.to = MagicMock(side_effect=_flash_to)
        srv._LOADED["flashvsr-wan21-bfloat16"] = srv.LoadedModel(
            name="flashvsr-wan21-bfloat16",
            pipe=flash,
            vram_bytes=8 * 1024**3,
            last_used_monotonic=0.0,
            on_device="cuda",
        )

        reloaded = MagicMock()

        def _reload() -> MagicMock:
            order.append("reload")
            return reloaded

        with patch.object(srv, "_load_pipeline", side_effect=_reload):
            srv._promote_wan_if_evicted()

        assert order.index("flash:to:cpu") < order.index("reload")
        assert srv._LOADED[srv._WAN_REGISTRY_NAME]["on_device"] == "cuda"
        assert srv._LOADED[srv._WAN_REGISTRY_NAME]["pipe"] is reloaded
        assert srv.pipe is reloaded
        assert srv._LOADED["flashvsr-wan21-bfloat16"]["on_device"] == "cpu"

    def test_promote_noop_when_wan_not_registered(
        self, _fake_cuda: dict[str, Any]
    ) -> None:
        """Bug caught: upscale-only pods (no eager Wan) must not crash
        the worker loop with a KeyError on every generate."""
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        srv._promote_wan_if_evicted()  # must not raise

    def test_promote_noop_when_wan_already_on_cuda(
        self, _fake_cuda: dict[str, Any]
    ) -> None:
        """Bug caught: gratuitous .to("cuda") round-trip on every job."""
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        wan = _wan_pipe()
        srv._register_eager_wan(wan)
        wan.to.reset_mock()

        srv._promote_wan_if_evicted()
        wan.to.assert_not_called()


class TestEvictionBeforeFreshLoad:
    def test_wan_evicted_before_flashvsr_constructor_runs(
        self, _fake_cuda: dict[str, Any]
    ) -> None:
        """Bug caught (pod 8bhz609nkvjqhx, 2026-07-03): _ensure_on_gpu
        loads the new model BEFORE _enforce_headroom, so FlashVSR's
        constructor OOMs against resident Wan and eviction never fires.
        The registry must free headroom for the incoming model FIRST.
        """
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        order: list[str] = []

        wan = MagicMock()
        srv._register_eager_wan(wan)
        srv.pipe = wan

        def _free_on_drop() -> None:
            order.append("drop")
            _fake_cuda["free"] = 75 * 1024**3

        flash = MagicMock()
        flash.vram_bytes = 8 * 1024**3

        def _loader(name: str) -> MagicMock:
            order.append("load")
            return flash

        with (
            patch.object(_gc, "collect", side_effect=_free_on_drop),
            patch.object(srv, "_load_model_to_gpu", side_effect=_loader),
        ):
            asyncio.run(srv._ensure_on_gpu("flashvsr-wan21-bfloat16"))

        assert "drop" in order and "load" in order
        assert order.index("drop") < order.index("load")

    def test_wan_drop_never_calls_to_cpu(self, _fake_cuda: dict[str, Any]) -> None:
        """Bug caught: .to("cpu") on the device_map="cuda" WanPipeline
        raises ValueError (accelerate hooks), and even when it works a
        ~70 GiB CPU copy OOM-kills 32 GiB-RAM hosts. The wan eviction
        path must never touch .to().
        """
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        wan = MagicMock()
        srv._register_eager_wan(wan)
        srv.pipe = wan
        assert srv._WAN_REGISTRY_NAME is not None

        def _free_on_drop() -> None:
            _fake_cuda["free"] = 75 * 1024**3

        with patch.object(_gc, "collect", side_effect=_free_on_drop):
            srv._evict_to_cpu(srv._WAN_REGISTRY_NAME)

        wan.to.assert_not_called()
        assert srv._LOADED[srv._WAN_REGISTRY_NAME]["on_device"] == "disk"
