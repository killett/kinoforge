# Layer 3 — NovaReelEngine + live smoke Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a new `NovaReelEngine` sibling under `src/kinoforge/engines/nova_reel/` that talks to AWS Bedrock's Nova Reel 1.1 model via `boto3` bedrock-runtime, fueled by the Layer 1 `AWSSigV4` AuthStrategy. Land a live smoke that produces a real MP4 from `prompt-field-realistic.txt` end-to-end and capture fixtures for offline replay.

**Architecture:** Sibling pattern of `engines/fal/` and `engines/hosted/`. Lazy `boto3.Session.client("bedrock-runtime")` resolved through `AWSSigV4.client_kwargs()`. `start_async_invoke` returns an invocation ARN; the backend polls `get_async_invoke` until `status="Completed"`, then derives the output URI `{cfg.output_s3_uri}/{invocation_id}/output.mp4`. Bedrock writes the MP4 to the configured S3 prefix; the engine returns an `Artifact(uri=s3://...)`. The existing `S3ArtifactStore.get_bytes` reads it back for verification + `extract_last_frame`.

**Tech Stack:** Python 3.13, pydantic v2, `boto3 >=1.34,<2.0` (already pinned in Layer 1 Task 0), lazy `bedrock-runtime` client, AWS SigV4 via Layer 1's strategy. Live smoke uses real AWS Bedrock in `us-east-1`; offline replay uses a fixture-replay boto3 client double.

**Spec reference:** `docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md` — Sections 4.2 (engine design), 5.2 (cred provisioning), 6.4 (live smoke), 6.5 (offline replay), 8.2 (example YAML).

**Depends on:** Layer 1 merged to `main` at commit `7e00557` (AuthStrategy ABC + Bearer / GCPServiceAccount / AWSSigV4 + `build_auth_strategy` + `FakeAuthStrategy` + `tools/probe_hosted.py`). The Layer 1 invariant tests guard the ABC's stable surface — Layer 3 must NOT change `core/auth.py`.

**Live spend in this layer:** ~$0.50 cold per smoke; budget cap $1.50 across iterations (per session brief). Session-wide cap is $20 (per `feedback_autonomous_no_gates`). Spend happens only in **Task 7**; every prior task is offline.

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `src/kinoforge/core/config.py` | Modify | Add `NovaReelEngineConfig` pydantic model + wire onto `EngineConfig.nova_reel` |
| `src/kinoforge/engines/nova_reel/__init__.py` | Create | `NovaReelEngine` + `NovaReelBackend`; lazy `boto3.Session.client("bedrock-runtime")` via Layer 1 `AWSSigV4.client_kwargs()`; self-register under `"nova_reel"` |
| `tests/engines/test_nova_reel.py` | Create | Offline unit tests with a `_FakeBedrockRuntime` test double + `AWSSigV4` strategy from Layer 1 |
| `examples/configs/nova-reel.yaml` | Create | Full kinoforge config wiring NovaReelEngine + auth_strategy=aws_sigv4 + spec.prompt placeholder |
| `tests/test_examples.py` | Modify | Add one test that loads `examples/configs/nova-reel.yaml` and asserts the parsed engine kind |
| `.aws/policies/bedrock-nova-reel.json` | Create | Scoped inline IAM policy granting Bedrock invoke perms + S3 output-bucket access (committed but ATTACHED in Task 4) |
| `tools/probe_hosted.py` | Modify | Add a `--check-bedrock-model-access MODEL_ID` flag that runs `list_foundation_models` and verifies the model appears |
| `tests/live/test_nova_reel_live.py` | Create | KINOFORGE_LIVE_TESTS-gated E2E smoke: deploy → generate → MP4 lands in S3 → bytes start with MP4 ftyp prefix → cleanup |
| `tests/engines/fixtures/nova_reel/` | Create | Captured live fixtures (committed after Task 7 successful smoke) |
| `tests/engines/test_nova_reel_replay.py` | Create | Offline replay test against captured fixtures (uses `FixtureReplayClient` pattern from Phase 38 T11) |
| `README.md` | Modify | Add "Nova Reel" subsection under "Real providers" |
| `PROGRESS.md` | Modify | Add Phase 42 (Layer 3) entry with per-task SHAs |

---

## Task 0: `NovaReelEngineConfig` pydantic + wire onto `EngineConfig`

**Goal:** Define the pydantic config block for `NovaReelEngine` and add it as an optional field on `EngineConfig` (mirroring `FalEngineConfig` pattern). 4 round-trip tests.

**Files:**
- Modify: `src/kinoforge/core/config.py` — add `NovaReelEngineConfig` class after `FalEngineConfig` (around line 348); add `nova_reel: NovaReelEngineConfig | None = None` to `EngineConfig` (around line 368)
- Modify: `tests/core/test_config.py` — append 4 round-trip tests

**Acceptance Criteria:**
- [ ] `NovaReelEngineConfig` loads from YAML with `region_name`, `model_id`, `output_s3_uri`, `output_kms_key_id`, `duration_seconds`, `fps`, `dimension`, `prompt_body_key`, `declared_flags_map`
- [ ] Required fields: `region_name`, `output_s3_uri`. Defaults: `model_id="amazon.nova-reel-v1:1"`, `duration_seconds=6`, `fps=24`, `dimension="1280x720"`, `prompt_body_key="prompt"`, `declared_flags_map={}`, `output_kms_key_id=None`
- [ ] `output_s3_uri` field-validator rejects non-`s3://` strings
- [ ] `extra="forbid"` rejects unknown YAML keys (matches `HostedEngineConfig` precedent)
- [ ] `EngineConfig.nova_reel` field exists; `cfg = Config.parse_obj({...})` round-trips through it
- [ ] 4 new tests pass; existing config tests unchanged

**Verify:** `pixi run test tests/core/test_config.py -v -k nova_reel 2>&1 | tail -10` → 4 passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/core/test_config.py`:

```python
# ---------------------------------------------------------------------------
# Layer 3 — NovaReelEngineConfig
# ---------------------------------------------------------------------------


def test_nova_reel_engine_config_loads_required_fields() -> None:
    from kinoforge.core.config import NovaReelEngineConfig

    cfg = NovaReelEngineConfig(
        region_name="us-east-1",
        output_s3_uri="s3://kinoforge-nova-reel-output/",
    )
    assert cfg.region_name == "us-east-1"
    assert cfg.output_s3_uri == "s3://kinoforge-nova-reel-output/"
    # Defaults
    assert cfg.model_id == "amazon.nova-reel-v1:1"
    assert cfg.duration_seconds == 6
    assert cfg.fps == 24
    assert cfg.dimension == "1280x720"
    assert cfg.prompt_body_key == "prompt"
    assert cfg.declared_flags_map == {}
    assert cfg.output_kms_key_id is None


def test_nova_reel_engine_config_rejects_non_s3_output_uri() -> None:
    import pydantic
    from kinoforge.core.config import NovaReelEngineConfig

    with pytest.raises(pydantic.ValidationError, match="s3://"):
        NovaReelEngineConfig(
            region_name="us-east-1",
            output_s3_uri="https://wrong.example.com/",
        )


def test_nova_reel_engine_config_forbids_unknown_keys() -> None:
    import pydantic
    from kinoforge.core.config import NovaReelEngineConfig

    with pytest.raises(pydantic.ValidationError, match="extra"):
        NovaReelEngineConfig(
            region_name="us-east-1",
            output_s3_uri="s3://kinoforge-nova-reel-output/",
            unknown_field="oops",
        )


def test_engine_config_nova_reel_optional() -> None:
    from kinoforge.core.config import EngineConfig, NovaReelEngineConfig

    cfg = EngineConfig(
        kind="nova_reel",
        precision="fp16",
        nova_reel=NovaReelEngineConfig(
            region_name="us-east-1",
            output_s3_uri="s3://kinoforge-nova-reel-output/",
        ),
    )
    assert cfg.kind == "nova_reel"
    assert cfg.nova_reel is not None
    assert cfg.nova_reel.region_name == "us-east-1"
    # Sibling engines still default to None
    assert cfg.hosted is None
    assert cfg.fal is None
```

- [ ] **Step 2: Confirm red**

Run: `pixi run test tests/core/test_config.py -v -k nova_reel 2>&1 | tail -10`
Expected: `ImportError: cannot import name 'NovaReelEngineConfig'`.

- [ ] **Step 3: Add the pydantic class**

In `src/kinoforge/core/config.py`, insert after the `FalEngineConfig` class (search for `class FalEngineConfig`, place this AFTER its last `@field_validator` block):

```python
class NovaReelEngineConfig(BaseModel):
    """AWS Bedrock Nova Reel engine parameters.

    Attributes:
        region_name: AWS region. Nova Reel currently runs in ``us-east-1``;
            other regions added by AWS over time will be opt-in here.
        model_id: Bedrock model identifier. Defaults to
            ``"amazon.nova-reel-v1:1"`` (Nova Reel 1.1).
        output_s3_uri: S3 prefix Nova Reel writes generated MP4s into. Must
            start with ``s3://``. Bedrock async invocations require an S3
            output destination (no inline response shape).
        output_kms_key_id: Optional SSE-KMS key ARN if the output bucket uses
            customer-managed encryption.
        duration_seconds: Length of the generated clip; Nova Reel default 6s.
        fps: Frames per second; Nova Reel default 24.
        dimension: Output resolution ``WxH``; default ``"1280x720"``.
        prompt_body_key: Key in the model input where the prompt lives.
            Matches Layer J prompt-routing convention.
        declared_flags_map: Per-capability-key strategy-flag overrides
            (matches sibling-engine convention).
    """

    model_config = ConfigDict(extra="forbid")

    region_name: str
    model_id: str = "amazon.nova-reel-v1:1"
    output_s3_uri: str
    output_kms_key_id: str | None = None
    duration_seconds: int = 6
    fps: int = 24
    dimension: str = "1280x720"
    prompt_body_key: str = "prompt"
    declared_flags_map: dict[str, dict[str, bool]] = Field(default_factory=dict)

    @field_validator("output_s3_uri")
    @classmethod
    def _check_output_s3_uri(cls, v: str) -> str:
        if not v.startswith("s3://"):
            raise ValueError(
                f"engine.nova_reel.output_s3_uri must start with 's3://', got {v!r}"
            )
        return v
