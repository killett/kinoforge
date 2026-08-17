# Provider capability declaration — design

**Status:** validated 2026-08-16, not yet implemented.
**Brief:** "make unenforced guardrails visible instead of silent" (Brief 2 of the cloud-layer wave).
**Depends on:** `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md` (F1, F3, F4),
`docs/superpowers/specs/2026-08-15-skypilot-instance-deadline-watchdog-design.md` (Brief 1, shipped).
**Live spend:** none required.

---

## 1. Problem

`ComputeProvider` gives three safety methods a permissive default: `last_heartbeat` returns `None`,
`probe_runtime` returns `None`, `set_heartbeat_endpoint` is `pass` (`core/interfaces.py:254-310`).
`SkyPilotProvider` inherits all three and adds no-op overrides of its own for `heartbeat()` and
`stop_instance()` (`providers/skypilot/__init__.py:994-1053`). Nothing tells an operator which
guardrails the selected provider can actually enforce, so a config can request protection that the
provider discards.

### 1.1 What the brief assumed, and what verification actually found

The brief was written against finding F3 as originally reported, and says "assumes F3 confirmed".
F3's verdict was **CHANGED**, not confirmed (verification doc `:193-339`). Two of its sub-claims do
not survive, and the design must not be built on them:

* **`HEARTBEAT_SUBSTRATE_MISSING` does not gate skypilot on the normal path.**
  `HeartbeatIntervalRequiredCheck` auto-fixes `heartbeat_interval_s: 30` at load, the loop starts, and
  `HeartbeatLoop._tick_once` writes both `last_heartbeat` (orchestrator-clock fallback) and
  `heartbeat_thread_tick` for every provider. SkyPilot rows classify `LIVE` / `IDLE_REAP` /
  `ORPHAN_REAP` / `OVERAGE_REAP` normally.
* **The config comment `heartbeat_interval_s: 30  # required when warm_reuse_auto_attach=true` is
  accurate.** The check genuinely requires the key. "The configs assert a guardrail the provider
  discards" is false as literally stated about that line.

The brief's *conclusion* nevertheless holds, for a different reason, and that reason is what this
design targets.

### 1.2 The actual dishonesty

**Guardrails are fake-satisfied, not absent.** The heartbeat loop writes `last_heartbeat` from the
orchestrator clock when the provider's read returns `None`. The resulting ledger row is
indistinguishable from one backed by a real wire-level read. So the reaper's liveness signal on
skypilot means *"the controller process is alive"*, not *"the cluster is alive"* — and nothing in the
config, the logs, or the ledger says so.

Secondary: `kinoforge stop --id` against a SkyPilot cluster reports success and the cluster keeps
billing (`stop_instance` is a silent no-op); against Modal it destroys the app and the warm container
(`stop_instance` aliases `destroy_instance`).

### 1.3 The union already exists, keyed by string

Five provider-support tables already ship, none tied to the class that implements the behaviour:

| Table | Contents |
|---|---|
| `core/heartbeat_endpoints.py:89` `_HEARTBEAT_SUPPORTED` | `{local, runpod}` |
| `core/util_endpoints.py:66` `_UTIL_SUPPORTED` | `{local, modal, runpod}` |
| `core/balance_endpoints.py:91` `_SUPPORTED` | `{runpod}` |
| `core/ephemeral.py:81` `EPHEMERAL_CAPABILITIES` | `(engine, provider) -> bool` |
| `cli/_reconcile.py:38` `_RECONCILABLE_PROVIDERS` | `{runpod, skypilot}` |

Because they are string sets, a provider can be listed as supporting something its method no-ops, and
nothing detects it. This answers the brief's challenge — a capability set does not *relocate* the
union problem into an enum, it *consolidates* five existing unions into one place that can be
checked against the code.

---

## 2. Goal

A provider declares what it can actually enforce. Config validation compares the declaration against
what the config asks for and reports every guardrail the provider cannot honour, before launch.

Capabilities are derived from what the code does. Every cell of the matrix in §7 names the call site
that makes it true.

