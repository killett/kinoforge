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
        """Init with an aggregated `LorasParseReport`."""
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
            # branch_raw may be "h"/"l" shortcut; LoraEntry's
            # _normalize_branch_alias validator (mode="before") normalizes
            # it before the Literal check runs. Cast keeps mypy quiet.
            entry = LoraEntry(
                ref=ref_expanded,
                strength=strength,
                branch=branch_raw,  # type: ignore[arg-type]
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
            line_no=line_no,
            col=1,
            kind="missing-scheme",
        )

    scheme = ref_raw[:colon].lower()
    if scheme.isdigit():
        # malformed numeric shorthand (e.g. "1234:" missing version id);
        # treat as missing scheme so the operator sees the intended fix.
        return ref_raw, LineError(
            line_no=line_no,
            col=1,
            kind="missing-scheme",
        )
    if scheme not in _KNOWN_SCHEMES:
        return ref_raw, LineError(
            line_no=line_no,
            col=1,
            kind="unknown-scheme",
            scheme=scheme,
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
        return f"{loc}: bad column count (got {err.got_kind}, expected {err.expected})"
    if err.kind == "unknown-scheme":
        allowed = ", ".join(sorted(_KNOWN_SCHEMES))
        return f"{loc}: unknown scheme `{err.scheme}` (expected one of: {allowed})"
    if err.kind == "missing-scheme":
        return (
            f"{loc}: missing scheme (use `civitai:`, `hf:`, `file:`, or "
            f"numeric `<modelId>:<versionId>`)"
        )
    if err.kind == "bad-strength":
        return f"{loc}: bad strength (not a float)"
    if err.kind == "pydantic":
        return f"{loc}: {err.field} validation failed ({err.got_kind})"
    if err.kind == "duplicate":
        return (
            f"{loc}: duplicate (ref, branch) — first declared on line {err.first_line}"
        )
    return f"{loc}: {err.kind}"  # defensive fallback; unreachable per Literal
