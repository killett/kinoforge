"""Tests for DiffusersEngine + DiffusersBackend (Task 21a).

All I/O seams (subprocess, HTTP, sleep) are injected spies.
No real torch, network, or diffusers traffic occurs.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import FrameExtractionError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    GenerationJob,
    Instance,
    ModelProfile,
    Segment,
)
from kinoforge.engines.diffusers import DiffusersEngine

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_DEFAULT_PROBE = ModelProfile(
    name="diffusers-test",
    max_frames=24,
    fps=8,
    supported_modes={"t2v"},
    max_resolution=(1024, 576),
    supports_native_extension=False,
    supports_joint_audio=False,
)

_INSTANCE = Instance(
    id="inst-1",
    provider="local",
    status="ready",
    created_at=0.0,
    endpoints={"diffusers": "http://127.0.0.1:8000"},
)


def _make_cfg(
    *,
    pip: list[str] | None = None,
    server_cmd: list[str] | None = None,
    base_url: str = "http://127.0.0.1:8000",
) -> dict[str, Any]:
    """Build a minimal config dict for DiffusersEngine tests."""
    return {
        "engine": {
            "diffusers": {
                "pip": pip or [],
                "server_cmd": server_cmd or ["python", "-m", "diffusers_server"],
                "base_url": base_url,
            }
        },
    }


def _make_engine(**kwargs: Any) -> DiffusersEngine:
    """Return a DiffusersEngine with all I/O seams replaced by safe no-ops."""
    defaults: dict[str, Any] = {
        "run_cmd": lambda argv, cwd=None: None,
        "http_post": lambda url, body: {},
        "http_get": lambda url: {},
        "http_get_bytes": lambda url: b"",
        "ffmpeg_run": lambda argv, stdin: b"",
        "sleep": lambda s: None,
        "probe_profile": _DEFAULT_PROBE,
        "declared_flags_map": {},
    }
    defaults.update(kwargs)
    return DiffusersEngine(**defaults)


def _make_job(spec: dict[str, Any]) -> GenerationJob:
    """Return a minimal GenerationJob with the given spec."""
    return GenerationJob(spec=spec, segments=[Segment(prompt="test")])


# ---------------------------------------------------------------------------
# AC1: provision issues a pip install run_cmd call for the cfg-declared dep list
# ---------------------------------------------------------------------------


class TestProvisionPipInstall:
    """AC1 — pip install command issued for declared dep list."""

    def test_pip_install_called_with_declared_deps(self) -> None:
        """Assert run_cmd receives pip install with each declared dep."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        cfg = _make_cfg(pip=["diffusers", "transformers", "accelerate"])
        engine.provision(None, cfg)

        pip_calls = [c for c in calls if "pip" in c and "install" in c]
        assert len(pip_calls) >= 1, "No pip install call found"
        # All declared packages must appear in the pip install invocation
        install_call = pip_calls[0]
        assert "diffusers" in install_call
        assert "transformers" in install_call
        assert "accelerate" in install_call

    def test_pip_install_skipped_when_empty_deps(self) -> None:
        """Assert no pip install call when pip list is empty."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        cfg = _make_cfg(pip=[], server_cmd=["echo", "start"])
        engine.provision(None, cfg)

        pip_calls = [c for c in calls if "pip" in c and "install" in c]
        assert pip_calls == [], "pip install should not be called with empty dep list"


# ---------------------------------------------------------------------------
# AC2: provision launches the headless server via run_cmd
# ---------------------------------------------------------------------------


class TestProvisionServerLaunch:
    """AC2 — server launch command issued via run_cmd."""

    def test_server_launch_command_issued(self) -> None:
        """Assert run_cmd is called with the configured server_cmd."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        server_cmd = ["python", "-m", "diffusers_server", "--port", "8000"]
        cfg = _make_cfg(server_cmd=server_cmd)
        engine.provision(None, cfg)

        assert server_cmd in calls, f"Expected {server_cmd!r} in calls, got: {calls}"

    def test_server_launch_happens_after_pip_install(self) -> None:
        """Assert pip install precedes server launch in call order."""
        calls: list[list[str]] = []

        def spy(argv: list[str], cwd: str | None = None) -> None:
            calls.append(list(argv))

        engine = _make_engine(run_cmd=spy)
        server_cmd = ["python", "-m", "diffusers_server"]
        cfg = _make_cfg(pip=["diffusers"], server_cmd=server_cmd)
        engine.provision(None, cfg)

        pip_idx = next(
            (i for i, c in enumerate(calls) if "pip" in c and "install" in c), None
        )
        server_idx = next((i for i, c in enumerate(calls) if c == server_cmd), None)
        assert pip_idx is not None, "pip install not found"
        assert server_idx is not None, "server launch not found"
        assert pip_idx < server_idx, "pip install must come before server launch"


