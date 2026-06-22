# Proxy-Retry Shared Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate `URLError`/`OSError` crashes across diffusers and comfyui poll loops by introducing a shared `retry_proxy_call` with a policy-parameterized retry strategy; close diffusers' pre-existing `cancel_token`-ignored debt at the same time.

**Architecture:** New module `src/kinoforge/engines/_proxy_retry.py` holds `RetryPolicy` (frozen dataclass), `RUNPOD_PROXY_POLICY` (module constant), generic `retry_proxy_call[T]`, and module-level `interpoll_wait`. Comfyui's existing `_retry_proxy_call`, `_PROXY_TRANSIENT_CODES`, `_SUBMIT_RETRY_BACKOFFS`, and the `_interpoll_wait` closure delete; both engines import from the new module. Diffusers `result()` rewritten in comfyui-parity shape (token-aware, wall-clock-timeout, last_transient diagnostic). All four diffusers HTTP transits (submit, result, lora.set_stack, artifact download) gain retry symmetrically.

**Tech Stack:** Python 3.13 stdlib only (dataclasses, urllib.error, logging, time, threading via existing `CancelToken`). pytest + pytest-mock for tests.

**User decisions (already made):**
- Option 2 scope: shared helper + diffusers + comfyui (not hosted/fal)
- Function + frozen RetryPolicy dataclass API shape (not kwargs, not wrappers, not class)
- Full comfyui parity for diffusers result-loop restructure (cancel_token + wall-clock + last_transient)
- Coverage = all four diffusers call sites (submit, result, lora.set_stack, artifact)
- Reuse existing `kinoforge.core.cancel._NULL_TOKEN` rather than creating a new constant (refinement during planning)

**Spec:** `docs/superpowers/specs/2026-06-21-proxy-retry-shared-helper-design.md`

**Existing native tasks:** #18-#26 already created during brainstorming. This plan enhances those tasks with full implementation steps; no new task IDs are created.

---

## File Structure

**New files:**
- `src/kinoforge/engines/_proxy_retry.py` — shared retry primitive
- `tests/engines/test_proxy_retry.py` — helper unit tests
- `tests/engines/test_diffusers_result_loop.py` — restructured result-loop tests
- `tests/engines/test_diffusers_submit_retry.py` — submit-site retry tests
- `tests/engines/test_diffusers_lora_retry.py` — lora-site retry tests
- `tests/engines/test_diffusers_artifact_retry.py` — artifact-site retry tests
- `tests/engines/test_comfyui_result_url_error.py` — new URLError absorption in comfyui result()

**Modified files:**
- `src/kinoforge/engines/diffusers/__init__.py` — result() restructure + 3 wrap sites + `_poll_timeout_s`/`_poll_interval_s` plumbing
- `src/kinoforge/engines/comfyui/__init__.py` — delete dead constants/helpers, import shared helper, add catch_classes branch in result()
- `tests/engines/conftest.py` — add `FAST_POLICY` fixture
- `tests/engines/test_comfyui.py` — mechanical signature updates + behavior update for `test_result_raises_after_persistent_404` and submit-upload URLError test

---

## Task 18: Create engines/_proxy_retry.py module

**Goal:** Ship the shared retry primitive used by both diffusers and comfyui.

**Files:**
- Create: `src/kinoforge/engines/_proxy_retry.py`

**Acceptance Criteria:**
- [ ] `RetryPolicy` is `@dataclass(frozen=True)`; mutation raises `FrozenInstanceError`
- [ ] `RUNPOD_PROXY_POLICY.transient_codes == frozenset({404, 502, 503, 504})`
- [ ] `RUNPOD_PROXY_POLICY.backoffs == (1.0, 2.0, 4.0, 8.0, 16.0, 16.0)`
- [ ] `RUNPOD_PROXY_POLICY.catch_classes == (URLError, OSError)`
- [ ] `RUNPOD_PROXY_POLICY.label_prefix == "proxy"`
- [ ] `retry_proxy_call` dispatches HTTPError BEFORE URLError catch (subclass ordering)
- [ ] `retry_proxy_call` re-raises non-transient HTTPError on attempt 1 without sleep
- [ ] `retry_proxy_call` retries transient HTTPError and `policy.catch_classes`
- [ ] After backoff exhaustion, the LAST exception raises
- [ ] `interpoll_wait(seconds, None, sleep)` → calls `sleep(seconds)`, returns False
- [ ] `interpoll_wait(seconds, token, _)` → calls `token.wait(seconds)`, returns its bool
- [ ] mypy clean, ruff clean

**Verify:** `pixi run pytest tests/engines/test_proxy_retry.py -v` (after Task 19) → all pass. For this task in isolation: `pixi run mypy src/kinoforge/engines/_proxy_retry.py` and `pixi run ruff check src/kinoforge/engines/_proxy_retry.py`.

**Steps:**

- [ ] **Step 1: Write the failing test (TDD red — minimal sanity check)**

Create `tests/engines/test_proxy_retry.py` with one anchor test that proves the module loads:

```python
"""Tests for the shared proxy-retry helper."""

from __future__ import annotations

import urllib.error
from dataclasses import FrozenInstanceError

import pytest


def test_module_loads_and_exposes_public_surface() -> None:
    from kinoforge.engines._proxy_retry import (
        RUNPOD_PROXY_POLICY,
        RetryPolicy,
        interpoll_wait,
        retry_proxy_call,
    )

    assert isinstance(RUNPOD_PROXY_POLICY, RetryPolicy)
    assert callable(retry_proxy_call)
    assert callable(interpoll_wait)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run pytest tests/engines/test_proxy_retry.py::test_module_loads_and_exposes_public_surface -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kinoforge.engines._proxy_retry'`.

- [ ] **Step 3: Write the module**

Create `src/kinoforge/engines/_proxy_retry.py`:

```python
"""Shared proxy-retry primitive for RunPod-proxy-fronted engines.

Consumed by diffusers and comfyui engine subpackages. Hosted/fal engines
sit in a different fault domain (vendor APIs, per-call billing) and do
not import this module today; if they opt in later, they MUST pass an
explicit policy to ``retry_proxy_call`` (the default
:data:`RUNPOD_PROXY_POLICY` is calibrated for the RunPod proxy only).
"""

from __future__ import annotations

import logging
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass

from kinoforge.core.cancel import CancelToken

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry strategy for a single fault domain.

    Attributes:
        transient_codes: HTTP status codes treated as transient
            (eligible for retry via :data:`backoffs`).
        backoffs: Per-attempt sleep schedule. Length determines the
            maximum number of retries (total attempts = ``1 + len(backoffs)``).
        catch_classes: Non-HTTPError exception classes treated as
            transient. Typical contents: ``(URLError, OSError)`` to
            absorb TLS resets, DNS failures, and socket-level errors.
            HTTPError is a subclass of URLError; the helper dispatches
            HTTPError first so this catch only fires for non-HTTP
            URLError variants.
        label_prefix: Tag prepended to WARNING log lines (e.g.
            ``"proxy"``). Concatenated with the per-call ``label``
            argument as ``[<prefix>.<label>]``.
    """

    transient_codes: frozenset[int]
    backoffs: tuple[float, ...]
    catch_classes: tuple[type[BaseException], ...]
    label_prefix: str


RUNPOD_PROXY_POLICY = RetryPolicy(
    transient_codes=frozenset({404, 502, 503, 504}),
    backoffs=(1.0, 2.0, 4.0, 8.0, 16.0, 16.0),
    catch_classes=(urllib.error.URLError, OSError),
    label_prefix="proxy",
)
"""Retry policy for RunPod-proxy-fronted HTTP calls.

Preserves the Phase 47 backoff calibration that closed the
``/upload/image`` + ``/history/{id}`` 404 race documented in
``project_task7_comfyui_404_regression``.
"""


def retry_proxy_call[T](
    label: str,
    url: str,
    fn: Callable[[], T],
    sleep: Callable[[float], None],
    policy: RetryPolicy = RUNPOD_PROXY_POLICY,
) -> T:
    """Run *fn* with bounded retry on transient proxy failures.

    Retries on (a) :class:`urllib.error.HTTPError` whose ``.code`` is in
    ``policy.transient_codes`` and (b) any exception class in
    ``policy.catch_classes``. Non-transient :class:`HTTPError` re-raises
    immediately. After ``policy.backoffs`` is exhausted, the final
    transient exception re-raises.

    Args:
        label: Call-site tag for log lines (e.g. ``"diffusers.result"``).
        url: URL passed to *fn*; included in WARNING messages.
        fn: Zero-arg callable performing the HTTP request.
        sleep: Injected sleep seam; receives per-attempt backoff seconds.
        policy: Retry policy. Defaults to :data:`RUNPOD_PROXY_POLICY`.
            Callers outside the RunPod-proxy fault domain MUST pass an
            explicit policy.

    Returns:
        Successful return value of *fn*.

    Raises:
        urllib.error.HTTPError: Last transient HTTPError after backoff
            exhaustion, or any non-transient HTTPError on any attempt.
        BaseException: Last instance of any ``policy.catch_classes``
            type after backoff exhaustion.
    """
    last_exc: BaseException | None = None
    attempts = 1 + len(policy.backoffs)
    for attempt_idx, delay in enumerate((0.0,) + policy.backoffs):
        if delay > 0:
            sleep(delay)
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code not in policy.transient_codes:
                raise
            _log.warning(
                "[%s.%s] transient HTTPError url=%s code=%d attempt=%d/%d",
                policy.label_prefix,
                label,
                url,
                exc.code,
                attempt_idx + 1,
                attempts,
            )
            last_exc = exc
        except policy.catch_classes as exc:
            _log.warning(
                "[%s.%s] transient transport-error url=%s type=%s "
                "reason=%s attempt=%d/%d",
                policy.label_prefix,
                label,
                url,
                type(exc).__name__,
                str(exc)[:200],
                attempt_idx + 1,
                attempts,
            )
            last_exc = exc
    if last_exc is None:  # pragma: no cover - unreachable
        raise RuntimeError(
            "retry_proxy_call exited loop without recording an error"
        )
    raise last_exc


def interpoll_wait(
    seconds: float,
    cancel_token: CancelToken | None,
    sleep: Callable[[float], None],
) -> bool:
    """Cancel-aware inter-poll sleep.

    If *cancel_token* is ``None``, falls back to *sleep* (legacy callers
    + tests that stub ``sleep=lambda s: None`` to keep the loop instant).
    Otherwise, blocks on ``cancel_token.wait(seconds)`` so a mid-wait
    cancellation returns promptly.

    Args:
        seconds: Maximum wait in seconds.
        cancel_token: Token to honor, or ``None`` to skip honoring.
        sleep: Sleep seam used when *cancel_token* is ``None``.

    Returns:
        ``True`` if the cancel token fired during the wait (caller
        should re-check via ``raise_if_set``); ``False`` if the wait
        completed naturally.
    """
    if cancel_token is None:
        sleep(seconds)
        return False
    return cancel_token.wait(seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run pytest tests/engines/test_proxy_retry.py::test_module_loads_and_exposes_public_surface -v`
