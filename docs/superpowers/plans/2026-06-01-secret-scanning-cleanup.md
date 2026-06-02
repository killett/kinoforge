# Secret-Scanning Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the literal credential-shaped strings on `main` that fired GitHub Secret Scanning, and add a permanent fail-closed audit that catches reintroduction.

**Architecture:** Forward-fix only (no history rewrite, no global allowlist). Tests get runtime string concatenation so the source no longer contains full credential-prefix literals but runtime tuples remain byte-identical. Docs get shape-describing placeholders. A new `tests/test_source_audit.py` walks docs + tests + repo-root markdown + `.env.example` and fails closed on any credential-prefix literal — using a scanner-grade subset of patterns (sk-, AKIA/ASIA, PEM, hf_ at canonical length), not the loose production `_CREDENTIAL_PATTERNS` used at fixture-capture time.

**Tech Stack:** Python 3.13 stdlib (`re`, `pathlib`, `dataclasses`), pytest (existing test runner via `pixi run test`), pre-commit (existing).

**Source spec:** `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md` (commit `49475a5`).

**Mid-plan amendment:** The spec assumed the audit would reuse `tests/providers/conftest_runpod.py::_CREDENTIAL_PATTERNS`. A dry-run showed that pattern set fires on 130 source locations across 13 files (mostly internal test tokens like `Bearer test-token` and shape examples like `rpa_xxxxxxxx`). User picked scanner-grade subset (Option A) during plan-writing: drop `rpa_token`, `fal_key`, `bearer_auth`; tighten `hf_token` to 32+ chars; keep `sk_token`, `aws_access_key`, `pem_private_key` as in production. Audit reduces to 39 hits across 4 files. Task 1 amends the spec to record this divergence.

---

## File Structure

| Path | Responsibility | Touched in |
|---|---|---|
| `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md` | The design contract this plan implements. Self-trips today's audit because §1.1, §3.1, §3.2, §3.3, §3.4, §7 quote literal credential-prefix strings. | T1 (amend) |
| `tests/providers/test_runpod_conftest.py` | Parametrised credential-pattern test exercising `_redact_credential_patterns`. 5 literal credential strings in the fixture at lines 310-316. | T2 |
| `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` | Layer P bug-fix #1 spec doc that quotes the same 5 example tuples + a `"sk-proj-" + "A"*20` callout. | T2 |
| `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md` | Layer P bug-fix #1 plan doc; AC checklist (247-250) + test-fixture transcription (277-283) quote the same examples. | T2 |
| `tests/test_source_audit.py` | NEW. Fail-closed walker over docs + tests + repo-root markdown + `.env.example`; applies scanner-grade patterns; raises a single assertion error listing every hit. | T3 (create) |

Out of scope (not touched by this plan):
- `tests/providers/conftest_runpod.py` — production redactor source; regex literals do not trip GitHub Secret Scanning.
- `AGENTS.md` — uses shape examples like `rpa_xxxxxxxx` that the audit's narrowed pattern set does not match.
- Older Layer plans (`docs/superpowers/plans/2026-05-31-layer-m.md`, etc.) — contain `Bearer test-token` style strings that the scanner-grade pattern set deliberately ignores.

---

## Task 1: Amend cleanup spec for scanner-grade pattern set + concat-escaped examples

**Goal:** Bring the spec at `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md` in line with the scanner-grade pattern decision and make the spec itself pass the audit it specifies.

**Files:**
- Modify: `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md`

**Acceptance Criteria:**
- [ ] §3.4.2 ("Pattern set") rewritten to specify a scanner-grade subset (`sk_token`, `aws_access_key`, `pem_private_key`, `hf_token` tightened to `\bhf_[A-Za-z0-9]{32,}\b`); no longer reuses production `_CREDENTIAL_PATTERNS`.
- [ ] §3.4.4 ("Self-test") rewritten to assert `>= 4` patterns and to name the expected canonical set (`sk_token`, `aws_access_key`, `pem_private_key`, `hf_token`).
- [ ] §1.0 file table updated to add the cleanup spec itself as a fourth in-scope file (it currently quotes its own Before block).
- [ ] §3.1, §3.2, §3.3 example blocks rewritten so neither the Before block nor the After block contains a literal `sk-proj-…`, `sk-ant-api03-…`, `AKIA…EXAMPLE`, `ASIA…EXAMPLE`, or complete PEM block. The After block is shown as runtime concatenation; the Before block is described in prose ("today's literal is …, escaped here as concat for spec-scan safety") and uses the same concat form so the spec itself passes the audit.
- [ ] §7 AC list updated: AC#1, AC#2 expanded to mention 4 files (not 3); AC#3 unchanged; add an AC for "the cleanup spec itself passes the audit".
- [ ] An inline `## Spec amendment 2026-06-01` section appears near the top recording the pivot (scanner-grade vs. production patterns) for posterity.
- [ ] The scanner-grade dry-run command (see Verify) returns zero hits against this spec.

