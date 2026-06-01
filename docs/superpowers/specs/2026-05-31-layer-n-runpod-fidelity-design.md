# Layer N — RunPod cloud-fidelity hardening

**Status:** validated 2026-05-31, awaiting plan
**Branch:** `build/layer-n` off `main@862e2d5`
**Closes:** PROGRESS:114 carry-forward #1 (`RunPodProvider.find_offers` REST/GraphQL shape stub)
**Defers:** SkyPilot SDK smoke test (PROGRESS:114 #2), S3/GCS medium-fidelity tests (PROGRESS:114 #3) — each their own future layer

## Why this layer

PROGRESS:113–116 lists three "Real-cloud verification gaps — offline-tested only". This layer
closes the first one (RunPod) under the principle that one cloud at a time is enough scope per
layer. All 24 existing RunPod tests today pass against **hand-crafted** GraphQL response dicts;
nothing has ever validated that those dicts match what the real RunPod GraphQL API returns. The
provider could be silently buggy and every offline test would still pass.

## Architecture

Three new artifacts on `build/layer-n` from `main@862e2d5`:

1. **`tests/providers/fixtures/runpod/`** — directory of committed real-API JSON captures:
   - `gpu_types.json` — `find_offers` GraphQL response
   - `list_pods.json` — `list_instances` response
   - `get_pod.json` — `get_instance` response (mid-lifecycle, ready state)
   - `create_pod.json` — `create_instance` response
   - `terminate_pod.json` — destroy mutation response
   - `sample_init_frame.png` — i2v input asset (~32 KB)
2. **`examples/configs/runpod-comfyui-wan.yaml`** — committed YAML driving the live smoke. Pins
   `min_vram_gb: 24`, `max_cost_rate_usd_per_hr: 0.50`, `budget: 2.00`, ComfyUI + Wan-i2v model
   entry, image `runpod/comfyui:<tag-pinned-at-first-smoke>`, exposed port 8188.
3. **`tests/providers/test_runpod_live.py`** — single pytest module, all tests gated by
   `@pytest.mark.skipif(os.environ.get("KINOFORGE_LIVE_RUNPOD") != "1", reason=…)`. Mirror of the
   Layer I fal smoke pattern.

Plus modifications:

- **`tests/providers/test_runpod.py`** — fixture-replay refactor: existing 24 tests load JSON from
  `fixtures/runpod/*.json` via a shared `_load_fixture(name)` helper instead of hand-crafted
  inline dicts. No behaviour change to production code under test.
- **`tests/providers/conftest_runpod.py`** (or inline at the top of `test_runpod.py`) — recording
  HTTP wrapper + `_load_fixture` helper + secret-redaction scrub.
- **`README.md`** — new "Real providers — RunPod" section parallel to the existing fal section.
- **`PROGRESS.md`** — Phase 24 entry; close carry-forward #1.

No production-code change is anticipated. Layer N is verification-only by default. If the live
smoke surfaces real bugs (Layer I precedent: 5 caught), those bugs become in-scope production
fixes on the same branch, each documented in PROGRESS Phase 24.

## Capture protocol + fixture format

`KINOFORGE_LIVE_RUNPOD=1` runs live tests. `KINOFORGE_SAVE_FIXTURES=1` (additive) writes JSON
captures back to `tests/providers/fixtures/runpod/` during the same run. Tests assert behaviour
on every live run; fixture writes only fire when both flags are set.

**Capture wiring:** the live test injects a recording `http_post` / `http_get` seam that wraps
`_urllib_post_json` / `_urllib_get_json`. Each real call is logged: GraphQL query string, full
response dict. The recorder dispatches each call to a fixed fixture filename via an
operation-name table:

| GraphQL fragment match | Fixture filename |
|---|---|
| `gpuTypes {` | `gpu_types.json` |
| `myself { pods` | `list_pods.json` |
| `pod(input:` | `get_pod.json` |
| `podFindAndDeployOnDemand` | `create_pod.json` |
| `podTerminate` | `terminate_pod.json` |

A query that doesn't match any row is captured to `unknown_<sha>.json` and the recorder logs a
WARNING so a new operation never silently goes unrecorded. Re-running with both flags overwrites
— current shape always wins.

**Fixture file shape:**

```json
{
  "_meta": {
    "captured_at": "2026-05-31T14:23:11-07:00",
    "git_sha": "<sha at capture>",
    "operation": "find_offers",
    "request_query": "{ gpuTypes { id displayName ... } }"
  },
  "response": { "data": { "gpuTypes": [...] } }
}
```

Offline tests load via `_load_fixture("gpu_types.json")["response"]` and feed it to spies. The
`_meta` block exists for forensic value only — not asserted on.

**Secret redaction:** the capture wrapper scrubs any field name matching
`r"(?i)(token|key|secret|password)"` to `"<REDACTED>"` before write. Pod IDs and GPU types stay
verbatim (not secret). Asserted by a unit test: feed a fake recorder a body containing
`"apiKey": "sk-real"`, write, read back, assert `<REDACTED>`.

**Idempotence:** committed fixtures are the contract. CI runs offline tests against committed
JSON. A contributor regenerating fixtures (e.g. RunPod schema upgrade) commits both `_meta` +
`response` updates in the same diff — reviewer sees git SHA + timestamp move forward in
lockstep.

## Live smoke control flow

Single pytest function: `test_runpod_live_e2e_wan_i2v_smoke`. Linear ordering:

1. **Preconditions** — assert `RUNPOD_API_KEY` and `RUNPOD_TERMINATE_KEY` set; load
   `examples/configs/runpod-comfyui-wan.yaml`; build recording HTTP seam if
   `KINOFORGE_SAVE_FIXTURES=1`.

2. **find_offers** — `provider.find_offers(reqs)` where `reqs` comes from cfg `requirements`.
   Assert non-empty result; assert every offer has `cost_rate_usd_per_hr <= 0.50`; assert sort
   order respects `gpu_preference`. Pick `offers[0]` (cheapest viable).

3. **deploy** — `try:` block opens. `orchestrator.deploy(cfg)` runs through `create_instance`
   → `get_instance` poll until `status="ready"` (cap: 10 min wall-time; raises `TeardownError`
   on timeout). Record real pod ID for `finally`.

4. **provision + generate** — `orchestrator.generate(request)` with a 1-image i2v request (input
   image bundled in `tests/providers/fixtures/runpod/sample_init_frame.png`). Wan i2v at
   5s/480p. Real artifact lands at `<run_dir>/<run_id>/<name>.mp4`.

5. **assertions** — artifact file exists; size > 100 KB and < 50 MB; magic bytes start with
   `\x00\x00\x00 ftypisom` (MP4 box header); generation duration < 15 min (else raise — runaway
   cost guard #2). Capability key persisted in profile cache. Smoke records the artifact path +
   size + sha256 to `tests/providers/fixtures/runpod/last_smoke.json` (capture-only, not
   asserted on).

6. **finally** — `provider.destroy_instance(pod_id)` always called. If `destroy_instance` raises
   `TeardownError`, the test re-raises after logging the dangling pod ID + a copy-pasteable
   `curl` command to terminate manually. Last line of defence before billing leak.

**Runaway-cost guards (independent):**

- a) `max_cost_rate_usd_per_hr=0.50` in YAML → `filter_offers` excludes expensive GPUs upstream
  of `create_instance`
- b) `budget=2.00` in YAML → `BudgetTracker.enforce` tears down mid-run if estimated spend
  crosses cap
