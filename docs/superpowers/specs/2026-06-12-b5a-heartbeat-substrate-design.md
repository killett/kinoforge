# B5a — `core/heartbeat_endpoints.py` substrate + RunPod satisfier

**Date:** 2026-06-12
**Status:** Design approved; ready for plan
**Tracking:** PROGRESS.md §B.B5; warm-reuse-tasks.txt (queue head)
**Spec hooks:**
- Upstream Layer V: `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §5 Risk 3 + §6 candidates
- Upstream Layer U: `src/kinoforge/core/heartbeat_loop.py` (eager-first-tick contract at `:152`)
- Downstream consumers: B1 (sweeper), B2 (cost dashboard), B3 (warm-reuse retrofit), B5b (SkyPilot satisfier)

---

## 1. Purpose

Today, `RunPodProvider.heartbeat()` and `SkyPilotProvider.heartbeat()` are pure no-ops with comments saying "native dead-man handles it" (`providers/runpod/__init__.py:445-452`, `providers/skypilot/__init__.py:785-791`). Neither provider implements `last_heartbeat()` at all — only `LocalProvider` does (`providers/local/__init__.py:189`).

When `HeartbeatLoop` (Layer U) runs against a real RunPod or SkyPilot deploy session today (`cfg.lifecycle.heartbeat_interval_s > 0`), the inner `provider.last_heartbeat(id)` call raises `AttributeError`, which the loop's broad `try/except Exception` silently swallows and re-logs every tick. The ledger entry never gains a meaningful `last_heartbeat` value. Layer V's `classify` then reads `None` forever and falls back to `HEARTBEAT_UNKNOWN` for every cloud instance — defeating the entire sentinel-gate contract on the providers that actually cost money.

B5a closes the substrate gap: a provider-agnostic `HeartbeatEndpoint` Protocol plus a RunPod GraphQL-tag satisfier so Layer V's classify, the future B1 sweeper, the future B2 cost dashboard, and the future B3 in-session warm-reuse retrofit all inherit truthful inputs on the only live cloud that costs money today. SkyPilot satisfier ships as B5b once GPU quota lands; the substrate Protocol is shape-agnostic, so the SkyPilot wire implementation slots in without ABI churn.

## 2. Decisions locked at brainstorm

Carried verbatim from the brainstorm prompt:

- Honesty-first axis chosen over felt-payoff axis. B5a substrate ships before any user-visible warm-reuse win.
- RunPod satisfier shape: GraphQL tag write/read (option (b) in warm-reuse-tasks.txt). `selfterm` HTTP (option (a)) deferred as a future second-satisfier slot-in.
- Heartbeat write path is SYNCHRONOUS, blocking the `HeartbeatLoop` tick. Tick interval (`cfg.lifecycle.heartbeat_interval_s`, default unset/30s when enabled) absorbs the round-trip cost. No async / fire-and-forget / background-queue shape.
- `heartbeat_mode` YAML field default is `"none"` for ALL providers (RunPod, SkyPilot, Local). Operators opt in per-deploy. No provider-specific default. Backward-compatible: existing deploys keep no-op heartbeat.
- Substrate Protocol is `runtime_checkable` — matches the codebase pattern (`Clock`, `ComputeProvider`). Tests use `isinstance(..., HeartbeatEndpoint)`.
- `TransportError` lives in `src/kinoforge/core/errors.py` next to the existing kinoforge error hierarchy. Not a substrate-module-local exception.
- GraphQL transport via injected `http` seam (`httpx.Client` passed as constructor kwarg; stdlib `urllib` default). Matches Phase 24 RunPod pattern ("All HTTP via injected seams"). No new SDK dependency.
- Live-spend authorized up to $0.05 for the B5a RunPod smoke.
- B5a includes a `FakeSkyPilotHeartbeatEndpoint` test double in `tests/providers/conftest.py` so B5b's contract is exercised before its wire-level real version ships.

Additional decisions made during this brainstorm:

- **YAML namespace.** `compute.heartbeat_mode: str = "none"` (sibling of `compute.provider`, NOT under `engine.*`). Warm-reuse-tasks.txt's `engine.runpod.heartbeat_mode` reference was structurally incorrect — `EngineConfig.kind` takes engine names (`comfyui`, `diffusers`, etc.); providers do not live under `engine.*`. `compute.lifecycle.heartbeat_interval_s` is the existing precedent: heartbeat-axis knobs live in `compute.*`. Warm-reuse-tasks.txt corrected at five sites in the same commit that creates this spec.
- **Datetime/float boundary.** `HeartbeatEndpoint` uses `datetime` (clean Python type, local-TZ idiom per `feedback_local_timezone_only`). `HeartbeatLoop` + `Ledger` keep `float | None` POSIX-epoch (unchanged). Conversion lives in `RunPodProvider.last_heartbeat` via `dt.timestamp()`. No upstream Layer U / Layer V signatures change.
- **`provider_heartbeat_supported` impl.** Hardcoded frozenset in `core/heartbeat_endpoints.py`. B5a-shipped set: `{"local", "runpod"}`. B5b adds `"skypilot"` (one-line change). No registry plumbing; core-purity preserved (string keys, no provider imports).
- **Classify verdict.** New `HEARTBEAT_SUBSTRATE_MISSING` verdict distinct from `HEARTBEAT_UNKNOWN`. Substrate-missing = no wire-level satisfier ships for this provider (sweeper MUST NEVER reap). Unknown = substrate works but data stale/absent (operator-opted-out path, sweeper dead-man fallback applies).
- **Auth scope for write.** `RUNPOD_API_KEY` (main key, not the scoped `RUNPOD_TERMINATE_KEY`). The terminate-scope key is delete-only; `podEditJob` mutation requires write scope. Live smoke verifies; if main key required, no operator action needed (key already in `.env`).

## 3. Architecture

Two new files, five surgical edits across the source tree:

**New:**
1. **`src/kinoforge/core/heartbeat_endpoints.py`** — substrate. Hosts `HeartbeatEndpoint` Protocol + `provider_heartbeat_supported` helper.
2. **`src/kinoforge/providers/runpod/heartbeat.py`** — `RunPodGraphQLHeartbeatEndpoint` satisfier. GraphQL tag write/read; injectable `http_post` seam.

**Edits:**
3. **`src/kinoforge/core/errors.py`** — adds `class TransportError(KinoforgeError)`.
4. **`src/kinoforge/core/config.py`** — adds `ComputeConfig.heartbeat_mode: str = "none"` + literal-set validator.
5. **`src/kinoforge/providers/runpod/__init__.py`** — constructor grows `heartbeat_endpoint: HeartbeatEndpoint | None = None`; `heartbeat(id)` + `last_heartbeat(id)` delegate when set, stay no-op when `None`. Backward-compatible.
6. **`src/kinoforge/_adapters.py`** — adapter dispatch builds `RunPodGraphQLHeartbeatEndpoint` from `cfg.compute.heartbeat_mode`; raises `AuthError` on missing `RUNPOD_API_KEY`; raises `ValidationError` on RunPod-incompatible mode strings.
7. **`src/kinoforge/core/reaper.py`** + **`src/kinoforge/core/reaper_actor.py`** (Layer V classify + act_on_verdict) — consults `provider_heartbeat_supported(provider_kind)` before emitting destructive verdicts; emits new `HEARTBEAT_SUBSTRATE_MISSING` verdict when unsupported; `act_on_verdict` grows a WARN-once no-destroy arm for the new verdict.

LocalProvider stays unchanged (in-memory dict still satisfies the contract). Optionally adapts `LocalHeartbeatEndpoint` wrapper for parity-test purposes — pure test hygiene, no production change.

## 4. `HeartbeatEndpoint` Protocol + `TransportError`

```python
# src/kinoforge/core/heartbeat_endpoints.py
from __future__ import annotations
from datetime import datetime
from typing import Protocol, runtime_checkable

