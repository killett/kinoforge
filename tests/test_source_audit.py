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
