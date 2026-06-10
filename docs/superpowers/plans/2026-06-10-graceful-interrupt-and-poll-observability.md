# Graceful interrupt + ComfyUI poll observability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Ctrl-C` during `kinoforge generate` return the shell cleanly within ~30 s, never leak a pod silently, and surface a per-tick ComfyUI poll log so the next stall self-diagnoses.

**Architecture:** Add a `CancelToken` (thin `threading.Event` wrapper) + a `Cancelled` error. Thread the token through `generate` → `deploy_session` → `pool.submit` → `backend.submit/result` via a defaulted kwarg (sentinel `_NULL_TOKEN`) so library callers and existing tests are unaffected. CLI installs a `SIGINT` handler that sets the token on first press and restores `SIG_DFL` on second. `ConcurrentPool.close` gains a bounded watchdog so a wedged worker can no longer block shutdown forever. `ComfyUIBackend.result` polls with a structured per-tick INFO line and a hard `poll_timeout_s` upper bound. Orchestrator stage-loop except arms gain a `(KeyboardInterrupt, Cancelled)` clause that logs a `WARN` naming the surviving pod ID + `kinoforge reap` recovery command — the pod is NOT destroyed on interrupt (warm-reuse intent preserved per commit `3bc6473`).

**Tech Stack:** Python 3.13, stdlib `signal` + `threading` + `concurrent.futures`, pydantic v2 (config), pytest, `caplog`.

**Spec:** `docs/superpowers/specs/2026-06-10-graceful-interrupt-and-poll-observability-design.md`.

---

## File structure

| Created | Responsibility |
|---|---|
| `src/kinoforge/core/cancel.py` | `CancelToken` + module-level `_NULL_TOKEN` sentinel. |
| `tests/core/test_cancel.py` | Unit tests for `CancelToken` set / wait / raise_if_set. |
| `tests/core/test_pool_cancel.py` | `ConcurrentPool.close(cancel_pending=True, timeout=...)` bounded shutdown. |
| `tests/engines/test_comfyui_cancel.py` | `ComfyUIBackend.result` honors a pre-set token. |
| `tests/engines/test_comfyui_timeout.py` | Hard `poll_timeout_s` upper bound. |
| `tests/engines/test_comfyui_poll_log.py` | Per-tick INFO log structure. |
| `tests/engines/test_remote_submit_poll_cancel.py` | `RemoteSubmitPollBackend.result` honors the token (covers Replicate / Runway / Luma / Fal). |
| `tests/core/test_orchestrator_interrupt.py` | Stage-loop `KeyboardInterrupt` arm — no destroy, WARN with pod ID. |
| `tests/core/test_orchestrator_cancelled.py` | Stage-loop `Cancelled` arm — same assertions. |
| `tests/cli/test_sigint_handler.py` | Two-press handler — first sets token, second re-raises. |

| Modified | Responsibility |
|---|---|
| `src/kinoforge/core/errors.py` | Add `Cancelled(KinoforgeError)`. |
| `src/kinoforge/core/__init__.py` | Re-export `CancelToken`, `Cancelled`. |
| `src/kinoforge/core/interfaces.py` | `BackendPool.submit` + `GenerationBackend.submit` / `.result` gain `cancel_token=None` kwarg. |
| `src/kinoforge/core/pool.py` | `SequentialPool.submit` accepts + forwards token; `ConcurrentPool.submit` forwards; `close()` gains `cancel_pending` + `timeout` kwargs + watchdog. |
| `src/kinoforge/core/remote_backend.py` | `submit` / `result` accept token; poll loop honors it. |
| `src/kinoforge/engines/comfyui/__init__.py` | `ComfyUIBackend.submit` / `.result` accept token; `result` poll loop adds structured per-tick log, hard timeout, interruptible sleep. `ComfyUIEngine.backend` plumbs `poll_timeout_s` from cfg. |
| `src/kinoforge/engines/fake/__init__.py` | `FakeBackend.submit` / `.result` accept ignored token kwarg (ABC compliance). |
| `src/kinoforge/engines/diffusers/__init__.py` | Same. |
| `src/kinoforge/engines/hosted/__init__.py` | Same. |
| `src/kinoforge/engines/bedrock_video/__init__.py` | Same. |
| `src/kinoforge/core/config.py` | `ComfyUIEngineConfig.poll_timeout_s: float = 600.0`. |
| `src/kinoforge/pipeline/generate_clip.py` | `GenerateClipStage.__init__` accepts `cancel_token=None`; forwards into `pool.submit`. |
| `src/kinoforge/core/orchestrator.py` | `generate()` + `deploy_session()` accept `cancel_token`; stage-loop except arms catch `(KeyboardInterrupt, Cancelled)`; `deploy_session.__exit__` passes `cancel_pending` to `pool.close`. |
| `src/kinoforge/cli/context.py` | `SessionContext.cancel_token: CancelToken` field. |
| `src/kinoforge/cli/_main.py` | Install SIGINT handler in `main()` before `_cmd_generate` / `_cmd_batch` dispatch. |
| `src/kinoforge/cli/_commands.py` | `_cmd_generate` + `_cmd_batch` thread `ctx.cancel_token` into `generate()`. |
| `README.md` | Note new `poll_timeout_s` knob + interrupt UX. |
| `PROGRESS.md` | Append Phase 50 closeout entry. |

---

## Task 0: `CancelToken` + `Cancelled` foundation

**Goal:** Add the `CancelToken` primitive and `Cancelled` exception with no callers yet. Pure foundation.

**Files:**
- Create: `src/kinoforge/core/cancel.py`
- Modify: `src/kinoforge/core/errors.py`
- Modify: `src/kinoforge/core/__init__.py`
- Create: `tests/core/test_cancel.py`

**Acceptance Criteria:**
- [ ] `from kinoforge.core import CancelToken, Cancelled` works.
- [ ] `CancelToken().is_set()` returns `False` initially; after `.set()` returns `True`.
- [ ] `CancelToken().wait(0.05)` returns `False` (timed out); when `set()` mid-wait from a helper thread, returns `True` promptly (< 0.5 s).
- [ ] `CancelToken().raise_if_set()` is a no-op when unset; raises `Cancelled` when set.
- [ ] Module exposes `_NULL_TOKEN` — a `CancelToken` that is never set; documented as the default-kwarg sentinel.
- [ ] `Cancelled` subclasses `KinoforgeError`.

**Verify:** `pixi run pytest tests/core/test_cancel.py -v` → 4 PASS.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/core/test_cancel.py
"""Unit tests for CancelToken + Cancelled."""

from __future__ import annotations

import threading
import time

import pytest

from kinoforge.core import CancelToken, Cancelled
from kinoforge.core.cancel import _NULL_TOKEN


def test_initial_state_not_set() -> None:
    """A fresh CancelToken reports is_set() == False."""
    token = CancelToken()
    assert token.is_set() is False


def test_set_flips_is_set() -> None:
    """set() makes is_set() return True."""
    token = CancelToken()
    token.set()
    assert token.is_set() is True


def test_wait_returns_false_on_timeout() -> None:
    """wait(timeout) on an unset token returns False after the timeout."""
    token = CancelToken()
    start = time.monotonic()
    result = token.wait(0.05)
    elapsed = time.monotonic() - start
    assert result is False
    assert elapsed >= 0.04


def test_wait_returns_true_when_set_mid_wait() -> None:
    """wait() unblocks promptly when another thread calls set()."""
    token = CancelToken()

    def _setter() -> None:
        time.sleep(0.05)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()
    start = time.monotonic()
    result = token.wait(1.0)
    elapsed = time.monotonic() - start
    assert result is True
    assert elapsed < 0.5, f"wait() took {elapsed:.2f}s — should have returned promptly"


def test_raise_if_set_noop_when_unset() -> None:
    """raise_if_set on an unset token does nothing."""
    token = CancelToken()
    token.raise_if_set()  # must not raise


def test_raise_if_set_raises_when_set() -> None:
    """raise_if_set on a set token raises Cancelled."""
    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        token.raise_if_set()


