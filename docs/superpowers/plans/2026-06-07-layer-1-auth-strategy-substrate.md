# Layer 1 — `AuthStrategy` substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a pluggable `AuthStrategy` ABC with Bearer / GCPServiceAccount / AWSSigV4 implementations, a `build_auth_strategy` registry function, a `HostedAPIEngine` retrofit that consumes the ABC with full backward-compat, a `tools/probe_hosted.py` preflight tool, and a signature-baseline invariant test. Foundation for Layer 2 (Veo) and Layer 3 (Nova Reel) plus future Bearer providers (Replicate / Runway / Luma).

**Architecture:** A small, locked ABC surface (5 methods, typed boundary objects) with concrete strategies that lazy-import vendor SDKs to preserve the core-import-ban invariant. The registry function maps a YAML `auth:` block discriminator to a concrete strategy instance, consumed by engine configs. Backward-compat is enforced by defaulting `auth_strategy=None` on `HostedAPIEngine` to a Bearer derived from the existing `cfg.api_key_env`.

**Tech Stack:** Python 3.13, pydantic v2, `re`, `dataclasses`, lazy `google.auth` (Task 3), lazy `boto3` (Task 4). No live cloud calls in Layer 1.

**Spec reference:** `docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md` — Sections 3, 4.3, 4.4, 6.7 (Layer 1 ordering), 7 (risk mitigation).

**Layer sequencing hard-block:** Layer 2 (Veo) and Layer 3 (Nova Reel) plans MUST hard-block on this layer merged to `main` per spec §2. The merge commit SHA must appear in the Layer 2/3 plan headers as a `## Depends on` line.

**Live spend in this layer:** $0.00. Fully offline.

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `pixi.toml` | Modify | Pin `boto3 = ">=1.34,<2.0"` (replacing current `"*"`) |
| `src/kinoforge/core/auth.py` | Create | `AuthStrategy` ABC + `HealthResult` + `HttpRequest` + 3 concrete strategies + `build_auth_strategy` registry |
| `tests/core/test_auth.py` | Create | Unit tests for ABC, 3 strategies, and registry |
| `tests/fixtures/auth_strategy_baseline.json` | Create | Signature baseline for invariant test |
| `tests/test_core_invariant.py` | Modify | Add `test_auth_strategy_abc_stable_surface` test; extend subprocess-isolation check to scan for lazy-SDK leaks |
| `tests/_fixtures/__init__.py` | Create | New shared-fixtures package |
| `tests/_fixtures/fake_auth.py` | Create | `FakeAuthStrategy` no-network fixture used by future engine tests |
| `tests/_fixtures/test_fake_auth.py` | Create | Tests for the fixture itself |
| `src/kinoforge/engines/hosted/__init__.py` | Modify | Retrofit `HostedAPIEngine.__init__` to accept `auth_strategy: AuthStrategy \| None = None`; default-derive Bearer from `cfg.api_key_env` for backward-compat |
| `tests/engines/test_hosted.py` | Modify | Existing tests stay green; add 2 new tests covering explicit `auth_strategy=Bearer(...)` path |
| `tools/probe_hosted.py` | Create | Preflight tool: load config, walk every configured `AuthStrategy`, run `credentials_present` + `health_check` + per-strategy feature probe; atomic snapshot write |
| `tests/test_probe_hosted.py` | Create | Unit tests for probe_hosted with `FakeAuthStrategy` |
| `pixi.toml` | Modify | Add `[tasks] probe-hosted = "python -m tools.probe_hosted"` |
| `README.md` | Modify | Add "Auth strategies" subsection under "Real providers" |
| `PROGRESS.md` | Modify | Add Phase 41 (Layer 1) entry with per-task SHAs |

---

## Task 0: Pin `boto3` to known-good range in `pixi.toml`

**Goal:** Replace `boto3 = "*"` with `boto3 = ">=1.34,<2.0"` so the SigV4 strategy in Task 4 builds against a tested SDK version range. Mirrors Phase 38's intent that was never formalized.

**Files:**
- Modify: `pixi.toml` (the existing `boto3 = "*"` line under `[dependencies]`)

**Acceptance Criteria:**
- [ ] `boto3 = ">=1.34,<2.0"` replaces `boto3 = "*"` in `pixi.toml`
- [ ] `pixi install --dry-run` resolves cleanly (no version conflicts)
- [ ] Full test suite still passes after the pin

**Verify:** `pixi install && pixi run test 2>&1 | tail -5` → exit 0, "passed" in summary

**Steps:**

- [ ] **Step 1: Show the existing boto3 line**

Run: `rg -n "^boto3" pixi.toml`
Expected output: a single line like `64:boto3 = "*"`

- [ ] **Step 2: Apply the pin**

Edit `pixi.toml` — change:

```toml
boto3 = "*"
```

to:

```toml
boto3 = ">=1.34,<2.0"
```

- [ ] **Step 3: Reinstall and verify resolution**

Run: `pixi install --dry-run 2>&1 | tail -10`
Expected: no "conflict" or "could not find" lines; environment resolves.

- [ ] **Step 4: Run full suite as regression check**

Run: `pixi run test 2>&1 | tail -10`
Expected: all green, same pass count as pre-change.

- [ ] **Step 5: Commit**

```bash
git add pixi.toml
git commit -m "chore(deps): pin boto3 to >=1.34,<2.0 (was unpinned)

Layer 1 AuthStrategy AWSSigV4 lazy-imports boto3; lock the major
range now to prevent silent SDK shape drift between Phase 38 (which
established the pattern but left the pin loose) and Layer 1 codifying
it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 1: `AuthStrategy` ABC + typed boundary objects

**Goal:** Create `core/auth.py` with `HealthResult` and `HttpRequest` frozen dataclasses plus the abstract `AuthStrategy` ABC. No concrete strategy yet — just the contract.

**Files:**
- Create: `src/kinoforge/core/auth.py`
- Create: `tests/core/test_auth.py`

**Acceptance Criteria:**
- [ ] `HealthResult` is a frozen dataclass with fields `(ok: bool, identity: str | None, reason: str | None)`
- [ ] `HttpRequest` is a frozen dataclass with fields `(method: str, url: str, headers: dict[str, str], body: bytes | None)`
- [ ] `AuthStrategy` is an `ABC` with 5 `@abstractmethod`s: `credentials_present`, `health_check`, `redact_patterns`, `apply`, `client_kwargs`
- [ ] Attempting to instantiate `AuthStrategy` directly raises `TypeError` (the standard ABC-abstract-method behavior)
- [ ] `HealthResult` is frozen (assignment raises `dataclasses.FrozenInstanceError`)
- [ ] `HttpRequest` is frozen (same)

**Verify:** `pixi run test tests/core/test_auth.py -v 2>&1 | tail -10` → 5 tests passed

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_auth.py`:

```python
"""Layer 1 — AuthStrategy ABC + typed boundary objects."""

from __future__ import annotations

import dataclasses
import re

import pytest

from kinoforge.core.auth import (
    AuthStrategy,
    HealthResult,
    HttpRequest,
)


# ---------------------------------------------------------------------------
# Boundary types
# ---------------------------------------------------------------------------


def test_health_result_is_frozen_dataclass() -> None:
    r = HealthResult(ok=True, identity="kinoforge-runner@proj.iam.gserviceaccount.com", reason=None)
    assert r.ok is True
    assert r.identity == "kinoforge-runner@proj.iam.gserviceaccount.com"
    assert r.reason is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.ok = False  # type: ignore[misc]


def test_http_request_is_frozen_dataclass() -> None:
    req = HttpRequest(method="GET", url="https://x", headers={"k": "v"}, body=None)
    assert req.method == "GET"
    assert req.url == "https://x"
    assert req.headers == {"k": "v"}
    assert req.body is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.method = "POST"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


def test_auth_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError, match="abstract"):
        AuthStrategy()  # type: ignore[abstract]


def test_auth_strategy_exposes_five_abstract_methods() -> None:
    expected = {
        "credentials_present",
        "health_check",
        "redact_patterns",
        "apply",
        "client_kwargs",
    }
    assert AuthStrategy.__abstractmethods__ == expected


def test_auth_strategy_subclass_must_implement_all_five() -> None:
    class Partial(AuthStrategy):  # missing methods on purpose
        def credentials_present(self) -> bool:
            return True

    with pytest.raises(TypeError, match="abstract"):
        Partial()  # type: ignore[abstract]
```

