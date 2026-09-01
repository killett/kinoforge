# Compute Seam S4 — Declarative Selection and a Verified Rate Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the hourly rate a number kinoforge *verifies against the launched instance* instead of
a filter it applies to a catalog it hopes is accurate, and move offer enumeration off the portable
seam into the two providers that actually have a marketplace.

**Architecture:** Stage 4 of 5 from
`docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §5 and §6. Two halves,
landing in that order. **Part A (Tasks 0–5)** adds `ComputeProvider.realized_rate()`, the
`RATE_READBACK` / `RATE_DETERMINISTIC` capability split, and a teardown-on-violation check between
`create_instance` and `engine.provision`; `Instance.cost_rate_usd_per_hr` starts coming from that
read. **Part B (Tasks 6–11)** takes `find_offers` off the ABC, moves the offer-retry loop inside
RunPod, deletes `HardwareRequirements` and `InstanceSpec.offer`, and gates `kinoforge offers` on a
new `CATALOG_ENUMERATION` capability. Part A is independently shippable and closes the money bug;
stopping after Task 5 leaves a green tree.

**Tech Stack:** Python 3.13, pydantic v2, pytest, pixi, ruff, mypy. Providers: runpod (GraphQL),
skypilot (SDK, pinned `skypilot-0.12.3.post1`), modal (SDK), local. Engines: diffusers, comfyui,
fake.

---

## TWO THINGS THE DESIGN GETS WRONG — READ THIS FIRST

Both were verified against HEAD (`2f062b75`) while writing this plan. Implementing §5/§6 literally
would ship a money regression and break the ratchet every prior stage was measured against.

### 1. Making `max_usd_per_hr` a *post-launch* check REMOVES protection RunPod has today

§5 item 6 says the cap "stops being a catalog filter and becomes the verified cap of §6", and §6's
Part 1 lists the fields that narrow the request — `accelerators`, `accelerator_count`, `region`,
`spot`, `clouds` — and does **not** include the cap.

But `core/offers.py:38` today reads:

```python
if o.mode == "pod" and o.cost_rate_usd_per_hr > reqs.max_usd_per_hr:
    continue
