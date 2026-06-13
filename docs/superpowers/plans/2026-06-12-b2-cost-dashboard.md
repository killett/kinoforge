# B2 — Cost Dashboard + Provider-Account Balance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kinoforge cost` subcommand reading ledger + classify verdicts + RunPod GraphQL balance, rendering human / `--json` / `--prom` modes, with disk-cached balance reads and zero live spend.

**Architecture:** Mirrors B5a substrate-plus-satisfier split. New pure substrate `core/balance_endpoints.py` (Protocol + `ProviderBalance` + `TransportError` + `NoBalanceEndpoint` + `provider_balance_supported`). New pure aggregator `core/cost.py` (folds ledger + verdicts into `CostSnapshot`). RunPod GraphQL satisfier at `providers/runpod/balance.py`. Dispatch helper `build_balance_endpoint_for` joins kinoforge's existing `_adapters.py` registry next to `build_heartbeat_endpoint_for`. CLI `_cmd_cost` (in `cli/_commands.py`) owns ledger IO + classify + cache + render; cache lives at `<store>/cost/balance_<provider>.json` via existing `cfg.store` routing.

**Tech Stack:** Python 3.12 stdlib (urllib, json, dataclasses, datetime). pydantic v2 for existing Config; no new model. pytest for offline tests. No new runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md` (commit `b85f49e`).

**Live spend:** $0. One operator-captured GraphQL fixture (snapshot of `{ myself { clientBalance } }`); all tests use injected `http_post` seam.

---

## File Structure

Files created or modified, each with one clear responsibility:

| Path | Responsibility |
|---|---|
| `src/kinoforge/core/balance_endpoints.py` (create) | Pure substrate: `BalanceEndpoint` Protocol + `ProviderBalance` dataclass + `TransportError` + `NoBalanceEndpoint` + `provider_balance_supported(kind)` helper. No I/O. No provider imports. |
| `src/kinoforge/providers/runpod/balance.py` (create) | RunPod GraphQL `clientBalance` satisfier with injected `http_post` seam. Mirrors `providers/runpod/heartbeat.py` shape. |
| `src/kinoforge/_adapters.py` (modify) | +`build_balance_endpoint_for(cfg, creds)` next to `build_heartbeat_endpoint_for`. Lazy-imports RunPod satisfier; returns `NoBalanceEndpoint()` for all other kinds. |
| `src/kinoforge/core/cost.py` (create) | Pure aggregator: `CostSnapshot` + `ProviderBreakdown` frozen dataclasses + `aggregate(...)` fold. No I/O. No provider imports. `_BURNING_VERDICTS` constant. |
| `src/kinoforge/cli/_commands.py` (modify) | +`_cmd_cost` + `cached_balance_read` + `_render_cost_human` + `_render_cost_json` + `_render_cost_prom`. Reads `KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var. |
| `src/kinoforge/cli/_main.py` (modify) | +`cost` subparser between `gc` and `batch`; flags `--json` / `--prom` (mutex) / `--no-cache` / `--cache-ttl`. |
| `tests/core/test_balance_endpoints.py` (create) | Substrate unit tests. |
| `tests/providers/test_runpod_balance.py` (create) | RunPod satisfier unit tests; happy / transport / schema / cred / negative. |
| `tests/providers/fixtures/runpod_balance_response.json` (create) | One operator-captured snapshot of `{ myself { clientBalance } }` GraphQL response. Operator runs the `curl` once at task t2 start. |
| `tests/core/test_cost.py` (create) | Pure aggregator unit tests. |
| `tests/cli/test_balance_cache.py` (create) | Cache hit / miss / stale-fallback + `--no-cache` round-trip across `LocalArtifactStore` + mocked-S3. |
| `tests/cli/test_cmd_cost.py` (create) | Golden table render + `--json` shape lock + `--prom` exposition lock + failure-mode parametrize. |
| `tests/test_examples.py` (modify) | One new case for `examples/configs/cost.yaml`. |
| `examples/configs/cost.yaml` (create) | Minimal documented cost-dashboard YAML — RunPod compute block with credentials reference. |
| `README.md` (modify) | New "Cost dashboard" section after the existing "Operator heartbeat semantics" section. |
| `PROGRESS.md` (modify) | Strike B2; reference commit sha. |
| `warm-reuse-tasks.txt` (modify) | Flip "design APPROVED" → "CLOSED commit <sha>". |
| `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` (modify) | Amend §6 to point at B2 closing the Layer X gap. |

---

## Task 1: Substrate — `core/balance_endpoints.py`

**Goal:** Pure substrate (Protocol + ProviderBalance + TransportError + NoBalanceEndpoint + provider_balance_supported helper) committed with full offline test coverage.

**Files:**
- Create: `src/kinoforge/core/balance_endpoints.py`
- Test: `tests/core/test_balance_endpoints.py`

**Acceptance Criteria:**
- [ ] `ProviderBalance` is a frozen dataclass with fields `usd: float`, `as_of: datetime`, `source: str`, `currency: str = "USD"`; mutation raises `dataclasses.FrozenInstanceError`.
- [ ] `BalanceEndpoint` is a `Protocol` with one method `read() -> ProviderBalance | None`.
- [ ] `NoBalanceEndpoint().read()` returns `None` unconditionally and is `BalanceEndpoint`-compatible (duck-typed; `isinstance(obj, BalanceEndpoint)` is irrelevant since Protocol is structural).
- [ ] `TransportError` is a direct subclass of `Exception` (NOT `ValueError`), so accidental broad catches don't swallow it.
- [ ] `provider_balance_supported("runpod")` is `True`; everything else (`"skypilot"`, `"local"`, `""`, `"unknown"`) is `False`.
- [ ] No imports from `kinoforge.providers.*` / `kinoforge.engines.*` / `kinoforge.sources.*` (core-import-ban invariant survives).
- [ ] All public objects are exported via `__all__`.

**Verify:** `pixi run pytest tests/core/test_balance_endpoints.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_balance_endpoints.py`:

```python
"""Substrate tests for B2 / Layer X balance-readout."""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from kinoforge.core.balance_endpoints import (
    BalanceEndpoint,
    NoBalanceEndpoint,
    ProviderBalance,
    TransportError,
    provider_balance_supported,
)


def test_provider_balance_frozen() -> None:
    """BUG CATCH: mutation MUST raise; aggregator relies on frozen identity."""
    b = ProviderBalance(usd=10.0, as_of=datetime(2026, 6, 12), source="src")
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.usd = 20.0  # type: ignore[misc]


def test_provider_balance_default_currency() -> None:
    """Currency defaults to USD so today's call sites don't need to pass it."""
    b = ProviderBalance(usd=10.0, as_of=datetime(2026, 6, 12), source="src")
    assert b.currency == "USD"


def test_no_balance_endpoint_read_returns_none() -> None:
    """NoBalanceEndpoint short-circuits read() to None unconditionally."""
    assert NoBalanceEndpoint().read() is None


def test_no_balance_endpoint_is_balance_endpoint_protocol() -> None:
    """Protocol structural conformance — required for build_balance_endpoint_for return type."""
    endpoint: BalanceEndpoint = NoBalanceEndpoint()
    assert endpoint.read() is None


def test_transport_error_is_exception_not_value_error() -> None:
    """BUG CATCH: TransportError under ValueError gets swallowed by broad
    `except ValueError` arms in legacy CLI code — keep it under bare Exception."""
    assert issubclass(TransportError, Exception)
    assert not issubclass(TransportError, ValueError)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("runpod", True),
        ("skypilot", False),
        ("local", False),
        ("", False),
        ("unknown", False),
        ("RUNPOD", False),  # case-sensitive
    ],
)
def test_provider_balance_supported(kind: str, expected: bool) -> None:
    """RunPod is the lone shipping satisfier; everything else False per spec §3."""
    assert provider_balance_supported(kind) is expected


def test_substrate_does_not_import_provider_modules() -> None:
    """Core-import-ban invariant: balance_endpoints.py imports nothing
    from kinoforge.providers / engines / sources."""
    import sys

    import kinoforge.core.balance_endpoints  # noqa: F401

    forbidden_prefixes = (
        "kinoforge.providers",
        "kinoforge.engines",
        "kinoforge.sources",
    )
    for module_name in list(sys.modules):
        if module_name.startswith(forbidden_prefixes):
            # Allowed: the test framework may have already loaded fixtures.
            # The check is on what THIS module's import alone pulls in;
            # we run it in a subprocess for the strict variant in
            # tests/test_core_invariant.py. Here we just assert the
            # source code itself contains no such import statement.
            pass
    src = (
        __import__(
            "kinoforge.core.balance_endpoints",
            fromlist=["*"],
        ).__file__
    )
    assert src is not None
    with open(src) as fh:
        text = fh.read()
    for prefix in forbidden_prefixes:
        assert prefix not in text, (
            f"balance_endpoints.py contains forbidden import prefix {prefix!r}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_balance_endpoints.py -v`
Expected: every test FAILs with `ModuleNotFoundError: No module named 'kinoforge.core.balance_endpoints'`.

- [ ] **Step 3: Write the substrate module**

Create `src/kinoforge/core/balance_endpoints.py`:

```python
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
        usd: Numeric balance in the declared currency. ``-`` is allowed
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

    def read(self) -> ProviderBalance | None: ...


class NoBalanceEndpoint:
    """Ships for every provider/engine without a real satisfier.

    ``read()`` returns ``None`` unconditionally; the renderer pairs this
    with :func:`provider_balance_supported` returning ``False`` to pick
    the ``balance: N/A`` literal instead of ``balance: ? (no credential)``.
    """

    def read(self) -> None:
        return None


_SUPPORTED: frozenset[str] = frozenset({"runpod"})


def provider_balance_supported(provider_kind: str) -> bool:
    """True iff a real satisfier ships for ``provider_kind``.

    Sister to B5a's :func:`kinoforge.core.heartbeat_endpoints.provider_heartbeat_supported`.
    Renderer uses this to pick ``balance: N/A`` (no satisfier) vs
    ``balance: ? (no credential)`` (no cred) vs ``balance: $X`` (success).

    Args:
        provider_kind: Lowercase provider kind string from ``cfg.compute.provider``
            (e.g. ``"runpod"``, ``"skypilot"``, ``"local"``).

    Returns:
        True only when a balance satisfier module ships for that kind.
        Today this is ``"runpod"`` only.
    """
    return provider_kind in _SUPPORTED
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pixi run pytest tests/core/test_balance_endpoints.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Run lint + type-check + invariants**

Run: `pixi run pre-commit run --files src/kinoforge/core/balance_endpoints.py tests/core/test_balance_endpoints.py && pixi run pytest tests/test_core_invariant.py -v`
Expected: all hooks PASS; the existing core-invariant tests stay GREEN (no new core→provider imports).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/balance_endpoints.py tests/core/test_balance_endpoints.py
git commit -m "feat(b2): core/balance_endpoints.py substrate

BalanceEndpoint Protocol + ProviderBalance frozen dataclass
+ TransportError + NoBalanceEndpoint + provider_balance_supported.
Mirrors B5a heartbeat_endpoints.py shape. Pure substrate; no I/O.
"
```

---

## Task 2: RunPod satisfier — `providers/runpod/balance.py`

**Goal:** Inject-seam RunPod GraphQL satisfier hitting `{ myself { clientBalance } }`, all four failure modes (happy / transport / schema / cred-missing / negative) covered offline; one fixture committed.

**Files:**
- Create: `src/kinoforge/providers/runpod/balance.py`
- Create: `tests/providers/test_runpod_balance.py`
- Create: `tests/providers/fixtures/runpod_balance_response.json`

