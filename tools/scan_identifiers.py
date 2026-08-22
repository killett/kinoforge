"""Lockdown: no tracked file may contain a concrete cloud identifier.

Sibling of `tools/scan_secrets.py`. That module answers "is this a
credential?"; this one answers "is this a real account, key, project,
service account, or bucket?" — identifiers that are not secrets but whose
presence in a tracked file is exactly the discipline `.gitignore:90-91`
already enforces for two files and nothing else.

Escape hatch is a per-line pragma (`kinoforge: allow-identifier`), matching
the `kinoforge: allow-secret` idiom in
`src/kinoforge/core/credential_patterns.py:304`. Deliberately NOT a
directory exclusion: a blanket exclusion is the decay this guard exists to
stop.

See `docs/superpowers/specs/2026-08-21-least-privilege-onboarding-design.md`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

ALLOW_PRAGMA = "kinoforge: allow-identifier"

# AWS's reserved-for-documentation account, plus the all-zero form. Both
# appear throughout tracked examples and are not identifiers of anything.
_RESERVED_AWS_ACCOUNTS = frozenset({"123456789012", "000000000000"})

# Fake project parts already used by tracked tests and docs.
_FAKE_GCP_PROJECTS = frozenset(
    {"proj", "kinoforge-prod-deadbeef", "example", "test", "fake"}
)

# Test doubles in use across tests/ and docs/. Adding a name here is a
# deliberate act — that is the point.
_ALLOWED_BUCKETS = frozenset(
    {"bkt", "bucket", "my-bucket", "layer-w-test", "probe-discard"}
)


@dataclass(frozen=True)
class IdentifierPattern:
    """A named concrete-identifier shape plus its exemptions.

    Attributes:
        name: Stable pattern name, reported in findings.
        regex: Compiled pattern whose group 1 is the identifier value.
        allowed: Values that must never be reported.
    """

    name: str
    regex: re.Pattern[str]
    allowed: frozenset[str]


@dataclass(frozen=True)
class IdentifierFinding:
    """One concrete identifier found in text.

    Attributes:
        pattern_name: Which :class:`IdentifierPattern` fired.
        line_no: 1-indexed line number.
        value: The concrete identifier as matched.
    """

    pattern_name: str
    line_no: int
    value: str


IDENTIFIER_PATTERNS: tuple[IdentifierPattern, ...] = (
    IdentifierPattern(
        name="aws_account_in_arn",
        # Anchored to ARN position. A bare \d{12} matches 53 hash fragments
        # in pixi.lock alone, which would make this guard unshippable.
        regex=re.compile(r"arn:aws[a-z-]*:[a-z0-9*-]*:[a-z0-9-]*:(\d{12})"),
        allowed=_RESERVED_AWS_ACCOUNTS,
    ),
    IdentifierPattern(
        name="aws_account_labelled",
        regex=re.compile(r"""(?i)account[_ -]?id["']?\s*[:=]\s*["']?(\d{12})"""),
        allowed=_RESERVED_AWS_ACCOUNTS,
    ),
    IdentifierPattern(
        name="kms_key_uuid",
        regex=re.compile(
            r"key/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        ),
        allowed=frozenset({"00000000-0000-0000-0000-000000000000"}),
    ),
    IdentifierPattern(
        name="gcp_service_account_email",
        regex=re.compile(r"[a-z0-9-]+@([a-z0-9-]+)\.iam\.gserviceaccount\.com"),
        allowed=_FAKE_GCP_PROJECTS,
    ),
    IdentifierPattern(
        name="gcp_project_id",
        # The negative lookahead hands a project id embedded in a service
        # account email to `gcp_service_account_email`, which is the more
        # specific pattern. Without it one such line yields two findings for
        # a single identifier -- noise in the lockdown test's failure list.
        regex=re.compile(
            r"\b(kinoforge-prod-[0-9a-z]{8})\b(?!\.iam\.gserviceaccount\.com)"
        ),
        allowed=frozenset({"kinoforge-prod-deadbeef"}),
    ),
    IdentifierPattern(
        name="cloud_bucket_uri",
        regex=re.compile(r"(?:gs|s3)://([a-z0-9][a-z0-9._-]{2,62})"),
        allowed=_ALLOWED_BUCKETS,
    ),
)


def iter_identifier_findings(
    text: str, *, patterns: tuple[IdentifierPattern, ...] = IDENTIFIER_PATTERNS
) -> Iterator[IdentifierFinding]:
    """Yield every concrete identifier in *text*.

    Suppression is per-line: a line carrying :data:`ALLOW_PRAGMA` yields
    nothing, and neighbouring lines are unaffected.

    Args:
        text: Content to scan.
        patterns: Pattern tier to apply. Defaults to
            :data:`IDENTIFIER_PATTERNS`.

    Yields:
        One :class:`IdentifierFinding` per non-exempt match.
    """
    for line_no, line in enumerate(text.splitlines(), start=1):
        if ALLOW_PRAGMA in line.lower():
            continue
        for pattern in patterns:
            for match in pattern.regex.finditer(line):
                value = match.group(1)
                if value in pattern.allowed:
                    continue
                yield IdentifierFinding(
                    pattern_name=pattern.name, line_no=line_no, value=value
                )


def scan_all_tracked_identifiers(
    repo: Path, *, patterns: tuple[IdentifierPattern, ...] = IDENTIFIER_PATTERNS
) -> list[tuple[str, IdentifierFinding]]:
    """Scan the working-tree content of every tracked file.

    Binary detection and decoding mirror
    :func:`tools.scan_secrets.scan_all_tracked` exactly, and for the same
    reason: this is a standing guard with no layer behind it, so one stray
    byte must cost one garbled character, not a file's whole coverage.

    A tracked path absent from the working tree is skipped — that is a
    legitimate mid-rebase state. A path that exists but cannot be read is a
    hard error; treating it as "no findings" is the fail-open this guard
    exists to prevent.

    Args:
        repo: Repository working directory.
        patterns: Pattern tier to apply.

    Returns:
        ``(path, IdentifierFinding)`` pairs.

    Raises:
        RuntimeError: A tracked file exists but could not be read.
    """
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    results: list[tuple[str, IdentifierFinding]] = []
    for rel in listing.split("\0"):
        if not rel:
            continue
        target = repo / rel
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot read tracked file {rel}: {exc}") from exc
        if b"\x00" in raw[:8000]:
            continue
        text = raw.decode("utf-8", errors="replace")
        results.extend(
            (rel, finding)
            for finding in iter_identifier_findings(text, patterns=patterns)
        )
    return results
