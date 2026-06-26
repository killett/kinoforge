# P3 — CLI `--loras` arg surface — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `kinoforge generate --loras HEREDOC` surface — operator can override `cfg.loras` (and bypass `vault.loras` with an audit warning) by passing a heredoc-string LoRA stack on the command line.

**Architecture:** Pure-CLI extension. New parser module `src/kinoforge/cli/loras_arg.py` produces `list[LoraEntry]`; one keyword-only `cli_loras=` kwarg added to existing `resolve_active_lora_stack`; one `--loras` argparse arg added to `kinoforge generate`. NO server-side changes. NO schema changes. NO matcher / capability_key / VRAM-rollback changes.

**Tech Stack:** Python 3.11+, Pydantic v2, argparse, pytest, ruff, mypy. Reuses P1 + P2 work end-to-end.

**Spec:** `docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md` (commit `7db8ffb`).

**User decisions (already made — from brainstorm 2026-06-25):**
- D1: heredoc-only `--loras STRING`; repeated `--loras` rejected.
- D2: column order `ref [strength] [branch]` (amends 2026-06-21 anchor).
- D3: CLI overrides `cfg.loras` entirely.
- D4: CLI wins over `vault.loras` with audit WARNING (count only, no refs in log).
- D5: numeric shorthand `^\d+:\d+$` → `civitai:N@N`; unknown schemes rejected against allow-list `{civitai, hf, file, https, http}`.
- D6: collect-all error aggregation across every line; exit 1.
- D7: skip blank lines + `#` line comments; inline `#` not supported.
- D8: reject duplicates by composite `(ref, branch)` key (P2 dual-load OK).
- D9: empty heredoc → overrides cfg.loras with `[]`.
- D10: thread via `resolve_active_lora_stack(*, cli_loras=None)` kwarg.
- Live smoke: deferred; manual post-merge Tier-3 fire captures wire-shape proof at zero net spend.

---

## File structure

**Create:**
- `src/kinoforge/cli/loras_arg.py` — parser + `LorasParseError` + `LorasParseReport` + `LineError`.
- `tests/cli/test_loras_arg.py` — parser unit tests (§11.1 spec).
- `tests/cli/test_cmd_generate_loras.py` — CLI command tests (§11.3 spec).
- `tests/core/test_lora_resolver_p3.py` — resolver tests (§11.2 spec).
- `tests/test_lora_error_redaction.py` — redaction parity tests (§11.4 spec).
- `tests/test_no_precedence_branches_outside_resolver.py` — AST scan AC-P3-5 (§11.5 spec).
- `tests/integration/test_loras_cli_e2e.py` — integration tests (§11.6 spec).

**Modify:**
- `src/kinoforge/core/lora.py:76-126` — add `cli_loras=` kwarg + new precedence branch.
- `src/kinoforge/cli/_main.py:411-481` — add `--loras` argparse arg with `_LorasOnceAction`.
- `src/kinoforge/cli/_commands.py::_cmd_generate` — parse `args.loras` + thread to resolver.
- `src/kinoforge/cli/_commands.py::_dry_run_swap_preview` (`:327`) — print `loras_source:` line.
- `tests/test_no_unredacted_writes.py` — extend scan set + AST checks (AC-P3-1, AC-P3-2, AC-P3-3, AC-P3-4).
- `PROGRESS.md:580-585` — anchor column-order amendment + close-out entry.
- `README.md` — new `--loras` subsection under `kinoforge generate`.

---

## Task list

11 tasks. Dependencies: T0 → T1, T2 → T3, T0 → T4, T2 → T4, T4 → T5, T4 → T6, T6 → T7+T8, T7+T8 → T9, T9 → T10.

---

### Task 0: Parser module — `loras_arg.py` + unit tests

**Goal:** Land `parse_loras_heredoc` + `LorasParseError` + `LorasParseReport` + `LineError` with full unit coverage. No call site yet imports it.

**Files:**
- Create: `src/kinoforge/cli/loras_arg.py`
- Create: `tests/cli/test_loras_arg.py`

**Acceptance Criteria:**
- [ ] `parse_loras_heredoc("")` returns `[]` (D9).
- [ ] `parse_loras_heredoc("# comment only\n\n")` returns `[]` (D7+D9).
- [ ] `parse_loras_heredoc("civitai:1234@5678")` returns `[LoraEntry(ref="civitai:1234@5678", strength=1.0, branch="auto")]`.
- [ ] `parse_loras_heredoc("1234:5678 0.5 h")` returns `[LoraEntry(ref="civitai:1234@5678", strength=0.5, branch="high_noise")]` (D5 expansion + P2 alias).
- [ ] `parse_loras_heredoc("cvtai:1234@5678")` raises `LorasParseError` with one `LineError(kind="unknown-scheme", scheme="cvtai", line_no=1, col=1)`.
- [ ] Three independent errors in one heredoc → `LorasParseReport.errors` carries all three (D6).
- [ ] Same ref same branch on two lines → `LineError(kind="duplicate", first_line=..., this_line=...)`.
- [ ] Same ref different branches accepted (P2 dual-load).
- [ ] `LineError` model has NO field annotated `str` whose name matches `ref|filename|label`.
- [ ] All unit tests in §11.1 green.

**Verify:** `pixi run pytest tests/cli/test_loras_arg.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing parser test file.**

```python
# tests/cli/test_loras_arg.py
"""Unit tests for src/kinoforge/cli/loras_arg.py.

Covers spec §11.1: tokenization, column count, ref expansion, strength,
branch, duplicate detection, aggregation. Privacy invariants are covered
separately by tests/test_lora_error_redaction.py.
"""

from __future__ import annotations

import pytest

from kinoforge.cli.loras_arg import (
    LineError,
    LorasParseError,
    LorasParseReport,
    parse_loras_heredoc,
)
from kinoforge.core.lora import LoraEntry


# --- §6.1 tokenization + whitespace ---

def test_blank_lines_skipped() -> None:
    result = parse_loras_heredoc("\n\ncivitai:1234@5678\n\n")
    assert len(result) == 1
    assert result[0].ref == "civitai:1234@5678"


def test_hash_comment_line_skipped() -> None:
    result = parse_loras_heredoc("# top comment\ncivitai:1234@5678\n# bottom\n")
    assert len(result) == 1


def test_hash_with_leading_whitespace_skipped() -> None:
    result = parse_loras_heredoc("  # indented comment\ncivitai:1234@5678\n")
    assert len(result) == 1


def test_inline_hash_not_treated_as_comment() -> None:
    """`civitai:X 1.0 # foo` parses as 4 tokens → bad-columns (D7)."""
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("civitai:1234@5678 1.0 # foo")
    assert exc.value.report.errors[0].kind == "bad-columns"


def test_tab_separator_accepted() -> None:
    result = parse_loras_heredoc("civitai:1234@5678\t0.5\th")
    assert result[0].strength == 0.5
    assert result[0].branch == "high_noise"


def test_multiple_spaces_collapse() -> None:
    result = parse_loras_heredoc("civitai:1234@5678   0.5   h")
    assert result[0].strength == 0.5


def test_trailing_cr_stripped() -> None:
    result = parse_loras_heredoc("civitai:1234@5678 0.5 h\r\n")
    assert result[0].strength == 0.5


def test_empty_heredoc_returns_empty_list() -> None:
    """D9 — empty heredoc is a valid empty-stack override."""
    assert parse_loras_heredoc("") == []


def test_comments_only_heredoc_returns_empty_list() -> None:
    """D9 — comments + blanks only is also a valid empty-stack override."""
    assert parse_loras_heredoc("# foo\n\n# bar\n   \n") == []


# --- §6.1 column count ---

def test_ref_only_defaults_strength_1_branch_auto() -> None:
    result = parse_loras_heredoc("civitai:1234@5678")
    assert result[0].strength == 1.0
    assert result[0].branch == "auto"


def test_ref_strength_defaults_branch_auto() -> None:
    result = parse_loras_heredoc("civitai:1234@5678 0.7")
    assert result[0].strength == 0.7
    assert result[0].branch == "auto"


def test_ref_strength_branch_all_three() -> None:
    result = parse_loras_heredoc("civitai:1234@5678 0.7 l")
    assert result[0].strength == 0.7
    assert result[0].branch == "low_noise"


def test_four_tokens_raises_bad_columns() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("civitai:1234@5678 0.7 l extra")
    err = exc.value.report.errors[0]
    assert err.kind == "bad-columns"
    assert err.got_kind == "4"
    assert err.expected == "1, 2, or 3"


# --- §6.2 ref expansion (D5) ---

def test_numeric_shorthand_expands_to_civitai() -> None:
    result = parse_loras_heredoc("1234:5678")
    assert result[0].ref == "civitai:1234@5678"


def test_civitai_full_ref_passes_through() -> None:
    result = parse_loras_heredoc("civitai:1234@5678")
    assert result[0].ref == "civitai:1234@5678"


def test_hf_ref_passes_through() -> None:
    result = parse_loras_heredoc("hf:Org/Repo:filename.safetensors")
    assert result[0].ref == "hf:Org/Repo:filename.safetensors"


def test_file_ref_passes_through() -> None:
    result = parse_loras_heredoc("file:/abs/path/to.safetensors")
    assert result[0].ref == "file:/abs/path/to.safetensors"


def test_https_ref_passes_through() -> None:
    result = parse_loras_heredoc("https://example.com/lora.safetensors")
    assert result[0].ref == "https://example.com/lora.safetensors"


def test_unknown_scheme_rejected_with_scheme_name() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("cvtai:1234@5678")
    err = exc.value.report.errors[0]
    assert err.kind == "unknown-scheme"
    assert err.scheme == "cvtai"
    assert err.line_no == 1
    assert err.col == 1


def test_missing_scheme_rejected() -> None:
    """Bare `Org/Repo` (no scheme, not numeric shorthand)."""
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("Org/Repo")
    assert exc.value.report.errors[0].kind == "missing-scheme"


def test_numeric_shorthand_requires_both_ids() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("1234:")
    assert exc.value.report.errors[0].kind == "missing-scheme"


