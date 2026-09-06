# Modal command matrix

**Purpose.** Run every `kinoforge` subcommand and the important flag combinations against the
Modal provider, and record one verdict per cell. The operator has been hitting errors on
assorted commands without a record of which; this document is that record.

**Started:** 2026-09-05
**Budget:** $20 hard. Tier 3 (Wan 2.2 14B) is capped at $4 and never retried.
**Plan:** `docs/superpowers/plans/2026-09-05-modal-command-matrix.md`
**Spec:** `docs/superpowers/specs/2026-09-05-modal-command-matrix-design.md`
**Logs:** `/home/claudeuser/kinoforge-matrix/logs/<cell id>.log` (operator-side, not tracked)

**Spend so far: $0.38** (Tier 0 $0.00 + Tier 1a $0.38)

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
| T1-04 | `kinoforge logs --id ID` and `--file server.log --out <path>` | WAN13B | FAIL | $0.00 | `logs/T1-04a.log`, `logs/T1-04b.log` | Exit 1 on both forms, and **not** a clean 'unsupported on modal': it fetched `https://run-20260906-010255-8001.**proxy.runpod.net**/bootstrap.log` for a *Modal* pod and reported `HTTP 404 Not Found`. `_cmd_logs` (`src/kinoforge/cli/_commands.py:2510-2512`) does `del ctx  # ledger not consulted` and hard-codes the RunPod proxy template with no provider check, so on Modal it fabricates a hostname that never existed. A 404 reads as 'the pod has no such log' rather than 'this command is RunPod-only'. No `--out` file written. See follow-up F5 / **U4** |
| T1-05 | `kinoforge cost` / `--json` / `--no-cache` | WAN13B | PASS | $0.00 | `logs/T1-05a.log`, `logs/T1-05b.log`, `logs/T1-05c.log` | All three exit 0. Modal burn rate visible and correct: `Burn rate: $1.10/hr`, per-provider row `modal: $1.10/hr spend $0.21 balance N/A [LIVE=1]`. `--json` emits the stable schema incl. `heartbeat_partial_truth: [modal]`; `--no-cache` re-reads and agrees. `balance N/A` is expected — no Modal balance adapter (documented absent at T0-13) |
| T1-06 | `kinoforge pod lora ls ID` | WAN13B | FAIL | $0.00 | `logs/T1-06.log` | Exit 2, `pod lora ls: no endpoint URL for pod run-20260906-010255`. Not an empty inventory and not a clean 'unsupported' — the endpoint URL **is** in the ledger row. `provider.ensure_endpoints()` (the 'repairing door', `_commands.py:2413`) returns empty on Modal because `ModalProvider.endpoints` reads the per-process `_deployments` dict and falls back to `instance.endpoints`, which `get_instance()` builds from `modal app list` without any URL. Same root cause as T1-03; see follow-up F4 / **U3** |
| T1-07 | `kinoforge reap` and `reap --format json --id ID` | WAN13B | PASS | $0.00 | `logs/T1-07a.log`, `logs/T1-07b.log` | Both exit 0 and classify the pod LIVE without destroying it: `HEARTBEAT_SUBSTRATE_MISSING`, `1 entries classified — pass --apply to act on default policy`. `--format json --id` emits `{"type": "header", "entries": 1}` then the verdict object with `provider: modal` (the T0-12 fix holds on a non-empty ledger). Pod confirmed still alive afterwards. **Cosmetic defect:** the human table's verdict column is too narrow for the longest verdict, so it renders `HEARTBEAT_SUBSTRATE_MISSINGrun-20260906-010255` with no separator; see follow-up F6 |
| T1-08 | `kinoforge sweeper status` with no daemon | WAN13B | PASS | $0.00 | `logs/T1-08.log` | Exit 0 with `running=false`, `pid=none`, `sweeps_total=0`, `errors_total=0`. Requires `-c/--config` (as recorded at T0-15) |
| T1-09 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` (second process) | WAN13B | PASS | $0.02 | `logs/T1-09.log`, `sheetA.png` | Exit 0 in **40s** total. `warm-reuse: attached to run-20260906-010255` — no deploy line, so the matcher re-used the warm pod. **The matcher selected the Modal pod**, i.e. U1 did not bite (the index held only Modal rows after the operator's 2026-09-06 clear). Frame-QA **PASS**: coherent, prompt-adherent, stable |
| T1-10 | same + `--instance-id ID` | WAN13B | PASS | $0.02 | `logs/T1-10.log`, `sheetA.png` | Exit 0 in 50s, explicit attach, no deploy. Frame-QA **PASS with a soft flag**: content is coherent and prompt-adherent but noticeably hazier/more overexposed than its siblings, with a blurred face — seed variance at 1.3B/480px, not corruption |
| T1-11 | same + `--force-attach --instance-id ID` | WAN13B | PASS | $0.02 | `logs/T1-11.log`, `sheetA.png` | Exit 0 in 41s, attached bypassing the matcher, no deploy. Frame-QA **PASS**: the strongest clip of the set — clean subject, correct butterflies, stable camera push-in |
| T1-12 | `kinoforge --vault vault.yaml generate -c CFG --mode t2v` | WAN13B | FAIL | $0.01 | `logs/T1-12.log`, `logs/T1-12b.log` | Two-part failure. **(a)** Without `--prompt`: exit 2, `error: the following arguments are required: --prompt` — argparse still mandates it, so the vault can never *be* the prompt source. **(b)** Per the brief, retried with `--prompt ""`: exit 1 with an **uncaught traceback** (`ValueError: prompt yielded zero non-empty segments`) raised *after* `warm-reuse: attached`, i.e. it acquired and billed the pod before failing. Diagnosis: `vault.positive_prompt` is referenced in exactly one place in the tree — `register_vault_tokens` (`src/kinoforge/core/vault.py:228`), which only registers it as a *redaction token*. It is never wired into prompt resolution, so the `--vault` help text ('holding the positive prompt') describes a capability `generate` does not have. See follow-up F7 / **U5** |
| T1-13 | `kinoforge batch -c CFG --manifest batch.yaml --concurrent 1` then `--stream-format jsonl` | WAN13B | PASS | $0.05 | `logs/T1-13a.log`, `logs/T1-13b.log`, `sheetB.png` | Both runs exit 0. **One attach, no deploy** (`warm-reuse: attached to run-20260906-010255`), two artifacts each, `_batch_summary.json` written to the batch dir. Human format streams `[1/matrix-a] OK 36.2s <uri>` + a summary table; `--stream-format jsonl` emits well-formed `entry_start` / `entry_finish` / `batch_summary` records with per-entry `status`, `duration_s`, `uri`. Frame-QA **PASS** on all four clips; minor flag on `T1-13b/matrix-a`, whose first two frames are bloom-blown before converging |
| T1-14 | `kinoforge generate ... --run-id matrix-runid --output-dir <dir>` and `--no-output-dir` | WAN13B | PASS | $0.04 | `logs/T1-14a.log`, `logs/T1-14b.log`, `logs/T1-14c.log`, `sheetC.png` | All three exit 0 and placement honours every flag. `--run-id matrix-runid` -> `.kinoforge/matrix-runid/d1921fdc15369a32.mp4`. `--output-dir` -> `output published: /home/claudeuser/kinoforge-matrix/out/20260906-012353_diffusers_Wan2.1-T2V-1.3B-Diffuser_x.mp4` (both flags **do** exist on `generate`; the truncated usage line in T1-12's argparse error is not the full flag list). `--no-output-dir` -> no publish line, clip only in the store. **Two caveats, recorded not hidden:** the `--output-dir`/`--no-output-dir` probes used `--prompt "x"` rather than the standard prompt, so their clips are abstract colour fields — degenerate-by-construction, uninformative as a quality signal though free of corruption; and the `--run-id` artifact was consumed by the T1-18 `gc --run matrix-runid` probe before frames could be pulled. Quality evidence for this pod rests on the eight standard-prompt clips in `sheetA.png`/`sheetB.png` |
| T1-15 | `kinoforge stop --id ID` | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-15.log` | Exit 1, no traceback, exactly the by-design refusal: `modal cannot pause billing; instances are either running or destroyed.` followed by the actionable `To tear it down: kinoforge destroy --id run-20260906-010255`. Pod confirmed still alive after (T1-16 destroyed it) |
| T1-16 | `kinoforge destroy --id ID` then `kinoforge list` + Modal app list | WAN13B | PASS | $0.00 | `logs/T1-16.log`, `logs/T1-16-proof-list.log`, `logs/T1-16-proof-apps.log` | Exit 0, `destroyed: run-20260906-010255` at 01:25:14 (pod lifetime 01:04:22 -> 01:25:14 = **20m52s**). **Teardown proof from new processes:** `kinoforge list` prints both required lines (`[instance overview] No running instances.` AND `No instances recorded in ledger.`); `modal app list` shows the single `kinoforge-*` app in state **`stopped`** with **0 tasks**. Nothing left running |
| T1-17 | `kinoforge forget --id ID` after destroy | WAN13B | EXPECTED-REFUSAL | $0.00 | `logs/T1-17.log` | Exit 1, `instance 'run-20260906-010255' not found in ledger`, no traceback — correct, because `destroy` already removed the entry |
| T1-18 | `kinoforge gc --config CFG` | WAN13B | PASS | $0.00 | `logs/T1-18.log`, `logs/T1-18b.log` | Exit 0. Bare `gc --config` prints `gc: nothing to do (specify --run <id>)` even though the store now holds real artifacts — `--run` is required to act, matching T0-14 on the empty store. Exercised for real with `--run matrix-runid`: exit 0, `gc: removed 1 artifact(s)`, and the run directory is empty afterwards, so collection genuinely works |

