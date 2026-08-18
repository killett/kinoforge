# Lifecycle, reaping, and the sweeper daemon

(Moved from README §Operator commands (status, Heartbeat persistence Layer U), §Reaping orphan pods (incl. forget), §Sweeper daemon (B1 / Layer W), §Interrupting a generation, §Configurable ComfyUI poll timeout on 2026-06-27. See [../README.md](../README.md).)

## Operator commands

### `kinoforge status --id <id>` — introspect one instance

`kinoforge status` reads the local ledger first and dispatches to the
provider recorded for that instance. The output is an alphabetised block
of `key=value` lines covering ledger-side facts (age, accrued spend,
lifecycle policy) plus live `provider_status` and `endpoints` from the
provider.

```
$ kinoforge status --id ia66l3rlto5x66
accrued_spend_usd=0.8400
age_h=2.4
cost_rate_usd_per_hr=0.3500
created_at=2026-06-05T14:23:11-07:00
endpoints={"http": "https://abc.proxy.runpod.net"}
id=ia66l3rlto5x66
idle_timeout_s=900
max_age_s=14400
provider=runpod
provider_status=ready
```

When the provider has no record of the id (stale ledger), `status`
exits 0 and appends an advisory:

```
provider_status=unknown (stale ledger — provider has no record)
advisory: ledger entry is stale — run 'kinoforge forget --id ia66l3rlto5x66'
```

Transient provider failures (network outage, SDK 5xx) exit 2.

Pass `--config PATH` (or `-c PATH`) to fill missing lifecycle fields on
legacy entries written before Layer S.

When the entry carries a `last_heartbeat` field, `status` also surfaces
it as an ISO timestamp. The writer is the Layer U `HeartbeatLoop` — see
*Heartbeat persistence* below for how to enable it.

### Heartbeat persistence (Layer U)

Set `lifecycle.heartbeat_interval_s` in your YAML to enable background
heartbeat writes from `kinoforge generate` / `kinoforge batch`:

```yaml
lifecycle:
  budget: 25.0
  heartbeat_interval_s: 30   # seconds; null (the default) disables
```

While a `deploy_session` is open, a daemon thread calls
`provider.heartbeat(id)` and persists the timestamp to the ledger as
`last_heartbeat`. A later `kinoforge status --id <id>` from any
process — even on a different machine when the ledger is on S3 or GCS
— shows "last seen N seconds ago".

**Operator guidance:** values < 10 risk ledger lock contention at
scale. The recommended starting point is 30s; tune up if your fleet
size makes the per-tick lock acquisition visible in `kinoforge gc`
timings.

**Crash-safety contract.** Every successful tick writes a sentinel
`heartbeat_thread_tick` alongside `last_heartbeat`. If the loop ever
dies silently (logged via `kinoforge.core.heartbeat_loop` at ERROR),
`kinoforge status` emits an advisory after `3 * heartbeat_interval_s`:

```
advisory: heartbeat thread stale (90s since last tick)
```

Any future code that consults `last_heartbeat` for a destructive
decision (e.g. a heartbeat-aware reaper) **MUST** check sentinel
freshness first — otherwise a crashed loop would look indistinguishable
from a healthy quiet session and the reaper would destroy live pods.
See `Ledger.touch`'s docstring for the formal contract.

## Reaping orphan pods

`kinoforge reap` classifies every ledger entry and (optionally)
destroys idle, over-age, or orphaned compute. Layer V is heartbeat-
aware: an entry whose Layer U `heartbeat_thread_tick` sentinel is
fresh is treated as live; a stale sentinel + past-grace pod becomes
an `ORPHAN_REAP` candidate.

### Dry-run (default)

```bash
kinoforge reap -c config.yaml
```

Prints a verdict table; no destructive action. Pass `--apply` to act.

### Acting on the default policy

```bash
kinoforge reap -c config.yaml --apply
```

Default policy destroys `IDLE_REAP` + `OVERAGE_REAP` and forgets
`STALE_LEDGER` entries. `ORPHAN_REAP` requires explicit opt-in:

```bash
kinoforge reap -c config.yaml --apply --include-orphans
```

### Other flags