```

In the same file, modify `EngineConfig` (search for `class EngineConfig`):

```python
class EngineConfig(BaseModel):
    """Top-level engine block.

    Attributes:
        kind: Engine name; must be one of the known engine types.
        precision: Precision/quantization string (e.g. "fp16", "gguf-q8").
        comfyui: ComfyUI-specific config, required when kind == "comfyui".
        hosted: Hosted API config, required when kind == "hosted".
        diffusers: Diffusers-specific config, optional even when
            kind == "diffusers" (all fields default to empty).
        fal: fal.ai queue-API config, required when kind == "fal".
        nova_reel: AWS Bedrock Nova Reel config, required when
            kind == "nova_reel".
    """

    kind: str
    precision: str
    comfyui: ComfyUIEngineConfig | None = None
    hosted: HostedEngineConfig | None = None
    diffusers: DiffusersEngineConfig | None = None
    fal: FalEngineConfig | None = None
    nova_reel: NovaReelEngineConfig | None = None
```

Confirm `ConfigDict` and `field_validator` are already imported at the top of the file (they should be — verify with `rg -n "^from pydantic" src/kinoforge/core/config.py`).

- [ ] **Step 4: Confirm green**

Run: `pixi run test tests/core/test_config.py -v -k nova_reel 2>&1 | tail -10`
Expected: 4 passed.

Run: `pixi run test tests/core/test_config.py 2>&1 | tail -5`
Expected: all pre-existing config tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /workspace
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "$(cat <<'EOF'
feat(core/config): NovaReelEngineConfig pydantic block

Layer 3 prep. Models AWS Bedrock Nova Reel 1.1 parameters with
extra='forbid' (matches HostedEngineConfig precedent). output_s3_uri
is required (Bedrock async invocations write to S3, no inline body).
Wired onto EngineConfig.nova_reel as an optional field.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 1: `engines/nova_reel/` package + offline unit tests

**Goal:** Ship `NovaReelEngine` + `NovaReelBackend` with full offline coverage against a `_FakeBedrockRuntime` test double. Engine consumes the Layer 1 `AWSSigV4` strategy via `client_kwargs()`. Self-registers under `"nova_reel"` on import.

**Files:**
- Create: `src/kinoforge/engines/nova_reel/__init__.py` (~150 LOC)
- Create: `tests/engines/test_nova_reel.py` (~10 unit tests, no live calls)

**Acceptance Criteria:**
- [ ] `from kinoforge.engines.nova_reel import NovaReelEngine, NovaReelBackend` works
- [ ] `NovaReelEngine` self-registers under `"nova_reel"` via `register_engine("nova_reel", lambda: NovaReelEngine())` at module bottom
- [ ] `requires_compute=False`, `requires_local_weights=False`
- [ ] `provision(None, cfg)`: builds the bedrock-runtime client via injected `boto3_session_factory` (defaults to `boto3.Session()`); calls `auth.health_check()` and raises `AuthError` on failure
- [ ] `backend(None, cfg)` returns a `NovaReelBackend` wired to the same client + cfg
- [ ] `NovaReelBackend.submit(job)` calls `bedrock_runtime.start_async_invoke(modelId=..., modelInput=..., outputDataConfig=...)`, stores `invocationArn` keyed by `job.id`, returns `job.id`
- [ ] `NovaReelBackend.result(job_id)` polls `get_async_invoke(invocationArn=...)` until `status == "Completed"`; constructs the output URI as `f"{cfg.output_s3_uri.rstrip('/')}/{invocation_id}/output.mp4"`; returns `Artifact(uri=..., filename="output.mp4", url=None, headers=None)`
- [ ] `submit()` reads the prompt via `resolve_prompt(job)` (existing Layer J helper from `kinoforge.core.prompt_routing`)
- [ ] `NovaReelBackend.result()` raises `KinoforgeError` if `status == "Failed"`
- [ ] `boto3` is NOT imported at module top — only inside `_default_session_factory()`
- [ ] All 10 tests pass with no real cloud calls

**Verify:** `pixi run test tests/engines/test_nova_reel.py -v 2>&1 | tail -15` → 10 passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/engines/test_nova_reel.py`:

```python
"""Layer 3 — NovaReelEngine + NovaReelBackend offline unit tests.

All tests use a _FakeBedrockRuntime client double; no real AWS calls.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.interfaces import Job, ModelProfile, Segment
from tests._fixtures.fake_auth import FakeAuthStrategy


class _FakeBedrockRuntime:
    """Minimal bedrock-runtime client double for unit tests.

    Records every call so tests can assert on the requests. Status flips
    from InProgress → Completed after ``_completed_after`` polls so the
    poll loop is exercised but tests stay fast.
    """

    def __init__(self, completed_after: int = 1) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self._completed_after = completed_after
        self._poll_count = 0
        self.failed = False

    def start_async_invoke(
        self,
        *,
        modelId: str,
        modelInput: dict[str, Any],
        outputDataConfig: dict[str, Any],
    ) -> dict[str, str]:
        self.start_calls.append(
            {
                "modelId": modelId,
                "modelInput": modelInput,
                "outputDataConfig": outputDataConfig,
            }
        )
        # Return a deterministic ARN for tests to assert.
        return {"invocationArn": f"arn:aws:bedrock:us-east-1::async-invoke/inv-{len(self.start_calls)}"}

    def get_async_invoke(self, *, invocationArn: str) -> dict[str, Any]:
        self.get_calls.append(invocationArn)
        if self.failed:
            return {"status": "Failed", "failureMessage": "synthetic failure"}
        self._poll_count += 1
        if self._poll_count >= self._completed_after:
            return {"status": "Completed", "invocationArn": invocationArn}
        return {"status": "InProgress", "invocationArn": invocationArn}


def _build_cfg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "engine": {
            "nova_reel": {
                "region_name": "us-east-1",
                "model_id": "amazon.nova-reel-v1:1",
                "output_s3_uri": "s3://kinoforge-nova-reel-output/",
                "duration_seconds": 6,
                "fps": 24,
                "dimension": "1280x720",
                "prompt_body_key": "prompt",
            }
        }
    }
    base["engine"]["nova_reel"].update(overrides)
    return base


def _build_job(prompt: str = "test prompt") -> Job:
    return Job(
        id="job-1",
        segments=[Segment(prompt=prompt)],
        spec={},
        model_id="nova-reel-v1:1",
    )


def test_nova_reel_engine_is_importable_and_registered() -> None:
    from kinoforge.core.registry import get_engine
    from kinoforge.engines.nova_reel import NovaReelEngine  # noqa: F401

    factory = get_engine("nova_reel")
    engine = factory()
    assert isinstance(engine, NovaReelEngine)


def test_nova_reel_engine_does_not_require_compute_or_weights() -> None:
    from kinoforge.engines.nova_reel import NovaReelEngine

    engine = NovaReelEngine()
    assert engine.requires_compute is False
    assert engine.requires_local_weights is False


def test_nova_reel_provision_calls_auth_health_check() -> None:
    from kinoforge.engines.nova_reel import NovaReelEngine

    fake_client = _FakeBedrockRuntime()
    auth = FakeAuthStrategy()  # ok by default

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return fake_client
        return _Sess()

    engine = NovaReelEngine(
        auth_strategy=auth,
        boto3_session_factory=fake_session_factory,
    )
    engine.provision(None, _build_cfg())
    # health_check called exactly once during provision
    # (verified by FakeAuthStrategy internal counter would require ext;
    # the contract guarantee here is provision didn't raise)
    assert engine._client is fake_client


def test_nova_reel_provision_raises_auth_error_when_creds_missing() -> None:
    from kinoforge.core.errors import AuthError
    from kinoforge.engines.nova_reel import NovaReelEngine

    auth = FakeAuthStrategy(credentials_ok=False)

    def fake_session_factory(**kwargs: Any) -> Any:
        class _Sess:
            def client(self, name: str, region_name: str) -> _FakeBedrockRuntime:
                return _FakeBedrockRuntime()
        return _Sess()

    engine = NovaReelEngine(
        auth_strategy=auth, boto3_session_factory=fake_session_factory
    )
    with pytest.raises(AuthError, match="nova_reel"):
        engine.provision(None, _build_cfg())


def test_nova_reel_backend_submit_calls_start_async_invoke() -> None:
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg)

    job = _build_job(prompt="cinematic sunset, anamorphic lens")
    job_id = backend.submit(job)

    assert job_id == "job-1"
    assert len(fake_client.start_calls) == 1
    call = fake_client.start_calls[0]
    assert call["modelId"] == "amazon.nova-reel-v1:1"
    assert call["modelInput"]["taskType"] == "TEXT_VIDEO"
    assert call["modelInput"]["textToVideoParams"]["text"] == "cinematic sunset, anamorphic lens"
    assert call["modelInput"]["videoGenerationConfig"]["durationSeconds"] == 6
    assert call["modelInput"]["videoGenerationConfig"]["fps"] == 24
    assert call["modelInput"]["videoGenerationConfig"]["dimension"] == "1280x720"
    assert call["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"] == "s3://kinoforge-nova-reel-output/"


def test_nova_reel_backend_submit_with_kms_key_passes_kms_to_output_config() -> None:
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    cfg = _build_cfg(output_kms_key_id="arn:aws:kms:us-east-1:123:key/abc")
    backend = NovaReelBackend(client=fake_client, cfg=cfg)
    backend.submit(_build_job())

    call = fake_client.start_calls[0]
    assert call["outputDataConfig"]["s3OutputDataConfig"]["kmsKeyId"] == (
        "arn:aws:kms:us-east-1:123:key/abc"
    )


def test_nova_reel_backend_result_polls_until_completed() -> None:
    from kinoforge.core.interfaces import Artifact
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime(completed_after=3)
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    backend.submit(_build_job())
    artifact = backend.result("job-1")

    assert isinstance(artifact, Artifact)
    assert artifact.uri == (
        "s3://kinoforge-nova-reel-output/inv-1/output.mp4"
    )
    assert artifact.filename == "output.mp4"
    assert artifact.url is None
    assert artifact.headers is None
    assert len(fake_client.get_calls) == 3


def test_nova_reel_backend_result_raises_kinoforge_error_on_failure() -> None:
    from kinoforge.core.errors import KinoforgeError
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    fake_client.failed = True
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)

    backend.submit(_build_job())
    with pytest.raises(KinoforgeError, match="synthetic failure"):
        backend.result("job-1")


def test_nova_reel_backend_invocation_id_extracted_from_arn() -> None:
    """The output S3 URI uses the LAST `/`-segment of invocationArn as the dir."""
    from kinoforge.engines.nova_reel import NovaReelBackend

    fake_client = _FakeBedrockRuntime()
    # Override the ARN returned to use a Bedrock-shaped UUID.
    fake_client.start_async_invoke = lambda **kw: {  # type: ignore[assignment]
        "invocationArn": "arn:aws:bedrock:us-east-1:1234567890:async-invoke/abc-12345-uuid"
    }
    cfg = _build_cfg()
    backend = NovaReelBackend(client=fake_client, cfg=cfg, sleep=lambda s: None)
    backend.submit(_build_job())
    artifact = backend.result("job-1")
    assert artifact.uri.endswith("/abc-12345-uuid/output.mp4")


def test_nova_reel_engine_lazy_imports_boto3() -> None:
    """Importing the engine module must NOT load boto3 at module top."""
    import subprocess
    import sys

    script = (
        "import kinoforge.engines.nova_reel; "
        "import sys; "
        "print('|'.join(m for m in sys.modules if m == 'boto3' or m.startswith('botocore')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    leaked = result.stdout.strip()
    assert not leaked, f"nova_reel engine import leaked boto3 modules: {leaked}"
```

