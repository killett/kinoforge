# Pod-path seam and CI recovery

Status: design approved 2026-09-21. Implementation plan to follow.

## The problem

CI is red in two unrelated places, and both have been red long enough that the
redness itself is load-bearing evidence: fixes made elsewhere in the project
stopped propagating here.

### 1. `CI` red on `main` since 2026-09-18

Five tests in `tests/engines/test_wan_t2v_server_torch_build_log.py` fail on both
runners:

```
PermissionError: [Errno 13] Permission denied: '/workspace'   # ubuntu-latest
OSError: [Errno 30] Read-only file system: '/workspace'       # macos-latest
```

`wan_t2v_server.py` resolves pod-absolute paths into module constants at import:

| line | constant | fallback |
|------|----------|----------|
| `:50` | `HF_HOME` (via `os.environ.setdefault`) | `/workspace/.hf_cache` |
| `:74` | `ARTIFACT_DIR` | `/workspace/artifacts` |
| `:76` | `LORAS_DIR` | `/workspace/loras` |

`_startup()` then mkdirs both dirs (`:1456`, `:1461`). The new test calls
`server._startup()` in-process without overriding the env, so it tries to create
`/workspace` on a runner.

Two facts make this a class rather than a one-off.

**The dev container cannot reproduce it.** Here `/workspace` *is* the repo root
and is writable, so the suite passes locally — verified: `7 passed in 0.95s`.
It has also been silently spilling into the tree: `/workspace/artifacts/` holds
281 stub files, every one exactly 1647 bytes, dating to 2026-06-21; plus
`/workspace/loras/civitai_A_1.safetensors`. Both gitignored, hence invisible.

**It was already diagnosed once, and fixed in one place only.**
`tests/smoke/local_cpu/conftest.py:53-57` carries the comment *"Defaults to
/workspace/{artifacts,loras} which doesn't exist on CI runners"* and redirects to
`tmp_path`. Because the mitigation was per-test-file, the next test to touch
startup re-broke CI. `minimax_h3_server.py:78` carries the same default and the
same ad-hoc workaround.

The 2026-09-05 break was the same shape: `test_watchdog_arm_idempotency.py`,
macOS-only `setsid` absence. Environment-dependence that only CI can see.

### 2. `smoke-wan21-weekly` red for 10+ consecutive weeks

Red since at least 2026-07-20, the limit of retained logs. Rotating causes, all
on `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`:

- **2026-09-14** (47 s): `RateCapExceeded: realized $0.4900/hr exceeds cap
  $0.4000/hr`. The cfg still reads `max_usd_per_hr: 0.40` — the cap the STATUS
  INDEX records as raised to `0.60` on 2026-09-12 (`2c446eef`) on the sibling
  1.3B grid cfg, for the stated reason that no RunPod offer could satisfy 0.40.
  The fix never propagated here.
- **2026-09-21** (19 min, real spend): `ProvisionFailed: pod 'nn7goq4pu1fmnh'
  boot stalled`. The cfg has no `cloud_type: secure` pin, while its
  `max_lifetime` is 60m and `boot_timeout` 30m — far past the ~10-minute
  community-pool reclamation window (U42).

Cost is roughly $0.15-0.30 a week for zero signal. The job also runs unattended
with none of the live-spend discipline CLAUDE.md mandates: no authorisation in
conversation, no utilisation polling, no frame QA. The `if: always()` leak sweep
inside it was never the safety net; the independent 20-minute `leak-sweep` cron
is, and it is green across the entire red streak.

## The underlying defect

