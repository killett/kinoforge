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

Known, accepted gap: this hook pattern-matches enumerated shell shapes
(env dumps, dotenv reads, cloud token prints, ...). It does not and
cannot cover arbitrary interpreter one-liners that read a credential
another way, e.g. `python -c 'import os;print(os.environ["HF_TOKEN"])'`.
That is a different problem class — the PostToolUse scrubber is the
second line of defence there, not this hook.

Also inherent to text matching: the hook reasons about command TEXT,
not what the shell will actually execute. A heredoc or string literal
that merely *writes prose about* a credential-echoing command (e.g. a
progress note documenting this very deny surface) can match and be
denied even though nothing would have leaked. This is a known false
positive, considered acceptable — write such prose to a file via
Write/Edit instead of through a Bash heredoc.

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

# Statement-start anchor: matches at the very beginning of the command, or
# immediately after anything that starts a new shell statement — a
# separator (;, &, |; this also covers `&&` and `||` without a dedicated
# alternative, since `.search()` tries every start position and the
# *second* character of a two-char operator satisfies the single-char
# branch on its own, e.g. in "a && b" the match starts at the second `&`),
# a newline (multi-line commands), or anything that opens a nested
# execution context: `(` (subshell), `` ` `` (backtick command
# substitution), `{` (brace group), or `$(` (command substitution — `(`
# alone already covers this via the same second-character trick, `\$\(`
# is kept explicit for clarity/robustness). Command substitution,
# backticks, subshells and newlines are ordinary exfiltration idioms
# (`echo "$(cat .env)"`, `` echo `cat .env` ``, `(cat .env)`,
# `x=$(declare -p)`) and MUST stay covered — an anchor that only
# recognised `;`/`&`/`|` let all of these bypass the rules below it.
#
# An UNanchored command-name test matches inside quoted strings and
# filenames too (`rg "declare -p" docs/`, `nl-report.csv`,
# `bash cut-video.sh .env`) — this fragment is what rules those out
# (neither a space nor a quote is in the class) while still catching the
# command wherever it legitimately starts a statement.
_STMT = r"(?:^|[\n;&|(`{]\s*|\$\(\s*)"

# Matches ".env" only when it is NOT the ".env.example" template — and
# "not the template" is anchored: ".example" must end the filename, not
# merely appear next. `.env.example.bak` / `.env.example.production`
# still carry real secrets under a decoy name and must deny; only an
# exact `.env.example` (nothing after) is the safe template.
_DOTENV_NOT_EXAMPLE = r"\.env(?!\.example(?![\w.-]))\b"

# Command names that dump a dotenv file's contents when pointed at it.
_DOTENV_READ_CMDS = (
    r"(?:cat|less|more|head|tail|bat|rg|grep|strings|awk|sed|od|xxd|nl|cut)"
)

DENY_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "bare environment dump",
        # Bare `env`/`printenv` (dumps every var) still denies at a
        # statement boundary. Piping to a pure counter (`wc`) is a shape
        # probe, not a leak, and stays allowed; any other pipe target
        # (grep, rg, head, ...) can filter straight to a credential and
        # is functionally identical to `printenv SOME_TOKEN`, so it
        # denies too. `env FOO=bar cmd` (env as a command prefix) is
        # unaffected — it never reaches end-of-statement, a pipe, or a
        # redirect. `>` is a terminator too: `env > out.txt` writes the
        # whole environment to a file just as surely as printing it.
        # Optional `sudo` prefix tolerated so `sudo env` still denies.
        re.compile(rf"{_STMT}(?:sudo\s+)?(?:env|printenv)\s*(?:$|[;&>]|\|(?!\s*wc\b))"),
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
        "bare shell-state dump",
        # Argument-less `set` dumps every shell variable. `set -euo
        # pipefail` (or any other flag/arg) is ordinary script hygiene
        # and must stay allowed — only the bare, boundary-anchored form
        # matches. Optional `sudo` prefix tolerated so `sudo set` still
        # denies.
        re.compile(rf"{_STMT}(?:sudo\s+)?set\s*(?:$|[;&|])"),
    ),
    (
        "declare/typeset dump",
        # Statement-anchored: an unanchored `\bdeclare\s+-p\b` matches
        # `declare -p` anywhere, including inside a quoted string (a
        # verification command like `rg "declare -p" docs/` does not run
        # it) — the anchor is what tells "run this" apart from "mention
        # this".
        re.compile(rf"{_STMT}(?:sudo\s+)?(?:declare|typeset)\s+-p\b"),
    ),
    (
        "dotenv read",
        # Statement-anchored for the same reason as declare/typeset:
        # without it, a short read-command token (`nl`, `cut`, `od`, ...)
        # matches as a *substring of an unrelated filename* anywhere
        # earlier in the statement — `mv nl-report.csv .env.production`
        # and `bash cut-video.sh .env` are renames/script-runs, not
        # reads, and must not deny. Anchoring the command to a statement
        # start closes that without narrowing what still gets caught:
        # the rule still fires whenever the read command genuinely
        # starts a statement, including after `;`, `&&`, `||`, and as
        # the right-hand side of a pipe.
        re.compile(
            rf"{_STMT}(?:sudo\s+)?{_DOTENV_READ_CMDS}\b[^;&|]*{_DOTENV_NOT_EXAMPLE}"
        ),
    ),
    (
        "dotenv source",
        # `source .env` / `. .env` load every credential into the
        # current shell for a later leak, even though nothing prints
        # yet. `source .venv/bin/activate` (or any path without a
        # literal `.env` component) is unaffected.
        re.compile(rf"{_STMT}(?:source|\.)\s+\S*{_DOTENV_NOT_EXAMPLE}"),
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