def test_null_token_is_never_set() -> None:
    """The module-level _NULL_TOKEN sentinel is never set, even after import."""
    assert _NULL_TOKEN.is_set() is False
```

- [ ] **Step 2: Run test to verify it fails.**

```
pixi run pytest tests/core/test_cancel.py -v
```

Expected: collection error or `ImportError` — `CancelToken` / `Cancelled` not defined yet.

- [ ] **Step 3: Add `Cancelled` to `core/errors.py`.**

Append after the existing `KinoforgeError` subclasses (anywhere in the file, alphabetical or by use; placement after `BudgetExceeded` keeps related "operation-aborted" errors together):

```python
class Cancelled(KinoforgeError):
    """Raised when a CancelToken is set mid-operation.

    Backends honoring cooperative cancellation raise this from their
    submit/result methods so the orchestrator can distinguish an operator
    interrupt from a real failure.
    """
```

- [ ] **Step 4: Create `src/kinoforge/core/cancel.py`.**

```python
"""Cooperative-cancellation primitive used across orchestrator + backends."""

from __future__ import annotations

import threading

from kinoforge.core.errors import Cancelled


class CancelToken:
    """Thin :class:`threading.Event` wrapper used to request cancellation.

    The class deliberately exposes a narrow surface so backends never grab
    the underlying :class:`~threading.Event`. Backends should treat the
    token as opaque: call :meth:`raise_if_set` before any blocking I/O and
    use :meth:`wait` in place of :func:`time.sleep` so an inter-poll sleep
    can be interrupted promptly.

    Thread-safe by virtue of :class:`threading.Event`.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        """Request cancellation.

        Safe to call from any thread (signal handler, sibling worker, etc.).
        """
        self._event.set()

    def is_set(self) -> bool:
        """Return True if cancellation has been requested."""
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block for up to ``timeout`` seconds.

        Returns ``True`` if the token was set during the wait, ``False`` if
        the timeout expired first. Drop-in replacement for ``time.sleep``
        in poll loops that need to honor cancellation.

        Args:
            timeout: Maximum wait in seconds.

        Returns:
            ``True`` when the token is (or becomes) set, ``False`` on
            timeout.
        """
        return self._event.wait(timeout)

    def raise_if_set(self) -> None:
        """Raise :class:`Cancelled` if the token has been set.

        Cheap polling primitive — call at the top of every poll iteration
        before any blocking I/O.

        Raises:
            Cancelled: When :meth:`is_set` is ``True``.
        """
        if self._event.is_set():
            raise Cancelled("cancellation requested by operator")


_NULL_TOKEN: CancelToken = CancelToken()
"""Sentinel token that is never set.

Used as the default value for ``cancel_token`` kwargs throughout the
codebase so library + test callers that pass no token get unchanged
behavior. Do **not** call :meth:`CancelToken.set` on this instance.
"""
```

- [ ] **Step 5: Re-export from `core/__init__.py`.**

Add to the existing `__init__.py` import block (alphabetical with other re-exports):

```python
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    AssetFetchError,
    AuthError,
    BudgetExceeded,
    Cancelled,
    CapabilityMismatch,
    # ... existing imports preserved
)
```

If `__init__.py` uses an `__all__` tuple, append `"CancelToken"` and `"Cancelled"`.

- [ ] **Step 6: Run tests to verify pass.**

```
pixi run pytest tests/core/test_cancel.py -v
```

Expected: 7 PASS.

- [ ] **Step 7: Lint + typecheck.**

```
pixi run pre-commit run --files src/kinoforge/core/cancel.py src/kinoforge/core/errors.py src/kinoforge/core/__init__.py tests/core/test_cancel.py
```

Expected: all hooks pass.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/core/cancel.py src/kinoforge/core/errors.py src/kinoforge/core/__init__.py tests/core/test_cancel.py
git commit -m "feat(core): CancelToken + Cancelled foundation for cooperative cancellation"
```

---

## Task 1: ABC + pool signature changes

**Goal:** Plumb `cancel_token=None` through the `BackendPool` + `GenerationBackend` ABCs and into `SequentialPool` / `ConcurrentPool`. Add `cancel_pending` + `timeout` kwargs to `ConcurrentPool.close` with a watchdog so a wedged worker no longer blocks shutdown forever. All existing concrete backends accept (and currently ignore) the new kwarg for ABC compliance.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (ABCs)
- Modify: `src/kinoforge/core/pool.py` (SequentialPool + ConcurrentPool)
- Modify: `src/kinoforge/engines/fake/__init__.py` (FakeBackend kwarg)
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (ComfyUIBackend.submit/.result kwarg — full cancel honoring deferred to Task 2)
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (DiffusersBackend kwarg)
- Modify: `src/kinoforge/engines/hosted/__init__.py` (HostedAPIBackend kwarg)
- Modify: `src/kinoforge/engines/bedrock_video/__init__.py` (BedrockVideoBackend kwarg)
- Modify: `src/kinoforge/core/remote_backend.py` (RemoteSubmitPollBackend.submit/.result kwarg — full cancel honoring deferred to Task 3)
- Create: `tests/core/test_pool_cancel.py`

**Acceptance Criteria:**
- [ ] `BackendPool.submit(job, *, cancel_token=None)` is the new ABC signature.
- [ ] `GenerationBackend.submit(job, *, cancel_token=None)` and `GenerationBackend.result(job_id, *, cancel_token=None)` are the new ABC signatures.
- [ ] `SequentialPool.submit` accepts `cancel_token` and forwards it to `backend.submit` + `backend.result`.
- [ ] `ConcurrentPool.submit` accepts `cancel_token` and forwards it via the slot's executor closure.
- [ ] `ConcurrentPool.close(cancel_pending=True, timeout=0.5)` returns within 1.5 s even when a worker is parked in `time.sleep(60)`; emits a single `WARN` log "worker still running after %.1fs; abandoning slot".
- [ ] `ConcurrentPool.close()` with no kwargs preserves today's behavior (`wait=True`, no `cancel_futures`).
- [ ] Every concrete backend (`FakeBackend`, `ComfyUIBackend`, `DiffusersBackend`, `HostedAPIBackend`, `BedrockVideoBackend`, `RemoteSubmitPollBackend`) accepts the new `cancel_token` kwarg without raising on `None`.
- [ ] Existing pool + backend tests still pass.

**Verify:** `pixi run pytest tests/core/test_pool_cancel.py tests/core/test_pool.py tests/engines/ -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/core/test_pool_cancel.py
"""Bounded-shutdown semantics for ConcurrentPool.close(cancel_pending=...)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import pytest

from kinoforge.core.interfaces import Artifact, GenerationBackend, GenerationJob
from kinoforge.core.pool import ConcurrentPool


@dataclass
class _SlowBackend(GenerationBackend):
    """Backend whose submit() parks the worker in time.sleep(60).

    Used to prove that ConcurrentPool.close(cancel_pending=True, timeout=...)
    no longer blocks for the full sleep duration.
    """

    name: str = "slow"
    started: threading.Event = field(default_factory=threading.Event)

    def submit(self, job, *, cancel_token=None):
        self.started.set()
        time.sleep(60.0)  # simulates a wedged worker; sleep is NOT interruptible
        return "irrelevant"

    def result(self, job_id, *, cancel_token=None):
        raise AssertionError("result() should never be reached")

    def validate_spec(self, job): return None


def _make_job() -> GenerationJob:
    """Minimal GenerationJob — segments + spec + params unused by _SlowBackend."""
    return GenerationJob(segments=[], spec={}, params={}, base_params={})


def test_close_returns_within_timeout_when_worker_wedged(caplog) -> None:
    """close(cancel_pending=True, timeout=0.5) returns even if worker stuck.

    Bug: ConcurrentPool.close currently calls executor.shutdown(wait=True)
    with no timeout. A worker parked in a forever-poll blocks shutdown
    indefinitely — the reason `kinoforge generate` requires two Ctrl-C
    presses to escape.
    """
    backend = _SlowBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)

    fut = pool.submit(_make_job())
    assert backend.started.wait(2.0), "worker did not start"

    caplog.set_level(logging.WARNING, logger="kinoforge")
    start = time.monotonic()
    pool.close(cancel_pending=True, timeout=0.5)
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"close() took {elapsed:.2f}s; should have bailed at ~0.5s"
    assert any(
        "abandoning slot" in rec.message for rec in caplog.records
    ), "expected WARN about abandoned slot"
    fut.cancel()


def test_close_no_kwargs_preserves_existing_behavior() -> None:
    """close() without kwargs still blocks until workers finish (today's behavior).

    Ensures the new kwargs are opt-in: no caller sees changed behavior unless
    they pass cancel_pending=True.
    """
    backend = _SlowBackend()
    pool = ConcurrentPool()
    pool.add(backend, max_in_flight=1)

    fut = pool.submit(_make_job())
    assert backend.started.wait(2.0)

    # Don't actually wait 60s — just confirm close() with no kwargs does NOT
    # use the watchdog path: call it in a thread, assert it does NOT return
    # within 0.5s.
    closed = threading.Event()

    def _close() -> None:
        pool.close()
        closed.set()

    threading.Thread(target=_close, daemon=True).start()
    assert closed.wait(0.5) is False, "close() with no kwargs returned suspiciously fast"
    fut.cancel()
```

