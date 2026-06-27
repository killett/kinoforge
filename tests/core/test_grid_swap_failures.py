"""Classification of swap-mode subprocess failures into RETRY/CONTINUE/ABORT."""

from __future__ import annotations

import pytest

from kinoforge.core.grid.swap_failures import (
    RETRY_BACKOFF_S,
    RETRY_MAX_ATTEMPTS,
    SwapFailureAction,
    _classify_swap_failure,
)


def test_retry_budget_constants_match_spec() -> None:
    """Spec §5 binds the budget to 3 attempts at 5s; loosening either
    silently changes live-fire cost and recovery latency."""
    assert RETRY_MAX_ATTEMPTS == 3
    assert RETRY_BACKOFF_S == pytest.approx(5.0)


def test_enum_has_exactly_three_actions() -> None:
    assert {a.name for a in SwapFailureAction} == {"RETRY", "CONTINUE", "ABORT"}


# ---------------------------------------------------------------------------
# strict policy: ALWAYS ABORT on any non-zero exit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr,exit_code",
    [
        ("ProxyWarmupTimeout: ...", 1),
        ("SwapRejectedDetails: ...", 1),
        ("VRAMRollbackFailure: ...", 1),
        ("", 137),
        ("anything else", 1),
    ],
)
def test_strict_policy_always_aborts_on_non_zero_exit(
    stderr: str, exit_code: int
) -> None:
    assert (
        _classify_swap_failure(stderr, exit_code, "strict") is SwapFailureAction.ABORT
    )


# ---------------------------------------------------------------------------
# classify policy: route by pattern catalogue.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "ProxyWarmupTimeout: pod proxy not ready after 60s",
        "ConnectionError: [Errno 111] Connection refused",
        "HTTPError: 502 Bad Gateway from https://x.proxy.runpod.net",
    ],
)
def test_classify_transient_returns_retry(stderr: str) -> None:
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.RETRY


@pytest.mark.parametrize(
    "stderr",
    [
        "SwapRejectedDetails: branch routing rejected by server",
        "BranchUnsupportedOnSingleTransformer: pipe has 1 transformer",
        "BranchAutoNotAllowedOnMoE: must specify high or low",
        "BranchUnknown: branch=bogus not in {high, low, auto}",
    ],
)
def test_classify_recoverable_returns_continue(stderr: str) -> None:
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.CONTINUE


@pytest.mark.parametrize(
    "stderr",
    [
        "VRAMRollbackFailure: peft load_lora_weights failed, pipe corrupted",
        "RunPodGraphQLError: podEditJob returned errors",
        "HTTPError: 500 Internal Server Error after 3 retries",
        "OOMKilled",
    ],
)
def test_classify_unrecoverable_returns_abort(stderr: str) -> None:
    assert _classify_swap_failure(stderr, 1, "classify") is SwapFailureAction.ABORT


def test_classify_exit_137_oom_returns_abort_regardless_of_stderr() -> None:
    assert _classify_swap_failure("", 137, "classify") is SwapFailureAction.ABORT
    assert (
        _classify_swap_failure("ProxyWarmupTimeout: pretend transient", 137, "classify")
        is SwapFailureAction.ABORT
    )


def test_classify_unknown_stderr_returns_abort_fail_safe() -> None:
    """Default to ABORT on unknown errors so a misclassified flake
    cannot quietly burn budget across the remaining cells."""
    assert (
        _classify_swap_failure("some unrelated traceback", 1, "classify")
        is SwapFailureAction.ABORT
    )


# ---------------------------------------------------------------------------
# continue policy: recoverable + ambiguous continue; unrecoverable abort.
# ---------------------------------------------------------------------------


def test_continue_policy_continues_on_ambiguous() -> None:
    assert (
        _classify_swap_failure("some unrelated traceback", 1, "continue")
        is SwapFailureAction.CONTINUE
    )


def test_continue_policy_continues_on_recoverable() -> None:
    assert (
        _classify_swap_failure("BranchUnknown: ...", 1, "continue")
        is SwapFailureAction.CONTINUE
    )


@pytest.mark.parametrize(
    "stderr",
    [
        "VRAMRollbackFailure: pipe state lost",
        "RunPodGraphQLError: api blew up",
    ],
)
def test_continue_policy_aborts_on_truly_unrecoverable(stderr: str) -> None:
    """`continue` is NOT 'continue at all costs' — known-corrupt pod
    state must still abort or the next cell will hit garbage."""
    assert _classify_swap_failure(stderr, 1, "continue") is SwapFailureAction.ABORT


def test_continue_policy_oom_137_aborts() -> None:
    assert _classify_swap_failure("", 137, "continue") is SwapFailureAction.ABORT


# ---------------------------------------------------------------------------
# Defensive: exit_code == 0 should not normally be passed, but guard it.
# ---------------------------------------------------------------------------


def test_zero_exit_returns_continue_defensively() -> None:
    """Caller invariant: only invoke on non-zero exit. Defensive
    fallback returns CONTINUE so a buggy caller doesn't accidentally
    abort the whole group on a successful subprocess."""
    assert _classify_swap_failure("", 0, "classify") is SwapFailureAction.CONTINUE
