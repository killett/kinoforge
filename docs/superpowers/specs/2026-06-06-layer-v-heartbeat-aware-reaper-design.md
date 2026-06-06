# Layer V — heartbeat-aware reaper

Closes the Layer U §6 carry-forward "heartbeat-aware reaper consuming the
new sentinel-gate contract" candidate. Ships the first production
consumer of Layer U's `last_heartbeat` + `heartbeat_thread_tick` ledger
fields, plus the substrate that makes future layers (sweeper daemon,
cross-session warm-reuse, dashboards) cheap.

The work is fully offline-tested. No live cloud spend.

---

## 1. Goals + scope

**In scope:**

- A pure `core/reaper.py` module: `Verdict` enum, `Policy` dataclass,
  `classify()`, `partition()`, `_resolve()` per-entry-override helper,
  `DEFAULT_APPLY_POLICY`, `DEFAULT_STRICT_VERDICTS`.
- An impure `core/reaper_actor.py` module: `act_on_verdict()`,
  `provider_for()`, `sweep()`, `SweepReport`, `ActionResult`.
- Rewrite of `kinoforge reap` CLI: multi-provider dispatch via ledger
  `entry["provider"]`, dry-run default, `--apply` / `--include-orphans`
  / `--force-forget` / `--strict` / `--id` / `--format` flags.
- Bundled verdict surfacing in `kinoforge status` (one extra line via
  the same `classify` call).
- One new YAML field: `lifecycle.grace_after_session_s: float = 300.0`
  with corresponding `Lifecycle` dataclass field.
- A core-invariant scan extension forbidding I/O imports in
  `core/reaper.py`.

**Out of scope (Layer W+ candidates):**

- **Long-running `kinoforge sweeper` daemon** that loops `sweep()` on a
  cadence. Substrate is ready; daemon is the next layer.
- **In-session orchestrator integration**: orchestrator consulting
  `classify` for warm-reuse when `_states[id]` is missing across CLI
  invocations.
- **Cooperative lock between `deploy_session.__enter__` and the
  reaper**, so a session claiming a warm pod cannot be reaped
  mid-claim. Documented Risk 3.
- **JSON / YAML policy files** for declarative reaper configuration.
  CLI flags are sufficient at the operator scale Layer V targets; a
  daemon will need this.
- **Per-entry `heartbeat_interval_s` override**. Reader uses cfg's
  global value because that's the cadence the writer was configured
  with.