- [ ] **Step 2: Run test, confirm fail.**

```
pixi run pytest tests/core/test_pool_cancel.py -v
```

Expected: `test_close_returns_within_timeout_when_worker_wedged` FAILs because `close()` blocks. Also possibly TypeError on `cancel_pending=` / `timeout=` kwargs not accepted yet, OR on `cancel_token=` kwarg in `_SlowBackend.submit` being rejected by the ABC.

- [ ] **Step 3: Update the ABCs in `core/interfaces.py`.**

Locate `class BackendPool(ABC):` and add `cancel_token: CancelToken | None = None` keyword-only kwarg to its abstract `submit` method. Locate `class GenerationBackend(ABC):` and add the same kwarg to abstract `submit` and `result`. Use `from __future__ import annotations` (already in the file) so the type hint is a string and we don't need a runtime import — but DO add a `TYPE_CHECKING` block import for `CancelToken`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
```

Then the abstract methods look like:

```python
@abstractmethod
def submit(
    self,
    job: GenerationJob,
    *,
    cancel_token: "CancelToken | None" = None,
) -> concurrent.futures.Future[Artifact]:  # BackendPool variant
    ...

@abstractmethod
def submit(  # GenerationBackend variant
    self,
    job: GenerationJob,
    *,
    cancel_token: "CancelToken | None" = None,
) -> str:
    ...

@abstractmethod
def result(
    self,
    job_id: str,
    *,
    cancel_token: "CancelToken | None" = None,
) -> Artifact:
    ...
```

- [ ] **Step 4: Update `SequentialPool` in `core/pool.py`.**

Modify `submit` at line 68:

```python
def submit(
    self,
    job: GenerationJob,
    *,
    cancel_token: CancelToken | None = None,
) -> concurrent.futures.Future[Artifact]:
    if not self._backends:
        raise RuntimeError("SequentialPool has no registered backend")
    backend = self._backends[0]
    job_id = backend.submit(job, cancel_token=cancel_token)
    artifact = backend.result(job_id, cancel_token=cancel_token)
    fut: concurrent.futures.Future[Artifact] = concurrent.futures.Future()
    fut.set_result(artifact)
    return fut
```

Add `from kinoforge.core.cancel import CancelToken` at the top.

`SequentialPool.map` is unchanged — it calls `self.submit(j).result()` and the new kwarg defaults to `None`.

`SequentialPool.close` stays a no-op (no executor to wait on); add the new kwargs to the signature for ABC parity but ignore them:

```python
def close(
    self,
    *,
    cancel_pending: bool = False,
    timeout: float | None = None,
) -> None:
    return None
```

- [ ] **Step 5: Update `ConcurrentPool` in `core/pool.py`.**

Modify `submit` at line 193 to accept + forward `cancel_token`. The slot dispatch is via `slot.executor.submit(self._run_one, slot, job)`; change to `slot.executor.submit(self._run_one, slot, job, cancel_token)` and add a `cancel_token` parameter to `_run_one`:

```python
def submit(
    self,
    job: GenerationJob,
    *,
    cancel_token: CancelToken | None = None,
) -> concurrent.futures.Future[Artifact]:
    with self._lock:
        if self._closed:
            raise RuntimeError("pool closed")
        if not self._slots:
            raise RuntimeError("ConcurrentPool has no registered backend")
    slot = self._pick()
    try:
        return slot.executor.submit(self._run_one, slot, job, cancel_token)
    except BaseException:
        self._release(slot)
        raise

def _run_one(
    self,
    slot: _Slot,
    job: GenerationJob,
    cancel_token: CancelToken | None,
) -> Artifact:
    try:
        job_id = slot.backend.submit(job, cancel_token=cancel_token)
        return slot.backend.result(job_id, cancel_token=cancel_token)
    finally:
        self._release(slot)
```

(If `_run_one` already exists with a different shape — read it first; the principle is "thread `cancel_token` through to both backend calls".)

- [ ] **Step 6: Rewrite `ConcurrentPool.close` with bounded watchdog.**

Replace the body of `close()` (currently at line 220) with:

```python
def close(
    self,
    *,
    cancel_pending: bool = False,
    timeout: float | None = None,
) -> None:
    """Shut down every per-backend executor.

    Two-phase: flip the ``_closed`` flag under the lock so new
    :meth:`submit` calls reject immediately; then shut down each slot
    outside the lock so long-running shutdowns do not serialise.

    Args:
        cancel_pending: When ``True``, queued-but-not-started futures are
            cancelled via ``cancel_futures=True``. Running workers still
            finish their current poll tick (cooperative cancellation
            happens via the ``CancelToken`` passed through ``submit``).
        timeout: Per-slot wait cap in seconds. When set, the shutdown
            joins each slot in a watchdog thread and logs
            ``WARN "worker still running after %.1fs; abandoning slot"``
            if the join exceeds the cap. ``None`` preserves the
            unconditional-wait behavior expected by existing callers.

    Idempotent — second call is a no-op.
    """
    with self._lock:
        if self._closed:
            return
        self._closed = True
        slots = list(self._slots)
    for slot in slots:
        _shutdown_slot(slot, cancel_pending=cancel_pending, timeout=timeout)


def _shutdown_slot(
    slot: _Slot,
    *,
    cancel_pending: bool,
    timeout: float | None,
) -> None:
    """Best-effort bounded shutdown of one slot's executor."""
    if timeout is None:
        slot.executor.shutdown(wait=True, cancel_futures=cancel_pending)
        return
    done = threading.Event()

    def _do_shutdown() -> None:
        slot.executor.shutdown(wait=True, cancel_futures=cancel_pending)
        done.set()

    watchdog = threading.Thread(
        target=_do_shutdown,
        daemon=True,
        name=f"kinoforge-pool-shutdown-{id(slot)}",
    )
    watchdog.start()
    if not done.wait(timeout):
        _log.warning(
            "worker still running after %.1fs; abandoning slot",
            timeout,
        )
```

Add at the top of `pool.py` if missing:

```python
import logging

from kinoforge.core.cancel import CancelToken

_log = logging.getLogger(__name__)
```

- [ ] **Step 7: Update concrete backends to accept the kwarg.**

For each backend listed in **Files** above, add `*, cancel_token: CancelToken | None = None` to `submit` and `result` signatures. Body unchanged — the kwarg is accepted but ignored at this stage. ComfyUI gets full honoring in Task 2; Remote in Task 3.

Use a `TYPE_CHECKING` import for `CancelToken` in each file to avoid runtime import churn:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
```

Example (`engines/fake/__init__.py`):

```python
def submit(
    self,
    job: GenerationJob,
    *,
    cancel_token: "CancelToken | None" = None,
) -> str:
    # existing body unchanged
    ...
```

- [ ] **Step 8: Run tests, confirm pass.**

