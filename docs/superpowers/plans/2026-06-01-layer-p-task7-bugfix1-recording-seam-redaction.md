# Layer P — Task 7 bug-fix #1 — `_RecordingHTTPSeam` redaction hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close PROGRESS:213 security finding by hardening `tests/providers/conftest_runpod.py::_redact` with a layered pipeline (shape detector + key-name walker + value-pattern matcher), a fail-closed runtime backstop, and a fixtures-audit lockdown test — so no live capture on `build/layer-p` can leak `RUNPOD_API_KEY`, `HF_TOKEN`, or any other recognised credential format into a committed fixture or test log.

**Architecture:** Three composable redaction passes piped together (`_redact_all = _redact_credential_patterns(_redact(_redact_kv_shape(x)))`). Each pass attacks a distinct leak class: shape detector catches GraphQL `{key, value}` env arrays (the PROGRESS:213 leak); existing key-name walker preserves Layer N behaviour; value-pattern matcher catches arbitrary credential strings buried in any field. A typed `CredentialLeakError` raised by `_RecordingHTTPSeam.flush()` fails closed when post-redaction payload still matches any pattern. A new `tests/providers/test_fixtures_audit.py` walks every committed `tests/**/*.json` and asserts pattern-cleanliness as a permanent lockdown.

**Tech Stack:** Python 3.13, stdlib only (`re`, `typing`, `collections.NamedTuple`, `logging`). No new pixi or pyproject changes. pytest + pytest-cov for the test layer (already pinned).

**Spec:** `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` (`edc8b3e`)

**Branch:** `build/layer-p` (off `main@7788f93`)

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `tests/providers/conftest_runpod.py` | Three new redaction passes + composition + audit primitives + `CredentialLeakError` + `_safe_log` + rewired `_RecordingHTTPSeam.flush()` + rewired dispatcher logger calls | Modify |
| `tests/providers/test_runpod_conftest.py` | 12 new unit tests covering each pass, composition, idempotence, sk- guard, backstop, safe_log, audit | Modify |
| `tests/providers/test_fixtures_audit.py` | One lockdown test walking `tests/**/*.json` through `_audit_for_leaks` | Create |
| `AGENTS.md` | Repo-root contributor guide on credential safety in tests | Create |
| `.env.example` | Add one-line header pointing contributors at the redactor + AGENTS.md | Modify |
| `README.md` | Add "Credential safety in tests" paragraph linking to `AGENTS.md` | Modify |
| `PROGRESS.md` | Closure snapshot block under Layer P Task 7 item #3 noting bug-catch #1 closed | Modify |

Each task below produces one self-contained commit.

---

## Task 1: Shape detector (`_redact_kv_shape`) + credential-name vocab helper

**Goal:** Add the first redaction pass that catches the exact PROGRESS:213 leak shape — `list[dict]` items with `key` + `value` keys where `key`'s value names a credential env var.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (add `_PROTECTED_NAME_SUFFIXES`, `_is_credential_name`, `_redact_kv_shape`)
- Modify: `tests/providers/test_runpod_conftest.py` (add 4 tests)

**Acceptance Criteria:**
- [ ] Input `{"variables": {"input": {"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL"}, {"key": "HF_TOKEN", "value": "hf_REAL"}, {"key": "PYTHONUNBUFFERED", "value": "1"}]}}}` → `env[0].value == "<REDACTED>"`, `env[1].value == "<REDACTED>"`, `env[2].value == "1"`.
- [ ] Extra-keys case: `{"key": "API_KEY", "value": "x", "comment": "y"}` inside list still redacts.
- [ ] Non-credential key name: `{"key": "IMAGE_NAME", "value": "alpine:latest"}` inside list passes through.
- [ ] Top-level `{"key": "x", "value": "y"}` NOT inside a list passes through unchanged (list-parent requirement).
- [ ] `_is_credential_name` returns True for `"RUNPOD_API_KEY"`, `"HF_TOKEN"`, `"FAL_KEY"`, `"AWS_SECRET_ACCESS_KEY"`, `"DB_PASSWORD"`, `"SSH_PASSPHRASE"`.
- [ ] `_is_credential_name` returns False for `"PYTHONUNBUFFERED"`, `"IMAGE_NAME"`, `"GPU_COUNT"`, `"keypoints"`, `"checkpoints"`.

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "kv_shape or credential_name"` → all 4 new tests PASS.

**Steps:**

- [ ] **Step 1: Write the four failing tests**

Add to `tests/providers/test_runpod_conftest.py` (after the existing `test_redact_*` tests):

```python
import pytest

from tests.providers.conftest_runpod import (
    _is_credential_name,
    _redact_kv_shape,
)


def test_redact_kv_shape_runpod_env_leak_regression() -> None:
    """PROGRESS:213 canonical RED: RunPod env array leaks the value side."""
    body = {
        "variables": {
            "input": {
                "env": [
                    {"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"},
                    {"key": "HF_TOKEN", "value": "hf_REAL12345"},
                    {"key": "PYTHONUNBUFFERED", "value": "1"},
                ]
            }
        }
    }
    out = _redact_kv_shape(body)
    env = out["variables"]["input"]["env"]
    assert env[0]["key"] == "RUNPOD_API_KEY"
    assert env[0]["value"] == "<REDACTED>"
    assert env[1]["key"] == "HF_TOKEN"
    assert env[1]["value"] == "<REDACTED>"
    assert env[2]["key"] == "PYTHONUNBUFFERED"
    assert env[2]["value"] == "1"


def test_redact_kv_shape_allows_extra_keys_in_item() -> None:
    """Item with `key` + `value` + extra fields still redacts."""
    body = {"env": [{"key": "API_KEY", "value": "secret", "comment": "main key"}]}
    out = _redact_kv_shape(body)
    assert out["env"][0]["value"] == "<REDACTED>"
    assert out["env"][0]["comment"] == "main key"


def test_redact_kv_shape_passes_non_credential_names() -> None:
    """Item whose `key` value is not a credential name is untouched."""
    body = {"env": [{"key": "IMAGE_NAME", "value": "alpine:latest"}]}
    out = _redact_kv_shape(body)
    assert out["env"][0]["value"] == "alpine:latest"


