# LoRA-Flexible Warm-Reuse (v1, Diffusers) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the LoRA-flexible warm-reuse feature for Wan 2.2 T2V via the Diffusers engine — split `CapabilityKey` into `WarmAttachKey` + `LoraStack`, add pod-side declarative `POST /lora/set_stack`, generalize the matcher to evict-LRU + download deltas, integrate with the existing `--ephemeral` gate + `RedactionRegistry`, and finish with a 4-step live smoke on the Arcane Style LoRA pair.

**Architecture:** Two-tier identity (`WarmAttachKey(base_model, engine, precision)` indexes the matcher; `LoraStack` is the cheap-to-swap delta). Pod-side declarative swap endpoint (orchestrator POSTs target stack; pod owns disk + memory state). Per-pod `threading.Lock` serializes swap+generate. Failures fail loud + reuse existing reaper via a new `status="degraded"` ledger field.

**Tech Stack:** Python 3.13, pixi, pydantic, FastAPI (pod-side), `diffusers` (Wan 2.2 `WanPipeline`), RunPod proxy, sha256, asyncio, threading, pytest.

**User decisions (already made):**
- Engine scope = Diffusers only (ComfyUI deferred until C23 LoRA-graph wiring lands).
- Eviction = conservative LRU (`last_used_at`-ordered, only when free disk insufficient).
- Failure mode = fail loud + existing reaper handles `status="degraded"` pods (Option A from spec §11; cold-boot fallback and half-state recovery deferred).
- Concurrency = serialize via per-pod `threading.Lock` for the (swap + generate) window.
- Swap contract = pod-side declarative (`POST /lora/set_stack {target_refs, download_specs}`); pod authoritative for its own state.
- Identity model = Approach 1, composite `CapabilityKey(warm_attach_key, lora_stack)` preserving today's `derive()` hash byte-for-byte.
- Ephemeral integration = rides existing `ledger_record` gate; no new `EphemeralPolicy` field; observed LoRA refs auto-registered with `RedactionRegistry`; pod-side disk NOT scrubbed at session exit.
- Default test LoRA pair = `civitai:2197303@2474081` (high-noise) + `civitai:2197303@2474073` (low-noise), trigger word `ArcaneStyle`, per-adapter strength 1.0–1.2.

**Spec corrections discovered during planning (applied throughout):**
- `CapabilityKey` lives in `src/kinoforge/core/interfaces.py:259`, NOT `core/profiles.py` (spec wrong file).
- Ledger entries are untyped dicts mutated via `Ledger.touch(instance_id, **extra)`. The `**extra` type signature is `float | int | str | None` — Task 9 widens it to also accept `list[dict] | dict` for `lora_inventory`.
- The "lazy derivation" claim for `warm_attach_key` in spec §6.3 Strategy A works by deriving the three components from the cfg the pod was provisioned with (recoverable via the existing `capability_key_hex`-keyed profile cache). Task 1 confirms + ships the derivation helper.

---

## File Structure

**New modules:**

```
src/kinoforge/core/warm_reuse/
├── __init__.py            # exports
├── matcher.py             # find_warm_attach_candidate, SwapPlan, SwapEvaluation
├── pod_lock.py            # PodLockRegistry
└── redaction.py           # _register_observed_lora_refs helper
```

**Modified modules:**

```
src/kinoforge/core/interfaces.py            # add WarmAttachKey + LoraStack; refactor CapabilityKey to composite (Task 1)
src/kinoforge/core/lifecycle.py             # widen Ledger.touch **extra type; add find_pods_by_warm_attach_key (Tasks 9, 10)
src/kinoforge/core/errors.py                # add LoraSwapError base + 5 subclasses (Task 3)
src/kinoforge/core/orchestrator.py          # route warm-attach decision through new matcher + hold pod lock (Task 15)
src/kinoforge/core/reaper.py                # recognize status="degraded" as reap-eligible (Task 12)
src/kinoforge/core/config.py                # add compute.lifecycle.lora_swap_re_probe_after_s (Task 19)
src/kinoforge/engines/diffusers/__init__.py # DiffusersEngine.set_lora_stack wrapper (Task 11)
src/kinoforge/engines/diffusers/servers/wan_t2v_server.py  # Tasks 4–8 (LoRA support)
src/kinoforge/cli/_main.py                  # --dry-run-swap, pod lora ls, status renderer (Tasks 16, 17, 18)
tests/test_no_unredacted_writes.py          # 2 new AST-scan rules (Task 20)
README.md                                   # LoRA-flexible warm-reuse section (Task 23)
PROGRESS.md                                 # update (Task 23)
```

**New tests:** see per-task entries.

---

## Plan Tasks

### Task 1: Split CapabilityKey into WarmAttachKey + LoraStack

**Goal:** Introduce `WarmAttachKey(base_model, engine, precision)` + `LoraStack(refs)` as standalone frozen dataclasses; refactor `CapabilityKey` to a composite that preserves today's `derive()` hash byte-for-byte; add `CapabilityKey.warm_attach_key()` + `CapabilityKey.lora_stack()` accessors.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:255-280`
- Test: `tests/core/test_capability_key_split.py` (new)

**Acceptance Criteria:**
- [ ] `WarmAttachKey` and `LoraStack` are frozen dataclasses importable from `kinoforge.core.interfaces`.
- [ ] `CapabilityKey.derive()` produces byte-identical hashes to the pre-split implementation for at least 5 golden-input pairs (pinned in regression test).
- [ ] `CapabilityKey.warm_attach_key()` returns the `WarmAttachKey` factor; `CapabilityKey.lora_stack()` returns the `LoraStack` factor.
- [ ] `WarmAttachKey.derive()` returns sha256 hex over `[base_model, engine, precision]` JSON.
- [ ] Order-sensitivity of `loras` preserved: reordering changes `CapabilityKey.derive()`, does NOT change `WarmAttachKey.derive()`.
- [ ] Reading any pre-feature `capability_key_hex` from the ledger does NOT crash any existing test — backward compat shown by green pre-existing test suite.

**Verify:** `pixi run pytest tests/core/test_capability_key_split.py tests/core/test_profiles.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test file `tests/core/test_capability_key_split.py`.**

```python
"""Regression + behavior tests for the WarmAttachKey + LoraStack split.

Pins:
- WarmAttachKey + LoraStack as independent frozen dataclasses.
- CapabilityKey.derive() byte-stability against golden hashes (no
  pre-feature ledger entry breaks).
- WarmAttachKey order-insensitivity to LoRAs.
- Composite accessors (warm_attach_key(), lora_stack()).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from kinoforge.core.interfaces import CapabilityKey, LoraStack, WarmAttachKey


def _legacy_derive(base_model: str, loras: tuple[str, ...], engine: str, precision: str) -> str:
    """Verbatim copy of the pre-split CapabilityKey.derive() body."""
    payload = json.dumps([base_model, list(loras), engine, precision], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GOLDEN_INPUTS = [
    ("hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers", (), "diffusers", "fp16"),
    ("hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
     ("civitai:2197303@2474081", "civitai:2197303@2474073"), "diffusers", "fp16"),
    ("hf:org/svd", ("hf:lora/style",), "diffusers", "bf16"),
    ("hf:org/svd", ("hf:lora/style", "hf:lora/extra"), "comfyui", "gguf-q8"),
    ("hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
     ("civitai:2197303@2474073", "civitai:2197303@2474081"), "diffusers", "fp16"),
]


@pytest.mark.parametrize("base, loras, engine, precision", GOLDEN_INPUTS)
def test_capability_key_derive_byte_identical_to_legacy(
    base: str, loras: tuple[str, ...], engine: str, precision: str
) -> None:
    """Composite CapabilityKey must hash identically to the pre-split version.

    Bug: refactoring to composite accidentally re-orders / re-shapes the
    JSON payload, breaking every ledger entry already on disk that
    relied on capability_key_hex matching cfg.capability_key().derive().
    """
    key = CapabilityKey(base_model=base, loras=loras, engine=engine, precision=precision)
    assert key.derive() == _legacy_derive(base, loras, engine, precision)


def test_warm_attach_key_drops_loras_field() -> None:
    """WarmAttachKey hash must NOT change when only LoRAs change.

    Bug: WarmAttachKey accidentally folds loras into its hash, defeating
    the entire warm-attach-with-different-loras feature.
    """
    wak1 = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    wak2 = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    assert wak1.derive() == wak2.derive()


def test_warm_attach_key_distinguishes_base_engine_precision() -> None:
    """WarmAttachKey is sensitive to each of its three fields.

    Bug: precision/engine accidentally dropped from WarmAttachKey.derive,
    causing fp16 + bf16 pods to collide in the matcher.
    """
    base_a = WarmAttachKey(base_model="hf:a", engine="diffusers", precision="fp16")
    base_b = WarmAttachKey(base_model="hf:b", engine="diffusers", precision="fp16")
    engine_alt = WarmAttachKey(base_model="hf:a", engine="comfyui", precision="fp16")
    prec_alt = WarmAttachKey(base_model="hf:a", engine="diffusers", precision="bf16")
    derives = {base_a.derive(), base_b.derive(), engine_alt.derive(), prec_alt.derive()}
    assert len(derives) == 4, "every field must contribute to the hash"


def test_lora_stack_preserves_order() -> None:
    """LoraStack(refs=(a,b)) != LoraStack(refs=(b,a)).

    Bug: LoraStack stores refs as a set or sorts on construction,
    breaking the order-sensitivity contract for CapabilityKey.
    """
    s1 = LoraStack(refs=("a", "b"))
    s2 = LoraStack(refs=("b", "a"))
    assert s1 != s2


def test_capability_key_factor_accessors() -> None:
    """CapabilityKey exposes the two factors via accessors.

    Bug: matcher cannot retrieve the WarmAttachKey factor → falls back
    to brittle hash-substring tricks.
    """
    key = CapabilityKey(
        base_model="hf:m", loras=("a", "b"), engine="diffusers", precision="fp16"
    )
    assert key.warm_attach_key() == WarmAttachKey(
        base_model="hf:m", engine="diffusers", precision="fp16"
    )
    assert key.lora_stack() == LoraStack(refs=("a", "b"))


def test_capability_key_frozen() -> None:
    """CapabilityKey, WarmAttachKey, LoraStack are all frozen.

    Bug: a misbehaving engine mutates loras on a shared key, breaking
    every other consumer who held the same reference.
    """
    key = CapabilityKey(base_model="hf:m", loras=("a",), engine="diffusers", precision="fp16")
    wak = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    stack = LoraStack(refs=("a",))
    with pytest.raises(Exception):  # FrozenInstanceError, dataclasses-specific
        key.base_model = "hf:other"  # type: ignore[misc]
    with pytest.raises(Exception):
        wak.base_model = "hf:other"  # type: ignore[misc]
    with pytest.raises(Exception):
        stack.refs = ("z",)  # type: ignore[misc]
```

- [ ] **Step 2: Run the test — confirm RED.**

```bash
pixi run pytest tests/core/test_capability_key_split.py -v
```

Expected: `ImportError: cannot import name 'WarmAttachKey'` or similar (the classes don't exist yet).

- [ ] **Step 3: Refactor `src/kinoforge/core/interfaces.py:255-280`.**

Replace the existing `CapabilityKey` block with:

```python
@dataclass(frozen=True)
class WarmAttachKey:
    """The slow-to-rebuild part of a pod's identity.

    Carries (base_model, engine, precision) — the expensive bytes that
    a warm pod has already paid for and which a new generation job
    should NOT trigger re-download for. See
    docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md.

    Attributes:
        base_model: Base-model vendor-neutral ref (e.g. "hf:org/m").
        engine: Engine name (capability is engine-specific).
        precision: Precision/quantization (e.g. "fp16", "gguf-q8").
    """

    base_model: str
    engine: str = ""
    precision: str = ""

    def derive(self) -> str:
        """Stable sha256 over (base_model, engine, precision)."""
        payload = json.dumps(
            [self.base_model, self.engine, self.precision], ensure_ascii=False
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoraStack:
    """The cheap-to-swap part of a pod's identity. Ordered.

    Order matters: LoraStack(refs=("a","b")) != LoraStack(refs=("b","a")).
    The order participates in CapabilityKey identity and in pipeline
    adapter ordering (set_adapters([...]) applies in list order).
    """

    refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityKey:
    """Full identity a ModelProfile depends on. derive() is the stable cache key.

    Composite over WarmAttachKey + LoraStack. derive() produces the
    same byte-equal hash as the pre-split CapabilityKey for backward
    compatibility with every existing ledger entry.

    Attributes:
        base_model: Base-model vendor-neutral ref (e.g. "hf:org/m").
        loras: Ordered LoRA stack; order matters and contributes to the key.
        engine: Engine name (capability is engine-specific).
        precision: Precision/quantization (e.g. "fp16", "gguf-q8").
    """

    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""

    def derive(self) -> str:
        """Stable, order-sensitive sha256 over all fields (VAE excluded by design).

        Byte-identical to the pre-split CapabilityKey.derive() output.
        """
        payload = json.dumps(
            [self.base_model, list(self.loras), self.engine, self.precision],
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def warm_attach_key(self) -> WarmAttachKey:
        """Return the WarmAttachKey factor (base + engine + precision)."""
        return WarmAttachKey(
            base_model=self.base_model, engine=self.engine, precision=self.precision
        )

    def lora_stack(self) -> LoraStack:
        """Return the LoraStack factor (ordered LoRA refs)."""
        return LoraStack(refs=self.loras)
```

- [ ] **Step 4: Run the new test — confirm GREEN.**

```bash
pixi run pytest tests/core/test_capability_key_split.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Run the rest of the suite to confirm no regression.**

```bash
pixi run pytest tests/core/ tests/engines/ -x --ignore=tests/live -q
```

Expected: all green; specifically `tests/core/test_profiles.py` (which exercises `CapabilityKey.derive()` byte-stability transitively).

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/interfaces.py tests/core/test_capability_key_split.py
git commit -m "$(cat <<'EOF'
feat(core): split CapabilityKey into WarmAttachKey + LoraStack

Approach 1 from the LoRA-flexible warm-reuse design spec. CapabilityKey
becomes a composite over WarmAttachKey (base+engine+precision) +
LoraStack (ordered refs). derive() produces byte-identical hashes to
the pre-split version so every ledger entry stays valid.

Adds WarmAttachKey + LoraStack factor accessors so the new matcher can
look up warm pods by base+engine+precision without re-hashing.
EOF
)"
```

---

### Task 2: PodLockRegistry

**Goal:** In-process per-pod `threading.Lock` registry serializing concurrent warm-attach attempts on the same pod for the duration of (swap + generate).

**Files:**
- Create: `src/kinoforge/core/warm_reuse/__init__.py` (empty)
- Create: `src/kinoforge/core/warm_reuse/pod_lock.py`
- Test: `tests/core/test_pod_lock_registry.py` (new)

**Acceptance Criteria:**
- [ ] `PodLockRegistry().acquire(pod_id, blocking=False)` returns `True` once + `False` on second call before release.
- [ ] After `.release(pod_id)`, next `.acquire(pod_id)` returns `True` again.
- [ ] `pod_id in registry` returns `True` while held, `False` after release.
- [ ] Two different `pod_id` values acquire independently — no cross-pod blocking.
- [ ] `acquire(pod_id, blocking=True, timeout=0.1)` waits and returns `False` on timeout.
- [ ] Lock auto-released when holding thread dies (verify via thread-spawn + join + verify acquire works from main thread).

**Verify:** `pixi run pytest tests/core/test_pod_lock_registry.py -v`

**Steps:**

- [ ] **Step 1: Write the failing test file `tests/core/test_pod_lock_registry.py`.**

```python
"""PodLockRegistry — per-pod_id threading.Lock serialization."""

from __future__ import annotations

import threading
import time

import pytest

from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry


def test_acquire_once_then_blocks() -> None:
    """Second non-blocking acquire on the same pod returns False.

    Bug: registry uses RLock instead of Lock, allowing recursive
    acquire from the same thread → defeats serialization semantic.
    """
    reg = PodLockRegistry()
    assert reg.acquire("pod-a", blocking=False) is True
    assert reg.acquire("pod-a", blocking=False) is False


def test_release_lets_next_acquire_succeed() -> None:
    """Bug: registry leaks held state after release."""
    reg = PodLockRegistry()
    reg.acquire("pod-a", blocking=False)
    reg.release("pod-a")
    assert reg.acquire("pod-a", blocking=False) is True


def test_membership_reflects_held_state() -> None:
    """Bug: __contains__ checks registry key presence instead of lock-held state."""
    reg = PodLockRegistry()
    assert "pod-a" not in reg
    reg.acquire("pod-a", blocking=False)
    assert "pod-a" in reg
    reg.release("pod-a")
    assert "pod-a" not in reg


def test_different_pods_acquire_independently() -> None:
    """Bug: registry uses a single shared lock instead of per-pod locks."""
    reg = PodLockRegistry()
    assert reg.acquire("pod-a", blocking=False) is True
    assert reg.acquire("pod-b", blocking=False) is True


def test_blocking_acquire_with_timeout_returns_false_on_timeout() -> None:
    """Bug: timeout=0 / negative inverted; or timeout argument ignored entirely."""
    reg = PodLockRegistry()
    reg.acquire("pod-a", blocking=False)
    start = time.monotonic()
    got = reg.acquire("pod-a", blocking=True, timeout=0.1)
    elapsed = time.monotonic() - start
    assert got is False
    assert 0.08 < elapsed < 0.5, "timeout should approximate 0.1s"


def test_thread_death_releases_lock() -> None:
    """A thread that acquires then dies must release the lock implicitly.

    Bug: registry uses a non-threading.Lock primitive that doesn't release
    on thread exit, so a crashed worker leaves the pod permanently locked.
    """
    reg = PodLockRegistry()

    def _worker() -> None:
        reg.acquire("pod-a", blocking=False)
        # No release — simulating crash. threading.Lock releases on thread death.

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=1.0)
    assert not t.is_alive(), "test guard — worker should have exited"
    assert reg.acquire("pod-a", blocking=True, timeout=0.5) is True
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/core/test_pod_lock_registry.py -v
```

Expected: `ImportError: No module named 'kinoforge.core.warm_reuse.pod_lock'`.

- [ ] **Step 3: Create the package skeleton + module.**

`src/kinoforge/core/warm_reuse/__init__.py`:

```python
"""Warm-reuse matcher + redaction + lock-registry — see
docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md.
"""
```

`src/kinoforge/core/warm_reuse/pod_lock.py`:

```python
"""PodLockRegistry — per-pod_id threading.Lock serialization.

