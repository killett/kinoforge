# MiniMax-H3 LoRA Shared Seam — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give MiniMax-H3 a working LoRA path on a shared seam a third model can reuse, and make `loras:` against a server that cannot serve them fail loudly instead of silently.

**Architecture:** A new `servers/_lora.py` owns the mechanism (schemas, inventory, download, apply job, FastAPI router); per-model differences are data in a `LoraProfile` the server builds after its pipeline loads. Capability truth is mirrored between a client registry (`core/lora_profiles.py`, checked at config load) and the server's `/health`, locked by a parity test. The stack reaches a pod over the existing async `/lora/set_stack` contract after `/health` goes ready — never through the pod env.

**Tech Stack:** Python 3.13, pydantic v2, FastAPI, pytest, pixi; diffusers 0.40.0 + torch 2.6.0 on the pod (Modal H200).

**Spec:** `docs/superpowers/specs/2026-09-22-h3-lora-shared-seam-design.md`

## Global Constraints

- **Wan's LoRA machinery is not modified.** One exception, and it is forced: `LoraTarget` in `wan_t2v_server.py` is `extra="forbid"`, so it MUST gain the additive `target` field or a payload carrying `target` 422s on a Wan pod. No behaviour changes there — `_check_branch_legal` keeps reading `branch`.
- **Wire back-compat:** the client includes `target` in the `/lora/set_stack` payload **only when it is not None**. An already-running warm pod from an older image has no `target` field and would 422 on an always-present key.
- **A failed apply fails the run.** Generation must never proceed with a stack that did not load. This is the defect the whole plan exists to remove.
- **Refs are sensitive under vault mode.** Never log a ref; log counts. Never put a stack in the pod env.
- **Golden regeneration order:** `pixi run pre-commit run --all-files` FIRST, then `pixi run python tools/snapshot_launch_payloads.py`. Reversed, goldens bake against unformatted bytes and move twice.
- **Pinned constants** (probed 2026-09-22, do not re-derive, do not substitute the `_comfyui_` variants — diffusers cannot load those):
  - Turbo: `hf:lightx2v/Minimax-h3-Turbo:minimax_h3_fl2v_turbo_8step_v1.0_bf16.safetensors` — 1,383,677,808 B, 624 BF16 tensors, `__metadata__ = {"alpha": "8"}`, keys carry `.default.`
  - Style: `hf:DiffSynth-Studio/MiniMax-H3-LoRA-LineartAnime:model.safetensors` — 1,258,532,696 B, 518 **F32** tensors, no `__metadata__`, keys start `blocks.`
  - Fallback turbo if the fl2v distillation reads poorly on t2va: `hf:larryvrh/MiniMax-H3-Turbo-Lora:minimax_h3_turbo_4step.safetensors` — 779,849,872 B, 4-step, alpha-less.
- **Env payload budget:** 90 KB total rendered env per RunPod diffusers config (ceiling is ~101 KB).

