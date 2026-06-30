"""Tests for the still-functional surfaces of SeedVR2Engine in extras-stub mode.

Most of the original test bodies migrated to ``test_seedvr2_extras_stub.py``
when the four heavyweight ABC methods became raise-only stubs. What stays
here is the registration check and the pure-functional ``model_identity``
contract — both unaffected by the extras gate.
"""

from __future__ import annotations

from typing import Any

from kinoforge.upscalers.seedvr2 import SeedVR2Engine


class TestRegistrySelfRegister:
    def test_registered_at_import(self) -> None:
        from kinoforge.core import registry

        eng = registry.get_upscaler("seedvr2")()
        assert eng.name == "seedvr2"


class TestModelIdentity:
    def test_default_3b_fp8(self) -> None:
        cfg: dict[str, Any] = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            }
        }
        assert SeedVR2Engine().model_identity(cfg) == "seedvr2-3b-fp8"

    def test_empty_cfg_does_not_raise(self) -> None:
        assert SeedVR2Engine().model_identity({}) == ""

    def test_missing_seedvr2_block_does_not_raise(self) -> None:
        assert SeedVR2Engine().model_identity({"upscale": {}}) == ""
