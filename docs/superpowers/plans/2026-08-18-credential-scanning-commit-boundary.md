# Credential Scanning at the Commit Boundary — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give kinoforge one credential-pattern list, a pre-commit hook that blocks a credential in staged content, in-repo Claude Code hooks that travel with a clone, and a fail-closed guard over the whole tracked tree.

**Architecture:** A stdlib-only module `src/kinoforge/core/credential_patterns.py` becomes the single list, split into a loose tier (redaction, over-matches by design) and a strict tier (anything that blocks). `tools/scan_secrets.py` reads `git diff --cached --unified=0` and scans added lines only, so partial staging is respected and binary files produce nothing to scan. Two stdlib-only hooks land at `.claude/hooks/` — a PreToolUse blocker that denies credential-exfiltrating Bash, and the ported PostToolUse scrubber — registered in a committed `.claude/settings.json`. The parity test loses its skip path.

**Tech Stack:** Python 3.12 stdlib (`re`, `subprocess`, `json`, `pathlib`), pytest, pre-commit local hooks driven by `pixi run python`, git plumbing (`diff --cached`, `ls-files -z`).

**Global Constraints:**
- **No credential value is ever printed.** Scanner output, test failure messages, and hook stderr print `<REDACTED:{pattern_name}>` excerpts only. Applies to every task.
- **The `.claude/hooks/*.py` files must run under bare `python3`** — no pixi env, no project on `sys.path`, no third-party imports. That is why their pattern list is duplicated rather than imported.
- **Test credential literals are built by runtime concatenation** (`"AKIA" + "QWERTYUIOPASDFGH"`), never written as a single literal — otherwise the tracked-tree guard flags the test file that tests the guard.
- **Strict-tier acceptance bar:** `python tools/scan_secrets.py --all-tracked` exits 0 over all tracked files. Tune patterns/markers to reach it; never allowlist a file wholesale.
- Conventional Commits, `pixi run pre-commit run --all-files` before every commit, commit after every task.
- No live spend. Every test in this plan is offline.

**User decisions (already made):**
- "Block + scrub" — ship both a PreToolUse deny hook and the PostToolUse scrubber; neither replaces the other.
- Shared list lives at `src/kinoforge/core/credential_patterns.py` (package module, importable by tools and tests).
- Design approved as written: "looks right".

**Spec:** `docs/superpowers/specs/2026-08-18-credential-scanning-commit-boundary-design.md`

---

## File structure

| File | Responsibility | Task |
| --- | --- | --- |
| `src/kinoforge/core/credential_patterns.py` | the one list; tiers, placeholder logic, `redact_string`, `iter_findings` | 0 |
| `tests/core/test_credential_patterns.py` | tier membership, redaction order, placeholder suppression, finding shape | 0 |
| `tools/_redact.py` | thin re-export; `safe_print` unchanged | 1 |
| `tests/providers/conftest_runpod.py` | imports the shared full list | 1 |
| `tools/scan_secrets.py` | staged-diff / all-tracked / stdin scanner, CLI entry | 2 |
| `tests/tools/test_scan_secrets.py` | staging semantics, binary, suppression, exit codes | 2 |
| `.pre-commit-config.yaml` | registers the scanner hook | 3 |
| `.claude/hooks/redact_secrets.py` | PostToolUse scrub (duplicate list) | 4 |
| `.claude/hooks/block_secret_reads.py` | PreToolUse deny | 4 |
| `.claude/settings.json` | registers both hooks | 4 |
| `tests/hooks/test_block_secret_reads.py` | deny surface + non-deny surface | 4 |
| `tests/test_redact_hook_parity.py` | fail-closed parity, both hooks | 5 |
| `tests/test_source_audit.py` | standing guard over all tracked files | 5 |
| `CLAUDE.md`, `AGENTS.md`, `PROGRESS.md` | credential-safety rules, pointer, resume state | 6 |

---

### Task 0: Shared credential-pattern module

**Goal:** One stdlib-only module holding every credential pattern, split into loose and strict tiers, with placeholder suppression and a redacted `Finding` type.

**Files:**
- Create: `src/kinoforge/core/credential_patterns.py`
- Test: `tests/core/test_credential_patterns.py`

**Acceptance Criteria:**
- [ ] `CREDENTIAL_PATTERNS` contains every pattern from all four existing lists, with the strongest variant where they disagreed (`AKIA|ASIA`, full `BEGIN…END` PEM span)
- [ ] `STRICT_PATTERNS` is exactly the `strict=True` subset, in declaration order
- [ ] `bearer_auth` is declared first, so `Bearer rpa_…` redacts to `<REDACTED:bearer_auth>`
- [ ] `looks_like_placeholder` suppresses on marker words and on the `kinoforge: allow-secret` pragma
- [ ] `iter_findings` yields `Finding` whose `redacted_excerpt` contains no matched credential text
- [ ] Module imports nothing outside the stdlib and nothing from `kinoforge`

**Verify:** `pixi run python -m pytest tests/core/test_credential_patterns.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_credential_patterns.py`:

```python
"""Behaviour of the single shared credential-pattern list.

Every credential literal here is built by runtime concatenation so this
file does not itself trip the tracked-tree guard (tests/test_source_audit.py).
"""

from __future__ import annotations

import re

from kinoforge.core import credential_patterns as cp

AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"  # 4 + 16, canonical AWS shape
HF_KEY = "hf_" + "a" * 34
RPA_KEY = "rpa_" + "B" * 30


def test_strict_is_the_strict_subset_in_declaration_order() -> None:
    """STRICT_PATTERNS must be derived from CREDENTIAL_PATTERNS, not hand-maintained.

    Fails if someone appends a strict pattern to only one of the two lists.
    """
    expected = [p for p in cp.CREDENTIAL_PATTERNS if p.strict]
    assert cp.STRICT_PATTERNS == expected
    assert len(cp.STRICT_PATTERNS) < len(cp.CREDENTIAL_PATTERNS)


def test_aws_pattern_matches_sts_temporary_credentials() -> None:
    """The F7 gap: ASIA... STS creds were invisible to the user-scope hook."""
    sts = "ASIA" + "QWERTYUIOPASDFGH"
    names = {f.pattern_name for f in cp.iter_findings(sts)}
    assert "aws_access_key" in names


def test_pem_redaction_covers_the_body_not_just_the_marker() -> None:
    """conftest's full-span variant wins over the hook's marker-only one."""
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxSECRETBODYxxxx\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = cp.redact_string(pem)
    assert "SECRETBODY" not in out
    assert "<REDACTED:pem_private_key>" in out


def test_bearer_declared_first_so_header_collapses_whole() -> None:
    """Ordering guarantee inherited from tools/_redact.py."""
    out = cp.redact_string(f"Authorization: Bearer {RPA_KEY}")
    assert out.endswith("<REDACTED:bearer_auth>")
    assert "rpa_" not in out


def test_credential_assignment_catches_a_pasted_export_line() -> None:
    """The actual leak vector: a terminal line pasted into a tracked file."""
    line = "export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYzcvKQ7MDENG"
    names = {f.pattern_name for f in cp.iter_findings(line)}
    assert "credential_assignment" in names


def test_empty_assignment_in_env_example_does_not_match() -> None:
    """.env.example's `VAR=` lines must not block every commit."""
    assert not list(cp.iter_findings("AWS_SECRET_ACCESS_KEY="))


def test_placeholder_marker_suppresses_a_real_shaped_match() -> None:
    """Same value, marker present -> no finding. Keeps .env.example + docs clean."""
    assert list(cp.iter_findings(AWS_KEY))  # baseline: it does match
    assert not list(cp.iter_findings(f"{AWS_KEY}  # example value"))
    assert not list(cp.iter_findings("AKIA" + "XXXXXXXXXXXXXXXX"))
    assert not list(cp.iter_findings(f"key = ${{AWS_KEY}}  {AWS_KEY}"))


def test_allow_pragma_suppresses_a_bare_match() -> None:
    """Escape hatch for a synthetic value with no marker word in it."""
    assert not list(cp.iter_findings(f"{AWS_KEY}  # kinoforge: allow-secret"))


def test_skip_placeholders_false_reports_everything() -> None:
    """Suppression must be a caller choice, not baked into the matcher."""
    hits = list(cp.iter_findings(f"{AWS_KEY} # example", skip_placeholders=False))
    assert [h.pattern_name for h in hits] == ["aws_access_key"]


def test_finding_excerpt_never_contains_the_credential() -> None:
    """A scanner that leaks what it caught is worse than no scanner."""
    text = f"line one\nexport KEY={AWS_KEY} trailing\nline three\n"
    (finding,) = [f for f in cp.iter_findings(text) if f.pattern_name == "aws_access_key"]
    assert finding.line_no == 2
    assert AWS_KEY not in finding.redacted_excerpt
    assert "<REDACTED:aws_access_key>" in finding.redacted_excerpt


def test_loose_tier_still_scrubs_short_project_tokens() -> None:
    """Redaction keeps today's aggressive behaviour; only blocking is strict."""
    short = "hf_" + "abcdefgh"
    assert "<REDACTED:" in cp.redact_string(short)
    assert not list(cp.iter_findings(short))  # strict tier ignores it


def test_every_pattern_has_a_unique_snake_case_name() -> None:
    """Names appear in <REDACTED:{name}> markers and in parity assertions."""
    names = [p.name for p in cp.CREDENTIAL_PATTERNS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `pixi run python -m pytest tests/core/test_credential_patterns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kinoforge.core.credential_patterns'`

