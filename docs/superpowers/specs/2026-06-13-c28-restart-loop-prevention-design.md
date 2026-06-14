# C28 — RunPod container-restart-loop prevention

**Status:** DESIGN (brainstorm-validated; pre-plan).
**Date:** 2026-06-13.
**Slug:** `c28-restart-loop-prevention`.
**Predecessors:**
- `2026-06-13-c25-runpod-heartbeat-preserve-and-merge-design.md`
- `2026-06-13-c26-runpod-util-aware-stall-classify-design.md`
- `2026-06-13-c27-restart-loop-stall-detection-design.md`

C27 ships the DETECTION layer for chronic container-restart loops on
RunPod (`Verdict.RESTART_LOOP_REAP`, `_update_uptime_counter`,
`_restart_loop_reap_predicate`). C28 ships the PREVENTION layer:
diagnose the root cause of the container-exit-non-zero events that
RunPod's default restart policy converts into the loop, then ship
the structural fixes the diagnostic data justifies.

---

## 1. Problem

Every recent Wan + ComfyUI cold-pod boot regresses into a
container-restart loop. C27 Phase B sidecar
(`tests/live/_c27_phase_b_evidence.json`) reproduced the symptom on
real Wan 2.1 14B T2V: gen1 ran 356.8 s before C27 self-fired
RESTART_LOOP_REAP, the pod was destroyed, and the orchestrator raised
`Cancelled`. Detection works; generation does not.

C26 Phase B sidecar (`tests/live/_c26_phase_b_smoke_evidence.json`)
captured the same shape earlier: `uptime_seconds=1` on every util tick,
`gpu_util_percent=0`, `cpu_percent=13` — the container is restarting
faster than the heartbeat cadence and never executes a real workload.

### What we know

- `RunPodProvider._create_pod` (`src/kinoforge/providers/runpod/__init__.py:552`)
  injects the provision script via `KINOFORGE_PROVISION_SCRIPT` env var
  (base64) and decodes it from `dockerArgs`:
  `bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh
  && chmod +x /tmp/p.sh && bash /tmp/p.sh"`.
- `ComfyUIEngine.render_provision`
  (`src/kinoforge/engines/comfyui/__init__.py:1168`) emits a bash script
  starting with `set -euo pipefail` and ending with
  `exec python main.py --listen 0.0.0.0 --port 8188`. ComfyUI itself
  becomes PID 1's exec replacement.
- The self-terminator (`src/kinoforge/providers/runpod/selfterm.py`)
  runs as a backgrounded `nohup` watchdog; it does NOT keep the
  container alive when the foreground script or `exec`'d Python
  process exits non-zero.
- C25 dockerArgs preserve-and-merge (sidecar
  `tests/live/_c25_smoke_evidence.json`) confirmed the wire-side
  format is `bash -c "..." # _kinoforge_hb:<ISO>` with exactly one
  marker. Decoder INTACT under heartbeat writes.

### What we don't know

Why the container exits non-zero. Candidate root causes from the
brainstorm seed:

| ID | Hypothesis |
| -- | ---------- |
| H1 | ComfyUI / custom-node import crash post-launch (Kijai Wan wrapper deps drift). |
| H2 | GPU OOM during VAE / T5 / Wan-14B load. |
| H3 | 24 GB weight curl partial-fail / HF rate-limit / gated-repo auth race. |
| H4 | pip install drift on the `runpod/pytorch:2.4.0-...` base image. |
| H5 | Selfterm dead-man injection edge case (env-var encoding, `set -e` on the bootstrap `python3 -c`). |
| H6 | dockerArgs heartbeat trailer breaks the entrypoint on restart. |

Ranking is in §3; H6 is the least likely given C25 evidence.

---

## 2. Goals and non-goals

### Goals

- Capture stdout, stderr, and exit code of the dying container off the
  pod fast enough to identify which provision-script line caused the
  exit.
