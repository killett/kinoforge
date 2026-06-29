# LoRA URL Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Accept civitai / civarchive / huggingface URLs at every `LoraEntry.ref` accept-site and normalize them to the canonical short form.

**Architecture:** Single chokepoint `_normalize_ref` function in `src/kinoforge/core/lora.py`, wired to `LoraEntry.ref` via a `@field_validator("ref", mode="before")`. Every site that constructs a `LoraEntry` (cfg.loras, vault.loras, grid `lora_swap.stack[].ref`, `--loras` heredoc) gains URL acceptance through one validator. CLI heredoc parser also gains `civarchive` in its `_KNOWN_SCHEMES` frozenset so the new scheme is parse-accepted at the heredoc layer.

**Tech Stack:** Python 3.13, pydantic v2, stdlib `urllib.parse` (no new deps), pytest + caplog for log assertions.

**User decisions (already made):**
- URL forms in scope: civitai-with-version, HuggingFace blob, civarchive (`/superpowers-extended-cc:brainstorming` 2026-06-28).
- HuggingFace bare-repo URL: OUT of scope; passes through unchanged.
- Civitai/civarchive URL missing `?modelVersionId=`: REJECT with clear error (no URL text in the message — privacy invariant).
- Civarchive routing: treat as separate scheme (NOT alias to civitai). Source module is sub-project B, pinned in `PROGRESS.md`. This plan ships civarchive scheme as parse-accepted; resolution waits for sub-project B.
- Numeric shorthand `<id>:<vid>` → `civitai:<id>@<vid>` unchanged.
- README updates land in `docs/warm-reuse.md` (the operator doc for `--loras`); the top-level `README.md` does NOT itself document `--loras` accepted formats per the 2026-06-27 README rewrite.

---

## File structure

| File | Status | Responsibility |
|------|--------|----------------|
| `src/kinoforge/core/lora.py` | MODIFY | Add `_normalize_ref` + `@field_validator("ref", mode="before")` |
| `src/kinoforge/cli/loras_arg.py` | MODIFY | Add `"civarchive"` to `_KNOWN_SCHEMES` + update `missing-scheme` error suggestion |
| `src/kinoforge/cli/_main.py` | MODIFY | Append URL-acceptance note to `--loras` help text |
| `tests/core/test_lora_entry.py` | MODIFY | Add LoraEntry-level URL normalization tests |
| `tests/core/test_lora_url_normalize.py` | CREATE | Pure `_normalize_ref` unit tests |
| `tests/cli/test_loras_arg.py` | MODIFY | Add heredoc-layer civarchive + URL tests |
| `docs/warm-reuse.md` | MODIFY | One-paragraph note on URL acceptance |

---

### Task 1: `_normalize_ref` pure function + tests

**Goal:** Add `_normalize_ref(s: str) -> str` to `src/kinoforge/core/lora.py`. Pure function, no pydantic wiring yet, so tests can be small and focused.

**Files:**
- Modify: `src/kinoforge/core/lora.py` (add `_normalize_ref` + private helpers + module-level constants)
- Create: `tests/core/test_lora_url_normalize.py`

**Acceptance Criteria:**
- [ ] `_normalize_ref("civitai:1234@5678")` returns `"civitai:1234@5678"` (canonical passthrough).
- [ ] `_normalize_ref("https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081")` returns `"civitai:2197303@2474081"`.
- [ ] `_normalize_ref("https://civitai.com/models/2197303?utm_source=x&modelVersionId=2474081")` returns `"civitai:2197303@2474081"`.
- [ ] `_normalize_ref("https://civitai.com/models/2197303/arcane-style")` raises `ValueError` whose message contains `"missing required ?modelVersionId="` AND does NOT contain `"civitai.com"` (URL text not in message).
- [ ] `_normalize_ref("https://civarchive.com/models/2197303?modelVersionId=2474081")` returns `"civarchive:2197303@2474081"`.
- [ ] `_normalize_ref("https://civarchive.com/models/2197303")` raises `ValueError` whose message contains `"civarchive URL missing"` AND does NOT contain `"civarchive.com"`.
- [ ] `_normalize_ref("https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors")` returns `"hf:Org/Repo:sub/file.safetensors"` with NO warning emitted.
- [ ] `_normalize_ref("https://huggingface.co/Org/Repo/blob/dev/file.safetensors")` returns `"hf:Org/Repo:file.safetensors"` AND emits one WARNING on logger `kinoforge.core.lora` containing the substring `"branch=dev dropped"`.
- [ ] `_normalize_ref("https://huggingface.co/Org/Repo")` returns the input verbatim (bare-repo passthrough — explicitly out of scope).
- [ ] `_normalize_ref("https://example.com/random/path")` returns the input verbatim (unknown host passthrough).
- [ ] `_normalize_ref("HTTPS://Civitai.com/models/1?modelVersionId=2")` returns `"civitai:1@2"` (case-insensitive host + scheme).

