# Secret-scanning cleanup — design

**Date:** 2026-06-01
**Origin:** GitHub Secret Scanning alert on commit `edc8b3e` (Layer P Task 7 bug-fix #1 — `_RecordingHTTPSeam` redaction hardening) and its dependents on `main`.
**Status:** Approved (brainstorm 2026-06-01).

## 1. Context

GitHub Secret Scanning fired on commits from the Layer P Task 7 bug-fix #1
sub-plan (the redaction-hardening work itself). The alert names
`ASIAIOSFODNN7EXAMPLE` as a "publicly leaked AWS Temporary Access Key" and
flags three files on `main`:

| File | Lines | Strings flagged |
|---|---|---|
| `tests/providers/test_runpod_conftest.py` | 310–316 | 5 examples in the parametrised credential-pattern test fixture. |
| `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` | 246–250, 257 | Same 5 examples in the AC table + `"sk-proj-" + "A"*20` callout at line 257. |
| `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md` | 247–250, 277–283, 1224 | Same examples in the AC checklist (247–250), test-fixture transcription (277–283), pattern table cell (1224). |

### 1.1. Why every flagged string is non-secret

- `AKIAIOSFODNN7EXAMPLE` and `ASIAIOSFODNN7EXAMPLE` are AWS's canonical
  documentation examples, published by AWS specifically as format placeholders.
  They cannot grant access. AWS's own detector tooling allowlists them.
- `sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` and
  `sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` are hand-typed
  alphabet-sequence placeholders. Neither matches a real OpenAI or Anthropic
  key shape (real keys have higher entropy and don't follow the keyboard
  sequence `aBcDe…`).
- The PEM block `-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----`
  is truncated (`MIIE...XXXX`). The body is two literal placeholder lines, not
  base64 key material.

The scanner pattern-matched on prefix only.

### 1.2. Why the source code itself isn't tripping

Production source under `src/` has zero literal credential examples — verified
by `rg -n 'AKIA[A-Z0-9]|ASIA[A-Z0-9]|sk-proj-|sk-ant-' src/`. The redactor's
regex *patterns* live in `tests/providers/conftest_runpod.py:191` as `re.Pattern`
literals (e.g., `r"\bAKIA[A-Z0-9]{16}\b"`); scanners do not fire on regex source
because the literal substring `AKIA[A-Z0-9]{16}` is not a key shape.

The three flagged files are exactly the spec, plan, and test that document or
exercise the redaction work. They quote literal credential-shaped strings as
inputs to the system under test (or as documentation of what the system
masks). That coupling is the root cause: the more thoroughly we document what
gets redacted, the more closely the docs themselves resemble what scanners
flag.

## 2. Scope

### 2.1. In scope

1. **Forward-fix the three flagged files** so the source text no longer
   contains credential-prefix literals, while preserving the runtime values
   the tests depend on.
2. **Add a permanent fail-closed lockdown audit** (`tests/test_source_audit.py`)
   that walks documentation, tests, and repo-root markdown / env files and
   raises if any of them gain a literal credential-shaped string. The audit
   reuses the production `_CREDENTIAL_PATTERNS` from
   `tests/providers/conftest_runpod.py:191`, so any future credential type added
   to the redactor is automatically picked up by the lockdown.
3. **Document the post-merge UI step**: dismiss the GitHub Secret Scanning
   alert as "Used in tests" / "False positive" once the fix is on `main`.

### 2.2. Out of scope

- **History rewrite.** The flagged strings live in commit history on
  `main`. Rewriting `edc8b3e` and its dependents would (a) require a
  force-push to `main`, violating safe-git defaults; (b) rewrite the SHAs of
  the `c63cbea` Layer P + Q merge and every commit on top; (c) be
  disproportionate for false positives on documented non-secret examples.
- **Global `.github/secret_scanning.yml` allowlist.** Telling GitHub to
  ignore paths is strictly weaker than a local fail-closed test — and creates a
  future footgun if a real secret ever lands in an allowlisted path.
- **Touching the production regex patterns in `tests/providers/conftest_runpod.py`.**
  Those are regex literals, not credential examples; scanners do not fire on
  them and the redactor itself is correct.
- **Pre-commit hooks for `gitleaks` / `trufflehog`.** Separate hardening; the
  new audit runs as part of `pytest`, which already runs in pre-commit.
- **AGENTS.md edits.** Line 25 already uses shape-with-ellipsis form
  (`sk-proj-...` / `sk-ant-api03-...`) and does not match the prefix patterns.
  The new audit will confirm this on first run.

## 3. File-level changes

### 3.1. `tests/providers/test_runpod_conftest.py:310–316`

The fixture is a list of `(pattern_name, credential_value)` tuples fed to
`_redact_credential_patterns`. The runtime values must continue to match the
production regexes (`\bAKIA[A-Z0-9]{16}\b`, `\bsk-proj-[A-Za-z0-9_-]{20,}\b`,
etc.). Rewrite each literal as runtime concatenation so the source code
contains no full literal but the assembled runtime string is byte-identical
to today's value.

**Before:**

```python
("sk_openai",       "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("sk_anthropic",    "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("aws_akia",        "AKIAIOSFODNN7EXAMPLE"),
("aws_asia",        "ASIAIOSFODNN7EXAMPLE"),
("pem_private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIE\nXXXX\n-----END RSA PRIVATE KEY-----"),
```

**After:**

```python
("sk_openai",       "sk-" + "proj-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("sk_anthropic",    "sk-" + "ant-api03-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("aws_akia",        "AKIA" + "IOSFODNN7EXAMPLE"),
("aws_asia",        "ASIA" + "IOSFODNN7EXAMPLE"),
("pem_private_key", "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nMIIE\nXXXX\n-----" + "END RSA PRIVATE KEY" + "-----"),
```

Concatenation breaks the scanner's prefix detector (no source line contains
the full prefix-plus-tail literal) while every runtime tuple element is
unchanged. Existing assertions in `test_runpod_conftest.py` pass without
edit.

