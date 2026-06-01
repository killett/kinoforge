"""Tests for RunPodProvider — pod + serverless modes.

All I/O is injected via spy callables; no real HTTP or RunPod account needed.

Coverage:
  AC1: find_offers — http_get + filter_offers delegation
  AC2: create_instance (pod mode) — body shape, cred injection, self-terminator embedding
  AC3: create_instance (serverless mode) — different endpoint, caps fields, status=ready
  AC4: endpoints — pod proxy URL pattern, serverless run URL
  AC5: destroy_instance — http_post + polling + idempotent on 404
  AC6: list_instances — http_get list → Instance list
  AC7: cred safety — RUNPOD_API_KEY never in pod body or returned Instance.tags
  AC8: self-term script content — max_lifetime, effective_deadline, heartbeat substrings
  AC9: self-registration — registry.get_provider("runpod")() returns RunPodProvider
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import (
    CredentialProvider,
    HardwareRequirements,
    Instance,
    InstanceSpec,
    Lifecycle,
    Offer,
)
from kinoforge.providers.runpod import RunPodProvider
from kinoforge.providers.runpod.selfterm import RENDER

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeCreds(CredentialProvider):
    """Minimal CredentialProvider that reads from a plain dict."""

    def __init__(self, map: dict[str, str]) -> None:  # noqa: A002
        self._map = map

    def get(self, key: str) -> str | None:
        """Return the value for *key* or None."""
        return self._map.get(key)


def _make_creds() -> FakeCreds:
    return FakeCreds(
        {
            "RUNPOD_TERMINATE_KEY": "t-secret",
            "RUNPOD_API_KEY": "main-secret",
        }
    )


# ---------------------------------------------------------------------------
# Spy callables
# ---------------------------------------------------------------------------


class HttpPostSpy:
    """Records every (url, body) call; returns the configured response."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._response: dict[str, Any] = response or {}

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, body))
        return self._response