```

So on RunPod and Modal the cap is enforced **before anything is booked**, for free. Replacing that
with "book it, read the rate back, destroy if over" means kinoforge would start paying for boots it
currently never starts. That is strictly worse on exactly the provider where the current behaviour
is correct.

**This plan keeps both.** Where a catalog exists, the cap stays a pre-book filter (Task 7 carries
`filter_offers` into the providers unchanged). The readback is added *on top*, because it catches
what a filter cannot: SkyPilot's optimizer choosing a SKU nobody enumerated, and any drift between
a catalog price and the billed price. §6's own framing — "in two parts" — is right; its Part 1 list
is what is incomplete.

### 2. `spec.offer` is load-bearing for the golden ratchet AND for SkyPilot's CPU branch

Deleting `InstanceSpec.offer` (implied by §5: selection becomes the provider's business) hits two
places the design does not mention:

* **`tools/snapshot_launch_payloads.py` gets its determinism from offer injection.** `build_spec`
  passes a frozen synthetic `_catalog_offer(cfg)` precisely so payloads do not depend on a live
  catalog call. Once the provider selects internally, that lever is gone and the fake transport has
  to return a deterministic catalog instead. The ratchet is what every stage since S1 has been
  measured against, so this is not incidental — it is Task 10, and it lands *before* the goldens are
  regenerated.
* **SkyPilot decides CPU-vs-GPU from `spec.offer.gpu_type` being empty** (`providers/skypilot/__init__.py:914-917`),
  which comes from its own `find_offers` short-circuit when `min_vram_gb == 0`. That is the code
  path `examples/configs/skypilot-cpu.yaml` — the config both the S2 and S3 live smokes used — runs
  on. After the inversion the signal must come from `placement` directly. Task 8 does that
  explicitly rather than letting it fall out of a refactor.

### 3. A smaller one, recorded but not fixed here

§6 says the check runs before `engine.provision` "so a violation is torn down before the expensive
part of a boot". True on RunPod and Modal. **False on SkyPilot**, where `sky.launch` runs
`Task.setup` before it returns, so by the time a rate is readable the expensive part has already
happened. §14 half-acknowledges this ("a violation discards several minutes of provisioning"). A
pre-launch `sky.optimize()` estimate would close it; that is deliberately **not** in this plan —
it is new wire surface wanting its own live proof, and the readback is what makes the cap true
either way. Task 12 records it as an S5 follow-up.

---

**Global Constraints:**
- **The golden ratchet is the measure, and Part A must not move it.** All 31 goldens stay
  byte-identical through Task 5. Part B moves them exactly once, in Task 10, as a reviewed act with
  a decoded diff — the same discipline S3 used.
- **Never book above the cap where a catalog exists.** Any task that makes RunPod or Modal launch an
  over-cap instance in order to then destroy it is a regression, not a result.
- **Additive before subtractive.** `find_offers` keeps working until Task 8 deletes it; every task
  before that leaves the full suite green.
- `kinoforge.core.*` must not import `kinoforge.providers.*` at module scope.
- Teardown-on-violation must never swallow a teardown failure — the raised error carries both the
  cap violation and the teardown failure.
- Google-style docstrings, full type hints, Conventional Commits in imperative mood, never
  `--no-verify`, `rg` not `grep`, pixi for everything, local timezone.

**User decisions (already made):**
- S1's ruling that UNSUPPORTED-field severity is by risk coverage governs every new `consumes()` /
  `capabilities()` row here, as it did in S2 and S3.
- S3's ruling that a golden may move when the movement is the intended fix, decoded and shown,
  stands. Task 10 is the only task permitted to move a golden.
- Live smokes are pre-authorised up to the session budget. S2 and S3 both measured ~$0.01–0.04 per
  run on `c6i.large`; both smokes here use that same SKU and config.

---

## File Structure

**Modified:**
- `src/kinoforge/core/capabilities.py` — `RATE_READBACK`, `RATE_DETERMINISTIC`, `CATALOG_ENUMERATION`.
- `src/kinoforge/core/errors.py` — `RateCapExceeded`.
- `src/kinoforge/core/interfaces.py` — `ComputeProvider.realized_rate()`; `HardwareRequirements` and
  `InstanceSpec.offer` deleted in Task 8; `find_offers` off the ABC in Task 8.
- `src/kinoforge/providers/{skypilot,runpod,modal,local}/__init__.py` — `realized_rate`, capability
  declarations, internal selection, `consumes()` rows.
- `src/kinoforge/core/orchestrator.py:510-604,940-1000,1728` — the enforcement point; the offer-retry
  loop moves out; `_create_with_capacity_wait` rewired.
- `src/kinoforge/core/offers.py` — `filter_offers` keeps its logic, gains a `Placement` signature.
- `src/kinoforge/core/config.py:1624` — `hardware_requirements()` shim deleted.
- `src/kinoforge/cli/_commands.py:289` — `offers` gated on `CATALOG_ENUMERATION`.
- `src/kinoforge/validation/checks/capabilities.py` — the "neither rate capability declared" ERROR.
- `tools/snapshot_launch_payloads.py` — determinism without offer injection.
- `tools/diagnose_pod_boot.py:378`, `tools/probe_pod_watchdog.py:246` — the two non-CLI
  `find_offers` callers.
- ~41 test modules touching `find_offers`, ~25 touching `HardwareRequirements`.

**Created:**
- `tests/core/test_realized_rate.py`, `tests/core/test_rate_cap_enforcement.py`,
  `tests/providers/test_provider_selection.py`,
  `tests/live/test_compute_seam_s4_rate_cap_smoke.py`,
  `tests/live/test_compute_seam_s4_selection_smoke.py`.

---

# PART A — the verified rate cap (Tasks 0–5)

## Task 0: The capability split and `RateCapExceeded`

**Goal:** Add the three capabilities and the error type, declared by every provider, with nothing
reading them yet.

**Files:**
- Modify: `src/kinoforge/core/capabilities.py`, `src/kinoforge/core/errors.py`,
  `src/kinoforge/providers/{skypilot,runpod,modal,local}/__init__.py`
- Test: `tests/core/test_realized_rate.py`

**Acceptance Criteria:**
- [ ] `Capability.RATE_READBACK`, `Capability.RATE_DETERMINISTIC` and
      `Capability.CATALOG_ENUMERATION` exist.
- [ ] skypilot declares `RATE_READBACK`; runpod and modal declare `RATE_DETERMINISTIC`;
      runpod, modal and local declare `CATALOG_ENUMERATION`; skypilot does NOT.
- [ ] No provider declares BOTH rate capabilities — they are mutually exclusive claims about who
      chooses the SKU, and a provider claiming both is claiming the catalog is authoritative *and*
      that it isn't.
- [ ] `RateCapExceeded` carries `realized: float | None`, `cap: float`, `instance_id: str`,
      `placement_summary: str`, and its `str()` names both numbers and the identity.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/core/test_realized_rate.py tests/providers/test_launch_payload_goldens.py -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_realized_rate.py
"""Behavior: who chooses the SKU decides how the rate is known.

A marketplace provider books the SKU it was handed, so the catalog price IS
the rate (RATE_DETERMINISTIC). A declarative placer chooses for itself, so the
only honest source is a readback from the launched instance (RATE_READBACK).
Conflating the two is how a Lambda A100 billed $1.99 under a $1.09 cap while
every kinoforge surface reported the catalog number.
"""

from __future__ import annotations

import pytest

from kinoforge.core import registry
from kinoforge.core.capabilities import Capability
from kinoforge.core.errors import RateCapExceeded

_RATE_CAPS = {Capability.RATE_READBACK, Capability.RATE_DETERMINISTIC}


@pytest.mark.parametrize(
    ("provider_name", "expected"),
    [
        ("skypilot", Capability.RATE_READBACK),
        ("runpod", Capability.RATE_DETERMINISTIC),
        ("modal", Capability.RATE_DETERMINISTIC),
    ],
)
def test_each_provider_declares_the_rate_source_it_actually_has(
    provider_name: str, expected: Capability
) -> None:
    """Bug caught: declaring RATE_DETERMINISTIC on skypilot would make the
    orchestrator trust a catalog number the optimizer never consulted — which
    is precisely the F4 under-report this stage exists to end."""
    import kinoforge._adapters  # noqa: F401 — registers the providers

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert expected in cls.capabilities()


@pytest.mark.parametrize("provider_name", ["skypilot", "runpod", "modal", "local"])
def test_no_provider_claims_both_rate_sources(provider_name: str) -> None:
    """Bug caught: declaring both lets the enforcement point pick whichever
    branch it happens to test first, so the rule that governs a money decision
    would depend on statement order."""
    import kinoforge._adapters  # noqa: F401

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert len(_RATE_CAPS & cls.capabilities()) <= 1


def test_skypilot_does_not_claim_catalog_enumeration() -> None:
    """Bug caught: SkyPilot has no catalog to enumerate — `kinoforge offers`
    against it today prints a synthetic single-entry list that no launch ever
    consults. Declaring the capability keeps that fiction alive."""
    import kinoforge._adapters  # noqa: F401

    cls = registry.provider_class("skypilot")
    assert cls is not None
    assert Capability.CATALOG_ENUMERATION not in cls.capabilities()


def test_rate_cap_exceeded_names_both_numbers_and_the_identity() -> None:
    """Bug caught: a message that says only 'over budget' leaves an operator
    unable to tell a cap that is too low from a placement that is wrong, which
    is the difference between editing one YAML line and debugging a provider."""
    exc = RateCapExceeded(
        realized=1.99,
        cap=1.09,
        instance_id="kinoforge-xyz",
        placement_summary="sku=A100:1, cloud=lambda, region=us-west-2",
    )
    text = str(exc)
    assert "1.9900" in text
    assert "1.0900" in text
    assert "kinoforge-xyz" in text
    assert "cloud=lambda" in text


def test_rate_cap_exceeded_renders_an_unreadable_rate_without_lying() -> None:
    """Bug caught: formatting None through a float format string either raises
    or prints '0.0000', and a report of $0.0000/hr for a rate nobody could read
    is worse than saying it was unreadable."""
    exc = RateCapExceeded(
        realized=None,
        cap=1.09,
        instance_id="kinoforge-xyz",
        placement_summary="sku=A100:1",
    )
    assert "unreadable" in str(exc)
    assert "0.0000" not in str(exc)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_realized_rate.py -q`
Expected: FAIL — `ImportError: cannot import name 'RateCapExceeded' from 'kinoforge.core.errors'`.

- [ ] **Step 3: Implement**

In `core/capabilities.py`, extend the enum (append-only; the members are compared by name, so
ordering is cosmetic):

```python
    RATE_READBACK = "RATE_READBACK"
    RATE_DETERMINISTIC = "RATE_DETERMINISTIC"
    CATALOG_ENUMERATION = "CATALOG_ENUMERATION"
```

and document the split in the enum's docstring:

```
    RATE_READBACK — the provider CHOOSES the SKU, so the rate is only knowable
        by reading it back off the launched instance. skypilot.
    RATE_DETERMINISTIC — the requested SKU is the billed SKU, so the catalog
        price is the rate. runpod, modal.
    CATALOG_ENUMERATION — the provider can list what is bookable. Gates
        `kinoforge offers`. runpod, modal, local; NOT skypilot, whose optimizer
        takes constraints rather than publishing a catalog.
```

In `core/errors.py`:

```python
class RateCapExceeded(KinoforgeError):
    """A launched instance bills above ``placement.max_usd_per_hr``.

    Raised after ``create_instance`` and before ``engine.provision``, once the
    instance has been destroyed. Names both numbers and the identity of what
    was launched: an operator seeing only "over budget" cannot tell a cap that
    is too low from a placement that went somewhere unintended.

    Attributes:
        realized: The rate read back, or None when it could not be read.
        cap: The configured ceiling.
        instance_id: The instance that was launched and then destroyed.
        placement_summary: Human-readable identity of what was booked, e.g.
            ``"sku=A100:1, cloud=lambda, region=us-west-2"``.
    """

    def __init__(
        self,
        *,
        realized: float | None,
        cap: float,
        instance_id: str,
        placement_summary: str,
    ) -> None:
        """Initialise with both numbers and the launched identity."""
        self.realized = realized
        self.cap = cap
        self.instance_id = instance_id
        self.placement_summary = placement_summary
        rate = f"${realized:.4f}/hr" if realized is not None else "<unreadable>"
        super().__init__(
            f"realized {rate} exceeds cap ${cap:.4f}/hr\n"
            f"  ({placement_summary}, instance={instance_id})\n"
            f"  instance destroyed"
        )
```

