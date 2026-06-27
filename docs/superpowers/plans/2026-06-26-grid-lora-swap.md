# Grid `lora_swap:` cell variant — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kinoforge grid` `lora_swap:` cell variant so a strength sweep on Wan 2.2 14B uses ONE pod with `/lora/set_stack` swaps between cells instead of N cold-boots.

**Architecture:** New `LoraSwapCell` pydantic model in `src/kinoforge/core/grid/spec.py`. Group key collapses to `WarmAttachKey(base, engine, precision)` so all cells with the same base+engine+precision pack into one group. Grid executor's `_run_swap_group` cold-boots one pod for cell-1 via subprocess (existing `kinoforge generate` + new `--emit-provision-record <path>` flag), reads back the `pod_id`, then for cells 2..N invokes `kinoforge generate --attach-pod <id> --loras <heredoc>` which skips provision and routes the stack through the existing P3 `--loras` path → server `POST /lora/set_stack`. Group exit destroys the pod via `kinoforge destroy --id` + post-run `kinoforge list` residual probe. Failure policy lives on `GridSpec.on_swap_failure` (`strict|continue|classify`, default `classify`).

**Tech Stack:** Python 3.12, pydantic v2, asyncio subprocess, pixi, pytest, ruff/mypy/pre-commit. RunPod provider for live fires.

**User decisions (already made):**
- Q1 = A: new third top-level cell variant `lora_swap:` (mutex with `generate:`/`path:`).
- Q2 = A: subprocess shell-out (not in-process); grid executor invokes `kinoforge` subprocesses.
- Q3 = revised B: cell-1 cold-boots WITH its own stack via `--loras`; cells 2..N attach via `--attach-pod`.
- Q4 = C: `on_swap_failure` taxonomy (strict/continue/classify) with strict-mode fallback knob.
- Q5 = A: group key = `WarmAttachKey(base, engine, precision)` only; LoRA stack OUT.
- Q6 = A: one new flag `--attach-pod <id>` on existing `generate` (no new verbs). Spec self-review added a second new flag `--emit-provision-record <path>` (the only way the grid executor learns the cell-1 pod_id machine-readably; also useful for ad-hoc operator scripting).
- Q7 = A: ledger-based attach with strict `WarmAttachKey` match; no "any pod" escape hatch.
- Q8 = C: whole-group wall-clock cost + structured `.cost.json` sidecar.
- Q9 = A: `on_swap_failure` is a spec-level (whole-grid) knob.

**Spec:** `docs/superpowers/specs/2026-06-26-grid-lora-swap-design.md` (commit `3e2462f`).

**Existing surfaces used by this plan:**
- `WarmAttachKey` + `CapabilityKey` + `LoraStack` — `src/kinoforge/core/interfaces.py:262-340`. `WarmAttachKey.derive()` returns sha256 hex over `(base_model, engine, precision)`.
- `Ledger.find_pods_by_warm_attach_key(wak_hex)` — `src/kinoforge/core/lifecycle.py:591`. Returns list of entry dicts.
- `Ledger.touch(instance_id, **extra)` — `src/kinoforge/core/lifecycle.py:637`. Strict update (no-op on missing).
- `RedactionRegistry` — `src/kinoforge/core/redaction.py:36`. `instance().add(token, kind=...)`. Grid spec already registers title + captions at `src/kinoforge/core/grid/spec.py:201`.
- Grid executor private helpers — `src/kinoforge/core/grid/executor.py`: `_cell_capability_key` (line 98), `_resolve_spec_cells` (line 137), `_cell_output_dir` (line 197), `_build_generate_cmd` (line 202), `_run_one_cell` (line 266), `_run_group` (line 343), `_check_no_residual_pods` (line 397), `run_grid` (line 458).
- Existing `--loras` CLI surface — `src/kinoforge/cli/_main.py:499-515`. `_LorasOnceAction` enforces single-occurrence at argparse.
- Existing related flags — `--instance-id` (`_main.py:445-454`), `--force-attach` (`_main.py:455-464`), `--no-reuse` (`_main.py:465-476`), `--dry-run-swap` (`_main.py:488-498`). `--instance-id` uses full `CapabilityKey` (LoRA stack included) which is why we need a separate `--attach-pod` for the swap use case.
- `cost_rate_usd_per_hr` on InstanceStatus — `src/kinoforge/core/interfaces.py:50, 166`; populated from `pod.costPerHr` in RunPod provider at `src/kinoforge/providers/runpod/__init__.py:1008`.
- AST scan canon — `tests/test_no_unredacted_writes.py` (AC8 `# kinoforge:lora-redact-exempt`).

**Implementation note for the implementer:** `--attach-pod` is distinct from existing `--instance-id`. `--instance-id` runs the warm-reuse matcher (which can reject via `HEARTBEAT_UNKNOWN`, `IDLE_REAP`, etc.) AND enforces full `CapabilityKey` match (which always rejects when the LoRA stack differs — that's why a grid swap-mode cell can't reuse it). `--attach-pod` is the simpler "I just provisioned this pod; attach by ID with `WarmAttachKey`-only check; skip the matcher." Implementations should share the underlying deploy-session attach plumbing where possible but keep the validation surfaces distinct.

---

## File Structure

**Created:**
- `src/kinoforge/core/grid/swap_failures.py` — `SwapFailureAction` enum + `_classify_swap_failure()` + pattern-match helpers.
- `src/kinoforge/core/grid/cost_sidecar.py` — `CostSidecarBuilder` class; per-group cost polling + sidecar JSON writer.
- `tests/_smoke_harness/lora_swap_grid.py` — `write_lora_swap_grid_spec()` keyword-only helper.
- `tests/core/grid/test_spec_lora_swap.py` (~6 tests)
- `tests/core/grid/test_grouping_lora_swap.py` (~4 tests)
- `tests/core/grid/test_swap_failures.py` (~9 tests)
- `tests/core/grid/test_executor_swap_group.py` (~6 tests)
- `tests/core/grid/test_cost_sidecar.py` (~5 tests)
- `tests/cli/test_generate_attach_pod.py` (~8 tests)
- `tests/integration/grid/test_lora_swap_executor_integration.py` (~2 tests)

**Modified:**
- `src/kinoforge/core/grid/spec.py` — add `LoraStackEntry`, `LoraSwapCell`; extend `GridCell` 3-way mutex; add `GridSpec.on_swap_failure`; extend `_register_caption_tokens` to register `lora_swap.stack[*].ref`.
- `src/kinoforge/core/grid/executor.py` — extend `_cell_capability_key` dispatch; add `_run_swap_group`; extend `run_grid` dispatch; reuse `_check_no_residual_pods`.
- `src/kinoforge/cli/_main.py` — add `--attach-pod`, `--emit-provision-record` argparse on `p_generate`.
- `src/kinoforge/cli/_commands.py` — extend `cmd_generate` for `--attach-pod` / `--emit-provision-record` validation + dispatch.
- `src/kinoforge/core/orchestrator.py` (or wherever `deploy_session` lives — implementer to confirm) — add attach-mode branch when `attach_pod_id` is set; add provision-record write hook.
- `tests/test_no_unredacted_writes.py` — add AC10 for `LoraSwapCell.stack[*].ref` redaction invariant.
- `README.md` — extend `kinoforge grid` section with `lora_swap:` doc + `--attach-pod` / `--emit-provision-record` flag docs.
- `PROGRESS.md` — append CLOSED entry.
- `successful-generations.md` — amend §11 (`kinoforge grid` capability axis) with new "See also" line after Tier-4 live fire.

---

## Task 1: Spec models — `LoraSwapCell` + `LoraStackEntry` + 3-way mutex + `on_swap_failure`

**Goal:** Extend `src/kinoforge/core/grid/spec.py` so a YAML spec can declare `lora_swap:` cells alongside existing `generate:` / `path:` cells, and so `GridSpec` carries a whole-grid `on_swap_failure` knob. Register every `lora_swap.stack[*].ref` with `RedactionRegistry`.

**Files:**
- Modify: `src/kinoforge/core/grid/spec.py` (lines 36-215)
- Test: `tests/core/grid/test_spec_lora_swap.py` (NEW)

**Acceptance Criteria:**
- [ ] `LoraStackEntry(ref, strength, branch)` model exists with `extra="forbid"`, `strength` range `[-1.0, 2.0]`, `branch` literal `{"high","low","auto"}`.
- [ ] `LoraSwapCell(config, stack)` model exists with `extra="forbid"`, `stack: list[LoraStackEntry] = Field(min_length=0)` (empty legal per P3 D9).
- [ ] `GridCell` accepts exactly ONE of `generate:`, `path:`, `lora_swap:` (3-way mutex); pydantic `model_validator(mode="after")` raises on 0 or >1.
- [ ] `GridSpec.on_swap_failure: Literal["strict","continue","classify"] = "classify"` defaults correctly; non-literal value rejected.
- [ ] `_register_caption_tokens` registers `cell.lora_swap.stack[*].ref` with `RedactionRegistry.instance().add(token, kind="grid:lora_ref")`.

**Verify:** `pixi run pytest tests/core/grid/test_spec_lora_swap.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/core/grid/test_spec_lora_swap.py
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kinoforge.core.grid.spec import (
    GridSpec,
    LoraStackEntry,
    LoraSwapCell,
)
from kinoforge.core.redaction import RedactionRegistry


def _write_spec(tmp_outside_repo: Path, body: dict) -> Path:
    p = tmp_outside_repo / "spec.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_lora_stack_entry_extra_forbid_and_defaults():
    e = LoraStackEntry(ref="civitai:1@2")
    assert e.strength == 1.0
    assert e.branch == "auto"
    with pytest.raises(ValidationError):
        LoraStackEntry(ref="civitai:1@2", bogus_field="x")


def test_lora_stack_entry_strength_range_and_branch_literal():
    with pytest.raises(ValidationError):
        LoraStackEntry(ref="civitai:1@2", strength=2.5)
    with pytest.raises(ValidationError):
        LoraStackEntry(ref="civitai:1@2", strength=-1.5)
    with pytest.raises(ValidationError):
        LoraStackEntry(ref="civitai:1@2", branch="bogus")
    # boundary inclusive
    assert LoraStackEntry(ref="civitai:1@2", strength=2.0).strength == 2.0
    assert LoraStackEntry(ref="civitai:1@2", strength=-1.0).strength == -1.0


def test_lora_swap_cell_empty_stack_legal_and_extra_forbid(tmp_path):
    cfg = tmp_path / "base.yaml"
    cfg.write_text("version: 1\n")
    swap = LoraSwapCell(config=cfg, stack=[])
    assert swap.stack == []
    with pytest.raises(ValidationError):
        LoraSwapCell(config=cfg, stack=[], bogus="x")


def test_grid_cell_mutex_rejects_two_variants(tmp_outside_repo):
    body = {
        "budget_cap_usd": 1.0,
        "cells": [
            {
                "generate": {"config": str(tmp_outside_repo / "x.yaml")},
                "lora_swap": {
                    "config": str(tmp_outside_repo / "x.yaml"),
                    "stack": [],
                },
            }
        ],
    }
    spec_path = _write_spec(tmp_outside_repo, body)
    with pytest.raises(Exception) as ei:  # GridSpecParseError wraps ValidationError
        GridSpec.load(spec_path)
    assert "exactly one of" in str(ei.value)


def test_grid_spec_on_swap_failure_defaults_to_classify(tmp_outside_repo):
    cfg = tmp_outside_repo / "base.yaml"
    cfg.write_text("version: 1\n")
    body = {
        "budget_cap_usd": 1.0,
        "cells": [
            {"lora_swap": {"config": str(cfg), "stack": []}}
        ],
    }
    spec = GridSpec.load(_write_spec(tmp_outside_repo, body))
    assert spec.on_swap_failure == "classify"


def test_lora_swap_refs_registered_with_redaction_registry(tmp_outside_repo):
    RedactionRegistry._singleton = None  # reset for isolation
    cfg = tmp_outside_repo / "base.yaml"
    cfg.write_text("version: 1\n")
    body = {
        "budget_cap_usd": 1.0,
        "cells": [
            {
                "lora_swap": {
                    "config": str(cfg),
                    "stack": [{"ref": "civitai:42@99", "strength": 0.5}],
                }
            },
        ],
    }
    GridSpec.load(_write_spec(tmp_outside_repo, body))
    reg = RedactionRegistry.instance()
    # Token registered; redact() replaces it with the registered placeholder.
    out = reg.redact("debug log contains civitai:42@99 here")
    assert "civitai:42@99" not in out
```

