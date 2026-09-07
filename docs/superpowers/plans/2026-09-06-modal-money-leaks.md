# Closing the Four Modal Money Leaks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the four most urgent defects from the Modal command-matrix campaign — an untracked `provision`, an ephemeral run with no durable record and no reaper, a warm-attach that cold-boots duplicates, and an `--attach-pod` that refuses healthy pods — and prove each fix live.

**Architecture:** Two themes plus one extension. Theme A writes the durable row *before* the billing resource exists (`provision`, ephemeral). Theme B consults the row that already exists (attach endpoints, matcher skip reasons). The extension makes the sweeper cover the run shape that leaves no ledger row. The matcher fix itself is discovery-then-fix: a $0 diagnostic task runs first and names the cause.

**Tech Stack:** Python 3.13, kinoforge CLI in the `live-modal` pixi env, Modal serverless GPUs, pytest with the `test-design` skill standards.

**Spec:** `docs/superpowers/specs/2026-09-06-modal-money-leaks-design.md`

## Global Constraints

- **Budget: ~$2, hard ceiling $4.** Live cells run on A10 ($1.10/hr) except one A100-80GB ($2.50/hr) pass for the duplicate-boot cell. Record actual spend per cell.
- **Every kinoforge command runs as `pixi run -e live-modal kinoforge …`.** The default env has no `modal` binary, so `destroy` there silently skips the orphan probe.
- **`pixi run preflight` must exit 0 before the first live spend of any task.**
- **Never leave a pod running.** After every teardown prove it from a NEW process: `kinoforge list` printing `[instance overview] No running instances.` AND `No instances recorded in ledger.`, plus the Modal app list showing no running `kinoforge-*` app. A mid-run log line is not proof.
- **Poll utilisation, never spend.** GPU 0% for three consecutive polls during an in-flight generation means the pod is dead: capture the log, destroy, mark the cell failed.
- **Known and unfixed: a Modal pod's endpoint is unreachable from a fresh process (U3).** `kinoforge status --id` will not give you the URL. Read it from the ledger JSON under `.kinoforge/` directly. Task 2 may fix this as a side effect — verify, do not assume.
- **Standard prompt, verbatim:** `--prompt "$(cat examples/configs/prompts/field-realistic.txt)"`.
- **Fixture clip:** `output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`.
- **Cheap config** (A10, ~90 s boot): `examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml`.
- **Upscale configs** for the duplicate-boot reproducer: `examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml` and `examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml`. They differ only in `upscale.scale`.
- **TDD is mandatory.** Failing test first, confirm RED, minimum implementation, confirm GREEN. Use the `test-design` skill for every test.
- **Never `--no-verify`.** Run `pixi run pre-commit run --files <changed>` before each commit.
- **Never read or print credentials. Never `git checkout <sha>` in the working tree.**
- Commit trailers on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01CmmaFdtGCd4jHqU44kzUw1`
- **When a task closes a filed defect**, update its entry in the `## URGENT ACTION ITEMS — Modal command matrix (opened 2026-09-06)` section of `PROGRESS.md` to say FIXED with the commit named, and update the corresponding row in `docs/modal-command-matrix.md`. A stale "still open" entry beside a shipped fix is the exact failure mode that campaign kept catching.

**User decisions (already made):**
- "Live-proven, ~$2 budget" — offline green is not sufficient; each defect's own reproducer runs live, including a deliberate mid-run kill.
- "Both halves" of the ephemeral leak — the durable row AND a sweeper that actually reaps ephemeral pods.
- "No changes" to the spec as written.

---

### Task 0: Make the warm-scan report never silent (spec B2)

