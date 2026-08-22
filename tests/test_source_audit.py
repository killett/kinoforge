"""Lockdown: no tracked file in the repo may contain a credential-shaped literal.

Fail-closed standing guard over `git ls-files`, not a hand-listed path
subset. Fires even when the committer used `--no-verify`, which the
pre-commit hook cannot see.

Pairs with:
- `tools/scan_secrets.py` (the same strict tier, applied to staged
  content at commit time via the pre-commit hook).
- `_RecordingHTTPSeam.flush()` in tests/providers/conftest_runpod.py
  (runtime backstop for NEW leaks at fixture-capture time).
- tests/providers/test_fixtures_audit.py (walks tests/**/*.json with the
  loose production credential patterns).

Previously this walked a hand-listed subset (docs/superpowers/**.md,
tests/**.py, five root files) with a private 4-pattern copy. That could
not see a credential pasted into examples/, tools/, src/, or a config
file, and the pattern list could silently drift from
`src/kinoforge/core/credential_patterns.py`. Both problems are closed by
scanning every tracked file with the shared strict tier via
`tools.scan_secrets.scan_all_tracked`.
"""

from __future__ import annotations

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
    """Guards against a refactor that empties or guts the strict tier.

    ``hf_token`` (unsuffixed) is a *loose*-tier name only — the naming
    rule in ``credential_patterns.py`` reserves the plain name for the
    redactor pattern and puts the strict variant under ``hf_token_strict``
    — so the canonical strict-tier name asserted here is the ``_strict``
    form, not the bare one.
    """
    names = {p.name for p in STRICT_PATTERNS}
    expected = {"sk_token", "aws_access_key", "pem_private_key", "hf_token_strict"}
    assert not expected - names, (
        f"strict tier missing canonical names: {expected - names}"
    )
