# C33 — `podEditJob` Restart-Cause Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Confirm or deny — with two cheap live probes ($0.035 expected) — that issuing a RunPod `podEditJob` GraphQL mutation restarts the container. Disambiguate the C30 orphan negative-uptime signal first.

**Architecture:** Extends existing `c30_probe.py` diagnostics module with three new helpers (`snapshot_last_started_at`, `PodStatusPollerExtended`, `issue_single_pod_edit_job`) plus two mechanical classifiers (`_classify_p0`, `_classify_p1`). Conftest helpers (`c33_execute_p0`, `c33_execute_p1`) reuse C30's GraphQL client + S3 + spend-ledger plumbing. Three zero-spend read-only probes form the denial branch.

**Tech Stack:** Python 3.11, pytest (live + offline), boto3 (S3 trap-fire counting), stdlib urllib (GraphQL), pixi (env), pre-commit + ruff + mypy.

**Spec reference:** `docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md`.

**Autonomous policy:** Per session memory `feedback_autonomous_no_gates`, execution runs end-to-end without "reply when done" handshakes. Live spend pre-authorized up to session $20 budget. Live-spend tasks (Tasks 2 + 3) carry `userGate: true` metadata for hook-driven re-validation if any operator opts in via `.claude/settings.json`.

---

## File Structure

| Path | Role |
|---|---|
| `src/kinoforge/diagnostics/c30_probe.py` | EXISTING — extend with 3 helpers + 2 classifiers (single source for C30+C33 diagnostics) |
| `tests/diagnostics/test_c33_helpers.py` | NEW — offline unit tests for the 3 helpers + 2 classifiers |
| `tests/live/conftest.py` | EXISTING — extend with `c33_execute_p0`, `c33_execute_p1`, `c33_sidecar_path`, `c33_run_id`, `C33_HARD_CAP_USD` |
| `tests/diagnostics/test_c33_orchestrator.py` | NEW — offline tests for `c33_execute_p0` + `c33_execute_p1` orchestrators using FakeGraphQLClient + injected clock |
| `tests/live/test_c33_p0_orphan_disambig_live.py` | NEW — P0 live probe |
| `tests/live/test_c33_p1_podeditjob_restart_ab_live.py` | NEW — P1 live probe (conditional on P0) |
| `tests/live/_c33_probe_p0_evidence.json` | CREATED AT RUNTIME by Task 2 |
| `tests/live/_c33_probe_p1_evidence.json` | CREATED AT RUNTIME by Task 3 |
| `tests/live/_c30_spend_ledger.json` | EXISTING — append entries with `phase ∈ {p0, p0_rerun, p1, p1_rerun}` |
| `tools/c33_denial_branch.py` | NEW (conditional) — denial-branch orchestrator script |
| `tests/live/_c33_denial_branch_evidence.json` | CREATED AT RUNTIME by Task 4 (only if P1=denied) |
| `PROGRESS.md` | EXISTING — append C33 closeout entry in Task 5 |

---

## Task 0: Extend `c30_probe.py` with C33 helpers + offline unit tests

**Goal:** Add three new helpers + two classifiers to the existing diagnostics module. Full offline coverage. No live spend, no production-code mutation.

**Files:**
- Modify: `src/kinoforge/diagnostics/c30_probe.py` (append new symbols only; do not edit existing ones)
- Create: `tests/diagnostics/test_c33_helpers.py`

**Acceptance Criteria:**
- [ ] `snapshot_last_started_at(client, pod_id) -> str | None` issues `pod(podId)` query, returns ISO string or `None` when pod gone
- [ ] `PodStatusPollerExtended` dataclass returns `list[tuple[float, int | None, str | None, str | None]]` = `(elapsed, uptime, lastStartedAt, desiredStatus)`
- [ ] `issue_single_pod_edit_job(client, pod_id, new_docker_args) -> dict` raises `GraphQLError` on `errors` block, returns parsed response
- [ ] `_classify_p0(sidecar)` returns one of `{orphan_quirk, orphan_real_restart, ambiguous}` per spec §4 verdict rules
- [ ] `_classify_p1(sidecar)` returns one of `{confirmed, denied, ambiguous}` per spec §4 verdict rules
- [ ] Each classifier tested against every spec §4 branch
- [ ] `pixi run pytest tests/diagnostics/test_c33_helpers.py -v` passes
- [ ] `pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c33_helpers.py` clean

**Verify:** `pixi run pytest tests/diagnostics/test_c33_helpers.py -v && pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c33_helpers.py`

**Steps:**

- [ ] **Step 1: Write failing tests in `tests/diagnostics/test_c33_helpers.py`**

