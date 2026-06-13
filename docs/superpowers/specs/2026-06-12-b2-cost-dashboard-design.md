# B2 — Layer X: Cost Dashboard + Provider-Account Balance Readout

Status: design APPROVED 2026-06-12.

Prereqs (all CLOSED before B2): B5a (heartbeat substrate + RunPod satisfier, `bade08c` + `5aa2dcb`); B7 (cooperative session-claim lock, merge `b2d5b8b`); B4 (cross-CLI warm-reuse exposure, `54d2867`).

Spec hooks:
- `warm-reuse-tasks.txt` B2 entry (amended `b4f8240`, 2026-06-12) — locks `BalanceEndpoint` Protocol shape, per-provider feasibility matrix, stable `--json` schema, failure-mode contract, ~7-10 task envelope, zero-live-spend.
- `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §6 — Layer X candidate.
- `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §13 — `provider_heartbeat_supported` helper consumed by B2's `heartbeat_partial_truth` gate.

---

## 1. Brainstorm decision lock

Six OPEN questions resolved 2026-06-12 before spec-write.

| Q | Decision | Reason |
|---|---|---|
| **a** Burn-rate verdict set | All pod-up verdicts at full rate + per-verdict column breakdown. `_BURNING_VERDICTS = {LIVE, IDLE_REAP, OVERAGE_REAP, ORPHAN_REAP, HEARTBEAT_UNKNOWN, HEARTBEAT_SUBSTRATE_MISSING}`. STALE_LEDGER and UNROUTABLE excluded. | STALE_LEDGER is the only "pod gone" verdict; every other verdict sits on a pod that is UP at the provider and accruing cost. Operator-facing dashboard is honest about worst-case burn; uncertainty surfaces via the per-verdict column, not a sweetened scalar. |
| **b** Aggregation key | Per-provider only. Defer per-batch / per-session / per-run to a follow-on micro-layer. Zero ledger schema growth. | Today's ledger entry carries no `batch_id` / `run_id` / `session_id` join key. Adding one couples B2 to a schema mutation other layers will inherit. YAGNI — per-provider is the breakdown the spec already names; `kinoforge cost` scoped to the run_id-namespaced ledger.json implicitly bounds the per-session question. |
| **c** Prom layout | `kinoforge_*` prefix (collision-free); units in metric name; raw `Verdict` StrEnum label (8 values) + `provider` label on every gauge. | Idiomatic Prometheus; lets PromQL group / filter without re-emitting metrics; preserves honest verdict information for Grafana. |
| **d** Cache contract | Disk cache with TTL default 15s, `--cache-ttl=N` / `--no-cache` flags. Cache at `<store>/cost/balance_<provider>.json` via existing `cfg.store` routing. | `watch -n 2 kinoforge cost` would otherwise burn ~30 GraphQL calls/min — double the B5a-proven 5s-safe cadence. 15s TTL caps at 4 calls/min worst case. Cloud-store routing gives cross-machine caching for free (Layer T precedent). |
| **e** Replicate throttle home | `KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var; default `4.50`; `0` disables. | Single source. Threshold is operator-billing-specific (free-tier vs prepaid); leaks into YAML otherwise. Zero schema growth on a read-only dashboard layer. |
| **f** Balance type | `ProviderBalance(usd: float, as_of: datetime, source: str, currency: str = "USD")`. | Explicit name; zero collision risk with future `Money` libs. `currency` defaults to USD so today's call sites stay simple; future EUR/GBP provider adds a value without Protocol churn. Decimal precision unnecessary for dashboard display. |

---

## 2. Module split

Mirrors B5a substrate + per-provider satisfier separation.

```
src/kinoforge/core/cost.py                 # pure aggregator (walk + classify + group)
src/kinoforge/core/balance_endpoints.py    # Protocol + ProviderBalance + TransportError
                                           #   + NoBalanceEndpoint + provider_balance_supported helper
