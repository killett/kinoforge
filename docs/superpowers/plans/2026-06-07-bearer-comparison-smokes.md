# Bearer-Provider Comparison Smokes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a kinoforge-owned `RemoteSubmitPollBackend` ABC plus per-provider subclasses (Replicate, Runway, Luma, fal-retrofit, Replicate-image) so a single `kinoforge batch` command produces four side-by-side MP4s per mode (t2v / i2v / flf2v) for the standard comparison prompt, with provider+model encoded into every output filename.

**Architecture:** New foundation ABC in `core/remote_backend.py` owns the universal submit-poll-fetch lifecycle (poll loop, AuthStrategy threading, error mapping, GenerationBackend public surface) and exposes 5 abstract subclass hooks. Each provider lazy-imports its official Python SDK inside method bodies (preserving the core-import-ban invariant) and implements the hooks against the SDK's response shape. Fal is retrofitted onto the same base at landing so the abstraction is validated against 4 real wire shapes. OutputSink Protocol gains additive `provider`/`model` named params; `LocalOutputSink` filenames extend to `{ts}_{provider}_{model}_{slug}{ext}`. Layer L `kinoforge batch` drives the comparison via a single manifest (12 video entries + 2 shared keyframe pre-stages via `ReplicateImageEngine`).

**Tech Stack:** Python 3.13, pixi, pydantic, official provider SDKs (`replicate`, `runwayml`, `lumaai`, `fal-client`) lazy-imported only inside per-provider engine modules.

**Foundation priority:** Foundation flexibility > minimal scope. Every cross-cutting feature added later (rate limiting, spend tracking, retry policy, webhook callbacks, telemetry) must have one home on `RemoteSubmitPollBackend`. This priority is what drove (a) Option D ABC over Option A loose per-provider engines, (b) fal retrofit at landing, (c) `ReplicateImageEngine` reusing the same base. A fresh implementer reading "rewrite FalEngine as RemoteSubmitPollBackend subclass" should not second-guess the scope — the retrofit is the proof that the abstraction handles 4 wire shapes, not 3.

**Autonomy posture:** Per the user's `feedback_autonomous_no_gates` memory, live smokes are pre-authorised up to a $20 session budget. NO live tasks in this plan are user-gates. Each live task runs preflight mechanically, then fires. Total projected spend across all live tasks: ~$2.32.

**Spec:** `docs/superpowers/specs/2026-06-07-bearer-comparison-smokes-design.md`

---

## File Structure

**New files:**

```
src/kinoforge/core/remote_backend.py
src/kinoforge/engines/replicate/__init__.py
src/kinoforge/engines/runway/__init__.py
src/kinoforge/engines/luma/__init__.py
src/kinoforge/image_engines/replicate/__init__.py

tests/core/test_remote_backend.py
tests/engines/test_replicate.py
tests/engines/test_runway.py
tests/engines/test_luma.py
tests/image_engines/__init__.py
tests/image_engines/test_replicate.py
tests/outputs/test_format_filename.py

tests/live/test_replicate_live.py
tests/live/test_runway_live.py
tests/live/test_luma_live.py
tests/live/test_comparison_batch_live.py

examples/configs/comparison/replicate-t2v.yaml
examples/configs/comparison/replicate-i2v.yaml
examples/configs/comparison/replicate-flf2v.yaml
examples/configs/comparison/runway-t2v.yaml
examples/configs/comparison/runway-i2v.yaml
examples/configs/comparison/runway-flf2v.yaml
examples/configs/comparison/luma-t2v.yaml
examples/configs/comparison/luma-i2v.yaml
examples/configs/comparison/luma-flf2v.yaml
examples/configs/comparison/fal-t2v.yaml
examples/configs/comparison/fal-i2v.yaml
examples/configs/comparison/fal-flf2v.yaml
examples/configs/comparison/keyframe-i2v.yaml
examples/configs/comparison/keyframe-flf2v.yaml
examples/configs/comparison/compare-all-providers.yaml
```

**Modified files:**

```
src/kinoforge/engines/fal/__init__.py              # rewritten as RemoteSubmitPollBackend subclass
src/kinoforge/outputs/base.py                       # OutputSink Protocol + format_filename signature
src/kinoforge/outputs/local.py                      # LocalOutputSink.publish forwards provider+model
src/kinoforge/pipeline/generate_clip.py             # GenerateClipStage threads provider+model to sink
src/kinoforge/_adapters.py                          # self-registration of 4 new engines
pixi.toml                                            # [feature.live-hosted.pypi-dependencies]
tools/preflight.py                                   # --check-hosted flag
tests/engines/test_fal.py                            # rewritten against new base
tests/live/test_fal_live.py                          # extended with i2v + flf2v
tests/outputs/test_local.py                          # provider+model field coverage
tests/test_core_invariant.py                         # vendor-SDK confinement + ABC stable surface

README.md                                            # Comparison Smokes section
PROGRESS.md                                          # Phase 43 (Layer 4) entry
```

---

## Task 0: `RemoteSubmitPollBackend` ABC + `RemoteSubmitPollEngine` ABC

**Goal:** Foundation ABCs in `core/remote_backend.py` that own the universal submit-poll-fetch lifecycle; subclass hooks for the 5 wire-shape-specific behaviors.

**Files:**
- Create: `src/kinoforge/core/remote_backend.py`
- Create: `tests/core/test_remote_backend.py`

**Acceptance Criteria:**
- [ ] `RemoteSubmitPollBackend(GenerationBackend)` exposes 5 abstract methods (`_submit`, `_poll_one`, `_is_done`, `_is_failed`, `_extract_output_url`) and 2 default-impl hooks (`_extract_filename`, `_endpoints_map`).
- [ ] `submit()` calls `_submit(self._client_factory(), job)`; returns its result as the job_id string.
- [ ] `result(job_id)` polls via `_poll_one` up to `max_poll` iterations, calling `_is_failed` and `_is_done` each iteration; raises `KinoforgeError(f"<provider>: {reason}")` on `_is_failed`; raises `TimeoutError` after `max_poll` exhaustion; returns `Artifact(filename=_extract_filename(status), url=_extract_output_url(status), meta={"job_id": job_id})` on done.
- [ ] `capabilities()` and `inspect_capabilities()` both return the constructor `probe_profile`.
- [ ] `endpoints()` returns `self._endpoints_map()`.
- [ ] `RemoteSubmitPollEngine(GenerationEngine)` with `requires_compute = False`, `requires_local_weights = False`; `provision(instance, cfg)` raises `KinoforgeError` if `instance is not None`, then calls `self._auth.credentials_present()` (raising `AuthError` when false); `backend(instance, cfg)` delegates to `self._build_backend(cfg, instance)`; `key_base(cfg)` returns `cfg["spec"]["model"]` (raising `ConfigError` when absent/empty); `extract_last_frame(artifact)` reuses `frames.ffmpeg_last_frame` against bytes fetched via `self._http_get_bytes(artifact.url)`.
- [ ] AuthStrategy is required at backend construction time; backend stores it and base `submit/result` make no direct credential calls (subclass hooks own SDK construction).
- [ ] All I/O seams injected: `client_factory: Callable[[], Any]`, `sleep: Callable[[float], None]`, `http_get_bytes: Callable[[str], bytes]` (engine only).

**Verify:** `pixi run pytest tests/core/test_remote_backend.py -v` → 11 passed

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/core/test_remote_backend.py
"""Tests for RemoteSubmitPollBackend + RemoteSubmitPollEngine ABCs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.core.errors import AuthError, ConfigError, KinoforgeError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationJob,
    ModelProfile,
    Segment,
)
from kinoforge.core.remote_backend import (
    RemoteSubmitPollBackend,
    RemoteSubmitPollEngine,
)


_PROFILE = ModelProfile(
    name="probe",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _job(spec: dict[str, Any] | None = None) -> GenerationJob:
    return GenerationJob(
        segments=[Segment(prompt="cat", params={}, assets=[])],
        spec=spec or {"model": "demo", "params": {}},
        params={},
    )


class _Backend(RemoteSubmitPollBackend):
    """Concrete subclass for hook-driven tests."""

    def __init__(self, *, statuses: list[dict[str, Any]], submit_id: str = "JOB-1", **kw):
        super().__init__(**kw)
        self._statuses = list(statuses)
        self._submit_id = submit_id
        self.submit_calls: list[GenerationJob] = []

    def _submit(self, client, job):
        self.submit_calls.append(job)
        return self._submit_id

    def _poll_one(self, client, job_id):
        return self._statuses.pop(0)

    def _is_done(self, status):
        return status.get("state") == "done"

    def _is_failed(self, status):
        if status.get("state") == "failed":
            return True, status.get("reason", "unknown")
        return False, ""

    def _extract_output_url(self, status):
        return str(status.get("url", ""))

    def _extract_filename(self, status):
        return str(status.get("filename", ""))


def _backend_factory(*, statuses, sleeps: list[float] | None = None, **kw) -> _Backend:
    sleep_calls = sleeps if sleeps is not None else []
    def _sleep(s: float) -> None:
        sleep_calls.append(s)
    return _Backend(
        statuses=statuses,
        client_factory=lambda: object(),
        sleep=_sleep,
        max_poll=4,
        poll_interval_s=0.25,
        probe_profile=_PROFILE,
        **kw,
    )


def test_submit_returns_job_id_from_hook():
    b = _backend_factory(statuses=[{"state": "done", "url": "https://x/v.mp4"}])
    job = _job()
    assert b.submit(job) == "JOB-1"
    assert b.submit_calls == [job]


def test_result_polls_until_done_and_returns_artifact():
    b = _backend_factory(
        statuses=[
            {"state": "running"},
            {"state": "running"},
            {"state": "done", "url": "https://x/v.mp4", "filename": "v.mp4"},
        ]
    )
    art = b.result("JOB-1")
    assert isinstance(art, Artifact)
    assert art.url == "https://x/v.mp4"
    assert art.filename == "v.mp4"
    assert art.meta == {"job_id": "JOB-1"}


def test_result_raises_kinoforge_error_on_failed():
    b = _backend_factory(
        statuses=[{"state": "failed", "reason": "OOM"}],
    )
    with pytest.raises(KinoforgeError, match="OOM"):
        b.result("JOB-1")


def test_result_raises_timeout_after_max_poll():
    b = _backend_factory(
        statuses=[{"state": "running"}] * 4,
    )
    with pytest.raises(TimeoutError):
        b.result("JOB-1")


def test_sleep_is_injected_not_real():
    sleeps: list[float] = []
    b = _backend_factory(
        statuses=[
            {"state": "running"},
            {"state": "done", "url": "https://x/v.mp4"},
        ],
        sleeps=sleeps,
    )
    b.result("JOB-1")
    assert sleeps == [0.25]


def test_capabilities_returns_probe():
    b = _backend_factory(statuses=[{"state": "done"}])
    assert b.capabilities() is _PROFILE
    assert b.inspect_capabilities() is _PROFILE


def test_endpoints_default_empty():
    b = _backend_factory(statuses=[{"state": "done"}])
    assert b.endpoints() == {}


# --- Engine ABC -----------------------------------------------------------


class _Engine(RemoteSubmitPollEngine):
    name = "demo"

    def _build_client_factory(self, cfg, creds):
        return lambda: object()

    def _build_backend(self, cfg, instance):
        return _backend_factory(statuses=[{"state": "done"}])


def test_engine_provision_rejects_non_none_instance():
    from kinoforge.core.auth import Bearer
    e = _Engine(auth=Bearer(env_var="X"))
    with pytest.raises(KinoforgeError):
        e.provision(object(), {"engine": {"demo": {}}, "spec": {"model": "m"}})


def test_engine_provision_raises_auth_error_when_creds_missing(monkeypatch):
    from kinoforge.core.auth import Bearer
    monkeypatch.delenv("DEMO_KEY", raising=False)
    e = _Engine(auth=Bearer(env_var="DEMO_KEY"))
    with pytest.raises(AuthError):
        e.provision(None, {"engine": {"demo": {}}, "spec": {"model": "m"}})


def test_engine_key_base_raises_config_error_on_missing_spec_model():
    from kinoforge.core.auth import Bearer
    e = _Engine(auth=Bearer(env_var="X"))
    with pytest.raises(ConfigError):
        e.key_base({"engine": {"demo": {}}, "spec": {}})


def test_engine_key_base_returns_spec_model():
    from kinoforge.core.auth import Bearer
    e = _Engine(auth=Bearer(env_var="X"))
    assert e.key_base({"spec": {"model": "wan-t2v"}}) == "wan-t2v"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_remote_backend.py -v`
Expected: ImportError (module doesn't exist) or collection error.

- [ ] **Step 3: Create `src/kinoforge/core/remote_backend.py`**

```python
"""RemoteSubmitPollBackend + RemoteSubmitPollEngine — foundation ABCs.

