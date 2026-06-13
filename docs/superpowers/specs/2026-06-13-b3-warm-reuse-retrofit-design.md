# B3 — In-session orchestrator warm-reuse retrofit — Design

**Status:** approved at brainstorm (2026-06-13).
**Prereqs:** B5a heartbeat substrate (CLOSED, commit `bade08c`); B7 cooperative session-claim lock (CLOSED, commit `8f1ee89`); B4 cross-CLI warm-reuse CLI exposure (CLOSED, commit `54d2867`); B1 sweeper daemon (CLOSED, commit `cbe5337`).
**Unblocks:** none directly; composes with B2 (cost dashboard) for LIVE-busy vs LIVE-idle split.
**Cross-references:**
- `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md` §1.1, §3.4, §11 — `hold_until_first_tick` + `provision:<id>` lock reused unchanged.
- `docs/superpowers/specs/2026-06-12-b4-cross-cli-warm-reuse-design.md` §3.3, §11 — `_resolve_warm_instance` helper factored with B3's scan.
- `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §3.3, §5.6, §6 — `classify` Verdict tree consumed; B3 closes the §6 Layer Y candidate.
- `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §7, §13 — `provider_heartbeat_supported` gate inherited.
- `warm-reuse-tasks.txt:532-566` — B3 brief.
- `PROGRESS.md §85-110` — established patterns.

---

## 1. Goal & scope

Surface auto-warm-reuse at `kinoforge generate` / `kinoforge batch`. Every fresh-shell invocation walks the ledger for non-busy LIVE pods matching the current `capability_key`; on hit, attaches via the existing Layer P `instance=` kwarg seam; on miss, cold-creates as today. Kills the 1–5 min ComfyUI + Wan cold-spin-up on the second-through-Nth invocation in any operator session.

### 1.1 Scope guardrails

- Auto-discovery only. Operator-friendly default-on; per-config opt-out via `compute.warm_reuse_auto_attach`; per-invocation override via `--no-reuse` (cold + ephemeral).
- Zero new substrate modules. Reuses B7's `hold_until_first_tick`, B4's `_resolve_warm_instance`, B1's `reaper:<id>` lock semantics.
- Cross-CLI session-busy ledger fields (`session_start`, `session_end`) added — closes the "is another CLI mid-generate on this pod?" gap that B7 + B4 deferred. Pure additive via existing `Ledger.touch(**extra)` seam.
- `--no-reuse` ships ephemeral-pod semantics: cold create + immediate destroy at `deploy_session.__exit__` under `reaper:<id>` lock.
- Live spend: ~$2.50 RunPod (single attach-to-warm round-trip smoke; two generations).

### 1.2 Non-goals

- B5b SkyPilot satisfier — gated on A3/A4 GPU quota; consumers already hard-pin no-attach on SkyPilot via `HEARTBEAT_SUBSTRATE_MISSING`.
- B16 distributed cross-host lock semantics on cloud stores.
- B6 per-entry `heartbeat_interval_s` override.
- Engine-specific warm-reuse veto hook (engine.refuses_warm_attach).
- B2 cost-dashboard consumption of `session_start`/`session_end` (substrate ships here; consumer follows separately).
- `compute.destroy_on_exit: bool` YAML knob (YAGNI; `--no-reuse` covers v1 use case).
- `kinoforge list --verbose` adding busy-state column.

---

## 2. Decisions locked at brainstorm

| # | Decision | Choice | Reason |
|---|---|---|---|
| D1 | Candidate-selection ordering when ≥2 LIVE matches | Cross-CLI session-busy ledger fields filter busy entries first; non-busy tiebreaker = newest `heartbeat_thread_tick`. New `is_session_busy` pure helper. | Encodes truthful "this pod is being actively used by another CLI right now" instead of guessing from `created_at` or heartbeat freshness alone. Stale-busy auto-clears via existing Layer V `3 * heartbeat_interval_s` sentinel window — no separate timeout knob. Bonus: B2 cost dashboard inherits LIVE-busy vs LIVE-idle split. |
| D2 | Cross-CLI attach lock semantics | Pre-orchestrator non-blocking probe of B7's existing `provision:<id>` key during candidate validation. On held → skip + re-scan. Zero new lock key. | Reuses B7's lock taxonomy. Avoids fcntl per-fd flock issue B7 R2 documented. B7's blocking-acquire inside `deploy_session.__enter__` stays as backstop for genuine same-microsecond races. |
| D3 | `HEARTBEAT_UNKNOWN` on RunPod handling | Non-attachable. B3 auto-discovery only attaches to verdict=LIVE. Skip + WARN-once-per-session naming pod id + remediation. | Conservative-on-ignorance per warm-reuse-tasks.txt:10-15 interval contract. `HEARTBEAT_SUBSTRATE_MISSING` (SkyPilot pre-B5b) similarly non-attachable via `provider_heartbeat_supported` gate. Operator-at-keyboard `--force-attach` override remains via B4 only. |
| D4 | Mismatched-engine-state on attached pod | Trust marker + idempotent `engine.provision`. Loud failure at first backend.submit HTTP call on drift. No pre-probe ABC method. No verify-on-attach. | Reuses warm-supplied semantics unchanged (Layer P Q4 "loud failure not silent"). Persistent drift remedy: `kinoforge reap` or `kinoforge destroy --id <X>`. Avoids ~50ms per-attach round-trip on the happy path (99% of attaches). |
| D5 | B1-sweeper-vs-B3-attach race | B3 candidate-validation probes `reaper:<id>` non-blocking BEFORE `provision:<id>` probe. Mirrors B1's acquire order (reaper before provision). | Symmetric ordering → no AB-BA deadlock. Closes most of the race window; residual microsecond TOCTOU slips into loud-fail-at-first-HTTP per D4. No new substrate. |
| D6 | Validate-fail logging | Silent skip per candidate (DEBUG-level reason code). Single INFO summary at end: attached vs cold-create with reason counts. Never fail-hard. | Auto-discovery is opportunistic; cold create is the fall-through. Reason codes machine-parseable for future B2 dashboard ingestion. Quiet happy path; observable when chronic warm-reuse failure surfaces. |
| D7 | CLI surface | `--no-reuse` flag on generate/batch + `compute.warm_reuse_auto_attach: bool = True` YAML knob. `--no-reuse` forces cold create AND triggers immediate destroy + `ledger.forget` at `deploy_session.__exit__` under `reaper:<id>` lock. Mutex with `--force-attach`; composes with `--instance-id`. | Operator-friendly default-on; explicit opt-out via flag (one-shot job, benchmarking, drift recovery) or YAML (per-project policy). Ephemeral semantics carve clean axis (attach vs destroy) for future extension. `--no-reuse` in batch = destroy after whole batch, not per-row. |

