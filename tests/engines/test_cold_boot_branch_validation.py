"""Cold-boot ``KINOFORGE_INITIAL_LORA_STACK_JSON`` shape + branch validation.

P2 §6.2 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Cold-boot now accepts the canonical dict shape::

    [
        {
            "ref": "civitai:A@1",
            "download_spec": {...},
            "strength": 1.0,
            "branch": "high_noise",
        },
        ...
    ]

with the legacy ``[ref, {spec}]`` tuple shape auto-promoted to
``strength=1.0, branch="auto"``. Every entry's branch is validated
against the resolved pipeline arity BEFORE the cold-boot returns — a
mismatched branch (auto on MoE, explicit h/l on single-transformer)
raises and the server NEVER reports ready.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Snapshot/restore module globals each test."""
    original_arity = wan_t2v_server._pipe_arity
    original_inventory = wan_t2v_server._inventory.copy()
    yield
    wan_t2v_server._pipe_arity = original_arity
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory.update(original_inventory)


class _SinglePipe:
    """Wan-2.1-shape stub (single transformer)."""

    def __init__(self) -> None:
        self.transformer = self
        self.loaded: list[dict[str, Any]] = []
        self.activated: list[tuple[list[str], list[float]]] = []

    def load_lora_weights(
        self,
        path: str,
        adapter_name: str,
        load_into_transformer_2: bool = False,
    ) -> None:
        self.loaded.append(
            {
                "path": path,
                "adapter_name": adapter_name,
                "load_into_transformer_2": load_into_transformer_2,
            }
        )

    def set_adapters(
        self,
        names: list[str],
        adapter_weights: list[float] | None = None,
    ) -> None:
        self.activated.append((list(names), list(adapter_weights or [])))


class _MoEPipe:
    """Wan-2.2-shape stub (two transformers)."""

    def __init__(self) -> None:
        self.transformer = _Recorder()
        self.transformer_2 = _Recorder()
        self.loaded: list[dict[str, Any]] = []

    def load_lora_weights(
        self,
        path: str,
        adapter_name: str,
        load_into_transformer_2: bool = False,
    ) -> None:
        self.loaded.append(
            {
                "path": path,
                "adapter_name": adapter_name,
                "load_into_transformer_2": load_into_transformer_2,
            }
        )


class _Recorder:
    """Per-transformer set_adapters recorder."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[float]]] = []

    def set_adapters(
        self,
        names: list[str],
        adapter_weights: list[float] | None = None,
    ) -> None:
        self.calls.append((list(names), list(adapter_weights or [])))


def _stub_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub _download_one to return (filename, size) without touching disk."""

    def _fake(
        spec: wan_t2v_server.ArtifactDownloadSpec, _dest_dir: Any
    ) -> tuple[str, int]:
        return f"/tmp/{spec.filename}", 1024

    monkeypatch.setattr(wan_t2v_server, "_download_one", _fake)


def test_dict_form_env_parses_carrying_strength_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: new dict env shape ignored; cold-boot reads only legacy
    tuples and silently drops the per-entry strength + branch fields."""
    pipe = _MoEPipe()
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", lambda: pipe)
    _stub_download(monkeypatch)
    stack: list[Any] = [
        {
            "ref": "civitai:A@1",
            "download_spec": {"url": "https://x/a", "headers": {}, "filename": "a.s"},
            "strength": 0.6,
            "branch": "high_noise",
        },
        {
            "ref": "civitai:B@2",
            "download_spec": {"url": "https://x/b", "headers": {}, "filename": "b.s"},
            "strength": 0.4,
            "branch": "low_noise",
        },
    ]
    wan_t2v_server._load_pipeline(initial_lora_stack=stack)
    inventory = wan_t2v_server._inventory
    assert inventory[("civitai:A@1", "high_noise")]["last_strength"] == 0.6
    assert inventory[("civitai:A@1", "high_noise")]["branch"] == "high_noise"
    assert inventory[("civitai:B@2", "low_noise")]["last_strength"] == 0.4
    assert inventory[("civitai:B@2", "low_noise")]["branch"] == "low_noise"
    # Routing kwargs reach the loader correctly.
    assert pipe.loaded[0]["load_into_transformer_2"] is False
    assert pipe.loaded[1]["load_into_transformer_2"] is True


def test_legacy_tuple_form_auto_promoted_to_strength_1_branch_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: legacy tuple env crashes on the new dict-only path; rolling
    pods that pre-date the dict shape would refuse to boot."""
    pipe = _SinglePipe()
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", lambda: pipe)
    _stub_download(monkeypatch)
    spec = wan_t2v_server.ArtifactDownloadSpec(
        url="https://x/c", headers={}, filename="c.s"
    )
    stack: list[Any] = [("civitai:C@1", spec)]
    wan_t2v_server._load_pipeline(initial_lora_stack=stack)
    inventory = wan_t2v_server._inventory
    assert ("civitai:C@1", "auto") in inventory
    assert inventory[("civitai:C@1", "auto")]["last_strength"] == 1.0
    assert inventory[("civitai:C@1", "auto")]["branch"] == "auto"


def test_moe_pipe_with_auto_branch_rejected_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: cold-boot accepts ``branch="auto"`` on a Wan 2.2 MoE pipe and
    silently lands every LoRA into the high-noise stage. The server
    should refuse to come up — ready never sets — so the orchestrator
    treats the pod as failed and the operator sees a loud error."""
    pipe = _MoEPipe()
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", lambda: pipe)
    _stub_download(monkeypatch)
    stack: list[Any] = [
        {
            "ref": "civitai:D@1",
            "download_spec": {"url": "https://x/d", "headers": {}, "filename": "d.s"},
            "strength": 1.0,
            "branch": "auto",
        },
    ]
    with pytest.raises(wan_t2v_server.BranchAutoNotAllowedOnMoE):
        wan_t2v_server._load_pipeline(initial_lora_stack=stack)


def test_single_transformer_pipe_with_explicit_branch_rejected_at_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: cold-boot on Wan 2.1 accepts an explicit ``branch="high_noise"``
    entry and silently collapses to the single transformer. Wan 2.2 LoRA
    cfg loaded into a Wan 2.1 pod must NOT be served — the operator
    needs a loud failure rather than a degraded run."""
    pipe = _SinglePipe()
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", lambda: pipe)
    _stub_download(monkeypatch)
    stack: list[Any] = [
        {
            "ref": "civitai:E@1",
            "download_spec": {"url": "https://x/e", "headers": {}, "filename": "e.s"},
            "strength": 1.0,
            "branch": "high_noise",
        },
    ]
    with pytest.raises(wan_t2v_server.BranchUnsupportedOnSingleTransformer):
        wan_t2v_server._load_pipeline(initial_lora_stack=stack)
