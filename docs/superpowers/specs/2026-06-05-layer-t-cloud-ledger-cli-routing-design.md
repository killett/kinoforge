# Layer T — cloud-ledger CLI routing (design spec)

**Status:** approved 2026-06-05
**Authors:** Emmy Killett, Claude (Opus 4.7)
**Closes carry-forward:** PROGRESS:127 — `cli._ledger(state_dir)` always constructs a `LocalArtifactStore`, even when `store.kind` is `s3`/`gcs`.
**Prereq layers shipped:** H (`acquire_lock` ABC on all 3 stores), M (Authorization-header passthrough — unrelated; mentioned only to clarify what is NOT relied on).

---

## 1. Problem

Layer C (Phase 13) gave kinoforge first-class S3/GCS artifact stores. Layer H (Phase 18) gave every store an `acquire_lock` ABC so cross-process mutual exclusion on ledger writes is safe over the network. But `src/kinoforge/cli.py:54-64` still hardcodes a `LocalArtifactStore(state_dir)` for the ledger, even when `cfg.store.kind` is `s3` or `gcs`. Consequence: artifacts go to the bucket; the ledger that names instance IDs, lifecycle policy, and tags stays on whichever host ran `kinoforge deploy`. Multi-node coordination (the headline win of Layer H) is unreachable from the CLI.

A second smell sits behind this: `Ledger._compute_uri` in `src/kinoforge/core/lifecycle.py:399-415` switches on `isinstance(LocalArtifactStore)` and raises `TypeError` otherwise. The `store.uri_for` ABC (Phase 11 / Layer A) makes that switch redundant.

Layer T routes the ledger through the configured store, eliminates the `isinstance` switch, and lays a `SessionContext` foundation that future layers (streaming logs, spend cap, daemon mode, multi-tenant profiles) can extend by adding fields rather than threading new params through nine subcommand signatures.

## 2. Goals

1. `Ledger` constructed by the CLI uses `cfg.store` when a config is loaded, falling back to a sidecar pointer recorded in `state_dir` for no-config commands.
2. No silent split-brain: switching configs on the same `state_dir` is a hard error; switching from local to cloud while in-flight pods exist locally is a hard error.
3. The always-on instance overview degrades gracefully when cloud credentials are unavailable (warning header, no exit).
4. `cli.py` (currently 1000+ LOC) splits into a `cli/` package with one module per concern.
5. Foundation for future CLI-layer work: a single `SessionContext` object threaded through every subcommand.

## 3. Non-goals

- Real-cloud verification of S3/GCS round-trips (gated by separate Layer N-style smoke spec).
- Cross-machine sidecar bootstrap (operator runs the same `kinoforge.yaml` on every host; `--store-uri` flag is a Layer T+1 candidate, additive, non-breaking).
- Live-spend smoke (no live cloud during Layer T).
- Ledger schema migration tooling (`SidecarMigrationBlocked` is the safety net, not a migration command).
- Changing any store's `acquire_lock` / `uri_for` ABC.

## 4. Approach

Approach **D** — single `SessionContext` built once in `main()`, threaded through every subcommand, with a JSON sidecar in `state_dir` that records which store backs the ledger.

Three approaches were considered and rejected:

| Approach | Reject reason |
|---|---|
| A — thread `cfg` through 9 fns | repeats per-layer pain; no foundation for future state |
| B — pre-context refactor of `cli.py` only | identical surface change without packaging the lessons into a reusable type |
| C — encode store config in directory names (`state_dir/s3-<bucket>/`) | mangles bucket names into paths; flat layout is simpler |

D's foundation payoff is articulated in §11.

## 5. Operator-facing semantics

| Command class | `--config` required today | `--config` required under Layer T |
|---|---|---|
| `deploy`, `provision`, `generate`, `gc`, `batch` | yes | yes (unchanged) |
| `status` | optional | optional (unchanged) |
| `list`, `stop`, `destroy`, `forget`, `reap` | no | **no** — they discover the store via the sidecar in `state_dir` |

