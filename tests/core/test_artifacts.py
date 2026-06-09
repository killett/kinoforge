"""Tests for core.artifacts.opaque_store_name."""

import hashlib

from kinoforge.core.artifacts import opaque_store_name


def test_basic_sha_and_extension() -> None:
    """Name is sha256(bytes)[:16] + ext."""
    name = opaque_store_name(b"hello", ".mp4")
    expected_prefix = hashlib.sha256(b"hello").hexdigest()[:16]
    assert name == f"{expected_prefix}.mp4"


def test_invalid_extension_dropped() -> None:
    r"""Extensions not matching \.[A-Za-z0-9]{1,5} become empty suffix.

    Would-fail-bug: passing a prompt-derived 'extension' through verbatim
    would leak material into the store-side filename.
    """
    assert "." not in opaque_store_name(b"x", "junk-not-an-ext")
    assert "." not in opaque_store_name(b"x", ".foo bar")  # space in ext
    assert "." not in opaque_store_name(b"x", ".verylongextension")  # > 5 chars
    assert "." not in opaque_store_name(b"x", "")


def test_deterministic_same_bytes_same_ext() -> None:
    """Same input → same output across calls."""
    assert opaque_store_name(b"abc", ".mp4") == opaque_store_name(b"abc", ".mp4")


def test_different_bytes_different_names() -> None:
    """Hash collisions astronomically unlikely; trivial bytes differ."""
    assert opaque_store_name(b"a", ".mp4") != opaque_store_name(b"b", ".mp4")