Then add the declarations to each provider's `capabilities()` frozenset: `RATE_READBACK` on
skypilot; `RATE_DETERMINISTIC` + `CATALOG_ENUMERATION` on runpod and modal; `CATALOG_ENUMERATION`
on local. Local declares neither rate capability on purpose — it is unbilled, and Task 2's
validation ERROR must not fire for it, so give local `RATE_DETERMINISTIC` as well with the comment
that its catalog price is the literal `0.0`.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core/test_realized_rate.py tests/providers -q`
Expected: all PASS, `git status --short tests/providers/golden/` empty.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/capabilities.py src/kinoforge/core/errors.py src/kinoforge/providers tests/core/test_realized_rate.py
git commit -m "feat(core): add the rate-source capability split and RateCapExceeded"
```

---

## Task 1: `realized_rate()` on the ABC and on every provider

**Goal:** Give each provider a way to answer "what will this instance actually bill", implemented
from the source its capability declares.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`,
  `src/kinoforge/providers/{skypilot,runpod,modal,local}/__init__.py`
- Test: `tests/core/test_realized_rate.py` (extend)

**Acceptance Criteria:**
- [ ] `ComputeProvider.realized_rate(instance)` is a non-abstract method returning `None` by
      default, so a fifth provider that forgets it fails the Task 2 validation rather than
      silently reporting a rate it never read.
- [ ] SkyPilot reads `sky.status()` → the matching record's `handle.launched_resources.get_cost(3600)`.
      Verified present at the pinned `skypilot-0.12.3.post1`: `Resources.get_cost(seconds) -> float`
      exists and `CloudVmRayResourceHandle.__init__` assigns `self.launched_resources`.
- [ ] RunPod reads the pod's `costPerHr`, which `_LIST_PODS_QUERY` already selects.
- [ ] Modal returns the catalog price for the requested GPU class.
- [ ] Every implementation returns `None` rather than raising when the source is missing or
      unparseable — the caller distinguishes "over cap" from "unreadable", and an exception here
      would turn an unreadable rate into a crash mid-launch.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/core/test_realized_rate.py tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_skypilot_realized_rate_reads_the_launched_handle() -> None:
    """Bug caught: sourcing the rate from spec.offer (what create_instance does
    today, providers/skypilot/__init__.py:1105) reports the number kinoforge
    ASKED for. The optimizer's choice is only in the handle."""
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.skypilot import SkyPilotProvider

    class _Resources:
        def get_cost(self, seconds: float) -> float:
            assert seconds == 3600
            return 1.99

    class _Handle:
        launched_resources = _Resources()

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "kf-1", "handle": _Handle(), "status": "UP"}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) == pytest.approx(1.99)


def test_skypilot_realized_rate_is_none_when_the_handle_has_no_resources() -> None:
    """Bug caught: raising here turns an unreadable rate into a crash between
    create_instance and provision, leaving a live instance nobody tore down.
    None is what lets Task 2 destroy it deliberately."""
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.skypilot import SkyPilotProvider

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "kf-1", "handle": None, "status": "UP"}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_skypilot_realized_rate_is_none_for_an_unknown_cluster() -> None:
    """Boundary. Bug caught: returning the FIRST record's cost regardless of
    name would report a neighbouring cluster's rate — worse than unreadable,
    because it looks authoritative."""
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.skypilot import SkyPilotProvider

    class _Resources:
        def get_cost(self, seconds: float) -> float:
            return 99.0

    class _Handle:
        launched_resources = _Resources()

    class _FakeSky:
        def status(self, **_kw: object) -> list[dict[str, object]]:
            return [{"name": "someone-elses-cluster", "handle": _Handle()}]

    provider = SkyPilotProvider(sky_client=_FakeSky())
    inst = Instance(id="kf-1", provider="skypilot", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) is None


def test_runpod_realized_rate_reads_cost_per_hr_from_the_pod() -> None:
    """Bug caught: RunPod's catalog price and the pod's billed costPerHr can
    drift (spot/community repricing). The pod is the authority."""
    from kinoforge.core.interfaces import Instance
    from kinoforge.providers.runpod import RunPodProvider

    def _post(url: str, body: dict[str, object]) -> dict[str, object]:
        return {"data": {"myself": {"pods": [{"id": "pod-1", "costPerHr": "0.34"}]}}}

    provider = RunPodProvider(creds=None, http_post=_post, http_get=lambda _: {})
    inst = Instance(id="pod-1", provider="runpod", status="ready", created_at=0.0)
    assert provider.realized_rate(inst) == pytest.approx(0.34)
```

> Executor note: read `providers/runpod/__init__.py:1482-1500` (`_pod_to_instance`) for the exact
> `costPerHr` parsing already in place — reuse it rather than writing a second parser, and match
> the transport idiom in `tests/providers/test_runpod_create_pod_cloud_type.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_realized_rate.py -k realized_rate -q`
Expected: FAIL — `AttributeError: 'SkyPilotProvider' object has no attribute 'realized_rate'`.

- [ ] **Step 3: Implement**

On the ABC in `core/interfaces.py`, beside the other non-abstract hooks:

```python
    def realized_rate(self, instance: Instance) -> float | None:
        """Return the hourly rate *instance* will actually bill at.

        Read AFTER launch, from whatever source this provider's rate
        capability names: the launched handle where the provider chooses the
        SKU (``RATE_READBACK``), the catalog price where the requested SKU is
        the billed one (``RATE_DETERMINISTIC``).

        Args:
            instance: The instance to price, already created.

        Returns:
            The rate in USD per hour, or None when it cannot be read. Never
            raises: an unreadable rate is a decision for the caller (which
            tears the instance down), not a crash mid-launch.
        """
        return None
```

SkyPilot:

```python
    def realized_rate(self, instance: Instance) -> float | None:
        """Return the rate the optimizer's chosen resources will bill at.

        The launch payload is discarded by :meth:`create_instance` (the cluster
        name is the canonical id), so the handle is re-read from ``status()``.
        That also makes this correct on a warm attach, where no launch payload
        exists at all.

        Args:
            instance: The cluster to price.

        Returns:
            USD per hour, or None when the cluster or its handle is absent.
        """
        sky = self._sky()
        try:
            clusters = _resolve(sky, sky.status())
        except Exception:  # noqa: BLE001 — an unreadable rate is not a crash
            return None
        for cluster in clusters or []:
            if _record_field(cluster, "name") != instance.id:
                continue
            handle = (
                cluster.get("handle")
                if isinstance(cluster, dict)
                else getattr(cluster, "handle", None)
            )
            launched = getattr(handle, "launched_resources", None)
            if launched is None:
                return None
            try:
                return float(launched.get_cost(3600))
            except Exception:  # noqa: BLE001 — same reason
                return None
        return None
```

RunPod: query `_LIST_PODS_QUERY`, find the pod by id, and reuse the existing `costPerHr` coercion.
Modal: look the requested GPU class up in `providers/modal/_catalog.py` and return its price;
`instance.cost_rate_usd_per_hr` is already that number today, so returning it is correct and the
docstring should say the catalog IS the rate rather than implying a read happened. Local: return
`0.0`.

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/core tests/providers -q`
Expected: all PASS, goldens unmoved.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/interfaces.py src/kinoforge/providers tests/core/test_realized_rate.py
git commit -m "feat(providers): read the realized hourly rate from the launched instance"
```

