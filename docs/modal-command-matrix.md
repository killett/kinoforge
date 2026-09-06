# Modal command matrix

**Purpose.** Run every `kinoforge` subcommand and the important flag combinations against the
Modal provider, and record one verdict per cell. The operator has been hitting errors on
assorted commands without a record of which; this document is that record.

**Started:** 2026-09-05
**Budget:** $20 hard. Tier 3 (Wan 2.2 14B) is capped at $4 and never retried.
**Plan:** `docs/superpowers/plans/2026-09-05-modal-command-matrix.md`
**Spec:** `docs/superpowers/specs/2026-09-05-modal-command-matrix-design.md`
**Logs:** `/home/claudeuser/kinoforge-matrix/logs/<cell id>.log` (operator-side, not tracked)

**Spend so far: $0.00**

**Verdicts.** `PASS` — behaved as expected. `FAIL` — a crash, a traceback, or a wrong result.
`EXPECTED-REFUSAL` — refused cleanly and on purpose (not-found id, unsupported operation,
a guard firing). `SKIPPED` — deliberately not run. Cells not yet run carry the placeholder
verdict in the Verdict column.

**Config shorthand** (all under `examples/configs/`):

| Short | File |
|-------|------|
| `WAN13B` | `modal-diffusers-wan-2_1-1_3b-t2v.yaml` |
| `WAN14B` | `modal-diffusers-wan-2_2-14b-t2v.yaml` |
| `VSRX4` | `modal-diffusers-flashvsr-x4-upscale.yaml` |
| `VSR1080` | `modal-diffusers-flashvsr-1080p-upscale.yaml` |
| `RIFE60` | `modal-diffusers-rife-60fps-interpolate.yaml` |
| `CFGS` | all five of the above |
| `FIX` | `output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` |
| `PROMPT` | `examples/configs/prompts/field-realistic.txt`, verbatim |

---

## Tier 0 — the offline surface ($0)