Expected: PASS.

- [ ] **Step 5: Lint + typecheck**

Run: `pixi run ruff check src/kinoforge/engines/_proxy_retry.py && pixi run ruff format --check src/kinoforge/engines/_proxy_retry.py && pixi run mypy src/kinoforge/engines/_proxy_retry.py`
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/_proxy_retry.py tests/engines/test_proxy_retry.py
pixi run pre-commit run --files src/kinoforge/engines/_proxy_retry.py tests/engines/test_proxy_retry.py
git add pixi.lock 2>/dev/null || true
git commit -m "feat: add shared retry_proxy_call + RetryPolicy primitive

Introduces engines/_proxy_retry.py with a frozen RetryPolicy dataclass,
the RUNPOD_PROXY_POLICY constant (preserving Phase 47 backoff
calibration), a generic retry_proxy_call[T] helper, and the
cancel-aware interpoll_wait. Consumers (diffusers, comfyui) land in
follow-up commits."
```

---

## Task 19: Write tests/engines/test_proxy_retry.py (full coverage)

**Goal:** Test the helper exhaustively per the test-design skill — every test states the behavior under test and the concrete bug it would catch.

**Files:**
- Modify: `tests/engines/test_proxy_retry.py` (extend the anchor test from Task 18)
- Modify: `tests/engines/conftest.py` (add `FAST_POLICY` fixture)

**Acceptance Criteria:**
- [ ] All 12 tests listed in Steps below pass
- [ ] Each test docstring or name encodes the concrete bug it catches
- [ ] No mock that mirrors the helper's internal control flow (mocks only at the HTTP boundary — the `fn` callable)
- [ ] Sleep schedule asserted with EXACT values, not "called multiple times"
- [ ] Anti-pattern guard: no test asserts a behavior that's only true because the test itself stubbed it

**Verify:** `pixi run pytest tests/engines/test_proxy_retry.py -v` → 12 tests pass.

**Steps:**

- [ ] **Step 1: Add `FAST_POLICY` fixture to conftest**

Edit `tests/engines/conftest.py` to add:

```python
# (existing imports + fixtures preserved above)

import dataclasses
import urllib.error

import pytest

from kinoforge.engines._proxy_retry import RUNPOD_PROXY_POLICY, RetryPolicy


@pytest.fixture
def fast_policy() -> RetryPolicy:
    """RetryPolicy with three zero-second retries for retry-aware tests.

    Same transient codes + catch classes as RUNPOD_PROXY_POLICY so
    dispatch behavior is identical; only the schedule is compressed so
    tests finish in microseconds.
    """
    return dataclasses.replace(
        RUNPOD_PROXY_POLICY, backoffs=(0.0, 0.0, 0.0)
    )
```

- [ ] **Step 2: Write all 12 helper tests**

Replace the body of `tests/engines/test_proxy_retry.py` (keeping the anchor test) with:

```python
"""Tests for the shared proxy-retry helper.

Every test asserts an observable behavior and names (in its docstring)
the concrete bug it catches. Mocks live only at the HTTP boundary
(the ``fn`` callable + the ``sleep`` callable); they do not mirror
the helper's internal control flow.
"""

from __future__ import annotations

import dataclasses
import urllib.error
from dataclasses import FrozenInstanceError

import pytest

from kinoforge.engines._proxy_retry import (
    RUNPOD_PROXY_POLICY,
    RetryPolicy,
    interpoll_wait,
    retry_proxy_call,
)


def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example/x",
        code=code,
        msg="x",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --- public surface ---------------------------------------------------


def test_module_loads_and_exposes_public_surface() -> None:
    """Catches: accidental rename or deletion of public exports."""
    assert isinstance(RUNPOD_PROXY_POLICY, RetryPolicy)
    assert callable(retry_proxy_call)
    assert callable(interpoll_wait)


def test_runpod_policy_constant_values() -> None:
    """Catches: drift from Phase 47 calibration (silent behavior change).

    The exact constants are load-bearing for the comfyui regression
    suite + diffusers production behavior.
    """
    assert RUNPOD_PROXY_POLICY.transient_codes == frozenset({404, 502, 503, 504})
    assert RUNPOD_PROXY_POLICY.backoffs == (1.0, 2.0, 4.0, 8.0, 16.0, 16.0)
    assert RUNPOD_PROXY_POLICY.catch_classes == (urllib.error.URLError, OSError)
    assert RUNPOD_PROXY_POLICY.label_prefix == "proxy"


def test_policy_is_frozen() -> None:
    """Catches: someone flips frozen=False, introducing shared mutable state."""
    with pytest.raises(FrozenInstanceError):
        RUNPOD_PROXY_POLICY.backoffs = ()  # type: ignore[misc]


# --- retry_proxy_call dispatch ----------------------------------------


def test_retries_transient_http_code(fast_policy: RetryPolicy) -> None:
    """Catches: HTTPError transient-codes branch dropped or .code check omitted."""
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return "ok"

    result = retry_proxy_call("test", "http://x", fn, sleeps.append, fast_policy)
    assert result == "ok"
    assert attempts["n"] == 3
    assert sleeps == [0.0, 0.0]


def test_raises_non_transient_http_immediately(fast_policy: RetryPolicy) -> None:
    """Catches: missing ``code not in transient_codes`` guard.

    A 500 must propagate on attempt 1 without sleep — otherwise we
    retry hard-failures and amplify load on the server.
    """
    sleeps: list[float] = []

    def fn() -> str:
        raise _make_http_error(500)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, sleeps.append, fast_policy)
    assert exc_info.value.code == 500
    assert sleeps == []


def test_retries_url_error(fast_policy: RetryPolicy) -> None:
    """Catches: catch_classes branch missing or URLError absent from policy.

    This is the exact failure mode from the user's 2026-06-21 crash.
    """
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return "ok"

    result = retry_proxy_call("test", "http://x", fn, sleeps.append, fast_policy)
    assert result == "ok"
    assert attempts["n"] == 2
    assert sleeps == [0.0]


