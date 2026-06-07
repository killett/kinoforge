# Layer W+β — SkyPilot T4 GPU smoke (GCP-only first cycle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove `providers/skypilot/` adapter works end-to-end against a real T4 GPU instance on GCP — captured as a parametrized live test that scales to AWS at W+β2 by removing one skip mark.

**Architecture:** Extend `tests/live/test_skypilot_live.py` with one new parametrized live test sitting next to the existing Phase 31 CPU smoke. New fixtures land in a dedicated `tests/providers/fixtures/skypilot/gpu/` subdir so the GPU recording proxy never overwrites the CPU fixtures. Adapter source `src/kinoforge/providers/skypilot/__init__.py` is expected unchanged; if the live run surfaces a bug, the fix is part of T4 and lands as a separate commit.

**Tech Stack:** Python 3.13 + pytest (`live-skypilot` pixi env) + skypilot[gcp] (already pinned) + `gcloud` CLI (conda-installed in the env) + `subprocess` for SSH stdout capture + the existing `_RecordingProxy`.

**Spec:** `docs/superpowers/specs/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke-design.md`

**Spend ceiling:** $0.10 GCP (target $0.03–$0.06 for one 5–10 min run + one retry).

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `tests/live/test_skypilot_live.py` | MODIFY | Add `HW_REQS_T4`, `_ssh_capture_stdout`, and `test_skypilot_live_e2e_t4_gpu_lifecycle_smoke` (parametrized) |
| `tests/providers/fixtures/skypilot/gpu/launch.json` | CREATE (live capture) | GPU `sky.launch()` return-shape |
| `tests/providers/fixtures/skypilot/gpu/status.json` | CREATE (live capture) | GPU `sky.status()` return-shape |
| `tests/providers/fixtures/skypilot/gpu/down.json` | CREATE (live capture) | GPU `sky.down()` return-shape |
| `tests/providers/fixtures/skypilot/gpu/list_accelerators.json` | CREATE (live capture) | `sky.list_accelerators()` T4 entries |
| `tests/providers/test_skypilot.py` | MODIFY | Add `test_t4_fixture_shape` offline regression (skipif-fixture-missing → unblocks when T4 lands) |
| `examples/configs/skypilot-gpu.yaml` | CREATE | T4:1 documentation example; idle_timeout=180s; nvidia-smi run cmd |
| `docs/CLOUD-CREDS.md` | MODIFY | Append "first real GPU smoke artifact" line under SkyPilot section |
| `PROGRESS.md` | MODIFY | Phase 40 entry + Single-next-action pointer |

`src/kinoforge/providers/skypilot/__init__.py` should be untouched. If the live run surfaces an adapter bug, the fix gets its own commit within T4 and is referenced in the Phase 40 entry.

---

## Task 1 — Helpers + parametrized test scaffold (RED)

**Goal:** Add the GPU smoke test function alongside the existing CPU one. Test must skip cleanly when `KINOFORGE_LIVE_TESTS != "1"` and otherwise wire up the new helpers without making any live calls. Commit before any live spend so a crash mid-T4 doesn't lose the scaffold.

**Files:**
- Modify: `tests/live/test_skypilot_live.py`

**Acceptance Criteria:**
- [ ] New module-level constants: `HW_REQS_T4` (HardwareRequirements with `min_vram_gb=8`, `min_cuda="11.0"`), `_GPU_FIXTURE_DIR = FIXTURE_DIR / "gpu"`, `_GPU_READY_TIMEOUT_S = 900.0`.
- [ ] New helper `_ssh_capture_stdout(cluster_name, cmd, timeout_s)` shells out to `ssh <cluster_name> <cmd>` via `subprocess.run`, returns decoded stdout, raises on non-zero exit.
- [ ] New helper `_t4_smoke_spec(cluster_name, offer)` returns an `InstanceSpec` shaped like the existing CPU spec but with GPU image + GPU autostop.
- [ ] New test `test_skypilot_live_e2e_t4_gpu_lifecycle_smoke(cloud)` parametrized on `cloud=["gcp"]`.
- [ ] Test docstring documents: scope, cost ceiling, the AWS extension path.
- [ ] Offline (no `KINOFORGE_LIVE_TESTS=1`): module-level skip still fires; no spend.
- [ ] Pre-commit clean.

**Verify:**
```
pixi run pytest tests/live/test_skypilot_live.py --collect-only -q
```
Expected: shows the new parametrized test under `gcp` AND collects with skip reason (CPU smoke also skipped). Then run:
```
pixi run pytest tests/live/test_skypilot_live.py -v
```
Expected: both tests SKIP module-level (gate not set).

**Steps:**

- [ ] **Step 1.1: Open the existing live test for reference.**

```bash
sed -n '54,70p' tests/live/test_skypilot_live.py
```

