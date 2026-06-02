# Layer P — Task 7 item #1: orchestrator offer-retry on capacity errors — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fragile `offers[0]` + `ValueError` string-match with typed `CapacityError` raised from `RunPodProvider.create_instance` and a private `_create_with_offer_retry` helper consumed by both orchestrator call sites (`deploy()` and `_provision_instance_and_build_backend`). Smoke loop swaps to typed catch.

**Architecture:** Provider raises typed `CapacityError` only when the mutation error message contains `"resources to deploy"` (case-insensitive); all other mutation errors keep generic `ValueError`. Orchestrator iterates offers in `find_offers`-returned order, catching only `CapacityError`. On exhaustion, raises `CapacityError("all N offers exhausted; ...")` with the last per-offer exception as `__cause__`.

**Tech Stack:** stdlib (no new deps). pydantic v2 (existing). pytest (existing). pixi (existing).

**Spec:** `docs/superpowers/specs/2026-06-01-layer-p-task7-item1-offer-retry-design.md`.

**Branch:** `build/layer-p` (off `main@7788f93`). Three atomic commits, no merge in scope (parent Layer P merges all together at Task 10).

**Spec correction:** The spec's "deploy_session.__enter__" call site actually lives in the shared helper `_provision_instance_and_build_backend` (orchestrator.py:224–297). Same code path, just one level deeper. This plan locks the correct symbol. Behavior contract unchanged.

---

## File structure

| File | Role |
|---|---|
| `src/kinoforge/providers/runpod/__init__.py` | Capacity-error detection in `create_instance` (~10 LOC) |
| `src/kinoforge/core/orchestrator.py` | `_create_with_offer_retry` helper + 2 call-site rewires (~40 LOC) |
| `tests/providers/test_runpod.py` | +3 regression tests for typed CapacityError |
| `tests/core/test_orchestrator.py` | +5 regression tests + `_OfferRetryProvider` fake |
| `tests/live/test_comfyui_wan_live.py` | Smoke retrofit: `except ValueError + sniff` → `except CapacityError` |

No new files. No ABC changes. No dependency / `.env` / CI / example-YAML changes.

---

## Task 1: Provider typed CapacityError on no-resources mutation

**Goal:** `RunPodProvider.create_instance` raises `CapacityError` (chained from `ValueError`) when the GraphQL mutation `errors` block contains the substring `"resources to deploy"` (case-insensitive). All other mutation errors retain generic `ValueError`.

**Files:**
- Modify: `src/kinoforge/providers/runpod/__init__.py:34` (imports), `:502-507` (capacity-error branch)
- Modify: `tests/providers/test_runpod.py` (+3 tests)

**Acceptance Criteria:**
- [ ] AC1 — `pytest.raises(CapacityError)` triggers when mutation error message contains `"resources to deploy"`.
- [ ] AC2 — `pytest.raises(CapacityError)` also triggers on `"RESOURCES TO DEPLOY"` (case-insensitive).
- [ ] AC3 — `CapacityError.__cause__` is the underlying `ValueError`; `str(exc.__cause__)` contains the original RunPod error message verbatim.
- [ ] AC4 — Non-capacity mutation errors (e.g., `"template not found"`) still raise `ValueError`, never `CapacityError`.
- [ ] AC5 — `CapacityError` message contains the offer's `gpu_type` for operator debugging.

**Verify:** `pixi run pytest tests/providers/test_runpod.py -v -k "capacity_error or non_capacity"`

**Steps:**

- [ ] **Step 1.1: Write the 4 failing tests in `tests/providers/test_runpod.py`**

Add the four tests adjacent to the existing `test_create_pod_*` block (around line 319+ in the module). They reuse the existing `HttpPostSpy` / `pod_spec` fixture / `_make_creds()` infrastructure documented above the new block.

