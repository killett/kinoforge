# C25 — RunPod heartbeat preserve-and-merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a wire-level fix for the B5a heartbeat / Phase 24 selfterm `dockerArgs` collision so `engine.kind=comfyui` + `compute.heartbeat_mode=graphql-tag` is safe on real workloads, B3 cross-CLI auto-discovery warm-reuse works on two identical `kinoforge generate` CLI invocations on Wan, and the existing `_RUNPOD_HEARTBEAT_SAFE_ENGINES` runtime guard is retired.

**Architecture:** Probe-then-branch. Task a runs a ~$0.05 live probe to disambiguate undocumented `podEditJob` `env`-array semantics; the outcome is captured in `tests/live/_runpod_env_semantics.json` and pins Task b's branch. Branch A (env merges additively) ships a one-round-trip env-slot satisfier; Branch B (env replaces, or env field not surfaced on read) ships `dockerArgs` preserve-and-merge with a trailing `# _kinoforge_hb:<ISO>` bash-comment marker. Either branch deletes the `_RUNPOD_HEARTBEAT_SAFE_ENGINES` guard so the substrate Protocol becomes the sole contract. Task d's two-prompt Wan smoke is the acceptance gate.

**Tech Stack:** Python 3.11; pytest; stdlib `urllib`; RunPod GraphQL (`podEditJob`, `pod`); pixi for dep/env management; existing `RunPodGraphQLHeartbeatEndpoint` substrate from B5a.

**Spec:** `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` (commit `e9443ef`).

---

## File Structure

**Created files:**
- `tests/live/test_runpod_env_semantics_probe.py` — env-merge probe (Task 1).
- `tests/live/_runpod_env_semantics.json` — probe outcome sidecar (Task 1 output; committed).
- `tests/live/cfg_c25_wan_comfyui.yaml` — Wan acceptance smoke cfg (Task 4).
- `tests/live/test_c25_warm_reuse_comfyui_wan_live.py` — Wan acceptance smoke (Task 4).

**Modified files:**
- `src/kinoforge/providers/runpod/heartbeat.py` — `RunPodGraphQLHeartbeatEndpoint.write` / `.read` rewritten per probe outcome (Task 2).
- `tests/providers/runpod/test_heartbeat.py` — wire-shape unit tests rewritten per branch (Task 2).
- `src/kinoforge/_adapters.py` — delete `_RUNPOD_HEARTBEAT_SAFE_ENGINES` constant + engine-kind `ValidationError` block (Task 3).
- `tests/test_adapters_heartbeat.py` — invert the guard test (Task 3).
- `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` — amend §3 with probe outcome (Task 1) + §16 closeout (Task 5).
- `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` — §9 wire-discovery note closure pointer (Task 5).
- `PROGRESS.md` — §C C25 strike-through (Task 5).
- `successful-generations.md` — new entry #7 OR entry #6 closure (Task 5).

Each task produces a single atomic commit per CLAUDE.md durability rules. RED scaffolds for Tasks 1 and 4 commit BEFORE the live invocations they drive.

---

## Task 1 (spec Task a): Env-semantics live probe

**Goal:** Disambiguate `podEditJob` env-array merge semantics on real RunPod and capture the outcome in a committed sidecar JSON that Task 2 reads.

**Files:**
- Create: `tests/live/test_runpod_env_semantics_probe.py`
- Create: `tests/live/_runpod_env_semantics.json` (output of live run)
- Modify: `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` (amend §3 with selected branch)

**Acceptance Criteria:**
- [ ] `tests/live/test_runpod_env_semantics_probe.py` exists and is gated by `KINOFORGE_LIVE_RUNPOD=1`.
- [ ] After live invocation, `tests/live/_runpod_env_semantics.json` contains `{"semantics": "additive" | "replace" | "read-unavailable", "captured_at": "<ISO local TZ>", "tested_pod_id": "<id>", "envelope": {...}}` with the envelope holding the full GraphQL response payload for the env query.
- [ ] Spec §3 amended with the outcome string and the chosen Task 2 branch.
- [ ] Pod destroyed at smoke end; absent from `myself.pods` post-destroy.
- [ ] Live spend ≤ $0.05.

**Verify:** `pixi run pytest tests/live/test_runpod_env_semantics_probe.py -v -s` (with `KINOFORGE_LIVE_RUNPOD=1`) → PASS; sidecar JSON written; spec amended in next commit.

**Steps:**

- [ ] **Step 1.1: Write the probe RED scaffold.** Create the file below verbatim. Module models after `tests/live/test_runpod_heartbeat_live.py`'s gate + offer-selection + try/finally teardown patterns.

