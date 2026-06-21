# LoRA-Flexible Warm-Reuse Smoke Test Pyramid — Design

**Status:** Brainstormed 2026-06-21. Awaiting implementation plan.

**Goal:** Replace the single-tier, expensive, manually-fired Wan 2.2 14B
LoRA-swap smoke (current `tests/live/test_wan22_lora_warm_reuse.py`,
$1-2/fire, 5 attempts × $2.15 wasted on harness bugs) with a
3-tier + watchdog pyramid: (1) free local CPU smoke via real uvicorn
subprocess + faithful stub pipe, on every PR; (3) cheap weekly live
smoke on RunPod A5000 + Wan 2.1 1.3B Diffusers + 2 single LoRAs,
~$0.20/fire; (4) manual Wan 2.2 14B ops-confidence smoke fired before
tagging a release, ~$1-2/fire. All three tiers share a single
engine-agnostic harness module so future engine adds (C23 ComfyUI,
Wan 3.0, Flux) inherit the kinoforge-internal patterns (UA, api_key,
URLError retry, leak sweep) without rediscovering them.

A separate every-30-min leak-detection cron caps any tier-3/4 pod's
runaway-spend lifetime at 1 hour, defending against the failure mode
that lost $0.63 in this session when a smoke crash defeated its own
`finally` block before the per-id pod_id had been captured.

## Background — what this design replaces

The LoRA-flexible warm-reuse workstream (spec
`2026-06-20-lora-flexible-warm-reuse-design.md`, plan
`2026-06-20-lora-flexible-warm-reuse.md`) shipped 22 of 23 tasks
including pod-side endpoints, matcher, integration helper, CLI
surface, AST invariants, and 5 integration tests. T22 — the single
4-step live smoke against a real RunPod A100 80GB pod with the
Arcane Style Wan 2.2 LoRA pair — fired 5 times across 2026-06-20
22:01-23:34 PT and validated step 1 (cold-boot + plain Wan 2.2 T2V
generation) three times, but steps 2-4 (LoRA-swap matrix) never ran
live because each attempt blocked on a different smoke-harness bug
that was already documented in the kinoforge engine internals:

1. Attempt 1 — `provider.endpoints()` returned an empty port map
   immediately after `kinoforge generate` completed. Fix
   (`dc018a3`): hardcode `https://{pod_id}-{port}.proxy.runpod.net`.
2. Attempt 2 — `_run_cli` crashed mid-cold-boot on a transient
   `urllib.URLError` from RunPod's GraphQL surface before the smoke
   could call `_extract_pod_id`. The pod_id stayed `None`, the
   `finally`'s per-id destroy was a no-op, and a $1.39/hr A100 sat
   idle for 30 min until preflight blocked the next attempt
   ($0.63 wasted). Fix (`f7677b2`): belt-and-suspenders
   `_destroy_all_active_pods()` sweep in `finally`.
3. Attempt 4 — RunPod's `*.proxy.runpod.net` returned HTTP 403 to
   bare proxy URL calls. Fix (`7e55036`): append
   `?api_key=<RUNPOD_API_KEY>` to every request.
4. Attempt 5 — proxy still returned 403 because Cloudflare (which
   fronts `*.proxy.runpod.net`) rejects the default
   `Python-urllib/X.Y` User-Agent. Fix (`7ce3a09`): send
   `User-Agent: kinoforge-smoke/0.1` — same dodge documented at
   `src/kinoforge/engines/diffusers/__init__.py:207-212`.

