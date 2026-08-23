# Cloud-layer findings verification — 2026-08-15

Read-only verification of an external review of the kinoforge cloud / SkyPilot layer.
No remediation was performed. Every verdict below is against HEAD `67627cd0`
(`fix(runpod): make the selfterm cap say what it does (audit B4)`), working tree clean.

**Pinned SkyPilot version:** `skypilot-0.12.3.post1` (`pixi.lock`; `pixi.toml:195`
declares `skypilot = { version = "*", extras = ["gcp", "aws", "vast", "lambda"] }`,
so the lock is the only pin). Autostop claims below were verified against the
**installed source** at
`.pixi/envs/live-skypilot/lib/python3.12/site-packages/sky/`, not from docs or memory.

---

## Summary table

| # | Finding | Verdict | Severity (my call) |
|---|---------|---------|--------------------|
| F1 | Autostop inert for server-mode deploys | **CONFIRMED** (job mechanism); **REFUTED** (ssh mechanism) | **Critical** |
| F2 | Autostop stops rather than terminates | **CONFIRMED** | Medium (moot while F1 holds) |
| F3 | Cost guardrails degrade to silent no-ops | **CHANGED** — heartbeat path works; the reaper is unreachable for a different reason | **Critical** |
| F4 | Rate cap does not bind at launch | **CONFIRMED** | High |
| F5 | ComputeConfig / InstanceSpec are vendor-dialect unions | **CONFIRMED**; no validation warns | Medium |
| F6 | No `region` in ComputeConfig; `_region` unreachable from config | **CONFIRMED** | High |
| F7 | No secret scanning at the commit boundary; three regex lists disagree | **CONFIRMED** | High |
| F8 | Transcript hook user-scoped, guard fails open, nothing installs it | **CONFIRMED** | Medium |
| F9 | Onboarding contradicts security artifacts; `skypilot-minimal.json` never validated | **CONFIRMED** (repo states it outright) | Medium |
| F10 | Scrub discipline inconsistent | **CONFIRMED** and **wider than reported** | High |
| F11 | Provider conventions leak; two endpoint shapes from one provider | **CONFIRMED** | High |
| F12 | Remote-resource state is process-local; no durable pre-launch record | **CONFIRMED** | **Critical** |

---

## F1 — SkyPilot autostop is inert for server-mode deploys

**Verdict: CONFIRMED for the job-queue mechanism. REFUTED for the SSH mechanism at this pinned version.**

### Current code

`src/kinoforge/providers/skypilot/__init__.py:711`:

```python
autostop_minutes: int = int(spec.lifecycle.idle_timeout_s / 60.0)
```

`src/kinoforge/providers/skypilot/__init__.py:764-780`:

```python
        if spec.provision_script:
            task_config["setup"] = _strip_trailing_exec(spec.provision_script)
        if spec.run_cmd:
            task_config["run"] = " ".join(shlex.quote(c) for c in spec.run_cmd)

        task = sky.Task.from_yaml_config(task_config)
        cluster_name: str = spec.run_id or "skypilot-cluster"
        launch_kwargs: dict[str, Any] = {
            "cluster_name": cluster_name,
            "idle_minutes_to_autostop": autostop_minutes,
        }
        if self._retry_until_up:
            launch_kwargs["retry_until_up"] = True
        raw = sky.launch(task, **launch_kwargs)
```

### Mechanism 1 — job queue (CONFIRMED)

`sky/skylet/events.py:192-252`, `AutostopEvent`:

```python
        ignore_idle_check = (
            autostop_config.wait_for == autostop_lib.AutostopWaitFor.NONE)
        is_idle = True
        if not ignore_idle_check:
            if not job_lib.is_cluster_idle(
            ) or managed_job_state.get_num_alive_jobs() or (
                    autostop_config.wait_for
                    == autostop_lib.AutostopWaitFor.JOBS_AND_SSH and
                    autostop_lib.has_active_ssh_sessions()):
                is_idle = False
        ...
        else:
            autostop_lib.set_last_active_time_to_now()
            minutes_since_last_active = -1
```

`sky/skylet/job_lib.py:981-994`:

```python
def is_cluster_idle() -> bool:
    """Returns if the cluster is idle (no in-flight jobs)."""
    ...
    in_progress_status = [
        status.value for status in JobStatus.nonterminal_statuses()
    ]
```

`Task.run` is submitted as a cluster job and stays in a non-terminal status for as
long as the process lives. The kinoforge sky path puts the **video server** there —
`examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml` sets
`engine.diffusers.server_cmd` to `env … python -m kinoforge.engines.diffusers.servers.wan_t2v_server`,
which becomes `RenderedProvision.run_cmd` and then `spec.run_cmd` and then
`task_config["run"]`. That job never terminates, so `is_cluster_idle()` is
permanently `False`, the `else` branch resets `last_active_time` every 60 s tick,
and the autostop deadline is never reached. **The `idle_minutes_to_autostop` value
kinoforge passes has no effect on any server-mode deploy.**

### Mechanism 2 — SSH sessions (REFUTED at 0.12.3.post1)

The default is indeed `jobs_and_ssh` — `sky/skylet/autostop_lib.py:117`:

```python
DEFAULT_AUTOSTOP_WAIT_FOR: AutostopWaitFor = AutostopWaitFor.JOBS_AND_SSH
```

But the check is PTY-based, not connection-based —
`sky/skylet/autostop_lib.py:236-279`:

```python
def has_active_ssh_sessions() -> bool:
    """Check if any PTY traces back to sshd in the process tree."""
    ...
            if terminal and terminal.startswith('/dev/pts/'):
                pts_to_pid.setdefault(terminal, proc.info['pid'])
        ...
        for terminal, pid in pts_to_pid.items():
            ...
                for parent in psutil.Process(pid).parents():
                    if parent.name() == 'sshd':
                        return True
```

kinoforge's tunnel allocates **no PTY** — `providers/skypilot/__init__.py:369-382`
spawns `ssh -N -T … -L …`; `-T` explicitly disables pseudo-tty allocation and `-N`
runs no remote command. No `/dev/pts/*` entry is created, so
`has_active_ssh_sessions()` returns `False` for kinoforge's tunnel. The reviewer's
second mechanism does not apply here. (It would apply to an interactive `sky ssh`
session an operator opens by hand.)

This does not change the outcome — mechanism 1 alone is sufficient — but it matters
for remediation: switching `wait_for` to `jobs` would fix nothing.

