"""Grouping invariants for the `lora_swap:` cell variant.

`lora_swap:` cells must group by ``WarmAttachKey(base, engine, precision)``
ONLY — the LoRA stack is intentionally out, so a strength sweep that
varies the stack still packs into ONE warm pod. `generate:` cells stay
on full ``CapabilityKey`` (which includes the LoRA refs). `path:` cells
stay under the sentinel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from kinoforge.core.grid.executor import _cell_capability_key, _ResolvedCell
from kinoforge.core.grid.grouping import (
    _PATH_GROUP_KEY,
    group_cells_by_capability_key,
)
from kinoforge.core.interfaces import CapabilityKey, WarmAttachKey


def _fake_cfg(
    base: str = "hf:org/base",
    engine_kind: str = "diffusers",
    precision: str = "bf16",
    lora_refs: tuple[str, ...] = (),
) -> Any:
    cfg = MagicMock()
    base_model = MagicMock()
    base_model.kind = "base"
    base_model.ref = base
    cfg.models = [base_model]
    cfg.loras = [MagicMock(ref=r) for r in lora_refs]
    eng = MagicMock()
    eng.kind = engine_kind
    eng.precision = precision
    cfg.engine = eng
    return cfg


def _swap_cell(idx: int, cfg: Any) -> _ResolvedCell:
    return _ResolvedCell(
        idx=idx,
        caption=None,
        cfg_path=Path("/tmp/x.yaml"),
        effective_cfg=cfg,
        mp4_path=None,
        is_lora_swap=True,
    )


def _gen_cell(idx: int, cfg: Any) -> _ResolvedCell:
    return _ResolvedCell(
        idx=idx,
        caption=None,
        cfg_path=Path("/tmp/x.yaml"),
        effective_cfg=cfg,
        mp4_path=None,
        is_lora_swap=False,
    )


def _path_cell(idx: int) -> _ResolvedCell:
    return _ResolvedCell(
        idx=idx,
        caption=None,
        cfg_path=None,
        effective_cfg=None,
        mp4_path=Path("/tmp/foo.mp4"),
    )


# ---------------------------------------------------------------------------
# Direct _cell_capability_key behavior — swap returns WarmAttachKey hash,
# generate returns CapabilityKey hash; they MUST never collide for the
# same (base, engine, precision).
# ---------------------------------------------------------------------------


def test_swap_cell_capability_key_equals_warm_attach_key_hex() -> None:
    cfg = _fake_cfg(lora_refs=("civitai:1@1", "civitai:2@2"))
    cell = _swap_cell(0, cfg)
    expected = WarmAttachKey(
        base_model="hf:org/base", engine="diffusers", precision="bf16"
    ).derive()
    assert _cell_capability_key(cell) == expected


def test_swap_cell_capability_key_ignores_lora_stack() -> None:
    cell_a = _swap_cell(0, _fake_cfg(lora_refs=("civitai:1@1",)))
    cell_b = _swap_cell(1, _fake_cfg(lora_refs=("civitai:99@99",)))
    assert _cell_capability_key(cell_a) == _cell_capability_key(cell_b)


def test_generate_cell_capability_key_equals_full_capability_key_hex() -> None:
    cfg = _fake_cfg(lora_refs=("civitai:1@1",))
    cell = _gen_cell(0, cfg)
    expected = CapabilityKey(
        base_model="hf:org/base",
        loras=("civitai:1@1",),
        engine="diffusers",
        precision="bf16",
    ).derive()
    assert _cell_capability_key(cell) == expected


def test_swap_and_gen_keys_never_collide_on_identical_inputs() -> None:
    cfg = _fake_cfg(lora_refs=("civitai:1@1",))
    swap_key = _cell_capability_key(_swap_cell(0, cfg))
    gen_key = _cell_capability_key(_gen_cell(1, cfg))
    assert swap_key != gen_key


# ---------------------------------------------------------------------------
# Grouping behavior end-to-end.
# ---------------------------------------------------------------------------


def test_two_swap_cells_with_same_warm_attach_key_share_one_group() -> None:
    cfg_a = _fake_cfg(lora_refs=("civitai:1@1",))
    cfg_b = _fake_cfg(lora_refs=("civitai:2@2",))
    cells = [_swap_cell(0, cfg_a), _swap_cell(1, cfg_b)]
    groups = group_cells_by_capability_key(cells)
    assert len(groups) == 1
    [(_, group_cells)] = groups.items()
    assert [c.idx for c in group_cells] == [0, 1]


def test_swap_and_generate_with_same_factors_go_to_different_groups() -> None:
    cfg = _fake_cfg(lora_refs=("civitai:1@1",))
    cells = [_swap_cell(0, cfg), _gen_cell(1, cfg)]
    groups = group_cells_by_capability_key(cells)
    assert len(groups) == 2
    # Each group has exactly one cell:
    for cells_in_group in groups.values():
        assert len(cells_in_group) == 1


def test_swap_cells_with_different_precision_split_into_two_groups() -> None:
    cfg_bf16 = _fake_cfg(precision="bf16")
    cfg_fp8 = _fake_cfg(precision="fp8")
    groups = group_cells_by_capability_key(
        [_swap_cell(0, cfg_bf16), _swap_cell(1, cfg_fp8)]
    )
    assert len(groups) == 2


def test_swap_cells_with_different_base_model_split() -> None:
    groups = group_cells_by_capability_key(
        [
            _swap_cell(0, _fake_cfg(base="hf:org/a")),
            _swap_cell(1, _fake_cfg(base="hf:org/b")),
        ]
    )
    assert len(groups) == 2


def test_swap_cells_with_different_engine_split() -> None:
    groups = group_cells_by_capability_key(
        [
            _swap_cell(0, _fake_cfg(engine_kind="diffusers")),
            _swap_cell(1, _fake_cfg(engine_kind="comfyui")),
        ]
    )
    assert len(groups) == 2


def test_path_cells_still_land_under_sentinel() -> None:
    groups = group_cells_by_capability_key([_path_cell(0), _path_cell(1)])
    assert list(groups.keys()) == [_PATH_GROUP_KEY]
    assert [c.idx for c in groups[_PATH_GROUP_KEY]] == [0, 1]


def test_mixed_path_and_swap_cells_separate_keys() -> None:
    cfg = _fake_cfg()
    groups = group_cells_by_capability_key(
        [_path_cell(0), _swap_cell(1, cfg), _path_cell(2)]
    )
    # Path cells under sentinel; swap cell under WAK hash.
    assert _PATH_GROUP_KEY in groups
    assert [c.idx for c in groups[_PATH_GROUP_KEY]] == [0, 2]
    swap_keys = [k for k in groups if k != _PATH_GROUP_KEY]
    assert len(swap_keys) == 1
    assert [c.idx for c in groups[swap_keys[0]]] == [1]