All 4 patterns were already encoded in
`engines/diffusers/__init__.py` for the orchestrator's
provisioner-path HTTP calls. The T22 smoke didn't inherit them
because there was no shared harness module — every smoke
reinvents the wheel. This design's primary leverage is centralising
those patterns so the next smoke (and every smoke thereafter)
inherits them by import, not by rediscovery.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Tier 1 — local-cpu-smoke                                        │
│  Real uvicorn subprocess; faithful in-memory pipe stub.          │
│  Validates: HTTP contract, pydantic JSON shapes, error routing,  │
│  LRU eviction, disk arithmetic, VRAM-OOM rollback path,          │
│  status codes, the 4 kinoforge-internal patterns end-to-end.     │
│  Runs: every GH Actions PR + `pixi run smoke-local`.             │
│  Cost: $0.                                                       │
└──────────────────────────────────────────────────────────────────┘
                              │
                              │ shared harness module
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  Tier 3 — weekly-wan21-live                                      │
│  Real RunPod A5000 24GB pod, Wan 2.1 1.3B Diffusers, 2 single   │
│  LoRA refs. Same 4-step matrix shape as Tier 1 but with real    │
│  GPU, real diffusers, real downloads.                            │
│  Validates: real-diffusers adapter semantics, real CUDA OOM      │
│  rollback, real proxy + Cloudflare path, real civitai download.  │
│  Runs: GH Actions cron Mon 04:00 PT (12:00 UTC)                  │
│        + `pixi run smoke-21b-live`.                              │
│  Cost: ~$0.20/fire = ~$0.80/month.                               │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  Tier 4 — release-gate-wan22                                     │
│  Manual `pixi run smoke-wan22-live` before tagging a release.    │
│  Real A100 80GB, Wan 2.2 14B, Arcane Style pair.                 │
│  Validates: full headline use case on production stack.          │
│  Cost: ~$1-2/fire, operator-bounded.                             │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│  Leak-detection cron (defense-in-depth, separate workflow)       │
│  GH Actions runs every 30 min: query RunPod for any pod tagged   │
│  `kinoforge-smoke-{tier}` older than the tier's age budget       │
│  → destroy + post issue.                                         │
└──────────────────────────────────────────────────────────────────┘
```

## Tier 1 — local CPU HTTP smoke

**Boundary:** real uvicorn subprocess on a random localhost port,
driven by the same urllib client the live tiers use. Catches
real-socket failure modes (Cloudflare-style header rejection,
real Content-Length, real status codes, concurrent request behaviour
under uvicorn's worker pool) that FastAPI's in-process `TestClient`
skips.

**Stub pipe (`_FaithfulStubPipe`):** tracks adapter state, enforces a
configurable VRAM budget (default 80 000 MB), raises a `RuntimeError`
matching the exact `"CUDA out of memory"` substring the server
matches so the `LoraSwapVramOomError` rollback path runs end-to-end.

```python
class _FaithfulStubPipe:
    _loaded_adapters: list[tuple[str, int]]   # (name, fake_size_mb)
    _vram_budget_mb: int = 80_000             # configurable via env
    _active: list[str]                         # current set_adapters target

    def load_lora_weights(self, path, adapter_name):
        # append; raise OSError if "disk" full (configurable cap)
        ...

    def unload_lora_weights(self):
        self._loaded_adapters.clear()
        self._active = []

    def delete_adapters(self, names):
        self._loaded_adapters = [
            (n, s) for n, s in self._loaded_adapters if n not in names
        ]

    def set_adapters(self, names):
        prospective = sum(
            size for n, size in self._loaded_adapters if n in names
        )
        if prospective > self._vram_budget_mb:
            raise RuntimeError("CUDA out of memory")
        self._active = list(names)
```

**Server hook:** `wan_t2v_server.py::_diffusers_load` reads
`KINOFORGE_DIFFUSERS_LOAD_STUB`; when set, imports + calls that
dotted-path callable instead of `WanPipeline.from_pretrained`. Tests
set it to `tests.smoke.local_cpu.stub_pipe._stub_diffusers_load`.

**Matrix (mirrors Tiers 3/4):**
1. `GET /lora/inventory` → empty
2. `POST /lora/set_stack [A, B]` → `inventory == [A, B]`, adapters active
3. `POST /lora/set_stack [B, C]` → A evicted, C downloaded, `inventory == [B, C]`
4. `POST /lora/set_stack []` → empty, adapters cleared

**Error paths (exclusive to Tier 1; too expensive in live tiers):**
- VRAM budget exceeded → `LoraSwapVramOomError`, rollback to prior
  `_active`, inventory unchanged.
- Tight-disk + LRU eviction insufficient → 507 disk-full response.
- Download URL returns 504 mid-stream → `LoraSwapDownloadError`,
  pod inventory unchanged.
- Pod-unreachable via mid-call uvicorn shutdown → driver retries
  N times then `LoraSwapPodUnreachableError`.

**Where it runs:** CI (GitHub Actions, gates merge) + local on demand
via `pixi run smoke-local`.

## Tier 3 — weekly Wan 2.1 1.3B live

**Cfg:** new `examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`.
Wan 2.1 1.3B is single-transformer (per operator note); LoRAs are
single, not pairs. A5000 24GB is sufficient ($0.20/hr typical),
fallback RTX 4090 / L4. Boot timeout 15m (vs 60m for 14B); job
timeout 5m; budget cap $0.50. 33 frames at 480×480 to keep gen
≤30 s on A5000.

```yaml
engine:
  kind: diffusers
  precision: fp16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"]
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules: ["kinoforge.engines.diffusers.servers"]