- **Real `provider.heartbeat()` implementations for RunPod /
  SkyPilot.** Both have native dead-man mechanisms; out of scope per
  Layer U §6.

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Layer V is a **substrate**, not a CLI patch. Pure `classify` + `Policy` + `partition` + `act_on_verdict` is the load-bearing extraction; `_cmd_reap` is one consumer of many. | Future Layer W daemon, Layer X dashboards, Layer Y warm-reuse retrofit all reuse the substrate. CLI-shaped logic would force re-extraction later. |
| D2 | Strict purity split: `core/reaper.py` (pure) vs. `core/reaper_actor.py` (impure). Enforced by `test_core_invariant.py` scan. | Matches PROGRESS:91 ("Strategy / validation / continuity / splitter helpers are pure functions"). Sentinel-gate logic lives in one architecturally-enforced place. |
| D3 | Verdict tree is **D-hybrid** from brainstorm Q2: `LIVE` / `IDLE_REAP` / `ORPHAN_REAP` / `OVERAGE_REAP` / `STALE_LEDGER` / `HEARTBEAT_UNKNOWN` / `UNROUTABLE`. | A-alone misses orphans, B-alone wastes the heartbeat signal, C-alone wastes Layer U. D realises Layer U's sentinel-gate contract in code. |
| D4 | `ORPHAN_REAP` fires when sentinel is stale AND `pod_age > grace_after_session_s`. | Sentinel-stale alone is not enough — distinguishes brand-new pod (no first tick yet) from genuinely orphaned pod. Grace window also lets cross-invocation warm-reuse complete before the reaper acts. |
| D5 | `STALE_LEDGER` (pod gone in provider) is acted on under default `--apply`. | Provider is authoritative on existence. Closes the latent ledger-drift bug in the current `reap()` (lines 692-694 force-forget against Local-only `live_ids`). |
| D6 | Dry-run is the default; `--apply` is opt-in. | Multi-provider blast radius. Mirrors `terraform plan` / `kubectl --dry-run=client` ergonomics operators trust. Also means future consumers (Layer W daemon, Layer Y orchestrator) get the verdict-surfacing path for free. |
| D7 | `ORPHAN_REAP` opt-in via `--include-orphans`. Default policy = `{IDLE_REAP, OVERAGE_REAP, STALE_LEDGER}`. | Stale sentinel is a "I might be wrong about session state" signal; require explicit operator acknowledgement. |
| D8 | `UNROUTABLE` (unknown provider / construction error) is its own first-class verdict, never destroyed implicitly. `--force-forget` opt-in. | Distinguishes "I don't know what this is" from "I confirmed it's gone". Forgetting unroutable entries under `--apply` would mask `RUNPOD_API_KEY`-unset misconfiguration. |
| D9 | `act_on_verdict` **re-classifies inside a Layer 18 per-instance lock** before destroying. | Eliminates the human-in-the-loop race window between dry-run snapshot and `--apply`. Closes Q6. |
| D10 | Lock key is `f"reaper/{instance_id}"`. TTL 30s. | Per-instance granularity lets parallel reapers/daemon proceed independently. TTL accommodates SkyPilot's slower API + `destroy_confirmed` retries. |
| D11 | YAML adds **one** field: `lifecycle.grace_after_session_s`. Reuses existing `idle_timeout_s`, `max_lifetime_s`, `heartbeat_interval_s`. | Matches Layer U D5/D6 minimal-surface precedent. New field is operator-visible and per-entry-overridable. |
| D12 | `classify` signature uses explicit threshold kwargs, NOT a `Lifecycle` instance. | Pure-function purity. Future Layer W daemon doesn't drag in a `Lifecycle` import to call `classify`. Bridge function `classify_from_lifecycle` wraps the unpacking. |
| D13 | Per-entry override via `_resolve(entry, field, default)` helper. Mirrors Layer S `_ledger_field_or_cfg`. | Same pattern operator already learned in Layer S `_cmd_status`. |
| D14 | Provider dispatch uses Layer S precedent: `registry.get_provider(entry["provider"])()`. Per-call cache by name. | Zero-arg factories already self-configure from env (`RUNPOD_API_KEY`, etc.). `.env` loader (Phase 14) handles secrets transparently. |
| D15 | `kinoforge status` adds a `verdict=<…>` line via the same `classify` call. | Exercises substrate from a second consumer in the same layer — validates that `classify` is genuinely reusable, not CLI-shaped. Operator gets a "what would reap do" preview without invoking reap. |
| D16 | Bundling the `status` line into Layer V (not deferred). | Zero new I/O, zero new ABC. ~10 LOC + 2 AC tests. Catches "the substrate is CLI-shaped" smell early. |

---

## 3. Architecture

### 3.1 Module map

```
src/kinoforge/
  core/
    reaper.py           NEW pure  ~180 LOC  Verdict, Policy, classify, partition, _resolve, DEFAULTs
    reaper_actor.py     NEW       ~130 LOC  act_on_verdict, provider_for, sweep, SweepReport, ActionResult
    config.py           MODIFIED  +5  LOC   LifecycleConfig.grace_after_session_s + validator
    interfaces.py       MODIFIED  +1  LOC   Lifecycle.grace_after_session_s field
  cli/
    _commands.py        MODIFIED  ~+120 LOC _cmd_reap rewrite, _cmd_status verdict line
    _main.py            MODIFIED  +8  LOC   --apply / --include-orphans / --force-forget / --strict / --id / --format flags
tests/
  core/
    test_reaper.py              NEW  ~25 tests
    test_reaper_actor.py        NEW  ~12 tests
    test_reaper_sweep.py        NEW  ~6  tests
    test_lifecycle.py           MODIFIED  +2 tests (grace_after_session_s wiring)
    test_config.py              MODIFIED  +3 tests (YAML round-trip, default, negative reject)
  cli/
    test_cmd_reap.py            NEW  ~10 tests
    test_cmd_status.py          MODIFIED  +2 tests (verdict line)
  test_core_invariant.py        MODIFIED  +1 scan (reaper.py purity)
examples/
  configs/*.yaml                MODIFIED  + lifecycle.grace_after_session_s comment
README.md                       MODIFIED  + Operator → Reaping section
PROGRESS.md                     MODIFIED  + Phase 37 entry
```

