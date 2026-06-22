# Proxy-Retry Shared Helper Design

**Date:** 2026-06-21
**Status:** Approved (design)
**Authors:** Dr. Twinklebrane + Claude (brainstorming)

## Problem

`python -m kinoforge generate --config runpod-diffusers-wan-t2v-14b-2_2.yaml` crashed
mid-poll with `ConnectionResetError: [Errno 104]` during the SSL handshake against
the RunPod HTTPS proxy. The crash bubbled all the way up to `SystemExit` from
`kinoforge/engines/diffusers/__init__.py:419-420`, where `result()` calls bare
`self._http_get(url)` inside the poll loop with no retry. One transient
TLS reset = full job loss after submission already succeeded.

Investigation (this session) confirmed:

1. **Diffusers has zero retry.** `result()` calls `self._http_get(url)` directly
   inside `for _ in range(_MAX_POLL)`. Comfyui's Phase 47 `_retry_proxy_call` was
   never ported to diffusers.
2. **Comfyui's `_retry_proxy_call` catches only `urllib.error.HTTPError`** (status-
   code-bearing). `ConnectionResetError` surfaces as bare `URLError` with no
   `.code` — would fall through unhandled. Comfyui's `result()` loop
   (`comfyui/__init__.py:897`) has the identical latent bug.
3. **Four diffusers call sites are vulnerable** to the same proxy reset:
   - `submit()` POST `/generate` (line 387)
   - `result()` GET `/status/{id}` (line 420 — the crash)
   - `set_lora_stack()` POST `/lora/set_stack` (line 515)
   - artifact download GET `/artifacts/{filename}` (engine layer, line 1113)
4. **Hosted/fal engines have the same poll-loop shape** but a different fault
   domain (vendor APIs, per-call billing). Out of scope here; opt-in later.

## Goal

Eliminate the proxy-reset class of failure across both RunPod-proxy-fronted
engines (diffusers + comfyui) with a shared, policy-parameterized retry helper.
Close diffusers' pre-existing `cancel_token`-ignored debt at the same time, so
adding retry does not deepen the unresponsive Ctrl-C window.

## Non-goals

- Wiring hosted/fal engines through the new helper (separate fault domain,
  different retry economics).
- Generalizing the entire result-loop pattern across engines. Comfyui's
  status-parsing is materially more complex (queue_pos, exec_node, history-
  shape) and resists shared abstraction. The shared piece is the retry call;
  the loop itself stays per-engine.
- Changing the Phase 47 backoff schedule. `(1, 2, 4, 8, 16, 16)` ships as the
  default for the shared `RUNPOD_PROXY_POLICY`.

## Architecture

New module: `src/kinoforge/engines/_proxy_retry.py`. Sibling of the engine
subpackages. Contains:

- `RetryPolicy` — frozen dataclass capturing one retry strategy
  (`transient_codes`, `backoffs`, `catch_classes`, `label_prefix`).
- `RUNPOD_PROXY_POLICY` — module-level constant for the diffusers+comfyui
  fault domain. Both engines share this constant by reference.
- `retry_proxy_call[T](label, url, fn, sleep, policy) -> T` — generic in T;
  works for `dict` (JSON), `bytes` (artifact), `str` (multipart upload).
- `_NULL_CANCEL_TOKEN` — module-level no-op cancel token (moved from comfyui).
- `interpoll_wait(seconds, cancel_token, sleep) -> bool` — cancel-aware sleep.
  **Lifted from comfyui's closure (`comfyui/__init__.py:850`) to a real module-
  level helper** with explicit `(seconds, cancel_token, sleep)` signature, so
  both engines share one implementation. Returns True if the cancel token
  fired during the wait. When `cancel_token is None`, falls back to the
  injected `sleep` (preserves comfyui's legacy-test contract where `sleep=lambda s: None`
  keeps the loop instant).

Existing comfyui constants and helper deleted; comfyui imports the new ones.
Both engines reference the shared policy by name, so any future tuning lands
in one place.

## RetryPolicy + retry_proxy_call API