models:
  # Confirm HF repo id; fall back to "Wan-AI/Wan2.1-T2V-1.3B" bare ref
  # + an explicit diffusers conversion step if -Diffusers variant
  # does not exist on the Hub.
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
    kind: base
    target: checkpoints

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  mode: pod
  warm_reuse_auto_attach: true
  tags:
    smoke_tier: "kinoforge-smoke-tier-3"
  requirements:
    min_vram_gb: 24
    min_cuda: "12.4"
    max_usd_per_hr: 0.40
    gpu_preference:
      - "NVIDIA RTX A5000"
      - "NVIDIA RTX 4090"
      - "NVIDIA L4"
    disk_gb: 40
  lifecycle:
    idle_timeout: 10m
    job_timeout: 5m
    time_buffer: 2m
    max_lifetime: 30m
    boot_timeout: 15m
    budget: 0.50
    heartbeat_interval_s: 30
    lora_swap_re_probe_after_s: 300

spec:
  model: "Wan2.1-T2V-1.3B-Diffusers"
  pipeline: "WanPipeline"
  scheduler: "UniPCMultistepScheduler"
  width: 480
  height: 480
  num_frames: 33
  fps: 16

# Operator-supplied refs — committed to cfg, NOT in models[] (driven via /lora/set_stack)
smoke:
  lora_a: "<operator-supplied civitai or hf ref — Wan 2.1 1.3B-compatible>"
  lora_b: "<operator-supplied civitai or hf ref — Wan 2.1 1.3B-compatible>"
```

**Matrix (single-LoRA shape):**
1. Cold-boot 0 LoRAs → `kinoforge generate` plain → mp4_1,
   `inventory == []`.
2. `set_stack [A]` → `inventory == [A]` →
   `kinoforge generate --instance-id` → mp4_2 (sha ≠ mp4_1).
3. `set_stack [B]` → `inventory == [B]` (A evicted, B downloaded) →
   `kinoforge generate --instance-id` → mp4_3 (sha ≠ mp4_2).
4. `set_stack []` → `inventory == []` →
   `kinoforge generate --instance-id` → mp4_4 (sha ≠ mp4_3).
5. Pod destroy in `finally` + belt-and-suspenders sweep.

**Trigger:** GH Actions cron `0 12 * * 1` (Monday 04:00 PT =
12:00 UTC) + `workflow_dispatch` + `pixi run smoke-21b-live`.

**Operator action required pre-first-fire:** supply 2 Wan 2.1 1.3B-
compatible single-LoRA refs via Civitai or HF. Per user-scope
memory `fetch-lora-metadata-not-just-ids`, refs come with trigger
word + recommended strength + sampler hints fetched from source
page. Refs committed to the cfg's `smoke.lora_a` + `smoke.lora_b`
fields BEFORE the first cron fire.

**Workflow secrets:** `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`,
`CIVITAI_TOKEN` — must be added to the repo's GH Actions secrets
before the cron is enabled.

## Tier 4 — Wan 2.2 14B release gate

**Scope:** the existing T22 smoke + cfg become Tier 4 verbatim, with
their 4 harness fixes (`dc018a3`, `f7677b2`, `7e55036`, `7ce3a09`)
already absorbed. The Tier 4 test is ~30 lines after the shared
harness extraction — cfg path + matrix invocation + Wan 2.2-
specific post-conditions (4 mp4s, sha distinctness, $2 cap).

**Trigger:** manual only. `pixi run smoke-wan22-live` from the
release checklist. No GH Actions workflow. Aligns with
`feedback_autonomous_no_gates` (live spend pre-authorised by user
statement) and the new `destroy-pods-when-work-is-done` memory.

**Lineage:** step 1 cold-boot validated 3 times during the
2026-06-20 session (mp4s `output/20260620-{221751,231141,233336}_…`).
Steps 2-4 pending the first operator fire under the new tier
scaffold; the scaffolding itself ships without new live spend.

**Move:**
- `tests/live/test_wan22_lora_warm_reuse.py`
  → `tests/smoke/release_wan22/test_lora_swap_matrix.py`
- `examples/configs/wan22-lora-flexible-warm-reuse-smoke.yaml`
  → `examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml`

## Shared harness module

**Layout:**

```
tests/_smoke_harness/
├── __init__.py
├── http.py                   # UA + api_key + URLError retry
├── runpod_lifecycle.py       # proxy URL, leak sweep, stat poller
├── civitai.py                # ref → ArtifactDownloadSpec
├── matrix.py                 # engine-agnostic 4-step runner
├── budget.py                 # cap tracker
└── README.md
```

**`http.py` — single source of truth for kinoforge-internal patterns:**

```python
_PROXY_UA = "kinoforge-smoke/0.1"

