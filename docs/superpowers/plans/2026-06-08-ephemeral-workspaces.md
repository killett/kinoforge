# Prompt + LoRA confidentiality (vault + `--ephemeral`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the always-on content-confidentiality policy + `--ephemeral` flag described in the spec. Prompts and LoRA refs/filenames/labels/derived-hashes never appear on local disk outside the user's vault file or in any committed YAML; `--ephemeral` additionally deletes the run from provider dashboards (best-effort) and from local disk except the user-configured output dir.

**Architecture:** Vault loader + RedactionRegistry singleton + RedactingLogFilter on root logger (Sub-α). Canonical write-site pattern (`session = EphemeralSession.current(); if session and not session.policy.<gate>: shadow; return; payload = registry.redact_json(...); store.put_json(...)`) applied at every persistent-write site; ArtifactStore gains `delete_run` + `manual_cleanup_command`; `OutputSink.publish` registers basename; opaque sha256-derived names at every `put_bytes`; opaque-when-local LoRA download cache (Sub-β). `EphemeralSession` context manager via `contextvars` with `EphemeralPolicy` toggling each gate (Sub-γ). Hosted-engine `_delete_with_retries` on `RemoteSubmitPollBackend` with per-engine implementations; `EPHEMERAL_CAPABILITIES` pre-flight table refuses fal (Sub-δ). AST-based `tests/test_no_unredacted_writes.py` invariant fails any merge that bypasses the pattern (Sub-ε). E2E integration tests anchor "only the output dir survives under ephemeral" and "logs redact the prompt slug after publish."

**Tech Stack:** Python 3.11+, pydantic v2 for vault models, stdlib `contextvars` for EphemeralSession propagation through `ConcurrentPool`, stdlib `logging.Filter` for redaction, stdlib `ast` + `pathlib` for the CI invariant. No new runtime deps beyond what's already in `pyproject.toml`.

**Spec:** `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md` (committed `2396788`).

---

## File map

**Create:**
- `src/kinoforge/core/secret.py` — `Secret` lightweight newtype used only at vault→orchestrator→engine boundary (Task 1).
- `src/kinoforge/core/redaction.py` — `RedactionRegistry` singleton + `RedactingLogFilter` (Tasks 2, 3).
- `src/kinoforge/core/vault.py` — `Vault` pydantic models + `load_vault` + `compute_profile_alias` + path validation (Task 4).
- `src/kinoforge/core/artifacts.py` — `opaque_store_name(payload, ext) -> str` (Task 5).
- `src/kinoforge/core/ephemeral.py` — `EphemeralPolicy`, `EphemeralSession`, `EPHEMERAL_CAPABILITIES` (Task 14).
- `tests/core/test_secret.py` — 5 tests pinning Secret type (Task 1).
- `tests/core/test_redaction.py` — 12 tests pinning registry + log filter (Tasks 2, 3).
- `tests/core/test_vault.py` — 14 tests pinning vault load + validation + alias (Task 4).
- `tests/core/test_artifacts.py` — 4 tests pinning opaque_store_name (Task 5).
- `tests/core/test_ephemeral.py` — 10 tests pinning EphemeralSession (Task 14).
- `tests/core/test_ledger_redaction.py` — 6 tests pinning canonical pattern on Ledger (Task 8).
- `tests/core/test_profile_cache_redaction.py` — 7 tests pinning alias-keyed cache (Task 9).
- `tests/core/test_batch_summary_skipped.py` — 4 tests pinning batch summary skip (Task 10).
- `tests/core/test_ephemeral_run_cleanup.py` — 5 tests pinning __exit__ store cleanup (Task 15).
- `tests/stores/test_delete_run.py` — 9 tests pinning delete_run + manual_cleanup_command across all three stores (Tasks 6, 7).
- `tests/pipeline/test_opaque_store_name.py` — 4 tests pinning GenerateClipStage opaque names (Task 13).
- `tests/engines/test_delete_on_completion.py` — 12 tests pinning hosted delete (Tasks 16, 17).
- `tests/engines/test_fal_ephemeral_refused.py` — 2 tests pinning fal refusal (Task 17).
- `tests/engines/test_fixture_capture_refused.py` — 4 tests pinning _save_fixture refusal (Task 13).
- `tests/cli/test_preflight_ephemeral.py` — 5 tests pinning pre-flight gate (Task 18).
- `tests/cli/test_flags_validation.py` — 3 tests pinning flag exclusion (Task 18).
- `tests/integration/test_ephemeral_only_output_dir_survives.py` — E2E proof-point (Task 20). **USER-GATE.**
- `tests/integration/test_logging_filter_e2e.py` — E2E captured-stderr-has-no-prompt (Task 20). **USER-GATE.**
- `tests/integration/test_output_filename_redacted_in_logs.py` — E2E filename redaction (Task 20). **USER-GATE.**
- `tests/test_no_unredacted_writes.py` — AST-based CI invariant; 6 ACs (Task 19). **USER-GATE.**
- `examples/vault/example.yaml` — template vault file (Task 21).

**Modify:**
- `src/kinoforge/core/errors.py` — add `VaultError`, `VaultPathError`, `VaultUnderRepoError`, `VaultParseError`, `VaultEmptyError`, `EphemeralError`, `EphemeralDeleteUnsupportedError`, `EphemeralDeleteHTTPError`, `EphemeralDeleteFailedError`, `EphemeralStoreCleanupFailedError` (Tasks 4, 14, 16).
- `src/kinoforge/core/lifecycle.py` — `Ledger.record` + `Ledger.touch` canonical pattern; `Ledger.forget` removes from both store and in-memory shadow (Task 8).
- `src/kinoforge/core/profiles.py` — `JsonProfileCache.resolve_or_discover` accepts `alias` param; `_persist` canonical pattern; `name` field persisted as alias (Task 9).
- `src/kinoforge/core/batch.py` — `_write_summary` canonical pattern (skip under ephemeral) (Task 10).
- `src/kinoforge/core/downloader.py` — `download(opaque_name=False)` parameter; sha256-required path; registers filename with `RedactionRegistry` on resolve (Task 12).
- `src/kinoforge/stores/base.py` — `ArtifactStore.delete_run` + `manual_cleanup_command` abstract methods (Task 6).
- `src/kinoforge/stores/local.py` — `LocalArtifactStore.delete_run` + `manual_cleanup_command` impls (Task 6).
- `src/kinoforge/stores/s3.py` — `S3ArtifactStore.delete_run` + `manual_cleanup_command` impls (Task 7).
- `src/kinoforge/stores/gcs.py` — `GCSArtifactStore.delete_run` + `manual_cleanup_command` impls (Task 7).
- `src/kinoforge/stores/sinks.py` — `LocalOutputSink.publish` registers basename with `RedactionRegistry` (Task 11).
- `src/kinoforge/pipeline/generate_clip.py` — `put_bytes` uses `opaque_store_name`; registers store with `EphemeralSession`; consults `EphemeralSession` for path selection (Task 13).
- `src/kinoforge/engines/replicate/__init__.py` — `_delete` impl + `manual_cleanup_url` classmethod; `_save_fixture` registry check (Tasks 13, 17).
- `src/kinoforge/engines/runway/__init__.py` — `_delete` + `manual_cleanup_url`; `_save_fixture` registry check (Tasks 13, 17).
- `src/kinoforge/engines/fal/__init__.py` — `_delete` raises `EphemeralDeleteUnsupportedError`; `_save_fixture` registry check (Tasks 13, 17).
- `src/kinoforge/engines/hosted/__init__.py` — `_save_fixture` registry check; pre-flight capability key (Task 13).
- `src/kinoforge/engines/comfyui/__init__.py` — `_save_fixture` registry check (Task 13).
- `src/kinoforge/engines/diffusers/__init__.py` — `_save_fixture` registry check (Task 13).
- `src/kinoforge/engines/fake/__init__.py` — `_save_fixture` registry check (Task 13).
- `src/kinoforge/engines/hosted/remote_submit.py` — `RemoteSubmitPollBackend._delete` abstract + `_delete_with_retries` concrete (Task 16).
- `src/kinoforge/core/orchestrator.py` — `deploy_session` wraps `EphemeralSession`; threads vault into `GenerationRequest`; registers store with session; pod name + tags consult policy (Task 15).
- `src/kinoforge/core/lifecycle.py` (second pass) — pod name + tags consult `EphemeralSession.current()` (Task 15).
- `src/kinoforge/cli.py` — `--vault PATH`, `--ephemeral`, `--debug-show-secrets` flags; load vault; install `RedactingLogFilter` on root logger; pre-flight gate; wrap `_cmd_generate` / `_cmd_batch` / `_cmd_deploy` in `EphemeralSession` (Task 18).
- `DESIGN.md` — "Privacy boundary" section (Task 21).
- `PROGRESS.md` — Phase 45 entry with α–ε SHAs (Task 21).

**Untouched (anchors for backward-compat):**
- All existing tests (~750) must pass at every sub-merge boundary.
- `src/kinoforge/SPEC.md` — no ABC field changes per architecture choice C.
- `README.md` — no change (per user instruction during brainstorm).
- `CLAUDE.md` — no change (vault is per-run, not per-session).
- All existing `examples/configs/*.yaml` — public-by-design, untouched.
- `prompt-field-realistic.txt` / `prompt-field-dreamlike.txt` — public-by-design, untouched.

---

## Sub-merge sequencing

Each sub spans the listed tasks. ε MUST land last so its assertions are clean — it would fail mid-β.

| Sub | Tasks | Description |
|---|---|---|
| **α** foundation | 1, 2, 3, 4, 5 | Secret, RedactionRegistry, RedactingLogFilter, Vault, opaque_store_name. No write-site retrofits yet. |
| **β** sink retrofit | 6, 7, 8, 9, 10, 11, 12, 13 | ArtifactStore.delete_run + manual_cleanup_command, canonical pattern at every persistent-write site, OutputSink.publish registers basename, Downloader opaque_name, GenerateClipStage uses opaque_store_name, per-engine _save_fixture check. |
| **γ** ephemeral | 14, 15 | EphemeralSession + EphemeralPolicy + EPHEMERAL_CAPABILITIES; __exit__ store cleanup; pod naming. |
| **δ** hosted delete | 16, 17 | RemoteSubmitPollBackend._delete + per-engine impls + error UX. |
| **CLI** | 18 | --vault, --ephemeral, --debug-show-secrets, pre-flight gate, session wrap. |
| **ε** CI invariant | 19 | tests/test_no_unredacted_writes.py with all 6 ACs. |
| **integration + docs** | 20, 21 | E2E integration tests; example vault; DESIGN.md; PROGRESS.md. |

---

### Task 1: `core/secret.py` — Secret lightweight newtype

**Goal:** Add a thin `Secret` wrapper used at the vault loader → orchestrator → engine seam to make unwrap sites explicit. The ABCs (`GenerationRequest.prompt`, `Segment.prompt`, etc.) stay `str` per architecture choice C; `Secret` is only carried inside the narrow path from vault load to engine HTTP submit.

**Files:**
- Create: `src/kinoforge/core/secret.py`
- Test: `tests/core/test_secret.py`

**Acceptance Criteria:**
- [ ] `Secret("hello").reveal() == "hello"`
- [ ] `str(Secret("hello")) == "<Secret>"` and `repr(Secret("hello")) == "<Secret>"`
- [ ] `f"got: {Secret('hello')}" == "got: <Secret>"` (default formatting uses `__str__`, not the underlying value)
- [ ] `Secret("a") == Secret("a")` and `Secret("a") != Secret("b")` (equality compares underlying)
- [ ] `Secret("a") == "a"` is `False` (Secret never equals a bare str — accidental cross-type comparison is a likely bug)
- [ ] `hash(Secret("a"))` is stable across calls (lets `Secret` be a dict key)
- [ ] `json.dumps(Secret("a"))` raises `TypeError` (Secret is not JSON-serializable by accident)
- [ ] `pixi run pre-commit run --files src/kinoforge/core/secret.py tests/core/test_secret.py` passes

**Verify:** `pixi run pytest tests/core/test_secret.py -v` → 7 tests pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_secret.py`** with the 7 ACs.

```python
"""Tests for core.secret.Secret — the lightweight newtype used at the
vault → orchestrator → engine boundary.

Secret is NOT in the SPEC ABCs (per architecture choice C). It's a marker
type carried only inside the narrow boundary code so unwrap sites are
self-documenting. The redaction registry + sink-canonical pattern do the
actual on-disk enforcement.
"""

import json
import pytest

from kinoforge.core.secret import Secret


def test_reveal_returns_underlying_value() -> None:
    """Secret.reveal() returns the wrapped string verbatim."""
    assert Secret("hello world").reveal() == "hello world"


def test_str_returns_placeholder_not_value() -> None:
    """str(Secret) returns '<Secret>' so accidental string interpolation
    does not leak the value. Would-fail-bug: a Secret.__str__ returning
    self._value would leak via every f-string."""
    s = Secret("super secret prompt")
    assert str(s) == "<Secret>"
    assert "super" not in str(s)


def test_repr_returns_placeholder() -> None:
    """repr(Secret) returns '<Secret>' so traceback locals don't leak."""
    assert repr(Secret("x")) == "<Secret>"


def test_fstring_interpolation_uses_str_not_value() -> None:
    """f-string default ({secret}) calls __str__, returning placeholder.
    Would-fail-bug: someone overriding __format__ to expose the value."""
    s = Secret("prompt body here")
    assert f"got: {s}" == "got: <Secret>"
    assert "prompt body here" not in f"got: {s}"


def test_equality_compares_underlying() -> None:
    """Secret('a') == Secret('a'); Secret('a') != Secret('b')."""
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")


def test_equality_never_matches_bare_str() -> None:
    """Secret('a') == 'a' is False — accidental cross-type comparison
    is a likely caller bug, not a feature. Forces explicit .reveal()."""
    assert Secret("a") != "a"
    assert not (Secret("a") == "a")


def test_hash_stable_for_dict_key() -> None:
    """hash(Secret) stable across calls so Secret can be a dict key."""
    s = Secret("a")
    assert hash(s) == hash(s)
    d: dict[Secret, int] = {s: 1, Secret("b"): 2}
    assert d[Secret("a")] == 1


def test_json_dumps_raises_typeerror() -> None:
    """json.dumps(Secret(...)) raises TypeError. Would-fail-bug: serializing
    a Secret to disk silently. Catches the most likely persistence leak."""
    with pytest.raises(TypeError):
        json.dumps(Secret("x"))
    with pytest.raises(TypeError):
        json.dumps({"prompt": Secret("x")})
```

- [ ] **Step 2: Run test, confirm FAIL** with `ModuleNotFoundError: No module named 'kinoforge.core.secret'`.

```
pixi run pytest tests/core/test_secret.py -v
```

- [ ] **Step 3: Write `src/kinoforge/core/secret.py`.**

```python
"""Lightweight Secret newtype used at the vault → orchestrator → engine boundary.

Carried inside narrow boundary code so unwrap sites are self-documenting.
Not used in any SPEC ABC; the ABCs stay str-typed per architecture choice C.
On-disk enforcement is the redaction registry + sink-canonical pattern in
core.redaction + the canonical write-site shape at every persistent sink.
"""

from __future__ import annotations

from typing import final


@final
class Secret:
    """A string whose contents must never reach logs, JSON, or any persistent
    surface except via an explicit ``reveal()`` call.

    Args:
        value: The string to wrap.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the wrapped string.

        Returns:
            The underlying string value.
        """
        return self._value

    def __repr__(self) -> str:
        return "<Secret>"

    def __str__(self) -> str:
        return "<Secret>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return False

    def __hash__(self) -> int:
        return hash(self._value)
```

- [ ] **Step 4: Run test, confirm PASS.**

```
pixi run pytest tests/core/test_secret.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Pre-commit, then commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/secret.py tests/core/test_secret.py
git add src/kinoforge/core/secret.py tests/core/test_secret.py
git commit -m "feat(core): add Secret newtype for vault→engine boundary

Lightweight wrapper carried only in boundary code; unwrap sites use .reveal().
SPEC ABCs unchanged per architecture choice C. On-disk enforcement remains
via the redaction registry + canonical sink pattern (next tasks).

7/7 ACs pass."
```

---

### Task 2: `core/redaction.py` — RedactionRegistry singleton

**Goal:** Process-wide registry of strings that must be substituted with placeholders on every persistent surface (logs, JSON files, stdout). Vault loader is the only writer; sinks are readers.

**Files:**
- Create: `src/kinoforge/core/redaction.py` (registry only; log filter in Task 3)
- Test: `tests/core/test_redaction.py` (registry tests; filter tests appended in Task 3)

**Acceptance Criteria:**
- [ ] `RedactionRegistry.instance()` returns the same object across calls (lazy singleton).
- [ ] `add(token, kind=...)` registers; subsequent `redact(s)` substitutes the token with `<{kind}:{short_id}>` where short_id is 6 deterministic hex chars derived from the token.
- [ ] `add` rejects tokens shorter than 4 chars (raises `ValueError`).
- [ ] `add` rejects whitespace-only tokens.
- [ ] `add` rejects tokens matching `<.+?:.+?>` (placeholder pattern — chicken-and-egg avoidance).
- [ ] `add` is idempotent: a second `add(token, kind)` with the same token is a no-op; existing placeholder wins.
- [ ] `redact(s)` applies tokens longest-first (so `add("foo bar")` then `add("foo")` does not partial-overlap on `"foo bar baz"`).
- [ ] `redact(s)` is case-sensitive.
- [ ] Empty registry → `redact(s) == s` (identity, public-by-design path).
- [ ] `redact_json(obj)` deep-walks `dict`, `list`, `tuple` — every `str` leaf passes through `redact`; non-str leaves untouched; new structure returned (input not mutated).
- [ ] `add_many([(t1, k1), (t2, k2), ...])` bulk-registers; each pair flows through `add` rules.
- [ ] `clear_session()` drops all tokens; `is_active` returns `False` afterward.
- [ ] `is_active` returns `True` iff any tokens are registered.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/redaction.py tests/core/test_redaction.py` passes.

**Verify:** `pixi run pytest tests/core/test_redaction.py -v` → 12 tests pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_redaction.py`** with 12 registry tests (filter tests append in Task 3).

```python
"""Tests for core.redaction.RedactionRegistry — process-wide token registry.

The registry is the sole source of truth for what gets substituted on every
persistent surface. Vault loader is the only writer; sinks read via redact()
or redact_json().
"""

import logging
import pytest

from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clear_registry() -> None:
    """Ensure the singleton starts empty for each test."""
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_singleton_returns_same_instance() -> None:
    """instance() is idempotent. Would-fail-bug: a fresh registry each call
    would mean writers and readers see different state."""
    a = RedactionRegistry.instance()
    b = RedactionRegistry.instance()
    assert a is b


def test_add_then_redact_substitutes_placeholder() -> None:
    """A registered token is substituted with <kind:short_id> in subsequent
    redact() calls."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    out = r.redact("the prompt was supersecret today")
    assert "supersecret" not in out
    assert "<prompt:positive:" in out


def test_add_rejects_short_tokens() -> None:
    """Tokens shorter than 4 chars would false-positive on unrelated text.
    Would-fail-bug: registering 'a' would corrupt every log line containing
    the letter 'a'."""
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="at least 4"):
        r.add("abc", kind="prompt:positive")


def test_add_rejects_whitespace_only_tokens() -> None:
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="whitespace"):
        r.add("    \t\n", kind="prompt:positive")


def test_add_rejects_placeholder_pattern() -> None:
    """A token matching the placeholder syntax would create a chicken-and-egg
    cycle. Would-fail-bug: registering '<prompt:positive:abc123>' would
    cause redact() to re-substitute its own output."""
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="placeholder"):
        r.add("<prompt:positive:abc>", kind="prompt:positive")


def test_add_is_idempotent() -> None:
    """A second add() with the same token is a no-op; existing placeholder
    wins. Would-fail-bug: per-call placeholder regeneration would make
    redact() output non-deterministic across calls."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    first = r.redact("got supersecret")
    r.add("supersecret", kind="prompt:positive")  # idempotent
    second = r.redact("got supersecret")
    assert first == second


def test_redact_applies_tokens_longest_first() -> None:
    """When 'foo bar' and 'foo' are both registered, 'foo bar baz' should
    redact the longer match first to avoid partial overlap. Would-fail-bug:
    shortest-first would replace 'foo' inside 'foo bar' and leave 'bar' loose."""
    r = RedactionRegistry.instance()
    r.add("foo", kind="prompt:positive")
    r.add("foo bar", kind="prompt:negative")
    out = r.redact("got foo bar baz")
    assert "<prompt:negative:" in out
    assert "foo" not in out
    assert " bar" not in out


def test_redact_case_sensitive() -> None:
    """Prompts are case-sensitive content. 'FOO' is not 'foo'."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    assert "SUPERSECRET" in r.redact("got SUPERSECRET")
    assert "supersecret" not in r.redact("got supersecret")


def test_redact_empty_registry_is_identity() -> None:
    """Public-by-design path: no vault loaded → registry empty → redact is
    a passthrough. Would-fail-bug: a defensive 'redact everything that
    looks suspicious' default would break non-vault runs."""
    r = RedactionRegistry.instance()
    assert r.redact("anything goes through") == "anything goes through"