@runtime_checkable
class HeartbeatEndpoint(Protocol):
    """Provider-agnostic substrate for orchestrator-side heartbeat I/O.

    Contract invariants (satisfied by every wire-level satisfier):
      - write(id, ts) is idempotent on duplicate ts (double-write same value is no-op).
      - read(id) returns the most-recently-written ts for id, or None if
        the instance is gone, the storage slot was never written, or the
        underlying side-channel was wiped.
      - read(id) precision: at least 1-second granularity. Sub-second
        precision is permitted but never required by consumers (Layer V
        dead-man window >> 1s).
      - Transport failures (HTTP non-2xx, SSH connection refused, GraphQL
        rate-limit) propagate as TransportError from BOTH write and read.
      - read returning None is NOT a transport failure — it is a valid
        "never written / instance gone" answer.
      - ts_local is a timezone-aware datetime in local TZ per project memory
        feedback_local_timezone_only. Satisfiers store-and-return the same
        TZ; round-trip preserves wall-clock.
    """
    def write(self, instance_id: str, ts_local: datetime) -> None: ...
    def read(self, instance_id: str) -> datetime | None: ...


_HEARTBEAT_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})

def provider_heartbeat_supported(provider_kind: str) -> bool:
    """Whether a wire-level HeartbeatEndpoint satisfier ships for this provider.

    B5a-shipped set: {"local", "runpod"}. B5b adds "skypilot". Consumers
    (Layer V classify, B1 sweeper, B3 warm-reuse) gate destructive verdicts
    on this flag — conservative-on-ignorance contract.
    """
    return provider_kind in _HEARTBEAT_SUPPORTED
```

`src/kinoforge/core/errors.py` grows:

```python
class TransportError(KinoforgeError):
    """Raised when a HeartbeatEndpoint satisfier's underlying transport fails.

    Examples: RunPod GraphQL non-2xx, SkyPilot SSH connection refused,
    selfterm HTTP timeout. Distinct from KinoforgeError because callers
    (HeartbeatLoop._tick_once) treat transport flakes differently from
    semantic errors — they retry on the next tick rather than aborting.
    """
