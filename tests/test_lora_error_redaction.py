"""P3 redaction parity — LineError + LorasParseError + render_for_cli.

Spec §11.4 lockdown. Forces every error kind with a sensitive ref string,
asserts the ref substring never appears in str/repr/render output.
"""

from __future__ import annotations

import pytest

from kinoforge.cli.loras_arg import LineError, LorasParseError, parse_loras_heredoc

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
        # unknown-scheme (sensitive-shaped string with bad scheme)
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