# ---------------------------------------------------------------------------
# AC3: validate_spec raises on missing pipeline or scheduler
# ---------------------------------------------------------------------------


class TestValidateSpec:
    """AC3 — validate_spec enforces required spec keys."""

    def test_missing_scheduler_raises(self) -> None:
        """validate_spec raises ValidationError when scheduler is absent."""
        engine = _make_engine()
        job = _make_job({"pipeline": "StableDiffusionPipeline"})
        with pytest.raises(ValidationError, match="scheduler"):
            engine.validate_spec(job)

    def test_missing_pipeline_raises(self) -> None:
        """validate_spec raises ValidationError when pipeline is absent."""
        engine = _make_engine()
        job = _make_job({"scheduler": "EulerDiscreteScheduler"})
        with pytest.raises(ValidationError, match="pipeline"):
            engine.validate_spec(job)

    def test_both_keys_present_passes(self) -> None:
        """validate_spec does not raise when both pipeline and scheduler are present."""
        engine = _make_engine()
        job = _make_job(
            {
                "pipeline": "StableDiffusionPipeline",
                "scheduler": "EulerDiscreteScheduler",
            }
        )
        engine.validate_spec(job)  # must not raise

    def test_empty_spec_raises(self) -> None:
        """validate_spec raises ValidationError when spec is empty."""
        engine = _make_engine()
        job = _make_job({})
        with pytest.raises(ValidationError):
            engine.validate_spec(job)


# ---------------------------------------------------------------------------
# AC4: backend.submit POSTs to /generate with spec body; returns job_id
# ---------------------------------------------------------------------------


class TestBackendSubmit:
    """AC4 — submit posts to /generate and returns the job_id."""

    def test_submit_posts_to_generate_endpoint(self) -> None:
        """Assert submit calls http_post on <base_url>/generate."""
        post_calls: list[tuple[str, dict[str, Any]]] = []

        def spy_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
            post_calls.append((url, body))
            return {"job_id": "abc123"}

        engine = _make_engine(http_post=spy_post)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        job = _make_job(
            {
                "pipeline": "StableDiffusionPipeline",
                "scheduler": "EulerDiscreteScheduler",
            }
        )
        backend.submit(job)

        assert len(post_calls) == 1
        url, body = post_calls[0]
        assert url == "http://127.0.0.1:8000/generate"
        assert "pipeline" in body or "spec" in body

    def test_submit_returns_job_id_from_response(self) -> None:
        """Assert submit returns the job_id string from the server response."""

        def spy_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
            return {"job_id": "job-xyz-999"}

        engine = _make_engine(http_post=spy_post)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        job = _make_job(
            {
                "pipeline": "StableDiffusionPipeline",
                "scheduler": "EulerDiscreteScheduler",
            }
        )
        job_id = backend.submit(job)
        assert job_id == "job-xyz-999"

    def test_submit_body_contains_spec(self) -> None:
        """Assert the POST body includes the job spec fields."""
        post_calls: list[tuple[str, dict[str, Any]]] = []

        def spy_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
            post_calls.append((url, body))
            return {"job_id": "id1"}

        engine = _make_engine(http_post=spy_post)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        spec = {
            "pipeline": "CogVideoXPipeline",
            "scheduler": "DDIMScheduler",
            "steps": 30,
        }
        job = _make_job(spec)
        backend.submit(job)

        _, body = post_calls[0]
        # The spec data must be present somewhere in the body
        body_str = str(body)
        assert "CogVideoXPipeline" in body_str
        assert "DDIMScheduler" in body_str