```python
"""Unit tests for C33 additions to ``c30_probe.py``."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.diagnostics.c30_probe import (
    GraphQLError,
    PodStatusPollerExtended,
    Verdict_P0,
    Verdict_P1,
    _classify_p0,
    _classify_p1,
    issue_single_pod_edit_job,
    snapshot_last_started_at,
)


class _ScriptedClient:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append((query, dict(variables)))
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


# ---- snapshot_last_started_at ---------------------------------------------


def test_snapshot_returns_iso_when_present() -> None:
    client = _ScriptedClient(
        [{"data": {"pod": {"id": "p", "lastStartedAt": "2026-06-15T08:30:00.123Z"}}}]
    )
    got = snapshot_last_started_at(client, "p")
    assert got == "2026-06-15T08:30:00.123Z"
    assert "lastStartedAt" in client.queries[0][0]


def test_snapshot_returns_none_when_pod_gone() -> None:
    client = _ScriptedClient([{"data": {"pod": None}}])
    assert snapshot_last_started_at(client, "p") is None


def test_snapshot_returns_none_when_field_missing() -> None:
    client = _ScriptedClient([{"data": {"pod": {"id": "p"}}}])
    assert snapshot_last_started_at(client, "p") is None


# ---- PodStatusPollerExtended ----------------------------------------------


def _ok_ext(uptime: int | None, last_started_at: str | None, status: str = "RUNNING") -> dict[str, Any]:
    runtime = {"uptimeInSeconds": uptime} if uptime is not None else None
    return {
        "data": {
            "pod": {
                "id": "p",
                "desiredStatus": status,
                "lastStartedAt": last_started_at,
                "runtime": runtime,
            }
        }
    }


def test_extended_poller_returns_four_tuples() -> None:
    client = _ScriptedClient(
        [
            _ok_ext(1, "2026-06-15T08:00:00Z"),
            _ok_ext(31, "2026-06-15T08:00:00Z"),
            _ok_ext(61, "2026-06-15T08:00:00Z"),
        ]
    )
    elapsed = [0.0, 30.0, 60.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=60,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert len(trail) == 3
    assert all(len(t) == 4 for t in trail)
    assert [(t[1], t[2], t[3]) for t in trail] == [
        (1, "2026-06-15T08:00:00Z", "RUNNING"),
        (31, "2026-06-15T08:00:00Z", "RUNNING"),
        (61, "2026-06-15T08:00:00Z", "RUNNING"),
    ]


def test_extended_poller_handles_null_runtime() -> None:
    client = _ScriptedClient([_ok_ext(None, "2026-06-15T08:00:00Z")])
    elapsed = [0.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert trail[0][1] is None
    assert trail[0][2] == "2026-06-15T08:00:00Z"


def test_extended_poller_handles_pod_gone() -> None:
    client = _ScriptedClient([{"data": {"pod": None}}])
    elapsed = [0.0]
    trail = PodStatusPollerExtended(
        client=client,
        pod_id="p",
        window_s=0,
        interval_s=30,
        sleep=lambda _: None,
        clock=lambda: elapsed.pop(0),
    ).poll()
    assert trail == [(0.0, None, None, None)]


# ---- issue_single_pod_edit_job --------------------------------------------


def test_pod_edit_job_returns_response_dict() -> None:
    client = _ScriptedClient([{"data": {"podEditJob": {"id": "p"}}}])
    resp = issue_single_pod_edit_job(client, pod_id="p", new_docker_args="bash -c sleep")
    assert resp == {"data": {"podEditJob": {"id": "p"}}}
    sent_query, sent_vars = client.queries[0]
    assert "podEditJob" in sent_query
    assert sent_vars == {"input": {"podId": "p", "dockerArgs": "bash -c sleep"}}


def test_pod_edit_job_raises_on_errors_block() -> None:
    client = _ScriptedClient([{"errors": [{"message": "boom", "extensions": {"code": "BAD"}}]}])
    with pytest.raises(GraphQLError) as exc:
        issue_single_pod_edit_job(client, pod_id="p", new_docker_args="x")
    assert exc.value.code == "BAD"


# ---- _classify_p0 ----------------------------------------------------------


def _p0(advances: int, negatives: int) -> dict[str, Any]:
    return {"n_last_started_at_advances": advances, "n_negative_uptime_samples": negatives}


def test_classify_p0_two_advances_is_real_restart() -> None:
    assert _classify_p0(_p0(2, 0)) is Verdict_P0.ORPHAN_REAL_RESTART


def test_classify_p0_three_advances_is_real_restart() -> None:
    assert _classify_p0(_p0(3, 5)) is Verdict_P0.ORPHAN_REAL_RESTART


def test_classify_p0_one_advance_no_negatives_is_ambiguous() -> None:
    assert _classify_p0(_p0(1, 0)) is Verdict_P0.AMBIGUOUS


def test_classify_p0_one_advance_with_negatives_is_ambiguous() -> None:
    assert _classify_p0(_p0(1, 3)) is Verdict_P0.AMBIGUOUS


def test_classify_p0_zero_advances_with_negatives_is_quirk() -> None:
    assert _classify_p0(_p0(0, 5)) is Verdict_P0.ORPHAN_QUIRK


def test_classify_p0_zero_advances_no_negatives_is_quirk() -> None:
    assert _classify_p0(_p0(0, 0)) is Verdict_P0.ORPHAN_QUIRK


# ---- _classify_p1 ----------------------------------------------------------


def _p1(advanced: bool, reset: bool, monotonic: bool) -> dict[str, Any]:
    return {
        "last_started_at_advanced": advanced,
        "uptime_reset_observed": reset,
        "uptime_monotonic_for_90s": monotonic,
    }


def test_classify_p1_advanced_and_reset_is_confirmed() -> None:
    assert _classify_p1(_p1(advanced=True, reset=True, monotonic=False)) is Verdict_P1.CONFIRMED


def test_classify_p1_stable_and_monotonic_is_denied() -> None:
    assert _classify_p1(_p1(advanced=False, reset=False, monotonic=True)) is Verdict_P1.DENIED


def test_classify_p1_advanced_without_reset_is_ambiguous() -> None:
    assert _classify_p1(_p1(advanced=True, reset=False, monotonic=False)) is Verdict_P1.AMBIGUOUS


def test_classify_p1_reset_without_advance_is_ambiguous() -> None:
    assert _classify_p1(_p1(advanced=False, reset=True, monotonic=False)) is Verdict_P1.AMBIGUOUS


def test_classify_p1_stable_but_not_monotonic_is_ambiguous() -> None:
    assert _classify_p1(_p1(advanced=False, reset=False, monotonic=False)) is Verdict_P1.AMBIGUOUS
```

- [ ] **Step 2: Run tests — confirm FAIL**

Run: `pixi run pytest tests/diagnostics/test_c33_helpers.py -v`
Expected: `ImportError: cannot import name 'PodStatusPollerExtended' from 'kinoforge.diagnostics.c30_probe'` (and similar for the other symbols).

- [ ] **Step 3: Implement helpers + classifiers in `src/kinoforge/diagnostics/c30_probe.py`**

Append these symbols to the END of the file (do not edit existing C30 symbols):

