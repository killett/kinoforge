# Modal command matrix

**Purpose.** Run every `kinoforge` subcommand and the important flag combinations against the
Modal provider, and record one verdict per cell. The operator has been hitting errors on
assorted commands without a record of which; this document is that record.

**Started:** 2026-09-05
**Budget:** $20 hard. Tier 3 (Wan 2.2 14B) is capped at $4 and never retried.
**Plan:** `docs/superpowers/plans/2026-09-05-modal-command-matrix.md`
**Spec:** `docs/superpowers/specs/2026-09-05-modal-command-matrix-design.md`
**Logs:** `/home/claudeuser/kinoforge-matrix/logs/<cell id>.log` (operator-side, not tracked)

**Total spend: $2.61 of the $20 budget** (Tier 0 $0.00 + Tier 1a $0.38 + Tier 1b $0.28 + Tier 1c $0.13
+ Tier 1d $0.10 + Tier 2a ~$0.50 + Tier 2b ~$0.07 + Tier 3 $1.15). Tier 3, the one cell with a $4 cap,
came in at $1.15.

**Verdicts.** `PASS` — behaved as expected. `FAIL` — a crash, a traceback, or a wrong result.
`EXPECTED-REFUSAL` — refused cleanly and on purpose (not-found id, unsupported operation,
a guard firing). `SKIPPED` — deliberately not run. Cells not yet run carry the placeholder
verdict in the Verdict column.

**The Verdict column carries one of those four tokens and nothing else** — no warning glyph, no
qualifier, no "PASS but". Every nuance belongs in Notes. A `PASS ⚠️` in that column is how T1-28's
whole-clip render defect went unnoticed until the 2026-09-06 review; a cell whose own notes describe a
real defect is a FAIL.

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

17 cells, all run 2026-09-05. Four failed: two were cheap and were fixed in-session
(T0-02, T0-12), two are not and stay FAIL pending the follow-ups below (T0-07, T0-08).

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T0-01 | `kinoforge --version` | — | PASS | $0.00 | `logs/T0-01.log` | prints `kinoforge 0.5.0`, exit 0 |
| T0-02 | `kinoforge doctor -c <cfg>` | CFGS | PASS | $0.00 | `logs/T0-02.log` | FAILed on first pass — all five cfgs raised a `heartbeat_interval_required` ERROR and exited 1. Fixed in `c9d9b284`; re-run is exit 0 with zero `✗` rows on all five. Three WARN rows per cfg remain (modal cannot enforce `max_lifetime` / `job_timeout` / `heartbeat_interval_s`, and ignores `disk_gb` / `max_usd_per_hr`) — warnings, by design, not errors |
| T0-03 | `kinoforge deploy --config <cfg> --dry-run` | CFGS | PASS | $0.00 | `logs/T0-03.log` | plan printed for all five (engine, provider, placement, lifecycle ceilings, capability key), exit 0, no traceback, nothing provisioned |
| T0-04 | `kinoforge upscale -c VSRX4 --video FIX --dry-run` | VSRX4 | PASS | $0.00 | `logs/T0-04.log` | `scale: 4x`, `engine: flashvsr`, `no_reuse: False`, exit 0 |
| T0-05 | `kinoforge upscale -c VSR1080 --video FIX --dry-run` | VSR1080 | PASS | $0.00 | `logs/T0-05.log` | `scale: 1080p`, exit 0. The dry-run prints the raw cfg string, not the resolved target; `ScaleTarget(kind="height", value=1080)` is asserted by `tests/test_modal_config.py::test_flashvsr_1080p_config_is_height_target` |
| T0-06 | `kinoforge interpolate -c RIFE60 --video FIX --fps 60 --dry-run` | RIFE60 | PASS | $0.00 | `logs/T0-06.log` | `fps: 60.0`, `engine: rife`, exit 0 |
| T0-07 | `kinoforge generate -c WAN13B --mode t2v --prompt PROMPT --dry-run-swap` | WAN13B | FAIL | $0.00 | `logs/T0-07.log` | exit 0 and the swap plan printed (`loras_source: empty`, empty evict/download, `cost: 0.0s`) with no deploy — but the plan is **wrong**: `matcher: selected pod i5y9um06fxkq83` names a RunPod pod, dead since 2026-07-13, for a Modal cfg. A wrong result, so FAIL, not PASS. Not cheap to fix; see follow-up F1 |
| T0-08 | `kinoforge batch -c WAN13B --manifest batch.yaml --dry-run-swap` | WAN13B | FAIL | $0.00 | `logs/T0-08.log` | exit 0, but carrying the same wrong matcher result as T0-07 (F1), and the manifest is never parsed on this path — verified by re-running with a `--manifest` path that does not exist, which also exits 0. Two defects, neither cheap; see follow-ups F1 and F2 |
| T0-09 | `kinoforge grid --spec grid.yaml --out grid-dry.mp4 --dry-run` | WAN13B | PASS | $0.00 | `logs/T0-09.log` | `[grid dry-run] 2 cells, layout=1x2, budget_cap=$0.60`, exit 0. Spec lives outside the repo as the loader requires |
| T0-10 | `kinoforge list` | — | PASS | $0.00 | `logs/T0-10.log` | `[instance overview] No running instances.` + `No instances recorded in ledger.`, exit 0 |
| T0-11 | `kinoforge status --id does-not-exist` | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-11.log` | `instance 'does-not-exist' not found in ledger`, exit 1, no traceback |
| T0-12 | `kinoforge reap` / `kinoforge reap --format json` | — | PASS | $0.00 | `logs/T0-12a.log`, `logs/T0-12b.log` | FAILed on first pass: `--format json` printed the human line `reap: ledger empty (nothing to do)` rather than JSON, breaking any `jq` consumer on the empty ledger. Fixed in `3c7822b8`; the re-run emits `{"type": "header", "entries": 0}` under `--format json` and keeps the sentence under the default human format. Both exit 0 |
| T0-13 | `kinoforge cost -c WAN13B` / `-c WAN13B --json` | WAN13B | PASS | $0.00 | `logs/T0-13c.log`, `logs/T0-13b.log` | `Burn rate: $0.00/hr`, `(no entries in ledger)`, exit 0; `--json` emits a well-formed object. `-c/--config` is **mandatory** — the config-less forms in the brief exit 2 on argparse. No Modal row: the ledger is empty and `balance` is `{}` (no Modal balance adapter; documented absent) |
| T0-14 | `kinoforge gc --config WAN13B` | WAN13B | PASS | $0.00 | `logs/T0-14.log` | `gc: nothing to do (specify --run <id>)`, exit 0 on the empty store |
| T0-15 | `kinoforge sweeper status -c WAN13B` / `sweeper metrics -c WAN13B --prom` | WAN13B | PASS | $0.00 | `logs/T0-15a.log`, `logs/T0-15b.log` | `running=false`, `pid=none`, `sweeps_total=0`, exit 0; `--json` variant matches. `metrics` renders the Prom textfile exposition, exit 0. Both subcommands require `-c/--config` (exit 2 without it), and `metrics` additionally requires `--prom` |
| T0-16 | `kinoforge forget --id does-not-exist` | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-16.log` | `instance 'does-not-exist' not found in ledger`, exit 1, no traceback |
| T0-17 | bare `modal app list` without env, then with env loaded | — | EXPECTED-REFUSAL | $0.00 | `logs/T0-17a.log`, `logs/T0-17b.log` | without env: `Token missing. Could not authenticate client.`, exit 1 — expected. With the dotenv loader: the Apps table renders **empty**, exit 0, confirming no stray Modal app before Tier 1 |

**Tier 0 tally:** 12 PASS, 3 EXPECTED-REFUSAL, **2 FAIL outstanding** (T0-07 and T0-08, both
blocked on follow-up F1 and, for T0-08, F2). Two further failures were found and fixed in
session — T0-02 (`c9d9b284`) and T0-12 (`3c7822b8`) — and those cells now pass.

---
## Tier 1 — Wan 2.1 1.3B on A10 (~$0.70)

