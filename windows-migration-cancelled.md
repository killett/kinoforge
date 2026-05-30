# Plan: Fix windows-latest CI failure by adding real win-64 support

## Context

The `Test (windows-latest)` job at
https://github.com/killett/kinoforge/actions/runs/26671821360/job/78616380633
fails in 13s. The visible error is `ENOENT: lstat 'D:\a\kinoforge\kinoforge\.pixi'`
in Post-job cleanup, but that's a symptom: `setup-pixi` couldn't create `.pixi`
because **pixi.lock has zero `win-64` entries**. `pixi.toml:32` declares only
`platforms = ["linux-64", "osx-arm64", "osx-64"]`, so the lock never solved for
Windows. Every Windows CI run since the matrix was set up has been silently red.

User decision: **add real win-64 support** (vs the simpler option of dropping
windows-latest from the matrix). Goal: a single SHA on `main` whose CI is green
on all three OSes, with `pixi.lock` consistent across all four platforms.

## Implementation plan

### Phase 1 — pixi.toml: enable win-64 + gate Unix-only convenience tools

**File:** `/workspace/pixi.toml`

- Line 32: add `"win-64"` to `platforms`.
- Trim `[dependencies]` to the portable runtime + QA core. Keep:
  `python`, `ruff`, `mypy`, `pytest`, `pytest-cov`, `pre-commit`, `requests`,
  `tenacity`, `tqdm`, `pydantic`, `pyyaml`, `types-pyyaml`.
- Remove `openjdk = "=25"` outright — grep confirms zero references in
  `src/`, `tests/`, `examples/`, `.pre-commit-config.yaml`, CI.
- Move convenience tools into existing per-target sections (already used for
  `linux-64`). No `[feature]` split needed — per-target dependencies are
  sufficient and avoid a `pixi shell -e dev` workflow change:

  ```toml
  [target.linux-64.dependencies]
  ipython = "*"
  ipdb = "*"
  py-spy = "*"
  shellcheck = "*"
  go-shfmt = "*"
  taplo = "*"
  git-delta = "*"
  difftastic = "*"
  hyperfine = "*"
  go-yq = "*"

  [target.osx-64.dependencies]
  # same list — drop any package that has no osx-64 conda-forge build;
  # surfaced authoritatively by Phase 2 solve.

  [target.osx-arm64.dependencies]
  # same list — same caveat.
  ```

**Success criterion:** `pixi.toml` declares 4 platforms; `[dependencies]` lists
only portable packages; convenience tools live under per-target Unix sections.

### Phase 2 — Regenerate `pixi.lock` for all 4 platforms

**Command (run locally on Linux):**

```bash
rm pixi.lock
pixi install
```

`pixi install` solves every declared platform in one pass and writes the new
lock. Then sanity:

```bash
pixi lock --check
rg "^- name:" pixi.lock | sort -u   # should list all 4 platforms
```

**Expected outcome:** new `pixi.lock` containing solved package sets for
`linux-64`, `osx-arm64`, `osx-64`, `win-64`. `win-64` should be free of the
Unix-only tools.

**Failure handling:** if a *core* package fails to solve on win-64 (unlikely —
`pydantic`/`pyyaml`/`requests`/`tenacity`/`tqdm` all have win-64 conda-forge
builds), pin or move to `[pypi-dependencies]`. If an *osx* solve fails, drop
the offending tool from that target. Do **not** drop win-64 from `platforms`.

### Phase 3 — Fix the Windows test blocker

**File:** `/workspace/src/kinoforge/stores/local.py:134`

`str(p.relative_to(run_dir))` returns `profiles\abc.json` on Windows; the test
at `/workspace/tests/stores/test_local.py:105` asserts
`"profiles/abc.json" in names`. Fix:

```python
return [p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*") if p.is_file()]
```

Documented in the ABC contract anyway — `name` is a forward-slash-joined
relative path (see `local.py:50` docstring: "may contain forward slashes").

**TDD:** add a `tests/stores/test_local.py` regression test that puts a
nested name (`a/b/c.bin`) and asserts the returned name uses `/` exactly —
that test would fail today on Windows with backslashes. Then apply the fix
and confirm green on both Linux and Windows CI.

### Phase 4 — Make `check-added-large-files` Windows-portable

**File:** `/workspace/.pre-commit-config.yaml:34-37`

Current hook is `bash -c '...wc -c...'`. `windows-latest` runners have no
`bash` on PATH by default. Rewrite as pure Python through `pixi run python`,
matching the existing `check-toml` shape at line 45:

```yaml
- id: check-added-large-files
  name: check-added-large-files (limit 500 KB)
  entry: 'pixi run python -c ''import sys; max=500000; rc=0
[loop over sys.argv[1:], print and rc=1 if size>max]; sys.exit(rc)'''
  language: system
```

Verify locally: `pixi run pre-commit run --all-files`.

### Phase 5 — Push and observe Windows CI; iterate on test failures

