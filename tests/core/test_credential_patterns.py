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
    # Split at "PRIVATE KEY" so this file never carries a full BEGIN..END span —
    # the tracked-tree guard would flag the test that tests the guard.
    pem = (
        "-----BEGIN RSA PRIVATE " + "KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxSECRETBODYxxxx\n"
        "-----END RSA PRIVATE " + "KEY-----"
    )
    out = cp.redact_string(pem)
    assert "SECRETBODY" not in out
    assert "<REDACTED:pem_private_key>" in out


def test_multiline_pem_is_detected_by_iter_findings() -> None:
    """The blocking path must fire on a real pasted key, not just redact_string.

    A real PEM has BEGIN and END on different lines, so a per-line scan
    (the old implementation) never sees the whole span and STRICT_PATTERNS
    — what the pre-commit scanner and tracked-tree guard actually use —
    silently let it through. Split at "PRIVATE KEY" (as above) so this
    fixture itself does not carry a full BEGIN..END span.
    """
    pem = (
        "-----BEGIN RSA PRIVATE " + "KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxSECRETLINEONExxxx\n"
        "MIIEowIBAAKCAQEAxxxxSECRETLINETWOxxxx\n"
        "-----END RSA PRIVATE " + "KEY-----"
    )
    text = f"prefix line\n{pem}\nsuffix line\n"

    (finding,) = list(cp.iter_findings(text))

    assert finding.pattern_name == "pem_private_key"
    assert finding.line_no == 2  # the BEGIN line
    assert "SECRETLINEONE" not in finding.redacted_excerpt
    assert "SECRETLINETWO" not in finding.redacted_excerpt
    assert "MIIEow" not in finding.redacted_excerpt
    assert "<REDACTED:pem_private_key>" in finding.redacted_excerpt


def test_bearer_declared_first_so_header_collapses_whole() -> None:
    """Ordering guarantee inherited from tools/_redact.py."""
    out = cp.redact_string(f"Authorization: Bearer {RPA_KEY}")
    assert out.endswith("<REDACTED:bearer_auth>")
    assert "rpa_" not in out


def test_credential_assignment_catches_a_pasted_export_line() -> None:
    """The actual leak vector: a terminal line pasted into a tracked file."""
    secret_value = "wJalrXUtnFEMIK7MDENGbPxRfiCYzcvKQ7" + "MDENG"
    line = f"export AWS_SECRET_ACCESS_KEY={secret_value}"  # kinoforge: allow-secret
    names = {f.pattern_name for f in cp.iter_findings(line)}
    assert "credential_assignment" in names


def test_empty_assignment_in_env_example_does_not_match() -> None:
    """.env.example's `VAR=` lines must not block every commit."""
    assert not list(cp.iter_findings("AWS_SECRET_ACCESS_KEY="))


def test_placeholder_marker_suppresses_a_real_shaped_match() -> None:
    """STRONG markers suppress line-wide. Keeps .env.example + docs clean."""
    assert list(cp.iter_findings(AWS_KEY))  # baseline: it does match
    assert not list(cp.iter_findings("AKIA" + "XXXXXXXXXXXXXXXX"))  # xxxx: strong
    assert not list(cp.iter_findings(f"key = ${{AWS_KEY}}  {AWS_KEY}"))  # ${: strong


def test_weak_marker_in_trailing_comment_does_not_suppress_a_real_key() -> None:
    """WEAK markers only suppress when the marker text is inside the match itself.

    A real key followed by a chatty comment ("# sample bucket for
    us-west-2", "# example: rotate before prod") must still block —
    ordinary English words near a real credential are not proof it is
    synthetic. This is the fix for the bug where any comment containing
    "example" silently defeated the strict tier.
    """
    assert list(cp.iter_findings(f"{AWS_KEY}  # sample bucket for us-west-2"))
    assert list(cp.iter_findings(f"{AWS_KEY}  # example: rotate before prod"))


def test_weak_marker_inside_the_match_suppresses() -> None:
    """The canonical AWS docs fixture: the marker word is part of the key body."""
    canonical = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS's own documented placeholder
    assert not list(cp.iter_findings(canonical))


