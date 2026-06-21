"""try_warm_attach_with_swap — end-to-end matcher + backend + ledger contract.

Mocks the ledger, backend factory, and PodLockRegistry to verify the seven
acceptance criteria from spec §11.2 / plan Task 15 without needing the real
deploy_session flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from kinoforge.core.errors import (
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)
from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.warm_reuse.integration import try_warm_attach_with_swap


@dataclass
class _StubCfg:
    cap_key: CapabilityKey

    def capability_key(self) -> CapabilityKey:
        return self.cap_key


class _FakeLedger:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.touches: list[tuple[str, dict[str, Any]]] = []

    def find_pods_by_warm_attach_key(self, wak_hex: str) -> list[dict[str, Any]]:
        return [e for e in self._entries if e.get("warm_attach_key") == wak_hex]

    def touch(self, pod_id: str, **kwargs: Any) -> bool:
        self.touches.append((pod_id, dict(kwargs)))
        return True


@dataclass
class _FakeLockRegistry:
    locked: set[str] = field(default_factory=set)
    released: list[str] = field(default_factory=list)

    def acquire(self, pod_id: str, *, blocking: bool = False) -> bool:
        if pod_id in self.locked:
            return False
        self.locked.add(pod_id)
        return True

    def release(self, pod_id: str) -> None:
        self.locked.discard(pod_id)
        self.released.append(pod_id)

    def __contains__(self, pod_id: str) -> bool:
        return pod_id in self.locked


class _FakeBackend:
    def __init__(self, response: Any = None, raises: Exception | None = None) -> None:
        self.response = response or {
            "inventory": [
                {
                    "ref": "B",
                    "filename": "b.s",
                    "size_bytes": 200,
                    "downloaded_at_local": "x",
                    "last_used_at_local": "x",
                    "adapter_name": "lora_0",
                }
            ],
            "free_bytes": 9000,
            "swap_rejected": None,
        }
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def set_lora_stack(
        self,
        *,
        pod_id: str,
        target_refs: list[str],
        download_specs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "pod_id": pod_id,
                "target_refs": list(target_refs),
                "download_specs": dict(download_specs),
            }
        )
        if self.raises is not None:
            raise self.raises
        return self.response


def _entry(
    pod_id: str, *, loras: tuple[str, ...] = (), free_bytes: int = 10_000_000
) -> dict[str, Any]:
    key = CapabilityKey(
        base_model="hf:m", loras=loras, engine="diffusers", precision="fp16"
    )
    return {
        "id": pod_id,
        "capability_key_hex": key.derive(),
        "warm_attach_key": key.warm_attach_key().derive(),
        "lora_inventory": [],
        "loras_dir_free_bytes": free_bytes,
        "loras_dir_free_bytes_observed_at_local": datetime.now().isoformat(),
        "status": "ready",
    }


def _cfg(loras: tuple[str, ...] = ()) -> _StubCfg:
    return _StubCfg(
        cap_key=CapabilityKey(
            base_model="hf:m", loras=loras, engine="diffusers", precision="fp16"
        )
    )


def test_returns_none_when_no_candidate() -> None:
    """No matching pods → returns None so caller cold-boots.

    Bug: wrapper raises StopIteration / IndexError on the empty match,
    crashing the cold-boot fallback before it can fire.
    """
    backends: list[_FakeBackend] = []

    def _build(_pid: str) -> _FakeBackend:
        backend = _FakeBackend()
        backends.append(backend)
        return backend

    result = try_warm_attach_with_swap(
        _cfg(),
        _FakeLedger([]),
        _build,
        pod_lock_registry=_FakeLockRegistry(),
    )
    assert result is None
    assert backends == []


def test_swap_called_with_matcher_plan_and_ledger_touched() -> None:
    """Happy path: set_lora_stack invoked with plan; ledger touched once.

    Bug: wrapper passes the whole download_specs map (including refs that
    are already on the pod) so the pod redownloads everything.
    """
    backend = _FakeBackend()
    ledger = _FakeLedger([_entry("pod-a")])
    specs = {"B": {"url": "x", "headers": {}, "filename": "b.s", "size_hint": 200}}
    result = try_warm_attach_with_swap(
        _cfg(("B",)),
        ledger,
        lambda _pid: backend,
        pod_lock_registry=_FakeLockRegistry(),
        download_specs=specs,
    )
    assert result is not None
    assert backend.calls[0]["target_refs"] == ["B"]
    assert backend.calls[0]["download_specs"] == {"B": specs["B"]}
    touch_kwargs = ledger.touches[0][1]
    assert touch_kwargs["lora_inventory"][0]["ref"] == "B"
    assert touch_kwargs["loras_dir_free_bytes"] == 9000
    assert "loras_dir_free_bytes_observed_at_local" in touch_kwargs


def test_no_swap_when_exact_byte_match() -> None:
    """Exact-byte fast path → no set_lora_stack call + no ledger touch.

    Bug: wrapper unconditionally posts an empty swap, causing the pod to
    unload + reload its existing adapters for no reason.
    """
    backend = _FakeBackend()
    ledger = _FakeLedger([_entry("pod-a", loras=())])
    result = try_warm_attach_with_swap(
        _cfg(()),
        ledger,
        lambda _pid: backend,
        pod_lock_registry=_FakeLockRegistry(),
    )
    assert result is not None
    assert backend.calls == []
    assert ledger.touches == []


def test_degraded_error_marks_ledger_and_releases_lock() -> None:
    """LoraSwapDegradedPodError → ledger.touch(status=degraded) + lock released.

    Bug: wrapper releases the lock but forgets the status update, leaving
    the matcher routing future requests to a broken pod.
    """
    err = LoraSwapDegradedPodError(
        pod_id="pod-a",
        evict_completed=["X"],
        download_failed="B",
        underlying="504",
    )
    backend = _FakeBackend(raises=err)
    ledger = _FakeLedger([_entry("pod-a")])
    registry = _FakeLockRegistry()
    with pytest.raises(LoraSwapDegradedPodError):
        try_warm_attach_with_swap(
            _cfg(("B",)),
            ledger,
            lambda _pid: backend,
            pod_lock_registry=registry,
            download_specs={"B": {"size_hint": 1}},
        )
    assert ledger.touches[-1] == ("pod-a", {"status": "degraded"})
    assert registry.released == ["pod-a"]


@pytest.mark.parametrize(
    "err_cls",
    [LoraSwapPodUnreachableError, LoraSwapDiskFullError],
)
def test_unreachable_and_disk_full_mark_degraded(err_cls: type) -> None:
    """Pod-unreachable and disk-full both flip the pod to degraded.

    Bug: wrapper only marks degraded for LoraSwapDegradedPodError, so a
    disk-full or unreachable pod stays in the matcher's candidate set
    forever.
    """
    kwargs: dict[str, Any] = {"pod_id": "pod-a"}
    if err_cls is LoraSwapPodUnreachableError:
        kwargs["underlying"] = "ConnectionResetError"
    if err_cls is LoraSwapDiskFullError:
        kwargs["evict_completed"] = []
        kwargs["download_failed"] = "B"
    err = err_cls(**kwargs)
    backend = _FakeBackend(raises=err)
    ledger = _FakeLedger([_entry("pod-a")])
    registry = _FakeLockRegistry()
    with pytest.raises(err_cls):
        try_warm_attach_with_swap(
            _cfg(("B",)),
            ledger,
            lambda _pid: backend,
            pod_lock_registry=registry,
            download_specs={"B": {"size_hint": 1}},
        )
    assert any(t[1].get("status") == "degraded" for t in ledger.touches)
    assert registry.released == ["pod-a"]


@pytest.mark.parametrize(
    "err_cls,err_kwargs",
    [
        (LoraSwapDownloadError, {"ref": "B", "underlying": "504"}),
        (LoraSwapVramOomError, {"dropped_refs": ["big"]}),
    ],
)
def test_download_and_oom_release_without_degrading(
    err_cls: type, err_kwargs: dict[str, Any]
) -> None:
    """Clean download fail + VRAM OOM rollback → lock released, pod healthy.

    Bug: wrapper marks every error as degraded, retiring healthy pods
    after one transient CivitAI 504.
    """
    err = err_cls(pod_id="pod-a", **err_kwargs)
    backend = _FakeBackend(raises=err)
    ledger = _FakeLedger([_entry("pod-a")])
    registry = _FakeLockRegistry()
    with pytest.raises(err_cls):
        try_warm_attach_with_swap(
            _cfg(("B",)),
            ledger,
            lambda _pid: backend,
            pod_lock_registry=registry,
            download_specs={"B": {"size_hint": 1}},
        )
    assert not any(t[1].get("status") == "degraded" for t in ledger.touches)
    assert registry.released == ["pod-a"]


def test_unexpected_exception_releases_lock() -> None:
    """Any other Exception still releases the lock before propagating.

    Bug: wrapper's narrow except blocks let unexpected errors propagate
    without releasing, deadlocking the pod for the next job.
    """
    backend = _FakeBackend(raises=RuntimeError("boom"))
    ledger = _FakeLedger([_entry("pod-a")])
    registry = _FakeLockRegistry()
    with pytest.raises(RuntimeError, match="boom"):
        try_warm_attach_with_swap(
            _cfg(("B",)),
            ledger,
            lambda _pid: backend,
            pod_lock_registry=registry,
            download_specs={"B": {"size_hint": 1}},
        )
    assert registry.released == ["pod-a"]