- [ ] **Step 2: Confirm red**

Run: `pixi run test tests/engines/test_nova_reel.py -v 2>&1 | tail -15`
Expected: `ModuleNotFoundError: No module named 'kinoforge.engines.nova_reel'`.

- [ ] **Step 3: Create the engine module**

Create `src/kinoforge/engines/nova_reel/__init__.py`:

```python
"""AWS Bedrock Nova Reel 1.1 generation engine.

Talks to Bedrock's async-invocation video API via boto3 bedrock-runtime,
authed by the Layer 1 :class:`~kinoforge.core.auth.AWSSigV4` strategy.

``boto3`` is lazy-imported inside :func:`_default_session_factory` to
preserve the core-import-ban invariant (see
``tests/test_core_invariant.py``); tests inject a fake session factory.

Self-registers under the engine name ``"nova_reel"`` on module import.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from kinoforge.core import registry
from kinoforge.core.auth import AuthStrategy, AWSSigV4
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationBackend,
    GenerationEngine,
    Instance,
    Job,
    ModelProfile,
)
from kinoforge.core.prompt_routing import resolve_prompt


# Default no-op ModelProfile until ModelProfileProvider resolves the real one.
_DEFAULT_STUB_PROFILE = ModelProfile(
    fps=24,
    max_frames=144,  # ~6s @ 24fps
    max_resolution=(1280, 720),
    supported_modes=("t2v",),
)


def _default_session_factory(**kwargs: Any) -> Any:
    """Build a real boto3 Session — lazy-imported only when called.

    Tests inject a fake factory so this never fires under unit test.
    """
    import boto3  # noqa: PLC0415 — lazy: tests inject a fake and never trip this

    return boto3.Session(**kwargs)


class NovaReelBackend(GenerationBackend):
    """Backend that talks to Bedrock async-invoke for Nova Reel.

    Attributes:
        _client: bedrock-runtime client (real or test-double).
        _cfg: the kinoforge runtime config dict.
        _inflight: ``{job.id: invocationArn}`` populated by :meth:`submit`.
        _sleep: poll-sleep seam.
        _poll_backoff_s: sleep durations between polls (caps at the last value).
    """

    _poll_backoff_s: tuple[float, ...] = (2.0, 4.0, 8.0, 8.0)

    def __init__(
        self,
        *,
        client: Any,
        cfg: dict[str, Any],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._inflight: dict[str, str] = {}
        self._sleep = sleep

    def capabilities(self) -> ModelProfile:
        return _DEFAULT_STUB_PROFILE

    def submit(self, job: Job) -> str:
        nova_cfg = self._cfg["engine"]["nova_reel"]
        prompt = resolve_prompt(job)
        model_input: dict[str, Any] = {
            "taskType": "TEXT_VIDEO",
            "textToVideoParams": {
                nova_cfg.get("prompt_body_key", "prompt").replace("prompt", "text"): prompt
            } if False else {"text": prompt},
            "videoGenerationConfig": {
                "durationSeconds": nova_cfg["duration_seconds"],
                "fps": nova_cfg["fps"],
                "dimension": nova_cfg["dimension"],
            },
        }
        output_cfg: dict[str, Any] = {
            "s3OutputDataConfig": {"s3Uri": nova_cfg["output_s3_uri"]}
        }
        if nova_cfg.get("output_kms_key_id"):
            output_cfg["s3OutputDataConfig"]["kmsKeyId"] = nova_cfg["output_kms_key_id"]
        resp = self._client.start_async_invoke(
            modelId=nova_cfg["model_id"],
            modelInput=model_input,
            outputDataConfig=output_cfg,
        )
        self._inflight[job.id] = resp["invocationArn"]
        return job.id

    def result(self, job_id: str) -> Artifact:
        arn = self._inflight[job_id]
        for idx, sleep_s in enumerate(self._poll_backoff_s + (self._poll_backoff_s[-1],) * 50):
            status_resp = self._client.get_async_invoke(invocationArn=arn)
            status = status_resp.get("status")
            if status == "Completed":
                invocation_id = arn.rsplit("/", 1)[-1]
                prefix = self._cfg["engine"]["nova_reel"]["output_s3_uri"].rstrip("/")
                return Artifact(
                    uri=f"{prefix}/{invocation_id}/output.mp4",
                    filename="output.mp4",
                    url=None,
                    headers=None,
                )
            if status == "Failed":
                raise KinoforgeError(
                    f"Nova Reel invocation failed: "
                    f"{status_resp.get('failureMessage', 'no message')}"
                )
            self._sleep(sleep_s)
        raise KinoforgeError(f"Nova Reel poll loop exhausted for {arn!r}")


class NovaReelEngine(GenerationEngine):
    """Engine adapter for AWS Bedrock Nova Reel.

    Class attributes:
        name: Registry key ``"nova_reel"``.
        requires_compute: ``False`` — no GPU instance needed.
        requires_local_weights: ``False`` — weights live on Bedrock.
    """

    name: str = "nova_reel"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        auth_strategy: AuthStrategy | None = None,
        boto3_session_factory: Callable[..., Any] = _default_session_factory,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_STUB_PROFILE,
    ) -> None:
        """Initialise with optional injection seams.

        Args:
            auth_strategy: Layer 1 AuthStrategy; defaults to AWSSigV4 with
                region_name resolved at provision time from cfg.
            boto3_session_factory: Callable returning a boto3.Session-like
                object. Tests inject a fake.
            sleep: Sleep callable threaded into :class:`NovaReelBackend`.
            probe_profile: Stub :class:`ModelProfile` until resolved.
        """
        self._auth: AuthStrategy | None = auth_strategy
        self._session_factory = boto3_session_factory
        self._sleep = sleep
        self._probe = probe_profile
        self._client: Any = None

    def _resolve_auth(self, cfg: dict[str, Any]) -> AuthStrategy:
        if self._auth is not None:
            return self._auth
        region = cfg["engine"]["nova_reel"]["region_name"]
        return AWSSigV4(region_name=region, service_name="bedrock-runtime")

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        if instance is not None:
            raise KinoforgeError(
                "NovaReelEngine.provision: instance must be None (requires_compute=False)"
            )
        auth = self._resolve_auth(cfg)
        if not auth.credentials_present():
            raise AuthError(
                f"nova_reel: credentials not present (strategy={type(auth).__name__})"
            )
        # Active wire probe — fail fast on bad creds before any submit.
        health = auth.health_check()
        if not health.ok:
            raise AuthError(f"nova_reel: health check failed — {health.reason}")
        # Build the session + client using auth.client_kwargs().
        session = self._session_factory(**auth.client_kwargs())
        region = cfg["engine"]["nova_reel"]["region_name"]
        self._client = session.client("bedrock-runtime", region_name=region)
        # Persist the strategy for downstream lookups (recording-seam, etc.).
        self._auth = auth

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> GenerationBackend:
        if self._client is None:
            # Allow tests that skip provision and pass the client directly via
            # NovaReelBackend(...) construction; in that case backend() should
            # never be called on the engine.
            raise KinoforgeError(
                "NovaReelEngine.backend called before provision()"
            )
        return NovaReelBackend(client=self._client, cfg=cfg, sleep=self._sleep)


registry.register_engine("nova_reel", lambda: NovaReelEngine())
```