| Flag | Effect |
|---|---|
| `--force-forget` | Adds UNROUTABLE → ledger.forget under --apply |
| `--strict` | Exit code 3 if any UNROUTABLE / HEARTBEAT_UNKNOWN present |
| `--id <X>` | Restrict to one ledger entry |
| `--format json` | JSONL output, one record per snapshot entry + per action |

### Exit codes

- 0 — normal (dry-run or --apply with no failures)
- 2 — at least one teardown failed under --apply
- 3 — `--strict` tripped
- 4 — invalid flag combo (e.g. `--include-orphans` without `--apply`)

### Sentinel-gate contract (Layer U → V)

The reaper trusts `last_heartbeat` only when the
`heartbeat_thread_tick` sentinel is fresh (within
`3 × heartbeat_interval_s`). Stale-sentinel + pod-up past
`grace_after_session_s` triggers `ORPHAN_REAP`. The grace window
(default 5 min) is operator-configurable via
`lifecycle.grace_after_session_s` in YAML or per-entry override.

### Verdict-only inspection

`kinoforge status --id <X>` surfaces the same `verdict=<...>` line
the reaper would compute for that entry — a "what would reap do
to this pod" view without invoking reap.

### `kinoforge forget --id <id>` — clear a stale ledger entry

Removes a single entry from the local ledger without touching the
upstream provider. Use when `kinoforge status` reports
`provider_status=unknown (stale ledger ...)`. Pairs naturally with
`kinoforge gc` for sweep-style cleanup. Non-idempotent by design: a
second `forget` on the same id (after the first removes it) exits 1.

```
$ kinoforge forget --id ia66l3rlto5x66
forgot: ia66l3rlto5x66
```

## Provider capability matrix

Every guardrail kinoforge can offer — an idle autostop, an on-instance
deadline watchdog, a per-job timeout, a real heartbeat read, a util
snapshot — is only as real as the provider that claims to enforce it.
`kinoforge.core.capabilities.Capability` is the vocabulary; each provider's
`capabilities(shape)` classmethod declares, per `WorkloadShape`
(`SERVER` — a long-lived process, autostop can never fire; `BATCH` — the
provision script exits, autostop can fire), which of these it actually
implements. A guardrail a cfg asserts that the selected provider cannot
enforce, and that no declared capability substitutes for, is either an
ERROR (spend-risk guardrail, nothing bounds it — load is refused) or a
WARN (naming the substitute and the numeric bound it actually enforces)
at config load and again at launch. See
`src/kinoforge/validation/checks/capabilities.py` and design doc
`docs/superpowers/specs/2026-08-16-provider-capability-declaration-design.md`
§6–§7.

| | local | runpod | skypilot | modal |
|---|---|---|---|---|
| `HEARTBEAT_READ` | ✓ | ✓ | ✗ | ✗ |
| `RUNTIME_PROBE` | ✗ | ✓ | ✗ | ✓ |
| `UTIL_SNAPSHOT` | ✓ (scripted, in-process test seam — not a measurement) | ✓ | ✗ | ✓ |
| `IDLE_AUTOSTOP` | ✗ | ✗ | ✓ **BATCH shape only** | ✓ |
| `ON_INSTANCE_DEADLINE` | ✗ | ✓ | ✓ | ✓ |
| `JOB_TIMEOUT` | ✗ | ✓ | ✗ | ✗ |
| `PAUSE_BILLING` | ✓ | ✓ | ✗ | ✗ |
| `BALANCE_QUERY` | ✗ | ✓ | ✗ | ✗ |
| `billed` | `False` | `True` | `True` | `True` |

Four cells whose caveat matters more than the checkmark:

- **`local` `UTIL_SNAPSHOT`** is a scripted in-process test seam, not a
  real measurement. It stays declared because the endpoint does return
  snapshots and `local` is unbilled, so no money decision rides on it.
- **`runpod` has no `IDLE_AUTOSTOP`.** `selfterm.py` is a boot-relative
  money cap, not idle detection — RunPod idle reaping is controller-side
  only, which does not count as provider-enforced coverage.
- **`skypilot` `IDLE_AUTOSTOP` holds only at `BATCH`.** A `SERVER`-shape
  deploy's `run_cmd` is a never-terminating `Task.run`, so
  `is_cluster_idle()` is permanently `False` and autostop cannot fire —
  see `providers/skypilot/watchdog.py` for the guardrail that actually
  holds on that shape (`ON_INSTANCE_DEADLINE`).