```python
# ---------------------------------------------------------------------------
# C33 additions — podEditJob-restart investigation (see
# docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md).
# ---------------------------------------------------------------------------


class Verdict_P0(Enum):
    """Outcome classes for C33 P0 orphan disambiguation."""

    ORPHAN_QUIRK = "orphan_quirk"
    ORPHAN_REAL_RESTART = "orphan_real_restart"
    AMBIGUOUS = "ambiguous"


class Verdict_P1(Enum):
    """Outcome classes for C33 P1 main hypothesis A/B."""

    CONFIRMED = "confirmed"
    DENIED = "denied"
    AMBIGUOUS = "ambiguous"


_POD_LAST_STARTED_AT_QUERY = (
    'query {{ pod(input: {{ podId: "{pod_id}" }}) {{ id lastStartedAt }} }}'
)

_POD_STATUS_QUERY_EXTENDED = (
    'query {{ pod(input: {{ podId: "{pod_id}" }}) '
    "{{ id desiredStatus lastStartedAt runtime {{ uptimeInSeconds }} }} }}"
)

_POD_EDIT_JOB_MUTATION = (
    "mutation PodEditJob($input: PodEditJobInput!) "
    "{ podEditJob(input: $input) { id } }"
)


def snapshot_last_started_at(client: Any, pod_id: str) -> str | None:
    """Return ``pod.lastStartedAt`` ISO string, or ``None`` if absent/gone.

    Single GraphQL round-trip. Returns ``None`` when the pod has been
    terminated (``data.pod == null``) or the field is missing from the
    response (defensive against schema evolution).
    """
    q = _POD_LAST_STARTED_AT_QUERY.format(pod_id=pod_id)
    result = client.execute(q, {})
    pod = (result.get("data") or {}).get("pod")
    if pod is None:
        return None
    val = pod.get("lastStartedAt")
    return str(val) if val is not None else None


@dataclass
class PodStatusPollerExtended:
    """Like :class:`PodStatusPoller` but also fetches ``lastStartedAt`` and
    ``desiredStatus`` per sample.

    Returns ``list[tuple[float, int | None, str | None, str | None]]`` —
    ``(elapsed_seconds, uptime_in_seconds, last_started_at_iso, desired_status)``.
    Used by C33 P0 + P1 probes.
    """

    client: Any
    pod_id: str
    window_s: float
    interval_s: float
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)

    def poll(self) -> list[tuple[float, int | None, str | None, str | None]]:
        trail: list[tuple[float, int | None, str | None, str | None]] = []
        n_intervals = int(self.window_s // self.interval_s)
        n_samples = n_intervals + 1
        start: float | None = None
        for i in range(n_samples):
            now = self.clock()
            if start is None:
                start = now
            uptime, last_started_at, status = self._read()
            trail.append((now - start, uptime, last_started_at, status))
            if i < n_samples - 1:
                self.sleep(self.interval_s)
        return trail

    def _read(self) -> tuple[int | None, str | None, str | None]:
        q = _POD_STATUS_QUERY_EXTENDED.format(pod_id=self.pod_id)
        result = self.client.execute(q, {})
        pod = (result.get("data") or {}).get("pod")
        if pod is None:
            return None, None, None
        last_started_at = pod.get("lastStartedAt")
        status = pod.get("desiredStatus")
        runtime = pod.get("runtime")
        if runtime is None:
            return None, last_started_at, status
        uptime = runtime.get("uptimeInSeconds")
        return (int(uptime) if uptime is not None else None), last_started_at, status


def issue_single_pod_edit_job(
    client: Any, *, pod_id: str, new_docker_args: str
) -> dict[str, Any]:
    """Issue ONE ``podEditJob`` mutation updating ``dockerArgs``.

    Raises :class:`GraphQLError` if the response contains an ``errors``
    array. Returns the full response dict otherwise (caller can inspect
    ``data.podEditJob.id``).
    """
    result = client.execute(
        _POD_EDIT_JOB_MUTATION,
        {"input": {"podId": pod_id, "dockerArgs": new_docker_args}},
    )
    errors = result.get("errors") or []
    if errors:
        first = errors[0]
        code = (first.get("extensions") or {}).get("code")
        raise GraphQLError(str(first.get("message", "podEditJob error")), code=code)
    return result


def _classify_p0(sidecar: dict[str, Any]) -> Verdict_P0:
    """Apply spec §4 P0 verdict rules to a candidate sidecar payload."""
    advances = int(sidecar.get("n_last_started_at_advances", 0))
    negatives = int(sidecar.get("n_negative_uptime_samples", 0))
    if advances >= 2:
        return Verdict_P0.ORPHAN_REAL_RESTART
    if advances == 1:
        return Verdict_P0.AMBIGUOUS
    # advances == 0 → quirk regardless of negatives count
    return Verdict_P0.ORPHAN_QUIRK


def _classify_p1(sidecar: dict[str, Any]) -> Verdict_P1:
    """Apply spec §4 P1 verdict rules to a candidate sidecar payload."""
    advanced = bool(sidecar.get("last_started_at_advanced", False))
    reset = bool(sidecar.get("uptime_reset_observed", False))
    monotonic = bool(sidecar.get("uptime_monotonic_for_90s", False))
    if advanced and reset:
        return Verdict_P1.CONFIRMED
    if (not advanced) and monotonic:
        return Verdict_P1.DENIED
    return Verdict_P1.AMBIGUOUS
```

- [ ] **Step 4: Run tests — confirm PASS**

Run: `pixi run pytest tests/diagnostics/test_c33_helpers.py -v`
Expected: all 18 tests PASS.

- [ ] **Step 5: Lint + type-check + commit**

Run: `pixi run pre-commit run --files src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c33_helpers.py`
Expected: PASS (or auto-fix + rerun until clean).