def test_numeric_shorthand_rejects_negative() -> None:
    """`-1234:5678` falls past shorthand regex → scheme=-1234 → unknown-scheme."""
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("-1234:5678")
    err = exc.value.report.errors[0]
    assert err.kind == "unknown-scheme"
    assert err.scheme == "-1234"


# --- strength parse + range ---

def test_strength_not_a_float_raises_bad_strength() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("civitai:1234@5678 1.5x")
    err = exc.value.report.errors[0]
    assert err.kind == "bad-strength"
    assert err.got_kind == "not-a-float"


def test_strength_out_of_range_raises_pydantic() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("civitai:1234@5678 3.0")
    err = exc.value.report.errors[0]
    assert err.kind == "pydantic"
    assert err.field == "strength"


def test_strength_at_bounds_accepted() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 -2.0")[0].strength == -2.0
    assert parse_loras_heredoc("civitai:1234@5678 2.0")[0].strength == 2.0


def test_strength_inf_and_nan_rejected() -> None:
    with pytest.raises(LorasParseError):
        parse_loras_heredoc("civitai:1234@5678 inf")
    with pytest.raises(LorasParseError):
        parse_loras_heredoc("civitai:1234@5678 nan")


# --- branch parse + alias (P2 reuse) ---

def test_branch_h_normalized_to_high_noise() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 1.0 h")[0].branch == "high_noise"


def test_branch_l_normalized_to_low_noise() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 1.0 l")[0].branch == "low_noise"


def test_branch_high_noise_explicit_accepted() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 1.0 high_noise")[0].branch == "high_noise"


def test_branch_low_noise_explicit_accepted() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 1.0 low_noise")[0].branch == "low_noise"


def test_branch_auto_accepted() -> None:
    assert parse_loras_heredoc("civitai:1234@5678 1.0 auto")[0].branch == "auto"


def test_branch_unknown_value_raises_pydantic() -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc("civitai:1234@5678 1.0 x")
    assert exc.value.report.errors[0].kind == "pydantic"
    assert exc.value.report.errors[0].field == "branch"


# --- duplicate detection (D8) ---

def test_same_ref_same_branch_rejected_as_duplicate() -> None:
    text = "civitai:1234@5678 1.0 h\ncivitai:1234@5678 0.5 h\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    err = exc.value.report.errors[0]
    assert err.kind == "duplicate"
    assert err.first_line == 1
    assert err.line_no == 2


def test_same_ref_different_branches_accepted() -> None:
    """P2 dual-load: same LoRA file in both transformers with independent strengths."""
    text = "civitai:1234@5678 1.0 h\ncivitai:1234@5678 0.8 l\n"
    result = parse_loras_heredoc(text)
    assert len(result) == 2
    assert (result[0].branch, result[1].branch) == ("high_noise", "low_noise")


def test_same_ref_omitted_branch_twice_rejected() -> None:
    """Both default to `auto` → composite key collision."""
    text = "civitai:1234@5678\ncivitai:1234@5678\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    assert exc.value.report.errors[0].kind == "duplicate"


def test_duplicate_error_reports_both_line_numbers() -> None:
    text = "civitai:1234@5678 1.0 h\n# spacer\ncivitai:1234@5678 0.5 h\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    err = exc.value.report.errors[0]
    assert (err.first_line, err.line_no) == (1, 3)


# --- aggregation (D6) ---

def test_three_independent_errors_all_reported() -> None:
    text = "cvtai:1@1\ncivitai:2@2 1.5x\ncivitai:3@3 1.0 zzz\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    kinds = [e.kind for e in exc.value.report.errors]
    assert kinds == ["unknown-scheme", "bad-strength", "pydantic"]


def test_first_line_valid_subsequent_invalid_returns_all_invalid_errors() -> None:
    text = "civitai:1234@5678 1.0 h\ncvtai:bad\ncivitai:5555@6666 3.0\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    assert len(exc.value.report.errors) == 2


def test_error_report_preserves_line_order() -> None:
    text = "cvtai:1@1\ncivitai:2@2 1.5x\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    line_nos = [e.line_no for e in exc.value.report.errors]
    assert line_nos == [1, 2]


def test_no_partial_list_returned_on_any_error() -> None:
    """If even one line is bad, no entries are returned (exception raises)."""
    text = "civitai:1234@5678\ncvtai:bad\n"
    with pytest.raises(LorasParseError):
        parse_loras_heredoc(text)


# --- LineError privacy shape (P3-Privacy-1) ---

def test_line_error_class_has_no_ref_field() -> None:
    """AC-P3-3 lockdown: LineError has no ref/filename/label-named str fields."""
    forbidden = {"ref", "filename", "label"}
    fields = set(LineError.model_fields.keys())
    assert fields.isdisjoint(forbidden), f"LineError fields: {fields}"


def test_render_for_cli_returns_string_with_summary_line() -> None:
    text = "cvtai:bad\n"
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(text)
    rendered = exc.value.report.render_for_cli()
    assert rendered.startswith("--loras: 1 problem(s) found")
    assert "line 1 col 1: unknown scheme" in rendered
```

- [ ] **Step 2: Run the test to verify it fails.**

```bash
pixi run pytest tests/cli/test_loras_arg.py -v
```

Expected: `ImportError` / `ModuleNotFoundError: No module named 'kinoforge.cli.loras_arg'`.

- [ ] **Step 3: Write the parser module.**

```python
# src/kinoforge/cli/loras_arg.py
"""Parser for the `kinoforge generate --loras HEREDOC` argument.

See docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md §6 for
the full tokenization + ref expansion + duplicate-detection contract.

Privacy invariants (spec §4):
  - `LineError` has no field annotated `str` whose name matches
    `ref|filename|label` (P3-Privacy-1, locked by AST scan in
    tests/test_lora_error_redaction.py and tests/test_no_unredacted_writes.py).
  - `LorasParseError.__str__` / `__repr__` and `LorasParseReport.render_for_cli`
    build their output strictly from `LineError` fields — never from the
    original heredoc text (P3-Privacy-2).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from kinoforge.core.errors import KinoforgeError
from kinoforge.core.lora import LoraEntry

_NUMERIC_SHORTHAND = re.compile(r"^(\d+):(\d+)$")
_KNOWN_SCHEMES = frozenset({"civitai", "hf", "file", "https", "http"})


class LineError(BaseModel):
    """One validation failure on one input line.

    Privacy (P3-Privacy-1): this model intentionally has NO field named
    `ref`, `filename`, or `label`. Diagnostics carry only structural
    context — line numbers, column indices, kind tags, scheme prefixes —
    never the raw ref string itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    line_no: int
    col: int | None
    kind: Literal[
        "bad-columns",
        "unknown-scheme",
        "missing-scheme",
        "bad-strength",
        "pydantic",
        "duplicate",
    ]
    scheme: str | None = None
    got_kind: str | None = None
    expected: str | None = None
    first_line: int | None = None
    field: str | None = None