Confirm the imports (`HardwareRequirements`, `InstanceSpec`, `Lifecycle`, `SkyPilotProvider`, `_RecordingProxy`) and constants (`FIXTURE_DIR`, `_POLL_INTERVAL_S`, `_READY_TIMEOUT_S`, `_DESTROY_TIMEOUT_S`, `HW_REQS_CPU`).

- [ ] **Step 1.2: Add GPU-specific constants after the existing CPU constants.**

In `tests/live/test_skypilot_live.py`, locate the block:

```python
HW_REQS_CPU = HardwareRequirements(min_vram_gb=0, min_cuda="0.0")
```

Append directly after it:

```python
# T4 smoke (Layer W+β): 16 GB VRAM, modern CUDA. min_vram_gb=8 is
# safely below the T4's 16 GB and excludes accelerators smaller than T4.
HW_REQS_T4 = HardwareRequirements(min_vram_gb=8, min_cuda="11.0")

_GPU_FIXTURE_DIR = FIXTURE_DIR / "gpu"
_GPU_READY_TIMEOUT_S: float = 900.0  # 15 min — GPU provision slower than CPU
_SSH_TIMEOUT_S: float = 60.0
```

- [ ] **Step 1.3: Add the `_ssh_capture_stdout` helper.**

Insert before the existing `_teardown` function:

```python
def _ssh_capture_stdout(
    cluster_name: str,
    cmd: str,
    timeout_s: float = _SSH_TIMEOUT_S,
) -> str:
    """SSH to ``cluster_name`` (sky-managed ~/.ssh/config) and capture stdout.

    Args:
        cluster_name: SkyPilot cluster name (also the SSH host alias sky
            writes into ``~/.ssh/config`` after a successful launch).
        cmd: Shell command to run on the remote host.
        timeout_s: Wall-clock seconds before the SSH call is killed.

    Returns:
        Decoded stdout (utf-8, trailing newline preserved).

    Raises:
        subprocess.CalledProcessError: SSH exit code was non-zero. The
            captured stderr is included on the exception for debugging.
        subprocess.TimeoutExpired: ``timeout_s`` elapsed before the
            command returned.
    """
    completed = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", cluster_name, cmd],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_s,
    )
    return completed.stdout
```

- [ ] **Step 1.4: Add the `_t4_smoke_spec` helper.**

Insert directly after `_ssh_capture_stdout`:

```python
def _t4_smoke_spec(cluster_name: str, offer: Any) -> InstanceSpec:
    """Build the GPU-smoke ``InstanceSpec`` for a given T4 offer.

    Mirrors the CPU-smoke shape: same Lifecycle pattern (idle_timeout
    drives autostop), tag scoped to this layer, ``run_cmd`` runs
    ``nvidia-smi`` so launch-time logs also carry the T4 confirmation as
    a secondary signal (the primary assertion uses ``_ssh_capture_stdout``).

    Args:
        cluster_name: SkyPilot cluster name (also the SSH host alias).
        offer: A ``ComputeOffer`` with a T4 GPU; usually
            ``provider.find_offers(HW_REQS_T4)[0]``.

    Returns:
        ``InstanceSpec`` ready to pass to ``provider.create_instance``.
    """
    lifecycle = Lifecycle(idle_timeout_s=180, max_lifetime_s=1800)
    return InstanceSpec(
        run_id=cluster_name,
        image="skypilot/skypilot-gpu:latest",
        env={},
        tags={"layer": "layer-w-beta-smoke"},
        lifecycle=lifecycle,
        offer=offer,
        provision_script="",
        run_cmd=["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    )
```

You also need to add `from typing import Any` to the existing imports if it's not present:

```bash
grep -n "from typing" tests/live/test_skypilot_live.py || echo "ABSENT"
```

If absent, add `from typing import Any` to the imports near the top of the file (the section above the module-level skip gate, alongside `from pathlib import Path`).

- [ ] **Step 1.5: Add the parametrized test at the end of the file.**

Append:

```python
@pytest.mark.parametrize("cloud", ["gcp"])
def test_skypilot_live_e2e_t4_gpu_lifecycle_smoke(cloud: str) -> None:
    """End-to-end live smoke: T4 GPU lifecycle via ``providers/skypilot``.

    Layer W+β. Cheapest GPU test that exercises the adapter end-to-end:
    ``find_offers`` (T4 surfaced) → ``create_instance`` (accelerators=T4:1)
    → poll until ready → SSH ``nvidia-smi`` → assert T4 in stdout →
    4-tier teardown.

    Parametrize prepared for AWS at Layer W+β2; today only ``gcp`` is in
    the parameter list because the AWS GPU vCPU quota request
    (``L-DB2E81BA``, AWS case
    ``cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR``) is still pending.

    Cost ceiling: $0.10 GCP per run. Typical: $0.03–$0.06 on
    ``n1-standard-4 + nvidia-tesla-t4`` for ~5–10 min wall-clock.
    """
    cluster_name = f"kinoforge-w-beta-t4-{cloud}-{secrets.token_hex(4)}"
    provider = SkyPilotProvider(sky_client=_RecordingProxy(sky, _GPU_FIXTURE_DIR))

    try:
        offers = provider.find_offers(HW_REQS_T4)
        _log.info("find_offers returned %d offers", len(offers))
        t4_offers = [o for o in offers if "T4" in (getattr(o, "gpu_name", "") or "")]
        if not t4_offers:
            pytest.skip(
                f"no T4 offer surfaced for cloud={cloud!r}; "
                "check sky.list_accelerators() / region quota"
            )
        offer = t4_offers[0]
        _log.info("picked T4 offer: %r", offer)

        spec = _t4_smoke_spec(cluster_name, offer)
        _log.info(
            "launching cluster=%s accelerators=T4:1 autostop=3min", cluster_name
        )
        inst = provider.create_instance(spec)

        _poll_until_ready(provider, inst.id, timeout_s=_GPU_READY_TIMEOUT_S)

        stdout = _ssh_capture_stdout(
            cluster_name, "nvidia-smi --query-gpu=name --format=csv,noheader",
        )
        _log.info("nvidia-smi stdout: %r", stdout)
        assert "T4" in stdout, f"expected T4 in nvidia-smi output, got: {stdout!r}"
    finally:
        _teardown(provider, cluster_name)
```

- [ ] **Step 1.6: Confirm collection + skip behavior offline.**

```bash
pixi run pytest tests/live/test_skypilot_live.py --collect-only -q
```

Expected: 2 tests collected — the existing CPU smoke + the new parametrized T4 smoke shown as `test_skypilot_live_e2e_t4_gpu_lifecycle_smoke[gcp]`.

```bash
pixi run pytest tests/live/test_skypilot_live.py -v
```

Expected: both tests SKIP at module level with reason `"KINOFORGE_LIVE_TESTS=1 required"` (or similar gate reason).

- [ ] **Step 1.7: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/live/test_skypilot_live.py
```

```bash
git add tests/live/test_skypilot_live.py
git commit -m "$(cat <<'EOF'
test(skypilot-live): T4 GPU smoke scaffold (RED) — Layer W+β T1

Parametrized on cloud (["gcp"] today, AWS added at W+β2 by removing
the skip-mark). Adds HW_REQS_T4, _GPU_FIXTURE_DIR, _GPU_READY_TIMEOUT_S,
_ssh_capture_stdout, _t4_smoke_spec helpers. Test currently skips
module-level offline; live spend gated by T3.
EOF
)"
```

---

## Task 2 — GPU example config + offline fixture-shape regression

**Goal:** Land the documentation example (`examples/configs/skypilot-gpu.yaml`) and the offline regression test (`test_t4_fixture_shape`) before any live capture. The regression test is `skipif`-gated on fixture file existence so it's a no-op until T4 captures the real fixtures.

**Files:**
- Create: `examples/configs/skypilot-gpu.yaml`
- Modify: `tests/providers/test_skypilot.py`

**Acceptance Criteria:**
- [ ] `examples/configs/skypilot-gpu.yaml` is a minimal-diff variant of `skypilot.yaml` with `min_vram_gb=8`, `min_cuda="11.0"`, image `skypilot/skypilot-gpu:latest`, and a head-comment pointer to the live smoke.
- [ ] `tests/providers/test_skypilot.py` has new `test_t4_fixture_shape` that reads the 4 GPU fixture files; marked `pytest.mark.skipif(not GPU_FIXTURE_DIR.exists())`.
- [ ] All existing 37 SkyPilot offline tests continue to pass.
- [ ] Pre-commit clean.

**Verify:**
```
pixi run pytest tests/providers/test_skypilot.py -v
```
Expected: 37 pre-existing tests PASS + new `test_t4_fixture_shape` SKIPPED (fixtures don't exist yet).

**Steps:**

- [ ] **Step 2.1: Read the existing CPU example for structure.**

```bash
sed -n '1,60p' examples/configs/skypilot.yaml
```

Note the headings: `engine`, `models`, `compute`, `compute.requirements`, `compute.lifecycle`. The new file mirrors this with two-line deltas only.

- [ ] **Step 2.2: Write `examples/configs/skypilot-gpu.yaml`.**

```yaml
# kinoforge example: SkyPilot (T4 GPU lifecycle smoke) — Layer W+β
#
# Demonstrates the bare SkyPilotProvider configuration used by the
# Layer W+β real-cloud verification smoke (spec
# docs/superpowers/specs/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke-design.md).
#
#   - Cheapest GCP T4 SKU (n1-standard-4 + nvidia-tesla-t4 in us-central1)
#   - autostop = 3 min, max_lifetime = 30 min
#   - run command is nvidia-smi so launch-time logs carry the T4
#     confirmation as a secondary signal alongside the SSH stdout
#     assertion in tests/live/test_skypilot_live.py.
#   - 4-tier teardown shared with the Phase 31 CPU smoke.
#
# Cost envelope: ~$0.03–$0.06 per smoke run (5–10 min wall-clock),
# layer ceiling $0.10.