**Acceptance Criteria:**
- [ ] `RunPodBalanceEndpoint(api_key: str | None, http_post: Callable | None = None)` constructible with either real or fake `http_post`.
- [ ] `read()` returns `ProviderBalance(usd=<value>, as_of=<now-local-TZ>, source="runpod-graphql-clientBalance")` on the happy path.
- [ ] `api_key is None or ""` → `read()` returns `None` and makes ZERO calls to `http_post` (spy assertion).
- [ ] Missing `data` / `myself` / `clientBalance` key in response → raises `TransportError` with `"schema drift"` substring.
- [ ] Non-numeric `clientBalance` value → raises `TransportError`.
- [ ] Underlying `http_post` raising any `TransportError` propagates unchanged.
- [ ] Negative `clientBalance` (e.g. `-3.50`) flows through verbatim (no clamping, no error).
- [ ] Fixture `tests/providers/fixtures/runpod_balance_response.json` is a valid GraphQL response shape captured from the live API.

**Verify:** `pixi run pytest tests/providers/test_runpod_balance.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Operator captures the fixture (one-time, no test-suite spend)**

Run on the operator host (NOT in tests):

```bash
curl -sS -X POST https://api.runpod.io/graphql \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ myself { clientBalance } }"}'
```

Snapshot the JSON response into `tests/providers/fixtures/runpod_balance_response.json`. Redact / scrub if the balance value is sensitive; round to `10.00` if needed. Commit the file with the satisfier code in Step 6.

If `tests/providers/fixtures/` does not exist, create it.

- [ ] **Step 2: Write the failing tests**

Create `tests/providers/test_runpod_balance.py`:

```python
"""Tests for the B2 RunPod GraphQL balance satisfier."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from kinoforge.core.balance_endpoints import ProviderBalance, TransportError
from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint

_FIXTURE = Path(__file__).parent / "fixtures" / "runpod_balance_response.json"


def _fixture_with_balance(balance: float) -> dict:
    """Return a fixture-shaped dict with the clientBalance value overridden."""
    base = json.loads(_FIXTURE.read_text())
    base["data"]["myself"]["clientBalance"] = balance
    return base


def test_happy_path_returns_provider_balance() -> None:
    """BUG CATCH: float() unwrap MUST work whether SDK returns int or float."""
    captured: list[tuple[str, dict, dict]] = []

    def fake_http_post(url: str, body: dict, headers: dict) -> dict:
        captured.append((url, body, headers))
        return _fixture_with_balance(42.18)

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    before = datetime.now()
    result = endpoint.read()
    after = datetime.now()

    assert isinstance(result, ProviderBalance)
    assert result.usd == 42.18
    assert result.source == "runpod-graphql-clientBalance"
    assert result.currency == "USD"
    assert before <= result.as_of <= after
    # Verify wire shape
    url, body, headers = captured[0]
    assert url == "https://api.runpod.io/graphql"
    assert body == {"query": "{ myself { clientBalance } }"}
    assert headers["Authorization"] == "Bearer rp_test"
    assert headers["Content-Type"] == "application/json"


@pytest.mark.parametrize("api_key", [None, ""])
def test_missing_credential_returns_none_without_call(api_key: str | None) -> None:
    """No credential → return None; MUST NOT call http_post (spy).

    BUG CATCH: an `api_key or "MISSING"` fallback that still hits the API
    would 401 the operator silently and burn a wire call per `kinoforge cost`.
    """
    spy_call_count = 0

    def spy_http_post(url: str, body: dict, headers: dict) -> dict:
        nonlocal spy_call_count
        spy_call_count += 1
        return {}

    endpoint = RunPodBalanceEndpoint(api_key=api_key, http_post=spy_http_post)
    assert endpoint.read() is None
    assert spy_call_count == 0


@pytest.mark.parametrize(
    "missing_path",
    [
        # No top-level data
        lambda fx: {"errors": [{"message": "auth"}]},
        # data exists but no myself
        lambda fx: {"data": {}},
        # myself exists but no clientBalance
        lambda fx: {"data": {"myself": {"id": "abc"}}},
        # clientBalance not numeric
        lambda fx: {"data": {"myself": {"clientBalance": "not-a-number"}}},
        # clientBalance is None
        lambda fx: {"data": {"myself": {"clientBalance": None}}},
    ],
)
def test_schema_drift_raises_transport_error(missing_path) -> None:
    """BUG CATCH: a KeyError leaking out of read() would break the render
    path's contract. EVERY schema-drift shape MUST land as TransportError."""

    def fake_http_post(url: str, body: dict, headers: dict) -> dict:
        return missing_path(None)

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    with pytest.raises(TransportError) as exc_info:
        endpoint.read()
    assert "schema drift" in str(exc_info.value)


def test_transport_error_propagates() -> None:
    """An http_post raising TransportError (network failure) MUST propagate."""

    def fake_http_post(url: str, body: dict, headers: dict) -> dict:
        raise TransportError("connection refused")

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    with pytest.raises(TransportError, match="connection refused"):
        endpoint.read()


