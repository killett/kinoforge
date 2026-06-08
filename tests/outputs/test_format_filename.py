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
            provider="replicate",
            model="seedance-1-lite",
            slug="",
            extension=".mp4",
        )
        == "20260607-143015_replicate_seedance-1-lite_.mp4"
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


def test_format_filename_with_kind_inserts_kind_slot() -> None:
    """`kind` slots between ts and provider with an underscore separator."""
    assert (
        format_filename(
            ts="20260607-200115",
            provider="replicate",
            model="flux-schnell",
            slug="photorealistic-c",
            extension=".png",
            kind="keyframe-init",
        )
        == "20260607-200115_keyframe-init_replicate_flux-schnell_photorealistic-c.png"
    )


def test_format_filename_empty_kind_omits_slot() -> None:
    """Default empty `kind` keeps the legacy video schema verbatim."""
    out = format_filename(
        ts="A",
        provider="B",
        model="C",
        slug="D",
        extension=".e",
        kind="",
    )
    assert out == "A_B_C_D.e"


def test_format_filename_flf2v_keyframe_first_and_last_distinct() -> None:
    """flf2v generates frame0 + frame1; the kind slot keeps them apart."""
    a = format_filename(
        ts="t",
        provider="p",
        model="m",
        slug="s",
        extension=".png",
        kind="keyframe-first",
    )
    b = format_filename(
        ts="t",
        provider="p",
        model="m",
        slug="s",
        extension=".png",
        kind="keyframe-last",
    )
    assert a != b
    assert "keyframe-first" in a
    assert "keyframe-last" in b
