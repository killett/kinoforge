# Bearer-Provider Comparison Smokes — Design

**Layer:** 4 (post-Layer-3 Bedrock pivot)
**Date:** 2026-06-07
**Status:** Design — awaiting plan
**Spec deps:** Layer 1 (`AuthStrategy` substrate, `2026-06-05-auth-strategy-substrate-design.md`),
              Layer F (per-engine asset wiring),
              Layer K (spec & params routing),
              Layer L (batch CLI + manifest),
              Layer O (output sink),
              Layer R (keyframe stage + image engines)
**Standard prompt:** `/workspace/prompt-field-realistic.txt` (verbatim per memory `feedback_standard_test_prompt`)

---

## 1 — Goal

Ship end-to-end MP4 generation through four hosted video providers
(Replicate, Runway, Luma, fal) so an operator can fire one command,
get four MP4s for the same prompt, and compare them by eye. Each MP4
filename encodes provider + model so attribution is obvious at
`ls` time.

Layer also introduces a foundational ABC,
`RemoteSubmitPollBackend`, that captures the universal
submit-poll-fetch lifecycle every hosted video API follows.
Future providers (Pika, Kling, Higgsfield, …) and future
cross-cutting features (rate limiting, spend tracking, retry
policy, webhook callbacks, telemetry) land as thin additions
on this single base class.

**Scope:** t2v + i2v + flf2v across all four providers — 12 video
smokes plus 2 shared keyframe pre-stages. Total live spend
~$2.32. Total session budget unchanged ($20 ceiling per
`feedback_autonomous_no_gates`).

---

## 2 — Architectural fork settled

The Phase 19 fal integration produced a one-off `FalEngine`
+ `FalBackend` because fal’s wire shape (`POST queue.fal.run/...`
+ poll `https://queue.fal.run/.../requests/{id}/status`) does not
match the `HostedAPIEngine` synthetic shim contract
(`POST {endpoint}` → `{"job_id": ...}`, then
`GET {endpoint}/status/{job_id}` → `{"status": "done", ...}`).
Same shape mismatch applies to Replicate, Runway, and Luma:

| Provider | Submit | Poll | Status enum | Done value | Output path |
|---|---|---|---|---|---|
| Replicate | `POST /v1/predictions` | `GET /v1/predictions/{id}` | `status` | `succeeded` | `output` (str or list[str]) |
| Runway | `POST /v1/{text,image}_to_video` *(needs `X-Runway-Version` header)* | `GET /v1/tasks/{id}` | `status` | `SUCCEEDED` | `output[0]` |
| Luma | `POST /dream-machine/v1/generations/video` | `GET /dream-machine/v1/generations/{id}` | `state` | `completed` | `assets.video` |
| Fal | `POST queue.fal.run/{model}` | `GET queue.fal.run/{model}/requests/{id}/status` | `status` | `COMPLETED` | `response.video.url` |

Three architectural responses were considered:

- **A.** Per-provider engines wrapping each provider’s official Python SDK (mirrors `FalEngine`)
- **B.** Generalise `HostedAPIEngine` with response-shape YAML knobs (a wire-description DSL)
- **C.** Per-provider engines hand-rolled in `urllib` (no SDK deps)
- **D.** Kinoforge-owned `RemoteSubmitPollBackend` ABC + per-provider subclasses (best foundation)

**D** wins on the foundation-quality axis: shared spine for
cross-cutting features (rate limiting, spend tracking, retry,
telemetry); new providers compress to ~30-50 LOC subclasses; SDK
absorption of each provider’s wire-shape quirks is per-subclass
implementation detail. **A** is the per-provider implementation
tactic adopted *inside* D — each subclass lazy-imports its SDK
and calls `Bearer.client_kwargs()` → SDK constructor.

Fal retrofitted as the 4th subclass; the abstraction is validated
against 4 wire shapes at landing, not 3.

---

## 3 — Module layout

### 3.1 — Foundation (`src/kinoforge/core/remote_backend.py`)