```python
import dataclasses

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import Offer


_CAPACITY_OFFER = Offer(
    id="rtx-4090",
    gpu_type="NVIDIA GeForce RTX 4090",
    vram_gb=24,
    cuda="12.0",
    cost_rate_usd_per_hr=0.69,
    mode="pod",
)


def _spec_with_offer(pod_spec: InstanceSpec) -> InstanceSpec:
    """pod_spec fixture has no offer; attach _CAPACITY_OFFER for these tests."""
    return dataclasses.replace(pod_spec, offer=_CAPACITY_OFFER)


def test_create_instance_raises_capacity_error_on_no_resources(
    pod_spec: InstanceSpec,
) -> None:
    """RunPod mutation error containing 'resources to deploy' → typed CapacityError.

    Bug catch: if provider re-raises ValueError instead of CapacityError,
    orchestrator-side retry catches nothing and the original PROGRESS:182
    failure shape returns.
    """
    err_msg = "This machine does not have the resources to deploy your pod"
    http_post = HttpPostSpy(response={"errors": [{"message": err_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError) as exc_info:
        provider.create_instance(spec)

    # AC5: message names the offer's gpu_type so operators can debug
    assert _CAPACITY_OFFER.gpu_type in str(exc_info.value)


def test_create_instance_capacity_error_case_insensitive(
    pod_spec: InstanceSpec,
) -> None:
    """AC2: substring match is case-insensitive.

    Bug catch: a future RunPod copy edit (e.g., 'RESOURCES TO DEPLOY')
    silently turns into a ValueError if the match is case-sensitive,
    re-introducing PROGRESS:182.
    """
    err_msg = "MACHINE DOES NOT HAVE THE RESOURCES TO DEPLOY YOUR POD"
    http_post = HttpPostSpy(response={"errors": [{"message": err_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError):
        provider.create_instance(spec)


def test_create_instance_capacity_error_chains_underlying_value_error(
    pod_spec: InstanceSpec,
) -> None:
    """CapacityError.__cause__ preserves original RunPod ValueError.

    Bug catch: dropping `from value_error` (or `from None`) loses the
    raw RunPod message across the orchestrator boundary, blinding
    operators to the actual capacity reason.
    """
    raw_msg = "This machine does not have the resources to deploy your pod"
    http_post = HttpPostSpy(response={"errors": [{"message": raw_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError) as exc_info:
        provider.create_instance(spec)

    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert raw_msg in str(cause)


def test_create_instance_non_capacity_error_still_raises_value_error(
    pod_spec: InstanceSpec,
) -> None:
    """Auth / template / malformed-body errors keep raising ValueError.

    Bug catch: an over-eager match (e.g., regex on the whole errors
    block, or unconditional CapacityError on any mutation error) would
    silently turn auth failures into retry-eligible capacity errors,
    causing the orchestrator to retry across every offer for a problem
    that fails identically on each.
    """
    http_post = HttpPostSpy(response={"errors": [{"message": "template not found"}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(ValueError) as exc_info:
        provider.create_instance(spec)

    # Explicit: must NOT be CapacityError or any subclass
    assert not isinstance(exc_info.value, CapacityError)
```

Add `import dataclasses` to the test file's import block if not already present, and ensure `from kinoforge.core.errors import CapacityError` and `from kinoforge.core.interfaces import Offer` are imported. The four new tests use `HttpPostSpy` (line 67 of the existing file), `pod_spec` fixture (line 141), and `_make_creds()` helper (line 53) — all pre-existing.

Note this is **4 tests, not 3** — the case-insensitivity AC was promoted into its own discriminating test rather than folded into the first. Updates spec AC count: AC1 + AC2 (case-insensitivity) + AC3 (chain) + AC4 (non-capacity passthrough) + AC5 (gpu_type in message).

- [ ] **Step 1.2: Run the 3 new tests and confirm RED**

Run: `pixi run pytest tests/providers/test_runpod.py -v -k "capacity_error or non_capacity"`
Expected: All 3 FAIL. The first two fail because `create_instance` raises `ValueError`, not `CapacityError`. The third probably already passes (existing behavior); keep it — it's a regression lock.

- [ ] **Step 1.3: Modify `src/kinoforge/providers/runpod/__init__.py`**

Imports — replace line 34:

```python
from kinoforge.core.errors import CapacityError, TeardownError
```

In `RunPodProvider.create_instance`, find the existing pod-mode `errors` branch (around lines 502–507):