### Blast radius

- Every sky config that runs a server: `examples/configs/skypilot-lambda-comfyui.yaml`,
  `skypilot-lambda-diffusers-flashvsr-upscale.yaml`, `skypilot-vast-diffusers-flashvsr-upscale.yaml`.
  (`skypilot-cpu.yaml` / `skypilot-gpu.yaml` run one-shot commands and DO autostop
  correctly — their `run` terminates.)
- `tests/providers/test_skypilot.py:386 test_ac4_create_instance_passes_idle_minutes_to_autostop`
  pins the current mapping and asserts the int type. Any fix that stops passing the
  kwarg breaks this test; it currently certifies a no-op.
- The provider module docstring (`__init__.py:43-51`) advertises autostop as the
  SkyPilot cost model. That paragraph is the load-bearing false claim.

**Severity: Critical.** This is the only provider-side cost backstop on SkyPilot and
it does not fire for the exact workload shape the layer exists to run.

---

## F2 — Autostop stops rather than terminates

**Verdict: CONFIRMED.**

`sky/client/sdk.py:568-575` shows `launch` accepts `down: bool = False`.
`providers/skypilot/__init__.py:774-779` (quoted in F1) builds `launch_kwargs` with
`cluster_name`, `idle_minutes_to_autostop`, and optionally `retry_until_up` — no
`down`. Repo-wide there is no `down=True` and no call to `sky.autostop` anywhere:

```
$ rg -n 'idle_minutes_to_autostop|down=True' src tools tests --type py
src/kinoforge/providers/skypilot/__init__.py:776   "idle_minutes_to_autostop": autostop_minutes,
tests/providers/test_skypilot.py                    (assertions on that kwarg only)
```

So a cluster that *did* autostop would be STOPPED, keeping its EBS/persistent disk
billed. `_SKY_STATUS_MAP` (`__init__.py:385-393`) maps both `STOPPED` and
`AUTOSTOPPING` to kinoforge status `"stopped"`, and `_cluster_record_to_instance`
still returns it from `list_instances()` — so a stopped cluster stays visible in
`kinoforge list` rather than vanishing, which is the one thing working in favour here.

`stop_instance` is a documented no-op (`__init__.py:849-858`), so kinoforge itself
never produces a stopped cluster; only autostop could.

### Blast radius

Nothing consumes a stopped-cluster state today, precisely because F1 means autostop
never fires. Fixing F1 without fixing F2 converts an "instance still billing" leak
into a "disk still billing" leak.

**Severity: Medium.** Real, but strictly downstream of F1 — it cannot bite until F1 is fixed.

---

## F3 — Cost guardrails degrade to silent no-ops on SkyPilot

**Verdict: CHANGED.** Three of the four sub-claims are confirmed as written; the
central causal claim (heartbeat-based reaping cannot fire because
`HEARTBEAT_SUBSTRATE_MISSING` is non-destructive) is **REFUTED on the normal path**.
The conclusion — nothing reaps a SkyPilot cluster automatically — still holds, but for
a different and more interesting reason.

### Confirmed as written

`providers/skypilot/__init__.py:849-858` and `:902-908`:

```python
    def stop_instance(self, instance_id: str) -> None:
        """No-op for SkyPilot: use destroy_instance or rely on autostop.
        ...
        # SkyPilot clusters are either UP or torn down; no intermediate pause.

    def heartbeat(self, instance_id: str) -> None:
        """No-op: SkyPilot manages cluster liveness via autostop.
        ...
        # Autostop is set at launch time; no heartbeat mechanism is needed.
```

`core/interfaces.py:254-272` (`last_heartbeat` → `None`), `:274-288`
(`probe_runtime` → `None`), `:290-309` (`set_heartbeat_endpoint` → `pass`). SkyPilot
inherits all three.

`core/heartbeat_endpoints.py:89`:

```python
_HEARTBEAT_SUPPORTED: frozenset[str] = frozenset({"local", "runpod"})
```

`core/reaper.py:77-83` — `HEARTBEAT_SUBSTRATE_MISSING` is in
`DEFAULT_STRICT_VERDICTS`, not in `DEFAULT_APPLY_POLICY` (`:63-75`), so it is
non-destructive. Confirmed.

`_adapters.py:185-189` hard-rejects any non-`none` `heartbeat_mode` on skypilot:

```python
    if provider == "skypilot":
        raise ValidationError(
            f"skypilot heartbeat substrate ships in B5b "
            f"(compute.heartbeat_mode={mode!r}); set to 'none' for now"
        )
```

### Where the finding is wrong

The `HEARTBEAT_SUBSTRATE_MISSING` gate in `core/reaper.py:446-452` only fires when
the ledger row is **missing** heartbeat fields:

```python
    if hb_tick is None or hb is None or heartbeat_interval_s is None:
        provider_kind = entry.get("provider_kind") or entry.get("provider")
        if provider_kind is not None and not provider_heartbeat_supported(
            str(provider_kind)
        ):
            return Verdict.HEARTBEAT_SUBSTRATE_MISSING
```

But `HeartbeatLoop._tick_once` writes both fields for **every** provider —
`core/heartbeat_loop.py:236-285`:

```python
            self._provider.heartbeat(self._instance_id)
            last_hb = self._provider.last_heartbeat(self._instance_id)
            ...
            if last_hb is None:
                last_hb = now
            extra: dict[str, float | int | str | None] = {
                "heartbeat_thread_tick": now,
            }
            ...
            self._ledger.touch(
                self._instance_id,
                last_heartbeat=last_hb,
                **extra,
            )
```

The provider no-ops are absorbed by the orchestrator-clock fallback. And the loop
*does* start on skypilot: `HeartbeatIntervalRequiredCheck`
(`validation/checks/heartbeat.py:19-78`) is a STATIC ERROR with an `auto_fix` that
sets `heartbeat_interval_s: 30`, and `load_config` runs `validate_for_load` →
`_run_gated` → `_run_with_autofix` on STATIC (`validation/__init__.py:199-203`,
`core/config.py:1570-1572`). So even the two FlashVSR sky configs that omit the key
get 30 s injected at load. `orchestrator.py:1221-1227` then builds `_start_heartbeat`
because `_hb_interval > 0` and the engine `requires_compute`.

