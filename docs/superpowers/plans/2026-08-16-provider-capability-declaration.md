# Provider Capability Declaration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every compute provider declare the guardrails it can actually enforce, and have config validation report — before launch — every guardrail the selected provider cannot honour.

**Architecture:** A `Capability` StrEnum plus a `ComputeProvider.capabilities(shape)` classmethod becomes the single source of truth; the five existing provider-string tables derive from it. A STATIC validation check maps each guardrail a config asserts to the risk it prevents, and reports ERROR when no declared capability covers that risk, WARN naming the substitute when one does. The reaper's heartbeat gate consults the declaration so an expected absence stops masking age/grace verdicts.

**Tech Stack:** Python 3.13, pydantic v2 configs, pytest, ruff, mypy, pixi.

**Global Constraints:**
- Design doc is `docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md`. Every capability cell must trace to a call site; no aspirational declarations.
- No provider gains enforcement it does not already have. This plan makes existing behaviour honest.
- No live spend. Every test runs offline with fake transports.
- `Verdict.HEARTBEAT_SUBSTRATE_MISSING` keeps its name and string value — it is a serialized public contract (`core/reaper.py:27-31`).
- `DEFAULT_APPLY_POLICY` must not gain `ORPHAN_REAP`. Plain `kinoforge reap --apply` behaviour is unchanged by this plan.
- Every shipped `examples/configs/*.yaml` must still load. WARNs are expected; ERRORs are a regression.
- `core/registry.py` must not import a concrete adapter module (its own module docstring rule) — the lazy composition-root import lives in `core/capabilities.py`.

**User decisions (already made):**
- The dishonesty to fix is **fake-satisfied guardrails**: the heartbeat loop writes `last_heartbeat` from the orchestrator clock, so a skypilot ledger row is indistinguishable from one backed by a real wire-level read.
- Severity is **risk-covered downgrade**: fatal when no other declared capability bounds the same risk, loud WARN naming the substitute when one does.
- `IDLE_AUTOSTOP` is **parameterised by workload shape**, re-derived at launch from `spec.run_cmd`.
- The reaper **splits expected from unexpected** heartbeat absence; no verdict becomes destructive on its own.
- The existing string tables **derive from the declaration**; they are not kept in parallel with a parity test.
- Declaration shape is the **capability set (option A)**; the per-provider refusal hook is deliberately omitted under YAGNI.

---

## File Structure

**Created:**
- `src/kinoforge/core/capabilities.py` — `Capability`, `WorkloadShape`, `capabilities_for()`, `provider_billed()`. No kinoforge imports at module level, so `core/interfaces.py` can import it freely.
- `src/kinoforge/validation/checks/capabilities.py` — `Gap`, `evaluate_capability_gaps()`, `ProviderCapabilityCheck`.
- `tests/core/test_capabilities.py`, `tests/core/test_capability_parity.py`, `tests/validation/test_capability_check.py`, `tests/core/test_capability_shape_launch.py`, `tests/core/test_reaper_capability_gate.py`, `tests/validation/test_shipped_configs_capabilities.py`, `tests/providers/test_pause_refusals.py`.

**Modified:**
- `src/kinoforge/core/interfaces.py:228-313` — `billed` ClassVar + `capabilities()` classmethod on `ComputeProvider`; narrow `set_heartbeat_endpoint`.
- `src/kinoforge/core/registry.py:33-59` — store provider classes; `register_provider` third argument.
- `src/kinoforge/providers/{local,runpod,skypilot,modal}/__init__.py` — declarations, registration call, `stop_instance` refusals.
- `src/kinoforge/core/{heartbeat_endpoints,util_endpoints,balance_endpoints}.py` — predicates derive from the declaration.
- `src/kinoforge/core/reaper.py:446-452` — capability-aware Row-7 gate.
- `src/kinoforge/core/orchestrator.py:827` — launch-time shape re-check.
- `src/kinoforge/cli/_commands.py:2381-2390` — `PAUSE_BILLING` pre-check for `kinoforge stop`.
- `examples/configs/skypilot-{gpu,cpu,lambda-comfyui}.yaml`, both `*-flashvsr-upscale.yaml`.
- `docs/{lifecycle,warm-reuse,extending}.md`.

---

## Task 0: Capability vocabulary and declaration surface

**Goal:** Ship the enum, the `ComputeProvider` declaration surface, and a by-name lookup that works in a process which never imported the providers.

**Files:**
- Create: `src/kinoforge/core/capabilities.py`
- Modify: `src/kinoforge/core/interfaces.py:228-232`, `src/kinoforge/core/registry.py:33-59`
- Test: `tests/core/test_capabilities.py`

**Acceptance Criteria:**
- [ ] `Capability` has exactly the eight members from the design doc §4; `WorkloadShape` has `SERVER` and `BATCH`.
- [ ] `ComputeProvider.capabilities()` defaults to `frozenset()` and `ComputeProvider.billed` defaults to `True`.
- [ ] `capabilities_for("nope")` returns an empty frozenset rather than raising.
- [ ] `capabilities_for` resolves a provider in a fresh interpreter that never imported `kinoforge.providers.*`.
- [ ] `core/registry.py` contains no import of a concrete adapter module.

**Verify:** `pixi run python -m pytest tests/core/test_capabilities.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_capabilities.py
"""Capability vocabulary + by-name lookup (Brief 2, Task 0)."""

from __future__ import annotations

import subprocess
import sys

from kinoforge.core.capabilities import (
    Capability,
    WorkloadShape,
    capabilities_for,
    provider_billed,
)
from kinoforge.core.interfaces import ComputeProvider


def test_capability_members_are_exactly_the_declared_eight() -> None:
    """Vocabulary is closed — a stray member means an undesigned capability."""
    assert {c.value for c in Capability} == {
        "HEARTBEAT_READ",
        "RUNTIME_PROBE",
        "UTIL_SNAPSHOT",
        "IDLE_AUTOSTOP",
        "ON_INSTANCE_DEADLINE",
        "JOB_TIMEOUT",
        "PAUSE_BILLING",
        "BALANCE_QUERY",
    }


def test_workload_shape_members() -> None:
    assert {s.value for s in WorkloadShape} == {"server", "batch"}


def test_abc_default_declares_nothing() -> None:
    """A provider that forgets to declare must claim nothing, not everything."""
    assert ComputeProvider.capabilities() == frozenset()
    assert ComputeProvider.capabilities(WorkloadShape.BATCH) == frozenset()
    assert ComputeProvider.billed is True


def test_unknown_provider_name_returns_empty_not_raises() -> None:
    """Matches the old `name not in frozenset` semantics for unknown kinds."""
    assert capabilities_for("nope") == frozenset()
    assert provider_billed("nope") is True


def test_lookup_resolves_in_a_process_that_never_imported_providers() -> None:
    """Catches the reaper answering 'nothing is supported' for every provider
    when the composition root was never imported — a silent, total gate
    disabling. Asserts the MECHANISM only: at Task 0 every provider still
    inherits the empty ABC default, so the capability CONTENT assertion
    belongs to Task 1.
    """
    code = (
        "from kinoforge.core import registry;"
        "assert registry.provider_class('runpod') is None, 'pre-imported';"
        "from kinoforge.core.capabilities import capabilities_for;"
        "caps = capabilities_for('runpod');"
        "print(registry.provider_class('runpod') is not None, isinstance(caps, frozenset))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "True True"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run python -m pytest tests/core/test_capabilities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.capabilities'`

- [ ] **Step 3: Create the capabilities module**