def test_redact_kv_shape_requires_list_parent() -> None:
    """A top-level {key, value} dict (not inside a list) is not redacted."""
    body = {"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"}
    out = _redact_kv_shape(body)
    assert out == body  # unchanged
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "kv_shape"`

Expected: `ImportError` or `ModuleAttributeError` on `_is_credential_name` / `_redact_kv_shape`. Tests fail to collect.

- [ ] **Step 3: Add credential-name vocab + helper to `tests/providers/conftest_runpod.py`**

Insert immediately after the existing `_PROTECTED_WORDS` block (around line 59):

```python
_PROTECTED_NAME_SUFFIXES: frozenset[str] = frozenset(
    {"_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSPHRASE"}
)
_PROTECTED_NAME_WHOLES: frozenset[str] = frozenset(
    {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE"}
)


def _is_credential_name(name: str) -> bool:
    """Return True if *name* looks like a credential env var.

    Match policy: uppercase the input, then return True when it equals one of
    the bare whole-word forms ({"KEY", "TOKEN", "SECRET", "PASSWORD",
    "PASSPHRASE"}) OR ends with one of the suffix forms ({"_KEY", "_TOKEN",
    "_SECRET", "_PASSWORD", "_PASSPHRASE"}).

    Args:
        name: A string that came from the `key` field of a GraphQL env-var
            list entry.

    Returns:
        True when *name* names a credential; False otherwise.  Substrings
        like ``keypoints`` or ``checkpoints`` return False.
    """
    upper = name.upper()
    if upper in _PROTECTED_NAME_WHOLES:
        return True
    return any(upper.endswith(suffix) for suffix in _PROTECTED_NAME_SUFFIXES)
```

- [ ] **Step 4: Add `_redact_kv_shape` pass**

Insert immediately after `_redact` (around line 100):

```python
def _redact_kv_shape(obj: Any) -> Any:
    """Recursively redact GraphQL ``[{"key": NAME, "value": VAL}, ...]`` env shapes.

    Pass 1 of the layered redactor.  Walks every list; for each item that is a
    dict with both ``key`` AND ``value`` keys, checks whether the ``key``
    field's STRING VALUE matches :func:`_is_credential_name`.  When it does,
    replaces the sibling ``value`` field with ``<REDACTED>``.  Recurses into
    all other containers normally.

    Args:
        obj: Any JSON-serialisable Python value.

    Returns:
        A redacted copy of *obj*.  Original is not mutated.
    """
    if isinstance(obj, list):
        out_list: list[Any] = []
        for item in obj:
            if (
                isinstance(item, dict)
                and "key" in item
                and "value" in item
                and isinstance(item["key"], str)
                and _is_credential_name(item["key"])
            ):
                redacted_item = dict(item)
                redacted_item["value"] = "<REDACTED>"
                # Recurse into other keys so nested credentials still get caught.
                for k, v in item.items():
                    if k != "value":
                        redacted_item[k] = _redact_kv_shape(v)
                out_list.append(redacted_item)
            else:
                out_list.append(_redact_kv_shape(item))
        return out_list
    if isinstance(obj, dict):
        return {k: _redact_kv_shape(v) for k, v in obj.items()}
    return obj
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "kv_shape or credential_name"`

Expected: 4 PASS.

- [ ] **Step 6: Run full conftest test file to verify no regressions**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v`

Expected: all existing tests still PASS + 4 new tests PASS.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): _redact_kv_shape pass for GraphQL env arrays

Pass 1 of the layered _RecordingHTTPSeam redactor.  Catches the exact
PROGRESS:213 leak shape: list items shaped {"key": NAME, "value": VAL}
where NAME ends in _KEY / _TOKEN / _SECRET / _PASSWORD / _PASSPHRASE (or
is one of those bare).  Sibling `value` field redacted to <REDACTED>;
non-credential names (IMAGE_NAME, PYTHONUNBUFFERED, etc) pass through.
Requires list parent — top-level {key, value} dicts ignored to avoid
clobbering ordinary dicts that happen to share the field names.

Layer P Task 7 bug-fix #1 — sub-plan Task 1.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Value-pattern matcher (`_redact_credential_patterns` + `_redact_string`)

**Goal:** Add the third redaction pass — a recursive value-side regex sweep that catches every credential prefix from spec §4.3 in arbitrary string values, with the sk- false-positive guard.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (add `_CREDENTIAL_PATTERNS`, `_redact_string`, `_redact_credential_patterns`)
- Modify: `tests/providers/test_runpod_conftest.py` (add 3 tests: parametrised credential formats, sk- guard, recursion)

**Acceptance Criteria:**
- [ ] `rpa_AB12cdEF34GhIj` inside any string → `<REDACTED>`.
- [ ] `hf_AbCdEf12345678` → `<REDACTED>`.
- [ ] `fal_key_xY7zPQ9ABCDEFGH` → `<REDACTED>`.
- [ ] `Bearer eyJhbGciOiJIUzI1NiJ9.foo` → `<REDACTED>` (full `Bearer X` substring).
- [ ] `sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` → `<REDACTED>`.
- [ ] `sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345` → `<REDACTED>`.
- [ ] `AKIAIOSFODNN7EXAMPLE` → `<REDACTED>`; same for `ASIAIOSFODNN7EXAMPLE`.
- [ ] Multi-line PEM block `-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----` → `<REDACTED>` (whole block).
- [ ] `ask-me about checkpoints` → unchanged (no word boundary before `sk`).
- [ ] `sk-only-4chars` → unchanged (content gate < 20 chars).
- [ ] Credential at `{"a": {"b": ["c", {"d": "rpa_REAL12345"}]}}` → caught and replaced.

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"` → all 3 (parametrised expands to 9+ cases) PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_runpod_conftest.py`:

```python
from tests.providers.conftest_runpod import (
    _redact_credential_patterns,
    _redact_string,
)


@pytest.mark.parametrize(
    ("label", "needle"),
    [
        ("rpa_token", "rpa_AB12cdEF34GhIj"),
        ("hf_token", "hf_AbCdEf12345678"),
        ("fal_key", "fal_key_xY7zPQ9ABCDEFGH"),
        ("bearer_auth", "Bearer eyJhbGciOiJIUzI1NiJ9.foo"),
        ("sk_openai", "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("sk_anthropic", "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
        ("aws_akia", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_asia", "ASIAIOSFODNN7EXAMPLE"),
        (
            "pem_private_key",
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\nXXXX\n-----END RSA PRIVATE KEY-----",
        ),
    ],
)
def test_credential_pattern_matcher_catches_each_format(label: str, needle: str) -> None:
    """Each documented credential prefix is replaced in arbitrary string values."""
    prose = f"prefix [{needle}] suffix"
    out = _redact_string(prose)
    assert needle not in out, f"{label}: needle {needle!r} survived in {out!r}"
    assert "<REDACTED>" in out


@pytest.mark.parametrize(
    "haystack",
    [
        "please ask-me about checkpoints, no sk-x here",  # no \b before sk
        "this is sk-only-4chars",  # content gate < 20
        "sk-",  # bare prefix
        "sk-aaa",  # 3-char content
    ],
)
def test_sk_pattern_guard_against_false_positives(haystack: str) -> None:
    """The sk- pattern requires \\b boundary + 20+ url-safe chars after prefix."""
    out = _redact_string(haystack)
    assert out == haystack, f"false positive: {haystack!r} → {out!r}"


def test_credential_pattern_matcher_recurses_into_nested_structure() -> None:
    """A credential string buried deep in a payload is still caught."""
    payload = {"a": {"b": ["c", {"d": "rpa_REAL_TOKEN_12345"}]}}
    out = _redact_credential_patterns(payload)
    assert out["a"]["b"][1]["d"] == "<REDACTED>"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"`

