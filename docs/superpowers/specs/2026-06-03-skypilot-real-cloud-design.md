# Design — SkyPilot real-cloud verification (Phase 31)

**Closes:** PROGRESS:114 carry-forward #2 (*"`SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK"*).

**Layer name:** Phase 31 — SkyPilot real-cloud verification.

**Precedent:** Phase 24 (Layer N) — RunPod real-cloud fidelity. Bare pod lifecycle smoke that caught 10 production bugs at ~$0.001/run. Sibling pattern for SDK-based (not HTTP-based) providers.

## 1. Locked decisions

| # | Axis | Pick | Why |
|---|---|---|---|
| 1 | Smoke scope | Bare CPU lifecycle only | Layer-N analog. Smallest available CPU SKU (SkyPilot picks the SKU from a `cpus="1+", memory="2+"`-style spec; expected to land on `e2-micro` or `n1-standard-1` on GCP) exercises every code path the smoke can cover. GPU adds 100× cost for marginal additional signal. |
| 2 | Bug-catch rigor | Capture SDK return shapes to JSON fixtures | Layer-N analog. Fixture diff IS the PR review surface; same mechanism that surfaced 10 RunPod bugs. |
| 3 | Cleanup paranoia | try/finally + `autostop=1` + `preflight` extension + `gcloud` nuclear fallback | Four independent layers. Worst case (process killed mid-launch, sky SDK state corrupt, gcloud broken): `autostop=1` still terminates the VM ~1 min after the orchestrator dies. |
| 4 | Dep placement | Pixi feature env `live-skypilot` | Default `pixi install` stays lean (~500 MB transitive deps avoided). `pixi run -e live-skypilot test-live-skypilot` is the only path that imports the real SDK. |
| 5 | Method coverage | Full chain: `find_offers → create → status → endpoints → destroy` | Mirror Layer N. `find_offers` is where most RunPod bugs lived; likely the same pattern here. |
| 6 | Recording-seam location | Test-scope decorator/proxy (not in production code) | Provider source untouched. All recording logic in `tests/live/test_skypilot_live.py`. Sibling to Layer N's HTTP recorder for the SDK-call surface. |

## 2. Architecture

**Files touched:**

- `tests/live/test_skypilot_live.py` (new, ~210 LOC) — the smoke + recording proxy
- `tests/providers/test_skypilot_recording_proxy.py` (new, ~80 LOC) — unit tests for the proxy and `_to_jsonable` helper, runnable without skypilot installed
- `tests/providers/fixtures/skypilot/*.json` (new dir, 5 files) — captured SDK return shapes from the first successful live run
- `examples/configs/skypilot.yaml` (new, ~80 LOC) — operator-facing config mirroring `wan.yaml` shape
- `pixi.toml` (modified) — add `[feature.live-skypilot.pypi-dependencies]` + `[environments]` entry + `test-live-skypilot` task
- `tools/preflight.py` (modified, ~25 LOC) — extend pod-count check to also scan active SkyPilot clusters via lazy-imported `sky.status()`
- `src/kinoforge/providers/skypilot/__init__.py` (modified, 0-30 LOC, **only if the live run forces it**) — bug-fix lines for SDK shape disagreements the smoke surfaces
- `PROGRESS.md` (modified) — Phase 31 entry; mirror Phase 30/24 shape (per-task SHAs + decisions + live-smoke confirmation block)

**Dependency direction:**

Test code imports the production `SkyPilotProvider` and wraps its `sky_client` attribute with a `_RecordingProxy` defined locally in the test module. Production code never imports test code. Existing `tests/providers/test_skypilot.py` (16 offline tests with a synthetic fake) stays untouched.

**Test/runtime separation:**

`pixi run test` continues to skip `tests/live/` and never loads skypilot. `pixi run -e live-skypilot test-live-skypilot` is the only path that imports the real SDK.

**Cost envelope:**

≤ $0.05 per smoke run (smallest GCP CPU SKU at ~$0.01/hr × < 30 min wall-clock, plus negligible storage/network). Layer-wide ceiling: $0.50 across all dev iterations.

## 3. Components

### 3.1 `_RecordingProxy` (test-local, ~40 LOC)