engine:
  kind: comfyui
  precision: fp16
  comfyui:
    version: "0.3.10"

models:
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B:wan2.2_14b.safetensors"
    kind: base
    target: checkpoints

compute:
  # SkyPilot picks the concrete SKU from accelerator + memory
  # requirements at launch time. min_vram_gb=8 forces a GPU with ≥ 8 GB
  # VRAM (T4 has 16 GB and clears the bar; weaker accelerators are
  # excluded). The smoke test pins region us-central1 explicitly; this
  # YAML does not (ComputeConfig has no region field today).
  provider: skypilot
  image: "skypilot/skypilot-gpu:latest"
  mode: pod
  requirements:
    min_vram_gb: 8
    min_cuda: "11.0"
    max_usd_per_hr: 1.00
    disk_gb: 100
  lifecycle:
    idle_timeout_s: 180
    max_lifetime_s: 1800
    job_timeout_s: 600
    boot_timeout_s: 900
    budget_usd: 0.10
```

- [ ] **Step 2.3: Add the fixture-shape regression test.**

Open `tests/providers/test_skypilot.py`. Locate the top-of-file imports and module-level constants. Add near them:

```python
GPU_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "skypilot" / "gpu"
```

(Use the existing `Path` import; do NOT duplicate the import.)

Then append to the end of the file:

```python
@pytest.mark.skipif(
    not GPU_FIXTURE_DIR.exists(),
    reason="T4 GPU fixtures not captured yet — Layer W+β T4 will land them",
)
def test_t4_fixture_shape() -> None:
    """Lockdown: GPU fixtures must satisfy SkyPilotProvider's dual-shape parse.

    Lands after Layer W+β T4 captures the fixtures. Catches sky SDK shape
    drift before the next live run is attempted.
    """
    list_accel = json.loads((GPU_FIXTURE_DIR / "list_accelerators.json").read_text())
    # SkyPilotProvider handles both dict (modern) and flat-list (legacy) shapes.
    assert isinstance(list_accel, (dict, list)), \
        f"unexpected list_accelerators shape: {type(list_accel)}"
    blob = json.dumps(list_accel)
    assert "T4" in blob, "T4 not present in list_accelerators fixture"

    launch_blob = json.dumps(json.loads((GPU_FIXTURE_DIR / "launch.json").read_text()))
    assert "T4" in launch_blob, "T4 not present in launch fixture"

    status_blob = json.dumps(json.loads((GPU_FIXTURE_DIR / "status.json").read_text()))
    # The status response after a healthy launch should reference the cluster
    # name pattern this smoke uses. "kinoforge-w-beta-t4" is the prefix in
    # test_skypilot_live._t4_smoke_spec.
    assert "kinoforge-w-beta-t4" in status_blob or "T4" in status_blob, \
        "status fixture neither names the cluster nor mentions T4"

    # down.json may be sparse; just confirm it's valid JSON (already loaded above
    # would be missing — explicit load here for parity).
    down_obj = json.loads((GPU_FIXTURE_DIR / "down.json").read_text())
    assert down_obj is not None
```

If `import json` is not already present at the top of the file, add it.

- [ ] **Step 2.4: Run the regression test.**

```bash
pixi run pytest tests/providers/test_skypilot.py::test_t4_fixture_shape -v
```

Expected: SKIPPED with reason "T4 GPU fixtures not captured yet — Layer W+β T4 will land them".

```bash
pixi run pytest tests/providers/test_skypilot.py -v 2>&1 | tail -5
```

Expected: 37 PASSED + 1 SKIPPED (the new test) or similar — no regressions.

- [ ] **Step 2.5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files examples/configs/skypilot-gpu.yaml tests/providers/test_skypilot.py
```

```bash
git add examples/configs/skypilot-gpu.yaml tests/providers/test_skypilot.py
git commit -m "$(cat <<'EOF'
test(skypilot): T4 fixture-shape regression + GPU example config

Layer W+β T2. test_t4_fixture_shape skipif-gated on fixture dir
existence so it's a no-op until T4 captures the live fixtures.
examples/configs/skypilot-gpu.yaml documents the T4 smoke config
shape with idle_timeout=180s, budget=$0.10.
EOF
)"
```

---

## Task 3 — Pre-spend gate

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it. Operator confirms the pre-spend checklist green before T4 makes any live SDK call.

**Goal:** Verify preflight green, zero active sky clusters, working tree clean. Operator explicitly approves the live spend in chat.

**Files:** (no file changes — verification only)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0.
- [ ] `pixi run -e live-skypilot sky status` shows no active clusters.
- [ ] `git status --short` shows no uncommitted changes (T1 + T2 scaffold is committed).
- [ ] Operator says "go" in chat.