```python
        resp = self._http_post(self._base_url, body)
        if "errors" in resp:
            error_msgs = [str(e.get("message", e)) for e in resp.get("errors", [])]
            raise ValueError(
                "RunPod create-pod mutation returned errors:\n"
                + "\n".join(f"  - {m}" for m in error_msgs)
            )
```

Replace with:

```python
        resp = self._http_post(self._base_url, body)
        if "errors" in resp:
            error_msgs = [str(e.get("message", e)) for e in resp.get("errors", [])]
            assembled = "RunPod create-pod mutation returned errors:\n" + "\n".join(
                f"  - {m}" for m in error_msgs
            )
            value_error = ValueError(assembled)
            joined_lower = "\n".join(error_msgs).lower()
            if "resources to deploy" in joined_lower:
                raise CapacityError(
                    f"RunPod has no current capacity for "
                    f"{gpu_type_id!r}: {assembled}"
                ) from value_error
            raise value_error
```

Use the existing local `gpu_type_id` variable (line 479) — handles `spec.offer is None` correctly (falls back to `""`).

Leave the serverless `errors` branch (around lines 555–560) UNCHANGED — serverless capacity model is different (concurrency caps, not host availability), so generic `ValueError` is correct there.

Leave the null-pod-id branch (around line 514) UNCHANGED — `"returned no pod id"` is a malformed-response signal, not a capacity signal.

- [ ] **Step 1.4: Run the 3 new tests and confirm GREEN**

Run: `pixi run pytest tests/providers/test_runpod.py -v -k "capacity_error or non_capacity"`
Expected: All 3 PASS.

- [ ] **Step 1.5: Run the full provider test file to confirm no regression**

Run: `pixi run pytest tests/providers/test_runpod.py -v`
Expected: All tests pass. If any pre-existing test broke (e.g., one that asserted `ValueError` on a `"resources to deploy"` shape), update it to assert `CapacityError` — that's a contract upgrade, not a regression.

- [ ] **Step 1.6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod.py
git add src/kinoforge/providers/runpod/__init__.py tests/providers/test_runpod.py
git commit -m "$(cat <<'EOF'
feat(providers/runpod): typed CapacityError on no-resources mutation

Detect the RunPod 'This machine does not have the resources to deploy
your pod' mutation error (case-insensitive substring) in
RunPodProvider.create_instance and raise CapacityError chained from the
original ValueError. All other mutation errors (auth, template, etc.)
continue to raise generic ValueError.

Surfaces a typed signal for the orchestrator-side offer-retry helper
landing in the next commit. Aligns with Layer N's 'typed shapes, no
string sniffing' philosophy.

Spec: docs/superpowers/specs/2026-06-01-layer-p-task7-item1-offer-retry-design.md
Layer P Task 7 item #1 of 3 atomic commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/providers/runpod/__init__.py", "tests/providers/test_runpod.py"], "verifyCommand": "pixi run pytest tests/providers/test_runpod.py -v -k 'capacity_error or non_capacity'", "acceptanceCriteria": ["RunPodProvider.create_instance raises CapacityError when mutation error message contains 'resources to deploy' (case-insensitive)", "non-capacity mutation errors still raise ValueError", "CapacityError.__cause__ is the underlying ValueError preserving original RunPod message", "CapacityError message names the offer's gpu_type"]}
```

---

## Task 2: Orchestrator offer-retry helper + two call-site rewires

**Goal:** Add private `_create_with_offer_retry` helper in `orchestrator.py`; wire it into both `deploy()` (line ~636) and `_provision_instance_and_build_backend` (line ~283). On `CapacityError`, iterate offers in input order; on exhaustion, raise `CapacityError` with chained `__cause__`. Non-`CapacityError` exceptions propagate immediately (no retry).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (add `Callable` + `Offer` imports, new helper, 2 rewires)
- Modify: `tests/core/test_orchestrator.py` (+5 tests + `_OfferRetryProvider` fake)

**Acceptance Criteria:**
- [ ] AC1 — `deploy()` retries `create_instance` across all offers on `CapacityError`, returning the first success.
- [ ] AC2 — `deploy()` iterates offers in exact `find_offers`-returned input order (discriminating).
- [ ] AC3 — `deploy()` raises `CapacityError` with message `"all N offers exhausted"` when every offer raises `CapacityError`; `__cause__` is the last per-offer `CapacityError`.
- [ ] AC4 — `deploy()` does NOT retry on non-`CapacityError` exceptions; exactly 1 `create_instance` call before propagation.
- [ ] AC5 — `_provision_instance_and_build_backend` (used by `deploy_session`) exhibits the same retry behavior.

**Verify:** `pixi run pytest tests/core/test_orchestrator.py -v -k "retries_next_offer or iterates_offers or offers_exhausted or non_capacity or provision_instance_helper"`

**Steps:**

- [ ] **Step 2.1: Write the 5 failing tests in `tests/core/test_orchestrator.py`**

Add the `_OfferRetryProvider` fake class plus 5 tests near the existing `test_deploy_destroys_pod_when_get_instance_raises` (line ~1192) so deploy regression tests cluster together.

```python
from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import Offer


