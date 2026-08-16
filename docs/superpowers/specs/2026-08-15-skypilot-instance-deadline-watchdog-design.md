# SkyPilot instance-side deadline watchdog — design

- **Date:** 2026-08-15
- **Status:** IMPLEMENTED + LIVE-GREEN 2026-08-15 (see §4.3). Plan:
  `docs/superpowers/plans/2026-08-15-skypilot-instance-deadline-watchdog.md`
- **Brief:** "guarantee a SkyPilot cluster dies without the client"
- **Depends on:** `docs/superpowers/research/2026-08-15-cloud-layer-findings-verification.md`
  (findings F1, F2, F12)
- **Sky pin:** `skypilot-0.12.3.post1` (`pixi.lock`); every SkyPilot-internal claim below was
  read from the INSTALLED sources under
  `.pixi/envs/live-skypilot/lib/python3.12/site-packages/sky/`, not from docs.

---

## 1. Premise check

The brief assumes F1, F2 and F12 were confirmed. Against HEAD `cdded908`:

| Finding | Verdict | Effect on this design |
|---|---|---|
| F1 — autostop inert for server-mode | **CONFIRMED** for the job-queue mechanism; **REFUTED** for the ssh mechanism | Design proceeds. See correction below. |
| F2 — autostop stops rather than terminates | **CONFIRMED** | `down=True` is in scope. |
| F12 — no durable record before `sky.launch` | **CONFIRMED** | Pre-launch ledger row is in scope. |

**Correction to the brief's problem statement.** The brief says "an open ssh tunnel resets the
idleness timer independently". That is not true at this pin. `autostop_lib.has_active_ssh_sessions()`
(`sky/skylet/autostop_lib.py:236-279`) walks `/dev/pts/*` PTYs and asks whether any traces back to
`sshd`. kinoforge's tunnel is `ssh -N -T` (`providers/skypilot/__init__.py:369-382`) — `-T`
disables PTY allocation, `-N` runs no remote command, so no `/dev/pts` entry exists and the check
returns `False` for kinoforge's tunnel.

The conclusion survives on the other mechanism alone: `spec.run_cmd` becomes `Task.run`, which is
submitted as a cluster job and stays non-terminal forever, so `job_lib.is_cluster_idle()` is
permanently `False` and the 60 s `AutostopEvent` tick resets `last_active_time` on every pass
(`sky/skylet/events.py:241-243`). `idle_minutes_to_autostop` therefore has no effect on any
server-mode deploy. The practical consequence of the correction: switching `wait_for` to `jobs`
would fix nothing, and is not part of this design.

---

## 2. Goal and non-goals

**Goal.** Every SkyPilot cluster carries a wall-clock deadline enforced ON the instance, armed
during `setup`, before the heavy installs, surviving the death of the orchestrator process.

**Non-goals** (other briefs own these): `ComputeProvider`'s ABC, the reaper's verdict tree, the
YAML config schema. Everything here is additive.

---

## 3. Architecture

Three pieces. Piece 1 is new, pieces 2 and 3 are edits to `create_instance`.

```
controller (dies at any time)              instance (must die anyway)
─────────────────────────────              ──────────────────────────
compute_deadline(...)  ──┐
ledger.record(provisional)│  ← F12 fix: durable BEFORE launch
sky.launch(              │
  setup = RENDER_ARM(deadline) + provision_script   ──► arm watchdog (first line of setup)
  down  = True,          │                                  │
  idle_minutes_to_autostop = ...)                           │  poll every 15 s
ledger.forget(provisional)│  ← success path only            ▼
                          ┘                            now >= deadline
                                                            │
                                            1. skylet autodown-now  → real terminate
                                            2. sudo shutdown -h now → halt (fallback)
                                            3. sudo shutdown -h +N  → kernel timer (arm-failure fallback)
```

### 3.1 `src/kinoforge/providers/skypilot/watchdog.py` (new)

Modelled on `providers/runpod/selfterm.py`: a template module with pure render functions and no
imports of anything heavier than `string.Template`. Three public names.

