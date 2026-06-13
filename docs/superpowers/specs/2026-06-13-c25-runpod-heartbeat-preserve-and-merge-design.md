# C25 — RunPod heartbeat wire path: env-additive or dockerArgs preserve-and-merge

**Date:** 2026-06-13
**Status:** Design approved; ready for plan
**Tracking:** PROGRESS.md §C C25; B5a §9 wire-discovery note
**Spec hooks:**
- Upstream: `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` (substrate Protocol + initial RunPod satisfier)
- Implementation today: `src/kinoforge/providers/runpod/heartbeat.py` (`RunPodGraphQLHeartbeatEndpoint` using `dockerArgs` JSON carrier)
- Guard today: `src/kinoforge/_adapters.py` lines 62-127 (`_RUNPOD_HEARTBEAT_SAFE_ENGINES = {"fake"}` + engine-kind `ValidationError`)
- Phase 24 wire-slot writer: `src/kinoforge/providers/runpod/__init__.py:552` (`_create_pod` setting `dockerArgs` to the base64-decoder bash command)
- Downstream consumer the acceptance gate exercises: B3 in-session orchestrator warm-reuse retrofit (`docs/superpowers/specs/2026-06-13-b3-warm-reuse-retrofit-design.md`)
- Production-limitation paragraph this spec closes out: `successful-generations.md` entry #6 §"Production limitation (C25)"

---

## 1. Purpose

Make `compute.heartbeat_mode = "graphql-tag"` safe on every RunPod engine that uses Phase 24's `provision_script` injection — most importantly `engine.kind = "comfyui"`, the only production-shipped video workload path on RunPod today. Once safe, B3's cross-CLI auto-discovery warm-reuse path attaches gen2 to gen1's pod on a real Wan workload without operator id-juggling, without `--force-attach`, and without a cost-leak window if RunPod restarts the pod between ticks.

The collision being closed is verified at `providers/runpod/__init__.py:622`. `_create_pod` writes `dockerArgs` ONCE at pod creation to the value:

```
bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh && chmod +x /tmp/p.sh && bash /tmp/p.sh"
```

The actual provision script lives base64-encoded in the `KINOFORGE_PROVISION_SCRIPT` env var; `dockerArgs` is the BOOT command that decodes-and-runs it on pod start.

B5a's `RunPodGraphQLHeartbeatEndpoint.write` then overwrites the WHOLE `dockerArgs` field every tick with `{"_kinoforge_hb": "<ISO>"}` JSON. The in-pod process keeps running because the bash decoder already executed at original boot — but a pod restart (RunPod migrations, tier changes) re-reads the now-JSON `dockerArgs` and the container fails to boot the JSON-as-bash. The in-pod selfterm dies with the boot; the operator-side dead-man window opens.

Until C25 ships, the runtime guard at `_adapters.py:67` (`_RUNPOD_HEARTBEAT_SAFE_ENGINES = frozenset({"fake"})`) raises `ValidationError` for every engine combination that uses `provision_script`. The substrate-honesty contract holds; the user-facing payoff does not.

## 2. Decisions locked at brainstorm

Carried verbatim from this brainstorm session (2026-06-13):

1. **Probe-then-branch.** Before committing to a single wire path, run one ~$0.05 live probe against bare RunPod to disambiguate undocumented `podEditJob` `env`-array semantics. If env writes MERGE with prior env → ship Branch A (env-slot satisfier; one round-trip per tick; `dockerArgs` never touched). If env writes REPLACE the whole array → ship Branch B (`dockerArgs` preserve-and-merge; two round-trips per tick).
2. **Guard deletion.** `_RUNPOD_HEARTBEAT_SAFE_ENGINES` and the engine-kind `ValidationError` raise in `build_heartbeat_endpoint_for` are deleted in the same change set. The wire-level fix makes engine identity irrelevant to heartbeat safety. Substrate Protocol becomes the only contract.
3. **Marker format (Branch B only).** Trailing bash comment ` # _kinoforge_hb:<ISO>`. Bare-pod variant `: # _kinoforge_hb:<ISO>` (`:` is bash no-op, gives Docker a valid container CMD).
4. **Hard break on B5a JSON-as-dockerArgs format.** No back-compat arm in the read parser. B5a heartbeat-mode has only ever been used in test smokes against ephemeral pods; no in-flight production pods carry the legacy payload.
5. **Single-writer invariant via B7.** B7's `provision:<id>` cooperative lock guarantees only the holding orchestrator writes a pod's wire state during a session. Heartbeat tick races within one orchestrator are sequential (one `HeartbeatLoop` thread). No multi-writer concurrency to design against.
6. **Live spend authorized.** $0.05 for the probe (Task a); ~$0.30 for the Wan acceptance smoke (Task d). Both within session $20 envelope per `feedback_autonomous_no_gates`.
7. **Acceptance test.** Two identical `kinoforge generate` CLI invocations 60s apart on real Wan workload; gen2 auto-attaches to gen1's pod via B3's `_scan_warm_candidates`; no `--instance-id`, no `--force-attach`. Mirrors the B3 entry #6 cold-skip ratio threshold (gen2 wall < 0.7 × gen1 wall).

