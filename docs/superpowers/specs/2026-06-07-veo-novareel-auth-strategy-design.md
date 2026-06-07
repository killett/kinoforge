# Veo + Nova Reel + `AuthStrategy` substrate — design

Hosted-engine integrations for **Vertex AI Veo 2** and **AWS Bedrock Nova Reel
1.1** on top of a new pluggable-auth substrate (`AuthStrategy` ABC + concrete
`Bearer`, `GCPServiceAccount`, `AWSSigV4` strategies). Builds the foundation
every future hosted-engine layer reuses — Replicate / Runway / Luma in a later
session, Azure / OCI / Bedrock-Claude / Vertex-Imagen later still.

Ships as three sequential layers sharing this one spec.

## Status / pointers

- Date: 2026-06-07
- Brainstorm transcript: this spec captures the agreed design
- Implementation plans: one per layer, written by `gsd-plan-phase` after this
  spec is reviewed and approved
- Budget envelope: $20 (per `feedback_autonomous_no_gates`); projected ~$9
  total live spend across Layers 2–3

## 1. Goals and non-goals

### Goals

1. Ship a pluggable `AuthStrategy` ABC that admits Bearer-token, GCP SA, AWS
   SigV4, and (verified by appendix) Azure AD and OCI signing.
2. Validate the ABC end-to-end against two cloud-native hosted video providers
   that reuse existing GCP + AWS creds — no new operator signups.
3. Produce one real MP4 each from Veo and Nova Reel via
   `prompt-field-realistic.txt` end-to-end through `kinoforge generate`.
4. Lock the ABC's public surface with an invariant test from day one.
5. Front-load every cred / role / API enablement that can be automated; surface
   the one unavoidable operator action (GCP billing tier) clearly.
6. Capture live fixtures from both providers; commit deterministic offline
   replay tests that run in CI.

### Non-goals (explicit deferrals)

- Bearer providers requiring web signup (Replicate, Runway, Luma) — separate
  session per user's deferral.
- Real Azure AD code — pseudocode in appendix only.
- Real OCI code — sanity-check in appendix only.
- Self-hosted Veo / Nova Reel weights — both are managed-only.
- Streaming generation responses (both providers are batch async; streaming is
  a future layer).
- Per-call cost reporting beyond live-smoke `_meta.estimated_spend_usd` — Layer
  X candidate.

## 2. Three-layer decomposition

| Layer | Scope | Live spend | Net new LOC |
|---|---|---|---|
| **L1: `AuthStrategy` substrate** | ABC + 3 impls + `HostedAPIEngine` retrofit + `probe_hosted.py` + invariant + Azure/OCI appendices | $0 (fully offline) | ~250 LOC |
| **L2: `VeoEngine` + live smoke** | `engines/veo/` sibling + Vertex AI provisioning + live MP4 | ~$7.50 | ~80 LOC |
| **L3: `NovaReelEngine` + live smoke** | `engines/nova_reel/` sibling + Bedrock provisioning + live MP4 | ~$1.50 | ~80 LOC |

**Sequencing rule**: Layer 1 must merge to `main` before any Layer 2 or 3 work
begins. Layer 2 and Layer 3 are independent of each other and can proceed in
either order.

## 3. `AuthStrategy` ABC

### 3.1 Stable surface (locked by `test_core_invariant.py`)