Expected: ImportError on `_redact_credential_patterns` / `_redact_string`. Tests fail to collect.

- [ ] **Step 3: Add `_CREDENTIAL_PATTERNS` table + `_redact_string` + `_redact_credential_patterns` to `tests/providers/conftest_runpod.py`**

Insert immediately after `_redact_kv_shape` (added in Task 1):

```python
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rpa_token", re.compile(r"\brpa_[A-Za-z0-9_\-]{8,}\b")),
    ("hf_token", re.compile(r"\bhf_[A-Za-z0-9_\-]{8,}\b")),
    ("fal_key", re.compile(r"\bfal_key_[A-Za-z0-9_\-]{8,}\b")),
    ("bearer_auth", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}")),
    ("sk_token", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
    ),
]


def _redact_string(s: str) -> str:
    """Apply every entry in :data:`_CREDENTIAL_PATTERNS` to *s* in declaration order.

    Each match is replaced with ``<REDACTED>``.  Multiple distinct patterns can
    fire within the same string (e.g. ``Bearer rpa_xxx`` triggers both
    ``bearer_auth`` and ``rpa_token`` — the first match wins for any given
    substring; later patterns operate on the partially-redacted output).

    Args:
        s: Input string.

    Returns:
        The string with every credential match replaced by ``<REDACTED>``.
    """
    out = s
    for _name, pattern in _CREDENTIAL_PATTERNS:
        out = pattern.sub("<REDACTED>", out)
    return out


def _redact_credential_patterns(obj: Any) -> Any:
    """Pass 3 — recursive value-side credential-pattern sweep.

    Walks every nested container.  For each string value, applies
    :func:`_redact_string`.  Non-string scalars pass through unchanged.

    Args:
        obj: Any JSON-serialisable Python value.

    Returns:
        A redacted copy of *obj*.  Original is not mutated.
    """
    if isinstance(obj, str):
        return _redact_string(obj)
    if isinstance(obj, dict):
        return {k: _redact_credential_patterns(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_credential_patterns(v) for v in obj]
    return obj
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "credential_pattern or sk_guard or pattern_recursion"`

Expected: parametrised credential-format test → 9 PASS; sk- guard parametrised → 4 PASS; recursion → 1 PASS. Total 14 new PASSes.

- [ ] **Step 5: Run full conftest test file**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v`

Expected: every test from Task 1 + new tests PASS.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): _redact_credential_patterns value-side sweep

Pass 3 of the layered _RecordingHTTPSeam redactor.  Recursive walk that
runs every string value through a credential-prefix regex table:
rpa_ (RunPod), hf_ (HuggingFace), fal_key_ (Layer M), Bearer auth,
sk- (OpenAI/Anthropic-old, guarded by \\b + 20-char content gate to
reject `ask-me` and short prose), AKIA/ASIA (AWS — forward-looking for
S3 stores per DESIGN §stores), and -----BEGIN PRIVATE KEY----- PEM
blocks (GCS service accounts — forward-looking).

Layer P Task 7 bug-fix #1 — sub-plan Task 2.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Composition `_redact_all` + key-name walker regression

**Goal:** Wire the three passes into a single `_redact_all` callable in fixed order (shape → key-name → pattern) + lock down idempotence and prove the existing key-name walker still works through the composition.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (add `_redact_all`)
- Modify: `tests/providers/test_runpod_conftest.py` (add 2 composition tests)

**Acceptance Criteria:**
- [ ] `_redact_all({"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"}]})["env"][0]["value"] == "<REDACTED>"`.
- [ ] `_redact_all(_redact_all(x)) == _redact_all(x)` for the same payload (idempotence).
- [ ] Existing `_redact` tests pass when re-run against `_redact_all` (key-name walker still covered).

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "redact_all or composition"` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_runpod_conftest.py`:

```python
from tests.providers.conftest_runpod import _redact_all


def test_redact_all_composition_handles_runpod_env_shape() -> None:
    """Composition catches the canonical PROGRESS:213 leak regardless of which pass fires."""
    body = {"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL12345"}]}
    out = _redact_all(body)
    assert out["env"][0]["value"] == "<REDACTED>"


def test_redact_all_composition_is_idempotent() -> None:
    """Running _redact_all twice yields the same payload as running it once."""
    body = {
        "data": {
            "token": "raw-secret",
            "env": [{"key": "HF_TOKEN", "value": "hf_REAL12345"}],
            "log": "container started with key=rpa_REAL12345 bearer=Bearer abcdefghij",
        }
    }
    once = _redact_all(body)
    twice = _redact_all(once)
    assert once == twice


def test_redact_all_preserves_existing_key_name_walker_behavior() -> None:
    """Layer N key-name walker tests still pass when run through the composition."""
    body = {
        "data": {
            "Token": "bearer-x",
            "pod": {"password": "pw", "imageName": "foo:bar"},
        },
        "Secret_Tail": "y",
    }
    out = _redact_all(body)
    assert out["data"]["Token"] == "<REDACTED>"
    assert out["data"]["pod"]["password"] == "<REDACTED>"
    assert out["data"]["pod"]["imageName"] == "foo:bar"
    assert out["Secret_Tail"] == "<REDACTED>"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "redact_all"`

Expected: ImportError on `_redact_all`.

- [ ] **Step 3: Add `_redact_all` to `tests/providers/conftest_runpod.py`**

Insert immediately after `_redact_credential_patterns`:

```python
def _redact_all(obj: Any) -> Any:
    """Run all three redaction passes in order.

    Pipeline: shape detector → key-name walker → value-pattern matcher.

    Order chosen so the shape detector replaces structurally-leaky values
    BEFORE the key-name walker scrubs the harmless ``key`` field name, and
    the pattern matcher runs last as a catch-all backstop for any credential
    string the first two passes did not recognise.

    Args:
        obj: Any JSON-serialisable Python value.

    Returns:
        A redacted copy of *obj*.  Original is not mutated.  Idempotent —
        ``_redact_all(_redact_all(x)) == _redact_all(x)``.
    """
    return _redact_credential_patterns(_redact(_redact_kv_shape(obj)))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "redact_all"`

Expected: 3 PASS.

- [ ] **Step 5: Run full conftest test file + full project test suite**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v`
Expected: all PASS.

