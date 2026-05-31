"""FalEngine + FalBackend unit tests with injected HTTP spies (Layer I Task 11).

Tests cover engine provisioning (cred resolution, optional health probe,
non-None instance rejection), backend submission (POST shape, auth header,
asset injection), and result polling (state transitions, terminal failures,
timeouts, server-supplied URLs). Self-registration under ``"fal"`` is also
exercised.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    CredentialProvider,
    GenerationJob,
    Instance,
    ModelProfile,
    Segment,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _StaticCreds(CredentialProvider):
    """Minimal CredentialProvider backed by an in-memory mapping."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._m = mapping

    def get(self, key: str) -> str | None:  # noqa: D102
        return self._m.get(key)


def _make_job(spec: dict[str, Any] | None = None) -> GenerationJob:
    """Build a minimal GenerationJob with a single segment."""
    return GenerationJob(
        segments=[Segment(prompt="a cat", params={}, assets=[])],
        params={},
        spec=spec if spec is not None else {"prompt": "a cat"},
    )


_TEST_PROFILE = ModelProfile(
    name="fal-stub",
    max_frames=120,
    fps=24,
    supported_modes={"t2v", "i2v"},
    max_resolution=(1024, 1024),
    supports_native_extension=False,
    supports_joint_audio=False,
)


# ---------------------------------------------------------------------------
# Engine provision
# ---------------------------------------------------------------------------


def test_provision_missing_cred_raises_auth_error() -> None:
    """Missing api_key env var raises AuthError mentioning the env var name.

    Bug catch: a generic "auth failed" message without the env-var name
    forces operators to grep the config to discover which variable they
    forgot to export.
    """
    from kinoforge.engines.fal import FalEngine

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": None}))
    cfg = {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": ""}}}
    with pytest.raises(AuthError) as exc:
        eng.provision(None, cfg)
    assert "FAL_KEY" in str(exc.value)


def test_provision_skips_health_when_empty() -> None:
    """Empty health_url means no probe — provision succeeds without GET.

    Bug catch: probing an empty URL would emit a confusing GET '' error
    on configs that intentionally disable health-checks.
    """
    from kinoforge.engines.fal import FalEngine

    pings: list[str] = []

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        pings.append(url)
        return {}

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_spy_get)
    eng.provision(
        None,
        {"engine": {"fal": {"api_key_env": "FAL_KEY", "health_url": ""}}},
    )
    assert pings == []


def test_provision_pings_health_when_set() -> None:
    """A non-empty health_url is GET-ed exactly once during provision.

    Bug catch: skipping the probe on misconfigured endpoints lets bad
    deploys reach submit() before failing.
    """
    from kinoforge.engines.fal import FalEngine

    pings: list[str] = []

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        pings.append(url)
        return {}

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_spy_get)
    eng.provision(
        None,
        {
            "engine": {
                "fal": {
                    "api_key_env": "FAL_KEY",
                    "health_url": "https://q.fal/health",
                }
            }
        },
    )
    assert pings == ["https://q.fal/health"]


def test_provision_health_failure_raises() -> None:
    """A failing health probe surfaces as KinoforgeError, not a raw OSError.

    Bug catch: leaking OSError to the orchestrator breaks the
    'fal endpoint unreachable' contract callers depend on.
    """
    from kinoforge.engines.fal import FalEngine

    def _bad_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        raise OSError("connection refused")

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}), http_get=_bad_get)
    with pytest.raises(KinoforgeError) as exc:
        eng.provision(
            None,
            {
                "engine": {
                    "fal": {
                        "api_key_env": "FAL_KEY",
                        "health_url": "https://q.fal/health",
                    }
                }
            },
        )
    assert "fal endpoint unreachable" in str(exc.value)