src/kinoforge/providers/runpod/balance.py  # RunPod GraphQL satisfier
src/kinoforge/cli/_adapters.py             # +build_balance_endpoint_for(cfg, creds) registry
src/kinoforge/cli/_commands.py             # +_cmd_cost + balance disk cache helpers
src/kinoforge/cli/__init__.py              # +`cost` subparser
examples/configs/cost.yaml                 # documented dashboard config
README.md                                  # +Cost dashboard section
```

No engine changes. No provider changes outside the new `providers/runpod/balance.py` file. No ledger schema diff. No new YAML fields. No new pydantic models on `Config`. The core-import-ban invariant (`tests/test_core_invariant.py`) is preserved: `core/cost.py` and `core/balance_endpoints.py` import nothing from `kinoforge.providers.*` / `kinoforge.engines.*` / `kinoforge.sources.*`.

---

## 3. Substrate — `core/balance_endpoints.py`

```python
"""Layer X: provider-agnostic balance-readout substrate.

Mirrors B5a `core/heartbeat_endpoints.py`. Provider construction is unchanged;
the BalanceEndpoint is built CLI-side via `build_balance_endpoint_for` and
called directly by `_cmd_cost`. Provider classes do not own the endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class TransportError(Exception):
    """Wire-level failure: 5xx, timeout, DNS, malformed body, schema drift.

    Cred-missing failures do NOT raise; satisfier returns None instead.
    """


@dataclass(frozen=True)
class ProviderBalance:
    usd: float
    as_of: datetime
    source: str
    currency: str = "USD"


class BalanceEndpoint(Protocol):
    """Read the operator's account balance with the provider.

    Implementations bind credentials at construction time; `read()` takes
    no arguments and returns a fresh balance or None.

    Failure contract:
      - Transport / 5xx / shape drift -> raise TransportError.
      - Missing credential -> return None.
      - Schema-valid response with negative balance -> return it verbatim.
    """

    def read(self) -> ProviderBalance | None: ...


class NoBalanceEndpoint:
    """Ships for every provider/engine without a satisfier.

    `read()` is unconditional None; `provider_balance_supported(kind)` returns
    False for the kind that resolves to this; renderer picks `balance: N/A`.
    """

    def read(self) -> None:
        return None


_SUPPORTED: frozenset[str] = frozenset({"runpod"})


def provider_balance_supported(provider_kind: str) -> bool:
    """True iff a real satisfier ships for `provider_kind`.

    Sister to B5a's `provider_heartbeat_supported`. Renderer uses this to
    pick `balance: N/A` (no satisfier) vs `balance: ? (no credential)` (no
    cred) vs `balance: $X` (success).
    """
    return provider_kind in _SUPPORTED
```

Three Protocols-and-helpers, one no-op, one error class, one dataclass. No I/O at the substrate layer. `NoBalanceEndpoint` is a concrete class; satisfiers are instances built by `build_balance_endpoint_for`.

---

## 4. RunPod satisfier — `providers/runpod/balance.py`

```python
"""RunPod GraphQL `clientBalance` reader.

One method; mirrors the existing `RunPodProvider` http-seam injection
pattern. Endpoint URL is the same `https://api.runpod.io/graphql` used
by `RunPodProvider._LIST_PODS_QUERY` at providers/runpod/__init__.py:774.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from kinoforge.core.balance_endpoints import ProviderBalance, TransportError

_QUERY = "{ myself { clientBalance } }"
_URL = "https://api.runpod.io/graphql"


