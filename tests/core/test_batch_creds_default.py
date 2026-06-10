"""Regression — :func:`kinoforge.core.batch.batch_generate` must default
``creds`` to :class:`EnvCredentialProvider` when the caller omits it.

Sibling regression to
:mod:`tests.core.test_orchestrator_creds_default`. Same bug shape, same
fix shape, batched-call path. CLI's ``_cmd_batch`` called
``batch_generate(...)`` without ``creds=``, so ``creds=None``
propagated through ``deploy_session`` to
:func:`kinoforge.core.orchestrator._provision_instance_and_build_backend`
and raised :class:`AuthError` on the first ``env_required`` var.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.batch import batch_generate
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# Re-use the existing batch test scaffolding for cfg + spy engine + manifest.
from tests.core.test_batch_generate import (  # type: ignore[attr-defined]
    _compute_cfg,
    _make_spy_engine,
    _three_entry_manifest,
)


def test_batch_generate_defaults_creds_to_env_credential_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling ``batch_generate(...)`` without ``creds=`` must hand
    :class:`EnvCredentialProvider` to the provisioner.

    Same bug shape as the single-clip path; same fix surface.
    """
    captured: list[Any] = []
    real_helper = orchestrator._provision_instance_and_build_backend

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("creds"))
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_provision_instance_and_build_backend", spy)

    cfg = _compute_cfg()
    engine = _make_spy_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    assert captured, (
        "_provision_instance_and_build_backend never called — test scaffolding mismatch"
    )
    seen = captured[0]
    assert seen is not None, (
        "creds reached the provisioner as None — batch_generate did not "
        "default it; the CLI batch bug lives on."
    )
    assert isinstance(seen, EnvCredentialProvider), (
        f"creds defaulted to {type(seen).__name__}, expected EnvCredentialProvider"
    )


def test_batch_generate_preserves_explicit_creds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``creds=`` must not be replaced by the default-shim."""
    captured: list[Any] = []
    real_helper = orchestrator._provision_instance_and_build_backend

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs.get("creds"))
        return real_helper(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_provision_instance_and_build_backend", spy)

    cfg = _compute_cfg()
    engine = _make_spy_engine()
    provider = LocalProvider()
    store = LocalArtifactStore(tmp_path)

    sentinel = EnvCredentialProvider()
    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
        creds=sentinel,
    )

    assert captured, "spy not invoked"
    assert captured[0] is sentinel, (
        "explicit creds was replaced — default-shim must only fire on None"
    )