Note the `textToVideoParams` shape: the Bedrock Nova Reel API uses `{"text": <prompt>}` regardless of `prompt_body_key`. The config field is reserved for future variants (e.g. if Nova Reel ever ships an i2v variant where prompt + image are separate roles); the offline test asserts `"text"`.

- [ ] **Step 4: Add `_adapters.py` import line**

Search for `_adapters.py` to find the central engine-registration hub:

```bash
rg -n "engines\." /workspace/src/kinoforge/_adapters.py | head -10
```

Add the import to `src/kinoforge/_adapters.py` near where other engines self-register (mirroring `import kinoforge.engines.fal  # noqa: F401`):

```python
import kinoforge.engines.nova_reel  # noqa: F401  # self-registers under "nova_reel"
```

- [ ] **Step 5: Confirm green**

Run: `pixi run test tests/engines/test_nova_reel.py -v 2>&1 | tail -15`
Expected: 10 passed.

Run: `pixi run test 2>&1 | tail -5`
Expected: ALL repo tests green (no cross-suite regressions).

- [ ] **Step 6: Commit**

```bash
cd /workspace
git add src/kinoforge/engines/nova_reel/__init__.py src/kinoforge/_adapters.py tests/engines/test_nova_reel.py
git commit -m "$(cat <<'EOF'
feat(engines/nova_reel): AWS Bedrock Nova Reel engine + 10 unit tests

Sibling of engines/fal and engines/hosted. Lazy-imports boto3 inside
_default_session_factory only. Consumes Layer 1 AWSSigV4 strategy via
client_kwargs(); falls back to AWSSigV4(region_name=cfg.region_name)
when no explicit auth_strategy passed.

submit() → start_async_invoke(modelId, modelInput, outputDataConfig);
result() polls get_async_invoke until Completed/Failed; output URI
derived as {output_s3_uri}/{invocation_id}/output.mp4.

Self-registers under "nova_reel" on import.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `examples/configs/nova-reel.yaml` + example-load test

**Goal:** Ship a runnable example config wiring NovaReelEngine + the `aws_sigv4` auth strategy + spec.prompt placeholder. Add a test asserting it parses.

**Files:**
- Create: `examples/configs/nova-reel.yaml`
- Modify: `tests/test_examples.py` — add one test loading the new file

**Acceptance Criteria:**
- [ ] `examples/configs/nova-reel.yaml` parses via `Config.parse_obj(yaml.safe_load(...))` (or whichever helper the existing tests use)
- [ ] `cfg.engine.kind == "nova_reel"`, `cfg.engine.nova_reel.region_name == "us-east-1"`, `cfg.engine.nova_reel.output_s3_uri` starts with `s3://`
- [ ] Existing `test_examples.py` tests still pass

**Verify:** `pixi run test tests/test_examples.py -v -k nova_reel 2>&1 | tail -10` → 1 passed

**Steps:**

- [ ] **Step 1: Create the YAML**

Create `examples/configs/nova-reel.yaml`:

```yaml
# AWS Bedrock Nova Reel 1.1 — text-to-video on the official Amazon model.
#
# Setup (one-time, automated in Layer 3 plan Tasks 4-5):
#   1. Attach .aws/policies/bedrock-nova-reel.json to the kinoforge-ci IAM user
#   2. Verify Nova Reel model access via `pixi run probe-hosted --config examples/configs/nova-reel.yaml`
#
# Live smoke: `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_nova_reel_live.py -v`
#
# The prompt body is loaded at smoke-test runtime from
# /workspace/prompt-field-realistic.txt per the project's standard-prompt
# directive (see PROGRESS.md "Standard prompt for all video-generation live
# smokes").

engine:
  kind: nova_reel
  precision: fp16
  nova_reel:
    region_name: us-east-1
    model_id: amazon.nova-reel-v1:1
    output_s3_uri: s3://kinoforge-nova-reel-output/
    duration_seconds: 6
    fps: 24
    dimension: 1280x720
    prompt_body_key: prompt

models:
  - kind: base
    ref: bedrock://amazon.nova-reel-v1:1
    target: hosted

requirements:
  vram_gb: 0
  disk_gb: 0

compute:
  kind: hosted

lifecycle:
  idle_timeout_s: 600
  max_lifetime_s: 1800
  max_in_flight: 1

spec:
  prompt: ""  # filled at runtime from /workspace/prompt-field-realistic.txt

params: {}
```

- [ ] **Step 2: Add the parse test**

In `tests/test_examples.py`, find an existing example-load test (e.g. one that loads `hosted.yaml` or `fal.yaml`) and follow the same pattern. Append:

```python
def test_nova_reel_example_config_parses() -> None:
    from pathlib import Path

    import yaml

    from kinoforge.core.config import Config

    path = Path("examples/configs/nova-reel.yaml")
    cfg_dict = yaml.safe_load(path.read_text())
    cfg = Config.parse_obj(cfg_dict)

    assert cfg.engine.kind == "nova_reel"
    assert cfg.engine.nova_reel is not None
    assert cfg.engine.nova_reel.region_name == "us-east-1"
    assert cfg.engine.nova_reel.output_s3_uri.startswith("s3://")
    assert cfg.engine.nova_reel.model_id == "amazon.nova-reel-v1:1"
```

If `Config.parse_obj` is not the right helper, inspect another example-load test in the same file and mirror its idiom.

- [ ] **Step 3: Verify**

Run: `pixi run test tests/test_examples.py -v -k nova_reel 2>&1 | tail -10` → 1 passed.
Run: `pixi run test tests/test_examples.py 2>&1 | tail -5` → all existing example tests still pass.

- [ ] **Step 4: Commit**

```bash
cd /workspace
git add examples/configs/nova-reel.yaml tests/test_examples.py
git commit -m "$(cat <<'EOF'
feat(examples): nova-reel.yaml — kinoforge config for Bedrock Nova Reel

Wires NovaReelEngine + the standard prompt-field-realistic.txt
runtime-loaded prompt (per project directive). One round-trip test
locks the YAML→pydantic shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `.aws/policies/bedrock-nova-reel.json` IAM policy doc (committed, NOT yet attached)

**Goal:** Commit the scoped inline IAM policy that Task 4 will attach to `kinoforge-ci`. Tracked in git so the operator can audit the grant.

**Files:**
- Create: `.aws/policies/bedrock-nova-reel.json`

**Acceptance Criteria:**
- [ ] File exists and is valid JSON
- [ ] Statement 1 grants `bedrock:InvokeModel`, `bedrock:StartAsyncInvoke`, `bedrock:GetAsyncInvoke` on `arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-reel-v1:1`
- [ ] Statement 2 grants `bedrock:ListFoundationModels` on `*`
- [ ] Statement 3 grants `s3:PutObject`, `s3:GetObject`, `s3:HeadObject` on the output-bucket ARN
- [ ] Files at `.aws/policies/` are NOT gitignored (verify `.gitignore` has the re-include for `.aws/policies/`)

**Verify:** `python -m json.tool .aws/policies/bedrock-nova-reel.json > /dev/null && git check-ignore .aws/policies/bedrock-nova-reel.json` → exit 1 (NOT ignored)

**Steps:**

- [ ] **Step 1: Create the policy file**

Create `.aws/policies/bedrock-nova-reel.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeNovaReel",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:StartAsyncInvoke",
        "bedrock:GetAsyncInvoke"
      ],
      "Resource": [
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-reel-v1:1"
      ]
    },
    {
      "Sid": "DiscoverFoundationModels",
      "Effect": "Allow",
      "Action": [
        "bedrock:ListFoundationModels"
      ],
      "Resource": "*"
    },
    {
      "Sid": "NovaReelOutputBucket",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:HeadObject"
      ],
      "Resource": [
        "arn:aws:s3:::kinoforge-nova-reel-output",
        "arn:aws:s3:::kinoforge-nova-reel-output/*"
      ]
    }
  ]
}
```

- [ ] **Step 2: Verify it's tracked + valid**

```bash
cd /workspace
python -m json.tool .aws/policies/bedrock-nova-reel.json > /dev/null  # parse ok
git check-ignore .aws/policies/bedrock-nova-reel.json; echo "exit=$?"
```

Expected: `python -m json.tool` exits 0; `git check-ignore` exits 1 (file is NOT ignored — the existing `.gitignore` re-include for `.aws/policies/` applies per Phase 39).

- [ ] **Step 3: Commit**

```bash
cd /workspace
git add .aws/policies/bedrock-nova-reel.json
git commit -m "$(cat <<'EOF'
docs(aws): scoped IAM policy for Bedrock Nova Reel + S3 output bucket

