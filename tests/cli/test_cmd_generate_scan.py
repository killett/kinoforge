"""B3 Task e — _cmd_generate scan dispatch + precedence."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge.cli  # noqa: F401 — registers `generate` re-export
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.cli._commands import _cmd_generate, _ScanReport
from kinoforge.cli.context import SessionContext
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import Instance

_CFG_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake-base.safetensors"
    kind: base
    target: diffusion_models
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
"""


def _make_ctx(tmp_path: Path) -> SessionContext:
    p = tmp_path / "cfg.yaml"
    p.write_text(_CFG_YAML)
    cfg = load_config(p)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return SessionContext(state_dir=state_dir, cfg=cfg, sidecar=None)


def _make_args(
    *,
    instance_id: str | None = None,
    no_reuse: bool = False,
    force_attach: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        prompt="x",
        mode="t2v",
        run_id="r-1",
        instance_id=instance_id,
        force_attach=force_attach,
        no_reuse=no_reuse,
        output_dir=None,
        no_output_dir=True,
    )


def _fake_instance(iid: str) -> Instance:
    return Instance(
        id=iid,
        provider="local",
        tags={},
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        status="ready",
        endpoints={},
    )


def _stub_generate(monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]) -> None:
    def _gen(cfg: Any, request: Any, **kw: Any) -> tuple[Any, Any]:
        captured.update(kw)
        artifact = MagicMock()
        artifact.uri = "test://x"
        return (artifact, None)

    monkeypatch.setattr("kinoforge.cli.generate", _gen, raising=False)
    monkeypatch.setattr("kinoforge.cli._commands.generate", _gen)


# ---------------------------------------------------------------------------
# Default path: scan dispatched
# ---------------------------------------------------------------------------


def test_generate_calls_scan_when_no_explicit_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: omitting scan would force every fresh shell to cold-create."""
    scan_called: list[bool] = []

    def spy_scan(ctx: Any, cfg: Any, **kw: Any) -> tuple[Any, _ScanReport]:
        scan_called.append(True)
        return (None, _ScanReport())

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", spy_scan)
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(_make_args(), _make_ctx(tmp_path))
    assert rc == 0
    assert scan_called == [True]


def test_generate_skips_scan_when_no_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: scanning under --no-reuse would defeat the explicit cold-create intent."""
    scan_called: list[bool] = []

    def spy_scan(ctx: Any, cfg: Any, **kw: Any) -> tuple[Any, _ScanReport]:
        scan_called.append(True)
        return (None, _ScanReport())

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", spy_scan)
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(_make_args(no_reuse=True), _make_ctx(tmp_path))
    assert rc == 0
    assert scan_called == []
    assert captured.get("single") is True


def test_generate_explicit_id_takes_precedence_over_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: scanning despite --instance-id would race operator's explicit choice."""
    scan_called: list[bool] = []

    def spy_scan(*a: Any, **kw: Any) -> tuple[Any, _ScanReport]:
        scan_called.append(True)
        return (None, _ScanReport())

    monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", spy_scan)
    monkeypatch.setattr(
        "kinoforge.cli._commands._resolve_warm_instance",
        lambda *a, **kw: (_fake_instance("explicit-pod"), None),
    )
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(_make_args(instance_id="explicit-pod"), _make_ctx(tmp_path))
    assert rc == 0
    assert scan_called == []
    assert captured["instance"].id == "explicit-pod"


# ---------------------------------------------------------------------------
# Scan hit → instance threaded through
# ---------------------------------------------------------------------------


def test_generate_scan_hit_threads_instance_to_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: not threading scan's instance into generate would re-cold-create."""
    hit = _fake_instance("warm-pod")
    monkeypatch.setattr(
        "kinoforge.cli._commands._scan_warm_candidates",
        lambda *a, **kw: (hit, _ScanReport(attached="warm-pod")),
    )
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)

    rc = _cmd_generate(_make_args(), _make_ctx(tmp_path))
    assert rc == 0
    assert captured["instance"] is hit


# ---------------------------------------------------------------------------
# Summary logging
# ---------------------------------------------------------------------------


def test_generate_logs_scan_summary_on_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug: silent attach would deprive operators of visibility into reuse."""
    monkeypatch.setattr(
        "kinoforge.cli._commands._scan_warm_candidates",
        lambda *a, **kw: (
            _fake_instance("warm-pod"),
            _ScanReport(attached="warm-pod"),
        ),
    )
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)
    with caplog.at_level(logging.INFO, logger="kinoforge.cli._commands"):
        _cmd_generate(_make_args(), _make_ctx(tmp_path))
    assert any(
        "warm-reuse: attached to warm-pod" in r.getMessage() for r in caplog.records
    )


def test_generate_logs_scan_summary_on_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug: silent cold-create with skips would hide diagnostic info."""
    monkeypatch.setattr(
        "kinoforge.cli._commands._scan_warm_candidates",
        lambda *a, **kw: (
            None,
            _ScanReport(skipped=[("pod-1", "reaper-held")]),
        ),
    )
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)
    with caplog.at_level(logging.INFO, logger="kinoforge.cli._commands"):
        _cmd_generate(_make_args(), _make_ctx(tmp_path))
    assert any(
        "0 attachable" in r.getMessage() and "reaper-held" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Mutex (already gated by Task d, regression-anchor here)
# ---------------------------------------------------------------------------


def test_generate_no_reuse_with_force_attach_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug: not enforcing mutex would let operators stumble into incoherent state."""
    rc = _cmd_generate(
        _make_args(no_reuse=True, force_attach=True), _make_ctx(tmp_path)
    )
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# D7: --no-reuse + --instance-id compose
# ---------------------------------------------------------------------------


def test_generate_no_reuse_with_instance_id_composes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: rejecting --no-reuse + --instance-id together would block D7 composition."""
    monkeypatch.setattr(
        "kinoforge.cli._commands._resolve_warm_instance",
        lambda *a, **kw: (_fake_instance("warm-pod"), None),
    )
    captured: dict[str, Any] = {}
    _stub_generate(monkeypatch, captured)
    rc = _cmd_generate(
        _make_args(instance_id="warm-pod", no_reuse=True), _make_ctx(tmp_path)
    )
    assert rc == 0
    assert captured["instance"].id == "warm-pod"
    assert captured["single"] is True
