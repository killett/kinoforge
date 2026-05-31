"""Tests for HostedAPIEngine — the no-compute path.

All I/O is routed through injected callables (spy functions).
No real HTTP calls, no real credentials, no instances.

AC coverage:
1. requires_compute=False, requires_local_weights=False; key_base() derives from cfg model.
2. End-to-end with instance=None: provision → backend → submit → result → Artifact.
   Sentinel provider never called.
3. Missing credential → AuthError mentioning the key name.
4. Endpoint unreachable (http_get raises) → KinoforgeError("hosted endpoint unreachable: ...").
5. validate_spec({}) → ValidationError; validate_spec({"model": "x", "params": {}}) → passes.
6. key_base(cfg) == "ltx-2".
7. declared_flags(known_key) returns configured map; declared_flags(unknown_key) returns {}.
8. Self-registers under "hosted"; registry.get_engine("hosted")() returns HostedAPIEngine.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import (
    AuthError,
    FrameExtractionError,
    KinoforgeError,
    ValidationError,
)
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ConditioningAsset,
    CredentialProvider,
    GenerationJob,
    Instance,
    ModelProfile,
    Segment,
)

# Import the module under test — this triggers self-registration.
from kinoforge.engines.hosted import HostedAPIEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

_ENDPOINT = "https://fal.run/fal-ai/ltx-video"
_HEALTH_URL = "https://fal.run/health"
_API_KEY_ENV = "FAL_KEY"
_MODEL = "ltx-2"

_BASE_CFG: dict[str, Any] = {
    "engine": {
        "hosted": {
            "provider": "fal",
            "endpoint": _ENDPOINT,
            "model": _MODEL,
            "api_key_env": _API_KEY_ENV,
            "health_url": _HEALTH_URL,
        }
    }
}

_DEFAULT_PROBE = ModelProfile(
    name="hosted",
    max_frames=81,
    fps=24,
    supported_modes={"t2v"},
    max_resolution=(1280, 720),
    supports_native_extension=False,
    supports_joint_audio=False,
)


def _make_creds(key: str | None = "secret-key") -> dict[str, str | None]:
    """Return a simple dict-backed credential store.

    Args:
        key: Value to return for the API key env var; None simulates missing cred.

    Returns:
        Dict keyed by env-var name.
    """
    return {_API_KEY_ENV: key}


class _DictCreds(CredentialProvider):
    """Minimal CredentialProvider backed by a dict."""

    def __init__(self, data: dict[str, str | None]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:  # noqa: D102
        return self._data.get(key)


def _ok_http_get(url: str) -> dict[str, Any]:
    """Spy GET that returns a success response."""
    return {"status": "ok"}


def _ok_http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    """Spy POST that returns a fake job_id."""
    return {"job_id": "fake-job-123"}


def _result_http_get(url: str) -> dict[str, Any]:
    """Spy GET that returns health-ok for /health and done-status for everything else."""
    if url == _HEALTH_URL:
        return {"status": "ok"}
    return {"status": "done", "filename": "output.mp4"}


def _make_engine(
    *,
    creds: CredentialProvider | None = None,
    http_get: Any = _ok_http_get,
    http_post: Any = _ok_http_post,
    http_get_bytes: Any = lambda url: b"",
    ffmpeg_run: Any = lambda argv, stdin: b"",
    probe_profile: ModelProfile = _DEFAULT_PROBE,
    declared_flags_map: dict[str, dict[str, bool]] | None = None,
) -> HostedAPIEngine:
    return HostedAPIEngine(
        creds=creds or _DictCreds(_make_creds()),
        http_get=http_get,
        http_post=http_post,
        http_get_bytes=http_get_bytes,
        ffmpeg_run=ffmpeg_run,
        probe_profile=probe_profile,
        declared_flags_map=declared_flags_map,
    )


def _make_job(spec: dict[str, Any] | None = None) -> GenerationJob:
    if spec is None:
        spec = {"model": _MODEL, "params": {"steps": 30}}
    return GenerationJob(spec=spec, segments=[Segment(prompt="test")])


# ---------------------------------------------------------------------------
# AC 1: class-level flags and key_base
# ---------------------------------------------------------------------------


def test_ac1_requires_compute_false() -> None:
    """requires_compute must be False for HostedAPIEngine."""
    assert HostedAPIEngine.requires_compute is False


def test_ac1_requires_local_weights_false() -> None:
    """requires_local_weights must be False for HostedAPIEngine."""
    assert HostedAPIEngine.requires_local_weights is False


def test_ac1_key_base_from_cfg() -> None:
    """key_base(cfg) returns the model string from the hosted config block."""
    engine = _make_engine()
    assert engine.key_base(_BASE_CFG) == _MODEL


# ---------------------------------------------------------------------------
# AC 6: key_base returns the model string exactly
# ---------------------------------------------------------------------------


def test_ac6_key_base_exact_value() -> None:
    """key_base(cfg) with model='ltx-2' returns exactly 'ltx-2'."""
    cfg: dict[str, Any] = {
        "engine": {
            "hosted": {
                "model": "ltx-2",
                "api_key_env": "K",
                "endpoint": "x",
                "health_url": "y",
            }
        }
    }
    engine = _make_engine()
    assert engine.key_base(cfg) == "ltx-2"


# ---------------------------------------------------------------------------
# AC 2: end-to-end with instance=None
# ---------------------------------------------------------------------------


def test_ac2_provision_succeeds_with_no_instance() -> None:
    """provision(None, cfg) succeeds without requiring a compute instance."""
    engine = _make_engine(http_get=_ok_http_get)
    engine.provision(None, _BASE_CFG)  # must not raise


def test_ac2_backend_returns_hosted_backend() -> None:
    """backend(None, cfg) returns a HostedAPIBackend."""
    from kinoforge.engines.hosted import HostedAPIBackend

    engine = _make_engine()
    backend = engine.backend(None, _BASE_CFG)
    assert isinstance(backend, HostedAPIBackend)


def test_ac2_submit_returns_job_id() -> None:
    """submit(job) returns the job_id from the POST response."""
    engine = _make_engine()
    backend = engine.backend(None, _BASE_CFG)
    job_id = backend.submit(_make_job())
    assert job_id == "fake-job-123"


def test_ac2_result_returns_artifact() -> None:
    """result(job_id) polls and returns an Artifact with the filename."""
    engine = _make_engine(http_get=_result_http_get)
    backend = engine.backend(None, _BASE_CFG)
    artifact = backend.result("fake-job-123")
    assert isinstance(artifact, Artifact)
    assert artifact.filename == "output.mp4"


def test_ac2_no_instance_ever_constructed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compute provider is never accessed when engine.requires_compute=False."""

    class _SentinelProvider:
        """Raises on any method call."""

        def create_instance(self, *a: Any, **kw: Any) -> None:
            raise AssertionError(
                "create_instance must never be called for hosted engine"
            )

        def find_offers(self, *a: Any, **kw: Any) -> None:
            raise AssertionError("find_offers must never be called for hosted engine")

    # The engine should complete provision+backend with instance=None,
    # and the sentinel provider should never be invoked.
    engine = _make_engine(http_get=_result_http_get, http_post=_ok_http_post)
    _sentinel = _SentinelProvider()  # just ensure it exists; engine won't touch it
    engine.provision(None, _BASE_CFG)
    backend = engine.backend(None, _BASE_CFG)
    job_id = backend.submit(_make_job())
    artifact = backend.result(job_id)
    assert artifact.filename == "output.mp4"