---

## 3. Architecture

### 3.1 Module map

**Edits only. Two new helpers; zero new modules.**

| File | Change | LOC |
|---|---|---|
| `src/kinoforge/cli/_commands.py` | New `_scan_warm_candidates(ctx, cfg, *, clock)` helper sibling of B4's `_resolve_warm_instance`. New `_probe_lock_held(store, key)` non-blocking lock probe. New `_ScanReport` dataclass. Thread `instance=` (from scan or `--instance-id`) + `single` into `_cmd_generate` + `_cmd_batch` dispatchers. | ~80 |
| `src/kinoforge/cli/_main.py` | Add `--no-reuse` flag to `p_generate` and `p_batch`. Mutex with `--force-attach`; composes with `--instance-id`. | ~10 |
| `src/kinoforge/core/orchestrator.py` | `deploy_session` grows `single: bool = False` kwarg. `__enter__` writes `session_start` via `Ledger.touch` after `hb_loop.start()` returns. `__exit__` writes `session_end`; when `single=True` AND `instance is not None`, acquires `reaper:<id>` and calls `destroy_confirmed` + `ledger.forget`. Thread `single` through `generate` + `batch_generate`. | ~50 |
| `src/kinoforge/core/lifecycle.py` | New module-level pure helper `is_session_busy(entry, *, now, heartbeat_interval_s) -> bool`. `Ledger.touch` already accepts `**extra`; new fields pass through. | ~15 |
| `src/kinoforge/core/config.py` | `ComputeConfig.warm_reuse_auto_attach: bool = True`. | ~5 |

**Test additions:**

| File | Cases | LOC |
|---|---|---|
| `tests/cli/test_scan_warm_candidates.py` (new) | ~18 unit cases per §7.1 | ~150 |
| `tests/core/test_ledger_session_fields.py` (new) | ~12 unit cases per §7.2 | ~110 |
| `tests/core/test_orchestrator_session_fields.py` (new) | ~6 unit cases per §7.3 | ~80 |
| `tests/core/test_orchestrator_no_reuse.py` (new) | ~9 unit cases per §7.4 | ~110 |
| `tests/cli/test_cmd_generate.py` (delta) | ~10 cases per §7.5 | ~90 |
| `tests/cli/test_cmd_batch.py` (delta) | ~4 cases per §7.6 | ~40 |
| `tests/core/test_b3_warm_attach_xprocess.py` (new) | ~5 subprocess cases per §7.7 | ~150 |
| `tests/core/test_reaper_actor.py` (delta) | 1 case per §7.8 | ~25 |
| `tests/live/test_b3_warm_attach_live.py` (new) | 1 RunPod smoke | ~50 |

**Docs:**

| File | Change | LOC |
|---|---|---|
| `README.md` | Operator warm-reuse section gets "Auto-discovery" subsection + `--no-reuse` docs. | ~25 |
| `PROGRESS.md` | Strike B3, replace with closeout pointer. | ~5 |
| `warm-reuse-tasks.txt` | Replace B3 entry (532-566) with closeout. | ~5 |
| `examples/configs/*.yaml` | Add `# warm_reuse_auto_attach: true  # default` comment. | ~5 |

**Totals:** ~160 LOC source, ~630 LOC tests, ~40 LOC docs.

### 3.2 Layering invariant

Orchestrator stays pure of CLI imports. Auto-discovery scan lives in `cli/_commands.py` (operator decision-context) and threads its result through the existing `instance=` kwarg. Orchestrator only grows the `single` kwarg + `session_start`/`session_end` ledger writes + the `__exit__` destroy branch.

```
┌──────────────────────────────────────────────────────────────────────┐
│  cli/_commands.py  (consumer)                                         │
│   _cmd_generate / _cmd_batch                                          │
│     ├─ explicit --instance-id?  → _resolve_warm_instance (B4)         │
│     ├─ --no-reuse?              → skip scan; single=True              │
│     ├─ cfg.warm_reuse_auto?     → _scan_warm_candidates  (NEW B3)     │
│     └─ default cold              → instance=None                      │
└──────────────────────────────────────────────────────────────────────┘
                       │ threads instance + single
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/orchestrator.py  deploy_session                                 │
│   ├─ __enter__:                                                       │
│   │    cold or warm provision → HeartbeatLoop.start                   │
│   │    Ledger.touch(session_start)                                    │
│   ├─ yield session                                                    │
│   └─ __exit__:                                                        │
│        Ledger.touch(session_end)                                      │
│        if single: acquire reaper:<id>;                                │
│                   destroy_confirmed + ledger.forget                   │
└──────────────────────────────────────────────────────────────────────┘
                       │ calls
                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  core/lifecycle.py  (substrate)                                       │
│    Ledger.touch(session_start=…, session_end=…)  — existing seam      │
│    is_session_busy(entry, *, now, hb_interval) -> bool   NEW pure     │
│    destroy_confirmed(provider, id, …)  — existing                     │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.3 Ledger field semantics

New fields written via existing `Ledger.touch(**extra)` seam — no schema migration; legacy entries (pre-B3) have both fields absent and are correctly classified as "not busy" by `is_session_busy`.

| Field | Type | Writer | Semantics |
|---|---|---|---|
| `session_start` | `float \| None` | `deploy_session.__enter__` post-`hb_loop.start()` | POSIX timestamp when this session claimed pod for in-flight work. |
| `session_end` | `float \| None` | `deploy_session.__exit__` post-`pool.close()` | POSIX timestamp when this session released pod. |

**Pure helper:**

```python
# src/kinoforge/core/lifecycle.py
def is_session_busy(
    entry: Mapping[str, Any],
    *,
    now: float,
    heartbeat_interval_s: float | None,
) -> bool:
    """Whether a ledger entry has an active in-flight session.

    Busy iff ``session_start`` is more recent than ``session_end`` (or
    ``session_end`` absent) AND the heartbeat sentinel is fresh per the
    Layer V ``3 * heartbeat_interval_s`` window. Stale-busy (writer
    process crashed) auto-clears via the sentinel-freshness gate.

    Args:
        entry: A ledger-shaped dict. May carry ``session_start``,
            ``session_end``, ``heartbeat_thread_tick``.
        now: Wall-clock seconds (operator-injected clock).
        heartbeat_interval_s: Cfg heartbeat cadence; ``None`` means HB
            feature disabled this invocation — fall back to trusting
            the marker (treat as busy).

    Returns:
        True iff entry should be skipped as a warm-attach candidate
        because another live session is claiming it.
    """
    s_start = entry.get("session_start")
    s_end = entry.get("session_end")
    if s_start is None:
        return False
    if s_end is not None and float(s_end) >= float(s_start):
        return False  # cleanly closed
    if heartbeat_interval_s is None:
        return True  # no HB → trust the marker
    tick = entry.get("heartbeat_thread_tick")
    if tick is None:
        return False  # claimant never started ticking; treat as crashed
    sentinel_window = 3.0 * heartbeat_interval_s
    return (now - float(tick)) <= sentinel_window