```
pixi run pytest tests/core/test_pool_cancel.py tests/core/test_pool.py tests/engines/ -v
```

Expected: `test_pool_cancel.py` 2 PASS; existing tests still PASS.

- [ ] **Step 9: Lint + typecheck.**

```
pixi run pre-commit run --files \
  src/kinoforge/core/interfaces.py src/kinoforge/core/pool.py \
  src/kinoforge/engines/fake/__init__.py src/kinoforge/engines/comfyui/__init__.py \
  src/kinoforge/engines/diffusers/__init__.py src/kinoforge/engines/hosted/__init__.py \
  src/kinoforge/engines/bedrock_video/__init__.py src/kinoforge/core/remote_backend.py \
  tests/core/test_pool_cancel.py
```

Expected: all hooks pass. If mypy complains about the abstract-method signature change in fakes used by tests, ensure each fake matches the new ABC exactly.

- [ ] **Step 10: Commit.**

```bash
git add -p src/kinoforge/core/interfaces.py src/kinoforge/core/pool.py \
  src/kinoforge/engines/ src/kinoforge/core/remote_backend.py tests/core/test_pool_cancel.py
git commit -m "feat(core/pool): bounded ConcurrentPool.close + cancel_token plumbing on ABCs"
```

---

## Task 2: ComfyUI poll observability + cancel + hard timeout

**Goal:** `ComfyUIBackend.result` honors the cancel token, emits a structured per-tick INFO log, and raises `TimeoutError` after `poll_timeout_s` (cfg-driven, default 600 s).

**Files:**
- Modify: `src/kinoforge/core/config.py` (add `poll_timeout_s` field)
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (`ComfyUIBackend.result` poll loop + ctor; `ComfyUIEngine.backend` plumbs `poll_timeout_s`)
- Create: `tests/engines/test_comfyui_cancel.py`
- Create: `tests/engines/test_comfyui_timeout.py`
- Create: `tests/engines/test_comfyui_poll_log.py`

**Acceptance Criteria:**
- [ ] `ComfyUIEngineConfig.poll_timeout_s: float = 600.0` is the new pydantic field; YAML round-trips correctly.
- [ ] `ComfyUIBackend.result(job_id, cancel_token=<set>)` raises `Cancelled` on the first iteration with zero `http_get` calls.
- [ ] `ComfyUIBackend.result` honors a `set()` token during the inter-poll wait (uses `cancel_token.wait` instead of `time.sleep`).
- [ ] `ComfyUIBackend.result` raises `TimeoutError` whose message contains the literal substrings `last_status=` and `exec_node=` after `poll_timeout_s` elapses without outputs.
- [ ] Every poll iteration emits a single INFO log line matching the regex `comfyui poll job=\S+ elapsed=\d+\.\d+s status=\S+ queue_pos=\S+ exec_node=\S+`.
- [ ] `queue_pos` field is `None` (rendered `"None"`) when `last_status != "queued"`; a real value when queued.

**Verify:** `pixi run pytest tests/engines/test_comfyui_cancel.py tests/engines/test_comfyui_timeout.py tests/engines/test_comfyui_poll_log.py tests/engines/test_comfyui.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

```python
# tests/engines/test_comfyui_cancel.py
"""ComfyUIBackend honors a CancelToken passed via .result()."""

from __future__ import annotations

import pytest

from kinoforge.core import CancelToken, Cancelled
from kinoforge.engines.comfyui import ComfyUIBackend


def test_result_raises_cancelled_on_preset_token() -> None:
    """Pre-set token short-circuits before the first http_get call.

    Bug: today ComfyUIBackend.result polls forever with no cancellation
    mechanism — the reason `kinoforge generate` hangs on Ctrl-C.
    """
    calls: list[str] = []

    def _http_get(url: str) -> dict:
        calls.append(url)
        return {"outputs": {}}

    backend = ComfyUIBackend(
        base_url="http://x",
        http_get=_http_get,
        poll_interval_s=0.01,
        poll_timeout_s=1.0,
    )
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.result("job-123", cancel_token=token)

    assert calls == [], f"expected no http_get calls, got {calls}"


def test_result_honors_token_set_during_wait() -> None:
    """Token set after one poll tick raises Cancelled within ~poll_interval_s."""
    import threading

    tick_count = [0]

    def _http_get(url: str) -> dict:
        tick_count[0] += 1
        return {}  # never complete

    backend = ComfyUIBackend(
        base_url="http://x",
        http_get=_http_get,
        poll_interval_s=0.05,
        poll_timeout_s=60.0,
    )
    token = CancelToken()

    def _setter() -> None:
        import time as _t
        _t.sleep(0.1)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()
    with pytest.raises(Cancelled):
        backend.result("job-456", cancel_token=token)

    # Should have ticked 1-5 times before cancellation, not hundreds.
    assert tick_count[0] < 10, f"too many ticks ({tick_count[0]}) — wait not interruptible"
```

```python
# tests/engines/test_comfyui_timeout.py
"""ComfyUIBackend.result raises TimeoutError after poll_timeout_s."""

from __future__ import annotations

import pytest

from kinoforge.engines.comfyui import ComfyUIBackend


def test_timeout_raises_with_status_in_message() -> None:
    """poll_timeout_s=0.2 raises TimeoutError; message contains last_status + exec_node.

    Bug: today ComfyUIBackend.result has no upper-bound timeout — the
    reason the 2026-06-10 stall took 30s of operator patience to surface.
    """
    def _http_get(url: str) -> dict:
        # Simulate ComfyUI returning "running, currently on WanVideoSampler" forever.
        return {
            "status": {
                "status_str": "running",
                "exec_info": {"current_node": "WanVideoSampler"},
            }
        }

    backend = ComfyUIBackend(
        base_url="http://x",
        http_get=_http_get,
        poll_interval_s=0.05,
        poll_timeout_s=0.2,
    )

    with pytest.raises(TimeoutError) as exc_info:
        backend.result("job-789")

    msg = str(exc_info.value)
    assert "last_status=" in msg, f"missing last_status in message: {msg}"
    assert "exec_node=" in msg, f"missing exec_node in message: {msg}"
    assert "WanVideoSampler" in msg, f"missing actual node name in message: {msg}"
```

```python
# tests/engines/test_comfyui_poll_log.py
"""Every ComfyUIBackend.result poll tick emits the structured INFO log line."""

from __future__ import annotations

import logging
import re

import pytest

from kinoforge.engines.comfyui import ComfyUIBackend

POLL_LOG_RE = re.compile(
    r"comfyui poll job=\S+ elapsed=\d+\.\d+s status=\S+ queue_pos=\S+ exec_node=\S+"
)


