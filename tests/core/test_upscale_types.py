"""Tests for UpscaleJob and UpscaleResult shapes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kinoforge.core.interfaces import Artifact, UpscaleJob, UpscaleResult
from kinoforge.core.scale_target import ScaleTarget


def _art() -> Artifact:
    return Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=0)


class TestUpscaleJob:
    def test_defaults(self) -> None:
        j = UpscaleJob(source=_art(), scale=ScaleTarget(kind="factor", value=2.0))
        assert j.params == {}

    def test_frozen(self) -> None:
        j = UpscaleJob(source=_art(), scale=ScaleTarget(kind="factor", value=2.0))
        with pytest.raises(FrozenInstanceError):
            j.scale = ScaleTarget(kind="factor", value=4.0)  # type: ignore[misc]


class TestUpscaleResult:
    def test_defaults(self) -> None:
        r = UpscaleResult(
            artifact=_art(),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=12.3,
        )
        assert r.engine_meta == {}

    def test_frozen(self) -> None:
        r = UpscaleResult(
            artifact=_art(),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=12.3,
        )
        with pytest.raises(FrozenInstanceError):
            r.elapsed_s = 0.0  # type: ignore[misc]