```python
# src/kinoforge/core/auth.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class HealthResult:
    """Active-probe outcome for an AuthStrategy."""
    ok: bool
    identity: str | None      # populated on ok=True (e.g. SA email, IAM ARN)
    reason: str | None        # populated on ok=False


@dataclass(frozen=True)
class HttpRequest:
    """Mutable representation of an HTTP request for AuthStrategy.apply()."""
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class AuthStrategy(ABC):
    """Pluggable authentication strategy for engines that talk to remote APIs.

    Stable contract — methods below are locked by
    test_core_invariant.py::test_auth_strategy_abc_stable_surface against a
    checked-in baseline. Strategy-specific concerns live as constructor kwargs
    on concrete impls, NOT new ABC methods.
    """

    @abstractmethod
    def credentials_present(self) -> bool:
        """Cheap offline probe: are the required env vars / config files set?"""

    @abstractmethod
    def health_check(self) -> HealthResult:
        """Active wire probe: do credentials actually authenticate?

        Makes one cheap network call (token mint, sts.GetCallerIdentity, etc.).
        """

    @abstractmethod
    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Regex patterns matching secret-bearing content this strategy emits."""

    @abstractmethod
    def apply(self, request: HttpRequest) -> HttpRequest:
        """Add auth to a direct-HTTP request (engines NOT going through an SDK).

        SDK-wrapped engines may still call apply() to build the recording-seam-
        compatible signed request shape for fixture capture.
        """

    @abstractmethod
    def client_kwargs(self) -> dict[str, Any]:
        """Constructor kwargs for an engine's SDK client."""
```

### 3.2 Concrete strategies

```python
class Bearer(AuthStrategy):
    """For: existing HostedAPIEngine (fal); future Replicate/Runway/Luma."""
    def __init__(
        self,
        env_var: str,
        *,
        credential_provider: CredentialProvider | None = None,
        scheme: str = "Bearer",
        header_name: str = "Authorization",
        health_check_url: str | None = None,
    ) -> None: ...


class GCPServiceAccount(AuthStrategy):
    """For: VeoEngine; future Vertex AI integrations."""
    def __init__(
        self,
        *,
        scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",),
        quota_project_id: str | None = None,
        # Strategy-only extensions (NOT on ABC):
        impersonation_chain: tuple[str, ...] | None = None,
        subject: str | None = None,
    ) -> None: ...


class AWSSigV4(AuthStrategy):
    """For: NovaReelEngine; future Bedrock integrations."""
    def __init__(
        self,
        *,
        region_name: str,
        service_name: str = "bedrock-runtime",
        profile_name: str | None = None,
        # Strategy-only extensions:
        assume_role_arn: str | None = None,
        assume_role_external_id: str | None = None,
    ) -> None: ...
```

### 3.3 Lock-down: invariant test

```python
# tests/test_core_invariant.py::test_auth_strategy_abc_stable_surface

def test_auth_strategy_abc_stable_surface() -> None:
    """Lock AuthStrategy ABC against silent drift.

    To evolve the ABC intentionally, regenerate the baseline in the same
    commit as the ABC change.
    """
    import inspect, json
    from pathlib import Path
    from kinoforge.core.auth import AuthStrategy

    actual = {
        name: str(inspect.signature(getattr(AuthStrategy, name)))
        for name in (
            "credentials_present",
            "health_check",
            "redact_patterns",
            "apply",
            "client_kwargs",
        )
    }
    baseline = json.loads(
        Path("tests/fixtures/auth_strategy_baseline.json").read_text()
    )
    assert actual == baseline, (
        "AuthStrategy ABC drifted from baseline. If intentional, regenerate "
        "tests/fixtures/auth_strategy_baseline.json in the same commit."
    )
```

## 4. Per-engine design

### 4.1 `engines/veo/`

```
src/kinoforge/engines/veo/
  __init__.py     # registration + cfg + engine + backend
  config.py       # VeoEngineConfig pydantic
```

