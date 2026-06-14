# C29 — Heartbeat Earlier (Boot-Phase Protection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Heartbeat loop (and its C26 STALL_REAP + C27 RESTART_LOOP_REAP predicates) begins ticking the moment the pod's container is RUNNING per the provider, not after `engine.wait_for_ready` returns. `boot_timeout` demotes from sole boot-phase protection to outer backstop.

**Architecture:** `deploy_session` builds a `start_heartbeat` closure capturing everything except `instance.id`; passes it as new kwarg to `_provision_instance_and_build_backend`; helper invokes the closure right after the RunPod-status poll succeeds (orch:596) and before `engine.provision()` runs. Returns a new `ProvisionResult` NamedTuple `(instance, backend, hb_loop)`. `GenerationEngine.wait_for_ready` gains a `cancel_token` kwarg so a reap-mid-boot raises `Cancelled` cleanly; `Cancelled` propagates up + the outer except in the helper destroys the pod idempotently.

**Tech Stack:** Python 3.x, pixi, pytest, pre-commit (ruff/mypy), threading.Event, RunPod GraphQL provider, ComfyUI/Diffusers/Fake engines.

**Spec:** `docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md` (commit `b9a5da8`).

---

## File Structure

**Modified (existing files):**

- `src/kinoforge/core/interfaces.py` — `GenerationEngine` Protocol: `wait_for_ready` + `provision` gain `cancel_token` kwarg.
- `src/kinoforge/core/orchestrator.py` —
  - new `ProvisionResult` NamedTuple,
  - new `_build_start_heartbeat_closure` helper,
  - `_provision_instance_and_build_backend` signature + body changes,
  - `_provision_compute_once` threads `cancel_token`,
  - `deploy_session` wires closure + receives NamedTuple from both call sites.
- `src/kinoforge/engines/comfyui/__init__.py` — `wait_for_ready` + `provision` accept `cancel_token`.
- `src/kinoforge/engines/diffusers/__init__.py` — same.
- `src/kinoforge/engines/fake/__init__.py` — same (Protocol parity).

**Modified (existing tests):**

- `tests/core/test_orchestrator_heartbeat.py` (timing assertions)
- `tests/core/test_orchestrator_creds_default.py` (spy return shape)
- `tests/core/test_batch_creds_default.py` (spy return shape)
- `tests/engines/test_comfyui_wait_for_ready.py` (kwarg defaults)
- `tests/engines/test_diffusers_wait_for_ready.py` (kwarg defaults)
- `tests/engines/test_comfyui_provision_branch.py` (fake `wait_for_ready` signatures)
- `tests/engines/test_diffusers_provision_branch.py` (fake `wait_for_ready` signatures)

**New (tests):**

- `tests/core/test_orchestrator_c29_start_heartbeat.py`
- `tests/core/test_orchestrator_c29_cancel_during_boot.py`
- `tests/engines/test_comfyui_wait_for_ready_cancel.py`
- `tests/engines/test_diffusers_wait_for_ready_cancel.py`
- `tests/engines/test_fake_wait_for_ready_cancel.py`

**New (live smokes):**

- `tests/live/test_c29_phase_a_boot_stall_live.py` + `tests/live/_c29_phase_a_cfg.yaml`
- `tests/live/test_c29_phase_b_boot_restart_loop_live.py` + `tests/live/_c29_phase_b_cfg.yaml`
- `tests/live/test_c29_phase_c_boot_status_live.py` + `tests/live/_c29_phase_c_cfg.yaml`

**Doc updates:**

- `PROGRESS.md` — C29 status + closing notes.
- `docs/superpowers/plans/2026-06-14-c29-heartbeat-earlier.md.tasks.json` — task-state persistence.

---

## Task Granularity Notes

- Engine ABI changes are split per file/concern to keep each commit focused.
- Live smokes RED-scaffolded BEFORE live spend per CLAUDE.md durability rules.
- Smokes are NOT user-gated — pre-authorized per user memory `feedback_autonomous_no_gates`. Mechanical `pixi run preflight` is the spend gate.
- Total expected live spend: ~$0.30 across Smokes A/B/C.

---

### Task 0: Baseline + audit grep

**Goal:** Lock in a green-baseline test count before any code change and surface every call site the audit didn't catch in the spec.

**Files:**
- Read-only sweep — no edits.
- Capture: terminal output to working memory.

**Acceptance Criteria:**
- [ ] `pixi run test` exits 0 with full test count recorded.
- [ ] `rg` audit shows no surprise call sites beyond those enumerated in the spec.

**Verify:** `pixi run test 2>&1 | tail -10` → "X passed" (record X for baseline).

**Steps:**

- [ ] **Step 1: Capture baseline test count.**

```bash
pixi run test 2>&1 | tee /tmp/c29_baseline.log | tail -20
```

Record the "passed" line. Expect ≥ 2469 tests collected (per `pixi run test --collect-only`).

- [ ] **Step 2: Audit `wait_for_ready` callers.**

```bash
rg -n 'wait_for_ready' src/ tests/ | tee /tmp/c29_wfr_audit.log
```

Expected hits (must match — fail if extras):
- `src/kinoforge/engines/comfyui/__init__.py` (impl + provision-time call)
- `src/kinoforge/engines/diffusers/__init__.py` (impl + provision-time call)
- `src/kinoforge/engines/fake/__init__.py` (impl)
- 5 test files (comfyui_provision_branch, diffusers_provision_branch, comfyui_wait_for_ready, diffusers_wait_for_ready, plus 1 known)

- [ ] **Step 3: Audit `_provision_instance_and_build_backend` call sites.**

```bash
rg -n '_provision_instance_and_build_backend' src/ tests/ | tee /tmp/c29_helper_audit.log
```

Expected: 2 internal callers (orchestrator.py:889, :922) + 2 test spies (test_orchestrator_creds_default.py, test_batch_creds_default.py) + comment-only refs.

- [ ] **Step 4: Audit `HeartbeatLoop` construction.**

```bash
rg -n 'HeartbeatLoop\(|heartbeat_loop_factory' src/ tests/ | tee /tmp/c29_hb_audit.log
```

Confirm only one production construction site (orch:1019-1032) and the factory seam (orch:986, deploy_session signature).

- [ ] **Step 5: Confirm engine.provision sites.**

```bash
rg -n 'def provision' src/kinoforge/engines/ src/kinoforge/core/interfaces.py
```

Expected: 3 engine impls + 2 Protocol decls. No others.

- [ ] **Step 6: Record baseline + commit nothing.**

This task is read-only. No commit.

---

### Task 1: Engine wait_for_ready — cancel_token kwarg (Protocol + 3 impls)