Where `tmp_outside_repo` is a conftest fixture that returns a tmp directory NOT under the kinoforge repo (needed because `GridSpec.load` rejects under-repo paths). If the existing test file `tests/core/grid/test_spec.py` already defines such a fixture, reuse it; otherwise add to `tests/core/grid/conftest.py`:

```python
# tests/core/grid/conftest.py (add if missing)
import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_outside_repo(tmp_path_factory) -> Path:
    # tmp_path_factory roots under pytest's basetemp which is outside the repo
    # by default; assert explicitly to catch CI configurations that override.
    d = tmp_path_factory.mktemp("grid_spec")
    try:
        d.relative_to(Path(os.getcwd()))
        raise RuntimeError(
            f"tmp_outside_repo must be outside the repo; got {d}"
        )
    except ValueError:
        return d
```

- [ ] **Step 2: Run tests; confirm RED.**

```
pixi run pytest tests/core/grid/test_spec_lora_swap.py -v
```

Expected: every test fails with `ImportError` (`LoraStackEntry`, `LoraSwapCell` not importable) or `AttributeError` (`on_swap_failure` missing).

- [ ] **Step 3: Add models to `src/kinoforge/core/grid/spec.py`.**

Insert AFTER `class PathCell` (line ~76), BEFORE `class GridCell` (line ~78):

```python
class LoraStackEntry(BaseModel):
    """One LoRA reference in a `lora_swap:` cell's stack.

    Mirrors the P3 CLI `--loras` heredoc shape so the grid executor
    can serialize a list of these to a heredoc payload directly.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str
    strength: float = Field(default=1.0, ge=-1.0, le=2.0)
    branch: Literal["high", "low", "auto"] = "auto"


class LoraSwapCell(BaseModel):
    """Cell driving a server-side warm-attach LoRA-stack swap.

    Cells sharing the same `WarmAttachKey(base, engine, precision)`
    derived from `config` pack into one group; the group cold-boots
    one pod for cell-1 and attaches via `--attach-pod` for cells
    2..N. The cell's `stack` flows through the existing P3 `--loras`
    CLI surface and routes to `POST /lora/set_stack` on the warm pod.
    """

    model_config = ConfigDict(extra="forbid")

    config: Path
    stack: list[LoraStackEntry] = Field(min_length=0)
```

Extend `class GridCell` (replace the existing body):

```python
class GridCell(BaseModel):
    """One cell in the grid; exactly one of `generate`/`path`/`lora_swap` required."""

    model_config = ConfigDict(extra="forbid")

    generate: GenerateCell | None = None
    path: Path | None = None
    lora_swap: LoraSwapCell | None = None
    caption: str | None = None

    @model_validator(mode="after")
    def _check_variant(self) -> GridCell:
        n_set = sum(
            v is not None for v in (self.generate, self.path, self.lora_swap)
        )
        if n_set != 1:
            raise ValueError(
                "cell must declare exactly one of `generate:` / `path:` / `lora_swap:`"
            )
        return self
```

Add to `class GridSpec` (after `cells:` field):

```python
    on_swap_failure: Literal["strict", "continue", "classify"] = "classify"
```

Extend `_register_caption_tokens` (line ~201) — add inside the existing `for cell in spec.cells:` loop, after the `if cell.caption:` block:

```python
        if cell.lora_swap is not None:
            for entry in cell.lora_swap.stack:
                try:
                    reg.add(entry.ref, kind="grid:lora_ref")
                except ValueError:
                    pass
```

- [ ] **Step 4: Run tests; confirm GREEN.**

```
pixi run pytest tests/core/grid/test_spec_lora_swap.py -v
```

Expected: all 6 PASS.

- [ ] **Step 5: Run pre-commit.**

```
pixi run pre-commit run --files src/kinoforge/core/grid/spec.py tests/core/grid/test_spec_lora_swap.py tests/core/grid/conftest.py
```

Expected: all hooks pass (ruff, ruff-format, mypy).

- [ ] **Step 6: Commit.**

```
git add src/kinoforge/core/grid/spec.py tests/core/grid/test_spec_lora_swap.py tests/core/grid/conftest.py
git commit -m "feat(grid): LoraSwapCell + LoraStackEntry + GridSpec.on_swap_failure"
```

---

## Task 2: Grouping — `WarmAttachKey` derivation for `lora_swap:` cells

**Goal:** Extend `_cell_capability_key` in `src/kinoforge/core/grid/executor.py` so that `lora_swap:` cells group by `WarmAttachKey(base, engine, precision)` only (LoRA stack OUT). `generate:` cells stay on full `CapabilityKey`. `path:` cells stay on the sentinel. Mixed-variant grids route to disjoint groups by construction.

**Files:**
- Modify: `src/kinoforge/core/grid/executor.py` (lines 98-135 — `_cell_capability_key`); `_resolve_spec_cells` (lines 137-194) to load + apply overrides for `lora_swap:` cfgs too.
- Test: `tests/core/grid/test_grouping_lora_swap.py` (NEW)