### 3.2 Layered architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  cli/_commands.py  (consumer)                                         │
│     _cmd_reap   → sweep() → table → [--apply] act_on_verdict loop     │
│     _cmd_status → classify(entry,…) → verdict line                    │
└──────────────────────────────────────────────────────────────────────┘
                       │ calls
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/reaper_actor.py   IMPURE   (locks + provider + ledger I/O)      │
│     sweep(store, ledger, cfg_thresholds, policy, clock, registry)     │
│     act_on_verdict(store, provider, entry, verdict, *, thresholds, clock)│
│     provider_for(entry, registry_get_provider, cache)                 │
│     SweepReport, ActionResult                                         │
└──────────────────────────────────────────────────────────────────────┘
                       │ calls
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/reaper.py   PURE   (zero I/O, zero stateful objects)            │
│     Verdict enum  ·  Policy dataclass  ·  DEFAULT_APPLY_POLICY        │
│     classify(entry, live_pod_ids, now, *, …thresholds) → Verdict      │
│     partition(verdicts_by_id, policy) → (to_act, to_skip)             │
│     _resolve(entry, field, default)                                   │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.3 Verdict tree

Concrete decision logic for `classify(entry, live_pod_ids, now, *,
idle_timeout_s, max_lifetime_s, heartbeat_interval_s,
grace_after_session_s)`:

Derived inputs per entry:

```
id          = entry["id"]
created_at  = entry["created_at"]
pod_age     = now - created_at
pod_up      = id in live_pod_ids
hb          = entry.get("last_heartbeat")
hb_tick     = entry.get("heartbeat_thread_tick")
hb_age      = now - hb            (when hb present)
sent_age    = now - hb_tick       (when hb_tick present)
sentinel_window = 3 * (heartbeat_interval_s or 30.0)
```

Per-entry threshold overrides:

```
idle  = _resolve(entry, "idle_timeout_s",        idle_timeout_s)
max_age = _resolve(entry, "max_lifetime_s",      max_lifetime_s)
grace = _resolve(entry, "grace_after_session_s", grace_after_session_s)
```

First match wins, top to bottom:

| # | Condition | Verdict | In `DEFAULT_APPLY_POLICY`? |
|---|---|---|---|
| 1 | `pod_up == False` | `STALE_LEDGER` | YES |
| 2 | `pod_up` AND `pod_age > max_age` | `OVERAGE_REAP` | YES |
| 3 | `pod_up` AND `hb_tick` AND `sent_age <= sentinel_window` AND `hb_age <= idle` | `LIVE` | no |
| 4 | `pod_up` AND `hb_tick` AND `sent_age <= sentinel_window` AND `hb_age > idle` | `IDLE_REAP` | YES |
| 5 | `pod_up` AND `hb_tick` AND `sent_age > sentinel_window` AND `pod_age > grace` | `ORPHAN_REAP` | no (requires `--include-orphans`) |
| 6 | `pod_up` AND `hb_tick` AND `sent_age > sentinel_window` AND `pod_age <= grace` | `LIVE` | no |
| 7 | `pod_up` AND (`hb_tick` absent OR `heartbeat_interval_s` is None in cfg) | `HEARTBEAT_UNKNOWN` | no |

`UNROUTABLE` is assigned outside `classify` — `sweep()` records it
when `provider_for(entry, …)` returns `None` or
`provider.list_instances()` raises.

**Why row 5 needs `pod_age > grace`:** sentinel-stale + new pod could
mean the first `HeartbeatLoop` tick hasn't fired yet, or that
cross-invocation warm reuse is about to claim the pod. The grace
window (default 5 min, configurable per-entry) prevents the reaper
from racing a legitimate session start.

**Why heartbeat_interval_s falls back to 30.0 when None in cfg:**
matches the `_cmd_status` Layer U convention
(`src/kinoforge/cli/_commands.py:557`). Defensive coherence — a
sentinel field written by an older session that ran with heartbeat on,
read by a current session whose YAML has heartbeat off, still classifies
sensibly.

