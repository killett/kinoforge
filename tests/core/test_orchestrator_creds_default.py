"""Regression — :func:`kinoforge.core.orchestrator.generate` must default
``creds`` to :class:`EnvCredentialProvider` when the caller omits it.

Background: prior to this regression the CLI's ``_cmd_generate`` called
``generate(...)`` without ``creds=``, leaving ``creds=None``. That
propagated to
:func:`kinoforge.core.orchestrator._provision_instance_and_build_backend`
where the ``env_required`` cred check ran ``value = creds.get(var) if
creds is not None else None`` → ``None`` → :class:`AuthError` on the
first required env var. Live CLI invocations of any compute-bound engine
(ComfyUI on RunPod, etc.) consequently died with
``AuthError: missing required env var: HF_TOKEN`` despite the ``.env``
loader having populated ``os.environ`` correctly.

The fix lives at :mod:`kinoforge.core.orchestrator.generate`: when
``creds is None``, the function now instantiates
:class:`EnvCredentialProvider`. This test spies on
``_provision_instance_and_build_backend`` and asserts the helper sees a
non-``None`` :class:`EnvCredentialProvider` instance — locking the
default-shim behavior so a future regression cannot resurface silently.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.interfaces import GenerationRequest
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# Re-use the existing orchestrator test scaffolding for cfg + engine. The
# imports below mirror the patterns in tests/core/test_orchestrator.py.
from tests.core.test_orchestrator import _compute_cfg, _make_engine


def test_generate_defaults_creds_to_env_credential_provider(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling ``generate(...)`` without ``creds=`` must hand
    :class:`EnvCredentialProvider` to the provisioner.

    Bug it catches: the CLI omitting ``creds=`` (and any other caller
    that does the same) lands a ``None`` at the env-required cred check
    inside :func:`_provision_instance_and_build_backend`, which raises
    :class:`AuthError` on the first env var even though
    :class:`EnvCredentialProvider` would have resolved it from
    ``os.environ``.
    """
    captured: dict[str, Any] = {}
    real_helper = orchestrator._provision_instance_and_build_backend

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured["creds"] = kwargs.get("creds")
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_provision_instance_and_build_backend", spy)

    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(root=tmp_path / "artifacts")
    request = GenerationRequest(prompt="hi", mode="t2v")

    # Intentionally OMIT creds= so the orchestrator default-shim fires.
    orchestrator.generate(cfg, request, store=store, provider=provider, engine=engine)

    assert "creds" in captured, (
        "_provision_instance_and_build_backend was never called — "
        "the spy didn't run; revisit the test scaffolding"
    )
    seen = captured["creds"]
    assert seen is not None, (
        "creds reached the provisioner as None — orchestrator did not "
        "default it; the CLI bug (HF_TOKEN AuthError despite .env) lives "
        "on. Expected an EnvCredentialProvider instance."
    )
    assert isinstance(seen, EnvCredentialProvider), (
        f"creds defaulted to {type(seen).__name__}, expected "
        f"EnvCredentialProvider — the default-shim shape regressed"
    )


def test_generate_preserves_explicit_creds(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``creds=`` must not be replaced by the default-shim.

    Catches the inverse bug: a future default-shim implementation that
    unconditionally overwrites whatever the caller passed.
    """
    captured: dict[str, Any] = {}
    real_helper = orchestrator._provision_instance_and_build_backend

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured["creds"] = kwargs.get("creds")
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_provision_instance_and_build_backend", spy)

    cfg = _compute_cfg()
    engine = _make_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(root=tmp_path / "artifacts")
    request = GenerationRequest(prompt="hi", mode="t2v")

    sentinel = EnvCredentialProvider()
    orchestrator.generate(
        cfg,
        request,
        store=store,
        provider=provider,
        engine=engine,
        creds=sentinel,
    )

    assert captured.get("creds") is sentinel, (
        "explicit creds was replaced — default-shim must only fire on None"
    )