**Verify:** `pixi run pytest tests/core/test_lora_url_normalize.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test file**

Create `tests/core/test_lora_url_normalize.py`:

```python
"""Unit tests for kinoforge.core.lora._normalize_ref (URL → canonical ref).

Privacy invariant pinned per spec §"Privacy": ValueErrors raised by
_normalize_ref must NOT include the URL text in the message. Only the
scheme name + missing-param description.
"""

from __future__ import annotations

import logging

import pytest

from kinoforge.core.lora import _normalize_ref


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Canonical refs pass through unchanged.
        ("civitai:1234@5678", "civitai:1234@5678"),
        ("civarchive:111@222", "civarchive:111@222"),
        ("hf:Org/Repo:file.safetensors", "hf:Org/Repo:file.safetensors"),
        ("hf:Org/Repo", "hf:Org/Repo"),
        ("file:/local/path.safetensors", "file:/local/path.safetensors"),
        # Civitai URL — the user's exact example.
        (
            "https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081",
            "civitai:2197303@2474081",
        ),
        # Tolerates extra query params + capitalised host/scheme.
        (
            "https://civitai.com/models/2197303?utm_source=x&modelVersionId=2474081",
            "civitai:2197303@2474081",
        ),
        (
            "HTTPS://Civitai.com/models/1?modelVersionId=2",
            "civitai:1@2",
        ),
        # Civarchive URL — the user's exact example.
        (
            "https://civarchive.com/models/2197303?modelVersionId=2474081",
            "civarchive:2197303@2474081",
        ),
        # HF blob URL — main branch, no warning expected.
        (
            "https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors",
            "hf:Org/Repo:sub/file.safetensors",
        ),
        # HF bare-repo URL — explicitly OUT of scope, passes through.
        (
            "https://huggingface.co/Org/Repo",
            "https://huggingface.co/Org/Repo",
        ),
        # Unknown host — passthrough.
        (
            "https://example.com/random/path",
            "https://example.com/random/path",
        ),
    ],
)
def test_normalize_ref(raw: str, expected: str) -> None:
    assert _normalize_ref(raw) == expected


def test_normalize_civitai_url_without_modelVersionId_raises() -> None:
    """Bug catch: bare model URL is ambiguous (could be any version). Reject
    with a clear error; the error message must NOT echo the URL itself."""
    with pytest.raises(ValueError) as excinfo:
        _normalize_ref("https://civitai.com/models/2197303/arcane-style")
    msg = str(excinfo.value)
    assert "civitai URL missing required ?modelVersionId=" in msg
    # Privacy invariant — URL text must NOT appear in the message.
    assert "civitai.com" not in msg
    assert "2197303" not in msg
    assert "arcane-style" not in msg


def test_normalize_civarchive_url_without_modelVersionId_raises() -> None:
    with pytest.raises(ValueError) as excinfo:
        _normalize_ref("https://civarchive.com/models/2197303")
    msg = str(excinfo.value)
    assert "civarchive URL missing required ?modelVersionId=" in msg
    assert "civarchive.com" not in msg
    assert "2197303" not in msg


def test_normalize_hf_blob_url_main_branch_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug catch: emitting a branch-drop warning on the common `main` path
    would spam every operator who pastes a normal HF URL."""
    caplog.set_level(logging.WARNING, logger="kinoforge.core.lora")
    out = _normalize_ref(
        "https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors"
    )
    assert out == "hf:Org/Repo:sub/file.safetensors"
    drop_warnings = [
        r for r in caplog.records if "branch=" in r.message and "dropped" in r.message
    ]
    assert not drop_warnings, (
        f"main branch must NOT trigger a drop warning; got: "
        f"{[r.message for r in drop_warnings]}"
    )


def test_normalize_hf_blob_url_non_main_branch_warns_and_drops_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug catch: silently dropping a non-main branch would surprise an
    operator who pinned a specific branch on purpose."""
    caplog.set_level(logging.WARNING, logger="kinoforge.core.lora")
    out = _normalize_ref(
        "https://huggingface.co/Org/Repo/blob/dev/file.safetensors"
    )
    # Canonical hf: ref doesn't encode branch — `main` is the implicit pin.
    assert out == "hf:Org/Repo:file.safetensors"
    drop_warnings = [
        r for r in caplog.records if "branch=dev dropped" in r.message
    ]
    assert len(drop_warnings) == 1, (
        f"expected exactly one branch-drop warning; got: "
        f"{[r.message for r in caplog.records]}"
    )