17 cells, all run 2026-09-05. One FAIL (T0-02), fixed in-session; no cell left failing.

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T0-01 | `kinoforge --version` | — | PASS | $0.00 | `logs/T0-01.log` | prints `kinoforge 0.5.0`, exit 0 |
| T0-02 | `kinoforge doctor -c <cfg>` | CFGS | PASS | $0.00 | `logs/T0-02.log` | FAILed on first pass — all five cfgs raised a `heartbeat_interval_required` ERROR and exited 1. Fixed in `c9d9b284`; re-run is exit 0 with zero `✗` rows on all five. Three WARN rows per cfg remain (modal cannot enforce `max_lifetime` / `job_timeout` / `heartbeat_interval_s`, and ignores `disk_gb` / `max_usd_per_hr`) — warnings, by design, not errors |
| T0-03 | `kinoforge deploy --config <cfg> --dry-run` | CFGS | PASS | $0.00 | `logs/T0-03.log` | plan printed for all five (engine, provider, placement, lifecycle ceilings, capability key), exit 0, no traceback, nothing provisioned |
| T0-04 | `kinoforge upscale -c VSRX4 --video FIX --dry-run` | VSRX4 | PASS | $0.00 | `logs/T0-04.log` | `scale: 4x`, `engine: flashvsr`, `no_reuse: False`, exit 0 |
| T0-05 | `kinoforge upscale -c VSR1080 --video FIX --dry-run` | VSR1080 | PASS | $0.00 | `logs/T0-05.log` | `scale: 1080p`, exit 0. The dry-run prints the raw cfg string, not the resolved target; `ScaleTarget(kind="height", value=1080)` is asserted by `tests/test_modal_config.py::test_flashvsr_1080p_config_is_height_target` |
| T0-06 | `kinoforge interpolate -c RIFE60 --video FIX --fps 60 --dry-run` | RIFE60 | PASS | $0.00 | `logs/T0-06.log` | `fps: 60.0`, `engine: rife`, exit 0 |
| T0-07 | `kinoforge generate -c WAN13B --mode t2v --prompt PROMPT --dry-run-swap` | WAN13B | PASS | $0.00 | `logs/T0-07.log` | swap plan printed (`loras_source: empty`, empty evict/download, `cost: 0.0s`), no deploy, exit 0. ⚠ the matcher selected a **RunPod** pod for a Modal cfg — see follow-up F1 |
| T0-08 | `kinoforge batch -c WAN13B --manifest batch.yaml --dry-run-swap` | WAN13B | PASS | $0.00 | `logs/T0-08.log` | exit 0, same matcher preview as T0-07. ⚠ the manifest is never parsed on this path — a nonexistent `--manifest` also exits 0; see follow-up F2 |
| T0-09 | `kinoforge grid --spec grid.yaml --out grid-dry.mp4 --dry-run` | WAN13B | PASS | $0.00 | `logs/T0-09.log` | `[grid dry-run] 2 cells, layout=1x2, budget_cap=$0.60`, exit 0. Spec lives outside the repo as the loader requires |
| T0-10 | `kinoforge list` | — | PASS | $0.00 | `logs/T0-10.log` | `[instance overview] No running instances.` + `No instances recorded in ledger.`, exit 0 |
| T0-11 | `kinoforge status --id does-not-exist` | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-11.log` | `instance 'does-not-exist' not found in ledger`, exit 1, no traceback |
| T0-12 | `kinoforge reap` / `kinoforge reap --format json` | — | PASS | $0.00 | `logs/T0-12a.log`, `logs/T0-12b.log` | both exit 0 with `reap: ledger empty (nothing to do)`. ⚠ `--format json` emits that same human line rather than JSON on an empty ledger — see follow-up F3 |
| T0-13 | `kinoforge cost -c WAN13B` / `-c WAN13B --json` | WAN13B | PASS | $0.00 | `logs/T0-13c.log`, `logs/T0-13b.log` | `Burn rate: $0.00/hr`, `(no entries in ledger)`, exit 0; `--json` emits a well-formed object. `-c/--config` is **mandatory** — the config-less forms in the brief exit 2 on argparse. No Modal row: the ledger is empty and `balance` is `{}` (no Modal balance adapter; documented absent) |
| T0-14 | `kinoforge gc --config WAN13B` | WAN13B | PASS | $0.00 | `logs/T0-14.log` | `gc: nothing to do (specify --run <id>)`, exit 0 on the empty store |
| T0-15 | `kinoforge sweeper status -c WAN13B` / `sweeper metrics -c WAN13B --prom` | WAN13B | PASS | $0.00 | `logs/T0-15a.log`, `logs/T0-15b.log` | `running=false`, `pid=none`, `sweeps_total=0`, exit 0; `--json` variant matches. `metrics` renders the Prom textfile exposition, exit 0. Both subcommands require `-c/--config` (exit 2 without it), and `metrics` additionally requires `--prom` |
| T0-16 | `kinoforge forget --id does-not-exist` | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-16.log` | `instance 'does-not-exist' not found in ledger`, exit 1, no traceback |
| T0-17 | bare `modal app list` without env, then with env loaded | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-17a.log`, `logs/T0-17b.log` | without env: `Token missing. Could not authenticate client.`, exit 1 — expected. With the dotenv loader: the Apps table renders **empty**, exit 0, confirming no stray Modal app before Tier 1 |

**Tier 0 tally:** 14 PASS, 3 EXPECTED-REFUSAL, 0 FAIL outstanding (1 found and fixed).

---
## Tier 1 — Wan 2.1 1.3B on A10 (~$0.70)

### Tier 1a — one warm pod through every stateful command

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-01 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` | WAN13B | PENDING | | | |
| T1-02 | `kinoforge list` | WAN13B | PENDING | | | |
| T1-03 | `kinoforge status --id ID` | WAN13B | PENDING | | | |
| T1-04 | `kinoforge logs --id ID` and `--file server.log --out <path>` | WAN13B | PENDING | | | |
| T1-05 | `kinoforge cost` / `--json` / `--no-cache` | WAN13B | PENDING | | | |
| T1-06 | `kinoforge pod lora ls ID` | WAN13B | PENDING | | | |
| T1-07 | `kinoforge reap` and `reap --format json --id ID` | WAN13B | PENDING | | | |
| T1-08 | `kinoforge sweeper status` with no daemon | WAN13B | PENDING | | | |
| T1-09 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` (second process) | WAN13B | PENDING | | | |
| T1-10 | same + `--instance-id ID` | WAN13B | PENDING | | | |
| T1-11 | same + `--force-attach --instance-id ID` | WAN13B | PENDING | | | |
| T1-12 | `kinoforge --vault vault.yaml generate -c CFG --mode t2v` | WAN13B | PENDING | | | |
| T1-13 | `kinoforge batch -c CFG --manifest batch.yaml --concurrent 1` then `--stream-format jsonl` | WAN13B | PENDING | | | |
| T1-14 | `kinoforge generate … --run-id matrix-runid --output-dir <dir>` and `--no-output-dir` | WAN13B | PENDING | | | |
| T1-15 | `kinoforge stop --id ID` | WAN13B | PENDING | | | |
| T1-16 | `kinoforge destroy --id ID` then `kinoforge list` + Modal app list | WAN13B | PENDING | | | |
| T1-17 | `kinoforge forget --id ID` after destroy | WAN13B | PENDING | | | |
| T1-18 | `kinoforge gc --config CFG` | WAN13B | PENDING | | | |

### Tier 1b — deploy-first lifecycle

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-19 | `kinoforge deploy --config CFG` (also `--diagnostic-mode`) | WAN13B | PENDING | | | |
| T1-20 | `kinoforge provision -c CFG` | WAN13B | PENDING | | | |
| T1-21 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT --instance-id ID --skip-preflight` | WAN13B | PENDING | | | |
| T1-22 | `kinoforge reap --apply --id ID` (fallback `destroy --id ID`) then teardown proof | WAN13B | PENDING | | | |