def test_each_tick_emits_structured_log(caplog) -> None:
    """Three poll ticks → three matching INFO lines.

    Bug: today the poll loop emits no per-tick log; operator cannot tell
    where the stall is.
    """
    tick = [0]

    def _http_get(url: str) -> dict:
        tick[0] += 1
        if tick[0] >= 3:
            return {"outputs": {"node-1": {"images": [{"filename": "out.mp4"}]}}}
        return {"status": {"status_str": "running", "exec_info": {"current_node": "KSampler"}}}

    backend = ComfyUIBackend(
        base_url="http://x",
        http_get=_http_get,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.engines.comfyui")
    try:
        backend.result("job-xyz")
    except Exception:
        pass  # _build_artifact may need extra fakes; we only care about the log lines.

    poll_lines = [r.message for r in caplog.records if POLL_LOG_RE.search(r.message)]
    assert len(poll_lines) >= 3, f"expected >=3 structured log lines, got {len(poll_lines)}: {poll_lines}"


def test_queue_pos_none_when_not_queued(caplog) -> None:
    """queue_pos field is rendered as `None` when status != queued."""
    def _http_get(url: str) -> dict:
        return {"outputs": {"node": {"images": [{"filename": "x.mp4"}]}},
                "status": {"status_str": "complete"}}

    backend = ComfyUIBackend(
        base_url="http://x",
        http_get=_http_get,
        poll_interval_s=0.01,
        poll_timeout_s=10.0,
    )

    caplog.set_level(logging.INFO, logger="kinoforge.engines.comfyui")
    try:
        backend.result("job-q")
    except Exception:
        pass

    poll_lines = [r.message for r in caplog.records if "comfyui poll" in r.message]
    assert any("queue_pos=None" in line for line in poll_lines), f"expected queue_pos=None: {poll_lines}"
```

- [ ] **Step 2: Run tests, confirm fail.**

```
pixi run pytest tests/engines/test_comfyui_cancel.py tests/engines/test_comfyui_timeout.py tests/engines/test_comfyui_poll_log.py -v
```

Expected: all three FAIL — `poll_timeout_s` kwarg unknown; no cancellation; no per-tick log.

- [ ] **Step 3: Add `poll_timeout_s` to `ComfyUIEngineConfig`.**

In `src/kinoforge/core/config.py` near line 158:

```python
class ComfyUIEngineConfig(BaseModel):
    """ComfyUI-specific engine configuration."""

    # ... existing fields ...
    poll_interval_s: float = Field(default=2.0, gt=0.0)  # if not already present
    poll_timeout_s: float = Field(
        default=600.0,
        gt=0.0,
        description=(
            "Hard upper bound (seconds) on a single ComfyUIBackend.result "
            "poll wait. Raises TimeoutError with last_status + exec_node "
            "in the message when exceeded. Lift this for known-slow "
            "models (e.g. Wan 14B t2v can take ~6 min)."
        ),
    )
```

Confirm `poll_interval_s` already exists — `core/config.py` does not currently expose it (the backend has the default baked in). If you add `poll_interval_s` here for the first time, also thread it through `ComfyUIEngine.backend` below.

- [ ] **Step 4: Rewrite `ComfyUIBackend.__init__` + `.result`.**

In `src/kinoforge/engines/comfyui/__init__.py`, modify `ComfyUIBackend.__init__` (around line 494) to accept `poll_timeout_s: float = 600.0`:

```python
def __init__(
    self,
    *,
    base_url: str,
    http_get: Callable[[str], dict[str, Any]],
    # ... existing kwargs preserved
    poll_interval_s: float = 2.0,
    poll_timeout_s: float = 600.0,
    sleep: Callable[[float], None] = time.sleep,
    http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
) -> None:
    # ... existing assignments preserved
    self._poll_interval_s = poll_interval_s
    self._poll_timeout_s = poll_timeout_s
```

Replace the body of `.result` (around line 656) — keep the existing `_retry_proxy_call` wrapping for the actual `http_get` (Phase 47 layer; do not regress) but wrap the poll loop:

```python
def result(
    self,
    job_id: str,
    *,
    cancel_token: "CancelToken | None" = None,
) -> Artifact:
    from kinoforge.core.cancel import _NULL_TOKEN

    token = cancel_token or _NULL_TOKEN
    start = time.monotonic()
    last_status = "unknown"
    queue_pos: int | None = None
    exec_node: str | None = None
    while True:
        token.raise_if_set()
        elapsed = time.monotonic() - start
        if elapsed > self._poll_timeout_s:
            raise TimeoutError(
                f"comfyui poll timed out after {elapsed:.1f}s "
                f"(job={job_id}, last_status={last_status!r}, "
                f"exec_node={exec_node!r})"
            )
        envelope = _retry_proxy_call(
            lambda: self._http_get(f"{self._base_url}/history/{job_id}")
        )
        last_status, queue_pos, exec_node = _extract_poll_fields(envelope)
        if last_status == "queued":
            try:
                queue_envelope = self._http_get(f"{self._base_url}/queue")
                queue_pos = _extract_queue_position(queue_envelope, job_id)
            except Exception:
                queue_pos = None
        _log.info(
            "comfyui poll job=%s elapsed=%.1fs status=%s queue_pos=%s exec_node=%s",
            job_id, elapsed, last_status, queue_pos, exec_node,
        )
        outputs = envelope.get("outputs") or envelope.get(job_id, {}).get("outputs")
        if outputs:
            return self._build_artifact_from_outputs(outputs, job_id)
        token.wait(self._poll_interval_s)
```

Add the helpers near the top of the file (above the class):

```python
def _extract_poll_fields(
    envelope: dict[str, Any],
) -> tuple[str, int | None, str | None]:
    """Pull (status, queue_pos, exec_node) from a ComfyUI history envelope.

    ComfyUI versions vary the envelope shape. Tolerate missing keys by
    returning the unknown sentinel for status, ``None`` for the other two.
    """
    status_block = envelope.get("status", {}) if isinstance(envelope, dict) else {}
    status = status_block.get("status_str", "unknown") if isinstance(status_block, dict) else "unknown"
    exec_info = status_block.get("exec_info", {}) if isinstance(status_block, dict) else {}
    exec_node = exec_info.get("current_node") if isinstance(exec_info, dict) else None
    # queue_pos populated by separate /queue probe in the poll loop when status == "queued"
    return status, None, exec_node


def _extract_queue_position(
    envelope: dict[str, Any],
    job_id: str,
) -> int | None:
    """Best-effort lookup of *job_id* position in ComfyUI's `/queue` envelope."""
    if not isinstance(envelope, dict):
        return None
    running = envelope.get("queue_running", []) or []
    pending = envelope.get("queue_pending", []) or []
    for idx, entry in enumerate(running):
        if _entry_matches(entry, job_id):
            return idx
    for idx, entry in enumerate(pending):
        if _entry_matches(entry, job_id):
            return len(running) + idx
    return None


def _entry_matches(entry: object, job_id: str) -> bool:
    """ComfyUI queue entries are typically [number, prompt_id, ...]."""
    if isinstance(entry, list) and len(entry) >= 2:
        return entry[1] == job_id
    return False
```

Adjust `_extract_poll_fields` and the `outputs` extraction line to match the actual envelope shape used elsewhere in the file — read the existing `.result` body before rewriting to preserve the `_build_artifact_from_outputs` (or equivalent) helper name and the outputs-key path.

Add the import + module logger at the top if not already present:

```python
import logging

from kinoforge.core.cancel import CancelToken  # noqa: F401 (used in annotations)

_log = logging.getLogger(__name__)
```

Modify `.submit` (around line 544) to accept the kwarg:

```python
def submit(
    self,
    job: GenerationJob,
    *,
    cancel_token: "CancelToken | None" = None,
) -> str:
    from kinoforge.core.cancel import _NULL_TOKEN

    (cancel_token or _NULL_TOKEN).raise_if_set()
    # ... existing body
```

- [ ] **Step 5: Thread `poll_timeout_s` through `ComfyUIEngine.backend`.**

`ComfyUIEngine.backend` constructs a `ComfyUIBackend`. Read the existing kwargs being passed (likely around `engines/comfyui/__init__.py:916`); add:

```python
return ComfyUIBackend(
    base_url=...,
    # existing kwargs
    poll_interval_s=cfg.engine.comfyui.poll_interval_s if hasattr(cfg.engine.comfyui, "poll_interval_s") else 2.0,
    poll_timeout_s=cfg.engine.comfyui.poll_timeout_s,
)
```

- [ ] **Step 6: Run tests, confirm pass.**

```
pixi run pytest tests/engines/test_comfyui_cancel.py tests/engines/test_comfyui_timeout.py tests/engines/test_comfyui_poll_log.py -v
```

Expected: all PASS. Also run `tests/engines/test_comfyui.py` to confirm no regression on existing ComfyUI tests.

- [ ] **Step 7: Lint + typecheck.**

```
pixi run pre-commit run --files \
  src/kinoforge/core/config.py src/kinoforge/engines/comfyui/__init__.py \
  tests/engines/test_comfyui_cancel.py tests/engines/test_comfyui_timeout.py \
  tests/engines/test_comfyui_poll_log.py
```

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/core/config.py src/kinoforge/engines/comfyui/__init__.py \
  tests/engines/test_comfyui_cancel.py tests/engines/test_comfyui_timeout.py \
  tests/engines/test_comfyui_poll_log.py
git commit -m "feat(comfyui): per-tick poll log + hard poll_timeout_s + cooperative cancel"
```

---

## Task 3: RemoteSubmitPollBackend cancel honoring

**Goal:** `RemoteSubmitPollBackend.result` honors a `cancel_token` so Replicate / Runway / Luma / Fal Ctrl-C paths land the same way ComfyUI does.

**Files:**
- Modify: `src/kinoforge/core/remote_backend.py` (poll loop)
- Create: `tests/engines/test_remote_submit_poll_cancel.py`

**Acceptance Criteria:**
- [ ] `RemoteSubmitPollBackend.result(job_id, cancel_token=<set>)` raises `Cancelled` on the first iteration.
- [ ] During the inter-poll sleep, a `set()` from another thread raises `Cancelled` within ~one `poll_interval_s`.
- [ ] Existing `tests/engines/test_replicate.py`, `test_runway.py`, `test_luma.py`, `test_fal.py` still pass.

**Verify:** `pixi run pytest tests/engines/test_remote_submit_poll_cancel.py tests/engines/test_replicate.py tests/engines/test_runway.py tests/engines/test_luma.py tests/engines/test_fal.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/engines/test_remote_submit_poll_cancel.py
"""RemoteSubmitPollBackend honors cancel_token in .result()."""

from __future__ import annotations

import threading
import time

import pytest

from kinoforge.core import CancelToken, Cancelled
from kinoforge.core.remote_backend import RemoteSubmitPollBackend


class _FakeRemoteBackend(RemoteSubmitPollBackend):
    """Minimal concrete for ABC coverage; status never completes."""

    name = "fake-remote"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def _submit_remote(self, job):
        return "rj-1"

    def _fetch_status(self, job_id):
        self.calls += 1
        return {"status": "running"}

    def _is_complete(self, status):
        return False

    def _is_failed(self, status):
        return False

    def _extract_artifact(self, status, job_id):
        raise AssertionError("never called")

    def _failure_reason(self, status):
        return "n/a"


def test_result_raises_cancelled_on_preset_token() -> None:
    backend = _FakeRemoteBackend(poll_interval_s=0.01)
    token = CancelToken()
    token.set()
    with pytest.raises(Cancelled):
        backend.result("rj-1", cancel_token=token)
    assert backend.calls == 0


def test_result_honors_token_during_wait() -> None:
    backend = _FakeRemoteBackend(poll_interval_s=0.05)
    token = CancelToken()

    def _setter() -> None:
        time.sleep(0.1)
        token.set()

    threading.Thread(target=_setter, daemon=True).start()
    with pytest.raises(Cancelled):
        backend.result("rj-1", cancel_token=token)
    assert backend.calls < 10
```

Read `core/remote_backend.py:55-260` first to confirm the actual hook method names on the ABC (`_submit_remote` / `_fetch_status` / `_is_complete` / `_is_failed` / `_extract_artifact` / `_failure_reason` are best-guess names from the architecture pattern — adjust the fake to whatever the real ABC declares).

- [ ] **Step 2: Run tests, confirm fail.**

```
pixi run pytest tests/engines/test_remote_submit_poll_cancel.py -v
```

Expected: FAIL — `cancel_token` kwarg unknown OR poll loop ignores it.

- [ ] **Step 3: Modify `RemoteSubmitPollBackend.submit` + `.result`.**

In `src/kinoforge/core/remote_backend.py`, modify `submit` (line 161) and `result` (line 196) to accept `cancel_token: CancelToken | None = None`. In `result`, replace `self._sleep(self._poll_interval_s)` (line 238) with the token-aware pattern:

```python
def result(
    self,
    job_id: str,
    *,
    cancel_token: "CancelToken | None" = None,
) -> Artifact:
    from kinoforge.core.cancel import _NULL_TOKEN

    token = cancel_token or _NULL_TOKEN
    # ... existing setup
    while True:
        token.raise_if_set()
        status = self._fetch_status(job_id)
        # ... existing completion / failure checks
        if self._is_complete(status):
            return self._extract_artifact(status, job_id)
        if self._is_failed(status):
            raise ...
        token.wait(self._poll_interval_s)  # replaces self._sleep(self._poll_interval_s)
```

Same `submit` token-check pattern as ComfyUI Task 2 step 4 final block.

- [ ] **Step 4: Run tests, confirm pass.**

```
pixi run pytest tests/engines/test_remote_submit_poll_cancel.py \
  tests/engines/test_replicate.py tests/engines/test_runway.py \
  tests/engines/test_luma.py tests/engines/test_fal.py -v
```

Expected: new tests PASS; existing tests PASS.

- [ ] **Step 5: Lint + typecheck.**

```
pixi run pre-commit run --files src/kinoforge/core/remote_backend.py tests/engines/test_remote_submit_poll_cancel.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/remote_backend.py tests/engines/test_remote_submit_poll_cancel.py
git commit -m "feat(remote-backend): cooperative cancel honoring in shared poll loop"
```

---

## Task 4: Orchestrator stage-loop except + deploy_session cancel-pending

**Goal:** `orchestrator.generate` + `deploy_session` accept `cancel_token=None`; the stage-loop except arms at lines 1085 + 1173 gain a `(KeyboardInterrupt, Cancelled)` clause that logs a WARN naming the pod ID and does NOT destroy. `deploy_session.__exit__` passes `cancel_pending` (derived from the token state) to `pool.close(timeout=30.0)`. `GenerateClipStage` accepts `cancel_token` and forwards it into `pool.submit`.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (ctor + pool.submit call)
- Modify: `src/kinoforge/core/orchestrator.py` (generate + deploy_session + stage-loop except + GenerateClipStage construction)
- Create: `tests/core/test_orchestrator_interrupt.py`
- Create: `tests/core/test_orchestrator_cancelled.py`

**Acceptance Criteria:**
- [ ] `generate(cfg, request, *, cancel_token=None, ...)` is the new signature; default preserves library callers.
- [ ] `deploy_session(cfg, *, cancel_token=None, ...)` is the new signature.
- [ ] When a stage raises `KeyboardInterrupt`, `provider.destroy_instance` is NOT called.
- [ ] When a stage raises `Cancelled`, `provider.destroy_instance` is NOT called.
- [ ] Both cases emit exactly one WARN log line containing the pod ID and the substring `kinoforge reap`.
- [ ] `ValidationError` path still destroys the pod (existing behavior preserved).
- [ ] `deploy_session.__exit__` calls `pool.close(cancel_pending=True, timeout=30.0)` when the token is set; calls `pool.close()` (today's behavior) when unset.
- [ ] `GenerateClipStage.__init__` accepts `cancel_token=None`; the stored value is forwarded into every `pool.submit(job, cancel_token=...)` call inside `.run()`.

**Verify:** `pixi run pytest tests/core/test_orchestrator_interrupt.py tests/core/test_orchestrator_cancelled.py tests/core/test_orchestrator.py tests/pipeline/test_generate_clip.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
# tests/core/test_orchestrator_interrupt.py
"""Stage-loop KeyboardInterrupt arm — no destroy, WARN with pod ID + reap text."""

from __future__ import annotations

import logging

import pytest

# Imports + fixture setup follow the pattern of tests/core/test_orchestrator.py.
# Read that file first; reuse its `_FakeProvider` / `_FakeEngine` / minimal-cfg
# fixtures rather than duplicating them.


def test_keyboard_interrupt_during_stage_does_not_destroy(
    _orch_fixture,  # provides cfg, fake provider with spy, monkeypatch on stage.run
    caplog,
) -> None:
    """KeyboardInterrupt mid-generate keeps the pod alive (warm-reuse intent).

    Bug: today the stage-loop except clause catches only ValidationError;
    KeyboardInterrupt propagates with no log line, no destroy, and the
    operator has no way to know the pod is alive without checking RunPod.
    """
    cfg, provider, monkeypatch = _orch_fixture
    provider.destroy_calls = []

    # Force the stage loop to raise KeyboardInterrupt on first iteration.
    import kinoforge.pipeline.generate_clip as gc_mod
    def _raise(*_a, **_kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(gc_mod.GenerateClipStage, "run", _raise)

    from kinoforge.core.orchestrator import generate
    from kinoforge.core.interfaces import GenerationRequest

    caplog.set_level(logging.WARNING, logger="kinoforge.core.orchestrator")
    with pytest.raises(KeyboardInterrupt):
        generate(cfg, GenerationRequest(prompt="cat", mode="t2v"), state_dir=...)

    assert provider.destroy_calls == [], "pod must NOT be destroyed on interrupt"
    warn_records = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("kinoforge reap" in m for m in warn_records), (
        f"expected WARN with `kinoforge reap` recovery text: {warn_records}"
    )
    # Pod ID should appear in the WARN line.
    assert any(provider.last_created_id in m for m in warn_records if "kinoforge reap" in m)
```

```python
# tests/core/test_orchestrator_cancelled.py
"""Same as test_orchestrator_interrupt but the stage raises Cancelled."""

# Identical structure to test_orchestrator_interrupt.py except the monkeypatched
# stage raises `Cancelled("worker cancelled")` instead of KeyboardInterrupt.
```

Read `tests/core/test_orchestrator.py` first — adopt its fixture (`_orch_fixture` is illustrative; use the actual name). Pull the WARN format string from the new code in Step 4 below so the assertion matches.

- [ ] **Step 2: Run tests, confirm fail.**

```
pixi run pytest tests/core/test_orchestrator_interrupt.py tests/core/test_orchestrator_cancelled.py -v
```

Expected: FAIL — either fixture doesn't exist yet (rewrite to match real fixtures), the WARN line is missing, or `KeyboardInterrupt` propagates and `provider.destroy_instance` IS called (regression of current behavior). Confirm the failure reason matches the bug.

- [ ] **Step 3: Add `cancel_token` kwarg to `GenerateClipStage`.**

In `src/kinoforge/pipeline/generate_clip.py`, modify `__init__` (around line 36) to accept `cancel_token: CancelToken | None = None` and store it as `self.cancel_token`. In `.run()` (around line 151) replace `self.pool.submit(job)` with `self.pool.submit(job, cancel_token=self.cancel_token)`. Add `from kinoforge.core.cancel import CancelToken` import (or `TYPE_CHECKING` block).

- [ ] **Step 4: Add `cancel_token` to `deploy_session` + `generate` and the stage-loop except arms.**

In `src/kinoforge/core/orchestrator.py`:

`deploy_session` (line 492) — add `cancel_token: CancelToken | None = None` kwarg; in the `finally` (line 749) replace `pool.close()` with:

```python
finally:
    if hb_loop is not None:
        hb_loop.stop()
    if cancel_token is not None and cancel_token.is_set():
        try:
            pool.close(cancel_pending=True, timeout=30.0)
        except Exception as close_exc:
            _log.error("pool.close failed during interrupt cleanup: %s", close_exc)
    else:
        pool.close()
```

`generate` (line 911) — add `cancel_token: CancelToken | None = None` kwarg; pass it into `deploy_session(...)`. When constructing `GenerateClipStage` around line 1151, pass `cancel_token=cancel_token`. When constructing `KeyframeStage` around line 1090, pass `cancel_token=cancel_token` too.

Locate the stage-loop except clause at line 1173:

```python
try:
    for stage in stages:
        state = stage.run(state)
except ValidationError:
    _log.warning(
        "spec validation failed; tearing down instance before re-raising"
    )
    if (
        session.instance is not None
        and session.provider is not None
        and not _caller_supplied_instance
    ):
        session.provider.destroy_instance(session.instance.id)
    raise
except (KeyboardInterrupt, Cancelled) as exc:
    _log.warning(
        "%s during stages; pod %s kept alive (selfterm/reap path). "
        "Run `kinoforge reap` to destroy now.",
        type(exc).__name__,
        session.instance.id if session.instance is not None else "<hosted>",
    )
    raise
```

Mirror the same `(KeyboardInterrupt, Cancelled)` arm onto the keyframe block at line 1085 (it currently only catches `ValidationError`).

Add the import at the top of `orchestrator.py`:

```python
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled  # already imported via errors block; confirm
```

- [ ] **Step 5: Run tests, confirm pass.**

```
pixi run pytest tests/core/test_orchestrator_interrupt.py tests/core/test_orchestrator_cancelled.py tests/core/test_orchestrator.py tests/pipeline/test_generate_clip.py -v
```

Expected: new tests PASS; existing orchestrator + generate_clip tests still PASS.

- [ ] **Step 6: Lint + typecheck.**

```
pixi run pre-commit run --files \
  src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py \
  tests/core/test_orchestrator_interrupt.py tests/core/test_orchestrator_cancelled.py
```

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py \
  tests/core/test_orchestrator_interrupt.py tests/core/test_orchestrator_cancelled.py
git commit -m "feat(orchestrator): WARN-not-destroy on interrupt; cancel-aware pool close"
```

---

## Task 5: CLI signal handler + SessionContext.cancel_token

**Goal:** CLI installs a SIGINT handler in `main()` before `_cmd_generate` / `_cmd_batch` dispatch. First press sets the shared `CancelToken`; second press restores `SIG_DFL` and re-raises. `SessionContext` carries the token so command handlers can thread it into `generate()`.

**Files:**
- Modify: `src/kinoforge/cli/context.py` (`SessionContext.cancel_token` field)
- Modify: `src/kinoforge/cli/_main.py` (handler install)
- Modify: `src/kinoforge/cli/_commands.py` (thread `ctx.cancel_token` into `generate`)
- Create: `tests/cli/test_sigint_handler.py`

**Acceptance Criteria:**
- [ ] `SessionContext` carries a `cancel_token: CancelToken` field (default `CancelToken()` per-invocation; not the module sentinel).
- [ ] `main()` installs the SIGINT handler ONLY when dispatching `_cmd_generate` or `_cmd_batch` (read-only subcommands like `list` / `status` do not install).
- [ ] First `signal.raise_signal(SIGINT)` while handler is installed: `ctx.cancel_token.is_set() == True`; no `KeyboardInterrupt` raised.
- [ ] Second `signal.raise_signal(SIGINT)`: `KeyboardInterrupt` is raised; the default SIGINT handler is restored.
- [ ] `_cmd_generate` and `_cmd_batch` pass `cancel_token=ctx.cancel_token` into the `generate(...)` / `batch_generate(...)` call.

**Verify:** `pixi run pytest tests/cli/test_sigint_handler.py tests/cli/ -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
# tests/cli/test_sigint_handler.py
"""Two-press SIGINT handler — first sets token, second re-raises."""

from __future__ import annotations

import signal

import pytest

from kinoforge.core import CancelToken
from kinoforge.cli._main import _install_sigint_handler


def test_first_signal_sets_token_no_raise() -> None:
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        signal.raise_signal(signal.SIGINT)
        assert token.is_set() is True
    finally:
        signal.signal(signal.SIGINT, prior)


def test_second_signal_reraises_and_restores_default() -> None:
    token = CancelToken()
    prior = signal.signal(signal.SIGINT, signal.SIG_DFL)
    try:
        _install_sigint_handler(token)
        signal.raise_signal(signal.SIGINT)  # first — sets token
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)  # second — re-raises
        # Default handler restored: a third SIGINT would do the default action.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGINT, prior)
```

- [ ] **Step 2: Run test, confirm fail.**

```
pixi run pytest tests/cli/test_sigint_handler.py -v
```

Expected: FAIL — `_install_sigint_handler` doesn't exist.

- [ ] **Step 3: Add `cancel_token` to `SessionContext`.**

In `src/kinoforge/cli/context.py` near line 33:

```python
from kinoforge.core.cancel import CancelToken

@dataclass
class SessionContext:
    # ... existing fields ...
    cancel_token: CancelToken = field(default_factory=CancelToken)
```

Use `dataclasses.field(default_factory=CancelToken)` so each context gets a fresh token (not a shared one).

- [ ] **Step 4: Add `_install_sigint_handler` to `_main.py`.**

In `src/kinoforge/cli/_main.py`, add near the other helpers (before `main()`):

```python
import logging
import signal

from kinoforge.core.cancel import CancelToken

_log = logging.getLogger(__name__)


def _install_sigint_handler(token: CancelToken) -> None:
    """Install a two-press SIGINT handler that flips *token* on first press.

    First press: sets the token and logs a WARN line; the orchestrator and
    backends observe the token and unwind cooperatively.

    Second press: restores the default SIGINT handler and re-raises
    KeyboardInterrupt so the operator can always force-exit.
    """
    def _handler(signum: int, frame: object) -> None:
        if token.is_set():
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise KeyboardInterrupt
        _log.warning(
            "interrupt received; finishing in-flight work + draining pool. "
            "Press Ctrl-C again to force-exit."
        )
        token.set()

    signal.signal(signal.SIGINT, _handler)
```

In `main()` (line 414), install the handler ONLY when dispatching the two long-running subcommands:

```python
def main(argv: list[str] | None = None) -> int:
    # ... existing arg parsing, ctx setup
    if args.cmd in {"generate", "batch"}:
        _install_sigint_handler(ctx.cancel_token)
    return _DISPATCH[args.cmd](args, ctx)
```

- [ ] **Step 5: Thread `ctx.cancel_token` through `_cmd_generate` + `_cmd_batch`.**

In `src/kinoforge/cli/_commands.py::_cmd_generate` (line 278), modify the `_generate(...)` call to pass `cancel_token=ctx.cancel_token`. Do the same for `_cmd_batch`'s call into `batch_generate`. (If `batch_generate` does not yet accept the kwarg, add it in this step too, defaulted to `None`, and pass through to its internal `generate` calls.)

- [ ] **Step 6: Run tests, confirm pass.**

```
pixi run pytest tests/cli/test_sigint_handler.py tests/cli/ -v
```

Expected: new tests PASS; existing CLI tests still PASS.

- [ ] **Step 7: Lint + typecheck.**

```
pixi run pre-commit run --files \
  src/kinoforge/cli/context.py src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py \
  tests/cli/test_sigint_handler.py
```

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/cli/context.py src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py \
  tests/cli/test_sigint_handler.py
git commit -m "feat(cli): two-press SIGINT handler + SessionContext cancel_token"
```

---

## Task 6: README + PROGRESS closeout

**Goal:** Document the new operator-visible behavior (Ctrl-C UX + `poll_timeout_s` knob + WARN-on-interrupt) in README, append a Phase 50 closeout entry to PROGRESS.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] README has a "Interrupting a generation" section explaining: first `Ctrl-C` triggers graceful drain, WARN names surviving pod ID + `kinoforge reap`; second `Ctrl-C` force-exits.
- [ ] README documents the new `engine.comfyui.poll_timeout_s` YAML field with its default and intent.
- [ ] PROGRESS gets a Phase 50 closeout entry summarising the bug, the fix scope, the new test count delta, and any carry-forwards (e.g. backend-specific cancel hardening for Diffusers / Hosted-API / Bedrock if their poll loops differ from the shared pattern).
- [ ] PROGRESS "Single next action" pointer updated.
- [ ] PROGRESS "Known limitations & follow-ups" updated if any new items surfaced.
- [ ] All previous task commits are referenced by short SHA in the PROGRESS entry.