- Surface the root cause as one of H1-H6 (or a new hypothesis if
  evidence demands it) with cited log evidence.
- Ship the structural fix(es) the evidence justifies so that a real
  Wan + ComfyUI cold boot reaches `wait_for_ready` 200 and produces
  an asset without C27's predicate firing.
- Close the C25 Task 4 / C26 Task 14 generation gap (currently
  PROTECTED-BUT-UNFIXED) with a real cold-skip ratio < 0.7.

### Non-goals

- Redesign C27's detection layer.
- Modify B3 warm-reuse mechanics.
- Modify C26 / C27 cfg knobs.
- Introduce any infrastructure with a standing monthly charge
  (network volumes, persistent storage tiers beyond ~zero-cost
  default S3) — operator constraint: pay only while actively using.
- Embed weight files in the prebuilt Docker image (HF gated-license
  risk).
- Plumb `registryAuthId` / private registry support (deferred to
  C29 after C28 ships PUBLIC image).

---

## 3. Hypothesis ranking and classify table

Ranked by likelihood given evidence:

1. **H1** — `set -euo pipefail` + `exec python main.py` makes any
   Python import or first-frame crash the container exit. Kijai Wan
   custom node has fast-moving deps; the provision script does NOT
   pin Kijai's git ref.
2. **H2** — Wan 2.1 14B is ~28 GB FP16. Plus T5 (~5 GB) and VAE
   (~500 MB). On smaller GPU offers (RTX A5000 = 24 GB), first
   sampler run OOMs.
3. **H3** — `curl -L --fail` returns non-zero on a single
   chunked-transfer abort or HF 429. With `set -e`, that's container
   exit. On restart, the partial `.safetensors` exists and
   `[ ! -f file ]` SKIPS the re-download — ComfyUI later loads a
   corrupt file (looks identical to H1 from outside).
4. **H4** — Base image is pinned but `ComfyUI/requirements.txt`
   resolves fresh wheels every boot. A new transitive release in the
   last 48-72 h can break import.
5. **H5** — Low likelihood: would have hit alpine smoke too. Cheap
   to rule out via the captured boot.log.
6. **H6** — Very low: C25 sidecar shows correct trailer; C26 PB
   reproduced the loop BEFORE C25 wire-fix was active.

Phase A5 classify table (consumed by gating decision):

| `last_line` pattern | rc | Hypothesis | Triggers |
| ------------------- | -- | ---------- | -------- |
| `curl: (...) failed` / `HTTP/2 4xx` / partial-content errors | 22 / 56 / 18 | H3 | Phase C (already unconditional) |
| `pip install ... ERROR: ...` / wheel resolution failure | 1 | H4 | Phase B |
| `Traceback ... ImportError` / `ModuleNotFoundError` | 1 | H1 | Phase B |
| `Traceback ... torch.cuda.OutOfMemoryError` | 1 | H2 | Phase B (cfg knob: force ≥ 40 GB GPU offer; optional FP8 in image) |
| `KINOFORGE_SELFTERM_SCRIPT: unbound variable` / Python error in the `python3 -c` selfterm bootstrap | 1 / 2 | H5 | refactor selfterm injection (in-spec sub-task) |
| `bash: line N: unexpected token` near `# _kinoforge_hb:` trailer | 2 | H6 | OUT OF SCOPE for C28; re-open C25 wire fix in a follow-up spec |

If A5 evidence matches none of the above, Phase B fires by default
(image pre-bake is the broadest fix surface) and A5 is amended with a
new row.

If A5 evidence implicates H6 (dockerArgs heartbeat trailer breaking
the entrypoint on restart), C28 STOPS. H6 is a C25 wire-fix
regression and a new spec re-opens the preserve-and-merge contract.
Continuing Phase B / Phase C on a broken entrypoint would not help.

---

## 4. Phase architecture

