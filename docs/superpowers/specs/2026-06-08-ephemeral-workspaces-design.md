# Prompt + LoRA confidentiality (vault + `--ephemeral`) — design spec

**Date:** 2026-06-08
**Author:** brainstorm session (Dr. Twinklebrane + Claude)
**Issue:** none yet (recommend opening one before plan phase)
**Status:** validated, awaiting user spec review before plan phase

**Changelog:**
- **2026-06-10:** Revised §10.4 + §11.5 + Appendix A: `EphemeralSession.__exit__` no longer destroys the compute instance. Original wording would have forced cold-boot on every `--ephemeral` run, defeating the upcoming warm-reuse roadmap (PROGRESS B5→B3). Pod lifecycle governed by selfterm / sweeper / budget tracker as in any non-ephemeral run.

---

## 1. Motivation

Kinoforge's Phase 14 (`.env` secrets loader) closed the API-key surface: tokens
like `CIVITAI_TOKEN`, `HF_TOKEN`, `RUNPOD_API_KEY`, `REPLICATE_API_TOKEN`,
`RUNWAYML_API_SECRET` are kept out of YAML, out of git, out of shell history.

The **content** of generations — positive prompts, negative prompts, LoRA
references, LoRA filenames, derived hashes — is a separate axis with its own
disclosure surface:

| Surface | Today |
|---|---|
| YAML configs in repo | `examples/configs/*.yaml` carries inline prompts (Phase 43 comparison smokes) |
| `prompt-field-realistic.txt` / `prompt-field-dreamlike.txt` | Committed at repo root as the canonical standard test prompts |
| Local ArtifactStore (`<state_dir>/<run_id>/`) | Intermediate clips, profile cache JSON, ledger JSON |
| `_batch_summary.json` (Phase 22) | Per-entry results including (presumably) output URIs |
| Profile cache (Phase 1 Task 12) | Keyed by `CapabilityKey.derive()` hash; hash is a fingerprint of secret material |
| Live-smoke fixtures (`tests/engines/fixtures/`) | Captured under `KINOFORGE_SAVE_FIXTURES=1` from real runs |
| stdout / stderr structured logs | `urllib3`/`runpod`/engine adapters emit refs + occasionally prompt bodies |
| Hosted provider servers (Replicate, Runway, fal, Luma) | POST bodies live on their dashboards + internal logs |

This spec adds an **always-on** content-confidentiality policy and an
**`--ephemeral`** mode that additionally deletes any record of the run from
both local disk and (best-effort) provider-side dashboards. The output
directory is the sole exempt zone: anything that lands there is preserved
unconditionally, in both modes.

---

## 2. Scope

### In scope

- **Vault file** outside the repo carrying positive prompt, negative prompt,
  ordered LoRA refs, optional explicit alias. Loaded once at CLI entry.
- **`RedactionRegistry`** singleton + **`RedactingLogFilter`** installed on the
  root `kinoforge` logger. Substring substitution at log and JSON-write seams.
- **Opaque vault-side alias** as the profile cache key (replacing
  `CapabilityKey.derive()` hash on disk for vault-driven runs).
- **Opaque ArtifactStore filenames** (sha256-derived) at every `put_bytes`
  site. The user-configured output directory keeps its permissive
  `{ts}_{provider}_{model}_{prompt20}.{ext}` schema.
- **`EphemeralSession`** context manager + **`EphemeralPolicy`** + CLI
  `--ephemeral` flag + pre-flight capability gate.
- **Hosted provider delete-on-completion** for Replicate (DELETE
  `/v1/predictions/{id}`) and Runway (DELETE `/v1/tasks/{id}`); refusal at
  pre-flight for fal (no public delete endpoint) and Luma (direct API retired
  per Phase 44).
- **`ArtifactStore.delete_run(run_id)`** + **`manual_cleanup_command(run_id)`**
  on every store implementation.
- **CI invariant test** (`tests/test_no_unredacted_writes.py`) modeled on the
  existing `tests/test_core_invariant.py` pattern.

### Out of scope (this spec)

- Vault encryption at rest (chmod 600 is the boundary).
- Multi-vault composition / inheritance.
- Vault validation by hitting CivitAI/HF online.
- Keyring / OS credential-store integration for the vault.
- Provider-internal log retention coverage (Replicate/Runway/RunPod internal
  logs remain out of reach; documented limit).
- Git history rewrite for the existing `prompt-field-*.txt` / inline-prompt
  YAMLs (the canonical comparison material stays per §4 D6).
- `Secret[str]` newtype across the SPEC ABCs (architecture rejected per §4 D9).
- Per-segment LoRA stacks.
- Encrypted profile cache (opaque alias supersedes).
- `hooks.post_generate` (forward-compat contract spelled out instead).
- RunPod billing-log scrub.
- Auto-redact of output directory contents.
- Cost sidecar implementation (Layer 5 candidate; gate pre-wired only).

### Public-by-design (deliberately stays unchanged)

- `prompt-field-realistic.txt` + `prompt-field-dreamlike.txt` at repo root.
- Inline prompts in `examples/configs/*.yaml`.
- Existing live-smoke fixtures captured against the standard test prompt.

---

## 3. Threat model

| Adversary | Reach today | Reach after this spec |
|---|---|---|
| Read-only access to user's local disk | Sees every prompt, LoRA ref, profile cache, ledger, batch summary, every fixture | Sees vault file (if present, user-managed); on-disk artifact filenames opaque; logs/JSON sinks redact; nothing in `<state_dir>` outside vault links back to refs |
| Read access to user's CI logs / shell scrollback | Prompts appear in log lines, stdout success messages, error tracebacks | Redacted at source by `RedactingLogFilter`; output filenames registered at publish time so downstream surfaces show `<output:hash6>` |
| Read access to user's git remote | Sees committed YAMLs, prompt-field-*.txt | Same — public-by-design content stays; the **user's private** prompts live in vault outside repo, never committed |
| Hosted provider operator (Replicate, Runway, fal, Luma server-side) | Sees full POST bodies in their logs | Same on internal logs (out of reach); on `--ephemeral`, the prediction/task record is deleted from the public dashboard via DELETE endpoint |
| Hostile developer with PR access | Could add a new write site that logs a raw prompt | Caught by `test_no_unredacted_writes.py` AC1/AC2/AC3 at merge time |

---

## 4. Decisions locked during brainstorm