### Tier 1a — one warm pod through every stateful command

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-01 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` | WAN13B | PASS | $0.05 | `logs/T1-01.log`, `sheetA.png` | Cold boot, exit 0. App deployed in **86.9s**; `generate completed` at 01:05:37, **2m43s** wall from launch. Instance `run-20260906-010255` (A10, $1.10/hr), pod survived as designed (warm-reuse default). Util during generation: **gpu=100%**, cpu=5.9%, mem=0.5% — the pod was genuinely working. mp4 480x480/33f/16fps/2.06s. Frame-QA **PASS**: coherent alpine meadow + waterfall, correct golden-hour backlight, butterflies/wisps present, temporally stable, no false colour |
| T1-02 | `kinoforge list` | WAN13B | PASS | $0.00 | `logs/T1-02.log` | Exit 0. Pod listed twice as designed — the age/spend line (`age=0.2h est<=$0.1899`) and the identity line `provider=modal capability_key=0aaf4ee6e6c0` |
| T1-03 | `kinoforge status --id ID` | WAN13B | FAIL | $0.00 | `logs/T1-03.log` | Exit 0 and the state block is right (`provider_status=ready`, `cost_rate_usd_per_hr=1.1000`, `accrued_spend_usd=0.1901`, `verdict=HEARTBEAT_SUBSTRATE_MISSING`, stale-heartbeat advisory — all correct for Modal). **But `endpoints=unknown (no live endpoint)` is a wrong result**: the same ledger entry the command just read carries `endpoints={'8000': 'https://...modal.run'}` plus `last_gpu_util_percent`/`last_cpu_percent`. Neither the endpoint nor the util is rendered, so the cell's stated purpose (state + endpoints + util) is two-thirds unmet — and `live-constraints.md` tells the operator to get the pod URL from exactly this command. Root cause shared with T1-06; see follow-up F4 / **U3** |
| T1-04 | `kinoforge logs --id ID` and `--file server.log --out <path>` | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-04a.log`, `logs/T1-04b.log` | **FAILed as run; fixed in-session, now a clean refusal.** As run, exit 1 on both forms and **not** a clean 'unsupported on modal': it fetched `https://run-20260906-010255-8001.**proxy.runpod.net**/bootstrap.log` for a *Modal* pod and reported `HTTP 404 Not Found`, so a wrong-provider error read as 'the pod has no such log' while the pod was still billing. `_cmd_logs` did `del ctx  # ledger not consulted` and hard-coded the RunPod proxy template with no provider check. **Fixed in `c08c3cce`** — one guard: the handler now looks the id up in the ledger and, on any provider outside `_LOG_SIDECAR_PROVIDERS` (RunPod alone), refuses with `logs: unsupported on provider 'modal' (instance '<id>')` plus the Modal alternative, exit **2**, no network call, no `--out` file written, no traceback. An id absent from the ledger (destroyed / `forget`-ed pod) and an unreadable ledger both still fall through to the fetch, so post-mortem log pulls keep working. Verdict follows the T0-02 / T0-12 precedent: fixed in-session, so the cell records the fixed behaviour. Five red/green tests in `tests/cli/test_cmd_logs.py`. **U4 closed** |
| T1-05 | `kinoforge cost` / `--json` / `--no-cache` | WAN13B | PASS | $0.00 | `logs/T1-05a.log`, `logs/T1-05b.log`, `logs/T1-05c.log` | All three exit 0. Modal burn rate visible and correct: `Burn rate: $1.10/hr`, per-provider row `modal: $1.10/hr spend $0.21 balance N/A [LIVE=1]`. `--json` emits the stable schema incl. `heartbeat_partial_truth: [modal]`; `--no-cache` re-reads and agrees. `balance N/A` is expected — no Modal balance adapter (documented absent at T0-13) |
| T1-06 | `kinoforge pod lora ls ID` | WAN13B | FAIL | $0.00 | `logs/T1-06.log` | Exit 2, `pod lora ls: no endpoint URL for pod run-20260906-010255`. Not an empty inventory and not a clean 'unsupported' — the endpoint URL **is** in the ledger row. `provider.ensure_endpoints()` (the 'repairing door', `_commands.py:2413`) returns empty on Modal because `ModalProvider.endpoints` reads the per-process `_deployments` dict and falls back to `instance.endpoints`, which `get_instance()` builds from `modal app list` without any URL. Same root cause as T1-03; see follow-up F4 / **U3** |
| T1-07 | `kinoforge reap` and `reap --format json --id ID` | WAN13B | PASS | $0.00 | `logs/T1-07a.log`, `logs/T1-07b.log` | Both exit 0 and classify the pod LIVE without destroying it: `HEARTBEAT_SUBSTRATE_MISSING`, `1 entries classified — pass --apply to act on default policy`. `--format json --id` emits `{"type": "header", "entries": 1}` then the verdict object with `provider: modal` (the T0-12 fix holds on a non-empty ledger). Pod confirmed still alive afterwards. **Cosmetic defect:** the human table's verdict column is too narrow for the longest verdict, so it renders `HEARTBEAT_SUBSTRATE_MISSINGrun-20260906-010255` with no separator; see follow-up F6 |
| T1-08 | `kinoforge sweeper status` with no daemon | WAN13B | PASS | $0.00 | `logs/T1-08.log` | Exit 0 with `running=false`, `pid=none`, `sweeps_total=0`, `errors_total=0`. Requires `-c/--config` (as recorded at T0-15) |
| T1-09 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` (second process) | WAN13B | PASS | $0.02 | `logs/T1-09.log`, `sheetA.png` | Exit 0 in **40s** total. `warm-reuse: attached to run-20260906-010255` — no deploy line, so the matcher re-used the warm pod. **The matcher selected the Modal pod**, i.e. U1 did not bite (the index held only Modal rows after the operator's 2026-09-06 clear). Frame-QA **PASS**: coherent, prompt-adherent, stable |
| T1-10 | same + `--instance-id ID` | WAN13B | PASS | $0.02 | `logs/T1-10.log`, `sheetA.png` | Exit 0 in 50s, explicit attach, no deploy. Frame-QA **PASS with a soft flag**: content is coherent and prompt-adherent but noticeably hazier/more overexposed than its siblings, with a blurred face — seed variance at 1.3B/480px, not corruption |
| T1-11 | same + `--force-attach --instance-id ID` | WAN13B | PASS | $0.02 | `logs/T1-11.log`, `sheetA.png` | Exit 0 in 41s, attached bypassing the matcher, no deploy. Frame-QA **PASS**: the strongest clip of the set — clean subject, correct butterflies, stable camera push-in |
| T1-12 | `kinoforge --vault vault.yaml generate -c CFG --mode t2v` | WAN13B | FAIL | $0.01 | `logs/T1-12.log`, `logs/T1-12b.log` | Two-part failure. **(a)** Without `--prompt`: exit 2, `error: the following arguments are required: --prompt` — argparse still mandates it, so the vault can never *be* the prompt source. **(b)** Per the brief, retried with `--prompt ""`: exit 1 with an **uncaught traceback** (`ValueError: prompt yielded zero non-empty segments`) raised *after* `warm-reuse: attached`, i.e. it acquired and billed the pod before failing. Diagnosis: `vault.positive_prompt` is referenced in exactly one place in the tree — `register_vault_tokens` (`src/kinoforge/core/vault.py:228`), which only registers it as a *redaction token*. It is never wired into prompt resolution, so the `--vault` help text ('holding the positive prompt') describes a capability `generate` does not have. See follow-up F7 / **U5** |
| T1-13 | `kinoforge batch -c CFG --manifest batch.yaml --concurrent 1` then `--stream-format jsonl` | WAN13B | PASS | $0.05 | `logs/T1-13a.log`, `logs/T1-13b.log`, `sheetB.png` | Both runs exit 0. **One attach, no deploy** (`warm-reuse: attached to run-20260906-010255`), two artifacts each, `_batch_summary.json` written to the batch dir. Human format streams `[1/matrix-a] OK 36.2s <uri>` + a summary table; `--stream-format jsonl` emits well-formed `entry_start` / `entry_finish` / `batch_summary` records with per-entry `status`, `duration_s`, `uri`. Frame-QA **PASS** on all four clips; minor flag on `T1-13b/matrix-a`, whose first two frames are bloom-blown before converging |
| T1-14a | `kinoforge generate ... --run-id matrix-runid` | WAN13B | FAIL | $0.01 | `logs/T1-14a.log` | **Placement mechanics verified; quality UNVERIFIED.** Exit 0 and `--run-id matrix-runid` placed the clip at `.kinoforge/matrix-runid/d1921fdc15369a32.mp4` exactly as asked, so the flag does what it says. But the artifact was consumed by the T1-18 `gc --run matrix-runid` probe before a single frame could be pulled, so **no visual QA was ever performed on it and none now can be** — the file is gone. The binding rule (`live-constraints.md`: "Visual QA before any PASS") is unconditional, so this cell cannot be PASS on mechanics alone. Re-run needs `gc` sequenced after frame extraction, not before |
| T1-14b | `kinoforge generate ... --output-dir <dir>` | WAN13B | FAIL | $0.02 | `logs/T1-14b.log`, `sheetC.png` | **Mechanics verified; no quality signal.** Exit 0 and the publish line is correct: `output published: /home/claudeuser/kinoforge-matrix/out/20260906-012353_diffusers_Wan2.1-T2V-1.3B-Diffuser_x.mp4` (the flag **does** exist on `generate` — the truncated usage line in T1-12's argparse error is not the full flag list). Frames were pulled and read, but the probe ran on `--prompt "x"` instead of the standard prompt, so the clip is an abstract colour field: degenerate by construction, free of corruption, and **carrying no quality signal about the pod or the model**. Visual QA on a null prompt is not visual QA, so this is not a PASS |
| T1-14c | `kinoforge generate ... --no-output-dir` | WAN13B | FAIL | $0.01 | `logs/T1-14c.log`, `sheetC.png` | **Mechanics verified; no quality signal.** Exit 0, no publish line emitted, clip present in the store only — the flag suppresses publication as designed. Same disqualifier as T1-14b: the probe used `--prompt "x"`, so the frames read as an abstract colour field with no bearing on output quality |
| T1-15 | `kinoforge stop --id ID` | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-15.log` | Exit 1, no traceback, exactly the by-design refusal: `modal cannot pause billing; instances are either running or destroyed.` followed by the actionable `To tear it down: kinoforge destroy --id run-20260906-010255`. Pod confirmed still alive after (T1-16 destroyed it) |
| T1-16 | `kinoforge destroy --id ID` then `kinoforge list` + Modal app list | WAN13B | PASS | $0.00 | `logs/T1-16.log`, `logs/T1-16-proof-list.log`, `logs/T1-16-proof-apps.log` | Exit 0, `destroyed: run-20260906-010255` at 01:25:14 (pod lifetime 01:04:22 -> 01:25:14 = **20m52s**). **Teardown proof from new processes:** `kinoforge list` prints both required lines (`[instance overview] No running instances.` AND `No instances recorded in ledger.`); `modal app list` shows the single `kinoforge-*` app in state **`stopped`** with **0 tasks**. Nothing left running |
| T1-17 | `kinoforge forget --id ID` after destroy | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-17.log` | Exit 1, `instance 'run-20260906-010255' not found in ledger`, no traceback — correct, because `destroy` already removed the entry |
| T1-18 | `kinoforge gc --config CFG` | WAN13B | PASS | $0.00 | `logs/T1-18.log`, `logs/T1-18b.log` | Exit 0. Bare `gc --config` prints `gc: nothing to do (specify --run <id>)` even though the store now holds real artifacts — `--run` is required to act, matching T0-14 on the empty store. Exercised for real with `--run matrix-runid`: exit 0, `gc: removed 1 artifact(s)`, and the run directory is empty afterwards, so collection genuinely works |

**Why T1-14 is split and why none of the three is PASS.** The three probes were originally recorded as one PASS row on the strength of placement mechanics. That verdict was wrong: `live-constraints.md` makes visual QA a precondition of PASS with no mechanics-only exemption, and not one of the three artifacts cleared it — T1-14a was destroyed before extraction, T1-14b/c were rendered from `--prompt "x"`. The flags themselves look correct and the failure is in the evidence, not (as far as anyone can now tell) in the product; the rows say so explicitly rather than hiding it behind a green cell. Quality evidence for this pod rests entirely on the eight standard-prompt clips in `sheetA.png`/`sheetB.png` (T1-01, T1-09, T1-10, T1-11, T1-13), which is ample for the pod — it is simply not evidence about these three cells. **Nothing was re-run live to correct this**; the fix is a bookkeeping fix.

**Tier 1a tally (20 cells after the T1-14 split):** 11 PASS, 3 EXPECTED-REFUSAL, **6 FAIL**
(T1-03, T1-06, T1-12, T1-14a, T1-14b, T1-14c).

Actual spend **$0.38** — one A10 pod at $1.10/hr alive 01:04:22 -> 01:25:14 (20m52s) on
2026-09-06, carrying every cell. **The per-cell Cost column sums to roughly $0.21, not $0.38.**
The ~$0.17 difference is warm-pod idle time: the pod is billed for the whole 20m52s, including
the stretches between cells when nothing was generating (the offline cells T1-02 / T1-05 /
T1-07 / T1-08 / T1-15 / T1-17 / T1-18 are all $0.00 in the column yet each occupied wall-clock
on a running A10). The column attributes only the generating minutes to the cell that caused
them; the tier total is what was actually spent. A reader tallying the column will under-count
by design — always take the tier line as the spend of record.

Of the seven cells that failed as run, **one was fixed in-session**: `logs` (T1-04), whose cause
was a single guard, now refuses cleanly on Modal and is recorded EXPECTED-REFUSAL following the
T0-02 / T0-12 precedent — verified by five red/green unit tests, **not** by a live re-run, since
the A10 pod was long gone. Three of the remaining six are bookkeeping — the T1-14 split, where
the flags behaved but the artifacts never cleared visual QA (see above). The two genuine
product defects left standing are **the Modal pod's URL being unreachable from any fresh
process** (T1-03, T1-06 — the ledger has it, no read path consults it) and an advertised
`--vault` prompt capability that was never wired up (T1-12). `--vault`'s misleading help text
was corrected in-session; the wiring stays open. Cold boot, warm attach (all three forms),
batch, artifact placement and teardown all worked exactly as designed.

### Tier 1a utilisation readings (the health signal, per `live-constraints.md`)

Polled at 30-75s throughout. `gpu_util_percent=100.0, cpu=5.9, mem=0.5` at 01:05:24, mid-generation
— the pod was genuinely computing, not merely accruing cost. Readings of `gpu=0.0` at 01:06:39
onward are the **idle warm pod between cells**, with no generation in flight, so they are not the
stall signature the rule targets. No cell ever showed GPU 0% while a generation was running, and
no capture/destroy/fail-fast escalation was needed.

### Tier 1b — deploy-first lifecycle

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-19 | `kinoforge deploy --config CFG` (also `--diagnostic-mode`) | WAN13B | FAIL | $0.00 | `logs/T1-19.log`, `logs/T1-19b.log` | Exit 1 with an **uncaught traceback** before anything is booked: `ValueError: ModalProvider requires spec.setup_steps and spec.launch (the server boot command); got setup_steps=0 launch=None`. Root cause: `orchestrator.deploy` builds its `InstanceSpec` from a hard-coded **empty** `RenderedProvision(script="", image=image, ports=[], env_required=[])` (`src/kinoforge/core/orchestrator.py:2313-2323`) — it never calls `engine.render_provision`, unlike `deploy_session` and `_cmd_provision`. `kinoforge deploy` is therefore unusable on Modal, and on RunPod it would boot a pod with no server and no ports (the documented `forewgeluuy9qh` 2026-07-03 hazard). The Modal refusal itself is the *right* call — refusing to book a pod that could never serve — but it surfaces as a traceback, not a clean error. **Pre-launch provisional row confirmed working**: `kinoforge list` during boot showed `kinoforge-deploy-20260906-013616-45da4a provider=modal capability_key=<unknown>`, and per ruling C1 the row survived the raise (cleared here with `forget`). `--diagnostic-mode` was exercised free via `--dry-run`: exit 0, plan byte-identical to the plain dry-run — correct, the flag is RunPod-only by its own help text. **No spend.** See follow-up F8 / **U6** |
| T1-20 | `kinoforge provision -c CFG` | WAN13B | FAIL | $0.13 | `logs/T1-20.log` | Exit code not observed: the run was killed at ~7 min once `modal app list` revealed a live untracked container (see below), so the code is unrecorded rather than non-zero. Two defects. **(a)** It printed `provisioned: instance=''` — an **empty instance id**. `ModalProvider.create_instance` returned an instance whose id never reached the print, and the app was deployed as `kinoforge-` (bare prefix, no suffix) at `https://emmykillett--kinoforge--build-modal-app--locals--server.modal.run`. **(b)** `_cmd_provision` writes **nothing to the ledger** — no provisional row, no real row — so `kinoforge list` showed `No instances recorded in ledger.` while `modal app list` showed `ap-U8nQQQVqDECy3iTqpoKQ7j` `deployed` with **1 running task**, i.e. a live A10 billing at $1.10/hr that no kinoforge command could name or destroy. Recovered manually with `modal app stop -y ap-U8nQQQVqDECy3iTqpoKQ7j`; confirmed `stopped`/0 tasks. Alive 01:45:21 → 01:52 ≈ 7 min ≈ **$0.13 of unrecoverable spend**, the single most expensive defect in the campaign so far. Not a "re-provision of the existing instance" and not an "already provisioned" refusal — it is an unconditional second create with no ledger record. See follow-up F9 / **U7** |
| T1-21 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT --instance-id ID --skip-preflight` | WAN13B | PASS | $0.09 | `logs/T1-21-boot.log`, `logs/T1-21.log`, `logs/T1-21-util.log`, `sheetD.png` | **Precondition substituted, and this matters:** the cell's premise is an instance produced by `deploy`, and `deploy` cannot produce one (T1-19), so the instance came from a plain `kinoforge generate` cold boot at 01:51:07 (`run-20260906-015109`, A10, app deployed in **1.4 s** — the image was already baked from the 01:02 Tier 1a run — first mp4 at 01:54:09, **3m02s** wall). Util at 01:51:53, mid-generation: **gpu=95.0%**, cpu=5.9, mem=0.5 — genuinely computing. The cell's own command then exits 0 in **40 s** (01:58:01 → 01:58:41) with **zero deploy lines** in its log, i.e. it attached to the named instance and rendered a second clip. Both mp4s 480x480/33f/16fps. Frame-QA: cold-boot clip **PASS ⚠️** (coherent alpine meadow, waterfall, backlit subject, pink butterflies; the dress flips green → purple around frame 4 — 1.3B/480px instability, not false colour or temporal breakup); attach clip **PASS** (clean, stable, red dress, waterfall, glowing wisps, correct over-the-shoulder turn). So the `--instance-id --skip-preflight` attach works; the **deploy-first lifecycle this tier exists to prove does not**, because of T1-19 |
| T1-22 | `kinoforge reap --apply --id ID` (fallback `destroy --id ID`) then teardown proof | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-22.log`, `logs/T1-22b.log`, `logs/T1-22-proof-list.log`, `logs/T1-22-proof-apps.log` | `reap --apply --id run-20260906-015109` exits 0 and **declines to destroy**: `HEARTBEAT_SUBSTRATE_MISSING`, `acted on 0: 0 destroyed · 0 forgotten · 0 drift-skipped · 0 deferred · 0 failed`. Correct by design — `reap` acts on dead/idle/over-budget entries, and this pod was 8 minutes old and healthy, so `--apply` on a LIVE verdict is a no-op. `reap` is therefore **not** a teardown command for a warm pod; the documented fallback is. `destroy --id` exits 0 at 01:59:03 with `destroyed: run-20260906-015109` (lifetime 01:51:07 → 01:59:03 = **7m56s**). **Teardown proof, from new processes after the orchestrator exited:** `kinoforge list` prints `[instance overview] No running instances.` AND `No instances recorded in ledger.`; `modal app list` shows every `kinoforge-*` app `stopped` with **0 tasks** (`ap-OBPWR584j3WON7TSUYx0rB`, `ap-U8nQQQVqDECy3iTqpoKQ7j`, `ap-TxQQKII6kO7evRXi35KD5O`). The F6 verdict-column padding defect reproduced verbatim (`HEARTBEAT_SUBSTRATE_MISSINGrun-20260906-015109`) |

