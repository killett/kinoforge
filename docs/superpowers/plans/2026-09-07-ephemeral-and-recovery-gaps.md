# Closing the Ephemeral and Recovery Gaps — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **STATUS: COMPLETE.** All 7 tasks implemented, two-stage reviewed, and merged to `main`
> on 2026-09-09 at `8f9ffea4` (branch `fix/ephemeral-and-recovery-gaps`, since deleted).
> Live proof cost $0.15. Every box below is ticked; nothing here is outstanding. See the
> RESUME SNAPSHOT in `PROGRESS.md` for the outcome and the defects still open.

**Goal:** Close five filed defects — U23, U11, U18, U17 and U21 — so that every command that books a GPU leaves a durable record before it does, and every recovery path can act on the records that exist.

**Architecture:** Three themes. (A) The pre-create durable record reaches `batch` and `grid`, the two commands the 2026-09-06 money-leak branch did not touch. (B) The recovery paths — one-shot `reap` and `destroy` — can act on the records that now exist. (C) `provision` cleans up after a post-create failure and bounds its readiness loop. Offline TDD throughout, then one live task that deliberately kills runs.

**Tech Stack:** Python 3.13, kinoforge CLI in the `live-modal` pixi env, Modal serverless GPUs, pytest with the `test-design` skill standards.

**Spec:** `docs/superpowers/specs/2026-09-07-ephemeral-and-recovery-gaps-design.md`

---

## READ THIS FIRST — you have no memory of the session that wrote this plan

You are working in `/workspace` on **kinoforge**, a GPU video-generation orchestrator that books
cloud GPUs, runs video models on them, and tears them down. Real money is spent when it runs.

**Before anything else, follow the project's own resume protocol** (it is in `CLAUDE.md`):

1. Read the **RESUME SNAPSHOT** section at the top of `PROGRESS.md` (the file is large — read the
   first ~120 lines and the snapshot; do NOT attempt a full-file read).
2. Read this plan's spec, named above.
3. Run `git log --oneline -20`.

**The five defects are filed in full** in the `## URGENT ACTION ITEMS — Modal command matrix
(opened 2026-09-06)` section of `PROGRESS.md`, as **U11, U17, U18, U21 and U23**. Each entry
carries a symptom, an exact reproducer and a suspected site. Read the entry before you touch the
code it names. Every reproducer in this plan is also inline below, so you never need to hunt.

**The single most important habit for this codebase:** *a filed defect's stated mechanism may be
wrong.* Five filed defects in this project have had mechanisms that did not survive contact with
the code, and one diagnosis had to be retracted publicly. **Verify every claim against the source
before fixing it.** If a claim is wrong, fix what is actually there and say so in your report. That
is a good outcome, not a failure.

**Context you will need that is not obvious from the code:**

- `--ephemeral` is a privacy and hygiene flag. Under it, kinoforge writes no ledger row by design;
  the pod's only durable trace is a row in `.kinoforge/_lifecycle/ephemeral-index.json`. The
  provider-side resource is given an opaque name (`eph-<8 hex>` on Modal) so a run id, a local
  timestamp and a workload shape are never published to a third party.
- A branch merged at `d5dc0433` made that index row get written **before** `create_instance`, so a
  crashed run still leaves a nameable pod. **Read commits `9d34d008` and `8403a71c` before writing
  any code for Task 1** — they establish the pattern you must reuse: `_ephemeral_launch_row_reserve`,
  `_ephemeral_strict_session`, `_settle_unused_launch_row`, and `EphemeralSession.resource_name`.
- **Ruling C1** (a project convention): a create that raises **keeps** its row for the classifier
  to age out. Never delete the row on failure.
- The gate spelling for "is this a strict-policy ephemeral run" is
  `session is not None and not session.policy.ledger_record`. There is a helper,
  `_ephemeral_strict_session()`. Call it; do not re-inline the predicate.
- `EphemeralSession.resource_name` memoises **per run id**, not per session. `batch` and `grid`
  both launch several pods per invocation, so preserving that is a correctness requirement, not a
  tidiness one: two pods in one command must not collide on a name.

## Global Constraints

- **Tasks 1-5 are OFFLINE. Task 6 is the only task that spends money.** Do not book a GPU before Task 6.
- **Budget for Task 6: ~$0.50, hard ceiling $1.50.** Cheap config only:
  `examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml` (A10, ~90 s boot, $1.10/hr).
- **Every kinoforge command runs as `pixi run -e live-modal kinoforge …`.** The default env has no
  `modal` binary, so `destroy` there silently skips the orphan probe and reports a false negative.