- [ ] **Step 3: Write the module**

Create `src/kinoforge/core/credential_patterns.py`:

```python
"""Single source of truth for kinoforge's credential regexes.

Four lists used to disagree (``tools/_redact.py``, the Claude Code
transcript hook, ``tests/providers/conftest_runpod.py``,
``tests/test_source_audit.py``) and the weakest was the one wired into
repo tooling. This module is the union, with the strongest variant kept
wherever they differed.

Two tiers, because the two consumers have opposite cost asymmetries:

* **loose** (``strict=False``) — aggressive shapes used by the
  *redactors*. Over-matching costs a confusing log line.
* **strict** (``strict=True``) — canonical lengths and unambiguous
  prefixes, used by anything that *blocks* (the pre-commit scanner, the
  tracked-tree guard). Over-matching there teaches ``--no-verify``,
  after which the scanner protects nothing.

Declaration order matters: ``bearer_auth`` is first so a
``Bearer rpa_…`` header collapses to ``<REDACTED:bearer_auth>`` rather
than leaking the word ``Bearer`` around a redacted body.

Stdlib-only and free of ``kinoforge`` imports on purpose — ``tools/``
scripts and test fixtures both take it without pulling the package's
dependency graph. The ``.claude/hooks/*.py`` copies cannot import it at
all (they run under bare ``python3``); ``tests/test_redact_hook_parity.py``
is the drift guard for those.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import NamedTuple


class CredentialPattern(NamedTuple):
    """One named credential shape.

    Attributes:
        name: snake_case identifier; appears in ``<REDACTED:{name}>``
            markers, scanner output, and parity assertions.
        regex: Compiled pattern.
        strict: True when a match is overwhelmingly likely to be a real
            credential, i.e. safe to block a commit on.
    """

    name: str
    regex: re.Pattern[str]
    strict: bool


class Finding(NamedTuple):
    """One strict-tier match, with the credential already removed.

    Attributes:
        pattern_name: The :attr:`CredentialPattern.name` that matched.
        line_no: 1-based line number within the scanned text.
        col: 1-based column of the match start.
        redacted_excerpt: The matching line with every credential shape
            replaced. Safe to print to a terminal, a CI log, or a
            conversation transcript.
    """

    pattern_name: str
    line_no: int
    col: int
    redacted_excerpt: str


# Credential-bearing environment variables from .env.example. Names only —
# GOOGLE_APPLICATION_CREDENTIALS (a path) and DOCKERHUB_USERNAME (a username)
# are deliberately absent.
CREDENTIAL_ENV_VARS: tuple[str, ...] = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AZURE_CLIENT_SECRET",
    "B2_APPLICATION_KEY",
    "CIVITAI_TOKEN",
    "DOCKERHUB_TOKEN",
    "FAL_KEY",
    "GH_TOKEN",
    "HF_TOKEN",
    "KINOFORGE_R2_ACCESS_KEY_ID",
    "KINOFORGE_R2_SECRET_ACCESS_KEY",
    "LAMBDA_API_KEY",
    "LUMAAI_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "REPLICATE_API_TOKEN",
    "RUNPOD_API_KEY",
    "RUNPOD_TERMINATE_KEY",
    "RUNWAYML_API_SECRET",
    "VAST_API_KEY",
)

_ASSIGNMENT_RE = re.compile(
    r"\b(?:" + "|".join(CREDENTIAL_ENV_VARS) + r")\s*[=:]\s*[\"']?[^\s\"'#]{8,}"
)

CREDENTIAL_PATTERNS: list[CredentialPattern] = [
    # ---- loose tier: redaction only, over-matches by design ----------------
    CredentialPattern("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"), False),
    CredentialPattern("rpa_token_loose", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b"), False),
    CredentialPattern("hf_token_loose", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b"), False),
    # ---- strict tier: may block a commit -----------------------------------
    CredentialPattern("rpa_token", re.compile(r"\brpa_[A-Za-z0-9]{24,}\b"), True),
    CredentialPattern("hf_token", re.compile(r"\bhf_[A-Za-z0-9]{32,}\b"), True),
    CredentialPattern("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b"), True),
    CredentialPattern("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), True),
    CredentialPattern("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), True),
    CredentialPattern(
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
        True,
    ),
    CredentialPattern("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), True),
    CredentialPattern("github_app", re.compile(r"\b(?:gho|ghu|ghs)_[A-Za-z0-9]{36,}\b"), True),
    CredentialPattern("replicate_token", re.compile(r"\br8_[A-Za-z0-9]{30,}\b"), True),
    CredentialPattern("runway_key", re.compile(r"\bkey[-_][A-Za-z0-9]{30,}\b"), True),
    CredentialPattern("slack_token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b"), True),
    CredentialPattern("jwt", re.compile(r"\beyJ[A-Za-z0-9._=-]{20,}\b"), True),
    CredentialPattern("luma_key", re.compile(r"\bluma-[A-Za-z0-9-]{20,}\b"), True),
    CredentialPattern("modal_token", re.compile(r"\b(?:ak|as)-[A-Za-z0-9]{20,}\b"), True),
    CredentialPattern("lambda_key", re.compile(r"\bsecret_[A-Za-z0-9]+_[0-9a-f]{32,}\b"), True),
    CredentialPattern("gcp_access_token", re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}\b"), True),
    CredentialPattern("credential_assignment", _ASSIGNMENT_RE, True),
]

STRICT_PATTERNS: list[CredentialPattern] = [p for p in CREDENTIAL_PATTERNS if p.strict]

#: Words whose presence on a line marks its credential-shaped text as synthetic.
PLACEHOLDER_MARKERS: frozenset[str] = frozenset(
    {
        "example",
        "xxxx",
        "placeholder",
        "${",
        "your-",
        "your_",
        "dummy",
        "fake",
        "changeme",
        "redacted",
        "deadbeef",
        "notreal",
        "sample",
    }
)

#: Line-scoped escape hatch for a synthetic value carrying no marker word.
ALLOW_PRAGMA = "kinoforge: allow-secret"


def looks_like_placeholder(match_text: str, line: str) -> bool:
    """Report whether a credential-shaped match is synthetic.

    Args:
        match_text: The matched substring.
        line: The full line the match came from.

    Returns:
        True when the match or its line carries a
        :data:`PLACEHOLDER_MARKERS` word or the :data:`ALLOW_PRAGMA`
        comment, case-insensitively.
    """
    haystack = f"{match_text}\n{line}".lower()
    if ALLOW_PRAGMA in haystack:
        return True
    return any(marker in haystack for marker in PLACEHOLDER_MARKERS)


def redact_string(text: str) -> str:
    """Replace every credential-pattern match in *text* with a named marker.

    Applies **all** patterns (both tiers) in declaration order, so a
    ``Bearer …`` header collapses before its inner token is considered.

    Args:
        text: Arbitrary text — log line, exception repr, JSON body.

    Returns:
        A copy of *text* with each match replaced by
        ``<REDACTED:{pattern_name}>``. Non-matching text is preserved
        verbatim.
    """
    for pattern in CREDENTIAL_PATTERNS:
        text = pattern.regex.sub(f"<REDACTED:{pattern.name}>", text)
    return text


def iter_findings(
    text: str,
    *,
    patterns: Iterable[CredentialPattern] | None = None,
    skip_placeholders: bool = True,
) -> Iterator[Finding]:
    """Yield one :class:`Finding` per credential-shaped match in *text*.

    Args:
        text: Text to scan. Scanned line by line so a finding can carry a
            line number and a redacted excerpt.
        patterns: Patterns to apply. Defaults to :data:`STRICT_PATTERNS` —
            the tier that is safe to block on. Pass
            :data:`CREDENTIAL_PATTERNS` for an aggressive sweep.
        skip_placeholders: When True (default), matches suppressed by
            :func:`looks_like_placeholder` are not yielded.

    Yields:
        Findings in line order, then pattern-declaration order within a
        line. ``redacted_excerpt`` is the whole line passed through
        :func:`redact_string`, so it can never carry the matched value.

    Notes:
        Multi-line shapes (``pem_private_key``) are detected on their
        opening line only; the whole-line redaction in the excerpt means
        no body text is exposed regardless.
    """
    selected = list(STRICT_PATTERNS if patterns is None else patterns)
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in selected:
            for match in pattern.regex.finditer(line):
                if skip_placeholders and looks_like_placeholder(match.group(0), line):
                    continue
                yield Finding(
                    pattern_name=pattern.name,
                    line_no=line_no,
                    col=match.start() + 1,
                    redacted_excerpt=redact_string(line).strip()[:200],
                )
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `pixi run python -m pytest tests/core/test_credential_patterns.py -v`
Expected: PASS (12 tests). If `test_pem_redaction_covers_the_body_not_just_the_marker` fails, the multi-line PEM span is being applied per-line by `redact_string` — it is not; `redact_string` sees the whole string. Only `iter_findings` is line-scoped.

- [ ] **Step 5: Lint, type-check, commit**

```bash
pixi run pre-commit run --files \
  src/kinoforge/core/credential_patterns.py \
  tests/core/test_credential_patterns.py