def _default_http_post_json(
    url: str, body: dict, headers: dict[str, str], timeout_s: float = 10.0
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise TransportError(f"runpod-balance transport: {exc}") from exc


@dataclass
class RunPodBalanceEndpoint:
    api_key: str | None
    http_post_json: Callable[..., dict] = _default_http_post_json

    def read(self) -> ProviderBalance | None:
        if not self.api_key:
            return None
        body = {"query": _QUERY}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        resp = self.http_post_json(_URL, body, headers)
        try:
            usd = float(resp["data"]["myself"]["clientBalance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TransportError(f"runpod-balance schema drift: {exc}") from exc
        return ProviderBalance(
            usd=usd,
            as_of=datetime.now(),
            source="runpod-graphql-clientBalance",
        )
```

Schema-drift path: any of (`data` missing, `myself` missing, `clientBalance` missing, non-numeric value) → `TransportError`. The renderer treats schema drift and transport identically. Negative `usd` flows through verbatim per the failure-mode contract — RunPod auto-debit accounts can sit briefly negative and the operator deserves to see it.

`api_key` is bound at construction. `build_balance_endpoint_for` reads `RUNPOD_API_KEY` via the existing `EnvCredentialProvider`. Cred-missing path returns None at `read()` — the satisfier is still constructible, just inert.

---

## 5. Registry — `cli/_adapters.py`

```python
def build_balance_endpoint_for(
    cfg: Config, creds: CredentialProvider
) -> BalanceEndpoint:
    """Resolve a BalanceEndpoint for `cfg.compute.provider` or `cfg.engine.kind`.

    Returns NoBalanceEndpoint() for unknown / no-satisfier kinds. Never raises
    on lookup; only on construction faults (which today are impossible — the
    RunPod satisfier takes a credential that can be None).
    """
    provider_kind = cfg.compute.provider if cfg.compute else None
    engine_kind = cfg.engine.kind if cfg.engine else None
    kind = provider_kind or engine_kind
    if kind == "runpod":
        from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint

        return RunPodBalanceEndpoint(api_key=creds.get("RUNPOD_API_KEY"))
    return NoBalanceEndpoint()
```

Lazy import preserves the test_core_invariant.py vendor-SDK confinement scan. Hosted engines (replicate, runway, luma) fall through to `NoBalanceEndpoint`. So do unknown / unset provider kinds. So does `local` (LocalProvider has no balance concept). Mirrors `build_heartbeat_endpoint_for` dispatch shape from B5a; if the future calls for it, the helper grows a registry table.

---

## 6. Aggregator — `core/cost.py`

Pure. Mirrors `core/reaper.py` purity split. Input: ledger entries + `verdicts_by_id` + `now`. Output: frozen `CostSnapshot`. No I/O.

```python
"""Layer X: pure cost-aggregator substrate.

CLI owns ledger read + balance reads + classify call + env-var read + render.
This module folds the inputs into a CostSnapshot. Bad ledger entries (classify
raises mid-walk) are isolated: that entry is skipped + logged WARNING; the
rest of the snapshot is honest. Same isolation contract as `sweep()`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from kinoforge.core.balance_endpoints import ProviderBalance
from kinoforge.core.reaper import Verdict

logger = logging.getLogger(__name__)

_BURNING_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.LIVE,
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.ORPHAN_REAP,
        Verdict.HEARTBEAT_UNKNOWN,
        Verdict.HEARTBEAT_SUBSTRATE_MISSING,
    }
)


@dataclass(frozen=True)
class ProviderBreakdown:
    provider: str
    burn_rate_usd_per_hr: float
    spend_usd_total: float
    pod_counts_by_verdict: Mapping[Verdict, int]  # all 8 keys; zeros included


@dataclass(frozen=True)
class CostSnapshot:
    as_of: datetime
    burn_rate_usd_per_hr: float
    per_provider: tuple[ProviderBreakdown, ...]
    balances: Mapping[str, ProviderBalance | None]
    balance_errors: Mapping[str, str]
    heartbeat_partial_truth: tuple[str, ...]
    hosted_spend_pending: bool = True
    throttle_warnings: tuple[str, ...] = ()


def aggregate(
    entries: list[Mapping],
    verdicts_by_id: Mapping[str, Verdict],
    now: datetime,
    *,
    balances: Mapping[str, ProviderBalance | None],
    balance_errors: Mapping[str, str],
    heartbeat_partial_truth: tuple[str, ...],
    throttle_warnings: tuple[str, ...] = (),
) -> CostSnapshot:
    """Fold ledger + verdicts into a CostSnapshot.

    Pure. `entries` order does not affect outputs (per-provider tuple is
    sorted by provider name). `balances` / `balance_errors` are pass-through
    from the CLI, NOT computed here — aggregator does no I/O.
    """
    by_provider: dict[str, dict] = {}
    for entry in entries:
        instance_id = entry.get("id")
        if instance_id is None:
            continue
        verdict = verdicts_by_id.get(str(instance_id))
        if verdict is None:
            continue
        provider = str(entry.get("provider", "unknown"))
        rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        created_at = float(entry.get("created_at", now.timestamp()))
        slot = by_provider.setdefault(
            provider,
            {"burn": 0.0, "spend": 0.0, "counts": {v: 0 for v in Verdict}},
        )
        slot["counts"][verdict] += 1
        if verdict in _BURNING_VERDICTS:
            slot["burn"] += rate
            hours_up = max(0.0, (now.timestamp() - created_at) / 3600.0)
            slot["spend"] += rate * hours_up

    per_provider = tuple(
        ProviderBreakdown(
            provider=provider,
            burn_rate_usd_per_hr=slot["burn"],
            spend_usd_total=slot["spend"],
            pod_counts_by_verdict=dict(slot["counts"]),
        )
        for provider, slot in sorted(by_provider.items())
    )
    total_burn = sum(p.burn_rate_usd_per_hr for p in per_provider)
    return CostSnapshot(
        as_of=now,
        burn_rate_usd_per_hr=total_burn,
        per_provider=per_provider,
        balances=dict(balances),
        balance_errors=dict(balance_errors),
        heartbeat_partial_truth=heartbeat_partial_truth,
        throttle_warnings=throttle_warnings,
    )
```

Bad ledger entry (missing `id`, missing verdict, malformed `cost_rate_usd_per_hr`) → `continue` + WARNING (caller logs; aggregator is silent below ERROR level to keep the dashboard render fast). Zero-cost-rate entries fold in with zero burn but DO contribute to the per-verdict count — operator still sees the pod.

STALE_LEDGER and UNROUTABLE verdicts increment their per-verdict count but contribute zero to burn / spend per A(a). UNROUTABLE is the lone exception in the count dimension: a pod whose provider lookup failed has no `provider` field to bucket under, so it accumulates under `"unknown"` — same row, same column.

---

## 7. CLI — `kinoforge cost`

Subcommand grammar:

```
kinoforge cost                  # human-readable table (default)
kinoforge cost --json           # JSON per §10 schema
kinoforge cost --prom           # Prometheus text exposition per §9
kinoforge cost --no-cache       # bypass balance disk cache
kinoforge cost --cache-ttl=N    # override default 15s
```

`--json` and `--prom` are mutually exclusive; setting both → argparse error `error: --json and --prom are mutually exclusive`. Default mode is the human table.

`_cmd_cost(args)` flow:

1. Build ledger via `_build_store(cfg, state_dir)` + `Ledger(store, run_id)`.
2. `entries = ledger.entries()`.
3. For each entry, call `classify(entry, live_pod_ids, now, …)` with cfg-derived thresholds. `live_pod_ids` is `frozenset()` — `_cmd_cost` does NOT query providers for live pod IDs (would multiply latency, and the verdict at boundaries is still useful). Result: every up-pod is interpreted via the entry's own `heartbeat_thread_tick` + `last_heartbeat` data; STALE_LEDGER fires for entries whose provider is unreachable, which is fine for a dashboard.
4. For each distinct `provider` value seen in entries, build a `BalanceEndpoint` via `build_balance_endpoint_for` and read via cache helper (§8). Skip providers whose `NoBalanceEndpoint` would short-circuit; render `balance: N/A`.
5. Compute `heartbeat_partial_truth` from `core.heartbeat_endpoints.provider_heartbeat_supported(provider_kind)` over distinct providers; populate tuple.
6. Read `KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var; compute throttle warnings (stub today per §11).
7. Call `aggregate(...)` → `CostSnapshot`.
8. Render per mode.

`live_pod_ids` shortcut note: `classify` is row-by-row stateless on the live set; passing `frozenset()` means every entry hits Row 1's `not pod_up: return STALE_LEDGER` branch UNLESS we populate it. To preserve verdict accuracy, the CLI MUST populate `live_pod_ids` per provider. Cheapest path: lazy-import each provider, call `list_instances()`, union the IDs. Cost: one `list_instances()` per provider, same RunPod GraphQL endpoint that the existing `kinoforge list` already pays. Already cached at the per-provider HTTP-seam level if the user is composing `kinoforge cost` after `kinoforge list`; otherwise one extra round-trip per provider per invocation. Documented; `--no-cache` does NOT skip this call (the live-pod list is verdict-correctness, not balance-correctness).

For providers with no available `list_instances()` impl in-process (e.g. provider kind in ledger but provider module not imported this invocation) OR when the call raises any Exception, the CLI catches at the per-provider call site (`try: ids = provider.list_instances(); except Exception: log WARNING + fallback`) and falls back to `live_pod_ids = frozenset(<all ledger ids for that provider>)` — assume up; honest-on-ignorance per the sentinel-gate contract. The verdict degrades to LIVE/IDLE/HEARTBEAT_UNKNOWN per the heartbeat data; STALE_LEDGER is suppressed for that provider's rows in this invocation. The render path NEVER raises from a provider-call failure.

---

## 8. Balance disk cache

Cache key: `<store>/cost/balance_<provider>.json`. Written via `cfg.store.put_json`; read via `cfg.store.get_json`. Cloud-store users get cross-machine caching for free (Layer T precedent).

Cache entry schema:

```json
{
  "usd": 42.18,
  "as_of": "2026-06-12T14:32:01-07:00",
  "source": "runpod-graphql-clientBalance",
  "currency": "USD",
  "cached_at": "2026-06-12T14:32:01-07:00"
}
```

Algorithm:

```python
def cached_balance_read(
    *,
    store: ArtifactStore,
    provider: str,
    endpoint: BalanceEndpoint,
    cache_ttl_s: float,
    no_cache: bool,
    now: datetime,
) -> tuple[ProviderBalance | None, str | None]:
    """Return (balance, error_message). Either may be None.

    Stale-fallback: when no_cache=False and the wire fetch raises, the cached
    value is returned with an error message annotation; both are non-None.
    """
    key = f"cost/balance_{provider}.json"
    cached: dict | None = None
    if not no_cache:
        try:
            cached = json.loads(store.get_bytes(_run_id_for_cache, key).decode())
        except FileNotFoundError:
            cached = None
        if cached is not None:
            cached_at = datetime.fromisoformat(cached["cached_at"])
            age_s = (now - cached_at).total_seconds()
            if age_s < cache_ttl_s:
                return _balance_from_cache(cached), None
    try:
        fresh = endpoint.read()
    except TransportError as exc:
        if cached is not None:
            return _balance_from_cache(cached), f"transport (using cache): {exc}"
        return None, f"transport: {exc}"
    if fresh is None:
        return None, None
    if not no_cache:
        _write_cache(store, provider, fresh, now)
    return fresh, None
```

Stale-fallback contract: when a fresh fetch fails AND a cached entry exists (regardless of age), the renderer shows the cached value with a `(stale, transport error)` annotation in the human table and a `balance_errors.<provider>: "transport (using cache): <msg>"` entry in `--json`. The human table additionally prints `cached, Ns ago` whenever the rendered value came from cache (fresh-success and stale-fallback alike).

`--no-cache`: skips both the read AND the write. Stale-fallback path is unreachable under `--no-cache` (no cached entry to fall back to mid-invocation).

`_run_id_for_cache` is a fixed string `"_cost_cache"`, parallel to Layer S's `"_lifecycle"`. Lives alongside `ledger.json` in the store but in its own namespace.

---

## 9. Prometheus exposition

Five gauges + one counter, all `kinoforge_`-prefixed, raw Verdict StrEnum values as label values per A(c). All metrics use the `provider` label as the primary breakdown axis.

```
# HELP kinoforge_burn_rate_usd_per_hr Sum of cost_rate_usd_per_hr across pod-up verdicts.
# TYPE kinoforge_burn_rate_usd_per_hr gauge
kinoforge_burn_rate_usd_per_hr{provider="runpod"} 0.79

# HELP kinoforge_balance_usd Provider-account balance, when a balance endpoint ships.
# TYPE kinoforge_balance_usd gauge
kinoforge_balance_usd{provider="runpod"} 42.18

# HELP kinoforge_balance_as_of_seconds Unix timestamp the balance was read (or cached).
# TYPE kinoforge_balance_as_of_seconds gauge
kinoforge_balance_as_of_seconds{provider="runpod"} 1734036721

# HELP kinoforge_pod_count Pod count per provider per verdict.
# TYPE kinoforge_pod_count gauge
kinoforge_pod_count{provider="runpod",verdict="LIVE"} 1
kinoforge_pod_count{provider="runpod",verdict="IDLE_REAP"} 0
kinoforge_pod_count{provider="runpod",verdict="OVERAGE_REAP"} 0
kinoforge_pod_count{provider="runpod",verdict="ORPHAN_REAP"} 0
kinoforge_pod_count{provider="runpod",verdict="STALE_LEDGER"} 0
kinoforge_pod_count{provider="runpod",verdict="HEARTBEAT_UNKNOWN"} 0
kinoforge_pod_count{provider="runpod",verdict="HEARTBEAT_SUBSTRATE_MISSING"} 0
kinoforge_pod_count{provider="runpod",verdict="UNROUTABLE"} 0

# HELP kinoforge_spend_usd_total Lifetime $ spent on currently-up pods this provider.
# TYPE kinoforge_spend_usd_total gauge
kinoforge_spend_usd_total{provider="runpod"} 3.41

# HELP kinoforge_cost_scrape_errors_total Failed balance reads since process start.
# TYPE kinoforge_cost_scrape_errors_total counter
kinoforge_cost_scrape_errors_total{provider="runpod",reason="transport"} 0
```

`spend_usd_total` is a gauge (NOT counter) because the ledger isn't monotonic across `forget`s. `kinoforge cost --prom` is read-only per invocation; the counter `kinoforge_cost_scrape_errors_total` increments WITHIN one invocation only and resets between invocations — sized for textfile-collector cron pattern where each scrape is a fresh invocation. Operators wanting durable counters wire a sidecar that diffs.

Providers with `NoBalanceEndpoint` → `kinoforge_balance_usd` series absent; `kinoforge_balance_as_of_seconds` absent. `kinoforge_pod_count` and `kinoforge_burn_rate_usd_per_hr` and `kinoforge_spend_usd_total` always emit when the provider has at least one ledger entry. `kinoforge_cost_scrape_errors_total` always emits for every distinct provider in the ledger, even when zero (Prometheus convention: emit zeroes to keep series alive).

Textfile-collector cron pattern (documented in README):

```
*/30 * * * * kinoforge cost --prom > /var/lib/node_exporter/textfile/kinoforge.prom
```

---

## 10. Stable JSON schema

`--json` is authoritative; human + prom render derive from this same shape. Stable schema per Phase 33 / E24 precedent: future micro-layers add keys, never rename.

```json
{
  "as_of": "2026-06-12T14:32:01-07:00",
  "burn_rate_usd_per_hr": 0.79,
  "per_provider": [
    {
      "provider": "runpod",
      "burn_rate_usd_per_hr": 0.79,
      "spend_usd_total": 3.41,
      "pod_counts_by_verdict": {
        "LIVE": 1,
        "IDLE_REAP": 0,
        "OVERAGE_REAP": 0,
        "ORPHAN_REAP": 0,
        "STALE_LEDGER": 0,
        "HEARTBEAT_UNKNOWN": 0,
        "HEARTBEAT_SUBSTRATE_MISSING": 0,
        "UNROUTABLE": 0
      }
    }
  ],
  "balance": {
    "runpod": {
      "usd": 42.18,
      "as_of": "2026-06-12T14:32:01-07:00",
      "source": "runpod-graphql-clientBalance",
      "currency": "USD",
      "cached_age_s": 8
    }
  },
  "balance_errors": {},
  "heartbeat_partial_truth": [],
  "hosted_spend_pending": true,
  "throttle_warnings": []
}
```

Key semantics:

| Key | Type | Notes |
|---|---|---|
| `as_of` | ISO string | Local TZ per session memory `feedback_local_timezone_only`; never UTC. |
| `burn_rate_usd_per_hr` | float | Top-line = `sum(p.burn_rate_usd_per_hr for p in per_provider)`. |
| `per_provider` | list | Sorted by `provider` ascending. Always emits every provider that has ≥1 ledger entry. |
| `per_provider[].pod_counts_by_verdict` | object | All 8 Verdict StrEnum keys; zeros included. |
| `balance.<p>` | object \| null | `null` for missing-cred / no-satisfier / transport failure (with no cache fallback). Object with `cached_age_s: int` when fresh-success or stale-fallback. |
| `balance_errors.<p>` | string | Present only when an error occurred. Stable error prefixes: `"transport"`, `"transport (using cache)"`, `"schema drift"`, `"no credential"`. |
| `heartbeat_partial_truth` | list[string] | Provider kinds where `provider_heartbeat_supported(kind) == False` AND the ledger has ≥1 entry for that kind. Drops once B5b ships the SkyPilot satisfier. |
| `hosted_spend_pending` | bool | `true` until B10 (per-prediction hosted spend capture) lands. Signals that hosted-engine spend is not in `spend_usd_total`. |
| `throttle_warnings` | list[string] | Replicate threshold warnings. Empty until B10; see §11. |

---

## 11. Replicate throttle warning

`KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var read once at `_cmd_cost` startup. Default `4.50` (90% of Replicate's documented $5 free-tier soft-throttle per `engines/replicate/__init__.py:40`). `0` disables (no warning at any spend).

**Implication today (B2 ships, B10 does not):** Replicate uses `HostedAPIEngine` with `requires_compute=False`. Hosted engines do NOT register ledger entries today — `Ledger.record` is called only from `LifecycleManager.create_instance`, which hosted engines bypass. So the denominator for "replicate spend this session" is `0.00` until B10 (per-prediction hosted spend capture, PROGRESS.md B10) lands.

B2 wires the gate RED:

1. `_cmd_cost` reads the env-var, parses to float, defaults 4.50, treats `0` as disabled.
2. After aggregate, walks `per_provider` looking for `provider == "replicate"` rows.
3. Computes `spend_usd_total >= 0.9 * threshold` → adds to `throttle_warnings` tuple.
4. Renders the warning as `replicate spend $X.XX approaching $5 throttle (set KINOFORGE_REPLICATE_THROTTLE_AT_USD)` in the human table; emits the entry in `throttle_warnings` array in `--json`; no Prom metric (warnings are events, not measurements).

Today: condition is always False (no Replicate ledger entries exist). `throttle_warnings: []` always. Human table prints `replicate spend tracking pending B10` once at the bottom when env-var is set, to prevent operator confusion of "I set the env-var; why no warning?". B10 lights the path green: same `_cmd_cost` code; only the ledger-entry plumbing changes.

`HostedAPIBackend` will grow a single field someday — `Artifact.meta["cost_usd"]` per B10 — but that field-write happens engine-side. Two plausible B10 plumbing shapes (B10 picks; both preserve B2's `per_provider` interface):

(a) keep hosted spend in a separate sidecar (`<run_id>/<artifact>.cost.json`); B2's `_cmd_cost` reads sidecars alongside the ledger and folds them into a synthetic `provider="<engine_kind>"` row.

(b) push hosted artifacts onto the ledger with `cost_rate_usd_per_hr=0` plus a new `cost_usd` field on the entry; B2's aggregator grows one branch.

B2 ships neither today; the env-var gate is the only B10 hook in B2.

---

## 12. Failure-mode contract

| Failure | Aggregator | Renderer (human) | `--json` | `--prom` |
|---|---|---|---|---|
| Bad ledger entry (classify raises) | skip + WARNING | omit | omit (entry not counted in any aggregate) | omit (entry not counted) |
| Transport (5xx, timeout, DNS) | N/A | `balance: ? (transport error)` | `balance.<p>: null`; `balance_errors.<p>: "transport: <msg>"` | `kinoforge_balance_usd` series absent; `kinoforge_cost_scrape_errors_total{provider,reason="transport"} += 1` |
| Schema drift (missing `clientBalance` key) | N/A | `balance: ? (transport error)` | same as transport, prefix `"schema drift"` | series absent; `reason="schema"` counter |
| Missing credential | N/A | `balance: ? (no credential)` | `balance.<p>: null`; `balance_errors.<p>: "no credential"` | series absent; `reason="cred"` counter |
| Negative balance (auto-debit) | N/A | rendered verbatim, runway calc shows `< 0 h` | rendered verbatim | rendered verbatim (Prometheus accepts negative gauges) |
| No satisfier (replicate/runway/luma) | N/A | `balance: N/A` | `balance.<p>: null`, no entry in `balance_errors` | series absent (no counter increment — not an error) |
| Cached + fresh fetch failed | N/A | cached value + `(stale, transport error)` annotation | cached `usd`; `balance_errors.<p>: "transport (using cache): <msg>"` | cached value emitted; counter increments |
| `--no-cache` set, fetch failed | N/A | `balance: ? (transport error)` | `balance.<p>: null`; `balance_errors.<p>: "transport: <msg>"` | series absent; counter increments |

**Critical invariant:** transport / schema-drift / cred-missing / negative-balance / cache-IO failures NEVER raise from the render path. The `burn_rate_usd_per_hr` and `per_provider` rows ALWAYS render from ledger data alone (no provider call required). Balance is an additive overlay; its failure degrades only that one column. Tested via the offline `test_cmd_cost.py::test_balance_failure_does_not_block_burn_render` parametrized fixture (one case per failure row above).

`live_pod_ids` population (§7 step 3) is the one place where a provider call sits between "ledger read" and "render". If `list_instances()` raises, the CLI catches and falls back to "assume every ledger entry's id is live" — see the §7 fallback paragraph. Verdict-degradation is the price; render-failure is not acceptable.

---

## 13. Per-provider balance feasibility matrix (verified 2026-06-12)

Verbatim from the locked spec entry; reproduced for grep-ability.

| Provider | Source | B2 verdict |
|---|---|---|
| RunPod | `{ myself { clientBalance } }` GraphQL — one extra field on the existing `_LIST_PODS_QUERY` shape at `providers/runpod/__init__.py:774` | **SHIPS IN B2.** ~150 ms, reliable, no new transport. |
| Replicate | No public balance endpoint (dashboard-only). Engine already documents the < $5 soft-throttle at `engines/replicate/__init__.py:40`. | DOES NOT SHIP. `KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var shim — B2 reads + warns when burn × hours crosses it (gate is wired RED; B10 lights it green). |
| Runway | Generation surface only. | DOES NOT SHIP. Renders `balance: N/A`. |
| Luma | Direct video API retired in 2026 (Phase 44). | DOES NOT SHIP. Renders `balance: N/A`. |
| Bedrock | AWS Cost Explorer API — ~$0.01/req, ~24 h lag, requires `ce:GetCostAndUsage` IAM. | DEFERRED. Wrong shape for a live readout; track as follow-on layer when B10 lands. |
| Vertex | GCP Cloud Billing API — heavy IAM, comparable latency to Bedrock. | DEFERRED. Same reasoning as Bedrock. |
| SkyPilot | Pass-through to AWS / GCP / Azure billing. | DEFERRED. Same IAM + latency penalty as Bedrock / Vertex. |

---

## 14. Test surface

Mirrors B5a / B7 / B4 patterns. All offline. Zero live spend.

1. `tests/core/test_balance_endpoints.py`
   - `ProviderBalance` frozen-dataclass invariants (frozen, default currency, mutation raises).
   - `NoBalanceEndpoint.read()` returns None.
   - `provider_balance_supported("runpod")` → True; everything else → False.
   - `TransportError` is a subclass of Exception, not ValueError (avoids accidental catches).

2. `tests/core/test_cost.py`
   - Empty ledger → snapshot with `burn_rate_usd_per_hr == 0.0`, `per_provider == ()`.
   - Mixed verdicts: LIVE + IDLE_REAP + STALE_LEDGER + HEARTBEAT_UNKNOWN → STALE_LEDGER excluded from burn, others included; per-verdict counts all correct.
   - Per-provider aggregation: 3 RunPod entries + 1 SkyPilot entry → 2 `ProviderBreakdown` rows, alphabetically sorted.
   - Bad entry (no `id` field, no `cost_rate_usd_per_hr`) → silently skipped; rest of snapshot honest.
   - HEARTBEAT_SUBSTRATE_MISSING contributes to burn but does NOT contribute to `heartbeat_partial_truth` (that's CLI-computed from `provider_heartbeat_supported`, not entry-side).
   - `spend_usd_total` math: 1h-up entry at $0.79/hr → exactly $0.79.
   - `now` parameter respected (test passes `datetime(2026, 6, 12, 14, 0, 0)` and asserts snapshot `as_of`).

3. `tests/providers/test_runpod_balance.py`
   - Happy path: `http_post_json` returns `{"data": {"myself": {"clientBalance": 42.18}}}` → `ProviderBalance(usd=42.18, ...)`.
   - Transport failure: `http_post_json` raises `TransportError` → re-raised.
   - Schema drift: missing `data` / missing `myself` / missing `clientBalance` / non-numeric value → all raise `TransportError`.
   - Missing credential: `api_key=None` → `read()` returns None without making any HTTP call (spy assertion: `http_post_json.call_count == 0`).
   - Negative balance: `clientBalance: -3.50` → returns `ProviderBalance(usd=-3.50, ...)` verbatim.

4. `tests/cli/test_balance_cache.py`
   - Cache-miss path: fresh fetch → write to store → read back matches.
   - Cache-hit path (within TTL): no HTTP call (spy).
   - Cache-stale path (beyond TTL): HTTP call made; cache updated.
   - Stale-fallback: cached entry exists; fresh fetch raises TransportError → cached value returned + error message populated.
   - `--no-cache`: HTTP call made unconditionally; no write to store.
   - Parametrize across `LocalArtifactStore` + `FakeMockedS3Client` (Phase 38 precedent).

5. `tests/cli/test_cmd_cost.py`
   - Golden-output table render for a fixture snapshot (mirrors `_cmd_status` from Phase 33). Captures: top-line burn, per-provider rows, per-verdict columns, balance row, runway computation.
   - `--json` shape lock: assert exact key set + types match §10 schema.
   - `--prom` exposition lock: assert all 5 gauges + 1 counter emit; assert HELP+TYPE lines present; assert all 8 Verdict values appear in `kinoforge_pod_count` series; assert UTF-8 + LF line endings.
   - `--json --prom` together → argparse error with exit code 2.
   - Failure-mode parametrize per §12 table: one case per row, asserting render does NOT raise.
   - Throttle warning stub: env-var set, no Replicate entries → footer "replicate spend tracking pending B10" printed; `throttle_warnings: []` in JSON.
   - `live_pod_ids` fallback: provider whose `list_instances()` raises → STALE_LEDGER suppressed for that provider; WARNING logged.

6. `tests/test_examples.py` (existing) grows one case
   - `examples/configs/cost.yaml` loads via `load_config` without error.

All `http_post_json` interactions go through injection seams; no real RunPod calls. Operator captures a one-time GraphQL fixture via `curl` at spec-time:

```
curl -sS -X POST https://api.runpod.io/graphql \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ myself { clientBalance } }"}'
```

→ snapshot committed as `tests/providers/fixtures/runpod_balance_response.json`. No further live calls during the layer.

---

## 15. Task envelope

Target 7-10 per locked spec entry. Tracking 8.

```
t1. core/balance_endpoints.py
    - Protocol + ProviderBalance + TransportError + NoBalanceEndpoint
      + provider_balance_supported helper + offline tests (Test 1 above).
    - RED-first. Atomic commit.

t2. providers/runpod/balance.py
    - GraphQL satisfier + injectable http_post_json + offline tests (Test 3 above).
    - Operator captures fixture before this task.
    - RED-first. Atomic commit.

t3. cli/_adapters.py: build_balance_endpoint_for(cfg, creds)
    - Lazy-import RunPod satisfier; fall through to NoBalanceEndpoint.
    - Integration test: every known (provider, engine) tuple resolves to satisfier-or-No.
    - RED-first. Atomic commit.

t4. core/cost.py: aggregate + CostSnapshot + ProviderBreakdown
    - Pure aggregator + offline unit tests with fixture ledgers (Test 2 above).
    - RED-first. Atomic commit.

t5. CLI _cmd_cost + cost subparser
    - Three output modes (human, --json, --prom) + mutual-exclusion + golden tests (Test 5 above).
    - RED-first. Atomic commit.

t6. Balance disk cache: cached_balance_read + flags
    - --cache-ttl=N + --no-cache + stale-fallback + tests (Test 4 above).
    - RED-first. Atomic commit.

t7. Prom exposition assembly + scrape-error counter
    - heartbeat_partial_truth gate via B5a provider_heartbeat_supported.
    - format-lock test (Test 5 --prom case).
    - RED-first. Atomic commit.

t8. examples/configs/cost.yaml + README Cost dashboard section
    + PROGRESS / warm-reuse-tasks.txt closeout (strike B2 with commit sha;
    flip "design APPROVED" to "CLOSED commit <sha>"; amend Layer V spec §6).
    - Atomic commit.
```

All RED-first per CLAUDE.md TDD workflow. Live spend: **$0.** Per-task PR shape mirrors B4 / B7 (atomic commit per task; whole-layer merge via `git merge --no-ff` once t8 closes).

---

## 16. Forward consumers + closure hooks

- **B1 (sweeper)** does NOT consume `CostSnapshot`; pre-dates it in sequencing but operates on per-instance verdicts, not aggregates.
- **B3 (warm-reuse retrofit)** does NOT consume `CostSnapshot`; orchestrator hot path stays free of cost-aggregator imports.
- **Future Grafana dashboard** reads `--prom` via the textfile-collector cron pattern documented in README §Cost dashboard.
- **B10 (per-prediction hosted spend capture)** lights the Replicate throttle path green. B2 ships the gate RED so B10's diff is small: wire hosted artifacts into the ledger (or into a sidecar) without touching `_cmd_cost`.
- **B5b (SkyPilot heartbeat satisfier)** flipping `provider_heartbeat_supported` removes SkyPilot from `heartbeat_partial_truth` without touching any B2 code.
- **Future Bedrock / Vertex / Azure balance satisfiers** plug into `build_balance_endpoint_for` per the dispatch shape in §5. Adding a satisfier is one diff: new `_SUPPORTED` entry + new module + one branch.

---

## 17. Risk register

| # | Risk | Mitigation |
|---|---|---|
| 1 | RunPod GraphQL `clientBalance` shape drift | Isolated from `_LIST_PODS_QUERY`; satisfier raises `TransportError`; balance column degrades to `?` while burn / runway compute fine from ledger. Tested via injectable seam + snapshot fixture. |
| 2 | Per-prediction hosted spend not in ledger | Explicit `hosted_spend_pending: true` key in `--json`; `replicate spend tracking pending B10` footer in human table; documented in §11 + §15 (B10 closure hook). |
| 3 | `watch -n 1 kinoforge cost` rate-limit foot-gun | Disk cache TTL caps balance reads at 4/min worst case (15s default); README documents `--cache-ttl` override. |
| 4 | `list_instances()` per provider per invocation adds round-trips | Lazy-only for providers present in ledger; fallback to "assume every ledger entry up" when call fails; documented in §7. Cost is comparable to existing `kinoforge list`. |
| 5 | Cache write IO race with concurrent invocations | `cfg.store.put_json` writes are atomic on `LocalArtifactStore` (C1 pending root-cause; test-side bandage at `6b9fba3` covers concurrent reads). Cloud stores atomic by construction. Cache invariant tolerates one writer winning. |
| 6 | Future provider name collides with existing label values | Prom convention: provider names match `cfg.compute.provider` exactly. Today's set: `local`, `runpod`, `skypilot`. Future-name collision is a YAML-side rename, never a B2 concern. |

---

## 18. Live spend

Zero. RunPod GraphQL `myself` query authenticates only; no active pod required. Operator captures one fixture via `curl` at spec-time; tests use injected `http_post_json` spies throughout. No `pixi run preflight` call needed for the layer.