- **No provider's `ON_INSTANCE_DEADLINE` is simply `max_lifetime`.** All
  three declarations are honest — something on the instance really does
  terminate it — but each computes the deadline differently, and the
  capability WARN prints the real one per provider:
  - `runpod` — `min(2 * idle_timeout, max_lifetime - time_buffer)`, two
    independent boot-relative caps (`providers/runpod/selfterm.py`).
    Note the second term: with `max_lifetime <= time_buffer` the pod's own
    deadline is already past at boot, so selfterm reaps immediately.
  - `skypilot` — the **earlier** of `max_lifetime` and a budget bound
    (`budget_usd / rate_usd_per_hr`, `watchdog.compute_deadline`). The
    offer's rate is unknowable at load, so `max_lifetime` is reported as a
    ceiling ("at most"), not as the enforced deadline.
  - `modal` — derived from `boot_timeout`; `max_lifetime` is never sent to
    Modal at all. A modal cfg that writes `max_lifetime` therefore gets an
    extra `WARN` at load naming both numbers, so an operator who asked for
    90 m and is capped at 45 m learns it before the run rather than after.

A guardrail with no declared capability on the selected provider **and**
no declared substitute refuses the config load outright (`ERROR`,
spend-risk rows only); everything else downgrades to a `WARN` naming what
actually bounds the run instead. `kinoforge doctor` and config load both
run this check; nothing here changes what a provider is actually capable
of — it only stops the config from claiming a capability the provider does
not have.

## Sweeper daemon (B1 / Layer W)

The sweeper is a long-running foreground daemon that calls the same
`sweep()` substrate as `kinoforge reap` on a configurable cadence
(default 60s). It closes the idle-pod cost-leak window between manual
operator sweeps.

Subcommands:

```
kinoforge sweeper start    # foreground; blocks until SIGTERM
kinoforge sweeper stop     # SIGTERM the daemon owning sweeper:<host>
kinoforge sweeper status   # human or --json output
kinoforge sweeper metrics  # --prom textfile-collector target
```

YAML block (additive to existing config; defaults are safe):

```yaml
sweeper:
  interval_s: 60
  include_orphans: false   # extend default policy with ORPHAN_REAP
  force_forget: false      # extend default policy with UNROUTABLE
  host: null               # null → socket.gethostname()
```

Operator postures:

- **systemd**: `Type=simple` + `Restart=on-failure`. `ExecStart=/usr/local/bin/kinoforge sweeper start -c /etc/kinoforge.yaml`.
- **docker**: Run as PID 1; the daemon handles SIGTERM cleanly.
- **textfile-collector cron**:
  ```
  */30 * * * * kinoforge sweeper metrics --prom -c /etc/kinoforge.yaml \
                 > /var/lib/node_exporter/textfile/kinoforge_sweeper.prom
  ```

Signals:

- `SIGTERM` → drain the in-flight sweep then exit 0.
- `SIGHUP` → re-read the config file and swap policy / thresholds /
  interval without restarting the thread.
- `SIGUSR1` → log cumulative stats to stdout.

The daemon's own liveness lives in a reserved synthetic ledger entry
keyed `sweeper:<host>`. `sweep()` filters this prefix so the daemon
cannot reap itself. Use `kinoforge sweeper status --json` to read the
entry programmatically.

## Interrupting a generation

Press `Ctrl-C` once during `kinoforge generate` to trigger a graceful
drain. The orchestrator stops issuing new poll requests, in-flight
backend calls unwind cooperatively, and the CLI prints a WARN line
naming the surviving pod ID and the recovery command:

```
WARN orchestrator interrupt received; finishing in-flight work + draining pool. Press Ctrl-C again to force-exit.
WARN orchestrator KeyboardInterrupt during stages; pod abc123 kept alive (selfterm/reap path). Run `kinoforge reap` to destroy now.
```

The pod is **not** destroyed on interrupt — warm-reuse intent applies
in both `--ephemeral` and non-ephemeral modes (matches the Layer 5b
session-manager contract in `3bc6473`). The in-pod self-terminator
(idle-timeout / max-lifetime) kills the pod eventually, and
`kinoforge reap` destroys it immediately. Run `kinoforge reap` if you
don't want to wait.