Workflow:

1. First cfg-bearing command on a fresh `state_dir` writes `state_dir/store.json` (the sidecar) describing `cfg.store`.
2. Subsequent commands — cfg-bearing or not — match against the sidecar:
   - **Match** → no-op, dispatch continues.
   - **Mismatch** → hard error, `error: cfg.store ({...}) differs from sidecar ({...}); remove <path> or revert cfg.store to switch`, exit 1.
   - **No sidecar + cloud cfg + non-empty local ledger** → hard error, `error: refusing to switch to cloud store (s3) while local ledger has entries; run kinoforge destroy on each local-tracked instance, then re-run`, exit 1.
3. No-config commands read the sidecar and construct the matching store; absent sidecar → falls back to `LocalArtifactStore(state_dir)` (current behaviour preserved for non-cloud operators).

Backward compatibility:
- Any operator never touching cloud sees zero behavioural change.
- Anyone with cloud `cfg.store.kind` for artifacts but expecting the ledger to remain local: this is a deliberate break. Layer T's whole purpose is to unify them. Documented in README + CHANGELOG.

## 6. Architecture

Three new modules, one refactor, zero new runtime dependencies.

```
src/kinoforge/
  cli/                       (NEW package — was cli.py monolith)
    __init__.py              re-exports main()
    _main.py                 _build_parser, main(), dispatch table
    _commands.py             _cmd_deploy, _cmd_generate, ..., _build_store, _build_sink
    context.py               SessionContext + _build_store_from_sidecar
    sidecar.py               SidecarRecord + verify_or_write_sidecar
  core/
    errors.py                + SidecarMismatch, SidecarMigrationBlocked
    lifecycle.py             Ledger._compute_uri refactor
```

Entry point in `pyproject.toml` (`[project.scripts] kinoforge = "kinoforge.cli:main"`) keeps resolving to `kinoforge.cli.__init__.main`.

## 7. Module contracts

### 7.1 `cli/sidecar.py`

```python
SIDECAR_NAME = "store.json"
LEDGER_RUN_ID = "_lifecycle"
LEDGER_NAME = "ledger.json"


class SidecarRecord(BaseModel):
    """Frozen mirror of StoreConfig's identity fields."""
    model_config = {"frozen": True, "extra": "forbid"}
    kind: str
    bucket: str | None = None
    prefix: str = ""
    root: str | None = None

    @classmethod
    def from_cfg(cls, cfg: Config) -> SidecarRecord: ...

    def differs_from(self, other: SidecarRecord) -> bool: ...


def read_sidecar(state_dir: Path) -> SidecarRecord | None: ...
def write_sidecar(state_dir: Path, cfg: Config) -> None: ...
def verify_or_write_sidecar(state_dir: Path, cfg: Config) -> None:
    """Match-and-no-op, write fresh, or raise SidecarMismatch /
    SidecarMigrationBlocked."""
```

`SidecarRecord` is `extra="forbid"` so a future `StoreConfig` field that
sneaks past this layer's mirror fails the test in §9, not at runtime in
production.

`_local_ledger_nonempty(state_dir)` reads `state_dir/_lifecycle/ledger.json`
directly (no `LocalArtifactStore` construction) so the migration check is
cheap and side-effect-free even when the sidecar test path is exercised
during S3/GCS-bound runs.

### 7.2 `cli/context.py`

```python
@dataclass
class SessionContext:
    state_dir: Path
    cfg: Config | None
    sidecar: SidecarRecord | None
    clock: Clock = field(default_factory=RealClock)
    _store: ArtifactStore | None = None
    _ledger: Ledger | None = None

    @classmethod
    def from_args(
        cls, *, state_dir: Path, cfg_path: Path | None,
        clock: Clock | None = None,
    ) -> SessionContext:
        """Load cfg if path present, verify-or-write sidecar, snapshot sidecar."""

    def store(self) -> ArtifactStore:
        """Lazy build. Cached. Uses cfg.store > sidecar > LocalArtifactStore."""

    def ledger(self) -> Ledger:
        """Lazy build over self.store(). Cached. run_id=_lifecycle."""

    def ledger_safe(self) -> tuple[Ledger | None, str | None]:
        """Best-effort ledger for the always-on overview. Never raises."""
```