#### `compute_deadline(*, launch_epoch, max_lifetime_s, budget_usd, rate_usd_per_hr) -> float`

Pure, unit-testable, no clock read of its own.

```
lifetime_bound = launch_epoch + max_lifetime_s
budget_bound   = launch_epoch + (budget_usd / rate_usd_per_hr) * 3600.0   # only when rate > 0
deadline       = min(lifetime_bound, budget_bound)                        # budget_bound optional
```

Decisions baked in:

- **The deadline is measured from launch, not from boot.** It therefore covers the multi-minute
  provisioning window — which is precisely the F12 window where the controller can die with no
  durable record.
- **No `time_buffer_s` arithmetic.** `time_buffer_s` is a controller-side reap concept; folding it
  in here would make the instance-side backstop tighter than the configured policy and turn a
  backstop into a scheduler. At `Lifecycle()` defaults the deadline is `launch + 5 h`.
- **`rate_usd_per_hr <= 0` (unknown rate) drops the budget bound**, rather than producing a zero or
  infinite deadline. `budget_usd <= 0` likewise drops it: `Lifecycle.budget_usd` defaults to `0.0`
  and a zero budget must not mean "die immediately".
- A non-positive `max_lifetime_s` is a caller error; the function raises `ValueError` rather than
  rendering a script that kills the instance on the first tick.

#### `RENDER_WATCHDOG(*, poll_interval_s: float = 15.0, grace_before_halt_s: float = 600.0) -> str`

Renders the standalone python program that runs on the instance. Behaviour per tick:

1. Read the deadline from `$KF_WD_DIR/deadline` **every tick** (not once at startup). A setup
   re-run on cluster reuse rewrites that file, so the deadline is *replaced*, never stacked — this
   is half of the idempotency guarantee.
2. `now < deadline` → sleep and loop.
3. `now >= deadline` → fire, once:
   - **Stage 1 — skylet autodown-now (real terminate).** Resolve the node's own SkyPilot python
     from `~/.sky/python_path` (`sky/skylet/constants.py:68`, `SKY_PYTHON_PATH_FILE`) and run:

     ```python
     from sky.skylet import autostop_lib
     try:
         from sky.backends import cloud_vm_ray_backend as _b
         _backend = _b.CloudVmRayBackend.NAME
     except Exception:
         _backend = 'cloudvmray'
     autostop_lib.set_autostop(0, _backend,
                               autostop_lib.AutostopWaitFor.NONE, True)
     ```

     `wait_for=NONE` sets `ignore_idle_check=True` in `AutostopEvent`
     (`sky/skylet/events.py:220-232`), so the idle check that F1 showed is permanently `False` is
     bypassed entirely; `idle_minutes=0` makes the next ≤60 s tick fire `_stop_cluster`, and
     `down=True` routes it through the provisioner terminator — the instance AND its disk go away,
     and SkyPilot's own state stays consistent (a plain halt would leave sky believing the cluster
     is UP).

     The backend argument must be the runtime VALUE of `CloudVmRayBackend.NAME`
     (`sky/backends/cloud_vm_ray_backend.py:3087`), which is the string `'cloudvmray'` — not the
     class name `'CloudVmRayBackend'`. `_stop_cluster` (`sky/skylet/events.py:364`) compares the
     payload's backend field against that constant and `raise NotImplementedError`s on any
     non-match, so the class-name literal fails on every run (see "Live evidence" below). The code
     imports the constant on the instance and ties the payload to sky's own source of truth, with
     the literal string as a fallback for an import failure across a sky version bump.

     **No credential is embedded by kinoforge.** This path uses the cloud credentials SkyPilot
     itself already places on the head node for exactly this purpose — the same ones `_stop_cluster`
     uses on the normal autostop path. Signature verified at this pin
     (`autostop_lib.set_autostop(idle_minutes, backend, wait_for, down, hook=None, hook_timeout=None)`,
     `autostop_lib.py:165-170`).

     **Live evidence (2026-08-15, cluster `kinoforge-wd-6722c3f1`, 900 s deadline, run 3).** The
     on-instance watchdog log showed the expected sequence — `deadline reached; firing stage 1`
     followed by `stage 1 skylet autodown rc=0` — proving the autostop DECISION path (the
     `wait_for=NONE` / `idle_minutes=0` bypass of the permanently-False idle check) works exactly
     as designed. The instance nonetheless stayed `running`: the skylet's own `~/.sky/skylet.log`
     showed `_stop_cluster` raising `NotImplementedError` because the payload's backend string
     (`'CloudVmRayBackend'`) never matched `CloudVmRayBackend.NAME` (`'cloudvmray'`). Stage 1 has
     therefore never been able to terminate an instance, on any run to date — the earlier "first
     live AWS run" analysis below, which credited stage 1 with a successful-but-slow terminate,
     was itself observing this same silent no-op. Stage 2 was confirmed doing its job as designed:
     the local halt fired at +600 s and the instance transitioned to `stopped`, the exact
     stage-2-only outcome this design accepts as a fallback. Fixed by reading the NAME constant at
     runtime instead of hardcoding the class name (this section, and
     `kinoforge.providers.skypilot.watchdog._SKYLET_AUTODOWN_CODE`).
   - **Stage 2 — halt (fallback).** If the process is still alive `grace_before_halt_s` (600 s)
     after stage 1 — skylet missing, sky version drift, terminate API refusing — run
     `sudo shutdown -h now`, then `sudo halt -f`. Credential-free and local. Passwordless sudo is
     standard on SkyPilot's cloud images (the whole provisioning path depends on it).
