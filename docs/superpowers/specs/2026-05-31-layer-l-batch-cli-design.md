# Layer L — `kinoforge batch` CLI subcommand (design)

**Status:** validated 2026-05-31. Source of truth for *what* + *how* of Layer L.
**Plan:** `docs/superpowers/plans/2026-05-31-layer-l-batch-cli.md` (to be written next).
**Precedes:** GitHub issue (not yet filed) and PROGRESS Phase 22 entry.

## 1. Purpose & scope

### Problem

Producing N video clips on one engine/provider config today requires N parallel shell invocations.
That shape is hostile to both humans (`vars && cmd1 & cmd2` accidentally scopes vars to the
backgrounded subshell — silent footgun) and LLMs (no canonical way to express "render these 12
prompts on the same warm pod").

### Goal

Ship `kinoforge batch` — a single CLI subcommand that owns the parallel-dispatch shape end-to-end,
sharing one deployed compute instance across N entries with continue-on-error semantics and a
machine-readable summary file.

### Non-goals

- Cross-engine batch (each entry uses a different engine). Reject — different engines need
  different deploys.
- Background daemon / scheduler (cron-style batches). Out of scope for this layer.
- Retries-on-failure. Continue-on-error reports failure; the user/LLM decides what to do next.
- Per-entry budget envelopes. One shared envelope per batch (matches the shared-deploy model).
- Replacing `kinoforge generate`. `generate` stays the single-clip entry point; `batch` is a sibling.

## 2. Resolved decisions (locked)

| Topic | Decision |
|---|---|
| **Compute model** | One `create_instance` per batch; ConcurrentPool fans entries across the same backend. Cheaper, faster, reuses warm model. |
| **Input format** | YAML manifest file (`--manifest PATH`). Each entry is `{prompt|prompt_file, mode, run_id?, params?, spec?, assets?}`. Exactly one of `prompt` / `prompt_file` required per entry. |
| **Output namespacing** | `--batch-id ID`, default `batch-YYYYMMDD-HHMMSS` in **local** timezone. Entries land under `<store>/<batch_id>/<entry.run_id>/`. Per-entry `run_id` auto-indexes (`"0"`, `"1"`, …) when none are set; required-and-unique when any are set. |
| **Failure mode** | Continue-on-error per entry. Fatal batch-wide on `BudgetExceeded` / `CapabilityMismatch` / `TeardownError` (compute is gone — no point continuing). |
| **Concurrency** | `--concurrent N` flag overrides `cfg.lifecycle.max_in_flight`; default reads the cfg value. |
| **Architectural shape** | Extract steps 1–7 of `orchestrator.generate()` into a `deploy_session` context manager. Both `generate()` and the new `batch_generate()` consume it. Zero duplication, clean seam for future Stages (keyframe, audio). |
| **Per-entry overrides** | `params` / `spec` shallow-merged onto `cfg.params` / `cfg.spec` (entry wins per key); `assets` replaces; `mode` is per-entry only. Anything else → pydantic `extra="forbid"` error. |
| **Timezone policy** | Local timezone everywhere — no UTC. Filenames, batch IDs, summary JSON timestamps. |

## 3. Architecture

Two new public surfaces in `core/orchestrator.py`; one new module in `core/batch.py`. Adapter layers
unchanged.

```
core/
  orchestrator.py
    deploy_session(cfg, *, store, ..., run_id=None) -> ContextManager[DeploySession]
        # NEW. Extracts steps 1–7 of current generate():
        #   key, engine, provider, hosted preflight, profile (cache or discover),
        #   instance create + provision + verify.
        # Yields DeploySession dataclass:
        #   .backend       GenerationBackend
        #   .profile       ModelProfile
        #   .pool          ConcurrentPool already wrapping backend with max_in_flight
        #   .instance      Instance | None  (None on hosted path)
        #   .engine        GenerationEngine
        #   .provider      ComputeProvider | None
        # On __exit__: pool.close() (idempotent); instance teardown stays Ledger/sweeper-owned
        # (matches current generate() behavior — no implicit destroy at end).
        # On CapabilityMismatch / fatal raise inside the with-block: tears down
        # instance before re-raising (preserves Layer K's fail-hard contract).

    generate(cfg, request, ...) -> Artifact
        # REWRITTEN as: with deploy_session(...) as s: stage.run(request)
        # Public signature, return value, exceptions all unchanged.
        # Verified by existing 708-test suite.

  batch.py                                    # NEW
    BatchEntry                                # pydantic v2 model (§4)
    BatchManifest                             # pydantic v2 model
    load_manifest(path: Path) -> BatchManifest
    BatchOutcome                              # dataclass: run_id, status, artifact|error, duration_s
    BatchResult                               # dataclass: batch_id, started_at, finished_at, outcomes
    batch_generate(
        cfg: Config,
        manifest: BatchManifest,
        *,
        store: ArtifactStore,
        batch_id: str,
        concurrent: int | None = None,
        ...,
    ) -> BatchResult
        # NEW. with deploy_session(cfg, ..., run_id=batch_id) as s:
        #   futures = [(entry, executor.submit(stage.run, ...)) for entry in manifest.entries]
        #   collect via as_completed (NOT ConcurrentPool.map — map is fail-fast)
        #   each future wrapped to swallow per-entry exceptions into BatchOutcome
        #   fatal exceptions (BudgetExceeded, CapabilityMismatch, TeardownError) re-raise
        #     and abort batch (instance gone → no point continuing)

cli.py
    p_batch = sub.add_parser("batch", help="run a batch of generation jobs")
    _cmd_batch(args, state_dir) -> int
```

### Invariants preserved

- **Core-import-ban:** `core/batch.py` imports only from `core/` siblings and `pipeline/`. No
  `kinoforge.providers/sources/engines` import — enforced by `tests/test_core_invariant.py`.
- **Single-source `validate_request`:** runs once per entry inside `GenerateClipStage.run` (Layer K
  Task 2 moved it there). `batch_generate` does not call it directly.
- **Warm reuse:** `deploy_session.__exit__` does NOT destroy the instance on success — same as
  today's `generate()`. Sweeper / `BudgetTracker` / `LifecycleManager` own destroy.
- **Fail-hard on capability drift:** CapabilityMismatch inside the with-block still destroys the
  instance before re-raising. No new code path; reused from existing orchestrator behavior.

### Test surface that anchors the refactor

The existing 708-test suite of `orchestrator.generate()` is the regression contract. If any of
those tests need modification, the refactor is wrong. Specifically locked down:

- step ordering (`tests/core/test_orchestrator.py`, 12 ACs)
- profile cache hit/miss + verify-skip-on-fresh-discover
- CapabilityMismatch teardown path
- Layer K `cfg.params` / `cfg.spec` routing + ValidationError teardown
- compute preflight + provision-once locking
- hosted preflight + AuthError

## 4. Manifest schema

```python
# core/batch.py

from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kinoforge.core.errors import ConfigError


class BatchEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")        # unknown keys → pydantic ValidationError

    prompt: str | None = None
    prompt_file: str | None = None                   # path; resolved relative to manifest dir
    mode: str                                         # REQUIRED per entry (no inherited default)
    run_id: str | None = None                         # auto-indexed if absent
    params: dict[str, Any] | None = None              # merged onto cfg.params (entry wins, per-key)
    spec: dict[str, Any] | None = None                # merged onto cfg.spec   (entry wins, per-key)
    assets: list[dict[str, Any]] | None = None        # forwarded into GenerationRequest.assets

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> "BatchEntry":
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError(
                "entry must set exactly one of `prompt` / `prompt_file`"
            )
        return self


class BatchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[BatchEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_run_ids(self) -> "BatchManifest":
        # If ANY entry sets run_id, ALL run_ids (including auto-derived) must be unique.
        # If NONE set run_id, batch_generate auto-indexes "0", "1", ... — collision-free by construction.
        ids = [e.run_id for e in self.entries if e.run_id is not None]
        if ids and len(set(ids)) != len(ids):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"duplicate run_id in manifest: {dupes}")
        return self


def load_manifest(path: Path) -> BatchManifest:
    import yaml

    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ConfigError("manifest top-level must be a YAML list of entries")
    manifest = BatchManifest(entries=raw)

    # Resolve prompt_file paths + read text NOW (fail-fast on missing files).
    base = path.parent
    for entry in manifest.entries:
        if entry.prompt_file is not None:
            resolved = (base / entry.prompt_file).resolve()
            if not resolved.is_file():
                raise ConfigError(
                    f"prompt_file not found: {resolved} (entry mode={entry.mode!r})"
                )
            entry.prompt = resolved.read_text().strip()
            entry.prompt_file = None                  # collapse to inline; downstream sees one shape

    # Auto-index run_ids on entries that lack one.
    for idx, entry in enumerate(manifest.entries):
        if entry.run_id is None:
            entry.run_id = str(idx)

    return manifest
```

### Override merge rules

| Field | Base (cfg) | Override (entry) | Result |
|---|---|---|---|
| `params` | `cfg.params` (Layer K) | `entry.params` | shallow per-key merge; entry wins (matches Layer K segment-wins precedent) |
| `spec`   | `cfg.spec`   (Layer K) | `entry.spec`   | shallow per-key merge; entry wins |
| `assets` | (none in cfg today) | `entry.assets` | replace (list, not dict — no merge) |
| `mode`   | (n/a in cfg) | `entry.mode`   | always from entry |
| `run_id` | n/a | `entry.run_id` or auto-index | namespaces under `<batch_id>/<run_id>/` |

### Example manifest

```yaml
# examples/configs/batch-prompts.yaml — invoke: kinoforge batch -c base.yaml --manifest batch-prompts.yaml
- prompt: "waves crashing on basalt cliffs at dusk"
  mode: t2v
  run_id: waves

- prompt_file: prompts/forest.txt
  mode: t2v
  run_id: forest
  params:
    seed: 42

- prompt_file: prompts/dawn-flight.md
  mode: i2v
  run_id: dawn
  assets:
    - kind: image
      role: init_image
      ref: "file:///workspace/seed/dawn.png"
```

## 5. CLI surface

```
kinoforge batch -c CONFIG --manifest MANIFEST [--batch-id ID] [--concurrent N] [--env-file ENV]
```

### Flags

| Flag | Required | Default | Notes |
|---|---|---|---|
| `-c` / `--config PATH` | yes | — | Same loader as `generate` |
| `--manifest PATH` | yes | — | YAML list of entries (§4) |
| `--batch-id ID` | no | `batch-YYYYMMDD-HHMMSS` (local) | Same-second collision → exit 1 |
| `--concurrent N` | no | `cfg.lifecycle.max_in_flight` | Overrides cfg if set; `N < 1` → exit 1 |
| `--env-file PATH` | no | — | Mirrors existing `generate` flag (Phase 14) |
| `--state-dir PATH` | no | `.kinoforge` | Mirrors top-level CLI flag |

Removed vs `generate`: `--prompt`, `--mode`, `--run-id` — all live inside the manifest.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | All entries OK |
| 1 | One+ entry failed (continue-on-error semantics); CLI flag / manifest validation error; same-second `batch_id` collision |
| 2 | Fatal mid-batch (`BudgetExceeded` / `CapabilityMismatch` / `TeardownError`) — instance gone, batch aborted, remaining entries unstarted |

### Stdout

Streaming per-entry log:

```
[batch-20260531-093052] manifest loaded: 3 entries, concurrency=2
[batch-20260531-093052] deploying compute (engine=comfyui, provider=runpod) ...
[batch-20260531-093052] ready: instance=pod_abc123, profile_cache=hit
[batch-20260531-093052] waves    start
[batch-20260531-093052] forest   start
[batch-20260531-093052] waves    ok      14.2s  uri=file:///.kinoforge/artifacts/batch-20260531-093052/waves/abc.mp4
[batch-20260531-093052] dawn     start
[batch-20260531-093052] forest   fail    18.7s  ValidationError: prompt 4096 chars > engine limit
[batch-20260531-093052] dawn     ok      11.9s  uri=...
```

Final summary block (always printed last):

```
summary:
  waves     OK     14.2s   file:///.kinoforge/artifacts/batch-20260531-093052/waves/abc.mp4
  forest    FAIL   18.7s   ValidationError: prompt 4096 chars > engine limit
  dawn      OK     11.9s   file:///.kinoforge/artifacts/batch-20260531-093052/dawn/def.mp4
batch-id: batch-20260531-093052
results:  2/3 ok, 1 failed
exit 1
```

### Machine-readable summary file

Written to `<store>/<batch_id>/_batch_summary.json` **on every exit path** (success, per-entry
failure, fatal mid-batch). Schema:

```json
{
  "batch_id": "batch-20260531-093052",
  "started_at": "2026-05-31T09:30:52",
  "finished_at": "2026-05-31T09:31:30",
  "entries": [
    {"run_id": "waves",  "status": "ok",      "duration_s": 14.2, "uri": "file:///..."},
    {"run_id": "forest", "status": "fail",    "duration_s": 18.7, "error": "ValidationError: prompt 4096 chars > engine limit"},
    {"run_id": "dawn",   "status": "ok",      "duration_s": 11.9, "uri": "file:///..."}
  ]
}
```

Timestamps are local-timezone ISO without offset suffix.

### Interactions with existing CLI

- `kinoforge gc --run <batch_id>` already works — gc walks `<root>/<run_id>/` and `batch_id` IS the
  store-level run_id. Per-entry sub-namespaces live underneath, swept atomically.
- `kinoforge status` / `list` / `reap` unchanged. Batch shares one instance; one ledger row.

## 6. Data flow

End-to-end trace of one `kinoforge batch` invocation. 3 entries, ComfyUI engine on RunPod pod,
cold profile cache.

```
CLI _cmd_batch
  │
  ├─ load_config(args.config)                            → cfg
  ├─ load_manifest(args.manifest)                        → BatchManifest
  │     resolves prompt_file → reads text → collapses to prompt
  │     auto-indexes empty run_ids
  │     validates extra="forbid"
  │     validates unique run_ids (when any set)
  ├─ batch_id = args.batch_id or datetime.now().strftime("batch-%Y%m%d-%H%M%S")
  ├─ store = _build_store(cfg, state_dir)
  ├─ check_batch_id_unused(store, batch_id):
  │     if store.list(batch_id):                        # any existing keys under batch_id?
  │       print(f"error: batch_id collision: {batch_id} already has artifacts"); exit 1
  │     # Best-effort check — race-vulnerable if two concurrent `kinoforge batch`
  │     # invocations pick the same timestamp at the same instant. Documented
  │     # limitation; mitigated by users passing --batch-id ID for batch farms.
  │
  └─ batch_generate(cfg, manifest, store=store, batch_id=batch_id, concurrent=args.concurrent)
        │
        ├─ STAGE-0 — deploy_session entry (steps 1–7 of old generate)
        │   key = cfg.capability_key()
        │   resolved_engine = _resolve_engine(cfg)
        │   resolved_provider = _resolve_provider(cfg)        # iff requires_compute
        │   hosted preflight if not requires_compute
        │   profile_provider = JsonProfileCache(store)
        │   try resolve(key)
        │     hit:  backend = engine.backend(instance, cfg_dict)
        │           verify(profile, backend) → may CapabilityMismatch + teardown
        │     miss: provider.create_instance(spec_with_tags={batch_id, key_hash})
        │           poll until ready
        │           _provision_compute_once(...)
        │           backend = engine.backend(instance, cfg_dict)
        │           profile = discover(key, engine, backend)
        │   pool = ConcurrentPool().add(backend, max_in_flight=concurrent or cfg.lifecycle.max_in_flight)
        │   yield DeploySession(backend, profile, pool, instance, engine, provider)
        │
        ├─ STAGE-1 — per-entry submission (inside with-block)
        │   #
        │   # Two-layer concurrency, BOTH driven by the same `max_in_flight` value:
        │   #   - OUTER (this executor): how many entries' GenerateClipStage.run() run concurrently.
        │   #   - INNER (ConcurrentPool slot's executor, sized at deploy_session-time): how many
        │   #     segment-level jobs hit the backend at once.
        │   # Most entries today are 1 segment (Layer K), so OUTER and INNER concurrency coincide.
        │   # Using the same knob avoids surprising the user with two tunables.
        │   #
        │   executor = ThreadPoolExecutor(max_workers=concurrent or cfg.lifecycle.max_in_flight)
        │   futures: list[(entry, Future[Artifact])] = []
        │   for entry in manifest.entries:
        │     merged_params = {**cfg.params, **(entry.params or {})}
        │     merged_spec   = {**cfg.spec,   **(entry.spec   or {})}
        │     request = GenerationRequest(
        │         prompt=entry.prompt,
        │         mode=entry.mode,
        │         assets=[Asset(**a) for a in (entry.assets or [])],
        │     )
        │     entry_run_id = f"{batch_id}/{entry.run_id}"
        │     stage = GenerateClipStage(
        │         pool=pool, store=store, run_id=entry_run_id,
        │         profile=profile, params=merged_params, spec=merged_spec,
        │     )
        │     fut = executor.submit(stage.run, request)
        │     futures.append((entry, fut))
        │
        ├─ STAGE-2 — collect (as_completed)
        │   outcomes: list[BatchOutcome] = []
        │   completed_entries: set[int] = set()                       # by entry index
        │   try:
        │     for fut in as_completed([f for _, f in futures]):
        │       entry, idx = entry_for(fut)
        │       try:
        │         artifact = fut.result()
        │         outcomes.append(BatchOutcome.ok(entry, artifact, duration))
        │         completed_entries.add(idx)
        │       except (BudgetExceeded, CapabilityMismatch, TeardownError):
        │         # Mark in-flight entry as INTERRUPTED; cancel queued; record as ABORTED.
        │         outcomes.append(BatchOutcome.interrupted(entry, duration))
        │         completed_entries.add(idx)
        │         for j, (other_entry, other_fut) in enumerate(futures):
        │           if j in completed_entries:
        │             continue
        │           if other_fut.cancel():                            # queued, never started
        │             outcomes.append(BatchOutcome.aborted(other_entry))
        │           else:                                              # in-flight, can't cancel
        │             outcomes.append(BatchOutcome.interrupted(other_entry))
        │         raise                                                # → CLI exit 2
        │       except Exception as e:
        │         outcomes.append(BatchOutcome.fail(entry, e, duration))   # continue-on-error
        │         completed_entries.add(idx)
        │   finally:
        │     summary = BatchResult(batch_id, started_at, datetime.now(), outcomes)
        │     store.put_json(batch_id, "_batch_summary.json", summary.to_dict())
        │
        ├─ STAGE-3 — return
        │   return summary
        │
        └─ deploy_session __exit__
            pool.close()                                            # idempotent
            instance NOT destroyed — sweeper/budget owns lifecycle
```

### Data flow contracts

1. **Per-entry isolation.** Each entry gets its own `GenerateClipStage`, its own `merged_params` /
   `merged_spec` dicts (shallow-copied so cross-entry mutation is impossible), its own `request`.
   Shared: `pool`, `backend`, `profile`, `store`.
2. **Where merges happen.** Merges happen in `batch_generate`, not in `Stage` or `Engine`. Stage
   receives already-merged `params` / `spec` — its existing contract is untouched.
3. **`validate_request` runs once per entry** inside `GenerateClipStage.run`. No change.
4. **Budget envelope.** `BudgetTracker` lives at `deploy_session` scope. Single envelope across the
   whole batch. Breach mid-batch → fatal exit code 2.
5. **Profile cache.** Cold → `discover` ONCE for the whole batch. Warm → `verify` ONCE. All entries
   reuse the same `profile` reference.
6. **Asset fetching.** `GenerateClipStage.run` already fetches assets per-entry via Phase 16
   machinery. Independent per-entry — no shared state.
7. **Store namespacing.** `store.put_bytes(f"{batch_id}/{entry.run_id}", name, data)` — depth-2
   namespace. Local / S3 / GCS stores all accept `/` in `run_id` today.

## 7. Error handling & cost-safety

### Error classification

| Bucket | Exception types | Scope | Action | Exit |
|---|---|---|---|---|
| **Pre-flight** | `ConfigError`, `FileNotFoundError` (manifest / prompt_file), pydantic `ValidationError`, unknown CLI flag | Before `deploy_session` | print to stderr, exit 1 | 1 |
| **Setup fatal** | `CapacityError`, `AuthError`, `UnknownAdapter`, hosted-preflight `KinoforgeError`, provider create timeout | Inside `deploy_session.__enter__` | bubble; no compute held; print to stderr | 1 |
| **Per-entry** | `ValidationError` (request mode/role/asset), `AssetFetchError`, `FrameExtractionError`, `KinoforgeError` from backend.submit/result, transient network errors | Inside one entry's Stage | swallow into `BatchOutcome(status="fail")`, log line, **continue** | 1 if any |
| **Batch fatal** | `BudgetExceeded`, `CapabilityMismatch`, `TeardownError` | Per-entry but signals shared compute is gone or budget breached | cancel queued futures, drain in-flight, write partial summary JSON, **re-raise** | 2 |
| **Programmer bug** | `KeyError`, `AttributeError`, `TypeError`, anything not in `KinoforgeError` hierarchy | Anywhere | NOT swallowed — bubble with traceback | 1 |

### Decision rule for "per-entry vs batch-fatal"

A failure is **batch-fatal** iff the shared compute or shared budget is no longer usable. Test:
"Would the next entry running on the same backend hit the same failure?" If yes → batch-fatal.

### Cost-safety invariants

1. **One ledger row per batch.** `deploy_session` records to the ledger once at create.
   `cli list` shows the row with `tag.batch_id=<id>`. No per-entry ledger rows.
2. **Budget envelope = single, cfg-scoped.** `BudgetTracker` is constructed inside `deploy_session`
   and `enforce(instance.cost_rate)` runs after every entry (success or fail). Breach → fatal
   exit 2; teardown by tracker; futures cancelled. **Hosted path exception:** when
   `engine.requires_compute=False`, `instance is None` — no per-second cost rate exists, so
   `BudgetTracker` is skipped (matches today's `orchestrator.generate()` behavior on hosted).
3. **Warm reuse after partial failure.** A batch with N OK and M FAIL still ends with the instance
   warm + idle. Next `generate` / `batch` on the same `capability_key` reuses it via the existing
   warm-reuse path.
4. **Fatal mid-batch → no orphan compute.** `BudgetExceeded` and `CapabilityMismatch` paths destroy
   the instance before re-raise (existing orchestrator code). Batch propagates unchanged.
   `TeardownError` follows existing semantics: log ERROR, raise, sweeper picks up stragglers.
5. **CLI Ctrl-C / SIGTERM.** Python default behavior; `pool.close()` runs via `with` __exit__.
   In-flight HTTP requests in the engine layer aren't preempted (stdlib `urllib` limitation —
   documented, same as today's single-shot `generate`).
6. **Partial summary on fatal exit.** `_batch_summary.json` is written in the `finally` clause
   so even an exit-2 batch leaves a parseable record of what completed vs aborted.
7. **Same-second `batch_id` collision.** Checked via `store.list(batch_id)`: if the namespace
   already holds any keys, CLI exits 1 with `"batch_id collision: <id> exists (pass --batch-id
   to override)"`. Zero compute touched. **Limitation:** the check is best-effort against the
   store — two concurrent `kinoforge batch` invocations that pick the same `YYYYMMDD-HHMMSS`
   stamp at the same instant could both pass and then write into the same namespace. Mitigated
   by passing explicit `--batch-id ID` for batch farms.

### Per-entry status taxonomy

| Status | Meaning |
|---|---|
| `OK` | Future resolved with Artifact; `uri` set |
| `FAIL` | Future raised a per-entry exception; `error` field set |
| `ABORTED` | Submitted but cancelled (queued, never started) due to batch-fatal in another entry |
| `INTERRUPTED` | Started but in-flight when batch-fatal hit; result discarded |

## 8. Testing

### Existing 708-test suite anchors the refactor

`deploy_session` is a pure extraction. Acceptance criterion: zero existing tests modified. If a
`generate()` test breaks, the refactor is wrong.

### New test files

```
tests/core/test_deploy_session.py        ~6 tests
tests/core/test_batch_manifest.py        ~8 tests
tests/core/test_batch_generate.py        ~10 tests
tests/test_batch_cli.py                  ~5 tests
tests/test_examples.py                   +2 manifest-load tests
```

### `tests/core/test_deploy_session.py`

| # | Test | Bug it catches |
|---|---|---|
| 1 | `with deploy_session(cfg, ...) as s` round-trip: `s.pool` is `ConcurrentPool`; `s.backend` non-None | Refactor drops the pool wiring |
| 2 | __exit__ calls `pool.close()` exactly once even on exception inside with-block | ThreadPoolExecutor leak |
| 3 | __exit__ does NOT call `provider.destroy_instance` on clean exit | Refactor kills warm instance → breaks warm reuse |
| 4 | CapabilityMismatch inside with-block: instance destroyed before exception re-raises | Verify-fail orphan compute |
| 5 | Hosted path (`requires_compute=False`): `session.instance is None`, no `create_instance` called | Hosted regresses |
| 6 | Profile cache hit: discover never called; backend constructed exactly once | Backend double-construction |

### `tests/core/test_batch_manifest.py`

| # | Test | Bug it catches |
|---|---|---|
| 1 | YAML list of 3 entries loads → 3 `BatchEntry`s | Parser drops entries silently |
| 2 | `prompt` + `prompt_file` both set on one entry → `ValidationError` with helpful message | Ambiguous prompt source |
| 3 | Neither set → `ValidationError` | Empty entry slips through |
| 4 | `prompt_file` resolved relative to manifest's dir, not CWD | Path-portability breaks |
| 5 | `prompt_file` not found → `ConfigError`, file path in message | Silent fallback to empty prompt |
| 6 | `prompt_file` content stripped of trailing newline (multi-line preserved internally) | Trailing `\n` poisons engine prompt-length validators |
| 7 | Duplicate explicit `run_id` → `ValidationError` listing the dupes | Two entries collide in store namespace |
| 8 | `extra="forbid"`: per-entry `engine: foo` → `ValidationError` | Per-entry engine override silently ignored |

### `tests/core/test_batch_generate.py`

All tests use `FakeEngine` + `LocalProvider` + `FakeClock`. Zero network/subprocess.

| # | Test | Bug it catches |
|---|---|---|
| 1 | 3 entries, all OK → `BatchResult` with 3 outcomes, all `status="ok"`, distinct `uri`s | Result collection swaps/drops outcomes |
| 2 | Entry 2 raises `AssetFetchError` → outcomes 1,3 `ok`, outcome 2 `fail`; batch returns | Per-entry failure aborts wrongly |
| 3 | Entry 2 raises `BudgetExceeded` → batch re-raises; summary JSON written before raise | Fatal mid-batch loses partial results |
| 4 | `cfg.params={"seed": 1}` + entry has `params={"seed": 42}` → that entry's stage gets `seed=42`; others get `seed=1` | Merge mutates shared cfg.params |
| 5 | 3 entries × `cfg.params={"seed": 1}` + 1 entry override → other 2 still see seed=1 | Shallow-copy bug between entries |
| 6 | `concurrent=2`: with 3 entries, max 2 in flight observed via spy | Concurrency knob ignored |
| 7 | Discovery runs exactly once on cold cache (`backend.inspect_capabilities` count == 1) | Per-entry rediscovery |
| 8 | Warm cache: `discover` never called; `verify` called exactly once | Verify-per-entry waste |
| 9 | `validate_request` runs per entry (count == N_entries, via spy) | Skipped validation lets bad entries through |
| 10 | Each entry's artifact lands at `<root>/<batch_id>/<entry.run_id>/<filename>` (paths distinct) | Namespace collision |

### `tests/test_batch_cli.py`

| # | Test | Bug it catches |
|---|---|---|
| 1 | `kinoforge batch -c cfg.yaml --manifest m.yaml` on local-fake cfg → exit 0, summary printed, 3 artifacts on disk | CLI wiring busted |
| 2 | Missing `--manifest` → exit 1 with argparse error to stderr | Required-flag regression |
| 3 | `--batch-id existing` (directory pre-created) → exit 1, message names the path, no compute touched | Collision protection regressed |
| 4 | `--concurrent 0` → exit 1 with clear message | Off-by-one accepted |
| 5 | One entry's `mode` is bogus → exit 1 (entry-level fail), other 2 succeed, summary correct | Continue-on-error broken at CLI surface |

### `tests/test_examples.py` additions

| # | Test | Bug it catches |
|---|---|---|
| 1 | `examples/configs/batch-prompts.yaml` loads via `load_manifest` | Example manifest rots |
| 2 | Every entry in example manifest sets `mode` ∈ `{"t2v","i2v","flf2v"}` | Bogus example modes |

### Offline guarantee

All new tests obey the existing project invariant: no real cloud, no real network, no real GPU,
no real weights. Verified by `tests/test_core_invariant.py` — its subprocess-isolation test
catches any accidental adapter import inside `core/batch.py`.

### Test totals

~31 new tests. Post-Layer-L count: **708 + 31 = 739 tests** (give or take, per refinement).

## 9. Acceptance criteria

Each is a failing test before any implementation:

1. `with deploy_session(cfg, ...) as s` yields a working pool+backend+profile; all 708 existing
   `generate()` tests still pass after `generate()` is rewritten on top of `deploy_session`.
2. `load_manifest("prompts.yaml")` parses a valid 3-entry YAML, validates `extra="forbid"`,
   validates exactly-one-of `prompt`/`prompt_file`, reads `prompt_file` content, auto-indexes
   missing `run_id`s.
3. `load_manifest` raises `ConfigError` with the file path in the message when a `prompt_file`
   doesn't exist.
4. `batch_generate(cfg, manifest, store=store, batch_id="b")` with 3 entries on a local-fake cfg
   produces 3 artifacts at `<store>/b/<run_id>/<filename>` and returns a `BatchResult` with 3
   `status="ok"` outcomes.
5. One entry raising `AssetFetchError` does not abort the batch; the other entries complete; the
   summary records 1 fail + the rest ok.
6. One entry raising `BudgetExceeded` aborts the batch; `_batch_summary.json` is written before the
   exception propagates; CLI exits 2.
7. Per-entry `params` / `spec` merges are shallow and do NOT mutate `cfg.params` / `cfg.spec` or
   leak across entries.
8. `kinoforge batch -c cfg.yaml --manifest m.yaml` on local-fake cfg exits 0; the streaming log +
   final summary table appear on stdout.
9. Same-second `batch_id` collision → exit 1, no compute touched, clear error message.
10. `tests/test_core_invariant.py` still passes — `core/batch.py` introduces no adapter import.

## 10. Out of scope (deferred)

- Cross-engine batch (different engines per entry).
- Retries on per-entry failure (user/LLM decides).
- Per-entry budget envelopes.
- Background daemon / cron scheduler.
- Progress bar / TUI (streaming text log is the only output today).
- Multi-batch parallelism (one process = one batch).

## 11. Open follow-ups (post-Layer-L)

- `_batch_summary.json` schema versioning if downstream tooling consumes it.
- `--retry-failed PATH` flag that reads a prior summary JSON and reruns the `fail` entries on a
  fresh batch — natural follow-on but YAGNI until asked.
- ConcurrentPool's existing `map()` could absorb continue-on-error semantics behind a kwarg; today
  we route around it. Worth reconsidering when a second consumer wants continue-on-error.

## 12. Dependencies

- No new runtime dependencies. PyYAML, pydantic v2 already present.
- No new dev dependencies.
- `concurrent.futures.ThreadPoolExecutor` (stdlib) used inside `batch_generate` for as_completed
  iteration distinct from `ConcurrentPool`'s per-backend slot pool.