**Acceptance Criteria:**
- [ ] Two `lora_swap:` cells whose cfgs derive the same `(base, engine, precision)` share a group key (regardless of LoRA stack differences).
- [ ] A `lora_swap:` cell + a `generate:` cell whose cfgs share the same `(base, engine, precision)` go to DIFFERENT groups (the `generate:` key includes LoRA refs; `WarmAttachKey.derive()` doesn't).
- [ ] `path:` cells still land under `_PATH_GROUP_KEY` sentinel.
- [ ] Changing `precision` on one of two `lora_swap:` cells splits the group.

**Verify:** `pixi run pytest tests/core/grid/test_grouping_lora_swap.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/core/grid/test_grouping_lora_swap.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from kinoforge.core.grid.executor import _ResolvedCell, _cell_capability_key
from kinoforge.core.grid.grouping import _PATH_GROUP_KEY, group_cells_by_capability_key


def _fake_cfg(base: str, engine_kind: str, precision: str, loras: list[str] | None = None):
    cfg = MagicMock()
    base_model = MagicMock()
    base_model.kind = "base"
    base_model.ref = base
    cfg.models = [base_model]
    cfg.loras = [MagicMock(ref=r) for r in (loras or [])]
    eng = MagicMock()
    eng.kind = engine_kind
    eng.precision = precision
    cfg.engine = eng
    return cfg


def _swap_cell(idx, cfg):
    return _ResolvedCell(
        idx=idx, caption=None, cfg_path=Path("/tmp/x.yaml"),
        effective_cfg=cfg, mp4_path=None, is_lora_swap=True,
    )


def _gen_cell(idx, cfg):
    return _ResolvedCell(
        idx=idx, caption=None, cfg_path=Path("/tmp/x.yaml"),
        effective_cfg=cfg, mp4_path=None, is_lora_swap=False,
    )


def _path_cell(idx):
    return _ResolvedCell(
        idx=idx, caption=None, cfg_path=None, effective_cfg=None,
        mp4_path=Path("/tmp/foo.mp4"), is_lora_swap=False,
    )


def test_two_swap_cells_same_warm_attach_key_share_group():
    cfg_a = _fake_cfg("hf:org/base", "diffusers", "bf16", loras=["civitai:1@1"])
    cfg_b = _fake_cfg("hf:org/base", "diffusers", "bf16", loras=["civitai:2@2"])
    cells = [_swap_cell(0, cfg_a), _swap_cell(1, cfg_b)]
    groups = group_cells_by_capability_key(cells)
    assert len(groups) == 1
    [(_, cells_in_group)] = groups.items()
    assert [c.idx for c in cells_in_group] == [0, 1]


def test_swap_and_generate_with_same_warm_attach_key_go_to_different_groups():
    cfg = _fake_cfg("hf:org/base", "diffusers", "bf16", loras=["civitai:1@1"])
    cells = [_swap_cell(0, cfg), _gen_cell(1, cfg)]
    groups = group_cells_by_capability_key(cells)
    # Different keys: swap key = WarmAttachKey hex; gen key = CapabilityKey hex; never collide.
    assert len(groups) == 2


def test_swap_cells_with_different_precision_split():
    cfg_a = _fake_cfg("hf:org/base", "diffusers", "bf16")
    cfg_b = _fake_cfg("hf:org/base", "diffusers", "fp8")
    groups = group_cells_by_capability_key([_swap_cell(0, cfg_a), _swap_cell(1, cfg_b)])
    assert len(groups) == 2


def test_path_cells_still_under_sentinel():
    groups = group_cells_by_capability_key([_path_cell(0), _path_cell(1)])
    assert list(groups.keys()) == [_PATH_GROUP_KEY]
    assert [c.idx for c in groups[_PATH_GROUP_KEY]] == [0, 1]
```

- [ ] **Step 2: Run tests; confirm RED.**

```
pixi run pytest tests/core/grid/test_grouping_lora_swap.py -v
```

Expected: import error on `is_lora_swap` (new `_ResolvedCell` field) and on `_cell_capability_key` returning the wrong key shape for swap cells.

- [ ] **Step 3: Extend `_ResolvedCell` and `_cell_capability_key` in `executor.py`.**

Add `is_lora_swap: bool = False` field to the existing `_ResolvedCell` dataclass (executor.py:60). Then replace `_cell_capability_key` (lines 98-134) with:

```python
def _cell_capability_key(cell: _ResolvedCell) -> str | None:
    """Derive the cell's grouping key from the effective Config.

    - `generate:` cells return full CapabilityKey.derive() (base + LoRAs +
      engine + precision; LoRA strength omitted so strength sweeps share
      one group on the generate path).
    - `lora_swap:` cells return WarmAttachKey.derive() (base + engine +
      precision ONLY; LoRA refs OUT) so all cells sharing those slow-
      rebuild factors pack into one warm-pod group regardless of stack.
    - `path:` cells return None (folded under `_PATH_GROUP_KEY`).

    Lives at module scope so tests can monkeypatch it.
    """
    cfg = cell.effective_cfg
    if cfg is None:
        return None
    try:
        from kinoforge.core.interfaces import CapabilityKey, WarmAttachKey

        base_models = [
            m for m in getattr(cfg, "models", []) if getattr(m, "kind", None) == "base"
        ]
        base_ref = base_models[0].ref if base_models else ""
        engine = getattr(cfg, "engine", None)
        engine_kind = getattr(engine, "kind", "") if engine is not None else ""
        precision = getattr(engine, "precision", "") if engine is not None else ""

        if cell.is_lora_swap:
            return WarmAttachKey(
                base_model=base_ref, engine=engine_kind, precision=precision
            ).derive()

        loras = getattr(cfg, "loras", []) or []
        lora_refs = tuple(lo.ref for lo in loras)
        return CapabilityKey(
            base_model=base_ref,
            loras=lora_refs,
            engine=engine_kind,
            precision=precision,
        ).derive()
    except Exception:  # noqa: BLE001 — defensive fallback for cfg shape drift
        return str(cfg)
```

Extend `_resolve_spec_cells` (line 137) to handle the new `lora_swap:` variant. Add this branch alongside the existing `generate:` and `path:` branches:

```python
        elif cell.lora_swap is not None:
            base = load_config(Path(cell.lora_swap.config))
            cfg_path = tmp_dir / f"cell_{i}.yaml"
            cfg_path.write_text(  # kinoforge:public-write
                yaml.safe_dump(base.model_dump(mode="json")),
            )
            resolved.append(
                _ResolvedCell(
                    idx=i,
                    caption=cell.caption,
                    cfg_path=cfg_path,
                    effective_cfg=base,
                    mp4_path=None,
                    is_lora_swap=True,
                    lora_swap_stack=list(cell.lora_swap.stack),
                )
            )
```

Add `lora_swap_stack: list[LoraStackEntry] | None = None` to `_ResolvedCell` (dataclass default). Import `LoraStackEntry` from `kinoforge.core.grid.spec` at module top.

- [ ] **Step 4: Run tests; confirm GREEN.**

```
pixi run pytest tests/core/grid/test_grouping_lora_swap.py tests/core/grid/test_spec_lora_swap.py -v
```

Expected: all PASS. Existing grid tests in `tests/core/grid/` must also stay GREEN — run the full directory:

```
pixi run pytest tests/core/grid/ -v
```

- [ ] **Step 5: Run pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/core/grid/executor.py tests/core/grid/test_grouping_lora_swap.py
git add src/kinoforge/core/grid/executor.py tests/core/grid/test_grouping_lora_swap.py
git commit -m "feat(grid): WarmAttachKey grouping for lora_swap cells"
```

---

## Task 6: Failure taxonomy — `swap_failures.py` module

**Goal:** New module `src/kinoforge/core/grid/swap_failures.py` exporting `SwapFailureAction` enum + `_classify_swap_failure(stderr, exit_code, policy)`. Pattern-match against P2 structured-error strings (`SwapRejectedDetails`, `VRAMRollbackFailure`, `BranchUnsupportedOnSingleTransformer`, etc) + transient HTTP errors (502, ProxyWarmupTimeout, ConnectionError) + OOM (exit 137). Retry budget = 3 attempts at 5s backoff. (Built before Task 5 because Task 5 imports `_classify_swap_failure`.)

**Files:**
- Create: `src/kinoforge/core/grid/swap_failures.py`
- Test: `tests/core/grid/test_swap_failures.py` (NEW, ~9 tests)

**Acceptance Criteria:**
- [ ] `SwapFailureAction` enum has exactly `RETRY`, `CONTINUE`, `ABORT`.
- [ ] `policy="strict"` → ALWAYS `ABORT` on any non-zero exit.
- [ ] `policy="classify"` → `RETRY` for 502 / `ProxyWarmupTimeout` / `ConnectionError`; `CONTINUE` for `SwapRejectedDetails` / `BranchUnsupportedOnSingleTransformer` / `BranchAutoNotAllowedOnMoE` / `BranchUnknown`; `ABORT` for `VRAMRollbackFailure` / `RunPodGraphQLError` / 5xx after retry / 137 (OOM) / unknown.
- [ ] `policy="continue"` → recoverable + ambiguous continue; truly unrecoverable (`VRAMRollbackFailure`, `RunPodGraphQLError`) still ABORT.
- [ ] Module exports `RETRY_MAX_ATTEMPTS = 3` and `RETRY_BACKOFF_S = 5.0`.

**Verify:** `pixi run pytest tests/core/grid/test_swap_failures.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/core/grid/test_swap_failures.py
from __future__ import annotations

import pytest

from kinoforge.core.grid.swap_failures import (
    RETRY_BACKOFF_S,
    RETRY_MAX_ATTEMPTS,
    SwapFailureAction,
    _classify_swap_failure,
)


def test_retry_constants():
    assert RETRY_MAX_ATTEMPTS == 3
    assert RETRY_BACKOFF_S == pytest.approx(5.0)


@pytest.mark.parametrize(
    "stderr",
    [
        "ProxyWarmupTimeout: pod proxy not ready after 60s",
        "ConnectionError: [Errno 111] Connection refused",
        "HTTPError: 502 Bad Gateway from https://x.proxy.runpod.net",
    ],
)
def test_classify_transient_returns_retry(stderr):
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.RETRY


@pytest.mark.parametrize(
    "stderr",
    [
        "SwapRejectedDetails: branch routing rejected by server: ...",
        "BranchUnsupportedOnSingleTransformer: pipe has 1 transformer",
        "BranchAutoNotAllowedOnMoE: must specify high or low on Wan 2.2 MoE",
        "BranchUnknown: branch=bogus not in {high, low, auto}",
    ],
)
def test_classify_recoverable_returns_continue(stderr):
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.CONTINUE


@pytest.mark.parametrize(
    "stderr",
    [
        "VRAMRollbackFailure: peft load_lora_weights failed, pipe state corrupted",
        "RunPodGraphQLError: podEditJob returned {errors: [...]}",
        "HTTPError: 500 Internal Server Error after 3 retries",
        "OOMKilled",
    ],
)
def test_classify_unrecoverable_returns_abort(stderr):
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.ABORT


def test_classify_exit_137_oom_returns_abort():
    assert _classify_swap_failure("", 137, "classify") is SwapFailureAction.ABORT


def test_classify_unknown_error_returns_abort():
    assert _classify_swap_failure("some unrelated traceback", 1, "classify") is SwapFailureAction.ABORT


def test_strict_policy_always_aborts():
    assert _classify_swap_failure(
        "ProxyWarmupTimeout: ...", 1, "strict"
    ) is SwapFailureAction.ABORT
    assert _classify_swap_failure("", 137, "strict") is SwapFailureAction.ABORT


def test_continue_policy_continues_on_ambiguous_but_aborts_on_unrecoverable():
    assert _classify_swap_failure(
        "some unrelated traceback", 1, "continue"
    ) is SwapFailureAction.CONTINUE
    assert _classify_swap_failure(
        "VRAMRollbackFailure: ...", 1, "continue"
    ) is SwapFailureAction.ABORT
    assert _classify_swap_failure(
        "RunPodGraphQLError: ...", 1, "continue"
    ) is SwapFailureAction.ABORT


def test_classify_zero_exit_never_called():
    # By contract: caller only invokes on non-zero exit. Document explicitly.
    # If somehow called with exit_code=0, return CONTINUE (no-op-ish).
    assert _classify_swap_failure("", 0, "classify") is SwapFailureAction.CONTINUE
```

- [ ] **Step 2: Run tests; confirm RED (ModuleNotFoundError).**

```
pixi run pytest tests/core/grid/test_swap_failures.py -v
```

- [ ] **Step 3: Create the module.**

```python
# src/kinoforge/core/grid/swap_failures.py
"""Classify swap-mode subprocess failures into RETRY / CONTINUE / ABORT.

Called by the grid executor's `_run_swap_group` when a cell subprocess
exits non-zero. The policy literal comes from `GridSpec.on_swap_failure`
and overrides the default `classify` behavior.

Failure-pattern catalogue is sourced from the P2 server-side fixes in
`src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (structured
exceptions: VRAMRollbackFailure, BranchUnsupportedOnSingleTransformer,
BranchAutoNotAllowedOnMoE, BranchUnknown, SwapRejectedDetails) and from
the proxy/HTTP edges documented by memories
`wan_server_set_stack_proxy_warmup` + `wan_t2v_server async blocking`.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final, Literal

RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_BACKOFF_S: Final[float] = 5.0


class SwapFailureAction(Enum):
    """What the executor should do next after a swap-cell subprocess fails."""

    RETRY = "retry"        # transient — retry within cell, up to RETRY_MAX_ATTEMPTS
    CONTINUE = "continue"  # recoverable — cell fails, pod state known-good
    ABORT = "abort"        # unrecoverable — destroy pod, fail remaining cells


_TRANSIENT_PATTERNS = (
    re.compile(r"ProxyWarmupTimeout", re.I),
    re.compile(r"ConnectionError", re.I),
    re.compile(r"\b502\b"),
)

_RECOVERABLE_PATTERNS = (
    re.compile(r"SwapRejectedDetails", re.I),
    re.compile(r"BranchUnsupportedOnSingleTransformer", re.I),
    re.compile(r"BranchAutoNotAllowedOnMoE", re.I),
    re.compile(r"BranchUnknown", re.I),
)

_UNRECOVERABLE_PATTERNS = (
    re.compile(r"VRAMRollbackFailure", re.I),
    re.compile(r"RunPodGraphQLError", re.I),
    re.compile(r"\b5\d\d\b.*after\s+\d+\s+retries", re.I),
    re.compile(r"OOMKilled", re.I),
)


def _matches_any(stderr: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(stderr) for p in patterns)


def _classify_swap_failure(
    stderr: str,
    exit_code: int,
    policy: Literal["strict", "continue", "classify"],
) -> SwapFailureAction:
    """Classify a swap-cell subprocess failure into the next action.

    Args:
        stderr: Captured stderr of the failed subprocess.
        exit_code: Subprocess return code. 0 returns CONTINUE (defensive).
        policy: Grid-level failure policy from `GridSpec.on_swap_failure`.

    Returns:
        The action the executor should take.
    """
    if exit_code == 0:
        return SwapFailureAction.CONTINUE
    if exit_code == 137:
        return SwapFailureAction.ABORT  # OOM-kill: pod under-resourced

    if policy == "strict":
        return SwapFailureAction.ABORT

    if _matches_any(stderr, _UNRECOVERABLE_PATTERNS):
        return SwapFailureAction.ABORT

    if policy == "continue":
        return SwapFailureAction.CONTINUE

    # classify (default)
    if _matches_any(stderr, _TRANSIENT_PATTERNS):
        return SwapFailureAction.RETRY
    if _matches_any(stderr, _RECOVERABLE_PATTERNS):
        return SwapFailureAction.CONTINUE
    return SwapFailureAction.ABORT
```

- [ ] **Step 4: Run tests; confirm GREEN.**

```
pixi run pytest tests/core/grid/test_swap_failures.py -v
```

- [ ] **Step 5: Pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/core/grid/swap_failures.py tests/core/grid/test_swap_failures.py
git add src/kinoforge/core/grid/swap_failures.py tests/core/grid/test_swap_failures.py
git commit -m "feat(grid): swap_failures module — classify/retry/continue/abort"
```

---

## Task 3: CLI — `--attach-pod` flag with ledger validation

**Goal:** Add `--attach-pod <id>` to `kinoforge generate`. Validate against the ledger (exists, status=running, `WarmAttachKey` match). Skip provision; route HTTP backend at the ledger's recorded `endpoint_url`. `--loras` continues to flow through `resolve_active_lora_stack` → server `/lora/set_stack`. Mutex with `--no-reuse` AND `--emit-provision-record` (Task 4).

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (line ~498 — add `--attach-pod` argument BEFORE `--loras`)
- Modify: `src/kinoforge/cli/_commands.py` (add validation in `cmd_generate`)
- Modify: `src/kinoforge/core/orchestrator.py` (or wherever `deploy_session` lives; implementer should grep for it — likely `src/kinoforge/core/orchestrator.py` or `_adapters.py`) — add attach-mode branch
- Test: `tests/cli/test_generate_attach_pod.py` (NEW, ~8 tests including the Task-4 emit-record mutex)

**Acceptance Criteria:**
- [ ] `--attach-pod ID` + `--no-reuse` → exit 1 with `--attach-pod and --no-reuse are mutually exclusive` in stderr.
- [ ] `--attach-pod ID` against ledger-missing pod → exit 1 with `pod ID not in ledger`.
- [ ] `--attach-pod ID` against `status != running` → exit 1 with `--attach-pod requires running`.
- [ ] `--attach-pod ID` against WarmAttachKey mismatch → exit 1 with `pod ID is base=... engine=... precision=... but cfg requires ...`.
- [ ] Happy-path: orchestrator's `engine.provision` is NOT called; HTTP backend points at ledger's `endpoint_url`; `--loras` flows through `resolve_active_lora_stack`; pod survives at end (no `destroy_instance` call).
- [ ] (From Task 4) `--attach-pod` + `--emit-provision-record` → exit 1 with `mutually exclusive: attach does not provision`.

**Verify:** `pixi run pytest tests/cli/test_generate_attach_pod.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Locate `deploy_session` (or equivalent).**

```
rg -n "def deploy_session\b|def _deploy_session\b|engine\.provision\b" src/kinoforge/core/
```

Identify the function that owns the provision → deploy → generate → destroy sequence today. That's the function that gets the new `attach_pod_id` parameter.

- [ ] **Step 2: Write the failing tests.**

```python
# tests/cli/test_generate_attach_pod.py
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Test harness imports may need adjustment to match existing tests/cli/ conventions.
# Reuse fixtures from tests/cli/conftest.py where possible.


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke `kinoforge generate` via main; return (rc, stdout, stderr)."""
    from io import StringIO
    from contextlib import redirect_stderr, redirect_stdout
    from kinoforge.cli._main import main

    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as e:
            rc = e.code or 0
    return int(rc or 0), out.getvalue(), err.getvalue()


def test_attach_pod_and_no_reuse_mutex(tmp_path, minimal_cfg_path):
    rc, _, err = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "abc", "--no-reuse"]
    )
    assert rc == 1
    assert "--attach-pod and --no-reuse are mutually exclusive" in err