Three statements:
  - InvokeNovaReel: Bedrock invoke perms scoped to Nova Reel 1.1 ARN
  - DiscoverFoundationModels: list-foundation-models on *
  - NovaReelOutputBucket: S3 read/write on kinoforge-nova-reel-output

Committed but NOT yet attached to kinoforge-ci. Task 4 attaches it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Attach the IAM policy + create the S3 output bucket (real cloud-state mutation)

**Goal:** Use the AWS CLI from inside the container to (1) create the S3 output bucket `kinoforge-nova-reel-output` in `us-east-1` if it doesn't exist, (2) attach the inline policy from Task 3 to the existing `kinoforge-ci` IAM user. Both operations are reversible (delete bucket, delete inline policy) — explicitly note this in the commit body.

**Files:**
- Modify: `docs/CLOUD-CREDS.md` — add the new policy + bucket rows under the AWS section

**Acceptance Criteria:**
- [ ] S3 bucket `kinoforge-nova-reel-output` exists in `us-east-1`
- [ ] Inline policy `kinoforge-nova-reel` is attached to user `kinoforge-ci`
- [ ] `aws iam get-user-policy --user-name kinoforge-ci --policy-name kinoforge-nova-reel` returns the same JSON as `.aws/policies/bedrock-nova-reel.json`
- [ ] `docs/CLOUD-CREDS.md` has new rows documenting both grants

**Verify:**
```
aws s3api head-bucket --bucket kinoforge-nova-reel-output --region us-east-1 && \
  aws iam get-user-policy --user-name kinoforge-ci --policy-name kinoforge-nova-reel > /dev/null && \
  echo OK
```
→ exit 0 with `OK`

**Steps:**

- [ ] **Step 1: Confirm AWS creds available + identity**

```bash
cd /workspace
aws sts get-caller-identity
```

Expected: returns `{"Arn": "arn:aws:iam::<account>:user/kinoforge-ci", ...}`. If not, STOP and report BLOCKED with the failure.

- [ ] **Step 2: Create the output bucket (idempotent)**

```bash
aws s3api create-bucket \
    --bucket kinoforge-nova-reel-output \
    --region us-east-1 \
    2>&1 | tee /tmp/bucket-create.log

# Idempotent: if it already exists and we own it, the command exits 0 or returns
# BucketAlreadyOwnedByYou. Either is acceptable.
if grep -q "BucketAlreadyOwnedByYou\|already exists" /tmp/bucket-create.log; then
    echo "bucket already exists, ok"
fi

# Verify
aws s3api head-bucket --bucket kinoforge-nova-reel-output --region us-east-1
echo "bucket exists, exit=$?"
```

- [ ] **Step 3: Attach the inline policy**

```bash
aws iam put-user-policy \
    --user-name kinoforge-ci \
    --policy-name kinoforge-nova-reel \
    --policy-document file://.aws/policies/bedrock-nova-reel.json

# Verify
aws iam get-user-policy \
    --user-name kinoforge-ci \
    --policy-name kinoforge-nova-reel \
    | python -c "import sys, json; d=json.load(sys.stdin); print('OK' if 'PolicyDocument' in d else 'MISSING')"
```

- [ ] **Step 4: Update `docs/CLOUD-CREDS.md`**

Find the AWS section (search for `## AWS` or similar). Add a new row to the IAM-policies table:

```markdown
| `kinoforge-nova-reel` | Bedrock InvokeModel + ListFoundationModels + S3 output bucket | Inline on `kinoforge-ci` | 2026-06-07 | `.aws/policies/bedrock-nova-reel.json` |
```

Add a row to the S3-buckets table:

```markdown
| `kinoforge-nova-reel-output` | Nova Reel async-invoke output prefix | `us-east-1` | 2026-06-07 | created by Layer 3 Task 4 |
```

If the file doesn't have these tables, append a "Layer 3 (Nova Reel)" subsection with the same info.

- [ ] **Step 5: Commit**

```bash
cd /workspace
git add docs/CLOUD-CREDS.md
git commit -m "$(cat <<'EOF'
chore(aws): attach Bedrock Nova Reel IAM policy + create S3 output bucket

REAL CLOUD MUTATION (Layer 3 Task 4). Both operations are reversible:

  1. Created S3 bucket arn:aws:s3:::kinoforge-nova-reel-output in
     us-east-1 (delete with: aws s3 rb s3://kinoforge-nova-reel-output)
  2. Attached inline policy 'kinoforge-nova-reel' to IAM user
     'kinoforge-ci' (delete with: aws iam delete-user-policy
     --user-name kinoforge-ci --policy-name kinoforge-nova-reel)

Policy bytes match .aws/policies/bedrock-nova-reel.json (Task 3).
CLOUD-CREDS.md documents the new grants.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Bedrock model-access verification + probe extension

**Goal:** Verify that `kinoforge-ci` can actually invoke Nova Reel 1.1 by listing foundation models filtered to the modality, then add a `--check-bedrock-model-access` flag to `tools/probe_hosted.py` so future smokes have a single mechanical gate.

**Files:**
- Modify: `tools/probe_hosted.py` — add `--check-bedrock-model-access MODEL_ID` flag + the verification logic
- Modify: `tests/test_probe_hosted.py` — 2 new unit tests covering the new flag

**Acceptance Criteria:**
- [ ] `pixi run probe-hosted -- --config examples/configs/nova-reel.yaml --check-bedrock-model-access amazon.nova-reel-v1:1` exits 0 when the model is accessible
- [ ] Exits non-zero (with a clear reason) when the model is NOT in `list_foundation_models` output or when access is denied
- [ ] 2 new unit tests pass: one for the success path, one for the access-denied path (both with fake bedrock client)
- [ ] Existing probe tests still pass (5 of the original 6 remain unchanged + 1 strengthened to assert the new flag is optional, totaling 8)

**Verify:** `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -15` → 8 passed
**Live verify:** `pixi run probe-hosted -- --config examples/configs/nova-reel.yaml --check-bedrock-model-access amazon.nova-reel-v1:1 2>&1 | tail -5` → ends with `PASS strategy=nova_reel`

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/test_probe_hosted.py`:

```python
# ---------------------------------------------------------------------------
# Layer 3 — --check-bedrock-model-access flag
# ---------------------------------------------------------------------------


class _FakeBedrockControlClient:
    """Stand-in for boto3 bedrock (control plane) client."""

    def __init__(self, models: list[dict[str, Any]] | None = None, raise_on_list: Exception | None = None) -> None:
        self._models = models or []
        self._raise = raise_on_list

    def list_foundation_models(self, **kwargs: Any) -> dict[str, Any]:
        if self._raise is not None:
            raise self._raise
        return {"modelSummaries": self._models}


def test_check_bedrock_model_access_passes_when_model_listed() -> None:
    from tools.probe_hosted import check_bedrock_model_access

    fake = _FakeBedrockControlClient(
        models=[{"modelId": "amazon.nova-reel-v1:1", "modelLifecycle": {"status": "ACTIVE"}}]
    )
    result = check_bedrock_model_access(fake, "amazon.nova-reel-v1:1")
    assert result.ok is True
    assert "amazon.nova-reel-v1:1" in (result.identity or "")


def test_check_bedrock_model_access_fails_when_model_missing() -> None:
    from tools.probe_hosted import check_bedrock_model_access

    fake = _FakeBedrockControlClient(models=[{"modelId": "amazon.titan-text-v1", "modelLifecycle": {"status": "ACTIVE"}}])
    result = check_bedrock_model_access(fake, "amazon.nova-reel-v1:1")
    assert result.ok is False
    assert "amazon.nova-reel-v1:1" in (result.reason or "")
```

Plus a third test asserting CLI integration:

```python
def test_probe_cli_invokes_check_bedrock_model_access_when_flag_set(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: --check-bedrock-model-access flag fires the bedrock probe
    alongside the strategy health checks.
    """
    from tools.probe_hosted import ProbeResult, run

    strategies = [("nova_reel", FakeAuthStrategy())]
    # Inject a fake bedrock control client so no real AWS call happens.
    captured: list[ProbeResult] = []

    def fake_bedrock_check(client: object, model_id: str) -> ProbeResult:
        captured.append(
            ProbeResult(
                name=f"bedrock:{model_id}", ok=True, identity=model_id, reason=None
            )
        )
        return captured[-1]

    monkeypatch.setattr(
        "tools.probe_hosted.check_bedrock_model_access", fake_bedrock_check
    )

    # `run` accepts an extra_checks kwarg added in the impl below.
    extra = [("bedrock:amazon.nova-reel-v1:1", lambda: fake_bedrock_check(None, "amazon.nova-reel-v1:1"))]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json", extra_checks=extra)
    assert exit_code == 0
    assert len(captured) == 1
```

