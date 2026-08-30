# Breaking changes

(Moved from README §Breaking changes (Layer T cloud store.kind routing, Layer M engine.hosted.model removed) on 2026-06-27. See [../README.md](../README.md).)

## Breaking changes

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