```python
# src/kinoforge/core/capabilities.py
"""Provider capability vocabulary + by-name lookup.

A provider declares what it can ACTUALLY enforce (design doc
docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md).
Every member below traces to a call site in some provider; nothing here is
aspirational.

This module deliberately imports nothing from kinoforge at module scope so
``core/interfaces.py`` can import it without a cycle. The registry and the
composition root are imported lazily inside :func:`capabilities_for`.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "Capability",
    "WorkloadShape",
    "capabilities_for",
    "provider_billed",
]


class Capability(StrEnum):
    """A guardrail a provider can enforce on its own.

    HEARTBEAT_READ       — ``last_heartbeat()`` returns a timestamp sourced
                           from the instance, not the orchestrator clock.
    RUNTIME_PROBE        — ``probe_runtime()`` returns a real RuntimeProbe.
    UTIL_SNAPSHOT        — a util endpoint with a wire path to UtilSnapshot.
    IDLE_AUTOSTOP        — the provider stops the instance after idleness.
    ON_INSTANCE_DEADLINE — something ON the instance kills it at a wall-clock
                           deadline, surviving the controller's death.
    JOB_TIMEOUT          — the provider enforces cfg's per-job timeout.
    PAUSE_BILLING        — ``stop_instance()`` pauses billing without destroying.
    BALANCE_QUERY        — live account balance readable from the provider.
    """

    HEARTBEAT_READ = "HEARTBEAT_READ"
    RUNTIME_PROBE = "RUNTIME_PROBE"
    UTIL_SNAPSHOT = "UTIL_SNAPSHOT"
    IDLE_AUTOSTOP = "IDLE_AUTOSTOP"
    ON_INSTANCE_DEADLINE = "ON_INSTANCE_DEADLINE"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    PAUSE_BILLING = "PAUSE_BILLING"
    BALANCE_QUERY = "BALANCE_QUERY"


class WorkloadShape(StrEnum):
    """Whether the deploy leaves a long-lived process on the instance.

    SERVER — ``spec.run_cmd`` non-empty; the job never terminates, so
             provider-side idle detection can never fire (verification doc
             F1, confirmed against skypilot 0.12.3.post1).
    BATCH  — ``spec.run_cmd`` empty; the provision script runs and exits, so
             the instance genuinely reaches idle.
    """

    SERVER = "server"
    BATCH = "batch"


_adapters_imported = False


def _ensure_adapters_imported() -> None:
    """Import the composition root once so providers self-register.

    ``kinoforge._adapters`` is the single module that imports all four
    provider packages. Importing it here (lazily, once) keeps a reaper or
    validation context that never touched providers answering the same as
    production instead of silently degrading to "nothing is supported".
    """
    global _adapters_imported
    if _adapters_imported:
        return
    _adapters_imported = True
    import kinoforge._adapters  # noqa: F401,PLC0415 — composition root, lazy by design


def _provider_class(provider_kind: str) -> type | None:
    """Return the registered provider class for ``provider_kind`` or None."""
    from kinoforge.core import registry  # noqa: PLC0415 — avoids an import cycle

    cls = registry.provider_class(provider_kind)
    if cls is None:
        _ensure_adapters_imported()
        cls = registry.provider_class(provider_kind)
    return cls


def capabilities_for(
    provider_kind: str,
    shape: WorkloadShape = WorkloadShape.SERVER,
) -> frozenset[Capability]:
    """Return the capabilities ``provider_kind`` declares at ``shape``.

    Args:
        provider_kind: Registry key, e.g. ``"skypilot"``.
        shape: Workload shape the declaration is evaluated at.

    Returns:
        The declared set, or an empty frozenset for an unknown provider —
        matching the ``name not in frozenset`` semantics of the string
        tables this replaces.
    """
    cls = _provider_class(provider_kind)
    if cls is None:
        return frozenset()
    caps: frozenset[Capability] = cls.capabilities(shape)
    return caps


def provider_billed(provider_kind: str) -> bool:
    """Return whether instances of ``provider_kind`` cost money.

    Unknown providers are assumed billed — the conservative direction, since
    the spend-risk validation rows are skipped only when this is False.
    """
    cls = _provider_class(provider_kind)
    if cls is None:
        return True
    return bool(cls.billed)
```

- [ ] **Step 4: Add the declaration surface to the ABC**

In `src/kinoforge/core/interfaces.py`, add to the imports:

```python
from typing import ClassVar

from kinoforge.core.capabilities import Capability, WorkloadShape
```

and insert into `class ComputeProvider(ABC)` immediately after `name: str` (`:231`):

```python
    #: False for providers that cost nothing (LocalProvider). Spend-risk
    #: validation rows are skipped for unbilled providers; liveness rows
    #: still apply.
    billed: ClassVar[bool] = True

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Return the guardrails this provider can itself enforce.

        Default is EMPTY on purpose: a provider that has not declared
        claims nothing, so config validation refuses it loudly instead of
        silently trusting it. Declaring is part of writing a provider.

        Args:
            shape: Workload shape. Only matters where a capability is
                genuinely shape-dependent (skypilot's IDLE_AUTOSTOP).

        Returns:
            The declared capability set.
        """
        return frozenset()
```

- [ ] **Step 5: Store provider classes in the registry**

In `src/kinoforge/core/registry.py`, replace the `_providers` declaration and `register_provider` (`:33`, `:42-49`):

```python
_providers: dict[str, Callable[[], ComputeProvider]] = {}
_provider_classes: dict[str, type[ComputeProvider]] = {}


def register_provider(
    name: str,
    factory: Callable[[], ComputeProvider],
    provider_cls: type[ComputeProvider],
) -> None:
    """Register a compute provider factory + class under ``name`` (overwrites).

    The class is stored alongside the factory so capability lookups can be
    answered by name without constructing a provider — the reaper must be
    able to ask in a process that cannot reach the provider at all.

    Args:
        name: The registry key for this provider.
        factory: Zero-arg callable that returns a ``ComputeProvider`` instance.
        provider_cls: The class itself, for class-level capability lookup.
    """
    _providers[name] = factory
    _provider_classes[name] = provider_cls


def provider_class(name: str) -> type[ComputeProvider] | None:
    """Return the registered class for ``name``, or None if unregistered.

    Unlike :func:`get_provider` this never raises — callers (capability
    lookup) treat an unknown provider as "declares nothing".
    """
    return _provider_classes.get(name)
```

- [ ] **Step 6: Update the four registration call sites**

```python
# providers/local/__init__.py:207
registry.register_provider("local", lambda: LocalProvider(), LocalProvider)

# providers/runpod/__init__.py:1367
registry.register_provider("runpod", _default_factory, RunPodProvider)

# providers/skypilot/__init__.py:1075
registry.register_provider("skypilot", lambda: SkyPilotProvider(), SkyPilotProvider)

# providers/modal/__init__.py:383
registry.register_provider("modal", lambda: ModalProvider(), ModalProvider)
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `pixi run python -m pytest tests/core/test_capabilities.py -v`
Expected: PASS (5 tests)

- [ ] **Step 8: Run the full suite for registration-signature fallout**

Run: `pixi run python -m pytest tests/ -x -q`
Expected: PASS. Any test calling `register_provider` with two arguments must be updated to pass the class.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/core/capabilities.py src/kinoforge/core/interfaces.py \
        src/kinoforge/core/registry.py src/kinoforge/providers tests/core/test_capabilities.py
git commit -m "feat(core): add the provider capability vocabulary and lookup"
```

---

## Task 1: Provider declarations and the parity guard

**Goal:** Each of the four providers declares its real capabilities, and a table-driven test fails when a no-op override appears without a declaration edit.

**Files:**
- Modify: `src/kinoforge/providers/{local,runpod,skypilot,modal}/__init__.py`
- Test: `tests/core/test_capability_parity.py`

**Acceptance Criteria:**
- [ ] Declared matrix matches design doc §7.1 exactly, including `skypilot` gaining `IDLE_AUTOSTOP` only at `WorkloadShape.BATCH`.
- [ ] `LocalProvider.billed is False`; the other three are `True`.
- [ ] Parity test derives expectations from method identity / wire behaviour, never from a second hardcoded capability list.
- [ ] A synthetic provider that declares `RUNTIME_PROBE` without overriding it makes the parity assertion fail.
- [ ] Declared capabilities resolve through the lazy composition-root import (fresh interpreter: runpod has `HEARTBEAT_READ`, skypilot does not).

**Verify:** `pixi run python -m pytest tests/core/test_capability_parity.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing parity tests**

```python
# tests/core/test_capability_parity.py
"""Declaration <-> implementation parity (Brief 2, Task 1).

The regression guard that matters: adding a no-op override, or deleting a
real implementation, without editing the declaration must fail here.
"""

from __future__ import annotations

import pytest

from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.interfaces import ComputeProvider
from kinoforge.providers.local import LocalProvider
from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.skypilot import SkyPilotProvider

PROVIDERS = [LocalProvider, ModalProvider, RunPodProvider, SkyPilotProvider]


