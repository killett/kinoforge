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

Naming rule where a loose and a strict pattern cover the same secret
family (currently ``rpa_`` and ``hf_``): the **plain name belongs to
the loose pattern** — ``rpa_token``, ``hf_token`` — because that is the
name the redactors have always emitted in their ``<REDACTED:{name}>``
markers, and tool-stderr output carrying that marker is a human-visible
contract external callers grep for. The **strict variant takes the
``_strict`` suffix** — ``rpa_token_strict``, ``hf_token_strict``. Do
not swap this: a strict-tier addition must never silently rename what
the loose tier has always been called.

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

# Tuning notes (2026-08-18, Task 3, revised after review — see
# docs/superpowers/sdd/2026-08-18-credential-scanning-commit-boundary/
# task-3-report.md):
#
# 1. `[ \t]*` (not `\s*`) around the `=`/`:` — `\s*` matches a newline, so
#    an EMPTY assignment (`AWS_ACCESS_KEY_ID=` immediately followed by
#    another `VAR=` line, exactly the shape of `.env.example`) let the
#    match skip the blank value and swallow the *next line's variable
#    name* as if it were this line's secret. Restricting to same-line
#    whitespace makes an empty assignment correctly match nothing.
# 2. `(?!<REDACTED)(?!<[a-z][a-z_-]*(?:\s|>))` replaces the original
#    `(?!<REDACTED)`. A first attempt used a blanket `(?!<)`, but that
#    excludes ANY bracketed value — including a real credential someone
#    wrapped in `<...>` thinking the brackets marked it as fake, which
#    would then sail past the scanner undetected. The real false
#    positives are angle-bracket PROSE (`HF_TOKEN=<huggingface token>`,
#    `CIVITAI_TOKEN=<value>`, `RUNPOD_TERMINATE_KEY=<scoped>`), which is
#    characterised by a lowercase word right after `<` that either ends
#    the bracket immediately (`<value>`) or is followed by a space
#    (`<huggingface token>`) — a credential-shaped value inside brackets
#    is neither (mixed case / digits, no internal space). `<REDACTED`
#    is kept as an explicit exclusion because it doesn't fit that
#    lowercase-prose shape (capital R) but must still not re-match its
#    own redaction output — see test_redact_string_is_idempotent.
#    Tests: test_bracket_prose_placeholder_is_not_a_finding,
#    test_bracketed_credential_shaped_value_is_still_caught.
# 3. `(?!\$[A-Z_])` — a value of the form `$RUNPOD_API_KEY` is a shell
#    variable *reference*, not a literal secret; it is the standard way
#    this repo's docs show "pass your own key here" (e.g.
#    `RUNPOD_API_KEY=$RUNPOD_API_KEY pixi run ...`). No real credential
#    is spelled as a bare `$UPPER_CASE_NAME` token. Tests:
#    test_dollar_var_reference_is_not_a_finding,
#    test_literal_value_after_dollar_var_style_name_is_still_a_finding.
_ASSIGNMENT_RE = re.compile(
    r"\b(?:" + "|".join(CREDENTIAL_ENV_VARS) + r")[ \t]*[=:][ \t]*[\"']?"
    r"(?!<REDACTED)(?!<[a-z][a-z_-]*(?:\s|>))(?!\$[A-Z_])[^\s\"'#]{8,}"
)

