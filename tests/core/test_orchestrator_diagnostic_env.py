"""C28 A1.5 — ``_build_diagnostic_env`` orchestrator-side overlay builder.

Validates the env-shape the orchestrator hands to ``build_instance_spec`` as
the ``diagnostic_env`` overlay (merged into ``InstanceSpec.env`` there, per
compute-seam S1 Task 7) when ``cfg.diagnostic_mode`` is True. AWS keys are
resolved via the boto3
default chain (so ``AWS_SHARED_CREDENTIALS_FILE`` is honoured per
``cloud_creds_workspace_local``).
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.orchestrator import (
    _DIAG_REGION_DEFAULT,
    _build_diagnostic_env,
)


class _FakeFrozen:
    def __init__(self, access_key: str, secret_key: str, token: str = "") -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = token


class _FakeCreds:
    def __init__(self, frozen: _FakeFrozen) -> None:
        self._frozen = frozen

    def get_frozen_credentials(self) -> _FakeFrozen:
        return self._frozen


class _FakeSession:
    def __init__(self, creds: _FakeCreds | None) -> None:
        self._creds = creds

    def get_credentials(self) -> _FakeCreds | None:
        return self._creds


def _patch_boto3(
    monkeypatch: pytest.MonkeyPatch,
    creds: _FakeCreds | None,
) -> None:
    fake_boto3 = type("boto3", (), {"Session": lambda: _FakeSession(creds)})
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)


def test_diagnostic_env_omits_bucket_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``KINOFORGE_DIAG_BUCKET`` in the env → no bucket key in the overlay.

    Bug caught: a hardcoded fallback bucket name. There used to be one, and
    it was a real bucket in a real account — which put that identifier into
    every diagnostic-mode pod env, every golden derived from it, and this
    test file. The in-pod trap already guards on
    ``[ -n "${KINOFORGE_DIAG_BUCKET:-}" ]``, so "absent" is the documented
    degradation (upload skipped), not a crash. Prefix and region still
    default: neither is an identifier of anything.
    """
    for k in (
        "KINOFORGE_DIAG_BUCKET",
        "KINOFORGE_DIAG_PREFIX",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(k, raising=False)
    _patch_boto3(
        monkeypatch,
        _FakeCreds(_FakeFrozen("AKIA-FIXTURE", "fixture-secret")),
    )

    overlay = _build_diagnostic_env("run-abc")

    assert "KINOFORGE_DIAG_BUCKET" not in overlay
    assert overlay["KINOFORGE_DIAG_PREFIX"] == "boot-logs/run-abc"
    assert overlay["AWS_DEFAULT_REGION"] == _DIAG_REGION_DEFAULT
    assert overlay["AWS_ACCESS_KEY_ID"] == "AKIA-FIXTURE"
    assert overlay["AWS_SECRET_ACCESS_KEY"] == "fixture-secret"
    assert "AWS_SESSION_TOKEN" not in overlay


def test_diagnostic_env_omits_bucket_when_set_to_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``KINOFORGE_DIAG_BUCKET=`` (present, empty) is treated as unset.

    Bug caught: a plain ``os.environ.get`` passes ``""`` through, and the
    in-pod trap's ``-n`` guard then correctly skips — but only after the
    controller has shipped a meaningless empty var and any later reader
    that keys on presence rather than emptiness has been misled. An
    operator's ``.env`` copied from ``.env.example`` has exactly this
    empty-assignment shape.
    """
    monkeypatch.setenv("KINOFORGE_DIAG_BUCKET", "")
    _patch_boto3(monkeypatch, None)

    overlay = _build_diagnostic_env("run-empty")

    assert "KINOFORGE_DIAG_BUCKET" not in overlay


def test_diagnostic_env_honours_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-supplied env overrides win for bucket/prefix/region."""
    monkeypatch.setenv("KINOFORGE_DIAG_BUCKET", "custom-bucket")
    monkeypatch.setenv("KINOFORGE_DIAG_PREFIX", "custom/prefix")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    _patch_boto3(
        monkeypatch,
        _FakeCreds(_FakeFrozen("AKIA-FIXTURE", "fixture-secret")),
    )

    overlay = _build_diagnostic_env("run-zzz")

    assert overlay["KINOFORGE_DIAG_BUCKET"] == "custom-bucket"
    assert overlay["KINOFORGE_DIAG_PREFIX"] == "custom/prefix"
    assert overlay["AWS_DEFAULT_REGION"] == "eu-west-1"


def test_diagnostic_env_includes_session_token_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STS-issued temp creds bring AWS_SESSION_TOKEN through."""
    _patch_boto3(
        monkeypatch,
        _FakeCreds(_FakeFrozen("ASIA-temp", "temp-secret", "sts-token-xyz")),
    )

    overlay = _build_diagnostic_env("run-temp")

    assert overlay["AWS_SESSION_TOKEN"] == "sts-token-xyz"


def test_diagnostic_env_omits_aws_keys_when_no_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No boto3 chain creds → AWS keys absent (trap PUT fails silently)."""
    monkeypatch.setenv("KINOFORGE_DIAG_BUCKET", "example-diag-bucket")
    _patch_boto3(monkeypatch, None)

    overlay = _build_diagnostic_env("run-no-creds")

    assert "AWS_ACCESS_KEY_ID" not in overlay
    assert "AWS_SECRET_ACCESS_KEY" not in overlay
    # Non-credential keys still populated so the trap upload path can be
    # exercised end-to-end once creds become available.
    assert overlay["KINOFORGE_DIAG_BUCKET"] == "example-diag-bucket"
    assert overlay["KINOFORGE_DIAG_PREFIX"] == "boot-logs/run-no-creds"


def test_diagnostic_env_value_shape_is_strings_only() -> None:
    """All overlay values must be plain str — they ride on GraphQL pod env."""
    overlay = _build_diagnostic_env("run-shape")
    for k, v in overlay.items():
        assert isinstance(v, str), (
            f"non-string overlay value at {k!r}: {type(v).__name__}"
        )


def _make_arg_capture() -> tuple[
    list[Any],
    Any,
]:  # for future orchestrator-level integration test
    captured: list[Any] = []

    def _capture(arg: Any) -> None:
        captured.append(arg)

    return captured, _capture