- [ ] **Step 2: Run tests — confirm they all fail with `ImportError` (module doesn't exist yet)**

Run: `pixi run test tests/core/test_auth.py -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'kinoforge.core.auth'` (collection-time failure on every test).

- [ ] **Step 3: Create the module**

Create `src/kinoforge/core/auth.py`:

```python
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
    """Mutable representation of an HTTP request for :meth:`AuthStrategy.apply`.

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
```

- [ ] **Step 4: Run tests — confirm all 5 pass**

Run: `pixi run test tests/core/test_auth.py -v 2>&1 | tail -10`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/auth.py tests/core/test_auth.py
git commit -m "feat(core/auth): AuthStrategy ABC + typed boundary objects

5-method abstract base class with HealthResult + HttpRequest frozen
dataclasses. Lays the foundation; concrete strategies (Bearer, GCPSA,
SigV4) follow in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `Bearer` strategy + tests

**Goal:** Concrete strategy for Bearer-token auth from an environment variable. Used by the existing `HostedAPIEngine` (retrofit in Task 8) and by future Replicate / Runway / Luma in a later session.

**Files:**
- Modify: `src/kinoforge/core/auth.py` (append `Bearer` class)
- Modify: `tests/core/test_auth.py` (append tests)

**Acceptance Criteria:**
- [ ] `Bearer(env_var="FAL_KEY")` constructs without error
- [ ] `credentials_present()` returns True when env var is set, False when unset/empty
- [ ] `health_check()` returns ok=True when given a working `health_check_url` (via injected fake `http_get`) and ok=False on HTTP error
- [ ] `redact_patterns()` returns a list containing a regex that matches the actual secret value (NOT the env-var-name)
- [ ] `apply(request)` returns a copy of the request with `Authorization: Bearer <token>` added (header_name + scheme are overridable)
- [ ] `client_kwargs()` returns `{"api_key": <token>}` for SDK consumers (default shape)

**Verify:** `pixi run test tests/core/test_auth.py -v -k bearer 2>&1 | tail -10` → 6 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests for `Bearer`**

Append to `tests/core/test_auth.py`:

```python
# ---------------------------------------------------------------------------
# Bearer strategy
# ---------------------------------------------------------------------------


class _StaticCreds:
    """Inline CredentialProvider double for tests."""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self._mapping = mapping

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def test_bearer_credentials_present_true_when_env_var_set() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "secret-123"}))
    assert strat.credentials_present() is True


def test_bearer_credentials_present_false_when_env_var_unset_or_empty() -> None:
    from kinoforge.core.auth import Bearer

    for value in (None, ""):
        strat = Bearer(env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": value}))
        assert strat.credentials_present() is False


def test_bearer_health_check_ok_against_fake_endpoint() -> None:
    from kinoforge.core.auth import Bearer, HealthResult

    calls: list[tuple[str, dict[str, str]]] = []

    def fake_http_get(url: str, headers: dict[str, str]) -> dict[str, str]:
        calls.append((url, headers))
        return {"account_id": "acc-xyz"}

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": "secret-123"}),
        health_check_url="https://fal.run/health",
        http_get=fake_http_get,
    )
    result = strat.health_check()
    assert isinstance(result, HealthResult)
    assert result.ok is True
    assert result.identity is not None
    assert calls[0][0] == "https://fal.run/health"
    assert calls[0][1]["Authorization"] == "Bearer secret-123"


def test_bearer_health_check_fail_when_env_missing() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(
        env_var="FAL_KEY",
        credential_provider=_StaticCreds({"FAL_KEY": None}),
        health_check_url="https://fal.run/health",
    )
    result = strat.health_check()
    assert result.ok is False
    assert result.identity is None
    assert "missing" in (result.reason or "").lower()


def test_bearer_redact_patterns_matches_actual_secret_value() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "sk-secret-abc-123"}))
    patterns = strat.redact_patterns()
    assert any(p.search("Authorization: Bearer sk-secret-abc-123") for p in patterns)
    # Must NOT redact the env-var-name itself (we name the env var freely in logs).
    assert not any(p.search("FAL_KEY") for p in patterns)


def test_bearer_apply_adds_authorization_header() -> None:
    from kinoforge.core.auth import Bearer, HttpRequest

    strat = Bearer(env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "abc"}))
    req = HttpRequest(method="POST", url="https://fal.run/x", headers={"X-Foo": "y"}, body=b"{}")
    out = strat.apply(req)
    # Original untouched
    assert "Authorization" not in req.headers
    # New request has both old + new headers
    assert out.headers["X-Foo"] == "y"
    assert out.headers["Authorization"] == "Bearer abc"
    assert out.body == b"{}"


def test_bearer_apply_respects_scheme_and_header_name_overrides() -> None:
    from kinoforge.core.auth import Bearer, HttpRequest

    strat = Bearer(
        env_var="HF_TOKEN",
        credential_provider=_StaticCreds({"HF_TOKEN": "xyz"}),
        scheme="Token",
        header_name="X-Api-Key",
    )
    req = HttpRequest(method="GET", url="https://x", headers={}, body=None)
    out = strat.apply(req)
    assert out.headers == {"X-Api-Key": "Token xyz"}


def test_bearer_client_kwargs_returns_api_key_shape() -> None:
    from kinoforge.core.auth import Bearer

    strat = Bearer(env_var="FAL_KEY", credential_provider=_StaticCreds({"FAL_KEY": "abc"}))
    assert strat.client_kwargs() == {"api_key": "abc"}
```

- [ ] **Step 2: Run — confirm failures (ImportError on `Bearer`)**

Run: `pixi run test tests/core/test_auth.py -v -k bearer 2>&1 | tail -10`
Expected: `ImportError` (Bearer is not defined yet).

- [ ] **Step 3: Implement `Bearer` — append to `src/kinoforge/core/auth.py`**

```python
# At top of file, add imports:
from collections.abc import Callable

# Default urllib http_get for Bearer.health_check:
import json as _json
import urllib.error
import urllib.request


def _default_http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return _json.loads(resp.read().decode("utf-8"))


# After AuthStrategy class definition, add:


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
        credential_provider: "CredentialProvider | None" = None,
        scheme: str = "Bearer",
        header_name: str = "Authorization",
        health_check_url: str | None = None,
        http_get: Callable[[str, dict[str, str]], dict[str, Any]] = _default_http_get_json,
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
                When ``None``, ``health_check`` returns ``ok=False`` with a
                reason explaining no probe URL is configured.
            http_get: Injectable HTTP GET seam returning parsed JSON.
        """
        # Local import to avoid a top-level cycle (credentials imports core).
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
        value = self._creds.get(self._env_var)
        return bool(value)

    def health_check(self) -> HealthResult:
        token = self._creds.get(self._env_var)
        if not token:
            return HealthResult(
                ok=False, identity=None, reason=f"missing credential: {self._env_var}"
            )
        if self._health_check_url is None:
            # No probe URL — return ok with identity = the env-var-name as a
            # proxy. Used when health_check is just "key is present".
            return HealthResult(ok=True, identity=self._env_var, reason=None)
        headers = {self._header_name: f"{self._scheme} {token}"}
        try:
            body = self._http_get(self._health_check_url, headers)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=f"HTTP error: {exc}")
        identity = (
            body.get("account_id") or body.get("user") or body.get("id") or self._env_var
        )
        return HealthResult(ok=True, identity=str(identity), reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        token = self._creds.get(self._env_var)
        if not token:
            return []
        return [re.compile(re.escape(token))]

    def apply(self, request: HttpRequest) -> HttpRequest:
        token = self._creds.get(self._env_var)
        if not token:
            raise RuntimeError(f"Bearer.apply called with no token in {self._env_var}")
        new_headers = dict(request.headers)
        new_headers[self._header_name] = f"{self._scheme} {token}"
        return HttpRequest(
            method=request.method, url=request.url, headers=new_headers, body=request.body
        )

    def client_kwargs(self) -> dict[str, Any]:
        token = self._creds.get(self._env_var)
        return {"api_key": token} if token else {}
```

- [ ] **Step 4: Run tests — confirm all 8 Bearer tests pass plus 5 pre-existing**

Run: `pixi run test tests/core/test_auth.py -v 2>&1 | tail -20`
Expected: 13 passed total.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/auth.py tests/core/test_auth.py
git commit -m "feat(core/auth): Bearer strategy + 8 unit tests

Bearer reuses the existing CredentialProvider abstraction for env-var
lookup. health_check pings an optional URL via injected http_get seam;
redact_patterns escapes the actual secret value (never the env-var-name).
apply preserves immutability via dataclasses.replace pattern (returns
copy with merged headers).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: `GCPServiceAccount` strategy + tests (lazy `google.auth` import)

**Goal:** Concrete strategy that mints GCP access tokens via the `google.auth` default chain. Vendor SDK is lazy-imported inside methods to preserve the core-import-ban invariant. Tests use a fake `google.auth` to keep the offline isolation guarantee.

**Files:**
- Modify: `src/kinoforge/core/auth.py` (append `GCPServiceAccount` class)
- Modify: `tests/core/test_auth.py` (append tests with mocked `google.auth`)

**Acceptance Criteria:**
- [ ] `GCPServiceAccount()` constructs with default scopes
- [ ] `credentials_present()` returns True when `GOOGLE_APPLICATION_CREDENTIALS` points to an existing file, False otherwise
- [ ] `health_check()` returns `HealthResult(ok=True, identity=<sa-email>)` when fake `google.auth.default()` returns a working credential; returns `ok=False` when it raises
- [ ] `redact_patterns()` returns at least one regex matching the GCP access-token shape (`ya29.<...>`)
- [ ] `apply(request)` adds an `Authorization: Bearer <minted-token>` header — token comes from the credential's `token` property
- [ ] `client_kwargs()` returns `{"credentials": <google.auth.credentials.Credentials>}`
- [ ] `google.auth` is NOT imported at module top level (verified by Task 6's extended isolation check)

**Verify:** `pixi run test tests/core/test_auth.py -v -k gcp 2>&1 | tail -10` → 6 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests with a fake `google.auth` injected via monkeypatch**

Append to `tests/core/test_auth.py`:

```python
# ---------------------------------------------------------------------------
# GCPServiceAccount strategy
# ---------------------------------------------------------------------------


class _FakeGCPCredentials:
    """Minimal stand-in for ``google.auth.credentials.Credentials``."""

    def __init__(self, token: str, service_account_email: str | None = None) -> None:
        self.token = token
        self.service_account_email = service_account_email
        self.refresh_calls = 0

    def refresh(self, _request: object) -> None:
        self.refresh_calls += 1

    @property
    def expired(self) -> bool:
        return False

    @property
    def valid(self) -> bool:
        return True


def _install_fake_google_auth(
    monkeypatch: pytest.MonkeyPatch,
    credentials: _FakeGCPCredentials | None = None,
    raise_default: Exception | None = None,
) -> None:
    """Install a fake ``google.auth`` module so tests run without the SDK."""
    import sys
    import types

    fake_google = types.ModuleType("google")
    fake_auth = types.ModuleType("google.auth")
    fake_transport = types.ModuleType("google.auth.transport")
    fake_transport_requests = types.ModuleType("google.auth.transport.requests")

    def fake_default(scopes=None, quota_project_id=None):  # type: ignore[no-untyped-def]
        if raise_default is not None:
            raise raise_default
        return (credentials, "fake-project-id")

    fake_auth.default = fake_default  # type: ignore[attr-defined]
    fake_transport_requests.Request = lambda: object()  # type: ignore[attr-defined]

    fake_google.auth = fake_auth  # type: ignore[attr-defined]
    fake_auth.transport = fake_transport  # type: ignore[attr-defined]
    fake_transport.requests = fake_transport_requests  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.auth", fake_auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", fake_transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", fake_transport_requests)


def test_gcp_credentials_present_true_when_adc_file_exists(
    tmp_path, monkeypatch
) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    fake_sa = tmp_path / "sa.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_sa))
    strat = GCPServiceAccount()
    assert strat.credentials_present() is True


def test_gcp_credentials_present_false_when_adc_file_missing(monkeypatch) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    strat = GCPServiceAccount()
    assert strat.credentials_present() is False


def test_gcp_health_check_ok_when_default_returns_creds(monkeypatch) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    creds = _FakeGCPCredentials(
        token="ya29.fake-access-token",
        service_account_email="kinoforge-runner@proj.iam.gserviceaccount.com",
    )
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    result = strat.health_check()
    assert result.ok is True
    assert result.identity == "kinoforge-runner@proj.iam.gserviceaccount.com"
    assert creds.refresh_calls == 1


def test_gcp_health_check_fail_when_default_raises(monkeypatch) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    _install_fake_google_auth(monkeypatch, raise_default=RuntimeError("no ADC"))
    strat = GCPServiceAccount()
    result = strat.health_check()
    assert result.ok is False
    assert "no ADC" in (result.reason or "")


def test_gcp_redact_patterns_matches_access_token_shape() -> None:
    from kinoforge.core.auth import GCPServiceAccount

    strat = GCPServiceAccount()
    patterns = strat.redact_patterns()
    sample = "Authorization: Bearer ya29.abc-def_ghi-123"
    assert any(p.search(sample) for p in patterns)


def test_gcp_apply_adds_authorization_bearer_from_creds(monkeypatch) -> None:
    from kinoforge.core.auth import GCPServiceAccount, HttpRequest

    creds = _FakeGCPCredentials(
        token="ya29.actual-token", service_account_email="kf@proj.iam.gserviceaccount.com"
    )
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    req = HttpRequest(method="POST", url="https://aiplatform...", headers={}, body=b"{}")
    out = strat.apply(req)
    assert out.headers["Authorization"] == "Bearer ya29.actual-token"


def test_gcp_client_kwargs_returns_credentials_dict(monkeypatch) -> None:
    from kinoforge.core.auth import GCPServiceAccount

    creds = _FakeGCPCredentials(token="ya29.x", service_account_email="kf@proj.iam.gserviceaccount.com")
    _install_fake_google_auth(monkeypatch, credentials=creds)
    strat = GCPServiceAccount()
    kwargs = strat.client_kwargs()
    assert kwargs == {"credentials": creds}
```

- [ ] **Step 2: Run — confirm failures (ImportError)**

Run: `pixi run test tests/core/test_auth.py -v -k gcp 2>&1 | tail -10`
Expected: `ImportError: cannot import name 'GCPServiceAccount'`.

- [ ] **Step 3: Implement `GCPServiceAccount` — append to `src/kinoforge/core/auth.py`**

```python
import os


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
        self._scopes = tuple(scopes)
        self._quota_project_id = quota_project_id
        self._impersonation_chain = impersonation_chain
        self._subject = subject

    def credentials_present(self) -> bool:
        adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if adc and os.path.exists(adc):
            return True
        return False

    def health_check(self) -> HealthResult:
        try:
            import google.auth  # lazy
            import google.auth.transport.requests  # lazy

            credentials, _project = google.auth.default(
                scopes=self._scopes, quota_project_id=self._quota_project_id
            )
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=str(exc))
        identity = getattr(credentials, "service_account_email", None) or "gcp-credentials"
        return HealthResult(ok=True, identity=identity, reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        return [self._TOKEN_PATTERN]

    def apply(self, request: HttpRequest) -> HttpRequest:
        import google.auth  # lazy
        import google.auth.transport.requests  # lazy

        credentials, _project = google.auth.default(
            scopes=self._scopes, quota_project_id=self._quota_project_id
        )
        credentials.refresh(google.auth.transport.requests.Request())
        new_headers = dict(request.headers)
        new_headers["Authorization"] = f"Bearer {credentials.token}"
        return HttpRequest(
            method=request.method, url=request.url, headers=new_headers, body=request.body
        )

    def client_kwargs(self) -> dict[str, Any]:
        import google.auth  # lazy

        credentials, _project = google.auth.default(
            scopes=self._scopes, quota_project_id=self._quota_project_id
        )
        return {"credentials": credentials}
```

- [ ] **Step 4: Run tests — confirm all 7 GCP tests pass**

Run: `pixi run test tests/core/test_auth.py -v -k gcp 2>&1 | tail -15`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/auth.py tests/core/test_auth.py
git commit -m "feat(core/auth): GCPServiceAccount strategy + 7 unit tests

Lazy-imports google.auth inside each method to preserve the core-
import-ban invariant. Tests inject a fake google.auth module via
monkeypatch so the suite runs without the SDK installed. Strategy-
only kwargs (impersonation_chain, subject) stored but unused for now;
they wire into google.auth.default in a future per-engine retrofit
if needed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `AWSSigV4` strategy + tests (lazy `boto3` import)

**Goal:** Concrete strategy that signs AWS requests via the `boto3` Session credentials chain. SDK is lazy-imported. Tests use a fake boto3 to keep offline isolation.

**Files:**
- Modify: `src/kinoforge/core/auth.py` (append `AWSSigV4` class)
- Modify: `tests/core/test_auth.py` (append tests with mocked `boto3`)

**Acceptance Criteria:**
- [ ] `AWSSigV4(region_name="us-east-1")` constructs with default service name
- [ ] `credentials_present()` returns True when fake `boto3.Session().get_credentials()` returns non-None, False otherwise
- [ ] `health_check()` calls fake `sts.get_caller_identity()`; returns `HealthResult(ok=True, identity=<arn>)` on success, `ok=False` on failure
- [ ] `redact_patterns()` returns at least two regexes: one matching `AKIA[A-Z0-9]{16}` access keys, one matching SigV4 `Authorization` signature payload
- [ ] `apply(request)` uses `botocore.auth.SigV4Auth` to sign the request; resulting headers include `Authorization: AWS4-HMAC-SHA256 ...` and `X-Amz-Date`
- [ ] `client_kwargs()` returns `{"aws_access_key_id": ..., "aws_secret_access_key": ..., "region_name": ...}` populated from the boto3 session
- [ ] `boto3` is NOT imported at module top level

**Verify:** `pixi run test tests/core/test_auth.py -v -k sigv4 2>&1 | tail -10` → 7 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests with a fake `boto3` injected via monkeypatch**

Append to `tests/core/test_auth.py`:

```python
# ---------------------------------------------------------------------------
# AWSSigV4 strategy
# ---------------------------------------------------------------------------


class _FakeBoto3Credentials:
    def __init__(self, access_key: str, secret_key: str, token: str | None = None) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token

    def get_frozen_credentials(self) -> "_FakeBoto3Credentials":
        return self


class _FakeStsClient:
    def __init__(self, arn: str = "arn:aws:iam::123456789012:user/kinoforge-ci") -> None:
        self._arn = arn

    def get_caller_identity(self) -> dict[str, str]:
        return {"Arn": self._arn, "Account": "123456789012", "UserId": "AIDA..."}


class _FakeBoto3Session:
    def __init__(
        self,
        credentials: _FakeBoto3Credentials | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        self._credentials = credentials
        self.region_name = region_name

    def get_credentials(self) -> _FakeBoto3Credentials | None:
        return self._credentials

    def client(self, service_name: str, region_name: str | None = None) -> object:
        if service_name == "sts":
            return _FakeStsClient()
        raise NotImplementedError(f"FakeBoto3Session: no fake for service {service_name!r}")


def _install_fake_boto3(
    monkeypatch: pytest.MonkeyPatch,
    session: _FakeBoto3Session,
) -> None:
    import sys
    import types

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.Session = lambda profile_name=None: session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)


def test_sigv4_credentials_present_true_when_session_has_creds(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials("AKIATESTKEY", "secret")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    assert strat.credentials_present() is True


def test_sigv4_credentials_present_false_when_session_has_no_creds(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4

    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=None))
    strat = AWSSigV4(region_name="us-east-1")
    assert strat.credentials_present() is False


def test_sigv4_health_check_ok_via_sts_get_caller_identity(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials("AKIATESTKEY", "secret")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    result = strat.health_check()
    assert result.ok is True
    assert result.identity == "arn:aws:iam::123456789012:user/kinoforge-ci"


def test_sigv4_health_check_fail_when_no_creds(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4

    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=None))
    strat = AWSSigV4(region_name="us-east-1")
    result = strat.health_check()
    assert result.ok is False
    assert "no AWS credentials" in (result.reason or "")


def test_sigv4_redact_patterns_includes_access_key_and_authz_signature() -> None:
    from kinoforge.core.auth import AWSSigV4

    strat = AWSSigV4(region_name="us-east-1")
    patterns = strat.redact_patterns()
    assert any(p.search("AKIAIOSFODNN7EXAMPLE") for p in patterns)
    assert any(
        p.search("AWS4-HMAC-SHA256 Credential=AKIA.../20260607/us-east-1/bedrock/aws4_request")
        for p in patterns
    )


def test_sigv4_apply_signs_request_with_authorization_and_amz_date(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4, HttpRequest

    creds = _FakeBoto3Credentials("AKIATESTKEY", "VerySecretKey123")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1", service_name="bedrock-runtime")
    req = HttpRequest(
        method="POST",
        url="https://bedrock-runtime.us-east-1.amazonaws.com/async-invoke",
        headers={"Content-Type": "application/json"},
        body=b'{"foo": "bar"}',
    )
    out = strat.apply(req)
    assert out.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIATESTKEY/")
    assert "X-Amz-Date" in out.headers


def test_sigv4_client_kwargs_returns_aws_credential_dict(monkeypatch) -> None:
    from kinoforge.core.auth import AWSSigV4

    creds = _FakeBoto3Credentials("AKIATESTKEY", "VerySecretKey123", token="session-token-abc")
    _install_fake_boto3(monkeypatch, _FakeBoto3Session(credentials=creds))
    strat = AWSSigV4(region_name="us-east-1")
    kwargs = strat.client_kwargs()
    assert kwargs["aws_access_key_id"] == "AKIATESTKEY"
    assert kwargs["aws_secret_access_key"] == "VerySecretKey123"
    assert kwargs["aws_session_token"] == "session-token-abc"
    assert kwargs["region_name"] == "us-east-1"
```

- [ ] **Step 2: Run — confirm failures (ImportError)**

Run: `pixi run test tests/core/test_auth.py -v -k sigv4 2>&1 | tail -10`
Expected: `ImportError: cannot import name 'AWSSigV4'`.

- [ ] **Step 3: Implement `AWSSigV4` — append to `src/kinoforge/core/auth.py`**

```python
import datetime as _dt
import hashlib as _hashlib
import hmac as _hmac
import urllib.parse as _urlparse


class AWSSigV4(AuthStrategy):
    """AWS request signing via the ``boto3`` Session credential chain.

    Used by NovaReelEngine (Layer 3) and any future Bedrock integrations.
    SDK is lazy-imported inside method bodies.
    """

    # AWS IAM access-key shape (long-term keys + sts session keys).
    _ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
    # SigV4 Authorization header signature.
    _AUTHZ_PATTERN = re.compile(
        r"AWS4-HMAC-SHA256\s+Credential=[A-Z0-9]+/[0-9]+/[a-z0-9-]+/[a-z0-9-]+/aws4_request[^,\s]*"
    )
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
        self._region_name = region_name
        self._service_name = service_name
        self._profile_name = profile_name
        self._assume_role_arn = assume_role_arn
        self._assume_role_external_id = assume_role_external_id

    def _session(self) -> Any:
        import boto3  # lazy

        return boto3.Session(profile_name=self._profile_name)

    def credentials_present(self) -> bool:
        try:
            return self._session().get_credentials() is not None
        except Exception:  # noqa: BLE001
            return False

    def health_check(self) -> HealthResult:
        session = self._session()
        if session.get_credentials() is None:
            return HealthResult(ok=False, identity=None, reason="no AWS credentials in chain")
        try:
            sts = session.client("sts", region_name=self._region_name)
            ident = sts.get_caller_identity()
        except Exception as exc:  # noqa: BLE001
            return HealthResult(ok=False, identity=None, reason=str(exc))
        return HealthResult(ok=True, identity=str(ident["Arn"]), reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        return [self._ACCESS_KEY_PATTERN, self._AUTHZ_PATTERN, self._SESSION_TOKEN_PATTERN]

    def apply(self, request: HttpRequest) -> HttpRequest:
        """Sign a request with SigV4 and return a new HttpRequest.

        We do the signing ourselves rather than going through botocore.auth
        so the seam stays SDK-version-independent and lazy.
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

        amz_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        date_stamp = amz_date[:8]
        host = parsed.netloc

        headers_lc = {k.lower(): v for k, v in request.headers.items()}
        headers_lc.setdefault("host", host)
        headers_lc["x-amz-date"] = amz_date
        headers_lc["x-amz-content-sha256"] = payload_hash
        if frozen.token:
            headers_lc["x-amz-security-token"] = frozen.token

        signed_headers = ";".join(sorted(headers_lc))
        canonical_headers = (
            "".join(f"{k}:{headers_lc[k].strip()}\n" for k in sorted(headers_lc))
        )

        canonical_request = "\n".join(
            [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
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
```

- [ ] **Step 4: Run tests — confirm all 7 SigV4 tests pass**

Run: `pixi run test tests/core/test_auth.py -v -k sigv4 2>&1 | tail -15`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/auth.py tests/core/test_auth.py
git commit -m "feat(core/auth): AWSSigV4 strategy + 7 unit tests

Implements SigV4 signing directly (hashlib + hmac stdlib) rather than
going through botocore.auth, keeping the seam SDK-version-independent.
boto3 is lazy-imported only for session/credentials resolution; tests
inject a fake boto3 module via monkeypatch so the suite runs without
the SDK installed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `build_auth_strategy` registry + `UnknownAdapter` integration

**Goal:** Map a YAML `auth:` block (with `strategy:` discriminator) to a concrete `AuthStrategy` instance. Engines call `build_auth_strategy(cfg_dict)` at config-load time. Unknown strategy names raise the existing `UnknownAdapter` error.

**Files:**
- Modify: `src/kinoforge/core/auth.py` (append registry + factory)
- Modify: `tests/core/test_auth.py` (append registry tests)

**Acceptance Criteria:**
- [ ] `build_auth_strategy({"strategy": "bearer", "env_var": "FAL_KEY"})` returns a `Bearer` instance
- [ ] `build_auth_strategy({"strategy": "gcp_service_account"})` returns a `GCPServiceAccount` instance
- [ ] `build_auth_strategy({"strategy": "aws_sigv4", "region_name": "us-east-1"})` returns an `AWSSigV4` instance
- [ ] `build_auth_strategy({"strategy": "unknown_name"})` raises `UnknownAdapter` with the unknown name in the message
- [ ] `build_auth_strategy({})` (no strategy key) raises `KeyError` with a helpful message
- [ ] Unknown strategy-specific kwargs pass through and raise `TypeError` from the concrete strategy `__init__` (caught and re-raised by the factory for context)

**Verify:** `pixi run test tests/core/test_auth.py -v -k registry 2>&1 | tail -10` → 6 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests for the registry**

Append to `tests/core/test_auth.py`:

```python
# ---------------------------------------------------------------------------
# build_auth_strategy registry
# ---------------------------------------------------------------------------


def test_registry_builds_bearer() -> None:
    from kinoforge.core.auth import Bearer, build_auth_strategy

    strat = build_auth_strategy({"strategy": "bearer", "env_var": "FAL_KEY"})
    assert isinstance(strat, Bearer)


def test_registry_builds_gcp_service_account() -> None:
    from kinoforge.core.auth import GCPServiceAccount, build_auth_strategy

    strat = build_auth_strategy({"strategy": "gcp_service_account"})
    assert isinstance(strat, GCPServiceAccount)


def test_registry_builds_aws_sigv4() -> None:
    from kinoforge.core.auth import AWSSigV4, build_auth_strategy

    strat = build_auth_strategy({"strategy": "aws_sigv4", "region_name": "us-east-1"})
    assert isinstance(strat, AWSSigV4)


def test_registry_unknown_strategy_raises_unknown_adapter() -> None:
    from kinoforge.core.auth import build_auth_strategy
    from kinoforge.core.errors import UnknownAdapter

    with pytest.raises(UnknownAdapter, match="not_a_real_strategy"):
        build_auth_strategy({"strategy": "not_a_real_strategy"})


def test_registry_missing_strategy_key_raises_keyerror() -> None:
    from kinoforge.core.auth import build_auth_strategy

    with pytest.raises(KeyError, match="strategy"):
        build_auth_strategy({"env_var": "FAL_KEY"})


def test_registry_passes_through_strategy_specific_kwargs() -> None:
    from kinoforge.core.auth import Bearer, build_auth_strategy

    strat = build_auth_strategy(
        {
            "strategy": "bearer",
            "env_var": "HF_TOKEN",
            "scheme": "Token",
            "header_name": "X-Api-Key",
        }
    )
    assert isinstance(strat, Bearer)
    assert strat._scheme == "Token"
    assert strat._header_name == "X-Api-Key"
```

- [ ] **Step 2: Run — confirm failures (ImportError for `build_auth_strategy`)**

Run: `pixi run test tests/core/test_auth.py -v -k registry 2>&1 | tail -10`
Expected: ImportError on `build_auth_strategy`.

- [ ] **Step 3: Implement the registry — append to `src/kinoforge/core/auth.py`**

```python
from kinoforge.core.errors import UnknownAdapter


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
            "auth spec must include a 'strategy' key; got "
            f"keys: {sorted(spec.keys())}"
        )
    name = spec["strategy"]
    cls = _REGISTRY.get(name)
    if cls is None:
        raise UnknownAdapter(
            f"unknown auth strategy: {name!r} "
            f"(registered: {sorted(_REGISTRY)})"
        )
    kwargs = {k: v for k, v in spec.items() if k != "strategy"}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise TypeError(
            f"failed to construct auth strategy {name!r}: {exc}"
        ) from exc
```

- [ ] **Step 4: Run tests — confirm all 6 registry tests pass**

Run: `pixi run test tests/core/test_auth.py -v -k registry 2>&1 | tail -15`
Expected: 6 passed.

- [ ] **Step 5: Run the WHOLE test_auth.py suite — confirm all pass**

Run: `pixi run test tests/core/test_auth.py -v 2>&1 | tail -20`
Expected: ~33 passed total (5 ABC + 8 Bearer + 7 GCP + 7 SigV4 + 6 registry).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/auth.py tests/core/test_auth.py
git commit -m "feat(core/auth): build_auth_strategy registry + 6 unit tests

Maps YAML auth: discriminator to concrete strategy class. Unknown
names raise UnknownAdapter; missing 'strategy' key raises KeyError;
strategy-specific TypeError gets re-raised with context.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: ABC stable-surface invariant test + baseline + extended subprocess-isolation

**Goal:** Lock the 5 ABC method signatures against silent drift via a checked-in JSON baseline. Extend the existing subprocess-isolation test to scan for vendor-SDK leaks (`boto3`, `google.auth`) so the lazy-import discipline can't regress silently.

**Files:**
- Create: `tests/fixtures/auth_strategy_baseline.json`
- Modify: `tests/test_core_invariant.py` (add two new tests)

**Acceptance Criteria:**
- [ ] `tests/fixtures/auth_strategy_baseline.json` exists and contains JSON keyed by method name to signature string
- [ ] New test `test_auth_strategy_abc_stable_surface` passes against the current baseline
- [ ] Modifying any ABC method signature without updating the baseline causes the test to FAIL with an actionable message
- [ ] New test `test_core_auth_does_not_leak_sdk_imports` passes (asserts no `boto3` / `google.auth` / `botocore` in `sys.modules` after subprocess imports `kinoforge.core.auth`)
- [ ] The existing 6 invariant tests still pass

**Verify:** `pixi run test tests/test_core_invariant.py -v 2>&1 | tail -15` → 8 tests passed (6 pre-existing + 2 new)

**Steps:**

- [ ] **Step 1: Write the failing baseline test**

Append to `tests/test_core_invariant.py`:

```python
# ---------------------------------------------------------------------------
# AC 7: AuthStrategy ABC stable surface
# ---------------------------------------------------------------------------


def test_auth_strategy_abc_stable_surface() -> None:
    """Lock AuthStrategy ABC public method signatures against silent drift.

    To intentionally evolve the ABC, regenerate
    tests/fixtures/auth_strategy_baseline.json in the same commit:

        python -c "
        import inspect, json
        from pathlib import Path
        from kinoforge.core.auth import AuthStrategy
        sigs = {n: str(inspect.signature(getattr(AuthStrategy, n)))
                for n in ('credentials_present', 'health_check',
                          'redact_patterns', 'apply', 'client_kwargs')}
        Path('tests/fixtures/auth_strategy_baseline.json').write_text(
            json.dumps(sigs, indent=2) + '\n')
        "
    """
    import inspect
    import json

    from kinoforge.core.auth import AuthStrategy

    actual = {
        name: str(inspect.signature(getattr(AuthStrategy, name)))
        for name in (
            "credentials_present",
            "health_check",
            "redact_patterns",
            "apply",
            "client_kwargs",
        )
    }
    baseline_path = Path(__file__).parent / "fixtures" / "auth_strategy_baseline.json"
    expected = json.loads(baseline_path.read_text())

    assert actual == expected, (
        f"AuthStrategy ABC drifted from baseline.\n"
        f"  expected: {json.dumps(expected, indent=2)}\n"
        f"  actual:   {json.dumps(actual, indent=2)}\n"
        f"If intentional, regenerate {baseline_path} in the same commit."
    )


# ---------------------------------------------------------------------------
# AC 8: core/auth.py does not eagerly load vendor SDKs
# ---------------------------------------------------------------------------


def test_core_auth_does_not_leak_sdk_imports() -> None:
    """Lazy SDK imports — boto3 / google.auth / botocore stay out of sys.modules.

    Verifies that importing kinoforge.core.auth in a fresh interpreter does
    NOT pull in any of the vendor SDKs the concrete strategies depend on.
    Construction of strategies is also exercised to ensure lazy paths fire
    only when methods are called, not at instantiation.
    """
    script = (
        "import kinoforge.core.auth as a; "
        "a.Bearer(env_var='FAL_KEY'); "
        "a.GCPServiceAccount(); "
        "a.AWSSigV4(region_name='us-east-1'); "
        "import sys; "
        "print('|'.join(m for m in sys.modules "
        "if m == 'boto3' or m.startswith('botocore') "
        "or m == 'google.auth' or m.startswith('google.cloud')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    leaked = result.stdout.strip()
    if leaked:
        offending = "\n  ".join(leaked.split("|"))
        raise AssertionError(
            f"core.auth import leaked vendor SDK modules into sys.modules:\n  {offending}"
        )
```

- [ ] **Step 2: Run — confirm failure (baseline file does not exist yet)**

Run: `pixi run test tests/test_core_invariant.py::test_auth_strategy_abc_stable_surface -v 2>&1 | tail -10`
Expected: `FileNotFoundError` on `auth_strategy_baseline.json`.

- [ ] **Step 3: Generate the baseline file**

Run:

```bash
mkdir -p tests/fixtures
python -c "
import inspect, json
from pathlib import Path
from kinoforge.core.auth import AuthStrategy
sigs = {n: str(inspect.signature(getattr(AuthStrategy, n)))
        for n in ('credentials_present', 'health_check',
                  'redact_patterns', 'apply', 'client_kwargs')}
Path('tests/fixtures/auth_strategy_baseline.json').write_text(
    json.dumps(sigs, indent=2) + '\n')
print(Path('tests/fixtures/auth_strategy_baseline.json').read_text())
"
```

Expected output: JSON object with 5 keys, each value `'(self) -> ...'` or `'(self, request: HttpRequest) -> HttpRequest'`.

- [ ] **Step 4: Run both new tests — confirm pass**

Run: `pixi run test tests/test_core_invariant.py -v 2>&1 | tail -15`
Expected: 8 passed (6 pre-existing + 2 new).

- [ ] **Step 5: Verify drift detection works**

Manually edit `src/kinoforge/core/auth.py`: change `def credentials_present(self) -> bool:` to `def credentials_present(self, *, force: bool = False) -> bool:` (TEMPORARY change just to verify the test catches drift).

Run: `pixi run test tests/test_core_invariant.py::test_auth_strategy_abc_stable_surface -v 2>&1 | tail -15`
Expected: FAIL — the assertion message names the new signature vs the baseline.

Revert the change:

```bash
git checkout -- src/kinoforge/core/auth.py
```

Run: `pixi run test tests/test_core_invariant.py::test_auth_strategy_abc_stable_surface -v 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/auth_strategy_baseline.json tests/test_core_invariant.py
git commit -m "test(invariant): lock AuthStrategy ABC stable surface + SDK-leak check

Two new tests in test_core_invariant.py:
- test_auth_strategy_abc_stable_surface — diffs the public ABC's
  inspect.signature against a checked-in baseline file. Intentional
  evolution requires regenerating the baseline in the same commit.
- test_core_auth_does_not_leak_sdk_imports — extends the subprocess-
  isolation pattern (already covering adapter modules) to scan for
  boto3 / google.auth / botocore leaks after importing core.auth.
  Guards the lazy-import discipline of GCPServiceAccount + AWSSigV4.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `FakeAuthStrategy` shared test fixture

**Goal:** Ship a complete, no-network `FakeAuthStrategy` that future engine tests (Layers 2 + 3) and the probe-tool tests (Task 9) can use to exercise the ABC from the consumer side without instantiating a real strategy.

**Files:**
- Create: `tests/_fixtures/__init__.py`
- Create: `tests/_fixtures/fake_auth.py`
- Create: `tests/_fixtures/test_fake_auth.py`

**Acceptance Criteria:**
- [ ] `FakeAuthStrategy()` constructs without error
- [ ] All 5 ABC methods are implemented (not stubs)
- [ ] `credentials_present()` returns True by default; configurable via constructor kwarg
- [ ] `health_check()` returns `HealthResult(ok=True, identity="fake-identity")` by default
- [ ] `redact_patterns()` returns a list with one regex matching the configured fake token
- [ ] `apply(request)` adds `Authorization: Fake <fake-token>` header
- [ ] `client_kwargs()` returns `{"fake_token": "<token>"}`
- [ ] Can simulate failure: `FakeAuthStrategy(credentials_ok=False)` returns False / failure on health check / empty redact_patterns

**Verify:** `pixi run test tests/_fixtures/test_fake_auth.py -v 2>&1 | tail -10` → 8 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/_fixtures/test_fake_auth.py`:

```python
"""Tests for the FakeAuthStrategy shared test fixture."""

from __future__ import annotations

import re

import pytest

from kinoforge.core.auth import AuthStrategy, HealthResult, HttpRequest
from tests._fixtures.fake_auth import FakeAuthStrategy


def test_fake_auth_strategy_is_auth_strategy_subclass() -> None:
    assert issubclass(FakeAuthStrategy, AuthStrategy)


def test_fake_auth_strategy_constructs_with_defaults() -> None:
    strat = FakeAuthStrategy()
    assert strat.credentials_present() is True


def test_fake_auth_credentials_present_configurable() -> None:
    assert FakeAuthStrategy(credentials_ok=True).credentials_present() is True
    assert FakeAuthStrategy(credentials_ok=False).credentials_present() is False


def test_fake_auth_health_check_ok_default() -> None:
    r = FakeAuthStrategy().health_check()
    assert isinstance(r, HealthResult)
    assert r.ok is True
    assert r.identity == "fake-identity"


def test_fake_auth_health_check_failure_when_creds_missing() -> None:
    r = FakeAuthStrategy(credentials_ok=False).health_check()
    assert r.ok is False
    assert r.identity is None
    assert r.reason is not None


def test_fake_auth_redact_patterns_matches_configured_token() -> None:
    strat = FakeAuthStrategy(fake_token="fake-sk-xyz")
    patterns = strat.redact_patterns()
    assert any(p.search("Authorization: Fake fake-sk-xyz") for p in patterns)


def test_fake_auth_apply_adds_authorization_header() -> None:
    strat = FakeAuthStrategy(fake_token="t-1")
    req = HttpRequest(method="GET", url="https://x", headers={}, body=None)
    out = strat.apply(req)
    assert out.headers == {"Authorization": "Fake t-1"}


def test_fake_auth_client_kwargs_returns_fake_token() -> None:
    strat = FakeAuthStrategy(fake_token="abc")
    assert strat.client_kwargs() == {"fake_token": "abc"}
```

- [ ] **Step 2: Run — confirm failures (modules don't exist)**

Run: `pixi run test tests/_fixtures/test_fake_auth.py -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'tests._fixtures'`.

- [ ] **Step 3: Implement the fixture module**

Create `tests/_fixtures/__init__.py`:

```python
"""Shared no-network test fixtures used across kinoforge test suites."""
```

Create `tests/_fixtures/fake_auth.py`:

```python
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
        self._creds_ok = credentials_ok
        self._token = fake_token
        self._identity = fake_identity

    def credentials_present(self) -> bool:
        return self._creds_ok

    def health_check(self) -> HealthResult:
        if not self._creds_ok:
            return HealthResult(ok=False, identity=None, reason="fake credentials disabled")
        return HealthResult(ok=True, identity=self._identity, reason=None)

    def redact_patterns(self) -> list[re.Pattern[str]]:
        if not self._creds_ok:
            return []
        return [re.compile(re.escape(self._token))]

    def apply(self, request: HttpRequest) -> HttpRequest:
        new_headers = dict(request.headers)
        new_headers["Authorization"] = f"Fake {self._token}"
        return HttpRequest(
            method=request.method,
            url=request.url,
            headers=new_headers,
            body=request.body,
        )

    def client_kwargs(self) -> dict[str, Any]:
        return {"fake_token": self._token}
```

- [ ] **Step 4: Run tests — confirm 8 pass**

Run: `pixi run test tests/_fixtures/test_fake_auth.py -v 2>&1 | tail -15`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/_fixtures/__init__.py tests/_fixtures/fake_auth.py tests/_fixtures/test_fake_auth.py
git commit -m "test(_fixtures): add FakeAuthStrategy shared no-network fixture

Ships a complete FakeAuthStrategy implementing the full AuthStrategy
contract. Layer 2 + Layer 3 engine tests + probe-tool tests will use
this to exercise the ABC from the consumer side without instantiating
a real strategy. Configurable failure mode via credentials_ok=False.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `HostedAPIEngine` retrofit with backward-compat

**Goal:** Add an optional `auth_strategy: AuthStrategy | None = None` kwarg to `HostedAPIEngine.__init__`. When None, derive a `Bearer` from `cfg.api_key_env` at `provision()` time so every existing config continues to work bit-for-bit. New configs can pass an explicit strategy.

**Files:**
- Modify: `src/kinoforge/engines/hosted/__init__.py` (modify `__init__` signature; modify `provision()` to use the strategy)
- Modify: `tests/engines/test_hosted.py` (existing tests stay green; add 2 new tests for explicit strategy)

**Pre-implementation grep** (run first; confirm every call site is covered by the retrofit):

```bash
rg -n "HostedAPIEngine\(" src/ tests/ examples/ --type py
```

Expected sites (must all keep working unchanged):
- `src/kinoforge/engines/hosted/__init__.py:738` — factory closure
- `tests/engines/test_hosted.py:128, 710, 777, 969, 989, 1039, 1087` — 7 sites
- `tests/core/test_engine_abc_render_provision.py:54` — 1 site

Total: 9 construction sites. All currently pass `creds=...` or no auth-related kwargs. The retrofit must preserve all 9.

**Acceptance Criteria:**
- [ ] `HostedAPIEngine(creds=<CredentialProvider>)` still works (no auth_strategy kwarg) — derives default Bearer
- [ ] `HostedAPIEngine(auth_strategy=Bearer(env_var="FAL_KEY"))` works — uses explicit strategy
- [ ] All 9 pre-existing construction sites pass their existing assertions unchanged
- [ ] `provision()` ends up calling `self._auth.credentials_present()` before anything else and raises `AuthError` when False
- [ ] New tests: one for explicit `auth_strategy=Bearer(...)`, one verifying the default-Bearer path is exercised when only `creds=` is passed
- [ ] Full `test_hosted.py` suite stays green (pre-existing test count + 2 new)

**Verify:** `pixi run test tests/engines/test_hosted.py -v 2>&1 | tail -10` → all green; new count = previous + 2

**Steps:**

- [ ] **Step 1: Grep all construction sites**

Run: `rg -n "HostedAPIEngine\(" src/ tests/ examples/ --type py 2>&1`
Expected: 9 lines as listed above.

- [ ] **Step 2: Write the failing new tests FIRST**

Append to `tests/engines/test_hosted.py`:

```python
# ---------------------------------------------------------------------------
# Layer 1 retrofit — auth_strategy kwarg
# ---------------------------------------------------------------------------


def test_hosted_engine_accepts_explicit_auth_strategy() -> None:
    """Passing auth_strategy=Bearer(...) wires the strategy directly."""
    from kinoforge.core.auth import Bearer
    from kinoforge.engines.hosted import HostedAPIEngine

    class _Creds:
        def get(self, key: str) -> str | None:
            return "secret-explicit" if key == "FAL_KEY" else None

    strat = Bearer(env_var="FAL_KEY", credential_provider=_Creds())
    engine = HostedAPIEngine(auth_strategy=strat)
    # The engine should expose the strategy at a stable attribute
    # for downstream layers (recording-seam, probe-tool) to read.
    assert engine._auth is strat


def test_hosted_engine_default_derives_bearer_from_cfg_api_key_env() -> None:
    """When auth_strategy is omitted, provision() derives Bearer from cfg.api_key_env."""
    from kinoforge.engines.hosted import HostedAPIEngine
    import os

    # Set the env var so credentials_present() returns True.
    old = os.environ.get("KINOFORGE_TEST_API_KEY")
    os.environ["KINOFORGE_TEST_API_KEY"] = "secret-default"
    try:
        engine = HostedAPIEngine()  # no auth_strategy
        cfg = {
            "engine": {
                "hosted": {
                    "endpoint": "https://x/predict",
                    "api_key_env": "KINOFORGE_TEST_API_KEY",
                    "health_url": "https://x/health",
                }
            }
        }
        # We don't actually call provision (it would hit a real URL); instead
        # we assert the lazy resolver materialises a Bearer with the right env.
        from kinoforge.core.auth import Bearer

        derived = engine._resolve_auth(cfg)
        assert isinstance(derived, Bearer)
        assert derived._env_var == "KINOFORGE_TEST_API_KEY"
    finally:
        if old is None:
            del os.environ["KINOFORGE_TEST_API_KEY"]
        else:
            os.environ["KINOFORGE_TEST_API_KEY"] = old
```

- [ ] **Step 3: Run new tests — confirm failures**

Run: `pixi run test tests/engines/test_hosted.py::test_hosted_engine_accepts_explicit_auth_strategy tests/engines/test_hosted.py::test_hosted_engine_default_derives_bearer_from_cfg_api_key_env -v 2>&1 | tail -10`
Expected: `AttributeError: 'HostedAPIEngine' object has no attribute '_auth'` (or `'_resolve_auth'`).

- [ ] **Step 4: Apply the retrofit to `HostedAPIEngine.__init__`**

In `src/kinoforge/engines/hosted/__init__.py`:

Modify the `__init__` signature — ADD a new kwarg at the end (after `declared_flags_map`):

```python
def __init__(
    self,
    *,
    creds: CredentialProvider | None = None,
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]] = _urllib_post_json,
    http_get: Callable[[str], dict[str, Any]] = _urllib_get_json,
    http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
    ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
    sleep: Callable[[float], None] = time.sleep,
    probe_profile: ModelProfile = _DEFAULT_PROBE,
    declared_flags_map: dict[str, dict[str, bool]] | None = None,
    auth_strategy: "AuthStrategy | None" = None,  # NEW
) -> None:
```

After the existing initialisation block (after the `self._prompt_body_key = "prompt"` line), append:

```python
    # Layer 1 retrofit: optional explicit AuthStrategy. When omitted, the
    # engine derives a Bearer at provision() time from cfg.api_key_env.
    # This preserves bit-for-bit backward compatibility with every pre-
    # Layer-1 construction site.
    self._auth: "AuthStrategy | None" = auth_strategy
```

Add the lazy import at the top of `__init__.py` under TYPE_CHECKING (to avoid a circular import at module load):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.auth import AuthStrategy
```

Add a new method on `HostedAPIEngine` right after `__init__` and before any other method:

```python
    def _resolve_auth(self, cfg: dict[str, Any]) -> "AuthStrategy":
        """Return the active auth strategy, deriving a default if needed.

        Resolution order:
        1. If ``auth_strategy`` was passed to ``__init__``, use it.
        2. If ``cfg["engine"]["hosted"]["auth"]`` is a mapping, build via
           :func:`kinoforge.core.auth.build_auth_strategy`.
        3. Otherwise, derive ``Bearer(env_var=cfg["engine"]["hosted"]["api_key_env"])``.
        """
        if self._auth is not None:
            return self._auth
        from kinoforge.core.auth import Bearer, build_auth_strategy

        hosted_cfg = cfg.get("engine", {}).get("hosted", {})
        auth_spec = hosted_cfg.get("auth")
        if isinstance(auth_spec, dict):
            return build_auth_strategy(auth_spec)
        env_var = hosted_cfg.get("api_key_env")
        if not env_var:
            from kinoforge.core.errors import AuthError

            raise AuthError(
                "HostedAPIEngine: no auth_strategy provided and "
                "cfg.engine.hosted.api_key_env is missing"
            )
        return Bearer(env_var=env_var, credential_provider=self._creds)
```

Modify the existing `provision()` method on `HostedAPIEngine` to call `_resolve_auth(cfg)` and check `credentials_present()` before the existing health-URL ping. Find the existing `provision()` body and at the very top of the method add:

```python
        auth = self._resolve_auth(cfg)
        if not auth.credentials_present():
            from kinoforge.core.errors import AuthError

            raise AuthError(
                f"HostedAPIEngine: credentials not present (strategy={type(auth).__name__})"
            )
        # Stash for downstream code paths (backend, result, fixtures) to use.
        self._auth = auth
```

(The exact insertion point is the first statement inside `provision()` — replace any earlier "check credentials" logic with the strategy-based check.)

- [ ] **Step 5: Run the new tests — confirm pass**

Run: `pixi run test tests/engines/test_hosted.py::test_hosted_engine_accepts_explicit_auth_strategy tests/engines/test_hosted.py::test_hosted_engine_default_derives_bearer_from_cfg_api_key_env -v 2>&1 | tail -10`
Expected: 2 passed.

- [ ] **Step 6: Run the WHOLE `test_hosted.py` suite — confirm zero regressions**

Run: `pixi run test tests/engines/test_hosted.py -v 2>&1 | tail -15`
Expected: ALL passing (pre-existing count + 2 new).

- [ ] **Step 7: Run the full repo test suite — confirm no other tests regressed**

Run: `pixi run test 2>&1 | tail -10`
Expected: all green, full count = previous count + (Task 1–7 new tests).

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git commit -m "feat(engines/hosted): retrofit HostedAPIEngine to consume AuthStrategy

Adds optional auth_strategy kwarg. When None, provision() derives a
Bearer from cfg.engine.hosted.api_key_env (backward-compat with every
pre-Layer-1 construction site). When cfg.engine.hosted.auth is a
mapping, it goes through build_auth_strategy so future configs can
declare any registered strategy via YAML.

Verified: all 9 pre-Layer-1 HostedAPIEngine construction sites unchanged.
Two new tests cover the explicit-strategy and default-Bearer paths.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: `tools/probe_hosted.py` preflight tool + tests

**Goal:** Ship a preflight tool that walks every configured `AuthStrategy` in a kinoforge config and runs `credentials_present` + `health_check`. Atomic snapshot write to `.gcp/<config>-snapshot.json` or `.aws/<config>-snapshot.json` mirroring Phase 39's pattern. Used by Layer 2 / 3 live smokes as a fail-fast gate.

**Files:**
- Create: `tools/probe_hosted.py`
- Create: `tests/test_probe_hosted.py`
- Modify: `pixi.toml` (add `probe-hosted` task)

**Acceptance Criteria:**
- [ ] `python -m tools.probe_hosted --config examples/configs/hosted.yaml` exits 0 when all configured strategies pass health-check (mocked in tests)
- [ ] Exits non-zero when any strategy fails credentials_present or health_check
- [ ] Prints one line per strategy: `PASS strategy=<name> identity=<id>` or `FAIL strategy=<name> reason=<reason>`
- [ ] Writes atomic snapshot: `tools/_snapshots/probe-<config-stem>.json` (tmp + rename pattern from Phase 39)
- [ ] Snapshot includes `{git_sha, captured_at, strategies: [{name, ok, identity?, reason?}]}`
- [ ] `pixi run probe-hosted -- --config <path>` works
- [ ] All unit tests pass against `FakeAuthStrategy`

**Verify:** `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -10` → 6 tests passed

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/test_probe_hosted.py`:

```python
"""Unit tests for tools/probe_hosted.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._fixtures.fake_auth import FakeAuthStrategy
from tools.probe_hosted import (
    ProbeResult,
    probe_strategies,
    write_snapshot,
)


def test_probe_strategies_all_pass() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(fake_identity="id-1")),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert all(r.ok for r in results)
    assert [r.name for r in results] == ["hosted", "veo"]
    assert [r.identity for r in results] == ["id-1", "id-2"]


def test_probe_strategies_fails_on_missing_creds() -> None:
    strategies = [
        ("hosted", FakeAuthStrategy(credentials_ok=False)),
        ("veo", FakeAuthStrategy(fake_identity="id-2")),
    ]
    results = probe_strategies(strategies)
    assert results[0].ok is False
    assert "missing" in (results[0].reason or "").lower() or "disabled" in (results[0].reason or "").lower()
    assert results[1].ok is True


def test_write_snapshot_atomic_rename(tmp_path: Path, monkeypatch) -> None:
    results = [
        ProbeResult(name="hosted", ok=True, identity="id-1", reason=None),
        ProbeResult(name="veo", ok=False, identity=None, reason="boom"),
    ]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    body = json.loads(snap_path.read_text())
    assert body["git_sha"] == "deadbeef"
    assert body["strategies"] == [
        {"name": "hosted", "ok": True, "identity": "id-1", "reason": None},
        {"name": "veo", "ok": False, "identity": None, "reason": "boom"},
    ]
    assert "captured_at" in body


def test_write_snapshot_uses_tmp_then_rename(tmp_path: Path, monkeypatch) -> None:
    """No partial snapshot survives a crash mid-write."""
    results = [ProbeResult(name="hosted", ok=True, identity="id", reason=None)]
    snap_path = tmp_path / "probe-test.json"
    monkeypatch.setattr("tools.probe_hosted._git_sha", lambda: "deadbeef")
    write_snapshot(snap_path, results)
    # Tmp file must have been removed (rename = atomic).
    tmp_files = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert tmp_files == []


def test_probe_exit_code_zero_on_all_pass(tmp_path: Path, monkeypatch) -> None:
    """Run the tool entrypoint with FakeAuthStrategy injected."""
    from tools.probe_hosted import run

    strategies = [("hosted", FakeAuthStrategy())]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code == 0


def test_probe_exit_code_nonzero_on_any_fail(tmp_path: Path, monkeypatch) -> None:
    from tools.probe_hosted import run

    strategies = [
        ("hosted", FakeAuthStrategy()),
        ("veo", FakeAuthStrategy(credentials_ok=False)),
    ]
    exit_code = run(strategies, snapshot_path=tmp_path / "probe.json")
    assert exit_code != 0
```

- [ ] **Step 2: Run — confirm failures (modules don't exist)**

Run: `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'tools.probe_hosted'`.

- [ ] **Step 3: Implement the tool**

Create `tools/probe_hosted.py`:

```python
"""Preflight tool — walk every configured AuthStrategy + probe health.

Mirrors tools/preflight.py (Phase 39) but for hosted-engine auth. Used by
Layer 2 / 3 live smokes as a fail-fast gate before any cloud call.

Usage::

    pixi run probe-hosted -- --config examples/configs/veo.yaml

Exit 0 == every configured strategy's credentials_present() AND
health_check() pass. Non-zero == at least one strategy failed; the
checklist on stdout names every gap.

All I/O is injectable through the public API (probe_strategies,
write_snapshot, run); the CLI entry point is a thin wrapper.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from kinoforge.core.auth import AuthStrategy  # noqa: E402

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProbeResult:
    """One strategy's probe outcome."""

    name: str
    ok: bool
    identity: str | None
    reason: str | None


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def probe_strategies(
    strategies: Sequence[tuple[str, AuthStrategy]],
) -> list[ProbeResult]:
    """Run credentials_present + health_check on each strategy.

    Args:
        strategies: Sequence of ``(name, strategy)`` pairs; ``name`` is the
            engine / service the strategy authenticates.

    Returns:
        List of :class:`ProbeResult` in input order.
    """
    results: list[ProbeResult] = []
    for name, strat in strategies:
        if not strat.credentials_present():
            results.append(
                ProbeResult(name=name, ok=False, identity=None, reason="credentials missing")
            )
            continue
        outcome = strat.health_check()
        results.append(
            ProbeResult(
                name=name,
                ok=outcome.ok,
                identity=outcome.identity,
                reason=outcome.reason,
            )
        )
    return results


def write_snapshot(path: Path, results: Sequence[ProbeResult]) -> None:
    """Atomic snapshot write: ``path.tmp`` then ``os.replace`` to ``path``."""
    body = {
        "git_sha": _git_sha(),
        "captured_at": datetime.now().isoformat(),
        "strategies": [
            {
                "name": r.name,
                "ok": r.ok,
                "identity": r.identity,
                "reason": r.reason,
            }
            for r in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, indent=2) + "\n")
    os.replace(tmp, path)


def run(
    strategies: Sequence[tuple[str, AuthStrategy]],
    *,
    snapshot_path: Path | None = None,
) -> int:
    """Public entry point. Returns exit code."""
    results = probe_strategies(strategies)
    for r in results:
        if r.ok:
            print(f"PASS strategy={r.name} identity={r.identity}")
        else:
            print(f"FAIL strategy={r.name} reason={r.reason}")
    if snapshot_path is not None:
        write_snapshot(snapshot_path, results)
    return 0 if all(r.ok for r in results) else 1


def _load_strategies_from_config(config_path: Path) -> list[tuple[str, AuthStrategy]]:
    """Parse a kinoforge YAML config and instantiate every configured auth strategy."""
    import yaml

    from kinoforge.core.auth import build_auth_strategy

    cfg = yaml.safe_load(config_path.read_text())
    strategies: list[tuple[str, AuthStrategy]] = []
    engine_block = cfg.get("engine", {})
    for engine_name, engine_cfg in engine_block.items():
        if not isinstance(engine_cfg, dict):
            continue
        auth_spec = engine_cfg.get("auth")
        if isinstance(auth_spec, dict):
            strategies.append((engine_name, build_auth_strategy(auth_spec)))
    return strategies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kinoforge hosted-auth preflight probe")
    parser.add_argument("--config", required=True, type=Path, help="kinoforge YAML config")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Where to write the JSON snapshot (default: tools/_snapshots/probe-<config-stem>.json)",
    )
    args = parser.parse_args(argv)

    snapshot_path = args.snapshot or Path("tools/_snapshots") / f"probe-{args.config.stem}.json"
    strategies = _load_strategies_from_config(args.config)
    return run(strategies, snapshot_path=snapshot_path)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run unit tests — confirm 6 pass**

Run: `pixi run test tests/test_probe_hosted.py -v 2>&1 | tail -15`
Expected: 6 passed.

- [ ] **Step 5: Add the `probe-hosted` pixi task**

Edit `pixi.toml` — locate the `[tasks]` section (after the `test-live-skypilot` line) and append:

```toml
probe-hosted = "python -m tools.probe_hosted"
```

Verify the task is recognised:

Run: `pixi run probe-hosted --help 2>&1 | tail -5`
Expected: argparse usage block.

- [ ] **Step 6: Commit**

```bash
git add tools/probe_hosted.py tests/test_probe_hosted.py pixi.toml
git commit -m "feat(tools/probe-hosted): preflight tool for AuthStrategy health

Walks every configured AuthStrategy in a kinoforge YAML, runs
credentials_present() + health_check(), and writes an atomic JSON
snapshot. Exits 0 only if every strategy passes. Used by Layer 2 /
Layer 3 live smokes as a fail-fast gate before any cloud call.

Mirrors tools/preflight.py shape (Phase 39); 6 unit tests use the
FakeAuthStrategy fixture.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: docs + PROGRESS.md + final gate

**Goal:** Wire Layer 1 into the user-facing docs and the recovery index. Run the full suite + lint + typecheck as the closing gate.

**Files:**
- Modify: `README.md` — add "Auth strategies" subsection
- Modify: `PROGRESS.md` — add Phase 41 (Layer 1) entry with per-task SHAs

**Acceptance Criteria:**
- [ ] `README.md` has a new "Auth strategies" subsection under the existing "Real providers" section
- [ ] Subsection documents the 3 shipped strategies + the `auth:` YAML block shape + how to plug a new strategy
- [ ] `PROGRESS.md` has a `### Phase 41 — Layer 1 AuthStrategy substrate` entry with: per-task SHAs, key decisions, test count delta, deferred items (Veo + Nova Reel engines), "Closes" line
- [ ] `pixi run test` is fully green
- [ ] `pixi run lint` is clean
- [ ] `pixi run typecheck` is clean
- [ ] `pixi run pre-commit run --all-files` is clean

**Verify:** `pixi run test && pixi run lint && pixi run typecheck && pixi run pre-commit run --all-files 2>&1 | tail -20` → all green

**Steps:**

- [ ] **Step 1: README — add "Auth strategies" subsection**

Locate the "Real providers" section in `README.md` (search for `## Real providers` or similar). Append a new `### Auth strategies` subsection:

```markdown
### Auth strategies

Hosted engines authenticate via a pluggable `AuthStrategy`. Three concrete
strategies ship in `kinoforge.core.auth`:

| Name | Used by | Auth shape |
|---|---|---|
| `bearer` | `HostedAPIEngine` (fal today; Replicate / Runway / Luma later) | `Authorization: Bearer <env-var>` |
| `gcp_service_account` | VeoEngine (Layer 2); future Vertex AI integrations | `google.auth` default chain |
| `aws_sigv4` | NovaReelEngine (Layer 3); future Bedrock integrations | SigV4 request signing |

Each engine config carries a nested `auth:` block with a `strategy:`
discriminator. Example:

```yaml
engine:
  hosted:
    endpoint: https://fal.run/fal-ai/wan-t2v
    auth:
      strategy: bearer
      env_var: FAL_KEY
```

Backward-compat: when `auth:` is omitted on an existing hosted config,
`provision()` derives `Bearer(env_var=cfg.api_key_env)` automatically.

Preflight: `pixi run probe-hosted -- --config <config-path>` walks every
configured strategy and verifies credentials + health before any live
call.

Adding a new strategy: subclass `AuthStrategy`, implement all 5 methods,
register the class name in `_REGISTRY` in `core/auth.py`. The ABC's
stable surface is locked by `test_auth_strategy_abc_stable_surface` —
intentional evolution requires regenerating
`tests/fixtures/auth_strategy_baseline.json` in the same commit.
```

- [ ] **Step 2: PROGRESS.md — add Phase 41 entry**

Locate the most recent phase entry in `PROGRESS.md` (Phase 40 — Layer W+β PARTIAL) and append after it:

```markdown
### Phase 41 — Layer 1 AuthStrategy substrate

Pluggable-auth foundation for hosted engines. Ships an `AuthStrategy`
ABC (5 stable methods, locked by signature-baseline invariant) plus
Bearer / GCPServiceAccount / AWSSigV4 concrete strategies, a
`build_auth_strategy` registry, a backward-compat retrofit of
`HostedAPIEngine`, a `FakeAuthStrategy` test fixture, and a
`tools/probe_hosted.py` preflight tool. Foundation for Layer 2 (Veo)
and Layer 3 (Nova Reel) plus future Bearer providers
(Replicate / Runway / Luma).

Spec:
`docs/superpowers/specs/2026-06-07-veo-novareel-auth-strategy-design.md`.
Plan:
`docs/superpowers/plans/2026-06-07-layer-1-auth-strategy-substrate.md`.

- [x] Task 0: boto3 pin `>=1.34,<2.0` — commit `<T0-SHA>`
- [x] Task 1: AuthStrategy ABC + HealthResult + HttpRequest — commit `<T1-SHA>`
- [x] Task 2: Bearer strategy + 8 unit tests — commit `<T2-SHA>`
- [x] Task 3: GCPServiceAccount strategy + 7 unit tests — commit `<T3-SHA>`
- [x] Task 4: AWSSigV4 strategy + 7 unit tests — commit `<T4-SHA>`
- [x] Task 5: build_auth_strategy registry + 6 unit tests — commit `<T5-SHA>`
- [x] Task 6: ABC stable-surface invariant + extended subprocess-isolation — commit `<T6-SHA>`
- [x] Task 7: FakeAuthStrategy shared fixture — commit `<T7-SHA>`
- [x] Task 8: HostedAPIEngine retrofit (backward-compat) — commit `<T8-SHA>`
- [x] Task 9: tools/probe_hosted.py + pixi task — commit `<T9-SHA>`
- [x] Task 10: README + PROGRESS + final gate — commit `<T10-SHA>`

**Key design decisions:**

- **5-method ABC** — `credentials_present`, `health_check`,
  `redact_patterns`, `apply`, `client_kwargs`. Locked by signature
  baseline + invariant test.
- **Typed boundary objects** — `HealthResult` / `HttpRequest` frozen
  dataclasses. No duck-typed `dict[str, Any]` returns from the ABC.
- **Lazy vendor SDK imports** — `google.auth` and `boto3` only enter
  `sys.modules` when a strategy method is called, never at module
  import. Verified by extended subprocess-isolation invariant.
- **Direct SigV4 implementation** — hashlib + hmac stdlib rather than
  `botocore.auth.SigV4Auth`. Keeps the seam SDK-version-independent
  and lazy.
- **`build_auth_strategy` registry** — single discriminator-based
  factory. Unknown names raise `UnknownAdapter` for consistency with
  the rest of the registry pattern (engines, providers, sources,
  stores, splitters).
- **`HostedAPIEngine` backward-compat** — `auth_strategy=None` default
  derives `Bearer(env_var=cfg.api_key_env)` at `provision()` time.
  All 9 pre-Layer-1 construction sites pass unchanged.
- **Azure + OCI pseudocode in the spec, NOT the codebase** — verifies
  the ABC admits both providers without modification. Catches AWS+GCP
  over-fit before any real third-cloud integration lands.

**Test count:** <baseline> pre-Layer-1 → <baseline + N> post-Layer-1
(+N net Layer 1 tests; all offline, no live spend).

**Live spend:** $0. Fully offline-tested via `FakeAuthStrategy` and
monkeypatched fake `google.auth` + `boto3` modules.

**Layer sequencing hard-block:** Layer 2 (Veo) and Layer 3 (Nova Reel)
plans MUST hard-block on this layer's merge commit per the spec §2
sequencing rule.

**Forward-compat hooks** (spec §7): future Bearer providers
(Replicate / Runway / Luma) land config-only — no new engine code.
Future cloud-native providers (Vertex Imagen, Bedrock Claude, Azure
DALL-E) reuse the existing strategies or add one new strategy per
auth family.

Closes (partial): PROGRESS:113 carry-forward "Engine-integration live
smoke" — Layer 1 is the architectural foundation; Layer 2 + Layer 3
close the engine surface.
```

(Replace each `<TX-SHA>` with the actual short SHA from the corresponding `git rev-parse --short HEAD` after each task's commit. Run the SHA backfill as the final step.)

- [ ] **Step 3: Run the closing gate**

Run: `pixi run test 2>&1 | tail -5`
Expected: all green.

Run: `pixi run lint 2>&1 | tail -5`
Expected: clean.

Run: `pixi run typecheck 2>&1 | tail -10`
Expected: clean.

Run: `pixi run pre-commit run --all-files 2>&1 | tail -15`
Expected: clean.

- [ ] **Step 4: Backfill SHAs in PROGRESS.md**

Run:

```bash
git log --oneline -15
```

Copy the short SHAs for Tasks 0–10 into the `<TX-SHA>` placeholders in PROGRESS.md.

- [ ] **Step 5: Commit docs + final gate evidence**

```bash
git add README.md PROGRESS.md
git commit -m "docs(layer-1): README auth-strategies section + Phase 41 entry

Phase 41 wraps Layer 1 of the Veo + Nova Reel + AuthStrategy substrate
work. Closes the architectural foundation; Layers 2 + 3 plans depend
on this merge commit.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Self-review checklist (run after writing the plan, before handoff)

- [x] **Spec coverage** — every spec section addressed by a task:
  - §3.1 ABC stable surface → Task 1
  - §3.2 concrete strategies → Tasks 2, 3, 4
  - §3.3 invariant test + baseline → Task 6
  - §4.3 `build_auth_strategy` registry → Task 5
  - §4.4 `HostedAPIEngine` retrofit → Task 8
  - §6.7 Layer 1 TDD ordering → reflected in task sequence
  - §7.1 + §7.2 Azure / OCI appendices → already in the spec doc (no code)
  - §7.3 typed boundary objects → Task 1
  - §7.4 mandatory methods → enforced by `@abstractmethod`s in Task 1
  - §7.5 `FakeAuthStrategy` → Task 7
  - §7.6 SDK version pin → Task 0
  - §7.7 layer sequencing hard-block → plan header + Task 10 PROGRESS entry
  - §7.8 pre-implementation grep → Task 8 Step 1
  - `tools/probe_hosted.py` → Task 9

- [x] **No placeholders** — every step has actual code or actual commands.
- [x] **Type consistency** — `HealthResult`, `HttpRequest`, `AuthStrategy`, `build_auth_strategy`, `FakeAuthStrategy`, `ProbeResult` used consistently across tasks; method names match.
- [x] **Live-spend** — Layer 1 is offline; no smoke tests fire; no operator actions required.