git add src/kinoforge/core/credential_patterns.py tests/core/test_credential_patterns.py
git commit -m "feat(credentials): add the single shared credential-pattern list"
```

---

### Task 1: Rewire the existing consumers onto the shared list

**Goal:** `tools/_redact.py` and `tests/providers/conftest_runpod.py` stop carrying their own lists and import the shared one, with no behaviour change for their callers.

**Files:**
- Modify: `tools/_redact.py:27-62`
- Modify: `tests/providers/conftest_runpod.py:186-206`
- Test: `tests/tools/test_redact.py` (existing — must stay green unmodified)

**Acceptance Criteria:**
- [ ] `tools/_redact.py` defines no regex of its own; `_CREDENTIAL_PATTERNS` is re-exported from the shared module for backward compatibility
- [ ] `tools/_redact.redact_string` and `safe_print` keep their signatures and behaviour
- [ ] `conftest_runpod._CREDENTIAL_PATTERNS` is the shared full list; its `(name, regex)` tuple-unpacking callers keep working, or are updated in the same commit
- [ ] The existing test files for both are untouched and pass
- [ ] `rg -c 'AKIA' tests/providers/conftest_runpod.py tools/_redact.py` returns no pattern definitions

**Verify:** `pixi run python -m pytest tests/tools/test_redact.py tests/providers/ -q` → all pass

**Steps:**

- [ ] **Step 1: Check what breaks before changing anything**

The shared list yields 3-field `CredentialPattern` NamedTuples; both consumers unpack 2-field tuples (`for _name, pattern in …`). Find every unpack site:

```bash
rg -n 'for .*in .*_CREDENTIAL_PATTERNS' tools/ tests/
```

Expected hits: `tools/_redact.py:60`, `tests/providers/conftest_runpod.py:223`, `tests/test_redact_hook_parity.py:76-77`. `CredentialPattern` is a NamedTuple, so `for name, pat, _strict in …` works, and attribute access (`p.name`, `p.regex`) is clearer — use attribute access at every site.

- [ ] **Step 2: Rewrite `tools/_redact.py`**

Replace lines 27-62 (the pattern block and `redact_string` body) with:

```python
from kinoforge.core.credential_patterns import CREDENTIAL_PATTERNS
from kinoforge.core.credential_patterns import redact_string as _shared_redact

# Backward-compatible alias. The list itself now lives in
# kinoforge.core.credential_patterns — see that module's docstring for
# the loose/strict tier split. Callers that imported this name keep
# working; new code should import from the shared module directly.
_CREDENTIAL_PATTERNS = CREDENTIAL_PATTERNS


def redact_string(s: str) -> str:
    """Replace every credential-pattern match in *s* with a named marker.

    Thin delegate to
    :func:`kinoforge.core.credential_patterns.redact_string`; kept as
    ``tools``' public surface so live-capture scripts do not need to know
    where the list lives.

    Args:
        s: Arbitrary text — log line, exception ``repr``, JSON body.

    Returns:
        A copy of *s* with each match replaced by
        ``<REDACTED:{pattern_name}>``.
    """
    return _shared_redact(s)
```

Update the module docstring: the "Single source of truth for `tools/`" paragraph is now false — say the source of truth is `kinoforge.core.credential_patterns` and this module is the `tools/` entry point.

- [ ] **Step 3: Rewrite the `conftest_runpod.py` pattern block**

Replace lines 186-206 (comment block + `_CREDENTIAL_PATTERNS` literal) with:

```python
# Credential vocabulary now lives in kinoforge.core.credential_patterns —
# the full list (loose tier included), because fixture capture should
# over-scrub rather than under-scrub. The two patterns this module used to
# own outright (AKIA|ASIA, full-span PEM) are in that list.
from kinoforge.core.credential_patterns import CREDENTIAL_PATTERNS as _CREDENTIAL_PATTERNS
```

Then update its `_redact_string` loop (line ~223) to attribute access:

```python
    for pattern in _CREDENTIAL_PATTERNS:
        s = pattern.regex.sub("<REDACTED>", s)
```

Keep the `<REDACTED>` (unnamed) replacement — the fixture-audit assertions match on that exact literal.

- [ ] **Step 4: Run the affected suites**

Run: `pixi run python -m pytest tests/tools/test_redact.py tests/providers/ tests/test_source_audit.py -q`
Expected: PASS. `tests/test_redact_hook_parity.py` may now fail or skip — it is rewritten in Task 5; if it fails on the tuple unpack, fix only the unpack (`{p.regex.pattern for p in proj._CREDENTIAL_PATTERNS}`) and leave its skip logic alone for now.

- [ ] **Step 5: Full suite, then commit**

```bash
pixi run python -m pytest -q -x
pixi run pre-commit run --files tools/_redact.py tests/providers/conftest_runpod.py
git add tools/_redact.py tests/providers/conftest_runpod.py tests/test_redact_hook_parity.py
git commit -m "refactor(credentials): point the existing redactors at the shared list"
```

---

### Task 2: Staged-content scanner

**Goal:** `tools/scan_secrets.py` blocks a commit whose **staged** content carries a strict-tier credential, respecting partial staging and skipping binary files.

**Files:**
- Create: `tools/scan_secrets.py`
- Test: `tests/tools/test_scan_secrets.py`

**Acceptance Criteria:**
- [ ] Scans only added lines from `git diff --cached --unified=0`; a credential present in the working tree but not staged does not block
- [ ] A credential in a staged hunk blocks with exit 1 even when the same file has unstaged edits
- [ ] Binary files produce no findings and no exception
- [ ] Placeholder-marked and pragma-marked matches do not block
- [ ] Output names `path:line pattern_name` plus a redacted excerpt; the credential value never appears in stdout or stderr
- [ ] Exit codes: 0 clean, 1 findings, 2 git/usage error (a git failure is never reported as clean)
- [ ] `--all-tracked` scans every `git ls-files` path, skipping non-UTF-8 files
- [ ] `--stdin` scans piped text

**Verify:** `pixi run python -m pytest tests/tools/test_scan_secrets.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_scan_secrets.py`:

```python
"""Staging semantics of the pre-commit credential scanner.

These tests build real throwaway git repos and run real `git diff
--cached`. Mocking git would test nothing: the entire point of the unit
is its interaction with the staging area.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools import scan_secrets

AWS_KEY = "AKIA" + "QWERTYUIOPASDFGH"
OTHER_KEY = "ASIA" + "ZXCVBNMASDFGHJKL"


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside *repo* and return stdout."""
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one committed file and identity configured."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "notes.md").write_text("clean line\n")
    _git(tmp_path, "add", "notes.md")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_staged_credential_blocks(repo: Path) -> None:
    """The core case: a real-shaped key in staged content exits 1."""
    (repo / "notes.md").write_text(f"clean line\nexport KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]
    assert scan_secrets.main(["--repo", str(repo)]) == 1


def test_placeholder_marked_value_does_not_block(repo: Path) -> None:
    """Same value, marker word present -> commit proceeds.

    Without this, .env.example and the design docs block every commit and
    everyone learns --no-verify.
    """
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}  # example only\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []
    assert scan_secrets.main(["--repo", str(repo)]) == 0