**Tier 1b tally:** 1 PASS, 1 EXPECTED-REFUSAL, **2 FAIL** (T1-19, T1-20). Actual spend **$0.28**
against a ~$0.10 estimate — the overrun is entirely T1-20's untracked orphan, which billed for
7 minutes before `modal app list` exposed it.

**The tier's stated goal is not met.** `deploy` → `provision` → `generate --instance-id` →
`reap --apply` does not work on Modal: the first step crashes without booking anything (T1-19,
**U6**) and the second books a pod that no kinoforge command can see or destroy (T1-20, **U7**).
Only the third and fourth steps behave, and the fourth only once the fallback is used. Two
independent code paths — `orchestrator.deploy` and `_cmd_provision` — each build an `InstanceSpec`
by hand instead of going through the one route (`deploy_session`) that is exercised by the
`generate` path, and each is broken in its own way.

### Tier 1c — ephemeral runs and the reapers

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-23 | `kinoforge --ephemeral generate -c CFG --mode t2v --prompt PROMPT` | WAN13B | PASS | $0.03 | `logs/T1-23.log`, `logs/T1-23-util.log`, `sheetE.png` | Exit 0 in **78 s** (02:01:23 → 02:02:41; app deployed in ~1.4 s off the warm image). App named opaquely as the EM1 contract requires: `kinoforge-eph-61ee7764`. Every acceptance criterion met — `kinoforge list` prints `No instances recorded in ledger.`, `ephemeral-index.json` carries exactly one row (`id=eph-61ee7764`, `provider=modal`, `kinoforge_key=0aaf4ee6e6c0`, endpoint `…modal.run`), and the artifact exists **only** where `--ephemeral` allows: published to `output/20260906-020241_…mp4` while the `.kinoforge/run-20260906-020124/` store copy was deleted at exit (verified — the store path 404s afterwards). Frame-QA **PASS**: coherent alpine meadow, tall waterfall, yellow butterfly, subject enters and turns with the prompt's over-the-shoulder smile, temporally stable, no false colour. **Monitoring gap recorded, not a defect in this cell:** the index row's `created_at_local` is `02:02:41`, i.e. it is written when the run *finishes*; the util poller found no endpoint in either the ledger or the index at 02:01:23 and 02:02:08, so no utilisation sample exists for the generation itself and a crash mid-run would have left a billing app with no record anywhere. See follow-up F10 / **U8** |
| T1-24 | `kinoforge sweeper start -c CFG --interval-s 30` + `status` + `metrics` + `stop` | WAN13B | FAIL | $0.06 | `logs/T1-24.log`, `logs/T1-24-status.log`, `logs/T1-24-metrics.log`, `logs/T1-24-stop.log` | Daemon started cleanly (`policy=['DEGRADED_REAP','GC_404','IDLE_REAP','OVERAGE_REAP','RESTART_LOOP_REAP','STALE_LEDGER','STALL_REAP'] include_orphans=False`), `status` and `metrics --prom` both exit 0 and render, `stop` exits 0 and the process is gone. **But it never reached `STALL_REAP` because it never saw the pod at all.** Run for ~3 min against a stall-tight cfg (`stall_window_s: 60`, `--interval-s 30`) with `eph-61ee7764` idle at `gpu=0.0 cpu=0.0` throughout (confirmed by a direct `/util` poll every 45 s): after 6 sweeps, `sweeps_total=6`, `destroys_total=0`, and **every** `deferred_*` counter also 0 — the sweeper classified zero entries per pass. It sweeps the ledger, which `--ephemeral` deliberately leaves empty, and `sweeper start` exposes **no `--include-orphans` flag** (`-c` and `--interval-s` are its only options), so the ephemeral index is unreachable from the daemon by construction. The "safety net for unsupervised runs" in `CLAUDE.md` therefore does not cover the one run shape that has no ledger row to fall back on. Two further defects on the same cell: `status`/`metrics` report `interval_s=60` (read from cfg) while the daemon is demonstrably running at the `--interval-s 30` override — **still open** — and `sweeper start` wrote a pseudo-instance row `sweeper:59be2fa1c7fc` (`provider=_sweeper`) that `kinoforge list` rendered as a running instance and that **survived `sweeper stop`**, so it had to be `forget`-ed by hand before the teardown proof could print its two required lines — **fixed in `7d535503`**: `sweeper stop` now removes its own liveness row once it confirms the daemon gone, and a start/stop cycle leaves `Ledger.entries()` empty (two red/green tests). **The cell stays FAIL** on the defect it was actually run to find: the daemon still cannot see an ephemeral pod (U9), which is larger than the one-guard bar. See follow-ups F11 / **U9** (open) and F12 / **U10** (row leak fixed; interval reporting open) |
| T1-25 | `kinoforge --ephemeral generate …` again (second app) | WAN13B | PASS | $0.02 | `logs/T1-25.log`, `sheetE.png` | Exit 0 in **40 s** (02:07:06 → 02:07:46). **No second app was created** — `warm-reuse: attached to eph-61ee7764`, so the matcher re-used the first ephemeral pod off its index row, which is the documented EM2 behaviour and not a defect; the cell's parenthetical "(second app)" assumed `--no-reuse` semantics the command does not carry. **The matcher selected the Modal row**, so U1 did not bite. Frame-QA **PASS**: the cleanest clip of the pair — magenta-dress subject, waterfall, golden wildflower field, glowing wisps, stable across all five frames. Artifact published to `output/20260906-020746_…mp4`; store copy removed at exit; ledger still empty |
| T1-26 | `kinoforge reap --include-orphans` then `--apply` | WAN13B | FAIL | $0.02 | `logs/T1-26a.log`, `logs/T1-26b.log`, `logs/T1-26c.log`, `logs/T1-26d.log` | The bare form is a clean, deliberate refusal: exit **4**, `error: --include-orphans requires --apply (Layer V opt-in safety)`. The `--apply` form then **does** discover the ephemeral row that the daemon could not (`verdict=LIVE  id=eph-61ee7764  provider=modal`) — and destroys nothing: `acted on 0: 0 destroyed · 0 forgotten · 0 drift-skipped · 0 deferred · 0 failed`. Retried with `-c` pointing at the stall-tight cfg (`idle_timeout: 5m`, `stall_window_s: 60`) in case the first run was using `Lifecycle()` defaults: identical `LIVE`, identical `acted on 0`. Orphan rows carry no heartbeat and no last-used timestamp (`hb_age_s=-`, `sent_age_s=-`), so an ephemeral pod that is merely *reachable* is `LIVE` forever and no threshold can promote it to `IDLE_REAP` or `STALL_REAP`. Combined with T1-24 the practical result is that **no automatic mechanism reaps an idle ephemeral Modal pod** — this one billed for 7m16s and was only ever going to stop because a human typed a command. Teardown fell to the documented EM2 path: `kinoforge destroy --id eph-61ee7764` under `-e live-modal` → exit 0, `destroyed orphan: eph-61ee7764 (no ledger entry, provider=modal)`, and the index row is gone (`{"rows": []}`). See follow-up F11 / **U9** |
| T1-27 | `kinoforge --ephemeral --debug-show-secrets list` | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-27.log` | Exit 2, no traceback, and the message names the reason rather than the rule: `error: --ephemeral and --debug-show-secrets are mutually exclusive (the debug flag bypasses log redaction, which ephemeral requires).` Refused at argument parse, before any provider or credential is touched |

**Tier 1c tally:** 2 PASS, 1 EXPECTED-REFUSAL, **2 FAIL** (T1-24, T1-26). Actual spend **$0.13** —
one ephemeral A10 (`eph-61ee7764`) alive 02:01:23 → 02:08:39 (7m16s) carrying all five cells.
**Teardown proof from new processes:** `kinoforge list` printed both required lines,
`ephemeral-index.json` is `{"rows": []}`, and `modal app list` shows all four `kinoforge-*` apps
`stopped` with 0 tasks. No entry was made in `successful-generations.md` — every generation in
this tier was `--ephemeral`, which bars it.

**The tier's second and third goals are not met.** Ephemeral generation itself is solid: opaque
app naming, empty ledger, index row, store copy cleaned, warm re-attach across processes, and a
clean mutual-exclusion guard. What does not work is *reaping* it. The sweeper daemon cannot see
ephemeral rows at all and has no flag to make it, and `reap --include-orphans --apply` sees them
but can only ever say `LIVE`. An `--ephemeral` run that loses its controller leaves a Modal pod
that no automatic mechanism will ever stop.

### Tier 1d — grid

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T1-28 | `kinoforge grid --spec grid.yaml --out grid.mp4 --max-parallel-groups 1` | WAN13B | FAIL | $0.05 | `logs/T1-28.log`, `logs/T1-28-util.log`, `logs/T1-28-proof-list.log`, `logs/T1-28-proof-apps.log`, `sheetF.png`, `sheetF2.png` | Exit **0** (`status=full`; the `[grid summary] composed mp4 → …` line is printed only on that status). 02:11:07 → 02:13, composed mp4 **960x480/33f/16fps** = two 480x480 cells side by side, both captioned (`cell A`, `cell B 17f`) — the 1x2 layout and the per-cell `spec.num_frames: 17` override both landed. `budget_cap_usd: 0.60` was **not crossed**, so the run exited 0 rather than 3; the exit-3 path is therefore *not* exercised by this cell, only the cap's presence in the plan (T0-09). Util during cell 0: **gpu=100.0%**, cpu=5.6 — real compute. **The cells do not share a warm pod, and that is deliberate, not a defect:** `_run_group` passes `no_reuse=True` for every plain `generate:` cell (`src/kinoforge/core/grid/executor.py:852`), so each cell boots its own Modal app and auto-destroys — pod survival is reserved for LoRA-swap-mode cells, which pass `--attach-pod` and `no_reuse=False` (`executor.py:415`). Two apps were created (`…__cell0` 02:11-02:12, `…__cell1` 02:12-02:13), each ~1 min. Frame-QA: **cell A pass** — coherent waterfall/meadow, glowing butterfly, over-the-shoulder smile, temporally stable. **cell B fails** — a hard horizontal seam across the lower third with a flat red-brown corduroy-textured band replacing the flower field, **present in every frame**. It is inside cell B's own frame (not a compose seam), so the composed artifact this cell exists to produce is half-defective. **Verdict reclassified PASS ⚠️ → FAIL on 2026-09-06 review:** the cell's acceptance criterion demanded an unqualified frame-QA pass on the composed mp4, and a whole-clip structural band is a wrong result, not a transient wobble — the same standard that made T0-07 a FAIL for a wrong-but-exit-0 plan. The defect did not recur in T1-29's 17-frame cell, so the *likely* cause is seed variance at 17 frames rather than a systematic grid defect, and **the `grid` machinery itself is sound** — spec load, per-cell cfg override, sequential groups, ffmpeg compose with captions, exit-code mapping and per-cell teardown all behaved (see the Tier 1d tally). No fix commit and no urgent item: nothing in kinoforge is implicated, so re-running the cell is the remedy, and the campaign's no-retry budget rule keeps it unrun. **Teardown proof from new processes:** `kinoforge list` both lines, `modal app list` both grid apps `stopped`/0 tasks. Logged as `successful-generations.md` §29, which carries a caveat banner recording this defect |
| T1-29 | `kinoforge grid … --ephemeral` with `--out grid-eph.mp4` | WAN13B | FAIL | $0.05 | `logs/T1-29.log`, `logs/T1-29-proof-list.log`, `sheetG.png` | Exit **0** and the composed mp4 is correct (960x480/33f, both cells captioned, frame-QA **PASS** on both — cell A a strong backlit maroon-dress silhouette over a yellow flower field, cell B a clean teal-dress render with glowing pollen; no trace of T1-28's cell-B band). The ledger and the ephemeral index are both empty afterwards. **But `--ephemeral` is silently ignored.** The flag's help text says "pass-through to each underlying generate"; `_cmd_grid` (`src/kinoforge/cli/_commands.py:3612`) does `del ctx`, never reads `args.ephemeral`, and calls `run_grid(spec=…, output_dir=…, max_parallel_groups=…, out_path=…)` — the string `ephemeral` does not appear anywhere in `src/kinoforge/core/grid/`. The proof is on Modal's side: the two apps are named **`kinoforge-grid_20260906-022232_a61d55b2__cell0` / `__cell1`**, not the opaque `kinoforge-eph-<8hex>` shape that EM1's STRICT_POLICY requires, so the run id, the local timestamp and the cell index were all published to the provider. The clean ledger is an artefact of the `no_reuse=True` teardown described at T1-28, **not** of ephemeral mode — a plain non-ephemeral grid leaves exactly the same clean ledger. A user asking for ephemeral got a normal run with an identity-leaking app name and no warning. See follow-up F13 / **U11** |

**Tier 1d tally:** 0 PASS, **2 FAIL** (T1-28, T1-29). Actual spend **$0.10** — four A10 apps, each
alive about a minute (02:11-02:13 and 02:22-02:25). The two failures are unrelated in kind, and the
distinction matters: **T1-29 is a code defect** (`--ephemeral` accepted and dropped, U11), while
**T1-28 is an output defect** — the command worked and the render did not. `kinoforge grid`'s own
machinery is therefore healthy on Modal: spec load, per-cell cfg overrides, sequential group execution
under `--max-parallel-groups 1`, ffmpeg compose with captions, correct exit-code mapping, and automatic
per-cell teardown all behaved on both cells. What does not work is the `--ephemeral` flag, and what was
not obtained is a clean composed clip on the first attempt.

**Teardown proof (Tier 1d, from new processes):** `kinoforge list` →
`[instance overview] No running instances.` + `No instances recorded in ledger.`;
`ephemeral-index.json` → `{"rows": []}`; `modal app list` → all eight `kinoforge-*` apps of the
2026-09-06 session in state `stopped` with **0 tasks**.

---

## Tier 2 — FlashVSR upscale and RIFE interpolate (~$0.55)

### Tier 2a — FlashVSR upscale on A100-80GB

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T2-01 | `kinoforge upscale -c VSRX4 --video FIX` | VSRX4 | FAIL | $0.32 | `logs/T2-01.log`, `logs/T2-01.util.log` | **Boot and compute fine; the job dies at the mp4 encode.** Cold boot succeeded: image bake + `✓ App deployed in 538.837s` (8m59s — the slowest boot in the campaign, as expected for A100 + FlashVSR, and well inside `boot_timeout: 45m`). Instance `upscale-20260906-023846` (A100-80GB, $2.50/hr). **Util proves real compute**, not wall-clock accrual: `gpu_util_percent=100.0, cpu=6.1, mem=1.0` at 02:48:28. Then the job failed server-side: `kinoforge.core.errors.UpscaleFailed: upscale job u-44451cdebe... failed on server: Cannot change width after codec is open.` — a PyAV/imageio encoder error raised writing the upscaled clip, at `_pod_http.py:147`. **No artifact was produced, so no frame QA was possible and the cell cannot be PASS.** Reproduced identically at T2-03 and T2-05b (3/3), so it is deterministic, not a flake. This exact path was live-green on 2026-07-10 (§24), so it read as environment drift in the freshly-baked image rather than a code regression. **That hypothesis is now CONFIRMED** by a $0 CPU-only Modal build on 2026-09-06 (`tools/diagnose_flashvsr_writer_modal.py`): the image carries `av` **18.1.0** with `imageio` 2.37.4, and the same `imwrite` call fails on plain uint8 zeros with no GPU and no FlashVSR present. `av` 17.1.0 and below write fine, so the break is exactly at `av` 18 and the pin is `av<18`. The unpinned `"av"` lives in shared provision code (`upscalers/flashvsr/_engine.py:163`), not in the cfg, so **RunPod and SkyPilot are on the same fuse**. Larger than the one-guard bar. **Second defect in the same cell:** after the exception the CLI **never exited** — still running 16 minutes later with the pod up, killed manually. See follow-ups F14 / F15 and **U12** / **U13** |
| T2-02 | `kinoforge upscale -c VSRX4 --video FIX --scale 1080p --attach-pod ID` | VSRX4 | EXPECTED-REFUSAL | $0.00 | `logs/T2-02.log` | Exit 2, no traceback, refused at argument validation *before* touching the pod: `error: --scale 1080p deferred to a later session; use --scale Nx for v1`. A deliberate guard — the height-target path is a **cfg** key (`upscale.scale: 1080p`, the VSR1080 cfg, live-green as §27), not a CLI flag, and the CLI says so plainly. Correct behaviour, but note what it costs the matrix: because the refusal fires before attach, **this cell never exercised `--attach-pod`**, so warm-attach on the `upscale` path is left unproven here (T2-03 covers the matcher instead, and fails). The brief's efficiency plan — T2-02 warm-attaching to T2-01's pod rather than paying a second A100 boot — could not run as written |
| T2-03 | `kinoforge upscale -c VSR1080 --video FIX` | VSR1080 | FAIL | $0.08 | `logs/T2-03.log` | **Two findings, both negative.** (a) **It did not warm-attach.** T2-01's pod `upscale-20260906-023846` was alive and idle at 0% GPU when this launched, and the run log shows this cfg resolving to the **same capability key `7afe34198cc9`** — yet the matcher cold-booted a *second* A100 app, `upscale-20260906-025335`, putting two $2.50/hr cards on the clock at once. Deploy took only `1.477s` because the image was now cached, so the answer to the brief's "attach or cold boot?" is **cold boot, despite an identical key on a live pod**. See follow-up F16 / **U14**. (b) It then died at exactly the same encode: `UpscaleFailed: upscale job u-c11cfbd94d... failed on server: Cannot change width after codec is open.` No artifact, no frame QA |
| T2-04 | `kinoforge destroy --id ID` + proof | VSRX4 | PASS | $0.00 | `logs/T2-04-proof-list.log`, `logs/T2-04-proof-apps.log` | Exit 0 on both pods — `destroyed: upscale-20260906-023846` and `destroyed: upscale-20260906-025335` at 02:55:3x. **Teardown proof from a new process after the orchestrator exited:** `kinoforge list` prints both required lines (`[instance overview] No running instances.` AND `No instances recorded in ledger.`), and a `modal app list` scan counts **0 non-stopped `kinoforge-*` apps**. Both A100s off the clock |
| T2-05 | `kinoforge --ephemeral upscale -c VSRX4 --video FIX --scale 1080p --no-reuse` + proof | VSRX4 | EXPECTED-REFUSAL | $0.00 | `logs/T2-05.log` | Exit 2, identical guard to T2-02: `error: --scale 1080p deferred to a later session; use --scale Nx for v1`, raised before any pod work, so the cell cost nothing and started nothing. The `--ephemeral` and `--no-reuse` flags were never reached |
| T2-05b | same, **without** `--scale 1080p` (added so the cell tested something) | VSRX4 | FAIL | $0.10 | `logs/T2-05b.log` | Added because T2-05 as written refuses before doing anything, leaving the ephemeral + `--no-reuse` teardown path — the money-critical half of the cell — untested. Result: the upscale died at the same encoder (3/3, `u-0814ca0238...`), **but the teardown behaved correctly under failure**, which is the reassuring finding here: `--no-reuse` destroyed the pod even though the job raised, verified from a new process as **0 non-stopped `kinoforge-*` apps** while the CLI was still hung. So the hang of F15 / U13 is in the **post-teardown unwind**, not before the destroy — it does not leak a pod. Recorded FAIL on the generation, not on the teardown |

**Tier 2a tally (6 cells):** 1 PASS, 2 EXPECTED-REFUSAL, **3 FAIL** (T2-01, T2-03, T2-05b).
Actual spend **~$0.50** across three A100-80GB containers at $2.50/hr — `upscale-20260906-023846`
(02:47:57 → 02:55:35, 7m38s, $0.32), `upscale-20260906-025335` (02:53:46 → 02:55:42, ~2m, $0.08)
and the T2-05b ephemeral pod (~2.5m, $0.10). Modal's image **build** time is billed on the
builder, not on the A100, so the 8m59s first boot is not 8m59s of A100; the per-cell figures above
are container time. The two pods that overlapped did so because T2-03 refused to warm-attach.

**FlashVSR upscale on Modal is currently non-functional**, so no `successful-generations.md`
See-also line is added under §24 or §27 — there was no successful generation to record. Every one
of the three real attempts reached the GPU, computed (gpu=100%), and then lost the result at the
container's video-writer. The failure is deterministic and identical across two different cfgs
(VSRX4 and VSR1080) and both the warm-reuse and `--ephemeral --no-reuse` routes, which rules out
the cfg and the lifecycle route and points squarely at the baked image. **Nothing here was fixed
in-session:** an unpinned dependency in a baked container image is not a one-guard fix, and each
verification attempt costs a fresh A100 boot. Filed as **U12** (encoder), **U13** (post-failure
hang) and **U14** (warm-attach miss).

### Tier 2b — RIFE interpolate on T4

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T2-06 | `kinoforge interpolate -c RIFE60 --video FIX --fps 60 --no-reuse` | RIFE60 | PASS | $0.04 | `logs/T2-06.log`, `logs/T2-06.util.log`, `frames/T2-06.png` | Exit 0, **3m30s end to end including the first image bake** — cold boot at 03:01:52, provision at 03:04:55, artifact materialised 03:05:21, published `output/20260906-030522_interpolated_rife_interp_interpolate.mp4`. `ffprobe`: **480×480, 60/1 fps, 304 frames, 5.066667s** against the source's 480×480 / 16 fps / 81 frames / 5.0625s — 3.75× the frames at 3.75× the rate, with **duration preserved**, which is the invariant that matters (a naive frame-doubler that leaves fps alone would stretch the clip to 19s). Frame-QA **PASS** (5-frame contact sheet, judged against the source sheet): the golden-hour meadow, backlit waterfall, red/purple dress and glowing butterflies are all exactly as in the source, colour is natural with **no false-colour cast** (the 2026-07-03 failure mode), and the sampled frames show no ghosting, warping or subject morphing. `--no-reuse` tore the pod down on its own (`--no-reuse: destroyed + forgot pod interpolate-20260906-030152`) |
| T2-07 | same cfg, `--fps 32 --no-reuse` (**not** on T2-01's output — see note) | RIFE60 | PASS | $0.03 | `logs/T2-07.log`, `frames/T2-07.png`, `logs/T2-07-proof-list.log`, `logs/T2-07-proof-apps.log` | Exit 0 in **32s** — the image was cached from T2-06, so this is the warm-image cold-boot cost. `ffprobe`: **480×480, 32/1 fps, 162 frames, 5.0625s** — exactly 2× the source's 81 frames at exactly 2× its 16 fps, duration bit-identical to the source. The alternate-fps path resolves correctly and is not hard-coded to the cfg's `fps: 60.0`. Frame-QA **PASS**, indistinguishable from the source sheet, no artefacts. **What this cell does NOT test, stated plainly:** the brief specified `--video <T2-01 output>` to put a 1920² clip through RIFE on a 16GB T4 and find out whether it OOMs. T2-01 produced no artifact (see U12), so **that input did not exist** and the fixture was substituted. The T4-VRAM question the cell was designed to answer is therefore **untested**, and stays open until FlashVSR on Modal produces a clip again. Incidental finding relevant to U14: `--fps 32` resolved to capability key `82e591e03237` where `--fps 60` gave `ce2647e63e90`, so the key *does* discriminate fps — while at T2-03 it did **not** discriminate `upscale.scale` |

**Tier 2b tally (2 cells):** **2 PASS**, 0 FAIL. Actual spend **~$0.07** on two T4 containers
(~$0.60/hr), 3m30s and 32s. **RIFE interpolate on Modal works**, at both the cfg fps and a CLI
override, with correct frame-count and duration arithmetic and clean frames — the one capability
in Tier 2 that is healthy. A See-also line is added under `successful-generations.md` §25.
`--no-reuse` self-teardown fired on both, and the proof from a new process after the orchestrator
exited shows `[instance overview] No running instances.`, `No instances recorded in ledger.` and
**0 non-stopped `kinoforge-*` apps**.

**Tier 2 total spend: ~$0.57** (2a ~$0.50 on A100-80GB, 2b ~$0.07 on T4).

---

## Tier 3 — Wan 2.2 14B on A100-80GB (capped $4)

| Cell | Command | Config | Verdict | Cost | Evidence | Notes |
|------|---------|--------|---------|------|----------|-------|
| T3-01 | `kinoforge generate -c WAN14B --mode t2v --prompt PROMPT --no-reuse` | WAN14B | PASS | $1.15 | `logs/T3-01.log`, `logs/T3-01-util.log`, `logs/T3-01-boot.log`, `sheetT3.png` | Exit 0. **Cold boot 24m25s, generation ~3m06s, total pod life 27m37s** — 03:32:28 create → `✓ App deployed in 84.812s` at 03:33:58 (image built in 81.4 s) → `provisioner.provision` → server up ~03:56:53 (`/util` reported `uptime_seconds=68` at 03:58:01) → artifact published 03:59:59 → `--no-reuse: destroyed + forgot pod run-20260906-033228` at 04:00:05. Instance `run-20260906-033228` (A100-80GB, **$2.50/hr**) → **$1.15**, well inside the $4 cap. **The weights were fetched COLD, and that is the number that matters here:** before launch the `kinoforge-hf-cache` Modal Volume held only `models--Wan-AI--Wan2.1-T2V-1.3B-Diffusers`; after the run it also holds `models--Wan-AI--Wan2.2-T2V-A14B-Diffusers`. So ~23 of the 27.6 billed minutes were the ~63 GB HF snapshot, and **a warm re-run off this now-populated volume should cost roughly $0.30 rather than $1.15** — the single biggest lever on this cell's price. **Utilisation proves real compute:** `gpu_util_percent=100.0, cpu=6.0, mem=0.5` at both 03:58:01 and 03:59:18, mid-generation. During the fetch phase `/util` is unreachable by design (the server is not up yet), so the boot-phase health signal was the Modal container's existence and the app's task count, polled every ~80 s: `containers=1 app=[deployed tasks=1]` throughout, never flat-lining. mp4 **480x480 / 81 frames / 16 fps / 5.06 s**, 1,195,886 B — exactly the cfg's `spec`. **Frame-QA PASS** (5 frames via `ffmpeg_frames_by_count`, montage `sheetT3.png`, plus a 3x crop of the subject): coherent alpine meadow of red/orange/yellow wildflowers, tall waterfall down mossy cliffs, correct golden-hour backlight with the sun flaring over the ridge, glowing butterflies and wisps, and the prompt's specific beats all land — the camera pushes in across the five frames and she turns to glance over her shoulder with a legible smile. Temporally stable, no false colour, no seams or banding. *Soft flags, named rather than hidden:* the multi-panel red/blue/yellow dress is flag-like and its drape is loosely resolved, and her right hand is indistinct where it meets the yellow fabric — ordinary 14B character rendering at 480², not corruption, and nothing on the order of T1-28's whole-clip band. **Cap enforcement, stated plainly:** an 85-minute watchdog was armed before launch and did fire at 04:58, but the pod had already been gone for 58 minutes, so it killed an already-dead process tree and destroyed nothing. It did not save this run; `--no-reuse` did. **Teardown proof from new processes after the orchestrator exited:** `kinoforge list` prints `[instance overview] No running instances.` AND `No instances recorded in ledger.`; `modal container list` is empty; `modal app list` shows **0 non-stopped `kinoforge-*` apps**. Recorded as a See-also under `successful-generations.md` §23 |

**Tier 3 tally (1 cell):** **1 PASS**, 0 FAIL. Actual spend **$1.15** on one A100-80GB alive
27m37s. **`kinoforge generate` works on Modal at the top of the model range**, unchanged from the
1.3B path — same command, same `--no-reuse` teardown, same artifact placement, no 14B-specific
handling and no failure. The cell was run once, last, and not retried, per the operator's recorded
decision; it came in at **29% of its $4 cap** because generation is cheap and the cold weight fetch
is not.

**A capability gap this cell makes concrete.** The run log warns that `max_lifetime: 90m` never
reaches Modal — the enforced ceiling is `@app.function(timeout=...)` derived from
`boot_timeout: 45m`, so a 14B cold boot that hung would have been cut at 45 minutes by Modal and
not at 90 by the cfg. That is follow-up **F3** stated in money: on this cfg the provider's ceiling
is *tighter* than the cfg's, which is the safe direction, but it is not the number the cfg says.

---

## Follow-ups

Filed from Tier 0. Each is larger than the "one function / one config key / one guard" bar
the campaign's fix policy sets, so each is recorded rather than fixed. The two failures that
*did* meet that bar were fixed in-session instead of filed: the `doctor` heartbeat ERROR
(`c9d9b284`) and the `reap --format json` empty-ledger path (`3c7822b8`). Two more met it later:
the `logs` provider guard and the `--vault` help text, both in `c08c3cce`.

**F1 — the ephemeral warm-reuse matcher is provider-blind.** `--dry-run-swap` on a
Modal cfg (T0-07, T0-08) selected pod `i5y9um06fxkq83`, a **RunPod** pod whose
`.proxy.runpod.net` endpoints have been dead since 2026-07-13. Diagnosis:
`find_warm_attach_candidate` (`src/kinoforge/core/warm_reuse/matcher.py`) never mentions
`provider`, and `EphemeralIndex.rows_by_wak` /
`rows_by_kinoforge_key` (`src/kinoforge/core/warm_reuse/ephemeral_index.py:188-194`)
filter on the warm-attach / capability key alone. The key does not encode the provider,
so a row written by one provider is a legal candidate for any other. The three stale
RunPod rows that triggered this have since been cleared from
`.kinoforge/_lifecycle/ephemeral-index.json` by the operator (backup kept outside the
repo), so the live tiers no longer carry that confound — but the **defect is unfixed**:
the next Modal ephemeral run writes rows that a RunPod cfg could equally claim, and vice
versa. Note also that `kinoforge list` never surfaces index rows at all (it reads the
ledger only), so this residue is invisible from the CLI. Fix shape: filter index rows by
`cfg.compute.provider`, or fold the provider into the warm-attach key. Blast radius spans
the warm-reuse tests, hence not attempted here.

**F2 — `batch --dry-run-swap` never reads the manifest.** T0-08 exits 0 with
`--manifest /home/claudeuser/kinoforge-matrix/NOPE.yaml`, a path that does not exist.
The flag short-circuits into `_dry_run_swap_preview` before the manifest is parsed, so
the cell proves the swap preview works but proves nothing about the manifest. Either
validate the manifest before the early return, or document that the flag is
matcher-only.

**F3 — Modal's declared guardrail gaps are worth a doc row, not a fix.** Every Modal cfg
warns that the provider cannot enforce `max_lifetime`, `job_timeout` or a wire-level
`heartbeat_interval_s` read, and ignores `disk_gb` and `max_usd_per_hr`; the effective
ceiling is Modal's `@app.function(timeout=boot_timeout)`. Correct and already surfaced by
`doctor`, but it means the cfg-level budget knobs on the Modal cfgs are decorative. Worth
stating in `docs/lifecycle.md` so the next operator does not trust them.

**F4 — a Modal pod's endpoint URL is unreachable from a fresh process.** `kinoforge status`
renders `endpoints=unknown (no live endpoint)` (T1-03) and `kinoforge pod lora ls` exits 2 with
`no endpoint URL for pod <id>` (T1-06), for a pod whose ledger entry holds
`endpoints={'8000': 'https://...modal.run'}`. `ModalProvider.endpoints`
(`src/kinoforge/providers/modal/__init__.py:370`) reads the per-process `_deployments` dict and
falls back to `instance.endpoints`; `get_instance()` builds the `Instance` from `modal app list`,
which returns no URL. So the URL survives only in the process that created the pod, and in the
ledger nobody reads. `_render_endpoints_for_status`'s docstring already anticipates this and
defers it deliberately for RunPod/Modal — but the consequence is now measured: the campaign's own
`live-constraints.md` instructs the operator to get the pod URL from `kinoforge status`, and that
does not work. This task had to read `.kinoforge/_lifecycle/ledger.json` directly to poll `/util`.
Fix shape: have the provider (or `_cmd_status` / `ensure_endpoints`) fall back to the ledger
entry's `endpoints` map. Filed as **U3**.

**F5 — `kinoforge logs` is hard-wired to the RunPod proxy.** `_cmd_logs`
(`src/kinoforge/cli/_commands.py:2510-2512`) carries the comment `del ctx  # ledger not consulted
— proxy URL is deterministic from id` and builds
`https://{id}-8001.proxy.runpod.net/{file}` unconditionally. On Modal (T1-04) this invents a
hostname that has never existed and surfaces `HTTP 404 Not Found`, which an operator reads as
"the pod has no log" rather than "this command does not work on this provider". The sidecar is a
RunPod-shaped thing, so being unsupported on Modal is legitimate — presenting it as a 404 is not.
**RESOLVED in `c08c3cce`** — the handler now consults the ledger and refuses cleanly (exit 2, provider
named, no fetch, no `--out` write) on any provider outside `_LOG_SIDECAR_PROVIDERS`; an id the
ledger does not hold still falls through to the fetch so post-mortem pulls keep working. T1-04 is
now EXPECTED-REFUSAL. **U4 closed.**