**Goal:** A cold create always states why, so the duplicate-boot cause becomes observable at $0.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_ScanReport.summarize`)
- Test: `tests/cli/test_scan_report.py` (create if absent; otherwise add to the existing scan-report test module)

**Background the implementer needs:** `_scan_warm_candidates` returns `(instance, _ScanReport(attached, skipped))`. `summarize()` returns a one-line INFO string, and its three callers (`_commands.py:528`, `:813`, `:913`) log it only `if summary:`. On a hit it names the pod; on a miss with skips it lists reason counts; **on no candidates at all it returns `""` and nothing is logged** — so a cold boot with an empty candidate list is completely silent. That silent branch is the one that matters: it cannot distinguish "no pod was running" from "a pod was running and never entered the candidate list".

**Acceptance Criteria:**
- [ ] `summarize()` returns a non-empty string when there were zero candidates, naming the count as zero and saying a cold create follows.
- [ ] The hit and skip branches keep their existing wording, so nothing that greps those lines breaks.
- [ ] The three call sites still log at INFO and need no change beyond removing the `if summary:` guard if it becomes dead.

**Verify:** `pixi run pytest tests/cli/test_scan_report.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
def test_summarize_names_a_cold_create_when_no_candidates_were_found() -> None:
    """An empty candidate list must not be silent.

    Bug caught: a cold create with zero candidates logged nothing at all,
    so an operator could not tell "no pod was running" from "a pod was
    running and never entered the candidate list" — which is exactly the
    ambiguity that made U14 undiagnosable without spending again.
    """
    report = _ScanReport(attached=None, skipped=[])
    summary = report.summarize()
    assert summary != ""
    assert "0" in summary
    assert "cold create" in summary
```

- [ ] **Step 2: Run it, confirm RED.** `pixi run pytest tests/cli/test_scan_report.py -k no_candidates -v` → FAIL (asserts on `""`).
- [ ] **Step 3: Implement.** In `summarize()`, replace the `if not self.skipped: return ""` early return on the miss path with a string naming zero candidates and the cold create.
- [ ] **Step 4: Run, confirm GREEN**, then run the whole module and `pixi run pytest tests/cli -q`.
- [ ] **Step 5: Commit** `feat(warm-reuse): say why a cold create happened when no candidate was found`.

---

### Task 1: `provision` writes a durable row and refuses to double-book (spec A1)

**Goal:** `kinoforge provision` can never leave a billing instance that no kinoforge command can name, and stops booking a second instance for a key that already has one.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_provision`, from line 255)
- Test: `tests/cli/test_cmd_provision.py` (create)

**Background:** `_cmd_provision` builds an `InstanceSpec` by hand, calls `provider.create_instance(spec)`, and touches neither `ctx.ledger()` nor `ctx.store()` anywhere in the function. `deploy` already writes a pre-launch provisional row tagged `kf_launch_phase=launching` (see `src/kinoforge/core/orchestrator.py`), and `cli/_reconcile._adopt_or_age_out` already resolves such a row provider-agnostically when the create dies. Reuse that mechanism rather than inventing a second one. On Modal the create returned an empty id, which produced an app named with a bare `kinoforge-` prefix that could only be killed with the raw `modal app stop` CLI.

**Acceptance Criteria:**
- [ ] A provisional row exists before `create_instance` is called, and is upgraded to a real row after it returns.
- [ ] A create that raises leaves the provisional row in place for the reconciler to age out (matching ruling C1) — it is not deleted.
- [ ] An empty or missing instance id from `create_instance` is an error, not a recorded instance: non-zero exit, clear message, no row claiming to name a pod it cannot.
- [ ] When a live instance for this capability key already exists, `provision` refuses, names that instance, and books nothing.

**Verify:** `pixi run pytest tests/cli/test_cmd_provision.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.** Four behaviours, each naming the bug it guards:

```python
def test_provision_writes_a_provisional_row_before_creating_the_instance() -> None:
    """The row must exist BEFORE money is committed.

    Bug caught: _cmd_provision called create_instance with no ledger write
    at all, so a crash between create and return left a billing pod that
    `kinoforge list` could not see — $0.13 of unrecoverable spend on
    2026-09-06, recoverable only via a raw `modal app stop`.
    """
    # A fake provider whose create_instance asserts the row is already there.