**Commits (atomic, conventional):**

1. `feat(pixi): enable win-64 platform, gate Unix-only convenience tools`
   (touches `pixi.toml`, `pixi.lock`).
2. `fix(stores/local): use as_posix() for list() so names round-trip on Windows`
   (touches `src/kinoforge/stores/local.py`, `tests/stores/test_local.py`).
3. `fix(pre-commit): rewrite check-added-large-files in python for Windows`
   (touches `.pre-commit-config.yaml`).

Push. CI runs the 3-OS matrix.

**Triage ordering for Windows-only failures:**

- **setup-pixi fails:** win-64 lock missing → re-run Phase 2.
- **lint fails on Windows only:** likely CRLF/encoding — add `.gitattributes`
  with `* text=auto eol=lf` if missing. Iterate.
- **typecheck fails on Windows only:** check stub package only declared on
  Unix; move into portable core.
- **tests fail:** one fix per failure, each its own commit (Phase 6).

Revert only if Phase 1 wedges Linux/macOS (regression in green platforms).
Windows-only failures are fixed forward.

### Phase 6 — Soak any remaining test failures

The Windows audit flagged one BLOCKER (Phase 3 above). Other `/tmp` usages in
`tests/engines/test_comfyui.py:195,210,227-229` and `tests/stores/test_local.py:147`
are string literals fed to spies or used as deliberately-missing-path probes;
they probably pass on Windows. If any does fail, one commit per fix:

- `fix(tests/...): use tmp_path / Path-based fixture instead of /tmp literal`

Coverage gate must remain ≥ 90% on Linux.

### Phase 7 — Housekeeping

- `/workspace/PROGRESS.md`: append a new bullet under "Post-MVP" — *"CI: enable
  real win-64 support — commit `<sha>`"*. Update "Single next action".
- Version tag: fold this fix into a not-yet-cut `v0.2.0` rather than `v0.1.1`.
  The MVP `v0.1.0` tag does not promise Windows; nothing shipped depends on the
  earlier Linux-only CI guarantee. Once the matrix is green, tag and push:
  ```bash
  git tag -a v0.2.0 -m "kinoforge v0.2.0 — prompt splitter + win-64 CI"
  git push origin v0.2.0
  ```
- README: no "3-OS" claim exists, but optionally add a "Supported platforms:
  Linux, macOS, Windows" line — defer unless requested.

## Critical files

- `/workspace/pixi.toml` — platforms + per-target dep gating.
- `/workspace/pixi.lock` — regenerated by Phase 2.
- `/workspace/src/kinoforge/stores/local.py` — `as_posix()` fix.
- `/workspace/tests/stores/test_local.py` — regression test for the fix.
- `/workspace/.pre-commit-config.yaml` — `check-added-large-files` rewrite.
- `/workspace/PROGRESS.md` — durability update.
- `/workspace/.github/workflows/ci.yml` — no change needed (already matrices
  on all 3 OSes; failure was config, not workflow).

## Reused patterns / existing utilities

- Per-target deps via `[target.<platform>.dependencies]` — pattern already in
  use at `pixi.toml:72` for `linux-64`. No new pixi feature/env split needed.
- `Path.as_posix()` — already imported via `pathlib` in `local.py`. Zero new
  imports.
- `pixi run python -c` pattern — already used for `check-toml` at
  `.pre-commit-config.yaml:45`. New hook copies the shape.
- Atomic conventional commits + PROGRESS.md update after each phase — durability
  rules in `/workspace/CLAUDE.md`.

## Verification

End-state checks (must all hold simultaneously on a single SHA on `main`):

1. `pixi lock --check` exits 0 locally.
2. GitHub Actions CI green on `ubuntu-latest`, `macos-latest`,
   `windows-latest` — all three reach and pass the `Test` step.
3. `pixi run pre-commit run --all-files` green on Linux (Windows hook
   coverage validated via the new Python-based hook structure).
4. Coverage ≥ 90% (existing gate, Linux-only measurement).
5. `origin/main` advanced to that SHA.
6. (Optional) `v0.2.0` tag pushed and points to that SHA.
7. `PROGRESS.md` "Single next action" updated to reflect post-CI state.

End-to-end smoke: from a fresh clone on a Windows machine (or via CI), run
`pixi install && pixi run test` — expect 378 passing tests in ~the same
runtime as Linux.

## Fallback triggers (when to abandon and drop windows-latest)

Pull the rip-cord and remove `windows-latest` from the matrix only if:

- (a) A runtime dep (`pydantic` / `pyyaml` / `requests` / `tenacity` / `tqdm` /
  `graphifyy`) has no working win-64 conda or PyPI build.
- (b) The test auditor surfaces > 10 Windows failures, or any failure requires
  rewriting a non-trivial subsystem (e.g. the threadpool downloader semantics
  differ structurally on Windows).
- (c) `mypy strict` can't resolve types on win-64 (would force source pinning).

Current evidence does not point to any of these triggering.