`/workspace` is RunPod's contract, not kinoforge's. The comment at
`wan_t2v_server.py:47-49` says so: *"`/workspace` is the RunPod volume mount
(`volumeInGb` in the cfg's `placement.disk_gb`)"*. Modal mounts its volume at
`/cache/hf` (`providers/modal/_app.py:34`, `__init__.py:283`).

Nothing in `src/`, `tools/` or `examples/` sets `KINOFORGE_ARTIFACT_DIR` or
`KINOFORGE_LORAS_DIR` during provisioning — only one test conftest does. So on
every Modal run `ARTIFACT_DIR` falls back to `/workspace/artifacts`: every
generated video is written to a path named after another provider's volume,
landing on ephemeral container disk rather than the mounted one. It works today
only because artifacts stream straight out via `FileResponse`.

The CI failure and the Modal mis-location are the same defect wearing different
hats: **a provider-specific absolute path hardcoded into provider-agnostic
server code.**

`HF_HOME` is the one case the repo already got right — Modal's provider exports
it explicitly (`__init__.py:288`), so the server's `setdefault` is a harmless
no-op there. `minimax_h3_server.py:52` documents that arrangement. The pattern
exists, is correct, and is applied to one of three paths.

## Design

### Section 1 — the pod-path seam

`InstanceSpec.volume_mount` (`core/interfaces.py:303`) is already the portable
field for this fact, and each provider already owns its default: RunPod resolves
`spec.volume_mount or "/workspace"` (`providers/runpod/__init__.py:1403`), Modal
`spec.volume_mount or "/cache/hf"` (`providers/modal/__init__.py:283`). SkyPilot
and Local are both marked `u` — no volume attached.

New module `core/pod_paths.py`, one pure function:

```python
def pod_path_env(volume_mount: str | None, *, hf_home: str | None = None) -> dict[str, str]
```

Given a resolved mount it returns `KINOFORGE_ARTIFACT_DIR` and
`KINOFORGE_LORAS_DIR` rooted there, plus `HF_HOME` when the caller supplies one.

**`hf_home` is an argument, not a derived layout — corrected 2026-09-21 during
planning.** An earlier draft of this section had the helper root all three vars
uniformly on the mount. That is wrong and would cause real damage: Modal's
`HF_HOME` is the Volume root itself (`/cache/hf`, visible in
`tests/providers/golden/launch_payloads/modal-diffusers-flashvsr-1080p-upscale-long-tiled.json`),
and that Volume is where Sub-project B put a 144 GiB fetch
(`minimax_h3_server.py:50-54`). Deriving `/cache/hf/.hf_cache` would orphan it
and silently re-download 123.8 GiB on the next run. RunPod wants a `.hf_cache`
subdir; Modal wants the root; the difference is real provider policy, so the
providers pass it. The helper owns only the layout that genuinely is shared. Given `None` or `""` it returns
the two dir vars pointed at pod-local scratch and **omits `HF_HOME` entirely**,
so `huggingface_hub`'s own `~/.cache/huggingface` default applies. Inventing a
scratch HF cache would be worse than no opinion: the default is already correct
for a volumeless host, and overriding it would silently discard any warm cache
the image shipped with. No I/O, no provider import, unit-testable offline.

SkyPilot and Local do not call the helper at all — they attach no volume, and the
server's own scratch fallback (Section 2) already produces the right answer for
them. Adding the call would be a no-op that implies a volume exists.

Each volume-carrying provider calls it once, immediately after resolving its own
mount, merging with `setdefault` so an explicit cfg value still wins:

- **RunPod** — where `env` is assembled in `_create_pod`, off the same
  `spec.volume_mount or "/workspace"` already computed for `volumeMountPath`.
- **Modal** — replaces the lone `env.setdefault("HF_HOME", volume_mount)` at
  `:288` with the full trio, off the mount resolved at `:283`.

**Why `HF_HOME` moves with them.** RunPod's cache pinning today depends entirely
on the server's own import-time `setdefault` at `:50` — the line being removed.
If the provider does not start exporting it, a 70 GB Wan shard download lands on
the 250 GB container disk instead of the volume, which is precisely the failure
the comment at `:47` exists to prevent. This is the one place the refactor can do
real damage, and the reason the trio moves together rather than the two dir vars
alone.

Rejected alternative: resolving centrally in `build_instance_spec`.
`spec.volume_mount` is still `""` at that point because the default is the
provider's to supply; making it work would mean hoisting each provider's mount
default into core, inverting the ownership the S1 compute-seam established.

### Section 2 — server-side resolution

Three fallback changes, no shape change:

- `wan_t2v_server.py:50` — the `HF_HOME` setdefault is **deleted**, not
  repointed. The provider is now authoritative on every host that has a volume;
  on a host that has none, `huggingface_hub`'s own default is already right.
- `wan_t2v_server.py:74` — `ARTIFACT_DIR` falls back to `/tmp/kf-artifacts`.
- `wan_t2v_server.py:76` — `LORAS_DIR` to `/tmp/kf-loras`; and
  `minimax_h3_server.py:78` the same treatment as `:74`.

Scratch follows the precedent in the same file: `_UPLOAD_DIR =
Path("/tmp/kf-uploads")` at `:77`, with its `noqa: S108 — pod-local writable
scratch`. So these names are idiom, not invention, and they carry the same noqa.

**These stay module constants.** Eight test files already monkeypatch them as
attributes (`test_diffusers_wan_t2v_server.py`, `test_server_upscale.py`,
`test_lora_http_branch_surface.py`, and five others). Lazy accessors would break
all eight and buy nothing: on a real pod the provider sets these as process env
*before* the server starts, so reading at import is correct. The only consumer
needing post-import mutation is tests, which already patch the attribute.

After this, no import of either server module and no `_startup()` call touches
`/workspace`. The five failing tests pass on a runner **unmodified**, and
`tests/smoke/local_cpu/conftest.py:53-57` — the ad-hoc workaround that named the
problem — becomes redundant and is deleted.

Expected fallout the plan must verify rather than assume: the golden launch
payloads shift, since provisioning env gains three exported vars.
`tests/engines/diffusers/_golden_provision.json` and the launch-payload goldens
under `tests/providers/golden/launch_payloads/` are regenerated via
`tools/snapshot_launch_payloads.py` — **after** formatting, never before.

### Section 3 — recurrence guard

The source fix kills this instance, but the dev container will differ from CI on
`/workspace` writability forever. Two layers stand in for the environment we
cannot reproduce.

**A standing source audit.** `tests/test_source_audit.py` already pairs
`test_no_tracked_file_contains_a_credential` with
`test_audit_fires_on_a_planted_credential` — a guard plus a falsification test
proving the guard catches. Same shape: no module under `src/kinoforge/` may use a
provider volume path as a runtime default, plus a companion that plants one and
proves the audit fires.

Scoping is deliberately narrow. `/workspace` appears legitimately in comments, in
docstrings, and in `providers/runpod/` where it is correct. The audit therefore
matches only a `/workspace` or `/cache/hf` literal in an
`os.environ.get`/`setdefault` fallback position, outside the owning provider.

**An autouse suite-wide fixture** in `tests/conftest.py` pointing the three vars
at `tmp_path`. This is what permanently ends the tree pollution: it holds
regardless of what any individual test does, so the next test to call
`_startup()` cannot spill into the repo root even if the source fix were undone.

**Residual risk, stated plainly.** Neither layer reproduces CI's read-only `/`
locally. A new absolute path spelled differently, built by string concatenation,
or read through a helper rather than `os.environ.get` directly, is invisible to
the audit. The audit is a compensating control, not an equivalent one. This class
becomes catchable offline; the dev/CI environment gap does not close.

### Section 4 — weekly smoke

Config — both edits propagate fixes the project already made elsewhere:

- `max_usd_per_hr: 0.40` -> `0.60`, matching the operator decision of 2026-09-12
  (`2c446eef`) on the sibling 1.3B grid cfg, for the identical reason.
- Add `backend_options.runpod.cloud_type: secure`.

Workflow — drop the `schedule:` block, keep `workflow_dispatch:`. Live spend
returns to being authorised by user statement in conversation, which is what
CLAUDE.md already requires. Nothing is lost: the independent `leak-sweep` cron
remains the safety net it always was.

**These edits are a paper fix until a dispatch run proves them.** They are
derived from evidence, not measured. Whether to spend the ~$0.15-0.30 to prove
the config green is a live-spend call for the operator at execution time, not an
assumption baked into the plan. If that run happens it is a one-shot validation:
`--no-reuse`, utilisation polling during, `kinoforge list` verified after.

### Section 5 — pollution cleanup

Remove `/workspace/artifacts/` (281 stub files, 1.2 MB) and `/workspace/loras/`
(16 KB). Both are gitignored; nothing tracked is at risk. The plan verifies
contents before removing rather than trusting this survey.

Explicitly out of scope: `output/`, the real CLI sink (`core/config.py:1369`,
`cli/_commands.py:4894`), which holds the FlashVSR fixture clip CLAUDE.md names
for upscaler validation.

This is symptom cleanup. Section 3's autouse fixture is what stops recurrence.

## Sequencing

Offline work first, paid work last.

1. Sections 1-2 — the seam and the server fallbacks. Greens CI. No spend.
2. Section 3 — the guard. No spend.
3. Section 5 — cleanup. No spend.
4. Section 4 — the weekly smoke config and workflow edit. No spend to make;
   optional operator-authorised dispatch run to prove.

## Out of scope

Deliberately excluded by operator decision 2026-09-21: the meta-problem that
`main` went red for three days and ten weekly failures accumulated without being
seen. No notification, branch-protection, or local CI-parity gate is designed
here.