**F6 — `reap`'s human table under-pads the verdict column.** With the longest verdict string the
column runs into the id: `HEARTBEAT_SUBSTRATE_MISSINGrun-20260906-010255` (T1-07). Cosmetic — the
JSON format is unaffected and the classification is correct — but it makes the default output
unparseable by eye at exactly the moment an operator is deciding whether to reap.

**F7 — `--vault` cannot supply the prompt it advertises.** `kinoforge generate` requires
`--prompt` at argparse even when `--vault` is given, and `--prompt ""` dies with an uncaught
`ValueError: prompt yielded zero non-empty segments` *after* the pod has been attached and billed
(T1-12). `vault.positive_prompt` appears exactly once in the tree — `register_vault_tokens`
(`src/kinoforge/core/vault.py:228`), which registers it for redaction only. Nothing ever reads it
as the prompt. Either wire vault -> prompt resolution and make `--prompt` conditionally optional,
**PARTLY RESOLVED in `c08c3cce`** — the help text no longer promises a vault "holding the positive
prompt"; it now says the vault supplies redaction tokens and that `--prompt` is still required and
is the only prompt source. **The wiring itself is untouched:** vault -> prompt resolution, making
`--prompt` conditionally optional, and failing the empty-prompt path *before* a pod is acquired
rather than after all remain open. **U5 stays open** for the wiring.

