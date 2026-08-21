"""Credential-pattern scrubber for ``tools/`` debug + error paths.

Lives under ``tools/`` so a live capture script can scrub stdout/stderr
without importing test fixtures. The pattern vocabulary itself now
lives in :mod:`kinoforge.core.credential_patterns` — that module is the
single source of truth; this one is just the ``tools/`` entry point.

Declaration order in the shared list matters: ``bearer_auth`` is
declared before the inner token patterns so a ``Bearer rpa_xxx`` header
collapses to ``<REDACTED:bearer_auth>`` rather than
``Bearer <REDACTED:rpa_token>``. This keeps the header's structural
shape from leaking the prefix word ``Bearer`` while still hiding the
token body.

Keep in sync with the Claude Code user-scope hook at
``~/.claude/hooks/redact_secrets.py`` (``CREDENTIAL_PATTERNS``). That
hook scrubs tool output before the bytes reach the conversation
transcript, and it carries a SUPERSET of this list (project shapes +
extra cloud-key shapes the project itself doesn't need to redact
internally). Drift caught by
``/workspace/tests/test_redact_hook_parity.py``.
"""

from __future__ import annotations

import sys

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
    ``tools``' public surface so live-capture scripts do not need to
    know where the list lives.

    Args:
        s: Arbitrary text — log line, exception ``repr``, JSON body.

    Returns:
        A copy of *s* with each match replaced by
        ``<REDACTED:{pattern_name}>``.
    """
    return _shared_redact(s)


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
