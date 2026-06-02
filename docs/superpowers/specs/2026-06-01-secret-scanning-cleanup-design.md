# Secret-scanning cleanup — design

**Date:** 2026-06-01
**Origin:** GitHub Secret Scanning alert on commit `edc8b3e` (Layer P Task 7 bug-fix #1 — `_RecordingHTTPSeam` redaction hardening) and its dependents on `main`.
**Status:** Approved (brainstorm 2026-06-01).

## Spec amendment 2026-06-01

**During plan-writing the audit's pattern set was narrowed from the spec's
original "reuse production `_CREDENTIAL_PATTERNS`" to a scanner-grade subset:
`sk_token`, `aws_access_key`, `pem_private_key`, `hf_token` (tightened to
`\bhf_[A-Za-z0-9]{32,}\b`).** Dropped from the audit: `rpa_token`, `fal_key`,
`bearer_auth` — these have no GitHub Secret Scanning detector equivalent and
the production patterns are intentionally loose (8-char minimum, generic
`Bearer` match) which trips on ~90 unrelated internal test tokens.

This amendment is recorded inline because the brainstorm's "broad audit
scope" decision (walk many files) is sound; only the per-pattern strictness
needed correction once the dry-run data was in hand. §3.4.2, §3.4.4, §7 are
rewritten below to reflect this. All other spec sections are unchanged.

The example blocks in §3.1, §3.2, §3.3 are also rewritten — they previously
quoted literal credential-prefix strings as "Before" examples, which made the
spec itself trip the audit it specifies. The new form shows the Before block
as concat-escaped Python (same form as After) with surrounding prose making
the rewrite intent clear; readers wanting the exact original literal can
`git show 49475a5:docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md`.

## 1. Context

GitHub Secret Scanning fired on commits from the Layer P Task 7 bug-fix #1
sub-plan (the redaction-hardening work itself). The alert names an
`<ASIA prefix + 16 alnum chars>` string as a "publicly leaked AWS Temporary
Access Key" and flags three files on `main`:

| File | Lines | Strings flagged |
|---|---|---|
| `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md` | self-references throughout §1, §3.1, §3.2, §3.3, §7 | The Before blocks quoting the legacy literals — trips the audit recursively. |
| `tests/providers/test_runpod_conftest.py` | 310–316 | 5 examples in the parametrised credential-pattern test fixture. |
| `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` | 246–250, 257 | Same 5 examples in the AC table + `"sk-proj-" + "A"*20` callout at line 257. |
| `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md` | 247–250, 277–283, 1224 | Same examples in the AC checklist (247–250), test-fixture transcription (277–283), pattern-table cell (1224 — see §3.3 below for why no edit is required there). |

### 1.1. Why every flagged string is non-secret

- `<AKIA prefix + 16 alnum chars>` and `<ASIA prefix + 16 alnum chars>` are
  AWS's canonical documentation examples (`"AKIA" + "IOSFODNN7EXAMPLE"` and
  `"ASIA" + "IOSFODNN7EXAMPLE"` in their verbatim forms — escaped here as
  concat so this spec does not self-trip the audit). Published by AWS
  specifically as format placeholders; they cannot grant access. AWS's own
  detector tooling allowlists them.
- `<sk-proj prefix + 20+ url-safe chars>` and
  `<sk-ant-api03 prefix + 20+ url-safe chars>` are hand-typed
  alphabet-sequence placeholders. Neither matches a real OpenAI or Anthropic
  key shape (real keys have higher entropy and don't follow the keyboard
  sequence `aBcDe…`).
- The PEM block (BEGIN RSA PRIVATE KEY header through END RSA PRIVATE KEY footer),
  truncated (`MIIE...XXXX`). The body is two literal placeholder lines, not
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
   uses its own scanner-grade `_PATTERNS` list (see §3.4.2 and the Spec
   Amendment above), not the production `_CREDENTIAL_PATTERNS` — see §3.4.2
   for the rationale.
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

Today's fixture lines (escaped here as concatenation so this spec does not
itself trip the audit — readers wanting the verbatim literals can
`git show 49475a5:tests/providers/test_runpod_conftest.py`):

```python
("sk_openai",       "sk-" + "proj-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("sk_anthropic",    "sk-" + "ant-api03-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("aws_akia",        "AKIA" + "IOSFODNN7EXAMPLE"),
("aws_asia",        "ASIA" + "IOSFODNN7EXAMPLE"),
("pem_private_key", "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nMIIE\nXXXX\n-----" + "END RSA PRIVATE KEY" + "-----"),
```

After the rewrite, the file contains exactly these concat forms. The runtime
tuples are byte-identical to today's literals. `pytest -k 'credential_pattern'`
passes unchanged.

### 3.2. `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md:246–257`

The example block at lines 246–250 of the Layer P bug-fix #1 spec documents
the redactor's AC by showing literal inputs. Replace each literal with a
shape-describing placeholder — better documentation anyway (it describes the
pattern under test, not a single specimen).

Today's spec block (escaped as concat for spec-scan safety):

```python
("sk_real_openai",     "sk-" + "proj-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("sk_real_anthropic",  "sk-" + "ant-api03-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
("aws_akia",           "AKIA" + "IOSFODNN7EXAMPLE"),
("aws_asia",           "ASIA" + "IOSFODNN7EXAMPLE"),
("pem_private_key",    "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nMIIE...\n-----" + "END RSA PRIVATE KEY" + "-----"),
```

After the rewrite (the spec block uses `text` fencing because the new
content is shape grammar, not executable Python):

