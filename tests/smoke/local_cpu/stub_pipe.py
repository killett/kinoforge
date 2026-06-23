"""Faithful in-memory stub for wan_t2v_server's WanPipeline.

Tracks adapter state + enforces a configurable VRAM budget so the
``LoraSwapVramOomError`` rollback path runs end-to-end against the
real HTTP contract in Tier 1 (no CUDA, no diffusers weights).

P2 (2026-06-22): the stub honors the ``KINOFORGE_STUB_MOE=1`` env var
so the Tier-1 smoke can exercise the Wan-2.2-shape MoE pipeline shape
(two transformers, per-LoRA branch routing) without renting a GPU.
When the env is unset / "0", the stub presents the Wan-2.1 single-
transformer shape — pre-P2 behavior, no test breakage.
"""

from __future__ import annotations

import os
from typing import Any

_DEFAULT_ADAPTER_MB = 500
_DEFAULT_VRAM_BUDGET_MB = 80_000


class _TransformerRecorder:
    """Per-transformer set_adapters recorder for the Tier-1 MoE stub."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[float]]] = []

    def set_adapters(
        self,
        names: list[str],
        adapter_weights: list[float] | None = None,
    ) -> None:
        self.calls.append((list(names), list(adapter_weights or [])))


class _FaithfulStubPipe:
    """In-memory pipe stub. Toggles single-transformer (Wan 2.1) vs MoE
    (Wan 2.2) shape via ``KINOFORGE_STUB_MOE``.

    Attributes:
        transformer: Always present — Wan 2.1 routes through this.
        transformer_2: Present iff ``moe`` is true — Wan 2.2 routes the
            ``branch="low_noise"`` LoRAs here.
    """

    def __init__(self) -> None:
        self._loaded_adapters: list[tuple[str, int]] = []
        self._active: list[str] = []
        self._vram_budget_mb: int = int(
            os.environ.get("KINOFORGE_STUB_VRAM_BUDGET_MB", _DEFAULT_VRAM_BUDGET_MB)
        )
        # P2 (2026-06-22): MoE shape opt-in via env var. ``moe=True``
        # exposes ``transformer_2`` (separate recorder) so
        # ``_detect_moe_arity`` returns 2 and per-transformer activation
        # routes through the right recorder. Default ``False`` aliases
        # ``self.transformer = self`` so the pre-P2 VRAM budget check
        # (in pipe-level ``set_adapters``) still fires under the
        # per-transformer activation loop (``pipe.transformer.set_adapters``
        # routes back to the same method).
        self.moe: bool = os.environ.get("KINOFORGE_STUB_MOE", "0") not in ("", "0")
        if self.moe:
            self.transformer = _TransformerRecorder()
            self.transformer_2 = _TransformerRecorder()
        else:
            self.transformer = self  # type: ignore[assignment]

    def load_lora_weights(
        self,
        path: str,
        adapter_name: str,
        load_into_transformer_2: bool = False,  # noqa: ARG002 — P2 kwarg routed at activation step.
    ) -> None:
        self._loaded_adapters.append((adapter_name, _DEFAULT_ADAPTER_MB))

    def unload_lora_weights(self) -> None:
        self._loaded_adapters.clear()
        self._active = []

    def delete_adapters(self, names: list[str]) -> None:
        self._loaded_adapters = [
            (n, s) for n, s in self._loaded_adapters if n not in names
        ]
        self._active = [n for n in self._active if n not in names]

    def set_adapters(
        self,
        names: list[str],
        adapter_weights: list[float] | None = None,  # noqa: ARG002
    ) -> None:
        prospective = sum(size for n, size in self._loaded_adapters if n in names)
        if prospective > self._vram_budget_mb:
            raise RuntimeError("CUDA out of memory")
        self._active = list(names)

    def to(self, *_args: Any, **_kw: Any) -> _FaithfulStubPipe:
        return self


def _stub_diffusers_load() -> _FaithfulStubPipe:
    """Returns a fresh stub pipe — invoked via KINOFORGE_DIFFUSERS_LOAD_STUB."""
    return _FaithfulStubPipe()
