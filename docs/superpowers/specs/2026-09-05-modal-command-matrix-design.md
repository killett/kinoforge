# Modal command matrix — design (2026-09-05)

## Purpose

The operator has been hitting errors on assorted `kinoforge` commands and does not
remember which. The Modal provider was last proven live on 2026-07-12; the
compute-seam rework S1–S5 (2026-08-24 → 2026-09-02) rewrote the setup/run split,
the endpoint shape, offer selection and the ledger rows, and every live proof in
those stages ran on SkyPilot or RunPod. This campaign exercises every kinoforge
subcommand and the important flag combinations against Modal, records a
works / does-not-work verdict per cell in one tracked document, and fixes the
cheap failures.

## Scope

- Provider: `modal` only. Configs: the five `examples/configs/modal-*.yaml`.
- Every subcommand: deploy, provision, doctor, generate, upscale, interpolate,
  list, status, stop, destroy, logs, forget, reap, gc, cost, pod lora ls,
  sweeper (start/stop/status/metrics), batch, grid, plus the global flags
  `--ephemeral`, `--vault`, `--state-dir`, `--version`.
- Budget: $20 hard, $18 working ceiling. Wan 2.2 14B capped at $4, no retry.

## Shape

Four tiers, cheapest first, so an early failure never strands the expensive
cells. Each cell's verdict is written to `docs/modal-command-matrix.md` and
committed before the next cell starts.

| Tier | What | Est. cost |
|------|------|-----------|
| 0 | Offline surface: doctor, every `--dry-run`, ledger/cost/gc/sweeper reads on an empty ledger, bare `modal` CLI with and without creds | $0 |
| 1 | Wan 2.1 1.3B on A10: one warm pod driven through every stateful command; deploy-first lifecycle; ephemeral + sweeper reap; grid 1x2 | ~$0.60 |
| 2 | FlashVSR x4 warm + 1080p warm-attach on A100-80GB; ephemeral upscale; RIFE interpolate on T4 + chained interpolate | ~$0.60 |
| 3 | Wan 2.2 14B on A100-80GB, one `generate --no-reuse`, killed at $4 | ≤ $4 |

## Rules binding every live cell

1. `pixi run preflight` exits 0 before the first spend of each task.
2. Every command runs as `pixi run -e live-modal kinoforge …` (the Modal token
   lives in `.env`; kinoforge loads it, pixi does not).
3. Utilisation is polled every 60–90 s through the Modal `/util` route; GPU 0%
   for three consecutive polls while a generation is in flight means the pod
   is dead: capture logs, destroy, mark the cell FAIL.
4. Every output video gets frame extraction (`ffmpeg_frames_by_count`, 5
   frames, one montage) and a visual verdict before the cell is marked PASS.
5. After every teardown, `kinoforge list` must print both
   `[instance overview] No running instances.` and
   `No instances recorded in ledger.`; `modal app list` must show no
   `kinoforge-*` app in a running state.
6. Warm-reuse cells end with an explicit `destroy`; one-shot cells pass
   `--no-reuse`.

## Failure protocol

Record exit code, the phase (validate / deploy / boot / generate / artifact /
teardown), the last 40 log lines and a diagnosis. If the cause is small and
local (one function, one config key, one guard), fix it red/green, commit, and
re-run that cell once. Anything larger is filed as a follow-up in
`PROGRESS.md`; the cell stays FAIL with the follow-up named.

## Artifacts

- Plan: `docs/superpowers/plans/2026-09-05-modal-command-matrix.md`
- Results: `docs/modal-command-matrix.md` (cell id, command, config, verdict,
  cost, evidence path, notes) — committed after every cell.
- `successful-generations.md`: new tuples (batch on Modal, grid on Modal,
  deploy-first on Modal) get sections; repeats get "See also" lines;
  `--ephemeral` runs never appear.
- Operator-side files that must live outside the repo (grid spec, vault) go
  under `/home/claudeuser/kinoforge-matrix/`.

## Decisions recorded

- Wan 2.2 14B: include, run last, hard cap $4, no retry.
- On failure: record, then fix only cheap local causes; one retry per cell.