### 3.4 Pure module — `core/reaper.py`

Skeleton:

```python
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    LIVE = "LIVE"
    IDLE_REAP = "IDLE_REAP"
    ORPHAN_REAP = "ORPHAN_REAP"
    OVERAGE_REAP = "OVERAGE_REAP"
    STALE_LEDGER = "STALE_LEDGER"
    HEARTBEAT_UNKNOWN = "HEARTBEAT_UNKNOWN"
    UNROUTABLE = "UNROUTABLE"


@dataclass(frozen=True)
class Policy:
    """Which verdicts the consumer chooses to act on."""
    act_verdicts: frozenset[Verdict]


DEFAULT_APPLY_POLICY = Policy(
    act_verdicts=frozenset({
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.STALE_LEDGER,
    })
)

DEFAULT_STRICT_VERDICTS = frozenset({
    Verdict.UNROUTABLE,
    Verdict.HEARTBEAT_UNKNOWN,
})


def policy_from_cli_flags(
    *,
    apply: bool,
    include_orphans: bool = False,
    force_forget: bool = False,
) -> Policy:
    """Build the Policy a CLI invocation should use.

    Args:
        apply: True iff --apply flag was set; False is dry-run.
        include_orphans: True iff --include-orphans flag was set.
        force_forget: True iff --force-forget flag was set.

    Returns:
        Empty-act-set Policy when ``apply=False`` (dry-run). Default
        apply policy plus opt-ins otherwise.
    """
    if not apply:
        return Policy(act_verdicts=frozenset())
    act = set(DEFAULT_APPLY_POLICY.act_verdicts)
    if include_orphans:
        act.add(Verdict.ORPHAN_REAP)
    if force_forget:
        act.add(Verdict.UNROUTABLE)
    return Policy(act_verdicts=frozenset(act))


def _resolve(entry: Mapping[str, Any], field: str, default: float) -> float:
    """Per-entry threshold override with type-safe fallback to default.

    Mirrors Layer S `_ledger_field_or_cfg`. Bad types fall through to
    default rather than raising — defensive against ledger corruption.
    """
    val = entry.get(field)
    if val is None:
        return float(default)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def classify(
    entry: Mapping[str, Any],
    live_pod_ids: frozenset[str] | set[str],
    now: float,
    *,
    idle_timeout_s: float,
    max_lifetime_s: float,
    heartbeat_interval_s: float | None,
    grace_after_session_s: float,
) -> Verdict:
    """Classify a single ledger entry against the current world state.

    Pure function. No I/O. See §3.3 for the decision tree.

    Args:
        entry: A ledger entry dict; must carry ``id``. May carry per-entry
            threshold overrides via `idle_timeout_s` / `max_lifetime_s` /
            `grace_after_session_s` keys.
        live_pod_ids: Set of ids the provider currently reports as live.
        now: Wall clock seconds.
        idle_timeout_s: Default idle threshold from cfg.
        max_lifetime_s: Default hard ceiling from cfg.
        heartbeat_interval_s: Cfg heartbeat cadence; None means heartbeat
            feature is disabled in this CLI invocation.
        grace_after_session_s: Default post-session warm-reuse grace.

    Returns:
        One of the seven Verdict values (UNROUTABLE is set by sweep, not
        classify).
    """
    instance_id = str(entry["id"])
    created_at = float(entry.get("created_at", now))
    pod_age = now - created_at
    pod_up = instance_id in live_pod_ids

    if not pod_up:
        return Verdict.STALE_LEDGER

    idle = _resolve(entry, "idle_timeout_s", idle_timeout_s)
    max_age = _resolve(entry, "max_lifetime_s", max_lifetime_s)
    grace = _resolve(entry, "grace_after_session_s", grace_after_session_s)

    if pod_age > max_age:
        return Verdict.OVERAGE_REAP

    hb_tick = entry.get("heartbeat_thread_tick")
    hb = entry.get("last_heartbeat")
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        return Verdict.HEARTBEAT_UNKNOWN

    sentinel_window = 3 * heartbeat_interval_s
    sent_age = now - float(hb_tick)
    hb_age = now - float(hb)

    if sent_age <= sentinel_window:
        if hb_age <= idle:
            return Verdict.LIVE
        return Verdict.IDLE_REAP

    if pod_age > grace:
        return Verdict.ORPHAN_REAP
    return Verdict.LIVE


def partition(
    verdicts_by_id: Mapping[str, Verdict],
    policy: Policy,
) -> tuple[dict[str, Verdict], dict[str, Verdict]]:
    """Split a verdict snapshot into (to_act, to_skip) per the policy.

    Pure. Caller iterates `to_act` and dispatches via act_on_verdict.
    """
    to_act = {k: v for k, v in verdicts_by_id.items() if v in policy.act_verdicts}
    to_skip = {k: v for k, v in verdicts_by_id.items() if v not in policy.act_verdicts}
    return to_act, to_skip
```