```
A — Diagnostic uplift            (UNCONDITIONAL)
B — Image pre-bake               (GATED on A5: H1 / H2 / H4)
C — curl retry + sha verify      (UNCONDITIONAL — addresses H3 without infra)
D — Closeout
```

Phase A runs first, produces evidence, names the hypothesis.
Phases B and C are independent of each other; C ships even if A5
indicates a different primary cause because it hardens a known-fragile
line with near-zero cost. B ships only when A5 justifies it.

---

## 5. Phase A — Diagnostic uplift

### A0 — Wire-discovery probe

Single GraphQL introspection query against RunPod:

```graphql
{ __type(name: "PodFindAndDeployOnDemandInput") {
    inputFields { name type { name kind ofType { name } } }
} }
```

Sidecar `tests/live/_c28_runpod_input_schema_probe.json` records the
result. Cost: $0 (introspection only; no pod boot).

Spec branches on result:
- `restartPolicy` present → A3 ships and threads through.
- `restartPolicy` absent → A2 trap alone carries the diagnostic; we
  accept restart-loop cost during the Phase A smoke (selfterm caps it).
- `networkVolumeId` field is captured for C29's possible future use;
  C28 does NOT consume it.
- `registryAuthId` field is captured for C29; C28 does NOT consume it.

### A1 — S3 ingest infra

- **Bucket:** `kinoforge-pod-diagnostics`, region `us-west-2` (Oregon
  per default-region memory).
- **Lifecycle:** 7-day expiry on objects under `boot-logs/`. Per
  operator constraint, no objects older than 7 days persist, so
  monthly storage cost ≈ rounding error.
- **IAM:** `kinoforge-ci` self-grants a scoped policy via
  `aws iam attach-user-policy` (per CI memory):
  `s3:PutObject` only, resource ARN
  `arn:aws:s3:::kinoforge-pod-diagnostics/boot-logs/*`, no other
  actions. Simulate first to confirm IAM-allow before applying.
- **Pod-side env vars** (injected by `_create_pod` when
  `cfg.diagnostic_mode == true`):
  `KINOFORGE_DIAG_BUCKET`, `KINOFORGE_DIAG_PREFIX`,
  `KINOFORGE_DIAG_ACCESS_KEY`, `KINOFORGE_DIAG_SECRET_KEY`.
  Access keys are generated fresh per pod via STS or via a scoped
  long-lived IAM user; in either case the credential is single-pod
  scoped with 24h TTL. Never echoed in logs (per
  `never_print_secret_values` memory).

### A2 — Trap-wrap `render_provision`

`ComfyUIEngine.render_provision` (`src/kinoforge/engines/comfyui/__init__.py:1168`)
gains a pre-amble emitted before any other line when
`cfg.diagnostic_mode == true`:

```bash
set -euo pipefail
exec > >(tee -a /tmp/boot.log) 2>&1
trap '_kinoforge_diag_capture $?' EXIT
_kinoforge_diag_capture() {
  local rc=$1
  local last_line
  last_line=$(tail -1 /tmp/boot.log 2>/dev/null || true)
  {
    echo "===== rc ====="
    echo "$rc"
    echo "===== last_line ====="
    echo "$last_line"
    echo "===== nvidia-smi ====="
    nvidia-smi || true
    echo "===== df -h ====="
    df -h || true
    echo "===== free -m ====="
    free -m || true
    echo "===== ls -la models/diffusion_models ====="
    ls -la /workspace/ComfyUI/models/diffusion_models 2>/dev/null || true
    echo "===== dpkg -l torch ====="
    dpkg -l 2>/dev/null | grep -iE 'torch|cuda' || true
    echo "===== boot.log ====="
    tail -500 /tmp/boot.log 2>/dev/null || true
  } > /tmp/diag.txt
  if [ -n "${KINOFORGE_DIAG_BUCKET:-}" ]; then
    aws s3 cp /tmp/diag.txt \
      "s3://${KINOFORGE_DIAG_BUCKET}/${KINOFORGE_DIAG_PREFIX}/diag-$(date -u +%Y%m%dT%H%M%SZ).txt" \
      || true
  fi
}
```

