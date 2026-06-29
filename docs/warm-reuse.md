# Warm-reuse, LoRA-flexible swaps, and smoke pyramid

(Moved from README §Operator warm-reuse (B4), §LoRA-flexible warm-reuse (Wan 2.2 + Diffusers) incl. eviction policy, per-pod swap lock, --loras, --dry-run-swap, pod lora ls, failure modes, configuration knob, deferred limitations, smoke test pyramid; §Default test LoRAs (Wan 2.1 1.3B T2V); §Default test LoRA (Wan 2.2 T2V); §kinoforge grid lora_swap: cells on 2026-06-27. See [../README.md](../README.md).)

## Operator warm-reuse (B4)

After a successful `kinoforge generate` or `kinoforge deploy`, the
provisioned pod stays in the local ledger. A second invocation can
reuse it without paying the 1–5 minute ComfyUI + Wan cold-start cost.

### Discovery loop

```bash
kinoforge list                       # shows id + provider + capability_key
# Match the printed capability_key against your cfg:
#   python -c "from kinoforge.core.config import load_config; \
#              print(load_config('cfg.yaml').capability_key().derive()[:12])"
kinoforge status --id <id>           # confirm verdict is LIVE
kinoforge generate -c cfg.yaml --prompt P --mode t2v --instance-id <id>
```

`kinoforge batch -c cfg.yaml --manifest m.yaml --instance-id <id>` reuses
the same pod across every manifest row.

### `--force-attach` matrix

When the classify verdict is not LIVE, `kinoforge generate` /
`kinoforge batch` refuses. Pass `--force-attach` to override the
salvageable verdicts:

| Verdict | Default | `--force-attach` |
|---|---|---|
| LIVE | attach | attach |
| HEARTBEAT_UNKNOWN | refuse | attach |
| IDLE_REAP | refuse | attach |
| ORPHAN_REAP | refuse | attach |
| STALE_LEDGER | refuse | refuse (pod is gone) |
| OVERAGE_REAP | refuse | refuse (max_lifetime policy) |
| UNROUTABLE | refuse | refuse (provider unreachable) |
| HEARTBEAT_SUBSTRATE_MISSING | refuse | refuse (no wire substrate) |

Capability_key mismatch is never bypassable — use a cfg matching the
pod or `kinoforge destroy --id <id>` to free the slot.

Exit codes:
- `0` — warm-attach succeeded.
- `1` — instance id not in ledger.
- `2` — precondition refused (provider mismatch / cap_key mismatch /
  classify verdict non-LIVE without `--force-attach` / pod raced
  destroyed between classify and attach / `--force-attach` passed
  without `--instance-id`).

## LoRA-flexible warm-reuse (Wan 2.2 + Diffusers)

The B4 warm-reuse path above keys on the full `CapabilityKey`
(base + LoRAs + engine + precision), so a pod loaded with LoRA set
`[A, B]` never warm-attaches a job that wants `[A, C]`. The
LoRA-flexible warm-reuse feature splits the key into two factors so
a single Wan 2.2 pod can serve many LoRA stacks in sequence without
paying the cold-boot cost again:

- **`WarmAttachKey(base_model, engine, precision)`** — the slow-to-rebuild
  identity that drives matcher lookup. A warm pod is a candidate iff
  this factor matches the requested cfg.
- **`LoraStack(refs)`** — the cheap-to-swap delta. When the matcher
  finds a `WarmAttachKey` match whose `LoraStack` differs, it POSTs
  the target stack to the pod's `/lora/set_stack` endpoint; the pod
  evicts what it has to, downloads what's missing, and rebuilds the
  diffusers adapter set in place.

`CapabilityKey.derive()` is byte-identical to the pre-split version
so every pre-feature ledger entry stays valid.

### Eviction policy

When the target LoRA stack needs more disk than the pod has free,
the pod-side helper picks the LRU-oldest evictable refs (those NOT
in the target stack, ordered by `last_used_at_local` ascending) and
evicts them in order until enough disk is freed. The matcher returns
`None` (cold-boot fall-through) if even evicting every candidate
would not free the needed bytes.

### Per-pod swap lock

