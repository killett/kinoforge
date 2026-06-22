"""``build_set_stack_request``: bridge ``LoraEntry`` (core) → ``LoraTarget``
(server).

Imports are intentionally INSIDE each test function. ``wan_t2v_server``
is reloaded by ``tests/engines/test_diffusers_wan_t2v_server.py`` (via
``importlib.reload``) to reset module-level state; if our
``SetStackRequest`` / ``ArtifactDownloadSpec`` symbols are bound at
module load time, a subsequent reload makes our bindings stale and the
new ``SetStackRequest`` from inside ``build_set_stack_request`` is a
different class object (``isinstance`` returns False, Pydantic refuses
the cross-class instances). Importing inside each test function pulls
the CURRENT class, post-reload.
"""

from __future__ import annotations

from kinoforge.core.lora import LoraEntry


def test_pairs_strengths_in_order() -> None:
    """Bug: a future edit zips the list out of order → strengths land
    on the wrong refs and the wrong adapter weights apply."""
    from kinoforge._adapters import build_set_stack_request
    from kinoforge.engines.diffusers.servers.wan_t2v_server import (
        SetStackRequest,
    )

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
    from kinoforge._adapters import build_set_stack_request

    req = build_set_stack_request([], download_specs={})
    assert req.target == []


def test_download_specs_pass_through_unchanged() -> None:
    from kinoforge._adapters import build_set_stack_request
    from kinoforge.engines.diffusers.servers.wan_t2v_server import (
        ArtifactDownloadSpec,
    )

    ds = ArtifactDownloadSpec(
        url="https://example.com/x.safetensors",
        filename="x.safetensors",
        size_hint=1,
    )
    stack = [LoraEntry(ref="b", strength=1.0)]
    req = build_set_stack_request(stack, download_specs={"b": ds})
    assert req.download_specs == {"b": ds}
