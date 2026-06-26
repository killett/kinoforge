# P3 — CLI `--loras` arg surface — design spec

**Status:** APPROVED (brainstorm 2026-06-25, section-by-section approvals captured).
**Sub-project:** P3 of the CLI `--loras` arg decomposition (PROGRESS.md 2026-06-21 anchor).
**Predecessors (must be CLOSED before plan execution):**

- P1 — Server per-LoRA strength weights (CODE-COMPLETE 2026-06-21, commits `c96078e..eeecf84`).
  Spec: `docs/superpowers/specs/2026-06-21-server-lora-strength-design.md`.
- P2 — Wan 2.2 dual-transformer routing (FULL_GREEN 2026-06-23, branch field on
  `LoraEntry`). Spec: `docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md`.

**Predecessor reading (required to understand invariants):**

- `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md` (vault +
  `RedactionRegistry` invariants this spec must satisfy).
- `docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md`
  (warm-reuse matcher mutability model — `WarmAttachKey` vs `LoraStack`).
- `docs/superpowers/specs/2026-06-21-server-lora-strength-design.md`
  (resolver, matcher, capability_key invariants P3 extends).
- `docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md`
  (branch field, alias normalization, MoE vs single-transformer validation).

---

## 1. Motivation

P1 + P2 landed the server- and config-side wiring for per-LoRA strength and
high-/low-noise transformer routing. The operator can express both today by
editing `examples/configs/*.yaml`'s top-level `loras:` block. P3 closes the
loop: a CLI surface that lets the operator override the cfg's LoRA stack
inline for one run without touching the file.

The shape was anchored 2026-06-21 (PROGRESS.md commit `4b22cde`) as a bash
heredoc passed via `--loras`. P3 brainstorm 2026-06-25 amended one detail
(column order — see D2 below) and locked the remaining open semantic
questions: precedence vs cfg.loras, precedence vs vault.loras, ref-shorthand
expansion rule, error reporting shape, blank/comment handling, duplicate
handling, and empty-heredoc semantics.

P3 changes NO server-side code. All work lives in `src/kinoforge/cli/` +
one keyword-only argument added to `src/kinoforge/core/lora.py::resolve_active_lora_stack`.
This keeps the blast radius small and preserves P1/P2's existing test
coverage unchanged.

## 2. Scope

### In scope

- New parser module `src/kinoforge/cli/loras_arg.py` with public surface
  `parse_loras_heredoc(text: str) -> list[LoraEntry]` and aggregated
  diagnostic types `LorasParseError` + `LorasParseReport` + `LineError`.
- New `--loras HEREDOC` argument on `kinoforge generate`. Repeat use on
  the same invocation rejected by a custom argparse action.
- Extension of `src/kinoforge/core/lora.py::resolve_active_lora_stack` with
  a keyword-only `cli_loras: list[LoraEntry] | None = None` argument.
- Single `WARNING` log site when CLI overrides a non-empty `vault.loras`
  (count only — refs never enter the log line).
- Single `RedactionRegistry.add_many` site for CLI-supplied refs.
- Numeric-shorthand expansion `^\d+:\d+$` → `civitai:<modelId>@<versionId>`.
- Unknown-scheme rejection (allow-list: `civitai`, `hf`, `file`, `https`, `http`).
- Composite-key `(ref, branch)` duplicate rejection.
- Collect-all error aggregation across every input line.
- Blank-line + `#`-line-comment skipping.
- Empty heredoc treated as a valid `[]` override.
- AST-scan extension of `tests/test_no_unredacted_writes.py` covering the
  new parser module + the new resolver branch + the `LineError` shape.
- README section + dotted-example update.
- Amendment of the PROGRESS.md 2026-06-21 anchor (column order change).

### Out of scope (deliberately deferred)

- `kinoforge batch` row-level CLI override. Batch consumes cfg per-row; the
  CLI-row override flow needs its own design.
- Repeatable `--lora ref=...,strength=...,branch=...` flag. Heredoc-only is
  locked for v1 (D1); the repeatable surface can be added in a follow-up if
  ergonomic friction shows up post-ship.