def test_redact_json_deep_walks_nested_structure() -> None:
    """redact_json walks dict/list/tuple; substitutes every str leaf;
    returns new structure; doesn't mutate input."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    payload = {
        "outer": "no secret here",
        "nested": {"prompt": "the supersecret text", "extra": [1, "with supersecret inside", 2]},
        "tup": ("supersecret in a tuple", 99),
    }
    out = r.redact_json(payload)
    assert payload["nested"]["prompt"] == "the supersecret text"  # input untouched
    assert "supersecret" not in str(out)
    assert "<prompt:positive:" in out["nested"]["prompt"]
    assert isinstance(out["nested"]["extra"], list)


def test_add_many_bulk_registers() -> None:
    """add_many flows each pair through add()."""
    r = RedactionRegistry.instance()
    r.add_many([("alpha-secret", "prompt:positive"), ("beta-secret", "lora:ref")])
    out = r.redact("got alpha-secret and beta-secret")
    assert "alpha-secret" not in out
    assert "beta-secret" not in out


def test_clear_session_resets_state() -> None:
    """clear_session drops every registered token."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    assert r.is_active
    r.clear_session()
    assert not r.is_active
    assert r.redact("got supersecret") == "got supersecret"


def test_is_active_reflects_registration() -> None:
    r = RedactionRegistry.instance()
    assert not r.is_active
    r.add("supersecret", kind="prompt:positive")
    assert r.is_active
```

- [ ] **Step 2: Run test, confirm FAIL** (module not found).

- [ ] **Step 3: Write `src/kinoforge/core/redaction.py`** (registry only).

```python
"""Process-wide redaction registry + logging filter.

The registry is the single source of truth for tokens that must be
substituted on every persistent surface (logs, JSON files, stdout, error
blocks). The vault loader is the only writer; sinks (Ledger, profile cache,
batch summary, OutputSink.publish, _save_fixture, etc.) are readers.

Empty registry == public-by-design passthrough (the standard test prompt
path).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"<.+?:.+?>")
_MIN_TOKEN_LEN = 4


def _short_id(token: str) -> str:
    """Deterministic 6-char hex suffix used in placeholders to distinguish
    multiple tokens of the same kind."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:6]


class RedactionRegistry:
    """Singleton holding the active vault's sensitive tokens.

    Use ``RedactionRegistry.instance()`` to access; never instantiate directly.
    """

    _singleton: "RedactionRegistry | None" = None

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}  # token -> placeholder

    @classmethod
    def instance(cls) -> "RedactionRegistry":
        """Lazy singleton accessor.

        Returns:
            The process-wide registry.
        """
        if cls._singleton is None:
            cls._singleton = RedactionRegistry()
        return cls._singleton

    def add(self, token: str, *, kind: str, replacement: str | None = None) -> None:
        """Register ``token`` for substitution.

        Args:
            token: The exact string to substitute. Must be at least 4 chars
                and not whitespace-only and not match the placeholder pattern.
            kind: A label describing the token category, e.g.
                ``'prompt:positive'``, ``'lora:ref'``, ``'output'``.
            replacement: Override the default placeholder. If ``None``,
                ``f'<{kind}:{short_id}>'`` is used.

        Raises:
            ValueError: On a token failing the length / whitespace / placeholder
                rules.
        """
        if len(token) < _MIN_TOKEN_LEN:
            raise ValueError(f"redaction token must be at least {_MIN_TOKEN_LEN} chars: {token!r}")
        if not token.strip():
            raise ValueError(f"redaction token cannot be whitespace-only: {token!r}")
        if _PLACEHOLDER_RE.fullmatch(token) or _PLACEHOLDER_RE.search(token):
            raise ValueError(f"redaction token matches placeholder pattern: {token!r}")
        if token in self._tokens:
            return  # idempotent
        self._tokens[token] = replacement if replacement is not None else f"<{kind}:{_short_id(token)}>"

    def add_many(self, tokens: list[tuple[str, str]]) -> None:
        """Bulk-register ``(token, kind)`` pairs."""
        for token, kind in tokens:
            self.add(token, kind=kind)

    def redact(self, s: str) -> str:
        """Substitute every registered token in ``s`` with its placeholder.

        Tokens are applied longest-first to avoid partial overlap. Case-sensitive.

        Args:
            s: The string to scan.

        Returns:
            A new string with substitutions applied.
        """
        if not self._tokens:
            return s
        result = s
        for token in sorted(self._tokens, key=len, reverse=True):
            if token in result:
                result = result.replace(token, self._tokens[token])
        return result

    def redact_json(self, obj: Any) -> Any:
        """Deep-walk ``obj`` (dict/list/tuple) and redact every str leaf.

        Returns a new structure; never mutates the input.
        """
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self.redact_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_json(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(self.redact_json(v) for v in obj)
        return obj

    def clear_session(self) -> None:
        """Drop every registered token. Called by the CLI at orchestrator exit
        so a long-running test session can load multiple vaults sequentially."""
        self._tokens.clear()

    @property
    def is_active(self) -> bool:
        """``True`` iff at least one token is registered."""
        return bool(self._tokens)
```

- [ ] **Step 4: Run test, confirm PASS.**

Expected: 13 passed (the 12 ACs + the autouse fixture isolation check).

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/redaction.py tests/core/test_redaction.py
git add src/kinoforge/core/redaction.py tests/core/test_redaction.py
git commit -m "feat(core): RedactionRegistry singleton + token rules

Process-wide registry of strings to substitute on every persistent surface.
Vault loader is the only writer; sinks read via redact / redact_json.
Empty registry is a passthrough (public-by-design path unchanged).

13/13 tests pass."
```

---

### Task 3: `core/redaction.py` — RedactingLogFilter

**Goal:** Install `logging.Filter` that calls `RedactionRegistry.redact()` on every log record before formatting. `bypass=True` (for `--debug-show-secrets`) passes through unchanged.

**Files:**
- Modify: `src/kinoforge/core/redaction.py` — append `RedactingLogFilter` class.
- Modify: `tests/core/test_redaction.py` — append 5 filter tests.

**Acceptance Criteria:**
- [ ] Installing `RedactingLogFilter(registry)` on a logger causes subsequent `logger.info("prompt: %s", "supersecret")` to emit redacted text.
- [ ] Filter substitutes tokens in `record.msg` AND in every `str` arg in `record.args`.
- [ ] Non-string args (`int`, `float`, `dict`) are left untouched in `record.args`.
- [ ] `bypass=True` filter is a passthrough — `record.msg` and `record.args` reach the handler unchanged.
- [ ] Empty registry + non-bypass filter: passthrough (no-op).
- [ ] Filter on root `kinoforge` logger reaches every `kinoforge.<submodule>` child without per-submodule installation.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/redaction.py tests/core/test_redaction.py` passes.

**Verify:** `pixi run pytest tests/core/test_redaction.py -v` → 18 tests pass (13 from Task 2 + 5 new).

**Steps:**

- [ ] **Step 1: Append failing tests to `tests/core/test_redaction.py`.**

```python
def test_log_filter_redacts_record_msg() -> None:
    """RedactingLogFilter substitutes registered tokens in record.msg."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    from kinoforge.core.redaction import RedactingLogFilter
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "got supersecret here", None, None)
    flt.filter(rec)
    assert "supersecret" not in rec.msg
    assert "<prompt:positive:" in rec.msg


def test_log_filter_redacts_string_args() -> None:
    """Filter substitutes tokens in str args; non-str args untouched."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    from kinoforge.core.redaction import RedactingLogFilter
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "got %s and %d", ("supersecret", 42), None)
    flt.filter(rec)
    assert "supersecret" not in rec.args[0]
    assert rec.args[1] == 42


def test_log_filter_bypass_passes_through() -> None:
    """bypass=True makes the filter a no-op; record reaches handler unchanged."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    from kinoforge.core.redaction import RedactingLogFilter
    flt = RedactingLogFilter(r, bypass=True)
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "got supersecret", None, None)
    flt.filter(rec)
    assert rec.msg == "got supersecret"


def test_log_filter_empty_registry_passes_through() -> None:
    """No tokens registered + non-bypass: filter is no-op."""
    r = RedactionRegistry.instance()
    from kinoforge.core.redaction import RedactingLogFilter
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "anything", None, None)
    flt.filter(rec)
    assert rec.msg == "anything"


def test_log_filter_on_root_reaches_children() -> None:
    """Installing filter on 'kinoforge' root catches 'kinoforge.engines.foo' children."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    from kinoforge.core.redaction import RedactingLogFilter
    root = logging.getLogger("kinoforge")
    flt = RedactingLogFilter(r)
    root.addFilter(flt)
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        child = logging.getLogger("kinoforge.engines.fake")
        child.info("submitting supersecret to backend")
        assert any("supersecret" not in m for m in captured)
        assert all("supersecret" not in m for m in captured)
    finally:
        root.removeHandler(handler)
        root.removeFilter(flt)
```

- [ ] **Step 2: Run test, confirm FAIL** with `ImportError: cannot import name 'RedactingLogFilter'`.

- [ ] **Step 3: Append `RedactingLogFilter` to `src/kinoforge/core/redaction.py`.**

```python
import logging


class RedactingLogFilter(logging.Filter):
    """A logging.Filter that calls RedactionRegistry.redact() on every record.

    Installed on the root ``kinoforge`` logger at CLI entry. Child loggers
    (``kinoforge.engines.fake``, etc.) inherit it automatically.

    Args:
        registry: The active registry. Usually ``RedactionRegistry.instance()``.
        bypass: When ``True``, the filter is a passthrough. Only set by
            ``--debug-show-secrets`` (forbidden under ``--ephemeral``).
    """

    def __init__(self, registry: RedactionRegistry, *, bypass: bool = False) -> None:
        super().__init__()
        self._registry = registry
        self._bypass = bypass

    def filter(self, record: logging.LogRecord) -> bool:
        if self._bypass:
            return True
        if isinstance(record.msg, str):
            record.msg = self._registry.redact(record.msg)
        if record.args:
            record.args = tuple(
                self._registry.redact(a) if isinstance(a, str) else a for a in record.args
            )
        return True
```

- [ ] **Step 4: Run test, confirm PASS.**

Expected: 18 passed.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/redaction.py tests/core/test_redaction.py
git add src/kinoforge/core/redaction.py tests/core/test_redaction.py
git commit -m "feat(core): RedactingLogFilter for root kinoforge logger

logging.Filter that calls registry.redact() on record.msg + string args.
Installed once on root logger; child loggers inherit. bypass=True for
--debug-show-secrets (forbidden under --ephemeral).

5/5 new tests pass; 18/18 total in test_redaction.py."
```

---

### Task 4: `core/vault.py` — Vault loader, alias derivation, path validation

**Goal:** Pydantic models + `load_vault(path) -> Vault` + `compute_profile_alias(config, vault) -> str` + repo-root path validation. Loaded once at CLI entry; tokens registered with `RedactionRegistry` after load.

**Files:**
- Modify: `src/kinoforge/core/errors.py` — add `VaultError`, `VaultPathError`, `VaultUnderRepoError`, `VaultParseError`, `VaultEmptyError`.
- Create: `src/kinoforge/core/vault.py`
- Test: `tests/core/test_vault.py`

**Acceptance Criteria:**
- [ ] `Vault` pydantic model: `positive_prompt: str | None`, `segments: list[VaultSegment] | None`, `negative_prompt: str | None`, `loras: list[VaultLoRA]`, `alias: str | None`.
- [ ] `extra="forbid"` on every model — unknown keys fail load.
- [ ] Exactly-one-of validator: both prompts or neither → `ValueError`.
- [ ] `alias` regex `^[a-z0-9][a-z0-9-]{0,63}$` — uppercase rejected; empty rejected.
- [ ] `load_vault(path)` raises `VaultPathError` on missing file.
- [ ] `load_vault(path)` raises `VaultUnderRepoError` when path resolves under `git rev-parse --show-toplevel`.
- [ ] `load_vault(path)` skips the repo-root check when not inside a git repo.
- [ ] `load_vault(path)` emits `WARNING` if file mode has any bits set for group/other (and continues — no block).
- [ ] `load_vault(path)` raises `VaultParseError` on malformed YAML or pydantic violation.
- [ ] `load_vault(path)` raises `VaultEmptyError` when neither `positive_prompt` nor `segments` is populated.
- [ ] `compute_profile_alias(config, vault=None)` returns `CapabilityKey.from_config(config).derive()` (existing behavior).
- [ ] `compute_profile_alias(config, vault)` with `vault.alias` set returns the override.
- [ ] `compute_profile_alias(config, vault)` with no override returns `"cfg-" + sha256(material)[:12]` where material is canonical-JSON over `{base, loras, engine, precision}` — sorted_keys=True so dict order doesn't change output.
- [ ] Two vaults with same content produce identical aliases.
- [ ] Vaults differing only in LoRA-stack order produce different aliases (order matters).
- [ ] `register_vault_tokens(vault)` calls `RedactionRegistry.add_many` with positive, negative (if set), every `lora.ref`, every `lora.label` (if set).
- [ ] `pixi run pre-commit run --files src/kinoforge/core/vault.py src/kinoforge/core/errors.py tests/core/test_vault.py` passes.

**Verify:** `pixi run pytest tests/core/test_vault.py -v` → 14 tests pass.

**Steps:**

- [ ] **Step 1: Add error classes to `src/kinoforge/core/errors.py`.**

```python
# append to errors.py
class VaultError(KinoforgeError):
    """Base for vault load / validation failures."""


class VaultPathError(VaultError):
    """Vault path missing, unresolvable, or unreadable."""


class VaultUnderRepoError(VaultError):
    """Vault path resolves under the active git repo root."""


class VaultParseError(VaultError):
    """Vault YAML malformed or pydantic violation."""

    def __init__(self, path: str, original: Exception) -> None:
        super().__init__(f"vault parse failed at {path}: {original}")
        self.path = path
        self.original = original


class VaultEmptyError(VaultError):
    """Neither positive_prompt nor segments populated."""
```

- [ ] **Step 2: Write `tests/core/test_vault.py`** with 14 tests covering every AC (see test signatures above).

```python
"""Tests for core.vault — vault file load, validation, alias derivation."""

import os
from pathlib import Path

import pytest
import yaml

from kinoforge.core.errors import (
    VaultEmptyError,
    VaultParseError,
    VaultPathError,
    VaultUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.core.vault import (
    Vault,
    VaultLoRA,
    VaultSegment,
    compute_profile_alias,
    load_vault,
    register_vault_tokens,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def _write_vault(tmp_path: Path, content: dict) -> Path:
    p = tmp_path / "vault.yaml"
    p.write_text(yaml.safe_dump(content))
    p.chmod(0o600)
    return p


def test_vault_positive_prompt_only_loads(tmp_path: Path) -> None:
    p = _write_vault(tmp_path, {"positive_prompt": "Cinematic shot of a sunrise"})
    v = load_vault(p)
    assert v.positive_prompt == "Cinematic shot of a sunrise"
    assert v.segments is None


def test_vault_segments_only_loads(tmp_path: Path) -> None:
    p = _write_vault(tmp_path, {"segments": [{"prompt": "wide shot"}, {"prompt": "close-up"}]})
    v = load_vault(p)
    assert v.positive_prompt is None
    assert v.segments is not None and len(v.segments) == 2
    assert v.segments[0].prompt == "wide shot"


def test_vault_both_positive_and_segments_rejected(tmp_path: Path) -> None:
    """Exactly-one-of validator catches the both-populated case."""
    p = _write_vault(tmp_path, {
        "positive_prompt": "wide shot",
        "segments": [{"prompt": "close-up"}],
    })
    with pytest.raises(VaultParseError, match="exactly one"):
        load_vault(p)


def test_vault_neither_positive_nor_segments_rejected(tmp_path: Path) -> None:
    """Empty vault is an error — caller asked for confidentiality of WHAT?"""
    p = _write_vault(tmp_path, {"negative_prompt": "blurry"})
    with pytest.raises(VaultEmptyError):
        load_vault(p)


def test_vault_extra_keys_rejected(tmp_path: Path) -> None:
    """extra='forbid' — unknown top-level keys fail load."""
    p = _write_vault(tmp_path, {"positive_prompt": "ok", "unknown_key": "x"})
    with pytest.raises(VaultParseError):
        load_vault(p)


def test_vault_alias_regex_lowercase_only(tmp_path: Path) -> None:
    """Alias must match ^[a-z0-9][a-z0-9-]{0,63}$ — uppercase rejected."""
    p = _write_vault(tmp_path, {"positive_prompt": "ok", "alias": "BadAlias"})
    with pytest.raises(VaultParseError):
        load_vault(p)


def test_vault_path_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(VaultPathError):
        load_vault(tmp_path / "nonexistent.yaml")


def test_vault_under_repo_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A vault resolved under the active git repo root is a hard error
    (otherwise the user might accidentally commit it). Would-fail-bug: not
    consulting `git rev-parse` would let a vault under the repo silently
    succeed."""
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root",
        lambda: tmp_path,
    )
    with pytest.raises(VaultUnderRepoError, match=str(tmp_path)):
        load_vault(p)


def test_vault_outside_repo_passes_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    monkeypatch.setattr(
        "kinoforge.core.vault._git_repo_root",
        lambda: tmp_path.parent.parent / "somewhere-else",  # vault NOT under this
    )
    v = load_vault(p)
    assert v.positive_prompt == "ok"


def test_vault_world_readable_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    p = _write_vault(tmp_path, {"positive_prompt": "ok"})
    p.chmod(0o644)
    with caplog.at_level("WARNING", logger="kinoforge.core.vault"):
        load_vault(p)
    assert any("chmod 600" in r.message for r in caplog.records)


def test_compute_alias_no_vault_uses_capability_hash() -> None:
    """Backward compat: no vault → existing CapabilityKey.derive() hash."""
    from kinoforge.core.config import Config  # adjust import as needed
    cfg = _minimal_config()  # helper fixture; see below
    alias_a = compute_profile_alias(cfg, vault=None)
    alias_b = compute_profile_alias(cfg, vault=None)
    assert alias_a == alias_b
    assert not alias_a.startswith("cfg-")


def test_compute_alias_explicit_override_wins() -> None:
    cfg = _minimal_config()
    v = Vault(positive_prompt="ok", alias="my-vault-id")
    assert compute_profile_alias(cfg, v) == "my-vault-id"


def test_compute_alias_auto_derive_stable_and_order_sensitive() -> None:
    """Auto-derived alias is sha256-based over canonical-JSON over
    (base, loras, engine, precision). LoRA order matters."""
    cfg = _minimal_config()
    v1 = Vault(positive_prompt="ok", loras=[VaultLoRA(ref="a"), VaultLoRA(ref="b")])
    v2 = Vault(positive_prompt="ok", loras=[VaultLoRA(ref="a"), VaultLoRA(ref="b")])
    v3 = Vault(positive_prompt="ok", loras=[VaultLoRA(ref="b"), VaultLoRA(ref="a")])
    assert compute_profile_alias(cfg, v1) == compute_profile_alias(cfg, v2)
    assert compute_profile_alias(cfg, v1) != compute_profile_alias(cfg, v3)
    assert compute_profile_alias(cfg, v1).startswith("cfg-")
    assert len(compute_profile_alias(cfg, v1)) == 4 + 12  # "cfg-" + 12 hex


def test_register_vault_tokens_registers_all_sensitive_strings() -> None:
    v = Vault(
        positive_prompt="positive body",
        negative_prompt="negative body",
        loras=[VaultLoRA(ref="civitai:1234@5678", label="my-style")],
    )
    register_vault_tokens(v)
    r = RedactionRegistry.instance()
    out = r.redact("got positive body and negative body and civitai:1234@5678 and my-style")
    assert "positive body" not in out
    assert "negative body" not in out
    assert "civitai:1234@5678" not in out
    assert "my-style" not in out


def _minimal_config():
    """Helper — minimal Config for alias tests. Adjust to actual Config shape."""
    # In real code, build via kinoforge.core.config.Config(...) with required fields.
    # Tests should use the existing fixture from tests/conftest.py if available.
    from kinoforge.core.config import Config  # adjust to real import
    # ... build minimal config; this helper depends on existing Config shape.
    ...
```

- [ ] **Step 3: Run test, confirm FAIL** with `ModuleNotFoundError: kinoforge.core.vault`.

- [ ] **Step 4: Write `src/kinoforge/core/vault.py`.**