```python
class _RecordingProxy:
    """Wraps the real `sky` module. Each method call delegates, then
    JSON-serializes the return value to fixtures/skypilot/<method>.json."""

    def __init__(self, real: Any, fixture_dir: Path) -> None: ...
    def __getattr__(self, name: str) -> Callable[..., Any]: ...
    # Returns a wrapper that: calls real.<name>(*a, **kw), serializes
    # result via _to_jsonable(), writes to fixture_dir / f"{name}.json",
    # returns result unchanged.
```

`_to_jsonable(obj)` handles:

- Dataclasses → `dataclasses.asdict(obj)`
- Enums → `obj.value`
- `pathlib.Path` → `str(obj)`
- `datetime` → `obj.isoformat()`
- Unknown SDK objects → `vars(obj)` then recurse; fall back to `repr(obj)`
- Lenient `default=str` final fallback (same pattern Layer N uses for the RunPod recorder)

### 3.2 `tests/live/test_skypilot_live.py` (~210 LOC)

Module-level skip if `KINOFORGE_LIVE_TESTS != "1"` or `GOOGLE_APPLICATION_CREDENTIALS` unset or `import sky` fails (mirrors `tests/live/test_runpod_live.py:33-39`).

Single test function:

```python
def test_skypilot_live_e2e_cpu_lifecycle_smoke() -> None:
    import sky
    provider = SkyPilotProvider(sky_client=_RecordingProxy(sky, FIXTURE_DIR))
    cluster_name = f"kinoforge-skypilot-smoke-{secrets.token_hex(4)}"
    try:
        offers = provider.find_offers(HW_REQS_CPU)
        chosen = filter_offers(offers, ...)[0]
        spec = InstanceSpec(run_id=cluster_name, ...)
        inst = provider.create_instance(spec)
        _poll_until_ready(provider, inst.id, timeout_s=600)
        ep = provider.endpoints(inst)
        assert ep["ssh"].startswith("ssh://")
        listed = provider.list_instances()
        assert any(i.id == inst.id for i in listed)
    finally:
        _teardown(provider, cluster_name)
```

`_teardown` is three independent layers (executed in order, each catches and logs its own exception so step N+1 always runs):

1. `provider.destroy_instance(inst.id)` + poll until gone (3-min timeout).
2. If still present: `sky.down(cluster_name, purge=True)` directly (bypass the provider).
3. If GCP still shows VMs labelled with the cluster: `gcloud compute instances list --filter="labels.skypilot-cluster=<name>"` + `gcloud compute instances delete --quiet` each.

`autostop=1` set during launch is the fourth implicit layer (server-side safety regardless of client state — VM self-terminates after 1 idle minute).

After all teardown attempts, if any VM survives, the test re-raises `RuntimeError("CLEANUP FAILED — manual VM deletion required: <ids>")`.

### 3.3 `examples/configs/skypilot.yaml` (~80 LOC)

Mirror `examples/configs/wan.yaml` shape. Engine = `comfyui` placeholder, with note that engine-level smoke is deferred (Layer N precedent). Compute block:

```yaml
compute:
  provider: skypilot
  region: us-central1
  idle_timeout_s: 60      # → autostop=1 via the lifecycle() mapping
  max_in_flight: 1
```

GCS store block remains commented-out in the example, matching the convention `examples/configs/wan.yaml` uses for all store backends. Operators uncomment + fill in `bucket:` directly. YAML configs do not support `${VAR}` interpolation today (verified: no `expandvars`/`envsubst` in `src/kinoforge/core/config.py`); adding interpolation is out of scope for this layer.

### 3.4 `pixi.toml` delta

```toml
[feature.live-skypilot.pypi-dependencies]
skypilot = { version = "*", extras = ["gcp"] }

[environments]
live-skypilot = { features = ["live-skypilot"] }

[tasks]
test-live-skypilot = { cmd = "python -m pytest tests/live/test_skypilot_live.py -v", env = { KINOFORGE_LIVE_TESTS = "1" } }
```

The `live-skypilot` env inherits the default deps plus the SkyPilot PyPI block. Default `pixi run test` continues to skip live tests and never installs skypilot. Lockfile gains a sibling `pixi.lock` entry for the feature.

### 3.5 `tools/preflight.py` extension (~25 LOC)

Add `_check_no_active_sky_clusters()`:

- Attempt `import sky` inside a try/except. ABSENT → log `"skypilot not installed; skipping"` + return OK (so default-env preflight is not blocked).
- PRESENT → call `sky.status(refresh=False)`. If any cluster has status `UP` or `INIT`, fail with the cluster names listed.

Mirrors the existing `_check_no_active_runpod_pods()`.

### 3.6 `SkyPilotProvider` (only if the live run forces it)

Spec budgets **zero** required changes here. If the smoke surfaces a bug (SDK shape disagreement, attribute access error, missed enum value), the patch lands as its own task in this layer. Layer N caught 10 RunPod bugs; honest expectation here is 0-5, given SkyPilot has a more disciplined Python SDK than RunPod's GraphQL.

## 4. Data flow

```
tests/live/test_skypilot_live.py
│
│ import sky                          # real SDK, only in live-skypilot env
│ proxy = _RecordingProxy(sky, …)
│ provider = SkyPilotProvider(sky_client=proxy)
▼

provider.find_offers(HW_REQS_CPU)
    │
    ├─► proxy.list_accelerators({})  ──►  sky.list_accelerators(…)
    │       │                                  │
    │       │◄─────────── dict result ─────────┘
    │       └─► serialize → fixtures/skypilot/list_accelerators.json
    │
    └─► returns list[Offer]

provider.create_instance(InstanceSpec(run_id=cluster_name, …))
    │
    ├─► proxy.launch(task_config, autostop=1)  ──►  sky.launch(…)
    │       │                                              │
    │       │◄────────── dict {cluster_name, …} ───────────┘
    │       │            (~3-7 min wall-clock; real VM)
    │       └─► serialize → fixtures/skypilot/launch.json
    │
    └─► returns Instance(id=cluster_name, …)

_poll_until_ready(provider, inst.id)   # 5s interval, 600s timeout
    │
    └─► provider.list_instances()
            │
            ├─► proxy.status(refresh=False)  ──►  sky.status(…)
            │       │                                  │
            │       │◄────── list[ClusterRecord] ──────┘
            │       └─► serialize → fixtures/skypilot/status.json
            │           (overwritten each poll; last-state-wins)
            │
            └─► returns list[Instance]

provider.endpoints(inst)               # no API; local format
    └─► {"ssh": f"ssh://{inst.id}"}

provider.destroy_instance(inst.id)
    │
    ├─► proxy.down(cluster_name, purge=True)  ──►  sky.down(…)
    │       │                                          │
    │       │◄────────── return (None|dict) ───────────┘
    │       └─► serialize → fixtures/skypilot/down.json
    │
    └─► polls list_instances() until inst absent

nuclear-fallback (only if explicit destroy didn't finish in 5 min):
gcloud compute instances list --filter="labels.skypilot-cluster=<name>"
    + gcloud compute instances delete --quiet
```

### Fixture freshness model

Every method captures **last-call-wins** to its `.json` file. The smoke is deterministic enough that successive runs produce identical shapes (after volatile stripping). PR reviewers diff the fixture files; the diff IS the review surface (exactly like Layer N).

Stable-key normalisation happens in `_to_jsonable`:

- Strip volatile fields (`launched_at`, `cluster_handle.cluster_name_on_cloud`, GCP instance IDs, IP addresses) → replace with sentinel `"<volatile>"` so PR diffs only show **shape** changes, not run-to-run noise.
- Sort dict keys (`json.dumps(..., sort_keys=True)`) for stable byte order.

## 5. Error handling

Six failure classes, each with explicit policy:

| Class | Trigger | Provider behaviour | Test behaviour |
|---|---|---|---|
| Module missing | `skypilot[gcp]` not installed in active env | `_get_sky()` raises `KinoforgeError("skypilot not installed — install via `pixi run -e live-skypilot …`")` | Module-level `pytest.skip(...)` if `import sky` fails |
| Auth missing | `GOOGLE_APPLICATION_CREDENTIALS` unset OR cred file unreadable OR SA revoked | First `sky.*` call raises `google.auth.exceptions.DefaultCredentialsError` (or 401 surfaced from API) | Module-level `pytest.skip` via `_preflight_check()` calling `google.auth.default()` |
| Quota / region not enabled | GCP project lacks Compute quota in `us-central1` OR Compute API not enabled OR billing not linked | `sky.launch()` raises `sky.exceptions.ResourcesUnavailableError` (or similar — captured shape becomes fixture) | Test FAILS loud (not skipped); message points to `gcloud services enable compute.googleapis.com` + quota console |
| Launch timeout | VM provisioning > 10 min (CPU SKUs normally 1-3 min) | `provider.create_instance` returns normally; ready-poll never reaches `UP` | Test FAILS via `_poll_until_ready` timeout. Teardown runs. Last `status.json` shows the stuck state |
| Mid-test crash | Anything between `create_instance` and the `finally` block | n/a | `finally` block executes the 3-tier teardown. `autostop=1` server-side safety net kicks in even if the whole process is `kill -9`'d |
| Teardown fails entirely | `sky.down()` errored AND `gcloud delete` errored AND VM still appears in `gcloud instances list` | n/a | Test re-raises `RuntimeError("CLEANUP FAILED ...")`. autostop terminates VM ~1 min later regardless |

### Logging

`_log = logging.getLogger(__name__)`. Every state transition logged at INFO:

- `find_offers returned N offers`
- `launching cluster=<name> region=us-central1 cpus=1+ memory=2+ autostop=1`
- `cluster status=<X> elapsed=<Ns>`
- `tearing down via provider.destroy_instance`
- `tearing down via direct sky.down`
- `tearing down via gcloud nuclear`
- `teardown complete cluster=<name>` (or `FAILED ...`)

Pytest captures these per `--capture=no -v` so a failing run produces an actionable timeline.

### What the test does **not** catch

- SDK signature mismatches (`sky.launch(...)` renamed): proxy attribute access raises `AttributeError`, pytest reports it — loud enough.
- Authorisation for billing: assumed already attached.
- Cross-region VPC/firewall edge cases: default VPC only.

## 6. Acceptance criteria

**Hard gates (smoke must pass before layer can ship):**

1. `pixi run -e live-skypilot test-live-skypilot -v` exits 0 against real GCP using the `kinoforge-runner` SA.
2. Test completes in < 10 min wall-clock; actual estimate 5-7 min.
3. Total spend per run ≤ $0.05 (smallest GCP CPU SKU × ~0.5 hr).
4. All 5 fixture files written under `tests/providers/fixtures/skypilot/` with `<volatile>`-stripped, `sort_keys=True` stable JSON.
5. Two successive runs produce **byte-identical** fixtures (no run-to-run noise after volatile stripping).
6. `gcloud compute instances list --project=$(gcloud config get-value project)` returns empty within 2 min of test exit (cleanup verified). The active GCP project is whatever the operator has set via `gcloud config set project`; the smoke does not hard-code a project ID.
7. `pixi run -e live-skypilot preflight` exits 0 before and after the smoke (no leaked cluster state).

**Soft gates (informational, not blocking):**

8. Fixture review surfaces 0-5 shape disagreements with offline assumptions. Each gets a typed task in this layer's plan; if the count exceeds 5, re-spec.
9. PROGRESS.md Phase 31 entry follows the Phase 30 shape (per-task SHAs + decisions + live-smoke confirmation block).

## 7. Explicit scope cuts (deferred, not gaps)

| Cut | Why deferred |
|---|---|
| GPU lifecycle smoke | Decision 1 — bare CPU catches the same code paths at 1/100th the cost. GPU smoke = separate future layer when GPU quota is approved AND a specific GPU-only bug class needs chasing. |
| Engine-on-SkyPilot smoke (ComfyUI/Wan via SkyPilot setup) | Layer N precedent (deferred ComfyUI engine smoke). Cost: $2-5/run. Lower marginal bug-catch over bare lifecycle. Future layer. |
| Multi-cloud verification (AWS, Azure, Lambda Labs) | This layer locks GCP only. SkyPilot's multi-cloud value-prop is structurally exercised the same way per cloud; one cloud suffices to validate our SDK use. |
| Retroactive backfill of offline tests from fixtures | Considered and rejected during Q2. Layer N didn't do it either. Future layer if fixture diff surface proves higher-maintenance than expected. |
| Cross-process recording (kinoforge CLI subprocess invoked by pytest) | Same architectural limit Layer N hit. The smoke runs in-process; CLI smoke is its own layer. |
| `find_offers` accuracy against actual GCP pricing | The smoke validates *shape*, not *correctness* of price/availability data. SkyPilot pulls prices from its own catalog; verifying our filter logic against ground-truth GCP pricing is YAGNI today. |
| Per-region failover testing | Single region (`us-central1`) only. Cross-region is a SkyPilot concern more than a kinoforge concern. |