- CivitAI browser-URL normalization (`https://civitai.com/models/X?modelVersionId=Y`).
  Considered + rejected for v1 (D5 alt C).
- Env-var or project-level override file precedence. Resolver signature is
  built to extend cleanly (one kwarg per new source) when this comes up.
- Trigger-word / sampler-hint metadata pass-through. Future `LoraEntry` field
  work, orthogonal to CLI surface.
- New live-smoke spend. P3 adds no server-side surface; existing Tier-3 +
  Tier-4 smokes prove strength + branch end-to-end. CLI surface covered by
  unit + integration tests; a manual post-merge `kinoforge generate --loras`
  invocation on the existing Tier-3 cfg captures the wire-shape proof at
  zero net spend.

### Pre-conditions inherited from P1

- `LoraEntry` exists with `ref + strength + sha256 + branch` (post-P2).
- `VaultLoRA(LoraEntry)` extends with `label`.
- `LoraTarget` server wire shape with `ref + strength + branch`.
- `set_adapters(adapter_weights=...)` server-side wiring lands strength.
- `_replace_adapter_stack` server-side dispatch routes by `branch` on MoE.
- `is_stack_match(refs + strength + branch)` matcher (P2 extension).
- VRAM-OOM rollback restores `(ref, strength, branch)` triples.
- `resolve_active_lora_stack(cfg, vault)` cfg-vs-vault precedence + P1 D11
  `LoraStackConflict` on diverging non-empty pair.

## 3. Decisions locked during brainstorm