### Tier 1c — ephemeral runs and the reapers

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-23 | `kinoforge --ephemeral generate -c CFG --mode t2v --prompt PROMPT` | WAN13B | PENDING | | | |
| T1-24 | `kinoforge sweeper start -c CFG --interval-s 30` + `status` + `metrics` + `stop` | WAN13B | PENDING | | | |
| T1-25 | `kinoforge --ephemeral generate …` again (second app) | WAN13B | PENDING | | | |
| T1-26 | `kinoforge reap --include-orphans` then `--apply` | WAN13B | PENDING | | | |
| T1-27 | `kinoforge --ephemeral --debug-show-secrets list` | WAN13B | PENDING | | | |

### Tier 1d — grid

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-28 | `kinoforge grid --spec grid.yaml --out grid.mp4 --max-parallel-groups 1` | WAN13B | PENDING | | | |
| T1-29 | `kinoforge grid … --ephemeral` with `--out grid-eph.mp4` | WAN13B | PENDING | | | |

---

## Tier 2 — FlashVSR upscale and RIFE interpolate (~$0.55)

### Tier 2a — FlashVSR upscale on A100-80GB

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T2-01 | `kinoforge upscale -c VSRX4 --video FIX` | VSRX4 | PENDING | | | |
| T2-02 | `kinoforge upscale -c VSRX4 --video FIX --scale 1080p --attach-pod ID` | VSRX4 | PENDING | | | |
| T2-03 | `kinoforge upscale -c VSR1080 --video FIX` | VSR1080 | PENDING | | | |
| T2-04 | `kinoforge destroy --id ID` + proof | VSRX4 | PENDING | | | |
| T2-05 | `kinoforge --ephemeral upscale -c VSRX4 --video FIX --scale 1080p --no-reuse` + proof | VSRX4 | PENDING | | | |

### Tier 2b — RIFE interpolate on T4

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T2-06 | `kinoforge interpolate -c RIFE60 --video FIX --fps 60 --no-reuse` | RIFE60 | PENDING | | | |
| T2-07 | same cfg, `--video <T2-01 output> --fps 32 --no-reuse` | RIFE60 | PENDING | | | |

---

## Tier 3 — Wan 2.2 14B on A100-80GB (capped $4)

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T3-01 | `kinoforge generate -c WAN14B --mode t2v --prompt PROMPT --no-reuse` | WAN14B | PENDING | | | |

---

## Follow-ups

Filed from Tier 0. None was fixed in-session: each is larger than the "one function /
one config key / one guard" bar the campaign's fix policy sets.

**F1 — the ephemeral warm-reuse matcher is provider-blind.** `--dry-run-swap` on a
Modal cfg (T0-07, T0-08) selected pod `i5y9um06fxkq83`, a **RunPod** pod whose
`.proxy.runpod.net` endpoints have been dead since 2026-07-13. Diagnosis:
`find_warm_attach_candidate` (`src/kinoforge/core/warm_reuse/matcher.py`) never mentions
`provider`, and `EphemeralIndex.rows_by_wak` /
`rows_by_kinoforge_key` (`src/kinoforge/core/warm_reuse/ephemeral_index.py:188-194`)
filter on the warm-attach / capability key alone. The key does not encode the provider,
so a row written by one provider is a legal candidate for any other. Three stale RunPod
rows sit in `.kinoforge/_lifecycle/ephemeral-index.json` right now; `kinoforge list`
does not surface them (it reads the ledger only), so they are invisible residue. Live
consequence to expect in Tier 1c: `--ephemeral generate` on Modal may try to attach to a
RunPod URL instead of booting. Fix shape: filter index rows by `cfg.compute.provider`,
or fold the provider into the warm-attach key. Blast radius spans the warm-reuse tests,
hence not attempted here.

**F2 — `batch --dry-run-swap` never reads the manifest.** T0-08 exits 0 with
`--manifest /home/claudeuser/kinoforge-matrix/NOPE.yaml`, a path that does not exist.
The flag short-circuits into `_dry_run_swap_preview` before the manifest is parsed, so
the cell proves the swap preview works but proves nothing about the manifest. Either
validate the manifest before the early return, or document that the flag is
matcher-only.

**F3 — `reap --format json` ignores the format on an empty ledger.** T0-12 prints the
human line `reap: ledger empty (nothing to do)` under `--format json`, so a JSON consumer
gets unparseable output for the empty case. A caller that pipes into `jq` breaks on the
empty ledger specifically — the case most likely to be hit by a scripted teardown check.

**F4 — Modal's declared guardrail gaps are worth a doc row, not a fix.** Every Modal cfg
warns that the provider cannot enforce `max_lifetime`, `job_timeout` or a wire-level
`heartbeat_interval_s` read, and ignores `disk_gb` and `max_usd_per_hr`; the effective
ceiling is Modal's `@app.function(timeout=boot_timeout)`. Correct and already surfaced by
`doctor`, but it means the cfg-level budget knobs on the Modal cfgs are decorative. Worth
stating in `docs/lifecycle.md` so the next operator does not trust them.