Net: SkyPilot ledger rows carry `last_heartbeat` and `heartbeat_thread_tick`.
`HEARTBEAT_SUBSTRATE_MISSING` does **not** fire on them, and `LIVE` / `IDLE_REAP` /
`ORPHAN_REAP` / `OVERAGE_REAP` classification all work.

### The question: what actually destroys a cluster if the orchestrator is killed mid-session?

Traced concretely:

1. **Nothing in-cluster.** Autostop is inert (F1). There is no SkyPilot analogue of
   RunPod's in-pod `selfterm.py` watchdog — `providers/skypilot/` contains only
   `__init__.py` and `vast_compat.py`.
2. **Nothing in-process.** The tunnel handle and the heartbeat thread both die with
   the process (F12).
3. **The ledger row survives** — written by `_record_then_install`
   (`orchestrator.py:1180-1202`), so a later `kinoforge reap` / `sweeper` can see it.
4. **`classify` would return the right verdict.** With the heartbeat thread dead, the
   sentinel goes stale, and `reaper.py:499-507` measures `grace_after_session_s`
   (default 1800 s) from `session_end` → `ORPHAN_REAP`. Independently,
   `reaper.py:426-427` returns `OVERAGE_REAP` once `pod_age > max_lifetime_s`,
   *before* any heartbeat gate is consulted.
5. **But `ORPHAN_REAP` is not in the default policy.** `reaper.py:63-75` — acting on
   it requires `--include-orphans` (`policy_from_cli_flags`, `:86-110`).
   `OVERAGE_REAP` *is* in the default set, so `kinoforge reap --apply` does destroy a
   SkyPilot cluster past `max_lifetime` (90 min in the FlashVSR configs).
6. **And the sweep cannot reach the provider from the default env.** `sky` is only
   installed in the `live-skypilot` feature env (`pixi.toml:191-195`). In the default
   env, `_get_sky()` (`providers/skypilot/__init__.py:86-101`) raises `KinoforgeError`
   on the first `list_instances()`, and `reaper_actor.sweep` catches it
   (`reaper_actor.py:511`, `:547`, `:553`) and marks every entry for that provider
   `UNROUTABLE`. `UNROUTABLE` never destroys (`:603-613`).

**Answer:** nothing automatic. The only thing that destroys an orphaned SkyPilot
cluster is an operator running `pixi run -e live-skypilot kinoforge reap --apply`
(or `sweeper start` in that same env) **after** `max_lifetime_s` has elapsed. A
`kinoforge reap --apply` in the default env silently reports `UNROUTABLE` and leaves
the cluster billing. The `--no-reuse` teardown path in the orchestrator is the only
non-manual teardown, and it dies with the process.

### On the config comments

