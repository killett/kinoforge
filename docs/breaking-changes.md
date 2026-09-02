# Breaking changes

(Moved from README §Breaking changes (Layer T cloud store.kind routing, Layer M engine.hosted.model removed) on 2026-06-27. See [../README.md](../README.md).)

## Breaking changes

### Compute-seam S4 — selection moved into the providers; the rate cap is verified

Three breaks, in the order an operator is likely to meet them.

**1. A custom provider must declare a rate source, or its configs stop loading.**

Every `ComputeProvider` now has to declare exactly one of `Capability.RATE_READBACK`
or `Capability.RATE_DETERMINISTIC`. A provider declaring neither is a validation
**ERROR** at load — not a warning — because `compute.placement.max_usd_per_hr`
cannot be enforced against something that cannot say what it bills:

```
[ERROR] compute.placement.max_usd_per_hr: <name> declares neither RATE_READBACK
  nor RATE_DETERMINISTIC, so kinoforge cannot know what a launched instance
  bills and max_usd_per_hr cannot be enforced.
```

Which to declare: `RATE_READBACK` if your provider CHOOSES the SKU (an optimizer,
a scheduler) — then implement `realized_rate(instance)` to read the rate off the
launched instance. `RATE_DETERMINISTIC` if the SKU you were handed is the SKU
that bills, so the catalog price IS the rate. Declaring both is refused: they are
opposite claims about who chooses.

**2. `find_offers` is off the `ComputeProvider` ABC, and `InstanceSpec.offer` is gone.**

Selection is the provider's business now. A custom provider no longer implements
`find_offers` unless it genuinely enumerates a catalog — in which case it also
declares `Capability.CATALOG_ENUMERATION` and keeps the method public. What every
provider DOES get is `spec.placement`, the portable resource block, and it selects
from that inside `create_instance`.

The offer-retry loop moved with it: the orchestrator no longer iterates a catalog
on any provider's behalf. A provider that wants "try the next offer on
CapacityError" implements that itself (RunPod does).

**3. `HardwareRequirements` and `Config.hardware_requirements()` are deleted.**

They described the same five numbers as `Placement` under different names.
`filter_offers(offers, placement)` now takes the portable block directly, and
`gpu_preference` is spelled `accelerators`. No YAML change: the config surface
was already `compute.placement`, and this only removes the internal shim behind
it.

**What did NOT change, deliberately.** `max_usd_per_hr` is still a pre-book
catalog filter wherever a catalog exists — `filter_offers` still excludes
`mode == "pod"` offers above the ceiling before anything is booked. S4 ADDS a
post-launch readback on top of that, because a filter cannot see a choice made by
an optimizer that never consulted the catalog. Removing the filter would have
made kinoforge pay for boots it currently never starts.

**New failure mode worth knowing.** A launched instance that bills above the cap
is now DESTROYED and the run raises `RateCapExceeded`, naming both numbers, the
instance id and what was booked. Previously it ran to completion while every
kinoforge surface reported the number you asked for.

### Compute-seam S3 — `RenderedProvision.script` is no longer what providers boot

This one cannot break a YAML config. It breaks a **custom engine**.

`RenderedProvision` used to carry one bash blob (`script`) whose last line was, by
convention, the command that started the workload — plus `run_cmd`, and the
`build_script` / `runtime_script` pair Modal used to split image-build work from
container-start work. Providers then had to guess where the setup ended and the
server began, which they did by substring-matching `" exec "` on the last line.
That guess was wrong on both shipped engines in opposite directions.

All four fields are gone. An engine now emits a **pair**:

```python
RenderedProvision(
    script="…",                                  # still there, see below
    setup_steps=(SetupStep("pip install …", bakeable=True, runtime=False),
                 SetupStep("export FOO=1")),     # what a provider RUNS as setup
    launch=Launch(argv=("python", "main.py"),    # what STARTS the workload
                  workdir="/workspace/ComfyUI",
                  exec_pid1=True),
    image="…", ports=[…], env_required=[…],
)
```