- c) `finally` block always destroys
- d) `idle_timeout_s=600` + selfterm script → pod self-destructs 10 min after last heartbeat
  even if the test process is killed mid-run

## Offline fixture-replay refactor

Existing `tests/providers/test_runpod.py` (24 tests) currently builds hand-crafted dicts inline.
After refactor: dicts come from JSON.

**Shared helper** added at module top of `tests/providers/test_runpod.py` (or
`conftest_runpod.py`):

```python
import json
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runpod"

def _load_fixture(name: str) -> dict[str, Any]:
    with (_FIXTURE_DIR / name).open() as f:
        return dict(json.load(f)["response"])
```

**Per-test refactor pattern:** an existing test that did

```python
http_get = lambda url: {"data": {"gpuTypes": [{"id": "RTX 3090", "memoryInGb": 24,
                                               "lowestPrice": {"uninterruptablePrice": 0.30}}]}}
```

becomes

```python
http_get = lambda url: _load_fixture("gpu_types.json")
```

Where hand-crafted shape diverges from real shape, the test changes to match real shape.
Assertions on `provider.find_offers()` output keep the same numeric values, because those values
are now the values that came from the live capture — still meaningful.

**Test additions (not just refactor):**

- `test_find_offers_real_shape_required_keys` — asserts every offer dict in fixture has the keys
  the production code reads (`id`, `memoryInGb`, `lowestPrice.uninterruptablePrice` OR
  `lowestPrice.minimumBidPrice`). Catches future RunPod schema upgrades that drop a field.