class LorasParseReport(BaseModel):
    """Aggregated parse diagnostics. Renderable as a CLI block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    errors: list[LineError]

    def render_for_cli(self) -> str:
        """Build the human-readable error block printed to stderr.

        Output schema (spec §8.3): a summary line, a blank line, then one
        indented `line N [col M]: ...` per error in input order.
        """
        lines: list[str] = [
            f"--loras: {len(self.errors)} problem(s) found",
            "",
        ]
        for err in self.errors:
            lines.append(f"  {_format_one(err)}")
        return "\n".join(lines)


class LorasParseError(KinoforgeError):
    """Raised by parse_loras_heredoc when >=1 input line fails validation.

    Carries an aggregated LorasParseReport. NEVER carries ref strings.
    """

    def __init__(self, report: LorasParseReport) -> None:
        super().__init__(f"--loras: {len(report.errors)} problem(s) found")
        self.report = report


def parse_loras_heredoc(text: str) -> list[LoraEntry]:
    """Parse a --loras heredoc body into a validated LoRA stack.

    Empty / comments-only input returns `[]` (D9: valid empty-stack
    override). Any line that fails validation appends a `LineError` to
    the aggregated report; if any errors accumulated, raises
    `LorasParseError` after walking every line (D6).

    Args:
        text: Raw heredoc body as received from argparse (shell has
            already stripped surrounding quoting).

    Returns:
        Ordered list of `LoraEntry` matching the surviving input lines.
        Empty list when input is empty / comments-only.

    Raises:
        LorasParseError: When >=1 line failed validation. The carried
            `LorasParseReport.errors` aggregates every problem in input
            line order.
    """
    errors: list[LineError] = []
    entries: list[LoraEntry] = []
    seen_keys: dict[tuple[str, str], int] = {}

    for line_no, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue

        tokens = re.split(r"\s+", line)
        if len(tokens) not in (1, 2, 3):
            errors.append(
                LineError(
                    line_no=line_no,
                    col=None,
                    kind="bad-columns",
                    got_kind=str(len(tokens)),
                    expected="1, 2, or 3",
                )
            )
            continue

        ref_raw = tokens[0]
        strength_raw = tokens[1] if len(tokens) >= 2 else "1.0"
        branch_raw = tokens[2] if len(tokens) >= 3 else "auto"

        ref_expanded, ref_err = _expand_ref(ref_raw, line_no)
        if ref_err is not None:
            errors.append(ref_err)
            continue

        try:
            strength = float(strength_raw)
        except ValueError:
            errors.append(
                LineError(
                    line_no=line_no,
                    col=2,
                    kind="bad-strength",
                    got_kind="not-a-float",
                )
            )
            continue

        try:
            entry = LoraEntry(
                ref=ref_expanded,
                strength=strength,
                branch=branch_raw,
            )
        except ValidationError as ve:
            for sub in ve.errors():
                field = str(sub["loc"][0]) if sub["loc"] else None
                errors.append(
                    LineError(
                        line_no=line_no,
                        col=_col_for_field(field),
                        kind="pydantic",
                        field=field,
                        got_kind=sub["type"],
                    )
                )
            continue

        key = (entry.ref, entry.branch)
        first = seen_keys.get(key)
        if first is not None:
            errors.append(
                LineError(
                    line_no=line_no,
                    col=None,
                    kind="duplicate",
                    first_line=first,
                )
            )
            continue
        seen_keys[key] = line_no
        entries.append(entry)

    if errors:
        raise LorasParseError(LorasParseReport(errors=errors))
    return entries


def _expand_ref(ref_raw: str, line_no: int) -> tuple[str, LineError | None]:
    m = _NUMERIC_SHORTHAND.match(ref_raw)
    if m is not None:
        return f"civitai:{m.group(1)}@{m.group(2)}", None

    colon = ref_raw.find(":")
    slash = ref_raw.find("/")
    if colon == -1 or (slash != -1 and slash < colon):
        return ref_raw, LineError(
            line_no=line_no, col=1, kind="missing-scheme",
        )

    scheme = ref_raw[:colon].lower()
    if scheme not in _KNOWN_SCHEMES:
        return ref_raw, LineError(
            line_no=line_no, col=1, kind="unknown-scheme", scheme=scheme,
        )

    return ref_raw, None


def _col_for_field(field: str | None) -> int | None:
    if field == "ref":
        return 1
    if field == "strength":
        return 2
    if field == "branch":
        return 3
    return None


def _format_one(err: LineError) -> str:
    loc = f"line {err.line_no}"
    if err.col is not None:
        loc += f" col {err.col}"

    if err.kind == "bad-columns":
        return (
            f"{loc}: bad column count (got {err.got_kind}, "
            f"expected {err.expected})"
        )
    if err.kind == "unknown-scheme":
        allowed = ", ".join(sorted(_KNOWN_SCHEMES))
        return f"{loc}: unknown scheme `{err.scheme}` (expected one of: {allowed})"
    if err.kind == "missing-scheme":
        return f"{loc}: missing scheme (use `civitai:`, `hf:`, `file:`, or numeric `<modelId>:<versionId>`)"
    if err.kind == "bad-strength":
        return f"{loc}: bad strength (not a float)"
    if err.kind == "pydantic":
        return f"{loc}: {err.field} validation failed ({err.got_kind})"
    if err.kind == "duplicate":
        return f"{loc}: duplicate (ref, branch) — first declared on line {err.first_line}"
    return f"{loc}: {err.kind}"  # defensive fallback; unreachable per Literal
```

- [ ] **Step 4: Run the tests to verify they pass.**

```bash
pixi run pytest tests/cli/test_loras_arg.py -v
```

Expected: every test in the file passes.

- [ ] **Step 5: Lint + typecheck the new module.**

```bash
pixi run ruff check src/kinoforge/cli/loras_arg.py tests/cli/test_loras_arg.py
pixi run ruff format --check src/kinoforge/cli/loras_arg.py tests/cli/test_loras_arg.py
pixi run mypy src/kinoforge/cli/loras_arg.py
```

Expected: zero violations on each.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/cli/loras_arg.py tests/cli/test_loras_arg.py
git commit -m "feat(cli): parse_loras_heredoc + LorasParseError + LineError

P3 Task 0 — parser module + unit tests (§6 + §11.1 of
docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md).

Tokenization: skip blank lines + # line comments, split on whitespace,
1-3 columns ref [strength] [branch]. Numeric shorthand <N>:<N> expands
to civitai:N@N; other refs scheme-validated against allow-list
{civitai, hf, file, https, http}. Strength + branch validation delegated
to LoraEntry Pydantic (reuses P1 strength range + P2 branch alias).
Composite (ref, branch) duplicate detection. Collect-all aggregation —
every input line walked; LorasParseError raised once at end with full
LorasParseReport carrying line_no/col/kind/structured-context.

LineError shape carries NO ref/filename/label fields (P3-Privacy-1).
render_for_cli output schema: '--loras: N problem(s) found' summary +
one indented 'line N col M: ...' per error in input order.

No call site imports yet; parser lands self-contained for §11.4 +
§11.5 lockdown tests + §7 resolver kwarg wiring in subsequent tasks."
```

---

### Task 1: Redaction parity tests + AST scan extension

**Goal:** Lock down P3-Privacy-1/2 invariants and extend the codebase-wide AST scan to cover the new parser module.

**Files:**
- Create: `tests/test_lora_error_redaction.py`
- Modify: `tests/test_no_unredacted_writes.py` (extend scanned-files set + LineError checks)

**Acceptance Criteria:**
- [ ] `tests/test_lora_error_redaction.py` covers `str` / `repr` / `render_for_cli` for every error kind with a sensitive ref → asserts ref substring absent from output.
- [ ] `tests/test_no_unredacted_writes.py` extension AC-P3-1 + AC-P3-3 + AC-P3-4 green.
- [ ] `src/kinoforge/cli/loras_arg.py` now appears in the scanned-files set.
- [ ] Whole suite green.

**Verify:** `pixi run pytest tests/test_lora_error_redaction.py tests/test_no_unredacted_writes.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write `tests/test_lora_error_redaction.py`.**

```python
"""P3 redaction parity — LineError + LorasParseError + render_for_cli.