```python
class RemoteSubmitPollBackend(GenerationBackend):
    """Submit-poll-fetch lifecycle backend for hosted video APIs.

    Concrete subclasses implement the five abstract hooks below;
    the base class owns the poll loop, AuthStrategy wiring, error
    mapping, and the GenerationBackend public surface.
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        auth: AuthStrategy,
        sleep: Callable[[float], None] = time.sleep,
        max_poll: int = 120,
        poll_interval_s: float = 2.0,
        probe_profile: ModelProfile,
    ) -> None: ...

    # ---- public GenerationBackend surface (final) ----
    def submit(self, job: GenerationJob) -> str: ...
    def result(self, job_id: str) -> Artifact: ...
    def capabilities(self) -> ModelProfile: ...
    def inspect_capabilities(self) -> ModelProfile: ...
    def endpoints(self) -> dict[str, str]: ...

    # ---- subclass hooks (abstract) ----
    @abstractmethod
    def _submit(self, client: Any, job: GenerationJob) -> str: ...
    @abstractmethod
    def _poll_one(self, client: Any, job_id: str) -> dict[str, Any]: ...
    @abstractmethod
    def _is_done(self, status: dict[str, Any]) -> bool: ...
    @abstractmethod
    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]: ...
    @abstractmethod
    def _extract_output_url(self, status: dict[str, Any]) -> str: ...

    # ---- subclass hooks (default impls) ----
    def _extract_filename(self, status: dict[str, Any]) -> str:
        return ""

    def _endpoints_map(self) -> dict[str, str]:
        return {}
```

Co-located in the same module:

```python
class RemoteSubmitPollEngine(GenerationEngine):
    """Companion ABC: provision() does auth-only validation,
    backend() returns a configured subclass instance,
    key_base() reads cfg["spec"]["model"],
    extract_last_frame() uses frames.ffmpeg_last_frame.
    """

    requires_compute: bool = False
    requires_local_weights: bool = False

    # Subclasses must implement:
    @abstractmethod
    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider
    ) -> Callable[[], Any]: ...
    @abstractmethod
    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend: ...
```

ABC stable-surface lockdown lives in
`tests/test_core_invariant.py::test_remote_submit_poll_backend_abc_stable_surface`
(mirrors `test_auth_strategy_abc_stable_surface`).

### 3.2 — Per-provider engines

```
src/kinoforge/engines/replicate/__init__.py
  ReplicateEngine(RemoteSubmitPollEngine)
  ReplicateBackend(RemoteSubmitPollBackend)
  Self-registers under "replicate"

src/kinoforge/engines/runway/__init__.py
  RunwayEngine(RemoteSubmitPollEngine)
  RunwayBackend(RemoteSubmitPollBackend)
  Self-registers under "runway"

src/kinoforge/engines/luma/__init__.py
  LumaEngine(RemoteSubmitPollEngine)
  LumaBackend(RemoteSubmitPollBackend)
  Self-registers under "luma"

src/kinoforge/engines/fal/__init__.py
  (REWRITTEN)
  FalEngine(RemoteSubmitPollEngine)
  FalBackend(RemoteSubmitPollBackend)
  Self-registers under "fal"  (key + YAML kind unchanged)
```

Each engine adds itself to `src/kinoforge/_adapters.py` for
import-time activation.

### 3.3 — Per-provider image engine (Layer R reuse)

```
src/kinoforge/image_engines/replicate/__init__.py
  ReplicateImageEngine(ImageEngine)
  ReplicateImageBackend(RemoteSubmitPollBackend)
```

Same base class — image generation is the same submit/poll/fetch
dance with a single-URL output. Used by Layer R `KeyframeStage`
to generate the shared init / bookend frames before video fan-out.

Default model: `black-forest-labs/flux-schnell` (~$0.003 per image).

---

## 4 — Per-provider subclass surface

Each subclass implements the five abstract hooks plus a small
`_inject_assets` helper for i2v / flf2v wiring. All HTTP work
happens through the provider’s lazy-imported SDK.

### 4.1 — Replicate