### 3.5 Impure module — `core/reaper_actor.py`

```python
@dataclass(frozen=True)
class ActionResult:
    instance_id: str
    snapshot_verdict: Verdict
    applied_verdict: Verdict
    action: str
    # action ∈ {"destroyed_and_forgot", "forgot", "forgot_unroutable",
    #          "skipped", "failed", "no_op"}
    reason: str | None = None


@dataclass(frozen=True)
class SweepReport:
    snapshot: Mapping[str, tuple[Mapping[str, Any], Verdict]]
    actions: list[ActionResult]


def provider_for(
    entry: Mapping[str, Any],
    registry_get_provider: Callable[[str], Callable[[], ComputeProvider]],
    cache: dict[str, ComputeProvider | None],
) -> ComputeProvider | None:
    """Resolve a provider for an entry; None when unroutable.

    Caches by provider name within the sweep so N entries with the same
    provider produce one construction (one GraphQL ping on RunPod).
    """
    name = str(entry.get("provider", "local"))
    if name in cache:
        return cache[name]
    try:
        provider = registry_get_provider(name)()
    except Exception as exc:  # noqa: BLE001 — any factory failure is unroutable
        _log.warning("provider %r unroutable: %s", name, exc)
        cache[name] = None
        return None
    cache[name] = provider
    return provider


def act_on_verdict(
    store: ArtifactStore,
    ledger: Ledger,
    provider: ComputeProvider,
    entry: Mapping[str, Any],
    snapshot_verdict: Verdict,
    *,
    thresholds: Mapping[str, Any],
    clock: Clock,
) -> ActionResult:
    """Lock + re-classify + dispatch. Single side-effecting surface."""
    instance_id = str(entry["id"])
    with store.acquire_lock(f"reaper/{instance_id}", ttl_s=30.0):
        live_ids = {i.id for i in provider.list_instances()}
        v2 = classify(entry, live_ids, clock.now(), **thresholds)
        if v2 != snapshot_verdict:
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=v2,
                action="skipped",
                reason=f"verdict drift {snapshot_verdict.value} → {v2.value}",
            )
        try:
            if v2 in {Verdict.IDLE_REAP, Verdict.OVERAGE_REAP, Verdict.ORPHAN_REAP}:
                destroy_confirmed(provider, instance_id, sleep=lambda _: None)
                ledger.forget(instance_id)
                action = "destroyed_and_forgot"
            elif v2 == Verdict.STALE_LEDGER:
                ledger.forget(instance_id)
                action = "forgot"
            elif v2 == Verdict.UNROUTABLE:
                ledger.forget(instance_id)
                action = "forgot_unroutable"
            else:
                action = "no_op"
        except TeardownError as exc:
            return ActionResult(
                instance_id=instance_id,
                snapshot_verdict=snapshot_verdict,
                applied_verdict=v2,
                action="failed",
                reason=str(exc),
            )
        return ActionResult(
            instance_id=instance_id,
            snapshot_verdict=snapshot_verdict,
            applied_verdict=v2,
            action=action,
        )


def sweep(
    store: ArtifactStore,
    ledger: Ledger,
    registry_get_provider: Callable[[str], Callable[[], ComputeProvider]],
    thresholds: Mapping[str, Any],
    clock: Clock,
    *,
    policy: Policy | None = None,
) -> SweepReport:
    """Classify all ledger entries; optionally act."""
    now = clock.now()
    provider_cache: dict[str, ComputeProvider | None] = {}
    live_pod_ids_cache: dict[str, set[str]] = {}

    entries = list(ledger.entries())
    snapshot: dict[str, tuple[Mapping[str, Any], Verdict]] = {}

    for entry in entries:
        eid = str(entry["id"])
        provider = provider_for(entry, registry_get_provider, provider_cache)
        if provider is None:
            snapshot[eid] = (entry, Verdict.UNROUTABLE)
            continue
        name = str(entry.get("provider", "local"))
        if name not in live_pod_ids_cache:
            try:
                live_pod_ids_cache[name] = {i.id for i in provider.list_instances()}
            except Exception as exc:  # noqa: BLE001
                _log.warning("list_instances failed on %s: %s", name, exc)
                live_pod_ids_cache[name] = set()
                provider_cache[name] = None
                snapshot[eid] = (entry, Verdict.UNROUTABLE)
                continue
        verdict = classify(entry, live_pod_ids_cache[name], now, **thresholds)
        snapshot[eid] = (entry, verdict)

    if policy is None:
        return SweepReport(snapshot=snapshot, actions=[])

    to_act, _to_skip = partition(
        {eid: v for eid, (_, v) in snapshot.items()}, policy
    )
    actions: list[ActionResult] = []
    for eid, verdict in to_act.items():
        entry, _ = snapshot[eid]
        name = str(entry.get("provider", "local"))
        provider = provider_cache.get(name)
        if provider is None:
            continue  # surfaced earlier as UNROUTABLE
        result = act_on_verdict(
            store, ledger, provider, entry, verdict,
            thresholds=thresholds, clock=clock,
        )
        actions.append(result)
    return SweepReport(snapshot=snapshot, actions=actions)
```