- **`pixi run preflight` must exit 0 before any live spend.** It checks credentials, that zero pods
  are active, and a clean working tree.
- **Never leave a pod running.** After any live cell, prove teardown from a NEW process:
  `kinoforge list` must print `[instance overview] No running instances.` AND
  `No instances recorded in ledger.`, and the Modal app list must show no running `kinoforge-*` app.
  A mid-run log line is NOT proof.
- **Known open defect that will affect you:** `kinoforge status --id` does NOT return a Modal pod's
  endpoint URL (filed as U3). Read the endpoint from the ledger JSON under `.kinoforge/` directly.
- **TDD is mandatory.** Failing test first, confirm RED for the stated reason, minimum
  implementation, confirm GREEN. Use the `test-design` skill for every test.
- **Never `--no-verify`.** Run `pixi run pre-commit run --files <changed>` before each commit.
  `pixi run pre-commit run --all-files` and `pixi run pytest -q` must be green at the end.
- **Never read or print credentials.** Never `git checkout <sha>` in the working tree (a project
  convention in `CLAUDE.md` — it has corrupted this tree before; use `git show <sha>:<path>`).
- Commit trailers on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01CmmaFdtGCd4jHqU44kzUw1`
- **When a task closes a filed defect**, update its entry in `PROGRESS.md` to say FIXED with the
  commit named, and say plainly whether the proof is offline or live. **Do not claim closure that
  was not proven.** A stale "still open" entry beside a shipped fix, or a closure claim with no
  proof, is the failure mode this project keeps hitting.
- **If a task turns out to need more than a contained change, STOP and report** rather than forcing
  it. A correct diagnosis with a filed follow-up is a good outcome.

**User decisions (already made):**
- "Offline now, live proof as a final task" — Tasks 1-5 offline, Task 6 live with a deliberate mid-run kill, ~$0.50.
- "Add the two recovery gaps" — scope is five defects (U23, U11, U18) plus (U17, U21), not three.

---

### Task 1: `batch` reserves a launch row before it books (U23)

**Goal:** An `--ephemeral batch` leaves a durable record naming its pod from before the pod exists, so a crash mid-batch leaves something that can be found and destroyed.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_batch`)
- Test: `tests/cli/test_cmd_batch_ephemeral.py` (create)

**Background:** `_cmd_batch` cold-creates through `batch_generate` → `deploy_session` and calls neither `_ephemeral_launch_row_reserve` (before the create) nor `_ephemeral_index_add` (after it); nothing in `core/batch.py` or the orchestrator writes an index row either. Under the strict policy the ledger write is suppressed and lives only in `session.in_memory_ledger`, so for the whole batch the pod exists in no state file on disk. A batch is the longest-running command in the CLI — a manifest of N entries on one pod — so this is the widest crash window there is.

**Reproducer (offline, $0 — the absence is structural):**
```
pixi run python -c "
import inspect
from kinoforge.cli import _commands
from kinoforge.core import batch
src = inspect.getsource(_commands._cmd_batch) + inspect.getsource(batch.batch_generate)
for name in ('_ephemeral_launch_row_reserve', '_ephemeral_index_add', 'EphemeralIndex'):
    print(f'{name}: {name in src}')
"
```

**Acceptance Criteria:**
- [x] Under `--ephemeral`, a row naming the pod exists before `create_instance` returns control to the caller.
- [x] The row is settled the same way `generate` settles it — released only on a confirmed destroy, upgraded and warned on an unconfirmed one.
- [x] A create that raises keeps the row (ruling C1).
- [x] An ordinary (non-ephemeral) batch reserves nothing — the gate is `_ephemeral_strict_session()`.
- [x] Several pods in one batch do not collide on a name.

**Verify:** `pixi run pytest tests/cli/test_cmd_batch_ephemeral.py -v` → all pass

**Steps:**

- [x] **Step 1: Read the established pattern.** `git show 9d34d008` and `git show 8403a71c`. Note how `_cmd_generate` reserves, then settles, and how the helpers are gated.
- [x] **Step 2: Verify the filed claim** with the reproducer above. Record what it printed.
- [x] **Step 3: Write the failing tests.** The strong shape for ordering, used by the merged branch, is to have the fake provider's `create_instance` itself assert the row is already present, reading through a fresh `SessionContext` against the same state dir — that is, through the same on-disk path a separate `kinoforge list` process would use. A test that only checks a row exists at the end would pass an unfixed implementation.