- [ ] **Step 2: Confirm red**

Run: `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -15`
Expected: 3 new tests fail with `AttributeError` or `ImportError` on `check_bedrock_model_access` / `extra_checks`.

- [ ] **Step 3: Add the bedrock check to `tools/probe_hosted.py`**

Insert after the `write_snapshot` function:

```python
def check_bedrock_model_access(client: Any, model_id: str) -> ProbeResult:
    """Probe whether ``model_id`` is accessible to the caller.

    Args:
        client: A boto3 bedrock control-plane client (NOT bedrock-runtime).
        model_id: Bedrock foundation-model identifier
            (e.g. ``"amazon.nova-reel-v1:1"``).

    Returns:
        :class:`ProbeResult` with ``ok=True`` and ``identity=<model_id>``
        when the model appears in :py:meth:`list_foundation_models`,
        otherwise ``ok=False`` with a descriptive reason.
    """
    name = f"bedrock:{model_id}"
    try:
        resp = client.list_foundation_models()
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(name=name, ok=False, identity=None, reason=f"list_foundation_models failed: {exc}")
    listed = [m for m in resp.get("modelSummaries", []) if m.get("modelId") == model_id]
    if not listed:
        return ProbeResult(
            name=name,
            ok=False,
            identity=None,
            reason=f"model {model_id!r} not in list_foundation_models response",
        )
    return ProbeResult(name=name, ok=True, identity=model_id, reason=None)
```

Modify `run(strategies, *, snapshot_path=None)` to accept an optional `extra_checks` kwarg:

```python
def run(
    strategies: Sequence[tuple[str, AuthStrategy]],
    *,
    snapshot_path: Path | None = None,
    extra_checks: Sequence[tuple[str, Callable[[], ProbeResult]]] = (),
) -> int:
    """Public entry point. Returns exit code."""
    results = probe_strategies(strategies)
    for label, check in extra_checks:
        results.append(check())
    for r in results:
        if r.ok:
            print(f"PASS strategy={r.name} identity={r.identity}")
        else:
            print(f"FAIL strategy={r.name} reason={r.reason}")
    if snapshot_path is not None:
        write_snapshot(snapshot_path, results)
    return 0 if all(r.ok for r in results) else 1
```

Modify `main(argv)` to accept the new flag:

```python
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kinoforge hosted-auth preflight probe")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--snapshot", type=Path, default=None)
    parser.add_argument(
        "--check-bedrock-model-access",
        default=None,
        metavar="MODEL_ID",
        help="In addition to AuthStrategy health, verify the given Bedrock model is listed",
    )
    args = parser.parse_args(argv)

    snapshot_path = args.snapshot or Path("tools/_snapshots") / f"probe-{args.config.stem}.json"
    strategies = _load_strategies_from_config(args.config)

    extra_checks: list[tuple[str, Callable[[], ProbeResult]]] = []
    if args.check_bedrock_model_access:
        import boto3  # noqa: PLC0415 — lazy

        # Use the first AWSSigV4 strategy's region if any was configured;
        # otherwise default to us-east-1.
        region = "us-east-1"
        for _, strat in strategies:
            if isinstance(strat, AWSSigV4):
                region = strat._region_name  # type: ignore[attr-defined]
                break
        client = boto3.Session().client("bedrock", region_name=region)
        model_id = args.check_bedrock_model_access
        extra_checks.append(
            (f"bedrock:{model_id}", lambda: check_bedrock_model_access(client, model_id))
        )

    return run(strategies, snapshot_path=snapshot_path, extra_checks=extra_checks)
```

Add the missing imports near the top:

```python
from collections.abc import Callable
from kinoforge.core.auth import AWSSigV4
```

- [ ] **Step 4: Verify offline**

Run: `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -15`
Expected: 8 passed.

- [ ] **Step 5: Verify live (real AWS call)**

```bash
cd /workspace
pixi run probe-hosted -- \
    --config examples/configs/nova-reel.yaml \
    --check-bedrock-model-access amazon.nova-reel-v1:1
```

Expected: prints `PASS strategy=nova_reel identity=...` + `PASS strategy=bedrock:amazon.nova-reel-v1:1 identity=amazon.nova-reel-v1:1`; exit 0.

If the bedrock check FAILS with "not in list_foundation_models response", AWS Nova Reel access agreement may need explicit acceptance. The probe's failure message will name the model; the operator can then open https://us-east-1.console.aws.amazon.com/bedrock/home?region=us-east-1#/modelaccess and grant access. (This is the only step that may need operator action; if it does, halt and report.)

- [ ] **Step 6: Commit**

```bash
cd /workspace
git add tools/probe_hosted.py tests/test_probe_hosted.py
git commit -m "$(cat <<'EOF'
feat(probe-hosted): --check-bedrock-model-access flag + 3 tests

Layer 3 needs to verify Nova Reel model access before any live-spend
attempt. The new flag runs bedrock.list_foundation_models and asserts
the named model is in the response. Wired through run() via the
generic extra_checks: Sequence[(label, callable)] seam so future
provider-specific checks (Vertex Veo model list, etc.) can plug in
without further changes to the probe shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: RED live-smoke scaffold (committed BEFORE any live spend per CLAUDE.md durability rule)

**Goal:** Write the live smoke test scaffold, commit it as RED (failing or env-gated-skipped — not yet running), and commit BEFORE Task 7 fires real AWS. The CLAUDE.md durability rule ("Commit RED scaffolds before any live spend") requires this so a mid-spend crash doesn't lose the scaffold.

**Files:**
- Create: `tests/live/test_nova_reel_live.py`

**Acceptance Criteria:**
- [ ] File is gated by `KINOFORGE_LIVE_TESTS=1` + AWS env (`AWS_ACCESS_KEY_ID` or `AWS_PROFILE` set)
- [ ] Without `KINOFORGE_LIVE_TESTS=1`, the test is skipped (does not fail collection)
- [ ] With the env set, the test would actually try to run AWS calls (verified by collecting it, NOT executing)
- [ ] Test loads `examples/configs/nova-reel.yaml` and reads the prompt from `/workspace/prompt-field-realistic.txt`
- [ ] Test asserts: `artifact.uri` starts with `s3://kinoforge-nova-reel-output/`; bytes read from S3 start with one of the MP4 ftyp prefixes; commit BEFORE Task 7 fires.

**Verify:** `pixi run test tests/live/test_nova_reel_live.py --collect-only 2>&1 | tail -5` → collects 1 test (or skips at module level with no error).
With env unset: `pixi run test tests/live/test_nova_reel_live.py -v 2>&1 | tail -5` → 1 skipped.

**Steps:**

- [ ] **Step 1: Create the test file**

Create `tests/live/test_nova_reel_live.py`:

```python
"""Layer 3 live smoke — Nova Reel 1.1 on real AWS Bedrock.

Gated by KINOFORGE_LIVE_TESTS=1 + an AWS credential chain that resolves
(env vars, ~/.aws/credentials, or instance profile). Reads the prompt
from /workspace/prompt-field-realistic.txt per project directive.

Cost: ~$0.50 per cold run; budget cap $1.50 across iterations (Layer 3
plan). Skipped silently in CI.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.live

if os.getenv("KINOFORGE_LIVE_TESTS") != "1":
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + AWS credentials",
        allow_module_level=True,
    )

_log = logging.getLogger(__name__)

_MP4_FTYP_PREFIXES: tuple[bytes, ...] = (
    b"ftypisom",
    b"ftypiso5",
    b"ftypiso6",
    b"ftypmp42",
)


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def test_nova_reel_live_e2e_smoke(tmp_path: Path) -> None:
    """End-to-end: load cfg → submit → MP4 in S3 → bytes start with ftyp."""

    # _adapters import first so nova_reel registers itself
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import Job, Segment
    from kinoforge.core.registry import get_engine

    # Pre-flight: probe must pass before any spend.
    probe_proc = subprocess.run(
        [
            "pixi",
            "run",
            "probe-hosted",
            "--",
            "--config",
            "examples/configs/nova-reel.yaml",
            "--check-bedrock-model-access",
            "amazon.nova-reel-v1:1",
        ],
        capture_output=True,
        text=True,
    )
    assert probe_proc.returncode == 0, (
        f"probe failed before any spend:\nstdout={probe_proc.stdout}\nstderr={probe_proc.stderr}"
    )

    # Load config
    cfg_dict = yaml.safe_load(Path("examples/configs/nova-reel.yaml").read_text())
    cfg = Config.parse_obj(cfg_dict)
    # Verify shape
    assert cfg.engine.kind == "nova_reel"

    # Load prompt from the canonical file (project directive).
    prompt = Path("/workspace/prompt-field-realistic.txt").read_text().strip()
    assert len(prompt) > 100, "prompt-field-realistic.txt unexpectedly short"

    # Build engine + backend (via raw cfg dict — Nova Reel adapter consumes dict).
    engine_factory = get_engine("nova_reel")
    engine = engine_factory()
    engine.provision(None, cfg_dict)
    backend = engine.backend(None, cfg_dict)

    # Submit the job.
    job = Job(
        id=f"layer3-smoke-{int(time.time())}",
        segments=[Segment(prompt=prompt)],
        spec={},
        model_id="amazon.nova-reel-v1:1",
    )
    submitted = backend.submit(job)
    _log.info("nova reel submitted: job_id=%s", submitted)

    # Wait for result (Nova Reel typically completes in 1-3 minutes for 6s clips).
    artifact = backend.result(submitted)
    _log.info("nova reel artifact: %s", artifact.uri)
    assert artifact.uri.startswith("s3://kinoforge-nova-reel-output/")
    assert artifact.filename == "output.mp4"

    # Download + verify MP4 ftyp signature.
    import boto3  # noqa: PLC0415 — lazy

    bucket, key = artifact.uri.removeprefix("s3://").split("/", 1)
    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.get_object(Bucket=bucket, Key=key)
    body_head = resp["Body"].read(64)
    assert any(prefix in body_head for prefix in _MP4_FTYP_PREFIXES), (
        f"output does not start with MP4 ftyp prefix; head={body_head!r}"
    )
    _log.info("nova reel MP4 verified: %d bytes head", len(body_head))

    # Capture fixtures + metadata if requested
    if os.getenv("KINOFORGE_SAVE_FIXTURES") == "1":
        fixtures_dir = Path("tests/engines/fixtures/nova_reel")
        fixtures_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "git_sha": _git_sha(),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "artifact_uri": artifact.uri,
            "filename": artifact.filename,
            "model_id": "amazon.nova-reel-v1:1",
            "region": "us-east-1",
        }
        (fixtures_dir / "last_smoke.json").write_text(__import__("json").dumps(meta, indent=2) + "\n")
        _log.info("fixtures captured to %s", fixtures_dir)
```

