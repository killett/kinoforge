# kinoforge Release Checklist

Run through these items before tagging a new release (`git tag v*`).

## Pre-tag

- [ ] `pixi run test` — full pytest suite green.
- [ ] `pixi run lint` + `pixi run typecheck` — clean.
- [ ] `pixi run smoke-local` — Tier 1 LoRA-swap smoke green.
- [ ] Tier 3 last weekly run (check the most recent
  `smoke-wan21-weekly` GH Actions run) — green within the last 7 days.
- [ ] **`pixi run smoke-wan22-live`** — Tier 4 ops-confidence smoke on
  real Wan 2.2 14B + Arcane Style pair. Expected wall-clock 20-30
  min; expected spend $1-2; bounded by `BudgetTracker(cap_usd=2.00)`.
  Verify 4 distinct mp4s landed under `output/`, pod destroyed
  cleanly via `kinoforge list`. Per the
  `destroy-pods-when-work-is-done` memory, the smoke's finally + the
  leak-sweep cron together cap any leak at 90 min.
- [ ] `gh issue list -L 5 -l leaked-smoke-pod` returns empty — no
  recent leaks waiting for triage.

## Tag + push

- [ ] Bump version in `pyproject.toml`.
- [ ] `git tag v<version>` + `git push --tags`.

See `docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md`
for the full smoke-pyramid design.