**F8 — `kinoforge deploy` never renders the engine's provision, so it can never boot a server.**
`orchestrator.deploy` builds its `InstanceSpec` from a hard-coded empty
`RenderedProvision(script="", image=image, ports=[], env_required=[])`
(`src/kinoforge/core/orchestrator.py:2313-2323`) — `engine.render_provision` is never called on
this route, unlike `deploy_session` and `_cmd_provision`. On Modal the provider refuses outright
(`ValueError: ModalProvider requires spec.setup_steps and spec.launch`), which is the correct
refusal delivered as an uncaught traceback; on RunPod the same spec would book a pod with no
ports and no bootstrap, the documented `forewgeluuy9qh` 2026-07-03 failure. Fix shape: render the
provision inside `_build_spec` and thread `env_required` / `ports` the way `deploy_session` does.
Not attempted here — it moves the launch payload for every provider, so the golden suite moves
with it, which is past the campaign's one-function bar. Filed as **U6**.

**F9 — `kinoforge provision` books an instance that nothing records and nothing can destroy.**
`_cmd_provision` calls `provider.create_instance(spec)` directly and writes **no ledger row at
all** — not the provisional row `deploy` writes, not a real row afterwards. On Modal the created
app is named from an empty instance id (`kinoforge-`, printed as `provisioned: instance=''`), so
even the app name carries no id to reap by. Observed live at T1-20: `kinoforge list` reported an
empty ledger while `modal app list` showed `ap-U8nQQQVqDECy3iTqpoKQ7j` `deployed` with one running
container at $1.10/hr; recovery required a bare `modal app stop -y`. Fix shape: route `provision`
through the same pre-launch-row contract as `deploy` (F12/ruling C1), and make the empty instance
id an error rather than an app name. Filed as **U7**.