```python
# tests/live/test_runpod_env_semantics_probe.py
"""Live probe: RunPod podEditJob env-array merge semantics (C25 Task 1).

Disambiguates whether RunPod's podEditJob mutation MERGES a single-key
env array into the pod's existing env map (Branch A path) or REPLACES
the whole env (Branch B path) OR does not surface env on the read side
(Branch B path).

Writes the outcome to ``tests/live/_runpod_env_semantics.json``. Task 2
of the C25 implementation plan reads that sidecar to pick the
RunPodGraphQLHeartbeatEndpoint wire shape.

Gated by ``KINOFORGE_LIVE_RUNPOD=1``. Live spend ceiling: $0.05.
Spec: docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md §8.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_BUDGET_USD_CAP = 0.05
_SESSION_LIFETIME_S = 120.0
_SIDECAR_PATH = Path("tests/live/_runpod_env_semantics.json")

# Probe env vars stamped on pod creation.
_PROBE_KEEP_A = ("PROBE_KEEP_A", "keep-a")
_PROBE_KEEP_B = ("PROBE_KEEP_B", "keep-b")
# Probe env var written by the podEditJob under test.
_PROBE_NEW = ("PROBE_NEW", "new-value")


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(
            f"set {_LIVE_GATE_ENV}=1 to run the env-semantics probe "
            f"(~$0.05 spend per invocation)"
        )


def test_runpod_env_array_merge_semantics() -> None:
    """Determine env-array semantics; write sidecar; destroy pod."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    api_key = creds.get("RUNPOD_API_KEY")
    assert api_key, "RUNPOD_API_KEY must be in environment for live probe"

    provider = RunPodProvider(creds=creds)

    # Pick the cheapest offer; the probe is GraphQL-only — no GPU needed.
    reqs = HardwareRequirements(
        min_vram_gb=0,
        min_cuda="0.0",
        max_usd_per_hr=10.0,
    )
    offers = provider.find_offers(reqs)
    assert offers, "no RunPod offers available"
    cheapest = min(offers, key=lambda o: o.cost_rate_usd_per_hr)
    estimated_spend = cheapest.cost_rate_usd_per_hr * (_SESSION_LIFETIME_S / 3600.0)
    assert estimated_spend <= _BUDGET_USD_CAP, (
        f"cheapest offer too expensive for ≤${_BUDGET_USD_CAP} budget: "
        f"{cheapest.cost_rate_usd_per_hr:.4f} USD/hr → "
        f"{estimated_spend:.5f} USD for {_SESSION_LIFETIME_S}s"
    )
    print(
        f"\nPod offer selected: id={cheapest.id!r} "
        f"cost={cheapest.cost_rate_usd_per_hr:.4f} USD/hr",
        file=sys.stderr,
    )

    spec = InstanceSpec(
        offer=cheapest,
        image="alpine:latest",
        env={_PROBE_KEEP_A[0]: _PROBE_KEEP_A[1],
             _PROBE_KEEP_B[0]: _PROBE_KEEP_B[1]},
        provision_script=None,
    )
    instance = provider.create_instance(spec)
    instance_id = instance.id
    print(f"Pod created: id={instance_id!r}", file=sys.stderr)

    semantics = "unknown"
    envelope: dict = {}

    try:
        # Wait for ready.
        deadline = time.monotonic() + _SESSION_LIFETIME_S
        while time.monotonic() < deadline:
            inst = provider.get_instance(instance_id)
            if inst.status == "ready":
                break
            time.sleep(3.0)
        else:
            pytest.fail(f"pod {instance_id} did not reach ready in {_SESSION_LIFETIME_S}s")

        # Issue podEditJob with a single-key env array.
        mutation = """
        mutation PodEditJob($input: PodEditJobInput!) {
          podEditJob(input: $input) { id }
        }
        """.strip()
        edit_resp = provider._http_post(  # noqa: SLF001 — wire-level probe needs the seam
            provider._base_url,
            {
                "query": mutation,
                "variables": {"input": {
                    "podId": instance_id,
                    "env": [{"key": _PROBE_NEW[0], "value": _PROBE_NEW[1]}],
                }},
            },
        )
        envelope["edit_resp"] = edit_resp
        if "errors" in edit_resp:
            pytest.fail(
                f"podEditJob env probe failed: {edit_resp['errors']!r}"
            )

        # Query the pod env back.
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) {
            id
            env { key value }
          }
        }
        """.strip()
        read_resp = provider._http_post(  # noqa: SLF001
            provider._base_url,
            {"query": query, "variables": {"podId": instance_id}},
        )
        envelope["read_resp"] = read_resp

        # Determine semantics.
        pod = (read_resp.get("data") or {}).get("pod") or {}
        env_field = pod.get("env")
        if env_field is None:
            # Pod query does not surface env at all — Branch B fallback.
            semantics = "read-unavailable"
        else:
            keys = {e.get("key") for e in env_field}
            has_a = _PROBE_KEEP_A[0] in keys
            has_b = _PROBE_KEEP_B[0] in keys
            has_new = _PROBE_NEW[0] in keys
            if has_a and has_b and has_new:
                semantics = "additive"
            elif has_new and not (has_a or has_b):
                semantics = "replace"
            else:
                pytest.fail(
                    f"unexpected env state: has_a={has_a} has_b={has_b} "
                    f"has_new={has_new}; envelope={envelope!r}"
                )
    finally:
        try:
            provider.destroy_instance(instance_id)
            print(f"Pod destroyed: id={instance_id!r}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARN: pod {instance_id!r} teardown raised {exc!r}; check console",
                file=sys.stderr,
            )

    sidecar = {
        "semantics": semantics,
        "captured_at": datetime.now().astimezone().isoformat(),
        "tested_pod_id": instance_id,
        "envelope": envelope,
    }
    _SIDECAR_PATH.write_text(json.dumps(sidecar, indent=2))
    print(
        f"RUNPOD_ENV_SEMANTICS={semantics} sidecar={_SIDECAR_PATH}",
        file=sys.stderr,
    )

    assert semantics in {"additive", "replace", "read-unavailable"}, (
        f"unexpected semantics value: {semantics!r}"
    )
```

- [ ] **Step 1.2: Commit the RED scaffold BEFORE the live invocation.**

```bash
git add tests/live/test_runpod_env_semantics_probe.py
git commit -m "test(c25): RED scaffold — RunPod env-array merge probe"
```

- [ ] **Step 1.3: Run the live probe.**

```bash
KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_runpod_env_semantics_probe.py -v -s
```

Expected: PASS within ~2 min wall, sidecar written, pod destroyed, stderr line `RUNPOD_ENV_SEMANTICS=<value>` printed.

- [ ] **Step 1.4: Amend spec §3 with the probe outcome.** Read `tests/live/_runpod_env_semantics.json`'s `semantics` field. In the spec at `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`, append a paragraph at the end of §3 ("Probe-then-branch flow") of the form:

> **Probe outcome (captured `<ISO>`):** `<semantics>`. Task 2 ships **Branch A** (env-additive) / **Branch B** (dockerArgs preserve-and-merge).

Replace `<semantics>` and the branch decision with the actual values.

- [ ] **Step 1.5: Commit the sidecar + spec amendment.**

```bash
git add tests/live/_runpod_env_semantics.json \
        docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md
git commit -m "live(c25): env-semantics probe = <semantics>; Task 2 picks Branch <X>"
```

```json:metadata
{
  "files": [
    "tests/live/test_runpod_env_semantics_probe.py",
    "tests/live/_runpod_env_semantics.json",
    "docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md"
  ],
  "verifyCommand": "KINOFORGE_LIVE_RUNPOD=1 pixi run pytest tests/live/test_runpod_env_semantics_probe.py -v -s",
  "acceptanceCriteria": [
    "test_runpod_env_semantics_probe.py exists and is KINOFORGE_LIVE_RUNPOD-gated",
    "sidecar JSON written with semantics in {additive, replace, read-unavailable}",
    "spec §3 amended with outcome and branch decision",
    "pod destroyed and absent from myself.pods",
    "live spend <= $0.05"
  ]
}
```

---

## Task 2 (spec Task b): Rewrite RunPodGraphQLHeartbeatEndpoint per probe outcome

**Goal:** Replace B5a's dockerArgs-JSON-overwrite write path with the wire shape selected by Task 1's probe — Branch A (env-slot, one round-trip) OR Branch B (dockerArgs preserve-and-merge, two round-trips).

**Files:**
- Modify: `src/kinoforge/providers/runpod/heartbeat.py`
- Modify: `tests/providers/runpod/test_heartbeat.py`

**Acceptance Criteria:**
- [ ] Sidecar `tests/live/_runpod_env_semantics.json` read; branch picked deterministically: `semantics == "additive"` → Branch A; otherwise → Branch B.
- [ ] Branch A: `RunPodGraphQLHeartbeatEndpoint.write` issues exactly ONE GraphQL round-trip per call; mutation payload contains `env: [{key: "KINOFORGE_LAST_HEARTBEAT", value: <iso>}]` and no `dockerArgs` key. Branch B: `write` issues exactly TWO round-trips (query then mutation); mutation `dockerArgs` value starts with whatever the pre-existing `dockerArgs` content was (with any prior `# _kinoforge_hb:` tail stripped) and ends with a single ` # _kinoforge_hb:<ISO>` tail.
- [ ] `read` returns the most-recently-written timestamp; returns `None` for never-written, instance-gone, and missing-marker cases; raises `TransportError` for corrupted ISO, GraphQL errors, and HTTP non-2xx.
- [ ] Wire-shape unit tests green on the selected branch. Branch-not-selected tests deleted, not skipped.

**Verify:** `pixi run pytest tests/providers/runpod/test_heartbeat.py -v` → PASS (7 tests on Branch A, 11 tests on Branch B — 9 + 2 standard arms).