def test_provision_rejects_non_none_instance() -> None:
    """provision(instance, cfg) with a non-None instance raises KinoforgeError.

    Bug catch: silently accepting an Instance would create the illusion that
    fal can be paired with a compute provider — it cannot.
    """
    from kinoforge.engines.fal import FalEngine

    eng = FalEngine(creds=_StaticCreds({"FAL_KEY": "abc"}))
    with pytest.raises(KinoforgeError):
        eng.provision(
            Instance(id="i-1", provider="x", status="ready", created_at=0.0),
            {"engine": {"fal": {"api_key_env": "FAL_KEY"}}},
        )


# ---------------------------------------------------------------------------
# Backend submit + result
# ---------------------------------------------------------------------------


def _make_backend(
    *,
    submit_response: dict[str, Any] | None = None,
    status_responses: list[dict[str, Any]] | None = None,
    result_response: dict[str, Any] | None = None,
    asset_paths: dict[str, str] | None = None,
) -> Any:
    """Build a FalBackend with HTTP spies replaying the provided sequence."""
    from kinoforge.engines.fal import FalBackend

    sr = submit_response or {"request_id": "r1"}
    statuses = list(status_responses or [{"status": "COMPLETED"}])
    rr = result_response or {"video": {"url": "https://media.fal/x.mp4"}}

    posts: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    gets: list[tuple[str, dict[str, str]]] = []

    def _spy_post(
        url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        posts.append((url, body, headers))
        return sr

    def _spy_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        gets.append((url, headers or {}))
        # Status URLs return queued/in_progress/completed; non-status URLs
        # return the final result body.
        if url.endswith("/status") or "/status" in url:
            return statuses.pop(0) if statuses else {"status": "COMPLETED"}
        return rr

    backend = FalBackend(
        endpoint="fal-ai/wan/v2.2/t2v",
        queue_base="https://queue.fal.run",
        api_key="abc",
        url_path="video.url",
        asset_paths=asset_paths or {},
        profile=_TEST_PROFILE,
        http_post=_spy_post,
        http_get=_spy_get,
        sleep=lambda _s: None,
        max_poll=50,
    )
    backend._spy_posts = posts  # type: ignore[attr-defined]
    backend._spy_gets = gets  # type: ignore[attr-defined]
    return backend


def test_submit_posts_to_queue_base_endpoint_with_auth() -> None:
    """submit() builds {queue_base}/{endpoint} URL, sets Authorization: Key,
    and forwards the spec as the JSON body.

    Bug catch: an off-by-one in URL composition (e.g. missing slash) or a
    missing Authorization header would silently fail at the real provider.
    """
    backend = _make_backend()
    request_id = backend.submit(_make_job())
    assert request_id == "r1"
    url, body, headers = backend._spy_posts[0]
    assert url == "https://queue.fal.run/fal-ai/wan/v2.2/t2v"
    assert headers["Authorization"] == "Key abc"
    assert headers["Content-Type"] == "application/json"
    assert body == {"prompt": "a cat"}


def test_submit_injects_asset_urls_at_configured_paths() -> None:
    """asset_paths declarations route asset.ref.url into the request body.

    Bug catch: ignoring asset_paths means the provider receives a body
    without the conditioning asset URL — silently producing t2v output
    when the user requested i2v.
    """
    backend = _make_backend(asset_paths={"init_image": "image_url"})
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://i.example/x.png"),
    )
    job = GenerationJob(
        segments=[Segment(prompt="x", params={}, assets=[asset])],
        params={},
        spec={"prompt": "x"},
    )
    backend.submit(job)
    _, body, _ = backend._spy_posts[0]
    assert body["image_url"] == "https://i.example/x.png"


def test_result_polls_until_completed_then_fetches_url() -> None:
    """Status loop transitions PENDING -> COMPLETED, then GETs response_url.

    Bug catch: a poll loop that fetches the response URL on every iteration
    would burst-fire requests at the provider before completion.
    """
    backend = _make_backend(
        status_responses=[
            {"status": "IN_QUEUE"},
            {"status": "IN_PROGRESS"},
            {"status": "COMPLETED"},
        ],
        result_response={"video": {"url": "https://media.fal/v.mp4"}},
    )
    backend.submit(_make_job())
    art = backend.result("r1")
    assert isinstance(art, Artifact)
    assert art.url == "https://media.fal/v.mp4"
    assert art.meta["request_id"] == "r1"
    # Three status polls; final non-status GET fetches the result body.
    status_calls = [u for u, _ in backend._spy_gets if "/status" in u]
    assert len(status_calls) == 3


