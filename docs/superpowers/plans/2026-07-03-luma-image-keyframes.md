# LumaImageEngine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hosted `ImageEngine` for Luma's dream-machine image API, registered as `"luma"`, usable from the Layer R `keyframe:` block; live-smoked with the $20 Luma credit.

**Architecture:** Raw-REST inner backend on `RemoteSubmitPollBackend` (no SDK dep), mirroring `image_engines/replicate/` — `_LumaHttp` urllib client behind the existing `client_factory` seam, ImageJob→GenerationJob adapter backend, Bearer(`LUMAAI_API_KEY`) engine, self-registration.

**Tech Stack:** Python 3.12/3.13, urllib (stdlib), pixi, pytest, pre-commit. Spec: `docs/superpowers/specs/2026-07-03-luma-image-keyframes-design.md`.

**User decisions (already made):** $20 Luma platform credit is pre-authorized for keyframe smokes (memory `project_luma_video_retirement_2026`); session runs autonomously (memory `feedback_autonomous_no_gates`); standard prompt file mandatory for video smokes — reused here for the image smoke (memory `feedback_standard_test_prompt`).

---

### Task 1: LumaImageEngine module + unit tests + registration

**Goal:** `image_engines/luma/` passes an offline unit suite mirroring the replicate sibling and self-registers as `"luma"`.

**Files:**
- Create: `src/kinoforge/image_engines/luma/__init__.py`
- Create: `tests/image_engines/test_luma.py`
- Modify: `src/kinoforge/_adapters.py` (add self-registration import after line 39)

**Acceptance Criteria:**
- [ ] `registry.get_image_engine("luma")()` returns a `LumaImageEngine` after `import kinoforge._adapters`.
- [ ] Submit POSTs to `/dream-machine/v1/generations/image` with body `{"prompt", "model", **spec.params}` — no ref fields ever.
- [ ] Poll maps `dreaming→(keep polling)`, `completed→Artifact` with `assets.image` URL, `failed→KinoforgeError` carrying `failure_reason`.
- [ ] `_delete` DELETEs `/dream-machine/v1/generations/{id}`.
- [ ] `validate_spec` raises `ValidationError` on missing `spec.model` or empty prompt.
- [ ] Missing `LUMAAI_API_KEY` → `AuthError` from `provision()`/`backend()`.
- [ ] `model_identity` returns `spec.model` or `""` (never raises).

**Verify:** `pixi run pytest tests/image_engines/test_luma.py -v` → all pass; `pixi run pytest tests/image_engines/ -q` → no regressions.

**Steps:**

- [ ] **Step 1: Write failing tests — `tests/image_engines/test_luma.py`**

