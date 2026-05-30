# `.env` secrets loader — design spec

**Date:** 2026-05-30
**Author:** brainstorm session (Dr. Twinklebrane + Claude)
**Issue:** none yet (post-MVP convenience layer; recommend opening one before plan phase)
**Status:** validated, awaiting user spec review before plan phase

---

## 1. Motivation

Kinoforge's `EnvCredentialProvider` (`src/kinoforge/core/credentials.py`) reads
secrets from `os.environ`.  Today the only way to populate those vars is by
exporting them in the shell (e.g. `~/.bashrc`), which has three problems:

1. **Plaintext in shell config** — `~/.bashrc` is world-readable on default
   permissions; secrets persist in shell history if exported wrong; secrets
   leak via subprocess inheritance and `env` dumps to logs.
2. **No per-project scoping** — one `FAL_KEY` in `~/.bashrc` means every
   project on the box shares it.  Compromise of one project ≠ compromise of
   all only if keys are project-scoped.
3. **Onboarding friction** — new users have no way to discover which env vars
   kinoforge needs without reading the source.

This layer adds a project-root `.env` file as the canonical single repository
of kinoforge's own API credentials (`FAL_KEY`, `CIVITAI_TOKEN`, `HF_TOKEN`,
`RUNPOD_API_KEY`, future hosted-provider keys).  Loaded once at CLI startup,
populates `os.environ`, every existing consumer reads transparently — no
changes to any engine, source, provider, or store.

---

## 2. Scope

### In scope (loaded from `.env`)

Kinoforge's own credential vars consumed via `EnvCredentialProvider`:

- `FAL_KEY` (hosted engine, fal.ai)
- `CIVITAI_TOKEN` (CivitAI source)
- `HF_TOKEN` (HuggingFace source)
- `RUNPOD_API_KEY` (RunPod provider)
- Any future hosted-provider key declared via `cfg.engine.hosted.api_key_env`

### Out of scope

- **AWS/GCP SDK credentials** (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `GOOGLE_APPLICATION_CREDENTIALS`, etc.) — boto3 + google-cloud-storage walk
  their own default credential chains (env → `~/.aws/credentials` → IMDS →
  IAM role / ADC → gcloud config → GCE metadata).  Kinoforge deliberately
  stays out of this (handoff `20260530-192536.md` §4.3) so IMDS/IAM-role
  auto-discovery on EC2/GCE keeps working out of the box.

  **However:** if a user chooses to put `AWS_ACCESS_KEY_ID` etc. into their
  `.env`, the SDK default chain will pick them up via `os.environ` because the
  shim populates `os.environ` transparently.  Supported but not required.

- **Non-secret configuration** (`KINOFORGE_S3_BUCKET`, `KINOFORGE_S3_PREFIX`,
  GCS equivalents) — these are config values, not secrets, and the CLI's main
  path reads them from YAML via `StoreConfig`.  Stay YAML-only.

- **Pod-internal runtime injection** (`RUNPOD_POD_ID`, `RUNPOD_TERMINATE_KEY`)
  — set inside the GPU pod by `selfterm.py` at runtime, not read from the
  user's dev shell.  Including them in `.env` would mislead.

---

## 3. Decisions (locked during brainstorm)

| # | Decision | Value | Why |
|---|---|---|---|
| D1 | Scope | Kinoforge creds only (see §2) | User's "single source" applies to what kinoforge owns; cloud SDK chains stay intact |
| D2 | File location | `./.env` (cwd-relative) + `--env-file PATH` override | Simplest, matches dotenv convention; explicit flag covers edge cases |
| D3 | Precedence | Shell wins; `.env` fills gaps (`override=False`) | Standard dotenv; CI/prod exports always win; dev `.env` is fallback |
| D4 | Missing-key UX | Lazy — existing `AuthError` on first use | No new abstractions, no required-keys manifest to maintain |
| D5 | `.env.example` | Yes, checked in, documents 4 known keys | Discoverable from repo; standard onboarding pattern |
| D6 | Library | `python-dotenv` (conda-forge) | Industry standard, handles quoting/multi-line/expansion |
| D7 | Mechanism | Transparent shim at CLI entry → `os.environ` | Zero changes to engines/sources/providers/stores; preserves SDK default chains |

---

## 4. Architecture

### 4.1 New module: `kinoforge.core.dotenv_loader`

Single function:

```python
def load_env_file(path: Path | None = None, *, override: bool = False) -> None:
    """Load environment variables from a .env file into os.environ.

    Args:
        path: Path to the .env file. Defaults to ``Path.cwd() / ".env"``.
        override: If False (default), existing os.environ values win.
            If True, .env values overwrite existing ones.

    Raises:
        FileNotFoundError: When ``path`` is explicitly provided but does
            not exist. Default path is silently skipped if absent.
    """
```

