# Hygiene re-audit brief (2026-10-21) — READ FULLY BEFORE STARTING

You are one of eight READ-ONLY auditors in a whole-repo hygiene pass over the `kinoforge`
repository at `/workspace` (git HEAD `f89eff2b`, branch `main`, working tree clean).
The controller will do all editing and committing. You audit and report.

## Hard rules (non-negotiable)

- **Read-only.** Do NOT edit, create, delete, move, or format any file under `/workspace`.
  Do NOT run `git checkout`, `git stash`, `git commit`, `git add`, `pixi add`, `pixi lock`,
  or any formatter/fixer. `ruff check` WITHOUT `--fix` is fine. Running specific non-live
  tests (`cd /workspace && pixi run python -m pytest <path> -q -p no:cacheprovider`) is fine
  if it helps verify a claim; never run live tests (`tests/live`, `tests/smoke/live_*`,
  `tests/smoke/release_*`, `tests/stores/live`) and never set `KINOFORGE_LIVE_TESTS`.
- **Credential safety.** Never `cat`/`echo`/print `.env`, anything under `.gcp/`, `.aws/`, or
  any credential value. Never print environment variables. You never need to.
- **Prefer `rg` and `fd`** over grep/find. Keep tool output small: use `rg -n` with targeted
  patterns, `sed -n 'A,Bp'` for ranges, never `cat` a multi-thousand-line file whole.
- **Only your assigned area** (given in your task prompt). You may READ anything in the repo
  to verify a claim, but only REPORT on files in your area; mention cross-area facts as
  one-liners under "Cross-area notes" so the controller can route them.

## Context you must read first

1. `/workspace/docs/hygiene-audit-2026-10-18.md` — the prior whole-repo audit (AUDIT-ONLY; the
   code is byte-identical today — the only commit since is the audit docs). It was produced
   by a different, weaker model; **its claims are hypotheses to re-verify, not facts**.
2. `/workspace/docs/hygiene-notes.md` — the running log of smells deliberately KEPT, with the
   reasons. Do not re-flag a keep unless you have NEW evidence that its stated reason is false
   (then say so explicitly, with evidence).
3. `/workspace/CLAUDE.md` — project rules. Relevant to you: "Known infra gotchas", "Credential
   safety", and the embed-closure/golden constraints summarised below.
4. The `/workspace/docs/hygiene-audit-2026-07-16.md` file is historical; skim only if a prior
   claim references it.

Baseline at HEAD (controller-verified): `pixi run pre-commit run --all-files` green;
`pixi run test` green (see prior audit for counts).

## What "behaviour-preserving" means here (hygiene skill definition)

No change in return values, exit codes, externally-observable side effects (file writes,
network calls, process exec), or **user-facing text** (stdout/stderr messages, log lines at
ANY level, error messages, prompts, CLI `--help` text). Renaming an internal symbol, fixing a
docstring/comment, deleting unreferenced code, or fixing a `docs/*.md` file IS
behaviour-preserving. Changing a log message or help string is NOT (it is a behaviour change
and belongs under NEEDS DISCUSSION, not FIX NOW).

## Category taxonomy

structural (over-long function/class, deep nesting, leaky abstraction) · dead (unused code,
unreachable branch, vestigial compat shim) · redundant (TRUE DRY violation — one decision in
two places that must change together; NOT textual similarity) · documentation (stale or
misleading comment/docstring/doc; public API missing docstring) · error-handling (swallowed
exception, inconsistent error types, broad `except` without an intent comment) · magic
(recurring hardcoded value with no named constant) · naming (inconsistent/misleading).

## Judgment rules

- DRY = one source of truth for a DECISION. Two blocks that look alike but model different
  concepts are NOT duplication — leave them. Borderline → flag as NEEDS DISCUSSION.
- Respect documented intent: if a comment, test, or design doc explains why a smell exists,
  it is a keep, not a finding (unless the explanation is demonstrably false — then say so).
- Chesterton's fence: before calling code dead, run `git log -S'<symbol>' --oneline` (add
  `--all`), read the introducing/last-touching commit message (`git show -s <sha>`), and
  cite it. If it names a non-obvious reason, downgrade to NEEDS DISCUSSION.
- Real bugs are SEPARATE findings with a concrete failure scenario (inputs/state → wrong
  output). The controller will NOT fix bugs in this pass; it will record them. Precision
  matters more than volume: a wrong bug claim costs more than a missed one.