`store()` and `ledger()` are lazy so:
- `kinoforge --help` never constructs a store (zero cloud round trips on help).
- The overview path goes through `ledger_safe`, which catches store
  construction errors (expired creds, missing SDK) and returns `(None,
  reason)` for the warning header.
- Tests can construct a `SessionContext` directly with a fake store and skip
  the `from_args` path entirely.

`_build_store_from_sidecar(sc, state_dir)` lives in `context.py` (not
`_commands.py`) because the no-config command path goes through it and
should not depend on the subcommand module.

### 7.3 `core/lifecycle.py` Ledger refactor

```diff
 def _compute_uri(self) -> str:
-    from kinoforge.stores.local import LocalArtifactStore
-    if isinstance(self._store, LocalArtifactStore):
-        return str(self._store._path(self._run_id, self._LEDGER_NAME))
-    raise TypeError(  # pragma: no cover
-        f"Ledger._compute_uri: unsupported store type {type(self._store).__name__!r}"
-    )
+    return self._store.uri_for(self._run_id, self._LEDGER_NAME)
```

`LocalArtifactStore.uri_for(run_id, name)` returns the same absolute path
string `_path(run_id, name)` does — verified at `src/kinoforge/stores/local.py:63`.
This is a pure cleanup with no behavioural change for the Local path and
the only enabling change for S3/GCS.

### 7.4 `core/errors.py` additions

```python
class SidecarMismatch(KinoforgeError):
    """cfg.store differs from sidecar on disk."""


class SidecarMigrationBlocked(KinoforgeError):
    """First cloud-store cmd refused while local ledger non-empty."""
```

Both subclass `KinoforgeError` so the batch command's existing
`except KinoforgeError` Setup-fatal arm (`src/kinoforge/cli.py:527`)
catches them as exit 1 without touching `_cmd_batch`.

### 7.5 `cli/_commands.py` — signature migration

Every subcommand handler moves from `(args, state_dir)` to `(args, ctx)`:

| Old | New |
|---|---|
| `_ledger(state_dir)` | `ctx.ledger()` |
| `_build_store(cfg, state_dir)` | `ctx.store()` |
| `state_dir / "weights"` | `ctx.state_dir / "weights"` |

`_build_store` and `_build_sink` move with the subcommands (used internally
by `_cmd_*`). `SessionContext.store()` calls `_build_store(self.cfg, ...)`
under the hood when `cfg is not None`, so the two paths share the same
factory.

### 7.6 `cli/_main.py` — orchestration

```python
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir)
    load_env_file(Path(args.env_file) if args.env_file else None)

    cfg_path = Path(args.config) if getattr(args, "config", None) else None
    try:
        ctx = SessionContext.from_args(state_dir=state_dir, cfg_path=cfg_path)
    except (SidecarMismatch, SidecarMigrationBlocked) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except PydanticValidationError as exc:
        print(
            f"error: sidecar at {state_dir / 'store.json'} is unreadable: "
            f"{exc}; rm to reset",
            file=sys.stderr,
        )
        return 1

    _print_instance_overview(ctx)

    if args.cmd is None:
        parser.print_help()
        return 0
    return _DISPATCH[args.cmd](args, ctx)
```

`_DISPATCH: dict[str, Callable[[Namespace, SessionContext], int]]` is built
in `_main.py` so adding a subcommand is a single-line change.

## 8. Data flow scenarios

### 8.1 First cmd, no sidecar yet, cfg-bearing (`kinoforge deploy --config cloud.yaml`)