class _OfferRetryProvider(LocalProvider):
    """Fake provider scripted per-call to test offer-retry mechanics.

    Configured with a list of offers from find_offers() and a parallel
    list of outcomes:
        "capacity" -> raise CapacityError(...) on create_instance
        "value"    -> raise ValueError("non-capacity") on create_instance
        "ok"       -> return a real Instance with id derived from the offer

    Records every (offer, outcome) pair so tests can assert iteration
    order and call count.
    """

    def __init__(self, offers: list[Offer], outcomes: list[str]) -> None:
        super().__init__()
        if len(offers) != len(outcomes):
            raise AssertionError("offers and outcomes must be same length")
        self._scripted_offers = offers
        self._outcomes = outcomes
        self._index = 0
        self.calls: list[Offer] = []
        # Track CapacityError exceptions so identity-check can verify __cause__
        self.last_capacity_excs: list[CapacityError] = []

    def find_offers(self, requirements):  # type: ignore[override, no-untyped-def]
        return list(self._scripted_offers)

    def create_instance(self, spec: InstanceSpec) -> Instance:
        idx = self._index
        self._index += 1
        self.calls.append(spec.offer)
        outcome = self._outcomes[idx]
        if outcome == "capacity":
            exc = CapacityError(
                f"RunPod has no current capacity for {spec.offer.gpu_type!r}"
            )
            self.last_capacity_excs.append(exc)
            raise exc
        if outcome == "value":
            raise ValueError("non-capacity error from provider")
        if outcome == "ok":
            return Instance(
                id=f"pod-{spec.offer.id}",
                provider="local",
                status="ready",  # skip the get_instance poll
                created_at=0.0,
                tags=dict(spec.tags),
            )
        raise AssertionError(f"unknown outcome {outcome!r}")


def _three_offers() -> list[Offer]:
    """Three distinct offers ordered by gpu_preference (already sorted)."""
    return [
        Offer(
            id=f"offer-{i}",
            gpu_type=f"GPU_{i}",
            vram_gb=24,
            cuda="12.0",
            cost_rate_usd_per_hr=0.10 * (i + 1),
            mode="pod",
        )
        for i in range(3)
    ]