def test_only_the_staged_hunk_is_scanned(repo: Path) -> None:
    """Partial staging: `git add -p` stages a hunk, not a file.

    Stage a clean edit, then dirty the working tree with a credential.
    A working-tree scanner would block here; a staged-content scanner
    must not.
    """
    (repo / "notes.md").write_text("clean line\nsecond clean line\n")
    _git(repo, "add", "notes.md")
    (repo / "notes.md").write_text(f"clean line\nsecond clean line\nKEY={AWS_KEY}\n")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_unstaged_removal_does_not_hide_a_staged_credential(repo: Path) -> None:
    """Inverse of the above — the staged bytes are what gets committed."""
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    (repo / "notes.md").write_text("clean line\n")  # working tree now clean
    findings = scan_secrets.scan_staged(repo, paths=[])
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]


def test_binary_file_is_skipped_without_crashing(repo: Path) -> None:
    """A staged PNG-ish blob must not raise UnicodeDecodeError."""
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
    _git(repo, "add", "blob.bin")
    assert scan_secrets.scan_staged(repo, paths=[]) == []
    assert scan_secrets.main(["--repo", str(repo)]) == 0


def test_deleted_lines_are_not_findings(repo: Path) -> None:
    """Removing a credential is a fix, not a leak."""
    (repo / "notes.md").write_text(f"clean line\nKEY={AWS_KEY}  # kinoforge: allow-secret\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "planted")
    (repo / "notes.md").write_text("clean line\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_pragma_suppresses_a_bare_synthetic_token(repo: Path) -> None:
    """Escape hatch, line-scoped and visible in review."""
    (repo / "notes.md").write_text(f"KEY={OTHER_KEY}  # kinoforge: allow-secret\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.scan_staged(repo, paths=[]) == []


def test_path_filter_limits_the_scan(repo: Path) -> None:
    """pre-commit passes filenames; the scan must honour them."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    (repo / "other.md").write_text("clean\n")
    _git(repo, "add", "notes.md", "other.md")
    assert scan_secrets.scan_staged(repo, paths=["other.md"]) == []
    assert scan_secrets.scan_staged(repo, paths=["notes.md"]) != []


def test_git_failure_exits_2_not_0(tmp_path: Path) -> None:
    """A broken git invocation must never be reported as 'clean'."""
    assert scan_secrets.main(["--repo", str(tmp_path / "not-a-repo")]) == 2


def test_report_never_prints_the_credential(repo: Path, capsys) -> None:
    """The scanner must not leak what it caught into a terminal or CI log."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    assert scan_secrets.main(["--repo", str(repo)]) == 1
    captured = capsys.readouterr()
    assert AWS_KEY not in captured.out + captured.err
    assert "aws_access_key" in captured.out
    assert "notes.md:1" in captured.out


def test_stdin_mode(monkeypatch, capsys) -> None:
    """--stdin gives an ad-hoc check without touching a repo."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(f"KEY={AWS_KEY}\n"))
    assert scan_secrets.main(["--stdin"]) == 1


def test_all_tracked_scans_the_repo(repo: Path) -> None:
    """--all-tracked is the standing guard's engine."""
    (repo / "notes.md").write_text(f"KEY={AWS_KEY}\n")
    _git(repo, "add", "notes.md")
    _git(repo, "commit", "-qm", "planted")
    findings = scan_secrets.scan_all_tracked(repo)
    assert [f.pattern_name for _p, f in findings] == ["aws_access_key"]
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `pixi run python -m pytest tests/tools/test_scan_secrets.py -v`
Expected: FAIL — `ImportError: cannot import name 'scan_secrets' from 'tools'`

- [ ] **Step 3: Write the scanner**

Create `tools/scan_secrets.py`:

```python
#!/usr/bin/env python3
"""Block a commit whose staged content carries a credential.

``.gitignore`` matches paths; it cannot stop a credential pasted into an
already-tracked file — the realistic leak vector in a repo where
``PROGRESS.md`` is hundreds of KB of pasted terminal output. This scanner
closes that gap at ``git commit``.

It reads **staged** content (``git diff --cached --unified=0``), not the
working tree, so ``git add -p`` is handled correctly: only the hunks
actually being committed are scanned. Binary files are skipped
structurally — git emits ``Binary files … differ`` and no ``+`` lines, so
there is nothing to decode and nothing to crash on.

Patterns come from :mod:`kinoforge.core.credential_patterns`, strict tier
only. The loose tier exists for redaction, where over-matching is
cosmetic; blocking a commit on a false match teaches ``--no-verify``,
after which this file protects nothing.

Exit codes: ``0`` clean, ``1`` findings, ``2`` usage or git error. A git
error is never reported as clean.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from kinoforge.core.credential_patterns import (
    ALLOW_PRAGMA,
    CREDENTIAL_PATTERNS,
    STRICT_PATTERNS,
    Finding,
    iter_findings,
    redact_string,
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

ScanResult = list[tuple[str, Finding]]


def _run_git(repo: Path, args: list[str]) -> str:
    """Run a git command in *repo* and return decoded stdout.

    Args:
        repo: Repository working directory.
        args: Arguments after ``git``.

    Returns:
        stdout decoded as UTF-8 with ``errors="replace"`` — diff output can
        legitimately carry undecodable bytes.

    Raises:
        RuntimeError: If git exits non-zero. The message carries git's
            stderr passed through :func:`redact_string`.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {redact_string(stderr)}")
    return proc.stdout.decode("utf-8", errors="replace")


def iter_staged_added_lines(repo: Path, paths: list[str]) -> list[tuple[str, int, str]]:
    """Collect every line the staged diff **adds**.

    Args:
        repo: Repository working directory.
        paths: Optional pathspec limiting the diff (pre-commit passes the
            staged filenames). Empty means the whole staged diff.

    Returns:
        ``(path, line_no, text)`` triples, where ``line_no`` is the line
        number in the post-commit file. Deleted lines are excluded —
        removing a credential is a fix, not a leak. Binary files
        contribute nothing, since git emits no ``+`` lines for them.
    """
    args = ["diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff"]
    if paths:
        args += ["--", *paths]
    diff = _run_git(repo, args)

    added: list[tuple[str, int, str]] = []
    current = ""
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current = "" if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("--- "):
            continue
        hunk = _HUNK_RE.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith("+") and current:
            added.append((current, line_no, raw[1:]))
            line_no += 1
    return added


def scan_staged(repo: Path, paths: list[str], *, strict: bool = True) -> ScanResult:
    """Scan staged added-lines for credential shapes.

    Args:
        repo: Repository working directory.
        paths: Pathspec limiting the diff; empty scans everything staged.
        strict: Use the strict tier (default). False sweeps with every
            pattern, including the deliberately loose redaction ones.

    Returns:
        ``(path, Finding)`` pairs in diff order. Empty means clean.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    results: ScanResult = []
    for path, line_no, text in iter_staged_added_lines(repo, paths):
        for finding in iter_findings(text, patterns=patterns):
            results.append((path, finding._replace(line_no=line_no)))
    return results


def scan_all_tracked(repo: Path, *, strict: bool = True) -> ScanResult:
    """Scan the working-tree content of every tracked file.

    Args:
        repo: Repository working directory.
        strict: See :func:`scan_staged`.

    Returns:
        ``(path, Finding)`` pairs. Files whose bytes are not valid UTF-8
        are skipped — they are binary, and a binary blob cannot carry a
        pasted terminal line.
    """
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS
    listing = _run_git(repo, ["ls-files", "-z"])
    results: ScanResult = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        target = repo / rel
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        results.extend((rel, finding) for finding in iter_findings(text, patterns=patterns))
    return results


def report(results: ScanResult, *, source: str) -> None:
    """Print a redacted report of *results* to stdout.

    Args:
        results: Findings to describe.
        source: Human label for what was scanned ("staged content",
            "tracked files").

    Notes:
        Every excerpt is already redacted by
        :func:`kinoforge.core.credential_patterns.iter_findings`; this
        function never has the raw value to leak.
    """
    noun = "match" if len(results) == 1 else "matches"
    print(f"tools/scan_secrets.py: {len(results)} credential-shaped {noun} in {source}\n")
    for path, finding in results:
        print(f"  {path}:{finding.line_no}  {finding.pattern_name}  {finding.redacted_excerpt}")
    print(
        "\nCommit blocked. If this is a placeholder, mark it "
        "(example / xxxx / <PLACEHOLDER> / ${VAR}) or add the pragma comment "
        f"`{ALLOW_PRAGMA}` on that line.\n"
        "If it is real: ROTATE the credential first, then remove it from the "
        "staged content. By the time you are reading this the value has "
        "already been in a shell buffer."
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``. Positional
            arguments are staged paths (pre-commit passes filenames).

    Returns:
        Process exit code — 0 clean, 1 findings, 2 usage or git error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="staged paths to limit the scan to")
    parser.add_argument("--repo", default=".", help="repository root (default: cwd)")
    parser.add_argument("--all-tracked", action="store_true", help="scan every tracked file")
    parser.add_argument("--stdin", action="store_true", help="scan text piped on stdin")
    parser.add_argument(
        "--tier",
        choices=("strict", "all"),
        default="strict",
        help="pattern tier; 'all' includes the loose redaction patterns",
    )
    args = parser.parse_args(argv)
    strict = args.tier == "strict"
    patterns = STRICT_PATTERNS if strict else CREDENTIAL_PATTERNS

    if args.stdin:
        results = [("<stdin>", f) for f in iter_findings(sys.stdin.read(), patterns=patterns)]
        source = "stdin"
    else:
        repo = Path(args.repo).resolve()
        try:
            if args.all_tracked:
                results = scan_all_tracked(repo, strict=strict)
                source = "tracked files"
            else:
                results = scan_staged(repo, args.paths, strict=strict)
                source = "staged content"
        except RuntimeError as exc:
            print(f"tools/scan_secrets.py: {exc}", file=sys.stderr)
            return 2

    if not results:
        return 0
    report(results, source=source)
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests, confirm they pass**

Run: `pixi run python -m pytest tests/tools/test_scan_secrets.py -v`
Expected: PASS (12 tests).

Two failures to expect and fix rather than work around:
- `test_git_failure_exits_2_not_0` — if git in a non-repo directory exits 0 (it can, walking up to a parent repo), pass `--repo` a path that does not exist so `subprocess` raises `FileNotFoundError`/`NotADirectoryError`; catch `OSError` alongside `RuntimeError` in `main`.
- `test_only_the_staged_hunk_is_scanned` — if it fails, `iter_staged_added_lines` is reading the file rather than the diff.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --files tools/scan_secrets.py tests/tools/test_scan_secrets.py
git add tools/scan_secrets.py tests/tools/test_scan_secrets.py
git commit -m "feat(credentials): scan staged content for credential shapes"
```

---

### Task 3: Tune to zero on the tracked tree, then wire the pre-commit hook

**Goal:** `scan_secrets.py --all-tracked` reports zero findings over all ~1377 tracked files, and the hook is registered so future commits are scanned.

**Files:**
- Modify: `src/kinoforge/core/credential_patterns.py` (pattern/marker tuning only)
- Modify: `.pre-commit-config.yaml` (append one hook)
- Modify: whichever tracked files carry an unmarked synthetic credential (add a marker or the pragma)

**Acceptance Criteria:**
- [ ] `pixi run python tools/scan_secrets.py --all-tracked` exits 0
- [ ] No file is excluded wholesale; suppression is per-line (marker or pragma) or by narrowing a pattern
- [ ] The pre-commit hook runs `tools/scan_secrets.py` with staged filenames and blocks on exit 1
- [ ] A deliberately planted credential in a staged file is caught by `pixi run pre-commit run scan-secrets --files <path>`, then removed

**Verify:** `pixi run python tools/scan_secrets.py --all-tracked; echo "exit=$?"` → `exit=0`

**Steps:**

- [ ] **Step 1: Run the scan and read every finding**

```bash
pixi run python tools/scan_secrets.py --all-tracked
```

Expect a handful of hits — the brief's scan found 23 with a broader pattern set, all synthetic. Triage each one into exactly one bucket:

| Bucket | Action |
| --- | --- |
| Synthetic value in a doc/fixture, no marker | add a marker word, or the `kinoforge: allow-secret` pragma if the exact bytes matter |
| Pattern matching a non-credential identifier | narrow the regex in `credential_patterns.py`, re-run |
| Pattern producing many unrelated hits | demote it to the loose tier (`strict=False`) and note why in a comment |
| An actual credential | **stop.** Rotate it, tell the operator, then clean |

- [ ] **Step 2: Re-run until clean**

```bash
pixi run python tools/scan_secrets.py --all-tracked; echo "exit=$?"
```
Expected: `exit=0`

Record every demotion or narrowing as a comment on the pattern — a future reader must be able to tell a tuning decision from an oversight.

- [ ] **Step 3: Confirm the scanner still catches a real shape after tuning**

```bash
printf 'export AWS_SECRET_ACCESS_KEY=%s\n' "$(python -c 'print("A"*40)')" \
  | pixi run python tools/scan_secrets.py --stdin; echo "exit=$?"
```
Expected: `exit=1` with one `credential_assignment` finding. If tuning made this pass, the tuning went too far.

- [ ] **Step 4: Add the pre-commit hook**

Append to the `hooks:` list in `.pre-commit-config.yaml`, after `check-toml`:

```yaml
      # Credential scan over STAGED content (not the working tree, so
      # `git add -p` partial staging is respected). .gitignore matches paths
      # and cannot stop a credential pasted into an already-tracked file —
      # this hook is that gap. Strict-tier patterns only; placeholder markers
      # and the `kinoforge: allow-secret` line pragma suppress synthetic
      # values. See src/kinoforge/core/credential_patterns.py.
      - id: scan-secrets
        name: scan-secrets (staged content)
        entry: pixi run python tools/scan_secrets.py
        language: system
        types: [text]
```

- [ ] **Step 5: Prove the hook fires, then prove it passes**

```bash
printf 'KEY=%s%s\n' "AKIA" "QWERTYUIOPASDFGH" > /tmp/leak-probe.md
cp /tmp/leak-probe.md leak-probe.md
git add leak-probe.md
pixi run pre-commit run scan-secrets --files leak-probe.md; echo "exit=$?"
```
Expected: `exit=1`, report names `leak-probe.md:1  aws_access_key`, and the value itself is absent from the output.

Then clean up and confirm green:

```bash
git rm -f --cached leak-probe.md && rm -f leak-probe.md /tmp/leak-probe.md
pixi run pre-commit run --all-files
```
Expected: every hook passes, including `scan-secrets`.

- [ ] **Step 6: Commit**

```bash
git add .pre-commit-config.yaml src/kinoforge/core/credential_patterns.py
git add -u  # any files that gained a marker or pragma
git commit -m "feat(credentials): block staged credentials at pre-commit"
```

---

### Task 4: In-repo Claude Code hooks — block and scrub

**Goal:** A fresh clone gets both transcript protections: a PreToolUse hook that denies credential-exfiltrating Bash, and the PostToolUse scrubber, registered in a committed `.claude/settings.json`.

**Files:**
- Create: `.claude/hooks/block_secret_reads.py`
- Create: `.claude/hooks/redact_secrets.py`
- Create: `.claude/settings.json`
- Test: `tests/hooks/test_block_secret_reads.py`

**Acceptance Criteria:**
- [ ] Both hooks run under bare `python3` with no `PYTHONPATH`, no pixi env, from any cwd
- [ ] `block_secret_reads.py` denies: bare `env`/`printenv`, `echo $X` / `echo ${X}` / `printenv X` for credential-ish `X`, reads of `.env` (but not `.env.example`), `gcloud auth print-access-token`, `gcloud auth print-identity-token`, `aws configure get …secret…`, `modal token …`
- [ ] It does **not** deny `[ -n "${HF_TOKEN:-}" ] && echo set`, `rg RUNPOD .env.example`, `env | wc -l`, or any non-Bash tool
- [ ] The deny reason names the safe alternative verbatim
- [ ] `redact_secrets.py` carries every pattern in the shared list, stays fail-open on exception, and keeps the PostToolUse contract
- [ ] `.claude/settings.json` registers PreToolUse (`Bash`) and PostToolUse (`*`) with `$CLAUDE_PROJECT_DIR`-relative paths

**Verify:** `pixi run python -m pytest tests/hooks/test_block_secret_reads.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/hooks/test_block_secret_reads.py`:

```python
"""Deny surface of the PreToolUse credential-exfiltration blocker.

The hook is driven the way Claude Code drives it: JSON on stdin, JSON on
stdout, via a real subprocess under bare `python3` with no PYTHONPATH —
that is the environment it actually runs in, and importing it in-process
would hide a stdlib-only violation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_secret_reads.py"


def _run(command: str, tool_name: str = "Bash") -> dict:
    """Invoke the hook with a Bash command and return its parsed stdout."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, "-E", "-S", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd="/",
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _is_deny(out: dict) -> bool:
    """True when the hook returned a PreToolUse deny decision."""
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv",
        "echo $AWS_SECRET_ACCESS_KEY",
        "echo ${HF_TOKEN}",
        'echo "$RUNPOD_API_KEY"',
        "printenv MODAL_TOKEN_SECRET",
        "cat .env",
        "cat /workspace/.env",
        "head -5 .env",
        "gcloud auth print-access-token",
        "gcloud auth print-identity-token",
        "aws configure get aws_secret_access_key",
        "modal token show",
        "echo hello && cat .env",
    ],
)
def test_denied(command: str) -> None:
    """Each shape would put a live credential into the transcript."""
    assert _is_deny(_run(command)), command


@pytest.mark.parametrize(
    "command",
    [
        '[ -n "${HF_TOKEN:-}" ] && echo set',
        'echo "HF_TOKEN len=${#HF_TOKEN}"',
        "rg RUNPOD_API_KEY .env.example",
        "cat .env.example",
        "env | wc -l",
        "echo $HOME",
        "echo $PIXI_PROJECT_ROOT",
        "aws sts get-caller-identity",
        "git status",
    ],
)
def test_not_denied(command: str) -> None:
    """Ordinary work must pass. A blocker that fires on these gets disabled."""
    assert not _is_deny(_run(command)), command


def test_non_bash_tool_is_ignored() -> None:
    """The hook only reasons about shell commands."""
    assert not _is_deny(_run("echo $AWS_SECRET_ACCESS_KEY", tool_name="Read"))


def test_deny_reason_teaches_the_safe_alternative() -> None:
    """A refusal that does not say what to do instead gets worked around."""
    reason = _run("echo $HF_TOKEN")["hookSpecificOutput"]["permissionDecisionReason"]
    assert '[ -n "${VAR:-}" ] && echo set' in reason


def test_malformed_stdin_does_not_block_the_session() -> None:
    """Fail-open on garbage: the blocker must never wedge the tool loop."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0
    assert not proc.stdout.strip()
```

- [ ] **Step 2: Run the tests, confirm they fail**

Run: `pixi run python -m pytest tests/hooks/test_block_secret_reads.py -v`
Expected: FAIL — `AssertionError` from the `assert proc.returncode == 0` line, because the hook file does not exist (`can't open file … block_secret_reads.py`).

- [ ] **Step 3: Write the blocker**

Create `.claude/hooks/block_secret_reads.py`:

```python
#!/usr/bin/env python3
"""Claude Code PreToolUse hook — deny Bash commands that would print a credential.

Blocking beats scrubbing where it applies. Post-hoc redaction cannot
catch a novel credential format, a value split across lines, or a shape
no regex anticipated — and by the time it runs, the bytes already exist.
Denying `echo $AWS_SECRET_ACCESS_KEY` means the value is never produced.

The deny surface is deliberately narrow and enumerated. A blocker that
fires on ordinary work gets disabled, and a disabled blocker protects
nothing. Its companion `redact_secrets.py` (PostToolUse) covers the other
direction: credentials that *arrive* in tool output nobody requested.

Contract (Claude Code PreToolUse):
- stdin:  JSON with `tool_name` and `tool_input`.
- stdout: empty + exit 0 to allow;
          {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "deny",
                                  "permissionDecisionReason": "..."}}
          to deny.
- Fail-open on any parse error — never wedge the tool loop.

Stdlib-only, and must stay that way: Claude Code runs it as bare
`python3` from an arbitrary cwd with no project on `sys.path`.
"""

from __future__ import annotations

import json
import re
import sys

SAFE_ALTERNATIVE = '[ -n "${VAR:-}" ] && echo set'

_CRED_WORD = r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"

DENY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "bare environment dump",
        re.compile(r"(?:^|[;&|]\s*)(?:env|printenv)\s*(?:$|[;&|])"),
    ),
    (
        "credential variable echo",
        re.compile(rf"\becho\s+[\"']?\$\{{?[A-Za-z0-9_]*{_CRED_WORD}[A-Za-z0-9_]*\}}?"),
    ),
    (
        "credential variable printenv",
        re.compile(rf"\bprintenv\s+[A-Za-z0-9_]*{_CRED_WORD}[A-Za-z0-9_]*\b"),
    ),
    (
        "dotenv read",
        re.compile(r"\b(?:cat|less|more|head|tail|bat|rg|grep|strings)\b[^;&|]*\.env(?!\.example)\b"),
    ),
    (
        "cloud token print",
        re.compile(r"\bgcloud\s+auth\s+print-(?:access|identity)-token\b"),
    ),
    (
        "aws secret read",
        re.compile(rf"\baws\s+configure\s+get\b[^;&|]*(?i:{_CRED_WORD})"),
    ),
    ("modal token print", re.compile(r"\bmodal\s+token\b")),
    ("runpod config print", re.compile(r"\brunpodctl\s+config\b")),
]


def deny_reason(command: str) -> str | None:
    """Return a refusal reason for *command*, or None to allow it.

    Args:
        command: The Bash command Claude is about to run.

    Returns:
        A reason string naming the matched rule and the safe alternative,
        or None when no rule matches. Only shapes that would put a live
        credential into the transcript match; shape probes that name no
        credential variable (``env | wc -l``) and reads of ``.env.example``
        deliberately do not.
    """
    for label, pattern in DENY_RULES:
        if pattern.search(command):
            return (
                f"Blocked by .claude/hooks/block_secret_reads.py ({label}). "
                "This would put a live credential into the conversation "
                "transcript, which is durable. Print length and shape "
                f"instead: {SAFE_ALTERNATIVE}, or `echo \"len=${{#VAR}}\"`. "
                "To confirm cloud identity use `aws sts get-caller-identity` "
                "or `gcloud config list account`, not the key itself."
            )
    return None


def main() -> int:
    """Read the PreToolUse payload from stdin, emit a deny decision if warranted.

    Returns:
        Always 0. Any parse failure passes through silently (fail-open):
        the blocker is defence in depth, and a crashing hook that wedges
        every Bash call would be worse than the risk it mitigates.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return 0

    try:
        if payload.get("tool_name") != "Bash":
            return 0
        command = payload.get("tool_input", {}).get("command", "")
        if not isinstance(command, str):
            return 0
        reason = deny_reason(command)
        if reason is None:
            return 0
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests, iterate on the deny surface**

Run: `pixi run python -m pytest tests/hooks/test_block_secret_reads.py -v`
Expected: PASS (24 parametrised cases + 3).

Known tuning points if a case fails:
- `env | wc -l` must NOT deny — the bare-dump regex requires `env` at a statement boundary followed by end-of-statement, so a pipe disqualifies it. If it denies, the trailing group is too permissive.
- `echo "$RUNPOD_API_KEY"` — the quote is consumed by the optional `["']?` before `$`.
- `aws configure get aws_secret_access_key` is lowercase; the rule uses an inline `(?i:…)` group for the credential word.

- [ ] **Step 5: Port the scrubber into the repo**

Copy `~/.claude/hooks/redact_secrets.py` to `.claude/hooks/redact_secrets.py`, then edit:
- Replace the 13-entry `CREDENTIAL_PATTERNS` with the full list from
  `src/kinoforge/core/credential_patterns.py` — same names, same sources, as plain
  `(name, compiled)` tuples (the hook cannot import `CredentialPattern`).
- Update the header comment: the source of truth is
  `src/kinoforge/core/credential_patterns.py`; drift is caught by
  `tests/test_redact_hook_parity.py`; this copy exists because the hook runs under bare
  `python3`.
- Keep `_load_env_values`, `scrub`, `main` and the fail-open posture unchanged.

Sanity-check it round-trips:

```bash
printf '{"tool_output":{"content":"tok=%s%s"}}' "AKIA" "QWERTYUIOPASDFGH" \
  | python3 .claude/hooks/redact_secrets.py
```
Expected: JSON containing `<REDACTED:aws_access_key>` and not the key.

- [ ] **Step 6: Register both hooks**

Create `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/block_secret_reads.py\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/redact_secrets.py\""
          }
        ]
      }
    ]
  }
}
```

Confirm it parses and that `.claude/` is not gitignored:

```bash
python3 -c 'import json;json.load(open(".claude/settings.json"))' && echo "settings ok"
git check-ignore -v .claude/settings.json || echo "not ignored — will be committed"
```

- [ ] **Step 7: Commit**

```bash
pixi run pre-commit run --files .claude/hooks/block_secret_reads.py \
  .claude/hooks/redact_secrets.py tests/hooks/test_block_secret_reads.py
git add .claude/hooks/block_secret_reads.py .claude/hooks/redact_secrets.py \
  .claude/settings.json tests/hooks/test_block_secret_reads.py
git commit -m "feat(hooks): block credential reads and scrub tool output in-repo"
```

---

### Task 5: Fail-closed parity + standing tracked-tree guard

**Goal:** The parity test fails when a hook is missing instead of skipping, and `test_source_audit.py` guards every tracked file instead of a hand-listed subset.

**Files:**
- Rewrite: `tests/test_redact_hook_parity.py`
- Rewrite: `tests/test_source_audit.py:31-122` (keep the reverse-test)

**Acceptance Criteria:**
- [ ] Repo hook parity has **no skip path** — a missing `.claude/hooks/redact_secrets.py` fails
- [ ] User-scope hook check fails when the hook is absent unless `KINOFORGE_SKIP_USER_REDACT_HOOK=1`
- [ ] The opt-out variable name appears in `CLAUDE.md` (asserted, so it cannot become folklore)
- [ ] `test_source_audit.py` scans every tracked file via `scan_all_tracked` and asserts zero findings
- [ ] The reverse-test (plant a credential, assert exactly one hit) survives
- [ ] `KINOFORGE_REQUIRE_REDACT_HOOK` no longer appears anywhere in the repo

**Verify:** `pixi run python -m pytest tests/test_redact_hook_parity.py tests/test_source_audit.py -v` → all pass; then `KINOFORGE_SKIP_USER_REDACT_HOOK=1 pixi run python -m pytest tests/test_redact_hook_parity.py -v` → still all pass

**Steps:**

- [ ] **Step 1: Rewrite the parity test**

Replace the whole of `tests/test_redact_hook_parity.py`:

```python
"""Drift guard — the Claude Code hooks MUST carry every shared pattern.

The hooks run under bare ``python3`` with no project on ``sys.path``, so
they cannot import :mod:`kinoforge.core.credential_patterns`. The
duplication is deliberate; this test is what keeps it honest.

**Fail-closed by design.** The previous version called ``pytest.skip``
when the hook file was missing, so a fresh clone got no transcript
protection, a green suite, and no signal. The repo hook is committed, so
its absence is now a hard failure. The user-scope hook keeps an opt-out —
``KINOFORGE_SKIP_USER_REDACT_HOOK=1`` — for environments where Claude
Code genuinely is not installed (CI runners, bare containers).
"""

from __future__ import annotations

import importlib.util
import os
import types
from pathlib import Path

import pytest

from kinoforge.core.credential_patterns import CREDENTIAL_PATTERNS

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_HOOK = REPO_ROOT / ".claude" / "hooks" / "redact_secrets.py"
USER_HOOK = Path.home() / ".claude" / "hooks" / "redact_secrets.py"
SKIP_USER_HOOK_ENV = "KINOFORGE_SKIP_USER_REDACT_HOOK"


def _load_module(path: Path, name: str) -> types.ModuleType:
    """Load a Python module from an arbitrary filesystem path.

    Args:
        path: Absolute path to the ``.py`` file.
        name: Name to register the module under; arbitrary.

    Returns:
        The fully-executed module object.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"no import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_superset(hook_path: Path, module_name: str) -> None:
    """Assert the hook at *hook_path* carries every shared pattern source."""
    hook = _load_module(hook_path, module_name)
    hook_sources = {pat.pattern for _name, pat in hook.CREDENTIAL_PATTERNS}
    shared_sources = {p.regex.pattern for p in CREDENTIAL_PATTERNS}
    missing = shared_sources - hook_sources
    assert not missing, (
        f"{hook_path} is missing {len(missing)} pattern(s) from "
        "src/kinoforge/core/credential_patterns.py:\n"
        + "\n".join(f"  {p!r}" for p in sorted(missing))
    )


def test_repo_hook_exists() -> None:
    """The in-repo hook is committed; its absence is a real regression.

    This is the inversion of the old fail-open guard: a fresh clone must
    not be able to lose transcript protection silently.
    """
    assert REPO_HOOK.is_file(), (
        f"{REPO_HOOK} is missing — a clone of this repo has no transcript "
        "scrubbing. Restore it; do not delete this test."
    )


def test_repo_hook_is_superset_of_shared_list() -> None:
    """Every shared pattern must reach the in-repo transcript hook."""
    _assert_superset(REPO_HOOK, "_repo_redact_hook")


def test_user_hook_present_or_explicitly_opted_out() -> None:
    """The user-scope hook is what actually runs on the operator's machine.

    Absent + no opt-out = failure. That is the point: a defence that
    silently is not installed provides confidence without coverage.
    """
    if os.getenv(SKIP_USER_HOOK_ENV) == "1":
        pytest.skip(f"{SKIP_USER_HOOK_ENV}=1 — user-scope hook check opted out")
    assert USER_HOOK.is_file(), (
        f"user-scope redact hook not installed at {USER_HOOK}. Copy "
        f"{REPO_HOOK} there, or set {SKIP_USER_HOOK_ENV}=1 if Claude Code "
        "is not installed in this environment."
    )
    _assert_superset(USER_HOOK, "_user_redact_hook")


def test_opt_out_variable_is_documented() -> None:
    """An undocumented opt-out becomes folklore, then becomes fail-open."""
    claude_md = (REPO_ROOT / "CLAUDE.md").read_text()
    assert SKIP_USER_HOOK_ENV in claude_md


def test_old_fail_open_variable_is_gone() -> None:
    """KINOFORGE_REQUIRE_REDACT_HOOK gated a skip; nothing may resurrect it."""
    for path in (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "AGENTS.md", Path(__file__)):
        assert "KINOFORGE_REQUIRE" + "_REDACT_HOOK" not in path.read_text()
```

- [ ] **Step 2: Run it, confirm the new failures are the intended ones**

Run: `pixi run python -m pytest tests/test_redact_hook_parity.py -v`
Expected at this point: `test_opt_out_variable_is_documented` FAILS (Task 6 writes that section). Everything else passes. Leave it red until Task 6 — do not weaken the assertion.

- [ ] **Step 3: Rewrite the source audit as a tracked-tree guard**

Replace `tests/test_source_audit.py` lines 31-122 (keep the module docstring's intent, update its text) with:

```python
from pathlib import Path

from kinoforge.core.credential_patterns import STRICT_PATTERNS, iter_findings
from tools.scan_secrets import scan_all_tracked

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]


def test_no_tracked_file_contains_a_credential() -> None:
    """Every tracked file must be free of strict-tier credential shapes.

    Widened from a hand-listed walk (docs/superpowers/**.md, tests/**.py,
    five root files) to `git ls-files` — the previous version could not
    see a credential pasted into examples/, tools/, src/, or a config.

    This is the standing guard: it fails even when the committer used
    `--no-verify`, which the pre-commit hook cannot.
    """
    findings = scan_all_tracked(_REPO_ROOT)
    detail = "\n".join(
        f"  {path}:{f.line_no}:{f.col} [{f.pattern_name}] {f.redacted_excerpt}"
        for path, f in findings
    )
    assert not findings, (
        f"Found {len(findings)} credential-shaped literal(s) in tracked files:\n"
        f"{detail}\n"
        "If real: ROTATE first, then remove. If synthetic: add a placeholder "
        "marker, or the `kinoforge: allow-secret` pragma on that line."
    )


def test_audit_fires_on_a_planted_credential(tmp_path: Path) -> None:
    """Reverse-test: without it, the guard above could no-op forever.

    Inherited from the original source audit — the single most valuable
    test in this file, because a guard that passes vacuously looks
    identical to a guard that works.
    """
    planted = "Some prose.\n\nA literal: " + "AKIA" + "QWERTYUIOPASDFGH" + "\n\nMore.\n"
    findings = list(iter_findings(planted, patterns=STRICT_PATTERNS))
    assert len(findings) == 1
    assert findings[0].pattern_name == "aws_access_key"
    assert findings[0].line_no == 3


def test_strict_tier_covers_the_canonical_scanner_shapes() -> None:
    """Guards against a refactor that empties or guts the strict tier."""
    names = {p.name for p in STRICT_PATTERNS}
    expected = {"sk_token", "aws_access_key", "pem_private_key", "hf_token"}
    assert not expected - names, f"strict tier missing canonical names: {expected - names}"
```

- [ ] **Step 4: Run both, then the full suite**

```bash
pixi run python -m pytest tests/test_source_audit.py -v
pixi run python -m pytest -q
```
Expected: `test_source_audit.py` all pass; the suite is green except `test_opt_out_variable_is_documented` (closed in Task 6).

- [ ] **Step 5: Verify the opt-out actually works**

```bash
KINOFORGE_SKIP_USER_REDACT_HOOK=1 pixi run python -m pytest \
  tests/test_redact_hook_parity.py::test_user_hook_present_or_explicitly_opted_out -v
```
Expected: SKIPPED (not failed, not passed-vacuously).

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --files tests/test_redact_hook_parity.py tests/test_source_audit.py
git add tests/test_redact_hook_parity.py tests/test_source_audit.py
git commit -m "test(credentials): fail closed on a missing hook, guard every tracked file"
```

---

### Task 6: Credential-safety documentation

**Goal:** `CLAUDE.md` carries the rules a hook cannot enforce and states the residual risk honestly; `AGENTS.md` points at it rather than duplicating it.

**Files:**
- Modify: `CLAUDE.md` (new `## Credential safety` section, after `## Environment & tools`)
- Modify: `AGENTS.md` (pointer)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT entry)