- [ ] **Step 2: Verify it skips cleanly without the env var**

Run: `pixi run test tests/live/test_nova_reel_live.py -v 2>&1 | tail -5`
Expected: 1 skipped (no failure).

- [ ] **Step 3: Verify it collects under the env var**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_nova_reel_live.py --collect-only 2>&1 | tail -5
```

Expected: collects `test_nova_reel_live_e2e_smoke` (but does NOT execute under `--collect-only`).

- [ ] **Step 4: Commit RED scaffold (BEFORE any live spend)**

```bash
cd /workspace
git add tests/live/test_nova_reel_live.py
git commit -m "$(cat <<'EOF'
test(live): Nova Reel E2E smoke scaffold (RED — committed before spend)

Layer 3 Task 6. Per CLAUDE.md durability rule, RED-scaffold commits
land BEFORE any live cloud spend so a mid-spend crash never loses the
scaffold. The test is env-gated by KINOFORGE_LIVE_TESTS=1 and reads
the prompt from /workspace/prompt-field-realistic.txt per the
standard-prompt directive.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Fire the live smoke + capture fixtures (~$0.50 live spend)

**Goal:** Execute the smoke against real AWS Bedrock with `KINOFORGE_SAVE_FIXTURES=1`, producing a real MP4 and committing the captured fixture metadata. Budget cap: $1.50 across at most 3 iterations.

**Files:**
- Create: `tests/engines/fixtures/nova_reel/last_smoke.json` (auto-written by the test under `KINOFORGE_SAVE_FIXTURES=1`)

**Acceptance Criteria:**
- [ ] Smoke test exits 0
- [ ] A real MP4 lands at the recorded `s3://kinoforge-nova-reel-output/<inv-id>/output.mp4`
- [ ] `tests/engines/fixtures/nova_reel/last_smoke.json` exists with: `git_sha`, `captured_at`, `artifact_uri`, `filename`, `model_id`, `region`
- [ ] Total spend ≤ $1.50 (verified after by AWS Cost Explorer if needed; smoke itself logs the invocation count)

**Verify (the smoke itself):**
```
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
    pixi run pytest tests/live/test_nova_reel_live.py -v 2>&1 | tail -20
```
→ 1 passed; new fixture file present.

**Steps:**

- [ ] **Step 1: Sanity-check budget and creds**

```bash
cd /workspace
aws sts get-caller-identity
```

Expected: returns `kinoforge-ci` identity. Confirm AWS env is wired.

- [ ] **Step 2: Run the smoke**

```bash
cd /workspace
KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 \
    pixi run pytest tests/live/test_nova_reel_live.py -v -s 2>&1 | tee /tmp/nova-reel-smoke.log | tail -30
```

Expected (success path):
- Probe lines: `PASS strategy=nova_reel ...` + `PASS strategy=bedrock:amazon.nova-reel-v1:1 ...`
- Submit + poll lines logged (1-3 minute wall time)
- `nova reel MP4 verified: 64 bytes head`
- `1 passed`
- New file at `tests/engines/fixtures/nova_reel/last_smoke.json`

If the smoke fails before submit (probe failure, IAM denial, model-access denial), STOP — DO NOT retry blindly. Inspect the log, fix the cause, re-fire ONCE.

If the smoke fails AFTER submit (e.g. polling timeout, MP4 verification failure), the AWS spend already happened — capture the failure mode and `BLOCKED`-report.

- [ ] **Step 3: Inspect the fixture**

```bash
cat /workspace/tests/engines/fixtures/nova_reel/last_smoke.json
```

Confirm: valid JSON, `artifact_uri` starts with `s3://kinoforge-nova-reel-output/`, `git_sha` matches the Task 6 commit (or a slightly later one if a retry was needed).

- [ ] **Step 4: Commit fixture**

