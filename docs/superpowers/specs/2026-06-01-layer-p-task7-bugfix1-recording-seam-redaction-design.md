# Layer P — Task 7 bug-fix #1 — `_RecordingHTTPSeam` credential redaction hardening

**Status:** APPROVED design — ready for plan
**Date:** 2026-06-01
**Branch:** `build/layer-p`
**Author:** Dr. Twinklebrane (via brainstorming session)
**Closes:** PROGRESS:213 (security finding — `_RecordingHTTPSeam` value-side leak)
**Blocks:** any further live capture on `build/layer-p` (a re-run at HEAD would re-leak)

## 1. Problem

`tests/providers/conftest_runpod.py::_redact` redacts dict values based on their **dict key NAME** matching a small protected vocab (`token`, `key`, `secret`, `password`). RunPod's GraphQL `podFindAndDeployOnDemand` mutation takes an env-var array shaped as

```json
"env": [
  {"key": "RUNPOD_API_KEY", "value": "rpa_REAL_SECRET_HERE"},
  {"key": "HF_TOKEN",       "value": "hf_REAL_SECRET_HERE"},
  {"key": "PYTHONUNBUFFERED", "value": "1"}
]
```

When the recorder serialises this for `tests/providers/fixtures/runpod/create_pod.json`, the walker
sees dict key `"key"` (matches protected vocab) and redacts its VALUE (the harmless env var NAME).
It then sees dict key `"value"` (does NOT match protected vocab) and passes its VALUE through —
which is the actual secret. **Exactly the wrong way around.**

Detected during Layer P Task 7 item #3 by reviewer scrutiny of an uncommitted fixture diff. Diff
was reverted via `git checkout HEAD --` before any commit landed; the key never reached git
history (PROGRESS:213). The redactor gap remains; a future smoke at HEAD would re-leak. **Hard
prerequisite for resuming any live capture work.**

## 2. Threat model + scope

**Asset under protection:** plaintext credential strings reaching disk via fixture files written
by `_RecordingHTTPSeam.flush()`, or stderr/CI logs via `_log.warning` calls in `_runpod_dispatch`
and `_comfy_dispatch`. Out of scope: in-process variables, network packets, env var inspection,
stdout from arbitrary test code.

**Adversary model:** honest-mistake. A future smoke test, provider plugin, or contributor that
wires a new credential format or new request/response shape into the seam without updating the
redactor list. Defaults must catch this.

**Entry surfaces covered:**

1. Request body (POST) — credentials in `variables.input.env[*].value`, custom headers, top-level body keys.
2. Response body — server-echoed env vars on `pod(input:)` reads.
3. URL query string — `?api_key=…` (already covered by Layer N `_redact_query_string`; not regressing).
4. Logger emissions — `_log.warning("query fragment: %s", query[:80])` and equivalents.
5. Already-written fixture files — historical leaks (PROGRESS:213 attests current tree is clean;
   lockdown test prevents recurrence).

**Trust boundary:** seam treats every captured field as untrusted. Pattern + shape + name walkers
run on input; backstop runs on output. Any escape from all three layers fails the test run via a
typed `CredentialLeakError`.

## 3. Approved decisions

| Q | Decision | Reason |
|---|---|---|
| Q1 | Layered: value-pattern matcher + `{key, value}` shape detector + existing key-name walker | Defense in depth; each pass catches a different failure class |
| Q2 | Patterns covered: `rpa_`, `hf_`, `fal_key_`, `Bearer`, `sk-` (guarded), `AKIA*`/`ASIA*`, PEM private key blocks | Confirmed in-use creds + forward-looking forms from DESIGN.md §stores roadmap |
| Q3 | Shape detector activates on `list[dict]` where item has `key` + `value` keys AND `key`'s value matches `*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_PASSPHRASE` suffix vocab | Catches the exact RunPod env shape and any future provider's env-array shape regardless of secret format |
| Q4 | Scope: code + tests + fixtures audit lockdown test + AGENTS.md + .env.example doc | Future contributors keep the redactor list in sync with new providers |
| Q5 | Runtime backstop: `CredentialLeakError` raised in `flush()` if post-redaction payload still matches any pattern; no fixture file lands on disk | Fail-closed, matches Layer N teardown precedent |
| Q6 | Logger emissions wrapped with `_safe_log` that pattern-redacts every str arg before format substitution | Closes drift gap where a non-query-string log site could leak |
| Q7 | Implementation organization: composed passes in `tests/providers/conftest_runpod.py` (no new module) | Cleanest test boundaries without premature module promotion |

