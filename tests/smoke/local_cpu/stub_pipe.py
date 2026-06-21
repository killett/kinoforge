"""Faithful in-memory stub for wan_t2v_server's WanPipeline.

Tracks adapter state + enforces a configurable VRAM budget so the
``LoraSwapVramOomError`` rollback path runs end-to-end against the
real HTTP contract in Tier 1 (no CUDA, no diffusers weights).
"""

from __future__ import annotations

import os
from typing import Any

_DEFAULT_ADAPTER_MB = 500
_DEFAULT_VRAM_BUDGET_MB = 80_000


class _FaithfulStubPipe:
    def __init__(self) -> None:
        self._loaded_adapters: list[tuple[str, int]] = []
        self._active: list[str] = []
        self._vram_budget_mb: int = int(
            os.environ.get("KINOFORGE_STUB_VRAM_BUDGET_MB", _DEFAULT_VRAM_BUDGET_MB)
        )

    def load_lora_weights(self, path: str, adapter_name: str) -> None:
        self._loaded_adapters.append((adapter_name, _DEFAULT_ADAPTER_MB))

    def unload_lora_weights(self) -> None:
        self._loaded_adapters.clear()
        self._active = []

    def delete_adapters(self, names: list[str]) -> None:
        self._loaded_adapters = [
            (n, s) for n, s in self._loaded_adapters if n not in names
        ]
        self._active = [n for n in self._active if n not in names]

    def set_adapters(self, names: list[str]) -> None:
        prospective = sum(size for n, size in self._loaded_adapters if n in names)
        if prospective > self._vram_budget_mb:
            raise RuntimeError("CUDA out of memory")
        self._active = list(names)

    def to(self, *_args: Any, **_kw: Any) -> _FaithfulStubPipe:
        return self


def _stub_diffusers_load() -> _FaithfulStubPipe:
    """Returns a fresh stub pipe — invoked via KINOFORGE_DIFFUSERS_LOAD_STUB."""
    return _FaithfulStubPipe()