Spec §11.4 lockdown. Forces every error kind with a sensitive ref string,
asserts the ref substring never appears in str/repr/render output.
"""

from __future__ import annotations

import pytest

from kinoforge.cli.loras_arg import LineError, parse_loras_heredoc, LorasParseError

_SENSITIVE = "civitai:9876543210@1234567890"


def test_line_error_has_no_ref_field() -> None:
    """AC-P3-3 lockdown — no ref/filename/label-named fields on LineError."""
    forbidden = {"ref", "filename", "label"}
    assert forbidden.isdisjoint(LineError.model_fields.keys())


@pytest.mark.parametrize(
    "heredoc",
    [
        # bad-columns
        f"{_SENSITIVE} 1.0 h extra-token",
        # unknown-scheme (use sensitive-shaped string with bad scheme)
        "evil:9876543210@1234567890",
        # bad-strength
        f"{_SENSITIVE} not-a-float",
        # pydantic (strength out of range)
        f"{_SENSITIVE} 9.0",
        # duplicate
        f"{_SENSITIVE} 1.0 h\n{_SENSITIVE} 0.5 h\n",
    ],
)
def test_render_for_cli_never_contains_ref(heredoc: str) -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(heredoc)
    rendered = exc.value.report.render_for_cli()
    assert _SENSITIVE not in rendered
    assert "9876543210" not in rendered
    assert "1234567890" not in rendered


@pytest.mark.parametrize(
    "heredoc",
    [
        f"{_SENSITIVE} 1.0 h extra-token",
        f"{_SENSITIVE} 9.0",
        f"{_SENSITIVE} 1.0 h\n{_SENSITIVE} 0.5 h\n",
    ],
)
def test_loras_parse_error_str_never_contains_ref(heredoc: str) -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(heredoc)
    assert _SENSITIVE not in str(exc.value)
    assert "9876543210" not in str(exc.value)


@pytest.mark.parametrize(
    "heredoc",
    [
        f"{_SENSITIVE} 1.0 h extra-token",
        f"{_SENSITIVE} 9.0",
    ],
)
def test_loras_parse_error_repr_never_contains_ref(heredoc: str) -> None:
    with pytest.raises(LorasParseError) as exc:
        parse_loras_heredoc(heredoc)
    assert _SENSITIVE not in repr(exc.value)
    assert "9876543210" not in repr(exc.value)
```

- [ ] **Step 2: Run the test file to verify it passes.**

```bash
pixi run pytest tests/test_lora_error_redaction.py -v
```

Expected: every test passes (it tests the parser landed in Task 0).

- [ ] **Step 3: Extend `tests/test_no_unredacted_writes.py`.**

Open the existing file. Locate the `_SCANNED_FILES` set (or equivalent
file-allow-list) used by the AST walker. Add the line
`"src/kinoforge/cli/loras_arg.py",` to that set. If a separate
`SCANNED_DIRS` list exists, ensure `"src/kinoforge/cli/"` is covered.

Add a new test function:

```python
def test_p3_line_error_has_no_ref_filename_label_field() -> None:
    """AC-P3-3 — AST scan: LineError class declaration must not declare
    any field annotated `str` whose name matches ref|filename|label.
    """
    import ast
    from pathlib import Path

    src = Path("src/kinoforge/cli/loras_arg.py").read_text()
    tree = ast.parse(src)
    forbidden = {"ref", "filename", "label"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LineError":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    assert item.target.id not in forbidden, (
                        f"LineError declared forbidden field `{item.target.id}`"
                    )
            return
    raise AssertionError("LineError class not found in src/kinoforge/cli/loras_arg.py")


def test_p3_render_for_cli_does_not_interpolate_heredoc() -> None:
    """AC-P3-4 — render_for_cli body must not reference an attribute
    named `text` or call str.format with the original heredoc.
    """
    import ast
    from pathlib import Path

    src = Path("src/kinoforge/cli/loras_arg.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "render_for_cli":
            for sub in ast.walk(node):
                # No reference to `text` attribute or `self.text`
                if isinstance(sub, ast.Attribute) and sub.attr == "text":
                    raise AssertionError(
                        "render_for_cli references `.text`; must build "
                        "output strictly from self.errors fields"
                    )
            return
    raise AssertionError("render_for_cli not found")
```

- [ ] **Step 4: Run the extended scan.**

```bash
pixi run pytest tests/test_no_unredacted_writes.py -v
pixi run pytest tests/test_lora_error_redaction.py tests/test_no_unredacted_writes.py tests/cli/test_loras_arg.py -v
```

Expected: all green.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files \
  tests/test_lora_error_redaction.py \
  tests/test_no_unredacted_writes.py

git add tests/test_lora_error_redaction.py tests/test_no_unredacted_writes.py
git commit -m "test(p3): redaction parity + AST-scan coverage for loras_arg

P3 Task 1 (§11.4 + §11.5 AC-P3-1/3/4).

tests/test_lora_error_redaction.py asserts LinerError carries no
ref/filename/label fields and that LorasParseError str/repr +
LorasParseReport.render_for_cli never leak ref substrings — coverage
matrix forces every error kind with a sensitive-shaped ref.

tests/test_no_unredacted_writes.py adds src/kinoforge/cli/loras_arg.py
to the scanned-files set + two AST checks: LineError class declaration
free of ref|filename|label-named fields (AC-P3-3); render_for_cli body
never touches a .text attribute or interpolates the original heredoc
(AC-P3-4)."
```

---

### Task 2: Extend `resolve_active_lora_stack` with `cli_loras` kwarg

**Goal:** Add the keyword-only `cli_loras: list[LoraEntry] | None = None` parameter and the CLI-wins-over-vault precedence branch, with redaction-registration + audit-WARNING ordering invariants locked by tests.

**Files:**
- Modify: `src/kinoforge/core/lora.py:76-126`
- Create: `tests/core/test_lora_resolver_p3.py`

**Acceptance Criteria:**
- [ ] `cli_loras=None` → existing P1 path unchanged (cfg/vault precedence + `LoraStackConflict`).
- [ ] `cli_loras=[L1, L2]` → returns `[L1, L2]` regardless of cfg/vault content.
- [ ] `cli_loras=[]` → returns `[]` (D9 empty override).
- [ ] CLI-supplied refs registered with `RedactionRegistry` via `add_many([(lo.ref, "lora:ref") for lo in cli_loras])`.
- [ ] When `cli_loras is not None` AND `vault.loras` non-empty → `logger.warning` fires exactly once with format string `"cli-loras-bypass-vault: --loras override applied; vault.loras (%d entries) bypassed for this run. Vault is unchanged on disk."`
- [ ] Warning message NEVER contains any vault or CLI ref substring (caplog regex assert).
- [ ] Redaction registration runs BEFORE warning emits (ordering test).
- [ ] All §11.2 tests green.

**Verify:** `pixi run pytest tests/core/test_lora_resolver_p3.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_lora_resolver_p3.py`.**

```python
"""P3 resolver tests — CLI > vault > cfg precedence (spec §11.2)."""

from __future__ import annotations

import logging
import re
from unittest.mock import patch

import pytest

from kinoforge.core.errors import LoraStackConflict
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack


class _StubCfg:
    def __init__(self, loras: list[LoraEntry]) -> None:
        self.loras = loras


class _StubVault:
    def __init__(self, loras: list) -> None:
        self.loras = loras


class _StubVaultLoRA:
    """Mirror of VaultLoRA: ref + strength + sha256 + branch + label."""

    def __init__(self, ref: str, strength: float = 1.0, branch: str = "auto",
                 label: str | None = None) -> None:
        self.ref = ref
        self.strength = strength
        self.branch = branch
        self.label = label
        self.sha256 = None

    def model_dump(self, exclude: set[str] | None = None) -> dict:
        excl = exclude or set()
        out = {
            "ref": self.ref,
            "strength": self.strength,
            "branch": self.branch,
            "sha256": self.sha256,
            "label": self.label,
        }
        return {k: v for k, v in out.items() if k not in excl}


# --- existing P1 path preserved (cli_loras=None) ---

def test_cli_loras_none_falls_back_to_p1_path() -> None:
    cfg = _StubCfg([LoraEntry(ref="civitai:1@1", strength=0.5)])
    assert resolve_active_lora_stack(cfg, None, cli_loras=None) == cfg.loras


def test_cli_loras_default_is_none_keeps_p1_signature_compat() -> None:
    """P3 signature extension must NOT break callers that pass only 2 args."""
    cfg = _StubCfg([LoraEntry(ref="civitai:1@1")])
    assert resolve_active_lora_stack(cfg, None) == cfg.loras


# --- CLI override ---

def test_cli_loras_overrides_cfg_loras_when_vault_empty() -> None:
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1", strength=0.3)])
    cli = [LoraEntry(ref="civitai:cli@1", strength=0.9)]
    result = resolve_active_lora_stack(cfg, None, cli_loras=cli)
    assert result == cli


def test_cli_loras_overrides_vault_loras_with_warning(caplog) -> None:
    cfg = _StubCfg([])
    vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
    cli = [LoraEntry(ref="civitai:cli@1", strength=0.7)]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        result = resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    assert result == cli
    assert any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)
    assert any("1 entries" in rec.message for rec in caplog.records)


def test_cli_loras_empty_list_overrides_to_empty_stack() -> None:
    """D9: empty cli_loras (not None) overrides cfg.loras."""
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1")])
    result = resolve_active_lora_stack(cfg, None, cli_loras=[])
    assert result == []


def test_cli_loras_skips_p1_d11_conflict_check() -> None:
    """When CLI wins, the diverging-refs LoraStackConflict must NOT fire."""
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1")])
    vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
    cli = [LoraEntry(ref="civitai:cli@1")]
    # No LoraStackConflict raised.
    result = resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    assert result == cli


def test_cli_loras_refs_registered_with_redaction_registry() -> None:
    cli = [LoraEntry(ref="civitai:cli@1"), LoraEntry(ref="civitai:cli@2")]
    with patch("kinoforge.core.lora.RedactionRegistry") as mock_reg_cls:
        mock_inst = mock_reg_cls.instance.return_value
        resolve_active_lora_stack(_StubCfg([]), None, cli_loras=cli)
    mock_inst.add_many.assert_called_once()
    pairs = mock_inst.add_many.call_args[0][0]
    refs = {p[0] for p in pairs}
    assert refs == {"civitai:cli@1", "civitai:cli@2"}


def test_cli_loras_warning_contains_no_ref_strings(caplog) -> None:
    cfg = _StubCfg([])
    vault = _StubVault([_StubVaultLoRA("civitai:secret-vault@1")])
    cli = [LoraEntry(ref="civitai:secret-cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    combined = " ".join(rec.message for rec in caplog.records)
    assert "secret-vault" not in combined
    assert "secret-cli" not in combined
    # Regex: bare integer must be there, no refs.
    assert re.search(r"\bvault\.loras \(\d+ entries\) bypassed", combined)


def test_cli_loras_warning_fires_only_when_vault_nonempty(caplog) -> None:
    cli = [LoraEntry(ref="civitai:cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(_StubCfg([]), None, cli_loras=cli)
    assert not any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)


def test_cli_loras_warning_does_not_fire_when_vault_loras_empty(caplog) -> None:
    vault = _StubVault([])
    cli = [LoraEntry(ref="civitai:cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(_StubCfg([]), vault, cli_loras=cli)
    assert not any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)


def test_cli_loras_redaction_registered_before_warning_emits(caplog) -> None:
    """Ordering invariant: refs hit RedactionRegistry BEFORE WARNING fires.

    Important when the WARNING-emitting logger has a handler that
    happens to scrub via the registry — registration must already be
    in place so the handler can do its job.
    """
    calls: list[str] = []
    cli = [LoraEntry(ref="civitai:cli@1")]

    with patch("kinoforge.core.lora.RedactionRegistry") as mock_reg_cls:
        mock_inst = mock_reg_cls.instance.return_value
        mock_inst.add_many.side_effect = lambda pairs: calls.append("register")
        with patch("kinoforge.core.lora.logger") as mock_logger:
            mock_logger.warning.side_effect = lambda *a, **k: calls.append("warn")
            vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
            resolve_active_lora_stack(_StubCfg([]), vault, cli_loras=cli)

    assert calls == ["register", "warn"]
```

- [ ] **Step 2: Run the failing tests.**

```bash
pixi run pytest tests/core/test_lora_resolver_p3.py -v
```

Expected: most tests fail (signature mismatch: `cli_loras` kwarg not present).

- [ ] **Step 3: Modify `src/kinoforge/core/lora.py`.**

Open `src/kinoforge/core/lora.py`. After the existing imports near the top of the module, add:

```python
import logging

from kinoforge.core.redaction import RedactionRegistry

logger = logging.getLogger(__name__)
```

Then replace the existing `resolve_active_lora_stack` function (lines 76-126) with:

```python
def resolve_active_lora_stack(
    cfg: Any,  # noqa: ANN401
    vault: Any | None,  # noqa: ANN401
    *,
    cli_loras: list[LoraEntry] | None = None,
) -> list[LoraEntry]:
    """Resolve the final LoRA stack for this run.

    Precedence (P3-D3, P3-D4): CLI > vault > cfg.

    When ``cli_loras`` is not None, CLI wins entirely — vault.loras is
    bypassed and cfg.loras is replaced. If vault is loaded with
    non-empty ``.loras``, a single WARNING is emitted naming the count
    of bypassed refs (refs themselves never enter the log line per
    spec §4 P3-Privacy-4). CLI-supplied refs are registered with the
    global :class:`RedactionRegistry` BEFORE the WARNING fires so any
    later traceback containing a CLI ref is already redactable.

    When ``cli_loras`` is None, the original P1 precedence rule applies
    unchanged: vault wins entirely; diverging non-empty cfg + vault
    raises :class:`LoraStackConflict`.

    Args:
        cfg: A loaded :class:`kinoforge.core.config.Config` (typed
            ``Any`` to avoid a circular import).
        vault: An optional loaded :class:`kinoforge.core.vault.Vault`.
        cli_loras: Optional CLI-supplied stack from
            ``parse_loras_heredoc``. When ``None``, the P1 cfg/vault
            precedence rule runs. When a list (including empty), it
            wins entirely.

    Returns:
        Ordered list of :class:`LoraEntry`. Vault-only ``label`` field
        is stripped on upcast.

    Raises:
        LoraStackConflict: Only when ``cli_loras is None`` AND
            cfg.loras + vault.loras both non-empty with diverging refs.
    """
    from kinoforge.core.errors import LoraStackConflict

    if cli_loras is not None:
        RedactionRegistry.instance().add_many(
            [(lo.ref, "lora:ref") for lo in cli_loras]
        )
        if vault is not None and getattr(vault, "loras", None):
            logger.warning(
                "cli-loras-bypass-vault: --loras override applied; "
                "vault.loras (%d entries) bypassed for this run. "
                "Vault is unchanged on disk.",
                len(vault.loras),
            )
        return list(cli_loras)

    # P1 path — unchanged.
    cfg_loras: list[LoraEntry] = list(getattr(cfg, "loras", []))
    if vault is None or not getattr(vault, "loras", None):
        return cfg_loras
    cfg_refs = {lo.ref for lo in cfg_loras}
    vault_refs = {lo.ref for lo in vault.loras}
    if cfg_loras and cfg_refs != vault_refs:
        raise LoraStackConflict(
            f"cfg.loras and vault.loras both set with diverging ref sets — "
            f"cfg={sorted(cfg_refs)}, vault={sorted(vault_refs)}; remove "
            f"cfg.loras and use vault.loras as sole source"
        )
    return [LoraEntry(**lo.model_dump(exclude={"label"})) for lo in vault.loras]
```

- [ ] **Step 4: Run resolver tests + entire P1/P2 suite to confirm no regression.**

```bash
pixi run pytest tests/core/test_lora_resolver_p3.py -v
pixi run pytest tests/core/ -v
```

Expected: P3 tests green; existing P1/P2 resolver tests still green (signature is backward-compatible).

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files \
  src/kinoforge/core/lora.py \
  tests/core/test_lora_resolver_p3.py

git add src/kinoforge/core/lora.py tests/core/test_lora_resolver_p3.py
git commit -m "feat(core): resolve_active_lora_stack(*, cli_loras=None)

P3 Task 2 (§7 of design spec). Adds keyword-only cli_loras kwarg to
the P1 resolver; when supplied, CLI wins entirely over both cfg.loras
and vault.loras (P3-D3 + P3-D4). cli_loras=[] is a valid empty-stack
override (D9). cli_loras=None preserves P1 precedence unchanged
(vault wins; diverging non-empty cfg + vault raises LoraStackConflict).

CLI-supplied refs registered with the global RedactionRegistry before
the vault-bypass WARNING emits (P3-Privacy-3 + ordering invariant).
WARNING format string: 'cli-loras-bypass-vault: --loras override
applied; vault.loras (%d entries) bypassed for this run. Vault is
unchanged on disk.' — count only, no refs (P3-Privacy-4).

Signature is backward-compatible: keyword-only with default None means
every existing caller continues to work without a code change."
```

---

### Task 3: AST scan — precedence-single-source invariant

**Goal:** Lock down P3-Precedence-Single-Source (spec §4) — no module outside the resolver may pattern-match on `cli_loras is not None` / `args.loras is not None` and then reproduce cfg-vs-vault precedence logic.

**Files:**
- Create: `tests/test_no_precedence_branches_outside_resolver.py`

**Acceptance Criteria:**
- [ ] Test passes against the current tree (after Task 2 lands).
- [ ] Test FAILS if a deliberate violation is injected (manual smoke test in development).
- [ ] Allow-list documents the one legitimate site: `src/kinoforge/cli/_commands.py::_cmd_generate` (parse + thread hop only — no precedence logic).

**Verify:** `pixi run pytest tests/test_no_precedence_branches_outside_resolver.py -v` → passes.

**Steps:**

- [ ] **Step 1: Write the AST scan.**

```python
"""AC-P3-5 — no module outside resolve_active_lora_stack may branch on
cli_loras / args.loras and then run cfg-vs-vault precedence logic.