```python
"""Vault file loader + alias derivation + repo-root path validation.

The vault is the user's sole on-disk place where positive/negative prompts
and LoRA refs/labels appear. Loaded once at CLI entry; contents live in
process memory only. After load, tokens are registered with the
RedactionRegistry so every downstream surface that interpolates them gets
redacted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from kinoforge.core.errors import (
    VaultEmptyError,
    VaultParseError,
    VaultPathError,
    VaultUnderRepoError,
)
from kinoforge.core.redaction import RedactionRegistry

logger = logging.getLogger(__name__)


class VaultSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class VaultLoRA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=1)
    label: str | None = None


class Vault(BaseModel):
    """The user's private prompts + LoRA refs.

    Lives outside the repo. Tokens registered with RedactionRegistry on load.
    """

    model_config = ConfigDict(extra="forbid")

    positive_prompt: str | None = None
    segments: list[VaultSegment] | None = None
    negative_prompt: str | None = None
    loras: list[VaultLoRA] = Field(default_factory=list)
    alias: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")

    @model_validator(mode="after")
    def _exactly_one_of_prompt_or_segments(self) -> "Vault":
        has_prompt = self.positive_prompt is not None and self.positive_prompt.strip() != ""
        has_segments = self.segments is not None and len(self.segments) > 0
        if has_prompt and has_segments:
            raise ValueError("vault: specify exactly one of positive_prompt or segments, not both")
        return self

    def __repr__(self) -> str:
        return f"<Vault alias={self.alias or '<unset>'}>"


def _git_repo_root() -> Path | None:
    """Return the active git repo root, or None if not inside a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip())


def load_vault(path: Path | str) -> Vault:
    """Load a vault YAML file.

    Args:
        path: Path to the vault file.

    Returns:
        The validated Vault model.

    Raises:
        VaultPathError: Path missing or unreadable.
        VaultUnderRepoError: Path resolves under the active git repo root.
        VaultParseError: YAML malformed or pydantic violation.
        VaultEmptyError: Neither positive_prompt nor segments populated.
    """
    p = Path(path).resolve()
    if not p.exists() or not p.is_file():
        raise VaultPathError(f"vault file not found: {p}")
    if not os.access(p, os.R_OK):
        raise VaultPathError(f"vault file not readable: {p}")

    repo_root = _git_repo_root()
    if repo_root is not None:
        try:
            p.relative_to(repo_root)
            raise VaultUnderRepoError(
                f"vault path is under the active repo root ({repo_root}): {p}; "
                f"move it outside the repo to avoid accidental commits"
            )
        except ValueError:
            pass  # not under repo — fine

    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "vault file %s is readable by group/other (mode %o); recommend chmod 600",
            p, mode,
        )

    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise VaultParseError(str(p), e) from e
    if not isinstance(raw, dict):
        raise VaultParseError(str(p), TypeError(f"vault YAML root must be a mapping, got {type(raw).__name__}"))

    try:
        v = Vault.model_validate(raw)
    except ValidationError as e:
        raise VaultParseError(str(p), e) from e

    has_prompt = v.positive_prompt is not None and v.positive_prompt.strip() != ""
    has_segments = v.segments is not None and len(v.segments) > 0
    if not (has_prompt or has_segments):
        raise VaultEmptyError(f"vault has neither positive_prompt nor segments: {p}")

    return v


def compute_profile_alias(config: Any, vault: Vault | None) -> str:
    """Compute the on-disk profile cache key.

    Args:
        config: The loaded Config (carries base model, engine kind, precision).
        vault: The loaded vault, or None for public-by-design runs.

    Returns:
        ``cfg-<sha256[:12]>`` when vault present and no explicit alias;
        ``vault.alias`` when set explicitly;
        ``CapabilityKey.from_config(config).derive()`` when no vault.
    """
    if vault is None:
        # Backward-compat: existing CapabilityKey.derive() hash.
        from kinoforge.core.interfaces import CapabilityKey
        return CapabilityKey.from_config(config).derive()

    if vault.alias:
        return vault.alias

    base_ref = next((m.ref for m in config.models if m.kind == "base"), "")
    material = json.dumps(
        {
            "base":      base_ref,
            "loras":     [l.ref for l in vault.loras],
            "engine":    config.engine.kind,
            "precision": config.engine.precision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "cfg-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def register_vault_tokens(vault: Vault) -> None:
    """Register every sensitive string from the vault with the registry.

    Idempotent — calling twice is safe.
    """
    r = RedactionRegistry.instance()
    pairs: list[tuple[str, str]] = []
    if vault.positive_prompt:
        pairs.append((vault.positive_prompt, "prompt:positive"))
    if vault.negative_prompt:
        pairs.append((vault.negative_prompt, "prompt:negative"))
    if vault.segments:
        for seg in vault.segments:
            pairs.append((seg.prompt, "prompt:positive"))
    for lora in vault.loras:
        pairs.append((lora.ref, "lora:ref"))
        if lora.label:
            pairs.append((lora.label, "lora:label"))
    r.add_many(pairs)
```

- [ ] **Step 5: Run test, confirm PASS.**

Expected: 14 passed.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/vault.py src/kinoforge/core/errors.py tests/core/test_vault.py
git add src/kinoforge/core/vault.py src/kinoforge/core/errors.py tests/core/test_vault.py
git commit -m "feat(core): Vault loader + alias derivation + repo-root check

Vault YAML with exactly-one-of positive_prompt / segments validator.
Repo-root path check via git rev-parse — hard fail when vault under repo.
Permissions warn (chmod 600). Auto-derived alias 'cfg-' + sha256[:12]
over canonical-JSON (base, loras, engine, precision). register_vault_tokens
bulk-registers every sensitive string with the RedactionRegistry.

14/14 tests pass. Closes Sub-α task 4."
```

---

### Task 5: `core/artifacts.py` — opaque_store_name helper

**Goal:** Centralized sha256-derived filename helper for every `store.put_bytes` call site. Used by `GenerateClipStage` (Task 13) and asserted by the CI invariant test AC2 (Task 19).

**Files:**
- Create: `src/kinoforge/core/artifacts.py`
- Test: `tests/core/test_artifacts.py`

**Acceptance Criteria:**
- [ ] `opaque_store_name(b"hello", ".mp4") == "<16-hex>.mp4"` where the hex is `sha256(b"hello").hexdigest()[:16]`.
- [ ] Extension is preserved verbatim when it matches `\.[A-Za-z0-9]{1,5}`.
- [ ] Invalid extensions (e.g. `"junk"`, `".foo bar"`, `".verylongextension"`, empty string) become empty suffix.
- [ ] Same bytes + same extension → same name (deterministic).
- [ ] Different bytes → different names.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/artifacts.py tests/core/test_artifacts.py` passes.

**Verify:** `pixi run pytest tests/core/test_artifacts.py -v` → 4 tests pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_artifacts.py`** with 4 tests:

```python
"""Tests for core.artifacts.opaque_store_name."""

import hashlib
import pytest

from kinoforge.core.artifacts import opaque_store_name


def test_basic_sha_and_extension() -> None:
    """Name is sha256(bytes)[:16] + ext."""
    name = opaque_store_name(b"hello", ".mp4")
    expected_prefix = hashlib.sha256(b"hello").hexdigest()[:16]
    assert name == f"{expected_prefix}.mp4"


def test_invalid_extension_dropped() -> None:
    """Extensions not matching \\.[A-Za-z0-9]{1,5} become empty suffix.
    Would-fail-bug: passing a prompt-derived 'extension' through verbatim
    would leak material into the store-side filename."""
    assert "." not in opaque_store_name(b"x", "junk-not-an-ext")
    assert "." not in opaque_store_name(b"x", ".foo bar")  # space in ext
    assert "." not in opaque_store_name(b"x", ".verylongextension")  # > 5 chars


def test_deterministic_same_bytes_same_ext() -> None:
    """Same input → same output across calls."""
    assert opaque_store_name(b"abc", ".mp4") == opaque_store_name(b"abc", ".mp4")


def test_different_bytes_different_names() -> None:
    """Hash collisions astronomically unlikely; trivial bytes differ."""
    assert opaque_store_name(b"a", ".mp4") != opaque_store_name(b"b", ".mp4")
```

- [ ] **Step 2: Run test, confirm FAIL.**

- [ ] **Step 3: Write `src/kinoforge/core/artifacts.py`.**

```python
"""Helper for opaque store-side filenames.

Every ``ArtifactStore.put_bytes(run_id, name, payload)`` call site uses
``opaque_store_name(payload, ext)`` so the on-disk filename is derived purely
from content hash — never from prompt-derived material. AC2 of the CI
invariant test enforces this at merge time.
"""

from __future__ import annotations

import hashlib
import re

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}")


def opaque_store_name(payload: bytes, original_ext: str) -> str:
    """Return a store-side filename derived purely from ``payload``'s sha256.

    Args:
        payload: The bytes being persisted.
        original_ext: An extension like ``".mp4"``. Verified against
            ``\\.[A-Za-z0-9]{1,5}``; dropped otherwise.

    Returns:
        ``<16-hex>[.ext]`` — no prompt-derived material ever appears in the
        returned name.
    """
    digest = hashlib.sha256(payload).hexdigest()[:16]
    safe_ext = original_ext if _EXT_RE.fullmatch(original_ext or "") else ""
    return f"{digest}{safe_ext}"
```

- [ ] **Step 4: Run test, confirm PASS** (4 passed).

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/artifacts.py tests/core/test_artifacts.py
git add src/kinoforge/core/artifacts.py tests/core/test_artifacts.py
git commit -m "feat(core): opaque_store_name helper for put_bytes call sites

sha256-derived 16-char prefix + verified extension. Caller sites in
GenerateClipStage will adopt this in Task 13; the CI invariant (Task 19
AC2) asserts every put_bytes site uses it.

4/4 tests pass. Closes Sub-α task 5."
```

---

### Task 6: `ArtifactStore.delete_run` + `manual_cleanup_command` ABC + LocalArtifactStore impls

**Goal:** Add the two abstract methods on `ArtifactStore` and implement on `LocalArtifactStore`. Foundation for the ephemeral session cleanup (Task 15) and the cleanup-failure UX (Task 16/17 errors).

**Files:**
- Modify: `src/kinoforge/stores/base.py` — add abstract methods.
- Modify: `src/kinoforge/stores/local.py` — implement.
- Test: `tests/stores/test_delete_run.py` — create with 3 tests (LocalArtifactStore section; S3/GCS sections appended in Task 7).

**Acceptance Criteria:**
- [ ] `ArtifactStore.delete_run(run_id)` is `@abstractmethod`.
- [ ] `ArtifactStore.manual_cleanup_command(run_id)` is `@abstractmethod`.
- [ ] `LocalArtifactStore.delete_run(run_id)` removes `<root>/<run_id>/` recursively; `FileNotFoundError` swallowed (idempotent).
- [ ] `LocalArtifactStore.manual_cleanup_command(run_id)` returns `rm -rf "<absolute path>"` with the path double-quoted.
- [ ] Existing `LocalArtifactStore` tests still pass.
- [ ] `pixi run pre-commit run --files src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_delete_run.py` passes.

**Verify:** `pixi run pytest tests/stores/ -v` → existing tests + 3 new pass.

**Steps:**

- [ ] **Step 1: Append abstract methods to `src/kinoforge/stores/base.py`.**

```python
# in ArtifactStore ABC
@abstractmethod
def delete_run(self, run_id: str) -> None:
    """Remove all artifacts under ``run_id``.

    Idempotent. Atomic at the per-name level; if the implementation can't
    atomically remove the whole prefix it MUST iterate ``list()`` and delete
    each, raising on the first per-name failure that isn't FileNotFoundError.
    """

@abstractmethod
def manual_cleanup_command(self, run_id: str) -> str:
    """Return a single-line shell command that deletes everything under
    this store's ``run_id`` prefix.

    Used in error messages when ``delete_run`` fails so the user can finish
    the cleanup by hand. Must produce an absolute, copy-pasteable command.
    """
```

- [ ] **Step 2: Write failing tests `tests/stores/test_delete_run.py`** (Local section only):

```python
"""Tests for ArtifactStore.delete_run + manual_cleanup_command.

S3 and GCS sections appended in Task 7.
"""

from pathlib import Path

import pytest

from kinoforge.stores.local import LocalArtifactStore


def test_local_delete_run_removes_directory(tmp_path: Path) -> None:
    store = LocalArtifactStore(root=tmp_path)
    store.put_json("run-1", "ledger.json", {"k": "v"})
    store.put_bytes("run-1", "abc.mp4", b"video bytes")
    assert (tmp_path / "run-1").exists()
    store.delete_run("run-1")
    assert not (tmp_path / "run-1").exists()


def test_local_delete_run_idempotent_on_missing(tmp_path: Path) -> None:
    """Calling delete_run on an absent run_id is a no-op, not an error.
    Would-fail-bug: EphemeralSession.__exit__ would crash on cleanup of a
    run that never wrote anything."""
    store = LocalArtifactStore(root=tmp_path)
    store.delete_run("never-existed")  # no raise


def test_local_manual_cleanup_command_shape(tmp_path: Path) -> None:
    """rm -rf <absolute path>, double-quoted."""
    store = LocalArtifactStore(root=tmp_path)
    cmd = store.manual_cleanup_command("abc-123")
    assert cmd.startswith("rm -rf ")
    assert f'"{tmp_path / "abc-123"}"' in cmd
```

- [ ] **Step 3: Run, confirm FAIL** with `AttributeError: 'LocalArtifactStore' object has no attribute 'delete_run'`.

- [ ] **Step 4: Implement on `LocalArtifactStore`.**

```python
# stores/local.py
import shutil

class LocalArtifactStore(ArtifactStore):
    # ... existing methods ...

    def delete_run(self, run_id: str) -> None:
        """Recursively remove the run's subdirectory; FileNotFoundError swallowed."""
        target = self._root / run_id
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            return

    def manual_cleanup_command(self, run_id: str) -> str:
        target = (self._root / run_id).resolve()
        return f'rm -rf "{target}"'
```

- [ ] **Step 5: Run, confirm PASS.** All existing `tests/stores/test_local.py` + 3 new pass.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_delete_run.py
git add src/kinoforge/stores/base.py src/kinoforge/stores/local.py tests/stores/test_delete_run.py
git commit -m "feat(stores): ArtifactStore.delete_run + manual_cleanup_command ABCs

LocalArtifactStore.delete_run uses shutil.rmtree (FileNotFoundError
swallowed for idempotence). manual_cleanup_command returns
rm -rf \"<absolute>\" for the EphemeralStoreCleanupFailedError UX.
S3 + GCS impls in Task 7.

3/3 new tests pass; existing 22 LocalArtifactStore tests pass."
```

---

### Task 7: S3 + GCS delete_run + manual_cleanup_command

**Goal:** Implement the two abstract methods on `S3ArtifactStore` and `GCSArtifactStore`.

**Files:**
- Modify: `src/kinoforge/stores/s3.py` — impls.
- Modify: `src/kinoforge/stores/gcs.py` — impls.
- Modify: `tests/stores/test_delete_run.py` — append 6 tests (3 per cloud store).
- Modify: `tests/stores/conftest.py` — `FakeS3Client` + `FakeGCSClient` gain `delete_objects` / `delete_blobs` simulation.

**Acceptance Criteria:**
- [ ] `S3ArtifactStore.delete_run(run_id)` paginates `list_objects_v2(Prefix=<prefix><run_id>/)` and calls `delete_objects` in batches of 1000.
- [ ] `S3ArtifactStore.delete_run` is idempotent (empty prefix → no calls).
- [ ] `S3ArtifactStore.manual_cleanup_command(run_id)` returns `aws s3 rm s3://<bucket>/<prefix><run_id>/ --recursive`.
- [ ] `GCSArtifactStore.delete_run(run_id)` calls `bucket.list_blobs(prefix=<prefix><run_id>/)` + `bucket.delete_blobs(list(blobs))`.
- [ ] `GCSArtifactStore.delete_run` is idempotent.
- [ ] `GCSArtifactStore.manual_cleanup_command(run_id)` returns `gcloud storage rm -r gs://<bucket>/<prefix><run_id>/`.
- [ ] `pixi run pre-commit run --files src/kinoforge/stores/s3.py src/kinoforge/stores/gcs.py tests/stores/test_delete_run.py tests/stores/conftest.py` passes.

**Verify:** `pixi run pytest tests/stores/test_delete_run.py -v` → 9 tests pass total.

**Steps:**

- [ ] **Step 1: Extend `tests/stores/conftest.py` `FakeS3Client`** with delete batching:

```python
# conftest.py — extend existing FakeS3Client
class FakeS3Client:
    # existing put_object, get_object, head_object, list_objects_v2 ...

    def delete_objects(self, *, Bucket: str, Delete: dict) -> dict:
        """Mirror boto3 shape: Delete={'Objects': [{'Key': str}, ...]}."""
        keys_to_drop = {o["Key"] for o in Delete["Objects"]}
        self._objects = {k: v for k, v in self._objects.items() if k not in keys_to_drop}
        return {"Deleted": [{"Key": k} for k in keys_to_drop]}
```

- [ ] **Step 2: Extend `FakeGCSClient`** with delete_blobs:

```python
class FakeGCSBucket:
    # existing blob, list_blobs ...

    def delete_blobs(self, blobs: list) -> None:
        for blob in blobs:
            self._blobs.pop(blob.name, None)
```

- [ ] **Step 3: Append S3 + GCS tests** to `tests/stores/test_delete_run.py`:

```python
def test_s3_delete_run_paginates_and_deletes(fake_s3_client) -> None:
    from kinoforge.stores.s3 import S3ArtifactStore
    store = S3ArtifactStore(bucket="b", prefix="kf/", client=fake_s3_client)
    for i in range(2500):  # exceeds the 1000-per-batch
        store.put_json("r1", f"file-{i}.json", {"i": i})
    store.delete_run("r1")
    assert store.list("r1") == []


def test_s3_delete_run_empty_prefix_idempotent(fake_s3_client) -> None:
    from kinoforge.stores.s3 import S3ArtifactStore
    store = S3ArtifactStore(bucket="b", prefix="kf/", client=fake_s3_client)
    store.delete_run("never-existed")  # no raise


def test_s3_manual_cleanup_command_shape(fake_s3_client) -> None:
    from kinoforge.stores.s3 import S3ArtifactStore
    store = S3ArtifactStore(bucket="my-bucket", prefix="kf/", client=fake_s3_client)
    assert store.manual_cleanup_command("r1") == "aws s3 rm s3://my-bucket/kf/r1/ --recursive"


def test_gcs_delete_run_lists_then_batches(fake_gcs_client) -> None:
    from kinoforge.stores.gcs import GCSArtifactStore
    store = GCSArtifactStore(bucket="b", prefix="kf/", client=fake_gcs_client)
    store.put_json("r1", "a.json", {"k": 1})
    store.put_json("r1", "b.json", {"k": 2})
    store.delete_run("r1")
    assert store.list("r1") == []


def test_gcs_delete_run_empty_prefix_idempotent(fake_gcs_client) -> None:
    from kinoforge.stores.gcs import GCSArtifactStore
    store = GCSArtifactStore(bucket="b", prefix="kf/", client=fake_gcs_client)
    store.delete_run("never-existed")


def test_gcs_manual_cleanup_command_shape(fake_gcs_client) -> None:
    from kinoforge.stores.gcs import GCSArtifactStore
    store = GCSArtifactStore(bucket="my-bucket", prefix="kf/", client=fake_gcs_client)
    assert store.manual_cleanup_command("r1") == "gcloud storage rm -r gs://my-bucket/kf/r1/"
```

- [ ] **Step 4: Run, confirm FAIL.**

- [ ] **Step 5: Implement `S3ArtifactStore.delete_run`.**

```python
# stores/s3.py
def delete_run(self, run_id: str) -> None:
    """Paginate list + batch delete in 1000-per-call chunks (S3 limit)."""
    prefix = f"{self._prefix}{run_id}/"
    paginator = self._client.get_paginator("list_objects_v2") if hasattr(self._client, "get_paginator") else None
    keys: list[dict] = []
    # Simple path — fake_s3_client supports list_objects_v2 directly without paginator.
    resp = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
    for obj in resp.get("Contents", []):
        keys.append({"Key": obj["Key"]})
        if len(keys) == 1000:
            self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})
            keys = []
    if keys:
        self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})

def manual_cleanup_command(self, run_id: str) -> str:
    return f"aws s3 rm s3://{self._bucket}/{self._prefix}{run_id}/ --recursive"
```

(Note: real boto3 uses a paginator for >1000 entries — the implementer should
check the existing `S3ArtifactStore.list()` shape and reuse its paginator logic.)

- [ ] **Step 6: Implement `GCSArtifactStore.delete_run`.**

```python
# stores/gcs.py
def delete_run(self, run_id: str) -> None:
    prefix = f"{self._prefix}{run_id}/"
    bucket = self._client.bucket(self._bucket)
    blobs = list(bucket.list_blobs(prefix=prefix))
    if blobs:
        bucket.delete_blobs(blobs)

def manual_cleanup_command(self, run_id: str) -> str:
    return f"gcloud storage rm -r gs://{self._bucket}/{self._prefix}{run_id}/"
```

- [ ] **Step 7: Run, confirm PASS.** 9 tests in `test_delete_run.py`.

- [ ] **Step 8: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/s3.py src/kinoforge/stores/gcs.py tests/stores/test_delete_run.py tests/stores/conftest.py
git add src/kinoforge/stores/s3.py src/kinoforge/stores/gcs.py tests/stores/test_delete_run.py tests/stores/conftest.py
git commit -m "feat(stores): S3 + GCS delete_run + manual_cleanup_command

S3 paginated list + 1000-batch delete_objects. GCS list_blobs + delete_blobs.
manual_cleanup_command returns the matching CLI command for each.

6/6 new tests pass; 9 total in test_delete_run.py."
```

---

### Task 8: `Ledger.record` + `Ledger.touch` canonical pattern

**Goal:** First application of the canonical write-site pattern. `Ledger.record` and `Ledger.touch` consult `EphemeralSession.current()` (forward reference — Task 14 creates the class; this task uses a stub-aware import) and `RedactionRegistry` before persisting.

**Important sequencing note:** `EphemeralSession` is created in Task 14. To keep tasks atomic, this task uses `try: from kinoforge.core.ephemeral import EphemeralSession; except ImportError: EphemeralSession = None`-style guarded imports. Task 14 removes the guards.

**Files:**
- Modify: `src/kinoforge/core/lifecycle.py` — `Ledger.record` + `Ledger.touch` + `Ledger.forget` adopt the pattern.
- Test: `tests/core/test_ledger_redaction.py` — create with 6 tests.

**Acceptance Criteria:**
- [ ] `Ledger.record(instance)` with no `EphemeralSession`: writes redacted payload to `ledger.json` via `store.put_json`.
- [ ] `Ledger.record` with vault-active registry: instance fields containing prompt/LoRA refs come out redacted in the persisted JSON.
- [ ] `Ledger.touch(instance_id, ts)` follows the same pattern under `heartbeat_ledger_touch` gate.
- [ ] `Ledger.forget(instance_id)` removes from both on-disk ledger AND from any in-memory shadow if `EphemeralSession.current()` is active.
- [ ] Existing `tests/core/test_lifecycle_sweeper.py` continues to pass.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/lifecycle.py tests/core/test_ledger_redaction.py` passes.

