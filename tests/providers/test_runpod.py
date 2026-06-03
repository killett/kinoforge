"""Tests for RunPodProvider — pod + serverless modes.

All I/O is injected via spy callables; no real HTTP or RunPod account needed.

Coverage:
  AC1: find_offers — http_post + filter_offers delegation
  AC2: create_instance (pod mode) — body shape, cred injection, self-terminator embedding
  AC3: create_instance (serverless mode) — different endpoint, caps fields, status=ready
  AC4: endpoints — pod proxy URL pattern, serverless run URL
  AC5: destroy_instance — http_post + polling + idempotent on 404
  AC6: list_instances — http_post list → Instance list
  AC7: cred safety — RUNPOD_API_KEY never in pod body or returned Instance.tags
  AC8: self-term script content — max_lifetime, effective_deadline, heartbeat substrings
  AC9: self-registration — registry.get_provider("runpod")() returns RunPodProvider
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest

from kinoforge.core.errors import CapacityError, TeardownError
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
from tests.providers.conftest_runpod import _load_fixture

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


class MultiResponseHttpPostSpy:
    """Returns a different response for each successive POST call.

    Useful for methods that POST multiple times (e.g. destroy_instance
    issues the terminate mutation then polls with repeated POSTs).
    When the response list is exhausted, returns ``{}``.
    """

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, body))
        if not self._responses:
            return {}
        return self._responses.pop(0)


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

# Real-API captures loaded at import time.  Layer N Task 4 committed these.
_GPU_LIST_RESPONSE: dict[str, Any] = _load_fixture("gpu_types.json")
_POD_CREATE_RESPONSE: dict[str, Any] = _load_fixture("create_pod.json")

# No live-smoke capture for serverless mode (Layer O candidate).
# Hand-crafted dict kept intentionally as the one allowed exception.
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
    """AC1: find_offers calls http_post once and returns filtered Offer list."""
    http_post = HttpPostSpy(response=_GPU_LIST_RESPONSE)
    provider = RunPodProvider(http_post=http_post)

    reqs = HardwareRequirements(min_vram_gb=48, max_usd_per_hr=2.20)
    offers = provider.find_offers(reqs)

    assert len(http_post.calls) == 1, "expected exactly one POST call"
    # Real fixture: 46 GPU types, 7 have vram>=48 GB and uninterruptablePrice<=2.20
    assert len(offers) == 7
    # A100 80GB PCIe is the first non-null-priced 80GB entry in the fixture
    assert offers[0].gpu_type == "NVIDIA A100 80GB PCIe"
    assert offers[0].vram_gb == 80


def test_find_offers_returns_all_when_no_filter_needed() -> None:
    """AC1b: both offers returned when requirements are loose."""
    http_post = HttpPostSpy(response=_GPU_LIST_RESPONSE)
    provider = RunPodProvider(http_post=http_post)

    reqs = HardwareRequirements(min_vram_gb=1, max_usd_per_hr=10.0)
    offers = provider.find_offers(reqs)

    # Real fixture: 25 of 46 GPU types have a non-null uninterruptablePrice ≤ $10/hr
    assert len(offers) == 25


def test_find_offers_skips_null_priced_entries() -> None:
    """Layer N regression — gpuTypes with null prices are filtered out.

    Real RunPod returns ``lowestPrice: null`` (or both nested fields null)
    for GPU types with no currently-available capacity.  ``find_offers``
    drops these silently — a $0 offer would always win ``max_cost_rate``
    filtering and the caller would hit "no instances available" at create.
    """
    http_post = HttpPostSpy(
        response={
            "data": {
                "gpuTypes": [
                    {
                        "id": "NVIDIA H100 PCIe",
                        "displayName": "H100",
                        "memoryInGb": 80,
                        "lowestPrice": None,  # no capacity
                    },
                    {
                        "id": "NVIDIA RTX A5000",
                        "displayName": "RTX A5000",
                        "memoryInGb": 24,
                        "lowestPrice": {
                            "minimumBidPrice": None,
                            "uninterruptablePrice": None,
                        },  # capacity check returned both null
                    },
                    {
                        "id": "NVIDIA A100 80GB PCIe",
                        "displayName": "A100",
                        "memoryInGb": 80,
                        "lowestPrice": {
                            "minimumBidPrice": 1.45,
                            "uninterruptablePrice": 1.89,
                        },
                    },
                ]
            }
        }
    )
    provider = RunPodProvider(http_post=http_post)

    offers = provider.find_offers(HardwareRequirements(min_vram_gb=1))

    by_id = {o.gpu_type: o for o in offers}
    assert "NVIDIA H100 PCIe" not in by_id, "null-price offer leaked through"
    assert "NVIDIA RTX A5000" not in by_id, "both-null-price offer leaked through"
    assert by_id["NVIDIA A100 80GB PCIe"].cost_rate_usd_per_hr == 1.89


def test_find_offers_post_body_contains_query() -> None:
    """Layer N (updated) — find_offers sends the GraphQL query in the POST body.

    After switching all GraphQL requests from GET to POST, the query must
    appear in the POST body dict, not as a URL query parameter.
    """
    http_post = HttpPostSpy(response=_GPU_LIST_RESPONSE)
    provider = RunPodProvider(http_post=http_post)

    provider.find_offers(HardwareRequirements())

    assert len(http_post.calls) == 1
    _url, body = http_post.calls[0]
    assert "query" in body, "POST body must contain a 'query' key"
    assert "gpuTypes" in body["query"], (
        f"POST body query must reference gpuTypes; got: {body['query']!r}"
    )


def test_get_list_destroy_all_send_query_in_post_body() -> None:
    """Layer N (updated) — list_instances, get_instance, and destroy_instance
    poll all send their GraphQL queries in the POST body, not as URL parameters.

    Replaces the retired URL-encoding tests now that every call site uses POST.
    """
    # destroy_instance: 1 terminate POST + up to 1 poll POST (returns gone immediately)
    http_post = MultiResponseHttpPostSpy(
        responses=[
            # list_instances
            {"data": {"myself": {"pods": []}}},
            # get_instance
            {"data": {"pod": {"id": "abc", "desiredStatus": "RUNNING"}}},
            # destroy_instance — terminate mutation
            {"data": {}},
            # destroy_instance — first poll → already gone
            {"data": {"pod": None}},
        ]
    )
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.list_instances()
    provider.get_instance("abc")
    provider.destroy_instance("abc")

    assert len(http_post.calls) >= 4, "expected at least 4 POST calls"
    for _url, body in http_post.calls:
        assert "query" in body, f"POST body missing 'query' key: {body!r}"


def test_find_offers_returns_offer_objects() -> None:
    """AC1c: returned items are Offer dataclass instances."""
    http_post = HttpPostSpy(response=_GPU_LIST_RESPONSE)
    provider = RunPodProvider(http_post=http_post)

    reqs = HardwareRequirements(min_vram_gb=1, max_usd_per_hr=10.0)
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
    assert inst.id == "ia66l3rlto5x66"  # real id from create_pod.json fixture


def test_create_pod_appends_http_protocol_suffix_to_bare_ports() -> None:
    """Bug it catches: sending `ports: "8188"` (no protocol) to RunPod's
    create-pod GraphQL allocates the port but RunPod's HTTP reverse
    proxy at https://{pod_id}-{port}.proxy.runpod.net returns 404 for
    every request — wait_for_ready then polls /system_stats forever
    without ever seeing a 200, eventually times out at boot_timeout_s.

    Observed live 2026-06-02 across T4 attempts 4-6 + diagnostic
    pods wp84fjph9uyuhl / 5sxk83ynwcsxw6: identical bash bootstrap,
    identical image, ports="8188" → proxy 404 forever. Same bootstrap
    with ports="8188/http" via phonehome diagnostic at HEAD 45cf5ab →
    proxy serves http.server response within 1 min.

    Fix appends "/http" to any port lacking an explicit protocol suffix.
    Callers needing TCP (e.g. SSH on port 22) pass "22/tcp" explicitly.
    """
    layer_q_spec = InstanceSpec(
        image="runpod/pytorch:2.1",
        ports=("8188",),  # bare port — caller doesn't know about protocol
        lifecycle=Lifecycle(
            idle_timeout_s=1800,
            job_timeout_s=900,
            time_buffer_s=300,
            max_lifetime_s=7200,
        ),
        tags={"mode": "pod"},
    )
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(layer_q_spec)

    # Exactly one create-pod POST call.
    assert len(http_post.calls) == 1
    body = http_post.calls[0][1]
    sent_ports = body["variables"]["input"]["ports"]
    assert sent_ports == "8188/http", (
        f"expected RunPod-proxy-compatible 'PORT/http' format, got {sent_ports!r}"
    )


def test_create_pod_preserves_explicit_protocol_suffix_on_ports() -> None:
    """Bug it catches: an over-eager normalizer that strips an
    operator-supplied protocol (e.g. "22/tcp" → "22/http"). SSH ports
    + future raw-TCP callers must round-trip their explicit protocol.
    """
    spec_with_explicit = InstanceSpec(
        image="runpod/pytorch:2.1",
        ports=("22/tcp", "8188/http"),  # mix of explicit protocols
        lifecycle=Lifecycle(
            idle_timeout_s=1800,
            job_timeout_s=900,
            time_buffer_s=300,
            max_lifetime_s=7200,
        ),
        tags={"mode": "pod"},
    )
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    provider.create_instance(spec_with_explicit)

    sent_ports = http_post.calls[0][1]["variables"]["input"]["ports"]
    assert sent_ports == "22/tcp,8188/http"


def test_create_pod_populates_endpoints_eagerly_from_spec_ports() -> None:
    """Bug it catches: ``_create_pod`` returns an Instance with empty
    ``endpoints`` even though RunPod's proxy URL pattern is
    deterministic from pod_id + port. ``wait_for_ready`` then raises
    ``ProvisionFailed("pod has no endpoints — cannot construct ready URL")``
    on the very first poll, before the pod has any chance to boot.
    Observed live: T4 third attempt against runpod-comfyui-wan.yaml at
    HEAD 4bbc94b — pod ``i2k0dixescr5eu`` created, immediately killed by
    this check, finally-clause destroyed pod, $0.02 wasted.

    Fix populates ``Instance.endpoints`` with the proxy pattern for each
    port in ``spec.ports`` (the Layer Q render_provision path). URLs
    return 5xx until the in-pod listener binds, but the URL strings
    themselves are stable from creation; wait_for_ready can poll them
    immediately.
    """
    layer_q_spec = InstanceSpec(
        image="runpod/pytorch:2.1",
        ports=("8188",),  # Layer Q path: render_provision.ports → spec.ports
        lifecycle=Lifecycle(
            idle_timeout_s=1800,
            job_timeout_s=900,
            time_buffer_s=300,
            max_lifetime_s=7200,
        ),
        tags={"mode": "pod"},  # no legacy "ports" string in tags
    )
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(layer_q_spec)

    assert "8188" in inst.endpoints
    assert inst.endpoints["8188"] == f"https://{inst.id}-8188.proxy.runpod.net"
    # tags["ports"] mirrors spec.ports so a later
    # provider.endpoints(instance) call reconstructs the same dict (e.g.
    # after process restart that lost in-memory Instance.endpoints).
    assert inst.tags.get("ports") == "8188"


def test_create_pod_populates_endpoints_from_legacy_tag_when_spec_ports_empty(
    pod_spec: InstanceSpec,
) -> None:
    """Bug it catches: the eager-endpoints fix only reads ``spec.ports``
    and ignores legacy callers that put port lists in ``tags["ports"]``
    instead. The existing ``provider.endpoints()`` method does parse the
    tag, so silently disagreeing on the source-of-truth would split the
    URL view (Instance.endpoints empty, provider.endpoints(inst)
    populated). Lockdown: both paths must yield the same dict.
    """
    # pod_spec fixture uses ``tags={"mode": "pod", "ports": "8188,22"}``
    # with ``ports=()`` — the legacy shape.
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)

    inst = provider.create_instance(pod_spec)

    assert set(inst.endpoints.keys()) == {"8188", "22"}
    assert inst.endpoints["8188"] == f"https://{inst.id}-8188.proxy.runpod.net"
    assert inst.endpoints["22"] == f"https://{inst.id}-22.proxy.runpod.net"
    # provider.endpoints() method MUST return the same dict as
    # the eagerly-populated Instance.endpoints — otherwise the eager
    # population is contradicting the method's source-of-truth.
    assert provider.endpoints(inst) == inst.endpoints


# ---------------------------------------------------------------------------
# Layer P Task 7 item #1: typed CapacityError on no-resources mutation
# ---------------------------------------------------------------------------


_CAPACITY_OFFER = Offer(
    id="rtx-4090",
    gpu_type="NVIDIA GeForce RTX 4090",
    vram_gb=24,
    cuda="12.0",
    cost_rate_usd_per_hr=0.69,
    mode="pod",
)


def _spec_with_offer(pod_spec: InstanceSpec) -> InstanceSpec:
    """pod_spec fixture has no offer; attach _CAPACITY_OFFER for these tests."""
    return dataclasses.replace(pod_spec, offer=_CAPACITY_OFFER)


def test_create_instance_raises_capacity_error_on_no_resources(
    pod_spec: InstanceSpec,
) -> None:
    """RunPod mutation error containing 'resources to deploy' -> typed CapacityError.

    Bug catch: if provider re-raises ValueError instead of CapacityError,
    orchestrator-side retry catches nothing and the original PROGRESS:182
    failure shape returns.
    """
    err_msg = "This machine does not have the resources to deploy your pod"
    http_post = HttpPostSpy(response={"errors": [{"message": err_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError) as exc_info:
        provider.create_instance(spec)

    # AC5: message names the offer's gpu_type so operators can debug
    assert _CAPACITY_OFFER.gpu_type in str(exc_info.value)


def test_create_instance_capacity_error_case_insensitive(
    pod_spec: InstanceSpec,
) -> None:
    """AC2: substring match is case-insensitive.

    Bug catch: a future RunPod copy edit (e.g., 'RESOURCES TO DEPLOY')
    silently turns into a ValueError if the match is case-sensitive,
    re-introducing PROGRESS:182.
    """
    err_msg = "MACHINE DOES NOT HAVE THE RESOURCES TO DEPLOY YOUR POD"
    http_post = HttpPostSpy(response={"errors": [{"message": err_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError):
        provider.create_instance(spec)


def test_create_instance_capacity_error_chains_underlying_value_error(
    pod_spec: InstanceSpec,
) -> None:
    """CapacityError.__cause__ preserves original RunPod ValueError.

    Bug catch: dropping `from value_error` (or `from None`) loses the
    raw RunPod message across the orchestrator boundary, blinding
    operators to the actual capacity reason.
    """
    raw_msg = "This machine does not have the resources to deploy your pod"
    http_post = HttpPostSpy(response={"errors": [{"message": raw_msg}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(CapacityError) as exc_info:
        provider.create_instance(spec)

    cause = exc_info.value.__cause__
    assert isinstance(cause, ValueError)
    assert raw_msg in str(cause)


def test_create_instance_non_capacity_error_still_raises_value_error(
    pod_spec: InstanceSpec,
) -> None:
    """Auth / template / malformed-body errors keep raising ValueError.

    Bug catch: an over-eager match (e.g., regex on the whole errors
    block, or unconditional CapacityError on any mutation error) would
    silently turn auth failures into retry-eligible capacity errors,
    causing the orchestrator to retry across every offer for a problem
    that fails identically on each.
    """
    http_post = HttpPostSpy(response={"errors": [{"message": "template not found"}]})
    provider = RunPodProvider(creds=_make_creds(), http_post=http_post)
    spec = _spec_with_offer(pod_spec)

    with pytest.raises(ValueError) as exc_info:
        provider.create_instance(spec)

    # Explicit: must NOT be CapacityError or any subclass
    assert not isinstance(exc_info.value, CapacityError)


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
    """AC5a: destroy POSTs terminate then polls via POST until 404 (empty)."""
    sleep = SleepSpy()
    # First call: terminate mutation → {}
    # Second call: first poll → still present
    # Third call: second poll → gone
    http_post = MultiResponseHttpPostSpy(
        responses=[
            {},  # terminate
            {"data": {"pod": {"id": "pod-abc123"}}},  # poll 1 — still present
            {},  # poll 2 — empty/gone
        ]
    )

    provider = RunPodProvider(http_post=http_post, sleep=sleep)
    provider.destroy_instance("pod-abc123")

    # 1 terminate + 2 poll calls = 3 total
    assert len(http_post.calls) == 3, (
        f"expected 3 POST calls (1 terminate + 2 polls), got {len(http_post.calls)}"
    )


def test_destroy_idempotent_on_immediate_404() -> None:
    """AC5b: destroy is idempotent when instance is already gone (first poll empty)."""
    sleep = SleepSpy()
    http_post = MultiResponseHttpPostSpy(
        responses=[
            {},  # terminate
            {},  # first poll → empty/gone immediately
        ]
    )

    provider = RunPodProvider(http_post=http_post, sleep=sleep)
    provider.destroy_instance("pod-abc123")  # must not raise


def test_destroy_raises_teardown_error_after_max_polls() -> None:
    """AC5c: TeardownError raised when instance never disappears within poll cap."""
    sleep = SleepSpy()
    present_resp: dict[str, Any] = {"data": {"pod": {"id": "pod-abc123"}}}
    # 1 terminate + enough "still present" poll responses to exceed the cap
    http_post = MultiResponseHttpPostSpy(
        responses=[{}] + [present_resp] * 15  # 1 terminate + 15 polls (cap is 10)
    )

    provider = RunPodProvider(http_post=http_post, sleep=sleep)

    with pytest.raises(TeardownError):
        provider.destroy_instance("pod-abc123")


# ---------------------------------------------------------------------------
# AC6: list_instances
# ---------------------------------------------------------------------------


def test_list_instances_returns_instances() -> None:
    """AC6: list_instances maps http_post response to Instance list."""
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
    http_post = HttpPostSpy(response=list_response)
    provider = RunPodProvider(http_post=http_post)

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
    http_post = HttpPostSpy(response=list_response)
    provider = RunPodProvider(http_post=http_post)

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


def test_selfterm_terminate_url_uses_rest_v1_pods_delete() -> None:
    """Bug it catches: ``_terminate()`` calling the wrong RunPod endpoint.

    Live verification 2026-06-03 (probe b9qo9toi3) showed the prior URL
    ``POST https://api.runpod.io/v2/{pod_id}/stop`` is the SERVERLESS
    endpoint — calling it against a pod ID returns 4xx silently
    (selfterm's broad except swallows the error) and the pod survives
    past ``effective_deadline``. The pod-side endpoint is
    ``DELETE https://rest.runpod.io/v1/pods/{pod_id}`` (same one
    ``RunPodProvider.destroy_instance`` + ``tools/preflight.py``
    sweep use).

    Lockdown for the rendered script string: must contain the correct
    REST URL pattern + DELETE method, must NOT contain the broken
    ``/v2/.../stop`` URL.
    """
    script = RENDER(
        idle_timeout=1800,
        max_lifetime=7200,
        job_timeout=900,
        time_buffer=300,
    )
    assert "https://rest.runpod.io/v1/pods/" in script
    assert 'method="DELETE"' in script
    # Negative lockdown: the broken endpoint must NOT reappear via a
    # future revert or upstream copy-paste.
    assert "/v2/" not in script
    assert "/stop" not in script


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
# Layer N regression: env array shape + error / empty-id guards
# ---------------------------------------------------------------------------


def test_create_pod_transforms_env_dict_to_key_value_array() -> None:
    """Layer N regression — env block must be ``[{key, value}, ...]``, not dict.

    RunPod's GraphQL EnvironmentVariableInput requires an array of
    {key, value} pairs; passing a plain dict yields BAD_USER_INPUT.
    """
    http_post = HttpPostSpy(response=_POD_CREATE_RESPONSE)
    creds = FakeCreds({"RUNPOD_API_KEY": "k", "RUNPOD_TERMINATE_KEY": "t"})
    provider = RunPodProvider(creds=creds, http_post=http_post)

    spec = InstanceSpec(
        image="runpod/pytorch:2.1",
        lifecycle=Lifecycle(
            idle_timeout_s=1800,
            job_timeout_s=900,
            time_buffer_s=300,
            max_lifetime_s=7200,
        ),
        tags={"mode": "pod"},
    )
    provider.create_instance(spec)

    _url, body = http_post.calls[0]
    env_field = body["variables"]["input"]["env"]
    assert isinstance(env_field, list), (
        f"env must be a list of {{key, value}} pairs, got: {type(env_field).__name__}"
    )
    for entry in env_field:
        assert isinstance(entry, dict)
        assert set(entry.keys()) == {"key", "value"}, (
            f"each env entry must have exactly {{key, value}}: {entry!r}"
        )


def test_create_pod_raises_on_graphql_errors_block() -> None:
    """Layer N regression — create-pod must raise when mutation returns errors.

    Bug it catches: silently returned ``Instance(id="")`` if the mutation
    failed, leaving the orchestrator with no way to track a possibly-real
    pod and risking cost leak.
    """
    http_post = HttpPostSpy(
        response={
            "errors": [
                {"message": "Field key required"},
                {"message": "Field value required"},
            ]
        }
    )
    creds = FakeCreds({"RUNPOD_API_KEY": "k", "RUNPOD_TERMINATE_KEY": "t"})
    provider = RunPodProvider(creds=creds, http_post=http_post)

    spec = InstanceSpec(
        image="x",
        lifecycle=Lifecycle(),
        tags={"mode": "pod"},
    )
    import pytest as _pytest

    with _pytest.raises(ValueError, match="RunPod create-pod mutation returned errors"):
        provider.create_instance(spec)


def test_create_pod_raises_on_empty_pod_id() -> None:
    """Layer N regression — create-pod must raise when response carries no id."""
    http_post = HttpPostSpy(
        response={"data": {"podFindAndDeployOnDemand": {"id": "", "desiredStatus": ""}}}
    )
    creds = FakeCreds({"RUNPOD_API_KEY": "k", "RUNPOD_TERMINATE_KEY": "t"})
    provider = RunPodProvider(creds=creds, http_post=http_post)

    spec = InstanceSpec(
        image="x",
        lifecycle=Lifecycle(),
        tags={"mode": "pod"},
    )
    import pytest as _pytest

    with _pytest.raises(ValueError, match="no pod id"):
        provider.create_instance(spec)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Layer N: real-shape lockdown
# ---------------------------------------------------------------------------


def test_find_offers_real_shape_required_keys() -> None:
    """Layer N lockdown — every gpuTypes entry has fields production reads.

    Catches: a future RunPod schema rename (e.g. memoryInGb → vramGb) that
    breaks ``find_offers`` silently if the fixture is regenerated and the
    production code is not updated.  Asserts only that the top-level
    fields exist; the nested ``lowestPrice`` may legitimately be null.
    """
    fixture = _load_fixture("gpu_types.json")
    gpus = fixture["data"]["gpuTypes"]
    assert gpus, "gpu_types fixture has no entries"
    for gpu in gpus:
        assert "id" in gpu, f"missing id in {gpu}"
        assert "memoryInGb" in gpu, f"missing memoryInGb in {gpu}"
        assert "lowestPrice" in gpu, f"missing lowestPrice in {gpu}"


def test_pod_status_mapping_covers_real_statuses() -> None:
    """Layer N lockdown — _runpod_status_to_kinoforge covers real statuses.

    Catches: RunPod adds a new desiredStatus (e.g. PAUSED) and the
    production code's fallback maps it to "starting", silently
    miscategorising real instance state.

    Note: the smoke ran fast enough that ``list_pods.json`` captured an
    empty pods list and ``get_pod.json`` captured an in-flight RUNNING
    state.  If either fixture has no observable statuses, the test
    skips with a clear message — the assertion only fires when there's
    real data to lock down.
    """
    from kinoforge.providers.runpod import _runpod_status_to_kinoforge

    observed: set[str] = set()
    for fixture_name in ("list_pods.json", "get_pod.json"):
        fixture = _load_fixture(fixture_name)
        data = fixture.get("data", {})
        if "myself" in data and data["myself"]:
            for pod in data["myself"].get("pods", []):
                status = pod.get("desiredStatus")
                if status:
                    observed.add(status)
        if "pod" in data and data["pod"]:
            status = data["pod"].get("desiredStatus")
            if status:
                observed.add(status)

    if not observed:
        pytest.skip(
            "no desiredStatus values observed in committed fixtures; "
            "regen with KINOFORGE_LIVE_TESTS=1 KINOFORGE_SAVE_FIXTURES=1 "
            "to lock the mapping down",
        )

    known_runpod_statuses = {"RUNNING", "EXITED", "DEAD"}
    valid_kinoforge_statuses = {"ready", "stopped", "terminated"}

    for status in observed:
        assert status.upper() in known_runpod_statuses, (
            f"unknown RunPod status {status!r} — "
            f"_runpod_status_to_kinoforge needs an explicit entry"
        )
        kf = _runpod_status_to_kinoforge(status)
        assert kf in valid_kinoforge_statuses, (
            f"status {status!r} maps to fallback {kf!r}; "
            f"add explicit mapping in _runpod_status_to_kinoforge"
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_env(body: dict[str, Any]) -> dict[str, str]:
    """Walk nested dicts/lists to find the first 'env' value.

    Handles both the old dict form ``{"KEY": "val"}`` and the new RunPod
    GraphQL array form ``[{"key": "KEY", "value": "val"}, ...]``, converting
    the latter to a plain dict so callers remain shape-agnostic.
    """
    if "env" in body:
        val = body["env"]
        if isinstance(val, dict):
            return val
        if isinstance(val, list):
            # RunPod EnvironmentVariableInput array: [{key, value}, ...]
            return {entry["key"]: entry["value"] for entry in val if "key" in entry}
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


# ---------------------------------------------------------------------------
# AC10: find_instance_by_tag
# ---------------------------------------------------------------------------


def test_find_instance_by_tag_returns_matching_ready_pod() -> None:
    """Ready instance with matching tag → returned."""
    provider = RunPodProvider(http_post=HttpPostSpy())
    matching = Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        tags={"kinoforge.layer": "layer-p-smoke", "mode": "pod"},
    )
    provider.list_instances = lambda: [matching]  # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")

    assert result is not None
    assert result.id == "pod-abc"


def test_find_instance_by_tag_skips_non_ready() -> None:
    """Matching tag but status != 'ready' → None."""
    provider = RunPodProvider(http_post=HttpPostSpy())
    starting = Instance(
        id="pod-xyz",
        provider="runpod",
        status="starting",
        created_at=0.0,
        tags={"kinoforge.layer": "layer-p-smoke"},
    )
    provider.list_instances = lambda: [starting]  # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")

    assert result is None


def test_find_instance_by_tag_no_match_returns_none() -> None:
    """No instance carries the requested tag → None."""
    provider = RunPodProvider(http_post=HttpPostSpy())
    other = Instance(
        id="pod-other",
        provider="runpod",
        status="ready",
        created_at=0.0,
        tags={"kinoforge.layer": "different-layer"},
    )
    provider.list_instances = lambda: [other]  # type: ignore[method-assign]

    result = provider.find_instance_by_tag("kinoforge.layer", "layer-p-smoke")

    assert result is None