**Verify:** Sequence of the three commands below + chat confirmation.

**Steps:**

- [ ] **Step 3.1: Run preflight.**

```bash
pixi run preflight
```

Expected: exit 0. If a check fails (creds missing, active pod, dirty tree), STOP and surface the gap.

- [ ] **Step 3.2: Confirm zero active sky clusters.**

```bash
pixi run -e live-skypilot sky status 2>&1 | tail -5
```

Expected: no active clusters reported (output may say "No clusters" or similar). If a cluster is active from a previous session, run `pixi run -e live-skypilot sky down <name> --purge` first.

- [ ] **Step 3.3: Confirm clean tree.**

```bash
git status --short
```

Expected: empty output. If anything is uncommitted, STOP and commit first.

- [ ] **Step 3.4: Ask operator.**

Post in chat:

> Pre-spend checklist green: preflight 0, zero sky clusters, clean tree.
> Ready to fire live T4 smoke (~$0.03–$0.06, 5–10 min). Reply "go" to proceed.

Wait for explicit "go". Do NOT proceed to T4 without it.

---

## Task 4 — Live GCP T4 smoke + fixture capture

**Goal:** Run the live smoke once. Capture the 4 redacted GPU fixtures. Confirm nvidia-smi output contains "T4". 4-tier teardown clean.

**Files:**
- Create: `tests/providers/fixtures/skypilot/gpu/launch.json`
- Create: `tests/providers/fixtures/skypilot/gpu/status.json`
- Create: `tests/providers/fixtures/skypilot/gpu/down.json`
- Create: `tests/providers/fixtures/skypilot/gpu/list_accelerators.json`

**Acceptance Criteria:**
- [ ] `KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest tests/live/test_skypilot_live.py::test_skypilot_live_e2e_t4_gpu_lifecycle_smoke -v` PASSES.
- [ ] Spend ≤ $0.10 (target $0.03–$0.06).
- [ ] Captured nvidia-smi stdout (in the test log) shows "T4" substring.
- [ ] 4 fixtures committed; each redacted per spec §7 (cluster name → `sky-REDACTED`, IPs → `REDACTED-IP`, project ID → `REDACTED-PROJECT`, SSH key paths → `~/.ssh/REDACTED.pem`, zones → `REDACTED-ZONE`).
- [ ] `gcloud compute instances list --filter='name~^sky-'` returns empty after teardown.
- [ ] `pixi run pytest tests/providers/test_skypilot.py::test_t4_fixture_shape -v` now PASSES (the T2 skipif gate releases).

**Verify:**
```
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest tests/live/test_skypilot_live.py::test_skypilot_live_e2e_t4_gpu_lifecycle_smoke -v
pixi run pytest tests/providers/test_skypilot.py::test_t4_fixture_shape -v
pixi run -e live-skypilot gcloud compute instances list --filter='name~^sky-'
```

Expected: first two PASS, third returns empty.

**Steps:**

- [ ] **Step 4.1: Ensure fixture dir exists pre-launch.**

```bash
mkdir -p tests/providers/fixtures/skypilot/gpu
```

(The recording proxy writes to this dir as `sky.*` calls happen.)

- [ ] **Step 4.2: Fire the live test.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-skypilot pytest \
  tests/live/test_skypilot_live.py::test_skypilot_live_e2e_t4_gpu_lifecycle_smoke \
  -v -s 2>&1 | tee /tmp/w-beta-t4-run.log
```

The `-s` flag streams stdout so you watch the lifecycle in real time. Expected wall-clock: 5–10 min total.

While running, in another terminal/session you can monitor:

```bash
pixi run -e live-skypilot gcloud compute instances list \
  --filter='labels.skypilot-cluster~kinoforge-w-beta-t4' \
  --format='table(name,zone,status)'
```

- [ ] **Step 4.3: Handle the three likely outcome paths.**

**Path A — PASS:** continue to Step 4.4.

**Path B — `pytest.skip` "no T4 offer surfaced":** `sky.list_accelerators()` did not return a T4 entry for the active region. Diagnose:

```bash
pixi run -e live-skypilot python -c "
import sky
accels = sky.list_accelerators(gpus_only=True)
print('keys:', list(accels.keys())[:20] if isinstance(accels, dict) else 'list')
print('T4 present:', any('T4' in str(k) for k in (accels if isinstance(accels, list) else accels.keys())))
"
```

If T4 is absent under any reasonable filter, check that the `live-skypilot` env is pointed at GCP and that the SA has `compute.viewer` (Layer W+α verified this — re-run `pixi run cloud:perms-probe --cloud gcp` to confirm). Adjust the test's filter logic only if a real shape mismatch is found.

**Path C — `create_instance` raises or `_poll_until_ready` times out:** read the cluster's sky log path from the exception, capture the relevant stack, run `pixi run -e live-skypilot sky logs <cluster_name>` if it still exists, and:

```bash
# Tier-3 nuclear cleanup if teardown didn't run
pixi run -e live-skypilot gcloud compute instances list \
  --filter='labels.skypilot-cluster~kinoforge-w-beta-t4' \
  --format='value(name,zone)'
