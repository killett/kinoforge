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

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T0-01 | `kinoforge --version` | — | PENDING | $0.00 | | |
| T0-02 | `kinoforge doctor -c <cfg>` | CFGS | PENDING | $0.00 | | |
| T0-03 | `kinoforge deploy --config <cfg> --dry-run` | CFGS | PENDING | $0.00 | | |
| T0-04 | `kinoforge upscale -c VSRX4 --video FIX --dry-run` | VSRX4 | PENDING | $0.00 | | |
| T0-05 | `kinoforge upscale -c VSR1080 --video FIX --dry-run` | VSR1080 | PENDING | $0.00 | | |
| T0-06 | `kinoforge interpolate -c RIFE60 --video FIX --fps 60 --dry-run` | RIFE60 | PENDING | $0.00 | | |
| T0-07 | `kinoforge generate -c WAN13B --mode t2v --prompt PROMPT --dry-run-swap` | WAN13B | PENDING | $0.00 | | |
| T0-08 | `kinoforge batch -c WAN13B --manifest batch.yaml --dry-run-swap` | WAN13B | PENDING | $0.00 | | |
| T0-09 | `kinoforge grid --spec grid.yaml --out grid-dry.mp4 --dry-run` | WAN13B | PENDING | $0.00 | | |
| T0-10 | `kinoforge list` | — | PENDING | $0.00 | | |
| T0-11 | `kinoforge status --id does-not-exist` | — | PENDING | $0.00 | | |
| T0-12 | `kinoforge reap` / `kinoforge reap --format json` | — | PENDING | $0.00 | | |
| T0-13 | `kinoforge cost` / `--json` / `-c WAN13B` | WAN13B | PENDING | $0.00 | | |
| T0-14 | `kinoforge gc --config WAN13B` | WAN13B | PENDING | $0.00 | | |
| T0-15 | `kinoforge sweeper status` / `kinoforge sweeper metrics` | — | PENDING | $0.00 | | |
| T0-16 | `kinoforge forget --id does-not-exist` | — | PENDING | $0.00 | | |
| T0-17 | bare `modal app list` without env, then with env loaded | — | PENDING | $0.00 | | |

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