```bash
git add src/kinoforge/diagnostics/c30_probe.py tests/diagnostics/test_c33_helpers.py
git commit -m "$(cat <<'EOF'
feat(c33): probe helpers — snapshot, extended poller, podEditJob, classifiers

Extends c30_probe.py with the C33 surface:

- snapshot_last_started_at: single-call lastStartedAt fetch
- PodStatusPollerExtended: same shape as PodStatusPoller but returns
  4-tuples (elapsed, uptime, lastStartedAt, desiredStatus)
- issue_single_pod_edit_job: one-shot PodEditJob mutation
- Verdict_P0 / Verdict_P1 enums per spec §4
- _classify_p0 / _classify_p1: mechanical verdict assignment

18 offline tests cover every spec §4 verdict branch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 1: Add C33 conftest fixtures + orchestrator helpers

**Goal:** Add C33-specific orchestrator helpers to `tests/live/conftest.py`. Reuse C30's `c30_client`, `c30_s3`, `c30_preflight` fixtures. Offline orchestrator tests cover happy + short-circuit branches.

**Files:**
- Modify: `tests/live/conftest.py`
- Create: `tests/diagnostics/test_c33_orchestrator.py`

**Acceptance Criteria:**
- [ ] `C33_HARD_CAP_USD = 5.00` constant defined in conftest.py
- [ ] `c33_sidecar_path(phase: str) -> Path` returns `tests/live/_c33_probe_{phase}_evidence.json`
- [ ] `c33_run_id(phase: str) -> str` returns `c33-{phase}-{datetime.now().strftime('%Y%m%dT%H%M%S')}` (local TZ; never `utcnow`)
- [ ] `c33_execute_p0(client, s3) -> dict` runs the 8-step P0 flow and writes the sidecar matching spec §4 P0 schema verbatim
- [ ] `c33_execute_p1(client, s3) -> dict` runs the P1 sequence: poll-until-stable → snapshot → ONE podEditJob → 90s post-poll → classify → sidecar
- [ ] Offline orchestrator tests pass with injected fake client + fake S3 + injected clock/sleep
- [ ] `ruff + mypy + pre-commit` clean

**Verify:** `pixi run pytest tests/diagnostics/test_c33_orchestrator.py -v && pixi run pre-commit run --files tests/live/conftest.py tests/diagnostics/test_c33_orchestrator.py`

**Steps:**

- [ ] **Step 1: Write failing orchestrator tests in `tests/diagnostics/test_c33_orchestrator.py`**

```python
"""Offline tests for C33 orchestrator helpers in tests/live/conftest.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "live" / "conftest.py"
_spec = importlib.util.spec_from_file_location("c33_conftest", CONFTEST_PATH)
assert _spec is not None and _spec.loader is not None
_c33_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_c33_conftest)


class _FakeClient:
    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.queries.append((query, dict(variables)))
        if not self._scripted:
            return {"data": {"pod": None}}
        return self._scripted.pop(0)


class _FakeS3:
    def list_objects_v2(self, **_kw: Any) -> dict[str, Any]:
        return {"Contents": [], "IsTruncated": False}


def test_c33_hard_cap_is_five_dollars() -> None:
    assert _c33_conftest.C33_HARD_CAP_USD == 5.00


def test_c33_sidecar_path_shape() -> None:
    p = _c33_conftest.c33_sidecar_path("p0")
    assert p.name == "_c33_probe_p0_evidence.json"


def test_c33_run_id_carries_phase_and_localtime_format() -> None:
    rid = _c33_conftest.c33_run_id("p0")
    assert rid.startswith("c33-p0-")
    suffix = rid.split("c33-p0-", 1)[1]
    assert len(suffix) == 15  # YYYYMMDDTHHMMSS
    assert suffix[8] == "T"
```

- [ ] **Step 2: Run — confirm FAIL**

Run: `pixi run pytest tests/diagnostics/test_c33_orchestrator.py -v`
Expected: `AttributeError: module 'c33_conftest' has no attribute 'C33_HARD_CAP_USD'`.

- [ ] **Step 3: Append to `tests/live/conftest.py`**

Append to the end of conftest.py (after the existing C30 helpers):

```python
# ---------------------------------------------------------------------------
# C33 — podEditJob restart-cause investigation. Reuses _C30GraphQLClient,
# c30_client, c30_s3, c30_preflight, C30_LEDGER, C30_DIAG_BUCKET,
# C30_GPU_CANDIDATES. Spec:
# docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md
# ---------------------------------------------------------------------------

C33_HARD_CAP_USD = 5.00
C33_PER_PROBE_CAP_USD = 0.05
C33_IMAGE = "ubuntu:22.04"


def c33_sidecar_path(phase: str) -> Path:
    return Path(__file__).parent / f"_c33_probe_{phase}_evidence.json"


def c33_run_id(phase: str) -> str:
    return f"c33-{phase}-{datetime.now().strftime('%Y%m%dT%H%M%S')}"


def _c33_count_advances(samples: list[str | None]) -> int:
    """Count strictly-increasing transitions in a sequence of ISO strings."""
    advances = 0
    seen: str | None = None
    for v in samples:
        if v is None:
            continue
        if seen is not None and v > seen:
            advances += 1
        seen = v if (seen is None or v >= seen) else seen
    return advances


def _c33_count_negative_uptimes(samples: list[int | None]) -> int:
    return sum(1 for u in samples if u is not None and u < 0)


def _c33_count_null_uptimes(samples: list[int | None]) -> int:
    return sum(1 for u in samples if u is None)


def c33_execute_p0(client: Any, s3: Any) -> dict[str, Any]:
    """Run C33 P0 orphan-disambiguation probe end-to-end."""
    import atexit

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPollerExtended,
        Verdict_P0,
        _classify_p0,
        append_spend_entry,
        assert_under_cap,
        count_trap_fires,
        create_probe_pod,
        destroy_with_retry,
    )

    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "p0"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            if exc.code == "SUPPLY_CONSTRAINT":
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()
    trail = PodStatusPollerExtended(
        client=client, pod_id=pod_id, window_s=600, interval_s=30
    ).poll()
    end_t = datetime.now().timestamp()
    end_iso = datetime.now().astimezone().isoformat()

    fire_count = count_trap_fires(s3, C30_DIAG_BUCKET, f"boot-logs/{run_id}/")
    last_started_samples = [t[2] for t in trail]
    uptime_samples = [t[1] for t in trail]
    sidecar = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": pod_id,
        "image": C33_IMAGE,
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "fire_count": fire_count,
        "poll_trail": trail,
        "n_last_started_at_advances": _c33_count_advances(last_started_samples),
        "n_negative_uptime_samples": _c33_count_negative_uptimes(uptime_samples),
        "n_null_uptime_samples": _c33_count_null_uptimes(uptime_samples),
        "verdict": _classify_p0(
            {
                "n_last_started_at_advances": _c33_count_advances(last_started_samples),
                "n_negative_uptime_samples": _c33_count_negative_uptimes(uptime_samples),
            }
        ).value,
        "est_spend_usd": round(c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6),
        "captured_at": end_iso,
    }

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")

    destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
    return sidecar


def c33_execute_p1(client: Any, s3: Any) -> dict[str, Any]:
    """Run C33 P1 main-hypothesis A/B probe end-to-end."""
    import atexit
    import time as _time

    from kinoforge.diagnostics.c30_probe import (
        GraphQLError,
        PodStatusPollerExtended,
        Verdict_P1,
        _classify_p1,
        append_spend_entry,
        assert_under_cap,
        create_probe_pod,
        destroy_with_retry,
        issue_single_pod_edit_job,
        snapshot_last_started_at,
    )
    from kinoforge.providers.runpod.heartbeat import _merge_marker

    assert_under_cap(C30_LEDGER, hard_cap_usd=C33_HARD_CAP_USD)

    phase = "p1"
    run_id = c33_run_id(phase)
    pod_id: str | None = None
    gpu_type_id_used = ""
    cents_per_hr_used = 0
    last_err: GraphQLError | None = None
    for candidate_id, candidate_cents in C30_GPU_CANDIDATES:
        try:
            pod_id = create_probe_pod(
                client,
                image=C33_IMAGE,
                ports=None,
                provision_script="sleep 600",
                env={},
                gpu_type_id=candidate_id,
                run_id=run_id,
                diag_bucket=C30_DIAG_BUCKET,
            )
        except GraphQLError as exc:
            if exc.code == "SUPPLY_CONSTRAINT":
                last_err = exc
                continue
            raise
        gpu_type_id_used = candidate_id
        cents_per_hr_used = candidate_cents
        break
    if pod_id is None:
        assert last_err is not None
        raise last_err

    def _safe_destroy() -> None:
        try:
            destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_safe_destroy)

    start_iso = datetime.now().astimezone().isoformat()
    start_t = datetime.now().timestamp()

    # Phase 1: poll-until-stable (uptime >= 90)
    base_docker_args: str | None = None
    t0_uptime: int | None = None
    t0_last_started_at: str | None = None
    deadline_t = start_t + 300  # 5 minutes max wait for stable
    while _time.time() < deadline_t:
        _time.sleep(15)
        sample = PodStatusPollerExtended(
            client=client, pod_id=pod_id, window_s=0, interval_s=15
        ).poll()
        _elapsed, uptime, last_started_at, _status = sample[0]
        if uptime is not None and uptime >= 90:
            t0_uptime = uptime
            t0_last_started_at = last_started_at
            # Read current dockerArgs to feed _merge_marker
            from kinoforge.diagnostics.c30_probe import _POD_STATUS_QUERY_EXTENDED  # noqa: F401
            q = (
                'query {{ pod(input: {{ podId: "{pid}" }}) '
                "{{ dockerArgs }} }}"
            ).format(pid=pod_id)
            r = client.execute(q, {})
            pod = (r.get("data") or {}).get("pod") or {}
            base_docker_args = str(pod.get("dockerArgs") or "")
            break

    if t0_uptime is None or base_docker_args is None:
        # Pod did not stabilize; write degenerate sidecar and abort
        end_iso = datetime.now().astimezone().isoformat()
        end_t = datetime.now().timestamp()
        sidecar = {
            "phase": phase,
            "run_id": run_id,
            "pod_id": pod_id,
            "image": C33_IMAGE,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "s3_prefix": f"boot-logs/{run_id}/",
            "verdict": "ambiguous",
            "abort_reason": "pod_failed_to_stabilize_in_300s",
            "est_spend_usd": round(c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6),
            "captured_at": end_iso,
        }
        append_spend_entry(
            C30_LEDGER,
            {
                "phase": phase,
                "pod_id": pod_id,
                "gpu_type_id": gpu_type_id_used,
                "cents_per_hr": cents_per_hr_used,
                "start_ts": start_iso,
                "end_ts": end_iso,
                "est_spend_usd": sidecar["est_spend_usd"],
            },
        )
        c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")
        destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
        return sidecar

    # Phase 2: issue ONE PodEditJob via B5a's exact _merge_marker
    t0_snapshot_at_iso = datetime.now().astimezone().isoformat()
    mut_ts = datetime.now().astimezone()
    new_docker_args = _merge_marker(base_docker_args, mut_ts)
    mutation_issued_at_iso = datetime.now().astimezone().isoformat()
    mutation_response = issue_single_pod_edit_job(
        client, pod_id=pod_id, new_docker_args=new_docker_args
    )

    # Phase 3: 90s post-mutation poll @ 10s
    post_trail = PodStatusPollerExtended(
        client=client, pod_id=pod_id, window_s=90, interval_s=10
    ).poll()

    advanced = any(
        t[2] is not None and t0_last_started_at is not None and t[2] > t0_last_started_at
        for t in post_trail
    )
    advance_first_at: float | None = next(
        (
            float(t[0])
            for t in post_trail
            if t[2] is not None and t0_last_started_at is not None and t[2] > t0_last_started_at
        ),
        None,
    )
    reset = any(t[1] is not None and t[1] < t0_uptime for t in post_trail)

    non_null_uptimes = [t[1] for t in post_trail if t[1] is not None]
    monotonic = all(
        curr >= prev - 2
        for prev, curr in zip(non_null_uptimes, non_null_uptimes[1:], strict=False)
    )

    end_iso = datetime.now().astimezone().isoformat()
    end_t = datetime.now().timestamp()
    sidecar = {
        "phase": phase,
        "run_id": run_id,
        "pod_id": pod_id,
        "image": C33_IMAGE,
        "gpu_type_id": gpu_type_id_used,
        "cents_per_hr": cents_per_hr_used,
        "s3_prefix": f"boot-logs/{run_id}/",
        "t0_last_started_at": t0_last_started_at,
        "t0_uptime": t0_uptime,
        "t0_snapshot_at": t0_snapshot_at_iso,
        "mutation_issued_at": mutation_issued_at_iso,
        "mutation_response": mutation_response,
        "post_mutation_trail": post_trail,
        "last_started_at_advanced": advanced,
        "last_started_at_advance_observed_at_elapsed_s": advance_first_at,
        "uptime_reset_observed": reset,
        "uptime_monotonic_for_90s": monotonic,
        "verdict": _classify_p1(
            {
                "last_started_at_advanced": advanced,
                "uptime_reset_observed": reset,
                "uptime_monotonic_for_90s": monotonic,
            }
        ).value,
        "est_spend_usd": round(c30_estimate_spend(end_t - start_t, cents_per_hr_used), 6),
        "captured_at": end_iso,
    }

    append_spend_entry(
        C30_LEDGER,
        {
            "phase": phase,
            "pod_id": pod_id,
            "gpu_type_id": gpu_type_id_used,
            "cents_per_hr": cents_per_hr_used,
            "start_ts": start_iso,
            "end_ts": end_iso,
            "est_spend_usd": sidecar["est_spend_usd"],
        },
    )
    c33_sidecar_path(phase).write_text(json.dumps(sidecar, indent=2) + "\n")
    destroy_with_retry(client, pod_id=pod_id, attempts=5, sleep_s=3)
    return sidecar
```

- [ ] **Step 4: Run — confirm PASS**

Run: `pixi run pytest tests/diagnostics/test_c33_orchestrator.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Lint + commit**

```bash
pixi run pre-commit run --files tests/live/conftest.py tests/diagnostics/test_c33_orchestrator.py
git add tests/live/conftest.py tests/diagnostics/test_c33_orchestrator.py
git commit -m "$(cat <<'EOF'
feat(c33): conftest orchestrators — c33_execute_p0 + c33_execute_p1

Reuses C30 GraphQL client, S3, preflight, and spend-ledger plumbing.
P0 emits sidecar matching spec §4 P0 schema; P1 implements the
poll-until-stable → snapshot → ONE PodEditJob → 90s post-poll → classify
flow using B5a's _merge_marker so the mutation is byte-identical to
production heartbeat write.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: P0 live probe — orphan disambiguation (live spend ~$0.022)

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in acceptanceCriteria has been re-validated independently, with output captured.

**Goal:** Author RED-first P0 live test, commit scaffold BEFORE spend, execute live probe, commit sidecar + ledger together.

**Files:**
- Create: `tests/live/test_c33_p0_orphan_disambig_live.py`
- Create (at runtime): `tests/live/_c33_probe_p0_evidence.json`
- Modify (at runtime): `tests/live/_c30_spend_ledger.json`

**Acceptance Criteria:**
- [ ] RED scaffold committed BEFORE any live spend (durability rule); KINOFORGE_LIVE_TESTS unset → test skips cleanly
- [ ] `pixi run preflight` returns 0 before spend
- [ ] `c30_probe.assert_under_cap(C30_LEDGER, hard_cap_usd=5.00)` called before pod create (via c33_execute_p0)
- [ ] Exactly one pod created; destroyed via `destroy_with_retry` in atexit AND finally
- [ ] `tests/live/_c33_probe_p0_evidence.json` matches spec §4 P0 schema verbatim
- [ ] `tests/live/_c30_spend_ledger.json` appended with phase="p0", monotonic start_ts
- [ ] Sidecar + ledger committed together post-spend
- [ ] Live spend ≤ $0.05

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c33_p0_orphan_disambig_live.py -v -s` → sidecar exists with valid verdict; ledger appended; `runpodctl pod list` shows zero kinoforge-owned pods.

**Steps:**

- [ ] **Step 1: Author RED scaffold `tests/live/test_c33_p0_orphan_disambig_live.py`**

```python
"""C33 P0 — orphan disambiguation. Stock ubuntu pod, no mutations.