```

- [ ] **Step 2: Run tests — expect import error**

```bash
pixi run pytest tests/core/test_lora_url_normalize.py -v
```

Expected: collection FAIL with `ImportError: cannot import name '_normalize_ref' from 'kinoforge.core.lora'`.

- [ ] **Step 3: Implement `_normalize_ref` in `src/kinoforge/core/lora.py`**

Edit `src/kinoforge/core/lora.py`. At the top, alongside the existing imports, add:

```python
from urllib.parse import parse_qs, urlparse
```

Below the `logger = ...` line, before the `class LoraEntry` block, insert:

```python
_CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com"})
_CIVARCHIVE_HOSTS = frozenset({"civarchive.com", "www.civarchive.com"})
_HF_HOSTS = frozenset({"huggingface.co", "www.huggingface.co"})

_CIVITAI_LIKE_PATH = re.compile(r"^/models/(\d+)(?:/[^/]*)?/?$")
_HF_BLOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+?)/?$")


def _normalize_ref(value: str) -> str:
    """Normalize a URL-shaped LoRA ref to its canonical short form.

    Recognises:
      * civitai.com /models/<id>?...modelVersionId=<vid>... → civitai:<id>@<vid>
      * civarchive.com (same shape) → civarchive:<id>@<vid>
      * huggingface.co /<org>/<repo>/blob/<branch>/<file> → hf:<org>/<repo>:<file>

    Inputs already in canonical short form (``civitai:...``, ``hf:...``,
    ``file:...``, etc.) pass through unchanged. Unknown URL hosts pass
    through unchanged so the existing ``http`` source module still
    resolves them. HuggingFace bare-repo URLs are explicitly out of scope
    and pass through unchanged.

    Raises:
        ValueError: civitai or civarchive URL is missing the
            ``modelVersionId`` query parameter. The error message does
            NOT include the URL text (privacy invariant — same posture as
            ``LineError`` in ``cli/loras_arg.py``).
    """
    if not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in _CIVITAI_HOSTS:
        return _normalize_civitai_like(parsed, "civitai")
    if host in _CIVARCHIVE_HOSTS:
        return _normalize_civitai_like(parsed, "civarchive")
    if host in _HF_HOSTS:
        return _normalize_hf(parsed, original=value)
    return value


def _normalize_civitai_like(parsed: Any, scheme: str) -> str:
    """Shared rule for civitai + civarchive URLs (identical path shape)."""
    m = _CIVITAI_LIKE_PATH.match(parsed.path)
    if m is None:
        return parsed.geturl()
    model_id = m.group(1)
    qs = parse_qs(parsed.query)
    version_ids = qs.get("modelVersionId") or qs.get("modelversionid")
    if not version_ids:
        # Privacy: NO URL text in the message. Operator sees the rule,
        # not the data they pasted.
        raise ValueError(
            f"{scheme} URL missing required ?modelVersionId=... query "
            f"parameter (canonical refs are version-pinned)"
        )
    return f"{scheme}:{model_id}@{version_ids[0]}"


def _normalize_hf(parsed: Any, *, original: str) -> str:
    """Recognise HF blob URLs only; bare-repo and others pass through."""
    m = _HF_BLOB_PATH.match(parsed.path)
    if m is None:
        return original
    org, repo, branch, file_path = m.groups()
    if branch != "main":
        logger.warning(
            "hf URL branch=%s dropped; canonical hf: ref does not encode "
            "branch (only `main` is pinned implicitly)",
            branch,
        )
    return f"hf:{org}/{repo}:{file_path}"