---

## Task 2: Refuse a config whose provider cannot price itself

**Goal:** Turn "neither rate capability declared" into a load-time ERROR, so the enforcement point
in Task 3 never has to decide what to do about a provider that cannot answer.

**Files:**
- Modify: `src/kinoforge/validation/checks/capabilities.py`
- Test: `tests/core/test_capability_parity.py` (extend)

**Acceptance Criteria:**
- [ ] A cfg naming a provider that declares neither `RATE_READBACK` nor `RATE_DETERMINISTIC` is a
      validation **ERROR**, not a warning, with a message naming the provider and both capabilities.
- [ ] All four shipped providers pass — the check refuses nobody today, exactly like S2's `region`
      ERROR did when it landed.
- [ ] The check is registered in the same registry as the other capability checks and appears in
      `kinoforge doctor` output.

**Verify:** `pixi run python -m pytest tests/core/test_capability_parity.py tests/validation -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_a_provider_with_no_rate_source_is_a_load_error() -> None:
    """Bug caught: without this, a fifth provider that never implements
    realized_rate() reaches the enforcement point returning None, and the only
    honest response there is to destroy every instance it ever launches. The
    refusal belongs at load, where it costs nothing."""
    from kinoforge.core.capabilities import Capability
    from kinoforge.validation.checks.capabilities import rate_source_declared

    class _Priceless:
        name = "priceless"

        @classmethod
        def capabilities(cls, shape: object = None) -> frozenset[Capability]:
            return frozenset({Capability.HEARTBEAT_READ})

    gaps = rate_source_declared(_Priceless)
    assert [g.severity for g in gaps] == [Severity.ERROR]
    assert "RATE_READBACK" in gaps[0].detail
    assert "RATE_DETERMINISTIC" in gaps[0].detail
    assert "priceless" in gaps[0].detail


@pytest.mark.parametrize("provider_name", ["skypilot", "runpod", "modal", "local"])
def test_every_shipped_provider_declares_a_rate_source(provider_name: str) -> None:
    """The check refuses nobody today. Bug caught: shipping a check that fires
    on a real config turns doctor into noise operators learn to skip."""
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core import registry
    from kinoforge.validation.checks.capabilities import rate_source_declared

    cls = registry.provider_class(provider_name)
    assert cls is not None
    assert rate_source_declared(cls) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_capability_parity.py -k rate_source -q`
Expected: FAIL — `ImportError: cannot import name 'rate_source_declared'`.

- [ ] **Step 3: Implement**

Add `rate_source_declared(provider_cls) -> list[Gap]` beside the existing capability checks,
following `ProviderCapabilityCheck`'s Gap construction (read `validation/checks/capabilities.py:504`
for the registration shape and the exact `Gap` fields — do not invent them):

```python
def rate_source_declared(provider_cls: type) -> list[Gap]:
    """Return an ERROR gap when *provider_cls* declares no rate source.

    Args:
        provider_cls: The provider class to inspect.

    Returns:
        One ERROR gap, or an empty list when a rate source is declared.
    """
    declared = provider_cls.capabilities()
    if declared & {Capability.RATE_READBACK, Capability.RATE_DETERMINISTIC}:
        return []
    name = getattr(provider_cls, "name", provider_cls.__name__)
    return [
        Gap(
            field="compute.placement.max_usd_per_hr",
            severity=Severity.ERROR,
            detail=(
                f"{name} declares neither RATE_READBACK nor RATE_DETERMINISTIC, "
                f"so kinoforge cannot know what a launched instance bills and "
                f"max_usd_per_hr cannot be enforced. A provider that chooses "
                f"its own SKU declares RATE_READBACK and implements "
                f"realized_rate(); one that books the SKU it is handed "
                f"declares RATE_DETERMINISTIC."
            ),
        )
    ]
```

Register it so `kinoforge doctor` runs it.

- [ ] **Step 4: Run and commit**

Run: `pixi run python -m pytest tests/core tests/validation -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/validation tests/core/test_capability_parity.py
git commit -m "feat(validation): refuse a provider that declares no rate source"
```

---

## Task 3: Enforce the cap between launch and provision

**Goal:** Check the realized rate after `create_instance` and before `engine.provision`, destroy on
violation, and never swallow a teardown failure.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:940-1000`
- Test: `tests/core/test_rate_cap_enforcement.py`

**Acceptance Criteria:**
- [ ] The check runs after `_create_with_capacity_wait` returns and BEFORE
      `_wait_for_provider_ready` and `_provision_compute_once`.
- [ ] `realized > cap` → `destroy_instance` then `RateCapExceeded`.
- [ ] `realized is None` on a `RATE_READBACK` provider → `destroy_instance` then `RateCapExceeded`
      with `realized=None`. An unreadable rate on a provider that chooses its own SKU is exactly
      the F4 case; trusting it would defeat the stage.
- [ ] `realized is None` on a `RATE_DETERMINISTIC` provider → **no teardown**, a WARNING, and the
      launch proceeds. The catalog is the rate there, so an unreadable readback is a missing
      nicety, not a money risk.
- [ ] `realized <= cap` → nothing happens; no extra provider calls beyond the one read.
- [ ] When `destroy_instance` ALSO fails, the raised error names both the cap violation and the
      teardown failure, and the instance id survives in the message so the reaper can finish.
- [ ] `on_instance_created` is called BEFORE the check, so a failed teardown leaves a ledger row
      pointing at the live instance. (§6 assumes §9's provisional rows, which are S5 — today only
      SkyPilot writes one pre-launch, so this ordering is what makes the RunPod path reapable.)
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/core/test_rate_cap_enforcement.py tests/core/test_orchestrator.py -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_rate_cap_enforcement.py
"""Behavior: an instance that bills above the cap is destroyed, loudly.

F4 in one sentence: a Lambda A100 billed $1.99/hr under a $1.09 cap and every
kinoforge surface reported $1.09, because the cap was a filter over a catalog
the optimizer never consulted. The filter cannot see the optimizer's choice;
only a readback can, and a readback is worthless unless something acts on it.
"""

from __future__ import annotations

import pytest

from kinoforge.core.capabilities import Capability
from kinoforge.core.errors import RateCapExceeded


def test_over_cap_destroys_the_instance_and_raises(rate_harness) -> None:
    """Bug caught: logging the violation and continuing bills the operator at
    a rate they capped, for the whole run."""
    h = rate_harness(realized=1.99, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    assert h.destroyed == [h.instance_id]
    assert ei.value.realized == pytest.approx(1.99)
    assert ei.value.cap == pytest.approx(1.09)
    assert h.provisioned is False


def test_unreadable_rate_on_a_readback_provider_destroys(rate_harness) -> None:
    """Bug caught: treating unreadable as OK on the ONE provider that chooses
    its own SKU reinstates F4 exactly — the number nobody could read is the
    number that was wrong."""
    h = rate_harness(realized=None, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    assert h.destroyed == [h.instance_id]
    assert ei.value.realized is None
    assert "unreadable" in str(ei.value)


def test_unreadable_rate_on_a_deterministic_provider_proceeds(rate_harness) -> None:
    """Bug caught: destroying here would tear down healthy RunPod pods over a
    missing nicety — the catalog already bounded the price before booking."""
    h = rate_harness(
        realized=None, cap=1.09, caps={Capability.RATE_DETERMINISTIC}
    )
    h.run()
    assert h.destroyed == []
    assert h.provisioned is True


def test_under_cap_proceeds_without_extra_provider_calls(rate_harness) -> None:
    """Bug caught: a check that re-reads the rate per retry turns one launch
    into N provider calls on the happy path."""
    h = rate_harness(realized=0.85, cap=1.09, caps={Capability.RATE_READBACK})
    h.run()
    assert h.destroyed == []
    assert h.realized_calls == 1
    assert h.provisioned is True


def test_teardown_failure_is_reported_with_the_violation(rate_harness) -> None:
    """Bug caught: swallowing the teardown error reports 'instance destroyed'
    about an instance that is still billing, and the operator has no id to
    chase."""
    h = rate_harness(
        realized=1.99,
        cap=1.09,
        caps={Capability.RATE_READBACK},
        destroy_raises=RuntimeError("provider refused"),
    )
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    text = str(ei.value)
    assert "provider refused" in text
    assert h.instance_id in text


def test_the_ledger_row_is_written_before_the_check(rate_harness) -> None:
    """Bug caught: checking before on_instance_created means a failed teardown
    leaves an instance with no ledger row — invisible to `kinoforge list` and
    to the reaper, which is the orphan class this project has paid for twice."""
    h = rate_harness(realized=1.99, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded):
        h.run()
    assert h.recorded == [h.instance_id]
```

