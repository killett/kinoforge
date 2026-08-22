"""Deny surface of the PreToolUse credential-exfiltration blocker.

The hook is driven the way Claude Code drives it: JSON on stdin, JSON on
stdout, via a real subprocess under bare `python3` with no PYTHONPATH —
that is the environment it actually runs in, and importing it in-process
would hide a stdlib-only violation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

HOOK = (
    Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "block_secret_reads.py"
)


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