```

Also add `import re` near the existing imports if not already present (it is not — check at top of file).

- [ ] **Step 4: Run tests — expect GREEN**

```bash
pixi run pytest tests/core/test_lora_url_normalize.py -v
```

Expected: all PASS.

- [ ] **Step 5: Pre-commit + commit**

```bash
git add src/kinoforge/core/lora.py tests/core/test_lora_url_normalize.py
pixi run pre-commit run --files src/kinoforge/core/lora.py tests/core/test_lora_url_normalize.py
git commit -m "feat(lora): _normalize_ref — URL → canonical ref (civitai/civarchive/hf)"
```

---

### Task 2: Wire `_normalize_ref` into `LoraEntry.ref` validator

**Goal:** Add a `@field_validator("ref", mode="before")` on `LoraEntry` so every site that constructs a `LoraEntry` (cfg, vault, grid, CLI) gains URL acceptance.

**Files:**
- Modify: `src/kinoforge/core/lora.py` (add validator method on `LoraEntry`)
- Modify: `tests/core/test_lora_entry.py` (add end-to-end LoraEntry-construction tests)

**Acceptance Criteria:**
- [ ] `LoraEntry(ref="https://civitai.com/models/111?modelVersionId=222")` yields `entry.ref == "civitai:111@222"`.
- [ ] `LoraEntry(ref="https://civarchive.com/models/111?modelVersionId=222")` yields `entry.ref == "civarchive:111@222"`.
- [ ] `LoraEntry(ref="https://huggingface.co/Org/Repo/blob/main/file.safetensors")` yields `entry.ref == "hf:Org/Repo:file.safetensors"`.
- [ ] `LoraEntry(ref="https://civitai.com/models/111")` raises `pydantic.ValidationError`; the textual representation does NOT contain `"civitai.com"`.
- [ ] `LoraEntry(ref="civitai:1234@5678")` (canonical) still works unchanged.
- [ ] Strength + branch + sha256 round-trip alongside a URL-normalized ref.

**Verify:** `pixi run pytest tests/core/test_lora_entry.py tests/core/test_lora_url_normalize.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Read current test_lora_entry.py shape**

```bash
pixi run python -c "import pathlib; print(pathlib.Path('tests/core/test_lora_entry.py').read_text()[:200])"
```

(Just to confirm import style + class names. Do not Read for analysis — use head.)

- [ ] **Step 2: Append the failing tests to `tests/core/test_lora_entry.py`**

Add at the bottom of the file:

```python
# ---------------------------------------------------------------------------
# URL → canonical normalization via the ref field validator
# (Sub-project A — see docs/superpowers/specs/2026-06-28-lora-url-normalization-design.md)
# ---------------------------------------------------------------------------

import pydantic
import pytest

from kinoforge.core.lora import LoraEntry


def test_LoraEntry_normalizes_civitai_url_with_modelVersionId() -> None:
    entry = LoraEntry(
        ref="https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081",
        strength=0.5,
        branch="high_noise",
    )
    assert entry.ref == "civitai:2197303@2474081"
    assert entry.strength == 0.5
    assert entry.branch == "high_noise"


def test_LoraEntry_normalizes_civarchive_url() -> None:
    entry = LoraEntry(
        ref="https://civarchive.com/models/2197303?modelVersionId=2474081",
    )
    assert entry.ref == "civarchive:2197303@2474081"


def test_LoraEntry_normalizes_hf_blob_url() -> None:
    entry = LoraEntry(
        ref="https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors",
    )
    assert entry.ref == "hf:Org/Repo:sub/file.safetensors"


def test_LoraEntry_rejects_civitai_url_without_modelVersionId() -> None:
    """Bug catch: the validator must surface the ambiguity, AND the
    pydantic ValidationError text must NOT echo the URL (privacy)."""
    with pytest.raises(pydantic.ValidationError) as excinfo:
        LoraEntry(
            ref="https://civitai.com/models/2197303/arcane-style",
        )
    text = str(excinfo.value)
    assert "civitai URL missing required ?modelVersionId=" in text
    # Privacy invariant — URL text must NOT appear in the ValidationError.
    assert "civitai.com" not in text
    assert "2197303" not in text
    assert "arcane-style" not in text


def test_LoraEntry_canonical_ref_passes_through_unchanged() -> None:
    entry = LoraEntry(ref="civitai:1234@5678")
    assert entry.ref == "civitai:1234@5678"
```

- [ ] **Step 3: Run tests — expect FAIL (validator not wired yet)**

```bash
pixi run pytest tests/core/test_lora_entry.py -v -k "normalize or rejects_civitai"
```

Expected: 4 FAIL — `pydantic.ValidationError: ... ref ... String should have at least 1 character` is NOT the error; the URL string passes `min_length=1` but does not get normalized. Tests asserting canonical form will fail.

- [ ] **Step 4: Add the field validator on `LoraEntry`**

In `src/kinoforge/core/lora.py`, inside the `LoraEntry` class, ABOVE the existing `_normalize_branch_alias` validator, insert:

```python
    @field_validator("ref", mode="before")
    @classmethod
    def _normalize_url_ref(cls, v: Any) -> Any:  # noqa: ANN401
        """Normalize URL-shaped refs to canonical short form.

        Runs ``mode="before"`` so pydantic's ``min_length=1`` check sees
        the post-normalization value. See module-level ``_normalize_ref``
        for the URL → canonical rules and the privacy invariant on raised
        errors.
        """
        if isinstance(v, str):
            return _normalize_ref(v)
        return v
```

- [ ] **Step 5: Run tests — expect GREEN**

```bash
pixi run pytest tests/core/test_lora_entry.py tests/core/test_lora_url_normalize.py -v
```

Expected: all PASS.

- [ ] **Step 6: Regression sweep on LoRA tests**

```bash
pixi run pytest tests/core/test_lora_entry.py tests/core/test_lora_resolve.py tests/core/test_lora_resolver_p3.py tests/test_lora_error_redaction.py tests/test_lora_schema_parity.py tests/test_no_unredacted_writes.py -v
```

Expected: all PASS. The privacy-invariant AST scans (`test_lora_error_redaction.py`, `test_no_unredacted_writes.py`) MUST still pass — if they fail, the new validator's error path is leaking and the fix is to revisit the error message in `_normalize_civitai_like`.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/lora.py tests/core/test_lora_entry.py
pixi run pre-commit run --files src/kinoforge/core/lora.py tests/core/test_lora_entry.py
git commit -m "feat(lora): wire _normalize_ref into LoraEntry.ref validator"
```

---

### Task 3: Add `civarchive` to CLI heredoc `_KNOWN_SCHEMES`

**Goal:** `--loras` heredoc accepts both `civarchive:111@222` canonical refs AND `https://civarchive.com/...` URLs. Without this change the heredoc's `_expand_ref` would error with `unknown scheme: civarchive` before `LoraEntry` ever runs.

**Files:**
- Modify: `src/kinoforge/cli/loras_arg.py` (extend `_KNOWN_SCHEMES`, update `missing-scheme` suggestion)
- Modify: `tests/cli/test_loras_arg.py` (add civarchive + URL tests)