**F10 — an `--ephemeral` run has no durable record until it has already finished.**
`_record_cold_instance` calls `_ephemeral_index_add` (`src/kinoforge/cli/_commands.py:583`) only
after the orchestrator returns, so the index row's `created_at_local` is the run's *completion*
time. Observed at T1-23: the row for `eph-61ee7764` is stamped `02:02:41` for a run launched at
`02:01:23`, and a poller reading both `.kinoforge/_lifecycle/ledger.json` and
`ephemeral-index.json` found nothing at `02:01:23` or `02:02:08`. This is the F12 hole that ruling
C1 closed for the ledger, still open on the ephemeral path: a controller killed mid-generation
leaves a billing Modal app that neither state file names. Fix shape: write the index row before
`create_instance` (the pre-launch contract `deploy` already implements) and age it out the same
way. Filed as **U8**.

**F11 — nothing automatic reaps an idle ephemeral pod.** Two halves, one consequence.
`kinoforge sweeper start` sweeps the ledger only and exposes no `--include-orphans` flag, so the
ephemeral index is unreachable from the daemon: at T1-24 six consecutive sweeps against an idle
`gpu=0.0` pod classified zero entries (`sweeps_total=6`, `destroys_total=0`, every `deferred_*`
counter 0). `kinoforge reap --include-orphans --apply` *does* read the index, but orphan rows
carry no heartbeat and no last-used timestamp, so the verdict is `LIVE` while the pod answers at
all — unchanged under a cfg with `idle_timeout: 5m` and `stall_window_s: 60` (T1-26). Between
them, an `--ephemeral` pod whose controller dies bills until a human runs
`kinoforge destroy --id eph-…`. Fix shape: give the daemon the orphan scan `reap` already has, and
give index rows the timestamps the classifier needs. Filed as **U9**.