**Acceptance Criteria:**
- [ ] `CLAUDE.md` has a `## Credential safety` section covering: never echo a credential variable, never paste one into a config/fixture/commit message, prefer identity probes, rotate-first on exposure
- [ ] It names `KINOFORGE_SKIP_USER_REDACT_HOOK` and what the opt-out means
- [ ] It states the residual risk in plain words — a regex filter reduces exposure, it does not eliminate it
- [ ] `AGENTS.md` links to the section instead of restating it
- [ ] `PROGRESS.md` records the work with the spec + plan paths
- [ ] `test_opt_out_variable_is_documented` now passes

**Verify:** `pixi run python -m pytest tests/test_redact_hook_parity.py tests/test_source_audit.py -q && pixi run pre-commit run --all-files`

**Steps:**

- [ ] **Step 1: Add the `CLAUDE.md` section**

Insert after the `## Environment & tools` section:

```markdown
## Credential safety

Three layers protect credentials here, and none of them is the control that
actually works:

1. `.gitignore` — keeps `.env`, `.gcp/`, `.aws/` untracked. Matches **paths**;
   blind to a credential pasted into a tracked file.
2. `tools/scan_secrets.py` at pre-commit — scans **staged content** (added
   lines only, so `git add -p` is respected) with the strict tier of
   `src/kinoforge/core/credential_patterns.py`. Blocks the commit on a hit.
   `tests/test_source_audit.py` runs the same scan over every tracked file, so
   `--no-verify` does not get you past it.
3. `.claude/hooks/` — `block_secret_reads.py` denies credential-printing Bash
   before it runs; `redact_secrets.py` scrubs tool output before it reaches the
   transcript. Both are committed and registered in `.claude/settings.json`, so
   a fresh clone has them.

### Rules a hook cannot enforce

- **Never echo a credential variable.** Print length and shape only:
  `[ -n "${HF_TOKEN:-}" ] && echo "HF_TOKEN set len=${#HF_TOKEN}"`.
- **Never paste a credential into a config, fixture, test, commit message, or
  design doc** — including "just to check the shape". Use the synthetic
  conventions already in the repo (`kinoforge-prod-deadbeef` and friends), or
  mark the line with `kinoforge: allow-secret` if the exact bytes matter.
- **Prefer identity probes over key inspection:** `aws sts get-caller-identity`,
  `gcloud config list account` — not `aws configure get aws_secret_access_key`.
- **Claude never Writes/Edits a secret-bearing file.** Even an empty template
  puts the path in the file tracker, and later operator edits leak into the
  transcript. The operator creates it; Claude only references the path.
- **If a credential does reach a file, a terminal, or a transcript: rotate
  first, clean second.** The value has already been somewhere durable.

### Opt-out

`KINOFORGE_SKIP_USER_REDACT_HOOK=1` skips the check that the *user-scope*
Claude Code hook (`~/.claude/hooks/redact_secrets.py`) is installed. Set it only
where Claude Code genuinely is not installed — a CI runner, a bare container.
It does not disable the in-repo hooks, the pre-commit scan, or the tracked-tree
guard, none of which have an opt-out.

### Residual risk — stated plainly

A regex filter **reduces exposure; it does not eliminate it.** The scanner
catches named shapes and assignments to known credential variables. It does not
catch a new provider's format, a value split across lines, a base64-wrapped
blob, a credential paraphrased into prose, or one that simply does not look like
a credential. `CIVITAI_TOKEN`, `VAST_API_KEY`, and `B2_APPLICATION_KEY` have no
distinguishing prefix and are only caught next to their variable name — a
deliberate trade, because bare 32/64-hex patterns would match every digest in
`pixi.lock`.

The layers buy time and catch mistakes. The control that works is not putting
the credential there.
```

