"""Deny surface of the PreToolUse credential-exfiltration blocker.

The hook's behavioral contract (deny/allow decisions) is driven the way
Claude Code drives it: JSON on stdin, JSON on stdout, via a real
subprocess under bare `python3` with no PYTHONPATH — that is the
environment it actually runs in, and importing it in-process would hide
a stdlib-only violation.

`_executable_context()` is also unit-tested directly, by import — this
is a deliberate exception to the subprocess-only rule above. It is a
pure string-transform function with no I/O and no dependency on being
invoked as a hook; importing it lets the quoting-matrix tests below
assert exactly what survives masking without going through the JSON
protocol, which round 5's review flagged as the actual gap (behavioral
tests are enumerative — a fixed command list — and enumerative tests
missed the escaped-quote fail-open that a model-level test catches by
construction). The subprocess-only rule stays in force for `main()` and
the deny/allow decision surface.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOK = (
    Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_secret_reads.py"
)


def _load_hook_module() -> Any:
    """Import block_secret_reads.py in-process for unit-testing its pure functions.

    Returns:
        The loaded module object, exposing `_executable_context` for
        direct assertions. Only used for that one pure function — every
        deny/allow behavioral test in this file still goes through the
        real subprocess contract via `_run`.
    """
    spec = importlib.util.spec_from_file_location("block_secret_reads", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HOOK_MODULE = _load_hook_module()
_executable_context = _HOOK_MODULE._executable_context


def _run(command: str, tool_name: str = "Bash") -> dict[str, Any]:
    """Invoke the hook with a Bash command and return its parsed stdout."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, "-E", "-S", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        cwd="/",
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _is_deny(out: dict[str, Any]) -> bool:
    """True when the hook returned a PreToolUse deny decision."""
    return out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "env",
        "printenv",
        "echo $AWS_SECRET_ACCESS_KEY",
        "echo ${HF_TOKEN}",
        'echo "$RUNPOD_API_KEY"',
        "printenv MODAL_TOKEN_SECRET",
        "cat .env",
        "cat /workspace/.env",
        "head -5 .env",
        "gcloud auth print-access-token",
        "gcloud auth print-identity-token",
        "aws configure get aws_secret_access_key",
        "modal token show",
        "echo hello && cat .env",
        "printenv | grep -i token",
        "env | rg RUNPOD",
        "set",
        "declare -p",
        "declare -p SOME_VAR",
        "typeset -p",
        "source .env",
        ". .env",
        "awk '{print}' .env",
        "cat .env.example.bak",
        "cat .env.example.production",
        "cat .env.example_bak",
        "cd /tmp && cat .env",
        "false || cat .env",
        "foo; declare -p",
        "echo hi; set",
        "env > out.txt",
        'echo "$(cat .env)"',
        "echo `cat .env`",
        "(cat .env)",
        "x=$(declare -p)",
        "foo\ncat .env",
        "sudo env",
        "sudo set",
        "{ cat .env; }",
        "if cat .env; then echo x; fi",
        "! cat .env",
        "time cat .env",
        "nohup cat .env &",
        "do cat .env; done",
        "if [ -f .env ]; then cat .env; fi",
        'echo "`cat .env`"',
        "echo \\' ; cat .env",
        'echo \\" ; cat .env',
    ],
)
def test_denied(command: str) -> None:
    """Each shape would put a live credential into the transcript."""
    assert _is_deny(_run(command)), command


@pytest.mark.parametrize(
    "command",
    [
        '[ -n "${HF_TOKEN:-}" ] && echo set',
        'echo "HF_TOKEN len=${#HF_TOKEN}"',
        "rg RUNPOD_API_KEY .env.example",
        "cat .env.example",
        "env | wc -l",
        "echo $HOME",
        "echo $PIXI_PROJECT_ROOT",
        "aws sts get-caller-identity",
        "git status",
        "set -euo pipefail",
        "env FOO=bar python x.py",
        "source .venv/bin/activate",
        ". .venv/bin/activate",
        'rg "declare -p" docs/',
        'rg "set -euo" tools/',
        "mv nl-report.csv .env.production",
        "bash cut-video.sh .env",
        "awk '{print $1}' data.csv",
        "sed -i s/a/b/ notes.md",
        "cut -d, -f1 report.csv",
        "nl script.sh",
        "od -c blob.bin",
        "xxd header.png",
        'echo "use $(git rev-parse HEAD)"',
        "files=$(ls src/)",
        "echo 'See `cat .env` for the pattern'",
        'echo "(cat .env config) is an example"',
        "printf '%s\\n' \"Steps: { cat .env; } to inspect\" > doc.txt",
        "echo 'the env command dumps everything'",
        'git commit -m "wire up cat .env guard"',
        'printf "run set or declare -p to inspect\\n"',
        "echo Please do cat .env inspection later",
        "echo This time cat .env matters a lot",
        "echo the command cat .env dumps secrets",
        "# do cat .env cleanup later",
    ],
)
def test_not_denied(command: str) -> None:
    """Ordinary work must pass. A blocker that fires on these gets disabled."""
    assert not _is_deny(_run(command)), command