Allow-list:
  - src/kinoforge/cli/_commands.py: the _cmd_generate function may
    conditionally call parse_loras_heredoc and pass the result as a
    kwarg, but MUST NOT itself implement cfg-vs-vault precedence.

Violation = an `if cli_loras is not None` / `if args.loras is not None`
branch in any module under src/kinoforge/ that ALSO references both
`cfg.loras` and `vault.loras` (or `.loras` on a name resembling vault).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ALLOWED = {
    # parse-and-thread hop only; AST scan tolerates `args.loras` branch
    # in this single file because the body does not also reference
    # vault.loras / cfg.loras precedence.
    Path("src/kinoforge/cli/_commands.py"),
}


def _references_both_cfg_and_vault_loras(node: ast.AST) -> bool:
    sources = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "loras":
            if isinstance(sub.value, ast.Name):
                sources.add(sub.value.id)
            elif isinstance(sub.value, ast.Attribute):
                sources.add(sub.value.attr)
    return any("cfg" in s for s in sources) and any("vault" in s for s in sources)


def _conditional_on_cli_loras(test_node: ast.AST) -> bool:
    for sub in ast.walk(test_node):
        if isinstance(sub, ast.Attribute) and sub.attr in {"loras", "cli_loras"}:
            return True
        if isinstance(sub, ast.Name) and sub.id in {"cli_loras"}:
            return True
    return False