> Executor note: build `rate_harness` as a fixture in this module. It needs a fake provider
> (`create_instance` → a fixed `Instance`, recording `realized_rate` calls, `destroy_instance`
> recording ids or raising), a fake engine, and a call into
> `orchestrator._provision_instance_and_build_backend`. Model it on the existing fakes in
> `tests/core/test_orchestrator_provision_threading.py` — that module already drives this exact
> function with mocks and is the cheapest correct template.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_rate_cap_enforcement.py -q`
Expected: FAIL — the instance is provisioned and nothing is destroyed, because no check exists.

- [ ] **Step 3: Implement**

In `_provision_instance_and_build_backend`, immediately after `on_instance_created(instance)` and
before the `dataclasses.replace(instance, status=_wait_for_provider_ready(...))` block:

```python
    # compute-seam S4: the cap is verified against what was LAUNCHED, not
    # filtered against a catalog the chooser may never have consulted. Runs
    # before _wait_for_provider_ready and engine.provision so a violation is
    # torn down before the expensive part of a boot — on RunPod and Modal.
    # On SkyPilot `sky.launch` already ran Task.setup by the time it returned,
    # so there the teardown discards work that has been done; that is the
    # accepted trade (design §14) and a pre-launch estimate is an S5 follow-up.
    _enforce_rate_cap(
        provider=resolved_provider,
        instance=instance,
        cap=cfg.placement().max_usd_per_hr,
    )
```

and the helper beside `_create_with_capacity_wait`:

```python
def _enforce_rate_cap(
    *,
    provider: ComputeProvider,
    instance: Instance,
    cap: float,
    logger: logging.Logger = _log,
) -> None:
    """Destroy *instance* and raise when it bills above *cap*.

    Args:
        provider: The provider that launched it.
        instance: The freshly created instance.
        cap: ``placement.max_usd_per_hr``.
        logger: Injected for testability.

    Raises:
        RateCapExceeded: The realized rate exceeds *cap*, or could not be read
            on a provider that chooses its own SKU. The instance is destroyed
            first; a teardown failure is folded into the same error rather
            than replacing it.
    """
    declared = provider.capabilities()
    realized = provider.realized_rate(instance)
    if realized is not None and realized <= cap:
        return
    if realized is None and Capability.RATE_READBACK not in declared:
        # The catalog already bounded this before booking; an unreadable
        # readback is a missing nicety, not a money risk.
        logger.warning(
            "[rate-cap] %s: realized rate unreadable; the catalog price "
            "bounded this launch before it was booked",
            instance.id,
        )
        return

    summary = _placement_summary(instance)
    teardown_error: str | None = None
    try:
        provider.destroy_instance(instance.id)
    except Exception as exc:  # noqa: BLE001 — folded into the raised error
        teardown_error = repr(exc)
        logger.error(
            "[rate-cap] %s: teardown FAILED after a cap violation; the "
            "instance is still billing and its ledger row is the only "
            "handle on it",
            instance.id,
        )
    err = RateCapExceeded(
        realized=realized,
        cap=cap,
        instance_id=instance.id,
        placement_summary=summary,
    )
    if teardown_error is not None:
        err.args = (f"{err.args[0]}\n  TEARDOWN ALSO FAILED: {teardown_error}",)
    raise err
```

`_placement_summary(instance)` returns `f"sku={instance.tags.get('sku', '?')}, provider={instance.provider}"`
plus region/cloud when the tags carry them — keep it to what an `Instance` actually holds, and do
not invent fields.

- [ ] **Step 4: Run and commit**

Run: `pixi run python -m pytest tests/core tests/providers -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/orchestrator.py tests/core/test_rate_cap_enforcement.py
git commit -m "feat(orchestrator): verify the realized rate and tear down a violation"
```

---

## Task 4: `Instance.cost_rate_usd_per_hr` sources from the realized read

**Goal:** Make the number every downstream surface reads — ledger, `est_spend`, `kinoforge list`,
every `lifecycle.budget` computation — the one that was read back, not the one that was asked for.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py:1105`,
  `src/kinoforge/providers/modal/__init__.py:289`, `src/kinoforge/core/orchestrator.py`
- Test: `tests/core/test_rate_cap_enforcement.py` (extend)

**Acceptance Criteria:**
- [ ] After a successful cap check, `instance.cost_rate_usd_per_hr` equals the realized rate.
- [ ] On a `RATE_DETERMINISTIC` provider with an unreadable readback, the existing catalog value
      survives — this must not zero out RunPod's cost tracking.
- [ ] The ledger row written by `on_instance_created` carries the realized number. Since that
      callback runs BEFORE the check (Task 3), the row is updated after a successful check rather
      than being written twice with different numbers — state which mechanism is used and why.
- [ ] SkyPilot's create-time `cost_rate_usd_per_hr=spec.offer...` is replaced; that assignment is
      the literal F4 under-report.
- [ ] All 31 goldens byte-identical. (The goldens capture the WIRE payload, not the returned
      Instance, so a changed cost source must not move them — if one moves, the capture is reading
      the spec rather than the wire and that is the finding.)

**Verify:** `pixi run python -m pytest tests/core tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_the_instance_carries_the_realized_rate_after_the_check(rate_harness) -> None:
    """Bug caught, and it is F4 itself: reporting the asked-for rate makes the
    ledger, est_spend, kinoforge list and every budget computation agree with
    each other and disagree with the invoice."""
    h = rate_harness(
        realized=0.85, cap=1.09, caps={Capability.RATE_READBACK}, catalog_rate=0.40
    )
    h.run()
    assert h.final_instance.cost_rate_usd_per_hr == pytest.approx(0.85)


def test_an_unreadable_deterministic_rate_keeps_the_catalog_number(rate_harness) -> None:
    """Bug caught: overwriting with None zeroes RunPod's cost tracking, so
    budget guardrails silently stop firing — a worse failure than the one this
    task fixes, because it is invisible."""
    h = rate_harness(
        realized=None,
        cap=1.09,
        caps={Capability.RATE_DETERMINISTIC},
        catalog_rate=0.40,
    )
    h.run()
    assert h.final_instance.cost_rate_usd_per_hr == pytest.approx(0.40)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/core/test_rate_cap_enforcement.py -k realized_rate_after -q`