**F12 — the sweeper's own bookkeeping row is displayed as a running instance and outlives the
daemon.** `sweeper start` writes `sweeper:<host>` with `provider=_sweeper` into the ledger;
`kinoforge list` renders it in `[instance overview]` exactly like a pod
(`sweeper:59be2fa1c7fc  age=0.1h  est<=$0.0000`) and it was still there after `sweeper stop` exits
0. That breaks the campaign's teardown-proof contract — `kinoforge list` cannot print
`No instances recorded in ledger.` while a sweeper has ever run — and it had to be cleared with
`kinoforge forget --id sweeper:59be2fa1c7fc` before Tier 1c's proof could be taken.
**RESOLVED in `7d535503`** — `_cmd_sweeper_stop` now `forget`s the row on the branch where it
confirms the daemon stopped. The cause was fixed rather than the symptom: the row is a liveness
signal that outlived its signaller, so it is deleted once, instead of being filtered at both
display sites (`_print_instance_overview` and `_cmd_list` — two guards) while a permanently stale
row stays visible to every other ledger reader. The timeout branch keeps the row on purpose: a
daemon that did not stop is still alive. Two red/green tests in `tests/cli/test_cmd_sweeper.py`.
**Still open, same command, and U10 stays open for it:** `sweeper status` and `sweeper metrics`
report `interval_s=60` from the cfg while the daemon is running at the `--interval-s 30` override,
so the two commands that exist to observe the daemon disagree with it.


