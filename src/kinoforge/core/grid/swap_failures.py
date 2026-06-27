"""Classify swap-mode subprocess failures into RETRY / CONTINUE / ABORT.

Called by :func:`_run_swap_group` when a cell's ``kinoforge generate``
subprocess exits non-zero. The grid spec's ``on_swap_failure`` literal
(``strict`` / ``continue`` / ``classify``) selects the policy.

Pattern catalogue sourced from:
- P2 server-side structured exceptions in
  ``src/kinoforge/engines/diffusers/servers/wan_t2v_server.py``:
  ``VRAMRollbackFailure``, ``BranchUnsupportedOnSingleTransformer``,
  ``BranchAutoNotAllowedOnMoE``, ``BranchUnknown``, ``SwapRejectedDetails``.
- Proxy / HTTP edges documented by memories
  ``wan_server_set_stack_proxy_warmup`` and ``wan_t2v_server async blocking``:
  502 from RunPod proxy on first POST, ``ProxyWarmupTimeout`` after long
  downloads, plain ``ConnectionError`` mid-flight.
- OOM-kill signal (exit code 137) from the kernel.

Unknown errors default to ABORT under ``classify`` so a misclassified
flake cannot quietly burn budget across the remaining cells.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Final, Literal

RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_BACKOFF_S: Final[float] = 5.0


class SwapFailureAction(Enum):
    """What the executor should do next after a swap-cell subprocess fails."""

    RETRY = "retry"
    CONTINUE = "continue"
    ABORT = "abort"


_TRANSIENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ProxyWarmupTimeout", re.IGNORECASE),
    re.compile(r"ConnectionError", re.IGNORECASE),
    re.compile(r"\b502\b"),
)

_RECOVERABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"SwapRejectedDetails", re.IGNORECASE),
    re.compile(r"BranchUnsupportedOnSingleTransformer", re.IGNORECASE),
    re.compile(r"BranchAutoNotAllowedOnMoE", re.IGNORECASE),
    re.compile(r"BranchUnknown", re.IGNORECASE),
)

_UNRECOVERABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"VRAMRollbackFailure", re.IGNORECASE),
    re.compile(r"RunPodGraphQLError", re.IGNORECASE),
    re.compile(r"\b5\d\d\b.*after\s+\d+\s+retries", re.IGNORECASE),
    re.compile(r"OOMKilled", re.IGNORECASE),
)


def _matches_any(stderr: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(p.search(stderr) for p in patterns)


def _classify_swap_failure(
    stderr: str,
    exit_code: int,
    policy: Literal["strict", "continue", "classify"],
) -> SwapFailureAction:
    """Classify a swap-cell subprocess failure into the next action.

    Args:
        stderr: Captured stderr of the failed subprocess.
        exit_code: Subprocess return code. ``0`` returns ``CONTINUE``
            defensively (callers should only invoke on non-zero, but a
            buggy caller must not accidentally abort the group).
        policy: Grid-level failure policy from
            :attr:`GridSpec.on_swap_failure`.

    Returns:
        The :class:`SwapFailureAction` the executor should take.
    """
    if exit_code == 0:
        return SwapFailureAction.CONTINUE
    if exit_code == 137:
        return SwapFailureAction.ABORT

    if policy == "strict":
        return SwapFailureAction.ABORT

    if _matches_any(stderr, _UNRECOVERABLE_PATTERNS):
        return SwapFailureAction.ABORT

    if policy == "continue":
        return SwapFailureAction.CONTINUE

    if _matches_any(stderr, _TRANSIENT_PATTERNS):
        return SwapFailureAction.RETRY
    if _matches_any(stderr, _RECOVERABLE_PATTERNS):
        return SwapFailureAction.CONTINUE
    return SwapFailureAction.ABORT