```

**Write order rules:**
- `session_start` written AFTER `hb_loop.start()` returns. Guarantees: heartbeat substrate is alive → busy-derivation trusts the freshness gate.
- `session_end` written BEFORE the optional `--no-reuse` destroy. Preserves causal chain — concurrent classify never sees `STALE_LEDGER` for an entry still flagged busy.
- `Ledger.touch` filters `_PROTECTED_LEDGER_KEYS` already (`{id, provider, tags, created_at, cost_rate_usd_per_hr}`); `session_start`/`session_end` not in that set, pass through.

**Stale-session correctness:** if two `deploy_session` contexts ever ran simultaneously against the same `instance.id` (B7 blocking-acquire serializes; theoretical), the later `session_start` overwrites earlier. Latest-writer-wins. `session_end` from a stale (earlier-completed) session has timestamp < newer `session_start`, so the `s_end >= s_start` check correctly returns False (still busy).

### 3.4 `_scan_warm_candidates` algorithm

```python
# src/kinoforge/cli/_commands.py
def _scan_warm_candidates(
    ctx: SessionContext,
    cfg: Config,
    *,
    clock: Clock | None = None,
) -> tuple[Instance | None, _ScanReport]:
    """Auto-discover a warm pod for cfg's capability_key.

    Walks ledger for non-busy LIVE candidates matching cfg's provider
    + capability_key. Validates each via B4's cheap-first chain plus
    reaper:<id> + provision:<id> non-blocking probes. Returns
    (Instance, report) on first valid candidate; (None, report) when
    all candidates exhausted or none exist.
    """
```

Algorithm:

```text
1. entries = ctx.ledger().entries()
2. now = (clock or RealClock()).now()
3. hb_interval = cfg.lifecycle().heartbeat_interval_s
4. cap_key = cfg.capability_key().derive()[:12]
5. provider_kind = cfg.compute.provider

6. # Coarse-filter pure-ledger pass (no I/O):
   matches = [
     e for e in entries
     if e.get("provider") == provider_kind
     and e.get("tags", {}).get("kinoforge_key") == cap_key
     and not is_session_busy(e, now=now, heartbeat_interval_s=hb_interval)
   ]

7. # Sort by newest heartbeat_thread_tick (D1 tiebreaker among non-busy):
   matches.sort(key=lambda e: e.get("heartbeat_thread_tick", 0.0), reverse=True)

8. # Per-candidate validation:
   skipped: list[tuple[str, str]] = []
   for entry in matches:
       instance_id = entry["id"]

       # D5 reaper probe (cheap, fcntl flock):
       if _probe_lock_held(ctx.store, f"reaper/{instance_id}"):
           skipped.append((instance_id, "reaper-held")); continue

       # D2 provision probe (cheap, fcntl flock):
       if _probe_lock_held(ctx.store, f"provision/{instance_id}"):
           skipped.append((instance_id, "provision-held")); continue

       # B4 cheap-first chain (force_attach=False -> D3 verdict gate strict):
       instance, rc = _resolve_warm_instance(
           ctx, cfg, instance_id, force_attach=False, clock=clock,
       )
       if rc is not None:
           skipped.append((instance_id, _rc_to_reason(rc, entry))); continue

       return (instance, _ScanReport(attached=instance_id, skipped=skipped))

9. return (None, _ScanReport(attached=None, skipped=skipped))
```

**`_probe_lock_held`:**

```python
def _probe_lock_held(store: ArtifactStore, key: str) -> bool:
    """Non-blocking probe-only: is `key` currently held by another process?

    Mirrors B7 reaper-side probe pattern at core/reaper_actor.py:193.
    ttl_s=0.0 reflects 'not claiming this lock for any duration.'
    """
    try:
        lock = store.acquire_lock(key, ttl_s=0.0)
        token = lock.acquire(blocking=False)
    except LockTimeout:
        return True
    if token is None:
        return True
    lock.release(token)
    return False
```

**Reason-code vocabulary (fixed set for B2 ingestion):**

| Code | Trigger |
|---|---|
| `reaper-held` | D5 reaper probe positive |
| `provision-held` | D2 provision probe positive |
| `cap-key-drift` | B4 step 3 (defensive; coarse-filter prevents but mirrored for safety) |
| `provider-mismatch` | B4 step 2 |
| `provider-unconstructable` | B4 step 4 |
| `list-instances-failed` | B4 step 5 RPC raised |
| `classify-not-live` | B4 step 5 verdict gate rejected (D3 HEARTBEAT_UNKNOWN, IDLE_REAP, ORPHAN_REAP, STALE_LEDGER, OVERAGE_REAP, UNROUTABLE, HEARTBEAT_SUBSTRATE_MISSING) |
| `get-instance-keyerror` | B4 step 6 TOCTOU |

**`_ScanReport`:**

```python
@dataclass(frozen=True)
class _ScanReport:
    attached: str | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def summarize(self) -> str:
        """Single-line INFO summary per D6.

        On hit:    "warm-reuse: attached to <id> (skipped N: K1 K2 ...)"
        On miss:   "warm-reuse: scanned N, 0 attachable (reasons: ...) — cold create"
        On empty:  "" (no candidates matched cap_key; silent cold create)
        """
