"""Layer X: provider-agnostic balance-readout substrate.

Mirrors B5a ``core/heartbeat_endpoints.py``. Provider construction is
unchanged; the BalanceEndpoint is built CLI-side via
:func:`kinoforge._adapters.build_balance_endpoint_for` and called directly
by ``_cmd_cost``. Provider classes do not own the endpoint.

The substrate ships one real satisfier today (RunPod GraphQL); every
other provider / engine kind resolves to :class:`NoBalanceEndpoint`,
which makes :func:`provider_balance_supported` False and the renderer
pick the ``balance: N/A`` literal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

__all__ = [
    "BalanceEndpoint",
    "NoBalanceEndpoint",
    "ProviderBalance",
    "TransportError",
    "provider_balance_supported",
]


class TransportError(Exception):
    """Wire-level failure: 5xx, timeout, DNS, malformed body, schema drift.

    A direct subclass of :class:`Exception` (NOT :class:`ValueError`) so
    accidental broad ``except ValueError`` arms in CLI code do not swallow
    it. Cred-missing failures do NOT raise; satisfier returns ``None``.
    """


@dataclass(frozen=True)
class ProviderBalance:
    """Operator account balance with a provider.

    Attributes:
        usd: Numeric balance in the declared currency. Negative is allowed
            verbatim per the failure-mode contract (RunPod auto-debit
            accounts can sit briefly negative).
        as_of: Local-TZ timestamp the wire read returned.
        source: Provenance string (e.g. ``"runpod-graphql-clientBalance"``).
        currency: Three-letter currency code; defaults to ``"USD"`` so
            today's call sites stay simple. Future satisfiers can declare
            non-USD without Protocol churn.
    """

    usd: float
    as_of: datetime
    source: str
    currency: str = "USD"


@runtime_checkable
class BalanceEndpoint(Protocol):
    """Read the operator's account balance with the provider.

    Implementations bind credentials at construction time; ``read()``
    takes no arguments and returns a fresh :class:`ProviderBalance` or
    ``None``.

    Failure contract:
        * Transport / 5xx / shape drift → raise :class:`TransportError`.
        * Missing credential → return ``None``.
        * Schema-valid response with negative balance → return verbatim.
    """

    def read(self) -> ProviderBalance | None:
        """Read the operator's account balance."""
        ...


class NoBalanceEndpoint:
    """Ships for every provider/engine without a real satisfier.

    ``read()`` returns ``None`` unconditionally; the renderer pairs this
    with :func:`provider_balance_supported` returning ``False`` to pick
    the ``balance: N/A`` literal instead of ``balance: ? (no credential)``.
    """

    def read(self) -> None:
        """Return ``None`` unconditionally; no satisfier ships."""
        return None


_SUPPORTED: frozenset[str] = frozenset({"runpod"})


def provider_balance_supported(provider_kind: str) -> bool:
    """True iff a real satisfier ships for ``provider_kind``.

    Sister to B5a's
    :func:`kinoforge.core.heartbeat_endpoints.provider_heartbeat_supported`.
    Renderer uses this to pick ``balance: N/A`` (no satisfier) vs
    ``balance: ? (no credential)`` (no cred) vs ``balance: $X`` (success).

    Args:
        provider_kind: Lowercase provider kind string from
            ``cfg.compute.provider`` (e.g. ``"runpod"``, ``"skypilot"``,
            ``"local"``).

    Returns:
        True only when a balance satisfier module ships for that kind.
        Today this is ``"runpod"`` only.
    """
    return provider_kind in _SUPPORTED