def test_retries_os_error(fast_policy: RetryPolicy) -> None:
    """Catches: OSError absent from catch_classes."""
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise OSError(104, "Connection reset by peer")
        return "ok"

    assert retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy) == "ok"
    assert attempts["n"] == 2


def test_exhausted_retry_reraises_last(fast_policy: RetryPolicy) -> None:
    """Catches: helper masks the final exception with RuntimeError or first-exc."""
    counter = {"n": 0}

    def fn() -> None:
        counter["n"] += 1
        raise _make_http_error(503)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy)
    assert exc_info.value.code == 503
    # 1 initial + 3 retries = 4 attempts with FAST_POLICY
    assert counter["n"] == 4


def test_success_on_third_attempt_returns_value(fast_policy: RetryPolicy) -> None:
    """Catches: helper keeps sleeping/retrying after success."""
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fn() -> int:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return 42

    assert retry_proxy_call("test", "http://x", fn, sleeps.append, fast_policy) == 42
    assert sleeps == [0.0, 0.0]


def test_sleep_called_with_exact_schedule() -> None:
    """Catches: off-by-one in backoff indexing or wrong tuple element selected."""
    sleeps: list[float] = []
    attempts = {"n": 0}
    policy = dataclasses.replace(
        RUNPOD_PROXY_POLICY, backoffs=(0.5, 1.5, 3.5)
    )

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return "ok"

    retry_proxy_call("test", "http://x", fn, sleeps.append, policy)
    assert sleeps == [0.5, 1.5]


def test_http_error_dispatch_precedes_url_error() -> None:
    """Catches: except-clause ordering inverted.

    HTTPError is a subclass of URLError. If the URLError branch is
    listed first, a non-transient HTTPError (e.g. 500) would be caught
    as URLError, logged as transport-error, retried, and never propagate
    correctly. This test asserts a 500 raises ON ATTEMPT 1 even though
    URLError is in catch_classes.
    """
    sleeps: list[float] = []
    policy = dataclasses.replace(
        RUNPOD_PROXY_POLICY,
        backoffs=(0.0, 0.0, 0.0),
        catch_classes=(urllib.error.URLError, OSError),
    )

    def fn() -> str:
        raise _make_http_error(500)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, sleeps.append, policy)
    assert exc_info.value.code == 500
    assert sleeps == []


# --- interpoll_wait ---------------------------------------------------


def test_interpoll_wait_none_token_uses_sleep() -> None:
    """Catches: regression where None-token path raises AttributeError."""
    sleeps: list[float] = []
    result = interpoll_wait(2.5, None, sleeps.append)
    assert result is False
    assert sleeps == [2.5]


def test_interpoll_wait_token_returns_token_wait_result() -> None:
    """Catches: helper swallows the token's wait return value (lost cancel signal)."""
    from kinoforge.core.cancel import CancelToken

    token = CancelToken()
    sleeps: list[float] = []
    # Pre-set the token so wait returns True immediately.
    token.set()
    result = interpoll_wait(10.0, token, sleeps.append)
    assert result is True
    assert sleeps == []  # token path does not call the sleep seam
```

- [ ] **Step 3: Run all tests**

Run: `pixi run pytest tests/engines/test_proxy_retry.py -v`
Expected: 12 PASS.

- [ ] **Step 4: Lint + typecheck**

Run: `pixi run ruff check tests/engines/test_proxy_retry.py tests/engines/conftest.py && pixi run mypy tests/engines/test_proxy_retry.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/engines/test_proxy_retry.py tests/engines/conftest.py
pixi run pre-commit run --files tests/engines/test_proxy_retry.py tests/engines/conftest.py
git add pixi.lock 2>/dev/null || true
git commit -m "test: cover retry_proxy_call dispatch, exhaustion, and interpoll_wait

12 tests with bug-catching docstrings. FAST_POLICY fixture in
conftest reused by downstream engine tests."
```

---

## Task 20: Restructure diffusers result() with full comfyui parity

**Goal:** Replace the diffusers `result()` for-loop with a while-True loop that honors `cancel_token`, enforces `poll_timeout_s`, absorbs both transient HTTP codes and transport errors via `retry_proxy_call`, and re-raises `last_transient` over bare `TimeoutError` when sustained transients caused the timeout.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`DiffusersBackend.__init__`, `DiffusersBackend.result`, `DiffusersEngine._make_backend`)

**Acceptance Criteria:**
- [ ] `del cancel_token` removed from `result()`
- [ ] `cancel_token.raise_if_set()` called at top of every loop iteration
- [ ] New `self._poll_timeout_s: float = 1800.0` instance state, accepted by `__init__`
- [ ] New `self._poll_interval_s: float = 1.0` instance state, accepted by `__init__`
- [ ] `_make_backend` forwards both new params (defaults preserve today's behavior)
- [ ] Outer try around `retry_proxy_call` catches HTTPError (transient codes → continue with last_transient) and `RUNPOD_PROXY_POLICY.catch_classes` (transport errors → continue with last_transient)
- [ ] Inter-poll wait via `interpoll_wait(self._poll_interval_s, token, self._sleep)`
- [ ] On timeout: raise `last_transient` if set, else `TimeoutError`
- [ ] `_MAX_POLL` retained as belt-and-braces iteration cap
- [ ] `status == "done"` returns Artifact with base_url-derived URL (existing behavior preserved)
- [ ] `status == "error"` raises GenerationError (existing behavior preserved)

**Verify:** `pixi run pytest tests/engines/test_diffusers_result_loop.py -v` (after Task 21) → all pass. For this task: `pixi run mypy src/kinoforge/engines/diffusers/__init__.py` clean.

**Steps:**

- [ ] **Step 1: Add new instance state to `DiffusersBackend.__init__`**

In `src/kinoforge/engines/diffusers/__init__.py`, update the `__init__` signature and body (around line 289). Add the two new kwargs:

```python
    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        asset_paths: dict[str, str] | None = None,
        prompt_body_key: str | None = "prompt",
        poll_timeout_s: float = 1800.0,
        poll_interval_s: float = 1.0,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the diffusers server, e.g.
                ``"http://127.0.0.1:8000"``.  No trailing slash.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            asset_paths: Optional mapping from role name to dot-path in the
                request body where the matching asset's URI is written.
            prompt_body_key: Top-level body key written from
                ``resolve_prompt(job)`` when no explicit ``spec["prompt"]``
                is provided. ``None`` / empty disables routing entirely.
            poll_timeout_s: Wall-clock cap on ``result()`` polling.
                Default ``1800.0`` matches the legacy ``_MAX_POLL * 1.0s``
                effective bound.
            poll_interval_s: Sleep between successive ``/status`` polls.
                Default ``1.0`` matches today's hard-coded value.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe_profile
        self._sleep = sleep
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
        self._prompt_body_key: str | None = prompt_body_key
        self._poll_timeout_s = poll_timeout_s
        self._poll_interval_s = poll_interval_s
