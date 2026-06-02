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
   | `sk_token` | `sk-proj-...` / `sk-ant-api03-...` | OpenAI / Anthropic Console (guarded: ≥20 url-safe chars) |
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

All five MUST live in `.env` (gitignored). See `.env.example` for the canonical list:

- `RUNPOD_API_KEY` / `RUNPOD_TERMINATE_KEY` — RunPod provider (paired; terminate key reuses the main key via `${RUNPOD_API_KEY}` interpolation)
- `HF_TOKEN` — HuggingFace weight downloads
- `FAL_KEY` — fal.ai hosted engine
- `CIVITAI_TOKEN` — CivitAI gated/private models

### Out of scope for the redactor

- Process env scrubbing — a test that prints `os.environ['RUNPOD_API_KEY']` to stdout bypasses
  the seam. Don't write that test.
- Git history scrub — the project has never committed a real credential per PROGRESS:213.
- Encryption-at-rest of fixtures — fixtures are public test data; redaction IS the protection.
