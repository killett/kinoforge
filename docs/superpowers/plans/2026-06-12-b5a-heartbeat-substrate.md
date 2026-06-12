# B5a Heartbeat Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers-extended-cc:subagent-driven-development` (recommended) or `superpowers-extended-cc:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a provider-agnostic `HeartbeatEndpoint` Protocol + a RunPod GraphQL-tag satisfier so Layer V's `classify`, the future B1/B2/B3 consumers, and the future B5b SkyPilot satisfier inherit truthful heartbeat data on real-cloud providers.

**Architecture:** New `core/heartbeat_endpoints.py` defines a `runtime_checkable` Protocol with `write(id, ts_local)` / `read(id)` methods plus a `provider_heartbeat_supported(kind) -> bool` helper. A new `RunPodGraphQLHeartbeatEndpoint` satisfies the Protocol via RunPod's `podEditJob` mutation (tag write) and `pod` query (tag read), using the existing Phase 24 `_http_post` injectable seam. The orchestrator's `_resolve_provider` calls a dispatch helper in `_adapters.py` that builds the endpoint from `cfg.compute.heartbeat_mode` and injects it onto the resolved provider via a new `set_heartbeat_endpoint` method. Layer V's `classify` consults the helper before emitting destructive verdicts on substrate-missing providers, emitting a new `HEARTBEAT_SUBSTRATE_MISSING` verdict that `act_on_verdict` hard-pins to no-destroy + WARN-once.

**Tech Stack:** Python 3.12+, `pydantic` v2 (config), `httpx`/`urllib.request` (transport seam), `pytest` + `pytest-cov`, RunPod GraphQL API, pixi env management.

**Spec:** `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` (commit `d0c009d`).

---

## File Structure

**New files (3):**
- `src/kinoforge/core/heartbeat_endpoints.py` — substrate Protocol + helper.
- `src/kinoforge/providers/runpod/heartbeat.py` — RunPod GraphQL-tag satisfier.
- `tests/providers/test_heartbeat_parity.py` — cross-provider Protocol parity tests.

**New test files (3):**
- `tests/core/test_heartbeat_endpoints.py` — substrate Protocol tests.
- `tests/providers/runpod/test_heartbeat.py` — RunPod wire-shape tests.
- `tests/live/test_runpod_heartbeat_live.py` — live RunPod smoke.

**Modified files (8):**
- `src/kinoforge/core/errors.py` — `class TransportError(KinoforgeError)`.
- `src/kinoforge/core/config.py` — `ComputeConfig.heartbeat_mode` + validator.
- `src/kinoforge/core/interfaces.py` — `ComputeProvider.set_heartbeat_endpoint` ABC no-op default.
- `src/kinoforge/core/reaper.py` — `HEARTBEAT_SUBSTRATE_MISSING` verdict + classify gate.
- `src/kinoforge/core/reaper_actor.py` — `act_on_verdict` no-destroy arm.
- `src/kinoforge/core/orchestrator.py` — `_resolve_provider` injects endpoint.
- `src/kinoforge/providers/runpod/__init__.py` — constructor kwarg, `set_heartbeat_endpoint`, `heartbeat()` + `last_heartbeat()` delegation.
- `src/kinoforge/_adapters.py` — `build_heartbeat_endpoint_for(cfg, creds)` dispatch.
- `tests/providers/conftest.py` — `FakeRunPodHeartbeatEndpoint` + `FakeSkyPilotHeartbeatEndpoint` test doubles.
- `tests/providers/test_runpod.py` — kwarg + delegation tests.
- `tests/core/test_config.py` — `heartbeat_mode` literal-set tests.
- `tests/core/test_heartbeat_loop.py` — RunPod integration test extension.
- `tests/core/test_reaper.py` — classify + act_on_verdict tests.

---

## Task a: Substrate Protocol + TransportError + provider_heartbeat_supported helper

**Goal:** Ship `core/heartbeat_endpoints.py` + `TransportError` in `core/errors.py` + fake doubles in `tests/providers/conftest.py` so all downstream tasks have the contract to consume.

**Files:**
- Create: `src/kinoforge/core/heartbeat_endpoints.py`
- Modify: `src/kinoforge/core/errors.py:46` (insert after `TeardownError`)
- Modify: `tests/providers/conftest.py` (append two fakes)
- Create: `tests/core/test_heartbeat_endpoints.py`

**Acceptance Criteria:**
- [ ] `HeartbeatEndpoint` Protocol is `runtime_checkable`; `write(instance_id: str, ts_local: datetime) -> None` and `read(instance_id: str) -> datetime | None` signatures present.
- [ ] `provider_heartbeat_supported("local")` returns True; `("runpod")` returns True; `("skypilot")` returns False; `("unknown")` returns False.
- [ ] `TransportError` subclasses `KinoforgeError`.
- [ ] `FakeRunPodHeartbeatEndpoint`: dict-backed; sub-second precision; `inject_transport_failure(method: Literal["read", "write"])` toggle raises `TransportError` on next call to that method.
- [ ] `FakeSkyPilotHeartbeatEndpoint`: dict-backed with mtime field; round-trip truncates to seconds; `inject_ssh_refused()` raises `TransportError`; `cold_latency_s` constructor param injectable.
- [ ] `isinstance(FakeRunPodHeartbeatEndpoint(), HeartbeatEndpoint)` is True.
- [ ] `isinstance(FakeSkyPilotHeartbeatEndpoint(), HeartbeatEndpoint)` is True.

**Verify:** `pixi run test tests/core/test_heartbeat_endpoints.py -v && pixi run typecheck && pixi run pre-commit run --files src/kinoforge/core/heartbeat_endpoints.py src/kinoforge/core/errors.py tests/providers/conftest.py tests/core/test_heartbeat_endpoints.py`

**Steps:**

- [ ] **Step a.1: Write failing test for Protocol + helper**

```python
# tests/core/test_heartbeat_endpoints.py
"""Substrate Protocol + helper tests (B5a Task a).

Verifies:
- HeartbeatEndpoint Protocol is runtime_checkable and structurally satisfied
  by the fake doubles in tests/providers/conftest.py.
- provider_heartbeat_supported returns True for B5a-shipped providers
  (local + runpod) and False otherwise (drift-detector for B5b).
- TransportError sits under the KinoforgeError hierarchy.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kinoforge.core.errors import KinoforgeError, TransportError
from kinoforge.core.heartbeat_endpoints import (
    HeartbeatEndpoint,
    provider_heartbeat_supported,
)


def test_transport_error_is_kinoforge_error() -> None:
    """TransportError must subclass KinoforgeError so existing broad-catch
    arms (HeartbeatLoop._tick_once) keep working."""
    err = TransportError("boom")
    assert isinstance(err, KinoforgeError)


@pytest.mark.parametrize(
    ("provider_kind", "expected"),
    [
        ("local", True),
        ("runpod", True),
        ("skypilot", False),  # drift-detector: flips True when B5b ships
        ("unknown", False),
        ("", False),
    ],
)
def test_provider_heartbeat_supported_table(
    provider_kind: str, expected: bool
) -> None:
    """provider_heartbeat_supported is the gate B1/B2/B3 + classify
    consult before destroying on HEARTBEAT_UNKNOWN. B5a-shipped set is
    {local, runpod}; B5b adds skypilot."""
    assert provider_heartbeat_supported(provider_kind) is expected


def test_protocol_is_runtime_checkable_via_fake_runpod(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """The fake double is structurally a HeartbeatEndpoint via the
    runtime_checkable Protocol — guarantees parity tests can parametrize
    over the Protocol type and the cross-provider fixture works."""
    assert isinstance(fake_runpod_heartbeat_endpoint, HeartbeatEndpoint)


def test_protocol_is_runtime_checkable_via_fake_skypilot(
    fake_skypilot_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """B5b drift mitigation: the SkyPilot fake must satisfy the contract
    BEFORE the wire-level real version ships."""
    assert isinstance(fake_skypilot_heartbeat_endpoint, HeartbeatEndpoint)


def test_fake_runpod_round_trips_tz_aware_datetime(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """The Protocol contract: write+read preserves the TZ-aware datetime.
    Bug catch: a fake that silently strips tzinfo or normalises to UTC
    would let a real wire bug ride."""
    ts = datetime(2026, 6, 12, 14, 23, 5, tzinfo=timezone.utc).astimezone()
    fake_runpod_heartbeat_endpoint.write("pod-x", ts)
    got = fake_runpod_heartbeat_endpoint.read("pod-x")
    assert got == ts
    assert got is not None and got.tzinfo is not None


def test_fake_skypilot_truncates_to_seconds(
    fake_skypilot_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """SkyPilot satisfier reads filesystem mtime (`stat -c %Y`),
    which is second-precision. The fake must mirror that truncation
    so consumers can't accidentally depend on sub-second precision."""
    ts = datetime.now().astimezone().replace(microsecond=500_000)
    fake_skypilot_heartbeat_endpoint.write("cluster-x", ts)
    got = fake_skypilot_heartbeat_endpoint.read("cluster-x")
    assert got is not None
    assert got.microsecond == 0


def test_read_of_never_written_returns_none(
    fake_runpod_heartbeat_endpoint: HeartbeatEndpoint,
) -> None:
    """Reading a slot that was never written is NOT a transport failure;
    it is a valid 'no data yet' answer (Protocol invariant)."""
    assert fake_runpod_heartbeat_endpoint.read("never-written") is None


def test_inject_transport_failure_raises(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """Fake's inject_transport_failure toggle is the contract for the
    cross-provider parity tests (Task e) — must raise TransportError,
    not a generic Exception."""
    fake_runpod_heartbeat_endpoint.inject_transport_failure("write")
    with pytest.raises(TransportError):
        fake_runpod_heartbeat_endpoint.write(
            "pod-x", datetime.now().astimezone()
        )
```

- [ ] **Step a.2: Run test to verify it fails**

Run: `pixi run test tests/core/test_heartbeat_endpoints.py -v`
Expected: FAIL with `ImportError: cannot import name 'TransportError'` / `'HeartbeatEndpoint'` / `'provider_heartbeat_supported'`.

- [ ] **Step a.3: Implement `TransportError`**

Append to `src/kinoforge/core/errors.py` (after `TeardownError` at line 46, before `UnknownAdapter` at line 49):

```python
class TransportError(KinoforgeError):
    """Raised when a HeartbeatEndpoint satisfier's underlying transport fails.

    Examples: RunPod GraphQL non-2xx, RunPod GraphQL ``errors`` response,
    SkyPilot SSH ``Connection refused``, selfterm HTTP timeout. Distinct
    from other KinoforgeError subclasses because callers (HeartbeatLoop
    ._tick_once) treat transport flakes differently from semantic errors
    — they retry on the next tick rather than aborting.
    """
```

- [ ] **Step a.4: Implement `core/heartbeat_endpoints.py`**

Create `src/kinoforge/core/heartbeat_endpoints.py`:

```python
"""Provider-agnostic substrate for orchestrator-side heartbeat I/O (B5a).

This module hosts the cross-provider Protocol and capability gate that the
Layer U HeartbeatLoop's provider-side delegation rests on. Concrete
satisfiers live under ``kinoforge.providers.<name>.heartbeat``; this module
must never import them (core-import-ban invariant — see
PROGRESS.md §"Key decisions").

The B5a-shipped set in :data:`_HEARTBEAT_SUPPORTED` is the source of truth
for which providers have a wire-level satisfier. B5b adds ``"skypilot"`` in
one line; downstream consumers (Layer V classify, B1 sweeper, B3 warm-reuse)
gate destructive verdicts via :func:`provider_heartbeat_supported`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = ["HeartbeatEndpoint", "provider_heartbeat_supported"]


@runtime_checkable
class HeartbeatEndpoint(Protocol):
    """Provider-agnostic substrate for orchestrator-side heartbeat I/O.

    Contract invariants (satisfied by every wire-level satisfier):

    - ``write(id, ts)`` is idempotent on duplicate ``ts`` (double-write
      same value is a no-op).
    - ``read(id)`` returns the most-recently-written ts for ``id``, or
      ``None`` if the instance is gone, the storage slot was never
      written, or the underlying side-channel was wiped.
    - ``read(id)`` precision is at least 1-second granularity. Sub-second
      precision is permitted but never required by consumers (Layer V
      dead-man window is ``heartbeat_interval_s * 3``, minimum ~30s).
    - Transport failures (HTTP non-2xx, SSH connection refused, GraphQL
      rate-limit) propagate as :class:`~kinoforge.core.errors.TransportError`
      from BOTH write and read.
    - ``read`` returning ``None`` is NOT a transport failure — it is a
      valid "never written / instance gone" answer.
    - ``ts_local`` is a timezone-aware datetime in local TZ per project
      memory ``feedback_local_timezone_only``. Satisfiers store-and-return
      the same TZ; round-trip preserves wall-clock.
    """

    def write(self, instance_id: str, ts_local: datetime) -> None:
        """Record ``ts_local`` as the most-recent heartbeat for ``instance_id``.

        Args:
            instance_id: Provider-local instance identifier.
            ts_local: Timezone-aware datetime in local TZ.

        Raises:
            TransportError: The underlying side-channel write failed.
        """
        ...

    def read(self, instance_id: str) -> datetime | None:
        """Return the most-recent heartbeat for ``instance_id``, or ``None``.

        Args:
            instance_id: Provider-local instance identifier.

        Returns:
            The most-recent written timestamp, or ``None`` if the instance
            is gone or the slot was never written.

        Raises:
            TransportError: The underlying side-channel read failed.
        """
        ...


# B5a-shipped set. B5b adds "skypilot". Downstream consumers consult this
# via provider_heartbeat_supported() before treating HEARTBEAT_UNKNOWN as
# actionable on cloud providers.
_HEARTBEAT_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})


def provider_heartbeat_supported(provider_kind: str) -> bool:
    """Whether a wire-level :class:`HeartbeatEndpoint` ships for ``provider_kind``.

    Used by :func:`kinoforge.core.reaper.classify` to emit the new
    ``HEARTBEAT_SUBSTRATE_MISSING`` verdict on providers whose substrate
    has not yet shipped (e.g. SkyPilot pre-B5b). Consumers
    (:func:`kinoforge.core.reaper_actor.act_on_verdict`) hard-pin that
    verdict to no-destroy + WARN-once.

    Args:
        provider_kind: The ``compute.provider`` field value
            (``"local"`` / ``"runpod"`` / ``"skypilot"`` / ...).

    Returns:
        ``True`` when a wire-level :class:`HeartbeatEndpoint` satisfier
        is shipped for this provider; ``False`` otherwise.
    """
    return provider_kind in _HEARTBEAT_SUPPORTED
```