```

- [ ] **Step 2: Rewrite `result()` with comfyui-parity shape**

Replace `DiffusersBackend.result()` body (lines ~390-446) with:

```python
    def result(
        self,
        job_id: str,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Artifact:
        """Poll ``/status/{job_id}`` until ``status == "done"``.

        Honors *cancel_token* both at the top of every iteration and
        across the inter-poll wait. Absorbs transient HTTPError codes
        and transport-class exceptions (URLError, OSError) via
        :func:`retry_proxy_call`. On wall-clock timeout, re-raises the
        last absorbed transient in preference to a bare TimeoutError so
        operators see the underlying proxy failure.

        Args:
            job_id: The job ID returned by a prior ``submit`` call.
            cancel_token: Cooperative cancellation token. ``None``
                (or default) means cancellation is not honored.

        Returns:
            An ``Artifact`` whose ``filename`` comes from the server
            response and whose ``meta`` contains ``{"job_id": job_id}``.
            The ``url`` is built from this backend's ``base_url`` so
            remote pods resolve through the RunPod proxy (the
            server-supplied ``localhost:8000`` URL is ignored).

        Raises:
            TimeoutError: Wall-clock or iteration-count exceeded with
                no sustained transient to surface.
            urllib.error.HTTPError: Re-raised when a sustained transient
                caused the timeout (preferred over TimeoutError).
            urllib.error.URLError | OSError: Same as above for
                transport-class transients.
            kinoforge.core.errors.Cancelled: ``cancel_token`` fired.
            GenerationError: Server reported ``status == "error"``.
        """
        from kinoforge.core.cancel import _NULL_TOKEN
        from kinoforge.core.errors import GenerationError
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            interpoll_wait,
            retry_proxy_call,
        )

        token = cancel_token if cancel_token is not None else _NULL_TOKEN
        url = f"{self._base_url}/status/{job_id}"
        start = time.monotonic()
        last_transient: BaseException | None = None
        poll_idx = 0
        while True:
            token.raise_if_set()
            elapsed = time.monotonic() - start
            if poll_idx >= _MAX_POLL or elapsed > self._poll_timeout_s:
                if last_transient is not None:
                    raise last_transient
                raise TimeoutError(
                    f"diffusers poll timed out after {elapsed:.1f}s "
                    f"(job={job_id}, polls={poll_idx})"
                )
            try:
                data = retry_proxy_call(
                    "diffusers.result",
                    url,
                    lambda: self._http_get(url),
                    self._sleep,
                    RUNPOD_PROXY_POLICY,
                )
            except urllib.error.HTTPError as exc:
                if exc.code in RUNPOD_PROXY_POLICY.transient_codes:
                    _log.warning(
                        "[diffusers.result] transient HTTPError exhausted "
                        "elapsed=%.1fs job=%s code=%d",
                        elapsed,
                        job_id,
                        exc.code,
                    )
                    last_transient = exc
                    poll_idx += 1
                    if interpoll_wait(
                        self._poll_interval_s, cancel_token, self._sleep
                    ):
                        token.raise_if_set()
                    continue
                raise
            except RUNPOD_PROXY_POLICY.catch_classes as exc:
                _log.warning(
                    "[diffusers.result] transient transport-error exhausted "
                    "elapsed=%.1fs job=%s type=%s",
                    elapsed,
                    job_id,
                    type(exc).__name__,
                )
                last_transient = exc
                poll_idx += 1
                if interpoll_wait(
                    self._poll_interval_s, cancel_token, self._sleep
                ):
                    token.raise_if_set()
                continue

            status = data.get("status")
            if status == "done":
                filename = str(data.get("filename", ""))
                artifact_url = f"{self._base_url.rstrip('/')}/artifacts/{filename}"
                return Artifact(
                    filename=filename, url=artifact_url, meta={"job_id": job_id}
                )
            if status == "error":
                err_msg = str(
                    data.get("error", "<server reported error with no message>")
                )
                raise GenerationError(
                    f"diffusers server reported error for job {job_id!r}: {err_msg}"
                )
            poll_idx += 1
            if interpoll_wait(self._poll_interval_s, cancel_token, self._sleep):
                token.raise_if_set()
```

Also add at the top of `src/kinoforge/engines/diffusers/__init__.py` (with the other module-level imports):

```python
import logging
import urllib.error

_log = logging.getLogger(__name__)
```

(Skip whichever already exists; `_log` may already be defined — verify first.)

- [ ] **Step 3: Update `DiffusersEngine._make_backend` to forward new kwargs**

Around line 1004, update the factory:

```python
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=self._probe,
            sleep=self._sleep,
            asset_paths=asset_paths,
            prompt_body_key=prompt_body_key,
            poll_timeout_s=float(
                diffusers_cfg.get("poll_timeout_s", 1800.0)
            ),
            poll_interval_s=float(
                diffusers_cfg.get("poll_interval_s", 1.0)
            ),
        )
```

- [ ] **Step 4: Lint + typecheck the engine module**

Run: `pixi run ruff check src/kinoforge/engines/diffusers/__init__.py && pixi run mypy src/kinoforge/engines/diffusers/__init__.py`
Expected: clean.

- [ ] **Step 5: Smoke-run existing diffusers tests**

Run: `pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_backend_error_handling.py tests/engines/test_diffusers_wait_for_ready.py -x -v`
Expected: all PASS. Any failures here indicate the restructure broke an existing contract.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py
git add pixi.lock 2>/dev/null || true
git commit -m "refactor(diffusers): rewrite result() with comfyui-parity poll loop

Restructures DiffusersBackend.result() to use a while True loop that
honors cancel_token (closing pre-existing del cancel_token debt),
absorbs transient HTTP codes + transport errors via retry_proxy_call,
enforces a wall-clock poll_timeout_s, and re-raises last_transient
over TimeoutError when sustained transients caused the timeout. Adds
self._poll_timeout_s and self._poll_interval_s kwargs (defaults
preserve today's behavior); _make_backend forwards them from cfg."
```

---

## Task 21: Write tests/engines/test_diffusers_result_loop.py

**Goal:** Cover the restructured result-loop per the test-design skill.

**Files:**
- Create: `tests/engines/test_diffusers_result_loop.py`

**Acceptance Criteria:**
- [ ] 8 tests listed in Steps below all pass
- [ ] Mocks live at the `http_get` boundary, not at retry-helper internals
- [ ] Cancel-token tests use a real `CancelToken` (not a mock) and assert observable side effects (raise of `Cancelled`, zero `http_get` calls, etc.)
- [ ] The TLS-reset test reproduces the exact original crash: two `URLError(ConnectionResetError)`s followed by `{"status": "done"}`, expects Artifact returned

**Verify:** `pixi run pytest tests/engines/test_diffusers_result_loop.py -v` → 8 PASS.

**Steps:**

- [ ] **Step 1: Write the test file**

Create `tests/engines/test_diffusers_result_loop.py`:

```python
"""Tests for DiffusersBackend.result() restructure.

Each test states the behavior under test and the concrete bug it
would catch. Mocks are at the HTTP boundary (http_get callable)
plus the clock + sleep seams; no mocking of retry_proxy_call itself.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import Cancelled, GenerationError
from kinoforge.engines.diffusers import DiffusersBackend
from kinoforge.core.profiles import ModelProfile


def _make_backend(
    *,
    http_get: Callable[[str], dict[str, Any]],
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_timeout_s: float = 60.0,
    poll_interval_s: float = 0.0,
) -> DiffusersBackend:
    return DiffusersBackend(
        http_post=http_post or (lambda _u, _b: {"job_id": "jid"}),
        http_get=http_get,
        base_url="http://pod.example",
        probe_profile=ModelProfile(
            name="test",
            max_frames=81,
            fps=24,
            supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False,
            supports_joint_audio=False,
        ),
        sleep=sleep or (lambda _s: None),
        poll_timeout_s=poll_timeout_s,
        poll_interval_s=poll_interval_s,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="x",
        hdrs=None, fp=None,  # type: ignore[arg-type]
    )


# --- cancel_token honoring -------------------------------------------


def test_cancel_token_raises_before_first_io() -> None:
    """Catches: del cancel_token regression or pre-I/O check missing.

    A token set before result() is called must abort BEFORE any
    http_get call lands.
    """
    calls: list[str] = []

    def http_get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"status": "done", "filename": "x.mp4"}

    backend = _make_backend(http_get=http_get)
    token = CancelToken()
    token.set()

    with pytest.raises(Cancelled):
        backend.result("jid", cancel_token=token)
    assert calls == []


def test_cancel_token_raises_during_interpoll_wait() -> None:
    """Catches: inter-poll sleep not token-aware.

    A token set between polls must surface as Cancelled within the
    next poll_interval_s window, not at the next http_get call.
    """
    state = {"polls": 0}
    token = CancelToken()

    def http_get(url: str) -> dict[str, Any]:
        state["polls"] += 1
        if state["polls"] == 1:
            # Schedule cancellation BEFORE returning, so the inter-poll
            # wait sees a pre-set token immediately.
            token.set()
        return {"status": "pending"}

    backend = _make_backend(http_get=http_get, poll_interval_s=10.0)

    with pytest.raises(Cancelled):
        backend.result("jid", cancel_token=token)
    # Should hit at most one http_get before cancel surfaces.
    assert state["polls"] == 1


# --- timeout bounds ---------------------------------------------------


def test_wall_clock_timeout_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches: poll_idx-only bound (sleep-stubbed tests would hang).

    Stub time.monotonic so the clock advances past poll_timeout_s
    within a few iterations. Expect TimeoutError.
    """
    times = iter([0.0, 0.5, 1.5, 100.0, 200.0])

    def fake_monotonic() -> float:
        return next(times)

    import kinoforge.engines.diffusers as diffusers_mod

    monkeypatch.setattr(diffusers_mod.time, "monotonic", fake_monotonic)

    def http_get(url: str) -> dict[str, Any]:
        return {"status": "pending"}

    backend = _make_backend(http_get=http_get, poll_timeout_s=10.0)
    with pytest.raises(TimeoutError):
        backend.result("jid")


def test_max_poll_belt_and_braces() -> None:
    """Catches: removing _MAX_POLL fallback (sleep-stubbed tests would loop forever).

    With sleep stubbed to noop and the clock running at real speed,
    poll_timeout_s might not fire within a reasonable test wall-clock.
    The _MAX_POLL iteration cap is the safety net.
    """
    import kinoforge.engines.diffusers as diffusers_mod

    # Lower _MAX_POLL just for this test via monkeypatch.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(diffusers_mod, "_MAX_POLL", 5)

        def http_get(url: str) -> dict[str, Any]:
            return {"status": "pending"}

        backend = _make_backend(
            http_get=http_get,
            poll_timeout_s=100000.0,  # wall-clock will not fire
            poll_interval_s=0.0,
        )
        with pytest.raises(TimeoutError):
            backend.result("jid")


def test_last_transient_preferred_over_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches: bare TimeoutError masking the actual proxy failure.

    If sustained 502s cause the timeout, operators must see the
    HTTPError(502) not a generic TimeoutError.
    """
    import kinoforge.engines.diffusers as diffusers_mod

    times = iter([0.0, 0.5, 1.5, 100.0, 200.0, 300.0])
    monkeypatch.setattr(
        diffusers_mod.time, "monotonic", lambda: next(times)
    )

    def http_get(url: str) -> dict[str, Any]:
        raise _http_error(502)

    backend = _make_backend(
        http_get=http_get, poll_timeout_s=10.0, poll_interval_s=0.0
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.result("jid")
    assert exc_info.value.code == 502


# --- the actual original crash ---------------------------------------


def test_tls_reset_absorbed_then_done() -> None:
    """Catches: the exact ConnectionResetError crash from 2026-06-21.

    Two URLError(ConnectionResetError)s in a row, then status=done.
    Expect the helper to absorb both and return an Artifact.
    """
    calls = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return {"status": "done", "filename": "out.mp4"}

    backend = _make_backend(http_get=http_get)
    artifact = backend.result("jid")
    assert artifact.filename == "out.mp4"
    assert artifact.url == "http://pod.example/artifacts/out.mp4"
    assert artifact.meta == {"job_id": "jid"}
    assert calls["n"] == 3


# --- status branches --------------------------------------------------


def test_status_done_builds_url_from_base() -> None:
    """Catches: regression where server-supplied (localhost) URL is honored.

    Workspace can't reach localhost on the pod; URL must be built from
    backend base_url. Server's url field is intentionally ignored.
    """

    def http_get(url: str) -> dict[str, Any]:
        return {
            "status": "done",
            "filename": "v.mp4",
            "url": "http://localhost:8000/artifacts/v.mp4",  # wrong; ignored
        }

    backend = _make_backend(http_get=http_get)
    artifact = backend.result("jid")
    assert artifact.url == "http://pod.example/artifacts/v.mp4"


def test_status_error_raises_generation_error() -> None:
    """Catches: error-path swallowed during refactor."""

    def http_get(url: str) -> dict[str, Any]:
        return {"status": "error", "error": "out of memory"}

    backend = _make_backend(http_get=http_get)
    with pytest.raises(GenerationError, match="out of memory"):
        backend.result("jid")
```

