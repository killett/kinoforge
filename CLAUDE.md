# Project: kinoforge

## Session resume protocol (read this first, every session)
This project is built across multiple sessions, and a session can die mid-run — e.g. an API `400`
that poisons the conversation so every subsequent turn fails until it is cleared. On **every** new
or resumed session, before doing anything else:

1. Read `PROGRESS.md` at the repo root. It is the source of truth for where the build is.
2. Read the design doc and the implementation plan that `PROGRESS.md` points to.
3. Run `git log --oneline -20` to see what is already committed.
4. Resume from the first unchecked task in the plan. **Do not** redo work that is already committed.

If `PROGRESS.md` does not exist yet, you are at the very start of the project; create it as soon as
a design or plan exists (see Durability rules).

## Durability rules (always)
- **Git is the source of truth, not the conversation.** Commit after every completed task or
  passing test, with a clear message. Never end a step with completed work left uncommitted.
- **Keep `PROGRESS.md` current.** It must contain: the design-doc path, the plan path, the task
  checklist (each item done / in-progress / next), key decisions and gotchas, and the single next
  action. Update and commit it after each task.
- **Persist the brainstorm as it forms.** During brainstorming, append each validated design
  section to the design doc and commit it — never leave the agreed design only in the conversation.
- **Commit RED scaffolds before any live spend.** Any tool, script, or fixture an agent generates
  whose purpose is to drive live cloud, paid API, or network spend MUST be committed (RED is fine —
  failing tests, xfail markers, or scaffold-only impl) BEFORE the spend is invoked. Reason: a
  mid-spend crash that loses the uncommitted scaffold forces the next session to redo the work
  before retrying the spend, and tempts a `git checkout .` cleanup that wipes 100+ LOC. Rule applies
  to subagents too — controller must verify the scaffold is committed (atomic, even just the
  scaffold + a failing test) before dispatching the live-spend subagent.
- **Run `pixi run preflight` before any live spend.** Checks RUNPOD/HF creds present (auto-loaded
  from `.env`), zero active RunPod pods, clean working tree. Exit 0 == safe to spend. See
  `tools/preflight.py` for the contract. There is NO operator-side env-switch — Claude runs inside
  a container and the user does not (and should not) need to `docker exec` to flip a flag. Live
  spend is authorised by user statement in conversation, not by env-var ceremony.
- **Log every qualifying successful generation.** Any kinoforge generation that produces a video AND
  introduces a new capability axis (new mode — t2v / i2v / flf2v / keyframe — new provider, engine,
  model, or YAML shape that changes the reproduction recipe, new kinoforge command, etc.) AND was
  NOT run with the `--ephemeral` flag MUST get a new detailed section in
  `/workspace/successful-generations.md` per the schema in that file's preamble. Same-tuple
  `(provider, engine, model, mode)` repeats get a "See also" line under the existing TOC entry, not
  a new section. Generations invoked with `--ephemeral` must NEVER appear in that file.

## Process & testing
- **Superpowers owns the workflow:** brainstorm → plan → execute, with red/green TDD and two-stage
  review. Follow its skills for test structure, fixtures, mocking style, naming, and granularity —
  do not impose a competing test process.
- **Spec vs. this file:** the requirements (the *what*) live in the build brief you were handed.
  This file owns the *how* — process and durability. Where they overlap on process, defer to
  Superpowers; the durability rules above are additive.

## Environment & tools

- Use `rg` instead of `grep`, `fd` instead of `find`.

## Cloud CLI invocation (`gcloud`, `aws`, `sky`)

`gcloud` and `aws` binaries live ONLY in the `live-skypilot` pixi env, NOT
the `default` env. Common failure mode (avoid):

```
$ pixi run -- gcloud config get-value project
# wrong — pixi run is the task runner; without a matching task name
# it just prints the task list. No error, just confusing output.

$ gcloud config get-value project
# wrong — gcloud not on PATH in the bare shell.
/bin/bash: line 1: gcloud: command not found
```

Working invocations:

```bash
# Option A — drop into live-skypilot env for one command
pixi run -e live-skypilot gcloud config get-value project
pixi run -e live-skypilot aws sts get-caller-identity
pixi run -e live-skypilot sky check

# Option B — direct PATH + cred env vars (when pixi env activation isn't on)
PATH="/workspace/.pixi/envs/live-skypilot/share/google-cloud-sdk-570.0.0-0/bin:/workspace/.pixi/envs/live-skypilot/bin:$PATH" \
  CLOUDSDK_CONFIG=/workspace/.gcp/gcloud-config \
  GOOGLE_APPLICATION_CREDENTIALS=/workspace/.gcp/kinoforge-sa.json \
  AWS_SHARED_CREDENTIALS_FILE=/workspace/.aws/credentials \
  AWS_CONFIG_FILE=/workspace/.aws/config \
  gcloud config get-value project
```

**Why the env vars matter:** `pixi.toml [activation.env]` wires
`AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`,
`GOOGLE_APPLICATION_CREDENTIALS`, and `CLOUDSDK_CONFIG` so that
`pixi run ...` automatically finds workspace-local creds. Outside of a
`pixi run` invocation those env vars are NOT set — the bare `aws` /
`gcloud` will report `Unable to locate credentials`.

For Python code (boto3, google-cloud-*), prefer `pixi run python -m
<module>` so activation fires and SDK default chains pick up the
workspace creds automatically.

Quick identity probe pattern:

```bash
pixi run -e live-skypilot gcloud config list account --format='value(core.account)'
pixi run -e live-skypilot aws sts get-caller-identity
```

## Workspace scaffolding

This project has already been scaffolded. The following files
already exist and should NOT be recreated:

- `pyproject.toml` — project metadata + ruff/mypy/pytest/coverage config
- `pixi.toml` — pixi workspace with dev dependencies (ruff, mypy, pytest, pytest-cov, pre-commit)
- `.pre-commit-config.yaml` — pre-commit hooks (ruff, ruff-format, mypy, trailing-whitespace, end-of-file-fixer, etc.)
- `.gitignore` — standard Python ignores

**Do NOT run `pixi init` or `pre-commit sample-config`.**

## Project layout

Use a `src/` layout:

```
src/
  <package_name>/
    __init__.py
    __main__.py
    ...
tests/
  __init__.py
  test_*.py
```

The package name should be the project name normalized to a valid Python identifier
(lowercase, spaces/hyphens replaced with underscores).

## Running tools

All dev tools are installed via pixi. Use `pixi run` to invoke them:

- `pixi run test` — run tests (pytest)
- `pixi run lint` — lint (ruff check .)
- `pixi run format` — format (ruff format .)
- `pixi run typecheck` — type check (mypy .)
- `pixi run pre-commit run` — run pre-commit on staged files
- `pixi run pre-commit run --files <path>` — run pre-commit on specific files
- `pixi run pre-commit run --all-files` — run pre-commit on every file
- `pixi run pre-commit install` — install the git pre-commit hook
  (`pre-commit` is only available via `pixi run` — no system binary)

To add a new dependency: `pixi add <package>`
To add a PyPI-only dependency: `pixi add --pypi <package>`

## First-time setup

Already done by the container entrypoint — git repo
initialized, git identity configured, dependencies
installed, pre-commit hooks active, initial scaffold
committed.