## 4. Pattern catalog

Three composable passes run in fixed order: **shape detector → key-name walker (existing) →
value-pattern matcher**. Order chosen so shape detector replaces structurally-leaky values
BEFORE the key-name walker scrubs the harmless `key` field name, and the pattern matcher runs
LAST as the catch-all backstop.

### 4.1 Pass 1 — Shape detector (`_redact_kv_shape`)

Walks recursively. Detects lists where any item is a dict with `key` AND `value` keys present
(extra keys allowed). If that item's `key` field's STRING VALUE matches credential-name vocab
`*_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_PASSPHRASE` (case-insensitive, suffix or
whole-word match), replaces the sibling `value` field with `<REDACTED>`. Recurses into all
other containers normally.

Rationale: catches the exact PROGRESS:213 RunPod env shape and any future provider that emits a
credential env-array. Independent of secret format. Requires `list` parent so that ordinary
`{"key": "x", "value": "y"}` dicts at top level (not inside a list) are not ambiguously
clobbered.

### 4.2 Pass 2 — Key-name walker (`_redact`, existing, unchanged)

Today's behaviour preserved verbatim. Whole-segment match against `{token, key, secret, password}`
redacts the value. Existing tests pass unmodified. Kept because it correctly handles top-level
shapes like `{"apiKey": "x"}` that the shape detector deliberately ignores.

### 4.3 Pass 3 — Value-pattern matcher (`_redact_credential_patterns`)

Walks recursively. For each string value, applies the pattern table below in order; first match
replaces the matched substring with `<REDACTED>`. Multiple patterns can fire within a single
string (e.g. a log line containing both `Bearer rpa_xxx` and `hf_yyy`).

