# LoRA ref accepts URLs (civitai, civarchive, huggingface)

## Status

Sub-project A of a two-spec decomposition. Sub-project B (civarchive
source module) is its own spec, pinned in `PROGRESS.md` as the
top-priority next workstream after this one ships.

## Problem

`LoraEntry.ref` accepts the canonical short forms only:
`civitai:<id>@<vid>` / `hf:<org>/<repo>:<file>`. Operators copy URLs
from civitai.com, civarchive.com, and huggingface.co; pasting them
into `--loras`, vault `loras:`, cfg `loras:`, or grid
`lora_swap.stack[].ref` requires manual translation into the short
form. Manual translation is the single biggest source of typos in
LoRA-bearing configs.

## Goal

Accept the three common URL shapes everywhere a LoRA ref is accepted,
normalize them to the canonical short form, and surface a clear
error on the one ambiguous case (civitai/civarchive URL missing the
`modelVersionId` query parameter).

## Non-goals

- Building a `civarchive` source module. Civarchive refs are
  parse-accepted by this spec but `civarchive:` resolution at download
  time is the next workstream (sub-project B).
- Auto-fetching "latest" version when `modelVersionId` is absent.
  Canonical refs are version-pinned; pinning is explicit.
- Resolving HuggingFace `tree/` URLs, branch refs other than `main`,
  or non-blob endpoints. Only `blob/main/<file>` and bare repo URLs
  in scope.
- Touching numeric-shorthand expansion in `parse_loras_heredoc`
  (`<id>:<vid>` → `civitai:<id>@<vid>`). Keeps working unchanged.

## Approach

Single chokepoint: a normalization function called from
`LoraEntry.ref`'s `mode="before"` field validator. Every site that
constructs a `LoraEntry` (cfg load, vault load, grid load, CLI
heredoc) gains URL acceptance automatically through pydantic
validation.

The CLI heredoc parser `parse_loras_heredoc` ALSO needs a tiny
change: `_KNOWN_SCHEMES` must add `civarchive`. The URL itself passes
through `_expand_ref` unchanged (scheme `https` already allowed); the
canonicalization happens later when `LoraEntry(ref=...)` validates.

The CLI `--loras` help text and the README's LoRA-source operator
docs gain a one-line "URLs from civitai.com, civarchive.com,
huggingface.co are accepted and normalized" note.

## URL → canonical-ref rules

| URL shape | Canonical |
|-----------|-----------|
| `https://civitai.com/models/<id>[/...]?[...&]modelVersionId=<vid>[&...]` | `civitai:<id>@<vid>` |
| `https://civarchive.com/models/<id>[/...]?[...&]modelVersionId=<vid>[&...]` | `civarchive:<id>@<vid>` |
| `https://huggingface.co/<org>/<repo>/blob/<branch>/<path-to-file>` | `hf:<org>/<repo>:<path-to-file>` |

(HuggingFace bare-repo URLs — `https://huggingface.co/<org>/<repo>` —
are NOT in scope; the operator must paste a `blob/<branch>/<file>`
URL so the canonical `hf:` ref pins a specific file.)

Behaviours:

- Scheme is case-insensitive on the URL (`HTTPS://...`); the canonical
  output is always lowercase.
- Trailing slashes and `?utm_*`/other query params are tolerated.
  Anchor fragments (`#...`) are stripped.
- HuggingFace branch in `blob/<branch>/` is captured but the canonical
  `hf:` ref does NOT encode branch today; warn-once at parse time when
  branch is anything other than `main` so an operator knows their
  branch pinning is being dropped.
- Civitai URL without `modelVersionId` → raise `ValueError`. Message:
  `"civitai URL missing required ?modelVersionId=... query parameter"`.
  No URL text in the message (privacy invariant — same posture as
  `LineError`).
- Civarchive URL without `modelVersionId` → same rule, same message
  shape (`"civarchive URL ..."`).
- Unknown URL host (e.g. `https://example.com/...`) → leave ref as-is
  (pass-through). Some downstream sources may still resolve a raw
  `https://` ref (the existing `http` source module). No regression.