```

**Datetime/float seam.** Conversion lives in `RunPodProvider.last_heartbeat`:

```python
def last_heartbeat(self, instance_id: str) -> float | None:
    if self._heartbeat_endpoint is None:
        return None
    dt = self._heartbeat_endpoint.read(instance_id)
    return dt.timestamp() if dt is not None else None
```

`.timestamp()` is TZ-correct on local-aware datetimes (converts to POSIX epoch UTC under the hood; round-trip via `datetime.fromtimestamp` gives local TZ back). Layer U `HeartbeatLoop` signature (`last_heartbeat -> float | None`) and Layer V `Ledger.touch(last_heartbeat: float | None = None, ...)` remain unchanged.

## 5. RunPod satisfier shape

`src/kinoforge/providers/runpod/heartbeat.py` (new) — `RunPodGraphQLHeartbeatEndpoint`.

**Storage slot.** Pod `tags` is the mutable per-pod label map exposed via `podEditJob` GraphQL mutation, returned in `pod` query response. Tag key: `_kinoforge_last_heartbeat`. Value: ISO-8601 local-TZ string (e.g. `2026-06-12T14:23:05-07:00`). Underscore prefix marks kinoforge-internal — operators reading tags in the RunPod console see the namespace.

**Write path:**

```python
def write(self, instance_id: str, ts_local: datetime) -> None:
    mutation = """
    mutation EditJob($input: PodEditJobInput!) {
      podEditJob(input: $input) { id }
    }
    """
    payload = {
        "query": mutation,
        "variables": {"input": {
            "podId": instance_id,
            "tags": [{"key": "_kinoforge_last_heartbeat",
                      "value": ts_local.isoformat()}],
        }},
    }
    resp = self._http_post(self._graphql_url, payload)
    if "errors" in resp:
        raise TransportError(f"RunPod podEditJob failed: {resp['errors']}")
```

Single mutation. Idempotent — same key+value rewrites same slot. `_http_post` is the injected seam (matches Phase 24 RunPod pattern); stdlib `urllib.request` default, injectable for tests.

**Read path:**

```python
def read(self, instance_id: str) -> datetime | None:
    query = """query GetPod($podId: String!) {
      pod(input: {podId: $podId}) { id tags { key value } }
    }"""
    resp = self._http_post(self._graphql_url, {
        "query": query,
        "variables": {"podId": instance_id},
    })
    if "errors" in resp:
        raise TransportError(f"RunPod pod query failed: {resp['errors']}")
    pod = resp.get("data", {}).get("pod")
    if pod is None:
        return None  # instance gone — valid None
    for tag in pod.get("tags") or []:
        if tag.get("key") == "_kinoforge_last_heartbeat":
            try:
                return datetime.fromisoformat(tag["value"])
            except ValueError as e:
                raise TransportError(
                    f"corrupted heartbeat tag for {instance_id}: {tag['value']!r}"
                ) from e
    return None  # never written — valid None