def _overrides(cls: type, method: str) -> bool:
    """True iff ``cls`` overrides ``ComputeProvider.<method>``."""
    return getattr(cls, method) is not getattr(ComputeProvider, method)


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_heartbeat_read_declared_iff_last_heartbeat_overridden(cls: type) -> None:
    """Catches a `return None` override added, or a real read deleted,
    without editing the declaration."""
    declared = Capability.HEARTBEAT_READ in cls.capabilities()
    assert declared is _overrides(cls, "last_heartbeat")


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda c: c.__name__)
def test_runtime_probe_declared_iff_probe_runtime_overridden(cls: type) -> None:
    """Same guard for the sweeper's probe substrate."""
    declared = Capability.RUNTIME_PROBE in cls.capabilities()
    assert declared is _overrides(cls, "probe_runtime")


def test_declared_matrix_matches_the_design_doc() -> None:
    """Pins the whole §7.1 matrix so a silent widening is caught even where
    no ABC method exists to compare against."""
    assert LocalProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.UTIL_SNAPSHOT,
            Capability.PAUSE_BILLING,
        }
    )
    assert RunPodProvider.capabilities() == frozenset(
        {
            Capability.HEARTBEAT_READ,
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.ON_INSTANCE_DEADLINE,
            Capability.JOB_TIMEOUT,
            Capability.PAUSE_BILLING,
            Capability.BALANCE_QUERY,
        }
    )
    assert ModalProvider.capabilities() == frozenset(
        {
            Capability.RUNTIME_PROBE,
            Capability.UTIL_SNAPSHOT,
            Capability.IDLE_AUTOSTOP,
            Capability.ON_INSTANCE_DEADLINE,
        }
    )
    assert SkyPilotProvider.capabilities() == frozenset(
        {Capability.ON_INSTANCE_DEADLINE}
    )


def test_skypilot_idle_autostop_is_batch_only() -> None:
    """Catches a single-boolean regression: autostop is inert for server
    specs (F1) and real for batch specs (run_cmd=[])."""
    server = SkyPilotProvider.capabilities(WorkloadShape.SERVER)
    batch = SkyPilotProvider.capabilities(WorkloadShape.BATCH)
    assert Capability.IDLE_AUTOSTOP not in server
    assert Capability.IDLE_AUTOSTOP in batch