**User decisions (already made):**
- "Shared seam, cold-boot surface first" — build the seam properly, mount the minimal surface.
- "HTTP apply after ready" — never the pod env.
- "Client registry + server declaration, parity-tested" — capability truth lives in both, locked by a test.
- "New module + H3 only; Wan untouched" — no registry/LRU extraction this increment.
- "Both LoRAs, one pod, two applies" — live proof covers the metadata-alpha and the fp32-restore branches.
- Approach A (profile **data**, not per-model subclasses) over a `LoraAdapter` Protocol.
- Spec approved 2026-09-22 (commit `a0ce295f`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/kinoforge/core/lora.py` | `LoraEntry` gains `target`; `branch` deprecated alias | 1 |
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` | `LoraTarget` gains `target` (additive only) | 1 |
| `tests/test_lora_schema_parity.py` | extended: `branch`→`target` resolves identically | 1 |
| `src/kinoforge/core/lora_profiles.py` | **new** — client registry keyed by server module | 2 |
| `src/kinoforge/validation/checks/loras.py` | **new** — config-load ERROR for unserveable stacks | 2 |
| `src/kinoforge/engines/diffusers/servers/_lora.py` | **new** — the shared seam: schemas, profile, inventory, download, apply, router | 3, 4 |
| `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py` | builds the H3 profile, mounts the router, declares `/health.lora` | 5 |
| `src/kinoforge/core/errors.py` | `LoraFormatUnsupportedError`, `LoraLoadFailedError` | 6 |
| `src/kinoforge/engines/diffusers/__init__.py` | maps the two new error bodies; threads `target` when set | 6 |
| `src/kinoforge/core/lora_apply.py` | **new** — resolve stack → specs → apply → or fail the run | 7 |
| `src/kinoforge/core/orchestrator.py` | one call before `yield session` | 7 |
| `src/kinoforge/_adapters.py` | delete `build_set_stack_request` | 8 |
| `examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml` | **new** — the live-proof config | 9 |
| `tests/providers/test_env_payload_ceiling.py` | **new** — 90 KB guard | 9 |
| `docs/breaking-changes.md`, `PROGRESS.md`, `successful-generations.md` | deprecation + debt + live evidence | 10, 11 |

---

### Task 1: `target` on the schema, `branch` deprecated

**Goal:** Both LoRA models carry `target`; `branch` still works and maps onto it; disagreement is refused.

**Files:**
- Modify: `src/kinoforge/core/lora.py:105-170`
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:866-897`
- Test: `tests/test_lora_schema_parity.py` (extend), `tests/test_lora_target_field.py` (create)

**Acceptance Criteria:**
- [ ] `LoraEntry(ref="x", branch="h").target == "high_noise"` and `.branch == "high_noise"`
- [ ] `LoraEntry(ref="x", branch="high_noise", target="low_noise")` raises `ValidationError`
- [ ] `LoraEntry(ref="x").target is None`
- [ ] `LoraTarget` accepts `target` and resolves the alias identically to `LoraEntry`
- [ ] One WARNING per model instance that used `branch`, naming a count, never a ref
- [ ] Wan behaviour unchanged: `_check_branch_legal` still reads `branch`

**Verify:** `pixi run python -m pytest tests/test_lora_target_field.py tests/test_lora_schema_parity.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_lora_target_field.py
"""Behavior: `target` generalises `branch`, and the two cannot disagree.

A misrouted H3 LoRA loads successfully and degrades output silently (the two
partitions share module names), so the routing token has to be unambiguous at
the schema edge rather than at the card.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry


def test_branch_alias_populates_target() -> None:
    """`branch="h"` yields canonical branch AND the matching target."""
    entry = LoraEntry(ref="civitai:1@2", branch="h")
    assert entry.branch == "high_noise"
    assert entry.target == "high_noise"


def test_target_defaults_to_none_when_unspecified() -> None:
    """An undeclared target stays None so the profile's default resolves it."""
    assert LoraEntry(ref="civitai:1@2").target is None


def test_disagreeing_branch_and_target_is_refused() -> None:
    """Both set and disagreeing raises — no precedence rule silently drops one."""
    with pytest.raises(ValidationError, match="branch.*target.*disagree"):
        LoraEntry(ref="civitai:1@2", branch="high_noise", target="low_noise")


def test_agreeing_branch_and_target_is_accepted() -> None:
    """The redundant-but-consistent case is legal — it is not a conflict."""
    entry = LoraEntry(ref="civitai:1@2", branch="low_noise", target="low_noise")
    assert entry.target == "low_noise"


def test_h3_target_needs_no_branch() -> None:
    """An H3 routing token is expressible without touching Wan's vocabulary."""
    entry = LoraEntry(ref="hf:o/r:f.safetensors", target="transformer_ref")
    assert entry.target == "transformer_ref"
    assert entry.branch == "auto"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/test_lora_target_field.py -v`
Expected: FAIL — `LoraEntry` has `extra="forbid"`, so `target=` raises "Extra inputs are not permitted".

- [ ] **Step 3: Implement on `LoraEntry`**

Add the field after `branch` in `src/kinoforge/core/lora.py`, plus a model validator:

```python
    target: str | None = Field(default=None)

    @model_validator(mode="after")
    def _resolve_branch_to_target(self) -> "LoraEntry":
        """Map the deprecated `branch` onto `target`; refuse disagreement.

        `branch` is Wan's MoE vocabulary and cannot name H3's workflow
        partitions. `target` is resolved against the server profile's
        vocabulary instead. `None` means "the profile's default target",
        which reproduces `branch="auto"` exactly.
        """
        implied = None if self.branch == "auto" else self.branch
        if implied is not None and self.target is not None and implied != self.target:
            raise ValueError(
                f"branch and target disagree: branch={self.branch!r} implies "
                f"target={implied!r}, but target={self.target!r} was set; "
                f"set only `target` (branch is deprecated)"
            )
        if implied is not None and self.target is None:
            object.__setattr__(self, "target", implied)
            logger.warning(
                "deprecated-lora-branch: 1 entry used `branch`; it is mapped to "
                "`target`. Use `target:` — see docs/breaking-changes.md"
            )
        return self
```

Import `model_validator` from pydantic alongside the existing imports.

- [ ] **Step 4: Mirror onto `LoraTarget`**

Apply the identical field + validator to `LoraTarget` in `wan_t2v_server.py`, keeping the mirror comment that already says DO NOT diverge. The server module has no `kinoforge.core` import — copy the logic, do not import it. Use `logging.getLogger(...)` already present in that module. **Additive only:** do not touch `_check_branch_legal`, `_detect_moe_arity`, or any handler — Wan keeps routing on `branch`.

- [ ] **Step 5: Extend the parity test**

```python
# append to tests/test_lora_schema_parity.py
@pytest.mark.parametrize(
    ("kwargs", "expected_target"),
    [
        ({"branch": "h"}, "high_noise"),
        ({"branch": "l"}, "low_noise"),
        ({"branch": "auto"}, None),
        ({"target": "transformer_ref"}, "transformer_ref"),
        ({}, None),
    ],
)
def test_core_and_server_resolve_target_identically(
    kwargs: dict[str, str], expected_target: str | None
) -> None:
    """Both schemas resolve routing identically.

    Catches the alias being taught to one side only, which produces a cfg
    value the pod rejects at 422 after the card is already booked.
    """
    from kinoforge.core.lora import LoraEntry
    from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget

    assert LoraEntry(ref="civitai:1@2", **kwargs).target == expected_target
    assert LoraTarget(ref="civitai:1@2", **kwargs).target == expected_target
```

- [ ] **Step 6: Run both test files**

Run: `pixi run python -m pytest tests/test_lora_target_field.py tests/test_lora_schema_parity.py -v`
Expected: PASS

- [ ] **Step 7: Run the LoRA regression surface**

Run: `pixi run python -m pytest tests/ -k "lora" -q`
Expected: PASS — no existing LoRA test regresses on the additive field.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/core/lora.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
git add tests/test_lora_target_field.py tests/test_lora_schema_parity.py
git commit -m "feat(lora): generalise branch to target across both schemas"
```

---

### Task 2: Client capability registry + config-load check

**Goal:** A `loras:` stack against a server that cannot serve it, or naming a target that model does not have, is an ERROR before any pod is created.

**Files:**
- Create: `src/kinoforge/core/lora_profiles.py`
- Create: `src/kinoforge/validation/checks/loras.py`
- Test: `tests/core/test_lora_profiles.py`, `tests/validation/test_lora_check.py`

**Acceptance Criteria:**
- [ ] `client_profile_for_server_module("…wan_t2v_server")` reports supported with targets `("high_noise", "low_noise")`
- [ ] `client_profile_for_server_module("…minimax_h3_server")` reports supported with targets `("transformer", "transformer_ref")`
- [ ] An unregistered module reports unsupported
- [ ] Cfg with `loras:` + an unregistered `server_cmd` module → `Severity.ERROR`, message naming the module
- [ ] Cfg with `target: "high_noise"` on the H3 server module → `Severity.ERROR` naming H3's legal targets
- [ ] Cfg with `loras: []` → check does not apply

**Verify:** `pixi run python -m pytest tests/core/test_lora_profiles.py tests/validation/test_lora_check.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_lora_profiles.py
"""Behavior: the client registry knows which server modules serve LoRAs.

The universe is per model family. A globally-keyed vocabulary would let a Wan
token pass config load and fail only after a 45-minute H200 boot.
"""

from __future__ import annotations

from kinoforge.core.lora_profiles import client_profile_for_server_module

_WAN = "kinoforge.engines.diffusers.servers.wan_t2v_server"
_H3 = "kinoforge.engines.diffusers.servers.minimax_h3_server"


def test_h3_module_is_supported_with_partition_targets() -> None:
    """H3's universe is its two checkpoint partitions, not Wan's noise split."""
    profile = client_profile_for_server_module(_H3)
    assert profile is not None
    assert profile.supported is True
    assert profile.target_universe == ("transformer", "transformer_ref")


def test_wan_module_is_supported_with_moe_targets() -> None:
    """Wan's universe stays the MoE noise split."""
    profile = client_profile_for_server_module(_WAN)
    assert profile is not None
    assert profile.target_universe == ("high_noise", "low_noise")


def test_unregistered_module_returns_none() -> None:
    """An unknown server module is not silently assumed to serve LoRAs.

    Catches a future fifth server inheriting today's silent no-op by omission.
    """
    assert client_profile_for_server_module("pkg.mod.some_new_server") is None
```

```python
# tests/validation/test_lora_check.py
"""Behavior: an unserveable LoRA stack is refused at config load.

Each case here is money: the failure it prevents otherwise surfaces after an
H200 has booked, or not at all (a video generated without the LoRA).
"""

from __future__ import annotations

from kinoforge.core.config import Config
from kinoforge.validation.checks.loras import LoraServerSupportCheck
from kinoforge.validation.protocol import Severity


def _cfg(server_module: str, loras: list[dict[str, str]]) -> Config:
    return Config.model_validate(
        {
            "mode": "t2v",
            "engine": {
                "kind": "diffusers",
                "diffusers": {
                    "server_cmd": ["python", "-m", server_module],
                    "base_url": "http://localhost:8000",
                },
            },
            "loras": loras,
            "spec": {"model": "m", "pipeline": "P", "scheduler": "S"},
        }
    )


def test_unregistered_server_with_loras_is_an_error() -> None:
    """A stack aimed at a server with no LoRA surface fails before spend."""
    cfg = _cfg("pkg.mod.some_new_server", [{"ref": "civitai:1@2"}])
    check = LoraServerSupportCheck()
    assert check.applies_to(cfg) is True
    result = check.run(cfg)
    assert result.passed is False
    assert result.severity is Severity.ERROR
    assert "some_new_server" in result.message


def test_wan_target_on_h3_server_is_an_error() -> None:
    """Wan's vocabulary on H3 is refused, and the message names H3's targets."""
    cfg = _cfg(
        "kinoforge.engines.diffusers.servers.minimax_h3_server",
        [{"ref": "hf:o/r:f.safetensors", "target": "high_noise"}],
    )
    result = LoraServerSupportCheck().run(cfg)
    assert result.passed is False
    assert "transformer" in result.message


def test_valid_h3_stack_passes() -> None:
    """A legal H3 stack is not obstructed."""
    cfg = _cfg(
        "kinoforge.engines.diffusers.servers.minimax_h3_server",
        [{"ref": "hf:o/r:f.safetensors", "target": "transformer"}],
    )
    assert LoraServerSupportCheck().run(cfg).passed is True


def test_empty_stack_does_not_apply() -> None:
    """No LoRAs, no opinion — the check must not fire on ordinary configs."""
    cfg = _cfg("pkg.mod.some_new_server", [])
    assert LoraServerSupportCheck().applies_to(cfg) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/core/test_lora_profiles.py tests/validation/test_lora_check.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.core.lora_profiles`

- [ ] **Step 3: Implement the registry**

```python
# src/kinoforge/core/lora_profiles.py
"""Client-side knowledge of which diffusers server modules serve LoRAs.

Keyed by the dotted module named in ``engine.diffusers.server_cmd`` — the same
signal the provision renderer keys off.

This registry answers only what a controller can know WITHOUT a pod: does this
server serve LoRAs, and what is the target universe for its model family. It
cannot know which checkpoint partitions a given pod actually loaded, so the pod
narrows further at apply time. Config load catches wrong family / no support /
typo; the pod catches not-loaded-in-this-workflow.

Mirrored by each server's ``/health.lora`` block and locked by
``tests/engines/diffusers/test_lora_profile_parity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClientLoraProfile:
    """What the controller knows about one server module's LoRA support."""

    supported: bool
    target_universe: tuple[str, ...]


_REGISTRY: dict[str, ClientLoraProfile] = {
    "kinoforge.engines.diffusers.servers.wan_t2v_server": ClientLoraProfile(
        supported=True, target_universe=("high_noise", "low_noise")
    ),
    "kinoforge.engines.diffusers.servers.minimax_h3_server": ClientLoraProfile(
        supported=True, target_universe=("transformer", "transformer_ref")
    ),
}


def client_profile_for_server_module(module: str) -> ClientLoraProfile | None:
    """Return the profile for *module*, or ``None`` when unregistered.

    Args:
        module: Dotted server module path from ``server_cmd``.

    Returns:
        The registered :class:`ClientLoraProfile`, else ``None``.
    """
    return _REGISTRY.get(module)


def server_module_from_cfg(cfg: object) -> str | None:
    """Extract the dotted server module from a cfg's ``server_cmd``.

    Recognises the ``python -m <module>`` shape every shipped diffusers
    config uses.

    Args:
        cfg: A loaded ``Config``.

    Returns:
        The dotted module, or ``None`` when the argv is not ``-m``-shaped.
    """
    engine = getattr(cfg, "engine", None)
    diffusers = getattr(engine, "diffusers", None) if engine is not None else None
    argv = list(getattr(diffusers, "server_cmd", []) or [])
    if "-m" in argv:
        idx = argv.index("-m")
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None
```

- [ ] **Step 4: Implement the check**

Follow `validation/checks/upscale.py` exactly for structure (`name`, `category`, `severity`, `applies_to`, `run`, `auto_fix`, `register(...)` at module scope):

```python
# src/kinoforge/validation/checks/loras.py  (body of `run`)
    def run(self, cfg: Config) -> CheckResult:
        module = server_module_from_cfg(cfg)
        profile = (
            client_profile_for_server_module(module) if module is not None else None
        )
        if profile is None or not profile.supported:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"cfg declares {len(cfg.loras)} LoRA(s) but server module "
                    f"{module!r} is not known to serve them — the stack would be "
                    f"silently ignored on the pod"
                ),
                fix_suggestion=(
                    "remove the `loras:` block, or point `server_cmd` at a server "
                    "with LoRA support (see kinoforge.core.lora_profiles)"
                ),
            )
        illegal = sorted(
            {
                lo.target
                for lo in cfg.loras
                if lo.target is not None and lo.target not in profile.target_universe
            }
        )
        if illegal:
            return CheckResult(
                name=self.name,
                passed=False,
                severity=Severity.ERROR,
                message=(
                    f"LoRA target(s) {illegal} are not in this model's vocabulary; "
                    f"legal targets: {list(profile.target_universe)}"
                ),
                fix_suggestion=(
                    f"set target to one of {list(profile.target_universe)}, or omit "
                    f"it to use the pod's default partition"
                ),
            )
        return CheckResult(
            name=self.name, passed=True, severity=self.severity, message="ok"
        )
```

`applies_to` returns `bool(getattr(cfg, "loras", []))`. `auto_fix` returns `None` — the operator chooses.

Register the check module wherever `validation/checks/__init__.py` imports the others; match the existing import style exactly.

- [ ] **Step 5: Run the tests**

Run: `pixi run python -m pytest tests/core/test_lora_profiles.py tests/validation/test_lora_check.py -v`
Expected: PASS

- [ ] **Step 6: Confirm no shipped config regressed**

Run: `pixi run python -m pytest tests/ -k "validation or doctor" -q`
Expected: PASS — the shipped Wan LoRA configs still validate.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/lora_profiles.py src/kinoforge/validation/checks/loras.py
git add src/kinoforge/validation/checks/__init__.py
git add tests/core/test_lora_profiles.py tests/validation/test_lora_check.py
git commit -m "feat(lora): refuse an unserveable LoRA stack at config load"
```

---

### Task 3: The shared seam — profile, inventory, apply

**Goal:** `servers/_lora.py` owns the LoRA mechanism as pure functions over a `LoraProfile`, with rollback and replace correct, and no FastAPI yet.

**Files:**
- Create: `src/kinoforge/engines/diffusers/servers/_lora.py`
- Test: `tests/engines/diffusers/servers/test_lora_apply_core.py`

**Acceptance Criteria:**
- [ ] `apply_stack` loads each entry in order with adapter names `lora_0`, `lora_1`, …
- [ ] Strengths reach `set_adapters(names, weights)` on the module the profile names
- [ ] `after_load` runs exactly once, after every entry has loaded
- [ ] A failure on entry *k* leaves the inventory EMPTY and re-raises
- [ ] Applying [A] then [B] leaves exactly [B] active; both files remain on disk
- [ ] An entry with `target=None` resolves to `profile.default_target`; a `None` default raises `TargetRequired` naming the legal targets
- [ ] `explain_load_failure` output is carried on the raised `LoraLoadError`

**Verify:** `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_apply_core.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/engines/diffusers/servers/test_lora_apply_core.py
"""Behavior: the shared apply sequence — ordering, rollback, replace.

These are the invariants a per-model reimplementation gets wrong. A half-applied
stack that still reports the full inventory is the worst outcome available: the
controller believes a LoRA is active that is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.engines.diffusers.servers import _lora


class FakeModule:
    """Stands in for a transformer; records set_adapters calls."""

    def __init__(self) -> None:
        self.adapter_calls: list[tuple[list[str], list[float]]] = []
        self.dtype_calls: list[Any] = []

    def set_adapters(self, names: list[str], weights: list[float]) -> None:
        self.adapter_calls.append((list(names), list(weights)))

    def to(self, dtype: Any) -> "FakeModule":
        self.dtype_calls.append(dtype)
        return self


class FakePipe:
    """Records load/unload ordering the way the real mixin would see it."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.module = FakeModule()
        self.loaded: list[tuple[str, str, str]] = []
        self.unload_count = 0
        self._fail_on = fail_on

    def unload_lora_weights(self) -> None:
        self.unload_count += 1
        self.loaded.clear()

    def load(self, path: str, adapter_name: str, target: str) -> None:
        if self._fail_on is not None and self._fail_on in path:
            raise RuntimeError("size mismatch for blocks.0.attn.out_proj.lora_B")
        self.loaded.append((path, adapter_name, target))


def _profile(pipe: FakePipe, *, after_load: list[str] | None = None) -> _lora.LoraProfile:
    calls = after_load if after_load is not None else []
    return _lora.LoraProfile(
        name="fake",
        targets=("transformer",),
        default_target="transformer",
        load=lambda p, path, adapter_name, target: p.load(path, adapter_name, target),
        module_for=lambda p, target: p.module,
        after_load=lambda p: calls.append("after_load"),
        explain_load_failure=lambda exc: (
            "pruned-checkpoint hint" if "size mismatch" in str(exc) else None
        ),
    )


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    paths = {}
    for name in ("a.safetensors", "b.safetensors"):
        p = tmp_path / name
        p.write_bytes(b"x" * 16)
        paths[name] = p
    return paths


def test_apply_loads_in_order_with_positional_adapter_names(
    files: dict[str, Path],
) -> None:
    """Order is the activation order; names are positional and stable."""
    pipe = FakePipe()
    inv = _lora.apply_stack(
        pipe,
        _profile(pipe),
        entries=[
            _lora.ResolvedEntry(ref="r:a", path=files["a.safetensors"], strength=0.8, target=None),
            _lora.ResolvedEntry(ref="r:b", path=files["b.safetensors"], strength=0.4, target=None),
        ],
    )
    assert [n for _p, n, _t in pipe.loaded] == ["lora_0", "lora_1"]
    assert pipe.module.adapter_calls == [(["lora_0", "lora_1"], [0.8, 0.4])]
    assert [e.ref for e in inv] == ["r:a", "r:b"]


def test_after_load_runs_once_after_every_entry(files: dict[str, Path]) -> None:
    """The dtype restore must not run per-entry — it is a whole-model fixup."""
    pipe = FakePipe()
    calls: list[str] = []
    _lora.apply_stack(
        pipe,
        _profile(pipe, after_load=calls),
        entries=[
            _lora.ResolvedEntry(ref="r:a", path=files["a.safetensors"], strength=1.0, target=None),
            _lora.ResolvedEntry(ref="r:b", path=files["b.safetensors"], strength=1.0, target=None),
        ],
    )
    assert calls == ["after_load"]


def test_failure_midway_rolls_back_to_empty(files: dict[str, Path]) -> None:
    """Entry 2 of 2 fails -> nothing stays loaded, and the error carries the hint.

    Catches a mid-loop break that leaves adapter lora_0 live while the caller
    believes the whole stack applied.
    """
    pipe = FakePipe(fail_on="b.safetensors")
    with pytest.raises(_lora.LoraLoadError) as excinfo:
        _lora.apply_stack(
            pipe,
            _profile(pipe),
            entries=[
                _lora.ResolvedEntry(ref="r:a", path=files["a.safetensors"], strength=1.0, target=None),
                _lora.ResolvedEntry(ref="r:b", path=files["b.safetensors"], strength=1.0, target=None),
            ],
        )
    assert excinfo.value.hint == "pruned-checkpoint hint"
    assert excinfo.value.ref == "r:b"
    assert pipe.loaded == []
    assert pipe.unload_count == 2  # once at entry, once rolling back


def test_second_apply_replaces_rather_than_accumulates(files: dict[str, Path]) -> None:
    """Applying [A] then [B] leaves exactly [B].

    Catches a skipped unload, where A and B blend silently while the inventory
    still reads [B].
    """
    pipe = FakePipe()
    profile = _profile(pipe)
    _lora.apply_stack(
        pipe, profile,
        entries=[_lora.ResolvedEntry(ref="r:a", path=files["a.safetensors"], strength=1.0, target=None)],
    )
    inv = _lora.apply_stack(
        pipe, profile,
        entries=[_lora.ResolvedEntry(ref="r:b", path=files["b.safetensors"], strength=1.0, target=None)],
    )
    assert [e.ref for e in inv] == ["r:b"]
    assert [p for p, _n, _t in pipe.loaded] == [str(files["b.safetensors"])]
    assert files["a.safetensors"].exists()


def test_missing_default_target_is_refused_with_legal_values() -> None:
    """A two-partition profile with no default must not guess.

    This is diffusers' documented H3 hazard: the wrong partition loads fine and
    degrades output with no error anywhere.
    """
    pipe = FakePipe()
    profile = _lora.LoraProfile(
        name="dual",
        targets=("transformer", "transformer_ref"),
        default_target=None,
        load=lambda p, path, adapter_name, target: None,
        module_for=lambda p, target: p.module,
        after_load=lambda p: None,
        explain_load_failure=lambda exc: None,
    )
    with pytest.raises(_lora.TargetRequired, match="transformer_ref"):
        _lora.apply_stack(
            pipe, profile,
            entries=[_lora.ResolvedEntry(ref="r:a", path=Path("/tmp/x"), strength=1.0, target=None)],
        )
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_apply_core.py -v`
Expected: FAIL — `ImportError: cannot import name '_lora'`

- [ ] **Step 3: Implement the module**

`src/kinoforge/engines/diffusers/servers/_lora.py` — module docstring explaining it is the shared seam, the pod has no `kinoforge.core` import available, and that per-model behaviour is data. Contents, in order:

```python
@dataclass(frozen=True)
class LoraProfile:
    """Per-model LoRA behaviour, as data. Built after the pipeline loads."""

    name: str
    targets: tuple[str, ...]
    default_target: str | None
    load: Callable[[Any, str, str, str], None]
    module_for: Callable[[Any, str], Any]
    after_load: Callable[[Any], None]
    explain_load_failure: Callable[[BaseException], str | None]


@dataclass(frozen=True)
class ResolvedEntry:
    """One stack entry whose bytes are already on disk."""

    ref: str
    path: Path
    strength: float
    target: str | None


@dataclass(frozen=True)
class InventoryEntry:
    """One row of the pod's LoRA inventory (wire shape lives in the router)."""

    ref: str
    filename: str
    size_bytes: int
    adapter_name: str
    strength: float
    target: str


class TargetRequired(Exception):
    """Raised when an entry declares no target and the profile has no default."""


class LoraLoadError(Exception):
    """A load failed. Carries the offending ref and the profile's hint."""

    def __init__(self, ref: str, hint: str | None, underlying: BaseException) -> None:
        super().__init__(f"loading {ref} failed: {underlying}")
        self.ref = ref
        self.hint = hint
        self.underlying = underlying
```

`apply_stack(pipe, profile, *, entries)`:

1. `pipe.unload_lora_weights()`.
2. For each `i, entry`: resolve `target = entry.target or profile.default_target`; raise `TargetRequired(f"entry {entry.ref} declares no target and profile {profile.name} has no default; legal targets: {list(profile.targets)}")` when still `None`; call `profile.load(pipe, str(entry.path), f"lora_{i}", target)` inside `try`; on `BaseException as exc` call `pipe.unload_lora_weights()`, clear the module-level inventory, and `raise LoraLoadError(entry.ref, profile.explain_load_failure(exc), exc) from exc`.
3. `profile.after_load(pipe)`.
4. Group `(adapter_name, strength)` by resolved target; for each, `profile.module_for(pipe, target).set_adapters(names, weights)`.
5. Replace the module-level inventory with the new rows and return them.

Also implement, in the same module:

- `download_one(spec, dest_dir) -> tuple[Path, int]` — lift the body of `wan_t2v_server._download_one` (`wan_t2v_server.py:1035-1080`) verbatim including the `kinoforge-pod-download/0.1` User-Agent and the 600 s timeout; keep the `.partial` rename discipline. Return `Path`, not `str`.
- `ensure_downloaded(ref, spec, loras_dir)` — return the existing path when the file is already there, else download.
- `inventory_snapshot()` / `disk_free_bytes(path)`.

- [ ] **Step 4: Run the tests**

Run: `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_apply_core.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/_lora.py
git add tests/engines/diffusers/servers/test_lora_apply_core.py
git commit -m "feat(lora): add the shared apply seam with rollback and replace"
```

---

### Task 4: The router — endpoints, async job, error bodies

**Goal:** The three endpoints exist behind `build_lora_router`, matching the wire contract `DiffusersBackend` already speaks.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/_lora.py`
- Test: `tests/engines/diffusers/servers/test_lora_router.py`

**Acceptance Criteria:**
- [ ] `POST /lora/set_stack` returns `202`-style `{"job_id": …}` and runs the work in the background
- [ ] `GET /lora/set_stack/status/{job_id}` walks `queued` → `running` → `done`, with `inventory` and `free_bytes` on `done`
- [ ] An unknown `job_id` is a 404
- [ ] A `LoraLoadError` carrying a hint ends the job with `{"status": 400, "error": "lora_format_unsupported", "hint": …, "ref": …}`
- [ ] A `LoraLoadError` with no hint ends with `{"status": 500, "error": "lora_load_failed", …}`
- [ ] A download failure ends with `{"status": 502, "error": "lora_download_failed", "download_failed": ref}`
- [ ] `GET /lora/inventory` returns `{"inventory": [...], "free_bytes": int}`
- [ ] A request naming a target outside `profile.targets` is a 400 **before** any download

**Verify:** `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_router.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Drive a real `FastAPI()` app with `TestClient`, mounting `build_lora_router` over a fake pipe + profile (reuse `FakePipe`/`_profile` from Task 3 by importing them from the sibling test module). Cover each acceptance criterion; the two that matter most:

```python
def test_format_failure_surfaces_the_hint_at_400(client: Any, ...) -> None:
    """A pruned-checkpoint LoRA ends the job non-retryably with the hint.

    Catches it falling into the generic 502 download path, where the proxy
    retry budget burns three times on a file that can never load.
    """
    resp = client.post("/lora/set_stack", json={"target": [...], "download_specs": {...}})
    job = _poll(client, resp.json()["job_id"])
    assert job["state"] == "error"
    assert job["error"]["status"] == 400
    assert job["error"]["error"] == "lora_format_unsupported"
    assert "lightx2v" in job["error"]["hint"]


def test_illegal_target_is_rejected_before_download(client: Any, downloads: list) -> None:
    """Target legality is checked on the synchronous submit path.

    Catches a legality gate that runs inside the job, after 1.4 GB has already
    come down the wire.
    """
    resp = client.post(
        "/lora/set_stack",
        json={"target": [{"ref": "r:a", "target": "high_noise"}], "download_specs": {}},
    )
    assert resp.status_code == 400
    assert downloads == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_router.py -v`
Expected: FAIL — `AttributeError: module … has no attribute 'build_lora_router'`

- [ ] **Step 3: Implement the router**

Add to `_lora.py`:

- `LoraTarget` (pydantic, `extra="forbid"`: `ref`, `strength` default 1.0 bounded −2.0…2.0, `target: str | None = None`, `branch` accepted and mapped exactly as Task 1 does — copy that validator so the pod refuses a disagreement too), `SetStackRequest` (`target: list[LoraTarget]`, `download_specs: dict[str, ArtifactDownloadSpec]`), `ArtifactDownloadSpec` (`url`, `headers`, `filename`, `size_hint`), `LoraInventoryEntryModel`, `InventoryResponse`. Field names must match `wan_t2v_server`'s exactly — the client is shared.
- `build_lora_router(get_pipe, get_profile, loras_dir) -> APIRouter` with a module-level `asyncio.Lock`, a `_jobs: dict[str, dict]` record, and the three routes. Submit-path validation: every `t.target` that is not `None` must be in `profile.targets`, else `HTTPException(400, {"error": "lora_target_unsupported", "target": t.target, "legal": list(profile.targets)})`.
- `_run_apply_job(job_id, req)`: `state="running"` → download each missing ref via `ensure_downloaded` (a failure ends the job `502 lora_download_failed` with `download_failed=<ref>`, `evict_completed=[]`) → `apply_stack` in a thread (`await asyncio.to_thread(...)` — the load is sync and blocking, and a blocked event loop makes `/health` hang and the provider proxy 502, per the standing memory) → on `LoraLoadError`, end with 400 + hint when `hint` is set, else 500 `lora_load_failed` → on success set `state="done"`, `inventory`, `free_bytes`.

- [ ] **Step 4: Run the tests**

Run: `pixi run python -m pytest tests/engines/diffusers/servers/test_lora_router.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/_lora.py
git add tests/engines/diffusers/servers/test_lora_router.py
git commit -m "feat(lora): serve the set_stack contract from the shared router"
```

---

### Task 5: Mount it in the H3 server

**Goal:** The H3 server builds its profile from the loaded pipeline, mounts the router, and declares its LoRA capability in `/health`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py` (`_startup`, `health`, new `_build_lora_profile`)
- Modify: `tests/engines/diffusers/servers/h3_stub_pipe.py` (stub gains `transformer` / `transformer_ref` / `load_lora_weights` / `unload_lora_weights`)
- Test: `tests/engines/diffusers/test_minimax_h3_server.py` (extend), `tests/engines/diffusers/test_lora_profile_parity.py` (create)

**Acceptance Criteria:**
- [ ] `/health` reports `lora.targets == ["transformer"]` when the stub holds only `transformer`
- [ ] `/health` reports both targets, and `default_target is None`, when the stub holds both
- [ ] `lora.profile == "minimax-h3-t2va"`
- [ ] The profile's `load` passes `load_into_transformer_ref=True` only for `target == "transformer_ref"`
- [ ] `after_load` calls `.to(torch.bfloat16)` on the loaded partition
- [ ] `explain_load_failure` returns the pruned-checkpoint hint for a size-mismatch error and `None` otherwise
- [ ] Parity: every module in `core.lora_profiles._REGISTRY` declares a matching universe

**Verify:** `pixi run python -m pytest tests/engines/diffusers/test_minimax_h3_server.py tests/engines/diffusers/test_lora_profile_parity.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/engines/diffusers/test_lora_profile_parity.py
"""Behavior: client registry and server declaration agree.

The dangerous direction is a client MORE permissive than the pod: config load
passes, the H200 boots, and the apply refuses.
"""

from __future__ import annotations

import importlib

import pytest

from kinoforge.core.lora_profiles import _REGISTRY


@pytest.mark.parametrize("module_path", sorted(_REGISTRY))
def test_server_declares_the_same_target_universe(module_path: str) -> None:
    """Each registered module's declared universe matches the client's."""
    mod = importlib.import_module(module_path)
    declared = tuple(getattr(mod, "LORA_TARGET_UNIVERSE"))
    assert declared == _REGISTRY[module_path].target_universe
```

Plus, in the H3 server test module, using the existing `server` fixture:

```python
def test_health_declares_the_loaded_partition_only(server: Any) -> None:
    """A t2va pod advertises one target — the partition it actually holds.

    Catches a profile built from a constant: a ref2va pod would otherwise
    advertise a partition it cannot serve.
    """
    with TestClient(server.app) as client:
        body = client.get("/health").json()
    assert body["lora"]["supported"] is True
    assert body["lora"]["targets"] == ["transformer"]
    assert body["lora"]["default_target"] == "transformer"
    assert body["lora"]["profile"] == "minimax-h3-t2va"
```

and a dual-partition variant asserting `targets == ["transformer", "transformer_ref"]` and `default_target is None`.

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/engines/diffusers/test_lora_profile_parity.py -v`
Expected: FAIL — `AttributeError: module … has no attribute 'LORA_TARGET_UNIVERSE'`

- [ ] **Step 3: Implement in the H3 server**

```python
LORA_TARGET_UNIVERSE: tuple[str, ...] = ("transformer", "transformer_ref")

_PRUNED_HINT = (
    "this looks like a LoRA trained against a PRUNED MiniMax-H3 checkpoint "
    "(the `*_pruned_*` / `*_comfyui_*` files in Comfy-Org/MiniMax-H3; "
    "joyfox/MiniMax-H3-Turbo is one). diffusers cannot load those. Use "
    "lightx2v/Minimax-h3-Turbo (…_8step_v1.0_bf16.safetensors) or "
    "larryvrh/MiniMax-H3-Turbo-Lora instead."
)


def _explain_load_failure(exc: BaseException) -> str | None:
    """Return the pruned-checkpoint hint for a size mismatch, else None."""
    text = str(exc).lower()
    if "size mismatch" in text or "shape mismatch" in text:
        return _PRUNED_HINT
    return None


def _build_lora_profile(pipe_obj: Any) -> _lora.LoraProfile:
    """Build the H3 LoRA profile from the partitions this workflow loaded.

    Read off the pipeline, never from a constant: `t2va` holds `transformer`
    alone, `ref2va` holds `transformer_ref` alone, and only a pipeline holding
    BOTH is ambiguous enough to require an explicit target.
    """
    present = tuple(
        name
        for name in LORA_TARGET_UNIVERSE
        if getattr(pipe_obj, name, None) is not None
    )
    default = present[0] if len(present) == 1 else None

    def _load(p: Any, path: str, adapter_name: str, target: str) -> None:
        p.load_lora_weights(
            path,
            adapter_name=adapter_name,
            load_into_transformer_ref=(target == "transformer_ref"),
        )

    def _after_load(p: Any) -> None:
        # DiffSynth-Studio H3 LoRAs carry fp32 factors; without this the
        # unfused path computes in fp32 and the bf16 memory budget is gone on
        # a card already at ~77 GB of 131 GiB.
        import torch

        for name in present:
            getattr(p, name).to(torch.bfloat16)

    return _lora.LoraProfile(
        name="minimax-h3-t2va",
        targets=present,
        default_target=default,
        load=_load,
        module_for=lambda p, target: getattr(p, target),
        after_load=_after_load,
        explain_load_failure=_explain_load_failure,
    )
```

In `_startup`, after `pipe, manager = _load()` and before `ready.set()`: build the profile into a module-level `lora_profile`, then `app.include_router(_lora.build_lora_router(lambda: pipe, lambda: lora_profile, LORAS_DIR))`. Extend `health()` with the `lora` block, reporting `supported: False` with empty targets while `ready` is unset.

- [ ] **Step 4: Extend the stub**

Give `FakePipe` in `h3_stub_pipe.py` a `transformer` attribute (a small object with `set_adapters` and `to`), an optional `transformer_ref`, and recording `load_lora_weights` / `unload_lora_weights`. Add a `stub_loader_dual` entry point for the two-partition case. Keep the existing recording-dict discipline the module docstring explains.

- [ ] **Step 5: Run the tests**

Run: `pixi run python -m pytest tests/engines/diffusers/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/minimax_h3_server.py
git add tests/engines/diffusers/servers/h3_stub_pipe.py
git add tests/engines/diffusers/test_minimax_h3_server.py
git add tests/engines/diffusers/test_lora_profile_parity.py
git commit -m "feat(h3): mount the LoRA router and declare the loaded partitions"
```

---

### Task 6: Client error mapping + `target` on the wire

**Goal:** The two new error bodies map to typed exceptions instead of `RuntimeError`, and `target` reaches the pod only when set.

**Files:**
- Modify: `src/kinoforge/core/errors.py`
- Modify: `src/kinoforge/engines/diffusers/__init__.py:718-724` (payload), `:872-929` (`_raise_lora_swap_error`)
- Test: `tests/engines/test_diffusers_set_lora_stack.py` (extend)

**Acceptance Criteria:**
- [ ] `{"error": "lora_format_unsupported", "hint": …, "ref": …}` at 400 raises `LoraFormatUnsupportedError` carrying both
- [ ] `{"error": "lora_load_failed", …}` at 500 raises `LoraLoadFailedError`
- [ ] Both expose `.manual_cleanup_command()` like their siblings
- [ ] An entry with `target=None` produces a payload with **no** `target` key
- [ ] An entry with `target="transformer"` produces `{"ref": …, "strength": …, "branch": …, "target": "transformer"}`
- [ ] Unknown bodies still raise `RuntimeError`

**Verify:** `pixi run python -m pytest tests/engines/test_diffusers_set_lora_stack.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
def test_format_unsupported_body_raises_typed_error() -> None:
    """A pruned-checkpoint refusal is typed and carries the hint.

    Catches it landing in the `unknown … error body` RuntimeError, which loses
    the one piece of information that tells the operator what to do next.
    """
    backend = _backend_returning(
        status=400,
        body={"error": "lora_format_unsupported", "hint": "use lightx2v/…", "ref": "hf:o/r:f"},
    )
    with pytest.raises(LoraFormatUnsupportedError) as excinfo:
        backend.set_lora_stack(pod_id="p1", active_stack=[_entry()], download_specs={})
    assert excinfo.value.ref == "hf:o/r:f"
    assert "lightx2v" in excinfo.value.hint


def test_unset_target_is_omitted_from_the_payload() -> None:
    """An absent target must not appear as a null on the wire.

    A warm pod from an older image has no `target` field and `extra="forbid"`,
    so an always-present key 422s every swap against it.
    """
    sent: dict[str, Any] = {}
    backend = _backend_capturing(sent)
    backend.set_lora_stack(pod_id="p1", active_stack=[_entry(target=None)], download_specs={})
    assert "target" not in sent["body"]["target"][0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/engines/test_diffusers_set_lora_stack.py -v`
Expected: FAIL — `ImportError: cannot import name 'LoraFormatUnsupportedError'`

- [ ] **Step 3: Add the error classes**

In `core/errors.py`, beside `LoraSwapDownloadError`, following its constructor and `manual_cleanup_command` shape exactly:

```python
class LoraFormatUnsupportedError(KinoforgeError):
    """The pod refused a LoRA file it can never load (wrong checkpoint lineage).

    Non-retryable: retrying downloads the same bytes and fails identically.
    """

    def __init__(self, pod_id: str, ref: str, hint: str) -> None:
        super().__init__(f"pod {pod_id} cannot load LoRA {ref}: {hint}")
        self.pod_id = pod_id
        self.ref = ref
        self.hint = hint


class LoraLoadFailedError(KinoforgeError):
    """A LoRA download succeeded but the load raised; the stack was rolled back."""

    def __init__(self, pod_id: str, ref: str, underlying: str) -> None:
        super().__init__(f"pod {pod_id} failed to load LoRA {ref}: {underlying}")
        self.pod_id = pod_id
        self.ref = ref
        self.underlying = underlying
```

- [ ] **Step 4: Map them, and make `target` conditional**

In `_raise_lora_swap_error`, before the `disk_full` branch:

```python
        if err == "lora_format_unsupported":
            raise LoraFormatUnsupportedError(
                pod_id=pod_id,
                ref=str(body.get("ref", "")),
                hint=str(body.get("hint", "")),
            )
        if err == "lora_load_failed":
            raise LoraLoadFailedError(
                pod_id=pod_id,
                ref=str(body.get("ref", "")),
                underlying=str(body.get("underlying", "")),
            )
```

In the payload builder, replace the dict comprehension with one that adds `target` only when it is not `None`, and keep the existing comment explaining why (add the old-pod 422 reason).

- [ ] **Step 5: Run the tests**

Run: `pixi run python -m pytest tests/engines/test_diffusers_set_lora_stack.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/errors.py src/kinoforge/engines/diffusers/__init__.py
git add tests/engines/test_diffusers_set_lora_stack.py
git commit -m "feat(lora): type the format/load failures and gate target on the wire"
```

---

### Task 7: Apply the stack before the first generation

**Goal:** Every generate path applies the resolved stack after the pod is ready, and a failure fails the run.

**Files:**
- Create: `src/kinoforge/core/lora_apply.py`
- Modify: `src/kinoforge/core/orchestrator.py:2158-2206` (immediately before `yield session`)
- Test: `tests/core/test_lora_apply.py`, `tests/integration/test_lora_apply_gates_generate.py`

**Acceptance Criteria:**
- [ ] An empty stack makes no HTTP call
- [ ] A non-empty stack resolves each ref through `registry.source_for_ref` and POSTs one `set_lora_stack`
- [ ] A backend without `set_lora_stack` (hosted engines) is a no-op
- [ ] On `LoraFormatUnsupportedError` the error propagates and `submit` is never called
- [ ] Refs never appear in any log line emitted by this module
- [ ] `hf:` and `civitai:` refs both produce a spec with `url`, `headers`, `filename`, `size_hint`

**Verify:** `pixi run python -m pytest tests/core/test_lora_apply.py tests/integration/test_lora_apply_gates_generate.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_lora_apply_gates_generate.py
"""Behavior: a LoRA that did not load must cost an error, not a video.

This is the defect the whole design exists to remove. If this test ever goes
green while generation proceeds, the feature is worse than not having it: the
operator gets a plausible-looking clip with no LoRA in it.
"""

def test_failed_apply_prevents_generation(...) -> None:
    """A refused stack raises out of deploy_session and submit is never called."""
    backend = FakeBackend(set_stack_raises=LoraFormatUnsupportedError(
        pod_id="p1", ref="hf:o/r:f", hint="use lightx2v/…"
    ))
    with pytest.raises(LoraFormatUnsupportedError):
        with deploy_session(cfg_with_loras, store=store, ...) as session:
            session.pool.submit(job)
    assert backend.submit_calls == []


def test_empty_stack_issues_no_http_call(...) -> None:
    """Runs without LoRAs are untouched.

    Catches a set_stack POST on every run, which against a server with no LoRA
    surface is a 404 that breaks runs working today.
    """
    with deploy_session(cfg_without_loras, store=store, ...) as session:
        pass
    assert backend.set_stack_calls == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/integration/test_lora_apply_gates_generate.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.core.lora_apply`

- [ ] **Step 3: Implement `core/lora_apply.py`**

```python
def resolve_download_specs(
    refs: list[str], creds: CredentialProvider
) -> dict[str, dict[str, object]]:
    """Resolve each ref to the pod-side download spec shape.

    Routes through ``registry.source_for_ref`` so `hf:`, `civitai:`,
    `civarchive:` and `http:` all work with no vendor code here. Picks the
    `.safetensors` artifact when the source returns several.

    Privacy: raises carry no ref text — the caller logs counts only.
    """
```

and:

```python
def ensure_lora_stack(
    *, backend: object, cfg: Any, pod_id: str | None, creds: CredentialProvider
) -> None:
    """Apply cfg/vault/CLI's resolved LoRA stack to a ready pod.

    No-ops when the stack is empty or the backend has no
    ``set_lora_stack``. Any failure propagates: a stack that did not load
    must fail the run rather than silently produce a LoRA-less video.
    """
    stack = resolve_active_lora_stack(cfg, vault, cli_loras=cli_loras)
    if not stack or not hasattr(backend, "set_lora_stack") or pod_id is None:
        return
    _log.info("lora-apply: applying %d entrie(s) to pod", len(stack))  # count, never refs
    specs = resolve_download_specs([lo.ref for lo in stack], creds)
    backend.set_lora_stack(pod_id=pod_id, active_stack=stack, download_specs=specs)
```

Read vault + CLI overrides through `EphemeralSession.current()` exactly as `warm_reuse/integration.py:115-120` does.

- [ ] **Step 4: Call it from the orchestrator**

In `deploy_session`, directly before `try: yield session` (after the `Ledger.touch(session_start=…)` block), add:

```python
            # The LoRA stack is applied HERE — after the pod reports ready and
            # before any job is submitted — so cold pods and caller-supplied
            # warm pods take the same path. A failure raises: generating with a
            # stack that did not load is the silent defect this closes.
            ensure_lora_stack(
                backend=backend,
                cfg=cfg,
                pod_id=instance.id if instance is not None else None,
                creds=creds,
            )
```

- [ ] **Step 5: Run the tests**

Run: `pixi run python -m pytest tests/core/test_lora_apply.py tests/integration/test_lora_apply_gates_generate.py -v`
Expected: PASS

- [ ] **Step 6: Run the orchestrator suite**

Run: `pixi run python -m pytest tests/ -k "orchestrator or deploy_session" -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/lora_apply.py src/kinoforge/core/orchestrator.py
git add tests/core/test_lora_apply.py tests/integration/test_lora_apply_gates_generate.py
git commit -m "feat(lora): apply the stack after ready, and fail the run if it cannot"
```

---

### Task 8: Delete the lossy adapter bridge

**Goal:** One payload builder, not two.

**Files:**
- Modify: `src/kinoforge/_adapters.py:341-374` (delete `build_set_stack_request`)
- Delete: `tests/test_adapters_build_set_stack_request.py`
- Modify: `tests/integration/test_loras_cli_e2e.py` (use the backend payload builder)

**Acceptance Criteria:**
- [ ] `build_set_stack_request` no longer exists anywhere in `src/`
- [ ] `tests/integration/test_loras_cli_e2e.py` asserts the same behaviour through `DiffusersBackend`
- [ ] Full suite green

**Verify:** `pixi run python -m pytest tests/ -q` → 0 failures

**Steps:**

- [ ] **Step 1: Confirm the only callers are tests**

Run: `rg -n "build_set_stack_request" src/ tests/`
Expected: one definition in `src/kinoforge/_adapters.py`, plus two test modules.

- [ ] **Step 2: Port the e2e assertions**

Rewrite `tests/integration/test_loras_cli_e2e.py` to capture the body `DiffusersBackend.set_lora_stack` posts (the same capture seam Task 6 added), asserting ref/strength/target ordering survives the CLI → resolve → payload path.

- [ ] **Step 3: Delete**

```bash
git rm tests/test_adapters_build_set_stack_request.py
```
and remove the function plus its now-unused imports from `_adapters.py`.

- [ ] **Step 4: Run the full suite**

Run: `pixi run python -m pytest tests/ -q`
Expected: 0 failures.

- [ ] **Step 5: Commit**

```bash
git add -A src/kinoforge/_adapters.py tests/
git commit -m "refactor(lora): drop the second, lossy set_stack payload builder"
```

---

### Task 9: The turbo config, the goldens, and the payload ceiling

**Goal:** A shipped H3 LoRA config exists, the golden ratchet is re-baked deliberately, and the env payload can never silently cross RunPod's limit.

**Files:**
- Create: `examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml`
- Create: `tests/providers/test_env_payload_ceiling.py`
- Modify: every `tests/providers/golden/launch_payloads/*.json` + `_golden_provision.json` (regenerated, not hand-edited)

**Acceptance Criteria:**
- [ ] `pixi run kinoforge doctor -c examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml` exits 0
- [ ] The config names the exact turbo ref from Global Constraints, `num_inference_steps: 8`, 640x352, 124 frames
- [ ] A new golden exists for the config and every pre-existing golden's diff is blob-only
- [ ] `test_env_payload_ceiling` asserts < 90 KB for every RunPod diffusers config and fails when the budget is exceeded
- [ ] Goldens were regenerated AFTER `pre-commit run --all-files`

**Verify:** `pixi run python -m pytest tests/providers/ -q` → 0 failures

**Steps:**

- [ ] **Step 1: Write the ceiling guard first**

```python
# tests/providers/test_env_payload_ceiling.py
"""Behavior: no shipped RunPod config may approach the create-mutation limit.

RunPod's `podFindAndDeployOnDemand` returns a RAW HTTP 500 — no GraphQL
`errors[]` body — once the env payload crosses ~101 KB, which reads as an
outage and cost a session to root-cause on 2026-07-05. A guard is ten lines.
"""

_BUDGET_BYTES = 90_000


@pytest.mark.parametrize("cfg_path", sorted(_runpod_diffusers_configs()))
def test_rendered_env_stays_under_budget(cfg_path: Path) -> None:
    """Each config's total rendered env is under 90 KB.

    Catches the next embed addition crossing the ceiling, whose only symptom
    at runtime is an unexplained 500 at pod create.
    """
    env = _render_env_for(cfg_path)
    total = sum(len(k) + len(v) for k, v in env.items())
    assert total < _BUDGET_BYTES, f"{cfg_path.name}: env {total} B exceeds {_BUDGET_BYTES} B"
```

Run it before adding `_lora.py` to confirm it passes today, then again after — the delta is the module's real cost.

- [ ] **Step 2: Write the config**

Copy `examples/configs/modal-diffusers-minimax-h3-t2va-long-640.yaml` and change only: the header (explain the LoRA, name the pruned-file trap, state that `_comfyui_` variants are unloadable), `spec.num_inference_steps: 8`, `spec.num_frames: 124`, and add:

```yaml
loras:
  # PROBED 2026-09-22 — 1.38 GB, 624 BF16 tensors, __metadata__ alpha=8.
  # Do NOT substitute the 1.96 GB `_comfyui_bf16` sibling: it is trained
  # against the pruned checkpoint and diffusers refuses it with a size
  # mismatch. Target is omitted deliberately — under workflow="t2va" the pod
  # holds one partition and resolves it itself.
  - ref: "hf:lightx2v/Minimax-h3-Turbo:minimax_h3_fl2v_turbo_8step_v1.0_bf16.safetensors"
    strength: 1.0
```

- [ ] **Step 3: Validate the config offline**

Run: `pixi run kinoforge doctor -c examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml`
Expected: exit 0, no ERROR rows.

- [ ] **Step 4: Format FIRST, then regenerate goldens**

```bash
git add -A
pixi run pre-commit run --all-files
git add -A
pixi run python tools/snapshot_launch_payloads.py
```

- [ ] **Step 5: Review the golden diff key-by-key**

Run: `git diff --stat tests/providers/golden/`
Expected: every changed file differs ONLY in the embedded base64 blob leaves plus the one new config's golden. Any changed non-blob key is a real regression — stop and investigate.

- [ ] **Step 6: Run the provider suite**

Run: `pixi run python -m pytest tests/providers/ -q`
Expected: 0 failures.

- [ ] **Step 7: Commit**

```bash
git add -A examples/configs tests/providers
git commit -m "feat(h3): ship the turbo-LoRA config and guard the env payload ceiling"
```

---

### Task 10: Docs, PROGRESS, and the deprecation record

**Goal:** The deprecation and the debt are written down where the next session will find them.

**Files:**
- Modify: `docs/breaking-changes.md`, `docs/warm-reuse.md`, `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `docs/breaking-changes.md` describes `branch` → `target` with a before/after YAML pair
- [ ] `docs/warm-reuse.md` states that H3 serves set_stack WITHOUT eviction
- [ ] `PROGRESS.md` RESUME SNAPSHOT records: two coexisting LoRA implementations, no eviction on H3 (`<volume>/loras` grows unbounded), and that Wan cold-boot `cfg.loras` is still a no-op
- [ ] Full suite green

**Verify:** `pixi run python -m pytest tests/ -q` → 0 failures

**Steps:**

- [ ] **Step 1: Write the breaking-changes entry** with a concrete before/after:

```yaml
# before (still works, warns)
loras:
  - ref: "civitai:1234@5678"
    branch: high_noise
# after
loras:
  - ref: "civitai:1234@5678"
    target: high_noise
```

- [ ] **Step 2: Update `docs/warm-reuse.md`** — one paragraph: H3 serves the same contract minus eviction; the matcher does not yet route H3 pods.

- [ ] **Step 3: Update `PROGRESS.md`** RESUME SNAPSHOT + the debt list from spec §10.

- [ ] **Step 4: Full suite + pre-commit**

```bash
pixi run pre-commit run --all-files
pixi run python -m pytest tests/ -q
```
Expected: hooks pass, 0 test failures.

- [ ] **Step 5: Commit**

```bash
git add -A docs PROGRESS.md
git commit -m "docs: record the target deprecation and the H3 LoRA debt"
```

---

### Task 11: Live proof — one pod, two applies

**Goal:** Prove on real weights that both profile branches work and that the LoRAs visibly engage.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new section + See-also)
- Modify: `PROGRESS.md` (evidence + spend)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before any spend; working tree clean (Tasks 1–10 committed)
- [ ] Run 1: turbo LoRA applied, 8 steps, 640x352 / 124 frames, MP4 produced with audio
- [ ] Run 2: second apply of the style LoRA on the same pod, MP4 produced
- [ ] `/lora/inventory` after run 2 lists exactly the style LoRA as active
- [ ] GPU utilisation observed non-zero during each denoise (probe every 60–90 s)
- [ ] Frame QA on BOTH outputs: contact sheets read, verdict recorded; run 2 is lineart or the run is a FAIL
- [ ] `kinoforge list` from a fresh process AFTER the orchestrator exits shows no running instances and an empty ledger
- [ ] Total spend recorded; `successful-generations.md` entry written

**Verify:** `pixi run kinoforge list` → `[instance overview] No running instances.` AND `No instances recorded in ledger.`

**Steps:**

- [ ] **Step 1: Preflight and confirm the tree is committed**

```bash
pixi run preflight
git status --porcelain
```
Expected: preflight exit 0; `git status` empty. A dirty tree here violates the RED-scaffold-before-spend rule — commit first.

- [ ] **Step 2: Run 1 — turbo**

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml \
  --mode t2va \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --emit-provision-record /tmp/h3-lora-pod.json
```

Note: **no `--no-reuse`** on this run — run 2 needs the warm pod. Teardown is explicit in Step 6.

- [ ] **Step 3: Poll utilisation while it runs (every 60–90 s)**

```python
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
import os
from kinoforge.providers.modal.util import probe  # match the Modal util probe's actual entry point
```

Use the provider's own util probe; surface `gpuUtilPercent` each poll. GPU 0% for three consecutive probes during a denoise → capture the log, destroy, fail fast.

- [ ] **Step 4: Run 2 — style LoRA, same pod**

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-minimax-h3-t2va-lora-turbo.yaml \
  --attach-pod "$(python -c 'import json;print(json.load(open("/tmp/h3-lora-pod.json"))["pod_id"])')" \
  --mode t2va \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --loras "$(cat <<'EOF'
hf:DiffSynth-Studio/MiniMax-H3-LoRA-LineartAnime:model.safetensors
EOF
)" \
  --no-reuse
```

- [ ] **Step 5: Frame QA on both outputs**

```bash
pixi run python -c "
from pathlib import Path
from kinoforge.core.frames import ffmpeg_frames_by_count
print(ffmpeg_frames_by_count(Path('<run1.mp4>'), count=5, out_dir=Path('/tmp/qa1')))
"
```

Read the contact sheets. Run 2 must be visibly lineart. For run 1, compare against `output/20260918-152249_diffusers_MiniMax-H3_A-giant-chocolate-vo.mp4` (50 steps, no LoRA). **If run 1's 8-step output is ambiguous rather than clearly distilled, fire one more 8-step render with an empty stack (~$0.10) to settle it** — do not record a maybe.

- [ ] **Step 6: Verify teardown from a fresh process**

```bash
pixi run kinoforge list
```
Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` If either shows a pod: `pixi run kinoforge destroy --id <pod-id>`, then re-check.

- [ ] **Step 7: Record the evidence**

Write the `successful-generations.md` section per that file's preamble schema (new capability axis: first LoRA on H3, first LoRA on a t2va model), including both refs, the step counts, the wall-clock, the spend, and the QA verdicts. Update `PROGRESS.md`.

- [ ] **Step 8: Commit**

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs: record the H3 LoRA live proof — turbo and style, one pod"
```

---

## Self-Review

**Spec coverage:** §3 → Task 1. §4.1 → Tasks 3, 5. §4.2 → Task 2. §4.3 → Tasks 4, 5, 6. §5.1 → Tasks 3, 4. §5.2 → Task 7. §5.3 → Tasks 3, 4, 6. §6 → Tasks 5, 9. §7 → Task 9. §8 tests 1–13 → Tasks 1 (1–3), 2 (4–6), 3 (8, 10), 4 (9), 5 (12), 6 (9), 7 (7, 11), 9 (13). §9 → Task 11. §10 → Task 10.

**Deviation from the spec, recorded deliberately:** spec §2 D8 says "Wan untouched". Task 1 adds the `target` field to `LoraTarget` inside `wan_t2v_server.py`. This is forced, not optional: that model is `extra="forbid"`, so a payload carrying `target` would 422 on every Wan pod. The change is purely additive and Wan's routing still reads `branch`; no handler, no LRU, no registry code is touched. The companion rule — the client omits `target` when `None` — keeps already-running older pods working.

**Type consistency:** `LoraProfile` fields are identical in Tasks 3, 4 and 5. `ResolvedEntry(ref, path, strength, target)` is used identically in Tasks 3 and 4. `LORA_TARGET_UNIVERSE` is defined in Task 5 and consumed by the parity test in the same task. `client_profile_for_server_module` / `server_module_from_cfg` are defined in Task 2 and used in Task 2's check only. `LoraFormatUnsupportedError` / `LoraLoadFailedError` are defined in Task 6 and referenced in Task 7's test.