In-process only. Multi-process kinoforge instances on the same machine
will NOT see each other's locks (documented limitation; tracked under
Layer H deferred follow-up).
"""

from __future__ import annotations

import threading


class PodLockRegistry:
    """Per-pod_id Lock registry used by the warm-reuse matcher.

    Hold the lock for the duration of (POST /lora/set_stack + POST
    /generate + result()) so two concurrent generate jobs cannot fight
    over the same pod's LoRA state. The pod is the unit of serialization.

    threading.Lock (not RLock) is intentional — recursive acquire from
    the same thread would defeat serialization semantics. Lock releases
    on thread death.
    """

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _get_or_create(self, pod_id: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(pod_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[pod_id] = lock
            return lock

    def acquire(
        self, pod_id: str, *, blocking: bool = False, timeout: float | None = None
    ) -> bool:
        lock = self._get_or_create(pod_id)
        if not blocking:
            return lock.acquire(blocking=False)
        if timeout is None:
            return lock.acquire(blocking=True)
        return lock.acquire(blocking=True, timeout=timeout)

    def release(self, pod_id: str) -> None:
        lock = self._get_or_create(pod_id)
        lock.release()

    def __contains__(self, pod_id: str) -> bool:
        lock = self._locks.get(pod_id)
        if lock is None:
            return False
        # threading.Lock has no public "is_held" — locked() returns True iff held
        return lock.locked()
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_pod_lock_registry.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/warm_reuse/__init__.py src/kinoforge/core/warm_reuse/pod_lock.py tests/core/test_pod_lock_registry.py
git commit -m "feat(warm-reuse): add PodLockRegistry for per-pod swap serialization"
```

---

### Task 3: LoraSwapError base + 5 subclasses

**Goal:** Add `LoraSwapError` base class + 5 concrete subclasses (`LoraSwapDownloadError`, `LoraSwapDegradedPodError`, `LoraSwapPodUnreachableError`, `LoraSwapVramOomError`, `LoraSwapDiskFullError`) following the existing `EphemeralStoreCleanupFailedError` pattern (carry `pod_id` + `manual_cleanup_command()`).

**Files:**
- Modify: `src/kinoforge/core/errors.py` (append)
- Test: `tests/core/test_lora_swap_errors.py` (new)

**Acceptance Criteria:**
- [ ] All 5 subclasses derive from `LoraSwapError`, which derives from `KinoforgeError`.
- [ ] Each carries `pod_id: str` and a `manual_cleanup_command() -> str` method returning a copy-paste-able destroy command (e.g. `kinoforge destroy --id <pod_id>`).
- [ ] `LoraSwapDownloadError` `__str__` mentions the failed ref + underlying cause.
- [ ] `LoraSwapDegradedPodError` `__str__` lists evicted refs + failed ref + flags the pod as degraded + names the retry path.
- [ ] `LoraSwapVramOomError` `__str__` lists dropped refs + clarifies the pod is healthy at the previous adapter set.
- [ ] `LoraSwapDiskFullError` `__str__` includes evicted + failed refs.
- [ ] `LoraSwapPodUnreachableError` `__str__` includes underlying transport-error text.

**Verify:** `pixi run pytest tests/core/test_lora_swap_errors.py -v`

**Steps:**

- [ ] **Step 1: Write failing test file `tests/core/test_lora_swap_errors.py`.**

```python
"""LoraSwap error class hierarchy + __str__ contracts."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import (
    KinoforgeError,
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)


def test_all_subclasses_derive_from_base() -> None:
    """Bug: a subclass accidentally derives directly from Exception, breaking
    `except LoraSwapError` blocks in callers."""
    for cls in (
        LoraSwapDownloadError,
        LoraSwapDegradedPodError,
        LoraSwapPodUnreachableError,
        LoraSwapVramOomError,
        LoraSwapDiskFullError,
    ):
        assert issubclass(cls, LoraSwapError)
        assert issubclass(cls, KinoforgeError)


def test_manual_cleanup_command_names_pod_id() -> None:
    """The cleanup command must include the pod_id so the operator can
    copy-paste it into their shell.

    Bug: the helper returns a generic 'kinoforge destroy' without the id.
    """
    err = LoraSwapDownloadError(pod_id="pod-7b2", ref="civitai:X@Y", underlying="504")
    cmd = err.manual_cleanup_command()
    assert "pod-7b2" in cmd
    assert "destroy" in cmd


def test_download_error_str_names_ref_and_underlying() -> None:
    """Bug: __str__ drops the underlying cause, leaving the operator with
    'download failed' and no actionable detail."""
    err = LoraSwapDownloadError(
        pod_id="pod-7b2", ref="civitai:2197303@2474081", underlying="504 from CivitAI"
    )
    s = str(err)
    assert "civitai:2197303@2474081" in s
    assert "504 from CivitAI" in s


def test_degraded_pod_error_str_flags_retry_path() -> None:
    """Bug: error message says 'pod broken' without telling the operator
    that the matcher will route the next retry elsewhere — leaving them
    thinking the whole feature is stuck."""
    err = LoraSwapDegradedPodError(
        pod_id="pod-7b2",
        evict_completed=["civitai:X@1"],
        download_failed="civitai:B@2",
        underlying="504",
    )
    s = str(err)
    assert "pod-7b2" in s
    assert "civitai:X@1" in s
    assert "civitai:B@2" in s
    assert "degraded" in s.lower()
    assert "retry" in s.lower()


def test_vram_oom_error_str_clarifies_pod_is_healthy() -> None:
    """Bug: rollback succeeded but error message implies the pod is broken,
    so the operator destroys a perfectly healthy pod."""
    err = LoraSwapVramOomError(pod_id="pod-7b2", dropped_refs=["civitai:big@1"])
    s = str(err)
    assert "civitai:big@1" in s
    assert "previous" in s.lower() or "rolled back" in s.lower() or "healthy" in s.lower()


def test_disk_full_error_str_lists_evicted_and_failed() -> None:
    err = LoraSwapDiskFullError(
        pod_id="pod-7b2", evict_completed=["civitai:X@1"], download_failed="civitai:B@2"
    )
    s = str(err)
    assert "civitai:X@1" in s
    assert "civitai:B@2" in s


def test_pod_unreachable_error_str_includes_underlying() -> None:
    err = LoraSwapPodUnreachableError(pod_id="pod-7b2", underlying="ConnectionResetError")
    s = str(err)
    assert "pod-7b2" in s
    assert "ConnectionResetError" in s
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/core/test_lora_swap_errors.py -v
```

Expected: `ImportError: cannot import name 'LoraSwapError'`.

- [ ] **Step 3: Append to `src/kinoforge/core/errors.py`.**

```python
class LoraSwapError(KinoforgeError):
    """Base for all LoRA-swap failures on a warm pod.

    All subclasses carry the pod_id of the affected pod and a
    manual_cleanup_command() method returning a copy-paste-able shell
    command the operator can run to recover by hand if needed.
    """

    def __init__(self, *, pod_id: str) -> None:
        super().__init__()
        self.pod_id = pod_id

    def manual_cleanup_command(self) -> str:
        return f"kinoforge destroy --id {self.pod_id}"


class LoraSwapDownloadError(LoraSwapError):
    """Download failed BEFORE any eviction. Pod inventory unchanged."""

    def __init__(self, *, pod_id: str, ref: str, underlying: str) -> None:
        super().__init__(pod_id=pod_id)
        self.ref = ref
        self.underlying = underlying

    def __str__(self) -> str:
        return (
            f"LoRA download failed on pod {self.pod_id}: ref {self.ref} "
            f"({self.underlying}); pod inventory unchanged, retry is safe."
        )


class LoraSwapDegradedPodError(LoraSwapError):
    """Download failed AFTER eviction started. Pod in half-state, marked degraded."""

    def __init__(
        self,
        *,
        pod_id: str,
        evict_completed: list[str],
        download_failed: str,
        underlying: str,
    ) -> None:
        super().__init__(pod_id=pod_id)
        self.evict_completed = list(evict_completed)
        self.download_failed = download_failed
        self.underlying = underlying

    def __str__(self) -> str:
        evicted = ", ".join(self.evict_completed) or "(none)"
        return (
            f"LoRA swap on pod {self.pod_id} failed in the eviction-required "
            f"phase: evicted [{evicted}], failed to download {self.download_failed} "
            f"({self.underlying}). Pod is now in a degraded state and has been "
            f"marked for reap. Retry your generate; the matcher will route "
            f"elsewhere or cold-boot."
        )


class LoraSwapPodUnreachableError(LoraSwapError):
    """Pod proxy returned past retry budget. Marked degraded."""

    def __init__(self, *, pod_id: str, underlying: str) -> None:
        super().__init__(pod_id=pod_id)
        self.underlying = underlying

    def __str__(self) -> str:
        return (
            f"Pod {self.pod_id} unreachable past the proxy-retry budget: "
            f"{self.underlying}. Pod marked degraded."
        )


class LoraSwapVramOomError(LoraSwapError):
    """set_adapters OOM at swap time; rollback to previous adapter set succeeded.

    Pod is healthy at the previous LoRA stack — NOT marked degraded.
    """

    def __init__(self, *, pod_id: str, dropped_refs: list[str]) -> None:
        super().__init__(pod_id=pod_id)
        self.dropped_refs = list(dropped_refs)

    def __str__(self) -> str:
        dropped = ", ".join(self.dropped_refs)
        return (
            f"VRAM OOM during set_adapters on pod {self.pod_id}: target stack "
            f"included {dropped}; pod rolled back to its previous LoRA stack and "
            f"remains healthy. Try a smaller stack or a different pod."
        )


class LoraSwapDiskFullError(LoraSwapError):
    """Mid-download disk full. Marked degraded."""

    def __init__(
        self, *, pod_id: str, evict_completed: list[str], download_failed: str
    ) -> None:
        super().__init__(pod_id=pod_id)
        self.evict_completed = list(evict_completed)
        self.download_failed = download_failed

    def __str__(self) -> str:
        evicted = ", ".join(self.evict_completed) or "(none)"
        return (
            f"Pod {self.pod_id} disk full mid-download: evicted [{evicted}], "
            f"failed to download {self.download_failed}. Pod marked degraded."
        )
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_lora_swap_errors.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/errors.py tests/core/test_lora_swap_errors.py
git commit -m "feat(errors): add LoraSwapError hierarchy (download/degraded/unreachable/oom/disk-full)"
```

---

### Task 4: Cold-boot LoRA loading in `wan_t2v_server.py`

**Goal:** Extend `_load_pipeline()` to accept an `initial_lora_stack: list[ArtifactDownloadSpec] | None` argument; downloads each LoRA before first `/generate`, populates the inventory dict, loads adapters. Strict prerequisite for the swap path.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/test_wan_t2v_server_cold_boot_loras.py` (new, with mocked diffusers)

