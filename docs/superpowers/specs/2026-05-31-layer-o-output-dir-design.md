# Layer O — User-facing output directory

**Status:** validated 2026-05-31, awaiting plan
**Branch:** `build/layer-o` off `main@454e514` (post-Layer-N merge)
**Closes:** Operator UX gap — final artifacts buried under `.kinoforge/<run_id>/<engine-derived-name>` with engine-derived filenames that mean nothing at a glance and collide silently when `--run-id` is omitted.
**Defers:** Hardlink optimization (zero-copy via `ArtifactStore.local_path_for`), cloud-native sinks (S3 mirror, webhook POST), filename template customization, migration of existing `.kinoforge/<run_id>/*.mp4` files — each their own future layer.

## Why this layer

Today every finished clip lands at `<state_dir>/<run_id>/<artifact.filename>` where:

- `state_dir` defaults to `.kinoforge/` — a dotfile not meant for human inspection.
- `run_id` defaults to the literal string `"run"`, so every CLI invocation that omits `--run-id` writes into the same `.kinoforge/run/` directory.
- `artifact.filename` is engine-derived: FakeEngine produces `fake_<sha256-of-prompt>.bin`; ComfyUI returns sequential `ComfyUI_00001_.mp4` resetting per server instance; fal returns nanoid+tmp suffixes like `n9TG4YoyIIkzR1rouhQCw_tmpykhkugmc.mp4`; hosted returns server-chosen job ids. None of these carry human-recognizable context.

Two concrete operator failures result:

1. **Findability.** After a productive afternoon of `kinoforge generate` invocations a user has a flat directory of `ComfyUI_00001_.mp4`, `ComfyUI_00001_.mp4`, `ComfyUI_00001_.mp4` — silently overwriting each other across runs — and no way to tell which prompt produced which clip without inspecting bytes.
2. **Persistence.** Without an explicit `--run-id`, the second `kinoforge generate` invocation deterministically overwrites the first's internal artifact + ledger entry. Recoverable only if the user happened to have moved the file out of `.kinoforge/run/` between invocations.

