# .env secrets loader — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a project-root `.env` file as kinoforge's canonical single source of API credentials, loaded transparently at CLI startup via `python-dotenv`, with zero changes to existing engines/sources/providers/stores.

**Architecture:** New module `kinoforge.core.dotenv_loader` exposes one function `load_env_file(path, *, override=False)`. CLI's `main()` calls it once before subcommand dispatch, with an optional `--env-file PATH` flag to override the default `./.env`. `EnvCredentialProvider` and every other secret consumer reads `os.environ` unchanged — the shim populates that dict before any consumer wakes up. AWS/GCP SDKs read their default credential chains (env → files → IMDS) unmodified; users can put `AWS_*` / `GOOGLE_APPLICATION_CREDENTIALS` in `.env` if they want, and the SDK chains pick them up automatically.

**Tech Stack:** `python-dotenv` (conda-forge), pytest, monkeypatch, argparse, pathlib.

**Spec:** [`docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md`](../specs/2026-05-30-dotenv-secrets-design.md).

---

## Task 1: Infrastructure (deps, .gitignore, .env.example)

**Goal:** Wire `python-dotenv` as a dependency, ensure `.env` is gitignored, and check in `.env.example` so users know what keys to populate.

**Files:**
- Modify: `pixi.toml` — add `python-dotenv = "*"` under `[dependencies]`
- Modify: `.gitignore` — add `.env` (currently absent — critical gap)
- Create: `.env.example` (repo root)

**Acceptance Criteria:**
- [ ] `pixi list | rg python-dotenv` shows the package present in the env
- [ ] `cat .gitignore | rg "^\.env$"` finds the entry
- [ ] `.env.example` exists at repo root with documented entries for `FAL_KEY`, `CIVITAI_TOKEN`, `HF_TOKEN`, `RUNPOD_API_KEY`
- [ ] `pixi run pre-commit run --all-files` clean

**Verify:** `pixi list 2>&1 | rg python-dotenv && rg "^\.env$" .gitignore && ls .env.example`

**Steps:**

- [ ] **Step 1: Add `python-dotenv` to pixi.toml**

Run:

```bash
pixi add python-dotenv
```

This appends `python-dotenv = "*"` under `[dependencies]` and updates `pixi.lock`.

Verify:

```bash
pixi list 2>&1 | rg python-dotenv
```

Expected: line showing `python-dotenv` with a version (conda-forge package).

- [ ] **Step 2: Add `.env` to `.gitignore`**

Open `.gitignore`. Find the existing "Editor + OS" / "Local-only working scratch" sections. Add a new section before them or append:

```
# Secrets — never commit .env
.env
```

Verify:

```bash
rg "^\.env$" .gitignore
```

Expected: one line `.env`.

- [ ] **Step 3: Create `.env.example` at repo root**

Create `/workspace/.env.example` with this exact content (no values, comments only):

```bash
# kinoforge credentials — copy this file to .env and fill in values you need
# .env is in .gitignore; never commit your real .env file
# Recommended: chmod 600 .env
#
# Shell environment variables (export FAL_KEY=... in your shell or CI) ALWAYS
# win over .env values — .env only fills variables that are otherwise unset.

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

- [ ] **Step 4: Run pre-commit, verify clean**

Run:

```bash
pixi run pre-commit run --all-files
```

Expected: all hooks Passed.

- [ ] **Step 5: Commit**

```bash
git add pixi.toml pixi.lock .gitignore .env.example
git commit -m "$(cat <<'EOF'
chore: add python-dotenv dep + .env.example + gitignore .env

Layer-D infra: prepares for dotenv-loader module (next commit).
Critical: .env was NOT previously in .gitignore — closing that gap
before any real credentials hit the repo. .env.example checked in
to document required kinoforge credential variables (FAL_KEY,
CIVITAI_TOKEN, HF_TOKEN, RUNPOD_API_KEY).