```python
class ReplicateBackend(RemoteSubmitPollBackend):
    def _submit(self, client, job):
        version = job.spec["model"]   # e.g. "wan-video/wan-t2v-1.3b"
        input = {"prompt": resolve_prompt(job), **job.spec.get("params", {})}
        self._inject_assets(input, job)
        pred = client.predictions.create(version=version, input=input)
        return pred.id

    def _poll_one(self, client, job_id):
        pred = client.predictions.get(job_id)
        return {
            "id": pred.id,
            "status": pred.status,
            "output": pred.output,
            "error": pred.error,
        }

    def _is_done(self, status):
        return status["status"] == "succeeded"

    def _is_failed(self, status):
        if status["status"] == "failed":
            return True, status.get("error") or "replicate prediction failed"
        return False, ""

    def _extract_output_url(self, status):
        out = status["output"]
        if isinstance(out, list):
            return out[0] if out else ""
        return str(out) if out else ""

    def _inject_assets(self, input_dict, job):
        for asset in job.segments[0].assets if job.segments else []:
            if asset.role == "init_image":
                input_dict["image"] = asset.ref.uri
            elif asset.role == "start_image":
                input_dict["start_image"] = asset.ref.uri
            elif asset.role == "end_image":
                input_dict["end_image"] = asset.ref.uri
```

### 4.2 — Runway

```python
class RunwayBackend(RemoteSubmitPollBackend):
    def _submit(self, client, job):
        model = job.spec["model"]              # e.g. "gen3a_turbo"
        prompt = resolve_prompt(job)
        kw = {"model": model, "prompt_text": prompt, **job.spec.get("params", {})}
        self._inject_assets(kw, job)
        task = client.text_to_video.create(**kw) if job.spec.get("mode") == "t2v" \
               else client.image_to_video.create(**kw)
        return task.id

    def _poll_one(self, client, job_id):
        task = client.tasks.retrieve(job_id)
        return {
            "id": task.id,
            "status": task.status,
            "output": task.output,
            "failure": getattr(task, "failure", None),
        }

    def _is_done(self, status):
        return status["status"] == "SUCCEEDED"

    def _is_failed(self, status):
        if status["status"] == "FAILED":
            return True, status.get("failure") or "runway task failed"
        return False, ""

    def _extract_output_url(self, status):
        out = status["output"]
        return out[0] if out else ""

    def _inject_assets(self, kw, job):
        for asset in job.segments[0].assets if job.segments else []:
            if asset.role == "init_image":
                kw["prompt_image"] = asset.ref.uri
            elif asset.role == "start_image":
                kw["first_image"] = asset.ref.uri
            elif asset.role == "end_image":
                kw["last_image"] = asset.ref.uri
```

`X-Runway-Version` header injection is handled inside the
`runwayml` SDK; no manual header work needed at the kinoforge
layer.

### 4.3 — Luma

```python
class LumaBackend(RemoteSubmitPollBackend):
    def _submit(self, client, job):
        model = job.spec["model"]              # e.g. "ray-2-flash"
        kw = {"prompt": resolve_prompt(job), "model": model, **job.spec.get("params", {})}
        self._inject_assets(kw, job)
        gen = client.generations.create(**kw)
        return gen.id

    def _poll_one(self, client, job_id):
        gen = client.generations.get(job_id)
        return {
            "id": gen.id,
            "state": gen.state,
            "assets": gen.assets or {},
            "failure_reason": getattr(gen, "failure_reason", None),
        }

    def _is_done(self, status):
        return status["state"] == "completed"

    def _is_failed(self, status):
        if status["state"] == "failed":
            return True, status.get("failure_reason") or "luma generation failed"
        return False, ""

    def _extract_output_url(self, status):
        return str(status["assets"].get("video", "") or "")

    def _inject_assets(self, kw, job):
        keyframes = {}
        for asset in job.segments[0].assets if job.segments else []:
            if asset.role in ("init_image", "start_image"):
                keyframes["frame0"] = {"type": "image", "url": asset.ref.uri}
            elif asset.role == "end_image":
                keyframes["frame1"] = {"type": "image", "url": asset.ref.uri}
        if keyframes:
            kw["keyframes"] = keyframes
```

### 4.4 — Fal (retrofit)