```python
"""Tests for LumaImageEngine + LumaImageBackend (raw-REST, no SDK)."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import ImageJob


class _FakeLumaHttp:
    """Records requests; replays canned generation states."""

    def __init__(
        self,
        *,
        submit_response: dict[str, Any] | None = None,
        get_responses: list[dict[str, Any]] | None = None,
    ) -> None:
        self.submit_response = submit_response or {"id": "gen-1", "state": "dreaming"}
        self.get_responses = list(get_responses or [])
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []
        self.delete_calls: list[str] = []

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.post_calls.append((path, body))
        return self.submit_response

    def get_json(self, path: str) -> dict[str, Any]:
        self.get_calls.append(path)
        return self.get_responses.pop(0)

    def delete(self, path: str) -> None:
        self.delete_calls.append(path)


def _backend(fake: _FakeLumaHttp) -> Any:
    from kinoforge.image_engines.luma import LumaImageBackend

    return LumaImageBackend(client_factory=lambda: fake, sleep=lambda _s: None)


def _job(**spec_extra: Any) -> ImageJob:
    return ImageJob(
        spec={"model": "photon-1", **spec_extra},
        prompt="a lighthouse at dawn",
    )


def test_submit_posts_image_generation_body() -> None:
    """Bug caught: wrong endpoint path or ref fields leaking into the body."""
    fake = _FakeLumaHttp()
    job_id = _backend(fake).submit(_job(params={"aspect_ratio": "16:9"}))
    assert job_id == "gen-1"
    path, body = fake.post_calls[0]
    assert path == "/dream-machine/v1/generations/image"
    assert body == {
        "prompt": "a lighthouse at dawn",
        "model": "photon-1",
        "aspect_ratio": "16:9",
    }
    assert "image_ref" not in body and "style_ref" not in body


def test_poll_dreaming_then_completed_returns_image_artifact() -> None:
    """Bug caught: treating 'dreaming' as terminal, or reading the wrong
    assets key (assets.video is null for image generations)."""
    fake = _FakeLumaHttp(
        get_responses=[
            {"id": "gen-1", "state": "dreaming", "assets": None},
            {
                "id": "gen-1",
                "state": "completed",
                "assets": {"video": None, "image": "https://cdn.luma/img.png"},
            },
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    art = backend.result("gen-1")
    assert art.uri == "https://cdn.luma/img.png"
    assert fake.get_calls == [
        "/dream-machine/v1/generations/gen-1",
        "/dream-machine/v1/generations/gen-1",
    ]


def test_poll_failed_raises_with_failure_reason() -> None:
    """Bug caught: swallowing failure_reason strands live-smoke debugging."""
    fake = _FakeLumaHttp(
        get_responses=[
            {"id": "gen-1", "state": "failed", "failure_reason": "nsfw filter"}
        ]
    )
    backend = _backend(fake)
    backend.submit(_job())
    with pytest.raises(KinoforgeError, match="nsfw filter"):
        backend.result("gen-1")


def test_delete_calls_generation_endpoint() -> None:
    """Bug caught: _delete left as a scaffold raise (replicate-sibling
    copy/paste) — Luma documents DELETE and ephemeral mode relies on it."""
    fake = _FakeLumaHttp()
    backend = _backend(fake)
    backend._inner._delete("gen-9")
    assert fake.delete_calls == ["/dream-machine/v1/generations/gen-9"]


def test_validate_spec_requires_model_and_prompt() -> None:
    from kinoforge.image_engines.luma import LumaImageEngine

    engine = LumaImageEngine(
        auth=Bearer(env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider())
    )
    with pytest.raises(ValidationError, match="spec.model"):
        engine.validate_spec(ImageJob(spec={}, prompt="p"))
    with pytest.raises(ValidationError, match="prompt"):
        engine.validate_spec(ImageJob(spec={"model": "photon-1"}, prompt=""))


def test_backend_without_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kinoforge.image_engines.luma import LumaImageEngine

    monkeypatch.delenv("LUMAAI_API_KEY", raising=False)
    engine = LumaImageEngine(
        auth=Bearer(env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider())
    )
    with pytest.raises(AuthError):
        engine.backend(None, {})


def test_provision_rejects_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug caught: hosted image engine silently accepting a compute pod."""
    from kinoforge.image_engines.luma import LumaImageEngine

    monkeypatch.setenv("LUMAAI_API_KEY", "k")
    engine = LumaImageEngine(
        auth=Bearer(env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider())
    )
    with pytest.raises(KinoforgeError, match="instance must be None"):
        engine.provision(object(), {})  # type: ignore[arg-type]


def test_model_identity_reads_spec_model() -> None:
    from kinoforge.image_engines.luma import LumaImageEngine

    engine = LumaImageEngine(
        auth=Bearer(env_var="LUMAAI_API_KEY", credential_provider=EnvCredentialProvider())
    )
    assert engine.model_identity({"spec": {"model": "uni-1.1"}}) == "uni-1.1"
    assert engine.model_identity({}) == ""


def test_registry_registration() -> None:
    import kinoforge._adapters  # noqa: F401 — side-effect registration

    from kinoforge.core import registry
    from kinoforge.image_engines.luma import LumaImageEngine

    assert isinstance(registry.get_image_engine("luma")(), LumaImageEngine)
```

- [ ] **Step 2: Confirm RED** — `pixi run pytest tests/image_engines/test_luma.py -v` → collection error `ModuleNotFoundError: kinoforge.image_engines.luma`.

- [ ] **Step 3: Write `src/kinoforge/image_engines/luma/__init__.py`**