Gated on `cfg.diagnostic_mode == true` so prod is byte-identical.
Pure-additive in `render_provision`. `aws` binary is already present
in the `runpod/pytorch` base image; if not, fall back to raw HTTPS
PUT with sigv4 via `curl`.

### A3 — Restart-policy override

When A0 confirms `restartPolicy` in the schema:
- `InstanceSpec` gains
  `restart_policy: Literal["always", "never"] = "always"`.
- `_create_pod` body adds the field to the GraphQL input when
  non-default.
- YAML cfg: `compute.restart_policy_override: "never"` enabled via
  `--diagnostic-mode` CLI flag on `kinoforge deploy`. Default mode is
  unchanged.

When A0 says no `restartPolicy` field, A3 is a no-op; A4 smoke still
fires but accepts that the container restart-loops once or twice
before C27 reaps. The S3 PUT inside the trap completes from the FIRST
iteration of the loop, so logs survive even in this mode.

### A4 — Phase A live smoke

`tests/live/test_c28_phase_a_diagnostic_capture_live.py`:
- Cfg: same Wan + ComfyUI cfg as `tests/live/cfg_c27_phase_b.yaml`
  with `diagnostic_mode: true` added.
- Boot real pod. Wait for either ready or C27 reap.
- Pull S3 object via the workspace-side `aws` CLI; assert:
  - File present at expected key.
  - Contains markers `===== rc =====`, `===== last_line =====`,
    `===== nvidia-smi =====`.
  - `rc` field is non-zero (we are CAPTURING a failure).
  - `boot.log` tail is ≥ 100 lines and ends with a recognisable
    failure signature.
- **Retry policy on intermittent success:** if a single boot
  unexpectedly reaches `wait_for_ready` 200 instead of failing, the
  smoke retries up to 3 times to capture a failure boot. If all
  3 succeed, A5 records "no reproduction" and the spec gating
  switches to "ship Phase B + Phase C unconditionally as belt-and-
  suspenders" — the loop has stopped happening on its own, and we
  ship the hardening anyway.
- Cost cap: $0.20 (one cold boot to failure ~$0.05; 3-attempt
  retry budget ~$0.15).

### A5 — Classify

Read the S3 object's `last_line` and `rc`. Match against the §3
classify table. Write `tests/live/_c28_phase_a_evidence.json`
recording: pod id, S3 key, `rc`, `last_line`, matched hypothesis,
cited log lines.

Phase B is GATED on the matched hypothesis. The §3 table specifies
which hypothesis triggers Phase B.

---

## 6. Phase B — Image pre-bake (Docker Hub public)

**Gated on A5:** ships when matched hypothesis is H1, H2, or H4. Does
not ship if A5 names H3 or H5 alone.

### B0 — Dockerfile

New file `docker/wan-comfyui/Dockerfile`:

```dockerfile
FROM runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04

ARG COMFYUI_REF=v0.3.10
ARG KIJAI_WAN_REF

# Clone + pin ComfyUI
RUN git clone --depth 1 --branch ${COMFYUI_REF} \
      https://github.com/comfyanonymous/ComfyUI /workspace/ComfyUI && \
    cd /workspace/ComfyUI && \
    pip install --no-cache-dir -r requirements.txt

# Clone + pin Kijai Wan wrapper + deps
RUN cd /workspace/ComfyUI && \
    git clone https://github.com/kijai/ComfyUI-WanVideoWrapper \
      custom_nodes/ComfyUI-WanVideoWrapper && \
    cd custom_nodes/ComfyUI-WanVideoWrapper && \
    git checkout ${KIJAI_WAN_REF} && \
    pip install --no-cache-dir -r requirements.txt

# Pin transitive deps identified by A5 evidence
# (filled in at plan time once A5 names them)

# Build-time import smoke — broken combos fail `docker build`
RUN cd /workspace/ComfyUI && \
    python -c "import sys; sys.path.insert(0,'.'); import comfy"

WORKDIR /workspace
```

