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

Quoting model: before matching, the command is rewritten into an
"executable context" string (see `_executable_context`) — single-quoted
spans are masked out entirely (bash never expands anything inside
them), double-quoted spans are masked except for the `$(...)`,
`` `...` ``, `$VAR`, and `${VAR}` regions that bash actually expands or
runs even inside double quotes, and a character escaped by a leading
backslash outside any quotes — an escaped quote character, for
instance — is passed through as its own literal pair rather than
treated as a quote-open. Every rule below
matches against that rewritten string, not the raw one. This is what
tells "this text runs a command" apart from "this text merely mentions
one" — `echo "(cat .env config) is an example"` and ``echo 'See `cat
.env` for the pattern'`` are prose, not execution, and the quoting they
use is exactly how bash tells the difference too.

Statement-start anchoring (`_STMT`) additionally requires that a
keyword/prefix word (`if`, `then`, `sudo`, `!`, ...) only counts as a
statement start when it is ITSELF preceded by a real punctuation
boundary or another such prefix word — a bare keyword anywhere in the
text is not enough. This is what tells `if cat .env; then ...` (a real
statement) apart from `echo Please do cat .env inspection later`
(ordinary prose that happens to contain the word `do`).

Known, accepted gaps this hook does not cover:
- Arbitrary interpreter one-liners that read a credential another way,
  e.g. `python -c 'import os;print(os.environ["HF_TOKEN"])'`. Different
  problem class — the PostToolUse scrubber is the second line of
  defence there, not this hook.
- A keyword or command name appearing inside a multi-line heredoc body
  (`cat <<EOF` ... `EOF`) is not distinguished from one that would
  actually run; the heredoc body is ordinary command text as far as
  this hook is concerned. Write such prose to a file via Write/Edit
  instead of through a Bash heredoc.
- The quoting model above is a linear scan, not a real shell parser: it
  does not track nested/mismatched quotes across separate arguments,
  or arithmetic `$(( ))` beyond incidentally handling it as nested
  `$(`. These are judged rare enough in practice not to be worth a
  bespoke shell grammar here.
- ANSI-C quoting (`$'...'`) is not recognised at all — bash decodes
  hex/octal/unicode escape sequences inside `$'...'` (a hex-encoded
  `.env` payload decodes to the literal text `.env`), but this hook
  sees only an ordinary `$VAR`-shaped token check (`$` followed by a
  word character doesn't match a single quote, so the leading `$'` is
  passed through as regular text and nothing inside is ever decoded).
  A hex-encoded `.env` payload passed to `cat` is NOT denied even
  though real bash runs it as a plain `.env` read. Confirmed live.
  Recognised, not fixed this round — decoding ANSI-C escapes correctly
  would need real unescaping logic, a materially bigger change than the
  quoting model above.

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
_PUNCT = r"(?:^|[\n;&|(`{]\s*|\$\(\s*)"

# Prefix words that can chain after a real punctuation anchor without
# themselves being "the" command: shell keywords (`if`, `then`, `elif`,
# `else`, `do`, `while`, `until`, `time`, `nohup`, `command`), `sudo`,
# and the `!` negation operator (kept out of the word list since it is
# punctuation, not a word — `\b` doesn't apply to it the same way).
# `!` requires trailing whitespace (`\s+`, not `\s*`): real bash parses
# `!cat` with no space as a single command-not-found token, not
# negation, so an unspaced `!cat .env` must not deny either.
_STMT_PREFIX = r"(?:(?:if|then|elif|else|do|while|until|time|nohup|command|sudo)|!)\s+"

# Statement-start anchor: a real punctuation boundary (see `_PUNCT`),
# optionally followed by a CHAIN of prefix words — `if sudo cat .env`,
# `; then cat .env`, `do cat .env; done`. The chain only starts once a
# genuine `_PUNCT` boundary has been found; a prefix word is not itself
# an anchor. This is what tells "if cat .env; then ..." (real statement,
# `if` sits at position 0) apart from "echo Please do cat .env later"
# (ordinary prose — `do` is preceded by `Please `, not by any `_PUNCT`
# boundary, so the chain can never reach it, and "the command cat .env
# dumps secrets" is caught the same way even though `command` is itself
# one of the chain words: nothing anchors it either). Command
# substitution, backticks, subshells and newlines are ordinary
# exfiltration idioms (`echo "$(cat .env)"`, `` echo `cat .env` ``,
# `(cat .env)`, `x=$(declare -p)`) and MUST stay covered.
_STMT = rf"(?:{_PUNCT}(?:{_STMT_PREFIX})*)"

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