CREDENTIAL_PATTERNS: list[CredentialPattern] = [
    # ---- loose tier: redaction only, over-matches by design ----------------
    # Plain names (rpa_token, hf_token) belong here, not to the strict
    # variants below — see the naming-rule paragraph in the module
    # docstring. redact_string() runs every pattern in this declaration
    # order, so these fire and claim the marker name before their
    # _strict counterparts get a chance to.
    CredentialPattern(
        "bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"), False
    ),
    CredentialPattern("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b"), False),
    CredentialPattern("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b"), False),
    # ---- strict tier: may block a commit -----------------------------------
    CredentialPattern(
        "rpa_token_strict", re.compile(r"\brpa_[A-Za-z0-9]{24,}\b"), True
    ),
    CredentialPattern("hf_token_strict", re.compile(r"\bhf_[A-Za-z0-9]{32,}\b"), True),
    CredentialPattern("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b"), True),
    # 2026-08-18 whole-branch review, Finding 1: the plain `\b`-anchored
    # narrow form (`\bsk-[A-Za-z0-9_\-]*[A-Za-z0-9]{16,}\b`) was a
    # NARROWING of the two lists it replaced, not a superset — it missed
    # separator-dense real keys (`sk-ant-api03-Ab3_Ab3_...`,
    # `sk-proj-x_x_x_...yyyy`) that need the old pre-Task-5 alternative
    # (`\bsk-[A-Za-z0-9_\-]{20,}\b`, no contiguous-run requirement) to
    # match. A first attempt at unioning the two alternatives verbatim
    # re-broke `test_sk_token_ignores_ordinary_kebab_case_identifiers`
    # (`generate-sk-thumbnail-preview-cache-key` matched again), because
    # `\b` alone is satisfied by ANY word/non-word transition — including
    # the `-` right before the `sk` inside that hyphenated identifier,
    # which is not a token start at all.
    #
    # Fix: replace the leading `\b` with a negative lookbehind for
    # `[A-Za-z0-9_\-]` — i.e. "not immediately preceded by an
    # identifier-or-hyphen character". A real credential's `sk-` is
    # always at a genuine token start (start of string, after `=`/`:`/
    # whitespace/quote, or after prose punctuation); a kebab-case
    # fragment's `sk-` is always preceded by a hyphen from the identifier
    # itself. This is the one condition that tells them apart:
    # `generate-sk-...` has `-` immediately before `sk`, so the
    # lookbehind excludes it, while `OPENAI_API_KEY=sk-...`, a quoted
    # `"sk-..."`, and a bare `sk-...` all pass. With the anchor fixed,
    # the union restores the `{20,}` alternative that both pre-existing
    # lists (`tests/test_source_audit.py`, `tests/providers/
    # conftest_runpod.py`) carried before this branch — so this is no
    # longer a narrowing of what it replaced, it is the intended
    # superset.
    # Tests: test_sk_token_ignores_ordinary_kebab_case_identifiers (must
    # keep passing unmodified), test_sk_token_matches_separator_dense_
    # real_key_shapes, test_sk_token_ignores_kebab_case_in_a_path_or_
    # branch_name.
    CredentialPattern(
        "sk_token",
        re.compile(
            r"(?<![A-Za-z0-9_\-])sk-(?:[A-Za-z0-9_\-]{20,}|[A-Za-z0-9_\-]*[A-Za-z0-9]{16,})\b"
        ),
        True,
    ),
    CredentialPattern(
        "aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), True
    ),
    CredentialPattern(
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
        True,
    ),
    CredentialPattern("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), True),
    CredentialPattern(
        "github_app", re.compile(r"\b(?:gho|ghu|ghs)_[A-Za-z0-9]{36,}\b"), True
    ),
    CredentialPattern("replicate_token", re.compile(r"\br8_[A-Za-z0-9]{30,}\b"), True),
    CredentialPattern("runway_key", re.compile(r"\bkey[-_][A-Za-z0-9]{30,}\b"), True),
    CredentialPattern(
        "slack_token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b"), True
    ),
    # Requires all three dot-separated base64url segments (header.payload.
    # signature). A bare `eyJ...` prefix is just base64 for `{"` and matches
    # ANY base64-encoded JSON body — this repo's GCS fixtures are full of
    # them (e.g. `eyJraW5kIjoic3RvcmFnZSNvYmplY3Rz` decodes to
    # `{"kind":"storage#object...`, not a token). The dots are what make a
    # JWT structurally distinct from arbitrary base64 JSON.
    CredentialPattern(
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
        True,
    ),
    # Narrowed from `\bluma-[A-Za-z0-9-]{20,}\b` (Task 3, 2026-08-18): the
    # real credential shape is `luma-api-...` (see the docstring of
    # `LumaAgentsImageEngine` in src/kinoforge/image_engines/luma_agents/
    # __init__.py, corroborated by .env.example and Luma's own docs). The
    # old bare `luma-` prefix collided with ordinary kebab-case doc
    # filenames and markdown anchors — `luma-image-keyframes-design.md`,
    # `#luma-uni-1-image-keyframe-via-agents-api` — which are 20+ chars of
    # `[A-Za-z0-9-]` right after `luma-` and are not credentials at all.
    # Tradeoff: unlike rpa_/hf_, luma_key has no loose counterpart, and
    # redact_string() runs every pattern regardless of tier — so this
    # narrow also shrinks what gets REDACTED, not just what blocks a
    # commit. If Luma ever ships a key under a different prefix, add a
    # second `luma_key`-named strict pattern for the new shape (keeping
    # this one) rather than loosening this regex back toward the bare
    # `luma-` prefix that caused the original false positives.
    # Tests: test_luma_api_prefix_is_required_to_match.
    CredentialPattern("luma_key", re.compile(r"\bluma-api-[A-Za-z0-9_-]{8,}\b"), True),
    CredentialPattern(
        "modal_token", re.compile(r"\b(?:ak|as)-[A-Za-z0-9]{20,}\b"), True
    ),
    CredentialPattern(
        "lambda_key", re.compile(r"\bsecret_[A-Za-z0-9]+_[0-9a-f]{32,}\b"), True
    ),
    CredentialPattern(
        "gcp_access_token", re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}\b"), True
    ),
    CredentialPattern("credential_assignment", _ASSIGNMENT_RE, True),
]