- [ ] **Step a.5: Implement fake doubles**

Append to `tests/providers/conftest.py` (or create if absent — verify with `head tests/providers/conftest.py` first):

```python
"""Per-package fixtures for provider-layer tests.

The B5a heartbeat substrate adds two test doubles that satisfy the
:class:`kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint` Protocol.
Both are dict-backed and exercise the Protocol contract from the angle of
their respective wire-level real versions (RunPod = sub-second ISO; SkyPilot
= second-precision filesystem mtime).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import pytest

from kinoforge.core.errors import TransportError


@dataclass
class FakeRunPodHeartbeatEndpoint:
    """Dict-backed test double for the RunPod GraphQL-tag satisfier.

    Mirrors the wire-level shape exactly: sub-second-precision round-trip;
    explicit transport-failure injection point per call type.
    """

    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"FakeRunPodHeartbeatEndpoint: injected write failure for {instance_id}"
            )
        self._slots[instance_id] = ts_local

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"FakeRunPodHeartbeatEndpoint: injected read failure for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_transport_failure(self, method: Literal["read", "write"]) -> None:
        """Arm the next call of ``method`` to raise :class:`TransportError`."""
        if method == "read":
            self._fail_on_read = True
        elif method == "write":
            self._fail_on_write = True
        else:
            raise ValueError(f"method must be 'read' or 'write'; got {method!r}")

    def destroy_instance(self, instance_id: str) -> None:
        """Test helper: simulate the pod being destroyed.

        After this call, ``read(instance_id)`` returns ``None`` per the
        Protocol invariant 'returns None if the instance is gone'.
        """
        self._slots.pop(instance_id, None)


@dataclass
class FakeSkyPilotHeartbeatEndpoint:
    """Dict-backed test double for the future B5b SSH-touch satisfier.

    Mirrors the SkyPilot wire shape: round-trip truncates to seconds
    (``stat -c %Y`` returns POSIX-seconds); cold-vs-warm SSH latency is
    injectable but not actually measured here.
    """

    cold_latency_s: float = 0.0
    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"FakeSkyPilotHeartbeatEndpoint: SSH connection refused for {instance_id}"
            )
        # SkyPilot stores via filesystem mtime — second-precision only.
        truncated = ts_local.replace(microsecond=0)
        self._slots[instance_id] = truncated

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"FakeSkyPilotHeartbeatEndpoint: SSH connection refused for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_ssh_refused(self) -> None:
        """Arm BOTH next read and next write to raise :class:`TransportError`.

        Mirrors the SkyPilot SSH-multiplexer failure mode where one bad
        ControlMaster takes down both directions.
        """
        self._fail_on_read = True
        self._fail_on_write = True

    def destroy_instance(self, instance_id: str) -> None:
        """Test helper: simulate the cluster being torn down."""
        self._slots.pop(instance_id, None)


@pytest.fixture()
def fake_runpod_heartbeat_endpoint() -> FakeRunPodHeartbeatEndpoint:
    """Fresh fake RunPod heartbeat endpoint per test."""
    return FakeRunPodHeartbeatEndpoint()


@pytest.fixture()
def fake_skypilot_heartbeat_endpoint() -> FakeSkyPilotHeartbeatEndpoint:
    """Fresh fake SkyPilot heartbeat endpoint per test."""
    return FakeSkyPilotHeartbeatEndpoint()
```

Note: if `tests/providers/conftest.py` already exists with other fixtures, append the imports and the dataclass+fixture block; do NOT replace the file.

- [ ] **Step a.6: Run tests to verify they pass**

Run: `pixi run test tests/core/test_heartbeat_endpoints.py -v`
Expected: All 8 tests PASS.

- [ ] **Step a.7: Run lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/heartbeat_endpoints.py src/kinoforge/core/errors.py tests/providers/conftest.py tests/core/test_heartbeat_endpoints.py`
Expected: All hooks PASS.

- [ ] **Step a.8: Commit**

```bash
git add src/kinoforge/core/heartbeat_endpoints.py \
        src/kinoforge/core/errors.py \
        tests/providers/conftest.py \
        tests/core/test_heartbeat_endpoints.py
git commit -m "$(cat <<'EOF'
feat(b5a): heartbeat substrate Protocol + TransportError + fakes

Substrate Protocol for B5a (HeartbeatEndpoint, runtime_checkable;
write+read of TZ-aware datetime) and the provider_heartbeat_supported
helper that B1/B2/B3 consult before destructive verdicts. Hardcoded
frozenset({local, runpod}) — B5b flips skypilot to True.

TransportError lives under the KinoforgeError hierarchy so the Layer U
HeartbeatLoop's broad try/except keeps swallowing it (single-bad-tick
must not kill the loop).

FakeRunPodHeartbeatEndpoint + FakeSkyPilotHeartbeatEndpoint ship now so
B5b's contract is exercised before its wire version lands (drift
mitigation per spec §11 Risk 4).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task b: RunPodGraphQLHeartbeatEndpoint satisfier + wire-shape tests

**Goal:** Concrete wire-level satisfier that round-trips a heartbeat tag through RunPod's `podEditJob` mutation + `pod` query, with the `_http_post` seam injectable for offline tests.

**Files:**
- Create: `src/kinoforge/providers/runpod/heartbeat.py`
- Create: `tests/providers/runpod/test_heartbeat.py`

**Acceptance Criteria:**
- [ ] write: POST URL equals `graphql_url`; payload contains `podEditJob` mutation; `variables.input.podId == instance_id`; `variables.input.tags == [{"key": "_kinoforge_last_heartbeat", "value": "<iso>"}]`.
- [ ] write: GraphQL `errors` array in response → `TransportError`.
- [ ] write: HTTP non-2xx (raised by the seam) → `TransportError`.
- [ ] read: payload contains `pod` query; `variables.podId == instance_id`.
- [ ] read: response with populated tag → returns parsed `datetime`.
- [ ] read: `data.pod == null` → returns `None` (no exception).
- [ ] read: pod present but tag missing → returns `None`.
- [ ] read: ISO parse failure → `TransportError` (corrupted slot).
- [ ] TZ preservation: write a TZ-aware datetime with explicit offset `-07:00`, read back, assert `tzinfo` matches via UTC offset equality.
- [ ] No new SDK dependency; default `_http_post` uses stdlib `urllib.request`.

**Verify:** `pixi run test tests/providers/runpod/test_heartbeat.py -v && pixi run typecheck && pixi run pre-commit run --files src/kinoforge/providers/runpod/heartbeat.py tests/providers/runpod/test_heartbeat.py`

**Steps:**

- [ ] **Step b.1: Write failing wire-shape tests**

```python
# tests/providers/runpod/test_heartbeat.py
"""RunPod GraphQL-tag heartbeat satisfier wire-shape tests (B5a Task b).

Tests the precise GraphQL payload shape produced by
:class:`RunPodGraphQLHeartbeatEndpoint` via a spy ``http_post`` seam, so
upstream wire drift (RunPod schema change, tag-key namespace conflict,
missing field) fails loud rather than silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.heartbeat import (
    HEARTBEAT_TAG_KEY,
    RunPodGraphQLHeartbeatEndpoint,
)


def _make_endpoint(
    responses: list[dict[str, Any]],
) -> tuple[RunPodGraphQLHeartbeatEndpoint, list[tuple[str, dict[str, Any]]]]:
    """Build an endpoint with a spy ``http_post`` returning ``responses`` in order.

    Returns the endpoint and a captured ``[(url, payload), ...]`` list so
    tests can introspect the precise wire shape.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    response_iter = iter(responses)

    def spy_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((url, payload))
        return next(response_iter)

    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake",
        graphql_url="https://api.runpod.io/graphql",
        http_post=spy_post,
    )
    return endpoint, calls


def test_write_posts_pod_edit_job_mutation_with_tag() -> None:
    """write must POST a podEditJob mutation carrying the heartbeat tag.

    Bug catch: a payload that nests tags under the wrong key, omits
    podId, or uses a different mutation name silently breaks the
    cross-session warm-reuse contract (the read path looks for tags on
    the pod schema).
    """
    endpoint, calls = _make_endpoint([{"data": {"podEditJob": {"id": "pod-x"}}}])
    ts = datetime(2026, 6, 12, 14, 23, 5, tzinfo=timezone(timedelta(hours=-7)))

    endpoint.write("pod-x", ts)

    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.runpod.io/graphql"
    assert "podEditJob" in payload["query"]
    variables = payload["variables"]
    assert variables["input"]["podId"] == "pod-x"
    assert variables["input"]["tags"] == [
        {"key": HEARTBEAT_TAG_KEY, "value": ts.isoformat()}
    ]


def test_write_raises_transport_error_on_graphql_errors() -> None:
    """GraphQL responses with an ``errors`` array must surface as
    TransportError — silently swallowing would let a typo or schema
    change kill heartbeats without operator visibility."""
    endpoint, _ = _make_endpoint(
        [{"errors": [{"message": "field 'podEditJob' missing on Mutation"}]}]
    )

    with pytest.raises(TransportError, match="podEditJob"):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_write_raises_transport_error_when_seam_raises() -> None:
    """The injected ``http_post`` may raise (HTTP non-2xx maps to its own
    exception in the prod seam). The endpoint must re-raise as
    TransportError so consumers can branch on the substrate exception
    rather than the transport's vendor type."""

    def explode(_url: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("HTTP 502 Bad Gateway")

    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake",
        graphql_url="https://api.runpod.io/graphql",
        http_post=explode,
    )
    with pytest.raises(TransportError, match="502"):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_read_returns_parsed_datetime_from_tag() -> None:
    """The full write→read round trip on the wire. The read path looks
    up the pod and parses the well-known tag key."""
    ts_iso = "2026-06-12T14:23:05-07:00"
    endpoint, calls = _make_endpoint(
        [
            {
                "data": {
                    "pod": {
                        "id": "pod-x",
                        "tags": [{"key": HEARTBEAT_TAG_KEY, "value": ts_iso}],
                    }
                }
            }
        ]
    )

    got = endpoint.read("pod-x")

    assert got is not None
    assert got.isoformat() == ts_iso
    assert got.utcoffset() == timedelta(hours=-7)  # tzinfo preserved
    # Verify the read payload shape
    assert len(calls) == 1
    url, payload = calls[0]
    assert "pod(" in payload["query"]
    assert payload["variables"]["podId"] == "pod-x"


def test_read_returns_none_when_pod_destroyed() -> None:
    """A read after the pod is destroyed returns ``data.pod == null``;
    the satisfier must surface this as ``None``, NOT as TransportError —
    pod-gone is a valid 'no heartbeat available' answer."""
    endpoint, _ = _make_endpoint([{"data": {"pod": None}}])
    assert endpoint.read("ghost-pod") is None


def test_read_returns_none_when_tag_absent() -> None:
    """Pod is alive but the heartbeat tag was never written.
    Returns None (never-written invariant), not TransportError."""
    endpoint, _ = _make_endpoint(
        [{"data": {"pod": {"id": "pod-x", "tags": [{"key": "other", "value": "v"}]}}}]
    )
    assert endpoint.read("pod-x") is None


def test_read_raises_transport_error_on_iso_parse_failure() -> None:
    """A corrupted slot (tag value not parseable as ISO) is loud-on-violation
    — should never happen in production but a silent fall-through could
    cascade into 'permanent HEARTBEAT_UNKNOWN' across the ledger."""
    endpoint, _ = _make_endpoint(
        [
            {
                "data": {
                    "pod": {
                        "id": "pod-x",
                        "tags": [{"key": HEARTBEAT_TAG_KEY, "value": "not-an-iso-date"}],
                    }
                }
            }
        ]
    )
    with pytest.raises(TransportError, match="corrupted heartbeat tag"):
        endpoint.read("pod-x")


def test_read_raises_transport_error_on_graphql_errors() -> None:
    """Same surface as write: GraphQL errors array → TransportError."""
    endpoint, _ = _make_endpoint([{"errors": [{"message": "rate limit exceeded"}]}])
    with pytest.raises(TransportError, match="rate limit"):
        endpoint.read("pod-x")


def test_default_http_post_uses_stdlib() -> None:
    """No new SDK dependency. With http_post=None the constructor must
    pick a stdlib-backed callable, not silently fail or import httpx."""
    endpoint = RunPodGraphQLHeartbeatEndpoint(
        api_key="sk-fake", graphql_url="https://api.runpod.io/graphql"
    )
    # Just verify the attribute resolves to a callable; we don't fire it.
    assert callable(endpoint._http_post)
```