1. `load_env_file` populates `os.environ`.
2. `SessionContext.from_args(state_dir, cloud.yaml)`:
   1. `load_config(cloud.yaml)` → `cfg.store.kind="s3"`, `bucket="kf-prod"`.
   2. `verify_or_write_sidecar`: no existing sidecar; local ledger empty; writes `state_dir/store.json`.
   3. `read_sidecar` → snapshot for `ctx.sidecar`.
3. `_print_instance_overview(ctx)`: `ctx.ledger_safe()` constructs `S3ArtifactStore`, `Ledger.entries()` returns `[]` (clean S3), prints `No running instances.`.
4. `_cmd_deploy(args, ctx)`: `deploy(cfg)` returns instance; `ctx.ledger().record(...)` writes through S3 under `acquire_lock("ledger/_lifecycle", ttl_s=30)`.

### 8.2 Second cmd, no `--config` (`kinoforge list`)

1. `SessionContext.from_args(state_dir, cfg_path=None)`: skips `verify_or_write`; `read_sidecar` returns the S3 record.
2. Overview builds the S3 store via `_build_store_from_sidecar` and reads the ledger transparently.
3. `_cmd_list(args, ctx)` uses the cached `ctx.ledger()`.

### 8.3 Mismatch — `kinoforge deploy --config different-cloud.yaml`

`verify_or_write_sidecar` raises `SidecarMismatch`; caught in `main()`; prints `error: cfg.store ({...}) differs from sidecar ({...}); remove <path> or revert cfg.store to switch`; exits 1.

### 8.4 Migration block

First cloud-cfg cmd on a `state_dir` whose `_lifecycle/ledger.json` has entries: `_local_ledger_nonempty → True`, raises `SidecarMigrationBlocked`; prints `error: refusing to switch to cloud store (s3) while local ledger has entries; ...`; exits 1.

### 8.5 Broken creds during overview

`ctx.ledger_safe()` catches `boto3 ClientError("ExpiredToken")`, returns `(None, "ClientError: ExpiredToken")`. Overview prints `[instance overview] unavailable: ClientError: ExpiredToken`. Subcommand dispatches. If the subcommand also calls `ctx.ledger()`, the same error re-raises and the per-cmd handler maps it to exit 1.

### 8.6 Cross-machine gap (documented safety risk)

Host B with a fresh `state_dir` and no sidecar sees `LocalArtifactStore` and an empty ledger. Two consequences:

1. **UX:** `kinoforge list` on Host B shows no instances even when Host A has running pods routed to the same S3 bucket.
2. **Safety:** `_cmd_deploy`'s duplicate-instance guard (`cli.py:298-307`) checks `ledger.entries()` for a `kinoforge_key` match. On Host B that ledger is the empty local fallback, so the guard does not fire and a duplicate pod can be created.

Mitigation under Layer T: documented as a hard constraint — **first command on every new host must be cfg-bearing** (e.g. `kinoforge deploy --dry-run --config kinoforge.yaml`). This writes the sidecar before any state-mutating command runs. README adds an explicit "Multi-host setup" section calling out the constraint and the safety implication.

Layer T+1 candidate: `--store-uri s3://kf-prod` global flag or `KINOFORGE_STORE_URI` env var that lets non-cfg commands bootstrap their sidecar in memory. Non-breaking, additive — `SessionContext.from_args` learns one new source.

## 9. Error handling matrix