### 4.2 CLI integration

Top of `kinoforge.cli.main(argv)`:

1. argparse gains a top-level optional `--env-file PATH`.
2. Before subcommand dispatch: `load_env_file(args.env_file)` (no
   `override` kwarg — CLI always uses the default `False`).

That is the only call site.  Order is locked: load happens before
`_build_orchestrator`, `_build_store`, or any adapter construction.

The `override=True` path on `load_env_file` is **library-only** — exposed for
library users who explicitly want `.env` to clobber existing `os.environ`
values.  No CLI flag flips it.  Rationale: the CLI default is the safe
production behavior (shell wins, CI exports always take precedence); library
users have full control by calling the function themselves.

### 4.3 Non-CLI (library) entry points

Library users importing kinoforge from their own Python scripts must call
`load_env_file()` themselves (or export vars in their shell, or populate
`os.environ` some other way).  Rationale: implicit cwd-relative file IO
inside library code is surprising; explicit at the CLI boundary is the right
scope.  Documented in README.

---

## 5. Data flow

```
$ kinoforge generate --config wan.yaml
    │
    ▼
cli.main(argv)
  ├─ argparse parses --env-file (optional, default None)
  ├─ load_env_file(args.env_file)        ◄── ONE call, before everything
  │     │
  │     ├─ path = args.env_file or Path.cwd() / ".env"
  │     ├─ if not path.exists():
  │     │     if args.env_file is None: return    (default, silent)
  │     │     else: raise FileNotFoundError(path) (explicit, hard fail)
  │     ├─ python_dotenv.load_dotenv(path, override=False)
  │     │     │
  │     │     └─ for k, v in parsed_pairs:
  │     │           if k not in os.environ: os.environ[k] = v
  │     │           # else: shell wins (override=False)
  │     └─ log.info("loaded .env from <path> (<N> keys)")
  │
  ├─ args.func(args)                     ◄── subcommand dispatch
  │     │
  │     ├─ _cmd_generate / _cmd_deploy / etc
  │     │     └─ EnvCredentialProvider().get("FAL_KEY")
  │     │           └─ os.environ.get("FAL_KEY")   ◄── sees .env value
  │     │
  │     └─ _build_store(cfg)
  │           └─ S3ArtifactStore(...) → boto3.client("s3")
  │                 └─ boto3 default chain reads os.environ["AWS_*"]
```

### Key invariant

`load_env_file` mutates `os.environ` exactly once, exactly at CLI startup,
exactly before any consumer can read it.  Every downstream secret consumer
(kinoforge's own `EnvCredentialProvider`, boto3 default chain,
google-cloud-storage default chain, any future SDK) is unmodified and reads
through `os.environ`.

---

## 6. Error handling

| Condition | Behavior | Rationale |
|---|---|---|
| `.env` absent at default path | Silent no-op | First-time users without `.env` still get existing `AuthError` on first secret use |
| `--env-file PATH` given, file absent | Raise `FileNotFoundError(path)` | Explicit path = explicit intent; silent miss would hide typos |
| `.env` present but unparseable | Propagate `python-dotenv` error | Surfaces the bad line; don't mask |
| `.env` present, parses fine | Log `INFO loaded .env from <path> (<N> keys)` | Confirms what was loaded without leaking values |
| `chmod` permissions check | Not enforced | OS file perms are user's responsibility; documented in README + `.env.example` |
| Required secret missing at use time | Existing `AuthError("missing FAL_KEY")` | Locked in D4 |
| Variable value leaks to logs | Never logged | INFO line shows count + path, never keys, never values |

No new error classes.  Reuses `FileNotFoundError` (stdlib) and whatever
`python-dotenv` raises for parse errors.  Reuses existing `AuthError` for
downstream missing-key cases.

---

## 7. Components / files touched

### New

- `src/kinoforge/core/dotenv_loader.py` (~15 LOC + module docstring)
- `tests/core/test_dotenv_loader.py` (8 unit tests)
- `.env.example` (repo root) — documents 4 known keys with comments

### Modified

- `src/kinoforge/cli.py` — top of `main()` adds `--env-file PATH` argparse +
  `load_env_file(args.env_file)` call before subcommand dispatch (~6 LOC)
- `tests/test_cli.py` — 2 integration tests for CLI flag plumbing
- `pixi.toml` — `pixi add python-dotenv` under `[dependencies]` (conda-forge)
- `.gitignore` — verify `.env` listed; add if absent
- `README.md` — new "Credentials" section: points at `.env.example`, explains
  shell-wins precedence, `chmod 600 .env`, never commit
- `PROGRESS.md` — Phase 14 entry

### Untouched (deliberately)