Run: `pixi run pytest -q`
Expected: 862 → 862 + new tests from Tasks 1-3 PASS, no regressions.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): _redact_all composition pipeline

Compose the three layered redaction passes in fixed order: shape
detector → existing key-name walker → value-pattern matcher.  Idempotent
by construction.  Existing Layer N _redact behaviour preserved via the
key-name walker pass; composition tests lock that down.

Layer P Task 7 bug-fix #1 — sub-plan Task 3.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Audit infrastructure (`LeakHit` + `_audit_for_leaks` + `CredentialLeakError`)

**Goal:** Add the typed primitives the runtime backstop and the fixtures-audit lockdown both depend on. `_audit_for_leaks` walks a payload and returns every credential-pattern match as a `LeakHit(pattern_name, json_pointer, match_snippet)`. `CredentialLeakError` carries the hit list + the fixture filename for human-readable diagnosis.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (add `LeakHit`, `_audit_for_leaks`, `CredentialLeakError`)
- Modify: `tests/providers/test_runpod_conftest.py` (add 2 audit tests)

**Acceptance Criteria:**
- [ ] `_audit_for_leaks({"data": {"k": "rpa_REAL_TOKEN_12345"}})` returns one `LeakHit` with `pattern_name == "rpa_token"`, `json_pointer == "/data/k"`, `match_snippet` starting with `"rpa_"`.
- [ ] `_audit_for_leaks(<committed gpu_types.json payload>)` returns `[]`.
- [ ] Multiple leaks at different paths each produce one hit.
- [ ] `CredentialLeakError(hits, "create_pod.json").__str__()` formats as the spec §5.4 multi-line block.
- [ ] `CredentialLeakError` is a subclass of `Exception` (not `AssertionError`).
- [ ] `LeakHit` is a `NamedTuple` with attribute access (`.pattern_name`, `.json_pointer`, `.match_snippet`).

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "audit or leak_error"` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_runpod_conftest.py`:

```python
from tests.providers.conftest_runpod import (
    CredentialLeakError,
    LeakHit,
    _audit_for_leaks,
)


def test_audit_for_leaks_returns_empty_for_clean_payload() -> None:
    """A payload with no credential patterns produces no LeakHits."""
    payload = {"data": {"gpuTypes": [{"id": "g1", "memoryInGb": 24}]}}
    assert _audit_for_leaks(payload) == []


def test_audit_for_leaks_reports_pattern_name_and_pointer() -> None:
    """A buried credential produces a LeakHit with pattern_name + json pointer."""
    payload = {"data": {"deep": {"k": "rpa_REAL_TOKEN_12345"}}}
    hits = _audit_for_leaks(payload)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.pattern_name == "rpa_token"
    assert hit.json_pointer == "/data/deep/k"
    assert hit.match_snippet.startswith("rpa_")
    assert len(hit.match_snippet) <= 32


def test_audit_for_leaks_handles_list_indices_in_pointer() -> None:
    """List items produce numeric-index json pointers."""
    payload = {"env": [{"key": "X", "value": "hf_REAL_TOKEN_1234567"}]}
    hits = _audit_for_leaks(payload)
    assert len(hits) == 1
    assert hits[0].json_pointer == "/env/0/value"
    assert hits[0].pattern_name == "hf_token"


def test_credential_leak_error_is_exception_subclass() -> None:
    """CredentialLeakError signals infrastructure failure, not test assertion failure."""
    err = CredentialLeakError([], "x.json")
    assert isinstance(err, Exception)
    assert not isinstance(err, AssertionError)


def test_credential_leak_error_str_format() -> None:
    """__str__ lists each hit with pattern name + pointer + snippet."""
    hits = [
        LeakHit("rpa_token", "/response/env/0/value", "rpa_AB12cdEF34GhIj"),
        LeakHit("hf_token", "/response/env/3/value", "hf_xY7zPQ9ABCDEFGH"),
    ]
    err = CredentialLeakError(hits, "create_pod.json")
    text = str(err)
    assert "refusing to write" in text
    assert "create_pod.json" in text
    assert "rpa_token" in text
    assert "/response/env/0/value" in text
    assert "rpa_AB12cdEF34GhIj" in text
    assert "hf_token" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "audit or leak_error"`

Expected: ImportError on `LeakHit` / `_audit_for_leaks` / `CredentialLeakError`.

- [ ] **Step 3: Add the primitives to `tests/providers/conftest_runpod.py`**

Add imports at the top:

```python
from typing import Any, NamedTuple
```

Insert immediately after `_redact_all`:

```python
class LeakHit(NamedTuple):
    """A single credential-pattern hit produced by :func:`_audit_for_leaks`.

    Attributes:
        pattern_name: The canonical name from :data:`_CREDENTIAL_PATTERNS`
            (e.g. ``"rpa_token"``, ``"bearer_auth"``).
        json_pointer: RFC 6901 pointer to the offending location.
        match_snippet: First 32 chars of the matched substring.  Enough for
            shape diagnosis without re-emitting the full secret.
    """

    pattern_name: str
    json_pointer: str
    match_snippet: str


def _audit_for_leaks(obj: Any, _pointer: str = "") -> list[LeakHit]:
    """Walk *obj* and return every credential-pattern match.

    Recursive.  Visits every string value; runs every entry in
    :data:`_CREDENTIAL_PATTERNS` against it; collects every match as a
    :class:`LeakHit`.  Empty list means the payload is clean.

    Args:
        obj: A JSON-deserialised payload.
        _pointer: Internal — RFC 6901 pointer prefix for the current
            recursion frame.

    Returns:
        Every credential-pattern hit found, in walk order.
    """
    hits: list[LeakHit] = []
    if isinstance(obj, str):
        for name, pattern in _CREDENTIAL_PATTERNS:
            for match in pattern.finditer(obj):
                hits.append(
                    LeakHit(
                        pattern_name=name,
                        json_pointer=_pointer or "/",
                        match_snippet=match.group(0)[:32],
                    )
                )
        return hits
    if isinstance(obj, dict):
        for key, value in obj.items():
            sub = f"{_pointer}/{_escape_pointer_segment(str(key))}"
            hits.extend(_audit_for_leaks(value, sub))
        return hits
    if isinstance(obj, list):
        for idx, item in enumerate(obj):
            hits.extend(_audit_for_leaks(item, f"{_pointer}/{idx}"))
        return hits
    return hits


def _escape_pointer_segment(segment: str) -> str:
    """RFC 6901 pointer-segment escape: ``~`` → ``~0``, ``/`` → ``~1``."""
    return segment.replace("~", "~0").replace("/", "~1")


class CredentialLeakError(Exception):
    """Raised by :meth:`_RecordingHTTPSeam.flush` when post-redaction payload still leaks.

    Carries the offending fixture filename and the full hit list so the
    contributor can identify which redactor pass needs a new pattern or
    shape entry.  Subclasses ``Exception`` (not ``AssertionError``) so the
    test runner shows it as ERROR, signalling infrastructure failure rather
    than a behavioural assertion failure.

    Attributes:
        hits: Every :class:`LeakHit` returned by :func:`_audit_for_leaks`.
        filename: The fixture filename that would have leaked.
    """

    def __init__(self, hits: list[LeakHit], filename: str) -> None:
        self.hits = hits
        self.filename = filename
        super().__init__(self._format())

    def _format(self) -> str:
        lines = [f"refusing to write {self.filename}"]
        for hit in self.hits:
            lines.append(
                f"  - {hit.pattern_name} at {hit.json_pointer}: {hit.match_snippet!r}"
            )
        lines.append(
            "Update _CREDENTIAL_PATTERNS or _redact_kv_shape vocab to cover this shape."
        )
        return "\n".join(lines)

    def __str__(self) -> str:
        return self._format()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "audit or leak_error"`

