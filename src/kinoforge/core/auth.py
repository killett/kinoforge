"""Pluggable authentication strategy for engines that talk to remote APIs.

Stable contract — the public surface of :class:`AuthStrategy` is locked by
:func:`tests.test_core_invariant.test_auth_strategy_abc_stable_surface`
against a checked-in baseline. Strategy-specific concerns live as
constructor kwargs on concrete impls, NOT new ABC methods.

Concrete strategies live below the ABC in this same module. Vendor SDKs are
lazy-imported inside method bodies to preserve the core-import-ban
invariant (see ``test_core_invariant.py``).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HealthResult:
    """Outcome of :meth:`AuthStrategy.health_check`.

    Attributes:
        ok: True if the credentials authenticate.
        identity: When ``ok`` is True, a printable string identifying the
            authenticated principal (e.g. SA email, IAM user ARN, Bearer
            account id). When ``ok`` is False, ``None``.
        reason: When ``ok`` is False, a short human-readable failure reason.
            When ``ok`` is True, ``None``.
    """

    ok: bool
    identity: str | None
    reason: str | None


@dataclass(frozen=True)
class HttpRequest:
    """Immutable representation of an HTTP request for :meth:`AuthStrategy.apply`.

    Used by direct-HTTP engines that do not go through an SDK. SDK-wrapped
    engines may still build an :class:`HttpRequest` and call ``apply()`` to
    produce a recording-seam-compatible request shape for fixture capture.
    """

    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class AuthStrategy(ABC):
    """Pluggable auth strategy for remote-API engines.

    The five methods below form the stable contract; concrete strategies
    MUST implement all five. Strategy-specific options live as constructor
    kwargs, not ABC methods.
    """

    @abstractmethod
    def credentials_present(self) -> bool:
        """Cheap offline probe: are required env vars / config files set?

        Returns:
            True if every credential this strategy needs is configured.
            False otherwise. Must not make any network call.
        """

    @abstractmethod
    def health_check(self) -> HealthResult:
        """Active wire probe: do credentials actually authenticate?

        Returns:
            :class:`HealthResult` with ``ok=True`` and ``identity`` populated
            on success, or ``ok=False`` and ``reason`` populated on failure.
        """

    @abstractmethod
    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Regex patterns matching secret-bearing content this strategy emits.

        Returns:
            A list of compiled :class:`re.Pattern` instances. The recording-
            seam redactor concatenates patterns from every configured
            strategy and rewrites matching content to ``"<REDACTED>"`` in
            captured fixtures.
        """

    @abstractmethod
    def apply(self, request: HttpRequest) -> HttpRequest:
        """Return a copy of ``request`` with auth added.

        Bearer adds an ``Authorization`` header; GCPServiceAccount mints +
        caches an access token and adds it as Bearer; AWSSigV4 signs the
        full request via botocore signers.
        """

    @abstractmethod
    def client_kwargs(self) -> dict[str, Any]:
        """Constructor kwargs for an engine's SDK client.

        Engines that go through a first-party SDK use this method instead of
        :meth:`apply`. Each strategy returns the kwargs its target SDK expects
        for authenticated construction.
        """