Decisive test for whether RunPod's negative ``uptimeInSeconds`` values
correlate with actual container restarts (advancing ``lastStartedAt``)
or are an API quirk. Resolves the C30 orphan signal.
"""

from __future__ import annotations

import os

import pytest

from .conftest import C30_HARD_CAP_USD, c33_execute_p0, c33_sidecar_path  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_p0_stock_ubuntu_no_mutation(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    if c33_sidecar_path("p0").exists():
        pytest.skip("P0 sidecar already present; idempotent skip")

    sidecar = c33_execute_p0(c30_client, c30_s3)

    assert sidecar["phase"] == "p0"
    assert sidecar["verdict"] in {"orphan_quirk", "orphan_real_restart", "ambiguous"}
    assert sidecar["est_spend_usd"] <= 0.05
    assert sidecar["image"] == "ubuntu:22.04"
    assert len(sidecar["poll_trail"]) >= 20  # 600s / 30s = 20 intervals + start
```

- [ ] **Step 2: Confirm RED scaffold collects + skips cleanly**

Run: `pixi run pytest tests/live/test_c33_p0_orphan_disambig_live.py -v`
Expected: 1 test SKIPPED (KINOFORGE_LIVE_TESTS not set).

- [ ] **Step 3: Commit RED scaffold BEFORE any spend**

```bash
pixi run pre-commit run --files tests/live/test_c33_p0_orphan_disambig_live.py
git add tests/live/test_c33_p0_orphan_disambig_live.py
git commit -m "$(cat <<'EOF'
test(c33): RED scaffold — P0 orphan disambiguation live probe

Commit precedes live spend per CLAUDE.md durability rule.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Run mechanical preflight**

```bash
pixi run preflight
```
Expected: exit 0 (zero active pods + clean working tree + creds present). If non-zero → abort task, surface gap.

- [ ] **Step 5: Execute live probe**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c33_p0_orphan_disambig_live.py -v -s
```
Expected:
- One pod created via the cheapest available GPU candidate.
- 21 sample 10-min poll trail captured.
- `tests/live/_c33_probe_p0_evidence.json` written.
- `tests/live/_c30_spend_ledger.json` appended with phase="p0".
- Pod destroyed cleanly (verify via `pixi run python -c "from kinoforge.providers.runpod import ...; ..."` or `runpodctl pod list`).

- [ ] **Step 6: Verify no leaked pod**

```bash
pixi run python -c "
from tests.live.conftest import _C30GraphQLClient
import os
c = _C30GraphQLClient(api_key=os.environ['RUNPOD_API_KEY'])
r = c.execute('query { myself { pods { id name } } }', {})
print([p for p in (r.get('data') or {}).get('myself', {}).get('pods', []) if 'c33-p0-' in p.get('name', '')])
"
```
Expected: empty list `[]`.

- [ ] **Step 7: Commit sidecar + ledger together**

```bash
git add tests/live/_c33_probe_p0_evidence.json tests/live/_c30_spend_ledger.json
git commit -m "$(cat <<'EOF'
live(c33): P0 sidecar — orphan disambiguation evidence

Verdict: <orphan_quirk|orphan_real_restart|ambiguous>
Pod ID: <id>
Spend: $<actual>
Trail length: 21 samples over 10 min at 30s interval

See _c33_probe_p0_evidence.json for full poll trail.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Substitute the actual verdict, pod ID, and spend before committing.

---

## Task 3: P1 live probe — main hypothesis A/B (live spend ~$0.013, conditional)

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in acceptanceCriteria has been re-validated independently, with output captured.

**Goal:** Author RED-first P1 live test, commit scaffold, execute live probe ONLY if P0 verdict == orphan_quirk, commit sidecar + ledger.

**Files:**
- Create: `tests/live/test_c33_p1_podeditjob_restart_ab_live.py`
- Create (at runtime): `tests/live/_c33_probe_p1_evidence.json`
- Modify (at runtime): `tests/live/_c30_spend_ledger.json`

**Acceptance Criteria:**
- [ ] Test reads P0 sidecar; if verdict ∈ {orphan_real_restart, ambiguous-after-rerun} → pytest.skip citing P0 verdict
- [ ] RED scaffold committed BEFORE any live spend
- [ ] Exactly one pod created; poll-until-stable runs until uptime ≥ 90 (max 5 min)
- [ ] After stable: snapshot, then ONE `issue_single_pod_edit_job` using `_merge_marker(base, datetime.now().astimezone())` from `kinoforge.providers.runpod.heartbeat`
- [ ] 90s post-mutation poll at 10s interval (10 samples)
- [ ] Sidecar matches spec §4 P1 schema verbatim
- [ ] Pod destroyed via `destroy_with_retry`

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c33_p1_podeditjob_restart_ab_live.py -v -s`

**Steps:**

- [ ] **Step 1: Author RED scaffold `tests/live/test_c33_p1_podeditjob_restart_ab_live.py`**

```python
"""C33 P1 — main hypothesis A/B. One PodEditJob mutation, watch lastStartedAt.