Expected: FAIL — the instance still carries the catalog number.

- [ ] **Step 3: Implement**

Have `_enforce_rate_cap` return the realized rate (or None) and, in the caller, fold it onto the
instance with `dataclasses.replace` when it is not None. Update the ledger row through the same
path `on_instance_created` used, so there is exactly one row and its final value is the realized
one. In SkyPilot's `create_instance`, stop sourcing `cost_rate_usd_per_hr` from `spec.offer` —
leave it `0.0` with a comment that the orchestrator fills it from `realized_rate`, because a
provider that cannot know the rate at create time should not guess one.

- [ ] **Step 4: Run and commit**

Run: `pixi run python -m pytest tests/core tests/providers tests/cli -q`

```bash
pixi run pre-commit run --all-files
git add src/kinoforge tests/core/test_rate_cap_enforcement.py
git commit -m "fix(cost): source cost_rate_usd_per_hr from the realized read, closing F4"
```

---

## Task 5: Live smoke — a violated cap tears the instance down (USER GATE)

**Goal:** Prove on real infrastructure that a cap set below what the cloud actually bills destroys
the instance and raises, rather than proceeding at the higher rate.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current
> conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or
> by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been
> re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s4_rate_cap_smoke.py`,
  `tests/live/_s4_rate_cap_evidence.json`

**Acceptance Criteria:**
- [ ] The RED scaffold is COMMITTED BEFORE any live spend — project durability rule.
- [ ] `pixi run preflight` exits 0 before the run, and its exit code is recorded in the evidence.
- [ ] `examples/configs/skypilot-cpu.yaml` is launched through `build_provider_for` with
      `placement.max_usd_per_hr` overridden to a value BELOW `c6i.large`'s real rate (the S2/S3
      smokes measured $0.085/hr; use `0.01`). The run must raise `RateCapExceeded`.
- [ ] The raised error's `realized` is a real number read from the handle — NOT None. A None here
      would prove only that the unreadable branch works, which is not the claim.
- [ ] `realized` is compared against the same cluster's rate read independently via
      `sky status` / the AWS published price, and the evidence records both.
- [ ] The instance is destroyed by the enforcement path itself, and teardown is verified AFTER the
      process exits: both `kinoforge list` lines, `sky status`, and EC2 `describe-instances`
      reporting `terminated`. Reuse the S1/S2/S3 teardown helper by importing it from
      `tests/live/test_compute_seam_s1_smoke.py`; say so in the report.
- [ ] Utilisation polled every 60–90 s and recorded; spend recorded and under $0.06.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s4_rate_cap_smoke.py -v -s` → PASS, followed by `pixi run kinoforge list` showing both empty lines.

**Steps:**

- [ ] **Step 1: Write the scaffold and commit it RED**

Model it on `tests/live/test_compute_seam_s3_setup_run_smoke.py`, which is green and already
carries the skip gate, the util-poll loop, the evidence shape and the imported teardown. The
structural difference: this one asserts that the run RAISES, and reads `exc.realized` off the
raised error rather than reading a task_config off a recording proxy.

```bash
git add tests/live/test_compute_seam_s4_rate_cap_smoke.py
git commit -m "test(live): add the RED S4 rate-cap teardown smoke scaffold"
```

- [ ] **Step 2: Preflight** — `pixi run preflight` → expect exit 0. A transient RunPod SSL
      handshake timeout has produced a spurious exit 1 twice across S2/S3; re-run before concluding
      anything.

- [ ] **Step 3: Run the smoke, polling throughout.** Do NOT run other `aws` commands while it is in
      flight — S2 proved a concurrent `describe-instances` pushes the smoke's own query past its
      120 s ceiling.

- [ ] **Step 4: Verify teardown after exit**

```bash
pixi run kinoforge list
pixi run -e live-skypilot sky status
```

Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` AND
`No existing clusters.`

- [ ] **Step 5: Write evidence and commit.** Follow `tests/live/_s3_smoke_evidence.json`'s shape.
      Local timezone, no credential value anywhere.

---

# PART B — declarative selection (Tasks 6–11)

## Task 6: `filter_offers` takes a `Placement`, and the `HardwareRequirements` shim dies

**Goal:** Collapse the two overlapping resource types into the portable one, without yet moving any
selection.

**Files:**
- Modify: `src/kinoforge/core/offers.py:20`, `src/kinoforge/core/config.py:1624`,
  `src/kinoforge/core/interfaces.py:39-55`
- Test: `tests/core/test_offers.py` (extend)

**Acceptance Criteria:**
- [ ] `filter_offers(offers, placement)` takes a `Placement`; `gpu_preference` reads from
      `placement.accelerators`, which S1 already documented as the same semantics under a new name.
- [ ] The price filter is UNCHANGED — `mode == "pod" and rate > max_usd_per_hr` still excludes.
      This is finding #1: it is what keeps RunPod from booking over-cap in the first place.
- [ ] `Config.hardware_requirements()` and `HardwareRequirements` are deleted;
      `rg -n 'HardwareRequirements' src/` returns nothing.
- [ ] `find_offers` signatures change to `(self, placement: Placement)` on all four providers —
      still on the ABC at this point.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/core tests/providers -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_filter_offers_takes_a_placement_and_still_excludes_over_cap_pods() -> None:
    """The price filter is the pre-book half of the cap and must survive S4.

    Bug caught: dropping it because "the cap is verified after launch now"
    makes RunPod book an over-cap pod and then destroy it — paying for a boot
    that today never starts.
    """
    from kinoforge.core.interfaces import Offer, Placement
    from kinoforge.core.offers import filter_offers

    cheap = Offer(id="a", gpu_type="A", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=0.50)
    dear = Offer(id="b", gpu_type="B", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=9.00)
    kept = filter_offers([cheap, dear], Placement(max_usd_per_hr=1.00))
    assert [o.id for o in kept] == ["a"]


def test_filter_offers_ranks_by_accelerators_not_gpu_preference() -> None:
    """Bug caught: reading a field that no longer exists silently ranks
    nothing, so an operator's ordered preference becomes input order."""
    from kinoforge.core.interfaces import Offer, Placement
    from kinoforge.core.offers import filter_offers

    a = Offer(id="a", gpu_type="A100", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=1.0)
    h = Offer(id="h", gpu_type="H100", vram_gb=80, cuda="12.8", cost_rate_usd_per_hr=1.0)
    kept = filter_offers([a, h], Placement(accelerators=("H100", "A100")))
    assert [o.gpu_type for o in kept] == ["H100", "A100"]
```

- [ ] **Step 2: Run to verify it fails** — `TypeError` on the `Placement` argument.

- [ ] **Step 3: Implement.** Change the signature, swap `reqs.gpu_preference` for
      `placement.accelerators`, delete `HardwareRequirements` and the config shim, and update the
      four `find_offers` signatures plus the two tools and the CLI. Expect ~25 test modules to need
      the type swap; it is mechanical.