```python
class FalBackend(RemoteSubmitPollBackend):
    def _submit(self, client, job):
        # Endpoint comes from cfg.engine.fal.endpoint (mirrored onto the
        # backend at construction time as self._endpoint_default).
        # Preserves Phase 19 FalEngineConfig surface — spec.endpoint is NOT
        # a valid YAML knob.
        endpoint = self._endpoint_default
        input = {"prompt": resolve_prompt(job), **job.spec.get("params", {})}
        self._inject_assets(input, job)
        handler = client.submit(endpoint, arguments=input)
        return handler.request_id

    def _poll_one(self, client, job_id):
        endpoint = self._endpoint_default
        st = client.status(endpoint, job_id, with_logs=False)
        # status method returns {"status": "...", "queue_position": ...}
        # done means response is fetchable
        if str(st.get("status", "")).upper() == "COMPLETED":
            resp = client.result(endpoint, job_id)
            return {"status": "COMPLETED", "response": resp}
        return {"status": str(st.get("status", "")).upper(), "response": None}

    def _is_done(self, status):
        return status["status"] == "COMPLETED"

    def _is_failed(self, status):
        if status["status"] in ("FAILED", "CANCELLED"):
            return True, f"fal status={status['status']}"
        return False, ""

    def _extract_output_url(self, status):
        resp = status.get("response") or {}
        url = walk_dot_path(resp, self._url_path)   # existing helper
        return url

    def _inject_assets(self, input_dict, job):
        for asset in job.segments[0].assets if job.segments else []:
            if asset.role in self._asset_paths:
                set_by_dot_path(input_dict, self._asset_paths[asset.role], asset.ref.uri)
```

Existing Phase 19 fal-specific config knobs (`endpoint`,
`queue_base`, `api_key_env`, `url_path`, `asset_paths`) preserved
on `FalEngineConfig`. YAML surface unchanged → `examples/configs/fal.yaml`
and `examples/configs/keyframe-fal-*.yaml` continue to load.

---

## 5 — Output filename + `OutputSink` Protocol change

### 5.1 — Protocol additive change

`src/kinoforge/outputs/base.py`:

```python
class OutputSink(Protocol):
    def publish(
        self,
        data: bytes,
        *,
        prompt: str,
        extension: str,
        namespace: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str: ...
```

`provider` and `model` are named-only additive params with `None`
defaults — every existing call site continues to compile and
produce a working (provider-less) filename.

### 5.2 — `format_filename` signature change

```python
def format_filename(
    *,
    ts: str,
    provider: str,
    model: str,
    slug: str,
    extension: str,
) -> str:
    """Compose ``{ts}_{provider}_{model}_{slug}{extension}``.

    Provider and model are slugified independently (max 20 and 24
    chars respectively). Empty / unknown values become the literal
    "unknown" so the filename schema is stable.
    """
    return f"{ts}_{provider}_{model}_{slug}{extension}"
```

Single call site: `LocalOutputSink.publish`. Update is atomic.

### 5.3 — Wiring path

- `GenerateClipStage.run` already has `cfg` in scope; extracts
  `provider = cfg["engine"]["kind"]` and `model = cfg["spec"].get("model", "")`
  and threads through to `sink.publish(...)`.
- `Orchestrator.generate` and `batch_generate` already thread
  `cfg` through to the stage; no signature change at orchestrator
  or batch layer.

### 5.4 — Backward compat

- `OutputSink.publish` — additive named params with `None`
  defaults. No Protocol-conformance break.
- `LocalOutputSink.publish` — when `provider` / `model` are
  `None`, falls back to the literal `"unknown"` for that field.
- `format_filename` — signature break for any direct caller. Grep
  confirms one call site (`LocalOutputSink.publish`) — atomic
  update.

### 5.5 — Example filenames

(`dir: output/comparison` per YAML + `batch_id: compare-all-providers`;
Layer L `batch_generate` namespaces by `batch_id` → actual paths
nest under `output/comparison/compare-all-providers/`.)

```
output/comparison/compare-all-providers/20260607-143015_replicate_wan-video-wan-t2v-1-3b_photorealistic-c.mp4
output/comparison/compare-all-providers/20260607-143015_runway_gen3a-turbo_photorealistic-cine.mp4
output/comparison/compare-all-providers/20260607-143015_luma_ray-2-flash_photorealistic-cinema.mp4
output/comparison/compare-all-providers/20260607-143015_fal_fal-ai-wan-t2v_photorealistic-cinem.mp4
```

