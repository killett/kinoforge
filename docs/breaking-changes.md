# Breaking changes

(Moved from README §Breaking changes (Layer T cloud store.kind routing, Layer M engine.hosted.model removed) on 2026-06-27. See [../README.md](../README.md).)

## Breaking changes

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