```python
@dataclass(frozen=True)
class RetryPolicy:
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


def retry_proxy_call[T](
    label: str,
    url: str,
    fn: Callable[[], T],
    sleep: Callable[[float], None],
    policy: RetryPolicy = RUNPOD_PROXY_POLICY,
) -> T:
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
                policy.label_prefix, label, url, exc.code,
                attempt_idx + 1, attempts,
            )
            last_exc = exc
        except policy.catch_classes as exc:
            _log.warning(
                "[%s.%s] transient transport-error url=%s type=%s reason=%s "
                "attempt=%d/%d",
                policy.label_prefix, label, url, type(exc).__name__,
                str(exc)[:200], attempt_idx + 1, attempts,
            )
            last_exc = exc
    if last_exc is None:  # pragma: no cover
        raise RuntimeError("retry_proxy_call exited loop without recording an error")
    raise last_exc
```

Key design choices:

- **HTTPError dispatch precedes URLError catch.** `HTTPError` is a subclass of
  `URLError`; the order ensures HTTP-code branch wins, and `policy.catch_classes`
  only fires for non-HTTP URLErrors (TLS resets, DNS, etc).
- **`(0.0,) + policy.backoffs`** — first attempt has no pre-sleep; subsequent
  attempts sleep per the schedule. 1 initial + len(backoffs) retries = 7 attempts
  for default policy.
- **`last_exc: BaseException | None`** widened from comfyui's `HTTPError | None`
  to also hold transport-class exceptions.
- **`policy` defaults to `RUNPOD_PROXY_POLICY`.** Future hosted/fal adoption MUST
  pass an explicit policy; module docstring + helper docstring flag this.

## Diffusers result() restructure

Rewrite `result()` (`diffusers/__init__.py:390-446`) with full comfyui parity:

- `while True` outer loop (replaces `for _ in range(_MAX_POLL)`)
- `token.raise_if_set()` at top of every iteration, before any I/O
- Outer try wraps `retry_proxy_call` and catches BOTH `HTTPError`
  (for exhausted transient codes) and `RUNPOD_PROXY_POLICY.catch_classes`
  (for exhausted transport errors); both record `last_transient` and continue
  polling within `poll_timeout_s`