Build-time `import comfy` is the cheap insurance — a broken combo
fails the build, never ships to Docker Hub.

### B1 — Build/push pipeline

- **`pixi.toml` task** `build-image-wan-comfyui`:
  `docker build --build-arg COMFYUI_REF=... --build-arg KIJAI_WAN_REF=...
   -t kinoforge/wan-comfyui:${TAG} docker/wan-comfyui/`,
  then `docker push kinoforge/wan-comfyui:${TAG}`.
- **Tag scheme:** `${COMFYUI_REF}-${KIJAI_SHA8}-cu124`; a moving
  `latest` tag is pushed alongside.
- **GitHub Actions** `.github/workflows/build-wan-comfyui-image.yml`:
  `workflow_dispatch` only (manual trigger). NOT on every push —
  15-20 GB push is too expensive for CI churn.
- **Docker Hub creds:** `DOCKERHUB_USERNAME` + `DOCKERHUB_TOKEN`
  added to GitHub Actions secrets at C28 plan-phase pre-flight
  (per `front_load_creds` memory).
- **Operator pre-flight checklist:**
  1. Verify Docker Hub account active.
  2. Confirm token has R/W/D scope.
  3. Add token to GH Actions secrets.
  4. Run `pixi run build-image-wan-comfyui` locally once to validate.

### B2 — `render_provision` slim-mode branch

When `cfg.engine.comfyui.image` starts with `kinoforge/wan-comfyui:`,
`render_provision` SKIPS:
- `git clone ComfyUI`
- `pip install -r requirements.txt`
- Every custom-node `git clone` and `pip install`

It still emits:
- Selfterm bootstrap.
- Model downloads (Phase C-hardened curl).
- `exec python main.py`.

Pure-additive branch — the existing path is unchanged when the image
prefix does not match.

### B3 — YAML cfg switch

`tests/live/cfg_c28_wan_comfyui.yaml` flips
`image: "kinoforge/wan-comfyui:v0.3.10-<sha>-cu124"`.

### B4 — Phase B live smoke

`tests/live/test_c28_phase_b_image_prebake_live.py`:
- Boot Wan + ComfyUI on the pre-baked image.
- Assert `wait_for_ready` returns 200 within `boot_timeout_s`.
- Assert C27 `RESTART_LOOP_REAP` does NOT fire
  (`consecutive_low_uptime_count` stays at 0).
- Generate one asset using the standard prompt at
  `/workspace/prompt-field-realistic.txt`.
- Assert asset sha-stable vs the C27 Phase B baseline.
- Cost cap: $0.30 (one full Wan gen ~$0.10 + headroom).

### B5 — Privatize follow-up (deferred to C29)

Phase B ships PUBLIC image. Once stable, C29 will:
- Add `registryAuthId` plumbing in `_create_pod` + `InstanceSpec` +
  cfg loader.
- Flip image to private repo + RunPod registry-auth credential.

Out of scope for C28.

---

## 7. Phase C — curl retry + sha verify

**Unconditional.** Addresses H3 with no infrastructure. Co-ships with
B even when A5 names a different cause because it hardens a
known-fragile line.

### C1 — `_kinoforge_download` helper

Prepended to `render_provision` output (unconditional, applies even
when `diagnostic_mode == false`):