def test_provision_keeps_the_provisional_row_when_create_raises() -> None:
    """A failed create must leave the row for the reconciler (ruling C1)."""


def test_provision_refuses_an_empty_instance_id() -> None:
    """An id-less instance is not a record.

    Bug caught: Modal returned an empty id, the CLI printed
    `provisioned: instance=''`, and the id needed to reap the pod never
    existed.
    """


def test_provision_refuses_when_an_instance_for_this_key_already_exists() -> None:
    """`provision` implies re-provision, not an unconditional second create."""
```

- [ ] **Step 2: Run them, confirm RED** — all four fail against current `_cmd_provision`.
- [ ] **Step 3: Implement** the provisional-row write, the post-create upgrade, the empty-id guard, and the existing-instance check, in that order, re-running the tests as each goes green.
- [ ] **Step 4: Confirm GREEN** and run `pixi run pytest tests/cli -q`.
- [ ] **Step 5: Commit** `fix(provision): write the durable row before the pod exists, and refuse to double-book`.
- [ ] **Step 6: Update the records** — U7 in `PROGRESS.md` marked FIXED with the commit, and the T1-20 row in `docs/modal-command-matrix.md`. Commit separately as `docs`.

---

### Task 2: `--attach-pod` merges the ledger's endpoints, not only its tags (spec B1)

**Goal:** Attaching to a healthy pod whose endpoint the ledger holds succeeds instead of refusing.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_resolve_attach_pod`, the merge block at 1765-1789)
- Test: `tests/cli/test_resolve_attach_pod.py` (create)

**Background:** the merge block copies `entry["tags"]` onto `live.tags`, then calls `provider.ensure_endpoints(live)` and refuses when the result is falsy. The ledger entry's sibling `endpoints` field is never read. On Modal, `get_instance` builds the instance from `modal app list` (no URL) and `ModalProvider.endpoints` falls back to a per-process `_deployments` dict a fresh process never populated — so `ensure_endpoints` has nothing to work from and the command refuses a pod that was answering `GET /util` seconds earlier. The refusal message compounds this by reporting *tag* keys as evidence about endpoints, which is what made the filed cause a hypothesis.

**Acceptance Criteria:**
- [ ] The ledger entry's `endpoints` are merged onto the live instance before `ensure_endpoints` is called, so the provider has something to repair rather than nothing to find.
- [ ] A pod whose ledger entry holds endpoints and whose provider returns none still attaches.
- [ ] Live values still win over ledger values on collision, matching the existing tag-merge precedence.
- [ ] The refusal message, when it does fire, reports what it actually checked — no tag keys offered as evidence about endpoints.

**Verify:** `pixi run pytest tests/cli/test_resolve_attach_pod.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test.**

```python
def test_attach_pod_uses_the_endpoints_the_ledger_is_holding() -> None:
    """The ledger's endpoints must survive the merge.

    Bug caught: the merge copied `tags` and never `endpoints`, so on a
    provider whose get_instance returns no URL (Modal), --attach-pod
    refused a warm A100 that was answering /util at that moment, with a
    message blaming the tag keys.
    """
    # Ledger entry holds endpoints {"8000": "https://example.invalid"};
    # a fake provider returns an Instance with none and an
    # ensure_endpoints that echoes what it is given.
    # Assert: attach succeeds and the resolved instance carries the URL.
```

- [ ] **Step 2: Run it, confirm RED** — attach returns `(None, 1)`.
- [ ] **Step 3: Implement** the endpoints merge alongside the tag merge, live-wins-on-collision, then correct the refusal message.
- [ ] **Step 4: Confirm GREEN**, run `pixi run pytest tests/cli -q`.
- [ ] **Step 5: Check the U3 prediction.** The spec predicts this may also fix `status` / `pod lora ls` reporting no endpoint. Look at whether those paths read the same ledger field. If this change fixes them, say so; **if it does not, leave U3 filed and say that too.** Do not widen scope to chase it.
- [ ] **Step 6: Commit** `fix(attach): merge the ledger's endpoints, not only its tags`, then update U15 in `PROGRESS.md` and the T2-02 row in the matrix as `docs`.