**Verify:** `pixi run pytest tests/core/test_ledger_redaction.py tests/core/test_lifecycle_sweeper.py -v` → existing tests + 6 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_ledger_redaction.py`** — 6 tests focusing on the redaction path (ephemeral shadow path is exercised in Task 15's `test_ephemeral_run_cleanup.py`):

```python
"""Tests for Ledger.record / touch / forget redaction.

The EphemeralSession shadow path is exercised in test_ephemeral_run_cleanup.py
(Task 15). This file pins the always-on policy: vault-loaded → ledger entries
contain placeholders for any sensitive substrings.
"""

from pathlib import Path

import pytest

from kinoforge.core.lifecycle import Ledger
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture(autouse=True)
def _clean() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_ledger_record_redacts_when_registry_active(tmp_path: Path) -> None:
    """A registered token appears as a placeholder in the persisted JSON."""
    RedactionRegistry.instance().add("super-secret-style", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    # Use whatever Instance/dataclass Ledger.record expects in current code.
    instance = _fake_instance(label="super-secret-style")
    ledger.record(instance)
    persisted = (tmp_path / "r1" / "ledger.json").read_text()
    assert "super-secret-style" not in persisted
    assert "<lora:label:" in persisted


def test_ledger_record_passthrough_when_registry_empty(tmp_path: Path) -> None:
    """Public-by-design path: empty registry → ledger writes plain."""
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(label="public-label"))
    persisted = (tmp_path / "r1" / "ledger.json").read_text()
    assert "public-label" in persisted


def test_ledger_touch_redacts_heartbeat_payload(tmp_path: Path) -> None:
    """Ledger.touch writes the same shape as record; redaction applies."""
    RedactionRegistry.instance().add("super-secret-style", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1", label="super-secret-style"))
    ledger.touch("i1", heartbeat_ts=12345.0)
    persisted = (tmp_path / "r1" / "ledger.json").read_text()
    assert "super-secret-style" not in persisted


def test_ledger_forget_removes_from_disk(tmp_path: Path) -> None:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1"))
    ledger.forget("i1")
    assert "i1" not in (tmp_path / "r1" / "ledger.json").read_text()


def test_ledger_record_within_existing_single_flight_lock(tmp_path: Path) -> None:
    """The Phase 18 single-flight lock still wraps the persistent path —
    redaction does not interfere with the lock semantics."""
    # The lock check is the no-corruption guarantee — assert by writing
    # twice in quick succession and reading back a coherent payload.
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1"))
    ledger.record(_fake_instance(id="i2"))
    import json
    payload = json.loads((tmp_path / "r1" / "ledger.json").read_text())
    assert set(payload.keys()) == {"i1", "i2"}


def test_ledger_record_longer_token_wins(tmp_path: Path) -> None:
    """redact() applies tokens longest-first — confirm the path is exercised
    through redact_json."""
    RedactionRegistry.instance().add("style", kind="lora:label")
    RedactionRegistry.instance().add("super-style", kind="lora:label")
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="r1")
    ledger.record(_fake_instance(id="i1", label="super-style"))
    persisted = (tmp_path / "r1" / "ledger.json").read_text()
    assert "super-style" not in persisted
    assert "style" not in persisted


def _fake_instance(*, id: str = "i1", label: str = "x"):
    """Adapt to the actual Instance dataclass from core/lifecycle.py."""
    # Implementer: use the existing Instance shape from core/lifecycle.py.
    ...
```

- [ ] **Step 2: Run, confirm FAIL** (redaction not yet applied).

- [ ] **Step 3: Edit `src/kinoforge/core/lifecycle.py` — `Ledger.record`** to use the canonical pattern with guarded `EphemeralSession` import:

```python
# top of lifecycle.py
try:
    from kinoforge.core.ephemeral import EphemeralSession
except ImportError:  # ephemeral module not yet present in Sub-α/β
    EphemeralSession = None  # type: ignore[assignment,misc]

from kinoforge.core.redaction import RedactionRegistry


class Ledger:
    # ... existing fields, single-flight lock, etc. ...

    def record(self, instance: Instance) -> None:
        session = EphemeralSession.current() if EphemeralSession is not None else None
        if session is not None and not session.policy.ledger_record:
            session.in_memory_ledger[instance.id] = asdict(instance)
            return
        with self._store.acquire_lock(self._run_id, "ledger.lock", ttl_s=self.mutate_ttl_s):
            entries = self._load_or_empty()
            redacted = RedactionRegistry.instance().redact_json(asdict(instance))
            entries[instance.id] = redacted
            self._store.put_json(self._run_id, "ledger.json", entries)

    def touch(self, instance_id: str, heartbeat_ts: float) -> None:
        session = EphemeralSession.current() if EphemeralSession is not None else None
        if session is not None and not session.policy.heartbeat_ledger_touch:
            shadow = session.in_memory_ledger.setdefault(instance_id, {})
            shadow["last_heartbeat"] = heartbeat_ts
            return
        with self._store.acquire_lock(self._run_id, "ledger.lock", ttl_s=self.mutate_ttl_s):
            entries = self._load_or_empty()
            if instance_id not in entries:
                return
            entries[instance_id]["last_heartbeat"] = heartbeat_ts
            redacted = RedactionRegistry.instance().redact_json(entries)
            self._store.put_json(self._run_id, "ledger.json", redacted)

    def forget(self, instance_id: str) -> None:
        session = EphemeralSession.current() if EphemeralSession is not None else None
        if session is not None:
            session.in_memory_ledger.pop(instance_id, None)
        # Always also remove from disk (idempotent: missing is fine).
        with self._store.acquire_lock(self._run_id, "ledger.lock", ttl_s=self.mutate_ttl_s):
            entries = self._load_or_empty()
            if instance_id in entries:
                entries.pop(instance_id)
                redacted = RedactionRegistry.instance().redact_json(entries)
                self._store.put_json(self._run_id, "ledger.json", redacted)
```

- [ ] **Step 4: Run, confirm PASS.** 6 new tests + existing `test_lifecycle_sweeper.py` pass.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/lifecycle.py tests/core/test_ledger_redaction.py
git add src/kinoforge/core/lifecycle.py tests/core/test_ledger_redaction.py
git commit -m "feat(core): Ledger.record/touch/forget canonical redaction pattern

First adopter of the canonical sink shape:
  session = EphemeralSession.current() if EphemeralSession is not None else None
  if session and not session.policy.<gate>: shadow; return
  payload = RedactionRegistry.instance().redact_json(...); store.put_json(...)

Guarded EphemeralSession import (Task 14 creates the class; Task 15 removes
the guard). Existing single-flight lock semantics preserved.

6/6 redaction tests pass; existing lifecycle tests pass."
```

---

### Task 9: `JsonProfileCache` alias-keyed + canonical redaction

**Goal:** Profile cache keyed by `alias: str` instead of `CapabilityKey.derive()` hash on disk. `_persist` adopts the canonical pattern. `ModelProfile.name` persisted as the alias.

**Files:**
- Modify: `src/kinoforge/core/profiles.py` — `resolve_or_discover` signature, `_persist`, alias-keyed path.
- Test: `tests/core/test_profile_cache_redaction.py` — create with 7 tests.

**Acceptance Criteria:**
- [ ] `JsonProfileCache.resolve_or_discover(alias, capability_key, engine, backend, *, discover_ttl_s=30.0)` is the new signature. `alias: str` replaces what was previously `key.derive()` on disk.
- [ ] `capability_key` is still threaded for `engine.declared_flags(key)` and for `discover()`.
- [ ] Disk file path is `profiles/<alias>.json`.
- [ ] Persisted JSON has `"name": <alias>` (not the human-readable name).
- [ ] Persisted JSON contains no LoRA refs (assertion: no string in the file matches any registered `lora:ref` token).
- [ ] Cross-process lock filename is `<alias>.lock`.
- [ ] Backward compat: a previously-written `profiles/<hash>.json` is still readable for public-by-design runs (alias for non-vault runs == hash).
- [ ] `_persist` follows canonical pattern (gate is `profile_cache_persist`).
- [ ] `pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_profile_cache_redaction.py` passes.

**Verify:** `pixi run pytest tests/core/test_profile_cache_redaction.py tests/core/test_profiles.py -v` → existing 13 tests pass + 7 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_profile_cache_redaction.py`** — 7 tests covering alias-keying and redaction.

```python
"""Tests for JsonProfileCache alias keying + canonical redaction pattern."""

from pathlib import Path

import pytest

from kinoforge.core.profiles import JsonProfileCache
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture(autouse=True)
def _clean() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_cache_persists_at_alias_path(tmp_path: Path) -> None:
    """profiles/<alias>.json — not profiles/<hash>.json."""
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="profiles")
    profile = _fake_profile(name="ignored")
    cache._persist("cfg-a3f7e1b2c4d5", profile)
    assert (tmp_path / "profiles" / "profiles" / "cfg-a3f7e1b2c4d5.json").exists()


def test_persisted_name_field_is_alias(tmp_path: Path) -> None:
    """ModelProfile.name on disk is the alias, not the human name."""
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="profiles")
    cache._persist("cfg-a3f7e1b2c4d5", _fake_profile(name="Wan + secret-LoRA fp16"))
    import json
    persisted = json.loads(
        (tmp_path / "profiles" / "profiles" / "cfg-a3f7e1b2c4d5.json").read_text()
    )
    assert persisted["name"] == "cfg-a3f7e1b2c4d5"
    assert "secret-LoRA" not in str(persisted)


def test_lora_ref_never_in_persisted_json(tmp_path: Path) -> None:
    """Any registered lora:ref token does not appear in the persisted profile."""
    RedactionRegistry.instance().add("civitai:1234@5678", kind="lora:ref")
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="profiles")
    p = _fake_profile(name="civitai:1234@5678 + wan")
    cache._persist("cfg-x", p)
    text = (tmp_path / "profiles" / "profiles" / "cfg-x.json").read_text()
    assert "civitai:1234@5678" not in text


def test_resolve_signature_takes_alias_and_capability_key(tmp_path: Path) -> None:
    """resolve_or_discover(alias, capability_key, engine, backend, ...) — both passed."""
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="profiles")
    engine, backend = _fake_engine_and_backend()
    cap_key = _fake_capability_key()
    profile = cache.resolve_or_discover("cfg-x", cap_key, engine, backend)
    assert profile is not None


def test_lock_file_uses_alias(tmp_path: Path) -> None:
    """Cross-process lock (Phase 18) is keyed by alias, not by hash."""
    # Inspect the lock filename used inside resolve_or_discover —
    # check the store sees a lock acquire on '<alias>.lock'.
    ...  # implementer: instrument store.acquire_lock and assert on the lock key


def test_backward_compat_hash_key_still_readable(tmp_path: Path) -> None:
    """A previously-written profiles/<hash>.json (no vault) loads identically."""
    store = LocalArtifactStore(root=tmp_path)
    cache = JsonProfileCache(store=store, run_id="profiles")
    cache._persist("abc123def456" * 4, _fake_profile(name="public"))  # hash-shaped alias
    cap_key = _fake_capability_key()
    engine, backend = _fake_engine_and_backend()
    # Resolve with the hash-shaped alias as if no vault was loaded.
    profile = cache.resolve_or_discover("abc123def456abc123def456abc123def456abc123def456abc123def456abc1", cap_key, engine, backend)
    # Should round-trip without re-discovery.
    assert profile is not None


def test_persist_canonical_pattern_with_session(tmp_path: Path) -> None:
    """When EphemeralSession.policy.profile_cache_persist is False, _persist
    writes to in_memory_profiles instead of disk."""
    # Stub EphemeralSession for now via a context manager.
    # Full integration tested in tests/core/test_ephemeral_run_cleanup.py (Task 15).
    ...


def _fake_profile(name: str):
    from kinoforge.core.interfaces import ModelProfile
    return ModelProfile(name=name, max_frames=80, fps=16, supported_modes={"t2v"}, max_resolution=(720, 480), supports_native_extension=False, supports_joint_audio=False)


def _fake_capability_key():
    from kinoforge.core.interfaces import CapabilityKey
    return CapabilityKey(base_model="hf:foo/bar", loras=(), engine="fake", precision="fp16")


def _fake_engine_and_backend():
    from kinoforge.engines.fake import FakeEngine
    e = FakeEngine()
    b = e.backend(None, {})
    return e, b
```

- [ ] **Step 2: Run, confirm FAIL** (signature mismatch or path mismatch).

- [ ] **Step 3: Edit `src/kinoforge/core/profiles.py`.** Change `resolve_or_discover` signature; update `_persist` to canonical pattern; persist with `name=alias`.

```python
def resolve_or_discover(
    self,
    alias: str,
    capability_key: CapabilityKey,
    engine: GenerationEngine,
    backend: GenerationBackend,
    *,
    discover_ttl_s: float = 30.0,
) -> ModelProfile:
    # ... existing single-flight + cache-hit fast path, but keyed on alias ...

def _persist(self, alias: str, profile: ModelProfile) -> None:
    session = EphemeralSession.current() if EphemeralSession is not None else None
    if session is not None and not session.policy.profile_cache_persist:
        session.in_memory_profiles[alias] = profile
        return
    payload = self._profile_to_dict(profile)
    payload["name"] = alias  # the persisted display name is the alias itself
    payload = RedactionRegistry.instance().redact_json(payload)
    self._store.put_json(self._run_id, f"profiles/{alias}.json", payload)
```

- [ ] **Step 4: Update existing callers** (orchestrator threads the alias in addition to the capability_key). Search via `rg "resolve_or_discover" src/`; touch each call site to pass `alias` as first arg.

- [ ] **Step 5: Run, confirm PASS.** Existing `tests/core/test_profiles.py` (13 tests) pass + 7 new pass.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_profile_cache_redaction.py
git add src/kinoforge/core/profiles.py tests/core/test_profile_cache_redaction.py
git commit -m "feat(core): JsonProfileCache alias-keyed + canonical redaction

resolve_or_discover(alias, capability_key, ...) — alias is the on-disk key.
_persist writes profiles/<alias>.json with name=alias (no human-readable name
on disk). lora:ref tokens registered by the vault loader cannot appear in
persisted profile bodies. Backward-compat: alias for no-vault runs is the
CapabilityKey.derive() hash, so existing cache entries still resolve.

7/7 new tests pass; 13/13 existing test_profiles.py tests pass."
```

---

### Task 10: `batch_generate` summary canonical pattern

**Goal:** `_write_summary` in `core/batch.py` consults `EphemeralSession` (skip under ephemeral) and `RedactionRegistry` (redact under default). Per-entry results stay in process memory regardless.

**Files:**
- Modify: `src/kinoforge/core/batch.py` — `_write_summary` adopts canonical pattern.
- Test: `tests/core/test_batch_summary_skipped.py` — 4 tests.

**Acceptance Criteria:**
- [ ] `_write_summary` with default policy: writes redacted `_batch_summary.json` via `store.put_json`.
- [ ] `_write_summary` with `EphemeralSession` active (`batch_summary_write=False`): nothing written to disk; per-entry results returned to caller in process memory.
- [ ] Existing `tests/core/test_batch_generate.py` continues to pass.
- [ ] No new top-level field on `BatchResult`; the per-entry list is returned to caller as today.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_summary_skipped.py` passes.

**Verify:** `pixi run pytest tests/core/test_batch_summary_skipped.py tests/core/test_batch_generate.py -v` → existing 10 tests pass + 4 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_batch_summary_skipped.py`** — 4 tests:

```python
"""Tests for batch summary write canonical pattern."""

from pathlib import Path

import pytest

from kinoforge.core.batch import batch_generate, BatchEntry, BatchManifest
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture(autouse=True)
def _clean() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_summary_written_in_default_mode(tmp_path: Path) -> None:
    """No vault, no ephemeral: _batch_summary.json present."""
    _run_minimal_batch(tmp_path)
    assert (tmp_path / "_batch_summary.json").exists()


def test_summary_redacts_prompt_tokens(tmp_path: Path) -> None:
    """When the registry has a token, the summary file contains placeholders."""
    RedactionRegistry.instance().add("super-secret-prompt", kind="prompt:positive")
    _run_minimal_batch(tmp_path, prompts=["super-secret-prompt"])
    persisted = (tmp_path / "_batch_summary.json").read_text()
    assert "super-secret-prompt" not in persisted


def test_summary_skipped_in_ephemeral(tmp_path: Path) -> None:
    """Under EphemeralSession.policy.batch_summary_write=False, no file written.
    Per-entry results still returned to caller in process memory."""
    from kinoforge.core.ephemeral import EphemeralSession  # available after Task 14
    with EphemeralSession(enabled=True):
        result = _run_minimal_batch(tmp_path)
    assert not (tmp_path / "_batch_summary.json").exists()
    assert len(result.entries) > 0  # in-memory list still populated


def test_summary_path_unchanged(tmp_path: Path) -> None:
    """Filename and root path stay as today — no surprise relocations."""
    _run_minimal_batch(tmp_path)
    assert (tmp_path / "_batch_summary.json").exists()


def _run_minimal_batch(tmp_path: Path, prompts: list[str] | None = None):
    """Helper using FakeEngine + LocalProvider + minimal manifest."""
    # Implementer: build via existing batch fixtures from tests/core/test_batch_generate.py.
    ...
```

Note: `test_summary_skipped_in_ephemeral` cross-references `EphemeralSession` from Task 14. Run this test only AFTER Task 14 lands. Skip it with `@pytest.mark.skip(reason="depends on Task 14")` until then; remove the skip in the Task 14 commit.

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Edit `src/kinoforge/core/batch.py` `_write_summary` to canonical pattern.**

```python
def _write_summary(self, payload: dict) -> None:
    session = EphemeralSession.current() if EphemeralSession is not None else None
    if session is not None and not session.policy.batch_summary_write:
        return  # in-memory only; per-entry list already in BatchResult
    redacted = RedactionRegistry.instance().redact_json(payload)
    self._store.put_json(self._batch_id, "_batch_summary.json", redacted)
```

- [ ] **Step 4: Run, confirm PASS.** 4 new + 10 existing pass.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch_summary_skipped.py
git add src/kinoforge/core/batch.py tests/core/test_batch_summary_skipped.py
git commit -m "feat(core): batch_generate _write_summary canonical pattern

_batch_summary.json skipped entirely under ephemeral; redacted under default.
Per-entry results stay in process memory regardless of mode.

4/4 new tests pass (one skipped pending Task 14); 10/10 existing batch
tests pass."
```

---

### Task 11: `LocalOutputSink.publish` registers basename

**Goal:** Two-line addition at `OutputSink.publish` — register the published basename with `RedactionRegistry` so every downstream log line / stdout / JSON summary / traceback substitutes `<output:<hash6>>`. The file on disk in the output dir keeps its permissive name.

**Files:**
- Modify: `src/kinoforge/stores/sinks.py` — `LocalOutputSink.publish` registers basename.
- Test: `tests/stores/test_sink_basename_registration.py` — create with 3 tests.

**Acceptance Criteria:**
- [ ] After `LocalOutputSink.publish(b"video", meta)`, `RedactionRegistry.instance().is_active` is True and `redact(str(returned_path))` substitutes the basename.
- [ ] The file on disk has its permissive filename — registration does not rename.
- [ ] `LocalOutputSink.publish` returns the full path (existing contract preserved).
- [ ] When `RedactionRegistry` already has the basename registered (re-publish of identical name), `add` is idempotent (no error).
- [ ] `pixi run pre-commit run --files src/kinoforge/stores/sinks.py tests/stores/test_sink_basename_registration.py` passes.

**Verify:** `pixi run pytest tests/stores/test_sink_basename_registration.py tests/stores/test_sinks.py -v` → existing + 3 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/stores/test_sink_basename_registration.py`:**

```python
"""LocalOutputSink.publish registers basename with RedactionRegistry."""

from pathlib import Path

import pytest

from kinoforge.core.redaction import RedactionRegistry
from kinoforge.stores.sinks import LocalOutputSink


@pytest.fixture(autouse=True)
def _clean() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_publish_registers_basename(tmp_path: Path) -> None:
    sink = LocalOutputSink(output_dir=tmp_path)
    path_str = sink.publish(b"video bytes", meta={
        "timestamp": "20260608-1200", "provider": "fake", "model": "x", "prompt": "a cinematic shot"})
    p = Path(path_str)
    redacted = RedactionRegistry.instance().redact(path_str)
    assert p.name not in redacted
    assert "<output:" in redacted
    # The user-configured output dir path prefix remains visible.
    assert str(tmp_path) in redacted


def test_published_file_keeps_permissive_name(tmp_path: Path) -> None:
    """Registration does not rename the file on disk."""
    sink = LocalOutputSink(output_dir=tmp_path)
    path_str = sink.publish(b"video bytes", meta={
        "timestamp": "20260608-1200", "provider": "fake", "model": "x", "prompt": "a cinematic shot"})
    assert Path(path_str).exists()


def test_publish_idempotent_re_register(tmp_path: Path) -> None:
    """Publishing twice with the same filename: registry's add is idempotent."""
    sink = LocalOutputSink(output_dir=tmp_path)
    sink.publish(b"bytes-a", meta={
        "timestamp": "20260608-1200", "provider": "fake", "model": "x", "prompt": "same"})
    sink.publish(b"bytes-b", meta={
        "timestamp": "20260608-1200", "provider": "fake", "model": "x", "prompt": "same"})
    # No raise — second add() is a no-op.
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Edit `src/kinoforge/stores/sinks.py` `LocalOutputSink.publish`.**

```python
from kinoforge.core.redaction import RedactionRegistry

class LocalOutputSink:
    # ... existing fields, format_filename, etc. ...

    def publish(self, payload: bytes, meta: dict) -> str:
        filename = self._format_filename(meta)
        path     = self._output_dir / filename
        path.write_bytes(payload)
        RedactionRegistry.instance().add(filename, kind="output")
        return str(path)
