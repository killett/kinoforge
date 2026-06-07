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

import datetime as _dt
import hashlib as _hashlib
import hmac as _hmac
import json as _json
import os
import re
import urllib.parse as _urlparse
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


class GCPServiceAccount(AuthStrategy):
    """GCP auth via the ``google.auth`` default credential chain.

    Used by VeoEngine (Layer 2) and any future Vertex AI integrations
    (Imagen, Lyria, Gemini-Vision). The ``google.auth`` SDK is lazy-imported
    inside method bodies to preserve the core-import-ban invariant.
    """

    # GCP access-token shape: ya29.<base64-ish>
    _TOKEN_PATTERN = re.compile(r"ya29\.[A-Za-z0-9_\-]+")

    def __init__(
        self,
        *,
        scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/cloud-platform",),
        quota_project_id: str | None = None,
        impersonation_chain: tuple[str, ...] | None = None,
        subject: str | None = None,
    ) -> None:
        """Initialise.

        Args:
            scopes: OAuth2 scopes to request. Defaults to cloud-platform.
            quota_project_id: GCP project to bill API usage against.
            impersonation_chain: Service account impersonation chain (unused in
                Layer 1; wired in a future per-engine retrofit if needed).
            subject: Subject claim for domain-wide delegation (unused in
                Layer 1; wired in a future per-engine retrofit if needed).
        """
        self._scopes = tuple(scopes)
        self._quota_project_id = quota_project_id
        self._impersonation_chain = impersonation_chain
        self._subject = subject

    def credentials_present(self) -> bool:
        """Return True when ``GOOGLE_APPLICATION_CREDENTIALS`` points to an existing file.

        Returns:
            True if the ADC file exists, False otherwise.
        """
        adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if adc and os.path.exists(adc):
            return True
        return False

    def health_check(self) -> HealthResult:
        """Mint a token via ``google.auth.default`` and return identity.

        Returns:
            :class:`HealthResult` with ``ok=True`` and SA email as identity
            on success, or ``ok=False`` with the exception text as reason.
        """
        try:
            import google.auth  # lazy
            import google.auth.transport.requests  # lazy

            credentials, _project = google.auth.default(
                scopes=self._scopes, quota_project_id=self._quota_project_id
            )
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)  # type: ignore[no-untyped-call]
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=str(exc))
        identity = (
            getattr(credentials, "service_account_email", None) or "gcp-credentials"
        )
        return HealthResult(ok=True, identity=identity, reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Return a pattern matching the GCP access-token shape (``ya29.<...>``).

        Returns:
            A list containing one compiled :class:`re.Pattern`.
        """
        return [self._TOKEN_PATTERN]

    def apply(self, request: HttpRequest) -> HttpRequest:
        """Return a new :class:`HttpRequest` with ``Authorization: Bearer <token>`` added.

        Mints a fresh token via ``google.auth.default`` on every call; callers
        that need caching should wrap this strategy.

        Args:
            request: The original request. Not mutated.

        Returns:
            A new :class:`HttpRequest` with the Authorization header added.
        """
        import google.auth  # lazy
        import google.auth.transport.requests  # lazy

        credentials, _project = google.auth.default(
            scopes=self._scopes, quota_project_id=self._quota_project_id
        )
        credentials.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
        new_headers = dict(request.headers)
        new_headers["Authorization"] = f"Bearer {credentials.token}"
        return HttpRequest(
            method=request.method,
            url=request.url,
            headers=new_headers,
            body=request.body,
        )

    def client_kwargs(self) -> dict[str, Any]:
        """Return ``{"credentials": <google.auth.credentials.Credentials>}``.

        Returns:
            Dict with the credentials object for SDK client construction.
        """
        import google.auth  # lazy

        credentials, _project = google.auth.default(
            scopes=self._scopes, quota_project_id=self._quota_project_id
        )
        return {"credentials": credentials}


class AWSSigV4(AuthStrategy):
    """AWS request signing via the ``boto3`` Session credential chain.

    Used by NovaReelEngine (Layer 3) and any future Bedrock integrations.
    The ``boto3`` SDK is lazy-imported inside method bodies to preserve
    the core-import-ban invariant. SigV4 signing is performed directly
    with stdlib ``hashlib`` + ``hmac`` so the seam stays SDK-version-
    independent.
    """

    # AWS IAM access-key shapes: long-term (AKIA) + STS session (ASIA).
    _ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
    # SigV4 Authorization header signature payload.
    _AUTHZ_PATTERN = re.compile(
        r"AWS4-HMAC-SHA256\s+Credential=[^\s,]+/[0-9]+/[a-z0-9-]+/[a-z0-9-]+/aws4_request"
    )
    # Session-token header value.
    _SESSION_TOKEN_PATTERN = re.compile(r"X-Amz-Security-Token:[^\r\n]+")

    def __init__(
        self,
        *,
        region_name: str,
        service_name: str = "bedrock-runtime",
        profile_name: str | None = None,
        assume_role_arn: str | None = None,
        assume_role_external_id: str | None = None,
    ) -> None:
        """Initialise.

        Args:
            region_name: AWS region (e.g. ``"us-east-1"``).
            service_name: AWS service name used in credential scope.
                Defaults to ``"bedrock-runtime"``.
            profile_name: Named AWS CLI profile to use. ``None`` uses the
                default credential chain.
            assume_role_arn: IAM role ARN to assume before signing (stored
                for future use; not wired in Layer 1).
            assume_role_external_id: External ID for role assumption (stored
                for future use; not wired in Layer 1).
        """
        self._region_name = region_name
        self._service_name = service_name
        self._profile_name = profile_name
        self._assume_role_arn = assume_role_arn
        self._assume_role_external_id = assume_role_external_id

    def _session(self) -> Any:  # noqa: ANN401
        """Return a ``boto3.Session``, lazy-importing boto3.

        Returns:
            A ``boto3.Session`` instance.
        """
        import boto3  # lazy — never at module top level

        return boto3.Session(profile_name=self._profile_name)

    def credentials_present(self) -> bool:
        """Return True when the boto3 session resolves credentials.

        Returns:
            True if ``boto3.Session().get_credentials()`` returns non-None.
        """
        try:
            return self._session().get_credentials() is not None
        except Exception:  # noqa: BLE001
            return False

    def health_check(self) -> HealthResult:
        """Call STS ``GetCallerIdentity`` to verify credentials.

        Returns:
            :class:`HealthResult` with the caller ARN as identity on success,
            or ``ok=False`` with the exception text as reason on failure.
        """
        session = self._session()
        if session.get_credentials() is None:
            return HealthResult(
                ok=False, identity=None, reason="no AWS credentials in chain"
            )
        try:
            sts = session.client("sts", region_name=self._region_name)
            ident = sts.get_caller_identity()
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=str(exc))
        return HealthResult(ok=True, identity=str(ident["Arn"]), reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        """Return patterns matching AWS access keys, SigV4 signatures, and session tokens.

        Returns:
            A list of three compiled :class:`re.Pattern` instances.
        """
        return [
            self._ACCESS_KEY_PATTERN,
            self._AUTHZ_PATTERN,
            self._SESSION_TOKEN_PATTERN,
        ]

    def apply(self, request: HttpRequest) -> HttpRequest:
        """Sign ``request`` with AWS SigV4 and return a new :class:`HttpRequest`.

        Signing is performed directly via stdlib ``hashlib`` + ``hmac`` rather
        than going through ``botocore.auth``, keeping the seam SDK-version-
        independent.

        Args:
            request: The original request. Not mutated.

        Returns:
            A new :class:`HttpRequest` with ``Authorization``, ``X-Amz-Date``,
            and ``X-Amz-Content-Sha256`` headers added (plus
            ``X-Amz-Security-Token`` when a session token is present).

        Raises:
            RuntimeError: If no AWS credentials are available.
        """
        creds = self._session().get_credentials()
        if creds is None:
            raise RuntimeError("AWSSigV4.apply called with no AWS credentials")
        frozen = creds.get_frozen_credentials()

        method = request.method.upper()
        parsed = _urlparse.urlparse(request.url)
        canonical_uri = parsed.path or "/"
        canonical_query = parsed.query
        body = request.body or b""
        payload_hash = _hashlib.sha256(body).hexdigest()

        amz_date = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        host = parsed.netloc

        # Build the signed-headers map (lowercase keys).
        headers_lc = {k.lower(): v for k, v in request.headers.items()}
        headers_lc.setdefault("host", host)
        headers_lc["x-amz-date"] = amz_date
        headers_lc["x-amz-content-sha256"] = payload_hash
        if frozen.token:
            headers_lc["x-amz-security-token"] = frozen.token

        signed_headers = ";".join(sorted(headers_lc))
        canonical_headers = "".join(
            f"{k}:{headers_lc[k].strip()}\n" for k in sorted(headers_lc)
        )

        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )

        credential_scope = (
            f"{date_stamp}/{self._region_name}/{self._service_name}/aws4_request"
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                _hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return _hmac.new(key, msg.encode("utf-8"), _hashlib.sha256).digest()

        k_date = _sign(("AWS4" + frozen.secret_key).encode("utf-8"), date_stamp)
        k_region = _sign(k_date, self._region_name)
        k_service = _sign(k_region, self._service_name)
        k_signing = _sign(k_service, "aws4_request")
        signature = _hmac.new(
            k_signing, string_to_sign.encode("utf-8"), _hashlib.sha256
        ).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 "
            f"Credential={frozen.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        new_headers = dict(request.headers)
        new_headers["Authorization"] = authorization
        new_headers["X-Amz-Date"] = amz_date
        new_headers["X-Amz-Content-Sha256"] = payload_hash
        if frozen.token:
            new_headers["X-Amz-Security-Token"] = frozen.token

        return HttpRequest(
            method=request.method,
            url=request.url,
            headers=new_headers,
            body=request.body,
        )

    def client_kwargs(self) -> dict[str, Any]:
        """Return AWS credential kwargs for SDK client construction.

        Returns:
            Dict with ``aws_access_key_id``, ``aws_secret_access_key``,
            ``region_name``, and (when present) ``aws_session_token``.
        """
        creds = self._session().get_credentials()
        if creds is None:
            return {"region_name": self._region_name}
        frozen = creds.get_frozen_credentials()
        kwargs: dict[str, Any] = {
            "aws_access_key_id": frozen.access_key,
            "aws_secret_access_key": frozen.secret_key,
            "region_name": self._region_name,
        }
        if frozen.token:
            kwargs["aws_session_token"] = frozen.token
        return kwargs


# ---------------------------------------------------------------------------
# Registry + factory
# ---------------------------------------------------------------------------

from kinoforge.core.errors import UnknownAdapter  # noqa: E402 — after all classes

_REGISTRY: dict[str, type[AuthStrategy]] = {
    "bearer": Bearer,
    "gcp_service_account": GCPServiceAccount,
    "aws_sigv4": AWSSigV4,
}


def build_auth_strategy(spec: dict[str, Any]) -> AuthStrategy:
    """Construct a concrete :class:`AuthStrategy` from a parsed YAML block.

    Args:
        spec: A mapping with a required ``"strategy"`` key naming one of
            the registered strategy names plus any strategy-specific kwargs.

    Returns:
        An instance of the named strategy.

    Raises:
        KeyError: ``"strategy"`` key is missing from ``spec``.
        UnknownAdapter: ``"strategy"`` names an unregistered strategy.
        TypeError: strategy-specific kwargs are wrong; re-raised with the
            offending strategy name for context.
    """
    if "strategy" not in spec:
        raise KeyError(
            f"auth spec must include a 'strategy' key; got keys: {sorted(spec.keys())}"
        )
    name = spec["strategy"]
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownAdapter(
            f"unknown auth strategy: {name!r} (registered: {sorted(_REGISTRY)})"
        )
    kwargs = {k: v for k, v in spec.items() if k != "strategy"}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise TypeError(f"failed to construct auth strategy {name!r}: {exc}") from exc