---

## 6 — Configs + comparison manifest

### 6.1 — Per-(provider × mode) YAMLs

12 video YAMLs under `examples/configs/comparison/`:

```
replicate-t2v.yaml      replicate-i2v.yaml      replicate-flf2v.yaml
runway-t2v.yaml         runway-i2v.yaml         runway-flf2v.yaml
luma-t2v.yaml           luma-i2v.yaml           luma-flf2v.yaml
fal-t2v.yaml            fal-i2v.yaml            fal-flf2v.yaml
```

Budget-tier defaults per provider (candidate model IDs —
planner verifies each is currently published + accepting jobs at
plan time; spec.model values below are placeholders that may
shift to nearest-equivalent at planning):

| Provider | t2v model (candidate) | i2v model (candidate) | flf2v model (candidate) | Per-5s clip |
|---|---|---|---|---|
| Replicate | `wan-video/wan-2.1-t2v-1.3b` | `wan-video/wan-2.1-i2v-480p` | `wan-video/wan-2.1-flf2v-720p` | ~$0.04–0.08 |
| Runway | `gen3a_turbo` | `gen3a_turbo` *(image-to-video)* | `gen3a_turbo` *(first+last frame)* | ~$0.25 |
| Luma | `ray-2-flash` | `ray-2-flash` *(keyframes.frame0)* | `ray-2-flash` *(keyframes.frame0+frame1)* | ~$0.35 |
| Fal | `fal-ai/wan-t2v` | `fal-ai/wan-i2v` | `fal-ai/wan-flf2v` | ~$0.10 |

Total live-spend projection per full comparison run: **~$2.32**
(t2v + i2v + flf2v × 4 providers + ~$0.036 keyframe overhead).
A model ID swap during planning may shift this by ±50% per
affected slot; cumulative ceiling is held by
`lifecycle.budget: 1.50` per YAML.

Each YAML shape (Replicate t2v shown — others analogous):

```yaml
engine:
  kind: replicate

spec:
  model: "wan-video/wan-t2v-1.3b"
  mode: t2v

params:
  duration: 5
  fps: 24
  aspect_ratio: "16:9"

lifecycle:
  budget: 1.50

output:
  kind: local
  dir: output/comparison
  enabled: true
```

i2v YAMLs reference the shared init-frame URI via Layer F
`asset_paths`. flf2v YAMLs reference both bookend images:

```yaml
# i2v
segment_assets:
  - role: init_image
    ref:
      uri: <keyframe-i2v-uri>

# flf2v
segment_assets:
  - role: start_image
    ref:
      uri: <keyframe-flf2v-frame0-uri>
  - role: end_image
    ref:
      uri: <keyframe-flf2v-frame1-uri>
```

**Keyframe-URI determinism is a planning-time decision** —
manifest entry ordering guarantees keyframe outputs land first;
the planner picks one of the following mechanisms to give
downstream YAMLs a stable reference:

1. **Keyframe YAML sets `output.enabled: false`** and downstream
   YAMLs reference the ArtifactStore URI under
   `<state-dir>/<run-id>/<artifact-filename>` — `run_id` is
   manifest-fixed (`keyframe-i2v` / `keyframe-flf2v`); artifact
   filename is engine-derived but stable per `(run_id, role)`.
2. **Extend `LocalOutputSink` with a literal-filename mode**
   (opt-in via YAML knob `output.filename_mode: literal`) so
   keyframes land at `output/comparison/keyframe-i2v/init.png`
   verbatim.
3. **Per-video-config Layer R chain with a frozen seed** —
   each video YAML carries its own keyframe block; all four use
   `flux-schnell` + identical `spec.params.seed` so each
   provider regenerates the same image deterministically.
   Trade-off: keyframes regenerated 4× (i2v) or 8× (flf2v) per
   run, adding ~$0.036.

Planner picks one at task definition; spec does not lock the
mechanism because all three preserve the apples-to-apples
guarantee.

### 6.2 — Shared keyframe pre-stage YAMLs

