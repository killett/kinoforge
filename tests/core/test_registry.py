import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.interfaces import ModelSource


class _Src(ModelSource):
    """Minimal ModelSource subclass for routing tests."""

    def __init__(self, scheme: str, prefix: str) -> None:
        self.scheme = scheme
        self._prefix = prefix

    def handles(self, ref: str) -> bool:
        return ref.startswith(self._prefix)

    def resolve(self, ref, creds):  # not exercised here
        raise NotImplementedError


def test_provider_factory_round_trips():
    registry.register_provider("dummy", lambda: "P")  # type: ignore[arg-type, return-value]
    # Bug this catches: get_provider returning the constructed value instead of the factory.
    assert registry.get_provider("dummy")() == "P"  # type: ignore[comparison-overlap]


def test_engine_factory_round_trips():
    registry.register_engine("dummy_e", lambda: "E")  # type: ignore[arg-type, return-value]
    assert registry.get_engine("dummy_e")() == "E"  # type: ignore[comparison-overlap]


def test_unknown_provider_raises_named():
    with pytest.raises(UnknownAdapter, match="nope_prov"):
        registry.get_provider("nope_prov")


def test_unknown_engine_raises_named():
    with pytest.raises(UnknownAdapter, match="nope_eng"):
        registry.get_engine("nope_eng")


def test_source_dispatches_by_handles_not_scheme_equality():
    # A source registered for scheme "fake" should match by handles(ref), not by
    # naive prefix-on-scheme. So a scheme-of-"fake" source claiming the "fake:" prefix
    # wins over an unrelated registered source.
    other = _Src(scheme="other", prefix="other:")
    fake = _Src(scheme="fake", prefix="fake:")
    registry.register_source(other)
    registry.register_source(fake)
    # Bug this catches: dispatch by scheme equality (would miss handles()-based routing).
    assert registry.source_for_ref("fake:123") is fake


def test_unknown_ref_raises_named():
    with pytest.raises(UnknownAdapter, match="nosuchscheme"):
        registry.source_for_ref("nosuchscheme:1")


def test_provider_re_registration_overwrites():
    registry.register_provider("dup", lambda: "first")  # type: ignore[arg-type, return-value]
    registry.register_provider("dup", lambda: "second")  # type: ignore[arg-type, return-value]
    # Bug this catches: append-only registry that returns the first registration.
    assert registry.get_provider("dup")() == "second"  # type: ignore[comparison-overlap]


def test_source_re_registration_replaces_by_scheme():
    # Two instances with the SAME scheme — the second should replace the first.
    s1 = _Src(scheme="dup", prefix="dup:old:")
    s2 = _Src(scheme="dup", prefix="dup:new:")
    registry.register_source(s1)
    registry.register_source(s2)
    # Old prefix no longer routed (only new instance remains for this scheme).
    with pytest.raises(UnknownAdapter):
        registry.source_for_ref("dup:old:x")
    assert registry.source_for_ref("dup:new:x") is s2


# --- artifact-store registry -------------------------------------------------


def test_store_factory_round_trips():
    """register_store + get_store return the registered factory callable.

    Bug this catches: get_store returning the constructed value instead of the factory.
    """
    registry.register_store("dummy_store", lambda: "S")  # type: ignore[arg-type, return-value]
    assert registry.get_store("dummy_store")() == "S"  # type: ignore[comparison-overlap]


def test_unknown_store_raises_unknown_adapter():
    """get_store raises UnknownAdapter for an unregistered name.

    Bug this catches: raising KeyError instead of the typed UnknownAdapter.
    """
    with pytest.raises(UnknownAdapter, match="nope_store"):
        registry.get_store("nope_store")