- [ ] **Step b.2: Run test to verify it fails**

Run: `pixi run test tests/providers/runpod/test_heartbeat.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kinoforge.providers.runpod.heartbeat'`.

- [ ] **Step b.3: Implement the satisfier**

Create `src/kinoforge/providers/runpod/heartbeat.py`:

```python
"""RunPod GraphQL-tag heartbeat satisfier (B5a Task b).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by reading/writing a well-known tag (``_kinoforge_last_heartbeat``) on
the RunPod pod resource via the GraphQL ``podEditJob`` mutation and the
``pod`` query. Both methods go through an injected ``http_post`` seam so
tests can spy the precise wire payload without a real RunPod account.

The tag survives across orchestrator process lifetimes; this is what
makes the cross-session warm-reuse path (B3) workable on a fresh shell
— the previous orchestrator's last write persists in RunPod's tag store
and the next orchestrator reads it back without any local state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from kinoforge.core.errors import TransportError

__all__ = ["HEARTBEAT_TAG_KEY", "RunPodGraphQLHeartbeatEndpoint"]

#: Tag key written to RunPod pods. Underscore prefix marks kinoforge-internal
#: so operators reading tags in the RunPod console can recognise the
#: namespace.
HEARTBEAT_TAG_KEY: str = "_kinoforge_last_heartbeat"

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    tags { key value }
  }
}
""".strip()


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth.

    Phase 24 pattern: HTTP via stdlib urllib by default, replaceable via
    the constructor's ``http_post`` kwarg in tests and production opt-in.
    """

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "kinoforge-heartbeat/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                data: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            raise TransportError(
                f"RunPod GraphQL HTTP {exc.code}: {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise TransportError(f"RunPod GraphQL transport error: {exc.reason}") from exc
        try:
            decoded: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TransportError(f"RunPod GraphQL non-JSON response: {exc}") from exc
        return decoded

    return _post


class RunPodGraphQLHeartbeatEndpoint:
    """GraphQL-tag satisfier: ``podEditJob`` (write) + ``pod`` (read).

    Both methods raise :class:`TransportError` on HTTP non-2xx, GraphQL
    ``errors`` arrays, JSON parse failures, and corrupted tag values.
    Pod-gone (``data.pod == null``) and tag-absent are valid ``None``
    returns, not transport failures.

    Args:
        api_key: RunPod API key with write scope (the main
            ``RUNPOD_API_KEY``, NOT the scoped ``RUNPOD_TERMINATE_KEY``
            which is delete-only).
        graphql_url: RunPod GraphQL endpoint URL. Defaults to the
            production URL.
        http_post: Optional injected POST callable. ``None`` builds a
            stdlib-urllib callable with Bearer auth.
    """

    def __init__(
        self,
        *,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._api_key = api_key
        self._graphql_url = graphql_url
        self._http_post = http_post if http_post is not None else _default_http_post(api_key)

    def write(self, instance_id: str, ts_local: datetime) -> None:
        """Stamp the heartbeat tag on ``instance_id`` with ``ts_local``.

        Idempotent: writing the same value twice rewrites the same slot.
        """
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {
                "input": {
                    "podId": instance_id,
                    "tags": [
                        {"key": HEARTBEAT_TAG_KEY, "value": ts_local.isoformat()}
                    ],
                }
            },
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface ANY transport flake as TransportError
            raise TransportError(f"RunPod podEditJob transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")

    def read(self, instance_id: str) -> datetime | None:
        """Read the heartbeat tag on ``instance_id``.

        Returns:
            The parsed timestamp, or ``None`` when the pod is gone or the
            tag has never been written.

        Raises:
            TransportError: HTTP failure, GraphQL ``errors`` array, JSON
                parse failure, or a tag value that is present but not
                parseable as an ISO-8601 datetime.
        """
        payload = {
            "query": _POD_QUERY,
            "variables": {"podId": instance_id},
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod pod query failed: {resp['errors']}")
        pod = resp.get("data", {}).get("pod")
        if pod is None:
            return None  # instance gone — valid None
        for tag in pod.get("tags") or []:
            if tag.get("key") == HEARTBEAT_TAG_KEY:
                value = tag.get("value")
                if not isinstance(value, str):
                    raise TransportError(
                        f"corrupted heartbeat tag for {instance_id}: value not a string"
                    )
                try:
                    return datetime.fromisoformat(value)
                except ValueError as exc:
                    raise TransportError(
                        f"corrupted heartbeat tag for {instance_id}: {value!r}"
                    ) from exc
        return None  # never written — valid None
```

- [ ] **Step b.4: Run tests to verify they pass**

Run: `pixi run test tests/providers/runpod/test_heartbeat.py -v`
Expected: All 9 tests PASS.

- [ ] **Step b.5: Run lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/providers/runpod/heartbeat.py tests/providers/runpod/test_heartbeat.py`
Expected: All hooks PASS.

- [ ] **Step b.6: Commit**

```bash
git add src/kinoforge/providers/runpod/heartbeat.py \
        tests/providers/runpod/test_heartbeat.py
git commit -m "$(cat <<'EOF'
feat(b5a): RunPod GraphQL-tag heartbeat satisfier

RunPodGraphQLHeartbeatEndpoint writes _kinoforge_last_heartbeat tag via
podEditJob mutation, reads back via pod query. Phase 24 _http_post
injectable seam; stdlib urllib default; no new SDK dep.

Failure mapping: HTTP non-2xx, GraphQL `errors` arrays, JSON parse fails,
and corrupted ISO tag values all raise TransportError. Pod-gone
(data.pod == null) and tag-absent return None per the Protocol invariant.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task c: RunPod provider wiring + YAML gate + adapter dispatch + integration test

**Goal:** Production-side wire-up. `RunPodProvider` accepts `heartbeat_endpoint`, delegates from `heartbeat()`+`last_heartbeat()`; `ComputeConfig.heartbeat_mode` validates the literal set; `_adapters.build_heartbeat_endpoint_for(cfg, creds)` dispatches; `_resolve_provider` injects the endpoint onto the resolved provider.

**Files:**
- Modify: `src/kinoforge/core/config.py` (add `heartbeat_mode` to `ComputeConfig` at line 488)
- Modify: `src/kinoforge/core/interfaces.py` (add `ComputeProvider.set_heartbeat_endpoint` ABC method with no-op default)
- Modify: `src/kinoforge/providers/runpod/__init__.py` (constructor kwarg + `set_heartbeat_endpoint` + `heartbeat()` + `last_heartbeat()`)
- Modify: `src/kinoforge/_adapters.py` (add `build_heartbeat_endpoint_for(cfg, creds)`)
- Modify: `src/kinoforge/core/orchestrator.py` (`_resolve_provider` injects endpoint)
- Modify: `tests/providers/test_runpod.py` (kwarg + delegation tests)
- Modify: `tests/core/test_config.py` (heartbeat_mode validator tests)
- Modify: `tests/core/test_heartbeat_loop.py` (RunPod integration test)

**Acceptance Criteria:**
- [ ] `ComputeConfig.heartbeat_mode` accepts `{"none", "graphql-tag", "selfterm-http", "ssh-touch"}`; rejects anything else with `ValueError` at config-load.
- [ ] Default `heartbeat_mode="none"` preserves existing YAML compatibility.
- [ ] `RunPodProvider(heartbeat_endpoint=...)` accepts the kwarg.
- [ ] `RunPodProvider.heartbeat(id)` delegates to `endpoint.write(id, datetime.now().astimezone())` when set; no-op when `None`.
- [ ] `RunPodProvider.last_heartbeat(id)` returns `endpoint.read(id).timestamp()` when set + tag present; returns `None` when set + tag absent; returns `None` when endpoint is `None`.
- [ ] `RunPodProvider.set_heartbeat_endpoint(endpoint)` installs the endpoint post-construction.
- [ ] `_adapters.build_heartbeat_endpoint_for` returns `None` for `mode="none"`; raises `AuthError` when `mode="graphql-tag"` and `RUNPOD_API_KEY` is None; raises `ValidationError` on RunPod-incompatible mode strings (e.g. `"ssh-touch"` on `provider="runpod"`); returns `RunPodGraphQLHeartbeatEndpoint` instance when args valid.
- [ ] `_resolve_provider` calls `build_heartbeat_endpoint_for` and `set_heartbeat_endpoint` when `cfg.compute.heartbeat_mode != "none"`.
- [ ] HeartbeatLoop 3-tick integration: ledger entries have monotonically-advancing `last_heartbeat` floats matching `FakeClock` advance.
- [ ] All existing `tests/providers/test_runpod.py` tests pass unchanged (backward compatibility).

**Verify:** `pixi run test tests/providers/test_runpod.py tests/core/test_heartbeat_loop.py tests/core/test_config.py -v && pixi run typecheck && pixi run pre-commit run --files src/kinoforge/providers/runpod/__init__.py src/kinoforge/core/config.py src/kinoforge/_adapters.py src/kinoforge/core/orchestrator.py src/kinoforge/core/interfaces.py`

**Steps:**

- [ ] **Step c.1: Write failing tests for `ComputeConfig.heartbeat_mode` validator**

Append to `tests/core/test_config.py`:

```python
def test_compute_config_heartbeat_mode_default_is_none() -> None:
    """Backward compat: existing YAMLs without compute.heartbeat_mode
    must load unchanged with mode='none' (no-op heartbeat path)."""
    from kinoforge.core.config import ComputeConfig
    cfg = ComputeConfig(
        provider="runpod",
        image="runpod/base:latest",
    )
    assert cfg.heartbeat_mode == "none"


@pytest.mark.parametrize(
    "mode", ["none", "graphql-tag", "selfterm-http", "ssh-touch"]
)
def test_compute_config_heartbeat_mode_accepts_valid_literals(mode: str) -> None:
    """All four literals in the union of supported modes load.

    Provider-mode compatibility (e.g. RunPod doesn't accept 'ssh-touch')
    is enforced at adapter dispatch time, not config load — config can't
    know which provider satisfies which mode without violating
    core-import-ban.
    """
    from kinoforge.core.config import ComputeConfig
    cfg = ComputeConfig(
        provider="runpod",
        image="runpod/base:latest",
        heartbeat_mode=mode,
    )
    assert cfg.heartbeat_mode == mode


def test_compute_config_heartbeat_mode_rejects_unknown() -> None:
    """Typo-class bugs ('graphqltag', 'graphql_tag', 'none ') fail loud
    at config-load, not at runtime when the orchestrator dispatches."""
    from pydantic import ValidationError as PydanticValidationError
    from kinoforge.core.config import ComputeConfig
    with pytest.raises(PydanticValidationError, match="heartbeat_mode"):
        ComputeConfig(
            provider="runpod",
            image="runpod/base:latest",
            heartbeat_mode="graphql_tag",  # underscore not dash — common typo
        )
```

- [ ] **Step c.2: Verify the test fails**

Run: `pixi run test tests/core/test_config.py::test_compute_config_heartbeat_mode_default_is_none -v`
Expected: FAIL with `unexpected keyword argument` or attribute missing.

- [ ] **Step c.3: Add `heartbeat_mode` to `ComputeConfig`**

Edit `src/kinoforge/core/config.py` — modify `ComputeConfig` (line 473-488):

```python
class ComputeConfig(BaseModel):
    """The compute block describing where workloads run.

    Attributes:
        provider: Compute provider name (e.g. "runpod").
        image: Container image reference.
        mode: Instance mode; "pod" or "serverless".
        requirements: Hardware requirements override.
        lifecycle: Lifecycle guardrails (budget required here for non-hosted).
        heartbeat_mode: Heartbeat substrate gate (B5a). Value space is the
            union across all providers; provider-mode compatibility is
            checked at adapter-dispatch time. Default ``"none"`` preserves
            pre-B5a no-op heartbeat behaviour.
    """

    provider: str
    image: str
    mode: str = "pod"
    requirements: RequirementsConfig = RequirementsConfig()
    lifecycle: LifecycleConfig | None = None
    heartbeat_mode: str = "none"

    @field_validator("heartbeat_mode")
    @classmethod
    def _validate_heartbeat_mode(cls, v: str) -> str:
        """Reject heartbeat_mode values outside the union of supported literals.

        Provider-specific compatibility (e.g. RunPod accepts ``"none"`` +
        ``"graphql-tag"`` only) is verified by
        :func:`kinoforge._adapters.build_heartbeat_endpoint_for` at
        orchestrator dispatch time, where the per-provider module IS
        importable. Config-load can't gate on provider without violating
        core-import-ban.
        """
        allowed = {"none", "graphql-tag", "selfterm-http", "ssh-touch"}
        if v not in allowed:
            raise ValueError(
                f"heartbeat_mode must be one of {sorted(allowed)}; got {v!r}"
            )
        return v
```