def _executable_context(command: str) -> str:
    """Rewrite *command* into the text bash would actually treat as live.

    Args:
        command: The raw Bash command text.

    Returns:
        A string the same length as *command* (see the offset-safety
        note below) where every DENY_RULES pattern should be matched
        instead of the raw command. Single-quoted
        spans are replaced with spaces — bash performs zero expansion
        inside single quotes, not even backticks, so nothing there can
        run. Double-quoted spans are replaced with spaces EXCEPT for
        `$(...)`, `` `...` ``, `${...}`, and `$VAR` regions, which bash
        expands or runs even inside double quotes and so are kept
        verbatim. Text outside any quotes is passed through unchanged.
        A backslash-escaped character inside double quotes is masked
        together with its backslash, since bash treats the pair as the
        literal character, not an expansion. OUTSIDE any quotes, a
        character escaped by a leading backslash is passed through
        verbatim (as its own two literal characters) rather than
        examined as a possible quote-open — an escaped single or double
        quote character is itself a literal character, not the start of
        a quoted span, and treating it as a quote-open previously masked
        everything from that point to end-of-string, including any
        command after it (for example, an escaped single quote followed
        by `; cat .env` really does run `cat .env` in bash). Quote
        characters themselves are kept in the output; they carry no
        keyword meaning to any rule below. The return value is always
        exactly the same length as *command*: every branch below
        replaces N input characters with exactly N output characters
        (masked to spaces or kept verbatim), which keeps character
        offsets — and therefore newline-anchored multi-line matching —
        valid after the rewrite.
    """
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch == "'":
            end = command.find("'", i + 1)
            end = n if end == -1 else end + 1
            out.append(" " * (end - i))
            i = end
            continue
        if ch == '"':
            out.append('"')
            i += 1
            while i < n and command[i] != '"':
                if command[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if command[i] == "`":
                    j = command.find("`", i + 1)
                    j = n if j == -1 else j + 1
                    out.append(command[i:j])
                    i = j
                    continue
                if command[i] == "$" and i + 1 < n and command[i + 1] == "(":
                    depth = 1
                    j = i + 2
                    while j < n and depth:
                        if command[j] == "(":
                            depth += 1
                        elif command[j] == ")":
                            depth -= 1
                        j += 1
                    out.append(command[i:j])
                    i = j
                    continue
                if command[i] == "$" and i + 1 < n and command[i + 1] == "{":
                    j = command.find("}", i + 2)
                    j = n if j == -1 else j + 1
                    out.append(command[i:j])
                    i = j
                    continue
                if (
                    command[i] == "$"
                    and i + 1 < n
                    and (command[i + 1].isalpha() or command[i + 1] == "_")
                ):
                    j = i + 1
                    while j < n and (command[j].isalnum() or command[j] == "_"):
                        j += 1
                    out.append(command[i:j])
                    i = j
                    continue
                out.append(" ")
                i += 1
            if i < n:
                out.append('"')
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
        # `sudo env` denies too, via `_STMT`'s prefix chain.
        re.compile(rf"{_STMT}(?:env|printenv)\s*(?:$|[;&>]|\|(?!\s*wc\b))"),
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
        # matches. `sudo set` denies too, via `_STMT`'s prefix chain.
        re.compile(rf"{_STMT}set\s*(?:$|[;&|])"),
    ),
    (
        "declare/typeset dump",
        # Statement-anchored: an unanchored `\bdeclare\s+-p\b` matches
        # `declare -p` anywhere, including inside a quoted string (a
        # verification command like `rg "declare -p" docs/` does not run
        # it) — the anchor is what tells "run this" apart from "mention
        # this". `sudo declare -p` denies too, via `_STMT`'s prefix
        # chain.
        re.compile(rf"{_STMT}(?:declare|typeset)\s+-p\b"),
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
        # the right-hand side of a pipe. `sudo cat .env` denies too, via
        # `_STMT`'s prefix chain.
        re.compile(rf"{_STMT}{_DOTENV_READ_CMDS}\b[^;&|]*{_DOTENV_NOT_EXAMPLE}"),
    ),
    (
        "dotenv source",
        # `source .env` / `. .env` load every credential into the
        # current shell for a later leak, even though nothing prints
        # yet. `source .venv/bin/activate` (or any path without a
        # literal `.env` component) is unaffected. `sudo` is handled by
        # `_STMT`'s prefix chain along with every other anchored rule —
        # `source` is a shell builtin so `sudo source` isn't really
        # meaningful, but a rule silently missing the tolerance invites
        # the next reader to assume there's a reason it's exempt.
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
        deliberately do not. Matching happens against
        :func:`_executable_context`'s rewrite of *command*, not the raw
        text, so quoted prose that merely mentions a denied shape (a
        commit message, a doc search) does not match it.
    """
    context = _executable_context(command)
    for label, pattern in DENY_RULES:
        if pattern.search(context):
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