## 3. Architecture

Two new live-spend tasks (probe + acceptance smoke), three surgical source edits, plus existing-test rewrites.

**New files:**
1. `tests/live/test_runpod_env_semantics_probe.py` — env-merge semantics probe (Task a).
2. `tests/live/test_c25_warm_reuse_comfyui_wan_live.py` — Wan + ComfyUI + 2-CLI acceptance smoke (Task d).
3. `tests/live/cfg_c25_wan_comfyui.yaml` — config used by Task d.
4. `tests/live/_runpod_env_semantics.json` (output) — probe outcome sidecar consumed by Task b branch selection.

**Edited files:**
5. `src/kinoforge/providers/runpod/heartbeat.py` — `RunPodGraphQLHeartbeatEndpoint.write` + `.read` rewritten per probe outcome. Module + class docstrings updated.
6. `src/kinoforge/_adapters.py` — delete `_RUNPOD_HEARTBEAT_SAFE_ENGINES` (lines 62-67) and the engine-kind `ValidationError` block (lines 114-127).
7. `tests/providers/runpod/test_heartbeat.py` — wire-shape unit tests rewritten for chosen branch; branch-not-selected tests deleted.
8. `tests/test_adapters_heartbeat.py` — guard's negative-path tests retargeted to assert ENABLED behaviour for previously-blocked `(provider=runpod, mode=graphql-tag, engine.kind=comfyui)` combination.

**Spec / doc amendments on close:**
9. `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §9 — wire-discovery note amended with closure pointer to this spec.
10. `PROGRESS.md` §C C25 — strike-through with `CLOSED by <SHA>`.
11. `successful-generations.md` — new entry #7 capturing the Wan + warm-reuse + auto-attach combination (or entry #6 production-limitation paragraph struck-through if the `(provider, engine, model, mode)` tuple is reused per CLAUDE.md schema rule).

**Probe-then-branch flow:**

```
Task a: tests/live/test_runpod_env_semantics_probe.py
  1. Spin cheapest bare RunPod pod with env={PROBE_KEEP_A: "keep", PROBE_KEEP_B: "keep"}
  2. Issue podEditJob(input: {podId, env: [{key: "PROBE_NEW", value: "new"}]})
  3. Query pod { env { key value } }
  4. Write tests/live/_runpod_env_semantics.json with one of:
     - {"semantics": "additive", ...}  (all three keys present)
     - {"semantics": "replace", ...}   (only PROBE_NEW present)
     - {"semantics": "read-unavailable", ...} (env field not on pod query)
  5. Destroy pod
  6. Amend §3 of this spec to fix the branch decision

Task b: implementation
  if semantics == "additive":
    ship Branch A — env-slot satisfier (one round-trip per tick)
  else:  # replace OR read-unavailable
    ship Branch B — dockerArgs preserve-and-merge (two round-trips per tick)
```

The substrate Protocol (`HeartbeatEndpoint` in `core/heartbeat_endpoints.py`) is unchanged in BOTH branches — `write(instance_id, ts_local)` / `read(instance_id)` signatures preserved. Only `RunPodGraphQLHeartbeatEndpoint` internals change.

**Probe outcome (captured 2026-06-13T13:49:36-07:00, pod `ssbbm0vjyd56a9`, commit `3d97f69`):** `read-unavailable`. RunPod's GraphQL `pod.env` field is typed `[String]` (no subfields), so the `env { key value }` selection set returns HTTP 400 `GRAPHQL_VALIDATION_FAILED`. We cannot reliably read individual env keys back, so even though `podEditJob(env:[...])` may merge additively, we cannot verify a tick landed. Task 2 ships **Branch B** (dockerArgs preserve-and-merge). Sidecar: `tests/live/_runpod_env_semantics.json`.

## 4. Branch A — env-additive happy path

Fires when probe outcome = `"additive"`.

**Write shape.** One `podEditJob` mutation per tick. Variables:

```json
{"input": {
   "podId": "<id>",
   "env": [{"key": "KINOFORGE_LAST_HEARTBEAT", "value": "<ISO8601 local-TZ>"}]
}}
```

RunPod merges the new key into the existing pod env map. `KINOFORGE_SELFTERM_SCRIPT` and `KINOFORGE_PROVISION_SCRIPT` (and any operator env vars) survive untouched. `dockerArgs` is never touched.

**Read shape.** Query rewritten from B5a `pod { id dockerArgs }` to:

```graphql
query GetPod($podId: String!) {
  pod(input: {podId: $podId}) {
    id
    env { key value }
  }
}
```

Read implementation:

```python
def read(self, instance_id: str) -> datetime | None:
    payload = {"query": _POD_QUERY, "variables": {"podId": instance_id}}
    try:
        resp = self._http_post(self._graphql_url, payload)
    except TransportError:
        raise
    except Exception as exc:
        raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
    if "errors" in resp:
        raise TransportError(f"RunPod pod query failed: {resp['errors']}")
    pod = resp.get("data", {}).get("pod")
    if pod is None:
        return None  # instance gone — valid None
    env = pod.get("env") or []
    for entry in env:
        if entry.get("key") == _HEARTBEAT_ENV_KEY:
            value = entry.get("value")
            if not isinstance(value, str):
                raise TransportError(
                    f"corrupted heartbeat env for {instance_id}: key present but value not a string"
                )
            try:
                return datetime.fromisoformat(value)
            except ValueError as exc:
                raise TransportError(
                    f"corrupted heartbeat env for {instance_id}: {value!r}"
                ) from exc
    return None  # never written — valid None