A process-wide `PodLockRegistry` serializes concurrent
swap-then-generate attempts on the same pod, so two parallel jobs
that both want to attach to pod X cannot race the LoRA state. The
lock is non-blocking — a contended pod is skipped and the matcher
considers the next candidate. Multi-process kinoforge instances do
NOT share the lock; that's a documented deferral (Layer H follow-up).

### `kinoforge generate --loras` — CLI LoRA stack override

Override `cfg.loras` (and bypass `vault.loras` with an audit warning)
by passing a heredoc-string LoRA stack on the command line. No edit to
the YAML needed for one-off experimentation.

**Shape.** One LoRA per line. Whitespace-separated columns:
`ref [strength] [branch]`. Only `ref` is required; `strength` defaults
to `1.0`; `branch` defaults to `auto`. Blank lines and `#` line
comments are silently dropped.

**Minimal example** (one LoRA, defaults):

```bash
kinoforge generate -c ../examples/configs/wan21-1_3b.yaml \
  --prompt "test" --mode t2v --no-reuse \
  --loras "civitai:1234@5678"
```

**Full example** (multi-LoRA with strength + branch):

```bash
kinoforge generate -c ../examples/configs/wan22-14b.yaml \
  --prompt "test" --mode t2v --no-reuse \
  --loras "$(cat <<'EOF'
1111:2222 1.0 h
3333:4444 1.2 l
EOF
)"
```

(`1111:2222` is numeric shorthand for `civitai:1111@2222`. Other refs
— `civitai:N@N`, `civarchive:N@N`, `hf:Org/Repo[:filename]`,
`file:/abs/path`, `https://...` — pass through verbatim. Unknown
schemes rejected.)

**URL paste supported.** As of 2026-06-28 you can paste full URLs in
place of the canonical short ref, both at the `--loras` heredoc and
inside cfg / vault / grid `loras:` blocks:

| URL shape | Canonical |
|-----------|-----------|
| `https://civitai.com/models/<id>/...?modelVersionId=<vid>` | `civitai:<id>@<vid>` |
| `https://civarchive.com/models/<id>?modelVersionId=<vid>` | `civarchive:<id>@<vid>` |
| `https://huggingface.co/<org>/<repo>/blob/main/<file>` | `hf:<org>/<repo>:<file>` |

Civitai and civarchive URLs MUST carry `?modelVersionId=...`;
canonical refs are version-pinned and a bare model URL is rejected
with `civitai URL missing required ?modelVersionId=... query
parameter`. HuggingFace branches other than `main` are dropped with a
warn-once (the canonical `hf:` ref does not encode branch). Bare HF
repo URLs (`huggingface.co/<org>/<repo>`) are NOT normalized — paste
the `blob/<branch>/<file>` URL instead.

`civarchive:<id>@<vid>` is parse-accepted by this release but the
downstream resolver is the next workstream (see `PROGRESS.md` →
"NEXT SESSION — TOP PRIORITY"). Until that ships, civarchive refs
will fail at resolution time with a clear "civarchive source not yet
implemented" error.

**Precedence.**

| Source | Wins when... |
|---|---|
| `--loras` | passed (override mode; D3 + D4) |
| `vault.loras` | no `--loras` AND vault loaded with non-empty `.loras` |
| `cfg.loras` | no `--loras` AND no vault.loras |
| `[]` (empty) | `--loras ""` (or comments-only heredoc); explicit empty override |

When `--loras` is passed AND `vault.loras` is non-empty, a single
WARNING line goes to stderr naming the bypass count:

```
cli-loras-bypass-vault: --loras override applied; vault.loras (3 entries) bypassed for this run. Vault is unchanged on disk.
```

The vault file on disk is unchanged.

**Errors.** All input lines parsed in one pass; aggregated diagnostics
printed to stderr on exit 1:

```
--loras: 2 problem(s) found

  line 2 col 1: unknown scheme `cvtai` (expected one of: civitai, file, hf, http, https)
  line 4: duplicate (ref, branch) — first declared on line 1
```

Diagnostic output carries line numbers, column indices, error kinds,
and scheme prefixes only — never the raw `ref` string. This invariant
is locked by `../tests/test_lora_error_redaction.py` and the
`../tests/test_no_unredacted_writes.py` AST scan.