---

### Task 3: the ephemeral index row is written before `create_instance` (spec A2)

**Goal:** An ephemeral run is nameable from the moment it starts billing, not from the moment it finishes.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_stamp_cold_created_instance` at 536, its `_ephemeral_index_add` call at ~583, the helper itself at ~1629, and the sibling call site at ~936)
- Test: `tests/cli/test_ephemeral_index_timing.py` (create)

**Background:** the index row is written after the orchestrator returns and stamped with completion time — a run launched at 02:01:23 produced a row stamped 02:02:41. For that whole window nothing anywhere names the pod, so a Ctrl-C, an OOM, or a session death strands a billing app, and a monitor cannot poll `/util` because the endpoint has not been written down. `EphemeralIndexRow` already carries `created_at_local`, documented as a future sweeper backstop.

**Acceptance Criteria:**
- [ ] A row naming the pod exists before `create_instance` returns control to the caller, carrying the id and whatever endpoints are known at that point.
- [ ] `created_at_local` is launch time, not completion time.
- [ ] When endpoints only become known after create, the row is updated rather than duplicated — one row per pod.
- [ ] A create that raises leaves the row for the classifier to age out, consistent with Task 1 and ruling C1.

**Verify:** `pixi run pytest tests/cli/test_ephemeral_index_timing.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
def test_ephemeral_row_exists_before_create_returns() -> None:
    """Bug caught: the row landed at completion, so a kill mid-run left a
    billing Modal app that no state file and no kinoforge command could
    name. Reading the index during a live run showed {"rows": []}."""


def test_ephemeral_row_is_stamped_with_launch_time_not_completion_time() -> None:
    """Bug caught: a run launched 02:01:23 produced a row stamped 02:02:41,
    so any age-based reaping would under-count the pod's real lifetime."""


def test_ephemeral_row_is_updated_not_duplicated_when_endpoints_arrive() -> None:
    """One pod, one row."""