**Migration.** An engine that returns only `script` now provisions **nothing** —
every provider composes from `setup_steps` and `launch`, and neither is
synthesised from anything else. That silence is deliberate: a guessed launch is
exactly what this stage removed. Emit the pair:

* `setup_steps` — the provisioning steps in declaration order. `bakeable=True`
  means "safe to run at image-BUILD time"; `runtime=True` (the default) means
  "must run at container start". They are independent, because a step can be
  both — the diffusers module embed has to exist in the image for the
  build-phase weights fetch AND in the container for the server to import.
* `launch` — `Launch(argv, workdir="", exec_pid1=False)`. All three are
  properties of the WORKLOAD, not of a provider: `exec_pid1=False` is how the
  diffusers engine keeps bash as PID 1 so its EXIT trap fires when the server
  dies. `argv` is joined **verbatim**, so an argument needing shell quoting must
  arrive already quoted. `launch=None` is legal and means "this workload starts
  nothing" (a BATCH shape); RunPod and Modal refuse a spec that declares steps
  but no launch rather than booting a container that serves nothing.

`RenderedProvision.script` survives, but only as a human-readable rendering for
`kinoforge doctor` and the C30 diagnostics probe. Nothing boots it.

Two live bugs this fixed, both on shipped configs: SkyPilot + diffusers put the
server command inside `Task.setup` (which could then never terminate) *and*
repeated it in `Task.run`; SkyPilot + comfyui lost `cd /workspace/ComfyUI` from
`Task.run` and ran `main.py` from the login directory.

### Compute-seam S2 — `compute` forbids unknown keys, and `mode: serverless` finally routes

Two changes an existing config can notice.

**1. `ComputeConfig` now carries `extra="forbid"`.** An unknown key under
`compute:` is a load-time `ConfigError` instead of being dropped. This was
deferred through S1 on purpose: the permissive default was hiding two keys
operators legitimately write — `tags`, and `mode` before anything read it — so
refusing them would have been worse than dropping them. Both are real fields
now, which makes any remaining unknown key a typo:

```diff
 compute:
   provider: runpod
   image: ...
-  placemnt:            # silently applied every placement DEFAULT, discarding this block
+  placement:
     disk_gb: 200
```

Every shipped config still loads. S1's removed-key messages are unaffected —
`compute.cloud` still names `compute.backend_options.skypilot.clouds` rather
than degrading to "Extra inputs are not permitted", because that validator
runs in `mode="before"`, ahead of pydantic's extra handling.

**2. `compute.mode: serverless` now takes the serverless branch.** 46 shipped
configs wrote `mode` and nothing read it; `RunPodProvider.create_instance`
branched on a `spec.tags["mode"]` that no code path ever set, so
`mode: serverless` silently created a *pod* and produced a byte-identical
payload. `build_instance_spec` now writes the tag.

If you have a RunPod config that says `mode: serverless` and has been running
as a pod, it will now create a serverless endpoint — a different resource with
different billing. Every shipped config is `mode: pod`, which was already the
branch taken, so nothing in this repo changes behaviour. Change the key to
`pod` to keep the old effect.

Two related additions that break nothing: `compute.tags` is a real field (it
was written by four shipped configs and silently dropped), and
`compute.placement.region` is new with a `None` default meaning "let the
provider decide". Setting `region` on a `runpod` or `modal` config IS a new
load-time ERROR — neither provider sends it anywhere, and nothing else in the
config bounds where the run lands. No shipped config does this.

### Compute-seam S1 — `compute.requirements` / `compute.cloud` / `compute.cloud_type` / `lifecycle.capacity_wait` removed

Four config keys are gone with **no alias and no deprecation shim**. The
compute block now splits into a *portable* `placement` block that every
provider reads identically, and a per-provider `backend_options.<provider>`
namespace for anything that does not generalize across clouds.

Migration:

```diff
 compute:
   provider: skypilot
   image: ...
-  cloud: [lambda]                   # skypilot-only, silently ignored elsewhere
-  cloud_type: secure                # runpod-only, silently ignored elsewhere
-  requirements:
-    gpu_preference: [A100-80GB, H100]
-    min_vram_gb: 48
-    min_cuda: "12.8"
-    max_usd_per_hr: 1.09
-    disk_gb: 200
+  placement:
+    accelerators: [A100-80GB, H100]   # was `gpu_preference`
+    accelerator_count: 1
+    min_vram_gb: 48
+    min_cuda: "12.8"
+    max_usd_per_hr: 1.09
+    disk_gb: 200
+  backend_options:
+    skypilot: {clouds: [lambda], retry_until_up: true}
+    runpod: {cloud_type: secure, capacity_wait_s: 300}
   lifecycle:
     budget: 25.00
-    capacity_wait: 5m
```

Key by key:

| Removed | Replacement |
|---|---|
| `compute.requirements` | `compute.placement` |
| `compute.requirements.gpu_preference` | `compute.placement.accelerators` |
| `compute.cloud` | `compute.backend_options.skypilot.clouds` |
| `compute.cloud_type` | `compute.backend_options.runpod.cloud_type` |
| `lifecycle.capacity_wait` | `compute.backend_options.runpod.capacity_wait_s` (seconds, not a duration string) |

Failure mode: each removed key raises a load-time `ConfigError` naming its new
path — e.g. `compute.requirements was removed…`. Nothing is silently ignored,
and an unknown key *inside* a `backend_options.<provider>` namespace, or an
unknown provider name, is also a `ConfigError` (each namespace is validated by
the owning provider's own options model).

**One behaviour change, not just a rename:** capacity-wait retry-on-
`CapacityError` is now **RunPod-only**. `lifecycle.capacity_wait` used to apply
to whatever provider was selected; `backend_options.runpod.capacity_wait_s`
applies to RunPod alone, and every other provider gets a 0-second window (fail
on the first `CapacityError`). SkyPilot operators who relied on the old knob
want `backend_options.skypilot.retry_until_up` instead — a separate mechanism
inside SkyPilot's own launcher, not the same loop wearing a second name.

Non-breaking for: hosted configs (no `compute:` block at all), and configs that
never set any of the four keys.

Related: `kinoforge doctor` now reports every `placement` field the *selected*
provider does not actually consume. Most are WARNs naming the real substitute
bound and the config still runs; `accelerator_count` is the one hard
`ConfigError` on the billed providers. See the README's "Configuration at a
glance" section.

### Layer T — cloud `store.kind` now routes the ledger too

Operators who configured `store.kind: s3` (or `gcs`) for artifacts but
expected the instance ledger to remain on local disk: the ledger now
lives in the configured store. Same authentication, same bucket; the
sidecar at `<state-dir>/store.json` records the routing.

Detection: kinoforge hard-blocks the first cloud-routed command if your
local state directory still has tracked instances. See
[Migration from a local ledger](#migration-from-a-local-ledger) for the
4-step procedure.

Non-breaking for: operators on `store.kind: local` (default), operators
on fresh state directories, and operators who already had no in-flight
local instances.

### Layer M — `engine.hosted.model` removed; use top-level `spec.model`

Hosted configs that previously declared the model identifier under
`engine.hosted.model` must move the value to top-level `spec.model`. The
two locations carried the same string in every shipped config, with a
"keep these in sync" comment block as the only safeguard. Layer M
collapses them: `spec.model` is now the single source of truth, read both
by `HostedAPIBackend.submit` (wire body) and by
`HostedAPIEngine.key_base` (cache identity).

Migration:

```diff
 engine:
   kind: hosted
   hosted:
     provider: my-shim
     endpoint: "https://shim/inference"
-    model: "wan-ai/Wan2.2-T2V-A14B"
     api_key_env: "MY_SHIM_KEY"
     health_url: "https://shim/health"
     url_path: video.url

 spec:
   model: "wan-ai/Wan2.2-T2V-A14B"
```

Failure mode: configs still carrying `engine.hosted.model` raise a
load-time `ValidationError` with the message
`"engine.hosted.model is no longer supported; move the value to
top-level spec.model"`.
