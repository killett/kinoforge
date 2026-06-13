# B4 — Cross-CLI warm-reuse CLI exposure — Design

**Status:** approved at brainstorm (2026-06-12).
**Prereqs:** Layer P Task 7 item #2 (warm-supplied `instance=` kwarg, CLOSED); B5a heartbeat substrate (CLOSED); B7 cooperative session-claim lock (CLOSED, commit `8f1ee89`).
**Unblocks:** none directly; composes with B2 (cost dashboard) and becomes the manual override for B3 (in-session orchestrator warm-reuse retrofit) once B3 lands.
**Cross-references:**
- `docs/superpowers/specs/2026-06-01-layer-p-task7-item2-warm-reuse-design.md` lines 54, 545–547 — the original "Layer Q candidate" deferral note this layer closes.
- `docs/superpowers/specs/2026-06-12-b7-cooperative-session-claim-lock-design.md` §1.1, §5 — B7 already wires the warm-supplied path through `hold_until_first_tick`; B4 consumes it unchanged.
- `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` §3.3 — Verdict tree consumed by B4's classify gate.
- `warm-reuse-tasks.txt:367–455` — authoritative B4 starter spec / risk surface.

---

## 1. Goal & scope

Surface the warm-supplied `instance=` kwarg path (Layer P) at the CLI. Operators can pass `kinoforge generate --instance-id <id>` or `kinoforge batch --instance-id <id>` to reuse a pre-existing pod they already provisioned (in an earlier shell, in a different `kinoforge generate` invocation, or via `kinoforge deploy`). The cold-start ComfyUI + Wan spin-up (1–5 min) is skipped on the second invocation.

### 1.1 Scope guardrails

- Manual escape hatch only. No automatic discovery, no in-orchestrator classify, no LifecycleManager.warm_reuse_or_create reuse from the CLI path (operator already chose the id).
- No orchestrator changes. The `instance=` and `tags=` kwargs already exist on `deploy_session`, `generate`, and `batch_generate` (Layer P).
- B7's `provision:<id>` cooperative lock is reused unchanged. B4 does NOT acquire any lock from the CLI.
- No new specs or substrate. No new ABCs. Pure CLI surface + classify gate + capability-key precheck + `kinoforge list` column.
- Live spend: $0. FakeProvider smoke covers the full CLI path; live RunPod warm-reuse is already exercised by the Layer P smoke (`tests/live/test_comfyui_wan_live.py`).

### 1.2 Non-goals

- Automatic warm-reuse (B3 / Layer Y) — B4 is the manual override that B3 builds on top of.
- Cost dashboard surfacing of `--instance-id`-attached vs. cold-created sessions (B2 / Layer X).
- `--instance-id` on `kinoforge deploy` (deploy is a cold-start command by definition).
- A `--dry-run` flag on `kinoforge generate` / `kinoforge batch` (D5).
- Verdict surfacing inside `kinoforge list` (D4 — list stays cheap and RPC-free; `kinoforge status --id <X>` carries the verdict).

---

## 2. Decisions locked at brainstorm