The submit-poll-fetch lifecycle every hosted video API follows. Subclasses
implement 5 wire-shape-specific hooks; the base class owns the poll loop,
AuthStrategy wiring, error mapping, and the public GenerationBackend +
GenerationEngine surfaces. Cross-cutting features (rate limiting, spend
tracking, retry policy, webhook callbacks, telemetry) bolt onto this single
foundation in future layers.

Stable contract — the public method set of both ABCs is locked by
``tests.test_core_invariant.test_remote_submit_poll_backend_abc_stable_surface``
against a checked-in baseline.
"""

from __future__ import annotations

import time
import urllib.request
from abc import abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kinoforge.core import frames
from kinoforge.core.errors import (
    AuthError,
    ConfigError,
    FrameExtractionError,
    KinoforgeError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    GenerationBackend,
    GenerationEngine,
    GenerationJob,
    Instance,
    ModelProfile,
    RenderedProvision,
)

if TYPE_CHECKING:
    from kinoforge.core.auth import AuthStrategy


def _urllib_get_bytes(url: str) -> bytes:
    """Default HTTP GET returning raw bytes."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())


class RemoteSubmitPollBackend(GenerationBackend):
    """Submit-poll-fetch lifecycle backend for hosted video APIs.

    Concrete subclasses implement five abstract hooks; the base class
    owns the poll loop, AuthStrategy wiring, error mapping, and the
    public GenerationBackend surface (``submit`` / ``result`` /
    ``capabilities`` / ``inspect_capabilities`` / ``endpoints``).
    """

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any],
        sleep: Callable[[float], None] = time.sleep,
        max_poll: int = 120,
        poll_interval_s: float = 2.0,
        probe_profile: ModelProfile,
    ) -> None:
        """Initialise the backend with injected lifecycle seams.

        Args:
            client_factory: Zero-arg callable returning a configured
                SDK client. Called lazily on first ``submit`` /
                ``result`` invocation so credential resolution can run
                at construction time without forcing SDK import.
            sleep: Injectable sleep between poll iterations.
            max_poll: Maximum poll iterations before TimeoutError.
            poll_interval_s: Seconds between poll iterations.
            probe_profile: ModelProfile returned by capability methods.
        """
        self._client_factory = client_factory
        self._sleep = sleep
        self._max_poll = max_poll
        self._poll_interval_s = poll_interval_s
        self._probe = probe_profile
        self._client_cached: Any = None

    # ------------------------------------------------------------------
    # Subclass hooks (abstract)
    # ------------------------------------------------------------------

    @abstractmethod
    def _submit(self, client: Any, job: GenerationJob) -> str:
        """Submit a job; return the provider's job id string."""

    @abstractmethod
    def _poll_one(self, client: Any, job_id: str) -> dict[str, Any]:
        """Fetch one status snapshot for ``job_id``; return a dict."""

    @abstractmethod
    def _is_done(self, status: dict[str, Any]) -> bool:
        """True when ``status`` indicates the job completed successfully."""

    @abstractmethod
    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(failed, reason)``; ``reason`` may be empty."""

    @abstractmethod
    def _extract_output_url(self, status: dict[str, Any]) -> str:
        """Return the output URL from a done ``status``."""

    # ------------------------------------------------------------------
    # Subclass hooks (default impls)
    # ------------------------------------------------------------------

    def _extract_filename(self, status: dict[str, Any]) -> str:
        """Return the provider's filename suggestion; default empty."""
        return ""

    def _endpoints_map(self) -> dict[str, str]:
        """Return a dict for :meth:`endpoints`; default empty."""
        return {}

    # ------------------------------------------------------------------
    # GenerationBackend interface (final — do not override)
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        if self._client_cached is None:
            self._client_cached = self._client_factory()
        return self._client_cached

    def submit(self, job: GenerationJob) -> str:
        """Build + submit the request via :meth:`_submit`."""
        return self._submit(self._client(), job)

    def result(self, job_id: str) -> Artifact:
        """Poll until done or failed; return an Artifact on done."""
        client = self._client()
        for _ in range(self._max_poll):
            status = self._poll_one(client, job_id)
            failed, reason = self._is_failed(status)
            if failed:
                raise KinoforgeError(
                    f"{type(self).__name__}: {reason or 'job failed'}"
                )
            if self._is_done(status):
                return Artifact(
                    filename=self._extract_filename(status),
                    url=self._extract_output_url(status),
                    meta={"job_id": job_id},
                    headers={},
                )
            self._sleep(self._poll_interval_s)
        raise TimeoutError(
            f"{type(self).__name__}: job {job_id!r} not done after "
            f"{self._max_poll} polls"
        )

    def capabilities(self) -> ModelProfile:
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        return self._probe

    def endpoints(self) -> dict[str, str]:
        return self._endpoints_map()


class RemoteSubmitPollEngine(GenerationEngine):
    """Companion ABC: hosted-no-compute engine wrapping the submit-poll backend.

    Subclasses implement two methods:

    - :meth:`_build_client_factory` — returns a zero-arg callable that
      constructs the provider's SDK client using ``Bearer.client_kwargs()``
      (or equivalent) from the AuthStrategy stashed at construction.
    - :meth:`_build_backend` — returns a configured
      :class:`RemoteSubmitPollBackend` subclass instance.
    """

    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(
        self,
        *,
        auth: AuthStrategy,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        probe_profile: ModelProfile | None = None,
        declared_flags_map: dict[str, dict[str, bool]] | None = None,
    ) -> None:
        """Initialise the engine.

        Args:
            auth: AuthStrategy used by :meth:`provision` to verify
                credentials and by :meth:`_build_client_factory` to
                build SDK kwargs.
            http_get_bytes: Injectable bytes-fetch seam for
                :meth:`extract_last_frame`.
            ffmpeg_run: Injectable subprocess seam for ffmpeg.
            probe_profile: ModelProfile (subclass may override).
            declared_flags_map: CapabilityKey-keyed flag map.
        """
        from kinoforge.core.interfaces import ModelProfile as _MP

        self._auth = auth
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
        self._probe = probe_profile or _MP(
            name=type(self).__name__,
            max_frames=81,
            fps=24,
            supported_modes={"t2v"},
            max_resolution=(1280, 720),
            supports_native_extension=False,
            supports_joint_audio=False,
        )
        self._declared_flags_map: dict[str, dict[str, bool]] = dict(
            declared_flags_map or {}
        )

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], Any]:
        """Return a zero-arg callable that builds the provider's SDK client."""

    @abstractmethod
    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        """Return a configured subclass backend instance."""

    # ------------------------------------------------------------------
    # GenerationEngine interface
    # ------------------------------------------------------------------

    def provision(self, instance: Instance | None, cfg: dict[str, Any]) -> None:
        if instance is not None:
            raise KinoforgeError(
                f"{type(self).__name__}.provision: instance must be None "
                "(hosted engine has no compute to configure)"
            )
        if not self._auth.credentials_present():
            raise AuthError(
                f"{type(self).__name__}: credentials not present "
                f"(strategy={type(self._auth).__name__})"
            )

    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> RemoteSubmitPollBackend:
        return self._build_backend(cfg, instance)

    def key_base(self, cfg: dict[str, Any]) -> str:
        spec = cfg.get("spec", {})
        model = str(spec.get("model", "")) if isinstance(spec, dict) else ""
        if not model:
            raise ConfigError(
                f"{type(self).__name__} requires spec.model at the top level "
                "of the YAML config"
            )
        return model

    def profile_for(self, key: CapabilityKey) -> ModelProfile:
        raise NotImplementedError(
            f"{type(self).__name__}.profile_for is supplied by ModelProfileProvider"
        )

    def declared_flags(self, key: CapabilityKey) -> dict[str, bool]:
        return dict(self._declared_flags_map.get(key.derive(), {}))

    def validate_spec(self, job: GenerationJob) -> None:
        """Default: require spec.model present. Subclasses extend."""
        if not job.spec.get("model"):
            from kinoforge.core.errors import ValidationError

            raise ValidationError(
                f"{type(self).__name__}: job.spec is missing 'model'"
            )

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty"
            )
        try:
            video_bytes = self._http_get_bytes(artifact.url)
        except Exception as exc:
            raise FrameExtractionError(
                f"{type(self).__name__}: fetch from {artifact.url!r} "
                f"failed: {exc}"
            ) from exc
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_remote_backend.py -v`
Expected: 11 passed.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/remote_backend.py tests/core/test_remote_backend.py
git add src/kinoforge/core/remote_backend.py tests/core/test_remote_backend.py
git commit -m "feat(core): RemoteSubmitPollBackend + RemoteSubmitPollEngine ABCs

Foundation for hosted video providers — owns the universal
submit-poll-fetch lifecycle. Subclasses implement 5 wire-shape-
specific hooks; base owns the poll loop, AuthStrategy wiring,
error mapping, and the public GenerationBackend +
GenerationEngine surfaces. Cross-cutting features (rate limiting,
spend tracking, retry, webhooks, telemetry) bolt onto this
foundation in future layers."
```

```json:metadata
{"files": ["src/kinoforge/core/remote_backend.py", "tests/core/test_remote_backend.py"], "verifyCommand": "pixi run pytest tests/core/test_remote_backend.py -v", "acceptanceCriteria": ["RemoteSubmitPollBackend ABC with 5 abstract hooks + 2 default hooks", "submit/result/capabilities/inspect_capabilities/endpoints public surface final", "RemoteSubmitPollEngine ABC with provision/backend/key_base/extract_last_frame", "Poll loop raises TimeoutError after max_poll and KinoforgeError on _is_failed", "AuthStrategy required at engine construction, threaded into provision()", "11 tests pass"]}
```

---

## Task 1: ABC stable-surface invariant + vendor-SDK confinement scan

**Goal:** Lock the public surface of `RemoteSubmitPollBackend` + `RemoteSubmitPollEngine` against a checked-in JSON baseline (mirror of `test_auth_strategy_abc_stable_surface`). Pre-stage the vendor-SDK confinement scan and core-import-ban for the engine modules that land in later tasks.

**Files:**
- Modify: `tests/test_core_invariant.py`
- Create: `tests/fixtures/remote_backend_abc_surface.json`

**Acceptance Criteria:**
- [ ] `test_remote_submit_poll_backend_abc_stable_surface` reads the baseline JSON and asserts the current ABC's public method names + signatures match byte-for-byte.
- [ ] `test_vendor_sdk_confinement` extended: `replicate` only in `engines/replicate/` + `image_engines/replicate/`; `runwayml` only in `engines/runway/`; `lumaai` only in `engines/luma/`; `fal_client` only in `engines/fal/`.
- [ ] `test_no_concrete_adapter_imports_in_core` extended to scan `kinoforge.image_engines.*` (already covers `kinoforge.engines.*`).
- [ ] Baseline JSON pretty-printed; any future signature drift surfaces in `git diff` of this fixture.

**Verify:** `pixi run pytest tests/test_core_invariant.py -v` → all invariant tests pass.

**Steps:**

- [ ] **Step 1: Generate baseline JSON**

Run the following once locally (or inline in a Python REPL) to extract the canonical baseline:

```python
import inspect
import json
from kinoforge.core.remote_backend import (
    RemoteSubmitPollBackend,
    RemoteSubmitPollEngine,
)

def _sig(cls):
    out = {}
    for name in sorted(dir(cls)):
        if name.startswith("_") and not name.startswith("__"):
            # Capture single-underscore hooks too (subclass contract).
            pass
        elif name.startswith("__"):
            continue
        obj = getattr(cls, name)
        if not callable(obj):
            continue
        try:
            out[name] = str(inspect.signature(obj))
        except (ValueError, TypeError):
            out[name] = "<unintrospectable>"
    return out

baseline = {
    "RemoteSubmitPollBackend": _sig(RemoteSubmitPollBackend),
    "RemoteSubmitPollEngine": _sig(RemoteSubmitPollEngine),
}
print(json.dumps(baseline, indent=2, sort_keys=True))
```

Save the resulting JSON to `tests/fixtures/remote_backend_abc_surface.json`.

- [ ] **Step 2: Write the failing invariant test**

Add to `tests/test_core_invariant.py`:

```python
def test_remote_submit_poll_backend_abc_stable_surface():
    """Lock the RemoteSubmitPollBackend + Engine public surface.

    Any change here is intentional contract drift — update the
    baseline fixture in the same commit so reviewers can see the
    surface change.
    """
    import inspect
    import json
    from pathlib import Path

    from kinoforge.core.remote_backend import (
        RemoteSubmitPollBackend,
        RemoteSubmitPollEngine,
    )

    def _sig(cls):
        out = {}
        for name in sorted(dir(cls)):
            if name.startswith("__"):
                continue
            obj = getattr(cls, name)
            if not callable(obj):
                continue
            try:
                out[name] = str(inspect.signature(obj))
            except (ValueError, TypeError):
                out[name] = "<unintrospectable>"
        return out

    actual = {
        "RemoteSubmitPollBackend": _sig(RemoteSubmitPollBackend),
        "RemoteSubmitPollEngine": _sig(RemoteSubmitPollEngine),
    }
    baseline_path = Path("tests/fixtures/remote_backend_abc_surface.json")
    baseline = json.loads(baseline_path.read_text())
    assert actual == baseline, (
        "RemoteSubmitPollBackend / RemoteSubmitPollEngine public surface "
        "drifted from the locked baseline. If this is intentional, "
        f"regenerate {baseline_path} in the same commit."
    )
```