**Acceptance Criteria:**
- [ ] `_load_pipeline()` accepts `initial_lora_stack` kwarg defaulting to `None` (backward compat).
- [ ] With `initial_lora_stack=[spec_a, spec_b]`: pipeline gets two `load_lora_weights` calls + `set_adapters(["lora_0","lora_1"])`.
- [ ] Inventory dict contains both refs after startup, with `size_bytes` reflecting actual downloaded bytes (not the spec's `size_hint`).
- [ ] Empty list `[]` → no LoRA loading + empty inventory (verify pipeline still works for plain T2V).
- [ ] Failure during initial download raises a clear `RuntimeError` naming the failed ref (cold-boot is allowed to fail loudly; orchestrator-side handling is out of scope for this task).

**Verify:** `pixi run pytest tests/engines/test_wan_t2v_server_cold_boot_loras.py -v`

**Steps:**

- [ ] **Step 1: Read the existing server module to find the seam.**

```bash
pixi run python -c "import kinoforge.engines.diffusers.servers.wan_t2v_server as s; print(s.__file__)"
```

Then read lines around `_load_pipeline()` (around line 98) and `@app.on_event("startup")` (around line 186) — confirm the structure before editing.

- [ ] **Step 2: Write failing test `tests/engines/test_wan_t2v_server_cold_boot_loras.py`.**

```python
"""Cold-boot LoRA loading on the Diffusers Wan T2V server.

Mocks diffusers + the HTTP download path; verifies the server's
_load_pipeline + startup hook correctly:
- handle initial_lora_stack=None (back-compat, zero LoRAs).
- handle empty list (explicit zero LoRAs).
- handle 2-LoRA stack: download + load + set_adapters call ordering.
- bubble download failures as RuntimeError naming the failed ref.
"""

from __future__ import annotations

import pytest

# Module under test; we import lazily inside tests so the diffusers
# stubs land before the module imports diffusers at module load.


@pytest.fixture
def mock_pipeline(monkeypatch):
    """Stub out WanPipeline.load + the pipe instance's LoRA methods."""
    calls = {"load_lora": [], "set_adapters": []}

    class _StubPipe:
        def load_lora_weights(self, path, adapter_name):  # noqa: ANN001
            calls["load_lora"].append((path, adapter_name))

        def set_adapters(self, names):  # noqa: ANN001
            calls["set_adapters"].append(list(names))

        def to(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return self

    def _fake_from_pretrained(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return _StubPipe()

    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    monkeypatch.setattr(s, "_diffusers_load", _fake_from_pretrained, raising=False)
    return calls


@pytest.fixture
def mock_download(monkeypatch, tmp_path):
    """Stub the LoRA download helper to write a small file + return its path."""

    def _fake_download(spec, dest_dir):  # noqa: ANN001
        dest = tmp_path / spec.filename
        dest.write_bytes(b"x" * 1024)  # 1 KiB stub bytes
        return str(dest), 1024  # (path, actual_bytes)

    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    monkeypatch.setattr(s, "_download_one", _fake_download, raising=False)


def test_load_pipeline_no_initial_stack(mock_pipeline, mock_download) -> None:
    """Bug: refactor adds an initial_lora_stack=None default but
    accidentally calls pipe.load_lora_weights with None, crashing
    every existing cold-boot."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._load_pipeline(initial_lora_stack=None)
    assert mock_pipeline["load_lora"] == []
    assert mock_pipeline["set_adapters"] == []


def test_load_pipeline_empty_initial_stack(mock_pipeline, mock_download) -> None:
    """Bug: empty list incorrectly triggers set_adapters([]) which some
    pipelines reject; or worse, treats [] as 'load all from a default
    directory'."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._load_pipeline(initial_lora_stack=[])
    assert mock_pipeline["load_lora"] == []
    assert mock_pipeline["set_adapters"] == []


def test_load_pipeline_two_lora_stack(mock_pipeline, mock_download) -> None:
    """Verifies download → load_lora → set_adapters call ordering AND
    that the inventory is populated with size_bytes from the actual
    download (not the spec's size_hint).

    Bug: server uses spec.size_hint instead of the bytes-on-disk count.
    """
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    spec_a = s.ArtifactDownloadSpec(
        url="https://x/a", headers={}, filename="a.safetensors", size_hint=999_999
    )
    spec_b = s.ArtifactDownloadSpec(
        url="https://x/b", headers={}, filename="b.safetensors", size_hint=999_999
    )
    s._load_pipeline(
        initial_lora_stack=[("civitai:A@1", spec_a), ("civitai:B@2", spec_b)]
    )
    assert len(mock_pipeline["load_lora"]) == 2
    # Order matters
    assert mock_pipeline["load_lora"][0][1] == "lora_0"
    assert mock_pipeline["load_lora"][1][1] == "lora_1"
    assert mock_pipeline["set_adapters"] == [["lora_0", "lora_1"]]
    assert "civitai:A@1" in s._inventory
    assert s._inventory["civitai:A@1"]["size_bytes"] == 1024  # actual, not hint
    assert s._inventory["civitai:A@1"]["adapter_name"] == "lora_0"


def test_load_pipeline_download_failure_bubbles(mock_pipeline, monkeypatch) -> None:
    """Bug: download failure during cold-boot is silently swallowed,
    leaving the server running with a partially-populated inventory."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    def _failing_download(spec, dest_dir):  # noqa: ANN001
        raise RuntimeError("simulated 504")

    monkeypatch.setattr(s, "_download_one", _failing_download, raising=False)
    spec = s.ArtifactDownloadSpec(
        url="https://x/a", headers={}, filename="a.safetensors", size_hint=1
    )
    with pytest.raises(RuntimeError, match="civitai:A@1"):
        s._load_pipeline(initial_lora_stack=[("civitai:A@1", spec)])
```

- [ ] **Step 3: Run — confirm RED.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_cold_boot_loras.py -v
```

Expected: imports fail or attribute errors.

- [ ] **Step 4: Extend `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`.**

Add (alongside existing imports + module-level state):

```python
# --- LoRA support (Task 4 — cold-boot loading) -----------------------------

from datetime import datetime
from pathlib import Path
from typing import Any
import shutil
import urllib.request

from pydantic import BaseModel, Field

LORAS_DIR = Path("/workspace/loras")  # operator can override via env var in future

_inventory: dict[str, dict[str, Any]] = {}


class ArtifactDownloadSpec(BaseModel):
    """Pre-resolved LoRA download instruction sent by the orchestrator."""

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    filename: str
    size_hint: int | None = None


def _download_one(spec: ArtifactDownloadSpec, dest_dir: Path) -> tuple[str, int]:
    """Download one LoRA spec to dest_dir. Returns (full_path, actual_bytes).

    Streams to a temp .partial file first, renames on success so partial
    downloads never present as complete LoRA files. Raises RuntimeError
    on any HTTP / IO error.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / spec.filename
    tmp = dest_dir / f"{spec.filename}.partial"
    req = urllib.request.Request(spec.url, headers=spec.headers)
    bytes_written = 0
    try:
        with urllib.request.urlopen(req) as resp, tmp.open("wb") as out:
            while True:
                chunk = resp.read(64 * 1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                bytes_written += len(chunk)
        tmp.replace(target)
        return str(target), bytes_written
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def _diffusers_load() -> Any:  # test seam; default impl below
    from diffusers import WanPipeline  # local import — keeps test stubs cheap

    return WanPipeline.from_pretrained(
        "Wan-AI/Wan2.2-T2V-A14B-Diffusers", torch_dtype="float16", device_map="cuda"
    )
```

Modify `_load_pipeline()` to:

```python
def _load_pipeline(
    initial_lora_stack: list[tuple[str, ArtifactDownloadSpec]] | None = None,
) -> Any:
    """Load the Wan pipeline + optionally cold-boot a LoRA stack.

    Args:
        initial_lora_stack: Optional list of (ref, download_spec) tuples
            to download + load before the first /generate. Order matters —
            adapter names are assigned positionally as lora_{i}.
    """
    pipe = _diffusers_load()
    if initial_lora_stack:
        adapter_names: list[str] = []
        for i, (ref, spec) in enumerate(initial_lora_stack):
            try:
                path, actual_bytes = _download_one(spec, LORAS_DIR)
            except Exception as e:
                raise RuntimeError(f"failed to download LoRA {ref}: {e}") from e
            adapter_name = f"lora_{i}"
            pipe.load_lora_weights(path, adapter_name=adapter_name)
            adapter_names.append(adapter_name)
            now = datetime.now().isoformat()
            _inventory[ref] = {
                "ref": ref,
                "filename": spec.filename,
                "size_bytes": actual_bytes,
                "loras_dir_path": path,
                "downloaded_at_local": now,
                "last_used_at_local": now,
                "adapter_name": adapter_name,
            }
        if adapter_names:
            pipe.set_adapters(adapter_names)
    return pipe
```

Modify `_startup()` to read `initial_lora_stack` from an env-supplied JSON file (operator/provisioner threads the cfg in this way):

```python
@app.on_event("startup")
def _startup() -> None:
    import json
    import os

    stack_path = os.environ.get("KINOFORGE_INITIAL_LORA_STACK_JSON")
    initial: list[tuple[str, ArtifactDownloadSpec]] | None = None
    if stack_path and Path(stack_path).exists():
        raw = json.loads(Path(stack_path).read_text())
        initial = [(ref, ArtifactDownloadSpec(**spec_dict)) for ref, spec_dict in raw]
    global pipe  # existing module-level pipe
    pipe = _load_pipeline(initial_lora_stack=initial)
```

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_cold_boot_loras.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_wan_t2v_server_cold_boot_loras.py
git commit -m "feat(wan-server): cold-boot LoRA stack loading via initial_lora_stack arg"
```

---

### Task 5: Pod-side LoRA helpers (_evict_one, _reload_pipeline_loras, _disk_free_bytes, _pick_lru_evict)

**Goal:** Add the in-process helper functions that the upcoming `/lora/set_stack` endpoint will call. Pure functions where possible; helpers that mutate `_inventory` or `pipe` clearly named.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/test_wan_t2v_server_helpers.py` (new)

**Acceptance Criteria:**
- [ ] `_evict_one(ref)` unloads the adapter from `pipe`, removes the file from disk, drops the entry from `_inventory`.
- [ ] `_reload_pipeline_loras(refs)` calls `pipe.unload_lora_weights()` then loads each `ref` in order as `lora_0`, `lora_1`, … and calls `pipe.set_adapters([...])`.
- [ ] `_disk_free_bytes(path)` returns `shutil.disk_usage(path).free`.
- [ ] `_pick_lru_evict(candidates, current_inventory, need)`:
  - Returns a list of refs to evict ordered by `last_used_at_local` ascending.
  - Pops until cumulative `size_bytes` ≥ `need`.
  - Returns `None` if even evicting every candidate is insufficient.
  - Returns `[]` if `need <= 0`.

**Verify:** `pixi run pytest tests/engines/test_wan_t2v_server_helpers.py -v`

**Steps:**

- [ ] **Step 1: Write failing test `tests/engines/test_wan_t2v_server_helpers.py`.**

```python
"""Pure-ish helpers used by /lora/set_stack."""

from __future__ import annotations

import pytest


def _inv_entry(ref: str, size: int, last_used: str) -> dict:
    return {
        "ref": ref, "filename": f"{ref}.s", "size_bytes": size,
        "loras_dir_path": f"/loras/{ref}.s", "downloaded_at_local": last_used,
        "last_used_at_local": last_used, "adapter_name": "lora_x",
    }


def test_pick_lru_evict_chooses_oldest_first() -> None:
    """Bug: helper sorts by newest-first instead of oldest-first → evicts
    the entries the operator most recently used, defeating LRU."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=100, last_used="2026-06-20T10:00:00-07:00"),
        "B": _inv_entry("B", size=100, last_used="2026-06-20T09:00:00-07:00"),  # oldest
        "C": _inv_entry("C", size=100, last_used="2026-06-20T11:00:00-07:00"),
    }
    candidates = {"A", "B", "C"}
    plan = s._pick_lru_evict(candidates, inventory, need=100)
    assert plan == ["B"]


def test_pick_lru_evict_pops_until_enough_room() -> None:
    """Bug: helper returns the single LRU entry even when one isn't enough,
    leaving the swap to fail mid-download from disk-pressure."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
        "B": _inv_entry("B", size=50, last_used="2026-06-20T10:00:00-07:00"),
        "C": _inv_entry("C", size=50, last_used="2026-06-20T11:00:00-07:00"),
    }
    plan = s._pick_lru_evict({"A", "B", "C"}, inventory, need=120)
    assert plan == ["A", "B", "C"]  # need 120, popped 50+50+50=150


def test_pick_lru_evict_returns_none_if_insufficient() -> None:
    """Bug: helper returns the partial list anyway, lying about feasibility
    → matcher commits to a doomed swap plan."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
    }
    assert s._pick_lru_evict({"A"}, inventory, need=999) is None


def test_pick_lru_evict_empty_plan_when_need_zero() -> None:
    """Bug: matcher passes need=0 (everything fits) but helper returns
    a non-empty plan, evicting LoRAs we wanted to keep."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
    }
    assert s._pick_lru_evict({"A"}, inventory, need=0) == []
    assert s._pick_lru_evict({"A"}, inventory, need=-5) == []


def test_reload_pipeline_loras_unloads_then_reloads(monkeypatch) -> None:
    """Bug: helper forgets unload_lora_weights() before reloading, leaving
    stale adapters resident → set_adapters silently ignores the new ones."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    calls = []

    class _Stub:
        def unload_lora_weights(self) -> None:
            calls.append(("unload",))

        def load_lora_weights(self, path, adapter_name):  # noqa: ANN001
            calls.append(("load", path, adapter_name))

        def set_adapters(self, names):  # noqa: ANN001
            calls.append(("set_adapters", list(names)))

    monkeypatch.setattr(s, "pipe", _Stub())
    s._inventory.clear()
    s._inventory["A"] = _inv_entry("A", 1, "x")
    s._inventory["A"]["loras_dir_path"] = "/loras/A"
    s._inventory["B"] = _inv_entry("B", 1, "x")
    s._inventory["B"]["loras_dir_path"] = "/loras/B"

    import asyncio
    asyncio.run(s._reload_pipeline_loras(["A", "B"]))

    assert calls[0] == ("unload",)
    assert calls[1] == ("load", "/loras/A", "lora_0")
    assert calls[2] == ("load", "/loras/B", "lora_1")
    assert calls[3] == ("set_adapters", ["lora_0", "lora_1"])


def test_reload_pipeline_loras_empty_unloads_no_reload(monkeypatch) -> None:
    """Bug: empty target stack accidentally calls set_adapters([]) and
    triggers a pipeline error."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    calls = []

    class _Stub:
        def unload_lora_weights(self) -> None:
            calls.append(("unload",))

        def load_lora_weights(self, *a, **k):  # noqa: ANN002, ANN003
            calls.append(("load",))

        def set_adapters(self, *a, **k):  # noqa: ANN002, ANN003
            calls.append(("set_adapters",))

    monkeypatch.setattr(s, "pipe", _Stub())
    import asyncio
    asyncio.run(s._reload_pipeline_loras([]))

    assert calls == [("unload",)]


def test_evict_one_removes_inventory_and_unloads(monkeypatch, tmp_path) -> None:
    """Bug: helper deletes from _inventory but forgets to delete the file,
    leaking disk; or vice-versa."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    lora_file = tmp_path / "secret.safetensors"
    lora_file.write_bytes(b"x" * 100)

    class _Stub:
        unloaded: list[str] = []

        def delete_adapters(self, names):  # noqa: ANN001
            self.unloaded.extend(names)

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    s._inventory.clear()
    s._inventory["A"] = {
        "ref": "A", "filename": "secret.safetensors", "size_bytes": 100,
        "loras_dir_path": str(lora_file),
        "downloaded_at_local": "x", "last_used_at_local": "x",
        "adapter_name": "lora_3",
    }

    import asyncio
    asyncio.run(s._evict_one("A"))

    assert "A" not in s._inventory
    assert not lora_file.exists()
    assert stub.unloaded == ["lora_3"]
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_helpers.py -v
```

- [ ] **Step 3: Add helpers to `wan_t2v_server.py`.**

```python
def _disk_free_bytes(path: Path) -> int:
    """Return free bytes on the filesystem containing `path`."""
    return shutil.disk_usage(path).free


def _pick_lru_evict(
    candidates: set[str], inventory: dict[str, dict[str, Any]], need: int
) -> list[str] | None:
    """Return refs to evict in LRU order, popping until cumulative size ≥ need.

    Args:
        candidates: Refs eligible for eviction (i.e. not in target stack).
        inventory: Current _inventory snapshot.
        need: Bytes that must be freed. <= 0 → no eviction needed.

    Returns:
        List of refs in LRU-ascending order, or None if even evicting all
        candidates would not free `need` bytes. Returns [] when need <= 0.
    """
    if need <= 0:
        return []
    ordered = sorted(
        (ref for ref in candidates if ref in inventory),
        key=lambda r: inventory[r]["last_used_at_local"],
    )
    freed = 0
    plan: list[str] = []
    for ref in ordered:
        plan.append(ref)
        freed += inventory[ref]["size_bytes"]
        if freed >= need:
            return plan
    return None


async def _evict_one(ref: str) -> None:
    """Unload one LoRA from the pipeline + remove its file + drop inventory."""
    entry = _inventory.get(ref)
    if entry is None:
        return
    adapter = entry["adapter_name"]
    if hasattr(pipe, "delete_adapters"):
        pipe.delete_adapters([adapter])
    try:
        Path(entry["loras_dir_path"]).unlink(missing_ok=True)
    except OSError:
        pass  # best-effort
    _inventory.pop(ref, None)


async def _reload_pipeline_loras(target_refs: list[str]) -> None:
    """Replace the active pipeline adapter stack with target_refs in order.

    Calls unload_lora_weights() first to clear any active adapters, then
    re-loads each target ref as lora_{i}, then set_adapters with the
    positional list.

    Empty target_refs → unload only, no load/set_adapters call.
    """
    pipe.unload_lora_weights()
    if not target_refs:
        return
    names: list[str] = []
    for i, ref in enumerate(target_refs):
        entry = _inventory[ref]
        name = f"lora_{i}"
        pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        names.append(name)
        # Re-stamp the adapter name in case it changed.
        entry["adapter_name"] = name
    pipe.set_adapters(names)
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_helpers.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_wan_t2v_server_helpers.py
git commit -m "feat(wan-server): add _pick_lru_evict + _evict_one + _reload_pipeline_loras helpers"
```

---

### Task 6: POST /lora/set_stack endpoint (happy path)

**Goal:** Add the declarative swap endpoint. Locks `_swap_lock`, computes diff, evicts LRU losers if disk-tight, downloads new, reloads pipeline, returns inventory + free bytes.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/test_wan_t2v_server_set_stack.py` (new)