- [ ] **Step 4: Run and commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(core): fold HardwareRequirements into Placement"
```

---

## Task 7: RunPod owns its own offer retry

**Goal:** Move `_create_with_offer_retry` inside RunPod, carrying its tests with it, so the
orchestrator stops iterating a catalog on behalf of providers that do not have one.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:510-549,940-1000,1728`,
  `src/kinoforge/providers/runpod/__init__.py`
- Test: move the existing retry tests, adapted, into `tests/providers/test_runpod_offer_retry.py`

**Acceptance Criteria:**
- [ ] RunPod's `create_instance` internally enumerates, filters and retries on `CapacityError`,
      preserving TODAY's semantics exactly: first-offer-first, `accelerators` order, and
      non-capacity errors propagating immediately without trying the next offer.
- [ ] The existing orchestrator retry tests are MOVED and adapted, not rewritten from scratch —
      design §14 names this as the load-bearing behaviour most at risk in S4.
- [ ] `_create_with_capacity_wait` survives and now wraps `provider.create_instance(spec)`; on a
      provider that declares no `capacity_wait_s`, it is a pass-through.
- [ ] Both orchestrator call sites (`:954` and `:1728`) are migrated.
- [ ] All 31 goldens byte-identical.

**Verify:** `pixi run python -m pytest tests/providers/test_runpod_offer_retry.py tests/core/test_orchestrator.py tests/providers -q`

**Steps:**

- [ ] **Step 1: Inventory the behaviour before moving it**

```bash
rg -n 'offer_retry|CapacityError' tests/ | rg -o '^tests/[^:]+' | sort -u
```

Record the list in the commit message. Every one of those files is asserting a behaviour that must
still hold after the move; a test that disappears in this task is a behaviour that stopped being
checked.

- [ ] **Step 2: Write the failing test** (in the new provider-side module)

```python
def test_runpod_tries_the_next_offer_on_capacity_error() -> None:
    """Bug caught: giving up on the first CapacityError fails a run during a
    capacity drought that the previous behaviour rode out — the exact regression
    design §14 flags as S4's most likely invisible break."""
    ...


def test_runpod_does_not_retry_a_non_capacity_error() -> None:
    """Bug caught: retrying an auth failure across every offer turns one clear
    error into N confusing ones and delays the real message."""
    ...


def test_runpod_honours_accelerator_order_when_retrying() -> None:
    """Bug caught: retrying in catalog order discards the operator's stated
    preference precisely when capacity is tight and it matters most."""
    ...
```

> Executor note: fill these in from the bodies of the current orchestrator tests found in Step 1 —
> copy the assertions, re-point the entry call at `RunPodProvider.create_instance`.

- [ ] **Step 3: Implement, then Step 4: Run and commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(runpod): own the offer-retry loop instead of the orchestrator"
```

---

## Task 8: `find_offers` leaves the ABC; `spec.offer` is deleted

**Goal:** Make selection the provider's business, and remove the field that let a caller pre-decide
a SKU a declarative placer cannot honour.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:451`, all four providers,
  `src/kinoforge/core/spec_builder.py`
- Test: `tests/providers/test_provider_selection.py`

**Acceptance Criteria:**
- [ ] `ComputeProvider` has no `find_offers`; RunPod, Modal and Local keep it as a public method
      (gated by `CATALOG_ENUMERATION`), SkyPilot does not have one at all.
- [ ] `InstanceSpec.offer` is deleted. `rg -n 'spec\.offer' src/` returns nothing.
- [ ] **SkyPilot decides CPU-vs-GPU from `placement`, not from a synthetic offer.** With
      `min_vram_gb == 0` and no `accelerators`, the task requests `cpus`/`memory`; otherwise it
      requests the named accelerator. This is finding #2 — `examples/configs/skypilot-cpu.yaml` is
      the config that exercises it and both prior live smokes ran on it.
- [ ] RunPod's `gpuTypeId` and Modal's `gpu=` come from the provider's own selection.
- [ ] `Offer` still exists as a provider-internal type in `core/interfaces.py`, documented as no
      longer a seam type.
- [ ] Goldens are NOT regenerated in this task — Task 10 owns that, after the harness is reworked.
      Expect `tests/providers/test_launch_payload_goldens.py` to FAIL at the end of this task and
      say so in the commit message; it goes green again in Task 10.

**Verify:** `pixi run python -m pytest tests/providers -q --ignore=tests/providers/test_launch_payload_goldens.py`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_skypilot_cpu_config_requests_cpus_not_an_accelerator() -> None:
    """Bug caught, and it would be invisible offline: the CPU branch keys off
    `spec.offer.gpu_type` being empty today (providers/skypilot/__init__.py:914).
    Delete spec.offer without moving that signal onto placement and the CPU
    smoke config starts asking for a GPU — a config both the S2 and S3 live
    smokes ran on, at ~20x the price.
    """
    from pathlib import Path

    task_config = _captured_task_config("examples/configs/skypilot-cpu.yaml")
    assert "accelerators" not in task_config["resources"]
    assert task_config["resources"]["cpus"]


def test_a_gpu_config_still_requests_its_named_accelerator() -> None:
    """The other side of the same branch. Bug caught: collapsing both arms into
    the CPU shape books a CPU box for a Wan render, which fails late and
    expensively at model load."""
    task_config = _captured_task_config(
        "examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml"
    )
    assert task_config["resources"]["accelerators"]
```

- [ ] **Step 2: Run to verify it fails**, **Step 3: Implement**, **Step 4: Run and commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(core): take find_offers off the ABC and delete spec.offer"
```

---

## Task 9: `kinoforge offers` is gated on `CATALOG_ENUMERATION`

**Goal:** Stop printing a catalog for a provider that does not have one.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py:289`, `tools/diagnose_pod_boot.py:378`,
  `tools/probe_pod_watchdog.py:246`
- Test: `tests/cli/test_cmd_offers.py`

**Acceptance Criteria:**
- [ ] On a provider declaring `CATALOG_ENUMERATION`, `kinoforge offers` behaves exactly as today.
- [ ] On skypilot it exits non-zero with a message saying the provider does not enumerate and
      pointing at `sky show-gpus` — not an empty table, which reads as "no capacity".
- [ ] The two tools are migrated to the provider-side method.

**Verify:** `pixi run python -m pytest tests/cli -q`

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
def test_offers_on_skypilot_explains_instead_of_printing_an_empty_table(capsys) -> None:
    """Bug caught: an empty table is indistinguishable from a capacity drought,
    so an operator retries for hours against a provider that was never going
    to list anything. The synthetic single-entry catalog it prints today is
    worse still — it names a SKU no launch consults."""
    rc = _run_offers("examples/configs/skypilot-cpu.yaml")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert rc != 0
    assert "does not enumerate" in out
    assert "sky show-gpus" in out
```