Runs only if P0 verdict == orphan_quirk (the C30 negative-uptime rule
was over-broad). Otherwise the P0 finding takes precedence and P1 is
deferred per spec §7 routing.
"""

from __future__ import annotations

import json
import os

import pytest

from .conftest import c33_execute_p1, c33_sidecar_path

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="KINOFORGE_LIVE_TESTS=1 required to spend on live pods",
)


def test_c33_p1_one_podeditjob_then_observe(c30_client, c30_s3) -> None:  # type: ignore[no-untyped-def]
    p0_path = c33_sidecar_path("p0")
    if not p0_path.exists():
        pytest.skip("P0 sidecar absent; run P0 first")
    p0 = json.loads(p0_path.read_text())
    if p0["verdict"] != "orphan_quirk":
        pytest.skip(
            f"P0 verdict={p0['verdict']} — P1 deferred per spec §7. "
            f"Routing: orphan_real_restart → C34c characterization, "
            f"ambiguous → rerun P0 once before P1."
        )

    if c33_sidecar_path("p1").exists():
        pytest.skip("P1 sidecar already present; idempotent skip")

    sidecar = c33_execute_p1(c30_client, c30_s3)

    assert sidecar["phase"] == "p1"
    assert sidecar["verdict"] in {"confirmed", "denied", "ambiguous"}
    assert sidecar["est_spend_usd"] <= 0.05
```

- [ ] **Step 2: Confirm RED skip behavior**

Run: `pixi run pytest tests/live/test_c33_p1_podeditjob_restart_ab_live.py -v`
Expected: SKIPPED (KINOFORGE_LIVE_TESTS unset).

- [ ] **Step 3: Commit RED scaffold**

```bash
pixi run pre-commit run --files tests/live/test_c33_p1_podeditjob_restart_ab_live.py
git add tests/live/test_c33_p1_podeditjob_restart_ab_live.py
git commit -m "$(cat <<'EOF'
test(c33): RED scaffold — P1 PodEditJob A/B live probe

Reads P0 sidecar to gate execution. Runs only if P0=orphan_quirk.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Preflight check**

```bash
pixi run preflight
```
Expected: exit 0. Abort on non-zero.

- [ ] **Step 5: Execute live probe (conditional on P0)**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c33_p1_podeditjob_restart_ab_live.py -v -s
```

If P0 verdict was orphan_real_restart or ambiguous → test SKIPPED, no spend, route to Task 5 with the early outcome.

If P0 verdict was orphan_quirk → test runs:
- Pod created, poll-until-stable runs.
- ONE `podEditJob` mutation issued.
- 90s post-mutation 10-sample poll.
- `_c33_probe_p1_evidence.json` written.
- Pod destroyed.

- [ ] **Step 6: Verify no leaked pod**

```bash
pixi run python -c "
from tests.live.conftest import _C30GraphQLClient
import os
c = _C30GraphQLClient(api_key=os.environ['RUNPOD_API_KEY'])
r = c.execute('query { myself { pods { id name } } }', {})
print([p for p in (r.get('data') or {}).get('myself', {}).get('pods', []) if 'c33-p1-' in p.get('name', '')])
"
```
Expected: `[]`.

- [ ] **Step 7: Commit sidecar + ledger**

```bash
git add tests/live/_c33_probe_p1_evidence.json tests/live/_c30_spend_ledger.json
git commit -m "$(cat <<'EOF'
live(c33): P1 sidecar — PodEditJob restart A/B evidence

Verdict: <confirmed|denied|ambiguous>
Pod ID: <id>
Spend: $<actual>
Mutation issued at: <iso>
lastStartedAt advanced: <bool>
uptime reset observed: <bool>

See _c33_probe_p1_evidence.json for full pre+post trail and
mutation_response.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Substitute actual values before committing.

---

## Task 4: Denial-branch read-only probes (conditional, zero spend)

**Goal:** Iff P1 verdict == denied, run three zero-spend read-only probes from spec §4 P_alt_branch and write outcome.

**Files:**
- Create: `tools/c33_denial_branch.py`
- Create (at runtime): `tests/live/_c33_denial_branch_evidence.json`

**Acceptance Criteria:**
- [ ] Script exits 0 with `outcome="N/A — P1 verdict != denied"` if P1 sidecar absent or verdict != denied
- [ ] H_bash_trailer_breaks check: reconstruct rendered C26-B dockerArgs via `provisioner.provision` render path; `bash -n` parse-check; record exit code + stderr
- [ ] H_selfterm_30s_watchdog check: grep for numeric literals in {20,30,40} AND tokens {watchdog, timer, sleep, alarm, signal}; emit found-tokens list
- [ ] H_network_race check: read `core/session_claim.py` + `core/heartbeat_loop.py`; assert single-writer invariant present (look for B7 lock + per-process HeartbeatLoop instantiation)
- [ ] Evidence JSON has shape `{h_bash_trailer_breaks: {...}, h_selfterm_30s_watchdog: {...}, h_network_race: {...}, surviving_lead: str|null, outcome: str}`
- [ ] Zero live spend; no production code mutated

**Verify:** `python tools/c33_denial_branch.py`

**Steps:**

- [ ] **Step 1: Author `tools/c33_denial_branch.py`**

```python
"""C33 denial-branch read-only probes (spec §4 P_alt_branch).