**Goal:** `GenerationEngine.wait_for_ready` Protocol + ComfyUI/Diffusers/Fake impls accept `cancel_token: CancelToken | None = None` and call `cancel_token.raise_if_set()` at the top of the poll loop. Default-None keeps existing callers unchanged.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (Protocol — both `GenerationEngine` declarations at :359 and :575 region; identify which is `wait_for_ready`'s)
- Modify: `src/kinoforge/engines/comfyui/__init__.py:1430-1486` (`wait_for_ready` impl)
- Modify: `src/kinoforge/engines/diffusers/__init__.py:525-...` (`wait_for_ready` impl)
- Modify: `src/kinoforge/engines/fake/__init__.py:300-...` (`wait_for_ready` impl)
- Create: `tests/engines/test_comfyui_wait_for_ready_cancel.py`
- Create: `tests/engines/test_diffusers_wait_for_ready_cancel.py`
- Create: `tests/engines/test_fake_wait_for_ready_cancel.py`

**Acceptance Criteria:**
- [ ] Each of the 3 engine `wait_for_ready` impls accepts `cancel_token: CancelToken | None = None` kwarg.
- [ ] Each impl calls `cancel_token.raise_if_set()` at the top of the poll loop (before `http_get` for ComfyUI/Diffusers, equivalent point in Fake).
- [ ] Protocol declaration in `interfaces.py` includes the kwarg.
- [ ] All 3 new test files contain at minimum: token-set-before-poll raises Cancelled; token-set-mid-poll raises Cancelled on next iter; token=None preserves today's behaviour.
- [ ] Existing `tests/engines/test_comfyui_wait_for_ready.py` + `tests/engines/test_diffusers_wait_for_ready.py` pass UNCHANGED (default-None preserves behaviour).

**Verify:** `pixi run pytest tests/engines/test_comfyui_wait_for_ready_cancel.py tests/engines/test_diffusers_wait_for_ready_cancel.py tests/engines/test_fake_wait_for_ready_cancel.py tests/engines/test_comfyui_wait_for_ready.py tests/engines/test_diffusers_wait_for_ready.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Locate `wait_for_ready` in `interfaces.py`.**

```bash
rg -n 'wait_for_ready' src/kinoforge/core/interfaces.py
```

Identify the Protocol method declaration line(s). There may be one or two (base + subclass Protocol).

- [ ] **Step 2: Write failing tests for ComfyUI cancel-token behaviour.**

Create `tests/engines/test_comfyui_wait_for_ready_cancel.py`:

```python
"""C29 — wait_for_ready honors CancelToken (boot-phase reap path)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.engines.comfyui import ComfyUIEngine
from kinoforge.core.interfaces import Instance


def _instance() -> Instance:
    return Instance(
        id="pod-c29",
        status="ready",
        endpoints={"8188": "http://example.invalid:8188"},
        provider="runpod",
        offer=None,  # type: ignore[arg-type]
    )


def test_wait_for_ready_raises_cancelled_when_token_set_before_poll() -> None:
    token = CancelToken()
    token.set()
    http_get_calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        http_get_calls.append(url)
        return {}

    with pytest.raises(Cancelled):
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            _instance(),
            http_get=http_get,
            sleep=lambda _s: None,
            get_instance=lambda _id: _instance(),
            timeout_s=5.0,
            cancel_token=token,
        )
    assert http_get_calls == [], "http_get must not be called when token is pre-set"


def test_wait_for_ready_raises_cancelled_when_token_set_mid_poll() -> None:
    token = CancelToken()
    poll_count = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        raise RuntimeError("not yet")

    def sleep(_s: float) -> None:
        poll_count["n"] += 1
        if poll_count["n"] == 2:
            token.set()

    with pytest.raises(Cancelled):
        ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
            _instance(),
            http_get=http_get,
            sleep=sleep,
            get_instance=lambda _id: _instance(),
            timeout_s=60.0,
            cancel_token=token,
        )
    assert poll_count["n"] == 2, (
        "expected the token to be observed at the top of iter 3 (set during iter 2 sleep)"
    )


def test_wait_for_ready_no_cancel_token_preserves_today_behavior() -> None:
    calls = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"system_stats": True}

    ComfyUIEngine(probe_profile=None).wait_for_ready(  # type: ignore[arg-type]
        _instance(),
        http_get=http_get,
        sleep=lambda _s: None,
        get_instance=lambda _id: _instance(),
        timeout_s=5.0,
    )
    assert calls["n"] == 1
```

- [ ] **Step 3: Run failing test.**

```bash
pixi run pytest tests/engines/test_comfyui_wait_for_ready_cancel.py -v
```

Expected: FAIL — `wait_for_ready() got an unexpected keyword argument 'cancel_token'`.

- [ ] **Step 4: Add `cancel_token` to ComfyUI `wait_for_ready`.**

Modify `src/kinoforge/engines/comfyui/__init__.py:1430` signature + poll-loop body. Locate `def wait_for_ready(...) -> None:` and rewrite:

```python
def wait_for_ready(
    self,
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
    cancel_token: CancelToken | None = None,
) -> None:
    """Poll ``GET <comfyui>/system_stats`` until 200, status terminal, or timeout.

    Args:
        instance: The just-created compute instance.
        http_get: HTTP GET seam — raises on error, returns dict on success.
        sleep: Sleep seam used between polls.
        get_instance: Provider lookup for status checks between polls.
        timeout_s: Maximum total wait.
        cancel_token: C29 cooperative cancellation. When set (e.g. by a boot-
            phase heartbeat reap), the next loop iteration raises Cancelled
            before any I/O. Default None preserves pre-C29 behaviour.

    Raises:
        ProvisionFailed: Pod entered terminal status before ready.
        ProvisionTimeout: ``timeout_s`` elapsed without a successful ready check.
        Cancelled: ``cancel_token`` was set during the wait.
    """
    if not instance.endpoints:
        raise ProvisionFailed(
            f"pod {instance.id!r} has no endpoints — cannot construct ready URL"
        )
    port_key = (
        "8188"
        if "8188" in instance.endpoints
        else next(iter(instance.endpoints), "8188")
    )
    base = instance.endpoints.get(port_key, "")
    ready_url = f"{base.rstrip('/')}/system_stats"

    start = time.monotonic()
    while True:
        if cancel_token is not None:
            cancel_token.raise_if_set()
        now = time.monotonic()
        if now - start >= timeout_s:
            raise ProvisionTimeout(
                f"engine ready check timed out after {timeout_s:.0f}s "
                f"for pod {instance.id!r}"
            )
        try:
            http_get(ready_url)
            return
        except Exception:  # noqa: BLE001, S110
            pass
        current = get_instance(instance.id)
        if current.status in ("terminated", "stopped"):
            raise ProvisionFailed(
                f"pod {instance.id!r} entered terminal status "
                f"{current.status!r} before ready"
            )
        sleep(_READY_POLL_INTERVAL_S)
```

Add import at file top if missing: `from kinoforge.core.cancel import CancelToken`.

- [ ] **Step 5: Run ComfyUI cancel tests — expect green.**

```bash
pixi run pytest tests/engines/test_comfyui_wait_for_ready_cancel.py tests/engines/test_comfyui_wait_for_ready.py -v
```

Expected: all pass.

- [ ] **Step 6: Mirror for Diffusers.**

Create `tests/engines/test_diffusers_wait_for_ready_cancel.py` (same 3 tests, swap `ComfyUIEngine` → `DiffusersEngine`, `port_key="8000"` if applicable — read `src/kinoforge/engines/diffusers/__init__.py:525` to confirm endpoint key). Then modify Diffusers impl identically: add `cancel_token` kwarg + `cancel_token.raise_if_set()` at top of `while True:`. Verify:

```bash
pixi run pytest tests/engines/test_diffusers_wait_for_ready_cancel.py tests/engines/test_diffusers_wait_for_ready.py -v
```

Expected: all pass.

- [ ] **Step 7: Mirror for Fake.**

Create `tests/engines/test_fake_wait_for_ready_cancel.py` — minimal Protocol-parity test:

```python
"""C29 — FakeEngine.wait_for_ready honors CancelToken (Protocol parity)."""

from __future__ import annotations

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled
from kinoforge.engines.fake import FakeEngine
from kinoforge.core.interfaces import Instance


def _instance() -> Instance:
    return Instance(
        id="pod-fake",
        status="ready",
        endpoints={"8000": "http://example.invalid:8000"},
        provider="fake",
        offer=None,  # type: ignore[arg-type]
    )


def test_fake_wait_for_ready_raises_cancelled_when_token_set() -> None:
    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        FakeEngine().wait_for_ready(  # type: ignore[call-arg]
            _instance(),
            http_get=lambda _u: {},
            sleep=lambda _s: None,
            get_instance=lambda _id: _instance(),
            timeout_s=1.0,
            cancel_token=token,
        )


def test_fake_wait_for_ready_no_token_preserves_behavior() -> None:
    FakeEngine().wait_for_ready(  # type: ignore[call-arg]
        _instance(),
        http_get=lambda _u: {},
        sleep=lambda _s: None,
        get_instance=lambda _id: _instance(),
        timeout_s=1.0,
    )
```

Modify `FakeEngine.wait_for_ready` at `src/kinoforge/engines/fake/__init__.py:300` to add `cancel_token: CancelToken | None = None` kwarg + the `raise_if_set()` check at the top of whatever loop/return path it uses. Verify:

```bash
pixi run pytest tests/engines/test_fake_wait_for_ready_cancel.py -v
```

- [ ] **Step 8: Update Protocol declarations in interfaces.py.**

Locate both `wait_for_ready` Protocol method decls (from Step 1 grep). Update each to:

```python
def wait_for_ready(
    self,
    instance: Instance,
    *,
    http_get: Callable[[str], dict[str, Any]],
    sleep: Callable[[float], None],
    get_instance: Callable[[str], Instance],
    timeout_s: float,
    cancel_token: "CancelToken | None" = None,
) -> None: ...  # noqa: D102
```

Add the forward-reference TYPE_CHECKING import for `CancelToken` at the top of `interfaces.py` if not already present:

```python
if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
```

- [ ] **Step 9: Full engine-test sweep.**

```bash
pixi run pytest tests/engines/ -v 2>&1 | tail -30
```

Expected: ALL pass. Any failure in `test_comfyui_wait_for_ready.py` / `test_diffusers_wait_for_ready.py` / `test_*_provision_branch.py` means a fake signature missed the kwarg — those tests must keep working unchanged (default-None semantics). If they fail with `unexpected keyword argument`, the issue is that the existing tests' fake `wait_for_ready` impls don't accept `cancel_token` even though the real ones do — that's fine as long as the orchestrator doesn't pass cancel_token to fakes in those tests. Investigate; if needed, defer existing-test fixups to Task 6.

- [ ] **Step 10: Commit.**

```bash
git add src/kinoforge/core/interfaces.py \
       src/kinoforge/engines/comfyui/__init__.py \
       src/kinoforge/engines/diffusers/__init__.py \
       src/kinoforge/engines/fake/__init__.py \
       tests/engines/test_comfyui_wait_for_ready_cancel.py \
       tests/engines/test_diffusers_wait_for_ready_cancel.py \
       tests/engines/test_fake_wait_for_ready_cancel.py
pixi run pre-commit run --files \
    src/kinoforge/core/interfaces.py \
    src/kinoforge/engines/comfyui/__init__.py \
    src/kinoforge/engines/diffusers/__init__.py \
    src/kinoforge/engines/fake/__init__.py \
    tests/engines/test_comfyui_wait_for_ready_cancel.py \
    tests/engines/test_diffusers_wait_for_ready_cancel.py \
    tests/engines/test_fake_wait_for_ready_cancel.py
git commit -m "$(cat <<'EOF'
feat(c29): engine wait_for_ready honors CancelToken for boot-phase reap

GenerationEngine.wait_for_ready Protocol + ComfyUI/Diffusers/Fake impls accept
cancel_token: CancelToken | None = None; call raise_if_set() at the top of the
poll loop so a boot-phase heartbeat reap (or operator Ctrl-C) raises Cancelled
on the next iteration before any I/O. Default-None preserves pre-C29 behaviour.

EOF
)"
```

---

### Task 2: Engine `provision()` threads cancel_token

**Goal:** `GenerationEngine.provision()` accepts `cancel_token` kwarg + threads it into the `self.wait_for_ready(...)` call. Protocol + 3 impls updated.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (both `provision` Protocol method decls at :359 and :575)
- Modify: `src/kinoforge/engines/comfyui/__init__.py:1093` + the internal `wait_for_ready` call at `:1160`
- Modify: `src/kinoforge/engines/diffusers/__init__.py:423` + the internal `wait_for_ready` call at `:468`
- Modify: `src/kinoforge/engines/fake/__init__.py:201` (no-op pass-through if body doesn't call wait_for_ready)

**Acceptance Criteria:**
- [ ] All 3 engine `provision()` impls accept `cancel_token: CancelToken | None = None`.
- [ ] ComfyUI + Diffusers `provision()` pass `cancel_token=cancel_token` into their internal `self.wait_for_ready(...)` call.
- [ ] Protocol declarations updated.
- [ ] All existing `engines/test_*_provision_branch.py` tests pass UNCHANGED (default-None).
- [ ] No new test files this task — exercised by Task 4's integration tests.

**Verify:** `pixi run pytest tests/engines/ -v 2>&1 | tail -10` → all pass.

**Steps:**

- [ ] **Step 1: Update Protocol decls in `interfaces.py`.**

```python
def provision(
    self,
    instance: Instance | None,
    cfg: dict[str, object],
    *,
    cancel_token: "CancelToken | None" = None,
) -> None: ...  # noqa: D102
```

Both at line 359 and 575 (per the grep audit). Keep the forward reference to `CancelToken`.

- [ ] **Step 2: Update ComfyUI `provision()`.**

Modify `src/kinoforge/engines/comfyui/__init__.py:1093` signature:

```python
def provision(
    self,
    instance: Instance | None,
    cfg: dict[str, Any],
    *,
    cancel_token: CancelToken | None = None,
) -> None:
```

At the `self.wait_for_ready(` call (around :1160), add `cancel_token=cancel_token,` to the kwargs.

- [ ] **Step 3: Update Diffusers `provision()`.**

Same as Step 2 for `src/kinoforge/engines/diffusers/__init__.py:423` + `:468`.

- [ ] **Step 4: Update Fake `provision()`.**

`src/kinoforge/engines/fake/__init__.py:201`: add the kwarg. If body does NOT call `wait_for_ready`, ignore the kwarg internally (still accept for Protocol parity).

- [ ] **Step 5: Run engine + provision-branch tests.**

```bash
pixi run pytest tests/engines/ -v 2>&1 | tail -30
```

Expected: ALL pass.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/interfaces.py \
       src/kinoforge/engines/comfyui/__init__.py \
       src/kinoforge/engines/diffusers/__init__.py \
       src/kinoforge/engines/fake/__init__.py
pixi run pre-commit run --files \
    src/kinoforge/core/interfaces.py \
    src/kinoforge/engines/comfyui/__init__.py \
    src/kinoforge/engines/diffusers/__init__.py \
    src/kinoforge/engines/fake/__init__.py
git commit -m "$(cat <<'EOF'
feat(c29): engine.provision threads cancel_token into wait_for_ready

GenerationEngine.provision Protocol + 3 impls accept cancel_token kwarg and
forward into self.wait_for_ready(). Closes the gap between orchestrator-level
cancellation and engine-level boot-phase polling.

EOF
)"
```

---

### Task 3: `_provision_compute_once` threads cancel_token

**Goal:** Orchestrator helper accepts + forwards `cancel_token` to `engine.provision()`.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:246` (`_provision_compute_once` signature + body)
- Modify: `src/kinoforge/core/orchestrator.py:605` (call site inside `_provision_instance_and_build_backend`)
- Modify: `src/kinoforge/core/orchestrator.py:~1377` (second call site mentioned in audit; verify)

**Acceptance Criteria:**
- [ ] `_provision_compute_once` accepts `cancel_token: CancelToken | None = None` kwarg.
- [ ] Forwards into `engine.provision(instance, cfg_dict, cancel_token=cancel_token)`.
- [ ] Both internal call sites compile (1377 site may not need cancel_token if it's outside the orchestrator boot path; investigate).
- [ ] Existing orchestrator tests pass UNCHANGED.

**Verify:** `pixi run pytest tests/core/test_orchestrator_compute.py tests/core/test_orchestrator_session_claim.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Inspect both `_provision_compute_once` call sites.**

```bash
rg -n '_provision_compute_once' src/kinoforge/core/orchestrator.py
```

Confirm the line 1377 site is or is not a fresh boot-path (likely it's `deploy_compute` — read 20 lines around).

- [ ] **Step 2: Add kwarg to `_provision_compute_once`.**

At `src/kinoforge/core/orchestrator.py:246`, update signature:

```python
def _provision_compute_once(
    *,
    engine: GenerationEngine,
    cfg: Config,
    instance: Instance,
    creds: CredentialProvider | None,
    store: ArtifactStore,
    state_dir: Path,
    capability_key_hex: str,
    cfg_dict_override: dict[str, object] | None = None,
    cancel_token: CancelToken | None = None,
) -> None:
```

In the body, locate the call to the provisioner / `engine.provision(...)` and add `cancel_token=cancel_token` to its kwargs. The actual call may be wrapped via `provisioner.provision`; if so, that wrapper also needs the kwarg threaded through. Read 30 lines after :246 to identify; thread or stop at `engine.provision`.

- [ ] **Step 3: Update internal call site at orchestrator.py:605.**

Replace:

```python
_provision_compute_once(
    engine=resolved_engine,
    cfg=cfg,
    instance=instance,
    creds=creds,
    store=store,
    state_dir=state_dir,
    capability_key_hex=marker_key_for(cfg, default=key.derive()),
    cfg_dict_override=cfg_dict,
)
```

with the same call plus `cancel_token=cancel_token` (the kwarg comes from the new `_provision_instance_and_build_backend` parameter added in Task 4 — for this task add the parameter to `_provision_instance_and_build_backend` first as `cancel_token: CancelToken | None = None`; full closure-arg wiring happens in Task 4).

- [ ] **Step 4: Add `cancel_token` to `_provision_instance_and_build_backend` signature (interim).**

Add at the end of its kwargs:

```python
cancel_token: CancelToken | None = None,
```

(Both callers in `deploy_session` will start passing this in Task 4.)

- [ ] **Step 5: Decide on the 1377 call site.**

If line 1377 belongs to `deploy_compute` (the synchronous deploy-without-yield helper used by `kinoforge deploy`) and that helper has no `cancel_token`, leave it omitting the kwarg (default None preserves behaviour). Note in this step's commit message which way it was resolved.

- [ ] **Step 6: Run orchestrator-compute + session-claim tests.**

```bash
pixi run pytest tests/core/test_orchestrator_compute.py tests/core/test_orchestrator_session_claim.py tests/core/test_orchestrator_heartbeat.py -v
```

Expected: all pass UNCHANGED (the kwarg defaults preserve existing behaviour).

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/orchestrator.py
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py
git commit -m "$(cat <<'EOF'
feat(c29): _provision_compute_once + _provision_instance_and_build_backend
accept cancel_token

Threads CancelToken into engine.provision() via the orchestrator helpers so a
boot-phase reap can interrupt a blocking wait_for_ready cleanly. Defaults to
None to preserve every existing caller's behaviour. Pure plumbing — no logic
change in this commit.

EOF
)"
```

---

### Task 4: `ProvisionResult` NamedTuple + `start_heartbeat` closure

**Goal:** Hoist hb_loop construction out of `deploy_session`'s post-provision block into a `start_heartbeat` closure that fires INSIDE `_provision_instance_and_build_backend` right after the RunPod-status poll loop (orch:596) succeeds. Helper returns a new `ProvisionResult(instance, backend, hb_loop)` NamedTuple.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (multiple regions — see Steps)
- Create: `tests/core/test_orchestrator_c29_start_heartbeat.py`

**Acceptance Criteria:**
- [ ] New `ProvisionResult` NamedTuple defined near top of `orchestrator.py` (or alongside `DeploySession`).
- [ ] New `_build_start_heartbeat_closure(...)` helper builds a closure capturing `(ledger, provider, interval, util_endpoint, cancel_token, provider_kind, stall_*, restart_loop_*, factory)`.
- [ ] `_provision_instance_and_build_backend` accepts `start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None`.
- [ ] After `instance.status == "ready"` at line 596, before `attach_get_instance` at line 602, `_provision_instance_and_build_backend` invokes `start_heartbeat(instance)` if supplied (wrapped in try/except logging — closure failure falls through to None).
- [ ] `_provision_instance_and_build_backend` returns `ProvisionResult(instance, backend, hb_loop)`.
- [ ] `deploy_session` builds the closure (extracting today's lines 972-1033) and passes it to both `_provision_instance_and_build_backend` call sites (orch:889, :922).
- [ ] Caller-supplied-instance branches at orch:883-887 + :917-920 keep inline hb construction at original position (byte-identical).
- [ ] `tests/core/test_orchestrator_c29_start_heartbeat.py` covers: invoked-after-status-ready, NOT-invoked-for-caller-supplied, NOT-invoked-when-interval-None, closure-failure-falls-through-to-None, ProvisionResult shape.

**Verify:** `pixi run pytest tests/core/test_orchestrator_c29_start_heartbeat.py tests/core/test_orchestrator_compute.py tests/core/test_orchestrator_heartbeat.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests first.**

Create `tests/core/test_orchestrator_c29_start_heartbeat.py`:

```python
"""C29 — start_heartbeat closure fires after RunPod status-ready, before engine.provision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.heartbeat_loop import HeartbeatLoopProtocol
from kinoforge.core.interfaces import Instance


@dataclass
class _FakeLoop:
    started: bool = False
    stopped: bool = False
    instance_id: str = ""

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def test_provision_result_namedtuple_shape() -> None:
    """ProvisionResult is a NamedTuple with three fields."""
    inst = MagicMock(spec=Instance)
    backend = MagicMock()
    loop = _FakeLoop()
    result = orchestrator.ProvisionResult(
        instance=inst, backend=backend, hb_loop=loop
    )
    assert result.instance is inst
    assert result.backend is backend
    assert result.hb_loop is loop
    # NamedTuple unpacking works
    a, b, c = result
    assert (a, b, c) == (inst, backend, loop)


def test_start_heartbeat_closure_invoked_after_status_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_heartbeat is invoked AFTER the RunPod-status poll succeeds.

    Concrete signal: a fake provider whose get_instance returns "creating"
    twice then "ready". start_heartbeat must be called exactly once, AFTER
    instance.status flips to ready and BEFORE _provision_compute_once.
    """
    # Construct minimal cfg, fake provider, fake engine that records call order.
    # The test exercises _provision_instance_and_build_backend directly.
    # ... (full setup omitted in plan — implementer fills based on existing
    #      test_orchestrator_heartbeat.py patterns)
    pytest.skip("plan-skeleton — implementer must build per existing test fixture patterns")


def test_start_heartbeat_not_invoked_for_caller_supplied_instance() -> None:
    """When _caller_supplied_instance=True branch runs, the closure is never invoked.

    Tested via deploy_session with instance= kwarg supplied.
    """
    pytest.skip("plan-skeleton — implementer follows test_orchestrator_session_claim.py patterns")


def test_start_heartbeat_not_invoked_when_hb_interval_is_none() -> None:
    """cfg with heartbeat_interval_s=None → no closure built → not passed."""
    pytest.skip("plan-skeleton")


def test_start_heartbeat_closure_failure_falls_through_to_none() -> None:
    """If the closure raises, _provision_instance_and_build_backend returns
    ProvisionResult with hb_loop=None and logs (no crash)."""
    pytest.skip("plan-skeleton")
```

Then expand each `pytest.skip` test into a concrete implementation using the patterns from `tests/core/test_orchestrator_heartbeat.py` (which already builds fake providers + engines for the existing late-start hb tests). The implementer reads that file first and mirrors its fixtures.

- [ ] **Step 2: Run failing tests.**

```bash
pixi run pytest tests/core/test_orchestrator_c29_start_heartbeat.py -v
```

Expected: `test_provision_result_namedtuple_shape` FAILS with `AttributeError: module 'kinoforge.core.orchestrator' has no attribute 'ProvisionResult'`.

- [ ] **Step 3: Add `ProvisionResult` NamedTuple.**

Near the top of `src/kinoforge/core/orchestrator.py`, after the existing imports, add:

```python
from typing import NamedTuple

# ... later, near the DeploySession dataclass:

class ProvisionResult(NamedTuple):
    """C29 — return shape of :func:`_provision_instance_and_build_backend`.

    ``hb_loop`` is ``None`` when ``start_heartbeat`` was not supplied
    (hosted-engine paths, ``heartbeat_interval_s ≤ 0``, or callers that
    explicitly opted out). NamedTuple supports both field-access and
    positional unpacking so existing ``instance, backend = ...`` call sites
    can migrate in one step.

    Attributes:
        instance: The polled-ready compute instance.
        backend: The engine-built backend wired to ``instance``.
        hb_loop: A running HeartbeatLoop (``start()`` already called), or
            ``None`` when no closure was supplied.
    """

    instance: Instance
    backend: GenerationBackend
    hb_loop: "HeartbeatLoopProtocol | None"
```

- [ ] **Step 4: Add `_build_start_heartbeat_closure` helper.**

Near `_provision_instance_and_build_backend`, add:

```python
def _build_start_heartbeat_closure(
    *,
    ledger: Ledger,
    provider: ComputeProvider,
    interval: float,
    util_endpoint: "UtilSnapshotEndpoint | None",
    cancel_token: CancelToken | None,
    provider_kind: str | None,
    stall_window_s: float | None,
    stall_gpu_threshold: float,
    stall_cpu_threshold: float,
    restart_loop_window_s: float | None,
    restart_loop_uptime_threshold_s: float,
    factory: Callable[..., HeartbeatLoopProtocol],
) -> Callable[[Instance], HeartbeatLoopProtocol]:
    """C29 — closure that builds + starts a HeartbeatLoop given an instance.

    Captures every HeartbeatLoop kwarg except ``instance_id``. The closure is
    passed into :func:`_provision_instance_and_build_backend` and invoked
    right after the RunPod-status poll succeeds, so the loop ticks throughout
    ``engine.provision`` / ``wait_for_ready`` rather than waiting until
    ``deploy_session`` resumes after provision returns. Steady-state lifetime
    + ``stop()`` remain owned by ``deploy_session``'s finally block.
    """

    def start_heartbeat(inst: Instance) -> HeartbeatLoopProtocol:
        loop = factory(
            ledger=ledger,
            provider=provider,
            instance_id=inst.id,
            interval_s=interval,
            util_endpoint=util_endpoint,
            cancel_token=cancel_token,
            provider_kind=provider_kind,
            stall_window_s=stall_window_s,
            stall_gpu_threshold=stall_gpu_threshold,
            stall_cpu_threshold=stall_cpu_threshold,
            restart_loop_window_s=restart_loop_window_s,
            restart_loop_uptime_threshold_s=restart_loop_uptime_threshold_s,
        )
        loop.start()
        return loop

    return start_heartbeat
```

- [ ] **Step 5: Update `_provision_instance_and_build_backend` signature + body.**

Modify `src/kinoforge/core/orchestrator.py:465` signature to add:

```python
start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None,
```

(cancel_token was added in Task 3.)

Update the return-type annotation from `tuple[Instance, GenerationBackend]` to `ProvisionResult`.

In the body, right BEFORE line 602 (`resolved_engine.attach_get_instance(...)`), add:

```python
# C29: start the heartbeat loop NOW — instance is RUNNING per provider, so
# util_endpoint will return real data, and STALL/RESTART_LOOP predicates can
# fire throughout engine.provision + wait_for_ready instead of waiting until
# deploy_session resumes after provision returns. Failure to build the loop
# falls through to None and the late-start path in deploy_session preserves
# pre-C29 behaviour.
hb_loop: HeartbeatLoopProtocol | None = None
if start_heartbeat is not None:
    try:
        hb_loop = start_heartbeat(instance)
    except Exception:  # noqa: BLE001 — fall-through to late-start
        _log.exception(
            "C29: start_heartbeat closure failed for %s; falling through to "
            "late-start hb_loop construction in deploy_session",
            instance.id,
        )
        hb_loop = None
```

Modify the existing try/except block at orch:604-622 (the `try: _provision_compute_once(...) except (...) destroy + raise`) so it ALSO stops the hb_loop before destroying (and adds `Cancelled` to the except tuple per spec — but the wider Cancelled handling lands in Task 5). For this task, only stop the loop on the existing exception types:

```python
try:
    _provision_compute_once(
        engine=resolved_engine,
        cfg=cfg,
        instance=instance,
        creds=creds,
        store=store,
        state_dir=state_dir,
        capability_key_hex=marker_key_for(cfg, default=key.derive()),
        cfg_dict_override=cfg_dict,
        cancel_token=cancel_token,
    )
except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
    if hb_loop is not None:
        hb_loop.stop()
    resolved_provider.destroy_instance(instance.id)
    raise
```

Replace the existing `return instance, backend` with:

```python
return ProvisionResult(instance=instance, backend=backend, hb_loop=hb_loop)
```

- [ ] **Step 6: Update `deploy_session` to build + pass the closure.**

In `deploy_session` (orch:660+), BEFORE the cache-resolve/profile block, build the closure once:

```python
# C29: build the start_heartbeat closure once per deploy_session. The closure
# is invoked from inside _provision_instance_and_build_backend right after the
# RunPod-status poll succeeds, replacing the pre-C29 post-provision late-start
# block. The closure is None for hosted-engine paths, when heartbeat is
# disabled, or when no provider is resolved.
_start_heartbeat: Callable[[Instance], HeartbeatLoopProtocol] | None = None
_util_endpoint: UtilSnapshotEndpoint | None = None
_interval = cfg.lifecycle().heartbeat_interval_s
if (
    _interval is not None
    and _interval > 0
    and resolved_engine.requires_compute
    and resolved_provider is not None
):
    from kinoforge._adapters import build_util_endpoint_for

    _util_endpoint = (
        build_util_endpoint_for(cfg, creds) if creds is not None else None
    )
    _stall_window_s: float | None = None
    _stall_gpu_threshold = 5.0
    _stall_cpu_threshold = 20.0
    _restart_loop_window_s: float | None = None
    _restart_loop_uptime_threshold_s = 90.0
    _provider_kind: str | None = None
    if cfg.compute is not None:
        _provider_kind = cfg.compute.provider
        lc = cfg.compute.lifecycle
        if lc is not None and lc.stall_reap_enabled:
            _stall_window_s = lc.stall_window_s
            _stall_gpu_threshold = lc.stall_gpu_threshold
            _stall_cpu_threshold = lc.stall_cpu_threshold
        if lc is not None and lc.restart_loop_reap_enabled:
            _restart_loop_window_s = lc.restart_loop_window_s
            _restart_loop_uptime_threshold_s = lc.restart_loop_uptime_threshold_s
    _factory: Callable[..., HeartbeatLoopProtocol] = (
        heartbeat_loop_factory or HeartbeatLoop
    )
    _start_heartbeat = _build_start_heartbeat_closure(
        ledger=Ledger(store=store),
        provider=resolved_provider,
        interval=_interval,
        util_endpoint=_util_endpoint,
        cancel_token=cancel_token,
        provider_kind=_provider_kind,
        stall_window_s=_stall_window_s,
        stall_gpu_threshold=_stall_gpu_threshold,
        stall_cpu_threshold=_stall_cpu_threshold,
        restart_loop_window_s=_restart_loop_window_s,
        restart_loop_uptime_threshold_s=_restart_loop_uptime_threshold_s,
        factory=_factory,
    )
```

(Position this block carefully — it must come AFTER `resolved_engine`, `resolved_provider`, `creds` are all bound, but BEFORE the cache-resolve / profile-discovery branch.)

- [ ] **Step 7: Update both `_provision_instance_and_build_backend` call sites in `deploy_session`.**

At orch:889 (cache-miss/discovery branch):

```python
result = _provision_instance_and_build_backend(
    resolved_engine=resolved_engine,
    resolved_provider=resolved_provider,
    cfg=cfg,
    run_id=run_id,
    key=key,
    creds=creds,
    store=store,
    state_dir=state_dir,
    for_discovery=True,
    tags=tags,
    on_instance_created=_record_then_install,
    start_heartbeat=_start_heartbeat,
    cancel_token=cancel_token,
)
instance, backend, hb_loop = result
```

At orch:922 (cache-hit branch needing backend): same pattern, with `for_discovery=False`. Both assignments populate the outer-scope `hb_loop`.

- [ ] **Step 8: Replace today's lines 972-1033 hb-construction block.**

Remove the lines 978-1033 hb construction (`hb_loop: HeartbeatLoopProtocol | None = None; interval = cfg.lifecycle().heartbeat_interval_s; if ... factory(...); hb_loop.start()`). Replace with:

```python
# C29: hb_loop is now started inside _provision_instance_and_build_backend
# (right after RunPod status=ready) for the cold-boot branches above.
# The caller-supplied-instance branch (lines 883-887, 917-920) keeps the
# pre-C29 late-start behaviour — that path has no boot phase to protect.
if hb_loop is None and _caller_supplied_instance and _start_heartbeat is not None:
    # Caller-supplied: pod is already warm; tick at original cadence.
    try:
        hb_loop = _start_heartbeat(instance)
    except Exception:  # noqa: BLE001
        _log.exception(
            "C29: late-start hb_loop construction failed for caller-supplied %s",
            instance.id if instance is not None else "<no-instance>",
        )
        hb_loop = None
```

Keep the existing `try: Ledger(store=store).touch(instance.id, session_start=time.time())` block (it sits after hb construction).

The `finally: if hb_loop is not None: hb_loop.stop()` at orch:1048-1049 stays untouched.

- [ ] **Step 9: Run new tests + existing heartbeat-construction tests.**

```bash
pixi run pytest tests/core/test_orchestrator_c29_start_heartbeat.py tests/core/test_orchestrator_heartbeat.py tests/core/test_orchestrator_session_claim.py -v
```

Expected: new tests PASS; existing heartbeat-construction tests likely FAIL because they expected hb construction in `deploy_session` post-provision. The implementer audits each failure and either:
  (a) Updates the test to assert the new construction site (closure → invoked from `_provision_instance_and_build_backend` right after `instance.status == "ready"`).
  (b) Leaves a contract-compatible assertion (e.g. "hb_loop is non-None on a HB-enabled cfg by the time deploy_session yields").

Defer the broader existing-test cleanup to Task 6. For Task 4, the bar is: new c29 tests green + no NEW regressions outside heartbeat-construction-timing tests.

- [ ] **Step 10: Commit.**

```bash
git add src/kinoforge/core/orchestrator.py \
       tests/core/test_orchestrator_c29_start_heartbeat.py
pixi run pre-commit run --files \
    src/kinoforge/core/orchestrator.py \
    tests/core/test_orchestrator_c29_start_heartbeat.py
git commit -m "$(cat <<'EOF'
feat(c29): ProvisionResult NamedTuple + start_heartbeat closure pattern

deploy_session builds a start_heartbeat closure capturing every HeartbeatLoop
kwarg except instance_id. _provision_instance_and_build_backend invokes the
closure right after the RunPod-status poll succeeds (orch:596) and before
engine.provision runs, returning ProvisionResult(instance, backend, hb_loop).
deploy_session stores the running loop for steady-state continuation; the
existing finally: hb_loop.stop() block stays unchanged. Caller-supplied-instance
branch keeps pre-C29 late-start behaviour byte-identically (no boot phase to
protect).

Existing heartbeat-construction-timing tests may flip to new assertions in
Task 6 — this commit ships the production behaviour change.

EOF
)"
```

---

### Task 5: `Cancelled` handling in `_provision_instance_and_build_backend`

**Goal:** Operator-Ctrl-C-during-boot path destroys the pod cleanly; hb-reap-during-boot path lets `Cancelled` propagate with idempotent re-destroy.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:604-625` (the try/except around `_provision_compute_once` + the backend build)
- Create: `tests/core/test_orchestrator_c29_cancel_during_boot.py`

**Acceptance Criteria:**
- [ ] Outer `try` around the existing `_provision_compute_once` + `backend = ...` region also catches `Cancelled`.
- [ ] On `Cancelled`: `hb_loop.stop()` is called (if hb_loop is non-None and not already stopped), `destroy_instance(instance.id)` is called (idempotent under RunPod), then `Cancelled` re-raised.
- [ ] New test file covers: (a) cancel_token set mid-wait_for_ready destroys pod, (b) engine.provision raising ProvisionFailed mid-boot stops hb_loop before destroy, (c) hb-reap-during-boot propagates Cancelled.

**Verify:** `pixi run pytest tests/core/test_orchestrator_c29_cancel_during_boot.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/core/test_orchestrator_c29_cancel_during_boot.py`:

```python
"""C29 — Cancelled raised mid-boot destroys the pod (reap or Ctrl-C path)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled, ProvisionFailed
from kinoforge.core.interfaces import Instance


# Implementer: build minimal fixtures mirroring tests/core/test_orchestrator_heartbeat.py
# fake-provider / fake-engine patterns. Each test exercises
# _provision_instance_and_build_backend directly with a concrete cancel_token
# and a fake engine.provision that triggers the failure mode.

def test_cancel_token_set_mid_wait_for_ready_destroys_pod() -> None:
    """Token set during engine.provision → Cancelled propagates → pod destroyed."""
    pytest.skip("plan-skeleton — implementer follows test_orchestrator_heartbeat.py fixture style")


def test_engine_provision_failure_stops_hb_loop_before_destroy() -> None:
    """ProvisionFailed raised → hb_loop.stop() called BEFORE destroy_instance."""
    pytest.skip("plan-skeleton")


def test_hb_reap_during_boot_propagates_cancelled() -> None:
    """hb_loop sets cancel_token + destroys pod; engine.wait_for_ready raises
    Cancelled; helper sees Cancelled (not ProvisionFailed); helper idempotently
    re-destroys (RunPod 404 acceptable)."""
    pytest.skip("plan-skeleton")
```

Implementer fills the skeletons per the fixture patterns in `tests/core/test_orchestrator_heartbeat.py`.

- [ ] **Step 2: Run failing tests.**

```bash
pixi run pytest tests/core/test_orchestrator_c29_cancel_during_boot.py -v
```

Expected: all SKIPPED initially. Implementer must replace each `pytest.skip` with concrete fixtures + assertions before this task ships.

- [ ] **Step 3: Extend the existing except block to catch Cancelled.**

Modify `src/kinoforge/core/orchestrator.py` around the try/except added in Task 4:

```python
try:
    _provision_compute_once(
        engine=resolved_engine,
        cfg=cfg,
        instance=instance,
        creds=creds,
        store=store,
        state_dir=state_dir,
        capability_key_hex=marker_key_for(cfg, default=key.derive()),
        cfg_dict_override=cfg_dict,
        cancel_token=cancel_token,
    )
except (ProvisionFailed, ProvisionTimeout, CapabilityMismatch, ValidationError):
    if hb_loop is not None:
        hb_loop.stop()
    resolved_provider.destroy_instance(instance.id)
    raise
except Cancelled:
    # C29: reap-during-boot OR operator-Ctrl-C path. hb_loop has already
    # destroyed the pod via _maybe_fire_reap → RunPod 404 on re-destroy is
    # idempotent (logged + swallowed by the provider). Re-destroy is load-
    # bearing for the Ctrl-C path where hb_loop did NOT destroy.
    if hb_loop is not None:
        hb_loop.stop()
    try:
        resolved_provider.destroy_instance(instance.id)
    except Exception as destroy_exc:  # noqa: BLE001
        _log.warning(
            "C29: idempotent destroy after Cancelled raised %s for %s",
            destroy_exc,
            instance.id,
        )
    raise
```

Add `from kinoforge.core.errors import Cancelled` to orchestrator.py imports if not already present (the import path is `kinoforge.core.errors` per `src/kinoforge/core/cancel.py:6`).

- [ ] **Step 4: Run failing tests — expect green.**

After implementer fills the `pytest.skip` skeletons:

```bash
pixi run pytest tests/core/test_orchestrator_c29_cancel_during_boot.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_c29_cancel_during_boot.py
pixi run pre-commit run --files \
    src/kinoforge/core/orchestrator.py \
    tests/core/test_orchestrator_c29_cancel_during_boot.py
git commit -m "$(cat <<'EOF'
feat(c29): Cancelled handling in _provision_instance_and_build_backend

Outer except now catches Cancelled raised out of engine.provision /
wait_for_ready (boot-phase reap OR operator Ctrl-C path). Stops hb_loop +
idempotently re-destroys pod before re-raising. RunPod destroy_instance is
404-idempotent so reap-already-destroyed becomes log noise, while Ctrl-C
path is load-bearing.

EOF
)"
```

---

### Task 6: Modified existing tests

**Goal:** Bring the 7 existing test files identified in the spec back to green after the Task 1-5 changes.

**Files:**
- Modify: `tests/core/test_orchestrator_heartbeat.py` (hb construction-timing assertions migrate)
- Modify: `tests/core/test_orchestrator_creds_default.py` (spy returns ProvisionResult)
- Modify: `tests/core/test_batch_creds_default.py` (spy returns ProvisionResult)
- Modify: `tests/engines/test_comfyui_provision_branch.py` (fake `wait_for_ready` signatures)
- Modify: `tests/engines/test_diffusers_provision_branch.py` (fake `wait_for_ready` signatures)
- Audit + likely-modify: `tests/core/test_orchestrator_compute.py`, `tests/core/test_orchestrator_no_reuse.py`, `tests/core/test_orchestrator_session_claim.py`, `tests/core/test_orchestrator_session_fields.py`

**Acceptance Criteria:**
- [ ] `pixi run pytest tests/core/test_orchestrator_heartbeat.py tests/core/test_orchestrator_creds_default.py tests/core/test_batch_creds_default.py tests/engines/test_comfyui_provision_branch.py tests/engines/test_diffusers_provision_branch.py -v` → ALL pass.
- [ ] Auditor files: `pixi run pytest tests/core/test_orchestrator_compute.py tests/core/test_orchestrator_no_reuse.py tests/core/test_orchestrator_session_claim.py tests/core/test_orchestrator_session_fields.py -v` → ALL pass.
- [ ] No regression in test_heartbeat_loop.py / test_heartbeat_loop_util.py / test_ledger_*.py / test_heartbeat_endpoints.py / test_sweeper.py / providers/test_heartbeat_parity.py.

**Verify:** `pixi run pytest tests/core/ tests/engines/ -v 2>&1 | tail -15` → all pass.

**Steps:**

- [ ] **Step 1: Fix the spy return shapes.**

In `tests/core/test_orchestrator_creds_default.py` AND `tests/core/test_batch_creds_default.py`: each test monkeypatches `_provision_instance_and_build_backend` with a `spy` that wraps `real_helper` and returns its result. The old code returned `(instance, backend)` 2-tuple; the new code returns `ProvisionResult`. Update each spy to return the NamedTuple unchanged:

```python
real_helper = orchestrator._provision_instance_and_build_backend