- `test_pod_status_mapping_covers_real_statuses` — fixture `list_pods.json` may contain
  `desiredStatus` values not in the current `_runpod_status_to_kinoforge` map. Assert every real
  status maps to a defined kinoforge status (no fallback-to-`"starting"` silent
  miscategorisation).
- `test_redaction_scrub_unit` — feeds fake recorder a body with `apiKey: "sk-real"`, asserts
  post-write JSON contains `<REDACTED>`.

**Backward compat:** if a test under refactor depended on a hand-crafted shape that real RunPod
doesn't return (e.g. a fictional field), the test changes to use the real field. Reviewer
treats every value change as a potential bug in the production code that was previously masked.

## Acceptance criteria

All must pass before merge.

1. **AC1 — Fixtures committed.** `tests/providers/fixtures/runpod/` contains real RunPod
   responses for `find_offers`, `list_instances`, `get_instance`, `create_instance`,
   `destroy_instance` paths. Each file has a `_meta` block with `captured_at`, `git_sha`,
   `operation`, `request_query`.
2. **AC2 — Secret redaction enforced.** A unit test feeds a response body containing a key
   matching `r"(?i)(token|key|secret|password)"` to the recording wrapper, asserts the persisted
   JSON contains `<REDACTED>` for that field. No real secrets land in committed fixtures
   (reviewer also scans).
3. **AC3 — Offline tests use fixtures.** All 24 existing `tests/providers/test_runpod.py` tests
   load via `_load_fixture(name)`. `rg "lowestPrice|memoryInGb" tests/providers/test_runpod.py`
   returns zero hits in test bodies (only in fixture JSON).
4. **AC4 — Real-shape required-keys lockdown.** `test_find_offers_real_shape_required_keys` and
   `test_pod_status_mapping_covers_real_statuses` pass against committed fixtures. Both fail
   loudly if a fixture is regenerated against a future RunPod schema that drops a field.
5. **AC5 — Live smoke produces real artifact.** `KINOFORGE_LIVE_RUNPOD=1 pixi run pytest
   tests/providers/test_runpod_live.py::test_runpod_live_e2e_wan_i2v_smoke` produces an MP4
   ≥ 100 KB on real RunPod via ComfyUI + Wan i2v, then destroys the pod cleanly. SHA + path +
   size recorded in PROGRESS Phase 24 entry.
6. **AC6 — Cost safety quadruple-locked.** Live smoke YAML pins `max_cost_rate_usd_per_hr=0.50`,
   `budget=2.00`, `idle_timeout_s=600` (selfterm fallback). `finally:` block always calls
   `destroy_instance`. All four independent guards verified by reading the YAML + live-test
   source diff.
7. **AC7 — CI green offline-only.** Full suite (existing 755 + Layer-N additions) passes
   without `KINOFORGE_LIVE_RUNPOD` set on Linux + macOS CI matrix. Live test marked `skipif`,
   shows as `skipped` in the report.
8. **AC8 — README + PROGRESS updated.** New "Real providers — RunPod" README section parallel to
   fal section, includes the two-env-var capture protocol. PROGRESS Phase 24 entry committed.
9. **AC9 — Verification-only by default.** No production-code change unless the live smoke
   surfaces a real bug. If bugs are found (Layer I precedent: 5 caught), each is documented in
   PROGRESS Phase 24 as "Live-smoke bug catches integrated".

## Non-goals (explicitly out of scope)

- Serverless mode read-paths or live smoke (pod-only this layer).
- SkyPilot SDK smoke test or S3/GCS medium-fidelity tests (PROGRESS:114 carry-forwards #2 + #3
  — separate future layers).
- Multi-GPU smoke (single GPU, single offer).
- Smoke that runs in CI by default (would cost money + require a shared RunPod account).
- Recorded-fixture replay for write-path request bodies (`create_instance` / `destroy_instance`
  / `stop_instance` request bodies are not snapshot-tested; their responses are).
- Live smoke for Diffusers/Hosted engines deployed on RunPod (Layer N+1 candidates).
- Cross-engine retry/back-off on RunPod 5xx (out of scope; existing seams handle on the caller
  side).

