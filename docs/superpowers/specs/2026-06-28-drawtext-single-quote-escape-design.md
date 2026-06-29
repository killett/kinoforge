# drawtext caption escape: empirical double-backslash for `:`

## Status

REVISED 2026-06-28 after empirical ffmpeg test refuted the original
single-quote hypothesis. Filename kept for git continuity; content
documents the corrected approach.

## Problem

`kinoforge.core.grid.compose._escape_drawtext` currently emits a
single-level per-char escape (`:` → `\:`, `'` → `\'`, `%` → `\%`,
`\` → `\\`). Captions like `fixture: realistic prompt` truncated
mid-string during `compose_grid_mp4`, breaking T8 / T13 grid renders
on 2026-06-28. The workaround at commit 610862b renamed captions to
avoid `:`; the underlying executor bug is unfixed.

## Goal

Make `_escape_drawtext` robust against `:` `,` `;` `'` `%` in caption
text so arbitrary user-supplied captions render correctly.

## Non-goals

- Live-ffmpeg integration tests beyond the unit tier. T8 / T13 reruns
  exercise the end-to-end path.
- Reverting `examples/configs/grids/*.yaml` to colon-bearing captions.
  Separate follow-up commit once this lands.

## Empirical findings (ffmpeg 8.1.1)

Sweep over `text=a<bs>Xb` for each special char with 1, 2, and 4
literal backslashes on the argv wire (no shell mangling — subprocess
argv direct), with the standard `:fontcolor=...:x=...:y=...` suffix:

| char | 1×`\` | 2×`\` | 4×`\` |
|------|-------|-------|-------|
| `:`  | FAIL  | OK    | FAIL  |
| `,`  | OK    | FAIL  | FAIL  |
| `;`  | OK    | FAIL  | FAIL  |
| `'`  | OK    | FAIL  | FAIL  |
| `%`  | OK    | OK    | OK    |

`:` is the outlier — it requires `\\:` (two backslashes on wire) where
the others want a single backslash. The single-quote-wrap hypothesis
that this spec previously documented is FALSE for ffmpeg's
filtergraph parser: `'…'` does NOT make `:` inert. Independent test:
`text='fixture: realistic prompt'` (with literal quotes, no inner
backslash) → `No option name near ' realistic prompt:fontcolor='`.

Likely mechanism (not load-bearing on the fix): drawtext's `text=`
value goes through an extra unescape pass on `:` specifically, so the
chain parser's single-backslash escape is consumed before drawtext
gets a chance to interpret. Other separators (`,` `;`) only meet one
unescape pass.

## Approach

Replace the per-char escape with the empirically validated rules:

- `\` → `\\` (double — drawtext's own escape pass interprets `\X`
  sequences such as `\n` for newline; doubling preserves user-supplied
  literal backslashes).
- `:` → `\\:` (double-backslash — the empirical winner; single
  backslash fails).
- `,` `;` `'` `%` → `\X` (single backslash — empirically sufficient
  and the `%` rule disables drawtext's strftime-like `%{...}`).

Ordering matters: backslash MUST be doubled first, otherwise the
later substitutions' inserted backslashes would themselves be doubled.

## Change

```python
# core/grid/compose.py

def _escape_drawtext(s: str) -> str:
    r"""Escape a caption for ffmpeg drawtext text= value.

    Empirically validated against ffmpeg 8.1.1 (2026-06-28):

      * '\\' doubles to '\\\\' so drawtext's text= parse does not
        consume user backslashes via its own \\X interpretation.
      * ':' becomes '\\\\:' (two backslashes on wire) — the chain
        parser does not consume a single-backslash escape for ':'
        in drawtext's text= value, while a double-backslash survives
        chain-syntax stripping.
      * ',' ';' "'" '%' each take a single backslash — empirically
        sufficient and the '%' rule keeps drawtext's strftime-like
        '%{...}' substitution inert on user input.

    Backslash MUST be doubled first; later substitutions insert
    backslashes that we do NOT want re-escaped.
    """
    out = s.replace("\\", "\\\\")
    out = out.replace(":", "\\\\:")
    for ch in (",", ";", "'", "%"):
        out = out.replace(ch, "\\" + ch)
    return out
```

Caller in `_build_filter_graph` unchanged shape — value is no longer
wrapped in quotes; emit remains `f",drawtext=text={esc}:fontcolor=..."`
which was the original shape.

The old `_DRAWTEXT_ESCAPED` dict is removed.

## Tests (RED first)

In `tests/core/test_grid_compose.py`:

1. `test_escape_drawtext_colon_uses_double_backslash`
   - Input `"a:b"` → expected `r"a\\:b"` (two backslashes, then `:`).
   - Bug catch: a single-backslash escape (the broken shipped form)
     would emit `r"a\:b"` and fail this assertion. Matches the
     empirical ffmpeg-8.1.1 requirement.

2. `test_escape_drawtext_single_backslash_for_chain_separators`
   - Parametrize `","`, `";"`, `"'"`, `"%"` → each emits `\X`.
   - Bug catch: applying the colon's double-backslash rule to these
     chars would over-escape and break ffmpeg's chain parser
     (empirically verified failure mode).

3. `test_escape_drawtext_user_backslash_is_doubled`
   - Input `"a\\b"` (one literal backslash) → expected `r"a\\\\b"`
     (two literal backslashes).
   - Bug catch: a single user backslash that survives un-doubled
     would let drawtext's own escape pass consume the next character
     (e.g. `\b` interpreted as a control sequence).

4. `test_escape_drawtext_ordering_does_not_double_inserted_backslashes`
   - Input `"a:b"` must NOT emit `r"a\\\\:b"` (four backslashes).
   - Bug catch: doing the colon substitution before the backslash
     doubling would re-double the colon's inserted backslashes,
     yielding the four-backslash form that empirically FAILS the
     ffmpeg parser.

5. `test_escape_drawtext_empty_string_passthrough`
   - Input `""` → expected `""`.
   - Bug catch: a guard like `return f"'{s}'"` left over from the
     abandoned single-quote design would emit `"''"`.

6. `test_build_filter_graph_caption_with_colon_uses_double_backslash`
   - Caption `"strength:0.5"` → graph contains literal substring
     `r"text=strength\\:0.5"`. Replaces the existing
     `test_build_filter_graph_caption_with_colon_is_escaped` which
     pins the broken single-backslash form.

7. `test_build_filter_graph_caption_with_colon_and_space`
   - Real-world caption `"fixture: realistic prompt"` → graph contains
     `r"text=fixture\\: realistic prompt"`. Pins the exact bug from
     the 2026-06-28 production failure.

## Risk

Low. The empirical sweep covered every relevant character; the
proposed rules each round-trip OK through ffmpeg 8.1.1's parser when
exercised in isolation and in a combined caption. Risk vector that
remains: an exotic Unicode or multi-special input not covered by the
sweep. Mitigation: T8 / T13 reruns after merge act as live smoke; if
a real caption breaks we add a targeted test.

## Follow-up

Separate commit reverts the colon-avoidance workaround in:

- `examples/configs/grids/wan21-mixed-path-plus-generate.grid.yaml`
- `examples/configs/grids/wan22-14b-mixed-path-plus-generate.grid.yaml`

Captions return to `fixture: realistic prompt` / `fixture: strength=0.5`.
That commit is the end-to-end proof; it is NOT in this spec's scope.