- [ ] **Step 2: Add the `AGENTS.md` pointer**

Add to `AGENTS.md`, in the section covering environment/tooling rules:

```markdown
## Credential safety

See `CLAUDE.md` → **Credential safety** for the full rules (never echo a
credential variable, never paste one into a tracked file, rotate before
cleaning) and for what the three enforcement layers do and do not catch.
Deliberately not duplicated here — two copies drift.
```

- [ ] **Step 3: Confirm the documentation test closes**

Run: `pixi run python -m pytest tests/test_redact_hook_parity.py -v`
Expected: all PASS, including `test_opt_out_variable_is_documented`.

- [ ] **Step 4: Update `PROGRESS.md`**

Add to the RESUME SNAPSHOT pointer list:

```markdown
- **Credential scanning at the commit boundary (COMPLETE 2026-08-18):**
  `docs/superpowers/specs/2026-08-18-credential-scanning-commit-boundary-design.md` +
  `docs/superpowers/plans/2026-08-18-credential-scanning-commit-boundary.md` (7 tasks 0-6;
  `.tasks.json` co-located). Closes verification findings F7 + F8. Four disagreeing credential
  lists collapsed into `src/kinoforge/core/credential_patterns.py` with a loose tier (redaction,
  over-matches by design) and a strict tier (blocking). `tools/scan_secrets.py` scans STAGED
  added-lines at pre-commit; `tests/test_source_audit.py` runs the same scan over every tracked
  file, so `--no-verify` does not get past it. `.claude/hooks/{block_secret_reads,redact_secrets}.py`
  + `.claude/settings.json` are committed, so a fresh clone has both a PreToolUse deny and a
  PostToolUse scrub. The parity test lost its skip path — a missing hook now FAILS
  (`KINOFORGE_SKIP_USER_REDACT_HOOK=1` is the documented opt-out for the user-scope hook only).
  No live spend.
```

