# `tests/_smoke_harness/` — shared smoke-test harness

Centralises the kinoforge-internal HTTP patterns + RunPod lifecycle
helpers that every smoke tier (local CPU, weekly live, release gate)
inherits. See `docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`
for the design.

## Four kinoforge-internal patterns (all rediscovered during T22)

1. **`User-Agent: kinoforge-smoke/0.1`** — Cloudflare (which fronts
   `*.proxy.runpod.net`) returns HTTP 403 to the default
   `Python-urllib/X.Y` UA. Original source:
   `src/kinoforge/engines/diffusers/__init__.py:207-212`.

2. **`?api_key=<RUNPOD_API_KEY>` URL suffix** — RunPod's pod proxy
   requires query-param auth. Original source:
   `src/kinoforge/providers/runpod/__init__.py:138`.

3. **`urllib.error.URLError` retry budget** — RunPod's GraphQL
   surface periodically returns connection-reset; one transient
   should not crash a 15-minute cold-boot. Caught during T22
   attempt 2.

4. **`destroy_all_active_pods()` sweep in `finally`** — a smoke
   that crashes before its in-test `pod_id` variable is captured
   cannot rely on a per-id destroy. Caught during T22 attempt 2
   ($0.63 wasted).

## Modules

| Module | Purpose |
|---|---|
| `http.py` | `post_json`, `get_json` — UA + api_key + URLError retry |
| `runpod_lifecycle.py` | `resolve_proxy_url`, `destroy_all_active_pods`, `PodStatPoller` |
| `civitai.py` | `resolve(ref) → ArtifactDownloadSpec` |
| `matrix.py` | `MatrixStep` + `run_matrix(...)` engine-agnostic runner |
| `budget.py` | `BudgetTracker(cap_usd, pod_id)` |

## Usage (Tier 3 example)

```python
from tests._smoke_harness import http, runpod_lifecycle, matrix, budget, civitai

base_url = runpod_lifecycle.resolve_proxy_url(pod_id, port=8000)
specs = {ref: civitai.resolve(ref).to_download_spec()
         for ref in (LORA_A, LORA_B)}
report = matrix.run_matrix(cfg_path=CFG, pod_proxy_url=base_url,
                           steps=STEPS, download_specs=specs)
budget.BudgetTracker(cap_usd=0.30, pod_id=pod_id).assert_under_cap()
```