---

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Declarative capability set, not provider-declared refusal | A refusal API (`validate_spec(spec)` raising) answers yes/no for one concrete spec. It cannot populate `provider_heartbeat_supported(name)`, cannot express "warn — the watchdog covers it", and gives the reaper no answer at all, since the reaper has a ledger row and no spec. |
| D2 | No refusal hook alongside (brief's option C rejected) | Workload shape is the only condition in the code today and D4 handles it. A second mechanism creates ambiguity about which one owns a rule. Add it when a real condition appears that shape cannot express. |
| D3 | Severity by risk coverage, not blanket fatal or blanket warn | Fatal when *no* declared capability bounds the same risk; loud WARN naming the substitute when one does. Blanket-fatal breaks all five shipped skypilot configs; blanket-warn is the status quo the brief is ending. |
| D4 | `IDLE_AUTOSTOP` parameterised by workload shape | Both shapes ship. Server specs (`spec.run_cmd` non-empty) make autostop provably inert (F1); batch specs (`run_cmd=[]`, e.g. `upscalers/flashvsr/_engine.py:199`) reach idle and autostop fires. A single boolean would lie in one direction or the other. |
| D5 | Existing string tables derive from the declaration | Drift becomes structurally impossible instead of test-enforced. Call sites keep their signatures. |
| D6 | Reaper: split expected from unexpected heartbeat absence; no verdict becomes destructive | Fail-open was right for the *destroy* decision. It was wrong for *classification* — the verdict short-circuited before age and grace evidence that never depended on heartbeat. |
| D7 | Controller-side reaping never counts as risk coverage | `OVERAGE_REAP` does destroy a stale cluster, but only when a human runs `reap --apply` in the `live-skypilot` env (F3 `:315-320`). A guardrail requiring someone to remember it is not a guardrail. |

---

## 4. Vocabulary

New module `src/kinoforge/core/capabilities.py`.

```python
class Capability(StrEnum):
    HEARTBEAT_READ        # last_heartbeat() returns a timestamp sourced from the instance
    RUNTIME_PROBE         # probe_runtime() returns a real RuntimeProbe (existence + util)
    UTIL_SNAPSHOT         # build_util_endpoint_for() has a wire path returning UtilSnapshot
    IDLE_AUTOSTOP         # the provider itself stops the instance after idleness
    ON_INSTANCE_DEADLINE  # something ON the instance kills it at a wall-clock deadline
    JOB_TIMEOUT           # the provider enforces cfg's per-job execution timeout
    PAUSE_BILLING         # stop_instance() pauses billing without destroying
    BALANCE_QUERY         # live account balance readable from the provider


class WorkloadShape(StrEnum):
    SERVER = "server"   # spec.run_cmd non-empty -> long-lived process, never idle
    BATCH  = "batch"    # spec.run_cmd empty     -> provision script runs and exits
```

Three departures from the brief's straw enum, each forced by code:

* **`RUNTIME_PROBE` splits into `RUNTIME_PROBE` + `UTIL_SNAPSHOT`.** Different substrates, different
  consumers. `probe_runtime()` (sweeper `GC_404` / `STALL_REAP`) exists on runpod and modal only; the
  util-endpoint path (`_UTIL_SUPPORTED`, `_adapters.py:280`) also lists `local`, whose
  `providers/local/util.py:5` docstring states it exists "so `provider_util_supported('local')`
  returns True without a real wire path". One member would force a lie about local either way.
* **`LIVE_COST_QUERY` → `BALANCE_QUERY`.** `core/balance_endpoints.py` reads account balance, not
  per-instance realized cost. Nothing in the codebase queries realized instance cost (F4), so a
  `LIVE_COST_QUERY` member would be aspirational on every provider.
* **`PAUSE_RESUME` → `PAUSE_BILLING`.** RunPod's stop mutation pauses billing, but `ComputeProvider`
  has no `start_instance` — nothing in kinoforge can resume.

`JOB_TIMEOUT` was added during the audit: `lifecycle.job_timeout` is not inert, it reaches RunPod as
`executionTimeoutMs` at create. `core/lifecycle.py` also consumes it for budget *estimation*, which is
arithmetic, not enforcement, and does not count as a declaration.

---

## 5. Declaration surface

```python
class ComputeProvider(ABC):
    billed: ClassVar[bool] = True

    @classmethod
    def capabilities(
        cls, shape: WorkloadShape = WorkloadShape.SERVER
    ) -> frozenset[Capability]:
        return frozenset()
```

* **Classmethod**, so a by-name lookup needs no instance and no credentials — required by the reaper,
  which must answer in a process that may not be able to reach the provider at all.
* **Default empty**, not inherit-everything. A provider that forgets to declare is refused loudly by
  config validation rather than silently trusted.
* **`billed`** is a property, not a capability. `LocalProvider` sets `False`; spend-risk rows in §6 are
  skipped for unbilled providers, liveness rows still apply. Without it, every local config errors for
  having no deadline substrate on a provider that bills nothing.

**Registry.** `core/registry.py:42` `register_provider(name, factory, provider_cls)` — third argument
required, four call sites (`providers/{local,runpod,skypilot,modal}/__init__.py`). New
`capabilities_for(name, shape) -> frozenset[Capability]`; unknown name returns the empty set, matching
today's `name not in frozenset` semantics. On a miss it first lazily imports `kinoforge._adapters` —
the single place all four provider modules are imported — so a reaper unit context that never touched
providers resolves identically to production instead of silently degrading to "nothing is supported".

`provider_heartbeat_supported()`, `provider_util_supported()` and the balance predicate keep their
signatures and become one-line derivations. Call sites at `reaper.py:178/239/448`, `_adapters.py:280`
and `cli/_commands.py:3020` are untouched.

---

## 6. Validation

New check `ProviderCapabilityCheck` in `validation/checks/capabilities.py`, `CheckCategory.STATIC`,
registered like `HeartbeatIntervalRequiredCheck`. `applies_to`: `cfg.compute is not None`.
**No `auto_fix`** — silently rewriting a guardrail request is how the current state became invisible.

It works from a risk table, not a per-key capability table:

| Config asserts | Risk | Primary | Accepted substitute |
|---|---|---|---|
| `lifecycle.max_lifetime` + `lifecycle.budget` | instance outlives the controller | `ON_INSTANCE_DEADLINE` | none |
| `lifecycle.idle_timeout` | alive but doing nothing | `IDLE_AUTOSTOP` (at inferred shape) | `ON_INSTANCE_DEADLINE` (bounds it, coarser) |
| `lifecycle.job_timeout` | a single job runs away | `JOB_TIMEOUT` | `ON_INSTANCE_DEADLINE` |
| `lifecycle.heartbeat_interval_s` | liveness signal is fiction | `HEARTBEAT_READ` | none (risk is a wrong warm-attach, not spend) |
| `lifecycle.stall_window_s` | GPU idle mid-job | `UTIL_SNAPSHOT` | none |

Resolution per row: primary declared → silent pass. Substitute declared → **WARN naming the substitute
and the numeric bound it actually enforces**. Neither declared → **ERROR (load refused) when the row is
a spend risk**, WARN otherwise.

The spend-risk qualifier is what makes the severity rule match the outcomes in §6.1. `max_lifetime`,
`idle_timeout` and `job_timeout` bound money: a config that asks for one where nothing can honour it is
asking for a protection that does not exist, and refusing the load is the honest answer. The liveness
rows — `heartbeat_interval_s` and `stall_window_s` — degrade a *signal*, not the spend bound: on
skypilot the heartbeat still runs and warm-attach still works, it just proves the controller is alive
rather than the cluster. Refusing those would break every shipped skypilot config to report something
that costs no money.

`max_usd_per_hr` is deliberately absent from the table. It binds only at offer selection
(`core/offers.py:38`, F4) and is provider-independent; making it a capability row would misrepresent a
selection filter as a runtime guardrail.

### 6.1 Effect on what ships today

* `skypilot-gpu.yaml`, `skypilot-cpu.yaml`, `skypilot-lambda-comfyui.yaml` (SERVER): `max_lifetime`
  passes (watchdog); `idle_timeout` WARNs — "skypilot autostop is inert for server specs; bounded
  instead by the instance-side deadline watchdog at max_lifetime=30m"; `job_timeout` WARNs with the
  same substitute; `heartbeat_interval_s` WARNs — "no wire-level heartbeat read; `last_heartbeat` is
  the orchestrator clock, so it proves the controller is alive, not the cluster". **No errors — nothing
  that ships today breaks.**
* `skypilot-lambda-diffusers-flashvsr-upscale.yaml`,
  `skypilot-vast-diffusers-flashvsr-upscale.yaml`: **also SERVER**, and they warn exactly like the three
  above. Corrected 2026-08-16 during Task 4 review — an earlier draft of this section called them BATCH
  on the mistaken belief that `upscale_only: true` produces an empty `run_cmd`. It does not:
  `engines/diffusers/__init__.py:1274` always sets `run_cmd=server_cmd`, and `upscale_only` only adds
  `KINOFORGE_SKIP_WAN_LOAD=1`. The `run_cmd=[]` renders in `upscalers/*/_engine.py` and
  `interpolators/rife/_engine.py` belong to pipeline STAGES that run against an already-provisioned
  instance (`orchestrator.py:2008-2022`, `:2031-2035`) — they never provision a pod.
* Had Brief 1 not landed, skypilot would declare no `ON_INSTANCE_DEADLINE` and `max_lifetime` would be
  a hard ERROR. The fatal path is reachable and fires where money actually escapes.
* RunPod configs setting `idle_timeout` gain a WARN (see §7, runpod has no idle autostop). New warning
  on the most-used provider, and accurate.

### 6.2 Shape inference

**Every spec kinoforge provisions today is SERVER.** No shipped path renders an empty `run_cmd` into an
`InstanceSpec`, so load-time inference returns `SERVER` unconditionally and says so in its docstring —
inventing a heuristic for a shape nothing produces would be exactly the aspirational modelling this
design bans elsewhere.

`WorkloadShape` is kept rather than deleted because the capability claim it guards is real and
verifiable: SkyPilot autostop genuinely does fire for a spec whose job terminates, and
`providers/skypilot/__init__.py:908-910` already branches on an empty `run_cmd`. Declaring
`IDLE_AUTOSTOP` unconditionally would be a lie about server deploys; declaring it never would be a lie
about the substrate.

The check therefore **re-runs at launch** inside `deploy_session`, where `spec.run_cmd` is
authoritative — that is the only place `BATCH` can arise today. The launch-time verdict wins; an ERROR
there aborts before `create_instance`. A load-time/launch-time disagreement is logged as an inference
miss rather than swallowed, so the day a batch-shaped deploy does appear, it announces itself.

---

## 7. Provider audit

**Governing rule.** A no-op on a method the runtime calls *unconditionally* stays a no-op and is
declared absent. A no-op on a method an *operator invokes deliberately* becomes an explicit refusal —
the operator asked a question and deserves an answer.

| Method | Provider | Today | Decision |
|---|---|---|---|
| `last_heartbeat` | ABC default | `None` | Keep. `HeartbeatLoop._tick_once` calls it every tick on every provider; the 2026-06 AttributeError incident is why the default exists. Declared via `HEARTBEAT_READ`. |
| `probe_runtime` | ABC default | `None` | Keep. Sweeper already reads `None` as `SKIP_NO_PROBE`. Declared via `RUNTIME_PROBE`. |
| `set_heartbeat_endpoint` | ABC default | `pass` | **Narrow.** Raise `ValueError` when a non-`None` endpoint reaches a provider that does not declare `HEARTBEAT_READ`; the `None` clear path still passes. Discarding an endpoint someone built is a wiring bug, not a capability gap. |
| `heartbeat()` | skypilot, modal | no-op | Keep — the loop calls it unconditionally. Docstrings change from "SkyPilot manages liveness via autostop" (disproved by F1) to "no-op by design; `HEARTBEAT_READ` not declared". |
| `stop_instance()` | **skypilot** | silent no-op | **`NotImplementedError`.** Today `kinoforge stop --id` reports success while the cluster keeps billing. |
| `stop_instance()` | **modal** | aliases `destroy_instance` | **`NotImplementedError`.** Asking to pause and getting a destroyed app plus a lost warm container is a data-losing surprise; `kinoforge destroy` already serves that intent. |

`cli/_commands.py:2385-2386` gains a `PAUSE_BILLING` pre-check, so the operator gets a routed message
("skypilot cannot pause billing; use `kinoforge destroy --id`") rather than a traceback. The
`NotImplementedError` remains the backstop for callers that bypass the CLI.

### 7.1 Declared matrix

| | local | runpod | skypilot | modal |
|---|---|---|---|---|
| `HEARTBEAT_READ` | ✓ `local:189` | ✓ `runpod:614` | ✗ | ✗ |
| `RUNTIME_PROBE` | ✗ | ✓ `runpod:640` | ✗ | ✓ `modal:285` |
| `UTIL_SNAPSHOT` | ✓ seam | ✓ | ✗ | ✓ |
| `IDLE_AUTOSTOP` | ✗ | ✗ | ✓ **BATCH only** | ✓ `_app.py:37` `scaledown_window=300` |
| `ON_INSTANCE_DEADLINE` | ✗ | ✓ `selfterm.py` boot cap | ✓ `watchdog.py` | ✓ `_app.py:135` `timeout` |
| `JOB_TIMEOUT` | ✗ | ✓ `executionTimeoutMs` | ✗ | ✗ |
| `PAUSE_BILLING` | ✓ | ✓ `runpod:530` | ✗ | ✗ |
| `BALANCE_QUERY` | ✗ | ✓ | ✗ | ✗ |
| `billed` | `False` | `True` | `True` | `True` |

Two cells whose caveat must survive into the code, not just this table:

* **local `UTIL_SNAPSHOT`** is a scripted in-process test seam. It stays declared — the endpoint does
  return snapshots, `local` is unbilled, and no money decision rides on it — with a docstring saying
  scripted, in-process, not a measurement.
* **runpod has no `IDLE_AUTOSTOP`.** `selfterm.py` is a boot-relative cap (B4, `67627cd0`), not idle
  detection. RunPod idle reaping is controller-side only, which D7 rules out as coverage.

---

## 8. Reaper — ATTEMPTED, REVERTED 2026-08-17

**Outcome: the gate is unchanged. `HEARTBEAT_SUBSTRATE_MISSING` stays fail-open.** The brief asked
whether fail-open should survive now that capabilities are explicit. Three rounds of implementation and
adversarial review answered it empirically, and the answer is yes — for a reason that is worth more
than the change would have been.

**On a capability-less provider, the ledger cannot distinguish a stranded row from an actively-driven
one.** The two shapes are byte-identical:

```
{"id": "sky-1", "provider": "skypilot", "created_at": t0, "session_end": t1}
```

That is *both* the orphan this change set out to unstrand *and* a warm-reused pod midway through a
second render. Warm re-attach — kinoforge's default flow — writes nothing to the row: `Ledger.record`
runs only on cold create, and `session_start`'s single writer (`orchestrator.py:1509-1514`) is gated on
a heartbeat loop that skypilot can never have, because `_adapters.py:185-189` forces
`heartbeat_mode: none` on that provider. So grace keeps measuring from the *previous* session's
`session_end`, and a render that starts 25 minutes later is classified `ORPHAN_REAP` five minutes in.

Two weaker guards were tried and disproved before this conclusion:

1. **`is_session_busy` alone** — inert. `session_start` exists only when a heartbeat loop runs, and when
   one runs it writes both sentinel fields, so the gate is never reached. On every row where the gate
   *is* reachable with a live driver, the guard reads `False`.
2. **`is_session_busy` + `session_end is not None`** — closes the first-session case, and the warm
   re-attach case walks straight through it, because `session_end` is exactly the field a warm row
   carries.

No predicate over the current ledger fields separates the two, so this cannot be fixed inside
`classify`. The upstream fixes that would work are real but out of scope here: write `session_start`
unconditionally on attach (drop the `hb_loop` gate), or run the heartbeat loop on capability-less
providers so those rows leave the row-7 path entirely. Either belongs to whoever owns the attach path
and the B5b heartbeat substrate.

**What this costs:** stranded rows — ephemeral index rows, cross-process warm rows, the provisional
`kf_launch_phase=launching` row — stay a dead end and still need `kinoforge forget`. That is the price
of not having a destroy path that cannot tell a live pod from an abandoned one, and it is the right
trade for a component whose only power is destruction.

The design below is retained as the record of what was attempted and why it was withdrawn.

### 8.1 The withdrawn design

Target: the Row-7 gate at `core/reaper.py:446-452`.

Two facts to keep straight. `OVERAGE_REAP` returns at `:426`, *before* this gate, so the lifetime cap
already works on skypilot. And the gate only fires when the ledger row lacks heartbeat fields, which a
running loop always writes — so the rows that actually dead-end here are those written outside a
heartbeat loop: ephemeral index rows, cross-process warm rows, and (since Brief 1) the provisional
`kf_launch_phase=launching` row written before `sky.launch`.

**Change.** The gate consults `Capability.HEARTBEAT_READ` (through the derived
`provider_heartbeat_supported`, so the call site is unchanged) and stops returning early on expected
absence:

* Capability **not** declared + fields missing → fall through to the grace evaluation at `:499-507`,
  **guarded by a liveness precondition**. Past `grace_after_session_s` **and no open session claim** →
  `ORPHAN_REAP`; otherwise → `HEARTBEAT_SUBSTRATE_MISSING`, exactly as today.

  The precondition was added 2026-08-16 after the Task 6 review found that the grace rule's safety
  premise does not transfer to this call site. On the rows-5-and-6 path, grace is only consulted after
  `sent_age > sentinel_window` has already proven the driver is dead. Here the heartbeat fields are
  simply *absent*, so age alone would be asserting orphanhood with no liveness evidence at all. With
  `compute.heartbeat_mode: none` the gate condition is true for every row permanently, and
  `session_end` is written only at teardown — so an actively-generating skypilot or modal pod would
  classify `ORPHAN_REAP` at 30 minutes of pod age and be destroyed by a sweeper running
  `include_orphans`, where before it was immune. `core/lifecycle.py:60-98` already implements
  `is_session_busy` for exactly this question; the row also carries `session_start` and
  `kf_launch_phase`. A stranded row with no open claim still gets judged on its age evidence, which is
  the point of the change.

  **`is_session_busy` alone is not sufficient** — established by the round-2 re-review. `session_start`
  has exactly one writer (`orchestrator.py:1510-1514`) and it sits inside the heartbeat-loop branch, so
  it exists only when a loop is running; and when a loop runs it writes both sentinel fields
  (`heartbeat_loop.py:236-256`, substituting the orchestrator clock when the provider read returns
  `None`), which means the gate is never reached at all. Whenever the gate IS reachable with a live
  driver — heartbeat disabled, or the pre-loop launch window — the row carries no `session_start` and
  the busy check returns `False`. The guard would have been correct code that never fires.

  The fall-through therefore requires **`session_end is not None`**: positive evidence that a session
  ran and finished, rather than the absence of evidence that one is running. A row measuring grace from
  `pod_age` alone — including the provisional `kf_launch_phase=launching` row — stays
  `HEARTBEAT_SUBSTRATE_MISSING`, non-destructive, exactly as before. Every row this change was written
  to unstrand (ephemeral index rows and cross-process warm rows from completed sessions) carries
  `session_end`, so the goal survives.
* Capability **declared** + fields missing → `HEARTBEAT_UNKNOWN`, unchanged. This is now a genuine
  anomaly: the provider says it can read a heartbeat and the row has none.

**The verdict name `HEARTBEAT_SUBSTRATE_MISSING` is kept.** The `Verdict` docstring (`:27-31`) declares
insertion order and values a serialized public contract, and the string appears in
`_FORCE_BYPASSABLE_VERDICTS`, `DEFAULT_STRICT_VERDICTS`, `docs/warm-reuse.md` and the sweeper tables.
Its meaning narrows to "expected absence, still inside grace"; behaviour there is identical, so nothing
downstream migrates. A rename buys a better word and costs a migration.

**No verdict becomes destructive on its own.** `ORPHAN_REAP` stays out of `DEFAULT_APPLY_POLICY`
(`:63-75`); reaching it still requires `--include-orphans` or `cfg.sweeper.include_orphans`. Plain
`kinoforge reap --apply` behaves identically on every row. What changes: an operator who *asks* for
orphan reaping now gets these rows instead of a verdict that explains why nothing can be done.

---

## 9. Configs and docs

`skypilot-gpu.yaml` header `:8` currently reads `autostop = 3 min, max_lifetime = 30 min`. That is the
false assertion — not the heartbeat line. The config is `engine: comfyui`, i.e. SERVER shape, where
autostop provably never fires. Rewritten to state that `idle_timeout: 180` maps to SkyPilot autostop
which is **inert for server-mode deploys**, and that the bound which holds is the instance-side
deadline watchdog at `max_lifetime: 1800`. Same for `:49` and `:51`.

`:54` keeps `# required when warm_reuse_auto_attach=true` — verified accurate — and gains the missing
half: this heartbeat is orchestrator-clock, so it proves the controller is alive, not the cluster.

`skypilot-cpu.yaml:7-8` claims "Quadruple-locked cleanup: idle_timeout 60 s -> SkyPilot autostop=1
(minute)", and `:47-50` repeats the mapping as if it enforced something; one of the four locks does not
exist for this shape. Corrected the same way. `skypilot-lambda-comfyui.yaml:57-62` likewise.

The two `*-flashvsr-upscale.yaml` get the **same** correction as the three above, not an opposite one:
`upscale_only: true` does not empty `run_cmd`, so these are server deploys and autostop is inert for
them too. Their comment additionally records why the intuition fails — the upscaler runs as a pipeline
stage against an already-provisioned pod, so the `run_cmd=[]` in `upscalers/flashvsr/_engine.py` never
reaches an `InstanceSpec`. Both currently omit `heartbeat_interval_s` and receive `30` by auto-fix at
load; the key is written in explicitly, because an invisible auto-fix is the same category of problem
as an unenforced guardrail.

Docs: `docs/warm-reuse.md` and the sweeper verdict tables encode the pre-change gate behaviour (already
flagged stale in F3's blast radius) and are rewritten against §8. `docs/lifecycle.md` gains the §7.1
matrix. `docs/extending.md` gains the rule for new providers — `capabilities()` defaults to empty, so a
provider that does not declare is refused by validation; declaring is part of writing a provider.

---

## 10. Test plan

Expected values are hand-derived from §7.1 and documented defaults, never by executing the code under
test. Mocking stays at the network boundary — fake transports, real `Config` / `load_config`, real
`classify`. **No live spend.**

**A. Declaration ↔ implementation parity** (the regression guard the brief names). Table-driven over
four provider classes × 8 capabilities, each capability with an *observable* predicate rather than a
second hardcoded list:

* `HEARTBEAT_READ` ⟺ `cls.last_heartbeat is not ComputeProvider.last_heartbeat`; same identity test
  for `RUNTIME_PROBE`. Catches: a `return None` override added or a real one deleted without editing
  the declaration.
* `PAUSE_BILLING` ⟺ `stop_instance` against a fake transport issues a provider call; absent ⟺ raises
  `NotImplementedError`. Catches: a provider declaring pause while its method silently does nothing —
  today's skypilot bug, reintroduced.
* `JOB_TIMEOUT` on runpod ⟺ create payload carries `executionTimeoutMs == job_timeout * 1000`;
  `IDLE_AUTOSTOP` on modal ⟺ built app request carries `scaledown_window`; `ON_INSTANCE_DEADLINE` on
  skypilot ⟺ rendered `Task.setup` opens with the watchdog arm prelude. Catches: a wiring regression
  leaving the declaration true and the enforcement gone — the F1 failure mode.
* A synthetic provider class registered in-test that declares `RUNTIME_PROBE` with no override must
  make the parity test **fail**. Catches: a parity test that passes vacuously.

**B. Validator.** `heartbeat_interval_s` + skypilot → `WARN`, message names the orchestrator clock
(catches a severity mapping that errors on covered risk or warns on uncovered). `max_lifetime` on a
provider with neither primary nor substitute → `load_config` raises, message names capability and
provider (catches a missing risk row letting an unbounded-spend config launch). Local config with
`idle_timeout` loads clean (catches `billed` being ignored). Check exposes no `auto_fix` and
`load_config` leaves guardrail values byte-identical (catches a future auto-fix that silences the
diagnostic by rewriting the guardrail). `infer_shape` returns SERVER for an upscale-only cfg (catches a
heuristic that guesses BATCH from `upscale_only` and thereby reports `idle_timeout` as enforced on a
cluster where autostop is inert — the dangerous direction, and the actual Task 4 review finding). Launch-time re-check fed a spec whose `run_cmd` contradicts the load-time
inference asserts the diagnostic follows the **spec** (catches a re-check that re-reads cfg and can
never catch an inference miss).

**C. Shipped configs.** Parametrized over `examples/configs/*.yaml`: zero ERROR findings, and for the
three server skypilot configs the **exact expected WARN set**. Catches both directions — a config edit
reintroducing an unenforceable guardrail, and risk-table drift making real warnings disappear.

**D. Reaper.** Boundary pair at the documented 1800 s grace with no capability and no heartbeat fields:
`session_end = now - 1801` → `ORPHAN_REAP`, `now - 1799` → `HEARTBEAT_SUBSTRATE_MISSING` (catches a
fall-through measuring from `pod_age`, and off-by-one at the boundary). Capability declared + fields
missing → `HEARTBEAT_UNKNOWN` (catches an inverted capability check). Row past `max_lifetime` with no
fields → still `OVERAGE_REAP` (catches the fall-through being inserted above the age check at `:426`).
`partition` under `DEFAULT_APPLY_POLICY` puts new `ORPHAN_REAP` rows in `to_skip` (catches someone
adding `ORPHAN_REAP` to the default policy while wiring this, silently making the change destructive).

**E. Derived tables.** Parametrized over registry names:
`provider_heartbeat_supported(n) == (HEARTBEAT_READ in capabilities_for(n))`; unknown name → `False`
(catches the derivation diverging from the declaration it replaced). Lazy-import path exercised from a
context that never imported providers, asserting `True` for runpod (catches the reaper answering
`False` for every provider in a process where registration had not happened — a silent, total
disabling of the gate).

**F. Refusals.** `NotImplementedError` from skypilot and modal `stop_instance`, message naming
`destroy`. CLI `stop --id` on a skypilot row exits non-zero with **zero** destroy calls recorded on the
fake provider (catches a pre-check that falls through and turns a pause request into a teardown).
`set_heartbeat_endpoint(non-None)` on skypilot → `ValueError`; `None` → no raise (catches the uniform
install path silently discarding a wired endpoint).

---

## 11. Non-goals

* Restructuring `ComputeConfig`'s provider-specific fields — Brief 5.
* New provider features. No provider gains enforcement it lacks today; this design makes existing
  behaviour honest.
* Folding `EPHEMERAL_CAPABILITIES` and `_RECONCILABLE_PROVIDERS` into the vocabulary. The former is
  keyed on engine as well as provider and would drag engine capabilities into a provider enum.
* Fixing the env-routing gap (F3: `sky` only in the `live-skypilot` env, so default-env sweeps mark
  every skypilot row `UNROUTABLE`). Real, separate, and orthogonal to declaration.
* A `region` field, secret-scanning parity, or any other F-finding remediation.

---

## 12. Residual gaps after this lands

1. **The env-routing gap stays open.** A default-env `reap --apply` still reports `UNROUTABLE` for
   skypilot rows and leaves the cluster billing. §8 makes more rows classifiable, not more reachable.
2. **`WorkloadShape` inference at load is heuristic.** §6.2 makes the launch-time re-check
   authoritative, so a wrong inference is caught rather than trusted — but a config can still emit a
   load-time diagnostic that the launch corrects.
3. **`billed` is a coarse flag.** It is per-class, so a hypothetical provider that bills in some modes
   and not others would need it promoted to a method.
