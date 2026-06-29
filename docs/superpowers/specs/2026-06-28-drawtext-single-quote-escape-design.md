# drawtext caption escape: switch to single-quote wrap

## Problem

`kinoforge.core.grid.compose._escape_drawtext` currently emits the
character-substitution form: `:` → `\:`, `'` → `\'`, `%` → `\%`,
`\` → `\\`, `\n` → `\n` (literal). That single layer of backslash escape
is not robust against ffmpeg's `-filter_complex` parser pipeline: in
practice, captions containing `:` such as
`fixture: realistic prompt` truncated mid-string during
`compose_grid_mp4`, breaking T8 / T13 grid renders on 2026-06-28. The
workaround shipped at commit 610862b sidesteps the bug by renaming
captions to avoid `:`, but the escape is still wrong for any caption a
user supplies that contains `:` (and likely `,` and `;` too, which the
chain syntax also overloads).

## Goal

Make `_escape_drawtext` robust against `:` `,` `;` in caption text, so
arbitrary user-supplied captions render correctly without forcing
operators to memorise an ad-hoc forbidden-character list.

## Non-goals

- Adding live-ffmpeg integration tests beyond the unit tier. T8 / T13
  reruns will exercise the end-to-end path.
- Reverting `examples/configs/grids/*.yaml` to colon-bearing captions.
  Separate follow-up commit once this lands.

## Approach

Switch to ffmpeg's documented single-quote wrap. Inside a
single-quoted `text='...'` value, the filtergraph parser treats `:`
`,` `;` as literal — no per-character escape needed. Only `'`, `\`,
and `%` still need handling inside the quotes:

- `'` → `\'` so the quoted region doesn't close early.
- `\` → `\\` so drawtext's own escape pass doesn't consume the next
  character (`\b`, `\n` etc. have meaning).
- `%` → `\%` so drawtext's strftime-like `%{...}` substitution stays
  inert on user input (this rule survives even inside single quotes).

Newline handling unchanged: drawtext's text= treats the two-character
sequence `\n` as a line break, so an actual `'\n'` in the input
becomes `\n` in the quoted output via the backslash-doubling rule
(input `\` doubles to `\\`, input `n` stays literal — yielding `\\n`
on the wire). That matches existing observed-as-newline behavior; if
that proves wrong in practice we add a targeted test and revisit.

## Change

```python
# core/grid/compose.py

def _escape_drawtext(s: str) -> str:
    r"""Escape caption text for ffmpeg drawtext text= value.

    Wraps the result in single quotes; inside the quotes the only
    special characters are '\'', '\\', and '%' (kept escaped to
    disable strftime-like substitution). Filtergraph chain separators
    (':' ',' ';') are NOT escaped — they're inert inside single quotes.
    """
    inner = s.replace("\\", "\\\\").replace("'", r"\'").replace("%", r"\%")
    return f"'{inner}'"
```

Caller in `_build_filter_graph`:

```python
if cell.caption:
    text_value = _escape_drawtext(cell.caption)  # already single-quoted
    chain += (
        f",drawtext=text={text_value}:fontcolor=white:fontsize=h*0.05:"
        f"box=1:boxcolor=black@0.5:boxborderw=8:"
        f"x=(w-text_w)/2:y=20"
    )
```

The old `_DRAWTEXT_ESCAPED` constant is removed.

## Tests (RED first)

In `tests/core/test_grid_compose.py`:

1. `test_escape_drawtext_single_quotes_output_and_keeps_colon_literal`
   - Input: `"a:b"`. Expected: `"'a:b'"`.
   - Bug it catches: a fix that still per-char escapes `:` would emit
     `'a\:b'` and fail this assertion. Pins the new wrapping scheme.

2. `test_escape_drawtext_escapes_inner_single_quote`
   - Input: `"it's"`. Expected: `r"'it\'s'"`.
   - Bug it catches: forgetting the `'` substitution would close the
     quoted region early at the apostrophe, producing
     `'it's'` which the filtergraph parser would see as `'it'` + `s'`.

3. `test_escape_drawtext_doubles_backslash_so_drawtext_does_not_consume`
   - Input: `"a\\b"` (Python literal: backslash). Expected: `r"'a\\b'"`.
   - Bug it catches: a single backslash inside the quoted region would
     let drawtext's escape consume `b`, mis-rendering as `a` + control
     char.

4. `test_escape_drawtext_keeps_percent_escape_to_disable_strftime`
   - Input: `"100%"`. Expected: `r"'100\%'"`.
   - Bug it catches: dropping `%` from the escape would let drawtext
     interpret `%{n}` style substitutions on user input.

5. `test_escape_drawtext_empty_string_still_quoted`
   - Input: `""`. Expected: `"''"`.
   - Bug it catches: a guard like `if not s: return s` would emit a
     bare `text=` (no value), which ffmpeg rejects as a parse error.

6. `test_build_filter_graph_caption_with_colon_emits_single_quoted_text`
   - Caption `"strength:0.5"`. Graph contains literal substring
     `"text='strength:0.5'"`. Replaces the existing
     `test_build_filter_graph_caption_with_colon_is_escaped` (which
     pins the old `\:` form and must fail after the switch).

7. Update existing parametrized cases in `test_escape_drawtext` that
   pin per-character escape outputs (`r"a\:b"` etc.). They were
   pinning the broken contract; replace with the new quoted forms.

## Risk

Low-medium. Single function, unit-tested. The risk is that ffmpeg
behaves differently than the spec describes for an exotic Unicode
input we didn't cover. Mitigation: T8 / T13 reruns after merge act as
live smoke; if a real caption breaks we add a targeted test.

## Follow-up

Separate commit reverts the colon-avoidance workaround in:

- `examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml`
- `examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml`

Captions return to `fixture: realistic prompt` / `fixture: strength=0.5`.
That commit is the end-to-end proof; it is NOT in this spec's scope.