- `EnvCredentialProvider` (`core/credentials.py`) — no changes
- All engines, sources, providers, stores — no changes
- All existing tests — unaffected (no behavior change when `.env` absent)

### Boundary clarity

`dotenv_loader` knows about `python-dotenv` and filesystem paths.  Nothing
else does.  Swapping `python-dotenv` for `keyring`, `op run`, or a cloud
secrets manager later changes only this one file.

---

## 8. `.env.example` contract

Checked into git at repo root.  No values.  Comments document where to get
each key.

```
# kinoforge credentials — copy this file to .env and fill in values you need
# .env is in .gitignore; never commit your real .env file
# Recommended: chmod 600 .env

# fal.ai (hosted inference API)
# Get from: https://fal.ai/dashboard/keys
FAL_KEY=

# CivitAI (model source)
# Get from: https://civitai.com/user/account → API Keys
# Only required for gated / private models
CIVITAI_TOKEN=

# HuggingFace (model source)
# Get from: https://huggingface.co/settings/tokens (read-only token suffices)
# Only required for gated / private repos
HF_TOKEN=

# RunPod (compute provider)
# Get from: https://www.runpod.io/console/user/settings → API Keys
RUNPOD_API_KEY=
```

Updates to this file are required whenever a new hosted provider or
credentialed adapter is added.

---

## 9. Testing strategy

### 9.1 Unit tests — `tests/core/test_dotenv_loader.py`

All offline, `tmp_path` + `monkeypatch.setenv`/`delenv`.  No real `.env` in
test tree.

| # | Test | Bug it catches |
|---|---|---|
| 1 | `test_absent_default_path_is_silent_noop` | Returns None, no log, no exception when default `./. env` absent.  Catches: future refactor accidentally requiring the file. |
| 2 | `test_loads_keys_into_environ` | Write `tmp_path/.env` with `FAL_KEY=abc`, call loader, assert `os.environ["FAL_KEY"] == "abc"`.  Catches: parser regression. |
| 3 | `test_shell_value_wins_over_env_file` | `monkeypatch.setenv("FAL_KEY", "shell")`, write `.env` with `FAL_KEY=file`, load, assert env is `"shell"`.  Pins `override=False`.  Catches: silent flip of override semantics. |
| 4 | `test_env_file_fills_unset_keys` | Same setup, `CIVITAI_TOKEN` unset in shell + present in file → loaded.  Catches: regression where `override=False` blocks new keys. |
| 5 | `test_explicit_path_missing_raises_FileNotFoundError` | `load_env_file(tmp_path/"nope.env")` raises `FileNotFoundError`.  Catches: explicit miss silently swallowed. |
| 6 | `test_malformed_env_propagates_error` | Write `tmp_path/.env` with content that python-dotenv rejects (verified by reading upstream parser behavior during plan phase — likely an unparseable line such as a bare key with no `=`).  Assert error propagates.  Catches: silent corrupt-file regression. |
| 7 | `test_info_log_shows_count_and_path_not_values` | `caplog` captures INFO.  Assert log contains path + key count, does NOT contain any of the values.  Catches: accidental value leak via logging. |
| 8 | `test_two_calls_idempotent_under_override_false` | Call twice with same file → second call no-ops on already-set keys.  Catches: double-load weirdness. |

### 9.2 Integration tests — `tests/test_cli.py`

| # | Test | Bug it catches |
|---|---|---|
| 9 | `test_cli_loads_env_from_cwd_default` | `monkeypatch.chdir(tmp_path)`, write `.env` with `FAL_KEY=cwd-val`, run `main(["status"])`, assert `os.environ["FAL_KEY"] == "cwd-val"`.  Catches: CLI not calling loader. |
| 10 | `test_cli_env_file_flag_overrides_default` | Pass `--env-file <other>`, assert that file loaded not the cwd one.  Catches: flag plumbing broken. |

### 9.3 What is NOT tested