Expected: 5 PASS.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): LeakHit + _audit_for_leaks + CredentialLeakError

Typed audit primitives for the layered _RecordingHTTPSeam redactor.
_audit_for_leaks walks any payload and returns LeakHit(pattern_name,
json_pointer, match_snippet) for every credential-pattern match — empty
list means clean.  CredentialLeakError subclasses Exception (not
AssertionError) so test-runner output flags infra failure rather than
behavioural failure; carries the hit list + offending fixture filename
for human-readable diagnosis.  Used in Task 5 by the flush() backstop
and in Task 7 by the cross-tree fixtures-audit lockdown.

Layer P Task 7 bug-fix #1 — sub-plan Task 4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Wire `_redact_all` + backstop into `_RecordingHTTPSeam.flush()`

**Goal:** Rewire the existing two `_redact(...)` call sites in `flush()` to use `_redact_all(...)`; add `_audit_for_leaks` backstop that raises `CredentialLeakError` BEFORE any fixture file is written.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (rewire `_RecordingHTTPSeam.flush()` body)
- Modify: `tests/providers/test_runpod_conftest.py` (add 1 backstop test)

**Acceptance Criteria:**
- [ ] `flush()` uses `_redact_all` (not `_redact`) at both call sites: request body redaction + response redaction.
- [ ] After building `payload`, `flush()` calls `_audit_for_leaks(payload)`. If non-empty, raises `CredentialLeakError(hits, filename)` BEFORE `write_text`.
- [ ] Existing `test_recording_seam_dispatches_to_named_files` still passes (returns/redacts unchanged for non-leaky payloads).
- [ ] New `test_flush_raises_credential_leak_error_when_redactor_gapped` reproduces a fake gap by monkeypatching `_redact_all` to identity, confirms `CredentialLeakError` raised with `pattern_name == "bearer_auth"`, no fixture file appears on disk.

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "flush or backstop or dispatch"` → existing dispatch tests + new backstop test PASS.

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/test_runpod_conftest.py`:

```python
def test_flush_raises_credential_leak_error_when_redactor_gapped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When _redact_all is bypassed but a leak is present, _audit_for_leaks catches it."""
    import tests.providers.conftest_runpod as conf

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"text": "Authorization: Bearer abcdef1234567890"}

    def fake_get(url: str) -> dict[str, Any]:
        return {}

    # Simulate a redactor gap: identity passthrough.
    monkeypatch.setattr(conf, "_redact_all", lambda x: x)

    seam = conf._RecordingHTTPSeam(fake_post, fake_get, out_dir=tmp_path)
    seam.http_post(
        "https://api.runpod.io/graphql",
        {"query": "mutation { podFindAndDeployOnDemand(input: $i) { id } }"},
    )

    with pytest.raises(conf.CredentialLeakError) as exc_info:
        seam.flush()

    assert exc_info.value.filename == "create_pod.json"
    assert any(h.pattern_name == "bearer_auth" for h in exc_info.value.hits)
    # No fixture should have been written.
    assert not (tmp_path / "create_pod.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "backstop"`

Expected: FAIL — `flush()` writes the fixture (no backstop yet).

- [ ] **Step 3: Rewire `_RecordingHTTPSeam.flush()` in `tests/providers/conftest_runpod.py`**

Edit the `flush()` body. Current lines 296–323 become:

```python
        self._out.mkdir(parents=True, exist_ok=True)
        for filename, url, request_body, response in self._records:
            # Redact both the request body and the response via the layered pipeline.
            redacted_body: dict[str, Any] | None = (
                _redact_all(request_body) if request_body is not None else None
            )
            # Build the _meta.request_query for backward-compat with Layer N
            # fixtures that carry the raw GraphQL query string.
            if request_body and isinstance(request_body.get("query"), str):
                raw_query = request_body["query"]
            elif "?query=" in url:
                raw_query = url.split("?query=", 1)[1]
            else:
                raw_query = url
            meta: dict[str, Any] = {
                "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "git_sha": git_sha,
                "operation": filename.removesuffix(".json"),
                "request_query": _redact_string(_redact_query_string(raw_query))[:200],
            }
            if redacted_body is not None:
                meta["request_body"] = redacted_body
            payload = {
                "_meta": meta,
                "response": _redact_all(response),
            }
            # Runtime backstop: refuse to write any fixture whose post-redaction
            # payload still matches a credential pattern.
            hits = _audit_for_leaks(payload)
            if hits:
                raise CredentialLeakError(hits, filename)
            (self._out / filename).write_text(
                json.dumps(payload, indent=2, sort_keys=False) + "\n",
            )
```

- [ ] **Step 4: Run backstop test + existing flush tests**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "backstop or dispatch or recording"`

Expected: backstop test PASS. Existing dispatcher / recording-seam tests still PASS (the apiKey field they exercise is caught by the key-name walker pass).

- [ ] **Step 5: Run full conftest tests**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v`

Expected: every test from Tasks 1–4 + new backstop test PASS.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): flush() backstop + _redact_all rewire

Rewire _RecordingHTTPSeam.flush() to call _redact_all for both the
request body and the response, and to run _audit_for_leaks on the
fully-built payload BEFORE writing the fixture.  Any post-redaction
credential-pattern match raises CredentialLeakError carrying the hit
list + the offending filename; no fixture file lands on disk.  Also
runs _redact_string over the meta.request_query field (after the
existing _redact_query_string) so non-query-string credentials in logs
can't survive either.  Fail-closed matches Layer N teardown precedent.

Layer P Task 7 bug-fix #1 — sub-plan Task 5.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `_safe_log` wrapper + dispatcher logger rewires

**Goal:** Wrap `_log.warning` calls in `_runpod_dispatch` and `_comfy_dispatch` with `_safe_log`, which applies `_redact_string` to every string arg before format substitution. Closes the drift gap where a credential could survive in a logger emission even after URL/body redaction.