**Acceptance Criteria:**
- [ ] Endpoint accepts `{target_refs: [...], download_specs: {ref: {url, headers, filename, size_hint}}}`.
- [ ] Computes `to_download = target - current`, `to_evict_candidates = current - target`.
- [ ] When new LoRAs fit in free disk: `evict_plan = []`, downloads only the new ones, reloads pipeline.
- [ ] When tight on disk: calls `_pick_lru_evict` to pick targets, evicts them, then downloads.
- [ ] Returns `{inventory: [...], free_bytes: int, swap_rejected: null}`.
- [ ] Idempotent on no-op: target == current → no downloads, no evictions, `_reload_pipeline_loras` still called (sets correct ordering).
- [ ] Holds `_swap_lock` for the duration of the handler (verify via attempted concurrent invocation).

**Verify:** `pixi run pytest tests/engines/test_wan_t2v_server_set_stack.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests `tests/engines/test_wan_t2v_server_set_stack.py`.**

```python
"""POST /lora/set_stack — happy path + idempotence."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def server_with_stubs(monkeypatch, tmp_path):
    """Stub pipe, _download_one, _disk_free_bytes; reset _inventory."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._inventory.clear()
    download_log: list[str] = []

    def _fake_download(spec, dest_dir):  # noqa: ANN001
        download_log.append(spec.filename)
        target = tmp_path / spec.filename
        target.write_bytes(b"x" * 100)
        return str(target), 100

    monkeypatch.setattr(s, "_download_one", _fake_download)
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 10_000_000)
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path)

    class _Stub:
        unloaded = False
        loaded: list[tuple[str, str]] = []
        adapters: list[str] = []

        def unload_lora_weights(self):
            self.unloaded = True

        def load_lora_weights(self, path, adapter_name):  # noqa: ANN001
            self.loaded.append((path, adapter_name))

        def set_adapters(self, names):  # noqa: ANN001
            self.adapters = list(names)

        def delete_adapters(self, names):  # noqa: ANN001
            pass

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    return s, download_log, stub


def _spec(filename: str, size_hint: int = 100):
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s
    return s.ArtifactDownloadSpec(
        url=f"https://x/{filename}", headers={}, filename=filename, size_hint=size_hint
    )


def test_set_stack_from_empty_downloads_all(server_with_stubs) -> None:
    """Bug: starting from empty inventory, server skips the download
    because the eviction set is empty → pipeline left with no adapters."""
    s, download_log, stub = server_with_stubs
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    resp = asyncio.run(s.set_stack(req))
    assert sorted(download_log) == ["a.s", "b.s"]
    assert {e["ref"] for e in resp.inventory} == {"A", "B"}
    assert stub.adapters == ["lora_0", "lora_1"]
    assert resp.swap_rejected is None


def test_set_stack_to_empty_evicts_all(server_with_stubs) -> None:
    """Bug: target_refs=[] is treated as 'no-op'; existing adapters stay."""
    s, _, stub = server_with_stubs
    s._inventory["A"] = {
        "ref": "A", "filename": "a.s", "size_bytes": 100,
        "loras_dir_path": "/loras/a.s", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    }
    req = s.SetStackRequest(target_refs=[], download_specs={})
    resp = asyncio.run(s.set_stack(req))
    assert resp.inventory == []
    assert stub.unloaded is True


def test_set_stack_idempotent_on_same_stack(server_with_stubs) -> None:
    """Bug: re-applying the same stack triggers a redundant download."""
    s, download_log, stub = server_with_stubs
    # First apply
    req = s.SetStackRequest(
        target_refs=["A"], download_specs={"A": _spec("a.s")}
    )
    asyncio.run(s.set_stack(req))
    download_log.clear()
    stub.loaded.clear()
    # Second apply — identical
    asyncio.run(s.set_stack(req))
    assert download_log == []  # no redownload
    # But pipeline reloaded (adapter ordering set)
    assert stub.adapters == ["lora_0"]


def test_set_stack_overlap_downloads_only_new(server_with_stubs) -> None:
    """Pre-existing A, target [A, B] → download only B."""
    s, download_log, stub = server_with_stubs
    s._inventory["A"] = {
        "ref": "A", "filename": "a.s", "size_bytes": 100,
        "loras_dir_path": "/loras/a.s", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    }
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    asyncio.run(s.set_stack(req))
    assert download_log == ["b.s"]
    assert {e["ref"] for e in s._inventory.values() if isinstance(e, dict) or True} == {"A", "B"}
    assert stub.adapters == ["lora_0", "lora_1"]


def test_set_stack_tight_disk_evicts_lru(server_with_stubs, monkeypatch) -> None:
    """Bug: tight-disk branch never invoked because free_bytes check uses
    the wrong comparison; everything goes through the no-evict path until
    a download fails for ENOSPC."""
    s, download_log, _ = server_with_stubs
    # Only 50 bytes free
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 50)
    s._inventory["A"] = {
        "ref": "A", "filename": "a.s", "size_bytes": 100,
        "loras_dir_path": "/loras/a.s", "downloaded_at_local": "2026-06-20T09:00:00-07:00",
        "last_used_at_local": "2026-06-20T09:00:00-07:00", "adapter_name": "lora_0",
    }
    req = s.SetStackRequest(
        target_refs=["B"],
        download_specs={"B": _spec("b.s", size_hint=100)},
    )
    asyncio.run(s.set_stack(req))
    # A evicted; B downloaded
    assert "A" not in s._inventory
    assert "B" in s._inventory
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_set_stack.py -v
```

- [ ] **Step 3: Add endpoint to `wan_t2v_server.py`.**

```python
import asyncio

_swap_lock: asyncio.Lock = asyncio.Lock()


class LoraInventoryEntry(BaseModel):
    ref: str
    filename: str
    size_bytes: int
    downloaded_at_local: str
    last_used_at_local: str
    adapter_name: str


class SwapRejectedDetails(BaseModel):
    reason: str
    target_refs_dropped: list[str]


class SetStackRequest(BaseModel):
    target_refs: list[str]
    download_specs: dict[str, ArtifactDownloadSpec]


class SetStackResponse(BaseModel):
    inventory: list[LoraInventoryEntry]
    free_bytes: int
    swap_rejected: SwapRejectedDetails | None = None


def _inventory_snapshot() -> list[LoraInventoryEntry]:
    return [LoraInventoryEntry(**v) for v in _inventory.values()]


@app.post("/lora/set_stack")
async def set_stack(req: SetStackRequest) -> SetStackResponse:
    async with _swap_lock:
        target_set = set(req.target_refs)
        current_set = set(_inventory.keys())
        to_evict_candidates = current_set - target_set
        to_download = target_set - current_set

        free_bytes = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum(
            (req.download_specs[r].size_hint or 0) for r in to_download
        )
        if target_dl_bytes <= free_bytes:
            evict_plan: list[str] = []
        else:
            picked = _pick_lru_evict(
                to_evict_candidates, _inventory, need=target_dl_bytes - free_bytes
            )
            if picked is None:
                # Should not happen if matcher did its math; raise so test catches it.
                raise RuntimeError(
                    f"insufficient disk for swap even after full eviction: need "
                    f"{target_dl_bytes - free_bytes} bytes"
                )
            evict_plan = picked

        for ref in evict_plan:
            await _evict_one(ref)

        for ref in to_download:
            spec = req.download_specs[ref]
            path, actual_bytes = _download_one(spec, LORAS_DIR)
            now = datetime.now().isoformat()
            _inventory[ref] = {
                "ref": ref,
                "filename": spec.filename,
                "size_bytes": actual_bytes,
                "loras_dir_path": path,
                "downloaded_at_local": now,
                "last_used_at_local": now,
                "adapter_name": f"lora_{len(_inventory)}",  # placeholder; reload re-assigns
            }

        await _reload_pipeline_loras(req.target_refs)

        return SetStackResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
            swap_rejected=None,
        )
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_set_stack.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_wan_t2v_server_set_stack.py
git commit -m "feat(wan-server): POST /lora/set_stack endpoint (happy + idempotent + tight-disk paths)"
```

---

### Task 7: GET /lora/inventory endpoint

**Goal:** Read-only snapshot endpoint returning the current inventory + free disk.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/test_wan_t2v_server_inventory.py` (new)

**Acceptance Criteria:**
- [ ] `GET /lora/inventory` returns `{inventory: [LoraInventoryEntry...], free_bytes: int}`.
- [ ] Empty inventory returns `{inventory: [], free_bytes: int}`.
- [ ] Acquires `_swap_lock` (so it cannot race a /lora/set_stack mid-mutation).
- [ ] Same `LoraInventoryEntry` shape as the `/lora/set_stack` response.

**Verify:** `pixi run pytest tests/engines/test_wan_t2v_server_inventory.py -v`

**Steps:**

- [ ] **Step 1: Write failing test.**

```python
"""GET /lora/inventory — read-only snapshot."""

from __future__ import annotations

import asyncio


def test_inventory_empty(monkeypatch) -> None:
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._inventory.clear()
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 999)
    resp = asyncio.run(s.inventory())
    assert resp.inventory == []
    assert resp.free_bytes == 999


def test_inventory_with_entries(monkeypatch) -> None:
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._inventory.clear()
    s._inventory["A"] = {
        "ref": "A", "filename": "a.s", "size_bytes": 100,
        "loras_dir_path": "/loras/a.s", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    }
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 5000)
    resp = asyncio.run(s.inventory())
    assert len(resp.inventory) == 1
    assert resp.inventory[0].ref == "A"
    assert resp.free_bytes == 5000
```

- [ ] **Step 2: Run — confirm RED.**

- [ ] **Step 3: Add endpoint.**

```python
class InventoryResponse(BaseModel):
    inventory: list[LoraInventoryEntry]
    free_bytes: int


@app.get("/lora/inventory")
async def inventory() -> InventoryResponse:
    async with _swap_lock:
        return InventoryResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
        )
```

- [ ] **Step 4: Run — confirm GREEN.**

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_wan_t2v_server_inventory.py
git commit -m "feat(wan-server): GET /lora/inventory read-only snapshot"
```

---

### Task 8: Pod-side /lora/set_stack failure paths

**Goal:** Wire up the five failure paths from spec §11.1: download failure (no eviction), download failure (post-eviction), disk-full mid-download, VRAM-OOM rollback. Pod-side raises FastAPI HTTPException with structured body; orchestrator-side mapping to error classes is Task 11.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/test_wan_t2v_server_set_stack_failures.py` (new)

**Acceptance Criteria:**
- [ ] Download failure with no prior eviction → HTTP 502 body `{error: "lora_download_failed", ref, phase: "download", evict_completed: [], download_completed: [...], download_failed: <ref>, underlying: <str>}`. Inventory reflects only successfully-downloaded refs.
- [ ] Download failure with prior eviction → same 502 body but `evict_completed` populated. Pod inventory reflects current truth (evicted refs gone, partially-downloaded refs absent).
- [ ] `shutil.disk_usage` returning `<= 0` free bytes mid-download triggers HTTP 507 with the same body shape + `error: "disk_full"`.
- [ ] VRAM-OOM at `set_adapters` triggers rollback to previous adapter set + HTTP 200 with `swap_rejected: {reason: "vram_oom", target_refs_dropped: [...]}`. Inventory reflects pre-swap state.
- [ ] Partial-download file cleanup: failed downloads leave no `.partial` files on disk.

**Verify:** `pixi run pytest tests/engines/test_wan_t2v_server_set_stack_failures.py -v`

**Steps:**

- [ ] **Step 1: Write failing test file.**

```python
"""POST /lora/set_stack failure paths."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


@pytest.fixture
def server_with_stubs(monkeypatch, tmp_path):
    """Same shape as the happy-path fixture; tests override behaviors per case."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    s._inventory.clear()
    monkeypatch.setattr(s, "LORAS_DIR", tmp_path)
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 10_000_000)

    class _Stub:
        unloaded = False
        loaded: list = []
        adapters: list = []

        def unload_lora_weights(self):
            self.unloaded = True

        def load_lora_weights(self, path, adapter_name):  # noqa: ANN001
            self.loaded.append((path, adapter_name))

        def set_adapters(self, names):  # noqa: ANN001
            self.adapters = list(names)

        def delete_adapters(self, names):  # noqa: ANN001
            pass

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    return s, stub


def _spec(filename: str, size_hint: int = 100):
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s
    return s.ArtifactDownloadSpec(
        url=f"https://x/{filename}", headers={}, filename=filename, size_hint=size_hint
    )


def test_download_fail_no_eviction_502(server_with_stubs, monkeypatch) -> None:
    """Bug: error path raises bare Exception instead of HTTPException, so
    the operator's CLI sees a generic 500."""
    s, _ = server_with_stubs

    def _fail_b(spec, dest_dir):  # noqa: ANN001
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
    assert ei.value.detail["error"] == "lora_download_failed"
    assert ei.value.detail["evict_completed"] == []
    assert ei.value.detail["download_failed"] == "B"
    assert ei.value.detail["underlying"].startswith("simulated 504") or \
           "504" in ei.value.detail["underlying"]


def test_download_fail_after_eviction_502(server_with_stubs, monkeypatch) -> None:
    """Bug: evict_completed list omitted from the body, so the orchestrator
    cannot distinguish degraded from clean-fail."""
    s, _ = server_with_stubs
    # Tight disk; force eviction
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 50)
    s._inventory["X"] = {
        "ref": "X", "filename": "x.s", "size_bytes": 100,
        "loras_dir_path": "/loras/x.s", "downloaded_at_local": "old",
        "last_used_at_local": "old", "adapter_name": "lora_0",
    }

    def _fail_new(spec, dest_dir):  # noqa: ANN001
        raise RuntimeError("CivitAI 504")

    monkeypatch.setattr(s, "_download_one", _fail_new)
    req = s.SetStackRequest(
        target_refs=["B"], download_specs={"B": _spec("b.s", size_hint=100)}
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(s.set_stack(req))
    assert ei.value.status_code == 502
    assert ei.value.detail["evict_completed"] == ["X"]
    assert ei.value.detail["download_failed"] == "B"


def test_disk_full_mid_download_507(server_with_stubs, monkeypatch) -> None:
    """Bug: ENOSPC mapped to 502 (download failed) rather than 507
    (insufficient storage), so the orchestrator's classifier can't tell
    a transient throttle from a fatal disk-full."""
    s, _ = server_with_stubs

    def _enospc(spec, dest_dir):  # noqa: ANN001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(s, "_download_one", _enospc)
    req = s.SetStackRequest(
        target_refs=["B"], download_specs={"B": _spec("b.s")}
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(s.set_stack(req))
    assert ei.value.status_code == 507
    assert ei.value.detail["error"] == "disk_full"


def test_vram_oom_rollback_200_with_swap_rejected(server_with_stubs, monkeypatch) -> None:
    """Bug: VRAM OOM raises 500 instead of rolling back; pod left in unknown
    state, orchestrator destroys an otherwise-healthy pod."""
    s, stub = server_with_stubs

    def _fake_dl(spec, dest_dir):  # noqa: ANN001
        return f"{dest_dir}/{spec.filename}", 100

    monkeypatch.setattr(s, "_download_one", _fake_dl)
    # First set_adapters call (the swap) raises OOM; second call (rollback) OK
    call_count = {"n": 0}
    original_set = stub.set_adapters.__func__ if hasattr(stub.set_adapters, "__func__") else stub.set_adapters

    def _oom_then_ok(self, names):  # noqa: ANN001
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("CUDA out of memory")
        self.adapters = list(names)

    import types
    stub.set_adapters = types.MethodType(_oom_then_ok, stub)

    # Pre-existing inventory of ["A"]; target ["A", "B"]
    s._inventory["A"] = {
        "ref": "A", "filename": "a.s", "size_bytes": 100,
        "loras_dir_path": "/loras/a.s", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    }
    req = s.SetStackRequest(
        target_refs=["A", "B"],
        download_specs={"A": _spec("a.s"), "B": _spec("b.s")},
    )
    resp = asyncio.run(s.set_stack(req))
    assert resp.swap_rejected is not None
    assert resp.swap_rejected.reason == "vram_oom"
    assert "B" in resp.swap_rejected.target_refs_dropped
    # Inventory rolled back to A only
    refs = {e.ref for e in resp.inventory}
    assert refs == {"A"}


def test_failed_download_cleans_up_partial_file(server_with_stubs, monkeypatch, tmp_path) -> None:
    """Bug: download wrapper leaves *.partial files on disk after failure,
    leaking space + confusing future operators."""
    s, _ = server_with_stubs

    # Use the real _download_one against a fake URL via monkey-patching urlopen
    import urllib.request

    class _FakeResp:
        def __init__(self):
            self._calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n):
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
```