### 3.6 Config

`Lifecycle` gains:

```python
@dataclass
class Lifecycle:
    # … existing fields …
    grace_after_session_s: float = 300.0   # NEW — Layer V (5 min default)
```

`LifecycleConfig` gains:

```python
class LifecycleConfig(BaseModel):
    # … existing fields …
    grace_after_session_s: float = 300.0

    @field_validator("grace_after_session_s")
    @classmethod
    def _validate_grace(cls, v: float) -> float:
        if v < 0:
            raise ValueError("grace_after_session_s must be >= 0")
        return v
```

`Config.lifecycle()` wires:

```python
def lifecycle(self) -> Lifecycle:
    lc = Lifecycle()
    # … existing field copies …
    lc.grace_after_session_s = self.lifecycle.grace_after_session_s
    return lc
```

YAML surface (additive, backwards-compat):

```yaml
lifecycle:
  idle_timeout_s: 7200
  max_lifetime_s: 28800
  heartbeat_interval_s: 30          # Layer U
  grace_after_session_s: 300        # Layer V — post-session warm-reuse window
```

### 3.7 CLI surface

#### `kinoforge reap`

| Flag | Default | Effect |
|---|---|---|
| (none) | dry-run | Print verdict table; exit 0. |
| `--apply` | off | Act on `DEFAULT_APPLY_POLICY`. |
| `--include-orphans` | off | Adds `ORPHAN_REAP` to act-set. Requires `--apply`. |
| `--force-forget` | off | Adds `UNROUTABLE` to act-set. Requires `--apply`. |
| `--strict` | off | Exit nonzero (3) if any `UNROUTABLE` / `HEARTBEAT_UNKNOWN` present. |
| `--id <X>` | sweep all | Restrict to one ledger entry. |
| `--format human\|json` | `human` | Table to stdout vs. JSONL records. |
| `--config PATH` / `-c` | none | Loads cfg for thresholds + `heartbeat_interval_s`. |

Exit codes:

- 0 — normal (dry-run or `--apply` with no actor errors)
- 2 — `--apply` actor raised `TeardownError` on one+ instance
- 3 — `--strict` triggered by `UNROUTABLE` / `HEARTBEAT_UNKNOWN` presence
- 4 — invalid flag combo (`--include-orphans` without `--apply`, etc.)

Dry-run output (human format):