def test_declared_capabilities_survive_a_lazy_composition_root_import() -> None:
    """The Task 0 lookup test pinned the mechanism; this pins the CONTENT
    through the same lazy path. Catches a declaration that only resolves
    when something else has already imported the providers."""
    import subprocess
    import sys

    code = (
        "from kinoforge.core.capabilities import Capability, capabilities_for;"
        "print(Capability.HEARTBEAT_READ in capabilities_for('runpod'),"
        " Capability.HEARTBEAT_READ in capabilities_for('skypilot'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "True False"


def test_billed_flags() -> None:
    """Catches local being treated as a billed provider, which would error
    every local config on the spend rows."""
    assert LocalProvider.billed is False
    assert all(c.billed is True for c in (RunPodProvider, ModalProvider, SkyPilotProvider))


def test_parity_check_catches_a_declaration_without_an_implementation() -> None:
    """The parity test must not pass vacuously."""

    class LyingProvider(ComputeProvider):  # type: ignore[misc]
        @classmethod
        def capabilities(
            cls, shape: WorkloadShape = WorkloadShape.SERVER
        ) -> frozenset[Capability]:
            return frozenset({Capability.RUNTIME_PROBE})

    declared = Capability.RUNTIME_PROBE in LyingProvider.capabilities()
    assert declared is True
    assert _overrides(LyingProvider, "probe_runtime") is False
    # Parity would be violated -> the real parametrized test would fail.
    assert declared is not _overrides(LyingProvider, "probe_runtime")
```

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/core/test_capability_parity.py -v`
Expected: FAIL — every provider currently returns the empty ABC default.

- [ ] **Step 3: Declare capabilities on each provider**

```python
# providers/local/__init__.py — inside class LocalProvider
    billed: ClassVar[bool] = False

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """In-process provider: real heartbeat dict, real stop, scripted util.

        UTIL_SNAPSHOT is ``providers/local/util.py`` — a scripted in-process
        seam, not a measurement. It stays declared because the endpoint does
        return snapshots and LocalProvider is unbilled, so no money decision
        rides on it.
        """
        return frozenset(
            {
                Capability.HEARTBEAT_READ,
                Capability.UTIL_SNAPSHOT,
                Capability.PAUSE_BILLING,
            }
        )
```

```python
# providers/runpod/__init__.py — inside class RunPodProvider
    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Richest provider. Deliberately NOT IDLE_AUTOSTOP.

        ``selfterm.py`` is a boot-relative money cap (audit B4, 67627cd0), not
        idle detection — RunPod idle reaping is controller-side only, which the
        design doc rules out as risk coverage.
        """
        return frozenset(
            {
                Capability.HEARTBEAT_READ,
                Capability.RUNTIME_PROBE,
                Capability.UTIL_SNAPSHOT,
                Capability.ON_INSTANCE_DEADLINE,
                Capability.JOB_TIMEOUT,
                Capability.PAUSE_BILLING,
                Capability.BALANCE_QUERY,
            }
        )
```

```python
# providers/skypilot/__init__.py — inside class SkyPilotProvider
    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Instance-side deadline always; autostop only for batch specs.

        ON_INSTANCE_DEADLINE is the watchdog armed at the top of ``Task.setup``
        (providers/skypilot/watchdog.py). IDLE_AUTOSTOP holds only at BATCH:
        a server spec's ``run_cmd`` becomes a never-terminating ``Task.run``,
        so ``job_lib.is_cluster_idle()`` is permanently False (verification
        doc F1) and the 60 s AutostopEvent tick resets the timer forever.
        """
        caps = {Capability.ON_INSTANCE_DEADLINE}
        if shape is WorkloadShape.BATCH:
            caps.add(Capability.IDLE_AUTOSTOP)
        return frozenset(caps)
```

```python
# providers/modal/__init__.py — inside class ModalProvider
    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        """Container-level idle scaledown + a function timeout deadline.

        IDLE_AUTOSTOP is ``_app.py`` ``scaledown_window`` (default 300 s);
        ON_INSTANCE_DEADLINE is the ``@app.function(timeout=...)`` cap. Modal
        does NOT honour cfg's ``lifecycle.job_timeout``, so JOB_TIMEOUT is
        absent, and there is no wire-level heartbeat read.
        """
        return frozenset(
            {
                Capability.RUNTIME_PROBE,
                Capability.UTIL_SNAPSHOT,
                Capability.IDLE_AUTOSTOP,
                Capability.ON_INSTANCE_DEADLINE,
            }
        )
```

Each provider module needs `from typing import ClassVar` (local only) and
`from kinoforge.core.capabilities import Capability, WorkloadShape`.

- [ ] **Step 4: Run to verify pass**

Run: `pixi run python -m pytest tests/core/test_capability_parity.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/providers tests/core/test_capability_parity.py
git commit -m "feat(providers): declare per-provider capabilities with a parity guard"
```

---

## Task 2: Derive the existing support tables

**Goal:** `_HEARTBEAT_SUPPORTED`, `_UTIL_SUPPORTED` and the balance predicate stop being independent string sets and become derivations of the declaration.

**Files:**
- Modify: `src/kinoforge/core/heartbeat_endpoints.py:81-109`, `src/kinoforge/core/util_endpoints.py:66-78`, `src/kinoforge/core/balance_endpoints.py:91-111`
- Test: `tests/core/test_capability_parity.py` (extend)

**Acceptance Criteria:**
- [ ] The three module-level frozensets are deleted; the predicates read `capabilities_for`.
- [ ] Predicate answers for every registered provider are unchanged from the pre-change values: heartbeat `{local, runpod}`, util `{local, modal, runpod}`, balance `{runpod}`.
- [ ] Call sites at `reaper.py:178/239/448`, `_adapters.py:280`, `cli/_commands.py:3020` are untouched.

**Verify:** `pixi run python -m pytest tests/core/ tests/providers/ -q` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/core/test_capability_parity.py`)

```python
from kinoforge.core.balance_endpoints import provider_balance_supported
from kinoforge.core.capabilities import capabilities_for
from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
from kinoforge.core.util_endpoints import provider_util_supported

REGISTERED = ["local", "runpod", "skypilot", "modal"]


@pytest.mark.parametrize("name", REGISTERED)
def test_heartbeat_predicate_equals_the_declaration(name: str) -> None:
    """Catches the derivation diverging from the declaration it replaced."""
    assert provider_heartbeat_supported(name) is (
        Capability.HEARTBEAT_READ in capabilities_for(name)
    )


@pytest.mark.parametrize("name", REGISTERED)
def test_util_predicate_equals_the_declaration(name: str) -> None:
    assert provider_util_supported(name) is (
        Capability.UTIL_SNAPSHOT in capabilities_for(name)
    )


def test_predicate_answers_are_unchanged_from_the_string_tables() -> None:
    """Pins the pre-change behaviour: a derivation that silently widens or
    narrows support would change reaper verdicts on live rows."""
    assert {n for n in REGISTERED if provider_heartbeat_supported(n)} == {
        "local",
        "runpod",
    }
    assert {n for n in REGISTERED if provider_util_supported(n)} == {
        "local",
        "modal",
        "runpod",
    }
    assert {n for n in REGISTERED if provider_balance_supported(n)} == {"runpod"}
```

If `core/balance_endpoints.py` exposes its predicate under a different name, use
the existing name and keep this test's import in sync — do not rename the
public function in this task.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/core/test_capability_parity.py -k predicate -v`
Expected: FAIL — predicates still read the frozensets, so skypilot/modal answers disagree with the declaration for at least one axis.

- [ ] **Step 3: Derive the predicates**

```python
# core/heartbeat_endpoints.py — delete _HEARTBEAT_SUPPORTED, replace the predicate
def provider_heartbeat_supported(provider_kind: str) -> bool:
    """Return True iff ``provider_kind`` declares HEARTBEAT_READ.

    Derived from the provider's own declaration (Brief 2) rather than a
    string table, so a provider whose ``last_heartbeat`` is the inherited
    ``None`` default cannot be listed as supported.
    """
    from kinoforge.core.capabilities import (  # noqa: PLC0415 — avoids an import cycle
        Capability,
        capabilities_for,
    )

    return Capability.HEARTBEAT_READ in capabilities_for(provider_kind)
```

Apply the same shape to `provider_util_supported` (`Capability.UTIL_SNAPSHOT`) and the balance predicate (`Capability.BALANCE_QUERY`), deleting `_UTIL_SUPPORTED` and `_SUPPORTED`. Update each module docstring to say the source of truth is now `core/capabilities.py`.

- [ ] **Step 4: Run to verify pass**

Run: `pixi run python -m pytest tests/core/ tests/providers/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core tests/core/test_capability_parity.py
git commit -m "refactor(core): derive the provider support tables from capabilities"
```

---

## Task 3: Honest refusals for pause and endpoint installation

**Goal:** `kinoforge stop` stops lying — skypilot no longer reports success while the cluster bills, modal no longer destroys when asked to pause — and a heartbeat endpoint can no longer be silently discarded.

**Files:**
- Modify: `src/kinoforge/providers/skypilot/__init__.py:994-1003`, `src/kinoforge/providers/modal/__init__.py:229-231`, `src/kinoforge/core/interfaces.py:290-310`, `src/kinoforge/cli/_commands.py:2381-2390`
- Test: `tests/providers/test_pause_refusals.py`

**Acceptance Criteria:**
- [ ] `SkyPilotProvider.stop_instance` and `ModalProvider.stop_instance` raise `NotImplementedError` naming `destroy`.
- [ ] `kinoforge stop --id` on a skypilot ledger row exits non-zero, prints a routed message, and makes zero destroy calls.
- [ ] `set_heartbeat_endpoint(<non-None>)` on a provider without `HEARTBEAT_READ` raises `ValueError`; `set_heartbeat_endpoint(None)` still returns cleanly.

**Verify:** `pixi run python -m pytest tests/providers/test_pause_refusals.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/test_pause_refusals.py
"""Pause + endpoint-install refusals (Brief 2, Task 3)."""

from __future__ import annotations

import pytest

from kinoforge.providers.modal import ModalProvider
from kinoforge.providers.skypilot import SkyPilotProvider


def test_skypilot_stop_refuses_instead_of_silently_doing_nothing() -> None:
    """Catches the current lie: `kinoforge stop` reports success while the
    cluster keeps billing."""
    with pytest.raises(NotImplementedError, match="destroy"):
        SkyPilotProvider().stop_instance("kf-cluster")


def test_modal_stop_refuses_instead_of_destroying() -> None:
    """Catches pause-means-destroy: asking to pause must not lose the app
    and the warm container."""
    with pytest.raises(NotImplementedError, match="destroy"):
        ModalProvider().stop_instance("eph-abc123")


def test_set_heartbeat_endpoint_rejects_a_wired_endpoint_it_would_discard() -> None:
    """Catches the uniform install path silently dropping an endpoint that
    someone deliberately built."""
    provider = SkyPilotProvider()
    with pytest.raises(ValueError, match="HEARTBEAT_READ"):
        provider.set_heartbeat_endpoint(object())


def test_set_heartbeat_endpoint_none_still_passes() -> None:
    """The clear path stays a no-op — it discards nothing."""
    assert SkyPilotProvider().set_heartbeat_endpoint(None) is None
```

And the CLI test (same file):

```python
def test_cli_stop_on_skypilot_row_refuses_without_destroying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a pre-check that falls through and turns a pause request into
    a teardown."""
    from kinoforge.cli import _commands

    calls: list[str] = []

    class _FakeProvider:
        name = "skypilot"

        @classmethod
        def capabilities(cls, shape=None):  # noqa: ANN001, ANN206
            from kinoforge.core.capabilities import Capability

            return frozenset({Capability.ON_INSTANCE_DEADLINE})

        def stop_instance(self, instance_id: str) -> None:
            calls.append(f"stop:{instance_id}")
            raise NotImplementedError("use destroy")

        def destroy_instance(self, instance_id: str) -> None:
            calls.append(f"destroy:{instance_id}")

    monkeypatch.setattr(
        _commands.registry, "get_provider", lambda name: _FakeProvider
    )
    monkeypatch.setattr(
        _commands.registry, "provider_class", lambda name: _FakeProvider
    )
    rc = _commands.cmd_stop(_stop_ctx_with_skypilot_row(), _stop_args("kf-cluster"))
    assert rc != 0
    assert calls == []
```

Implement `_stop_ctx_with_skypilot_row()` / `_stop_args()` in the test module using the same ledger + `SessionContext` fixtures the existing `tests/cli/` stop tests use; match the current `cmd_stop` entry point name found at `cli/_commands.py:2374`.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/providers/test_pause_refusals.py -v`
Expected: FAIL — skypilot returns None, modal destroys, `set_heartbeat_endpoint` passes.

- [ ] **Step 3: Implement the refusals**

```python
# providers/skypilot/__init__.py — replace stop_instance
    def stop_instance(self, instance_id: str) -> None:
        """Refuse: SkyPilot has no pause-billing primitive.

        A cluster is either UP or torn down. This used to be a silent no-op,
        so ``kinoforge stop --id`` reported success while the cluster kept
        billing. PAUSE_BILLING is not declared; the honest answer is a
        refusal that names the operation that does work.

        Args:
            instance_id: The cluster name the caller wanted paused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            f"skypilot cannot pause billing for {instance_id!r}; "
            f"use `kinoforge destroy --id {instance_id}` to tear the cluster down"
        )
```

```python
# providers/modal/__init__.py — replace stop_instance
    def stop_instance(self, instance_id: str) -> None:
        """Refuse: stopping a Modal app destroys it.

        This used to alias ``destroy_instance``, so a pause request silently
        destroyed the app and threw away the warm container.

        Args:
            instance_id: The run-id the caller wanted paused.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            f"modal cannot pause billing for {instance_id!r}; stopping an app "
            f"destroys it — use `kinoforge destroy --id {instance_id}` if that "
            f"is what you want"
        )