| # | Decision | Value | Why |
|---|---|---|---|
| D1 | Arg surface | Heredoc-only `--loras STRING`. Repeated `--loras` on one invocation rejected by custom argparse action with `parser.error`. | Heredoc anchor from 2026-06-21 already chosen by user; keeps v1 surface small. Repeatable `--lora` deferred to a follow-up. |
| D2 | Column order | `ref [strength] [branch]` — `ref` required, `strength` defaults `1.0`, `branch` defaults `auto`. **Amends** the 2026-06-21 anchor's `strength ref branch` ordering. | `ref` is the only required column post-P1; trailing-optional pattern allows shortest valid line `civitai:X@Y`. Matches sd-webui muscle memory (`<lora:NAME:WEIGHT>`). Avoids numeric-prefix mis-read-as-line-number. |
| D3 | CLI vs `cfg.loras` | Override (CLI replaces `cfg.loras` entirely; not extend). | Single, predictable rule. Matches `--prompt` overriding cfg.prompt today. Append semantics need a merge rule that P3 v1 deliberately skips. |
| D4 | CLI vs `vault.loras` | CLI wins over vault. WARNING fires when bypass occurs. | Operator's `--loras` is explicit + auditable in shell history; treating it as deliberate override matches its purpose. P1 D2 ("vault is sole authoritative source when loaded") is narrowed by this explicit exception, NOT silently violated — the WARNING preserves the audit trail. |
| D5 | Ref shorthand expansion | Numeric `^\d+:\d+$` → `civitai:<modelId>@<versionId>`. Other refs pass through verbatim. Unknown scheme prefixes rejected against allow-list `{civitai, hf, file, https, http}`. | Catches typos (`cvtai:...`) at parse time with a precise line/col. URL normalization (alt C) rejected for v1 — too many edge cases. |
| D6 | Error reporting | Collect-all: walk every line, accumulate all `LineError`s, raise one `LorasParseError(report=...)` after the full sweep. CLI prints `render_for_cli()` to stderr, exits 1. | Operators paste multi-line stacks; one-error-at-a-time iterate-and-retry is bad ergonomics. Aggregated report mirrors mypy / ruff diagnostics. |
| D7 | Blank + comment handling | Blank lines silently dropped. Lines starting with `#` (after optional whitespace) silently dropped. Inline `#` NOT treated as comment (a line of `civitai:X 1.0 # foo` parses as 4 tokens → `bad-columns`). | Matches `.env` / `requirements.txt` convention. Lets operators annotate stacks (`# Arcane high-noise pair`). Inline-comment support sacrificed for a simpler tokenizer + clearer error on accidental-trailing-token. |
| D8 | Duplicate handling | Reject by composite `(ref, branch)` key. Same ref with `branch=h` AND `branch=l` accepted (P2's dual-load case). Same ref with same branch (or both omitted-default-to-`auto`) rejected with `LineError(kind="duplicate", first_line=..., this_line=...)`. | Honors P2's `(ref, branch)` composite inventory invariant. Catches paste-duplicates without forbidding the legitimate MoE dual-load case. |
| D9 | Empty heredoc | Allowed. Empty heredoc (or comments + blanks only) returns `[]` and overrides `cfg.loras` to `[]` for this run. | Consistent with D3 override semantics. Workflow: "this cfg ships a preset stack; I want to run it without any LoRAs this time." No new sentinel flag needed. |
| D10 | Threading approach | Extend `resolve_active_lora_stack(cfg, vault, *, cli_loras=None)`. CLI parser produces `list[LoraEntry] | None`; resolver consolidates all precedence in one place. | Single source of truth for precedence (P1 architecture preserved). Single redaction-registration site. Single audit-log site. Cleanest forward extension to env-var / project-override / MCP-tool precedence sources. Alt B (cfg-mutation) actively breaks P1 D11. Alt C (SessionContext-branch) fragments precedence across N consumers. |

## 4. Key invariants

- **P3-Privacy-1:** `LineError` Pydantic model has no field annotated `str`
  whose name matches `r"ref|filename|label"`. Lockdown:
  `tests/test_lora_error_redaction.py::test_line_error_has_no_ref_field`
  (AST scan over the class body).
- **P3-Privacy-2:** `LorasParseError.__str__` / `__repr__` and
  `LorasParseReport.render_for_cli` build their output strictly from
  `LineError` fields. Lockdown:
  `tests/test_lora_error_redaction.py::test_render_for_cli_never_contains_ref`
  (force every error kind with a sensitive ref; assert ref substring absent
  from output across `str`, `repr`, and `render_for_cli`).
- **P3-Privacy-3:** CLI-supplied refs are registered with the global
  `RedactionRegistry` inside `resolve_active_lora_stack` BEFORE the vault-bypass
  WARNING fires. Lockdown:
  `tests/core/test_lora_resolver_p3.py::test_cli_loras_refs_registered_with_redaction_registry`
  + `test_cli_loras_warning_contains_no_ref_strings`.
- **P3-Privacy-4:** vault-bypass WARNING message contains the count of
  bypassed entries and the literal string `"Vault is unchanged on disk."`
  but does not contain any vault or CLI ref string. Lockdown:
  `tests/core/test_lora_resolver_p3.py::test_cli_loras_warning_contains_no_ref_strings`
  (caplog regex assertion).
- **P3-CI-1:** `tests/test_no_unredacted_writes.py` AST scan extends
  coverage to (a) `src/kinoforge/cli/loras_arg.py`, (b) the
  `cli_loras is not None` branch of `resolve_active_lora_stack`, and
  (c) the `LineError` class declaration (no `ref|filename|label`-named
  `str` fields).
- **P3-Precedence-Single-Source:** all CLI-vs-vault-vs-cfg precedence
  computation lives inside `resolve_active_lora_stack`. No call site
  branches on `cli_loras is not None` before calling the resolver
  (other than the trivial "parse arg → pass kwarg" hop in `_cmd_generate`).
  Lockdown: `tests/test_no_precedence_branches_outside_resolver.py`
  AST scan rejects any module under `src/kinoforge/` that pattern-matches
  on `ctx.cli_loras is not None` or `args.loras is not None` followed by
  conditional cfg-vs-vault logic.
- **P3-Identity-Inherited:** strength remains OUT of `capability_key`
  hash material (P1 D4). Branch remains IN `capability_key` hash material
  (P2). CLI-supplied stack uses the same derivation, so a CLI override
  matching cfg.loras refs+branches lands on the same warm pod.
- **P3-Override-Empty:** `cli_loras = []` (not `None`) MUST override
  cfg.loras and vault.loras to the empty stack. Lockdown:
  `tests/core/test_lora_resolver_p3.py::test_cli_loras_empty_list_overrides_to_empty_stack`.
- **P3-No-Server-Change:** the diff for P3 does not touch
  `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` or
  `src/kinoforge/_adapters.py::build_set_stack_request`. Lockdown:
  CI grep over the P3 PR diff.

## 5. Architecture overview

```
                  ┌─────────────────────────┐
                  │  argv  --loras HEREDOC  │
                  └────────────┬────────────┘
                               │
                               ▼
              ┌─────────────────────────────────┐
              │  cli/loras_arg.py               │
              │  parse_loras_heredoc(text)      │
              │    → list[LoraEntry]            │
              │    OR raise LorasParseError(    │
              │       LorasParseReport)         │
              └────────────────┬────────────────┘
                               │
                               │  list[LoraEntry] | None
                               ▼
┌─────────────────────────────────────────────────────────┐
│  cli/_commands.py::_cmd_generate                         │
│    cli_loras = parse_loras_heredoc(args.loras)           │
│      if args.loras is not None else None                 │
│    active_stack = resolve_active_lora_stack(             │
│        cfg, vault, cli_loras=cli_loras                   │
│    )                                                     │
└────────────────────────────┬─────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────┐
│  core/lora.py::resolve_active_lora_stack                 │
│    precedence: CLI > vault > cfg                         │
│    cli_loras is not None:                                │
│      → register CLI refs with RedactionRegistry          │
│      → if vault.loras non-empty: WARNING (count only)    │
│      → return cli_loras                                  │
│    cli_loras is None:  (P1 path unchanged)               │
│      → vault wins; LoraStackConflict on diverging cfg    │
└────────────────────────────┬─────────────────────────────┘
                             │  list[LoraEntry]
                             ▼
              (existing P1/P2 path: build_set_stack_request,
               capability_key derivation, matcher, server)
```

## 6. Parser specification

### 6.1 Tokenization

```python
def parse_loras_heredoc(text: str) -> list[LoraEntry]:
    """Parse a --loras heredoc body into a validated LoRA stack.

    Raises:
        LorasParseError: when ≥1 input line fails validation; the carried
            LorasParseReport aggregates every line's errors.
    """
    errors: list[LineError] = []
    entries: list[LoraEntry] = []
    seen_keys: dict[tuple[str, str], int] = {}  # (ref, branch) → first line_no

    for line_no, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue

        tokens = re.split(r"\s+", line)
        if len(tokens) not in (1, 2, 3):
            errors.append(LineError(
                line_no=line_no,
                col=None,
                kind="bad-columns",
                got_kind=str(len(tokens)),
                expected="1, 2, or 3",
            ))
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
            errors.append(LineError(
                line_no=line_no, col=2,
                kind="bad-strength",
                got_kind="not-a-float",
            ))
            continue

        try:
            entry = LoraEntry(
                ref=ref_expanded,
                strength=strength,
                branch=branch_raw,  # P2 alias validator normalizes h/l
            )
        except ValidationError as ve:
            for err in ve.errors():
                errors.append(LineError(
                    line_no=line_no,
                    col=_col_for_field(err["loc"]),
                    kind="pydantic",
                    field=str(err["loc"][0]) if err["loc"] else None,
                    got_kind=err["type"],
                ))
            continue

        key = (entry.ref, entry.branch)
        first = seen_keys.get(key)
        if first is not None:
            errors.append(LineError(
                line_no=line_no, col=None,
                kind="duplicate",
                first_line=first,
            ))
            continue
        seen_keys[key] = line_no
        entries.append(entry)

    if errors:
        raise LorasParseError(LorasParseReport(errors=errors))
    return entries
```

### 6.2 Ref expansion (D5)

```python
_NUMERIC_SHORTHAND = re.compile(r"^(\d+):(\d+)$")
_KNOWN_SCHEMES = frozenset({"civitai", "hf", "file", "https", "http"})


def _expand_ref(ref_raw: str, line_no: int) -> tuple[str, LineError | None]:
    """Apply D5 expansion + scheme validation."""
    m = _NUMERIC_SHORTHAND.match(ref_raw)
    if m is not None:
        return f"civitai:{m.group(1)}@{m.group(2)}", None

    # Scheme detection: "<scheme>:<rest>", where the colon precedes any "/"
    # (so hf:Org/Repo:filename still parses as scheme=hf).
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
```

## 7. Resolver extension

### 7.1 Signature + precedence

```python
# src/kinoforge/core/lora.py

def resolve_active_lora_stack(
    cfg: Config,
    vault: Vault | None,
    *,
    cli_loras: list[LoraEntry] | None = None,
) -> list[LoraEntry]:
    """Resolve the active LoRA stack.

    Precedence (P3-D3, P3-D4):
      CLI > vault > cfg

    When cli_loras is not None, CLI wins entirely — vault.loras is bypassed
    and cfg.loras is replaced. If vault is loaded with non-empty .loras,
    a single WARNING is emitted naming the count of bypassed refs (refs
    themselves never enter the log line per P3-Privacy-4).

    When cli_loras is None, the original P1 precedence + LoraStackConflict
    rule applies unchanged.

    Raises:
        LoraStackConflict: only when cli_loras is None AND cfg.loras +
            vault.loras both non-empty with diverging ref sets.
    """
    if cli_loras is not None:
        redaction_registry.add_many(lo.ref for lo in cli_loras)
        if vault is not None and vault.loras:
            logger.warning(
                "cli-loras-bypass-vault: --loras override applied; "
                "vault.loras (%d entries) bypassed for this run. "
                "Vault is unchanged on disk.",
                len(vault.loras),
            )
        return list(cli_loras)

    # --- existing P1 path, unchanged ---
    if vault is not None and vault.loras:
        cfg_refs = {lo.ref for lo in cfg.loras}
        vault_refs = {lo.ref for lo in vault.loras}
        if cfg.loras and cfg_refs != vault_refs:
            raise LoraStackConflict(
                "cfg.loras and vault.loras both set with diverging ref sets — "
                "remove cfg.loras and use vault.loras as sole source"
            )
        return [LoraEntry(**lo.model_dump(exclude={"label"})) for lo in vault.loras]
    return list(cfg.loras)
```

### 7.2 Behavior matrix

| `cli_loras` | `vault.loras` | `cfg.loras` | Result |
|---|---|---|---|
| `None` | empty | empty | `[]` |
| `None` | empty | non-empty | `cfg.loras` |
| `None` | non-empty | empty | `vault.loras` upcast |
| `None` | non-empty | non-empty, same refs | `vault.loras` upcast (P1 D11) |
| `None` | non-empty | non-empty, diverging refs | `LoraStackConflict` (P1 D11) |
| `[]` | any | any | `[]` (P3-D9 empty override) + WARNING if vault non-empty |
| `[L1, L2]` | empty | any | `[L1, L2]` |
| `[L1, L2]` | non-empty | any | `[L1, L2]` + WARNING |

## 8. CLI surface

### 8.1 Argparse change

```python
# src/kinoforge/cli/_main.py — slot after the --dry-run-swap block

class _LorasOnceAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error("--loras may be specified at most once")
        setattr(namespace, self.dest, values)


p_generate.add_argument(
    "--loras",
    action=_LorasOnceAction,
    default=None,
    metavar="HEREDOC",
    help=(
        "override cfg.loras AND vault.loras with a CLI-supplied LoRA stack. "
        "Heredoc body: one LoRA per line, whitespace-separated columns "
        "`ref [strength] [branch]`. `#` line comments + blank lines ignored. "
        "Numeric shorthand `<modelId>:<versionId>` expands to "
        "`civitai:<modelId>@<versionId>`; other refs (civitai:..., hf:..., "
        "file:..., https://...) pass through verbatim; unknown schemes "
        "rejected. Strength defaults to 1.0; branch defaults to `auto`. "
        "Empty heredoc clears the stack for this run. Vault.loras bypass "
        "logged to stderr."
    ),
)
```

### 8.2 Command wiring

```python
# src/kinoforge/cli/_commands.py::_cmd_generate — slot before
# resolve_active_lora_stack call