def test_deploy_retries_next_offer_on_capacity_error() -> None:
    """deploy() walks past the first CapacityError and uses offer[1].

    Bug catch: if _create_with_offer_retry isn't wired into deploy(),
    deploy crashes on the first CapacityError exactly as PROGRESS:182
    describes. The chosen-instance id assertion locks the off-by-one
    case where the helper returns offers[0]'s spec but advances past it.
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    result = deploy(cfg, provider=provider, engine=engine)

    assert result.instance is not None
    assert result.instance.id == "pod-offer-1"
    assert [o.id for o in provider.calls] == ["offer-0", "offer-1"]


def test_deploy_iterates_offers_in_input_order() -> None:
    """deploy() walks offers in exact find_offers-returned order.

    Bug catch: a future change that uses set() / reversed() / random
    iteration silently breaks the cost-aware sort done by filter_offers.
    Cheapest available offer would no longer be tried first.
    """
    offers = _three_offers()
    # offer[3] would succeed if it existed; here we exhaust 3 to keep
    # the assertion focused on iteration order only
    provider = _OfferRetryProvider(offers, ["capacity", "capacity", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    deploy(cfg, provider=provider, engine=engine)

    assert [o.id for o in provider.calls] == ["offer-0", "offer-1", "offer-2"]


def test_deploy_raises_capacity_error_when_all_offers_exhausted() -> None:
    """Every offer raises CapacityError → final exc is CapacityError with chain.

    Bug catch: raising ValueError, KinoforgeError, or a fresh
    CapacityError without __cause__ blinds the operator to the last
    real RunPod message. Identity check on __cause__ catches misuse
    of `raise X from None` (or no `from` at all, which falls through to
    the in-handler implicit chaining and wraps the wrong exception).
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "capacity", "capacity"])
    cfg = _compute_cfg()
    engine = _make_engine()

    with pytest.raises(CapacityError) as exc_info:
        deploy(cfg, provider=provider, engine=engine)

    assert "3 offers exhausted" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, CapacityError)
    # Identity: the chained cause IS the last per-offer exception
    assert exc_info.value.__cause__ is provider.last_capacity_excs[-1]


def test_deploy_does_not_retry_on_non_capacity_error() -> None:
    """Non-CapacityError exceptions propagate after exactly 1 create call.

    Bug catch: a too-broad `except Exception:` in the retry helper
    would silently retry auth / config errors across every offer,
    burning time and obscuring the real failure.
    """
    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["value", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()

    with pytest.raises(ValueError, match="non-capacity"):
        deploy(cfg, provider=provider, engine=engine)

    assert len(provider.calls) == 1, (
        f"non-CapacityError must propagate immediately; "
        f"got {len(provider.calls)} create_instance calls"
    )


def test_provision_instance_helper_retries_next_offer_on_capacity_error(
    tmp_path: Path,
) -> None:
    """The deploy_session compute helper retries identically to deploy().

    Tests `_provision_instance_and_build_backend` directly because it
    is the actual site of the second `offers[0]` (PROGRESS:182 only
    flagged deploy()'s site at line 626; this one at line 283 was
    silently sharing the same bug). Tested at the helper level rather
    than through `with deploy_session(...)` to avoid pulling in
    provisioner / profile-cache machinery unrelated to offer-retry.

    Bug catch: forgetting to rewire _provision_instance_and_build_backend
    leaves generate() and batch_generate() broken on the same capacity
    blip — a silent regression with no observable difference in deploy()
    tests.
    """
    from kinoforge.core.orchestrator import _provision_instance_and_build_backend

    offers = _three_offers()
    provider = _OfferRetryProvider(offers, ["capacity", "ok", "ok"])
    cfg = _compute_cfg()
    engine = _make_engine()
    store = LocalArtifactStore(tmp_path)

    # Patch the in-helper provisioner to a no-op so the test focuses on
    # the offer-retry mechanism rather than weights / profile cache I/O.
    with patch(
        "kinoforge.core.orchestrator._provision_compute_once",
        return_value=None,
    ):
        instance, _backend = _provision_instance_and_build_backend(
            resolved_engine=engine,
            resolved_provider=provider,
            cfg=cfg,
            run_id="t",
            key=cfg.capability_key(),
            creds=None,
            store=store,
            state_dir=tmp_path,
            for_discovery=False,
        )

    assert instance.id == "pod-offer-1"
    assert [o.id for o in provider.calls] == ["offer-0", "offer-1"]
```

If `Offer`'s required fields differ from the constructor above, mirror whatever `_compute_cfg()` / existing tests pass when constructing test offers. Inspect any existing `Offer(...)` literal in `tests/core/` and copy the field set verbatim.

- [ ] **Step 2.2: Run the 5 new tests and confirm RED**

Run: `pixi run pytest tests/core/test_orchestrator.py -v -k "offer_retry or exhausted or non_capacity or deploy_session_retries"`
Expected: All 5 FAIL. `_create_with_offer_retry` does not exist yet; current `deploy()` and `_provision_instance_and_build_backend` both pick `offers[0]` unconditionally.

- [ ] **Step 2.3: Modify `src/kinoforge/core/orchestrator.py` — imports**

Replace line 20:

```python
from collections.abc import Callable, Iterator
```

In the interfaces import block (lines 34–46), add `Offer` to the alphabetically-sorted list:

```python
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ComputeProvider,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationRequest,
    Instance,
    InstanceSpec,
    ModelProfile,
    ModelProfileProvider,
    Offer,
)
```

- [ ] **Step 2.4: Add the `_create_with_offer_retry` helper to `orchestrator.py`**

Insert immediately above `_provision_instance_and_build_backend` (currently line ~224). Exact body:

```python
def _create_with_offer_retry(
    provider: ComputeProvider,
    build_spec: Callable[[Offer], InstanceSpec],
    offers: list[Offer],
) -> tuple[Instance, Offer]:
    """Iterate offers until create_instance succeeds.

    The first offer is tried first (the list is already sorted by
    filter_offers' gpu_preference). On CapacityError, continue to the
    next offer. Any other exception propagates immediately — non-
    capacity errors fail every offer identically.

    Args:
        provider: The resolved compute provider.
        build_spec: Closure that builds an InstanceSpec for one offer.
            Called once per offer attempted.
        offers: Non-empty list of offers in attempt order.

    Returns:
        (instance, offer) — the first offer for which create_instance
        succeeded, paired with the live instance.

    Raises:
        CapacityError: Every offer raised CapacityError. The last
            per-offer CapacityError is chained as __cause__.
    """
    last_capacity_exc: CapacityError | None = None
    for offer in offers:
        spec = build_spec(offer)
        try:
            instance = provider.create_instance(spec)
            return instance, offer
        except CapacityError as exc:
            last_capacity_exc = exc
            _log.warning(
                "[offer-retry] %s @ $%.4f/hr unavailable: %s",
                offer.gpu_type,
                offer.cost_rate_usd_per_hr,
                exc,
            )
            continue
    raise CapacityError(
        f"all {len(offers)} offers exhausted; provider "
        f"{getattr(provider, 'name', repr(provider))!r} "
        f"has no current capacity"
    ) from last_capacity_exc