```bash
_kinoforge_download() {
  local url=$1; local out=$2; local expected_sha=${3:-}
  local attempt
  for attempt in 1 2 3; do
    rm -f "${out}.partial"
    if curl -L --fail --retry 0 -C - \
         ${HF_TOKEN:+-H "Authorization: Bearer $HF_TOKEN"} \
         "$url" -o "${out}.partial"; then
      if [ -n "$expected_sha" ]; then
        local actual
        actual=$(sha256sum "${out}.partial" | awk '{print $1}')
        if [ "$actual" != "$expected_sha" ]; then
          echo "sha mismatch attempt $attempt: $actual vs $expected_sha" >&2
          sleep $((5 * attempt))
          continue
        fi
      fi
      mv "${out}.partial" "$out"
      return 0
    fi
    sleep $((5 * attempt))
  done
  return 1
}
```

Three attempts, exp-backoff (5s, 10s, 15s), sha verify, partial-file
cleanup so the next attempt does NOT silently use a corrupt file.

### C2 — Replace inline curl

Existing `render_provision` line:

```bash
[ ! -f {subdir}/{filename} ] && \
  curl -L --fail{auth_header} '{artifact.url}' -o {subdir}/{filename}
```

becomes:

```bash
[ ! -f {subdir}/{filename} ] && \
  _kinoforge_download '{artifact.url}' '{subdir}/{filename}' '{sha256_or_empty}'
```

### C3 — sha sourcing

- `models[i].sha256` cfg field becomes optional; when set, threaded
  into the helper call.
- HuggingFace `Source.resolve()` already returns sha when the manifest
  provides it (existing plumbing in the artifact path).
- When sha is absent, retries still help but no verify happens.

### C4 — Phase C smoke

`tests/live/test_c28_phase_c_curl_retry_live.py`:
- Re-uses the Phase B pod (no new pod boot).
- Replaces one model URL with a 404 endpoint to force the retry path.
- Assert log shows three attempts at 5s/10s/15s spacing + clean
  failure at the helper level.
- Assert no partial files remain in `models/diffusion_models/`.
- Cost cap: $0.05.

---

## 8. Phase D — Closeout

### D1 — `PROGRESS.md` §C entry

Single bullet at the §C top of `PROGRESS.md`, same shape as the C27
entry:

```
- **C28. RunPod container-restart-loop prevention.** CLOSED. Spec:
  `docs/superpowers/specs/2026-06-13-c28-restart-loop-prevention-design.md`.
  Plan: `docs/superpowers/plans/2026-06-13-c28-restart-loop-prevention.md`.
  Diagnostic-first uplift: S3 PUT in `EXIT` trap (`boot-logs/<pod>/`
  with 7-day lifecycle) + restart-policy=Never (if RunPod input
  supports it) + classify table maps `last_line` → hypothesis.
  Structural fixes gated on Phase A evidence: image pre-bake
  (kinoforge/wan-comfyui Docker Hub public) + curl retry + sha verify
  in `render_provision`. Closes the C27-protected-but-unfixed
  restart-loop class. Closes deferred C25 Task 4 / C26 Task 14
  generation gate.
```

### D2 — `docs/successful-generations.md`

Per CLAUDE.md durability rule: Phase B smoke produces a video with
the same `(provider=runpod, engine=comfyui, model=wan-2.1-14b,
mode=t2v)` tuple as the existing C25/C26/C27 attempts. If
image-pre-bake counts as a new `engine_variant` axis, add ONE new
section; otherwise append a "See also" line under the existing TOC
entry.

### D3 — C27 spec §13 backlink

Append pointer to `2026-06-13-c27-restart-loop-stall-detection-design.md`
§13:

```
- C28 closes the restart-loop class C27 protected against. See
  `2026-06-13-c28-restart-loop-prevention-design.md` §8.
```

### D4 — C26 spec §17 backlink

Same pointer appended to C26's §17 (which already points to C27's
§13).

---

## 9. Acceptance gates

### Phase A

- A0 sidecar exists and records `restartPolicy` presence (yes/no),
  plus `networkVolumeId` and `registryAuthId` presence for C29
  forward-reference.