```

**Transport failure mapping:**
- HTTP non-2xx → `TransportError` (raised by `_http_post`, mirrors Phase 24 RunPodProvider).
- GraphQL `errors` array present in response → `TransportError`.
- `data.pod == null` → `read` returns `None` (instance destroyed; not a transport failure).
- Tag absent → `read` returns `None` (never written; not a transport failure).
- ISO parse failure → `TransportError` (corrupted slot; loud-on-violation).

**Constructor:**

```python
def __init__(
    self,
    *,
    api_key: str,
    graphql_url: str = "https://api.runpod.io/graphql",
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    self._api_key = api_key
    self._graphql_url = graphql_url
    self._http_post = http_post or _default_http_post  # stdlib urllib
```

The default `_http_post` honors `self._api_key` via `Authorization: Bearer …` header.

## 6. Provider wiring + YAML gate + adapter dispatch

**`ComputeConfig` grows one field** (`src/kinoforge/core/config.py`):

```python
class ComputeConfig(BaseModel):
    provider: str
    image: str
    mode: str = "pod"
    requirements: RequirementsConfig = RequirementsConfig()
    lifecycle: LifecycleConfig | None = None
    heartbeat_mode: str = "none"  # NEW

    @field_validator("heartbeat_mode")
    @classmethod
    def _validate_heartbeat_mode(cls, v: str) -> str:
        allowed = {"none", "graphql-tag", "selfterm-http", "ssh-touch"}
        if v not in allowed:
            raise ValueError(
                f"heartbeat_mode must be one of {sorted(allowed)}; got {v!r}"
            )
        return v
```

Value space is the union across all providers (B5a + B5b + future selfterm-HTTP). Provider-mode compatibility checked at adapter dispatch time (where the provider IS imported); config doesn't know which provider satisfies which mode without violating core-purity.

**`RunPodProvider` constructor grows one kwarg** (`src/kinoforge/providers/runpod/__init__.py`):

```python
def __init__(
    self,
    *,
    # ... existing kwargs ...
    heartbeat_endpoint: HeartbeatEndpoint | None = None,
) -> None:
    # ...
    self._heartbeat_endpoint = heartbeat_endpoint
```

`heartbeat()` + `last_heartbeat()` delegate:

```python
def heartbeat(self, instance_id: str) -> None:
    if self._heartbeat_endpoint is None:
        return  # no-op; pre-B5a behavior preserved
    self._heartbeat_endpoint.write(instance_id, datetime.now().astimezone())

def last_heartbeat(self, instance_id: str) -> float | None:
    if self._heartbeat_endpoint is None:
        return None
    dt = self._heartbeat_endpoint.read(instance_id)
    return dt.timestamp() if dt is not None else None
```

`datetime.now().astimezone()` = naïve-local-now promoted to TZ-aware local — satisfies the `ts_local` contract + `feedback_local_timezone_only`.

**Adapter dispatch** (`src/kinoforge/_adapters.py`):

```python
def _build_runpod_provider(cfg: Config, creds: CredentialProvider) -> RunPodProvider:
    from kinoforge.providers.runpod.heartbeat import RunPodGraphQLHeartbeatEndpoint
    mode = cfg.compute.heartbeat_mode
    endpoint: HeartbeatEndpoint | None
    if mode == "none":
        endpoint = None
    elif mode == "graphql-tag":
        api_key = creds.get("RUNPOD_API_KEY")
        if api_key is None:
            raise AuthError(
                "RUNPOD_API_KEY required when compute.heartbeat_mode == 'graphql-tag'"
            )
        endpoint = RunPodGraphQLHeartbeatEndpoint(api_key=api_key)
    else:
        raise ValidationError(
            f"RunPod does not support compute.heartbeat_mode={mode!r}; "
            f"valid: 'none', 'graphql-tag'"
        )
    return RunPodProvider(..., heartbeat_endpoint=endpoint)
```

Provider-specific compatibility lives in the dispatch site. No core-purity violation.

**Backward compatibility.** `compute.heartbeat_mode` defaults to `"none"` → `heartbeat_endpoint=None` → existing no-op behavior. Every existing YAML loads unchanged. The single new field is the only operator-visible surface.

## 7. Layer V classify hook

**New verdict.** `core/reaper.py` grows:

```python
class Verdict(Enum):
    LIVE = "live"
    IDLE_REAP = "idle_reap"
    ORPHAN_REAP = "orphan_reap"
    HEARTBEAT_UNKNOWN = "heartbeat_unknown"
    HEARTBEAT_SUBSTRATE_MISSING = "heartbeat_substrate_missing"  # NEW
```

**Classify gate.** Inside `classify()`, at the point `HEARTBEAT_UNKNOWN` would be emitted because `last_heartbeat is None`:

```python
if not provider_heartbeat_supported(entry.provider_kind):
    return Verdict.HEARTBEAT_SUBSTRATE_MISSING
return Verdict.HEARTBEAT_UNKNOWN
```

**Verdict semantics:**
- `HEARTBEAT_SUBSTRATE_MISSING` — no wire-level satisfier ships for this provider (e.g. SkyPilot pre-B5b). Operator can't fix per-entry. Sweeper MUST NEVER reap on this verdict. B1 / B3 inherit a hard-pin skip + WARN.
- `HEARTBEAT_UNKNOWN` — substrate works but data is stale or absent for this entry (e.g. RunPod with `heartbeat_mode="none"`, operator opted out). Sweeper's dead-man fallback still applies; operator-tunable.

**`act_on_verdict` change** (`core/reaper_actor.py`):

```python
case Verdict.HEARTBEAT_SUBSTRATE_MISSING:
    self._log_warning_once(
        f"provider {entry.provider_kind} has no heartbeat substrate; "
        f"skipping reap decision for {entry.instance_id} (B5b pending)"
    )
    return  # no destroy
```

`_log_warning_once` dedupes by `(provider_kind, instance_id)` — one line per pod, not per-tick spam.

**Ledger schema.** `provider_kind` already on ledger entries (Layer S). No migration. Existing `last_heartbeat` field unchanged.

**B1 / B2 / B3 inheritance.** All three are downstream of `classify`. None touch B5a directly. The interval contract — "treat `HEARTBEAT_SUBSTRATE_MISSING` as do-not-destroy on cloud during B5a-shipped-B5b-pending window" — lands as one verdict-table change here. B5b flips `provider_heartbeat_supported("skypilot")` to True; the verdict stops firing; no consumer code change required.

## 8. Test substrate

Three test layers. All offline; no live spend.

**a. Substrate Protocol tests** (`tests/core/test_heartbeat_endpoints.py`):

- `provider_heartbeat_supported("local")` → True
- `provider_heartbeat_supported("runpod")` → True
- `provider_heartbeat_supported("skypilot")` → False (asserts B5b not shipped — drift-detector; this test flips when B5b lands)
- `provider_heartbeat_supported("unknown")` → False
- `isinstance(FakeRunPodHeartbeatEndpoint(), HeartbeatEndpoint)` → True (runtime_checkable verification)
- `isinstance(FakeSkyPilotHeartbeatEndpoint(), HeartbeatEndpoint)` → True
- `isinstance(LocalHeartbeatEndpoint(clock=FakeClock()), HeartbeatEndpoint)` → True
- `TransportError` instance is also `KinoforgeError` instance (hierarchy invariant)

**b. Cross-provider parity tests** (`tests/providers/test_heartbeat_parity.py`):

One parametrized class covering all three satisfiers (Local + FakeRunPod + FakeSkyPilot):

- `test_read_of_never_written_returns_none`
- `test_write_then_read_round_trips_wall_clock`
- `test_double_write_same_ts_is_idempotent`
- `test_write_then_overwrite_returns_latest`
- `test_read_after_instance_destroyed_returns_none`
- `test_transport_failure_raises_TransportError_on_write`
- `test_transport_failure_raises_TransportError_on_read`
- `test_second_precision_minimum` — wall-clock round-trip preserves at least 1s of precision (SkyPilot mtime truncation tolerance)

The parity test is the load-bearing artifact: it freezes the Protocol contract from BOTH the RunPod (sub-second precision GraphQL ISO round-trip) and the SkyPilot (second-precision mtime) sides BEFORE B5b's wire implementation lands. When B5b ships, the parity test must still pass; only the SkyPilot real wire replaces `FakeSkyPilotHeartbeatEndpoint` in the live-path injection.

**c. RunPod satisfier wire-shape tests** (`tests/providers/runpod/test_heartbeat.py`):

Spy `http_post`; assert:

- write: POST URL == `graphql_url`; payload contains `podEditJob` mutation; `variables.input.podId == instance_id`; `variables.input.tags == [{"key": "_kinoforge_last_heartbeat", "value": "<iso>"}]`
- read: payload contains pod query; returns parsed datetime from tag
- write: GraphQL `errors` → `TransportError`
- write: HTTP non-2xx → `TransportError` (via the seam's existing mapping)
- read: `data.pod == null` → returns None (no `TransportError`)
- read: tag absent → returns None
- read: ISO parse failure → `TransportError`
- TZ preservation: write ISO with offset `-07:00`, read back, assert `tzinfo` matches

**d. HeartbeatLoop integration test** (extends `tests/core/test_heartbeat_loop.py`):

Inject `FakeRunPodHeartbeatEndpoint` into a `RunPodProvider`, wrap in `HeartbeatLoop`, run 3 ticks against a `FakeClock`, assert ledger entries have monotonically-advancing `last_heartbeat` floats matching the clock advance. Validates the full chain: `HeartbeatLoop → provider.heartbeat → endpoint.write → endpoint.read → provider.last_heartbeat → ledger.touch`.

**e. Layer V classify hook tests** (extends `tests/core/test_reaper.py`):

- `entry.provider_kind="skypilot"` + stale heartbeat → `HEARTBEAT_SUBSTRATE_MISSING` (B5a-shipped, B5b-pending)
- `entry.provider_kind="runpod"` + `heartbeat_mode="none"` + stale → `HEARTBEAT_UNKNOWN` (operator-opted-out path)
- `entry.provider_kind="runpod"` + fresh heartbeat → `LIVE` (substrate working)
- `entry.provider_kind="runpod"` + heartbeat older than dead-man window → `IDLE_REAP`
- `act_on_verdict` on `HEARTBEAT_SUBSTRATE_MISSING` → does NOT call `destroy_instance`; emits WARNING-once

**Fake satisfier shapes** (`tests/providers/conftest.py`):

- `FakeRunPodHeartbeatEndpoint`: dict-backed; sub-second-precision; `inject_transport_failure(method)` toggle raises `TransportError` on next call.
- `FakeSkyPilotHeartbeatEndpoint`: dict-backed with `mtime` field; round-trip truncates to seconds (mirrors `stat -c %Y`); injectable `cold_latency_s` simulates first-tick SSH cost; `inject_ssh_refused()` raises `TransportError`.

## 9. Live smoke

`tests/live/test_runpod_heartbeat_live.py`, gated by `KINOFORGE_LIVE_RUNPOD=1`:

- Spin up the cheapest bare RunPod pod (no GPU). Image: `runpod/base:ubuntu` or equivalent; ~$0.05/hr CPU pod.
- 60s session budget cap → live spend ≤ $0.001 wall, $0.05 budget envelope.
- Construct `RunPodGraphQLHeartbeatEndpoint(api_key=os.environ["RUNPOD_API_KEY"])` with prod stdlib `urllib` transport.
- Tick 1: `write(pod_id, t1)`, sleep 1s, `read(pod_id)` → assert equals `t1` (sub-second tolerance OK; ISO round-trip).
- Tick 2: `write(pod_id, t2)`, sleep 1s, `read(pod_id)` → assert equals `t2` (overwrite verified).
- Latency capture: record per-tick wall-time round-trip; print `RUNPOD_HEARTBEAT_LATENCY_MS_P50=… P99=…` for spec annotation.
- 429-detection: catch `TransportError` with `429` in message; if seen within 30s, fail hard with diagnostic — locks the substrate-invariant floor.
- Teardown: orchestrator destroys pod via existing path. Verify ledger entry's `last_heartbeat` matches the final write within 1s.
- Successful-generations log: NOT applicable (no video). Skip.

Budget ceiling per `feedback_autonomous_no_gates`: $0.05. Within session $20 envelope.

**Measured (2026-06-12 live smoke, RunPod GraphQL, dockerArgs carrier):**
- P50 round-trip: 460 ms
- P99 round-trip: 583 ms
- Rate-limit (429) observed within 60s @ 5s cadence: no
- Pod spec: NVIDIA RTX A2000 @ $0.12/hr (cheapest available offer; RunPod has no CPU-only GraphQL surface — see Task f wire-discovery note below)
- Estimated session spend: $0.002 USD (60s at $0.12/hr)
- Sidecar: `tests/live/_runpod_heartbeat_smoke_latencies.json`

**Wire-discovery note (Task f):** The B5a spec assumed `PodEditJobInput` has a `tags` field (`[{key, value}]`). Live API returned `BAD_USER_INPUT: Field "tags" is not defined by type "PodEditJobInput"`. The corrected carrier is `dockerArgs` (string), valid in both `podEditJob` (write) and `pod { dockerArgs }` (read). The heartbeat value is encoded as compact JSON: `{"_kinoforge_hb": "<ISO8601>"}`. The `HEARTBEAT_TAG_KEY` constant is preserved for reference; the wire key is `_kinoforge_hb` in `_HEARTBEAT_JSON_KEY`. Implementation updated in commit `0219a13` (heartbeat.py + wire-shape tests). The `dockerArgs` approach overwrites any genuine docker start command on the pod, which is acceptable for heartbeat-mode pods that have `provision_script=None`.

**2026-06-13 closure (C25):** the dockerArgs / selfterm collision flagged in this note is closed by `docs/superpowers/specs/2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`. The wire shape ships as **Branch B** (dockerArgs preserve-and-merge) per the env-semantics probe outcome (`tests/live/_runpod_env_semantics.json`, semantics = `read-unavailable`; RunPod's GraphQL `pod.env` is typed `[String]` with no subfields, so the env-slot satisfier path Branch A cannot verify a tick landed). The shipped satisfier reads current `dockerArgs`, strips any stale `# _kinoforge_hb:<ISO>` trailer, and appends a fresh one — Phase 24's selfterm bash decoder survives verbatim across every tick because bash treats `#` as start-of-comment. `_RUNPOD_HEARTBEAT_SAFE_ENGINES` allow-list deleted; the substrate Protocol is the contract again. Wire fix validated on production pod (Task 4 evidence at `tests/live/_c25_smoke_evidence.json`).

## 10. Task split

| # | Task | Files | Live |
|---|---|---|---|
| a | Substrate Protocol + `TransportError` + `provider_heartbeat_supported` + offline tests + Fake doubles | `core/heartbeat_endpoints.py`, `core/errors.py`, `tests/core/test_heartbeat_endpoints.py`, `tests/providers/conftest.py` | no |
| b | `RunPodGraphQLHeartbeatEndpoint` satisfier + wire-shape tests | `providers/runpod/heartbeat.py`, `tests/providers/runpod/test_heartbeat.py` | no |
| c | RunPod provider wiring + YAML gate + dispatch + integration test | `providers/runpod/__init__.py`, `core/config.py`, `_adapters.py`, `tests/providers/test_runpod.py`, `tests/core/test_heartbeat_loop.py` (extend) | no |
| d | Layer V classify hook + `HEARTBEAT_SUBSTRATE_MISSING` verdict + `act_on_verdict` WARN arm + classify tests | `core/reaper.py`, `core/reaper_actor.py`, `tests/core/test_reaper.py` (extend) | no |
| e | Cross-provider parity test suite (Local + FakeRunPod + FakeSkyPilot) | `tests/providers/test_heartbeat_parity.py` | no |
| f | Live RunPod heartbeat smoke + spec rate-limit annotation + PROGRESS closeout | `tests/live/test_runpod_heartbeat_live.py`, this spec amend, `PROGRESS.md` | ≤$0.05 |

Order: a → b → c → d in strict order (each consumes the prior). e parallel after a. f after c (needs prod RunPodProvider end-to-end).

**RED-scaffold commit policy.** Per `CLAUDE.md`, task f's smoke script + RED test commit BEFORE live invocation. Smoke is the qualifying live-spend artifact; mid-spend crash must leave the scaffold in git.

## 11. Risk register

Five concrete risks (carried from warm-reuse-tasks.txt B5a section; mitigations folded into the spec above):

1. **Per-tick cost (RunPod side).** GraphQL `podEditJob` mutation ~150ms; at 30s `heartbeat_interval`, that's 0.5% wall-time consumed by tick. Acceptable. Argues for default `heartbeat_interval_s >= 30s` when heartbeat enabled. The Layer U field validator already rejects non-positive values; no separate min-clamp lands in B5a, but is documented as a substrate invariant.
2. **RunPod GraphQL rate-limit (unknown).** Phase 24 didn't characterize this. If the mutation is rate-limited, the loop will throttle and Layer V will fire false-positive stale verdicts. **Mitigation:** B5a live smoke task f characterizes the actual rate-limit; if 429 surfaces within 30s of ticks, document the measured ceiling here as a substrate invariant and clamp the default interval upward. If 429 NEVER surfaces in 60s of ticks at 1s cadence, no clamp needed.

   **Mitigation update (2026-06-12 live smoke):** RunPod GraphQL did NOT return 429 at 5s-cadence ticks over 60s. No clamp needed. Document "no rate-limit observed at 5s cadence within 60s" as the working baseline; revisit if a future B1 sweeper running multi-pod 30s sweeps trips throttling.
3. **Substrate Protocol designed for one provider could miss SkyPilot quirks.** **Mitigation:** B5b Implementation Notes (§13 below) are committed INTO this spec as a top-level section, NOT as inline comments that decay. The Protocol code-review checklist explicitly verifies each B5b note is satisfied by the Protocol signature (datetime precision, failure modes, idempotency on double-write, read-of-never-written returns None).
4. **B5b drift risk.** Months may pass between B5a and B5b while GPU quota lands. **Mitigation:** (a) §13 below carries B5b Implementation Notes verbatim. (b) `FakeSkyPilotHeartbeatEndpoint` ships in `tests/providers/conftest.py` during B5a task a and exercises the Protocol from the SkyPilot side (touch-then-stat semantics, second-precision mtime, cold-vs-warm latency simulation). When B5b implementation lands, the test double IS the contract; only the wire implementation is new.
5. **Interval-state consumer dishonesty.** B1/B2/B3 ship during the B5a-shipped / B5b-not-shipped window. SkyPilot classify returns `HEARTBEAT_UNKNOWN` (pre-spec) or `HEARTBEAT_SUBSTRATE_MISSING` (post-spec); if B1 destroys on either, it destroys SkyPilot pods that are actively working. **Mitigation:** This spec introduces the dedicated `HEARTBEAT_SUBSTRATE_MISSING` verdict and the `provider_heartbeat_supported` helper. `act_on_verdict` has a hard-pin no-destroy + WARN-once arm for the new verdict. B5b flips one line in the helper; no consumer code change.

## 12. Out of scope / deferred

- **selfterm-HTTP RunPod satisfier (option (a)).** Second satisfier slot-in once option (b) is shipped and characterized. Substrate Protocol is shape-agnostic; new file `providers/runpod/selfterm_heartbeat.py` + new `heartbeat_mode = "selfterm-http"` dispatch arm.
- **Per-entry `heartbeat_interval_s` override** (B6 in PROGRESS.md). Today's `compute.lifecycle.heartbeat_interval_s` is per-deploy. A per-entry override would let operators tune live pods without restarting the orchestrator. Deferred.
- **SkyPilot satisfier (B5b).** Gated on A3 / A4 GPU quota landing. Spec hook above; FakeSkyPilotHeartbeatEndpoint freezes the contract.
- **`provider_heartbeat_supported` registry-mediated impl.** Hardcoded frozenset is the B5a choice. If a fourth provider lands with substrate support before B5b, revisit.
- **Sentinel-file write-on-pod alternative for RunPod** (in-pod sidecar writes the tag via GraphQL). Locked decision: orchestrator-side write+read. Future second-satisfier slot.

## 13. B5b Implementation Notes (verbatim from warm-reuse-tasks.txt:81-110)

These notes constrain the substrate Protocol so the SkyPilot satisfier slots in without ABI churn. Committed here at B5a-ship time to prevent drift.

> SkyPilot has no native side-channel; cluster state APIs don't expose a writable user metadata field. Three options:
> (a) touch a sentinel file via SSH (`ssh <id> touch /tmp/kf-heartbeat`) on write, `ssh <id> stat -c %Y` on read.
> (b) `sky exec <id> --` runs an arbitrary command on the cluster — same idea wrapped in SkyPilot's own SSH path.
> (c) the SkyPilot cluster YAML has a setup block that could start a local heartbeat HTTP daemon at launch, then provider methods curl it via the public SSH endpoint.
> Lock (a) for B5b first cut; per-tick cost is one SSH round-trip (~100ms on a warm connection, ~1s cold). SkyPilot's autostop watches the SSH session for idle, so a periodic touch ALSO acts as an autostop-defer signal — convenient secondary effect.
>
> SkyPilot timestamp precision is filesystem mtime (second granularity), NOT millisecond ISO. Substrate Protocol's `read()` returns datetime; second-truncation is acceptable. Heartbeat-staleness threshold in Layer V classify must NOT be sub-second; today it isn't (Layer V dead-man window is `heartbeat_interval_s * 3`, minimum ~30s). Documented as substrate invariant.
>
> SkyPilot SSH-cold latency (~1s) is 6x worse than RunPod GraphQL (~150ms). Substrate Protocol must NOT mandate a single per-tick latency budget; `HeartbeatLoop`'s `heartbeat_interval_s` already absorbs both. Document: implementers SHOULD complete write+read within `heartbeat_interval_s / 2` to leave margin.
>
> SkyPilot SSH failure modes (`Connection refused`, `ProxyCommand error`, `HostKeyVerificationFailed`) all map to `TransportError` per substrate contract. The SSH-touch path MUST reuse SkyPilot's existing SSH multiplexer config (`ControlMaster` in `~/.ssh/config` injected by `sky launch`) to avoid opening a new connection per tick — opening fresh SSH on every tick interacts badly with autostop's idle detection.

**Substrate Protocol code-review checklist** (verify before B5a merge):

- [ ] `read(instance_id) -> datetime | None` accepts second-precision return values (no fractional-second requirement on consumer side).
- [ ] `TransportError` is the umbrella type for SSH connection refused, ProxyCommand error, HostKeyVerificationFailed (B5b mapping target).
- [ ] `write(id, ts)` is idempotent on duplicate ts — a periodic SSH `touch` of the same file is naturally idempotent.
- [ ] `read` of a never-written slot returns None (matches `stat -c %Y` of a non-existent file: B5b satisfier must catch `FileNotFoundError` and return None).
- [ ] Protocol does NOT mandate a per-tick latency budget. `HeartbeatLoop.interval_s` absorbs the spread between RunPod (~150ms) and SkyPilot cold-SSH (~1s).
- [ ] `ts_local` is a timezone-aware datetime. SkyPilot satisfier converts mtime POSIX-seconds → `datetime.fromtimestamp(mtime).astimezone()` on read.

## 14. Acceptance criteria

A B5a-shipped state satisfies:

- AC1. `core/heartbeat_endpoints.py` exports `HeartbeatEndpoint` (Protocol, `runtime_checkable`) and `provider_heartbeat_supported`. `core/errors.py` exports `TransportError`.
- AC2. `RunPodGraphQLHeartbeatEndpoint` round-trips wall-clock writes through a spy `http_post` seam — assert ISO-string equality in the mutation payload, TZ preservation on read.
- AC3. `RunPodProvider(heartbeat_endpoint=...)` constructor accepts the kwarg; `heartbeat(id)` + `last_heartbeat(id)` delegate when set, no-op when None.
- AC4. `ComputeConfig.heartbeat_mode` accepts `{"none", "graphql-tag", "selfterm-http", "ssh-touch"}` and rejects anything else at config-load with `ValueError`. Default `"none"` preserves existing behavior.
- AC5. `_adapters.py` builds `RunPodProvider` with `heartbeat_endpoint=RunPodGraphQLHeartbeatEndpoint(...)` when `compute.heartbeat_mode == "graphql-tag"` and `RUNPOD_API_KEY` is set; raises `AuthError` if key missing; raises `ValidationError` on RunPod-incompatible mode (e.g. `"ssh-touch"`).
- AC6. `classify` emits `HEARTBEAT_SUBSTRATE_MISSING` when `last_heartbeat is None` AND `provider_heartbeat_supported(entry.provider_kind) is False`. Emits `HEARTBEAT_UNKNOWN` when `last_heartbeat is None` AND substrate supported (operator opt-out path).
- AC7. `act_on_verdict(HEARTBEAT_SUBSTRATE_MISSING)` does NOT call `destroy_instance` and emits a WARN-once log line.
- AC8. Cross-provider parity tests pass for Local + FakeRunPod + FakeSkyPilot fakes (8 invariants × 3 fakes = 24 tests).
- AC9. HeartbeatLoop integration test: 3-tick run against `RunPodProvider(heartbeat_endpoint=FakeRunPodHeartbeatEndpoint())` produces monotonically-advancing ledger `last_heartbeat` floats matching the injected `FakeClock` advance.
- AC10. Live smoke (task f): bare RunPod pod, 60s session, 2 heartbeat ticks, orchestrator-side `last_heartbeat` matches write time within 1s; per-tick latency P50 and P99 captured in spec amend.

## 15. PROGRESS.md updates on B5a close

- §B (Spec-locked future layers): B5 entry → strike through with "CLOSED by Phase NN". New B5b entry retained as gated on A3/A4.
- §Known limitations: no entries added (this layer closes a gap; doesn't introduce new ones).
- §RESUME — START HERE: pointer to B7 (next in queue) per warm-reuse-tasks sequencing.
- `successful-generations.md`: NOT applicable (no video; live smoke is heartbeat-only).
