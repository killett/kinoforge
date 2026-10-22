# kinoforge Release Checklist

Run through these items before tagging a new release (`git tag v*`).

## Pre-tag

- [ ] `pixi run test` — full pytest suite green.
- [ ] `pixi run lint` + `pixi run typecheck` — clean.
- [ ] `pixi run smoke-local` — Tier 1 LoRA-swap smoke green.
- [ ] Tier 3: dispatch `smoke-wan21-weekly` (workflow_dispatch only since
  2026-09-21) or run `pixi run smoke-21b-live` locally — green within the last 7 days.
- [ ] **`pixi run smoke-wan22-live`** — Tier 4 ops-confidence smoke on
  real Wan 2.2 14B + Arcane Style pair. Expected wall-clock 20-30
  min; expected spend $1-2; each module is bounded by its `_BUDGET_CAP`
  ($1.50–$4.00, harness `tests/_smoke_harness/budget.py`).
  Verify 4 distinct mp4s landed under `output/`, pod destroyed
  cleanly via `kinoforge list`. Per the
  `destroy-pods-when-work-is-done` memory, the smoke's finally + a post-run
  `kinoforge list` are the leak guard (the leak-sweep cron is disabled —
  Finding #0 in `docs/hygiene-audit-2026-10-18.md`).
- [ ] `gh issue list -L 5 -l leaked-smoke-pod` returns empty — no
  recent leaks waiting for triage.

## Tag + push

- [ ] `pixi run release X.Y.Z [--note "…"]` — bumps `pyproject.toml`, commits,
  and creates the annotated tag (see `docs/releasing.md`).
- [ ] `git push origin main --follow-tags`.

See `docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`
for the full smoke-pyramid design.