**Files:**
- Modify: `tests/providers/conftest_runpod.py` (add `_safe_log`; rewire two `_log.warning(...)` call sites)
- Modify: `tests/providers/test_runpod_conftest.py` (add 1 safe-log test)

**Acceptance Criteria:**
- [ ] `_safe_log(_log, logging.WARNING, "container started, key=%s", "rpa_REAL12345")` → the emitted log record's `getMessage()` returns `"container started, key=<REDACTED>"` (the secret never reaches the log handler).
- [ ] `_runpod_dispatch` and `_comfy_dispatch` continue to emit a WARNING when handed an unknown URL/query (existing `test_recording_seam_dispatches_to_named_files` assertion that one warning record is logged must still pass).
- [ ] Non-string args pass through unchanged.

**Verify:** `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "safe_log or dispatch"` → all PASS.

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/providers/test_runpod_conftest.py`:

```python
def test_safe_log_redacts_string_args_before_format_substitution(
    caplog: Any,
) -> None:
    """A credential passed as a printf-style arg never reaches the log record."""
    import logging as _logging

    import tests.providers.conftest_runpod as conf

    logger = _logging.getLogger("kinoforge_test_safe_log")
    with caplog.at_level(_logging.WARNING, logger=logger.name):
        conf._safe_log(
            logger,
            _logging.WARNING,
            "container started, key=%s, count=%d",
            "rpa_REAL12345",
            42,
        )

    matches = [rec for rec in caplog.records if rec.name == logger.name]
    assert len(matches) == 1
    msg = matches[0].getMessage()
    assert "rpa_REAL12345" not in msg
    assert "<REDACTED>" in msg
    assert "count=42" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "safe_log"`

Expected: ImportError on `_safe_log`.

- [ ] **Step 3: Add `_safe_log` to `tests/providers/conftest_runpod.py`**

Insert immediately after `CredentialLeakError`:

```python
def _safe_log(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args: Any,
) -> None:
    """Emit a log record after running every string arg through :func:`_redact_string`.

    Non-string args pass through unchanged.  Use in place of
    ``logger.warning(msg, arg1, arg2)`` whenever a credential could appear in
    any of the substitution args.

    Args:
        logger: Standard :class:`logging.Logger` instance.
        level: Log level (e.g. ``logging.WARNING``).
        msg: Printf-style format string.  NOT redacted itself — format
            strings are author-controlled.
        *args: Substitution args.  Each str is redacted via
            :func:`_redact_string`; other types are forwarded as-is.
    """
    safe_args = tuple(_redact_string(a) if isinstance(a, str) else a for a in args)
    logger.log(level, msg, *safe_args)
```

- [ ] **Step 4: Rewire dispatcher logger calls**

In `_runpod_dispatch`, replace the `_log.warning(...)` call (around current line 179) with:

```python
    _safe_log(
        _log,
        logging.WARNING,
        "RecordingHTTPSeam: unrecognized GraphQL query, writing to "
        "unknown_%s.json (query fragment: %s)",
        sha,
        query[:80],
    )