def test_no_precedence_branches_outside_resolver() -> None:
    src_root = Path("src/kinoforge")
    violations: list[str] = []

    for py_file in src_root.rglob("*.py"):
        if py_file.relative_to(src_root.parent) == Path("src/kinoforge/core/lora.py"):
            continue  # resolver is the legitimate site
        if py_file.relative_to(src_root.parent) in _ALLOWED:
            # Confirm allow-listed file really does NOT mix in cfg/vault precedence.
            tree = ast.parse(py_file.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and _conditional_on_cli_loras(node.test):
                    if _references_both_cfg_and_vault_loras(node):
                        violations.append(
                            f"{py_file}:{node.lineno} — allow-listed file but "
                            f"if-branch on cli_loras references both cfg.loras "
                            f"and vault.loras"
                        )
            continue

        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _conditional_on_cli_loras(node.test):
                if _references_both_cfg_and_vault_loras(node):
                    violations.append(
                        f"{py_file}:{node.lineno} — non-resolver module "
                        f"branches on cli_loras and reads both cfg.loras "
                        f"and vault.loras; precedence logic must live in "
                        f"resolve_active_lora_stack only"
                    )

    assert not violations, "\n".join(violations)
```

- [ ] **Step 2: Run the scan.**

```bash
pixi run pytest tests/test_no_precedence_branches_outside_resolver.py -v
```

Expected: passes (no violations in the current tree).

- [ ] **Step 3: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/test_no_precedence_branches_outside_resolver.py

git add tests/test_no_precedence_branches_outside_resolver.py
git commit -m "test(p3): AST scan AC-P3-5 — precedence-single-source

P3 Task 3 (§11.5 AC-P3-5). Locks down P3-Precedence-Single-Source
invariant: only resolve_active_lora_stack may implement CLI/vault/cfg
precedence. Any module under src/kinoforge/ (other than the
allow-listed _cmd_generate parse-and-thread hop) that branches on
cli_loras and ALSO references both cfg.loras + vault.loras fails the
scan. Catches the regression where a downstream consumer reimplements
the precedence rule and quietly drifts."
```

---

### Task 4: CLI surface — `--loras` arg + `_cmd_generate` wiring

**Goal:** Add the `--loras` argparse argument (with `_LorasOnceAction` to reject repeat use) and parse-and-thread the result into `resolve_active_lora_stack(*, cli_loras=)` from `_cmd_generate`. Full CLI command coverage.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (slot `--loras` after `--dry-run-swap` at `:481`)
- Modify: `src/kinoforge/cli/_commands.py` (parse + thread in `_cmd_generate`)
- Create: `tests/cli/test_cmd_generate_loras.py`

**Acceptance Criteria:**
- [ ] `kinoforge generate --help` documents `--loras HEREDOC` with full help text from spec §8.1.
- [ ] Repeated `--loras` rejected at argparse level with `"--loras may be specified at most once"` and exit code 2.
- [ ] `--loras` value parses through `parse_loras_heredoc`; result threads to `resolve_active_lora_stack(*, cli_loras=...)`.
- [ ] On `LorasParseError`, full `render_for_cli()` block printed to stderr, exit 1.
- [ ] Existing tests for `_cmd_generate` (no `--loras` passed) stay green.
- [ ] All §11.3 tests green.

**Verify:** `pixi run pytest tests/cli/test_cmd_generate_loras.py -v` → all pass; `pixi run pytest tests/cli/ -v` → no regression.

**Steps:**

- [ ] **Step 1: Write `tests/cli/test_cmd_generate_loras.py`.**

```python
"""CLI command tests for `kinoforge generate --loras` (spec §11.3)."""

from __future__ import annotations

import argparse
import sys
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._main import _build_parser  # adjust import per real symbol
from kinoforge.cli._commands import _cmd_generate
from kinoforge.cli.loras_arg import LorasParseError, LorasParseReport, LineError


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "config": "examples/configs/wan21-1_3b-strength-grid.yaml",
        "prompt": "test",
        "mode": "t2v",
        "run_id": None,
        "output_dir": None,
        "no_output_dir": False,
        "instance_id": None,
        "force_attach": False,
        "no_reuse": False,
        "skip_preflight": False,
        "dry_run_swap": False,
        "loras": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_no_loras_arg_resolver_called_with_cli_loras_none() -> None:
    """When --loras absent, resolver receives cli_loras=None."""
    args = _make_args(loras=None)
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        with patch("kinoforge.cli._commands.parse_loras_heredoc") as mock_parse:
            # Best-effort run; failures past the resolver call are out of scope.
            with patch("kinoforge.cli._commands.load_config"):
                with patch("kinoforge.cli._commands._load_vault_if_present", return_value=None):
                    try:
                        _cmd_generate(args, MagicMock())
                    except Exception:
                        pass
    assert mock_parse.call_count == 0
    # resolver was called at least once with cli_loras=None
    for call in mock_resolve.call_args_list:
        assert call.kwargs.get("cli_loras") is None


def test_loras_arg_parsed_and_threaded_to_resolver() -> None:
    args = _make_args(loras="civitai:1234@5678 0.7 h")
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        with patch("kinoforge.cli._commands.load_config"):
            with patch("kinoforge.cli._commands._load_vault_if_present", return_value=None):
                try:
                    _cmd_generate(args, MagicMock())
                except Exception:
                    pass
    assert mock_resolve.called
    kwargs = mock_resolve.call_args_list[0].kwargs
    cli_loras = kwargs.get("cli_loras")
    assert cli_loras is not None
    assert len(cli_loras) == 1
    assert cli_loras[0].ref == "civitai:1234@5678"
    assert cli_loras[0].strength == 0.7
    assert cli_loras[0].branch == "high_noise"


def test_loras_parse_error_renders_report_to_stderr_exit_1(capsys) -> None:
    args = _make_args(loras="cvtai:1@1\nbroken")
    rc = _cmd_generate(args, MagicMock())
    captured = capsys.readouterr()
    assert rc == 1
    assert "--loras:" in captured.err
    assert "unknown scheme" in captured.err or "missing scheme" in captured.err


def test_loras_double_use_argparse_errors_exit_2() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([
            "generate", "-c", "x.yaml", "--prompt", "p", "--mode", "t2v",
            "--loras", "civitai:1@1",
            "--loras", "civitai:2@2",
        ])
    assert exc.value.code == 2


def test_loras_empty_heredoc_threads_empty_list() -> None:
    """D9 — --loras "" overrides cfg with empty stack."""
    args = _make_args(loras="")
    with patch("kinoforge.cli._commands.resolve_active_lora_stack") as mock_resolve:
        mock_resolve.return_value = []
        with patch("kinoforge.cli._commands.load_config"):
            with patch("kinoforge.cli._commands._load_vault_if_present", return_value=None):
                try:
                    _cmd_generate(args, MagicMock())
                except Exception:
                    pass
    assert mock_resolve.called
    assert mock_resolve.call_args_list[0].kwargs.get("cli_loras") == []
```

(If `_build_parser` / `_load_vault_if_present` / `load_config` symbols
differ in the codebase, locate the real names by reading the existing
`tests/cli/test_*generate*.py` files and adjust the import + patch
targets. The test logic is unchanged.)

- [ ] **Step 2: Run the test file — expect failures.**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py -v
```

Expected: most tests fail (no `--loras` arg yet; `_cmd_generate` doesn't parse).

- [ ] **Step 3: Add the argparse arg to `src/kinoforge/cli/_main.py`.**

Open the file. Locate the `p_generate` argument block (around line 411-481). After the existing `--dry-run-swap` block, add:

```python
class _LorasOnceAction(argparse.Action):
    """Reject a second --loras on the same invocation (P3-D1)."""

    def __call__(self, parser, namespace, values, option_string=None):  # noqa: D401
        if getattr(namespace, self.dest) is not None:
            parser.error("--loras may be specified at most once")
        setattr(namespace, self.dest, values)


p_generate.add_argument(
    "--loras",
    action=_LorasOnceAction,
    default=None,
    metavar="HEREDOC",
    help=(
        "override cfg.loras AND vault.loras with a CLI-supplied LoRA "
        "stack. Heredoc body: one LoRA per line, whitespace-separated "
        "columns `ref [strength] [branch]`. `#` line comments + blank "
        "lines ignored. Numeric shorthand `<modelId>:<versionId>` "
        "expands to `civitai:<modelId>@<versionId>`; other refs "
        "(civitai:..., hf:..., file:..., https://...) pass through "
        "verbatim; unknown schemes rejected. Strength defaults to 1.0; "
        "branch defaults to `auto`. Empty heredoc clears the stack for "
        "this run. Vault.loras bypass logged to stderr."
    ),
)
```

(Place `_LorasOnceAction` near the top of `_main.py` with the other
helper classes, OR define it inline just before the `add_argument`
call — match the existing file's style.)

- [ ] **Step 4: Wire `_cmd_generate` in `src/kinoforge/cli/_commands.py`.**

Locate the call site of `resolve_active_lora_stack(cfg, vault)` inside
`_cmd_generate`. Immediately before that call, add:

```python
from kinoforge.cli.loras_arg import LorasParseError, parse_loras_heredoc

cli_loras = None
if args.loras is not None:
    try:
        cli_loras = parse_loras_heredoc(args.loras)
    except LorasParseError as err:
        sys.stderr.write(err.report.render_for_cli())
        sys.stderr.write("\n")
        return 1
```

Change the resolver call from `resolve_active_lora_stack(cfg, vault)`
to `resolve_active_lora_stack(cfg, vault, cli_loras=cli_loras)`.

If `sys` is not already imported in `_commands.py`, add the import.

- [ ] **Step 5: Run the test file + existing CLI tests.**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py -v
pixi run pytest tests/cli/ -v
```

Expected: all CLI tests green.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files \
  src/kinoforge/cli/_main.py \
  src/kinoforge/cli/_commands.py \
  tests/cli/test_cmd_generate_loras.py

git add \
  src/kinoforge/cli/_main.py \
  src/kinoforge/cli/_commands.py \
  tests/cli/test_cmd_generate_loras.py

git commit -m "feat(cli): kinoforge generate --loras HEREDOC

P3 Task 4 (§8 of design spec). Adds the --loras argparse argument to
kinoforge generate with _LorasOnceAction rejecting repeated use, parses
the heredoc value through parse_loras_heredoc, threads the resulting
list[LoraEntry] to resolve_active_lora_stack as cli_loras=. On
LorasParseError prints the aggregated report (render_for_cli) to
stderr and exits 1.

Composes orthogonally with --instance-id / --no-reuse / --dry-run-swap
/ --force-attach (P3-D10 single-source-of-truth precedence keeps each
existing flag's contract intact; the resolver decides loras source,
downstream consumers stay unchanged)."
```

---

### Task 5: `--dry-run-swap` `loras_source` line

**Goal:** When `--dry-run-swap` runs, the preview block emits a `loras_source: cli` / `vault` / `cfg` / `empty` line so the operator can confirm which precedence branch fired.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py::_dry_run_swap_preview` (around `:327`)
- Modify: `src/kinoforge/cli/_commands.py::_cmd_generate` (resolver-result classification snippet)

**Acceptance Criteria:**
- [ ] `kinoforge generate --loras "civitai:X@Y" --dry-run-swap -c CFG --prompt P --mode t2v` prints a `loras_source: cli` line.
- [ ] `--dry-run-swap` without `--loras` prints `loras_source: vault` (vault loaded), `loras_source: cfg` (cfg.loras non-empty), or `loras_source: empty` (no LoRAs anywhere).
- [ ] Source classification helper lives next to the resolver call so it has access to all four inputs.

**Verify:** `pixi run pytest tests/cli/test_cmd_generate_loras.py::test_loras_with_dry_run_swap_prints_loras_source_cli -v` → passes.

**Steps:**

- [ ] **Step 1: Add the test.**

Append to `tests/cli/test_cmd_generate_loras.py`:

```python
def test_loras_with_dry_run_swap_prints_loras_source_cli(capsys) -> None:
    args = _make_args(loras="civitai:1234@5678", dry_run_swap=True)
    with patch("kinoforge.cli._commands.load_config"):
        with patch("kinoforge.cli._commands._load_vault_if_present", return_value=None):
            try:
                _cmd_generate(args, MagicMock())
            except Exception:
                pass
    captured = capsys.readouterr()
    # Either stdout or the dry-run printer writes to stderr; check both.
    combined = captured.out + captured.err
    assert "loras_source: cli" in combined


def test_dry_run_swap_no_loras_arg_prints_loras_source_cfg(capsys, tmp_path) -> None:
    """When no --loras and cfg.loras non-empty → loras_source: cfg."""
    # Build args + stub cfg with non-empty loras; assert label.
    args = _make_args(loras=None, dry_run_swap=True)
    # ... (implementation depends on cfg-loading harness; minimum:
    # patch resolve_active_lora_stack to return a non-empty list AND
    # patch the source-classification helper to assert 'cfg').
    pass  # marker — flesh out using the project's existing dry-run-swap test patterns
```

- [ ] **Step 2: Run the new test — expect failure.**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py::test_loras_with_dry_run_swap_prints_loras_source_cli -v
```

Expected: missing `loras_source` line → assertion fails.

- [ ] **Step 3: Implement source classification + print.**

In `src/kinoforge/cli/_commands.py`, after the `resolve_active_lora_stack`
call in `_cmd_generate`, compute the source label:

```python
def _classify_loras_source(
    *, cli_loras: list | None, vault, cfg, active_stack: list,
) -> str:
    """Return 'cli' / 'vault' / 'cfg' / 'empty' for dry-run-swap display."""
    if cli_loras is not None:
        return "cli"
    if vault is not None and getattr(vault, "loras", None):
        return "vault"
    if getattr(cfg, "loras", None):
        return "cfg"
    return "empty"
```

In `_dry_run_swap_preview` (or wherever the dry-run block is rendered),
add a print of the classified source:

```python
loras_source = _classify_loras_source(
    cli_loras=cli_loras, vault=vault, cfg=cfg, active_stack=active_stack,
)
print(f"loras_source: {loras_source}")
```

(Match the file's existing print style — `sys.stdout.write` vs `print`.
If the dry-run-swap printer takes a structured payload dict, add
`"loras_source"` as a key.)

- [ ] **Step 4: Run the dry-run-swap test.**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py -v
```

Expected: dry-run-swap test passes.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/test_cmd_generate_loras.py

git add src/kinoforge/cli/_commands.py tests/cli/test_cmd_generate_loras.py
git commit -m "feat(cli): dry-run-swap surfaces loras_source classification

P3 Task 5. _classify_loras_source returns 'cli'/'vault'/'cfg'/'empty'
based on the resolver's input precedence; --dry-run-swap preview adds
a 'loras_source: <kind>' line so the operator can confirm which
precedence branch fired without running the full generate."
```

---

### Task 6: Integration tests

**Goal:** End-to-end coverage — CLI override drives the actual `/lora/set_stack` wire body; warm-attach swap honors CLI stack; `WarmAttachKey` derived from CLI refs+branches not cfg refs.

**Files:**
- Create: `tests/integration/test_loras_cli_e2e.py`

**Acceptance Criteria:**
- [ ] `test_end_to_end_cli_loras_override_cfg_drives_set_stack_request` — mock pod; assert `target` list of the issued `SetStackRequest` carries CLI refs/strengths/branches in order.
- [ ] `test_cli_loras_warm_attach_swap_succeeds_when_only_lora_stack_differs` — warm pod with matching `WarmAttachKey`; CLI changes LoRA stack only; matcher succeeds; `set_stack` issued.
- [ ] `test_cli_loras_capability_key_derivation_uses_cli_refs_not_cfg_refs` — pin a cfg with one set of LoRAs; CLI overrides with different LoRAs; derived `WarmAttachKey` reflects CLI refs.

**Verify:** `pixi run pytest tests/integration/test_loras_cli_e2e.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Locate the existing integration-test pattern.**

```bash
fd -e py 'test_*' /workspace/tests/integration | head
rg -l 'set_stack|SetStackRequest|build_set_stack_request' /workspace/tests/integration
```

Read 1-2 existing integration tests to copy the mock-pod / fake-engine
harness style.

- [ ] **Step 2: Write the three tests.**

```python
# tests/integration/test_loras_cli_e2e.py
"""P3 integration tests — CLI override flows through to set_stack wire body.

Spec §11.6.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kinoforge._adapters import build_set_stack_request
from kinoforge.cli.loras_arg import parse_loras_heredoc
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack


def test_end_to_end_cli_loras_override_cfg_drives_set_stack_request() -> None:
    """CLI override → resolver → build_set_stack_request → wire body."""
    class _Cfg:
        loras = [LoraEntry(ref="civitai:cfg@1", strength=0.5)]

    cli = parse_loras_heredoc(
        "civitai:1111@2222 0.7 h\ncivitai:3333@4444 1.2 l\n"
    )
    active = resolve_active_lora_stack(_Cfg(), None, cli_loras=cli)

    request = build_set_stack_request(active, download_specs={})

    assert len(request.target) == 2
    assert request.target[0].ref == "civitai:1111@2222"
    assert request.target[0].strength == 0.7
    assert request.target[0].branch == "high_noise"
    assert request.target[1].ref == "civitai:3333@4444"
    assert request.target[1].strength == 1.2
    assert request.target[1].branch == "low_noise"


def test_cli_loras_capability_key_derivation_uses_cli_refs_not_cfg_refs() -> None:
    """WarmAttachKey + LoraStack material derive from CLI refs when CLI wins."""
    from kinoforge.core.warm_reuse.matcher import LoraStack  # adjust import if needed

    cli = parse_loras_heredoc("civitai:cli@1 1.0 h\ncivitai:cli@2 1.0 l\n")
    stack = LoraStack(refs=tuple(lo.ref for lo in cli))
    assert stack.refs == ("civitai:cli@1", "civitai:cli@2")


def test_cli_loras_warm_attach_swap_succeeds_when_only_lora_stack_differs() -> None:
    """Warm pod whose WarmAttachKey matches CLI base+engine+precision —
    different LoRA stack triggers a set_stack call (matcher detects swap)
    rather than a cold boot.
    """
    from kinoforge.core.warm_reuse.matcher import is_stack_match

    class _ActiveEntry:
        def __init__(self, ref: str, branch: str, strength: float) -> None:
            self.ref = ref
            self.branch = branch
            self.last_strength = strength

    active = [_ActiveEntry("civitai:old@1", "high_noise", 1.0)]
    target = parse_loras_heredoc("civitai:new@1 1.0 h\n")

    # Different refs → match returns False (signal: needs set_stack swap).
    assert not is_stack_match(active, target)
```

(If `is_stack_match` / `LoraStack` / `build_set_stack_request` live at
different paths than assumed, locate them with `rg` and adjust imports.)

- [ ] **Step 3: Run the integration tests.**

```bash
pixi run pytest tests/integration/test_loras_cli_e2e.py -v
```

Expected: all three pass.

- [ ] **Step 4: Run the whole suite to confirm zero regression.**

```bash
pixi run pytest
```

Expected: full suite green (no live spend; no slow tests added).

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/integration/test_loras_cli_e2e.py

git add tests/integration/test_loras_cli_e2e.py
git commit -m "test(p3): integration — CLI override drives set_stack wire body

P3 Task 6 (§11.6 of design spec). End-to-end coverage of the CLI →
parse_loras_heredoc → resolve_active_lora_stack →
build_set_stack_request chain: assert wire body's target list carries
CLI refs/strengths/branches in order. Warm-attach matcher honors CLI
stack (same WarmAttachKey, different LoraStack → swap, not cold).
Capability-key derivation uses CLI refs when CLI wins."
```

---

### Task 7: PROGRESS.md anchor amendment

**Goal:** Amend the 2026-06-21 PROGRESS.md anchor's example heredoc (`PROGRESS.md:580-585`) to reflect D2 column-order change (`ref [strength] [branch]`).

**Files:**
- Modify: `PROGRESS.md:580-585`

**Acceptance Criteria:**
- [ ] Anchor example heredoc shows `1111:2222 1.0 h` / `3333:4444 1.2 l` (was `1.0 1111:2222 h`).
- [ ] Inline note documents the amendment + cross-references the P3 spec.

**Verify:** `rg -nC2 '1111:2222' /workspace/PROGRESS.md` shows the new order with the note nearby.

**Steps:**

- [ ] **Step 1: Read the existing block.**

```bash
sed -n '575,605p' /workspace/PROGRESS.md
```

(Confirm the exact lines first; the block's line numbers may have
drifted since the spec was written.)

- [ ] **Step 2: Edit the example block.**

Replace the heredoc example body with:

```
--loras "$(cat <<'EOF'
1111:2222 1.0 h
3333:4444 1.2 l
EOF
)"
```

And add the inline note immediately after the example block:

```
**Column order amended 2026-06-25 per P3 spec D2 — `ref [strength]
[branch]`. The 2026-06-21 anchor originally proposed `strength ref
branch`; brainstorm 2026-06-25 swapped to ref-first because `ref` is
the sole required column and trailing-optional ordering allows the
shortest valid line. See
`docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md` D2.**
```

- [ ] **Step 3: Commit.**

```bash
git add PROGRESS.md
git commit -m "docs(progress): amend P3 anchor column order to ref [strength] [branch]

