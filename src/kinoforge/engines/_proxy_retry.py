"""Shared proxy-retry primitive for RunPod-proxy-fronted engines.

Consumed by diffusers and comfyui engine subpackages. Hosted/fal engines
sit in a different fault domain (vendor APIs, per-call billing) and do
not import this module today; if they opt in later, they MUST pass an
explicit policy to ``retry_proxy_call`` (the default
:data:`RUNPOD_PROXY_POLICY` is calibrated for the RunPod proxy only).
"""

from __future__ import annotations

import logging
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass

from kinoforge.core.cancel import CancelToken

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry strategy for a single fault domain.

    Attributes:
        transient_codes: HTTP status codes treated as transient
            (eligible for retry via :data:`backoffs`).
        backoffs: Per-attempt sleep schedule. Length determines the
            maximum number of retries (total attempts = ``1 + len(backoffs)``).
        catch_classes: Non-HTTPError exception classes treated as
            transient. Typical contents: ``(URLError, OSError)`` to
            absorb TLS resets, DNS failures, and socket-level errors.
            HTTPError is a subclass of URLError; the helper dispatches
            HTTPError first so this catch only fires for non-HTTP
            URLError variants.
        label_prefix: Tag prepended to WARNING log lines (e.g.
            ``"proxy"``). Concatenated with the per-call ``label``
            argument as ``[<prefix>.<label>]``.
    """

    transient_codes: frozenset[int]
    backoffs: tuple[float, ...]
    catch_classes: tuple[type[BaseException], ...]
    label_prefix: str


RUNPOD_PROXY_POLICY = RetryPolicy(
    transient_codes=frozenset({404, 502, 503, 504}),
    backoffs=(1.0, 2.0, 4.0, 8.0, 16.0, 16.0),
    catch_classes=(urllib.error.URLError, OSError),
    label_prefix="proxy",
)
"""Retry policy for RunPod-proxy-fronted HTTP calls.

Preserves the Phase 47 backoff calibration that closed the
``/upload/image`` + ``/history/{id}`` 404 race documented in
``project_task7_comfyui_404_regression``.
"""


def retry_proxy_call[T](
    label: str,
    url: str,
    fn: Callable[[], T],
    sleep: Callable[[float], None],
    policy: RetryPolicy = RUNPOD_PROXY_POLICY,
) -> T:
    """Run *fn* with bounded retry on transient proxy failures.

    Retries on (a) :class:`urllib.error.HTTPError` whose ``.code`` is in
    ``policy.transient_codes`` and (b) any exception class in
    ``policy.catch_classes``. Non-transient :class:`HTTPError` re-raises
    immediately. After ``policy.backoffs`` is exhausted, the final
    transient exception re-raises.

    Args:
        label: Call-site tag for log lines (e.g. ``"diffusers.result"``).
        url: URL passed to *fn*; included in WARNING messages.
        fn: Zero-arg callable performing the HTTP request.
        sleep: Injected sleep seam; receives per-attempt backoff seconds.
        policy: Retry policy. Defaults to :data:`RUNPOD_PROXY_POLICY`.
            Callers outside the RunPod-proxy fault domain MUST pass an
            explicit policy.

    Returns:
        Successful return value of *fn*.

    Raises:
        urllib.error.HTTPError: Last transient HTTPError after backoff
            exhaustion, or any non-transient HTTPError on any attempt.
        BaseException: Last instance of any ``policy.catch_classes``
            type after backoff exhaustion.
    """
    last_exc: BaseException | None = None
    attempts = 1 + len(policy.backoffs)
    for attempt_idx, delay in enumerate((0.0,) + policy.backoffs):
        if delay > 0:
            sleep(delay)
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            if exc.code not in policy.transient_codes:
                raise
            _log.warning(
                "[%s.%s] transient HTTPError url=%s code=%d attempt=%d/%d",
                policy.label_prefix,
                label,
                url,
                exc.code,
                attempt_idx + 1,
                attempts,
            )
            last_exc = exc
        except policy.catch_classes as exc:
            _log.warning(
                "[%s.%s] transient transport-error url=%s type=%s "
                "reason=%s attempt=%d/%d",
                policy.label_prefix,
                label,
                url,
                type(exc).__name__,
                str(exc)[:200],
                attempt_idx + 1,
                attempts,
            )
            last_exc = exc
    if last_exc is None:  # pragma: no cover - unreachable
        raise RuntimeError("retry_proxy_call exited loop without recording an error")
    raise last_exc


def interpoll_wait(
    seconds: float,
    cancel_token: CancelToken | None,
    sleep: Callable[[float], None],
) -> bool:
    """Cancel-aware inter-poll sleep.

    If *cancel_token* is ``None``, falls back to *sleep* (legacy callers
    + tests that stub ``sleep=lambda s: None`` to keep the loop instant).
    Otherwise, blocks on ``cancel_token.wait(seconds)`` so a mid-wait
    cancellation returns promptly.

    Args:
        seconds: Maximum wait in seconds.
        cancel_token: Token to honor, or ``None`` to skip honoring.
        sleep: Sleep seam used when *cancel_token* is ``None``.

    Returns:
        ``True`` if the cancel token fired during the wait (caller
        should re-check via ``raise_if_set``); ``False`` if the wait
        completed naturally.
    """
    if cancel_token is None:
        sleep(seconds)
        return False
    return cancel_token.wait(seconds)
