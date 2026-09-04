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

# Google API key: `AIza` + a url-safe body. Split at the prefix so this file
# carries no matchable literal of its own.
GOOGLE_KEY = "AIza" + "SyD9mQpVzXnRtYuIoPaSdFgHjKlZxCvBnM"

# GCP service-account JSON: `private_key_id` is 40 lowercase hex chars.
SA_KEY_ID = "a3f9c1d2e4b6a8c0" + "d2e4f6a8b0c2d4e6f8a0b2c4"


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


def test_dollar_var_reference_is_not_a_finding() -> None:
    """Tuning boundary: `VAR=$OTHER_VAR` is a shell variable *reference*.

    Docs show `RUNPOD_API_KEY=$RUNPOD_API_KEY pixi run ...` as the standard
    "pass your own key here" convention (docs/engines.md and several
    plans). No real credential is spelled as a bare `$UPPER_CASE_NAME`
    token, so this must not block a commit.
    """
    assert not list(cp.iter_findings("RUNPOD_API_KEY=$RUNPOD_API_KEY"))


def test_literal_value_after_dollar_var_style_name_is_still_a_finding() -> None:
    """Contrast case for the `$VAR` exclusion above.

    The exclusion must be narrow to the `$UPPER_CASE_NAME` shape — a
    literal (non-reference) value of comparable length assigned to the
    same variable name must still be caught.
    """
    literal_value = "B" * 40
    finding = f"RUNPOD_API_KEY={literal_value}"  # kinoforge: allow-secret
    assert list(cp.iter_findings(finding))


def test_bracket_prose_placeholder_is_not_a_finding() -> None:
    """Tuning boundary: `<lowercase word(s)>` in angle brackets is doc
    prose (`HF_TOKEN=<huggingface token>`, `CIVITAI_TOKEN=<value>`,
    `RUNPOD_TERMINATE_KEY=<scoped>`), not a secret, and must not block.
    """
    assert not list(cp.iter_findings("HF_TOKEN=<huggingface token>"))
    assert not list(cp.iter_findings("CIVITAI_TOKEN=<value>"))
    assert not list(cp.iter_findings("RUNPOD_TERMINATE_KEY=<scoped>"))


def test_bracketed_credential_shaped_value_is_still_caught() -> None:
    """Regression guard for the bracket-prose exclusion above.

    A first attempt at that exclusion used a blanket `(?!<)`, which also
    hid a real credential someone wrapped in `<...>` (mistakenly thinking
    the brackets marked it as fake) — mixed-case/digit content inside
    brackets, with no internal space, is not prose and must still block.
    """
    bracketed_credential = "<Zm9vYmFyMTIzNDU2Nzg5MDEyMzQ1Njc4OTA+ab>"
    finding = f"AWS_SECRET_ACCESS_KEY={bracketed_credential}"  # kinoforge: allow-secret
    assert list(cp.iter_findings(finding))


def test_luma_api_prefix_is_required_to_match() -> None:
    """Tuning boundary: the real Luma key shape is `luma-api-...` (see the
    docstring of `LumaAgentsImageEngine`). The formerly-bare `luma-`
    prefix collided with doc filenames/anchors like
    `luma-image-keyframes-design.md`, which are not credentials.
    """
    tail = "N3q7Zk2Ht8Vw1Ry4Xs6L"  # 20 mixed-case/digit chars, no marker words
    assert list(cp.iter_findings("luma-api-" + tail))
    assert not list(cp.iter_findings("luma-" + tail))


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


def test_sk_token_matches_separator_dense_real_key_shapes() -> None:
    """Union regression guard (2026-08-18 whole-branch review, Finding 1).

    Real Anthropic/OpenAI keys often break the alnum run into <16-char
    chunks via underscores (`sk-ant-api03-Ab3_Ab3_...`,
    `sk-proj-x_x_x_...yyyy`), which the narrow contiguous-run alternative
    alone misses — only the restored `{20,}` alternative (any run of
    `[A-Za-z0-9_\\-]`, no contiguous-alnum requirement) catches these. Both
    literals built by runtime concatenation so this file itself carries
    no matchable credential shape.
    """

    def _dense(prefix: str, n: int) -> str:
        return prefix + "Ab3_" * n

    anthropic_dense = _dense("sk-ant-api03-", 10)
    openai_dense = _dense("sk-proj-", 10)
    names_a = {f.pattern_name for f in cp.iter_findings(anthropic_dense)}
    names_b = {f.pattern_name for f in cp.iter_findings(openai_dense)}
    assert "sk_token" in names_a
    assert "sk_token" in names_b