def spy(*args, **kwargs):
    spy.calls.append((args, kwargs))
    result = real_helper(*args, **kwargs)
    # result is a ProvisionResult NamedTuple; unchanged pass-through.
    return result

spy.calls = []
monkeypatch.setattr(orchestrator, "_provision_instance_and_build_backend", spy)
```

If a spy was previously returning a hand-built 2-tuple sentinel, replace with `orchestrator.ProvisionResult(instance=..., backend=..., hb_loop=None)`.

- [ ] **Step 2: Fix the fake `wait_for_ready` signatures.**

In `tests/engines/test_comfyui_provision_branch.py` AND `tests/engines/test_diffusers_provision_branch.py`: each of the 5 fake `wait_for_ready` impls (3 + 2) currently looks like:

```python
def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s):
    ...
```

Update each to:

```python
def wait_for_ready(self, instance, *, http_get, sleep, get_instance, timeout_s, cancel_token=None):  # noqa: ANN001
    ...
```

Same for any `provision` fake that calls `self.wait_for_ready` — accept `cancel_token=None` and forward.

- [ ] **Step 3: Fix `test_orchestrator_heartbeat.py` timing assertions.**

Read the existing tests; identify any that asserted hb_loop is constructed AT a specific point in `deploy_session` (e.g. "factory called after backend is returned"). Migrate to assert the new contract:
- HB-enabled cfg + cold-boot path: `factory` is invoked from inside `_provision_instance_and_build_backend` AFTER `instance.status == "ready"` and BEFORE `_provision_compute_once`. Concrete sentinel: a fake provider's `get_instance` call counter + a fake engine.provision invocation counter, with `factory` invocation timestamped between them.
- HB-enabled cfg + caller-supplied-instance: `factory` invoked from `deploy_session` (post-provision late-start branch). Today's pattern, unchanged.

If existing tests are too tightly coupled to "construction-after-backend", rewrite them to test the externally-observable contract ("hb_loop is non-None + running by the time deploy_session yields") instead.

- [ ] **Step 4: Audit the four "audit + likely-modify" files.**

```bash
for f in tests/core/test_orchestrator_compute.py tests/core/test_orchestrator_no_reuse.py tests/core/test_orchestrator_session_claim.py tests/core/test_orchestrator_session_fields.py; do
    pixi run pytest "$f" -v 2>&1 | tail -10
