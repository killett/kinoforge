"""Tests for the HTTP model source."""

import kinoforge.sources.http  # noqa: F401  — registers the source on import
from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.sources.http import HTTPSource


def test_handles_http_and_https_only():
    src = HTTPSource()
    assert src.handles("https://x/y.safetensors") is True
    assert src.handles("http://x/y.safetensors") is True
    # Bug this catches: matching by raw prefix without scheme separator,
    # which would route "hf:..." through HTTP.
    assert src.handles("hf:org/m") is False
    assert src.handles("civitai:1@2") is False


def test_resolve_returns_single_artifact_with_url_and_filename():
    src = HTTPSource()
    creds = EnvCredentialProvider()
    artifacts = src.resolve("https://example.com/path/to/model.safetensors", creds)
    assert len(artifacts) == 1
    a = artifacts[0]
    assert a.url == "https://example.com/path/to/model.safetensors"
    assert a.filename == "model.safetensors"


def test_filename_strips_query_string():
    src = HTTPSource()
    artifacts = src.resolve("https://x/a.bin?token=foo&v=2", EnvCredentialProvider())
    # Bug this catches: building filename from the entire URL, which would write
    # files like "a.bin?token=foo&v=2" on disk.
    assert artifacts[0].filename == "a.bin"


def test_self_registers_on_import():
    # The import at the top of this file should have registered HTTPSource.
    # Routing for an https ref must succeed (registry returns the registered instance).
    src = registry.source_for_ref("https://x/y.bin")
    assert isinstance(src, HTTPSource)


def test_scheme_attribute_is_https():
    assert HTTPSource().scheme == "https"