def test_non_bash_tool_is_ignored() -> None:
    """The hook only reasons about shell commands."""
    assert not _is_deny(_run("echo $AWS_SECRET_ACCESS_KEY", tool_name="Read"))


def test_deny_reason_teaches_the_safe_alternative() -> None:
    """A refusal that does not say what to do instead gets worked around."""
    reason = _run("echo $HF_TOKEN")["hookSpecificOutput"]["permissionDecisionReason"]
    assert '[ -n "${VAR:-}" ] && echo set' in reason


def test_malformed_stdin_does_not_block_the_session() -> None:
    """Fail-open on garbage: the blocker must never wedge the tool loop."""
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="not json",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert not proc.stdout.strip()


# ---------------------------------------------------------------------------
# _executable_context() quoting matrix — unit-level, by import (see the
# module docstring above for why this is a deliberate exception to the
# subprocess-only rule). Round 5's review found a Critical fail-open that
# the enumerative deny/allow tests above never exercised, because they
# only ever pinned specific COMMANDS, not the quoting MODEL those commands
# happen to use. These tests pin the model directly: for each quoting
# shape, assert what survives as "live" text and what gets masked.
# ---------------------------------------------------------------------------


def test_context_escaped_single_quote_outside_quotes_stays_literal() -> None:
    """A backslash-escaped `'` outside any quotes must not open a quoted span.

    This is the round-5 Critical: treating a bare `'` as a real quote-open
    masked everything from that point to end-of-string, including the
    `; cat .env` statement after it — `echo \\' ; cat .env` really does
    run `cat .env` in bash, but the hook ALLOWED it. Since no real quote
    is ever opened here, nothing should be masked at all.
    """
    command = "echo \\' ; cat .env"
    assert _executable_context(command) == command


def test_context_escaped_double_quote_outside_quotes_stays_literal() -> None:
    """Same Critical bug, `\\"` instead of `\\'`."""
    command = 'echo \\" ; cat .env'
    assert _executable_context(command) == command


def test_context_single_quoted_span_fully_masked() -> None:
    """Bash expands nothing inside single quotes, not even backticks."""
    command = "echo 'cat .env and `cat .env` too'"
    context = _executable_context(command)
    assert ".env" not in context
    assert "`" not in context
    assert context.startswith("echo ")


def test_context_double_quoted_dollar_paren_survives() -> None:
    """`$(...)` inside double quotes actually runs — must survive masking."""
    command = 'echo "prefix $(cat .env) suffix"'
    context = _executable_context(command)
    assert "$(cat .env)" in context
    assert "prefix" not in context
    assert "suffix" not in context


def test_context_double_quoted_backtick_survives() -> None:
    """Backtick substitution inside double quotes actually runs too."""
    command = 'echo "prefix `cat .env` suffix"'
    context = _executable_context(command)
    assert "`cat .env`" in context
    assert "prefix" not in context


def test_context_double_quoted_dollar_brace_var_survives() -> None:
    """`${VAR}` inside double quotes expands — must survive masking."""
    command = 'echo "prefix ${RUNPOD_API_KEY} suffix"'
    context = _executable_context(command)
    assert "${RUNPOD_API_KEY}" in context
    assert "prefix" not in context


def test_context_double_quoted_dollar_var_survives() -> None:
    """Bare `$VAR` (no braces) inside double quotes expands too."""
    command = 'echo "prefix $RUNPOD_API_KEY suffix"'
    context = _executable_context(command)
    assert "$RUNPOD_API_KEY" in context
    assert "prefix" not in context


