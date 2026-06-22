"""``build_set_stack_request``: bridge ``LoraEntry`` (core) → ``LoraTarget``
(server)."""

from __future__ import annotations

from kinoforge._adapters import build_set_stack_request
from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    ArtifactDownloadSpec,
    SetStackRequest,
)


def _ds() -> ArtifactDownloadSpec:
    return ArtifactDownloadSpec(
        url="https://example.com/x.safetensors",
        filename="x.safetensors",
        size_hint=1,
    )


def test_pairs_strengths_in_order() -> None:
    """Bug: a future edit zips the list out of order → strengths land
    on the wrong refs and the wrong adapter weights apply."""
    stack = [
        LoraEntry(ref="a", strength=0.5),
        LoraEntry(ref="b", strength=1.2),
    ]
    req = build_set_stack_request(stack, download_specs={})
    assert isinstance(req, SetStackRequest)
    assert [t.ref for t in req.target] == ["a", "b"]
    assert [t.strength for t in req.target] == [0.5, 1.2]


def test_empty_stack_returns_empty_target() -> None:
    """Bug: a future edit treats empty stack as a contract violation;
    empty MUST be valid (unloads every active adapter on the pod)."""
    req = build_set_stack_request([], download_specs={})
    assert req.target == []


def test_download_specs_pass_through_unchanged() -> None:
    stack = [LoraEntry(ref="b", strength=1.0)]
    ds = _ds()
    req = build_set_stack_request(stack, download_specs={"b": ds})
    assert req.download_specs == {"b": ds}