```

- [ ] **Step 4: Run, confirm PASS.** Existing sink tests + 3 new pass.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/stores/sinks.py tests/stores/test_sink_basename_registration.py
git add src/kinoforge/stores/sinks.py tests/stores/test_sink_basename_registration.py
git commit -m "feat(stores): LocalOutputSink.publish registers basename for downstream redaction

Two-line addition. The file on disk keeps its permissive filename; every
downstream surface that interpolates path or basename (logs, stdout, JSON
summary, error tracebacks) substitutes <output:<hash6>>. Output-dir path
prefix stays visible.

3/3 new tests pass."
```

---

### Task 12: `Downloader.download(opaque_name=True)` path

**Goal:** Downloader gains optional `opaque_name=True` path; writes `<sha256>.bin`; registers original filename with `RedactionRegistry`. Provisioner (called in Task 13 wiring) sets the flag when the engine runs locally.

**Files:**
- Modify: `src/kinoforge/core/downloader.py` — add `opaque_name` param + sha256-required path + registration.
- Test: `tests/core/test_downloader_opaque_name.py` — 5 tests.

**Acceptance Criteria:**
- [ ] `download(artifact, target_dir, opaque_name=True)` writes `target_dir/<sha256>.bin` (NOT `target_dir/<artifact.filename>`).
- [ ] `opaque_name=True` with `artifact.sha256 is None` raises `ValueError` ("opaque-naming requires sha256").
- [ ] `download(artifact, target_dir, opaque_name=True)` registers `artifact.filename` with `RedactionRegistry` (kind=`lora:filename`) BEFORE the download begins.
- [ ] Resume `.part` file uses `<sha256>.bin.part` shape (no original name on disk).
- [ ] Default `opaque_name=False` preserves existing behavior — backward compat.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/downloader.py tests/core/test_downloader_opaque_name.py` passes.

**Verify:** `pixi run pytest tests/core/test_downloader_opaque_name.py tests/core/test_downloader.py -v` → existing 8 tests pass + 5 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/core/test_downloader_opaque_name.py`.**

```python
"""Tests for Downloader opaque_name path."""

from pathlib import Path

import pytest

from kinoforge.core.downloader import Downloader
from kinoforge.core.interfaces import Artifact
from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clean() -> None:
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_opaque_name_writes_sha_filename(tmp_path: Path, http_server) -> None:
    """opaque_name=True → target is target_dir/<sha>.bin."""
    art = Artifact(url=http_server.url("/foo.safetensors"), filename="foo.safetensors",
                   sha256="aaaaaa...the-actual-sha", size=1234, headers={})
    Downloader().download(art, tmp_path, opaque_name=True)
    assert (tmp_path / "aaaaaa...the-actual-sha.bin").exists()
    assert not (tmp_path / "foo.safetensors").exists()


def test_opaque_name_requires_sha(tmp_path: Path, http_server) -> None:
    art = Artifact(url=http_server.url("/foo.safetensors"), filename="foo.safetensors",
                   sha256=None, size=1234, headers={})
    with pytest.raises(ValueError, match="sha256"):
        Downloader().download(art, tmp_path, opaque_name=True)


def test_opaque_name_registers_filename(tmp_path: Path, http_server) -> None:
    """Original filename registered with RedactionRegistry BEFORE the download."""
    art = Artifact(url=http_server.url("/foo.safetensors"), filename="my_secret_lora_v3.safetensors",
                   sha256="abc123", size=10, headers={})
    Downloader().download(art, tmp_path, opaque_name=True)
    out = RedactionRegistry.instance().redact("downloading my_secret_lora_v3.safetensors")
    assert "my_secret_lora_v3" not in out
    assert "<lora:filename:" in out


def test_part_file_uses_sha_shape(tmp_path: Path, http_server) -> None:
    """Resume mechanic: .part file is named <sha>.bin.part — no original name."""
    # Trigger a partial download by interrupting; observe the .part filename.
    ...  # implementer: use the existing http_server fixture's chunk-stream helper


def test_default_opaque_name_false_preserves_behavior(tmp_path: Path, http_server) -> None:
    """Existing callers (no opaque_name kwarg) keep the existing behavior."""
    art = Artifact(url=http_server.url("/foo.safetensors"), filename="foo.safetensors",
                   sha256="abc123", size=10, headers={})
    Downloader().download(art, tmp_path)  # default opaque_name=False
    assert (tmp_path / "foo.safetensors").exists()
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Edit `src/kinoforge/core/downloader.py`.**

```python
from kinoforge.core.redaction import RedactionRegistry

class Downloader:
    def download(self, artifact: Artifact, target_dir: Path, *, opaque_name: bool = False) -> Path:
        if opaque_name:
            if not artifact.sha256:
                raise ValueError("opaque-naming requires Artifact.sha256")
            target_name = f"{artifact.sha256}.bin"
            RedactionRegistry.instance().add(artifact.filename, kind="lora:filename")
        else:
            target_name = artifact.filename
        target = target_dir / target_name
        # ... existing fetch / resume / verify logic, using target as the destination ...
        return target
```

- [ ] **Step 4: Run, confirm PASS.** Existing 8 + 5 new pass.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/downloader.py tests/core/test_downloader_opaque_name.py
git add src/kinoforge/core/downloader.py tests/core/test_downloader_opaque_name.py
git commit -m "feat(core): Downloader opaque_name path with sha-shaped filenames

opaque_name=True writes <sha256>.bin and registers Artifact.filename with
the RedactionRegistry before download begins. Sha required; ValueError
otherwise. Default behavior unchanged.

5/5 new tests pass; 8/8 existing downloader tests pass."
```

---

### Task 13: `GenerateClipStage` opaque store names + per-engine `_save_fixture` registry check

**Goal:** Two bundled changes that close related sink-write paths:
1. `GenerateClipStage._persist_clip` uses `opaque_store_name(payload, ext)` at every `put_bytes`. CI invariant AC2 will enforce this in Task 19.
2. Every engine's `_save_fixture` consults `RedactionRegistry.instance().is_active` and refuses when active (belt-and-suspenders with `EphemeralSession` once Task 14 lands).

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` — `put_bytes` calls.
- Modify: every engine `_save_fixture` site: `engines/replicate/__init__.py`, `engines/runway/__init__.py`, `engines/fal/__init__.py`, `engines/hosted/__init__.py`, `engines/comfyui/__init__.py`, `engines/diffusers/__init__.py`, `engines/fake/__init__.py` (subset that actually has `_save_fixture` — search via `rg "_save_fixture" src/`).
- Test: `tests/pipeline/test_opaque_store_name.py` — 4 tests.
- Test: `tests/engines/test_fixture_capture_refused.py` — 4 tests.

**Acceptance Criteria:**
- [ ] `GenerateClipStage._persist_clip` calls `store.put_bytes(run_id, opaque_store_name(payload, ext), payload)` — the second arg comes from `opaque_store_name`.
- [ ] Existing `tests/pipeline/test_generate_clip.py` continues to pass (the change is a filename-format-only swap; tests assert on bytes/URIs not on the exact filename string).
- [ ] Each engine's `_save_fixture` checks `RedactionRegistry.instance().is_active` at top of method; refuses (returns early with `logger.warning`) if active.
- [ ] CI invariant AC2 (added in Task 19) will fail any future `put_bytes` site that does not use `opaque_store_name` — but this task closes every existing site by hand.
- [ ] `pixi run pre-commit run --files <every touched file>` passes.

**Verify:** `pixi run pytest tests/pipeline/test_opaque_store_name.py tests/engines/test_fixture_capture_refused.py tests/pipeline/ tests/engines/ -v` → all pass.

**Steps:**

- [ ] **Step 1: Locate every `put_bytes` call in `src/kinoforge/pipeline/generate_clip.py`.**

```bash
rg -n "put_bytes" src/kinoforge/pipeline/
```

- [ ] **Step 2: Locate every `_save_fixture` definition.**

```bash
rg -n "_save_fixture" src/kinoforge/engines/
```

- [ ] **Step 3: Write `tests/pipeline/test_opaque_store_name.py`** with 4 tests asserting that store-side filenames come from `opaque_store_name`:

```python
"""GenerateClipStage uses opaque_store_name at every put_bytes call site."""

from pathlib import Path
import re

import pytest

from kinoforge.core.artifacts import opaque_store_name


def test_persisted_filename_matches_opaque_shape(tmp_path: Path) -> None:
    """The store-side name is <16-hex>[.ext] — never derived from prompt."""
    # Run GenerateClipStage with a FakeBackend that returns known bytes.
    # Assert store.list(run_id) entries match _OPAQUE_RE.
    from kinoforge.stores.local import LocalArtifactStore
    store = LocalArtifactStore(root=tmp_path)
    # ... build minimal GenerateClipStage; run; assert store.list("run-1")
    OPAQUE_RE = re.compile(r"^[0-9a-f]{16}(\.[A-Za-z0-9]{1,5})?$")
    for name in store.list("run-1"):
        assert OPAQUE_RE.fullmatch(name), f"non-opaque filename leaked: {name!r}"


def test_no_prompt_slug_in_store_names(tmp_path: Path) -> None:
    """Even if the engine returns Artifact.filename='cinematic-shot-of-a.mp4',
    the store filename is sha-derived."""
    ...


def test_opaque_name_uses_payload_bytes(tmp_path: Path) -> None:
    """Sanity: filename is opaque_store_name(payload, ext) — same bytes →
    same name."""
    name1 = opaque_store_name(b"abc", ".mp4")
    name2 = opaque_store_name(b"abc", ".mp4")
    assert name1 == name2


def test_extension_preserved_through_stage(tmp_path: Path) -> None:
    """ext is taken from Artifact.filename suffix; extension survives."""
    ...
```

- [ ] **Step 4: Write `tests/engines/test_fixture_capture_refused.py`** with 4 tests (one per fixture-capturing engine — Replicate, Runway, Fal, Hosted — matching `rg` output):

```python
"""_save_fixture refuses when RedactionRegistry is active."""

import os
from pathlib import Path

import pytest

from kinoforge.core.redaction import RedactionRegistry


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    RedactionRegistry.instance().clear_session()
    monkeypatch.setenv("KINOFORGE_SAVE_FIXTURES", "1")
    yield
    RedactionRegistry.instance().clear_session()


def test_replicate_save_fixture_refused_when_registry_active(tmp_path: Path) -> None:
    RedactionRegistry.instance().add("super-secret", kind="prompt:positive")
    # Call _save_fixture directly with a backend instance; assert no file written.
    from kinoforge.engines.replicate import ReplicateBackend
    backend = ReplicateBackend(...)  # minimal stub
    backend._fixture_dir = tmp_path
    backend._save_fixture("test.json", {"prompt": "super-secret"})
    assert not (tmp_path / "test.json").exists()


def test_runway_save_fixture_refused(tmp_path: Path) -> None:
    ...


def test_fal_save_fixture_refused(tmp_path: Path) -> None:
    ...