**Verify:** `pixi run pre-commit run --files README.md PROGRESS.md` → PASS, AND `git log --oneline -10` shows the six task commits in order.

**Steps:**

- [ ] **Step 1: Append `### Interrupting a generation` section to README.md.**

```markdown
### Interrupting a generation

Press `Ctrl-C` once during `kinoforge generate` to trigger a graceful drain.
The orchestrator stops issuing new poll requests, in-flight backend calls
unwind cooperatively, and the CLI prints a WARN line naming the surviving
pod ID and the recovery command:

```
WARN orchestrator interrupt received; finishing in-flight work + draining pool. Press Ctrl-C again to force-exit.
WARN orchestrator KeyboardInterrupt during stages; pod abc123 kept alive (selfterm/reap path). Run `kinoforge reap` to destroy now.
```

The pod is **not** destroyed on interrupt — warm-reuse intent applies in
both `--ephemeral` and non-ephemeral modes. The in-pod self-terminator
(idle-timeout / max-lifetime) kills the pod eventually, and `kinoforge
reap` destroys it immediately. Run `kinoforge reap` if you don't want to
wait.

Press `Ctrl-C` a second time to force-exit immediately. The default
SIGINT handler is restored, so a third `Ctrl-C` would terminate the
process the usual way.

#### Configurable ComfyUI poll timeout

`ComfyUIBackend.result` has a hard upper bound on a single poll wait:

```yaml
engine:
  kind: comfyui
  comfyui:
    poll_interval_s: 2.0
    poll_timeout_s: 600.0    # raise for known-slow models (Wan 14B t2v ~6 min)