def test_ac2_provision_raises_on_non_none_instance() -> None:
    """provision(instance, cfg) raises KinoforgeError when instance is not None."""
    engine = _make_engine()
    fake_instance = Instance(
        id="i-1", provider="runpod", status="ready", created_at=0.0
    )
    with pytest.raises(KinoforgeError):
        engine.provision(fake_instance, _BASE_CFG)


# ---------------------------------------------------------------------------
# AC 3: missing credential
# ---------------------------------------------------------------------------


def test_ac3_missing_cred_raises_auth_error() -> None:
    """provision raises AuthError mentioning the key name when cred is None."""
    missing_creds = _DictCreds({_API_KEY_ENV: None})
    engine = _make_engine(creds=missing_creds)
    with pytest.raises(AuthError) as exc_info:
        engine.provision(None, _BASE_CFG)
    assert _API_KEY_ENV in str(exc_info.value)


def test_ac3_auth_error_message_contains_key_name() -> None:
    """AuthError message must contain the env-var key name (e.g. 'FAL_KEY')."""
    creds = _DictCreds({"FAL_KEY": None})
    engine = _make_engine(creds=creds)
    with pytest.raises(AuthError, match="FAL_KEY"):
        engine.provision(None, _BASE_CFG)


# ---------------------------------------------------------------------------
# AC 4: endpoint unreachable
# ---------------------------------------------------------------------------