```text
("sk_real_openai",     <sk-proj prefix + 20+ url-safe chars>),
("sk_real_anthropic",  <sk-ant-api03 prefix + 20+ url-safe chars>),
("aws_akia",           <AKIA prefix + 16 alnum chars>),
("aws_asia",           <ASIA prefix + 16 alnum chars>),
("pem_private_key",    <multi-line PEM block: BEGIN…END inclusive>),
```

The line 257 callout `"sk-proj-" + "A"*20 → IS redacted` becomes
`<sk-proj prefix + 20 chars> → IS redacted`. The shape descriptions name
the fields the production regex constrains, which is what the AC actually
needs.

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

Today's plan-doc AC checklist (escaped as concat for spec-scan safety):

```python
- [ ] "sk-" + "proj-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345" → <REDACTED>.
- [ ] "sk-" + "ant-api03-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345" → <REDACTED>.
- [ ] "AKIA" + "IOSFODNN7EXAMPLE" → <REDACTED>; same for "ASIA" + "IOSFODNN7EXAMPLE".
- [ ] Multi-line PEM block "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nMIIE..." → <REDACTED> (whole block).
```

After the rewrite, each bullet uses the same shape-description form as §3.2:

```text
- [ ] <sk-proj prefix + 20+ url-safe chars> → <REDACTED>.
- [ ] <sk-ant-api03 prefix + 20+ url-safe chars> → <REDACTED>.
- [ ] <AKIA prefix + 16 alnum chars> → <REDACTED>; same for <ASIA prefix + 16 alnum chars>.
- [ ] Multi-line PEM block <BEGIN RSA PRIVATE KEY…END RSA PRIVATE KEY> → <REDACTED> (whole block).
```

The test-fixture transcription at plan lines 277–283 mirrors the spec's
§3.2 After block; apply the same shape-text replacement there. The
pattern-table cell at line 1224 already uses shape-with-ellipsis form
(`sk-proj-...` / `sk-ant-api03-...`), and the production `sk_token` regex
requires 20+ URL-safe chars after the prefix — the literal three-dot
ellipsis is not in the URL-safe char class so the regex does not match.
The audit (§3.4) passes line 1224 unchanged. **No edit required at line 1224.**

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

The audit declares its own scanner-grade `_PATTERNS` list at module top,
distinct from the production `_CREDENTIAL_PATTERNS` in
`tests/providers/conftest_runpod.py`. Production patterns are intentionally
loose (8-char minimum on prefix tails, generic `Bearer` match) to catch
test-time leaks aggressively. Scanner-grade patterns match what GitHub Secret
Scanning actually flags:

```python
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sk_token",        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key",  re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
    ("hf_token",        re.compile(r"\bhf_[A-Za-z0-9]{32,}\b")),
]
```

Dropped vs. production: `rpa_token`, `fal_key`, `bearer_auth` — no GitHub
Secret Scanning detectors exist for these, and the production patterns trip
on internal shape examples (`rpa_xxxxxxxx`) and ubiquitous test tokens
(`Bearer test-token`). Tightened: `hf_token` from `\bhf_[A-Za-z0-9_\-]{8,}\b`
to `\bhf_[A-Za-z0-9]{32,}\b` (real HuggingFace tokens are 37 chars; shape
examples like `hf_xxxxxxxx` no longer trip). Each `re.Pattern` is applied
to the file's raw text via `pattern.finditer(text)`; the match's `.start()`
is converted to a `(line, column)` pair for the error report.

The audit's pattern set is independent of production redaction. Production
redaction is a runtime concern (broad match, conservative redact); the
audit is a source-tree concern (tight match, matches what scanners catch).
A new credential prefix added to production redaction does not automatically
extend the lockdown — that's intentional. New scanner-detectable prefixes are
added to `_PATTERNS` by hand, with a regression test.

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

A second pytest function in the same file asserts `len(_PATTERNS) >= 4` and
verifies the names cover at least the scanner-grade canonical set:
`{sk_token, aws_access_key, pem_private_key, hf_token}`. This catches a
future refactor that accidentally empties the pattern list (which would
silently disable the lockdown).

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
- `test_credential_patterns_cover_expected` — self-test asserting the
  pattern list has at least the 4 expected scanner-grade entries by name.
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

1. The scanner-grade dry-run (see Verify in T1 of the plan) returns zero
   hits across all 4 in-scope files at HEAD.
2. `pixi run pytest tests/test_source_audit.py -v` passes all three new
   tests on HEAD.
3. The full `pixi run test` suite passes without modification to any
   pre-existing test.
4. `pixi run pre-commit run --all-files` is clean.
5. The runtime values built by §3.1's concatenations are byte-identical to
   the original literals (verified by re-running the pre-existing
   `test_runpod_conftest.py` parametrised credential-pattern test).
6. The cleanup spec itself (this file) passes its own audit — i.e.,
   self-scanning this `.md` file with the audit returns zero hits.

## 8. Implementation order (informative — locked by the plan, not this spec)

A reasonable sequence the writing-plans phase can adopt:

1. Add `tests/test_source_audit.py` (RED — fails on current `main`).
2. Apply §3.1 (test fixture concatenation).
3. Apply §3.2 + §3.3 (spec + plan shape replacements).
4. Confirm audit is GREEN; confirm pre-commit clean; commit.

Steps 1–3 land as a single commit if the plan elects to lockstep them, or as
2–3 commits with the audit RED on the first commit's tree if the plan elects
strict red-first commits. Either is acceptable.