done
```

For each failure, classify:
  - Construction-timing assumption → update assertion.
  - 2-tuple unpacking → unpack 3-tuple via NamedTuple.
  - Closure-arg-missing fixture → add `start_heartbeat=None` to the test's `_provision_instance_and_build_backend` direct call (when tests bypass `deploy_session`).

- [ ] **Step 5: Run core + engines sweep.**

```bash
pixi run pytest tests/core/ tests/engines/ -v 2>&1 | tail -30
```

Expected: ALL pass.

- [ ] **Step 6: Commit.**

```bash
git add tests/core/test_orchestrator_heartbeat.py \
       tests/core/test_orchestrator_creds_default.py \
       tests/core/test_batch_creds_default.py \
       tests/engines/test_comfyui_provision_branch.py \
       tests/engines/test_diffusers_provision_branch.py \
       tests/core/test_orchestrator_compute.py \
       tests/core/test_orchestrator_no_reuse.py \
       tests/core/test_orchestrator_session_claim.py \
       tests/core/test_orchestrator_session_fields.py
pixi run pre-commit run --files \
    tests/core/test_orchestrator_heartbeat.py \
    tests/core/test_orchestrator_creds_default.py \
    tests/core/test_batch_creds_default.py \
    tests/engines/test_comfyui_provision_branch.py \
    tests/engines/test_diffusers_provision_branch.py \
    tests/core/test_orchestrator_compute.py \
    tests/core/test_orchestrator_no_reuse.py \
    tests/core/test_orchestrator_session_claim.py \
    tests/core/test_orchestrator_session_fields.py