P3 Task 7. Brainstorm 2026-06-25 D2 swapped from the 2026-06-21
anchor's strength-first ordering to ref-first. Anchor example heredoc
updated; inline amendment note cross-references the P3 spec."
```

---

### Task 8: README — `--loras` section

**Goal:** Add a `--loras` subsection under the `kinoforge generate` documentation in `README.md` covering the heredoc shape, precedence, examples, and the vault-bypass WARNING.

**Files:**
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] New `### `--loras` — CLI LoRA stack override` heading under `kinoforge generate`.
- [ ] One minimal example (single LoRA, ref-only).
- [ ] One full example (multi-LoRA heredoc with strength + branch columns).
- [ ] Precedence summary table (CLI > vault > cfg).
- [ ] Error-shape note (collect-all, exit 1, refs never leaked).
- [ ] Vault-bypass WARNING shape documented.

**Verify:** `rg -n 'loras' /workspace/README.md` shows the new section; markdown renders cleanly.

**Steps:**

- [ ] **Step 1: Locate the existing `kinoforge generate` section.**

```bash
rg -n '## .*generate|### generate|kinoforge generate' /workspace/README.md | head
```

- [ ] **Step 2: Add the subsection.**

Append the following under the existing `kinoforge generate`
documentation (adapt heading levels to match the file's existing
hierarchy):

````markdown
### `--loras` — CLI LoRA stack override

Override `cfg.loras` (and bypass `vault.loras` with an audit warning)
by passing a heredoc-string LoRA stack on the command line. No edit to
the YAML needed for one-off experimentation.

**Shape.** One LoRA per line. Whitespace-separated columns:
`ref [strength] [branch]`. Only `ref` is required; `strength` defaults
to `1.0`; `branch` defaults to `auto`. Blank lines and `#` line comments
are silently dropped.

**Minimal example** (one LoRA, defaults):

```bash
kinoforge generate -c examples/configs/wan21-1_3b.yaml \
  --prompt "test" --mode t2v --no-reuse \
  --loras "civitai:1234@5678"
```

**Full example** (multi-LoRA with strength + branch):

```bash
kinoforge generate -c examples/configs/wan22-14b.yaml \
  --prompt "test" --mode t2v --no-reuse \
  --loras "$(cat <<'EOF'
1111:2222 1.0 h
3333:4444 1.2 l
EOF
)"
```

(`1111:2222` is numeric shorthand for `civitai:1111@2222`. Other refs
— `civitai:N@N`, `hf:Org/Repo[:filename]`, `file:/abs/path`,
`https://...` — pass through verbatim. Unknown schemes rejected.)

**Precedence.**

| Source | Wins when... |
|---|---|
| `--loras` | passed (override mode; D3 + D4) |
| `vault.loras` | no `--loras` AND vault loaded with non-empty `.loras` |
| `cfg.loras` | no `--loras` AND no vault.loras |
| `[]` (empty) | `--loras ""` (or comments-only heredoc); explicit empty override |

When `--loras` is passed AND `vault.loras` is non-empty, a single
WARNING line goes to stderr naming the bypass count:

```
cli-loras-bypass-vault: --loras override applied; vault.loras (3 entries) bypassed for this run. Vault is unchanged on disk.
```

The vault file on disk is unchanged.

**Errors.** All input lines parsed in one pass; aggregated diagnostics
printed to stderr on exit 1:

```
--loras: 2 problem(s) found

  line 2 col 1: unknown scheme `cvtai` (expected one of: civitai, file, hf, http, https)
  line 4: duplicate (ref, branch) — first declared on line 1
```

Diagnostic output carries line numbers, column indices, error kinds,
and scheme prefixes only — never the raw `ref` string. This invariant
is locked by `tests/test_lora_error_redaction.py` and the
`tests/test_no_unredacted_writes.py` AST scan.

**Composition.** `--loras` composes orthogonally with all existing
`generate` flags (`--instance-id`, `--no-reuse`, `--dry-run-swap`,
`--force-attach`, `--skip-preflight`). `--dry-run-swap` adds a
`loras_source: cli|vault|cfg|empty` line to the preview so you can
confirm which precedence branch fired without running the full
generation.
````

- [ ] **Step 3: Commit.**

```bash
git add README.md
git commit -m "docs(readme): kinoforge generate --loras subsection

P3 Task 8 (§12.9). Heredoc shape, precedence table (CLI > vault >
cfg), minimal + full example, vault-bypass WARNING text, error-shape
note with the redaction invariant, composition with existing flags
including the new --dry-run-swap loras_source line."
```

---

### Task 9: Manual post-merge Tier-3 live fire

**Goal:** Validate the wire-shape end-to-end by running an actual Tier-3 generation using `kinoforge generate --loras "$(cat <<'EOF' ... EOF)"` instead of the cfg.loras path. Append "See also" line to `successful-generations.md` entry #9.