- [ ] **Step 2–4: Run RED, implement, run green, commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "feat(cli): gate kinoforge offers on CATALOG_ENUMERATION"
```

---

## Task 10: Rework the golden harness, then regenerate

**Goal:** Restore the ratchet's determinism now that it can no longer inject an offer, and
regenerate the goldens as one reviewed act.

**Files:**
- Modify: `tools/snapshot_launch_payloads.py:181-300`
- Modify: `tests/providers/golden/launch_payloads/*.json` (regenerated — **sanctioned**)

**Acceptance Criteria:**
- [ ] `build_spec` no longer passes an offer. Determinism comes from a frozen fake catalog returned
      by each provider's injected transport, so the captured payload still depends only on the
      config.
- [ ] `_catalog_offer` is deleted or becomes the fake catalog's single entry — whichever it is, the
      module docstring's determinism contract is updated to match, because that contract is what a
      future reader trusts.
- [ ] Every moved golden is reviewed with a decoded diff, and the report states, per provider, what
      moved and why. Expected: the RunPod `gpuTypeId` and Modal `gpu=` should be UNCHANGED (the
      same SKU is still selected, just internally); movement there is a finding, not a result.
- [ ] `tests/providers/test_launch_payload_goldens.py` is green again.
- [ ] `pixi run test` green; `pixi run typecheck` and `pixi run lint` clean.

**Verify:** `pixi run python tools/snapshot_launch_payloads.py && pixi run python -m pytest tests/providers -q`

**Steps:**

- [ ] **Step 1: Capture the before-state**

```bash
pixi run python - <<'PY'
import json, pathlib
for p in sorted(pathlib.Path("tests/providers/golden/launch_payloads").glob("*.json")):
    d = json.loads(p.read_text())
    inp = d.get("input") or d.get("request") or {}
    key = inp.get("gpuTypeId") or inp.get("gpu") or (d.get("task_config", {}).get("resources"))
    print(f"{p.stem}: {key}")
PY
```

Record it. Step 4 compares against it — this is the SKU-selection identity the inversion must
preserve.

- [ ] **Step 2: Rework the harness**, **Step 3: Regenerate**, **Step 4: Diff and review**

```bash
pixi run python tools/snapshot_launch_payloads.py
git diff --stat tests/providers/golden/launch_payloads/
```

Re-run the Step 1 script and diff the two listings. Any changed SKU is a regression — STOP and
report BLOCKED.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(goldens): make the ratchet deterministic without offer injection"
```

---

## Task 11: Live smoke — the inverted path still books the right box (USER GATE)

**Goal:** Prove that with selection inside the provider, the CPU config still books `c6i.large` and
reaches ready — the claim the golden ratchet cannot make, because it asserts the payload rather
than what AWS does with it.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current
> conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or
> by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been
> re-validated independently, with output captured.

**Files:**
- Create: `tests/live/test_compute_seam_s4_selection_smoke.py`,
  `tests/live/_s4_selection_evidence.json`

**Acceptance Criteria:**
- [ ] The RED scaffold is COMMITTED BEFORE any live spend.
- [ ] `pixi run preflight` exits 0 before the run, recorded in the evidence.
- [ ] `examples/configs/skypilot-cpu.yaml` launches through `build_provider_for` with NO offer
      passed by the caller, reaches ready, and EC2 reports the instance type is `c6i.large` in
      `us-west-2a` — the same SKU and AZ S2 and S3 both measured, so a change is attributable to
      this stage.
- [ ] `realized_rate` returns a real number for the live cluster and it is recorded alongside the
      published `c6i.large` price; the run passes its cap rather than violating it (Task 5 covered
      the violation path).
- [ ] Teardown convergent and verified AFTER the process exits: both `kinoforge list` lines,
      `sky status`, EC2 `terminated`. Reuse the S1 teardown helper by importing it; say so.
- [ ] Utilisation polled every 60–90 s and recorded; spend under $0.06.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot python -m pytest tests/live/test_compute_seam_s4_selection_smoke.py -v -s` → PASS, then `pixi run kinoforge list` showing both empty lines.

**Steps:** Same five-step shape as Task 5 — scaffold committed RED, preflight, run with polling,
verify teardown after exit, write evidence and commit.

---

## Task 12: Documentation, the design-doc corrections, and PROGRESS

**Goal:** Leave the record accurate, including the two places the design was wrong and the one
limitation this stage accepts.

**Files:**
- Modify: `docs/superpowers/specs/2026-08-24-compute-seam-portable-core-design.md` §5, §6, §11
- Modify: `README.md`, `SPEC.md`, `docs/breaking-changes.md`, `docs/lifecycle.md`, `PROGRESS.md`

**Acceptance Criteria:**
- [ ] §5 item 6 and §6 Part 1 are corrected inline: the cap remains a pre-book filter wherever a
      catalog exists AND is verified after launch. Follow the convention S1–S3 used — correct the
      text in place and say when and why.
- [ ] §6's "torn down before the expensive part of a boot" is qualified: true on RunPod and Modal,
      false on SkyPilot, with the pre-launch-estimate idea recorded as an S5 follow-up.
- [ ] `docs/breaking-changes.md` gains an S4 entry: `compute.requirements`-era
      `HardwareRequirements` is gone, `kinoforge offers` now refuses on skypilot, and a custom
      provider must declare a rate capability or fail validation.
- [ ] PROGRESS's RESUME SNAPSHOT records S4 shipped, both live smoke results, which goldens moved
      and why, and the next action (write the S5 plan: endpoint shape + ledger generalisation).
- [ ] The S1/S2/S3 follow-ups this stage closed are marked closed; the ones it did not stay listed.
- [ ] `pixi run test && pixi run typecheck && pixi run lint` → all green.

**Verify:** `pixi run test && pixi run typecheck && pixi run lint`

**Steps:** Correct §5/§6/§11 → sweep for stale references → README/SPEC → breaking-changes → new
PROGRESS RESUME SNAPSHOT with the previous one demoted → full suite → commit.

---

## Self-Review Notes

**Spec coverage:** §5 (declarative selection, `find_offers` off the ABC, retry inside RunPod,
`kinoforge offers` gating, `Offer` as an internal type, `HardwareRequirements` folded in, Modal's
placement mapping) → Tasks 6–9. §6 (both parts of rate-cap enforcement, the capability split,
teardown-on-violation, `Instance.cost_rate_usd_per_hr` from the realized read) → Tasks 0–4. §10's
per-stage live smoke → Tasks 5 and 11. §11's S4 line → the whole plan.

**Where this plan departs from the design, deliberately:** the cap stays a pre-book filter on
enumerating providers (finding #1 — removing it would make kinoforge pay for boots it currently
never starts); the golden harness is reworked before any golden is regenerated (finding #2 — the
ratchet is the instrument every prior stage was measured with, and it is built on the very field
this stage deletes); and §6's "before the expensive part of a boot" is recorded as false on
SkyPilot rather than repeated.

**Deliberately NOT in this plan:** a pre-launch `sky.optimize()` cost estimate (new wire surface,
wants its own live proof — S5 follow-up); wiring `region` on RunPod or Modal (still where S2 left
it); the 11 ungated `tests/live` modules; the golden ratchet's non-recursive glob, which still
misses 7 configs under `grids/` and `extras/`.

**The ordering that matters:** Part A (0–5) must land before Part B, because Task 4 removes
`Instance.cost_rate_usd_per_hr`'s dependency on `spec.offer` and Task 8 deletes that field — doing
B first would strand the cost source with nothing to replace it. Task 6 must precede Task 8, or
`find_offers` changes signature and location in one commit. Task 10 must follow Task 8 and is the
only task permitted to move a golden.
