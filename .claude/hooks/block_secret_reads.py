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
        re.compile(r"(?:^|[;&|]\s*)(?:env|printenv)\s*(?:$|[;&])"),
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
        re.compile(
            r"\b(?:cat|less|more|head|tail|bat|rg|grep|strings)\b[^;&|]*\.env(?!\.example)\b"
        ),
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
                f'instead: {SAFE_ALTERNATIVE}, or `echo "len=${{#VAR}}"`. '
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