```
examples/configs/comparison/keyframe-i2v.yaml      # ReplicateImageEngine → 1 init frame
examples/configs/comparison/keyframe-flf2v.yaml    # ReplicateImageEngine → frame0 + frame1
```

Use `image_engines.replicate` with `black-forest-labs/flux-schnell`.
Output sink writes PNG to `output/comparison/keyframe-{i2v,flf2v}/`.

### 6.3 — Comparison manifest

`examples/configs/comparison/compare-all-providers.yaml`:

```yaml
batch_id: compare-all-providers
entries:
  # ---- Pre-stage: shared keyframes via Replicate flux-schnell ----
  - run_id: keyframe-i2v
    config: examples/configs/comparison/keyframe-i2v.yaml
    prompt_file: prompt-field-realistic.txt

  - run_id: keyframe-flf2v
    config: examples/configs/comparison/keyframe-flf2v.yaml
    prompt_file: prompt-field-realistic.txt

  # ---- t2v fan-out ----
  - run_id: replicate-t2v
    config: examples/configs/comparison/replicate-t2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: runway-t2v
    config: examples/configs/comparison/runway-t2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: luma-t2v
    config: examples/configs/comparison/luma-t2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: fal-t2v
    config: examples/configs/comparison/fal-t2v.yaml
    prompt_file: prompt-field-realistic.txt

  # ---- i2v fan-out (uses keyframe-i2v output via segment_assets) ----
  - run_id: replicate-i2v
    config: examples/configs/comparison/replicate-i2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: runway-i2v
    config: examples/configs/comparison/runway-i2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: luma-i2v
    config: examples/configs/comparison/luma-i2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: fal-i2v
    config: examples/configs/comparison/fal-i2v.yaml
    prompt_file: prompt-field-realistic.txt

  # ---- flf2v fan-out (uses keyframe-flf2v frame0 + frame1) ----
  - run_id: replicate-flf2v
    config: examples/configs/comparison/replicate-flf2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: runway-flf2v
    config: examples/configs/comparison/runway-flf2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: luma-flf2v
    config: examples/configs/comparison/luma-flf2v.yaml
    prompt_file: prompt-field-realistic.txt
  - run_id: fal-flf2v
    config: examples/configs/comparison/fal-flf2v.yaml
    prompt_file: prompt-field-realistic.txt
```

Layer L `kinoforge batch` provides the per-entry summary,
continue-on-error, BudgetExceeded fatal handling, and
`_batch_summary.json` output ledger.

### 6.4 — Invocation

```bash
KINOFORGE_LIVE_TESTS=1 \
  pixi run -e live-hosted kinoforge batch \
  examples/configs/comparison/compare-all-providers.yaml
```

---

## 7 — Tests

### 7.1 — Offline tests (per-PR, runs in CI)

| File | Coverage | Approx test count |
|---|---|---|
| `tests/core/test_remote_backend.py` | base ABC poll loop, error mapping, AuthStrategy threading, ABC stable-surface | 8 |
| `tests/engines/test_replicate.py` | FakeReplicateClient — submit / poll / done / failed / output / i2v / flf2v / errors | 12 |
| `tests/engines/test_runway.py` | FakeRunwayClient — same matrix | 12 |
| `tests/engines/test_luma.py` | FakeLumaClient — same matrix | 12 |
| `tests/engines/test_fal.py` | **REWRITTEN**: existing 24 tests preserved against new base; +4 base-class-hook tests | 28 |
| `tests/image_engines/test_replicate.py` | ReplicateImageEngine smoke | 4 |
| `tests/outputs/test_local.py` | provider/model in filename + collision + None-fallback | extended +5 |
| `tests/outputs/test_format_filename.py` | new helper coverage | 6 |
| `tests/test_core_invariant.py` | vendor-SDK confinement, core-import-ban, RemoteSubmitPollBackend ABC stable surface | extended +3 |

Total net new offline tests: ~60. All sub-millisecond.

### 7.2 — Live tests (skipped without env opt-in)

