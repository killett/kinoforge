"""Tests for B2 balance disk cache (``cached_balance_read``).

Parametrized across :class:`LocalArtifactStore` + ``FakeS3Client``-backed
:class:`S3ArtifactStore` so the same cache code is exercised through both
implementations of the :class:`ArtifactStore` ABC.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import cached_balance_read
from kinoforge.core.balance_endpoints import ProviderBalance, TransportError
from kinoforge.stores.base import ArtifactStore


@pytest.fixture(params=["local", "s3-fake"])
def store_fixture(request: pytest.FixtureRequest, tmp_path: Path) -> ArtifactStore:
    """Parametrize cache tests across LocalArtifactStore + fake-S3-backed S3."""
    if request.param == "local":
        from kinoforge.stores.local import LocalArtifactStore

        return LocalArtifactStore(tmp_path / "store")
    from kinoforge.stores.s3 import S3ArtifactStore
    from tests.stores.conftest import FakeS3Client

    return S3ArtifactStore(bucket="kinoforge-test", client=FakeS3Client())


def _now_minus(seconds: float) -> datetime:
    return datetime.now() - timedelta(seconds=seconds)


def test_cache_miss_writes_fresh(store_fixture: ArtifactStore) -> None:
    pb = ProviderBalance(
        usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    assert bal is pb
    assert err is None
    assert endpoint.read.call_count == 1


def test_cache_hit_within_ttl_skips_endpoint(store_fixture: ArtifactStore) -> None:
    pb = ProviderBalance(
        usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    assert bal is not None
    assert bal.usd == 42.18
    assert err is None
    assert endpoint.read.call_count == 1  # NOT incremented


def test_cache_stale_beyond_ttl_refetches(store_fixture: ArtifactStore) -> None:
    pb_old = ProviderBalance(
        usd=10.0, as_of=_now_minus(60), source="runpod-graphql-clientBalance"
    )
    pb_new = ProviderBalance(
        usd=20.0, as_of=datetime.now(), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.side_effect = [pb_old, pb_new]

    base_time = datetime.now()
    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=base_time,
    )
    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=base_time + timedelta(seconds=30),
    )
    assert bal is not None
    assert bal.usd == 20.0
    assert endpoint.read.call_count == 2


def test_stale_fallback_on_transport_error(store_fixture: ArtifactStore) -> None:
    """BUG CATCH: fresh fetch fails -> cached value MUST be returned
    so the dashboard keeps showing the last-known value."""
    pb = ProviderBalance(
        usd=42.18, as_of=_now_minus(60), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.side_effect = [pb, TransportError("simulated")]

    base = datetime.now()
    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=base,
    )
    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=base + timedelta(seconds=30),
    )
    assert bal is not None
    assert bal.usd == 42.18
    assert err is not None
    assert "transport (using cache)" in err


def test_no_cache_skips_read_and_write(store_fixture: ArtifactStore) -> None:
    pb = ProviderBalance(
        usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=True,
        now=datetime.now(),
    )
    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=True,
        now=datetime.now(),
    )
    assert endpoint.read.call_count == 2


def test_cache_write_failure_does_not_raise(
    store_fixture: ArtifactStore, caplog: pytest.LogCaptureFixture
) -> None:
    """If put_json fails (disk full, S3 5xx), still return the fresh value."""
    pb = ProviderBalance(
        usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance"
    )
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    broken_store = MagicMock(wraps=store_fixture)
    broken_store.get_bytes.side_effect = FileNotFoundError()
    broken_store.get_json.side_effect = FileNotFoundError()
    broken_store.put_json.side_effect = OSError("disk full")

    with caplog.at_level("WARNING"):
        bal, err = cached_balance_read(
            store=broken_store,
            provider="runpod",
            endpoint=endpoint,
            cache_ttl_s=15.0,
            no_cache=False,
            now=datetime.now(),
        )
    assert bal is pb
    assert any("cache write" in rec.message.lower() for rec in caplog.records)


def test_no_cache_with_transport_error_returns_error(
    store_fixture: ArtifactStore,
) -> None:
    """--no-cache + TransportError → return (None, transport error)."""
    endpoint = MagicMock()
    endpoint.read.side_effect = TransportError("net down")

    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=True,
        now=datetime.now(),
    )
    assert bal is None
    assert err is not None
    assert "transport" in err