| # | Decision | Value | Why |
|---|---|---|---|
| D1 | Online scope | Moderate — hosted engines allowed; send only the prompt body; delete prediction records on completion best-effort; provider-internal logs out of scope (documented). | Hosted Bearer engines (Replicate, Runway) are too useful to forbid; deletion endpoint covers the dashboard surface. |
| D2 | Mode framing | New policy is **always-on** when vault loaded; `--ephemeral` adds: hosted-side delete + refuses any local trace except output-dir files + memory-only run_id + forces `--debug-show-secrets` off. | "Always-on + flag-adds-strict" is more defensible than "flag-only," and matches how the user thinks about it. |
| D3 | Input surface | Vault file outside the repo. `--vault PATH` / `KINOFORGE_VAULT`. Path validated to NOT be under the active git repo root. | Matches `.env` ergonomics; explicit boundary between repo content and private content. |
| D4 | LoRA-name scope | Sensitive: ref string + downloaded filename + display label + derived hashes. All four. | User's most defensive call; drives the profile-cache opaque-alias decision. |
| D5 | Profile cache | Keyed by **opaque vault-side alias** (`cfg-<sha256[:12]>`). Adversary reading `profiles/cfg-….json` sees capability data only, no link back to refs. | Hash-key would itself be sensitive (D4); ephemeralizing the cache would defeat its purpose. |
| D6 | Historical/comparison prompts in repo | `prompt-field-*.txt` and `examples/configs/*` inline prompts stay public-by-design. | Comparison material — load-bearing for standardized live smokes. |
| D7 | Output filename | Schema stays `{ts}_{provider}_{model}_{prompt20}.{ext}`; permissive — any sensitive material MAY appear in filenames within the user-configured output dir; nothing else exempt. | Filename is the one allowed disclosure surface; policy must not forbid what the user explicitly allows. |
| D8 | Delete failure in `--ephemeral` | Hard fail with 3-retry exponential backoff; on terminal failure, exit non-zero with one-line copy-paste cleanup command (curl for hosted, `rm -rf` for local). | Strong contract: "ephemeral either fully scrubs or tells you it didn't." |
| D9 | Log redaction | At source via `RedactingLogFilter` on the root logger; `--debug-show-secrets` opt-in bypass for logs only (NOT for on-disk writes); rejected under `--ephemeral`. | Catches all log emissions including third-party libs (urllib3, runpod-python). |
| D10 | Architecture | C + CI invariant test (vault + secrets registry + EphemeralSession + AST-scan write-site invariant). Not B (`Secret[str]` in ABCs). | C matches kinoforge's existing "small invariants enforced at known seams" idiom (`test_core_invariant.py`); B's ABC churn isn't worth the marginal type-safety gain once `.reveal()` at the engine boundary is accounted for. |
| D11 | Vault modes | v1 ships both single-string (`positive_prompt: \|`) and explicit-segments (`segments: [...]`); pydantic enforces exactly-one-of. | Single-string flows into existing `HeuristicSplitter` (Phase 10); explicit-segments uses `GenerateClipStage.segments_override` (Phase 15). |
| D12 | Output dir = sole exempt zone | Anything in the user-configured output dir is preserved unconditionally — final mp4, keyframe images (Phase 43 Task 8), future stage deliverables. `--ephemeral` deletes everything **outside** the output dir for the run; output dir untouched. | User shouldn't have to navigate the opaque-named ArtifactStore. |
| D13 | Output filename surface | Registered with `RedactionRegistry` at `OutputSink.publish` time so logs / stdout / JSON summaries / tracebacks substitute `<output:hash6>`. Output-dir path prefix stays visible. | Otherwise the 20-char slug bleeds through every downstream surface. |
| D14 | Error blocks list no preserved files | `EphemeralDeleteFailedError` and `EphemeralStoreCleanupFailedError` show only failure details + cleanup command; never enumerate output filenames. | Listing slugs in errors puts them in bug-reports, CI captures, error aggregators. |

---

## 5. Architecture overview

### 5.1 New modules

```
src/kinoforge/core/
├── vault.py              # Vault loader, pydantic models, alias derivation, path validation
├── redaction.py          # RedactionRegistry singleton + RedactingLogFilter
├── ephemeral.py          # EphemeralSession context manager + EphemeralPolicy + state guard
└── secret.py             # Lightweight Secret[str] used at the orchestrator→engine seam only
```

### 5.2 Edited files (touch list)

```
src/kinoforge/core/orchestrator.py     # wraps generate()/batch_generate() with EphemeralSession; threads Secret prompt to engine
src/kinoforge/core/lifecycle.py        # Ledger.record consults RedactionRegistry; Ledger.touch ditto
src/kinoforge/core/profiles.py         # JsonProfileCache keyed by vault alias
src/kinoforge/core/batch.py            # batch_generate respects EphemeralSession; no _batch_summary.json under ephemeral
src/kinoforge/core/downloader.py       # opaque_name=True path; registers filename with RedactionRegistry on resolve
src/kinoforge/cli.py                   # --vault, --ephemeral, --debug-show-secrets flags; pre-flight gate; alias derivation call site
src/kinoforge/engines/{hosted,replicate,runway,fal}/__init__.py
                                       # delete-on-completion hook on RemoteSubmitPollBackend
src/kinoforge/stores/base.py           # ArtifactStore.delete_run + manual_cleanup_command ABC additions
src/kinoforge/stores/{local,s3,gcs}.py # delete_run + manual_cleanup_command impls; _redacted_for_disk helper at every put_json site
src/kinoforge/pipeline/generate_clip.py # opaque_store_name at put_bytes; registers output filename via sink
src/kinoforge/stores/sinks.py          # OutputSink.publish registers filename with RedactionRegistry
```

### 5.3 New tests

```
tests/core/test_vault.py
tests/core/test_redaction.py
tests/core/test_ephemeral.py
tests/core/test_ledger_redaction.py
tests/core/test_profile_cache_redaction.py
tests/core/test_batch_summary_skipped.py
tests/core/test_ephemeral_run_cleanup.py
tests/engines/test_delete_on_completion.py
tests/engines/test_fal_ephemeral_refused.py
tests/engines/test_fixture_capture_refused.py
tests/cli/test_preflight_ephemeral.py
tests/cli/test_flags_validation.py
tests/stores/test_delete_run.py
tests/pipeline/test_opaque_store_name.py
tests/integration/test_ephemeral_only_output_dir_survives.py
tests/integration/test_logging_filter_e2e.py
tests/integration/test_output_filename_redacted_in_logs.py
tests/test_no_unredacted_writes.py     # the CI invariant
```

### 5.4 Key invariants

1. The vault file is the only on-disk place where positive/negative prompts and
   LoRA refs/labels appear, and it lives outside the repo.
2. Every persistent-write site routes through `_redacted_for_disk()` or is
   gated by `EphemeralSession.policy.<gate>` short-circuit.
3. The CI invariant test parses the source tree, finds every persistent-write
   call, and asserts each is wrapped or annotated with the exemption tag.
4. The `RedactionRegistry` singleton holds the active vault's tokens; with no
   vault loaded, it is empty and behaves as a no-op (= public-by-design path
   unchanged).
5. Public-by-design loads (standard test prompt, examples/ YAMLs) never call
   `Vault.load()`, so their tokens are never registered; they serialize plain
   without any branching.

---

## 6. Vault file format & load semantics

### 6.1 Schema

YAML. Lives outside the repo. Loaded once at CLI entry; contents in process
memory only.

```yaml
# ~/.kinoforge/vault/cinematic-2026-06-08.yaml — example path; user-chosen
positive_prompt: |              # mode 1: auto-split via HeuristicSplitter (blank-line separators)
  Cinematic shot of a ...
  ...end of first beat.

  Cut to a close-up ...

negative_prompt: |              # optional
  blurry, low quality

loras:                          # optional; ordered (order is part of CapabilityKey identity)
  - ref: civitai:1234@5678
    label: my-secret-style      # optional; vault-internal label only, never persisted
  - ref: hf:org/lora:foo.safetensors

alias: null                     # optional; default = "cfg-" + sha256(material)[:12]
```

### 6.2 Both modes (D11)

Single-string and explicit-segments are mutually exclusive (`exactly-one-of`
pydantic validator):

```yaml
# mode 2: explicit segments (bypasses splitter)
segments:
  - prompt: "Cinematic wide shot..."
    params: { seed: 42 }
  - prompt: "Cut to a close-up..."
    params: { seed: 43 }
```

Mode 2 routes to `GenerateClipStage.run(request, *, segments_override=[...])`
(Phase 15 Task 15); orchestrator-side wiring already in place.

### 6.3 Alias derivation

