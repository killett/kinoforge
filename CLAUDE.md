# Project: kinoforge

## Environment & tools

- Use `rg` instead of `grep`, `fd` instead of `find`.

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