git commit -m "$(cat <<'EOF'
test(c29): update existing tests for new construction-timing + return shape

ProvisionResult NamedTuple replaces (instance, backend) — spies + unpack sites
updated. wait_for_ready fakes accept cancel_token=None for Protocol parity.
Heartbeat-construction-timing assertions migrate to "constructed inside
_provision_instance_and_build_backend after status=ready" semantics. No
production-code change.

EOF
)"
```

---

### Task 7: Full green sweep

**Goal:** Whole `pixi run test` is green; no surprises from the audit's "no change expected" files.

**Files:**
- Read-only test run.
- Possible touch-ups in any test that the audit missed.

**Acceptance Criteria:**
- [ ] `pixi run test` exits 0.
- [ ] Test count matches Task 0 baseline + (~13 new) − (existing fakes still counted).
- [ ] `pixi run lint` + `pixi run typecheck` clean.

**Verify:** `pixi run test 2>&1 | tail -10` → "X passed" with X ≥ baseline + ~13.

**Steps:**

- [ ] **Step 1: Run full test sweep.**

```bash
pixi run test 2>&1 | tee /tmp/c29_full.log | tail -30
```

Expected: green.

- [ ] **Step 2: Run lint + typecheck.**

```bash
pixi run lint && pixi run typecheck
```

Expected: both exit 0.

- [ ] **Step 3: If failures surface, fix in place.**

For any test that fails for a reason not anticipated in Tasks 1-6 (e.g. a hidden `wait_for_ready` impl, a tuple-unpack site missed by the audit), fix the file and stage it. Do NOT chase scope creep — keep the fix surgical and Note it as a deviation in the commit message.

- [ ] **Step 4: Commit any surgical fixups.**

```bash
git add -p <each-file>
pixi run pre-commit run --files <staged-files>
git commit -m "$(cat <<'EOF'
test(c29): green-sweep fixups for sites missed by the audit

