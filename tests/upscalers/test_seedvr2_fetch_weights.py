"""Tests for the SeedVR2 _fetch_weights CLI module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run(argv: list[str]) -> int:
    """Invoke the module's main via argv injection."""
    from kinoforge.upscalers.seedvr2._fetch_weights import main

    return main(argv)


class TestArgParsing:
    def test_rejects_unknown_variant(self) -> None:
        with pytest.raises(SystemExit):
            _run(["--variant", "13B", "--precision", "fp8", "--dest", "/tmp/x"])

    def test_rejects_unknown_precision(self) -> None:
        with pytest.raises(SystemExit):
            _run(["--variant", "3B", "--precision", "int4", "--dest", "/tmp/x"])

    def test_requires_dest(self) -> None:
        with pytest.raises(SystemExit):
            _run(["--variant", "3B", "--precision", "fp8"])


class TestDispatch:
    def test_uses_registry_source_for_ref(self, tmp_path: Path) -> None:
        fake_source = MagicMock()
        fake_artifact = MagicMock()
        fake_artifact.uri = str(tmp_path / "seedvr2-3b")
        fake_source.resolve.return_value = [fake_artifact]

        with patch(
            "kinoforge.upscalers.seedvr2._fetch_weights.registry.source_for_ref",
            return_value=fake_source,
        ) as m:
            rc = _run(
                [
                    "--variant",
                    "3B",
                    "--precision",
                    "fp8",
                    "--dest",
                    str(tmp_path),
                ]
            )
            assert rc == 0
            m.assert_called_once()
            assert "ByteDance-Seed/SeedVR2-3B" in m.call_args.args[0]

    def test_7b_variant_uses_7b_ref(self, tmp_path: Path) -> None:
        fake_source = MagicMock()
        fake_source.resolve.return_value = [MagicMock(uri=str(tmp_path))]

        with patch(
            "kinoforge.upscalers.seedvr2._fetch_weights.registry.source_for_ref",
            return_value=fake_source,
        ) as m:
            _run(
                [
                    "--variant",
                    "7B",
                    "--precision",
                    "fp16",
                    "--dest",
                    str(tmp_path),
                ]
            )
            assert "SeedVR2-7B" in m.call_args.args[0]