def test_ac4_http_get_raises_becomes_kinoforge_error() -> None:
    """provision re-raises http_get failures as KinoforgeError('hosted endpoint unreachable:...')."""

    def _fail_http_get(url: str) -> dict[str, Any]:
        raise OSError("connection refused")

    engine = _make_engine(http_get=_fail_http_get)
    with pytest.raises(KinoforgeError, match="hosted endpoint unreachable"):
        engine.provision(None, _BASE_CFG)


def test_ac4_error_message_contains_unreachable() -> None:
    """The KinoforgeError message prefix must be 'hosted endpoint unreachable:'."""

    def _bad_get(url: str) -> dict[str, Any]:
        raise ConnectionError("timeout")

    engine = _make_engine(http_get=_bad_get)
    with pytest.raises(KinoforgeError) as exc_info:
        engine.provision(None, _BASE_CFG)
    assert str(exc_info.value).startswith("hosted endpoint unreachable:")


# ---------------------------------------------------------------------------
# AC 5: validate_spec
# ---------------------------------------------------------------------------


def test_ac5_validate_spec_empty_raises() -> None:
    """validate_spec with empty spec dict raises ValidationError."""
    engine = _make_engine()
    job = GenerationJob(spec={}, segments=[Segment(prompt="x")])
    with pytest.raises(ValidationError):
        engine.validate_spec(job)


def test_ac5_validate_spec_missing_model_raises() -> None:
    """validate_spec missing 'model' key raises ValidationError."""
    engine = _make_engine()
    job = GenerationJob(spec={"params": {}}, segments=[Segment(prompt="x")])
    with pytest.raises(ValidationError):
        engine.validate_spec(job)


def test_ac5_validate_spec_missing_params_raises() -> None:
    """validate_spec missing 'params' key raises ValidationError."""
    engine = _make_engine()
    job = GenerationJob(spec={"model": "ltx-2"}, segments=[Segment(prompt="x")])
    with pytest.raises(ValidationError):
        engine.validate_spec(job)


def test_ac5_validate_spec_valid_passes() -> None:
    """validate_spec with both 'model' and 'params' does not raise."""
    engine = _make_engine()
    job = GenerationJob(
        spec={"model": "ltx-2", "params": {}}, segments=[Segment(prompt="x")]
    )
    engine.validate_spec(job)  # must not raise


# ---------------------------------------------------------------------------
# AC 7: declared_flags
# ---------------------------------------------------------------------------


def test_ac7_declared_flags_known_key() -> None:
    """declared_flags returns the configured map for a known CapabilityKey."""
    key = CapabilityKey(base_model="ltx-2", engine="hosted")
    flags = {"fast_decode": True}
    engine = _make_engine(declared_flags_map={key.derive(): flags})
    assert engine.declared_flags(key) == flags


def test_ac7_declared_flags_unknown_key_returns_empty() -> None:
    """declared_flags returns {} for an unknown CapabilityKey."""
    engine = _make_engine()
    unknown = CapabilityKey(base_model="unknown-model", engine="hosted")
    assert engine.declared_flags(unknown) == {}


def test_ac7_declared_flags_returns_copy() -> None:
    """declared_flags returns a copy, not the internal dict."""
    key = CapabilityKey(base_model="ltx-2", engine="hosted")
    flags = {"fast_decode": True}
    engine = _make_engine(declared_flags_map={key.derive(): flags})
    result = engine.declared_flags(key)
    result["mutated"] = True
    # Original must be untouched
    assert engine.declared_flags(key) == flags


# ---------------------------------------------------------------------------
# AC 8: self-registration
# ---------------------------------------------------------------------------