[List each file + one-sentence rationale per deviation.]

EOF
)"
```

If no fixups needed, skip this commit.

- [ ] **Step 5: Tag the integration milestone.**

```bash
git log --oneline -10 | head -10
```

Confirm Tasks 1-7 commits are sequential + atomic.

---

### Task 8: RED-scaffold Smoke A (boot-phase STALL_REAP)

**Goal:** Committed RED smoke proving boot-phase STALL_REAP fires when GPU stays at 0 during provision_script sleep. Scaffold lands BEFORE any live spend per CLAUDE.md durability rules.

**Files:**
- Create: `tests/live/test_c29_phase_a_boot_stall_live.py`
- Create: `tests/live/_c29_phase_a_cfg.yaml`

**Acceptance Criteria:**
- [ ] Smoke file exists with `@pytest.mark.live` + `@pytest.mark.xfail(reason="C29 Phase A — live capture pending")`.
- [ ] Cfg YAML at `_c29_phase_a_cfg.yaml`: `heartbeat_interval_s=10`, `stall_window_s=60`, `stall_reap_enabled=true`, `restart_loop_reap_enabled=false`, `boot_timeout=600`, provision_script body = `sleep 600`, image = `kinoforge/wan-comfyui:latest`, lowest-tier GPU.
- [ ] Smoke is committed (RED) — `git log --oneline -1` shows the commit.
- [ ] Smoke is NOT executed in this task.

**Verify:** `pixi run pytest tests/live/test_c29_phase_a_boot_stall_live.py --collect-only` → 1 test collected.

**Steps:**

- [ ] **Step 1: Read existing C26/C27 phase-A live smokes for the template.**

```bash
cat tests/live/test_c26_phase_a_stall_detection_live.py | head -60
```

Match the project's live-smoke skeleton: env-var preflight, cfg-YAML loading, `pixi run preflight` precondition, RunPod cleanup atexit, sidecar evidence JSON.

- [ ] **Step 2: Write `_c29_phase_a_cfg.yaml`.**

```yaml
# tests/live/_c29_phase_a_cfg.yaml
# C29 Phase A — boot-phase STALL_REAP smoke.
# Provision script does NOT launch ComfyUI; pod sleeps 600s with GPU idle.
# Expected: hb_loop starts after RunPod status=ready (~90s), ticks at
# heartbeat_interval_s=10 with gpu_util_percent < 5, counter reaches
# stall_window_s/heartbeat_interval_s = 6 ticks ≈ 60s, STALL_REAP fires,
# pod destroyed at ~150s total wall-clock.
engine: comfyui
compute:
  provider: runpod
  image: kinoforge/wan-comfyui:latest
  hardware:
    gpu_count: 1
    gpu_type: "RTX 3060"  # or whatever the lowest-cost RunPod tier supports
  lifecycle:
    heartbeat_interval_s: 10
    boot_timeout_s: 600
    idle_timeout_s: 300
    max_lifetime_s: 1800
    stall_reap_enabled: true
    stall_window_s: 60
    stall_gpu_threshold: 5.0
    stall_cpu_threshold: 20.0
    restart_loop_reap_enabled: false
  provision_script_override: |
    #!/bin/bash
    set -euxo pipefail
    echo "C29 Phase A — sleeping 600s with GPU idle"
    sleep 600