- [ ] **Step 2: Run the tests**

Run: `pixi run pytest tests/engines/test_diffusers_result_loop.py -v`
Expected: 8 PASS.

- [ ] **Step 3: Lint + typecheck**

Run: `pixi run ruff check tests/engines/test_diffusers_result_loop.py && pixi run mypy tests/engines/test_diffusers_result_loop.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/engines/test_diffusers_result_loop.py
pixi run pre-commit run --files tests/engines/test_diffusers_result_loop.py
git add pixi.lock 2>/dev/null || true
git commit -m "test(diffusers): cover restructured result() loop

8 tests: cancel_token honoring (pre-I/O + inter-poll), wall-clock
timeout, _MAX_POLL belt-and-braces, last_transient preference,
the exact 2026-06-21 TLS-reset crash, and the two status branches."
```

---

## Task 22: Wrap diffusers submit/lora/artifact call sites with retry_proxy_call

**Goal:** Symmetric retry coverage across all four diffusers HTTP transits.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`DiffusersBackend.submit`, `DiffusersBackend.set_lora_stack`, `DiffusersEngine.run` artifact-download site)

**Acceptance Criteria:**
- [ ] `submit()` POST `/generate` wrapped with `retry_proxy_call("diffusers.submit", ...)`; exhausted retry propagates
- [ ] `set_lora_stack()` wrapped with `retry_proxy_call("diffusers.lora.set_stack", ...)`; non-transient HTTPError still flows into `_translate_error_response` for semantic-error handling
- [ ] Artifact download at the engine layer wrapped with `retry_proxy_call("diffusers.artifact", ...)` using `_http_get_bytes`
- [ ] No existing tests regress

**Verify:** `pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_set_lora_stack.py tests/engines/test_diffusers_video_io.py -v` → all PASS. Task 23 adds the new retry-specific tests.

**Steps:**

- [ ] **Step 1: Wrap `submit()`**

In `src/kinoforge/engines/diffusers/__init__.py`, replace the body of `DiffusersBackend.submit()` (around line 343-388) — keep the asset-injection block intact, only change the final POST:

```python
        url = f"{self._base_url}/generate"
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            retry_proxy_call,
        )

        response = retry_proxy_call(
            "diffusers.submit",
            url,
            lambda: self._http_post(url, body),
            self._sleep,
            RUNPOD_PROXY_POLICY,
        )
        return str(response["job_id"])
```

- [ ] **Step 2: Wrap `set_lora_stack()`**

In `DiffusersBackend.set_lora_stack()` (around line 466-530), the body currently does:

```python
        url = f"{self._base_url}/lora/set_stack"
        body = ...  # existing
        try:
            resp = self._http_post(url, body)
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read() if hasattr(exc, "read") else b""
            self._translate_error_response(exc.code, body_bytes)
```

Replace the try block with:

```python
        from kinoforge.engines._proxy_retry import (
            RUNPOD_PROXY_POLICY,
            retry_proxy_call,
        )

        try:
            resp = retry_proxy_call(
                "diffusers.lora.set_stack",
                url,
                lambda: self._http_post(url, body),
                self._sleep,
                RUNPOD_PROXY_POLICY,
            )
        except urllib.error.HTTPError as exc:
            # Transient codes get re-raised by retry_proxy_call after
            # backoff exhaustion; non-transient codes carry the
            # semantic-error body that _translate_error_response parses.
            body_bytes = exc.read() if hasattr(exc, "read") else b""
            self._translate_error_response(exc.code, body_bytes)
            raise  # _translate_error_response either raises a typed
            # exception or returns; re-raise to preserve "always raises"
            # contract.
```

Important: read the actual current shape of `set_lora_stack` first — the surrounding code may differ. The change is mechanical: wrap the `self._http_post(url, body)` call in `retry_proxy_call` and keep the HTTPError except path intact. Re-verify after the edit that `_translate_error_response` is still reachable for non-transient HTTPError bodies.

- [ ] **Step 3: Wrap the engine-layer artifact download**

In `DiffusersEngine.run` (around line 1113), replace:

```python
            video_bytes = self._http_get_bytes(artifact.url)
```

with:

```python
            from kinoforge.engines._proxy_retry import (
                RUNPOD_PROXY_POLICY,
                retry_proxy_call,
            )

            video_bytes = retry_proxy_call(
                "diffusers.artifact",
                artifact.url,
                lambda: self._http_get_bytes(artifact.url),
                self._sleep,
                RUNPOD_PROXY_POLICY,
            )
```

The helper is generic in `T`; `_http_get_bytes` returns `bytes`, so `video_bytes: bytes` is inferred.

- [ ] **Step 4: Lint + typecheck**

Run: `pixi run ruff check src/kinoforge/engines/diffusers/__init__.py && pixi run mypy src/kinoforge/engines/diffusers/__init__.py`
Expected: clean.

- [ ] **Step 5: Run existing diffusers tests for regression**

Run: `pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_set_lora_stack.py tests/engines/test_diffusers_video_io.py tests/engines/test_diffusers_backend_error_handling.py -x -v`
Expected: all PASS. If `_translate_error_response`-related tests fail, the lora wrap broke the semantic-error path — fix before continuing.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py
git add pixi.lock 2>/dev/null || true
git commit -m "feat(diffusers): wrap submit/lora/artifact in retry_proxy_call