- A4 smoke produces a single S3 object containing all of: `rc`,
  `last_line`, `nvidia-smi` snapshot, `df -h`, `free -m`,
  `ls -la models/diffusion_models`, `dpkg -l | grep -iE
  'torch|cuda'`, and a `boot.log` tail of ≥ 100 lines.
- A5 sidecar names one of H1-H6 (or a new hypothesis Hn) with
  cited evidence lines from the captured `boot.log`.

### Phase B (when triggered by A5)

- Dockerfile builds locally with build-time `python -c "import comfy"`
  green.
- Image pushed to `kinoforge/wan-comfyui:<tag>` on Docker Hub, public,
  pullable from a clean network without credentials.
- B4 smoke: real Wan + ComfyUI cold pod reaches `wait_for_ready` 200,
  generates one asset, C27 predicate does NOT fire.
- Generated asset's sha is stable against the C27 Phase B baseline
  (deterministic when seed + workflow JSON are stable).

### Phase C (unconditional)

- All inline `curl -L --fail` lines in `render_provision` replaced
  with `_kinoforge_download` helper calls.
- C3 smoke: forced 404 produces three log attempts at 5s / 10s / 15s
  spacing with clean fail at helper level; no partial files remain.
- Healthy-path latency unchanged within ±10% of B4 baseline.

### Spec-level

- Three consecutive Wan + ComfyUI cold-pod boots reach
  `wait_for_ready` 200 with C27 predicate silent on all three.
- One re-fire of
  `tests/live/test_c27_phase_b_wan_warm_reuse_live.py` flips
  acceptance path from PROVEN-PROTECTION → PROVEN (gen2 cold-skip
  ratio < 0.7), closing the C25 Task 4 / C26 Task 14 generation gate.

---

## 10. Risks

| Risk | Mitigation |
| ---- | ---------- |
| RunPod schema does NOT support `restartPolicy` | A2 trap captures stdout/stderr from the FIRST iteration of the loop before container restart; logs reach S3. Slower diagnostic but works. |
| Phase B image breaks against base-image security patches | Tag string includes base-image digest; weekly rebuilds NOT required; operator triggers rebuild on dep change only. |
| Phase B image embeds a transitive that drifts before push | Build-time `import comfy` smoke + custom-node import test refuse to publish broken images. |
| Phase C retries mask a real upstream HF outage | After 3 attempts the helper hard-fails; final stderr reaches S3 via A's trap when `diagnostic_mode == true`. |
| S3 PutObject fails (DNS, IAM key revoked) | Trap wraps PUT in `|| true`; falls back to `/tmp/boot.log` on disk; readable via RunPod's REST `/v1/pods/{id}/logs` API if A0 surfaces it. |
| Docker Hub free-tier rate-limit (100 pulls / 6h auth) | At ~5-10 cold boots/day we have 10× headroom; C29 privatize migrates to GHCR if it ever becomes a constraint. |
| Pod-side AWS creds leak in transcript | Creds NEVER printed; selfterm-style length-and-shape only when debugging; `redact_secrets.py` hook is backstop. |
| Diagnostic mode silently stays enabled in prod | Default cfg `diagnostic_mode: false`; CLI flag is opt-in per-invocation; YAML cfgs in `tests/live/` are the only checked-in callers that set it true. |
| RunPod blocks GraphQL introspection on the public schema | A0 falls back to documented field names (`restartPolicy`, `networkVolumeId`, `registryAuthId`); spec ships under the assumption that documented fields work and A4 smoke surfaces the truth. If documented fields are wrong, A4 fails fast with a clear GraphQL error and we revise A3 inline. |
| Phase A reproduces successfully (loop has stopped happening) | A4 retry policy (§5 A4) escalates to "ship B + C unconditionally as belt-and-suspenders" after 3 successful boots in a row — the hardening lands either way. |

---

## 11. Out of scope

Locked exclusions:
- C27 detection layer.
- B3 warm-reuse mechanics.
- C26 / C27 cfg knobs.
- Network volume / weight pre-stage (operator constraint: no standing
  monthly charge).
