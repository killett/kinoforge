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

import json as _json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kinoforge.core.interfaces import CredentialProvider


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


def _default_http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """Module-private default HTTP GET that returns parsed JSON.

    Args:
        url: URL to GET.
        headers: Request headers to send.

    Returns:
        Parsed JSON response body as a dict.
    """
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return _json.loads(resp.read().decode("utf-8"))  # type: ignore[no-any-return]


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


class Bearer(AuthStrategy):
    """Bearer-token auth from a named env var.

    Used by the existing :class:`HostedAPIEngine` for fal.ai today; future
    Replicate / Runway / Luma integrations reuse this class with different
    ``env_var`` values.
    """

    def __init__(
        self,
        env_var: str,
        *,
        credential_provider: CredentialProvider | None = None,
        scheme: str = "Bearer",
        header_name: str = "Authorization",
        health_check_url: str | None = None,
        http_get: Callable[
            [str, dict[str, str]], dict[str, Any]
        ] = _default_http_get_json,
    ) -> None:
        """Initialise.

        Args:
            env_var: Environment variable name holding the secret token.
            credential_provider: Lookup seam. Defaults to env-backed lookup
                via :class:`kinoforge.core.credentials.EnvCredentialProvider`
                if omitted.
            scheme: Authorization scheme. Default ``"Bearer"``; some APIs use
                ``"Token"`` or ``"ApiKey"``.
            header_name: HTTP header name. Default ``"Authorization"``; some
                APIs use ``"X-Api-Key"``.
            health_check_url: Optional URL pinged in :meth:`health_check`.
                When ``None``, ``health_check`` returns ``ok=True`` with
                identity = env-var-name (used when "key is present" is all
                the check needs).
            http_get: Injectable HTTP GET seam returning parsed JSON.
        """
        # Local import to avoid a top-level cycle (credentials may import core).
        if credential_provider is None:
            from kinoforge.core.credentials import EnvCredentialProvider

            credential_provider = EnvCredentialProvider()
        self._creds = credential_provider
        self._env_var = env_var
        self._scheme = scheme
        self._header_name = header_name
        self._health_check_url = health_check_url
        self._http_get = http_get

    def credentials_present(self) -> bool:
        """Return True if the env var is set and non-empty.

        Returns:
            True when a non-empty token exists, False otherwise.
        """
        value = self._creds.get(self._env_var)
        return bool(value)

    def health_check(self) -> HealthResult:
        """Probe credentials, optionally via a live HTTP request.

        Returns:
            :class:`HealthResult` with ``ok=True`` on success or ``ok=False``
            with ``reason`` on failure.
        """
        token = self._creds.get(self._env_var)
        if not token:
            return HealthResult(
                ok=False, identity=None, reason=f"missing credential: {self._env_var}"
            )
        if self._health_check_url is None:
            # No probe URL — return ok with identity = env-var-name as a
            # proxy. Used when health_check is just "key is present".
            return HealthResult(ok=True, identity=self._env_var, reason=None)
        headers = {self._header_name: f"{self._scheme} {token}"}
        try:
            body = self._http_get(self._health_check_url, headers)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=f"HTTP error: {exc}")
        identity = (
            body.get("account_id")
            or body.get("user")
            or body.get("id")
            or self._env_var
        )
        return HealthResult(ok=True, identity=str(identity), reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Return patterns matching the actual token value.

        Returns:
            A list containing one pattern that matches the raw token string,
            or an empty list if no token is present.
        """
        token = self._creds.get(self._env_var)
        if not token:
            return []
        return [re.compile(re.escape(token))]

    def apply(self, request: HttpRequest) -> HttpRequest:
        """Return a new :class:`HttpRequest` with the Authorization header added.

        Args:
            request: The original request. Not mutated.

        Returns:
            A new :class:`HttpRequest` with merged headers.

        Raises:
            RuntimeError: If no token is present.
        """
        token = self._creds.get(self._env_var)
        if not token:
            raise RuntimeError(f"Bearer.apply called with no token in {self._env_var}")
        new_headers = dict(request.headers)
        new_headers[self._header_name] = f"{self._scheme} {token}"
        return HttpRequest(
            method=request.method,
            url=request.url,
            headers=new_headers,
            body=request.body,
        )

    def client_kwargs(self) -> dict[str, Any]:
        """Return ``{"api_key": <token>}`` when present, else ``{}``.

        Returns:
            Dict suitable for passing to SDK client constructors.
        """
        token = self._creds.get(self._env_var)
        return {"api_key": token} if token else {}