This layer adds a user-facing publish step that places every finished clip at a stable, predictable, sortable, human-readable path **without disturbing the existing `ArtifactStore` contract**. Internal state (profile cache, ledger, weights cache, intermediate artifacts) stays in `.kinoforge/` exactly as today; ledger/`uri_for`/`gc`/registry semantics are untouched. The `OutputSink` is opt-out (`output.enabled: false` or `--no-output-dir` restores today's behavior verbatim).

## Resolved questions (locked during brainstorm)

| Topic | Decision |
|---|---|
| **Scope** | Final per-clip Artifacts only. Profile cache, ledger, weights cache, intermediate segments all stay in `.kinoforge/`. |
| **Mechanism** | Publish step after `store.put_bytes`. ArtifactStore semantics fully preserved. Disk doubles for local store (acceptable for sub-GB videos). Hardlink optimization deferred. |
| **Slug rules** | ASCII-conservative: allow `[A-Za-z0-9._-]`, replace everything else with `-`, collapse runs, strip leading/trailing `-.`, truncate to 20 chars post-clean, fallback `"clip"` if empty. Cross-platform safe, shell-friendly, grep-friendly, no NFC/NFD weirdness, no terminal-display surprises. |
| **Layout** | Flat for single-clip runs (`output/<file>`); nested under `batch_id` for batch runs (`output/<batch_id>/<file>`). `run_id` is NOT part of the output path — stays a logical tag for store/ledger/uri_for. |
| **Default run_id collision fix** | CLI default `--run-id` changes from `"run"` to `f"run-{ts}"` (local TZ). Closes the silent-overwrite foot-gun on the internal store side too. Explicit `--run-id foo` invocations unaffected. |
| **Timestamp granularity** | `YYYYMMDD-HHMMSS` (local TZ per `feedback_local_timezone_only.md`). Collisions within the same second resolved via `_2`..`_99` suffix, then 6-char sha256 hash. |
| **Config surface** | YAML `output:` block (kind, dir, enabled) + CLI `--output-dir PATH` / `--no-output-dir`. Precedence: `--no-output-dir` > `--output-dir` > YAML > default `"output"`. |

## Architecture

### New seam: `OutputSink`

Parallel to `ArtifactStore` but with one job — publish a finished clip with a user-facing filename to a user-visible directory. Lives at `src/kinoforge/outputs/`, mirroring the `stores/` layout. Registered via the existing `register_*` / `get_*` pattern (proven in Phases 11/13/17), so a future `S3OutputSink` or `WebhookOutputSink` slots in by name.

```
src/kinoforge/outputs/
  __init__.py        OutputSink Protocol + register_sink/get_sink + self-register on import
  base.py            slugify() pure helper + format_filename() pure helper + OutputPublishError
  local.py           LocalOutputSink(dir, clock) — self-registers as "local"
```

`OutputSink` is **optional**, not required. Disabled → `GenerateClipStage` behaves bit-for-bit as today. Zero impact on any existing call path, ledger entry, or stored artifact.

### Sink Protocol

```python
class OutputSink(Protocol):
    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,                # ".mp4"
        namespace: str | None = None,  # batch_id or None
    ) -> str: ...                      # returns absolute path of published file
```

V1 takes `bytes` already in memory inside the stage (post `_artifact_bytes` resolution). Yes, this means local store + local sink writes the bytes twice. For a 3–100 MB mp4 the duplication is negligible; for a future hypothetical 5 GB clip an optimization is warranted. **Documented as a follow-up:** add `ArtifactStore.local_path_for(run_id, name) -> Path | None`, sink uses `os.link()` when both ends share a filesystem.

### Filename construction

```python
def slugify(prompt: str, max_chars: int = 20) -> str:
    """Conservative ASCII slug for filesystem-safe filenames.

    Pipeline:
      1. NFC normalize, then encode('ascii', 'ignore').decode() — drops emoji/CJK/accents.
      2. Replace any character not in [A-Za-z0-9._-] with '-'.
      3. Collapse runs of '-' to a single '-'.
      4. Strip leading/trailing '-' and '.'.
      5. Truncate to max_chars.
      6. Strip trailing '-' and '.' again (post-truncation).
      7. Return "clip" if empty.
    """
```

Full filename: `f"{ts}_{slug}{ext}"` where

- `ts = clock.now().strftime("%Y%m%d-%H%M%S")` — **local TZ** (per `feedback_local_timezone_only.md`).
- `slug = slugify(prompt, 20)`.
- `ext = pathlib.Path(artifact.filename).suffix or ".bin"`.

**Collision policy** (target path already exists):

1. Try `_2`, `_3`, … `_99` suffixes inserted before extension: `20260531-210015_Waves-crash-dusk_2.mp4`.
2. If 99 exhausted: 6-char `sha256(str(time.monotonic_ns())).hexdigest()[:6]` → `20260531-210015_Waves-crash-dusk_a3f1c8.mp4`.
3. Deterministic under `FakeClock` + injected monotonic seed in tests.

### Path layout

```
output/                                          # single-clip runs land flat
  20260531-210015_Waves-crash-dusk.mp4
  20260531-210132_Cliffs-at-noon.mp4

output/batch-20260531-204500/                    # batch runs nest under batch_id
  20260531-204512_Waves-crash-dusk.mp4
  20260531-204517_Forest-creek-at-d.mp4
```

`run_id` does not appear in the output path. It remains the namespacing key for `store.put_bytes` / `ledger.record` / `store.uri_for` / `kinoforge gc`. Batch's `batch_id` IS used — provides the meaningful grouping the operator asked for ("all videos from Tuesday's batch").

### Configuration

```yaml
# Optional output: block — absent block uses defaults below.
output:
  kind: local            # default; future: s3, webhook
  dir: output            # default; relative to cwd, or absolute
  enabled: true          # opt-out flag; false = today's behavior (store-only)
```

CLI flags on `generate` and `batch`:

- `--output-dir PATH` — overrides YAML.
- `--no-output-dir` — disables publish for this invocation.

**Precedence:** `--no-output-dir` > `--output-dir` > `output.dir` (YAML) > default `"output"`.

`--no-output-dir` and `--output-dir` are mutually exclusive at the argparse level (`add_mutually_exclusive_group`).

### Default `--run-id` change (collision foot-gun fix)

`cli.py:164` changes from:

```python
p_generate.add_argument("--run-id", default="run", metavar="ID")
```

to:

```python
p_generate.add_argument("--run-id", default=None, metavar="ID")
# ... in _cmd_generate:
run_id = args.run_id or f"run-{clock.now().strftime('%Y%m%d-%H%M%S')}"
```

Explicit `--run-id foo` invocations unaffected. Default behavior changes: each invocation gets a fresh `run_id` under `.kinoforge/`, so internal artifacts + ledger entries no longer silently overwrite each other. Batch CLI unchanged (per-entry `run_id` already explicit in manifest).

This change is breaking in the narrow sense that scripts grepping for `.kinoforge/run/` no longer find anything. Documented in PROGRESS as a breaking change alongside the `kinoforge gc --config PATH` precedent from Layer C.

## Wiring touchpoints

| File | Change |
|---|---|
| `src/kinoforge/outputs/__init__.py` | NEW. `OutputSink` Protocol re-export + `register_sink` / `get_sink`. Self-imports `local` on first registry hit. |
| `src/kinoforge/outputs/base.py` | NEW. `slugify()` + `format_filename()` pure helpers + `OutputPublishError`. |
| `src/kinoforge/outputs/local.py` | NEW. `LocalOutputSink(dir: Path, clock: Clock, slugify_fn: Callable = slugify)`. Self-registers under `"local"`. |
| `src/kinoforge/core/config.py` | Add `OutputConfig` pydantic block on `Config` (default-factory pattern matching `StoreConfig` / `SplitterConfig`). Fields: `kind: Literal["local"]`, `dir: Path = Path("output")`, `enabled: bool = True`. |
| `src/kinoforge/cli.py` | (a) Change `--run-id` default to `None` + uniquify in `_cmd_generate`. (b) Add `--output-dir PATH` / `--no-output-dir` mutually exclusive group to `generate` and `batch` subparsers. (c) New `_build_sink(cfg, args) -> OutputSink \| None`. (d) Thread sink into `orchestrator.generate()` and `batch.batch_generate()`. |
| `src/kinoforge/core/orchestrator.py` | `generate()` gains `sink: OutputSink \| None = None` kwarg. Threads into `GenerateClipStage` constructor. None default preserves existing test signatures. |
| `src/kinoforge/pipeline/generate_clip.py` | Constructor gains `sink: OutputSink \| None = None` and `namespace: str \| None = None`. After `store.put_bytes(...)`, if `sink is not None`: `prompt = segment.prompt` (Segment dataclass guarantees this string field — `core/interfaces.py:234`); `ext = Path(artifact.filename).suffix or ".bin"`; `sink.publish(bytes, prompt=prompt, extension=ext, namespace=self._namespace)`. |
| `src/kinoforge/core/batch.py` | `batch_generate()` gains `sink: OutputSink \| None = None`. Passes `namespace=batch_id` into the per-entry `GenerateClipStage` construction. |
| `.gitignore` | Add `output/` at repo root. |
| `examples/configs/*.yaml` | Add commented `output:` block to each existing example (wan, diffusers, hosted, fal, local-fake, runpod-comfyui-wan). |
| `README.md` | New "Output directory" section before "Running tools". |
| `PROGRESS.md` | Phase 25 entry; breaking-changes note for `--run-id` default. |
| `tests/test_core_invariant.py` | Add `outputs/` to allowed side-tree allowlist; assert no `kinoforge.outputs.*` import within `core/`. |

## Data flow

Per-clip publish (after engine result + store write):

```
engine.result(job_id) → Artifact
        ↓
GenerateClipStage._artifact_bytes(Artifact) → bytes  (existing)
        ↓
store.put_bytes(run_id, artifact.filename, bytes) → store URI   (existing)
        ↓
sink.publish(bytes, prompt=segment.prompt, extension=ext, namespace=batch_id or None) → output_path
        ↓
return Artifact (unchanged signature; published path NOT folded into Artifact)
```

The published path is logged at INFO level so operators see it. It is **not** folded into the `Artifact` dataclass — keeping the ABC stable. A future layer can add an `Artifact.published_path: str | None` field if downstream consumers (CLI status, batch summary) need to reference it.

## Edge cases

| Case | Behavior |
|---|---|
| Empty prompt | slug → `"clip"` → `20260531-210015_clip.mp4` |
| Prompt = `"???!!!"` | After strip → empty → `"clip"` |
| Prompt > 20 chars | Truncate to 20; re-strip trailing `-` and `.` to avoid `Waves-crash-on-basal-.mp4` |
| Prompt contains NUL or `/` | Replaced with `-` in step 2 of slugify |
| Prompt = `"---"` | After strip → empty → `"clip"` |
| Prompt = `"   "` (whitespace) | Whitespace not in `[A-Za-z0-9._-]` → all become `-` → strip → empty → `"clip"` |
| Prompt with newlines / tabs | Replaced with `-` |
| Two clips, same second, same prompt prefix | `_2`, `_3`, … `_99` then 6-char sha256 of `time.monotonic_ns()` |
| Output dir missing | `mkdir(parents=True, exist_ok=True)` at publish time |
| Output dir read-only / disk full | `OSError` wrapped as `OutputPublishError`; raised; clip still safe in store |
| Multi-segment non-native | Each Artifact publishes independently with its own `clock.now()` at publish time |
| Cloud store + local output dir | Bytes already in memory after `_artifact_bytes` download; sink writes locally — works seamlessly |
| `--output-dir` relative path | Resolved against cwd at sink construction (`Path(arg).resolve()`) |
| `--output-dir` absolute path | Used as-is |
| `output.enabled: false` | `_build_sink` returns `None`; stage skips publish branch |
| Both `--output-dir` and `--no-output-dir` | argparse mutually-exclusive group → CLI error before any work |
| Engine returns artifact with no extension | `Path("name").suffix` → `""` → defaults to `".bin"` |
| Empty prompt string on segment (e.g. test paths) | `slugify("")` → `"clip"` per AC-1; published as `<ts>_clip<ext>` |
| Existing `.kinoforge/<run_id>/*.mp4` from old runs | NOT migrated; remain accessible via old paths; new runs use new layout |
| Symlink loop in output dir | `mkdir` raises `OSError` → wrapped as `OutputPublishError` |
| `--run-id` default still old in user shell scripts | Scripts grepping `.kinoforge/run/` find nothing; documented breaking change |
| Sink raises mid-batch | Per-entry try/except already exists in `batch_generate` continue-on-error policy; sink failure recorded as entry error; batch continues |

## Testing strategy

All offline, deterministic, follow `test-design` skill (behavior-under-test + concrete failing bug per test). No real FS beyond `tmp_path`. No real network/GPU/weights.

| File | Test count | Key behaviors |
|---|---|---|
| `tests/outputs/test_slugify.py` | ~12 | Empty → `"clip"`; emoji/CJK dropped; all-special → `"clip"`; ASCII length cap; leading/trailing `-.` stripped; NFC normalization; exactly-20-char input passes through; 21-char-into-20 truncation produces no trailing `-`; NUL + `/` replacement; multiple internal dashes collapsed; whitespace handling; newline handling. |
| `tests/outputs/test_local.py` | ~10 | Basic publish writes bytes; namespace nesting creates subdir; `mkdir(parents=True)` covers nested-missing; collision `_2` path returned; collision `_99` exhausted → hash path; `OSError` wrapped as `OutputPublishError`; absolute dir passes through; relative dir resolved against cwd; FakeClock determinism; extension preservation. |
| `tests/core/test_config.py` | +3 | `OutputConfig` absent block defaults; explicit block round-trips; `enabled: false` round-trips. |
| `tests/pipeline/test_generate_clip.py` | +3 | sink=None → no publish call; sink present → published with right args; multi-segment → publish called N times. |
| `tests/core/test_batch.py` | +2 | `namespace=batch_id` propagated to sink; per-entry sink calls. |
| `tests/cli/test_cli.py` | +5 | `--output-dir` overrides YAML; `--no-output-dir` disables; default = `"output"`; mutex group error when both flags passed; **`--run-id` default uniquifies** via FakeClock. |
| `tests/test_examples.py` | +6 | Each existing example YAML round-trips with commented `output:` block. |
| `tests/test_core_invariant.py` | +1 | `outputs/` added to allowlist; ban scan extended. |

**Lockdown invariants:**

- `slugify("a" * 1000)` → length == 20.
- `slugify("///!!!")` → `"clip"`.
- Timestamp formatter uses local TZ (assert by injecting `FakeClock` returning a fixed naive `datetime(2026, 5, 31, 21, 0, 15)`; assert exact `"20260531-210015"` string match).
- `--run-id` default uniquifies — locks in CLI argparse default `None` + `_cmd_generate` derivation logic (regression test prevents accidental revert to `"run"`).

Net test delta: ~42 new tests. Pre-Layer-O baseline `778 + 1 skipped` → projected `~820 + 1 skipped`.

## Build order (TDD red-first, one commit per task)

1. **Task 1 — slugify helper + OutputSink Protocol.** `outputs/base.py` + `outputs/__init__.py` + 12 slugify tests. Pure functions, no I/O, easy red-first.
2. **Task 2 — LocalOutputSink.** `outputs/local.py` + 10 sink tests; self-registers on import. Uses injected `Clock` for determinism.
3. **Task 3 — `OutputConfig` pydantic block.** `core/config.py` + 3 round-trip tests. Default-factory pattern matching existing `StoreConfig`.
4. **Task 4 — `GenerateClipStage` sink integration.** Constructor arg + publish call + 3 stage tests (FakeOutputSink).
5. **Task 5 — `core/batch.py` namespace wiring.** Threads `sink` through + 2 tests.
6. **Task 6 — `core/orchestrator.py` sink threading.** `generate()` gains kwarg + 2 tests.
7. **Task 7 — CLI flags + `_build_sink` + `--run-id` default change.** `cli.py` + 5 CLI tests. Two concerns bundled (both touch argparse on the same subparsers).
8. **Task 8 — `.gitignore` + example YAML updates.** Commented `output:` block on each + 6 round-trip tests.
9. **Task 9 — Invariant test extension + README "Output directory" section + PROGRESS Phase 25 entry.**
10. **Final gate.** `pixi run pre-commit run --all-files` + `pixi run test` (assert green; assert test count matches projection); `--no-ff` merge into `main`.

Acceptance criteria below are each written as a failing test first.

## Acceptance criteria

1. **AC-1 slugify contract.** `slugify("Waves crashing on basalt cliffs at dusk!")` returns `"Waves-crashing-on-ba"`. `slugify("🌊 ???")` returns `"clip"`. `slugify("a" * 1000)` returns a 20-char ASCII string with no trailing `-.`.
2. **AC-2 local publish layout.** `LocalOutputSink(tmp_path, FakeClock(at=2026-05-31T21:00:15)).publish(b"data", prompt="Waves crashing", extension=".mp4")` writes `tmp_path / "20260531-210015_Waves-crashing.mp4"` and returns its absolute string.
3. **AC-3 namespace nesting.** Same call with `namespace="batch-20260531-204500"` writes to `tmp_path / "batch-20260531-204500" / "20260531-210015_Waves-crashing.mp4"`.
4. **AC-4 collision suffix.** Two consecutive publishes with the same prompt and same FakeClock time produce `<...>.mp4` and `<...>_2.mp4`.
5. **AC-5 stage publish call.** `GenerateClipStage(sink=spy_sink).run(...)` calls `spy_sink.publish` exactly once per Artifact returned by engine, with `prompt` matching `segment.prompt` and `extension` matching `Path(artifact.filename).suffix`.
6. **AC-6 stage no-publish when sink=None.** `GenerateClipStage(sink=None).run(...)` produces zero `publish` calls and identical Artifact stream — exact byte-for-byte compatibility with today's behavior.
7. **AC-7 batch namespace propagation.** `batch_generate(batch_id="batch-X", sink=spy_sink, ...)` calls `spy_sink.publish` with `namespace="batch-X"` for every entry's Artifact.
8. **AC-8 CLI precedence.** `kinoforge generate -c cfg.yaml --output-dir /tmp/foo` overrides `output.dir` from YAML. `kinoforge generate -c cfg.yaml --no-output-dir` results in `sink=None`. Both flags together → argparse error.
9. **AC-9 default `--run-id` uniquifies.** Two CLI invocations with no `--run-id` produce two distinct run_id strings (locked via FakeClock); ledger entries do not overwrite.
10. **AC-10 `output.enabled: false` honored.** YAML `output: {enabled: false}` produces `sink=None`; CLI behaves as today.
11. **AC-11 OSError wrap.** `LocalOutputSink` against a read-only tmp_path raises `OutputPublishError` (not raw `OSError`); store artifact remains intact.
12. **AC-12 invariant preserved.** `tests/test_core_invariant.py` still passes — no `kinoforge.outputs.*` import found within `core/`; `outputs/` added to allowlist.
13. **AC-13 `.gitignore` line present.** `output/` line exists in `.gitignore` and `git status` ignores newly created `output/<file>` after a generate run.

## Out of scope (Layer P+ candidates)

- **Hardlink optimization** — zero-copy via `ArtifactStore.local_path_for(run_id, name) -> Path | None`; sink uses `os.link()` when both ends are on the same filesystem. Skipped because v1 disk doubling is negligible for sub-GB clips.
- **Cloud-native sinks** — `S3OutputSink`, `GCSOutputSink`, `WebhookOutputSink`. Architecture already supports them via `register_sink`; just no impl in Layer O.
- **Filename template customization** — `output.filename_template: "{ts}_{slug}{ext}"` with Jinja-style placeholders. Hard-coded in v1.
- **Migration of existing `.kinoforge/<run_id>/*.mp4`** files into the new `output/` layout. Old runs stay where they are; new runs use new layout.
- **Streaming progress per published file in `batch`** — already deferred from Layer L Task 4.
- **`Artifact.published_path` field** — extend the dataclass once a downstream consumer (CLI status, batch summary table) wants to reference the published path. For now the path is logged at INFO level.
- **Engine integration on RunPod** (the original Layer-O candidate per PROGRESS:154) — moves to Layer P or later. This layer reclaims the Layer-O slot for output-dir UX, which is a more pressing operator pain point.

## Deviations from current patterns / conventions

None. The `OutputSink` ABC + registry pattern parallels `ArtifactStore` exactly. Injected `Clock` follows the precedent set in Phase 9 / `core/clock.py`. Pure-function slug helper follows the precedent set in `core/strategy.py` / `core/prompt_routing.py` / `core/assets.py`. Test layout matches `tests/stores/` and `tests/pipeline/`. PROGRESS:87 "Injected I/O seams on every adapter" applies to `LocalOutputSink` (constructor takes `clock`, FS root path).

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `--run-id` default change breaks user shell scripts that grep `.kinoforge/run/` | Low | Documented in PROGRESS breaking-changes section; `--run-id run` restores prior behavior verbatim. |
| Slug collisions across rapid-fire generates with same prompt | Possible | `_2..._99` then sha256 hash; covered by AC-4. |
| Disk doubling concerns for large clips | Possible at multi-GB scale | Documented follow-up (hardlink optimization); v1 acceptable for sub-GB. |
| `output/` accidentally committed to git | Low (gitignore added) | AC-13 lockdown; `output/` line in `.gitignore` mandatory. |
| OSError mid-publish leaves partial file | Possible | `LocalOutputSink` writes to `<path>.tmp` first then `os.replace(tmp, final)` for atomicity. Documented in `local.py`. |
| Sink invoked on FakeEngine paths during smoke / e2e tests | Expected | Tests already construct sinks explicitly via FakeOutputSink; `examples/configs/local-fake.yaml` includes commented `output:` block for parity. |