def test_sk_token_ignores_kebab_case_in_a_path_or_branch_name() -> None:
    """Pins the token-start anchor itself, not just the one identifier string.

    The fix for the union regression (see
    test_sk_token_matches_separator_dense_real_key_shapes) replaces the
    leading `\\b` with a negative lookbehind for
    `[A-Za-z0-9_\\-]` — a real credential's `sk-` is always at a genuine
    token start, while a kebab-case fragment's `sk-` is always preceded
    by a hyphen from the identifier itself. Both variants below embed the
    same kebab-case identifier from
    test_sk_token_ignores_ordinary_kebab_case_identifiers inside realistic
    surrounding contexts (a doc path, a branch name) to confirm the
    anchor holds regardless of what comes before the leading hyphen.
    """
    tail = "generate-sk-thumbnail-" + "preview-cache-key"
    as_path = "docs/" + tail + ".md"
    as_branch = "feature/" + tail
    assert not list(cp.iter_findings(as_path))
    assert not list(cp.iter_findings(as_branch))


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


def test_google_api_key_is_a_finding() -> None:
    """G1: `AIza...` is a well-known fixed shape gcloud and SkyPilot can surface.

    Fails if the pattern is absent — a browser-key paste from a GCP console
    or a SkyPilot GCP error body commits clean today.
    """
    names = {f.pattern_name for f in cp.iter_findings(GOOGLE_KEY)}
    assert "google_api_key" in names


def test_google_api_key_length_band_rejects_short_and_long_bodies() -> None:
    """The `AIza` prefix is the discriminator; the band rejects non-key shapes.

    Fails if the body quantifier is left open-ended (`{30,}` with no upper
    bound, or no trailing anchor): a 64-char base64 blob that happens to
    start with `AIza` would then block every commit that touches it. Also
    fails if the lower bound is dropped, which would match the bare word
    `AIzaSy` in prose.
    """
    assert not list(cp.iter_findings("AIza" + "Sy0123456789abcdef"))  # 18-char body
    assert not list(cp.iter_findings("AIza" + "Q1" * 32))  # 64-char body


def test_google_api_key_ignores_the_prefix_inside_a_longer_token() -> None:
    """A `AIza` run *inside* another token is not a key.

    Fails if the leading lookbehind is dropped: `pixi.lock` is full of long
    base64 digests, and one containing `AIza` mid-string would then be
    reported forever with no way to fix it but an allowlist.
    """
    embedded = "sha256-Zm9vYmFy" + GOOGLE_KEY
    assert not list(cp.iter_findings(embedded))


def test_google_api_key_placeholder_forms_do_not_fire() -> None:
    """Docs and templates must be able to show the shape without blocking.

    Fails if the new pattern is wired somewhere that bypasses
    `looks_like_placeholder` — a `<PLACEHOLDER>` or `xxxx` body in
    .env.example would then block every commit.
    """
    assert not list(cp.iter_findings("AIza" + "X" * 20 + "xxxx" + "Y" * 10))
    assert not list(cp.iter_findings("AIza" + "SyEXAMPLE" + "b" * 25))


def test_partial_service_account_json_without_the_pem_body_is_a_finding() -> None:
    """G3, the one that matters here: `.gcp/kinoforge-sa.json` IS the credential.

    The full SA JSON only blocks today via `pem_private_key`. A truncated
    terminal capture — the metadata rows, no key body — is exactly what a
    scrolled-off `cat` produces, and it commits clean. Fails if the
    `private_key_id` field is not covered.
    """
    # No project_id / client_email row: both are cloud IDENTIFIERS, and
    # tests/test_cloud_identifier_scrub.py scans this file for them. The
    # private_key_id row is the whole point of the fixture anyway.
    partial = (
        '{"type": "service_account", '
        f'"private_key_id": "{SA_KEY_ID}"}}'  # kinoforge: allow-secret
    )
    (finding,) = [
        f for f in cp.iter_findings(partial) if f.pattern_name == "gcp_private_key_id"
    ]
    assert SA_KEY_ID not in finding.redacted_excerpt
    assert "<REDACTED:gcp_private_key_id>" in finding.redacted_excerpt


