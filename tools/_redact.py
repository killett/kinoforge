"""Credential-pattern scrubber for ``tools/`` debug + error paths.

Mirrors the credential regex vocabulary in
:mod:`tests.providers.conftest_runpod` (which itself was hardened by
PROGRESS:213 / Layer P bug-fix #1) but lives under ``tools/`` so a live
capture script can scrub stdout/stderr without importing test
fixtures.

Pattern declaration order matters: the first matching pattern for a
given substring wins (``re.sub`` is applied in order). ``Bearer …`` is
declared before the inner token patterns so a ``Bearer rpa_xxx`` header
collapses to ``<REDACTED:bearer_auth>`` rather than
``Bearer <REDACTED:rpa_token>``. This keeps the header's structural
shape from leaking the prefix word ``Bearer`` while still hiding the
token body.

Single source of truth for ``tools/``. If a third consumer of
credential redaction lands (e.g. an SDK adapter), factor this module
into ``kinoforge.core.redaction`` and re-import here.
"""

from __future__ import annotations

import re
import sys

_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b")),
    ("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
]


def redact_string(s: str) -> str:
    """Replace every credential-pattern match in *s* with a named marker.

    Args:
        s: Arbitrary text — log line, exception ``repr``, JSON body.

    Returns:
        A copy of *s* with each match of every pattern in
        :data:`_CREDENTIAL_PATTERNS` (in declaration order) replaced by
        ``<REDACTED:{pattern_name}>``. Non-matching text is preserved
        verbatim, including surrounding punctuation, whitespace, and
        unrelated identifiers.
    """
    for name, pattern in _CREDENTIAL_PATTERNS:
        s = pattern.sub(f"<REDACTED:{name}>", s)
    return s


def safe_print(msg: str) -> None:
    """Emit *msg* to ``sys.stderr`` after passing it through :func:`redact_string`.

    Args:
        msg: The string to emit. Already-stringified — callers wrap
            ``str(exc)`` or f-strings themselves so the redactor only
            ever sees ``str``.

    Notes:
        Writes to ``stderr`` only; tool stdout is reserved for the
        machine-parseable success line. Never raises — if writing
        fails (rare; closed pipe), the exception propagates naturally.
    """
    print(redact_string(msg), file=sys.stderr)