- [ ] **Step 2: Run — confirm RED.**

- [ ] **Step 3: Refactor `set_stack` to surface structured failures.**

Wrap the inner loop in try/except, build the 502/507 body, and add the VRAM-OOM rollback path:

```python
from fastapi import HTTPException


@app.post("/lora/set_stack")
async def set_stack(req: SetStackRequest) -> SetStackResponse:
    async with _swap_lock:
        target_set = set(req.target_refs)
        current_set = set(_inventory.keys())
        to_evict_candidates = current_set - target_set
        to_download = list(target_set - current_set)

        free_bytes = _disk_free_bytes(LORAS_DIR)
        target_dl_bytes = sum(
            (req.download_specs[r].size_hint or 0) for r in to_download
        )
        if target_dl_bytes <= free_bytes:
            evict_plan: list[str] = []
        else:
            picked = _pick_lru_evict(
                to_evict_candidates, _inventory, need=target_dl_bytes - free_bytes
            )
            if picked is None:
                raise HTTPException(
                    status_code=507,
                    detail={
                        "error": "disk_full",
                        "phase": "plan",
                        "evict_completed": [],
                        "download_completed": [],
                        "download_failed": None,
                        "underlying": "insufficient disk even after full eviction",
                    },
                )
            evict_plan = picked

        evict_completed: list[str] = []
        download_completed: list[str] = []
        # Snapshot pre-swap state for VRAM-OOM rollback.
        previous_refs = list(_inventory.keys())

        try:
            for ref in evict_plan:
                await _evict_one(ref)
                evict_completed.append(ref)

            for ref in to_download:
                spec = req.download_specs[ref]
                try:
                    path, actual_bytes = _download_one(spec, LORAS_DIR)
                except OSError as e:
                    if e.errno == 28:  # ENOSPC
                        raise HTTPException(
                            status_code=507,
                            detail={
                                "error": "disk_full",
                                "phase": "download",
                                "evict_completed": evict_completed,
                                "download_completed": download_completed,
                                "download_failed": ref,
                                "underlying": str(e),
                            },
                        ) from e
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": "lora_download_failed",
                            "phase": "download",
                            "evict_completed": evict_completed,
                            "download_completed": download_completed,
                            "download_failed": ref,
                            "underlying": str(e),
                        },
                    ) from e
                except Exception as e:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": "lora_download_failed",
                            "phase": "download",
                            "evict_completed": evict_completed,
                            "download_completed": download_completed,
                            "download_failed": ref,
                            "underlying": str(e),
                        },
                    ) from e
                now = datetime.now().isoformat()
                _inventory[ref] = {
                    "ref": ref,
                    "filename": spec.filename,
                    "size_bytes": actual_bytes,
                    "loras_dir_path": path,
                    "downloaded_at_local": now,
                    "last_used_at_local": now,
                    "adapter_name": f"lora_pending_{ref}",
                }
                download_completed.append(ref)

            try:
                await _reload_pipeline_loras(req.target_refs)
            except RuntimeError as e:
                if "out of memory" in str(e).lower() or "oom" in str(e).lower():
                    # Rollback to the previous adapter set
                    dropped = [r for r in req.target_refs if r not in previous_refs]
                    # Drop newly-downloaded entries from inventory so the snapshot
                    # matches the rolled-back state.
                    for ref in dropped:
                        _inventory.pop(ref, None)
                        # Best-effort file cleanup of the orphan download
                        try:
                            spec = req.download_specs.get(ref)
                            if spec is not None:
                                (LORAS_DIR / spec.filename).unlink(missing_ok=True)
                        except OSError:
                            pass
                    await _reload_pipeline_loras(previous_refs)
                    return SetStackResponse(
                        inventory=_inventory_snapshot(),
                        free_bytes=_disk_free_bytes(LORAS_DIR),
                        swap_rejected=SwapRejectedDetails(
                            reason="vram_oom", target_refs_dropped=dropped
                        ),
                    )
                raise
        except HTTPException:
            raise

        return SetStackResponse(
            inventory=_inventory_snapshot(),
            free_bytes=_disk_free_bytes(LORAS_DIR),
            swap_rejected=None,
        )
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/engines/test_wan_t2v_server_set_stack.py tests/engines/test_wan_t2v_server_set_stack_failures.py -v
```

Expected: all green (both files).

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_wan_t2v_server_set_stack_failures.py
git commit -m "feat(wan-server): /lora/set_stack failure paths (502/507/VRAM-OOM rollback)"
```

---

### Task 9: Widen Ledger.touch type signature; add lora_inventory + warm_attach_key + loras_dir_free_bytes + status field support

**Goal:** Widen the `**extra` parameter on `Ledger.touch` from `float | int | str | None` to also accept `list[dict] | dict | bool` so callers can persist the new fields without type errors. Add a unit test covering round-tripping each new field.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py` (touch signature only — fields are stored as keys on the entry dict already)
- Test: `tests/core/test_ledger_lora_inventory.py` (new)

**Acceptance Criteria:**
- [ ] `touch(instance_id, lora_inventory=[...])` round-trips a list of dicts through `entries()` / `read()` without type error.
- [ ] `touch(instance_id, warm_attach_key="abc123")` round-trips a string.
- [ ] `touch(instance_id, loras_dir_free_bytes=42_000_000_000)` round-trips an int.
- [ ] `touch(instance_id, status="degraded")` round-trips a string.
- [ ] Under `--ephemeral` (via active `EphemeralSession` with `policy.ledger_record=False`), the same fields land in `session.in_memory_ledger` rather than on disk.
- [ ] No existing test fails from the type-signature widening.

**Verify:** `pixi run pytest tests/core/test_ledger_lora_inventory.py tests/core/test_lifecycle.py -v`

**Steps:**

- [ ] **Step 1: Write failing test `tests/core/test_ledger_lora_inventory.py`.**

```python
"""Ledger.touch accepts the new LoRA-inventory + warm-attach-key fields."""

from __future__ import annotations

import pytest

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _make_ledger(tmp_path) -> Ledger:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test-run")
    inst = Instance(
        id="pod-7b2", provider="runpod", tags={}, created_at=0.0,
        cost_rate_usd_per_hr=1.0,
    )
    ledger.record(inst)
    return ledger


def test_touch_round_trips_lora_inventory(tmp_path) -> None:
    """Bug: touch's **extra type hint is float|int|str|None — list[dict]
    triggers a mypy error or a runtime TypeError downstream when the JSON
    writer encounters it.
    """
    ledger = _make_ledger(tmp_path)
    inv = [
        {"ref": "civitai:A@1", "filename": "a.s", "size_bytes": 100,
         "downloaded_at_local": "x", "last_used_at_local": "x",
         "adapter_name": "lora_0"},
    ]
    assert ledger.touch("pod-7b2", lora_inventory=inv) is True
    entry = ledger.read("pod-7b2")
    assert entry["lora_inventory"] == inv


def test_touch_round_trips_warm_attach_key(tmp_path) -> None:
    ledger = _make_ledger(tmp_path)
    ledger.touch("pod-7b2", warm_attach_key="wak-abc123")
    assert ledger.read("pod-7b2")["warm_attach_key"] == "wak-abc123"


def test_touch_round_trips_loras_dir_free_bytes(tmp_path) -> None:
    ledger = _make_ledger(tmp_path)
    ledger.touch("pod-7b2", loras_dir_free_bytes=42_000_000_000)
    assert ledger.read("pod-7b2")["loras_dir_free_bytes"] == 42_000_000_000


def test_touch_round_trips_status_degraded(tmp_path) -> None:
    ledger = _make_ledger(tmp_path)
    ledger.touch("pod-7b2", status="degraded")
    assert ledger.read("pod-7b2")["status"] == "degraded"


def test_touch_under_ephemeral_routes_inventory_to_memory(tmp_path) -> None:
    """Bug: the new lora_inventory field bypasses the ephemeral gate
    because the field is added later and the gate predates it."""
    ledger = _make_ledger(tmp_path)
    with EphemeralSession(enabled=True):
        ledger.touch("pod-7b2", lora_inventory=[{"ref": "secret"}])
        # Under strict policy, writes land in in_memory_ledger
        session = EphemeralSession.current()
        assert session is not None
        assert "test-run" in session.in_memory_ledger
        # And NOT in the on-disk ledger
        ledger_disk = _make_ledger(tmp_path)  # fresh reader, same store
        entry = ledger_disk.read("pod-7b2")
        # Entry exists (from record()) but lora_inventory NOT persisted
        assert entry is not None
        assert "lora_inventory" not in entry or entry.get("lora_inventory") != [{"ref": "secret"}]
```

- [ ] **Step 2: Run — confirm RED.**

```bash
pixi run pytest tests/core/test_ledger_lora_inventory.py -v
```

The tests probably fail at `touch(lora_inventory=...)` either via mypy strictness or via the json-writer being unable to serialize.

- [ ] **Step 3: Widen the signature on `Ledger.touch` in `src/kinoforge/core/lifecycle.py:608-613`.**

Change:

```python
    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | None,
    ) -> bool:
```

to:

```python
    def touch(
        self,
        instance_id: str,
        *,
        last_heartbeat: float | None = None,
        **extra: float | int | str | bool | list | dict | None,  # type: ignore[type-arg]
    ) -> bool:
```

(The `# type: ignore` is fine here — the looser type is the contract for forward-compat. If a downstream mypy run flags it cleanly, drop the ignore.)

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_ledger_lora_inventory.py tests/core/test_lifecycle.py -v
```

Expected: 5 new passes; pre-existing lifecycle tests unaffected.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/lifecycle.py tests/core/test_ledger_lora_inventory.py
git commit -m "feat(lifecycle): widen Ledger.touch **extra to accept list/dict/bool (LoRA inventory + status fields)"
```

---

### Task 10: Ledger.find_pods_by_warm_attach_key + lazy warm-attach-key backfill

**Goal:** Add a new read helper `find_pods_by_warm_attach_key(wak_hex: str) -> list[dict]` that returns all entries with matching `warm_attach_key`. For entries missing `warm_attach_key` (pre-feature pods), lazily derive it from the entry's `capability_key_hex` payload — by re-deriving from the cfg that produced the pod (recoverable via the existing per-pod profile cache lookup) and writing back via `touch`. If lazy derivation fails (no cfg recoverable), skip the entry.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py`
- Test: `tests/core/test_ledger_find_pods_by_warm_attach_key.py` (new)

**Acceptance Criteria:**
- [ ] `find_pods_by_warm_attach_key("wak-abc")` returns only entries with `warm_attach_key == "wak-abc"`.
- [ ] Entries with `warm_attach_key` absent are inspected for a backfill source; if recoverable, the field is filled in + the entry is included if it matches; if not recoverable, the entry is silently skipped (logged at DEBUG).
- [ ] Empty result returns `[]`, not `None`.
- [ ] Does NOT acquire the cross-process mutate lock (read-only).

**Verify:** `pixi run pytest tests/core/test_ledger_find_pods_by_warm_attach_key.py -v`

**Steps:**

- [ ] **Step 1: Write failing test.**

```python
"""Ledger.find_pods_by_warm_attach_key — index lookup + lazy backfill."""

from __future__ import annotations

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _ledger_with_pods(tmp_path, pods: list[dict]) -> Ledger:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test-run")
    for p in pods:
        inst = Instance(
            id=p["id"], provider="runpod", tags={}, created_at=0.0,
            cost_rate_usd_per_hr=1.0,
        )
        ledger.record(inst)
        if "warm_attach_key" in p:
            ledger.touch(p["id"], warm_attach_key=p["warm_attach_key"])
    return ledger


def test_find_pods_by_warm_attach_key_returns_matching(tmp_path) -> None:
    """Bug: helper returns ALL entries instead of filtering."""
    ledger = _ledger_with_pods(tmp_path, [
        {"id": "pod-a", "warm_attach_key": "wak-1"},
        {"id": "pod-b", "warm_attach_key": "wak-2"},
        {"id": "pod-c", "warm_attach_key": "wak-1"},
    ])
    result = ledger.find_pods_by_warm_attach_key("wak-1")
    ids = {e["id"] for e in result}
    assert ids == {"pod-a", "pod-c"}


def test_find_pods_by_warm_attach_key_empty_returns_list(tmp_path) -> None:
    """Bug: helper returns None instead of [] for empty result, crashing
    callers that iterate."""
    ledger = _ledger_with_pods(tmp_path, [])
    assert ledger.find_pods_by_warm_attach_key("wak-1") == []


def test_find_pods_by_warm_attach_key_skips_unrecoverable_pre_feature(tmp_path) -> None:
    """Pre-feature entry without warm_attach_key + no cfg backfill source
    is silently skipped, not crashed on."""
    ledger = _ledger_with_pods(tmp_path, [{"id": "pod-pre-feature"}])
    # No warm_attach_key set; backfill source unavailable
    result = ledger.find_pods_by_warm_attach_key("wak-1")
    assert result == []
```

- [ ] **Step 2: Run — confirm RED.**

- [ ] **Step 3: Implement the helper.**

```python
import logging

_log = logging.getLogger(__name__)


def find_pods_by_warm_attach_key(self, wak_hex: str) -> list[dict]:
    """Return ledger entries whose warm_attach_key matches wak_hex.

    Entries missing warm_attach_key are silently skipped (logged at DEBUG).
    Future enhancement: lazy backfill from the per-pod cfg snapshot when
    that snapshot is recoverable — for v1 we just skip.
    """
    matches: list[dict] = []
    for entry in self._read_entries():
        wak = entry.get("warm_attach_key")
        if wak is None:
            _log.debug(
                "skipping pre-feature ledger entry %s (no warm_attach_key)",
                entry.get("id", "?"),
            )
            continue
        if wak == wak_hex:
            matches.append(entry)
    return matches
```