def test_private_key_id_requires_its_field_name() -> None:
    """A bare 40-hex run is a digest, not a credential.

    Fails if the pattern is widened to bare hex — `pixi.lock` carries
    thousands of 40+ hex digests, and the guard would be unusable. The
    field name is what makes the value identifiable, exactly as the
    identifier scanner's documented narrowings describe.
    """
    assert not list(cp.iter_findings(SA_KEY_ID))
    assert not list(cp.iter_findings(f'"sha1": "{SA_KEY_ID}"'))


def test_private_key_id_placeholder_forms_do_not_fire() -> None:
    """A redacted SA JSON in a design doc must not block a commit."""
    marked = '"private_key_id": "' + "0" * 40 + '"  # placeholder, not a real key'
    assert not list(cp.iter_findings(marked))
    assert not list(cp.iter_findings('"private_key_id": "' + "deadbeef" * 5 + '"'))


def test_url_embedded_password_is_a_finding() -> None:
    """G2: `scheme://user:pass@host` hides a credential with no prefix of its own.

    Fails if the pattern is absent — a connection string pasted into a
    config or a test fixture carries a live password past every other
    pattern, because nothing about the value itself looks credential-shaped.
    """
    url = "postgres://admin:" + "Str0ngP4ssw0rdHere" + "@db.internal:5432/kino"
    names = {f.pattern_name for f in cp.iter_findings(url)}
    assert "url_credentials_strict" in names


def test_short_url_password_redacts_but_does_not_block() -> None:
    """The tier split for G2, on the brief's own second example.

    `https://user:token@host/path` is the shape docs use; blocking on it
    teaches --no-verify. It must still be scrubbed from the transcript.
    Fails if the strict floor is removed (docs example blocks) or if the
    loose companion is missing (the password reaches the transcript).
    """
    url = "https://user:" + "token" + "@host/path"
    assert not list(cp.iter_findings(url))
    assert "<REDACTED:url_credentials>" in cp.redact_string(url)


def test_wordy_doc_password_redacts_but_does_not_block() -> None:
    """The digit requirement, pinned on the canonical Postgres docs string.

    `mysecretpassword` is 16 chars — past any length floor — and carries no
    placeholder marker, so length alone cannot tell it from a real secret.
    Fails if the digit requirement is dropped, which would make the most
    widely copy-pasted connection string in existence block every commit.
    """
    url = "postgres://user:" + "mysecretpassword" + "@localhost:5432/db"
    assert not list(cp.iter_findings(url))
    assert "<REDACTED:url_credentials>" in cp.redact_string(url)


def test_url_password_that_is_a_shell_variable_reference_does_not_block() -> None:
    """Same boundary `credential_assignment` already draws for `VAR=$OTHER`.

    `https://oauth2:$GH_TOKEN@github.com/...` is this repo's documented way
    of showing "pass your own token here". Fails if the `$VAR` exclusion is
    missing from the strict variant.
    """
    url = "https://oauth2:" + "$GH_TOKEN" + "@github.com/emmy/kinoforge.git"
    assert not list(cp.iter_findings(url))


def test_url_with_a_port_but_no_userinfo_is_untouched() -> None:
    """`host:port` is not `user:password`.

    Fails if the `@` terminator is dropped or the userinfo class is allowed
    to cross `/`: every ordinary URL in every log line would then be
    redacted, which is the over-scrub that corrupts `Read` output.
    """
    for url in (
        "https://db.internal:5432/kino",
        "postgres://db.internal/kino",
        "https://abcd1234-8001.proxy.runpod.net/bootstrap.log",
    ):
        assert cp.redact_string(url) == url


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
    url = "postgres://admin:" + "Str0ngP4ssw0rdHere" + "@db.internal:5432/kino"
    text = (
        f"HF_TOKEN={HF_KEY} key={AWS_KEY} Authorization: Bearer {RPA_KEY} "  # kinoforge: allow-secret
        f'{url} {GOOGLE_KEY} "private_key_id": "{SA_KEY_ID}"'
    )
    once = cp.redact_string(text)
    twice = cp.redact_string(once)
    assert twice == once