Press `Ctrl-C` a second time to force-exit immediately. The default
SIGINT handler is restored on the second press, so a third `Ctrl-C`
would terminate the process the usual way.

### Configurable ComfyUI poll timeout

`ComfyUIBackend.result` has a hard upper bound on a single poll wait
to surface silent stalls quickly:

```yaml
engine:
  kind: comfyui
  comfyui:
    poll_interval_s: 2.0
    poll_timeout_s: 600.0    # raise for known-slow models (Wan 14B t2v ~6 min)
```

Default is `600.0` s (10 min). When the timeout fires, `TimeoutError`
is raised with the last observed `status` and `current_node` baked into
the message — enough state to diagnose a stall without re-running the
smoke. Each poll tick also emits a structured INFO line
(`comfyui poll job=… elapsed=…s status=… queue_pos=… exec_node=…`)
so a hang shows you exactly which node is blocking before the timeout
fires.

## Sweeper-side ephemeral pod reap (2026-06-28)

The sweeper daemon now reaps **ephemeral pods** — pods registered in
`ephemeral-index.json` (Layer Warm-Reuse) rather than `ledger.json`.
Without this, an ephemeral pod whose selfterm watchdog crashed would
bleed cost until the operator manually destroyed it.

### How it works

Each `SweeperLoop._tick_once()`:

1. Reads `EphemeralIndex.rows()` and unions them with `ledger.entries()`.
   Ledger wins on overlap (it has the full heartbeat substrate).
2. For each ephemeral-only row, calls `provider.probe_runtime(pod_id)`
   with a per-tick cache so two rows for the same provider share one
   call. Provider's `probe_runtime` is the new ABC method
   (default returns `None`); `RunPodProvider` overrides it via the
   existing C26 GraphQL substrate, and `ModalProvider` via app-list
   existence + the M5 `GET /util` route. For providers exposing
   `note_endpoints` (Modal), `_probe_with_cache` first primes the
   provider's URL cache from the index row's persisted endpoints —
   Modal `.modal.run` URLs are not rebuildable cross-process.
3. Synthesises a ledger-shape entry flagged `kinoforge_ephemeral=True`
   with a `probe_state` of `ok` / `not_found` / `no_substrate` /
   `failed` and dispatches `classify()`.
4. `_classify_ephemeral` runs a heartbeat-free decision tree (spec
   §3.5): probe_state dispatch first, then OVERAGE, then STALL if a
   cross-tick `stall_history` deque is present, else LIVE.

### Verdict matrix (ephemeral branch)

| probe_state    | Verdict          | Default action       |
|----------------|------------------|----------------------|
| `not_found`    | `GC_404`         | remove index row     |
| `no_substrate` | `SKIP_NO_PROBE`  | WARN-once, no-op     |
| `failed`       | `PROBE_FAILED`   | WARN-once, no-op     |
| `ok` + overage | `OVERAGE_REAP`   | destroy + remove row |
| `ok` + stall   | `STALL_REAP`     | destroy + remove row |
| `ok` + live    | `LIVE`           | no-op                |

`STALL_REAP` fires only when `SweeperLoop` has accumulated N
consecutive zero-util samples (N = `ceil(stall_window_s /
heartbeat_interval_s)`). The one-shot `kinoforge reap` CLI passes
`stall_history=None`, so `STALL_REAP` is impossible from a single
sweep — a single 0% sample during a model-load is not enough evidence.

### What's deliberately NOT in apply policy

`SKIP_NO_PROBE` and `PROBE_FAILED` are logged inline in `sweep()`
(WARN-once, dedup keyed on `(provider, pod_id, error_class)`) and
deliberately omitted from `DEFAULT_APPLY_POLICY` — they describe
infrastructure conditions, not pod state, and operators cannot fix
them by destroying the pod.

`IDLE_REAP` from the ephemeral branch is also impossible: model-load
periods (Wan 14B weight fetch, 4–8 min at 0% GPU) would otherwise
trip a false positive. Idle is the wrong predicate for ephemeral
pods; stall (cross-tick sustained zero util) is the right one.