cli_loras: list[LoraEntry] | None = None
if args.loras is not None:
    try:
        cli_loras = parse_loras_heredoc(args.loras)
    except LorasParseError as err:
        sys.stderr.write(err.report.render_for_cli())
        sys.stderr.write("\n")
        return 1

active_stack = resolve_active_lora_stack(cfg, vault, cli_loras=cli_loras)
```

### 8.3 `render_for_cli` output shape

```
--loras: 3 problem(s) found

  line 2 col 1: unknown scheme `cvtai` (expected one of: civitai, file, hf, http, https)
  line 4 col 2: bad strength (not a float)
  line 7: duplicate (ref, branch) — first declared on line 3
```

- Fixed schema. Always starts with the count summary line.
- One indented `line N [col M]: ...` per error, preserving input line order.
- No ref strings, no filenames, no labels.
- Whole-line errors omit the column ("line 7: duplicate ...").

### 8.4 Composition with existing flags

| Existing flag | Composition |
|---|---|
| `-c/--config` | required + unchanged. `cfg.loras` is the override target. |
| `--prompt` | unchanged. |
| `--mode` | unchanged. |
| `--run-id` | unchanged. |
| `--output-dir` / `--no-output-dir` | unchanged. |
| `--instance-id` | unchanged. If pinned pod's `WarmAttachKey` mismatches CLI stack's base/engine/precision, existing capability-key reject fires. |
| `--force-attach` | unchanged. Still never bypasses `capability_key` mismatch. |
| `--no-reuse` | unchanged. CLI stack still resolved; pod still destroyed at end. |
| `--skip-preflight` | unchanged. |
| `--dry-run-swap` | unchanged. Dry-run printer gains one line `loras_source: cli`/`vault`/`cfg`/`empty` derived from the resolver's decision. |

## 9. Error class hierarchy

```python
# src/kinoforge/cli/loras_arg.py