- [ ] **Step c.4: Run config tests**

Run: `pixi run test tests/core/test_config.py -v -k heartbeat_mode`
Expected: All 3 new tests PASS.

- [ ] **Step c.5: Write failing tests for RunPod heartbeat delegation**

Append to `tests/providers/test_runpod.py` (after existing imports + helpers):

```python
def test_runpod_constructor_accepts_heartbeat_endpoint(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """Backward-compatible kwarg; default is None (no-op)."""
    from kinoforge.providers.runpod import RunPodProvider
    p_default = RunPodProvider(creds=None, http_post=lambda *_: {}, http_get=lambda _: {})
    assert p_default._heartbeat_endpoint is None

    p_with = RunPodProvider(
        creds=None,
        http_post=lambda *_: {},
        http_get=lambda _: {},
        heartbeat_endpoint=fake_runpod_heartbeat_endpoint,
    )
    assert p_with._heartbeat_endpoint is fake_runpod_heartbeat_endpoint


def test_runpod_heartbeat_no_op_when_endpoint_none() -> None:
    """Backward compat: existing deploys without heartbeat_endpoint set
    must keep the pre-B5a no-op behaviour. heartbeat() must not raise."""
    from kinoforge.providers.runpod import RunPodProvider
    p = RunPodProvider(creds=None, http_post=lambda *_: {}, http_get=lambda _: {})
    p.heartbeat("pod-x")  # no exception
    assert p.last_heartbeat("pod-x") is None


def test_runpod_heartbeat_delegates_to_endpoint(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """heartbeat(id) calls endpoint.write(id, now().astimezone());
    last_heartbeat(id) calls endpoint.read(id) and converts to float."""
    from kinoforge.providers.runpod import RunPodProvider
    p = RunPodProvider(
        creds=None,
        http_post=lambda *_: {},
        http_get=lambda _: {},
        heartbeat_endpoint=fake_runpod_heartbeat_endpoint,
    )
    p.heartbeat("pod-x")
    got_float = p.last_heartbeat("pod-x")
    assert got_float is not None
    # Round-trip: float comes from datetime.timestamp(); fake stored a
    # TZ-aware datetime; reconstructed datetime must be within a second
    # of "now".
    import time
    assert abs(time.time() - got_float) < 5.0


def test_runpod_set_heartbeat_endpoint_installs_post_construction(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """Post-construction injection path used by orchestrator._resolve_provider."""
    from kinoforge.providers.runpod import RunPodProvider
    p = RunPodProvider(creds=None, http_post=lambda *_: {}, http_get=lambda _: {})
    assert p._heartbeat_endpoint is None
    p.set_heartbeat_endpoint(fake_runpod_heartbeat_endpoint)
    assert p._heartbeat_endpoint is fake_runpod_heartbeat_endpoint


def test_runpod_last_heartbeat_returns_none_on_endpoint_returning_none(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """endpoint.read returning None (pod gone / tag absent) must surface
    as last_heartbeat returning None — Layer V classify treats both as
    'no data yet' (HEARTBEAT_UNKNOWN row 7)."""
    from kinoforge.providers.runpod import RunPodProvider
    p = RunPodProvider(
        creds=None,
        http_post=lambda *_: {},
        http_get=lambda _: {},
        heartbeat_endpoint=fake_runpod_heartbeat_endpoint,
    )
    # never wrote — endpoint.read returns None
    assert p.last_heartbeat("never-written") is None
```

- [ ] **Step c.6: Verify the test fails**

Run: `pixi run test tests/providers/test_runpod.py -v -k heartbeat`
Expected: FAIL with `unexpected keyword argument 'heartbeat_endpoint'` and `AttributeError: ... last_heartbeat`.

- [ ] **Step c.7: Add `set_heartbeat_endpoint` to `ComputeProvider` ABC**

Read the existing ABC location: `rg -n "class ComputeProvider" src/kinoforge/core/interfaces.py`

Append to the body of `ComputeProvider` ABC in `src/kinoforge/core/interfaces.py` (the method itself; place after the existing `heartbeat` method or wherever logical):

```python
    def set_heartbeat_endpoint(
        self,
        endpoint: object | None,
    ) -> None:
        """Install a HeartbeatEndpoint post-construction (B5a).

        Default implementation is a no-op so providers that do not yet
        support the heartbeat substrate (e.g. SkyPilot pre-B5b, Local)
        silently accept the call. RunPodProvider overrides to wire the
        endpoint into its ``heartbeat()`` / ``last_heartbeat()`` paths.

        ``endpoint`` is typed as ``object | None`` (not
        ``HeartbeatEndpoint | None``) to keep ``core/interfaces.py`` free
        of any heartbeat-module import — the Protocol satisfaction is
        verified at the call site, not the type-system seam.

        Args:
            endpoint: A :class:`HeartbeatEndpoint`-Protocol-satisfying
                instance, or ``None`` to clear.
        """
        # Default: ignore. Providers that wire heartbeat override.
```

- [ ] **Step c.8: Wire RunPodProvider**

Edit `src/kinoforge/providers/runpod/__init__.py`:

1. Add import at the top of the file (near other `kinoforge.core.*` imports):

```python
from datetime import datetime
from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint
```

2. Modify the `__init__` signature (currently at line 207-235) to add `heartbeat_endpoint`:

```python
    def __init__(
        self,
        creds: CredentialProvider | None = None,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        http_get: Callable[[str], dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = _DEFAULT_BASE_URL,
        heartbeat_endpoint: HeartbeatEndpoint | None = None,
    ) -> None:
```

3. Inside the constructor body, before the closing line, add:

```python
        self._heartbeat_endpoint: HeartbeatEndpoint | None = heartbeat_endpoint
```

4. Replace the no-op `heartbeat` (currently at line 445-452) with the delegation:

```python
    def heartbeat(self, instance_id: str) -> None:
        """Stamp a heartbeat for ``instance_id`` via the configured endpoint.

        No-op when no :class:`HeartbeatEndpoint` has been wired (operator
        opted out via ``compute.heartbeat_mode = "none"``). Otherwise
        delegates to ``endpoint.write(instance_id, datetime.now().astimezone())``
        — TZ-aware local time per project memory
        ``feedback_local_timezone_only``.

        Args:
            instance_id: Pod id whose heartbeat to stamp.

        Raises:
            TransportError: Propagated from the endpoint's wire layer
                (HTTP non-2xx, GraphQL errors). The Layer U
                HeartbeatLoop wraps this in its broad try/except so a
                single bad tick never kills the loop.
        """
        if self._heartbeat_endpoint is None:
            return
        self._heartbeat_endpoint.write(instance_id, datetime.now().astimezone())

    def last_heartbeat(self, instance_id: str) -> float | None:
        """Return the most-recent heartbeat for ``instance_id`` as POSIX epoch.

        The datetime/float seam: :class:`HeartbeatEndpoint` returns
        TZ-aware datetime; ``HeartbeatLoop`` and ``Ledger`` consume float
        POSIX epoch. ``datetime.timestamp()`` is TZ-correct on
        local-aware datetimes (converts to UTC POSIX under the hood;
        ``datetime.fromtimestamp`` reverses).

        Args:
            instance_id: Pod id whose heartbeat to read.

        Returns:
            POSIX-epoch float when the endpoint returned a datetime,
            ``None`` when the endpoint returned None or no endpoint is
            wired.

        Raises:
            TransportError: Propagated from the endpoint's wire layer
                (same envelope as :meth:`heartbeat`).
        """
        if self._heartbeat_endpoint is None:
            return None
        dt = self._heartbeat_endpoint.read(instance_id)
        return dt.timestamp() if dt is not None else None

    def set_heartbeat_endpoint(
        self,
        endpoint: HeartbeatEndpoint | None,
    ) -> None:
        """Install or clear the heartbeat endpoint post-construction.

        Used by :func:`kinoforge.core.orchestrator._resolve_provider` to
        inject the dispatched endpoint after the registry's zero-arg
        factory built the provider.
        """
        self._heartbeat_endpoint = endpoint
```

- [ ] **Step c.9: Verify provider tests pass**

Run: `pixi run test tests/providers/test_runpod.py -v`
Expected: All existing + new tests PASS.

- [ ] **Step c.10: Write failing test for `_adapters.build_heartbeat_endpoint_for`**

Create `tests/test_adapters_heartbeat.py`:

```python
"""Adapter-dispatch tests for B5a heartbeat-endpoint construction.

Verifies the cross-provider dispatch function in _adapters.py that
:func:`kinoforge.core.orchestrator._resolve_provider` calls to build the
right HeartbeatEndpoint instance from cfg.compute.heartbeat_mode.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import AuthError, ValidationError
from kinoforge._adapters import build_heartbeat_endpoint_for


class _StubCreds:
    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self._mapping = mapping or {}

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def _make_cfg(provider: str, heartbeat_mode: str):
    from kinoforge.core.config import (
        ComputeConfig,
        Config,
        EngineConfig,
        LifecycleConfig,
        ModelEntry,
    )
    return Config(
        compute=ComputeConfig(
            provider=provider,
            image="runpod/base:latest",
            lifecycle=LifecycleConfig(budget=10.0),
            heartbeat_mode=heartbeat_mode,
        ),
        engine=EngineConfig(kind="fake"),
        models=[ModelEntry(kind="base", ref="hf:fake/repo:weights.bin")],
    )


def test_mode_none_returns_none() -> None:
    cfg = _make_cfg(provider="runpod", heartbeat_mode="none")
    assert build_heartbeat_endpoint_for(cfg, _StubCreds()) is None


def test_runpod_graphql_tag_builds_endpoint() -> None:
    cfg = _make_cfg(provider="runpod", heartbeat_mode="graphql-tag")
    creds = _StubCreds({"RUNPOD_API_KEY": "sk-fake"})
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint
    got = build_heartbeat_endpoint_for(cfg, creds)
    assert isinstance(got, RunPodGraphQLHeartbeatEndpoint)


def test_runpod_graphql_tag_raises_auth_error_when_key_missing() -> None:
    """Missing RUNPOD_API_KEY is a startup-time failure — operator must
    see the error before the orchestrator boots a real pod."""
    cfg = _make_cfg(provider="runpod", heartbeat_mode="graphql-tag")
    creds = _StubCreds({})  # no key
    with pytest.raises(AuthError, match="RUNPOD_API_KEY"):
        build_heartbeat_endpoint_for(cfg, creds)


def test_runpod_incompatible_mode_raises_validation_error() -> None:
    """RunPod does not accept ssh-touch (SkyPilot-only mode). Caught at
    dispatch, not at config-load (config-load doesn't know providers)."""
    cfg = _make_cfg(provider="runpod", heartbeat_mode="ssh-touch")
    with pytest.raises(ValidationError, match="runpod"):
        build_heartbeat_endpoint_for(cfg, _StubCreds({"RUNPOD_API_KEY": "sk-fake"}))


def test_skypilot_any_mode_other_than_none_raises_not_implemented() -> None:
    """B5b ships the skypilot satisfier. Pre-B5b, any non-none mode on
    SkyPilot must fail-loud rather than silently no-op (operator could
    be expecting heartbeat substrate that doesn't exist yet)."""
    cfg = _make_cfg(provider="skypilot", heartbeat_mode="ssh-touch")
    with pytest.raises(ValidationError, match="B5b"):
        build_heartbeat_endpoint_for(cfg, _StubCreds())
```

- [ ] **Step c.11: Implement `build_heartbeat_endpoint_for`**

Edit `src/kinoforge/_adapters.py`. Update the docstring head + append the dispatch function:

```python
"""Adapter self-registration hub + provider-aware dispatch helpers.

This is the SOLE module in the kinoforge package that imports concrete adapter
implementations.  Every import here triggers the adapter's self-registration
call, making it visible to the registry under its declared name or scheme.

Core and the CLI MUST NOT import concrete adapters directly — they go through
the registry (``kinoforge.core.registry``).  This module is the one permitted
exception: it wires all adapters in one place so the rest of the codebase
stays agnostic of concrete implementations.

Usage::

    import kinoforge._adapters  # noqa: F401

Importing this module is side-effect-only for self-registration; this
module also exposes a small set of cross-provider dispatch helpers
(:func:`build_heartbeat_endpoint_for`) that need to import concrete
providers and therefore cannot live in core.
"""
```

Then at the very bottom of the file:

```python
# --------------------------------------------------------------------------
# Cross-provider dispatch helpers (live here because they import concrete
# provider modules — disallowed everywhere else in kinoforge.core).
# --------------------------------------------------------------------------

from typing import TYPE_CHECKING  # noqa: E402

if TYPE_CHECKING:
    from kinoforge.core.config import Config
    from kinoforge.core.credentials import CredentialProvider
    from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint


def build_heartbeat_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
) -> "HeartbeatEndpoint | None":
    """Build the right :class:`HeartbeatEndpoint` for the configured provider.

    Dispatches on ``(cfg.compute.provider, cfg.compute.heartbeat_mode)``.
    Lives in ``_adapters.py`` because it must import the concrete provider
    satisfier modules (``providers/runpod/heartbeat.py``, etc.), which
    ``kinoforge.core.*`` is forbidden from doing per the core-import-ban
    invariant.

    Args:
        cfg: The loaded kinoforge config (must have a ``compute`` block).
        creds: Credential provider that yields ``RUNPOD_API_KEY`` /
            other provider-specific keys.

    Returns:
        A :class:`HeartbeatEndpoint` instance, or ``None`` when the
        operator selected ``heartbeat_mode = "none"`` (backward-compatible
        no-op heartbeat path).

    Raises:
        AuthError: Mode requires a credential that is not set
            (e.g. ``graphql-tag`` without ``RUNPOD_API_KEY``).
        ValidationError: The (provider, mode) pair is incompatible
            (e.g. RunPod with ``ssh-touch``, which is SkyPilot-only).
    """
    from kinoforge.core.errors import AuthError, ValidationError

    if cfg.compute is None:
        return None
    mode = cfg.compute.heartbeat_mode
    if mode == "none":
        return None
    provider = cfg.compute.provider
    if provider == "runpod":
        if mode == "graphql-tag":
            api_key = creds.get("RUNPOD_API_KEY")
            if api_key is None:
                raise AuthError(
                    "RUNPOD_API_KEY must be set when "
                    "compute.heartbeat_mode == 'graphql-tag'"
                )
            from kinoforge.providers.runpod.heartbeat import (
                RunPodGraphQLHeartbeatEndpoint,
            )
            return RunPodGraphQLHeartbeatEndpoint(api_key=api_key)
        raise ValidationError(
            f"runpod does not support compute.heartbeat_mode={mode!r}; "
            "valid values for runpod: 'none', 'graphql-tag'"
        )
    if provider == "skypilot":
        raise ValidationError(
            f"skypilot heartbeat substrate ships in B5b "
            f"(compute.heartbeat_mode={mode!r}); set to 'none' for now"
        )
    if provider == "local":
        # LocalProvider's in-memory _heartbeats dict already covers
        # local-mode tests; no separate substrate satisfier needed.
        return None
    raise ValidationError(
        f"unknown provider for heartbeat dispatch: {provider!r}"
    )
```

- [ ] **Step c.12: Run dispatch tests**

Run: `pixi run test tests/test_adapters_heartbeat.py -v`
Expected: All 5 tests PASS.

- [ ] **Step c.13: Write failing integration test in `test_heartbeat_loop.py`**

Append to `tests/core/test_heartbeat_loop.py`:

```python
def test_heartbeat_loop_with_runpod_and_fake_endpoint_round_trips(
    fake_runpod_heartbeat_endpoint,
) -> None:
    """End-to-end Layer U → B5a chain:

      HeartbeatLoop.tick()
        -> provider.heartbeat(id)
          -> endpoint.write(id, now)
        -> provider.last_heartbeat(id)
          -> endpoint.read(id) -> .timestamp() -> float
        -> ledger.touch(id, last_heartbeat=float, heartbeat_thread_tick=now)

    Bug catches: timestamp/datetime conversion errors, fake-fixture
    plumbing errors, FakeClock advance not propagating, ledger.touch
    silently dropping the last_heartbeat kwarg.
    """
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.heartbeat_loop import HeartbeatLoop
    from kinoforge.providers.runpod import RunPodProvider

    clock = FakeClock(start=1_000_000.0)
    provider = RunPodProvider(
        creds=None,
        http_post=lambda *_: {},
        http_get=lambda _: {},
        heartbeat_endpoint=fake_runpod_heartbeat_endpoint,
    )

    class _SpyLedger:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def touch(self, instance_id, **kwargs):  # noqa: ANN001
            self.calls.append({"id": instance_id, **kwargs})
            return True

    ledger = _SpyLedger()

    loop = HeartbeatLoop(
        ledger=ledger,
        provider=provider,
        instance_id="pod-x",
        interval_s=1.0,
        clock=clock,
    )

    # Drive 3 tick cycles manually — don't start the thread.
    loop._tick_once()
    clock.advance(1.0)
    loop._tick_once()
    clock.advance(1.0)
    loop._tick_once()

    # 3 ticks → 3 ledger touches
    assert len(ledger.calls) == 3
    # last_heartbeat must be populated on every tick (no AttributeError swallow)
    for call in ledger.calls:
        assert call["last_heartbeat"] is not None
        assert isinstance(call["last_heartbeat"], float)
    # Monotonically advancing (matches our FakeClock advance of 1s/tick)
    lhs = [float(c["last_heartbeat"]) for c in ledger.calls]
    assert lhs[1] >= lhs[0]
    assert lhs[2] >= lhs[1]
```

- [ ] **Step c.14: Write failing test for orchestrator's `_resolve_provider` wiring**

Append to `tests/core/test_orchestrator.py`:

```python
def test_resolve_provider_injects_heartbeat_endpoint_when_mode_set(
    monkeypatch,
) -> None:
    """When cfg.compute.heartbeat_mode != 'none' and creds are present,
    _resolve_provider must call build_heartbeat_endpoint_for and inject
    the result onto the provider via set_heartbeat_endpoint.

    Bug catches: orchestrator returning a no-op-heartbeat RunPodProvider
    even when the operator set compute.heartbeat_mode = 'graphql-tag' —
    the operator believes substrate is on but Layer V keeps emitting
    HEARTBEAT_UNKNOWN.
    """
    from kinoforge.core.config import (
        ComputeConfig,
        Config,
        EngineConfig,
        LifecycleConfig,
        ModelEntry,
    )
    from kinoforge.core.orchestrator import _resolve_provider

    cfg = Config(
        compute=ComputeConfig(
            provider="runpod",
            image="runpod/base:latest",
            lifecycle=LifecycleConfig(budget=10.0),
            heartbeat_mode="graphql-tag",
        ),
        engine=EngineConfig(kind="fake"),
        models=[ModelEntry(kind="base", ref="hf:fake/repo:weights.bin")],
    )
    # Ensure RUNPOD_API_KEY is visible to EnvCredentialProvider.
    monkeypatch.setenv("RUNPOD_API_KEY", "sk-fake-for-test")

    provider = _resolve_provider(cfg, provider=None)
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint
    assert provider._heartbeat_endpoint is not None
    assert isinstance(provider._heartbeat_endpoint, RunPodGraphQLHeartbeatEndpoint)
```

- [ ] **Step c.15: Wire `_resolve_provider`**

Edit `src/kinoforge/core/orchestrator.py` — modify `_resolve_provider` (lines 123-142):

```python
def _resolve_provider(
    cfg: Config,
    provider: ComputeProvider | None,
) -> ComputeProvider:
    """Return the injected provider or resolve from the registry.

    Args:
        cfg: The loaded kinoforge config.  Must have a ``compute`` block.
        provider: Optional pre-constructed provider (test injection).

    Returns:
        A ready ``ComputeProvider`` instance, with the B5a heartbeat
        endpoint installed when ``cfg.compute.heartbeat_mode != "none"``.

    Raises:
        ValueError: ``cfg.compute`` is ``None`` (called on a hosted config).
        AuthError: heartbeat mode requires a credential that is not set.
        ValidationError: provider does not support the configured
            heartbeat_mode.
    """
    if provider is not None:
        return provider
    if cfg.compute is None:
        raise ValueError(
            "cannot resolve provider: cfg.compute is None (hosted engine path)"
        )
    p = registry.get_provider(cfg.compute.provider)()
    # B5a: install the heartbeat substrate endpoint when the operator
    # opted in via compute.heartbeat_mode. Lives here (not in the registry
    # factory) because the factory is zero-arg by ABC and the dispatch
    # needs cfg + creds. The dispatch lives in _adapters because importing
    # the concrete satisfier module from core would violate core-import-ban.
    if cfg.compute.heartbeat_mode != "none":
        from kinoforge._adapters import build_heartbeat_endpoint_for
        endpoint = build_heartbeat_endpoint_for(cfg, EnvCredentialProvider())
        p.set_heartbeat_endpoint(endpoint)
    return p
```

- [ ] **Step c.16: Run all Task c tests**

Run:
```bash
pixi run test tests/providers/test_runpod.py \
              tests/core/test_heartbeat_loop.py \
              tests/core/test_config.py \
              tests/core/test_orchestrator.py \
              tests/test_adapters_heartbeat.py -v
```
Expected: All tests PASS.

- [ ] **Step c.17: Lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/providers/runpod/__init__.py src/kinoforge/core/config.py src/kinoforge/_adapters.py src/kinoforge/core/orchestrator.py src/kinoforge/core/interfaces.py`
Expected: All hooks PASS.

- [ ] **Step c.18: Commit**

```bash
git add src/kinoforge/providers/runpod/__init__.py \
        src/kinoforge/core/config.py \
        src/kinoforge/_adapters.py \
        src/kinoforge/core/orchestrator.py \
        src/kinoforge/core/interfaces.py \
        tests/providers/test_runpod.py \
        tests/core/test_config.py \
        tests/core/test_heartbeat_loop.py \
        tests/core/test_orchestrator.py \
        tests/test_adapters_heartbeat.py
git commit -m "$(cat <<'EOF'
feat(b5a): wire RunPod heartbeat endpoint + YAML gate + dispatch

ComputeConfig.heartbeat_mode (literal-set: none, graphql-tag,
selfterm-http, ssh-touch). Default "none" preserves backward compat.

RunPodProvider grows the heartbeat_endpoint kwarg + set_heartbeat_endpoint
setter. heartbeat() and last_heartbeat() now delegate to the endpoint
when set; no-op when None. ComputeProvider ABC grows a default-no-op
set_heartbeat_endpoint so other providers ignore the call silently.

_adapters.build_heartbeat_endpoint_for(cfg, creds) is the cross-provider
dispatch — lives in _adapters because it must import concrete satisfier
modules (core-import-ban).

orchestrator._resolve_provider injects the endpoint post-factory when
cfg.compute.heartbeat_mode != "none". HeartbeatLoop 3-tick integration
test confirms the end-to-end Layer U → B5a chain produces monotonically-
advancing ledger.last_heartbeat floats.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task d: Layer V classify hook + HEARTBEAT_SUBSTRATE_MISSING verdict + act_on_verdict WARN arm

**Goal:** Make Layer V's `classify` and `act_on_verdict` aware of the substrate-missing axis. New `HEARTBEAT_SUBSTRATE_MISSING` verdict; conservative-on-ignorance contract — sweeper MUST NEVER reap on this verdict.

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (add verdict + gate in `classify`)
- Modify: `src/kinoforge/core/reaper_actor.py` (no-destroy + WARN-once arm in `act_on_verdict`)
- Modify: `tests/core/test_reaper.py` (verdict-table tests)

**Acceptance Criteria:**
- [ ] `Verdict.HEARTBEAT_SUBSTRATE_MISSING` exists with value `"HEARTBEAT_SUBSTRATE_MISSING"`.
- [ ] Default `Policy.act_verdicts` does NOT contain `HEARTBEAT_SUBSTRATE_MISSING` (sweeper never reaps it).
- [ ] `classify` reads `entry.get("provider_kind")` and consults `provider_heartbeat_supported` before returning a heartbeat-data-absent verdict.
- [ ] When `last_heartbeat is None` and `provider_heartbeat_supported(provider_kind) is False`: returns `HEARTBEAT_SUBSTRATE_MISSING`.
- [ ] When `last_heartbeat is None` and `provider_heartbeat_supported(provider_kind) is True`: returns `HEARTBEAT_UNKNOWN` (operator-opted-out path).
- [ ] When `provider_kind` is missing from the entry (legacy ledger entries): treats as `HEARTBEAT_UNKNOWN` (defensive — never emit the new verdict on legacy entries that predate `provider_kind` persistence).
- [ ] `act_on_verdict` on `HEARTBEAT_SUBSTRATE_MISSING` does NOT call `destroy_instance` or `ledger.forget`; emits WARNING log line.
- [ ] WARNING log line is deduped per `(provider_kind, instance_id)` — running 100 sweeps produces 1 line, not 100.

**Verify:** `pixi run test tests/core/test_reaper.py -v && pixi run typecheck && pixi run pre-commit run --files src/kinoforge/core/reaper.py src/kinoforge/core/reaper_actor.py`

**Steps:**

- [ ] **Step d.1: Write failing classify tests**

Append to `tests/core/test_reaper.py`:

```python
def test_classify_emits_substrate_missing_on_unsupported_provider() -> None:
    """SkyPilot pre-B5b: provider_kind='skypilot', last_heartbeat=None.
    Must NOT emit HEARTBEAT_UNKNOWN — that would let a future B1 sweeper
    reap a live working SkyPilot pod once HEARTBEAT_UNKNOWN is added to
    the apply policy. Emit the dedicated verdict instead."""
    from kinoforge.core.reaper import Verdict, classify
    entry = {
        "id": "cluster-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"cluster-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_classify_emits_heartbeat_unknown_on_supported_provider_with_no_data() -> None:
    """RunPod with compute.heartbeat_mode='none' (operator opted out):
    provider_kind='runpod', last_heartbeat=None. Operator made the
    choice — sweeper's dead-man fallback (IDLE_REAP after dead-man
    window) is the next layer of defence. Keep HEARTBEAT_UNKNOWN."""
    from kinoforge.core.reaper import Verdict, classify
    entry = {
        "id": "pod-x",
        "provider_kind": "runpod",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"pod-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_UNKNOWN


def test_classify_treats_missing_provider_kind_as_unknown() -> None:
    """Legacy ledger entries pre-Layer-S persistence may lack provider_kind.
    Defensive: do NOT emit HEARTBEAT_SUBSTRATE_MISSING on legacy entries —
    that would block operator-driven reaps of orphaned legacy pods."""
    from kinoforge.core.reaper import Verdict, classify
    entry = {
        "id": "legacy-x",
        # provider_kind absent
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    v = classify(
        entry,
        live_pod_ids={"legacy-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.HEARTBEAT_UNKNOWN


def test_classify_emits_live_with_fresh_heartbeat_on_runpod() -> None:
    """Smoke: substrate working end-to-end. provider_kind='runpod',
    fresh sentinel + fresh heartbeat → LIVE (the path the operator
    actually wants)."""
    from kinoforge.core.reaper import Verdict, classify
    entry = {
        "id": "pod-x",
        "provider_kind": "runpod",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": 1_990.0,  # 10s old → fresh under 90s window
        "last_heartbeat": 1_990.0,
    }
    v = classify(
        entry,
        live_pod_ids={"pod-x"},
        now=2_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=300.0,
    )
    assert v == Verdict.LIVE


def test_default_policy_does_not_act_on_substrate_missing() -> None:
    """B1 sweeper inherits this Policy. Sweeper must NEVER reap on
    HEARTBEAT_SUBSTRATE_MISSING — operator cannot fix the substrate
    by destroying the pod."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY, Verdict
    assert Verdict.HEARTBEAT_SUBSTRATE_MISSING not in DEFAULT_APPLY_POLICY.act_verdicts
```

- [ ] **Step d.2: Verify the tests fail**

Run: `pixi run test tests/core/test_reaper.py -v -k substrate_missing`
Expected: FAIL with `AttributeError: HEARTBEAT_SUBSTRATE_MISSING`.

- [ ] **Step d.3: Add the verdict + classify gate**

Edit `src/kinoforge/core/reaper.py`:

1. Add the new verdict to the `Verdict` enum (line 21-34):

```python
class Verdict(StrEnum):
    """Possible classification outcomes for a single ledger entry.

    Insertion order is part of the public contract — Layer W daemons
    and Layer Y orchestrator hooks may serialise verdict values.
    """

    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    HEARTBEAT_SUBSTRATE_MISSING = "HEARTBEAT_SUBSTRATE_MISSING"  # NEW (B5a)
    UNROUTABLE = "UNROUTABLE"
```

2. Update the `DEFAULT_STRICT_VERDICTS` block to include the new verdict (so `--strict-mode` flags it as a not-actionable state):

```python
DEFAULT_STRICT_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.UNROUTABLE,
        Verdict.HEARTBEAT_UNKNOWN,
        Verdict.HEARTBEAT_SUBSTRATE_MISSING,  # NEW (B5a)
    }
)
```

3. Add the import at the top:

```python
from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
```

4. Modify the heartbeat-data-absent branch in `classify` (currently at lines 168-173):

Replace:
```python
    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")

    # Row 7 — heartbeat data unavailable
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        return Verdict.HEARTBEAT_UNKNOWN
```

With:
```python
    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")

    # Row 7 — heartbeat data unavailable.
    # B5a: gate on provider substrate support. When the entry's provider
    # has no wire-level HeartbeatEndpoint shipped yet (e.g. SkyPilot
    # pre-B5b), emit HEARTBEAT_SUBSTRATE_MISSING so consumers do not
    # treat the absence as actionable. The provider_kind field is set
    # by Layer S Ledger.record; legacy entries that pre-date it fall
    # through to HEARTBEAT_UNKNOWN — that path is operator-opted-in
    # via cfg.compute.heartbeat_mode="none".
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        provider_kind = entry.get("provider_kind")
        if (
            provider_kind is not None
            and not provider_heartbeat_supported(str(provider_kind))
        ):
            return Verdict.HEARTBEAT_SUBSTRATE_MISSING
        return Verdict.HEARTBEAT_UNKNOWN
```

- [ ] **Step d.4: Update classify's docstring**

Edit the `Returns:` block of `classify` (around line 142-150) to enumerate the new verdict:

```python
    Returns:
        One of the seven non-UNROUTABLE Verdict values:
        LIVE, IDLE_REAP, ORPHAN_REAP, OVERAGE_REAP, STALE_LEDGER,
        HEARTBEAT_UNKNOWN, or HEARTBEAT_SUBSTRATE_MISSING. UNROUTABLE
        is assigned by :func:`kinoforge.core.reaper_actor.sweep` when
        provider lookup fails, never by classify itself. Callers may
        rely on this exclusion when partitioning.
```

- [ ] **Step d.5: Verify classify tests pass**

Run: `pixi run test tests/core/test_reaper.py -v -k substrate_missing` and `pixi run test tests/core/test_reaper.py -v -k heartbeat_unknown_on_supported`
Expected: All new tests PASS, existing tests unaffected.

- [ ] **Step d.6: Write failing act_on_verdict tests**

Append to `tests/core/test_reaper_actor.py` (or wherever `act_on_verdict` tests live — verify with `rg -l 'act_on_verdict' tests/`):

```python
def test_act_on_verdict_substrate_missing_does_not_destroy(
    monkeypatch,
) -> None:
    """The conservative-on-ignorance contract: operator cannot fix the
    substrate by destroying the pod; sweeper must skip.

    Bug catch: a forgotten case-arm could fall through to the destroy
    branch and silently kill working SkyPilot pods during the
    B5a-shipped-B5b-pending window.
    """
    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict

    # Build fakes for store, ledger, provider — only the destroy and
    # forget calls matter for this assertion.
    class _DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _StubStore:
        def acquire_lock(self, _key, ttl_s=30.0):  # noqa: ANN001
            return _DummyLock()

    destroy_calls: list[str] = []
    forget_calls: list[str] = []

    class _StubProvider:
        def list_instances(self):
            from kinoforge.core.interfaces import Instance
            return [Instance(id="pod-x", status="ready", tags={})]

        def destroy_instance(self, instance_id):  # noqa: ANN001
            destroy_calls.append(instance_id)

    class _StubLedger:
        def forget(self, instance_id):  # noqa: ANN001
            forget_calls.append(instance_id)

    entry = {
        "id": "pod-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    result = act_on_verdict(
        store=_StubStore(),
        ledger=_StubLedger(),
        provider=_StubProvider(),
        entry=entry,
        snapshot_verdict=Verdict.HEARTBEAT_SUBSTRATE_MISSING,
        thresholds={
            "idle_timeout_s": 600.0,
            "max_lifetime_s": 18_000.0,
            "heartbeat_interval_s": 30.0,
            "grace_after_session_s": 300.0,
        },
        clock=FakeClock(start=2_000.0),
    )
    assert destroy_calls == []
    assert forget_calls == []
    assert result.action == "no_op"
    assert result.applied_verdict == Verdict.HEARTBEAT_SUBSTRATE_MISSING


def test_act_on_verdict_substrate_missing_warns_once_per_pair(caplog) -> None:
    """Across N sweeps over the same (provider_kind, instance_id) pair,
    only the FIRST WARNING fires. Operators don't want 100 lines of
    'skypilot has no substrate' per minute."""
    import logging

    from kinoforge.core.clock import FakeClock
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import act_on_verdict, reset_warning_dedup

    reset_warning_dedup()  # test-helper from reaper_actor

    class _DummyLock:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _StubStore:
        def acquire_lock(self, _key, ttl_s=30.0):  # noqa: ANN001
            return _DummyLock()

    class _StubProvider:
        def list_instances(self):
            from kinoforge.core.interfaces import Instance
            return [Instance(id="cluster-x", status="ready", tags={})]

        def destroy_instance(self, _instance_id):  # noqa: ANN001
            pass

    class _StubLedger:
        def forget(self, _instance_id):  # noqa: ANN001
            pass

    entry = {
        "id": "cluster-x",
        "provider_kind": "skypilot",
        "created_at": 1_000.0,
        "heartbeat_thread_tick": None,
        "last_heartbeat": None,
    }
    caplog.set_level(logging.WARNING, logger="kinoforge.core.reaper_actor")

    for _ in range(5):
        act_on_verdict(
            store=_StubStore(),
            ledger=_StubLedger(),
            provider=_StubProvider(),
            entry=entry,
            snapshot_verdict=Verdict.HEARTBEAT_SUBSTRATE_MISSING,
            thresholds={
                "idle_timeout_s": 600.0,
                "max_lifetime_s": 18_000.0,
                "heartbeat_interval_s": 30.0,
                "grace_after_session_s": 300.0,
            },
            clock=FakeClock(start=2_000.0),
        )

    # Only one WARNING log line per (provider_kind, instance_id)
    relevant = [
        r for r in caplog.records
        if "heartbeat substrate" in r.getMessage().lower()
    ]
    assert len(relevant) == 1
```

- [ ] **Step d.7: Verify those tests fail**

Run: `pixi run test tests/core/test_reaper_actor.py -v -k substrate_missing`
Expected: FAIL with `ImportError: cannot import name 'reset_warning_dedup'` AND/OR assertion failure that destroy was called.

- [ ] **Step d.8: Implement the no-destroy + WARN-once arm in `act_on_verdict`**

Edit `src/kinoforge/core/reaper_actor.py`:

1. Add at the top of the module (after existing imports):

```python
import logging

_log = logging.getLogger(__name__)

# B5a: dedup keys for HEARTBEAT_SUBSTRATE_MISSING warnings. Per
# (provider_kind, instance_id) — operators get one line per pod, not
# one per tick. Module-level state is acceptable because the dedup is
# best-effort (a process restart resets it; the alternative would be
# leaking dedup state into the ledger).
_WARNED_SUBSTRATE_MISSING: set[tuple[str, str]] = set()


def reset_warning_dedup() -> None:
    """Clear the substrate-missing WARN dedup set.

    Test helper. Production code does not call this — the dedup persists
    for the life of the process per the documented best-effort contract.
    """
    _WARNED_SUBSTRATE_MISSING.clear()
```

2. Modify the `try`/`except` block in `act_on_verdict` (lines 134-148). Insert a new arm BEFORE the existing `if v2 in {Verdict.IDLE_REAP, ...}` block:

```python
        try:
            if v2 == Verdict.HEARTBEAT_SUBSTRATE_MISSING:
                # Conservative-on-ignorance. The substrate hasn't shipped
                # for this provider yet (e.g. SkyPilot pre-B5b). Operator
                # cannot fix it by destroying the pod. Skip + WARN-once.
                provider_kind = str(entry.get("provider_kind", ""))
                dedup_key = (provider_kind, instance_id)
                if dedup_key not in _WARNED_SUBSTRATE_MISSING:
                    _WARNED_SUBSTRATE_MISSING.add(dedup_key)
                    _log.warning(
                        "provider %r has no heartbeat substrate; "
                        "skipping reap decision for %s (B5b pending)",
                        provider_kind,
                        instance_id,
                    )
                action = "no_op"
            elif v2 in {Verdict.IDLE_REAP, Verdict.OVERAGE_REAP, Verdict.ORPHAN_REAP}:
                destroy_confirmed(provider, instance_id, sleep=lambda _: None)
                ledger.forget(instance_id)
                action = "destroyed_and_forgot"
            elif v2 == Verdict.STALE_LEDGER:
                ledger.forget(instance_id)
                action = "forgot"
            else:
                # LIVE / HEARTBEAT_UNKNOWN → no_op.
                # UNROUTABLE is unreachable here: classify never returns it and
                # sweep skips UNROUTABLE entries (no provider to invoke). The
                # `forgot_unroutable` path lives in sweep() — see Layer V T5.
                action = "no_op"
        except TeardownError as exc:
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=v2,
                action="failed",
                reason=str(exc),
            )
```

- [ ] **Step d.9: Verify act_on_verdict tests pass**

Run: `pixi run test tests/core/test_reaper_actor.py -v`
Expected: All tests PASS.

- [ ] **Step d.10: Run full Task d test sweep**

Run: `pixi run test tests/core/test_reaper.py tests/core/test_reaper_actor.py -v`
Expected: All tests PASS.

- [ ] **Step d.11: Lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/reaper.py src/kinoforge/core/reaper_actor.py tests/core/test_reaper.py tests/core/test_reaper_actor.py`
Expected: All hooks PASS.

- [ ] **Step d.12: Commit**

```bash
git add src/kinoforge/core/reaper.py \
        src/kinoforge/core/reaper_actor.py \
        tests/core/test_reaper.py \
        tests/core/test_reaper_actor.py
git commit -m "$(cat <<'EOF'
feat(b5a): Layer V HEARTBEAT_SUBSTRATE_MISSING verdict + WARN-once arm