def test_result_raises_on_failed_status() -> None:
    """Status FAILED surfaces as KinoforgeError with provider logs.

    Bug catch: a code path that returned a stub Artifact on FAILED would
    let bad jobs propagate as successful artifacts.
    """
    backend = _make_backend(
        status_responses=[{"status": "FAILED", "logs": [{"message": "boom"}]}]
    )
    backend.submit(_make_job())
    with pytest.raises(KinoforgeError) as exc:
        backend.result("r1")
    assert "failed" in str(exc.value).lower()


def test_result_raises_on_unknown_status() -> None:
    """An unknown status string is a hard error, not silently retried.

    Bug catch: treating unknown as PENDING would let buggy provider
    responses livelock the poll loop until timeout.
    """
    backend = _make_backend(status_responses=[{"status": "EXPLODED"}])
    backend.submit(_make_job())
    with pytest.raises(KinoforgeError) as exc:
        backend.result("r1")
    assert "unknown status" in str(exc.value).lower()


def test_result_raises_timeout_when_max_poll_exceeded() -> None:
    """Exceeding max_poll iterations raises TimeoutError, not KinoforgeError.

    Bug catch: surfacing as a generic KinoforgeError makes it impossible
    for callers to distinguish 'job never finished' from real provider
    errors.
    """
    backend = _make_backend(status_responses=[{"status": "IN_PROGRESS"}] * 100)
    backend.submit(_make_job())
    with pytest.raises(TimeoutError):
        backend.result("r1")


def test_submit_uses_server_supplied_status_url_for_polling() -> None:
    """If submit response includes status_url, polling uses it verbatim.

    Bug catch: a backend that always reconstructs the status URL from
    queue_base + endpoint would skip the server's canonical URL, breaking
    providers that route requests through request-specific hosts.
    """
    backend = _make_backend(
        submit_response={
            "request_id": "r1",
            "status_url": "https://custom.fal/path/status",
            "response_url": "https://custom.fal/path/result",
        },
        status_responses=[{"status": "COMPLETED"}],
        result_response={"video": {"url": "https://media.fal/v.mp4"}},
    )
    backend.submit(_make_job())
    backend.result("r1")
    status_urls = [u for u, _ in backend._spy_gets if "/status" in u]
    assert "https://custom.fal/path/status" in status_urls


def test_engine_self_registers_under_fal() -> None:
    """Importing engines.fal registers the engine factory under 'fal'.

    Bug catch: a regression that drops the register_engine call would
    leave kinoforge.core.registry unable to construct the engine when
    the CLI loads a fal config.
    """
    import kinoforge.engines.fal  # noqa: F401  (import side effect)
    from kinoforge.core import registry

    factory = registry.get_engine("fal")
    eng = factory()
    assert eng.name == "fal"


def test_backend_endpoints_returns_full_queue_url() -> None:
    """endpoints() returns a queue->{queue_base}/{endpoint} mapping.

    Bug catch: a regression that returns just the queue_base would hide
    the endpoint path from operators inspecting the running pool.
    """
    backend = _make_backend()
    eps = backend.endpoints()
    assert eps == {"queue": "https://queue.fal.run/fal-ai/wan/v2.2/t2v"}


def test_validate_spec_requires_prompt() -> None:
    """validate_spec rejects specs without a non-empty 'prompt'.

    Bug catch: an engine that accepts an empty prompt would submit a
    blank-prompt job to the provider, wasting credits.
    """
    from kinoforge.engines.fal import FalEngine

    eng = FalEngine()
    job = GenerationJob(segments=[], params={}, spec={})
    with pytest.raises((KinoforgeError, ValidationError)):
        eng.validate_spec(job)