```

### 3.5 `deploy_session` wire-in

**New kwarg:**

```python
@contextmanager
def deploy_session(
    cfg: Config,
    *,
    store: ArtifactStore,
    # ... existing kwargs ...
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
    heartbeat_loop_factory: ... = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,  # NEW (B3 — --no-reuse semantic)
) -> Iterator[DeploySession]:
```

**`session_start` write** (inside the `with claim_holder:` block, after `hb_loop.start()`):

```python
if hb_loop is not None and instance is not None:
    Ledger(store=store).touch(
        instance.id,
        session_start=time.time(),
    )
```

Per §3.3 race semantics: brief microsecond window where `session_start` precedes first `heartbeat_thread_tick`. `is_session_busy` returns False during this window (tick is None → not busy); both scans race to attach; B7 blocking-acquire on `provision:<id>` serializes. Already covered.

**`session_end` write + `--no-reuse` destroy at `__exit__`:**

```python
finally:
    if hb_loop is not None:
        hb_loop.stop()

    if cancel_token is not None and cancel_token.is_set():
        try:
            pool.close(cancel_pending=True, timeout=30.0)
        except Exception as close_exc:
            _log.error("pool.close failed during interrupt cleanup: %s", close_exc)
    else:
        pool.close()

    # B3 — write session_end before any destroy
    if instance is not None and resolved_provider is not None:
        try:
            Ledger(store=store).touch(instance.id, session_end=time.time())
        except Exception as touch_exc:
            _log.warning(
                "ledger.touch(session_end) failed for %s: %s",
                instance.id, touch_exc,
            )

    # B3 — --no-reuse destroy under reaper:<id> lock (D7 composition includes
    # caller-supplied instance: explicit --instance-id + --no-reuse destroys)
    if single and instance is not None and resolved_provider is not None:
        try:
            with store.acquire_lock(f"reaper/{instance.id}", ttl_s=30.0):
                destroy_confirmed(
                    resolved_provider, instance.id, sleep=time.sleep,
                )
                Ledger(store=store).forget(instance.id)
                _log.info("--no-reuse: destroyed + forgot pod %s", instance.id)
        except TeardownError as destroy_exc:
            _log.error(
                "--no-reuse destroy failed for %s: %s "
                "(use `kinoforge reap --apply` to recover)",
                instance.id, destroy_exc,
            )
        except Exception as destroy_exc:
            _log.error(
                "--no-reuse destroy raised unexpected for %s: %s",
                instance.id, destroy_exc,
            )
```

**Existing teardown branches unchanged:** `ValidationError` / `CapabilityMismatch` arms (orchestrator.py:847, 1278, 1369) keep their `not _caller_supplied_instance` guards — those preserve drift-protection semantics distinct from `--no-reuse`'s ephemeral-by-choice.

**Hosted-engine guard:** `instance is None or resolved_provider is None` → all B3 writes + destroy skipped. `--no-reuse` on hosted-engine path = no-op (with one-line INFO from `_cmd_generate` acknowledging it).

### 3.6 CLI surface

**Argparse** (`cli/_main.py`):

```python
# Added to both p_generate and p_batch:
p_generate.add_argument(
    "--no-reuse",
    action="store_true",
    dest="no_reuse",
    help=(
        "force cold create_instance (skip warm-reuse auto-discovery) AND "
        "destroy the pod immediately when generation finishes. "
        "Use for one-shot jobs, benchmarking cold-boot, or forcing a "
        "fresh pod after suspected engine-state drift. Mutex with "
        "--force-attach. Composes with --instance-id (attach to that "
        "pod, then destroy at end)."
    ),
)
```

**Dispatch mutex** (head of `_cmd_generate` and `_cmd_batch`):

```python
if args.no_reuse and args.force_attach:
    print(
        "error: --no-reuse and --force-attach are mutually exclusive "
        "(--no-reuse forces cold create; --force-attach bypasses "
        "verdicts for warm attach)",
        file=sys.stderr,
    )
    return 2
```

**Precedence chain** (highest wins, both `_cmd_generate` and `_cmd_batch`):

```python
single = bool(getattr(args, "no_reuse", False))
auto_attach_cfg = cfg.compute.warm_reuse_auto_attach if cfg.compute else False

instance: Instance | None = None
if getattr(args, "instance_id", None) is not None:
    instance, rc = _resolve_warm_instance(
        ctx, cfg, args.instance_id, force_attach=bool(args.force_attach),
    )
    if rc is not None:
        return rc
elif single:
    _log.info("--no-reuse: skipping warm-reuse scan; cold create + destroy on exit")
elif auto_attach_cfg:
    instance, report = _scan_warm_candidates(ctx, cfg)
    summary = report.summarize()
    if summary:
        _log.info(summary)

artifact, _ = _generate(
    cfg, request, ...,
    instance=instance,
    single=single,
)
```

**Config** (`core/config.py`):

```python
class ComputeConfig(BaseModel):
    provider: str
    image: str
    mode: str = "pod"
    requirements: RequirementsConfig = RequirementsConfig()
    lifecycle: LifecycleConfig | None = None
    heartbeat_mode: str = "none"
    warm_reuse_auto_attach: bool = True  # NEW (B3)
```

Default-on. Per-project opt-out:

```yaml
compute:
  provider: runpod
  image: runpod/comfyui:latest
  warm_reuse_auto_attach: false
