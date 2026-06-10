# Graceful interrupt + ComfyUI poll observability — design

**Status:** validated 2026-06-10 in brainstorming session.
**Triggered by:** `kinoforge --ephemeral generate ... --mode t2v` (Wan 14B t2v on RunPod) hung silently after `provisioner.provision` returned; required two `Ctrl-C` presses to escape; second `Ctrl-C` left `provider.destroy_instance` unrun. Pod survived only because the in-pod Layer Q self-terminator eventually killed it.
**Scope:** orchestrator interrupt handling, `ConcurrentPool` close semantics, `ComfyUIBackend` poll observability, CLI signal handler. Single-PR-sized.

---

## 1. Problem

Three orthogonal defects surface in one user-visible failure:

1. **Silent stall.** Between `provisioner.provision` returning and the operator's first `Ctrl-C`, ~30 s elapsed with no log line. `ComfyUIBackend.result` polls `/history/{id}` on a 2 s cadence but emits no per-tick log; the operator cannot tell whether the stall is in the RunPod proxy, the ComfyUI worker, or a graph node.
2. **Two-`Ctrl-C` requirement.** `deploy_session.__exit__` calls `pool.close()`, which calls `executor.shutdown(wait=True)` on every per-backend `ThreadPoolExecutor`. The worker thread is parked inside `backend.result`'s poll loop, which has no cancellation mechanism, so `shutdown(wait=True)` blocks indefinitely. The first `Ctrl-C` escapes `pool.submit(...).result()`; the second escapes `shutdown`.
3. **No teardown on interrupt.** `orchestrator.generate`'s stage loop wraps `for stage in stages: state = stage.run(state)` in `except ValidationError:`. `KeyboardInterrupt` (a `BaseException`, not an `Exception`) and any other non-`ValidationError` exit class propagate without entering the except clause, so the explicit `destroy_instance` path runs only on validation failures. The operator gets no log line about the surviving pod and no guidance to run `kinoforge reap`.

The asymmetry — `ValidationError` destroys, every other failure mode leaks — is the headline defect. The two-`Ctrl-C` UX and the silent-stall observability gap are the proximate aggravations.

A separate observation that constrains the design: the Layer 5b spec change in commit `3bc6473` ("Layer 5b session manager no longer tears down compute on exit") is intentional. The success path keeps the pod alive to enable in-process warm-reuse (the only warm-reuse path that works today; cross-call warm-reuse remains unbuilt — PROGRESS B3/B4). The interrupt path inherits the same intent: leave the pod for warm-reuse, signal the operator clearly that the pod is alive, and let the in-pod self-terminator + `kinoforge reap` handle eventual cleanup.

## 2. Goals

- Single `Ctrl-C` returns the shell prompt within a bounded interval (≤ 30 s).
- Second `Ctrl-C` force-exits with a clear `WARN` naming the surviving pod ID and the `kinoforge reap` recovery command.
- Every ComfyUI poll tick emits a structured log line carrying enough state (`elapsed`, `last_status`, `queue_pos`, `exec_node`) that a future stall self-diagnoses without re-running the smoke.
- Hard upper bound on ComfyUI poll wait time, `engine.comfyui.poll_timeout_s` (default 600 s), raises `TimeoutError` with `last_status` + `exec_node` in the message.
- No silent leaks. Every interrupt path that leaves a pod alive emits exactly one `WARN` line naming the pod ID and the recovery command.
- All new tests offline. No live smoke required to land this layer; the next live `kinoforge generate` becomes the live regression test for free.

## 3. Non-goals