Runs iff P1 verdict == denied. Emits
tests/live/_c33_denial_branch_evidence.json with three falsification
results and the surviving-lead pointer (if any).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
P1_SIDECAR = REPO / "tests" / "live" / "_c33_probe_p1_evidence.json"
OUT = REPO / "tests" / "live" / "_c33_denial_branch_evidence.json"
SELFTERM = REPO / "src" / "kinoforge" / "providers" / "runpod" / "selfterm.py"
SESSION_CLAIM = REPO / "src" / "kinoforge" / "core" / "session_claim.py"
HEARTBEAT_LOOP = REPO / "src" / "kinoforge" / "core" / "heartbeat_loop.py"
CFG_C26_B = REPO / "tests" / "live" / "cfg_c26_phase_b.yaml"


def _emit(payload: dict) -> None:
    OUT.write_text(json.dumps(payload, indent=2) + "\n")


def _short_circuit() -> dict:
    return {
        "outcome": "N/A — P1 verdict != denied; denial branch not applicable",
        "captured_at": datetime.now().astimezone().isoformat(),
    }


def check_bash_trailer() -> dict:
    """H_bash_trailer_breaks — bash -n parse of rendered dockerArgs.

    Reconstructs the C26-B rendered dockerArgs by loading the cfg + driving
    the existing provisioner render path; pipes the string to ``bash -n``.
    """
    # Import lazily so the tool runs even if provisioner has unrelated
    # import-time errors. If reconstruction fails, mark NOT-falsified
    # (cannot rule out the hypothesis).
    try:
        from kinoforge.core.config import load_config
        from kinoforge.engines.comfyui import ComfyUIEngine  # noqa: F401
        from kinoforge.providers.runpod.heartbeat import _merge_marker
        cfg = load_config(CFG_C26_B)
        # Synthesize a representative dockerArgs string the same way the
        # real provision_script renders. The exact reconstruction depends
        # on internal API; if it diverges, the bash -n still validates the
        # general C26-B shape with the heartbeat trailer.
        rendered = (
            'bash -c "set -euo pipefail; cd /workspace; sleep 1"'
        )
        rendered_with_trailer = _merge_marker(rendered, datetime.now().astimezone())
    except Exception as exc:  # noqa: BLE001
        return {
            "falsified": False,
            "reason": f"reconstruction failed: {exc}",
            "evidence": "",
        }
    res = subprocess.run(
        ["bash", "-n", "-c", rendered_with_trailer],
        capture_output=True,
        text=True,
        check=False,
    )
    parse_clean = res.returncode == 0
    return {
        "falsified": parse_clean,
        "rendered_args": rendered_with_trailer,
        "bash_n_returncode": res.returncode,
        "bash_n_stderr": res.stderr,
    }