# ---------------------------------------------------------------------------
# AC5: backend.result polls /status/{job_id} until done; returns Artifact
# ---------------------------------------------------------------------------


class TestBackendResult:
    """AC5 — result polls status endpoint until done, returns Artifact."""

    def test_result_returns_artifact_on_done(self) -> None:
        """Assert result returns Artifact with correct filename and meta."""
        call_count = 0

        def spy_get(url: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return {"status": "pending"}
            return {"status": "done", "filename": "output.mp4"}

        engine = _make_engine(http_get=spy_get)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        artifact = backend.result("job-42")
        assert isinstance(artifact, Artifact)
        assert artifact.filename == "output.mp4"
        assert artifact.meta == {"job_id": "job-42"}

    def test_result_polls_correct_url(self) -> None:
        """Assert result calls http_get on /status/{job_id}."""
        get_calls: list[str] = []

        def spy_get(url: str) -> dict[str, Any]:
            get_calls.append(url)
            return {"status": "done", "filename": "out.mp4"}

        engine = _make_engine(http_get=spy_get)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        backend.result("my-job-id")

        assert any("status" in u and "my-job-id" in u for u in get_calls), (
            f"Expected /status/my-job-id in calls, got: {get_calls}"
        )

    def test_result_polls_until_done_on_second_call(self) -> None:
        """Assert polling continues until status == done (2nd call scenario)."""
        call_count = 0

        def spy_get(url: str) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return {"status": "running"}
            return {"status": "done", "filename": "clip.mp4"}

        engine = _make_engine(http_get=spy_get)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        artifact = backend.result("j1")
        assert call_count == 2
        assert artifact.filename == "clip.mp4"

    def test_result_raises_on_timeout(self) -> None:
        """Assert TimeoutError raised after poll cap exhausted."""

        def spy_get(url: str) -> dict[str, Any]:
            return {"status": "running"}

        engine = _make_engine(http_get=spy_get)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        with pytest.raises(TimeoutError):
            backend.result("stuck-job")

    def test_result_meta_contains_job_id(self) -> None:
        """Assert result Artifact meta always has job_id key."""

        def spy_get(url: str) -> dict[str, Any]:
            return {"status": "done", "filename": "result.mp4"}

        engine = _make_engine(http_get=spy_get)
        cfg = _make_cfg(base_url="http://127.0.0.1:8000")
        backend = engine.backend(None, cfg)

        artifact = backend.result("sentinel-id")
        assert artifact.meta.get("job_id") == "sentinel-id"


# ---------------------------------------------------------------------------
# AC6: declared_flags returns configured map for known key; {} for unknown
# ---------------------------------------------------------------------------


class TestDeclaredFlags:
    """AC6 — declared_flags returns configured flags or {} for unknown keys."""

    def test_known_key_returns_configured_flags(self) -> None:
        """Assert declared_flags returns the configured dict for a known key."""
        key = CapabilityKey(
            base_model="hf:org/model",
            engine="diffusers",
            precision="fp16",
        )
        flags = {"use_native_ext": True, "joint_audio": False}
        engine = _make_engine(declared_flags_map={key.derive(): flags})

        result = engine.declared_flags(key)
        assert result == flags

    def test_unknown_key_returns_empty_dict(self) -> None:
        """Assert declared_flags returns {} for an unrecognised key."""
        engine = _make_engine(declared_flags_map={})
        key = CapabilityKey(base_model="hf:unknown/model", engine="diffusers")
        assert engine.declared_flags(key) == {}

    def test_declared_flags_returns_copy(self) -> None:
        """Assert declared_flags returns a copy, not a mutable reference."""
        key = CapabilityKey(base_model="hf:org/m", engine="diffusers")
        flags = {"flag_a": True}
        engine = _make_engine(declared_flags_map={key.derive(): flags})

        result1 = engine.declared_flags(key)
        result1["injected"] = False
        result2 = engine.declared_flags(key)
        assert "injected" not in result2


# ---------------------------------------------------------------------------
# AC7: self-registers under "diffusers"; registry.get_engine("diffusers")()
# ---------------------------------------------------------------------------


class TestSelfRegistration:
    """AC7 — DiffusersEngine self-registers in the global registry."""

    def test_registry_contains_diffusers(self) -> None:
        """Assert registry.get_engine('diffusers') returns a factory."""
        factory = registry.get_engine("diffusers")
        assert factory is not None

    def test_registry_factory_produces_diffusers_engine(self) -> None:
        """Assert registry factory returns a DiffusersEngine instance."""
        factory = registry.get_engine("diffusers")
        assert factory is not None
        engine = factory()
        assert isinstance(engine, DiffusersEngine)


# ---------------------------------------------------------------------------
# AC8: requires_compute and requires_local_weights are True
# ---------------------------------------------------------------------------


class TestClassAttributes:
    """AC8 — class-level capability flags are set correctly."""

    def test_requires_compute_is_true(self) -> None:
        """Assert DiffusersEngine.requires_compute is True."""
        assert DiffusersEngine.requires_compute is True

    def test_requires_local_weights_is_true(self) -> None:
        """Assert DiffusersEngine.requires_local_weights is True."""
        assert DiffusersEngine.requires_local_weights is True

    def test_name_is_diffusers(self) -> None:
        """Assert DiffusersEngine.name is 'diffusers'."""
        assert DiffusersEngine.name == "diffusers"

    def test_instance_inherits_class_attrs(self) -> None:
        """Assert instance-level access also returns True for both flags."""
        engine = _make_engine()
        assert engine.requires_compute is True
        assert engine.requires_local_weights is True


# ---------------------------------------------------------------------------
# extract_last_frame + result() URL passthrough (Layer extract_last_frame)
# ---------------------------------------------------------------------------


def test_result_passes_url_from_server_response() -> None:
    """DiffusersBackend.result() reads 'url' from the polled response body.

    Bug this catches: backend ignores the new field, leaving extract_last_frame
    with nothing to fetch.
    """
    from kinoforge.engines.diffusers import DiffusersBackend

    payload = {
        "status": "done",
        "filename": "clip.mp4",
        "url": "http://127.0.0.1:8000/file/clip.mp4",
    }
    backend = DiffusersBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: payload,
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("JOB")

    assert artifact.filename == "clip.mp4"
    assert artifact.url == "http://127.0.0.1:8000/file/clip.mp4"


def test_result_defaults_url_to_empty_string_when_server_omits_field() -> None:
    """A server that doesn't return 'url' leaves Artifact.url == ''.

    extract_last_frame will then raise FrameExtractionError with a clear
    message — preferable to a corrupt download.

    Bug this catches: backend crashes with KeyError, leaking server-shape
    details into engine-layer code.
    """
    from kinoforge.engines.diffusers import DiffusersBackend

    backend = DiffusersBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: {"status": "done", "filename": "clip.mp4"},
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("JOB")

    assert artifact.url == ""


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """Same shape as ComfyUI extract test, with DiffusersEngine.

    Bug this catches: engine drops the fetched bytes or skips ffmpeg.
    """
    fetch_calls: list[str] = []
    ffmpeg_calls: list[tuple[list[str], bytes]] = []

    def fake_fetch(url: str) -> bytes:
        fetch_calls.append(url)
        return b"VIDEO"

    def fake_ffmpeg(argv: list[str], stdin: bytes) -> bytes:
        ffmpeg_calls.append((argv, stdin))
        return b"PNG"

    engine = _make_engine(http_get_bytes=fake_fetch, ffmpeg_run=fake_ffmpeg)

    artifact = Artifact(
        filename="clip.mp4",
        url="http://127.0.0.1:8000/file/clip.mp4",
        meta={"job_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG"
    assert fetch_calls == ["http://127.0.0.1:8000/file/clip.mp4"]
    assert ffmpeg_calls[0][1] == b"VIDEO"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """artifact.url == '' raises FrameExtractionError mentioning DiffusersEngine.

    Bug this catches: shared body copy-paste leaves the wrong class name.
    """
    engine = _make_engine()
    artifact = Artifact(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="DiffusersEngine"):
        engine.extract_last_frame(artifact)