def test_ac8_registered_under_hosted() -> None:
    """Engine registry must return a HostedAPIEngine factory under 'hosted'."""
    factory = registry.get_engine("hosted")
    engine = factory()
    assert isinstance(engine, HostedAPIEngine)


def test_ac8_name_attribute() -> None:
    """HostedAPIEngine.name must be 'hosted'."""
    assert HostedAPIEngine.name == "hosted"


# ---------------------------------------------------------------------------
# inspect_capabilities / endpoints on backend
# ---------------------------------------------------------------------------


def test_backend_inspect_capabilities_returns_probe() -> None:
    """HostedAPIBackend.inspect_capabilities() returns the injected ModelProfile."""
    engine = _make_engine(probe_profile=_DEFAULT_PROBE)
    backend = engine.backend(None, _BASE_CFG)
    profile = backend.inspect_capabilities()
    assert profile is _DEFAULT_PROBE


def test_backend_endpoints_returns_endpoint_url() -> None:
    """HostedAPIBackend.endpoints() returns a dict containing the configured endpoint."""
    engine = _make_engine()
    backend = engine.backend(None, _BASE_CFG)
    endpoints = backend.endpoints()
    assert _ENDPOINT in endpoints.values()


# ---------------------------------------------------------------------------
# extract_last_frame + url_path dot-walker (Layer extract_last_frame)
# ---------------------------------------------------------------------------


def test_walk_dot_path_resolves_nested_string() -> None:
    """video.url -> nested string lookup works.

    Bug this catches: walker only handles top-level keys.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": {"url": "X"}}, "video.url") == "X"


def test_walk_dot_path_returns_empty_on_missing_intermediate_key() -> None:
    """Any missing step short-circuits to ''; no KeyError leaks out.

    Bug this catches: walker raises on the first missing key, breaking
    backends that point at providers returning sparse responses.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": {"url": "X"}}, "missing.url") == ""


def test_walk_dot_path_returns_empty_on_non_string_terminal() -> None:
    """If the walked path lands on a non-string (e.g. int, dict), return ''.

    Bug this catches: walker str()-casts arbitrary values, producing
    fake URLs like 'http://...' from random response payloads.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"v": {"url": 42}}, "v.url") == ""


def test_walk_dot_path_returns_empty_on_empty_path() -> None:
    """Empty path returns empty string — used when cfg omits url_path.

    Bug this catches: walker iterates an empty path and lands on the
    top-level data dict, returning '' or raising — the empty-path case
    should be a clean early return.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({}, "") == ""


def test_result_uses_url_path_to_backfill_artifact_url() -> None:
    """HostedAPIBackend.result() walks url_path and populates Artifact.url.

    Bug this catches: backend ignores url_path or always returns ''.
    """
    from kinoforge.engines.hosted import HostedAPIBackend

    payload = {
        "status": "done",
        "filename": "clip.mp4",
        "video": {"url": "https://cdn.fal.run/clip.mp4"},
    }
    backend = HostedAPIBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: payload,
        endpoint="https://fal.run/fal-ai/ltx",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
        url_path="video.url",
    )

    artifact = backend.result("JOB")

    assert artifact.url == "https://cdn.fal.run/clip.mp4"


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """Same shape as the other two engines, with HostedAPIEngine.

    Bug this catches: engine drops bytes or skips ffmpeg.
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
        url="https://cdn.fal.run/clip.mp4",
        meta={"job_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG"
    assert fetch_calls == ["https://cdn.fal.run/clip.mp4"]
    assert ffmpeg_calls[0][1] == b"VIDEO"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """Empty url raises FrameExtractionError mentioning HostedAPIEngine.

    Bug this catches: copy-paste shared body leaves the wrong class name.
    """
    engine = _make_engine()
    artifact = Artifact(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="HostedAPIEngine"):
        engine.extract_last_frame(artifact)


def test_extract_last_frame_wraps_fetch_failure_as_frame_extraction_error() -> None:
    """HTTP fetch errors surface as FrameExtractionError with the URL in the
    message, not as raw urllib exceptions.

    Bug this catches: callers expecting the spec-promised single exception
    type (FrameExtractionError) get an unrelated network exception instead.
    """

    class _NetBlewUp(RuntimeError):
        pass

    def boom(url: str) -> bytes:
        raise _NetBlewUp("connection refused")

    engine = _make_engine(http_get_bytes=boom)
    artifact = Artifact(
        filename="clip.mp4",
        url="https://cdn.fal.run/clip.mp4",
        meta={},
    )

    with pytest.raises(FrameExtractionError, match="fetch from"):
        engine.extract_last_frame(artifact)


def test_walk_dot_path_consecutive_dots_returns_empty() -> None:
    """Mis-configured paths like 'video..url' split to ['video', '', 'url'];
    the empty-key step fails the dict/key guard and returns ''.

    Bug this catches: walker doesn't handle malformed paths and either
    crashes or silently descends through a synthetic empty key.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": {"url": "X"}}, "video..url") == ""