```

When the timeout fires, `TimeoutError` is raised with the last observed
`status` and `current_node` baked into the message — enough state to
diagnose a stall without re-running the smoke.
```

- [ ] **Step 2: Append Phase 50 closeout to PROGRESS.md.**

Append after the Phase 49 block (around line 2343) using the section format already in use (e.g. the Phase 49 closeout at line 2311):

```markdown
### Phase 50 — Graceful interrupt + ComfyUI poll observability

Sibling layer triggered by a 2026-06-10 Wan 14B t2v live smoke that hung
silently after `provisioner.provision` returned, required two `Ctrl-C`
presses to escape, and left `provider.destroy_instance` unrun. Three
orthogonal defects (silent stall, two-press requirement, no
KeyboardInterrupt WARN) repaired in one PR; no live spend.

- [x] Task 0: `CancelToken` + `Cancelled` foundation — commit `<SHA0>`
- [x] Task 1: ABC + pool signature changes + watchdog close — commit `<SHA1>`
- [x] Task 2: ComfyUI per-tick poll log + `poll_timeout_s` + cancel — commit `<SHA2>`
- [x] Task 3: `RemoteSubmitPollBackend` cancel honoring — commit `<SHA3>`
- [x] Task 4: Orchestrator stage-loop except + cancel-aware pool close — commit `<SHA4>`
- [x] Task 5: CLI SIGINT handler + `SessionContext.cancel_token` — commit `<SHA5>`
- [x] Task 6: Closeout (this commit)

**Key design decisions:** spec at `docs/superpowers/specs/2026-06-10-graceful-interrupt-and-poll-observability-design.md`.

**Carry-forwards / known follow-ups:**
- Diffusers / Hosted-API / Bedrock per-backend cancel hardening (inherited via ABC; activate when one of those engines exhibits a similar stall).
- `kinoforge reap --orphans` helper that walks RunPod REST for pods absent from the ledger.

**Test delta:** +<N> net tests (offline only; no live smoke required).
```

Fill `<SHA0>` ... `<SHA5>` with `git log --oneline -10` output. Fill `<N>` with the new-test count.

Update the "Single next action" line near the top of PROGRESS.md to reflect Phase 50 closeout.

If new Carry-forward items belong in the canonical "Known limitations & follow-ups" index (e.g. backend-specific cancel hardening as a new C-section item), add them there too.

- [ ] **Step 3: Pre-commit run.**

```
pixi run pre-commit run --files README.md PROGRESS.md
```

Expected: PASS.

- [ ] **Step 4: Commit.**

```bash
git add README.md PROGRESS.md
git commit -m "docs(progress+readme): Phase 50 closeout — graceful interrupt + poll observability"
```

---

## Final verification

After Task 6 commit, run the full suite + a clean-tree check:

```
pixi run pytest -q
git status
git log --oneline -10
```

Expected:
- Full test suite green.
- Clean working tree.
- Six new commits visible in `git log`.

No live smoke required for this layer. The next operator-initiated `kinoforge generate` carries the new per-tick logging and the new hard timeout for free; any future stall will self-diagnose via the structured `status` / `queue_pos` / `exec_node` log fields.