- Private registry / `registryAuthId` plumbing (deferred to C29).
- Image pre-bake of weight files themselves (HF gated-license risk).
- Multi-engine prevention. ComfyUI is the only consumer; other
  engines pick this pattern up via the same `render_provision` trap
  in a future spec when they regress.

---

## 12. Wire-discovery notes for plan phase

- **`restartPolicy` field on `PodFindAndDeployOnDemandInput`** —
  probed in A0. If present, schema is likely
  `restartPolicy: PodRestartPolicy` (enum: `ALWAYS`, `NEVER`,
  `ON_FAILURE`). Probe captures the exact shape.
- **`networkVolumeId` field** — also probed in A0 (recorded for
  C29 forward-reference; not consumed in C28).
- **`registryAuthId` field** — same (C29 forward-reference).
- **Trap shell semantics:** `set -e` + `trap '...' EXIT` — trap
  fires AFTER the failing line exits non-zero. `rc=$?` capture must
  be the FIRST statement in the trap body. Verified pattern.
- **`tee` + `exec` redirection:** `exec > >(tee -a /tmp/boot.log) 2>&1`
  survives `exec python main.py` exec-replacement because the tee
  subshell becomes the new stdout/stderr destination at the
  containing shell level. The python process inherits those fds.
- **`runtime.container.lastExitCode`** — RunPod GraphQL fallback
  path; probed in A0 if `restartPolicy` is absent.

---

## 13. Live-spend budget

| Phase | Cap | Reason |
| ----- | --- | ------ |
| A0 | $0.00 | GraphQL introspection only |
| A4 | $0.20 | one cold boot to failure |
| B4 | $0.30 | one full Wan gen on pre-baked image |
| C3 | $0.05 | re-uses B4 pod |
| Spec-level smoke | $0.30 | one re-fire of C27 PB cfg |
| **Total** | **$0.85** | well under $20 session budget |

No standing monthly costs (operator constraint). S3 storage cost ≈
rounding error (7-day lifecycle, ~100 KB/log).

---

## 14. Cross-spec updates on C28 close

- Append C28 §C entry to `PROGRESS.md` (D1).
- Append backlink pointer to C27 spec §13 (D3).
- Append backlink pointer to C26 spec §17 (D4).
- New entry (or "See also" line) in `docs/successful-generations.md`
  per the rule in `/workspace/CLAUDE.md` (D2).

---

## 15. Plan-phase task ordering

Suggested task breakdown for the plan phase (consumed by
`/superpowers-extended-cc:write-plan`):

```
1.  A0 wire-discovery probe + sidecar.
2.  A1 S3 bucket + IAM policy + key issuance via kinoforge-ci.
3.  A2 trap-wrap render_provision (RED → GREEN with table-driven tests
    asserting trap pre-amble byte-equality and gating on diagnostic_mode).
4.  A3 InstanceSpec.restart_policy + _create_pod wire + CLI flag +
    tests (skipped if A0 says restartPolicy unsupported).
5.  A4 RED scaffold (live smoke test file committed before spend).
6.  A4 live smoke + S3 object capture + sidecar.
7.  A5 classify table consumption + sidecar.
8.  GATE: read A5 sidecar; branch to B-only / B+C / C-only.
9.  B0 Dockerfile + local build smoke.
10. B1 pixi task + GH Actions workflow + secrets pre-flight.
11. B2 render_provision slim-mode branch + tests.
12. B3 cfg YAML.
13. B4 RED scaffold + live smoke.
14. C1 _kinoforge_download helper + table-driven tests
    (retry, sha verify, partial-file cleanup).
15. C2 inline-curl replacement + tests.
16. C3 RED scaffold + live smoke (reuse B4 pod).
17. Spec-level smoke: three consecutive cold boots + C27 PB re-fire.
18. D1-D4 closeout commits.
```
