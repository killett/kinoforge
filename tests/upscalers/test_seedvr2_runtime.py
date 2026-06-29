"""Tests for SeedVR2Runtime wrapper.

Upstream module is patched out — these tests run without the real seedvr
package installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def _patched_seedvr() -> Any:
    """Patch the lazy import so SeedVR2Runtime can be constructed offline."""
    fake_inferencer = MagicMock()
    fake_inferencer.from_pretrained = MagicMock(return_value=fake_inferencer)
    fake_inferencer.upscale = MagicMock(return_value=Path("/tmp/out.mp4"))
    fake_module = MagicMock()
    fake_module.SeedVR2Inferencer = fake_inferencer
    with patch.dict(
        sys.modules, {"seedvr": MagicMock(), "seedvr.inference": fake_module}
    ):
        yield fake_inferencer


class TestModuleImportIsLazy:
    def test_module_import_does_not_require_upstream(self) -> None:
        # Importing the module while upstream is absent must not fail.
        # If this raises, the module is eagerly importing upstream and
        # the lazy-import contract is broken.
        import kinoforge.upscalers.seedvr2._runtime  # noqa: F401


class TestConstruction:
    def test_constructs_with_patched_upstream(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        assert rt is not None


class TestUpscale:
    def test_factor_branch_returns_path(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        out = rt.upscale(
            Path("/tmp/in.mp4"),
            ScaleTarget(kind="factor", value=2.0),
            {},
        )
        assert out == Path("/tmp/out.mp4")

    def test_factor_passes_to_inferencer(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        rt.upscale(
            Path("/tmp/in.mp4"),
            ScaleTarget(kind="factor", value=4.0),
            {"tile_size": 256, "steps": None},
        )
        # None-valued params filtered out; factor passed through.
        _, kwargs = _patched_seedvr.upscale.call_args
        assert kwargs == {"factor": 4.0, "tile_size": 256}

    def test_height_branch_refuses(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        with pytest.raises(NotYetImplementedError, match="1080p"):
            rt.upscale(
                Path("/tmp/in.mp4"),
                ScaleTarget(kind="height", value=1080),
                {},
            )


class TestEvictionHook:
    def test_to_cpu(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        rt.to("cpu")
        _patched_seedvr.to.assert_called_with("cpu")

    def test_to_cuda(self, _patched_seedvr: Any) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        rt.to("cuda")
        _patched_seedvr.to.assert_called_with("cuda")