def test_attach_pod_and_emit_provision_record_mutex(tmp_path, minimal_cfg_path):
    rc, _, err = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "abc",
         "--emit-provision-record", str(tmp_path / "p.json")]
    )
    assert rc == 1
    assert "mutually exclusive" in err
    assert "attach does not provision" in err


def test_attach_pod_missing_from_ledger(tmp_path, minimal_cfg_path, empty_ledger):
    rc, _, err = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "ghost-pod-id"]
    )
    assert rc == 1
    assert "ghost-pod-id" in err
    assert "not in ledger" in err


def test_attach_pod_status_not_running(tmp_path, minimal_cfg_path, ledger_with_destroyed_pod):
    rc, _, err = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "destroyed-pod"]
    )
    assert rc == 1
    assert "--attach-pod requires running" in err


def test_attach_pod_warm_attach_key_mismatch(
    tmp_path, minimal_cfg_path, ledger_with_wrong_kind_pod
):
    rc, _, err = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "wrong-kind-pod"]
    )
    assert rc == 1
    assert "wrong-kind-pod" in err
    assert "base=" in err and "engine=" in err and "precision=" in err


def test_attach_pod_happy_path_skips_provision(
    tmp_path, minimal_cfg_path, ledger_with_matching_running_pod, mock_engine
):
    rc, _, _ = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "good-pod"]
    )
    assert rc == 0
    mock_engine.provision.assert_not_called()
    # Pod survives: destroy_instance NOT called by orchestrator's cleanup.
    mock_engine.destroy.assert_not_called()


def test_attach_pod_loras_flows_through_resolve_active_lora_stack(
    tmp_path, minimal_cfg_path, ledger_with_matching_running_pod,
    mock_engine, capture_set_stack_payload,
):
    rc, _, _ = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--attach-pod", "good-pod",
         "--loras", "civitai:42@99 0.5 high\n"]
    )
    assert rc == 0
    payload = capture_set_stack_payload.payload
    assert payload is not None
    assert payload["loras"][0]["ref"] == "civitai:42@99"
    assert payload["loras"][0]["strength"] == 0.5
    assert payload["loras"][0]["branch"] == "high"


def test_attach_pod_emit_provision_record_happy_path_writes_json(
    tmp_path, minimal_cfg_path, mock_engine_with_provision,
):
    # Sanity test for Task 4's record-emission path under happy provision.
    record_path = tmp_path / "p.json"
    rc, _, _ = _run_cli(
        ["generate", "-c", str(minimal_cfg_path),
         "--prompt", "x", "--mode", "t2v",
         "--no-reuse",
         "--emit-provision-record", str(record_path)]
    )
    assert rc == 0
    assert record_path.exists()
    rec = json.loads(record_path.read_text())
    assert "pod_id" in rec and rec["pod_id"]
    assert "endpoint_url" in rec
    assert "provider" in rec
    assert "warm_attach_key" in rec
    assert "provision_ts" in rec
```

Fixtures `minimal_cfg_path`, `empty_ledger`, `ledger_with_*`, `mock_engine`, `mock_engine_with_provision`, `capture_set_stack_payload` should live in `tests/cli/conftest.py` — implementer should grep existing test files for the matching fixture shapes and either reuse or extend. Concrete fixture skeletons (add to `tests/cli/conftest.py`):

```python
@pytest.fixture
def minimal_cfg_path(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("""
version: 1
prompt: "test prompt"
mode: t2v
models:
  - kind: base
    ref: hf:test/base
engine:
  kind: diffusers
  precision: bf16
loras: []
output:
  dir: """ + str(tmp_path / "out") + "\n")
    return p

@pytest.fixture
def empty_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("KINOFORGE_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    return tmp_path / "ledger.jsonl"

# ledger_with_* fixtures append entries with matching/mismatched warm_attach_key + status fields
```

- [ ] **Step 3: Run tests; confirm RED.**

```
pixi run pytest tests/cli/test_generate_attach_pod.py -v
```

- [ ] **Step 4: Add argparse for `--attach-pod` in `src/kinoforge/cli/_main.py`.**

In the `p_generate` block (after `--dry-run-swap`, before `--loras` — around line 498):

```python
    p_generate.add_argument(
        "--attach-pod",
        type=str,
        default=None,
        metavar="POD_ID",
        help=(
            "attach to an existing warm pod from the ledger; skip provision; "
            "pod survives at end. Pod must be ledger status=running AND match "
            "cfg's WarmAttachKey(base, engine, precision). For grid lora_swap "
            "use case primarily; distinct from --instance-id which uses full "
            "CapabilityKey (and would reject a different LoRA stack). "
            "Mutex with --no-reuse and --emit-provision-record."
        ),
    )
```

- [ ] **Step 5: Add validation in `cmd_generate` in `src/kinoforge/cli/_commands.py`.**

Find `cmd_generate` in `_commands.py`. Add validation block BEFORE the orchestrator dispatch:

```python
    if getattr(args, "attach_pod", None) is not None:
        if getattr(args, "no_reuse", False):
            print(
                "--attach-pod and --no-reuse are mutually exclusive: "
                "--attach-pod implies pod survival",
                file=sys.stderr,
            )
            return 1
        if getattr(args, "emit_provision_record", None) is not None:
            print(
                "--attach-pod and --emit-provision-record are mutually "
                "exclusive: attach does not provision",
                file=sys.stderr,
            )
            return 1

        from kinoforge.core.interfaces import WarmAttachKey
        from kinoforge.core.lifecycle import Ledger

        ledger = Ledger.open()  # use the same Ledger factory the rest of cmd_generate uses
        entry = ledger.get(args.attach_pod)  # implementer: confirm exact method name
        if entry is None:
            print(
                f"pod {args.attach_pod} not in ledger; cannot attach "
                f"(ledger path: {ledger.path})",
                file=sys.stderr,
            )
            return 1
        if entry.get("status") != "running":
            print(
                f"pod {args.attach_pod} has ledger status="
                f"{entry.get('status')!r}; --attach-pod requires running",
                file=sys.stderr,
            )
            return 1

        # WarmAttachKey check
        cfg = load_config(Path(args.config))
        base_models = [m for m in (cfg.models or []) if m.kind == "base"]
        cfg_wak = WarmAttachKey(
            base_model=base_models[0].ref if base_models else "",
            engine=cfg.engine.kind if cfg.engine else "",
            precision=cfg.engine.precision if cfg.engine else "",
        ).derive()
        if entry.get("warm_attach_key") != cfg_wak:
            print(
                f"pod {args.attach_pod} is base="
                f"{entry.get('base_model','?')} engine="
                f"{entry.get('engine','?')} precision="
                f"{entry.get('precision','?')} but cfg requires base="
                f"{base_models[0].ref if base_models else '?'} "
                f"engine={cfg.engine.kind if cfg.engine else '?'} "
                f"precision={cfg.engine.precision if cfg.engine else '?'}",
                file=sys.stderr,
            )
            return 1

        # All validated; dispatch attach-mode deploy_session.
        return _cmd_generate_attach_mode(args, cfg, entry)
```

Implementer: replace `Ledger.open()` and `ledger.get(args.attach_pod)` with the actual ledger factory + lookup method names — grep `Ledger` usage in `_commands.py` to match the existing pattern. `_cmd_generate_attach_mode` is a new private helper that wraps the existing deploy-session call with `provision_skip=True, endpoint_url_override=entry["endpoint_url"]` (or equivalent — implementer to wire to the actual `deploy_session` signature change in Step 6).

- [ ] **Step 6: Extend `deploy_session` (or equivalent) to support attach mode.**

Add `attach_pod_id: str | None = None` parameter. When set:
- Skip `engine.provision(...)`.
- Construct the engine's HTTP backend pointed at the ledger entry's `endpoint_url`.
- Do NOT add a destroy callback to the cleanup chain (pod survives).
- Existing `--loras` → `resolve_active_lora_stack` → `set_stack` flow remains untouched.

Pseudocode:

```python
async def deploy_session(
    *,
    cfg: Config,
    ...,
    attach_pod_id: str | None = None,
    emit_provision_record: Path | None = None,
):
    if attach_pod_id is not None:
        entry = ledger.get(attach_pod_id)
        backend = engine.build_backend_from_endpoint(entry["endpoint_url"])
        # skip provision; skip destroy
    else:
        result = await engine.provision(...)
        backend = result.backend
        if emit_provision_record is not None:
            _write_provision_record(emit_provision_record, result)
        # existing destroy wiring stays as-is
    ...
```

Implementer: adjust to match the actual `deploy_session` signature; this is a sketch.

- [ ] **Step 7: Run tests; confirm GREEN.**

```
pixi run pytest tests/cli/test_generate_attach_pod.py -v
pixi run pytest tests/cli/ tests/core/ -v  # full sanity
```

- [ ] **Step 8: Pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py src/kinoforge/core/orchestrator.py tests/cli/test_generate_attach_pod.py
git add -- src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py src/kinoforge/core/orchestrator.py tests/cli/test_generate_attach_pod.py tests/cli/conftest.py
git commit -m "feat(cli): --attach-pod flag with ledger + WarmAttachKey validation"
```

---

## Task 4: CLI — `--emit-provision-record` flag

**Goal:** Add `--emit-provision-record <path>` to `kinoforge generate`. On successful provision, write JSON `{pod_id, endpoint_url, provider, warm_attach_key, provision_ts}` to the target path. Not written on provision failure. Mutex with `--attach-pod` (already covered in Task 3). Useful for grid swap-mode AND any operator scripting the CLI.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (argparse addition)
- Modify: `src/kinoforge/core/orchestrator.py` (or wherever provision returns) — call `_write_provision_record()` on success
- Modify: `src/kinoforge/cli/_commands.py` (thread `emit_provision_record` into `deploy_session` call)
- Test: covered by `test_attach_pod_emit_provision_record_happy_path_writes_json` (Task 3) + the mutex test.

**Acceptance Criteria:**
- [ ] `--emit-provision-record PATH` writes JSON with keys `{pod_id, endpoint_url, provider, warm_attach_key, provision_ts}` on successful provision.
- [ ] File is NOT written when provision fails (subprocess exits non-zero, no provision returned).
- [ ] `provision_ts` is ISO-8601 LOCAL TZ (per `feedback_local_timezone_only` memory — never UTC).
- [ ] `warm_attach_key` value matches `WarmAttachKey.derive()` over the cfg's `(base_model, engine.kind, engine.precision)`.

**Verify:** Tests from Task 3 cover this; `pixi run pytest tests/cli/test_generate_attach_pod.py::test_attach_pod_emit_provision_record_happy_path_writes_json -v`.

**Steps:**

- [ ] **Step 1: Add argparse in `_main.py` (right after `--attach-pod` from Task 3).**

```python
    p_generate.add_argument(
        "--emit-provision-record",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "on successful provision, write a JSON record "
            "{pod_id, endpoint_url, provider, warm_attach_key, provision_ts} "
            "to PATH. Used by `kinoforge grid` swap-mode and by operator "
            "scripting. Not written on provision failure. Mutex with --attach-pod."
        ),
    )
```

- [ ] **Step 2: Add the writer in orchestrator (or wherever provision returns).**

```python
def _write_provision_record(
    path: Path,
    *,
    pod_id: str,
    endpoint_url: str,
    provider: str,
    warm_attach_key: str,
) -> None:
    """Write a provision-record JSON file (consumed by grid swap-mode / ad-hoc scripting)."""
    from datetime import datetime
    record = {
        "pod_id": pod_id,
        "endpoint_url": endpoint_url,
        "provider": provider,
        "warm_attach_key": warm_attach_key,
        "provision_ts": datetime.now().isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")  # kinoforge:public-write
```

Call site: inside the existing `deploy_session` after `engine.provision()` returns successfully and before any subsequent step that could raise:

```python
result = await engine.provision(...)
if emit_provision_record is not None:
    _write_provision_record(
        emit_provision_record,
        pod_id=result.instance_id,
        endpoint_url=result.endpoint_url,
        provider=result.provider_name,
        warm_attach_key=cfg_warm_attach_key,
    )
```

`cfg_warm_attach_key` derived once at the top of `deploy_session`.

- [ ] **Step 3: Run the emission test; confirm GREEN.**

```
pixi run pytest tests/cli/test_generate_attach_pod.py::test_attach_pod_emit_provision_record_happy_path_writes_json -v
```

- [ ] **Step 4: Pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/core/orchestrator.py
git add -- src/kinoforge/cli/_main.py src/kinoforge/core/orchestrator.py
git commit -m "feat(cli): --emit-provision-record flag for machine-readable pod handoff"
```

---

## Task 7: Cost sidecar writer

**Goal:** New module `src/kinoforge/core/grid/cost_sidecar.py` with `CostSidecarBuilder` class: tracks per-group provision_ts + cost_per_hr + accrued cost; computes total at grid exit; writes `<grid_out>.cost.json` always (success + partial + abort) per § 6 schema. Drives cap-trip detection.

**Files:**
- Create: `src/kinoforge/core/grid/cost_sidecar.py`
- Test: `tests/core/grid/test_cost_sidecar.py` (NEW, ~5 tests)

**Acceptance Criteria:**
- [ ] `CostSidecarBuilder.start_group(group_key, pod_id, provider, cost_per_hr)` records a group's provision_ts.
- [ ] `record_cell(group_key, cell_idx, gen_wall_time_s, swap_wall_time_s, status, mp4_sha256, size_bytes)` appends a cell record.
- [ ] `mark_cell_error(group_key, cell_idx, error_msg)` records a `status="budget_killed"` or `status="failed"` cell with error.
- [ ] `total_cost_usd()` returns sum of `(now - provision_ts) * cost_per_hr / 3600` across groups (called at finish).
- [ ] `write(out_path)` emits the sidecar JSON with the schema from spec § 6 (`grid_id`, `spec_path` redacted via `RedactionRegistry.instance().redact()`, `out_mp4`, `total_cost_usd`, `budget_cap_usd`, `wall_time_s`, `groups[…]`).
- [ ] Sidecar always written, even on partial / aborted runs.

**Verify:** `pixi run pytest tests/core/grid/test_cost_sidecar.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/core/grid/test_cost_sidecar.py
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kinoforge.core.grid.cost_sidecar import CostSidecarBuilder


def test_start_group_and_record_cell_writes_schema(tmp_path):
    b = CostSidecarBuilder(
        grid_id="grid-1",
        spec_path=Path("/outside/spec.yaml"),
        out_mp4=Path("/outside/out.mp4"),
        budget_cap_usd=2.0,
    )
    b.start_group(
        group_key="abc", pod_id="pod-1", provider="runpod", cost_per_hr=1.0,
        provision_ts=datetime(2026, 6, 26, 12, 0, 0),
    )
    b.record_cell(
        "abc", cell_idx=0, gen_wall_time_s=10.0, swap_wall_time_s=0.0,
        status="ok", mp4_sha256="abc123", size_bytes=12345,
    )
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 30, 0))  # +30 min
    data = json.loads(out.read_text())
    assert data["grid_id"] == "grid-1"
    assert data["budget_cap_usd"] == 2.0
    assert data["groups"][0]["cost_per_hr_usd"] == 1.0
    assert data["groups"][0]["cost_usd"] == pytest.approx(0.5)
    assert data["total_cost_usd"] == pytest.approx(0.5)
    assert data["groups"][0]["cells"][0]["status"] == "ok"
    assert data["groups"][0]["cells"][0]["mp4_sha256"] == "abc123"