Refs: docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md
EOF
)"
```

---

## Task 2: `dotenv_loader` module + unit tests

**Goal:** Implement `kinoforge.core.dotenv_loader.load_env_file(path, *, override=False)` with TDD-first tests covering silent no-op, parse + populate, shell-wins precedence, explicit-path-missing, malformed-file propagation, log content, and idempotency.

**Files:**
- Create: `src/kinoforge/core/dotenv_loader.py`
- Create: `tests/core/test_dotenv_loader.py`

**Acceptance Criteria:**
- [ ] `load_env_file(None)` is a silent no-op when `./. env` does not exist
- [ ] `load_env_file(path)` populates `os.environ` from a real `.env` file
- [ ] Shell-set values win over `.env` values (override=False default)
- [ ] `.env` values fill keys that are unset in the shell
- [ ] Explicit path missing raises `FileNotFoundError(path)`
- [ ] Malformed file content propagates the underlying parser error
- [ ] INFO log shows path + key count, never values
- [ ] Calling twice with same file under override=False is idempotent
- [ ] All 8 tests pass via `pixi run test tests/core/test_dotenv_loader.py -v`
- [ ] `pixi run pre-commit run --all-files` clean
- [ ] `pixi run typecheck` clean (mypy strict)

**Verify:** `pixi run test tests/core/test_dotenv_loader.py -v` → 8 passed

**Steps:**

- [ ] **Step 1: Write all 8 failing tests first**

Create `/workspace/tests/core/test_dotenv_loader.py`:

```python
"""Tests for kinoforge.core.dotenv_loader.

All tests are offline. They use ``tmp_path`` for the .env file and
``monkeypatch`` for ``os.environ`` mutations so the host environment is
never touched.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from kinoforge.core.dotenv_loader import load_env_file


def _write_env(path: Path, content: str) -> None:
    """Write *content* to *path* with a trailing newline."""
    path.write_text(content + "\n", encoding="utf-8")


def test_absent_default_path_is_silent_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No .env at default path → no-op, no log, no exception."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAL_KEY", raising=False)
    caplog.set_level(logging.INFO, logger="kinoforge.core.dotenv_loader")

    load_env_file()  # default path = cwd/.env, which does not exist

    assert os.environ.get("FAL_KEY") is None
    assert caplog.records == []


def test_loads_keys_into_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env containing FAL_KEY=abc populates os.environ['FAL_KEY']."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=abc")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "abc"


def test_shell_value_wins_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shell-set value persists; .env value is ignored (override=False)."""
    monkeypatch.setenv("FAL_KEY", "shell")
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=file")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "shell"


def test_env_file_fills_unset_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keys absent from the shell are filled from the .env file."""
    monkeypatch.setenv("FAL_KEY", "shell")
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=file\nCIVITAI_TOKEN=fromfile")

    load_env_file(env_file)

    assert os.environ.get("FAL_KEY") == "shell"
    assert os.environ.get("CIVITAI_TOKEN") == "fromfile"


def test_explicit_path_missing_raises_FileNotFoundError(tmp_path: Path) -> None:
    """An explicitly passed missing path raises FileNotFoundError."""
    missing = tmp_path / "nope.env"

    with pytest.raises(FileNotFoundError, match=str(missing)):
        load_env_file(missing)


def test_malformed_env_propagates_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .env file with content python-dotenv rejects raises (not swallowed).

    python-dotenv is fairly permissive; the most reliably-failing case across
    versions is a file containing a non-UTF-8 byte sequence which fails
    decoding during read. The implementation MUST surface this rather than
    silently treating it as empty.
    """
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfeFAL_KEY=abc\n")  # invalid UTF-8 leading bytes

    with pytest.raises(Exception):  # underlying UnicodeDecodeError or dotenv variant
        load_env_file(env_file)


def test_info_log_shows_count_and_path_not_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """INFO log mentions path + count, never the secret values."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("CIVITAI_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=secret_value_abc\nCIVITAI_TOKEN=tok_xyz")
    caplog.set_level(logging.INFO, logger="kinoforge.core.dotenv_loader")

    load_env_file(env_file)

    messages = "\n".join(rec.getMessage() for rec in caplog.records)
    assert str(env_file) in messages
    assert "2" in messages  # key count
    assert "secret_value_abc" not in messages
    assert "tok_xyz" not in messages


def test_two_calls_idempotent_under_override_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second call leaves already-set values unchanged."""
    monkeypatch.delenv("FAL_KEY", raising=False)
    env_file = tmp_path / ".env"
    _write_env(env_file, "FAL_KEY=first")

    load_env_file(env_file)
    assert os.environ.get("FAL_KEY") == "first"

    # Rewrite the file with a different value; without override=True the
    # already-set value MUST persist.
    _write_env(env_file, "FAL_KEY=second")
    load_env_file(env_file)
    assert os.environ.get("FAL_KEY") == "first"