Symmetric retry coverage across all four HTTP transits against the
RunPod proxy. set_lora_stack preserves the _translate_error_response
semantic-error path: non-transient HTTPError bodies still parse
into typed exceptions; only transient codes + transport errors
get absorbed by the retry helper."
```

---

## Task 23: Write diffusers submit/lora/artifact retry tests

**Goal:** Cover the three additional wrap sites.

**Files:**
- Create: `tests/engines/test_diffusers_submit_retry.py`
- Create: `tests/engines/test_diffusers_lora_retry.py`
- Create: `tests/engines/test_diffusers_artifact_retry.py`

**Acceptance Criteria:**
- [ ] Each file has at least 3 tests: happy-then-transient-recover, exhaustion, non-transient-bypass
- [ ] Lora file adds a 4th test: non-transient HTTPError body still routes into `_translate_error_response`
- [ ] All tests pass

**Verify:** `pixi run pytest tests/engines/test_diffusers_submit_retry.py tests/engines/test_diffusers_lora_retry.py tests/engines/test_diffusers_artifact_retry.py -v`

**Steps:**

- [ ] **Step 1: Write submit-retry tests**

Create `tests/engines/test_diffusers_submit_retry.py`:

```python
"""Tests for DiffusersBackend.submit() retry-wrapping."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from kinoforge.engines.diffusers import DiffusersBackend
from kinoforge.core.profiles import ModelProfile
from kinoforge.core.types import GenerationJob, Segment


def _make_backend(http_post):
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda _u: {"status": "done", "filename": "x.mp4"},
        base_url="http://pod.example",
        probe_profile=ModelProfile(
            name="test", max_frames=81, fps=24, supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False, supports_joint_audio=False,
        ),
        sleep=lambda _s: None,
    )


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="x",
        hdrs=None, fp=None,  # type: ignore[arg-type]
    )


def _job() -> GenerationJob:
    return GenerationJob(
        spec={"prompt": "a cat on the moon"},
        segments=[Segment(assets=[])],
    )


def test_submit_recovers_from_transient_502() -> None:
    """Catches: submit not wrapped; bare _http_post crashes on first 502."""
    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(502)
        return {"job_id": "jid-42"}

    backend = _make_backend(http_post)
    assert backend.submit(_job()) == "jid-42"
    assert attempts["n"] == 2


def test_submit_exhaustion_raises_last_transient() -> None:
    """Catches: helper masks the final exception type."""

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _http_error(503)

    backend = _make_backend(http_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.submit(_job())
    assert exc_info.value.code == 503


def test_submit_non_transient_raises_immediately() -> None:
    """Catches: helper retries hard-fail status codes (would amplify load)."""
    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        raise _http_error(400)

    backend = _make_backend(http_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.submit(_job())
    assert exc_info.value.code == 400
    assert attempts["n"] == 1


def test_submit_recovers_from_tls_reset() -> None:
    """Catches: submit doesn't absorb transport errors (the original crash class)."""
    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return {"job_id": "ok"}

    backend = _make_backend(http_post)
    assert backend.submit(_job()) == "ok"