```

- [ ] **Step 2.5: Rewire `_provision_instance_and_build_backend`**

Find the existing `spec = InstanceSpec(...)` block at lines ~272–282 followed by `instance = resolved_provider.create_instance(spec)` at line 283. Replace those 12 lines with:

```python
    def _build_spec(offer: Offer) -> InstanceSpec:
        return InstanceSpec(
            image=image,
            offer=offer,
            lifecycle=lifecycle,
            tags={
                "kinoforge_engine": resolved_engine.name,
                "kinoforge_key": key_hash,
            },
            env={},
            run_id=run_id,
        )

    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )
```

Everything after (the `while instance.status != "ready":` poll loop, the `_provision_compute_once` call, the backend construction, and the return) stays unchanged.

- [ ] **Step 2.6: Rewire `deploy()`**

Find the existing `spec = InstanceSpec(...)` block at lines ~624–634 followed by `instance = resolved_provider.create_instance(spec)` at line 636. Replace those 13 lines with:

```python
    def _build_spec(offer: Offer) -> InstanceSpec:
        return InstanceSpec(
            image=image,
            offer=offer,
            lifecycle=lifecycle,
            tags={
                "kinoforge_engine": resolved_engine.name,
                "kinoforge_key": key_hash,
            },
            env={},
            run_id="",
        )

    instance, _chosen_offer = _create_with_offer_retry(
        resolved_provider, _build_spec, offers
    )