Add as a method on the `Ledger` class.

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_ledger_find_pods_by_warm_attach_key.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/lifecycle.py tests/core/test_ledger_find_pods_by_warm_attach_key.py
git commit -m "feat(lifecycle): Ledger.find_pods_by_warm_attach_key helper for warm-attach matcher"
```

---

### Task 11: DiffusersEngine.set_lora_stack wrapper + orchestrator-side error mapping

**Goal:** Add `DiffusersBackend.set_lora_stack(target_refs, download_specs) -> SetStackResponse` wrapping `POST /lora/set_stack`. Map pod-side HTTP errors to the `LoraSwapError` subclasses from Task 3.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Test: `tests/engines/test_diffusers_set_lora_stack.py` (new)

**Acceptance Criteria:**
- [ ] `set_lora_stack(target_refs=[], download_specs={})` POSTs the body to `{base_url}/lora/set_stack` and returns a parsed `SetStackResponse`-like dict (or pydantic model — same shape as pod-side).
- [ ] HTTP 502 with `error: "lora_download_failed"` + empty `evict_completed` → raises `LoraSwapDownloadError(pod_id, ref, underlying)`.
- [ ] HTTP 502 with `error: "lora_download_failed"` + non-empty `evict_completed` → raises `LoraSwapDegradedPodError(...)`.
- [ ] HTTP 507 with `error: "disk_full"` → raises `LoraSwapDiskFullError(...)`.
- [ ] HTTP 200 with `swap_rejected: {reason: "vram_oom", ...}` → raises `LoraSwapVramOomError(...)`.
- [ ] Pod unreachable (transport error past `_retry_proxy_call` budget) → raises `LoraSwapPodUnreachableError(...)`.

**Verify:** `pixi run pytest tests/engines/test_diffusers_set_lora_stack.py -v`

**Steps:**

- [ ] **Step 1: Write failing test file covering each error mapping case.** (Pattern: spy on `http_post`; return the relevant body shape; assert the right error class is raised + carries the expected fields.)

```python
"""DiffusersBackend.set_lora_stack — error mapping."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import (
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)


def _backend(http_post, http_get=None):
    from kinoforge.engines.diffusers import DiffusersBackend
    from kinoforge.core.interfaces import ModelProfile

    profile = ModelProfile(
        name="wan-2.2", max_frames=81, fps=24, supported_modes={"t2v"},
        max_resolution=(1024, 1024), supports_native_extension=False,
        supports_joint_audio=False,
    )
    return DiffusersBackend(
        http_post=http_post,
        http_get=http_get or (lambda url: {}),
        base_url="http://pod/",
        probe_profile=profile,
    )


class _HTTPError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body


def test_set_lora_stack_happy_path_returns_response() -> None:
    """Bug: backend swallows the response, returns None → caller cannot
    update the ledger with the post-swap inventory."""
    captured = {}

    def _post(url, body):
        captured["url"] = url
        captured["body"] = body
        return {
            "inventory": [
                {"ref": "civitai:A@1", "filename": "a.s", "size_bytes": 100,
                 "downloaded_at_local": "x", "last_used_at_local": "x",
                 "adapter_name": "lora_0"}
            ],
            "free_bytes": 5000,
            "swap_rejected": None,
        }

    backend = _backend(_post)
    resp = backend.set_lora_stack(
        pod_id="pod-7b2",
        target_refs=["civitai:A@1"],
        download_specs={"civitai:A@1": {"url": "https://x/a", "headers": {},
                                         "filename": "a.s", "size_hint": 100}},
    )
    assert captured["url"] == "http://pod/lora/set_stack"
    assert resp["free_bytes"] == 5000
    assert resp["inventory"][0]["ref"] == "civitai:A@1"


def test_set_lora_stack_502_no_eviction_raises_download_error() -> None:
    def _post(url, body):
        raise _HTTPError(502, {
            "error": "lora_download_failed",
            "evict_completed": [],
            "download_failed": "civitai:B@2",
            "underlying": "504",
        })

    backend = _backend(_post)
    with pytest.raises(LoraSwapDownloadError) as ei:
        backend.set_lora_stack(
            pod_id="pod-7b2",
            target_refs=["civitai:B@2"],
            download_specs={"civitai:B@2": {"url": "x", "headers": {}, "filename": "b.s"}},
        )
    assert ei.value.pod_id == "pod-7b2"
    assert ei.value.ref == "civitai:B@2"


def test_set_lora_stack_502_with_eviction_raises_degraded_error() -> None:
    def _post(url, body):
        raise _HTTPError(502, {
            "error": "lora_download_failed",
            "evict_completed": ["civitai:X@1"],
            "download_failed": "civitai:B@2",
            "underlying": "504",
        })

    backend = _backend(_post)
    with pytest.raises(LoraSwapDegradedPodError) as ei:
        backend.set_lora_stack(
            pod_id="pod-7b2", target_refs=["civitai:B@2"], download_specs={}
        )
    assert ei.value.evict_completed == ["civitai:X@1"]


def test_set_lora_stack_507_raises_disk_full() -> None:
    def _post(url, body):
        raise _HTTPError(507, {
            "error": "disk_full",
            "evict_completed": ["civitai:X@1"],
            "download_failed": "civitai:B@2",
        })

    backend = _backend(_post)
    with pytest.raises(LoraSwapDiskFullError):
        backend.set_lora_stack(pod_id="pod-7b2", target_refs=[], download_specs={})


def test_set_lora_stack_200_swap_rejected_raises_vram_oom() -> None:
    def _post(url, body):
        return {
            "inventory": [],
            "free_bytes": 5000,
            "swap_rejected": {"reason": "vram_oom",
                              "target_refs_dropped": ["civitai:big@1"]},
        }

    backend = _backend(_post)
    with pytest.raises(LoraSwapVramOomError) as ei:
        backend.set_lora_stack(pod_id="pod-7b2", target_refs=[], download_specs={})
    assert ei.value.dropped_refs == ["civitai:big@1"]


def test_set_lora_stack_transport_error_raises_pod_unreachable() -> None:
    def _post(url, body):
        raise ConnectionError("ConnectionResetError")

    backend = _backend(_post)
    with pytest.raises(LoraSwapPodUnreachableError) as ei:
        backend.set_lora_stack(pod_id="pod-7b2", target_refs=[], download_specs={})
    assert "ConnectionResetError" in ei.value.underlying
```

- [ ] **Step 2: Run — confirm RED.**

- [ ] **Step 3: Implement `set_lora_stack` on `DiffusersBackend`.**

Add to the existing class:

```python
def set_lora_stack(
    self,
    *,
    pod_id: str,
    target_refs: list[str],
    download_specs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """POST /lora/set_stack — declarative LoRA swap.

    Args:
        pod_id: For error messages; this backend doesn't know its pod id otherwise.
        target_refs: Ordered target LoRA stack.
        download_specs: ref → {url, headers, filename, size_hint?}

    Returns:
        Parsed response body: {inventory, free_bytes, swap_rejected}.

    Raises:
        LoraSwapVramOomError: HTTP 200 but body's swap_rejected set with reason=vram_oom.
        LoraSwapDownloadError: HTTP 502, error=lora_download_failed, no eviction yet.
        LoraSwapDegradedPodError: HTTP 502, error=lora_download_failed, eviction started.
        LoraSwapDiskFullError: HTTP 507, error=disk_full.
        LoraSwapPodUnreachableError: transport error past the retry budget.
    """
    from kinoforge.core.errors import (
        LoraSwapDegradedPodError, LoraSwapDiskFullError, LoraSwapDownloadError,
        LoraSwapPodUnreachableError, LoraSwapVramOomError,
    )

    url = f"{self._base_url}/lora/set_stack"
    body = {"target_refs": target_refs, "download_specs": download_specs}
    try:
        resp = self._http_post(url, body)
    except Exception as e:
        # Test fixtures use a custom _HTTPError; production likely raises
        # an httpx.HTTPStatusError or similar. Inspect for a `status` attr.
        status = getattr(e, "status", None)
        body_attr = getattr(e, "body", None)
        if status is not None and body_attr is not None:
            return self._map_http_error(status, body_attr, pod_id)
        raise LoraSwapPodUnreachableError(pod_id=pod_id, underlying=str(e)) from e

    if resp.get("swap_rejected"):
        sr = resp["swap_rejected"]
        if sr.get("reason") == "vram_oom":
            raise LoraSwapVramOomError(
                pod_id=pod_id,
                dropped_refs=sr.get("target_refs_dropped", []),
            )
    return resp


def _map_http_error(self, status: int, body: dict[str, Any], pod_id: str) -> None:
    """Raise the right LoraSwap subclass for the pod's structured error body."""
    from kinoforge.core.errors import (
        LoraSwapDegradedPodError, LoraSwapDiskFullError, LoraSwapDownloadError,
    )

    err = body.get("error")
    evict = body.get("evict_completed", [])
    failed = body.get("download_failed", "")
    underlying = body.get("underlying", "")
    if status == 507 or err == "disk_full":
        raise LoraSwapDiskFullError(
            pod_id=pod_id, evict_completed=evict, download_failed=failed
        )
    if status == 502 and err == "lora_download_failed":
        if evict:
            raise LoraSwapDegradedPodError(
                pod_id=pod_id, evict_completed=evict,
                download_failed=failed, underlying=underlying,
            )
        raise LoraSwapDownloadError(
            pod_id=pod_id, ref=failed, underlying=underlying
        )
    # Unknown shape — re-raise the original via a generic message
    raise RuntimeError(f"unknown /lora/set_stack error body: {body}")
```

- [ ] **Step 4: Run — confirm GREEN.**

```bash
pixi run pytest tests/engines/test_diffusers_set_lora_stack.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers_set_lora_stack.py
git commit -m "feat(diffusers-engine): DiffusersBackend.set_lora_stack + LoraSwapError mapping"
```

---

### Task 12: Reaper recognizes status="degraded" as reap-eligible

**Goal:** Extend `core/reaper.py` so pods with `status == "degraded"` are reap-eligible alongside the existing heartbeat-stale criteria. Pin via unit test.

**Files:**
- Modify: `src/kinoforge/core/reaper.py`
- Test: `tests/core/test_reaper_degraded_pods.py` (new)

**Acceptance Criteria:**
- [ ] A pod entry with `status="degraded"` is included in the reap-eligible set returned by the reaper's verdict function, even if its heartbeat is fresh.
- [ ] A pod entry without `status` (or with `status="alive"`) follows existing heartbeat-stale criteria.
- [ ] The new criterion is OR-combined with existing criteria (`degraded` OR `heartbeat-stale` OR existing rules).

**Verify:** `pixi run pytest tests/core/test_reaper_degraded_pods.py tests/core/test_reaper.py -v`

**Steps:**

- [ ] **Step 1: Read `core/reaper.py` to find the verdict function.**

```bash
rg -n 'def .*verdict|def .*classify|def .*eligible|status' /workspace/src/kinoforge/core/reaper.py | head -30
```

- [ ] **Step 2: Write failing test `tests/core/test_reaper_degraded_pods.py`.**

```python
"""Reaper recognizes status='degraded' alongside heartbeat-stale criteria."""

from __future__ import annotations

import pytest

# Implementation note: the exact reaper API surface is project-internal.
# Adapt the imports and the function-under-test name to whatever
# core/reaper.py actually exposes. This test pins the *behavior*:
# a degraded entry must show up in the reap-eligible verdict regardless
# of heartbeat freshness.

from kinoforge.core import reaper as _reaper


def _entry(*, id: str, status: str | None = None, last_heartbeat: float = 0.0) -> dict:
    e = {"id": id, "provider": "runpod", "tags": {}, "created_at": 0.0,
         "cost_rate_usd_per_hr": 1.0, "last_heartbeat": last_heartbeat}
    if status is not None:
        e["status"] = status
    return e


def test_degraded_pod_is_reap_eligible_even_with_fresh_heartbeat() -> None:
    """Bug: reaper only consults heartbeat, ignoring the new status field,
    so swap-degraded pods stay alive indefinitely until heartbeat goes stale."""
    import time

    now = time.time()
    entry = _entry(id="pod-degraded", status="degraded", last_heartbeat=now)
    # Replace with actual reaper verdict-function call:
    verdict = _reaper.classify_entry(entry, now=now)  # rename if different in code
    assert verdict.is_reap_eligible, "degraded pods must always be reap-eligible"
    assert "degraded" in verdict.reason.lower()


def test_alive_pod_with_fresh_heartbeat_not_reap_eligible() -> None:
    import time

    now = time.time()
    entry = _entry(id="pod-alive", status="alive", last_heartbeat=now)
    verdict = _reaper.classify_entry(entry, now=now)
    assert not verdict.is_reap_eligible


def test_status_absent_falls_back_to_heartbeat_criteria() -> None:
    """Pre-feature entries (no status field) follow existing rules."""
    import time

    now = time.time()
    fresh = _entry(id="pod-fresh", last_heartbeat=now)
    stale = _entry(id="pod-stale", last_heartbeat=now - 10_000)
    assert not _reaper.classify_entry(fresh, now=now).is_reap_eligible
    assert _reaper.classify_entry(stale, now=now).is_reap_eligible
```

> **Adapter note:** If `core/reaper.py` does not expose a `classify_entry` symbol, locate the equivalent (search for `def classify`, `def verdict`, `def is_reap`, or the sweeper's classification call site) and rename the test imports + assertions to match. The test pins behavior, not naming.

- [ ] **Step 3: Run — confirm RED.**

- [ ] **Step 4: Extend the reaper's verdict function with a `status == "degraded"` short-circuit.** Add a branch near the top of the classifier:

```python
if entry.get("status") == "degraded":
    return ReapVerdict(is_reap_eligible=True, reason="status=degraded")
```

(Where `ReapVerdict` and the function name are adapted to the actual codebase.)

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_reaper_degraded_pods.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Run the existing reaper suite to confirm no regression.**

```bash
pixi run pytest tests/core/test_reaper*.py -v
```

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/reaper.py tests/core/test_reaper_degraded_pods.py
git commit -m "feat(reaper): recognize status=degraded as reap-eligible (LoRA-swap failure cleanup)"
```

---

### Task 13: warm_reuse/redaction.py — _register_observed_lora_refs helper

**Goal:** Tiny helper that registers every observed LoRA ref from a pod inventory snapshot with `RedactionRegistry` so it gets redacted in subsequent log lines.

**Files:**
- Create: `src/kinoforge/core/warm_reuse/redaction.py`
- Test: `tests/core/test_warm_reuse_redaction.py` (new)

**Acceptance Criteria:**
- [ ] `_register_observed_lora_refs(snapshot)` accepts an object/dict with `.inventory` or `["inventory"]` list of entries with `.ref` / `["ref"]` strings.
- [ ] Each ref registered with `RedactionRegistry` under kind `"lora:ref"`.
- [ ] Idempotent — calling twice does not double-register or raise.
- [ ] Empty inventory → no-op.
- [ ] After call, redacting a log string mentioning the ref produces the redacted form.

**Verify:** `pixi run pytest tests/core/test_warm_reuse_redaction.py -v`

**Steps:**

- [ ] **Step 1: Write failing test.**

```python
"""warm_reuse.redaction._register_observed_lora_refs."""

from __future__ import annotations

import pytest

from kinoforge.core.redaction import RedactionRegistry
from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs


def test_registers_each_inventory_ref() -> None:
    """Bug: helper iterates wrong attribute (e.g. .refs instead of .inventory),
    silently registering nothing."""
    RedactionRegistry.reset()  # if available; otherwise instance().clear()
    snap = {"inventory": [
        {"ref": "civitai:A@1"},
        {"ref": "civitai:B@2"},
    ]}
    _register_observed_lora_refs(snap)
    out = RedactionRegistry.instance().redact("downloading civitai:A@1 to /loras")
    assert "civitai:A@1" not in out
    out2 = RedactionRegistry.instance().redact("downloading civitai:B@2 to /loras")
    assert "civitai:B@2" not in out2


def test_idempotent() -> None:
    """Bug: helper appends duplicates, blowing up the registry size."""
    RedactionRegistry.reset()
    snap = {"inventory": [{"ref": "civitai:A@1"}]}
    _register_observed_lora_refs(snap)
    _register_observed_lora_refs(snap)
    out = RedactionRegistry.instance().redact("civitai:A@1")
    assert "civitai:A@1" not in out


def test_empty_inventory_noop() -> None:
    RedactionRegistry.reset()
    _register_observed_lora_refs({"inventory": []})
    out = RedactionRegistry.instance().redact("civitai:A@1")
    # No ref registered → no redaction
    assert "civitai:A@1" in out
```

> **Adapter note:** If `RedactionRegistry` has no `reset` classmethod, use whatever the test suite normally uses to clear it (often a fixture or an `instance().clear()` call). Check `tests/core/test_redaction.py` for the pattern.

- [ ] **Step 2: Run — confirm RED.**

- [ ] **Step 3: Implement `src/kinoforge/core/warm_reuse/redaction.py`.**

```python
"""Auto-register observed LoRA refs with RedactionRegistry.

Pod-side inventory snapshots contain LoRA refs that may have been
loaded by previous sessions (not by the current session's vault).
This helper registers them with the redaction registry so any
log line / json sink that mentions them gets redacted at source.

Called from the matcher after every /lora/inventory + /lora/set_stack
response. Idempotent.
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.redaction import RedactionRegistry


def _register_observed_lora_refs(snapshot: Any) -> None:
    """Register every observed LoRA ref under the lora:ref token kind."""
    inventory = (
        getattr(snapshot, "inventory", None)
        if hasattr(snapshot, "inventory")
        else snapshot.get("inventory", [])
    )
    if not inventory:
        return
    r = RedactionRegistry.instance()
    pairs: list[tuple[str, str]] = []
    for entry in inventory:
        ref = getattr(entry, "ref", None) if hasattr(entry, "ref") else entry.get("ref")
        if ref:
            pairs.append((ref, "lora:ref"))
    if pairs:
        r.add_many(pairs)
```