The `heartbeat_interval_s: 30  # required when warm_reuse_auto_attach=true` comment
in `skypilot-gpu.yaml:54`, `skypilot-cpu.yaml:59`, `skypilot-lambda-comfyui.yaml:62`
is **accurate** — `HeartbeatIntervalRequiredCheck` genuinely requires it. The
reviewer read it as a false claim; it is not. (Note the finding also says "both
`skypilot-*.yaml`" — there are five such configs now; three set the key, two do not.)

### Blast radius

- Every SkyPilot run in the default pixi env is unreapable.
- `tests/core/test_runtime_probe.py` asserts the inherited `probe_runtime` → `None`.
- `docs/warm-reuse.md` and the sweeper tables encode the `HEARTBEAT_SUBSTRATE_MISSING`
  behaviour for skypilot — those docs are describing a path that the auto-fix
  actually bypasses, so they are stale in the same direction as this finding.

**Severity: Critical.** The reviewer's conclusion is right; the mechanism they named
is not the one to fix. Fixing `_HEARTBEAT_SUPPORTED` would change nothing.

---

## F4 — The rate cap does not bind at launch

**Verdict: CONFIRMED. Nothing re-checks realized cost post-launch.**

`max_usd_per_hr` is consumed in exactly one place — `core/offers.py:38`:

```python
        if o.mode == "pod" and o.cost_rate_usd_per_hr > reqs.max_usd_per_hr:
            continue
```

That filters the **catalog** returned by `find_offers`. The launch request
(`providers/skypilot/__init__.py:713-772`) carries `image_id`, `accelerators`,
`disk_size`, `use_spot`, `region`, `cloud`/`any_of` — no price constraint of any
kind. The in-repo comment the reviewer cites is at `:748-753` and is accurate:

```python
            # Pin the LAUNCH cloud to the operator's compute.cloud set. The
            # _clouds filter only narrows find_offers' CATALOG enumeration; sky's
            # optimizer otherwise still launches on the globally-cheapest cloud
            # for the accelerator (observed 2026-07-07: a compute.cloud=["vast"]
            # config provisioned a Lambda A100 at $1.99, defeating the vast pin
            # and the price cap). One cloud → ``cloud``; several → ``any_of``.
```

The cloud pin was the fix applied at the time; the **price** cap was not fixed and
still does not travel to the launch.

Post-launch: `create_instance` returns
`cost_rate_usd_per_hr=spec.offer.cost_rate_usd_per_hr` (`:812-814`) — the *catalog*
rate for the offer kinoforge picked, not the rate SkyPilot's optimizer actually
realized. `_cluster_record_to_instance` (`:415-436`) does not populate cost at all,
and `orchestrator.py:1631-1640` documents that `cost_rate_usd_per_hr` is deliberately
NOT refreshed from the polled record. `kinoforge status` refreshes the rate from the
live provider only on the RunPod path (`core/lifecycle.py:402-406` comment names
`pod.costPerHr`). There is no budget-ceiling comparison against a realized rate
anywhere in `core/cost.py` or `core/orchestrator.py`.

### Blast radius

- Ledger `cost_rate_usd_per_hr`, `est_spend`, `kinoforge list`, and the cost
  dashboard all report the catalog rate for SkyPilot. Under-reporting is silent.
- `lifecycle.budget` in every sky config (`budget: 2.0`, `budget: 0.10`) is computed
  from that same wrong rate.
- `filter_offers` is shared with RunPod and Modal; RunPod's create is SKU-exact so
  the gap is SkyPilot-specific.

**Severity: High.** A cap that filters a catalog but not a launch is a cap the
operator believes in and does not have.

---

## F5 — ComputeConfig and InstanceSpec are unions of vendor dialects

**Verdict: CONFIRMED. No validation warns when an ignored field is set.**

### The documented-as-ignored fields

`core/config.py:778-783`:

```python
        cloud: Phase 53 Stage C — optional list of sky cloud names
            ...
            behaviour — sky considers every enabled cloud and picks by
            price. Ignored by non-skypilot providers.
```

`core/config.py:794-800`:

```python
    # 2026-07-03: RunPod host-pool pin. "any" = historical cloudType ALL
    ...
    # Ignored by non-runpod providers.
    cloud_type: Literal["any", "secure", "community"] = "any"
```

`core/interfaces.py:164-187` documents `image_build_script` / `runtime_provision_script`
as Modal-only ("RunPod ignores both"), `restart_policy` as RunPod-schema-probed, and
`cloud_type` in RunPod host-pool terms. `capacity_wait_s` lives on `Lifecycle`
(`interfaces.py:91-93`), documented purely in RunPod terms:

```python
    #: Max seconds to keep retrying create on a RunPod capacity miss before
    #: giving up (2026-07-07). 0 = fail on the first miss.
    capacity_wait_s: float = 300.0
```

### Counts

`InstanceSpec` has 17 fields. Fields each provider actually reads (from a
`spec\.[a-z_]+` sweep of each provider module):

| Provider | Reads | Ignores |
|---|---|---|
| runpod | `cloud_type, diagnostic_env, env, image, lifecycle, offer, ports, provision_script, restart_policy, run_id, tags, volume_gb, volume_mount` | `image_build_script, runtime_provision_script, run_cmd, spot` (4) |
| skypilot | `env, image, lifecycle, offer, provision_script, run_cmd, run_id, spot, tags` | `ports, volume_gb, volume_mount, image_build_script, runtime_provision_script, diagnostic_env, restart_policy, cloud_type` (8) |
| modal | `env, image, image_build_script, lifecycle, offer, provision_script, run_cmd, run_id, runtime_provision_script, tags, volume_mount` | `ports, volume_gb, spot, diagnostic_env, restart_policy, cloud_type` (6) |
| local | `tags` | 16 |

**Counting the three real cloud providers: 10 of 17 `InstanceSpec` fields are ignored
by at least one provider** (`ports, volume_gb, volume_mount, image_build_script,
runtime_provision_script, run_cmd, spot, diagnostic_env, restart_policy, cloud_type`).
Only `tags` is read by all four. `ComputeConfig` has 9 fields; `cloud` is
skypilot-only, `cloud_type` runpod-only, and `heartbeat_mode` is rejected outright on
skypilot (`_adapters.py:185-189`) — **3 of 9**.

Notable: `spec.run_cmd` is ignored by RunPod (it bakes the run into the provision
script's trailing `exec`) — which is the exact convention that forces
`_strip_trailing_exec` in F11.

### Validation

The registered check inventory is:

```
providers/runpod/__init__.py:1411      RunPodCapacityHintCheck
providers/skypilot/__init__.py:958     SkyPilotCloudPinSupportedCheck
validation/checks/custom_nodes.py:39   CustomNodeSHAReachableCheck
validation/checks/heartbeat.py:19      HeartbeatIntervalRequiredCheck
validation/checks/image.py:49          ImageReachableCheck
validation/checks/ledger.py:48         LedgerStaleRowsCheck
validation/checks/lifecycle.py:26      IdleTimeoutVsHeartbeatCheck
validation/checks/lifecycle.py:74      GraceAfterSessionTooTightCheck
validation/checks/models.py:53         ModelRefReachableCheck
validation/checks/upscale.py:18        SeedVR2ExtrasPendingCheck
```

None of these cross-references a field against `cfg.compute.provider`.
`SkyPilotCloudPinSupportedCheck` validates *membership* of `compute.cloud`
(`__init__.py:965-967` gates on `cfg.compute.cloud is not None`) but never asks
whether the provider is actually skypilot. So `provider: runpod` + `cloud: ["lambda"]`
passes validation clean and the `cloud` list is silently discarded. Same for
`provider: skypilot` + `cloud_type: secure`.

**Severity: Medium.** Not a live money leak; a reliable source of "I set the thing and
it did nothing" incidents. The silent-discard of `cloud` under a non-skypilot provider
is the sharpest edge.

---

## F6 — No `region` field in ComputeConfig; `_region` unreachable from config

**Verdict: CONFIRMED.**

`ComputeConfig` fields (`core/config.py:786-800`): `provider, image, mode,
requirements, lifecycle, heartbeat_mode, warm_reuse_auto_attach, cloud, cloud_type`.
No `region`. The example config says so in as many words —
`examples/configs/skypilot-gpu.yaml:38-40`:

```yaml
  # excluded). The smoke test pins region us-central1 explicitly; this
  # YAML does not (ComputeConfig has no region field today).
```

`SkyPilotProvider.__init__` accepts `region` (`providers/skypilot/__init__.py:512`)
and applies it at `:746-747`:

```python
            if self._region:
                resources["region"] = self._region
```

But the only config-driven construction path is `_adapters.py:117-129`:

```python
    provider = registry.get_provider(cfg.compute.provider)()
    if cfg.compute.provider == "skypilot" and cfg.compute.cloud is not None:
        from kinoforge.providers.skypilot import SkyPilotProvider
        ...
        provider._clouds = list(cfg.compute.cloud)
    return provider
```

Only `_clouds` is injected. The registry factory is
`registry.register_provider("skypilot", lambda: SkyPilotProvider())`
(`providers/skypilot/__init__.py:930`) — zero args, so `_region` is `None` and
`_retry_until_up` is `False` on every config-driven launch.

**No config path reaches `_region`.** The only callers that set it are
`tests/live/test_skypilot_live.py:356` (`region="us-west1"`) and the offline unit
tests. `_retry_until_up` is in the same position — dead from config, despite its
docstring (`:540-546`) calling it "Required when spot capacity is bursty (the typical
case for preemptible GPUs)".

This directly contradicts the standing project rule that region must be pinned on
every cloud (default Oregon; `sky` was observed picking `asia-southeast1`).

### Blast radius

- `tests/providers/test_skypilot.py:575 test_ac4_create_instance_region_lands_in_resources_when_set`
  and `:598 …_omits_region_when_unset` pin the provider-level behaviour, which is
  correct — the gap is purely the missing config→constructor wire.
- Adding `region` to `ComputeConfig` requires touching `_adapters.build_provider_for`
  only; the provider side is already done.

**Severity: High.** Two constructor knobs that the docstrings describe as essential
are unreachable from any YAML. The region one has a documented history of landing
launches in the wrong hemisphere.

---

## F7 — No secret scanning at the commit boundary

**Verdict: CONFIRMED. The three lists do NOT currently agree.**

`.pre-commit-config.yaml` hooks in full: `ruff`, `ruff-format`, `mypy`,
`check-merge-conflict`, `check-added-large-files`, `check-toml`. No secret scan, no
`detect-secrets`, no `gitleaks`.

### The three lists

**`tools/_redact.py:38-43` — 5 patterns:**

```python
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b")),
    ("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
]
```

**`~/.claude/hooks/redact_secrets.py:38-55` — 13 patterns** (the 5 above, plus
`aws_access_key` `\bAKIA[A-Z0-9]{16}\b`, `github_token`, `github_app`,
`replicate_token`, `runway_key`, `slack_token`, `jwt`, `private_key_pem`).

**`tests/providers/conftest_runpod.py:191-203` — 7 patterns** (the 5 above, plus
`aws_access_key` as `\b(?:AKIA|ASIA)[0-9A-Z]{16}\b` and `pem_private_key` as a
full BEGIN…END span).

### Where they disagree

1. **`tools/_redact.py` is the weakest of the three** and is the one used by
   in-repo tooling. It catches **no AWS key and no PEM private key**. A recording
   proxy or tool output routed through it leaks both.
2. **The AWS pattern differs between the two lists that have one.** The hook matches
   `AKIA` only; `conftest_runpod.py` matches `AKIA|ASIA`. **STS temporary
   credentials (`ASIA…`) are invisible to the user-scope hook** — and STS creds are
   exactly what a SkyPilot instance-profile session produces.
3. **The PEM patterns differ.** Hook: `-----BEGIN [A-Z ]+PRIVATE KEY-----` (marker
   only, leaves the key body). conftest: full BEGIN→END span (redacts the body).
   The hook's is the weaker of the two.
4. **Only one pairing is enforced.** `tests/test_redact_hook_parity.py` asserts
   hook ⊇ `tools/_redact.py`. That passes today. **Nothing checks
   `conftest_runpod.py` against either list**, so its two extra patterns can drift
   or be deleted without a test noticing — and, more importantly, its stronger
   patterns are never propagated back.

### Blast radius

- `tools/_redact.py` is imported by the recording/proxy tooling; anything it scrubs
  is under-scrubbed for AWS and PEM.
- `tests/providers/conftest_runpod.py:294-353` runs a leak assertion over fixture
  recordings with a hard "Update `_CREDENTIAL_PATTERNS` … to cover this shape"
  failure message — so the test-fixture path is the best-defended of the three,
  which is backwards.

**Severity: High.** Three lists, one enforced pairing, and the weakest list is the
one wired into repo tooling. The `ASIA` gap is concrete and SkyPilot-relevant.

---

## F8 — The transcript hook is user-scoped and its guard fails open

**Verdict: CONFIRMED. Nothing in the repo installs the hook for a fresh clone.**

`tests/test_redact_hook_parity.py:32` and `:65-72`:

```python
HOOK_PATH = Path.home() / ".claude" / "hooks" / "redact_secrets.py"
...
    if not HOOK_PATH.exists():
        if os.getenv("KINOFORGE_REQUIRE_REDACT_HOOK") == "1":
            pytest.fail(
                f"KINOFORGE_REQUIRE_REDACT_HOOK=1 but hook not installed at {HOOK_PATH}"
            )
        pytest.skip(f"redact hook not installed at {HOOK_PATH}")
```

`KINOFORGE_REQUIRE_REDACT_HOOK` appears nowhere else in the repo — not in `pixi.toml`
tasks, not in `.pre-commit-config.yaml`, not in CI config. So the default behaviour
everywhere except a hand-configured shell is **skip**. The hook file itself is
user-scope (`~/.claude/hooks/`), outside the repo, and there is no installer script
for it (`tools/local_hooks/install.sh` is explicitly deprecated per project memory).

A fresh clone therefore gets: no hook, a green test suite, and no signal that the
redaction backstop is absent.

Sidebar observed during this audit: the installed hook produces **false-positive
redactions of benign identifiers**. During verification, `rg` output had
`idle_minutes_to_autostop`, `SkyPilotProvider(`, `skypilot-minimal`, and
`class …Check:` all replaced with a literal `n` in the tool output. Every code quote
in this document was therefore re-verified via `Read` or a Python reader rather than
`rg`. This is a usability defect in the hook, not a security one, but it makes
grep-driven auditing unreliable and is worth logging alongside F7/F8.

**Severity: Medium.** The hook is a defence-in-depth layer, not the primary control.
But a guard whose default is "silently skip" provides confidence without coverage.

---

## F9 — Onboarding contradicts the security artifacts

**Verdict: CONFIRMED. `skypilot-minimal.json` has never been attached, let alone validated.**

`.env.example:84-91`:

```
#   aws iam create-user --user-name kinoforge-runner
#   for P in AmazonEC2FullAccess AmazonS3FullAccess AmazonBedrockFullAccess; do
#     aws iam attach-user-policy --user-name kinoforge-runner \
#         --policy-arn arn:aws:iam::aws:policy/$P
#   done
#   aws iam create-access-key --user-name kinoforge-runner   # prints key + secret
# (Tighten policies for prod — these wide grants are bootstrap-only.)
```

`.env.example` never mentions `.aws/policies/skypilot-minimal.json`.

`.gcp/README.md:28-35`:

```
- Roles (granted on the project):
  - `roles/compute.admin`
  - `roles/iam.securityAdmin` ← self-grant capability; additional roles can be added without re-auth.
```

The repo answers the "aspirational?" question itself — `docs/CLOUD-CREDS.md:162-175`:

```
- **AWS scoped policy doc:** `.aws/policies/skypilot-minimal.json` (tracked,
  not secret). ... NOT attached
  to `kinoforge-ci` in this layer (operator opted for AWS-managed broad
  policies instead — see "AWS — actually attached policies" below). The
  doc stays in repo as the scope-down target for a future layer.
- **AWS — actually attached policies:** `AmazonEC2FullAccess` +
  `IAMFullAccess` + `AmazonS3FullAccess` + `ServiceQuotasFullAccess`
  (AWS managed) + `kinoforge-ci-kms` ...
  The four managed policies are broader than
  required; the scoped `.aws/policies/skypilot-minimal.json` is the
  documented swap-in target.
```

So: **never validated against a real launch, by the repo's own record.** It is a
design document with a `.json` extension. It is also stale in a way that matters —
the attached set includes `IAMFullAccess` and `ServiceQuotasFullAccess`, neither of
which `.env.example` mentions, so the onboarding instructions do not even reproduce
the environment the project actually ran on.

Context that changes the priority: Phase 53 (2026-06-17) abandoned GCP+AWS GPU work
entirely in favour of RunPod / Lambda / Vast / Modal. This whole AWS+GCP SkyPilot
credential surface is dead weight, not an active attack surface.

**Severity: Medium.** The `iam.securityAdmin` self-grant is the genuinely
uncomfortable part — a compromised SA key can widen its own scope. The
`AmazonEC2FullAccess` onboarding line is a bad default that a future operator will
copy verbatim.

---

## F10 — Scrub discipline is inconsistent

**Verdict: CONFIRMED, and the problem is wider than the finding reports.**

### The reported instance

`.gitignore:90-91` excludes the key identifiers:

```
.aws/kms-test-key.arn
.gcp/kms-test-key.name
```

`.aws/policies/skypilot-minimal.json`, `KMSLayerW` statement, as of 2026-08-15
(scrubbed in Task 2 — the tracked file itself now reads `<KMS_KEY_ID>`; this
block is a verbatim quote of the pre-fix content, kept as evidence of what the
finding was, not a live copy of the file):

```json
      "Resource": [
        "arn:aws:kms:us-east-1:<AWS_ACCOUNT>:key/<KMS_KEY_ID>"  # kinoforge: allow-identifier
      ]
```

Every other identifier in that file is a placeholder (`<AWS_ACCOUNT>`,
`<GCS_KMS_KEYRING>`). The KMS key UUID is literal. Confirmed exactly as reported.

Separately, that same file uses `<GCS_KMS_KEYRING>` as the placeholder for an **S3
bucket prefix** (`"arn:aws:s3:::<GCS_KMS_KEYRING>-*"`) — a GCS-flavoured placeholder
name in an AWS S3 ARN. Cosmetic, but it signals the file was assembled by
find-and-replace rather than reviewed.

### The wider sweep

Sweeping all tracked files for concrete 12-digit AWS account ids, GCP project ids,
SA emails, and bucket names:

- **AWS account ids:** clean. Every hit is the reserved-for-docs `123456789012`, or
  the `<AWS_ACCOUNT>` placeholder.
- **Bucket names:** clean. All test doubles (`s3://bkt`, `gs://bucket`,
  `s3://layer-w-test`, `s3://probe-discard`).
- **Service-account emails:** clean in tracked source — `kinoforge-runner@proj.iam…`
  in `tests/core/test_auth.py` is a fake domain; `.gcp/README.md:24` uses
  `<GCP_PROJECT>`.
- **GCP project id: NOT clean, as of 2026-08-15 (scrubbed in Task 2).** The real
  project id `<GCP_PROJECT>` <!-- kinoforge: allow-identifier -->
  appeared in **9 tracked files**, including a *production code default*:

  `tools/quota_burn_lib.py:266`:
  ```python
      billing_dataset: str = "<GCP_PROJECT>.all_billing_data",  # kinoforge: allow-identifier
  ```

  plus `tests/tools/test_quota_burn_gcp.py` (12 sites),
  `tests/tools/test_quota_burn_cli.py` (4), `tests/tools/test_quota_burn_submit.py` (4),
  `docs/superpowers/specs/2026-06-10-gpu-quota-utilization-burn-design.md`,
  `docs/superpowers/plans/2026-06-10-gpu-quota-utilization-burn.md` (+ its
  `.tasks.json`), and `PROGRESS.md`.

  Note `tests/stores/test_recording.py:48-50` uses `kinoforge-prod-deadbeef` and
  asserts it gets scrubbed — so the project *has* a convention for fake project ids,
  and the quota-burn tests simply did not follow it.

A GCP project id is low-sensitivity on its own (it is not a credential and is
visible in any public bucket URL). But it is exactly the class of identifier the
`.gitignore` KMS exclusions exist to keep out, so the discipline is inconsistent in
both directions.

**Severity: High** — for inconsistency, not for exposure. A scrub policy that is
enforced by `.gitignore` for two files and by nothing at all for the rest is a
policy that will be violated again. This is the concrete argument for F7's missing
commit-boundary scanner.

---

## F11 — Provider conventions leak across adapters

**Verdict: CONFIRMED. Two shapes, and the consumers do not agree on which they get.**

### `_strip_trailing_exec`

`providers/skypilot/__init__.py:310-334`:

```python
def _strip_trailing_exec(script: str) -> str:
    """Strip a final line of the form `[<prefix> && ]exec <args>` from *script*.

    ``RenderedProvision.script`` (Layer Q) ends with an ``exec <run_cmd>`` line
    so the run process becomes PID 1 on RunPod's single-dockerArgs path. On
    SkyPilot, ``Task.setup`` must terminate so ``Task.run`` can start — the
    trailing ``exec`` would prevent that.
    ...
    last = lines[-1]
    # " && exec " would be subsumed by the " exec " substring check.
    if " exec " in last or last.startswith("exec "):
        return "\n".join(lines[:-1])
```

The convention it works around is documented at `core/interfaces.py:117-119`:

```
        run_cmd: Long-running command launched after the script completes.
            Convention: the script ends with ``exec <run_cmd>`` so the run
            cmd becomes the container's PID 1.
```

That is a RunPod deployment detail promoted to a cross-provider contract, then
undone by a substring heuristic in a second provider. The heuristic is
string-matching on `" exec "` — a provision script whose last line legitimately
contains that substring (e.g. a comment, or `foo --exec bar`) loses its last line
silently.

### Two endpoint shapes

`create_instance` (`:786-804`):

```python
        endpoints: dict[str, str] = {}
        # Only a server spec (long-running run_cmd) needs an HTTP tunnel; a
        # server-less deploy (CPU smoke) gets no tunnel and empty endpoints.
        if spec.run_cmd:
            local_port = self._alloc_port()
            ...
            self._tunnels[cluster_name] = tunnel
            endpoints = {"8000": f"http://127.0.0.1:{local_port}"}
```

with `_VIDEO_SERVER_PORT: int = 8000` hardcoded at `:343`. Versus `endpoints()`
(`:914-923`):

```python
    def endpoints(self, instance: Instance) -> dict[str, str]:
        ...
        return {"ssh": f"ssh://{instance.id}"}
```

### Who consumes which

**The `{"8000": …}` shape** (via `instance.endpoints`, set at create time):
`engines/_wait_ready.py:78-87`, `engines/_pod_http.py:252-264`,
`engines/diffusers/__init__.py:1359-1361`, `engines/comfyui/__init__.py:1462-1475`.
These build the ready-poll and generation URLs. They are the ones that must get the
tunnel URL.

**The `{"ssh": …}` shape** (via `provider.endpoints(instance)`):
`orchestrator.py:1647` (bare `deploy()` return), `cli/_commands.py:1773`
(`live.endpoints = provider.endpoints(live)` in status), `:2180` (status JSON
output), `:2328`, and — critically — `:2024-2026`, the **warm-attach rehydration**:

```python
    entry_endpoints = entry.get("endpoints")
    if isinstance(entry_endpoints, dict) and entry_endpoints:
        endpoints_dict = {str(k): str(v) for k, v in entry_endpoints.items()}
    elif hasattr(provider, "endpoints"):
        try:
            endpoints_dict = provider.endpoints(instance)
```

That branch is a live hazard on SkyPilot, and it is a hazard **either way**:

- If the ledger row has no `endpoints`, warm-attach falls to
  `provider.endpoints(instance)` → `{"ssh": "ssh://<cluster>"}` → `_wait_ready.py:85`
  takes `next(iter(...))` → base URL `"ssh://<cluster>"` → the engine tries to HTTP
  a `ssh://` URL.
- If the ledger row *does* have `endpoints` (it does since `1cb4299` made
  `Ledger.record` persist them), warm-attach replays
  `{"8000": "http://127.0.0.1:<port>"}` — a **localhost port belonging to a tunnel
  process that died with the previous CLI invocation**. See F12.

`cli/_commands.py:1773` has the same shape mismatch in `kinoforge status`: it
overwrites a live instance's tunnel endpoint with the ssh string.

### Blast radius

- `tests/providers/test_skypilot.py:811 test_ac8_endpoints_returns_ssh_url` pins the
  ssh shape.
- `tests/providers/test_skypilot_tunnel.py` pins the `{"8000": …}` shape.
- Both are green; nothing tests the interaction.

**Severity: High.** Two shapes from one provider with no discriminator, feeding a
warm-attach path that is wrong on both branches. This is the most likely source of
the next SkyPilot "attached successfully then failed instantly" incident.

---

## F12 — Remote-resource state is process-local

**Verdict: CONFIRMED. No durable record is written before `sky.launch`; only after a successful return.**

### The in-memory handle

`providers/skypilot/__init__.py:565-566`:

```python
        #: cluster_name -> live tunnel subprocess handle (killed on destroy).
        self._tunnels: dict[str, Any] = {}
```

Written at `:803` (`self._tunnels[cluster_name] = tunnel`), popped at `:881` in
`destroy_instance`. Purely in-process. If the CLI dies, the `ssh -N -T` child is
orphaned (or reaped by the OS), and the local port it held is lost — while the
cluster stays UP with a running server.

### Write ordering

The launch itself is `:780`:

```python
        raw = sky.launch(task, **launch_kwargs)
```

Nothing precedes it but `sky.Task.from_yaml_config`. The provider writes no state
file, no ledger row, no index row.

Every durable write happens strictly **after** `create_instance` returns, as an
`on_instance_created` callback — `orchestrator.py:864-877`:

```python
    def _create(offers: list[Offer]) -> tuple[Instance, Offer]:
        return _create_with_offer_retry(resolved_provider, _build_spec, offers)

    instance, _chosen_offer = _create_with_capacity_wait(
        find_offers=_find_offers,
        create=_create,
        capacity_wait_s=lifecycle.capacity_wait_s,
    )
    # B7 — acquire the cooperative session-claim lock now that instance.id is
    # known, BEFORE engine.provision runs.
    if on_instance_created is not None:
        on_instance_created(instance)
```

and `orchestrator.py:1189-1194`:

```python
        try:
            _ledger_for_claim.record(
                inst,
                idle_timeout_s=int(cfg.lifecycle().idle_timeout_s),
                max_age_s=int(cfg.lifecycle().max_lifetime_s),
            )
```

Same on the bare `deploy()` path (`:1623-1625`) and in the CLI's
`_stamp_cold_created_instance` (`cli/_commands.py:536-583`) and
`_ephemeral_index_add` (`:1629-1656`) — all post-create.

### The failure window

Between `sky.launch` starting and `create_instance` returning, kinoforge holds
**zero durable evidence that a cluster exists**. `sky.launch` on a GPU cloud is a
multi-minute call (provisioning + `setup` + `run` start). A SIGKILL, an OOM, or a
container restart in that window leaves a running, billing cluster that no kinoforge
command can see: `kinoforge list` reads the ledger, `reap` reads the ledger, and
`sweep` would need `list_instances()` which requires the `live-skypilot` env (F3).

The mitigation that exists for RunPod does not exist here: RunPod names pods
deterministically and `destroy` has an orphan-probe path
(`"destroyed orphan: … (no ledger entry, provider=modal)"` for Modal). SkyPilot's
cluster name is `spec.run_id`, which is derivable — but nothing derives it, and
`kinoforge destroy --id <cluster>` in the default env cannot reach `sky` at all.

Compounding: even a *successful* run's durable record is misleading. The ledger
persists `endpoints = {"8000": "http://127.0.0.1:<port>"}` — a local port that is
meaningless in any other process. See F11.

### Blast radius

- `tests/providers/test_skypilot_tunnel.py` covers tunnel spawn/kill within one
  process; nothing covers process death.
- The `--no-reuse` teardown, the `destroy_instance` tunnel-kill `finally`, and the
  deploy-error `destroy_instance` fallback (`orchestrator.py:1655-1671`) are all
  in-process and all die with it.
- `CLAUDE.md`'s "verify with `kinoforge list` AFTER the orchestrator exits" rule is
  the operator-side compensating control — and on SkyPilot it inspects a ledger that
  may never have been written.

**Severity: Critical.** Combined with F1 (no autostop) and F3 (no reachable reaper),
a process kill during launch produces an indefinitely-billing cluster with no
in-repo way to find it.

---

## Recommended remediation ordering

The three Critical findings compose into a single failure mode: *a SkyPilot cluster
can outlive every mechanism that is supposed to kill it.* Fix them as one workstream,
in this order.

**1. F12 — write a durable pre-launch record.** Do this first because it is the
prerequisite for verifying any of the others. A row written *before* `sky.launch`
(cluster name + provider + created_at, upgraded in place on return) makes every
orphan discoverable. Small, self-contained, no cross-provider blast radius. It also
retires the "I killed the CLI and don't know what's running" class of incident
immediately, before any of the harder fixes land.

**2. F1 — stop relying on autostop for server-mode.** The current mapping is a
no-op, and worse, it is *documented* as the SkyPilot cost model in the module
docstring and asserted by a passing test. Two viable shapes:
(a) run the server as a background process inside `setup` and give `run` a
terminating command, so the job queue actually goes idle; or (b) drop
`idle_minutes_to_autostop` and own the timer controller-side. Whichever is chosen,
`tests/providers/test_skypilot.py:386` must be rewritten to pin the *effect* rather
than the kwarg — that test is currently certifying the bug. Do not bother switching
`wait_for`: the SSH mechanism is already inert for `ssh -N -T` (see F1).

**3. F3 — make the reaper reachable.** Concretely: `reap`/`sweeper` must not
silently degrade to `UNROUTABLE` when `sky` is absent from the active env. Either
make the CLI refuse with a clear "run this under `-e live-skypilot`" error, or move
the skypilot dependency so the reaper path works from the default env. Also consider
promoting `ORPHAN_REAP` into the default apply policy for providers with no
substrate-side backstop — today `OVERAGE_REAP` at `max_lifetime` is the only default
that fires, which is 90 minutes of billing on the FlashVSR configs.

**4. F2 — pass `down=True`.** One kwarg. Do it in the same change as F1; it is
meaningless before and free after.

**5. F11 — one endpoint shape, or an explicit discriminator.** The warm-attach
rehydration branch (`cli/_commands.py:2024-2026`) is wrong on both paths for
SkyPilot. Minimum viable fix: make SkyPilot's `endpoints()` reconstruct or refuse
rather than return an ssh URL that downstream code will treat as an HTTP base. The
deeper fix — `create_instance` and `endpoints()` returning the same keyspace for all
providers — is the right one but touches RunPod and Modal.

**6. F6 — wire `region` (and `retry_until_up`) into `ComputeConfig`.** Provider side
is already implemented and tested; this is a `ComputeConfig` field plus three lines
in `_adapters.build_provider_for`. Cheap, and it closes a documented "sky picked
asia-southeast1" hazard that the project has a standing rule against.

**7. F4 — bind the rate cap at launch.** Either pass a price ceiling into the sky
optimizer, or re-read the realized rate post-launch and abort + destroy above cap.
The second is more portable and also fixes the silently-wrong `est_spend` on every
SkyPilot row. Rank it below the lifecycle work because an over-priced cluster that
*does* get reaped is a bounded loss; an under-priced one that never dies is not.

**8. F7 — add a commit-boundary secret scan, and collapse the three regex lists to
one.** The scanner matters more than the lists. Make `tools/_redact.py` the single
source (adopting `conftest_runpod.py`'s stronger `AKIA|ASIA` and full-span PEM
patterns), keep the hook a superset, and extend the parity test to cover
`conftest_runpod.py` too.

**9. F10 — scrub the real GCP project id and the KMS UUID.** Mechanical: swap the
project id for the `kinoforge-prod-deadbeef` convention already used in
`tests/stores/test_recording.py`, and placeholder the KMS UUID. Low value on its own;
high value as the thing the F7 scanner then keeps clean. Doing F10 before F7 just
means doing it again.

**10. F5 — flag ignored fields at validation time.** A single check that
cross-references provider-scoped fields (`cloud`, `cloud_type`, `heartbeat_mode`,
`restart_policy`, `spot`, `diagnostic_env`) against `cfg.compute.provider` and WARNs.
Cheap, purely additive, no behaviour change.

### Not worth fixing

- **F9 — the AWS/GCP credential surface.** `skypilot-minimal.json` is aspirational
  by the repo's own admission (`docs/CLOUD-CREDS.md:166-168`), and Phase 53 abandoned
  GCP+AWS GPU work entirely on 2026-06-17. Attaching and validating a least-privilege
  policy against a launch path the project no longer uses is effort spent on dead
  infrastructure. **Do the two-line version instead:** delete the
  `AmazonEC2FullAccess`/`AmazonS3FullAccess`/`AmazonBedrockFullAccess` recipe from
  `.env.example` (a future operator will paste it), and add one line to `.gcp/README.md`
  noting that `roles/iam.securityAdmin` is a self-grant capability retained
  deliberately. Skip the policy validation.
- **F5's `InstanceSpec` restructuring.** Splitting the 17-field union into
  per-provider spec types is the architecturally correct answer and is not worth it.
  Every provider already reads only its own subset, the ignores are documented inline,
  and the refactor touches four providers, the orchestrator, and every provider test.
  The validation WARN in item 10 captures nearly all the practical value for a
  fraction of the risk.
- **F8's fail-open guard, as a standalone fix.** Setting
  `KINOFORGE_REQUIRE_REDACT_HOOK=1` in the pixi test task would turn a green suite red
  on any machine without the user-scope hook — including CI, which is where it would
  be most annoying and least useful. The hook is defence-in-depth for the *transcript*,
  not for the repo. Once F7 lands a real commit-boundary scanner, the hook's failure
  mode stops mattering. Worth doing then, or not at all. (The false-positive
  redaction noted under F8 is a separate, smaller nuisance — worth a bug report to
  wherever that hook is maintained, not a repo change.)

---

## Verification method notes

- SkyPilot autostop semantics were read from the **installed** `sky` package at
  `.pixi/envs/live-skypilot/lib/python3.12/site-packages/sky/` (version
  `0.12.3.post1`), specifically `skylet/events.py`, `skylet/autostop_lib.py`,
  `skylet/job_lib.py`, and `client/sdk.py`. No documentation or recall was used.
- No code was modified. No tests were run. No cloud calls were made.
- The user-scope redaction hook corrupts `rg` output for several benign identifiers
  in this codebase (see F8). All quotes above were taken via `Read` or a Python line
  reader to avoid mangled text.