```python
def test_ephemeral_batch_reserves_a_row_before_the_pod_exists() -> None:
    """Bug caught: _cmd_batch never reserved a launch row, so for the whole
    batch — the longest-running command there is — the pod existed in no
    state file and a crash stranded a billing GPU nothing could name."""


def test_ordinary_batch_reserves_nothing() -> None:
    """The ledger already covers the non-ephemeral path; reserving there
    would put an endpoint-less row in front of the warm matcher."""


def test_batch_row_survives_a_create_that_raises() -> None:
    """Ruling C1: a failed create keeps its row for the classifier."""
```

- [x] **Step 4: Run them, confirm RED.**
- [x] **Step 5: Implement**, reusing the helpers. Do not build a parallel path.
- [x] **Step 6: Confirm GREEN**, then `pixi run pytest tests/cli -q`.
- [x] **Step 7: Commit** `fix(batch): reserve the ephemeral launch row before the pod exists`.
- [x] **Step 8: Update U23** in `PROGRESS.md` — FIXED with the commit, offline-proven, live proof owed (Task 6). Commit separately as `docs:`.

---

### Task 2: `grid` passes `--ephemeral` through to each cell (U11)

**Goal:** `grid --ephemeral` actually suppresses run identity at the provider instead of silently publishing it.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_grid`), `src/kinoforge/core/grid/executor.py` (`_build_generate_cmd`, `run_grid`)
- Test: extend `tests/core/test_grid_executor.py` (argv construction) and `tests/cli/test_cmd_grid.py` (the flag reaching the executor) — both already exist; there is no `tests/core/grid/` directory

**Background:** the `grid` parser declares `--ephemeral` with help text "pass-through to each underlying generate". `_cmd_grid` does `del ctx`, never reads `args.ephemeral`, and the string `ephemeral` appears nowhere under `src/kinoforge/core/grid/`. Each cell is run as a subprocess whose argv `_build_generate_cmd` builds, so that is where the flag must land.

**Observed live (T1-29):** the run exits 0 and the provider's app list then shows cells named `kinoforge-grid_<local timestamp>_<hash>__cell0` rather than the opaque `eph-<8hex>` the strict policy requires. The ledger is empty afterwards, but that is the per-cell teardown, not the flag working — a plain non-ephemeral grid produces the same empty ledger, which is why this went unnoticed.

**Acceptance Criteria:**
- [x] `--ephemeral` appears in each cell's subprocess argv when the flag is set, and does not when it is not.
- [x] The report states whether opaque naming then follows automatically from the strict policy in the child process, or whether the grid path needs more. If more is needed and it is contained, do it; if it means restructuring the executor, STOP and file.
- [x] Cells in one grid do not collide on a name.

**Verify:** `pixi run pytest tests/core/test_grid_executor.py tests/cli/test_cmd_grid.py -v` → all pass

**Steps:**

- [x] **Step 1: Verify the filed claim.** Confirm `_cmd_grid` really ignores `args.ephemeral` and that nothing under `core/grid/` reads it.
- [x] **Step 2: Write the failing test** asserting the built argv carries the flag when set and omits it when not.
- [x] **Step 3: Confirm RED**, implement the pass-through, confirm GREEN.
- [x] **Step 4: Answer the naming question.** Trace what the child process does with the flag. Record the answer in your report either way — it is the part a reader cannot infer.
- [x] **Step 5: Run** `pixi run pytest tests/core tests/cli -q`.
- [x] **Step 6: Commit** `fix(grid): pass --ephemeral through to every cell`, then update U11 as `docs:`.

---

### Task 3: one-shot `reap` gates on the union (U18)

**Goal:** `kinoforge reap` finds an ephemeral orphan instead of printing "nothing to do" over a billing pod.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_reap`, the short-circuit near the "ledger empty (nothing to do)" print)
- Test: `tests/cli/test_cmd_reap.py` (extend)

**Background:** `_cmd_reap` short-circuits on `if not ledger.entries():` and returns before calling `sweep()`. An ephemeral run writes no ledger row by design, so its orphan lives only in the `EphemeralIndex`, which is unioned in *inside* `sweep()` — on the far side of the guard. The function's own comment claims the one-shot reap "gets the same age+idle backstop the daemon does". It does not, for exactly the case that needs it.

**Observed live:** with a live Modal ephemeral pod and a populated index, `kinoforge reap --format json` printed `{"type": "header", "entries": 0}` and the human format printed `reap: ledger empty (nothing to do)`.

**Acceptance Criteria:**
- [x] The short-circuit fires only when the ledger AND the ephemeral index are both empty.
- [x] With an empty ledger and a non-empty index, `sweep()` runs and the orphan is classified.
- [x] The message says what was actually searched, not "ledger empty".
- [x] Both output formats stay correct — there is an existing pair of tests pinning the human sentence and the JSON shape on the empty path; keep both honest.

