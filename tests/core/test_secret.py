"""Tests for core.secret.Secret — the lightweight newtype used at the
vault → orchestrator → engine boundary.

Secret is NOT in the SPEC ABCs (per architecture choice C). It's a marker
type carried only inside the narrow boundary code so unwrap sites are
self-documenting. The redaction registry + sink-canonical pattern do the
actual on-disk enforcement.
"""

import json

import pytest

from kinoforge.core.secret import Secret


def test_reveal_returns_underlying_value() -> None:
    """Secret.reveal() returns the wrapped string verbatim."""
    assert Secret("hello world").reveal() == "hello world"


def test_str_returns_placeholder_not_value() -> None:
    """str(Secret) returns '<Secret>' so accidental string interpolation
    does not leak the value. Would-fail-bug: a Secret.__str__ returning
    self._value would leak via every f-string."""
    s = Secret("super secret prompt")
    assert str(s) == "<Secret>"
    assert "super" not in str(s)


def test_repr_returns_placeholder() -> None:
    """repr(Secret) returns '<Secret>' so traceback locals don't leak."""
    assert repr(Secret("x")) == "<Secret>"


def test_fstring_interpolation_uses_str_not_value() -> None:
    """f-string default ({secret}) calls __str__, returning placeholder.
    Would-fail-bug: someone overriding __format__ to expose the value."""
    s = Secret("prompt body here")
    assert f"got: {s}" == "got: <Secret>"
    assert "prompt body here" not in f"got: {s}"


def test_equality_compares_underlying() -> None:
    """Secret('a') == Secret('a'); Secret('a') != Secret('b')."""
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")


def test_equality_never_matches_bare_str() -> None:
    """Secret('a') == 'a' is False — accidental cross-type comparison
    is a likely caller bug, not a feature. Forces explicit .reveal()."""
    assert Secret("a") != "a"
    assert not (Secret("a") == "a")


def test_hash_stable_for_dict_key() -> None:
    """hash(Secret) stable across calls so Secret can be a dict key."""
    s = Secret("a")
    assert hash(s) == hash(s)
    d: dict[Secret, int] = {s: 1, Secret("b"): 2}
    assert d[Secret("a")] == 1


def test_json_dumps_raises_typeerror() -> None:
    """json.dumps(Secret(...)) raises TypeError. Would-fail-bug: serializing
    a Secret to disk silently. Catches the most likely persistence leak."""
    with pytest.raises(TypeError):
        json.dumps(Secret("x"))
    with pytest.raises(TypeError):
        json.dumps({"prompt": Secret("x")})