```
tests/live/test_replicate_live.py             # t2v + i2v + flf2v subprocess CLI invocations
tests/live/test_runway_live.py                # same shape
tests/live/test_luma_live.py                  # same shape
tests/live/test_fal_live.py                   # EXTENDED with i2v + flf2v
tests/live/test_comparison_batch_live.py      # end-to-end manifest
```

Each live test:

- `pytestmark = pytest.mark.live`
- Module-level skip on missing
  `KINOFORGE_LIVE_TESTS=1` + per-provider env var
- subprocess invocation of `python -m kinoforge generate` (or
  `kinoforge batch` for the manifest test)
- Asserts: return-code 0, at least one `.mp4` published, ISO-BMFF
  `ftyp` magic-byte check at offset 4
- Standard prompt loaded verbatim from
  `/workspace/prompt-field-realistic.txt`

### 7.3 — Fake-SDK shape (illustrative)

```python
class FakeReplicateClient:
    def __init__(
        self,
        *,
        predictions_create_response: dict,
        predictions_get_responses: list[dict],
    ) -> None:
        self.predictions = _FakePredictionsAPI(
            create_response=predictions_create_response,
            get_responses=list(predictions_get_responses),
        )

class _FakePredictionsAPI:
    def __init__(self, *, create_response, get_responses):
        self._create = create_response
        self._gets = get_responses
        self.create_calls: list[dict] = []
    def create(self, **kw):
        self.create_calls.append(kw)
        return _FakePrediction(self._create)
    def get(self, pred_id):
        return _FakePrediction(self._gets.pop(0))
```

Analogous fakes for `runwayml.Client`, `lumaai.Client`,
`fal_client.AsyncClient`.

### 7.4 — Invariants

`tests/test_core_invariant.py` extended:

- Vendor-SDK confinement scan:
  - `replicate` only under `engines/replicate/` + `image_engines/replicate/`
  - `runwayml` only under `engines/runway/`
  - `lumaai` only under `engines/luma/`
  - `fal_client` only under `engines/fal/`
- Core-import-ban scan: `kinoforge.engines.*` and
  `kinoforge.image_engines.*` not imported from `core/`.
- `RemoteSubmitPollBackend` ABC stable-surface test (mirror of
  `test_auth_strategy_abc_stable_surface`) lockable JSON baseline
  checked into `tests/fixtures/`.

### 7.5 — Test count projection

Pre-Layer-4 baseline: ~1111 collected / 6 skipped.
Post-Layer-4 projection: ~1171 collected / 11 skipped (+5 live
modules under default env).

---

## 8 — Dependencies & pre-flight

### 8.1 — `pixi.toml`

```toml
[feature.live-hosted.pypi-dependencies]
replicate    = ">=1.0.0"
runwayml     = ">=3.0.0"
lumaai       = ">=1.0.0"
fal-client   = ">=0.5.0"
```

New `live-hosted` feature env. Default `pixi run test` env stays
lean — three new deps loaded only when feature env activated. SDK
lazy-imports inside method bodies preserve core-import-ban.

### 8.2 — `.env.example`

Already populated with `REPLICATE_API_TOKEN`,
`RUNWAYML_API_SECRET`, `LUMAAI_API_KEY`, `FAL_KEY`. **No edits
required.**

### 8.3 — Pre-flight

`tools/preflight.py` gains a `--check-hosted` flag. When set,
verifies the four Bearer env vars present (non-empty). Default
off. Comparison batch invocation pattern includes
`pixi run preflight --check-hosted` before live spend.

---

## 9 — Error handling

Subclass `_is_failed` hooks return `(failed, reason)`. Base class
raises `KinoforgeError(f"{provider}: {reason}")` on `failed=True`.

SDK exceptions wrapped at the seam:

| Provider | Auth-shape exception | → kinoforge error |
|---|---|---|
| Replicate | `replicate.exceptions.ReplicateError` with 401 status | `AuthError("replicate auth failed: ...")` |
| Runway | `runwayml.APIError` with 401/403 | `AuthError("runway auth failed: ...")` |
| Luma | `lumaai.APIError` with 401/403 | `AuthError("luma auth failed: ...")` |
| Fal | preserved from current behavior | preserved |

All other SDK exceptions wrapped as
`KinoforgeError(f"{provider}: {exc}") from exc`.