```

- [ ] **Step 2: Run tests to verify they fail (red)**

```bash
pixi run test tests/core/test_dotenv_loader.py -v
```

Expected: ImportError or 8 errors, all with `ModuleNotFoundError: No module named 'kinoforge.core.dotenv_loader'`.

- [ ] **Step 3: Implement `dotenv_loader.py`**

Create `/workspace/src/kinoforge/core/dotenv_loader.py`:

```python
"""Load environment variables from a project-root .env file.

Single-purpose module. Exposes one function, :func:`load_env_file`, which is
called once at CLI startup (see :func:`kinoforge.cli.main`) to populate
``os.environ`` with values from a ``.env`` file. Every downstream secret
consumer — :class:`kinoforge.core.credentials.EnvCredentialProvider`, the
boto3 default credential chain, the google-cloud-storage default credential
chain — reads ``os.environ`` unchanged.

Design contract:
- Shell-set values win (``override=False`` default).
- Default path is ``Path.cwd() / ".env"``; absent default file is a silent no-op.
- An explicitly-passed missing path raises :class:`FileNotFoundError`.
- INFO log on successful load shows the path + key count, never values.

See ``docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md`` for the
full design contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_LOG = logging.getLogger(__name__)


def load_env_file(
    path: Path | None = None, *, override: bool = False
) -> None:
    """Load environment variables from a .env file into ``os.environ``.

    Args:
        path: Path to the .env file. Defaults to ``Path.cwd() / ".env"``.
            When the default path does not exist, the call is a silent no-op.
            When an explicit *path* is provided and does not exist, raises
            :class:`FileNotFoundError`.
        override: When ``False`` (default), existing ``os.environ`` values
            win and ``.env`` only fills unset keys. When ``True``, ``.env``
            values overwrite existing ``os.environ`` values.

            The CLI always calls with the default ``False``; ``override=True``
            is exposed for library users who explicitly want ``.env`` to
            clobber existing values.

    Raises:
        FileNotFoundError: When *path* is explicitly provided but does not
            exist on disk.
    """
    explicit = path is not None
    resolved = path if path is not None else Path.cwd() / ".env"

    if not resolved.exists():
        if explicit:
            raise FileNotFoundError(str(resolved))
        return

    parsed = dotenv_values(resolved)
    load_dotenv(resolved, override=override)
    _LOG.info(
        "loaded .env from %s (%d keys)", str(resolved), len(parsed)
    )
```

Notes for the implementer:
- Use `dotenv_values()` to get the parsed key count for the log (this also
  forces the parser to run, surfacing malformed-file errors).
- `load_dotenv(resolved, override=override)` does the actual `os.environ`
  mutation.
- Both calls happen on the same path; doing them separately keeps the log
  honest (count comes from parsing, not from inspecting `os.environ`
  afterward).

- [ ] **Step 4: Run tests to verify they pass (green)**

```bash
pixi run test tests/core/test_dotenv_loader.py -v
```

Expected: 8 passed.

If `test_malformed_env_propagates_error` fails because the `\xff\xfe` bytes
do NOT raise (some python-dotenv versions silently coerce), pick an
alternative malformed payload. Candidate: a file with a single line
`KEY"=value` (unclosed quote) — verify against the installed
`python-dotenv` version. If no payload reliably raises across versions, this
test becomes `xfail` with a note explaining python-dotenv is more permissive
than expected, and §6 of the spec's "Error handling" row for malformed input
should be revised in a follow-up commit to reflect actual behavior.

- [ ] **Step 5: Type-check and lint**

```bash
pixi run typecheck
pixi run pre-commit run --files src/kinoforge/core/dotenv_loader.py tests/core/test_dotenv_loader.py
```

Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/dotenv_loader.py tests/core/test_dotenv_loader.py
git commit -m "$(cat <<'EOF'
feat(dotenv): add load_env_file() with shell-wins precedence

New module kinoforge.core.dotenv_loader exposes one function,
load_env_file(path, *, override=False), that reads a .env file and
populates os.environ. Shell values always win when override=False
(the default and CLI-only path); .env fills unset keys. Explicit
missing path raises FileNotFoundError; default missing path is a
silent no-op. INFO log shows path + key count, never values.

8 unit tests cover silent no-op, parse + populate, precedence,
explicit-miss, malformed-file propagation, log content, and
idempotency. All offline via tmp_path + monkeypatch.

CLI wiring + .env.example come in follow-up commits.

Refs: docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md
EOF
)"
```

---

## Task 3: CLI wiring + integration tests

**Goal:** Add a top-level `--env-file PATH` argparse flag to the kinoforge CLI and call `load_env_file(args.env_file)` from `main()` before subcommand dispatch. Cover with 2 integration tests proving the cwd-default and the flag override.

**Files:**
- Modify: `src/kinoforge/cli.py` — `_build_parser` adds `--env-file`; `main()` calls loader before `_print_instance_overview`
- Modify: `tests/test_cli.py` — append 2 integration tests

**Acceptance Criteria:**
- [ ] `kinoforge --help` shows the `--env-file PATH` flag
- [ ] When `cwd/.env` exists, `main(["status", "--id", "anything"])` (or any subcommand) populates `os.environ` from it before subcommand work
- [ ] When `--env-file PATH` is passed, that file is loaded instead of the cwd default
- [ ] The 2 new integration tests in `tests/test_cli.py` pass
- [ ] All 440 pre-existing tests still pass
- [ ] `pixi run pre-commit run --all-files` clean
- [ ] `pixi run typecheck` clean

**Verify:** `pixi run test tests/test_cli.py -v` → all CLI tests including the 2 new ones pass; `pixi run test` → all tests pass

**Steps:**

- [ ] **Step 1: Write the 2 failing integration tests first**

Append to `/workspace/tests/test_cli.py` (after the existing tests):

```python
# ---------------------------------------------------------------------------
# .env loader integration (Task 3 of dotenv-secrets plan)
# ---------------------------------------------------------------------------


def test_cli_loads_env_from_cwd_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() loads ./.env from cwd before subcommand dispatch."""
    from kinoforge.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KINOFORGE_TEST_ENV_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "KINOFORGE_TEST_ENV_KEY=cwd-value\n", encoding="utf-8"
    )

    # `list` is a no-arg subcommand that exits 0 cleanly under empty state.
    rc = main(["--state-dir", str(tmp_path / "state"), "list"])
    assert rc == 0

    assert os.environ.get("KINOFORGE_TEST_ENV_KEY") == "cwd-value"