- [ ] **Step 3: Extend `test_vendor_sdk_confinement`**

Locate the existing test (already covers `sky`/`skypilot` and `runpod`). Append the four new SDKs:

```python
# Inside test_vendor_sdk_confinement:
_SDK_RULES = [
    # ... existing entries ...
    ("replicate", ("src/kinoforge/engines/replicate/", "src/kinoforge/image_engines/replicate/")),
    ("runwayml", ("src/kinoforge/engines/runway/",)),
    ("lumaai", ("src/kinoforge/engines/luma/",)),
    ("fal_client", ("src/kinoforge/engines/fal/",)),
]
```

- [ ] **Step 4: Extend core-import-ban scan**

Locate `test_no_concrete_adapter_imports_in_core` (already scans `kinoforge.providers.`, `kinoforge.sources.`, `kinoforge.engines.`). Add `kinoforge.image_engines.` to the forbidden-prefix list.

- [ ] **Step 5: Run tests**

Run: `pixi run pytest tests/test_core_invariant.py -v`
Expected: every invariant test passes; new ABC-surface test passes against the fresh baseline.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/test_core_invariant.py tests/fixtures/remote_backend_abc_surface.json
git add tests/test_core_invariant.py tests/fixtures/remote_backend_abc_surface.json
git commit -m "test(invariant): lock RemoteSubmitPollBackend ABC surface; extend SDK-confinement scans

Adds JSON-baselined stable-surface lockdown for the new
foundation ABCs (mirrors test_auth_strategy_abc_stable_surface).
Pre-stages vendor-SDK confinement entries for replicate /
runwayml / lumaai / fal_client and adds kinoforge.image_engines
to the core-import-ban forbidden-prefix list, so the engine
landings in later tasks immediately benefit from the
invariants."
```

```json:metadata
{"files": ["tests/test_core_invariant.py", "tests/fixtures/remote_backend_abc_surface.json"], "verifyCommand": "pixi run pytest tests/test_core_invariant.py -v", "acceptanceCriteria": ["ABC stable-surface test reads baseline JSON and asserts byte-equality", "Vendor-SDK confinement extended for 4 new SDKs", "Core-import-ban extended for kinoforge.image_engines.*", "All invariant tests pass"]}
```

---

## Task 2: OutputSink Protocol + `format_filename` extension + `LocalOutputSink` update

**Goal:** Additive `provider` / `model` named params on `OutputSink.publish`; new `format_filename` signature `(ts, provider, model, slug, extension)`; `LocalOutputSink.publish` forwards through with `"unknown"` fallback.

**Files:**
- Modify: `src/kinoforge/outputs/base.py`
- Modify: `src/kinoforge/outputs/local.py`
- Modify: `tests/outputs/test_local.py`
- Create: `tests/outputs/test_format_filename.py`

**Acceptance Criteria:**
- [ ] `OutputSink.publish` Protocol gains `provider: str | None = None` and `model: str | None = None` named-only params (kwargs after `*`).
- [ ] `format_filename(*, ts, provider, model, slug, extension) -> str` returns `f"{ts}_{provider}_{model}_{slug}{extension}"`.
- [ ] `LocalOutputSink.publish` slugifies `provider` (max 20) and `model` (max 24); when either is `None` or empty after slugify, substitutes the literal string `"unknown"`; passes both to `format_filename`.
- [ ] Existing single-provider tests that don't pass provider/model continue to pass with `unknown_unknown` infix.
- [ ] Collision suffix loop unchanged and still verified.
- [ ] New `test_format_filename.py` covers: t2v happy path, `None` fallbacks, slug-overflow truncation, extension preserved verbatim.

**Verify:** `pixi run pytest tests/outputs/ -v` → all green.

**Steps:**

- [ ] **Step 1: Write failing tests in `tests/outputs/test_format_filename.py`**

```python
"""Tests for format_filename helper (Layer 4 schema)."""

from kinoforge.outputs.base import format_filename


def test_format_filename_happy_path():
    assert format_filename(
        ts="20260607-143015",
        provider="replicate",
        model="wan-t2v-1-3b",
        slug="photorealistic-c",
        extension=".mp4",
    ) == "20260607-143015_replicate_wan-t2v-1-3b_photorealistic-c.mp4"


def test_format_filename_empty_slug():
    assert format_filename(
        ts="20260607-143015",
        provider="luma",
        model="ray-2",
        slug="",
        extension=".mp4",
    ) == "20260607-143015_luma_ray-2_.mp4"


def test_format_filename_preserves_extension_verbatim():
    assert format_filename(
        ts="20260607-143015",
        provider="runway",
        model="gen3a-turbo",
        slug="x",
        extension=".png",
    ).endswith(".png")


def test_format_filename_no_sanitisation_in_helper():
    # Helper does NOT slugify; LocalOutputSink owns sanitisation.
    out = format_filename(
        ts="20260607-143015",
        provider="WEIRD/PROV",
        model="m/v:1",
        slug="x",
        extension=".mp4",
    )
    assert "WEIRD/PROV" in out
    assert "m/v:1" in out


def test_format_filename_unknown_marker_round_trip():
    # The literal "unknown" sentinel is just a string at this layer.
    assert format_filename(
        ts="20260607-143015",
        provider="unknown",
        model="unknown",
        slug="cat",
        extension=".mp4",
    ) == "20260607-143015_unknown_unknown_cat.mp4"


def test_format_filename_underscore_count_stable():
    # Schema is exactly 3 underscores between fixed fields + extension.
    out = format_filename(
        ts="A",
        provider="B",
        model="C",
        slug="D",
        extension=".e",
    )
    # A_B_C_D.e — three underscores
    assert out.count("_") == 3
```

- [ ] **Step 2: Extend `tests/outputs/test_local.py`**

Add inside the existing test module:

```python
def test_publish_with_provider_and_model_in_filename(tmp_path):
    from kinoforge.outputs.local import LocalOutputSink

    sink = LocalOutputSink(root=tmp_path)
    out = sink.publish(
        b"\x00\x00\x00\x18ftypisom...",
        prompt="a cat sitting on a fence",
        extension=".mp4",
        provider="replicate",
        model="wan-video/wan-t2v-1.3b",
    )
    out_path = Path(out)
    # Filename schema: {ts}_{provider}_{model-slug}_{prompt-slug}.mp4
    parts = out_path.stem.split("_")
    assert len(parts) >= 4
    assert parts[1] == "replicate"
    assert parts[2].startswith("wan-video-wan-t2v")  # slugified, / → -


def test_publish_without_provider_or_model_uses_unknown(tmp_path):
    from kinoforge.outputs.local import LocalOutputSink

    sink = LocalOutputSink(root=tmp_path)
    out = sink.publish(
        b"x",
        prompt="cat",
        extension=".mp4",
    )
    assert "_unknown_unknown_" in Path(out).name


def test_publish_collision_suffix_still_works_with_provider(tmp_path):
    from kinoforge.outputs.local import LocalOutputSink

    sink = LocalOutputSink(root=tmp_path)
    a = sink.publish(b"x", prompt="cat", extension=".mp4",
                     provider="luma", model="ray-2")
    b = sink.publish(b"y", prompt="cat", extension=".mp4",
                     provider="luma", model="ray-2")
    assert a != b
    assert Path(a).exists() and Path(b).exists()
```

- [ ] **Step 3: Run failing tests**

Run: `pixi run pytest tests/outputs/ -v`
Expected: new test_format_filename collection fails (helper signature mismatch) + new test_local cases fail (publish signature mismatch).

- [ ] **Step 4: Update `src/kinoforge/outputs/base.py`**

Replace the existing `OutputSink` Protocol's `publish` method signature and the `format_filename` function:

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
    ) -> str:
        """Publish *data* under a name derived from *prompt*, *provider*, *model*.

        Args:
            data: The raw clip bytes to write.
            prompt: User-facing prompt; first 20 ASCII-safe chars become the slug.
            extension: File suffix including the dot.
            namespace: Optional sub-directory under the sink's root.
            provider: Engine registry key (``replicate`` / ``runway`` / ``luma``
                / ``fal``). ``None`` or empty falls back to the literal ``unknown``.
            model: ``cfg["spec"]["model"]`` slugified to max 24 chars. ``None``
                or empty falls back to the literal ``unknown``.
        """
        ...


def format_filename(
    *,
    ts: str,
    provider: str,
    model: str,
    slug: str,
    extension: str,
) -> str:
    """Compose ``{ts}_{provider}_{model}_{slug}{extension}``.

    Caller MUST pre-slugify ``provider``, ``model``, and ``slug``;
    this helper performs no sanitisation.
    """
    return f"{ts}_{provider}_{model}_{slug}{extension}"
```

- [ ] **Step 5: Update `src/kinoforge/outputs/local.py`**

Update `LocalOutputSink.publish` to accept `provider` + `model`, slugify each, and pass through. Substitute `"unknown"` when input is `None` or empty after slugify.

```python
# Inside LocalOutputSink.publish, at the top of the method, after existing
# argument capture but before format_filename is called:

provider_slug = slugify(provider or "", max_chars=20) if provider else ""
model_slug = slugify(model or "", max_chars=24) if model else ""
if not provider_slug:
    provider_slug = "unknown"
if not model_slug:
    model_slug = "unknown"
```

Then pass them through to `format_filename(...)` per the new signature.

The collision-suffix loop downstream of `format_filename` is unchanged.

- [ ] **Step 6: Run tests**

Run: `pixi run pytest tests/outputs/ -v`
Expected: all green.

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/outputs/base.py src/kinoforge/outputs/local.py tests/outputs/test_format_filename.py tests/outputs/test_local.py
git add src/kinoforge/outputs/base.py src/kinoforge/outputs/local.py tests/outputs/test_format_filename.py tests/outputs/test_local.py
git commit -m "feat(outputs): provider+model in filename schema