def _auth_suffix() -> str:
    """?api_key=<RUNPOD_API_KEY> — required by RunPod proxy."""

def post_json(url: str, body: dict, timeout: int) -> dict:
    """POST with UA + (optional) api_key suffix; retries on URLError."""

def get_json(url: str, timeout: int) -> dict:
    """GET with UA + (optional) api_key suffix; retries on URLError."""
```

The `urllib.error.URLError` retry (attempt 2's silent failure mode)
lives here once. Every smoke tier inherits it.

**`runpod_lifecycle.py`:**

```python
def resolve_proxy_url(pod_id: str, port: int = 8000) -> str:
    """Hardcoded https://{pod_id}-{port}.proxy.runpod.net pattern."""

def destroy_all_active_pods(tag_filter: str | None = None) -> list[str]:
    """Belt-and-suspenders sweep. tag_filter='kinoforge-smoke-tier-3'
    so a tier-3 sweep doesn't reap a tier-4 pod sharing the workspace."""

class PodStatPoller(threading.Thread):
    """GPU util + costPerHr every 90s; writes to a log file."""
```

**`matrix.py` — engine-agnostic 4-step runner:**

```python
@dataclass
class MatrixStep:
    name: str                          # "step-1-cold-boot-0-loras"
    target_stack: list[str]
    expected_inventory: list[str]
    expected_evict: list[str] | None   # None = no assertion
    expected_download: list[str] | None

def run_matrix(
    cfg_path: Path,
    pod_proxy_url: str,
    steps: list[MatrixStep],
    download_specs: dict[str, dict],
    generate_per_step: bool = True,    # Tier 1 toggles False
    sha_distinct_required: bool = True,
) -> MatrixReport:
    """Drive the matrix; return per-step inventory + mp4 sha + timing."""
```

Tier 1 instantiates with `generate_per_step=False` (HTTP-only).
Tiers 3 + 4 use `True`.

**`civitai.py`:**

```python
def resolve(ref: str) -> ArtifactDownloadSpec:
    """civitai:<id>[@<ver>] → {url, headers, filename, size_hint}
    via CivitAISource. Loads .env first."""
```

**`budget.py`:**

```python
class BudgetTracker:
    def __init__(self, cap_usd: float, pod_id: str): ...
    def assert_under_cap(self) -> None:
        """Query live costPerHr × wall-clock; raise if > cap."""
```

**Reuse forward:**
- C23 ComfyUI smoke → `tests/smoke/{local_cpu_comfy,live_comfy}/`
  reusing every module.
- Future Wan 3.0 / Flux smoke → swap cfg + matrix-step definitions;
  harness unchanged.

## Cost guardrails — three layers

**Layer 1 — in-band budget assertion.** Each live tier instantiates
`BudgetTracker(cap_usd=X, pod_id=Y)` and asserts under-cap as a
post-condition. Tier 3 cap $0.30, Tier 4 cap $2.00. Test failure on
exceeded budget → workflow failure → GH notification.

**Layer 2 — cfg-side selfterm dead-man's switch.** Both live cfgs
set `lifecycle.budget: <cap>`. The existing kinoforge selfterm
watcher inside the pod monitors accrued spend and self-terminates
the pod when spend > budget, regardless of orchestrator state.
Enforced even if the GH Actions runner crashes mid-test.

**Layer 3 — independent leak-detection cron.** Separate GH Actions
workflow `leak-sweep.yml` runs `*/30 * * * *`:

```python
# tools/smoke_leak_sweep.py
_AGE_BUDGET = {
    # 45 min ceiling. Tier-3 cfg max_lifetime=30m → 15 min slack
    # for tear-down + RunPod-side stop latency.
    "kinoforge-smoke-tier-3": 0.75,
    # 90 min ceiling. Tier-4 cfg max_lifetime=150m is defensively
    # generous (it was set for the original T22 manual smoke);
    # practical wall-clock is cold-boot 18m + 4 × generate 3m =
    # ~30m + 30m of slack over a typical run. 90 min still beats
    # the failure mode this is defending against (24h idle).
    "kinoforge-smoke-tier-4": 1.50,
    # Untagged ad-hoc pods (manual operator runs outside the
    # smoke tiers): 4 h ceiling.
    None: 4.00,
}

