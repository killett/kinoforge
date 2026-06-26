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
    parse_loras_heredoc,
)

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
    assert (
        parse_loras_heredoc("civitai:1234@5678 1.0 high_noise")[0].branch
        == "high_noise"
    )


def test_branch_low_noise_explicit_accepted() -> None:
    assert (
        parse_loras_heredoc("civitai:1234@5678 1.0 low_noise")[0].branch == "low_noise"
    )


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