def test_cli_env_file_flag_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--env-file PATH loads that file instead of the cwd default."""
    from kinoforge.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KINOFORGE_TEST_ENV_KEY", raising=False)

    # Default cwd .env that we should NOT load.
    (tmp_path / ".env").write_text(
        "KINOFORGE_TEST_ENV_KEY=cwd-value\n", encoding="utf-8"
    )

    # Explicit file we SHOULD load.
    custom = tmp_path / "custom.env"
    custom.write_text(
        "KINOFORGE_TEST_ENV_KEY=custom-value\n", encoding="utf-8"
    )

    rc = main(
        [
            "--env-file", str(custom),
            "--state-dir", str(tmp_path / "state"),
            "list",
        ]
    )
    assert rc == 0

    assert os.environ.get("KINOFORGE_TEST_ENV_KEY") == "custom-value"
```

Also add `import os` to the imports at the top of `tests/test_cli.py` if not already present.

- [ ] **Step 2: Run new tests to verify they fail (red)**

```bash
pixi run test tests/test_cli.py::test_cli_loads_env_from_cwd_default tests/test_cli.py::test_cli_env_file_flag_overrides_default -v
```

Expected: 2 failed. Either:
- `unrecognized arguments: --env-file` from argparse (if `_build_parser` not updated yet), or
- `KeyError` / `None` assertion failure (if argparse accepts it but main doesn't call the loader).

- [ ] **Step 3: Modify `_build_parser` to add `--env-file`**

Open `src/kinoforge/cli.py`. Find `_build_parser` (line 118). After the existing `--state-dir` argument block (ends ~line 136), add the new argument:

```python
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help=(
            "path to a .env file containing kinoforge credentials "
            "(default: ./.env if it exists; absent default is silent)"
        ),
    )
```

- [ ] **Step 4: Modify `main()` to call the loader**

Open `src/kinoforge/cli.py`. Find `main()` (line 503). After `args = parser.parse_args(argv)` and **before** `_print_instance_overview(state_dir)`, insert the load call:

```python
    # Load secrets from .env (default: cwd/.env; explicit via --env-file).
    # Shell-set values always win (override=False).
    env_file = Path(args.env_file) if args.env_file is not None else None
    load_env_file(env_file)
```

Also add the import at the top of `cli.py` alongside the existing imports:

```python
from kinoforge.core.dotenv_loader import load_env_file
```

The full main() prelude after the change reads:

```python
def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)

    # Load secrets from .env (default: cwd/.env; explicit via --env-file).
    # Shell-set values always win (override=False).
    env_file = Path(args.env_file) if args.env_file is not None else None
    load_env_file(env_file)

    # Print instance overview on every invocation (before subcommand work).
    _print_instance_overview(state_dir)

    # ...rest unchanged...
```

- [ ] **Step 5: Run new tests to verify they pass (green)**

```bash
pixi run test tests/test_cli.py::test_cli_loads_env_from_cwd_default tests/test_cli.py::test_cli_env_file_flag_overrides_default -v
```

Expected: 2 passed.

- [ ] **Step 6: Run full test suite — verify no regressions**

```bash
pixi run test
```

Expected: 442 passed (440 pre-existing + 2 new). Zero regressions.

- [ ] **Step 7: Verify `--help` shows the new flag**

```bash
pixi run python -m kinoforge --help 2>&1 | rg env-file
```

Expected: one line mentioning `--env-file PATH`.

- [ ] **Step 8: Type-check and lint**

```bash
pixi run typecheck
pixi run pre-commit run --files src/kinoforge/cli.py tests/test_cli.py
```

Expected: both clean.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat(cli): wire --env-file flag + auto-load cwd/.env at startup

main() now calls load_env_file(args.env_file) once before subcommand
dispatch. Default path is cwd/.env (silent no-op if absent); explicit
--env-file PATH overrides and raises FileNotFoundError if absent.
Every downstream consumer (EnvCredentialProvider, boto3 + GCS default
chains) reads the populated os.environ transparently — zero changes
to any engine, source, provider, or store.

2 integration tests cover the cwd-default and flag-override paths.
All 442 tests pass (440 prior + 2 new).

Refs: docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md
EOF
)"
```

---

## Task 4: README Credentials section + PROGRESS Phase 14 entry

**Goal:** Document the new flow for users and add the layer to the progress index.

**Files:**
- Modify: `README.md` — new "Credentials" section
- Modify: `PROGRESS.md` — Phase 14 entry

**Acceptance Criteria:**
- [ ] README has a top-level or sub-section "Credentials" (or "Configuration → Credentials") that mentions: `.env.example`, the 4 known keys, shell-wins precedence, `chmod 600 .env`, never commit
- [ ] `PROGRESS.md` Phase 14 lists Tasks 1–4 with their commit SHAs (gathered after Tasks 1–3 land) and re-points "Single next action" to the next layer candidate
- [ ] `pixi run pre-commit run --all-files` clean

**Verify:** `rg -A 5 "## Credentials|### Credentials" README.md && rg "Phase 14" PROGRESS.md`

**Steps:**

- [ ] **Step 1: Inspect README structure**

```bash
rg "^##" README.md
```

Find an appropriate insertion point. Likely between "Quickstart" / "Installation" and "Extending" / "Roadmap".

- [ ] **Step 2: Add Credentials section to README.md**

Insert a new section (level chosen to match existing README structure — probably `##`):

```markdown
## Credentials

Kinoforge reads its API credentials from environment variables. To avoid
exporting them in `~/.bashrc`, copy the checked-in template:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and fill in the keys you need.
```

The CLI auto-loads `./.env` at startup. To use a different file:

```bash
kinoforge --env-file /path/to/other.env generate --config ...
```

### Precedence

Shell-set values **always win** over `.env` values. This means CI/prod
exports always take precedence over a stale dev `.env`. To override
this behavior in your own scripts, call
`kinoforge.core.dotenv_loader.load_env_file(path, override=True)` from
Python.

### Known keys

| Variable | Used by | Required when |
|---|---|---|
| `FAL_KEY` | `HostedAPIEngine` (fal.ai) | Hosted engine path against fal.ai |
| `CIVITAI_TOKEN` | `CivitAISource` | Downloading gated/private CivitAI models |
| `HF_TOKEN` | `HuggingFaceSource` | Downloading gated/private HF repos |
| `RUNPOD_API_KEY` | `RunPodProvider` | Provisioning RunPod compute |

AWS / GCP credentials are NOT managed by kinoforge — the `boto3` and
`google-cloud-storage` SDKs walk their own default credential chains
(env → `~/.aws/credentials` → IMDS → IAM role / ADC → gcloud config →
GCE metadata) unchanged. You may put `AWS_ACCESS_KEY_ID`,
`GOOGLE_APPLICATION_CREDENTIALS`, etc. into your `.env` if you prefer
a single file; the SDK chains pick them up via `os.environ`.

### Never commit `.env`

`.env` is in `.gitignore`. Only commit `.env.example` (no values).
```

- [ ] **Step 3: Update PROGRESS.md with Phase 14 entry**

Open `PROGRESS.md`. Find the existing Phase 13 block. After it, add Phase 14
(use the actual commit SHAs gathered from `git log --oneline` after Tasks
1–3 land — replace `<SHA>` placeholders):

```markdown
### Phase 14 — .env secrets loader (post-MVP Layer D)
- [x] Task 1: python-dotenv dep + .gitignore .env + .env.example — commit `<SHA1>`
- [x] Task 2: dotenv_loader module + 8 unit tests — commit `<SHA2>`
- [x] Task 3: CLI --env-file flag + 2 integration tests — commit `<SHA3>`
- [x] Task 4: README Credentials section + PROGRESS Phase 14 entry — commit `<SHA4>` (this commit)
```

Then update "Single next action" to point to the next chosen layer (per the
three candidates currently listed: per-engine `extract_last_frame`,
ConcurrentPool issue #3, or keyframe stage issue #4). If unsure, leave the
existing three-candidate list intact and note: "Layer D shipped; next
layer choice pending."

- [ ] **Step 4: Run pre-commit**

```bash
pixi run pre-commit run --all-files
```

Expected: all hooks Passed.

- [ ] **Step 5: Commit**

```bash
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: README Credentials section + PROGRESS Phase 14 entry

Documents the new .env workflow for users: .env.example, shell-wins
precedence, chmod 600, known-keys table, AWS/GCP carve-out. PROGRESS
gains Phase 14 listing all four atomic commits.

Closes Layer D (dotenv secrets loader).

Refs: docs/superpowers/specs/2026-05-30-dotenv-secrets-design.md
EOF
)"
```

---

## Final verification

After all 4 tasks land:

```bash
pixi run pre-commit run --all-files     # Expected: clean
pixi run typecheck                      # Expected: clean
pixi run test                           # Expected: 442 passed (440 + 2)
pixi run test-cov                       # Expected: >= 90% coverage
pixi run python -m kinoforge --help     # Expected: --env-file listed
```

Recommend cutting `v0.5.0` for the Layer D boundary after pushing to
`origin/main`:

```bash
git tag -a v0.5.0 -m "kinoforge v0.5.0 — .env secrets loader (Layer D)"
git push origin v0.5.0
```

(Run outside container since `gh` / push perms live there.)