def test_negative_balance_flows_through_verbatim() -> None:
    """RunPod auto-debit accounts can sit briefly negative; rendered verbatim
    per spec §12 (negative balance row)."""

    def fake_http_post(url: str, body: dict, headers: dict) -> dict:
        return _fixture_with_balance(-3.50)

    endpoint = RunPodBalanceEndpoint(api_key="rp_test", http_post=fake_http_post)
    result = endpoint.read()
    assert result is not None
    assert result.usd == -3.50
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pixi run pytest tests/providers/test_runpod_balance.py -v`
Expected: every test FAILs with `ModuleNotFoundError: No module named 'kinoforge.providers.runpod.balance'`.

- [ ] **Step 4: Write the satisfier**

Create `src/kinoforge/providers/runpod/balance.py`:

```python
"""RunPod GraphQL ``clientBalance`` reader for B2 / Layer X.

One method. Mirrors :class:`kinoforge.providers.runpod.heartbeat.RunPodGraphQLHeartbeatEndpoint`
constructor + injected-seam shape. Hits the same
``https://api.runpod.io/graphql`` endpoint as
:data:`kinoforge.providers.runpod._LIST_PODS_QUERY` (providers/runpod/__init__.py:774);
distinct query, distinct concern, no shared transport.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.balance_endpoints import ProviderBalance, TransportError

_QUERY = "{ myself { clientBalance } }"
_URL = "https://api.runpod.io/graphql"


def _default_http_post(api_key: str) -> Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]:
    """Build the default POST closure with the operator's key baked in.

    Mirrors :func:`kinoforge.providers.runpod.heartbeat._default_http_post`
    shape; separate factory because the balance signature also takes a
    ``headers`` dict so tests can spy the exact wire shape.
    """

    def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        all_headers = {**headers, "Content-Type": "application/json"}
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers=all_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return json.loads(resp.read())  # type: ignore[no-any-return]
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise TransportError(f"runpod-balance transport: {exc}") from exc

    return _post


class RunPodBalanceEndpoint:
    """Read the operator's RunPod account balance via GraphQL.

    Construction binds the API key; ``read()`` takes no arguments.
    Failure contract per :class:`kinoforge.core.balance_endpoints.BalanceEndpoint`:
        * Transport / shape drift → raise :class:`TransportError`.
        * Missing credential (``api_key`` is ``None`` or empty) → return
          ``None`` without making any wire call.
        * Negative balance → return verbatim.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]] | None = None,
    ) -> None:
        """Bind credentials and inject the transport seam.

        Args:
            api_key: RunPod API key (any scope; read-only suffices).
            http_post: Optional seam. ``None`` builds the default urllib closure.
        """
        self._api_key = api_key
        self._http_post = (
            http_post if http_post is not None else _default_http_post(api_key or "")
        )

    def read(self) -> ProviderBalance | None:
        """Read the account balance.

        Returns:
            A fresh :class:`ProviderBalance`, or ``None`` when the API key
            is missing.

        Raises:
            TransportError: Transport failure or response-shape drift.
        """
        if not self._api_key:
            return None
        headers = {"Authorization": f"Bearer {self._api_key}"}
        body = {"query": _QUERY}
        resp = self._http_post(_URL, body, headers)
        try:
            raw = resp["data"]["myself"]["clientBalance"]
            usd = float(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise TransportError(f"runpod-balance schema drift: {exc}") from exc
        return ProviderBalance(
            usd=usd,
            as_of=datetime.now(),
            source="runpod-graphql-clientBalance",
        )
```

- [ ] **Step 5: Run tests to verify GREEN**

Run: `pixi run pytest tests/providers/test_runpod_balance.py -v`
Expected: all 9 parametrized cases PASS.

- [ ] **Step 6: Run lint + type-check**

Run: `pixi run pre-commit run --files src/kinoforge/providers/runpod/balance.py tests/providers/test_runpod_balance.py tests/providers/fixtures/runpod_balance_response.json`
Expected: hooks PASS.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/providers/runpod/balance.py \
        tests/providers/test_runpod_balance.py \
        tests/providers/fixtures/runpod_balance_response.json
git commit -m "feat(b2): RunPod GraphQL balance satisfier

RunPodBalanceEndpoint with injectable http_post seam mirrors the
heartbeat satisfier shape. Five failure modes covered offline;
operator-captured fixture commits the GraphQL response shape.
"
```

---

## Task 3: Dispatch — `_adapters.py` registry helper

**Goal:** `build_balance_endpoint_for(cfg, creds)` returns `RunPodBalanceEndpoint` for RunPod config, `NoBalanceEndpoint()` for every other provider / engine kind, never raises on lookup.

**Files:**
- Modify: `src/kinoforge/_adapters.py:75-152` (add new function below `build_heartbeat_endpoint_for`)
- Create: `tests/test_adapters_balance.py`

**Acceptance Criteria:**
- [ ] `build_balance_endpoint_for(cfg, creds)` returns `RunPodBalanceEndpoint` when `cfg.compute is not None and cfg.compute.provider == "runpod"`.
- [ ] Returns `NoBalanceEndpoint()` for `cfg.compute.provider in {"local", "skypilot"}`.
- [ ] Returns `NoBalanceEndpoint()` when `cfg.compute is None` (hosted engines: replicate / runway / luma / bedrock_video / fal).
- [ ] Returns `NoBalanceEndpoint()` for unknown / future provider names without raising.
- [ ] Cred-missing case: returns a `RunPodBalanceEndpoint(api_key=None)` — the satisfier's own short-circuit handles the rest. Does NOT raise `AuthError`.
- [ ] Lazy-imports `kinoforge.providers.runpod.balance` only on the RunPod branch (no global import).

**Verify:** `pixi run pytest tests/test_adapters_balance.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/test_adapters_balance.py`:

```python
"""Tests for the B2 build_balance_endpoint_for dispatch helper."""

from __future__ import annotations

import pytest

from kinoforge._adapters import build_balance_endpoint_for
from kinoforge.core.balance_endpoints import NoBalanceEndpoint
from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint


class _FakeCfg:
    """Minimal Config-shaped object — only the attrs the dispatcher reads."""

    def __init__(self, *, compute=None, engine=None):
        self.compute = compute
        self.engine = engine


class _FakeCompute:
    def __init__(self, provider: str):
        self.provider = provider


class _FakeEngine:
    def __init__(self, kind: str):
        self.kind = kind


class _FakeCreds:
    def __init__(self, mapping: dict[str, str | None]):
        self._mapping = mapping

    def get(self, key: str) -> str | None:
        return self._mapping.get(key)


def test_runpod_provider_returns_runpod_balance_endpoint() -> None:
    cfg = _FakeCfg(
        compute=_FakeCompute("runpod"),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({"RUNPOD_API_KEY": "rp_test"})
    endpoint = build_balance_endpoint_for(cfg, creds)
    assert isinstance(endpoint, RunPodBalanceEndpoint)


@pytest.mark.parametrize("provider", ["local", "skypilot", "unknown", ""])
def test_non_runpod_provider_returns_no_balance_endpoint(provider: str) -> None:
    cfg = _FakeCfg(
        compute=_FakeCompute(provider),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({})
    endpoint = build_balance_endpoint_for(cfg, creds)
    assert isinstance(endpoint, NoBalanceEndpoint)


@pytest.mark.parametrize("engine_kind", ["replicate", "runway", "hosted", "fal", "bedrock_video"])
def test_hosted_engine_no_compute_returns_no_balance_endpoint(engine_kind: str) -> None:
    cfg = _FakeCfg(compute=None, engine=_FakeEngine(engine_kind))
    creds = _FakeCreds({})
    endpoint = build_balance_endpoint_for(cfg, creds)
    assert isinstance(endpoint, NoBalanceEndpoint)


def test_runpod_with_missing_api_key_does_not_raise() -> None:
    """BUG CATCH: dispatch MUST NOT raise on missing cred — the satisfier's
    own None-short-circuit handles missing-cred rendering."""
    cfg = _FakeCfg(
        compute=_FakeCompute("runpod"),
        engine=_FakeEngine("comfyui"),
    )
    creds = _FakeCreds({})  # No RUNPOD_API_KEY
    endpoint = build_balance_endpoint_for(cfg, creds)
    assert isinstance(endpoint, RunPodBalanceEndpoint)
    assert endpoint.read() is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/test_adapters_balance.py -v`
Expected: every test FAILs with `ImportError: cannot import name 'build_balance_endpoint_for' from 'kinoforge._adapters'`.

- [ ] **Step 3: Add the dispatch function**

Add to `src/kinoforge/_adapters.py` (after `build_heartbeat_endpoint_for`, around line 152):

```python
def build_balance_endpoint_for(
    cfg: "Config",
    creds: "CredentialProvider",
):
    """Build the right ``BalanceEndpoint`` for the configured provider.

    Sister to :func:`build_heartbeat_endpoint_for` but with a different
    failure contract: this helper NEVER raises ``AuthError`` /
    ``ValidationError`` on lookup. Missing-cred / unknown-provider cases
    fall through to :class:`NoBalanceEndpoint`, whose ``read()`` returns
    ``None`` so the cost-render path stays free of provider-dispatch
    failures.

    Args:
        cfg: The loaded kinoforge config.
        creds: Credential provider; the RunPod branch reads
            ``RUNPOD_API_KEY``.

    Returns:
        A :class:`BalanceEndpoint`. RunPod kind → satisfier; everything
        else → :class:`NoBalanceEndpoint`. Hosted engines (no
        ``compute`` block) also resolve to :class:`NoBalanceEndpoint`.
    """
    from kinoforge.core.balance_endpoints import (
        BalanceEndpoint,
        NoBalanceEndpoint,
    )

    if cfg.compute is None:
        return NoBalanceEndpoint()
    provider = cfg.compute.provider
    if provider == "runpod":
        from kinoforge.providers.runpod.balance import RunPodBalanceEndpoint

        return RunPodBalanceEndpoint(api_key=creds.get("RUNPOD_API_KEY"))
    return NoBalanceEndpoint()
```

Note: the explicit `from kinoforge.core.balance_endpoints import BalanceEndpoint` is unused inside the function body but pulls the Protocol into the import graph so the return-type annotation (added in a follow-on type-cleanup) has it. Keep it for forward-compat; mypy ignores unused-import inside functions.

Also add the imports referenced by the new function's annotation block. Edit the existing `TYPE_CHECKING` block (around lines 69-72) to add:

```python
if TYPE_CHECKING:
    from kinoforge.core.balance_endpoints import BalanceEndpoint  # noqa: F401
    from kinoforge.core.config import Config
    from kinoforge.core.heartbeat_endpoints import HeartbeatEndpoint
    from kinoforge.core.interfaces import CredentialProvider
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pixi run pytest tests/test_adapters_balance.py -v`
Expected: all 11 parametrized cases PASS.

- [ ] **Step 5: Run lint + type-check + invariants**

Run: `pixi run pre-commit run --files src/kinoforge/_adapters.py tests/test_adapters_balance.py && pixi run pytest tests/test_core_invariant.py -v`
Expected: hooks PASS; core invariant tests stay GREEN.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/_adapters.py tests/test_adapters_balance.py
git commit -m "feat(b2): build_balance_endpoint_for dispatch helper

Sibling of build_heartbeat_endpoint_for in _adapters.py. Returns
RunPodBalanceEndpoint for runpod compute, NoBalanceEndpoint for every
other provider or hosted engine. Never raises on lookup; missing-cred
falls through to the satisfier's own None-short-circuit.
"
```

---

## Task 4: Pure aggregator — `core/cost.py`

**Goal:** Pure `aggregate(...)` fold over ledger entries + verdicts → `CostSnapshot`. No I/O. Bad-entry isolation matches `sweep()`.

**Files:**
- Create: `src/kinoforge/core/cost.py`
- Create: `tests/core/test_cost.py`

**Acceptance Criteria:**
- [ ] `_BURNING_VERDICTS = frozenset({LIVE, IDLE_REAP, OVERAGE_REAP, ORPHAN_REAP, HEARTBEAT_UNKNOWN, HEARTBEAT_SUBSTRATE_MISSING})`. STALE_LEDGER and UNROUTABLE excluded.
- [ ] `ProviderBreakdown` is a frozen dataclass with `provider: str`, `burn_rate_usd_per_hr: float`, `spend_usd_total: float`, `pod_counts_by_verdict: Mapping[Verdict, int]`.
- [ ] `CostSnapshot` is a frozen dataclass with all spec §10 fields including defaults `hosted_spend_pending: bool = True` and `throttle_warnings: tuple[str, ...] = ()`.
- [ ] `aggregate(...)` returns a fresh snapshot; calling twice with same inputs yields equal snapshots.
- [ ] Empty ledger → `burn_rate_usd_per_hr == 0.0`, `per_provider == ()`.
- [ ] Mixed verdicts: LIVE + IDLE_REAP + STALE_LEDGER + HEARTBEAT_UNKNOWN — STALE excluded from burn; others sum; per-verdict counts honest.
- [ ] Per-provider aggregation: 3 RunPod + 1 SkyPilot entries → 2 rows, alphabetically sorted (`runpod` before `skypilot`).
- [ ] Bad entry (no `id`, non-numeric `cost_rate_usd_per_hr`) → silently skipped; the rest of the snapshot is honest.
- [ ] `spend_usd_total` math: a 1h-old entry at $0.79/hr → exactly $0.79.
- [ ] `now: datetime` parameter respected: snapshot's `as_of` matches input `now`.
- [ ] All 8 Verdict keys appear in every `pod_counts_by_verdict` map; zeros included.
- [ ] No imports from `kinoforge.providers.*` / `kinoforge.engines.*` / `kinoforge.sources.*`.

**Verify:** `pixi run pytest tests/core/test_cost.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_cost.py`:

```python
"""Tests for the B2 pure cost aggregator."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from kinoforge.core.balance_endpoints import ProviderBalance
from kinoforge.core.cost import (
    CostSnapshot,
    ProviderBreakdown,
    _BURNING_VERDICTS,
    aggregate,
)
from kinoforge.core.reaper import Verdict


_NOW = datetime(2026, 6, 12, 14, 0, 0)


def _entry(
    *,
    id: str,
    provider: str = "runpod",
    rate: float = 0.79,
    created_at_offset_hours: float = 1.0,
) -> dict:
    """Build a ledger-shaped entry. created_at is _NOW minus the offset."""
    created_at = (_NOW - timedelta(hours=created_at_offset_hours)).timestamp()
    return {
        "id": id,
        "provider": provider,
        "cost_rate_usd_per_hr": rate,
        "created_at": created_at,
    }


def test_burning_verdicts_constant_excludes_stale_and_unroutable() -> None:
    """BUG CATCH: STALE_LEDGER MUST NOT contribute to burn — that verdict
    means the pod is gone from the provider per Layer V Row 1."""
    assert Verdict.LIVE in _BURNING_VERDICTS
    assert Verdict.IDLE_REAP in _BURNING_VERDICTS
    assert Verdict.OVERAGE_REAP in _BURNING_VERDICTS
    assert Verdict.ORPHAN_REAP in _BURNING_VERDICTS
    assert Verdict.HEARTBEAT_UNKNOWN in _BURNING_VERDICTS
    assert Verdict.HEARTBEAT_SUBSTRATE_MISSING in _BURNING_VERDICTS
    assert Verdict.STALE_LEDGER not in _BURNING_VERDICTS
    assert Verdict.UNROUTABLE not in _BURNING_VERDICTS


def test_empty_ledger_yields_zero_snapshot() -> None:
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.0
    assert snap.per_provider == ()
    assert snap.as_of == _NOW


def test_single_live_entry_burn_and_spend() -> None:
    entries = [_entry(id="a", rate=0.79, created_at_offset_hours=1.0)]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"a": Verdict.LIVE},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.79
    assert len(snap.per_provider) == 1
    p = snap.per_provider[0]
    assert p.provider == "runpod"
    assert p.burn_rate_usd_per_hr == 0.79
    assert p.spend_usd_total == pytest.approx(0.79, abs=1e-9)


def test_stale_ledger_excluded_from_burn() -> None:
    """BUG CATCH: STALE_LEDGER counts ARE incremented, but burn excludes them."""
    entries = [
        _entry(id="live", rate=0.50),
        _entry(id="stale", rate=99.0),
    ]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"live": Verdict.LIVE, "stale": Verdict.STALE_LEDGER},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.burn_rate_usd_per_hr == 0.50
    p = snap.per_provider[0]
    assert p.pod_counts_by_verdict[Verdict.LIVE] == 1
    assert p.pod_counts_by_verdict[Verdict.STALE_LEDGER] == 1


def test_per_provider_sorted_alphabetically() -> None:
    entries = [
        _entry(id="r1", provider="runpod", rate=0.50),
        _entry(id="s1", provider="skypilot", rate=1.20),
        _entry(id="r2", provider="runpod", rate=0.30),
    ]
    verdicts = {"r1": Verdict.LIVE, "s1": Verdict.LIVE, "r2": Verdict.IDLE_REAP}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert [p.provider for p in snap.per_provider] == ["runpod", "skypilot"]
    runpod = snap.per_provider[0]
    assert runpod.burn_rate_usd_per_hr == pytest.approx(0.80, abs=1e-9)
    assert runpod.pod_counts_by_verdict[Verdict.LIVE] == 1
    assert runpod.pod_counts_by_verdict[Verdict.IDLE_REAP] == 1


def test_bad_entry_silently_skipped() -> None:
    """BUG CATCH: a malformed entry MUST NOT poison the whole snapshot.
    Mirrors sweep() bad-entry isolation."""
    entries = [
        _entry(id="ok", rate=0.50),
        {"provider": "runpod"},  # no id
        {"id": "bad-rate", "provider": "runpod", "cost_rate_usd_per_hr": "NaN-string"},
    ]
    verdicts = {"ok": Verdict.LIVE, "bad-rate": Verdict.LIVE}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    # ok contributes; bad-rate and no-id are skipped (no id → continue,
    # bad rate → float() raises → continue inside aggregate)
    assert snap.burn_rate_usd_per_hr == 0.50


def test_all_eight_verdict_keys_present_in_counts() -> None:
    """BUG CATCH: counts dict MUST carry every Verdict key (zeros included)
    so --json / --prom emit a stable shape."""
    entries = [_entry(id="a", rate=0.10)]
    verdicts = {"a": Verdict.LIVE}
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts,
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    p = snap.per_provider[0]
    for v in Verdict:
        assert v in p.pod_counts_by_verdict


def test_balances_and_errors_pass_through() -> None:
    """Aggregator does NOT do I/O; balances and errors are CLI-supplied passthrough."""
    pb = ProviderBalance(usd=42.18, as_of=_NOW, source="runpod-graphql-clientBalance")
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={"runpod": pb},
        balance_errors={"skypilot": "no satisfier"},
        heartbeat_partial_truth=("skypilot",),
    )
    assert snap.balances["runpod"] is pb
    assert snap.balance_errors["skypilot"] == "no satisfier"
    assert snap.heartbeat_partial_truth == ("skypilot",)


def test_snapshot_is_frozen() -> None:
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.burn_rate_usd_per_hr = 99.0  # type: ignore[misc]


def test_provider_breakdown_is_frozen() -> None:
    entries = [_entry(id="a", rate=0.10)]
    snap = aggregate(
        entries=entries,
        verdicts_by_id={"a": Verdict.LIVE},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.per_provider[0].burn_rate_usd_per_hr = 99.0  # type: ignore[misc]


def test_hosted_spend_pending_default_true() -> None:
    """Until B10 ships, hosted-engine spend is NOT in the totals; flag stays True."""
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
    )
    assert snap.hosted_spend_pending is True


def test_throttle_warnings_passthrough() -> None:
    snap = aggregate(
        entries=[],
        verdicts_by_id={},
        now=_NOW,
        balances={},
        balance_errors={},
        heartbeat_partial_truth=(),
        throttle_warnings=("replicate approaching $5 throttle",),
    )
    assert snap.throttle_warnings == ("replicate approaching $5 throttle",)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_cost.py -v`
Expected: every test FAILs with `ModuleNotFoundError: No module named 'kinoforge.core.cost'`.

- [ ] **Step 3: Write the aggregator**

Create `src/kinoforge/core/cost.py`:

```python
"""Layer X: pure cost-aggregator substrate.

CLI owns ledger read + balance reads + classify call + env-var read +
render. This module folds the inputs into a :class:`CostSnapshot`. Bad
ledger entries (missing ``id`` / malformed ``cost_rate_usd_per_hr``) are
isolated: that entry is skipped silently; the rest of the snapshot is
honest. Same isolation contract as :func:`kinoforge.core.reaper_actor.sweep`.

No I/O. No mutable globals. No imports from providers / engines / sources.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from kinoforge.core.balance_endpoints import ProviderBalance
from kinoforge.core.reaper import Verdict

__all__ = [
    "CostSnapshot",
    "ProviderBreakdown",
    "_BURNING_VERDICTS",
    "aggregate",
]

_BURNING_VERDICTS: frozenset[Verdict] = frozenset(
    {
        Verdict.LIVE,
        Verdict.IDLE_REAP,
        Verdict.OVERAGE_REAP,
        Verdict.ORPHAN_REAP,
        Verdict.HEARTBEAT_UNKNOWN,
        Verdict.HEARTBEAT_SUBSTRATE_MISSING,
    }
)


@dataclass(frozen=True)
class ProviderBreakdown:
    """One row in the per-provider table.

    Attributes:
        provider: Provider kind string (e.g. ``"runpod"``).
        burn_rate_usd_per_hr: Sum of ``cost_rate_usd_per_hr`` across
            entries with a verdict in :data:`_BURNING_VERDICTS`.
        spend_usd_total: Sum of ``rate * hours_up`` across the same set.
            Hours_up = ``max(0, (now - created_at) / 3600)``.
        pod_counts_by_verdict: All 8 Verdict keys; zeros included for the
            verdicts not seen on this provider.
    """

    provider: str
    burn_rate_usd_per_hr: float
    spend_usd_total: float
    pod_counts_by_verdict: Mapping[Verdict, int]


@dataclass(frozen=True)
class CostSnapshot:
    """Authoritative aggregator output.

    The CLI render paths (human / --json / --prom) all derive from this
    snapshot. Field defaults match spec §10's JSON schema; future micro-
    layers add keys but never rename.
    """

    as_of: datetime
    burn_rate_usd_per_hr: float
    per_provider: tuple[ProviderBreakdown, ...]
    balances: Mapping[str, ProviderBalance | None]
    balance_errors: Mapping[str, str]
    heartbeat_partial_truth: tuple[str, ...]
    hosted_spend_pending: bool = True
    throttle_warnings: tuple[str, ...] = field(default_factory=tuple)


def aggregate(
    *,
    entries: list[Mapping],
    verdicts_by_id: Mapping[str, Verdict],
    now: datetime,
    balances: Mapping[str, ProviderBalance | None],
    balance_errors: Mapping[str, str],
    heartbeat_partial_truth: tuple[str, ...],
    throttle_warnings: tuple[str, ...] = (),
) -> CostSnapshot:
    """Fold ledger entries + verdicts into a :class:`CostSnapshot`.

    Pure. ``entries`` order does not affect outputs (per-provider tuple is
    sorted by provider name ascending). ``balances`` / ``balance_errors``
    / ``heartbeat_partial_truth`` / ``throttle_warnings`` are pass-through
    from the CLI; aggregator does no I/O and does not look up balances.

    Args:
        entries: Ledger entries (from :meth:`Ledger.entries`). Each must
            carry ``id`` and ``provider``; missing keys are tolerated
            (bad entries skipped silently).
        verdicts_by_id: Pre-computed verdict per entry id, from
            :func:`kinoforge.core.reaper.classify`. Entries whose id is
            absent from this mapping are skipped (defensive — caller
            should pass a verdict for every entry).
        now: Wall-clock used for ``as_of`` and ``spend_usd_total`` math.
        balances: Per-provider balance read; ``None`` is missing-cred /
            no-satisfier / transport failure.
        balance_errors: Per-provider error message; empty when all OK.
        heartbeat_partial_truth: Provider kinds whose verdicts may be
            HEARTBEAT_SUBSTRATE_MISSING because the wire substrate has
            not shipped yet (B5b SkyPilot).
        throttle_warnings: Provider-warning strings (Replicate $5
            throttle gate); empty when none active.

    Returns:
        A fresh frozen :class:`CostSnapshot`.
    """
    by_provider: dict[str, dict] = {}
    for entry in entries:
        instance_id_raw = entry.get("id")
        if instance_id_raw is None:
            continue
        instance_id = str(instance_id_raw)
        verdict = verdicts_by_id.get(instance_id)
        if verdict is None:
            continue
        provider = str(entry.get("provider", "unknown"))
        try:
            rate = float(entry.get("cost_rate_usd_per_hr", 0.0))
        except (TypeError, ValueError):
            continue
        try:
            created_at = float(entry.get("created_at", now.timestamp()))
        except (TypeError, ValueError):
            created_at = now.timestamp()
        slot = by_provider.setdefault(
            provider,
            {"burn": 0.0, "spend": 0.0, "counts": dict.fromkeys(Verdict, 0)},
        )
        slot["counts"][verdict] = slot["counts"][verdict] + 1
        if verdict in _BURNING_VERDICTS:
            slot["burn"] += rate
            hours_up = max(0.0, (now.timestamp() - created_at) / 3600.0)
            slot["spend"] += rate * hours_up

    per_provider = tuple(
        ProviderBreakdown(
            provider=provider,
            burn_rate_usd_per_hr=slot["burn"],
            spend_usd_total=slot["spend"],
            pod_counts_by_verdict=dict(slot["counts"]),
        )
        for provider, slot in sorted(by_provider.items())
    )
    total_burn = sum(p.burn_rate_usd_per_hr for p in per_provider)
    return CostSnapshot(
        as_of=now,
        burn_rate_usd_per_hr=total_burn,
        per_provider=per_provider,
        balances=dict(balances),
        balance_errors=dict(balance_errors),
        heartbeat_partial_truth=heartbeat_partial_truth,
        throttle_warnings=throttle_warnings,
    )
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pixi run pytest tests/core/test_cost.py -v`
Expected: all 12 tests PASS.

- [ ] **Step 5: Run lint + type-check + invariants**

Run: `pixi run pre-commit run --files src/kinoforge/core/cost.py tests/core/test_cost.py && pixi run pytest tests/test_core_invariant.py -v`
Expected: hooks PASS; core invariant tests stay GREEN.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/cost.py tests/core/test_cost.py
git commit -m "feat(b2): pure cost aggregator

core/cost.py folds ledger entries + verdicts into CostSnapshot.
Pure: no I/O. Bad-entry isolation matches sweep(). All 8 Verdict
keys emit zeros in pod_counts_by_verdict for shape stability.
"
```

---

## Task 5: CLI — `_cmd_cost` + `cost` subparser + human / JSON / Prom renderers

**Goal:** `kinoforge cost` subcommand ships with three output modes (default human table; `--json`; `--prom`), reading ledger + classify + balance dispatch + render, all failure modes contained.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (add `cost` subparser around line 414, between `gc` and `batch`)
- Modify: `src/kinoforge/cli/_commands.py` (add `_cmd_cost` + three renderers near the bottom)
- Create: `tests/cli/test_cmd_cost.py`

**Acceptance Criteria:**
- [ ] `kinoforge cost` prints a human table with: top-line burn rate, per-provider rows (provider name + burn_rate + spend + verdict counts), balance section (per provider with `balance: $X` / `balance: ? (no credential)` / `balance: N/A` per matrix), runway derived from `balance / burn_rate` when both present.
- [ ] `kinoforge cost --json` emits the exact §10 schema with all keys present including `hosted_spend_pending: true` and empty `throttle_warnings`.
- [ ] `kinoforge cost --prom` emits all 5 gauges + 1 counter with HELP+TYPE lines per §9; all 8 Verdict label values appear in `kinoforge_pod_count` series.
- [ ] `kinoforge cost --json --prom` → argparse exits with code 2 and message including `mutually exclusive`.
- [ ] Bad-ledger / classify-raise / balance-transport-error / balance-cred-missing failures do NOT raise from the render path; each produces the documented degradation per spec §12.
- [ ] `live_pod_ids` per-provider fallback: when `provider.list_instances()` raises, the CLI catches, logs WARNING, and falls back to `frozenset(<ledger ids for that provider>)`.
- [ ] `KINOFORGE_REPLICATE_THROTTLE_AT_USD` env-var read once at `_cmd_cost` start; default 4.50; `0` disables. Today's path: no replicate ledger entries → `throttle_warnings` empty + footer "replicate spend tracking pending B10" printed.

**Verify:** `pixi run pytest tests/cli/test_cmd_cost.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_cmd_cost.py`:

```python
"""Tests for the B2 `kinoforge cost` CLI subcommand."""

from __future__ import annotations

import argparse
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_cost


@pytest.fixture()
def fake_ctx(tmp_path: Path):
    """A SessionContext stub with an in-memory ledger and stub cfg."""
    from kinoforge.cli._main import SessionContext  # adjust path if SessionContext lives elsewhere
    from kinoforge.core.config import Config
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path / "store")
    cfg = MagicMock(spec=Config)
    cfg.compute = MagicMock()
    cfg.compute.provider = "runpod"
    cfg.engine = MagicMock()
    cfg.engine.kind = "comfyui"
    cfg.lifecycle.return_value.heartbeat_interval_s = None
    cfg.lifecycle.return_value.idle_timeout_s = 600.0
    cfg.lifecycle.return_value.max_lifetime_s = 3600.0
    cfg.lifecycle.return_value.grace_after_session_s = 300.0
    cfg.store = MagicMock()
    cfg.store.kind = "local"

    ctx = MagicMock(spec=SessionContext)
    ctx.cfg = cfg
    ctx.state_dir = tmp_path

    return ctx, store


def _seed_ledger(store, *, entries: list[dict]) -> None:
    """Write `entries` under <state>/ledger.json via the Ledger API."""
    from kinoforge.core.lifecycle import Ledger

    ledger = Ledger(store=store, run_id="_lifecycle")
    # Test helper: directly write entries via the store
    store.put_json("_lifecycle", "ledger.json", entries)


def _args(**overrides) -> argparse.Namespace:
    return argparse.Namespace(
        json=overrides.get("json", False),
        prom=overrides.get("prom", False),
        no_cache=overrides.get("no_cache", True),  # default to no-cache in tests
        cache_ttl=overrides.get("cache_ttl", 15.0),
    )


def test_empty_ledger_human_table(fake_ctx, capsys) -> None:
    ctx, _ = fake_ctx
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = []
        rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Burn rate" in out
    assert "0.00" in out
    assert "no entries" in out.lower() or "per-provider" in out.lower()


def test_json_mode_emits_stable_schema(fake_ctx, capsys) -> None:
    ctx, _ = fake_ctx
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = []
        rc = _cmd_cost(_args(json=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # §10 required keys
    for key in (
        "as_of",
        "burn_rate_usd_per_hr",
        "per_provider",
        "balance",
        "balance_errors",
        "heartbeat_partial_truth",
        "hosted_spend_pending",
        "throttle_warnings",
    ):
        assert key in payload, f"missing key {key!r}"
    assert payload["hosted_spend_pending"] is True
    assert payload["throttle_warnings"] == []


def test_json_and_prom_mutex(fake_ctx) -> None:
    """BUG CATCH: argparse MUST reject --json + --prom together at parse time."""
    from kinoforge.cli._main import _build_parser  # the function that wires subparsers

    parser = _build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["cost", "--json", "--prom"])
    assert exc.value.code == 2


def test_prom_mode_emits_all_gauges_and_help(fake_ctx, capsys) -> None:
    """All 5 gauges + 1 counter present with HELP+TYPE lines; 8 Verdict labels emitted."""
    ctx, store = fake_ctx
    entry = {
        "id": "pod-abc",
        "provider": "runpod",
        "cost_rate_usd_per_hr": 0.79,
        "created_at": (datetime.now() - timedelta(hours=2)).timestamp(),
        "tags": {},
    }
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = [entry]
        # Stub provider lookup so list_instances() returns the live id
        with patch("kinoforge.core.registry.get_provider") as get_prov:
            prov_inst = MagicMock()
            prov_inst.list_instances.return_value = [MagicMock(id="pod-abc")]
            get_prov.return_value = lambda: prov_inst
            rc = _cmd_cost(_args(prom=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    for metric in (
        "kinoforge_burn_rate_usd_per_hr",
        "kinoforge_balance_usd",
        "kinoforge_balance_as_of_seconds",
        "kinoforge_pod_count",
        "kinoforge_spend_usd_total",
        "kinoforge_cost_scrape_errors_total",
    ):
        assert f"# HELP {metric} " in out, f"missing HELP for {metric}"
        assert f"# TYPE {metric} " in out, f"missing TYPE for {metric}"
    # All 8 Verdict labels emitted (zeros included)
    for verdict in (
        "LIVE",
        "IDLE_REAP",
        "OVERAGE_REAP",
        "ORPHAN_REAP",
        "STALE_LEDGER",
        "HEARTBEAT_UNKNOWN",
        "HEARTBEAT_SUBSTRATE_MISSING",
        "UNROUTABLE",
    ):
        assert f'verdict="{verdict}"' in out


def test_balance_failure_does_not_block_burn_render(fake_ctx, capsys) -> None:
    """Critical invariant per spec §12: transport / schema / cred failures
    NEVER raise from the render path; burn rate still renders from ledger."""
    ctx, _ = fake_ctx
    entry = {
        "id": "pod-abc",
        "provider": "runpod",
        "cost_rate_usd_per_hr": 0.79,
        "created_at": (datetime.now() - timedelta(hours=1)).timestamp(),
        "tags": {},
    }
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = [entry]
        with patch("kinoforge.core.registry.get_provider") as get_prov:
            prov_inst = MagicMock()
            prov_inst.list_instances.return_value = [MagicMock(id="pod-abc")]
            get_prov.return_value = lambda: prov_inst
            with patch(
                "kinoforge._adapters.build_balance_endpoint_for"
            ) as build_bal:
                from kinoforge.core.balance_endpoints import TransportError

                ep = MagicMock()
                ep.read.side_effect = TransportError("simulated")
                build_bal.return_value = ep
                rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "transport error" in out.lower()
    # Burn rate still rendered
    assert "0.79" in out


def test_list_instances_failure_fallback(fake_ctx, caplog) -> None:
    """When provider.list_instances raises, fall back to assume-up; do NOT raise."""
    ctx, _ = fake_ctx
    entry = {
        "id": "pod-abc",
        "provider": "runpod",
        "cost_rate_usd_per_hr": 0.79,
        "created_at": (datetime.now() - timedelta(hours=1)).timestamp(),
        "tags": {},
    }
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = [entry]
        with patch("kinoforge.core.registry.get_provider") as get_prov:
            prov_inst = MagicMock()
            prov_inst.list_instances.side_effect = RuntimeError("provider broken")
            get_prov.return_value = lambda: prov_inst
            with caplog.at_level("WARNING"):
                rc = _cmd_cost(_args(), ctx)
    assert rc == 0
    assert any("provider broken" in rec.message or "list_instances" in rec.message
               for rec in caplog.records)


def test_replicate_throttle_stub_footer(fake_ctx, capsys, monkeypatch) -> None:
    """Env-var set, zero Replicate ledger entries → stub footer printed,
    throttle_warnings stays empty."""
    monkeypatch.setenv("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "4.50")
    ctx, _ = fake_ctx
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = []
        rc = _cmd_cost(_args(json=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload["throttle_warnings"] == []


def test_replicate_throttle_disabled_zero(fake_ctx, monkeypatch, capsys) -> None:
    """KINOFORGE_REPLICATE_THROTTLE_AT_USD=0 → no warning ever."""
    monkeypatch.setenv("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "0")
    ctx, _ = fake_ctx
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = []
        rc = _cmd_cost(_args(), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    assert "approaching $5" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/cli/test_cmd_cost.py -v`
Expected: every test FAILs with `ImportError: cannot import name '_cmd_cost' from 'kinoforge.cli._commands'`.

- [ ] **Step 3: Add the subparser to `_main.py`**

In `src/kinoforge/cli/_main.py`, after line 414 (the `p_gc = sub.add_parser("gc", ...)` block) and before line 420 (`p_batch = sub.add_parser("batch", ...)`), insert:

```python
    p_cost = sub.add_parser(
        "cost",
        help="show cost dashboard: burn rate + per-provider breakdown + balance",
    )
    cost_mode = p_cost.add_mutually_exclusive_group()
    cost_mode.add_argument(
        "--json",
        action="store_true",
        help="emit stable JSON schema for piping (Grafana, jq, etc.)",
    )
    cost_mode.add_argument(
        "--prom",
        action="store_true",
        help="emit Prometheus text exposition format",
    )
    p_cost.add_argument(
        "--no-cache",
        action="store_true",
        help="bypass disk cache for balance reads (force fresh)",
    )
    p_cost.add_argument(
        "--cache-ttl",
        type=float,
        default=15.0,
        metavar="SECONDS",
        help="balance cache TTL (default 15s)",
    )
    p_cost.set_defaults(cmd_handler="cost")
```

Also extend the dispatch table at the bottom of `_main.py` (find the dict mapping `"deploy"` → `_cmd_deploy` etc.) to add `"cost": _cmd_cost` plus the import of `_cmd_cost` at the top.

- [ ] **Step 4: Write `_cmd_cost` and the three renderers in `_commands.py`**

Append to `src/kinoforge/cli/_commands.py` (after `_cmd_status`):

```python
def _cmd_cost(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``cost`` subcommand: ledger walk + classify + balance dispatch + render.

    Three output modes (mutually exclusive): default human table, ``--json``,
    ``--prom``. Disk cache for balance reads honors ``--no-cache`` and
    ``--cache-ttl``. Never raises from the render path — every failure
    degrades the affected column per spec §12.

    Args:
        args: Parsed CLI arguments. Fields: ``json``, ``prom``, ``no_cache``,
            ``cache_ttl``.
        ctx: Per-invocation session context.

    Returns:
        Exit code: 0 on success (including degraded balance / partial truth);
        1 only on unexpected internal failure (which the test suite
        explicitly forbids).
    """
    from kinoforge._adapters import build_balance_endpoint_for
    from kinoforge.core import registry
    from kinoforge.core.balance_endpoints import (
        BalanceEndpoint,
        ProviderBalance,
        TransportError,
        provider_balance_supported,
    )
    from kinoforge.core.cost import aggregate
    from kinoforge.core.credentials import EnvCredentialProvider
    from kinoforge.core.heartbeat_endpoints import provider_heartbeat_supported
    from kinoforge.core.reaper import Verdict, classify

    cfg = ctx.cfg
    ledger = ctx.ledger()
    entries = ledger.entries()

    # Local TZ per session memory feedback_local_timezone_only
    now_dt = datetime.now()
    now_ts = now_dt.timestamp()

    # 1. Resolve per-provider live_pod_ids. List_instances failures fall
    #    back to "assume every ledger entry is up" for that provider.
    providers_in_ledger = {str(e.get("provider", "unknown")) for e in entries}
    live_pod_ids_by_provider: dict[str, frozenset[str]] = {}
    for provider_kind in providers_in_ledger:
        try:
            prov_inst = registry.get_provider(provider_kind)()
            ids = frozenset(i.id for i in prov_inst.list_instances())
            live_pod_ids_by_provider[provider_kind] = ids
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: provider %s list_instances failed (%s); "
                "assuming all ledger ids are up",
                provider_kind,
                exc.__class__.__name__,
            )
            fallback = frozenset(
                str(e["id"])
                for e in entries
                if e.get("provider") == provider_kind and e.get("id") is not None
            )
            live_pod_ids_by_provider[provider_kind] = fallback

    # 2. Classify every entry.
    verdicts_by_id: dict[str, Verdict] = {}
    for entry in entries:
        entry_id = entry.get("id")
        if entry_id is None:
            continue
        provider_kind = str(entry.get("provider", "unknown"))
        live_ids = live_pod_ids_by_provider.get(provider_kind, frozenset())
        try:
            verdicts_by_id[str(entry_id)] = classify(
                entry,
                live_ids,
                now_ts,
                idle_timeout_s=cfg.lifecycle().idle_timeout_s if cfg else 600.0,
                max_lifetime_s=cfg.lifecycle().max_lifetime_s if cfg else 3600.0,
                heartbeat_interval_s=cfg.lifecycle().heartbeat_interval_s
                if cfg else None,
                grace_after_session_s=cfg.lifecycle().grace_after_session_s
                if cfg else 300.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: classify failed on entry %s (%s); skipping",
                entry_id,
                exc.__class__.__name__,
            )

    # 3. Read balance per distinct provider via cache helper.
    balances: dict[str, ProviderBalance | None] = {}
    balance_errors: dict[str, str] = {}
    creds = EnvCredentialProvider()
    store = ctx.cfg.store if cfg else None
    for provider_kind in providers_in_ledger:
        if not provider_balance_supported(provider_kind):
            balances[provider_kind] = None
            continue
        # Build a tiny cfg shim mirroring just what build_balance_endpoint_for reads
        endpoint: BalanceEndpoint = build_balance_endpoint_for(cfg, creds)
        bal, err = cached_balance_read(
            store=ctx.store,
            provider=provider_kind,
            endpoint=endpoint,
            cache_ttl_s=args.cache_ttl,
            no_cache=args.no_cache,
            now=now_dt,
        )
        balances[provider_kind] = bal
        if err is not None:
            balance_errors[provider_kind] = err

    # 4. heartbeat_partial_truth: providers in ledger whose substrate isn't shipped.
    heartbeat_partial_truth = tuple(
        sorted(p for p in providers_in_ledger if not provider_heartbeat_supported(p))
    )

    # 5. Replicate throttle stub (B10 lights it green).
    try:
        threshold = float(os.environ.get("KINOFORGE_REPLICATE_THROTTLE_AT_USD", "4.50"))
    except ValueError:
        threshold = 4.50
    throttle_warnings: tuple[str, ...] = ()
    if threshold > 0:
        replicate_spend = sum(
            p.spend_usd_total
            for p in []  # No Replicate entries today; B10 populates.
        )
        if replicate_spend >= 0.9 * threshold:
            throttle_warnings = (
                f"replicate spend ${replicate_spend:.2f} approaching $5 throttle "
                f"(set KINOFORGE_REPLICATE_THROTTLE_AT_USD)",
            )

    # 6. Aggregate.
    snap = aggregate(
        entries=entries,
        verdicts_by_id=verdicts_by_id,
        now=now_dt,
        balances=balances,
        balance_errors=balance_errors,
        heartbeat_partial_truth=heartbeat_partial_truth,
        throttle_warnings=throttle_warnings,
    )

    # 7. Render.
    if args.json:
        sys.stdout.write(_render_cost_json(snap))
    elif args.prom:
        sys.stdout.write(_render_cost_prom(snap, balance_errors))
    else:
        sys.stdout.write(_render_cost_human(snap, threshold_set=(threshold > 0)))
    return 0
```

Then add the three renderers (sketch — flesh out per AC):

```python
def cached_balance_read(
    *,
    store: ArtifactStore,
    provider: str,
    endpoint: BalanceEndpoint,
    cache_ttl_s: float,
    no_cache: bool,
    now: datetime,
) -> tuple[ProviderBalance | None, str | None]:
    """See Task 6 — stubbed here so Task 5 tests can import the name; Task 6 implements."""
    raise NotImplementedError("Task 6: cached_balance_read")


def _render_cost_human(snap, *, threshold_set: bool) -> str:
    """Human-readable table per spec §5 sketch.

    Top-line: Burn rate $X/hr. Per-provider rows with verdict columns.
    Balance section. Runway. Footer for hosted-spend-pending / replicate
    stub / heartbeat_partial_truth annotations.
    """
    lines = []
    lines.append(f"As of {snap.as_of.isoformat(timespec='seconds')}")
    lines.append(f"Burn rate: ${snap.burn_rate_usd_per_hr:.2f}/hr")
    if not snap.per_provider:
        lines.append("(no entries in ledger)")
    else:
        lines.append("")
        lines.append("Per-provider:")
        for p in snap.per_provider:
            counts_str = " ".join(
                f"{v.value}={p.pod_counts_by_verdict.get(v, 0)}"
                for v in Verdict
                if p.pod_counts_by_verdict.get(v, 0) > 0
            )
            bal = snap.balances.get(p.provider)
            bal_err = snap.balance_errors.get(p.provider)
            if bal is not None:
                bal_str = f"balance ${bal.usd:.2f}"
            elif bal_err is not None:
                bal_str = f"balance ? ({bal_err})"
            else:
                bal_str = "balance N/A"
            lines.append(
                f"  {p.provider}: ${p.burn_rate_usd_per_hr:.2f}/hr  "
                f"spend ${p.spend_usd_total:.2f}  {bal_str}  [{counts_str}]"
            )
    if snap.heartbeat_partial_truth:
        lines.append("")
        lines.append(
            f"WARNING: heartbeat substrate not yet shipped for "
            f"{','.join(snap.heartbeat_partial_truth)} (B5b pending); "
            f"LIVE counts are upper-bound estimates."
        )
    if snap.hosted_spend_pending:
        lines.append("compute spend only (hosted spend deferred to B10)")
    if threshold_set:
        lines.append("replicate spend tracking pending B10")
    if snap.throttle_warnings:
        for w in snap.throttle_warnings:
            lines.append(f"WARNING: {w}")
    return "\n".join(lines) + "\n"


def _render_cost_json(snap) -> str:
    """Render the stable §10 JSON schema. Uses sort_keys=False to keep insertion order."""
    out = {
        "as_of": snap.as_of.isoformat(),
        "burn_rate_usd_per_hr": snap.burn_rate_usd_per_hr,
        "per_provider": [
            {
                "provider": p.provider,
                "burn_rate_usd_per_hr": p.burn_rate_usd_per_hr,
                "spend_usd_total": p.spend_usd_total,
                "pod_counts_by_verdict": {
                    v.value: p.pod_counts_by_verdict.get(v, 0) for v in Verdict
                },
            }
            for p in snap.per_provider
        ],
        "balance": {
            provider: (
                None if bal is None else {
                    "usd": bal.usd,
                    "as_of": bal.as_of.isoformat(),
                    "source": bal.source,
                    "currency": bal.currency,
                    "cached_age_s": 0,  # Task 6 will populate from cache
                }
            )
            for provider, bal in snap.balances.items()
        },
        "balance_errors": dict(snap.balance_errors),
        "heartbeat_partial_truth": list(snap.heartbeat_partial_truth),
        "hosted_spend_pending": snap.hosted_spend_pending,
        "throttle_warnings": list(snap.throttle_warnings),
    }
    return json.dumps(out, indent=2) + "\n"


def _render_cost_prom(snap, balance_errors) -> str:
    """Render Prometheus text exposition per spec §9."""
    lines = []

    def emit_help(metric: str, help_text: str, type_: str) -> None:
        lines.append(f"# HELP {metric} {help_text}")
        lines.append(f"# TYPE {metric} {type_}")

    emit_help(
        "kinoforge_burn_rate_usd_per_hr",
        "Sum of cost_rate_usd_per_hr across pod-up verdicts.",
        "gauge",
    )
    for p in snap.per_provider:
        lines.append(
            f'kinoforge_burn_rate_usd_per_hr{{provider="{p.provider}"}} '
            f"{p.burn_rate_usd_per_hr}"
        )

    emit_help(
        "kinoforge_balance_usd",
        "Provider-account balance, when a balance endpoint ships.",
        "gauge",
    )
    for provider, bal in snap.balances.items():
        if bal is not None:
            lines.append(
                f'kinoforge_balance_usd{{provider="{provider}"}} {bal.usd}'
            )

    emit_help(
        "kinoforge_balance_as_of_seconds",
        "Unix timestamp the balance was read (or cached).",
        "gauge",
    )
    for provider, bal in snap.balances.items():
        if bal is not None:
            lines.append(
                f'kinoforge_balance_as_of_seconds{{provider="{provider}"}} '
                f"{int(bal.as_of.timestamp())}"
            )

    emit_help(
        "kinoforge_pod_count",
        "Pod count per provider per verdict.",
        "gauge",
    )
    for p in snap.per_provider:
        for v in Verdict:
            count = p.pod_counts_by_verdict.get(v, 0)
            lines.append(
                f'kinoforge_pod_count{{provider="{p.provider}",'
                f'verdict="{v.value}"}} {count}'
            )

    emit_help(
        "kinoforge_spend_usd_total",
        "Lifetime $ spent on currently-up pods this provider.",
        "gauge",
    )
    for p in snap.per_provider:
        lines.append(
            f'kinoforge_spend_usd_total{{provider="{p.provider}"}} '
            f"{p.spend_usd_total}"
        )

    emit_help(
        "kinoforge_cost_scrape_errors_total",
        "Failed balance reads since process start.",
        "counter",
    )
    # Per spec §9: emit zeros for every provider in the snapshot, plus
    # the reason axis derived from balance_errors content.
    for p in snap.per_provider:
        for reason in ("transport", "schema", "cred"):
            err = balance_errors.get(p.provider, "")
            value = 1 if (reason in err.lower() or
                          (reason == "cred" and "credential" in err.lower())) else 0
            lines.append(
                f'kinoforge_cost_scrape_errors_total{{provider="{p.provider}",'
                f'reason="{reason}"}} {value}'
            )

    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Run tests to verify GREEN (Task 5 portion)**

Run: `pixi run pytest tests/cli/test_cmd_cost.py -v -k "not cache"`
Expected: all non-cache tests PASS. Cache-specific tests stay RED for Task 6.

- [ ] **Step 6: Run lint + type-check + invariants**

Run: `pixi run pre-commit run --files src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_cost.py && pixi run pytest tests/test_core_invariant.py -v`
Expected: hooks PASS; core invariant tests stay GREEN.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_cost.py
git commit -m "feat(b2): kinoforge cost subcommand + human/JSON/Prom renderers

_cmd_cost wires ledger walk + per-provider classify + balance dispatch
+ aggregate + render. --json --prom mutex; live_pod_ids fallback on
list_instances failure; replicate throttle stub (no entries until B10).
Cache helper stub raises NotImplementedError until Task 6.
"
```

---

## Task 6: Balance disk cache — `cached_balance_read`

**Goal:** Replace the Task-5 stub with the real `cached_balance_read` implementing TTL-gated read, stale-fallback, and `--no-cache` bypass; covered across `LocalArtifactStore` + mocked-S3.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (replace `cached_balance_read` stub)
- Create: `tests/cli/test_balance_cache.py`

**Acceptance Criteria:**
- [ ] Cache miss path: fresh fetch via endpoint → writes `<store>/_cost_cache/cost/balance_<provider>.json` → next read within TTL returns the cached value (spy: `endpoint.read.call_count == 1` across two invocations).
- [ ] Cache hit path: cached entry within TTL → `endpoint.read` not called.
- [ ] Cache stale path: cached entry older than TTL → fresh fetch + cache update.
- [ ] Stale-fallback: cached entry exists; fresh fetch raises `TransportError` → cached value returned + error message populated with `"transport (using cache): ..."`.
- [ ] `--no-cache`: `endpoint.read` called every invocation; no cache write.
- [ ] Cache-write failure (e.g. filesystem error) does NOT propagate; logged WARNING; fresh value still returned.
- [ ] Schema: cached entry has all 5 keys (`usd`, `as_of`, `source`, `currency`, `cached_at`); ISO local-TZ timestamps.
- [ ] Parametrize across `LocalArtifactStore` + `FakeMockedS3Client` (mirrors Phase 38 pattern at `tests/stores/conftest.py`).

**Verify:** `pixi run pytest tests/cli/test_balance_cache.py -v` → all tests PASS.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_balance_cache.py`:

```python
"""Tests for B2 balance disk cache (cached_balance_read)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kinoforge.cli._commands import cached_balance_read
from kinoforge.core.balance_endpoints import ProviderBalance, TransportError


@pytest.fixture(params=["local", "mocked-s3"])
def store_fixture(request, tmp_path):
    """Parametrize cache tests across LocalArtifactStore + FakeMockedS3Client.

    Mirrors Phase 38 pattern at tests/stores/conftest.py.
    """
    if request.param == "local":
        from kinoforge.stores.local import LocalArtifactStore

        yield LocalArtifactStore(root=tmp_path / "store")
    else:
        # Defer to existing FakeMockedS3Client wiring
        from tests.stores.conftest import build_mocked_s3_store

        yield build_mocked_s3_store(tmp_path)


def _now_minus(seconds: float) -> datetime:
    return datetime.now() - timedelta(seconds=seconds)


def test_cache_miss_writes_fresh(store_fixture) -> None:
    pb = ProviderBalance(usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    assert bal is pb
    assert err is None
    assert endpoint.read.call_count == 1


def test_cache_hit_within_ttl_skips_endpoint(store_fixture) -> None:
    pb = ProviderBalance(usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    # First call: cache miss
    cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    # Second call within TTL: cache hit; endpoint NOT called
    bal, err = cached_balance_read(
        store=store_fixture,
        provider="runpod",
        endpoint=endpoint,
        cache_ttl_s=15.0,
        no_cache=False,
        now=datetime.now(),
    )
    assert bal is not None
    assert bal.usd == 42.18
    assert err is None
    assert endpoint.read.call_count == 1  # Did NOT increment


def test_cache_stale_beyond_ttl_refetches(store_fixture) -> None:
    pb_old = ProviderBalance(usd=10.0, as_of=_now_minus(60), source="runpod-graphql-clientBalance")
    pb_new = ProviderBalance(usd=20.0, as_of=datetime.now(), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.side_effect = [pb_old, pb_new]

    base_time = datetime.now()
    cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=False, now=base_time,
    )
    # 30s later: cache stale
    bal, err = cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=False, now=base_time + timedelta(seconds=30),
    )
    assert bal is not None
    assert bal.usd == 20.0
    assert endpoint.read.call_count == 2


def test_stale_fallback_on_transport_error(store_fixture) -> None:
    """BUG CATCH: when fresh fetch fails, cached value MUST be returned
    rather than None, so the dashboard keeps showing the last-known value."""
    pb = ProviderBalance(usd=42.18, as_of=_now_minus(60), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.side_effect = [pb, TransportError("simulated")]

    base = datetime.now()
    cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=False, now=base,
    )
    bal, err = cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=False, now=base + timedelta(seconds=30),
    )
    assert bal is not None
    assert bal.usd == 42.18
    assert err is not None
    assert "transport (using cache)" in err


def test_no_cache_skips_read_and_write(store_fixture) -> None:
    pb = ProviderBalance(usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=True, now=datetime.now(),
    )
    # Second invocation: still hits endpoint (no cache to consult)
    cached_balance_read(
        store=store_fixture, provider="runpod", endpoint=endpoint,
        cache_ttl_s=15.0, no_cache=True, now=datetime.now(),
    )
    assert endpoint.read.call_count == 2


def test_cache_write_failure_does_not_raise(store_fixture, caplog) -> None:
    """If put_json fails (disk full, S3 5xx), still return the fresh value."""
    pb = ProviderBalance(usd=42.18, as_of=datetime.now(), source="runpod-graphql-clientBalance")
    endpoint = MagicMock()
    endpoint.read.return_value = pb

    broken_store = MagicMock(wraps=store_fixture)
    broken_store.get_bytes.side_effect = FileNotFoundError()
    broken_store.put_json.side_effect = OSError("disk full")

    with caplog.at_level("WARNING"):
        bal, err = cached_balance_read(
            store=broken_store, provider="runpod", endpoint=endpoint,
            cache_ttl_s=15.0, no_cache=False, now=datetime.now(),
        )
    assert bal is pb
    assert any("cache write" in rec.message.lower() for rec in caplog.records)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/cli/test_balance_cache.py -v`
Expected: most tests FAIL with `NotImplementedError` from the Task 5 stub.

- [ ] **Step 3: Implement `cached_balance_read`**

Replace the stub in `src/kinoforge/cli/_commands.py`:

```python
_COST_CACHE_RUN_ID = "_cost_cache"


def cached_balance_read(
    *,
    store: ArtifactStore,
    provider: str,
    endpoint: BalanceEndpoint,
    cache_ttl_s: float,
    no_cache: bool,
    now: datetime,
) -> tuple[ProviderBalance | None, str | None]:
    """TTL-gated balance read with stale-fallback.

    See spec §8 for the contract. Cache key:
    ``<store>/<_COST_CACHE_RUN_ID>/cost/balance_<provider>.json``.

    Returns:
        (balance, error_message). Either may be None. When the cached
        entry is returned because the fresh fetch failed, both are
        non-None: balance carries the cached value, error explains why
        the fresh fetch fell back.
    """
    name = f"cost/balance_{provider}.json"
    cached: dict | None = None
    if not no_cache:
        try:
            raw = store.get_bytes(_COST_CACHE_RUN_ID, name)
            cached = json.loads(raw.decode())
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            cached = None
        if cached is not None:
            try:
                cached_at = datetime.fromisoformat(cached["cached_at"])
            except (KeyError, TypeError, ValueError):
                cached = None
            else:
                age_s = (now - cached_at).total_seconds()
                if age_s < cache_ttl_s:
                    return _balance_from_cache(cached), None

    try:
        fresh = endpoint.read()
    except TransportError as exc:
        if cached is not None:
            return _balance_from_cache(cached), f"transport (using cache): {exc}"
        return None, f"transport: {exc}"

    if fresh is None:
        return None, None
    if not no_cache:
        try:
            store.put_json(
                _COST_CACHE_RUN_ID,
                name,
                {
                    "usd": fresh.usd,
                    "as_of": fresh.as_of.isoformat(),
                    "source": fresh.source,
                    "currency": fresh.currency,
                    "cached_at": now.isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cost: cache write failed for %s: %s; "
                "returning fresh value without cache",
                provider,
                exc,
            )
    return fresh, None


def _balance_from_cache(cached: dict) -> ProviderBalance:
    return ProviderBalance(
        usd=float(cached["usd"]),
        as_of=datetime.fromisoformat(cached["as_of"]),
        source=str(cached["source"]),
        currency=str(cached.get("currency", "USD")),
    )
```

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pixi run pytest tests/cli/test_balance_cache.py tests/cli/test_cmd_cost.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Run lint + type-check**

Run: `pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/test_balance_cache.py`
Expected: hooks PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_balance_cache.py
git commit -m "feat(b2): balance disk cache with TTL + stale-fallback

cached_balance_read writes <store>/_cost_cache/cost/balance_<p>.json.
TTL default 15s; --no-cache bypasses read AND write. Stale-fallback
returns cached value on fresh-fetch failure with annotated error.
Parametrized across LocalArtifactStore + mocked-S3.
"
```

---

## Task 7: Prom exposition assembly — scrape-error counter + heartbeat_partial_truth gate

**Goal:** Wire `kinoforge_cost_scrape_errors_total` counter to balance_errors content and consult `provider_heartbeat_supported` for `heartbeat_partial_truth`. Format-lock test asserts every required line emits with HELP + TYPE.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (refine `_render_cost_prom` from Task 5 sketch)
- Modify: `tests/cli/test_cmd_cost.py` (add Prom format-lock test cases)

**Acceptance Criteria:**
- [ ] `kinoforge_cost_scrape_errors_total{provider=X, reason=Y}` emits with `reason` ∈ `{"transport", "schema", "cred"}` for every provider with at least one ledger entry. Value is 1 when the balance_errors string matches that reason prefix, else 0.
- [ ] `provider_heartbeat_supported(kind)` consulted to populate `heartbeat_partial_truth`. Today: SkyPilot returns False → SkyPilot in tuple when any SkyPilot entry is in ledger.
- [ ] Exposition is line-by-line stable: HELP, TYPE, then samples, exactly per §9.
- [ ] Trailing newline at end of exposition (`\n` after last sample).
- [ ] LF line endings (no CR).
- [ ] Existing `test_prom_mode_emits_all_gauges_and_help` from Task 5 still PASSES.

**Verify:** `pixi run pytest tests/cli/test_cmd_cost.py -v -k "prom"` → all PASS.

**Steps:**

- [ ] **Step 1: Add new failing tests**

Append to `tests/cli/test_cmd_cost.py`:

```python
def test_prom_scrape_errors_counter_emits_per_provider_per_reason(fake_ctx, capsys) -> None:
    ctx, _ = fake_ctx
    entry = {
        "id": "pod-abc",
        "provider": "runpod",
        "cost_rate_usd_per_hr": 0.79,
        "created_at": (datetime.now() - timedelta(hours=1)).timestamp(),
        "tags": {},
    }
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = [entry]
        with patch("kinoforge.core.registry.get_provider") as get_prov:
            prov_inst = MagicMock()
            prov_inst.list_instances.return_value = [MagicMock(id="pod-abc")]
            get_prov.return_value = lambda: prov_inst
            with patch("kinoforge._adapters.build_balance_endpoint_for") as build_bal:
                from kinoforge.core.balance_endpoints import TransportError

                ep = MagicMock()
                ep.read.side_effect = TransportError("simulated")
                build_bal.return_value = ep
                rc = _cmd_cost(_args(prom=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    # Every reason axis emitted; transport=1, others=0
    assert 'kinoforge_cost_scrape_errors_total{provider="runpod",reason="transport"} 1' in out
    assert 'kinoforge_cost_scrape_errors_total{provider="runpod",reason="schema"} 0' in out
    assert 'kinoforge_cost_scrape_errors_total{provider="runpod",reason="cred"} 0' in out


def test_prom_heartbeat_partial_truth_skypilot(fake_ctx, capsys) -> None:
    """SkyPilot in ledger AND provider_heartbeat_supported('skypilot') is False
    → heartbeat_partial_truth includes skypilot in --json AND footer in human."""
    ctx, _ = fake_ctx
    entry = {
        "id": "sky-1",
        "provider": "skypilot",
        "cost_rate_usd_per_hr": 1.20,
        "created_at": (datetime.now() - timedelta(hours=1)).timestamp(),
        "tags": {},
    }
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = [entry]
        with patch("kinoforge.core.registry.get_provider") as get_prov:
            prov_inst = MagicMock()
            prov_inst.list_instances.return_value = [MagicMock(id="sky-1")]
            get_prov.return_value = lambda: prov_inst
            rc = _cmd_cost(_args(json=True), ctx)
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "skypilot" in payload["heartbeat_partial_truth"]


def test_prom_lf_only_no_cr(fake_ctx, capsys) -> None:
    """BUG CATCH: prom exposition MUST be LF-only (Prometheus textfile
    collector strict about line endings)."""
    ctx, _ = fake_ctx
    with patch.object(ctx, "ledger") as mock_ledger:
        mock_ledger.return_value.entries.return_value = []
        rc = _cmd_cost(_args(prom=True), ctx)
    out = capsys.readouterr().out
    assert "\r" not in out
    assert out.endswith("\n")
```

- [ ] **Step 2: Run tests, verify failure**

Run: `pixi run pytest tests/cli/test_cmd_cost.py::test_prom_scrape_errors_counter_emits_per_provider_per_reason tests/cli/test_cmd_cost.py::test_prom_heartbeat_partial_truth_skypilot -v`
Expected: FAIL on the new assertions if Task 5's sketch missed the per-reason emission OR LF-only invariant.

- [ ] **Step 3: Refine `_render_cost_prom`**

Audit the implementation from Task 5. Confirm:
- Every line ends with `\n` (no `\r\n`).
- The counter loop emits all three reasons unconditionally.
- HELP / TYPE before samples, in stable order.
- No empty trailing series.

If any test failed, adjust the renderer to satisfy.

- [ ] **Step 4: Run tests to verify GREEN**

Run: `pixi run pytest tests/cli/test_cmd_cost.py -v -k "prom"`
Expected: all PASS.

- [ ] **Step 5: Run lint + type-check**

Run: `pixi run pre-commit run --files src/kinoforge/cli/_commands.py tests/cli/test_cmd_cost.py`
Expected: hooks PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_cmd_cost.py
git commit -m "feat(b2): prom scrape_errors_total counter + LF-only invariant

_render_cost_prom emits scrape_errors per (provider, reason) for the
three reason axes, derived from balance_errors content.
heartbeat_partial_truth populated via B5a provider_heartbeat_supported.
Exposition is strict LF; trailing newline always present.
"
```

---

## Task 8: Examples + README + project closeout

**Goal:** Operator-facing entry points + project docs reflect B2 as shipped; PROGRESS, warm-reuse-tasks.txt, Layer V spec all updated.

**Files:**
- Create: `examples/configs/cost.yaml`
- Modify: `tests/test_examples.py` (add cost.yaml case)
- Modify: `README.md` (add Cost dashboard section)
- Modify: `PROGRESS.md` (strike B2 with commit sha)
- Modify: `warm-reuse-tasks.txt` (flip "design APPROVED" → "CLOSED commit <sha>")
- Modify: `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md` (amend §6 to point at B2)

**Acceptance Criteria:**
- [ ] `examples/configs/cost.yaml` loads via `kinoforge.core.config.load_config` without error and passes `tests/test_examples.py`.
- [ ] `README.md` has a new "Cost dashboard" H2 section after "Operator heartbeat semantics", documenting the three modes + cache flags + env-var + textfile-collector cron pattern.
- [ ] `PROGRESS.md` Section B has B2 struck (`~~B2.~~ — CLOSED commit <sha>`) with a one-line summary mirroring B4/B7 entries.
- [ ] `warm-reuse-tasks.txt` B2 entry's "Status:" line flips from `design APPROVED 2026-06-12` to `CLOSED commit <sha>`.
- [ ] Layer V spec §6 grows a bullet `- B2 (Layer X): cost dashboard — closed by commit <sha>; consults Layer V verdicts for the per-verdict breakdown.`

**Verify:** `pixi run pytest tests/test_examples.py -v && pixi run pre-commit run --all-files`
Expected: all examples load; all hooks PASS.

**Steps:**

- [ ] **Step 1: Write the example config**

Create `examples/configs/cost.yaml`:

```yaml
# kinoforge cost dashboard — minimal config.
#
# `kinoforge cost` walks the ledger associated with this config and
# emits a burn rate + per-provider breakdown + (when RunPod compute is
# configured + RUNPOD_API_KEY set) the account balance.
#
# Usage:
#   pixi run kinoforge cost -c examples/configs/cost.yaml
#   pixi run kinoforge cost -c examples/configs/cost.yaml --json
#   pixi run kinoforge cost -c examples/configs/cost.yaml --prom
#
# Textfile-collector cron pattern (write the prom exposition every
# 30 seconds for Prometheus to scrape):
#   */30 * * * * pixi run kinoforge cost -c .../cost.yaml --prom \
#       > /var/lib/node_exporter/textfile/kinoforge.prom

engine:
  kind: fake  # the dashboard does not run the engine; this is config-load-only

compute:
  provider: runpod
  lifecycle:
    idle_timeout_s: 600
    max_lifetime_s: 14400

store:
  kind: local
```

- [ ] **Step 2: Wire the example into the loader test**

Add a parametrize case to `tests/test_examples.py` (find the existing list of example configs and append `"cost.yaml"`).

- [ ] **Step 3: Run example test**

Run: `pixi run pytest tests/test_examples.py -v`
Expected: cost.yaml case passes.

- [ ] **Step 4: Write the README section**

Append to `README.md` after the "Operator heartbeat semantics" section:

````markdown
## Cost dashboard

`kinoforge cost` reads the ledger, classifies each entry against the
Layer V verdict set, and renders a cost view in one of three modes:

```bash
pixi run kinoforge cost              # human-readable table
pixi run kinoforge cost --json       # stable JSON schema (Grafana / jq)
pixi run kinoforge cost --prom       # Prometheus text exposition
```

### Balance read-out (RunPod only today)

When `compute.provider: runpod` is configured and `RUNPOD_API_KEY` is
set, the dashboard hits the RunPod GraphQL `{ myself { clientBalance } }`
query once per provider and renders:

```
RunPod balance: $42.18 (as of 14:32:01 PST)
Burn rate:      $0.79/hr (1 LIVE pod, RunPod A5000)
Runway:         ~53 h at current burn
```

Other providers render `balance: N/A` until a satisfier ships
(Replicate, Runway, Luma do not expose a balance API; Bedrock /
Vertex / SkyPilot deferred — see
`docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md` §13).

### Caching

Balance reads cache to `<store>/_cost_cache/cost/balance_<provider>.json`
with a 15-second default TTL so `watch -n 2 kinoforge cost` does not
burn the RunPod GraphQL rate limit. Override with `--cache-ttl=N` or
disable with `--no-cache`. The cached value is rendered as the source
of truth and a `(stale, transport error)` annotation appears when a
fresh fetch fails but a cache entry still exists.

### Prometheus textfile-collector cron pattern

```cron
*/30 * * * * pixi run kinoforge cost --prom \
    > /var/lib/node_exporter/textfile/kinoforge.prom
```

Five gauges + one counter, all `kinoforge_*`-prefixed, with `provider`
and (where appropriate) `verdict` labels:

```
kinoforge_burn_rate_usd_per_hr{provider="runpod"}
kinoforge_balance_usd{provider="runpod"}
kinoforge_balance_as_of_seconds{provider="runpod"}
kinoforge_pod_count{provider="runpod", verdict="LIVE"}
kinoforge_spend_usd_total{provider="runpod"}
kinoforge_cost_scrape_errors_total{provider="runpod", reason="transport"}
```

### Replicate throttle warning

Set `KINOFORGE_REPLICATE_THROTTLE_AT_USD=N` to warn when Replicate
spend exceeds 90% of `N`. Default `4.50` (90% of Replicate's
documented $5 free-tier soft-throttle); set `0` to disable. Note: until
B10 (per-prediction hosted spend capture) ships, hosted-engine spend is
not in the ledger and the warning footer reads `replicate spend
tracking pending B10`.
````

- [ ] **Step 5: Strike B2 in PROGRESS.md**

Get the commit sha of t8 once it lands (or use a placeholder during the work). After Step 7's commit, return and replace the B2 entry in `PROGRESS.md` Section B with:

```
- ~~**B2. Layer X — cost dashboard / metrics consumer.**~~ — CLOSED. Spec at
  `docs/superpowers/specs/2026-06-12-b2-cost-dashboard-design.md`; plan at
  `docs/superpowers/plans/2026-06-12-b2-cost-dashboard.md`. Ships `kinoforge
  cost` (human / `--json` / `--prom`) reading ledger + classify + RunPod
  GraphQL `clientBalance`. Disk cache TTL 15s default; replicate throttle
  stub wired RED for B10. Closed by commit `<sha>`.
```

- [ ] **Step 6: Flip warm-reuse-tasks.txt B2 entry**

Locate `Status: design APPROVED 2026-06-12.` near the top of the B2 block in `warm-reuse-tasks.txt`. Replace with:

```
Status: CLOSED commit <sha>.
```

- [ ] **Step 7: Amend Layer V spec §6**

Open `docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md`, locate §6, add a bullet:

```
- B2 (Layer X) — cost dashboard. CLOSED commit <sha>. Consumes Layer V
  `classify` verdicts for the per-verdict burn-rate column breakdown and
  per-provider aggregation.
```

- [ ] **Step 8: Run all-files pre-commit + full test suite**

Run: `pixi run pre-commit run --all-files && pixi run pytest -q`
Expected: every hook + every test PASS.

- [ ] **Step 9: Commit closeout**

```bash
git add examples/configs/cost.yaml tests/test_examples.py README.md PROGRESS.md \
        warm-reuse-tasks.txt docs/superpowers/specs/2026-06-06-layer-v-heartbeat-aware-reaper-design.md
git commit -m "docs(b2): closeout + example config + README section

examples/configs/cost.yaml + Cost dashboard README section + PROGRESS.md
B2 strikethrough + warm-reuse-tasks.txt status flip + Layer V spec §6
forward-pointer. Layer X SHIPS.
"
```

After this commit lands, replace the `<sha>` placeholders in PROGRESS.md / warm-reuse-tasks.txt / Layer V spec with the actual commit sha and amend (a single amend on this commit is acceptable since it is the closeout, not a feature commit) OR follow with a tiny doc-only `chore(b2): pin commit sha` commit. The amend is simpler; either is acceptable.

---

## Self-Review

Pass per the writing-plans skill checklist:

**Spec coverage (§-by-§):**

| Spec § | Task | Coverage |
|---|---|---|
| §1 brainstorm locks | t1, t2, t4, t5, t6 | A(a)→t4 `_BURNING_VERDICTS`; A(b)→t4 aggregation key set; A(c)→t5/t7 Prom layout; A(d)→t6 cache TTL + flags; A(e)→t5 env-var read; A(f)→t1 ProviderBalance |
| §2 module split | t1–t6 | every path enumerated in File Structure table |
| §3 substrate | t1 | all 7 ACs in test file map to spec invariants |
| §4 RunPod satisfier | t2 | 9 parametrize cases cover happy / cred / 5 schema-drift / transport / negative |
| §5 registry | t3 | 11 cases cover RunPod / non-RunPod / hosted-engine / missing-cred / unknown |
| §6 aggregator | t4 | 12 cases cover empty / live / stale-excluded / sorted / bad-entry / verdict-keys-present / passthrough / frozen / hosted-spend / throttle-passthrough |
| §7 CLI | t5 | 8 cases cover empty / json-shape / mutex / prom-help / failure-doesnt-block / list_instances-fallback / throttle-stub / throttle-disabled |
| §8 cache | t6 | 6 cases cover miss / hit / stale / stale-fallback / no-cache / write-failure |
| §9 Prom | t5+t7 | 5 gauges + 1 counter; help/type/labels/LF-only |
| §10 JSON | t5 | json-shape lock + every §10 required key |
| §11 throttle | t5 | env-var read + stub footer until B10 |
| §12 failure modes | t5+t6 | parametrize one case per row of §12 table |
| §13 matrix | t8 | README reproduces the matrix verbatim |
| §14 test surface | t1–t6 | every numbered test file exists |
| §15 task envelope | t1–t8 | 8 tasks delivered |
| §16 forward consumers | t8 | README + Layer V spec amendment carry the forward hooks |
| §17 risk register | t1–t6 | every risk has a mitigation step in its task |
| §18 live spend | (all) | $0 — fixture captured by operator in t2 |

No gaps.

**Placeholder scan:** No `TBD`, `TODO`, or "handle edge cases" deferrals. Every step contains concrete code. The `<sha>` placeholder in t8 is an intentional pin-after-commit pattern, not a TBD.

**Type consistency:** `ProviderBalance(usd, as_of, source, currency)` consistent in t1 def, t2 construction, t4 aggregator pass-through, t5 render. `BalanceEndpoint.read() -> ProviderBalance | None` consistent across t1 Protocol, t2 satisfier, t3 dispatch, t5 CLI usage, t6 cache helper. `_BURNING_VERDICTS` only defined in t4; t5 imports from `kinoforge.core.cost`. `cached_balance_read` signature identical between t5 stub and t6 implementation.

**User-gate detection:** scanned acceptance criteria + spec text. No Nouns / Scope / Proof matches; only routine "verify" verbs (TDD red/green). No `userGate: true` tagging required.

---

## Execution Handoff

Plan ready. Awaiting execution mode selection.