```python
"""LumaImageEngine — Layer-R image engine for Luma's dream-machine image API.

Raw-REST (urllib) — no ``lumaai`` SDK dependency. Video surface retired
by the provider (Phase 44 deleted the old ``LumaEngine``); this module is
image/keyframe-only. API contract verified 2026-07-03 against
``docs.lumalabs.ai`` — see the design doc.

Self-registers under ``"luma"`` via the image-engine registry.
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError

from kinoforge.core import registry
from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationJob,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
    ModelProfile,
    Segment,
)
from kinoforge.core.remote_backend import RemoteSubmitPollBackend

_BASE_URL = "https://api.lumalabs.ai"
_IMAGE_PROBE = ImageProfile(
    name="luma-image",
    max_resolution=(1920, 1080),
    supported_modes={"t2i"},
)


class _LumaHttp:
    """Minimal Bearer-authenticated JSON client for the Luma REST API."""

    def __init__(self, *, token: str, base_url: str = _BASE_URL) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(  # noqa: S310 — https base, fixed host
            f"{self._base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                raw = resp.read()
        except HTTPError as exc:
            detail = exc.read()[:500].decode(errors="replace")
            raise KinoforgeError(
                f"luma-image: {method} {path} -> HTTP {exc.code}: {detail}"
            ) from exc
        if not raw:
            return {}
        parsed: dict[str, Any] = json.loads(raw)
        return parsed

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST ``body`` as JSON; return the parsed JSON response."""
        return self._request("POST", path, body)

    def get_json(self, path: str) -> dict[str, Any]:
        """GET; return the parsed JSON response."""
        return self._request("GET", path)

    def delete(self, path: str) -> None:
        """DELETE; body (if any) ignored."""
        self._request("DELETE", path)


class _LumaImageInnerBackend(RemoteSubmitPollBackend):
    """Submit-poll backend for ``/dream-machine/v1/generations/image``."""

    def _submit(self, client: object, job: GenerationJob) -> str:
        http: _LumaHttp = client  # type: ignore[assignment]
        prompt = job.segments[0].prompt if job.segments else ""
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": job.spec["model"],
            **(job.spec.get("params") or {}),
        }
        resp = http.post_json("/dream-machine/v1/generations/image", body)
        gen_id = str(resp.get("id", ""))
        if not gen_id:
            raise KinoforgeError(f"luma-image: submit returned no id: {resp!r}")
        return gen_id

    def _poll_one(self, client: object, job_id: str) -> dict[str, Any]:
        http: _LumaHttp = client  # type: ignore[assignment]
        return http.get_json(f"/dream-machine/v1/generations/{job_id}")

    def _is_done(self, status: dict[str, Any]) -> bool:
        return status.get("state") == "completed"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        if status.get("state") == "failed":
            return True, str(
                status.get("failure_reason") or "luma-image generation failed"
            )
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        assets = status.get("assets") or {}
        return str(assets.get("image") or "")

    def _delete(self, job_id: str) -> None:
        http: _LumaHttp = self._client()  # type: ignore[assignment]
        http.delete(f"/dream-machine/v1/generations/{job_id}")

    @classmethod
    def manual_cleanup_url(cls, job_id: str) -> str:
        """Dashboard URL an operator can visit to purge the record by hand."""
        return "https://lumalabs.ai/dream-machine/creations"


class LumaImageBackend(ImageBackend):
    """Image-shape adapter around the Luma submit-poll lifecycle."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], object],
        sleep: Callable[[float], None] = time.sleep,
        max_poll: int = 60,
        poll_interval_s: float = 2.0,
        probe_profile: ImageProfile = _IMAGE_PROBE,
    ) -> None:
        """Initialise with injectable lifecycle seams (test double = fake http)."""
        self._probe = probe_profile
        self._inner = _LumaImageInnerBackend(
            client_factory=client_factory,
            sleep=sleep,
            max_poll=max_poll,
            poll_interval_s=poll_interval_s,
            probe_profile=ModelProfile(
                name=probe_profile.name,
                max_frames=1,
                fps=24,
                supported_modes={"t2i"},
                max_resolution=probe_profile.max_resolution,
                supports_native_extension=False,
                supports_joint_audio=False,
            ),
        )

    def capabilities(self) -> ImageProfile:
        """Return the configured ImageProfile."""
        return self._probe

    def inspect_capabilities(self) -> ImageProfile:
        """Return the configured ImageProfile (no live probe)."""
        return self._probe

    def submit(self, job: ImageJob) -> str:
        """Adapt the ImageJob to a single-segment GenerationJob and submit."""
        adapted = GenerationJob(
            segments=[Segment(prompt=job.prompt, params={}, assets=[])],
            spec=job.spec,
            params=job.params,
        )
        return self._inner.submit(adapted)

    def result(self, job_id: str) -> Artifact:
        """Poll until the image is ready."""
        return self._inner.result(job_id)

    def endpoints(self) -> dict[str, str]:
        """No endpoint URLs for the hosted path."""
        return {}


class LumaImageEngine(ImageEngine):
    """Hosted Luma dream-machine image-engine adapter (photon / UNI-1)."""

    name: str = "luma"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(self, *, auth: Bearer) -> None:
        """Initialise the engine with an explicit Bearer strategy.

        Args:
            auth: Bearer strategy carrying ``LUMAAI_API_KEY``.
        """
        self._auth = auth

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: object | None = None,
    ) -> None:
        """Validate credentials; reject any non-None ``instance``."""
        if instance is not None:
            raise KinoforgeError("LumaImageEngine.provision: instance must be None")
        if not self._auth.credentials_present():
            raise AuthError("luma-image: LUMAAI_API_KEY not present")

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> LumaImageBackend:
        """Build the image backend bound to the Bearer credential."""
        del instance, cfg
        kwargs = self._auth.client_kwargs()
        token = kwargs.get("api_key")
        if not token:
            raise AuthError("luma-image: LUMAAI_API_KEY is empty")
        return LumaImageBackend(client_factory=lambda: _LumaHttp(token=token))

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        """Profiles flow through ImageProfileProvider — not the engine."""
        raise NotImplementedError(
            "LumaImageEngine.profile_for is supplied by ImageProfileProvider"
        )

    def validate_spec(self, job: ImageJob) -> None:
        """Require ``spec.model`` and a non-empty prompt."""
        from kinoforge.core.errors import ValidationError

        if not job.spec.get("model"):
            raise ValidationError("luma-image: spec.model missing")
        if not job.prompt:
            raise ValidationError("luma-image: prompt is empty")

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Luma image identity is the model slug at ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""


def _default_factory() -> LumaImageEngine:
    """Zero-arg engine factory used by the image-engine registry."""
    return LumaImageEngine(
        auth=Bearer(
            env_var="LUMAAI_API_KEY",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_image_engine("luma", _default_factory)
```