**Composition.** `--loras` composes orthogonally with all existing
`generate` flags (`--instance-id`, `--no-reuse`, `--dry-run-swap`,
`--force-attach`, `--skip-preflight`). `--dry-run-swap` adds a
`loras_source: cli|vault|cfg|empty` line to the preview so you can
confirm which precedence branch fired without running the full
generation.

### `kinoforge generate --dry-run-swap` — preview without spend

```bash
kinoforge generate --config cfg.yaml --prompt P --mode t2v --dry-run-swap
# matcher: selected pod {pod-id}
#   evict:    ['civitai:foo@1']
#   download: ['civitai:bar@2']
#   cost:     8.4s
```

No pod lock acquired. No HTTP traffic to the pod. No
`validate_for_generate`. Use it before a paid run to verify the
matcher will reuse the pod you expect. Also works on `kinoforge
batch --dry-run-swap`.

### `kinoforge pod lora ls <pod_id>` — direct pod-side inventory

Reads `GET /lora/inventory` straight from the pod (not from the
ledger snapshot) so you see the LIVE resident LoRA set, even when
the ledger is stale (matcher's last update was minutes ago) or
when the ledger is in-memory-only (`--ephemeral`). Exits 2 on
unreachable pod.

```bash
kinoforge pod lora ls <pod-id>
#   loras (2 resident, 1.4GB used, 18.6GB free):
#     civitai:2197303@2474081  720MB  last_used 2026-06-20T22:18  adapter lora_0
#     civitai:2197303@2474073  720MB  last_used 2026-06-20T22:18  adapter lora_1
```

### Failure modes (operator one-liners)

| Failure | Pod state | Operator next step |
|---|---|---|
| `LoraSwapDownloadError` | unchanged (download failed pre-eviction) | retry safely; same pod is still a candidate |
| `LoraSwapDegradedPodError` | half-state, ledger marked `status=degraded` | reaper will destroy on next sweep; matcher routes elsewhere or cold-boots |
| `LoraSwapPodUnreachableError` | proxy timed out past retry budget | check pod via `kinoforge status --id`; ledger marked degraded |
| `LoraSwapVramOomError` | rolled back to previous adapter set; HEALTHY | try a smaller LoRA stack or a different pod — pod itself is fine |
| `LoraSwapDiskFullError` | disk full mid-download, ledger marked degraded | same recovery as degraded |

Every error class exposes `.manual_cleanup_command()` returning a
copy-paste `kinoforge destroy --id <pod-id>` string for hand-recovery
when the automated reaper is unavailable.

### Configuration knob

```yaml
compute:
  lifecycle:
    # Seconds the matcher trusts the ledger's loras_dir_free_bytes
    # snapshot before re-probing the pod's /lora/inventory. 0 disables
    # the stale-check. Default: 300.
    lora_swap_re_probe_after_s: 300
```

### Deferred / known limitations

- **Engine scope = Diffusers only.** ComfyUI uses graph-tagged LoRA
  loading; integrating the matcher there is the C23 follow-up.
- **`deploy_session` integration is deferred.** The matcher + swap
  endpoints + helpers are all shipped + tested; the orchestrator-side
  wiring through `deploy_session` is staged behind a separate task.
  Operators can drive the matcher manually via the standalone
  `kinoforge.core.warm_reuse.integration.try_warm_attach_with_swap`
  helper in the meantime.
- **Cross-process lock not shared.** Two `kinoforge` invocations on
  the same machine each maintain their own `PodLockRegistry`.
- **No hot-swap UX.** A swap blocks the calling generate; there is
  no progress stream surfaced to the operator beyond the existing
  generate log line.
- **No LoRA pinning.** The matcher's LRU never reserves a ref against
  eviction.

See `docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md`
for the full design + `docs/superpowers/plans/2026-06-20-lora-flexible-warm-reuse.md`
for the implementation plan.

### Smoke test pyramid

Three tiers + a watchdog (full design:
`docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`):

| Tier | Trigger | What it tests | Cost |
|---|---|---|---|
| 1 — `pixi run smoke-local` | Every PR (CI) + on demand | HTTP contract, eviction, disk math, VRAM-OOM rollback against a stub pipe over real uvicorn | $0 |
| 3 — `pixi run smoke-21b-live` | Weekly Mon 04:00 PT + on demand | Real-diffusers semantics, real CUDA, real RunPod proxy + Cloudflare path on Wan 2.1 1.3B + 2 single LoRAs | ~$0.20 |
| 4 — `pixi run smoke-wan22-live` | Manual, pre-release | Full Wan 2.2 14B + Arcane Style pair end-to-end on A100 80GB | ~$1-2 |

A separate `pixi run smoke-leak-sweep` cron runs every 30 min to reap
any tier-tagged pod older than its ceiling (Tier 3: 45 min, Tier 4:
90 min) and post a GitHub issue per reap. All four tiers share
`../tests/_smoke_harness/` so the kinoforge-internal HTTP patterns
(UA + `?api_key=` + URLError retry + leak sweep) are inherited by
import, not by rediscovery.

## Default test LoRAs (Wan 2.1 1.3B T2V)

This repo's canonical LoRA-pair test default for **Wan 2.1 T2V-1.3B** (the cheap
weekly Tier-3 smoke target, `../examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`)
is two single-LoRA refs picked for cross-style coverage. Both ship explicitly
on the **Wan Video 1.3B t2v** base so they're guaranteed compatible. Wan 2.1
is single-transformer, so each ref is a single tensor (no high/low pair).