- [ ] **Step 5: Full verification, then commit**

```bash
pixi run python -m pytest -q
pixi run python tools/scan_secrets.py --all-tracked; echo "exit=$?"
pixi run pre-commit run --all-files
git add CLAUDE.md AGENTS.md PROGRESS.md
git commit -m "docs: state the credential-safety rules and the residual risk"
```
Expected: suite green, `exit=0`, every hook passes.

---

## Self-review

**Spec coverage:**

| Spec unit | Task |
| --- | --- |
| Unit 1 — shared list, two tiers | 0 (+ consumers rewired in 1, source audit in 5) |
| Unit 2 — staged-content scanner | 2 |
| Unit 3 — false-positive suppression | 0 (mechanism) + 3 (empirical tuning) |
| Unit 4 — `.claude/` hooks + settings, block **and** scrub | 4 |
| Unit 5 — fail-closed parity + standing guard | 5 |
| Unit 6 — `CLAUDE.md` credential safety | 6 |
| Brief test list — all 10 rows | 0 (5 rows), 2 (5 rows), 5 (3 rows) |

**Type consistency:** `CredentialPattern(name, regex, strict)` and `Finding(pattern_name, line_no, col, redacted_excerpt)` are used with those exact field names in Tasks 0, 2, 5. `scan_staged`/`scan_all_tracked` both return `list[tuple[str, Finding]]`, and every caller (report, tests, source audit) unpacks `(path, finding)`.

**Known red-until-later:** `test_opt_out_variable_is_documented` (Task 5) fails until Task 6 writes the `CLAUDE.md` section. Called out in both tasks; do not weaken it to get green early.