OutputSink Protocol gains additive provider/model named params
with None defaults (backward-compatible). format_filename
signature becomes (ts, provider, model, slug, extension);
single caller (LocalOutputSink.publish) updated atomically.
Empty/None provider or model substitutes literal 'unknown'
so the filename schema is stable across configs that have not
yet wired the new fields."
```

```json:metadata
{"files": ["src/kinoforge/outputs/base.py", "src/kinoforge/outputs/local.py", "tests/outputs/test_format_filename.py", "tests/outputs/test_local.py"], "verifyCommand": "pixi run pytest tests/outputs/ -v", "acceptanceCriteria": ["OutputSink.publish gains provider/model named-only params with None defaults", "format_filename signature is (ts, provider, model, slug, extension)", "LocalOutputSink slugifies + falls back to 'unknown'", "Collision suffix loop preserved", "All output tests green"]}
```

---

## Task 3: `pixi.toml` `live-hosted` feature env + `preflight --check-hosted`

**Goal:** Three new lazy SDKs available via a `live-hosted` feature env so `pixi run test` default env stays lean. `tools/preflight.py` gains a `--check-hosted` flag that asserts the four Bearer env vars present (`REPLICATE_API_TOKEN`, `RUNWAYML_API_SECRET`, `LUMAAI_API_KEY`, `FAL_KEY`).

**Files:**
- Modify: `pixi.toml`
- Modify: `tools/preflight.py`
- Create: `tests/tools/test_preflight_hosted.py`

**Acceptance Criteria:**
- [ ] `pixi.toml` has `[feature.live-hosted.pypi-dependencies]` listing `replicate >= 1.0.0`, `runwayml >= 3.0.0`, `lumaai >= 1.0.0`, `fal-client >= 0.5.0`. `live-hosted` feature wired into a `live-hosted` env identical to the existing `live-skypilot` pattern.
- [ ] `pixi.lock` regenerates cleanly.
- [ ] `tools/preflight.py` adds an argparse flag `--check-hosted` (default off). When set, the script additionally asserts each of the four env vars is non-empty.
- [ ] When `--check-hosted` is set but any of the four vars is missing, the script exits non-zero and prints the missing var name(s).
- [ ] Existing preflight invocations (without `--check-hosted`) behave identically — zero behavior change for the existing live-RunPod / live-SkyPilot flows.

**Verify:** `pixi run pytest tests/tools/test_preflight_hosted.py -v` → 4 passed; `pixi run -e live-hosted python -c "import replicate, runwayml, lumaai, fal_client; print('ok')"` → `ok`.

**Steps:**

- [ ] **Step 1: Write failing tests in `tests/tools/test_preflight_hosted.py`**

```python
"""Tests for `preflight --check-hosted` env-var gate."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_PREFLIGHT = Path(__file__).resolve().parents[2] / "tools" / "preflight.py"
_HOSTED_VARS = ("REPLICATE_API_TOKEN", "RUNWAYML_API_SECRET",
                "LUMAAI_API_KEY", "FAL_KEY")


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_PREFLIGHT), *args],
        env={**env},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_preflight_without_check_hosted_ignores_missing_keys(monkeypatch, tmp_path):
    # Baseline: pre-existing preflight behavior unchanged when --check-hosted
    # is not passed. We assert that the script does NOT fail on missing
    # hosted vars in the default invocation.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    # Strip any of the four hosted vars from the env we pass to the subprocess.
    for var in _HOSTED_VARS:
        env.pop(var, None)
    result = _run(env)  # no --check-hosted
    # Pre-existing exit codes have semantic meaning (clean=0, dirty=1, etc.);
    # we only assert the script DID NOT emit a 'missing hosted credential'
    # complaint on stderr.
    assert "REPLICATE_API_TOKEN" not in result.stderr


def test_preflight_check_hosted_passes_when_all_four_set(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REPLICATE_API_TOKEN": "x",
        "RUNWAYML_API_SECRET": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
        # Bypass other preflight checks that depend on git state etc. by
        # marking ignore-other-checks if such a flag exists; otherwise
        # we accept whatever exit code the script returns AS LONG AS no
        # hosted-cred complaint is emitted.
    }
    result = _run(env, "--check-hosted")
    for var in _HOSTED_VARS:
        assert f"missing {var}" not in result.stderr.lower()


def test_preflight_check_hosted_fails_on_missing_replicate(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNWAYML_API_SECRET": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
    }
    result = _run(env, "--check-hosted")
    assert result.returncode != 0
    assert "REPLICATE_API_TOKEN" in result.stderr


def test_preflight_check_hosted_fails_on_missing_runway(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "REPLICATE_API_TOKEN": "x",
        "LUMAAI_API_KEY": "x",
        "FAL_KEY": "x",
    }
    result = _run(env, "--check-hosted")
    assert result.returncode != 0
    assert "RUNWAYML_API_SECRET" in result.stderr
```

- [ ] **Step 2: Run failing tests**

Run: `pixi run pytest tests/tools/test_preflight_hosted.py -v`
Expected: tests fail because `--check-hosted` flag does not exist.

- [ ] **Step 3: Add the flag to `tools/preflight.py`**

Locate the argparse setup and add:

```python
parser.add_argument(
    "--check-hosted",
    action="store_true",
    help=(
        "Verify hosted Bearer credentials are present in env: "
        "REPLICATE_API_TOKEN, RUNWAYML_API_SECRET, LUMAAI_API_KEY, FAL_KEY"
    ),
)
```

After argparse-parse, add the gate:

```python
if args.check_hosted:
    _hosted_required = (
        "REPLICATE_API_TOKEN",
        "RUNWAYML_API_SECRET",
        "LUMAAI_API_KEY",
        "FAL_KEY",
    )
    missing = [v for v in _hosted_required if not os.environ.get(v)]
    if missing:
        for var in missing:
            print(f"preflight: missing {var}", file=sys.stderr)
        sys.exit(2)
```

- [ ] **Step 4: Add `live-hosted` feature env to `pixi.toml`**

Locate the existing `[feature.live-skypilot.*]` blocks and add (mirror the same pattern):

```toml
[feature.live-hosted.pypi-dependencies]
replicate = ">=1.0.0"
runwayml = ">=3.0.0"
lumaai = ">=1.0.0"
fal-client = ">=0.5.0"
```

And add the env composition:

```toml
[environments]
# ... existing entries ...
live-hosted = ["live-hosted"]
```

- [ ] **Step 5: Regenerate `pixi.lock`**

Run: `pixi install -e live-hosted`
Expected: clean lockfile regen. If a CVE-bypass entry under `[pypi-exclude-newer]` is needed for any of these SDKs, add per the verified per-package override syntax already in `pixi.toml`.

- [ ] **Step 6: Verify the env imports cleanly**

Run: `pixi run -e live-hosted python -c "import replicate, runwayml, lumaai, fal_client; print('ok')"`
Expected: `ok`.

- [ ] **Step 7: Run all tests**

Run: `pixi run pytest tests/tools/test_preflight_hosted.py -v`
Expected: 4 passed.

- [ ] **Step 8: Pre-commit + commit (stage pixi.lock alongside pixi.toml)**

```bash
pixi run pre-commit run --files pixi.toml pixi.lock tools/preflight.py tests/tools/test_preflight_hosted.py
git add pixi.toml pixi.lock tools/preflight.py tests/tools/test_preflight_hosted.py
git commit -m "feat(infra): live-hosted pixi feature env + preflight --check-hosted

Three SDKs (replicate, runwayml, lumaai) + fal-client wired
behind the live-hosted feature env so default pixi run test
stays lean. preflight.py gains a --check-hosted flag that
asserts the four Bearer env vars are present; exits 2 with
missing-var names on stderr when any is missing. Pre-existing
preflight behavior unchanged when the flag is absent."
```

```json:metadata
{"files": ["pixi.toml", "pixi.lock", "tools/preflight.py", "tests/tools/test_preflight_hosted.py"], "verifyCommand": "pixi run pytest tests/tools/test_preflight_hosted.py -v", "acceptanceCriteria": ["pixi.toml has [feature.live-hosted.pypi-dependencies] with 4 SDKs", "pixi.lock regenerates cleanly", "preflight --check-hosted fails on any missing var", "Existing preflight invocations unchanged"]}
```

---

## Task 4: `ReplicateEngine` + `ReplicateBackend` + `FakeReplicateClient`

**Goal:** First per-provider subclass landing — proves the Task 0 abstraction against a real wire shape. Replicate Bearer auth via `REPLICATE_API_TOKEN`; SDK lazy-imported inside method bodies; self-registers under `"replicate"` in `_adapters.py`.

**Files:**
- Create: `src/kinoforge/engines/replicate/__init__.py`
- Create: `tests/engines/test_replicate.py`
- Modify: `src/kinoforge/_adapters.py`

**Acceptance Criteria:**
- [ ] `ReplicateBackend` implements all 5 abstract hooks per spec §4.1.
- [ ] `_extract_output_url` handles BOTH `output: str` and `output: list[str]` shapes (unwraps `[0]` when list).
- [ ] `_inject_assets` writes asset URIs to provider-specific input fields: `init_image → input["image"]`, `start_image → input["start_image"]`, `end_image → input["end_image"]`.
- [ ] `ReplicateEngine._build_client_factory` returns a zero-arg callable; the callable lazy-imports `replicate` and constructs `replicate.Client(**Bearer.client_kwargs())`.
- [ ] Self-registers under `"replicate"` via `registry.register_engine`.
- [ ] Self-registration triggered from `_adapters.py` (concrete-import hub).
- [ ] `FakeReplicateClient` shape: `client.predictions.create(version, input)` returns `_FakePrediction`; `client.predictions.get(id)` returns next response from a pre-loaded list.
- [ ] 12 tests cover: submit shape, poll path, done/failed/timeout, str output, list output, all 3 asset role injections, auth-failure mapping, registry self-registration.

**Verify:** `pixi run pytest tests/engines/test_replicate.py -v` → 12 passed.

**Steps:**

- [ ] **Step 1: Write failing tests (illustrative shape — full test count = 12)**

```python
# tests/engines/test_replicate.py
"""Tests for ReplicateEngine + ReplicateBackend via FakeReplicateClient."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.auth import Bearer
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError
from kinoforge.core.interfaces import (
    AssetRef,
    ConditioningAsset,
    GenerationJob,
    Segment,
)


# --- Fakes ---------------------------------------------------------------


class _FakePrediction:
    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data.get("id", "PRED-1")
        self.status = data.get("status", "succeeded")
        self.output = data.get("output", "https://x/v.mp4")
        self.error = data.get("error")


class _FakePredictionsAPI:
    def __init__(self, *, create_response: dict, get_responses: list[dict]) -> None:
        self._create = create_response
        self._gets = list(get_responses)
        self.create_calls: list[dict] = []

    def create(self, **kw: Any) -> _FakePrediction:
        self.create_calls.append(kw)
        return _FakePrediction(self._create)

    def get(self, pred_id: str) -> _FakePrediction:
        if not self._gets:
            raise IndexError("FakeReplicateClient: ran out of get responses")
        return _FakePrediction(self._gets.pop(0))


class FakeReplicateClient:
    def __init__(
        self,
        *,
        predictions_create_response: dict,
        predictions_get_responses: list[dict],
    ) -> None:
        self.predictions = _FakePredictionsAPI(
            create_response=predictions_create_response,
            get_responses=predictions_get_responses,
        )


# --- Helpers -------------------------------------------------------------


def _job(spec, assets=()):
    return GenerationJob(
        segments=[Segment(prompt="cat", params={}, assets=list(assets))],
        spec=spec,
        params={},
    )


def _backend(*, create, polls, asset_paths=None):
    from kinoforge.engines.replicate import ReplicateBackend
    client = FakeReplicateClient(
        predictions_create_response=create,
        predictions_get_responses=polls,
    )
    return ReplicateBackend(
        client_factory=lambda: client,
        sleep=lambda _s: None,
        max_poll=8,
        poll_interval_s=0.0,
        probe_profile=_PROBE,
    )