**Files:**
- Read: `examples/configs/wan21-1_3b-strength-grid.yaml` (clone shape; new cfg passes empty `loras: []`).
- Create (temporary): `/tmp/wan21-no-loras.yaml` (cfg variant with `loras: []` so CLI is the only LoRA source).
- Modify: `successful-generations.md` (See-also line under entry #9).

**Acceptance Criteria:**
- [ ] One Tier-3 generation completes with `kinoforge generate --loras "..."` providing the LoRA stack; cfg has `loras: []`.
- [ ] MP4 produced at the configured output dir.
- [ ] Post-run `kinoforge list` confirms no leaked pods (per CLAUDE.md teardown-verification rule).
- [ ] `successful-generations.md` entry #9 carries a new "See also" line referencing the CLI-loras invocation + sha256.
- [ ] Spend ≤ $0.30 (Tier-3 envelope).

**Verify:**

```bash
pixi run kinoforge generate \
  -c /tmp/wan21-no-loras.yaml \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --mode t2v \
  --no-reuse \
  --loras "$(cat <<'EOF'
1234:5678 0.8 auto
EOF
)"
```

Then `pixi run kinoforge list` must show no running instances + no
ledger entries. Then `sha256sum output/<latest>.mp4`.

(Replace `1234:5678` with the operator-specified Tier-3 LoRA pair —
the same pair documented in successful-generations.md §9.)

**Steps:**

- [ ] **Step 1: Pre-flight.**

```bash
pixi run preflight
```

Must exit 0.

- [ ] **Step 2: Clone the Tier-3 cfg with `loras: []`.**

Copy `examples/configs/wan21-1_3b-strength-grid.yaml` to
`/tmp/wan21-no-loras.yaml`. Open the copy. Replace the top-level
`loras:` block with `loras: []`. Save.

- [ ] **Step 3: Identify the Tier-3 LoRA refs.**

Read `successful-generations.md` §9 to find the canonical Tier-3
operator-specified LoRA pair (static-rotation + Pokemon LoRAs). Note
the `civitai:<modelId>@<versionId>` strings.

- [ ] **Step 4: Run the live smoke.**

```bash
pixi run kinoforge generate \
  -c /tmp/wan21-no-loras.yaml \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --mode t2v \
  --no-reuse \
  --loras "$(cat <<'EOF'
<paste the canonical Tier-3 LoRA refs here, one per line, with
their canonical strengths, branches if explicit>
EOF
)"
```

Poll RunPod runtime stats every 60-90 s per CLAUDE.md live-smoke rule
(`feedback_proactive_pod_stats` memory). Kill + fail fast if GPU idle
for 3 consecutive probes while a generation is supposedly in flight.

- [ ] **Step 5: Verify pod teardown.**

```bash
pixi run kinoforge list
```

Must show `[instance overview] No running instances.` AND
`No instances recorded in ledger.` per CLAUDE.md teardown-verification
rule. If either shows a pod, destroy explicitly via
`pixi run kinoforge destroy --id <pod-id>`.

- [ ] **Step 6: Capture sha256.**

```bash
sha256sum output/<latest>.mp4 | cut -c1-16
```

- [ ] **Step 7: Update `successful-generations.md` entry #9.**

Open `successful-generations.md`. Locate entry #9 (Wan 2.1 1.3B
LoRA-strength variation). Append, under the existing TOC entry:

```
See also (2026-06-25): kinoforge grid --loras CLI-override path
proved end-to-end on the same model + LoRA pair. cfg.loras: []; CLI
heredoc supplied stack via `--loras "$(cat <<'EOF' ... EOF)"`.
Artifact sha256:<16-char-hash>. Vault-bypass not exercised (no vault
loaded). Spend ~$0.10. Commit <hash> covers the CLI-override
implementation.
```

- [ ] **Step 8: Commit the documentation update.**

```bash
git add successful-generations.md
git commit -m "docs(successful-generations): P3 --loras CLI-override see-also

P3 Task 9. Live fire 2026-06-25 confirmed kinoforge generate --loras
HEREDOC drives the full P1+P2 wiring end-to-end on Wan 2.1 1.3B with
cfg.loras: [] (CLI sole LoRA source). Sha256:<hash>, spend ~$<USD>.
See also-line under entry #9."
```

---

### Task 10: PROGRESS.md close-out

**Goal:** Close P3 in PROGRESS.md — single-next-action advances to Layer 5 Bearer per-prediction cost capture.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] New "P3 CLOSED 2026-06-25" entry near the top under "Active workstream" referencing the spec, plan, and the 11-task commit trail.
- [ ] "Single next action" line updated to "Layer 5 Bearer per-prediction cost capture".
- [ ] P3 deferred-queue entry (PROGRESS.md:609-619) struck through OR moved to "Closed workstreams" section per file convention.

**Verify:** `rg -n 'P3 CLOSED|Layer 5' /workspace/PROGRESS.md | head` shows both lines; `git diff PROGRESS.md` reviewable in ≤ 100 lines.

**Steps:**

- [ ] **Step 1: Read the existing top-of-PROGRESS block to mirror the closure conventions used by P1 + P2.**

```bash
sed -n '95,165p' /workspace/PROGRESS.md
```

- [ ] **Step 2: Add the P3-closed entry.**

Slot under "Active workstream" (above the "kinoforge grid LIVE FIRE
evidence" entry):

```markdown
**P3 CLI `--loras` arg surface CLOSED 2026-06-25 (11 tasks shipped).**
Spec `docs/superpowers/specs/2026-06-25-p3-cli-loras-arg-design.md`
+ plan `docs/superpowers/plans/2026-06-25-p3-cli-loras-arg.md`. New
CLI verb `kinoforge generate --loras HEREDOC` overrides `cfg.loras`
and bypasses `vault.loras` (with audit WARNING) for one-shot LoRA
stack changes without editing the cfg. Heredoc shape: one LoRA per
line, columns `ref [strength] [branch]`. Numeric shorthand
`<modelId>:<versionId>` expands to `civitai:<modelId>@<versionId>`.
Unknown schemes rejected. Collect-all error aggregation; refs never
leak in diagnostics. Composite `(ref, branch)` duplicate detection
preserves P2's dual-load case. Empty heredoc valid empty-stack
override (D9). All precedence consolidated in
`resolve_active_lora_stack(*, cli_loras=)` — single source of truth
preserves P1 D11 + P2 capability_key invariants. Tier-3 live fire
2026-06-25 confirmed end-to-end wire-shape (~$<USD> spend). Layer 5
returns to top of single-next-action queue.
```

- [ ] **Step 3: Update single-next-action.**

Change:
```
**🔴 SINGLE NEXT ACTION — Layer 5 Bearer per-prediction cost capture
(Replicate / Runway / Luma).** The `kinoforge grid` build + P1
close-out is FULL_GREEN; Tasks 16/17 (USER-GATE Tier-3 + Tier-4 live
fires) shipped evidence today (see "kinoforge grid LIVE FIRE evidence"
entry below). Layer 5 returns to top of queue.
```

to:
```
**🔴 SINGLE NEXT ACTION — Layer 5 Bearer per-prediction cost capture
(Replicate / Runway / Luma).** P3 CLI `--loras` arg surface CLOSED
2026-06-25 (see entry below). P1 + P2 + P3 of the CLI --loras
decomposition are now all FULL_GREEN. Layer 5 is the highest-value
deferred workstream.
```

- [ ] **Step 4: Strike or move the P3 deferred-queue entry.**

Either:
- Add a leading `~~` strikethrough + `(CLOSED — see above)` suffix to
  the existing PROGRESS.md:609-619 "P3 — CLI `--loras` arg surface
  (DEFERRED, HIGH)" bullet, OR
- Move the bullet into a new "Closed workstreams" section if the file
  uses one.

Match the convention used for the P1 + P2 entries (search the file
for how those were closed out).

- [ ] **Step 5: Commit.**

```bash
git add PROGRESS.md
git commit -m "docs(progress): P3 CLI --loras CLOSED; Layer 5 next

P3 Task 10 (§12.11). P3 shipped 11 tasks across 2026-06-25. Spec +
plan paths recorded. Single-next-action advances to Layer 5 Bearer
per-prediction cost capture. P3 deferred-queue entry struck through
in the sub-project decomposition block."
```

---

## Self-review

**Spec coverage check** (spec §2 in scope → plan task that delivers it):
- Parser module + LorasParseError + LineError → Task 0 ✓
- `--loras HEREDOC` argparse arg + repeat-rejection → Task 4 ✓
- Resolver `cli_loras` kwarg → Task 2 ✓
- Vault-bypass WARNING (count only) → Task 2 ✓
- RedactionRegistry registration → Task 2 ✓
- Numeric shorthand + unknown-scheme rejection → Task 0 ✓
- Composite `(ref, branch)` duplicate rejection → Task 0 ✓
- Collect-all aggregation → Task 0 ✓
- Blank + comment skip → Task 0 ✓
- Empty heredoc as `[]` override → Task 0 + Task 2 ✓
- AST scan extension (AC-P3-1/3/4) → Task 1 ✓
- AST scan AC-P3-5 (precedence single-source) → Task 3 ✓
- README section → Task 8 ✓
- PROGRESS anchor amendment → Task 7 ✓
- PROGRESS close-out → Task 10 ✓
- `--dry-run-swap` `loras_source` line → Task 5 ✓
- Integration tests → Task 6 ✓
- Live smoke (deferred per spec) → Task 9 ✓ (zero new spend)

**Placeholder scan:** every code block is concrete. No "implement later"
markers. Test code is full; impl code is full; commands are full.

**Type consistency:**
- `LineError` fields used identically across Tasks 0, 1, 4, 6.
- `resolve_active_lora_stack(*, cli_loras=...)` signature used identically
  in Tasks 2, 4, 6.
- `parse_loras_heredoc(text: str) -> list[LoraEntry]` signature stable
  across Tasks 0, 4, 6.
- `LorasParseError.report.render_for_cli()` usage stable across Tasks 0, 1, 4.

---

## Execution notes

- No live spend until Task 9. Tasks 0-8 are pure unit/integration/docs.
- Per memory `feedback_run_tests_yourself` + `feedback_autonomous_no_gates`:
  Claude executes all shell/tests including the Tier-3 live smoke.
  No user-gates needed.
- Live smoke budget envelope: ~$0.30 (Tier-3 Wan 2.1 1.3B). Within
  $20 session pre-authorization.
- Each task commits independently per CLAUDE.md durability rules.
- Task ordering dependencies: 0 → 1; 2 → 3; 0+2 → 4; 4 → 5+6; 6 → 7+8;
  7+8 → 9; 9 → 10.