class HttpGetSpy:
    """Returns a different response for each successive call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if not self._responses:
            return {}
        return self._responses.pop(0)


class SleepSpy:
    """Records sleep durations; never actually sleeps."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, secs: float) -> None:
        self.calls.append(secs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_GPU_LIST_RESPONSE: dict[str, Any] = {
    "data": {
        "gpuTypes": [
            {
                "id": "NVIDIA RTX A4000",
                "displayName": "RTX A4000",
                "memoryInGb": 16,
                "secureCloud": True,
                "communityCloud": True,
                "lowestPrice": {"minimumBidPrice": 0.24, "uninterruptablePrice": 0.32},
            },
            {
                "id": "NVIDIA A100 80GB PCIe",
                "displayName": "A100 80GB",
                "memoryInGb": 80,
                "secureCloud": True,
                "communityCloud": True,
                "lowestPrice": {"minimumBidPrice": 1.45, "uninterruptablePrice": 1.89},
            },
        ]
    }
}

_POD_CREATE_RESPONSE: dict[str, Any] = {
    "data": {
        "podFindAndDeployOnDemand": {
            "id": "pod-abc123",
            "desiredStatus": "RUNNING",
            "imageName": "runpod/pytorch:2.1",
        }
    }
}

_SERVERLESS_CREATE_RESPONSE: dict[str, Any] = {
    "data": {
        "saveTemplate": {
            "id": "sl-xyz789",
        }
    }
}


@pytest.fixture()
def pod_spec() -> InstanceSpec:
    """InstanceSpec for pod mode with two exposed ports."""
    return InstanceSpec(
        image="runpod/pytorch:2.1",
        lifecycle=Lifecycle(
            idle_timeout_s=1800,
            job_timeout_s=900,
            time_buffer_s=300,
            max_lifetime_s=7200,
        ),
        tags={"mode": "pod", "ports": "8188,22"},
    )


@pytest.fixture()
def serverless_spec() -> InstanceSpec:
    """InstanceSpec for serverless mode."""
    return InstanceSpec(
        image="runpod/pytorch:2.1",
        lifecycle=Lifecycle(
            job_timeout_s=600,
            max_workers=3,
            max_in_flight=5,
        ),
        tags={"mode": "serverless"},
    )


# ---------------------------------------------------------------------------
# AC1: find_offers
# ---------------------------------------------------------------------------


def test_find_offers_fetches_gpu_list_and_filters() -> None:
    """AC1: find_offers calls http_get once and returns filtered Offer list."""
    http_get = HttpGetSpy([_GPU_LIST_RESPONSE])
    provider = RunPodProvider(http_get=http_get)

    reqs = HardwareRequirements(min_vram_gb=48, max_cost_rate_usd_per_hr=2.20)
    offers = provider.find_offers(reqs)

    assert len(http_get.calls) == 1, "expected exactly one GET call"
    # Only A100 (80 GB) meets min_vram_gb=48
    assert len(offers) == 1
    assert offers[0].gpu_type == "NVIDIA A100 80GB PCIe"
    assert offers[0].vram_gb == 80


def test_find_offers_returns_all_when_no_filter_needed() -> None:
    """AC1b: both offers returned when requirements are loose."""
    http_get = HttpGetSpy([_GPU_LIST_RESPONSE])
    provider = RunPodProvider(http_get=http_get)

    reqs = HardwareRequirements(min_vram_gb=1, max_cost_rate_usd_per_hr=10.0)
    offers = provider.find_offers(reqs)

    assert len(offers) == 2


def test_find_offers_url_encodes_query_string() -> None:
    """Layer N regression — find_offers MUST URL-encode the GraphQL query.

    Python 3.13's urllib raises ``http.client.InvalidURL`` if a URL contains
    control characters (including spaces).  Layer N's first live-smoke run
    hit this on the real RunPod API because the production code previously
    concatenated the raw query string directly.  Lock the fix down.
    """
    http_get = HttpGetSpy([_GPU_LIST_RESPONSE])
    provider = RunPodProvider(http_get=http_get)

    provider.find_offers(HardwareRequirements())

    sent_url = http_get.calls[0]
    assert " " not in sent_url, f"raw space in URL: {sent_url!r}"
    assert "%20" in sent_url or "+" in sent_url, (
        f"GraphQL query string not URL-encoded: {sent_url!r}"
    )


def test_get_list_destroy_all_url_encode_their_queries() -> None:
    """Layer N regression — every GET site URL-encodes its GraphQL query.

    Covers ``list_instances``, ``get_instance``, and the ``destroy_instance``
    poll loop.  Each call site previously hit the same control-character bug
    that ``find_offers`` did.
    """
    http_get = HttpGetSpy(
        [
            {"data": {"myself": {"pods": []}}},
            {"data": {"pod": {"id": "abc", "desiredStatus": "RUNNING"}}},
            {"data": {"pod": None}},
        ]
    )
    http_post = HttpPostSpy(response={"data": {}})
    provider = RunPodProvider(
        creds=_make_creds(), http_post=http_post, http_get=http_get
    )

    provider.list_instances()
    provider.get_instance("abc")
    provider.destroy_instance("abc")

    assert len(http_get.calls) >= 3, "expected at least 3 GET calls"
    for url in http_get.calls:
        assert " " not in url, f"raw space in URL: {url!r}"


def test_find_offers_returns_offer_objects() -> None:
    """AC1c: returned items are Offer dataclass instances."""
    http_get = HttpGetSpy([_GPU_LIST_RESPONSE])
    provider = RunPodProvider(http_get=http_get)

    reqs = HardwareRequirements(min_vram_gb=1, max_cost_rate_usd_per_hr=10.0)
    offers = provider.find_offers(reqs)

    for o in offers:
        assert isinstance(o, Offer)


# ---------------------------------------------------------------------------
# AC2: create_instance — pod mode
# ---------------------------------------------------------------------------


def test_create_pod_calls_http_post(pod_spec: InstanceSpec) -> None:
    """AC2a: pod create calls http_post once."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(pod_spec)

    assert len(http_post.calls) == 1


def test_create_pod_body_contains_image(pod_spec: InstanceSpec) -> None:
    """AC2b: pod body references the spec image."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(pod_spec)

    _url, body = http_post.calls[0]
    # Walk the body to find image somewhere in the nested query / variables
    body_str = str(body)
    assert "runpod/pytorch:2.1" in body_str


def test_create_pod_injects_terminate_key(pod_spec: InstanceSpec) -> None:
    """AC2c: env block contains RUNPOD_TERMINATE_KEY set to the scoped key."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(pod_spec)

    _url, body = http_post.calls[0]
    env = _extract_env(body)
    assert "RUNPOD_TERMINATE_KEY" in env
    assert env["RUNPOD_TERMINATE_KEY"] == "t-secret"


def test_create_pod_does_not_inject_main_api_key(pod_spec: InstanceSpec) -> None:
    """AC2d / AC7: env block NEVER contains RUNPOD_API_KEY."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(pod_spec)

    _url, body = http_post.calls[0]
    env = _extract_env(body)
    assert "RUNPOD_API_KEY" not in env


def test_create_pod_embeds_selfterm_script(pod_spec: InstanceSpec) -> None:
    """AC2e: env block contains KINOFORGE_SELFTERM_SCRIPT with lifecycle params."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(pod_spec)

    _url, body = http_post.calls[0]
    env = _extract_env(body)
    assert "KINOFORGE_SELFTERM_SCRIPT" in env
    script = env["KINOFORGE_SELFTERM_SCRIPT"]
    assert "max_lifetime" in script
    assert "effective_deadline" in script
    assert "heartbeat" in script


def test_create_pod_returns_instance_starting(pod_spec: InstanceSpec) -> None:
    """AC2f: returned Instance has provider='runpod' and status='starting'."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(pod_spec)

    assert inst.provider == "runpod"
    assert inst.status == "starting"
    assert inst.id == "pod-abc123"


# ---------------------------------------------------------------------------
# AC3: create_instance — serverless mode
# ---------------------------------------------------------------------------


def test_create_serverless_returns_ready(serverless_spec: InstanceSpec) -> None:
    """AC3a: serverless instance returns status='ready' immediately."""
    http_post = HttpPostSpy(response=_SERVERLESS_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(serverless_spec)

    assert inst.provider == "runpod"
    assert inst.status == "ready"
    assert inst.id == "sl-xyz789"


def test_create_serverless_no_selfterm_script(serverless_spec: InstanceSpec) -> None:
    """AC3b: serverless body must NOT contain KINOFORGE_SELFTERM_SCRIPT."""
    http_post = HttpPostSpy(response=_SERVERLESS_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(serverless_spec)

    _url, body = http_post.calls[0]
    body_str = str(body)
    assert "KINOFORGE_SELFTERM_SCRIPT" not in body_str


def test_create_serverless_uses_lifecycle_caps(serverless_spec: InstanceSpec) -> None:
    """AC3c: serverless body references max_workers and max_in_flight."""
    http_post = HttpPostSpy(response=_SERVERLESS_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(serverless_spec)

    _url, body = http_post.calls[0]
    body_str = str(body)
    # The values 3 (max_workers) and 5 (max_in_flight) must appear somewhere
    assert "3" in body_str
    assert "5" in body_str


# ---------------------------------------------------------------------------
# AC4: endpoints
# ---------------------------------------------------------------------------


def test_pod_endpoints_proxy_url_pattern() -> None:
    """AC4a: pod endpoints use https://{id}-{port}.proxy.runpod.net pattern."""
    provider = RunPodProvider()
    inst = Instance(
        id="pod-abc123",
        provider="runpod",
        status="starting",
        created_at=0.0,
        tags={"mode": "pod", "ports": "8188,22"},
    )

    eps = provider.endpoints(inst)

    assert "8188" in eps
    assert "22" in eps
    pattern = re.compile(r"https://pod-abc123-\d+\.proxy\.runpod\.net")
    for port, url in eps.items():
        assert pattern.match(url), f"port {port!r} URL {url!r} does not match pattern"


def test_serverless_endpoints_run_url() -> None:
    """AC4b: serverless endpoints return the /run URL."""
    provider = RunPodProvider()
    inst = Instance(
        id="sl-xyz789",
        provider="runpod",
        status="ready",
        created_at=0.0,
        tags={"mode": "serverless"},
    )

    eps = provider.endpoints(inst)

    assert "run" in eps
    assert eps["run"] == "https://api.runpod.ai/v2/sl-xyz789/run"


# ---------------------------------------------------------------------------
# AC5: destroy_instance
# ---------------------------------------------------------------------------


def test_destroy_polls_until_gone() -> None:
    """AC5a: destroy calls terminate then polls until 404 (empty)."""
    sleep = SleepSpy()
    http_post = HttpPostSpy(response={})
    # First GET → still present; second GET → 404/empty
    http_get = HttpGetSpy([{"data": {"pod": {"id": "pod-abc123"}}}, {}])

    provider = RunPodProvider(http_post=http_post, http_get=http_get, sleep=sleep)
    provider.destroy_instance("pod-abc123")

    assert len(http_post.calls) == 1, "expected one terminate call"
    assert len(http_get.calls) == 2, "expected two poll calls"


def test_destroy_idempotent_on_immediate_404() -> None:
    """AC5b: destroy is idempotent when instance is already gone (first poll empty)."""
    sleep = SleepSpy()
    http_post = HttpPostSpy(response={})
    http_get = HttpGetSpy([{}])  # already gone

    provider = RunPodProvider(http_post=http_post, http_get=http_get, sleep=sleep)
    provider.destroy_instance("pod-abc123")  # must not raise


def test_destroy_raises_teardown_error_after_max_polls() -> None:
    """AC5c: TeardownError raised when instance never disappears within poll cap."""
    sleep = SleepSpy()
    http_post = HttpPostSpy(response={})
    # Always return 'still present'
    present_resp: dict[str, Any] = {"data": {"pod": {"id": "pod-abc123"}}}
    http_get = HttpGetSpy([present_resp] * 15)  # more than the cap

    provider = RunPodProvider(http_post=http_post, http_get=http_get, sleep=sleep)

    with pytest.raises(TeardownError):
        provider.destroy_instance("pod-abc123")


# ---------------------------------------------------------------------------
# AC6: list_instances
# ---------------------------------------------------------------------------


def test_list_instances_returns_instances() -> None:
    """AC6: list_instances maps http_get response to Instance list."""
    list_response: dict[str, Any] = {
        "data": {
            "myself": {
                "pods": [
                    {"id": "pod-1", "desiredStatus": "RUNNING", "imageName": "img:1"},
                    {"id": "pod-2", "desiredStatus": "RUNNING", "imageName": "img:2"},
                ]
            }
        }
    }
    http_get = HttpGetSpy([list_response])
    provider = RunPodProvider(http_get=http_get)

    instances = provider.list_instances()

    assert len(instances) == 2
    ids = {i.id for i in instances}
    assert ids == {"pod-1", "pod-2"}
    for inst in instances:
        assert isinstance(inst, Instance)
        assert inst.provider == "runpod"


def test_list_instances_empty_when_no_pods() -> None:
    """AC6b: list_instances returns [] when pods list is empty."""
    list_response: dict[str, Any] = {"data": {"myself": {"pods": []}}}
    http_get = HttpGetSpy([list_response])
    provider = RunPodProvider(http_get=http_get)

    assert provider.list_instances() == []


# ---------------------------------------------------------------------------
# AC7: cred safety — returned Instance carries no credentials
# ---------------------------------------------------------------------------


def test_instance_tags_contain_no_api_key(pod_spec: InstanceSpec) -> None:
    """AC7: returned Instance.tags must not contain the main API key value."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(pod_spec)

    tags_str = str(inst.tags)
    assert "main-secret" not in tags_str
    assert "t-secret" not in tags_str


def test_instance_tags_contain_no_terminate_key(pod_spec: InstanceSpec) -> None:
    """AC7b: returned Instance.tags must not contain the terminate key value."""
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(pod_spec)

    assert "RUNPOD_TERMINATE_KEY" not in inst.tags
    assert "RUNPOD_API_KEY" not in inst.tags


# ---------------------------------------------------------------------------
# AC8: self-term script content
# ---------------------------------------------------------------------------


def test_selfterm_render_contains_required_substrings() -> None:
    """AC8: RENDER() produces a script with max_lifetime, effective_deadline, heartbeat."""
    script = RENDER(
        idle_timeout=1800,
        max_lifetime=7200,
        job_timeout=900,
        time_buffer=300,
    )
    assert "max_lifetime" in script
    assert "effective_deadline" in script
    assert "heartbeat" in script


def test_selfterm_render_embeds_values() -> None:
    """AC8b: RENDER() substitutes numeric lifecycle values into the script."""
    script = RENDER(
        idle_timeout=999,
        max_lifetime=8888,
        job_timeout=777,
        time_buffer=111,
    )
    assert "999" in script
    assert "8888" in script


# ---------------------------------------------------------------------------
# AC9: self-registration
# ---------------------------------------------------------------------------


def test_self_registration() -> None:
    """AC9: importing the module registers 'runpod' in the global registry."""
    import kinoforge.providers.runpod  # noqa: F401
    from kinoforge.core import registry

    factory = registry.get_provider("runpod")
    p = factory()
    assert isinstance(p, RunPodProvider)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Layer N regression: Authorization header injection
# ---------------------------------------------------------------------------


def test_default_seams_inject_api_key_query_param() -> None:
    """Layer N regression — default seams append ?api_key=<key>, NOT Bearer.

    Bug it catches: previous fix used Authorization: Bearer header but
    RunPod's legacy GraphQL endpoint returns 403 to Bearer auth.  The
    actual auth scheme is the api_key query parameter.  Verified against
    the real endpoint during Layer N live smoke.
    """
    import urllib.request as _urlreq

    from kinoforge.providers.runpod import _make_default_http_seams

    authed_post, authed_get = _make_default_http_seams("sk-fake-key")

    captured: dict[str, Any] = {}

    def _fake_urlopen(req: Any) -> Any:  # noqa: ANN401
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)

        class _Resp:
            def __enter__(self_inner: object) -> object:
                return self_inner

            def __exit__(
                self_inner: object,
                exc_type: object,
                exc: object,
                tb: object,
            ) -> None:
                return None

            def read(self_inner: object) -> bytes:
                return b'{"data": {}}'

        return _Resp()

    real_urlopen = _urlreq.urlopen
    _urlreq.urlopen = _fake_urlopen  # type: ignore[assignment]
    try:
        authed_get("https://api.example.com/graphql?query=%7Bx%7D")
        url_with_existing_query = captured["url"]
        authed_get("https://api.example.com/graphql")
        url_no_query = captured["url"]
    finally:
        _urlreq.urlopen = real_urlopen

    assert "api_key=sk-fake-key" in url_with_existing_query, (
        f"api_key not appended to URL with existing query: {url_with_existing_query!r}"
    )
    assert url_with_existing_query.count("?") == 1, (
        f"second ? introduced instead of &: {url_with_existing_query!r}"
    )
    assert "&api_key=sk-fake-key" in url_with_existing_query, (
        "api_key must be appended with & when URL already has a ? query"
    )
    assert "?api_key=sk-fake-key" in url_no_query, (
        f"api_key not appended to URL without existing query: {url_no_query!r}"
    )

    # No Bearer header — that scheme returns 403 against real RunPod.
    assert "Authorization" not in captured["headers"], (
        f"Authorization header leaked: {captured['headers']!r}"
    )

    # Content-Type: application/json bypasses RunPod GraphQL's CSRF block on
    # GETs — without it the server returns HTTP 400 with "potential
    # Cross-Site Request Forgery".  Verified empirically on the real
    # endpoint during Layer N live smoke.
    assert captured["headers"].get("Content-type") == "application/json", (
        f"Content-Type missing or wrong: {captured['headers']!r}"
    )

    # User-Agent must NOT be Python-urllib/* — RunPod's edge layer blocks
    # that prefix with HTTP 403.  Any non-default UA passes.  Verified
    # empirically during Layer N live smoke.
    ua = captured["headers"].get("User-agent", "")
    assert ua and not ua.lower().startswith("python-urllib"), (
        f"User-Agent must override Python-urllib default: {ua!r}"
    )


def test_default_seams_without_key_skip_auth() -> None:
    """When no api_key is supplied, the default seams are bare urllib funcs."""
    from kinoforge.providers.runpod import (
        _make_default_http_seams,
        _urllib_get_json,
        _urllib_post_json,
    )

    post, get = _make_default_http_seams(None)
    assert post is _urllib_post_json
    assert get is _urllib_get_json

    post2, get2 = _make_default_http_seams("")
    assert post2 is _urllib_post_json
    assert get2 is _urllib_get_json


def test_provider_init_resolves_default_seams_from_creds() -> None:
    """RunPodProvider(creds=...) uses authed defaults when no seams passed."""
    from kinoforge.providers.runpod import RunPodProvider, _urllib_get_json

    class _Creds(CredentialProvider):
        def get(self, key: str) -> str | None:
            """Return a fake API key for RUNPOD_API_KEY."""
            return "sk-test-key" if key == "RUNPOD_API_KEY" else None

    p = RunPodProvider(creds=_Creds())
    # Default seam was replaced with an authed closure — not the bare urllib fn
    assert p._http_get is not _urllib_get_json


def test_default_factory_uses_env_credentials() -> None:
    """The "runpod" provider factory wires EnvCredentialProvider in."""
    import os

    from kinoforge.core import registry

    os.environ["RUNPOD_API_KEY"] = "sk-env-test-key"
    try:
        factory = registry.get_provider("runpod")
        provider = factory()
        assert isinstance(provider, RunPodProvider)
        assert provider._creds is not None, "factory dropped creds"
        assert provider._creds.get("RUNPOD_API_KEY") == "sk-env-test-key"
    finally:
        os.environ.pop("RUNPOD_API_KEY", None)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_env(body: dict[str, Any]) -> dict[str, str]:
    """Walk nested dicts/lists to find the first 'env' dict."""
    if "env" in body:
        val = body["env"]
        if isinstance(val, dict):
            return val
    for v in body.values():
        if isinstance(v, dict):
            result = _extract_env(v)
            if result is not None:
                return result
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    result = _extract_env(item)
                    if result is not None:
                        return result
    return {}