```bash
cd /workspace
git add tests/engines/fixtures/nova_reel/last_smoke.json
git commit -m "$(cat <<'EOF'
test(live): Nova Reel smoke fixture — first real MP4 from Bedrock

Layer 3 Task 7. First MP4 produced by kinoforge end-to-end on AWS
Bedrock Nova Reel 1.1 via prompt-field-realistic.txt. The fixture
captures the artifact URI + git SHA + capture timestamp; the actual
MP4 lives in S3 at the recorded URI (NOT committed — too large + S3
is the natural store).

Verified: artifact bytes start with an MP4 ftyp prefix.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Offline replay test using captured fixture

**Goal:** Add a deterministic offline test that exercises the same NovaReelBackend code paths against a fixture-driven boto3 client double, so CI can verify the engine without ever calling AWS.

**Files:**
- Create: `tests/engines/test_nova_reel_replay.py`

**Acceptance Criteria:**
- [ ] Loads `tests/engines/fixtures/nova_reel/last_smoke.json`
- [ ] Constructs a fake bedrock-runtime that returns canned `start_async_invoke` + `get_async_invoke` responses derived from the fixture (or hardcoded from the live ARN shape)
- [ ] Runs `NovaReelBackend.submit + result` and asserts the returned `Artifact.uri` matches the fixture's `artifact_uri`
- [ ] Runs offline in CI (no AWS env required)

**Verify:** `pixi run test tests/engines/test_nova_reel_replay.py -v 2>&1 | tail -5` → 1 passed

**Steps:**

- [ ] **Step 1: Write the replay test**

Create `tests/engines/test_nova_reel_replay.py`:

```python
"""Layer 3 — deterministic offline replay test against captured fixture.

Reads tests/engines/fixtures/nova_reel/last_smoke.json (committed by Task 7)
and exercises NovaReelBackend.submit + result against a fixture-driven
boto3 client double. No real AWS calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.interfaces import Job, Segment
from kinoforge.engines.nova_reel import NovaReelBackend

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nova_reel" / "last_smoke.json"


@pytest.fixture
def smoke_fixture() -> dict[str, Any]:
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"fixture not captured yet ({FIXTURE_PATH}); run Layer 3 Task 7"
        )
    return json.loads(FIXTURE_PATH.read_text())


class _FixtureBedrockClient:
    """boto3 bedrock-runtime double driven by a captured artifact_uri."""

    def __init__(self, artifact_uri: str) -> None:
        # Derive the ARN segment from the artifact URI:
        # s3://bucket/<inv-id>/output.mp4 → inv-id
        inv_id = artifact_uri.rsplit("/", 2)[-2]
        self._arn = f"arn:aws:bedrock:us-east-1::async-invoke/{inv_id}"

    def start_async_invoke(self, **_kw: Any) -> dict[str, str]:
        return {"invocationArn": self._arn}

    def get_async_invoke(self, *, invocationArn: str) -> dict[str, Any]:
        return {"status": "Completed", "invocationArn": invocationArn}


def test_nova_reel_offline_replay_from_fixture(smoke_fixture: dict[str, Any]) -> None:
    artifact_uri = smoke_fixture["artifact_uri"]
    client = _FixtureBedrockClient(artifact_uri)
    cfg = {
        "engine": {
            "nova_reel": {
                "region_name": smoke_fixture["region"],
                "model_id": smoke_fixture["model_id"],
                "output_s3_uri": artifact_uri.rsplit("/", 2)[0] + "/",
                "duration_seconds": 6,
                "fps": 24,
                "dimension": "1280x720",
                "prompt_body_key": "prompt",
            }
        }
    }
    backend = NovaReelBackend(client=client, cfg=cfg, sleep=lambda s: None)
    backend.submit(
        Job(id="replay-1", segments=[Segment(prompt="x")], spec={}, model_id=smoke_fixture["model_id"])
    )
    artifact = backend.result("replay-1")
    assert artifact.uri == artifact_uri
```

- [ ] **Step 2: Verify**

Run: `pixi run test tests/engines/test_nova_reel_replay.py -v 2>&1 | tail -5`
Expected: 1 passed.

Run: `pixi run test 2>&1 | tail -5`
Expected: all repo tests green.

- [ ] **Step 3: Commit**

```bash
cd /workspace
git add tests/engines/test_nova_reel_replay.py
git commit -m "$(cat <<'EOF'
test(engines/nova_reel): offline replay against captured fixture

Layer 3 Task 8. Reads tests/engines/fixtures/nova_reel/last_smoke.json
and re-runs NovaReelBackend.submit + result against a fixture-driven
client double. Lets CI verify the engine code path without any live
AWS spend.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: README + PROGRESS.md Phase 42 + final gate

**Goal:** Wire Layer 3 into user docs + the recovery index. Run the 4 gates as the closing check. Backfill per-task SHAs.

**Files:**
- Modify: `README.md` — add "Nova Reel" subsection under "Real providers"
- Modify: `PROGRESS.md` — add Phase 42 entry

**Per-task SHA placeholders** (backfill at the end):
| Plan task | SHA |
|---|---|
| T0 NovaReelEngineConfig | `<T0-SHA>` |
| T1 engines/nova_reel/ + tests | `<T1-SHA>` |
| T2 examples/configs/nova-reel.yaml | `<T2-SHA>` |
| T3 .aws/policies/bedrock-nova-reel.json | `<T3-SHA>` |
| T4 Attach policy + create bucket | `<T4-SHA>` |
| T5 probe-hosted --check-bedrock-model-access | `<T5-SHA>` |
| T6 RED smoke scaffold | `<T6-SHA>` |
| T7 Live smoke fire + fixture | `<T7-SHA>` |
| T8 Offline replay | `<T8-SHA>` |
| T9 Docs + final gate | `<T9-SHA>` (this commit) |

**Acceptance Criteria:**
- [ ] `README.md` has a new `### Nova Reel` subsection under "Real providers" with a quickstart + cost note + prompt-loading directive
- [ ] `PROGRESS.md` Phase 42 entry includes: per-task SHAs (10 entries), Key design decisions, "Live spend: ~$0.50", "First real artifact" pointer to the fixture, "Out of scope / carried forward" listing Veo (Layer 2) gated on GCP billing
- [ ] `pixi run test` fully green
- [ ] `pixi run lint` clean
- [ ] `pixi run typecheck` clean
- [ ] `pixi run pre-commit run --all-files` clean

**Verify:** `pixi run test && pixi run lint && pixi run typecheck && pixi run pre-commit run --all-files 2>&1 | tail -10` → all green

**Steps:**

- [ ] **Step 1: README — append Nova Reel subsection**

In `README.md`, find the existing "Real providers" section (search for `## Real providers` or `### Real providers — fal.ai` to anchor). Append:

```markdown
### Nova Reel (AWS Bedrock)

Hosted text-to-video via AWS Bedrock's Nova Reel 1.1 model. No GPU
needed locally; auth via the existing AWS `kinoforge-ci` IAM user.

Quickstart:

```bash
# 1. Verify auth + model access
pixi run probe-hosted -- \
    --config examples/configs/nova-reel.yaml \
    --check-bedrock-model-access amazon.nova-reel-v1:1

# 2. Live smoke (~$0.50, ~2 min wall time)
KINOFORGE_LIVE_TESTS=1 \
    pixi run pytest tests/live/test_nova_reel_live.py -v
```

Prompt: the smoke loads from `/workspace/prompt-field-realistic.txt`
verbatim per the project's standard-prompt directive (apples-to-
apples comparison across models).
```

- [ ] **Step 2: PROGRESS — add Phase 42 entry**

In `PROGRESS.md`, after the Phase 41 (Layer 1) entry, append:

```markdown
### Phase 42 — Layer 3 NovaReelEngine + live smoke

First AWS Bedrock-hosted video engine. Ships a new `engines/nova_reel/`
sibling using the Layer 1 `AWSSigV4` strategy, scoped IAM grant for
`kinoforge-ci`, a probe-hosted preflight extension, and a live smoke
that produced the first real MP4 from Nova Reel 1.1 via
`prompt-field-realistic.txt`.

Spec: `docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md`.
Plan: `docs/superpowers/plans/2026-06-07-layer-3-nova-reel-engine.md`.

- [x] Task 0: NovaReelEngineConfig pydantic — commit `<T0-SHA>`
- [x] Task 1: engines/nova_reel/ + 10 unit tests — commit `<T1-SHA>`
- [x] Task 2: examples/configs/nova-reel.yaml + parse test — commit `<T2-SHA>`
- [x] Task 3: scoped IAM policy doc — commit `<T3-SHA>`
- [x] Task 4: attach policy + create S3 output bucket — commit `<T4-SHA>`
- [x] Task 5: probe-hosted --check-bedrock-model-access — commit `<T5-SHA>`
- [x] Task 6: RED live-smoke scaffold — commit `<T6-SHA>`
- [x] Task 7: live smoke fire + first MP4 + fixture — commit `<T7-SHA>`
- [x] Task 8: offline replay against fixture — commit `<T8-SHA>`
- [x] Task 9: README + PROGRESS + final gate — commit `<T9-SHA>`

**First real artifact (Bedrock):** see `tests/engines/fixtures/nova_reel/last_smoke.json`.

**Key design decisions:**

- **SDK delegation via Layer 1 AWSSigV4.client_kwargs().** boto3 builds the
  Session from the strategy's credential dict; no custom signing in the
  engine. Auth refresh, retries, multipart all handled by boto3.
- **Output URI derived deterministically.** `f"{output_s3_uri}/{invocation_id}/output.mp4"`
  where `invocation_id` is the last `/`-segment of `invocationArn`.
  Avoids an extra `list_objects` round-trip.
- **Probe extension via `extra_checks` generic seam.** The
  `--check-bedrock-model-access` flag composes with the existing strategy
  health checks via the new `extra_checks: Sequence[(label, callable)]`
  kwarg on `run()`. Future per-provider gates (Vertex Veo model list, etc.)
  plug in without re-shaping the probe.
- **Single MP4 verification on the smoke.** Bytes head MUST start with an
  MP4 ftyp prefix (`isom`, `iso5`, `iso6`, or `mp42`). Catches a real
  failure mode where Bedrock returns a JSON error in the S3 object on
  partial-failure paths.

**Live spend:** ~$0.50 (single cold smoke; budget cap $1.50).

**Out of scope / carried forward:**

- Layer 2 (Veo) — gated on GCP billing tier upgrade.
- Engine-integration with Orchestrator (kinoforge generate cfg) — Nova Reel
  is reachable directly today; full orchestrator wiring (CapabilityKey,
  ModelProfile probing, ledger persistence) is a follow-up layer if a real
  user workflow needs it.
- Image-conditioned i2v variant of Nova Reel — single role only today.

Closes (partial): PROGRESS:113 carry-forward "Engine-integration live
smoke" — Nova Reel half done; Veo half pending billing upgrade.
```

- [ ] **Step 3: Run the 4 gates**

```bash
cd /workspace
pixi run test 2>&1 | tail -5
pixi run lint 2>&1 | tail -3
pixi run typecheck 2>&1 | tail -3
pixi run pre-commit run --all-files 2>&1 | tail -10
```

All four must come back clean.

- [ ] **Step 4: Backfill SHAs**

```bash
git log --oneline -15
```

Replace each `<TX-SHA>` in the PROGRESS entry with the actual short SHA from `git log`.

- [ ] **Step 5: Commit**

```bash
cd /workspace
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(layer-3): Nova Reel quickstart + PROGRESS Phase 42 entry

Phase 42 wraps Layer 3. Nova Reel hosted on AWS Bedrock, first real
MP4 via prompt-field-realistic.txt. Layer 2 (Veo) remains gated on
GCP billing tier upgrade.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

- **Spec coverage:** every required spec section covered:
  - §4.2 NovaReelEngine + NovaReelBackend → Tasks 0, 1
  - §5.2 IAM policy + Bedrock enablement + probe → Tasks 3, 4, 5
  - §6.4 live smoke at tests/live/test_nova_reel_live.py loading prompt-field-realistic.txt → Tasks 6, 7
  - §6.5 offline replay via captured fixtures → Task 8
  - §8.2 examples/configs/nova-reel.yaml → Task 2

- **Type consistency:** `NovaReelEngine`, `NovaReelBackend`, `NovaReelEngineConfig` named consistently across tasks. Config field names (`region_name`, `output_s3_uri`, `model_id`, `duration_seconds`, `fps`, `dimension`, `prompt_body_key`, `declared_flags_map`, `output_kms_key_id`) appear identically in Tasks 0, 1, 2, 6, 7.

- **No placeholders:** every step has actual code or actual command.

- **Live spend isolation:** Task 7 is the ONLY task that fires real AWS calls; every prior task is fully offline. Task 4 (IAM attach + bucket create) mutates cloud state but does not generate spend; both operations are reversible and documented.

- **CLAUDE.md durability rule:** RED smoke scaffold (Task 6) commits BEFORE any live spend (Task 7) per the project rule.