def test_strong_marker_anywhere_on_line_still_suppresses() -> None:
    """STRONG markers stay line-scoped even in a trailing comment."""
    assert not list(cp.iter_findings(f"{AWS_KEY}  # placeholder, do not use"))


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
    (finding,) = [
        f for f in cp.iter_findings(text) if f.pattern_name == "aws_access_key"
    ]
    assert finding.line_no == 2
    assert AWS_KEY not in finding.redacted_excerpt
    assert "<REDACTED:aws_access_key>" in finding.redacted_excerpt


def test_loose_tier_still_scrubs_short_project_tokens() -> None:
    """Redaction keeps today's aggressive behaviour; only blocking is strict."""
    short = "hf_" + "abcdefgh"
    assert "<REDACTED:" in cp.redact_string(short)
    assert not list(cp.iter_findings(short))  # strict tier ignores it


def test_sk_token_ignores_ordinary_kebab_case_identifiers() -> None:
    """sk-... must not false-positive on identifiers that merely contain 'sk-'.

    Built by concatenation: the source-audit guard's own (still-old,
    pre-Task-5) sk_token regex is exactly the weak one this finding
    replaces, so a bare literal here would trip it.
    """
    identifier = "generate-sk-thumbnail-" + "preview-cache-key"
    assert not list(cp.iter_findings(identifier))


def test_sk_token_matches_real_anthropic_and_openai_shapes() -> None:
    """Real keys keep hyphens in the prefix; the tail is a long alnum run."""
    anthropic_key = "sk-ant-api03-" + "A" * 40
    openai_key = "sk-proj-" + "b" * 40
    names_a = {f.pattern_name for f in cp.iter_findings(anthropic_key)}
    names_b = {f.pattern_name for f in cp.iter_findings(openai_key)}
    assert "sk_token" in names_a
    assert "sk_token" in names_b


def test_every_pattern_has_a_unique_snake_case_name() -> None:
    """Names appear in <REDACTED:{name}> markers and in parity assertions."""
    names = [p.name for p in cp.CREDENTIAL_PATTERNS]
    assert len(names) == len(set(names))
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", n) for n in names)


def test_jwt_pattern_finds_a_real_three_segment_token() -> None:
    """A genuine JWT — three dot-separated base64url segments — must still block."""
    header = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    payload = "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
    signature = "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    token = f"{header}.{payload}.{signature}"
    names = {f.pattern_name for f in cp.iter_findings(token)}
    assert "jwt" in names


def test_jwt_pattern_ignores_base64_json_bodies() -> None:
    """Regression guard: bare `eyJ...` with no dots is base64 for `{"`, not a JWT.

    This exact literal comes from a committed GCS fixture
    (tests/stores/fixtures/gcs/test_gcs_hot_path.json) and decodes to
    `{"kind":"storage#object...` — an ordinary API response body, not a
    credential. The old pattern (`\\beyJ[A-Za-z0-9._=-]{20,}\\b`, no dot
    requirement) matched it and four other committed fixtures, which is
    what `tests/providers/test_fixtures_audit.py` caught. A JWT always has
    exactly two dots separating header/payload/signature; requiring them
    is what tells a real token apart from arbitrary base64 JSON.
    """
    base64_json_body = "eyJraW5kIjoic3RvcmFnZSNvYmplY3Rz"
    assert not list(cp.iter_findings(base64_json_body))


def test_redact_string_is_idempotent() -> None:
    """redact_string(redact_string(s)) must equal redact_string(s).

    Regression guard for two related bugs: (1) ``credential_assignment``
    re-matching its own ``<REDACTED:...>`` output because the marker text
    is 8+ non-whitespace characters, collapsing a specific marker like
    ``<REDACTED:hf_token>`` into the generic ``<REDACTED:credential_assignment>``
    on a second pass; (2) a loose/strict pair racing to claim the same
    marker name. A single pass already redacts every credential shape, so
    running redact_string again on its own output must be a no-op.
    """
    text = f"HF_TOKEN={HF_KEY} key={AWS_KEY} Authorization: Bearer {RPA_KEY}"  # kinoforge: allow-secret
    once = cp.redact_string(text)
    twice = cp.redact_string(once)
    assert twice == once