## Change

```python
# core/lora.py

import re
from urllib.parse import urlparse, parse_qs

_CIVITAI_HOSTS = {"civitai.com", "www.civitai.com"}
_CIVARCHIVE_HOSTS = {"civarchive.com", "www.civarchive.com"}
_HF_HOSTS = {"huggingface.co", "www.huggingface.co"}

_CIVITAI_PATH = re.compile(r"^/models/(\d+)(?:/[^/]*)?/?$")
_HF_BLOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+?)/?$")
_HF_REPO_PATH = re.compile(r"^/([^/]+)/([^/]+)/?$")


def _normalize_ref(value: str) -> str:
    """Normalize a URL ref to canonical short form; leave non-URLs as-is."""
    if not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in _CIVITAI_HOSTS:
        return _normalize_civitai_like(parsed, "civitai")
    if host in _CIVARCHIVE_HOSTS:
        return _normalize_civitai_like(parsed, "civarchive")
    if host in _HF_HOSTS:
        return _normalize_hf(parsed)
    return value  # unknown host — pass through


def _normalize_civitai_like(parsed, scheme: str) -> str:
    m = _CIVITAI_PATH.match(parsed.path)
    if m is None:
        return parsed.geturl()  # unrecognised path — pass through
    model_id = m.group(1)
    qs = parse_qs(parsed.query)
    version_ids = qs.get("modelVersionId") or qs.get("modelversionid")
    if not version_ids:
        raise ValueError(
            f"{scheme} URL missing required ?modelVersionId=... query parameter"
        )
    return f"{scheme}:{model_id}@{version_ids[0]}"


def _normalize_hf(parsed) -> str:
    m = _HF_BLOB_PATH.match(parsed.path)
    if m is None:
        return parsed.geturl()  # not a blob URL — pass through
    org, repo, branch, file_path = m.groups()
    if branch != "main":
        logger.warning(
            "hf URL branch=%s dropped; canonical hf: ref does not encode "
            "branch (only `main` is pinned implicitly)",
            branch,
        )
    return f"hf:{org}/{repo}:{file_path}"
```

(The `_HF_REPO_PATH` regex is intentionally omitted — bare-repo URLs
are out of scope per the table above.)

`LoraEntry` gains a `@field_validator("ref", mode="before")` that
calls `_normalize_ref` before pydantic's `min_length=1` check.

```python
# cli/loras_arg.py — single addition
_KNOWN_SCHEMES = frozenset(
    {"civitai", "civarchive", "hf", "file", "https", "http"}
)
```

CLI help text in `cli/_main.py` `--loras` block: append
` URLs from civitai.com, civarchive.com, huggingface.co are
accepted and normalized to the canonical form.` to the existing
description.

## Tests (RED first)

`tests/core/test_lora.py` — new test module if it does not exist
already; otherwise extend:

1. `test_normalize_civitai_url_with_modelVersionId_query_param`
   - Input `"https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081"`
     → `"civitai:2197303@2474081"`.
   - Bug catch: pasting the user's exact example URL must produce
     the canonical form.

2. `test_normalize_civitai_url_tolerates_extra_query_params`
   - Input `"https://civitai.com/models/2197303?utm_source=x&modelVersionId=2474081"`
     → `"civitai:2197303@2474081"`.

3. `test_normalize_civitai_url_without_modelVersionId_raises`
   - Input `"https://civitai.com/models/2197303/arcane-style"` →
     `ValueError` containing `"missing required ?modelVersionId="`.
     URL text MUST NOT appear in the error message (privacy).

4. `test_normalize_civarchive_url_with_modelVersionId`
   - Input `"https://civarchive.com/models/2197303?modelVersionId=2474081"`
     → `"civarchive:2197303@2474081"`.

5. `test_normalize_civarchive_url_without_modelVersionId_raises`
   - Analog of #3 with `"civarchive URL"` in message.

6. `test_normalize_hf_blob_url_main_branch_no_warning`
   - Input `"https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors"`
     → `"hf:Org/Repo:sub/file.safetensors"`.
   - Use `caplog` to assert NO branch-drop warning.