# For each survivor:
# pixi run -e live-skypilot gcloud compute instances delete <name> --zone=<zone> --quiet
```

If the failure is a kinoforge adapter bug (e.g., GPU offer path mishandled), commit the fix as its own commit before re-running. Document in the Phase 40 entry under T7.

- [ ] **Step 4.4: Confirm fixtures landed.**

```bash
ls -la tests/providers/fixtures/skypilot/gpu/
```

Expected: at least `launch.json`, `status.json`, `down.json`, `list_accelerators.json` present (the recorder writes one file per distinct sky method called).

- [ ] **Step 4.5: Redact each fixture in place.**

For each of the 4 fixture files, apply the redaction rules:

```bash
pixi run python -c "
import json, re
from pathlib import Path

PROJECT_ID = '<EXACT-PROJECT-ID-FROM-.gcp/kinoforge-sa.json>'
CLUSTER_PREFIX = 'kinoforge-w-beta-t4-gcp-'

# Patterns to redact.
IP_RE = re.compile(r'\b(\d{1,3}\.){3}\d{1,3}\b')
ZONE_RE = re.compile(r'us-central1-[a-z]')
SSH_KEY_RE = re.compile(r'/[^\"\\s]+\.pem')

def redact(s: str) -> str:
    s = s.replace(PROJECT_ID, 'REDACTED-PROJECT')
    s = IP_RE.sub('REDACTED-IP', s)
    s = ZONE_RE.sub('REDACTED-ZONE', s)
    s = SSH_KEY_RE.sub('~/.ssh/REDACTED.pem', s)
    # Cluster names — sky writes 'sky-<8hex>-<userhash>' as the on-cloud name.
    s = re.sub(r'sky-[0-9a-f]{4,}-[0-9a-f]+', 'sky-REDACTED', s)
    s = re.sub(rf'{CLUSTER_PREFIX}[0-9a-f]+', f'{CLUSTER_PREFIX}REDACTED', s)
    return s

for p in Path('tests/providers/fixtures/skypilot/gpu').glob('*.json'):
    text = p.read_text()
    p.write_text(redact(text))
    print('redacted', p)