def test_context_double_quoted_prose_is_masked() -> None:
    """Plain prose text inside double quotes carries no execution power."""
    command = 'echo "just a sentence about cat .env usage"'
    context = _executable_context(command)
    assert "cat" not in context
    assert ".env" not in context


def test_context_escaped_dollar_inside_double_quotes_masked() -> None:
    """`\\$` inside double quotes is a literal `$`, not an expansion."""
    command = 'echo "\\$HOME"'
    context = _executable_context(command)
    assert "$HOME" not in context


def test_context_escaped_backtick_inside_double_quotes_masked() -> None:
    """An escaped backtick inside double quotes is literal, not substitution."""
    command = 'echo "\\`cat .env\\`"'
    context = _executable_context(command)
    assert "cat" not in context
    assert ".env" not in context


def test_context_escaped_dollar_brace_inside_double_quotes_masked() -> None:
    """`\\${` inside double quotes is literal, not the start of `${VAR}`."""
    command = 'echo "\\${RUNPOD_API_KEY}"'
    context = _executable_context(command)
    assert "${RUNPOD_API_KEY}" not in context


def test_context_single_quote_inside_double_quotes_is_literal() -> None:
    """A `'` has no special meaning inside double quotes — masked as prose."""
    command = 'echo "it' + "'" + 's a .env file"'
    context = _executable_context(command)
    assert ".env" not in context


def test_context_double_quote_inside_single_quotes_is_literal() -> None:
    """A `"` has no special meaning inside single quotes — the span isn't reopened."""
    command = "echo 'say \"cat .env\" aloud'"
    context = _executable_context(command)
    assert ".env" not in context
    assert context.startswith("echo ")


def test_context_unterminated_single_quote_masked_to_end() -> None:
    """An unterminated quote is masked to end-of-string.

    This is safe, not lossy: an unterminated quote is a bash syntax
    error, so nothing in the whole command executes — masking everything
    after the opening quote (rather than guessing where it "should" have
    closed) cannot hide a real credential read that would actually run.
    """
    command = "echo 'oops ; cat .env"
    context = _executable_context(command)
    assert ".env" not in context
    assert context.startswith("echo ")


def test_context_unterminated_double_quote_masked_to_end() -> None:
    """Same safety argument as the single-quote case, double-quote form."""
    command = 'echo "oops ; cat .env'
    context = _executable_context(command)
    assert ".env" not in context
    assert context.startswith("echo ")


@pytest.mark.parametrize(
    "command",
    [
        "echo \\' ; cat .env",
        'echo \\" ; cat .env',
        "echo 'cat .env and `cat .env` too'",
        'echo "prefix $(cat .env) suffix"',
        'echo "prefix `cat .env` suffix"',
        'echo "prefix ${RUNPOD_API_KEY} suffix"',
        'echo "prefix $RUNPOD_API_KEY suffix"',
        'echo "just a sentence about cat .env usage"',
        'echo "\\$HOME"',
        'echo "\\`cat .env\\`"',
        'echo "\\${RUNPOD_API_KEY}"',
        "echo 'oops ; cat .env",
        'echo "oops ; cat .env',
        "if cat .env; then echo x; fi",
        "echo Please do cat .env inspection later",
    ],
)
def test_context_output_length_matches_input_length(command: str) -> None:
    """Offsets must stay valid after the rewrite.

    `_STMT`'s `\\n` anchor (and every other character-class anchor)
    assumes the rewritten string lines up 1:1 with the original — if
    masking ever shrank or grew the string, multi-line anchoring would
    silently point at the wrong character. This holds across every shape
    in the matrix above, not just the simple ones.
    """
    assert len(_executable_context(command)) == len(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo \\' ; cat .env",
        'echo \\" ; cat .env',
    ],
)
def test_context_escaped_quote_regressions_still_deny(command: str) -> None:
    """Behavioral regression guard for the round-5 Critical, via the real hook."""
    assert _is_deny(_run(command)), command


@pytest.mark.parametrize(
    "command",
    [
        "echo Please do cat .env inspection later",
        "echo This time cat .env matters a lot",
        "echo the command cat .env dumps secrets",
        "# do cat .env cleanup later",
    ],
)
def test_context_keyword_prose_regressions_still_allowed(command: str) -> None:
    """Behavioral regression guard for the round-5 Important, via the real hook."""
    assert not _is_deny(_run(command)), command
