#!/usr/bin/env python3
"""Claude Code PostToolUse hook — scrubs secrets from tool output.

Scrubs before the bytes reach the conversation transcript.

Fail-open: any exception lets raw output through unchanged plus a stderr
warning. Goal is best-effort scrub, never to break debugging flow.

Pattern list MUST stay in sync with
`src/kinoforge/core/credential_patterns.py`'s ``CREDENTIAL_PATTERNS`` —
that module is the single source of truth for kinoforge's credential
regexes. This copy exists only because the hook runs under bare
`python3` with no project on `sys.path` and cannot import it. Drift is
caught by `tests/test_redact_hook_parity.py`, which compares regex
source strings exactly against the shared list.

Hook contract (Claude Code PostToolUse):
- stdin:  JSON with tool_output.content (and tool_output.stderr for Bash).
- stdout: empty + exit 0 to pass through unchanged.
          {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "updatedToolOutput": {"content": ...}}}
          to replace what the model sees.
- exit:   non-zero is treated as fail-open by Claude Code (model sees raw).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# CREDENTIAL_PATTERNS — mirrors src/kinoforge/core/credential_patterns.py's
# CREDENTIAL_PATTERNS exactly: same names, same regex source strings, same
# declaration order. tests/test_redact_hook_parity.py is the drift guard.
#
# Order matters: bearer_auth declared FIRST so a "Bearer <inner-token>"
# header collapses to <REDACTED:bearer_auth> rather than leaking the word
# "Bearer". See the docstring of src/kinoforge/core/credential_patterns.py
# for the full naming-tier rationale (loose vs. _strict variants).
# ---------------------------------------------------------------------------
CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ---- loose tier: redaction only, over-matches by design ----------------
    ("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b")),
    # ---- strict tier: may block a commit -----------------------------------
    ("rpa_token_strict", re.compile(r"\brpa_[A-Za-z0-9]{24,}\b")),
    ("hf_token_strict", re.compile(r"\bhf_[A-Za-z0-9]{32,}\b")),
    ("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]*[A-Za-z0-9]{16,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
    ("github_token", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b")),
    ("github_app", re.compile(r"\b(?:gho|ghu|ghs)_[A-Za-z0-9]{36,}\b")),
    ("replicate_token", re.compile(r"\br8_[A-Za-z0-9]{30,}\b")),
    ("runway_key", re.compile(r"\bkey[-_][A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b")),
    # Requires all three dot-separated base64url segments (header.payload.
    # signature). A bare `eyJ...` prefix is just base64 for `{"` and matches
    # ANY base64-encoded JSON body — this repo's GCS fixtures are full of
    # them (e.g. `eyJraW5kIjoic3RvcmFnZSNvYmplY3Rz` decodes to
    # `{"kind":"storage#object...`, not a token). The dots are what make a
    # JWT structurally distinct from arbitrary base64 JSON.
    (
        "jwt",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    ("luma_key", re.compile(r"\bluma-api-[A-Za-z0-9_-]{8,}\b")),
    ("modal_token", re.compile(r"\b(?:ak|as)-[A-Za-z0-9]{20,}\b")),
    ("lambda_key", re.compile(r"\bsecret_[A-Za-z0-9]+_[0-9a-f]{32,}\b")),
    ("gcp_access_token", re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}\b")),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:"
            + "|".join(
                [
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
                ]
            )
            + r")[ \t]*[=:][ \t]*[\"']?"
            r"(?!<REDACTED)(?!<[a-z][a-z_-]*(?:\s|>))(?!\$[A-Z_])[^\s\"'#]{8,}"
        ),
    ),
]


def _load_env_values() -> dict[str, str]:
    """Read project-root .env if present; return ``{value: VARNAME}`` map.

    Tries ``/workspace/.env`` first (current dev container), then
    ``cwd/.env``. Values shorter than 8 chars are skipped — too noisy.
    All exceptions swallowed; failed read returns empty dict (fail-open
    at the data layer too).
    """
    for p in (Path("/workspace/.env"), Path.cwd() / ".env"):
        if not p.is_file():
            continue
        try:
            values: dict[str, str] = {}
            for raw in p.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if len(val) >= 8:
                    values[val] = key
            return values
        except Exception:
            return {}
    return {}


_ENV_VALUES = _load_env_values()


def scrub(text: str) -> str:
    """Apply .env-value substitution then regex patterns. Idempotent.

    Longest .env values substituted first so a short value embedded in
    a longer one doesn't shadow the longer match.
    """
    if not text:
        return text
    for val in sorted(_ENV_VALUES, key=len, reverse=True):
        if val in text:
            text = text.replace(val, f"<REDACTED:{_ENV_VALUES[val]}>")
    for name, pat in CREDENTIAL_PATTERNS:
        text = pat.sub(f"<REDACTED:{name}>", text)
    return text


def main() -> int:
    """Read hook JSON from stdin; emit hookSpecificOutput with scrubbed content.

    Fail-open everywhere — every exception path either returns 0 with
    empty stdout (pass-through) or returns 0 after writing a best-effort
    payload. Never returns non-zero, never raises.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        print(
            f"[redact_secrets] stdin parse failed, pass-through: {exc}", file=sys.stderr
        )
        return 0

    try:
        tool_output = payload.get("tool_output", {})
        if not isinstance(tool_output, dict):
            return 0  # nothing to scrub
        original_content = tool_output.get("content")
        original_stderr = tool_output.get("stderr")
        changed = False
        new_content = original_content
        new_stderr = original_stderr
        if isinstance(original_content, str):
            new_content = scrub(original_content)
            changed = changed or (new_content != original_content)
        if isinstance(original_stderr, str):
            new_stderr = scrub(original_stderr)
            changed = changed or (new_stderr != original_stderr)
        if not changed:
            return 0  # pass-through
        updated: dict[str, object] = {}
        if isinstance(new_content, str):
            updated["content"] = new_content
        if isinstance(new_stderr, str):
            updated["stderr"] = new_stderr
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "updatedToolOutput": updated,
                    }
                }
            )
        )
        return 0
    except Exception as exc:
        print(f"[redact_secrets] scrub failed, pass-through: {exc}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