```python
def compute_profile_alias(config: Config, vault: Vault | None) -> str:
    if vault is None:
        # Public-by-design path: backward-compat with existing cached entries.
        return CapabilityKey.from_config(config).derive()
    if vault.alias:
        return vault.alias    # explicit user override
    material = json.dumps({
        "base":      next(m.ref for m in config.models if m.kind == "base"),
        "loras":     [l.ref for l in vault.loras],
        "engine":    config.engine.kind,
        "precision": config.engine.precision,
    }, sort_keys=True)
    return "cfg-" + hashlib.sha256(material.encode()).hexdigest()[:12]
```

The derivation lives in memory; the inputs (especially `vault.loras[*].ref`)
never reach disk paired with the resulting alias.

### 6.4 Load order at CLI entry

1. Resolve `--vault PATH` (or `KINOFORGE_VAULT` env var).
2. Normalize to absolute path via `Path.resolve()`.
3. **Repo-root check.** Locate repo root via `git rev-parse --show-toplevel`. If
   resolved path is under that root, raise `VaultUnderRepoError`. If not in a
   git repo, skip.
4. **Permissions check.** `stat.S_IMODE(...) & 0o077` — if any bits set for
   group/other, emit `WARNING: vault file is readable by group/other; recommend chmod 600`.
   Doesn't block (Windows/NTFS users may not have unix perms).
5. Parse YAML → pydantic `Vault` model (`extra="forbid"`).
6. Compute `alias` (use override if set, else derive).
7. Register sensitive tokens with `RedactionRegistry.add_many([...])`.
8. Stash the `Vault` object on `cli_state` for the orchestrator to thread into
   `GenerationRequest`.

### 6.5 Pydantic models

```python
class VaultSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    params: dict[str, Any] = {}

class VaultLoRA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1)
    label: str | None = None

class Vault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positive_prompt: str | None = None
    segments: list[VaultSegment] | None = None
    negative_prompt: str | None = None
    loras: list[VaultLoRA] = []
    alias: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")

    @model_validator(mode="after")
    def exactly_one_of_prompt_or_segments(self) -> "Vault":
        has_prompt = self.positive_prompt is not None and self.positive_prompt.strip() != ""
        has_segments = self.segments is not None and len(self.segments) > 0
        if has_prompt and has_segments:
            raise ValueError("vault: specify exactly one of positive_prompt or segments")
        if not (has_prompt or has_segments):
            raise ValueError("vault: must specify either positive_prompt or segments")
        return self
```

### 6.6 Errors (all subclass `VaultError(KinoforgeError)`)

- `VaultPathError` — path not absolute / can't resolve / doesn't exist / not readable.
- `VaultUnderRepoError` — resolved path is under the active git repo root. Hard error with the path printed.
- `VaultParseError` — YAML malformed or pydantic violation. The vault loader catches `yaml.YAMLError` and `pydantic.ValidationError` and re-raises as `VaultParseError(path, original)` so the CLI error block shows a single error class with file:line and a human-readable cause.
- `VaultEmptyError` — neither `positive_prompt` nor `segments` populated.

### 6.7 Lifetime

- `Vault` object held by the CLI dispatcher for the duration of the process.
- On orchestrator exit (normal or exceptional), `RedactionRegistry.clear_session()` removes the registered tokens.
- `Vault.__repr__` returns `"<Vault alias=cfg-a3f7e1>"` — the alias is safe to log (opaque on-disk part).

---

## 7. RedactionRegistry & logging filter

### 7.1 API

```python
class RedactionRegistry:
    @classmethod
    def instance(cls) -> "RedactionRegistry": ...   # lazy singleton

    def add(self, token: str, *, kind: str, replacement: str | None = None) -> None:
        """Register `token`. `kind` is one of
        {'prompt:positive','prompt:negative','lora:ref','lora:label','lora:filename','output'}.
        If `replacement` omitted, default = f'<{kind}:{short_id}>' where short_id
        is a deterministic 6-char hash-derived suffix that distinguishes
        multiple tokens of the same kind in logs."""

    def add_many(self, tokens: list[tuple[str, str]]) -> None:
        """Bulk-register (token, kind) pairs. Used by the vault loader to
        register positive_prompt, negative_prompt, every lora.ref, every
        lora.label in one shot. Each pair flows through `add` with the same
        rejection rules."""

    def redact(self, s: str) -> str:
        """Substring-replace every registered token with its placeholder.
        Tokens applied longest-first to avoid partial overlap; case-sensitive."""

    def redact_json(self, obj: Any) -> Any:
        """Deep-walk dict/list/tuple, calling redact() on every str leaf.
        Returns a new structure; never mutates the input."""

    def clear_session(self) -> None: ...

    @property
    def is_active(self) -> bool: ...
```

### 7.2 Token registration rules

- Tokens shorter than 4 chars are rejected (false-positive risk).
- Whitespace-only tokens are rejected.
- Duplicate `add(token, ...)` is idempotent — the existing replacement wins.
- A token matching the placeholder pattern `<.+?:.+?>` is rejected.

### 7.3 Tokens registered by the vault loader

`positive_prompt`, `negative_prompt`, every `lora.ref`, every `lora.label`, plus
(at download-resolve time) every LoRA `Artifact.filename`.

### 7.4 `RedactingLogFilter`

```python
class RedactingLogFilter(logging.Filter):
    """Installed on the root 'kinoforge' logger at CLI entry. Calls
    RedactionRegistry.redact() on record.msg and every string arg before
    formatting. When bypass=True (only --debug-show-secrets), the filter is
    a passthrough."""

    def __init__(self, registry: RedactionRegistry, *, bypass: bool = False) -> None: ...
    def filter(self, record: logging.LogRecord) -> bool: ...
```

Installed in the CLI shim immediately after vault load. Also installed on the
**root logger** (level WARNING) as belt-and-suspenders for third-party libs
(`urllib3`, `boto3`, `runpod-python`).

### 7.5 Sink usage pattern (canonical)

```python
# in Ledger.record / JsonProfileCache._persist / batch summary writer / any put_json site
payload = RedactionRegistry.instance().redact_json(payload)
store.put_json(run_id, name, payload)
```

For `put_bytes` paths (raw bytes — intermediate frames, final mp4): no
redaction needed; bytes are media, not text.

### 7.6 `--debug-show-secrets` semantics

- Affects **only the logging filter** (`bypass=True`).
- Does NOT bypass `redact_json` at on-disk write sites — those always redact.
- Mutually exclusive with `--ephemeral` — passing both is a CLI error before
  any work begins.
- Not exposed via env var (deliberate friction).

---

## 8. EphemeralSession

### 8.1 EphemeralPolicy + DEFAULT/STRICT

```python
@dataclass(frozen=True)
class EphemeralPolicy:
    # Persistent-write gates
    ledger_record: bool                   # default True, ephemeral False
    profile_cache_persist: bool           # default True, ephemeral False
    batch_summary_write: bool             # default True, ephemeral False
    cost_sidecar_write: bool              # default True, ephemeral False
    heartbeat_ledger_touch: bool          # default True, ephemeral False
    # Provider-side
    delete_on_completion: bool            # default False, ephemeral True
    delete_retries: int                   # default 0, ephemeral 3
    # Identifiers
    memory_only_run_id: bool              # default False, ephemeral True
    pod_name_includes_alias: bool         # default True, ephemeral False
    # Logging
    force_debug_show_secrets_off: bool    # default False, ephemeral True

DEFAULT_POLICY = EphemeralPolicy(
    ledger_record=True, profile_cache_persist=True, batch_summary_write=True,
    cost_sidecar_write=True, heartbeat_ledger_touch=True,
    delete_on_completion=False, delete_retries=0,
    memory_only_run_id=False, pod_name_includes_alias=True,
    force_debug_show_secrets_off=False,
)
STRICT_POLICY = EphemeralPolicy(
    ledger_record=False, profile_cache_persist=False, batch_summary_write=False,
    cost_sidecar_write=False, heartbeat_ledger_touch=False,
    delete_on_completion=True, delete_retries=3,
    memory_only_run_id=True, pod_name_includes_alias=False,
    force_debug_show_secrets_off=True,
)
```