```

Everything after (the `try / except BaseException` block with the Layer N destroy-on-error wrap and the poll-to-ready logic) stays unchanged.

- [ ] **Step 2.7: Run the 5 new tests and confirm GREEN**

Run: `pixi run pytest tests/core/test_orchestrator.py -v -k "offer_retry or exhausted or non_capacity or deploy_session_retries"`
Expected: All 5 PASS.

- [ ] **Step 2.8: Run the full orchestrator + provider test files to confirm no regression**

Run: `pixi run pytest tests/core/test_orchestrator.py tests/providers/test_runpod.py -v`
Expected: All tests pass (including the existing `test_deploy_destroys_pod_when_get_instance_raises` regression).

- [ ] **Step 2.9: Run typecheck + lint to catch unused imports / type mismatches**

Run:
```bash
pixi run typecheck
pixi run lint
```
Expected: No new errors. Common pitfalls to fix inline if they surface:
- `Offer` imported but unused → only happens if the helper signature is wrong; verify the helper.
- `Callable` unused → check that step 2.3's import-line edit is in.
- mypy complains about `Callable[[Offer], InstanceSpec]` → confirm `Offer` is in the interfaces module (it is, at line 39).

- [ ] **Step 2.10: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(core/orchestrator): offer-retry across deploy + deploy_session

Add private _create_with_offer_retry helper and wire it into both
compute-deploy call sites: deploy() and _provision_instance_and_build_backend
(the shared compute helper used by deploy_session, which fronts generate()
and batch_generate()).

Iterates offers in find_offers-returned order (already sorted by
filter_offers' gpu_preference). On CapacityError, continue to the next
offer. Any other exception propagates immediately. On exhaustion, raises
CapacityError("all N offers exhausted; ...") with the last per-offer
CapacityError chained as __cause__.

Closes the production-code half of PROGRESS:182 (the smoke loop
landed at 4a673d7; this is the orchestrator equivalent).

Spec: docs/superpowers/specs/2026-06-01-layer-p-task7-item1-offer-retry-design.md
Layer P Task 7 item #2 of 3 atomic commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/orchestrator.py", "tests/core/test_orchestrator.py"], "verifyCommand": "pixi run pytest tests/core/test_orchestrator.py tests/providers/test_runpod.py -v", "acceptanceCriteria": ["deploy() retries create_instance on CapacityError across all offers in input order", "deploy() does NOT retry on non-CapacityError exceptions (exactly one create call)", "deploy() raises CapacityError with 'all N offers exhausted' message and last per-offer CapacityError as __cause__", "_provision_instance_and_build_backend (deploy_session helper) exhibits identical retry behavior", "no regression on existing deploy / orchestrator tests"]}
```

---

## Task 3: Smoke retrofit — typed CapacityError catch

**Goal:** Replace the live smoke's fragile `except ValueError as exc: if "resources to deploy" in str(exc):` pattern with `except CapacityError:`. Keep the surrounding `for candidate in offers:` loop intact (smoke still calls `provider.create_instance` directly, not through `deploy_session`).

**Files:**
- Modify: `tests/live/test_comfyui_wan_live.py` (replace the string-sniff catch)

**Acceptance Criteria:**
- [ ] AC1 — Smoke catches `CapacityError` instead of `ValueError + substring`.
- [ ] AC2 — Smoke control flow unchanged: still iterates `offers` in order, still calls `pytest.fail` on exhaustion.
- [ ] AC3 — `pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v` reports the test as collected-and-skipped without live env vars (module-gating preserved).
- [ ] AC4 — `pixi run lint` + `pixi run typecheck` clean.

**Verify:** `pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v`

**Steps:**

- [ ] **Step 3.1: Locate the existing catch block in the smoke**

The catch is in `test_runpod_comfyui_wan_live_e2e_smoke`, in the `[phase=create_instance]` section. Current shape (from commit `4a673d7`, lines ~226–238):

```python
                try:
                    instance = provider.create_instance(ispec)
                    chosen = candidate
                    _log.info(
                        "[phase=create_instance] %s @ $%.4f/hr -> %s",
                        candidate.gpu_type,
                        candidate.cost_rate_usd_per_hr,
                        instance.id,
                    )
                    break
                except ValueError as exc:
                    if "resources to deploy" in str(exc):
                        _log.warning(
                            "[phase=create_instance] %s unavailable, trying next",
                            candidate.gpu_type,
                        )
                        continue
                    raise
```

- [ ] **Step 3.2: Replace with typed catch**

```python
                try:
                    instance = provider.create_instance(ispec)
                    chosen = candidate
                    _log.info(
                        "[phase=create_instance] %s @ $%.4f/hr -> %s",
                        candidate.gpu_type,
                        candidate.cost_rate_usd_per_hr,
                        instance.id,
                    )
                    break
                except CapacityError:
                    _log.warning(
                        "[phase=create_instance] %s unavailable, trying next",
                        candidate.gpu_type,
                    )
                    continue
```