```

In `_comfy_dispatch`, replace the `_log.warning(...)` call (around current line 218) with:

```python
    _safe_log(
        _log,
        logging.WARNING,
        "RecordingHTTPSeam: unrecognized ComfyUI URL, writing to "
        "unknown_%s.json (url: %s)",
        sha,
        url[:120],
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run pytest tests/providers/test_runpod_conftest.py -v -k "safe_log or dispatch"`

Expected: new safe_log test PASS + existing dispatch tests PASS.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git add tests/providers/conftest_runpod.py tests/providers/test_runpod_conftest.py
git commit -m "$(cat <<'EOF'
feat(test/conftest_runpod): _safe_log wrapper + dispatcher rewires

Wrap _log.warning calls in _runpod_dispatch + _comfy_dispatch with a
small _safe_log helper that runs _redact_string over every printf-style
string arg before format substitution.  Closes the drift gap where a
credential could survive in a logger emission even after URL/body
redaction (e.g. a log line embedding a `?api_key=rpa_xxx` query without
hitting the URL-redaction path).  Format strings themselves are author-
controlled and not redacted.

Layer P Task 7 bug-fix #1 — sub-plan Task 6.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Fixtures-audit lockdown test

**Goal:** Lock down the entire `tests/**/*.json` tree against future credential leaks by walking every committed JSON fixture through `_audit_for_leaks` and asserting the hit list is empty. Catches both PRE-EXISTING leaks (none expected per PROGRESS:213 attestation) and any FUTURE leak that the runtime backstop misses.

**Files:**
- Create: `tests/providers/test_fixtures_audit.py`

**Acceptance Criteria:**
- [ ] Test walks every `*.json` file under `tests/` (recursively).
- [ ] For each file, deserialises the JSON and runs `_audit_for_leaks`.
- [ ] Failure message lists each offending file path + every hit's pattern name + json pointer + match snippet.
- [ ] Test passes against the current `build/layer-p` tree (zero leaks).
- [ ] Test does NOT depend on conftest_runpod's `_load_fixture` (we want to scan ALL json fixtures, not only the `tests/providers/fixtures/runpod/` ones).

**Verify:** `pixi run pytest tests/providers/test_fixtures_audit.py -v` → PASS.

**Steps:**

- [ ] **Step 1: Create the failing test (RED only if the tree leaks; expected GREEN here)**

Create `tests/providers/test_fixtures_audit.py`:

```python
"""Lockdown: no committed *.json fixture under tests/ may contain a credential.

Pairs with the runtime _RecordingHTTPSeam backstop (which catches NEW leaks at
capture time) to catch PRE-EXISTING leaks and any future drift.  Pattern set
comes from tests/providers/conftest_runpod.py::_CREDENTIAL_PATTERNS.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.providers.conftest_runpod import LeakHit, _audit_for_leaks

_TESTS_ROOT: Path = Path(__file__).resolve().parents[1]


def _format_offenders(offenders: list[tuple[Path, list[LeakHit]]]) -> str:
    """Build a human-readable multi-line block describing every leak."""
    lines = [f"Found credential leaks in {len(offenders)} fixture file(s):"]
    for path, hits in offenders:
        rel = path.relative_to(_TESTS_ROOT.parent)
        lines.append(f"  {rel}:")
        for hit in hits:
            lines.append(
                f"    - {hit.pattern_name} at {hit.json_pointer}: "
                f"{hit.match_snippet!r}"
            )
    lines.append(
        "Either rotate the leaked credential AND scrub the fixture, or update "
        "the redactor to cover the shape and regenerate."
    )
    return "\n".join(lines)


def test_no_committed_fixture_contains_a_credential() -> None:
    """Every committed *.json under tests/ must pass _audit_for_leaks."""
    offenders: list[tuple[Path, list[LeakHit]]] = []
    for path in _TESTS_ROOT.rglob("*.json"):
        try:
            with path.open() as f:
                payload = json.load(f)
        except json.JSONDecodeError:
            # Non-JSON file accidentally suffixed .json — skip and let other
            # tests catch the malformation.
            continue
        hits = _audit_for_leaks(payload)
        if hits:
            offenders.append((path, hits))
    assert not offenders, _format_offenders(offenders)
```

- [ ] **Step 2: Run the test against the current tree**

Run: `pixi run pytest tests/providers/test_fixtures_audit.py -v`

Expected: PASS (tree is clean per PROGRESS:213 attestation).

If FAIL: the failure message lists every offending file. Either (a) the redactor is over-zealous on a benign string (fix the pattern; e.g. add a `\b` boundary), or (b) a real leak exists and must be rotated + scrubbed before this plan can ship. Do NOT commit until the audit is GREEN.

- [ ] **Step 3: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/providers/test_fixtures_audit.py
git add tests/providers/test_fixtures_audit.py
git commit -m "$(cat <<'EOF'
test(providers): cross-tree fixtures audit lockdown

Walks every committed *.json under tests/ and runs each payload through
_audit_for_leaks.  Pairs with the runtime _RecordingHTTPSeam backstop:
backstop catches NEW leaks at capture time; this test catches
PRE-EXISTING leaks and any future drift.  Current tree clean per
PROGRESS:213 attestation.

Layer P Task 7 bug-fix #1 — sub-plan Task 7.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Documentation + PROGRESS closure + full-gate verification

**Goal:** Land contributor-facing docs (AGENTS.md + README "Credential safety" section + .env.example header comment), close PROGRESS Layer P Task 7 bug-fix #1, and run the full project gate (`pixi run pytest`, `pixi run pre-commit run --all-files`, `pixi run typecheck`).

**Files:**
- Create: `AGENTS.md` (repo root)
- Modify: `.env.example` (prepend one-line redactor pointer)
- Modify: `README.md` (add "Credential safety in tests" subsection)
- Modify: `PROGRESS.md` (add closure snapshot under Layer P Task 7 item #3)

**Acceptance Criteria:**
- [ ] `AGENTS.md` exists at repo root with the four sections from spec §7.1.
- [ ] `.env.example` first non-comment line block references `AGENTS.md` and `tests/providers/conftest_runpod.py` redactor.
- [ ] `README.md` has a "Credential safety in tests" section linking to `AGENTS.md`.
- [ ] `PROGRESS.md` has a new dated closure block: "Layer P Task 7 bug-fix #1 — ✅ CLOSED <date> at HEAD `<sha>`" with sub-plan commit table, key design decisions, test count delta.
- [ ] `pixi run pytest -q` → 862 + new tests from Tasks 1–7 (target ~891) PASS + 1 skipped, no regressions.
- [ ] `pixi run pre-commit run --all-files` → clean.
- [ ] `pixi run typecheck` → clean (`CredentialLeakError` + `LeakHit` typed correctly).

**Verify:**
```
pixi run pytest -q
pixi run pre-commit run --all-files
pixi run typecheck
```
→ all three exit 0.

**Steps:**

- [ ] **Step 1: Create `AGENTS.md` at repo root**

```bash
ls AGENTS.md 2>&1  # must not exist
```

Create `AGENTS.md`:

```markdown
# AGENTS.md — Contributor guide

## Credential safety in tests

**Rule:** secrets enter kinoforge tests via `.env` only. Never wire a raw credential into any test
code, fixture file, example YAML, log message, or commit message.

### The redactor

`tests/providers/conftest_runpod.py` runs every captured `_RecordingHTTPSeam` payload through three
layered redaction passes before any fixture lands on disk or any logger emission goes out:

1. **Shape detector** (`_redact_kv_shape`) — catches GraphQL `[{"key": NAME, "value": VAL}]` env
   arrays where `NAME` ends in `_KEY` / `_TOKEN` / `_SECRET` / `_PASSWORD` / `_PASSPHRASE`.
2. **Key-name walker** (`_redact`) — Layer N behaviour, unchanged. Redacts values at any dict key
   whose name matches `{token, key, secret, password}`.
3. **Value-pattern matcher** (`_redact_credential_patterns`) — recursive sweep that catches:

   | Pattern | Example | Source |
   |---|---|---|
   | `rpa_token` | `rpa_xxxxxxxx...` | RunPod API key |
   | `hf_token` | `hf_xxxxxxxx...` | HuggingFace token |
   | `fal_key` | `fal_key_xxxxxxxx...` | fal.ai key |
   | `bearer_auth` | `Bearer eyJ...` | HTTP Authorization header |
   | `sk_token` | `sk-proj-...` / `sk-ant-api03-...` | OpenAI / Anthropic-old (guarded: ≥20 url-safe chars) |
   | `aws_access_key` | `AKIA....` / `ASIA....` | AWS access key ID |
   | `pem_private_key` | `-----BEGIN ... PRIVATE KEY-----...` | PEM blocks (e.g. GCS service accounts) |

A final runtime backstop (`_audit_for_leaks` inside `_RecordingHTTPSeam.flush()`) re-scans the
fully-built payload and raises `CredentialLeakError` (refusing to write the fixture) if any
pattern still matches.

### When you see `CredentialLeakError` at test time

It signals a **redactor gap, not a test failure**. The error message names the pattern + JSON
pointer + match snippet. Fix the redactor:

- New credential format → add a regex to `_CREDENTIAL_PATTERNS` in
  `tests/providers/conftest_runpod.py`, then add a parametrised unit test in
  `tests/providers/test_runpod_conftest.py`.
- New container shape → extend `_redact_kv_shape` or add a new pass.

Never catch the exception. Never edit the fixture by hand. Fix the redactor and let the audit
test in `tests/providers/test_fixtures_audit.py` confirm cleanliness across the whole tree.

### Adding a new credential pattern

1. Add the regex to `_CREDENTIAL_PATTERNS` in `tests/providers/conftest_runpod.py` (canonical
   snake_case name + compiled `re.Pattern`).
2. Add a parametrised case to the credential-format unit test in
   `tests/providers/test_runpod_conftest.py`.
3. Run `pixi run pytest tests/providers/ -v` and confirm both the unit test and the cross-tree
   `test_no_committed_fixture_contains_a_credential` audit still pass.
4. Cross-reference the new pattern in this section's table.

### Env vars used by live smokes

All four MUST live in `.env` (gitignored). See `.env.example` for the canonical list:

- `RUNPOD_API_KEY` / `RUNPOD_TERMINATE_KEY` — RunPod provider
- `HF_TOKEN` — HuggingFace weight downloads
- `FAL_KEY` — fal.ai hosted engine
- `CIVITAI_TOKEN` — CivitAI gated/private models

### Out of scope for the redactor

- Process env scrubbing — a test that prints `os.environ['RUNPOD_API_KEY']` to stdout bypasses
  the seam. Don't write that test.
- Git history scrub — the project has never committed a real credential per PROGRESS:213.
- Encryption-at-rest of fixtures — fixtures are public test data; redaction IS the protection.
```

- [ ] **Step 2: Prepend redactor pointer to `.env.example`**

Insert at the very top of `.env.example` (before the existing `# kinoforge credentials —` line):

```env
# Credential safety: tests redact these values automatically — see AGENTS.md
# "Credential safety in tests" + tests/providers/conftest_runpod.py.
# A `CredentialLeakError` at test time means the redactor needs a new pattern,
# not that your credential is wrong.
#
```

- [ ] **Step 3: Add "Credential safety in tests" section to `README.md`**

Locate the existing development / testing section in `README.md` (search for "test" or "pixi run pytest"). Append a new subsection:

```markdown
### Credential safety in tests

Secrets enter kinoforge tests via `.env` only — never via test code, fixtures, example YAML, or
commit messages. The `_RecordingHTTPSeam` in `tests/providers/conftest_runpod.py` runs a layered
redaction pipeline over every captured payload and refuses (via `CredentialLeakError`) to write a
fixture that still contains a credential pattern. See [`AGENTS.md`](AGENTS.md) for the contributor
guide, the pattern table, and the procedure for adding a new credential format.
```

(If the README has no obvious testing section, append the subsection under the existing project structure / development docs.)

- [ ] **Step 4: Add PROGRESS closure block**

In `PROGRESS.md`, locate the existing "Layer P Task 7 item #3 (workflow API JSON + first green
MP4) — ⛔ PARTIAL-CLOSE" block and append immediately after the "Bug-catch #1 — security finding"
paragraph (currently around line 213):

```markdown
**Layer P Task 7 bug-fix #1 (`_RecordingHTTPSeam` redaction hardening) — ✅ CLOSED <YYYY-MM-DD> at HEAD `<sha>`.**

Sub-spec + sub-plan + 7 task commits closed PROGRESS:213. `_redact` (key-name walker)
preserved verbatim; layered around it are `_redact_kv_shape` (GraphQL env-array shape detector)
and `_redact_credential_patterns` (value-side regex sweep covering rpa_, hf_, fal_key_, Bearer,
sk- guarded, AWS AKIA/ASIA, PEM blocks). `_RecordingHTTPSeam.flush()` runs `_audit_for_leaks` as
a runtime backstop and raises typed `CredentialLeakError` if anything still matches —
fail-closed, no fixture lands on disk. New `tests/providers/test_fixtures_audit.py` walks every
committed `tests/**/*.json` and asserts cleanliness as a permanent lockdown.

- Sub-spec: `docs/superpowers/specs/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction-design.md` (`edc8b3e`)
- Sub-plan: `docs/superpowers/plans/2026-06-01-layer-p-task7-bugfix1-recording-seam-redaction.md` (+ `.tasks.json`)
- T1 — `_redact_kv_shape` + credential-name vocab + 4 tests
- T2 — `_redact_credential_patterns` + `_redact_string` + 14 tests (parametrised)
- T3 — `_redact_all` composition + 3 idempotence/regression tests
- T4 — `LeakHit` + `_audit_for_leaks` + `CredentialLeakError` + 5 tests
- T5 — `flush()` backstop rewire + 1 test
- T6 — `_safe_log` wrapper + dispatcher rewires + 1 test
- T7 — `tests/providers/test_fixtures_audit.py` cross-tree lockdown
- T8 — AGENTS.md + .env.example header + README section + this closure block

Test count 862 → ~891 offline (final figure tracked at HEAD). typecheck/lint/pre-commit
all-files clean.

**Hard prerequisite for resuming any live capture on `build/layer-p`** — without this fix the
next smoke attempt would re-leak `RUNPOD_API_KEY` via the GraphQL `env[*].value` field.
```

(Replace `<YYYY-MM-DD>` with the local-timezone date at commit time per global feedback memory, and
`<sha>` with the actual final commit SHA from `git rev-parse --short HEAD` after the previous step.)

- [ ] **Step 5: Run full project gate**

```bash
pixi run pytest -q
pixi run pre-commit run --all-files
pixi run typecheck
```

Expected: all three exit 0. `pytest -q` shows 862 + delta from Tasks 1–7 (target ~891) passing,
1 skipped (live test).

If any gate fails, fix the issue and re-run. Do NOT commit until all three are clean.

- [ ] **Step 6: Capture final test count + commit SHA, edit PROGRESS placeholders**

After Step 5 is green:

```bash
git rev-parse --short HEAD  # current SHA on build/layer-p
pixi run pytest -q --co 2>&1 | tail -3  # collect-only test count
```

Edit `PROGRESS.md` and replace:
- `<YYYY-MM-DD>` with today's date in LOCAL timezone (e.g. `2026-06-01`)
- `<sha>` with the short SHA captured above
- `~891` with the actual final test count

- [ ] **Step 7: Pre-commit + final commit**

```bash
pixi run pre-commit run --files AGENTS.md .env.example README.md PROGRESS.md
git add AGENTS.md .env.example README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): Layer P Task 7 bug-fix #1 — closure snapshot

AGENTS.md contributor guide on credential safety in tests + redactor
overview + procedure for adding new patterns.  .env.example header
points at the redactor.  README adds a "Credential safety in tests"
subsection linking to AGENTS.md.  PROGRESS.md gets the closure block
under Layer P Task 7 item #3 noting bug-catch #1 closed; sub-plan
commit table, key design decisions, test count delta.

Closes PROGRESS:213.  Hard prerequisite for any further live capture on
build/layer-p satisfied; item #3 (workflow API JSON) remains blocked on
the separate ComfyUI-remote-provision sub-spec.

Layer P Task 7 bug-fix #1 — sub-plan Task 8.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Sub-plan completion checklist

- [ ] Tasks 1–8 all committed atomically on `build/layer-p`.
- [ ] `pixi run pytest -q` clean (target ~891 + 1 skipped).
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] `pixi run typecheck` clean.
- [ ] `PROGRESS.md` closure block edited with real date / SHA / test count.
- [ ] No merge to `main` yet — bug-fix #1 stays on `build/layer-p` until Layer P merges as a whole (after the separate ComfyUI-remote-provision sub-plan also lands).

## Rollback

Each task is one atomic commit. `git revert <sub-plan range>` restores pre-fix behaviour; test
count drops by exactly the new test delta; no other system impact.