```

Precedence (highest wins): `--instance-id` → `--no-reuse` → `warm_reuse_auto_attach=false` → default scan.

### 3.7 Lock interaction summary

Three lock keys touch B3's path. None new.

| Key | Holder | Purpose | B3 interaction |
|---|---|---|---|
| `ledger/<run_id>` | `Ledger.record`/`forget`/`touch` | RMW serialization | `session_start`/`session_end` writes use it. |
| `provision:<id>` | `deploy_session` via `hold_until_first_tick` (B7) | Mid-cold-boot serialization | B3 NON-BLOCKING probes during scan (D2). |
| `reaper/<id>` | `act_on_verdict` (B1) + B3 `--no-reuse` destroy | Per-instance reap serialization | B3 NON-BLOCKING probes during scan (D5); B3 BLOCKING acquires on `--no-reuse` exit. |

### 3.8 Acquire-order analysis

| Site | Order |
|---|---|
| B1 reaper `act_on_verdict` | `reaper:<id>` blocking → `provision:<id>` non-blocking probe → release probe → destroy → release reaper |
| B3 scan candidate-validate (per candidate) | `reaper:<id>` non-blocking probe → release → `provision:<id>` non-blocking probe → release |
| B3 `--no-reuse` `__exit__` destroy | `reaper:<id>` blocking → destroy → release reaper |
| `deploy_session.__enter__` (B7 cold or warm path) | `provision:<id>` blocking via `hold_until_first_tick` → release after first-tick polling phase at `__exit__` |

Strict order: `reaper:<id>` ALWAYS before `provision:<id>` at every site that takes both. No AB-BA cycle.

---

## 4. Failure modes

| # | Mode | Handling |
|---|---|---|
| F1 | Empty ledger (first-ever generate, all reaped) | `_scan_warm_candidates` returns `(None, _ScanReport(skipped=[]))`. `summarize()` returns empty string. Cold create silently. |
| F2 | Ledger has entries, none match cap_key | Coarse-filter step 6 yields empty matches. Same as F1. |
| F3 | Cap_key matches but provider mismatch | Coarse-filter step 6 drops them. Same as F1. |
| F4 | All cap_key matches are busy (other CLIs mid-generate) | `is_session_busy` filters them. Same as F1. |
| F5 | Cap_key matches all classify as non-LIVE | B4 verdict gate refuses each; `_resolve_warm_instance` returns rc=2 with reason `classify-not-live`. Skipped + counted. Cold create. |
| F6 | `reaper:<id>` held on first candidate (B1 mid-destroy) | D5 probe → skip + record `reaper-held`. Loop continues. |
| F7 | `provision:<id>` held on first candidate (another CLI mid-cold-boot) | D2 probe → skip + record `provision-held`. Loop continues. |
| F8 | Cap_key drift between coarse-filter and B4 step 3 | Defensive in-depth no-op; same field source. |
| F9 | `provider.list_instances()` raises | rc=2 with `list-instances-failed`. Loop exits; cold-create path also fails at `_provision_instance_and_build_backend` → same RPC raises. Loud. |
| F10 | TOCTOU: candidate destroyed by B1 between scan + B4 step 6 | rc=2 with `get-instance-keyerror`. Skip + continue. |
| F11 | TOCTOU: candidate destroyed by B1 between `_resolve_warm_instance` success + `deploy_session.__enter__` lock-acquire (~µs window) | `engine.provision` HTTP call fails. Per D4 loud failure. Operator re-runs; B3 picks different candidate or cold-creates. |
| F12 | `Ledger.touch(session_start=...)` write fails (cloud-store transient) | WARN logged; `yield session` proceeds. Worst case: another B3 scan briefly sees entry as "not busy" → both attach → B7 blocking-acquire serialises. |
| F13 | `Ledger.touch(session_end=...)` write fails at `__exit__` | WARN logged; pod survives. Next B3 scan sees `session_start` set + `session_end` absent + heartbeat ticking → busy. Stale-busy clears when HB stops and tick ages out. |
| F14 | `--no-reuse` destroy: `destroy_confirmed` raises `TeardownError` after retries | ERROR logged with pod id + `kinoforge reap --apply` recovery command. Ledger entry NOT forgotten — preserved for `reap`. RunPod-side selfterm dual safety still kills pod. |
| F15 | `--no-reuse` destroy: `reaper:<id>` lock held by B1 sweeper | Blocking acquire on ttl_s=30. Worst-case 30s wait. Serialises with B1 rather than racing. |
| F16 | `--no-reuse` + `--instance-id` + caller did not expect destroy | Per D7 + §3.5, `--no-reuse` destroys regardless of attach origin. Documented contract; operator opt-out by not passing `--no-reuse`. |
| F17 | Two parallel B3 scans pick different candidates (cap_key match has ≥2 entries) | Both succeed. No collision. Warm pods balance across concurrent sessions. |
| F18 | Heartbeat substrate disabled (`heartbeat_mode=none`) → all entries `HEARTBEAT_UNKNOWN` | Per D3, B3 never attaches on `HEARTBEAT_UNKNOWN`. All candidates skipped → cold create. Summary INFO names cause. Operator enables `heartbeat_mode` for warm-reuse. |

---

## 5. Test plan

### 5.1 `tests/cli/test_scan_warm_candidates.py` (new)

```python
def test_empty_ledger_returns_none():                                    # F1
def test_returns_none_when_no_cap_key_match():                           # F2
def test_returns_none_when_provider_mismatch():                          # F3
def test_filters_busy_entries_via_is_session_busy():                     # F4
def test_filters_classify_non_live_entries():                            # F5
def test_sorts_candidates_by_newest_heartbeat_thread_tick():             # D1
def test_skips_reaper_lock_held_candidate():                             # D5 / F6
def test_skips_provision_lock_held_candidate():                          # D2 / F7
def test_skips_candidate_on_resolve_warm_instance_failure():             # F5-F10
def test_returns_first_valid_candidate():                                # happy path
def test_record_includes_skipped_reasons_with_stable_codes():            # D6 vocabulary
def test_scan_report_summarize_attached_case():                          # D6
def test_scan_report_summarize_miss_case():                              # D6
def test_scan_report_summarize_empty_ledger_returns_empty_string():      # D6
def test_force_attach_param_is_false_always():                           # never bypass
def test_provider_constructed_once_across_candidates():                  # F9 cache
def test_list_instances_failure_aborts_scan_early():                     # F9
def test_uses_injected_clock_for_is_session_busy():                      # purity
```

### 5.2 `tests/core/test_ledger_session_fields.py` (new)

```python
def test_touch_writes_session_start():
def test_touch_writes_session_end():
def test_touch_session_start_then_session_end_both_persisted():
def test_protected_keys_filter_does_not_drop_session_fields():
def test_is_session_busy_false_when_session_start_absent():
def test_is_session_busy_true_when_session_start_set_session_end_absent_hb_fresh():
def test_is_session_busy_false_when_session_end_GTE_session_start():
def test_is_session_busy_true_when_session_end_lt_session_start():
def test_is_session_busy_false_when_heartbeat_thread_tick_stale():
def test_is_session_busy_false_when_heartbeat_thread_tick_missing():
def test_is_session_busy_true_when_heartbeat_interval_s_None_and_session_start_set():
def test_is_session_busy_uses_3x_sentinel_window_floor():
```

### 5.3 `tests/core/test_orchestrator_session_fields.py` (new)

```python
def test_deploy_session_writes_session_start_after_hb_start():
def test_deploy_session_writes_session_end_in_finally():
def test_deploy_session_session_start_absent_when_hb_disabled():
def test_deploy_session_session_start_absent_on_hosted_engine_path():
def test_session_end_written_even_on_exception_in_yielded_block():
def test_session_end_touch_failure_logs_warning_does_not_raise():
```

### 5.4 `tests/core/test_orchestrator_no_reuse.py` (new)

```python
def test_no_reuse_destroys_pod_at_exit():
def test_no_reuse_forgets_ledger_after_destroy():
def test_no_reuse_acquires_reaper_lock_during_destroy():
def test_no_reuse_destroys_even_when_caller_supplied_instance():        # D7
def test_no_reuse_destroy_failure_logs_error_does_not_raise():          # F14
def test_no_reuse_skips_destroy_on_hosted_engine_path():
def test_no_reuse_skips_destroy_when_instance_none():
def test_no_reuse_TeardownError_preserves_ledger_entry_for_reap_recovery():
def test_no_reuse_writes_session_end_before_destroy():
```

### 5.5 `tests/cli/test_cmd_generate.py` (delta)

```python
def test_generate_calls_scan_warm_candidates_when_no_instance_id():
def test_generate_skips_scan_when_no_reuse_flag_set():
def test_generate_skips_scan_when_warm_reuse_auto_attach_false():
def test_generate_passes_instance_kwarg_from_scan_hit():
def test_generate_no_reuse_threads_single_True_to_generate():
def test_generate_logs_scan_summary_on_hit():
def test_generate_logs_scan_summary_on_miss():
def test_generate_no_reuse_force_attach_mutex_exits_2():
def test_generate_explicit_instance_id_takes_precedence_over_scan():
def test_generate_no_reuse_with_instance_id_composes():
```

### 5.6 `tests/cli/test_cmd_batch.py` (delta)

```python
def test_batch_calls_scan_warm_candidates_when_no_instance_id():
def test_batch_no_reuse_destroys_after_full_batch_not_per_row():
def test_batch_no_reuse_force_attach_mutex_exits_2():
def test_batch_passes_instance_kwarg_from_scan_hit():
```

### 5.7 `tests/core/test_b3_warm_attach_xprocess.py` (new, MANDATORY)

Mirrors PROGRESS:1130 + B7 xprocess shape.

```python
def test_two_cli_invocations_share_warm_pod(tmp_path):
    """Two subprocess CLIs run kinoforge generate against the same cfg
    + FakeProvider. First creates pod; second auto-discovers it via B3.
    Assert: only ONE create_instance was called on the FakeProvider."""

