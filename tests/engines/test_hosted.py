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
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
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
    probe_profile: ModelProfile = _DEFAULT_PROBE,
    declared_flags_map: dict[str, dict[str, bool]] | None = None,
) -> HostedAPIEngine:
    return HostedAPIEngine(
        creds=creds or _DictCreds(_make_creds()),
        http_get=http_get,
        http_post=http_post,
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