def test_hosted_save_fixture_refused(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 5: Edit `GenerateClipStage._persist_clip`.**

```python
from kinoforge.core.artifacts import opaque_store_name

class GenerateClipStage:
    def _persist_clip(self, payload: bytes, artifact: Artifact) -> str:
        ext = Path(artifact.filename).suffix
        store_name = opaque_store_name(payload, ext)
        return self._store.put_bytes(self._run_id, store_name, payload)
```

- [ ] **Step 6: Edit every `_save_fixture` site** to insert the registry check at top:

```python
def _save_fixture(self, name: str, payload: dict) -> None:
    if os.environ.get("KINOFORGE_SAVE_FIXTURES") != "1":
        return
    if RedactionRegistry.instance().is_active:
        logger.warning("fixture capture refused: redaction registry is active (vault loaded)")
        return
    self._fixture_dir.mkdir(exist_ok=True, parents=True)
    (self._fixture_dir / name).write_text(json.dumps(payload, indent=2))
```

(Add the EphemeralSession check in Task 15 — for now, registry check is sufficient since vault-loaded ⇒ registry active.)

- [ ] **Step 7: Run, confirm PASS.** 4 + 4 = 8 new tests; existing pipeline + engine tests pass.

- [ ] **Step 8: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py src/kinoforge/engines/replicate/__init__.py src/kinoforge/engines/runway/__init__.py src/kinoforge/engines/fal/__init__.py src/kinoforge/engines/hosted/__init__.py tests/pipeline/test_opaque_store_name.py tests/engines/test_fixture_capture_refused.py
git add src/kinoforge/pipeline/generate_clip.py src/kinoforge/engines/ tests/pipeline/test_opaque_store_name.py tests/engines/test_fixture_capture_refused.py
git commit -m "feat(pipeline,engines): opaque store names + _save_fixture refuses when registry active

GenerateClipStage._persist_clip routes payload+ext through opaque_store_name
so every put_bytes call uses a sha-derived 16-hex filename. Each engine's
_save_fixture checks RedactionRegistry.instance().is_active and refuses with
a WARNING when the vault has registered any tokens.

8/8 new tests pass; existing pipeline + engine tests pass. Closes Sub-β."
```

---

### Task 14: `core/ephemeral.py` — EphemeralSession + EphemeralPolicy + EPHEMERAL_CAPABILITIES

**Goal:** The central context manager + policy dataclass + pre-flight capability table. Plus removing the guarded-import shims added in Tasks 8-13.

**Files:**
- Create: `src/kinoforge/core/ephemeral.py`
- Modify: `src/kinoforge/core/errors.py` — add `EphemeralError` base.
- Modify: `src/kinoforge/core/lifecycle.py` + `core/profiles.py` + `core/batch.py` + engine `_save_fixture` sites — remove `try: from ... import EphemeralSession; except ImportError` guards; direct import now.
- Test: `tests/core/test_ephemeral.py` — 10 tests.

**Acceptance Criteria:**
- [ ] `EphemeralPolicy` is `@dataclass(frozen=True)` with all 10 fields from spec §8.1.
- [ ] `DEFAULT_POLICY` and `STRICT_POLICY` module-level constants match the spec values exactly.
- [ ] `EphemeralSession(enabled=True).policy == STRICT_POLICY`.
- [ ] `EphemeralSession.current()` returns `None` outside any `with` block.
- [ ] Inside `with EphemeralSession(enabled=...) as s: EphemeralSession.current() is s`.
- [ ] `EphemeralSession.current()` propagates into `ThreadPoolExecutor` workers (via `contextvars`).
- [ ] `EphemeralSession.in_memory_ledger`, `in_memory_profiles` are empty dicts on init.
- [ ] `EphemeralSession.register_store(store, run_id)` appends to `_registered_stores`.
- [ ] `EPHEMERAL_CAPABILITIES` matches spec Appendix B (11 entries).
- [ ] `pixi run pre-commit run --files src/kinoforge/core/ephemeral.py tests/core/test_ephemeral.py` passes.

**Verify:** `pixi run pytest tests/core/test_ephemeral.py -v` → 10 tests pass; full suite still passes (`pixi run test`).

**Steps:**

- [ ] **Step 1: Write `tests/core/test_ephemeral.py`** with 10 tests for policy + session lifecycle (the __exit__ cleanup tests live in `test_ephemeral_run_cleanup.py`, Task 15):

```python
"""Tests for EphemeralSession / EphemeralPolicy / EPHEMERAL_CAPABILITIES."""

import concurrent.futures

import pytest

from kinoforge.core.ephemeral import (
    DEFAULT_POLICY,
    EPHEMERAL_CAPABILITIES,
    EphemeralPolicy,
    EphemeralSession,
    STRICT_POLICY,
)


def test_policy_is_frozen() -> None:
    """Policy is immutable — mutation attempts raise."""
    with pytest.raises(AttributeError):
        DEFAULT_POLICY.ledger_record = False  # type: ignore[misc]


def test_default_policy_all_gates_open() -> None:
    """Default mode: writes happen, no delete-on-completion."""
    assert DEFAULT_POLICY.ledger_record is True
    assert DEFAULT_POLICY.profile_cache_persist is True
    assert DEFAULT_POLICY.batch_summary_write is True
    assert DEFAULT_POLICY.delete_on_completion is False


def test_strict_policy_all_gates_closed() -> None:
    """Ephemeral mode: skips writes, deletes on completion."""
    assert STRICT_POLICY.ledger_record is False
    assert STRICT_POLICY.profile_cache_persist is False
    assert STRICT_POLICY.batch_summary_write is False
    assert STRICT_POLICY.delete_on_completion is True
    assert STRICT_POLICY.delete_retries == 3


def test_session_current_none_outside_with() -> None:
    assert EphemeralSession.current() is None


def test_session_current_inside_with() -> None:
    with EphemeralSession(enabled=True) as s:
        assert EphemeralSession.current() is s


def test_session_enabled_uses_strict_policy() -> None:
    with EphemeralSession(enabled=True) as s:
        assert s.policy == STRICT_POLICY


def test_session_disabled_uses_default_policy() -> None:
    with EphemeralSession(enabled=False) as s:
        assert s.policy == DEFAULT_POLICY


def test_session_propagates_through_threadpool() -> None:
    """contextvars propagate the active session into ThreadPoolExecutor workers.
    Would-fail-bug: using threading.local would lose the session in workers."""
    with EphemeralSession(enabled=True):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            results = list(ex.map(lambda _: EphemeralSession.current() is not None, range(4)))
    assert all(results)


def test_session_register_store_appends() -> None:
    from kinoforge.stores.local import LocalArtifactStore
    with EphemeralSession(enabled=True) as s:
        store = LocalArtifactStore(root="/tmp/test")  # type: ignore[arg-type]
        s.register_store(store, "run-1")
        assert (store, "run-1") in s._registered_stores


def test_capability_table_contents() -> None:
    """Table contents match spec Appendix B."""
    assert EPHEMERAL_CAPABILITIES[("comfyui", "runpod")] is True
    assert EPHEMERAL_CAPABILITIES[("replicate", None)] is True
    assert EPHEMERAL_CAPABILITIES[("runway", None)] is True
    assert EPHEMERAL_CAPABILITIES[("fal", None)] is False
    assert EPHEMERAL_CAPABILITIES[("luma", None)] is False
    assert EPHEMERAL_CAPABILITIES[("hosted", None)] is False
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Write `src/kinoforge/core/ephemeral.py`.**

```python
"""EphemeralSession + EphemeralPolicy + pre-flight capability table.

EphemeralSession is a context manager carried via contextvars so it
propagates through ThreadPoolExecutor workers (kinoforge's ConcurrentPool,
Phase 17). Inside the with-block, every persistent-write site consults
the policy via EphemeralSession.current().
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ModelProfile
    from kinoforge.stores.base import ArtifactStore


@dataclass(frozen=True)
class EphemeralPolicy:
    """Per-session toggle of every persistent-write gate + provider-side actions."""

    # Persistent-write gates
    ledger_record: bool
    profile_cache_persist: bool
    batch_summary_write: bool
    cost_sidecar_write: bool
    heartbeat_ledger_touch: bool
    # Provider-side
    delete_on_completion: bool
    delete_retries: int
    # Identifiers
    memory_only_run_id: bool
    pod_name_includes_alias: bool
    # Logging
    force_debug_show_secrets_off: bool


DEFAULT_POLICY = EphemeralPolicy(
    ledger_record=True, profile_cache_persist=True, batch_summary_write=True,
    cost_sidecar_write=True, heartbeat_ledger_touch=True,
    delete_on_completion=False, delete_retries=0,
    memory_only_run_id=False, pod_name_includes_alias=True,
    force_debug_show_secrets_off=False,
)

STRICT_POLICY = EphemeralPolicy(
    ledger_record=False, profile_cache_persist=False, batch_summary_write=False,
    cost_sidecar_write=False, heartbeat_ledger_touch=False,
    delete_on_completion=True, delete_retries=3,
    memory_only_run_id=True, pod_name_includes_alias=False,
    force_debug_show_secrets_off=True,
)


EPHEMERAL_CAPABILITIES: dict[tuple[str, str | None], bool] = {
    ("comfyui",   "runpod"):   True,
    ("comfyui",   "local"):    True,
    ("comfyui",   "skypilot"): True,
    ("diffusers", "runpod"):   True,
    ("diffusers", "local"):    True,
    ("diffusers", "skypilot"): True,
    ("hosted",    None):       False,
    ("replicate", None):       True,
    ("runway",    None):       True,
    ("fal",       None):       False,
    ("luma",      None):       False,
}


class EphemeralSession:
    """Context manager activating the ephemeral policy.

    Use ``with EphemeralSession(enabled=...) as s:``. Inside the block,
    ``EphemeralSession.current()`` returns this session; outside, ``None``.
    """

    _active: contextvars.ContextVar["EphemeralSession | None"] = contextvars.ContextVar(
        "kinoforge_ephemeral_session", default=None
    )

    def __init__(self, *, enabled: bool) -> None:
        self.policy = STRICT_POLICY if enabled else DEFAULT_POLICY
        self.in_memory_ledger: dict[str, dict] = {}
        self.in_memory_profiles: dict[str, "ModelProfile"] = {}
        self._registered_stores: list[tuple["ArtifactStore", str]] = []
        self._token: contextvars.Token["EphemeralSession | None"] | None = None

    @classmethod
    def current(cls) -> "EphemeralSession | None":
        return cls._active.get()

    def register_store(self, store: "ArtifactStore", run_id: str) -> None:
        """Called by orchestrator at the top of generate()/batch_generate()."""
        self._registered_stores.append((store, run_id))

    def __enter__(self) -> "EphemeralSession":
        self._token = self._active.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Store cleanup wired in Task 15 (after EphemeralStoreCleanupFailedError exists).
        if self._token is not None:
            self._active.reset(self._token)
            self._token = None
```

- [ ] **Step 4: Remove guarded-import shims** in `lifecycle.py`, `profiles.py`, `batch.py`, and every engine `_save_fixture` site — replace with `from kinoforge.core.ephemeral import EphemeralSession` (no try/except).

- [ ] **Step 5: Add `EphemeralError` base class to `errors.py`.**

```python
class EphemeralError(KinoforgeError):
    """Base for ephemeral-mode failures."""
```

- [ ] **Step 6: Un-skip the `test_summary_skipped_in_ephemeral` test in `tests/core/test_batch_summary_skipped.py`.**

- [ ] **Step 7: Run, confirm PASS.** Full `pixi run test` should pass with this and the un-skipped test.

- [ ] **Step 8: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/ephemeral.py src/kinoforge/core/errors.py src/kinoforge/core/lifecycle.py src/kinoforge/core/profiles.py src/kinoforge/core/batch.py src/kinoforge/engines/ tests/core/test_ephemeral.py
git add src/kinoforge/core/ephemeral.py src/kinoforge/core/errors.py src/kinoforge/core/lifecycle.py src/kinoforge/core/profiles.py src/kinoforge/core/batch.py src/kinoforge/engines/ tests/core/test_ephemeral.py tests/core/test_batch_summary_skipped.py
git commit -m "feat(core): EphemeralSession + EphemeralPolicy + EPHEMERAL_CAPABILITIES

ContextVar-based session manager; STRICT/DEFAULT policies match spec §8.1.
EPHEMERAL_CAPABILITIES table covers comfyui/diffusers/replicate/runway/fal/
luma/hosted. Removes guarded ImportError shims from Tasks 8-13.

10/10 new tests pass; previously-skipped batch summary test un-skipped and
passes. Closes Sub-γ first half."
```

---

### Task 15: EphemeralSession.__exit__ store cleanup + pod naming

**Goal:** Wire the run-directory cleanup into `EphemeralSession.__exit__`; add `EphemeralStoreCleanupFailedError`; make pod naming consult `EphemeralSession.current()` for the alias-strip + `kinoforge-ephemeral=true` tag.

**Files:**
- Modify: `src/kinoforge/core/ephemeral.py` — `__exit__` calls `delete_run` per registered store; raises `EphemeralStoreCleanupFailedError` on failure.
- Modify: `src/kinoforge/core/errors.py` — add `EphemeralStoreCleanupFailedError(store, run_id, original_error)`.
- Modify: `src/kinoforge/core/orchestrator.py` — orchestrator calls `session.register_store(store, run_id)` at top of `generate()` / `batch_generate()` paths.
- Modify: `src/kinoforge/providers/runpod/__init__.py` — `RunPodProvider.create_instance` reads `EphemeralSession.current()` for pod-name and tag selection.
- Test: `tests/core/test_ephemeral_run_cleanup.py` — 5 tests.

**Acceptance Criteria:**
- [ ] `EphemeralSession.__exit__` calls `store.delete_run(run_id)` for every registered store, AFTER the with-block body (so OutputSink.publish has run).
- [ ] On any `delete_run` exception, `EphemeralStoreCleanupFailedError(store, run_id, original_error)` is raised with the store's `manual_cleanup_command(run_id)` available via `.cleanup_command` property.
- [ ] `EphemeralStoreCleanupFailedError.__str__` produces the exact spec §10.5 block: ERROR line, store, run_id, error, "To finish the scrub, run:", the cleanup command, exit-code note. No output-file enumeration (per D14).
- [ ] When `policy.delete_on_completion is False` (default mode), `__exit__` does NOT call `delete_run`.
- [ ] When the with-block raised an exception, store cleanup STILL runs (so a partial run doesn't leave a footprint).
- [ ] `RunPodProvider.create_instance` under ephemeral sets pod name to `kinoforge-<rand8>` (no alias linkage); adds tag `kinoforge-ephemeral=true`. Under default, name is `kinoforge-<alias>-<rand4>` with `capability=<alias>` tag.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/ephemeral.py src/kinoforge/core/errors.py src/kinoforge/core/orchestrator.py src/kinoforge/providers/runpod/__init__.py tests/core/test_ephemeral_run_cleanup.py` passes.

**Verify:** `pixi run pytest tests/core/test_ephemeral_run_cleanup.py tests/providers/test_runpod.py tests/core/test_orchestrator.py -v` → existing + 5 new pass.

**Steps:**

- [ ] **Step 1: Add `EphemeralStoreCleanupFailedError` to `errors.py`.**

```python
class EphemeralStoreCleanupFailedError(EphemeralError):
    def __init__(self, store, run_id: str, original_error: Exception) -> None:
        self.store = store
        self.run_id = run_id
        self.original_error = original_error
        self.cleanup_command = store.manual_cleanup_command(run_id)
        super().__init__(self._format())

    def _format(self) -> str:
        return (
            "ERROR: --ephemeral could not delete the run's on-disk artifacts.\n"
            f"  store:    {self.store!r}\n"
            f"  run_id:   {self.run_id}\n"
            f"  error:    {self.original_error}\n"
            "\n"
            "To finish the scrub, run:\n"
            "\n"
            f"  {self.cleanup_command}\n"
            "\n"
            "(kinoforge exited 1 because ephemeral requires a clean scrub.)"
        )
```

- [ ] **Step 2: Write `tests/core/test_ephemeral_run_cleanup.py` — 5 tests.**

```python
"""Tests for EphemeralSession.__exit__ store cleanup."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import EphemeralStoreCleanupFailedError
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.stores.local import LocalArtifactStore


def test_exit_calls_delete_run_for_every_registered_store(tmp_path: Path) -> None:
    s_a = LocalArtifactStore(root=tmp_path / "a")
    s_b = LocalArtifactStore(root=tmp_path / "b")
    s_a.put_json("r1", "x.json", {})
    s_b.put_json("r2", "y.json", {})
    with EphemeralSession(enabled=True) as sess:
        sess.register_store(s_a, "r1")
        sess.register_store(s_b, "r2")
    assert not (tmp_path / "a" / "r1").exists()
    assert not (tmp_path / "b" / "r2").exists()


def test_default_mode_does_not_cleanup(tmp_path: Path) -> None:
    s = LocalArtifactStore(root=tmp_path)
    s.put_json("r1", "x.json", {})
    with EphemeralSession(enabled=False) as sess:
        sess.register_store(s, "r1")
    assert (tmp_path / "r1").exists()


def test_cleanup_runs_even_after_exception(tmp_path: Path) -> None:
    """The with-block raising still triggers __exit__ — must scrub anyway."""
    s = LocalArtifactStore(root=tmp_path)
    s.put_json("r1", "x.json", {})
    with pytest.raises(RuntimeError, match="downstream-failure"):
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(s, "r1")
            raise RuntimeError("downstream-failure")
    assert not (tmp_path / "r1").exists()


def test_cleanup_failure_raises_with_command(tmp_path: Path) -> None:
    """delete_run raising → EphemeralStoreCleanupFailedError with cleanup_command."""
    store = MagicMock()
    store.manual_cleanup_command.return_value = 'rm -rf "/some/path"'
    store.delete_run.side_effect = PermissionError("denied")
    with pytest.raises(EphemeralStoreCleanupFailedError) as exc_info:
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(store, "r1")
    assert 'rm -rf "/some/path"' in str(exc_info.value)
    assert exc_info.value.cleanup_command == 'rm -rf "/some/path"'


def test_error_block_does_not_list_output_files(tmp_path: Path) -> None:
    """Per D14: error block has no preserved-file enumeration."""
    store = MagicMock()
    store.manual_cleanup_command.return_value = 'rm -rf "/x"'
    store.delete_run.side_effect = PermissionError("denied")
    try:
        with EphemeralSession(enabled=True) as sess:
            sess.register_store(store, "r1")
    except EphemeralStoreCleanupFailedError as e:
        msg = str(e)
        # No "files in your output directory are preserved:" prefix.
        assert "preserved" not in msg.lower()
        assert "output/" not in msg
```

- [ ] **Step 3: Edit `EphemeralSession.__exit__`.**

```python
def __exit__(self, exc_type, exc, tb) -> None:
    try:
        if self.policy.delete_on_completion:
            for store, run_id in self._registered_stores:
                try:
                    store.delete_run(run_id)
                except Exception as e:
                    from kinoforge.core.errors import EphemeralStoreCleanupFailedError
                    raise EphemeralStoreCleanupFailedError(store, run_id, e) from e
    finally:
        if self._token is not None:
            self._active.reset(self._token)
            self._token = None
```

- [ ] **Step 4: Wire `session.register_store` into orchestrator.**

Locate `orchestrator.generate()` and `batch_generate()`; immediately after the `with deploy_session(...)` opens (or wherever the store is bound), call:

```python
session = EphemeralSession.current()
if session is not None:
    session.register_store(store, run_id)
```

- [ ] **Step 5: Wire pod naming into `RunPodProvider.create_instance`.**

```python
# providers/runpod/__init__.py
from kinoforge.core.ephemeral import EphemeralSession

def create_instance(self, spec):
    session = EphemeralSession.current()
    alias = spec.tags.get("capability") if spec.tags else None
    if session is not None and not session.policy.pod_name_includes_alias:
        name = f"kinoforge-{secrets.token_hex(4)}"  # 8 hex chars
        tags = {"engine": spec.engine_kind, "kinoforge-ephemeral": "true"}
    else:
        name = f"kinoforge-{alias}-{secrets.token_hex(2)}" if alias else f"kinoforge-{secrets.token_hex(4)}"
        tags = {"engine": spec.engine_kind}
        if alias:
            tags["capability"] = alias
    # ... existing create call with name + tags ...
```

- [ ] **Step 6: Run, confirm PASS.**

- [ ] **Step 7: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/core/ephemeral.py src/kinoforge/core/errors.py src/kinoforge/core/orchestrator.py src/kinoforge/providers/runpod/__init__.py tests/core/test_ephemeral_run_cleanup.py
git add src/kinoforge/core/ephemeral.py src/kinoforge/core/errors.py src/kinoforge/core/orchestrator.py src/kinoforge/providers/runpod/__init__.py tests/core/test_ephemeral_run_cleanup.py
git commit -m "feat(core,providers): EphemeralSession __exit__ cleanup + pod naming

__exit__ calls store.delete_run for every registered store after the
with-block (so OutputSink.publish has already run). On failure, raises
EphemeralStoreCleanupFailedError with the manual cleanup command — no
output-file enumeration (per D14). RunPodProvider.create_instance reads
the session policy to decide pod name shape and tag set.

5/5 new tests pass; existing provider + orchestrator tests pass.
Closes Sub-γ."
```

---

### Task 16: `RemoteSubmitPollBackend._delete` ABC + base implementation + error classes

**Goal:** Add the abstract `_delete` method, the concrete `_delete_with_retries` base implementation with exponential backoff, and the three remaining error classes. `result()` becomes the single place that calls `_delete_with_retries` under ephemeral.

**Files:**
- Modify: `src/kinoforge/engines/hosted/remote_submit.py` (or wherever `RemoteSubmitPollBackend` is defined — find via `rg "class RemoteSubmitPollBackend" src/`).
- Modify: `src/kinoforge/core/errors.py` — add `EphemeralDeleteUnsupportedError`, `EphemeralDeleteHTTPError`, `EphemeralDeleteFailedError`.
- Test: `tests/engines/test_delete_with_retries.py` — 6 tests covering the base class behavior.

**Acceptance Criteria:**
- [ ] `RemoteSubmitPollBackend._delete(job_id)` is `@abstractmethod`.
- [ ] `RemoteSubmitPollBackend.manual_cleanup_url(job_id)` is `@classmethod` + `@abstractmethod`.
- [ ] `_delete_with_retries(job_id, retries=3)` calls `_delete`; on transient error retries with 1s/2s/4s backoff; on terminal failure raises `EphemeralDeleteFailedError`.
- [ ] `_delete_with_retries` is injectable-sleep (test passes a `sleep_fn` so retries are fast).
- [ ] `result(job_id)` calls `_delete_with_retries` iff `EphemeralSession.current() and EphemeralSession.current().policy.delete_on_completion`.
- [ ] `EphemeralDeleteFailedError.__str__` matches spec §10.5 hosted-delete block exactly — no output-file enumeration.
- [ ] `pixi run pre-commit run --files src/kinoforge/engines/hosted/remote_submit.py src/kinoforge/core/errors.py tests/engines/test_delete_with_retries.py` passes.

**Verify:** `pixi run pytest tests/engines/test_delete_with_retries.py -v` → 6 tests pass.

**Steps:**

- [ ] **Step 1: Add the three errors to `errors.py`.**

```python
class EphemeralDeleteUnsupportedError(EphemeralError):
    """Engine's provider has no public DELETE endpoint. Pre-flight should
    have caught this; raised at runtime as belt-and-suspenders."""


class EphemeralDeleteHTTPError(EphemeralError):
    """A single DELETE attempt returned non-2xx (and not 404)."""


class EphemeralDeleteFailedError(EphemeralError):
    def __init__(self, job_id: str, provider: str, manual_url: str, attempts: int, last_error: str) -> None:
        self.job_id = job_id
        self.provider = provider
        self.manual_url = manual_url
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(self._format())

    def _format(self) -> str:
        return (
            "ERROR: --ephemeral could not delete the provider-side record.\n"
            f"  provider: {self.provider}\n"
            f"  job_id:   {self.job_id}\n"
            f"  attempts: {self.attempts}\n"
            f"  last:     {self.last_error}\n"
            "\n"
            "To finish the scrub, run:\n"
            "\n"
            f"  curl -X DELETE {self.manual_url}\n"
            "\n"
            "(kinoforge exited 1 because ephemeral requires a clean scrub.)"
        )
```

- [ ] **Step 2: Write `tests/engines/test_delete_with_retries.py` — 6 tests.**

```python
"""Tests for RemoteSubmitPollBackend._delete_with_retries + result() integration."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import EphemeralDeleteFailedError, EphemeralDeleteHTTPError
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.engines.hosted.remote_submit import RemoteSubmitPollBackend


class _FakeBackend(RemoteSubmitPollBackend):
    """Test-only backend exposing _delete + manual_cleanup_url + result polling."""
    def __init__(self, *, delete_responses: list[Any]) -> None:
        self._responses = list(delete_responses)
        self._delete_calls: list[str] = []
        super().__init__()

    def _delete(self, job_id: str) -> None:
        self._delete_calls.append(job_id)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://example.com/jobs/{job_id}"

    def _poll_until_done(self, job_id: str) -> Any:
        from kinoforge.core.interfaces import Artifact
        return Artifact(url=f"https://e/{job_id}", filename=f"{job_id}.mp4", sha256=None, size=0, headers={})

    # ... other abstracts stubbed ...


def test_delete_success_no_retry() -> None:
    backend = _FakeBackend(delete_responses=[None])
    backend._delete_with_retries("job-1", retries=3, sleep_fn=lambda _: None)
    assert backend._delete_calls == ["job-1"]


def test_delete_transient_then_success() -> None:
    backend = _FakeBackend(delete_responses=[
        EphemeralDeleteHTTPError("503"), EphemeralDeleteHTTPError("503"), None,
    ])
    sleeps: list[float] = []
    backend._delete_with_retries("job-1", retries=3, sleep_fn=sleeps.append)
    assert len(backend._delete_calls) == 3
    assert sleeps == [1.0, 2.0]  # backoff before 2nd + 3rd attempt


def test_delete_terminal_failure_raises_with_manual_url() -> None:
    backend = _FakeBackend(delete_responses=[
        EphemeralDeleteHTTPError("503"), EphemeralDeleteHTTPError("503"), EphemeralDeleteHTTPError("503"),
    ])
    with pytest.raises(EphemeralDeleteFailedError) as exc:
        backend._delete_with_retries("job-1", retries=3, sleep_fn=lambda _: None)
    assert "https://example.com/jobs/job-1" in str(exc.value)
    assert exc.value.attempts == 3


def test_result_calls_delete_under_ephemeral() -> None:
    backend = _FakeBackend(delete_responses=[None])
    with EphemeralSession(enabled=True):
        backend.result("job-1")
    assert backend._delete_calls == ["job-1"]


def test_result_skips_delete_under_default_mode() -> None:
    backend = _FakeBackend(delete_responses=[])
    with EphemeralSession(enabled=False):
        backend.result("job-1")
    assert backend._delete_calls == []


def test_result_skips_delete_with_no_session() -> None:
    backend = _FakeBackend(delete_responses=[])
    backend.result("job-1")  # no with-block
    assert backend._delete_calls == []
```

- [ ] **Step 3: Run, confirm FAIL.**

- [ ] **Step 4: Edit `RemoteSubmitPollBackend`.**

```python
from abc import abstractmethod
import time

from kinoforge.core.errors import EphemeralDeleteFailedError, EphemeralDeleteHTTPError
from kinoforge.core.ephemeral import EphemeralSession


class RemoteSubmitPollBackend(GenerationBackend, ABC):
    # ... existing submit, _poll_status, _extract_url, etc. ...

    @abstractmethod
    def _delete(self, job_id: str) -> None:
        """Issue the provider's DELETE; raise EphemeralDeleteHTTPError on
        retryable failures; EphemeralDeleteUnsupportedError when no endpoint."""

    @classmethod
    @abstractmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Provider-specific cleanup URL for the error block."""

    def _delete_with_retries(
        self, job_id: str, *, retries: int = 3, sleep_fn=time.sleep,
    ) -> None:
        last_error = ""
        for attempt in range(retries):
            try:
                self._delete(job_id)
                return
            except EphemeralDeleteHTTPError as e:
                last_error = str(e)
                if attempt + 1 < retries:
                    sleep_fn(2 ** attempt)  # 1, 2, 4
        raise EphemeralDeleteFailedError(
            job_id=job_id,
            provider=self.__class__.__name__.replace("Backend", "").lower(),
            manual_url=self.manual_cleanup_url(job_id),
            attempts=retries,
            last_error=last_error,
        )

    def result(self, job_id: str):
        artifact = self._poll_until_done(job_id)
        session = EphemeralSession.current()
        if session is not None and session.policy.delete_on_completion:
            self._delete_with_retries(job_id, retries=session.policy.delete_retries)
        return artifact
```

- [ ] **Step 5: Run, confirm PASS.** 6 new tests pass; existing tests unaffected.

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/engines/hosted/remote_submit.py src/kinoforge/core/errors.py tests/engines/test_delete_with_retries.py
git add src/kinoforge/engines/hosted/remote_submit.py src/kinoforge/core/errors.py tests/engines/test_delete_with_retries.py
git commit -m "feat(engines): RemoteSubmitPollBackend _delete ABC + retries + error UX

_delete abstract; _delete_with_retries concrete (1s/2s/4s exponential backoff,
injectable sleep for tests). result() calls retries iff ephemeral session
active. EphemeralDeleteFailedError.__str__ matches spec §10.5 — no
output-file enumeration.

6/6 new tests pass."
```

---

### Task 17: Per-engine `_delete` + `manual_cleanup_url` implementations

**Goal:** Concrete `_delete` and `manual_cleanup_url` on `ReplicateBackend`, `RunwayBackend`, `FalBackend`. Fal raises `EphemeralDeleteUnsupportedError`.

**Files:**
- Modify: `src/kinoforge/engines/replicate/__init__.py`
- Modify: `src/kinoforge/engines/runway/__init__.py`
- Modify: `src/kinoforge/engines/fal/__init__.py`
- Test: `tests/engines/test_delete_on_completion.py` — 12 tests (4 per engine).
- Test: `tests/engines/test_fal_ephemeral_refused.py` — 2 tests.

**Acceptance Criteria:**
- [ ] `ReplicateBackend._delete(prediction_id)` sends `DELETE https://api.replicate.com/v1/predictions/{id}` with `Authorization: Bearer <token>`; 200/204/404 success; other non-2xx → `EphemeralDeleteHTTPError`.
- [ ] `ReplicateBackend.manual_cleanup_url(id)` returns `https://replicate.com/predictions/{id}`.
- [ ] `RunwayBackend._delete(task_id)` sends `DELETE https://api.dev.runwayml.com/v1/tasks/{id}`; same 200/204/404 success pattern.
- [ ] `RunwayBackend.manual_cleanup_url(id)` returns `https://app.runwayml.com/tasks/{id}`.
- [ ] `FalBackend._delete(request_id)` raises `EphemeralDeleteUnsupportedError("fal has no public DELETE endpoint")`.
- [ ] `FalBackend.manual_cleanup_url(id)` returns `""` (unreachable in practice — pre-flight refuses fal).
- [ ] All three backends use the existing injectable HTTP transport (Phase 43 pattern); tests inject fakes.
- [ ] `pixi run pre-commit run --files <every touched file>` passes.

**Verify:** `pixi run pytest tests/engines/test_delete_on_completion.py tests/engines/test_fal_ephemeral_refused.py -v` → 14 tests pass.

**Steps:**

- [ ] **Step 1: Write `tests/engines/test_delete_on_completion.py`** with 4 tests per engine following the existing engine-test pattern in `tests/engines/test_replicate.py`:

```python
"""Per-engine DELETE-on-completion tests for Replicate + Runway."""

from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import EphemeralDeleteHTTPError
from kinoforge.engines.replicate import ReplicateBackend
from kinoforge.engines.runway import RunwayBackend


def test_replicate_delete_sends_correct_request() -> None:
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=204)
    backend = ReplicateBackend(token="t-XXXX", http=http)  # adjust to actual ctor shape
    backend._delete("pred-abc")
    http.request.assert_called_once()
    call = http.request.call_args
    assert call.args[0] == "DELETE"
    assert call.args[1] == "https://api.replicate.com/v1/predictions/pred-abc"
    assert call.kwargs["headers"]["Authorization"] == "Bearer t-XXXX"


def test_replicate_delete_404_is_success() -> None:
    """404 = already gone, treated as success."""
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=404)
    backend = ReplicateBackend(token="t", http=http)
    backend._delete("pred-abc")  # no raise


def test_replicate_delete_503_raises_http_error() -> None:
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=503)
    backend = ReplicateBackend(token="t", http=http)
    with pytest.raises(EphemeralDeleteHTTPError, match="503"):
        backend._delete("pred-abc")


def test_replicate_manual_cleanup_url_shape() -> None:
    assert ReplicateBackend.manual_cleanup_url("pred-abc") == "https://replicate.com/predictions/pred-abc"


def test_runway_delete_sends_correct_request() -> None:
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=204)
    backend = RunwayBackend(token="t", http=http)
    backend._delete("task-xyz")
    call = http.request.call_args
    assert call.args[0] == "DELETE"
    assert call.args[1] == "https://api.dev.runwayml.com/v1/tasks/task-xyz"


def test_runway_delete_404_is_success() -> None:
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=404)
    backend = RunwayBackend(token="t", http=http)
    backend._delete("task-xyz")  # no raise


def test_runway_delete_500_raises() -> None:
    http = MagicMock()
    http.request.return_value = MagicMock(status_code=500)
    backend = RunwayBackend(token="t", http=http)
    with pytest.raises(EphemeralDeleteHTTPError):
        backend._delete("task-xyz")


def test_runway_manual_cleanup_url_shape() -> None:
    assert RunwayBackend.manual_cleanup_url("task-xyz") == "https://app.runwayml.com/tasks/task-xyz"


# 4 more tests covering retry orchestration + result-path integration per backend...
```

- [ ] **Step 2: Write `tests/engines/test_fal_ephemeral_refused.py`** with 2 tests:

```python
"""fal has no DELETE endpoint — _delete raises; manual URL is empty."""

import pytest

from kinoforge.core.errors import EphemeralDeleteUnsupportedError
from kinoforge.engines.fal import FalBackend


def test_fal_delete_raises_unsupported() -> None:
    backend = FalBackend()  # adjust ctor as needed
    with pytest.raises(EphemeralDeleteUnsupportedError, match="DELETE endpoint"):
        backend._delete("req-abc")


def test_fal_manual_url_empty() -> None:
    """No browser-facing dashboard URL; pre-flight refuses fal anyway."""
    assert FalBackend.manual_cleanup_url("req-abc") == ""
```

- [ ] **Step 3: Run, confirm FAIL.**

- [ ] **Step 4: Implement on each backend.**

```python
# replicate/__init__.py
from kinoforge.core.errors import EphemeralDeleteHTTPError

class ReplicateBackend(RemoteSubmitPollBackend):
    def _delete(self, prediction_id: str) -> None:
        url = f"https://api.replicate.com/v1/predictions/{prediction_id}"
        resp = self._http.request("DELETE", url, headers={"Authorization": f"Bearer {self._token}"})
        if resp.status_code not in (200, 204, 404):
            raise EphemeralDeleteHTTPError(f"replicate DELETE returned {resp.status_code}")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://replicate.com/predictions/{job_id}"
```

```python
# runway/__init__.py
class RunwayBackend(RemoteSubmitPollBackend):
    def _delete(self, task_id: str) -> None:
        url = f"https://api.dev.runwayml.com/v1/tasks/{task_id}"
        resp = self._http.request("DELETE", url, headers={"Authorization": f"Bearer {self._token}"})
        if resp.status_code not in (200, 204, 404):
            raise EphemeralDeleteHTTPError(f"runway DELETE returned {resp.status_code}")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return f"https://app.runwayml.com/tasks/{job_id}"
```

```python
# fal/__init__.py
from kinoforge.core.errors import EphemeralDeleteUnsupportedError

class FalBackend(RemoteSubmitPollBackend):
    def _delete(self, request_id: str) -> None:
        raise EphemeralDeleteUnsupportedError("fal has no public DELETE endpoint")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        return ""
```

- [ ] **Step 5: Run, confirm PASS.**

- [ ] **Step 6: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/engines/replicate/__init__.py src/kinoforge/engines/runway/__init__.py src/kinoforge/engines/fal/__init__.py tests/engines/test_delete_on_completion.py tests/engines/test_fal_ephemeral_refused.py
git add src/kinoforge/engines/replicate/__init__.py src/kinoforge/engines/runway/__init__.py src/kinoforge/engines/fal/__init__.py tests/engines/test_delete_on_completion.py tests/engines/test_fal_ephemeral_refused.py
git commit -m "feat(engines): per-engine _delete + manual_cleanup_url

Replicate: DELETE /v1/predictions/{id} with Bearer auth; 200/204/404 success.
Runway: DELETE /v1/tasks/{id}, same shape. Fal: raises
EphemeralDeleteUnsupportedError (pre-flight refuses fal regardless).

14/14 tests pass (8 hosted-engine tests + 2 fal-refusal tests + 4 retry
tests from Task 16). Closes Sub-δ."
```

---

### Task 18: CLI — `--vault` / `--ephemeral` / `--debug-show-secrets` + pre-flight gate + session wrap

**Goal:** Wire the three CLI flags through `cli.py`. Install `RedactingLogFilter` on root + `kinoforge` loggers. Load vault, register tokens. Pre-flight check against `EPHEMERAL_CAPABILITIES`. Wrap `_cmd_generate` / `_cmd_batch` / `_cmd_deploy` bodies in `with EphemeralSession(enabled=args.ephemeral):` and thread vault into `GenerationRequest`.

**Files:**
- Modify: `src/kinoforge/cli.py` — argparse extension, vault load, filter install, pre-flight, session wrap, vault → GenerationRequest threading.
- Test: `tests/cli/test_preflight_ephemeral.py` — 5 tests.
- Test: `tests/cli/test_flags_validation.py` — 3 tests.

**Acceptance Criteria:**
- [ ] `kinoforge generate --vault PATH ...` loads the vault and calls `register_vault_tokens` before any orchestration.
- [ ] `KINOFORGE_VAULT` env var fallback works when `--vault` omitted.
- [ ] `--ephemeral` + `--debug-show-secrets` together → CLI exits non-zero with clear error before any work.
- [ ] `--ephemeral` with `engine.kind=fal` → CLI exits non-zero at pre-flight with the spec §11.4 error block.
- [ ] `--ephemeral` with `engine.kind=replicate` → pre-flight passes.
- [ ] Vault under repo root → CLI exits non-zero with `VaultUnderRepoError` content.
- [ ] `RedactingLogFilter` is installed on `logging.getLogger("kinoforge")` AND `logging.getLogger()` (root, for third-party libs).
- [ ] When `--ephemeral` is set on read-mostly subcommands (`status`, `list`, `stop`, `destroy`, `reap`, `gc`, `forget`), a one-line stderr note is printed but no error: `note: --ephemeral has no effect on read-only subcommands`.
- [ ] When the vault is loaded, the orchestrator threads `vault.positive_prompt` / `vault.segments` into the `GenerationRequest`; the alias from `compute_profile_alias(config, vault)` is passed to the profile cache.
- [ ] `pixi run pre-commit run --files src/kinoforge/cli.py tests/cli/test_preflight_ephemeral.py tests/cli/test_flags_validation.py` passes.

**Verify:** `pixi run pytest tests/cli/test_preflight_ephemeral.py tests/cli/test_flags_validation.py tests/test_cli.py -v` → existing + 8 new pass.

**Steps:**

- [ ] **Step 1: Write `tests/cli/test_flags_validation.py` — 3 tests.**

```python
"""CLI flag exclusion + env-var fallback."""

import os
from pathlib import Path

import pytest

from kinoforge.cli import main


def test_ephemeral_and_debug_show_secrets_excluded(tmp_path: Path, capsys) -> None:
    """Both flags together → non-zero exit with clear error before any work."""
    with pytest.raises(SystemExit) as exc:
        main(["generate", "--config", "x.yaml", "--ephemeral", "--debug-show-secrets"])
    assert exc.value.code != 0
    out = capsys.readouterr().err
    assert "--debug-show-secrets" in out
    assert "--ephemeral" in out


def test_vault_env_var_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """KINOFORGE_VAULT env var used when --vault omitted."""
    vault = tmp_path / "vault.yaml"
    vault.write_text("positive_prompt: test\n")
    vault.chmod(0o600)
    monkeypatch.setenv("KINOFORGE_VAULT", str(vault))
    # Call CLI with --vault omitted; verify vault loaded.
    ...  # implementer: snapshot CLI state after main() entry to assert vault loaded


def test_vault_under_repo_rejected(tmp_path: Path) -> None:
    """A vault path resolving under the active repo → non-zero exit."""
    ...
```

- [ ] **Step 2: Write `tests/cli/test_preflight_ephemeral.py` — 5 tests.**

```python
"""Pre-flight EPHEMERAL_CAPABILITIES gate."""

import pytest

from kinoforge.cli import main


def test_ephemeral_fal_refused(tmp_path: Path, capsys) -> None:
    """--ephemeral + engine=fal → non-zero exit with spec §11.4 message."""
    # Build a minimal fal config under tmp_path.
    cfg = _write_fal_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["generate", "--config", str(cfg), "--ephemeral"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "fal" in err
    assert "DELETE" in err  # the spec error block names DELETE


def test_ephemeral_replicate_passes(tmp_path: Path) -> None:
    cfg = _write_replicate_config(tmp_path)
    # Should not raise at pre-flight (may still fail later for unrelated reasons).
    ...


def test_ephemeral_runway_passes(tmp_path: Path) -> None: ...


def test_ephemeral_comfyui_runpod_passes(tmp_path: Path) -> None: ...


def test_ephemeral_hosted_refused(tmp_path: Path, capsys) -> None:
    """Generic hosted (kind=hosted, no provider override) refused."""
    ...
```

- [ ] **Step 3: Edit `src/kinoforge/cli.py` — add flags + pre-flight + filter + session wrap.**

```python
# top of cli.py
import logging
import os
import sys
from pathlib import Path

from kinoforge.core.ephemeral import EPHEMERAL_CAPABILITIES, EphemeralSession
from kinoforge.core.redaction import RedactingLogFilter, RedactionRegistry
from kinoforge.core.vault import compute_profile_alias, load_vault, register_vault_tokens


def _install_redacting_filter(*, bypass: bool) -> None:
    registry = RedactionRegistry.instance()
    flt = RedactingLogFilter(registry, bypass=bypass)
    logging.getLogger("kinoforge").addFilter(flt)
    logging.getLogger().addFilter(flt)  # belt-and-suspenders for urllib3, runpod, etc.


def _add_privacy_flags(parser) -> None:
    parser.add_argument("--vault", type=Path, default=None,
                        help="Path to vault YAML outside the repo. Or set KINOFORGE_VAULT.")
    parser.add_argument("--ephemeral", action="store_true",
                        help="Strict mode: skip local writes, delete provider records, memory-only run id.")
    parser.add_argument("--debug-show-secrets", action="store_true",
                        help="Bypass logging redaction (forbidden under --ephemeral).")


def _validate_and_load_vault(args) -> "Vault | None":
    if args.debug_show_secrets and args.ephemeral:
        print("ERROR: --debug-show-secrets and --ephemeral are mutually exclusive.", file=sys.stderr)
        sys.exit(2)
    vault_path = args.vault or os.environ.get("KINOFORGE_VAULT")
    if vault_path is None:
        return None
    vault = load_vault(Path(vault_path))
    register_vault_tokens(vault)
    return vault


def _preflight_ephemeral(args, config) -> None:
    if not args.ephemeral:
        return
    engine_kind = config.engine.kind
    provider = config.compute.provider if config.compute else None
    cap = EPHEMERAL_CAPABILITIES.get((engine_kind, provider))
    if not cap:
        print(_preflight_error_block(engine_kind, provider), file=sys.stderr)
        sys.exit(2)


def _preflight_error_block(engine: str, provider: str | None) -> str:
    return (
        "ERROR: --ephemeral is not supported for this configuration.\n"
        f"  engine:    {engine}\n"
        f"  provider:  {provider or '(none — hosted API)'}\n"
        f"  reason:    {engine} has no public prediction-delete endpoint.\n"
        "\n"
        "  Use one of these instead:\n"
        "    engine: replicate     (DELETE /v1/predictions/{id})\n"
        "    engine: runway        (DELETE /v1/tasks/{id})\n"
        "    engine: comfyui       (any pod-based provider)\n"
        "    engine: diffusers     (any pod-based provider)\n"
        "\n"
        "  Or drop --ephemeral to allow provider-side record retention."
    )


def _cmd_generate(args, config) -> int:
    vault = _validate_and_load_vault(args)
    _install_redacting_filter(bypass=args.debug_show_secrets)
    _preflight_ephemeral(args, config)
    alias = compute_profile_alias(config, vault)
    with EphemeralSession(enabled=args.ephemeral):
        # ... existing generate body, threading vault.positive_prompt / segments / alias ...
        return 0


def _cmd_status(args, config) -> int:
    if args.ephemeral:
        print("note: --ephemeral has no effect on read-only subcommands", file=sys.stderr)
    # ... existing body ...
```

Mirror the same wrap for `_cmd_batch` and `_cmd_deploy`.

- [ ] **Step 4: Run, confirm PASS.** 8 new tests pass + existing CLI tests pass.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files src/kinoforge/cli.py tests/cli/test_preflight_ephemeral.py tests/cli/test_flags_validation.py
git add src/kinoforge/cli.py tests/cli/test_preflight_ephemeral.py tests/cli/test_flags_validation.py
git commit -m "feat(cli): --vault / --ephemeral / --debug-show-secrets + pre-flight gate

Three new flags. Vault load + register tokens at CLI entry; RedactingLogFilter
on root + kinoforge loggers. Pre-flight EPHEMERAL_CAPABILITIES check refuses
fal/luma/generic-hosted with the spec §11.4 block. Mutually-exclusive flag
validation. Vault path validated not-under-repo. Read-mostly subcommands
silently ignore --ephemeral with one-line stderr note. _cmd_generate / batch
/ deploy wrap their bodies in 'with EphemeralSession(...)'.

8/8 new CLI tests pass; existing CLI tests pass."
```

---

### Task 19: `tests/test_no_unredacted_writes.py` — CI invariant

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** AST-based test that scans `src/kinoforge/` for every persistent-write call and asserts each follows the canonical pattern (or carries the `# kinoforge:public-write` exemption tag). Catches every regression at merge time.

**Files:**
- Create: `tests/test_no_unredacted_writes.py` — 6 AC functions + helpers.

**Acceptance Criteria:**
- [ ] AC1: every `<store>.put_json(...)` call in `src/kinoforge/` is in a function whose body (above the call, in order) contains: (a) assignment with RHS `EphemeralSession.current()`, (b) `if session and not session.policy.<gate>:` branch, (c) assignment with RHS `RedactionRegistry.instance().redact_json(...)`. Exemption tag: `# kinoforge:public-write`.
- [ ] AC2: every `<store>.put_bytes(...)` call has second arg derived from `opaque_store_name(...)`. Exemption tag: `# kinoforge:public-name`.
- [ ] AC3: every `class *OutputSink` with a concrete `publish` method has a `RedactionRegistry.instance().add(<basename>, kind="output", ...)` call between file-write and return.
- [ ] AC4: every `_save_fixture` method contains `RedactionRegistry.instance().is_active` check before the file write.
- [ ] AC5: `src/kinoforge/cli.py` entry function installs `RedactingLogFilter` on root logger.
- [ ] AC6: every concrete `ArtifactStore` subclass implements both `delete_run` and `manual_cleanup_command`.
- [ ] AC7 (belt-and-suspenders): no `Path.write_bytes` / `Path.write_text` / `open(..., 'w'/'wb')` call outside an `ArtifactStore` or `OutputSink` subclass without exemption tag.
- [ ] Each AC failure message includes the offending `file:line`, which gate is missing, a pointer to the canonical reference (`core/lifecycle.py::Ledger.record`), and the exemption tag.
- [ ] The test runs in <2s on the current src tree.
- [ ] `pixi run pre-commit run --files tests/test_no_unredacted_writes.py` passes.

**Verify:** `pixi run pytest tests/test_no_unredacted_writes.py -v` → all 7 ACs pass. Adding a malformed `put_json` site to `src/kinoforge/core/lifecycle.py` (temp commit) → AC1 fails with a clear message; revert.

**Steps:**

- [ ] **Step 1: Write `tests/test_no_unredacted_writes.py` — 7 test functions.**

```python
"""CI invariant: every persistent-write site follows the canonical pattern.

Modeled on tests/test_core_invariant.py (Phase 9 Task 24). AST-based scan
of src/kinoforge/. Fails the build on any merge that bypasses the
RedactionRegistry + EphemeralSession pattern.

Exemption tags (line-level comments):
  # kinoforge:public-write   — opt out of AC1 / AC7 for a specific call
  # kinoforge:public-name    — opt out of AC2 for a specific put_bytes call
"""

from __future__ import annotations

import ast
import pathlib
import re

SRC = pathlib.Path(__file__).parent.parent / "src" / "kinoforge"
EXEMPT_WRITE = "# kinoforge:public-write"
EXEMPT_NAME = "# kinoforge:public-name"
REFERENCE = "see core/lifecycle.py::Ledger.record for the canonical shape"


def _all_py_files() -> list[pathlib.Path]:
    return sorted(SRC.rglob("*.py"))


def _line_text(source: str, lineno: int) -> str:
    return source.splitlines()[lineno - 1] if 1 <= lineno <= source.count("\n") + 1 else ""


def _is_call_to_method(node: ast.AST, method_name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == method_name


def _enclosing_func(tree: ast.AST, target: ast.Call) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    return node
    return None


def _body_above(func: ast.FunctionDef | ast.AsyncFunctionDef, target: ast.Call) -> list[ast.stmt]:
    """Statements in func.body that fully precede target (by lineno)."""
    above: list[ast.stmt] = []
    for stmt in func.body:
        if stmt.end_lineno is not None and stmt.end_lineno < target.lineno:
            above.append(stmt)
    return above


def _has_session_current_assign(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute):
                if (
                    isinstance(stmt.value.func.value, ast.Name)
                    and stmt.value.func.value.id == "EphemeralSession"
                    and stmt.value.func.attr == "current"
                ):
                    return True
    return False


def _has_policy_guard(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            if "policy" in ast.dump(stmt.test):
                return True
    return False


def _has_redact_json_call(stmts: list[ast.stmt]) -> bool:
    for stmt in stmts:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "redact_json":
                    return True
    return False


def test_ac1_put_json_canonical_pattern() -> None:
    """Every <store>.put_json(...) call must follow the canonical write pattern."""
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _is_call_to_method(node, "put_json"):
                line_text = _line_text(source, node.lineno)
                if EXEMPT_WRITE in line_text:
                    continue
                func = _enclosing_func(tree, node)
                if func is None:
                    violations.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_json outside a function")
                    continue
                stmts = _body_above(func, node)
                missing = []
                if not _has_session_current_assign(stmts):
                    missing.append("EphemeralSession.current() assign")
                if not _has_policy_guard(stmts):
                    missing.append("if session and not session.policy.<gate> guard")
                if not _has_redact_json_call(stmts):
                    missing.append("RedactionRegistry.instance().redact_json(...) call")
                if missing:
                    violations.append(
                        f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_json missing {missing}"
                    )
    assert not violations, (
        "Canonical write-site pattern violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\n{REFERENCE}\n"
        + f"Or add '{EXEMPT_WRITE}' on the put_json line for genuinely public writes."
    )


def test_ac2_put_bytes_uses_opaque_name() -> None:
    """Every <store>.put_bytes(run_id, name, ...) call uses opaque_store_name
    or carries the exemption tag."""
    violations: list[str] = []
    for path in _all_py_files():
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _is_call_to_method(node, "put_bytes") and len(node.args) >= 2:
                line_text = _line_text(source, node.lineno)
                if EXEMPT_NAME in line_text:
                    continue
                name_arg = node.args[1]
                refs_opaque = False
                if isinstance(name_arg, ast.Call) and isinstance(name_arg.func, ast.Name):
                    refs_opaque = name_arg.func.id == "opaque_store_name"
                elif isinstance(name_arg, ast.Name):
                    # Walk preceding assigns in the function to find name_arg.id
                    func = _enclosing_func(tree, node)
                    if func is not None:
                        for stmt in _body_above(func, node):
                            if isinstance(stmt, ast.Assign) and any(
                                isinstance(t, ast.Name) and t.id == name_arg.id for t in stmt.targets
                            ):
                                if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name):
                                    if stmt.value.func.id == "opaque_store_name":
                                        refs_opaque = True
                if not refs_opaque:
                    violations.append(
                        f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: put_bytes name not from opaque_store_name"
                    )
    assert not violations, (
        "put_bytes opaque-name violations:\n"
        + "\n".join(f"  {v}" for v in violations)
        + f"\n\nUse opaque_store_name(payload, ext) or add '{EXEMPT_NAME}' on the put_bytes line."
    )


def test_ac3_output_sink_registers_basename() -> None:
    """Every *OutputSink class with a concrete publish method must call
    RedactionRegistry.instance().add(<basename>, kind='output') before return."""
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("OutputSink"):
                publish = next(
                    (m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "publish"),
                    None,
                )
                if publish is None:
                    continue
                # ABC stub (no body / just docstring + ...) — skip
                if all(isinstance(s, (ast.Pass, ast.Expr, ast.Raise)) for s in publish.body):
                    continue
                if not _has_add_call_with_kind_output(publish):
                    violations.append(
                        f"{path.relative_to(SRC.parent.parent)}:{publish.lineno}: {node.name}.publish missing RedactionRegistry.add(..., kind='output')"
                    )
    assert not violations, "\n".join(violations)


def _has_add_call_with_kind_output(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add":
            for kw in node.keywords:
                if kw.arg == "kind" and isinstance(kw.value, ast.Constant) and kw.value.value == "output":
                    return True
    return False


def test_ac4_save_fixture_checks_registry() -> None:
    """Every _save_fixture method body contains a RedactionRegistry.is_active check."""
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_save_fixture":
                if "is_active" not in ast.unparse(node):
                    violations.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: _save_fixture missing is_active check")
    assert not violations, "\n".join(violations)


def test_ac5_cli_installs_log_filter() -> None:
    """src/kinoforge/cli.py contains a RedactingLogFilter install call."""
    cli = (SRC / "cli.py").read_text()
    assert "RedactingLogFilter" in cli, "cli.py does not import/install RedactingLogFilter"


def test_ac6_artifact_stores_implement_delete_run_and_manual_cleanup() -> None:
    """Every concrete ArtifactStore subclass defines delete_run + manual_cleanup_command."""
    violations: list[str] = []
    for path in _all_py_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("ArtifactStore") and node.name != "ArtifactStore":
                methods = {m.name for m in node.body if isinstance(m, ast.FunctionDef)}
                missing = {"delete_run", "manual_cleanup_command"} - methods
                if missing:
                    violations.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno}: {node.name} missing {missing}")
    assert not violations, "\n".join(violations)


def test_ac7_no_path_write_outside_store_and_sink() -> None:
    """Path.write_bytes / Path.write_text / open(..., 'w'/'wb') outside
    ArtifactStore / OutputSink subclass is a violation (unless tagged)."""
    violations: list[str] = []
    pattern = re.compile(r"(\.write_bytes\b|\.write_text\b|open\(.+?['\"][wba]+['\"])")
    for path in _all_py_files():
        source = path.read_text()
        for lineno, line in enumerate(source.splitlines(), 1):
            if pattern.search(line) and EXEMPT_WRITE not in line:
                # Skip if inside a class ending in ArtifactStore or OutputSink
                tree = ast.parse(source)
                inside_ok = False
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef) and (
                        node.name.endswith("ArtifactStore") or node.name.endswith("OutputSink")
                    ):
                        if node.lineno <= lineno <= (node.end_lineno or 0):
                            inside_ok = True
                            break
                if not inside_ok:
                    violations.append(f"{path.relative_to(SRC.parent.parent)}:{lineno}: write outside store/sink")
    assert not violations, "\n".join(violations)
```

- [ ] **Step 2: Run the test against the current tree. If any pre-existing site violates: fix it inline (each fix is a separate small commit before the test commit).** Expected: clean.

- [ ] **Step 3: Run all 7 ACs, confirm PASS.**

- [ ] **Step 4: Independently re-verify by deliberately introducing a violation, running the test, then reverting.**

```bash
# in a scratch worktree or temp commit:
# 1. Add a bogus `store.put_json("run", "x.json", {"prompt": "leak"})` line in src/kinoforge/core/lifecycle.py
# 2. pixi run pytest tests/test_no_unredacted_writes.py::test_ac1_put_json_canonical_pattern -v
#    → expect FAIL with the canonical-pattern violation message
# 3. revert the change
# 4. pixi run pytest tests/test_no_unredacted_writes.py -v
#    → expect PASS
```

Capture both outputs (FAIL + PASS) in the commit message body as proof.

- [ ] **Step 5: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/test_no_unredacted_writes.py
git add tests/test_no_unredacted_writes.py
git commit -m "test: tests/test_no_unredacted_writes.py — CI invariant gate

AST-based scan of src/kinoforge/ asserting:
  AC1: every put_json follows canonical pattern (EphemeralSession.current +
       policy guard + redact_json) — or carries # kinoforge:public-write
  AC2: every put_bytes name comes from opaque_store_name — or # kinoforge:public-name
  AC3: every *OutputSink.publish registers basename with kind='output'
  AC4: every _save_fixture checks RedactionRegistry.is_active
  AC5: cli.py installs RedactingLogFilter
  AC6: every concrete ArtifactStore implements delete_run + manual_cleanup_command
  AC7: no Path.write_bytes / write_text / open(...,'w') outside ArtifactStore/OutputSink

Independent re-verification: temporarily added a bogus put_json site,
confirmed AC1 failed with the canonical-pattern violation message
naming the file:line; reverted; full suite passes.

Closes Sub-ε. 7/7 ACs pass."
```

---

### Task 20: E2E integration tests

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Three end-to-end integration tests that prove the policy works at runtime, using `FakeEngine` + `LocalProvider` + `LocalArtifactStore` + `LocalOutputSink`. No real cloud, no real network.

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_ephemeral_only_output_dir_survives.py` — the main proof point.
- Create: `tests/integration/test_logging_filter_e2e.py` — captured stderr contains no prompt.
- Create: `tests/integration/test_output_filename_redacted_in_logs.py` — post-publish log lines redact basename.

**Acceptance Criteria:**
- [ ] `test_ephemeral_only_output_dir_survives.py`: vault + `--ephemeral` + full FakeEngine run → assert artifact store run dir is empty (or absent), state dir has no run-tagged sidecars, output dir contains the published file(s) with permissive names. No assertion on output-file count (deliberate per D14 — could be 1, 2, or N depending on stages).
- [ ] `test_logging_filter_e2e.py`: vault loaded → run orchestrator → capture stderr via `caplog` → assert no prompt body substring appears in any log record.
- [ ] `test_output_filename_redacted_in_logs.py`: publish a file → trigger a downstream log line that interpolates the path → assert the basename has been substituted with `<output:<hash6>>`; the output dir prefix path remains visible.
- [ ] All three tests are deterministic (no real network, no real GPU, no real model weights — `LocalProvider` + `FakeEngine`).
- [ ] Each test runs in <5s.
- [ ] `pixi run pre-commit run --files tests/integration/` passes.

**Verify:** `pixi run pytest tests/integration/ -v` → all 3 tests pass.

**Steps:**

- [ ] **Step 1: Create `tests/integration/__init__.py` (empty).**

- [ ] **Step 2: Write `test_ephemeral_only_output_dir_survives.py`.**

```python
"""E2E: after vault+--ephemeral run, only the user-configured output dir survives.

The artifact store run dir is empty (or absent); state dir has no run-tagged
sidecars; output dir contains the published file(s) with permissive names.
Per spec §14.2 Appendix C.
"""

from pathlib import Path

import pytest
import yaml


def test_ephemeral_only_output_dir_survives(tmp_path: Path) -> None:
    """Full FakeEngine + LocalProvider + LocalArtifactStore + LocalOutputSink run
    with vault + --ephemeral ends with only output-dir files on disk."""
    # Layout
    vault_path = tmp_path / "vault.yaml"
    vault_path.write_text(yaml.safe_dump({"positive_prompt": "the secret prompt body"}))
    vault_path.chmod(0o600)
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_minimal_fake_config_yaml(state_dir, output_dir))

    # Run CLI
    from kinoforge.cli import main
    exit_code = main([
        "generate",
        "--config", str(config_path),
        "--vault", str(vault_path),
        "--ephemeral",
    ])
    assert exit_code == 0

    # Output dir: at least one file present, all with permissive names.
    output_files = list(output_dir.glob("*"))
    assert len(output_files) >= 1
    # Permissive name shape (existing OutputSink schema): ts_provider_model_promptslug.ext
    for p in output_files:
        assert "_" in p.name
        assert p.is_file()

    # State dir: no run-tagged sidecars beyond pre-existing fixed files.
    state_artifacts = [p for p in state_dir.rglob("*") if p.is_file()]
    # No ledger.json for this run; no _batch_summary.json; no profile cache file.
    assert not any("ledger.json" in str(p) for p in state_artifacts)
    assert not any("_batch_summary.json" in str(p) for p in state_artifacts)


def _minimal_fake_config_yaml(state_dir: Path, output_dir: Path) -> str:
    """Adjust to actual Config schema. FakeEngine + LocalProvider."""
    return yaml.safe_dump({
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [{"ref": "fake:base", "kind": "base", "target": "diffusion_models"}],
        "compute": {"provider": "local", "image": "x"},
        "store": {"kind": "local", "root": str(state_dir)},
        "output": {"dir": str(output_dir)},
    })
```

- [ ] **Step 3: Write `test_logging_filter_e2e.py`.**

```python
"""E2E: with vault loaded, no log line contains the prompt body."""

from pathlib import Path

import pytest
import yaml


def test_no_log_record_contains_prompt_body(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    vault = tmp_path / "vault.yaml"
    vault.write_text(yaml.safe_dump({"positive_prompt": "MAGIC-CANARY-PROMPT"}))
    vault.chmod(0o600)
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    config = tmp_path / "config.yaml"
    config.write_text(_minimal_fake_config_yaml(state_dir, output_dir))

    from kinoforge.cli import main
    with caplog.at_level("DEBUG", logger="kinoforge"):
        main(["generate", "--config", str(config), "--vault", str(vault)])

    for record in caplog.records:
        msg = record.getMessage()
        assert "MAGIC-CANARY-PROMPT" not in msg, f"prompt leaked in log: {msg}"


def _minimal_fake_config_yaml(state_dir: Path, output_dir: Path) -> str:
    # ... same as Step 2 ...
    ...
```

- [ ] **Step 4: Write `test_output_filename_redacted_in_logs.py`.**

```python
"""E2E: after OutputSink.publish, basename is registered; subsequent log lines
that interpolate the path render with <output:<hash6>>."""

import logging
from pathlib import Path

import pytest

from kinoforge.core.redaction import RedactingLogFilter, RedactionRegistry
from kinoforge.stores.sinks import LocalOutputSink


def test_published_basename_redacted_in_subsequent_logs(tmp_path: Path) -> None:
    RedactionRegistry.instance().clear_session()
    flt = RedactingLogFilter(RedactionRegistry.instance())
    logger = logging.getLogger("kinoforge.test")
    logger.addFilter(flt)

    captured: list[str] = []
    class _Cap(logging.Handler):
        def emit(self, rec): captured.append(rec.getMessage())
    logger.addHandler(_Cap())
    logger.setLevel(logging.INFO)

    sink = LocalOutputSink(output_dir=tmp_path)
    path_str = sink.publish(b"video", meta={
        "timestamp": "20260608-1200", "provider": "fake", "model": "x",
        "prompt": "a cinematic shot of CANARY"})
    logger.info("wrote artifact to %s", path_str)

    out = captured[-1]
    assert "CANARY" not in out
    assert "<output:" in out
    # The output dir prefix path remains visible.
    assert str(tmp_path) in out

    RedactionRegistry.instance().clear_session()
```

- [ ] **Step 5: Run all 3 integration tests, confirm PASS.**

```
pixi run pytest tests/integration/ -v
```

- [ ] **Step 6: Independently re-verify by capturing test output + a deliberate-leak-then-revert cycle.**

For each of the three tests, deliberately introduce a bug that should make it fail (e.g., remove the `RedactionRegistry.instance().add(filename, kind="output")` call from `LocalOutputSink.publish`). Confirm the test fails with a clear assertion. Revert. Capture both outputs in the commit message body.

- [ ] **Step 7: Pre-commit + commit.**

```bash
pixi run pre-commit run --files tests/integration/
git add tests/integration/
git commit -m "test(integration): E2E proof points for vault + --ephemeral policy

test_ephemeral_only_output_dir_survives — vault+--ephemeral full FakeEngine
run leaves the output dir as the sole on-disk footprint. ArtifactStore
run dir gone, state dir clean, output dir present with permissive names.

test_logging_filter_e2e — captured stderr (caplog at DEBUG) contains no
substring of the registered prompt body. RedactingLogFilter on root logger
+ child kinoforge loggers catches every emission.

test_output_filename_redacted_in_logs — after OutputSink.publish, downstream
log lines interpolating the path render the basename as <output:<hash6>>;
the output-dir prefix remains visible (per D13).

Independent re-verification: each test was deliberately broken (one at
a time, reverted before commit) and the failure assertion confirmed.
Output captured in scratch notes.

3/3 integration tests pass."
```

---

### Task 21: Documentation + PROGRESS.md update

**Goal:** `examples/vault/example.yaml` template; `DESIGN.md` "Privacy boundary" section; `PROGRESS.md` phase entry with α–ε SHAs.

**Files:**
- Create: `examples/vault/example.yaml` — template with placeholder content.
- Modify: `DESIGN.md` — append "Privacy boundary" section.
- Modify: `PROGRESS.md` — new Phase 45 entry with task list + SHAs.

**Acceptance Criteria:**
- [ ] `examples/vault/example.yaml` contains every documented field (positive_prompt, negative_prompt, segments shown as commented-out alternative, loras with ref+label, alias commented out).
- [ ] `examples/vault/example.yaml` has a top comment block warning: "DO NOT use this file as-is. Copy outside the repo. Set chmod 600. Edit the placeholders."
- [ ] `DESIGN.md` gains a "Privacy boundary" section pointing at the spec + listing the 8 forward-compat contracts from spec §16.
- [ ] `PROGRESS.md` gets a "Phase 45 — Layer Privacy" entry with all 21 task SHAs (recorded at commit time during execution; placeholders in the plan).
- [ ] `pixi run pre-commit run --files examples/vault/example.yaml DESIGN.md PROGRESS.md` passes.

**Verify:** `git log --oneline | head -25` shows 21 commits matching the sub-merge sequence; `pixi run test` passes; `pixi run pytest tests/test_no_unredacted_writes.py -v` passes.

**Steps:**

- [ ] **Step 1: Write `examples/vault/example.yaml`.**

```yaml
# Example vault file for kinoforge — DO NOT use as-is.
#
# 1. Copy this file to a path OUTSIDE the repo (e.g. ~/.kinoforge/vault/foo.yaml).
# 2. chmod 600 so it's user-only readable.
# 3. Edit the placeholders below with your actual content.
# 4. Run kinoforge with --vault ~/.kinoforge/vault/foo.yaml (or set KINOFORGE_VAULT).
#
# Anything in this file is registered with the RedactionRegistry on load
# and will be substituted with placeholders in every log line, JSON write,
# and error traceback. See docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md.

positive_prompt: |
  REPLACE THIS with your actual prompt. Multi-line via YAML block scalar.
  Blank lines separate segments when fed through the HeuristicSplitter.

# Alternative to positive_prompt — exactly one of the two:
# segments:
#   - prompt: "first segment text"
#     params: { seed: 42 }
#   - prompt: "second segment text"
#     params: { seed: 43 }

negative_prompt: |
  REPLACE THIS with your negative prompt body, or remove the field entirely.

loras:
  - ref: civitai:0000@0000
    label: example-style
  - ref: hf:org/lora:foo.safetensors

# alias: my-stable-id    # optional explicit override; default is sha256-derived
```

- [ ] **Step 2: Edit `DESIGN.md` — append "Privacy boundary" section.**

```markdown
## Privacy boundary

Phase 45 (Layer Privacy) added an always-on content-confidentiality policy
plus a `--ephemeral` flag. See
`docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md`.

Forward-compat contracts (any layer that touches these areas MUST honor):

1. External sweeper treats `kinoforge-ephemeral=true` pod tag as
   alive-by-construction.
2. New `core/cost.py` (Layer 5 candidate) consults
   `EphemeralSession.current().policy.cost_sidecar_write`.
3. New `ArtifactStore` implementations MUST implement `delete_run` +
   `manual_cleanup_command`.
4. New `RemoteSubmitPollBackend` subclasses MUST implement `_delete` (or
   raise `EphemeralDeleteUnsupportedError`) AND register in
   `EPHEMERAL_CAPABILITIES`.
5. Future `hooks.post_generate` MUST receive paths via stdin or env var,
   never argv.
6. New `OutputSink` subclasses MUST call
   `RedactionRegistry.instance().add(basename, kind="output")` before
   `publish` returns.
7. New splitter adapters — no contract change.
8. New `_save_fixture` methods MUST check
   `RedactionRegistry.instance().is_active` and refuse when active.
```

- [ ] **Step 3: Edit `PROGRESS.md` — add Phase 45 entry under the post-MVP section** (executor backfills SHAs as commits land):

```markdown
### Phase 45 — Layer Privacy (vault + --ephemeral)

Spec: `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md` (`2396788`)
Plan: `docs/superpowers/plans/2026-06-08-ephemeral-workspaces.md`

Sub-α (foundation):
- [ ] Task 1: core/secret.py — commit `<SHA>`
- [ ] Task 2: RedactionRegistry — commit `<SHA>`
- [ ] Task 3: RedactingLogFilter — commit `<SHA>`
- [ ] Task 4: Vault loader + alias derivation — commit `<SHA>`
- [ ] Task 5: opaque_store_name — commit `<SHA>`

Sub-β (sink retrofit):
- [ ] Task 6: ArtifactStore.delete_run + manual_cleanup_command (Local) — commit `<SHA>`
- [ ] Task 7: S3 + GCS delete_run + manual_cleanup_command — commit `<SHA>`
- [ ] Task 8: Ledger canonical pattern — commit `<SHA>`
- [ ] Task 9: JsonProfileCache alias-keyed — commit `<SHA>`
- [ ] Task 10: batch_generate summary skipped — commit `<SHA>`
- [ ] Task 11: LocalOutputSink.publish registers basename — commit `<SHA>`
- [ ] Task 12: Downloader opaque_name path — commit `<SHA>`
- [ ] Task 13: GenerateClipStage opaque + _save_fixture refusal — commit `<SHA>`

Sub-γ (ephemeral):
- [ ] Task 14: EphemeralSession + EphemeralPolicy — commit `<SHA>`
- [ ] Task 15: __exit__ cleanup + pod naming — commit `<SHA>`

Sub-δ (hosted delete):
- [ ] Task 16: RemoteSubmitPollBackend._delete ABC — commit `<SHA>`
- [ ] Task 17: Per-engine impls (Replicate/Runway/Fal) — commit `<SHA>`

CLI:
- [ ] Task 18: --vault / --ephemeral / --debug-show-secrets + pre-flight — commit `<SHA>`

Sub-ε (CI invariant):
- [ ] Task 19: tests/test_no_unredacted_writes.py — commit `<SHA>`

Integration:
- [ ] Task 20: E2E proof points (USER-GATE) — commit `<SHA>`
- [ ] Task 21: examples/vault, DESIGN, PROGRESS — commit `<SHA>`
```

- [ ] **Step 4: Pre-commit + commit.**

```bash
pixi run pre-commit run --files examples/vault/example.yaml DESIGN.md PROGRESS.md
git add examples/vault/example.yaml DESIGN.md PROGRESS.md
git commit -m "docs(privacy): example vault, DESIGN.md, PROGRESS.md Phase 45 entry

examples/vault/example.yaml — template with safety preamble + all documented
fields shown. DESIGN.md gains 'Privacy boundary' section pointing at the
spec + listing 8 forward-compat contracts. PROGRESS.md Phase 45 entry with
the 21 task checklist; executor backfills SHAs as commits land.

Closes the prompt+LoRA confidentiality layer."
```

---

## Self-review

**1. Spec coverage check.** Each spec section maps to a task:

| Spec §  | Topic                                            | Task(s) |
|---------|--------------------------------------------------|---------|
| §6      | Vault format + load                              | 4 |
| §7      | RedactionRegistry + log filter                   | 2, 3 |
| §8      | EphemeralSession + policy + state matrix         | 14, 15 |
| §9      | Profile cache aliasing + LoRA cache              | 9, 12 |
| §10     | Hosted delete-on-completion + errors             | 16, 17 |
| §11     | CLI surface + pre-flight + flag validation       | 18 |
| §12.1-2 | Canonical pattern + every site                   | 8, 9, 10, 11, 13 |
| §12.3   | Output filename audit                            | 11 |
| §12.4   | delete_run + manual_cleanup_command              | 6, 7 |
| §12.5   | EphemeralStoreCleanupFailedError UX              | 15 |
| §13     | CI invariant 6 ACs + path-write scan             | 19 |
| §14     | Migration sub-merge sequence                     | (sequencing, all tasks) |
| §15     | Non-goals                                        | (no implementation) |
| §16     | Forward-compat contracts                         | 21 (DESIGN.md) |
| Appendix A | Full state matrix                             | 14 (EphemeralPolicy) + 15 (pod naming) |
| Appendix B | EPHEMERAL_CAPABILITIES                        | 14 |
| Appendix C | Acceptance criteria checklist                 | each task's ACs map back |
| D5  Profile cache opaque alias                       | 9 |
| D11 Vault both modes                                 | 4 |
| D12 Output dir sole exempt zone                      | 15, 20 |
| D13 Output filename surface registration             | 11 |
| D14 Error blocks list no preserved files             | 15, 16 |

No gaps. Sub-merge sequence in §14 matches the task ordering above.

**2. Placeholder scan.** No `TBD` / `TODO` / `implement later` / `add appropriate error handling` in the task bodies. Test helper bodies have `...` ellipsis where the implementer is expected to adapt to existing fixture shapes — these are deliberate, not placeholders, and the surrounding comments name the existing fixture to copy from.

**3. Type consistency.** Method names checked: `RedactionRegistry.{add, add_many, redact, redact_json, clear_session, is_active}` consistent across Tasks 2, 3, 4, 8, 9, 10, 11, 12, 13, 18, 19, 20. `EphemeralSession.{current, register_store, __enter__, __exit__}` + `.policy.{ledger_record, profile_cache_persist, batch_summary_write, cost_sidecar_write, heartbeat_ledger_touch, delete_on_completion, delete_retries, memory_only_run_id, pod_name_includes_alias, force_debug_show_secrets_off}` consistent across Tasks 8-20. `ArtifactStore.{delete_run, manual_cleanup_command}` consistent across Tasks 6, 7, 15, 16, 19. `opaque_store_name(payload, ext)` consistent across Tasks 5, 13, 19. No drift detected.

---

## Estimated effort

Per spec §14.4: ~600 LOC new + ~150 LOC touched + ~35 new tests + ~10 touched tests + 21 atomic commits across 5 sub-merges. Each task fits in <60 min of focused work for a skilled engineer with the spec in hand.

Expected post-merge test count: ~785 (current ~750 + 35 new).