## 8. Post-layer follow-up updates

After this layer ships, in PROGRESS.md "Known limitations & follow-ups":

- Strike: *"`SkyPilotProvider._get_sky()` lazy path wired but unexercised against real `sky` SDK."*
- Add: *"SkyPilot live smoke is CPU lifecycle only; GPU + engine smokes remain deferred."* — clear next-target marker.

## 9. Testing strategy

### Three concentric rings

| Ring | Tests | Default-run status | Notes |
|---|---|---|---|
| 1 — Offline existing | 16 (unchanged) | pass | `tests/providers/test_skypilot.py`, fake `sky_client`. The live smoke validates what these offline tests assume. |
| 2 — Recording-proxy unit tests | +6 (new) | pass | `tests/providers/test_skypilot_recording_proxy.py`. Pure-Python; no live cost; runs in default `pixi run test`. |
| 3 — Live smoke | +1 (new) | skipped by default | `tests/live/test_skypilot_live.py`. Gated by `KINOFORGE_LIVE_TESTS=1` + creds + skypilot installed. |

Ring 2 ACs:

1. Proxy delegates every method call to underlying object
2. Proxy preserves return value bit-for-bit (no transformation visible to caller)
3. JSON file written to correct path with `<method>.json` name
4. Dataclass return values serialise via `asdict`
5. Enum return values serialise via `.value`
6. Volatile-key sentinel replacement applied (`launched_at` → `<volatile>`)

### Pre-live-spend checklist (CLAUDE.md mandate)

Before invoking the smoke for the first time, in order:

1. `git status` — clean tree (RED scaffold committed first per CLAUDE.md rule).
2. `pixi run -e live-skypilot pre-commit run --all-files` — green.
3. `pixi run -e live-skypilot test` — Rings 1 + 2 green.
4. `pixi run -e live-skypilot preflight` — extended check passes (zero active sky clusters, GOOGLE_APPLICATION_CREDENTIALS reachable, clean tree).
5. Single committed RED scaffold of `test_skypilot_live.py` exists in git **before** the first live invocation (per CLAUDE.md "Commit RED scaffolds before any live spend").

Then: `pixi run -e live-skypilot test-live-skypilot -v`.

### Fixture review protocol

After the first live run produces fixtures:

1. Stage all 5 fixtures (`git add tests/providers/fixtures/skypilot/*.json`).
2. Re-run the smoke to confirm fixtures are stable (no run-to-run diff after volatile stripping).
3. Commit the fixtures in their own commit so the PR diff isolates the captured-shape review surface.
4. Eyeball each fixture for unexpected SDK shape. Anything surprising becomes a `SkyPilotProvider` fix task in this layer.

### Cost log

After every live run, update the Phase 31 PROGRESS.md entry with a `**Live-smoke confirmation**` block (mirroring Phase 30's `KINOFORGE_LIVE_TESTS=1 pixi run test …` block):

- Wall-clock seconds
- Cluster name used
- Estimated cost (cloud-billing pricing × wall-clock)
- Fixture sha256 chain (so PROGRESS.md proves which fixtures the smoke confirmed)

### Total test count delta

Current: 1071 passed, 5 skipped.

Post-layer: **1077 passed, 6 skipped** (3 existing + 2 HF live + 1 SkyPilot live).

If the smoke surfaces N production bugs in `SkyPilotProvider`, each fix lands one or more additional offline regression tests (estimated 0-5 added).

### CI behaviour

`tests/live/test_skypilot_live.py` is skipped on every CI run because `KINOFORGE_LIVE_TESTS` is never set in CI. Same as the four existing live tests. Zero CI cost.

## 10. References

- `docs/superpowers/specs/2026-05-31-layer-n-runpod-fidelity-design.md` — Layer N spec; sibling pattern.
- `tests/live/test_runpod_live.py` — Layer N live smoke; structural template.
- `tests/providers/fixtures/runpod/*.json` — Layer N fixtures; format precedent.
- `tools/preflight.py` — extension target.
- PROGRESS:114 carry-forwards #2 — explicit work this layer closes.