def test_concurrent_attach_serializes_at_b7_lock(tmp_path):
    """Two subprocess CLIs scan ledger same instant + pick same pod.
    Both probe provision:<id> non-blocking → both see unheld → both
    enter deploy_session → blocking-acquire serializes."""

def test_no_reuse_destroys_during_concurrent_b3_scan(tmp_path):
    """Subprocess A runs --no-reuse. Subprocess B scans during A's
    __exit__ destroy window. Assert: B's scan skips A's pod with
    reason=reaper-held; B falls through to cold create."""

def test_busy_marker_blocks_concurrent_attach(tmp_path):
    """Subprocess A is mid-yield (writes session_start, holds the
    yielded block). Subprocess B scans. Assert: B's coarse-filter
    (§3.4 step 6) drops A's pod via is_session_busy=True; B's scan
    matches list is empty; B falls through to cold create. A's pod
    does NOT appear in B's _ScanReport.skipped (busy entries are
    filtered before per-candidate validation, not recorded as skips)."""

def test_stale_session_start_clears_via_heartbeat_freshness(tmp_path):
    """Subprocess A writes session_start then KILL -9 (no session_end).
    heartbeat_thread_tick ages out past 3*interval. Subprocess B scans.
    Assert: B's is_session_busy returns False; classify still LIVE."""
```

### 5.8 `tests/core/test_reaper_actor.py` (delta)

```python
def test_act_on_verdict_blocks_when_b3_no_reuse_destroy_holds_reaper_lock():
    """B3 __exit__ holds reaper:<id>; B1 sweeper-side act_on_verdict
    blocks at reaper:<id> acquire. After --no-reuse releases, B1
    re-classify sees pod gone (STALE_LEDGER) — clean cooperation."""
```

### 5.9 `tests/live/test_b3_warm_attach_live.py` (new)

Gated by `KINOFORGE_LIVE_RUNPOD=1`. Live spend ~$2.50.

```python
def test_two_generations_share_warm_pod_via_b3_auto_discovery():
    """
    Steps:
    1. Run `kinoforge generate` with prompt from
       /workspace/prompt-field-realistic.txt.
       Capture pod_id_1 from ledger. Assert artifact mp4 exists.
    2. Sleep 30s.
    3. Run `kinoforge generate` again with SAME cfg + SAME prompt.
       Capture pod_id_2 from ledger.
    4. Assert pod_id_1 == pod_id_2 (warm reuse fired).
    5. Assert second invocation's stdout contains
       "warm-reuse: attached to".
    6. Assert second invocation's spin-up time < 30s (cold = 1-5 min).
    7. Cleanup: `kinoforge destroy --id <pod_id_1>`.

    Budget: 2 generations × ~$0.50/gen + ~5 min warm-pod idle = ~$1.50
    wall. Buffer to $2.50 for cold-boot Wan model load + teardown
    polling.
    """