Verify `from kinoforge.core.errors import CapacityError` exists in the imports block at the top of the file. If absent (the smoke previously only relied on `ValueError`), add it:

```python
from kinoforge.core.errors import CapacityError
```

Alphabetize it correctly within the existing `from kinoforge.core.errors import ...` line if one exists; otherwise add a new import line in the kinoforge-imports group.

- [ ] **Step 3.3: Verify collection + skip-without-env preserved**

Run: `pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v`
Expected: 1 item collected, marked as skipped (live env vars not set in this environment). No syntax errors.

- [ ] **Step 3.4: Run lint + typecheck**

Run:
```bash
pixi run lint
pixi run typecheck
```
Expected: clean.

- [ ] **Step 3.5: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/live/test_comfyui_wan_live.py
git add tests/live/test_comfyui_wan_live.py
git commit -m "$(cat <<'EOF'
refactor(test/live): swap ValueError sniff to typed CapacityError

The Layer P Task 7 live smoke (4a673d7) caught the RunPod
no-current-capacity case via `except ValueError as exc:
if "resources to deploy" in str(exc):`. RunPodProvider.create_instance
now raises typed CapacityError on that path (previous commit), so the
string sniff is redundant and fragile.

Replace with `except CapacityError:`. Control flow unchanged.

The surrounding `for candidate in offers:` loop stays because the smoke
calls provider.create_instance directly (not through deploy_session).
It deletes naturally once Layer P Task 7 item #2 lands the `instance=`
kwarg on deploy_session and the smoke drives orchestrator-side create.

Spec: docs/superpowers/specs/2026-06-01-layer-p-task7-item1-offer-retry-design.md
Layer P Task 7 item #3 of 3 atomic commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["tests/live/test_comfyui_wan_live.py"], "verifyCommand": "pixi run pytest tests/live/test_comfyui_wan_live.py --collect-only -v", "acceptanceCriteria": ["smoke catches CapacityError instead of ValueError + substring sniff", "smoke control flow unchanged (still iterates offers, still calls pytest.fail on exhaustion)", "collection green + skip-without-env preserved", "lint + typecheck clean"]}
```

---

## Final offline gate (run after Task 3)

Spec AC9–AC10. Single gate, no commit; just confirms the full suite is green before handing back to Layer P Task 7's remaining work (items #2, #3).

```bash
pixi run test
pixi run typecheck
pixi run lint
pixi run pre-commit run --all-files
```

Expected:
- `pixi run test`: full suite passes; count rises from ~836 → ~845 (+9 net new tests: 4 in Task 1 + 5 in Task 2).
- `pixi run typecheck`: clean.
- `pixi run lint`: clean.
- `pixi run pre-commit run --all-files`: clean.

No commit at this step. If anything fails: fix inline (the gate is the goal, not a separate commit boundary).

---

## Spec coverage verification

| Spec AC | Task | Step |
|---|---|---|
| AC1 (CapacityError on "resources to deploy") | Task 1 | 1.1 (test 1) / 1.3 |
| AC1b (case-insensitive match) | Task 1 | 1.1 (test 2) / 1.3 |
| AC2 (other errors keep ValueError) | Task 1 | 1.1 (test 4) / 1.3 |
| AC3 (__cause__ chain) | Task 1 | 1.1 (test 3) / 1.3 |
| AC4 (deploy retries) | Task 2 | 2.1 (test 1) / 2.6 |
| AC5 (no retry on non-Capacity) | Task 2 | 2.1 (test 4) / 2.4 |
| AC6 (exhaustion CapacityError with chain) | Task 2 | 2.1 (test 3) / 2.4 |
| AC7 (deploy_session helper parity) | Task 2 | 2.1 (test 5) / 2.5 |
| AC8 (smoke retrofit) | Task 3 | 3.1 / 3.2 |
| AC9 (full suite ~845) | Final gate | post-Task-3 |
| AC10 (typecheck/lint/pre-commit clean) | Final gate | post-Task-3 |

Every spec AC has a task + step. No gaps.