# (full test bodies — 12 cases total — covering: submit happy path,
#  poll-once-done, poll-three-then-done, FAILED mapping, TIMEOUT after
#  max_poll, output-as-string vs output-as-list, three asset-role
#  injections, AuthError on missing REPLICATE_API_TOKEN at provision,
#  registry self-registration via `from kinoforge.engines.replicate
#  import ReplicateEngine; assert registry.get_engine("replicate")() ...`)
```

- [ ] **Step 2: Run failing tests**

Run: `pixi run pytest tests/engines/test_replicate.py -v`
Expected: ImportError (module missing).

- [ ] **Step 3: Create `src/kinoforge/engines/replicate/__init__.py`**

```python
"""ReplicateEngine + ReplicateBackend — hosted Bearer adapter for replicate.com.

Lazy-imports the official `replicate` SDK inside method bodies to preserve
the core-import-ban invariant. Self-registers under "replicate".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from kinoforge.core import registry
from kinoforge.core.auth import Bearer
from kinoforge.core.errors import AuthError
from kinoforge.core.interfaces import (
    CredentialProvider,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.prompt_routing import resolve_prompt
from kinoforge.core.remote_backend import (
    RemoteSubmitPollBackend,
    RemoteSubmitPollEngine,
)


_PROBE = ModelProfile(
    name="replicate",
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v", "flf2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


class ReplicateBackend(RemoteSubmitPollBackend):
    """Submit/poll backend for Replicate predictions API."""

    def _submit(self, client: Any, job: GenerationJob) -> str:
        # `version` is the model ref (e.g. "wan-video/wan-2.1-t2v-1.3b").
        version = job.spec["model"]
        input_dict: dict[str, Any] = {
            "prompt": resolve_prompt(job) or "",
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(input_dict, job)
        try:
            pred = client.predictions.create(version=version, input=input_dict)
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("replicate.predictions.create", exc)
        return str(pred.id)

    def _poll_one(self, client: Any, job_id: str) -> dict[str, Any]:
        try:
            pred = client.predictions.get(job_id)
        except Exception as exc:  # noqa: BLE001
            self._raise_for_sdk_error("replicate.predictions.get", exc)
        return {
            "id": pred.id,
            "status": pred.status,
            "output": pred.output,
            "error": pred.error,
        }

    def _is_done(self, status: dict[str, Any]) -> bool:
        return status.get("status") == "succeeded"

    def _is_failed(self, status: dict[str, Any]) -> tuple[bool, str]:
        if status.get("status") == "failed":
            return True, str(status.get("error") or "replicate prediction failed")
        return False, ""

    def _extract_output_url(self, status: dict[str, Any]) -> str:
        out = status.get("output")
        if isinstance(out, list):
            return str(out[0]) if out else ""
        return str(out) if out else ""

    def _extract_filename(self, status: dict[str, Any]) -> str:
        return ""

    # --- helpers --------------------------------------------------------

    def _inject_assets(self, input_dict: dict[str, Any], job: GenerationJob) -> None:
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role == "init_image":
                input_dict["image"] = asset.ref.uri
            elif asset.role == "start_image":
                input_dict["start_image"] = asset.ref.uri
            elif asset.role == "end_image":
                input_dict["end_image"] = asset.ref.uri

    def _raise_for_sdk_error(self, op: str, exc: BaseException) -> None:
        import replicate  # lazy

        # Replicate raises replicate.exceptions.ReplicateError with .status.
        if isinstance(exc, replicate.exceptions.ReplicateError):
            status = getattr(exc, "status", None)
            if status in (401, 403):
                raise AuthError(f"replicate auth failed: {exc}") from exc
        raise RuntimeError(f"replicate: {op} failed: {exc}") from exc


class ReplicateEngine(RemoteSubmitPollEngine):
    name: str = "replicate"

    def _build_client_factory(
        self, cfg: dict[str, Any], creds: CredentialProvider | None
    ) -> Callable[[], Any]:
        kwargs = self._auth.client_kwargs()
        if not kwargs.get("api_key"):
            raise AuthError("replicate: REPLICATE_API_TOKEN is empty")

        def _factory() -> Any:
            import replicate  # lazy
            return replicate.Client(**kwargs)

        return _factory

    def _build_backend(
        self, cfg: dict[str, Any], instance: Instance | None
    ) -> RemoteSubmitPollBackend:
        del instance
        return ReplicateBackend(
            client_factory=self._build_client_factory(cfg, None),
            probe_profile=self._probe,
        )


def _default_factory() -> ReplicateEngine:
    from kinoforge.core.credentials import EnvCredentialProvider

    return ReplicateEngine(
        auth=Bearer(
            env_var="REPLICATE_API_TOKEN",
            credential_provider=EnvCredentialProvider(),
        ),
    )


registry.register_engine("replicate", _default_factory)
```

- [ ] **Step 4: Add to `src/kinoforge/_adapters.py`**

Add the import line in the existing engine-import block:

```python
import kinoforge.engines.replicate  # noqa: F401 — self-registers under "replicate"
```

- [ ] **Step 5: Run tests**

Run: `pixi run pytest tests/engines/test_replicate.py -v`
Expected: 12 passed.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/replicate/__init__.py src/kinoforge/_adapters.py tests/engines/test_replicate.py
git add src/kinoforge/engines/replicate/__init__.py src/kinoforge/_adapters.py tests/engines/test_replicate.py
git commit -m "feat(engines): ReplicateEngine via RemoteSubmitPollBackend

First per-provider subclass — proves the Task 0 abstraction
against Replicate predictions API. Lazy-imports replicate SDK
inside method bodies (core-import-ban preserved). Self-registers
under 'replicate'. _extract_output_url handles both str and
list[str] output shapes. Asset injection covers init_image /
start_image / end_image roles."
```

```json:metadata
{"files": ["src/kinoforge/engines/replicate/__init__.py", "src/kinoforge/_adapters.py", "tests/engines/test_replicate.py"], "verifyCommand": "pixi run pytest tests/engines/test_replicate.py -v", "acceptanceCriteria": ["ReplicateBackend implements all 5 abstract hooks", "_extract_output_url handles str and list output", "_inject_assets maps init/start/end image roles", "Engine self-registers under 'replicate'", "12 tests pass"]}
```

---

## Task 5: `RunwayEngine` + `RunwayBackend` + `FakeRunwayClient`

**Goal:** Second per-provider subclass. Runway Bearer via `RUNWAYML_API_SECRET`. `X-Runway-Version` header injection is handled inside the `runwayml` SDK — no manual header work needed at the kinoforge layer.

**Files:**
- Create: `src/kinoforge/engines/runway/__init__.py`
- Create: `tests/engines/test_runway.py`
- Modify: `src/kinoforge/_adapters.py`

**Acceptance Criteria:**
- [ ] Subclass implements all 5 abstract hooks per spec §4.2.
- [ ] Status enum is uppercase: `SUCCEEDED` / `FAILED` / `RUNNING` / `PENDING`. `_is_done` matches `SUCCEEDED` exactly; `_is_failed` matches `FAILED`.
- [ ] `_submit` dispatches on `job.spec.get("mode")`: `t2v` → `client.text_to_video.create`; `i2v` / `flf2v` → `client.image_to_video.create`.
- [ ] Asset injection: `init_image → prompt_image`, `start_image → first_image`, `end_image → last_image`.
- [ ] `_extract_output_url` returns `output[0]` (always a list per Runway shape).
- [ ] `runwayml.APIError` with 401/403 → `AuthError("runway auth failed: ...")`.
- [ ] Self-registers under `"runway"`; added to `_adapters.py`.
- [ ] 12 tests cover all of the above.

**Verify:** `pixi run pytest tests/engines/test_runway.py -v` → 12 passed.

**Steps:**

- [ ] **Step 1: Write failing tests** (mirror Task 4 shape; FakeRunwayClient exposes `text_to_video.create`, `image_to_video.create`, `tasks.retrieve`).

- [ ] **Step 2: Create `src/kinoforge/engines/runway/__init__.py`**

Mirror Task 4's structure with these hook bodies:

```python
def _submit(self, client, job):
    model = job.spec["model"]
    mode = (job.spec.get("mode") or "t2v").lower()
    prompt = resolve_prompt(job) or ""
    base_kw = {"model": model, **(job.spec.get("params") or {})}
    self._inject_assets(base_kw, job)
    try:
        if mode == "t2v":
            task = client.text_to_video.create(prompt_text=prompt, **base_kw)
        else:  # i2v / flf2v
            task = client.image_to_video.create(prompt_text=prompt, **base_kw)
    except Exception as exc:  # noqa: BLE001
        self._raise_for_sdk_error("runway.create", exc)
    return str(task.id)

def _poll_one(self, client, job_id):
    try:
        task = client.tasks.retrieve(job_id)
    except Exception as exc:  # noqa: BLE001
        self._raise_for_sdk_error("runway.tasks.retrieve", exc)
    return {
        "id": task.id,
        "status": task.status,
        "output": getattr(task, "output", None),
        "failure": getattr(task, "failure", None),
    }

def _is_done(self, status):
    return status.get("status") == "SUCCEEDED"

def _is_failed(self, status):
    if status.get("status") == "FAILED":
        return True, str(status.get("failure") or "runway task failed")
    return False, ""

def _extract_output_url(self, status):
    out = status.get("output") or []
    return str(out[0]) if out else ""

def _inject_assets(self, kw, job):
    if not job.segments:
        return
    for asset in job.segments[0].assets:
        if asset.role == "init_image":
            kw["prompt_image"] = asset.ref.uri
        elif asset.role == "start_image":
            kw["first_image"] = asset.ref.uri
        elif asset.role == "end_image":
            kw["last_image"] = asset.ref.uri

def _raise_for_sdk_error(self, op, exc):
    import runwayml  # lazy
    if isinstance(exc, runwayml.APIError):
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            raise AuthError(f"runway auth failed: {exc}") from exc
    raise RuntimeError(f"runway: {op} failed: {exc}") from exc
```

Engine class + `_default_factory` + `registry.register_engine("runway", _default_factory)` follow the Task 4 template verbatim; substitute env var `RUNWAYML_API_SECRET`.

- [ ] **Step 3-5:** Same as Task 4 — add to `_adapters.py`, run tests, pre-commit, commit.

```json:metadata
{"files": ["src/kinoforge/engines/runway/__init__.py", "src/kinoforge/_adapters.py", "tests/engines/test_runway.py"], "verifyCommand": "pixi run pytest tests/engines/test_runway.py -v", "acceptanceCriteria": ["RunwayBackend implements all 5 abstract hooks", "Mode dispatch t2v vs i2v/flf2v", "Uppercase status enum SUCCEEDED/FAILED", "Asset roles map to prompt_image/first_image/last_image", "401/403 mapped to AuthError", "Self-registers under 'runway'", "12 tests pass"]}
```

---

## Task 6: `LumaEngine` + `LumaBackend` + `FakeLumaClient`

**Goal:** Third per-provider subclass. Luma Bearer via `LUMAAI_API_KEY`. Note: Luma uses `state` (not `status`) and nests output under `assets.video`.

**Files:**
- Create: `src/kinoforge/engines/luma/__init__.py`
- Create: `tests/engines/test_luma.py`
- Modify: `src/kinoforge/_adapters.py`

**Acceptance Criteria:**
- [ ] Subclass implements all 5 abstract hooks per spec §4.3.
- [ ] Status field is `state`; done value is `completed`; failed value is `failed`; failure_reason field is `failure_reason`.
- [ ] Asset injection writes to `keyframes.frame0` (start) / `keyframes.frame1` (end) as `{"type": "image", "url": "..."}` records.
- [ ] `_extract_output_url` returns `assets.video` (string).
- [ ] `lumaai.APIError` with 401/403 → `AuthError("luma auth failed: ...")`.
- [ ] Self-registers under `"luma"`; added to `_adapters.py`.

**Verify:** `pixi run pytest tests/engines/test_luma.py -v` → 12 passed.

**Steps:**

Mirror Task 4 / 5 structure. Hook bodies per spec §4.4 (the Luma section in the spec). Env var: `LUMAAI_API_KEY`.

```python
def _submit(self, client, job):
    model = job.spec["model"]
    kw = {
        "prompt": resolve_prompt(job) or "",
        "model": model,
        **(job.spec.get("params") or {}),
    }
    self._inject_assets(kw, job)
    try:
        gen = client.generations.create(**kw)
    except Exception as exc:
        self._raise_for_sdk_error("luma.generations.create", exc)
    return str(gen.id)

def _poll_one(self, client, job_id):
    try:
        gen = client.generations.get(job_id)
    except Exception as exc:
        self._raise_for_sdk_error("luma.generations.get", exc)
    return {
        "id": gen.id,
        "state": gen.state,
        "assets": dict(gen.assets) if gen.assets else {},
        "failure_reason": getattr(gen, "failure_reason", None),
    }

def _is_done(self, status):
    return status.get("state") == "completed"

def _is_failed(self, status):
    if status.get("state") == "failed":
        return True, str(status.get("failure_reason") or "luma generation failed")
    return False, ""

def _extract_output_url(self, status):
    return str((status.get("assets") or {}).get("video", "") or "")

def _inject_assets(self, kw, job):
    if not job.segments:
        return
    keyframes: dict[str, Any] = {}
    for asset in job.segments[0].assets:
        if asset.role in ("init_image", "start_image"):
            keyframes["frame0"] = {"type": "image", "url": asset.ref.uri}
        elif asset.role == "end_image":
            keyframes["frame1"] = {"type": "image", "url": asset.ref.uri}
    if keyframes:
        kw["keyframes"] = keyframes
```

```json:metadata
{"files": ["src/kinoforge/engines/luma/__init__.py", "src/kinoforge/_adapters.py", "tests/engines/test_luma.py"], "verifyCommand": "pixi run pytest tests/engines/test_luma.py -v", "acceptanceCriteria": ["LumaBackend implements all 5 abstract hooks", "state (not status) field used", "Asset roles map to keyframes.frame0 / frame1", "Output extracted from assets.video", "401/403 mapped to AuthError", "Self-registers under 'luma'", "12 tests pass"]}
```

---

## Task 7: Fal retrofit onto `RemoteSubmitPollBackend`

**Goal:** Rewrite `FalEngine` / `FalBackend` as a `RemoteSubmitPollBackend` subclass. Existing YAML surface (`engine.fal.endpoint`, `queue_base`, `api_key_env`, `url_path`, `asset_paths`) preserved exactly — this is a code-only refactor. Self-registration key + YAML kind unchanged.

**Files:**
- Modify: `src/kinoforge/engines/fal/__init__.py` (rewrite)
- Modify: `tests/engines/test_fal.py` (rewrite against the new base while preserving the existing 24 acceptance criteria)

**Acceptance Criteria:**
- [ ] `examples/configs/fal.yaml` and `examples/configs/keyframe-fal-*.yaml` load unchanged.
- [ ] Every existing `tests/engines/test_fal.py` test case still passes after rewrite (24 cases). +4 new tests cover base-class hook integration.
- [ ] `FalEngine.validate_spec` behavior preserved (accepts non-empty prompt on `segments[0]` OR `job.spec`).
- [ ] `FalBackend._inject_assets` reads `engine.fal.asset_paths` from cfg (mirrored onto backend at construction) — preserves Layer F asset wiring.
- [ ] `FalBackend._extract_output_url` walks the configured `url_path` over the queue `result()` response.
- [ ] Net LOC reduction roughly ~120 → ~50 in `engines/fal/__init__.py`.

**Verify:** `pixi run pytest tests/engines/test_fal.py tests/test_examples.py -v` → all green (28 fal tests + example-load tests).

**Steps:**

- [ ] **Step 1: Read current `tests/engines/test_fal.py`** to inventory existing AC coverage. Keep every behavioural assertion; only construction sites change to wire the new base class.

- [ ] **Step 2: Rewrite `src/kinoforge/engines/fal/__init__.py`**

Structure:

```python
class FalBackend(RemoteSubmitPollBackend):
    def __init__(self, *, endpoint_default: str, url_path: str,
                 asset_paths: dict[str, str] | None = None, **kw) -> None:
        super().__init__(**kw)
        self._endpoint_default = endpoint_default
        self._url_path = url_path
        self._asset_paths = dict(asset_paths or {})

    def _submit(self, client, job):
        # Endpoint from cfg.engine.fal.endpoint (mirrored at construction).
        endpoint = self._endpoint_default
        input_dict = {
            "prompt": resolve_prompt(job) or "",
            **(job.spec.get("params") or {}),
        }
        self._inject_assets(input_dict, job)
        handler = client.submit(endpoint, arguments=input_dict)
        return str(handler.request_id)

    def _poll_one(self, client, job_id):
        endpoint = self._endpoint_default
        st = client.status(endpoint, job_id, with_logs=False)
        status_upper = str(getattr(st, "status", "") or "").upper()
        if status_upper == "COMPLETED":
            resp = client.result(endpoint, job_id)
            return {"status": "COMPLETED", "response": resp}
        return {"status": status_upper, "response": None}

    def _is_done(self, status):
        return status.get("status") == "COMPLETED"

    def _is_failed(self, status):
        if status.get("status") in ("FAILED", "CANCELLED"):
            return True, f"fal status={status['status']}"
        return False, ""

    def _extract_output_url(self, status):
        resp = status.get("response") or {}
        return _walk_dot_path(resp, self._url_path)

    def _inject_assets(self, input_dict, job):
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role in self._asset_paths:
                _set_by_dot_path(input_dict, self._asset_paths[asset.role], asset.ref.uri)


class FalEngine(RemoteSubmitPollEngine):
    name: str = "fal"

    def _build_client_factory(self, cfg, creds):
        kwargs = self._auth.client_kwargs()
        if not kwargs.get("api_key"):
            raise AuthError("fal: api_key empty")

        def _factory():
            import fal_client  # lazy
            return fal_client.SyncClient(key=kwargs["api_key"])

        return _factory

    def _build_backend(self, cfg, instance):
        del instance
        fal_cfg = cfg.get("engine", {}).get("fal", {})
        return FalBackend(
            client_factory=self._build_client_factory(cfg, None),
            endpoint_default=str(fal_cfg.get("endpoint", "")),
            url_path=str(fal_cfg.get("url_path", "video.url")),
            asset_paths={str(k): str(v) for k, v in (fal_cfg.get("asset_paths") or {}).items()},
            probe_profile=self._probe,
        )

    def validate_spec(self, job):
        # Preserve Phase 19 behavior: accept prompt on segments[0] or job.spec.
        prompt = resolve_prompt(job)
        if not prompt:
            from kinoforge.core.errors import ValidationError
            raise ValidationError("fal: no prompt in job.spec or segments[0]")
```

`registry.register_engine("fal", _default_factory)` at module bottom with env var `FAL_KEY`.

- [ ] **Step 3: Rewrite `tests/engines/test_fal.py`** — preserve every existing AC, swap construction sites to inject `FakeFalClient` per the new base-class shape. Add 4 new tests verifying base-class hooks (TimeoutError after max_poll, KinoforgeError on _is_failed, sleep is injected, capabilities returns probe).

- [ ] **Step 4: Run tests**

Run: `pixi run pytest tests/engines/test_fal.py tests/test_examples.py -v`
Expected: 28 fal tests pass + all example-load tests pass.

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/engines/fal/__init__.py tests/engines/test_fal.py
git add src/kinoforge/engines/fal/__init__.py tests/engines/test_fal.py
git commit -m "refactor(engines): retrofit FalEngine onto RemoteSubmitPollBackend

Existing YAML surface preserved exactly (engine.fal.endpoint /
queue_base / api_key_env / url_path / asset_paths).
Self-registration key + YAML kind unchanged.
~120 LOC -> ~50 LOC by deferring to base class for the poll
loop / error mapping / AuthStrategy wiring. Validates the
RemoteSubmitPollBackend abstraction against 4 wire shapes
(Replicate + Runway + Luma + fal) at landing."
```

```json:metadata
{"files": ["src/kinoforge/engines/fal/__init__.py", "tests/engines/test_fal.py"], "verifyCommand": "pixi run pytest tests/engines/test_fal.py tests/test_examples.py -v", "acceptanceCriteria": ["YAML surface preserved", "examples/configs/fal.yaml + keyframe-fal-*.yaml load unchanged", "All existing 24 test cases still pass after rewrite", "+4 base-class hook tests added", "Net LOC reduction ~120 -> ~50"]}
```

---

## Task 8: `ReplicateImageEngine` + `ReplicateImageBackend`

**Goal:** Image-engine sibling for Layer R `KeyframeStage`. Same `RemoteSubmitPollBackend` base — image generation is the same submit-poll-fetch dance with a single-URL output. Default model `black-forest-labs/flux-schnell` (~$0.003 per image).

**Files:**
- Create: `src/kinoforge/image_engines/replicate/__init__.py`
- Create: `tests/image_engines/__init__.py`
- Create: `tests/image_engines/test_replicate.py`
- Modify: `src/kinoforge/_adapters.py`

**Acceptance Criteria:**
- [ ] `ReplicateImageBackend(RemoteSubmitPollBackend)` overrides 5 hooks identically to `ReplicateBackend` except `_extract_output_url` unwraps a single image URL.
- [ ] `ReplicateImageEngine(ImageEngine)` from Layer R — wraps the submit-poll backend; produces a single image artifact.
- [ ] Self-registers via `image_engines.registry` (or whatever Layer R's image-engine registry is named) under `"replicate"`.
- [ ] 4 tests cover: submit shape, poll-once-done, output extraction, registry self-registration.

**Verify:** `pixi run pytest tests/image_engines/test_replicate.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Inspect Layer R `ImageEngine` ABC** — read `src/kinoforge/image_engines/__init__.py` (or wherever the Layer R image-engine substrate lives) to learn the exact ABC shape + registry helper.

- [ ] **Step 2: Write failing tests** mirroring Task 4 but with `client.predictions.create(version="black-forest-labs/flux-schnell", input={"prompt": ...})` and an `output: list[str]` shape (flux-schnell returns a list with 1 URL).

- [ ] **Step 3: Create `src/kinoforge/image_engines/replicate/__init__.py`**

Reuse `ReplicateBackend`'s pattern. The image-engine `ImageEngine` ABC defines its own `submit` / `result` shape returning `ImageArtifact` (or whatever Layer R named it); wire the `RemoteSubmitPollBackend`'s underlying machinery and adapt the return type at the engine layer.

- [ ] **Step 4: Add to `_adapters.py`**

```python
import kinoforge.image_engines.replicate  # noqa: F401
```

- [ ] **Step 5: Run + commit**

Run: `pixi run pytest tests/image_engines/test_replicate.py -v` → 4 passed.

```bash
pixi run pre-commit run --files src/kinoforge/image_engines/replicate/__init__.py src/kinoforge/_adapters.py tests/image_engines/test_replicate.py tests/image_engines/__init__.py
git add src/kinoforge/image_engines/replicate/__init__.py src/kinoforge/_adapters.py tests/image_engines/test_replicate.py tests/image_engines/__init__.py
git commit -m "feat(image_engines): ReplicateImageEngine for shared comparison keyframes

Layer R image-engine sibling for Replicate flux-schnell. Reuses
RemoteSubmitPollBackend — image generation is the same
submit-poll-fetch dance with a single-URL output. Used by the
comparison batch to generate ONE init image (i2v) + ONE bookend
pair (flf2v) shared across all four video providers."
```

```json:metadata
{"files": ["src/kinoforge/image_engines/replicate/__init__.py", "src/kinoforge/_adapters.py", "tests/image_engines/test_replicate.py", "tests/image_engines/__init__.py"], "verifyCommand": "pixi run pytest tests/image_engines/test_replicate.py -v", "acceptanceCriteria": ["ReplicateImageBackend reuses RemoteSubmitPollBackend base", "ReplicateImageEngine conforms to Layer R ImageEngine ABC", "Self-registers via image-engine registry under 'replicate'", "4 tests pass"]}
```

---

## Task 9: `GenerateClipStage` threads `provider` + `model` to `sink.publish`

**Goal:** Surgical change to `src/kinoforge/pipeline/generate_clip.py` — read `provider = cfg["engine"]["kind"]` and `model = cfg["spec"].get("model", "")` from cfg at stage construction; pass both to `sink.publish(...)` on each artifact.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py`
- Modify: `tests/pipeline/test_generate_clip.py`

**Acceptance Criteria:**
- [ ] `GenerateClipStage.__init__` accepts `provider: str | None = None`, `model: str | None = None` named-only params.
- [ ] When non-None, both are forwarded verbatim to every `sink.publish(...)` call.
- [ ] Stage construction sites in `Orchestrator.generate` and `batch_generate` read the values from `cfg` and pass them in.
- [ ] Existing single-config tests that don't pass provider/model continue to pass (filename gets `unknown_unknown` infix per Task 2).
- [ ] 3 new tests in `test_generate_clip.py`: provider/model forwarded; None defaults; provider but no model.

**Verify:** `pixi run pytest tests/pipeline/ -v` → green.

**Steps:**

- [ ] **Step 1: Find the construction sites** — `rg -n 'GenerateClipStage(' src/`

- [ ] **Step 2: Write failing tests** in `tests/pipeline/test_generate_clip.py`:

```python
def test_stage_forwards_provider_and_model_to_sink(tmp_path):
    from kinoforge.outputs.local import LocalOutputSink
    sink = LocalOutputSink(root=tmp_path)
    stage = GenerateClipStage(
        pool=_pool_with_done_backend(),
        store=_in_mem_store(),
        sink=sink,
        provider="replicate",
        model="wan-video/wan-t2v-1.3b",
    )
    result = stage.run(_request(), segments_override=None)
    pub_path = result.published_path
    assert "_replicate_" in pub_path
    assert "_wan-video-wan-t2v-1-3b_" in pub_path

def test_stage_defaults_to_none_uses_unknown(tmp_path):
    sink = LocalOutputSink(root=tmp_path)
    stage = GenerateClipStage(pool=..., store=..., sink=sink)
    result = stage.run(...)
    assert "_unknown_unknown_" in result.published_path
```

- [ ] **Step 3: Add params to `GenerateClipStage.__init__`** and forward to `sink.publish(...)`:

```python
def __init__(
    self,
    *,
    pool: BackendPool,
    store: ArtifactStore,
    sink: OutputSink | None = None,
    provider: str | None = None,
    model: str | None = None,
    # ... existing params ...
) -> None:
    self._provider = provider
    self._model = model
    # ... existing assignment ...

# Inside run(...), find each sink.publish(...) call and pass:
#   provider=self._provider, model=self._model
```

- [ ] **Step 4: Update construction sites in `Orchestrator.generate` + `batch_generate`**

```python
# Orchestrator.generate(...)
stage = GenerateClipStage(
    # ... existing args ...
    provider=str(cfg.get("engine", {}).get("kind", "")) or None,
    model=str((cfg.get("spec") or {}).get("model", "")) or None,
)

# batch_generate per-entry — same pattern, with each entry's cfg.
```

- [ ] **Step 5: Run tests**

Run: `pixi run pytest tests/pipeline/ tests/core/test_orchestrator.py tests/core/test_batch.py -v`
Expected: all green (existing tests still pass; new tests pass).

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py src/kinoforge/core/batch.py tests/pipeline/test_generate_clip.py
git add src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py src/kinoforge/core/batch.py tests/pipeline/test_generate_clip.py
git commit -m "feat(pipeline): GenerateClipStage threads provider+model to OutputSink

cfg.engine.kind -> provider; cfg.spec.model -> model. Both
forwarded verbatim to sink.publish() so LocalOutputSink can
embed them in the user-facing filename per Task 2's schema.
Construction sites in Orchestrator.generate and batch_generate
populate the new params from cfg."
```

```json:metadata
{"files": ["src/kinoforge/pipeline/generate_clip.py", "src/kinoforge/core/orchestrator.py", "src/kinoforge/core/batch.py", "tests/pipeline/test_generate_clip.py"], "verifyCommand": "pixi run pytest tests/pipeline/ tests/core/test_orchestrator.py tests/core/test_batch.py -v", "acceptanceCriteria": ["GenerateClipStage accepts provider/model named-only params", "Forwarded to every sink.publish(...) call", "Orchestrator + batch_generate construction sites updated", "Existing tests still pass; 3 new tests pass"]}
```

---

## Task 10: Comparison configs (12 video YAMLs + 2 keyframe YAMLs + manifest)

**Goal:** Land all 15 example YAMLs for the comparison batch. Each video YAML is small (~30 lines); manifest aggregates them.

**Files:**
- Create: `examples/configs/comparison/replicate-t2v.yaml`
- Create: `examples/configs/comparison/replicate-i2v.yaml`
- Create: `examples/configs/comparison/replicate-flf2v.yaml`
- Create: `examples/configs/comparison/runway-t2v.yaml`
- Create: `examples/configs/comparison/runway-i2v.yaml`
- Create: `examples/configs/comparison/runway-flf2v.yaml`
- Create: `examples/configs/comparison/luma-t2v.yaml`
- Create: `examples/configs/comparison/luma-i2v.yaml`
- Create: `examples/configs/comparison/luma-flf2v.yaml`
- Create: `examples/configs/comparison/fal-t2v.yaml`
- Create: `examples/configs/comparison/fal-i2v.yaml`
- Create: `examples/configs/comparison/fal-flf2v.yaml`
- Create: `examples/configs/comparison/keyframe-i2v.yaml`
- Create: `examples/configs/comparison/keyframe-flf2v.yaml`
- Create: `examples/configs/comparison/compare-all-providers.yaml`
- Modify: `tests/test_examples.py` (add load-tests for the new files)

**Acceptance Criteria:**
- [ ] All 15 YAMLs parse via `Config.model_validate(yaml.safe_load(path.read_text()))` without errors.
- [ ] Each video YAML names a valid budget-tier `spec.model` per the spec §6.1 table (candidate names; planner verified at execution time).
- [ ] i2v / flf2v YAMLs reference the shared keyframe outputs per the spec §6.1 mechanism CHOSEN AT THIS TASK from the spec §6.1 three-option deferral list. **Decision for this plan: Option 1 — `output.enabled: false` on keyframe YAMLs, downstream YAMLs reference ArtifactStore URIs under `<state-dir>/<run-id>/<artifact-filename>`.**
- [ ] Manifest entries listed in dependency order (2 keyframe pre-stages first, then t2v × 4, then i2v × 4, then flf2v × 4).
- [ ] `tests/test_examples.py` gains a parametrised test loading each of the 15 YAMLs; passes.

**Verify:** `pixi run pytest tests/test_examples.py -v` → all parametrised cases pass.

**Steps:**

- [ ] **Step 1: Write `examples/configs/comparison/replicate-t2v.yaml`**

```yaml
# kinoforge example: Replicate budget-tier t2v
#
# Sign up:  https://replicate.com/signin
# Get key:  https://replicate.com/account/api-tokens
# Set REPLICATE_API_TOKEN in your .env file.

engine:
  kind: replicate

spec:
  model: "wan-video/wan-2.1-t2v-1.3b"
  mode: t2v

params:
  num_frames: 81
  fps: 24
  aspect_ratio: "16:9"

lifecycle:
  budget: 1.50

output:
  kind: local
  dir: output/comparison
  enabled: true
```

- [ ] **Step 2: Write 8 more video YAMLs** following the same shape. Substitute `engine.kind` + `spec.model` per the spec §6.1 table. For i2v / flf2v variants, add a `segment_assets:` block referencing the ArtifactStore URI for the upstream keyframe entry:

```yaml
# Replicate i2v — references init frame produced by the keyframe-i2v pre-stage entry
segment_assets:
  - role: init_image
    ref:
      # Resolved at batch-run time: <state-dir>/keyframe-i2v/<artifact-filename>
      # See examples/configs/comparison/compare-all-providers.yaml for the
      # exact run_id used by the upstream entry.
      uri: "file://${KINOFORGE_STATE_DIR}/keyframe-i2v/<expected-filename>.png"
```

*(Note: the exact `<expected-filename>` is engine-derived. The plan executor MUST either (a) define a stable filename on the keyframe-i2v YAML via a CLI flag at the upstream entry, OR (b) use a glob/first-file lookup. Execution-time concern — flag in the implementer note inline.)*

- [ ] **Step 3: Write `examples/configs/comparison/keyframe-i2v.yaml`**

```yaml
# Shared init frame for all four i2v comparison entries.
# Generated ONCE per batch via Replicate flux-schnell; reused by
# every downstream i2v video config.

engine:
  kind: replicate-image      # ImageEngine registry key (Layer R)

spec:
  model: "black-forest-labs/flux-schnell"

params:
  num_outputs: 1
  aspect_ratio: "16:9"
  output_format: "png"

lifecycle:
  budget: 0.10

output:
  kind: local
  dir: output/comparison
  enabled: false              # internal artifact only; never user-published
```

- [ ] **Step 4: Write `examples/configs/comparison/keyframe-flf2v.yaml`**

Same shape; `params.num_outputs: 2` (produces frame0 + frame1).

- [ ] **Step 5: Write `examples/configs/comparison/compare-all-providers.yaml`**

```yaml
# Comparison batch — 12 video clips across 4 hosted providers,
# 3 modes (t2v / i2v / flf2v), all on the standard prompt.
#
# Total live spend: ~$2.32 (12 video × ~$0.04-0.70 each
# + 2 keyframe pre-stages via flux-schnell ~$0.036).
#
# Invocation:
#   KINOFORGE_LIVE_TESTS=1 \
#   pixi run -e live-hosted kinoforge batch \
#     examples/configs/comparison/compare-all-providers.yaml
#
# Outputs land at output/comparison/compare-all-providers/<filename>
# where filename = {ts}_{provider}_{model-slug}_{prompt-slug}.mp4

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

  # ---- i2v fan-out (references keyframe-i2v output) ----
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

  # ---- flf2v fan-out (references keyframe-flf2v frame0 + frame1) ----
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

- [ ] **Step 6: Extend `tests/test_examples.py`**

```python
@pytest.mark.parametrize("yaml_path", sorted(Path("examples/configs/comparison/").glob("*.yaml")))
def test_comparison_yaml_loads(yaml_path):
    raw = yaml.safe_load(yaml_path.read_text())
    # Manifest vs config dispatch — manifest has "entries", config doesn't.
    if "entries" in raw:
        from kinoforge.core.batch import load_manifest
        load_manifest(yaml_path)
    else:
        from kinoforge.core.config import Config
        Config.model_validate(raw)
```

- [ ] **Step 7: Run + commit**

```bash
pixi run pytest tests/test_examples.py -v
```

```bash
pixi run pre-commit run --files examples/configs/comparison/ tests/test_examples.py
git add examples/configs/comparison/ tests/test_examples.py
git commit -m "feat(examples): comparison batch configs (12 video + 2 keyframe + manifest)

Budget-tier video configs per provider/mode (candidate model
IDs — planner verifies at execution time). Two keyframe
pre-stage configs use ReplicateImageEngine flux-schnell;
output.enabled=false so they stay internal artifacts.
Manifest aggregates all 14 entries in dependency order
(keyframes first, then t2v fan-out, then i2v, then flf2v).
Per-yaml load tests added to tests/test_examples.py."
```

```json:metadata
{"files": ["examples/configs/comparison/", "tests/test_examples.py"], "verifyCommand": "pixi run pytest tests/test_examples.py -v", "acceptanceCriteria": ["15 YAMLs parse cleanly", "Keyframe YAMLs have output.enabled: false", "i2v/flf2v YAMLs reference upstream keyframe URI", "Manifest lists entries in dependency order", "Parametrised load test green"]}
```

---

## Task 11: Replicate live smoke (t2v + i2v + flf2v)

**Goal:** Real subprocess `kinoforge generate` invocations against Replicate for all three modes. Gated by `KINOFORGE_LIVE_TESTS=1` + `REPLICATE_API_TOKEN`. Per the `feedback_autonomous_no_gates` memory, this fires autonomously after `preflight --check-hosted` passes.

**Files:**
- Create: `tests/live/test_replicate_live.py`

**Acceptance Criteria:**
- [ ] Module-level skip on `KINOFORGE_LIVE_TESTS != "1"` or `REPLICATE_API_TOKEN` missing.
- [ ] 3 live tests: t2v, i2v, flf2v.
- [ ] Each test invokes `python -m kinoforge generate -c <yaml> --prompt-file prompt-field-realistic.txt --run-id live-replicate-<mode>`.
- [ ] Each asserts return-code 0, at least one `.mp4` produced under tmp_path, ISO-BMFF `ftyp` magic-byte check at offset 4.
- [ ] Standard prompt loaded verbatim from `/workspace/prompt-field-realistic.txt` (test reads file; subprocess receives `--prompt-file`).
- [ ] Total spend: ~$0.04 × 3 = ~$0.12.

**Verify (live):**

```bash
pixi run preflight --check-hosted && \
  KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_replicate_live.py -v -s
```
Expected: 3 passed, ~5-10 min wall clock, ~$0.12 spend.

**Steps:**

- [ ] **Step 1: Write `tests/live/test_replicate_live.py` mirroring `tests/live/test_fal_live.py`:**

```python
"""Opt-in live tests against the real Replicate predictions API.

