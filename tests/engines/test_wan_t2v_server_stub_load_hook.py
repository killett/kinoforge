"""KINOFORGE_DIFFUSERS_LOAD_STUB env hook for wan_t2v_server."""

from __future__ import annotations

import sys
from typing import Any

import pytest


def _stub_factory() -> str:
    """Importable by dotted path in the env-var test below."""
    return "stubbed"


def test_stub_env_invokes_dotted_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: hook imports module but forgets to call the callable."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    monkeypatch.setenv(
        "KINOFORGE_DIFFUSERS_LOAD_STUB",
        "tests.engines.test_wan_t2v_server_stub_load_hook._stub_factory",
    )
    pipe = s._diffusers_load()
    assert pipe == "stubbed"


def test_absent_env_falls_through_to_real_diffusers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: stub-hook branch is taken even when env var is unset →
    production cold-boot never reaches real diffusers."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    monkeypatch.delenv("KINOFORGE_DIFFUSERS_LOAD_STUB", raising=False)
    sentinel_called: list[int] = []

    def _fake_wan_load(*_a: Any, **_k: Any) -> str:
        sentinel_called.append(1)
        return "real"

    fake_torch = type("M", (), {"bfloat16": "bf16"})()
    fake_diffusers = type(
        "M",
        (),
        {
            "WanPipeline": type(
                "W",
                (),
                {"from_pretrained": staticmethod(_fake_wan_load)},
            ),
        },
    )()
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    pipe = s._diffusers_load()
    assert pipe == "real"
    assert sentinel_called == [1]


def test_invalid_dotted_path_raises_importerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: hook returns None silently → server crashes later with
    obscure AttributeError instead of a clear ImportError at boot."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    monkeypatch.setenv(
        "KINOFORGE_DIFFUSERS_LOAD_STUB",
        "nonexistent.pkg.nope",
    )
    with pytest.raises(ImportError, match="nonexistent.pkg.nope"):
        s._diffusers_load()