diagnostic_mode: false
```

Confirm cfg shape against `tests/live/_c28_phase_a_*.yaml` patterns — copy any required wrapping keys.

- [ ] **Step 3: Write `test_c29_phase_a_boot_stall_live.py`.**

```python
"""C29 Phase A — boot-phase STALL_REAP live smoke.

Prove that a pod whose provision_script keeps the GPU idle for the entire
boot phase reaps via STALL_REAP before boot_timeout fires. Concretely:

* heartbeat_interval_s=10, stall_window_s=60, stall_reap_enabled=true.
* Provision script: ``sleep 600`` (no ComfyUI launch, no GPU work).
* Expected: pod status=ready at ~30-90s, hb_loop starts immediately, 6+ ticks
  of gpu_util_percent < 5, STALL_REAP fires at counter=6, pod destroyed by
  ~150s, total cost ≤ $0.10.

Pass: ledger consecutive_low_util_count >= 6; logs contain
``STALL_REAP fired for <pod-id>``; provider.get_instance returns terminated
within 30s of reap; total spend < $0.20.

RED until executed against live RunPod. Marked xfail so CI does not run it
without the explicit live env vars + budget allowance.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from kinoforge.core.config import Config
from kinoforge.core.orchestrator import deploy_session
from kinoforge.core.storage.local import LocalArtifactStore
from kinoforge.core.lifecycle import Ledger

_CFG_PATH = Path(__file__).parent / "_c29_phase_a_cfg.yaml"
_EVIDENCE_PATH = Path(__file__).parent / "_c29_phase_a_evidence.json"


pytestmark = [
    pytest.mark.live,
    pytest.mark.xfail(reason="C29 Phase A — live capture pending"),
]


def _require_live_env() -> None:
    """Skip the smoke unless KINOFORGE_LIVE=1 and creds are present."""
    if os.environ.get("KINOFORGE_LIVE") != "1":
        pytest.skip("set KINOFORGE_LIVE=1 to run live smokes")
    for var in ("RUNPOD_API_KEY", "HF_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"missing {var}")


def test_c29_phase_a_boot_stall_reap() -> None:
    """Boot-phase STALL_REAP fires before boot_timeout."""
    _require_live_env()

    cfg_dict = yaml.safe_load(_CFG_PATH.read_text())
    cfg = Config.model_validate(cfg_dict)

    store = LocalArtifactStore(Path(".kinoforge_c29_phase_a"))
    ledger = Ledger(store=store)

    evidence: dict[str, object] = {
        "phase": "C29-A",
        "start_unix": time.time(),
    }

    start_wall = time.monotonic()
    pod_id: str | None = None

    with pytest.raises(Exception):  # ANY exception — destroy_instance + propagation
        with deploy_session(cfg, store=store) as session:
            pod_id = session.instance.id if session.instance is not None else None
            evidence["pod_id"] = pod_id
            # Hold the session open until reap fires (cancel_token tripped).
            time.sleep(600)

    elapsed = time.monotonic() - start_wall
    evidence["elapsed_s"] = elapsed
    evidence["end_unix"] = time.time()

    # Read final ledger state for the pod.
    if pod_id is not None:
        entry = ledger.read(pod_id) or {}
        evidence["final_ledger"] = entry
        assert entry.get("consecutive_low_util_count", 0) >= 6, (
            f"expected consecutive_low_util_count >= 6, got {entry.get('consecutive_low_util_count')}"
        )

    # Reap must fire before boot_timeout (600s); typical ~150s.
    assert elapsed < 300, (
        f"expected reap within 300s, got {elapsed:.0f}s — boot_timeout fallback may have fired instead"
    )

    _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True))
```

Adjust import paths + Config shape to match the actual project APIs (read `tests/live/test_c26_phase_a_stall_detection_live.py` for the working pattern).

- [ ] **Step 4: Confirm collection.**

```bash
pixi run pytest tests/live/test_c29_phase_a_boot_stall_live.py --collect-only
```

Expected: 1 test collected.

- [ ] **Step 5: Commit RED.**

```bash
git add tests/live/test_c29_phase_a_boot_stall_live.py tests/live/_c29_phase_a_cfg.yaml
pixi run pre-commit run --files \
    tests/live/test_c29_phase_a_boot_stall_live.py \
    tests/live/_c29_phase_a_cfg.yaml
git commit -m "$(cat <<'EOF'
live(c29): A0 Phase A RED scaffold — boot-phase STALL_REAP smoke

cfg: heartbeat_interval_s=10, stall_window_s=60, stall_reap_enabled=true,
provision_script=sleep 600. Expected: STALL_REAP fires at ~150s wall-clock
before boot_timeout=600s. Cost ≤ $0.10. Committed RED per CLAUDE.md
durability rule (any tool whose purpose is to drive live cloud spend MUST
be committed before invocation).

EOF
)"
```

---

### Task 9: Live Smoke A execution

**Goal:** Run Smoke A against real RunPod. Capture evidence. Smoke flips from xfail → pass.

**Files:**
- Read: `tests/live/_c29_phase_a_evidence.json` (sidecar produced by the smoke).
- Modify: `tests/live/test_c29_phase_a_boot_stall_live.py` (remove xfail after pass).

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 (zero active RunPod pods, clean tree, creds loaded).
- [ ] Smoke runs against live RunPod; STALL_REAP fires.
- [ ] `_c29_phase_a_evidence.json` populated with `pod_id`, `elapsed_s < 300`, `final_ledger.consecutive_low_util_count >= 6`.
- [ ] xfail marker removed from the test.
- [ ] Total spend ≤ $0.20.

**Verify:**
```bash
KINOFORGE_LIVE=1 pixi run pytest tests/live/test_c29_phase_a_boot_stall_live.py -v 2>&1 | tail -10
```
→ test PASSES (no longer XFAIL).

**Steps:**

- [ ] **Step 1: Preflight.**

```bash
pixi run preflight
```

Expected: exit 0. Any failure (active pods, dirty tree, missing creds) blocks the spend.

- [ ] **Step 2: Execute the smoke.**

```bash
KINOFORGE_LIVE=1 pixi run pytest tests/live/test_c29_phase_a_boot_stall_live.py -v 2>&1 | tee /tmp/c29_smoke_a.log
```

Expected wall-clock: ~150-200s. Cost ≤ $0.10. The smoke raises in the `with pytest.raises(Exception)` block when reap fires, then exits cleanly.

- [ ] **Step 3: Capture evidence + verify atexit cleanup.**

Read `_c29_phase_a_evidence.json`:

```bash
cat tests/live/_c29_phase_a_evidence.json
```

Confirm `final_ledger.consecutive_low_util_count >= 6` and `elapsed_s < 300`.

```bash
RUNPOD_API_KEY=$RUNPOD_API_KEY pixi run python -c "
from kinoforge.providers.runpod import RunPodProvider
import os
p = RunPodProvider(api_key=os.environ['RUNPOD_API_KEY'])
print([i for i in p.list_instances() if i.status not in ('terminated','stopped')])
"
```

Expected: empty list (atexit cleanup honored).

- [ ] **Step 4: Remove xfail marker.**

In `tests/live/test_c29_phase_a_boot_stall_live.py`, change:

```python
pytestmark = [
    pytest.mark.live,
    pytest.mark.xfail(reason="C29 Phase A — live capture pending"),
]
```

to:

```python
pytestmark = [pytest.mark.live]
```

- [ ] **Step 5: Commit evidence + marker removal.**

```bash
git add tests/live/test_c29_phase_a_boot_stall_live.py tests/live/_c29_phase_a_evidence.json
pixi run pre-commit run --files \
    tests/live/test_c29_phase_a_boot_stall_live.py \
    tests/live/_c29_phase_a_evidence.json
git commit -m "$(cat <<'EOF'
live(c29): A1 Phase A PASS — boot-phase STALL_REAP fires at ~150s

Evidence sidecar captured: consecutive_low_util_count=$N, elapsed_s=$X,
total spend ~$0.NN. STALL_REAP fired BEFORE boot_timeout=600s, proving the
C29 boot-phase protection. xfail marker removed.

EOF
)"
```

(Substitute concrete numbers from evidence.)

---

### Task 10: RED-scaffold Smoke B (boot-phase RESTART_LOOP_REAP)

**Goal:** Committed RED smoke proving boot-phase RESTART_LOOP_REAP fires when container restarts every ~10s during provision.

**Files:**
- Create: `tests/live/test_c29_phase_b_boot_restart_loop_live.py`
- Create: `tests/live/_c29_phase_b_cfg.yaml`

**Acceptance Criteria:**
- [ ] Smoke file marked `xfail` + `live`.
- [ ] Cfg: `heartbeat_interval_s=10`, `restart_loop_window_s=60`, `restart_loop_reap_enabled=true`, `restart_loop_uptime_threshold_s=30`, `stall_reap_enabled=false`. Provision script: `exit 1`. `restart_policy=always`.
- [ ] Smoke committed RED.

**Verify:** `pixi run pytest tests/live/test_c29_phase_b_boot_restart_loop_live.py --collect-only` → 1 test collected.

**Steps:**

- [ ] **Step 1: Copy Phase A skeleton + mutate config.**

Create `tests/live/_c29_phase_b_cfg.yaml`:

```yaml
# tests/live/_c29_phase_b_cfg.yaml
engine: comfyui
compute:
  provider: runpod
  image: kinoforge/wan-comfyui:latest
  hardware:
    gpu_count: 1
    gpu_type: "RTX 3060"
  lifecycle:
    heartbeat_interval_s: 10
    boot_timeout_s: 600
    idle_timeout_s: 300
    max_lifetime_s: 1800
    stall_reap_enabled: false
    restart_loop_reap_enabled: true
    restart_loop_window_s: 60
    restart_loop_uptime_threshold_s: 30
  restart_policy: always  # required for the container-restart loop
  provision_script_override: |
    #!/bin/bash
    echo "C29 Phase B — exit 1, container restart every ~10s"
    exit 1
diagnostic_mode: false
```

- [ ] **Step 2: Write `test_c29_phase_b_boot_restart_loop_live.py`.**

Mirror Phase A test; change assertion:

```python
assert entry.get("consecutive_low_uptime_count", 0) >= 6, (
    f"expected consecutive_low_uptime_count >= 6, got "
    f"{entry.get('consecutive_low_uptime_count')}"
)
```

and reason-message: `"C29 Phase B — live capture pending"`.

- [ ] **Step 3: Confirm collection.**

```bash
pixi run pytest tests/live/test_c29_phase_b_boot_restart_loop_live.py --collect-only
```

- [ ] **Step 4: Commit RED.**

```bash
git add tests/live/test_c29_phase_b_boot_restart_loop_live.py tests/live/_c29_phase_b_cfg.yaml
pixi run pre-commit run --files \
    tests/live/test_c29_phase_b_boot_restart_loop_live.py \
    tests/live/_c29_phase_b_cfg.yaml
git commit -m "$(cat <<'EOF'
live(c29): B0 Phase B RED scaffold — boot-phase RESTART_LOOP_REAP smoke

cfg: restart_loop_window_s=60, restart_loop_uptime_threshold_s=30, provision
script exit 1 with restart_policy=always. Expected: RESTART_LOOP_REAP fires
at ~150s. Cost ≤ $0.10. Committed RED per durability rule.

EOF
)"
```

---

### Task 11: Live Smoke B execution

**Goal:** Run Smoke B against real RunPod; flip xfail → pass.

**Files:**
- `tests/live/_c29_phase_b_evidence.json`
- `tests/live/test_c29_phase_b_boot_restart_loop_live.py`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0.
- [ ] RESTART_LOOP_REAP fires; `consecutive_low_uptime_count >= 6`.
- [ ] xfail removed.

**Verify:**
```bash
KINOFORGE_LIVE=1 pixi run pytest tests/live/test_c29_phase_b_boot_restart_loop_live.py -v
```
→ test passes.

**Steps:**

- [ ] **Step 1: Preflight.** Same as Task 9 Step 1.
- [ ] **Step 2: Execute smoke.** Same shape as Task 9 Step 2.
- [ ] **Step 3: Verify evidence + atexit cleanup.** Same.
- [ ] **Step 4: Remove xfail.** Same.
- [ ] **Step 5: Commit.**

```bash
git commit -m "$(cat <<'EOF'
live(c29): B1 Phase B PASS — boot-phase RESTART_LOOP_REAP fires at ~150s

Evidence: consecutive_low_uptime_count=$N, elapsed=$X, spend ~$0.NN.

EOF
)"
```

---

### Task 12: RED-scaffold Smoke C (`kinoforge status` during boot)

**Goal:** Committed RED smoke proving `kinoforge status --id <pod>` returns non-empty liveness metrics within 2 * heartbeat_interval_s of pod-ready.

**Files:**
- Create: `tests/live/test_c29_phase_c_boot_status_live.py`
- Create: `tests/live/_c29_phase_c_cfg.yaml`

**Acceptance Criteria:**
- [ ] Smoke marked xfail + live.
- [ ] Cfg: `heartbeat_interval_s=10`, all reap disabled, `provision_script=sleep 120`.
- [ ] Test polls `kinoforge status --id <pod>` from a child process and asserts 4 ledger fields populated.

**Verify:** `pixi run pytest tests/live/test_c29_phase_c_boot_status_live.py --collect-only` → 1 test collected.

**Steps:**

- [ ] **Step 1: Write `_c29_phase_c_cfg.yaml`.**

```yaml
engine: comfyui
compute:
  provider: runpod
  image: kinoforge/wan-comfyui:latest
  hardware:
    gpu_count: 1
    gpu_type: "RTX 3060"
  lifecycle:
    heartbeat_interval_s: 10
    boot_timeout_s: 600
    idle_timeout_s: 300
    max_lifetime_s: 1800
    stall_reap_enabled: false
    restart_loop_reap_enabled: false
  provision_script_override: |
    #!/bin/bash
    echo "C29 Phase C — sleep 120 to observe boot-phase status fields"
    sleep 120
diagnostic_mode: false
```

- [ ] **Step 2: Write `test_c29_phase_c_boot_status_live.py`.**

```python
"""C29 Phase C — kinoforge status shows liveness during boot.