4. Every stage is best-effort and swallows exceptions; the loop keeps running so a transient
   failure retries on the next tick.

**Why 600 s, not the original 120 s.** The first live AWS run (2026-08-15, cluster
`kinoforge-wd-13b6aaeb`, us-west-2) proved stage 1 firing was not enough by itself: the log showed
`stage 1 skylet autodown rc=0` — the autodown call succeeded — but stage 2 fired ~120 s later while
SkyPilot's teardown (`AutostopEvent` tick, ≤60 s, → `_stop_cluster` → provisioner terminate) was
still in flight, and the local halt killed the box mid-teardown. The instance ended up `stopped`,
not `terminated`: compute billing stopped but the EBS volume kept billing — exactly the outcome
stage 1 exists to avoid. The grace was raised to 600 s so a healthy stage-1 terminate has room to
finish before stage 2 is allowed to preempt it; stage 2 itself is unchanged — it still exists to
catch a genuinely wedged stage 1.

**Billing semantics of stage 2, stated plainly.** A halt is not a terminate:

| Cloud | after stage 1 | after stage 2 only |
|---|---|---|
| AWS | instance terminated, EBS deleted | `stopped` — EBS still billed |
| GCP | instance deleted, PD deleted | `TERMINATED` (stopped) — PD still billed |
| Lambda / Vast | instance released | VM halted, **still billed at full rate** |

So on rented-GPU clouds stage 2 alone does not stop the money. That is the honest limit of a
credential-free local action, and it is why stage 1 leads. Clusters that reach only stage 2 remain
discoverable via the pre-launch ledger row (§3.3) and are killable by
`pixi run -e live-skypilot kinoforge destroy --id <cluster>`.

#### `RENDER_ARM(*, deadline_epoch: float, poll_interval_s: float = 15.0, ...) -> str`

Renders the bash prelude that becomes the first lines of `Task.setup`:

```bash
KF_WD_DIR="${KF_WD_DIR:-$HOME/.kinoforge-watchdog}"
mkdir -p "$KF_WD_DIR"
printf '%s\n' "<deadline_epoch>" > "$KF_WD_DIR/deadline.tmp"
mv -f "$KF_WD_DIR/deadline.tmp" "$KF_WD_DIR/deadline"       # atomic replace
cat > "$KF_WD_DIR/watchdog.py" <<'KF_WD_EOF'
<RENDER_WATCHDOG output>
KF_WD_EOF
if [ -f "$KF_WD_DIR/pid" ] && kill -0 "$(cat "$KF_WD_DIR/pid")" 2>/dev/null; then
  echo "[kinoforge-watchdog] already armed (pid $(cat "$KF_WD_DIR/pid")); deadline refreshed"
else
  setsid nohup "${KF_WD_PYTHON:-python3}" "$KF_WD_DIR/watchdog.py" \
      >> "$KF_WD_DIR/watchdog.log" 2>&1 &
  echo $! > "$KF_WD_DIR/pid"
  echo "[kinoforge-watchdog] armed pid $(cat "$KF_WD_DIR/pid") deadline=<deadline_epoch>"
fi
```

- **Idempotent on cluster reuse.** Setup re-runs; the pid-file + `kill -0` guard means the second
  run refreshes the deadline file and spawns nothing. Two watchdogs never race.
- **`setsid`** detaches the watchdog from setup's process group, so it survives the setup shell
  exiting and any group-directed signal aimed at the launch.
- **Kernel poweroff backstop is UNCONDITIONAL (F3, final-review fix wave, 2026-08-15).** The
  original design scheduled `sudo shutdown -h +N` only on a failed spawn. Verified gap: the python
  daemon can die AFTER a successful arm (OOM-kill, crash) with zero enforcement left — `Task.setup`
  never re-runs on a live cluster, so there is no later re-arm to notice. The prelude now cancels
  any previous timer (`shutdown -c`) and reschedules `shutdown -h +N` on **every** arm, success or
  failure alike. `N` is no longer "minutes to deadline" — it is sized to
  `deadline_epoch + grace_before_halt_s + backstop_margin_s` (margin = one poll interval + the
  daemon's `run_command` subprocess timeout), so the kernel timer always fires strictly AFTER the
  daemon's own stage-1/stage-2 sequence would have finished. This makes it a pure backstop that
  never races a healthy daemon — it only fires when the daemon isn't there (or didn't spawn) to beat
  it to the punch. Cancelling first, every time, is what keeps repeated re-arms (cluster reuse
  across multiple `setup` runs) from stacking timers.
- **Privileged calls are indirected through `KF_WD_SUDO` (F2, final-review fix wave, 2026-08-15).**
  Both `shutdown -c` / `shutdown -h +N` in this prelude and the daemon's own stage-2 `shutdown -h
  now` / `halt -f` resolve the sudo binary from `KF_WD_SUDO` (default `sudo`), exported alongside
  `KF_WD_DIR` / `KF_WD_PYTHON`. This exists so unit tests can point it at a recording stub instead
  of ever invoking the real privileged binary — the original tests ran the real prelude verbatim,
  which would schedule/cancel a REAL poweroff on any host with passwordless sudo (harmless only
  because this container has none).
- **`KF_WD_DIR` / `KF_WD_PYTHON` / `KF_WD_SUDO` env overrides** exist so the unit test can run the
  real snippet twice in a temp directory with stub interpreter + stub sudo — no cloud, no mocking
  of the thing under test.

### 3.2 `create_instance` — arming and `down=True`

Both edits are inside `SkyPilotProvider.create_instance`
(`src/kinoforge/providers/skypilot/__init__.py:677-815`).

**Arming.** Today `task_config["setup"]` is set only when `spec.provision_script` is truthy. After
the change the key is **always** set, with the arming snippet first:

```python
setup_parts = [watchdog.RENDER_ARM(deadline_epoch=deadline, ...)]
if spec.provision_script:
    setup_parts.append(_strip_trailing_exec(spec.provision_script))