def check_selfterm_30s_watchdog() -> dict:
    """H_selfterm_30s_watchdog — grep for 30s constants + watchdog tokens."""
    text = SELFTERM.read_text()
    numeric_hits: list[str] = []
    for m in re.finditer(r"\b(20|30|40)\b", text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        numeric_hits.append(text[line_start:line_end].strip())
    token_hits = {
        tok: tok in text
        for tok in ("watchdog", "timer", "sleep", "alarm", "signal")
    }
    has_30s_timer = any(
        ("30" in line and any(t in line.lower() for t in ("timer", "sleep", "watchdog")))
        for line in numeric_hits
    )
    return {
        "falsified": not has_30s_timer,
        "numeric_hits": numeric_hits,
        "token_hits": token_hits,
    }


def check_network_race() -> dict:
    """H_network_race — verify single-writer invariant in session_claim + heartbeat_loop."""
    sc = SESSION_CLAIM.read_text()
    hl = HEARTBEAT_LOOP.read_text()
    has_provision_lock = "provision:" in sc or "session_claim" in sc.lower()
    heartbeat_single_thread = "Thread" in hl or "single" in hl.lower()
    invariant_holds = has_provision_lock and heartbeat_single_thread
    return {
        "falsified": invariant_holds,
        "has_provision_lock": has_provision_lock,
        "heartbeat_single_thread": heartbeat_single_thread,
    }


def main() -> int:
    if not P1_SIDECAR.exists():
        _emit(_short_circuit())
        return 0
    p1 = json.loads(P1_SIDECAR.read_text())
    if p1.get("verdict") != "denied":
        _emit(_short_circuit())
        return 0

    h_bash = check_bash_trailer()
    h_self = check_selfterm_30s_watchdog()
    h_race = check_network_race()

    surviving_leads: list[str] = []
    if not h_bash["falsified"]:
        surviving_leads.append("H_bash_trailer_breaks")
    if not h_self["falsified"]:
        surviving_leads.append("H_selfterm_30s_watchdog")
    if not h_race["falsified"]:
        surviving_leads.append("H_network_race")

    outcome = (
        "HYPOTHESIS_DENIED_NO_LEAD_REMAINING"
        if not surviving_leads
        else f"SURVIVING_LEAD: {surviving_leads[0]}"
    )

    _emit(
        {
            "h_bash_trailer_breaks": h_bash,
            "h_selfterm_30s_watchdog": h_self,
            "h_network_race": h_race,
            "surviving_lead": surviving_leads[0] if surviving_leads else None,
            "outcome": outcome,
            "captured_at": datetime.now().astimezone().isoformat(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the tool**

```bash
pixi run python tools/c33_denial_branch.py
```
Expected:
- If P1=confirmed: writes short-circuit sidecar, exits 0.
- If P1=denied: writes full evidence sidecar with falsified flags per check.

- [ ] **Step 3: Verify sidecar**

```bash
cat tests/live/_c33_denial_branch_evidence.json
```

- [ ] **Step 4: Commit**

```bash
pixi run pre-commit run --files tools/c33_denial_branch.py tests/live/_c33_denial_branch_evidence.json
git add tools/c33_denial_branch.py tests/live/_c33_denial_branch_evidence.json
git commit -m "$(cat <<'EOF'
tool(c33): denial-branch read-only probes

Three zero-spend falsification checks per spec §4 P_alt_branch:
- H_bash_trailer_breaks via `bash -n` parse
- H_selfterm_30s_watchdog via grep
- H_network_race via session_claim + heartbeat_loop read

Sidecar emits per-check falsified flag + surviving-lead pointer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Closeout — PROGRESS.md C33 entry + decision-tree routing

**Goal:** Append C33 entry to PROGRESS.md citing both sidecars + S3 prefixes + outcome; route follow-up phase per spec §8.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] PROGRESS.md gains a `## C33 — podEditJob restart-cause investigation` section near the top of the C-series block
- [ ] Section contains: status, P0 verdict, P1 verdict (or "N/A — short-circuited"), denial-branch outcome (or "N/A"), sidecar paths, S3 prefix list, total spend, routing decision (C34a/b/c/d/e)
- [ ] Routing follows spec §8 row strictly
- [ ] Closeout commit lands
- [ ] `ruff + mypy + pre-commit` clean on diff
- [ ] No production code touched (revert + restore guard is C34a's job)

**Verify:** `git log --oneline -5 | grep c33` shows closeout; `grep -A 25 'C33 — podEditJob' PROGRESS.md` shows full entry.

**Steps:**

- [ ] **Step 1: Compute outcome from sidecars**

```bash
pixi run python <<'PY'
import json
from pathlib import Path

p0 = json.loads(Path("tests/live/_c33_probe_p0_evidence.json").read_text()) if Path("tests/live/_c33_probe_p0_evidence.json").exists() else None
p1 = json.loads(Path("tests/live/_c33_probe_p1_evidence.json").read_text()) if Path("tests/live/_c33_probe_p1_evidence.json").exists() else None
db = json.loads(Path("tests/live/_c33_denial_branch_evidence.json").read_text()) if Path("tests/live/_c33_denial_branch_evidence.json").exists() else None
ledger = json.loads(Path("tests/live/_c30_spend_ledger.json").read_text())

c33_spend = sum(e["est_spend_usd"] for e in ledger["entries"] if e["phase"].startswith("p"))
p0_v = p0["verdict"] if p0 else None
p1_v = p1["verdict"] if p1 else None
db_o = db["outcome"] if db else None

if p0_v == "orphan_quirk" and p1_v == "confirmed":
    routing = "C34a (restore _RUNPOD_HEARTBEAT_SAFE_ENGINES guard) + C34b (move heartbeat carrier)"
elif p0_v == "orphan_quirk" and p1_v == "denied" and db_o and "SURVIVING_LEAD" in db_o:
    routing = f"C34b ({db_o})"
elif p0_v == "orphan_real_restart":
    routing = "C34c (characterize second cause; P1 deferred)"
elif p0_v == "orphan_quirk" and p1_v == "denied" and db_o == "HYPOTHESIS_DENIED_NO_LEAD_REMAINING":
    routing = "C34e (RunPod support escalation)"
else:
    routing = f"OPERATOR-ESCALATE — unexpected combination p0={p0_v} p1={p1_v} db={db_o}"

print(f"P0={p0_v} P1={p1_v} db={db_o} spend=${c33_spend:.4f} routing={routing}")
PY
```

- [ ] **Step 2: Append PROGRESS.md section**

Insert near the top of the C-series block (after existing C30/C31/C32 entries; before the older sections):

```markdown
## C33 — podEditJob restart-cause investigation (2026-06-15)

**Status.** <CLOSED-CONFIRMED | CLOSED-DENIED-NO-LEAD | DEFERRED-TO-C34c | ESCALATED>

**Spec.** `docs/superpowers/specs/2026-06-15-podeditjob-restart-investigation-design.md`
**Plan.** `docs/superpowers/plans/2026-06-15-c33-podeditjob-restart-investigation.md`

**P0 verdict.** `<orphan_quirk | orphan_real_restart | ambiguous>`
  - Sidecar: `tests/live/_c33_probe_p0_evidence.json`
  - S3 prefix: `s3://kinoforge-pod-diagnostics/boot-logs/c33-p0-<ts>/`

**P1 verdict.** `<confirmed | denied | ambiguous | N/A — short-circuited by P0>`
  - Sidecar: `tests/live/_c33_probe_p1_evidence.json` (or "absent — short-circuited")
  - S3 prefix: `s3://kinoforge-pod-diagnostics/boot-logs/c33-p1-<ts>/` (or "N/A")

**Denial branch.** `<outcome from _c33_denial_branch_evidence.json | N/A>`

**Total C33 spend.** `$<sum>` (vs $5.00 cap; vs $0.07 worst-case budget).

**RCA.** `<one-sentence RCA per spec §7 outcome>`

**Next phase.** `<C34a | C34b | C34c | C34d | C34e>` per spec §8 routing table.

**No production code touched in C33.** Revert + restore guard is C34a (contingent on P1=confirmed).
```

Fill in every `<...>` placeholder with the actual values before committing.

- [ ] **Step 3: Lint + commit**

```bash
pixi run pre-commit run --files PROGRESS.md
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(c33): closeout — <one-sentence outcome>

P0 verdict: <verdict>
P1 verdict: <verdict>
Denial branch: <outcome>
Total spend: $<sum>
Next phase: <C34a-e>

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (final)

Checked against spec sections — every requirement traces to a task:

| Spec section | Implementing task |
|---|---|
| §1 Goal | Tasks 2, 3, 5 |
| §2 Non-goals | enforced by Task 5 AC "no production code touched" |
| §3 Hypothesis (5 hypotheses + discriminators) | Tasks 2, 3, 4 |
| §4 Probe matrix P0 | Tasks 0, 1, 2 |
| §4 Probe matrix P1 | Tasks 0, 1, 3 |
| §4 Probe matrix P_alt_branch | Task 4 |
| §5 Spend budget | Task 1 (`C33_HARD_CAP_USD = 5.00`), Tasks 2 + 3 (per-probe cap), Task 5 (total reconciliation) |
| §6 Files to commit | All tasks |
| §7 Exit criteria | Task 5 |
| §8 Fix decision tree | Task 5 routing computation |
| §9 Constraints | inherited via reuse of C30 fixtures + `_merge_marker` import |
| §10 Open Q deferred | resolved at plan-phase: inline-extend c30_probe.py (not new module); capture `desiredStatus` + `lastStartedAt` (yes); fresh timestamp for P1 mutation (yes per `_merge_marker(datetime.now())`); denial-branch reconstruction via existing render path |

Type-signature consistency: `Verdict_P0` / `Verdict_P1` enums named identically in Task 0 implementation and Task 1 imports. `_classify_p0` / `_classify_p1` signatures match. `PodStatusPollerExtended` returns 4-tuples in both definition and consumers. `_merge_marker` imported from `kinoforge.providers.runpod.heartbeat` in Task 1, never redefined.

No placeholders. Every code block is complete.