| Condition | Raised by | Caught at | Exit | Stderr |
|---|---|---|---|---|
| `cfg.store` ≠ sidecar | `verify_or_write_sidecar` | `main()` | 1 | `error: cfg.store ({...}) differs from sidecar ({...}); ...` |
| First cloud cmd + local ledger non-empty | `verify_or_write_sidecar` | `main()` | 1 | `error: refusing to switch to cloud store (s3) while local ledger has entries; ...` |
| Sidecar JSON corrupt or extra field | `pydantic.ValidationError` from `read_sidecar` | `main()` | 1 | `error: sidecar at <path> is unreadable: <reason>; rm to reset` |
| `cfg.store.kind` unknown | `UnknownAdapter` from `_build_store` | per-cmd handler | 1 | `error: unknown adapter — ...` |
| Sidecar `kind` unknown | `UnknownAdapter` from `_build_store_from_sidecar` | per-cmd handler | 1 | `error: unknown adapter — unknown sidecar kind: ...` |
| Cloud auth fail during overview | SDK ClientError | `ledger_safe()` | continues | `[instance overview] unavailable: <type>: <msg>` |
| Cloud auth fail during subcommand | re-raised on first `ctx.ledger()` | per-cmd handler | 1 | `error: <type>: <msg>` |
| Lock contention timeout | `LockTimeout` from `acquire_lock` | `_cmd_batch`'s existing `except KinoforgeError` arm (`cli.py:527`); other handlers let it propagate as bare traceback | 1 (batch) / 2 (others) | `error: KinoforgeError: ledger lock contended after N retries` |

Lock-contention surfacing in non-batch handlers is a pre-existing gap inherited from Phase 18 (`LockTimeout` is a `KinoforgeError` but only `_cmd_batch` has the catch arm). Layer T does not extend the catch sites — that is a separate polish layer if real contention starts being seen in practice.

## 10. Test plan

All tests offline (Phase 13 fakes for S3/GCS, Phase 18 lock fakes for cross-process serialisation, `pyfakefs` for tmp dirs where applicable).

**New tests (≈50 net):**

- `tests/cli/test_sidecar.py` (~15) — pure module: read/write/missing/corrupt/extra-field/migration-block/mismatch/parametrized-field-mirror.
- `tests/cli/test_context.py` (~10) — factory paths, lazy build, identity-cached lookups, `ledger_safe` failure modes.
- `tests/cli/test_commands_routing.py` (~12) — every `_cmd_*` consumes `ctx.ledger()` / `ctx.store()` exclusively; monkeypatch `_ledger` and `_build_store` to explode and assert no call.
- `tests/cli/test_main_flow.py` (~12) — end-to-end through `cli.main([...])`: help-degradation, sidecar lifecycle, mismatch, migration, cred-failure paths, dry-run still writes sidecar.
- `tests/cli/test_multinode_lock.py` (~1) — subprocess test against `FakeS3Client` proving concurrent record calls serialise on `acquire_lock`.

**Modified tests:**

- `tests/core/test_lifecycle.py` (~4): drop the `TypeError` assertion; add `Ledger(store=fake_s3)` / `(store=fake_gcs)` round-trip tests.
- `tests/test_cli.py` (~30 sites): adapt to the `(args, ctx)` signature where direct-invocation tests probe handler internals; `_call(["cmd", ...], state_dir)` style tests are signature-agnostic and unchanged.

**Test count projection:** 1222 → ≈1272.

**Bug-catch lockdowns (per `test-design` discipline):**

- `SidecarRecord` field-mirror: parametrize against every `StoreConfig` identity field and fail when a future field is added but not mirrored (precedent: Phase 16 post-merge `484e368`).
- `Ledger._compute_uri` lock: assert the implementation calls `store.uri_for(run_id, name)` exactly once and returns its result unmodified — prevents reintroduction of the `isinstance` switch.
- `ctx.ledger()` identity-cache lock: two calls return the same instance — fails fast if a future edit drops the cache and triggers per-call store reconstruction.

## 11. Foundation payoff

Future layers extend `SessionContext` rather than threading new params:

| Future layer | Per-feature work under Layer T |
|---|---|
| Streaming batch logs (PROGRESS:325) | add `ctx.log_sink` field + wiring at one site |
| `last_heartbeat` persistence (PROGRESS:162 #7) | tweak `ctx.ledger()` factory; subcommands untouched |
| Per-session spend cap | add `ctx.budget: BudgetTracker` field |
| Daemon / long-running mode | swap `SessionContext` lifetime; no signature churn |
| Multi-tenant `--profile foo` | resolve in `SessionContext.from_args`; no per-cmd plumbing |
| `--store-uri` / `KINOFORGE_STORE_URI` (cross-machine gap) | populate `ctx.sidecar` from a non-file source; zero subcommand changes |

## 12. Known limitations

- **Cross-machine bootstrap:** sidecar lives per-state_dir, not in the bucket. Operators must ship the same config to every host or use the future `--store-uri` flag. Workaround documented in README.
- **Two concurrent cfg-bearing cmds on the same state_dir:** both observe `read_sidecar → None`, both write; last writer wins. Mitigated by operator discipline; harden later with a file-lock around `write_sidecar` if it becomes a real failure mode.
- **Stale sidecar referencing a deleted bucket:** surfaces as a clean SDK error in the overview and per-cmd handlers. Operator runs `rm state_dir/store.json` to reset. No auto-detection.
- **No real-cloud verification:** Layer T ships offline-tested only. PROGRESS:116 is the gate for real-cloud S3/GCS work.

## 13. Migration & rollout

- Single PR / `--no-ff` merge to `main` (precedent: every prior `Layer X` line in PROGRESS).
- Commit-per-task per CLAUDE.md durability rules.
- README additions:
  - `Cloud-backed ledger` section explaining the sidecar and the workflow.
  - `Multi-node coordination` section updated to point at the sidecar (currently points at Layer H lock alone).
  - `Multi-host setup` subsection covering the cross-machine gap (§8.6) and the "first command must be cfg-bearing" constraint.
  - `Breaking change` note: anyone who today uses cloud `cfg.store` but expected the ledger to stay local will see it move with the next deploy.

**Existing-operator migration steps** (cloud `cfg.store` today, local ledger today):

1. `kinoforge list` — inventory any in-flight instances tracked locally.
2. `kinoforge destroy --id <id>` for each (empties the local `_lifecycle/ledger.json`).
3. Upgrade to the Layer T release.
4. `kinoforge deploy --config cloud.yaml` — writes the sidecar, opens the cloud ledger fresh.

Operators who skip step 2 hit `SidecarMigrationBlocked` on step 4 with a stderr line that names the action to take.
- PROGRESS additions: Phase 34 section under "Post-MVP" with per-task SHAs.

## 14. Open questions

None. The four user-facing decisions in §5–§9 are locked:

1. Store discovery for no-config cmds → sidecar in `state_dir`.
2. Mismatch policy → hard error.
3. Migration policy → hard block when local ledger non-empty.
4. Overview-degradation policy → best-effort warning + continue.

## 15. Decision log

| # | Decision | Rationale |
|---|---|---|
| 1 | Sidecar in `state_dir/store.json` over global flag | No new flags; no breaking change for single-user CLI; survives across sessions. |
| 2 | Hard error on mismatch over silent overwrite | Mirrors `kinoforge gc --config` precedent; "explicit > silent" is the project's bias on operator-visible state changes. |
| 3 | Hard block on migration over auto-copy | Cross-store atomicity is non-trivial; partial migration leaves the operator with split state and no clean recovery path. |
| 4 | Best-effort overview over hard fail | Keeps `kinoforge --help` working during credential rotation; matches operator expectation that diagnostic output never blocks dispatch. |
| 5 | `SessionContext` over thread-cfg-through-9-fns | Single integration point for every future per-session field; one-time refactor cost vs. recurring per-layer cost. |
| 6 | `cli.py` → `cli/` package | The file is already ~1000 LOC; splitting now while the surface is small avoids paying it later when more layers land here. |
| 7 | `Ledger._compute_uri` refactor bundled with Layer T | Layer T is the first layer that actually exercises non-Local stores from the Ledger; the dead-code path becomes live the moment we route through cloud. Folding the refactor here keeps the per-layer story self-contained. |
