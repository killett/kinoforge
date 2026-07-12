"""``_resolve_warm_instance`` must populate endpoints from ledger tags.

Regression context (2026-06-18 Wan 1.3B CLI warm-reuse smoke):
    After the HeartbeatLoop last_heartbeat fallback fix landed
    (``be33a67``), cmd 2 correctly classified pod ``di506yuuczuhht``
    as LIVE and ``_scan_warm_candidates`` attached. Generation then
    immediately failed with::

        kinoforge.core.errors.ProvisionFailed:
            pod 'di506yuuczuhht' has no endpoints — cannot construct
            ready URL

    The error fires from ``ComfyUIEngine.wait_for_ready``
    (``engines/comfyui/__init__.py:1472``) which asserts
    ``instance.endpoints`` is non-empty before constructing the
    ready URL.

Root cause: step 7 of ``_resolve_warm_instance``
(``cli/_commands.py:1080``) calls
``provider.get_instance(instance_id)`` and returns that Instance
verbatim. For RunPod, ``get_instance`` -> ``_pod_to_instance``
(``providers/runpod/__init__.py:904``) hard-codes
``tags={"mode": "pod"}`` and leaves ``endpoints={}`` because the
``Pod`` GraphQL query only returns ``id`` / ``desiredStatus`` /
``imageName`` (the same gap the orchestrator's cold path
explicitly defends against via ``dataclasses.replace`` at
``core/orchestrator.py:723-726``).

Fix: merge ledger tags onto the provider-fresh instance (provider
tags take precedence so e.g. ``"mode": "pod"`` survives), then call
``provider.endpoints(instance)`` (which reads ``tags["ports"]`` to
build the proxy URL dict), then ``dataclasses.replace`` the
instance with the populated tags + endpoints fields.

Cross-references:
  - Same root cause family as ``e33d564`` (orchestrator polling
    loop preserved created_at/tags/cost_rate; this is the
    warm-attach side of the same Instance-impoverishment surface).
  - B5b deferral spec §3 names the local ledger as authoritative
    for create-time fields under same-host scope; this patch
    honours that by reading ledger tags for the ports/endpoints
    field rather than the broken wire-level GraphQL response.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import _resolve_warm_instance
from kinoforge.cli.context import SessionContext
from kinoforge.core.interfaces import Instance

_NOW = 1_700_000_000.0
_POD_ID = "di-test-warm-endpoints"
_PROXY_URL_PATTERN = "https://{pod_id}-{port}.proxy.runpod.net"


def _entry_with_ports() -> dict[str, Any]:
    """Ledger entry shaped like RunPod's orchestrator persists post-fix.

    ``tags`` carries ``ports`` (set by RunPod's ``_create_pod`` and preserved
    across the polling loop by ``e33d564``), plus the ``kinoforge_*`` tags
    the orchestrator stamps on every pod.
    """
    return {
        "id": _POD_ID,
        "provider": "runpod-test",
        "created_at": _NOW - 60.0,
        "cost_rate_usd_per_hr": 0.16,
        "tags": {
            "kinoforge_engine": "comfyui",
            "kinoforge_key": "ab12cd34ef56",
            "ports": "8188",
        },
        "last_heartbeat": _NOW - 5.0,
        "heartbeat_thread_tick": _NOW - 5.0,
    }


class _RunPodShapeProvider:
    """Provider that reproduces RunPod's get_instance impoverishment.

    ``get_instance`` returns a minimal Instance with empty endpoints +
    sparse tags (``{"mode": "pod"}``), matching
    ``providers/runpod/__init__.py:_pod_to_instance``. ``endpoints``
    reads ``instance.tags["ports"]`` per RunPod's contract.
    """

    def list_instances(self) -> list[Instance]:
        return [
            Instance(
                id=_POD_ID,
                provider="runpod-test",
                status="ready",
                created_at=_NOW - 60.0,
                endpoints={},
                tags={"mode": "pod"},
            )
        ]

    def get_instance(self, iid: str) -> Instance:
        return Instance(
            id=iid,
            provider="runpod-test",
            status="ready",
            created_at=_NOW - 60.0,
            endpoints={},
            tags={"mode": "pod"},
        )

    def endpoints(self, instance: Instance) -> dict[str, str]:
        ports_raw = instance.tags.get("ports", "")
        ports = [p.strip() for p in ports_raw.split(",") if p.strip()]
        return {p: _PROXY_URL_PATTERN.format(pod_id=instance.id, port=p) for p in ports}


_MODAL_POD_ID = "modal-test-warm-endpoints"
_MODAL_URL = "https://x--kinoforge-run-build-27e651.modal.run"


def _entry_with_endpoints() -> dict[str, Any]:
    """Ledger entry shaped like the record()-fix persists for Modal.

    Unlike RunPod, Modal cannot rebuild its ``.modal.run`` URL from
    ``tags["ports"]`` (the URL carries a non-deterministic ``build-<hash>``
    suffix), so the endpoint MUST come from the persisted ``endpoints`` key
    on the entry. This entry carries it at the top level — exactly what the
    ``Ledger.record`` fix persists — and NO ``ports`` tag, so a green
    assert can only come from the entry replay, never a port-rebuild.
    """
    return {
        "id": _MODAL_POD_ID,
        "provider": "modal-test",
        "created_at": _NOW - 60.0,
        "cost_rate_usd_per_hr": 1.10,
        "endpoints": {"8000": _MODAL_URL},
        "tags": {
            "kinoforge_engine": "comfyui",
            "kinoforge_key": "ab12cd34ef56",
        },
        "last_heartbeat": _NOW - 5.0,
        "heartbeat_thread_tick": _NOW - 5.0,
    }


class _ModalShapeProvider:
    """Provider that reproduces Modal's non-rebuildable endpoints.

    ``get_instance`` returns a sparse Instance (empty endpoints, ``{"mode":
    "pod"}`` tags) like RunPod's, but crucially ``endpoints`` returns ``{}``
    unconditionally — Modal cannot deterministically reconstruct its URL. So
    the ONLY way ``_resolve_warm_instance`` can populate endpoints is by
    replaying the persisted ``entry["endpoints"]``.
    """

    def list_instances(self) -> list[Instance]:
        return [self.get_instance(_MODAL_POD_ID)]

    def get_instance(self, iid: str) -> Instance:
        return Instance(
            id=iid,
            provider="modal-test",
            status="ready",
            created_at=_NOW - 60.0,
            endpoints={},
            tags={"mode": "pod"},
        )

    def endpoints(self, instance: Instance) -> dict[str, str]:
        # Modal cannot rebuild its build-<hash> URL from ports.
        return {}


class _Compute:
    def __init__(self, provider: str) -> None:
        self.provider = provider


class _FakeCfg:
    def __init__(
        self,
        *,
        provider: str = "runpod-test",
        cap_hash: str = "ab12cd34ef56XX",
    ) -> None:
        self._provider = provider
        self._cap_hash = cap_hash
        self.compute = _Compute(provider)

    def capability_key(self) -> Any:
        cap_hash = self._cap_hash

        class _CapKey:
            def derive(self) -> str:
                return cap_hash

        return _CapKey()

    def lifecycle(self) -> Any:
        from kinoforge.core.interfaces import Lifecycle

        return Lifecycle(heartbeat_interval_s=30.0)


class _FakeCtx:
    def __init__(self, entry: dict[str, Any] | None, cfg: Any) -> None:
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.read = MagicMock(return_value=entry)
        self._ledger.entries = MagicMock(
            return_value=([entry] if entry is not None else [])
        )

    def ledger(self) -> MagicMock:
        return self._ledger


def _ctx(entry: dict[str, Any] | None, cfg: Any) -> SessionContext:
    return cast("SessionContext", _FakeCtx(entry, cfg))


@pytest.fixture
def patched_registry(monkeypatch: pytest.MonkeyPatch) -> _RunPodShapeProvider:
    """Patch `registry.get_provider` to return a RunPod-shape provider."""
    provider = _RunPodShapeProvider()

    def _factory(name: str) -> Any:
        def _ctor() -> _RunPodShapeProvider:
            return provider

        return _ctor

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _factory)
    return provider


@pytest.fixture
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kinoforge.cli._commands.time.time", lambda: _NOW)


def test_warm_attached_instance_carries_endpoints_built_from_ledger_tags(
    patched_registry: _RunPodShapeProvider,
    fixed_clock: None,
) -> None:
    """Returned Instance.endpoints must be populated, not empty."""
    cfg: Any = _FakeCfg(cap_hash="ab12cd34ef56XX")
    entry = _entry_with_ports()
    ctx = _ctx(entry, cfg)

    inst, rc = _resolve_warm_instance(ctx, cfg, _POD_ID, force_attach=False)

    assert rc is None, f"warm-attach unexpectedly refused: rc={rc!r}"
    assert inst is not None, "warm-attach returned None instance"

    assert inst.endpoints, (
        f"warm-attached Instance.endpoints empty — downstream "
        f"wait_for_ready will raise ProvisionFailed. Got: "
        f"{inst.endpoints!r}. Expected: dict with at least one "
        f"port entry derived from ledger tags['ports']."
    )
    expected_url = _PROXY_URL_PATTERN.format(pod_id=_POD_ID, port="8188")
    assert inst.endpoints.get("8188") == expected_url, (
        f"endpoints['8188'] mismatch: expected {expected_url!r}, "
        f"got {inst.endpoints.get('8188')!r}"
    )


@pytest.fixture
def patched_modal_registry(monkeypatch: pytest.MonkeyPatch) -> _ModalShapeProvider:
    """Patch `registry.get_provider` to return a Modal-shape provider."""
    provider = _ModalShapeProvider()

    def _factory(name: str) -> Any:
        def _ctor() -> _ModalShapeProvider:
            return provider

        return _ctor

    monkeypatch.setattr("kinoforge.core.registry.get_provider", _factory)
    return provider


def test_warm_attached_modal_replays_endpoints_from_ledger_entry(
    patched_modal_registry: _ModalShapeProvider,
    fixed_clock: None,
) -> None:
    """Non-rebuildable provider (Modal): endpoint must come from the entry.

    The provider's ``endpoints()`` returns ``{}`` (Modal cannot rebuild its
    ``build-<hash>`` URL), and the entry carries no ``ports`` tag — so the
    ONLY source for a populated ``endpoints`` field is the persisted
    ``entry["endpoints"]`` that the ``Ledger.record`` fix writes.

    Would-fail-bug: if ``_resolve_warm_instance`` stopped preferring
    ``entry["endpoints"]`` (or ``record()`` stopped persisting it), the
    returned Instance would carry an empty endpoints dict and downstream
    ``wait_for_ready`` would raise ProvisionFailed — exactly the live
    2026-07-11 Modal warm-reuse failure.
    """
    cfg: Any = _FakeCfg(provider="modal-test", cap_hash="ab12cd34ef56XX")
    entry = _entry_with_endpoints()
    ctx = _ctx(entry, cfg)

    inst, rc = _resolve_warm_instance(ctx, cfg, _MODAL_POD_ID, force_attach=False)

    assert rc is None, f"warm-attach unexpectedly refused: rc={rc!r}"
    assert inst is not None, "warm-attach returned None instance"

    assert inst.endpoints.get("8000") == _MODAL_URL, (
        f"Modal warm-attach must replay endpoint from the persisted ledger "
        f"entry (provider.endpoints() returns {{}} for Modal). Got "
        f"{inst.endpoints!r}; expected {{'8000': {_MODAL_URL!r}}}. An empty "
        f"dict here means the entry-replay path was lost → ProvisionFailed."
    )