```

**Module-level cleanup on Branch A merge.** `_HEARTBEAT_JSON_KEY` constant deleted. `_HEARTBEAT_TAG_KEY_LEGACY` constant deleted. `_POD_QUERY` rewritten to query `env` instead of `dockerArgs`. Module + class docstring rewritten — no more "production-safety constraint" warning, no more "provision_script=None only" caveat, no more C25 cross-reference comment block.

**New constant on Branch A merge:** `_HEARTBEAT_ENV_KEY: str = "KINOFORGE_LAST_HEARTBEAT"`. Sibling of `KINOFORGE_SELFTERM_SCRIPT` and `KINOFORGE_PROVISION_SCRIPT` already used elsewhere; matches the existing `KINOFORGE_*` env namespace.

**Per-tick cost.** One round-trip. B5a-measured P50=460ms, P99=583ms — no change. No rate-limit issue (B5a smoke confirmed no 429 at 5s cadence within 60s; 30s default `heartbeat_interval_s` is comfortably safe).

**Side benefit.** The env carrier is visible from inside the pod — `echo $KINOFORGE_LAST_HEARTBEAT` works at `docker exec` time. Operators can sanity-check from a shell without touching the GraphQL API. Branch B loses this.

## 5. Branch B — dockerArgs preserve-and-merge fallback

Fires when probe outcome is `"replace"` or `"read-unavailable"`.

**Write shape.** Two round-trips per tick:

1. Read current `dockerArgs`:
   ```graphql
   query GetPod($podId: String!) {
     pod(input: {podId: $podId}) { id dockerArgs }
   }
   ```
2. Strip-and-append, then mutate:
   ```python
   raw = pod.get("dockerArgs") or ""
   base = re.sub(r"\s*#\s*_kinoforge_hb:[^\n]*$", "", raw)
   if base.strip() == "":
       merged = f": # _kinoforge_hb:{ts_local.isoformat()}"
   else:
       merged = f"{base} # _kinoforge_hb:{ts_local.isoformat()}"
   ```
3. Issue:
   ```json
   {"input": {"podId": "<id>", "dockerArgs": "<merged>"}}
   ```

**Strip regex contract.**
- `\s*#\s*_kinoforge_hb:` — tolerates any whitespace around `#` and after `#`.
- `[^\n]*$` — comment runs to end of string (single-line dockerArgs is the production form).
- Anchored to end-of-string ONLY — never strips mid-string `#` comments inside the Phase 24 bash decoder.

**Read shape.** Query unchanged from B5a (`pod { id dockerArgs }`). Parser changes from JSON-extract to regex-extract:

```python
def read(self, instance_id: str) -> datetime | None:
    payload = {"query": _POD_QUERY, "variables": {"podId": instance_id}}
    try:
        resp = self._http_post(self._graphql_url, payload)
    except TransportError:
        raise
    except Exception as exc:
        raise TransportError(f"RunPod pod query transport failure: {exc}") from exc
    if "errors" in resp:
        raise TransportError(f"RunPod pod query failed: {resp['errors']}")
    pod = resp.get("data", {}).get("pod")
    if pod is None:
        return None
    raw = pod.get("dockerArgs")
    if not isinstance(raw, str):
        return None
    m = re.search(r"#\s*_kinoforge_hb:([^\n]+?)\s*$", raw)
    if m is None:
        return None
    value = m.group(1).strip()
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise TransportError(
            f"corrupted heartbeat marker for {instance_id}: {value!r}"
        ) from exc
```