- New `self._poll_timeout_s: float` instance state (default `1800.0` — matches
  today's effective bound); plumbed through `__init__` and `_make_backend`
- New `self._poll_interval_s: float` (default `1.0`)
- Inter-poll wait uses `interpoll_wait(self._poll_interval_s, token, self._sleep)`
  (cancel-aware module-level helper)
- On timeout, `last_transient` is preferred over bare `TimeoutError` if set, so
  operators see the underlying proxy failure
- `_MAX_POLL` retained as iteration-cap belt-and-braces (test fallback when
  sleep is stubbed)

Full implementation shape in Section 3 of the brainstorming transcript.

## Coverage — all four diffusers call sites

Every HTTP transit against the RunPod proxy gets wrapped:

1. **`submit()` POST /generate** — `retry_proxy_call("diffusers.submit", ...)`.
   Exhausted retry propagates; caller already surfaces as job failure.
2. **`result()` GET /status** — covered by the result-loop restructure above.
3. **`set_lora_stack()` POST /lora/set_stack** — wrapped with the helper, but
   the outer `except urllib.error.HTTPError` block must preserve the existing
   `_translate_error_response` path for non-transient 4xx bodies (semantic
   error contract).
4. **Artifact download GET /artifacts/{filename}** — wrapped at the engine
   layer (not the backend ABC) via `_http_get_bytes`. Highest blast radius
   (full compute already spent); coverage non-negotiable.

Log labels are call-site-specific (`"diffusers.submit"`, `"diffusers.result"`,
`"diffusers.lora.set_stack"`, `"diffusers.artifact"`) so log scraping
disambiguates which site retried.

## Comfyui migration

Delete: `_PROXY_TRANSIENT_CODES`, `_SUBMIT_RETRY_BACKOFFS`, `_retry_proxy_call`,
the `_interpoll_wait` closure inside `result()` (`comfyui/__init__.py:850-854`),
and the `_NULL_CANCEL_TOKEN` module-level constant. Replace closure call sites
(`comfyui/__init__.py:914, 997`) with `interpoll_wait(self._poll_interval_s,
cancel_token, self._sleep)`. Both engines import the new helpers from
`engines._proxy_retry`.

Update three call sites (`submit.upload`, `submit.prompt`, `result.history`) to
import the new helper and pass `RUNPOD_PROXY_POLICY` explicitly.

**Comfyui `result()` exception block update**: add `except
RUNPOD_PROXY_POLICY.catch_classes` branch alongside the existing `HTTPError`
branch, mirroring the diffusers loop. Closes the identical latent bug.

**Behavior change at comfyui submit.upload**: today's outer `except
(URLError, OSError)` catches transport errors immediately and raises
`AssetFetchError`. After this change, those errors retry inside the helper
first; persistent failure after backoff exhaustion still raises
`AssetFetchError`. Net: transport errors get N retries before giving up.

## Testing

Test-design skill compliance: every test states behavior under test + the
concrete bug it catches. No mock-only mirrors of implementation; no happy-path-
only coverage; strong assertions on exact sleep schedules.

### `tests/engines/test_proxy_retry.py`

- `test_retries_transient_http_code` — 502 retries to exhaustion
- `test_raises_non_transient_http_immediately` — 500 raises on first attempt
- `test_retries_url_error` — ConnectionResetError in URLError retries
- `test_retries_os_error` — bare OSError retries
- `test_exhausted_retry_reraises_last` — final exception is what raises
- `test_success_on_third_attempt_returns_value` — sleep stops on success
- `test_sleep_called_with_exact_schedule` — `[1.0, 2.0]` after two retries
- `test_http_error_dispatch_precedes_url_error` — except-order regression guard
- `test_policy_is_frozen` — FrozenInstanceError on mutation
- `test_runpod_policy_constant_values` — exact Phase 47 calibration preserved

### `tests/engines/diffusers/test_result_loop.py`

- `test_cancel_token_raises_before_first_io` — `del cancel_token` regression
- `test_cancel_token_raises_during_interpoll_wait` — token-aware sleep
- `test_wall_clock_timeout_fires` — `poll_timeout_s` bound
- `test_max_poll_belt_and_braces` — iteration-cap fallback for sleep-stubbed tests
- `test_last_transient_preferred_over_timeout` — proxy-diagnostic preserved
- `test_tls_reset_absorbed_then_done` — the exact crash this fix targets
- `test_status_done_builds_url_from_base` — local-URL regression guard
- `test_status_error_raises_generation_error` — server error path

### `tests/engines/diffusers/test_{submit,lora,artifact}_retry.py`

Each: happy-then-transient-recover + exhaustion + non-transient-bypass.
Lora additionally asserts `_translate_error_response` still fires on
non-transient HTTPError bodies.

### Comfyui regression

All existing `_retry_proxy_call` tests updated to pass `policy=RUNPOD_PROXY_POLICY`
explicitly. New `test_comfyui_result_absorbs_url_error`. Existing
`test_submit_upload_url_error_raises_asset_fetch_error` updated to assert
N retries before `AssetFetchError` (behavior change).

### Test fixtures

`FAST_POLICY = dataclasses.replace(RUNPOD_PROXY_POLICY, backoffs=(0.0, 0.0, 0.0))`
in `tests/engines/conftest.py`. Reused across all retry-aware tests.

## Risks and side effects

| Risk | Severity | Mitigation |
|------|----------|------------|
| Wall-clock timeout misaligns with a legitimately long generation | Low | Default 1800.0 mirrors today's effective bound; configurable via cfg if a model needs more |
| TLS reset retried 47s = wasted wall-clock | Low | Within poll_timeout_s envelope; future split-policy if observed wasteful |
| Comfyui signature change breaks existing tests | Medium | All sites updated atomically in same PR |
| Comfyui submit.upload behavior change (delayed AssetFetchError) | Medium | Documented; outer exception type preserved |
| HTTPError-before-URLError except ordering bug | Medium | Explicit test guards |
| Default policy arg lets future callers silently get RunPod policy | Low | Module + helper docstrings flag this; hosted/fal MUST pass explicit policy |
| Cancel-token check ordering — late cancel triggers one extra I/O | Low | Comfyui pattern: token check at TOP of iteration; test enforces |

**No new dependencies.** Pure stdlib.

**Pod state.** Crashed run's pod `q37c8bzlkppk4u` is still warm. Recommended
pre-merge validation: rerun the exact failed command against the same warm pod
to confirm the retry path executes against real proxy hiccups, not just
mocked ones.

## Out of scope

- Hosted (replicate/runway/luma) engine retry — separate fault domain, billed
  per call; opt in later with `HOSTED_API_POLICY`.
- Fal engine retry — same rationale.
- Shared poll-loop extraction across engines — premature (rule of three;
  comfyui's status-parsing differs materially from diffusers').
- Jittered backoffs — kinoforge doesn't run many parallel calls; jitter has
  no foundation value today.
- Metrics / retry-counter accumulation — future RetryRunner class layer on
  top of the dataclass; non-breaking when needed.
