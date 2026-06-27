# Credentials

(Moved from README §Credentials (full deep dive incl. Precedence, Known keys, .env safety, test safety, aria2c), §Cloud bootstrap (Layer W+α), §Auth strategies on 2026-06-27. See [../README.md](../README.md).)

## Cloud bootstrap (Layer W+α)

SkyPilot multi-cloud GPU work is gated by AWS + GCP permission and quota
readiness. Run `pixi run cloud:perms-probe` to verify; the probe writes
snapshots to `.aws/perms-snapshot.json` and `.gcp/perms-snapshot.json`
(gitignored). Exit 0 = green; 1 = auth or required action denied;
2 = quota gap pending (AWS auto-submits via the SDK, GCP emits a console
URL because no SDK surface exists for compute-quota requests). See
`docs/CLOUD-CREDS.md` for the bootstrap inventory, the scoped IAM policy
doc at `.aws/policies/skypilot-minimal.json`, and the SkyPilot
permissions summary.

## Credentials

Kinoforge reads its API credentials from environment variables. To avoid
exporting them in `~/.bashrc`, copy the checked-in template:

```bash
cp .env.example .env
chmod 600 .env
# Edit .env and fill in the keys you need.
```

The CLI auto-loads `./.env` from whatever directory you invoke `kinoforge`
in (typically the project root). Absent file is a silent no-op — you'll
get an `AuthError` on first secret use instead. To load a different file
explicitly:

```bash
kinoforge --env-file /path/to/other.env generate --config ...
```

### Precedence

Shell-set values **always win** over `.env` values. CI/prod exports always
take precedence over a stale dev `.env`. To override this in your own
Python scripts, call
`kinoforge.core.dotenv_loader.load_env_file(path, override=True)`.

### Known keys

| Variable | Used by | Required when |
|---|---|---|
| `FAL_KEY` | `HostedAPIEngine` (fal.ai) | Hosted engine path against fal.ai |
| `CIVITAI_TOKEN` | `CivitAISource` | Downloading gated/private CivitAI models |
| `HF_TOKEN` | `HuggingFaceSource` | Downloading gated/private HF repos |
| `RUNPOD_API_KEY` | `RunPodProvider` | Provisioning RunPod compute |

AWS / GCP credentials are NOT managed by kinoforge — the `boto3` and
`google-cloud-storage` SDKs walk their own default credential chains
(env → `~/.aws/credentials` → IMDS → IAM role / ADC → gcloud config →
GCE metadata) unchanged. You may put `AWS_ACCESS_KEY_ID` +
`AWS_SECRET_ACCESS_KEY` (boto3 needs both), `GOOGLE_APPLICATION_CREDENTIALS`,
etc. into your `.env` if you prefer a single file; the SDK chains pick
them up via `os.environ`.

### Never commit `.env`

`.env` is in `.gitignore`. Only commit `.env.example` (no values).

### Credential safety in tests

Secrets enter kinoforge tests via `.env` only — never via test code, fixtures, example YAML, or
commit messages. The `_RecordingHTTPSeam` in `tests/providers/conftest_runpod.py` runs a layered
redaction pipeline over every captured payload and refuses (via `CredentialLeakError`) to write a
fixture that still contains a credential pattern. See [`../AGENTS.md`](../AGENTS.md) for the contributor
guide, the pattern table, and the procedure for adding a new credential format.

### Faster downloads (aria2c)

kinoforge auto-detects `aria2c` on `PATH` and uses it as a transparent
multi-connection fast-path for every model fetch. With aria2c installed
on a typical residential link, the Wan 2.1 weight set (~9 GiB total)
downloads in roughly one-tenth the wall-clock time it takes via the
stdlib transport.

Install:
- Debian / Ubuntu: `sudo apt install aria2`
- macOS (Homebrew): `brew install aria2`
- Windows (Chocolatey): `choco install aria2`

No configuration is required. If aria2c is absent, or if the subprocess
fails for any reason (CDN rate-limit, transient network error,
unexpected flag deprecation in a future aria2c release), the failure is
logged at `WARNING` level and the stdlib single-connection path is used
as a fallback — operators always get the file.

## Auth strategies

Hosted engines authenticate via a pluggable `AuthStrategy`. Three concrete
strategies ship in `kinoforge.core.auth`:

| Name | Used by | Auth shape |
|---|---|---|
| `bearer` | `HostedAPIEngine` (fal, Replicate, Runway) | `Authorization: Bearer <env-var>` |
| `gcp_service_account` | VeoEngine (Layer 2); future Vertex AI integrations | `google.auth` default chain |
| `aws_sigv4` | NovaReelEngine (Layer 3); future Bedrock integrations | SigV4 request signing |

Each engine config carries a nested `auth:` block with a `strategy:`
discriminator. Example:

```yaml
engine:
  hosted:
    endpoint: https://fal.run/fal-ai/wan-t2v
    auth:
      strategy: bearer
      env_var: FAL_KEY
```

Backward-compat: when `auth:` is omitted on an existing hosted config,
`provision()` derives `Bearer(env_var=cfg.api_key_env)` automatically.

Preflight: `pixi run probe-hosted -- --config <config-path>` walks every
configured strategy and verifies credentials + health before any live
call.

Adding a new strategy: subclass `AuthStrategy`, implement all 5 methods
(`credentials_present`, `health_check`, `redact_patterns`, `apply`,
`client_kwargs`), then register the class name in `_REGISTRY` in
`src/kinoforge/core/auth.py`. The ABC's stable surface is locked by
`test_auth_strategy_abc_stable_surface` — intentional evolution requires
regenerating `tests/fixtures/auth_strategy_baseline.json` in the same
commit.