**F13 — `kinoforge grid --ephemeral` is accepted and silently dropped.** The flag is declared on
the `grid` parser with the help text "pass-through to each underlying generate", but `_cmd_grid`
(`src/kinoforge/cli/_commands.py:3612`) never reads `args.ephemeral` and `run_grid` has no such
parameter — `ephemeral` appears nowhere in `src/kinoforge/core/grid/`. Confirmed live at T1-29:
the Modal apps came out as `kinoforge-grid_20260906-022232_a61d55b2__cell0` / `__cell1` instead of
the opaque `kinoforge-eph-<8hex>` name EM1's STRICT_POLICY mandates, publishing the run id, the
local timestamp and the cell index to the provider. The empty ledger afterwards is produced by the
per-cell `no_reuse=True` teardown, not by ephemeral mode, so there is no observable signal that the
flag did nothing. Fix shape: thread `ephemeral` from `args` through `run_grid` into
`_build_generate_cmd` as a `--ephemeral` argument on each cell's subprocess — or, if that is not
wanted, reject the flag rather than accept it. Filed as **U11**.

**F14 — FlashVSR's mp4 writer fails on every provider: `av` 18 broke it. DIAGNOSED; the fix is
one string in shared code and is NOT applied.** Every Tier 2a upscale that reached the GPU died at
`iio.imwrite(str(out), video, fps=fps, plugin="pyav", codec="libx264")`
(`src/kinoforge/upscalers/flashvsr/_runtime.py:416`), surfacing at the controller as
`kinoforge.core.errors.UpscaleFailed: upscale job <id> failed on server: Cannot change width after
codec is open.` Three for three (T2-01, T2-03, T2-05b), across two cfgs and both lifecycle routes.

A CPU-only Modal build of the same image (`tools/diagnose_flashvsr_writer_modal.py`, $0, no GPU)
captured the stack and reproduced the failure with **no GPU, no model and no FlashVSR**: plain
`(T,H,W,C)` uint8 zeros through the same `imwrite` call raise the identical error at 1920² and
480². Versions: **`imageio` 2.37.4, `av` 18.1.0**, `imageio-ffmpeg` 0.6.0, python 3.13.14. Holding
`imageio` fixed and moving only `av`: **18.1.0 FAILS; 17.1.0, 16.1.0, 15.1.0 and 13.1.0 all write
successfully.** So the leading hypothesis (an unpinned newer PyAV) is **confirmed**, the break is
exactly at `av` 18, and the pin is **`av<18`**.

The unpinned requirement is **not in any cfg**. It is the last entry of the runtime-deps line in
`FlashVSREngine.render_provision` (`src/kinoforge/upscalers/flashvsr/_engine.py:163`), which is
rendered into **every** FlashVSR provision script on every provider — the four golden launch
payloads that embed it cover Modal x4, Modal 1080p, SkyPilot/Lambda and SkyPilot/Vast, and RunPod
renders the same body. **RunPod and SkyPilot are on the same fuse and will fail identically the
next time their images are rebuilt**; they are green today only because they have not been. Not
applied here: it re-snapshots four golden payloads and moves the boot payload for three providers,
which is past the campaign's one-function fix bar. Filed as **U12**, which carries the full table.

**F15 — the CLI never exits after `UpscaleFailed`.** Once `submit_and_poll`
(`src/kinoforge/engines/_pod_http.py:147`) raises, the traceback prints and the process stays
alive indefinitely — T2-01 was still running 16 minutes later and had to be `kill -9`ed, and
T2-05b behaved the same. The pod is **not** leaked (T2-05b proved `--no-reuse` destroys it even
on the failure path, verified as 0 non-stopped `kinoforge-*` apps while the CLI was still hung),
so the hang is in the post-teardown unwind — a non-daemon thread (heartbeat or util poller) that
is never joined on the error path. Still serious: an operator who trusts the process to exit will
sit on a dead run, and in CI it is a job that hangs until the runner's own timeout. Filed as
**U13**.

**F16 — warm-attach cold-booted a second A100 despite an identical capability key on a live
pod.** At T2-03 the VSR1080 cfg resolved to capability key `7afe34198cc9` — the *same* key T2-01's
still-running, idle pod `upscale-20260906-023846` was registered under — and the matcher started a
fresh app `upscale-20260906-025335` anyway, putting two $2.50/hr A100s on the clock at once. This
is the mirror image of F1: there the matcher was too eager and crossed providers, here it is too
reluctant and misses its own. Note the two cfgs differ only in `upscale.scale` (`4x` vs `1080p`),
which the key evidently does not distinguish, so key equality was not the discriminator — whatever
rejected the candidate lives past the key comparison. Fix shape: log the reject reason at the
match site so a miss is diagnosable without a second $2.50/hr boot; this task could not tell
whether the pod was rejected, never enumerated, or never consulted. Filed as **U14**.