- Hands off (flag only, never propose edits): generated code (`tools/_vendored/`), lockfiles
  (`pixi.lock`), anything whose edit would break external callers, `.env*`, credentials.
- Let the formatter own formatting — do not report whitespace/style ruff already normalises.
- Test code IS in scope with the same constraints.

## Project-specific constraints the controller must respect (so flag them)

- **Embedded pod files move goldens.** RunPod/Modal configs under `examples/configs/` embed
  specific source files into the pod boot payload (`embed_files:` / `embed_modules:` keys;
  see `tests/providers/test_pod_embed_closure.py`, `tests/providers/test_launch_payload_goldens.py`,
  `tests/providers/test_env_payload_ceiling.py`). Files under
  `src/kinoforge/engines/diffusers/servers/`, `src/kinoforge/core/errors.py`,
  `src/kinoforge/core/pod_paths.py` and others in those lists are embedded. ANY byte change
  there (even a docstring) regenerates 20+ launch goldens and payload baselines. For every
  FIX NOW proposal state explicitly: `embedded: yes/no` (verify with
  `rg -n '<basename>' examples/configs tests/providers/test_pod_embed_closure.py`).
- `tests/test_core_invariant.py` — `core/` must never import adapters (providers/engines/…).
- `tests/test_no_unredacted_writes.py` — direct writes need the public-write tag.
- `tests/test_source_audit.py` / `tests/test_cloud_identifier_scrub.py` — tracked-tree
  scanners; adding example strings that look like credentials or cloud identifiers trips them.
- Pre-commit runs ruff `--fix` + ruff-format + mypy strict; `tests/**` is exempt from ANN/D/S.
- Do not propose changes to `PROGRESS.md` (append-only recovery log) beyond what the
  controller will add itself.

## Your deliverable

Write ONE markdown report to the path given in your task prompt, with EXACTLY these sections:

### A. Prior-claim verification
A table, one row per prior-audit claim and hygiene-notes keep that falls in your area
(use the prior audit's own IDs: `B2`, `P3 …`, `FIX NOW …`, `ND #n`, `keep: …`):
`| id | prior claim (≤1 line) | verdict | evidence at HEAD (file:line, ≤2 lines) |`
Verdict ∈ CONFIRMED / WRONG / PARTIAL / CANNOT-VERIFY-OFFLINE. A WRONG verdict needs the
evidence that refutes it. "I read it and it looks right" is not evidence — quote the line.

### B. New bugs (missed by the prior audit)
Per bug: severity (P1 cost money/destroy work/corrupt output · P2 correctness · P3 narrow),
location, one-sentence defect, **concrete failure scenario**, and the introducing commit if
cheap to find. If you found none, say "None found" and list what you specifically checked.

### C. New hygiene findings
Per finding: location · category · strictly behaviour-preserving? (yes/no + why) · risk to fix
(low/med/high) · call (FIX NOW / LEAVE / NEEDS DISCUSSION) · ≤2 lines of rationale.

### D. FIX NOW edit list (precise, for the controller to apply)
Only items you rate FIX NOW, low risk, strictly behaviour-preserving. For EACH:
- file:line range; `embedded: yes/no`
- exact edit as a minimal old → new (quote the current text verbatim so an `Edit` matches)
- for a deletion: the `git log -S` fence check result (sha + message gist)
- `rg` proof that no test or doc asserts on the removed/changed text (give the command you ran
  and its hit count)
- which test file(s) exercise this code (so the controller can run them first)
Include the prior audit's FIX NOW items for your area that you CONFIRMED — the controller
will work from your list, not the prior one. Group docs-only items separately from code items.

### E. Needs discussion
Medium/high-risk or interface-touching items, with the decision the operator must make.

### F. Cross-area notes
One-liners about things outside your area you noticed while verifying.

Then RETURN to the controller only a summary of at most 15 lines: counts per section
(A verdicts by type, B by severity, C by call, D item count), then the 3-5 most important
things — especially any prior ✔ claim you found WRONG, any new P1/P2 bug, and any prior keep
whose reason is now false. No file dumps in the return message.

Work method: read the prior audit's rows for your area first, verify each against the code,
THEN do a fresh pass file-by-file (every file in your area, not just the ones the prior audit
named — the point of this re-run is to catch what a weaker pass missed). Budget your effort
toward correctness bugs and verifiably-safe FIX NOW items over broad structural commentary.