STRICT_PATTERNS: list[CredentialPattern] = [p for p in CREDENTIAL_PATTERNS if p.strict]

#: Markers that are unambiguously synthetic wherever they appear on the
#: line — a same-line comment ("# placeholder, do not use") is enough to
#: suppress, because no real credential shape needs these words nearby.
STRONG_PLACEHOLDER_MARKERS: frozenset[str] = frozenset(
    {
        "xxxx",
        "placeholder",
        "${",
        "your-",
        "your_",
        "changeme",
        "redacted",
        "deadbeef",
        "notreal",
    }
)

#: Markers that are ordinary English and appear in real chatty comments
#: ("# sample bucket for us-west-2", "# example: rotate before prod"), so
#: they only suppress when the marker text is inside the *matched*
#: credential-shaped text itself — e.g. the canonical AWS docs fixture
#: ``AKIA...EXAMPLE``. A same-line comment using one of these words does
#: NOT suppress a real key.
WEAK_PLACEHOLDER_MARKERS: frozenset[str] = frozenset(
    {
        "example",
        "sample",
        "fake",
        "dummy",
        "test",
    }
)

#: Union of both tiers, exported for callers/tests that want the full set.
PLACEHOLDER_MARKERS: frozenset[str] = (
    STRONG_PLACEHOLDER_MARKERS | WEAK_PLACEHOLDER_MARKERS
)

#: Line-scoped escape hatch for a synthetic value carrying no marker word.
ALLOW_PRAGMA = "kinoforge: allow-secret"


def looks_like_placeholder(match_text: str, line: str) -> bool:
    """Report whether a credential-shaped match is synthetic.

    Args:
        match_text: The matched substring.
        line: The full line the match came from.

    Returns:
        True when: the line carries the :data:`ALLOW_PRAGMA` comment; the
        line carries a :data:`STRONG_PLACEHOLDER_MARKERS` word anywhere
        (line-scoped — even a trailing comment suppresses); or the match
        itself carries a :data:`WEAK_PLACEHOLDER_MARKERS` word
        (match-scoped only — an ordinary word like "example" in a
        trailing comment must NOT suppress a real credential). All
        checks are case-insensitive.
    """
    if ALLOW_PRAGMA in line.lower():
        return True
    if any(marker in line.lower() for marker in STRONG_PLACEHOLDER_MARKERS):
        return True
    return any(marker in match_text.lower() for marker in WEAK_PLACEHOLDER_MARKERS)


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

    Each pattern is matched against the *whole* text, not line by line —
    ``pem_private_key`` needs its BEGIN and END markers to be in the same
    match, and a real pasted key has them on different lines. ``line_no``
    and ``col`` are then derived from the match's character offset, the
    same technique ``tests/test_source_audit.py`` uses.

    Args:
        text: Text to scan.
        patterns: Patterns to apply. Defaults to :data:`STRICT_PATTERNS` —
            the tier that is safe to block on. Pass
            :data:`CREDENTIAL_PATTERNS` for an aggressive sweep.
        skip_placeholders: When True (default), matches suppressed by
            :func:`looks_like_placeholder` are not yielded. The
            placeholder check runs against the line the match *starts*
            on.

    Yields:
        Findings in line order, then pattern-declaration order within a
        line. For a match that stays on one line, ``redacted_excerpt``
        is that line passed through :func:`redact_string`, so it can
        never carry the matched value. For a match that spans multiple
        lines (``pem_private_key``), whole-line redaction is not safe —
        an interior body line matches no pattern on its own and would
        pass its base64 content straight through — so the excerpt is
        instead the fixed string
        ``<REDACTED:{pattern_name}> (spans lines {start}-{end})``, which
        carries no source text at all.
    """
    selected = list(STRICT_PATTERNS if patterns is None else patterns)
    findings: list[Finding] = []
    for pattern in selected:
        for match in pattern.regex.finditer(text):
            start, end = match.start(), match.end()
            line_start = text.rfind("\n", 0, start) + 1
            next_newline = text.find("\n", start)
            line_end = next_newline if next_newline != -1 else len(text)
            line_text = text[line_start:line_end]
            start_line_no = text.count("\n", 0, start) + 1
            col = start - line_start + 1

            if skip_placeholders and looks_like_placeholder(match.group(0), line_text):
                continue

            if "\n" in match.group(0):
                end_line_no = text.count("\n", 0, end) + 1
                redacted_excerpt = f"<REDACTED:{pattern.name}> (spans lines {start_line_no}-{end_line_no})"
            else:
                redacted_excerpt = redact_string(line_text).strip()[:200]

            findings.append(
                Finding(
                    pattern_name=pattern.name,
                    line_no=start_line_no,
                    col=col,
                    redacted_excerpt=redacted_excerpt,
                )
            )
    findings.sort(key=lambda finding: finding.line_no)
    yield from findings