class LorasParseError(KinoforgeError):
    """Raised by parse_loras_heredoc when ≥1 line fails validation.

    Carries an aggregated LorasParseReport. NEVER carries .ref strings —
    only (line_no, col, kind, structured-context) per error.
    """
    def __init__(self, report: LorasParseReport) -> None:
        super().__init__(f"--loras: {len(report.errors)} problem(s) found")
        self.report = report


class LorasParseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    errors: list[LineError]

    def render_for_cli(self) -> str: ...


class LineError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    line_no: int                # 1-indexed
    col: int | None             # 1-indexed; None for whole-line
    kind: Literal[
        "bad-columns",
        "unknown-scheme",
        "missing-scheme",
        "bad-strength",
        "pydantic",
        "duplicate",
    ]
    # Structured context per kind (P3-Privacy-1: NO ref/filename/label fields):
    scheme: str | None = None      # unknown-scheme: the bad prefix
    got_kind: str | None = None    # bad-columns / bad-strength / pydantic
    expected: str | None = None    # bad-columns
    first_line: int | None = None  # duplicate: line where (ref,branch) first appeared
    field: str | None = None       # pydantic: which LoraEntry field failed
```

## 10. Error surfaces

| # | Where | Cause | Class | Exit |
|---|---|---|---|---|
| P3-E1 | `parse_loras_heredoc` | ≥1 line invalid (collect-all per D6) | `LorasParseError` | 1 (stderr `render_for_cli`) |
| P3-E2 | argparse | `--loras` passed twice | `parser.error` via `_LorasOnceAction` | 2 (argparse default) |
| P3-E3 | `resolve_active_lora_stack` | CLI overrides non-empty vault | none (WARNING only) | 0 (run continues) |
| P3-E4 | `resolve_active_lora_stack` | `cli_loras is None`, cfg + vault diverge | `LoraStackConflict` (P1) | 1 (unchanged) |
| P3-E5 | downstream (matcher / server) | CLI stack mismatches `--instance-id` pinned pod | `CapabilityKeyMismatch` (existing) | 1 (unchanged) |
| P3-E6 | server | `branch=auto` on MoE OR explicit branch on single-transformer | `BranchValidationError` (P2) | 1 (unchanged) |

## 11. Test scope

### 11.1 Parser unit tests — `tests/cli/test_loras_arg.py`

Tokenization + whitespace:
- `test_blank_lines_skipped`
- `test_hash_comment_line_skipped`
- `test_hash_with_leading_whitespace_skipped`
- `test_inline_hash_not_treated_as_comment`
- `test_tab_separator_accepted`
- `test_multiple_spaces_collapse`
- `test_trailing_cr_stripped`
- `test_empty_heredoc_returns_empty_list` (D9)
- `test_comments_only_heredoc_returns_empty_list` (D9)

Column count:
- `test_ref_only_defaults_strength_1_branch_auto`
- `test_ref_strength_defaults_branch_auto`
- `test_ref_strength_branch_all_three`
- `test_zero_tokens_after_strip_skipped`
- `test_four_tokens_raises_bad_columns`

Ref expansion (D5):
- `test_numeric_shorthand_expands_to_civitai`
- `test_civitai_full_ref_passes_through`
- `test_hf_ref_passes_through`
- `test_file_ref_passes_through`
- `test_https_ref_passes_through`
- `test_unknown_scheme_rejected_with_scheme_name`
- `test_missing_scheme_rejected`
- `test_numeric_shorthand_requires_both_ids`
- `test_numeric_shorthand_rejects_negative`

Strength parse + range:
- `test_strength_not_a_float_raises_bad_strength`
- `test_strength_out_of_range_raises_pydantic`
- `test_strength_at_bounds_accepted`
- `test_strength_inf_and_nan_rejected`

Branch parse + alias (P2 reuse):
- `test_branch_h_normalized_to_high_noise`
- `test_branch_l_normalized_to_low_noise`
- `test_branch_high_noise_explicit_accepted`
- `test_branch_low_noise_explicit_accepted`
- `test_branch_auto_accepted`
- `test_branch_unknown_value_raises_pydantic`

Duplicate detection (D8):
- `test_same_ref_same_branch_rejected_as_duplicate`
- `test_same_ref_different_branches_accepted`
- `test_same_ref_omitted_branch_twice_rejected`
- `test_duplicate_error_reports_both_line_numbers`

Aggregation (D6):
- `test_three_independent_errors_all_reported`
- `test_first_line_valid_subsequent_invalid_returns_all_invalid_errors`
- `test_error_report_preserves_line_order`
- `test_no_partial_list_returned_on_any_error`

### 11.2 Resolver tests — `tests/core/test_lora_resolver_p3.py`

- `test_cli_loras_none_falls_back_to_p1_path`
- `test_cli_loras_overrides_cfg_loras_when_vault_empty`
- `test_cli_loras_overrides_vault_loras_with_warning` (caplog)
- `test_cli_loras_empty_list_overrides_to_empty_stack` (D9)
- `test_cli_loras_skips_p1_d11_conflict_check`
- `test_cli_loras_refs_registered_with_redaction_registry`
- `test_cli_loras_warning_contains_no_ref_strings`
- `test_cli_loras_warning_fires_only_when_vault_nonempty`
- `test_cli_loras_redaction_registered_before_warning_emits` (ordering)

### 11.3 CLI command tests — `tests/cli/test_cmd_generate_loras.py`

- `test_no_loras_arg_resolver_called_with_cli_loras_none`
- `test_loras_arg_parsed_and_threaded_to_resolver`
- `test_loras_parse_error_renders_report_to_stderr_exit_1`
- `test_loras_double_use_argparse_errors_exit_2`
- `test_loras_with_instance_id_capability_mismatch_exits_1`
- `test_loras_with_dry_run_swap_prints_loras_source_cli`
- `test_loras_with_no_reuse_destroys_pod_after_run`

### 11.4 Redaction parity — `tests/test_lora_error_redaction.py`

- `test_line_error_has_no_ref_field` (AST scan over class body)
- `test_loras_parse_error_str_never_contains_ref`
- `test_render_for_cli_never_contains_ref` (force every error kind)
- `test_loras_parse_error_repr_never_contains_ref`

### 11.5 AST invariants

Extend `tests/test_no_unredacted_writes.py`:
- AC-P3-1: `src/kinoforge/cli/loras_arg.py` added to scanned-files set.
- AC-P3-2: `resolve_active_lora_stack` CLI branch counted as a
  redaction-aware write site (calls `RedactionRegistry.add_many` before
  any logging).
- AC-P3-3: `LineError` class declaration scanned — no `ref|filename|label`-named
  `str` fields.
- AC-P3-4: `render_for_cli` body does not interpolate the original heredoc text.

New file `tests/test_no_precedence_branches_outside_resolver.py`:
- AC-P3-5: AST scan over `src/kinoforge/` rejects any module (other than
  `cli/_commands.py::_cmd_generate`'s trivial parse-and-pass hop) that
  pattern-matches on `cli_loras is not None` or `args.loras is not None`
  followed by conditional cfg-vs-vault precedence logic.

### 11.6 Integration — `tests/integration/test_loras_cli_e2e.py`

- `test_end_to_end_cli_loras_override_cfg_drives_set_stack_request` (mock pod;
  assert wire body's `target` list carries CLI refs/strengths/branches in order).
- `test_cli_loras_warm_attach_swap_succeeds_when_only_lora_stack_differs`
  (matcher honors CLI stack via existing P1/P2 path).
- `test_cli_loras_capability_key_derivation_uses_cli_refs_not_cfg_refs`
  (CLI stack's refs+branches form the `WarmAttachKey` material).

### 11.7 Live smoke

DEFERRED. P3 adds no server-side surface. Post-merge manual fire on the
Tier-3 Wan 2.1 1.3B cfg using `kinoforge generate --loras ...` instead of
the cfg.loras path captures the wire-shape proof at zero net spend. Result
appended as a "See also" line under `successful-generations.md` entry #9.

## 12. Migration + commit sequence

1. **Land parser module (no behavior change).** New `src/kinoforge/cli/loras_arg.py`
   with `parse_loras_heredoc`, `LorasParseError`, `LorasParseReport`,
   `LineError`. Unit tests (§11.1) land in the same commit. No call site
   yet imports the parser.
2. **Land redaction parity tests + AST extensions.** §11.4 + §11.5
   (AC-P3-1 / AC-P3-3 / AC-P3-4). Red gate against the parser landed in
   step 1.
3. **Extend `resolve_active_lora_stack` with `cli_loras` kwarg.** Single
   atomic commit. Resolver tests (§11.2) land alongside. P1 + P2 existing
   tests stay green (call sites still pass no kwarg).
4. **Land precedence-single-source AST scan.** §11.5 AC-P3-5.
5. **Wire CLI surface.** `--loras` argument in `_main.py` + parse/thread
   hop in `_cmd_generate`. CLI command tests (§11.3) land in the same
   commit. AST scan AC-P3-2 extended.
6. **Wire `--dry-run-swap` `loras_source` line.** Small follow-up commit.
7. **Land integration tests.** §11.6. No live spend.
8. **Amend PROGRESS.md anchor.** Update the 2026-06-21 anchor's example
   heredoc from `1.0 1111:2222 h` to `1111:2222 1.0 h` (D2 column-order
   amendment). Add inline note: "Column order amended 2026-06-25 per P3
   spec D2 — `ref [strength] [branch]`."
9. **README section.** New subsection under `kinoforge generate` covering
   the `--loras` flag with one minimal + one full example + the column-order
   note + the precedence summary.
10. **Manual post-merge Tier-3 fire.** Use `--loras "$(cat <<'EOF' ...)" `
    instead of cfg.loras. Append "See also" line to
    `successful-generations.md` entry #9. No new live spend tracked under
    P3 budget.
11. **PROGRESS.md close-out.** P3 entry marked CLOSED with commit hash trail.
    Active workstream queue advances to Layer 5 (Bearer per-prediction cost
    capture).

## 13. Open questions for the implementation plan (writing-plans consumes)

- Task granularity for §12.1–§12.10 (each is ≥1 task; writing-plans decides
  the split).
- Whether the AC-P3-5 AST scan is its own file (`tests/test_no_precedence_branches_outside_resolver.py`)
  or an extension of `tests/test_no_unredacted_writes.py`. Recommendation:
  own file — different concern, different failure mode, different fix path.
- `_col_for_field` mapping in §6.1: which Pydantic `loc` tuples map to which
  CLI column index (ref=1, strength=2, branch=3). Trivial table; writing-plans
  decides whether to inline or factor out.

---

**End of P3 design spec. Approval gate: section-by-section approvals captured
during brainstorm 2026-06-25.**