| Slot | Name | CivitAI page | Model ID | Version ID | kinoforge ref |
|---|---|---|---|---|---|
| A | wan2.1 1.3b static rotation | <https://civitai.com/models/1479320/wan21-13b-static-rotation?modelVersionId=1673265> | 1479320 | 1673265 | `civitai:1479320@1673265` |
| B | Pokemon Sprite Animation Video LoRA | <https://civitai.com/models/1595383/pokemon-sprite-animation-video-lora?modelVersionId=1805395> | 1595383 | 1805395 | `civitai:1595383@1805395` |

### Activation: trigger word + strength

- **Slot A (static rotation):** trigger word `sttcrttn` — prepend to any
  prompt where the LoRA should activate (camera-rotation motion effect).
  Recommended strength: 1.0 (default).
- **Slot B (Pokemon sprite):** no trigger word — the model card explicitly
  states no trained text token. Style activates by load alone. Recommended
  output resolution per the model card: 768×768 for optimal sprite-art motion.
  Recommended strength: 1.0 (default).

### What this default exercises

- **Cross-style mp4 distinctness.** The two LoRAs produce visually very
  different outputs (camera-rotation motion vs Gen-5 Pokemon sprite art),
  so the matrix runner's `sha_distinct_required=True` post-condition fails
  loudly if a swap silently did nothing.
- **The full 4-step Wan 2.1 1.3B LoRA-swap matrix** in
  `../tests/smoke/live_wan21/test_lora_swap_matrix.py`:
  cold-boot → load [A] → swap to [B] → clear to []. Total bounded by
  `BudgetTracker(cap_usd=0.30)` per fire.

### How to use it

The pair is committed in
[`../examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`](../examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml)
under the `smoke:` block:

```yaml
smoke:
  lora_a: "civitai:1479320@1673265"   # wan2.1 1.3b static rotation
  lora_b: "civitai:1595383@1805395"   # Pokemon Sprite Animation
```