classify() consults provider_heartbeat_supported(provider_kind) before
emitting the heartbeat-absent verdict. Substrate-missing providers
(SkyPilot pre-B5b) get the new HEARTBEAT_SUBSTRATE_MISSING verdict
distinct from HEARTBEAT_UNKNOWN — operator-opted-out path (RunPod with
heartbeat_mode="none") still surfaces as UNKNOWN.

Legacy entries without provider_kind fall through to UNKNOWN (defensive:
don't block manual reaps on legacy ledger state).

act_on_verdict gains a no-destroy + WARN-once arm for the new verdict.
Dedup key is (provider_kind, instance_id) so operators see one line per
pod, not 100 lines per minute.

DEFAULT_STRICT_VERDICTS extended to include the new verdict.
DEFAULT_APPLY_POLICY untouched — sweeper never reaps on substrate-missing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task e: Cross-provider parity test suite

**Goal:** Freeze the substrate Protocol contract from both sides (RunPod sub-second + SkyPilot second-precision-mtime). Lands in parallel with Tasks b-d; depends only on Task a.

**Files:**
- Create: `tests/providers/test_heartbeat_parity.py`

**Acceptance Criteria:**
- [ ] 9 parity invariants × 3 fakes = 27 test cases produced via parametrize.
- [ ] `test_read_of_never_written_returns_none` passes for all three fakes.
- [ ] `test_write_then_read_round_trips_wall_clock` passes for all three (SkyPilot tolerates 1s truncation).
- [ ] `test_double_write_same_ts_is_idempotent` passes for all three.
- [ ] `test_write_then_overwrite_returns_latest` passes for all three.
- [ ] `test_read_after_instance_destroyed_returns_none` passes for all three.
- [ ] `test_transport_failure_raises_TransportError_on_write` passes for all three.
- [ ] `test_transport_failure_raises_TransportError_on_read` passes for all three.
- [ ] `test_second_precision_minimum` passes for all three.
- [ ] `test_overwrite_does_not_create_second_slot` passes for all three (proves the storage is a single per-id slot, not a log).

**Verify:** `pixi run test tests/providers/test_heartbeat_parity.py -v && pixi run typecheck && pixi run pre-commit run --files tests/providers/test_heartbeat_parity.py`

**Steps:**

- [ ] **Step e.1: Add `LocalHeartbeatEndpoint` test adapter**

Append to `tests/providers/conftest.py` (next to the two fakes from Task a):

```python
@dataclass
class LocalHeartbeatEndpoint:
    """Thin Protocol-shaped adapter around a dict, for parity tests only.

    LocalProvider already manages heartbeats in an in-memory dict and
    has no production reason to grow a HeartbeatEndpoint satisfier
    (offline tests use LocalProvider directly). This adapter exists so
    the cross-provider parity test (Task e) can parametrize across all
    three satisfiers symmetrically.

    NOT registered as a production satisfier — test fixture only.
    """

    _slots: dict[str, datetime] = field(default_factory=dict)
    _fail_on_write: bool = False
    _fail_on_read: bool = False

    def write(self, instance_id: str, ts_local: datetime) -> None:
        if self._fail_on_write:
            self._fail_on_write = False
            raise TransportError(
                f"LocalHeartbeatEndpoint: injected write failure for {instance_id}"
            )
        self._slots[instance_id] = ts_local

    def read(self, instance_id: str) -> datetime | None:
        if self._fail_on_read:
            self._fail_on_read = False
            raise TransportError(
                f"LocalHeartbeatEndpoint: injected read failure for {instance_id}"
            )
        return self._slots.get(instance_id)

    def inject_transport_failure(self, method: Literal["read", "write"]) -> None:
        if method == "read":
            self._fail_on_read = True
        elif method == "write":
            self._fail_on_write = True
        else:
            raise ValueError(f"method must be 'read' or 'write'; got {method!r}")

    def destroy_instance(self, instance_id: str) -> None:
        self._slots.pop(instance_id, None)
```

- [ ] **Step e.2: Write the parity test**

Create `tests/providers/test_heartbeat_parity.py`:

```python
"""Cross-provider parity tests for HeartbeatEndpoint (B5a Task e).

Parametrizes the Protocol-contract invariants across three fakes:
- LocalHeartbeatEndpoint (in-process dict; sub-second precision)
- FakeRunPodHeartbeatEndpoint (mirrors GraphQL-tag ISO round-trip;
  sub-second precision)
- FakeSkyPilotHeartbeatEndpoint (mirrors filesystem-mtime stat;
  second-precision truncation, SSH-refused transport failures)

The parity test is the load-bearing artifact: it freezes the contract
from BOTH the RunPod (sub-second) and SkyPilot (second-precision) sides
BEFORE B5b's wire implementation lands. When B5b ships, the parity
suite must still pass; only the SkyPilot real wire replaces the fake
in the live-path injection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint

EndpointFactory = Callable[[], HeartbeatEndpoint]


def _local_factory() -> HeartbeatEndpoint:
    # Imported lazily so the conftest.py fixtures registered by Task a's
    # `tests/providers/conftest.py` are guaranteed available.
    from tests.providers.conftest import LocalHeartbeatEndpoint
    return LocalHeartbeatEndpoint()


def _runpod_factory() -> HeartbeatEndpoint:
    from tests.providers.conftest import FakeRunPodHeartbeatEndpoint
    return FakeRunPodHeartbeatEndpoint()


def _skypilot_factory() -> HeartbeatEndpoint:
    from tests.providers.conftest import FakeSkyPilotHeartbeatEndpoint
    return FakeSkyPilotHeartbeatEndpoint()


_FACTORIES: dict[str, EndpointFactory] = {
    "local": _local_factory,
    "runpod": _runpod_factory,
    "skypilot": _skypilot_factory,
}


@pytest.fixture(params=list(_FACTORIES.keys()))
def endpoint(request: pytest.FixtureRequest) -> HeartbeatEndpoint:
    """Parametrized fixture: one HeartbeatEndpoint per registered fake."""
    return _FACTORIES[request.param]()


def test_read_of_never_written_returns_none(endpoint: HeartbeatEndpoint) -> None:
    """Protocol invariant: a slot that was never written reads as None,
    NOT raises TransportError. This is what makes B3 cross-session
    warm-reuse safe — a fresh CLI invocation queries the slot, gets None,
    and proceeds to provision a new pod (rather than crashing)."""
    assert endpoint.read("never-written") is None


def test_write_then_read_round_trips_wall_clock(endpoint: HeartbeatEndpoint) -> None:
    """Tolerates SkyPilot's second-precision truncation by comparing
    at second granularity (the floor for any consumer per substrate
    invariant)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    got = endpoint.read("pod-x")
    assert got is not None
    assert got == ts


def test_double_write_same_ts_is_idempotent(endpoint: HeartbeatEndpoint) -> None:
    """Writing the same ts twice must not raise (e.g. via a 'duplicate
    key' constraint in a hypothetical satisfier that misuses a unique
    index)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    endpoint.write("pod-x", ts)
    assert endpoint.read("pod-x") == ts


def test_write_then_overwrite_returns_latest(endpoint: HeartbeatEndpoint) -> None:
    """The slot is single-value, not a log. Latest write wins."""
    from datetime import timedelta
    ts1 = datetime.now().astimezone().replace(microsecond=0)
    ts2 = ts1 + timedelta(seconds=5)
    endpoint.write("pod-x", ts1)
    endpoint.write("pod-x", ts2)
    assert endpoint.read("pod-x") == ts2


def test_read_after_instance_destroyed_returns_none(
    endpoint: HeartbeatEndpoint,
) -> None:
    """When the underlying pod/cluster is gone, read returns None — not
    a stale value, not a TransportError. Layer V relies on this to
    classify STALE_LEDGER vs LIVE without false positives."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    # destroy_instance is a test-helper on the fakes (not on the
    # production Protocol). Mirrors real provider teardown.
    endpoint.destroy_instance("pod-x")  # type: ignore[attr-defined]
    assert endpoint.read("pod-x") is None


def test_transport_failure_raises_TransportError_on_write(
    endpoint: HeartbeatEndpoint,
) -> None:
    """The substrate exception is TransportError — concrete satisfiers
    that swallow vendor exceptions silently break the Layer U
    HeartbeatLoop's broad try/except envelope."""
    # Branch on fake kind: SkyPilot uses inject_ssh_refused (it can't
    # selectively fail just write — SSH refused breaks both).
    if hasattr(endpoint, "inject_ssh_refused"):
        endpoint.inject_ssh_refused()  # type: ignore[attr-defined]
    else:
        endpoint.inject_transport_failure("write")  # type: ignore[attr-defined]
    with pytest.raises(TransportError):
        endpoint.write("pod-x", datetime.now().astimezone())


def test_transport_failure_raises_TransportError_on_read(
    endpoint: HeartbeatEndpoint,
) -> None:
    """Same as above, read direction."""
    if hasattr(endpoint, "inject_ssh_refused"):
        endpoint.inject_ssh_refused()  # type: ignore[attr-defined]
    else:
        endpoint.inject_transport_failure("read")  # type: ignore[attr-defined]
    with pytest.raises(TransportError):
        endpoint.read("pod-x")


def test_second_precision_minimum(endpoint: HeartbeatEndpoint) -> None:
    """The contract floor: wall-clock round-trip preserves at LEAST
    1-second precision. Anything less precise breaks Layer V's
    dead-man window math (window is heartbeat_interval_s * 3,
    minimum 30s — second-precision is comfortably below that)."""
    ts = datetime.now().astimezone().replace(microsecond=0)
    endpoint.write("pod-x", ts)
    got = endpoint.read("pod-x")
    assert got is not None
    assert abs((got - ts).total_seconds()) < 1.0


def test_overwrite_does_not_create_second_slot(
    endpoint: HeartbeatEndpoint,
) -> None:
    """Belt-and-suspenders for the single-slot contract: write to id A,
    write to id B, write to id A again — neither id sees the other's
    values."""
    from datetime import timedelta
    a1 = datetime.now().astimezone().replace(microsecond=0)
    b1 = a1 + timedelta(seconds=1)
    a2 = a1 + timedelta(seconds=2)
    endpoint.write("A", a1)
    endpoint.write("B", b1)
    endpoint.write("A", a2)
    assert endpoint.read("A") == a2
    assert endpoint.read("B") == b1
```

- [ ] **Step e.3: Run the parity suite**

Run: `pixi run test tests/providers/test_heartbeat_parity.py -v`
Expected: 9 invariants × 3 fakes = 27 test cases all PASS.

- [ ] **Step e.4: Lint**

Run: `pixi run pre-commit run --files tests/providers/test_heartbeat_parity.py tests/providers/conftest.py`
Expected: All hooks PASS.

- [ ] **Step e.5: Commit**

```bash
git add tests/providers/test_heartbeat_parity.py tests/providers/conftest.py
git commit -m "$(cat <<'EOF'
test(b5a): cross-provider HeartbeatEndpoint parity suite

9 Protocol-contract invariants × 3 fakes (Local + FakeRunPod +
FakeSkyPilot) = 27 parametrized test cases. Freezes the substrate
contract from both the sub-second (RunPod GraphQL ISO) and
second-precision (SkyPilot mtime) sides BEFORE B5b's wire-level
SkyPilot satisfier ships. When B5b lands the parity suite must still
pass; only the wire-level real version replaces the fake.

Drift mitigation per spec §11 Risk 4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task f: Live RunPod heartbeat smoke + spec rate-limit annotation + PROGRESS closeout

**Goal:** Real-cloud verification that `RunPodGraphQLHeartbeatEndpoint` round-trips wall-clock + characterize GraphQL rate-limit headroom. ≤ $0.05 live spend per session.

**Pre-conditions:**
- Tasks a, b, c, d, e all merged.
- `pixi run preflight` exits 0 (no live pods, clean tree, RUNPOD creds loaded from `.env`).
- RED-scaffold (smoke script + failing test) committed BEFORE live invocation per `CLAUDE.md` Durability rule.

**Files:**
- Create: `tests/live/test_runpod_heartbeat_live.py`
- Amend: `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` (P50/P99 latency capture in §9; rate-limit observation in §11 Risk 2)
- Update: `PROGRESS.md` (close out per spec §15)

**Acceptance Criteria:**
- [ ] Bare CPU-only RunPod pod spawned (cheapest tier — verify ≤ $0.05/hr in console before commit).
- [ ] 60s session lifetime cap.
- [ ] Two heartbeat ticks with ≥ 1s spacing.
- [ ] Each tick's write→read round trip succeeds and the read value matches the write within 1s.
- [ ] Per-tick wall-clock latency captured for P50 + P99 reporting.
- [ ] 429 detection: if RunPod GraphQL returns 429 within the 60s window, test fails loud + records the rate-limit floor.
- [ ] Teardown: pod destroyed via existing `RunPodProvider.destroy_instance` path; `pixi run preflight` exits 0 after the run.
- [ ] Ledger entry's final `last_heartbeat` matches the final write within 1s.
- [ ] Spec amended with measured P50 + P99 + rate-limit observation.
- [ ] `PROGRESS.md` updated per spec §15.
- [ ] Total session spend ≤ $0.05 (verified in RunPod billing console post-run).

**Verify (manual orchestration — operator runs `pixi run preflight` between RED-scaffold commit and live invocation):**

```
pixi run preflight  # exit 0 required
KINOFORGE_LIVE_RUNPOD=1 pixi run -e live test tests/live/test_runpod_heartbeat_live.py -v
pixi run preflight  # exit 0 after to confirm no orphaned pods
```

**Steps:**

- [ ] **Step f.1: Write the RED smoke scaffold**

Create `tests/live/test_runpod_heartbeat_live.py`:

```python
"""Live RunPod heartbeat substrate smoke (B5a Task f).

Gated by ``KINOFORGE_LIVE_RUNPOD=1`` — refuses to run otherwise. Spends
≤ $0.05 per invocation. Characterizes RunPod GraphQL podEditJob mutation
+ pod query round-trip latency under real network conditions and detects
429 rate-limiting if present.

Test discipline:
- The RED-scaffold + this docstring + the actual test body all commit
  BEFORE the first live invocation per CLAUDE.md Durability rule
  ("Commit RED scaffolds before any live spend").
- Pod teardown is in a try/finally; an exception in the assertion block
  must NOT leak a live pod.
- All HTTP via stdlib urllib through the prod _http_post seam (no test
  injection at the wire layer — this IS the wire test).
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest


_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_TICK_COUNT = 2
_TICK_INTERVAL_S = 5.0
_SESSION_LIFETIME_S = 60.0


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    """Refuse to fire without the explicit opt-in env var.

    Belt-and-suspenders: pytest's marker config also gates this, but
    a defensive env-var check inside the fixture guarantees no
    accidental pod spawn from a stray `pytest -v` invocation.
    """
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the live RunPod heartbeat smoke "
            f"(~$0.05 spend per invocation)"
        )


def test_runpod_heartbeat_round_trip_against_live_pod() -> None:
    """End-to-end: spawn a CPU-only pod, write+read heartbeat 2x via
    GraphQL tags, capture latency, teardown.

    The single live test for B5a. Covers:
    - RUNPOD_API_KEY auth scope sufficient for podEditJob mutation
      (the open question from spec §2)
    - Real network round-trip latency (P50/P99) — feeds spec §9 amend
    - 429 detection within 60s of tick-cadence — feeds spec §11 Risk 2
      mitigation
    - Pod-side tag persistence across ~5s of network IO
    """
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be in environment for live smoke"

    endpoint = RunPodGraphQLHeartbeatEndpoint(api_key=api_key)
    provider = RunPodProvider(
        creds=creds,
        heartbeat_endpoint=endpoint,
    )

    # Find the cheapest CPU-only offer (no GPU — heartbeat path doesn't
    # exercise GPU and we want ≤ $0.05 spend).
    reqs = HardwareRequirements(
        gpu_count=0,
        cpu_count=1,
        memory_gb=2,
        cuda_min="0",  # ignored when gpu_count == 0
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod CPU offers available"
    cheapest = min(offers, key=lambda o: o.cost_per_hour_usd or 1e9)
    assert (cheapest.cost_per_hour_usd or 0.0) * (_SESSION_LIFETIME_S / 3600.0) < _BUDGET_USD_CAP, (
        f"cheapest offer too expensive for ≤${_BUDGET_USD_CAP} budget: "
        f"{cheapest.cost_per_hour_usd} USD/hr"
    )

    spec = InstanceSpec(
        offer=cheapest,
        image="runpod/base:latest",
        env={},
        provision_script=None,
    )

    instance = provider.create_instance(spec)
    instance_id = instance.id

    latencies_ms: list[float] = []
    rate_limit_hit = False

    try:
        # Wait for ready (bounded). Bare CPU pods boot in <60s typically.
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(2.0)
        else:
            pytest.fail(f"pod {instance_id} did not reach ready within {_SESSION_LIFETIME_S}s")

        # Two heartbeat ticks
        for tick_num in range(_TICK_COUNT):
            ts_before = datetime.now().astimezone()
            t0 = time.monotonic()
            try:
                provider.heartbeat(instance_id)
            except Exception as exc:  # noqa: BLE001
                if "429" in str(exc):
                    rate_limit_hit = True
                    pytest.fail(
                        f"RunPod GraphQL 429 within tick #{tick_num + 1} — "
                        f"rate limit observed: {exc}"
                    )
                raise
            t1 = time.monotonic()
            got_float = provider.last_heartbeat(instance_id)
            t2 = time.monotonic()
            latencies_ms.append((t1 - t0) * 1000.0)
            latencies_ms.append((t2 - t1) * 1000.0)

            assert got_float is not None, "last_heartbeat returned None after write"
            ts_after = datetime.fromtimestamp(got_float).astimezone()
            delta = abs((ts_after - ts_before).total_seconds())
            assert delta < 1.0, (
                f"tick {tick_num}: write→read mismatch {delta:.3f}s "
                f"(before={ts_before.isoformat()} after={ts_after.isoformat()})"
            )

            if tick_num < _TICK_COUNT - 1:
                time.sleep(_TICK_INTERVAL_S)

    finally:
        # Teardown — destroy pod regardless of test outcome.
        try:
            provider.destroy_instance(instance_id)
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: pod {instance_id} teardown raised {exc!r}; check console")

    # Capture latency stats to a sidecar JSON the operator can paste
    # into the spec amend in Step f.4.
    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p99 = (
        statistics.quantiles(latencies_ms, n=100)[98]
        if len(latencies_ms) >= 100
        else max(latencies_ms, default=0.0)
    )
    sidecar = Path("tests/live/_runpod_heartbeat_smoke_latencies.json")
    sidecar.write_text(
        json.dumps(
            {
                "p50_ms": p50,
                "p99_ms": p99,
                "samples": latencies_ms,
                "rate_limit_hit": rate_limit_hit,
                "spend_cap_usd": _BUDGET_USD_CAP,
                "tick_count": _TICK_COUNT,
                "session_lifetime_s": _SESSION_LIFETIME_S,
            },
            indent=2,
        )
    )

    print(
        f"RUNPOD_HEARTBEAT_LATENCY_MS_P50={p50:.0f} P99={p99:.0f} "
        f"rate_limit={'YES' if rate_limit_hit else 'no'}",
        file=sys.stderr,
    )
```

- [ ] **Step f.2: Commit the RED scaffold BEFORE invoking the live test**

```bash
git add tests/live/test_runpod_heartbeat_live.py
git commit -m "$(cat <<'EOF'
test(b5a): RED scaffold for live RunPod heartbeat smoke

Per CLAUDE.md durability rule: live-spend scaffolds commit BEFORE the
first live invocation, so a mid-spend crash leaves the scaffold in git
instead of forcing the next session to redo the work.

The test will fire under KINOFORGE_LIVE_RUNPOD=1. Budget cap $0.05;
60s session; 2 heartbeat ticks; latency sidecar written to
tests/live/_runpod_heartbeat_smoke_latencies.json.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step f.3: Run preflight + invoke the live smoke**

Operator runs:

```bash
pixi run preflight                                              # must exit 0
KINOFORGE_LIVE_RUNPOD=1 pixi run test tests/live/test_runpod_heartbeat_live.py -v -s
pixi run preflight                                              # exit 0 after — no orphaned pod
```

Expected: test PASSES; sidecar JSON written; pod destroyed; preflight clean.

If the test FAILS:
1. Verify no pod left running (`pixi run -e live-skypilot runpod-list-pods` or RunPod console).
2. Capture the error + sidecar + post-mortem; the RED scaffold is already committed so the next session can iterate.
3. Diagnose: 429 = rate-limit lower than expected; 4xx = `RUNPOD_API_KEY` scope insufficient for `podEditJob`; timeout = network or pod-boot.

- [ ] **Step f.4: Amend the spec with measured latency + rate-limit observation**

Read `tests/live/_runpod_heartbeat_smoke_latencies.json`. Append the observation to spec §9 (after the existing "Latency capture:" line):

```markdown
**Measured (2026-06-12 live smoke, RunPod GraphQL):**
- P50 round-trip: <P50_FROM_SIDECAR> ms
- P99 round-trip: <P99_FROM_SIDECAR> ms
- Rate-limit (429) observed within 60s @ 5s cadence: <YES/no, from sidecar>
- Pod spec: cheapest CPU offer ≤$0.05/hr
- Sidecar: `tests/live/_runpod_heartbeat_smoke_latencies.json`
```

If `rate_limit_hit` is True, also amend §11 Risk 2:

```markdown
**Mitigation update (2026-06-12 live smoke):** RunPod GraphQL returned
429 within <T>s of 5s-cadence ticks. The substrate-invariant minimum
heartbeat_interval_s for RunPod is now documented as `<observed_floor> s`.
B5a does NOT auto-clamp `compute.lifecycle.heartbeat_interval_s` —
operators tune per-deploy. Layer V's dead-man window
(`heartbeat_interval_s * 3`) gives a 3x safety margin.
```

If `rate_limit_hit` is False:

```markdown
**Mitigation update (2026-06-12 live smoke):** RunPod GraphQL did NOT
return 429 at 5s-cadence ticks over 60s. No clamp needed. Document
"no rate-limit observed at 5s cadence within 60s" as the working
baseline; revisit if a future B1 sweeper running multi-pod 30s sweeps
trips throttling.
```

- [ ] **Step f.5: Update PROGRESS.md per spec §15**

Edit `PROGRESS.md`:

1. Locate the entry under "Spec-locked future layers" for B5. Strike it through and add the close-out tag:

```markdown
- ~~**B5. Real `provider.heartbeat()` for RunPod / SkyPilot.**~~ — CLOSED by Phase NN (commit `<sha>`). B5a substrate + RunPod satisfier shipped end-to-end; B5b SkyPilot satisfier still gated on A3 / A4 GPU quota landing.
```

2. Add a new top-level entry for B5b (gated on quota):

```markdown
- **B5b. SkyPilot satisfier for `core/heartbeat_endpoints.py` substrate.** Gated on A3 / A4 GPU quota landing. Plug-in satisfier; substrate Protocol shipped in B5a, no substrate churn required. ~3-4 tasks. Live spend ~$0.05 (one bare-cluster SkyPilot smoke once quota lands; CPU cluster acceptable since the heartbeat path is GPU-irrelevant). Spec hook: `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §13 (B5b Implementation Notes).
```

3. Update the "Single next action" / "RESUME — START HERE" section to point at B7 (next in the warm-reuse queue per `warm-reuse-tasks.txt`).

- [ ] **Step f.6: Commit the closeout**

```bash
git add docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md \
        PROGRESS.md \
        tests/live/_runpod_heartbeat_smoke_latencies.json
git commit -m "$(cat <<'EOF'
docs(b5a): live smoke closeout — measured latency + rate-limit baseline

RunPod GraphQL podEditJob + pod query round-trip captured live on a
bare CPU pod. <P50/P99 ms> measured; <429 yes/no> at 5s cadence over
60s; total spend <X> USD ≤ $0.05 cap.

Spec §9 + §11 amended with the measured baseline. PROGRESS.md striked
the B5 layer entry and added a fresh B5b entry gated on A3 / A4 GPU
quota landing. Substrate is now honest on RunPod; B5b flips the
provider_heartbeat_supported helper for SkyPilot when quota lands.

Latency sidecar checked into tests/live/ for reproducibility.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- Spec §2 (locked decisions) — all carried into the plan; YAML namespace correction is reflected in Task c.
- Spec §3 (architecture) — Tasks a (Protocol + errors), b (RunPod satisfier), c (provider wiring + YAML + dispatch + orchestrator), d (Layer V hook + WARN arm).
- Spec §4 (Protocol + TransportError) — Task a.
- Spec §5 (RunPod satisfier shape) — Task b.
- Spec §6 (provider wiring + YAML + adapter dispatch) — Task c.
- Spec §7 (classify hook + verdict) — Task d.
- Spec §8 (test substrate) — Tasks a (substrate tests), b (wire-shape), c (integration), d (classify+actor), e (parity).
- Spec §9 (live smoke) — Task f.
- Spec §10 (task split) — same six tasks (a–f).
- Spec §14 (acceptance criteria AC1–AC10) — distributed across the six tasks' AC lists.
- Spec §15 (PROGRESS updates) — Task f.6.

**Placeholder scan:** No `TBD` / `TODO` / `FIXME`. `<P50_FROM_SIDECAR>` / `<P99_FROM_SIDECAR>` / `<sha>` / `<X>` in Task f.4-f.6 are templating slots filled at commit time from the sidecar JSON — flagged here as intentional, not placeholders.

**Type consistency:**
- `HeartbeatEndpoint.read(id) -> datetime | None` consistent across spec, Task a Protocol, Task b satisfier, Task e parity tests.
- `HeartbeatEndpoint.write(id, ts_local: datetime) -> None` consistent.
- `provider_heartbeat_supported(provider_kind: str) -> bool` consistent.
- `RunPodProvider.last_heartbeat(id) -> float | None` (POSIX epoch) consistent; conversion at `.timestamp()` documented.
- `Verdict.HEARTBEAT_SUBSTRATE_MISSING` enum value `"HEARTBEAT_SUBSTRATE_MISSING"` consistent across reaper + reaper_actor + tests.
- `compute.heartbeat_mode` literal-set `{"none", "graphql-tag", "selfterm-http", "ssh-touch"}` consistent across config + adapter dispatch + tests.

No issues found; proceeding to user-gate scan + handoff.