def test_total_cost_sums_across_groups(tmp_path):
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 5.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026,6,26,12,0))
    b.start_group("b", "p2", "runpod", 2.0, provision_ts=datetime(2026,6,26,12,30))
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 13, 0))
    data = json.loads(out.read_text())
    assert data["groups"][0]["cost_usd"] == pytest.approx(1.0)  # 60 min @ $1
    assert data["groups"][1]["cost_usd"] == pytest.approx(1.0)  # 30 min @ $2
    assert data["total_cost_usd"] == pytest.approx(2.0)


def test_mark_cell_error_sets_status_and_error(tmp_path):
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 1.0)
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026,6,26,12,0))
    b.mark_cell_error("a", cell_idx=2, status="budget_killed", error="cap reached")
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    cell = data["groups"][0]["cells"][0]
    assert cell["status"] == "budget_killed"
    assert cell["error"] == "cap reached"
    assert cell["idx"] == 2


def test_spec_path_redacted_via_registry(tmp_path):
    from kinoforge.core.redaction import RedactionRegistry
    RedactionRegistry._singleton = None
    reg = RedactionRegistry.instance()
    reg.add("/outside/secret/spec.yaml", kind="grid:spec_path")
    b = CostSidecarBuilder(
        "g", Path("/outside/secret/spec.yaml"), Path("/o/out.mp4"), 1.0,
    )
    b.start_group("a", "p1", "runpod", 1.0, provision_ts=datetime(2026,6,26,12,0))
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    assert "/outside/secret/spec.yaml" not in data["spec_path"]


def test_writes_even_with_no_groups(tmp_path):
    b = CostSidecarBuilder("g", Path("/o/spec.yaml"), Path("/o/out.mp4"), 1.0)
    out = tmp_path / "out.cost.json"
    b.write(out, now=datetime(2026, 6, 26, 12, 0))
    data = json.loads(out.read_text())
    assert data["groups"] == []
    assert data["total_cost_usd"] == 0.0