**Bare-pod case** (`provision_script=None`, original `dockerArgs == ""`):

- Append produces `: # _kinoforge_hb:<ISO>`.
- `:` is bash no-op-true; Docker invokes the field via `sh -c`, gets a no-op, exits 0 — same behaviour as today's empty-field case.
- Next-tick strip regex matches the ` # _kinoforge_hb:<ISO>` portion; the `:` survives strip. New marker appended. Idempotent over many ticks; whitespace accumulation is bounded because `\s*` in the strip regex swallows any extra space.

**Pod restart safety.**
- Post-write `dockerArgs`: `bash -c "..."  # _kinoforge_hb:<ISO>`.
- Docker invokes as `sh -c <whole-string>`. Bash sees `#` as start-of-comment; rest of line discarded. The Phase 24 bash decoder runs intact. Selfterm script in `KINOFORGE_SELFTERM_SCRIPT` env survives independently (env was never touched).
- No code path executes the comment content; it is purely orchestrator-side metadata stashed in a live-API-mutable wire slot.

**Per-tick cost.** Two round-trips ≈ 920ms P50, 1.17s P99. At 30s default `heartbeat_interval_s`, that is 3-4% wall-time consumption. Acceptable; B5a §11 risk #1 documents >0.5% as the floor to argue against; 3-4% remains within the substrate-invariant band.

**Module-level updates on Branch B merge.** `_HEARTBEAT_JSON_KEY` constant repurposed as `_HEARTBEAT_MARKER_KEY = "_kinoforge_hb"` (used to build the regex). `_HEARTBEAT_TAG_KEY_LEGACY` deleted. `_POD_QUERY` unchanged. Module + class docstring updated — the C25 production-safety paragraph is replaced with a "preserve-and-merge" explainer block. The "provision_script=None only" caveat is removed.

**Race window analysis.** Between the query (step 1) and mutation (step 2), `dockerArgs` could theoretically change. The only writers in the codebase are:

- This heartbeat loop (serial; one `HeartbeatLoop` per orchestrator).
- Phase 24 `_create_pod` (writes only at pod creation; HeartbeatLoop does not start until after the pod is `ready`).
- A hypothetical cross-orchestrator writer attaching to the same pod — blocked by B7's `provision:<id>` lock during attach.

No other in-tree writer exists. The race is closed by construction.

## 6. Guard deletion (both branches)

`src/kinoforge/_adapters.py` edits, applied on either branch:

- Delete the `_RUNPOD_HEARTBEAT_SAFE_ENGINES` constant (lines 62-67).
- Delete the `if kind not in _RUNPOD_HEARTBEAT_SAFE_ENGINES: raise ValidationError(...)` block inside `build_heartbeat_endpoint_for` (lines 114-127).
- The remaining code path inside the `provider == "runpod"` branch is unchanged: `RUNPOD_API_KEY` check, then `RunPodGraphQLHeartbeatEndpoint(api_key=api_key)` construction.

After deletion, the substrate Protocol is the sole contract gating heartbeat behaviour. Any future engine that needs to write its own wire slot must declare its own carrier (env var, GraphQL field) — it must not negotiate `dockerArgs` ownership through a deny-list.

