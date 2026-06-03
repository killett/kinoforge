"""Unit tests for :mod:`tools._redact`.

The tool ``tools/capture_object_info.py`` hits a live RunPod GraphQL +
REST surface. The pod ``env`` field carries plaintext ``HF_TOKEN`` and
the GraphQL mutation request body carries a plaintext
``RUNPOD_TERMINATE_KEY``. Any debug print, exception trace, or unhandled
fallthrough that surfaces one of those responses must scrub the secret
before it reaches stdout/stderr/disk.

These tests lock down the redaction primitives that future error paths
(or follow-up tooling) must reuse.
"""

from __future__ import annotations

import re

import pytest


def test_hf_token_replaced_with_named_marker() -> None:
    """Bug it catches: forgetting to scrub HF_TOKEN before printing
    a RunPod pod ``env`` field. Without redaction the token leaks to
    stdout. With redaction the literal ``hf_…`` payload is replaced
    by a named marker so an operator can still see WHICH secret was
    present without seeing its value.
    """
    from tools._redact import redact_string

    out = redact_string("HF_TOKEN=hf_AbCdEfGhIjKlMnOpQrStUv;next=field")

    assert "hf_AbCdEfGhIjKlMnOpQrStUv" not in out
    assert re.search(r"<REDACTED:hf_token>", out) is not None
    assert ";next=field" in out  # surrounding context preserved


def test_runpod_token_replaced_with_named_marker() -> None:
    """Bug it catches: scrubber only handling HF and missing the
    RunPod ``rpa_…`` shape. RUNPOD_TERMINATE_KEY shares this prefix
    and would leak via destroy-instance exception messages otherwise.
    """
    from tools._redact import redact_string

    out = redact_string("Authorization: Bearer rpa_xKsQwErTyUiOpAsDfGhJk")

    assert "rpa_xKsQwErTyUiOpAsDfGhJk" not in out
    # Bearer pattern fires first (declaration order in _CREDENTIAL_PATTERNS),
    # consuming the rpa_ token as part of the bearer payload.
    assert "<REDACTED:bearer_auth>" in out


def test_string_without_secrets_passes_through_verbatim() -> None:
    """Bug it catches: an over-eager regex that mangles benign
    strings (e.g. matches any ``\\bsk-\\w+\\b`` including ``sk-learn``).
    Any change that lowers a pattern's specificity must not eat
    non-secret text.
    """
    from tools._redact import redact_string

    benign = "GET /object_info HTTP/1.1 — pod 74i672j9h7nwqa idle 12s"
    assert redact_string(benign) == benign


def test_multiple_independent_secret_classes_in_one_string() -> None:
    """Bug it catches: a scrubber that early-returns after the first
    pattern hit and leaks every subsequent secret class. A real error
    trace may pack both an HF token (from env) AND a fal key (from
    headers) on the same line.
    """
    from tools._redact import redact_string

    msg = (
        "ConnectionError: provider=fal key=fal_key_QwErTyUiOpAsDfGhJk "
        "and HF_TOKEN=hf_AbCdEfGhIjKlMnOpQrStUv in pod env"
    )
    out = redact_string(msg)
    assert "fal_key_QwErTyUiOpAsDfGhJk" not in out
    assert "hf_AbCdEfGhIjKlMnOpQrStUv" not in out
    assert "<REDACTED:fal_key>" in out
    assert "<REDACTED:hf_token>" in out


def test_safe_print_scrubs_message_before_emitting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Bug it catches: callers wiring ``print(exc)`` directly instead
    of going through the safe wrapper. ``safe_print`` is the surface
    the tool's error paths consume; this test pins its stderr-by-default
    behaviour and scrub-on-write contract.
    """
    from tools._redact import safe_print

    safe_print("WARN destroy failed: token=hf_AbCdEfGhIjKlMnOpQrStUv")

    captured = capsys.readouterr()
    assert "hf_AbCdEfGhIjKlMnOpQrStUv" not in captured.err
    assert "<REDACTED:hf_token>" in captured.err
    assert captured.out == ""  # safe_print writes to stderr, not stdout