**Acceptance Criteria:**
- [ ] `parse_loras_heredoc("civarchive:111@222\n")` returns `[LoraEntry(ref="civarchive:111@222", ...)]` with no errors.
- [ ] `parse_loras_heredoc("https://civitai.com/models/111?modelVersionId=222\n")` returns one entry whose `.ref == "civitai:111@222"` (URL flows through the heredoc layer and lands in `LoraEntry`'s validator).
- [ ] `parse_loras_heredoc("https://civitai.com/models/111\n")` raises `LorasParseError`. The aggregated `LorasParseReport.errors[0].kind == "pydantic"`, `.field == "ref"`. `render_for_cli()` output does NOT contain `"civitai.com"` or `"111"` (privacy).
- [ ] Existing `missing-scheme` error message lists `civarchive` alongside `civitai`/`hf`/`file`.
- [ ] `"civarchive" in _KNOWN_SCHEMES`.

**Verify:** `pixi run pytest tests/cli/test_loras_arg.py tests/cli/test_cmd_generate_loras.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Append failing tests to `tests/cli/test_loras_arg.py`**

```python
# ---------------------------------------------------------------------------
# Civarchive scheme + URL-form acceptance
# (Sub-project A — see docs/superpowers/specs/2026-06-28-lora-url-normalization-design.md)
# ---------------------------------------------------------------------------

from kinoforge.cli.loras_arg import (
    _KNOWN_SCHEMES,
    LorasParseError,
    parse_loras_heredoc,
)


def test_known_schemes_includes_civarchive() -> None:
    """Bug catch: a heredoc with `civarchive:111@222` would error
    `unknown scheme` at the heredoc layer, before LoraEntry's validator
    ever runs."""
    assert "civarchive" in _KNOWN_SCHEMES


def test_heredoc_accepts_civarchive_canonical_ref() -> None:
    entries = parse_loras_heredoc("civarchive:111@222\n")
    assert len(entries) == 1
    assert entries[0].ref == "civarchive:111@222"


def test_heredoc_accepts_civitai_url_and_normalizes() -> None:
    """URL flows through _expand_ref (https scheme already allowed),
    then LoraEntry's validator canonicalises."""
    text = "https://civitai.com/models/111?modelVersionId=222\n"
    entries = parse_loras_heredoc(text)
    assert len(entries) == 1
    assert entries[0].ref == "civitai:111@222"


def test_heredoc_accepts_civarchive_url_and_normalizes() -> None:
    text = "https://civarchive.com/models/111?modelVersionId=222\n"
    entries = parse_loras_heredoc(text)
    assert len(entries) == 1
    assert entries[0].ref == "civarchive:111@222"


def test_heredoc_accepts_hf_blob_url_and_normalizes() -> None:
    text = "https://huggingface.co/Org/Repo/blob/main/file.safetensors 0.8\n"
    entries = parse_loras_heredoc(text)
    assert len(entries) == 1
    assert entries[0].ref == "hf:Org/Repo:file.safetensors"
    assert entries[0].strength == 0.8


def test_heredoc_civitai_url_without_version_raises_LorasParseError_privacy() -> None:
    """Pydantic ValidationError is wrapped into LineError(kind='pydantic').
    Privacy invariant: the rendered report MUST NOT echo the URL or any
    component of it."""
    text = "https://civitai.com/models/111\n"
    with pytest.raises(LorasParseError) as excinfo:
        parse_loras_heredoc(text)
    report = excinfo.value.report
    assert len(report.errors) == 1
    assert report.errors[0].kind == "pydantic"
    assert report.errors[0].field == "ref"
    rendered = report.render_for_cli()
    # URL components must NOT appear anywhere in the rendered report.
    assert "civitai.com" not in rendered
    assert "111" not in rendered
    assert "https://" not in rendered
```

(`pytest` is already imported at the top of the file. Verify with `grep` if unsure.)

- [ ] **Step 2: Run — expect FAIL**

```bash
pixi run pytest tests/cli/test_loras_arg.py -v -k "civarchive or url_and_normalizes or hf_blob_url or url_without_version"
```

Expected: tests for `civarchive` scheme + URL-bearing heredoc lines FAIL with `unknown scheme: civarchive` or `unknown scheme: https` (the latter passes today but the URL normalization isn't applied without Task 2 wiring — Task 2 must be merged first).

- [ ] **Step 3: Edit `src/kinoforge/cli/loras_arg.py`**

Find:

```python
_KNOWN_SCHEMES = frozenset({"civitai", "hf", "file", "https", "http"})
```

Replace with:

```python
_KNOWN_SCHEMES = frozenset(
    {"civitai", "civarchive", "hf", "file", "https", "http"}
)
```

Find the `_format_one` branch for `missing-scheme`:

```python
    if err.kind == "missing-scheme":
        return (
            f"{loc}: missing scheme (use `civitai:`, `hf:`, `file:`, or "
            f"numeric `<modelId>:<versionId>`)"
        )
```

Replace with:

```python
    if err.kind == "missing-scheme":
        return (
            f"{loc}: missing scheme (use `civitai:`, `civarchive:`, "
            f"`hf:`, `file:`, or numeric `<modelId>:<versionId>`)"
        )
```

- [ ] **Step 4: Run — expect GREEN**

```bash
pixi run pytest tests/cli/test_loras_arg.py tests/cli/test_cmd_generate_loras.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/loras_arg.py tests/cli/test_loras_arg.py
pixi run pre-commit run --files src/kinoforge/cli/loras_arg.py tests/cli/test_loras_arg.py
git commit -m "feat(cli): --loras heredoc accepts civarchive scheme + URL refs"
```

---

### Task 4: CLI `--loras` help text + `docs/warm-reuse.md` operator note

**Goal:** Surface URL acceptance to operators reading CLI `--help` and the warm-reuse doc.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (`--loras` help string near line 499)
- Modify: `docs/warm-reuse.md` (operator-facing note in the `--loras` section)
- Modify: `tests/cli/test_cmd_generate_loras.py` (extend `test_loras_help_includes_loras_arg`)

**Acceptance Criteria:**
- [ ] `pixi run kinoforge generate --help` output includes the substring `"URLs from civitai.com, civarchive.com, huggingface.co"`.
- [ ] `docs/warm-reuse.md` `--loras` section gains a paragraph documenting URL acceptance, the civarchive caveat (parse-accepted; resolution requires sub-project B), and the `modelVersionId` requirement for civitai/civarchive URLs.
- [ ] Updated test in `test_cmd_generate_loras.py` asserts the new help substring is present.

**Verify:** `pixi run pytest tests/cli/test_cmd_generate_loras.py::test_loras_help_includes_loras_arg -v` → PASS; `pixi run kinoforge generate --help | grep -F 'URLs from civitai.com'` → exit 0.

**Steps:**

- [ ] **Step 1: Update the help-string test**

In `tests/cli/test_cmd_generate_loras.py`, find `test_loras_help_includes_loras_arg`. After its existing assertions, add:

```python
    # Sub-project A — operators should see that URL paste is supported.
    assert "URLs from civitai.com, civarchive.com, huggingface.co" in help_text
```

(Variable name is `help_text` per the existing test; verify with `grep` if needed.)

- [ ] **Step 2: Run — expect FAIL**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py::test_loras_help_includes_loras_arg -v
```

Expected: FAIL — substring not found.

- [ ] **Step 3: Update `--loras` help in `src/kinoforge/cli/_main.py`**

Find the existing help text:

```python
            "branch defaults to `auto`. Empty heredoc clears the stack for "
            "this run. Vault.loras bypass logged to stderr."
```

Append before the closing `)`:

```python
            "branch defaults to `auto`. Empty heredoc clears the stack for "
            "this run. Vault.loras bypass logged to stderr. URLs from "
            "civitai.com, civarchive.com, huggingface.co are accepted and "
            "normalized to the canonical form; civitai/civarchive URLs "
            "MUST include `?modelVersionId=...` (canonical refs are "
            "version-pinned). civarchive refs are parse-accepted but their "
            "downstream resolver is pending."
```

- [ ] **Step 4: Run — expect GREEN**

```bash
pixi run pytest tests/cli/test_cmd_generate_loras.py::test_loras_help_includes_loras_arg -v
```

Expected: PASS.

- [ ] **Step 5: Update `docs/warm-reuse.md`**

Find the section that documents `--loras` (line ~92: `### kinoforge generate --loras — CLI LoRA stack override`). After the existing description paragraph, insert:

```markdown
**URL paste supported.** As of 2026-06-28 you can paste full URLs in
place of the canonical short ref, both at the `--loras` heredoc and
inside cfg / vault / grid `loras:` blocks:

| URL shape | Canonical |
|-----------|-----------|
| `https://civitai.com/models/<id>/...?modelVersionId=<vid>` | `civitai:<id>@<vid>` |
| `https://civarchive.com/models/<id>?modelVersionId=<vid>` | `civarchive:<id>@<vid>` |
| `https://huggingface.co/<org>/<repo>/blob/main/<file>` | `hf:<org>/<repo>:<file>` |

Civitai and civarchive URLs MUST carry `?modelVersionId=...`;
canonical refs are version-pinned and a bare model URL is rejected
with `civitai URL missing required ?modelVersionId=... query
parameter`. HuggingFace branches other than `main` are dropped with a
warn-once (the canonical `hf:` ref does not encode branch). Bare HF
repo URLs (`huggingface.co/<org>/<repo>`) are NOT normalized — paste
the `blob/<branch>/<file>` URL instead.

`civarchive:<id>@<vid>` is parse-accepted by this release but the
downstream resolver is the next workstream (see `PROGRESS.md` →
"NEXT SESSION — TOP PRIORITY"). Until that ships, civarchive refs
will fail at resolution time with a clear "civarchive source not yet
implemented" error.
```

- [ ] **Step 6: Final regression sweep**

```bash
pixi run pytest tests/core/test_lora_entry.py tests/core/test_lora_url_normalize.py tests/core/test_lora_resolve.py tests/cli/test_loras_arg.py tests/cli/test_cmd_generate_loras.py tests/test_lora_error_redaction.py tests/test_no_unredacted_writes.py tests/test_lora_schema_parity.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/cli/_main.py docs/warm-reuse.md tests/cli/test_cmd_generate_loras.py
pixi run pre-commit run --files src/kinoforge/cli/_main.py docs/warm-reuse.md tests/cli/test_cmd_generate_loras.py
git commit -m "docs(lora): document --loras URL acceptance in CLI help + warm-reuse doc"
```

---

### Task 5: PROGRESS.md close-out + workstream-CLOSED entry

**Goal:** Mark sub-project A SHIPPED in `PROGRESS.md` so the next session immediately picks up sub-project B (civarchive source module) per the pinned TOP PRIORITY note.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] "Active workstream" entry for sub-project A is replaced with a SHIPPED summary listing the four implementation commits (Tasks 1–4) + a pointer to the spec.
- [ ] "NEXT SESSION — TOP PRIORITY" block (sub-project B) is preserved verbatim — it must remain the active resume target.
- [ ] One new dated "SHIPPED" block above the existing 2026-06-28 sweeper-side ephemeral-reap block, in the same format used by prior workstreams.

**Verify:** `git log --oneline -10 | head` shows the close-out commit AND `head -50 PROGRESS.md` shows the SHIPPED block at the top of the SHIPPED list with TOP PRIORITY block intact.

**Steps:**

- [ ] **Step 1: Read current PROGRESS.md state via grep**

```bash
rg -n "Active workstream|NEXT SESSION|SHIPPED 2026-06-28" PROGRESS.md
```

- [ ] **Step 2: Edit `PROGRESS.md`**

Replace the entire `## Active workstream` block (the one currently containing "LoRA URL normalization (Sub-project A) — IN PROGRESS") with:

```markdown
## Active workstream

**No active workstream — next initiative TBD.** See "NEXT SESSION —
TOP PRIORITY" below.

---

**LoRA URL normalization (Sub-project A) SHIPPED 2026-06-28 (commits `<C1>..<C4>`, all 4 tasks GREEN).**
Spec `docs/superpowers/specs/2026-06-28-lora-url-normalization-design.md` +
plan `docs/superpowers/plans/2026-06-28-lora-url-normalization.md`.
`LoraEntry.ref` now accepts pasted URLs from civitai.com,
civarchive.com, and huggingface.co; URLs are normalized to the
canonical short form by a `mode=before` field validator so cfg.loras,
vault.loras, `--loras` heredoc, and grid `lora_swap.stack[].ref` all
inherit URL acceptance through one chokepoint. Civitai/civarchive URLs
without `?modelVersionId=...` are rejected with a privacy-respecting
error (no URL text in the message). HF non-`main` branches drop with a
warn-once. `civarchive` added to the CLI heredoc `_KNOWN_SCHEMES`.
Tasks:
- Task 1 — `_normalize_ref` pure function + unit tests (`<C1>`)
- Task 2 — wire validator into `LoraEntry.ref` (`<C2>`)
- Task 3 — civarchive in CLI `_KNOWN_SCHEMES` (`<C3>`)
- Task 4 — CLI help + `docs/warm-reuse.md` URL-paste note (`<C4>`)
**Workstream CLOSED — pasting URLs into LoRA configs now works
end-to-end for civitai + hf. Civarchive URLs are parse-accepted;
resolution waits for Sub-project B (see TOP PRIORITY below).**
```

Substitute `<C1>..<C4>` with the actual commit hashes after the commits land (use `git log --oneline -5`).

The "NEXT SESSION — TOP PRIORITY" block must remain intact below.

- [ ] **Step 3: Commit**

```bash
git add PROGRESS.md
pixi run pre-commit run --files PROGRESS.md
git commit -m "docs(progress): SHIPPED — LoRA URL normalization (sub-project A)"
```

---

## Self-Review

**Spec coverage:**
- §"URL → canonical-ref rules" table → Tasks 1 (pure function) + 2 (validator wiring) cover civitai/civarchive/HF; HF bare-repo passthrough explicitly tested.
- §"Change" code blocks → Tasks 1 (function) + 2 (validator) + 3 (`_KNOWN_SCHEMES`) + 4 (help text) cover every change.
- §"Tests (RED first)" — every numbered test in the spec maps to a Task's acceptance criteria. Spec test #15 (heredoc URL-without-version raises) is Task 3's `test_heredoc_civitai_url_without_version_raises_LorasParseError_privacy`.
- §"Privacy" → Task 1 (function-level `ValueError` privacy) + Task 2 (pydantic ValidationError privacy via AST scan regression in Step 6) + Task 3 (rendered `LorasParseReport` privacy).
- §"Risk" → mitigations live in Task 2 Step 6's regression sweep (privacy AST scans) and Task 4 Step 6's final sweep.
- §"Follow-up" → Task 5 records the workstream close-out and preserves the TOP PRIORITY pointer for Sub-project B.

**Placeholder scan:** none. Every step shows the exact code/command. Task 5 has `<C1>..<C4>` placeholders for commit hashes — these are filled at execute time from `git log`, which is the correct moment for them.

**Type consistency:**
- `_normalize_ref(value: str) -> str` — consistent across Task 1 (definition), Task 2 (validator call site).
- `_KNOWN_SCHEMES` — frozenset of str, consistent.
- `LoraEntry.ref` — field name matches across all tests + the spec.
- `_normalize_civitai_like` / `_normalize_hf` helper names match the spec's code block and Task 1's implementation step.

No user-gate tags assigned — sub-project A is mechanical refactor with strong unit + privacy-AST test coverage; no live-spend gates needed. (Sub-project B will need them for the civarchive download smoke.)