- `python-dotenv` internals (upstream's problem).
- `.env.example` content (it's documentation).
- `chmod` permission behavior (not enforced by kinoforge).

### 9.4 Adversarial review (per `test-design` skill)

- **Could a test pass under wrong behavior?**  Test 3 pins exact value
  `"shell"` not `"file"` — distinguishes the two outcomes.
- **Could implementation pass tests but fail in production?**  Test 9
  exercises real `main()` flow end-to-end.  Tests 5+6 cover failure modes
  most likely to be silently regressed.
- **Are tests asserting strong behavior?**  Yes — exact values, exact
  exception types, log content predicates, not "any log".
- **Mocking strategy:** only `monkeypatch` for env + `tmp_path` for FS.  No
  mocks of `python-dotenv` itself — use the real parser on real tmp files.

---

## 10. Security considerations

### 10.1 Threats mitigated

- **`.bashrc` plaintext** — secrets move out of shell config files into a
  project-scoped file the user can `chmod 600`.
- **Subprocess inheritance via global env** — partially mitigated: `.env`
  values still land in `os.environ` (necessary for SDK chains), so any
  subprocess kinoforge spawns inherits them.  Same risk as today; no
  regression.
- **Onboarding leak** — new users see `.env.example` and learn what is
  needed, rather than copy-pasting from chat logs or screenshots.

### 10.2 Threats NOT mitigated (acceptance)

- **`.env` accidentally committed** — `.gitignore` is the only safeguard.  No
  pre-commit hook scans for high-entropy strings.  Acceptable: standard
  industry practice; users can add `detect-secrets` or `gitleaks`
  independently.
- **`.env` file world-readable** — kinoforge does not enforce `chmod 600`.
  Documented in README and `.env.example`.  Cross-platform enforcement
  (Windows ACLs, mounted volumes) makes hard enforcement brittle.
- **Secrets in process memory dumps** — same as today; not a new exposure.

### 10.3 Defense in depth (built in)

- `load_env_file` INFO log shows path + key count, **never values**.
- No `__repr__` of credential objects logs values (existing behavior).
- `override=False` default means a malicious `.env` left by another process
  cannot clobber a CI-injected real key.

---

## 11. Migration / backwards compatibility

### Zero-impact migration

- Users with **no `.env` file** see no behavior change.  Existing exports in
  `.bashrc` continue to work.  Existing tests pass unchanged.
- Users **adopting `.env`** create one from `.env.example`, populate, and run
  as before.  Shell exports still take precedence (D3).
- **No CLI breaking changes.**  `--env-file PATH` is new and optional.

### Tags / version

Recommend cutting `v0.5.0` for this layer (post `v0.4.0` = S3/GCS).  Layer is
small enough to ship alone or bundle with the next layer.

---

## 12. Acceptance criteria (layer-level)

- L1: `pixi run pre-commit run --all-files` clean.
- L2: `pixi run typecheck` clean (mypy strict).
- L3: `pixi run test-cov` ≥ 90%.
- L4: All 8 unit tests in `tests/core/test_dotenv_loader.py` pass.
- L5: Both new CLI integration tests pass.
- L6: All 440 existing tests still pass (no regressions).
- L7: `.env.example` exists at repo root with the 4 documented keys.
- L8: `.gitignore` lists `.env` (verify; add if missing).
- L9: README has "Credentials" section pointing at `.env.example` +
  documenting shell-wins precedence + `chmod 600` recommendation.
- L10: `pixi.toml` lists `python-dotenv` under `[dependencies]` from
  conda-forge.
- L11: PROGRESS Phase 14 entry exists with commit SHAs.
- L12: `kinoforge --env-file /tmp/test.env status` works (manual smoke).

---

## 13. Deferred / explicitly not in scope

These were considered and rejected for this layer:

1. **`DotEnvCredentialProvider` (alternative B in brainstorm)** — breaks SDK
   default chains; requires plumbing through every consumer; defeats the
   "works with all parts" goal.
2. **Pre-flight required-keys check** — introduces a manifest concept that
   has to be maintained per adapter; lazy `AuthError` is simpler.
3. **`keyring` / `op run` / cloud KMS integration** — heavier deps; future
   layer if needed.  `dotenv_loader` is the swap point.
4. **Multiple `.env` file chain (`.env.local`, `.env.development`, etc.)** —
   YAGNI for solo-dev workflow; `--env-file PATH` covers the edge case.
5. **Auto-discover (walk up from cwd like git)** — surprises if parent dir
   has unrelated `.env`; rejected in Q2.
6. **`~/.config/kinoforge/.env` fallback** — breaks per-project key scoping;
   rejected in Q2.
7. **Pre-commit hook scanning for secrets** — out of scope; users can add
   `gitleaks` independently.

---

## 14. Open items for plan phase

- Confirm `python-dotenv`'s exact exception class for malformed input (Test
  6) before writing the test.
- Decide commit granularity: one atomic commit for the layer, or split into
  (a) module + tests, (b) CLI wiring, (c) `.env.example` + docs.
  Recommendation: split into 3 atomic commits matching the file-set
  boundaries.
- Confirm `pixi.toml` `[dependencies]` block syntax for adding
  `python-dotenv` (vs `[pypi-dependencies]` if conda-forge version is
  stale).

---

## 15. References

- Brainstorm session: 2026-05-30 (this file is the output).
- Audit of secret-access sites: see brainstorm Step 1 (in conversation).
- Carry-forward handoff context: `handoff_20260530-192536.md` §4.3, §8.13.
- Memory: `~/.claude/projects/-workspace/memory/feedback_verify_tags_before_recommending.md`.
