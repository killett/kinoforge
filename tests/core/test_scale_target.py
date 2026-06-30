"""Tests for ScaleTarget polymorphic parser."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class TestParseFactor:
    """Behaviour: parse `Nx` token shapes."""

    def test_parse_2x(self) -> None:
        assert ScaleTarget.parse("2x") == ScaleTarget(kind="factor", value=2.0)

    def test_parse_4x(self) -> None:
        assert ScaleTarget.parse("4x") == ScaleTarget(kind="factor", value=4.0)

    def test_parse_fractional(self) -> None:
        assert ScaleTarget.parse("1.5x") == ScaleTarget(kind="factor", value=1.5)


class TestParseHeight:
    """Behaviour: parse `Np` token shapes (parses now; consumer raises later)."""

    @pytest.mark.parametrize("raw,h", [("1080p", 1080), ("720p", 720), ("2160p", 2160)])
    def test_parse_height_tokens(self, raw: str, h: int) -> None:
        assert ScaleTarget.parse(raw) == ScaleTarget(kind="height", value=float(h))


class TestParseRejects:
    """Behaviour: malformed tokens raise ValueError."""

    @pytest.mark.parametrize(
        "raw", ["bogus", "2", "x", "px", "1080", "1080P", "2X", ""]
    )
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(ValueError, match="expected `Nx` or `Np` token"):
            ScaleTarget.parse(raw)

    @pytest.mark.parametrize("raw", ["0x", "-1x", "0p", "-1080p"])
    def test_rejects_non_positive(self, raw: str) -> None:
        with pytest.raises(ValueError):
            ScaleTarget.parse(raw)


class TestFrozenDataclass:
    def test_assignment_raises(self) -> None:
        t = ScaleTarget(kind="factor", value=2.0)
        with pytest.raises(FrozenInstanceError):
            t.kind = "height"  # type: ignore[misc]


class TestUnsupportedScaleError:
    def test_message_mentions_both(self) -> None:
        err = UnsupportedScaleError(
            scale=ScaleTarget(kind="factor", value=3.0), engine_name="seedvr2"
        )
        msg = str(err)
        assert "seedvr2" in msg
        assert "3" in msg


class TestNotYetImplementedError:
    def test_is_kinoforge_error(self) -> None:
        from kinoforge.core.errors import KinoforgeError

        assert issubclass(NotYetImplementedError, KinoforgeError)