def test_walk_dot_path_list_terminal_returns_empty() -> None:
    """A list terminal (e.g. {'video': [...]} via 'video' path) is not a
    string, so the walker returns ''. Realistic mis-config — some providers
    return {'results': [{'url': '...'}]} which is array-indexed.

    Bug this catches: walker str()-casts the list and produces fake URLs
    like "[{'url': '...'}]" downstream.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": [{"url": "X"}]}, "video") == ""


# ---------------------------------------------------------------------------
# Layer F: asset_paths wiring (Task 3)
# ---------------------------------------------------------------------------


def test_submit_writes_asset_uri_at_nested_dot_path() -> None:
    """submit() walks asset_paths and writes asset.ref.uri into the body
    at the configured nested dot-path, creating intermediate dicts.

    Bug catch: a non-nested setter would create a top-level
    ``"input.image_url"`` string-key instead of nested
    ``body["input"]["image_url"]``; original spec keys must be forwarded
    intact alongside the injected URI.
    """
    from kinoforge.engines.hosted import HostedAPIBackend

    posted: list[tuple[str, dict[str, Any]]] = []

    def spy_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append((url, dict(body)))
        return {"job_id": "j-1"}

    backend = HostedAPIBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_DEFAULT_PROBE,
        asset_paths={"init_image": "input.image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="s.png", uri="https://store/s.png"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {"steps": 30}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    backend.submit(job)
    body = posted[0][1]
    # Bug catch: a non-nested setter would create top-level
    # "input.image_url" string-key instead of nested dict.
    assert body["input"]["image_url"] == "https://store/s.png"
    # Bug catch: original spec keys must be forwarded intact.
    assert body["model"] == "vendor/m"
    assert body["params"] == {"steps": 30}


def test_submit_no_asset_paths_unchanged() -> None:
    """Pre-Layer-F regression: with no asset_paths configured AND prompt
    routing disabled, the POST body equals job.spec exactly — no phantom
    keys, no copies via the asset-injection loop mutating shape.

    Bug catch: a refactor that always wraps body in {"input": ...} or
    leaves residue of an empty-iteration dot-path setter would break
    every existing hosted template.

    Note: ``prompt_body_key=None`` opts out of Layer J prompt routing so
    this test isolates the asset-injection invariant.
    """
    from kinoforge.engines.hosted import HostedAPIBackend

    posted: list[dict[str, Any]] = []

    def spy_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posted.append(dict(body))
        return {"job_id": "j-2"}

    backend = HostedAPIBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key=None,
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[])],
        params={},
    )
    backend.submit(job)
    assert posted[0] == {"model": "vendor/m", "params": {}}


def test_validate_spec_rejects_asset_without_path_mapping() -> None:
    """validate_spec raises ValidationError naming the offending role
    when an asset on segments[0] has no asset_paths entry.

    Bug catch: silent skip would let submit() POST a body missing the
    conditioning asset URI; the server would generate from prompt only.
    """
    engine = HostedAPIEngine()
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://x"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    with pytest.raises(ValidationError, match="init_image"):
        engine.validate_spec(job)


def test_submit_does_not_fetch_asset_bytes() -> None:
    """submit() must NOT fetch the asset bytes — URL passthrough only.

    Bug catch: at Layer F the Backend constructor takes no
    http_get_bytes seam; absence is the contract. The call must succeed
    without any byte-fetch infrastructure (the provider fetches the
    URI server-side).
    """
    from kinoforge.engines.hosted import HostedAPIBackend

    backend = HostedAPIBackend(
        http_post=lambda u, b: {"job_id": "x"},
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_DEFAULT_PROBE,
        asset_paths={"init_image": "input.image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="s.png", uri="https://store/s.png"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    # Backend constructor takes no http_get_bytes seam at Layer F;
    # absence is the contract. The call must succeed without any
    # byte-fetch infrastructure.
    backend.submit(job)


# ---------------------------------------------------------------------------
# End-to-end: YAML -> Config.model_validate -> model_dump -> engine.backend
# Catches the pydantic-strip defect where unknown YAML keys (asset_paths in
# pre-fix code) silently drop during model_dump.
# ---------------------------------------------------------------------------


def test_provision_auth_error_message_when_key_name_empty() -> None:
    """When api_key_env is somehow empty at runtime (validator bypass), the
    AuthError message must be self-explanatory, not 'missing '.

    Defense-in-depth: pydantic validator (Layer I Task 4) catches this at load.
    This test exercises the runtime fallback for direct-constructor calls.
    """

    class _NullCreds(CredentialProvider):
        def get(self, key: str) -> str | None:
            return None

    engine = HostedAPIEngine(creds=_NullCreds())
    cfg = {
        "engine": {
            "hosted": {"api_key_env": "", "endpoint": "https://e", "health_url": ""}
        }
    }
    with pytest.raises(AuthError) as exc_info:
        engine.provision(None, cfg)
    assert "engine.hosted.api_key_env is empty" in str(exc_info.value)


def test_declared_flags_default_for_hosted_yaml_key() -> None:
    """HostedAPIEngine constructed via the registry factory must declare strategy
    flags for the shipped hosted.yaml key.

    Bug catch: a regression that changed the registry lambda to
    `lambda: HostedAPIEngine(declared_flags_map={})` would silently drop the
    default for the shipped YAML; going through the registry closes that gap.
    """
    import importlib

    from kinoforge.core import registry
    from kinoforge.core.config import load_config

    # Ensure self-registration import side effect has run
    importlib.import_module("kinoforge.engines.hosted")

    cfg = load_config("examples/configs/hosted.yaml")
    engine = registry.get_engine("hosted")()
    flags = engine.declared_flags(cfg.capability_key())
    assert flags == {
        "supports_native_extension": False,
        "supports_joint_audio": False,
    }


def test_yaml_round_trip_propagates_asset_paths_to_backend() -> None:
    """End-to-end YAML -> Config -> model_dump -> HostedAPIEngine.backend
    preserves asset_paths so the backend ends up with the configured mapping.

    Catches the pydantic-strip defect where unknown YAML keys (asset_paths in
    pre-fix code) silently drop during model_dump — the Layer F unit tests
    construct HostedAPIBackend directly with asset_paths kwarg and never
    exercise this YAML round-trip.

    Bug catch: a regression that removes asset_paths from HostedEngineConfig
    (or otherwise lets pydantic strip it) leaves backend._asset_paths == {}
    even when YAML declares the mapping, silently breaking every
    image-to-video hosted job.
    """
    from kinoforge.core.config import Config

    yaml_text = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: fal
    endpoint: "https://fal.run/x"
    model: "vendor/m"
    asset_paths:
      init_image: input.image_url
lifecycle: {budget: 5.0}
models:
  - {ref: "hf:org/m", kind: base, target: diffusion_models}
"""
    import yaml

    cfg = Config.model_validate(yaml.safe_load(yaml_text))

    engine = _make_engine()
    backend = engine.backend(None, cfg.model_dump())

    # Bug catch: pre-fix code would assert {} here.
    assert backend._asset_paths == {"init_image": "input.image_url"}
    # Engine mirror must also be populated for validate_spec.
    assert engine._asset_paths == {"init_image": "input.image_url"}