```
verdict          id              provider  age_h  hb_age_s  sent_age_s  cost_h
LIVE             i-abc123         runpod    0.3    8         2           0.35
IDLE_REAP        i-def456         runpod    4.1    7300      9           0.35
ORPHAN_REAP      i-ghi789         runpod    2.5    -         9000        0.79
STALE_LEDGER     i-old001         skypilot  -      -         -           -
HEARTBEAT_UNKNOWN i-leg002        local     1.2    -         -           0.00

5 entries: 1 LIVE · 1 IDLE_REAP · 1 ORPHAN_REAP · 1 STALE_LEDGER · 1 HEARTBEAT_UNKNOWN
dry-run — pass --apply to act on 2 (IDLE_REAP, STALE_LEDGER)
add --include-orphans to also act on 1 ORPHAN_REAP
```

Apply output:

```
acted on 3 entries:
  destroyed i-def456 (runpod, IDLE_REAP, $1.44 accrued)
  destroyed i-ghi789 (runpod, ORPHAN_REAP, $1.98 accrued)
  forgot    i-old001 (skypilot, STALE_LEDGER)
skipped 1 entry: i-i_unrouted (UNROUTABLE — pass --force-forget to clean)
verdict drift detected on 0 entries (act-time re-classify)
```

#### `kinoforge status`

Existing key=value block gains one line via the same `classify` call:

```
verdict=IDLE_REAP
```

When `entry["provider"]` is unknown or the factory raises:
`verdict=UNROUTABLE`. When `provider.list_instances()` shows the id is
gone: `verdict=STALE_LEDGER`. Single source of truth for "what reap
would do" with this entry.

Layer U's existing sentinel-staleness advisory line is retained
(`heartbeat thread stale (Xs since last tick)`) — distinct from the
verdict, since the advisory describes the writer's state and the
verdict describes the destructive policy.

### 3.8 Drift handling

`act_on_verdict` re-classifies inside the lock. Possible outcomes:

- `v2 == snapshot_verdict` → dispatch as designed.
- `v2 != snapshot_verdict` → `ActionResult(action="skipped", reason="verdict drift X → Y")`. Provider unaffected. Summarized in `--apply` output as "verdict drift detected on N entries".

This is the load-bearing race-mitigation for D9: the snapshot is for
operator review, the lock-protected re-classify is for action.
Concurrent reapers (or a sweeper daemon) cooperate by holding the same
`reaper/{id}` key.

---

## 4. Acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC1 | `classify` returns `STALE_LEDGER` when `id ∉ live_pod_ids`. | `tests/core/test_reaper.py::test_stale_ledger_*` |
| AC2 | `classify` returns `OVERAGE_REAP` when `pod_age > max_lifetime_s` regardless of heartbeat freshness. | overage table tests |
| AC3 | Sentinel-fresh + `hb_age > idle_timeout_s` → `IDLE_REAP`. | idle table tests |
| AC4 | Sentinel-stale + `pod_age > grace_after_session_s` → `ORPHAN_REAP`. | orphan table tests |
| AC5 | Sentinel-stale + `pod_age <= grace_after_session_s` → `LIVE` (grace window honored). | grace boundary test |
| AC6 | Heartbeat fields absent → `HEARTBEAT_UNKNOWN`. | row-7 tests |
| AC7 | Per-entry `idle_timeout_s` override beats cfg threshold. | `_resolve` tests |
| AC8 | `partition` separates verdicts by `policy.act_verdicts`. | partition tests |
| AC9 | `act_on_verdict` re-classifies inside lock; verdict drift → `ActionResult(action="skipped")` without destroying. | drift test with provider spy |
| AC10 | `act_on_verdict` acquires `f"reaper/{id}"` lock. | lock-spy test |
| AC11 | `sweep` caches `provider.list_instances()` per provider name. | cache assertion test |
| AC12 | `sweep` continues across `TeardownError` on one instance; other actions complete. | failure-isolation test |
| AC13 | `kinoforge reap` default = dry-run; provider spy confirms zero destructive calls. | CLI spy test |
| AC14 | `kinoforge reap --apply --include-orphans` destroys `ORPHAN_REAP` entries. | CLI integration test |
| AC15 | `kinoforge reap --strict` with `UNROUTABLE` present → exit code 3. | CLI strict test |
| AC16 | `kinoforge reap --format json` emits JSONL: one record per snapshot entry + one per action. | CLI format test |
| AC17 | `kinoforge status` shows `verdict=<…>` line; matches what `classify` returned for the entry. | `_cmd_status` test |
| AC18 | `LifecycleConfig.grace_after_session_s` round-trips through YAML; negative values rejected. | `tests/core/test_config.py` |
| AC19 | `core/reaper.py` imports no I/O modules. | invariant scan |
| AC20 | Full test suite green; ruff/ruff-format/mypy clean; pre-commit `--all-files` passes. | `pixi run pre-commit run --all-files` |