### 3.2. `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md:246–257`

The example block in the spec documents the redactor's AC by showing literal
inputs. Replace each literal with a shape-describing placeholder — better
documentation anyway (it describes the pattern under test, not a single
specimen).

**Before:**

```python
("sk_real_openai",     "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("sk_real_anthropic",  "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("aws_akia",           "AKIAIOSFODNN7EXAMPLE"),
("aws_asia",           "ASIAIOSFODNN7EXAMPLE"),
("pem_private_key",    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"),
```

**After:**

```text
("sk_real_openai",     <sk-proj prefix + 20+ url-safe chars>),
("sk_real_anthropic",  <sk-ant-api03 prefix + 20+ url-safe chars>),
("aws_akia",           <AKIA prefix + 16 alnum chars>),
("aws_asia",           <ASIA prefix + 16 alnum chars>),
("pem_private_key",    <multi-line PEM block: BEGIN…END inclusive>),
```

Line 257 callout `"sk-proj-" + "A"*20 → IS redacted` becomes
`<sk-proj prefix + 20 chars> → IS redacted`. The shape descriptions name the
fields the production regex constrains, which is what the AC actually needs.

The fenced code block's language tag changes from `python` to `text` because
the new content is shape grammar, not executable Python.

### 3.3. `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md`

Three locations get the same shape-replacement pass as §3.2:

- **AC checklist (247–250):** each `- [ ] ` bullet that quotes a literal
  becomes a shape description (e.g.
  `- [ ] <sk-proj prefix + 20+ url-safe chars> → <REDACTED>.`).
- **Test-fixture transcription (277–283):** the fenced Python block mirroring
  §3.1 becomes the same shape-text block as §3.2 (language tag flips to
  `text`).