**Verify:** `pixi run pytest tests/cli/test_cmd_reap.py -v` → all pass

**Steps:**

- [x] **Step 1: Verify the claim** — read the guard and confirm `sweep()` is where the index is unioned in.
- [x] **Step 2: Write the failing test:** empty ledger, one index row, assert the orphan is reached rather than short-circuited.
- [x] **Step 3: Confirm RED**, implement the union gate, confirm GREEN.
- [x] **Step 4: Re-word the message** and update the two existing empty-path tests to match.
- [x] **Step 5: Run** `pixi run pytest tests/cli -q`.
- [x] **Step 6: Commit** `fix(reap): gate on the union of ledger and ephemeral index`, then update U18 as `docs:`.

---

### Task 4: `destroy` resolves the identifier the provider accepts (U17)

**Goal:** `kinoforge destroy --id` can reap a Modal app killed mid-deploy, instead of handing the operator a traceback and requiring the raw provider CLI.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py` (`destroy_instance`), `src/kinoforge/providers/modal/_app.py` (`default_stop`)
- Test: `tests/providers/test_modal_destroy_by_app_id.py` (create)

**Background:** Modal registers an app's NAME only when the deploy completes. An app whose deploying client died is addressable **solely by its `app_id`**. `destroy_instance` builds `kinoforge-<run_id>` and hands it to `default_stop`, which shells `modal app stop <name> --yes` under `check=True`. Two failures compound: the wrong identifier, and `check=True` turning a diagnosable provider error into an unhandled `subprocess.CalledProcessError` traceback.

**Observed live:** a `provision` killed 1.0 s into `create_instance` left an app in state `initializing...` with `tasks=0` that did not self-resolve over ~60 s. `destroy --id` failed with `No App with name … found`; `modal app stop ap-UieraQfT1GhxX3v4etyrEA --yes` returned rc 0 and stopped it.

**Acceptance Criteria:**
- [x] Destroy looks the app up in `modal app list --json`, matching on `description`, and stops it by `app_id`.
- [x] It falls back to stopping by name when no id is found, preserving today's behaviour for a normally-deployed app.
- [x] A provider error is handled and reported with the app id it found — no unhandled `CalledProcessError`.
- [x] The JSON parse is defensive: an unexpected shape fails with a message naming what it could not find, rather than raising a parse error.
- [x] Tests inject the listing and the stopper; **no live call**.

**Verify:** `pixi run pytest tests/providers/test_modal_destroy_by_app_id.py -v` → all pass

**Steps:**

- [x] **Step 1: Verify the claim** against `destroy_instance` and `default_stop`.
- [x] **Step 2: Write the failing tests** — mid-deploy app resolvable only by id; normally-deployed app still stops by name; provider error reported not raised; malformed listing handled.
- [x] **Step 3: Confirm RED**, implement, confirm GREEN.
- [x] **Step 4: Run** `pixi run pytest tests/providers -q`.
- [x] **Step 5: Commit** `fix(modal): stop an app by the id the provider accepts`, then update U17 as `docs:` — offline-proven; note that a live re-proof means racing a 1-second window and is a separate follow-up.

---

### Task 5: `provision` guards its tail and bounds its readiness loop (U21)

**Goal:** A `provision` whose readiness poll or weight download fails tears its pod down instead of leaving it billing, and a pod that never becomes ready cannot spin forever.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_provision`, everything after the provisional-row collapse)
- Test: `tests/cli/test_cmd_provision.py` (extend — it already exists)

**Background:** everything after the collapse runs unguarded: a readiness loop calling `provider.get_instance`, then `provision(...)`. Any raise propagates out with the pod alive and billing. The loop also has **no timeout and no iteration cap** — a pod that never reaches `ready` spins forever at two seconds a turn, which is the same money leak with no exception at all.

This is NOT the defect U7 closed. U7 was "nothing can SEE the pod", which is fixed — the row is written pre-create and the pod is visible to `list`, `destroy` and the sweeper. This is "nothing tears the pod DOWN".

**Reproducer (offline, $0 — the absence is structural):**
```
pixi run python -c "
import kinoforge.cli._commands as C, inspect
tail = inspect.getsource(C._cmd_provision).rsplit('_collapse_provisional_row', 1)[1]
assert 'try:' not in tail and 'except' not in tail
print('unguarded after the create:'); print(tail.strip()[:400])
"
```