**Steps:**

- [ ] **Step 2.1: Read the sidecar and pick the branch.**

```bash
python -c "import json; print(json.load(open('tests/live/_runpod_env_semantics.json'))['semantics'])"
```

If output is `additive` → execute Steps 2.A.* below and skip 2.B.*. Otherwise (`replace` or `read-unavailable`) → execute Steps 2.B.* below and skip 2.A.*.

### Branch A — env-additive (execute IFF semantics == "additive")

- [ ] **Step 2.A.1: Rewrite `src/kinoforge/providers/runpod/heartbeat.py`.** Replace the entire file body with:

```python
"""RunPod env-slot heartbeat satisfier (C25 Task 2, Branch A).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by writing the heartbeat timestamp into the pod's ``env`` array under
the dedicated key ``KINOFORGE_LAST_HEARTBEAT``. RunPod's ``podEditJob``
mutation merges a single-key env array into the existing pod env map
(verified by the C25 Task 1 probe), so the Phase 24 selfterm script and
all operator env vars survive untouched.

Read path queries ``pod { env { key value } }`` and walks for the
kinoforge key.

Spec: docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["RunPodGraphQLHeartbeatEndpoint"]

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_HEARTBEAT_ENV_KEY: str = "KINOFORGE_LAST_HEARTBEAT"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    env { key value }
  }
}
""".strip()


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth."""

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
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
            raise TransportError(
                f"RunPod GraphQL transport error: {exc.reason}"
            ) from exc
        try:
            decoded: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TransportError(f"RunPod GraphQL non-JSON response: {exc}") from exc
        return decoded

    return _post


class RunPodGraphQLHeartbeatEndpoint:
    """Env-slot satisfier: ``podEditJob`` writes a single env key.

    RunPod merges the single-key env array into the pod's existing env
    map (additive semantics confirmed by the C25 Task 1 probe), so the
    Phase 24 selfterm and operator env vars survive every tick.

    Both methods raise :class:`TransportError` on HTTP non-2xx, GraphQL
    ``errors`` arrays, JSON parse failures, and corrupted env values.
    Pod-gone (``data.pod == null``) and key-absent are valid ``None``
    returns, not transport failures.
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
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key)
        )

    def write(self, instance_id: str, ts_local: datetime) -> None:
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {"input": {
                "podId": instance_id,
                "env": [{"key": _HEARTBEAT_ENV_KEY, "value": ts_local.isoformat()}],
            }},
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"RunPod podEditJob transport failure: {exc}"
            ) from exc
        if "errors" in resp:
            raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")

    def read(self, instance_id: str) -> datetime | None:
        payload = {"query": _POD_QUERY, "variables": {"podId": instance_id}}
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod pod query failed: {resp['errors']}")
        pod = (resp.get("data") or {}).get("pod")
        if pod is None:
            return None
        env = pod.get("env") or []
        for entry in env:
            if entry.get("key") == _HEARTBEAT_ENV_KEY:
                value = entry.get("value")
                if not isinstance(value, str):
                    raise TransportError(
                        f"corrupted heartbeat env for {instance_id}: "
                        "key present but value not a string"
                    )
                try:
                    return datetime.fromisoformat(value)
                except ValueError as exc:
                    raise TransportError(
                        f"corrupted heartbeat env for {instance_id}: {value!r}"
                    ) from exc
        return None
```

- [ ] **Step 2.A.2: Rewrite `tests/providers/runpod/test_heartbeat.py` (Branch A — 7 tests).** Replace entire file body with the seven tests below. Each test injects a `spy_post` callable into `RunPodGraphQLHeartbeatEndpoint(http_post=spy_post)` and asserts payload shape or read behaviour.

```python
"""Wire-shape unit tests for the C25 Branch A env-slot satisfier."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint


class _SpyPost:
    """Records the sequence of (url, payload) calls and returns canned responses."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses

    def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self._responses[len(self.calls) - 1]


def _ep(spy: _SpyPost) -> RunPodGraphQLHeartbeatEndpoint:
    return RunPodGraphQLHeartbeatEndpoint(api_key="sk-test", http_post=spy)


def test_write_payload_shape() -> None:
    spy = _SpyPost([{"data": {"podEditJob": {"id": "pod-x"}}}])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    assert len(spy.calls) == 1
    payload = spy.calls[0][1]
    assert payload["variables"]["input"]["podId"] == "pod-x"
    env = payload["variables"]["input"]["env"]
    assert env == [{"key": "KINOFORGE_LAST_HEARTBEAT", "value": ts.isoformat()}]
    assert "dockerArgs" not in payload["variables"]["input"]


def test_read_walks_env_array() -> None:
    iso = "2026-06-13T11:16:26-07:00"
    spy = _SpyPost([{"data": {"pod": {"env": [
        {"key": "OTHER", "value": "x"},
        {"key": "KINOFORGE_LAST_HEARTBEAT", "value": iso},
        {"key": "ANOTHER", "value": "y"},
    ]}}}])
    ep = _ep(spy)
    got = ep.read("pod-x")
    assert got == datetime.fromisoformat(iso)


def test_read_missing_key_returns_none() -> None:
    spy = _SpyPost([{"data": {"pod": {"env": [{"key": "OTHER", "value": "x"}]}}}])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_pod_null_returns_none() -> None:
    spy = _SpyPost([{"data": {"pod": None}}])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_corrupted_iso_raises_transport_error() -> None:
    spy = _SpyPost([{"data": {"pod": {"env": [
        {"key": "KINOFORGE_LAST_HEARTBEAT", "value": "not-an-iso"},
    ]}}}])
    ep = _ep(spy)
    with pytest.raises(TransportError, match="corrupted heartbeat env"):
        ep.read("pod-x")


def test_write_graphql_errors_raises_transport_error() -> None:
    spy = _SpyPost([{"errors": [{"message": "boom"}]}])
    ep = _ep(spy)
    with pytest.raises(TransportError, match="podEditJob failed"):
        ep.write("pod-x", datetime(2026, 6, 13, 12, tzinfo=timezone.utc))


def test_tz_preservation_roundtrip() -> None:
    """Write ISO with -07:00 offset, fake-storage returns it, read preserves tzinfo."""
    iso = "2026-06-13T11:16:26-07:00"
    written_payload: dict[str, Any] = {}

    def fake_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if "podEditJob" in payload["query"]:
            written_payload.update(payload)
            return {"data": {"podEditJob": {"id": "pod-x"}}}
        # pod query
        env_arr = written_payload["variables"]["input"]["env"]
        return {"data": {"pod": {"env": env_arr}}}

    ep = RunPodGraphQLHeartbeatEndpoint(api_key="sk-test", http_post=fake_post)
    ts = datetime.fromisoformat(iso)
    ep.write("pod-x", ts)
    got = ep.read("pod-x")
    assert got is not None
    assert got.isoformat() == iso
    assert got.tzinfo is not None
```

### Branch B — dockerArgs preserve-and-merge (execute IFF semantics != "additive")

- [ ] **Step 2.B.1: Rewrite `src/kinoforge/providers/runpod/heartbeat.py`.** Replace the entire file body with:

```python
"""RunPod dockerArgs preserve-and-merge heartbeat satisfier (C25 Task 2, Branch B).

Implements :class:`~kinoforge.core.heartbeat_endpoints.HeartbeatEndpoint`
by appending a trailing bash comment ``# _kinoforge_hb:<ISO>`` to the
pod's ``dockerArgs`` field. The Phase 24 selfterm boot bash (set at pod
creation by :meth:`RunPodProvider._create_pod`) is preserved verbatim
because bash treats ``#`` as start-of-comment; pod restart re-runs the
preserved boot bash and the in-pod selfterm survives.

Single-writer invariant: B7's ``provision:<id>`` cooperative lock
guarantees only the holding orchestrator writes a pod's wire state
during a session; intra-orchestrator HeartbeatLoop is single-threaded.

Spec: docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import TransportError

__all__ = ["RunPodGraphQLHeartbeatEndpoint"]

_DEFAULT_GRAPHQL_URL: str = "https://api.runpod.io/graphql"

_HEARTBEAT_MARKER_KEY: str = "_kinoforge_hb"

_POD_EDIT_JOB_MUTATION: str = """
mutation PodEditJob($input: PodEditJobInput!) {
  podEditJob(input: $input) { id }
}
""".strip()

_POD_QUERY: str = """
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    dockerArgs
  }
}
""".strip()

# Strip stale ` # _kinoforge_hb:<ISO>` trailer from prior tick before
# re-appending. Anchored to end-of-string so mid-string `#` inside the
# Phase 24 bash decoder is never touched.
_STRIP_RE: re.Pattern[str] = re.compile(
    r"\s*#\s*" + re.escape(_HEARTBEAT_MARKER_KEY) + r":[^\n]*$"
)

# Read-side extractor.
_READ_RE: re.Pattern[str] = re.compile(
    r"#\s*" + re.escape(_HEARTBEAT_MARKER_KEY) + r":([^\n]+?)\s*$"
)


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a stdlib-urllib POST callable with Bearer auth."""

    def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310
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
            raise TransportError(
                f"RunPod GraphQL transport error: {exc.reason}"
            ) from exc
        try:
            decoded: dict[str, Any] = json.loads(data)
        except json.JSONDecodeError as exc:
            raise TransportError(f"RunPod GraphQL non-JSON response: {exc}") from exc
        return decoded

    return _post


def _merge_marker(base: str, ts_local: datetime) -> str:
    """Strip any stale heartbeat marker and append a fresh one."""
    stripped = _STRIP_RE.sub("", base)
    if stripped.strip() == "":
        return f": # {_HEARTBEAT_MARKER_KEY}:{ts_local.isoformat()}"
    return f"{stripped} # {_HEARTBEAT_MARKER_KEY}:{ts_local.isoformat()}"


class RunPodGraphQLHeartbeatEndpoint:
    """dockerArgs preserve-and-merge satisfier.

    Write path: read current dockerArgs → strip any prior heartbeat
    marker → append fresh ``# _kinoforge_hb:<ISO>`` trailer → mutate.
    Two GraphQL round-trips per tick.

    Read path: query dockerArgs → regex-extract trailing marker.
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
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key)
        )

    def _read_dockerargs(self, instance_id: str) -> str | None:
        """Return current dockerArgs string, or None if pod gone."""
        payload = {"query": _POD_QUERY, "variables": {"podId": instance_id}}
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
        if "errors" in resp:
            raise TransportError(f"RunPod pod query failed: {resp['errors']}")
        pod = (resp.get("data") or {}).get("pod")
        if pod is None:
            return None
        raw = pod.get("dockerArgs")
        return raw if isinstance(raw, str) else ""

    def write(self, instance_id: str, ts_local: datetime) -> None:
        base = self._read_dockerargs(instance_id)
        if base is None:
            # Pod gone; no-op write. Next tick or classify will surface it.
            return
        merged = _merge_marker(base, ts_local)
        payload = {
            "query": _POD_EDIT_JOB_MUTATION,
            "variables": {"input": {"podId": instance_id, "dockerArgs": merged}},
        }
        try:
            resp = self._http_post(self._graphql_url, payload)
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(
                f"RunPod podEditJob transport failure: {exc}"
            ) from exc
        if "errors" in resp:
            raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")

    def read(self, instance_id: str) -> datetime | None:
        raw = self._read_dockerargs(instance_id)
        if raw is None or raw == "":
            return None
        m = _READ_RE.search(raw)
        if m is None:
            return None
        value = m.group(1).strip()
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise TransportError(
                f"corrupted heartbeat marker for {instance_id}: {value!r}"
            ) from exc
```

- [ ] **Step 2.B.2: Rewrite `tests/providers/runpod/test_heartbeat.py` (Branch B — 9 + 2 standard tests).** Replace entire file body with:

```python
"""Wire-shape unit tests for the C25 Branch B dockerArgs preserve-and-merge satisfier."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from kinoforge.core.errors import TransportError
from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

PHASE24_BASH = (
    'bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh '
    '&& chmod +x /tmp/p.sh && bash /tmp/p.sh"'
)


class _SpyPost:
    """Records call sequence; canned responses queued in order."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses

    def __call__(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self._responses[len(self.calls) - 1]


def _ep(spy: _SpyPost) -> RunPodGraphQLHeartbeatEndpoint:
    return RunPodGraphQLHeartbeatEndpoint(api_key="sk-test", http_post=spy)


def _query_resp(docker_args: str | None) -> dict[str, Any]:
    if docker_args is None:
        return {"data": {"pod": None}}
    return {"data": {"pod": {"id": "pod-x", "dockerArgs": docker_args}}}


def _ok_mutation() -> dict[str, Any]:
    return {"data": {"podEditJob": {"id": "pod-x"}}}


def test_write_does_read_then_mutation() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH), _ok_mutation()])
    ep = _ep(spy)
    ep.write("pod-x", datetime(2026, 6, 13, 11, tzinfo=timezone.utc))
    assert len(spy.calls) == 2
    assert "pod(input:" in spy.calls[0][1]["query"]
    assert "podEditJob" in spy.calls[1][1]["query"]


def test_write_preserves_bash_base() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written.startswith(PHASE24_BASH)
    assert f"# _kinoforge_hb:{ts.isoformat()}" in written