```

```python
# core/interfaces.py — narrow set_heartbeat_endpoint
    def set_heartbeat_endpoint(self, endpoint: object | None) -> None:
        """Install a HeartbeatEndpoint post-construction (B5a).

        Providers that declare HEARTBEAT_READ override this to wire the
        endpoint. The default accepts ``None`` (the clear path) and REJECTS a
        real endpoint: silently discarding one that a caller built is a
        wiring bug, not a capability gap.

        Args:
            endpoint: A HeartbeatEndpoint-Protocol instance, or None to clear.

        Raises:
            ValueError: ``endpoint`` is not None on a provider that does not
                declare ``Capability.HEARTBEAT_READ``.
        """
        if endpoint is None:
            return
        raise ValueError(
            f"{type(self).__name__} does not declare Capability.HEARTBEAT_READ; "
            f"refusing to silently discard the endpoint {endpoint!r}"
        )
```

- [ ] **Step 4: Add the CLI pre-check**

In `cli/_commands.py`, immediately after the provider is resolved at `:2385`:

```python
        from kinoforge.core.capabilities import Capability, capabilities_for

        if Capability.PAUSE_BILLING not in capabilities_for(str(provider_name)):
            print(
                f"{provider_name} cannot pause billing; instances are either "
                f"running or destroyed.\n"
                f"  To tear it down:  kinoforge destroy --id {args.id}",
                file=sys.stderr,
            )
            return 1
        provider.stop_instance(args.id)
```

- [ ] **Step 5: Run to verify pass**

Run: `pixi run python -m pytest tests/providers/test_pause_refusals.py tests/cli -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge tests/providers/test_pause_refusals.py
git commit -m "fix(providers): refuse pause instead of no-oping or destroying"
```

---

## Task 4: Risk table and the capability validation check

**Goal:** A config that asks for a guardrail the selected provider cannot enforce produces a diagnostic at load — ERROR when nothing covers the risk, WARN naming the substitute when something does.

**Files:**
- Create: `src/kinoforge/validation/checks/capabilities.py`
- Modify: `src/kinoforge/validation/checks/__init__.py` (import for self-registration, matching the existing pattern)
- Test: `tests/validation/test_capability_check.py`

**Acceptance Criteria:**
- [ ] `evaluate_capability_gaps(cfg, shape)` returns a list of `Gap` records; the check formats them into one `CheckResult`.
- [ ] `max_lifetime` risk is evaluated whenever `cfg.compute` is present and the provider is billed; the other rows only when the field was explicitly set in the YAML (`model_fields_set`).
- [ ] A billed provider declaring neither `ON_INSTANCE_DEADLINE` nor a substitute makes `load_config` raise `ValidationError` naming both the capability and the provider.
- [ ] `auto_fix` returns `None` unconditionally, and `load_config` leaves guardrail values byte-identical.
- [ ] Unbilled providers skip the spend rows.

**Verify:** `pixi run python -m pytest tests/validation/test_capability_check.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/validation/test_capability_check.py
"""ProviderCapabilityCheck (Brief 2, Task 4)."""

from __future__ import annotations

import pytest

from kinoforge.core.capabilities import Capability, WorkloadShape
from kinoforge.core.config import load_config
from kinoforge.core.errors import ValidationError
from kinoforge.validation.checks.capabilities import (
    ProviderCapabilityCheck,
    evaluate_capability_gaps,
)
from kinoforge.validation.protocol import Severity


def test_heartbeat_gap_on_skypilot_is_a_warn_naming_the_clock(tmp_path) -> None:  # noqa: ANN001
    """Catches a severity mapping that errors on a covered risk, and a
    message that does not tell the operator what the signal really means."""
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    hb = [g for g in gaps if g.field == "compute.lifecycle.heartbeat_interval_s"]
    assert len(hb) == 1
    assert hb[0].severity is Severity.WARN
    assert hb[0].missing is Capability.HEARTBEAT_READ
    result = ProviderCapabilityCheck().run(cfg)
    assert result.passed is False
    assert result.severity is Severity.WARN
    assert "orchestrator clock" in result.message