**Tier 1a tally:** 12 PASS, 2 EXPECTED-REFUSAL, **4 FAIL** (T1-03, T1-04, T1-06, T1-12).
Actual spend **$0.38** — one A10 pod at $1.10/hr alive 01:04:22 -> 01:25:14 (20m52s) on
2026-09-06, carrying all 18 cells. Nothing was fixed in-session: each of the four failures is
larger than the campaign's one-function / one-config-key / one-guard bar, so all four are filed
below and, per the operator directive, as urgent items **U3 / U4 / U5** in `PROGRESS.md`.

Three of the four collapse into two root causes worth stating plainly: **the Modal pod's URL is
unreachable from any fresh process** (T1-03, T1-06 — the ledger has it, no read path consults it),
and **`logs` is RunPod-only by construction** (T1-04). The fourth (T1-12) is an advertised
`--vault` capability that was never wired up. Cold boot, warm attach (all three forms), batch,
artifact placement and teardown all worked exactly as designed.

### Tier 1a utilisation readings (the health signal, per `live-constraints.md`)

Polled at 30-75s throughout. `gpu_util_percent=100.0, cpu=5.9, mem=0.5` at 01:05:24, mid-generation
— the pod was genuinely computing, not merely accruing cost. Readings of `gpu=0.0` at 01:06:39
onward are the **idle warm pod between cells**, with no generation in flight, so they are not the
stall signature the rule targets. No cell ever showed GPU 0% while a generation was running, and
no capture/destroy/fail-fast escalation was needed.

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

Filed from Tier 0. Each is larger than the "one function / one config key / one guard" bar
the campaign's fix policy sets, so each is recorded rather than fixed. The two failures that
*did* meet that bar were fixed in-session instead of filed: the `doctor` heartbeat ERROR
(`c9d9b284`) and the `reap --format json` empty-ledger path (`3c7822b8`).

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
Minimum fix: branch on the ledger entry's `provider` and refuse cleanly. Filed as **U4**.

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
or correct the `--vault` help text, which today promises a vault "holding the positive prompt".
Independently, the empty-prompt path should fail before acquiring a pod, not after. Filed as **U5**.