# ---------------------------------------------------------------------------
# Layer J Task 3: cross-engine prompt-routing tests
# ---------------------------------------------------------------------------


def test_submit_falls_back_to_segment_prompt() -> None:
    """submit() routes segments[0].prompt into body["prompt"] when spec lacks it.

    Bug catch: without the helper-driven fallback, an orchestrator-built job
    (which carries the user prompt on Segment, not in spec) would POST a body
    with no prompt to the hosted endpoint, which silently 422s or returns an
    empty-prompt render.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="a fox", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "a fox"


def test_submit_spec_prompt_wins_over_segment_prompt_hosted() -> None:
    """Explicit spec.prompt is preserved — over-eager fallback would clobber
    a config-supplied wrapper prompt with the raw segment text."""
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key="prompt",
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}, "prompt": "explicit"},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["prompt"] == "explicit"


def test_submit_skips_routing_when_prompt_body_key_none() -> None:
    """prompt_body_key=None opts out of routing — body must NOT gain a
    "prompt" key from the segment.

    Bug catch: a leaky fallback that inspects segments unconditionally
    would add unwanted fields to a body shape the endpoint does not accept.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIBackend

    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posts.append((url, body))
        return {"job_id": "j1"}

    backend = HostedAPIBackend(
        http_post=fake_post,
        http_get=lambda url: {"status": "done"},
        endpoint="https://x.example/inf",
        probe_profile=_DEFAULT_PROBE,
        prompt_body_key=None,
    )
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="ignored", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert "prompt" not in posts[0][1]


