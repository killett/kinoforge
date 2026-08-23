"""`.env.example` must stay in sync with what the code actually reads.

Two directions, two different failure modes. A key documented but consumed
by nothing sends an operator hunting for a credential they do not need. A
var consumed but documented nowhere makes a feature look broken.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from dotenv import dotenv_values

_REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_ENV_EXAMPLE: Path = _REPO_ROOT / ".env.example"

_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_MARKER_RE = re.compile(r"#\s*(UNIMPLEMENTED|OPTIONAL)\b")

# This file's own path, relative to the repo root, as `git ls-files` would
# report it. Excluded from its own search corpus below: every curated var
# name and every documented key is spelled out as a string literal in this
# file, so without the exclusion `key in source` and `v in source` are
# trivially true for anything this file merely *mentions* — the consumed
# check and the anti-rot guard would both pass vacuously regardless of
# whether real code reads the var.
_OWN_PATH_REL: str = Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()

# Directories/files whose mention of a var does not count as consumption — a
# doc naming a key proves nothing about whether code reads it. Any tracked
# `.md` file is prose by convention (CLAUDE.md, AGENTS.md, SPEC.md, DESIGN.md,
# per-directory READMEs, …); the explicit prefixes below additionally cover
# non-`.md` doc surfaces (e.g. the `.tasks.json` sidecar under docs/).
_DOC_PREFIXES = ("docs/", "PROGRESS.md", "README.md", "successful-generations.md")

# Vars an operator must set to use a documented kinoforge feature. Hand
# curated on purpose: adding a member is a deliberate act, which is what
# makes the reverse check meaningful. Internal knobs (KINOFORGE_LIVE_TESTS,
# KINOFORGE_DIAG_BUCKET, KINOFORGE_PROVISION_B64_*, KINOFORGE_SKIP_*,
# KINOFORGE_VALIDATE_SCOPED_LIVE) are out of scope by construction — they
# are not credentials or endpoints an operator configures to use a feature,
# they are internal safety/CI knobs whose safe default is "unset".
_OPERATOR_FACING_VARS = frozenset(
    {
        "HF_TOKEN",
        "RUNPOD_API_KEY",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KINOFORGE_GCS_BUCKET",
        "KINOFORGE_S3_BUCKET",
        "GCP_BILLING_ACCOUNT_ID",
        # Read at tests/live/test_nova_reel_live.py and
        # tests/live/test_luma_ray_live.py — both skip with a message
        # naming this var when it is unset. Before this test existed, it
        # was documented nowhere but those two pytest.skip strings.
        "KINOFORGE_LIVE_S3_BUCKET",
    }
)


def _tracked_source_text() -> str:
    """Return the concatenated text of every tracked non-doc file.

    Returns:
        Concatenation of tracked file contents, excluding `.env.example`
        itself, this test file's own source, and the doc surfaces matched
        by :data:`_DOC_PREFIXES` / the `.md` extension.
    """
    listing = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z"],  # noqa: S607
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    chunks: list[str] = []
    for rel in listing.split("\0"):
        if not rel or rel in (".env.example", _OWN_PATH_REL):
            continue
        if rel.startswith(_DOC_PREFIXES) or rel.endswith(".md"):
            continue
        raw = _REPO_ROOT / rel
        try:
            data = raw.read_bytes()
        except FileNotFoundError:
            continue
        if b"\x00" in data[:8000]:
            continue
        chunks.append(data.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _keys_with_marker() -> dict[str, bool]:
    """Map each documented key to whether it carries an exemption marker.

    The marker must sit on the line directly above the `KEY=` line. An
    inline trailing comment is not accepted: `KEY= # UNIMPLEMENTED` parses
    ambiguously across dotenv implementations.

    Returns:
        `{key: has_marker}` for every `KEY=` in `.env.example`.
    """
    lines = _ENV_EXAMPLE.read_text().splitlines()
    out: dict[str, bool] = {}
    for idx, line in enumerate(lines):
        match = _KEY_RE.match(line)
        if not match:
            continue
        above = lines[idx - 1] if idx else ""
        out[match.group(1)] = bool(_MARKER_RE.search(above))
    return out


def test_env_example_parses_as_dotenv() -> None:
    """The template must load through the same parser the CLI uses.

    A bug that would fail this: an unquoted `#` inside a value, or a stray
    backtick, which turns a documented key into a silently-missing one at
    runtime.
    """
    parsed = dotenv_values(_ENV_EXAMPLE)
    assert parsed, ".env.example produced no keys"
    assert "RUNPOD_API_KEY" in parsed


def test_runpod_terminate_key_expands_rather_than_being_literal(tmp_path: Path) -> None:
    """The `${RUNPOD_API_KEY}` reference must actually interpolate.

    `.env.example` promises the terminate key reuses the main key via
    expansion. Asserting only `"$" not in parsed["RUNPOD_TERMINATE_KEY"]`
    against the shipped template is too weak to prove that: `RUNPOD_API_KEY`
    ships empty, so expansion, deletion of the line, hardcoding it empty,
    and typo'ing the referenced var name all collapse to the same empty
    string and all pass. To actually distinguish expansion from absence,
    substitute a sentinel into a scratch copy of `RUNPOD_API_KEY` and assert
    the terminate key resolves to that exact sentinel — only a real
    `${RUNPOD_API_KEY}` expansion can produce it.

    A bug that would fail this: quoting the value so dotenv treats
    `${RUNPOD_API_KEY}` as a literal string (embedding `${RUNP...` into pod
    env instead of the key), deleting the terminate-key line, hardcoding it
    to a literal placeholder, or a typo in the referenced var name.
    """
    sentinel = (
        "sentinel-runpod-key-9f3c1a"  # synthetic placeholder value, not a real key
    )
    original = _ENV_EXAMPLE.read_text()
    substituted, count = re.subn(
        r"(?m)^RUNPOD_API_KEY=.*$",
        f"RUNPOD_API_KEY={sentinel}",  # placeholder substitution, not a real credential
        original,
    )
    assert count == 1, "expected exactly one RUNPOD_API_KEY= line in .env.example"

    scratch = tmp_path / ".env.example"
    scratch.write_text(substituted)
    parsed = dotenv_values(scratch)
    assert parsed.get("RUNPOD_TERMINATE_KEY") == sentinel


def test_every_documented_key_is_consumed_or_marked() -> None:
    """A documented key with no consumer sends operators hunting for nothing.

    A bug that would fail this: adding a credential block for a provider
    whose adapter was never written, with no marker saying so.
    """
    source = _tracked_source_text()
    unexplained = [
        key
        for key, marked in _keys_with_marker().items()
        if not marked and key not in source
    ]
    assert not unexplained, (
        f"{len(unexplained)} key(s) in .env.example are read by no tracked "
        f"source file and carry no marker: {sorted(unexplained)}\n"
        "Fix: either wire the key up, or put "
        "`# UNIMPLEMENTED — no kinoforge code reads this yet` on the line "
        "directly above it."
    )


def test_every_operator_facing_var_is_documented() -> None:
    """A var the code reads but the template omits makes a feature look broken.

    Seeded by `KINOFORGE_S3_BUCKET` (read at
    `src/kinoforge/stores/s3/__init__.py`, documented nowhere before this
    test existed) and `KINOFORGE_LIVE_S3_BUCKET` (read at two live-smoke
    test files, discoverable nowhere except their `pytest.skip` messages).

    A bug that would fail this: adding a new store that reads
    `KINOFORGE_<X>_BUCKET` and forgetting the `.env.example` entry.
    """
    documented = set(_keys_with_marker())
    missing = sorted(_OPERATOR_FACING_VARS - documented)
    assert not missing, (
        f"operator-facing var(s) absent from .env.example: {missing}\n"
        "Fix: add a documented block for each, or drop it from "
        "_OPERATOR_FACING_VARS if it is not something an operator sets."
    )


def test_curated_list_only_holds_vars_the_code_reads() -> None:
    """Guards the curated list against its own drift.

    Without this, a var could be deleted from the codebase and linger in
    `_OPERATOR_FACING_VARS` forever, forcing `.env.example` to document
    something that no longer exists.
    """
    source = _tracked_source_text()
    stale = sorted(v for v in _OPERATOR_FACING_VARS if v not in source)
    assert not stale, (
        f"_OPERATOR_FACING_VARS names vars no tracked source reads: {stale}"
    )