## File / diff inventory

**New files:**

| Path | Purpose | Approx LOC |
|---|---|---|
| `tests/providers/fixtures/runpod/gpu_types.json` | find_offers capture | ~50 |
| `tests/providers/fixtures/runpod/list_pods.json` | list_instances capture | ~30 |
| `tests/providers/fixtures/runpod/get_pod.json` | get_instance capture | ~20 |
| `tests/providers/fixtures/runpod/create_pod.json` | create_instance capture | ~15 |
| `tests/providers/fixtures/runpod/terminate_pod.json` | destroy_instance capture | ~10 |
| `tests/providers/fixtures/runpod/sample_init_frame.png` | i2v input asset | bin, ~32 KB |
| `examples/configs/runpod-comfyui-wan.yaml` | live smoke config | ~80 |
| `tests/providers/test_runpod_live.py` | live E2E gate | ~250 |
| `tests/providers/conftest_runpod.py` (or inline) | recording HTTP wrapper + `_load_fixture` + redaction | ~120 |

**Modified files:**

| Path | Change | Approx LOC delta |
|---|---|---|
| `tests/providers/test_runpod.py` | dicts → `_load_fixture`; +3 new tests (AC4, redaction) | ~ +60 / −80 net |
| `README.md` | new "Real providers — RunPod" section | +40 |
| `PROGRESS.md` | Phase 24 entry + close carry-forward #1 | +35 |

## Risk register

| Risk | Likelihood | Blast | Mitigation |
|---|---|---|---|
| Live smoke leaks money via dangling pod | low | high | 4 independent guards (AC6); `finally` block; selfterm script as last defence |
| Real secrets in committed fixtures | medium | high | Redaction scrub (AC2) + reviewer scan + first fixture commit inspected before push |
| RunPod GraphQL schema change breaks both CI + live | medium | medium | Fixture regen is one command (`KINOFORGE_LIVE_RUNPOD=1 KINOFORGE_SAVE_FIXTURES=1 pytest …`); commit shows shape delta |
| Fixture refactor changes existing test assertions (masked bug exposure) | medium | low–medium | Each value change reviewed; bugs found → in-scope production fix per AC9 |
| Wan i2v model unavailable / pulled from CivitAI mid-run | low | low | Smoke fails cleanly with `CivitAISource` error; not a Layer N invariant |
| First live run costs > $2 budget cap | low | low | BudgetTracker tears down; smoke fails with `BudgetExceeded`; reproducible-and-debuggable |
| ComfyUI image tag drift (e.g. `runpod/comfyui:latest` changes mid-layer) | medium | low | Pin to a specific tag in YAML at first capture; document tag in PROGRESS |
| Test-count delta breaks PROGRESS-line counters elsewhere | low | low | Update test-count in PROGRESS at AC8 like every prior layer |

## Open knobs (resolved before plan-writing or deferred to plan)

- ComfyUI image tag — Dr. Twinklebrane picks at smoke time; YAML pins whatever ships.
- Wan i2v specific model version — pick from existing `wan.yaml` example; reuse for parity.
- Init-frame PNG content — committed at first live run; small natural image, no identifiable
  people.

## Decisions log (from brainstorming, 2026-05-31)

- **Q1=B**: Read-path shape locking + live opt-in smoke. Pure shape (A) misses E2E; full
  GraphQL contract pinning (C) is overkill for one-cloud scope.
- **Q2=C**: Live smoke runs full E2E (create pod → deploy engine → generate). Bare pod (A) /
  bare pod + env (B) leave the real value-bearing paths untested.
- **Q3=A**: Pod-only. Serverless is a different shape and a different selfterm story (none);
  deferred to follow-up.
- **Q4=A**: ComfyUI + small Wan i2v. Canonical RunPod stack; exercises Phase 7 Task 20a
  end-to-end at the lowest cost per signal.
- **Q5=A**: Fixtures captured during live smoke and committed. First run is dual-purpose:
  validates code + generates fixtures.
- **Q6=A**: Hardcoded budget + GPU cost cap + finally-destroy. Three independent guards.
- **Q7=A**: `find_offers` drives the GPU pick. One live run validates find_offers + create +
  destroy in the same hit.
- **Approach=A** (Layer I-pattern): two test files (offline fixture-locked, live opt-in) +
  shared helper.
