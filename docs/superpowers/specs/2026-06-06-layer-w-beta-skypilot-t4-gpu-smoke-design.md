# Layer W+β — SkyPilot multi-cloud T4 GPU smoke (design)

**Status:** approved 2026-06-06.
**Predecessors:** Phase 31 (SkyPilot CPU lifecycle, GCP — adapter built),
Phase 39 / Layer W+α (cloud bootstrap — perms + GCP quota verified).
**Initial cycle:** GCP only. AWS gated on quota case
`cd3e0e81b66b4055bcc189bbf8653542I2kxtcvR` landing; same test adds AWS at
W+β2 by removing the parametrize skip-mark — no refactor.
**Spend ceiling for this layer:** $0.10 GCP. One 5-min T4 lifecycle.

---

## 1. Goal

Prove the `providers/skypilot/` adapter works end-to-end against a real
T4 GPU instance on GCP. Capture lockdown fixtures. Cheapest possible test
that exercises the GPU path of every adapter method (`find_offers` →
`create_instance` with `accelerators` → poll → SSH → `nvidia-smi` →
`destroy_instance` + 4-tier teardown).

This is the foundation move: future layers stack engines (ComfyUI,
Diffusers, Wan) + multi-cloud (AWS once quota lands) on top of a verified
adapter. Layer N (RunPod) proved this pattern at $0.001/run by deferring
engine smokes and catching 10 production bugs in the adapter alone.

## 2. Out of scope

- **AWS path** — quota case open since Layer W+α T5; deferred to W+β2.
- **Engine deploy** (ComfyUI, Diffusers, Wan) — separable layer that
  stacks on a verified adapter.
- **Azure / B2 / R2** — no creds.
- **Multi-region** — GCP `us-central1` only, matches Layer W+α quota row.

## 3. Files

| Path | Action | Responsibility |
|---|---|---|
| `tests/live/test_skypilot_live.py` | MODIFY | Add 1 parametrized test + helpers (`_t4_smoke_config`, `_ssh_capture_stdout`) |
| `tests/providers/fixtures/skypilot/find_offers_t4.json` | CREATE | Live-captured T4 entries from `sky.list_accelerators()` |
| `tests/providers/fixtures/skypilot/launch_gpu.json` | CREATE | GPU variant of the existing launch fixture |
| `tests/providers/fixtures/skypilot/status_gpu.json` | CREATE | GPU variant of the existing status fixture |
| `tests/providers/test_skypilot.py` | MODIFY | Add `test_t4_fixture_shape` (offline regression on the 3 new fixtures) |
| `examples/configs/skypilot-gpu.yaml` | CREATE | T4:1 example, idle_timeout=180s, run = `nvidia-smi …` |
| `docs/CLOUD-CREDS.md` | MODIFY | Append first-real-artifact line under SkyPilot perms section |
| `PROGRESS.md` | MODIFY | Phase 40 entry |

No changes to `src/kinoforge/providers/skypilot/__init__.py` expected.
If the live smoke surfaces an adapter bug, fixing it lands in the same
phase (carries one extra commit).

## 4. Test architecture

```python
# tests/live/test_skypilot_live.py — new test, alongside the Phase 31 CPU smoke

# Parametrize prepared for AWS already; "aws" added at W+β2 when quota lands.
@pytest.mark.parametrize("cloud", ["gcp"])
def test_skypilot_live_e2e_t4_gpu_lifecycle_smoke(cloud: str) -> None:
    cfg = _t4_smoke_config(cloud=cloud, region="us-central1")
    provider = SkyPilotProvider()

    offers = provider.find_offers(cfg.requirements)
    t4_offers = [o for o in offers if "T4" in (o.gpu_name or "")]
    assert t4_offers, f"no T4 offer surfaced by find_offers; offers={offers!r}"
    offer = t4_offers[0]

    spec = _task_spec(
        cfg, offer,
        run=["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
    )
    inst = provider.create_instance(spec)
    try:
        _wait_ready(provider, inst.id, timeout_s=900)
        stdout = _ssh_capture_stdout(
            inst, cmd="nvidia-smi --query-gpu=name --format=csv,noheader",
            timeout_s=60,
        )
        assert "T4" in stdout, f"expected T4 in nvidia-smi output, got: {stdout!r}"
    finally:
        _teardown(provider, inst.id)  # 4-tier from Phase 31
```

**Why parametrize on day one:** AWS smoke is the next layer's only delta.
Pre-existing parametrize means W+β2 = remove the GCP-only restriction +
land an AWS quota → green run. No structural test changes.

**Why `_ssh_capture_stdout` not the `run:` field:** SkyPilot's `task.run`
runs at provision time and exits; capturing its stdout post-hoc requires
fetching cluster logs from sky's state DB. Direct SSH after `ready` is
simpler, exercises the `endpoints()` adapter method, and gives an exact
return-code-checked assertion surface.

## 5. Smoke flow (4-tier teardown preserved)

1. **Preflight gate** (existing): `KINOFORGE_LIVE_TESTS=1` +
   `GOOGLE_APPLICATION_CREDENTIALS` + `import sky` + zero active sky
   clusters + clean git tree.
2. **find_offers** → assert at least one offer with `T4` in `gpu_name`.
   Pick the first (cheapest if cost is set on offers).