NOTE for the implementer: `_delete` calls `self._client()` — check
`RemoteSubmitPollBackend` for the actual cached-client accessor name
(it may be `self._client_cached` populated lazily or a `_client()`
helper; use whatever the base class provides, mirroring how the base
class's delete path obtains the client — read
`src/kinoforge/core/remote_backend.py` `_delete_with_retries` before
writing this line, and match the replicate/runway siblings if they
already implement a concrete `_delete`).

- [ ] **Step 4: Register in `_adapters.py`** — after the replicate image-engine import line:

```python
import kinoforge.image_engines.luma  # noqa: F401  # self-registers under "luma"
```

- [ ] **Step 5: Run to GREEN** — `pixi run pytest tests/image_engines/ -q` → all pass.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/image_engines/luma/__init__.py tests/image_engines/test_luma.py src/kinoforge/_adapters.py
git add src/kinoforge/image_engines/luma/__init__.py tests/image_engines/test_luma.py src/kinoforge/_adapters.py
git commit -m "feat(image): LumaImageEngine — dream-machine image API, raw REST"
```

---

### Task 2: Example keyframe cfg + examples-suite compatibility

**Goal:** `examples/configs/keyframe-luma.yaml` loads through `load_config` and the examples lockdown suite stays green.

**Files:**
- Create: `examples/configs/keyframe-luma.yaml`
- Test: existing `tests/test_examples.py` (parametrized over `examples/configs/*.yaml` — no new file unless a lockdown assert is warranted)

**Acceptance Criteria:**
- [ ] `load_config("examples/configs/keyframe-luma.yaml")` returns a Config whose `keyframe.engine == "luma"` and `keyframe.spec["model"]` is non-empty.
- [ ] `pixi run pytest tests/test_examples.py -q` green (the parametrized loaders pick the new file up automatically).

**Verify:** `pixi run pytest tests/test_examples.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Inspect an existing keyframe-bearing example** (`rg -l "keyframe:" examples/configs/`) and mirror its full document shape — the `keyframe:` block needs a host cfg (engine/models/spec) that passes `load_config` validation. Base the host on the smallest existing hosted example (e.g. the fal/replicate comparison cfgs) rather than inventing one.

- [ ] **Step 2: Write `examples/configs/keyframe-luma.yaml`** — host cfg + this block:

```yaml
keyframe:
  engine: luma
  prompt: "A lighthouse on a rugged cliff at golden hour, photorealistic"
  spec:
    model: photon-1        # flip to uni-1.1 if the Task 4 probe accepts it
    params:
      aspect_ratio: "16:9"
```

(Exact host-side keys: copy from the chosen template; keep the file
minimal. The Task 4 live smoke drives the engine directly and does not
depend on the host cfg's video engine being live.)

- [ ] **Step 3: Load-check** — `pixi run python -c "from kinoforge.core.config import load_config; c = load_config('examples/configs/keyframe-luma.yaml'); print(c.keyframe.engine, c.keyframe.spec)"` → `luma {'model': ...}`.

- [ ] **Step 4: Examples suite** — `pixi run pytest tests/test_examples.py -q` → green (fix slug/lockdown parametrization fallout if any surfaces).

- [ ] **Step 5: Commit**

```bash
git add examples/configs/keyframe-luma.yaml
git commit -m "feat(examples): keyframe-luma.yaml — Luma image keyframe cfg"
```

---

### Task 3: RED live-smoke scaffold (commit BEFORE spend)

**Goal:** `tests/live/test_luma_keyframe_live.py` committed and env-gated before any credit is burned (CLAUDE.md durability rule).

**Files:**
- Create: `tests/live/test_luma_keyframe_live.py`

**Acceptance Criteria:**
- [ ] Without `KINOFORGE_LIVE_SPEND=1` the test SKIPS (module joins the existing live-gating pattern; check `tests/test_live_gating_lockdown.py` expectations — add the module there if the lockdown enumerates live files).
- [ ] Test body: build engine from registry, probe model id (`uni-1.1` first, fall back to `photon-1` on `KinoforgeError` mentioning the model), submit standard prompt, poll to artifact, download bytes, assert size > 10 KB and magic bytes are PNG (`\x89PNG`) or JPEG (`\xff\xd8`), then `_delete` the generation.

**Verify:** `pixi run pytest tests/live/test_luma_keyframe_live.py -q` → `1 skipped` (no env var).

**Steps:**

- [ ] **Step 1: Write the test**

```python
"""Luma image-keyframe live smoke — first `t2i / luma` capability tuple.

Env-gated on KINOFORGE_LIVE_SPEND (same contract as the flashvsr live
module). Spend: one image generation (~cents) from the $20 Luma credit.
"""

from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import pytest

_LIVE_SPEND_ENV = "KINOFORGE_LIVE_SPEND"

_STANDARD_PROMPT_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")


def _require_live_spend_env() -> None:
    if os.environ.get(_LIVE_SPEND_ENV) != "1":
        pytest.skip(f"live-spend gate: set {_LIVE_SPEND_ENV}=1 to spend Luma credit")


def test_luma_keyframe_generation(tmp_path: Path) -> None:
    """Generate one image; assert real image bytes; delete the record.

    Bug caught: request-shape drift against the live API (the offline
    suite can only pin OUR side of the wire), and the uni-1.1 model-id
    question the docs left open (design doc §2).
    """
    _require_live_spend_env()
    import kinoforge._adapters  # noqa: F401

    from kinoforge.core import registry
    from kinoforge.core.errors import KinoforgeError
    from kinoforge.core.interfaces import ImageJob

    engine = registry.get_image_engine("luma")()
    engine.provision(None, {})
    backend = engine.backend(None, {})

    prompt = _STANDARD_PROMPT_PATH.read_text().strip()
    art = None
    model_used = None
    for model in ("uni-1.1", "photon-1"):
        job = ImageJob(
            spec={"model": model, "params": {"aspect_ratio": "16:9"}},
            prompt=prompt,
        )
        engine.validate_spec(job)
        try:
            job_id = backend.submit(job)
            art = backend.result(job_id)
            model_used = model
            break
        except KinoforgeError as exc:
            # Model-id rejection (400 naming the model field) -> try next.
            if "model" in str(exc).lower():
                continue
            raise
    assert art is not None, "both model ids rejected — API surface changed"

    out = tmp_path / "keyframe.img"
    with urllib.request.urlopen(art.uri, timeout=120) as resp:  # noqa: S310
        out.write_bytes(resp.read())
    data = out.read_bytes()
    assert len(data) > 10_000, f"suspiciously small image ({len(data)} B)"
    assert data[:4] in (b"\x89PNG"[:4], b"\xff\xd8\xff\xe0"[:4], b"\xff\xd8\xff\xe1"[:4]) or data[:3] == b"\xff\xd8\xff", (
        f"not PNG/JPEG magic: {data[:8]!r}"
    )
    # Record which model id the live API accepted — Task 4 pins it in cfg.
    print(f"MODEL_USED={model_used} BYTES={len(data)} URI={art.uri}")

    backend._inner._delete(job_id)
```

- [ ] **Step 2: Verify SKIP + lockdown** — `pixi run pytest tests/live/test_luma_keyframe_live.py -q` → 1 skipped; `pixi run pytest tests/test_live_gating_lockdown.py -q` → green (extend its module list if it enumerates).

- [ ] **Step 3: Commit (RED-scaffold rule)**

```bash
git add tests/live/test_luma_keyframe_live.py
git commit -m "test(live): RED scaffold — Luma image keyframe smoke (env-gated)"
```

---

### Task 4: Live smoke + evidence + close-out

**Goal:** Green live generation on real Luma credit; entry logged; cfg pinned to the accepted model id; PROGRESS + memory closed; pushed with CI green.

**Files:**
- Create: `tests/live/evidence/2026-07-03_luma_keyframe_stdout.txt` (date = actual run date)
- Modify: `examples/configs/keyframe-luma.yaml` (pin accepted model id)
- Modify: `/workspace/successful-generations.md` (new entry — new mode axis `t2i` + new provider tuple)
- Modify: `/workspace/PROGRESS.md` (section + follow-up bookkeeping)
- Modify: memory `project_luma_video_retirement_2026.md` (mark carry-forward closed)

**Acceptance Criteria:**
- [ ] `KINOFORGE_LIVE_SPEND=1 pixi run pytest tests/live/test_luma_keyframe_live.py -v -s | tee tests/live/evidence/<date>_luma_keyframe_stdout.txt` → 1 passed; evidence shows `MODEL_USED=`.
- [ ] Example cfg model matches `MODEL_USED`.
- [ ] `successful-generations.md` gains a schema-conformant entry (tuple `(luma, LumaImageEngine, <model>, t2i)`); TOC updated.
- [ ] Full suite green; pushed; CI green.

**Verify:** evidence file `1 passed`; `git log origin/main..HEAD` empty after push; CI run conclusion `success`.

**Steps:**

- [ ] **Step 1: Preflight** — `pixi run preflight` → PASS (tree clean is the part that matters; no pods involved).
- [ ] **Step 2: Fire** — the tee command from the AC. Cost ~cents; no pod polling needed (hosted Bearer; poll cadence handled by backend).
- [ ] **Step 3: Pin model id** in `examples/configs/keyframe-luma.yaml` per `MODEL_USED`; rerun `tests/test_examples.py`.
- [ ] **Step 4: Log entry** in `successful-generations.md` per the file's schema preamble (Exact command / Cfg / Output incl. sha256 + dims if obtainable via `pixi run python -c "from PIL import Image..."` — if PIL absent, record bytes + magic only / Notes). New section, not See-also (new mode + provider tuple).
- [ ] **Step 5: PROGRESS + memory** — SHIPPED section at top of PROGRESS.md; update `project_luma_video_retirement_2026.md` body to state the engine half is closed (keep file, flip status line).
- [ ] **Step 6: Commit + push + CI watch**

```bash
git add -A && git commit -m "test(live): Luma image keyframe GREEN + entry #<N> + close-out"
git push origin main
```