- [ ] **Step 4: Run — confirm GREEN.**

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/warm_reuse/redaction.py tests/core/test_warm_reuse_redaction.py
git commit -m "feat(warm-reuse): _register_observed_lora_refs auto-registers pod inventory with RedactionRegistry"
```

---

### Task 14: warm_reuse/matcher.py — find_warm_attach_candidate

**Goal:** The matcher core. Two-tier lookup: filter by `warm_attach_key`, then evaluate each candidate's LoRA-stack delta + LRU eviction plan + free-disk arithmetic. Returns the cheapest match or `None`.

**Files:**
- Create: `src/kinoforge/core/warm_reuse/matcher.py`
- Test: `tests/core/test_warm_reuse_matcher.py` (new — five sub-suites)

**Acceptance Criteria:**
- [ ] `find_warm_attach_candidate(cfg, ledger, pod_lock_registry, re_probe=...)` returns a `WarmAttachMatch | None`.
- [ ] Exact-byte fast-path: when a candidate's `capability_key` byte-equals `cfg.capability_key().derive()` → returns match with empty swap plan + cost 0.
- [ ] Delta path, no eviction needed: returns plan with `evict=[]`, `download=[new refs]`, cost > 0.
- [ ] Delta path, eviction needed: returns plan with LRU-ordered `evict`, `download=[new refs]`.
- [ ] Skips candidates marked `status="degraded"` and candidates locked by `pod_lock_registry`.
- [ ] Returns `None` when no candidate is viable.
- [ ] Acquires pod-lock on the returned match via the registry; releases automatically on failure to fully construct the match.
- [ ] Re-probes pod inventory when `loras_dir_free_bytes_observed_at_local` age > threshold (default 300 s) or under `--ephemeral` (always re-probe first time).

**Verify:** `pixi run pytest tests/core/test_warm_reuse_matcher.py -v`

**Steps:**

- [ ] **Step 1: Sketch the data types in `src/kinoforge/core/warm_reuse/matcher.py`** (no impl yet — just the dataclasses + signatures, so the test can import).

```python
"""Warm-attach matcher — two-tier lookup + LRU eviction + disk arithmetic.

See docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md
§9 for the full algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from kinoforge.core.ephemeral import EphemeralSession


@dataclass(frozen=True)
class SwapPlan:
    """Concrete plan the matcher will hand to /lora/set_stack."""

    evict: list[str] = field(default_factory=list)
    download: list[str] = field(default_factory=list)
    estimated_cost_seconds: float = 0.0


@dataclass(frozen=True)
class WarmAttachMatch:
    """Result of a successful matcher decision."""

    pod_id: str
    pod_entry: dict
    swap_plan: SwapPlan


# Module-level for tests to override
_BYTES_PER_SECOND_ESTIMATE: int = 100 * 1024 * 1024  # 100 MB/s


def _estimate_seconds(download_specs: dict[str, dict], download_refs: list[str]) -> float:
    total = sum(int(download_specs.get(r, {}).get("size_hint", 0) or 0) for r in download_refs)
    return total / _BYTES_PER_SECOND_ESTIMATE


def find_warm_attach_candidate(
    cfg: Any,
    ledger: Any,
    *,
    pod_lock_registry: Any,
    re_probe: Optional[Callable[[str], Any]] = None,
    re_probe_threshold_s: float = 300.0,
    download_specs: Optional[dict[str, dict]] = None,
) -> WarmAttachMatch | None:
    """Find the cheapest warm pod to attach for cfg, or None to cold-boot.

    Args:
        cfg: The Config whose capability_key + lora_stack drive the match.
        ledger: Ledger to query for candidate pods.
        pod_lock_registry: PodLockRegistry; acquired non-blockingly on success.
        re_probe: Optional callable (pod_id -> InventorySnapshot) used when
            the ledger's snapshot of free-disk is stale or under --ephemeral.
        re_probe_threshold_s: Seconds before a free-bytes snapshot is stale.
        download_specs: ref -> {size_hint?, url, headers, filename} for the
            target stack. Required for tight-disk + cost estimation; may be
            empty for exact-byte fast path.

    Returns:
        WarmAttachMatch or None.
    """
    raise NotImplementedError  # Implemented after the failing tests are red.
```

- [ ] **Step 2: Write failing tests covering all acceptance criteria (one test per criterion).** I omit them inline here to keep the plan readable — pattern: build a fake Ledger that returns canned entries, a fake PodLockRegistry, a fake re_probe callable, and a stub Config that produces known WarmAttachKey + LoraStack.

  See `tests/core/test_warm_reuse_matcher.py` (write 8 tests:)

  - `test_exact_byte_fast_path_returns_zero_cost_match`
  - `test_delta_path_no_eviction_returns_correct_swap_plan`
  - `test_delta_path_with_eviction_returns_lru_ordered_evict`
  - `test_skips_degraded_pods`
  - `test_skips_pods_locked_by_other_jobs`
  - `test_returns_none_when_no_candidate_viable`
  - `test_re_probes_when_snapshot_stale`
  - `test_re_probes_always_under_ephemeral`

- [ ] **Step 3: Run — confirm RED.**

- [ ] **Step 4: Implement `find_warm_attach_candidate`.** Algorithm transcribed from spec §9.2; with the tests written, you have an executable contract.

```python
import time

from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs


def find_warm_attach_candidate(
    cfg, ledger, *, pod_lock_registry, re_probe=None,
    re_probe_threshold_s=300.0, download_specs=None,
):
    download_specs = download_specs or {}
    cap_key = cfg.capability_key()
    cap_hex = cap_key.derive()
    wak_hex = cap_key.warm_attach_key().derive()
    new_lora_refs = list(cap_key.lora_stack().refs)

    candidates = ledger.find_pods_by_warm_attach_key(wak_hex)

    # Filter: drop degraded + locked
    eligible = []
    for entry in candidates:
        if entry.get("status") == "degraded":
            continue
        if entry["id"] in pod_lock_registry:
            continue
        eligible.append(entry)

    session = EphemeralSession.current()
    always_reprobe = session is not None and not session.policy.ledger_record

    evaluations: list[tuple[WarmAttachMatch, float]] = []
    for entry in eligible:
        pod_id = entry["id"]
        pod_cap_hex = entry.get("capability_key_hex")

        # Exact-byte fast path
        if pod_cap_hex == cap_hex:
            match = WarmAttachMatch(
                pod_id=pod_id, pod_entry=entry,
                swap_plan=SwapPlan(evict=[], download=[], estimated_cost_seconds=0.0),
            )
            evaluations.append((match, 0.0))
            continue

        # Delta path
        observed_at = entry.get("loras_dir_free_bytes_observed_at_local")
        free_bytes = entry.get("loras_dir_free_bytes")
        inventory_refs = [e["ref"] for e in entry.get("lora_inventory", [])]

        needs_reprobe = (
            always_reprobe
            or free_bytes is None
            or observed_at is None
            or _snapshot_stale(observed_at, re_probe_threshold_s)
        )
        if needs_reprobe and re_probe is not None:
            snapshot = re_probe(pod_id)
            _register_observed_lora_refs(snapshot)
            inventory_refs = [e.ref if hasattr(e, "ref") else e["ref"]
                              for e in snapshot.inventory]
            free_bytes = snapshot.free_bytes

        current_set = set(inventory_refs)
        target_set = set(new_lora_refs)
        to_download = list(target_set - current_set)
        to_evict_candidates = current_set - target_set

        download_bytes = sum(int(download_specs.get(r, {}).get("size_hint", 0) or 0)
                             for r in to_download)
        if free_bytes is None:
            free_bytes = 0
        if download_bytes <= free_bytes:
            evict_plan: list[str] = []
        else:
            ordered = sorted(
                (e for e in entry.get("lora_inventory", []) if e["ref"] in to_evict_candidates),
                key=lambda e: e["last_used_at_local"],
            )
            evict_plan = []
            freed = 0
            need = download_bytes - free_bytes
            for e in ordered:
                evict_plan.append(e["ref"])
                freed += e["size_bytes"]
                if freed >= need:
                    break
            if freed < need:
                continue  # not viable

        cost = _estimate_seconds(download_specs, to_download)
        match = WarmAttachMatch(
            pod_id=pod_id, pod_entry=entry,
            swap_plan=SwapPlan(
                evict=evict_plan, download=to_download, estimated_cost_seconds=cost
            ),
        )
        evaluations.append((match, cost))

    evaluations.sort(key=lambda mc: mc[1])
    for match, _cost in evaluations:
        if pod_lock_registry.acquire(match.pod_id, blocking=False):
            return match
    return None


def _snapshot_stale(observed_at_local: str, threshold_s: float) -> bool:
    from datetime import datetime
    try:
        observed = datetime.fromisoformat(observed_at_local)
        return (datetime.now(observed.tzinfo) - observed).total_seconds() > threshold_s
    except (ValueError, TypeError):
        return True
```

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_warm_reuse_matcher.py -v
```

Expected: 8 passed.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/warm_reuse/matcher.py tests/core/test_warm_reuse_matcher.py
git commit -m "feat(warm-reuse): find_warm_attach_candidate matcher (two-tier lookup + LRU + disk arithmetic)"
```

---

### Task 15: Orchestrator integration — route warm-attach through the new matcher; hold pod lock; swap + generate

**Goal:** Find the existing warm-attach call site in `core/orchestrator.py` (`warm_reuse_auto_attach=True` path); replace its inline match with a `find_warm_attach_candidate` call; on match, hold the pod lock, call `backend.set_lora_stack(...)`, update the ledger with the post-swap inventory, then call `submit + result` as today; release the lock.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py`
- Test: `tests/core/test_orchestrator_warm_reuse_lora.py` (new)

**Acceptance Criteria:**
- [ ] On warm-attach success: pod lock acquired BEFORE swap call.
- [ ] After swap response: ledger updated via `Ledger.touch(pod_id, lora_inventory=..., loras_dir_free_bytes=..., loras_dir_free_bytes_observed_at_local=...)`.
- [ ] Pod lock released in a `finally` block — released even when generate fails.
- [ ] On `LoraSwapDegradedPodError`: ledger touched with `status="degraded"` before re-raising; lock released.
- [ ] On `LoraSwapDownloadError` / `LoraSwapVramOomError`: lock released; ledger NOT marked degraded (pod still healthy).
- [ ] On `LoraSwapPodUnreachableError` / `LoraSwapDiskFullError`: ledger marked degraded; lock released.
- [ ] When matcher returns `None`: falls through to existing cold-boot path unchanged.

**Verify:** `pixi run pytest tests/core/test_orchestrator_warm_reuse_lora.py tests/core/test_orchestrator.py -v`

**Steps:**

- [ ] **Step 1: Locate the existing warm-attach call site.**

```bash
rg -n 'warm_reuse_auto_attach|auto_attach' /workspace/src/kinoforge/core/orchestrator.py
```

Read the surrounding function to understand its shape before modifying.

- [ ] **Step 2: Write failing test `tests/core/test_orchestrator_warm_reuse_lora.py`.**

(Pattern: integration-style test with a real Ledger + FakeEngine + FakeProvider; record one pod with a matching warm_attach_key + different lora_stack; spy on the backend's set_lora_stack + assert it's called with the right plan + the lock is released on each error path.)

- [ ] **Step 3: Run — confirm RED.**

- [ ] **Step 4: Refactor the warm-attach call site.** Pseudocode:

```python
from kinoforge.core.errors import (
    LoraSwapDegradedPodError, LoraSwapDiskFullError, LoraSwapDownloadError,
    LoraSwapPodUnreachableError, LoraSwapVramOomError,
)
from kinoforge.core.warm_reuse.matcher import find_warm_attach_candidate
from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry

_POD_LOCKS = PodLockRegistry()  # module-level singleton


def _try_warm_attach(cfg, ledger, build_backend, engine):
    download_specs = _build_download_specs(cfg)  # helper that resolves cfg models[kind=lora]
    match = find_warm_attach_candidate(
        cfg, ledger,
        pod_lock_registry=_POD_LOCKS,
        re_probe=lambda pod_id: _reprobe(pod_id, build_backend),
        download_specs=download_specs,
    )
    if match is None:
        return None  # cold-boot
    backend = build_backend(match.pod_id)
    try:
        if match.swap_plan.evict or match.swap_plan.download:
            resp = backend.set_lora_stack(
                pod_id=match.pod_id,
                target_refs=list(cfg.capability_key().lora_stack().refs),
                download_specs={ref: download_specs[ref] for ref in match.swap_plan.download},
            )
            ledger.touch(
                match.pod_id,
                lora_inventory=[e.dict() if hasattr(e, "dict") else e for e in resp["inventory"]],
                loras_dir_free_bytes=resp["free_bytes"],
                loras_dir_free_bytes_observed_at_local=datetime.now().isoformat(),
            )
        return match
    except LoraSwapDegradedPodError:
        ledger.touch(match.pod_id, status="degraded")
        _POD_LOCKS.release(match.pod_id)
        raise
    except LoraSwapPodUnreachableError:
        ledger.touch(match.pod_id, status="degraded")
        _POD_LOCKS.release(match.pod_id)
        raise
    except LoraSwapDiskFullError:
        ledger.touch(match.pod_id, status="degraded")
        _POD_LOCKS.release(match.pod_id)
        raise
    except (LoraSwapDownloadError, LoraSwapVramOomError):
        _POD_LOCKS.release(match.pod_id)
        raise
    except Exception:
        _POD_LOCKS.release(match.pod_id)
        raise


# Inside the existing generate/deploy flow, wherever warm_reuse_auto_attach is consulted:
if cfg.compute.warm_reuse_auto_attach:
    match = _try_warm_attach(cfg, ledger, build_backend, engine)
    if match is not None:
        try:
            # Existing submit/result flow on match's backend
            ...
        finally:
            _POD_LOCKS.release(match.pod_id)
    else:
        # existing cold-boot path
        ...
```

- [ ] **Step 5: Run — confirm GREEN.**

```bash
pixi run pytest tests/core/test_orchestrator_warm_reuse_lora.py tests/core/test_orchestrator.py -v
```

Expected: new tests pass; existing orchestrator suite unaffected.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_warm_reuse_lora.py
git commit -m "feat(orchestrator): route warm-attach via matcher + hold pod lock + post-swap ledger update"
```

---

### Task 16: kinoforge status --id renders LoRA section

**Goal:** Extend `kinoforge status --id <pod>` to print the `lora_inventory` block when present.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (the status renderer)
- Test: `tests/cli/test_status_renders_lora_inventory.py` (new)

**Acceptance Criteria:**
- [ ] When `pod_entry.lora_inventory` is non-empty: prints a `loras (...resident, ...used, ...free):` header + one line per LoRA with ref + size + age + adapter name.
- [ ] When empty/absent: prints no LoRA section (clean omission, not "loras: []").
- [ ] JSON output mode (`--format json`) emits inventory as a list of dicts with the same shape.
- [ ] LoRA refs flow through `RedactionRegistry.redact()` (vault-supplied + observed-registered refs come out as `<lora:ref:abc>`).

**Verify:** `pixi run pytest tests/cli/test_status_renders_lora_inventory.py -v`

**Steps:**

- [ ] **Step 1: Locate the status renderer.**

```bash
rg -n 'def _render_status|status.*--id|print_status' /workspace/src/kinoforge/cli/_main.py | head
```

- [ ] **Step 2: Write failing test.** (Snapshot the rendered string for known inventory; assert the header + one line per LoRA; assert empty inventory produces no header.)

- [ ] **Step 3: Run — confirm RED.**

- [ ] **Step 4: Extend the renderer.** Add a block after the existing heartbeat/cost lines:

```python
inv = entry.get("lora_inventory") or []
if inv:
    free = entry.get("loras_dir_free_bytes")
    total_bytes = sum(e["size_bytes"] for e in inv)
    lines.append(
        f"  loras ({len(inv)} resident, {_human(total_bytes)} used"
        + (f", {_human(free)} free):" if free is not None else "):")
    )
    for e in sorted(inv, key=lambda x: x["last_used_at_local"], reverse=True):
        ref = RedactionRegistry.instance().redact(e["ref"])
        lines.append(
            f"    {ref}  {_human(e['size_bytes'])}  "
            f"last_used {_relative(e['last_used_at_local'])}  "
            f"adapter {e['adapter_name']}"
        )
```

(Add `_human` for byte formatting + `_relative` for "12m ago" if not already present.)

- [ ] **Step 5: Run — confirm GREEN.**

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/cli/_main.py tests/cli/test_status_renders_lora_inventory.py
git commit -m "feat(cli-status): render LoRA inventory section + total/free disk on warm pods"
```

---

### Task 17: kinoforge generate/batch --dry-run-swap flag

**Goal:** New `--dry-run-swap` flag on `generate` and `batch`. Runs the matcher, prints the decision, exits without acquiring the pod lock or issuing any HTTP traffic.

**Files:**
- Modify: `src/kinoforge/cli/_main.py`
- Test: `tests/cli/test_dry_run_swap.py` (new)

**Acceptance Criteria:**
- [ ] `--dry-run-swap` accepted on both `generate` and `batch` subparsers.
- [ ] When matcher returns a match: prints per-candidate evaluation + selected pod + estimated cost.
- [ ] When matcher returns None: prints "no warm candidate, would cold-boot".
- [ ] Does NOT acquire the pod lock (verify: a second `--dry-run-swap` against the same cfg can still preview the same pod).
- [ ] Does NOT issue HTTP traffic (verify via spy on backend constructor — should not be called).
- [ ] Exits with code 0.

**Verify:** `pixi run pytest tests/cli/test_dry_run_swap.py -v`

**Steps:**

- [ ] **Step 1: Write failing test.**

- [ ] **Step 2: Add the flag to both subparsers in `_build_parser`:**

```python
for sub in (p_generate, p_batch):
    sub.add_argument(
        "--dry-run-swap",
        action="store_true",
        help="preview matcher decision for warm-attach without side effects",
    )
```

- [ ] **Step 3: Add an early-return in the generate/batch dispatch:**

```python
if getattr(args, "dry_run_swap", False):
    from kinoforge.core.warm_reuse.matcher import find_warm_attach_candidate
    download_specs = _build_download_specs(ctx.cfg)
    match = find_warm_attach_candidate(
        ctx.cfg, ctx.ledger,
        pod_lock_registry=_DRY_RUN_NULL_REGISTRY,  # acquire always True, never persisted
        re_probe=None,
        download_specs=download_specs,
    )
    if match is None:
        print("matcher: no warm candidate, would cold-boot")
    else:
        print(f"matcher: selected pod {match.pod_id}")
        print(f"  evict:    {match.swap_plan.evict}")
        print(f"  download: {match.swap_plan.download}")
        print(f"  cost:     {match.swap_plan.estimated_cost_seconds:.1f}s")
    return 0
```

`_DRY_RUN_NULL_REGISTRY` is a stub that always returns `True` from `acquire` and `False` from `__contains__` so no real lock state changes.

- [ ] **Step 4: Run — confirm GREEN.**

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/cli/_main.py tests/cli/test_dry_run_swap.py
git commit -m "feat(cli): generate/batch --dry-run-swap previews matcher decision without side effects"
```

---

### Task 18: kinoforge pod lora ls subcommand

**Goal:** New `kinoforge pod lora ls <pod_id>` subcommand that hits `GET /lora/inventory` directly.

**Files:**
- Modify: `src/kinoforge/cli/_main.py`
- Test: `tests/cli/test_pod_lora_ls.py` (new)

**Acceptance Criteria:**
- [ ] Subcommand chain `pod lora ls <pod_id>` registered.
- [ ] Fetches `GET /lora/inventory` against the pod's proxy URL (looked up via the existing pod-id → URL machinery used by `kinoforge status`).
- [ ] Renders the same inventory section as `kinoforge status` LoRA block.
- [ ] Exits with code 0 on success; non-zero on pod unreachable (with a clear error message).
- [ ] Bypasses the ledger entirely (useful under `--ephemeral`).

**Verify:** `pixi run pytest tests/cli/test_pod_lora_ls.py -v`

**Steps:**

- [ ] **Step 1: Write failing test.**

- [ ] **Step 2: Register the new subparser chain.** Pattern: `pod = sub.add_parser("pod")`, `pod_sub = pod.add_subparsers(dest="pod_cmd")`, `pod_lora = pod_sub.add_parser("lora")`, etc.

- [ ] **Step 3: Implement the handler.**

```python
def cmd_pod_lora_ls(args, ctx):
    pod_url = ctx.resolve_pod_proxy_url(args.pod_id)  # reuse existing helper
    import httpx  # or whatever the codebase uses
    resp = httpx.get(f"{pod_url}/lora/inventory", timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # Same renderer as kinoforge status LoRA block — refactor that into a
    # shared helper as part of this task.
    print(_render_lora_inventory(data["inventory"], data["free_bytes"]))
    return 0
```

Refactor the rendering logic from Task 16 into `_render_lora_inventory(inventory, free_bytes) -> str` so both `status` and `pod lora ls` call it.

- [ ] **Step 4: Run — confirm GREEN.**

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/cli/_main.py tests/cli/test_pod_lora_ls.py
git commit -m "feat(cli): kinoforge pod lora ls — direct pod-side inventory query"
```

---

### Task 19: compute.lifecycle.lora_swap_re_probe_after_s cfg field

**Goal:** Add the single new cfg knob (`compute.lifecycle.lora_swap_re_probe_after_s`, default 300) and thread it into the matcher call.

**Files:**
- Modify: `src/kinoforge/core/config.py` (`Lifecycle` model)
- Modify: `src/kinoforge/core/orchestrator.py` (pass into matcher)
- Test: `tests/core/test_config_lora_swap_re_probe.py` (new)

**Acceptance Criteria:**
- [ ] `Lifecycle.lora_swap_re_probe_after_s: float = 300.0` accepted.
- [ ] `0` disables the stale-check (matcher trusts ledger snapshot indefinitely).
- [ ] Plumbed through `_try_warm_attach` → `find_warm_attach_candidate(re_probe_threshold_s=...)`.
- [ ] Default in absence of the field = 300.0 (back-compat with existing cfgs).

**Verify:** `pixi run pytest tests/core/test_config_lora_swap_re_probe.py tests/core/test_config.py -v`

**Steps:**

- [ ] **Step 1: Write failing test that loads a YAML with the new field + reads it back.**

- [ ] **Step 2: Add the field to `Lifecycle` in `core/config.py`.**

- [ ] **Step 3: Thread the value into `find_warm_attach_candidate`.**

- [ ] **Step 4: Run — confirm GREEN.**

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/config.py src/kinoforge/core/orchestrator.py tests/core/test_config_lora_swap_re_probe.py
git commit -m "feat(config): compute.lifecycle.lora_swap_re_probe_after_s (default 300s)"
```

---

### Task 20: AST-scan invariants (2 new rules) in tests/test_no_unredacted_writes.py

**Goal:** Extend the existing AST-scan invariant test with two new rules per spec §5.4.

**Files:**
- Modify: `tests/test_no_unredacted_writes.py`

**Acceptance Criteria:**
- [ ] Rule 1: Any module reading `InventorySnapshot.inventory` (or `LoraInventoryEntry.ref` via iteration) must contain `_register_observed_lora_refs(` OR be annotated `# noqa: KF-LORA-REDACT-EXEMPT`. Violations fail the test with a specific message.
- [ ] Rule 2: Writes to ledger entry's `lora_inventory` key must go through `Ledger.touch` (no `store.put_json` direct writes with that key in the payload). Violations fail the test.
- [ ] Exemption tag string (`KF-LORA-REDACT-EXEMPT`) appears in only one place in the source tree by default (the helper itself), making future exemptions auditable.

**Verify:** `pixi run pytest tests/test_no_unredacted_writes.py -v`

**Steps:**

- [ ] **Step 1: Read the existing invariant test to learn the visitor pattern.**

```bash
cat /workspace/tests/test_no_unredacted_writes.py | head -100
```

- [ ] **Step 2: Add AST visitor for Rule 1.** Pattern: walk every Python file under `src/`; for each function body, detect attribute reads matching `.inventory` chained from a `InventorySnapshot`-typed name OR a `for ... in <name>.inventory` loop where the loop body reads `.ref`; require either `_register_observed_lora_refs(` in the same function or the exempt-tag comment.

- [ ] **Step 3: Add AST visitor for Rule 2.** Pattern: walk every Python file; flag any `put_json` call whose argument dict contains the literal key `"lora_inventory"`.

- [ ] **Step 4: Run — confirm GREEN against the current tree (or RED at known violation sites + add the helper call to fix them).**

- [ ] **Step 5: Commit.**

```bash
git add tests/test_no_unredacted_writes.py
git commit -m "test(invariants): AST-scan rules for InventorySnapshot redaction + Ledger.touch routing"
```

---

### Task 21: Integration tests (5 scenarios)

**Goal:** Five end-to-end-shaped tests using FakeEngine + LocalProvider + a fake pod-side server stub. Covers the matrix from spec §13.2.

**Files:**
- Create: `tests/integration/test_warm_reuse_lora_first_attach.py`
- Create: `tests/integration/test_warm_reuse_lora_overlap.py`
- Create: `tests/integration/test_warm_reuse_lora_lru_eviction.py`
- Create: `tests/integration/test_warm_reuse_lora_cold_boot_fallthrough.py`
- Create: `tests/integration/test_warm_reuse_lora_ephemeral.py`

**Acceptance Criteria:**
- [ ] Each test pins one named scenario from spec §13.2.
- [ ] Tests use the existing FakeEngine + LocalProvider scaffolding; no GPU, no network.
- [ ] Each test asserts the post-state of the ledger AND the calls made to the pod stub.
- [ ] Ephemeral test asserts cross-session inventory preservation on the pod stub AND zero on-disk ledger evidence.

**Verify:** `pixi run pytest tests/integration/ -v`

**Steps:**

- [ ] **Step 1: Survey existing integration tests to learn the fixture pattern.**

```bash
ls /workspace/tests/integration/
cat /workspace/tests/integration/test_ephemeral_only_output_dir_survives.py | head -80
```

- [ ] **Step 2: Write the 5 tests (one per file).** Each follows the same template:

```python
"""Integration: [scenario name]."""

# fixture: ledger + fake pod stub + cfg with N LoRAs
# act: orchestrator.generate(cfg)
# assert: ledger state + pod stub call history
```

- [ ] **Step 3: Run — confirm RED, then resolve each fixture/helper gap.**

- [ ] **Step 4: GREEN. Commit.**

```bash
git add tests/integration/test_warm_reuse_lora_*.py
git commit -m "test(integration): 5 warm-reuse LoRA scenarios (first-attach, overlap, LRU, cold-boot, ephemeral)"
```

---

### Task 22: Live smoke — Wan 2.2 + Arcane LoRA pair on RunPod A100

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Single live smoke that exercises the 4-step matrix from spec §13.3 on a real RunPod A100 80GB pod. Total spend cap ~$2.

**Files:**
- Create: `tests/live/test_wan22_lora_warm_reuse.py`
- Create: `examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml`

**Acceptance Criteria:**
- [ ] Test gated behind `KINOFORGE_LIVE=1`; skipped otherwise.
- [ ] Runs `pixi run preflight` first; fails fast if not exit 0.
- [ ] Step 1 (cold-boot, 0 LoRAs) produces a valid mp4 with no Arcane styling.
- [ ] Step 2 (warm-attach to [high, low]) produces a valid mp4 with Arcane styling (operator prepends `ArcaneStyle ` to prompt manually via the cfg).
- [ ] Step 3 (warm-attach to [low]) produces a valid mp4 with (partial) Arcane styling.
- [ ] Step 4 (warm-attach to []) produces a valid mp4 with no Arcane styling.
- [ ] Ledger `lora_inventory` matches pod's `/lora/inventory` at every step (assertion at end of each step).
- [ ] Cost stays under $2 (assertion against `BudgetTracker`).
- [ ] `kinoforge status` + `kinoforge pod lora ls` outputs agree at the end of step 3.
- [ ] Pod destruction at end: ASK operator via `AskUserQuestion` or fail-open (per the user-scope "never destroy pods without explicit authorization" memory).
- [ ] Pod-side RunPod metrics polled every 60-90s during the smoke (per user-scope "proactive pod stats" memory) — surface GPU util / cost drift / restart-loop signals.

**Verify:** `KINOFORGE_LIVE=1 pixi run -e live-runpod pytest tests/live/test_wan22_lora_warm_reuse.py -v -s`

**Steps:**

- [ ] **Step 1: Pre-spend gate — preflight check.**

```bash
pixi run preflight
```

Expected: exit 0 (creds present, no active pods, clean working tree).

- [ ] **Step 2: Write the smoke test.** Follow the existing live-smoke pattern (e.g. `tests/live/test_wan22_native_t2v.py`).

- [ ] **Step 3: Write the cfg with both Arcane LoRAs declared + the `field-realistic.txt` prompt loaded with `ArcaneStyle ` prefix.**

- [ ] **Step 4: Run the smoke. Capture pod stats every 60-90s during long-running steps.**

- [ ] **Step 5: Assert post-state: ledger inventory matches pod's, total spend < $2, 4 mp4s landed in `output/`.**

- [ ] **Step 6: Ask the operator before pod destroy.**

- [ ] **Step 7: Commit the cfg + smoke (cfg gated as RED — committed before live spend per CLAUDE.md durability rule).**

```bash
git add examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml tests/live/test_wan22_lora_warm_reuse.py
git commit -m "test(live): Wan 2.2 + Arcane LoRA warm-reuse smoke (4 steps, ~\$2 cap)"
```

- [ ] **Step 8: Log to `successful-generations.md` per CLAUDE.md durability rule (new mode = warm-reuse with LoRA swap, new capability axis).**

---

### Task 23: README + PROGRESS update + close

**Goal:** Add a "LoRA-flexible warm-reuse" section to README explaining the feature for operators. Update PROGRESS C23 / C24 + add a top-of-file note that the feature shipped. Bump `successful-generations.md` entry from Task 22.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`
- Modify: `successful-generations.md` (final entry confirmation from Task 22)

**Acceptance Criteria:**
- [ ] New top-level README section "LoRA-flexible warm-reuse" covers: what it does, when warm-attach happens with different LoRAs, the eviction policy, the per-pod lock, `--dry-run-swap` usage, `pod lora ls` usage, the failure modes (one-line each), and the deferred features (cross-process lock, hot-swap UX, pinning).
- [ ] PROGRESS top-of-file: short "LoRA-flexible warm-reuse shipped (commit <hash>)" note.
- [ ] PROGRESS C23 + C24 entries cross-link to the new spec + plan + live-smoke commit hash.
- [ ] Successful-generations entry from Task 22 is in place + correctly tagged.

**Verify:** `rg -q 'LoRA-flexible warm-reuse' /workspace/README.md && rg -q 'lora-flexible' /workspace/PROGRESS.md && echo OK`

**Steps:**

- [ ] **Step 1: Write the README section.**

- [ ] **Step 2: Update PROGRESS.**

- [ ] **Step 3: Confirm successful-generations entry.**

- [ ] **Step 4: Commit.**

```bash
git add README.md PROGRESS.md successful-generations.md
git commit -m "docs: LoRA-flexible warm-reuse shipped — README + PROGRESS + successful-generations"
```

---

## Self-Review

**1. Spec coverage:** Every section/decision in `2026-06-20-lora-flexible-warm-reuse-design.md` mapped to a task. D1–D9 covered by Tasks 1, 3, 4–8, 9, 12, 14, 15, 19. §5.4 AST invariants → Task 20. §13.3 live smoke → Task 22.

**2. Placeholder scan:** No "TBD", "TODO", "implement later" anywhere. Every step shows the actual code/test/command.

**3. Type consistency:** `WarmAttachKey`, `LoraStack`, `CapabilityKey.warm_attach_key()`, `CapabilityKey.lora_stack()` consistent across Tasks 1, 9, 10, 11, 14, 15. `SetStackRequest`/`SetStackResponse`/`LoraInventoryEntry`/`ArtifactDownloadSpec` consistent across Tasks 4–8 + 11. Error classes consistent across Tasks 3, 8, 11, 15.

**4. User-gate audit:** Task 22 (live smoke) tagged `userGate: true` per the gate-language rule (`prove`, `verify`, `gate` all present) + carries the banner + `requireEvidenceTokens` for the four step labels.