```

- [ ] **Step 2: Write lora-retry tests**

Create `tests/engines/test_diffusers_lora_retry.py`. Read the existing `tests/engines/test_diffusers_set_lora_stack.py` first to understand the spec shape for `set_lora_stack()` and the `_translate_error_response` semantic-error contract. Then add:

```python
"""Tests for DiffusersBackend.set_lora_stack() retry-wrapping.

The retry wrap MUST NOT swallow the _translate_error_response semantic-
error path: non-transient HTTPError bodies still parse into typed
exceptions; only transient codes + transport errors retry.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from kinoforge.engines.diffusers import DiffusersBackend
from kinoforge.core.profiles import ModelProfile


def _make_backend(http_post):
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda _u: {},
        base_url="http://pod.example",
        probe_profile=ModelProfile(
            name="test", max_frames=81, fps=24, supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False, supports_joint_audio=False,
        ),
        sleep=lambda _s: None,
    )


def _http_error(code: int, body: dict | None = None) -> urllib.error.HTTPError:
    fp = io.BytesIO(json.dumps(body or {}).encode("utf-8"))
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="x",
        hdrs=None, fp=fp,  # type: ignore[arg-type]
    )


def test_lora_recovers_from_transient_502() -> None:
    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(502)
        return {"ok": True}

    backend = _make_backend(http_post)
    # Use whatever the existing set_lora_stack signature is — adjust
    # the call below to match. Example placeholder:
    backend.set_lora_stack(loras=[])
    assert attempts["n"] == 2


def test_lora_exhaustion_raises_last_transient() -> None:
    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _http_error(503)

    backend = _make_backend(http_post)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        backend.set_lora_stack(loras=[])
    assert exc_info.value.code == 503


def test_lora_non_transient_routes_to_translate_error_response() -> None:
    """Catches: retry wrap swallows the semantic-error body parser.

    A 4xx with a structured body must hit _translate_error_response
    and surface as the matching typed exception (whatever the existing
    test_diffusers_set_lora_stack.py asserts).
    """
    # NOTE: This test's specific expected exception depends on what
    # _translate_error_response raises for a given body shape. Read
    # tests/engines/test_diffusers_set_lora_stack.py for the exact
    # contract and mirror its assertion here.
    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _http_error(400, body={"error": "unknown_lora", "lora_id": "x"})

    backend = _make_backend(http_post)
    # Replace with the actual typed exception raised by
    # _translate_error_response for {"error": "unknown_lora"}:
    with pytest.raises(Exception):  # narrow this when wired up
        backend.set_lora_stack(loras=[])


def test_lora_recovers_from_tls_reset() -> None:
    attempts = {"n": 0}

    def http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return {"ok": True}

    backend = _make_backend(http_post)
    backend.set_lora_stack(loras=[])
    assert attempts["n"] == 2
```

**Implementation note:** Before running these tests, read `tests/engines/test_diffusers_set_lora_stack.py` and `src/kinoforge/engines/diffusers/__init__.py` `_translate_error_response` (around line 534) to (1) get the correct `set_lora_stack` call signature and (2) replace the placeholder exception type in `test_lora_non_transient_routes_to_translate_error_response`.

- [ ] **Step 3: Write artifact-retry tests**

Create `tests/engines/test_diffusers_artifact_retry.py`:

```python
"""Tests for the engine-layer artifact-download retry wrap."""

from __future__ import annotations

import urllib.error

import pytest

# NOTE: The artifact download wrap is at the DiffusersEngine.run layer,
# not the DiffusersBackend. The test pattern below exercises
# retry_proxy_call against a stub _http_get_bytes seam — adjust to the
# actual engine fixture once the engine's seam is identified.

from kinoforge.engines._proxy_retry import (
    RUNPOD_PROXY_POLICY,
    retry_proxy_call,
)


def test_artifact_recovers_from_tls_reset() -> None:
    """Catches: artifact download not wrapped; full compute lost on TLS reset.

    Highest blast radius — re-running re-burns the entire generation.
    """
    attempts = {"n": 0}

    def fn() -> bytes:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return b"VIDEO_BYTES"

    result = retry_proxy_call(
        "diffusers.artifact", "http://pod/x.mp4", fn,
        lambda _: None,
        RUNPOD_PROXY_POLICY,
    )
    assert result == b"VIDEO_BYTES"
    assert attempts["n"] == 2


def test_artifact_exhaustion_propagates_url_error() -> None:
    def fn() -> bytes:
        raise urllib.error.URLError("dns failed")

    with pytest.raises(urllib.error.URLError):
        retry_proxy_call(
            "diffusers.artifact", "http://pod/x.mp4", fn,
            lambda _: None,
            RUNPOD_PROXY_POLICY,
        )


def test_artifact_non_transient_http_raises_immediately() -> None:
    attempts = {"n": 0}

    def fn() -> bytes:
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            url="http://x", code=404,  # 404 IS transient — use 410 instead
            msg="x", hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    # 404 is in the transient set (RunPod startup race). Use 410 for a
    # genuinely non-transient code:
    def fn_410() -> bytes:
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            url="http://x", code=410, msg="x",
            hdrs=None, fp=None,  # type: ignore[arg-type]
        )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call(
            "diffusers.artifact", "http://pod/x.mp4", fn_410,
            lambda _: None,
            RUNPOD_PROXY_POLICY,
        )
    assert exc_info.value.code == 410
    assert attempts["n"] == 1
```

- [ ] **Step 4: Run all three new test files**

Run: `pixi run pytest tests/engines/test_diffusers_submit_retry.py tests/engines/test_diffusers_lora_retry.py tests/engines/test_diffusers_artifact_retry.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/engines/test_diffusers_submit_retry.py \
        tests/engines/test_diffusers_lora_retry.py \
        tests/engines/test_diffusers_artifact_retry.py
pixi run pre-commit run --files \
    tests/engines/test_diffusers_submit_retry.py \
    tests/engines/test_diffusers_lora_retry.py \
    tests/engines/test_diffusers_artifact_retry.py
git add pixi.lock 2>/dev/null || true
git commit -m "test(diffusers): cover submit/lora/artifact retry wraps

Three new test files. submit/artifact: happy-recover, exhaustion,
non-transient-bypass, TLS-reset recovery. lora: same shape plus a
test asserting non-transient HTTPError bodies still route to
_translate_error_response (semantic-error contract preserved)."
```

---

## Task 24: Migrate comfyui to shared retry helper

**Goal:** Comfyui consumes `engines._proxy_retry`; latent URLError bug in `result()` closed.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py`

**Acceptance Criteria:**
- [ ] `_PROXY_TRANSIENT_CODES`, `_SUBMIT_RETRY_BACKOFFS`, `_retry_proxy_call` deleted
- [ ] `_interpoll_wait` closure inside `result()` deleted; call sites use module-level `interpoll_wait` from `_proxy_retry`
- [ ] Three call sites (submit.upload, submit.prompt, result.history) pass `policy=RUNPOD_PROXY_POLICY` explicitly
- [ ] `result()` exception block adds `except RUNPOD_PROXY_POLICY.catch_classes` branch alongside existing `HTTPError` branch (records last_transient, continues polling)
- [ ] All existing comfyui tests pass (Task 25 covers the behavior-change updates)
- [ ] mypy + ruff clean

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v -k "retry or result or upload"` → all PASS after Task 25 updates land. For this task standalone: `pixi run mypy src/kinoforge/engines/comfyui/__init__.py` clean.

**Steps:**

- [ ] **Step 1: Delete dead constants and helper**

In `src/kinoforge/engines/comfyui/__init__.py`:

1. Delete lines 86-93 (the `_PROXY_TRANSIENT_CODES` and `_SUBMIT_RETRY_BACKOFFS` block). Read surrounding context first to be sure no other module references them.
2. Delete the `_retry_proxy_call` function (lines 520-577).
3. Delete the local `_interpoll_wait` closure inside `result()` (lines ~850-854).
4. If comfyui defines its own `_NULL_CANCEL_TOKEN`, delete it; otherwise leave alone (the existing `kinoforge.core.cancel._NULL_TOKEN` is the canonical one).

- [ ] **Step 2: Add module-level imports**

Near the existing imports at the top of `comfyui/__init__.py`, add:

```python
from kinoforge.engines._proxy_retry import (
    RUNPOD_PROXY_POLICY,
    interpoll_wait,
    retry_proxy_call,
)
```

- [ ] **Step 3: Update three call sites to pass policy**

At each of the three sites (`comfyui/__init__.py:746` submit.upload, `:793` submit.prompt, `:891` result.history), add `RUNPOD_PROXY_POLICY` as the fifth positional arg or as `policy=RUNPOD_PROXY_POLICY`. Example for submit.prompt:

```python
        response = retry_proxy_call(
            "comfyui.submit.prompt",
            url,
            lambda: self._http_post(url, {"prompt": graph}),
            self._sleep,
            RUNPOD_PROXY_POLICY,
        )
```

- [ ] **Step 4: Replace `_interpoll_wait` closure calls with module-level helper**

At each call site (`comfyui/__init__.py:914`, `:997`), replace:

```python
                    if _interpoll_wait(self._poll_interval_s):
                        token.raise_if_set()
```

with:

```python
                    if interpoll_wait(
                        self._poll_interval_s, cancel_token, self._sleep
                    ):
                        token.raise_if_set()
```

Note the third arg is `self._sleep` — module-level `interpoll_wait` requires explicit sleep injection (the closure captured it implicitly).

- [ ] **Step 5: Add catch_classes branch in `result()`**

In `comfyui/__init__.py` `result()`, immediately after the existing `except urllib.error.HTTPError as exc:` block (line ~897-916), add a sibling `except RUNPOD_PROXY_POLICY.catch_classes` block. The existing block looks roughly like:

```python
            except urllib.error.HTTPError as exc:
                if exc.code in _PROXY_TRANSIENT_CODES:  # OLD constant
                    _log.warning(...)
                    last_transient = exc
                    poll_idx += 1
                    if _interpoll_wait(self._poll_interval_s):
                        token.raise_if_set()
                    continue
                _log.warning(...)
                raise
```

After updates to use `RUNPOD_PROXY_POLICY.transient_codes` and module-level `interpoll_wait`, add:

```python
            except RUNPOD_PROXY_POLICY.catch_classes as exc:
                _log.warning(
                    "[comfyui.result] transient transport-error exhausted "
                    "elapsed=%.1fs job=%s type=%s",
                    elapsed,
                    job_id,
                    type(exc).__name__,
                )
                last_transient = exc
                poll_idx += 1
                if interpoll_wait(
                    self._poll_interval_s, cancel_token, self._sleep
                ):
                    token.raise_if_set()
                continue
```

Type-note: `last_transient` was typed `urllib.error.HTTPError | None` in comfyui; widen to `BaseException | None`. Search for the annotation (line ~862) and update.

- [ ] **Step 6: Update outer submit.upload URLError handler (line ~752)**

Today's outer block immediately raises `AssetFetchError` on URLError/OSError. After this change, the helper inside retries first; the outer block now fires only on EXHAUSTED transport errors. The block stays as-is — same exception type, same outer behavior. Verify the block still reads:

```python
            except (urllib.error.URLError, OSError) as e:
                raise AssetFetchError(
                    f"ComfyUI /upload/image failed for role {role!r}: {e}"
                ) from e
```

No code change here — but flag for the test in Task 25 that this block's *trigger conditions* have shifted (now fires only after retry exhaustion).

- [ ] **Step 7: Lint + typecheck**

Run: `pixi run ruff check src/kinoforge/engines/comfyui/__init__.py && pixi run mypy src/kinoforge/engines/comfyui/__init__.py`
Expected: clean.

- [ ] **Step 8: Run existing comfyui suite**

Run: `pixi run pytest tests/engines/test_comfyui.py -x -v`
Expected: most tests pass. `test_result_raises_after_persistent_404` (and any URLError-immediate-raise test for submit.upload) will FAIL — those are the behavior changes covered by Task 25. Do not amend tests in this commit.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/engines/comfyui/__init__.py
pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py
git add pixi.lock 2>/dev/null || true
git commit -m "refactor(comfyui): consume shared retry_proxy_call + interpoll_wait

Deletes _PROXY_TRANSIENT_CODES, _SUBMIT_RETRY_BACKOFFS,
_retry_proxy_call, and the _interpoll_wait closure inside result().
Imports the same primitives from engines/_proxy_retry so diffusers
and comfyui share one helper. Adds an except RUNPOD_PROXY_POLICY
.catch_classes branch in result() to absorb URLError/OSError the
same way transient HTTP codes are absorbed — closes the latent bug
identical to the one fixed in diffusers.

Existing tests covering immediate URLError-raise behavior at
submit.upload will fail; updates land in the test-update commit."
```

---

## Task 25: Update comfyui tests for shared helper + new URLError absorption

**Goal:** Comfyui test surface tracks the migration; new URLError-in-result test added; signature updates applied.

**Files:**
- Modify: `tests/engines/test_comfyui.py`
- Create: `tests/engines/test_comfyui_result_url_error.py`

**Acceptance Criteria:**
- [ ] Existing `test_result_raises_after_persistent_404` still passes (the helper exhaustion behavior is unchanged — same code, same backoffs, same final-exception-raise)
- [ ] Existing URLError-immediate-raise test (if any) at submit.upload updated to assert "retries N times first, then AssetFetchError"
- [ ] New `test_comfyui_result_absorbs_url_error`: simulates TLS reset on `/history`; assert poll continues, doesn't crash
- [ ] Full comfyui suite passes

**Verify:** `pixi run pytest tests/engines/test_comfyui.py tests/engines/test_comfyui_result_url_error.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Read the existing submit.upload URLError test**

Run: `pixi run pytest tests/engines/test_comfyui.py -k "url_error or asset_fetch" --collect-only -q`

Read each matching test to identify which assert "URLError raised immediately" vs which assert "after retries". Update the former.

- [ ] **Step 2: Update test_result_raises_after_persistent_404 if signature changed**

If the test calls `_retry_proxy_call` directly (it shouldn't — but verify), update to `retry_proxy_call(..., RUNPOD_PROXY_POLICY)`. If it tests behavior through `backend.result()`, no change needed — the public surface is identical.

- [ ] **Step 3: Update submit.upload URLError test**

Find the test (likely named `test_submit_upload_url_error_raises_asset_fetch_error` or similar). Update its mock to verify the http_post-multipart spy is called MORE THAN ONCE (retries occurred) before the AssetFetchError raises. Example pattern:

```python
def test_submit_upload_url_error_raises_asset_fetch_error_after_retries(
    # ... existing fixtures
) -> None:
    attempts = {"n": 0}

    def upload(*args, **kwargs):
        attempts["n"] += 1
        raise urllib.error.URLError("conn reset")

    # ... existing setup using `upload` as the multipart-post seam ...

    with pytest.raises(AssetFetchError):
        backend.submit(job)

    # Helper retries 7 times (1 + 6 backoffs) before AssetFetchError raises.
    assert attempts["n"] == 7
```

- [ ] **Step 4: Add test_comfyui_result_url_error.py**

Create `tests/engines/test_comfyui_result_url_error.py`:

```python
"""Cover the new URLError absorption in ComfyUIBackend.result()."""

from __future__ import annotations

import urllib.error
from typing import Any

import pytest

# NOTE: ComfyUIBackend construction in tests follows the existing
# test_comfyui.py fixtures. Import or reuse the existing helper that
# builds a backend with injected http_get / http_post / sleep. If
# there is no shared fixture, adapt the inline _make_backend pattern
# from test_diffusers_result_loop.py.


def test_comfyui_result_absorbs_url_error(make_comfyui_backend) -> None:
    """Catches: comfyui result() crashes on TLS reset (the latent bug).

    Diffusers had the same bug; this test ensures comfyui's
    result() also continues polling through transport errors.
    """
    calls = {"n": 0}

    def http_get(url: str) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        # Return a "done" history-shape entry. The exact shape depends
        # on how the existing comfyui test_result_* tests structure it
        # — mirror the simplest passing case.
        return {
            "job_id": {
                "status": {"completed": True},
                "outputs": {},
            }
        }

    backend = make_comfyui_backend(http_get=http_get)
    # The backend.result call shape mirrors existing tests:
    backend.result("job_id")
    assert calls["n"] >= 3
```

**Implementation note:** `make_comfyui_backend` may not be a real fixture — adapt to whatever pattern `test_comfyui.py` uses to construct a backend with injected http_get. If there is no fixture, inline the construction as in `test_diffusers_result_loop.py::_make_backend`.

- [ ] **Step 5: Run the comfyui suite**

Run: `pixi run pytest tests/engines/test_comfyui.py tests/engines/test_comfyui_result_url_error.py -x -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/engines/test_comfyui.py tests/engines/test_comfyui_result_url_error.py
pixi run pre-commit run --files \
    tests/engines/test_comfyui.py \
    tests/engines/test_comfyui_result_url_error.py
git add pixi.lock 2>/dev/null || true
git commit -m "test(comfyui): cover shared helper + new URLError absorption

Updates the submit.upload URLError test to assert retries happen
before AssetFetchError raises (behavior change documented in the
spec). Adds test_comfyui_result_url_error.py for the latent-bug
closure: result() now absorbs URLError/OSError the same way it
absorbs transient HTTP codes."
```

---

## Task 26: Live-pod validation against warm pod q37c8bzlkppk4u

**Goal:** Confirm the fix path executes against real RunPod proxy hiccups, not just mocked ones.

**Files:** None (operational validation).

**Acceptance Criteria:**
- [ ] Job submits + polls + downloads artifact without TLS-reset crash
- [ ] If `[diffusers.result] transient transport-error` WARNING lines appear in the log, retry behavior is confirmed end-to-end against real proxy resets
- [ ] An MP4 lands under `outputs/`

**Verify:** Successful MP4 output written; no `URLError` propagation.

**Steps:**

- [ ] **Step 1: Check pod state**

Run RunPod CLI or kinoforge inventory to see if `q37c8bzlkppk4u` is still warm:

```bash
pixi run kinoforge instances list
```

Expected: pod listed with status RUNNING. If destroyed, provision a fresh pod via the same config:

```bash
pixi run kinoforge instances provision \
    --config examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml
```

- [ ] **Step 2: Rerun the exact failed command**

Per memory `feedback_standard_test_prompt`, use the canonical prompt file:

```bash
(
    eval "$(pixi shell-hook)"
    python -m kinoforge generate \
        --config examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml \
        --mode t2v \
        --prompt "$(cat /workspace/examples/configs/prompts/field-realistic.txt)"
)
```

Expected: job submits, polls, downloads MP4, returns 0.

- [ ] **Step 3: Inspect logs for retry signal**

Tail the run log and grep for retry traces:

```bash
grep -E "\[diffusers\.(submit|result|lora|artifact)\] transient" kinoforge.log
```

If any line appears, retry path is exercised against the real proxy — this is the strongest evidence the fix works end-to-end.

- [ ] **Step 4: Spot-check the artifact**

```bash
ls -lah outputs/*.mp4
ffprobe outputs/<latest>.mp4 2>&1 | head -20
```

Expected: valid MP4 with the configured resolution + duration.

- [ ] **Step 5: Smoke validation summary**

Report (no commit — this task produces no code):

- Pod status pre-run
- Wall-clock duration of the generate call
- Number of retry WARNINGs (per call site)
- Output MP4 path and size

If pre-validation runtime cost is under the $20 session budget (memory `feedback_autonomous_no_gates`), proceed without additional authorization. If the pod ran longer than expected or any retry warning suggests sustained instability, capture details and surface to the user.

- [ ] **Step 6: Optional teardown**

Per memory `feedback_never_destroy_without_explicit_authorization` (superseded by 2026-06-20 override): destroy the pod once work is finished and no concrete in-session follow-up needs the warm state:

```bash
pixi run kinoforge instances destroy q37c8bzlkppk4u
```

Skip if planning a follow-up experiment that needs warmth.

---

## Self-Review

**Spec coverage:**
- _proxy_retry.py module → Task 18 ✓
- RetryPolicy + RUNPOD_PROXY_POLICY → Task 18 ✓
- retry_proxy_call generic helper → Task 18 ✓
- interpoll_wait module-level → Task 18 ✓
- Helper tests → Task 19 ✓
- Diffusers result() restructure → Task 20 ✓
- Diffusers wall-clock timeout + cancel_token → Task 20 ✓
- Diffusers submit/lora/artifact wraps → Task 22 ✓
- Result-loop tests → Task 21 ✓
- Site-wrap tests → Task 23 ✓
- Comfyui migration → Task 24 ✓
- Comfyui URLError catch in result() → Task 24 ✓
- Comfyui submit.upload behavior change → Task 25 ✓
- Live validation → Task 26 ✓

**Placeholder scan:** Searched for "TBD"/"TODO"/"see existing"/"fill in" — none. Tasks 23 and 25 contain explicit READ-FIRST notes pointing at concrete existing test files (`test_diffusers_set_lora_stack.py`, `test_comfyui.py`) where the executor must verify call signatures before finalizing their tests. These are not placeholders — they are read-then-mirror instructions with the source file named.

**Type consistency:**
- `retry_proxy_call[T](label, url, fn, sleep, policy)` — same signature across Tasks 18, 20, 22
- `interpoll_wait(seconds, cancel_token, sleep) -> bool` — same signature across Tasks 18, 20, 24
- `RUNPOD_PROXY_POLICY.transient_codes` / `.backoffs` / `.catch_classes` / `.label_prefix` — same field names everywhere
- `self._poll_timeout_s`, `self._poll_interval_s` — consistent in Task 20
- `_NULL_TOKEN` from `kinoforge.core.cancel` — used consistently (refinement noted in header)
- `last_transient: BaseException | None` — widened from comfyui's `HTTPError | None` in Task 24 Step 5 explicitly

**No user-gate tasks tagged.** Memory `feedback_autonomous_no_gates` is explicit: skip "reply when done" handshakes; live smokes pre-authorized up to $20. Tasks #26's verification language ("confirm the fix path executes") matches Verbs bucket only; no Scope/Nouns/Proof co-occurrence, so the trigger rule does not fire.

---

## Tasks (native, already created during brainstorming)

#18-#26 exist with the dependency graph:

```
#18 _proxy_retry.py module
 ├── #19 helper tests
 ├── #20 diffusers result() restructure
 │    ├── #21 result-loop tests
 │    └── #22 diffusers wrap submit/lora/artifact
 │         └── #23 submit/lora/artifact tests
 └── #24 comfyui migration
      └── #25 comfyui tests update
           ┐
#21, #23, #25 ── #26 live-pod validation
```

This plan's Task N sections correspond directly to native task #N. The native tasks will be updated with the full **Goal / Files / Acceptance Criteria / Verify / Steps** description block from this plan, plus the `json:metadata` fence.
