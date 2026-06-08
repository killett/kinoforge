"""Tests for the format_filename helper (Layer 4 schema)."""

from kinoforge.outputs.base import format_filename


def test_format_filename_happy_path() -> None:
    assert (
        format_filename(
            ts="20260607-143015",
            provider="replicate",
            model="wan-t2v-1-3b",
            slug="photorealistic-c",
            extension=".mp4",
        )
        == "20260607-143015_replicate_wan-t2v-1-3b_photorealistic-c.mp4"
    )


def test_format_filename_empty_slug() -> None:
    assert (
        format_filename(
            ts="20260607-143015",
            provider="luma",
            model="ray-2",
            slug="",
            extension=".mp4",
        )
        == "20260607-143015_luma_ray-2_.mp4"
    )


def test_format_filename_preserves_extension_verbatim() -> None:
    assert format_filename(
        ts="20260607-143015",
        provider="runway",
        model="gen3a-turbo",
        slug="x",
        extension=".png",
    ).endswith(".png")


def test_format_filename_no_sanitisation_in_helper() -> None:
    # Helper does NOT slugify; LocalOutputSink owns sanitisation.
    out = format_filename(
        ts="20260607-143015",
        provider="WEIRD/PROV",
        model="m/v:1",
        slug="x",
        extension=".mp4",
    )
    assert "WEIRD/PROV" in out
    assert "m/v:1" in out


def test_format_filename_unknown_marker_round_trip() -> None:
    # The literal "unknown" sentinel is just a string at this layer.
    assert (
        format_filename(
            ts="20260607-143015",
            provider="unknown",
            model="unknown",
            slug="cat",
            extension=".mp4",
        )
        == "20260607-143015_unknown_unknown_cat.mp4"
    )


def test_format_filename_underscore_count_stable() -> None:
    # Schema is exactly 3 underscores between fixed fields + extension.
    out = format_filename(
        ts="A",
        provider="B",
        model="C",
        slug="D",
        extension=".e",
    )
    # A_B_C_D.e — three underscores
    assert out.count("_") == 3
