"""Tests for the shared proxy-retry helper.

Every test asserts an observable behavior and names (in its docstring)
the concrete bug it catches. Mocks live only at the HTTP boundary
(the ``fn`` callable + the ``sleep`` callable); they do not mirror
the helper's internal control flow.
"""

from __future__ import annotations

import dataclasses
import urllib.error
from dataclasses import FrozenInstanceError

import pytest

from kinoforge.engines._proxy_retry import (
    RUNPOD_PROXY_POLICY,
    RetryPolicy,
    interpoll_wait,
    retry_proxy_call,
)


def _make_http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://example/x",
        code=code,
        msg="x",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


# --- public surface ---------------------------------------------------


def test_module_loads_and_exposes_public_surface() -> None:
    """Catches: accidental rename or deletion of public exports."""
    assert isinstance(RUNPOD_PROXY_POLICY, RetryPolicy)
    assert callable(retry_proxy_call)
    assert callable(interpoll_wait)


def test_runpod_policy_constant_values() -> None:
    """Catches: drift from Phase 47 calibration (silent behavior change).

    The exact constants are load-bearing for the comfyui regression
    suite + diffusers production behavior.
    """
    assert RUNPOD_PROXY_POLICY.transient_codes == frozenset({404, 502, 503, 504})
    assert RUNPOD_PROXY_POLICY.backoffs == (1.0, 2.0, 4.0, 8.0, 16.0, 16.0)
    assert RUNPOD_PROXY_POLICY.catch_classes == (urllib.error.URLError, OSError)
    assert RUNPOD_PROXY_POLICY.label_prefix == "proxy"


def test_policy_is_frozen() -> None:
    """Catches: someone flips frozen=False, introducing shared mutable state."""
    with pytest.raises(FrozenInstanceError):
        RUNPOD_PROXY_POLICY.backoffs = ()  # type: ignore[misc]


# --- retry_proxy_call dispatch ----------------------------------------


def test_retries_transient_http_code(fast_policy: RetryPolicy) -> None:
    """Catches: HTTPError transient-codes branch dropped or .code check omitted."""
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return "ok"

    result = retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy)
    assert result == "ok"
    assert attempts["n"] == 3


def test_raises_non_transient_http_immediately(fast_policy: RetryPolicy) -> None:
    """Catches: missing ``code not in transient_codes`` guard.

    A 500 must propagate on attempt 1 without sleep — otherwise we
    retry hard-failures and amplify load on the server.
    """
    sleeps: list[float] = []

    def fn() -> str:
        raise _make_http_error(500)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, sleeps.append, fast_policy)
    assert exc_info.value.code == 500
    assert sleeps == []


def test_retries_url_error(fast_policy: RetryPolicy) -> None:
    """Catches: catch_classes branch missing or URLError absent from policy.

    This is the exact failure mode from the user's 2026-06-21 crash.
    """
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError(ConnectionResetError(104, "reset"))
        return "ok"

    result = retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy)
    assert result == "ok"
    assert attempts["n"] == 2


def test_retries_os_error(fast_policy: RetryPolicy) -> None:
    """Catches: OSError absent from catch_classes."""
    attempts = {"n": 0}

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise OSError(104, "Connection reset by peer")
        return "ok"

    assert retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy) == "ok"
    assert attempts["n"] == 2


def test_exhausted_retry_reraises_last(fast_policy: RetryPolicy) -> None:
    """Catches: helper masks the final exception with RuntimeError or first-exc."""
    counter = {"n": 0}

    def fn() -> None:
        counter["n"] += 1
        raise _make_http_error(503)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy)
    assert exc_info.value.code == 503
    # 1 initial + 3 retries = 4 attempts with fast_policy
    assert counter["n"] == 4


def test_success_on_third_attempt_returns_value(fast_policy: RetryPolicy) -> None:
    """Catches: helper keeps sleeping/retrying after success."""
    attempts = {"n": 0}

    def fn() -> int:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return 42

    assert retry_proxy_call("test", "http://x", fn, lambda _: None, fast_policy) == 42
    assert attempts["n"] == 3


def test_sleep_called_with_exact_schedule() -> None:
    """Catches: off-by-one in backoff indexing or wrong tuple element selected."""
    sleeps: list[float] = []
    attempts = {"n": 0}
    policy = dataclasses.replace(RUNPOD_PROXY_POLICY, backoffs=(0.5, 1.5, 3.5))

    def fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_http_error(502)
        return "ok"

    retry_proxy_call("test", "http://x", fn, sleeps.append, policy)
    assert sleeps == [0.5, 1.5]


def test_http_error_dispatch_precedes_url_error() -> None:
    """Catches: except-clause ordering inverted.

    HTTPError is a subclass of URLError. If the URLError branch is
    listed first, a non-transient HTTPError (e.g. 500) would be caught
    as URLError, logged as transport-error, retried, and never propagate
    correctly. This test asserts a 500 raises ON ATTEMPT 1 even though
    URLError is in catch_classes.
    """
    sleeps: list[float] = []
    policy = dataclasses.replace(
        RUNPOD_PROXY_POLICY,
        backoffs=(0.0, 0.0, 0.0),
        catch_classes=(urllib.error.URLError, OSError),
    )

    def fn() -> str:
        raise _make_http_error(500)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        retry_proxy_call("test", "http://x", fn, sleeps.append, policy)
    assert exc_info.value.code == 500
    assert sleeps == []


# --- interpoll_wait ---------------------------------------------------


def test_interpoll_wait_none_token_uses_sleep() -> None:
    """Catches: regression where None-token path raises AttributeError."""
    sleeps: list[float] = []
    result = interpoll_wait(2.5, None, sleeps.append)
    assert result is False
    assert sleeps == [2.5]


def test_interpoll_wait_token_returns_token_wait_result() -> None:
    """Catches: helper swallows the token's wait return value (lost cancel signal)."""
    from kinoforge.core.cancel import CancelToken

    token = CancelToken()
    sleeps: list[float] = []
    # Pre-set the token so wait returns True immediately.
    token.set()
    result = interpoll_wait(10.0, token, sleeps.append)
    assert result is True
    assert sleeps == []  # token path does not call the sleep seam