Layer L batch treats `BudgetExceeded`, `CapabilityMismatch`, and
`TeardownError` as batch-fatal; everything else (per-entry
`KinoforgeError`, `AuthError`, generic Python exception)
continues to the next entry. Existing Layer L behavior, no new
code.

---

## 10 — Cost guards

- `lifecycle.budget` per config caps per-clip spend at $1.50
  (3× the most expensive budget-tier provider, Luma Ray-2 at
  ~$0.70/5s; default headroom for retries).
- `BudgetTracker.enforce` fires on submit. Existing behavior.
- Layer L `--max-spend USD` knob caps cumulative batch spend;
  default unset. Comparison batch projection: ~$2.32.
- Session ceiling: $20 per `feedback_autonomous_no_gates`.

All four providers have `requires_compute=False`. No compute
instance created → no reaping needed → no orphan-pod L1/L2 risk
(unlike Phase 28 RunPod work).

---

## 11 — Scope cuts (deferred)

- **Rate limiting** on `RemoteSubmitPollBackend` (the home for it
  exists; YAGNI until measured RPS limits matter).
- **Spend tracking** via SDK response-metadata (each SDK surfaces
  per-call cost; future telemetry layer).
- **Webhook callback path** (each provider supports webhooks;
  polling cost is fine today).
- **HTTP-recording fixtures** for SDK-drift detection (offline
  coverage chose fake-SDK boundary instead).
- **Flagship-tier YAMLs** (budget-tier first; flagship comparison
  follows once budget tier validated).
- **Cross-provider quality scoring** (CLIP score, FVD, etc.) —
  human-eye comparison only.
- **Alt image engines** beyond Replicate flux-schnell for
  keyframes (SDXL via Replicate, Imagen via Vertex AI) —
  deferred.
- **Per-mode model upgrades** where budget tier does not support
  flf2v cleanly; flagged at YAML composition during plan tasks,
  resolved by per-YAML override (no spec change).

---

## 12 — Locked decisions

1. **Provider name in filename** = engine registry key
   (`replicate` / `runway` / `luma` / `fal`).
2. **Model name in filename** = `slugify(spec.model, max_chars=24)`.
3. **Init / bookend frames** generated via `ReplicateImageEngine`
   (`flux-schnell`) ONCE per batch run, shared across all four
   providers’ i2v / flf2v entries.
4. **All 12 video smokes** use the verbatim prompt from
   `/workspace/prompt-field-realistic.txt` (memory
   `feedback_standard_test_prompt`). The prompt is t2v-shaped
   (scene description, not motion-only or transition-only) — for
   i2v + flf2v the operator accepts that the prompt-text axis is
   held constant across modes at the cost of per-mode prompt
   suitability. Cross-test prompt invariance is the explicit
   priority per the memory; cross-mode optimisation is not.
5. **OutputSink Protocol** is extended additively (new named
   params with `None` defaults); `format_filename` signature
   changes atomically (single in-tree caller).
6. **Fal retrofitted** onto the new base class as the 4th
   subclass at landing.
7. **Per-provider subclass** uses the provider’s official Python
   SDK; lazy-imported inside method bodies.
8. **No HTTP-recording fixtures** in this layer; offline = fake
   SDK at the boundary, live = subprocess-CLI smokes.
9. **Comparison invocation** is `kinoforge batch
   examples/configs/comparison/compare-all-providers.yaml` — no
   new CLI subcommand.
10. **Layer R `KeyframeStage`** generates the shared init / bookend
    frames as the first two manifest entries; downstream video
    entries reference the resulting paths via Layer F
    `asset_paths`.

---

## 13 — Out of scope / future layers

- **Layer 5+ candidates that benefit from `RemoteSubmitPollBackend`:**
  Pika, Kling, Higgsfield, MiniMax, Hailuo — each lands as a
  thin subclass after this foundation ships.
- **Cross-cutting features** (rate limiting, spend tracking,
  webhook callbacks, telemetry) — one PR each, modifies only
  the base class and per-subclass override hooks where needed.
- **Flagship-tier comparison sweep** — separate manifest +
  separate per-(provider × mode) YAML set; same code path, no
  engine changes.