for pod in runpod.list_pods():
    tag = pod.tags.get("smoke_tier")
    if pod.age_hours > _AGE_BUDGET.get(tag, _AGE_BUDGET[None]):
        runpod.destroy(pod.id)
        # Surface via `gh issue create --title ... --body ...`,
        # auth via the workflow's GITHUB_TOKEN. Label "leaked-smoke-pod"
        # so triage filters cleanly. Body includes: pod_id, age_hours,
        # accrued_spend_usd, smoke_tier tag, cost_per_hr, last-known
        # status, the kinoforge destroy command for follow-up.
        post_github_issue(pod_id=pod.id, age=pod.age_hours, ...)
```

Pods get tagged at create time via the cfg's `compute.tags.smoke_tier`
field (Tier 3 cfg sets `kinoforge-smoke-tier-3`, Tier 4 cfg sets
`kinoforge-smoke-tier-4`). The `compute.tags` field is the existing
kinoforge RunPod-provider passthrough — no new orchestrator changes
required.

**Worst case bounded:** the attempt-2 leak (which consumed $0.63
over 30 min before preflight surfaced it) would be caught by
Layer 3 in ≤45 min and produce a GitHub issue with pod_id + spend +
tag — no operator intervention required.

**Monthly cost estimate:** $2-5 steady-state ($0.80 tier-3 weekly
+ ~$1.50 tier-4 monthly + ~$1-2 worst-case leak); bounded ~$10
worst-case with a single leak per week.

## File structure

**New:**

```
tests/_smoke_harness/        # shared harness (Section 5)
tests/smoke/
├── __init__.py
├── conftest.py              # shared fixtures
├── local_cpu/
│   ├── __init__.py
│   ├── conftest.py          # uvicorn subprocess fixture
│   ├── stub_pipe.py         # _FaithfulStubPipe + _stub_diffusers_load
│   └── test_lora_swap_matrix.py
├── live_wan21/
│   ├── __init__.py
│   └── test_lora_swap_matrix.py
└── release_wan22/
    ├── __init__.py
    └── test_lora_swap_matrix.py   # moved from tests/live/test_wan22_lora_warm_reuse.py

examples/configs/
├── wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml     # new
└── wan22-14b-lora-flexible-warm-reuse-release.yaml    # renamed

tools/smoke_leak_sweep.py    # Layer-3 watchdog driver

.github/workflows/
├── smoke-wan21-weekly.yml   # cron: Mon 04:00 PT
└── leak-sweep.yml           # cron: every 30 min

docs/
├── superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md   # this file
└── RELEASE-CHECKLIST.md     # appended: pre-tag smoke-wan22-live block
```

**Modified:**

```
src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
  - _diffusers_load reads KINOFORGE_DIFFUSERS_LOAD_STUB env; when set,
    imports + calls that dotted-path callable instead of WanPipeline.

pixi.toml
  - new tasks: smoke-local, smoke-21b-live, smoke-wan22-live, smoke-leak-sweep

README.md
  - "LoRA-flexible warm-reuse" section gains "Smoke test pyramid" subsection.

PROGRESS.md
  - top-of-file workstream entry references this spec; closes T22 partial-state
    by pointing at the tier-4 scaffold that absorbs it.
```

**Deleted:** `tests/live/test_wan22_lora_warm_reuse.py` (content
moves to `tests/smoke/release_wan22/`).

## Open items requiring operator input

1. **Wan 2.1 1.3B Diffusers HF repo id.** The
   `Wan-AI/Wan2.1-T2V-1.3B-Diffusers` variant may not exist on the
   Hub; the original `Wan-AI/Wan2.1-T2V-1.3B` repo is native
   format. If the -Diffusers variant is absent, the cfg falls back
   to the bare repo + an explicit diffusers conversion step in the
   server bootstrap. Verify before the first Tier-3 fire.

2. **Two Wan 2.1 1.3B-compatible single-LoRA refs.** Operator-
   supplied. Committed to the new cfg's `smoke.lora_a` +
   `smoke.lora_b` fields before the first cron run.

3. **GH repo secrets.** `RUNPOD_API_KEY`, `RUNPOD_TERMINATE_KEY`,
   `CIVITAI_TOKEN` must exist in GitHub Actions secrets before
   `smoke-wan21-weekly.yml` or `leak-sweep.yml` is enabled.