| # | Decision | Choice | Reason |
|---|---|---|---|
| D1 | `--instance-id` validation order | Cheap-first: ledger lookup → provider-kind check → capability_key check → classify (RPC). | Short-circuits on local checks before paying the `provider.list_instances()` RPC. STALE_LEDGER subsumes a redundant `status=='ready'` RPC because `classify` already returns it when the id is missing from `live_pod_ids`. |
| D2 | Exit-code shape | Layer S convention: `0` success / `1` ledger-absent / `2` precondition refused. | Mirrors `_cmd_status`'s "no such row" vs. "row says no" split. Operators get a clean two-bucket signal in scripts. |
| D3 | `--force-attach` scope | Bypasses `HEARTBEAT_UNKNOWN` + `IDLE_REAP` + `ORPHAN_REAP` only. Never bypasses `STALE_LEDGER`, `UNROUTABLE`, `OVERAGE_REAP`, or capability_key mismatch. | The three bypassable verdicts all mean "kinoforge isn't sure but the pod might be fine" — operator with `--force-attach` carries ground truth. STALE_LEDGER (pod gone), UNROUTABLE (provider unreachable), and OVERAGE_REAP (operator's own policy) are not salvageable by operator assertion. |
| D4 | `kinoforge list` column add | Append `capability_key=<12-char hash>` column. No new RPC. | Discovery path is `list → match cap_key → generate --instance-id`. Verdict stays in `kinoforge status --id <X>` (where the RPC cost is already paid). Keeps `list` as a fast pure-ledger read. |
| D5 | `--dry-run` × `--instance-id` interaction | No `--dry-run` added to generate / batch in B4. Mutex is moot. | `--dry-run` exists only on `_cmd_deploy` today; B4 introduces no flag overlap. A future layer that adds `--dry-run` to generate owns the mutex rule. |
| D6 | Cooperative-lock hold window | B7's `provision:<id>` is acquired ONLY inside `deploy_session.__enter__` (the existing single-acquire-site). B4 does NOT acquire any lock from the CLI. | B7 R2 ("fcntl per-fd flock inside same process") forbids a second acquire site. The ~8 ms CLI-classify → orchestrator-lock-acquire window is tolerable: a reaper that fires inside that gap causes engine.provision to fail loudly at HTTP-call time (Layer P Q4). |
| D7 | `kinoforge batch --instance-id` semantics | Single pod across all manifest rows. capability_key derived once from `cfg.capability_key()` and compared once to the ledger entry. Per-row prompt / mode / run-id vary normally. | Manifest schema today does not allow per-row cfg override; capability_key is cfg-derived, not per-row. Defensive per-row cap_key derivation pays for a feature that doesn't exist. |
| D8 | Error message wording | Layer-S-style cheap one-line errors with copy-paste recovery hints. No alternative-listing RPCs in the error path. | Mirrors `_cmd_status`. Operators get a runnable next-step (`kinoforge list`, `kinoforge destroy --id <X>`, `--force-attach`) without paying classify-per-entry on every failure path. |

---

## 3. Architecture

### 3.1 Module map

**Edits only. No new modules.**

| File | Change | LOC |
|---|---|---|
| `src/kinoforge/cli/_main.py` | Add `--instance-id` + `--force-attach` to `p_generate` and `p_batch` parsers. | ~12 |
| `src/kinoforge/cli/_commands.py` | New `_resolve_warm_instance` helper. Thread `instance=` into `_cmd_generate` + `_cmd_batch`. Add capability_key column to `_cmd_list`. Refuse-text per D8. | ~50 |
| `src/kinoforge/core/lifecycle.py` | None — `Ledger.read` was already shipped by B7 (`Ledger.read(instance_id) -> dict | None`). | 0 |
| `src/kinoforge/core/orchestrator.py` | None — `instance=` / `tags=` kwargs already plumbed (Layer P, Phase 28); `hold_until_first_tick` already wraps the warm-supplied path (B7 §1.1). | 0 |

**Test additions:**

| File | Cases | LOC |
|---|---|---|
| `tests/cli/test_resolve_warm_instance.py` (new) | ~15 unit cases per verdict / refusal path | ~110 |
| `tests/cli/test_cmd_generate.py` (delta) | ~4 dispatch cases | ~50 |
| `tests/cli/test_cmd_batch.py` (delta) | ~3 dispatch cases | ~40 |
| `tests/cli/test_cmd_list.py` (delta) | ~2 cap_key column cases | ~20 |
| `tests/live/test_warm_attach_dry_run.py` (new) | 1 FakeProvider end-to-end smoke | ~30 |

**Docs:**

| File | Change | LOC |
|---|---|---|
| `README.md` | New "Operator warm-reuse" section under Operator Guide. | ~20 |
| `PROGRESS.md` | Strike B4, point at this spec. | ~5 |
| `warm-reuse-tasks.txt` | Replace B4 starter entry with closeout pointer. | ~5 |

**Totals:** ~62 source LOC across 2 modified files, 0 new modules; ~250 LOC tests across 3 modified + 2 new test files.

### 3.2 CLI flag surface

`src/kinoforge/cli/_main.py` — argparse extensions:

```python
# inside _build_parser, after p_generate.add_argument("--run-id", ...):
p_generate.add_argument(
    "--instance-id",
    default=None,
    metavar="ID",
    help=(
        "reuse an existing pod from the local ledger instead of cold-creating "
        "(skip ComfyUI + Wan spin-up). Use `kinoforge list` to find candidate ids."
    ),
)
p_generate.add_argument(
    "--force-attach",
    action="store_true",
    help=(
        "override classify verdicts HEARTBEAT_UNKNOWN, IDLE_REAP, ORPHAN_REAP "
        "for the supplied --instance-id. Has no effect without --instance-id. "
        "Never bypasses STALE_LEDGER, OVERAGE_REAP, UNROUTABLE, or "
        "capability_key mismatch."
    ),
)

# Same two flags added to p_batch.
```

No mutex with `--dry-run` (D5: flag does not exist on these parsers).

### 3.3 `_resolve_warm_instance` helper

`src/kinoforge/cli/_commands.py` — new module-level helper, mirrors the `_build_store` / `_build_sink` / `_classify_for_status` precedent.

```python
def _resolve_warm_instance(
    ctx: SessionContext,
    cfg: Config,
    instance_id: str,
    *,
    force_attach: bool,
    clock: Clock | None = None,
) -> tuple[Instance | None, int | None]:
    """Validate operator-supplied --instance-id; return Instance or exit code.

    Order (D1, short-circuit on first failure):

      1. Ledger lookup: ``ctx.ledger().read(instance_id)``.
         None -> (None, 1) with stderr
         "instance not found in ledger: <id>. Run 'kinoforge list' to see
         available ids."

      2. Provider-kind: entry["provider"] != cfg.compute.provider ->
         (None, 2) with stderr
         "provider mismatch: cfg=<cfg_provider>, ledger says
         provider=<entry_provider> for <id>. Use a cfg matching the pod's
         provider."

      3. capability_key: cfg.capability_key().derive()[:12] !=
         entry.get("tags", {}).get("kinoforge_key") -> (None, 2) with stderr
         "capability_key mismatch: cfg=<cfg_hash>, ledger entry <id>=<entry_hash>.
         Either use a cfg matching this pod or 'kinoforge destroy --id <id>'
         first."
         When entry lacks tags["kinoforge_key"] (legacy row), treat as mismatch
         under the same message with "<unknown>" in place of the entry hash.
         (Source: orchestrator.py:492, 1015 stamps the tag on every cold-path
         provision; _cmd_deploy's dup-detect at _commands.py:186 reads the
         same path.)

      4. Construct provider via ``registry.get_provider(entry["provider"])()``.
         UnknownAdapter / construction-time exception -> (None, 2) with stderr
         "provider <name> unconstructable: <ExcName>: <msg>. Check provider
         credentials."

      5. classify(entry, live_ids, now, ...) via the same code path
         _cmd_status uses (_classify_for_status helper). live_ids is the set
         of ids returned by provider.list_instances(); list_instances exception
         -> (None, 2) with stderr "provider <name> list_instances failed:
         <ExcName>: <msg>."

         Verdict gate (D3):
           - LIVE: pass.
           - HEARTBEAT_UNKNOWN / IDLE_REAP / ORPHAN_REAP: pass IFF
             force_attach; else (None, 2) with stderr
             "classify verdict <V> blocks attach for <id>: <one-line reason>.
             Pass --force-attach to override, or 'kinoforge reap --apply' to
             clean up."
           - STALE_LEDGER: (None, 2) regardless of force_attach,
             "instance <id> is stale: provider no longer has this pod. Run
             'kinoforge forget --id <id>' and provision a fresh one."
           - OVERAGE_REAP: (None, 2) regardless of force_attach,
             "instance <id> exceeded max_lifetime_s (cfg policy). Destroy it
             with 'kinoforge destroy --id <id>' before reusing the slot."
           - UNROUTABLE: (None, 2) regardless of force_attach (matches the
             UnknownAdapter step above; safety net).

      6. provider.get_instance(instance_id) -> Instance. KeyError -> (None, 2)
         "instance <id> disappeared between classify and lookup; a concurrent
         reaper may have destroyed it. Re-run after `kinoforge list`."

      Return (Instance, None) on success.

    The per-verdict "one-line reason" text reads ledger fields directly:
        - IDLE_REAP -> f"hb_age={now - entry['last_heartbeat']:.0f}s > "
                       f"idle_timeout={idle_timeout_s:.0f}s"
        - ORPHAN_REAP -> f"sentinel_age={now - entry['heartbeat_thread_tick']:.0f}s "
                          f"past grace_after_session_s={grace_s:.0f}s"
        - HEARTBEAT_UNKNOWN -> "no sentinel data in ledger entry"
    """
```

Wiring at the two call sites:

```python
# _cmd_generate (after cfg / store / sink resolution, before _generate(...)):
instance: Instance | None = None
if getattr(args, "instance_id", None) is not None:
    instance, rc = _resolve_warm_instance(
        ctx, cfg, args.instance_id, force_attach=bool(args.force_attach),
    )
    if rc is not None:
        return rc
elif getattr(args, "force_attach", False):
    print(
        "error: --force-attach has no effect without --instance-id",
        file=sys.stderr,
    )
    return 2

artifact, _ = _generate(
    cfg, request, store=store, sink=sink, run_id=run_id,
    state_dir=ctx.state_dir, cancel_token=ctx.cancel_token,
    instance=instance,   # NEW
)
```

`_cmd_batch` is symmetric: same `if/elif` block, then `batch_generate(..., instance=instance)`.

### 3.4 `kinoforge list` column

`_cmd_list` (`_commands.py:459`) — print-format change only:

```python
for entry in entries:
    cap_key = str(entry.get("tags", {}).get("kinoforge_key", "<unknown>"))
    print(
        f"  {entry.get('id', '?')}  "
        f"provider={entry.get('provider', '?')}  "
        f"capability_key={cap_key}"
    )
```

The `capability_key=` label remains the user-facing column name (it's what the operator
matches against `cfg.capability_key()`); the ledger field path is the internal
`tags.kinoforge_key` from the orchestrator's Phase 18 tagging contract.

No new RPC. Pure ledger read.

### 3.5 Lock interaction (no change)

B4 does NOT acquire `provision:<id>`. The race window is the CLI's `_resolve_warm_instance` return → orchestrator's `deploy_session.__enter__` lock-acquire, on the order of milliseconds in one process. A reaper firing inside that window causes `engine.provision` to fail loudly at HTTP-call time per Layer P Q4 (downstream backend HTTP failure surfaces dead pods loudly; no preflight probe). Reaper-side B7 probe still defers correctly for any session that reaches `deploy_session.__enter__`.

B7's `hold_until_first_tick` wire-in at orchestrator §1.1 covers the warm-supplied path ("caller's instance → hold lock → idempotent engine.provision → HeartbeatLoop.start → first tick → release") unchanged. B4 inherits the lock semantics for free.

---

## 4. Failure modes

| # | Mode | Handling |
|---|---|---|
| F1 | Operator types unknown id | `_resolve_warm_instance` returns `(None, 1)` at step 1. Stderr suggests `kinoforge list`. |
| F2 | Operator's cfg targets wrong provider | `(None, 2)` at step 2 with explicit cfg-vs-ledger diff. |
| F3 | Operator's cfg has wrong capability_key | `(None, 2)` at step 3 with cfg-hash vs. ledger-hash diff. Suggests destroy + reprovision. |
| F4 | Provider unconstructable (UnknownAdapter, missing creds) | `(None, 2)` at step 4. Stderr names the provider and the exception. |
| F5 | Provider's `list_instances()` raises | `(None, 2)` at step 5. Stderr names the provider and the exception. |
| F6 | classify returns non-LIVE without `--force-attach` for a bypassable verdict | `(None, 2)` at step 5 verdict-gate. Stderr names the verdict + the one-line reason + the `--force-attach` next step. |
| F7 | classify returns a never-bypassable verdict (STALE_LEDGER / OVERAGE_REAP / UNROUTABLE) | `(None, 2)` at step 5. Stderr explains why `--force-attach` does not help and gives the recovery command. |
| F8 | Pod destroyed between classify and `provider.get_instance` | `(None, 2)` at step 6. Stderr suggests re-running after `kinoforge list`. |
| F9 | Pod destroyed AFTER `_resolve_warm_instance` returns success but BEFORE deploy_session lock-acquire (~8 ms window) | Not handled at CLI. orchestrator's `engine.provision` against the dead pod fails at HTTP-call time per Layer P Q4. Loud, not silent. |
| F10 | `--force-attach` without `--instance-id` | Exit 2 at the wiring `elif` ("--force-attach has no effect without --instance-id"). |
| F11 | Batch manifest row count = 0 | Existing batch validation handles it before warm-attach matters. |
| F12 | Legacy ledger entry missing `tags.kinoforge_key` | Treated as mismatch (step 3) with `<unknown>` shown in the diff. Operator destroys the legacy entry and reprovisions; or passes `--force-attach` — which does NOT bypass cap_key mismatch (D3). Hard refusal. |

---

## 5. Test plan

### 5.1 Helper unit — `tests/cli/test_resolve_warm_instance.py` (new)

```python
def test_returns_instance_on_happy_path():           # ledger ok, cap_key ok, LIVE
def test_returns_1_when_id_not_in_ledger():
def test_returns_2_on_provider_kind_mismatch():
def test_returns_2_on_capability_key_mismatch():
def test_returns_2_when_entry_missing_capability_key_field():  # legacy row
def test_returns_2_on_provider_construction_failure():
def test_returns_2_on_list_instances_failure():
def test_returns_2_on_STALE_LEDGER_even_with_force():
def test_returns_2_on_IDLE_REAP_without_force():
def test_passes_on_IDLE_REAP_with_force_attach():
def test_returns_2_on_ORPHAN_REAP_without_force():
def test_passes_on_ORPHAN_REAP_with_force_attach():
def test_returns_2_on_HEARTBEAT_UNKNOWN_without_force():
def test_passes_on_HEARTBEAT_UNKNOWN_with_force_attach():
def test_returns_2_on_OVERAGE_REAP_even_with_force():
def test_returns_2_when_get_instance_raises_keyerror():
def test_short_circuits_on_first_failure():          # cap_key fail -> no classify RPC
```

Each test seeds a FakeProvider via `ctx`, populates the ledger with the relevant entry, drives `_resolve_warm_instance` directly, asserts exit-code + Instance shape + stderr substring.

### 5.2 CLI dispatch — `tests/cli/test_cmd_generate.py` (delta)

```python
def test_generate_warm_attach_passes_instance_kwarg():
    # spy on cli.generate; assert call kwargs include instance=Instance(id=...).
def test_generate_refuses_unknown_instance_id_exit_1():
def test_generate_force_attach_without_instance_id_exit_2():
def test_generate_force_attach_passes_through_when_id_present():
    # IDLE_REAP entry + --force-attach -> generate spy receives instance kwarg.
```

### 5.3 CLI dispatch — `tests/cli/test_cmd_batch.py` (delta)

```python
def test_batch_warm_attach_single_pod_for_all_rows():
    # spy on batch_generate; assert single Instance object reused across rows.
def test_batch_refuses_capability_key_mismatch_exit_2():
def test_batch_per_row_prompt_mode_vary_under_one_instance():
    # manifest with 3 rows of differing prompt/mode all attach to one instance.
```

### 5.4 List column — `tests/cli/test_cmd_list.py` (delta)

```python
def test_list_includes_capability_key_column():
def test_list_prints_unknown_for_legacy_entry_missing_cap_key():
```

### 5.5 Dry-run smoke — `tests/live/test_warm_attach_dry_run.py` (new)

```python
def test_full_cli_warm_attach_smoke():
    # Seed FakeProvider with a pod + matching ledger entry.
    # subprocess: kinoforge generate -c fake.yaml --prompt P --mode t2v
    #             --instance-id <pod_id>
    # Assert: exit 0, stdout contains "generated: uri=", FakeProvider's
    #         create_instance was NEVER called (warm path),
    #         FakeProvider's get_instance was called once with the supplied id.
```

### 5.6 Live spend

None. Live RunPod warm-reuse is already exercised by the Layer P live smoke (`tests/live/test_comfyui_wan_live.py`) at the Python kwarg level; the CLI gate is purely a thin wrapper.

---

## 6. Acceptance criteria

- **AC1.** `kinoforge generate --instance-id <id>` and `kinoforge batch --instance-id <id>` thread the resolved `Instance` into `generate()` / `batch_generate()` via the existing `instance=` kwarg.
- **AC2.** `_resolve_warm_instance` enforces the D1 order: ledger → provider-kind → capability_key → classify → `get_instance`. Each failing step short-circuits before the next.
- **AC3.** Exit codes follow D2: `0` success, `1` ledger-absent, `2` precondition refused. No other codes returned by the helper.
- **AC4.** `--force-attach` bypasses HEARTBEAT_UNKNOWN, IDLE_REAP, and ORPHAN_REAP only. STALE_LEDGER, OVERAGE_REAP, UNROUTABLE, and capability_key mismatch refuse regardless of `--force-attach`.
- **AC5.** `--force-attach` without `--instance-id` exits 2 with a one-line stderr message.
- **AC6.** `kinoforge list` output appends `capability_key=<12-char hash>` per row (sourced from `entry["tags"]["kinoforge_key"]`). Legacy entries without `tags.kinoforge_key` render `capability_key=<unknown>`.
- **AC7.** No CLI-side lock acquire. B7's `hold_until_first_tick` remains the sole acquire site for `provision:<id>`.
- **AC8.** No orchestrator changes. `core/orchestrator.py` and `core/lifecycle.py` diff is empty.
- **AC9.** `kinoforge batch --instance-id <id>` reuses one pod across every manifest row. capability_key is derived once from cfg and validated once against the ledger entry.
- **AC10.** Error messages match the Layer-S-style cheap format from D8. No additional `list_instances` / classify RPCs in any error path beyond the single classify call gate.
- **AC11.** FakeProvider end-to-end smoke produces an mp4 artifact via the warm-attach path; `provider.create_instance` is never called.
- **AC12.** README "Operator warm-reuse" section documents the `list → match cap_key → generate --instance-id` discovery loop and the `--force-attach` bypassable-verdict matrix.

---

## 7. Risks

- **R1. Dead pod between CLI classify and orchestrator lock-acquire (~8 ms window).** A concurrent reaper that fires inside the gap causes `engine.provision` to fail loudly at HTTP-call time. Loud, not silent. Brief flags this as the dominant residual risk; mitigation is "loud failure" not "preemptive probe." (D6.)
- **R2. Cap_key derivation drift.** If `Config.capability_key().derive()[:12]` semantics change in a future refactor, B4's precheck silently diverges from `_cmd_deploy`'s persist. Mitigation: `tests/cli/test_resolve_warm_instance.py` asserts the prefix length and ledger schema field name verbatim; `_cmd_deploy` shares the same `[:12]` truncation at `_commands.py:211`.
- **R3. Verdict tree churn.** Layer V's Verdict enum is the contract `_resolve_warm_instance` gates on. If future layers add a verdict (e.g., `RECOVERABLE_BUSY`), B4's gate needs a default-deny branch. Mitigation: classify's return is an enum; an unmatched verdict in the gate raises (caught test) rather than silently passing.
- **R4. Operator confusion between `--instance-id` and `--id`.** Layer S already uses `--id` on `_cmd_status` / `_cmd_stop` / `_cmd_destroy` / `_cmd_forget`. We add `--instance-id` (longer name, no short flag) specifically to avoid clobbering the convention on `generate`/`batch` (where `--id` would clash with `--run-id`). Mitigation: README documents the distinction.
- **R5. Legacy ledger row missing `capability_key`.** B4 treats it as mismatch (F12), which is hard-refuse even with `--force-attach`. Mitigation: doc the migration path: `kinoforge destroy --id <id>` + reprovision under the current Layer S persist.

---

## 8. Out of scope (recorded follow-ups)

- B3 (Layer Y) — automatic warm-reuse: orchestrator consults `classify` against the ledger when `_states[id]` is empty and attaches to a LIVE matching-capability_key pod. `_resolve_warm_instance` shape is intentionally re-callable by B3 with the operator-supplied id replaced by an `instance_id_from_classify_scan(...)` discovery branch.
- B2 (Layer X) — cost dashboard separating cold-created vs. warm-attached sessions.
- `--instance-id` on `kinoforge deploy` — deploy is for cold-start by definition.
- `kinoforge list --verbose` flag adding verdict / age columns (D4 explicitly deferred).
- `--dry-run` parity on `kinoforge generate` / `kinoforge batch` (D5).
- Per-row capability_key derivation in batch (D7 — manifest schema does not allow per-row cfg override today).

---

## 9. Effort estimate

- ~12 LOC `src/kinoforge/cli/_main.py` (argparse flag adds).
- ~50 LOC `src/kinoforge/cli/_commands.py` (helper + wiring + list column).
- ~250 LOC tests across `tests/cli/test_resolve_warm_instance.py` (new, ~15 cases), `tests/cli/test_cmd_generate.py` delta, `tests/cli/test_cmd_batch.py` delta, `tests/cli/test_cmd_list.py` delta, `tests/live/test_warm_attach_dry_run.py` (new, 1 case).
- ~20 LOC README "Operator warm-reuse" section.

Live spend: **$0**. FakeProvider smoke covers the full CLI path.

---

## 10. Task split (for /superpowers-extended-cc:write-plan)

1. **Task a — `_resolve_warm_instance` helper + offline unit tests.** RED-first: write `tests/cli/test_resolve_warm_instance.py` with the ~15 cases from §5.1; implement helper in `_commands.py`; GREEN. One atomic commit.
2. **Task b — `_cmd_generate` wiring + dispatch tests.** RED-first: extend `tests/cli/test_cmd_generate.py` with the 4 cases from §5.2 (spy-on-generate pattern); add `--instance-id` + `--force-attach` to `p_generate` in `_main.py`; thread `instance=` into `_cmd_generate`; GREEN.
3. **Task c — `_cmd_batch` wiring + dispatch tests.** RED-first: extend `tests/cli/test_cmd_batch.py` with the 3 cases from §5.3 (spy-on-batch_generate pattern); add `--instance-id` + `--force-attach` to `p_batch` in `_main.py`; thread `instance=` into `_cmd_batch`; GREEN.
4. **Task d — `_cmd_list` capability_key column + tests.** RED-first: extend `tests/cli/test_cmd_list.py` with the 2 cases from §5.4; modify `_cmd_list` print f-string; GREEN.
5. **Task e — FakeProvider end-to-end smoke.** RED-first: write `tests/live/test_warm_attach_dry_run.py` from §5.5; runs after a–d land so the integration is real; assert mp4 + `create_instance` never-called.
6. **Task f — README + PROGRESS + warm-reuse-tasks closeout.** Add "Operator warm-reuse" section to README documenting the discovery loop + force-attach matrix. Strike B4 in `PROGRESS.md`. Replace `warm-reuse-tasks.txt:367–455` with a one-line closeout pointer.

---

## 11. Forward-compat hooks for downstream layers

- **B3 (in-session orchestrator warm-reuse retrofit).** B3 introduces `instance_id_from_classify_scan(ledger, cfg, ...)` that walks the ledger, picks the first LIVE matching-capability_key entry, and returns it as if the operator typed `--instance-id`. B3 wraps `_resolve_warm_instance` with this discovery branch — same helper, same gate, same refusal semantics. `_resolve_warm_instance`'s signature stays stable.
- **B2 (cost dashboard).** Dashboard reads the ledger entries directly. `--instance-id`-attached sessions produce no new ledger writes (Layer P Q4 — caller-supplied instance not re-recorded). The session's deploy_session does `Ledger.touch` per heartbeat tick, which the dashboard's per-entry classify already consumes. No B4 change.
- **B1 (sweeper daemon).** Sweeper inherits B7's `provision:<id>` non-blocking probe; B4 sessions running through `hold_until_first_tick` are deferred per B7 §3.4. No B4 change.

---

## 12. Sanity-checks against repo (verified 2026-06-12)

- `src/kinoforge/cli/_main.py:304–320` — `p_generate` parser block: confirmed no `--instance-id` / `--force-attach` / `--dry-run`.
- `src/kinoforge/cli/_main.py:400–423` — `p_batch` parser block: confirmed no `--instance-id` / `--force-attach` / `--dry-run`.
- `src/kinoforge/cli/_commands.py:278` — `_cmd_generate` signature confirmed; calls `_generate(cfg, request, ..., cancel_token=ctx.cancel_token)` without `instance=`.
- `src/kinoforge/cli/_commands.py:329` — `_cmd_batch` signature confirmed; calls `batch_generate(cfg, manifest, ...)` without `instance=`.
- `src/kinoforge/cli/_commands.py:459` — `_cmd_list` confirmed printing `id` + `provider` only.
- `src/kinoforge/cli/_commands.py:582` — `_classify_for_status` already wraps the Layer V `classify` call with the right thresholds; safe to reuse.
- `src/kinoforge/core/orchestrator.py:606,1070,1084` — `instance: Instance | None = None` kwargs on `deploy_session`, `generate`, `batch_generate` confirmed.
- `src/kinoforge/core/lifecycle.py:308–345` — `warm_reuse_or_create` confirmed pure helper; B4 does NOT consume it (CLI path is "operator already chose").
- B7 spec §1.1 — warm-supplied path through `hold_until_first_tick` confirmed: "caller's instance → hold lock → idempotent engine.provision → HeartbeatLoop.start → first tick → release."
- Layer V spec §3.3 — Verdict tree (LIVE / IDLE_REAP / ORPHAN_REAP / OVERAGE_REAP / STALE_LEDGER / HEARTBEAT_UNKNOWN / UNROUTABLE) confirmed.
- `src/kinoforge/core/orchestrator.py:492,1015` — cold-path provision stamps `tags["kinoforge_key"] = cfg.capability_key().derive()[:12]` on the Instance before `ledger.record`; `_cmd_deploy` at `_commands.py:186` reads the same path for dup-detection. §3.3 step 3 matches this contract verbatim.