---

## 5. Risks

1. **Provider cache holds connections across sweep duration.** Some
   providers may open sockets on construction. Layer V scope: log
   only. Documented as Layer W concern.
2. **`act_on_verdict` re-classify cost.** One extra `list_instances()`
   per acted entry → ~N additional API calls in `--apply`. For ~10
   instances on RunPod that's ~10 GraphQL calls, ~1s total.
   Acceptable. Mitigation if it bites: cache freshened
   `live_pod_ids` per provider per `--apply` pass — straightforward
   refactor.
3. **Race vs. concurrent `deploy_session.__enter__`.** Operator runs
   `--apply`; a new session starts on a warm-reuse pod at the same
   microsecond. If session is in setup before the first
   `HeartbeatLoop` tick fires → sentinel-stale → reaper destroys the
   session's pod. Mitigation: session code can `acquire_lock(f"reaper/{id}")`
   cooperatively when claiming a warm pod. Out of scope for Layer V;
   documented as Layer Y candidate.
4. **`HEARTBEAT_UNKNOWN` confusion for operators not running
   Layer U.** Status line could surprise. Mitigation: README documents
   the new verdict; CLI summary line explicitly calls it out
   informationally; never destructive.
5. **JSON encoding of float thresholds.** `grace_after_session_s`,
   `last_heartbeat`, `heartbeat_thread_tick` all use the same JSON
   number encoding `created_at` already uses. No new edge case.
6. **`Exception` catch in `provider_for` and `sweep`.** Two
   `# noqa: BLE001` sites are deliberate: provider construction and
   `list_instances` can raise anything from any vendor SDK. Logging at
   WARNING and demoting to `UNROUTABLE` is the right posture — a
   single misconfigured provider should not abort the sweep. Failures
   are visible in the verdict table.

---

## 6. Out of scope (Layer W+ candidates)

- **Layer W — `kinoforge sweeper` daemon.** `while True: sweep(...)`
  loop with config-driven `Policy` and per-provider cadence. Layer V
  substrate is ready; daemon needs only the loop + lock-driven mutual
  exclusion across instances.
- **Layer Y — in-session orchestrator integration.** Orchestrator
  consults `classify` for warm-reuse decisions when `_states[id]` is
  missing (cross-CLI-invocation reuse).
- **Layer Z — cost dashboard / metrics consumer.** Read-only consumer
  of `classify` over the ledger.
- **Cooperative lock between session-start and reaper** (mitigation
  for Risk 3 above).
- **Per-entry `heartbeat_interval_s` override.**
- **JSON / YAML policy file** (e.g. `--policy policy.yaml`).
- **Real `provider.heartbeat()` implementations for RunPod /
  SkyPilot** — both have native dead-man mechanisms.

---

## 7. Forward-compat hooks for downstream layers

Documenting the substrate's public surface so Layer W+ authors don't
have to re-derive it.

- **`classify` is the only place sentinel-gate logic lives.** Layer U
  §3.4 forward-compat contract realised in code, enforced by the
  `test_core_invariant.py` purity scan.
- **`Policy` is a frozen dataclass** taking `frozenset[Verdict]`. Layer W
  daemon constructs one from YAML; CLI constructs one from flags via
  `policy_from_cli_flags`. Same shape.
- **`sweep(...)` is the daemon entry point.** No extra wrapping
  needed; daemon iterates `while True: sweep(...); clock.sleep(...)`.
- **`act_on_verdict` is the single side-effecting surface.** Re-classify
  + lock semantics are owned here; any caller (CLI, daemon, future
  Layer Y orchestrator hook) inherits them.
- **Verdict ordering in the enum is stable.** External code may
  serialise verdicts; insertion order is part of the contract.
- **`UNROUTABLE` is the only verdict assigned outside `classify`.**
  Layer W and Y can rely on `classify`'s output set excluding
  `UNROUTABLE`; routing failures are a `sweep`-level concern.