**Verify:**

```bash
python3 -c "
import re
from pathlib import Path
patterns = [
    ('sk_token', re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----')),
    ('hf_token', re.compile(r'\bhf_[A-Za-z0-9]{32,}\b')),
]
p = Path('docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md')
txt = p.read_text()
hits = sum(len(pat.findall(txt)) for _, pat in patterns)
print(f'{p}: {hits} hits')
assert hits == 0, f'spec still trips audit: {hits} hits'
"
```

Expected: `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md: 0 hits`.

**Steps:**

- [ ] **Step 1: Read the existing spec to identify all literal-bearing locations**

```bash
python3 -c "
import re
from pathlib import Path
patterns = [
    ('sk_token', re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----')),
    ('hf_token', re.compile(r'\bhf_[A-Za-z0-9]{32,}\b')),
]
p = Path('docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md')
txt = p.read_text()
for name, pat in patterns:
    for m in pat.finditer(txt):
        line = txt[:m.start()].count(chr(10)) + 1
        print(f'{p}:{line} [{name}] {m.group(0)[:50]!r}')
"
```

Expected: ~19 hits across lines 11, 22, 25-26, 30, 100-104, 132-136, 311, 316 (sk-, AKIA, ASIA, PEM examples in §1.0 file table, §3.1 Before block, §3.2 Before block, §3.3 Before block, §3.4 example, §7 AC examples).

- [ ] **Step 2: Add the spec-amendment section at the top of the spec**

Insert after the `**Status:** Approved …` line, before §1:

```markdown
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
```

- [ ] **Step 3: Update §1.0 file table to add the cleanup spec as a fourth in-scope file**

The §1.0 file table currently lists 3 files. Add a fourth row at the top so
the order matches the in-flight implementation (this spec, then the 3
legacy files):

```markdown
| File | Lines | Strings flagged |
|---|---|---|
| `docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md` | self-references throughout §1, §3.1, §3.2, §3.3, §7 | The Before blocks quoting the legacy literals — trips the audit recursively. |
| `tests/providers/test_runpod_conftest.py` | 310–316 | 5 examples in the parametrised credential-pattern test fixture. |
| `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` | 246–250, 257 | Same 5 examples in the AC table + `"sk-proj-" + "A"*20` callout at line 257. |
| `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md` | 247–250, 277–283, 1224 | Same examples in the AC checklist (247–250), test-fixture transcription (277–283), pattern-table cell (1224 — see §3.3 below for why no edit is required there). |
```

- [ ] **Step 4: Replace §3.1 example blocks with concat-only form**

Find the §3.1 ("`tests/providers/test_runpod_conftest.py:310–316` — runtime concatenation") section. The current text has both a **Before** block and an **After** block, each a fenced ```python` code block listing 5 tuples with literal strings (or concat strings in the After case). Replace the whole section body with:

````markdown
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
````

- [ ] **Step 5: Replace §3.2 example blocks with concat-only form for Before, prose for change description**

Find the §3.2 ("`docs/superpowers/specs/…redaction-design.md:246–257` — shape-describing placeholders") section. Replace both the Before block and the After block plus surrounding prose with:

````markdown
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