- Auto-destroying the pod on interrupt. The user confirmed in brainstorming that warm-reuse intent applies in both `--ephemeral` and non-ephemeral modes; interrupt does not change that.
- Threading the `ephemeral` flag through `generate()` / `deploy_session()`. The teardown matrix collapses to "`ValidationError` destroys, every other exit class keeps the pod" — `ephemeral` is orthogonal.
- Root-causing the specific stall observed on 2026-06-10. The per-tick logging + hard timeout shipped here will self-diagnose the next reproduction. The pod is already gone (self-terminated); pod-side logs are not retrievable.
- Cross-process warm-reuse (PROGRESS B3 / B4). Out of scope.
- Changing the success-path teardown rule (commit `3bc6473`'s spec is preserved).
- Cancellation for the Diffusers / Hosted-API / Bedrock / Replicate / Runway backends beyond what falls out of the shared `RemoteSubmitPollBackend` ABC change. Per-backend hardening is a follow-up if those engines see a similar stall.

## 4. Architecture

### 4.1 `CancelToken` (new — `src/kinoforge/core/cancel.py`)

Thin `threading.Event` wrapper. Keeps the interface narrow so backends never grab the underlying event:

```python
class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to *timeout* seconds. Returns True if set, False on timeout."""
        return self._event.wait(timeout)

    def raise_if_set(self) -> None:
        """Raise :class:`Cancelled` if the token has been set."""
        if self._event.is_set():
            raise Cancelled("cancellation requested by operator")
```

A module-level `_NULL_TOKEN` sentinel (a `CancelToken` instance that is never set) is the default for every kwarg below. Library / test callers that pass no token get unchanged behavior.

### 4.2 `Cancelled` error (`core/errors.py`)

```python
class Cancelled(KinoforgeError):
    """Raised by a backend when a `CancelToken` is set mid-operation."""
```

`Cancelled` is a regular `Exception`, not a `BaseException`. The orchestrator's stage-loop except clause catches it explicitly alongside `KeyboardInterrupt`; tests that monkey-patch backends to raise `Cancelled` exercise the same teardown path as a real `Ctrl-C`.

### 4.3 Plumbing

| layer | change |
|---|---|
| `cli/_main.py` | install `SIGINT` handler at `main()` start; build a `CancelToken`; pass it via `kinoforge.cli.SessionContext.cancel_token`. |
| `cli/_commands.py::_cmd_generate` (+ `_cmd_batch`) | thread `cancel_token=ctx.cancel_token` into the `generate` call. |
| `core/orchestrator.py::generate` | new kwarg `cancel_token: CancelToken | None = None`; defaulted to `_NULL_TOKEN`. Pass to `deploy_session`, `KeyframeStage`, and `GenerateClipStage`. |
| `core/orchestrator.py::deploy_session` | new kwarg `cancel_token`; pass to pool construction (so pool can hand it to backends via `submit`). In the `finally`, compute `cancel_pending = cancel_token.is_set()` and pass to `pool.close(cancel_pending=..., timeout=30.0)`. |
| `core/pool.py::ConcurrentPool.submit` | new kwarg `cancel_token=None`; forwarded to `_run_one`; `_run_one` forwards to `backend.submit` and `backend.result`. |
| `core/pool.py::ConcurrentPool.close` | new kwargs `cancel_pending: bool = False, timeout: float | None = None`. When `cancel_pending=True`, call `executor.shutdown(wait=True, cancel_futures=True)` per slot. `timeout` bounds the wait — implemented by submitting `executor.shutdown` to a transient watchdog thread and joining with `timeout`; on expiry, log `WARN "worker still running after %.1fs; abandoning slot"` and continue. |
| `core/pool.py::SequentialPool.submit` | new kwarg `cancel_token=None`; forwarded to backend; pool itself does no waiting. |
| `pipeline/generate_clip.py::GenerateClipStage` | new ctor kwarg `cancel_token=None`; forwarded into `self.pool.submit(job, cancel_token=...)`. |
| `engines/comfyui/__init__.py::ComfyUIBackend.submit / .result` | new kwarg `cancel_token=None`; poll loop calls `cancel_token.raise_if_set()` before each `http_get` AND uses `cancel_token.wait(self.poll_interval_s)` instead of `time.sleep` so cancellation is observed during the inter-poll sleep too. |
| `core/remote_backend.py::RemoteSubmitPollBackend` (Replicate / Runway / Luma / Fal base) | identical signature change; inherited by all four concretes. |

### 4.4 CLI signal handler

Installed once at CLI entry, before any subcommand dispatch:

```python
def _install_sigint_handler(token: CancelToken) -> None:
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

The handler is installed only on `_cmd_generate` / `_cmd_batch` dispatch (not on read-only subcommands like `list` / `status`). Library callers of `generate()` never see signal mutation.

### 4.5 Orchestrator stage-loop except clauses

Both stage-loop sites — the keyframe block around `orchestrator.py:1085` and the main loop around line 1173 — get a new `(KeyboardInterrupt, Cancelled)` except arm that **logs + re-raises but does not destroy**:

```python
try:
    for stage in stages:
        state = stage.run(state)
except ValidationError:
    _log.warning("spec validation failed; tearing down instance before re-raising")
    if (session.instance is not None
            and session.provider is not None
            and not _caller_supplied_instance):
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

Order matters: `ValidationError` is `Exception`, `KeyboardInterrupt` is `BaseException`. The `ValidationError` arm runs first so a typo-driven validation failure still tears down the pod (existing behavior, untouched). The `(KeyboardInterrupt, Cancelled)` arm catches everything else that signals user cancellation.

Note the explicit `<hosted>` fallback for the hosted-engine path (`session.instance is None`).

### 4.6 ComfyUI poll observability + hard timeout

`ComfyUIBackend.result` poll loop becomes:

```python
def result(
    self,
    job_id: str,
    *,
    cancel_token: CancelToken | None = None,
) -> Artifact:
    token = cancel_token or _NULL_TOKEN
    start = time.monotonic()
    last_status = "unknown"
    while True:
        token.raise_if_set()
        elapsed = time.monotonic() - start
        if elapsed > self.poll_timeout_s:
            raise TimeoutError(
                f"comfyui poll timed out after {elapsed:.1f}s "
                f"(job={job_id}, last_status={last_status!r}, "
                f"exec_node={exec_node!r})"
            )
        envelope = self._retry_proxy_call(
            lambda: self.http_get(f"/history/{job_id}")
        )
        last_status, queue_pos, exec_node = _extract_poll_fields(envelope)
        _log.info(
            "comfyui poll job=%s elapsed=%.1fs status=%s queue_pos=%s exec_node=%s",
            job_id, elapsed, last_status, queue_pos, exec_node,
        )
        outputs = _try_extract_outputs(envelope)
        if outputs:
            return self._build_artifact(outputs)
        token.wait(self.poll_interval_s)  # interruptible sleep
```

`_extract_poll_fields` reads:
- `last_status` from `envelope.get("status", {}).get("status_str")` (ComfyUI returns `"queued"` / `"running"` / `"complete"` / `"error"`).
- `queue_pos` from a separate `GET /queue` call when `last_status == "queued"`. Cheap (one extra round-trip per tick during queue wait). When status is anything else, the field is `None`.
- `exec_node` from `envelope.get("status", {}).get("exec_info", {}).get("current_node")`. `None` outside the running window. Wan workflows expose `WanVideoSampler` here during the long step.

`poll_timeout_s` is a new field on `EngineConfig.comfyui` (defaults to `600.0`). Operators can lift it in YAML for known-slow models.

## 5. Surface changes

### 5.1 Public ABCs

`GenerationBackend.submit(self, spec, *, cancel_token=None) -> str` — kwarg added, default `None`. Backwards-compatible: existing concrete backends ignore the kwarg until they opt in.

`GenerationBackend.result(self, job_id, *, cancel_token=None) -> Artifact` — same.

`BackendPool.submit(self, job, *, cancel_token=None) -> Future[Artifact]` — same.

`BackendPool.close(self, *, cancel_pending=False, timeout=None) -> None` — `close()` callers without kwargs continue to work unchanged.

### 5.2 New public surface

- `kinoforge.core.cancel.CancelToken`
- `kinoforge.core.errors.Cancelled` (exported in `kinoforge.core.__init__`)

### 5.3 CLI surface

No new flags. `--ephemeral` semantics unchanged. The signal handler is invisible to operators except via the new `WARN` line on first `Ctrl-C`.

### 5.4 YAML surface

```yaml
engine:
  kind: comfyui
  comfyui:
    poll_interval_s: 2.0     # existing
    poll_timeout_s: 600.0    # NEW — hard upper bound; raises TimeoutError
```

Field is optional; absence preserves the 600 s default.

## 6. Test plan

All tests offline. Red-first per project `test-design` skill.

| # | file | bug-catch | red asserts |
|---|---|---|---|
| 1 | `tests/core/test_cancel.py` | `CancelToken.wait` returns `False` on timeout, `True` when set mid-wait; `raise_if_set` raises `Cancelled` only when set. | direct calls |
| 2 | `tests/core/test_pool_cancel.py` | `ConcurrentPool.close(cancel_pending=True, timeout=0.1)` returns within 0.5 s even when worker is parked in `time.sleep(60)`; emits `WARN "worker still running"`. | inject a backend whose `submit` sleeps; assert close-time |
| 3 | `tests/engines/test_comfyui_cancel.py` | `ComfyUIBackend.result` with a pre-set token raises `Cancelled` on first tick, does not call `http_get`. | inject a token; spy on `http_get` call count |
| 4 | `tests/engines/test_comfyui_timeout.py` | `poll_timeout_s=1.0` raises `TimeoutError` whose message contains `last_status` and `exec_node`. | inject a fake `http_get` that returns `status=running, exec_node=WanVideoSampler` forever |
| 5 | `tests/engines/test_comfyui_poll_log.py` | every poll tick emits the structured INFO line with all five fields; `queue_pos` is None when status != queued. | `caplog` |
| 6 | `tests/core/test_orchestrator_interrupt.py` | fake stage raises `KeyboardInterrupt`; `provider.destroy_instance` is NOT called; WARN log includes pod ID + `kinoforge reap` text. | spy on provider |
| 7 | `tests/core/test_orchestrator_cancelled.py` | same as #6 but stage raises `Cancelled`; same teardown / log assertions. | spy on provider |
| 8 | `tests/cli/test_sigint_handler.py` | `signal.raise_signal(SIGINT)` once → token set, no exception; raise again → `KeyboardInterrupt`; default handler restored after second signal. | install handler in a subtest; use `signal.raise_signal` |
| 9 | `tests/engines/test_remote_submit_poll_cancel.py` | `RemoteSubmitPollBackend.result` with a pre-set token raises `Cancelled`; covers Replicate / Runway / Luma transitively. | inject token on base ABC |

No new live smoke. The next operator-initiated `kinoforge generate` carries the new logging and the new timeout for free, and a future stall will self-diagnose.

## 7. Out of scope / carry-forwards

- **Diffusers / Hosted-API per-backend cancellation hardening.** Inherited at the ABC level; if those engines stall in production the same way ComfyUI did, the logs will tell us where, and a follow-up layer adds backend-specific tick logging.
- **Cross-process warm-reuse** (PROGRESS B3 / B4). Same selfterm/reap path applies; this design does not touch it.
- **`kinoforge reap --orphans` operator helper** that walks the RunPod REST API for pods not in the ledger. Useful when an interrupt leaves a pod with no ledger entry. Already mostly covered by Layer V `sweep` but ergonomics could improve.
- **Async / background teardown** (option B from brainstorming). Not picked; if synchronous teardown proves too slow in operator practice, revisit.
- **Auto-destroy on `--ephemeral` interrupt.** Explicitly rejected — warm-reuse intent applies in both modes.

## 8. Risks

- **CPython signal delivery during native code.** SIGINT is only delivered between bytecode instructions; a worker thread parked in a blocking `urllib.request.urlopen` will not be interrupted by the handler. Mitigation: the handler only sets a token; the worker checks the token between poll ticks (every `poll_interval_s` seconds). Worst-case latency on Ctrl-C is one poll interval (~2 s).
- **`executor.shutdown(wait=True, cancel_futures=True)` still blocks on running workers.** `cancel_futures=True` only cancels queued futures, not running ones. The timeout watchdog covers the worst case — after `timeout` seconds the slot is logged as abandoned and `close()` returns. The abandoned thread eventually exits when its current poll tick finishes and the token check raises `Cancelled`.
- **`token.wait(poll_interval_s)` semantic change.** Today the loop uses `time.sleep(poll_interval_s)`. `Event.wait` is a `Condition.wait`, which on POSIX is interruptible and on Linux uses `pthread_cond_timedwait`. Functionally equivalent to `time.sleep` for a never-set Event, but returns immediately when the token is set. Tested in #1.
- **WARN-only on interrupt leak.** Operators who ignore the WARN line may rack up RunPod cost until the in-pod self-terminator fires. Already an accepted trade-off per the success-path spec (`3bc6473`); the new WARN at least makes the situation legible.
- **`exec_node` field shape varies across ComfyUI versions / custom nodes.** `_extract_poll_fields` returns `None` on `KeyError`; the log line tolerates `None`. No hard dependency.

## 9. Files touched

- New: `src/kinoforge/core/cancel.py`
- Modified: `src/kinoforge/core/errors.py` (`Cancelled`)
- Modified: `src/kinoforge/core/__init__.py` (re-export `CancelToken`, `Cancelled`)
- Modified: `src/kinoforge/core/pool.py` (`submit` kwarg, `close` kwargs, watchdog)
- Modified: `src/kinoforge/core/orchestrator.py` (`generate` + `deploy_session` kwargs, stage-loop except arms at lines 1085 + 1173, `deploy_session.__exit__` finally block at line 749)
- Modified: `src/kinoforge/pipeline/generate_clip.py` (`cancel_token` ctor + forward)
- Modified: `src/kinoforge/engines/comfyui/__init__.py` (`submit` + `result` kwargs, poll loop, `_extract_poll_fields`)
- Modified: `src/kinoforge/core/remote_backend.py` (ABC kwarg pass-through)
- Modified: `src/kinoforge/core/config.py` (`EngineConfig.comfyui.poll_timeout_s` field)
- Modified: `src/kinoforge/cli/_main.py` (signal handler install)
- Modified: `src/kinoforge/cli/_commands.py` (`_cmd_generate` + `_cmd_batch` thread token)
- Modified: `src/kinoforge/cli/context.py` (`SessionContext.cancel_token` field)
- New tests per Section 6.