Within 2 * heartbeat_interval_s of pod-ready, status must report:
heartbeat_thread_tick, util_thread_tick, last_gpu_util_percent,
last_uptime_seconds — all non-null.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from kinoforge.core.config import Config
from kinoforge.core.orchestrator import deploy_session
from kinoforge.core.storage.local import LocalArtifactStore

_CFG_PATH = Path(__file__).parent / "_c29_phase_c_cfg.yaml"
_EVIDENCE_PATH = Path(__file__).parent / "_c29_phase_c_evidence.json"


pytestmark = [
    pytest.mark.live,
    pytest.mark.xfail(reason="C29 Phase C — live capture pending"),
]


def _require_live_env() -> None:
    if os.environ.get("KINOFORGE_LIVE") != "1":
        pytest.skip("set KINOFORGE_LIVE=1 to run live smokes")
    for var in ("RUNPOD_API_KEY", "HF_TOKEN"):
        if not os.environ.get(var):
            pytest.skip(f"missing {var}")


def test_c29_phase_c_status_during_boot() -> None:
    _require_live_env()

    cfg_dict = yaml.safe_load(_CFG_PATH.read_text())
    cfg = Config.model_validate(cfg_dict)
    store = LocalArtifactStore(Path(".kinoforge_c29_phase_c"))

    evidence: dict[str, object] = {"phase": "C29-C", "start_unix": time.time()}
    pod_id: str | None = None

    with deploy_session(cfg, store=store) as session:
        pod_id = session.instance.id if session.instance is not None else None
        assert pod_id is not None
        evidence["pod_id"] = pod_id

        # Wait 2 * heartbeat_interval_s past status=ready.
        time.sleep(2 * 10 + 5)  # 25s after yield

        # Invoke kinoforge status --id <pod> from a sibling process.
        result = subprocess.run(
            ["pixi", "run", "kinoforge", "status", "--id", pod_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
        evidence["status_stdout"] = result.stdout
        evidence["status_returncode"] = result.returncode

        # Status output should contain all 4 liveness markers.
        for marker in (
            "heartbeat_thread_tick",
            "util_thread_tick",
            "last_gpu_util_percent",
            "last_uptime_seconds",
        ):
            assert marker in result.stdout, (
                f"expected {marker!r} in status output, got: {result.stdout}"
            )

        # Eagerly tear down — Phase C is purely observational.
        from kinoforge.providers.runpod import RunPodProvider
        if session.provider is not None:
            session.provider.destroy_instance(pod_id)

    evidence["end_unix"] = time.time()
    _EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True))
```

Adjust the `kinoforge status` invocation to match the real CLI shape (read `src/kinoforge/cli/_commands.py` `cmd_status` definition).

- [ ] **Step 3: Confirm collection.**

```bash
pixi run pytest tests/live/test_c29_phase_c_boot_status_live.py --collect-only
```

- [ ] **Step 4: Commit RED.**

```bash
git add tests/live/test_c29_phase_c_boot_status_live.py tests/live/_c29_phase_c_cfg.yaml
pixi run pre-commit run --files \
    tests/live/test_c29_phase_c_boot_status_live.py \
    tests/live/_c29_phase_c_cfg.yaml
git commit -m "$(cat <<'EOF'
live(c29): C0 Phase C RED scaffold — kinoforge status shows boot-phase liveness

cfg: heartbeat_interval_s=10, reap disabled, sleep 120. Test polls
`kinoforge status --id` after 25s and asserts 4 ledger liveness fields
populated. Cost ≤ $0.10. Committed RED per durability rule.

EOF
)"
```

---

### Task 13: Live Smoke C execution

**Goal:** Run Smoke C against real RunPod; flip xfail → pass.

**Files:**
- `tests/live/_c29_phase_c_evidence.json`
- `tests/live/test_c29_phase_c_boot_status_live.py`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0.
- [ ] 4 liveness markers present in status output.
- [ ] xfail removed.

**Verify:**
```bash
KINOFORGE_LIVE=1 pixi run pytest tests/live/test_c29_phase_c_boot_status_live.py -v
```
→ pass.

**Steps:**

- [ ] **Step 1: Preflight.**
- [ ] **Step 2: Execute smoke.**
- [ ] **Step 3: Verify evidence + cleanup.**
- [ ] **Step 4: Remove xfail.**
- [ ] **Step 5: Commit.**

```bash
git commit -m "$(cat <<'EOF'
live(c29): C1 Phase C PASS — kinoforge status returns boot-phase liveness

Evidence: 4 markers present in status output at t=25s after pod-ready.
Total spend ~$0.NN.

EOF
)"
```

---

### Task 14: Close C29

**Goal:** PROGRESS.md updated, C29 marked CLOSED with evidence pointers; `successful-generations.md` left untouched (C29 introduces no new capability axis).

**Files:**
- Modify: `PROGRESS.md` (C29 backlog entry → CLOSED + evidence summary)

**Acceptance Criteria:**
- [ ] PROGRESS.md §C C29 entry shows status CLOSED with the three smoke-evidence paths cited.
- [ ] One-line summary of behaviour change for future readers.

**Verify:** `rg -n 'C29' PROGRESS.md | head -10` → entry shows CLOSED, evidence paths cited.

**Steps:**

- [ ] **Step 1: Update PROGRESS.md.**

Locate the C29 backlog entry (around line 203 per earlier grep) and rewrite as:

```markdown
- **C29. Heartbeat starts BEFORE wait_for_ready (boot-phase protection). CLOSED.**
  Heartbeat construction hoisted into a `start_heartbeat` closure built in
  `deploy_session` and invoked from `_provision_instance_and_build_backend`
  right after the RunPod-status poll succeeds (orch:596), before
  `engine.provision`. `GenerationEngine.wait_for_ready` gained an optional
  `cancel_token` kwarg so boot-phase reaps (or operator Ctrl-C) raise
  `Cancelled` cleanly. Helper returns a `ProvisionResult(instance, backend,
  hb_loop)` NamedTuple. Caller-supplied-instance branch keeps pre-C29
  late-start behaviour byte-identically. Evidence:
  - Smoke A `tests/live/_c29_phase_a_evidence.json`: STALL_REAP fired at
    ~$T_A$s with `consecutive_low_util_count=$N_A$`.
  - Smoke B `tests/live/_c29_phase_b_evidence.json`: RESTART_LOOP_REAP fired
    at ~$T_B$s with `consecutive_low_uptime_count=$N_B$`.
  - Smoke C `tests/live/_c29_phase_c_evidence.json`: `kinoforge status`
    returned 4 liveness markers at t=25s after pod-ready.
  Spec: `docs/superpowers/specs/2026-06-14-c29-heartbeat-earlier-design.md`.
  Plan: `docs/superpowers/plans/2026-06-14-c29-heartbeat-earlier.md`.
```

Substitute the concrete `$T_x$` and `$N_x$` from the evidence files.

- [ ] **Step 2: Commit.**

```bash
git add PROGRESS.md
pixi run pre-commit run --files PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(c29): C29 CLOSED — boot-phase heartbeat protection shipped

Three live smokes pass: STALL_REAP + RESTART_LOOP_REAP fire during boot phase
(~150s vs former boot_timeout=600s budget leak), and `kinoforge status` shows
liveness during boot. Spec + plan paths cited; per-smoke evidence sidecars
captured.

EOF
)"
```

- [ ] **Step 3: Final test sweep.**

```bash
pixi run test 2>&1 | tail -5
```

Expected: green. (Live smokes excluded by default — only run with `KINOFORGE_LIVE=1`.)

---

## Plan Self-Review

**Spec coverage:**
- §1 Loop start point → Task 4 Step 5.
- §2 Construction owner closure → Task 4 Step 4.
- §3 ProvisionResult return shape → Task 4 Step 3.
- §4 deploy_session wiring → Task 4 Steps 6-8.
- §5 Engine ABI cancel_token → Tasks 1, 2.
- §6 Cancelled handling → Task 5.
- Test surface §Modified → Task 6.
- Test surface §New unit tests → Tasks 1, 4, 5.
- Live smokes A/B/C → Tasks 8-13.
- Backward-compat §Operator runbook addendum → captured in spec, not duplicated in plan; referenced by Task 14 PROGRESS.md entry.
- Plan-phase entry contract §steps 1-10 → mapped to Tasks 0-14 (the spec's 10 steps fit inside 15 plan tasks for finer atomicity).

**Type consistency:**
- `ProvisionResult` named identically across Tasks 4-6.
- `start_heartbeat` kwarg named identically across orchestrator + tests.
- `_build_start_heartbeat_closure` helper named once, referenced from Task 4 only.
- `cancel_token` kwarg named identically across Engine Protocol + 3 impls + provision + `_provision_compute_once` + `_provision_instance_and_build_backend`.
- `HeartbeatLoopProtocol` (not `HeartbeatLoop`) used in NamedTuple field type — keeps the existing factory-seam contract.

**Placeholder scan:** No "TBD", "TODO", or "implement later" sentences. Plan-skeleton `pytest.skip` placeholders in Tasks 4 + 5 are explicitly marked + the implementer is given the fixture pattern to follow (`tests/core/test_orchestrator_heartbeat.py`).

**User-gate analysis:** Live smokes match `verifyverb` + `proof` + `Scope` (RED-before-spend) keywords, but user memory `feedback_autonomous_no_gates` overrides: "live smokes pre-authorized up to $20 session budget. Mechanical preflight checks only." No `userGate: true` tags. The `pixi run preflight` step in Tasks 9/11/13 is the mechanical gate.

---

## Dependencies (set via TaskUpdate addBlockedBy after creation)

```
Task 0  → no deps
Task 1  → blockedBy: 0
Task 2  → blockedBy: 1
Task 3  → blockedBy: 2
Task 4  → blockedBy: 3
Task 5  → blockedBy: 4
Task 6  → blockedBy: 5
Task 7  → blockedBy: 6
Task 8  → blockedBy: 7   (RED scaffold needs unit-test-green code)
Task 9  → blockedBy: 8   (live execute needs RED commit)
Task 10 → blockedBy: 9
Task 11 → blockedBy: 10
Task 12 → blockedBy: 11
Task 13 → blockedBy: 12
Task 14 → blockedBy: 13
```