Gated by:
- KINOFORGE_LIVE_TESTS=1
- REPLICATE_API_TOKEN=<real key>

Cost: ~$0.04 per clip on wan-video/wan-2.1-t2v-1.3b. ~$0.12 total
across t2v + i2v + flf2v. Skipped silently in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

if not (os.getenv("KINOFORGE_LIVE_TESTS") == "1" and os.getenv("REPLICATE_API_TOKEN")):
    pytest.skip(
        "live tests require KINOFORGE_LIVE_TESTS=1 + REPLICATE_API_TOKEN",
        allow_module_level=True,
    )


_PROMPT_FILE = "prompt-field-realistic.txt"


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "kinoforge", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )


def _assert_mp4(tmp_path: Path, result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"generate failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    candidates = list(tmp_path.rglob("*.mp4"))
    assert candidates, f"no .mp4 found under {tmp_path}; cli output:\n{result.stdout}"
    raw = candidates[0].read_bytes()
    assert raw[4:8] == b"ftyp", f"file {candidates[0]} is not an MP4"


def test_replicate_t2v_live(tmp_path: Path) -> None:
    result = _run_cli([
        "--state-dir", str(tmp_path),
        "generate",
        "--config", "examples/configs/comparison/replicate-t2v.yaml",
        "--prompt-file", _PROMPT_FILE,
        "--mode", "t2v",
        "--run-id", "live-replicate-t2v",
    ])
    _assert_mp4(tmp_path, result)


def test_replicate_i2v_live(tmp_path: Path) -> None:
    result = _run_cli([
        "--state-dir", str(tmp_path),
        "generate",
        "--config", "examples/configs/comparison/replicate-i2v.yaml",
        "--prompt-file", _PROMPT_FILE,
        "--mode", "i2v",
        "--run-id", "live-replicate-i2v",
    ])
    _assert_mp4(tmp_path, result)


def test_replicate_flf2v_live(tmp_path: Path) -> None:
    result = _run_cli([
        "--state-dir", str(tmp_path),
        "generate",
        "--config", "examples/configs/comparison/replicate-flf2v.yaml",
        "--prompt-file", _PROMPT_FILE,
        "--mode", "flf2v",
        "--run-id", "live-replicate-flf2v",
    ])
    _assert_mp4(tmp_path, result)
```

- [ ] **Step 2: Pre-flight + fire**

```bash
pixi run preflight --check-hosted
KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_replicate_live.py -v -s
```

- [ ] **Step 3: Commit the test file + a SHORT live-result note in the commit body**

```bash
git add tests/live/test_replicate_live.py
git commit -m "test(live): Replicate t2v + i2v + flf2v subprocess smokes

3 live tests gated by KINOFORGE_LIVE_TESTS=1 + REPLICATE_API_TOKEN.
Each invokes 'kinoforge generate' subprocess against the
corresponding comparison-batch YAML. Standard prompt loaded
verbatim from prompt-field-realistic.txt.

Live result: 3 passed, ~\$X.XX spend. First MP4 at <path>."
```

```json:metadata
{"files": ["tests/live/test_replicate_live.py"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest tests/live/test_replicate_live.py -v -s", "acceptanceCriteria": ["Module-level skip gate", "3 live tests (t2v/i2v/flf2v)", "Standard prompt verbatim from prompt-field-realistic.txt", "MP4 magic-bytes assertion", "Live spend ~$0.12"]}
```

---

## Task 12: Runway live smoke (t2v + i2v + flf2v)

**Goal:** Mirror Task 11 for Runway. Env var `RUNWAYML_API_SECRET`. Higher per-clip spend (~$0.25 × 3 = ~$0.75).

**Files:**
- Create: `tests/live/test_runway_live.py`

**Acceptance Criteria:**
- [ ] Same shape as Task 11 with env-var = `RUNWAYML_API_SECRET` and config paths under `examples/configs/comparison/runway-*.yaml`.
- [ ] 3 tests, MP4 magic-byte check, standard prompt verbatim.

**Verify (live):**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_runway_live.py -v -s
```
Expected: 3 passed, ~$0.75 spend.

**Steps:** Copy `test_replicate_live.py` verbatim → `test_runway_live.py`; swap `REPLICATE_API_TOKEN` → `RUNWAYML_API_SECRET` and `replicate-*.yaml` → `runway-*.yaml`. Fire pre-flight + run.

```json:metadata
{"files": ["tests/live/test_runway_live.py"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest tests/live/test_runway_live.py -v -s", "acceptanceCriteria": ["Module-level skip gate", "3 live tests (t2v/i2v/flf2v)", "MP4 magic-bytes assertion", "Live spend ~$0.75"]}
```

---

## Task 13: Luma live smoke (t2v + i2v + flf2v)

**Goal:** Mirror Task 11 for Luma. Env var `LUMAAI_API_KEY`. ~$0.35 × 3 = ~$1.05.

**Files:**
- Create: `tests/live/test_luma_live.py`

**Acceptance Criteria:**
- [ ] Same shape as Task 11, env-var = `LUMAAI_API_KEY`, configs under `examples/configs/comparison/luma-*.yaml`.

**Verify (live):**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_luma_live.py -v -s
```
Expected: 3 passed, ~$1.05 spend.

**Steps:** Copy `test_replicate_live.py` → `test_luma_live.py`; swap env var + config paths. Fire.

```json:metadata
{"files": ["tests/live/test_luma_live.py"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest tests/live/test_luma_live.py -v -s", "acceptanceCriteria": ["Module-level skip gate", "3 live tests (t2v/i2v/flf2v)", "MP4 magic-bytes assertion", "Live spend ~$1.05"]}
```

---

## Task 14: Fal live smoke extension (add i2v + flf2v)

**Goal:** Extend the existing `tests/live/test_fal_live.py` from t2v-only (Phase 19) to t2v + i2v + flf2v.

**Files:**
- Modify: `tests/live/test_fal_live.py`

**Acceptance Criteria:**
- [ ] Existing t2v test preserved (uses existing `examples/configs/fal.yaml` for backward compat OR switches to `examples/configs/comparison/fal-t2v.yaml` — pick the comparison config for symmetry with the other three providers).
- [ ] +2 tests for i2v + flf2v against `comparison/fal-i2v.yaml` + `comparison/fal-flf2v.yaml`.
- [ ] All three use the standard prompt verbatim.

**Verify (live):**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_fal_live.py -v -s
```
Expected: 3 passed, ~$0.30 spend.

**Steps:** Add two new test functions mirroring the t2v structure. Fire.

```json:metadata
{"files": ["tests/live/test_fal_live.py"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest tests/live/test_fal_live.py -v -s", "acceptanceCriteria": ["t2v test preserved", "+2 tests for i2v + flf2v", "Standard prompt verbatim across all three", "Live spend ~$0.30"]}
```

---

## Task 15: Comparison batch live smoke

**Goal:** End-to-end manifest run — single command, all 12 video clips + 2 keyframes, ~$2.32. The capstone live test.

**Files:**
- Create: `tests/live/test_comparison_batch_live.py`

**Acceptance Criteria:**
- [ ] Module-level skip on `KINOFORGE_LIVE_TESTS != "1"` OR any of the four env vars missing.
- [ ] Single test: subprocess invokes `python -m kinoforge batch examples/configs/comparison/compare-all-providers.yaml --state-dir tmp_path`.
- [ ] Asserts return-code 0, `_batch_summary.json` exists, batch_summary lists ≥12 video entries with `status: "ok"`, all 12 MP4s pass ISO-BMFF check.
- [ ] Total wall-clock ~10-15 min (sequential by Layer L default; ConcurrentPool with cap >1 could parallelise but spec doesn't require it).
- [ ] Total spend ~$2.32 — verified against `_batch_summary.json.total_spend` (if Layer L records this) OR inferred from per-entry counts.

**Verify (live):**

```bash
pixi run preflight --check-hosted && \
  KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_comparison_batch_live.py -v -s
```
Expected: 1 passed, ~$2.32 spend, 12 MP4s published.

**Steps:**

- [ ] **Step 1: Write `tests/live/test_comparison_batch_live.py`:**

```python
"""End-to-end live smoke for the cross-provider comparison batch.

Gated by KINOFORGE_LIVE_TESTS=1 + all 4 Bearer keys present.
~$2.32 spend, ~10-15 min wall clock. Skipped silently in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

_REQUIRED = ("REPLICATE_API_TOKEN", "RUNWAYML_API_SECRET",
             "LUMAAI_API_KEY", "FAL_KEY")

if not (os.getenv("KINOFORGE_LIVE_TESTS") == "1"
        and all(os.getenv(v) for v in _REQUIRED)):
    pytest.skip(
        f"live tests require KINOFORGE_LIVE_TESTS=1 + {', '.join(_REQUIRED)}",
        allow_module_level=True,
    )


def test_comparison_batch_live(tmp_path: Path) -> None:
    manifest = "examples/configs/comparison/compare-all-providers.yaml"
    result = subprocess.run(
        [
            sys.executable, "-m", "kinoforge",
            "--state-dir", str(tmp_path),
            "batch", manifest,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,  # 30 min ceiling
    )
    assert result.returncode == 0, (
        f"batch failed (exit {result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    summary_path = next(tmp_path.rglob("_batch_summary.json"))
    summary = json.loads(summary_path.read_text())
    video_entries = [e for e in summary["entries"]
                     if not e["run_id"].startswith("keyframe-")]
    assert len(video_entries) >= 12
    ok = [e for e in video_entries if e.get("status") == "ok"]
    assert len(ok) >= 12, f"some entries failed:\n{json.dumps(summary, indent=2)}"
    # ISO-BMFF magic on every published MP4.
    mp4s = sorted(tmp_path.rglob("*.mp4"))
    assert len(mp4s) >= 12
    for f in mp4s:
        raw = f.read_bytes()
        assert raw[4:8] == b"ftyp", f"{f} is not an MP4"
```

- [ ] **Step 2: Fire**

```bash
pixi run preflight --check-hosted
KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest \
  tests/live/test_comparison_batch_live.py -v -s
```

- [ ] **Step 3: Commit with live-result note**

```bash
git add tests/live/test_comparison_batch_live.py
git commit -m "test(live): end-to-end comparison batch smoke

Single capstone test for the comparison batch — 12 MP4s across
4 providers × 3 modes, ~\$2.32 spend, ~10-15 min wall clock.
Skipped silently in CI; runs autonomously when all 4 Bearer keys
+ KINOFORGE_LIVE_TESTS=1 set.

Live result: 12/12 ok, batch_summary at <path>, total \$X.XX."
```

```json:metadata
{"files": ["tests/live/test_comparison_batch_live.py"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run -e live-hosted pytest tests/live/test_comparison_batch_live.py -v -s", "acceptanceCriteria": ["Module-level skip gate (all 4 keys + KINOFORGE_LIVE_TESTS=1)", "1 test invoking kinoforge batch via subprocess", "_batch_summary.json verified", "12 MP4s ISO-BMFF magic checked", "Live spend ~$2.32"]}
```

---

## Task 16: README + PROGRESS + final invariant gate + `--no-ff` merge

**Goal:** Documentation + housekeeping wrap-up. README gains a "Comparison Smokes" section; PROGRESS.md gains a Phase 43 (Layer 4) entry; final invariant + full test gate; merge to main with `--no-ff` per project convention.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`
- (Final gate — no code changes)

**Acceptance Criteria:**
- [ ] README has a "Comparison Smokes" section with quickstart command, per-provider env-var list, total spend estimate, and link to `compare-all-providers.yaml`.
- [ ] PROGRESS.md Phase 43 (Layer 4) entry follows the established Phase-NN template: spec/plan paths, per-task SHAs, key design decisions, real-cloud verification note, test count delta, scope cuts.
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] `pixi run pytest -q` collected count matches projection (~1171); 0 failures.
- [ ] `pixi run pytest tests/test_core_invariant.py -v` all pass.
- [ ] Merge to `main` via `git merge --no-ff` with commit body referencing layer name + AC state + per-task SHAs + GH issue trailer (no GH issue here — instead reference the spec).

**Verify:** `pixi run pytest -q` → all green; `pixi run pre-commit run --all-files` → clean.

**Steps:**

- [ ] **Step 1: README — add section after the existing "Real providers — fal.ai" quickstart**

```markdown
## Comparison Smokes

Run the standard prompt through all four hosted providers
(Replicate / Runway / Luma / fal) across all three modes
(t2v / i2v / flf2v) in a single command. Outputs land at
`output/comparison/compare-all-providers/{ts}_{provider}_{model}_{slug}.mp4`
so attribution is obvious at `ls` time.

### Setup

Set the four Bearer keys in `.env`:

    REPLICATE_API_TOKEN=...
    RUNWAYML_API_SECRET=...
    LUMAAI_API_KEY=...
    FAL_KEY=...

### Quickstart

    pixi run preflight --check-hosted
    KINOFORGE_LIVE_TESTS=1 \
      pixi run -e live-hosted kinoforge batch \
      examples/configs/comparison/compare-all-providers.yaml

### Cost + wall clock

| Mode | Provider | Per-clip | Wall clock |
|---|---|---|---|
| t2v | Replicate | ~$0.04 | ~1-2 min |
| t2v | Runway    | ~$0.25 | ~2-3 min |
| t2v | Luma      | ~$0.35 | ~2-3 min |
| t2v | Fal       | ~$0.10 | ~1-2 min |
| i2v / flf2v | each | similar | similar |

Total batch spend: ~$2.32. Total wall clock: ~10-15 min sequential.
```

- [ ] **Step 2: PROGRESS.md — add Phase 43 (Layer 4) entry**

Insert after the most recent phase entry (currently Phase 42). Follow the template — spec path, plan path, per-task commit SHAs (filled at execution time), key design decisions, real-cloud verification note, test count delta, scope cuts.

- [ ] **Step 3: Full gate**

```bash
pixi run pre-commit run --all-files
pixi run pytest -q
pixi run pytest tests/test_core_invariant.py -v
```

All three must be green.

- [ ] **Step 4: Commit docs + final gate**

```bash
git add README.md PROGRESS.md
git commit -m "docs(layer-4): README Comparison Smokes section + PROGRESS Phase 43

Layer 4 wrap-up. README gains a Comparison Smokes quickstart;
PROGRESS adds Phase 43 entry with per-task SHAs, design
decisions, and the live-spend record. All invariant + full
suite green at this commit."
```

- [ ] **Step 5: `--no-ff` merge to main**

```bash
git checkout main
git merge --no-ff <layer-4-branch> -m "Merge Layer 4 — Bearer comparison smokes

Foundation: RemoteSubmitPollBackend ABC + per-provider subclasses
(Replicate / Runway / Luma; fal retrofit). Image-engine sibling
for shared keyframes. OutputSink filename schema extended with
provider+model. 12-video comparison batch via Layer L manifest.

Per-task SHAs: T0=…, T1=…, T2=…, T3=…, T4=…, T5=…, T6=…, T7=…,
              T8=…, T9=…, T10=…, T11=…, T12=…, T13=…, T14=…,
              T15=…, T16=…

Spec: docs/superpowers/specs/2026-06-07-bearer-comparison-smokes-design.md
Plan: docs/superpowers/plans/2026-06-07-bearer-comparison-smokes.md
Live result: 12/12 clips, ~\$2.32 spend, batch_summary archived."
```

```json:metadata
{"files": ["README.md", "PROGRESS.md"], "verifyCommand": "pixi run pytest -q && pixi run pre-commit run --all-files", "acceptanceCriteria": ["README has Comparison Smokes section", "PROGRESS Phase 43 entry added", "Pre-commit + full suite green", "Invariant tests green", "--no-ff merge with per-task SHA index"]}
```

---

## Self-Review

**1. Spec coverage** — each spec section maps to a task:

| Spec § | Coverage |
|---|---|
| §3.1 Foundation ABC | Task 0 |
| §3.2 Per-provider engines | Tasks 4, 5, 6, 7 |
| §3.3 ReplicateImageEngine | Task 8 |
| §4 Per-provider hooks | Tasks 4, 5, 6, 7 (hook bodies per spec) |
| §5 OutputSink + filename | Task 2 |
| §5.3 Stage wiring | Task 9 |
| §6 YAMLs + manifest | Task 10 |
| §7.1 Offline tests | Tasks 0, 4, 5, 6, 7, 8 (each carries its own) |
| §7.2 Live smokes | Tasks 11, 12, 13, 14, 15 |
| §7.4 Invariants | Task 1 |
| §8 Deps + preflight | Task 3 |
| §9 Error handling | Tasks 4, 5, 6, 7 (per-subclass `_raise_for_sdk_error`) |
| §10 Cost guards | Per-YAML `lifecycle.budget: 1.50` (Task 10) |
| §11 Scope cuts | Acknowledged in plan header; not new code |
| §12 Locked decisions | Each decision applied in the relevant task |

No gaps.

**2. Placeholder scan** — searched for "TBD", "TODO", "fill in", "implement later", "similar to Task N" — only intentional placeholders remain:
- `<expected-filename>` in Task 10 i2v/flf2v YAML snippets — intentional, deferred to execution-time per spec §6.1 deferral.
- `<state-dir>`/`<run-id>` in same — same.
- Per-task SHAs in Task 16 merge body — filled at execution.

**3. Type consistency** — method names across tasks:
- `_submit`, `_poll_one`, `_is_done`, `_is_failed`, `_extract_output_url`, `_extract_filename`, `_endpoints_map` — consistent across Tasks 0, 4, 5, 6, 7, 8.
- `_build_client_factory`, `_build_backend` — consistent across Tasks 0, 4, 5, 6, 7, 8.
- `provider`, `model` kwarg names — consistent across Tasks 2, 9.
- `Bearer`, `client_kwargs` — references Layer 1 substrate, signature already locked.

No inconsistencies found.

---