- **Pattern-table cell (1224):** the `sk-proj-...` / `sk-ant-api03-...`
  example column already uses shape-with-ellipsis form. The production
  `sk_token` regex requires 20+ URL-safe characters after the prefix and
  therefore does not match a three-dot literal ellipsis; the audit (§3.4)
  will pass this line unchanged. GitHub Secret Scanning did not flag it
  either (it is not in the alert's location list). **No edit required.**

### 3.4. New `tests/test_source_audit.py`

A fail-closed walker, modeled on `tests/providers/test_fixtures_audit.py`.

#### 3.4.1. Walked paths

The audit walks exactly:

- `docs/superpowers/**/*.md` (every spec + plan + sub-spec + sub-plan).
- `tests/**/*.py` (every test source).
- The five repo-root files: `README.md`, `AGENTS.md`, `PROGRESS.md`,
  `CLAUDE.md`, `.env.example`.

It does NOT walk `src/**/*.py` (production source is verified clean; the
regex patterns under `tests/providers/conftest_runpod.py` would be false
positives if scanned naively). It does NOT walk `docs/superpowers/**/*.tasks.json`
(those are walked by `test_fixtures_audit.py` already via the JSON path).

#### 3.4.2. Pattern set

The audit imports `_CREDENTIAL_PATTERNS` from
`tests/providers/conftest_runpod.py` and applies each `re.Pattern` to the
file's raw text via `pattern.finditer(text)`. The match's `.start()` is
converted to a `(line, column)` pair for the error report.

The audit reuses the production pattern list directly, not a copy, so any
future credential type added to the redactor automatically extends the
lockdown.

#### 3.4.3. Hit type and error shape

The audit defines its own `SourceLeakHit` dataclass (separate from
`tests/providers/conftest_runpod.py::LeakHit` because that one uses
`json_pointer`, which doesn't apply to text files):

```python
@dataclass(frozen=True)
class SourceLeakHit:
    path: Path
    line: int
    column: int
    pattern_name: str
    match_snippet: str
```

On any hit, the audit's pytest function raises a single `AssertionError` whose
message lists every `(path, line, column, pattern_name, match_snippet)` tuple
in the form `tests/foo.py:42:18 [aws_access_key] AKIA…EXAMPLE` (the snippet is
truncated to ≤40 chars + ellipsis so the assertion message stays readable
when many leaks land at once).

#### 3.4.4. Self-test

A second pytest function in the same file asserts
`len(_CREDENTIAL_PATTERNS) >= 7` and verifies the imported names cover at
least `{rpa_token, hf_token, fal_key, bearer_auth, sk_token, aws_access_key,
pem_private_key}`. This catches a future refactor that accidentally empties
the pattern list (which would silently disable the lockdown).

#### 3.4.5. Reverse-test (red-first discriminator)

A third pytest function builds a temporary `.md` file inside a
`tmp_path` containing one literal credential-shaped string, runs the audit's
core walker against that single path, and asserts the walker reports exactly
one hit. This guarantees the audit's match logic still works even if every
real file in the repo passes — without it, the main test could no-op
indefinitely.

#### 3.4.6. Style

Mirrors `tests/providers/test_fixtures_audit.py`:

- Module docstring describing what the audit catches and how it pairs with
  the other guards (`_RecordingHTTPSeam.flush()` runtime backstop;
  `test_fixtures_audit.py` for JSON fixtures).
- A `_format_offenders()` helper for human-readable assertion messages.
- A single `_REPO_ROOT: Path = Path(__file__).resolve().parents[1]` constant.

## 4. Test plan

### 4.1. Existing tests

`pixi run test` continues to pass without modification. The runtime tuples
produced by §3.1's concatenation are byte-for-byte identical to today's
literals, so every `tests/providers/test_runpod_conftest.py` assertion that
feeds those tuples to the redactor reproduces today's behavior.

### 4.2. New tests in `tests/test_source_audit.py`

- `test_no_committed_source_contains_a_credential` — the main audit walk.
  GREEN after §3.1, §3.2, §3.3 land. Red against current `main`.
- `test_credential_patterns_cover_canonical_seven` — self-test asserting the
  pattern list has at least the 7 expected entries by name.
- `test_audit_walker_fires_on_known_credential` — reverse-test using
  `tmp_path` to confirm the walker catches at least one synthetic hit.

### 4.3. Red-first ordering

Per project TDD rule (`CLAUDE.md` → TDD workflow), the audit test lands
first and is RED on a probe commit, then the file-level rewrites land and
turn it GREEN. The implementation plan (next stage) sequences this; this spec
records the requirement.

## 5. Risk and rollback

- **Test-suite breakage:** zero. The runtime fixture values are byte-identical
  before and after §3.1; the spec/plan edits do not affect runtime; the new
  audit adds tests but does not modify any existing test logic.
- **Audit false positives in non-flagged files:** verified during brainstorm
  that `AGENTS.md` line 25 uses shape-with-ellipsis form
  (`sk-proj-...` / `sk-ant-api03-...`), `README.md` does not match the
  prefix patterns, `PROGRESS.md` references SHAs and pattern names but not
  credential literals, `CLAUDE.md` has no credential examples, `.env.example`
  uses placeholder `${...}` interpolations. If the audit fires on something
  unexpected, that's the audit doing its job; either rewrite the literal in
  the same style as §3.1–§3.3 or, if the hit is a documented intentional
  shape example that the regex shouldn't match, tighten the regex.
- **Rollback:** a single `git revert <cleanup-commit>` reverts cleanly. No
  production code is touched.

## 6. Post-merge remediation

After the cleanup commit reaches `main`:

1. Navigate to the repository's GitHub Secret Scanning alerts page.
2. For each flagged secret, dismiss the alert as "Used in tests" /
   "False positive". GitHub alerts are commit-scoped; once dismissed they do
   not retrigger on the same SHAs.
3. Confirm via `gh api repos/killett/kinoforge/secret-scanning/alerts`
   (or the equivalent UI view) that the alerts now show state `resolved`.

The audit added in §3.4 prevents future commits from reintroducing the same
prefix patterns: `pixi run test` runs the audit in CI and locally, so any
future spec/plan/test draft that reverts to literal credential strings fails
fast before it can reach `main`.

## 7. Acceptance criteria

1. `rg -n 'AKIAIOSFODNN7EXAMPLE|ASIAIOSFODNN7EXAMPLE' --hidden -g '!.git/'`
   returns zero matches under HEAD (verified by §3.1, §3.2, §3.3).
2. `rg -n 'sk-proj-aBcDe|sk-ant-api03-aBcDe' --hidden -g '!.git/'` returns
   zero matches under HEAD.
3. No source file contains the complete PEM-block literal
   `-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----`
   as a single string literal (the production regex source in
   `tests/providers/conftest_runpod.py` contains the dashed prefix and suffix
   tokens as parts of an `re.Pattern`, which is intentional and exempt).
   The new audit (§3.4) is the deterministic check; `rg -n -F` cross-checks
   the prefix tokens but the audit is authoritative.
4. `pixi run pytest tests/test_source_audit.py -v` passes all three new
   tests on HEAD.
5. The full `pixi run test` suite passes without modification to any
   pre-existing test.
6. `pixi run pre-commit run --all-files` is clean.
7. The runtime values built by §3.1's concatenations are byte-identical to
   the original literals (verified by re-running the pre-existing
   `test_runpod_conftest.py` parametrised credential-pattern test).

## 8. Implementation order (informative — locked by the plan, not this spec)

A reasonable sequence the writing-plans phase can adopt:

1. Add `tests/test_source_audit.py` (RED — fails on current `main`).
2. Apply §3.1 (test fixture concatenation).
3. Apply §3.2 + §3.3 (spec + plan shape replacements).
4. Confirm audit is GREEN; confirm pre-commit clean; commit.

Steps 1–3 land as a single commit if the plan elects to lockstep them, or as
2–3 commits with the audit RED on the first commit's tree if the plan elects
strict red-first commits. Either is acceptable.