**Acceptance Criteria:**
- [x] A raise from the readiness poll or the provisioner destroys the pod before propagating.
- [x] The readiness loop is bounded by a deadline and surfaces a clear error naming the last status seen.
- [x] A destroy that itself fails is reported without masking the original error.
- [x] `deploy`'s existing destroy-on-error handling is followed rather than a second shape invented — read it first.
- [x] The behaviour change (provision now destroys on failure where it previously left the pod up) is stated in the U21 entry.

**Verify:** `pixi run pytest tests/cli/test_cmd_provision.py -v` → all pass

**Steps:**

- [x] **Step 1: Verify the claim** with the reproducer above, and read `deploy`'s destroy-on-error handling in `src/kinoforge/core/orchestrator.py`.
- [x] **Step 2: Write the failing tests** — a raising readiness poll destroys the pod; a raising provisioner destroys the pod; a never-ready pod hits the deadline rather than looping; a failing destroy is reported without hiding the original error.
- [x] **Step 3: Confirm RED**, implement, confirm GREEN.
- [x] **Step 4: Run** `pixi run pytest tests/cli -q`.
- [x] **Step 5: Commit** `fix(provision): tear down on a post-create failure, and bound the readiness loop`, then update U21 as `docs:`.

---

### Task 6: live proof (~$0.50, the only task that spends)

**Goal:** Prove the two money-leak fixes against the real provider, including by deliberately killing a run — because these fixes exist so that something works when a run fails, and a proof that never lets anything fail proves nothing.

**Files:**
- Modify: `PROGRESS.md` (the U23, U11, U18 entries), `docs/modal-command-matrix.md` (any affected rows)

**Acceptance Criteria:**
- [x] **A1 proof:** launch `--ephemeral batch`, read `.kinoforge/_lifecycle/ephemeral-index.json` **while it is still running** and capture a row naming the pod, then kill the controller and confirm the pod is nameable and reapable from a fresh process.
- [x] **A2 proof:** run `grid --ephemeral` and confirm the provider's app list shows opaque `eph-` names with no run id or timestamp.
- [x] **B1 proof:** with an ephemeral orphan live and an empty ledger, one-shot `reap` finds it rather than printing "nothing to do".
- [x] Teardown proof from a fresh process after every cell. No pod alive at the end.
- [x] Actual spend recorded per cell.
- [x] If a fix does NOT hold live, record it and do **not** retrofit the code to make the proof pass. Report it and stop that cell.

**Verify:** `pixi run -e live-modal kinoforge list` → both "no instances" lines, and no running `kinoforge-*` app in the Modal app list

**Steps:**

- [x] **Step 1: `pixi run preflight` → PASS.**
- [x] **Step 2: A1.** Build a two-entry manifest (see `examples/configs/manifests/batch-prompts.yaml` for the shape; put yours outside the repo, e.g. `/home/claudeuser/kinoforge-proof/batch.yaml`). Launch it under `--ephemeral`, poll the index file from a second shell during the run, capture the row, kill the controller, recover the pod.
- [x] **Step 3: A2.** A 1x2 grid spec must live OUTSIDE the repo — the loader refuses in-repo paths on purpose. Run it with `--ephemeral` and capture the provider's app names.
- [x] **Step 4: B1.** With the orphan from A1 or A2 still live and the ledger empty, run one-shot `reap` and capture the output.
- [x] **Step 5: Teardown proof and spend reconciliation.**
- [x] **Step 6: Update the records with the live evidence**, replacing "offline-proven, live proof owed" with what was actually observed. Commit.

---

### Task 7: close the record

**Goal:** The filed items and the matrix tell the truth about what is now fixed, proven, and still open.

**Files:**
- Modify: `PROGRESS.md`, `docs/modal-command-matrix.md`

**Acceptance Criteria:**
- [x] Every item this plan closed says FIXED with its commit and its proof level; every item it did not close still says open.
- [x] **Re-read the ENTIRE urgent-actions section for stale claims**, not only the entries this plan touched. This project has repeatedly shipped entries claiming a defect is open after it was fixed, and a section preamble that had quietly become false.
- [x] The status index counts (fixed / partial / open) balance against the individual entries.
- [x] `pixi run pre-commit run --all-files` green and `pixi run pytest -q` passing.

**Verify:** `pixi run pytest -q` → all pass, and `rg -n "U11|U17|U18|U21|U23" PROGRESS.md` shows each with a current status

**Steps:**

- [x] **Step 1: Re-read the whole urgent-actions section**, including its preamble and index.
- [x] **Step 2: Update the matrix** for any row these fixes change.
- [x] **Step 3:** `pixi run pre-commit run --all-files` and `pixi run pytest -q`.
- [x] **Step 4: Commit** `docs: record the ephemeral and recovery gaps closed`.