```

- [ ] **Step 2: Run, confirm RED.**
- [ ] **Step 3: Implement** — hoist the index write ahead of `create_instance`, stamp launch time, and make the post-create path update the existing row.
- [ ] **Step 4: Confirm GREEN**, run `pixi run pytest tests/cli -q`.
- [ ] **Step 5: Commit** `fix(ephemeral): write the index row before the pod exists`, then update U8 in `PROGRESS.md` as `docs`.

---

### Task 4: the sweeper covers ephemeral pods (spec C1)

**Goal:** `kinoforge sweeper start` actually reaps an idle ephemeral pod, so the safety net `CLAUDE.md` recommends is true.

**Files:**
- Modify: `src/kinoforge/core/reaper.py` (orphan classification), `src/kinoforge/cli/_main.py` (sweeper flags) and/or `src/kinoforge/core/config.py` — **which of these is decided by Step 1**
- Test: `tests/core/test_reaper_orphans.py` (create), `tests/cli/test_cmd_sweeper.py` (extend)

**Background and the first thing to establish:** U9 says the daemon exposes no `--include-orphans` flag so the index is unreachable "by construction". But `cfg.sweeper.include_orphans` already exists (`src/kinoforge/core/config.py:1222`) and is consumed at `:1841`. **Step 1 settles which is true.** If the config path already works, the enumeration half is a discoverability defect — a CLI flag plus documentation — not new plumbing. Do not build plumbing before checking.

The classification half is real either way: orphan rows carry no heartbeat and no last-used stamp, so a pod that merely answers is `LIVE` and no threshold promotes it. `created_at_local` supplies an age. **Age alone must never reap** — a long generation is not a leak. An orphan is reapable only when it is old enough AND idle on the GPU probe, and the reap must say what it observed.

**Acceptance Criteria:**
- [ ] Step 1's finding is recorded in the task report: does `cfg.sweeper.include_orphans` already reach the daemon's enumeration?
- [ ] An orphan row that is old enough AND idle on GPU classifies as reapable.
- [ ] An orphan that is old but NOT idle classifies as live and is not reaped.
- [ ] An orphan that is idle but young classifies as live and is not reaped.
- [ ] The reap decision states the age and the utilisation it observed.
- [ ] The daemon can reach the index — via config if that already works, otherwise via a new flag.

**Verify:** `pixi run pytest tests/core/test_reaper_orphans.py tests/cli/test_cmd_sweeper.py -v` → all pass

**Steps:**

- [ ] **Step 1: Establish the enumeration truth.** Read `config.py:1841`'s consumer and the sweeper's entry enumeration. Write down whether an operator setting `sweeper.include_orphans: true` in a config today gets orphan rows swept. Record the answer before writing any code.
- [ ] **Step 2: Write the failing classification tests** — the three cases above (old+idle reapable, old+busy live, young+idle live), each with a fake clock and a fake util probe so no live pod is needed.
- [ ] **Step 3: Run, confirm RED.**
- [ ] **Step 4: Implement** the orphan classification using `created_at_local` for age and the provider util probe for idleness. Conservative thresholds; a wrong reap destroys work.
- [ ] **Step 5: Close the enumeration gap** per Step 1's finding — a CLI flag if config already works, plumbing if it does not.
- [ ] **Step 6: Confirm GREEN**, run `pixi run pytest tests/core tests/cli -q`.
- [ ] **Step 7: Commit** `feat(sweeper): reap idle ephemeral pods`, then update U9 in `PROGRESS.md` as `docs`.

---

### Task 5: live proof of Tasks 1-4 on A10 (~$0.60)

**Goal:** Prove each fix against the real provider, including a deliberate mid-run kill, because offline green is what let the wrong cause get filed.

**Files:**
- Modify: `docs/modal-command-matrix.md` (the affected rows), `PROGRESS.md` (the affected urgent items)

**Acceptance Criteria:**
- [ ] **A1 proof:** run `provision`, kill the process mid-create, then from a NEW process confirm the row names the pod and `kinoforge destroy --id <id>` reaps it. Before the fix this required a raw `modal app stop`.
- [ ] **A2 proof:** launch an ephemeral generate, read `.kinoforge/_lifecycle/ephemeral-index.json` **while it is still running** and confirm a row naming the pod with a launch-time stamp, then kill the controller and confirm the pod is nameable and reapable.
- [ ] **B1 proof:** from a fresh process, `--attach-pod` a warm pod and have it attach.
- [ ] **C1 proof:** leave an ephemeral pod idle and confirm the daemon reaps it, with the reap naming the age and utilisation it saw.
- [ ] Teardown proof after every cell, from a fresh process. No pod alive at the end.
- [ ] Actual spend recorded per cell.

**Verify:** `pixi run -e live-modal kinoforge list` → both "no instances" lines, and the Modal app list shows no running `kinoforge-*` app

**Steps:**

- [ ] **Step 1: `pixi run preflight` → PASS.**
- [ ] **Step 2: A1 proof.** Launch `pixi run -e live-modal kinoforge provision -c examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml`, kill it once the provisional row appears, then recover it from a new process. Capture every command and its output.
- [ ] **Step 3: A2 proof.** Launch the ephemeral generate with the standard prompt, poll the index file from a second shell during the run, capture the row, kill the controller, recover the pod.
- [ ] **Step 4: B1 proof.** Boot a warm pod, then attach from a fresh process.
- [ ] **Step 5: C1 proof.** Leave the ephemeral pod idle, start the sweeper against a config with tight windows, and capture the reap decision.
- [ ] **Step 6: Teardown proof and spend reconciliation.**
- [ ] **Step 7: Update the matrix rows and urgent items with the live evidence, and commit.**

---

### Task 6: diagnose and fix the duplicate cold-boot (spec B3; one A100 pass, ~$0.60)

**Goal:** Find out why warm-attach rejected a live pod with the same key, then fix it.

**Files:**
- Modify: decided by Step 2 — candidates are `src/kinoforge/core/warm_reuse/matcher.py`, `src/kinoforge/core/warm_reuse/ephemeral_index.py`, or the ledger lookup feeding them
- Test: decided by Step 2
- Modify: `docs/modal-command-matrix.md`, `PROGRESS.md`

**Background:** the two configs in the reproducer differ only in `upscale.scale`, which the capability key does not distinguish, so key equality was not the discriminator. Task 0 has made the scan state its reason — including the previously silent zero-candidate case. Use it.

**This task is deliberately discovery-then-fix.** The fix is not specified because specifying it now would be guessing, and guessing is what produced U13's retracted suspected site.

**Acceptance Criteria:**
- [ ] The reproducer runs with Task 0's output visible and the recorded reason is captured verbatim.
- [ ] The cause is named from that evidence, not inferred from the error text.
- [ ] A failing test reproduces the cause offline, then the fix makes it pass.
- [ ] A second live upscale against a warm pod attaches instead of booting — the actual money proof.
- [ ] If the cause turns out to be structural rather than a guard, STOP: record the finding, re-file U14 with the real cause, and do not expand scope silently.

**Verify:** second live upscale logs a warm attach and no `✓ App deployed`

**Steps:**

- [ ] **Step 1: `pixi run preflight` → PASS.** Boot the first pod: `pixi run -e live-modal kinoforge upscale -c examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml --video <fixture>` (no `--no-reuse`, so it stays warm).
- [ ] **Step 2: Run the second config** (`modal-diffusers-flashvsr-1080p-upscale.yaml`, same fixture) and capture the warm-reuse line Task 0 guarantees. **This is the diagnosis.** If it names a skip reason, that is the cause; if it reports zero candidates, the cause is upstream in the candidate lookup rather than the eligibility filter.
- [ ] **Step 3: Destroy both pods and prove teardown** before doing any development. Do not hold an A100 while writing code.
- [ ] **Step 4: Write a failing test** reproducing the named cause offline, confirm RED, fix, confirm GREEN, run the warm-reuse test modules.
- [ ] **Step 5: Live re-proof** — boot one pod, run the second config, confirm it attaches. Destroy and prove teardown.
- [ ] **Step 6: Commit** the fix, then update U14 in `PROGRESS.md` and the T2-03 row in the matrix.

---

### Task 7: close the record

**Goal:** The urgent-items list and the matrix tell the truth about what is now fixed.

**Files:**
- Modify: `PROGRESS.md`, `docs/modal-command-matrix.md`

**Acceptance Criteria:**
- [ ] Every item this plan closed says FIXED with its commit; every item it did not close still says open.
- [ ] The urgent-section preamble and index reflect the new state — the campaign repeatedly caught stale "still open" text beside shipped fixes, so re-read the whole section, not only the entries you touched.
- [ ] The matrix summary states which defects are now closed and what the live proof cost.
- [ ] `pixi run pre-commit run --all-files` green and the full suite passes.

**Verify:** `pixi run pytest -q` → all pass, and `rg -n "U7|U8|U9|U14|U15" PROGRESS.md` shows each with a current status

**Steps:**

- [ ] **Step 1: Re-read the entire urgent-actions section** for stale claims, not only the entries this plan touched.
- [ ] **Step 2: Update the matrix summary** with what closed and the actual spend.
- [ ] **Step 3: `pixi run pre-commit run --all-files` and `pixi run pytest -q`.**
- [ ] **Step 4: Commit** `docs: record the four money leaks closed`.
