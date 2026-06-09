"""Tests for core.redaction.RedactionRegistry — process-wide token registry.

The registry is the sole source of truth for what gets substituted on every
persistent surface. Vault loader is the only writer; sinks read via redact()
or redact_json().
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from kinoforge.core.redaction import RedactingLogFilter, RedactionRegistry


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Ensure the singleton starts empty for each test."""
    RedactionRegistry.instance().clear_session()
    yield
    RedactionRegistry.instance().clear_session()


def test_singleton_returns_same_instance() -> None:
    """instance() is idempotent. Would-fail-bug: a fresh registry each call
    would mean writers and readers see different state."""
    a = RedactionRegistry.instance()
    b = RedactionRegistry.instance()
    assert a is b


def test_add_then_redact_substitutes_placeholder() -> None:
    """A registered token is substituted with <kind:short_id> in subsequent
    redact() calls."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    out = r.redact("the prompt was supersecret today")
    assert "supersecret" not in out
    assert "<prompt:positive:" in out


def test_add_rejects_short_tokens() -> None:
    """Tokens shorter than 4 chars would false-positive on unrelated text.
    Would-fail-bug: registering 'a' would corrupt every log line containing
    the letter 'a'."""
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="at least 4"):
        r.add("abc", kind="prompt:positive")


def test_add_rejects_whitespace_only_tokens() -> None:
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="whitespace"):
        r.add("    \t\n", kind="prompt:positive")


def test_add_rejects_placeholder_pattern() -> None:
    """A token matching the placeholder syntax would create a chicken-and-egg
    cycle. Would-fail-bug: registering '<prompt:positive:abc123>' would
    cause redact() to re-substitute its own output."""
    r = RedactionRegistry.instance()
    with pytest.raises(ValueError, match="placeholder"):
        r.add("<prompt:positive:abc>", kind="prompt:positive")


def test_add_is_idempotent() -> None:
    """A second add() with the same token is a no-op; existing placeholder
    wins. Would-fail-bug: per-call placeholder regeneration would make
    redact() output non-deterministic across calls."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    first = r.redact("got supersecret")
    r.add("supersecret", kind="prompt:positive")  # idempotent
    second = r.redact("got supersecret")
    assert first == second


def test_redact_applies_tokens_longest_first() -> None:
    """When 'foo bar' and 'foo' are both registered, 'foo bar baz' should
    redact the longer match first to avoid partial overlap. Would-fail-bug:
    shortest-first would replace 'foo' inside 'foo bar' and leave 'bar' loose."""
    r = RedactionRegistry.instance()
    r.add("food", kind="prompt:positive")
    r.add("food bar", kind="prompt:negative")
    out = r.redact("got food bar baz")
    assert "<prompt:negative:" in out
    assert "food" not in out
    assert " bar" not in out


def test_redact_case_sensitive() -> None:
    """Prompts are case-sensitive content. 'FOO' is not 'foo'."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    assert "SUPERSECRET" in r.redact("got SUPERSECRET")
    assert "supersecret" not in r.redact("got supersecret")


def test_redact_empty_registry_is_identity() -> None:
    """Public-by-design path: no vault loaded → registry empty → redact is
    a passthrough. Would-fail-bug: a defensive 'redact everything that
    looks suspicious' default would break non-vault runs."""
    r = RedactionRegistry.instance()
    assert r.redact("anything goes through") == "anything goes through"


def test_redact_json_deep_walks_nested_structure() -> None:
    """redact_json walks dict/list/tuple; substitutes every str leaf;
    returns new structure; doesn't mutate input."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    payload: dict[str, Any] = {
        "outer": "no secret here",
        "nested": {
            "prompt": "the supersecret text",
            "extra": [1, "with supersecret inside", 2],
        },
        "tup": ("supersecret in a tuple", 99),
    }
    out_obj = r.redact_json(payload)
    assert isinstance(out_obj, dict)
    out: dict[str, Any] = out_obj
    assert payload["nested"]["prompt"] == "the supersecret text"  # input untouched
    assert "supersecret" not in str(out)
    assert "<prompt:positive:" in out["nested"]["prompt"]
    assert isinstance(out["nested"]["extra"], list)


def test_add_many_bulk_registers() -> None:
    """add_many flows each pair through add()."""
    r = RedactionRegistry.instance()
    r.add_many([("alpha-secret", "prompt:positive"), ("beta-secret", "lora:ref")])
    out = r.redact("got alpha-secret and beta-secret")
    assert "alpha-secret" not in out
    assert "beta-secret" not in out


def test_clear_session_resets_state() -> None:
    """clear_session drops every registered token."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    assert r.is_active
    r.clear_session()
    assert not r.is_active
    assert r.redact("got supersecret") == "got supersecret"


def test_is_active_reflects_registration() -> None:
    r = RedactionRegistry.instance()
    assert not r.is_active
    r.add("supersecret", kind="prompt:positive")
    assert r.is_active


def test_log_filter_redacts_record_msg() -> None:
    """RedactingLogFilter substitutes registered tokens in record.msg."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "got supersecret here", None, None
    )
    flt.filter(rec)
    assert isinstance(rec.msg, str)
    assert "supersecret" not in rec.msg
    assert "<prompt:positive:" in rec.msg


def test_log_filter_redacts_string_args() -> None:
    """Filter substitutes tokens in str args; non-str args untouched."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "got %s and %d", ("supersecret", 42), None
    )
    flt.filter(rec)
    assert isinstance(rec.args, tuple)
    arg0 = rec.args[0]
    assert isinstance(arg0, str)
    assert "supersecret" not in arg0
    assert rec.args[1] == 42


def test_log_filter_bypass_passes_through() -> None:
    """bypass=True makes the filter a no-op; record reaches handler unchanged."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    flt = RedactingLogFilter(r, bypass=True)
    rec = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "got supersecret", None, None
    )
    flt.filter(rec)
    assert rec.msg == "got supersecret"


def test_log_filter_empty_registry_passes_through() -> None:
    """No tokens registered + non-bypass: filter is no-op."""
    r = RedactionRegistry.instance()
    flt = RedactingLogFilter(r)
    rec = logging.LogRecord("test", logging.INFO, __file__, 1, "anything", None, None)
    flt.filter(rec)
    assert rec.msg == "anything"


def test_log_filter_on_root_reaches_children() -> None:
    """Installing filter on a handler on the 'kinoforge' root catches
    every 'kinoforge.<sub>' child via propagation. Would-fail-bug: a
    filter that only sees same-logger records would miss every per-engine
    submodule log."""
    r = RedactionRegistry.instance()
    r.add("supersecret", kind="prompt:positive")
    root = logging.getLogger("kinoforge")
    flt = RedactingLogFilter(r)
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    handler = _Capture()
    handler.addFilter(flt)
    root.addHandler(handler)
    original_level = root.level
    root.setLevel(logging.INFO)
    try:
        child = logging.getLogger("kinoforge.engines.fake")
        child.info("submitting supersecret to backend")
        assert captured
        assert all("supersecret" not in m for m in captured)
    finally:
        root.removeHandler(handler)
        root.setLevel(original_level)