### 8.2 Context manager + `contextvars`

```python
class EphemeralSession:
    _active: contextvars.ContextVar["EphemeralSession | None"] = contextvars.ContextVar(
        "kinoforge_ephemeral_session", default=None
    )

    def __init__(self, *, enabled: bool) -> None:
        self.policy = STRICT_POLICY if enabled else DEFAULT_POLICY
        self.in_memory_ledger: dict[str, dict] = {}
        self.in_memory_profiles: dict[str, ModelProfile] = {}
        self._registered_stores: list[tuple[ArtifactStore, str]] = []
        self._token: contextvars.Token | None = None

    @classmethod
    def current(cls) -> "EphemeralSession | None":
        return cls._active.get()

    def register_store(self, store: ArtifactStore, run_id: str) -> None:
        """Called by orchestrator at the top of generate()/batch_generate()."""
        self._registered_stores.append((store, run_id))

    def __enter__(self) -> "EphemeralSession":
        self._token = self._active.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Order matters: provider-side first (external), then local (closer to user).
        # The output sink has already published before this point.
        if self.policy.delete_on_completion:
            for store, run_id in self._registered_stores:
                try:
                    store.delete_run(run_id)
                except Exception as e:
                    raise EphemeralStoreCleanupFailedError(store, run_id, e) from e
        self._token and self._active.reset(self._token)
```

`contextvars` (not `threading.local`) — propagates correctly through `asyncio`
and through `ThreadPoolExecutor` workers (the codebase's `ConcurrentPool` from
Phase 17).

### 8.3 Where the session is entered

```python
# cli.py — generate / batch / provision / deploy subcommands
with EphemeralSession(enabled=args.ephemeral):
    orchestrator.generate(config, request)
```

Read-mostly subcommands (`status / list / stop / destroy / reap / gc /
forget`) silently ignore `--ephemeral` with a one-line stderr note. They
consult the registry to redact display output.

### 8.4 Canonical sink pattern

```python
def some_persistent_write(self, payload: dict, ...) -> None:
    session = EphemeralSession.current()
    if session and not session.policy.<gate>:
        session.in_memory_<bucket>[<key>] = payload   # in-memory shadow
        return
    redacted = RedactionRegistry.instance().redact_json(payload)
    self._store.put_json(<run_id>, <name>, redacted)
```

Five required elements: (1) read `EphemeralSession.current()`, (2) check
matching `policy.<gate>`, (3) in-memory shadow on ephemeral-skip, (4)
`redact_json` before serialize, (5) actual store call. AST-checked by the CI
invariant test (§13).

### 8.5 Full state matrix

See Appendix A.

### 8.6 Pre-flight `EPHEMERAL_CAPABILITIES` table

See Appendix B.

### 8.7 Sentinel-gate amendment for heartbeat / reaper

Per Phase 36 Layer U: "any future heartbeat-aware reaper MUST check
`heartbeat_thread_tick` freshness before destructive decisions." Under
`--ephemeral`, the heartbeat loop runs (needed for the dead-man's switch
contract per SPEC §"Cost-safety" layer 1) but its in-pod liveness is the
source of truth; the ledger touch is skipped.

**Sweeper amendment:** the external sweeper MUST read the pod tag
`kinoforge-ephemeral=true` and treat heartbeat-absence-with-this-tag as
"alive by construction" — use create-time + `max_lifetime` as the only timers
for those pods. Regression test in `tests/core/test_lifecycle_sweeper.py`.

---

## 9. Profile cache aliasing & LoRA download cache

### 9.1 JsonProfileCache changes

`JsonProfileCache.resolve_or_discover` keys by `alias` (string) instead of
`CapabilityKey.derive()` hash:

```python
class JsonProfileCache:
    def resolve_or_discover(
        self,
        alias: str,                            # was: key: CapabilityKey
        capability_key: CapabilityKey,         # still needed for declared_flags + discover()
        engine: GenerationEngine,
        backend: GenerationBackend,
        *,
        discover_ttl_s: float = 30.0,
    ) -> ModelProfile: ...
```

Profile on disk at `profiles/<alias>.json` contains capability data only;
`ModelProfile.name` field is persisted as the alias itself (display-only, no
engine keys off it).

Cross-process lock (Phase 18 Layer H) is keyed on `alias` now (`<alias>.lock`).

### 9.2 Backward compat

The existing on-disk cache continues to work for public-by-design paths (no
vault). Files keyed by old `CapabilityKey.derive()` hashes stay readable; new
vault-driven entries appear with `cfg-…` prefix. Two naming schemes coexist
forever. No migration script.

### 9.3 LoRA download cache (opaque-when-local)

Downloader API gains `opaque_name: bool = False`:

```python
class Downloader:
    def download(self, artifact: Artifact, target_dir: Path,
                 *, opaque_name: bool = False) -> Path: ...
```

When `opaque_name=True`:
- Resolved `Artifact.sha256` required (else hard error).
- Target path = `target_dir / f"{artifact.sha256}.bin"`.
- Original `artifact.filename` registered with `RedactionRegistry` on download start.
- Resume `.part` file uses `<sha>.bin.part`.

**Who sets `opaque_name=True`** (in provisioner):

```python
opaque = (
    instance is None                              # no remote instance — local engine
    or instance.provider_name == "local"          # LocalProvider explicitly
)
```

In-pod downloads (RunPod, SkyPilot) keep original filenames (pod is
ephemeral). Local downloads use opaque names (forensic-disk-safe).

**ComfyUI graph implication when `opaque_name=True`:** ComfyUI's `LoraLoader`
node accepts any filename; the graph routes `lora_name: "<sha>.bin"`. Graph
itself is POSTed to ComfyUI at submit time, not persisted to disk by
kinoforge.

### 9.4 Ephemeral interaction

Inside `EphemeralSession`:
- Profile cache: `_persist` skipped; cache miss runs `discover()` but result
  held in `session.in_memory_profiles[alias]` only. Subsequent reads in the
  same process find it. Process exit drops it.
- LoRA cache opaque-name policy governed by "is this disk persistent?", not
  by ephemeral (always opaque on local).

---

## 10. Hosted delete-on-completion

### 10.1 ABC extension (additive on `RemoteSubmitPollBackend` from Phase 43 Task 0)

```python
class RemoteSubmitPollBackend(GenerationBackend, ABC):
    # existing: submit, result, _poll_status, _extract_url, ...
    @abstractmethod
    def _delete(self, job_id: str) -> None:
        """Issue the provider's DELETE for `job_id`. Raise on non-2xx.
        Subclass MUST implement; if the provider has no DELETE endpoint,
        raise EphemeralDeleteUnsupportedError so the pre-flight gate catches it."""

    @classmethod
    @abstractmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Provider-specific manual cleanup URL for the error block."""

    def _delete_with_retries(self, job_id: str, *, retries: int) -> None:
        """Exponential backoff (1s/2s/4s for retries=3). Final failure raises
        EphemeralDeleteFailedError."""
```