def test_idle_timeout_gap_names_the_substitute_and_its_bound(tmp_path) -> None:  # noqa: ANN001
    """Catches a WARN that says 'unsupported' without telling the operator
    what actually bounds the run."""
    cfg = load_config(_write_cfg(tmp_path, provider="skypilot"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    idle = [g for g in gaps if g.field == "compute.lifecycle.idle_timeout"]
    assert idle[0].substitute is Capability.ON_INSTANCE_DEADLINE
    assert idle[0].severity is Severity.WARN


def test_uncovered_spend_risk_is_fatal(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """Catches a missing risk row letting an unbounded-spend config launch:
    a billed provider with no deadline and no autostop must not load."""
    from kinoforge.providers.skypilot import SkyPilotProvider

    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError) as exc:
        load_config(_write_cfg(tmp_path, provider="skypilot"))
    assert "ON_INSTANCE_DEADLINE" in str(exc.value)
    assert "skypilot" in str(exc.value)


def test_unbilled_provider_skips_the_spend_rows(tmp_path) -> None:  # noqa: ANN001
    """Catches `billed` being ignored, which would error every local run."""
    cfg = load_config(_write_cfg(tmp_path, provider="local"))
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert [g.field for g in gaps if "max_lifetime" in g.field] == []


def test_check_never_auto_fixes_and_load_preserves_guardrail_values(
    tmp_path,  # noqa: ANN001
) -> None:
    """Catches a future auto-fix that silences the diagnostic by rewriting
    the guardrail — how the current state became invisible."""
    path = _write_cfg(tmp_path, provider="skypilot")
    cfg = load_config(path)
    assert ProviderCapabilityCheck().auto_fix(cfg) is None
    assert cfg.compute.lifecycle.idle_timeout == 180.0
    assert cfg.compute.lifecycle.max_lifetime == 1800.0
```

`_write_cfg(tmp_path, provider=...)` writes a minimal valid cfg (engine `comfyui`, one model, `compute.provider`, `lifecycle` with `idle_timeout: 180`, `max_lifetime: 1800`, `budget: 0.10`, `heartbeat_interval_s: 30`) and returns the path. Copy the fixture style from the existing `tests/validation/` modules.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/validation/test_capability_check.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.validation.checks.capabilities`

- [ ] **Step 3: Implement the risk table and the check**

```python
# src/kinoforge/validation/checks/capabilities.py
"""ProviderCapabilityCheck — STATIC, no auto-fix.

Compares the guardrails a cfg asserts against what the selected provider
declares it can enforce (design doc
docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md).

Severity is risk-covered downgrade: ERROR when no declared capability bounds
the same risk, WARN naming the substitute when one does. Controller-side
reaping never counts as coverage — it requires an operator to remember it.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.capabilities import (
    Capability,
    WorkloadShape,
    capabilities_for,
    provider_billed,
)
from kinoforge.core.config import Config
from kinoforge.validation.protocol import CheckCategory, CheckResult, Severity
from kinoforge.validation.registry import register


@dataclass(frozen=True)
class Gap:
    """One guardrail the selected provider cannot enforce.

    Attributes:
        field: Dotted cfg path the operator wrote.
        risk: One-line description of what the guardrail prevents.
        missing: The capability that would enforce it.
        substitute: A declared capability that bounds the same risk, or None.
        severity: WARN when ``substitute`` is set, ERROR otherwise.
        detail: Operator-facing explanation, including the numeric bound the
            substitute actually enforces.
    """

    field: str
    risk: str
    missing: Capability
    substitute: Capability | None
    severity: Severity
    detail: str


#: field -> (risk, primary capability, accepted substitutes, spend_risk)
_RISK_ROWS: tuple[tuple[str, str, Capability, tuple[Capability, ...], bool], ...] = (
    (
        "compute.lifecycle.max_lifetime",
        "the instance outlives the controller",
        Capability.ON_INSTANCE_DEADLINE,
        (),
        True,
    ),
    (
        "compute.lifecycle.idle_timeout",
        "the instance is alive but doing nothing",
        Capability.IDLE_AUTOSTOP,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.job_timeout",
        "a single job runs away",
        Capability.JOB_TIMEOUT,
        (Capability.ON_INSTANCE_DEADLINE,),
        True,
    ),
    (
        "compute.lifecycle.heartbeat_interval_s",
        "the liveness signal is fiction",
        Capability.HEARTBEAT_READ,
        (),
        False,
    ),
    (
        "compute.lifecycle.stall_window_s",
        "the GPU is idle mid-job",
        Capability.UTIL_SNAPSHOT,
        (),
        False,
    ),
)

_ALWAYS_EVALUATED = frozenset({"compute.lifecycle.max_lifetime"})

_DETAIL: dict[Capability, str] = {
    Capability.HEARTBEAT_READ: (
        "no wire-level heartbeat read; last_heartbeat is the orchestrator "
        "clock, so it proves the controller is alive, not the instance"
    ),
    Capability.IDLE_AUTOSTOP: (
        "provider-side autostop does not fire for this workload shape"
    ),
    Capability.JOB_TIMEOUT: "the provider does not enforce a per-job timeout",
    Capability.UTIL_SNAPSHOT: "no utilisation wire path; stall detection cannot run",
    Capability.ON_INSTANCE_DEADLINE: (
        "nothing on the instance terminates it if the controller dies"
    ),
}


def evaluate_capability_gaps(cfg: Config, shape: WorkloadShape) -> list[Gap]:
    """Return every guardrail ``cfg`` asserts that its provider cannot enforce.

    Args:
        cfg: A loaded Config with a ``compute`` block.
        shape: Workload shape the declaration is evaluated at.

    Returns:
        Gaps in ``_RISK_ROWS`` order; empty when the provider covers
        everything the cfg asks for.
    """
    if cfg.compute is None:
        return []
    provider = cfg.compute.provider
    declared = capabilities_for(provider, shape)
    billed = provider_billed(provider)
    lifecycle = cfg.compute.lifecycle
    set_fields = set(lifecycle.model_fields_set) if lifecycle is not None else set()

    gaps: list[Gap] = []
    for field, risk, primary, substitutes, spend_risk in _RISK_ROWS:
        leaf = field.rsplit(".", 1)[1]
        asserted = field in _ALWAYS_EVALUATED or leaf in set_fields
        if not asserted:
            continue
        if spend_risk and not billed:
            continue
        if primary in declared:
            continue
        covering = next((s for s in substitutes if s in declared), None)
        gaps.append(
            Gap(
                field=field,
                risk=risk,
                missing=primary,
                substitute=covering,
                severity=Severity.WARN if covering else Severity.ERROR,
                detail=_DETAIL[primary]
                + (
                    f"; bounded instead by {covering.value}"
                    if covering is not None
                    else ""
                ),
            )
        )
    return gaps


def _infer_shape(cfg: Config) -> WorkloadShape:
    """Infer the workload shape from cfg (see Task 5 for the launch re-check).

    BATCH iff the rendered provision will carry ``run_cmd=[]`` — upscale-only
    diffusers cfgs and interpolate-only cfgs. Mirrors the upscale-only
    predicate already used in ``core/config.py``.
    """
    diffusers = cfg.engine.diffusers if cfg.engine is not None else None
    if diffusers is not None and diffusers.upscale_only:
        return WorkloadShape.BATCH
    if cfg.interpolate is not None and not cfg.models:
        return WorkloadShape.BATCH
    return WorkloadShape.SERVER


class ProviderCapabilityCheck:
    """STATIC — report guardrails the selected provider cannot enforce."""

    name: str = "provider_capabilities"
    category: CheckCategory = CheckCategory.STATIC
    severity: Severity = Severity.WARN

    def applies_to(self, cfg: Config) -> bool:
        """Apply to any cfg with a compute block."""
        return cfg.compute is not None

    def run(self, cfg: Config) -> CheckResult:
        """Aggregate every gap into one result at the highest severity found."""
        gaps = evaluate_capability_gaps(cfg, _infer_shape(cfg))
        if not gaps:
            return CheckResult(
                name=self.name,
                passed=True,
                severity=Severity.WARN,
                message=(
                    f"{cfg.compute.provider if cfg.compute else '?'} enforces "
                    f"every guardrail this cfg asserts"
                ),
            )
        worst = (
            Severity.ERROR
            if any(g.severity is Severity.ERROR for g in gaps)
            else Severity.WARN
        )
        provider = cfg.compute.provider if cfg.compute is not None else "?"
        lines = [
            f"{g.field}: {provider} cannot enforce {g.missing.value} "
            f"({g.risk}) — {g.detail}"
            for g in gaps
        ]
        return CheckResult(
            name=self.name,
            passed=False,
            severity=worst,
            message="\n  ".join([f"{provider} guardrail gaps:", *lines]),
            fix_suggestion=(
                "drop the guardrail, or select a provider that declares it "
                "(see docs/lifecycle.md for the capability matrix)"
            ),
        )

    def auto_fix(self, cfg: Config) -> Config | None:
        """Never auto-fix — silently rewriting a guardrail is the bug."""
        return None


register(ProviderCapabilityCheck())
```

- [ ] **Step 4: Run to verify pass**

Run: `pixi run python -m pytest tests/validation/test_capability_check.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite for load-path fallout**

Run: `pixi run python -m pytest tests/ -q`
Expected: PASS — WARNs are logged, not raised. Any test asserting an exact set of validation warnings needs the new one added.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/validation tests/validation/test_capability_check.py
git commit -m "feat(validation): report guardrails the selected provider cannot enforce"
```

---

## Task 5: Launch-time shape re-check

**Goal:** The authoritative shape comes from `spec.run_cmd` at launch, so a wrong load-time inference is caught instead of trusted.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:827-848`
- Test: `tests/core/test_capability_shape_launch.py`

**Acceptance Criteria:**
- [ ] `deploy_session` re-evaluates gaps with `WorkloadShape.BATCH` iff `spec.run_cmd` is empty, before `create_instance` is reached.
- [ ] An ERROR-severity gap at launch raises before `create_instance` is called.
- [ ] A load-time/launch-time disagreement logs an inference-miss line naming both shapes.
- [ ] The bare `deploy()` path at `:1613` is untouched (out of scope, matching Brief 1).

**Verify:** `pixi run python -m pytest tests/core/test_capability_shape_launch.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_capability_shape_launch.py
"""Launch-time shape re-check (Brief 2, Task 5)."""

from __future__ import annotations

import logging

import pytest

from kinoforge.core.capabilities import WorkloadShape
from kinoforge.core.orchestrator import assert_launch_capabilities


def test_shape_comes_from_the_spec_not_the_cfg(minimal_skypilot_cfg) -> None:  # noqa: ANN001
    """Catches a re-check that re-reads cfg and therefore can never catch an
    inference miss: cfg infers SERVER, the spec says batch."""
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("t")
    )
    assert all(g.field != "compute.lifecycle.idle_timeout" for g in gaps)


def test_server_spec_still_reports_the_idle_gap(minimal_skypilot_cfg) -> None:  # noqa: ANN001
    gaps = assert_launch_capabilities(
        minimal_skypilot_cfg,
        run_cmd=["python", "-m", "server"],
        logger=logging.getLogger("t"),
    )
    assert any(g.field == "compute.lifecycle.idle_timeout" for g in gaps)


def test_inference_miss_is_logged_not_swallowed(
    minimal_skypilot_cfg,  # noqa: ANN001
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches a silent correction — the derivation must get fixed, not drift."""
    with caplog.at_level(logging.WARNING):
        assert_launch_capabilities(
            minimal_skypilot_cfg, run_cmd=[], logger=logging.getLogger("kinoforge")
        )
    assert any("shape inference miss" in r.message for r in caplog.records)


def test_error_gap_raises_before_create(minimal_skypilot_cfg, monkeypatch) -> None:  # noqa: ANN001
    """Catches an ERROR gap being logged instead of aborting the launch."""
    from kinoforge.core.errors import ValidationError
    from kinoforge.providers.skypilot import SkyPilotProvider

    monkeypatch.setattr(
        SkyPilotProvider,
        "capabilities",
        classmethod(lambda cls, shape=WorkloadShape.SERVER: frozenset()),
    )
    with pytest.raises(ValidationError, match="ON_INSTANCE_DEADLINE"):
        assert_launch_capabilities(
            minimal_skypilot_cfg,
            run_cmd=["python", "-m", "server"],
            logger=logging.getLogger("t"),
        )
```

Add a `minimal_skypilot_cfg` fixture to `tests/core/conftest.py` returning the same loaded cfg used in Task 4.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/core/test_capability_shape_launch.py -v`
Expected: FAIL — `ImportError: cannot import name 'assert_launch_capabilities'`

- [ ] **Step 3: Implement the launch-time re-check**

Add to `src/kinoforge/core/orchestrator.py`:

```python
def assert_launch_capabilities(
    cfg: Config,
    *,
    run_cmd: Sequence[str] | None,
    logger: logging.Logger = _log,
) -> list[Gap]:
    """Re-evaluate capability gaps against the authoritative workload shape.

    Load-time inference reads engine kind + the upscale/interpolate-only
    path; here ``run_cmd`` is the real thing, so a wrong inference is caught
    rather than trusted.

    Args:
        cfg: The loaded Config.
        run_cmd: The rendered provision's run command. Empty/None -> BATCH.
        logger: Injected for testability.

    Returns:
        The gaps at the authoritative shape (WARN-severity ones only; ERROR
        gaps raise).

    Raises:
        ValidationError: A guardrail the cfg asserts has no declared
            capability and no substitute at the authoritative shape.
    """
    shape = WorkloadShape.BATCH if not run_cmd else WorkloadShape.SERVER
    inferred = _infer_shape(cfg)
    if inferred is not shape:
        logger.warning(
            "[capabilities] shape inference miss: load-time inferred %s, "
            "spec.run_cmd says %s — the launch-time shape wins",
            inferred.value,
            shape.value,
        )
    gaps = evaluate_capability_gaps(cfg, shape)
    fatal = [g for g in gaps if g.severity is Severity.ERROR]
    if fatal:
        detail = "; ".join(f"{g.field} needs {g.missing.value}" for g in fatal)
        raise ValidationError(
            f"{cfg.compute.provider if cfg.compute else '?'} cannot enforce "
            f"guardrails this cfg asserts at shape={shape.value}: {detail}"
        )
    for gap in gaps:
        logger.warning("[capabilities] %s: %s", gap.field, gap.detail)
    return gaps
```

Import `Gap`, `evaluate_capability_gaps` and `_infer_shape` from
`kinoforge.validation.checks.capabilities` inside the function body if a
module-level import would create a cycle.

Then call it from the `_build_spec` closure at `:827`, immediately before the
`return InstanceSpec(...)`:

```python
        assert_launch_capabilities(cfg, run_cmd=rendered.run_cmd)
```

- [ ] **Step 4: Run to verify pass**

Run: `pixi run python -m pytest tests/core/test_capability_shape_launch.py tests/core/test_orchestrator*.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core
git commit -m "feat(orchestrator): re-check capability gaps against the launch shape"
```

---

## Task 6: Capability-aware reaper gate

**Goal:** An expected heartbeat absence stops masking the age and grace evidence that never depended on heartbeat.

**Files:**
- Modify: `src/kinoforge/core/reaper.py:432-452`
- Test: `tests/core/test_reaper_capability_gate.py`

**Acceptance Criteria:**
- [ ] Provider without `HEARTBEAT_READ` + missing heartbeat fields + past grace → `ORPHAN_REAP`.
- [ ] Same row within grace → `HEARTBEAT_SUBSTRATE_MISSING` (unchanged value and name).
- [ ] Provider with `HEARTBEAT_READ` + missing fields → `HEARTBEAT_UNKNOWN`.
- [ ] Row past `max_lifetime` still returns `OVERAGE_REAP`.
- [ ] `DEFAULT_APPLY_POLICY` still excludes `ORPHAN_REAP`.

**Verify:** `pixi run python -m pytest tests/core/test_reaper_capability_gate.py tests/core/test_reaper.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_reaper_capability_gate.py
"""Capability-aware Row-7 gate (Brief 2, Task 6)."""

from __future__ import annotations

from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict, classify, partition

NOW = 1_800_000_000.0
GRACE = 1800.0


def _row(provider: str, *, session_end_age: float, pod_age: float = 600.0) -> dict:
    """Ledger row with NO heartbeat fields (written outside a heartbeat loop)."""
    return {
        "id": "i-1",
        "provider": provider,
        "created_at": NOW - pod_age,
        "session_end": NOW - session_end_age,
        "grace_after_session_s": GRACE,
    }


def test_expected_absence_past_grace_reaps_as_orphan() -> None:
    """Catches the dead-end verdict: a row with no heartbeat substrate must
    still be judged on the age evidence it does have."""
    verdict = classify(_row("skypilot", session_end_age=GRACE + 1), now=NOW)
    assert verdict is Verdict.ORPHAN_REAP


def test_expected_absence_within_grace_is_unchanged() -> None:
    """Boundary partner — inside grace the row is not an orphan."""
    verdict = classify(_row("skypilot", session_end_age=GRACE - 1), now=NOW)
    assert verdict is Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_unexpected_absence_stays_strict() -> None:
    """Provider DECLARES a heartbeat read and the row has none — a real
    anomaly, not an expected gap. Catches an inverted capability check."""
    verdict = classify(_row("runpod", session_end_age=GRACE + 1), now=NOW)
    assert verdict is Verdict.HEARTBEAT_UNKNOWN


def test_overage_still_precedes_the_gate() -> None:
    """Catches the fall-through being inserted above the age check."""
    row = _row("skypilot", session_end_age=1.0, pod_age=100_000.0)
    row["max_lifetime_s"] = 3600.0
    assert classify(row, now=NOW) is Verdict.OVERAGE_REAP


def test_default_policy_does_not_act_on_the_new_orphans() -> None:
    """Catches ORPHAN_REAP being added to the default policy while wiring
    this, which would silently make the change destructive."""
    to_act, to_skip = partition({"i-1": Verdict.ORPHAN_REAP}, DEFAULT_APPLY_POLICY)
    assert to_act == {}
    assert to_skip == {"i-1": Verdict.ORPHAN_REAP}
```

Match `classify`'s real signature (keyword defaults for `idle_timeout_s`, `max_lifetime_s`, `grace_after_session_s`, `heartbeat_interval_s`) as used by the existing `tests/core/test_reaper.py`.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/core/test_reaper_capability_gate.py -v`
Expected: FAIL — the past-grace skypilot row returns `HEARTBEAT_SUBSTRATE_MISSING`.

- [ ] **Step 3: Implement the gate change**

Replace `core/reaper.py:446-452` with:

```python
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        provider_kind = entry.get("provider_kind") or entry.get("provider")
        expected_absence = provider_kind is not None and not (
            provider_heartbeat_supported(str(provider_kind))
        )
        if not expected_absence:
            # The provider DECLARES a heartbeat read and the row has none —
            # a real anomaly, kept strict and non-destructive.
            return Verdict.HEARTBEAT_UNKNOWN
        # Expected absence (the provider never had HEARTBEAT_READ). Do NOT
        # return here: the age + grace evidence below never depended on
        # heartbeat, and short-circuiting it is what made these rows a dead
        # end. Falls through to the grace evaluation; within grace the row
        # still classifies HEARTBEAT_SUBSTRATE_MISSING, past grace it becomes
        # ORPHAN_REAP (still not in DEFAULT_APPLY_POLICY).
        session_end = entry.get("session_end")
        if session_end is None:
            time_since_drive = pod_age
        else:
            time_since_drive = now - max(created_at, float(session_end))
        if time_since_drive > grace:
            return Verdict.ORPHAN_REAP
        return Verdict.HEARTBEAT_SUBSTRATE_MISSING
```

Update the `Verdict.HEARTBEAT_SUBSTRATE_MISSING` docstring/comment at `:39` to
say "expected absence, still inside grace", and note the name is retained
because the value is a serialized public contract.

- [ ] **Step 4: Run to verify pass**

Run: `pixi run python -m pytest tests/core/test_reaper_capability_gate.py tests/core/test_reaper.py tests/core/test_reaper_actor.py -q`
Expected: PASS. Existing tests asserting `HEARTBEAT_SUBSTRATE_MISSING` for a past-grace row must be updated to `ORPHAN_REAP` with a comment pointing at this task.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/reaper.py tests/core
git commit -m "fix(reaper): stop an expected heartbeat gap from masking age verdicts"
```

---

## Task 7: Configs, docs, and the shipped-config guard

**Goal:** The shipped configs stop asserting guardrails that do not apply, say what actually protects the run, and a test pins the exact expected diagnostics.

**Files:**
- Modify: `examples/configs/skypilot-gpu.yaml:8,49,51,54`, `examples/configs/skypilot-cpu.yaml:7-8,47-50,59`, `examples/configs/skypilot-lambda-comfyui.yaml:57-62`, both `examples/configs/*flashvsr-upscale.yaml`
- Modify: `docs/lifecycle.md`, `docs/warm-reuse.md`, `docs/extending.md`
- Test: `tests/validation/test_shipped_configs_capabilities.py`

**Acceptance Criteria:**
- [ ] Every `examples/configs/*.yaml` loads with zero ERROR findings.
- [ ] The three server skypilot configs produce exactly the expected WARN gap fields.
- [ ] The two flashvsr configs produce no `idle_timeout` gap and set `heartbeat_interval_s` explicitly.
- [ ] `docs/lifecycle.md` carries the capability matrix; `docs/extending.md` states that an undeclared provider is refused.

**Verify:** `pixi run python -m pytest tests/validation/test_shipped_configs_capabilities.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/validation/test_shipped_configs_capabilities.py
"""Shipped configs validate clean (Brief 2, Task 7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.capabilities import WorkloadShape
from kinoforge.core.config import load_config
from kinoforge.validation.checks.capabilities import evaluate_capability_gaps
from kinoforge.validation.protocol import Severity

CONFIG_DIR = Path("examples/configs")
SERVER_SKY = [
    "skypilot-gpu.yaml",
    "skypilot-cpu.yaml",
    "skypilot-lambda-comfyui.yaml",
]
BATCH_SKY = [
    "skypilot-lambda-diffusers-flashvsr-upscale.yaml",
    "skypilot-vast-diffusers-flashvsr-upscale.yaml",
]


@pytest.mark.parametrize(
    "name", sorted(p.name for p in CONFIG_DIR.glob("*.yaml")), ids=str
)
def test_no_shipped_config_has_an_error_gap(name: str) -> None:
    """Catches a config edit reintroducing an unenforceable guardrail."""
    cfg = load_config(CONFIG_DIR / name)
    if cfg.compute is None:
        pytest.skip("hosted cfg, no compute block")
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert [g.field for g in gaps if g.severity is Severity.ERROR] == []


@pytest.mark.parametrize("name", SERVER_SKY)
def test_server_skypilot_configs_warn_on_exactly_these_fields(name: str) -> None:
    """Catches risk-table drift making real warnings silently disappear."""
    cfg = load_config(CONFIG_DIR / name)
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.SERVER)
    assert {g.field for g in gaps} == {
        "compute.lifecycle.idle_timeout",
        "compute.lifecycle.job_timeout",
        "compute.lifecycle.heartbeat_interval_s",
    } - _unset_fields(cfg)


@pytest.mark.parametrize("name", BATCH_SKY)
def test_batch_skypilot_configs_have_no_idle_gap(name: str) -> None:
    """Autostop genuinely fires for run_cmd=[] specs — warning here would be
    the under-claim the shape parameter exists to avoid."""
    cfg = load_config(CONFIG_DIR / name)
    gaps = evaluate_capability_gaps(cfg, WorkloadShape.BATCH)
    assert all(g.field != "compute.lifecycle.idle_timeout" for g in gaps)
    assert cfg.compute.lifecycle.heartbeat_interval_s == 30
```

`_unset_fields(cfg)` returns the dotted names of risk-row fields absent from
`cfg.compute.lifecycle.model_fields_set`, so a config that does not set
`job_timeout` is not expected to warn about it.

- [ ] **Step 2: Run to verify failure**

Run: `pixi run python -m pytest tests/validation/test_shipped_configs_capabilities.py -v`
Expected: FAIL on the batch configs — neither sets `heartbeat_interval_s` yet.

- [ ] **Step 3: Fix the server config comments**

`examples/configs/skypilot-gpu.yaml` — replace line 8:

```yaml
#   - idle_timeout = 3 min -> SkyPilot autostop, which is INERT for this
#     server-mode deploy: run_cmd becomes a never-terminating Task.run, so
#     is_cluster_idle() is permanently False. The bound that actually holds
#     is the instance-side deadline watchdog at max_lifetime = 30 min.
```

and the lifecycle block (lines 48-54):

```yaml
  lifecycle:
    # Maps to SkyPilot autostop, but autostop cannot fire for a server-mode
    # deploy (see the header). Kept for provider portability; on skypilot the
    # protection comes from max_lifetime below.
    idle_timeout: 180
    # THE guardrail on this provider: the instance-side deadline watchdog
    # (providers/skypilot/watchdog.py) arms at the top of Task.setup and
    # autodowns the cluster at launch + max_lifetime, surviving controller death.
    max_lifetime: 1800
    # Not enforced on skypilot — only RunPod passes this through
    # (executionTimeoutMs). Used here for budget estimation only.
    job_timeout: 600
    boot_timeout: 900
    budget: 0.10
    # Required when warm_reuse_auto_attach=true (HeartbeatIntervalRequiredCheck),
    # and genuinely required. Note what it does NOT do: skypilot has no
    # wire-level heartbeat read, so last_heartbeat is the orchestrator clock —
    # it proves the controller is alive, not that the cluster is.
    heartbeat_interval_s: 30
```

Apply the same three corrections to `skypilot-cpu.yaml` (header `:7-8`, the
autostop-mapping comment at `:47-50`, and `:59`) and to
`skypilot-lambda-comfyui.yaml:57-62`.

- [ ] **Step 4: Fix the batch configs**

In both `*-flashvsr-upscale.yaml`, in the `lifecycle` block:

```yaml
    # BATCH shape: engine.diffusers.upscale_only=true renders run_cmd=[], so
    # the provision script exits and the cluster genuinely reaches idle —
    # SkyPilot autostop DOES fire here. Do not "fix" this into the
    # server-mode pattern used by skypilot-gpu.yaml.
    idle_timeout: 30m
    max_lifetime: 90m
    # Written explicitly rather than relying on the load-time auto-fix that
    # injects 30 — an invisible auto-fix is the same class of problem as an
    # unenforced guardrail.
    heartbeat_interval_s: 30
```

- [ ] **Step 5: Update the docs**

- `docs/lifecycle.md`: add a "Provider capability matrix" section with the §7.1 table, and state that a guardrail with no declared capability and no substitute refuses the load.
- `docs/warm-reuse.md`: update every `HEARTBEAT_SUBSTRATE_MISSING` row to the new meaning — expected absence within grace; past grace the row now classifies `ORPHAN_REAP` (still opt-in).
- `docs/extending.md`: add to the new-provider checklist — implement `capabilities()`, because the default is empty and an undeclared provider is refused by config validation.

- [ ] **Step 6: Run to verify pass**

Run: `pixi run python -m pytest tests/validation/test_shipped_configs_capabilities.py -v`
Expected: PASS

- [ ] **Step 7: Full green + pre-commit**

Run:
```bash
pixi run python -m pytest tests/ -q --cov=src/kinoforge
pixi run pre-commit run --all-files
```
Expected: PASS both.

- [ ] **Step 8: Commit**

```bash
git add examples/configs docs tests/validation
git commit -m "docs(configs): say what actually protects a skypilot run"
```

---

## Self-Review

**Spec coverage:** §4 vocabulary → Task 0. §5 declaration surface + registry → Task 0. §6 validator + risk table → Task 4; §6.2 shape inference → Tasks 4 (load) and 5 (launch). §7 audit → Task 1 (declarations) and Task 3 (refusals). §7.1 matrix → Task 1. §8 reaper → Task 6. §9 configs + docs → Task 7. §10 test plan A→Task 1, B→Task 4/5, C→Task 7, D→Task 6, E→Task 2, F→Task 3. No spec section is unclaimed.

**Type consistency:** `capabilities(shape: WorkloadShape) -> frozenset[Capability]` is used identically in Tasks 0, 1, 4, 5. `evaluate_capability_gaps(cfg, shape) -> list[Gap]` is used identically in Tasks 4, 5, 7. `Gap` fields (`field`, `risk`, `missing`, `substitute`, `severity`, `detail`) match every consumer. `capabilities_for(name, shape)` and `provider_billed(name)` match Tasks 0, 2, 3, 4.

**Known follow-ups the plan does not close** (design doc §12): the env-routing gap (default-env sweeps still mark skypilot rows `UNROUTABLE`), heuristic load-time shape inference (mitigated, not eliminated, by Task 5), and `billed` being per-class.