```

Per CLAUDE.md durability rule, smoke script + RED test committed BEFORE live invocation. Per `feedback_standard_test_prompt`, prompt body read verbatim from `prompt-field-realistic.txt`.

---

## 6. Acceptance criteria

- **AC1.** `Ledger.touch` accepts `session_start` + `session_end` kwargs and persists them via the existing `**extra` seam. `_PROTECTED_LEDGER_KEYS` unchanged.
- **AC2.** `is_session_busy(entry, *, now, heartbeat_interval_s)` pure helper at `core/lifecycle.py` returns True iff `session_start` set AND no later `session_end` AND `heartbeat_thread_tick` fresh per `3 * heartbeat_interval_s` window. Stale-busy auto-clears.
- **AC3.** `deploy_session.__enter__` writes `session_start` via `Ledger.touch` after `hb_loop.start()` returns, gated on `instance is not None AND resolved_provider is not None AND heartbeat_interval_s > 0`.
- **AC4.** `deploy_session.__exit__` writes `session_end` in the finally block before any `--no-reuse` destroy. Touch failure logs WARN and does not raise.
- **AC5.** `deploy_session(single=True)` at `__exit__` acquires `reaper:<id>` blocking, calls `destroy_confirmed` + `ledger.forget` for orchestrator-managed and caller-supplied paths alike (D7 composition). Destroy failure logs ERROR + preserves ledger entry; pod-id + recovery command named.
- **AC6.** `_scan_warm_candidates` filters ledger by `provider == cfg.compute.provider` AND `tags["kinoforge_key"] == cfg.capability_key().derive()[:12]` AND `not is_session_busy(entry)`. Sorts remaining by `heartbeat_thread_tick` descending.
- **AC7.** Per candidate: non-blocking probe `reaper:<id>` then `provision:<id>`; on held → skip with reason code. On unheld → call `_resolve_warm_instance(force_attach=False)`; on rc=2 → skip with reason code. On success → return `(Instance, _ScanReport)`.
- **AC8.** Reason-code vocabulary fixed: `reaper-held`, `provision-held`, `cap-key-drift`, `provider-mismatch`, `provider-unconstructable`, `list-instances-failed`, `classify-not-live`, `get-instance-keyerror`. No undocumented codes.
- **AC9.** `_ScanReport.summarize()` emits exactly the D6-locked single INFO line shapes. Empty-ledger / no-cap-key-match case returns empty string (silent cold create).
- **AC10.** `ComputeConfig.warm_reuse_auto_attach: bool = True`. YAML round-trip works; absent field defaults True; existing configs load unchanged.
- **AC11.** `_cmd_generate` + `_cmd_batch` precedence chain: `--instance-id` → `--no-reuse` → `compute.warm_reuse_auto_attach == false` → default scan. Each branch tested.
- **AC12.** `--no-reuse` AND `--force-attach` together → exit 2 with explicit mutex error. `--no-reuse` alone composes with `--instance-id`.
- **AC13.** argparse exposes `--no-reuse` on `p_generate` AND `p_batch`. Help text mentions cold-create + destroy semantics.
- **AC14.** Hosted-engine path (`requires_compute=False`): scan NEVER fires; `session_start`/`session_end` NEVER written; `--no-reuse` is a no-op (one INFO acknowledging it).
- **AC15.** Cross-process subprocess test demonstrates: two CLIs against same cfg → only one `create_instance` call; second CLI logs `warm-reuse: attached to <id>`.
- **AC16.** Live smoke (gated `KINOFORGE_LIVE_RUNPOD=1`): two generations 30s apart on same cfg attach to same pod; second invocation < 30s spin-up; ≤ $2.50 wall.
- **AC17.** `pixi run pre-commit run --all-files` clean. `pixi run pytest` green.
- **AC18.** `PROGRESS.md §B.B3` struck with closeout sha. `warm-reuse-tasks.txt:532-566` replaced with closeout pointer.

---

## 7. Risks

- **R1. Generate hot path blast radius.** B3 touches every `kinoforge generate` invocation. A scan bug could double-bill (attach + cold-create) OR wedge sessions waiting for a dead pod's lock. Mitigation: cross-process subprocess test (§5.7) is load-bearing; locked verdict gate refuses HEARTBEAT_UNKNOWN; cap_key derivation prevents wrong-pod attach.
- **R2. Cross-CLI session-busy field drift.** New ledger fields rely on consumers using `is_session_busy` correctly. Mitigation: pure helper at module scope; one writer site (deploy_session); one reader site (B3 scan, B2 follow-up). Centralized.
- **R3. `--no-reuse` destroy semantics surprise.** Operator using `--no-reuse` + `--instance-id` may not expect destroy. Mitigation: argparse help text explicit; D7 composition documented; mutex with `--force-attach` prevents the more dangerous combination.
- **R4. `session_start` written before first tick (race in §3.3).** Worst case: brief window where concurrent B3 scan sees entry as "not busy" and races to attach. B7 blocking-acquire serialises. No double-bill. Documented.
- **R5. Reason-code vocabulary lock-in.** B2 dashboard ingestion depends on the fixed code set. Adding a code requires updating both the helper + dashboard. Mitigation: 8 codes ship in B3; new codes treated as architecture change.
- **R6. Cross-host on cloud-store backend (S3/GCS).** B3 inherits B7's single-host assumption. Cross-host cooperative locking deferred to B16. Documented out-of-scope.

---

## 8. Out of scope

- B5b SkyPilot satisfier (gated A3/A4 GPU quota).
- B16 distributed cross-host lock semantics.
- B6 per-entry `heartbeat_interval_s` override.
- Engine-specific warm-reuse veto hook (engine.refuses_warm_attach).
- B2 cost-dashboard consumption of `session_start`/`session_end` for LIVE-busy vs LIVE-idle split (substrate ships here; consumer follows separately).
- `compute.destroy_on_exit: bool` YAML knob (YAGNI; `--no-reuse` covers v1).
- `kinoforge list --verbose` adding busy-state column.
- Per-row `--no-reuse` semantics in `kinoforge batch` (whole-batch only).
- `--no-reuse` for hosted-engine paths (no-op acknowledged via INFO).

---

## 9. Effort estimate

- ~80 LOC `src/kinoforge/cli/_commands.py` (scan + probe + report + wiring).
- ~10 LOC `src/kinoforge/cli/_main.py` (argparse).
- ~50 LOC `src/kinoforge/core/orchestrator.py` (single kwarg + session writes + destroy arm).
- ~15 LOC `src/kinoforge/core/lifecycle.py` (`is_session_busy` helper).
- ~5 LOC `src/kinoforge/core/config.py` (`warm_reuse_auto_attach` field).
- ~630 LOC tests across 5 new files + 4 deltas.
- ~40 LOC docs (README operator section + PROGRESS + closeout + YAML examples).

Live spend: **~$2.50** RunPod.

---

## 10. Task split (for /superpowers-extended-cc:write-plan)

| # | Task | Files | Live |
|---|---|---|---|
| a | `Ledger.touch` session-fields + `is_session_busy` helper + offline tests | `core/lifecycle.py`, `tests/core/test_ledger_session_fields.py` (new) | no |
| b | `deploy_session` `session_start`/`session_end` writes + offline tests | `core/orchestrator.py`, `tests/core/test_orchestrator_session_fields.py` (new) | no |
| c | `_scan_warm_candidates` + `_probe_lock_held` + `_ScanReport` + offline unit tests | `cli/_commands.py`, `tests/cli/test_scan_warm_candidates.py` (new) | no |
| d | `--no-reuse` argparse + `single` kwarg threading + mutex + `deploy_session.__exit__` destroy arm + offline tests | `cli/_main.py`, `cli/_commands.py`, `core/orchestrator.py`, `tests/core/test_orchestrator_no_reuse.py` (new) | no |
| e | `_cmd_generate` + `_cmd_batch` wiring (precedence chain, scan dispatch, summary log) + CLI dispatch tests | `cli/_commands.py`, `tests/cli/test_cmd_generate.py` (delta), `tests/cli/test_cmd_batch.py` (delta) | no |
| f | `ComputeConfig.warm_reuse_auto_attach` + config round-trip tests + YAML examples comment | `core/config.py`, `tests/core/test_config.py` (delta), `examples/configs/*.yaml` | no |
| g | Cross-process subprocess tests (xprocess shape) | `tests/core/test_b3_warm_attach_xprocess.py` (new) | no |
| h | Reaper integration delta | `tests/core/test_reaper_actor.py` (delta) | no |
| i | RED-scaffold commit: live smoke test + helper (per CLAUDE.md durability rule) | `tests/live/test_b3_warm_attach_live.py` (new, RED) | no (RED commit only) |
| j | Live RunPod smoke + PROGRESS + warm-reuse-tasks + spec closeout | run smoke; amend spec §9 with measured timings; strike PROGRESS §B.B3; update `warm-reuse-tasks.txt:532-566` | ≤$2.50 |

Order: a → b → c → d → e → f sequential; g + h parallel after b + d land; i commits before j; j is final.

---

## 11. Forward-compat hooks for downstream layers

- **B2 (cost dashboard).** `session_start`/`session_end` ledger fields enable LIVE-busy vs LIVE-idle split. `is_session_busy` is pure + reusable. B2 dashboard reads ledger entries directly; consumes the new fields by calling the helper. Reason-code vocabulary (D6) is stable + machine-parseable for per-skip-reason histograms in the dashboard.
- **B5b (SkyPilot satisfier).** B3's gate (D3) refuses attach on `HEARTBEAT_SUBSTRATE_MISSING`. When B5b ships and flips `provider_heartbeat_supported("skypilot") → True`, B3 transparently starts attaching to SkyPilot warm pods without code change.
- **B1 (sweeper daemon, already shipped).** Sweeper inherits B3's `--no-reuse` cooperation via the same `reaper:<id>` lock B1 takes for `act_on_verdict`. B3's destroy and B1's destroy serialise cleanly; no double-destroy.
- **B7 (cooperative lock, already shipped).** B3 reuses `hold_until_first_tick` unchanged; warm-supplied path through orchestrator inherits B7's blocking-acquire semantics for free.
- **B4 (cross-CLI manual attach, already shipped).** B3 calls B4's `_resolve_warm_instance` per candidate. B3's auto-discovery is the unattended-mode caller of B4's helper.
- **B6 (per-entry heartbeat_interval_s).** When B6 lands, `is_session_busy` should consult per-entry override. One-line change to use `_resolve(entry, "heartbeat_interval_s", default)` mirroring Layer V `_resolve` pattern.
- **B16 (distributed sweeper).** Cross-host `provision:<id>` + `reaper:<id>` semantics on S3/GCS-backed stores. B3 inherits B7's single-host assumption.

---

## 12. Sanity-checks against repo (verified 2026-06-13)

- `src/kinoforge/core/orchestrator.py:282-356` — `_LazyClaim` wrapper around `hold_until_first_tick` confirmed; warm-supplied `instance=` path enters at line 779 (cache-miss) and 812 (cache-hit) via `claim_holder.install(instance)`.
- `src/kinoforge/core/orchestrator.py:872-889` — HeartbeatLoop spawn gate confirmed: `interval > 0 AND instance is not None AND resolved_provider is not None`.
- `src/kinoforge/core/orchestrator.py:892-911` — `__exit__` finally block confirmed; B3 inserts `session_end` write + `--no-reuse` destroy arm here.
- `src/kinoforge/core/lifecycle.py:525-543` — `Ledger.read(instance_id)` confirmed (B7 shipped; B3 does NOT consume — pure scan via `entries()`).
- `src/kinoforge/core/lifecycle.py:562-631` — `Ledger.touch(**extra)` confirmed; `_PROTECTED_LEDGER_KEYS` filter at line 612 does NOT include `session_start`/`session_end`.
- `src/kinoforge/core/lifecycle.py:639-689` — `destroy_confirmed(provider, id, sleep=...)` confirmed; idempotent retry helper.
- `src/kinoforge/core/reaper.py:23-37` — `Verdict` enum confirmed; `HEARTBEAT_SUBSTRATE_MISSING` + `HEARTBEAT_UNKNOWN` distinct (B5a-shipped).
- `src/kinoforge/core/reaper_actor.py:~123,~193` — `with store.acquire_lock(f"reaper/{instance_id}", ttl_s=_LOCK_TTL_S)` + non-blocking `provision:<id>` probe confirmed (B7-shipped). B3's `--no-reuse` destroy mirrors this acquire shape.
- `src/kinoforge/core/heartbeat_endpoints.py:90-101` — `provider_heartbeat_supported` confirmed; B5a-shipped set `{"local", "runpod"}`.
- `src/kinoforge/core/config.py` — `ComputeConfig` confirmed; `heartbeat_mode` already at module body. B3 adds `warm_reuse_auto_attach` sibling field.
- B4 `_resolve_warm_instance` at `cli/_commands.py` confirmed; force_attach kwarg already exists.

---

## 13. PROGRESS.md updates on B3 close

- §Active workstream: B3 → CLOSED by closeout sha.
- §B Spec-locked future layers: strike B3 entry; replace with closeout pointer to this spec + plan + sha.
- §Known limitations: no entries added (B3 closes a gap).
- `successful-generations.md`: live smoke produces a video AND introduces a new capability axis (B3 auto-discovery). Add new entry per file preamble schema.
- `warm-reuse-tasks.txt:532-566`: replace B3 starter with one-line closeout pointer + commit sha.