| `pattern_name` | Description | Regex | Source |
|---|---|---|---|
| `rpa_token` | RunPod API key | `\brpa_[A-Za-z0-9_\-]{8,}\b` | confirmed in-use (`RUNPOD_API_KEY`) |
| `hf_token` | HuggingFace token | `\bhf_[A-Za-z0-9_\-]{8,}\b` | confirmed in-use (Layer P #3 `HF_TOKEN`) |
| `fal_key` | fal.ai key | `\bfal_key_[A-Za-z0-9_\-]{8,}\b` | confirmed (Layer M `FAL_KEY`) |
| `bearer_auth` | HTTP Authorization Bearer | `Bearer\s+[A-Za-z0-9._\-]{8,}` | Authorization header |
| `sk_token` | OpenAI / Anthropic-old style | `\bsk-[A-Za-z0-9_\-]{20,}\b` | guarded: 20-char content gate eliminates `ask-`-style prose FPs |
| `aws_access_key` | AWS access key ID | `\b(AKIA\|ASIA)[0-9A-Z]{16}\b` | AWS-documented format; zero-FP; forward-looking (DESIGN §stores S3 roadmap) |
| `pem_private_key` | PEM private key block | `-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*?-----END [A-Z ]{0,40}PRIVATE KEY-----` | GCS service accounts (DESIGN §stores); multi-line block replaced wholesale |

The `pattern_name` column is the canonical string used as `LeakHit.pattern_name` and as the
first element of each `_CREDENTIAL_PATTERNS` tuple. Tests reference these strings verbatim.

All patterns compiled at module import. Replacement sentinel uniformly `<REDACTED>` to match
existing `_redact` behaviour and preserve Layer N fixture diffs.

### 4.4 Composition

```python
def _redact_all(obj: Any) -> Any:
    return _redact_credential_patterns(_redact(_redact_kv_shape(obj)))
```

All three passes are idempotent. `_redact_all(_redact_all(x)) == _redact_all(x)`.

## 5. Architecture

### 5.1 Module layout (`tests/providers/conftest_runpod.py`)

```
_PROTECTED_WORDS                          (existing — unchanged)
_PROTECTED_NAME_SUFFIXES                  (NEW — frozenset {"_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSPHRASE"})
_CREDENTIAL_PATTERNS: list[tuple[str, re.Pattern[str]]]
                                          (NEW — name + compiled regex, compiled at import)

_is_protected_key(name) -> bool           (existing — unchanged)
_is_credential_name(name) -> bool         (NEW — uppercased name endswith any _PROTECTED_NAME_SUFFIXES,
                                                 OR equals one of {"KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE"})

_redact(obj)                              (existing — key-name walker, unchanged)
_redact_kv_shape(obj)                     (NEW — list-of-{key,value} dicts pass)
_redact_credential_patterns(obj)          (NEW — recursive value-side regex sweep)
_redact_all(obj)                          (NEW — composition)
_redact_string(s) -> str                  (NEW — single-string pass; applies _CREDENTIAL_PATTERNS in order)

class LeakHit(NamedTuple):                (NEW — (pattern_name, json_pointer, match_snippet))
    pattern_name: str
    json_pointer: str
    match_snippet: str                    # match[:32]

_audit_for_leaks(obj) -> list[LeakHit]    (NEW — walks payload, returns matches; empty == clean)

class CredentialLeakError(Exception):     (NEW)
    hits: list[LeakHit]
    filename: str
    def __str__(self) -> str: ...         # multi-line format per §5.4

_safe_log(logger, level, msg, *args)      (NEW — applies _redact_string to each str arg, then logger.log(level, msg, *args))
```

### 5.2 Call-site rewires

- **`_RecordingHTTPSeam.flush()`** — swap `_redact(...)` → `_redact_all(...)` at the two current
  call sites:
  - `redacted_body = _redact(request_body)` (current line 299) → `_redact_all(request_body)`
  - `"response": _redact(response)` (current line 319) → `_redact_all(response)`
  After building `payload`, run `hits = _audit_for_leaks(payload)`; if non-empty,
  `raise CredentialLeakError(hits, filename)` BEFORE `write_text`. No fixture file lands on
  disk.
- **`_runpod_dispatch` `_log.warning(...)`** → **`_safe_log(_log, logging.WARNING, ...)`**. Wrapper
  applies `_redact_string` to every str positional arg before format substitution.
- **`_comfy_dispatch` `_log.warning(...)`** → same swap.
- **`_RecordingHTTPSeam.flush()` `meta.request_query`** — already runs `_redact_query_string`;
  additionally run `_redact_string` over the result so any credential-pattern leak beyond
  query-param shape is caught. Final form:
  `meta["request_query"] = _redact_string(_redact_query_string(raw_query))[:200]`

### 5.3 Data flow

```
real http_post / http_get
  → _RecordingHTTPSeam captures (filename, url, body, response)
  → flush():
      for each record:
        redacted_body     = _redact_all(body)            if body else None
        redacted_response = _redact_all(response)
        redacted_query    = _redact_string(_redact_query_string(raw_query))[:200]
        payload = {_meta: {..., request_query: redacted_query, request_body: redacted_body},
                   response: redacted_response}
        hits = _audit_for_leaks(payload)
        if hits:
            raise CredentialLeakError(hits, filename)    # backstop, fail-closed
        path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
```

### 5.4 `CredentialLeakError` format

```
CredentialLeakError: refusing to write tests/providers/fixtures/runpod/create_pod.json
  - rpa_token at /response/data/podFindAndDeployOnDemand/env/0/value: "rpa_AB12cdEF34GhIj..."
  - hf_token  at /response/data/podFindAndDeployOnDemand/env/3/value: "hf_xY7zPQ9ABCDEFGH..."
Update _CREDENTIAL_PATTERNS or _redact_kv_shape vocab to cover this shape.
```

`json_pointer` follows RFC 6901; `match_snippet` is `match.group(0)[:32]` to give the contributor
enough context to identify the leak shape without re-emitting the full secret.

### 5.5 Error typing

`CredentialLeakError` subclasses `Exception` (not `AssertionError`). Test-runner output shows it
as ERROR, not FAIL — signalling infrastructure issue rather than a behaviour assertion. Carries
typed `hits: list[LeakHit]` + `filename: str`. Re-raised through `flush()`; no swallowing.

### 5.6 No new dependencies

Pure stdlib: `re`, `typing`, `collections.NamedTuple`, `logging`. No external packages, no pixi
or pyproject changes.

## 6. Test plan

All new unit tests added to `tests/providers/test_runpod_conftest.py`. One new file
`tests/providers/test_fixtures_audit.py` for the cross-tree lockdown.

### 6.1 Unit tests (added to `test_runpod_conftest.py`)

1. **Shape detector — RunPod env shape (canonical RED for PROGRESS:213):**
   Input: `{"variables": {"input": {"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL"},
   {"key": "HF_TOKEN", "value": "hf_REAL"}, {"key": "PYTHONUNBUFFERED", "value": "1"}]}}}`.
   Assert: `env[0].value == "<REDACTED>"`, `env[1].value == "<REDACTED>"`, `env[2].value == "1"`
   (PYTHONUNBUFFERED preserved — not credential-name).

2. **Shape detector — extra keys allowed:**
   `{"key": "API_KEY", "value": "x", "comment": "y"}` inside list still redacts.

3. **Shape detector — non-credential key name:**
   `{"key": "IMAGE_NAME", "value": "alpine:latest"}` inside list passes through.

4. **Shape detector — top-level {key,value} dict not in list:**
   NOT triggered. Locks the list-parent requirement.

5. **Pattern matcher — parametrised per credential format:**
   ```python
   ("rpa_token_in_log",   "container started, RUNPOD_API_KEY=rpa_AB12cdEF34"),
   ("hf_token_bare",      "hf_AbCdEf12345678"),
   ("fal_key_bare",       "fal_key_xY7zPQ9ABCDEFGH"),
   ("bearer_header",      "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.foo"),
   ("sk_real_openai",     "sk-proj-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
   ("sk_real_anthropic",  "sk-ant-api03-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345"),
   ("aws_akia",           "AKIAIOSFODNN7EXAMPLE"),
   ("aws_asia",           "ASIAIOSFODNN7EXAMPLE"),
   ("pem_private_key",    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"),
   ```
   Assert each redacts in-place; non-matching prose around the credential is preserved.

6. **Pattern matcher — `sk-` false-positive guard:**
   - `"please ask-me about checkpoints, no sk-x here"` → NOT redacted.
   - `"this is sk-only-4chars"` → NOT redacted (content too short).
   - `"sk-proj-" + "A"*20` → IS redacted.

7. **Pattern matcher — recursion:**
   Credential buried in `{"a": {"b": ["c", {"d": "rpa_REAL12345"}]}}` is caught.

8. **Composition order:**
   `{"env": [{"key": "RUNPOD_API_KEY", "value": "rpa_REAL"}]}` — output identical whether shape
   detector or pattern matcher fires first. Locks idempotence.

9. **Composition — key-name walker preserved:**
   Existing `test_redact_replaces_secret_field_names` /
   `test_redact_is_case_insensitive_and_recursive` /
   `test_redact_does_not_match_partial_word_collisions` re-run against `_redact_all`. All pass.

10. **`_safe_log` wrapper:**
    Use `caplog`. Emit
    `_safe_log(_log, logging.WARNING, "container started, key=%s", "rpa_REAL12345")`.
    Assert log record `getMessage()` contains `<REDACTED>` and not `rpa_REAL`.

11. **`CredentialLeakError` raised by `flush()` backstop:**
    Construct a `_RecordingHTTPSeam` with a fake response containing `Bearer rpa_REAL12345` in a
    non-key-named, non-env-shaped field (e.g. `{"text": "..."}`). Monkeypatch the redactor pipeline
    to a no-op via `monkeypatch.setattr("tests.providers.conftest_runpod._redact_all", lambda x:
    x)` (simulates a "redactor gap" while leaving `_audit_for_leaks` active). Call `flush()`.
    Assert: `CredentialLeakError` raised; no file exists at `tmp_path / "create_pod.json"`;
    `exc.hits[0].pattern_name == "bearer_auth"`; `exc.filename == "create_pod.json"`.

12. **`_audit_for_leaks` clean payload returns empty list:**
    Run against today's `tests/providers/fixtures/runpod/gpu_types.json`. Assert `[]`.

### 6.2 Fixtures audit lockdown (`tests/providers/test_fixtures_audit.py`, NEW)

```python
def test_no_committed_fixture_contains_a_credential() -> None:
    """Lockdown: every committed *.json under tests/ must pass _audit_for_leaks."""
    tests_root = Path(__file__).resolve().parents[1]
    offenders: list[tuple[Path, list[LeakHit]]] = []
    for path in tests_root.rglob("*.json"):
        with path.open() as f:
            payload = json.load(f)
        hits = _audit_for_leaks(payload)
        if hits:
            offenders.append((path, hits))
    assert not offenders, _format_offenders(offenders)
```

Walks all committed `tests/**/*.json`. Covers `tests/providers/fixtures/runpod/`,
`tests/engines/fixtures/`, and any future fixture directory. Fails with json-pointer + pattern
name if any leak ever lands. Pairs with the runtime backstop: backstop catches NEW leaks at
capture time; audit catches PRE-EXISTING leaks and any drift.

### 6.3 Coverage gate

New code paths covered ≥95%. Spec self-review verifies.

### 6.4 Test count projection

≈13 new tests (12 unit + 1 audit). 862 → ~875 offline. Final count tracked at plan-write time.

## 7. Documentation deliverables

### 7.1 `AGENTS.md` (NEW at repo root)

Sections:
- "Credential safety in tests" — high-level rule: secrets enter via `.env` only; never wire a raw
  credential into any test code, fixture, or example YAML.
- Pointer to `tests/providers/conftest_runpod.py` redaction helpers.
- Table of patterns currently covered (lifted from §4.3).
- Procedure for adding a new pattern: regex + parametrised unit test + audit reruns clean.
- Reminder: `CredentialLeakError` at test time = redactor gap, not a behaviour bug — fix the
  redactor, do not catch the exception.
- Pointer to env vars used by live smokes that MUST stay in `.env`.

### 7.2 `.env.example` (CREATE or UPDATE)

```env
# RunPod (live smoke + RunPod provider tests)
RUNPOD_API_KEY=rpa_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# Reused as terminate key — see PROGRESS Layer N bug catch #10
RUNPOD_TERMINATE_KEY=${RUNPOD_API_KEY}

# HuggingFace (required for Wan weight download in ComfyUI live smoke)
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# fal.ai (Layer M live tests)
FAL_KEY=fal_key_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Plus a one-line README pointer.

### 7.3 README

One-paragraph "Credential safety in tests" section under existing dev/test docs. Links to
`AGENTS.md`.

### 7.4 PROGRESS.md

Closure snapshot block under Layer P Task 7 item #3 noting bug-catch #1 closed at HEAD.

## 8. Out of scope

- **Process env scrubbing** — redactor only protects fixture writes and log emissions. A test
  that prints `os.environ['RUNPOD_API_KEY']` to stdout bypasses the seam entirely. AGENTS note
  flags this.
- **Network-level credential interception** — packet scrubbing is not the seam's concern.
- **CivitAI / SkyPilot / RunwayML / Pika specific patterns** — none ship yet. Shape detector +
  `*_KEY` / `*_TOKEN` suffix vocab covers env-array shapes without per-provider regex. Add
  concrete patterns when those providers land.
- **Git history scrub** — PROGRESS:213 attests current history is clean; no historical scrub
  needed.
- **Encryption-at-rest of fixtures** — fixtures are public test data; redaction IS the
  protection.
- **Automated rotation of leaked secrets** — operator responsibility.
- **ComfyUI remote provisioning** — Priority 0 blocker remains; separate brainstorm + sub-spec.

## 9. Acceptance criteria

1. `pixi run pytest tests/providers/test_runpod_conftest.py tests/providers/test_fixtures_audit.py -v` → all pass.
2. `pixi run pytest` full suite → ~875 passing + 1 skipped, no regressions on existing 862.
3. `pixi run pre-commit run --all-files` → clean.
4. `pixi run typecheck` → clean; `CredentialLeakError` + `LeakHit` typed properly.
5. Manual interactive check: payload `{"variables": {"input": {"env": [{"key": "RUNPOD_API_KEY",
   "value": "rpa_FAKEFAKEFAKE12345"}]}}}` through `_redact_all` → `value == "<REDACTED>"`.
6. AGENTS.md, .env.example, README "Credential safety" section all committed.
7. PROGRESS.md snapshot lands as the final commit on the sub-plan.

## 10. Branch placement

All commits land on `build/layer-p`. This bug-fix is a sub-plan of Layer P Task 7 — does NOT
merge to main on its own. Layer P merges as a single `--no-ff` once item #3 (remote provision)
also lands; the redaction fix is a hard prerequisite for that work to resume safely.

## 11. Rollback

Single file changed in conftest (`tests/providers/conftest_runpod.py`); two new files
(`tests/providers/test_fixtures_audit.py`, `AGENTS.md`, optionally `.env.example`); one README
section; one PROGRESS block. All commits atomic per task. `git revert` of the sub-plan range
restores prior behaviour with no other system impact. Test count drops by exactly the new test
delta.