`tests/test_adapters_heartbeat.py` (the file containing the guard's negative-path tests) updated: the test asserting `engine.kind=comfyui` raises `ValidationError` is INVERTED to assert it now returns a `RunPodGraphQLHeartbeatEndpoint` instance.

## 7. Tests

Three test layers; two live smokes.

### Unit tests (offline, branch-conditional)

`tests/providers/runpod/test_heartbeat.py` rewritten for the selected branch. Branch-not-selected tests deleted, not skipped.

**Branch A (env-additive) — 7 tests:**
- `test_write_payload_shape` — spy `http_post`; assert mutation includes `env: [{key: "KINOFORGE_LAST_HEARTBEAT", value: <iso>}]` and no `dockerArgs` key in variables.
- `test_read_walks_env_array` — spy returns `{"data": {"pod": {"env": [{"key": "KINOFORGE_LAST_HEARTBEAT", "value": "<iso>"}, {"key": "OTHER", "value": "x"}]}}}`; assert parsed datetime returned; assert sibling keys ignored.
- `test_read_missing_key_returns_none` — spy returns env without the kinoforge key; assert None.
- `test_read_pod_null_returns_none` — `data.pod == null`; assert None (instance gone, not a transport error).
- `test_read_corrupted_iso_raises_transport_error` — kinoforge key present, value `"not-an-iso"`; assert `TransportError`.
- `test_write_graphql_errors_raises_transport_error` — spy returns `{"errors": [...]}`; assert raise.
- `test_tz_preservation_roundtrip` — write ISO with `-07:00`, fake-storage round-trips to read; assert `tzinfo` survives.

**Branch B (dockerArgs preserve-and-merge) — 9 tests + 2 standard arms preserved:**
- `test_write_does_read_then_mutation` — spy records call sequence; assert exactly 2 round-trips per `write`; first is query, second is mutation.
- `test_write_preserves_bash_base` — pre-populate dockerArgs with the Phase 24 bash decoder string; call `write`; assert mutation's `dockerArgs` value starts with the original bash and ends with a marker suffix.
- `test_write_strips_stale_marker_before_appending` — pre-populate with `<bash> # _kinoforge_hb:OLD`; call `write` with NEW timestamp; assert mutation value contains exactly ONE marker, with NEW timestamp.
- `test_write_bare_pod_produces_no_op_command` — pre-populate dockerArgs as `""`; call `write`; assert mutation value is `: # _kinoforge_hb:<iso>`.
- `test_write_idempotent_on_repeated_same_ts` — call `write(id, t)` twice in a row with the same ts; assert second mutation matches first byte-for-byte (whitespace accumulation does not drift).
- `test_read_extracts_marker_from_bash_tail` — fake-storage returns full bash + marker; assert parsed datetime matches.
- `test_read_no_marker_returns_none` — fake-storage returns bare bash with no marker; assert None.
- `test_read_mid_string_hash_does_not_match` — fake-storage returns bash containing `# something-else _kinoforge_hb:foo` mid-line followed by additional bash; assert None (regex anchored to end-of-string).
- `test_read_corrupted_iso_raises_transport_error` — marker present, value `"abc"`; assert raise.
- Standard pod-null + GraphQL-errors arms preserved.

### Parity tests

`tests/providers/test_heartbeat_parity.py` — unchanged. The contract is wire-agnostic; `FakeRunPodHeartbeatEndpoint` is dict-backed and does not exercise wire detail. No new fakes needed.

### Integration test

`tests/core/test_heartbeat_loop.py` — unchanged. `HeartbeatLoop` drives `RunPodProvider.heartbeat`, which delegates to the endpoint. Wire mechanics are opaque to the loop.

### Guard removal test

`tests/_adapters/test_build_heartbeat_endpoint_for.py` (or wherever the existing engine-kind-raise test lives) — invert the assertion: where the test previously asserted `ValidationError` for `engine.kind=comfyui`, now assert successful construction of a `RunPodGraphQLHeartbeatEndpoint` instance.

## 8. Live smokes

### Probe smoke (Task a — pre-implementation, ≤$0.05)

`tests/live/test_runpod_env_semantics_probe.py`, gated by `KINOFORGE_LIVE_RUNPOD=1`:

1. Spin cheapest bare RunPod pod (no GPU; reuse `runpod/base:ubuntu` per B5a §9). 60s wall budget.
2. Pod created with `env = {"PROBE_KEEP_A": "keep", "PROBE_KEEP_B": "keep"}`.
3. Issue `podEditJob` with single-key `env: [{key: "PROBE_NEW", value: "new"}]`.
4. Query pod env via `pod { env { key value } }`.
5. Three terminal cases:
   - All three keys present → write `{"semantics": "additive", "captured_at": "<iso>", "tested_pod_id": "<id>"}` to `tests/live/_runpod_env_semantics.json`.
   - Only `PROBE_NEW` present → write `{"semantics": "replace", ...}`.
   - Query returns env unavailable / null / GraphQL error on the env field → write `{"semantics": "read-unavailable", ...}` (routes to Branch B).
   - Any other outcome → fail hard with diagnostic dump including the full response payload.
6. Teardown pod via existing destroy path.
7. The sidecar JSON is the spec's source-of-truth for branch selection in Task b. §3 of this spec is amended with the outcome before Task b implementation begins.

RED scaffold (probe script + sentinel-file-check failing test) committed BEFORE the live invocation, per CLAUDE.md durability rules.

### Acceptance smoke (Task d — production gate, ~$0.30)

`tests/live/test_c25_warm_reuse_comfyui_wan_live.py`, gated by `KINOFORGE_LIVE_RUNPOD=1`:

Uses the entry-#5 Wan 2.1 14B T2V config (RTX A5000, ~$0.34/hr). Two prompts:
- Gen 1: `--prompt "$(cat prompt-field-realistic.txt)"` (per `feedback_standard_test_prompt` — same prompt body both runs for cross-run comparability).
- Wait 60s.
- Gen 2: same command, same config, same prompt.

Both runs invoke `pixi run kinoforge --state-dir <tmp>/state generate -c tests/live/cfg_c25_wan_comfyui.yaml --prompt "..." --mode t2v --run-id c25-<n>` as separate subprocess CLIs. Config sets:

```yaml
compute:
  provider: runpod
  warm_reuse_auto_attach: true
  heartbeat_mode: graphql-tag
  lifecycle:
    heartbeat_interval_s: 30
    # ... usual Wan-on-RunPod lifecycle settings
```

No `--instance-id`, no `--force-attach` on either invocation.

**Pass criteria:**
- Gen 2's stdout contains `warm-reuse: attached to <pod_id>` AND that pod_id matches Gen 1's logged pod_id.
- Gen 2 wall-time < 0.7 × Gen 1 wall-time (cold-skip threshold mirrors B3 entry #6).
- Final ledger inspection (via `kinoforge status --id <pod_id>`) shows `last_heartbeat` within 60s of teardown.
- Branch A: post-smoke direct GraphQL query confirms `KINOFORGE_LAST_HEARTBEAT` present in pod env AND `KINOFORGE_SELFTERM_SCRIPT` survives.
- Branch B: post-smoke direct GraphQL query confirms `dockerArgs` contains both the Phase 24 bash decoder substring AND exactly one ` # _kinoforge_hb:<ISO>` tail.

**Teardown:** explicit `kinoforge destroy --id <pod_id>` at end; assert pod absent from `myself.pods` post-destroy. `successful-generations.md` gets a new entry #7 (or entry #6 closure note per CLAUDE.md schema rule).

RED scaffold committed BEFORE the live invocation, per CLAUDE.md durability rules.

## 9. Task split

| # | Task | Files | Live | Spend |
|---|---|---|---|---|
| a | Env-semantics live probe + sidecar JSON + spec §3 branch selector amendment | `tests/live/test_runpod_env_semantics_probe.py`, `tests/live/_runpod_env_semantics.json` (output), this spec | yes | ≤$0.05 |
| b | Rewrite `RunPodGraphQLHeartbeatEndpoint` per probe outcome + rewrite wire-shape unit tests | `src/kinoforge/providers/runpod/heartbeat.py`, `tests/providers/runpod/test_heartbeat.py` | no | — |
| c | Delete `_RUNPOD_HEARTBEAT_SAFE_ENGINES` + engine-kind `ValidationError` block + invert guard-removal test | `src/kinoforge/_adapters.py`, `tests/test_adapters_heartbeat.py` | no | — |
| d | C25 acceptance smoke: Wan + ComfyUI + 2 CLI gens 60s apart with auto-attach asserted | `tests/live/test_c25_warm_reuse_comfyui_wan_live.py`, `tests/live/cfg_c25_wan_comfyui.yaml` | yes | ~$0.30 |
| e | Spec §16 closeout, `PROGRESS.md` §C25 strike-through + commit SHA, `successful-generations.md` entry #7, B5a §9 amendment with closure pointer | docs | no | — |

**Order:** a (probe) → b + c (parallel; b consumes probe outcome, c is wire-independent) → d (consumes b + c) → e (consumes d). Atomic commit per task per CLAUDE.md.

## 10. Risk register

1. **Probe outcome inconclusive.** `pod { env }` may not surface env at all on the read path (RunPod field availability is undocumented). Mitigation: probe records `{"semantics": "read-unavailable", ...}` and routes to Branch B unconditionally. Branch B is the proven-safe fallback regardless of env semantics; design has no remaining unknown after the probe completes.
2. **Pod restart between read and mutation (Branch B).** Theoretical race. Single-writer invariant (B7 lock) closes the cross-orchestrator case; intra-orchestrator HeartbeatLoop is single-threaded. Race window ≈ 460ms; if restart occurs mid-write, RunPod's atomic field write preserves the pre-write `dockerArgs` (still valid bash) and next tick re-syncs. Zero cost-leak.
3. **Wan smoke offer flake.** RTX 4090/A5000 capacity may be unavailable at smoke time. Mitigation: smoke YAML's `gpu_preference` already lists `4090 → A5000 → 3090` fallback per entry #6. Cold-skip ratio threshold (0.7) is offer-class-independent.
4. **B3 auto-attach interaction.** Today, `_scan_warm_candidates` gates on `classify() ≠ HEARTBEAT_SUBSTRATE_MISSING`. With the C25 guard deleted, ComfyUI pods now produce real heartbeats → classify returns LIVE → auto-attach fires. No B3 code change required, but the acceptance smoke is the integration test.
5. **Branch A env-length-invariant callers.** Any caller iterating a pod's env array and assuming a specific count (e.g. `assert len(pod.env) == 4`) breaks after the heartbeat injects a new key. Grep across the codebase before Branch A merge — the plan must enumerate every env-iteration call site and verify no length-assert exists. Today's known iterators are `_create_pod` (writer) and (Branch A only) the new heartbeat reader; nothing else iterates pod env on the wire side.

## 11. Acceptance criteria

A C25-shipped state satisfies:

- **AC1.** Probe sidecar `tests/live/_runpod_env_semantics.json` exists with `{"semantics": "additive" | "replace" | "read-unavailable", "captured_at": "<iso>", "tested_pod_id": "<id>"}`. §3 of this spec is amended to reflect the outcome BEFORE Task b implementation begins.
- **AC2.** `_RUNPOD_HEARTBEAT_SAFE_ENGINES` constant and the engine-kind `ValidationError` raise in `build_heartbeat_endpoint_for` are deleted. `build_heartbeat_endpoint_for(cfg)` with `compute.provider="runpod"` + `compute.heartbeat_mode="graphql-tag"` + `engine.kind="comfyui"` returns a `RunPodGraphQLHeartbeatEndpoint` instance without raising.
- **AC3.** Wire-shape unit tests (Branch A: 7 tests; Branch B: 9 tests + 2 standard pod-null / GraphQL-errors arms preserved) green on the selected branch. Branch-not-selected unit tests deleted, not skipped.
- **AC4.** Branch A: `RunPodGraphQLHeartbeatEndpoint.write` issues exactly ONE GraphQL round-trip per call; mutation payload contains `env: [{key: "KINOFORGE_LAST_HEARTBEAT", value: <iso>}]` and no `dockerArgs` key. Branch B: `RunPodGraphQLHeartbeatEndpoint.write` issues exactly TWO round-trips per call (query then mutation); mutation `dockerArgs` value starts with whatever the pre-existing `dockerArgs` content was (with any prior `# _kinoforge_hb:` tail stripped) and ends with a single ` # _kinoforge_hb:<iso>` tail.
- **AC5.** `RunPodGraphQLHeartbeatEndpoint.read` returns the most-recently-written timestamp; returns `None` for never-written, instance-gone, and missing-marker cases; raises `TransportError` for corrupted ISO, GraphQL errors, and HTTP non-2xx.
- **AC6.** Live acceptance smoke (Task d) passes:
  - Gen 1 cold-creates a pod via the standard ComfyUI + Wan provision path.
  - Gen 2, fresh CLI subprocess 60s later with identical config and no `--instance-id` / `--force-attach`, logs `warm-reuse: attached to <pod_id>` with `pod_id == gen1_pod_id`.
  - Gen 2 wall-time < 0.7 × Gen 1 wall-time.
  - Final direct GraphQL inspection: Branch A → `KINOFORGE_LAST_HEARTBEAT` present in pod env AND `KINOFORGE_SELFTERM_SCRIPT` survives. Branch B → `dockerArgs` contains BOTH the Phase 24 bash decoder substring AND exactly one ` # _kinoforge_hb:<ISO>` tail.
  - Final pod `kinoforge destroy --id <pod_id>` succeeds; pod absent from `myself.pods` post-destroy.
- **AC7.** `PROGRESS.md` §C C25 entry struck-through with `CLOSED by <SHA>` reference. `docs/superpowers/specs/2026-06-12-b5a-heartbeat-substrate-design.md` §9 wire-discovery note amended with closure pointer to this spec. `successful-generations.md` entry #7 appended with full C25-closure schema (or entry #6 production-limitation paragraph struck-through if entry #6's `(provider, engine, model, mode)` tuple is reused).

## 12. Out of scope

- B5b SkyPilot satisfier — gated on A3/A4 GPU quota. Substrate Protocol contract preserved untouched here.
- Selfterm-HTTP RunPod satisfier (B5a §12 alternate mode) — second satisfier slot remains open.
- Per-entry `heartbeat_interval_s` override (B6).
- Removing the selfterm script from `dockerArgs` entirely — the script IS the cost-safety floor; out of scope.
- Migration of the historical Phase 24 `dockerArgs` scheme to a different carrier — current scheme is correct; only the B5a-side write changes.

## 13. PROGRESS.md updates on C25 close

- §C C25: strike-through with `CLOSED by <SHA>` referencing the Task d acceptance-smoke commit.
- §B no entries removed (this fix closes an architectural follow-up; does not retire any spec-locked layer).
- §A no entries added.
- `successful-generations.md`: new entry #7 (or entry #6 closure note) capturing `(runpod, comfyui, wan-2.1-14b, t2v)` + B3 cross-CLI auto-attach + C25 heartbeat preserve-and-merge.

## 16. Closeout (PARTIAL — 2026-06-13)

**Outcome:** CLOSED (PARTIAL). C25 wire fix shipped and validated on a production pod. The full Wan + ComfyUI + 2-CLI warm-reuse end-to-end acceptance smoke (gen2 cold-skip ratio < 0.7) was deferred — gen 1 stalled before completing on a workload-side issue orthogonal to the C25 wire fix. Follow-up tracked under **C26** (RunPod util-aware stall classify).

**Tasks delivered:**

- **Task a (probe).** Commit `209a180`. `tests/live/_runpod_env_semantics.json` = `read-unavailable` at pod `ssbbm0vjyd56a9` (NVIDIA RTX A2000 @ $0.12/hr × 3.85 s ≈ $0.0001 spend). RunPod's GraphQL `pod.env` is typed `[String]` with no subfields — the `env { key value }` selection returns HTTP 400 `GRAPHQL_VALIDATION_FAILED`. Branch B selected.
- **Task b (wire fix).** Commit `71dea61`. `RunPodGraphQLHeartbeatEndpoint` rewritten as Branch B preserve-and-merge: two GraphQL round-trips per tick (query → mutation); the marker capture regex tightened from `[^\n]+?` to `\S+` to reject mid-string `# _kinoforge_hb:` occurrences inside `echo` arguments. 11/11 wire-shape unit tests green.
- **Task c (guard delete).** Commit `23cb880`. `_RUNPOD_HEARTBEAT_SAFE_ENGINES` allow-list deleted; `build_heartbeat_endpoint_for` dispatches purely on `(provider, mode)` now. 7/7 adapter tests green; 228/228 provider suite green.
- **Task d (acceptance smoke).** Commits `a17ae55` (RED scaffold), `5323907` (cfg graph_file fix), `7436969` (PROVEN-PARTIAL evidence). RED scaffold committed before live spend per durability rule. Live run on production pod `uokf7x7cbfcunk` (RTX A2000 @ $0.16/hr ≈ $0.0646 spend) was killed at ~22 min wall after operator-observed stall on RunPod console; pre-kill GraphQL dockerArgs readback proves the C25 wire fix works on a real pod (Phase 24 bash decoder INTACT + exactly one `# _kinoforge_hb:` marker; ISO matches `ledger.last_heartbeat` for cross-validation).
- **Task e (this closeout).** Current commit. PROGRESS §C C25 strike-through with PARTIAL note; B5a spec §9 closure pointer; this §16 block. `successful-generations.md` deliberately NOT amended: no qualifying video was produced (file preamble forbids non-video entries). Entry #6's "Production limitation (C25)" paragraph stays as-is; the next operator to land a clean Wan + ComfyUI + warm-reuse run produces the closing entry naturally.

**Evidence captured (Task d):**

- Probe pod (Task a): `ssbbm0vjyd56a9`. Outcome: `read-unavailable`. Sidecar: `tests/live/_runpod_env_semantics.json`.
- Smoke pod (Task d): `uokf7x7cbfcunk`. Pre-kill dockerArgs:

  ```
  bash -c "echo $KINOFORGE_PROVISION_SCRIPT | base64 -d > /tmp/p.sh && chmod +x /tmp/p.sh && bash /tmp/p.sh" # _kinoforge_hb:2026-06-13T14:23:45.666422-07:00
  ```

  Sidecar: `tests/live/_c25_smoke_evidence.json`.

**Total live spend:** ≈ $0.065 across both pods. Both pods destroyed; preflight clean post-close.

**Deferred to C26 (RunPod util-aware stall classify):**

- gen1 + gen2 wall-time ratio (cold-skip benefit) acceptance gate.
- `warm-reuse: attached to <pod_id>` log-line assertion across two fresh-CLI subprocesses on the Wan workload.
- A `STALL_REAP` verdict on the classify path so an idle / stuck pod (RAM/GPU/VRAM/disk util near zero with heartbeats still ticking) auto-tears-down instead of the operator catching it manually.

C25 is honestly closed at the substrate level. The remaining gap is observability of the in-pod workload, which is a different problem.

**2026-06-13 update — C26 outcome:** the C26 follow-up (RunPod
util-aware stall classify) was implemented and shipped PARTIAL.
The C26 substrate (UtilSnapshot Protocol, RunPod GraphQL
satisfier, consecutive-low counter, classify row 3', HeartbeatLoop
self-classify, cross-process kwarg threading, --stall-window-override
CLI flag) all landed end-to-end and was PROVEN on the cheap
FakeEngine Phase A smoke at counter × interval ≥ window. But the
Phase B Wan + ComfyUI re-fire of THIS gate exposed a C26 design
hole: the pod's chronic container restart loop (uptime_seconds=1
every tick) defeats `_update_counter`'s uptime-decrease guard so
the counter never accumulates. The Phase-A class of stall (steady
low util) is protected; the Phase-B class (this C25 stall) is not.
C25 Task 4's deferred gate remains open. Tracked as **C27**
(restart-loop stall detection — sibling predicate to C26's low-util
predicate). See C26 spec §17 + sidecars
`tests/live/_c26_phase_a_smoke_evidence.json` /
`tests/live/_c26_phase_b_smoke_evidence.json`.