`result()` extended once (base class):

```python
def result(self, job_id: str) -> Artifact:
    artifact = self._poll_until_done(job_id)
    session = EphemeralSession.current()
    if session and session.policy.delete_on_completion:
        self._delete_with_retries(job_id, retries=session.policy.delete_retries)
    return artifact
```

### 10.2 Per-backend implementations

```python
# engines/replicate/__init__.py
class ReplicateBackend(RemoteSubmitPollBackend):
    def _delete(self, prediction_id: str) -> None:
        url = f"https://api.replicate.com/v1/predictions/{prediction_id}"
        resp = self._http.request(
            "DELETE", url,
            headers={"Authorization": f"Bearer {self._token.reveal()}"},
        )
        if resp.status_code not in (200, 204, 404):  # 404 = already gone
            raise EphemeralDeleteHTTPError(f"replicate DELETE returned {resp.status_code}")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://replicate.com/predictions/{job_id}"

# engines/runway/__init__.py
class RunwayBackend(RemoteSubmitPollBackend):
    def _delete(self, task_id: str) -> None:
        url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
        ...
    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://app.runwayml.com/tasks/{job_id}"

# engines/fal/__init__.py
class FalBackend(RemoteSubmitPollBackend):
    def _delete(self, request_id: str) -> None:
        raise EphemeralDeleteUnsupportedError("fal has no public DELETE endpoint")
    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return ""   # unreachable — pre-flight refuses fal under --ephemeral
```

### 10.3 Error hierarchy (`core/errors.py`)

```python
class EphemeralError(KinoforgeError): ...
class EphemeralDeleteUnsupportedError(EphemeralError): ...
class EphemeralDeleteHTTPError(EphemeralError): ...
class EphemeralDeleteFailedError(EphemeralError):
    def __init__(self, job_id: str, provider: str, manual_url: str) -> None: ...
class EphemeralStoreCleanupFailedError(EphemeralError):
    def __init__(self, store: "ArtifactStore", run_id: str, original_error: Exception) -> None: ...
```

### 10.4 Self-hosted engine handling