task_config["setup"] = "\n".join(setup_parts)
```

A server-less deploy (the CPU smoke) previously had no `setup` at all and now gets one containing
only the arming step. Arming ahead of `_strip_trailing_exec(...)` output is what satisfies "a
cluster that dies during a 20-minute pip install must still be covered".

**`down=True`.** New ctor arg `autodown: bool = True`, passed as `launch_kwargs["down"]`.

Verified at this pin that `down` and `idle_minutes_to_autostop` compose rather than conflict:
`sky/client/sdk.py:707-725` feeds both into a single `resource.override_autostop_config(down=...,
idle_minutes=..., wait_for=...)`, and the docstring states "If `--idle-minutes-to-autostop` is also
set, the cluster will be torn down after the specified idle time"
(`sdk.py:637-643`). On the skylet side `_stop_cluster` reads `autostop_config.down` and routes to
the terminating provisioner (`sky/skylet/events.py:270-298`).

Attached disks: autodown **terminates**, so the boot disk is deleted with the instance; autostop
(`down=False`) stops it and keeps billing the disk. No kinoforge workflow depends on
stop-and-restart onto the same disk — `stop_instance` is a documented no-op
(`providers/skypilot/__init__.py:849-858`) and F2 established that nothing consumes a stopped
cluster — so the safe default is `True`. The ctor arg is the escape hatch for a future workflow
that genuinely wants stop-restart; the YAML schema is deliberately untouched (Brief 5's territory).

`idle_minutes_to_autostop` keeps being passed. It is inert for server-mode (F1) but correct for the
one-shot configs (`skypilot-cpu.yaml`, `skypilot-gpu.yaml`) whose `run` terminates, and with
`down=True` those now autodown instead of autostopping.

### 3.3 Pre-launch durable record (F12)

`create_instance` writes a **provisional ledger row before `sky.launch`** and removes it on the
success path, letting the orchestrator's existing post-create `Ledger.record` write the final row.

```python
recorder.record_provisional(...)      # BEFORE sky.launch
raw = sky.launch(task, **launch_kwargs)
...
recorder.forget(cluster_name)         # success path only
return Instance(...)                  # orchestrator's on_instance_created records the real row
```

Row contents — enough for a later sweep to find and destroy the cluster:

| field | source |
|---|---|
| `id` | `cluster_name` (`spec.run_id`), which is the `sky down` handle |
| `provider` | `"skypilot"` |
| `cost_rate_usd_per_hr` | `spec.offer.cost_rate_usd_per_hr` — known before launch |
| `tags.kf_launch_phase` | `"launching"` |
| `tags.kf_cloud` | the pinned cloud (`self._clouds`) or `"auto"` |
| `tags.kf_run_id` | `spec.run_id` |
| `tags.kf_launched_at` | launch epoch |
| `tags.kf_deadline_epoch` | the watchdog deadline |

Choices:

- **Reuse `Ledger`, not a new journal file.** Every existing consumer — `kinoforge list`,
  `kinoforge destroy`, `kinoforge reap` — already reads the ledger, so an orphan becomes visible
  with no new plumbing. A dedicated journal would need read paths added to each of those, which is
  Brief 5's scope.
- **`forget` on success rather than an upsert.** `Ledger.record` appends; making it upsert-by-id
  would change behaviour for every provider. Forget-then-let-the-orchestrator-record keeps the
  change local and cannot produce a duplicate row.
- **The tunnel-failure path deliberately keeps the row.** When `_ssh_spawn` raises, the existing
  code best-effort `sky.down`s and raises `ProvisionFailed`. If that `down` fails the cluster is
  alive and orphaned — exactly the row's reason to exist — so `forget` is not called there.
- **Injected seam, no-op default.** `SkyPilotProvider(launch_recorder=...)`; default is a no-op so
  every existing test construction keeps working and the provider never reaches for a filesystem
  path it was not given. Production wiring lives in `_adapters.build_provider_for`, the same
  function that already pins `cfg.compute.cloud` onto the provider — so `cfg` (and therefore the
  configured store) is in hand and the config schema stays untouched.

### 3.4 Documentation correctness

The provider module docstring (`__init__.py:43-51`) advertises autostop as the SkyPilot cost model.
That is the load-bearing false claim identified by F1; it is replaced with the watchdog contract
plus the per-cloud stage-2 billing table. `test_ac4_create_instance_passes_idle_minutes_to_autostop`
keeps its assertion (the kwarg is still passed, and is still correct for one-shot configs) but its
docstring stops calling it the cost backstop.

---

## 4. Testing

### 4.1 Unit — no cloud

| # | Test | Bug it catches |
|---|---|---|
| U1 | `compute_deadline` returns `launch + max_lifetime_s` when no rate is known | buffer arithmetic creeping back in; boot-relative instead of launch-relative |
| U2 | `compute_deadline` returns the budget bound when `budget/rate < max_lifetime` | budget bound ignored, or applied as a max instead of a min |
| U3 | `compute_deadline` ignores the budget bound at `rate <= 0` / `budget <= 0`; raises on `max_lifetime_s <= 0` | zero-budget default rendering an already-expired deadline |
| U4 | generated setup contains the arming step, and its index precedes the first line of the provision script | arming placed after the 20-minute install |
| U5 | setup key is present even with no provision script | server-less deploys shipping unarmed |
| U6 | running the real arm snippet twice in a temp `KF_WD_DIR` with a stub `KF_WD_PYTHON` leaves exactly one live pid, unchanged across runs, and a deadline file holding the second run's value | two watchdogs racing on cluster reuse; deadline not refreshed |
| U7 | rendered watchdog `exec`'d against a fake clock + fake `subprocess`: no action before the deadline; stage-1 skylet command issued at the deadline; stage-2 `shutdown -h now` only after the grace window | watchdog firing early (kills healthy work) or never firing |
| U8 | `down=True` reaches `sky.launch` kwargs by default; `autodown=False` sends `down=False` | F2 regression |
| U9 | fake sky client and fake recorder share one call-sequence list; `record_provisional` appears before `launch` | F12 regression — record written after the launch returns |
| U10 | success path calls `forget`; the tunnel-failure path does not | orphan row deleted precisely when it matters |

U6 and U7 execute the real rendered artefacts rather than asserting on substrings — the lesson from
audit B4, where substring-presence tests let a mislabelled RunPod timer survive since `1be572d`
(`tests/providers/runpod/test_selfterm_reap_conditions.py` is the pattern being copied).

### 4.2 Live smoke — cheapest CPU SKU, AWS `us-west-2`

`tests/live/test_skypilot_watchdog_smoke.py`, opt-in via the project's existing live-test marker.

- Drive `SkyPilotProvider.create_instance` directly with a synthetic CPU offer and
  `Lifecycle(max_lifetime_s=300)`; `run_cmd = ["sleep", "3600"]` so the workload runs well past the
  deadline and the job queue never goes idle (the F1 shape).
- After launch returns, kill the tunnel handle and drop the provider reference — the client is gone;
  nothing in-process will ever destroy this cluster.
- Poll AWS directly every 30 s: `aws ec2 describe-instances`, matched on the SkyPilot cluster tag.
  **Pass condition: the instance's state is `shutting-down` or `terminated` in the EC2 API** — not
  merely absent from `sky status`. Record wall-clock from launch to termination.
- `finally`: `sky down --yes <cluster>` (idempotent when already gone), kill any surviving tunnel,
  and clear the ledger row. A test for a cost guardrail must not itself leak a cluster.
- Budget: cheapest CPU SKU is ~$0.01/hr; deadline 5 min + ≤60 s skylet tick + polling → **under
  $0.05**, EBS negligible against a terminate.

Live-spend rules per `CLAUDE.md`: the RED scaffold is committed before any spend, and
`pixi run preflight` runs first.

### 4.3 Smoke result

**GREEN on 2026-08-15, run 4** — `tests/live/test_skypilot_watchdog_smoke.py` → `1 passed` in
981 s (16:21).

| | |
|---|---|
| Cluster | `kinoforge-wd-02a20304` (EC2 `i-0b27bf5c8710c0b30`, `c6i.large`, us-west-2) |
| Deadline | `max_lifetime_s = 900`, armed at launch (epoch `1786847760.7`) |
| Workload | `sleep 3600` — never terminates, so SkyPilot autostop is inert by construction (F1) |
| Client | tunnel killed and every handle dropped after launch; `destroy` never called |
| Stage that fired | **Stage 1** — skylet autodown, a real provisioner terminate |
| Wall clock, deadline → EC2 `terminated` | ~50 s (detected on the first poll after the deadline) |
| Final state | `aws ec2 describe-instances` → `terminated`; `sky status` empty; `kinoforge list` → no instances, empty ledger |
| Cost | ~$0.12 across all four runs (`c6i.large` @ $0.0864/hr); run 4 alone ~$0.023 |

Four runs were needed. The three that failed each found a real defect, and all three are the kind
that only a live run can surface:

1. **Run 1** (`kinoforge-wd-13b6aaeb`, ~$0.033) — stage 1 fired `rc=0`, but 120 s later stage 2's
   halt preempted the in-flight terminate and the instance ended `stopped` (compute billing
   stopped, EBS still billing) instead of terminated. `grace_before_halt_s` raised 120 → 600 s
   (`9d923da6`). This also exposed that the smoke's EC2 filter used an exact `ray-cluster-name`
   match while SkyPilot tags `<cluster>-<8 hex>` — the oracle could never have matched.
2. **Run 2** (`kinoforge-wd-168d7225`, ~$0.019) — aborted at t+791 s, before the deadline, on a
   single transient `aws ec2 describe-instances` failure (`rc=255`, botocore XML parse error).
   The "raise on non-zero rc" hardening had no retry. Fixed with a bounded 3× retry; the teardown
   survivor check also had to learn to wait out the `stopping` → `terminated` transition
   (`d0b05ae1`).
3. **Run 3** (`kinoforge-wd-6722c3f1`, ~$0.037) — **the root cause of the stage-1 failure.** With
   the longer grace, stage 1 had time to be observed properly, and the skylet's own log showed:

   ```
   5.0 minute(s) since last active; threshold: 0 minutes. Stopping.
   AutostopEvent error: ... sky/skylet/events.py, line 364, in _stop_cluster
       raise NotImplementedError
   ```

   The autostop *decision* worked exactly as designed — `wait_for=NONE` plus `idle_minutes=0`
   bypassed the permanently-False idle check that F1 identified. The *dispatch* failed:
   `_stop_cluster` compares `autostop_config.backend` against
   `cloud_vm_ray_backend.CloudVmRayBackend.NAME`, whose value is `'cloudvmray'`, and this design
   specified the class name `'CloudVmRayBackend'`. Every non-matching backend falls through to
   `else: raise NotImplementedError`. Stage 1 had therefore never been able to terminate on any
   run; only stage 2's halt was killing the instance. The payload now imports the constant rather
   than hardcoding a string (`b9cb8edc`).

The lesson worth carrying: `rc=0` from the stage-1 command proves the *command* ran, not that the
terminate happened. The watchdog cannot see the skylet's asynchronous failure, which is precisely
why stage 2 exists — and why run 1's "stage 2 preempted stage 1" fix had to lengthen the grace
rather than remove the fallback.

---

## 5. Risks

| Risk | Mitigation |
|---|---|
| Sky's `autostop_lib.set_autostop` signature drifts on upgrade | Stage 1 is best-effort inside `try`; stage 2 halts regardless. Signature pinned by U7 and re-verified on any sky bump. |
| Passwordless sudo absent on some image | Stage 1 needs no sudo. Stage 2 tries `shutdown`, then `halt -f`; failure is logged to `watchdog.log`, which the tunnel-less debugging path can still reach via `sky logs`. |
| Deadline eats the provisioning window on a very slow GPU launch | Intentional — that window is the F12 failure window. Default `max_lifetime_s` is 5 h against a worst observed boot of ~30 min. |
| Halt-only clouds (Lambda / Vast) keep billing | Documented in §3.1 and in the module docstring; pre-launch ledger row keeps the cluster discoverable for `destroy`. |
| `watchdog.log` grows unbounded on a long-lived cluster | Log lines are emitted only on state changes and on fire, not per tick. |

---

## 6. Out of scope

- Reaper verdicts for a `kf_launch_phase=launching` row (Brief 5).
- `ComputeProvider` ABC changes so other providers get the same pre-launch seam (Brief 2).
- A YAML surface for `autodown` / deadline overrides (Brief 5).
- F11's broken warm-attach endpoint replay for skypilot — noted by the verification doc, untouched
  here.
- **`deploy()`'s missing F12 protection (F4, final-review fix wave, 2026-08-15) — re-flagged, still
  unwired.** The review confirmed the gap this doc already called out below: `deploy()`
  (`core/orchestrator.py:1520`, the entry point `cli/_commands.py:223` calls for the one-shot
  `kinoforge deploy` command — arguably the command most likely to eat a mid-launch Ctrl-C) takes no
  `store` or `state_dir` parameter, so it has no way to build the same `Ledger` the CLI's
  `SessionContext.ledger()` builds (which honours `cfg.store` / sidecar / `--state-dir`
  precedence). Wiring `set_launch_ledger` here without a store would mean either (a) adding a
  `store` parameter to `deploy()`'s public signature, or (b) constructing a store internally from a
  hardcoded default path — which risks silently writing the provisional ledger row to a location
  `kinoforge list` / the sweeper never reads, i.e. fake protection that looks wired but isn't. The
  fix-wave instructions were explicit: if closing this requires a signature change, stop and report
  rather than make it — so it was left as-is. Closing it for real needs a maintainer decision on
  whether `deploy()` gains a `store: ArtifactStore | None = None` parameter (mirroring
  `deploy_session`) with `_cmd_deploy` passing `ctx.store()`. The final re-review noted a third
  option: `deploy()` already accepts `provider: ComputeProvider | None` and `_resolve_provider`
  returns an injected provider as-is, so `_cmd_deploy` could build the provider, install the ledger,
  and pass it in — but that path skips the heartbeat wiring at `orchestrator.py:193-198` and would
  silently drop `heartbeat_mode`, so it is not a free win either.
- **The reconciler can forget an in-flight `launching` row (narrow F12 residue).** With `"skypilot"`
  now in `_RECONCILABLE_PROVIDERS`, a `kinoforge list` run in another process *while* a `sky.launch`
  is still provisioning can see the not-yet-registered cluster, treat the provisional row as dead,
  and forget it — reopening the exact window the row exists to cover, though only for the seconds a
  concurrent `list` overlaps a launch. `_reconcile_dead_ledger_entries` has no `kf_launch_phase`
  guard; adding one (skip rows whose phase is `launching` and whose `kf_launched_at` is younger than
  `boot_timeout_s`) is the natural fix and belongs with Brief 5's reaper work.

**Implementation deviation (Task 4):** §3.3 sketched an injected `launch_recorder` constructor
seam wired from `_adapters.build_provider_for`. The task-4 brief instead specified a duck-typed
`provider.set_launch_ledger(ledger)` setter, installed by `deploy_session` in
`core/orchestrator.py` (immediately after `resolved_provider = _resolve_provider(cfg, provider)`)
via `getattr(resolved_provider, "set_launch_ledger", None)`. Net effect is the same — an opt-in
seam that is a no-op unless a ledger is installed — but the wiring point is `deploy_session`, not
`_adapters.build_provider_for`, and the seam is a post-construction setter rather than a
constructor kwarg. `deploy_session` is the CLI path and the one that matters; the bare `deploy()`
entry point (no `store` in scope there) is left unwired, matching §3.3's use of the configured
store.

**F1 reconciliation (final-review fix wave, 2026-08-15):** `cli/_reconcile.py`'s
`_RECONCILABLE_PROVIDERS` now includes `"skypilot"` alongside `"runpod"`. Without it, a `sky.launch`
that raises (e.g. `ResourcesUnavailableError`) after the F12 provisional row was written left a
permanent ghost ledger row — nothing ever forgot it, since auto-reconcile was runpod-only.
`SkyPilotProvider.get_instance()` is backed by `sky_client.status()`, the same cross-process
authoritative signal RunPod's API provides, so a `KeyError` reliably means the cluster never came
up (or is long gone) and the row is safe to forget.