3. **create_instance** with:
   - `accelerators: "T4:1"`
   - `cloud: "gcp"`
   - `region: "us-central1"`
   - `image: "skypilot/skypilot-gpu:latest"` (or sky's default GPU image
     if that ref is wrong; the live run surfaces the correct ref and the
     test pins it)
   - `idle_timeout_s: 180`
   - `boot_timeout_s: 900`
4. **Poll until ready** (cap 15 min — GPU provision slower than CPU).
5. **SSH** to the cluster, run
   `nvidia-smi --query-gpu=name --format=csv,noheader`, capture stdout.
6. **Assert** `"T4"` is a substring of stdout.
7. **Teardown** (4-tier from Phase 31):
   1. `provider.destroy_instance(inst.id)` + poll until gone (cap 5 min).
   2. If still alive: `sky.down(<name>, purge=True)`.
   3. If still alive: `gcloud compute instances delete <name>
      --zone=<zone> --quiet`.
   4. Final assertion: zero matching instances in
      `gcloud compute instances list --filter='name~^sky-'`.

## 6. Error handling

| Failure | Behavior |
|---|---|
| No T4 in `find_offers` | Skip with clear message (`pytest.skip(...)`) — quota row says T4=1 in us-central1; if find_offers doesn't surface T4 it's a sky/region mismatch, not a kinoforge bug. |
| `create_instance` raises | Wrap in try/finally so teardown still runs. Fail with original exception. |
| Poll-until-ready timeout (15 min) | Teardown + fail with last-known cluster status. |
| SSH unreachable | Teardown + fail with sky logs path + cluster name. |
| nvidia-smi exit non-zero | Teardown + fail with stderr + exit code. |
| Teardown tier 1–3 all fail | Final assertion fires → pytest fails. Operator alerted via test output. |

## 7. Fixture capture

The Phase 31 recording seam (mentioned in PROGRESS, line ~602–608)
captures sky SDK calls during the live run. T4 cycle adds 3 fixtures:

- `find_offers_t4.json` — last `sky.list_accelerators()` call's response
  filtered to T4 rows (other accelerators stripped for size).
- `launch_gpu.json` — last `sky.launch()` config dict (resources include
  `accelerators=T4:1`).
- `status_gpu.json` — last `sky.status()` call once cluster is `UP`.

Redaction rules:
- Cluster name `sky-XXXXXX` → `sky-REDACTED`.
- IP addresses → `REDACTED-IP`.
- Cloud zone-specific URLs → `https://REDACTED`.
- Project ID `<GCP_PROJECT>` → `REDACTED-PROJECT`.
- SSH key paths → `~/.ssh/REDACTED.pem`.

## 8. Offline lockdown regression

`tests/providers/test_skypilot.py` gets one new test:

```python
def test_t4_fixture_shape() -> None:
    """Lockdown: T4 fixture shape must match what _FakeSky / SkyPilotProvider expects."""
    fixtures = Path(__file__).parent.parent / "providers" / "fixtures" / "skypilot"
    find_offers_t4 = json.loads((fixtures / "find_offers_t4.json").read_text())
    # Must satisfy SkyPilotProvider's dual-shape parse (modern + legacy).
    assert isinstance(find_offers_t4, (dict, list))
    if isinstance(find_offers_t4, dict):
        assert "T4" in find_offers_t4 or any("T4" in k for k in find_offers_t4)
    launch_gpu = json.loads((fixtures / "launch_gpu.json").read_text())
    assert "T4" in json.dumps(launch_gpu)
    status_gpu = json.loads((fixtures / "status_gpu.json").read_text())
    assert "T4" in json.dumps(status_gpu)
```

This catches sky SDK shape drift in CI before the next live run is
attempted.

## 9. Cost ceiling

| Phase | Duration | Cost (GCP `n1-standard-4 + nvidia-tesla-t4` @ ~$0.35/hr) |
|---|---|---|
| Provision (sky.launch → READY) | 4–7 min | $0.02–$0.04 |
| nvidia-smi + assert | <30 s | $0.003 |
| Teardown (sky.down + verify) | 1–2 min | $0.006–$0.012 |
| **Total per run** | **5–10 min** | **$0.03–$0.06** |
| **Layer budget (1 retry)** | **— ** | **$0.10** |

Project budget impact: ~$10.88 remaining → ~$10.78 after this layer.

## 10. Done criteria

- New parametrized live test passes against GCP T4.
- 3 redacted fixtures committed under `tests/providers/fixtures/skypilot/`.
- Offline `test_t4_fixture_shape` passes in CI.
- `docs/CLOUD-CREDS.md` carries the first-real-artifact line (instance
  ID, region, T4 confirmed via captured nvidia-smi output).
- `PROGRESS.md` Phase 40 entry committed with per-task SHAs + spend
  total + smoke artifact + any adapter fixes that landed.

## 11. Open follow-ups (carried forward — not blockers)

- **AWS arm of the same test** — W+β2, gated on quota approval landing.
- **Engine smoke on top of verified adapter** — ComfyUI / Wan i2v on the
  same T4. Separable layer; recommended next once W+β2 closes the
  multi-cloud row.
- **`accelerators_in_cost` ordering** — if find_offers surfaces multiple
  T4 offers, picking the cheapest by `usd_per_hour` is documented in
  PROGRESS:88 "stable gpu_preference sort" pattern; verify the same
  ordering applies in the GPU branch.
- **Multi-region** — only `us-central1` covered. Other GCP regions (and
  AWS `us-east-1`) deferred.