7. `test_normalize_hf_blob_url_non_main_branch_warns_and_drops_branch`
   - Input with `blob/dev/file.safetensors` → returns
     `"hf:Org/Repo:file.safetensors"` AND emits one warning
     containing `"branch=dev dropped"`.

8. `test_normalize_hf_bare_repo_url_passthrough`
   - Input `"https://huggingface.co/Org/Repo"` → unchanged
     (returned verbatim). Bare-repo URLs are explicitly out of scope;
     this test pins that they do NOT silently turn into `hf:Org/Repo`.
     The operator gets the downstream resolver's "unknown ref" error
     so they know to paste the `blob/<branch>/<file>` URL instead.

9. `test_normalize_passthrough_canonical_civitai_ref_unchanged`
   - Input `"civitai:1234@5678"` → `"civitai:1234@5678"`.
   - Bug catch: validator must not mangle inputs that are already
     canonical.

10. `test_normalize_unknown_host_passthrough`
    - Input `"https://example.com/random/path"` → unchanged.
    - Bug catch: a validator that rejected every non-civitai/HF URL
      would regress operators using the existing `http` source.

11. `test_LoraEntry_construction_accepts_civitai_url`
    - `LoraEntry(ref="https://civitai.com/models/1@2?modelVersionId=3")`
      → `.ref == "civitai:1@2..."` wait — actually
      `"https://civitai.com/models/1?modelVersionId=3"` →
      `.ref == "civitai:1@3"`. End-to-end through pydantic, proves
      the validator wiring.

12. `test_known_schemes_includes_civarchive`
    - `from kinoforge.cli.loras_arg import _KNOWN_SCHEMES`;
      `assert "civarchive" in _KNOWN_SCHEMES`.
    - Bug catch: a CLI heredoc with `civarchive:111@222` must be
      parse-accepted at the heredoc layer; otherwise the user gets
      `unknown scheme` before LoraEntry ever runs.

13. `test_cli_loras_heredoc_accepts_civarchive_canonical`
    - `parse_loras_heredoc("civarchive:111@222\n")` →
      `[LoraEntry(ref="civarchive:111@222", ...)]` no errors.

14. `test_cli_loras_heredoc_accepts_civitai_url_and_normalizes`
    - `parse_loras_heredoc("https://civitai.com/models/111?modelVersionId=222\n")`
      → entry with canonical `ref="civitai:111@222"`.
    - Bug catch: heredoc → `_expand_ref` (passes through `https:`)
      → `LoraEntry(ref=URL)` → normalized. Full pipeline.

15. `test_cli_loras_heredoc_civitai_url_without_version_raises_LorasParseError`
    - Heredoc with bare civitai URL → `LorasParseError`. Error report
      must NOT contain the URL text (privacy invariant).

## Privacy

`_normalize_ref` ValueErrors never include the URL string; only the
scheme name plus the missing-param description. `LineError.kind`
remains `"pydantic"` for these failures (pydantic wraps the
ValueError), and `LineError` continues to carry no `ref`/`filename`
field — already enforced by `tests/test_lora_error_redaction.py` and
`tests/test_no_unredacted_writes.py` (the AST scans should still
pass; if they don't, the new validator's error path is leaking).

Once a URL normalizes successfully, the canonical ref is registered
with `RedactionRegistry` through the existing
`resolve_active_lora_stack` path — same posture as today.

## Risk

Low. The validator change is additive (URL input was previously
rejected by `min_length` / `civitai:` scheme; now normalized). The
CLI heredoc change is one entry added to a frozenset. No
runtime-resolution behavior changes for civitai/HF refs that already
worked. Civarchive refs become parse-accepted but unresolvable until
sub-project B lands — that's the intended interim state and is
called out in the bypass message at resolve time.

## Follow-up

- Sub-project B (civarchive source module) — its own spec; pinned in
  `PROGRESS.md` as the next workstream.
- README LoRA-source operator section gains a "URLs accepted" line in
  this spec's commit.