ComfyUI / Diffusers on pod: no `_delete` needed. The "provider-side record"
is the pod itself, and pod teardown is **not** an `EphemeralSession`
responsibility — it never was. `deploy_session` deliberately leaves the
instance alive on a successful exit (`core/orchestrator.py` ~L531: "Does
NOT call `provider.destroy_instance` — the instance is left alive for warm
reuse by the next session or for the sweeper / budget tracker to reap.").
Forcing destruction under `--ephemeral` would defeat the upcoming
warm-reuse roadmap (PROGRESS B5 → B7 → B4 → B2 → B1 → B3) — every
`--ephemeral` run would pay a fresh cold-boot (~2-15 min + weight DL),
which is roughly half of inference wall time on a typical Wan run
(`successful-generations.md` entry #5: "Warm-reuse savings: ~140 s of
provision + boot ... roughly half its own runtime").

The pod is governed by the same safety nets as any non-ephemeral run:

- In-pod selfterm watchdog (Phase 7) — dead-man's switch at
  `idle_timeout`.
- `max_lifetime` wall cap inside selfterm.
- Out-of-band `kinoforge reap` + future external sweeper.
- `BudgetTracker` mid-run circuit breaker.

`EphemeralSession.__exit__` is therefore scoped to **records, not
compute** — it deletes the `ArtifactStore` run directory, leaves the
pod alone, and lets the warm-reuse machinery (current + future) decide
when the pod actually dies. The `kinoforge-ephemeral=true` pod tag (per
§A + §8.7) is what the future sweeper uses to skip heartbeat-staleness
checks for these pods and reap them by `create_time + max_lifetime`
instead.

### 10.5 Error block UX

**Provider delete failure:**

```
ERROR: --ephemeral could not delete the provider-side record.
  provider: replicate
  job_id:   a1b2c3d4-...
  attempts: 3
  last:     503 Service Unavailable

To finish the scrub, run:

  curl -X DELETE -H "Authorization: Bearer $REPLICATE_API_TOKEN" \
    https://api.replicate.com/v1/predictions/a1b2c3d4-...

(kinoforge exited 1 because ephemeral requires a clean scrub.)
```

**Store cleanup failure:**

```
ERROR: --ephemeral could not delete the run's on-disk artifacts.
  store:    /workspace/.kinoforge/state/runs/2026-06-08-abc123
  run_id:   2026-06-08-abc123
  error:    PermissionError: [Errno 13] Permission denied: '.../lock'

To finish the scrub, run:

  rm -rf "/workspace/.kinoforge/state/runs/2026-06-08-abc123"

(kinoforge exited 1 because ephemeral requires a clean scrub.)
```

**Neither block enumerates output filenames** (per D14). The user already knows
where their output dir is.

---

## 11. CLI / Config surface

### 11.1 New flags

| Flag | Env var | Default | Meaning |
|---|---|---|---|
| `--vault PATH` | `KINOFORGE_VAULT` | unset | Path to vault file. Loaded once at CLI entry. Validated not-under-repo. |
| `--ephemeral` | _(none)_ | off | Activate `EphemeralSession` for this invocation. |
| `--debug-show-secrets` | _(none)_ | off | Bypass logging filter ONLY. Rejected when `--ephemeral` is set. |

### 11.2 Decoupled semantics (D-recap)

- `--vault` (alone): always-on policy active; registry populated; sinks redact.
- `--ephemeral` (alone): no registry tokens, but strict policy skips local
  writes + delete-on-completion + memory-only run_id. Useful for comparison
  smokes that shouldn't pollute provider dashboards.
- `--vault` + `--ephemeral`: both. Most defensive.
- Neither: today's behavior, byte-identical.

### 11.3 Validation order at CLI entry

1. `--debug-show-secrets` + `--ephemeral` → reject with clear error.
2. `--vault` path → resolve → not-under-repo check → permissions warn → load
   YAML → register tokens.
3. `--ephemeral` → look up `(engine.kind, compute.provider)` in
   `EPHEMERAL_CAPABILITIES` (Appendix B) → reject if `False` / missing.
4. Begin orchestration inside `with EphemeralSession(enabled=args.ephemeral):`.

### 11.4 Pre-flight error example

```
ERROR: --ephemeral is not supported for this configuration.
  engine:    fal
  provider:  (none — fal is a hosted API)
  reason:    fal has no public prediction-delete endpoint.

  Use one of these instead:
    engine: replicate     (DELETE /v1/predictions/{id})
    engine: runway        (DELETE /v1/tasks/{id})
    engine: comfyui       (any pod-based provider — pod destruction is the scrub)
    engine: diffusers     (any pod-based provider — same)

  Or drop --ephemeral to allow provider-side record retention.
```

### 11.5 `kinoforge batch` interaction

`batch_generate()` already wraps work in one `deploy_session` (Phase 22 Task 1).
Under `--ephemeral`:

- Whole batch runs inside one `EphemeralSession` context.
- Per-entry hosted-API: `result()` calls `_delete()` after artifact retrieval.
- Per-pod entries share one pod for the duration of the batch (warm-reuse within); the pod survives `batch_generate` exit on the same terms as any single `generate()` call — `deploy_session` does not destroy on success. Safety nets per §10.4 govern teardown.
- `_batch_summary.json` not written.
- A single delete-failure on any entry: remaining entries continue, batch exits
  non-zero with summary of which job_ids need manual cleanup.

### 11.6 No Config schema changes

No new YAML fields. Vault path is per-run + machine-specific (could itself be
a small info leak); kept CLI-only. Ephemeral is a per-invocation decision.

---

## 12. Redaction at every known sink

### 12.1 Canonical pattern

(See §8.4.)

### 12.2 Reach table

| # | Site | File | Policy gate | In-memory bucket | Notes |
|---|---|---|---|---|---|
| 1 | `Ledger.record(instance)` | `core/lifecycle.py` | `ledger_record` | `in_memory_ledger[id]` | Existing single-flight lock (Phase 18 Task 7) wraps the persistent path |
| 2 | `Ledger.touch(id, heartbeat_ts)` | `core/lifecycle.py` | `heartbeat_ledger_touch` | `in_memory_ledger[id]["last_heartbeat"]` | Sentinel-gate amendment (§8.7) |
| 3 | `Ledger.forget(id)` | `core/lifecycle.py` | always run | n/a | Removal safe to apply to both stores |
| 4 | `JsonProfileCache._persist(alias, profile)` | `core/profiles.py` | `profile_cache_persist` | `in_memory_profiles[alias]` | Discovery still runs on miss; shadow consulted by subsequent `resolve` |
| 5 | `batch_generate` summary writer | `core/batch.py` | `batch_summary_write` | (no shadow — stdout only) | Per-entry results in process memory |
| 6 | `LocalArtifactStore.put_json` | `stores/local.py` | n/a — lowest-level writer | n/a | Redaction happens at caller (1/2/4/5) |
| 7 | `S3ArtifactStore.put_json` | `stores/s3.py` | same as 6 | n/a | |
| 8 | `GCSArtifactStore.put_json` | `stores/gcs.py` | same as 6 | n/a | |
| 9 | Cost sidecar `<output>.cost.json` (Layer 5 stub) | `core/cost.py` (new) | `cost_sidecar_write` | (no shadow) | Pre-wired; future layer lands with gate in place |
| 10 | `LocalOutputSink.publish(bytes, meta)` | `stores/sinks.py` | always writes | n/a | Allowed disclosure surface; bytes are media |
| 11 | Fixture capture (`KINOFORGE_SAVE_FIXTURES=1`) | `engines/<each>/__init__.py` | always refused if registry active OR ephemeral on | n/a | Belt-and-suspenders |
| 12 | `Ledger` migration / GC subcommand output | `cli.py::_cmd_gc` | n/a | n/a | Reads already-redacted entries |
| 13 | `kinoforge status / list` stdout | `cli.py::_cmd_status` | n/a | n/a | Heartbeat sentinel-staleness advisory prints alias only |
| 14 | `store.put_bytes` for clip/artifact bytes | `pipeline/generate_clip.py` | always opaque store-side name | n/a | `opaque_store_name(bytes, ext)` enforced by AC2 |

### 12.3 Output filename surface audit (the registration-at-publish fix)

`OutputSink.publish` registers the basename with `RedactionRegistry` before
returning:

```python
def publish(self, payload: bytes, meta: dict) -> str:
    filename = self._format_filename(meta)
    path     = self._output_dir / filename
    path.write_bytes(payload)
    RedactionRegistry.instance().add(filename, kind="output")
    return str(path)
```

After `publish` returns:
- Logs / stdout / JSON summaries / tracebacks that interpolate `path` or
  `filename` substitute the basename to `<output:<hash6>>`.
- Output dir path prefix (`/workspace/output/`) remains visible (D13).
- File on disk in output dir keeps its permissive name.

**Forward-compat constraint:** if `hooks.post_generate` is added, the hook MUST
receive the output path via **stdin or env var**, never argv (argv shows in
`ps -ef` / journalctl).

### 12.4 `ArtifactStore.delete_run` + `manual_cleanup_command`

```python
class ArtifactStore(ABC):
    @abstractmethod
    def delete_run(self, run_id: str) -> None: ...
    @abstractmethod
    def manual_cleanup_command(self, run_id: str) -> str:
        """Single-line shell command that deletes everything under this
        store's `run_id` prefix. Used in error blocks when delete_run fails."""
```

Implementations:
- `LocalArtifactStore.manual_cleanup_command`: `f'rm -rf "{self._root / run_id}"'`
- `S3ArtifactStore.manual_cleanup_command`: `f'aws s3 rm s3://{self._bucket}/{self._prefix}{run_id}/ --recursive'`
- `GCSArtifactStore.manual_cleanup_command`: `f'gcloud storage rm -r gs://{self._bucket}/{self._prefix}{run_id}/'`

`LocalArtifactStore.delete_run`: `shutil.rmtree(self._root / run_id)` with
`FileNotFoundError` swallowed.

### 12.5 `EphemeralStoreCleanupFailedError` UX

Per §10.5. Failure block does NOT enumerate preserved output files (D14).

---

## 13. CI invariant test (`tests/test_no_unredacted_writes.py`)

Modeled on `tests/test_core_invariant.py`. AST-based scan of `src/kinoforge/`,
~250 LOC, no deps beyond `ast` + `pathlib`, <1s runtime.

### 13.1 AC1 — every persistent JSON write follows the canonical pattern

For every `<store>.put_json(...)` call in `src/kinoforge/`, the enclosing
function must contain (in order, before the call):
1. Assignment with RHS `EphemeralSession.current()`.
2. `if session and not session.policy.<gate>:` early-return branch.
3. Assignment with RHS containing `RedactionRegistry.instance().redact_json(...)`.

Per-site exemption: line-level comment `# kinoforge:public-write`.

### 13.2 AC2 — every `put_bytes` clip site uses `opaque_store_name`

The second arg to `<store>.put_bytes(run_id, name, ...)` must be a Name or
Call referencing `opaque_store_name(...)`, or carry the `# kinoforge:public-name` tag.

### 13.3 AC3 — every `OutputSink.publish` registers the basename

Any class whose name ends in `OutputSink` and defines a concrete `publish`
method must call `RedactionRegistry.instance().add(<basename>, kind="output", ...)`
between the file-write and the return.

### 13.4 Smaller invariants (one-liners)

- `test_fixture_capture_checks_registry`: every `_save_fixture` method body
  must contain `RedactionRegistry.instance().is_active` check before the write.
- `test_redacting_log_filter_installed_in_cli_entry`: `cli.py` top-level
  entry installs `RedactingLogFilter` on root `kinoforge` logger.
- `test_artifact_store_subclasses_implement_delete_run_and_manual_cleanup`:
  every concrete `ArtifactStore` implements both methods.

### 13.5 Belt-and-suspenders path-write scan

`test_no_path_write_outside_store_and_sink`: in `src/kinoforge/`, any
`Path.write_bytes` / `Path.write_text` / `open(..., 'w'/'wb')` call must be
inside a class whose name ends in `ArtifactStore` or `OutputSink`, or be
annotated with `# kinoforge:public-write`.

### 13.6 What it doesn't catch

- Log emission interpolating an unregistered string (mitigated: vault loader
  registers tokens before orchestrator runs).
- A sink bypassing the ArtifactStore entirely (mitigated by §13.5).
- AST detection sidestepping via `getattr(store, "put_json")(...)`. Same gap
  as `test_core_invariant.py` accepts for `importlib.import_module`. Low
  probability hostile-developer territory.

### 13.7 Violation message format

Each assert includes (1) offending `file:line`, (2) one-line description of
which gate is missing, (3) pointer to the canonical reference
implementation (`core/lifecycle.py::Ledger.record`), (4) the exemption tag
the developer can add if the call is genuinely public.

---

## 14. Migration & rollout

### 14.1 Sub-merge sequence (α / β / γ / δ / ε)

| Sub | Scope | What it does | What it doesn't yet do |
|---|---|---|---|
| **α** | foundation | `core/vault.py` + `core/redaction.py` + `core/secret.py` + CLI `--vault` flag + `RedactingLogFilter` install | No write-site retrofits; most JSON writes still emit plaintext if vault is unused |
| **β** | sink retrofit | Canonical pattern at every persistent-write site; `opaque_store_name`; `ArtifactStore.delete_run` + `manual_cleanup_command`; `OutputSink.publish` registers basename; `_save_fixture` registry check | No EphemeralSession yet — gates always evaluate to "default" |
| **γ** | ephemeral | `core/ephemeral.py`; CLI `--ephemeral` + `--debug-show-secrets`; pre-flight gate; session `__exit__` cleanup | No hosted-side delete; ephemeral is local-only |
| **δ** | hosted delete | `RemoteSubmitPollBackend._delete` + per-engine impls (Replicate, Runway); `EphemeralDeleteUnsupportedError` for fal; UX with cleanup command | None — feature complete |
| **ε** | CI invariant | `tests/test_no_unredacted_writes.py` with all 6 ACs | Locks gates against regression |

ε ships LAST so its assertions are clean. β sub-merges can themselves span
multiple commits (one per write site); existing tests stay green at every step.

### 14.2 Backward compat

- All existing public-by-design paths byte-identical: `prompt-field-realistic.txt`
  smokes, `examples/configs/*.yaml` runs, Phase 22 batch flow without `--vault`.
- Every existing ABC: `ArtifactStore` gains `delete_run` + `manual_cleanup_command`
  (additive). All other ABCs untouched.
- 750+ existing tests continue to pass.
- `kinoforge gc` contract from Phase 13 Task 4 unchanged. `gc` does NOT
  participate in EphemeralSession.

### 14.3 Documentation deliverables

- **`examples/vault/example.yaml`** — template (user copies and edits).
- **`DESIGN.md`** — new "Privacy boundary" section pointing at this spec +
  forward-compat contracts.
- **`PROGRESS.md`** — new phase entry with α–ε SHAs as they land.
- **`CLAUDE.md`** — no change.
- **`README.md`** — no change.

### 14.4 Estimated diff size

- New code: ~600 LOC.
- Touched existing code: ~150 LOC of additions across ~10 files.
- New tests: ~35.
- Touched existing tests: ~10.
- Expected post-merge test count: ~785.

---

## 15. Non-goals

(See §2 "Out of scope (this spec)" — non-goals enumerated there.)

---

## 16. Forward-compat contracts

Spell out in `DESIGN.md` "Privacy boundary" section + `PROGRESS.md` regression notes:

1. **Heartbeat / external sweeper amendment.** Sweeper MUST treat
   `kinoforge-ephemeral=true` pod tag as alive-by-construction (no ledger
   heartbeat → use create-time + max_lifetime).
2. **Cost sidecar (Layer 5 candidate).** When `core/cost.py
   _write_cost_sidecar` lands, MUST consult
   `EphemeralSession.current().policy.cost_sidecar_write` and skip on ephemeral.
3. **New ArtifactStore implementations (Azure, B2, R2, future).** MUST
   implement `delete_run` AND `manual_cleanup_command`.
4. **New engines subclassing `RemoteSubmitPollBackend`.** MUST implement
   `_delete` (or raise `EphemeralDeleteUnsupportedError`) AND register in
   `EPHEMERAL_CAPABILITIES`.
5. **`hooks.post_generate` (future feature).** MUST receive output path via
   stdin or env var, never argv.
6. **New `OutputSink` subclasses.** MUST call
   `RedactionRegistry.instance().add(basename, kind="output")` before
   `publish` returns (AC3 enforces).
7. **New splitter adapters** (LLM, scene-detect — deferred). No contract
   change; vault format is splitter-agnostic.
8. **New `_save_fixture` methods.** MUST check
   `RedactionRegistry.instance().is_active` and refuse, plus check
   `EphemeralSession.current().policy.delete_on_completion`.

---

## Appendix A: Full state matrix

| Surface | Default (vault loaded) | `--ephemeral` |
|---|---|---|
| Vault file | User-managed on disk; never modified | Same |
| `RedactionRegistry` | Holds vault tokens | Same |
| Logs (stderr) | Redacted at source via `RedactingLogFilter` | Redacted; `--debug-show-secrets` rejected |
| Ledger (`ledger.json`) | Row written; sensitive fields → placeholders | Skipped; in-memory shadow only |
| Profile cache (`profiles/<alias>.json`) | Persisted; alias-keyed | Read-only on existing entries; new misses → in-memory shadow |
| Batch summary (`_batch_summary.json`) | Written; per-entry prompts → placeholders | Skipped entirely |
| Cost sidecar (`<output>.cost.json`) | Written; refs → placeholders (Layer 5 candidate) | Skipped |
| Heartbeat ledger touch | Writes `last_heartbeat` | Skipped (sweeper amendment §8.7) |
| Live-fixture capture (`KINOFORGE_SAVE_FIXTURES=1`) | Refused if `RedactionRegistry.is_active`; else allowed | Refused regardless |
| ArtifactStore filenames (`<run_id>/*`) | Opaque sha256-derived (always) | Opaque sha256-derived (always) |
| ArtifactStore contents (`<run_id>/*` directory) | Persists across runs (cleaned by `kinoforge gc`) | `delete_run(run_id)` at session exit |
| LoRA download local cache (LocalProvider engine) | `<sha256>.bin` opaque filenames | Same |
| LoRA download in-pod (RunPod / SkyPilot) | Original filename (ephemeral pod) | Same |
| Output sink dir contents | All files preserved unconditionally (final mp4, keyframes, future stage deliverables) | Same — sole exempt zone |
| Output filenames echoed via log / stdout / JSON / traceback | Redacted to `<output:<hash6>>`; path prefix visible | Same |
| Run id | `uuid4`, threaded through `Ledger.record` | `uuid4`, never written |
| Hosted-API request body | Sent (necessary) | Sent (necessary) |
| Hosted-API prediction record | Persists on provider | `DELETE /predictions/{id}` with 3-retry; hard-fail with manual cleanup command |
| Compute instance (pod / VM) | Survives session exit per `deploy_session` contract; governed by selfterm + sweeper + budget tracker | **Same — pod NOT destroyed by `EphemeralSession.__exit__`** (revised from earlier draft; see §10.4 rationale). `kinoforge-ephemeral=true` pod tag lets sweeper skip heartbeat-staleness check and reap by create-time + max_lifetime. |
| RunPod pod name | `kinoforge-{alias}-{rand4}` | `kinoforge-{rand8}` (no alias) |
| RunPod pod tags | `engine=<kind>`, `capability=<alias>` | `engine=<kind>`, `kinoforge-ephemeral=true` |
| RunPod self-terminate credential | Injected on create (Phase 7) | Same |
| Pre-flight gate | None | Refuses if engine/provider has no DELETE endpoint |
| Exception tracebacks | `Vault.__repr__` returns alias only | Same |

---

## Appendix B: `EPHEMERAL_CAPABILITIES` table

```python
# core/ephemeral.py
EPHEMERAL_CAPABILITIES: dict[tuple[str, str | None], bool] = {
    ("comfyui",   "runpod"):   True,   # pod destroyed; no provider-side prediction record
    ("comfyui",   "local"):    True,   # local pod ephemerality is user's filesystem
    ("comfyui",   "skypilot"): True,
    ("diffusers", "runpod"):   True,
    ("diffusers", "local"):    True,
    ("diffusers", "skypilot"): True,
    ("hosted",    None):       False,  # generic hosted — refuse by default
    ("replicate", None):       True,   # DELETE /v1/predictions/{id}
    ("runway",    None):       True,   # DELETE /v1/tasks/{id}
    ("fal",       None):       False,  # no public DELETE endpoint as of 2026-06
    ("luma",      None):       False,  # direct API retired (Phase 44)
}
```

---

## Appendix C: Acceptance criteria checklist

Treat each as a failing test first (per `CLAUDE.md`/Superpowers), then make it
pass.

**Vault & registry:**
- [ ] Vault file under repo root → `VaultUnderRepoError` at CLI entry.
- [ ] Vault YAML with both `positive_prompt` and `segments` → pydantic
  validation error.
- [ ] Vault YAML with neither → `VaultEmptyError`.
- [ ] Vault explicit alias matching `[a-z0-9][a-z0-9-]{0,63}` accepted;
  uppercase rejected.
- [ ] Auto-derived alias is `cfg-` + 12 hex chars; deterministic over same
  material; different over LoRA-stack reorder.
- [ ] `RedactionRegistry.add` rejects 3-char tokens.
- [ ] `RedactionRegistry.add` rejects placeholder-pattern tokens.
- [ ] `RedactionRegistry.redact` longest-first when overlap.
- [ ] `RedactionRegistry.redact_json` deep-walks nested dicts.
- [ ] `RedactingLogFilter` substitutes registered tokens in `record.msg` and
  `record.args`.
- [ ] `RedactingLogFilter` bypass=True passes through unchanged.
- [ ] Empty registry → `redact()` is identity (public-by-design path).

**EphemeralSession & policy:**
- [ ] `EphemeralSession.current()` returns the active session inside `with`,
  None outside.
- [ ] `EphemeralSession.current()` propagates through `ThreadPoolExecutor`
  workers (`contextvars` semantics).
- [ ] `Ledger.record` under default mode: redacted payload persisted.
- [ ] `Ledger.record` under ephemeral: payload in
  `session.in_memory_ledger[id]`, NOT on disk.
- [ ] `JsonProfileCache._persist` skipped under ephemeral; subsequent
  `resolve` finds in-memory entry.
- [ ] `_batch_summary.json` not on disk under ephemeral.
- [ ] `_save_fixture` refuses if registry active or ephemeral on.

**Profile cache & LoRA cache:**
- [ ] `JsonProfileCache` writes to `profiles/<alias>.json` under vault;
  `profiles/<hash>.json` without vault.
- [ ] Persisted profile contains no LoRA refs; `name` field is the alias.
- [ ] `Downloader.download(opaque_name=True)` requires `sha256`; writes
  `<sha>.bin`; registers original filename.
- [ ] Provisioner sets `opaque_name=True` when `instance is None` or
  `instance.provider_name == "local"`.

**Hosted delete-on-completion:**
- [ ] `ReplicateBackend._delete` sends DELETE to
  `https://api.replicate.com/v1/predictions/{id}` with Bearer auth; 200/204/404 succeed.
- [ ] `RunwayBackend._delete` sends DELETE to `/v1/tasks/{id}`; 200/204/404 succeed.
- [ ] `FalBackend._delete` raises `EphemeralDeleteUnsupportedError`.
- [ ] `_delete_with_retries` uses 1s/2s/4s backoff for retries=3.
- [ ] Terminal delete failure raises `EphemeralDeleteFailedError` with
  `job_id`, `provider`, `manual_url`.

**Output filename surface:**
- [ ] `LocalOutputSink.publish` registers basename with
  `RedactionRegistry.instance().add(filename, kind="output")` before return.
- [ ] After publish, captured stderr contains output dir path but NOT the
  prompt slug.
- [ ] `_batch_summary.json` under vault contains `<output:<hash6>>` substituted
  in `entry["output_uri"]`.
- [ ] Error blocks for delete-failure and cleanup-failure do NOT enumerate
  preserved output files.

**Store cleanup:**
- [ ] `LocalArtifactStore.delete_run(run_id)` removes
  `<root>/<run_id>/` recursively.
- [ ] `S3ArtifactStore.delete_run(run_id)` paginates
  `list_objects_v2` + `delete_objects` (1000-per-batch).
- [ ] `GCSArtifactStore.delete_run(run_id)` uses `bucket.delete_blobs`.
- [ ] `manual_cleanup_command` returns single-line shell command; absolute
  paths; double-quoted local paths.
- [ ] `EphemeralSession.__exit__` calls `delete_run` for every registered store
  AFTER the output sink has published.
- [ ] Store cleanup failure raises `EphemeralStoreCleanupFailedError` with
  cleanup command in message.

**CLI flag validation & pre-flight:**
- [ ] `--debug-show-secrets` + `--ephemeral` rejected at CLI entry.
- [ ] `--ephemeral` with `engine=fal` rejected at pre-flight (capability table
  miss).
- [ ] `--ephemeral` with `engine=replicate` accepted at pre-flight.
- [ ] `--vault PATH` env var fallback (`KINOFORGE_VAULT`) works.
- [ ] Vault file with group-readable perms emits WARNING but doesn't block.

**Pod-side identifiers:**
- [ ] Under default: pod named `kinoforge-{alias}-{rand4}`; tag
  `capability={alias}`.
- [ ] Under ephemeral: pod named `kinoforge-{rand8}`; tag
  `kinoforge-ephemeral=true`; no `capability` tag.

**CI invariant test:**
- [ ] `test_no_unredacted_writes.py` AC1 fails when a fresh `put_json` site
  is added without the canonical pattern.
- [ ] AC1 passes when annotated `# kinoforge:public-write`.
- [ ] AC2 fails when `put_bytes` site doesn't use `opaque_store_name`.
- [ ] AC3 fails when `OutputSink.publish` doesn't register basename.
- [ ] `test_no_path_write_outside_store_and_sink` fails on a rogue
  `Path.write_bytes` outside the two abstractions.

**Sweeper amendment:**
- [ ] External sweeper skips heartbeat-staleness check for pods tagged
  `kinoforge-ephemeral=true`; uses create-time + max_lifetime instead.

**End-to-end:**
- [ ] `test_ephemeral_only_output_dir_survives.py`: after vault+ephemeral
  run via `FakeEngine` + `LocalProvider` + `LocalArtifactStore` +
  `LocalOutputSink`, the artifact store run dir is empty, state dir has no
  run-tagged sidecars, output dir contains all stage deliverables with
  permissive names.

---

## Resolved questions (not to reopen)

| # | Q | A |
|---|---|---|
| 1 | Online scope | Moderate (D1) |
| 2 | Always-on vs flag | Always-on + ephemeral-adds-delete (D2) |
| 3 | Input surface | Vault file outside repo (D3) |
| 4 | LoRA-name scope | All four: ref, filename, label, hash (D4) |
| 5 | Profile cache | Opaque vault-side alias (D5) |
| 6 | Historical prompts | Test/comparison stay public (D6) |
| 7 | Output filename schema | Permissive (D7) |
| 8 | Delete failure | Hard fail with retries (D8) |
| 9 | Log redaction | At source + opt-in bypass (D9) |
| 10 | Architecture | C + CI invariant test (D10) |
| 11 | Vault modes | Both single-string and explicit-segments (D11) |
| 12 | Output dir exempt | Yes, anything goes (D12) |
| 13 | Output filename surface beyond disk | Registered at publish; path prefix visible (D13) |
| 14 | Error blocks listing files | No (D14) |