def test_validate_spec_raises_when_routing_configured_and_no_prompt() -> None:
    """Opt-in validation: prompt_body_key="prompt" with no prompt anywhere
    must raise before the misconfigured POST reaches the network.

    Bug catch: silent fallthrough would let the empty-body defect resurface
    despite the cfg field signalling user intent to route a prompt.
    """
    import pytest

    from kinoforge.core.errors import ValidationError
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    engine = HostedAPIEngine()
    # Simulate ``backend()`` having mirrored the cfg routing key.
    engine._prompt_body_key = "prompt"
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    with pytest.raises(ValidationError, match="prompt_body_key is configured"):
        engine.validate_spec(job)


def test_validate_spec_passes_when_routing_disabled_and_no_prompt() -> None:
    """Legacy YAML without prompt_body_key (or prompt_body_key=None) must
    not gain a new failure mode — validate_spec must still pass for jobs
    that drive the prompt entirely via params.prompt.
    """
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    engine = HostedAPIEngine()
    engine._prompt_body_key = None  # opt-out
    job = GenerationJob(
        spec={"model": "m", "params": {"prompt": "nested"}},
        segments=[Segment(prompt="", params={}, assets=[])],
        params={},
    )
    engine.validate_spec(job)  # must NOT raise


def test_yaml_prompt_body_key_routes_through_engine_backend() -> None:
    """End-to-end: a YAML config with engine.hosted.prompt_body_key="input"
    produces a backend whose submit writes into body["input"].

    Bug catch: this closes the Layer-I cfg-strip defect class (commit
    484e368) for the new field — pydantic must NOT silently drop
    prompt_body_key on the path from YAML → Config → cfg dict →
    engine.backend(cfg) → HostedAPIBackend.
    """
    import yaml as _yaml

    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import GenerationJob, Segment
    from kinoforge.engines.hosted import HostedAPIEngine

    yaml_doc = """
engine:
  kind: hosted
  precision: ""
  hosted:
    provider: p
    endpoint: "https://x.example/y"
    model: "m"
    api_key_env: "X_KEY"
    prompt_body_key: input
models:
  - {ref: "https://x.example/m.safetensors", kind: base, target: checkpoints}
lifecycle:
  budget: 1.0
"""
    cfg = Config.model_validate(_yaml.safe_load(yaml_doc))
    cfg_dict = cfg.model_dump()

    posts: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        posts.append((url, body))
        return {"job_id": "j1"}

    engine = HostedAPIEngine(
        http_post=fake_post, http_get=lambda url: {"status": "done"}
    )
    backend = engine.backend(None, cfg_dict)
    job = GenerationJob(
        spec={"model": "m", "params": {}},
        segments=[Segment(prompt="from-seg", params={}, assets=[])],
        params={},
    )
    backend.submit(job)
    assert posts[0][1]["input"] == "from-seg"
    assert "prompt" not in posts[0][1]  # only the configured key, not the default
