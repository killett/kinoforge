"""FakeAuthStrategy — a no-network AuthStrategy for engine tests.

Layer 1 ships this fixture so future engine tests (Layers 2 + 3) and the
probe-tool tests can exercise the AuthStrategy ABC contract from the
consumer side without instantiating Bearer / GCPServiceAccount / AWSSigV4
(which require either a real env var, real GCP ADC, or real boto3 chain).
"""

from __future__ import annotations

import re
from typing import Any

from kinoforge.core.auth import AuthStrategy, HealthResult, HttpRequest


class FakeAuthStrategy(AuthStrategy):
    """No-network test double implementing the full AuthStrategy contract."""

    def __init__(
        self,
        *,
        credentials_ok: bool = True,
        fake_token: str = "fake-token-default",
        fake_identity: str = "fake-identity",
    ) -> None:
        """Initialise.

        Args:
            credentials_ok: When True, ``credentials_present()`` returns True
                and ``health_check()`` returns ok=True. When False, both
                reflect failure. Defaults to True.
            fake_token: The token value reported by ``apply()`` and
                ``client_kwargs()``. Defaults to ``"fake-token-default"``.
            fake_identity: The identity string returned by ``health_check()``
                when credentials_ok=True. Defaults to ``"fake-identity"``.
        """
        self._creds_ok = credentials_ok
        self._token = fake_token
        self._identity = fake_identity

    def credentials_present(self) -> bool:
        """Return whether fake credentials are configured.

        Returns:
            The value of the ``credentials_ok`` constructor kwarg.
        """
        return self._creds_ok

    def health_check(self) -> HealthResult:
        """Return a fake health result.

        Returns:
            ``HealthResult(ok=True, identity=fake_identity)`` when
            ``credentials_ok=True``; ``HealthResult(ok=False, reason=...)``
            when ``credentials_ok=False``.
        """
        if not self._creds_ok:
            return HealthResult(
                ok=False, identity=None, reason="fake credentials disabled"
            )
        return HealthResult(ok=True, identity=self._identity, reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Return patterns matching the fake token.

        Returns:
            A list with one compiled pattern matching the fake token when
            ``credentials_ok=True``; an empty list when ``credentials_ok=False``.
        """
        if not self._creds_ok:
            return []
        return [re.compile(re.escape(self._token))]

    def apply(self, request: HttpRequest) -> HttpRequest:
        """Return a copy of ``request`` with ``Authorization: Fake <token>`` added.

        Args:
            request: The incoming HTTP request.

        Returns:
            A new :class:`HttpRequest` with the Authorization header set.
        """
        new_headers = dict(request.headers)
        new_headers["Authorization"] = f"Fake {self._token}"
        return HttpRequest(
            method=request.method,
            url=request.url,
            headers=new_headers,
            body=request.body,
        )

    def client_kwargs(self) -> dict[str, Any]:
        """Return SDK client kwargs containing the fake token.

        Returns:
            ``{"fake_token": "<token>"}`` keyed by ``"fake_token"``.
        """
        return {"fake_token": self._token}