"
```

Replace `<EXACT-PROJECT-ID-FROM-.gcp/kinoforge-sa.json>` with the literal project id (read via `pixi run python -c "import json; print(json.load(open('.gcp/kinoforge-sa.json'))['project_id'])"`).

- [ ] **Step 4.6: Sanity-check redaction.**

```bash
grep -E "<GCP_PROJECT>|^[0-9]{1,3}\." tests/providers/fixtures/skypilot/gpu/*.json | head -5
```

Expected: empty output (no project id leaks, no IPs). If anything leaks, extend the redaction pass.

- [ ] **Step 4.7: Run the now-unblocked offline regression.**

```bash
pixi run pytest tests/providers/test_skypilot.py::test_t4_fixture_shape -v
```

Expected: PASS (fixture dir now exists; T4 substring present).

- [ ] **Step 4.8: Confirm clean GCP state.**

```bash
pixi run -e live-skypilot gcloud compute instances list --filter='name~^sky-'
```

Expected: `Listed 0 items.` or empty.

- [ ] **Step 4.9: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/providers/fixtures/skypilot/gpu/launch.json \
  tests/providers/fixtures/skypilot/gpu/status.json \
  tests/providers/fixtures/skypilot/gpu/down.json \
  tests/providers/fixtures/skypilot/gpu/list_accelerators.json
```

```bash
git add tests/providers/fixtures/skypilot/gpu/
git commit -m "$(cat <<'EOF'
test(skypilot): captured T4 GPU live-smoke fixtures — Layer W+β T4

4 redacted fixtures (launch / status / down / list_accelerators) from
a real GCP T4 smoke run. nvidia-smi confirmed "Tesla T4" via SSH;
teardown clean. Spend: $<actual>. Recorded by _RecordingProxy at
tests/live/_skypilot_recorder.py.
EOF
)"
```

If an adapter bug fix landed mid-task, commit it separately first with a `fix(providers/skypilot): ...` message and reference the bug in the Phase 40 entry.

---

## Task 5 — Docs + Phase 40 + commit

**Goal:** Land the first-real-artifact line in `docs/CLOUD-CREDS.md`, write the PROGRESS Phase 40 entry, update the single-next-action block, run the full test gate.

**Files:**
- Modify: `docs/CLOUD-CREDS.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] `docs/CLOUD-CREDS.md` appendix carries: instance ID (redacted), region, T4 confirmed via nvidia-smi, actual cost, git SHA of fixtures commit.
- [ ] `PROGRESS.md` Phase 40 entry: per-task SHAs (T1–T5), first-real-artifact line, design decisions, deferred-to-W+β2 list.
- [ ] PROGRESS single-next-action block updated: W+β2 (AWS arm), gated on quota approval.
- [ ] `pixi run pre-commit run --all-files` exits 0.
- [ ] `pixi run test` exits 0 (full suite, including the newly-unblocked `test_t4_fixture_shape`).
- [ ] Pre-commit clean.

**Verify:**
```
pixi run pre-commit run --all-files
pixi run test
```

**Steps:**

- [ ] **Step 5.1: Append the artifact line to `docs/CLOUD-CREDS.md`.**

Add at the end of the existing "SkyPilot check — captured output" appendix (or in the SkyPilot permissions section — whichever fits the project's evolving structure):

```markdown
### First real artifact (Layer W+β GCP T4 smoke)

Captured <ISO timestamp>. Cluster name: `kinoforge-w-beta-t4-gcp-<REDACTED>`.
Region: GCP `us-central1`. Instance shape: `n1-standard-4 + nvidia-tesla-t4`.
`nvidia-smi --query-gpu=name` returned `Tesla T4`. Total wall-clock:
<N min>. Spend: $<actual>. Fixtures captured at
`tests/providers/fixtures/skypilot/gpu/{launch,status,down,list_accelerators}.json`,
committed at SHA `<T4 SHA>`.

Teardown clean: `gcloud compute instances list --filter='name~^sky-'`
returned empty. No survivors.
```

Replace the angle-bracket placeholders with literal values from the live run.

- [ ] **Step 5.2: Add the Phase 40 entry to `PROGRESS.md`.**

Append after the Phase 39 entry:

```markdown
### Phase 40 — Layer W+β (SkyPilot T4 GPU smoke, GCP-only first cycle)

First GPU lifecycle of the `providers/skypilot/` adapter against real
hardware. AWS arm deferred until the Layer W+α quota case lands. Spec:
`docs/superpowers/specs/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke-design.md`.
Plan: `docs/superpowers/plans/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke.md`.

- [x] Task 1: Helpers + parametrized scaffold (RED) — commit `<T1 SHA>`
- [x] Task 2: GPU example config + offline fixture-shape regression — commit `<T2 SHA>`
- [x] Task 3: Pre-spend gate — operator approved in chat
- [x] Task 4: Live GCP T4 smoke + fixture capture — commit `<T4 SHA>`
       (+ adapter fix at `<bug SHA>` if applicable)
- [x] Task 5: CLOUD-CREDS + this entry + final gate — commit `<T5 SHA>`

**Key design decisions:**

- **Parametrized on day one.** The test takes a `cloud` argument with
  `["gcp"]` initially. Layer W+β2 extends to AWS by appending `"aws"` to
  the list — no structural refactor.
- **Bare T4 lifecycle, no engine.** Mirrors the Layer N (RunPod) pattern:
  cheapest test that exercises every adapter method against real
  hardware. Engine smoke is a separable layer that stacks on top of the
  verified adapter.
- **Dedicated `gpu/` fixture subdir.** `_RecordingProxy` writes one file
  per sky method name; using `tests/providers/fixtures/skypilot/gpu/` for
  the GPU smoke keeps the existing CPU fixtures unchanged.
- **SSH stdout capture instead of `task.run` log fetch.** `_ssh_capture_stdout`
  is dead simple (sky writes `~/.ssh/config` after launch) and gives an
  exact return-code-checked assertion surface.
- **4-tier teardown preserved.** Phase 31's `_teardown` reused verbatim;
  no GCP-side cleanup divergence between CPU and GPU paths.

**First real artifact:** Cluster `kinoforge-w-beta-t4-gcp-<REDACTED>` on
`n1-standard-4 + nvidia-tesla-t4` in GCP `us-central1`. `nvidia-smi`
returned `Tesla T4`. Spend $<actual>. Fixtures captured at SHA `<T4 SHA>`.

**Spend total:** $<actual> (target $0.03–$0.06; ceiling $0.10).

**Deferred to W+β2 / later layers:**
- AWS arm of the same parametrized test — gated on case
  `cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` approval.
- Engine smoke (ComfyUI / Wan i2v) on a verified adapter — separable
  layer that stacks on this one.
- Multi-region (other GCP regions; AWS `us-east-1`).
- Azure / B2 / R2 enablement.
- `accelerators_in_cost` ordering verification on the GPU branch (the
  CPU branch's `gpu_preference` sort pattern probably applies; not
  formally re-exercised here).
```

Replace placeholders with the actual SHAs as commits land.

- [ ] **Step 5.3: Update the PROGRESS single-next-action block.**

Edit the "RESUME — START HERE" block at the top of `PROGRESS.md` to point at W+β2 (AWS arm) as the next layer, with a note that it's gated on the AWS quota case landing. Mirror the existing structure (where-we-are paragraph, read-in-this-order list, first-unchecked-task line, budget remaining).

Example replacement text:

```markdown
**Where we are:** Phase 40 fully CLOSED — Layer W+β (GCP-only T4 GPU
lifecycle smoke). HEAD at the T5 docs commit on `main`. Test suite at
~1509 + new T4 fixture-shape regression. Real T4 smoke captured at
SHA `<T4 SHA>`. Working tree clean.

**Read in this order:**
1. The Phase 40 entry below (per-task SHAs + real artifact + design decisions).
2. `docs/superpowers/specs/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke-design.md`.
3. `git log --oneline -15` for recent commits.

**First unchecked task in fresh session:** Layer W+β2 — AWS arm of the
same parametrized test. Gated on AWS Service Quotas case
`cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` (`L-DB2E81BA`, Running
On-Demand G/VT vCPUs ≥ 4 in `us-east-1`) landing. Check status:

    pixi run cloud:perms-probe --cloud aws

Exit 0 means quota landed; add `"aws"` to the parametrize list, fix the
`skypilot[aws]` pip pin path, run T1+T2 equivalents, fire the live smoke.

**Budget remaining: ~$<10.88 - actual W+β spend>** of $15.
```

- [ ] **Step 5.4: Final gate.**

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run cloud:perms-probe
```

Expected: first two exit 0; third exits 0 (GCP) / 2 (AWS quota gap pending).

- [ ] **Step 5.5: Commit + SHA backfill.**

```bash
git add docs/CLOUD-CREDS.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(layer-w-beta): CLOUD-CREDS + PROGRESS Phase 40

Layer W+β T5. First T4 GPU lifecycle of providers/skypilot against
real GCP hardware. Cluster name redacted. Spend $<actual>. Fixtures
at SHA <T4 SHA>. Single-next-action points to W+β2 (AWS arm) gated
on quota case landing.
EOF
)"
```

Then optionally backfill T5 SHA into the PROGRESS entry:

```bash
# Edit PROGRESS.md to replace <T5 SHA> with the actual SHA, then:
git add PROGRESS.md
git commit -m "chore(progress): backfill Layer W+β T5 SHA"
```

---

## Self-Review

**1. Spec coverage.**

| Spec section | Plan task |
|---|---|
| §3 Files: `tests/live/test_skypilot_live.py` modification | T1 |
| §3 Files: `examples/configs/skypilot-gpu.yaml` | T2 |
| §3 Files: `tests/providers/test_skypilot.py` modification | T2 |
| §3 Files: 4 fixtures under `tests/providers/fixtures/skypilot/gpu/` | T4 |
| §3 Files: `docs/CLOUD-CREDS.md` | T5 |
| §3 Files: `PROGRESS.md` | T5 |
| §4 Test architecture (parametrize, helpers, find_offers→create→ssh→teardown) | T1 (scaffold) + T4 (live) |
| §5 Smoke flow (preflight, find_offers, create, poll, SSH, assert, teardown) | T3 + T4 |
| §6 Error handling (no T4 → skip; create raises → try/finally; teardown 4-tier) | T1 (try/finally) + Phase 31 `_teardown` reused |
| §7 Fixture redaction rules | T4 Step 4.5 |
| §8 Offline lockdown regression | T2 |
| §9 Cost ceiling $0.10 | T3 (gate) + T4 (actual capture) |
| §10 Done criteria | T5 |

All spec sections covered.

**2. Placeholder scan.** Each step has either complete code or a complete shell command. The only legitimate angle-bracket markers (`<T1 SHA>`, `<actual>`, `<ISO timestamp>`, `<EXACT-PROJECT-ID-FROM-...>`) are post-hoc value substitutions tied to the live run — they MUST be replaced with literal values when the operator runs Step 5.1/5.2.

**3. Type consistency.**
- `HW_REQS_T4` consistent T1 → T2 (config has identical `min_vram_gb` + `min_cuda`).
- `_ssh_capture_stdout(cluster_name, cmd, timeout_s)` signature consistent T1 → T4.
- `_t4_smoke_spec(cluster_name, offer)` returns `InstanceSpec` shape matching `provider.create_instance` argument.
- `_GPU_FIXTURE_DIR` path `tests/providers/fixtures/skypilot/gpu/` matches Step 4.1's `mkdir -p` and T2's `GPU_FIXTURE_DIR` constant.
- `kinoforge-w-beta-t4-` cluster name prefix consistent across T1 (test creates), T2 (assertion), T4 (redaction).

All consistent.

**4. Operator-action minimization.**
- T1, T2, T4 (post-fire), T5: zero operator interaction beyond chat acks.
- T3: one chat ack ("go").
- T4 Step 4.2 fires the live test; no operator click. Step 4.5 redacts mechanically.
- Total operator chat-actions: 1 ("go" at T3).

---

**Plan saved to:** `docs/superpowers/plans/2026-06-06-layer-w-beta-skypilot-t4-gpu-smoke.md`