After the rewrite (the spec block uses ```text``` fencing because the new
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
````

- [ ] **Step 6: Replace §3.3 example blocks similarly**

Find the §3.3 ("`docs/superpowers/plans/…redaction.md`") section. The current
prose names three locations (AC checklist 247–250, test-fixture transcription
277–283, pattern-table cell 1224). Leave the prose intact but add a concat-escaped
block immediately after the prose, mirroring §3.2:

````markdown
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
````

- [ ] **Step 7: Rewrite §3.4.2 (Pattern set) and §3.4.4 (Self-test)**

Find §3.4.2 ("Pattern set"). Today's text says:

> The audit imports `_CREDENTIAL_PATTERNS` from `tests/providers/conftest_runpod.py`
> and applies each `re.Pattern` to the file's raw text via `pattern.finditer(text)`.

Replace with:

````markdown
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
````

Find §3.4.4 ("Self-test"). Today's text says:

> A second pytest function in the same file asserts `len(_CREDENTIAL_PATTERNS) >= 7`
> and verifies the imported names cover at least `{rpa_token, hf_token, fal_key,
> bearer_auth, sk_token, aws_access_key, pem_private_key}`.

Replace with:

````markdown
A second pytest function in the same file asserts `len(_PATTERNS) >= 4` and
verifies the names cover at least the scanner-grade canonical set:
`{sk_token, aws_access_key, pem_private_key, hf_token}`. This catches a
future refactor that accidentally empties the pattern list (which would
silently disable the lockdown).
````

- [ ] **Step 8: Replace AC list in §7 with scanner-grade variants**

Find §7 ("Acceptance criteria"). Replace the list with:

````markdown
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
````

- [ ] **Step 9: Re-run the dry-run script to confirm zero hits**

```bash
python3 -c "
import re
from pathlib import Path
patterns = [
    ('sk_token', re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----')),
    ('hf_token', re.compile(r'\bhf_[A-Za-z0-9]{32,}\b')),
]
p = Path('docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md')
txt = p.read_text()
total = 0
for name, pat in patterns:
    for m in pat.finditer(txt):
        line = txt[:m.start()].count(chr(10)) + 1
        print(f'{p}:{line} [{name}] {m.group(0)[:50]!r}')
        total += 1
assert total == 0, f'spec still trips audit: {total} hits'
print('OK')
"
```

Expected: `OK` (no leak lines printed).

- [ ] **Step 10: Pre-commit + commit**

```bash
git add docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md
pixi run pre-commit run --files docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md
```

Expected: all hooks Pass / Skip (no `.py` change, no `.toml` change).

```bash
git commit -m "$(cat <<'EOF'
docs(spec): amend secret-scanning cleanup for scanner-grade audit

Dry-run during plan-writing showed the spec's original "reuse production
_CREDENTIAL_PATTERNS" assumption fires the audit on 130 source locations
across 13 files — mostly internal test tokens (Bearer test-token) and shape
examples (rpa_xxxxxxxx) with no GitHub Secret Scanning detector equivalent.

Amends:
- §3.4.2 Pattern set: declares its own _PATTERNS list (sk_token,
  aws_access_key, pem_private_key, hf_token tightened to 32+ chars), not
  the production _CREDENTIAL_PATTERNS. Drops rpa_token, fal_key,
  bearer_auth (no scanner equivalents; production patterns over-trigger).
- §3.4.4 Self-test: assert >= 4 patterns with canonical names.
- §1.0 file table: add the cleanup spec itself as a 4th in-scope file
  (recursive trip — the Before blocks quoting legacy literals trip the
  audit).
- §3.1 / §3.2 / §3.3 example blocks: Before blocks rewritten as
  concat-escaped Python so the spec stops self-tripping the audit;
  surrounding prose links back to the verbatim literal via git history.
- §7 AC: scanner-grade dry-run + 4-file scope.

Adds §"Spec amendment 2026-06-01" near the top recording the pivot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit lands cleanly.

---

## Task 2: Forward-fix the 3 Layer P bug-fix #1 files

**Goal:** Remove every literal credential-prefix string from the 3 source files that fired GitHub Secret Scanning, preserving runtime values.

**Files:**
- Modify: `tests/providers/test_runpod_conftest.py:310-316`
- Modify: `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md:246-257`
- Modify: `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md:247-250, 277-283`

**Acceptance Criteria:**
- [ ] `tests/providers/test_runpod_conftest.py:310-316` rewrites the 5 sk_token / AKIA / ASIA / PEM fixture tuples as runtime concatenation. The 4 other fixture tuples in the same parametrise block (`rpa_token`, `hf_token`, `fal_key`, `bearer_auth`) remain untouched (scanner-grade audit does not match them).
- [ ] `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"` passes (runtime tuples byte-identical, so all production-pattern assertions hold).
- [ ] Layer P bug-fix #1 spec at lines 246-250 + 257: example tuples rewritten as shape descriptions per §3.2 of the amended cleanup spec.
- [ ] Layer P bug-fix #1 plan at lines 247-250 + 277-283: AC checklist + test-fixture transcription rewritten as shape descriptions per §3.3 of the amended cleanup spec. Line 1224 left untouched (already shape-with-ellipsis).
- [ ] Scanner-grade dry-run against these 3 files returns zero hits.
- [ ] `pixi run pre-commit run` clean.

**Verify:**

```bash
python3 -c "
import re
from pathlib import Path
patterns = [
    ('sk_token', re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----')),
    ('hf_token', re.compile(r'\bhf_[A-Za-z0-9]{32,}\b')),
]
files = [
    'tests/providers/test_runpod_conftest.py',
    'docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md',
    'docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md',
]
total = 0
for p in files:
    txt = Path(p).read_text()
    for name, pat in patterns:
        for m in pat.finditer(txt):
            line = txt[:m.start()].count(chr(10)) + 1
            print(f'{p}:{line} [{name}] {m.group(0)[:50]!r}')
            total += 1
assert total == 0, f'{total} hits remain'
print('OK')
"
pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"
```

Expected: dry-run prints `OK`; pytest passes the credential-pattern test cases (parametrised → 9 cases all PASS).

**Steps:**

- [ ] **Step 1: Rewrite the test fixture in `tests/providers/test_runpod_conftest.py`**

At lines 310-316 (inside the `@pytest.mark.parametrize` block), replace exactly these 5 tuples (the first 4 `rpa_token`/`hf_token`/`fal_key`/`bearer_auth` tuples at lines 306-309 are LEFT UNCHANGED):

```python
        ("sk_openai", "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("sk_anthropic", "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("aws_akia", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIAIOSFODNN7EXAMPLE"),
        (
            "pem_private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\nXXXX\n-----END RSA PRIVATE KEY-----",
        ),
```

with:

```python
        ("sk_openai", "sk-" + "proj-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("sk_anthropic", "sk-" + "ant-api03-" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("aws_akia", "AKIA" + "IOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIA" + "IOSFODNN7EXAMPLE"),
        (
            "pem_private_key",
            "-----" + "BEGIN RSA PRIVATE KEY" + "-----\nMIIE\nXXXX\n-----" + "END RSA PRIVATE KEY" + "-----",
        ),
```

Indentation matches the surrounding parametrise list (8 spaces from the function-level scope).

- [ ] **Step 2: Run pytest to confirm runtime values unchanged**

```bash
pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"
```

Expected: all parametrised cases PASS, including the 5 affected ones. If any FAIL, the concat reproduced a wrong byte (e.g., missing dash); diff the runtime tuple against the original literal to find the discrepancy.

- [ ] **Step 3: Rewrite Layer P bug-fix #1 spec example block**

In `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md`, find the example block at lines 246-250 (inside the §5 "Pattern matcher — parametrised per credential format" enumeration). The current block is:

```python
   ("sk_real_openai",     "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
   ("sk_real_anthropic",  "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
   ("aws_akia",           "AKIAIOSFODNN7EXAMPLE"),
   ("aws_asia",           "ASIAIOSFODNN7EXAMPLE"),
   ("pem_private_key",    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"),
```

(The first 4 `rpa_token_in_log`/`hf_token_bare`/`fal_key_bare`/`bearer_header` tuples at lines 240-245 are LEFT UNCHANGED.)

Replace with (note the fence language flips from `python` to `text` because the new content is shape grammar):

```text
   ("sk_real_openai",     <sk-proj prefix + 20+ url-safe chars>),
   ("sk_real_anthropic",  <sk-ant-api03 prefix + 20+ url-safe chars>),
   ("aws_akia",           <AKIA prefix + 16 alnum chars>),
   ("aws_asia",           <ASIA prefix + 16 alnum chars>),
   ("pem_private_key",    <multi-line PEM block: BEGIN…END inclusive>),
```

If the opening fence is `` ```python `` immediately above the block, change it to `` ```text ``. Leave the closing fence as `` ``` ``.

- [ ] **Step 4: Rewrite the §6 "sk- false-positive guard" callout at spec line 257**

Find:

```text
   - `"sk-proj-" + "A"*20` → IS redacted.
```

Replace with:

```text
   - `<sk-proj prefix + 20 chars>` → IS redacted.
```

- [ ] **Step 5: Rewrite Layer P bug-fix #1 plan AC checklist (lines 247-250)**

In `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md`, find:

```text
- [ ] `sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` → `<REDACTED>`.
- [ ] `sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` → `<REDACTED>`.
- [ ] `AKIAIOSFODNN7EXAMPLE` → `<REDACTED>`; same for `ASIAIOSFODNN7EXAMPLE`.
- [ ] Multi-line PEM block `-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----` → `<REDACTED>` (whole block).
```

Replace with:

```text
- [ ] `<sk-proj prefix + 20+ url-safe chars>` → `<REDACTED>`.
- [ ] `<sk-ant-api03 prefix + 20+ url-safe chars>` → `<REDACTED>`.
- [ ] `<AKIA prefix + 16 alnum chars>` → `<REDACTED>`; same for `<ASIA prefix + 16 alnum chars>`.
- [ ] Multi-line PEM block `<-----BEGIN RSA PRIVATE KEY----- through -----END RSA PRIVATE KEY----->` → `<REDACTED>` (whole block).
```

The PEM bullet's marker uses prose-with-dashes rather than literal bracket form because the production regex `\-{5}BEGIN [A-Z ]{0,40}PRIVATE KEY\-{5}` would match a literal dashed bracket. The chevron form `<…>` is non-word and safe.

- [ ] **Step 6: Rewrite Layer P bug-fix #1 plan test-fixture transcription (lines 277-283)**

Find:

```python
        ("sk_openai", "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("sk_anthropic", "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("aws_akia", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIAIOSFODNN7EXAMPLE"),
        (
            "pem_private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\nXXXX\n-----END RSA PRIVATE KEY-----",
        ),
```

(Inside a fenced ```python` block. The first 4 `rpa_token`/`hf_token`/`fal_key`/`bearer_auth` tuples that precede this block — lines ~273-276 — are LEFT UNCHANGED.)

Replace with the same shape-description form as §3.3 of the cleanup spec (fence language flips from `python` to `text`):

```text
        ("sk_openai",       <sk-proj prefix + 20+ url-safe chars>),
        ("sk_anthropic",    <sk-ant-api03 prefix + 20+ url-safe chars>),
        ("aws_akia",        <AKIA prefix + 16 alnum chars>),
        ("aws_asia",        <ASIA prefix + 16 alnum chars>),
        ("pem_private_key", <multi-line PEM block: BEGIN…END inclusive>),
```

If the opening fence is `` ```python `` change to `` ```text ``.

- [ ] **Step 7: Re-run the dry-run script for all 4 in-scope files**

```bash
python3 -c "
import re
from pathlib import Path
patterns = [
    ('sk_token', re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('pem_private_key', re.compile(r'-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----')),
    ('hf_token', re.compile(r'\bhf_[A-Za-z0-9]{32,}\b')),
]
files = [
    'tests/providers/test_runpod_conftest.py',
    'docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md',
    'docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md',
    'docs/superpowers/specs/2026-06-01-secret-scanning-cleanup-design.md',
]
total = 0
for p in files:
    txt = Path(p).read_text()
    for name, pat in patterns:
        for m in pat.finditer(txt):
            line = txt[:m.start()].count(chr(10)) + 1
            print(f'{p}:{line} [{name}] {m.group(0)[:50]!r}')
            total += 1
assert total == 0, f'{total} hits remain'
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 8: Pre-commit + commit**

```bash
git add tests/providers/test_runpod_conftest.py \
        docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md \
        docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md
pixi run pre-commit run --files \
    tests/providers/test_runpod_conftest.py \
    docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md \
    docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md
```

Expected: ruff + ruff-format + mypy clean for the test file; the `.md` files skipped by tool-specific hooks.

```bash
git commit -m "$(cat <<'EOF'
security: scrub literal credential-prefix strings from layer-P bug-fix #1 sources

GitHub Secret Scanning flagged literals in 3 files from the Layer P Task 7
bug-fix #1 sub-plan:

- tests/providers/test_runpod_conftest.py:310-316 — 5 fixture tuples
  (sk_openai, sk_anthropic, aws_akia, aws_asia, pem_private_key) rewritten
  as runtime concatenation. Runtime tuples are byte-identical; the existing
  parametrised credential-pattern tests pass unchanged.
- docs/.../layer-p-task7-bugfix1-recording-seam-redaction-design.md:246-257
  — example tuples + sk- guard callout rewritten as shape-describing
  placeholders (fence flipped from python to text).
- docs/.../layer-p-task7-bugfix1-recording-seam-redaction.md:247-250, 277-283
  — AC checklist + test-fixture transcription get the same shape-replace
  treatment. Line 1224 unchanged (already shape-with-ellipsis).

All flagged strings were non-secret (AWS canonical examples + hand-typed
placeholders); the cleanup removes them from source so future scans pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit lands cleanly.

---

## Task 3: Add the lockdown audit (`tests/test_source_audit.py`)

**Goal:** Add a permanent fail-closed test that walks documentation + tests + repo-root markdown + `.env.example` and raises on any future credential-prefix literal — using the scanner-grade pattern set established in T1.

**Files:**
- Create: `tests/test_source_audit.py`

**Acceptance Criteria:**
- [ ] `tests/test_source_audit.py` defines its own scanner-grade `_PATTERNS` list (not imported from `tests/providers/conftest_runpod.py`).
- [ ] The audit walks: `docs/superpowers/**/*.md`, `tests/**/*.py`, plus the repo-root `README.md`, `AGENTS.md`, `PROGRESS.md`, `CLAUDE.md`, `.env.example`.
- [ ] Three pytest functions:
  - `test_no_committed_source_contains_a_credential` — main walk.
  - `test_credential_patterns_cover_expected` — self-test (>= 4 named patterns).
  - `test_audit_walker_fires_on_known_credential` — reverse-test using `tmp_path`.
- [ ] `pixi run pytest tests/test_source_audit.py -v` → 3 PASS.
- [ ] `pixi run pytest` (full suite) passes.
- [ ] `pixi run pre-commit run --files tests/test_source_audit.py` clean (ruff + ruff-format + mypy all green).

**Verify:**

```bash
pixi run pytest tests/test_source_audit.py -v
```

Expected: `3 passed`.

```bash
pixi run pytest
```

Expected: full suite passes (973+ tests, depends on baseline).

**Steps:**

- [ ] **Step 1: Confirm the file does not already exist**

```bash
ls -la tests/test_source_audit.py 2>&1
```

Expected: `cannot access ... No such file or directory`.

- [ ] **Step 2: Write the failing test first (red-first per CLAUDE.md TDD rule)**

Create `tests/test_source_audit.py` with content:

```python
"""Lockdown: no committed text-source file may contain a credential-prefix literal.

Walks documentation, tests, and repo-root markdown for scanner-grade credential
prefixes (sk-proj-, sk-ant-api03-, AKIA/ASIA, PEM, hf_ tokens). Fail-closed:
raises a single AssertionError listing every hit so a future spec or test
draft that quotes a literal credential string fails fast before it can reach
main.

Pairs with:
- _RecordingHTTPSeam.flush() in tests/providers/conftest_runpod.py (runtime
  backstop for NEW leaks at fixture-capture time).
- tests/providers/test_fixtures_audit.py (walks tests/**/*.json with the
  loose production _CREDENTIAL_PATTERNS).

Why a separate, scanner-grade pattern set:
- Production _CREDENTIAL_PATTERNS in tests/providers/conftest_runpod.py is
  intentionally loose (8-char minimum on prefix tails, generic Bearer match)
  to catch test-time leaks aggressively. Applying it source-tree-wide trips
  on ~90 unrelated internal test tokens and shape examples.
- This audit instead targets what GitHub Secret Scanning actually flags:
  AWS access keys, OpenAI/Anthropic sk- tokens, PEM private keys, and
  HuggingFace tokens at canonical length.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{32,}\b")),
]


@dataclass(frozen=True)
class SourceLeakHit:
    """Single credential-shaped match in a source file."""

    path: Path
    line: int
    column: int
    pattern_name: str
    match_snippet: str


def _walked_paths() -> list[Path]:
    """Enumerate the files the audit walks.

    Order is deterministic for stable assertion messages: globs first
    (sorted), then the explicit repo-root files.
    """
    paths: list[Path] = []
    paths.extend(sorted((_REPO_ROOT / "docs" / "superpowers").rglob("*.md")))
    paths.extend(sorted((_REPO_ROOT / "tests").rglob("*.py")))
    for name in ("README.md", "AGENTS.md", "PROGRESS.md", "CLAUDE.md", ".env.example"):
        candidate = _REPO_ROOT / name
        if candidate.exists():
            paths.append(candidate)
    return paths


def _audit_text(text: str, path: Path) -> list[SourceLeakHit]:
    """Apply every `_PATTERNS` regex to *text* and collect every match."""
    hits: list[SourceLeakHit] = []
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            column = m.start() - (text.rfind("\n", 0, m.start()) + 1) + 1
            snippet = m.group(0)
            if len(snippet) > 40:
                snippet = snippet[:40] + "..."
            hits.append(
                SourceLeakHit(
                    path=path,
                    line=line,
                    column=column,
                    pattern_name=name,
                    match_snippet=snippet,
                )
            )
    return hits


def _format_offenders(hits: list[SourceLeakHit]) -> str:
    """Build a human-readable multi-line block describing every leak."""
    if not hits:
        return ""
    lines = [f"Found {len(hits)} credential-prefix literal(s) in source files:"]
    for h in hits:
        rel = h.path.relative_to(_REPO_ROOT)
        lines.append(
            f"  {rel}:{h.line}:{h.column} [{h.pattern_name}] {h.match_snippet!r}"
        )
    lines.append(
        "Either rewrite the literal as runtime concatenation (tests) or a "
        "shape-describing placeholder (docs), or — if the hit is intentional "
        "and shape-matches the regex — tighten the regex in _PATTERNS."
    )
    return "\n".join(lines)


def test_no_committed_source_contains_a_credential() -> None:
    """Every walked source file must be free of scanner-grade credential literals."""
    all_hits: list[SourceLeakHit] = []
    for path in _walked_paths():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        all_hits.extend(_audit_text(text, path))
    assert not all_hits, _format_offenders(all_hits)


def test_credential_patterns_cover_expected() -> None:
    """Audit's pattern set must cover the canonical scanner-grade names.

    Guards against a future refactor that empties the list (which would
    silently disable the lockdown).
    """
    assert len(_PATTERNS) >= 4
    names = {name for name, _ in _PATTERNS}
    expected = {"sk_token", "aws_access_key", "pem_private_key", "hf_token"}
    missing = expected - names
    assert not missing, f"_PATTERNS missing canonical names: {missing}"


def test_audit_walker_fires_on_known_credential(tmp_path: Path) -> None:
    """Reverse-test: planting a known credential literal must produce one hit.

    Confirms the audit's matcher logic still works even if every real file
    in the repo passes — without this, the main test could no-op forever.
    """
    leak_file = tmp_path / "rogue.md"
    leak_file.write_text(
        "Some prose.\n\nA literal: AKIA" + "IOSFODNN7EXAMPLE\n\nMore prose.\n"
    )
    hits = _audit_text(leak_file.read_text(), leak_file)
    assert len(hits) == 1
    assert hits[0].pattern_name == "aws_access_key"
    assert "AKIA" in hits[0].match_snippet
```

Notes:
- The reverse-test plants its literal via `"AKIA" + "IOSFODNN7EXAMPLE"` so this test file itself does NOT contain the full literal (the audit walks `tests/**/*.py`, so a literal in this file would self-trip).
- `_walked_paths()` is exposed (no leading `_` would help, but the underscore is fine — pytest doesn't introspect this; the reverse-test bypasses it intentionally).
- `_audit_text` is exposed for the reverse-test to call against a single in-memory tree without invoking the full walker.

- [ ] **Step 3: Run the new tests to confirm they pass after Tasks 1 + 2**

```bash
pixi run pytest tests/test_source_audit.py -v
```

Expected: `3 passed`.

- If `test_no_committed_source_contains_a_credential` FAILS:
  - Means T1 or T2 missed a literal. Read the assertion message — it lists every `(file, line, col, pattern_name, match_snippet)` tuple.
  - For each hit: rewrite the literal as concat (tests/.py) or shape (docs/.md), commit a follow-up fix, re-run.

- If `test_credential_patterns_cover_expected` FAILS:
  - Means the `_PATTERNS` list at the top of the file was renamed or one of the canonical names dropped. Restore.

- If `test_audit_walker_fires_on_known_credential` FAILS:
  - Means the matcher regression: a regex was changed or `_audit_text` returns wrong shape.

- [ ] **Step 4: Run the full pytest suite to confirm no regression**

```bash
pixi run pytest
```

Expected: full suite passes. Total test count should rise by 3 vs. pre-T3 baseline.

- [ ] **Step 5: Pre-commit clean**

```bash
git add tests/test_source_audit.py
pixi run pre-commit run --files tests/test_source_audit.py
```

Expected: ruff + ruff-format + mypy all clean. If mypy complains about missing return type annotations, add them; if ruff complains about line length, reflow. Do NOT use `# type: ignore` or `# noqa` — fix the underlying issue.

- [ ] **Step 6: Commit**

```bash
git commit -m "$(cat <<'EOF'
test(audit): tests/test_source_audit.py — fail-closed lockdown for source-tree credential literals

Walks docs/superpowers/**/*.md + tests/**/*.py + repo-root markdown +
.env.example, applies a scanner-grade pattern subset (sk_token,
aws_access_key, pem_private_key, hf_token@32+ chars), and raises on
any match.

Three tests:
- test_no_committed_source_contains_a_credential — main walk; GREEN
  after the cleanup in the previous commit.
- test_credential_patterns_cover_expected — self-test asserting the
  pattern list still names the 4 canonical scanner-grade entries
  (catches a refactor that empties the list).
- test_audit_walker_fires_on_known_credential — reverse-test plants a
  literal in tmp_path and confirms exactly one hit (catches a matcher
  regression where the main test could silently no-op).

Pattern set is independent of the production _CREDENTIAL_PATTERNS in
tests/providers/conftest_runpod.py — production patterns are loose
(8-char prefix tails, generic Bearer) to catch test-time leaks; this
audit is tight (matches what GitHub Secret Scanning catches) so it
doesn't trip on internal shape examples like rpa_xxxxxxxx or
ubiquitous test tokens like Bearer test-token.

Together with the runtime _RecordingHTTPSeam backstop and the JSON-
fixture audit, this closes the source-tree gap that fired GitHub
Secret Scanning on commit edc8b3e.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

Expected: commit lands cleanly. T3 done.

---

## Post-Plan Manual Steps

After all three tasks land on `main`:

1. **Dismiss the GitHub Secret Scanning alert.**
   - Navigate to https://github.com/killett/kinoforge/security/secret-scanning.
   - For each flagged secret (AWS Temporary Access Key ID #1, etc.), click the alert and select "Close as" → "Used in tests" / "False positive".
   - The alert state changes to `resolved`. GitHub does not re-flag the same SHAs after closure.

2. **Confirm the audit gates pre-commit + CI.** The new test runs as part of `pixi run test`, which is part of pre-commit and CI. No additional wiring required.

3. **Optional: update `PROGRESS.md`.** Add an entry under "Single next action" or a fresh `Phase N — Secret-Scanning Cleanup` section recording the commit SHAs (T1, T2, T3) and the test-count delta (+3). The plan does not include this step because PROGRESS hygiene is a separate concern; the user picks whether to fold it into T3 or as a follow-up commit.

---

## Self-Review

**Spec coverage:**
- Spec §2.1 in-scope items 1-3 → covered by T2 (item 1), T3 (item 2), and the Post-Plan UI step (item 3).
- Spec §2.2 out-of-scope items → not touched (verified by the file table at the top of this plan).
- Spec §3.1 → T2 Steps 1-2.
- Spec §3.2 → T2 Steps 3-4.
- Spec §3.3 → T2 Steps 5-6.
- Spec §3.4 → T3 Steps 1-6.
- Spec §4 (test plan) → T2 Step 2 verifies existing tests; T3 Step 4 runs full suite.
- Spec §5 (risk/rollback) → no plan task; risk inherent (concat byte-identical, no production code touched).
- Spec §6 (post-merge) → Post-Plan Manual Steps.
- Spec §7 (AC) → AC list at each task references the spec's ACs by intent.
- Spec amendment (mid-plan pattern-set narrowing) → T1 entirely.

**Placeholder scan:** none found. All steps either show exact text replacements, exact shell commands with expected output, or exact code blocks.

**Type consistency:** `_PATTERNS` (audit pattern list), `SourceLeakHit` (audit hit dataclass), `_walked_paths()` / `_audit_text()` / `_format_offenders()` (audit helpers) all referenced consistently between T1 (spec amendment) and T3 (implementation). `tests/providers/conftest_runpod.py::_CREDENTIAL_PATTERNS` (production) is never imported by the audit — distinct names prevent accidental coupling.