```

- [ ] **Step 2: Run tests; confirm RED.**

- [ ] **Step 3: Create `src/kinoforge/core/grid/cost_sidecar.py`.**

```python
"""Per-grid cost sidecar — writes <grid_out>.cost.json per spec § 6.

Polled by the grid executor; cap-trip check between cells. Sidecar
always written, even on partial / aborted runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kinoforge.core.redaction import RedactionRegistry


@dataclass
class _CellRecord:
    idx: int
    gen_wall_time_s: float = 0.0
    swap_wall_time_s: float = 0.0
    status: str = "ok"
    mp4_sha256: str | None = None
    size_bytes: int | None = None
    error: str | None = None


@dataclass
class _GroupRecord:
    key: str
    pod_id: str
    provider: str
    cost_per_hr_usd: float
    provision_ts: datetime
    cells: list[_CellRecord] = field(default_factory=list)


class CostSidecarBuilder:
    """Build and write the per-grid cost sidecar JSON."""

    def __init__(
        self,
        grid_id: str,
        spec_path: Path,
        out_mp4: Path,
        budget_cap_usd: float,
    ) -> None:
        self.grid_id = grid_id
        self.spec_path = spec_path
        self.out_mp4 = out_mp4
        self.budget_cap_usd = budget_cap_usd
        self._groups: dict[str, _GroupRecord] = {}
        self._start_ts = datetime.now()

    def start_group(
        self,
        group_key: str,
        pod_id: str,
        provider: str,
        cost_per_hr: float,
        *,
        provision_ts: datetime | None = None,
    ) -> None:
        self._groups[group_key] = _GroupRecord(
            key=group_key,
            pod_id=pod_id,
            provider=provider,
            cost_per_hr_usd=cost_per_hr,
            provision_ts=provision_ts or datetime.now(),
        )

    def record_cell(
        self,
        group_key: str,
        *,
        cell_idx: int,
        gen_wall_time_s: float,
        swap_wall_time_s: float,
        status: str = "ok",
        mp4_sha256: str | None = None,
        size_bytes: int | None = None,
    ) -> None:
        self._groups[group_key].cells.append(
            _CellRecord(
                idx=cell_idx,
                gen_wall_time_s=gen_wall_time_s,
                swap_wall_time_s=swap_wall_time_s,
                status=status,
                mp4_sha256=mp4_sha256,
                size_bytes=size_bytes,
            )
        )

    def mark_cell_error(
        self, group_key: str, *, cell_idx: int, status: str, error: str
    ) -> None:
        self._groups[group_key].cells.append(
            _CellRecord(idx=cell_idx, status=status, error=error)
        )

    def group_cost(self, group_key: str, *, now: datetime | None = None) -> float:
        g = self._groups[group_key]
        elapsed = ((now or datetime.now()) - g.provision_ts).total_seconds()
        return max(0.0, elapsed) * g.cost_per_hr_usd / 3600.0

    def total_cost_usd(self, *, now: datetime | None = None) -> float:
        n = now or datetime.now()
        return sum(self.group_cost(k, now=n) for k in self._groups)

    def write(self, out_path: Path, *, now: datetime | None = None) -> None:
        n = now or datetime.now()
        reg = RedactionRegistry.instance()
        data = {
            "grid_id": self.grid_id,
            "spec_path": reg.redact(str(self.spec_path)),
            "out_mp4": str(self.out_mp4),
            "total_cost_usd": round(self.total_cost_usd(now=n), 6),
            "budget_cap_usd": self.budget_cap_usd,
            "wall_time_s": (n - self._start_ts).total_seconds(),
            "groups": [
                {
                    "key": g.key,
                    "pod_id": g.pod_id,
                    "provider": g.provider,
                    "cost_per_hr_usd": g.cost_per_hr_usd,
                    "wall_time_s": (n - g.provision_ts).total_seconds(),
                    "cost_usd": round(self.group_cost(g.key, now=n), 6),
                    "cell_indices": [c.idx for c in g.cells],
                    "cells": [
                        {
                            k: v
                            for k, v in {
                                "idx": c.idx,
                                "gen_wall_time_s": c.gen_wall_time_s,
                                "swap_wall_time_s": c.swap_wall_time_s,
                                "status": c.status,
                                "mp4_sha256": c.mp4_sha256,
                                "size_bytes": c.size_bytes,
                                "error": c.error,
                            }.items()
                            if v is not None
                        }
                        for c in g.cells
                    ],
                }
                for g in self._groups.values()
            ],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, indent=2) + "\n")  # kinoforge:public-write
```

- [ ] **Step 4: Run tests; confirm GREEN.**

- [ ] **Step 5: Pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/core/grid/cost_sidecar.py tests/core/grid/test_cost_sidecar.py
git add -- src/kinoforge/core/grid/cost_sidecar.py tests/core/grid/test_cost_sidecar.py
git commit -m "feat(grid): cost sidecar builder + .cost.json schema"
```

---

## Task 8: AST scan AC10 — `lora_swap.stack[*].ref` redaction invariant

**Goal:** Extend `tests/test_no_unredacted_writes.py` with AC10: every code path writing `LoraSwapCell.stack[*].ref` to a file/log MUST go through `RedactionRegistry.redact()` (or carry an explicit exemption tag). Mirrors existing AC8 (InventorySnapshot) and AC9 (Ledger.touch) shapes.

**Files:**
- Modify: `tests/test_no_unredacted_writes.py`

**Acceptance Criteria:**
- [ ] New `test_ac10_*` function exists.
- [ ] AC10 fails on any `Path.write_text(...)` / `print(...)` / `logger.*(...)` call whose argument flows from `LoraSwapCell.stack[*].ref` OR `LoraStackEntry.ref` without `RedactionRegistry.instance().redact()` wrapping (or an explicit `# kinoforge:lora-redact-exempt` tag).
- [ ] Existing AC1-AC9 still pass.

**Verify:** `pixi run pytest tests/test_no_unredacted_writes.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Read existing AC8/AC9 implementations** in `tests/test_no_unredacted_writes.py` to understand the AST-walk + exemption-tag pattern. The new AC10 follows the same shape — replace the AST predicate to detect `LoraSwapCell.stack` / `LoraStackEntry.ref` source attribution.

- [ ] **Step 2: Add the new AC10 test.**

```python
# tests/test_no_unredacted_writes.py — append to existing file

def test_ac10_lora_swap_stack_refs_must_go_through_redaction():
    """AC10: every write/log of LoraSwapCell.stack[*].ref must be redacted.

    Catches the silent-leak failure mode where a future devleaks a swap
    cell's LoRA ref into PROGRESS.md / a log / stdout. Same shape as AC8
    (InventorySnapshot) and AC9 (Ledger.touch).
    """
    offenders: list[str] = []
    for py in _all_py_files():
        source = py.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match: writes/logs that pull from a name shaped like
            # `*.lora_swap.stack[*].ref` or `*.stack[*].ref` where the
            # enclosing function references LoraSwapCell / LoraStackEntry.
            if not _is_write_or_log_call(node):
                continue
            if not _arg_traces_to_lora_swap_ref(node, tree):
                continue
            extent = _call_lines(source, node.lineno, getattr(node, "end_lineno", None))
            if EXEMPT_LORA in extent or EXEMPT_WRITE in extent:
                continue
            # Allow goes-through-redact wrapper:
            if "RedactionRegistry" in extent and "redact" in extent:
                continue
            offenders.append(f"{py.relative_to(SRC.parent.parent)}:{node.lineno}")
    assert not offenders, (
        f"AC10: LoraSwapCell.stack[*].ref written/logged without redaction:\n"
        + "\n".join(offenders)
        + f"\n\n{REFERENCE}"
    )


def _is_write_or_log_call(node: ast.Call) -> bool:
    """True if node is Path.write_text / print / logger.<level>."""
    if not isinstance(node.func, ast.Attribute):
        return isinstance(node.func, ast.Name) and node.func.id == "print"
    name = node.func.attr
    if name in {"write_text", "write_bytes", "debug", "info", "warning", "error", "critical"}:
        return True
    return False


def _arg_traces_to_lora_swap_ref(node: ast.Call, tree: ast.AST) -> bool:
    """Heuristic: any argument string literal-or-fstring references `.ref` from a
    LoraStackEntry or `lora_swap.stack` traversal.

    The conservative shape: walk the call args + look for attribute chains
    that include `lora_swap.stack` or whose enclosing function carries a
    type annotation referencing LoraSwapCell / LoraStackEntry.
    """
    func = _enclosing_func(tree, node)
    if func is None:
        return False
    src_text = ast.unparse(func)
    if "LoraSwapCell" not in src_text and "LoraStackEntry" not in src_text:
        return False
    if "lora_swap" not in src_text and "LoraStackEntry" not in src_text:
        return False
    arg_text = " ".join(ast.unparse(a) for a in node.args)
    # The ref is the leaky payload; look for `.ref` access in the call args.
    return ".ref" in arg_text
```

- [ ] **Step 3: Run AC10; confirm RED if a write site exists, GREEN if not.**

```
pixi run pytest tests/test_no_unredacted_writes.py::test_ac10_lora_swap_stack_refs_must_go_through_redaction -v
```

Expected: GREEN (no current site writes `lora_swap.stack[*].ref` unredacted; cost sidecar writes the redacted spec_path, not refs). If RED, fix the offending site or add `# kinoforge:lora-redact-exempt` tag with justification.

- [ ] **Step 4: Run the FULL AC suite to ensure no regression.**

```
pixi run pytest tests/test_no_unredacted_writes.py -v
```

Expected: AC1-AC10 all GREEN.

- [ ] **Step 5: Pre-commit + commit.**

```
pixi run pre-commit run --files tests/test_no_unredacted_writes.py
git add tests/test_no_unredacted_writes.py
git commit -m "test(ast): AC10 — LoraSwapCell stack refs must be redacted"
```

---

## Task 9: Shared smoke harness helper — `write_lora_swap_grid_spec`

**Goal:** New keyword-only helper `write_lora_swap_grid_spec(*, tier, strengths, out_path)` in `tests/_smoke_harness/` covering Tier-3 single-LoRA-swap (Wan 2.1 1.3B Pokemon + static-rotation pair) and Tier-4 MoE-pair-swap (Wan 2.2 14B Arcane high+low). Used by live-fire tests + integration tests.

**Files:**
- Create: `tests/_smoke_harness/lora_swap_grid.py`
- Test: indirect via Tasks 10 + 11 (helper is a fixture-builder, not a unit under test).

**Acceptance Criteria:**
- [ ] `write_lora_swap_grid_spec(*, tier="tier3", strengths=[0.5,1.0,1.5], out_path=p)` writes a valid `GridSpec` YAML to `out_path`.
- [ ] `tier="tier4"` writes a Wan 2.2 14B Arcane high+low pair spec.
- [ ] The resulting YAML loads cleanly via `GridSpec.load(out_path)`.

**Steps:**

- [ ] **Step 1: Inspect existing harness helpers** for the Tier-3/Tier-4 LoRA references already used elsewhere — grep for "civitai" in `tests/_smoke_harness/` to find the canonical refs:

```
rg -n "civitai:" tests/_smoke_harness/ examples/configs/
```

- [ ] **Step 2: Create the helper.**

```python
# tests/_smoke_harness/lora_swap_grid.py
"""Build a grid YAML driving the lora_swap variant for Tier-3 or Tier-4.