**Config schema** (`VeoEngineConfig`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `project_id` | str | required | GCP project for Vertex AI calls |
| `location` | str | `"us-central1"` | Vertex region |
| `model` | str | `"veo-2.0-generate-001"` | Veo 2 GA model; Veo 3 preview also supported |
| `output_gcs_uri` | str | required | GCS prefix Veo writes outputs to |
| `duration_seconds` | int | 5 | Veo currently caps at 8s for Veo 2 |
| `aspect_ratio` | str | `"16:9"` | `"16:9"` or `"9:16"` |
| `prompt_body_key` | str | `"prompt"` | Layer J prompt-routing key |
| `declared_flags_map` | dict | `{}` | Per-model probe declared flags |

**SDK**: `google-genai` (newer unified SDK), lazy-imported inside the engine
module. Pin: `google-genai = "<2.0,>=1.0"` in `pixi.toml`.

**Engine class** (`VeoEngine`):
- `requires_compute = False`, `requires_local_weights = False`
- `provision(None, cfg)`:
  - `auth.health_check()` → fail-fast on missing GCP creds
  - `client = genai.Client(vertexai=True, project=cfg.project_id, location=cfg.location, **auth.client_kwargs())`
- `backend(None, cfg)` → `VeoBackend(client, cfg, auth_strategy)`
- `key_base(cfg)`: `cfg["spec"].get("model")` ⤓ falls back to `cfg["engine"]["veo"]["model"]` (Layer M precedent)
- `extract_last_frame`: lazy-import `google.cloud.storage`, read object at `artifact.uri`, pipe to existing `core/frames.ffmpeg_last_frame`

**Backend class** (`VeoBackend`):
- `submit(job)`:
  - `prompt = resolve_prompt(job)` (Layer J helper)
  - `operation = client.models.generate_videos(model=cfg.model, prompt=prompt, config={...})`
  - Store `operation` in `_inflight: dict[str, GenerateVideosOperation]` keyed by `job.id`
  - Return `job.id`
- `result(job_id)`:
  - Poll `operation.done()`; sleep backoff `1 / 2 / 4 / 8 / 8 ...` capped at 8s
  - On `done`: extract `operation.response.generated_videos[0].video.uri`
  - Return `Artifact(uri=gcs_uri, filename=basename(gcs_uri), url=None, headers=None)`
- `validate_spec(spec)`:
  - Require `prompt` OR `segments[0].prompt` present
- `asset_paths`: image-conditioned variants upload reference images to GCS
  before submit; engine resolves asset roles → GCS URIs in the request

### 4.2 `engines/nova_reel/`

```
src/kinoforge/engines/nova_reel/
  __init__.py     # registration + cfg + engine + backend
  config.py       # NovaReelEngineConfig pydantic
```

**Config schema** (`NovaReelEngineConfig`):

| Field | Type | Default | Notes |
|---|---|---|---|
| `region_name` | str | `"us-east-1"` | Nova Reel region |
| `model_id` | str | `"amazon.nova-reel-v1:1"` | Nova Reel 1.1 |
| `output_s3_uri` | str | required | S3 prefix Nova Reel writes outputs to |
| `output_kms_key_id` | str ⎮ None | None | SSE-KMS key if bucket uses CMEK |
| `duration_seconds` | int | 6 | Nova Reel default |
| `fps` | int | 24 | |
| `dimension` | str | `"1280x720"` | |
| `prompt_body_key` | str | `"prompt"` | |
| `declared_flags_map` | dict | `{}` | |

**SDK**: `boto3` (already a dep). Client: `bedrock-runtime`. Lazy-imported.

**Engine class** (`NovaReelEngine`):
- Same lifecycle shape as Veo
- `provision(None, cfg)`:
  - `auth.health_check()` → fail-fast on missing AWS creds
  - `session = boto3.Session(profile_name=auth.profile_name)`
  - `client = session.client("bedrock-runtime", region_name=cfg.region_name, **auth.client_kwargs())`
- `extract_last_frame`: read S3 object via existing `S3ArtifactStore` (uri-aware)

**Backend class** (`NovaReelBackend`):
- `submit(job)`:
  - `prompt = resolve_prompt(job)`
  - `response = bedrock_runtime.start_async_invoke(modelId=cfg.model_id, modelInput={"taskType": "TEXT_VIDEO", "textToVideoParams": {"text": prompt, ...}, "videoGenerationConfig": {...}}, outputDataConfig={"s3OutputDataConfig": {"s3Uri": cfg.output_s3_uri, "kmsKeyId": cfg.output_kms_key_id}})`
  - Store `invocationArn` in `_inflight` keyed by `job.id`
- `result(job_id)`:
  - Poll `bedrock_runtime.get_async_invoke(invocationArn=arn)`; sleep backoff `2 / 4 / 8 / 8 ...`
  - On `status == "Completed"`: construct output URI as `{cfg.output_s3_uri}/{invocation_id}/output.mp4` where `invocation_id` is the last `/`-segment of `invocationArn`. Verify the object exists via `S3ArtifactStore.exists(uri)` before returning.
  - Return `Artifact(uri=s3_uri, filename="output.mp4", url=None, headers=None)`

### 4.3 Config → `AuthStrategy` binding

Each engine config exposes an optional `auth:` sub-block with a `strategy:`
discriminator that the engine's config model parses into a concrete
`AuthStrategy` instance at config-load time. Per-engine config carries the
auth choice; there is NO global `auth:` block.

```python
# src/kinoforge/core/auth.py — at module bottom

_REGISTRY: dict[str, type[AuthStrategy]] = {
    "bearer": Bearer,
    "gcp_service_account": GCPServiceAccount,
    "aws_sigv4": AWSSigV4,
}


def build_auth_strategy(spec: dict[str, Any]) -> AuthStrategy:
    """Construct a concrete strategy from a parsed YAML auth: block.

    spec shape: {"strategy": "<name>", <strategy-specific kwargs>}
    """
    name = spec["strategy"]
    if name not in _REGISTRY:
        raise UnknownAdapter(f"unknown auth strategy: {name}")
    kwargs = {k: v for k, v in spec.items() if k != "strategy"}
    return _REGISTRY[name](**kwargs)
```

Each engine's pydantic config calls `build_auth_strategy` on a nested `auth:`
mapping. Strategy-specific extension kwargs (e.g. `impersonation_chain` for
GCP SA) pass through verbatim. Unknown strategy names raise `UnknownAdapter`.

**Examples** (full YAML in Section 8):

```yaml
engine:
  veo:
    project_id: ...
    auth:
      strategy: gcp_service_account
      scopes: ["https://www.googleapis.com/auth/cloud-platform"]

  nova_reel:
    region_name: us-east-1
    auth:
      strategy: aws_sigv4
      region_name: us-east-1
```

The existing `HostedAPIEngine` config gains the same nested `auth:` block;
when absent, defaults to `Bearer(env_var=cfg.api_key_env)` for backward-compat
(see 4.4).

### 4.4 `HostedAPIEngine` retrofit (existing, minor)

```python
def __init__(
    self,
    cfg,
    *,
    http_get=None,
    http_post=None,
    auth_strategy: AuthStrategy | None = None,
):
    if auth_strategy is None:
        # Backward-compat: existing cfg.api_key_env still works
        auth_strategy = Bearer(env_var=cfg.api_key_env)
    self._auth = auth_strategy
```

Existing fal config keeps working bit-for-bit. Future Replicate/Runway/Luma
configs instantiate via `auth_strategy=Bearer(env_var=...)`. Pre-implementation
grep at Layer 1 plan-write time enumerates every `HostedAPIEngine(...)`
construction site.

## 5. Front-loaded cred provisioning

### 5.1 Veo on Vertex AI

Layer 2 plan executes these via `gcloud` from inside the container:

```
gcloud services enable aiplatform.googleapis.com
gcloud projects add-iam-policy-binding $PID \
  --member=serviceAccount:$SA --role=roles/aiplatform.user
gcloud storage buckets create gs://kinoforge-veo-output-${PID}
```

**Probe (`tools/probe_hosted.py`) verifies post-enable**:
- `aiplatform.googleapis.com` is `ENABLED`
- `client.models.list()` includes `veo-2.0-generate-001`
- SA effective roles include `aiplatform.user`
- Output bucket exists and SA has `storage.objectAdmin` on it

**Unavoidable operator action**: GCP billing tier upgrade (same blocker that
paused Layer W+β). If already upgraded, Veo is fully autonomous. If not, probe
surfaces it via the same console URL handler from Layer W+α (commit `c0aa2d4`).

### 5.2 Nova Reel on AWS Bedrock

Layer 3 plan executes via `aws iam`:

```
aws iam put-user-policy --user-name kinoforge-ci \
  --policy-name kinoforge-nova-reel \
  --policy-document file://.aws/policies/bedrock-nova-reel.json
```

Inline policy grants:
- `bedrock:InvokeModel`, `bedrock:StartAsyncInvoke`, `bedrock:GetAsyncInvoke`
  on `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-reel-v1:1`
- `bedrock:ListFoundationModels` on `*`
- `s3:PutObject`, `s3:GetObject` on the output bucket

**Probe verifies post-enable**:
- `bedrock list-foundation-models --by-output-modality VIDEO` returns Nova Reel
  with `inferenceTypesSupported` including `ON_DEMAND` or `ASYNC`
- `sts.GetCallerIdentity` returns the expected user ARN

**Model-access agreement**: Amazon-first-party Nova models are usually
auto-granted on IAM grant. Probe surfaces console URL if agreement is required.

### 5.3 Secrets storage — no `.env` changes

Both engines reuse existing creds:
- Veo → `GOOGLE_APPLICATION_CREDENTIALS=/workspace/.gcp/kinoforge-sa.json`
- Nova Reel → AWS env chain (existing)

`.env.example` already documents this pattern (commit `8f72c25`).

### 5.4 Documentation

`docs/CLOUD-CREDS.md` gains two new rows:

| Service | Purpose | Auth | Region | Output sink |
|---|---|---|---|---|
| Vertex AI Veo 2 | t2v/i2v hosted | GCP SA | us-central1 | `gs://kinoforge-veo-output-${PID}/` |
| Bedrock Nova Reel 1.1 | t2v hosted | AWS SigV4 | us-east-1 | `s3://kinoforge-nova-reel-output/` |

Plus per-service subsections covering model-access status, attached IAM
policies, and probe history.

## 6. Testing strategy

### 6.1 Test layout

```
tests/core/test_auth.py                    # L1: ABC + 3 strategies
tests/_fixtures/fake_auth.py               # L1: FakeAuthStrategy for engine tests
tests/fixtures/auth_strategy_baseline.json # L1: ABC signature baseline
tests/engines/test_veo.py                  # L2: offline VeoEngine
tests/engines/fixtures/veo/                # L2: captured live fixtures
tests/engines/test_nova_reel.py            # L3: offline NovaReelEngine
tests/engines/fixtures/nova_reel/          # L3: captured live fixtures
tests/live/test_veo_live.py                # L2: live opt-in smoke
tests/live/test_nova_reel_live.py          # L3: live opt-in smoke
tests/test_offline_isolation.py            # L1: extend SDK-isolation check
```

### 6.2 Unit coverage per strategy

For each of `Bearer`, `GCPServiceAccount`, `AWSSigV4`:
- `credentials_present()` returns True/False per env state
- `health_check()` returns `HealthResult` shape (mocked SDK)
- `redact_patterns()` returns non-empty list; each pattern matches a synthetic
  secret sample
- `apply()` produces the correct Authorization header / signature on a sample
  request
- `client_kwargs()` returns the dict shape the engine's SDK expects
- Cross-strategy: redact pipeline catches every synthetic secret; raises
  `CredentialLeakError` on unredacted match

### 6.3 Recording-seam compatibility

Three injection paths feed one redaction pipeline:

| Engine path | Recording mechanism | Precedent |
|---|---|---|
| Direct HTTP (HostedAPIEngine via urllib) | Existing `http_get` / `http_post` seams | Layer N (commit `7c2de86`) |
| `boto3` SDK (NovaReelEngine) | `session.events.register("before-send.bedrock-runtime.*", recorder)` | Layer W (commit `6d61d60`) |
| `google-genai` SDK (VeoEngine) | `httpx.MockTransport` via `genai.Client(http_options={...})` | NEW — Layer 2 ships this seam |

All write the same JSON fixture shape: `{request, response, _meta: {git_sha,
capture_time, strategy}}`.

### 6.4 Live-smoke design

Both live smokes share structure (mirrors Layer P's `test_comfyui_wan_live`):

```python
# tests/live/test_veo_live.py
pytestmark = pytest.mark.live

if not (
    os.getenv("KINOFORGE_LIVE_TESTS") == "1"
    and Path(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")).exists()
):
    pytest.skip(
        "requires KINOFORGE_LIVE_TESTS=1 + GOOGLE_APPLICATION_CREDENTIALS",
        allow_module_level=True,
    )


def test_veo_live_e2e_smoke(tmp_path: Path) -> None:
    """End-to-end: deploy → generate (hosted, no provider) → MP4 in GCS → cleanup."""
    # 1. Run probe_hosted; assert exit 0
    # 2. Load examples/configs/veo.yaml
    # 3. Read prompt from /workspace/prompt-field-realistic.txt
    # 4. Verify estimated cost <= KINOFORGE_LIVE_BUDGET_USD (default $1.50)
    # 5. orchestrator.generate(cfg, request)
    # 6. Assert artifact.uri starts with gs://
    # 7. Download via GCS client; assert bytes start with MP4 ftyp prefix
    # 8. If KINOFORGE_SAVE_FIXTURES=1, write captured fixtures
    # 9. Cleanup: delete generated GCS object
```

Nova Reel smoke parallels with S3 + boto3 substituted.

### 6.5 Offline replay

`FixtureReplayClient` (generalized from Phase 38 T11) lets offline tests run
the same engine code paths against canned responses:

```python
def test_veo_offline_replay_from_fixtures() -> None:
    client = FixtureReplayClient.from_dir("tests/engines/fixtures/veo/")
    engine = VeoEngine(cfg, client=client, auth_strategy=FakeAuthStrategy())
    backend = engine.backend(None, cfg)
    job_id = backend.submit(job)
    artifact = backend.result(job_id)
    assert artifact.uri.startswith("gs://")
```

### 6.6 Offline isolation guard

`tests/test_offline_isolation.py::test_no_live_strategy_imports_in_offline_path`
extends the Phase 38 check to assert:
- `boto3` not in `sys.modules` after `import kinoforge.core.auth`
- `google.genai` not in `sys.modules` after `import kinoforge.core.auth`
- All SDK imports strictly lazy, inside engine modules only

Mirrors PROGRESS:62 core-import-ban invariant.

### 6.7 TDD red-first ordering

**Layer 1**:
1. `test_auth.py::test_bearer_credentials_present` — RED → GREEN
2. Repeat per method × per strategy
3. `test_auth_strategy_abc_stable_surface` — RED, commit baseline file, GREEN
4. `HostedAPIEngine` retrofit + regression suite stays GREEN
5. `probe_hosted.py` — RED first, then assertions

**Layer 2**:
1. `test_veo.py::test_config_loads_from_yaml` — RED → GREEN
2. `test_veo.py::test_submit_with_fake_client` — RED → GREEN
3. Continue down to engine wiring
4. **Commit RED live-smoke scaffold BEFORE any live spend** (CLAUDE.md
   durability rule)
5. Fire live smoke; capture fixtures; commit fixtures
6. Wire offline replay test; GREEN
7. Docs + PROGRESS

**Layer 3**: parallel structure for Nova Reel.

### 6.8 Budget cap mechanics

Beyond per-test caps:
- Layer 2 smoke caps at $1.50 — refuses to fire if predicted cost > cap
- Layer 3 smoke caps at $0.50
- Each captured fixture's `_meta.estimated_spend_usd` records actual billed
  amount
- Cumulative spend tracked via `tools/preflight.py` extension reading captured
  fixture metas

## 7. Risk mitigation

### 7.1 Azure pseudocode appendix (Appendix A — design-time deliverable)

Layer 1 spec ships a complete pseudocode of an `AzureAD` strategy implementing
all 5 ABC methods, including the new strategy-only kwargs (`tenant_id`,
`audience`). Verifies ABC requires zero changes to admit it. Pseudocode is
written FIRST during Layer 1 T1.1; ABC is designed to admit it.

### 7.2 OCI sanity check (Appendix B — design-time deliverable)

Layer 1 spec ships an `OCISignature` strategy pseudocode. Verifies per-request
RSA signing fits `apply()` without ABC change. Catches AWS+GCP over-fit.

### 7.3 Typed boundary objects

ABC takes / returns explicit dataclass types (`HealthResult`, `HttpRequest`),
not `dict` / `Any`. Prevents duck-typing drift across strategies.

### 7.4 Mandatory methods

Every concrete strategy MUST implement all 5 ABC methods. No `NotImplementedError`
stubs. Forces completeness at strategy-creation time.

### 7.5 `FakeAuthStrategy` shared fixture

Layer 1 ships a complete, no-network `FakeAuthStrategy` engine tests use to
exercise the ABC from the consumer side. Every engine test implicitly tests
the ABC contract.

### 7.6 SDK version pin

`pixi.toml` pins:
- `google-genai = "<2.0,>=1.0"` (Layer 2 lazy import)
- `boto3 = "<2.0,>=1.34"` (already pinned)

### 7.7 Layer sequencing hard-block

Layer 2 + Layer 3 plans hard-block on Layer 1 merged to `main`. Prevents
live-spend work without foundation in place.

### 7.8 Pre-implementation grep

Layer 1 plan-write step enumerates every `HostedAPIEngine(...)` construction
site to ensure the retrofit doesn't break any caller.

## 8. Examples / config

### 8.1 `examples/configs/veo.yaml`

```yaml
engine:
  veo:
    project_id: ${GCP_PROJECT_ID}
    location: us-central1
    model: veo-2.0-generate-001
    output_gcs_uri: gs://kinoforge-veo-output-${GCP_PROJECT_ID}/
    duration_seconds: 5
    aspect_ratio: "16:9"
    auth:
      strategy: gcp_service_account
      # GOOGLE_APPLICATION_CREDENTIALS picked from environment via google.auth
      # default chain — no explicit kwargs needed for default flow

spec:
  prompt: ""  # filled at runtime from prompt-field-realistic.txt

lifecycle:
  max_in_flight: 1
  idle_timeout_s: 600
```

### 8.2 `examples/configs/nova-reel.yaml`

```yaml
engine:
  nova_reel:
    region_name: us-east-1
    model_id: amazon.nova-reel-v1:1
    output_s3_uri: s3://kinoforge-nova-reel-output/
    duration_seconds: 6
    fps: 24
    dimension: "1280x720"
    auth:
      strategy: aws_sigv4
      region_name: us-east-1
      # profile_name: kinoforge-ci  # optional; defaults to AWS default chain

spec:
  prompt: ""  # filled at runtime from prompt-field-realistic.txt

lifecycle:
  max_in_flight: 1
  idle_timeout_s: 600
```

## Appendix A — Imaginary `AzureAD` strategy

```python
# NOT shipped. Verifies ABC admits third cloud-native provider.

class AzureAD(AuthStrategy):
    """Pseudocode — ABC verification only.

    New strategy-level dimensions surfaced by Azure:
    - tenant_id (explicit multi-tenancy)
    - audience (explicit resource scope, e.g. cognitiveservices.azure.com/.default)
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        audience: str,
        client_id: str | None = None,
        client_secret: str | None = None,
        # Strategy-only extensions:
        additionally_allowed_tenants: tuple[str, ...] = (),
        managed_identity_client_id: str | None = None,
    ) -> None:
        ...

    def credentials_present(self) -> bool:
        # AZURE_TENANT_ID + AZURE_CLIENT_ID + AZURE_CLIENT_SECRET in env,
        # OR managed identity available, OR az CLI logged in
        ...

    def health_check(self) -> HealthResult:
        # Call Microsoft Graph /me with the audience-scoped token;
        # returns identity = upn from the token payload
        ...

    def redact_patterns(self) -> list[re.Pattern[str]]:
        return [
            re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # JWT
            re.compile(self._client_secret_pattern),
        ]

    def apply(self, request: HttpRequest) -> HttpRequest:
        # Mint audience-scoped bearer token via DefaultAzureCredential, add to
        # Authorization header
        ...

    def client_kwargs(self) -> dict[str, Any]:
        return {
            "credential": DefaultAzureCredential(
                managed_identity_client_id=self._managed_identity_client_id,
                additionally_allowed_tenants=self._additionally_allowed_tenants,
            ),
            "subscription_id": self._subscription_id,
        }
```

| ABC method | Azure realization | ABC change needed? |
|---|---|---|
| `credentials_present` | Env chain check | No |
| `health_check` | Graph `/me` call | No |
| `redact_patterns` | JWT regex + client-secret regex | No |
| `apply` | Token mint + Authorization header | No |
| `client_kwargs` | `DefaultAzureCredential` + sub | No |

**Result**: zero ABC changes. ✓

## Appendix B — OCI sanity check

```python
# NOT shipped. Sanity check that ABC isn't over-fitted to AWS+GCP token model.

class OCISignature(AuthStrategy):
    """Pseudocode — sanity check. Per-request RSA signing, not token-based."""

    def __init__(
        self,
        *,
        tenancy_ocid: str,
        user_ocid: str,
        key_file: str,
        fingerprint: str,
        region: str,
        # Strategy-only:
        pass_phrase: str | None = None,
        delegation_token: str | None = None,
    ) -> None:
        ...

    def credentials_present(self) -> bool:
        # ~/.oci/config exists with [DEFAULT] profile populated
        ...

    def health_check(self) -> HealthResult:
        # identity.get_user(user_ocid) via oci.Signer
        ...

    def redact_patterns(self) -> list[re.Pattern[str]]:
        return [
            re.compile(self._fingerprint_pattern),
            re.compile(r"ocid1\.[a-z]+\.[a-z0-9.\-]+"),
        ]

    def apply(self, request: HttpRequest) -> HttpRequest:
        # Sign request with oci.signer.Signer (RSA per-request, not token)
        signer = oci.signer.Signer(
            tenancy=self._tenancy_ocid,
            user=self._user_ocid,
            fingerprint=self._fingerprint,
            private_key_file_location=self._key_file,
        )
        return signer.sign_request(request)

    def client_kwargs(self) -> dict[str, Any]:
        return {
            "signer": self._signer_singleton,
            "region": self._region,
        }
```

**Per-request RSA signing fits `apply()` cleanly** — signer wraps the request,
returns signed request. No ABC change. ✓ ABC is not over-fitted to AWS+GCP.

## Out of scope / explicit deferrals

- Replicate / Runway / Luma signups + integrations (separate session per user)
- Real Azure / OCI implementations
- Streaming response handling
- Per-call cost reporting beyond `_meta.estimated_spend_usd`
- Veo 3 audio generation
- Nova Reel multi-shot (>6s segmented)
- Multipart input uploads via SDKs (using S3/GCS pre-uploaded URIs only)
- Webhook-based result delivery (polling only)
- Cross-region failover

## Closes / partially closes

- PROGRESS:113 carry-forward "Engine-integration live smoke" — partial (closes
  the Veo + Nova Reel slice; ComfyUI closed earlier by Layer P; Diffusers,
  RunPod serverless still open as future layers)
- New entry: enables future Bearer-provider integrations (Replicate, Runway,
  Luma) to land config-only without engine work