def test_write_strips_stale_marker_before_appending() -> None:
    stale = f"{PHASE24_BASH} # _kinoforge_hb:2026-01-01T00:00:00-07:00"
    spy = _SpyPost([_query_resp(stale), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written.count("_kinoforge_hb:") == 1
    assert ts.isoformat() in written
    assert "2026-01-01" not in written


def test_write_bare_pod_produces_no_op_command() -> None:
    spy = _SpyPost([_query_resp(""), _ok_mutation()])
    ep = _ep(spy)
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    ep.write("pod-x", ts)
    written = spy.calls[1][1]["variables"]["input"]["dockerArgs"]
    assert written == f": # _kinoforge_hb:{ts.isoformat()}"


def test_write_idempotent_on_repeated_same_ts() -> None:
    ts = datetime(2026, 6, 13, 11, 16, 26, tzinfo=timezone(timedelta(hours=-7)))
    first = f"{PHASE24_BASH} # _kinoforge_hb:{ts.isoformat()}"
    spy = _SpyPost([
        _query_resp(PHASE24_BASH), _ok_mutation(),
        _query_resp(first), _ok_mutation(),
    ])
    ep = _ep(spy)
    ep.write("pod-x", ts)
    ep.write("pod-x", ts)
    second = spy.calls[3][1]["variables"]["input"]["dockerArgs"]
    assert second == first


def test_read_extracts_marker_from_bash_tail() -> None:
    iso = "2026-06-13T11:16:26-07:00"
    docker_args = f"{PHASE24_BASH} # _kinoforge_hb:{iso}"
    spy = _SpyPost([_query_resp(docker_args)])
    ep = _ep(spy)
    got = ep.read("pod-x")
    assert got == datetime.fromisoformat(iso)
    assert got.tzinfo is not None


def test_read_no_marker_returns_none() -> None:
    spy = _SpyPost([_query_resp(PHASE24_BASH)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_mid_string_hash_does_not_match() -> None:
    misleading = (
        f'bash -c "echo # _kinoforge_hb:foo && bash /tmp/p.sh"'
    )
    spy = _SpyPost([_query_resp(misleading)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_read_corrupted_iso_raises_transport_error() -> None:
    docker_args = f"{PHASE24_BASH} # _kinoforge_hb:not-an-iso"
    spy = _SpyPost([_query_resp(docker_args)])
    ep = _ep(spy)
    with pytest.raises(TransportError, match="corrupted heartbeat marker"):
        ep.read("pod-x")


# Standard arms preserved from B5a -----------------------------------------------

def test_read_pod_null_returns_none() -> None:
    spy = _SpyPost([_query_resp(None)])
    ep = _ep(spy)
    assert ep.read("pod-x") is None


def test_write_graphql_errors_raises_transport_error() -> None:
    spy = _SpyPost([
        _query_resp(PHASE24_BASH),
        {"errors": [{"message": "boom"}]},
    ])
    ep = _ep(spy)
    with pytest.raises(TransportError, match="podEditJob failed"):
        ep.write("pod-x", datetime(2026, 6, 13, 11, tzinfo=timezone.utc))
```

### Both branches

- [ ] **Step 2.3: Run unit tests; confirm green.**

```bash
pixi run pytest tests/providers/runpod/test_heartbeat.py -v
```

Expected: 7 passed (Branch A) OR 11 passed (Branch B).

- [ ] **Step 2.4: Run pre-commit.**

```bash
pixi run pre-commit run --files src/kinoforge/providers/runpod/heartbeat.py tests/providers/runpod/test_heartbeat.py
```

- [ ] **Step 2.5: Commit.**

```bash
git add src/kinoforge/providers/runpod/heartbeat.py tests/providers/runpod/test_heartbeat.py
git commit -m "feat(c25): RunPodGraphQLHeartbeatEndpoint = Branch <X> (<one-line explainer>)"
```

```json:metadata
{
  "files": [
    "src/kinoforge/providers/runpod/heartbeat.py",
    "tests/providers/runpod/test_heartbeat.py"
  ],
  "verifyCommand": "pixi run pytest tests/providers/runpod/test_heartbeat.py -v",
  "acceptanceCriteria": [
    "branch picked deterministically from sidecar semantics field",
    "Branch A: write issues 1 GraphQL roundtrip with env-only payload",
    "Branch B: write issues 2 roundtrips (query then mutation); merged dockerArgs preserves bash base and ends with single marker",
    "read returns most-recent write; None on missing/pod-null/no-marker; TransportError on corrupted ISO or GraphQL errors",
    "unit tests green; branch-not-selected tests deleted"
  ]
}
```

---

## Task 3 (spec Task c): Delete `_RUNPOD_HEARTBEAT_SAFE_ENGINES` guard

**Goal:** Remove the engine-kind allow-list and its `ValidationError` raise from `build_heartbeat_endpoint_for`; invert the existing guard test so it asserts the previously-blocked `(provider=runpod, mode=graphql-tag, engine.kind=comfyui)` combination now returns a working endpoint.

**Files:**
- Modify: `src/kinoforge/_adapters.py` (delete lines 62-67 + lines 114-127 per current spec line ranges).
- Modify: `tests/test_adapters_heartbeat.py` (invert `test_runpod_graphql_tag_refuses_unsafe_engine`).

**Acceptance Criteria:**
- [ ] `_RUNPOD_HEARTBEAT_SAFE_ENGINES` symbol absent from `src/kinoforge/_adapters.py`.
- [ ] No `if kind not in _RUNPOD_HEARTBEAT_SAFE_ENGINES` block survives in `build_heartbeat_endpoint_for`.
- [ ] `tests/test_adapters_heartbeat.py::test_runpod_graphql_tag_refuses_unsafe_engine` is RENAMED to `test_runpod_graphql_tag_allows_comfyui_engine_post_c25` and asserts a `RunPodGraphQLHeartbeatEndpoint` instance is returned.
- [ ] `test_runpod_graphql_tag_allows_safe_engine` (the `fake`-engine sibling) remains green or is consolidated into the comfyui test.
- [ ] All tests in `tests/test_adapters_heartbeat.py` green.

**Verify:** `pixi run pytest tests/test_adapters_heartbeat.py -v` → all PASS.

**Steps:**

- [ ] **Step 3.1: Delete `_RUNPOD_HEARTBEAT_SAFE_ENGINES` block from `src/kinoforge/_adapters.py`.** Remove lines 62-67 inclusive (the comment block + frozenset assignment):

```python
# C25: engines that do NOT inject a provision_script via Phase 24's
# dockerArgs path can safely share the heartbeat carrier. As of B5a-ship,
# only the test fake qualifies; ComfyUI / Diffusers / Fal / BedrockVideo
# all use provision_script. Hosted engines (Replicate / Runway / Luma)
# don't use RunPod at all so they're irrelevant.
_RUNPOD_HEARTBEAT_SAFE_ENGINES: frozenset[str] = frozenset({"fake"})
```

- [ ] **Step 3.2: Delete the engine-kind raise block inside `build_heartbeat_endpoint_for`.** Remove lines 114-127 inclusive (the `kind = cfg.engine.kind` + `if kind not in _RUNPOD_HEARTBEAT_SAFE_ENGINES` block). The surrounding code after the edit should read:

```python
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
```

- [ ] **Step 3.3: Update the `if TYPE_CHECKING` block.** No symbols change; leave the TYPE_CHECKING imports as-is.

- [ ] **Step 3.4: Invert `tests/test_adapters_heartbeat.py::test_runpod_graphql_tag_refuses_unsafe_engine`.** Replace lines 98-110 inclusive (the comfyui-refuses test) with:

```python
def test_runpod_graphql_tag_allows_comfyui_engine_post_c25() -> None:
    """C25 (post-fix): comfyui + graphql-tag heartbeat is safe because the
    wire-level satisfier no longer collides with Phase 24's dockerArgs
    selfterm injection. The pre-C25 guard at _RUNPOD_HEARTBEAT_SAFE_ENGINES
    is deleted; this test asserts the endpoint is built without raising.
    """
    cfg = _make_cfg(
        provider="runpod", heartbeat_mode="graphql-tag", engine_kind="comfyui"
    )
    creds = _StubCreds({"RUNPOD_API_KEY": "sk-fake"})
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint

    got = build_heartbeat_endpoint_for(cfg, creds)
    assert isinstance(got, RunPodGraphQLHeartbeatEndpoint)
```

The `test_runpod_graphql_tag_allows_safe_engine` (fake-engine) test remains as-is; the C25-context comment in its docstring should be updated to remove the `_RUNPOD_HEARTBEAT_SAFE_ENGINES` reference. Minimal docstring rewrite:

```python
def test_runpod_graphql_tag_allows_safe_engine() -> None:
    """Post-C25: every engine kind is permitted with graphql-tag mode."""
```

- [ ] **Step 3.5: Run the adapter tests.**

```bash
pixi run pytest tests/test_adapters_heartbeat.py -v
```

Expected: all pass; the inverted test now asserts ENABLED behaviour.

- [ ] **Step 3.6: Run pre-commit + full provider-test suite.**

```bash
pixi run pre-commit run --files src/kinoforge/_adapters.py tests/test_adapters_heartbeat.py
pixi run pytest tests/providers/ -v
```

- [ ] **Step 3.7: Commit.**

```bash
git add src/kinoforge/_adapters.py tests/test_adapters_heartbeat.py
git commit -m "feat(c25): delete _RUNPOD_HEARTBEAT_SAFE_ENGINES guard

substrate Protocol is the contract; engine identity no longer gates
heartbeat safety. wire-level fix in heartbeat.py makes every engine
safe with graphql-tag heartbeat mode."
```

```json:metadata
{
  "files": [
    "src/kinoforge/_adapters.py",
    "tests/test_adapters_heartbeat.py"
  ],
  "verifyCommand": "pixi run pytest tests/test_adapters_heartbeat.py -v",
  "acceptanceCriteria": [
    "_RUNPOD_HEARTBEAT_SAFE_ENGINES symbol absent from _adapters.py",
    "engine-kind ValidationError raise block removed from build_heartbeat_endpoint_for",
    "test_runpod_graphql_tag_refuses_unsafe_engine renamed + inverted to assert success",
    "fake-engine test passes unchanged",
    "tests/test_adapters_heartbeat.py all green"
  ]
}
```

---

## Task 4 (spec Task d): C25 acceptance smoke — Wan + ComfyUI + 2 CLI gens

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Prove gen2 auto-attaches to gen1's pod via B3 cross-CLI auto-discovery on a real Wan 2.1 14B T2V workload with `engine.kind=comfyui` + `heartbeat_mode=graphql-tag` enabled and no operator-supplied `--instance-id` / `--force-attach`. This is the production-limitation closure listed under `successful-generations.md` entry #6.

**Files:**
- Create: `tests/live/cfg_c25_wan_comfyui.yaml`
- Create: `tests/live/test_c25_warm_reuse_comfyui_wan_live.py`

**Acceptance Criteria:**
- [ ] Smoke is gated by `KINOFORGE_LIVE_RUNPOD=1` and `KINOFORGE_LIVE_TESTS=1`.
- [ ] Gen 1 cold-creates a pod via the standard ComfyUI + Wan provision path; ledger captures `pod_id_1`.
- [ ] Gen 2, fresh subprocess CLI 60s later with the SAME cfg/prompt and NO `--instance-id` / `--force-attach` flags, logs `warm-reuse: attached to <pod_id>` on stdout/stderr and `pod_id_2 == pod_id_1`.
- [ ] `gen2_elapsed < gen1_elapsed * 0.7` (cold-skip ratio threshold; mirrors B3 entry #6).
- [ ] Post-smoke direct GraphQL inspection:
  - Branch A: `KINOFORGE_LAST_HEARTBEAT` present in pod env AND `KINOFORGE_SELFTERM_SCRIPT` survives.
  - Branch B: `dockerArgs` contains BOTH the Phase 24 bash decoder substring AND exactly one ` # _kinoforge_hb:<ISO>` tail.
- [ ] `kinoforge destroy --id <pod_id>` succeeds; pod absent from `myself.pods` post-destroy.
- [ ] Live spend ≤ $0.30.

**Verify:** `KINOFORGE_LIVE_RUNPOD=1 KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c25_warm_reuse_comfyui_wan_live.py -v -s` → PASS.

**Steps:**

- [ ] **Step 4.1: Create `tests/live/cfg_c25_wan_comfyui.yaml`.** Near-copy of `examples/configs/runpod-comfyui-wan-t2v.yaml` with two added `compute` fields for B3 + B5a:

```yaml
# C25 acceptance-smoke cfg — Wan 2.1 14B t2v on RunPod + ComfyUI with
# B3 cross-CLI warm-reuse + B5a heartbeat substrate enabled.
#
# Differs from examples/configs/runpod-comfyui-wan-t2v.yaml by:
#   compute.warm_reuse_auto_attach: true   (B3 auto-discovery)
#   compute.heartbeat_mode: graphql-tag    (B5a satisfier; C25 ship makes this safe with ComfyUI)
#   compute.lifecycle.heartbeat_interval_s: 30
# Live spend ceiling: $0.30 across both gens.

engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"
    custom_nodes:
      - git: "https://github.com/kijai/ComfyUI-WanVideoWrapper"
        ref: "088128b224242e110d3906c6750e9a3a348a659b"
      - git: "https://github.com/kijai/ComfyUI-KJNodes"
        ref: "369c8aee9ad4641823d0ffd7035076bcd297b6f2"
      - git: "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite"
        ref: "4ee72c065db22c9d96c2427954dc69e7b908444b"

models:
  - ref: "hf:Kijai/WanVideo_comfy:Wan2_1-T2V-14B_fp8_e4m3fn.safetensors"
    kind: base
    target: diffusion_models
  - ref: "hf:Kijai/WanVideo_comfy:Wan2_1_VAE_bf16.safetensors"
    kind: vae
    target: vae
  - ref: "hf:Kijai/WanVideo_comfy:umt5-xxl-enc-fp8_e4m3fn.safetensors"
    kind: text_encoder
    target: text_encoders

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  warm_reuse_auto_attach: true
  heartbeat_mode: graphql-tag
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_usd_per_hr: 0.50
    gpu_preference:
      - "NVIDIA GeForce RTX 4090"
      - "NVIDIA RTX A5000"
      - "NVIDIA GeForce RTX 3090"
    disk_gb: 80
  lifecycle:
    idle_timeout: 25m
    job_timeout: 15m
    time_buffer: 5m
    max_lifetime: 90m
    boot_timeout: 30m
    budget: 4.0
    heartbeat_interval_s: 30

spec:
  graph_file: runpod-comfyui-wan-t2v.graph.json
  asset_node_ids: {}
  prompt_node_ids:
    positive: "16"
  prompt_input_field: positive_prompt
  node_overrides: {}

params:
  fps: 16
  num_frames: 81
  steps: 20
  width: 480
  height: 480
```

- [ ] **Step 4.2: Create `tests/live/test_c25_warm_reuse_comfyui_wan_live.py`.** Models after `tests/live/test_b3_warm_attach_live.py` — subprocess CLI invocations with the post-smoke GraphQL inspection step added.

```python
"""C25 acceptance smoke: Wan + ComfyUI + B3 cross-CLI warm-reuse + B5a heartbeat.

Closes the entry-#6 "Production limitation (C25)" paragraph. Two
identical ``kinoforge generate`` subprocess CLIs 60s apart on a real
Wan 2.1 14B T2V workload; gen2 auto-attaches to gen1's pod via B3's
``_scan_warm_candidates`` with no operator id-juggling.

Post-smoke a direct GraphQL inspection asserts the heartbeat carrier
slot (env var or dockerArgs trailer, depending on Task 2's branch)
contains the kinoforge marker AND the Phase 24 selfterm injection
survives.

Gated by ``KINOFORGE_LIVE_RUNPOD=1`` AND ``KINOFORGE_LIVE_TESTS=1``.
Live spend ceiling: $0.30 across both gens.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

_LIVE_GATE_ENV = "KINOFORGE_LIVE_RUNPOD"
_TESTS_GATE_ENV = "KINOFORGE_LIVE_TESTS"
_CFG_PATH = Path("tests/live/cfg_c25_wan_comfyui.yaml")
_PROMPT_PATH = Path("prompt-field-realistic.txt")
_SEMANTICS_SIDECAR = Path("tests/live/_runpod_env_semantics.json")
_SPEND_CAP_USD = 0.30


@pytest.fixture(autouse=True)
def _gate_on_live_env() -> None:
    if os.environ.get(_LIVE_GATE_ENV) != "1":
        pytest.skip(f"set {_LIVE_GATE_ENV}=1 to run the C25 acceptance smoke")
    if os.environ.get(_TESTS_GATE_ENV) != "1":
        pytest.skip(f"set {_TESTS_GATE_ENV}=1 to opt into live spend")


def _kinoforge_generate(
    *, cfg_path: Path, state_dir: Path, prompt: str, run_id: str,
    timeout_s: float = 3600.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pixi", "run", "kinoforge",
         "--state-dir", str(state_dir),
         "generate", "-c", str(cfg_path),
         "--prompt", prompt,
         "--mode", "t2v",
         "--run-id", run_id],
        capture_output=True, text=True, timeout=timeout_s, check=False,
    )


def _extract_pod_id_from_ledger(state_dir: Path) -> str | None:
    ledger_path = state_dir / "ledger.json"
    if not ledger_path.exists():
        return None
    data = json.loads(ledger_path.read_text())
    for entry in data.get("entries", []):
        if entry.get("provider_kind") == "runpod" and entry.get("instance_id"):
            return entry["instance_id"]
    return None


def _inspect_pod_via_graphql(pod_id: str, semantics_branch: str) -> dict[str, object]:
    """Direct GraphQL inspection of the C25 carrier slot."""
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.providers.runpod import RunPodProvider

    creds = EnvCredentialProvider()
    provider = RunPodProvider(creds=creds)
    if semantics_branch == "additive":
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) { id env { key value } }
        }
        """.strip()
    else:
        query = """
        query GetPod($podId: String!) {
          pod(input: {podId: $podId}) { id dockerArgs env { key value } }
        }
        """.strip()
    resp = provider._http_post(  # noqa: SLF001
        provider._base_url,
        {"query": query, "variables": {"podId": pod_id}},
    )
    pod = (resp.get("data") or {}).get("pod") or {}
    return pod


def test_c25_warm_reuse_comfyui_wan() -> None:
    assert _PROMPT_PATH.exists(), "standard test prompt missing"
    assert _CFG_PATH.exists(), f"cfg missing: {_CFG_PATH}"
    assert _SEMANTICS_SIDECAR.exists(), (
        "env-semantics sidecar missing — run Task 1 probe first"
    )
    semantics = json.loads(_SEMANTICS_SIDECAR.read_text())["semantics"]
    branch = "additive" if semantics == "additive" else "preserve-merge"

    prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    state_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"c25-smoke-{int(time.time())}"
    state_dir.mkdir(parents=True, exist_ok=True)

    pod_id_1: str | None = None
    try:
        # Gen 1: cold create.
        t0 = time.time()
        r1 = _kinoforge_generate(
            cfg_path=_CFG_PATH, state_dir=state_dir,
            prompt=prompt, run_id="c25-smoke-1",
        )
        gen1_elapsed = time.time() - t0
        assert r1.returncode == 0, (
            f"gen 1 failed: stderr={r1.stderr!r}\nstdout={r1.stdout!r}"
        )
        pod_id_1 = _extract_pod_id_from_ledger(state_dir)
        assert pod_id_1 is not None, "no pod id in ledger after gen 1"
        print(f"gen 1 elapsed: {gen1_elapsed:.1f}s pod={pod_id_1}")

        # Sleep 60s — well under idle_timeout (25m).
        time.sleep(60)

        # Gen 2: warm reuse via B3 auto-discovery.
        t0 = time.time()
        r2 = _kinoforge_generate(
            cfg_path=_CFG_PATH, state_dir=state_dir,
            prompt=prompt, run_id="c25-smoke-2",
        )
        gen2_elapsed = time.time() - t0
        assert r2.returncode == 0, (
            f"gen 2 failed: stderr={r2.stderr!r}\nstdout={r2.stdout!r}"
        )
        pod_id_2 = _extract_pod_id_from_ledger(state_dir)
        assert pod_id_2 == pod_id_1, (
            f"warm reuse failed: pod_id_2={pod_id_2!r} != pod_id_1={pod_id_1!r}"
        )

        combined = r2.stdout + r2.stderr
        assert "warm-reuse: attached to" in combined, (
            f"missing warm-reuse INFO; combined log:\n{combined}"
        )

        print(f"gen 2 elapsed: {gen2_elapsed:.1f}s")
        assert gen2_elapsed < gen1_elapsed * 0.7, (
            f"cold-skip ratio failed: "
            f"gen1={gen1_elapsed:.1f}s gen2={gen2_elapsed:.1f}s"
        )

        # Post-smoke GraphQL inspection.
        pod = _inspect_pod_via_graphql(pod_id_1, semantics)
        if branch == "additive":
            env = pod.get("env") or []
            keys = {e.get("key") for e in env}
            assert "KINOFORGE_LAST_HEARTBEAT" in keys, (
                f"heartbeat env var missing on pod: keys={keys!r}"
            )
            assert "KINOFORGE_SELFTERM_SCRIPT" in keys, (
                f"selfterm script env var missing on pod (Phase 24 collision!): "
                f"keys={keys!r}"
            )
        else:
            docker_args = pod.get("dockerArgs") or ""
            assert "bash /tmp/p.sh" in docker_args, (
                f"Phase 24 bash decoder missing from dockerArgs (C25 collision!): "
                f"{docker_args!r}"
            )
            markers = re.findall(r"#\s*_kinoforge_hb:", docker_args)
            assert len(markers) == 1, (
                f"expected exactly one heartbeat marker; got {len(markers)} "
                f"in dockerArgs={docker_args!r}"
            )
    finally:
        # Teardown.
        if pod_id_1 is not None:
            subprocess.run(
                ["pixi", "run", "kinoforge",
                 "--state-dir", str(state_dir),
                 "destroy", "--id", pod_id_1],
                check=False, timeout=120,
            )
```

- [ ] **Step 4.3: Commit the RED scaffold BEFORE the live invocation.**

```bash
git add tests/live/cfg_c25_wan_comfyui.yaml tests/live/test_c25_warm_reuse_comfyui_wan_live.py
git commit -m "test(c25): RED scaffold — Wan + ComfyUI 2-CLI warm-reuse smoke"
```

- [ ] **Step 4.4: Run the acceptance smoke.**

```bash
KINOFORGE_LIVE_RUNPOD=1 KINOFORGE_LIVE_TESTS=1 pixi run pytest \
  tests/live/test_c25_warm_reuse_comfyui_wan_live.py -v -s
```

Expected: PASS. Wall time ~12-15 min. Spend ~$0.10-0.30 depending on offer.

- [ ] **Step 4.5: Capture run summary.** Note: `pod_id`, `gen1_elapsed`, `gen2_elapsed`, cold-skip ratio, branch (additive / preserve-merge). Save into Task 5's `successful-generations.md` entry.

- [ ] **Step 4.6: Commit the smoke evidence.** Nothing new to add to git (sidecar updates if any). Just record the run in PROGRESS.md in Task 5.

```json:metadata
{
  "userGate": true,
  "tags": ["user-gate"],
  "files": [
    "tests/live/cfg_c25_wan_comfyui.yaml",
    "tests/live/test_c25_warm_reuse_comfyui_wan_live.py"
  ],
  "verifyCommand": "KINOFORGE_LIVE_RUNPOD=1 KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_c25_warm_reuse_comfyui_wan_live.py -v -s",
  "acceptanceCriteria": [
    "gen1 cold-creates pod and ledger captures pod_id_1",
    "gen2 fresh CLI 60s later (no --instance-id / --force-attach) attaches to pod_id_1 and logs 'warm-reuse: attached to <pod_id>'",
    "gen2_elapsed < 0.7 * gen1_elapsed",
    "post-smoke GraphQL: Branch A → KINOFORGE_LAST_HEARTBEAT in env AND KINOFORGE_SELFTERM_SCRIPT survives; Branch B → dockerArgs has Phase 24 bash decoder substring AND exactly one heartbeat marker",
    "pod destroyed; absent from myself.pods",
    "live spend <= $0.30"
  ],
  "requireEvidenceTokens": [
    ["gen1", "cold", "pod_id_1"],
    ["gen2", "warm", "attached", "pod_id_2"]
  ]
}
```

---

## Task 5 (spec Task e): Closeout docs

**Goal:** Document C25's closure in PROGRESS.md, the B5a spec wire-discovery note, this spec doc, and `successful-generations.md`.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md`
- Modify: `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`
- Modify: `successful-generations.md`

**Acceptance Criteria:**
- [ ] `PROGRESS.md` §C C25 entry struck-through with `CLOSED by <Task 4 commit SHA>` reference.
- [ ] `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §9 wire-discovery note amended with a closure pointer to this C25 spec.
- [ ] This C25 spec doc gets a §16 closeout block referencing Task 4's smoke evidence (pod_id, ratio, branch).
- [ ] `successful-generations.md` gets a new entry #7 capturing the `(runpod, comfyui, wan-2.1-14b, t2v)` warm-reuse + auto-attach + C25 heartbeat preserve-and-merge combination per the file's schema. Entry #6's "Production limitation (C25)" paragraph is struck-through with a pointer to entry #7.

**Verify:** `git log --oneline -1` shows the closeout commit; manual diff review of the four modified files.

**Steps:**

- [ ] **Step 5.1: Amend `PROGRESS.md` §C C25.** Find the C25 bullet (line ~199 per current PROGRESS state) and strike-through the body. Append `~~ — CLOSED by <SHA>; preserve-and-merge wire path landed; B3 auto-discovery confirmed on Wan workload (gen2 cold-skip ratio=<ratio> at pod=<pod_id>).` Use the actual Task 4 commit SHA and metrics captured in Step 4.5.

- [ ] **Step 5.2: Amend the B5a spec §9 wire-discovery note.** Open `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` and append a closing paragraph to §9:

> **2026-06-13 closure (C25):** the dockerArgs / selfterm collision flagged in this note is closed by `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` (Task 4 commit `<SHA>`). The wire shape ships as **Branch <X>** (env-additive / dockerArgs preserve-and-merge) per the C25 probe outcome. `_RUNPOD_HEARTBEAT_SAFE_ENGINES` guard deleted.

- [ ] **Step 5.3: Add §16 closeout to the C25 spec.** Open `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md` and append:

> ## 16. Closeout
>
> - Probe outcome: `<semantics>` captured `<ISO>` at pod `<probe_pod_id>`.
> - Implementation branch: `<A | B>`.
> - Acceptance smoke (Task 4): commit `<SHA>`; pod `<pod_id>`; gen1=<wall1>s gen2=<wall2>s ratio=<ratio>.
> - `_RUNPOD_HEARTBEAT_SAFE_ENGINES` deleted at commit `<SHA>`.
> - PROGRESS.md §C C25 struck-through at commit `<SHA>`.
> - `successful-generations.md` entry #7 appended at commit `<SHA>`.

- [ ] **Step 5.4: Append entry #7 to `successful-generations.md`.** Follow the file's preamble schema. Required sections per the file's existing format: stack triple, mode, kinoforge version, first-success SHA, date, layer/phase, new axis, exact command, YAML config inline, prompt reference, env vars (names only), region, capability key, output artifact, cost, success criterion, failure modes encountered before success, notes. Mirror entry #5's layout and entry #6's brevity. Stack triple: `runpod / ComfyUI / wan-2.1-14b-t2v`. New axis: "C25 preserve-and-merge — heartbeat-mode `graphql-tag` safe on real workload + B3 cross-CLI auto-attach landed on the Wan stack."

- [ ] **Step 5.5: Strike-through entry #6's production-limitation paragraph.** In `successful-generations.md`, find the `### Production limitation (C25)` section in entry #6 and convert to `### Production limitation (C25) — CLOSED` with the paragraph wrapped in `~~...~~` and a closing line: `Closed by entry #7 (commit <SHA>).`

- [ ] **Step 5.6: Commit.**

```bash
git add PROGRESS.md \
        docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md \
        docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md \
        successful-generations.md
git commit -m "docs(c25): closeout — Branch <X> shipped; B3 warm-reuse confirmed on Wan

C25 production-safety follow-up closed end-to-end: probe → branch
selection → wire-level fix → guard deletion → acceptance smoke.

PROGRESS §C C25 struck-through; B5a §9 amended; spec §16 closeout
appended; successful-generations entry #7 added (entry #6 production-
limitation struck-through)."
```

```json:metadata
{
  "files": [
    "PROGRESS.md",
    "docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md",
    "docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md",
    "successful-generations.md"
  ],
  "verifyCommand": "git log --oneline -1",
  "acceptanceCriteria": [
    "PROGRESS.md §C C25 struck-through with CLOSED-by-SHA",
    "B5a spec §9 amended with closure pointer",
    "C25 spec §16 closeout block appended",
    "successful-generations.md entry #7 added with full schema",
    "entry #6 production-limitation paragraph struck-through with pointer to #7"
  ]
}
```
