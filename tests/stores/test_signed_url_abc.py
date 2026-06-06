"""ArtifactStore.signed_url ABC contract tests."""

from __future__ import annotations

import pytest

# Must be imported before kinoforge.stores.base to avoid a circular import
# in kinoforge.core.__init__ → registry → stores.base.
from kinoforge.stores.local import LocalArtifactStore  # noqa: F401


def test_signed_url_is_abstract():
    from kinoforge.stores.base import ArtifactStore

    assert "signed_url" in ArtifactStore.__abstractmethods__


def test_local_signed_url_raises_not_implemented(tmp_path):
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(
        NotImplementedError, match="LocalArtifactStore does not support signed URLs"
    ):
        store.signed_url("run", "name", op="GET", ttl_s=60)


def test_local_signed_url_put_also_raises(tmp_path):
    store = LocalArtifactStore(tmp_path)
    with pytest.raises(NotImplementedError):
        store.signed_url("run", "name", op="PUT", ttl_s=60)


def test_signed_url_signature_keyword_only():
    """`op` and `ttl_s` must be keyword-only to prevent positional misuse."""
    import inspect

    from kinoforge.stores.base import ArtifactStore

    sig = inspect.signature(ArtifactStore.signed_url)
    params = sig.parameters
    assert params["op"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["ttl_s"].kind == inspect.Parameter.KEYWORD_ONLY