Used by:
- tests/smoke/release_wan22/* (Tier-4 live-fire)
- tests/smoke/* (Tier-3 live-fire)
- tests/integration/grid/* (mocked end-to-end)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml

# Canonical refs sourced from existing examples/configs/wan{21-1_3b,22-14b}-strength-grid.yaml.
# Verify against those files when this helper is wired — refs are pinned.
_TIER3_BASE_CFG = "wan21-warm-reuse-smoke.yaml"
_TIER3_LORAS = [
    {"ref": "civitai:<TIER3_POKEMON_MODEL_ID>@<TIER3_POKEMON_VERSION_ID>", "branch": "auto"},
    {"ref": "civitai:<TIER3_STATIC_ROTATION_MODEL_ID>@<TIER3_STATIC_ROTATION_VERSION_ID>", "branch": "auto"},
]

_TIER4_BASE_CFG = "wan22-warm-reuse-smoke.yaml"
_TIER4_LORAS = [
    {"ref": "civitai:<TIER4_ARCANE_HIGH_MODEL_ID>@<TIER4_ARCANE_HIGH_VERSION_ID>", "branch": "high"},
    {"ref": "civitai:<TIER4_ARCANE_LOW_MODEL_ID>@<TIER4_ARCANE_LOW_VERSION_ID>", "branch": "low"},
]


def write_lora_swap_grid_spec(
    *,
    tier: Literal["tier3", "tier4"],
    strengths: list[float],
    out_path: Path,
    base_cfg_path: Path,
) -> None:
    """Write a grid YAML with N `lora_swap:` cells varying strength.

    Args:
        tier: tier3 (Wan 2.1 1.3B, single LoRA on auto branch) or tier4
            (Wan 2.2 14B, MoE pair on high+low branches).
        strengths: one cell per value; e.g. [0.5, 1.0, 1.5].
        out_path: where to write the YAML.
        base_cfg_path: absolute path to the base cfg outside the repo
            (lora_swap.config field). The caller is responsible for
            placing this cfg outside the kinoforge repo.
    """
    template_loras = _TIER3_LORAS if tier == "tier3" else _TIER4_LORAS
    title_prefix = "Wan 2.1 1.3B" if tier == "tier3" else "Wan 2.2 14B Arcane"
    budget_cap = 0.5 if tier == "tier3" else 2.0
    layout = f"1x{len(strengths)}"

    cells = []
    for s in strengths:
        stack = [
            {"ref": l["ref"], "strength": s, "branch": l["branch"]}
            for l in template_loras
        ]
        cells.append({
            "lora_swap": {"config": str(base_cfg_path), "stack": stack},
            "caption": f"strength={s}",
        })

    body = {
        "title": f"{title_prefix} strength sweep",
        "budget_cap_usd": budget_cap,
        "layout": layout,
        "on_swap_failure": "classify",
        "cells": cells,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(body))  # kinoforge:public-write
```

**Note:** The placeholders `<TIER3_POKEMON_MODEL_ID>` etc. MUST be replaced with the actual civitai IDs from the existing `examples/configs/wan{21-1_3b,22-14b}-strength-grid.yaml`. Implementer: grep those files for the real values and substitute. The placeholders only exist here because PROGRESS.md does not commit those specific civitai IDs and a future operator may need to verify them against the live cfgs.

- [ ] **Step 3: Substitute the placeholders** by reading the existing strength-grid cfgs:

```
rg -n "ref:|civitai:" examples/configs/wan21-1_3b-strength-grid.yaml examples/configs/wan22-14b-strength-grid.yaml
```

Replace each `<TIER…>` placeholder with the verbatim ref.

- [ ] **Step 4: Add a sanity test that the helper's output validates.**

```python
# tests/_smoke_harness/test_lora_swap_grid_helper.py
from __future__ import annotations

from pathlib import Path

from tests._smoke_harness.lora_swap_grid import write_lora_swap_grid_spec
from kinoforge.core.grid.spec import GridSpec


def test_tier3_helper_writes_loadable_spec(tmp_outside_repo):
    base = tmp_outside_repo / "base.yaml"
    base.write_text("version: 1\nprompt: x\nmode: t2v\n")
    out = tmp_outside_repo / "spec.yaml"
    write_lora_swap_grid_spec(
        tier="tier3", strengths=[0.5, 1.0, 1.5],
        out_path=out, base_cfg_path=base,
    )
    spec = GridSpec.load(out)
    assert len(spec.cells) == 3
    assert all(c.lora_swap is not None for c in spec.cells)
    assert [c.lora_swap.stack[0].strength for c in spec.cells] == [0.5, 1.0, 1.5]


def test_tier4_helper_writes_loadable_spec_with_high_low_branches(tmp_outside_repo):
    base = tmp_outside_repo / "base.yaml"
    base.write_text("version: 1\nprompt: x\nmode: t2v\n")
    out = tmp_outside_repo / "spec.yaml"
    write_lora_swap_grid_spec(
        tier="tier4", strengths=[1.0],
        out_path=out, base_cfg_path=base,
    )
    spec = GridSpec.load(out)
    branches = {entry.branch for entry in spec.cells[0].lora_swap.stack}
    assert branches == {"high", "low"}
```

- [ ] **Step 5: Run + commit.**

```
pixi run pytest tests/_smoke_harness/test_lora_swap_grid_helper.py -v
pixi run pre-commit run --files tests/_smoke_harness/lora_swap_grid.py tests/_smoke_harness/test_lora_swap_grid_helper.py
git add tests/_smoke_harness/lora_swap_grid.py tests/_smoke_harness/test_lora_swap_grid_helper.py
git commit -m "test(harness): write_lora_swap_grid_spec helper (tier3 + tier4)"
```

---

## Task 5: Executor — `_run_swap_group` + `run_grid` dispatch

**Goal:** New async `_run_swap_group` in `src/kinoforge/core/grid/executor.py` driving cell-1 cold-boot via `kinoforge generate --loras --emit-provision-record`, reading the record, then for cells 2..N invoking `kinoforge generate --attach-pod --loras`. Budget polling (30s cadence) via `CostSidecarBuilder`. try/finally group teardown via `kinoforge destroy` + `_check_no_residual_pods`. On subprocess failure invokes `_classify_swap_failure`. Outer `run_grid` dispatches by cell type.

**Files:**
- Modify: `src/kinoforge/core/grid/executor.py`
- Test: `tests/core/grid/test_executor_swap_group.py` (NEW, ~6 tests)

**Acceptance Criteria:**
- [ ] Cell-1 subprocess argv contains `--loras` + `--emit-provision-record`; NO `--attach-pod`; NO `--no-reuse`.
- [ ] Cells 2..N subprocess argv contains `--attach-pod <id>` + `--loras`; NO `--emit-provision-record`; NO `--no-reuse`.
- [ ] Group teardown runs `kinoforge destroy --id <pod_id>` in finally on group success.
- [ ] Group teardown runs `kinoforge destroy --id <pod_id>` in finally on group abort.
- [ ] Post-run `_check_no_residual_pods` probe runs; residual pod surfaces as `GridResult.status="teardown"` (exit code 5).
- [ ] Budget cap-trip aborts group with exit code 3; remaining cells marked `budget_killed` in sidecar.

**Verify:** `pixi run pytest tests/core/grid/test_executor_swap_group.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests** (~6, with `subprocess.run` monkeypatched).

```python
# tests/core/grid/test_executor_swap_group.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.grid.executor import _ResolvedCell, _run_swap_group


@dataclass
class _FakeSubprocResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _build_swap_cell(idx: int, tmp_path: Path, strength: float):
    cfg = tmp_path / f"cell_{idx}.yaml"
    cfg.write_text(f"version: 1\nprompt: x\nmode: t2v\n")
    return _ResolvedCell(
        idx=idx, caption=f"strength={strength}",
        cfg_path=cfg, effective_cfg=MagicMock(),
        mp4_path=None, is_lora_swap=True,
        lora_swap_stack=[MagicMock(ref="civitai:1@1", strength=strength, branch="auto")],
    )


@pytest.fixture
def fake_subprocess(tmp_path, monkeypatch):
    """Captures all subprocess.run calls; produces fake mp4 + provision record on cmd-2."""
    calls: list[list[str]] = []

    def _fake_run(cmd, capture_output=False, text=False, check=False):
        calls.append(cmd)
        if "--emit-provision-record" in cmd:
            i = cmd.index("--emit-provision-record")
            record_path = Path(cmd[i + 1])
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(json.dumps({
                "pod_id": "fake-pod-1",
                "endpoint_url": "https://fake.proxy.runpod.net",
                "provider": "runpod",
                "warm_attach_key": "deadbeef",
                "provision_ts": "2026-06-26T15:00:00",
            }))
        # Always produce a fake mp4 in the cell's --output-dir
        if "--output-dir" in cmd:
            j = cmd.index("--output-dir")
            out = Path(cmd[j + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "fake.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00")
        return _FakeSubprocResult()

    monkeypatch.setattr("kinoforge.core.grid.executor.subprocess.run", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_cell_1_subprocess_has_loras_and_emit_record_no_attach_no_no_reuse(
    tmp_path, fake_subprocess
):
    cells = [_build_swap_cell(0, tmp_path, 0.5)]
    await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=2.0, budget_state=MagicMock(),
        timeout_per_cell_s=600.0,
    )
    cmd1 = fake_subprocess[0]
    assert "--loras" in cmd1
    assert "--emit-provision-record" in cmd1
    assert "--attach-pod" not in cmd1
    assert "--no-reuse" not in cmd1


@pytest.mark.asyncio
async def test_cells_2_through_n_have_attach_pod_and_loras_no_emit(
    tmp_path, fake_subprocess
):
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0])]
    await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=2.0, budget_state=MagicMock(),
        timeout_per_cell_s=600.0,
    )
    cmd2 = fake_subprocess[1]
    assert "--attach-pod" in cmd2
    assert "fake-pod-1" in cmd2
    assert "--loras" in cmd2
    assert "--emit-provision-record" not in cmd2
    assert "--no-reuse" not in cmd2


@pytest.mark.asyncio
async def test_group_destroy_runs_in_finally_on_success(tmp_path, fake_subprocess):
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0])]
    await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=2.0, budget_state=MagicMock(),
        timeout_per_cell_s=600.0,
    )
    destroy_calls = [c for c in fake_subprocess if c[:4] == ["pixi","run","kinoforge","destroy"]]
    assert len(destroy_calls) == 1
    assert "fake-pod-1" in destroy_calls[0]


@pytest.mark.asyncio
async def test_group_destroy_runs_in_finally_on_abort(tmp_path, monkeypatch):
    """If cell-2 fails ABORT-classified, destroy still runs."""
    calls: list[list[str]] = []
    def _fake_run(cmd, **kw):
        calls.append(cmd)
        if "--emit-provision-record" in cmd:
            i = cmd.index("--emit-provision-record")
            Path(cmd[i+1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[i+1]).write_text(json.dumps({
                "pod_id": "fake-pod-1", "endpoint_url": "x", "provider": "runpod",
                "warm_attach_key": "d", "provision_ts": "t",
            }))
            if "--output-dir" in cmd:
                j = cmd.index("--output-dir")
                Path(cmd[j+1]).mkdir(parents=True, exist_ok=True)
                (Path(cmd[j+1]) / "x.mp4").write_bytes(b"\x00")
            return _FakeSubprocResult()
        if "--attach-pod" in cmd:
            return _FakeSubprocResult(returncode=1, stderr="VRAMRollbackFailure: ...")
        return _FakeSubprocResult()
    monkeypatch.setattr("kinoforge.core.grid.executor.subprocess.run", _fake_run)
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0, 1.5])]
    await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=2.0, budget_state=MagicMock(),
        timeout_per_cell_s=600.0,
    )
    destroy_calls = [c for c in calls if c[:4] == ["pixi","run","kinoforge","destroy"]]
    assert len(destroy_calls) == 1


@pytest.mark.asyncio
async def test_post_run_residual_probe_surfaces_teardown_status(
    tmp_path, fake_subprocess, monkeypatch
):
    # Stub _check_no_residual_pods to claim a residual pod exists.
    monkeypatch.setattr(
        "kinoforge.core.grid.executor._check_no_residual_pods",
        lambda *a, **kw: ["leaked-pod"],
    )
    cells = [_build_swap_cell(0, tmp_path, 0.5)]
    results = await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=2.0, budget_state=MagicMock(),
        timeout_per_cell_s=600.0,
    )
    # Implementation detail: teardown status is communicated back via results / breadcrumb
    assert any("teardown" in (r.teardown_breadcrumb or "") for r in results)


@pytest.mark.asyncio
async def test_budget_cap_trip_aborts_group_marks_remaining_killed(
    tmp_path, fake_subprocess
):
    # Configure budget_state to flip cap-tripped after cell-1
    state = MagicMock()
    state.is_over_cap = MagicMock(side_effect=[False, True, True])
    cells = [_build_swap_cell(i, tmp_path, s) for i, s in enumerate([0.5, 1.0, 1.5])]
    results = await _run_swap_group(
        cells, on_swap_failure="classify",
        output_dir=tmp_path / "out", grid_id="g1",
        budget_cap_usd=0.10, budget_state=state,
        timeout_per_cell_s=600.0,
    )
    assert results[0].status == "ok"
    assert results[1].status == "budget_killed"
    assert results[2].status == "budget_killed"
```

- [ ] **Step 2: Run tests; confirm RED.**

- [ ] **Step 3: Implement `_run_swap_group`** in `src/kinoforge/core/grid/executor.py`.

Add new helpers + the swap-group runner. Refer to the existing `_run_one_cell` (line 266) + `_run_group` (line 343) for the established patterns. Key snippets:

```python
from kinoforge.core.grid.swap_failures import (
    RETRY_BACKOFF_S,
    RETRY_MAX_ATTEMPTS,
    SwapFailureAction,
    _classify_swap_failure,
)


def _stack_to_loras_heredoc(stack: list[LoraStackEntry]) -> str:
    """Render a stack into the P3 --loras heredoc format."""
    if not stack:
        return ""
    lines = []
    for e in stack:
        parts = [e.ref, str(e.strength)]
        if e.branch != "auto":
            parts.append(e.branch)
        lines.append(" ".join(parts))
    return "\n".join(lines) + "\n"


def _build_swap_generate_cmd(
    cell: _ResolvedCell,
    *,
    grid_id: str,
    output_dir: Path,
    attach_pod_id: str | None,
    emit_provision_record: Path | None,
) -> list[str]:
    cmd = _build_generate_cmd(
        cell, grid_id=grid_id, output_dir=output_dir, no_reuse=False,
    )
    # cell's stack as --loras heredoc value
    heredoc = _stack_to_loras_heredoc(cell.lora_swap_stack or [])
    cmd += ["--loras", heredoc]
    if attach_pod_id is not None:
        cmd += ["--attach-pod", attach_pod_id]
    if emit_provision_record is not None:
        cmd += ["--emit-provision-record", str(emit_provision_record)]
    return cmd


async def _run_swap_cell_once(
    cell: _ResolvedCell,
    *,
    grid_id: str,
    output_dir: Path,
    attach_pod_id: str | None,
    emit_provision_record: Path | None,
) -> tuple[int, str, str]:
    cmd = _build_swap_generate_cmd(
        cell, grid_id=grid_id, output_dir=output_dir,
        attach_pod_id=attach_pod_id,
        emit_provision_record=emit_provision_record,
    )
    proc = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, check=False,  # noqa: S603
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


async def _run_swap_group(
    group_cells: list[_ResolvedCell],
    *,
    on_swap_failure: Literal["strict", "continue", "classify"],
    output_dir: Path,
    grid_id: str,
    budget_cap_usd: float,
    budget_state: Any,
    timeout_per_cell_s: float,
) -> list[GridCellResult]:
    """Cold-boot one pod for cell-1; attach for cells 2..N; destroy on exit."""
    results: list[GridCellResult] = []
    pod_id: str | None = None
    try:
        for i, cell in enumerate(group_cells):
            # Cap-trip BEFORE running this cell
            if budget_state.is_over_cap():
                results.append(GridCellResult(
                    idx=cell.idx, status="budget_killed",
                    error=f"budget cap ${budget_cap_usd:.2f} reached",
                ))
                continue

            cell_out = _cell_output_dir(grid_id, cell.idx, output_dir)
            cell_out.mkdir(parents=True, exist_ok=True)

            attempt = 0
            while True:
                attempt += 1
                if i == 0:
                    record_path = cell_out / ".provision.json"
                    rc, stdout, stderr = await _run_swap_cell_once(
                        cell, grid_id=grid_id, output_dir=output_dir,
                        attach_pod_id=None,
                        emit_provision_record=record_path,
                    )
                    if rc == 0:
                        pod_id = json.loads(record_path.read_text())["pod_id"]
                else:
                    if pod_id is None:
                        raise RuntimeError(
                            "swap group cell-1 produced no pod_id; can't attach for cell "
                            f"{cell.idx}"
                        )
                    rc, stdout, stderr = await _run_swap_cell_once(
                        cell, grid_id=grid_id, output_dir=output_dir,
                        attach_pod_id=pod_id,
                        emit_provision_record=None,
                    )
                if rc == 0:
                    mp4s = list(cell_out.glob("*.mp4"))
                    if not mp4s:
                        results.append(GridCellResult(
                            idx=cell.idx, status="failed",
                            error="subprocess exited 0 but no mp4 produced",
                        ))
                        break
                    results.append(GridCellResult(
                        idx=cell.idx, status="ok",
                        mp4_path=mp4s[0], sha256=_sha256_file(mp4s[0]),
                    ))
                    break

                action = _classify_swap_failure(stderr, rc, on_swap_failure)
                if action is SwapFailureAction.RETRY and attempt < RETRY_MAX_ATTEMPTS:
                    await asyncio.sleep(RETRY_BACKOFF_S)
                    continue
                if action is SwapFailureAction.CONTINUE:
                    results.append(GridCellResult(
                        idx=cell.idx, status="failed", error=stderr[-500:],
                    ))
                    break
                # ABORT
                results.append(GridCellResult(
                    idx=cell.idx, status="failed", error=stderr[-500:],
                ))
                # Fail remaining cells
                for later in group_cells[i + 1:]:
                    results.append(GridCellResult(
                        idx=later.idx, status="aborted",
                        error="group aborted by earlier unrecoverable failure",
                    ))
                return results
    finally:
        if pod_id is not None:
            # subprocess kinoforge destroy
            destroy_cmd = ["pixi", "run", "kinoforge", "destroy", "--id", pod_id]
            await asyncio.to_thread(
                subprocess.run, destroy_cmd, capture_output=True, text=True, check=False,  # noqa: S603
            )
            residual = _check_no_residual_pods(pod_id)
            if residual:
                for r in results:
                    r.teardown_breadcrumb = (
                        f"teardown residual pods: {','.join(residual)}"
                    )
    return results
```

`_check_no_residual_pods` already exists (line 397) — confirm signature and adapt the call.

- [ ] **Step 4: Extend `run_grid` dispatch** (line 458):

```python
    for group_key, cells in groups.items():
        if group_key == _PATH_GROUP_KEY:
            # existing path-cell handler (unchanged)
            ...
        elif all(c.is_lora_swap for c in cells):
            cell_results = await _run_swap_group(
                cells,
                on_swap_failure=spec.on_swap_failure,
                output_dir=output_dir, grid_id=grid_id,
                budget_cap_usd=spec.budget_cap_usd,
                budget_state=budget_state,
                timeout_per_cell_s=timeout_per_cell_s,
            )
        else:
            cell_results = await _run_group(cells, ...)  # existing
        all_results.extend(cell_results)
```

- [ ] **Step 5: Run tests; confirm GREEN.**

```
pixi run pytest tests/core/grid/test_executor_swap_group.py -v
pixi run pytest tests/core/grid/ -v  # full sanity, no regression
```

- [ ] **Step 6: Pre-commit + commit.**

```
pixi run pre-commit run --files src/kinoforge/core/grid/executor.py tests/core/grid/test_executor_swap_group.py
git add -- src/kinoforge/core/grid/executor.py tests/core/grid/test_executor_swap_group.py
git commit -m "feat(grid): _run_swap_group executor + run_grid dispatch"
```

---

## Task 12: Integration tests + README + PROGRESS update

**Goal:** End-to-end integration test of the swap-mode executor against stubbed kinoforge subprocesses + stubbed `/lora/set_stack`. README extended with `lora_swap:` doc + new flag docs. PROGRESS.md gets a CLOSED entry.

**Files:**
- Create: `tests/integration/grid/test_lora_swap_executor_integration.py` (~2 tests)
- Modify: `README.md` (extend `kinoforge grid` section)
- Modify: `PROGRESS.md` (append CLOSED entry under the active workstream)

**Acceptance Criteria:**
- [ ] 3-cell swap group integration test produces 3 sha-distinct mp4s.
- [ ] Sidecar JSON shape matches spec § 6 fields.
- [ ] README `kinoforge grid` section documents `lora_swap:` YAML shape + cost savings vs `generate:`-per-cell + the two new flags (`--attach-pod`, `--emit-provision-record`).
- [ ] PROGRESS.md has a CLOSED entry naming all commits in the workstream + final pre-fire / post-fire spend.

**Steps:**

- [ ] **Step 1: Integration test (uses harness helpers + fake-subprocess fixture).**

```python
# tests/integration/grid/test_lora_swap_executor_integration.py
# Pattern mirrors existing tests/integration/grid/ tests.
# Drives run_grid() against a 3-cell lora_swap spec; fake subprocesses
# produce distinct mp4 bytes per cell (varying a counter) so shas differ.
# Asserts: 3 GridCellResult with status=ok, all shas distinct;
# sidecar JSON loadable with the documented keys.
```

(Implementer should adapt the fixture pattern from `test_executor_swap_group.py` but at the `run_grid()` entry-point level.)

- [ ] **Step 2: README update.**

Add a `### `lora_swap:` cells — warm-attach LoRA-stack swaps` subsection under the existing `kinoforge grid` section. Cover: YAML shape; cost-savings rationale; `on_swap_failure` knob; `--attach-pod` flag; `--emit-provision-record` flag.

- [ ] **Step 3: PROGRESS.md update.**

Add a `**`kinoforge grid` lora_swap variant CLOSED 2026-MM-DD (N tasks shipped).**` entry near the top of the active workstream section, naming the spec, the plan, the commit trail, the live-fire spend, and the resume-target update (Layer 5 returns to top).

- [ ] **Step 4: Commit.**

```
git add -- tests/integration/grid/test_lora_swap_executor_integration.py README.md PROGRESS.md
git commit -m "docs+test: integration coverage + README + PROGRESS for grid lora_swap"
```

---

## Task 10: Tier-3 live fires (happy-path + forced-failure)

**Goal:** Two Tier-3 live fires against Wan 2.1 1.3B on RunPod. Cumulative cost ≤ $0.50.

**Files:**
- Live invocation; no source changes expected. Smoke-spec YAML written via the Task 9 helper.

**Acceptance Criteria:**
- [ ] Happy-path 3-cell strength sweep {0.5, 1.0, 1.5} on Pokemon + static-rotation: 3 sha-distinct mp4s; composed grid; sidecar.json with the documented schema; cumulative cost ≤ $0.30.
- [ ] Forced-failure run: invalid `branch: bogus` on cell-2 → `classify` action = CONTINUE; cell-2 status="failed" in sidecar; cell-3 proceeds; partial exit code 2.
- [ ] Post-fire `pixi run kinoforge list` returns "No running instances." (per `feedback_use_no_reuse_for_one_shots`-style verification).

**Verify:** Manual eyeball on the composed mp4 + `cat <out>.cost.json`.

**Steps:**

- [ ] **Step 1: Preflight.**

```
pixi run preflight
```

Expected: exit 0.

- [ ] **Step 2: Build Tier-3 happy-path spec via helper.**

```python
# scratch script, run inline
from pathlib import Path
from tests._smoke_harness.lora_swap_grid import write_lora_swap_grid_spec
write_lora_swap_grid_spec(
    tier="tier3", strengths=[0.5, 1.0, 1.5],
    out_path=Path("/tmp/grids/tier3-swap.yaml"),
    base_cfg_path=Path("/tmp/grids/wan21-base.yaml"),
)
```

Place `wan21-base.yaml` outside the repo (use the same base shape as `examples/configs/wan21-1_3b-strength-grid.yaml`, with the canonical Tier-3 prompt from `examples/configs/prompts/field-realistic.txt`).

- [ ] **Step 3: Run the live fire.**

```
KINOFORGE_LIVE_TESTS=1 pixi run kinoforge grid \
  --spec /tmp/grids/tier3-swap.yaml \
  --out /tmp/output/tier3-swap.mp4
```

Monitor with the polling pattern from `CLAUDE.md → Live smoke monitoring`. Expected wall ~6 min, cost ≤ $0.30.

- [ ] **Step 4: Verify happy-path artifacts.**

```
sha256sum /tmp/output/_grid_*/cell_{0,1,2}_out/*.mp4
cat /tmp/output/tier3-swap.cost.json | python -m json.tool
pixi run kinoforge list
```

Expected: 3 distinct shas; sidecar has 3 cells with `status="ok"`; ledger empty.

- [ ] **Step 5: Build forced-failure spec** with `branch: bogus` on cell-2 (hand-edit the helper output OR add a `branch_override` keyword to the helper for this test).

- [ ] **Step 6: Run the forced-failure fire.**

Expected: exit code 2 (partial); sidecar cell-2 has `status="failed"`, `error` matching `BranchUnknown`; cell-3 has `status="ok"`. Cost ≤ $0.20.

- [ ] **Step 7: Update PROGRESS** with Tier-3 results inline (do NOT commit successful-generations.md amendment yet — that's Task 11 for Tier-4).

---

## Task 11: Tier-4 live fire + `successful-generations.md` amend

**Goal:** Tier-4 happy-path 3-cell strength sweep on Wan 2.2 14B Arcane high+low pair. Single pod, 3 generations. Cumulative cost ≤ $1.50.

**Files:**
- Live invocation; modify `successful-generations.md` (§11 "See also" line under `kinoforge grid` axis).
- Modify: `PROGRESS.md` (live-fire results).

**Acceptance Criteria:**
- [ ] 3 sha-distinct mp4s; composed grid; sidecar.json correctly attributes cost to the single shared pod; cumulative cost ≤ $1.50.
- [ ] `pixi run kinoforge list` post-fire returns "No running instances." (no leak).
- [ ] `successful-generations.md` §11 has a new "See also" line: `(lora_swap, 3-cell strength sweep, Wan 2.2 14B Arcane high+low, sha=...,...,..., cost=$X.XX, single-pod-warm-attach)`.

**Verify:** Manual eyeball + sidecar + ledger probe (same as Task 10 Step 4).

**Steps:** mirror Task 10 with `tier="tier4"`, expected wall ~35 min, cost target ≤ $1.50.

---

## Self-Review

**1. Spec coverage:**
- § 1 spec surface → Task 1 ✓
- § 2 grouping → Task 2 ✓
- § 3 CLI surface (two flags) → Tasks 3 + 4 ✓
- § 4 executor → Task 5 ✓
- § 5 failure taxonomy → Task 6 ✓
- § 6 cost sidecar → Task 7 ✓
- § 7 testing surface (AST + harness) → Tasks 8 + 9 ✓
- § 8 live-fire plan → Tasks 10 + 11 ✓
- Acceptance criteria AC1-AC11 + AC-Live → spread across Tasks 1-12 ✓
- Integration test + README + PROGRESS → Task 12 ✓

**2. Placeholder scan:** Placeholders remain only in Task 9 for civitai IDs (`<TIER3_POKEMON_MODEL_ID>` etc.) with explicit instructions to substitute by reading existing `examples/configs/wan{21-1_3b,22-14b}-strength-grid.yaml`. This is the correct shape — the plan tells the implementer *where to look* for the live values without committing them inline (PROGRESS.md memory `feedback_never_print_secret_values` policy applies to credentials, not LoRA IDs, but treating IDs as cfg-sourced rather than plan-sourced is the cleaner pattern).

**3. Type consistency:** `WarmAttachKey(base_model=..., engine=..., precision=...).derive()` referenced consistently across Tasks 2, 3, 4, 5. `SwapFailureAction.RETRY|CONTINUE|ABORT` referenced consistently across Tasks 5, 6. `CostSidecarBuilder.start_group/record_cell/mark_cell_error/write` referenced consistently across Tasks 5, 7.

**4. Cross-task ordering:** Task 6 (swap_failures) listed BEFORE Task 3/4 in plan body because Task 5 imports from it. Task ID order in the existing TaskList is 1, 2, 3, 4, 5, 6, 7, ... — the dependency graph in TaskList already encodes #5 blocked-by #6, so the implementer working dependency-aware (executing-plans / subagent-driven-development) will process 6 before 5 regardless.

---

## Execution

Next step is calling `AskUserQuestion` for execution mode.