Both resolve through `CivitAISource`, which requires `CIVITAI_TOKEN` in the
`.env` file (see [Credentials → Known keys](#known-keys)).

## Default test LoRA (Wan 2.2 T2V)

This repo's canonical LoRA-pair test default is **Arcane Style [WAN 2.2 T2V] v1.0**
(CivitAI model `2197303`). Wan 2.2 ships as a Mixture-of-Experts model with two
transformers — a **high-noise** transformer for the early sampling steps and a
**low-noise** transformer for the later steps — so a full-effect Wan 2.2 LoRA
ships as a *pair*, one tensor file per transformer. Both refs are listed below
so an operator cloning the repo has a known-good, reproducible LoRA stack to
test against.

| Role | CivitAI page | Model ID | Version ID | kinoforge ref |
|---|---|---|---|---|
| High noise | <https://civitai.com/models/2197303/arcane-style-wan-22-t2v?modelVersionId=2474081> | 2197303 | 2474081 | `civitai:2197303@2474081` |
| Low noise  | <https://civitai.com/models/2197303/arcane-style-wan-22-t2v?modelVersionId=2474073> | 2197303 | 2474073 | `civitai:2197303@2474073` |

### Activation: trigger word + strength

Both versions use the same activation keyword and recommended strength
per the CivitAI page:

- **Trigger word:** `ArcaneStyle` — prepend (with trailing space) to
  any prompt where at least one of the two Arcane tensors is loaded.
  Without the trigger word, the LoRA's style does not activate.
- **LoRA strength:** 1.0 to 1.2 (per-adapter weight via
  `pipe.set_adapters([...], adapter_weights=[1.0, 1.0])` for the
  Diffusers path).
- **Sampler steps:** at least 6 low-noise steps recommended when the
  low-noise tensor is loaded.

### What this default exercises

- **Both noise stages of Wan 2.2.** The pair is the canonical "full-effect on
  Wan 2.2" configuration. Mounting only the high-noise tensor or only the
  low-noise tensor IS technically supported by Wan 2.2 and will yield a
  partial-effect render — but that is NOT what this default is designed to
  demonstrate.
- **Multi-LoRA `CapabilityKey` identity.** Order of `loras:` entries is part
  of the cache key (see `compute_profile_alias` in `../src/kinoforge/core/vault.py`
  and `CapabilityKey.derive()` in `../src/kinoforge/core/profiles.py`). Always
  list high-noise first, then low-noise, for stable cache hits across runs.

### Not exercised by this default

- **Wan 2.1 LoRA used as a Wan 2.2 low-noise LoRA.** Wan 2.1 single-stage LoRAs
  can be mounted as the low-noise tensor in a Wan 2.2 stack (matched
  transformer shape). The Arcane Style v1.0 pair above ships native Wan 2.2
  high+low tensors, so this fallback path is not what we test here.
- **Single-tensor (high-only or low-only) Wan 2.2 runs.** Supported by Wan 2.2,
  not the focus of this default.

### How to use it

The recommended path is to put the pair in your vault file (`--vault PATH` or
`KINOFORGE_VAULT`), in the order shown above:

```yaml
# in your vault YAML — outside the repo, chmod 600
loras:
  - ref: civitai:2197303@2474081     # high-noise
    label: arcane-style-wan22-high
  - ref: civitai:2197303@2474073     # low-noise
    label: arcane-style-wan22-low
```

The same pair is committed (commented-out, behind the placeholder discussion)
in [`../examples/configs/wan.yaml`](../examples/configs/wan.yaml) and
[`../examples/vault/example.yaml`](../examples/vault/example.yaml) so a fresh clone
has the canonical refs locally.

Both resolves go through `CivitAISource`, which requires `CIVITAI_TOKEN` in
the `.env` file (see [Credentials → Known keys](#known-keys)).

> **Status caveat (PROGRESS C23).** Neither of the committed Wan ComfyUI graph
> JSONs (`runpod-comfyui-wan.graph.json`, `runpod-comfyui-wan-t2v.graph.json`)
> currently includes a `WanVideoLoraSelect` node wired into
> `WanVideoSampler.lora`. The refs above are the *specified* default for when
> the C23 graph-wiring follow-up lands; until then, mounting them via the
> ComfyUI engine is a no-op at sampler time. The Diffusers engine path
> (`../src/engines/diffusers/`) follows the same canonical pair when its LoRA-load
> hook ships.

## lora_swap: grid cells

> The `lora_swap:` cell variant supplements the `generate:` / `path:` variants documented in [batch-and-grid.md](batch-and-grid.md#kinoforge-grid--composed-side-by-side-mp4-from-n-generations).

A strength sweep using `generate:` cells cold-boots one pod per
distinct LoRA stack — strengths differ but the stack is the same, so
the matcher reuses a single pod for the whole sweep (good). A
DIFFERENT-ref sweep (e.g. comparing two LoRAs) cold-boots one pod per
ref — that's the cost the `lora_swap:` cell variant eliminates.

Each `lora_swap:` cell declares a full stack; cells sharing the same
`WarmAttachKey(base_model, engine, precision)` pack into ONE group.
Cell-1 cold-boots a pod via `kinoforge generate --loras
--emit-provision-record`; cells 2..N attach via `kinoforge generate
--attach-pod <pod_id> --loras` and route the new stack through the
existing `POST /lora/set_stack` server endpoint. One pod, N
generations, N server-side swaps — no N-fold cold-boot tax.

```yaml
title: Wan 2.2 14B Arcane strength sweep
layout: 1x3
budget_cap_usd: 2.0
on_swap_failure: classify   # strict | continue | classify (default)
cells:
  - lora_swap:
      config: /outside/repo/wan22-arcane-base.yaml
      stack:
        - ref: civitai:2197303@2474081
          strength: 0.5
          branch: high
        - ref: civitai:2197303@2474073
          strength: 0.5
          branch: low
    caption: strength=0.5
  - lora_swap:
      config: /outside/repo/wan22-arcane-base.yaml
      stack:
        - ref: civitai:2197303@2474081
          strength: 1.0
          branch: high
        - ref: civitai:2197303@2474073
          strength: 1.0
          branch: low
    caption: strength=1.0
  # ... cell 3 at strength 1.5
```

A `<grid_out>.cost.json` sidecar is written next to the composed mp4
(always — success, partial, or abort) with per-group wall-clock cost
broken down by cell. The grid-level `budget_cap_usd` re-checks between
cells; remaining cells are marked `budget_killed` once tripped.

**`on_swap_failure` policy** drives the per-failure executor decision:

- `strict` — ANY non-zero exit aborts the group + destroys the pod
  immediately. Use for paranoid pre-prod sweeps.
- `classify` (default) — pattern-matches stderr. Transient HTTP errors
  (502 / `ProxyWarmupTimeout` / `ConnectionError`) retry up to 3× at
  5s backoff; recoverable swap rejections (`BranchUnknown`,
  `SwapRejectedDetails`) fail the cell but keep the pod warm for the
  next attempt; unrecoverable (`VRAMRollbackFailure`,
  `RunPodGraphQLError`, OOM 137) abort the group.
- `continue` — even ambiguous failures continue to the next cell;
  truly unrecoverable failures still abort (corrupt pod state would
  poison the rest).

#### Related CLI flags

The grid executor drives these flags on `kinoforge generate` per
swap-mode cell; both are also useful for ad-hoc operator scripting.

`--attach-pod <pod_id>` — attach to a ledger-recorded warm pod whose
`warm_attach_key` matches the cfg AND whose live status is `ready`.
Skips provision. Distinct from `--instance-id` (which uses full
`CapabilityKey` + the warm-reuse matcher, and would reject a
different LoRA stack — that's why grid swap-mode needs the separate
flag). Mutex with `--no-reuse` and `--emit-provision-record`. Pod
survives at end.

`--emit-provision-record <path>` — on successful cold-boot, writes a
JSON record `{pod_id, endpoint_url, provider, warm_attach_key,
provision_ts, cost_per_hr_usd}` to `<path>`. NOT written on
provision failure. The grid executor reads this to hand cell-1's
fresh pod off to cells 2..N's `--attach-pod` invocations.

## Sweeper-side ephemeral pod reap

See [Sweeper-side ephemeral pod reap
(2026-06-28)](lifecycle.md#sweeper-side-ephemeral-pod-reap-2026-06-28)
in `lifecycle.md`. As of 2026-06-28, the long-running sweeper daemon
no longer ignores ephemeral pods: it reads `ephemeral-index.json` on
each tick, probes each pod via the provider's new `probe_runtime`
method, and reaps stale rows (`GC_404`), overage pods
(`OVERAGE_REAP`), or wedged pods (`STALL_REAP` — cross-tick history
required, so one-shot `kinoforge reap` skips it).

This closes the durability gap where an ephemeral pod whose
selfterm watchdog crashed bled cost until manual intervention. The
sweeper now catches that within `stall_window_s` seconds of the
crash.

Spec:
`docs/superpowers/specs/2026-06-28-sweeper-ephemeral-reap-design.md`.
